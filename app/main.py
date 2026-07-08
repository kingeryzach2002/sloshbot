"""Sloshbot web app. Read-only over the DB except for feedback writes.

Run: uv run uvicorn app.main:app --reload
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from db import get_conn, init_db
from ingest.geocode import _CACHE_DDL, _lookup
from scoring.weights import (LENSES, MAX_PICKS, PRESETS, WEIGHTS, TIER_CONFIDENT,
                             TIER_MAYBE, composite, lens_weights, tier)

app = FastAPI(title="sloshbot")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["scoring_info"] = {
    "weights": WEIGHTS, "confident": TIER_CONFIDENT, "maybe": TIER_MAYBE,
}
init_db()


def get_settings() -> dict:
    with get_conn() as conn:
        return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}


def included_tags(settings: dict) -> list[str]:
    raw = settings.get("included_tags", "")
    return [t for t in raw.split(",") if t]


def all_tags() -> list[str]:
    with get_conn() as conn:
        return [r["tag"] for r in conn.execute("SELECT DISTINCT tag FROM event_tags ORDER BY tag")]


def active_weights(settings: dict) -> tuple[str, dict]:
    """Current (preset_name, weights) — a preset from settings, custom sliders,
    or the code default."""
    preset = settings.get("preset", "balanced")
    if preset == "custom":
        try:
            return "custom", {k: float(settings[f"weight_{k}"]) for k in WEIGHTS}
        except (KeyError, ValueError):
            return "balanced", WEIGHTS
    return preset, PRESETS.get(preset, WEIGHTS)


def home_coords(settings: dict) -> tuple[float, float] | None:
    try:
        return float(settings["home_lat"]), float(settings["home_lon"])
    except (KeyError, ValueError):
        return None


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def gcal_link(e: dict) -> str:
    """Pre-filled Google Calendar template link — a tentative hold, no OAuth."""
    def gfmt(iso: str) -> str:
        return datetime.fromisoformat(iso).strftime("%Y%m%dT%H%M%S")

    start = gfmt(e["starts_at"])
    end = gfmt(e["ends_at"]) if e["ends_at"] else gfmt(
        (datetime.fromisoformat(e["starts_at"]) + timedelta(hours=2)).isoformat())
    params = {
        "action": "TEMPLATE",
        "text": f"[hold] {e['title']}",
        "dates": f"{start}/{end}",
        "location": ", ".join(filter(None, [e["venue_name"], e["address"]])),
        "details": f"{e['url']}\n\nAdded by sloshbot (tentative hold)",
        "crm": "TENTATIVE",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def load_events(start: datetime, end: datetime,
                max_mi: float | None = None) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE starts_at >= ? AND starts_at < ? ORDER BY starts_at",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        score_rows = conn.execute("SELECT * FROM scores").fetchall()
        fb_rows = conn.execute("SELECT event_id, verdict FROM feedback").fetchall()
        tag_rows = conn.execute("SELECT event_id, tag FROM event_tags ORDER BY tag").fetchall()
        rep_rows = conn.execute(
            """SELECT ev.host_name AS host, f.verdict, count(*) AS n
               FROM feedback f JOIN events ev ON ev.id = f.event_id
               WHERE f.verdict IN ('as_promised', 'not_as_promised')
                 AND ev.host_name IS NOT NULL
               GROUP BY ev.host_name, f.verdict""").fetchall()

    scores: dict[str, dict] = defaultdict(dict)
    rationales: dict[str, dict] = defaultdict(dict)
    for s in score_rows:
        scores[s["event_id"]][s["scorer"]] = s["score"]
        rationales[s["event_id"]][s["scorer"]] = s["rationale"]
    feedback = defaultdict(set)
    for f in fb_rows:
        feedback[f["event_id"]].add(f["verdict"])
    tags = defaultdict(list)
    for t in tag_rows:
        tags[t["event_id"]].append(t["tag"])
    host_rep: dict[str, dict] = defaultdict(lambda: {"ok": 0, "miss": 0})
    for r in rep_rows:
        host_rep[r["host"]]["ok" if r["verdict"] == "as_promised" else "miss"] += r["n"]

    settings = get_settings()
    home = home_coords(settings)
    _, weights = active_weights(settings)
    inc_tags = set(included_tags(settings))
    events = []
    for r in rows:
        e = dict(r)
        e["distance_mi"] = (haversine_mi(home[0], home[1], e["lat"], e["lon"])
                            if home and e["lat"] is not None else None)
        if max_mi is not None and e["distance_mi"] is not None and e["distance_mi"] > max_mi:
            continue  # events with unknown location are never dropped by the filter
        e["tags"] = tags.get(e["id"], [])
        if inc_tags and e["tags"] and not (inc_tags & set(e["tags"])):
            continue  # tag filter is a hard filter; untagged events are never dropped by it
        e["scores"] = scores.get(e["id"], {})
        e["rationales"] = rationales.get(e["id"], {})
        e["composite"] = composite(e["scores"], weights)
        e["tier"] = tier(e["scores"], weights) if e["scores"] else "maybe"  # unscored -> maybe, never hidden
        e["feedback"] = feedback.get(e["id"], set())
        e["host_rep"] = host_rep.get(e["host_name"]) if e["host_name"] else None
        e["gcal"] = gcal_link(e)
        e["start_dt"] = datetime.fromisoformat(e["starts_at"])
        e["end_dt"] = (datetime.fromisoformat(e["ends_at"]) if e["ends_at"]
                       else e["start_dt"] + timedelta(hours=2))  # same default as gcal_link
        if e["tier"] != "hidden":
            events.append(e)
    # Hard cap on the top tier: only the MAX_PICKS best composites stay
    # "confident"; the overflow demotes to maybe. Scarcity is deliberate.
    picks = sorted((e for e in events if e["tier"] == "confident"),
                   key=lambda e: -e["composite"])
    for e in picks[MAX_PICKS:]:
        e["tier"] = "maybe"
    events.sort(key=lambda e: (e["starts_at"], -e["composite"]))
    return events


def group_by_day(events: list[dict]) -> list[tuple[str, list[dict]]]:
    days: dict[str, list] = defaultdict(list)
    for e in events:
        days[e["start_dt"].strftime("%A %b %-d")].append(e)
    return list(days.items())


# Calendar grid runs 8am–midnight; earlier/later events are clamped to the edges.
CAL_START_MIN = 8 * 60
CAL_END_MIN = 24 * 60
CAL_SPAN = CAL_END_MIN - CAL_START_MIN


def layout_day(events: list[dict]) -> list[dict]:
    """Position events in a day column: top/height as % of the grid, and a
    lane index so overlapping events sit side by side (Google Calendar style)."""
    blocks = []
    for e in events:
        s = e["start_dt"].hour * 60 + e["start_dt"].minute
        dur = int((e["end_dt"] - e["start_dt"]).total_seconds() // 60)
        end = min(s + max(dur, 30), CAL_END_MIN)  # 30min floor keeps blocks tappable
        s = max(s, CAL_START_MIN)
        if end <= s:
            continue
        blocks.append({"e": e, "s": s, "end": end})
    blocks.sort(key=lambda b: (b["s"], -b["end"]))

    # Greedy lane assignment within clusters of transitively-overlapping events.
    cluster: list[dict] = []
    cluster_end = 0

    def flush() -> None:
        lanes: list[int] = []  # occupied-until minute per lane
        for b in cluster:
            for i, lane_end in enumerate(lanes):
                if lane_end <= b["s"]:
                    lanes[i] = b["end"]
                    b["lane"] = i
                    break
            else:
                b["lane"] = len(lanes)
                lanes.append(b["end"])
        for b in cluster:
            b["n_lanes"] = len(lanes)

    for b in blocks:
        if cluster and b["s"] >= cluster_end:
            flush()
            cluster = []
        cluster.append(b)
        cluster_end = max(cluster_end, b["end"]) if len(cluster) > 1 else b["end"]
    if cluster:
        flush()

    for b in blocks:
        b["top"] = (b["s"] - CAL_START_MIN) / CAL_SPAN * 100
        b["height"] = (b["end"] - b["s"]) / CAL_SPAN * 100
        b["left"] = b["lane"] / b["n_lanes"] * 100
        b["width"] = 100 / b["n_lanes"]
    return blocks


@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request):
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = load_events(today, today + timedelta(days=7))
    by_date = defaultdict(list)
    for e in events:
        by_date[e["start_dt"].date()].append(e)
    days = []
    for i in range(7):
        d = today + timedelta(days=i)
        days.append({
            "label": d.strftime("%a"),
            "num": d.strftime("%-d"),
            "is_today": i == 0,
            "blocks": layout_day(by_date.get(d.date(), [])),
        })
    now_min = now.hour * 60 + now.minute
    return templates.TemplateResponse(request, "calendar.html", {
        "days": days,
        "view": "calendar",
        "hours": list(range(8, 24)),
        "now_pct": ((now_min - CAL_START_MIN) / CAL_SPAN * 100
                    if CAL_START_MIN <= now_min < CAL_END_MIN else None),
    })


def pending_debrief() -> dict | None:
    """Most recent held event that has ended and has no feedback yet."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            """SELECT h.event_id, h.lens, e.title, e.starts_at
               FROM holds h JOIN events e ON e.id = h.event_id
               WHERE COALESCE(e.ends_at, e.starts_at) < ?
                 AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.event_id = h.event_id)
               ORDER BY e.starts_at DESC LIMIT 1""", (now_iso,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["lens"] = d["lens"] or LENSES[0]
    d["when"] = datetime.fromisoformat(d["starts_at"]).strftime("%A")
    return d


@app.get("/", response_class=HTMLResponse)
def home(request: Request, lens: str = "", max_mi: float | None = None):
    """Hero view: the single best move tonight under the active lens."""
    if lens not in LENSES:
        lens = LENSES[0]
    now = datetime.now()
    events = load_events(now - timedelta(hours=1), now + timedelta(days=7), max_mi)

    settings = get_settings()
    _, base = active_weights(settings)
    rank_w = lens_weights(lens, base)
    for e in events:
        e["match"] = composite(e["scores"], rank_w)

    today = now.date()
    tonight_evts = sorted((e for e in events if e["start_dt"].date() == today),
                          key=lambda e: -e["match"])
    hero = backups = later = None
    hero_label = "tonight's pick"
    if tonight_evts:
        hero, backups = tonight_evts[0], tonight_evts[1:]
        if hero["tier"] != "confident":
            hero_label = "best available tonight"  # honest framing, never oversell
    else:
        upcoming = sorted(events, key=lambda e: (-e["match"], e["starts_at"]))
        if upcoming:
            hero, backups = upcoming[0], []
            hero_label = f"next up — {hero['start_dt'].strftime('%A')}"
    later = [e for e in events if e is not hero and (not tonight_evts or e["start_dt"].date() != today)]

    return templates.TemplateResponse(request, "home.html", {
        "view": "tonight",
        "lens": lens, "lenses": LENSES,
        "hero": hero, "hero_label": hero_label,
        "backups": backups,
        "later_days": group_by_day(later),
        "n_later": len(later),
        "debrief": pending_debrief(),
        "max_mi": max_mi,
        "has_home": home_coords(settings) is not None,
        "included_tags": included_tags(settings),
    })


@app.get("/week", response_class=HTMLResponse)
def week(request: Request, max_mi: float | None = None):
    now = datetime.now()
    events = load_events(now - timedelta(hours=3), now + timedelta(days=7), max_mi)
    settings = get_settings()
    preset, weights = active_weights(settings)
    return templates.TemplateResponse(request, "week.html", {
        "days": group_by_day(events),
        "view": "week",
        "max_mi": max_mi,
        "has_home": home_coords(settings) is not None,
        "preset": preset, "weights": weights, "presets": list(PRESETS),
        "n_confident": sum(1 for e in events if e["tier"] == "confident"),
        "all_tags": all_tags(), "included_tags": included_tags(settings),
    })


@app.get("/tonight")
def tonight():
    return RedirectResponse("/", status_code=307)  # tonight IS the home view now


@app.get("/map", response_class=HTMLResponse)
def map_view(request: Request, max_mi: float | None = None):
    now = datetime.now()
    events = load_events(now - timedelta(hours=3), now + timedelta(days=7), max_mi)
    settings = get_settings()
    home = home_coords(settings)
    pins = [{
        "lat": e["lat"], "lon": e["lon"], "title": e["title"],
        "tier": e["tier"], "url": e["url"], "gcal": e["gcal"],
        "when": e["start_dt"].strftime("%a %-I:%M %p"),
        "venue": e["venue_name"] or "",
        "booze": round(e["scores"].get("booze", 0) * 100),
        "distance": round(e["distance_mi"], 1) if e["distance_mi"] is not None else None,
    } for e in events if e["lat"] is not None]
    return templates.TemplateResponse(request, "map.html", {
        "view": "map",
        "pins": pins,
        "home": {"lat": home[0], "lon": home[1]} if home else None,
        "home_address": settings.get("home_address", ""),
        "max_mi": max_mi,
        "n_unmapped": sum(1 for e in events if e["lat"] is None),
    })


@app.post("/settings/tune")
def set_tuning(preset: str = Form(...),
               weight_booze: float | None = Form(None),
               weight_logistics: float | None = Form(None),
               back: str = Form("/")):
    pairs = [("preset", preset)]
    if preset == "custom":
        pairs += [("weight_booze", str(weight_booze or 0)),
                  ("weight_logistics", str(weight_logistics or 0))]
    with get_conn() as conn:
        conn.executemany("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", pairs)
    return RedirectResponse(back, status_code=303)


@app.post("/settings/tags")
async def set_tags(request: Request):
    """Tag filter: which event tags you're interested in right now. Stored in
    `settings`, not code, so it can change as often as your taste does."""
    form = await request.form()
    selected = form.getlist("tags")
    back = form.get("back") or "/week"
    with get_conn() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('included_tags', ?)",
                     (",".join(selected),))
    return RedirectResponse(back, status_code=303)


@app.post("/settings/tags/toggle")
def toggle_tag_filter(tag: str):
    """One-tap tag filtering: clicking a tag chip anywhere flips it in/out of
    the included_tags setting."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'included_tags'").fetchone()
        current = [t for t in (row["value"] if row else "").split(",") if t]
        if tag in current:
            current.remove(tag)
            state = "off"
        else:
            current.append(tag)
            state = "on"
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('included_tags', ?)",
                     (",".join(current),))
    return {"ok": True, "state": state}


@app.post("/settings/tags/clear")
def clear_tag_filter():
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE key = 'included_tags'")
    return {"ok": True}


@app.post("/settings/home")
def set_home(home_address: str = Form(...)):
    """Save home address and geocode it inline (single Nominatim call, cached)."""
    with get_conn() as conn:
        conn.execute(_CACHE_DDL)
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('home_address', ?)",
                     (home_address.strip(),))
        lat, lon = _lookup(conn, home_address.strip())
        if lat is not None:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('home_lat', ?)", (str(lat),))
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('home_lon', ?)", (str(lon),))
        else:
            conn.execute("DELETE FROM settings WHERE key IN ('home_lat', 'home_lon')")
    return RedirectResponse("/map", status_code=303)


# Verdicts that contradict each other: setting one clears the other.
# went/skipped are universal (lens ''); the promise pair is scoped to a lens.
FEEDBACK_PAIRS = {"went": "skipped", "skipped": "went",
                  "as_promised": "not_as_promised", "not_as_promised": "as_promised"}


@app.post("/feedback/{event_id}/{verdict}")
def toggle_feedback(event_id: str, verdict: str, lens: str = ""):
    """Toggle a verdict: on if absent, off if present. Clears its opposite."""
    assert verdict in FEEDBACK_PAIRS
    if verdict in ("went", "skipped"):
        lens = ""
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM feedback WHERE event_id = ? AND verdict = ? AND lens = ?",
            (event_id, verdict, lens),
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM feedback WHERE event_id = ? AND verdict = ? AND lens = ?",
                         (event_id, verdict, lens))
            return {"ok": True, "state": "off"}
        conn.execute("DELETE FROM feedback WHERE event_id = ? AND verdict = ? AND lens = ?",
                     (event_id, FEEDBACK_PAIRS[verdict], lens))
        conn.execute(
            "INSERT INTO feedback (event_id, verdict, lens, created_at) VALUES (?,?,?,?)",
            (event_id, verdict, lens, datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True, "state": "on"}


@app.post("/hold/{event_id}")
def record_hold(event_id: str, lens: str = ""):
    """Remember that the user placed a calendar hold — feeds the debrief."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO holds (event_id, lens, created_at) VALUES (?,?,?)
               ON CONFLICT(event_id) DO NOTHING""",
            (event_id, lens, datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True}

"""Sloshbot web app. Thin HTTP layer that COMPOSES the read pipeline
(data -> presenter -> filters -> policy) and renders it, plus the small write
paths (feedback, holds, settings). Read-only over the DB except those writes.

Public multi-user app: every visitor gets an anonymous identity on first
touch (random id in a signed session cookie, no login/password — see
app.auth) — settings/feedback/holds are scoped to that user_id. The event
catalog itself (events/scores/tags) is shared and crowdsourced across every
user, deliberately unscoped — see app/data.py.

  data.fetch_events   -> raw events + scores/tags/(this user's)feedback/host_rep
  presenter.enrich    -> start/end datetimes, distance, calendar link
  filters.apply       -> the hard filters (source/price/distance/tag/booze)
  policy.rank         -> composite/tier, source demotion, per-day cap, sort

Run: uv run uvicorn app.main:app --reload
"""
import html
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app import data, filters, policy, presenter
from app.admin import router as admin_router
from app.auth import current_user
from app.filters import FilterState
from app.models import Event
from db import get_conn, init_db
from ingest.geocode import geocode
from scoring.weights import (LENS_META, LENSES, TIER_CONFIDENT, TIER_MAYBE,
                             WEIGHTS)

app = FastAPI(title="sloshbot")
# Signs the session cookie. SLOSHBOT_SECRET_KEY must be set to a real random
# value when hosting publicly — the fallback is fine for local dev only (it
# just means every restart invalidates existing sessions, not a security hole
# by itself, but a fixed/guessable key would let anyone forge a session).
app.add_middleware(SessionMiddleware,
                   secret_key=os.environ.get("SLOSHBOT_SECRET_KEY", "dev-insecure-secret-change-me"),
                   same_site="lax",
                   # 400 days: without this the cookie is a session cookie
                   # that dies when the browser closes, and anonymous users
                   # (who have no other way to recover their identity) would
                   # lose their settings/feedback/holds on every restart.
                   max_age=34560000)

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["scoring_info"] = {
    "weights": WEIGHTS, "confident": TIER_CONFIDENT, "maybe": TIER_MAYBE,
}
# Fallback lens for views (week/calendar) that never thread an active lens
# through — derived from LENSES so it can't drift from a hardcoded literal.
templates.env.globals["default_lens"] = LENSES[0]
templates.env.globals["lens_meta"] = LENS_META
# Verdicts that contradict each other: setting one clears the other. Defined
# once here and injected into the client JS (see _feedback.html) so the pairing
# rule lives in exactly one place.
FEEDBACK_PAIRS = {"went": "skipped", "skipped": "went",
                  "as_promised": "not_as_promised", "not_as_promised": "as_promised"}
templates.env.globals["feedback_pairs"] = FEEDBACK_PAIRS
init_db()
# Read-only prod host's sync seam with the external pipeline machine — see
# app/admin.py for the full contract. Fails closed (503) unless
# SLOSHBOT_ADMIN_TOKEN is set, so mounting it unconditionally is safe.
app.include_router(admin_router)


def resolve_filters(user_id: str, f: str | None, tags: str | None, sources: str | None,
                    price: str | None, max_mi: float | None,
                    min_booze: float | None) -> FilterState:
    """Resolve the active FilterState for a request.

    Sentinel rule: if `f` is present, filter state comes ONLY from the URL
    (from_query) — an absent filter param means that filter is OFF even if the
    user has saved defaults. If `f` is absent, the user's saved defaults apply
    (from_settings) — except max_mi/min_booze, which are overridden by the
    query params when explicitly provided (legacy URL compatibility)."""
    if f is not None:
        return filters.from_query(tags, sources, price, max_mi, min_booze)
    settings = data.get_settings(user_id)
    fs = filters.from_settings(settings)
    if max_mi is not None:
        fs.max_mi = max_mi
    if min_booze is not None:
        fs.min_booze = min_booze
    return fs


def load_events(user_id: str, start: datetime, end: datetime,
                fs: FilterState) -> list[Event]:
    """The read pipeline: fetch -> enrich -> filter -> rank. Returns tiered,
    capped, sorted events (hidden-tier already dropped)."""
    events = data.fetch_events(start, end, user_id)
    settings = data.get_settings(user_id)
    presenter.enrich(events, settings)          # start/end dt, distance, gcal
    events = filters.apply(events, fs)
    return policy.rank(events)                  # composite/tier, demote, cap, sort


def is_warming_up() -> bool:
    """True on a fresh deploy before the first scrape has landed any events.
    Lets the empty state say "fetching…" (and auto-refresh) instead of blaming
    the user for an empty pipeline. Once any event exists, an empty window is a
    genuinely quiet week, not a cold start."""
    with get_conn() as conn:
        return conn.execute("SELECT NOT EXISTS(SELECT 1 FROM events)").fetchone()[0] == 1


def _chip_counts_now(user_id: str) -> dict:
    """Filter-chip counts over the same upcoming 7-day window the views query,
    restricted to the hidden-tier-visible subset (see filters.chip_counts)."""
    now = datetime.now()
    return filters.chip_counts(data.fetch_events(now, now + timedelta(days=7), user_id))


_TAG_OR_URL_RE = re.compile(r"<[^>]*>|https?://\S+")
_WS_RE = re.compile(r"\s+")


def _blurb(desc: str | None, limit: int = 150) -> str:
    """Clean a raw event `description` into a short client-friendly one-liner:
    unescape HTML entities, strip tags/URLs, collapse whitespace, and truncate
    to ~`limit` chars at the last word boundary (with a trailing "…" when
    truncated). Returns "" for falsy input."""
    if not desc:
        return ""
    text = html.unescape(desc)
    text = _TAG_OR_URL_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip() + "…"


def _proto_event(e: dict) -> dict:
    """Augment an Event.to_dict() dict with the flat/normalized fields the
    ported prototype JS (design_day/real/calendar/map_live) expects, without
    dropping any existing key — see PORT_SPEC.md §2."""
    out = dict(e)
    out["booze"] = round((e.get("scores") or {}).get("booze", 0) * 100)
    out["rationale"] = (e.get("rationales") or {}).get("booze", "")
    # Prefer the stored AI blurb; fall back to mechanical truncation when absent.
    out["blurb"] = e.get("blurb") or _blurb(e.get("description"))
    return out


# Deliberately has NO dependency: every other route resolves an anonymous
# identity via app.auth.current_user, but a host's health checker doesn't
# carry a cookie and doesn't need one — it just needs a plain 200. Keep this
# endpoint dumb: no session, no user-scoped data, just proof the process is
# alive and the DB is reachable.
@app.get("/healthz")
def healthz():
    with get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    return {"ok": True, "events": total}


@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, f: str | None = None, tags: str | None = None,
            sources: str | None = None, price: str | None = None,
            max_mi: float | None = None, min_booze: float | None = None,
            partial: int | None = None, user_id: str = Depends(current_user)):
    fs = resolve_filters(user_id, f, tags, sources, price, max_mi, min_booze)
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = load_events(user_id, today, today + timedelta(days=7), fs)
    by_date = defaultdict(list)
    for e in events:
        by_date[e["start_dt"].date()].append(e)
    days = []
    for i in range(7):
        d = today + timedelta(days=i)
        # Keep only each day's top matches — the grid is for spotting the best
        # of the day and time conflicts, not for browsing all ~100 events.
        day_evts = sorted(by_date.get(d.date(), []), key=lambda e: -e["composite"])
        shown = day_evts[:presenter.CAL_MAX_PER_DAY]
        days.append({
            "label": d.strftime("%a"),
            "num": d.strftime("%-d"),
            "is_today": i == 0,
            "blocks": presenter.layout_day(shown),
            "hidden": len(day_evts) - len(shown),
        })
    now_min = now.hour * 60 + now.minute
    settings = data.get_settings(user_id)
    counts = _chip_counts_now(user_id)
    events_json = json.dumps([_proto_event(e.to_dict()) for e in events])
    ctx = {
        "days": days,
        "events_json": events_json,
        "no_events": not any(d["blocks"] or d["hidden"] for d in days),
        "view": "calendar",
        "hours": list(range(8, 24)),
        "cap": presenter.CAL_MAX_PER_DAY,
        "now_pct": ((now_min - presenter.CAL_START_MIN) / presenter.CAL_SPAN * 100
                    if presenter.CAL_START_MIN <= now_min < presenter.CAL_END_MIN else None),
        "max_mi": fs.max_mi,
        "min_booze": fs.min_booze,
        "has_home": presenter.home_coords(settings) is not None,
        "tag_counts": counts["tags"], "included_tags": fs.tags,
        "source_counts": counts["sources"], "included_sources": fs.sources,
        "price_filter": fs.price, "price_counts": counts["price"],
        "warming_up": is_warming_up(),
        "active_filters": fs.to_dict(),
    }
    template_name = "_main_calendar.html" if partial else "calendar.html"
    return templates.TemplateResponse(request, template_name, ctx)


def pending_debrief(user_id: str) -> dict | None:
    """This user's most recent held event that has ended and that THEY have
    no feedback on yet (feedback is per-viewer; holds are per-viewer too)."""
    now_iso = datetime.now().isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            """SELECT h.event_id, h.lens, e.title, e.starts_at
               FROM holds h JOIN events e ON e.id = h.event_id
               WHERE h.user_id = ?
                 AND COALESCE(e.ends_at, e.starts_at) < ?
                 AND NOT EXISTS (SELECT 1 FROM feedback f
                                 WHERE f.event_id = h.event_id AND f.user_id = h.user_id)
               ORDER BY e.starts_at DESC LIMIT 1""", (user_id, now_iso)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["lens"] = d["lens"] or LENSES[0]
    d["when"] = datetime.fromisoformat(d["starts_at"]).strftime("%A")
    return d


@app.get("/", response_class=HTMLResponse)
def home(request: Request, lens: str = "", f: str | None = None, tags: str | None = None,
         sources: str | None = None, price: str | None = None,
         max_mi: float | None = None, min_booze: float | None = None,
         partial: int | None = None, user_id: str = Depends(current_user)):
    """Hero view: the single best move tonight under the active lens."""
    if lens not in LENSES:
        lens = LENSES[0]
    fs = resolve_filters(user_id, f, tags, sources, price, max_mi, min_booze)
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=1), now + timedelta(days=7), fs)

    settings = data.get_settings(user_id)
    # Single lens ("booze"), so match is just the composite (booze) score.
    for e in events:
        e["match"] = e["composite"]

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

    counts = _chip_counts_now(user_id)
    events_json = json.dumps([_proto_event(e.to_dict()) for e in events])
    ctx = {
        "view": "tonight",
        "events_json": events_json,
        "lens": lens, "lenses": LENSES,
        "hero": hero, "hero_label": hero_label,
        "backups": backups,
        "later_days": presenter.group_by_day(later),
        "n_later": len(later),
        "debrief": pending_debrief(user_id),
        "max_mi": fs.max_mi,
        "min_booze": fs.min_booze,
        "has_home": presenter.home_coords(settings) is not None,
        "included_tags": fs.tags,
        "tag_counts": counts["tags"],
        "included_sources": fs.sources,
        "source_counts": counts["sources"],
        "price_filter": fs.price,
        "price_counts": counts["price"],
        "weights": WEIGHTS,
        "warming_up": is_warming_up(),
        "active_filters": fs.to_dict(),
    }
    template_name = "_main_home.html" if partial else "home.html"
    return templates.TemplateResponse(request, template_name, ctx)


@app.get("/week", response_class=HTMLResponse)
def week(request: Request, f: str | None = None, tags: str | None = None,
        sources: str | None = None, price: str | None = None,
        max_mi: float | None = None, min_booze: float | None = None,
        partial: int | None = None, user_id: str = Depends(current_user)):
    fs = resolve_filters(user_id, f, tags, sources, price, max_mi, min_booze)
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=3), now + timedelta(days=7), fs)
    settings = data.get_settings(user_id)
    counts = _chip_counts_now(user_id)
    events_json = json.dumps([_proto_event(e.to_dict()) for e in events])
    ctx = {
        "days": presenter.group_by_day(events),
        "events_json": events_json,
        "view": "week",
        "max_mi": fs.max_mi,
        "min_booze": fs.min_booze,
        "has_home": presenter.home_coords(settings) is not None,
        "weights": WEIGHTS,
        "n_confident": sum(1 for e in events if e["tier"] == "confident"),
        "tag_counts": counts["tags"], "included_tags": fs.tags,
        "source_counts": counts["sources"], "included_sources": fs.sources,
        "price_filter": fs.price, "price_counts": counts["price"],
        "warming_up": is_warming_up(),
        "active_filters": fs.to_dict(),
    }
    template_name = "_main_week.html" if partial else "week.html"
    return templates.TemplateResponse(request, template_name, ctx)


@app.get("/tonight")
def tonight():
    return RedirectResponse("/", status_code=307)  # tonight IS the home view now


@app.get("/map", response_class=HTMLResponse)
def map_view(request: Request, f: str | None = None, tags: str | None = None,
            sources: str | None = None, price: str | None = None,
            max_mi: float | None = None, min_booze: float | None = None,
            user_id: str = Depends(current_user)):
    fs = resolve_filters(user_id, f, tags, sources, price, max_mi, min_booze)
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=3), now + timedelta(days=7), fs)
    settings = data.get_settings(user_id)
    home = presenter.home_coords(settings)
    pins = [{
        "lat": e["lat"], "lon": e["lon"], "title": e["title"],
        "tier": e["tier"], "url": e["url"], "gcal": e["gcal"],
        "when": e["start_dt"].strftime("%a %-I:%M %p"),
        "venue": e["venue_name"] or "",
        "booze": round(e["scores"].get("booze", 0) * 100),
        "distance": round(e["distance_mi"], 1) if e["distance_mi"] is not None else None,
    } for e in events if e["lat"] is not None]
    home_ctx = {"lat": home[0], "lon": home[1]} if home else None
    map_json = json.dumps({
        "home": home_ctx,
        "pins": [{
            "id": e["id"], "title": e["title"],
            "venue": e["venue_name"] or "",
            "neighborhood": e["neighborhood"],
            "starts_at": e["starts_at"],
            "is_free": e["is_free"],
            "lat": e["lat"], "lon": e["lon"],
            "distance_mi": e["distance_mi"],
            "booze": round((e["scores"] or {}).get("booze", 0) * 100),
            "blurb": e.get("blurb") or _blurb(e["description"]),
            "tags": e["tags"],
            "source": e["source"],
            "url": e.get("url", ""),
            "gcal": e.get("gcal", ""),
        } for e in events if e["lat"] is not None],
    })
    counts = _chip_counts_now(user_id)
    return templates.TemplateResponse(request, "map.html", {
        "view": "map",
        "pins": pins,
        "map_json": map_json,
        "home": home_ctx,
        "home_address": settings.get("home_address", ""),
        "max_mi": fs.max_mi,
        "min_booze": fs.min_booze,
        "has_home": home is not None,
        "n_unmapped": sum(1 for e in events if e["lat"] is None),
        "tag_counts": counts["tags"], "included_tags": fs.tags,
        "source_counts": counts["sources"], "included_sources": fs.sources,
        "price_filter": fs.price, "price_counts": counts["price"],
        "active_filters": fs.to_dict(),
    })


# --- JSON API: the serialization seam. Same pipeline as the HTML views, but
# returns Event.to_dict() so a future client-rendered frontend needs no new
# backend logic — just these endpoints. ---
@app.get("/api/events")
def api_events(days: int = 7, f: str | None = None, tags: str | None = None,
               sources: str | None = None, price: str | None = None,
               max_mi: float | None = None, min_booze: float | None = None,
               user_id: str = Depends(current_user)):
    fs = resolve_filters(user_id, f, tags, sources, price, max_mi, min_booze)
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=1), now + timedelta(days=days), fs)
    return {"events": [e.to_dict() for e in events]}


@app.get("/api/counts")
def api_counts(user_id: str = Depends(current_user)):
    return _chip_counts_now(user_id)


class FilterSettingsBody(BaseModel):
    tags: list[str] = []
    sources: list[str] = []
    price: str = ""
    max_mi: float | None = None
    min_booze: float | None = None


@app.post("/settings/filters")
def save_filter_settings(body: FilterSettingsBody, user_id: str = Depends(current_user)):
    """Persist the user's sticky filter defaults. Empty list/string/null for a
    field deletes that settings key; a non-empty value upserts it."""
    values = {
        "included_tags": ",".join(t for t in body.tags if t),
        "included_sources": ",".join(s for s in body.sources if s),
        "price_filter": body.price if body.price in ("free", "paid") else "",
        "max_mi": str(body.max_mi) if body.max_mi is not None else "",
        "min_booze": str(body.min_booze) if body.min_booze is not None else "",
    }
    with get_conn() as conn:
        for key, value in values.items():
            if value:
                conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                             (user_id, key, value))
            else:
                conn.execute("DELETE FROM settings WHERE user_id = ? AND key = ?", (user_id, key))
    return {"ok": True}


def _save_home(user_id: str, addr: str) -> tuple[float | None, float | None]:
    """The one place home address + geocoded coords are written. addr="" clears
    all three home_* keys. A non-empty addr is stored and geocoded inline (one
    cached Nominatim call, see ingest.geocode.geocode); if geocoding can't
    resolve it, the address is still saved but the stale lat/lon are cleared so
    distance sorting quietly turns off rather than pointing at the wrong place.
    Returns (lat, lon) — (None, None) when cleared or unresolved."""
    with get_conn() as conn:
        if not addr:
            conn.execute("DELETE FROM settings WHERE user_id = ? "
                         "AND key IN ('home_address', 'home_lat', 'home_lon')", (user_id,))
            return None, None
        conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'home_address', ?)",
                     (user_id, addr))
        lat, lon = geocode(conn, addr)
        if lat is not None:
            conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'home_lat', ?)",
                         (user_id, str(lat)))
            conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'home_lon', ?)",
                         (user_id, str(lon)))
        else:
            conn.execute("DELETE FROM settings WHERE user_id = ? AND key IN ('home_lat', 'home_lon')",
                         (user_id,))
        return lat, lon


@app.get("/api/home")
def api_get_home(user_id: str = Depends(current_user)):
    """Current visitor's saved home address + coords, so a fetch-based setter
    on ANY page can read/pre-fill it (not just the map's server-rendered form)."""
    s = data.get_settings(user_id)
    return {
        "address": s.get("home_address", ""),
        "lat": float(s["home_lat"]) if s.get("home_lat") else None,
        "lon": float(s["home_lon"]) if s.get("home_lon") else None,
    }


class HomeBody(BaseModel):
    address: str = ""


@app.post("/api/home")
def api_set_home(body: HomeBody, user_id: str = Depends(current_user)):
    """Set (or clear, with address="") the visitor's home address via fetch,
    no page reload. `resolved` tells the UI whether the address geocoded — so
    it can warn "couldn't find that address; distance sorting is off" instead
    of silently doing nothing."""
    lat, lon = _save_home(user_id, body.address.strip())
    return {"ok": True, "resolved": lat is not None,
            "address": body.address.strip(), "lat": lat, "lon": lon}


@app.post("/settings/home")
def set_home(home_address: str = Form(...), user_id: str = Depends(current_user)):
    """Legacy form-POST setter behind the map page's server-rendered form —
    saves via the shared helper, then redirects back. The fetch-based
    /api/home above is the path for any new UI."""
    _save_home(user_id, home_address.strip())
    return RedirectResponse("/map", status_code=303)


@app.post("/feedback/{event_id}/{verdict}")
def toggle_feedback(event_id: str, verdict: str, lens: str = "",
                    user_id: str = Depends(current_user)):
    """Toggle a verdict: on if absent, off if present. Clears its opposite."""
    assert verdict in FEEDBACK_PAIRS
    if verdict in ("went", "skipped"):
        lens = ""
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM feedback WHERE user_id = ? AND event_id = ? AND verdict = ? AND lens = ?",
            (user_id, event_id, verdict, lens),
        ).fetchone()
        if exists:
            conn.execute("DELETE FROM feedback WHERE user_id = ? AND event_id = ? AND verdict = ? AND lens = ?",
                         (user_id, event_id, verdict, lens))
            return {"ok": True, "state": "off"}
        conn.execute("DELETE FROM feedback WHERE user_id = ? AND event_id = ? AND verdict = ? AND lens = ?",
                     (user_id, event_id, FEEDBACK_PAIRS[verdict], lens))
        conn.execute(
            "INSERT INTO feedback (user_id, event_id, verdict, lens, created_at) VALUES (?,?,?,?,?)",
            (user_id, event_id, verdict, lens, datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True, "state": "on"}


@app.post("/hold/{event_id}")
def record_hold(event_id: str, lens: str = "", user_id: str = Depends(current_user)):
    """Remember that the user placed a calendar hold — feeds their debrief."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO holds (user_id, event_id, lens, created_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id, event_id) DO NOTHING""",
            (user_id, event_id, lens, datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True}

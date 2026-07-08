"""Sloshbot web app. Thin HTTP layer that COMPOSES the read pipeline
(data -> presenter -> filters -> policy) and renders it, plus the small write
paths (feedback, holds, settings). Read-only over the DB except those writes.

Public multi-user app: every route below the auth routes requires a signed-in
user (session cookie via app.auth) — settings/feedback/holds are scoped to
that user_id. The event catalog itself (events/scores/tags) is shared and
crowdsourced across every user, deliberately unscoped — see app/data.py.

  data.fetch_events   -> raw events + scores/tags/(this user's)feedback/host_rep
  presenter.enrich    -> start/end datetimes, distance, calendar link
  filters.apply       -> the hard filters (source/price/distance/tag/booze)
  policy.rank         -> composite/tier, source demotion, per-day cap, sort

Run: uv run uvicorn app.main:app --reload
"""
import os
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request

from app import data, filters, policy, presenter
from app.auth import (RedirectToLogin, authenticate, create_user,
                      login_redirect_handler, require_user_api, require_user_html)
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
                   same_site="lax")
app.add_exception_handler(RedirectToLogin, login_redirect_handler)

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


def load_events(user_id: str, start: datetime, end: datetime,
                max_mi: float | None = None,
                min_booze: float | None = None) -> list[Event]:
    """The read pipeline: fetch -> enrich -> filter -> rank. Returns tiered,
    capped, sorted events (hidden-tier already dropped)."""
    events = data.fetch_events(start, end, user_id)
    settings = data.get_settings(user_id)
    presenter.enrich(events, settings)          # start/end dt, distance, gcal
    events = filters.apply(events, settings, max_mi, min_booze)
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


@app.get("/signup", response_class=HTMLResponse)
def signup_form(request: Request, error: str | None = None):
    return templates.TemplateResponse(request, "signup.html", {"error": error})


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...),
          password_confirm: str = Form(...)):
    if len(password) < 8:
        return templates.TemplateResponse(request, "signup.html",
            {"error": "Password must be at least 8 characters."})
    if password != password_confirm:
        return templates.TemplateResponse(request, "signup.html",
            {"error": "Passwords don't match."})
    try:
        user_id = create_user(email, password)
    except ValueError as exc:
        return templates.TemplateResponse(request, "signup.html", {"error": str(exc)})
    request.session["user_id"] = user_id
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None, next: str = "/"):
    return templates.TemplateResponse(request, "login.html", {"error": error, "next": next})


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...),
         next: str = Form("/")):
    user_id = authenticate(email, password)
    if not user_id:
        return templates.TemplateResponse(request, "login.html",
            {"error": "Incorrect email or password.", "next": next})
    request.session["user_id"] = user_id
    return RedirectResponse(next or "/", status_code=303)


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar(request: Request, max_mi: float | None = None, min_booze: float | None = None,
            user_id: str = Depends(require_user_html)):
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = load_events(user_id, today, today + timedelta(days=7), max_mi, min_booze)
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
    return templates.TemplateResponse(request, "calendar.html", {
        "days": days,
        "no_events": not any(d["blocks"] or d["hidden"] for d in days),
        "view": "calendar",
        "hours": list(range(8, 24)),
        "cap": presenter.CAL_MAX_PER_DAY,
        "now_pct": ((now_min - presenter.CAL_START_MIN) / presenter.CAL_SPAN * 100
                    if presenter.CAL_START_MIN <= now_min < presenter.CAL_END_MIN else None),
        "max_mi": max_mi,
        "min_booze": min_booze,
        "has_home": presenter.home_coords(settings) is not None,
        "tag_counts": counts["tags"], "included_tags": filters.included_tags(settings),
        "source_counts": counts["sources"], "included_sources": filters.included_sources(settings),
        "price_filter": filters.price_filter(settings), "price_counts": counts["price"],
        "warming_up": is_warming_up(),
    })


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
def home(request: Request, lens: str = "", max_mi: float | None = None,
         min_booze: float | None = None, user_id: str = Depends(require_user_html)):
    """Hero view: the single best move tonight under the active lens."""
    if lens not in LENSES:
        lens = LENSES[0]
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=1), now + timedelta(days=7), max_mi, min_booze)

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
    return templates.TemplateResponse(request, "home.html", {
        "view": "tonight",
        "lens": lens, "lenses": LENSES,
        "hero": hero, "hero_label": hero_label,
        "backups": backups,
        "later_days": presenter.group_by_day(later),
        "n_later": len(later),
        "debrief": pending_debrief(user_id),
        "max_mi": max_mi,
        "min_booze": min_booze,
        "has_home": presenter.home_coords(settings) is not None,
        "included_tags": filters.included_tags(settings),
        "tag_counts": counts["tags"],
        "included_sources": filters.included_sources(settings),
        "source_counts": counts["sources"],
        "price_filter": filters.price_filter(settings),
        "price_counts": counts["price"],
        "weights": WEIGHTS,
        "warming_up": is_warming_up(),
    })


@app.get("/week", response_class=HTMLResponse)
def week(request: Request, max_mi: float | None = None, min_booze: float | None = None,
        user_id: str = Depends(require_user_html)):
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=3), now + timedelta(days=7), max_mi, min_booze)
    settings = data.get_settings(user_id)
    counts = _chip_counts_now(user_id)
    return templates.TemplateResponse(request, "week.html", {
        "days": presenter.group_by_day(events),
        "view": "week",
        "max_mi": max_mi,
        "min_booze": min_booze,
        "has_home": presenter.home_coords(settings) is not None,
        "weights": WEIGHTS,
        "n_confident": sum(1 for e in events if e["tier"] == "confident"),
        "tag_counts": counts["tags"], "included_tags": filters.included_tags(settings),
        "source_counts": counts["sources"], "included_sources": filters.included_sources(settings),
        "price_filter": filters.price_filter(settings), "price_counts": counts["price"],
        "warming_up": is_warming_up(),
    })


@app.get("/tonight")
def tonight():
    return RedirectResponse("/", status_code=307)  # tonight IS the home view now


@app.get("/map", response_class=HTMLResponse)
def map_view(request: Request, max_mi: float | None = None,
            user_id: str = Depends(require_user_html)):
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=3), now + timedelta(days=7), max_mi)
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
    counts = _chip_counts_now(user_id)
    return templates.TemplateResponse(request, "map.html", {
        "view": "map",
        "pins": pins,
        "home": {"lat": home[0], "lon": home[1]} if home else None,
        "home_address": settings.get("home_address", ""),
        "max_mi": max_mi,
        "has_home": home is not None,
        "n_unmapped": sum(1 for e in events if e["lat"] is None),
        "tag_counts": counts["tags"], "included_tags": filters.included_tags(settings),
        "source_counts": counts["sources"], "included_sources": filters.included_sources(settings),
        "price_filter": filters.price_filter(settings), "price_counts": counts["price"],
    })


# --- JSON API: the serialization seam. Same pipeline as the HTML views, but
# returns Event.to_dict() so a future client-rendered frontend needs no new
# backend logic — just these endpoints. ---
@app.get("/api/events")
def api_events(days: int = 7, max_mi: float | None = None, min_booze: float | None = None,
               user_id: str = Depends(require_user_api)):
    now = datetime.now()
    events = load_events(user_id, now - timedelta(hours=1), now + timedelta(days=days), max_mi, min_booze)
    return {"events": [e.to_dict() for e in events]}


@app.get("/api/counts")
def api_counts(user_id: str = Depends(require_user_api)):
    return _chip_counts_now(user_id)


@app.post("/settings/tags/toggle")
def toggle_tag_filter(tag: str, user_id: str = Depends(require_user_api)):
    """One-tap tag filtering: clicking a tag chip anywhere flips it in/out of
    the included_tags setting."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE user_id = ? AND key = 'included_tags'",
                           (user_id,)).fetchone()
        current = [t for t in (row["value"] if row else "").split(",") if t]
        if tag in current:
            current.remove(tag)
            state = "off"
        else:
            current.append(tag)
            state = "on"
        conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'included_tags', ?)",
                     (user_id, ",".join(current)))
    return {"ok": True, "state": state}


@app.post("/settings/tags/clear")
def clear_tag_filter(user_id: str = Depends(require_user_api)):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE user_id = ? AND key = 'included_tags'", (user_id,))
    return {"ok": True}


@app.post("/settings/sources/toggle")
def toggle_source_filter(source: str, user_id: str = Depends(require_user_api)):
    """One-tap source filtering: mute a noisy scraper without hiding everything."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE user_id = ? AND key = 'included_sources'",
                           (user_id,)).fetchone()
        current = [s for s in (row["value"] if row else "").split(",") if s]
        if source in current:
            current.remove(source)
            state = "off"
        else:
            current.append(source)
            state = "on"
        conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'included_sources', ?)",
                     (user_id, ",".join(current)))
    return {"ok": True, "state": state}


@app.post("/settings/sources/clear")
def clear_source_filter(user_id: str = Depends(require_user_api)):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE user_id = ? AND key = 'included_sources'", (user_id,))
    return {"ok": True}


@app.post("/settings/price/toggle")
def toggle_price_filter(value: str, user_id: str = Depends(require_user_api)):
    """Single-select free/paid filter: picking the active value clears it."""
    assert value in ("free", "paid")
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE user_id = ? AND key = 'price_filter'",
                           (user_id,)).fetchone()
        current = row["value"] if row else ""
        new = "" if current == value else value
        conn.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'price_filter', ?)",
                     (user_id, new))
    return {"ok": True, "state": "on" if new else "off"}


@app.post("/settings/price/clear")
def clear_price_filter(user_id: str = Depends(require_user_api)):
    with get_conn() as conn:
        conn.execute("DELETE FROM settings WHERE user_id = ? AND key = 'price_filter'", (user_id,))
    return {"ok": True}


@app.post("/settings/home")
def set_home(home_address: str = Form(...), user_id: str = Depends(require_user_html)):
    """Save home address and geocode it inline (single Nominatim call, cached).
    Uses the public ingest.geocode.geocode() — no reaching into pipeline privates."""
    addr = home_address.strip()
    with get_conn() as conn:
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
    return RedirectResponse("/map", status_code=303)


@app.post("/feedback/{event_id}/{verdict}")
def toggle_feedback(event_id: str, verdict: str, lens: str = "",
                    user_id: str = Depends(require_user_api)):
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
def record_hold(event_id: str, lens: str = "", user_id: str = Depends(require_user_api)):
    """Remember that the user placed a calendar hold — feeds their debrief."""
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO holds (user_id, event_id, lens, created_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id, event_id) DO NOTHING""",
            (user_id, event_id, lens, datetime.now().isoformat(timespec="seconds")),
        )
    return {"ok": True}

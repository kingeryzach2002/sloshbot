"""DoTheBay (dothebay.com) source — runs on the DoStuff Media platform, which
exposes a JSON API alongside every HTML listing page: append ".json" to any
date-scoped listing URL (e.g. /events/2026/7/14.json) and you get the same
event objects that hydrate the page, fully structured, with no HTML parsing
required.

Big win vs. other sources: venue lat/lon are native fields on this API, no
geocoding needed.

Coverage strategy: iterate the next ~10 days (today..today+10) and
round-robin across them one page at a time so a big --limit doesn't get
entirely consumed by the busiest early days before later days get a look in
(this is the lesson the funcheap port learned the hard way).
"""
import json
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape

import requests

BASE = "https://dothebay.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
}
SOURCE = "dothebay"
DAYS_AHEAD = 10  # today..today+10 inclusive
PACING_SECONDS = 0.3

PRICE_RANGE_RE = re.compile(r"\$\s*([\d.]+)\s*(?:[-–]\s*\$?\s*([\d.]+))?")


def _fetch_date_page(dt: datetime, page: int, session: requests.Session) -> dict:
    url = f"{BASE}/events/{dt.year}/{dt.month}/{dt.day}.json"
    params = {"page": page} if page > 1 else None
    resp = session.get(url, headers=HEADERS, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _parse_price(ticket_info: str):
    """Best-effort $min/$max extraction from DoTheBay's free-text ticket_info
    field, e.g. '$25 - $30', '$95', 'Free, 21+', '$18-$20, 21+'."""
    if not ticket_info:
        return (None, None)
    if "free" in ticket_info.lower() and "$" not in ticket_info:
        return (0.0, 0.0)
    m = PRICE_RANGE_RE.search(ticket_info)
    if not m:
        return (None, None)
    lo = float(m.group(1))
    hi = float(m.group(2)) if m.group(2) else lo
    return (lo, hi)


def _strip_html(html: str) -> str | None:
    if not html:
        return None
    text = re.sub(r"<[^>]+>", "\n", html)
    text = unescape(text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text or None


def _derive_rsvp_type(ev: dict) -> str | None:
    if ev.get("sold_out"):
        return "sold_out"
    actions = ev.get("actions") or {}
    if actions.get("rsvp") and not actions.get("buy"):
        return "open"
    return None  # ticketed/on-sale events: not modeled by sloshbot's rsvp_type


def _derive_ends_at(ev: dict, starts_at_iso: str | None) -> str | None:
    """DoTheBay's tz_adjusted_end_date is reliable for single-occurrence
    events but can reflect a stale/unrelated date for recurring or
    long-running exhibit listings. Only trust it when it falls on the same
    calendar day as the start; otherwise leave None (caller/UI assumes +2h
    per ARCHITECTURE.md convention)."""
    end_raw = ev.get("tz_adjusted_end_date")
    if not end_raw or not starts_at_iso:
        return None
    if end_raw[:10] == starts_at_iso[:10]:
        return end_raw
    return None


def _occurrence_starts_at(ev: dict, day: datetime) -> str | None:
    """DoTheBay's day-scoped listing includes multi-day/recurring events
    (exhibits, ongoing residencies) whose tz_adjusted_begin_date is the
    run's original start date — sometimes months before the day actually
    being listed. Since we only ever see an event under a day-scoped
    endpoint because it occurs that day, rewrite the date portion to the
    day queried while keeping the original time-of-day and UTC offset."""
    begin_raw = ev.get("tz_adjusted_begin_date")
    if not begin_raw or len(begin_raw) < 11:
        return begin_raw
    time_and_offset = begin_raw[10:]  # "T10:00:00-07:00"
    return f"{day:%Y-%m-%d}{time_and_offset}"


def _coord(venue: dict, *keys: str) -> float | None:
    for key in keys:
        val = venue.get(key)
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, str):
            try:
                return float(val)
            except ValueError:
                continue
    return None


def _map(ev: dict, day: datetime) -> dict:
    starts_at = _occurrence_starts_at(ev, day)
    ends_at = _derive_ends_at(ev, starts_at)
    price_min, price_max = _parse_price(ev.get("ticket_info"))
    is_free = ev.get("is_free")
    venue = ev.get("venue") or {}

    return {
        "id": f"{SOURCE}:{ev['id']}",
        "source": SOURCE,
        "source_id": str(ev["id"]),
        "url": BASE + ev["permalink"] if ev.get("permalink") else None,
        "title": ev.get("title"),
        "description": _strip_html(ev.get("description")) or ev.get("excerpt"),
        "host_name": ev.get("presented_by") or None,
        "host_url": None,  # not present in listing JSON; would need detail-page scrape
        "venue_name": venue.get("title"),
        "address": venue.get("full_address") or venue.get("address"),
        "neighborhood": None,  # not exposed by this endpoint
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_free": 1 if is_free is True else (0 if is_free is False else None),
        "price_min": price_min,
        "price_max": price_max,
        "rsvp_type": _derive_rsvp_type(ev),
        "image_url": (ev.get("imagery") or {}).get("aws", {}).get("cover_image_w_1200_h_450")
        or (ev.get("imagery") or {}).get("photo")
        or None,
        "lat": _coord(venue, "latitude", "lat"),
        "lon": _coord(venue, "longitude", "lng", "lon"),
        "raw": json.dumps(ev),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


class _DayCursor:
    """Tracks per-day pagination state so callers can round-robin across
    days: pull one page from a day, move to the next day, and only revisit
    a day once every other day has had a turn."""

    def __init__(self, dt: datetime):
        self.dt = dt
        self.page = 1
        self.total_pages = 1
        self.exhausted = False
        self.buffer: list[dict] = []  # raw events fetched but not yet consumed

    def _refill(self, session: requests.Session) -> None:
        """Fetch the next page for this day into the buffer, if needed."""
        if self.buffer or self.exhausted:
            return
        try:
            data = _fetch_date_page(self.dt, self.page, session)
        except requests.HTTPError:
            self.exhausted = True
            return
        raw_events = data.get("events", [])
        self.total_pages = data.get("paging", {}).get("total_pages", 1)
        self.buffer = raw_events
        self.page += 1
        if not raw_events:
            self.exhausted = True

    def next_event(self, session: requests.Session) -> dict | None:
        """Pop one raw event for this day, fetching a new page if the
        buffer is empty. Returns None once the day is fully exhausted."""
        if not self.buffer:
            self._refill(session)
        if not self.buffer:
            self.exhausted = True
            return None
        ev = self.buffer.pop(0)
        if not self.buffer and self.page > self.total_pages:
            self.exhausted = True
        return ev


def fetch(limit: int = 50) -> list[dict]:
    today = datetime.now()
    days = [_DayCursor(today + timedelta(days=i)) for i in range(DAYS_AHEAD + 1)]

    session = requests.Session()
    seen_ids: set[str] = set()
    events: list[dict] = []

    first_request = True
    while len(events) < limit and any(not d.exhausted for d in days):
        for day in days:
            if len(events) >= limit:
                break
            if day.exhausted:
                continue

            needs_request = not day.buffer
            if needs_request and not first_request:
                time.sleep(PACING_SECONDS)
            first_request = False

            ev = day.next_event(session)
            if ev is None:
                continue

            eid = ev.get("id")
            if eid is None or eid in seen_ids:
                continue
            seen_ids.add(eid)
            events.append(_map(ev, day.dt))

    return events[:limit]

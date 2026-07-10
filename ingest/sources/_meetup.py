"""Meetup.com source — scrapes the public "find events" search page for San
Francisco events (embedded Next.js/Apollo GraphQL cache); no auth needed.

Method (from the feasibility spike):
1. GET https://www.meetup.com/find/?location=us--ca--San%20Francisco&source=EVENTS
   with customStartDate/customEndDate params, one window at a time, to cover
   the next ~10 days (a single window's cache doesn't reliably return the
   whole span). Response is server-rendered HTML containing a
   <script id="__NEXT_DATA__"> blob with an Apollo GraphQL normalized cache
   (`__APOLLO_STATE__`) -- the entire dataset the React app hydrates from.
2. For richer per-event fields not present in the list-page cache (notably
   venue lat/lon, ends_at, fee), GET the individual event page which embeds a
   fuller Apollo cache including a resolved Venue object with lat/lon.

Only `requests` + stdlib are used (no headless browser, no GraphQL calls).
robots.txt disallows /gql*, /api/, /mu_api/ -- not the /find/ HTML page.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

SF_LOCATION = "us--ca--San Francisco"
WINDOW_DAYS = 3          # width of each date window queried against /find
COVERAGE_DAYS = 10       # total span of days to cover
PACING_SECONDS = 0.4

# Meetup's RSVP-state enum -> sloshbot's rsvp_type contract.
RSVP_MAP = {
    "JOIN_OPEN": "open",
    "APPROVAL_NEEDED": "approval",
    "CLOSED": "sold_out",
    "WAITLIST_OPEN": "waitlist",
    "NOT_OPEN_YET": "waitlist",
}


def _fetch_next_data(url: str, session: requests.Session) -> dict:
    resp = session.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    m = NEXT_DATA_RE.search(resp.text)
    if not m:
        raise RuntimeError(f"__NEXT_DATA__ not found at {url}")
    return json.loads(m.group(1))


def _apollo_state_of(next_data: dict) -> dict:
    return next_data["props"]["pageProps"]["__APOLLO_STATE__"]


def _resolve(apollo: dict, ref_or_obj):
    """Follow an Apollo {'__ref': 'Type:id'} pointer, or pass through a plain dict."""
    if isinstance(ref_or_obj, dict) and "__ref" in ref_or_obj:
        return apollo.get(ref_or_obj["__ref"])
    return ref_or_obj


def _search_events(session: requests.Session, start: datetime, end: datetime) -> list[dict]:
    """Hit the SF find-events page filtered to a date window; return raw Event dicts."""
    loc = quote(SF_LOCATION)
    url = (
        f"https://www.meetup.com/find/?location={loc}&source=EVENTS"
        f"&customStartDate={quote(start.isoformat())}"
        f"&customEndDate={quote(end.isoformat())}"
    )
    data = _fetch_next_data(url, session)
    apollo = _apollo_state_of(data)
    events = [v for v in apollo.values() if isinstance(v, dict) and v.get("__typename") == "Event"]
    # attach the apollo cache so callers can resolve group/venue refs
    for e in events:
        e["_apollo"] = apollo
    return events


def _fetch_event_detail(session: requests.Session, event_url: str) -> tuple[dict, dict] | None:
    """Fetch an individual event page; return (event_dict, apollo_state) or None on failure."""
    try:
        data = _fetch_next_data(event_url, session)
    except Exception:
        return None
    apollo = _apollo_state_of(data)
    for v in apollo.values():
        if isinstance(v, dict) and v.get("__typename") == "Event":
            return v, apollo
    return None


def _map(raw_list_event: dict, session: requests.Session) -> dict:
    """Map a raw Meetup Event (+ detail-page fetch for lat/lon) to the sloshbot `events` row."""
    apollo = raw_list_event["_apollo"]
    group = _resolve(apollo, raw_list_event.get("group")) or {}
    venue = _resolve(apollo, raw_list_event.get("venue"))

    event_url = raw_list_event.get("eventUrl", "")
    source_id = raw_list_event.get("id", "")

    lat = lon = None
    ends_at = raw_list_event.get("endTime")
    rsvp_type = RSVP_MAP.get(raw_list_event.get("rsvpState"), None)
    is_free = None
    price_min = price_max = None

    # Detail-page fetch: gets venue lat/lon, endTime, and a more precise rsvpSettings type.
    detail = _fetch_event_detail(session, event_url) if event_url else None
    if detail:
        detail_event, detail_apollo = detail
        ends_at = ends_at or detail_event.get("endTime")
        detail_venue = _resolve(detail_apollo, detail_event.get("venue"))
        if detail_venue:
            # Online events carry a bogus/default lat-lon (e.g. near the date
            # line) -- only surface geocoding for physical venues.
            if detail_event.get("eventType") == "PHYSICAL":
                lat = detail_venue.get("lat")
                lon = detail_venue.get("lon")
            venue = venue or detail_venue
        settings_type = (detail_event.get("rsvpSettings") or {}).get("__typename", "")
        if settings_type == "RsvpOpenSettings":
            rsvp_type = rsvp_type or "open"
        elif settings_type == "RsvpApprovalSettings":
            rsvp_type = "approval"
        fee = detail_event.get("feeSettings")
        is_free = 1 if fee is None else 0
        price_min = fee.get("amount") if fee else None
        price_max = price_min

    venue = venue or {}
    address_parts = [venue.get("address"), venue.get("city"), venue.get("state")]
    address = ", ".join(p for p in address_parts if p) or None

    raw_for_json = {k: v for k, v in raw_list_event.items() if k != "_apollo"}

    return {
        "id": f"meetup:{source_id}",
        "source": "meetup",
        "source_id": source_id,
        "url": event_url,
        "title": raw_list_event.get("title"),
        "description": raw_list_event.get("description"),
        "host_name": group.get("name"),
        "host_url": f"https://www.meetup.com/{group.get('urlname')}/" if group.get("urlname") else None,
        "venue_name": venue.get("name"),
        "address": address,
        "neighborhood": None,  # not provided by source; normalized downstream
        "starts_at": raw_list_event.get("dateTime"),
        "ends_at": ends_at,
        "is_free": is_free,
        "price_min": price_min,
        "price_max": price_max,
        "rsvp_type": rsvp_type,
        "image_url": None,  # PhotoInfo ref resolvable but skipped for now
        "lat": lat,
        "lon": lon,
        "raw": json.dumps(raw_for_json, default=str),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch(limit: int = 50) -> list[dict]:
    session = requests.Session()

    now = datetime.now().astimezone()
    windows = []
    cursor = now
    end_of_coverage = now + timedelta(days=COVERAGE_DAYS)
    while cursor < end_of_coverage:
        window_end = min(cursor + timedelta(days=WINDOW_DAYS), end_of_coverage)
        windows.append((cursor, window_end))
        cursor = window_end

    # Dedup listing results by event id within this fetch.
    seen: dict[str, dict] = {}
    for start, end in windows:
        try:
            raw_events = _search_events(session, start, end)
        except Exception:
            continue
        for raw in raw_events:
            eid = raw.get("id")
            if eid and eid not in seen:
                seen[eid] = raw
        time.sleep(PACING_SECONDS)

    events: list[dict] = []
    for raw in list(seen.values()):
        if len(events) >= limit:
            break
        try:
            events.append(_map(raw, session))
        except Exception:
            continue
        time.sleep(PACING_SECONDS)
    return events

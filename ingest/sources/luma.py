"""Luma (lu.ma) source — unofficial JSON API discovered in the feasibility spike.

Discovery: api.lu.ma/discover/get-paginated-events (paginated, no auth)
Detail:    api.lu.ma/event/get (full description, hosts, geo, tickets, RSVP type)
"""
import json
import time
from datetime import datetime, timezone

import requests

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
DISCOVER_API = "https://api.lu.ma/discover/get-paginated-events"
EVENT_API = "https://api.lu.ma/event/get"
SF_PLACE_API_ID = "discplace-BDj7GNbGlsF7Cka"  # scraped from lu.ma/sf __NEXT_DATA__
PACING_SECONDS = 0.3

# Pagination notes (verified against the live API 2026-07-07):
#   - `pagination_cursor` DOES advance correctly and results are sorted
#     nearest-first (ascending start_at); confirmed by walking ~35 pages and
#     watching start_at climb from today through 2027 without repeats.
#   - There is no date-range/from/after filter param the API honors (tried
#     `after`, `date_range`, `start_after`, `period` — all silently ignored).
#     `sort_direction=desc` IS honored (flips to furthest-first) but that's
#     not useful for "next N days" coverage, so default ascending is kept.
#   - `pagination_limit` is capped server-side at ~48 regardless of the value
#     requested (25/50/100/200/500 all returned <=48 entries per page), so
#     requesting more than that per page just wastes a query param.
#   - The previous bug wasn't broken cursor advancement — it was simply that
#     ingest.run's default/typical --limit (~25-30) stopped pagination after
#     one page, which is always "today and tomorrow" because the feed is
#     nearest-first. Empirically, covering ~10 days of SF events out of this
#     feed takes on the order of 400-450 discover entries (page ~16-18 at the
#     ~48/page cap), so callers wanting 10-day coverage must pass a much
#     larger --limit; there's no cheaper way to skip ahead.
DISCOVER_PAGE_SIZE = 48


def _prosemirror_to_text(doc) -> str | None:
    chunks: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "text":
                chunks.append(node.get("text", ""))
            for child in node.get("content") or []:
                walk(child)
            if node.get("type") in ("paragraph", "heading", "listItem"):
                chunks.append("\n")
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(doc or {})
    return "".join(chunks).strip() or None


def _price(value) -> float | None:
    """Luma prices arrive as a plain number OR {cents, currency}."""
    if isinstance(value, dict):
        cents = value.get("cents")
        return cents / 100 if isinstance(cents, (int, float)) else None
    return float(value) if isinstance(value, (int, float)) else None


def _coord(event: dict, geo: dict, *keys: str) -> float | None:
    """Luma exposes coordinates in a few shapes; try the known spots."""
    candidates = [event.get("coordinate") or {}, geo, event]
    for container in candidates:
        for key in keys:
            val = container.get(key)
            if isinstance(val, (int, float)):
                return float(val)
    return None


def _rsvp_type(detail: dict, ticket_info: dict) -> str | None:
    reg = detail.get("registration_availability")
    if reg == "waitlist":
        return "waitlist"
    if ticket_info.get("is_sold_out"):
        return "sold_out"
    if ticket_info.get("require_approval"):
        return "approval"
    if reg in ("open", None):
        return "open"
    return reg


def _map(detail: dict) -> dict:
    event = detail.get("event", {})
    calendar = detail.get("calendar", {})
    hosts = detail.get("hosts") or []
    ticket_info = detail.get("ticket_info") or {}
    geo = event.get("geo_address_info") or {}
    is_free = ticket_info.get("is_free")

    host_url = None
    if calendar.get("slug"):
        host_url = f"https://lu.ma/{calendar['slug']}"
    elif calendar.get("website"):
        host_url = calendar["website"]

    return {
        "id": f"luma:{event.get('api_id')}",
        "source": "luma",
        "source_id": event.get("api_id"),
        "url": f"https://lu.ma/{event.get('url')}" if event.get("url") else None,
        "title": event.get("name"),
        "description": _prosemirror_to_text(detail.get("description_mirror")),
        "host_name": calendar.get("name") or (hosts[0].get("name") if hosts else None),
        "host_url": host_url,
        "venue_name": geo.get("short_address") or geo.get("sublocality"),
        "address": geo.get("full_address") if geo.get("mode") == "shown" else None,
        "neighborhood": geo.get("sublocality"),
        "starts_at": event.get("start_at"),  # UTC; normalize.py converts to Pacific
        "ends_at": event.get("end_at"),
        "is_free": 1 if is_free is True else 0 if is_free is False else None,
        "price_min": _price(ticket_info.get("price")),
        "price_max": _price(ticket_info.get("max_price")),
        "rsvp_type": _rsvp_type(detail, ticket_info),
        "image_url": event.get("cover_url") or event.get("social_image_url"),
        "lat": _coord(event, geo, "latitude", "lat"),
        "lon": _coord(event, geo, "longitude", "lng", "lon"),
        "raw": json.dumps(detail),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch(limit: int = 50) -> list[dict]:
    entries: list[dict] = []
    cursor = None
    while len(entries) < limit:
        params = {"place_api_id": SF_PLACE_API_ID,
                  "pagination_limit": min(limit - len(entries), DISCOVER_PAGE_SIZE)}
        if cursor:
            params["pagination_cursor"] = cursor
        resp = requests.get(DISCOVER_API, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("entries", [])
        if not batch:
            break
        entries.extend(batch)
        cursor = data.get("next_cursor")
        if not cursor or not data.get("has_more", bool(cursor)):
            break

    events = []
    for entry in entries[:limit]:
        api_id = entry.get("event", {}).get("api_id") or entry.get("api_id")
        if not api_id:
            continue
        resp = requests.get(EVENT_API, params={"event_api_id": api_id},
                            headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            continue
        events.append(_map(resp.json()))
        time.sleep(PACING_SECONDS)
    return events

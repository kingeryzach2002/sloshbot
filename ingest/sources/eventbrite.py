"""Eventbrite source — ported from the feasibility spike.

Eventbrite's public search API was shut down, so we scrape the public browse
pages, which embed `window.__SERVER_DATA__` (listing data), then enrich each
event from its public page's schema.org JSON-LD block (organizer, prices,
times with explicit UTC offset).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import requests

# Fuller browser-like header set. Eventbrite's edge (Akamai) sometimes serves a
# 405/403 to requests that carry only a User-Agent — the Accept / sec-fetch-* /
# sec-ch-ua trio is what a real Chrome navigation sends, and including them can
# be the difference between a block and a 200 from a datacenter IP. (It is NOT a
# cure for hard IP-reputation blocking; if the host IP itself is flagged, no
# header set gets through — see fetch()'s graceful partial-return handling.)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# Eventbrite serves two browse-page templates that both embed
# window.__SERVER_DATA__ but shape the listing data differently (see
# _collect_listing_events): the classic "buckets" template on the plain
# /events/ path, and a newer search-results template
# (search_data.events.results) used by date-filtered paths like
# events--this-weekend/, events--next-week/, and free--events/. The plain
# /events/ page alone skews heavily toward the next ~48 hours with a thin,
# scattered tail; the date-filtered paths are what actually pull in solid
# coverage of the rest of the next-10-day window. ?page=2 on the
# search-results template pages in real chronological additions (verified by
# checking parsed event dates), so we take one extra page there for depth.
# ?page=2 on the buckets template mostly reshuffles the same ~40 events
# (34/39 overlap when checked), so it isn't worth the extra request.
SEARCH_URLS = [
    "https://www.eventbrite.com/d/ca--san-francisco/events/",
    "https://www.eventbrite.com/d/ca--san-francisco/events--this-weekend/",
    "https://www.eventbrite.com/d/ca--san-francisco/events--next-week/",
    "https://www.eventbrite.com/d/ca--san-francisco/events--next-week/?page=2",
    "https://www.eventbrite.com/d/ca--san-francisco/free--events/",
]

SERVER_DATA_RE = re.compile(r"window\.__SERVER_DATA__\s*=\s*(\{.*\});\s*window", re.DOTALL)
LDJSON_RE = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
PACING_SECONDS = 0.5


def _get(url: str, timeout: int = 20) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def _get_search_server_data(html: str, url: str) -> dict:
    m = SERVER_DATA_RE.search(html)
    if not m:
        raise RuntimeError(
            f"window.__SERVER_DATA__ not found on {url} (page shape may have changed)"
        )
    return json.loads(m.group(1))


def _events_from_buckets(server_data: dict) -> list[dict]:
    """Classic browse-page template: server_data['buckets'][*]['events']."""
    events: list[dict] = []
    for bucket in server_data.get("buckets", []):
        events.extend(bucket.get("events", []))
    return events


def _events_from_search_data(server_data: dict) -> list[dict]:
    """Newer template used by date-filtered browse paths (this-weekend,
    next-week, free--events): server_data['search_data']['events']['results'].
    Event dicts here use ints for 'id' where the buckets template uses
    strings; _collect_listing_events normalizes that so cross-page dedup and
    id-based lookups both work regardless of which template a page used.
    """
    search_data = server_data.get("search_data") or {}
    events_block = search_data.get("events") or {}
    return events_block.get("results", []) or []


def _collect_listing_events(server_data: dict) -> list[dict]:
    raw_events = _events_from_buckets(server_data) or _events_from_search_data(server_data)
    seen: dict = {}
    for ev in raw_events:
        eid = ev.get("id") or ev.get("eid")
        if eid is None:
            continue
        eid = str(eid)
        if eid not in seen:
            ev = dict(ev)
            ev["id"] = eid
            seen[eid] = ev
    return list(seen.values())


def _get_event_ldjson(html: str) -> dict | None:
    """Return the schema.org SocialEvent/Event ld+json block, if present."""
    for block in LDJSON_RE.findall(html):
        try:
            d = json.loads(block)
        except json.JSONDecodeError:
            continue
        if d.get("@type") in ("SocialEvent", "Event", "BusinessEvent", "Festival"):
            return d
    return None


def _price_from_offers(ldjson: dict) -> tuple[float | None, float | None, bool | None]:
    offers = ldjson.get("offers")
    if not offers:
        return None, None, None
    if isinstance(offers, dict):
        offers = [offers]
    lows, highs = [], []
    for o in offers:
        try:
            if "lowPrice" in o:
                lows.append(float(o["lowPrice"]))
            if "highPrice" in o:
                highs.append(float(o["highPrice"]))
            elif "price" in o:
                lows.append(float(o["price"]))
                highs.append(float(o["price"]))
        except (TypeError, ValueError):
            continue
    price_min = min(lows) if lows else None
    price_max = max(highs) if highs else None
    is_free = None
    if price_min is not None:
        is_free = price_min == 0.0 and (price_max in (None, 0.0))
    return price_min, price_max, is_free


def _rsvp_type(listing_ev: dict, ldjson: dict | None) -> str | None:
    ta = listing_ev.get("ticket_availability") or {}
    if ta.get("sold_out"):
        return "sold_out"
    if ta.get("waitlist_available"):
        return "waitlist"
    if ta.get("has_available_tickets") is False:
        return "sold_out"
    if ldjson:
        offers = ldjson.get("offers")
        if isinstance(offers, dict):
            offers = [offers]
        if offers and any(
            (o.get("availability") or "").endswith("SoldOut") for o in offers
        ):
            return "sold_out"
    return "open"


def _map(listing_ev: dict, ldjson: dict | None) -> dict:
    eid = listing_ev.get("id") or listing_ev.get("eid")
    venue = listing_ev.get("primary_venue") or {}
    address = venue.get("address") or {}
    organizer = (ldjson or {}).get("organizer") or {}

    # Prefer JSON-LD startDate/endDate (explicit UTC offset); fall back to the
    # listing's separate date+time fields (naive local Pacific).
    starts_at = (ldjson or {}).get("startDate")
    ends_at = (ldjson or {}).get("endDate")
    if not starts_at and listing_ev.get("start_date"):
        starts_at = f"{listing_ev['start_date']}T{listing_ev.get('start_time', '00:00')}:00"
    if not ends_at and listing_ev.get("end_date"):
        ends_at = f"{listing_ev['end_date']}T{listing_ev.get('end_time', '00:00')}:00"

    price_min, price_max, is_free = _price_from_offers(ldjson or {})
    ta = listing_ev.get("ticket_availability") or {}
    if is_free is None and "is_free" in ta:
        is_free = ta["is_free"]
    if price_min is None and ta.get("minimum_ticket_price"):
        val = ta["minimum_ticket_price"].get("value")
        price_min = val / 100 if isinstance(val, (int, float)) else None
    if price_max is None and ta.get("maximum_ticket_price"):
        val = ta["maximum_ticket_price"].get("value")
        price_max = val / 100 if isinstance(val, (int, float)) else None

    description = listing_ev.get("full_description") or listing_ev.get("summary") or (
        ldjson.get("description") if ldjson else None
    )

    return {
        "id": f"eventbrite:{eid}",
        "source": "eventbrite",
        "source_id": eid,
        "url": listing_ev.get("url"),
        "title": listing_ev.get("name") or (ldjson or {}).get("name"),
        "description": description,
        "host_name": organizer.get("name"),
        "host_url": organizer.get("url"),
        "venue_name": venue.get("name") or ((ldjson or {}).get("location") or {}).get("name"),
        "address": address.get("localized_address_display") or address.get("streetAddress"),
        "neighborhood": None,  # not present; would need geocoding
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_free": 1 if is_free is True else 0 if is_free is False else None,
        "price_min": price_min,
        "price_max": price_max,
        "rsvp_type": _rsvp_type(listing_ev, ldjson),
        "image_url": (listing_ev.get("image") or {}).get("url"),
        "raw": json.dumps({"listing": listing_ev, "ldjson": ldjson}),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch(limit: int = 50) -> list[dict]:
    all_listing_events: list[dict] = []
    failures: list[str] = []
    for search_url in SEARCH_URLS:
        # One browse URL 405-ing/timing out (Eventbrite edge blocking) must not
        # sink the whole source — collect from whatever pages do respond, and
        # only surface an error if EVERY page failed (a true full block).
        try:
            html = _get(search_url)
            data = _get_search_server_data(html, search_url)
            all_listing_events.extend(_collect_listing_events(data))
        except (requests.RequestException, RuntimeError) as exc:
            failures.append(f"{search_url}: {exc}")
            continue

    if not all_listing_events:
        raise RuntimeError(
            "eventbrite: every browse page failed (likely datacenter-IP edge "
            "blocking — works from residential IPs, 405s from cloud hosts). "
            "Details: " + " | ".join(failures)
        )

    # dedup across the two search pages
    by_id = {ev.get("id"): ev for ev in all_listing_events if ev.get("id")}
    listing_events = list(by_id.values())

    events: list[dict] = []
    for listing_ev in listing_events[:limit]:
        url = listing_ev.get("url")
        ldjson = None
        if url:
            try:
                ldjson = _get_event_ldjson(_get(url))
            except requests.RequestException:
                pass  # keep listing-level data even if detail page fails
            time.sleep(PACING_SECONDS)
        events.append(_map(listing_ev, ldjson))
    return events

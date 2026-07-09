"""GarysGuide (garysguide.com) source — SF tech/startup events listing.

Discovery (from the feasibility spike): GarysGuide serves old-school
server-rendered HTML with no JSON-LD/structured data, so this parses the
markup with regexes against the observed page structure rather than pulling
in a general-purpose HTML parser (bs4 isn't a project dependency). This is
brittle against markup changes but was verified against the live site.

Flow:
    1. GET the SF events listing page (one request; GarysGuide renders
       several weeks of events on a single page, so no pagination needed).
    2. Extract every event detail-page URL from the listing.
    3. GET each detail page (politely paced) and parse it into the sloshbot
       `events` schema (see ARCHITECTURE.md).
    4. Each detail page's "Register" button points at a gary.to redirect
       shortener, not the real host page. Follow that redirect (best-effort)
       to get the actual registration URL — frequently a Luma URL — and
       store the *resolved* URL in `host_url`. This is what lets the dedup
       pass match a GarysGuide event to its Luma twin.
"""
import json
import re
import time
from datetime import datetime, timezone, timedelta

import requests

BASE = "https://www.garysguide.com"
LISTING_URL = f"{BASE}/events?region=sf"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
}
PACING_SECONDS = 0.4

PT = timezone(timedelta(hours=-7))  # PDT; good enough for now, not DST-correct


def _find_event_links(html: str) -> dict[str, str]:
    """Return {source_id: detail_url} for every unique event on the listing page."""
    links = re.findall(
        r"https://www\.garysguide\.com/events/([a-z0-9]+)/([A-Za-z0-9-]+)", html
    )
    seen: dict[str, str] = {}
    for source_id, slug in links:
        seen[source_id] = f"{BASE}/events/{source_id}/{slug}"
    return seen


def _resolve_registration_url(register_url: str, session: requests.Session) -> str | None:
    """Follow the gary.to redirect shortener to the real host/ticketing page
    (Luma, Eventbrite, Partiful, a company site, etc). Best-effort proxy for
    host_url since GarysGuide has no structured host field, and the field
    downstream dedup relies on to match GarysGuide events to Luma events."""
    try:
        resp = session.get(register_url, headers=HEADERS, timeout=10, allow_redirects=True)
        return resp.url
    except requests.RequestException:
        return None


def _parse_event_detail(html: str, url: str, source_id: str, session: requests.Session) -> dict:
    title_m = re.search(r"class=\"flogo\"><a[^>]*>([^<]+)</a>", html)
    title = title_m.group(1).strip() if title_m else None

    # The "Add to Google Calendar" link embeds a full ISO-ish datetime — far
    # more reliable than the human-readable "Jul 08 (Wed) @ 08:00 AM" text.
    gcal_m = re.search(r"dates=(\d{8}T\d{6})/(\d{8}T\d{6})", html)
    starts_at = ends_at = None
    if gcal_m:
        start_raw, end_raw = gcal_m.groups()
        start_dt = datetime.strptime(start_raw, "%Y%m%dT%H%M%S").replace(tzinfo=PT)
        starts_at = start_dt.isoformat()
        if end_raw != start_raw:
            end_dt = datetime.strptime(end_raw, "%Y%m%dT%H%M%S").replace(tzinfo=PT)
            ends_at = end_dt.isoformat()
        # Observed: GarysGuide sets end == start (no real end time given) far
        # more often than not, so ends_at is usually None -> app layer assumes
        # +2h per ARCHITECTURE.md.

    venue_m = re.search(
        r"fa-map-marker-alt fa-lg\"></i></td><td>(?:<b>([^<]*)</b>)?,?\s*([^<]*)</td>",
        html,
    )
    venue_name = venue_m.group(1).strip() if venue_m and venue_m.group(1) else None
    address = venue_m.group(2).strip().lstrip(", ") if venue_m else None

    price_m = re.search(r"fa-ticket fa-lg\"></i>&nbsp;&nbsp;(FREE|\$[\d,.]+)", html)
    is_free = None
    price_min = price_max = None
    if price_m:
        raw_price = price_m.group(1)
        if raw_price == "FREE":
            is_free = 1
            price_min = price_max = 0.0
        else:
            is_free = 0
            price_min = price_max = float(raw_price.replace("$", "").replace(",", ""))

    desc_m = re.search(r"class=\"fdescription\">(.*?)</font>", html, re.DOTALL)
    description = None
    if desc_m:
        raw_desc = desc_m.group(1)
        raw_desc = re.sub(r"<br\s*/?>", "\n", raw_desc)
        raw_desc = re.sub(r"<[^>]+>", "", raw_desc)
        description = raw_desc.strip() or None

    register_m = re.search(
        r"class=\"fbutton\" target=\"_blank\" href=\"([^\"]+)\">(?:&nbsp;|\s)*Register",
        html,
    )
    register_url = register_m.group(1) if register_m else None

    host_url = None
    resolved_registration_url = None
    if register_url:
        resolved_registration_url = _resolve_registration_url(register_url, session)
        host_url = resolved_registration_url  # best-effort proxy; matches dedup on Luma URL

    raw = {
        "register_url": register_url,
        "resolved_registration_url": resolved_registration_url,
    }

    return {
        "id": f"garysguide:{source_id}",
        "source": "garysguide",
        "source_id": source_id,
        "url": url,
        "title": title,
        "description": description,
        "host_name": None,  # not structured on GarysGuide
        "host_url": host_url,
        "venue_name": venue_name,
        "address": address,
        "neighborhood": None,  # not provided; normalize.py/geocoder can backfill
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_free": is_free,
        "price_min": price_min,
        "price_max": price_max,
        "rsvp_type": None,  # not exposed structurally by GarysGuide
        "image_url": None,  # no per-event image on detail page
        "lat": None,  # geocoder backfills later
        "lon": None,
        "raw": json.dumps(raw),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch(limit: int = 50) -> list[dict]:
    session = requests.Session()

    resp = session.get(LISTING_URL, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    html = resp.text

    links = _find_event_links(html)

    events: list[dict] = []
    for source_id, url in list(links.items())[:limit]:
        detail_resp = session.get(url, headers=HEADERS, timeout=15)
        if detail_resp.status_code != 200:
            continue
        events.append(_parse_event_detail(detail_resp.text, url, source_id, session))
        time.sleep(PACING_SECONDS)

    return events

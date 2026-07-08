"""Funcheap SF (sf.funcheap.com) source — ported from the feasibility spike.

Discovery: Funcheap's WordPress posts are ordered by PUBLISH date, not event
           date, so paginating /wp-json/wp/v2/posts overindexes on whatever
           was posted in roughly the last 48 hours and misses events later
           in the window that were posted a while ago. Instead we walk
           Funcheap's per-day listing pages (https://sf.funcheap.com/YYYY/MM/DD/,
           paginated as .../page/N/), which list every event happening on
           that specific calendar day regardless of when it was posted. This
           gives exact date coverage for the target window.
Detail:    each event page embeds a schema.org/Event JSON-LD block with the
           actual structured data (the REST payload's content is stripped).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

BASE = "https://sf.funcheap.com"
HEADERS = {
    "User-Agent": "sloshbot/0.1 (+https://github.com/kingeryzach2002; personal project)"
}
LDJSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
# Event permalinks on a day-listing page are marked with rel="bookmark";
# everything else on the page (nav, categories, sidebar "top" lists) isn't.
BOOKMARK_RE = re.compile(r'href="(https://sf\.funcheap\.com/[^"]+?)"\s+rel="bookmark"')
PACING_SECONDS = 0.5  # polite delay between page fetches
DAYS_AHEAD = 10  # coverage window: today through today + DAYS_AHEAD, inclusive
MAX_PAGES_PER_DAY = 8  # safety cap on per-day listing pagination
TZ = ZoneInfo("America/Los_Angeles")


def _day_url(date) -> str:
    return f"{BASE}/{date.year:04d}/{date.month:02d}/{date.day:02d}/"


def _discover_links_for_day(date) -> list[str]:
    """Fetch a Funcheap per-day listing page (and its pagination) and return
    the ordered, de-duplicated event permalinks posted for that calendar day."""
    base_url = _day_url(date)
    seen: set[str] = set()
    links: list[str] = []
    page = 1
    while page <= MAX_PAGES_PER_DAY:
        url = base_url if page == 1 else f"{base_url}page/{page}/"
        resp = requests.get(url, headers=HEADERS, timeout=15)
        time.sleep(PACING_SECONDS)
        if resp.status_code == 404:
            break
        resp.raise_for_status()
        # A single listing entry can carry rel="bookmark" twice (title link +
        # thumbnail link), so de-dupe within this page's matches too.
        page_links = list(dict.fromkeys(BOOKMARK_RE.findall(resp.text)))
        new_links = [link for link in page_links if link not in seen]
        if not new_links:  # ran off the end of this day's pagination
            break
        for link in new_links:
            seen.add(link)
            links.append(link)
        page += 1
    return links


def _discover_posts(limit: int) -> list[dict]:
    """Walk per-day listing pages for the next DAYS_AHEAD days to build an
    ordered, de-duplicated list of event post links within the target window."""
    today = datetime.now(TZ).date()
    seen: set[str] = set()
    posts: list[dict] = []
    for offset in range(DAYS_AHEAD + 1):
        if len(posts) >= limit:
            break
        day = today + timedelta(days=offset)
        for link in _discover_links_for_day(day):
            if link in seen:
                continue
            seen.add(link)
            posts.append({"id": None, "link": link})
            if len(posts) >= limit:
                break
    return posts


def _fetch_event_ldjson(url: str) -> dict | None:
    """Fetch an event page and extract the schema.org Event JSON-LD block."""
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    for block in LDJSON_RE.findall(resp.text):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        if data.get("@type") == "Event":
            return data
    return None


def _map(source_id: str, ld: dict, page_url: str) -> dict:
    """Map a schema.org Event JSON-LD blob onto the sloshbot `events` row shape."""
    offers = ld.get("offers") or {}
    location = ld.get("location") or {}

    price = offers.get("price")
    try:
        price_val = float(price) if price is not None else None
    except (TypeError, ValueError):
        price_val = None

    is_free = None
    if price_val is not None:
        is_free = 1 if price_val == 0 else 0

    outbound_url = offers.get("url")  # canonical outbound link (Eventbrite/FB/org site)
    host_name = None
    if outbound_url:
        try:
            host_name = urlparse(outbound_url).netloc or None
        except ValueError:
            host_name = None

    return {
        "id": f"funcheap:{source_id}",
        "source": "funcheap",
        "source_id": source_id,
        "url": page_url,  # Funcheap's own page is the canonical event URL
        "title": ld.get("name"),
        "description": ld.get("description"),
        "host_name": host_name,   # best effort: derived from outbound link's domain
        "host_url": outbound_url,
        "venue_name": location.get("name"),
        "address": location.get("address"),
        "neighborhood": None,     # not structured; would need geocoding/NLP later
        "starts_at": ld.get("startDate"),  # has -07:00 offset; normalize.py converts
        "ends_at": ld.get("endDate"),
        "is_free": is_free,
        "price_min": price_val,
        "price_max": price_val,   # JSON-LD only gives a single price point
        "rsvp_type": None,        # not present in Funcheap markup
        "image_url": ld.get("image"),
        "raw": json.dumps(ld),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def _within_window(start_raw: str | None, window_start, window_end) -> bool:
    """True if the JSON-LD startDate falls within [window_start, window_end].

    Recurring-event pages sometimes carry a JSON-LD startDate for a
    different occurrence than the one linked from a given day's listing
    page; this is a defensive filter so those don't leak outside the
    10-day coverage window. Unparseable dates are let through rather than
    silently dropped.
    """
    if not start_raw:
        return True
    try:
        dt = datetime.fromisoformat(start_raw)
    except ValueError:
        return True
    local_date = dt.astimezone(TZ).date() if dt.tzinfo else dt.date()
    return window_start <= local_date <= window_end


def fetch(limit: int = 50) -> list[dict]:
    posts = _discover_posts(limit)
    window_start = datetime.now(TZ).date()
    window_end = window_start + timedelta(days=DAYS_AHEAD)
    events: list[dict] = []
    for post in posts:
        ld = _fetch_event_ldjson(post["link"])
        time.sleep(PACING_SECONDS)
        if ld is None:  # non-event post (roundups, news); skip
            continue
        if not _within_window(ld.get("startDate"), window_start, window_end):
            continue
        source_id = post["link"].rstrip("/").rsplit("/", 1)[-1]
        events.append(_map(source_id, ld, post["link"]))
    return events

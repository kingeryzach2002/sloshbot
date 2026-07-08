"""Funcheap SF (sf.funcheap.com) source — ported from the feasibility spike.

Discovery: WordPress REST API (/wp-json/wp/v2/posts) lists recent event posts.
Detail:    each event page embeds a schema.org/Event JSON-LD block with the
           actual structured data (the REST payload's content is stripped).
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

BASE = "https://sf.funcheap.com"
HEADERS = {
    "User-Agent": "sloshbot/0.1 (+https://github.com/kingeryzach2002; personal project)"
}
LDJSON_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
PACING_SECONDS = 0.5  # polite delay between page fetches
WP_MAX_PER_PAGE = 100


def _discover_posts(limit: int) -> list[dict]:
    """Use the WP REST API to list recent event posts (id, link, title)."""
    posts: list[dict] = []
    page = 1
    while len(posts) < limit:
        resp = requests.get(
            f"{BASE}/wp-json/wp/v2/posts",
            params={"per_page": min(limit - len(posts), WP_MAX_PER_PAGE), "page": page},
            headers=HEADERS,
            timeout=15,
        )
        if resp.status_code == 400:  # past the last page
            break
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        posts.extend(batch)
        page += 1
    return [{"id": p["id"], "link": p["link"]} for p in posts[:limit]]


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


def fetch(limit: int = 50) -> list[dict]:
    posts = _discover_posts(limit)
    events: list[dict] = []
    for post in posts:
        ld = _fetch_event_ldjson(post["link"])
        time.sleep(PACING_SECONDS)
        if ld is None:  # non-event post (roundups, news); skip
            continue
        source_id = post["link"].rstrip("/").rsplit("/", 1)[-1]
        events.append(_map(source_id, ld, post["link"]))
    return events

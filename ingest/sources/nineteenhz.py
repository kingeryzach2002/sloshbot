"""19hz.info source — Bay Area electronic music event listings (HTML scrape).

One request gets the whole Bay Area table (no pagination, no API). We filter
down to San Francisco proper and to the next ~14 days: the table runs weeks/
months into the future and also covers the whole Bay Area, and ingesting the
entire long tail would dwarf every other source.

No description field exists on the source at all — the listing has only
tags/age/price/organizer, so `description` is synthesized from those (thin,
structural text; not prose). No addresses either, so `address`/`lat`/`lon`
are left None for the geocoder (keyed off `venue_name`) to backfill later.
`neighborhood` is only ever city-level here (e.g. "San Francisco"), not a
real neighborhood.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import requests

URL = "https://19hz.info/eventlisting_BayArea.php"
HEADERS = {"User-Agent": "Mozilla/5.0 (sloshbot/0.1; personal event aggregator)"}
WINDOW_DAYS = 14
SF_CITY_NAMES = {"san francisco", "sf"}

# One <tr> per event. Columns (see thead):
# Date/Time | Event Title @ Venue | Tags | Price | Age | Organizers | Links | (hidden sortable ISO date div)
#
# QUIRK: 19hz's markup is NOT well-formed — the 2nd <td> (title @ venue) is
# missing its closing </td> (verified against raw HTML), so a naive
# `<td>(.*?)</td>` regex silently shifts every downstream cell by one and
# corrupts the whole row. We split on literal "<td>" open-tag boundaries
# instead of relying on matched closes, which is robust to the missing tag.
ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.DOTALL)
TAG_STRIP_RE = re.compile(r"<[^>]+>")
TRAILING_TD_TR_RE = re.compile(r"(</td>\s*)+$", re.DOTALL)
LINK_RE = re.compile(r"<a\s+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", re.DOTALL)
ISO_DATE_RE = re.compile(r"<div class='shrink'>(\d{4}/\d{2}/\d{2})</div>")

START_TIME_RE = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)", re.IGNORECASE)


def _strip_tags(s: str) -> str:
    return TAG_STRIP_RE.sub("", s).strip()


def _parse_price_age(cell_text: str):
    """'free b4 1030 / $5 | 21+' -> (is_free, price_min, price_max, age)"""
    text = _strip_tags(cell_text)
    if "|" in text:
        price_part, age_part = text.split("|", 1)
    else:
        price_part, age_part = text, ""
    price_part = price_part.strip()
    age = age_part.strip() or None

    prices = [float(m) for m in re.findall(r"\$(\d+(?:\.\d+)?)", price_part)]
    is_free = 1 if re.search(r"\bfree\b", price_part, re.IGNORECASE) else (0 if prices else None)
    price_min = min(prices) if prices else (0.0 if is_free else None)
    price_max = max(prices) if prices else (0.0 if is_free else None)
    return is_free, price_min, price_max, age


def _parse_starts_at(date_cell_text: str, iso_date: str) -> str | None:
    """
    date_cell_text like 'Tue: Jul 7 <br />(8pm-12am)' (raw HTML). iso_date is
    the site's own sortable 'YYYY/MM/DD' (start date) — ground truth for the
    date; we still need to extract a start *time* from the parenthetical.
    Returns naive ISO 8601 string (America/Los_Angeles implied) or None.
    """
    year, month, day = (int(x) for x in iso_date.split("/"))
    m = START_TIME_RE.search(date_cell_text)
    hour, minute = 20, 0  # default 8pm if time missing/unparseable
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        ampm = m.group(3).lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    try:
        dt = datetime(year, month, day, hour, minute)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_title_venue(cell_html: str):
    """
    '<a href="URL">Title</a> @ Venue Name (City)'
    Returns (title, url, venue_name, city)
    """
    m = LINK_RE.search(cell_html)
    if m:
        url = m.group(1)
        title = _strip_tags(m.group(2))
        rest = cell_html[m.end():]
    else:
        url = None
        title = _strip_tags(cell_html.split("@")[0]) if "@" in cell_html else _strip_tags(cell_html)
        rest = cell_html

    rest = _strip_tags(rest)
    venue_name, city = None, None
    if "@" in rest:
        venue_part = rest.split("@", 1)[1].strip()
        city_m = re.search(r"\(([^)]+)\)\s*$", venue_part)
        if city_m:
            city = city_m.group(1).strip()
            venue_name = venue_part[: city_m.start()].strip()
        else:
            venue_name = venue_part.strip() or None
    return title, url, venue_name, city


def _split_cells(row_html: str) -> list[str]:
    """Split a <tr> body into its <td> cells, tolerant of 19hz's missing
    closing </td> on the title/venue column (see ROW_RE comment above)."""
    parts = row_html.split("<td>")[1:]  # drop text before first <td>
    return [TRAILING_TD_TR_RE.sub("", p) for p in parts]


def _map(row_html: str) -> dict | None:
    cells = _split_cells(row_html)
    if len(cells) < 5:
        return None  # header row or malformed
    date_cell, title_venue_cell, tags_cell, price_age_cell, organizers_cell = cells[:5]
    links_cell = cells[5] if len(cells) > 5 else ""

    iso_m = ISO_DATE_RE.search(row_html)
    if not iso_m:
        return None  # can't anchor a date -> skip (rare, e.g. malformed row)
    iso_date = iso_m.group(1)

    title, url, venue_name, city = _parse_title_venue(title_venue_cell)
    tags = [t.strip() for t in _strip_tags(tags_cell).split(",") if t.strip()]
    is_free, price_min, price_max, age = _parse_price_age(price_age_cell)
    organizers = _strip_tags(organizers_cell) or None
    starts_at = _parse_starts_at(date_cell, iso_date)
    if not starts_at or not title:
        return None

    extra_links = [(href, _strip_tags(text)) for href, text in LINK_RE.findall(links_cell)]

    # description: 19hz has NO event description field at all in the listing.
    # Best we can synthesize: tags + age, which is thin/structural, not prose.
    description_parts = []
    if tags:
        description_parts.append("Genres: " + ", ".join(tags))
    if age:
        description_parts.append(age)
    description = " | ".join(description_parts) or None

    source_id_raw = f"{title}|{venue_name}|{starts_at}"
    source_id = hashlib.sha1(source_id_raw.encode("utf-8")).hexdigest()[:16]

    return {
        "id": f"19hz:{source_id}",
        "source": "19hz",
        "source_id": source_id,
        "url": url or URL,
        "title": title,
        "description": description,
        "host_name": organizers,
        "host_url": extra_links[0][0] if extra_links else None,
        "venue_name": venue_name,
        "address": None,  # not provided by 19hz; geocoder backfills from venue_name
        "neighborhood": city,  # city-level only (e.g. "San Francisco"), not neighborhood
        "starts_at": starts_at,
        "ends_at": None,  # end time present in some rows but not reliably parsed
        "is_free": is_free,
        "price_min": price_min,
        "price_max": price_max,
        "rsvp_type": None,  # not exposed by 19hz
        "image_url": None,
        "lat": None,
        "lon": None,
        "raw": json.dumps({
            "date_cell": _strip_tags(date_cell),
            "tags": tags,
            "price_age_raw": _strip_tags(price_age_cell),
            "organizers_raw": organizers,
            "city_raw": city,
            "links": extra_links,
        }),
        "scraped_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch(limit: int = 50) -> list[dict]:
    resp = requests.get(URL, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    html = resp.text

    now = datetime.now()
    window_end = now + timedelta(days=WINDOW_DAYS)

    events = []
    for row_html in ROW_RE.findall(html):
        event = _map(row_html)
        if not event:
            continue
        city = (event["neighborhood"] or "").strip().lower()
        if city not in SF_CITY_NAMES:
            continue
        try:
            starts_dt = datetime.fromisoformat(event["starts_at"])
        except ValueError:
            continue
        if not (now <= starts_dt <= window_end):
            continue
        events.append(event)

    return events[:limit]

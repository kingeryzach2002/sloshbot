"""Normalize source dicts into the events table: validate, fix timezones, upsert.

Timezone policy (see ARCHITECTURE.md — starts_at is ISO 8601 Pacific):
- Aware timestamps (Luma's UTC "Z", Funcheap/Eventbrite's "-07:00") are
  converted to America/Los_Angeles.
- Naive timestamps are assumed to already be Pacific and get the offset attached.
"""
from __future__ import annotations

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from db import get_conn
from ingest.tags import extract_tags

PACIFIC = ZoneInfo("America/Los_Angeles")

REQUIRED_FIELDS = ("id", "title", "url", "starts_at")

COLUMNS = (
    "id", "source", "source_id", "url", "title", "description",
    "host_name", "host_url", "venue_name", "address", "neighborhood",
    "starts_at", "ends_at", "is_free", "price_min", "price_max",
    "rsvp_type", "image_url", "lat", "lon", "raw", "scraped_at",
)

# Human-readable text fields; sources (Funcheap especially) ship these with
# HTML entities still encoded ("&#8220;", "&amp;") — decode on the way in.
TEXT_COLUMNS = ("title", "description", "host_name", "venue_name", "address",
                "neighborhood")

MUTABLE_COLUMNS = (
    "title", "description", "starts_at", "ends_at",
    "is_free", "price_min", "price_max", "rsvp_type", "lat", "lon",
    "raw", "scraped_at",
)

_UPSERT_SQL = f"""
INSERT INTO events ({", ".join(COLUMNS)})
VALUES ({", ".join(":" + c for c in COLUMNS)})
ON CONFLICT(id) DO UPDATE SET
  {", ".join(f"{c} = excluded.{c}" for c in MUTABLE_COLUMNS)}
"""


def to_pacific(ts: str | None) -> str | None:
    """Normalize an ISO-8601-ish timestamp string to Pacific ISO 8601."""
    if not ts:
        return None
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=PACIFIC)  # naive => assume already Pacific
    else:
        dt = dt.astimezone(PACIFIC)
    return dt.isoformat()


def normalize(rows: list[dict]) -> list[dict]:
    """Validate and timezone-normalize; drops invalid rows."""
    out: list[dict] = []
    for row in rows:
        if any(not row.get(f) for f in REQUIRED_FIELDS):
            continue
        try:
            starts_at = to_pacific(row["starts_at"])
        except ValueError:
            continue  # unparseable start time => unusable event
        try:
            ends_at = to_pacific(row.get("ends_at"))
        except ValueError:
            ends_at = None  # bad end time is survivable
        normalized = {c: row.get(c) for c in COLUMNS}
        for c in TEXT_COLUMNS:
            if isinstance(normalized[c], str):
                normalized[c] = html.unescape(normalized[c]).strip()
        normalized["starts_at"] = starts_at
        normalized["ends_at"] = ends_at
        # free-form; stored in event_tags. Sources may pass explicit tags;
        # otherwise derive from raw categories + title/description keywords.
        normalized["tags"] = row.get("tags") or extract_tags(
            row.get("source"), row.get("raw"), row.get("title"), row.get("description"))
        out.append(normalized)
    return out


def upsert(rows: list[dict]) -> dict:
    """Validate + normalize + upsert rows. Returns {inserted, updated, skipped}."""
    valid = normalize(rows)
    skipped = len(rows) - len(valid)
    inserted = updated = 0
    with get_conn() as conn:
        for row in valid:
            exists = conn.execute(
                "SELECT 1 FROM events WHERE id = ?", (row["id"],)
            ).fetchone()
            tags = row.pop("tags")
            conn.execute(_UPSERT_SQL, row)
            if tags:  # replace so re-scrapes stay idempotent
                conn.execute("DELETE FROM event_tags WHERE event_id = ?", (row["id"],))
                conn.executemany(
                    "INSERT INTO event_tags (event_id, tag) VALUES (?, ?)",
                    [(row["id"], t) for t in dict.fromkeys(tags)],
                )
            if exists:
                updated += 1
            else:
                inserted += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}

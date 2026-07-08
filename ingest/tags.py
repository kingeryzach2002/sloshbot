"""Derive free-form event tags from source metadata + title/description keywords.

Luma and Eventbrite carry structured categories in `raw`; Funcheap (schema.org
JSON-LD) has none, so keyword tags are the only signal there. Tags are
lowercase, deduped, capped — an arbitrary set per event, no fixed vocabulary.
"""
from __future__ import annotations

import json
import re

MAX_TAGS = 5

# Keyword patterns matched against title + description (case-insensitive).
_KEYWORD_TAGS: list[tuple[str, str]] = [
    (r"happy hour", "happy hour"),
    (r"hackathon", "hackathon"),
    (r"\bpanel\b", "panel"),
    (r"\bmixer\b|networking", "networking"),
    (r"workshop", "workshop"),
    (r"demo day", "demo day"),
    (r"launch party|product launch", "launch"),
    (r"\bparty\b", "party"),
    (r"comedy|stand[- ]?up|improv", "comedy"),
    (r"\bconcert\b|live music|\bdj\b|open mic", "music"),
    (r"gallery|art show|art opening|exhibition", "art"),
    (r"festival|block party|street fair", "festival"),
    (r"yoga|run club|pilates|workout|5k\b", "fitness"),
    (r"trivia", "trivia"),
    (r"karaoke", "karaoke"),
    (r"(wine|beer|whiskey|sake|cocktail).{0,20}tasting|tasting room", "tasting"),
    (r"pop[- ]?up|food truck|supper club", "food"),
    (r"book club|author talk|reading", "books"),
    (r"screening|film festival|movie night", "film"),
    (r"talk\b|fireside|keynote|lecture", "talk"),
]

# Eventbrite tag display names too generic to be worth a chip.
_EB_SKIP = {"other", "seminar", "class", "expo"}

# Verbose source category names → the shorter keyword-tag vocabulary.
_RENAME = {
    "party or social gathering": "party",
    "class, training, or workshop": "workshop",
    "seminar or talk": "talk",
    "concert or performance": "music",
    "food & drink": "food",
    "arts & culture": "art",
    "business & professional": "business",
    "science & technology": "tech",
    "health & wellness": "wellness",
}


def _source_tags(source: str, raw: dict) -> list[str]:
    if source == "luma":
        return [c["name"].lower() for c in raw.get("categories") or [] if c.get("name")]
    if source == "eventbrite":
        names = [t.get("display_name", "").lower()
                 for t in (raw.get("listing") or {}).get("tags") or []]
        return [n for n in names if n and n not in _EB_SKIP]
    return []


def extract_tags(source: str, raw: str | dict | None,
                 title: str | None, description: str | None) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = {}
    tags = [_RENAME.get(t, t) for t in _source_tags(source, raw or {})]
    text = f"{title or ''} {description or ''}".lower()
    tags += [tag for pat, tag in _KEYWORD_TAGS if re.search(pat, text)]
    return list(dict.fromkeys(tags))[:MAX_TAGS]


def backfill() -> None:
    """Re-derive tags for every event already in the DB (replaces existing).

    Usage: uv run python -m ingest.tags
    """
    from db import get_conn, init_db
    init_db()
    tagged = 0
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, source, raw, title, description FROM events").fetchall()
        for r in rows:
            tags = extract_tags(r["source"], r["raw"], r["title"], r["description"])
            conn.execute("DELETE FROM event_tags WHERE event_id = ?", (r["id"],))
            if tags:
                conn.executemany(
                    "INSERT INTO event_tags (event_id, tag) VALUES (?, ?)",
                    [(r["id"], t) for t in tags])
                tagged += 1
    print(f"Tagged {tagged}/{len(rows)} events")


if __name__ == "__main__":
    backfill()

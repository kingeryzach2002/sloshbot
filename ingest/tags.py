"""Derive event tags from source metadata + title/description keywords.

Luma and Eventbrite carry structured categories in `raw`; Funcheap (schema.org
JSON-LD) has none, so keyword tags are the only signal there. Output is
constrained to ALLOWED_TAGS — the hand-pruned vocabulary (see tags.csv for the
pruning pass); source categories outside it are mapped in via _RENAME where
sensible and dropped otherwise.
"""
from __future__ import annotations

import json
import re

MAX_TAGS = 5

# The pruned vocabulary — the only tags that ever reach event_tags.
ALLOWED_TAGS = {
    "music", "party", "festival", "art", "tech", "comedy", "networking",
    "workshop", "ai", "talk", "food", "business", "climate", "panel", "cooking",
}

# Keyword patterns matched against title + description (case-insensitive).
# Every target must be in ALLOWED_TAGS.
_KEYWORD_TAGS: list[tuple[str, str]] = [
    (r"happy hour", "party"),
    (r"hackathon", "tech"),
    (r"\bpanel\b", "panel"),
    (r"\bmixer\b|networking|demo day|meetup", "networking"),
    (r"workshop", "workshop"),
    (r"launch party|product launch|\bparty\b", "party"),
    (r"comedy|stand[- ]?up|improv", "comedy"),
    (r"\bconcert\b|live music|\bdj\b|open mic", "music"),
    (r"gallery|art show|art opening|exhibition", "art"),
    (r"festival|block party|street fair", "festival"),
    (r"(wine|beer|whiskey|sake|cocktail).{0,20}tasting|tasting room", "food"),
    (r"pop[- ]?up|food truck|supper club", "food"),
    (r"cooking class|sushi making|baking|sourdough", "cooking"),
    (r"talk\b|fireside|keynote|lecture", "talk"),
    (r"\bai\b|artificial intelligence|\bllm\b", "ai"),
    (r"climate", "climate"),
]

# Eventbrite tag display names too generic to be worth a chip.
_EB_SKIP = {"other", "seminar", "class", "expo"}

# Source category names → the pruned vocabulary. Categories not renamed and
# not already in ALLOWED_TAGS are dropped by the final filter.
_RENAME = {
    "party or social gathering": "party",
    "class, training, or workshop": "workshop",
    "seminar or talk": "talk",
    "concert or performance": "music",
    "food & drink": "food",
    "arts & culture": "art",
    "business & professional": "business",
    "science & technology": "tech",
    "concert": "music",
    "dj": "music",
    "nightlife": "party",
    "r&b": "music",
    "reggaeton": "music",
    "dembow": "music",
    "bollywood": "music",
    "world": "music",
    "alternative": "music",
    "dayparty": "party",
    "launch party": "party",
    "opening": "art",
    "performing & visual arts": "art",
    "high tech": "tech",
    "data_analysis": "tech",
    "meetup": "networking",
    "meeting or networking event": "networking",
    "reception": "networking",
    "demo day": "networking",
    "conference": "talk",
    "lecture": "talk",
    "foodie": "food",
    "baking": "cooking",
    "sourdough": "cooking",
    "kitchen": "cooking",
    "career": "business",
    "careerfair": "business",
    "employment": "business",
    "recruitment": "business",
    "sales & marketing": "business",
    "smallbusiness": "business",
    "environment": "climate",
    "festival or fair": "festival",
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
    tags = [t for t in tags if t in ALLOWED_TAGS]
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

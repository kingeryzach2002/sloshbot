"""The in-memory contract between the app's data / filter / rank / present
layers — and the shape serialized over the JSON API.

`Event` is a Pydantic model so it validates on construction and serializes for
free (`.to_dict()`). It also supports dict-style access (`e["tier"]`, `e.get(...)`,
`"booze" in e.scores`) so templates, tooling, and any legacy `e[...]` code work
unchanged alongside attribute access.

Field provenance (which layer fills each) is noted below. Everything past the
base columns has a default, so a bare `Event.from_row(row)` is valid and later
stages enrich it in place.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class Event(BaseModel):
    # Allow later stages to set derived attributes; ignore unknown DB columns.
    model_config = ConfigDict(extra="ignore")

    # --- base: straight from the events row (data layer) ---
    id: str
    source: str
    source_id: str = ""
    url: str
    title: str
    description: Optional[str] = None
    host_name: Optional[str] = None
    host_url: Optional[str] = None
    venue_name: Optional[str] = None
    address: Optional[str] = None
    neighborhood: Optional[str] = None
    starts_at: str
    ends_at: Optional[str] = None
    is_free: Optional[int] = None
    price_min: Optional[float] = None
    price_max: Optional[float] = None
    rsvp_type: Optional[str] = None
    image_url: Optional[str] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    duplicate_of: Optional[str] = None

    # --- attached by the data layer (joins) ---
    scores: dict[str, float] = {}          # scorer -> 0..1
    rationales: dict[str, str] = {}        # scorer -> human "why"
    tags: list[str] = []
    feedback: set[str] = set()             # verdicts present for this event
    host_rep: Optional[dict[str, int]] = None   # {"ok": n, "miss": n} or None

    # --- computed by rank/policy (app) ---
    composite: float = 0.0                 # weighted score across present scorers
    tier: str = "maybe"                    # 'confident' | 'maybe' | 'hidden'
    match: float = 0.0                     # view-facing rank score (set by routes)

    # --- computed by the presenter ---
    distance_mi: Optional[float] = None
    gcal: str = ""
    start_dt: Optional[datetime] = None
    end_dt: Optional[datetime] = None

    @classmethod
    def from_row(cls, row: Any) -> "Event":
        """Build from a sqlite3.Row (or any mapping). Unknown columns ignored."""
        return cls(**dict(row))

    def to_dict(self) -> dict:
        """JSON-ready dict (sets -> sorted lists, datetimes -> ISO strings)."""
        return self.model_dump(mode="json")

    # --- dict-compatibility shims (templates / tooling / legacy access) ---
    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)

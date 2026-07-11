"""Filter layer: pure predicates over (events, FilterState). The ONE definition
of which events are visible, and the ONE place chip counts are computed (so a
chip can never advertise a count that doesn't show up). No DB, no ranking.

Replaces, from the old app/main.py:
  - the hard-filter block inside load_events (main.py:196-209): source, price,
    max_mi (distance), tag, min_booze.
  - _visible_events (main.py:61-75), tag_counts (78-94), source_counts (97-105),
    price_counts (108-117)  ->  a single chip_counts().
  - the settings parsers included_tags/included_sources/price_filter (main.py:37-49).

CONTRACT
  FilterState(tags, sources, price, max_mi, min_booze)
      The resolved, view-agnostic filter selection. `tags`/`sources` are
      lists[str], `price` is "free"|"paid"|"", `max_mi`/`min_booze` are
      float|None. `.to_dict()` renders it JSON-serializable for the
      `active_filters` template context key.

  from_settings(settings: dict) -> FilterState
      Parses a user's sticky-default settings row into a FilterState:
        - included_tags/included_sources: comma split, drop empties.
        - price_filter: "free"|"paid" else "".
        - max_mi/min_booze: float parse, None on missing/garbage.
      This is the ONE place settings-shape knowledge lives.

  from_query(tags, sources, price, max_mi, min_booze) -> FilterState
      Same parsing rules as from_settings, but from raw query-string values
      (tags/sources are comma-separated strings, price is a raw string,
      max_mi/min_booze are already-parsed float|None from FastAPI).

  apply(events, fs: FilterState, has_home: bool = False) -> list[Event]
      Keep an event iff it passes ALL of these hard filters (order-independent):
        - source:   if fs.sources set, keep only events whose source is in it.
        - price:    if fs.price set, keep only is_free==(1 if "free" else 0);
                    unknown is_free (None) matches NEITHER free nor paid.
        - max_mi:   distance_mi is None in two different situations that MUST
                    be told apart, which is why `has_home` exists:
                      * no home set at all -> distance is uncomputable for
                        EVERY event -> the filter has no reference point and
                        stays inert (drops nothing for distance), even if
                        fs.max_mi carries a sticky/URL default with no home.
                      * home IS set but this one event never got geocoded
                        (no lat/lon) -> its distance can't be confirmed
                        <= max_mi, so it must be dropped, same as an event
                        confirmed to be too far away.
                    Concretely: if fs.max_mi is given and has_home, drop
                    events whose distance_mi is None OR > max_mi; if not
                    has_home, distance_mi is None for every event (enrich
                    never had a reference point either) and the filter
                    drops nothing, matching the old behavior.
                    (distance_mi must already be set by presenter.enrich;
                    has_home must be derived from the SAME settings passed
                    to enrich, via presenter.home_coords(settings) is not
                    None — otherwise this and enrich can disagree about
                    whether a home is set.)
        - tag:      if fs.tags set, keep only events whose tags intersect
                    fs.tags — an event with no tags at all is dropped too
                    (an active tag filter means "only these tags").
        - min_booze:if given, drop events whose scores.get("booze", 0) < fs.min_booze
                    (unscored counts as 0 here).
      Returns a new list; does not mutate or reorder beyond dropping.

  chip_counts(events) -> {"tags": [{"tag","n"}...], "sources": [{"source","n"}...],
                          "price": {"free": int, "paid": int}}
      Computed over the subset of `events` that passes the hidden-tier cutoff
      (scores.get("booze") is None OR >= scoring.weights.TIER_MAYBE) — the same
      cutoff policy.rank uses to drop events. Tags/sources sorted by count desc
      then name asc. price counts is_free==1 / ==0 (None counts toward neither).
      NOTE: chip_counts does NOT apply the tag/source/price/distance filters —
      counts are independent of the active selection (matches old behavior).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from app.models import Event
from scoring.weights import TIER_MAYBE


@dataclass
class FilterState:
    tags: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    price: str = ""
    max_mi: float | None = None
    min_booze: float | None = None

    def to_dict(self) -> dict:
        return {
            "tags": self.tags,
            "sources": self.sources,
            "price": self.price,
            "max_mi": self.max_mi,
            "min_booze": self.min_booze,
        }


def _split_csv(raw: str) -> list[str]:
    return [t for t in raw.split(",") if t]


def _parse_price(raw: str) -> str:
    return raw if raw in ("free", "paid") else ""


def _parse_float(raw) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def from_settings(settings: dict) -> FilterState:
    """The ONE place settings-shape knowledge lives."""
    return FilterState(
        tags=_split_csv(settings.get("included_tags", "")),
        sources=_split_csv(settings.get("included_sources", "")),
        price=_parse_price(settings.get("price_filter", "")),
        max_mi=_parse_float(settings.get("max_mi")),
        min_booze=_parse_float(settings.get("min_booze")),
    )


def from_query(tags: str | None, sources: str | None, price: str | None,
               max_mi: float | None, min_booze: float | None) -> FilterState:
    return FilterState(
        tags=_split_csv(tags or ""),
        sources=_split_csv(sources or ""),
        price=_parse_price(price or ""),
        max_mi=_parse_float(max_mi),
        min_booze=_parse_float(min_booze),
    )


def apply(events: list[Event], fs: FilterState, has_home: bool = False) -> list[Event]:
    inc_tags = set(fs.tags)
    inc_sources = set(fs.sources)
    price = fs.price

    out = []
    for e in events:
        if inc_sources and e.source not in inc_sources:
            continue  # source filter is a hard filter — every event has a source
        if price and e.is_free != (1 if price == "free" else 0):
            continue  # unknown is_free matches neither free nor paid
        if fs.max_mi is not None and has_home:
            # A home exists, so distance_mi is None only because THIS event
            # never got geocoded — its distance can't be confirmed within
            # range, so treat it like a too-far event rather than letting it
            # slip through unverified.
            if e.distance_mi is None or e.distance_mi > fs.max_mi:
                continue
        # else: no home set -> distance_mi is None for every event (enrich
        # had no reference point either) -> the distance filter has nothing
        # to compare against and stays inert, even if fs.max_mi is a sticky
        # default from settings/URL for a visitor who never set a home.
        if inc_tags and not (inc_tags & set(e.tags)):
            continue  # active tag filter means "only these tags" — untagged events are dropped too
        if fs.min_booze is not None and e.scores.get("booze", 0) < fs.min_booze:
            continue  # confidence filter: unscored events count as 0, unlike the tag filter
        out.append(e)
    return out


def chip_counts(events: list[Event]) -> dict:
    visible = [e for e in events
               if e.scores.get("booze") is None or e.scores["booze"] >= TIER_MAYBE]

    tag_n: dict[str, int] = defaultdict(int)
    source_n: dict[str, int] = defaultdict(int)
    free = paid = 0
    for e in visible:
        for t in e.tags:
            tag_n[t] += 1
        source_n[e.source] += 1
        if e.is_free == 1:
            free += 1
        elif e.is_free == 0:
            paid += 1

    return {
        "tags": [{"tag": t, "n": n} for t, n in sorted(tag_n.items(), key=lambda kv: (-kv[1], kv[0]))],
        "sources": [{"source": s, "n": n} for s, n in sorted(source_n.items(), key=lambda kv: (-kv[1], kv[0]))],
        "price": {"free": free, "paid": paid},
    }

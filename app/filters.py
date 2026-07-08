"""Filter layer: pure predicates over (events, settings). The ONE definition of
which events are visible, and the ONE place chip counts are computed (so a chip
can never advertise a count that doesn't show up). No DB, no ranking.

Replaces, from the old app/main.py:
  - the hard-filter block inside load_events (main.py:196-209): source, price,
    max_mi (distance), tag, min_booze.
  - _visible_events (main.py:61-75), tag_counts (78-94), source_counts (97-105),
    price_counts (108-117)  ->  a single chip_counts().
  - the settings parsers included_tags/included_sources/price_filter (main.py:37-49).

CONTRACT
  included_tags(settings)   -> list[str]     # comma split of settings["included_tags"], drop empties
  included_sources(settings)-> list[str]     # same for "included_sources"
  price_filter(settings)    -> str           # "free"|"paid"|"" (anything else -> "")

  apply(events, settings, max_mi=None, min_booze=None) -> list[Event]
      Keep an event iff it passes ALL of these hard filters (order-independent):
        - source:   if included_sources set, keep only events whose source is in it.
        - price:    if price_filter set, keep only is_free==(1 if "free" else 0);
                    unknown is_free (None) matches NEITHER free nor paid.
        - max_mi:   if given, drop events with distance_mi is not None and > max_mi;
                    events with distance_mi is None are NEVER dropped by it.
                    (distance_mi must already be set by presenter.enrich.)
        - tag:      if included_tags set, drop an event only if it HAS tags and none
                    of them intersect included_tags; untagged events are never dropped.
        - min_booze:if given, drop events whose scores.get("booze", 0) < min_booze
                    (unscored counts as 0 here — unlike the tag filter).
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

from app.models import Event
from scoring.weights import TIER_MAYBE


def included_tags(settings: dict) -> list[str]:
    raw = settings.get("included_tags", "")
    return [t for t in raw.split(",") if t]


def included_sources(settings: dict) -> list[str]:
    raw = settings.get("included_sources", "")
    return [s for s in raw.split(",") if s]


def price_filter(settings: dict) -> str:
    v = settings.get("price_filter", "")
    return v if v in ("free", "paid") else ""


def apply(events: list[Event], settings: dict,
          max_mi: float | None = None, min_booze: float | None = None) -> list[Event]:
    inc_tags = set(included_tags(settings))
    inc_sources = set(included_sources(settings))
    price = price_filter(settings)

    out = []
    for e in events:
        if inc_sources and e.source not in inc_sources:
            continue  # source filter is a hard filter — every event has a source
        if price and e.is_free != (1 if price == "free" else 0):
            continue  # unknown is_free matches neither free nor paid
        if max_mi is not None and e.distance_mi is not None and e.distance_mi > max_mi:
            continue  # events with unknown location are never dropped by the filter
        if inc_tags and e.tags and not (inc_tags & set(e.tags)):
            continue  # tag filter is a hard filter; untagged events are never dropped by it
        if min_booze is not None and e.scores.get("booze", 0) < min_booze:
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

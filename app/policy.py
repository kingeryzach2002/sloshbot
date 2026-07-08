"""Ranking policy (app presentation layer). Turns scored events into tiered,
capped, sorted output. It CALLS the scoring domain (scoring.weights.composite /
tier) but owns the *presentation* decisions the product makes on top of scores:
what counts as unscored, which sources may never be "confident", and the
scarcity cap. Per the agreed design, MAX_PICKS scarcity is an app policy, not a
scoring-domain rule.

Replaces the ranking half of the old app/main.py::load_events
(main.py:207-238): composite/tier assignment, the unscored->maybe default, the
NEVER_CONFIDENT_SOURCES demotion, dropping hidden, the per-day MAX_PICKS cap,
and the final sort.

DEPENDS ON: presenter.enrich having already set start_dt on each event (the
per-day cap groups by start_dt.date()). So the pipeline order is:
    data.fetch_events -> presenter.enrich -> filters.apply -> policy.rank

CONTRACT
  rank(events, weights=None) -> list[Event]
      weights defaults to scoring.weights.WEIGHTS. For each event:
        - composite = scoring.weights.composite(e.scores, weights)
        - tier      = scoring.weights.tier(e.scores, weights) if e.scores else "maybe"
                      (an event with NO scores is "maybe", never hidden)
        - if tier == "confident" and e.source in scoring.weights.NEVER_CONFIDENT_SOURCES:
              tier = "maybe"
      Drop events whose tier == "hidden".
      Per-day MAX_PICKS cap: within each start_dt.date(), keep only the
      MAX_PICKS highest-composite events as "confident"; demote the overflow to
      "maybe". (Re-applied here every call so it stays filter-sensitive.)
      Finally sort by (starts_at, -composite) and return the new list.
      Sets e.composite and e.tier in place; returns the surviving, sorted list.
"""
from __future__ import annotations

from app.models import Event
from scoring.weights import (MAX_PICKS, NEVER_CONFIDENT_SOURCES, WEIGHTS,
                             composite, tier)


def rank(events: list[Event], weights: dict | None = None) -> list[Event]:
    w = weights or WEIGHTS
    kept: list[Event] = []
    for e in events:
        e.composite = composite(e.scores, w)
        e.tier = tier(e.scores, w) if e.scores else "maybe"
        if e.tier == "confident" and e.source in NEVER_CONFIDENT_SOURCES:
            e.tier = "maybe"
        if e.tier != "hidden":
            kept.append(e)

    by_day: dict = {}
    for e in kept:
        if e.tier == "confident":
            by_day.setdefault(e.start_dt.date(), []).append(e)
    for day_picks in by_day.values():
        day_picks.sort(key=lambda e: -e.composite)
        for e in day_picks[MAX_PICKS:]:
            e.tier = "maybe"

    kept.sort(key=lambda e: (e.starts_at, -e.composite))
    return kept

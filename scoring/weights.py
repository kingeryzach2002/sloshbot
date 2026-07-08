"""Scorer weights and tier thresholds — the one knob-file for ranking.

The pipeline must work with any subset of scorers present (booze is modular):
weighted average is computed over whichever scores an event actually has.
"""

WEIGHTS = {
    "booze": 1.0,
}

# Event-type preference isn't a weight at all: it's the tag filter (settings
# key "included_tags"), a hard filter applied in load_events.

# Perk lenses: the user-facing "what are we hunting" scorers. A future perk
# scorer (food, live music, ...) becomes a lens by adding its scorer key here.
LENSES = ["booze"]

TIER_CONFIDENT = 0.65  # >= this -> "confident"
TIER_MAYBE = 0.40      # >= this -> "maybe"; below -> hidden

MAX_PICKS = 3  # hard cap on top-tier events per day — scarcity is the product

# Nightlife-listing sources (RA, 19hz) are structurally paid-cover / cash-bar
# club nights, not free-booze mixers — a high booze score there is much more
# likely to be a scorer false-positive than genuine sponsorship. Never let
# them compete for a "confident" slot; they can still surface as "maybe".
NEVER_CONFIDENT_SOURCES = {"ra", "19hz"}


def composite(scores: dict[str, float], weights: dict[str, float] | None = None) -> float:
    """Weighted average over the scorers that are present."""
    w = weights or WEIGHTS
    present = {k: v for k, v in scores.items() if k in w}
    if not present:
        return 0.0
    total_w = sum(w[k] for k in present)
    if total_w == 0:
        return 0.0
    return sum(w[k] * v for k, v in present.items()) / total_w


def tier(scores: dict[str, float], weights: dict[str, float] | None = None) -> str:
    c = composite(scores, weights)
    if c >= TIER_CONFIDENT:
        return "confident"
    if c >= TIER_MAYBE:
        return "maybe"
    return "hidden"

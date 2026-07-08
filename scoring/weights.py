"""Scorer weights and tier thresholds — the one knob-file for ranking.

The pipeline must work with any subset of scorers present (booze is modular):
weighted average is computed over whichever scores an event actually has.
"""

WEIGHTS = {
    "booze": 0.70,
    "logistics": 0.30,
}

# With two scorers there is exactly one honest knob: the booze↔logistics
# balance slider in the sidebar (settings key "weight_booze"; logistics gets
# the remainder). Named presets are gone — they were personality, not signal.
# Event-type preference isn't a weight at all: it's the tag filter (settings
# key "included_tags"), a hard filter applied in load_events.

# Perk lenses: the user-facing "what are we hunting" scorers. A future perk
# scorer (food, live music, ...) becomes a lens by adding its scorer key here.
LENSES = ["booze"]
LENS_BOOST = 0.60  # the active lens's share of the blend when ranking

TIER_CONFIDENT = 0.65  # >= this -> "confident"
TIER_MAYBE = 0.40      # >= this -> "maybe"; below -> hidden

MAX_PICKS = 3  # hard cap on top-tier events per loaded window — scarcity is the product


def lens_weights(lens: str, base: dict[str, float] | None = None) -> dict[str, float]:
    """Blend weights with the active lens boosted to LENS_BOOST; the rest of the
    base weights share what's left, proportionally."""
    base = base or WEIGHTS
    if lens not in base:
        return base
    others = {k: v for k, v in base.items() if k != lens}
    total = sum(others.values()) or 1.0
    w = {k: (1 - LENS_BOOST) * v / total for k, v in others.items()}
    w[lens] = LENS_BOOST
    return w


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

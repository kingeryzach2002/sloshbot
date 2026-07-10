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

# Per-lens display metadata — the single home for how a lens is rendered, so a
# new lens is a config entry here (+ its scorer) rather than copy-pasted markup.
# emoji_html is the HTML entity so templates can emit it verbatim with |safe.
LENS_META = {
    "booze": {"label": "booze", "emoji_html": "&#127864;"},  # 🍸
}

TIER_CONFIDENT = 0.65  # >= this -> "confident"
TIER_MAYBE = 0.10      # >= this -> "maybe"; below -> hidden. Deliberately
                       # permissive (was 0.40): with the catalog now fully
                       # scraped, 0.40 starved every view to ~a quarter of the
                       # week's events — surface nearly everything and let the
                       # confident tier + ranking carry the taste.

MAX_PICKS = 3  # hard cap on top-tier events per day — scarcity is the product

# Sources to cap at "maybe" — never let them win a "confident" slot even on a
# high booze score, because that score is more likely a scorer false-positive
# than genuine free-booze sponsorship. Empty now: the original members (the
# nightlife-listing sources RA and 19hz — structurally paid-cover / cash-bar
# club nights) were removed as ingest sources entirely (they surfaced nothing).
# Kept as a live mechanism: add any future demote-but-don't-drop source here.
NEVER_CONFIDENT_SOURCES: set[str] = set()


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

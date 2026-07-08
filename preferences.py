"""Single-user logistics preferences as code (v1). Multi-user later = move to a table.

These feed the logistics scorer. Tune freely — rescore with:
  uv run python -m scoring.run --rescore --scorer logistics

Event-type preference used to live here too (CATEGORY_KEYWORDS, a hand-tuned
keyword dict). That's gone: preferences change too often to hand-edit a Python
file for. Event-type preference is now a tag filter — pick the tags you want
from the checklist on the week view; it's stored in the `settings` table
(key "included_tags") and edited live, no code change or rescore needed.
"""

# Neighborhoods that are easy for you (logistics boost). Others are neutral.
PREFERRED_NEIGHBORHOODS = {
    "mission", "mission district", "soma", "south of market", "dogpatch",
    "hayes valley", "mid-market", "downtown", "financial district",
    "embarcadero", "castro", "duboce triangle", "potrero hill", "nopa",
}

# Ideal start-hour windows (24h) by day type.
WEEKDAY_SWEET_SPOT = (17, 20)   # after work
WEEKEND_SWEET_SPOT = (14, 21)   # afternoons into evening

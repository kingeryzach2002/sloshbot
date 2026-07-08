"""Logistics scorer — pure heuristic, no LLM. Time-of-day/day-of-week fit,
neighborhood convenience, RSVP friction. Distance-from-home joins this scorer
once home location is set in settings (UI feature in progress).
"""
from datetime import datetime

from preferences import PREFERRED_NEIGHBORHOODS, WEEKDAY_SWEET_SPOT, WEEKEND_SWEET_SPOT


def score(event: dict) -> dict:
    reasons = []
    s = 0.5

    dt = datetime.fromisoformat(event["starts_at"])
    weekend = dt.weekday() >= 5
    lo, hi = WEEKEND_SWEET_SPOT if weekend else WEEKDAY_SWEET_SPOT
    if lo <= dt.hour <= hi:
        s += 0.25
        reasons.append(f"+25 {'weekend' if weekend else 'weeknight'} sweet spot ({dt:%-I%p})")
    elif dt.hour < 12:
        s -= 0.25
        reasons.append(f"−25 morning start ({dt:%-I%p})")
    elif dt.hour >= 22:
        s -= 0.15
        reasons.append(f"−15 late start ({dt:%-I%p})")

    hood = (event.get("neighborhood") or "").lower()
    if hood in PREFERRED_NEIGHBORHOODS:
        s += 0.2
        reasons.append(f"+20 easy neighborhood ({event['neighborhood']})")

    if event.get("rsvp_type") in ("waitlist", "sold_out"):
        s -= 0.3
        reasons.append(f"−30 {event['rsvp_type']} — hard to get in")
    elif event.get("rsvp_type") in ("approval", "application"):
        s -= 0.1
        reasons.append("−10 gated RSVP")

    return {"score": max(0.0, min(1.0, s)),
            "rationale": "base 50, " + ", ".join(reasons) if reasons
                         else "base 50, no adjustments (time/place neutral)"}

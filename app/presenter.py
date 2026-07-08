"""Presenter: view-model adornment. Adds everything derived from base fields
that the views (HTML or JSON) render — parsed datetimes, distance from home,
the Google-Calendar hold link, and the calendar-grid geometry. Pure functions
over Event objects + settings; no DB, no scoring.

Replaces, from the old app/main.py:
  - home_coords (main.py:120-124), haversine_mi (127-131), gcal_link (134-150)
  - the per-event enrichment in load_events (main.py:200-201 distance,
    217-220 gcal + start_dt/end_dt)
  - day_label (242-249), group_by_day (252-256)
  - the calendar constants + layout_day (259-312)

CONTRACT
  enrich(events, settings) -> None
      Runs FIRST in the pipeline (filters/policy depend on its output). For each
      event, set IN PLACE:
        - start_dt = datetime.fromisoformat(starts_at)
        - end_dt   = datetime.fromisoformat(ends_at) if ends_at else start_dt + 2h
        - distance_mi = haversine miles from home to (lat,lon) if home coords are
          set (settings home_lat/home_lon) and lat is not None, else None
        - gcal = gcal_link(event)
      The 2h end-time default MUST match gcal_link's default exactly.

  Also expose (moved verbatim, behavior-identical):
    home_coords(settings) -> tuple[float,float] | None   # parse home_lat/home_lon
    haversine_mi(lat1, lon1, lat2, lon2) -> float
    gcal_link(e) -> str                                   # calendar TEMPLATE link
    day_label(d) -> str                                   # "Tonight"/"Tomorrow"/"%A · %b %-d"
    group_by_day(events) -> list[tuple[str, list[Event]]]
    layout_day(events) -> list[dict]                      # calendar blocks w/ top/height/left/width/lane/n_lanes
    CAL_START_MIN, CAL_END_MIN, CAL_SPAN, CAL_MAX_PER_DAY constants
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlencode

from app.models import Event

# Calendar grid runs 8am-midnight; earlier/later events clamp to the edges.
CAL_START_MIN = 8 * 60
CAL_END_MIN = 24 * 60
CAL_SPAN = CAL_END_MIN - CAL_START_MIN
CAL_MAX_PER_DAY = 8


def home_coords(settings: dict) -> tuple[float, float] | None:
    try:
        return float(settings["home_lat"]), float(settings["home_lon"])
    except (KeyError, ValueError):
        return None


def haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def gcal_link(e: Event) -> str:
    """Pre-filled Google Calendar template link — a tentative hold, no OAuth."""
    def gfmt(iso: str) -> str:
        return datetime.fromisoformat(iso).strftime("%Y%m%dT%H%M%S")

    start = gfmt(e["starts_at"])
    end = gfmt(e["ends_at"]) if e["ends_at"] else gfmt(
        (datetime.fromisoformat(e["starts_at"]) + timedelta(hours=2)).isoformat())
    params = {
        "action": "TEMPLATE",
        "text": f"[hold] {e['title']}",
        "dates": f"{start}/{end}",
        "location": ", ".join(filter(None, [e["venue_name"], e["address"]])),
        "details": f"{e['url']}\n\nAdded by sloshbot (tentative hold)",
        "crm": "TENTATIVE",
    }
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def enrich(events: list[Event], settings: dict) -> None:
    home = home_coords(settings)
    for e in events:
        e.start_dt = datetime.fromisoformat(e.starts_at)
        e.end_dt = (datetime.fromisoformat(e.ends_at) if e.ends_at
                    else e.start_dt + timedelta(hours=2))  # same default as gcal_link
        e.distance_mi = (haversine_mi(home[0], home[1], e.lat, e.lon)
                          if home and e.lat is not None else None)
        e.gcal = gcal_link(e)


def day_label(d) -> str:
    """Relative day names — time reads as distance from now, not a datebook."""
    today = datetime.now().date()
    if d == today:
        return "Tonight"
    if d == today + timedelta(days=1):
        return "Tomorrow"
    return d.strftime("%A · %b %-d")


def group_by_day(events: list[Event]) -> list[tuple[str, list[Event]]]:
    days: dict[str, list] = defaultdict(list)
    for e in events:
        days[day_label(e.start_dt.date())].append(e)
    return list(days.items())


def layout_day(events: list[Event]) -> list[dict]:
    """Position events in a day column: top/height as % of the grid, and a
    lane index so overlapping events sit side by side (Google Calendar style)."""
    blocks = []
    for e in events:
        s = e.start_dt.hour * 60 + e.start_dt.minute
        dur = int((e.end_dt - e.start_dt).total_seconds() // 60)
        end = min(s + max(dur, 30), CAL_END_MIN)  # 30min floor keeps blocks tappable
        s = max(s, CAL_START_MIN)
        if end <= s:
            continue
        blocks.append({"e": e, "s": s, "end": end})
    blocks.sort(key=lambda b: (b["s"], -b["end"]))

    # Greedy lane assignment within clusters of transitively-overlapping events.
    cluster: list[dict] = []
    cluster_end = 0

    def flush() -> None:
        lanes: list[int] = []  # occupied-until minute per lane
        for b in cluster:
            for i, lane_end in enumerate(lanes):
                if lane_end <= b["s"]:
                    lanes[i] = b["end"]
                    b["lane"] = i
                    break
            else:
                b["lane"] = len(lanes)
                lanes.append(b["end"])
        for b in cluster:
            b["n_lanes"] = len(lanes)

    for b in blocks:
        if cluster and b["s"] >= cluster_end:
            flush()
            cluster = []
        cluster.append(b)
        cluster_end = max(cluster_end, b["end"]) if len(cluster) > 1 else b["end"]
    if cluster:
        flush()

    for b in blocks:
        b["top"] = (b["s"] - CAL_START_MIN) / CAL_SPAN * 100
        b["height"] = (b["end"] - b["s"]) / CAL_SPAN * 100
        b["left"] = b["lane"] / b["n_lanes"] * 100
        b["width"] = 100 / b["n_lanes"]
    return blocks

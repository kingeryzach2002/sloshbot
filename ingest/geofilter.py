"""Geographic gate: drop events that are provably outside the Bay Area.

Every source scraper is *supposed* to be pre-scoped to SF (via a place_api_id,
a `?region=sf` query param, an SF-only listing URL, etc.), but some feeds leak
— Luma's SF "discover place" feed has been observed returning events as far
away as Vancouver. Nothing downstream caught those: the app's per-user distance
filter (app.presenter.haversine_mi + FilterState.max_mi) is display-time and
only applies once a visitor sets a home address, so leaked events still sat in
the shared catalog.

This step is the source-agnostic backstop: after geocode has resolved
coordinates, delete any event whose (lat, lon) is more than RADIUS_MI from a
central SF point. It reuses the same haversine math as the app layer.

Policy (decided with the owner):
  - Reject only when we can PROVE distance: an event must HAVE coordinates AND
    be outside the radius. Events with NULL lat/lon are KEPT — they came from
    already-SF-scoped sources and may just have failed geocoding; dropping them
    on a geocoder hiccup would lose legitimate SF events.
  - Radius is centered on the Mission (where the owner actually lives/goes
    out), tuned as a coarse backstop against gross leaks rather than fine
    curation. See RADIUS_MI below — note it now excludes Berkeley (~9.5mi from
    the Mission), a known/accepted tradeoff, not an oversight; see the comment
    on MISSION_CENTER_LAT/LON below.
  - Never delete an event that has feedback or a hold (same guarantee as
    pipeline.prune_old_events): that's the crowdsourced host-reputation signal
    scoring depends on. In practice a far-away leak never has feedback, so this
    guard costs nothing and keeps the invariant simple.

Usage: uv run python -m ingest.geofilter
"""
import math

from db import get_conn, init_db

# Mission district center (the owner's actual anchor point, not a generic
# "downtown SF" centroid). The radius is a backstop against gross leaks
# (Vancouver, LA, NYC), not precise curation, but per the owner it should now
# be tight enough to meaningfully shrink the East Bay catchment: 8mi from the
# Mission covers all of SF proper; Oakland is now a mixed bag — West Oakland/
# Jack London Square (~7.3-7.9mi) stay in, but City Hall/downtown/Lake Merritt
# (~8.2-9.1mi) fall outside — and Berkeley is OUT entirely (~9.5-11.5mi across
# downtown/campus/West Berkeley, all past the cutoff). That's a known,
# owner-accepted tradeoff (told explicitly), not a bug: Berkeley events (and
# now much of Oakland) will be dropped by this filter until the radius is
# widened again, which is one constant to change here if that's ever
# revisited.
MISSION_CENTER_LAT = 37.7599
MISSION_CENTER_LON = -122.4148
RADIUS_MI = 8.0


def _haversine_mi(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in miles. Mirrors app.presenter.haversine_mi
    exactly (kept local to avoid an ingest -> app import dependency)."""
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(a))


def cull_far_events() -> int:
    """Delete events with coordinates farther than RADIUS_MI from SF center and
    with no feedback/hold. Returns the number of events removed."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT e.id, e.lat, e.lon FROM events e
               WHERE e.lat IS NOT NULL AND e.lon IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.event_id = e.id)
                 AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.event_id = e.id)"""
        ).fetchall()
        far = [r["id"] for r in rows
               if _haversine_mi(MISSION_CENTER_LAT, MISSION_CENTER_LON, r["lat"], r["lon"]) > RADIUS_MI]
        if not far:
            return 0
        placeholders = ",".join("?" * len(far))
        conn.execute(f"DELETE FROM scores WHERE event_id IN ({placeholders})", far)
        conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({placeholders})", far)
        conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", far)
        conn.commit()
    return len(far)


def main() -> int:
    init_db()
    removed = cull_far_events()
    print(f"geofilter: removed {removed} event(s) outside "
          f"{RADIUS_MI:.0f}mi of the Mission")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

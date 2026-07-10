"""Backfill lat/lon for events that lack them, via OpenStreetMap Nominatim.

Free service, hard limit 1 req/sec — results are cached in a table so each
distinct address is only ever geocoded once. Also geocodes the home address
from settings (home_lat/home_lon) if set but not yet resolved.

Usage: uv run python -m ingest.geocode
"""
import time

import requests

from db import get_conn, init_db

NOMINATIM = "https://nominatim.openstreetmap.org/search"
HEADERS = {"User-Agent": "sloshbot/0.1 (personal event aggregator)"}
PACING_SECONDS = 1.1

# Bay Area bounding box used to bias/validate geocoding of BARE addresses (no
# stated city) — a lone street address like "1148 masonic ave" typed into the
# home-address box, or an event venue string like "901 Market St". Nominatim's
# `viewbox` param wants "left,top,right,bottom" i.e. west-lon,north-lat,
# east-lon,south-lat. Deliberately loose (SF + the inner East Bay, plus
# slack) — its only job is disambiguating which "Masonic Ave" or "Broadway"
# the caller meant, not curating; ingest/geofilter.py does the tighter
# *event-catchment* radius check after coordinates are known.
BAY_AREA_VIEWBOX = "-122.75,38.0,-121.9,37.2"
BAY_LAT_MIN, BAY_LAT_MAX = 37.2, 38.0
BAY_LON_MIN, BAY_LON_MAX = -122.75, -121.9

CACHE_DDL = """CREATE TABLE IF NOT EXISTS geocode_cache (
  query TEXT PRIMARY KEY, lat REAL, lon REAL
)"""
_CACHE_DDL = CACHE_DDL


def geocode(conn, address: str) -> tuple[float, float] | tuple[None, None]:
    """Public entry point for callers outside the pipeline (e.g. the app).

    Ensures the cache table exists, then delegates to the existing lookup
    logic. Behaves identically to the old inline `conn.execute(_CACHE_DDL)`
    + `_lookup(conn, address)` pattern. This is also THE reason the home-
    address save path (app.main._save_home) and the pipeline's event/home
    backfill (this module's main()) can't drift: both call into `_lookup`
    (directly or via this wrapper), so a fix here — like the Bay Area bias
    below — benefits every caller identically, not just events.
    """
    conn.execute(CACHE_DDL)
    return _lookup(conn, address)


def _in_bay_area(lat: float | None, lon: float | None) -> bool:
    return (lat is not None and lon is not None
            and BAY_LAT_MIN <= lat <= BAY_LAT_MAX and BAY_LON_MIN <= lon <= BAY_LON_MAX)


def _has_locality(addr: str) -> bool:
    """True if `addr` already states where it is — a comma-separated
    city/state/country, or it names SF/California outright — in which case we
    trust it as written instead of biasing it toward the Bay Area.

    The old test here was a bare `"ca" in addr` substring, which matched
    "Canada", "Chicago", "Vatican", etc., so those skipped the nudge (fine) —
    but the naive inverse (nudge whenever SF/CA absent) would tack ", San
    Francisco, CA" onto "Vancouver, BC, Canada", mangling a real foreign
    address toward SF and hiding it from the geofilter step. Keying off "has
    a comma-delimited locality" avoids both failure modes: bare SF/Bay Area
    venues and street addresses still get biased, while any address that
    already states where it is geocodes truthfully so ingest.geofilter can
    reject it if it's out of the Bay Area.
    """
    low = addr.lower()
    return ("," in addr) or ("san francisco" in low) or ("california" in low)


def _nominatim_search(query: str, *, bounded: bool = False) -> tuple[float, float] | tuple[None, None]:
    """One paced call to Nominatim. `bounded=True` restricts results to
    BAY_AREA_VIEWBOX (`viewbox` + `bounded=1`); otherwise it's a plain
    unbounded search, trusting Nominatim's own ranking. Every call sleeps
    PACING_SECONDS afterward regardless of outcome, so the service's 1
    req/sec usage policy holds even across a multi-rung fallback chain (see
    `_bay_area_lookup`) — each rung is its own paced request.
    """
    params = {"q": query, "format": "json", "limit": 1}
    if bounded:
        params["viewbox"] = BAY_AREA_VIEWBOX
        params["bounded"] = 1
    resp = requests.get(NOMINATIM, params=params, headers=HEADERS, timeout=15)
    time.sleep(PACING_SECONDS)
    hit = resp.json()[0] if resp.ok and resp.json() else None
    if not hit:
        return None, None
    return float(hit["lat"]), float(hit["lon"])


def _bay_area_lookup(query: str) -> tuple[float, float] | tuple[None, None]:
    """Resolve a BARE address/venue string (no stated city) with a Bay Area
    preference — that's what the overwhelming majority of our bare inputs
    actually are: SF event venues, or a visitor typing their home street with
    no city ("1148 masonic ave").

    Root cause this exists to fix: an unbounded, un-nudged Nominatim search
    for a bare street address is at the mercy of Nominatim's global ranking
    (and reportedly some IP-geolocation biasing), which is fine when queried
    from a Bay-Area-resident machine but silently wrong — or just ambiguous
    for common street names — when the same query runs from prod's cloud
    host, which is where app.main._save_home actually executes. Forcing a
    Bay Area viewbox removes that non-determinism instead of hoping the
    caller's IP happens to be nearby.

    Ladder, each rung sanity-checked against the Bay Area box before being
    trusted (bounded=1 usually enforces this Nominatim-side too, but we don't
    rely on that being airtight — belt and suspenders):
      1. as typed, bounded to BAY_AREA_VIEWBOX — resolves the vast majority
         of cases outright, including disambiguating a street name that's
         unique within the box (e.g. Oakland's "2201 Broadway" resolves here
         with no city ever appended, since SF has no address at that number).
      2-4. as typed + ", San Francisco, CA" / ", Oakland, CA" / ", Berkeley,
         CA", each still bounded — covers the case where the bare street
         genuinely doesn't disambiguate to a unique in-box match on its own.
      5. as typed, fully unbounded, last resort — better a real coordinate
         (which ingest.geofilter can catch downstream if it's actually out of
         region) than leaving the address unresolved forever.
    """
    lat, lon = _nominatim_search(query, bounded=True)
    if _in_bay_area(lat, lon):
        return lat, lon
    for city in ("San Francisco, CA", "Oakland, CA", "Berkeley, CA"):
        lat, lon = _nominatim_search(f"{query}, {city}", bounded=True)
        if _in_bay_area(lat, lon):
            return lat, lon
    return _nominatim_search(query)


def _lookup(conn, query: str) -> tuple[float, float] | tuple[None, None]:
    """Shared by every geocode caller — pipeline event backfill, home-address
    save (app.main._save_home), and this module's home-address fallback loop
    in main(). This is the ONE place a raw address string turns into
    coordinates, so a fix here (the Bay Area bias) benefits all of them
    identically instead of only the pipeline's event path.

    Cache: keyed on the raw query string, as before. A row with a non-NULL
    lat is trusted and returned without re-querying. A row that exists with a
    NULL lat is treated as NO cache — we don't write those anymore (see
    below), but an old DB may still carry pre-fix NULL rows, and honoring
    them as "permanently unresolved" would just re-poison a query this fixed
    logic could now resolve.

    We used to cache misses too ("so we don't retry dead addresses"), but
    that policy is exactly what let a single bad geocode become permanent:
    one Nominatim hiccup — a timeout, a rate-limit response, or (the actual
    home-address bug this fix addresses) a bare address landing on the wrong
    city because the query ran unbounded from prod's cloud-host IP instead of
    a Bay-Area one — got cached forever, since the cache was checked before
    any retry ever happened. Now a failed lookup is simply retried the next
    time it's asked for. That does mean a genuinely dead address gets re-hit
    on every pipeline run, but that set is bounded (only future events still
    missing coordinates, or a home address that's never resolved) and one
    Nominatim call is cheap next to a permanently broken geocode.
    """
    row = conn.execute("SELECT lat, lon FROM geocode_cache WHERE query = ?", (query,)).fetchone()
    if row and row["lat"] is not None:
        return row["lat"], row["lon"]

    if _has_locality(query):
        # Address already states where it is — trust it as written, unbounded,
        # so a genuinely out-of-area address (e.g. "Vancouver, BC, Canada")
        # geocodes truthfully and ingest.geofilter can reject it downstream.
        lat, lon = _nominatim_search(query)
    else:
        lat, lon = _bay_area_lookup(query)

    if lat is not None:
        conn.execute("INSERT OR REPLACE INTO geocode_cache (query, lat, lon) VALUES (?,?,?)",
                     (query, lat, lon))
    return lat, lon


def _query_for(event) -> str | None:
    """The raw address string to geocode for an event: address if present,
    else the venue name. No locality nudging happens here anymore — that
    responsibility moved into `_lookup` (`_has_locality` + `_bay_area_lookup`)
    so it's applied identically whether the caller is this pipeline step or
    app.main._save_home's home-address path.
    """
    return event["address"] or event["venue_name"]


def main():
    init_db()
    with get_conn() as conn:
        conn.execute(_CACHE_DDL)
        todo = conn.execute(
            """SELECT id, address, venue_name FROM events
               WHERE lat IS NULL
                 AND starts_at >= datetime('now', 'localtime')""").fetchall()
        print(f"{len(todo)} future events missing coordinates")
        done = failed = 0
        # Queries that already failed THIS run. Misses aren't cached in
        # geocode_cache (deliberately — see _lookup), but many events share
        # one junk venue string ("Various locations", "TBA"), and the fallback
        # ladder spends up to 5 paced requests per attempt — without this
        # memo, 20 events sharing an unresolvable venue would burn ~2 minutes
        # re-failing the same lookup 20 times in one run.
        run_misses: set[str] = set()
        for e in todo:
            q = _query_for(e)
            if not q or q in run_misses:
                failed += 1
                continue
            lat, lon = _lookup(conn, q)
            if lat is not None:
                conn.execute("UPDATE events SET lat=?, lon=? WHERE id=?", (lat, lon, e["id"]))
                done += 1
            else:
                run_misses.add(q)
                failed += 1
            # Commit as we go, not once at the end: at ~1.1s/lookup a big
            # backlog keeps this transaction open for many minutes, and a
            # kill/crash/reboot mid-run used to roll back EVERY resolved
            # coordinate and cache row with it. Each lookup is independent
            # and idempotent, so there's nothing a partial commit can corrupt.
            conn.commit()

        # every user's home address (written by the UI) -> home_lat/home_lon.
        # Normally set_home() geocodes inline at save time; this is the
        # fallback for addresses that failed then or predate that.
        homes = conn.execute(
            "SELECT user_id, value FROM settings WHERE key = 'home_address'").fetchall()
        for h in homes:
            lat, lon = _lookup(conn, h["value"])
            if lat is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'home_lat', ?)",
                    (h["user_id"], str(lat)))
                conn.execute(
                    "INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, 'home_lon', ?)",
                    (h["user_id"], str(lon)))
                print(f"home address geocoded for {h['user_id']}: {lat:.5f}, {lon:.5f}")

    print(f"geocoded {done}, unresolved {failed}")


if __name__ == "__main__":
    main()

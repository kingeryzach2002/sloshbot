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

CACHE_DDL = """CREATE TABLE IF NOT EXISTS geocode_cache (
  query TEXT PRIMARY KEY, lat REAL, lon REAL
)"""
_CACHE_DDL = CACHE_DDL


def geocode(conn, address: str) -> tuple[float, float] | tuple[None, None]:
    """Public entry point for callers outside the pipeline (e.g. the app).

    Ensures the cache table exists, then delegates to the existing lookup
    logic. Behaves identically to the old inline `conn.execute(_CACHE_DDL)`
    + `_lookup(conn, address)` pattern.
    """
    conn.execute(CACHE_DDL)
    return _lookup(conn, address)


def _lookup(conn, query: str) -> tuple[float, float] | tuple[None, None]:
    row = conn.execute("SELECT lat, lon FROM geocode_cache WHERE query = ?", (query,)).fetchone()
    if row:
        return row["lat"], row["lon"]
    resp = requests.get(NOMINATIM, params={"q": query, "format": "json", "limit": 1},
                        headers=HEADERS, timeout=15)
    time.sleep(PACING_SECONDS)
    hit = resp.json()[0] if resp.ok and resp.json() else None
    lat = float(hit["lat"]) if hit else None
    lon = float(hit["lon"]) if hit else None
    conn.execute("INSERT OR REPLACE INTO geocode_cache (query, lat, lon) VALUES (?,?,?)",
                 (query, lat, lon))  # cache misses too, so we don't retry dead addresses
    return lat, lon


def _query_for(event) -> str | None:
    addr = event["address"] or event["venue_name"]
    if not addr:
        return None
    if "san francisco" not in addr.lower() and "ca" not in addr.lower():
        addr += ", San Francisco, CA"
    return addr


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
        for e in todo:
            q = _query_for(e)
            if not q:
                failed += 1
                continue
            lat, lon = _lookup(conn, q)
            if lat is not None:
                conn.execute("UPDATE events SET lat=?, lon=? WHERE id=?", (lat, lon, e["id"]))
                done += 1
            else:
                failed += 1

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

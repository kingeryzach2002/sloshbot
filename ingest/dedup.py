"""Cross-source dedup: collapse the same real-world event scraped from several
sources into one canonical row.

Sets events.duplicate_of = <canonical id> on the non-canonical copies (NULL on
the canonical one). The app shows only rows where duplicate_of IS NULL. Raw rows
are never deleted — dedup is fully recomputed each run (idempotent).

Two match tiers:
  1. URL identity (strong). Our sources cross-reference each other:
       - GarysGuide's resolved register link (stored in host_url) is usually a
         Luma/Eventbrite event URL → matches that native event.
       - 19hz's outbound link (its url) is often ra.co/events/<id> → matches RA.
     Any two events sharing a normalized identity key (same day) are the same.
  2. Fuzzy (title + date + venue/coords) for copies that don't cross-link.

Canonical = the copy from the highest-priority (richest) source in the cluster.

Usage: uv run python -m ingest.dedup
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime

from db import get_conn

# Richest sources first — the canonical copy is drawn from the earliest match.
# Luma/Meetup/Eventbrite carry host + rsvp + rich text; club sources are thin.
SOURCE_PRIORITY = ["luma", "meetup", "eventbrite", "dothebay", "garysguide",
                   "ra", "funcheap", "19hz"]

_TITLE_STOP = {"the", "a", "an", "sf", "san", "francisco", "bay", "area", "2026",
               "2025", "with", "and", "at", "of", "for", "presents", "night"}


# ---- URL identity -----------------------------------------------------------

def _url_key(url: str | None) -> str | None:
    """Normalize a URL to a cross-source identity key, or None if not usable."""
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u).split("?")[0].split("#")[0].rstrip("/")
    u = re.sub(r"^www\.", "", u)
    if not u:
        return None
    # Platform-specific: collapse to a stable per-event token.
    m = re.match(r"(?:lu\.ma|luma\.com)/([\w-]+)", u)
    if m:
        return f"luma:{m.group(1)}"
    m = re.match(r"ra\.co/events/(\d+)", u)
    if m:
        return f"ra:{m.group(1)}"
    m = re.match(r"meetup\.com/[\w-]+/events/(\d+)", u)
    if m:
        return f"meetup:{m.group(1)}"
    m = re.search(r"eventbrite\.\w+/e/[\w-]*?(\d{6,})", u)
    if m:
        return f"eventbrite:{m.group(1)}"
    return u  # generic: host+path, exact-match only


def _identity_keys(e: dict) -> set[str]:
    """Identity-bearing URL keys for an event. An event's own url always counts;
    GarysGuide additionally stores the resolved EVENT url in host_url (safe to
    treat as identity). Other sources' host_url is a host page, NOT identity —
    using it would wrongly merge every event by the same host, so we don't."""
    keys = {_url_key(e["url"])}
    if e["source"] == "garysguide":
        keys.add(_url_key(e["host_url"]))
    return {k for k in keys if k}


# ---- fuzzy match ------------------------------------------------------------

def _title_tokens(title: str | None) -> frozenset[str]:
    toks = re.sub(r"[^a-z0-9 ]", " ", (title or "").lower()).split()
    return frozenset(t for t in toks if t not in _TITLE_STOP and len(t) > 1)


def _title_sim(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter and (a <= b or b <= a) and min(len(a), len(b)) >= 2:
        return 1.0  # one title's tokens fully contain the other's
    return inter / len(a | b)


def _norm_venue(v: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (v or "").lower())


def _haversine_mi(lat1, lon1, lat2, lon2) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    h = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _fuzzy_same(e1: dict, e2: dict) -> bool:
    sim = _title_sim(e1["_toks"], e2["_toks"])
    if sim < 0.6:
        return False
    if sim >= 0.85:
        return True  # near-identical title on the same day is enough
    v1, v2 = _norm_venue(e1["venue_name"]), _norm_venue(e2["venue_name"])
    if v1 and v2 and (v1 == v2 or v1 in v2 or v2 in v1):
        return True
    if None not in (e1["lat"], e1["lon"], e2["lat"], e2["lon"]):
        return _haversine_mi(e1["lat"], e1["lon"], e2["lat"], e2["lon"]) < 0.2
    return False


# ---- union-find -------------------------------------------------------------

class _DSU:
    def __init__(self):
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def _canonical(cluster: list[dict]) -> dict:
    def rank(e: dict) -> tuple:
        pri = SOURCE_PRIORITY.index(e["source"]) if e["source"] in SOURCE_PRIORITY else 99
        return (pri, -len(e["description"] or ""), e["id"])  # richest source, longest desc, stable
    return min(cluster, key=rank)


def dedup() -> dict:
    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, source, url, host_url, title, starts_at, venue_name, "
            "lat, lon, description FROM events")]

    for e in rows:
        e["_toks"] = _title_tokens(e["title"])
        e["_date"] = e["starts_at"][:10]  # YYYY-MM-DD in stored Pacific ISO

    dsu = _DSU()
    for e in rows:
        dsu.find(e["id"])

    by_date: dict[str, list[dict]] = defaultdict(list)
    for e in rows:
        by_date[e["_date"]].append(e)

    # Tier 1: URL identity within a day.
    for day_events in by_date.values():
        key_owner: dict[str, str] = {}
        for e in day_events:
            for k in _identity_keys(e):
                if k in key_owner:
                    dsu.union(e["id"], key_owner[k])
                else:
                    key_owner[k] = e["id"]

    # Tier 2: fuzzy title+venue/coords within a day.
    for day_events in by_date.values():
        for i in range(len(day_events)):
            for j in range(i + 1, len(day_events)):
                if dsu.find(day_events[i]["id"]) == dsu.find(day_events[j]["id"]):
                    continue
                if _fuzzy_same(day_events[i], day_events[j]):
                    dsu.union(day_events[i]["id"], day_events[j]["id"])

    clusters: dict[str, list[dict]] = defaultdict(list)
    for e in rows:
        clusters[dsu.find(e["id"])].append(e)

    updates: list[tuple[str | None, str]] = []
    dup_pairs = defaultdict(int)
    n_dup = n_clusters = 0
    for members in clusters.values():
        canon = _canonical(members)
        if len(members) > 1:
            n_clusters += 1
        for e in members:
            dupe_of = None if e["id"] == canon["id"] else canon["id"]
            updates.append((dupe_of, e["id"]))
            if dupe_of:
                n_dup += 1
                dup_pairs[tuple(sorted((e["source"], canon["source"])))] += 1

    with get_conn() as conn:
        conn.executemany("UPDATE events SET duplicate_of = ? WHERE id = ?", updates)

    return {"events": len(rows), "clusters_with_dupes": n_clusters,
            "duplicates_marked": n_dup, "by_source_pair": dict(dup_pairs)}


if __name__ == "__main__":
    stats = dedup()
    print(f"events: {stats['events']}")
    print(f"merged clusters: {stats['clusters_with_dupes']}")
    print(f"duplicates marked: {stats['duplicates_marked']}")
    if stats["by_source_pair"]:
        print("by source pair:")
        for pair, n in sorted(stats["by_source_pair"].items(), key=lambda x: -x[1]):
            print(f"  {pair[0]} ↔ {pair[1]}: {n}")

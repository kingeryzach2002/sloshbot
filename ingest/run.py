"""Ingest CLI.

Usage:
    uv run python -m ingest.run [--source luma|funcheap|eventbrite] [--limit N]

Default (no --limit): each source pulls its own per-source default from
SOURCE_LIMITS below, sized to cover that source's date window (most walk
~10-14 days ahead and stop when the window is exhausted, so the limit only
needs to be high enough NOT to truncate the window early). Luma is the
outlier: its discover feed is nearest-first with no date filter, so covering
~10 days takes ~450 entries (see ingest/sources/luma.py) — a low limit there
silently yields only "today and tomorrow," which is exactly what starved the
live catalog down to a handful of events.

Passing --limit N explicitly overrides every source with N (use for manual
single-source spikes). One source failing does not kill the rest.
"""
from __future__ import annotations

import argparse
import importlib
import pkgutil
import sys
import traceback

from db import init_db
from ingest.normalize import upsert
import ingest.sources

# Auto-discover source modules: any ingest/sources/<name>.py exposing fetch().
# New sources just drop in a module — no edit here needed (avoids merge conflicts
# when several source modules are added in parallel).
SOURCES = {}
for _finder, _name, _ in pkgutil.iter_modules(ingest.sources.__path__):
    if _name.startswith("_"):
        continue
    try:  # a broken/half-written module must not block the other sources
        _mod = importlib.import_module(f"ingest.sources.{_name}")
    except Exception as _exc:
        print(f"[skip] ingest.sources.{_name} failed to import: {_exc}", file=sys.stderr)
        continue
    if hasattr(_mod, "fetch"):
        SOURCES[_name] = _mod.fetch

# Generous fallback for any source without an explicit entry below (including a
# newly dropped-in module) — high enough to fill a ~2-week window, low enough
# to stay cheap. New sources need no edit here; they inherit this.
DEFAULT_LIMIT = 80

# Per-source ceilings, sized to each source's coverage window (see the
# DAYS_AHEAD/WINDOW_DAYS/COVERAGE_DAYS constants in each module). These are
# ceilings, not targets: a source returns fewer if its window holds fewer, so
# over-provisioning here is cheap. One deliberate exception to "just go big":
#   - luma: nearest-first, no date filter, ~48/page — needs ~450 to reach ~10
#     days out (the root cause of the starved live catalog).
SOURCE_LIMITS = {
    "luma": 500,
    "funcheap": 250,
    "dothebay": 200,
    "eventbrite": 150,
    "garysguide": 60,
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.run")
    parser.add_argument("--source", choices=sorted(SOURCES), default=None,
                        help="ingest a single source (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="override max events for EVERY source with this value "
                             "(default: each source's own limit from SOURCE_LIMITS)")
    args = parser.parse_args()

    init_db()  # idempotent (CREATE TABLE IF NOT EXISTS)

    names = [args.source] if args.source else sorted(SOURCES)
    failures = 0
    for name in names:
        limit = args.limit if args.limit is not None else SOURCE_LIMITS.get(name, DEFAULT_LIMIT)
        try:
            rows = SOURCES[name](limit=limit)
            counts = upsert(rows)
            print(f"{name}: limit {limit}, fetched {len(rows)}, "
                  f"inserted {counts['inserted']}, updated {counts['updated']}, "
                  f"skipped {counts['skipped']}")
        except Exception as exc:  # one source failing must not kill the others
            failures += 1
            print(f"{name}: FAILED — {exc}", file=sys.stderr)
            traceback.print_exc()
    return 1 if failures == len(names) else 0


if __name__ == "__main__":
    sys.exit(main())

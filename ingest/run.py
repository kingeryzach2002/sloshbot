"""Ingest CLI.

Usage:
    uv run python -m ingest.run [--source luma|funcheap|eventbrite] [--limit N]

Default: all sources, limit 30 each. One source failing does not kill the rest.
"""
from __future__ import annotations

import argparse
import sys
import traceback

from db import init_db
from ingest.normalize import upsert
from ingest.sources import eventbrite, funcheap, luma

SOURCES = {
    "luma": luma.fetch,
    "funcheap": funcheap.fetch,
    "eventbrite": eventbrite.fetch,
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.run")
    parser.add_argument("--source", choices=sorted(SOURCES), default=None,
                        help="ingest a single source (default: all)")
    parser.add_argument("--limit", type=int, default=30,
                        help="max events per source (default: 30)")
    args = parser.parse_args()

    init_db()  # idempotent (CREATE TABLE IF NOT EXISTS)

    names = [args.source] if args.source else sorted(SOURCES)
    failures = 0
    for name in names:
        try:
            rows = SOURCES[name](limit=args.limit)
            counts = upsert(rows)
            print(f"{name}: fetched {len(rows)}, "
                  f"inserted {counts['inserted']}, updated {counts['updated']}, "
                  f"skipped {counts['skipped']}")
        except Exception as exc:  # one source failing must not kill the others
            failures += 1
            print(f"{name}: FAILED — {exc}", file=sys.stderr)
            traceback.print_exc()
    return 1 if failures == len(names) else 0


if __name__ == "__main__":
    sys.exit(main())

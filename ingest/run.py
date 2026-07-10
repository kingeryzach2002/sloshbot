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

Sources run concurrently (each is an independent, I/O-bound domain fetch —
see main()), but every DB write happens on the main thread so sqlite only
ever sees one writer.

Run report: every run appends one JSON line per source to a report file (see
REPORT_PATH below) — {"ts", "source", "status", "limit", "fetched",
"inserted", "updated", "skipped", "duration_s", "error"?, "traceback"?}. This
lets an agent (or human) reviewing a bad run read the last N lines, see
exactly which source failed and why, and re-run just that one with
`uv run python -m ingest.run --source <name>` — its outcome lands in the same
report, right next to the failure it's fixing.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import db
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

# Where the per-source JSON-lines run report is written. SLOSHBOT_INGEST_REPORT
# overrides explicitly (e.g. a host with a nonstandard writable path);
# otherwise it lives beside the DB file, since that's the one location every
# host shape (local dev, OpenHost persistent mount, etc — see db.py) is
# guaranteed to have already proven is writable and persistent.
def _report_path() -> Path:
    override = os.environ.get("SLOSHBOT_INGEST_REPORT")
    if override:
        return Path(override)
    return db.DB_PATH.parent / "ingest-report.jsonl"


# Cheap cap so the report never grows unbounded on a long-running host — no
# logrotate dependency, just keep the newest slice on open. Checked/trimmed
# once per process invocation (not per line) since ingest.run is a short-lived
# CLI, not a long-running writer.
REPORT_MAX_LINES = 2000
REPORT_KEEP_LINES = 1000


def _trim_report(path: Path) -> None:
    """If the report has grown past REPORT_MAX_LINES, truncate it down to the
    newest REPORT_KEEP_LINES. Best-effort: a failure here must not block
    ingest itself, so any error is swallowed."""
    try:
        if not path.exists():
            return
        lines = path.read_text().splitlines()
        if len(lines) <= REPORT_MAX_LINES:
            return
        kept = lines[-REPORT_KEEP_LINES:]
        path.write_text("\n".join(kept) + "\n")
    except OSError:
        pass  # report hygiene is best-effort; never let it break a run


def _append_report(path: Path, entry: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:  # reporting must never take down the ingest run
        print(f"[warn] failed to write ingest report: {exc}", file=sys.stderr)


def _run_source(name: str, limit: int) -> tuple[str, int, list[dict] | None, Exception | None, str | None]:
    """Fetch one source. Runs on a worker thread — no DB access here, so the
    only thing crossing back to the main thread is data (rows or an
    exception), keeping sqlite single-writer.

    Fetching within a source stays sequential (see each source module's own
    pacing, e.g. garysguide's PACING_SECONDS) — that's deliberate politeness
    toward that source's domain, not a bottleneck we want to parallelize
    away. Only the across-source fan-out below is concurrent."""
    try:
        rows = SOURCES[name](limit=limit)
        return name, limit, rows, None, None
    except Exception as exc:  # one source failing must not kill the others
        return name, limit, None, exc, traceback.format_exc()


def _process_result(name: str, limit: int, rows: list[dict] | None, exc: Exception | None,
                     tb: str | None, duration_s: float, report_path: Path) -> bool:
    """Upsert (if successful) and append the report line. Returns True on
    success. Must be called from the main thread — this is where the single
    sqlite writer lives."""
    ts = datetime.now(timezone.utc).isoformat()
    if exc is not None:
        print(f"{name}: FAILED — {exc}", file=sys.stderr)
        print(tb, file=sys.stderr)
        _append_report(report_path, {
            "ts": ts, "source": name, "status": "failed", "limit": limit,
            "fetched": None, "inserted": None, "updated": None, "skipped": None,
            "duration_s": round(duration_s, 1),
            "error": str(exc),
            # Trimmed: the full traceback already went to stderr; the report
            # only needs enough to triage without re-running.
            "traceback": tb[-4000:] if tb else None,
        })
        return False

    counts = upsert(rows)
    print(f"{name}: limit {limit}, fetched {len(rows)}, "
          f"inserted {counts['inserted']}, updated {counts['updated']}, "
          f"skipped {counts['skipped']}")
    _append_report(report_path, {
        "ts": ts, "source": name, "status": "ok", "limit": limit,
        "fetched": len(rows), "inserted": counts["inserted"],
        "updated": counts["updated"], "skipped": counts["skipped"],
        "duration_s": round(duration_s, 1),
    })
    return True


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m ingest.run")
    parser.add_argument("--source", choices=sorted(SOURCES), default=None,
                        help="ingest a single source (default: all)")
    parser.add_argument("--limit", type=int, default=None,
                        help="override max events for EVERY source with this value "
                             "(default: each source's own limit from SOURCE_LIMITS)")
    args = parser.parse_args()

    init_db()  # idempotent (CREATE TABLE IF NOT EXISTS)

    report_path = _report_path()
    _trim_report(report_path)

    names = [args.source] if args.source else sorted(SOURCES)
    failures = 0

    if len(names) == 1:
        # Single-source mode: no pool needed, nothing to fan out.
        name = names[0]
        limit = args.limit if args.limit is not None else SOURCE_LIMITS.get(name, DEFAULT_LIMIT)
        start = time.monotonic()
        _, _, rows, exc, tb = _run_source(name, limit)
        ok = _process_result(name, limit, rows, exc, tb, time.monotonic() - start, report_path)
        if not ok:
            failures += 1
    else:
        # Sources are independent domains and purely I/O-bound (network
        # waits), so fetch them concurrently — one worker per source. DB
        # writes stay serialized on the main thread via as_completed(), so
        # sqlite only ever sees a single writer even though N fetches are
        # in flight at once.
        limits = {name: (args.limit if args.limit is not None
                          else SOURCE_LIMITS.get(name, DEFAULT_LIMIT)) for name in names}
        starts = {name: time.monotonic() for name in names}
        with ThreadPoolExecutor(max_workers=len(names)) as pool:
            futures = {pool.submit(_run_source, name, limits[name]): name for name in names}
            for future in as_completed(futures):
                name, limit, rows, exc, tb = future.result()
                duration_s = time.monotonic() - starts[name]
                ok = _process_result(name, limit, rows, exc, tb, duration_s, report_path)
                if not ok:
                    failures += 1

    return 1 if failures == len(names) else 0


if __name__ == "__main__":
    sys.exit(main())

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
from datetime import datetime
from pathlib import Path

import requests
import urllib3.exceptions

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


# --- Wake-aware retry -------------------------------------------------------
#
# Root cause of the 2026-07-09 incident: launchd fires the daily job the
# instant the Mac wakes, before WiFi has renegotiated/DHCP'd/DNS'd. The first
# ingest pass lands in that dead window and several sources fail with
# read/connect timeouts within the first ~15 min; by the time a human
# re-runs `ingest.run --source <name>` by hand the network is back and it
# just works. That's a transient cold-network race, not a real block — so
# instead of a human having to notice and re-run, retry automatically, but
# ONLY for failures that are actually network-shaped. A source that raises
# because its own "every page failed" logic tripped (eventbrite) or because
# the page shape changed (JSON/parse error) won't be fixed by waiting; retrying
# those just wastes the retry budget and delays the report.
TRANSIENT_EXC_TYPES = (
    requests.exceptions.Timeout,       # covers ReadTimeout, ConnectTimeout
    requests.exceptions.ConnectionError,
    urllib3.exceptions.TimeoutError,
    urllib3.exceptions.NewConnectionError,
    urllib3.exceptions.MaxRetryError,
    ConnectionError,                   # stdlib base that urllib3/socket raise into
    TimeoutError,                      # stdlib base (socket.timeout is an alias in py3.10+)
)


def _is_transient(exc: Exception | None) -> bool:
    """True if exc (or anything it wraps) is network-shaped and therefore
    worth retrying once the network is back. Sources often re-raise a
    lower-level requests/urllib3 error wrapped in their own exception (e.g.
    a RuntimeError with the original error's str() folded in) — walk
    __cause__/__context__ so `raise RuntimeError(...) from exc` and bare
    `except: raise RuntimeError(...)` (implicit __context__) are both
    classified by the ORIGINAL exception type, not the wrapper's type.
    Classification is by isinstance, not string-matching, so it can't be
    fooled by an unrelated error whose message happens to mention "timeout".
    """
    seen: set[int] = set()
    cur = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, TRANSIENT_EXC_TYPES):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


# Reliable, cheap endpoints to poll for "is the network actually back yet" —
# lu.ma is one of our own sources (so a 200 there means the *kind* of request
# ingest makes will work), 1.1.1.1 is a stable fallback that doesn't depend on
# any one vendor being up. A HEAD is enough; we only care about "did this
# come back at all," not the response body.
CONNECTIVITY_URLS = ["https://api.lu.ma", "https://1.1.1.1"]
CONNECTIVITY_TIMEOUT_S = 5
CONNECTIVITY_POLL_INTERVAL_S = 10
CONNECTIVITY_WAIT_CAP_S = 180  # 3 min — matches the observed ~15 min cold window
# closing well before the OS/router would need that long; if it's still down
# after 3 min this isn't a wake race anymore, so stop waiting and just retry
# (the retry will fail fast and land in the report for a human to see).
MAX_RETRY_ROUNDS = 2


def _network_is_up() -> bool:
    for url in CONNECTIVITY_URLS:
        try:
            requests.head(url, timeout=CONNECTIVITY_TIMEOUT_S)
            return True  # any response at all (even a 4xx) proves the network path works
        except requests.exceptions.RequestException:
            continue
    return False


def _wait_for_network() -> float:
    """Poll connectivity every ~10s until it's back or CONNECTIVITY_WAIT_CAP_S
    elapses. Returns elapsed seconds (for the log line) regardless of outcome
    — if the cap is hit we proceed to retry anyway rather than give up
    silently, since a source that's still down will just fail again and that
    failure is itself useful information in the report."""
    start = time.monotonic()
    while True:
        if _network_is_up():
            return time.monotonic() - start
        elapsed = time.monotonic() - start
        if elapsed >= CONNECTIVITY_WAIT_CAP_S:
            return elapsed
        time.sleep(CONNECTIVITY_POLL_INTERVAL_S)


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
                     tb: str | None, duration_s: float, report_path: Path,
                     attempt: int = 1) -> bool:
    """Upsert (if successful) and append the report line. Returns True on
    success. Must be called from the main thread — this is where the single
    sqlite writer lives.

    `attempt` is 1 for the initial pass, 2/3 for wake-aware retries (see
    "Wake-aware retry" above) — carried into the report row so a human
    scanning ingest-report.jsonl can see a source recover on retry instead of
    just seeing two unexplained rows for the same source.

    `ts` uses the SAME basis as pipeline.py's _log — local-naive
    datetime.now(), not UTC — so report rows and pipeline stdout log lines
    can be cross-referenced by eye without a timezone conversion. The
    container sets TZ=America/Los_Angeles and the rest of the app uses naive
    datetime.now() throughout; this file previously used UTC, which was the
    one inconsistent timestamp basis in the codebase.
    """
    ts = datetime.now().isoformat(timespec="seconds")
    if exc is not None:
        print(f"{name}: FAILED (attempt {attempt}) — {exc}", file=sys.stderr)
        print(tb, file=sys.stderr)
        _append_report(report_path, {
            "ts": ts, "source": name, "status": "failed", "limit": limit,
            "fetched": None, "inserted": None, "updated": None, "skipped": None,
            "duration_s": round(duration_s, 1), "attempt": attempt,
            "error": str(exc),
            # Trimmed: the full traceback already went to stderr; the report
            # only needs enough to triage without re-running.
            "traceback": tb[-4000:] if tb else None,
        })
        return False

    counts = upsert(rows)
    print(f"{name}: limit {limit}, fetched {len(rows)}, "
          f"inserted {counts['inserted']}, updated {counts['updated']}, "
          f"skipped {counts['skipped']}" + (f" (attempt {attempt})" if attempt > 1 else ""))
    _append_report(report_path, {
        "ts": ts, "source": name, "status": "ok", "limit": limit,
        "fetched": len(rows), "inserted": counts["inserted"],
        "updated": counts["updated"], "skipped": counts["skipped"],
        "duration_s": round(duration_s, 1), "attempt": attempt,
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
    limits = {name: (args.limit if args.limit is not None
                      else SOURCE_LIMITS.get(name, DEFAULT_LIMIT)) for name in names}

    # `succeeded` tracks FINAL per-source status after every retry round, so
    # the exit code below reflects "did this source ever come back," not just
    # the first pass. A name starts out absent from `succeeded`; each attempt
    # (initial or retry) either adds it (success) or leaves it out (failure).
    succeeded: set[str] = set()

    def _run_round(round_names: list[str], attempt: int) -> list[str]:
        """Run one round (initial or retry) over round_names, in parallel
        when there's more than one, sequential-through-the-same-code-path
        otherwise. Returns the subset that failed with a transient error —
        i.e. the ones eligible for the NEXT retry round. Non-transient
        failures are dropped here for good; they're already in the report."""
        transient_failures: list[str] = []
        starts = {name: time.monotonic() for name in round_names}
        if len(round_names) == 1:
            name = round_names[0]
            _, _, rows, exc, tb = _run_source(name, limits[name])
            duration_s = time.monotonic() - starts[name]
            ok = _process_result(name, limits[name], rows, exc, tb, duration_s, report_path, attempt=attempt)
            if ok:
                succeeded.add(name)
            elif _is_transient(exc):
                transient_failures.append(name)
        else:
            # Sources are independent domains and purely I/O-bound (network
            # waits), so fetch them concurrently — one worker per source. DB
            # writes stay serialized on the main thread via as_completed(), so
            # sqlite only ever sees a single writer even though N fetches are
            # in flight at once.
            with ThreadPoolExecutor(max_workers=len(round_names)) as pool:
                futures = {pool.submit(_run_source, name, limits[name]): name for name in round_names}
                for future in as_completed(futures):
                    name, limit, rows, exc, tb = future.result()
                    duration_s = time.monotonic() - starts[name]
                    ok = _process_result(name, limit, rows, exc, tb, duration_s, report_path, attempt=attempt)
                    if ok:
                        succeeded.add(name)
                    elif _is_transient(exc):
                        transient_failures.append(name)
        return transient_failures

    retry_pool = _run_round(names, attempt=1)

    # Wake-aware retry: only for sources whose FIRST failure looked
    # network-shaped (see "Wake-aware retry" above). Attempt 2, then attempt
    # 3 — each round only re-fetches sources still in retry_pool, so a source
    # that recovers on attempt 2 doesn't get hit again on attempt 3.
    for attempt in range(2, MAX_RETRY_ROUNDS + 2):
        if not retry_pool:
            break
        print(f"{len(retry_pool)} source(s) failed with transient network errors; "
              f"waiting for network before retry…", file=sys.stderr)
        elapsed = _wait_for_network()
        # elapsed hitting the cap means _wait_for_network gave up rather than
        # observed a live connection — say so honestly instead of claiming
        # the network is "back" when it might still be down (see that
        # function's docstring: it proceeds to retry either way).
        status = "network back" if elapsed < CONNECTIVITY_WAIT_CAP_S else "still down after cap"
        print(f"{status} after {elapsed:.0f}s, retrying: {', '.join(sorted(retry_pool))}",
              file=sys.stderr)
        retry_pool = _run_round(retry_pool, attempt=attempt)

    return 1 if not succeeded else 0


if __name__ == "__main__":
    sys.exit(main())

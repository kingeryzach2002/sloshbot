"""Unified data-layer refresh: ingest -> dedup -> geocode -> geofilter -> score
-> blurbs -> prune.

Host-agnostic — invoke once from cron / a systemd timer, or pass --loop to run
forever with a sleep interval (for a host with no native scheduler). Ingestion
stays maximally inclusive (every source, every event, no category filtering);
show/hide is entirely the scoring layer's job (tiers, NEVER_CONFIDENT_SOURCES).

Usage:
  uv run python -m pipeline                     # one full refresh, then exit
  uv run python -m pipeline --loop               # refresh every 4h, forever
  uv run python -m pipeline --loop --interval 6  # every 6h
  uv run python -m pipeline --rescore             # also force a full booze rescore
"""
import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta

from db import get_conn, init_db

# Past events nobody interacted with (no feedback, no hold) are pruned once
# this far past their end time — keeps the DB bounded on a long-running host.
# Events WITH feedback/holds are kept forever: that's the signal host-reputation
# scoring depends on (scoring/scorers/booze.py::_host_reputation), so pruning
# must never remove it.
RETENTION_DAYS = 30

STEPS = [
    ("ingest", [sys.executable, "-m", "ingest.run"]),
    ("dedup", [sys.executable, "-m", "ingest.dedup"]),
    ("geocode", [sys.executable, "-m", "ingest.geocode"]),
    # geofilter runs after geocode (needs coordinates) and before scoring, so
    # we never spend LLM scoring calls on events we're about to drop as
    # out-of-area. Source-agnostic backstop for feeds that leak non-Bay-Area
    # events (e.g. Luma's SF place feed); see ingest/geofilter.py.
    ("geofilter", [sys.executable, "-m", "ingest.geofilter"]),
]


def _log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def prune_old_events() -> int:
    """Delete past events with no feedback/hold once RETENTION_DAYS past their
    end time. Returns the number of events removed."""
    cutoff = (datetime.now() - timedelta(days=RETENTION_DAYS)).isoformat()
    with get_conn() as conn:
        ids = [r[0] for r in conn.execute(
            """SELECT e.id FROM events e
               WHERE COALESCE(e.ends_at, e.starts_at) < ?
                 AND NOT EXISTS (SELECT 1 FROM feedback f WHERE f.event_id = e.id)
                 AND NOT EXISTS (SELECT 1 FROM holds h WHERE h.event_id = e.id)""",
            (cutoff,)).fetchall()]
        if not ids:
            return 0
        placeholders = ",".join("?" * len(ids))
        conn.execute(f"DELETE FROM scores WHERE event_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({placeholders})", ids)
        conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", ids)
        conn.commit()
    return len(ids)


def run_once(rescore: bool = False) -> bool:
    """One full refresh. Returns True if every step succeeded — a step failing
    (e.g. one scraper source breaking) doesn't block the rest of the pipeline."""
    init_db()
    ok = True
    for name, cmd in STEPS:
        _log(f"{name}: starting")
        result = subprocess.run(cmd)
        if result.returncode != 0:
            _log(f"{name}: FAILED (exit {result.returncode}) — continuing with remaining steps")
            ok = False
        else:
            _log(f"{name}: done")

    score_cmd = [sys.executable, "-m", "scoring.run"]
    if rescore:
        score_cmd.append("--rescore")
    _log("scoring: starting" + (" (full rescore)" if rescore else ""))
    result = subprocess.run(score_cmd)
    if result.returncode != 0:
        _log(f"scoring: FAILED (exit {result.returncode})")
        ok = False
    else:
        _log("scoring: done")

    # Self-heal card blurbs: newly-scored events already carry a blurb (the booze
    # call returns one), so this only fills upcoming events scored BEFORE blurbs
    # existed — a cheap no-op once every row is filled. Keeps prod current without
    # a manual backfill after deploy.
    _log("blurbs: backfilling any missing card blurbs")
    result = subprocess.run([sys.executable, "-m", "scoring.backfill_blurbs"])
    if result.returncode != 0:
        _log(f"blurbs: FAILED (exit {result.returncode}) — non-fatal")
        ok = False
    else:
        _log("blurbs: done")

    pruned = prune_old_events()
    _log(f"prune: removed {pruned} old event(s) with no feedback/hold")

    return ok


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m pipeline")
    ap.add_argument("--loop", action="store_true",
                    help="run forever, sleeping --interval hours between refreshes")
    ap.add_argument("--interval", type=float, default=4,
                    help="hours between refreshes in --loop mode (default: 4)")
    ap.add_argument("--rescore", action="store_true",
                    help="force a full booze rescore instead of unscored-only "
                         "(expensive — every event costs an LLM call; use sparingly, "
                         "e.g. after a scorer prompt/heuristic change)")
    args = ap.parse_args()

    if not args.loop:
        return 0 if run_once(rescore=args.rescore) else 1

    _log(f"looping every {args.interval}h (Ctrl-C to stop)")
    while True:
        run_once(rescore=args.rescore)
        _log(f"sleeping {args.interval}h")
        time.sleep(args.interval * 3600)


if __name__ == "__main__":
    sys.exit(main())

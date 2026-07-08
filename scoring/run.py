"""Score unscored future events and cache results in the scores table.

Usage:
  uv run python -m scoring.run                      # all scorers, unscored only
  uv run python -m scoring.run --rescore            # recompute everything
  uv run python -m scoring.run --scorer booze       # one scorer
"""
import argparse
import importlib
import pkgutil
from datetime import datetime, timedelta

from db import get_conn, init_db
import scoring.scorers

# Discover whichever scorer modules exist — the pipeline runs with any subset.
SCORERS = {
    name: importlib.import_module(f"scoring.scorers.{name}")
    for _, name, _ in pkgutil.iter_modules(scoring.scorers.__path__)
    if not name.startswith("_")
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scorer", default=",".join(SCORERS),
                    help="comma-separated subset of: " + ", ".join(SCORERS))
    ap.add_argument("--rescore", action="store_true",
                    help="recompute even if a cached score exists")
    ap.add_argument("--include-dummy", action="store_true")
    args = ap.parse_args()

    init_db()
    # 3h grace window matches the UI, so in-progress events still get scored
    now = (datetime.now().astimezone() - timedelta(hours=3)).isoformat()
    with get_conn() as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT * FROM events WHERE starts_at >= ? ORDER BY starts_at", (now,))]
    if not args.include_dummy:
        events = [e for e in events if e["source"] != "dummy"]

    names = [n.strip() for n in args.scorer.split(",") if n.strip() in SCORERS]
    with get_conn() as conn:
        cached = {(r["event_id"], r["scorer"])
                  for r in conn.execute("SELECT event_id, scorer FROM scores")}

    total = 0
    for name in names:
        module = SCORERS[name]
        todo = [e for e in events if args.rescore or (e["id"], name) not in cached]
        print(f"[{name}] scoring {len(todo)}/{len(events)} events")
        for e in todo:
            result = module.score(e)
            with get_conn() as conn:
                conn.execute(
                    """INSERT INTO scores (event_id, scorer, score, rationale, scored_at)
                       VALUES (?,?,?,?,?)
                       ON CONFLICT(event_id, scorer) DO UPDATE SET
                         score=excluded.score, rationale=excluded.rationale,
                         scored_at=excluded.scored_at""",
                    (e["id"], name, result["score"], result["rationale"],
                     datetime.now().isoformat(timespec="seconds")))
            total += 1
            print(f"  {result['score']:.2f}  {e['title'][:60]!r}  — {result['rationale'][:90]}")
    print(f"Done: {total} scores written.")


if __name__ == "__main__":
    main()

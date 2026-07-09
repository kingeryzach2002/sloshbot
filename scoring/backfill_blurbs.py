"""Backfill AI card blurbs onto existing booze scores WITHOUT re-scoring.

The in-pipeline path (scoring.run -> scorers.booze._llm) now returns a `blurb`
alongside the score, so freshly-scored events get one for free. This script is
for the events already scored before blurbs existed: it makes a separate minimal
Haiku call per event (scorers.booze.generate_blurb) and fills scores.blurb.

Usage:
  uv run python -m scoring.backfill_blurbs              # upcoming events, all NULL blurbs
  uv run python -m scoring.backfill_blurbs --all        # include past events too
  uv run python -m scoring.backfill_blurbs --limit 3    # cap for testing

Idempotent / resumable: only rows WHERE blurb IS NULL are touched, so re-running
picks up where it left off. Sequential calls — simple and rate-limit-safe.

DB location follows the same resolution as the rest of the code (db.DB_PATH,
overridable with the SLOSHBOT_DB env var).
"""
import argparse

from db import ensure_blurb_column, get_conn
from scoring.scorers.booze import generate_blurb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true",
                    help="include past events (default: only upcoming, to save cost)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap the number of events processed (for testing)")
    args = ap.parse_args()

    with get_conn() as conn:
        ensure_blurb_column(conn)

    where = ["s.scorer = 'booze'", "s.blurb IS NULL"]
    if not args.all:
        where.append("e.starts_at >= datetime('now')")
    sql = (f"""SELECT e.id, e.title, e.host_name, e.venue_name, e.neighborhood, e.description
               FROM scores s JOIN events e ON e.id = s.event_id
               WHERE {' AND '.join(where)}
               ORDER BY e.starts_at""")
    if args.limit is not None:
        sql += f" LIMIT {int(args.limit)}"

    with get_conn() as conn:
        rows = [dict(r) for r in conn.execute(sql)]

    print(f"Backfilling blurbs for {len(rows)} booze scores "
          f"({'all events' if args.all else 'upcoming only'})...")

    done = 0
    with get_conn() as conn:
        for i, event in enumerate(rows, 1):
            b = generate_blurb(event)
            if b:
                conn.execute("UPDATE scores SET blurb=? WHERE event_id=? AND scorer='booze'",
                             (b, event["id"]))
                done += 1
            if i % 25 == 0:
                conn.commit()
                print(f"  {i}/{len(rows)} processed, {done} blurbs written")
        conn.commit()

    print(f"Done: {done}/{len(rows)} blurbs written.")


if __name__ == "__main__":
    main()

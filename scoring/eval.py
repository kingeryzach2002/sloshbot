"""Evaluate the booze scorer against the hand-labeled golden set.

Usage:
  uv run python -m scoring.eval          # compare cached DB scores to labels
  uv run python -m scoring.eval --live   # re-run the scorer now (12 LLM calls)

The golden set (golden_set.csv) is never used for training/tuning automatically;
it exists so a human can see whether a prompt/weight change helped or hurt.
"""
import argparse
import csv
from pathlib import Path

from db import get_conn
from scoring.scorers import booze

GOLDEN = Path(__file__).parent.parent / "golden_set.csv"
THRESHOLD = 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="re-run the booze scorer instead of using cached scores")
    args = ap.parse_args()

    with open(GOLDEN) as f:
        golden = [r for r in csv.DictReader(f) if r.get("your_guess", "").strip()]
    if not golden:
        print("golden_set.csv has no labeled rows.")
        return

    with get_conn() as conn:
        events = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM events")}
        cached = {r["event_id"]: r["score"] for r in conn.execute(
            "SELECT event_id, score FROM scores WHERE scorer='booze'")}

    results = []
    for row in golden:
        eid = row["event_id"]
        label = row["your_guess"].strip().upper() in ("TRUE", "1", "YES")
        if args.live:
            if eid not in events:
                print(f"  [skip] {eid} no longer in DB")
                continue
            score = booze.score(events[eid])["score"]
        else:
            if eid not in cached:
                print(f"  [skip] {eid} has no cached booze score")
                continue
            score = cached[eid]
        results.append({"title": row["title"][:52], "label": label, "score": score,
                        "note": (row.get("notes") or "").strip()})

    correct = sum((r["score"] >= THRESHOLD) == r["label"] for r in results)
    brier = sum((r["score"] - r["label"]) ** 2 for r in results) / len(results)

    print(f"\n{'✓/✗':<4}{'score':<7}{'label':<7}title")
    for r in sorted(results, key=lambda r: -abs(r["score"] - r["label"])):
        ok = (r["score"] >= THRESHOLD) == r["label"]
        note = f"  ({r['note']})" if r["note"] else ""
        print(f"{'✓' if ok else '✗':<4}{r['score']:<7.2f}{str(r['label']):<7}{r['title']}{note}")

    print(f"\naccuracy@{THRESHOLD}: {correct}/{len(results)}"
          f"   brier: {brier:.3f} (lower is better, 0.25 = coin flip)")


if __name__ == "__main__":
    main()

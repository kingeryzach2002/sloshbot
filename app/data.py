"""Data layer: pure reads from SQLite -> Event objects. No filtering, no
ranking, no view concerns. This is the ONLY module that runs SELECTs for the
read path.

Replaces the read half of the old app/main.py::load_events (the five queries at
main.py:156-170 and the attach loops at main.py:172-186), plus get_settings
(main.py:32-34).

CONTRACT
  fetch_events(start, end, user_id) -> list[Event]
      One connection. Window query on events (starts_at in [start,end),
      duplicate_of IS NULL, ORDER BY starts_at) -> Event.from_row. Then attach,
      by joining the other tables in the SAME connection:
        - scores[scorer]=score and rationales[scorer]=rationale  (scores table)
        - tags = [tag,...] ordered by tag                        (event_tags)
        - feedback = {verdict,...}  THIS user_id's own verdicts only — the
          "did you go" button state is per-viewer, not shared.
        - host_rep = {"ok": n_as_promised, "miss": n_not_as_promised} or None,
          keyed by the event's host_name (events with no host_name -> None).
          Aggregated across EVERY user's feedback, not just user_id's — this
          is the crowdsourced signal, deliberately unscoped.
      Do NOT compute composite/tier/distance/gcal/dt here — later stages own those.
  get_settings(user_id) -> dict[str, str]     # key->value from the settings table
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from app.models import Event
from db import get_conn


def fetch_events(start: datetime, end: datetime, user_id: str) -> list[Event]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM events WHERE starts_at >= ? AND starts_at < ?
               AND duplicate_of IS NULL ORDER BY starts_at""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        score_rows = conn.execute("SELECT * FROM scores").fetchall()
        fb_rows = conn.execute(
            "SELECT event_id, verdict FROM feedback WHERE user_id = ?", (user_id,)).fetchall()
        tag_rows = conn.execute("SELECT event_id, tag FROM event_tags ORDER BY tag").fetchall()
        rep_rows = conn.execute(
            """SELECT ev.host_name AS host, f.verdict, count(*) AS n
               FROM feedback f JOIN events ev ON ev.id = f.event_id
               WHERE f.verdict IN ('as_promised', 'not_as_promised')
                 AND ev.host_name IS NOT NULL
               GROUP BY ev.host_name, f.verdict""").fetchall()

    scores: dict[str, dict] = defaultdict(dict)
    rationales: dict[str, dict] = defaultdict(dict)
    for s in score_rows:
        scores[s["event_id"]][s["scorer"]] = s["score"]
        rationales[s["event_id"]][s["scorer"]] = s["rationale"]
    feedback: dict[str, set] = defaultdict(set)
    for f in fb_rows:
        feedback[f["event_id"]].add(f["verdict"])
    tags: dict[str, list] = defaultdict(list)
    for t in tag_rows:
        tags[t["event_id"]].append(t["tag"])
    host_rep: dict[str, dict] = defaultdict(lambda: {"ok": 0, "miss": 0})
    for r in rep_rows:
        host_rep[r["host"]]["ok" if r["verdict"] == "as_promised" else "miss"] += r["n"]

    events: list[Event] = []
    for r in rows:
        e = Event.from_row(r)
        e.scores = scores.get(e.id, {})
        e.rationales = rationales.get(e.id, {})
        e.tags = tags.get(e.id, [])
        e.feedback = feedback.get(e.id, set())
        # .get (not []) so a host with no feedback stays None, not {ok:0,miss:0}
        rep = host_rep.get(e.host_name) if e.host_name else None
        e.host_rep = dict(rep) if rep else None
        events.append(e)
    return events


def get_settings(user_id: str) -> dict:
    with get_conn() as conn:
        return {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM settings WHERE user_id = ?", (user_id,))}

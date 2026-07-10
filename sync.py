"""Bridge between the local scraping host and the prod read-only server.

Sloshbot used to scrape and serve from the same box. It no longer can: most
sources rate-limit or outright block datacenter IPs, so scraping has to run
somewhere with a residential IP — this Mac. Prod, meanwhile, is where the app
actually gets browsed from, and has no business running a scraper. The two
hosts are disentangled into a "dumb read-only server + smart local worker"
split, bridged by exactly two admin endpoints on prod (see ARCHITECTURE.md's
sibling doc on the server side for their implementation):

    GET  /admin/export   prod  -> local   (per-user state: users/feedback/holds,
                                            plus the events those rows reference)
    POST /admin/ingest   local -> prod    (the catalog: events/scores/tags/geocode)

That split maps directly onto "who owns which table":

    prod-owned  (pull-only, never pushed)   users, feedback, holds
    local-owned (push-only, never pulled)   events, scores, event_tags, geocode_cache

events is the one table that appears on both sides of the wire, for different
reasons: pull needs a *copy* of the prod events referenced by feedback/holds
FKs (so foreign keys resolve locally even for an event this host has since
pruned — see pipeline.py's RETENTION_DAYS), while push sends this host's own,
fresher catalog for prod to serve. That's also why pull's event upsert is
`ON CONFLICT DO NOTHING`: this host's freshly-scraped copy of an event must
never be clobbered by a stale one coming back from prod.

Usage:
    uv run python -m sync pull   # prod's per-user state -> local DB
    uv run python -m sync push   # local catalog -> prod (--force to allow an
                                  # empty events list, which prod treats as
                                  # "delete every untouched event")
    uv run python -m sync run    # pull, refresh the catalog (pipeline.run_once),
                                  # push — the one command the daily launchd job runs
    uv run python -m sync run --rescore
                                  # same, but forces a full booze rescore
                                  # (pipeline.run_once(rescore=True)) instead of
                                  # unscored-only — expensive, one LLM call per
                                  # event; use sparingly, e.g. right after a
                                  # scorer prompt/heuristic change, to push the
                                  # rescored catalog to prod in one command
                                  # (mirrors pipeline.py's own --rescore)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime

import requests

from db import get_conn, init_db

REQUEST_TIMEOUT = 120  # prod may be doing real work (write + geocode joins); be patient


def _log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def _session_and_base() -> tuple[requests.Session, str]:
    """Reads prod URL + admin token from env and builds an authenticated
    Session. Fails loudly (rather than sending an unauthenticated or
    misdirected request) if either is missing — this is a cron job with no
    one watching, so a silent no-op failure could go unnoticed for days."""
    base = os.environ.get("SLOSHBOT_PROD_URL")
    token = os.environ.get("SLOSHBOT_ADMIN_TOKEN")
    missing = [name for name, val in
               [("SLOSHBOT_PROD_URL", base), ("SLOSHBOT_ADMIN_TOKEN", token)] if not val]
    if missing:
        _log(f"ERROR: missing required env var(s): {', '.join(missing)}. "
             f"Set them in the environment or in .env.sloshbot (see "
             f".env.sloshbot.example) before running sync.")
        sys.exit(1)

    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {token}"
    return session, base.rstrip("/")


# ---------------------------------------------------------------------------
# pull: prod's per-user state -> local DB
# ---------------------------------------------------------------------------

def pull() -> None:
    session, base = _session_and_base()
    _log(f"pull: fetching {base}/admin/export")
    resp = session.get(f"{base}/admin/export", timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        _log(f"pull: FAILED ({resp.status_code}): {resp.text[:2000]}")
        sys.exit(1)
    data = resp.json()

    users = data.get("users", [])
    feedback = data.get("feedback", [])
    holds = data.get("holds", [])
    events = data.get("events", [])

    # A fresh local checkout (or a brand-new laptop) has no DB file yet —
    # make sure the schema exists before we start writing into it.
    init_db()

    with get_conn() as conn:
        # (a) Events referenced by feedback/holds, so those FKs resolve even
        # for events this host has since pruned (RETENTION_DAYS) or never
        # scraped in the first place. ON CONFLICT DO NOTHING because this
        # host's own scrape of an event (if it has one) is strictly fresher
        # than whatever prod is echoing back — never let a pull regress a
        # local row.
        event_cols = None
        for e in events:
            if event_cols is None:
                event_cols = list(e.keys())
                col_list = ", ".join(event_cols)
                placeholders = ", ".join("?" * len(event_cols))
                sql = (f"INSERT INTO events ({col_list}) VALUES ({placeholders}) "
                       f"ON CONFLICT(id) DO NOTHING")
            conn.execute(sql, [e[c] for c in event_cols])

        # (b) Users: prod is the source of truth for accounts, but local
        # `settings` rows may already FK a user_id created by an earlier
        # pull — never delete a local user out from under those rows, just
        # add any prod has that we don't.
        for u in users:
            conn.execute(
                """INSERT OR IGNORE INTO users (id, email, password_hash, created_at)
                   VALUES (?, ?, ?, ?)""",
                (u["id"], u.get("email"), u.get("password_hash"), u["created_at"]))

        # (c) feedback/holds are entirely prod-owned — prod is the only place
        # users actually interact with the app, so the export is the full,
        # authoritative set. Wholesale replace rather than merge: any row
        # deleted on prod (undo a feedback tap, drop a hold) must disappear
        # locally too, and a diff/merge would need per-row change tracking
        # this schema doesn't have.
        conn.execute("DELETE FROM feedback")
        for f in feedback:
            conn.execute(
                """INSERT INTO feedback (user_id, event_id, verdict, lens, note, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (f["user_id"], f["event_id"], f["verdict"], f.get("lens", ""),
                 f.get("note"), f["created_at"]))

        conn.execute("DELETE FROM holds")
        for h in holds:
            conn.execute(
                """INSERT INTO holds (user_id, event_id, lens, created_at)
                   VALUES (?, ?, ?, ?)""",
                (h["user_id"], h["event_id"], h.get("lens", ""), h["created_at"]))

        conn.commit()

    _log(f"pull: done — events referenced={len(events)}, users={len(users)}, "
         f"feedback={len(feedback)}, holds={len(holds)}")


# ---------------------------------------------------------------------------
# push: local catalog -> prod
# ---------------------------------------------------------------------------

def _rows_as_dicts(conn, sql: str) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql).fetchall()]
    except Exception as exc:
        # geocode_cache in particular may not exist on a brand-new local DB
        # that hasn't run ingest/geocode.py yet — treat "table missing" as
        # "nothing to push" rather than a hard failure.
        if "no such table" in str(exc):
            return []
        raise


def push(force: bool = False) -> None:
    session, base = _session_and_base()

    with get_conn() as conn:
        events = _rows_as_dicts(conn, "SELECT * FROM events")
        scores = _rows_as_dicts(conn, "SELECT * FROM scores")
        event_tags = _rows_as_dicts(conn, "SELECT * FROM event_tags")
        geocode_cache = _rows_as_dicts(conn, "SELECT * FROM geocode_cache")

    # Safety interlock: prod's /admin/ingest reconciles deletions — every prod
    # event absent from this payload (and untouched by any user) gets dropped.
    # An EMPTY events list is therefore indistinguishable from "delete the
    # whole catalog", and the realistic way to produce one is a broken local
    # DB (fresh checkout, wrong SLOSHBOT_DB, catastrophic scrape failure) —
    # not an intentional wipe. Refuse unless explicitly forced.
    if not events and not force:
        _log("push: REFUSING to push an empty events list — this would tell "
             "prod to delete its entire untouched catalog. If the local DB "
             "really is the truth, rerun with --force.")
        sys.exit(1)

    payload = {
        "events": events,
        "scores": scores,
        "event_tags": event_tags,
        "geocode_cache": geocode_cache,
    }
    _log(f"push: sending {len(events)} events, {len(scores)} scores, "
         f"{len(event_tags)} event_tags, {len(geocode_cache)} geocode_cache rows "
         f"to {base}/admin/ingest")
    resp = session.post(f"{base}/admin/ingest", json=payload, timeout=REQUEST_TIMEOUT)
    if not resp.ok:
        _log(f"push: FAILED ({resp.status_code}): {resp.text[:2000]}")
        sys.exit(1)

    result = resp.json()
    _log(f"push: done — {result}")


# ---------------------------------------------------------------------------
# run: pull -> refresh catalog -> push (the daily launchd job)
# ---------------------------------------------------------------------------

def run(rescore: bool = False) -> int:
    """The one command the daily schedule runs. Always attempts all three
    steps, and always pushes even if the pipeline refresh partially failed
    (partial fresh data beats none reaching prod) — but the process still
    exits nonzero if the pipeline reported a failure OR the push itself
    failed, so a broken run shows up in the launchd log / exit status.

    rescore=True forces pipeline.run_once's full booze rescore (every event,
    not just unscored ones) — expensive (one LLM call per event), used
    right after a scorer prompt/heuristic change to get the rescored
    catalog pushed to prod in one command instead of a manual
    `pipeline --rescore` + `sync push`."""
    # Imported lazily (not at module top) so `python -m sync pull`/`push`
    # alone don't need the ingest/scoring stack importable — only `run` does.
    from pipeline import run_once

    # A pull failure (prod briefly down, network blip) must not cancel the
    # local refresh: the previous pull's feedback/holds are still in the local
    # DB, so scoring just runs on slightly stale crowd signal. The push below
    # will surface the outage anyway if prod is still down by then.
    _log("run: pulling per-user state from prod")
    pull_failed = False
    try:
        pull()
    except SystemExit as exc:
        pull_failed = exc.code not in (0, None)
        if pull_failed:
            _log("run: pull failed — continuing with the last pull's "
                 "feedback/holds (scoring uses slightly stale crowd signal)")

    _log("run: refreshing local catalog (pipeline.run_once)"
         + (" (full rescore)" if rescore else ""))
    pipeline_ok = run_once(rescore=rescore)
    if not pipeline_ok:
        _log("run: pipeline reported a failure — pushing partial data anyway")

    _log("run: pushing catalog to prod")
    push_failed = False
    try:
        push()
    except SystemExit as exc:
        push_failed = exc.code not in (0, None)

    if pull_failed or not pipeline_ok or push_failed:
        _log("run: FAILED (pull/pipeline/push failure — see above)")
        return 1
    _log("run: done")
    return 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] not in ("pull", "push", "run"):
        print(f"usage: {sys.argv[0]} {{pull|push|run}} [--rescore]", file=sys.stderr)
        return 2
    cmd, rest = args[0], args[1:]
    rescore = "--rescore" in rest
    force = "--force" in rest
    unknown = [a for a in rest if a not in ("--rescore", "--force")]
    if unknown or (rescore and cmd != "run") or (force and cmd != "push"):
        print(f"usage: {sys.argv[0]} {{pull|push [--force]|run [--rescore]}}  "
              f"(--rescore only applies to `run`; --force only to `push`, "
              f"to allow an empty-catalog push)", file=sys.stderr)
        return 2

    if cmd == "pull":
        pull()
        return 0
    if cmd == "push":
        push(force=force)
        return 0
    return run(rescore=rescore)


if __name__ == "__main__":
    sys.exit(main())

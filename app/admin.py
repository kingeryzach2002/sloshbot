"""Admin sync surface: the pipeline no longer runs on this host.

Scrape -> score now happens on an external (residential) machine, because the
event sources increasingly block datacenter IPs. This host is a read-only
server over the shared catalog; these two endpoints are the seam the external
pipeline syncs through:

  GET  /admin/export  -> the crowd signal the pipeline needs BEFORE it runs:
                         every user's feedback/holds (host reputation in
                         scoring/scorers/booze.py::_host_reputation aggregates
                         ALL users, and prune must never drop an event someone
                         interacted with), plus the users rows they FK onto and
                         the event rows they reference — the local DB needs
                         those event rows to satisfy feedback/holds FKs and to
                         keep host_name joins working.
  POST /admin/ingest  -> the freshly scraped+scored catalog pushed back:
                         events/scores/event_tags (upsert + reconcile
                         deletions) and the shared geocode_cache.

Auth is a static bearer token (SLOSHBOT_ADMIN_TOKEN) compared with
secrets.compare_digest — one trusted machine, no per-user identity needed.
If the env var is unset the endpoints are DISABLED (503), never open: a
misconfigured deploy must fail closed, because /admin/export hands out
password_hash columns (all NULL for anonymous users, but legacy rows exist)
and /admin/ingest writes the whole catalog.
"""
import os
import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

import pipeline
from db import get_conn, init_db
from ingest.geocode import CACHE_DDL

router = APIRouter(prefix="/admin")

# Column lists mirror ingest/schema.sql (+ the blurb migration in db.py) so the
# upsert SQL can't silently drop a column. If the schema grows a column, add it
# here — the payload comes from a peer running this same repo, so both sides
# move together.
EVENT_COLS = ["id", "source", "source_id", "url", "title", "description",
              "host_name", "host_url", "venue_name", "address", "neighborhood",
              "starts_at", "ends_at", "is_free", "price_min", "price_max",
              "rsvp_type", "image_url", "lat", "lon", "duplicate_of", "raw",
              "scraped_at"]
SCORE_COLS = ["event_id", "scorer", "score", "rationale", "blurb", "scored_at"]
# Mirrors ingest/geocode.py::CACHE_DDL's column order exactly.
GEOCODE_COLS = ["query", "lat", "lon"]

# NOT "INSERT OR REPLACE": that is a DELETE + re-INSERT under the hood, which
# would cascade-orphan (or FK-block) the feedback/holds rows pointing at the
# event. ON CONFLICT ... DO UPDATE mutates the row in place, keeping FKs alive.
_EVENT_UPSERT = (
    f"INSERT INTO events ({', '.join(EVENT_COLS)}) "
    f"VALUES ({', '.join('?' * len(EVENT_COLS))}) "
    "ON CONFLICT(id) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in EVENT_COLS if c != "id")
)
_SCORE_UPSERT = (
    f"INSERT INTO scores ({', '.join(SCORE_COLS)}) "
    f"VALUES ({', '.join('?' * len(SCORE_COLS))}) "
    "ON CONFLICT(event_id, scorer) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in SCORE_COLS
                if c not in ("event_id", "scorer"))
)

# Keep well under SQLite's per-statement parameter cap (999 on older builds)
# when expanding id lists into IN (...) placeholders.
_CHUNK = 500


def require_admin(authorization: str = Header(default="")) -> None:
    """Dependency: bearer-token gate for the sync endpoints. Fails closed —
    no configured token means the endpoints don't exist for anyone (503),
    not that they're open."""
    expected = os.environ.get("SLOSHBOT_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(status_code=503, detail="admin endpoints disabled: SLOSHBOT_ADMIN_TOKEN not set")
    supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
    # compare_digest, not ==, so a wrong token takes the same time as a
    # nearly-right one (no timing oracle on the shared secret).
    if not (supplied and secrets.compare_digest(supplied, expected)):
        raise HTTPException(status_code=401, detail="invalid or missing bearer token")


def _chunks(seq: list, n: int = _CHUNK):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


@router.get("/export", dependencies=[Depends(require_admin)])
def export():
    """Everything the local pipeline needs BEFORE it runs a cycle: the full
    crowd signal (users/feedback/holds) plus the event rows that signal FKs
    onto. Deliberately NOT the whole events table — the pipeline re-scrapes
    and re-uploads the catalog itself via /admin/ingest; export only needs to
    hand back what it can't re-derive (the per-user state)."""
    with get_conn() as conn:
        users = [dict(r) for r in conn.execute(
            "SELECT id, email, password_hash, created_at FROM users")]
        feedback = [dict(r) for r in conn.execute(
            "SELECT user_id, event_id, verdict, lens, note, created_at FROM feedback")]
        holds = [dict(r) for r in conn.execute(
            "SELECT user_id, event_id, lens, created_at FROM holds")]

        # Every event referenced by any feedback or hold row, deduped — the
        # local DB needs these rows to satisfy the feedback/holds FKs it will
        # re-insert, and to keep host_name joins (host reputation) working
        # for hosts whose events aren't otherwise in this pipeline cycle.
        event_ids = sorted({r["event_id"] for r in feedback} | {r["event_id"] for r in holds})
        events: list[dict] = []
        for chunk in _chunks(event_ids):
            placeholders = ",".join("?" * len(chunk))
            events.extend(dict(r) for r in conn.execute(
                f"SELECT * FROM events WHERE id IN ({placeholders})", chunk))

    return {"users": users, "feedback": feedback, "holds": holds, "events": events}


class IngestBody(BaseModel):
    """Plain-dict rows, not typed sub-models — this endpoint is a wire-format
    mirror of the local DB's own tables (peer runs the same repo, same
    schema), so validating column-by-column here would just duplicate
    ingest/schema.sql. Malformed rows fail loudly as a KeyError/sqlite error,
    which is fine for a trusted single-caller admin endpoint."""
    events: list[dict] = []
    scores: list[dict] = []
    event_tags: list[dict] = []
    geocode_cache: list[dict] = []


@router.post("/ingest", dependencies=[Depends(require_admin)])
def ingest(body: IngestBody):
    """Receive the freshly scraped+scored catalog from the external pipeline
    and make this host's DB match it, in one transaction:
      - upsert every event (never INSERT OR REPLACE — see _EVENT_UPSERT)
      - delete prod events absent from the payload that nobody has touched
        (feedback/holds are crowd signal — those events are always kept)
      - upsert scores per (event_id, scorer); skip rows for events that still
        don't exist after the event upsert (payload bug, not our problem to
        crash on)
      - replace each event's tags wholesale (idempotent, mirrors normalize.py)
      - mirror the geocode_cache (shared, safe to blind-upsert)
    Then, outside that transaction, run the normal retention prune — nothing
    else invokes it now that the pipeline doesn't run on this host."""
    init_db()  # cheap/idempotent; makes ingest safe to hit on a bare DB too

    events_upserted = 0
    scores_upserted = scores_skipped = 0
    tags_written = 0
    geocode_written = 0

    with get_conn() as conn:
        payload_ids = []
        for e in body.events:
            row = [e.get(c) for c in EVENT_COLS]
            conn.execute(_EVENT_UPSERT, row)
            events_upserted += 1
            payload_ids.append(e["id"])

        # Reconcile deletions: anything NOT in this payload and untouched by
        # any user (no feedback, no holds) is stale — drop it. Mirrors
        # pipeline.py::prune_old_events's cascade order (scores, event_tags,
        # then events) so we never leave orphaned child rows.
        existing_ids = {r[0] for r in conn.execute("SELECT id FROM events")}
        stale_ids = existing_ids - set(payload_ids)
        deleted = 0
        if stale_ids:
            keep = set()
            for chunk in _chunks(list(stale_ids)):
                placeholders = ",".join("?" * len(chunk))
                keep |= {r[0] for r in conn.execute(
                    f"SELECT DISTINCT event_id FROM feedback WHERE event_id IN ({placeholders})", chunk)}
                keep |= {r[0] for r in conn.execute(
                    f"SELECT DISTINCT event_id FROM holds WHERE event_id IN ({placeholders})", chunk)}
            to_delete = list(stale_ids - keep)
            for chunk in _chunks(to_delete):
                placeholders = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM scores WHERE event_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM event_tags WHERE event_id IN ({placeholders})", chunk)
                conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", chunk)
            deleted = len(to_delete)

        # Scores: only for events that actually exist post-upsert (the event
        # upsert above ran first, so this is "still missing" = payload bug).
        live_ids = {r[0] for r in conn.execute("SELECT id FROM events")}
        for s in body.scores:
            if s.get("event_id") not in live_ids:
                scores_skipped += 1
                continue
            conn.execute(_SCORE_UPSERT, [s.get(c) for c in SCORE_COLS])
            scores_upserted += 1

        # Tags: replace wholesale per event referenced in the payload's event
        # list (same idempotent-replace semantics as ingest/normalize.py).
        tags_by_event: dict[str, list[str]] = {}
        for t in body.event_tags:
            tags_by_event.setdefault(t["event_id"], []).append(t["tag"])
        for event_id in payload_ids:
            conn.execute("DELETE FROM event_tags WHERE event_id = ?", (event_id,))
            for tag in dict.fromkeys(tags_by_event.get(event_id, [])):
                conn.execute("INSERT INTO event_tags (event_id, tag) VALUES (?, ?)", (event_id, tag))
                tags_written += 1

        # geocode_cache: shared, keyed by query string — blind upsert is safe.
        conn.execute(CACHE_DDL)
        for g in body.geocode_cache:
            conn.execute(
                "INSERT OR REPLACE INTO geocode_cache (query, lat, lon) VALUES (?, ?, ?)",
                [g.get(c) for c in GEOCODE_COLS])
            geocode_written += 1

        conn.commit()

    # Retention prune now runs here instead of in pipeline.py's own run_once —
    # nothing else calls it since the pipeline doesn't execute on this host.
    pruned = pipeline.prune_old_events()

    return {
        "ok": True,
        "events": {"upserted": events_upserted, "deleted": deleted},
        "scores": {"upserted": scores_upserted, "skipped": scores_skipped},
        "tags": tags_written,
        "geocode_cache": geocode_written,
        "pruned": pruned,
    }

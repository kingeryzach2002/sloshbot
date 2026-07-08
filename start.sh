#!/bin/sh
# Container entrypoint. Two independent modes, controlled by env:
#
#   PIPELINE_INTERVAL_HOURS set (nonzero) -> this container also runs the
#     data-refresh loop (ingest -> dedup -> geocode -> score -> prune) in the
#     background via `pipeline.py --loop`, on top of serving the web app.
#     Use this on hosts with no native scheduler/cron.
#
#   PIPELINE_INTERVAL_HOURS unset or 0 -> web app only. Use this on hosts that
#     have native cron/scheduled-job support; point that at
#     `uv run python -m pipeline` as a separate scheduled job instead, so the
#     web process and the refresh job scale/restart independently.
#
# Plus one more, orthogonal, mode layered on top of both of the above:
#
#   LITESTREAM_BUCKET set (non-empty) -> offsite backup is enabled. On boot,
#     Litestream restores /data/sloshbot.db from the bucket if the local file
#     is missing and a backup exists (so a fresh volume self-heals), then
#     supervises the uvicorn process while continuously streaming changes
#     back to the bucket. See litestream.yml and README-DEPLOY.md.
#
#   LITESTREAM_BUCKET unset or empty -> Litestream is never invoked; the
#     container behaves exactly as it did before this feature existed.
#
# set -e would kill this script (and the whole container) if the background
# pipeline loop's subshell ever exited nonzero; we don't want a pipeline
# hiccup to take down the web server, so error handling is explicit below
# instead of relying on set -e.
set -u

INTERVAL="${PIPELINE_INTERVAL_HOURS:-0}"
if [ "$INTERVAL" != "0" ]; then
  (
    sleep 15  # let the web server bind first
    uv run python -m pipeline --loop --interval "$INTERVAL"
  ) &
fi

UVICORN_CMD="uv run uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"

if [ -n "${LITESTREAM_BUCKET:-}" ]; then
  # Self-healing restore: only fires if the DB file doesn't already exist
  # locally AND a backup exists in the bucket. Never overwrites an existing
  # local DB, so this is safe to run on every boot.
  litestream restore -if-db-not-exists -if-replica-exists /data/sloshbot.db

  # Litestream supervises uvicorn: replication starts, then uvicorn runs as
  # the child process, and the whole container exits when uvicorn does.
  exec litestream replicate -exec "$UVICORN_CMD"
else
  # Foreground process: uvicorn. The container lives or dies with this.
  exec $UVICORN_CMD
fi

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

# --- OpenHost bootstrapping ------------------------------------------------
#
# OpenHost has no env-var injection (no dashboard/manifest way to set custom
# env vars) and communicates the persistent per-app data directory ONLY via
# OPENHOST_APP_DATA_DIR at runtime. So on OpenHost, everything that would
# normally be a dashboard env var (ANTHROPIC_API_KEY, LITESTREAM_*,
# SLOSHBOT_SECRET_KEY, overrides) has to live in a file on that persistent
# disk instead: OPENHOST_APP_DATA_DIR/secrets.env. Source it now, before
# anything below reads these vars.
if [ -n "${OPENHOST_APP_DATA_DIR:-}" ] && [ -f "${OPENHOST_APP_DATA_DIR}/secrets.env" ]; then
  set -a
  . "${OPENHOST_APP_DATA_DIR}/secrets.env"
  set +a
fi

# Resolve the effective DB path with the same precedence as db.py: explicit
# SLOSHBOT_DB wins; else OPENHOST_APP_DATA_DIR; else the Railway-style /data
# volume default (preserved for hosts that mount a volume at /data but don't
# set OPENHOST_APP_DATA_DIR). Exporting it unconditionally means Litestream
# (which can't run db.py's Python logic) always gets a concrete path via
# litestream.yml's ${SLOSHBOT_DB} expansion, and db.py always sees an
# explicit SLOSHBOT_DB inside this container (its own fallback logic still
# exists for local/non-container runs that invoke the app directly).
if [ -z "${SLOSHBOT_DB:-}" ]; then
  if [ -n "${OPENHOST_APP_DATA_DIR:-}" ]; then
    SLOSHBOT_DB="${OPENHOST_APP_DATA_DIR}/sloshbot.db"
  else
    SLOSHBOT_DB="/data/sloshbot.db"
  fi
fi
export SLOSHBOT_DB

# First-boot bootstrap: OpenHost has no way to set SLOSHBOT_SECRET_KEY via
# the dashboard, so if it's still unset after sourcing secrets.env, generate
# one, persist it to secrets.env (creating the file 600-permissioned if it
# doesn't exist yet) so it survives redeploys, and export it for this run.
if [ -z "${SLOSHBOT_SECRET_KEY:-}" ] && [ -n "${OPENHOST_APP_DATA_DIR:-}" ]; then
  SECRETS_FILE="${OPENHOST_APP_DATA_DIR}/secrets.env"
  if [ ! -f "$SECRETS_FILE" ]; then
    : > "$SECRETS_FILE"
    chmod 600 "$SECRETS_FILE"
  fi
  SLOSHBOT_SECRET_KEY="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "SLOSHBOT_SECRET_KEY=${SLOSHBOT_SECRET_KEY}" >> "$SECRETS_FILE"
  export SLOSHBOT_SECRET_KEY
  echo "start.sh: generated and persisted a new SLOSHBOT_SECRET_KEY to ${SECRETS_FILE}"
fi

# OpenHost has no cron/scheduler, so the in-container refresh loop must be
# on by default there. Other hosts keep the explicit opt-in (0 = off).
if [ -z "${PIPELINE_INTERVAL_HOURS:-}" ] && [ -n "${OPENHOST_APP_DATA_DIR:-}" ]; then
  PIPELINE_INTERVAL_HOURS=4
fi
# -----------------------------------------------------------------------

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
  litestream restore -if-db-not-exists -if-replica-exists "$SLOSHBOT_DB"

  # Litestream supervises uvicorn: replication starts, then uvicorn runs as
  # the child process, and the whole container exits when uvicorn does.
  exec litestream replicate -exec "$UVICORN_CMD"
else
  # Foreground process: uvicorn. The container lives or dies with this.
  exec $UVICORN_CMD
fi

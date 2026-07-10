#!/bin/sh
# Container entrypoint. This host is a READ-ONLY web server: the scrape ->
# score refresh runs exclusively on an external residential machine (event
# sources increasingly block datacenter IPs, and this host should carry as
# little scraping capability as possible) and pushes the result here over
# HTTPS via POST /admin/ingest (see app/admin.py), after pulling the
# crowd-signal feedback/holds it needs via GET /admin/export. That external
# machine owns the refresh schedule; this container never runs the pipeline.
# (The old PIPELINE_INTERVAL_HOURS in-container loop is gone — run
# `uv run python -m pipeline` on the external machine instead.)
#
# One mode, controlled by env:
#
#   LITESTREAM_BUCKET set (non-empty) -> offsite backup is enabled. On boot,
#     Litestream restores /data/sloshbot.db from the bucket if the local file
#     is missing and a backup exists (so a fresh volume self-heals), then
#     supervises the uvicorn process while continuously streaming changes
#     back to the bucket. See litestream.yml and README-DEPLOY.md.
#
#   LITESTREAM_BUCKET unset or empty -> Litestream is never invoked; the
#     container behaves exactly as it did before this feature existed.
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

# First-boot bootstrap for the admin sync token, mirroring the
# SLOSHBOT_SECRET_KEY block above: OpenHost has no dashboard way to set
# SLOSHBOT_ADMIN_TOKEN either, so if it's still unset after sourcing
# secrets.env, generate one, persist it to secrets.env so it survives
# redeploys, and export it for this run. Without a token, app/admin.py's
# require_admin() fails closed (503) — the residential pipeline machine
# simply can't sync until an operator sets this (here, or manually).
if [ -z "${SLOSHBOT_ADMIN_TOKEN:-}" ] && [ -n "${OPENHOST_APP_DATA_DIR:-}" ]; then
  SECRETS_FILE="${OPENHOST_APP_DATA_DIR}/secrets.env"
  if [ ! -f "$SECRETS_FILE" ]; then
    : > "$SECRETS_FILE"
    chmod 600 "$SECRETS_FILE"
  fi
  SLOSHBOT_ADMIN_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  echo "SLOSHBOT_ADMIN_TOKEN=${SLOSHBOT_ADMIN_TOKEN}" >> "$SECRETS_FILE"
  export SLOSHBOT_ADMIN_TOKEN
  echo "start.sh: generated and persisted a new SLOSHBOT_ADMIN_TOKEN to ${SECRETS_FILE}"
fi
# -----------------------------------------------------------------------

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

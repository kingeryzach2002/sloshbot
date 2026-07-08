#!/bin/sh
# Container entrypoint: web app + a background ingest/score loop.
# PIPELINE_INTERVAL_HOURS=0 disables the loop (web app only).
set -u

INTERVAL="${PIPELINE_INTERVAL_HOURS:-6}"
if [ "$INTERVAL" != "0" ]; then
  (
    sleep 15  # let the web app come up first
    while true; do
      echo "[pipeline] ingest starting"
      uv run python -m ingest.run || echo "[pipeline] ingest failed; will retry next cycle"
      echo "[pipeline] scoring starting"
      uv run python -m scoring.run || echo "[pipeline] scoring failed; will retry next cycle"
      sleep $((INTERVAL * 3600))
    done
  ) &
fi

exec uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

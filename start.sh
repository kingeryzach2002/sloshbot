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

# Foreground process: uvicorn. The container lives or dies with this.
exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"

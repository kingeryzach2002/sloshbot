#!/bin/bash
# launchd wrapper for the daily sync run (com.sloshbot.daily.plist calls
# this, not `uv` directly) — launchd agents start with a bare environment
# (no shell profile sourced, no PATH beyond /usr/bin:/bin:/usr/sbin:/sbin), so
# this script is what stitches together a real working shell before handing
# off to `sync run`.
set -euo pipefail

REPO_DIR="/Users/zfrancis/Desktop/Personal/sloshbot"
cd "$REPO_DIR"

# Secrets/config live outside the repo's env (.env.sloshbot is gitignored —
# see .env.sloshbot.example for the template) since launchd has no way to
# pass them and this repo isn't the right place to commit them.
if [ -f "$REPO_DIR/.env.sloshbot" ]; then
  set -a  # export every var sourced below, so the `uv run` child process sees them
  source "$REPO_DIR/.env.sloshbot"
  set +a
fi

# launchd's PATH is bare, so `uv` (installed via homebrew or the standalone
# installer, never in launchd's default PATH) has to be added explicitly.
# Both locations are harmless to add even if only one exists on this Mac.
export PATH="/opt/homebrew/bin:$HOME/.local/bin:$PATH"

exec uv run python -m sync run

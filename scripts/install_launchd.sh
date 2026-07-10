#!/bin/bash
# Idempotent installer for the daily sync launchd agent. Safe to re-run any
# time the plist changes — it bootouts any existing registration before
# re-bootstrapping, rather than erroring on "already loaded".
set -euo pipefail

REPO_DIR="/Users/zfrancis/Desktop/Personal/sloshbot"
PLIST_SRC="$REPO_DIR/scripts/com.sloshbot.pipeline.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.sloshbot.pipeline.plist"
LOG_DIR="$HOME/Library/Logs/sloshbot"

mkdir -p "$LOG_DIR"

cp "$PLIST_SRC" "$PLIST_DST"

UID_NUM="$(id -u)"
# Tear down any prior registration first — bootstrap fails if the label is
# already loaded, so this makes re-running the installer (e.g. after editing
# the plist) safe instead of a manual "unload then reinstall" dance.
launchctl bootout "gui/$UID_NUM/com.sloshbot.pipeline" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DST"

echo "Installed com.sloshbot.pipeline -> $PLIST_DST"
echo "Scheduled to run daily at 7:30am (missed firings while asleep catch up on wake)."
echo
echo "Trigger a manual run right now:"
echo "  launchctl kickstart gui/$UID_NUM/com.sloshbot.pipeline"
echo
echo "Logs (stdout+stderr combined):"
echo "  $LOG_DIR/pipeline.log"

#!/usr/bin/env bash
# Fires when a fresh capture lands in the captures folder: extract, push to the
# private data repo, and kick a refresh so the site picks it up.
#
# Loaded by ~/Library/LaunchAgents/com.stateofsol.capture-watch.plist, which
# watches the folder rather than a clock — saving the payload is the trigger,
# so there is nothing to keep in sync with a schedule.

set -euo pipefail

REPO="${LLAMA_REPO:-$HOME/solana-dashboard}"
LOG="${LLAMA_LOG:-$HOME/Library/Logs/stateofsol-capture.log}"

exec >>"$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') capture detected ==="

cd "$REPO"

# launchd hands us a minimal PATH; gh and python3 live in the usual places.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"

set +e
tools/sync_local.sh
synced=$?
set -e

case "$synced" in
  0) ;;                                   # pushed something new
  2) echo "nothing new — skipping the refresh"; exit 0 ;;
  *) echo "sync failed — leaving the site on its previous data"; exit 1 ;;
esac

gh workflow run deploy.yml -f refresh=true
echo "refresh triggered"

#!/usr/bin/env bash
# Extract the saved DefiLlama captures and push them to the private data repo.
#
# The captures are DefiLlama's data, and solana-dashboard is a public repo, so
# they are kept out of it — committing them would leave a permanently
# downloadable copy of their dataset in git history, which is the same reason
# data.json is gitignored. They live in a private repo instead, and the build
# checks that out at deploy time.
#
# Run this after saving a fresh __NEXT_DATA__ payload:
#   tools/sync_local.sh
#
# Nothing here is scheduled and nothing fetches from DefiLlama — capturing the
# payload is a manual step you do in a browser.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAPTURES="${LLAMA_CAPTURES:-$HOME/Documents/defillama_data}"
DATA_REPO="${LLAMA_DATA_REPO:-$HOME/solana-dashboard-data}"

if [ ! -d "$DATA_REPO/.git" ]; then
  echo "no data repo at $DATA_REPO — git clone the private repo there first" >&2
  exit 1
fi

cd "$REPO"
built=()

# perps is the one that actually comes off the metered API. rwa is extracted
# too so a capture is on hand, but refresh_data.py will refuse it while the
# page only breaks out five of the ten chains that card shows.
for pair in "perps:next_data_latest.json" "rwa:rwa_next_data_latest.json"; do
  kind="${pair%%:*}"; file="$CAPTURES/${pair##*:}"
  if [ -f "$file" ]; then
    python3 tools/llama_local.py "$kind" "$file"
    built+=("$kind")
  else
    echo "  skip $kind — no capture at $file" >&2
  fi
done

if [ ${#built[@]} -eq 0 ]; then
  echo "nothing to sync" >&2
  exit 1
fi

mkdir -p "$DATA_REPO/local"
cp local/*.json "$DATA_REPO/local/"

cd "$DATA_REPO"
git add local
if git diff --cached --quiet; then
  echo "no change — captures already current"
  exit 0
fi
# Date the commit by the data, not the clock: a capture taken after midnight
# local time still describes the previous UTC day.
git commit -qm "captures through $(python3 -c "
import json,glob
print(max(json.load(open(f))['last'] for f in glob.glob('local/*.json')))")"
git push -q origin HEAD
echo "pushed $(git rev-parse --short HEAD) to $(git remote get-url origin)"

#!/usr/bin/env bash
# Open every page that needs capturing, in order.
#
# Save each one's __NEXT_DATA__ payload into the captures folder under the
# filename listed beside it. The folder watcher does everything after that.
#
# Eleven tabs is the price of keeping RWA off the API: its all-chains page
# buckets everything outside a top-N into "Others", so the ten chains have to
# come from their own pages. Skipping one is safe — refresh_data.py refuses a
# capture that drops a chain it is already publishing, and falls back instead.

set -euo pipefail

BASE="https://defillama.com"

open "$BASE/perps/chains"                 # -> next_data_latest.json

for slug in ethereum bsc solana avalanche arbitrum base polygon tron sui robinhood-chain; do
  open "$BASE/rwa/chain/$slug"            # -> rwa_chain_<slug>.json
done

cat <<'EOF'
Save each payload into ~/Documents/defillama_data as:

  perps/chains              -> next_data_latest.json
  rwa/chain/ethereum        -> rwa_chain_ethereum.json
  rwa/chain/bsc             -> rwa_chain_bsc.json
  rwa/chain/solana          -> rwa_chain_solana.json
  rwa/chain/avalanche       -> rwa_chain_avalanche.json
  rwa/chain/arbitrum        -> rwa_chain_arbitrum.json
  rwa/chain/base            -> rwa_chain_base.json
  rwa/chain/polygon         -> rwa_chain_polygon.json
  rwa/chain/tron            -> rwa_chain_tron.json
  rwa/chain/sui             -> rwa_chain_sui.json
  rwa/chain/robinhood-chain -> rwa_chain_robinhood-chain.json
EOF

# Front-end captures

Perps volume comes from a saved capture of DefiLlama's own page rather than
their metered API. RWA has a capture too, but the dashboard still uses the API
for it — see below.

## Why captures at all

`/perps/chains` server-renders its **entire** history into the page — all ~2,000
days for 109 chains, on every load. Not a delta. So one capture is a complete
history, and a missed day costs nothing: the next capture backfills it. That is
why `refresh_data.py` replaces the block wholesale instead of appending.

Wholesale also avoids a real problem. Ten of the eleven chains match the paid
endpoint to four decimals, but **Ethereum reads ~0.87 of it** — the two sources
disagree about which venues count as Ethereum perps. Splicing one onto the
other would put a visible step in that chart.

## Daily routine

1. Open <https://defillama.com/perps/chains>, save the `__NEXT_DATA__` payload
   to `~/Documents/defillama_data/next_data_latest.json`
2. Run:

   ```bash
   tools/sync_local.sh
   ```

That extracts the eleven chains we publish, writes a compact file, and pushes it
to the private data repo. The next build picks it up.

Skip a day and nothing breaks — the capture carries full history, and if it goes
stale past yesterday `refresh_data.py` says so and falls back to the API.

## Why a private repo

These are DefiLlama's data and `solana-dashboard` is public. Committing captures
there would leave a permanently downloadable copy of their dataset in git
history — the same reason `data.json` is gitignored. They live in
`phummy1337/solana-dashboard-data` (private) and the build checks that out.

## One-off setup

Already done, recorded here for when it needs redoing.

The build reads the private repo with a **read-only deploy key**, not a PAT — a
deploy key is scoped to one repository and cannot reach anything else in the
account, and it does not expire:

```bash
ssh-keygen -t ed25519 -f data_key -N "" -C "stateofsol-build (read-only deploy key)"
gh api repos/phummy1337/solana-dashboard-data/keys \
  -f title="stateofsol build (read-only)" -f key="$(cat data_key.pub)" -F read_only=true
gh secret set DATA_REPO_SSH_KEY -R phummy1337/solana-dashboard < data_key
rm -f data_key data_key.pub
```

If the key is ever revoked the checkout step is `continue-on-error`, so the
build carries on and perps falls back to the metered API. It degrades, it does
not break.

## RWA

`tools/llama_local.py rwa` works, but the RWA page groups everything outside its
top-N into "Others" — it breaks out five of the ten chains that card shows
(missing base, polygon, robinhood, sui, tron). Its numbers also run 0.77–0.92 of
the API's, so it is measuring something adjacent, not the same series.

`refresh_data.py` therefore refuses an RWA capture that omits chains already
published, and falls back to the API. The extractor still runs so a capture is
on hand if DefiLlama ever breaks out more chains — at which point the guard
passes on its own and RWA comes off the API with no code change.

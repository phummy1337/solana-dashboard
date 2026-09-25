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

**Save the payload. That is the whole job.**

`com.stateofsol.capture-open` opens all eleven pages at 08:30 each morning
(`tools/open_captures.sh`, which also prints the filename each one wants). Save
their `__NEXT_DATA__` payloads into `~/Documents/defillama_data` and stop there —
`com.stateofsol.capture-watch` watches that folder and does the rest: extract,
push to the private data repo, trigger a refresh.

| Page | Save as |
|---|---|
| `/perps/chains` | `next_data_latest.json` |
| `/rwa/chain/<slug>` × 10 | `rwa_chain_<slug>.json` |

Slugs: `ethereum bsc solana avalanche arbitrum base polygon tron sui robinhood-chain`

Capturing is deliberately manual. DefiLlama puts a Cloudflare challenge in front
of those pages, and clearing it unattended is the thing the challenge exists to
stop, so nothing here fetches from them — a browser you are sitting at does.

Log: `~/Library/Logs/stateofsol-capture.log`

Skip a day and nothing breaks — the capture carries full history, and if it goes
stale past yesterday `refresh_data.py` says so and falls back to the API. Saving
the same day twice is a no-op: the sync compares the data, not the file, so it
will not spend a build re-publishing an identical capture.

To run it by hand, or to sync an RWA capture too:

```bash
tools/sync_local.sh
```

The agents live in `~/Library/LaunchAgents/com.stateofsol.capture-{open,watch}.plist`.
Unload either with `launchctl bootout gui/$(id -u)/com.stateofsol.capture-watch`.

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

## Why RWA takes ten captures

The all-chains `/rwa` page groups everything outside a top-N into "Others", so
it only ever breaks out five of the ten chains this card shows — base, polygon,
robinhood, sui and tron vanish into the bucket. Each chain's own page carries
its full history under a single "Total Active AUM" column, so `rwa-chains` reads
one capture per chain and loses nothing.

Two dead ends, recorded so they are not re-tried:

- **The `assets` array** in the all-chains payload reproduces DefiLlama's
  headline total *exactly* ($37,955,365,140) once stablecoins and governance
  tokens are filtered out — but its per-chain breakdown does not reconcile with
  the chain series (0.58x to 1.32x, tron 112x), and it is a snapshot with no
  history at all.
- **`rwa_chain_mcap_history.csv`** is a flattened dump of all 6,037 assets. Its
  `chain` column holds JSON arrays and `_date` holds asset issuance dates; there
  are zero rows carrying a date and an activeMcap together.

Front-end values run ~0.92 of the API's, consistently, so the series is replaced
wholesale like perps rather than spliced. `refresh_data.py` refuses any capture
set that omits a chain it is already publishing and falls back to the API, so a
half-finished folder degrades instead of shrinking the card.

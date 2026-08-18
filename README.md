# Solana Dashboard

SOL returns, 2026 year-to-date network and market activity, and live chain health.
Visual language follows [solanarainbow.com](https://solanarainbow.com) — same tokens,
type and chrome, so the two sites read as siblings.

## Layout

1. **Price & Returns** — spot price, 3-month, YTD, 1-year and 5-year returns.
2. **Daily Snapshot / Year-to-Date** — twelve tiles covering transactions, TPS,
   revenue, stablecoin supply, DEX volume, average fee, validators, volume vs.
   average, tokenized equity supply and volume, traders, and perps volume.
   A toggle switches between the latest complete day (default) and YTD totals.
3. **SOL Price History** — 3M/YTD/1Y/5Y/All with a linear/log toggle, plus a
   **SOL Rainbow** overlay reproducing solanarainbow.com's power-law bands
   (constants lifted from its source; fair-value and top-band values verified
   identical). Enabling it defaults the scale to log, like the source site.
4. **Activity Trends** — daily sparklines for the six series above.
5. **Live Network Health** — epoch progress, 12h throughput, stake and yield,
   and top-validator stake distribution.

## Data sources

| Section | Source | Refresh |
| --- | --- | --- |
| Returns, all YTD tiles, activity trends | Blockworks API, pre-aggregated into `data.json` | `refresh_data.py`, twice daily via Actions |
| DeFi TVL | DefiLlama (`api.llama.fi`), pre-aggregated into `data.json` | same refresh |
| Epoch, TPS, validators, stake, version | Public Solana RPC, client-side | every 30s in the browser |
| Total supply (for staked %) | CoinGecko, client-side | on load |

### Why data.json exists

The Blockworks key must never reach the browser — a static page cannot hide it.
`refresh_data.py` runs server-side, so the key lives only in
`BLOCKWORKS_API_KEY` (a GitHub Actions secret locally an env var). It also
pre-computes the YTD aggregates, which otherwise means pulling ~30k paginated
rows in every visitor's browser.

Live chain data needs no key, so it stays client-side and updates in real time.

## Running locally

```bash
BLOCKWORKS_API_KEY=your_key python3 refresh_data.py
python3 -m http.server 8931
```

Then open <http://localhost:8931>. `pip install certifi` if you hit
`CERTIFICATE_VERIFY_FAILED` (the python.org macOS builds ship no root certificates).

## Notes on the numbers

- **Revenue** is Blockworks' Network REV (base fees, priority fees, Jito tips).
  Chart 103 is denominated in **SOL**; the script converts at each day's close.
  Verified against `transaction-fee-total-usd` to within 0.03%.
- **Average transaction fee** is total YTD fees ÷ total YTD transactions, not a
  mean of daily averages.
- **Average TPS** includes vote transactions.
- **Perps volume** sums only rows where `symbol == "Total"`; the series also
  carries per-symbol rows that would double count.
- **Tokenized equity volume** is summed across issuers per day.
- **Estimated staking yield** is inflation ÷ staking ratio, before MEV and
  validator commission.
- YTD covers 1 January to the latest complete day and is **not** annualised.

## Blockworks resources used

Base `https://api.blockworks.com`, auth via `x-api-key`.

- `/v1/assets/solana/price`
- `/v1/metrics/{token-price-usd,transaction-total,transaction-fee-total-usd,dex-spot-volume-total-usd,stablecoin-supply-total-usd}?project=solana`
- Charts `103` (Network REV), `9185` (Daily Active Traders), `8907` (Perp DEXs —
  Futures Notional Volume), `10634` (Tokenized Equities Volume by Token Issuer),
  `10631` (Tokenized Equities Supply)

Chart ids and titles are recorded in `data.json` under `sources` so a renamed or
re-pointed chart shows up on the next refresh.

## Caveats

- The public RPC (`solana-rpc.publicnode.com`) is rate-limited and disables
  `getSupply`; swap in Helius/Triton via `CONFIG.RPC` for production.
  `api.mainnet-beta.solana.com` will not work — it returns 403 to browser origins.
- `getVoteAccounts` returns ~300KB per call, refreshed every 30s.

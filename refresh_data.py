#!/usr/bin/env python3
"""Build data.json for the Solana dashboard.

Runs server-side (locally or in CI) so the Blockworks API key never reaches the
browser. Live keyless data — TPS, epoch, validators — is fetched client-side
instead and is deliberately not duplicated here.

Usage:
    BLOCKWORKS_API_KEY=... python3 refresh_data.py
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# The python.org macOS builds ship no system roots, so HTTPS fails with
# CERTIFICATE_VERIFY_FAILED. Prefer certifi's bundle when it is installed.
try:
    import certifi

    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

BW_BASE = "https://api.blockworks.com"
BW_KEY = os.environ.get("BLOCKWORKS_API_KEY", "").strip()
OUT = Path(__file__).parent / "data.json"

YEAR = date.today().year
YTD_START = date(YEAR, 1, 1)
PREV_YEAR_END = f"{YEAR - 1}-12-31"

# Blockworks Analytics chart ids, resolved from /v1/charts?search=... . Titles are
# recorded so a renamed or re-pointed chart is obvious on the next refresh.
CHARTS = {
    "rev":        (103,   "Solana: Network REV"),
    "traders":    (9185,  "Solana: Daily Active Traders"),
    "perps":      (8907,  "Solana: Perp DEXs — Futures Notional Volume"),
    "tokeq_vol":  (10634, "Solana: Tokenized Equities Volume by Token Issuer"),
    "tokeq_sup":  (10631, "Solana: Tokenized Equities Supply"),
    "tokeq_chain": (6874, "Spot DEXs: Tokenized Equities Volume by Blockchain"),
}

# Comparison sets for the Activity Trends charts: Solana vs major L1/L2s.
# Per-metric lists because one invalid slug 400s the whole Blockworks request —
# tron/polygon/aptos have no dex-spot-volume series, for example.
CHAINS_ALL = ["solana", "ethereum", "base", "arbitrum", "bnb", "avalanche",
              "sui", "tron", "hyperevm", "polygon"]
CHAINS_DEX = ["solana", "ethereum", "base", "arbitrum", "bnb", "avalanche",
              "sui", "hyperevm"]
LLAMA_SLUGS = {"solana": "Solana", "ethereum": "Ethereum", "base": "Base",
               "arbitrum": "Arbitrum", "bnb": "BSC", "avalanche": "Avalanche",
               "sui": "Sui", "tron": "Tron", "hyperevm": "Hyperliquid",
               "polygon": "Polygon"}

warnings: list[str] = []


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"  !! {msg}", file=sys.stderr)


# The edge in front of the API answers 403 to the default Python-urllib agent.
UA = "solana-dashboard/1.0 (+refresh_data.py)"


def get(url: str, headers: dict | None = None, tries: int = 3) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **(headers or {})})
    last = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=90, context=_SSL_CTX) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001 - retry any transport/parse failure
            last = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET {url.split('?')[0]} failed: {last}")


def bw(path: str, **params) -> dict | list:
    if not BW_KEY:
        raise RuntimeError("BLOCKWORKS_API_KEY is not set")
    url = f"{BW_BASE}/{path.lstrip('/')}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return get(url, {"x-api-key": BW_KEY})


def metric(slug: str, project: str = "solana") -> dict[str, float]:
    """Return {date: value} for a /v1/metrics series."""
    d = bw(f"v1/metrics/{slug}", project=project)
    rows = d.get(project) or []
    return {r["date"]: r["value"] for r in rows if r.get("value") is not None}


def chart_rows(chart_id: int, stop_before: str | None = None, page_size: int = 5000) -> list[dict]:
    """Fetch chart rows newest-first, stopping early once past stop_before."""
    out: list[dict] = []
    page = 1
    while True:
        d = bw(f"v1/charts/{chart_id}/data", limit=page_size, page=page)
        rows = d.get("data") or []
        out.extend(rows)
        total = d.get("total", 0)
        if not rows or len(out) >= total:
            break
        if stop_before:
            oldest = min((row_date(r) or "9999") for r in rows)
            if oldest < stop_before:
                break
        page += 1
        if page > 40:  # safety valve; 40 * 5000 rows is far beyond any chart here
            warn(f"chart {chart_id}: stopped paginating at page {page}")
            break
    return out


def row_date(r: dict) -> str | None:
    """Charts label their date column inconsistently — normalise to YYYY-MM-DD."""
    for k in ("date", "dt", "block_date", "day", "timestamp"):
        if k in r and r[k]:
            return str(r[k])[:10]
    return None


def ytd(series: dict[str, float]) -> dict[str, float]:
    return {d: v for d, v in series.items() if d >= YTD_START.isoformat()}


def at_or_before(series: dict[str, float], target: date, window: int = 10) -> float | None:
    """Nearest value at or before target, tolerating gaps in the series."""
    for back in range(window + 1):
        key = (target.fromordinal(target.toordinal() - back)).isoformat()
        if key in series:
            return series[key]
    return None


def main() -> int:
    print("Refreshing Solana dashboard data...")
    data: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "year": YEAR,
        "stats": {},
        "series": {},
        "sources": {"blockworks_charts": {k: {"id": v[0], "title": v[1]} for k, v in CHARTS.items()}},
    }
    stats = data["stats"]
    # Latest-complete-day readings for the dashboard's Daily view. Each value is
    # taken at its own series' newest date; Blockworks lands them together, so
    # in practice they share the date recorded in daily["as_of"].
    daily: dict = {}

    # ---------------------------------------------------------------- price
    prices: dict[str, float] = {}
    try:
        prices = metric("token-price-usd")
        print(f"  price series: {len(prices)} days")
    except Exception as e:  # noqa: BLE001
        warn(f"price series: {e}")

    spot = None
    try:
        spot = bw("v1/assets/solana/price").get("usd")
        print(f"  spot price: ${spot}")
    except Exception as e:  # noqa: BLE001
        warn(f"spot price: {e}")

    # 24h return from the rolling-24h OHLCV open vs the live spot quote.
    try:
        o = bw("v1/assets/solana/ohlcv")
        cur = spot or o.get("close")
        if o.get("open") and cur:
            stats["return_24h"] = (cur / o["open"] - 1) * 100
            print(f"  24h return: {stats['return_24h']:+.2f}%")
    except Exception as e:  # noqa: BLE001
        warn(f"ohlcv 24h: {e}")

    if prices:
        latest_date = max(prices)
        # Prefer the live spot quote; fall back to the last daily close.
        current = spot if spot else prices[latest_date]
        stats["price"] = current
        stats["price_as_of"] = latest_date
        # Full history, so the chart's "All" range is honest — it only costs a
        # few hundred extra points over a 5-year clip.
        data["series"]["price"] = [{"d": d, "v": prices[d]} for d in sorted(prices)]

        today = date.today()
        windows = {
            "return_3m": at_or_before(prices, date.fromordinal(today.toordinal() - 91)),
            "return_ytd": prices.get(PREV_YEAR_END) or at_or_before(prices, date(YEAR - 1, 12, 31)),
            "return_1y": at_or_before(prices, date.fromordinal(today.toordinal() - 365)),
            "return_5y": at_or_before(prices, date.fromordinal(today.toordinal() - 1826)),
        }
        for k, base in windows.items():
            stats[k] = ((current / base) - 1) * 100 if base else None
            if base is None:
                warn(f"{k}: no baseline price found")

    # ------------------------------------------------------- network volume
    def sum_metric(slug: str, key: str, also_series: bool = False) -> dict[str, float]:
        try:
            s = metric(slug)
            y = ytd(s)
            stats[key] = sum(y.values())
            stats[f"{key}_days"] = len(y)
            if also_series:
                data["series"][key] = [{"d": d, "v": s[d]} for d in sorted(s) if d >= f"{YEAR - 5}-01-01"]
            print(f"  {slug}: YTD {stats[key]:,.0f} over {len(y)} days")
            return s
        except Exception as e:  # noqa: BLE001
            warn(f"{slug}: {e}")
            stats[key] = None
            return {}

    txns = sum_metric("transaction-total", "ytd_transactions", also_series=True)
    fees_usd = sum_metric("transaction-fee-total-usd", "ytd_fees_usd")
    dex = sum_metric("dex-spot-volume-total-usd", "ytd_dex_volume", also_series=True)

    if txns:
        d0 = max(txns)
        daily["as_of"] = d0
        daily["transactions"] = txns[d0]
        daily["tps"] = txns[d0] / 86400
    if fees_usd:
        d0 = max(fees_usd)
        daily["fees_usd"] = fees_usd[d0]
        if txns.get(d0):
            daily["fee_avg"] = fees_usd[d0] / txns[d0]
    if dex:
        daily["dex_volume"] = dex[max(dex)]

    # Average TPS across the year so far, measured against elapsed wall-clock
    # rather than a nominal 365 days.
    if txns and stats.get("ytd_transactions"):
        days = len(ytd(txns))
        if days:
            stats["avg_tps_ytd"] = stats["ytd_transactions"] / (days * 86400)

    # Volume-weighted, not a mean of daily averages.
    if stats.get("ytd_fees_usd") and stats.get("ytd_transactions"):
        stats["avg_fee_ytd"] = stats["ytd_fees_usd"] / stats["ytd_transactions"]

    if stats.get("ytd_dex_volume") and stats.get("ytd_dex_volume_days"):
        stats["avg_daily_dex_volume_ytd"] = stats["ytd_dex_volume"] / stats["ytd_dex_volume_days"]

    # ----------------------------------------------------------- DeFi TVL
    # Blockworks carries no Solana chain-TVL series, so this one comes from
    # DefiLlama (keyless). TVL is a level, not a flow: the tile shows the
    # current reading in both modes, with the YTD change as context.
    try:
        rows = get("https://api.llama.fi/v2/historicalChainTvl/Solana")
        tvl = {
            datetime.fromtimestamp(r["date"], tz=timezone.utc).date().isoformat(): r["tvl"]
            for r in rows if r.get("tvl") is not None
        }
        if tvl:
            stats["defi_tvl"] = tvl[max(tvl)]
            stats["defi_tvl_as_of"] = max(tvl)
            ytd_open = tvl.get(PREV_YEAR_END) or at_or_before(tvl, date(YEAR - 1, 12, 31), 30)
            if ytd_open:
                stats["defi_tvl_ytd_change"] = ((stats["defi_tvl"] / ytd_open) - 1) * 100
            data["series"]["defi_tvl"] = [
                {"d": d, "v": tvl[d]} for d in sorted(tvl) if d >= f"{YEAR - 5}-01-01"
            ]
            print(f"  DeFi TVL: ${stats['defi_tvl']:,.0f} ({max(tvl)})")
    except Exception as e:  # noqa: BLE001
        warn(f"defillama tvl: {e}")

    # ---------------------------------------------------- stablecoin supply
    try:
        s = metric("stablecoin-supply-total-usd")
        if s:
            stats["stablecoin_supply"] = s[max(s)]
            stats["stablecoin_supply_as_of"] = max(s)
            ytd_open = s.get(PREV_YEAR_END) or at_or_before(s, date(YEAR - 1, 12, 31), 30)
            if ytd_open:
                stats["stablecoin_supply_ytd_change"] = ((stats["stablecoin_supply"] / ytd_open) - 1) * 100
            data["series"]["stablecoin_supply"] = [
                {"d": d, "v": s[d]} for d in sorted(s) if d >= f"{YEAR - 5}-01-01"
            ]
            print(f"  stablecoin supply: ${stats['stablecoin_supply']:,.0f}")
    except Exception as e:  # noqa: BLE001
        warn(f"stablecoin-supply-total-usd: {e}")

    # ------------------------------------------------------------ REV (SOL)
    # Chart 103 is denominated in SOL, verified against transaction-fee-total-usd:
    # (vote + base + priority) fees x daily close matched the USD metric to 0.03%.
    try:
        rows = chart_rows(*[CHARTS["rev"][0]], stop_before=f"{YEAR}-01-01")
        rev_by: dict[str, float] = {}
        for r in rows:
            d = row_date(r)
            if not d or d < YTD_START.isoformat() or d in rev_by:
                continue
            v = r.get("rev")
            if v is not None:
                rev_by[d] = v
        rev_sol = sum(rev_by.values())
        rev_usd = sum(v * prices.get(d, prices.get(max(prices)) if prices else 0)
                      for d, v in rev_by.items())
        stats["ytd_revenue_sol"] = rev_sol
        stats["ytd_revenue_usd"] = rev_usd
        if rev_by:
            d0 = max(rev_by)
            daily["revenue_sol"] = rev_by[d0]
            daily["revenue_usd"] = rev_by[d0] * prices.get(d0, prices.get(max(prices)) if prices else 0)
        print(f"  YTD REV: {rev_sol:,.0f} SOL / ${rev_usd:,.0f} over {len(rev_by)} days")
    except Exception as e:  # noqa: BLE001
        warn(f"network REV chart: {e}")

    # -------------------------------------------------------- active traders
    # Full history for the trend chart's longer ranges; stats stay YTD.
    series_since = f"{YEAR - 5}-01-01"
    try:
        rows = chart_rows(CHARTS["traders"][0])
        vals = {}
        for r in rows:
            d = row_date(r)
            if d and r.get("unique_traders") is not None:
                vals[d] = r["unique_traders"]
        y = ytd(vals)
        if y:
            stats["avg_daily_traders_ytd"] = sum(y.values()) / len(y)
            daily["traders"] = vals[max(vals)]
            print(f"  avg daily traders YTD: {stats['avg_daily_traders_ytd']:,.0f} over {len(y)} days")
        if vals:
            data["series"]["traders"] = [{"d": d, "v": v} for d, v in sorted(vals.items()) if d >= series_since]
    except Exception as e:  # noqa: BLE001
        warn(f"daily active traders chart: {e}")

    # ----------------------------------------------------------- perps volume
    try:
        rows = chart_rows(CHARTS["perps"][0])
        # The series carries one row per symbol plus a rolled-up "Total" row;
        # summing everything would double count.
        vals = {}
        for r in rows:
            d = row_date(r)
            if d and r.get("symbol") == "Total" and r.get("vol_totals") is not None:
                vals[d] = r["vol_totals"]
        y = ytd(vals)
        if y:
            stats["ytd_perps_volume"] = sum(y.values())
            stats["ytd_perps_days"] = len(y)
            daily["perps_volume"] = vals[max(vals)]
            print(f"  YTD perps volume: ${stats['ytd_perps_volume']:,.0f} over {len(y)} days")
        else:
            warn("perps chart: no rows with symbol='Total' in YTD range")
        if vals:
            data["series"]["perps"] = [{"d": d, "v": v} for d, v in sorted(vals.items()) if d >= series_since]
    except Exception as e:  # noqa: BLE001
        warn(f"perps chart: {e}")

    # ------------------------------------------------------ tokenized equity
    try:
        rows = chart_rows(CHARTS["tokeq_vol"][0])
        vals: dict[str, float] = {}
        for r in rows:
            d = row_date(r)
            if d and r.get("volume_usd") is not None:
                # Rows are per-issuer, so accumulate rather than assign.
                vals[d] = vals.get(d, 0) + r["volume_usd"]
        y = ytd(vals)
        if y:
            stats["ytd_tokenized_equity_volume"] = sum(y.values())
            daily["tokenized_equity_volume"] = vals[max(vals)]
            print(f"  YTD tokenized equity volume: ${stats['ytd_tokenized_equity_volume']:,.0f}")
        if vals:
            data["series"]["tokenized_equity_volume"] = [{"d": d, "v": v} for d, v in sorted(vals.items()) if d >= series_since]
    except Exception as e:  # noqa: BLE001
        warn(f"tokenized equity volume chart: {e}")

    try:
        rows = chart_rows(CHARTS["tokeq_sup"][0])
        supply = {}
        for r in rows:
            d = row_date(r)
            v = r.get("circulating_supply_usd")
            if d and v is not None:
                supply[d] = v
        if supply:
            stats["tokenized_equity_supply"] = supply[max(supply)]
            stats["tokenized_equity_as_of"] = max(supply)
            data["series"]["tokenized_equity_supply"] = [{"d": d, "v": v} for d, v in sorted(supply.items())]
            print(f"  tokenized equity supply: ${stats['tokenized_equity_supply']:,.0f} ({max(supply)})")
        else:
            warn("tokenized equity supply chart: all values null")
    except Exception as e:  # noqa: BLE001
        warn(f"tokenized equity supply chart: {e}")

    # ------------------------------------------------- cross-chain comparisons
    # Multi-project series for the trend charts. Kept separate from "series"
    # so the single-chain cards stay untouched.
    compare: dict = {}
    since = f"{YEAR - 5}-01-01"

    # A day still in progress reports a fraction of its true value, which draws
    # a false cliff at the right edge — chains publish at different times, so
    # exclude today outright rather than trusting each series' last point.
    today_utc = datetime.now(timezone.utc).date().isoformat()

    # Blockworks intermittently returns an empty array for a chain that
    # normally has data (seen on stablecoin supply for ethereum/polygon).
    # Carry the previous refresh's series forward so a card doesn't silently
    # lose a chain — but only if it is still recent, and always warn.
    prev_compare: dict = {}
    try:
        prev_compare = json.loads(OUT.read_text()).get("compare") or {}
    except Exception:  # noqa: BLE001 - first run or unreadable
        pass

    def compare_metric(slug: str, key: str, chains: list[str]) -> None:
        try:
            d = bw(f"v1/metrics/{slug}", project=",".join(chains))
            out = {}
            for chain in chains:
                rows = d.get(chain) or []
                pts = [{"d": r["date"], "v": r["value"]} for r in sorted(rows, key=lambda r: r["date"])
                       if r.get("value") is not None and since <= r["date"] < today_utc]
                if pts:
                    out[chain] = pts
            if out:
                # Chains publish on their own schedules. A chain sitting ahead
                # of the pack has written a day the others haven't finished —
                # and that early value is a fraction of its true total, drawing
                # a false cliff. Trim everything past the common frontier (the
                # median of each chain's last date).
                lasts = sorted(v[-1]["d"] for v in out.values())
                frontier = lasts[len(lasts) // 2]
                stale_cutoff = (date.fromisoformat(frontier) - timedelta(days=4)).isoformat()
                for chain in chains:
                    if chain in out:
                        continue
                    old = (prev_compare.get(key) or {}).get(chain) or []
                    if old and old[-1]["d"] >= stale_cutoff:
                        out[chain] = old
                        warn(f"{slug}: empty for {chain}, reused previous series "
                             f"(through {old[-1]['d']})")
                for chain in list(out):
                    out[chain] = [p for p in out[chain] if p["d"] <= frontier]
                    if not out[chain]:
                        del out[chain]
            if out:
                compare[key] = out
                print(f"  compare {slug}: " + ", ".join(f"{c}:{len(v)}" for c, v in out.items())
                      + f" (frontier {frontier})")
        except Exception as e:  # noqa: BLE001
            warn(f"compare {slug}: {e}")

    compare_metric("rev-usd", "rev", CHAINS_ALL)
    compare_metric("active-address-total", "active_addresses", CHAINS_ALL)
    compare_metric("transaction-succeed-total", "succeeded", CHAINS_ALL)
    compare_metric("transaction-total", "transactions", CHAINS_ALL)
    compare_metric("dex-spot-volume-total-usd", "dex_volume", CHAINS_DEX)
    compare_metric("stablecoin-supply-total-usd", "stablecoin_supply", CHAINS_ALL)

    # ------------------------------------------------- fee stability (FSR)
    # DFDV's Fee Stability Ratio: 1 / (median fee x median-fee volatility),
    # volatility taken as the 30-day rolling stdev of the daily median fee.
    # Tron is excluded — its median fee is 0 (bandwidth model), so FSR blows up.
    # Avalanche is excluded per Pete: not a comparison he wants on this card.
    FEE_CHAINS = [c for c in CHAINS_ALL if c not in ("tron", "avalanche")]
    try:
        d = bw("v1/metrics/transaction-fee-med-usd", project=",".join(FEE_CHAINS))
        fee_out: dict = {}
        vol_out: dict = {}
        fsr_out: dict = {}
        for chain in FEE_CHAINS:
            rows = sorted((r for r in (d.get(chain) or []) if r.get("value") is not None),
                          key=lambda r: r["date"])
            vals = [r["value"] for r in rows]
            fees, vols, fsrs = [], [], []
            for i, r in enumerate(rows):
                if r["date"] < since:
                    continue
                w = vals[max(0, i - 29):i + 1]
                m = sum(w) / len(w)
                sd = (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5
                fees.append({"d": r["date"], "v": vals[i]})
                if sd > 0:
                    vols.append({"d": r["date"], "v": sd})
                    if vals[i] > 0:
                        fsrs.append({"d": r["date"], "v": 1 / (vals[i] * sd)})
            if fees:
                fee_out[chain] = fees
            if vols:
                vol_out[chain] = vols
            if fsrs:
                fsr_out[chain] = fsrs
        if fsr_out:
            compare["fee_median"] = fee_out
            compare["fee_vol"] = vol_out
            compare["fsr"] = fsr_out
            print("  compare fee/FSR: " + ", ".join(f"{c}:{len(v)}" for c, v in fsr_out.items()))
    except Exception as e:  # noqa: BLE001
        warn(f"fee stability: {e}")

    # Tokenized-asset volume by blockchain (chart 6874). Equities-only isn't
    # broken out for most of the history, so approximate it as total tokenized
    # asset volume minus the commodities category where that's reported.
    try:
        rows = chart_rows(CHARTS["tokeq_chain"][0])
        chain_map = {"solana": "solana", "ethereum": "ethereum", "base": "base",
                     "arbitrum": "arbitrum", "bnb": "bnb", "bsc": "bnb",
                     "avalanche": "avalanche", "sui": "sui", "tron": "tron",
                     "hyperevm": "hyperevm", "polygon": "polygon"}
        by: dict = {}
        for r in rows:
            d0 = row_date(r)
            ch = chain_map.get((r.get("blockchain") or "").lower())
            v = r.get("tokenizedasset_volume_usd")
            if not d0 or not ch or v is None:
                continue
            v = max(0, v - (r.get("category_commodities_volume_usd") or 0))
            by.setdefault(ch, {})
            by[ch][d0] = by[ch].get(d0, 0) + v
        out = {c: [{"d": d0, "v": v} for d0, v in sorted(pts.items()) if d0 >= since]
               for c, pts in by.items()}
        out = {c: p for c, p in out.items() if p}
        if out:
            compare["tokenized_equity_volume"] = out
            print("  compare tokenized-equity: " + ", ".join(f"{c}:{len(v)}" for c, v in out.items()))
    except Exception as e:  # noqa: BLE001
        warn(f"compare tokenized equity chart: {e}")

    try:
        out = {}
        for chain, slug in LLAMA_SLUGS.items():
            rows = get(f"https://api.llama.fi/v2/historicalChainTvl/{slug}")
            pts = [{"d": datetime.fromtimestamp(r["date"], tz=timezone.utc).date().isoformat(),
                    "v": r["tvl"]}
                   for r in rows if r.get("tvl")]
            pts = [p for p in pts if p["d"] >= since]
            if pts:
                out[chain] = pts
        if out:
            compare["defi_tvl"] = out
            print("  compare defi-tvl: " + ", ".join(f"{c}:{len(v)}" for c, v in out.items()))
    except Exception as e:  # noqa: BLE001
        warn(f"compare defillama tvl: {e}")

    # -------------------------------------------------- yield-bearing stables
    # Top single-asset stablecoin yield products on Solana from DefiLlama's
    # yields API (chain=Solana, stablecoin, TVL > $10M, positive APY; LP pairs
    # excluded, one row per project+symbol keeping the deepest market).
    # apyUSD is pinned: its yield comes from the Apyx protocol pool on
    # DefiLlama and its Solana TVL from the apyUSD supply Worker ($1 peg).
    APYX_POOL_ID = "cb6139f9-4a68-4efd-8245-0312a92aee55"
    try:
        pools = get("https://yields.llama.fi/pools")["data"]
        best: dict = {}
        for p in pools:
            sym = (p.get("symbol") or "").upper()
            if (p.get("chain") != "Solana" or not p.get("stablecoin") or "-" in sym
                    or (p.get("tvlUsd") or 0) < 10e6 or (p.get("apy") or 0) <= 0):
                continue
            k = (p["project"], sym)
            if k not in best or p["tvlUsd"] > best[k]["tvlUsd"]:
                best[k] = p
        # Platform homepages, for the linked Platform column. Referral query
        # strings (DefiLlama tags some URLs) are stripped.
        proto_urls: dict = {}
        try:
            proto_urls = {p.get("slug"): (p.get("url") or "").split("?")[0]
                          for p in get("https://api.llama.fi/protocols")}
        except Exception as e:  # noqa: BLE001
            warn(f"protocol urls: {e}")

        # Product-wide TVL: the same project+symbol summed across every chain.
        totals: dict = {}
        for p in pools:
            k = (p.get("project"), (p.get("symbol") or "").upper())
            totals[k] = totals.get(k, 0) + (p.get("tvlUsd") or 0)

        # Token logos via CoinGecko search, exact-symbol match only. Falls back
        # to the previous refresh's logo when the search is rate-limited, so a
        # 429 never blanks icons that were already resolved.
        prev_logos: dict = {}
        try:
            prev = json.loads(OUT.read_text())["yield_products"]
            for it in (prev.get("items") or []):
                if it.get("logo"):
                    prev_logos[it["symbol"]] = it["logo"]
        except Exception:  # noqa: BLE001 - first run or old schema
            pass
        logo_cache: dict = {}

        def logo_for(sym: str):
            if sym in logo_cache:
                return logo_cache[sym]
            url = None
            try:
                res = get(f"https://api.coingecko.com/api/v3/search?query={sym}")
                for c in res.get("coins", []):
                    if (c.get("symbol") or "").upper() == sym.upper():
                        url = c.get("large") or c.get("thumb")
                        break
            except Exception as e:  # noqa: BLE001
                url = prev_logos.get(sym)
                warn(f"logo search {sym}: {e}" + (" — reusing previous logo" if url else ""))
            logo_cache[sym] = url
            return url

        items = [{
            "symbol": s, "project": proj, "tvl": round(p["tvlUsd"]),
            "tvl_total": round(totals.get((proj, s), p["tvlUsd"])),
            "apy": round(p.get("apy") or 0, 2), "apy30d": round(p.get("apyMean30d") or 0, 2),
            "url": proto_urls.get(proj) or None,
            "logo": logo_for(s),
        } for (proj, s), p in best.items()]
        items.sort(key=lambda x: -x["apy30d"])

        apyx_pool = next((p for p in pools if p.get("pool") == APYX_POOL_ID), None)
        apyusd = None
        if apyx_pool:
            apyusd = {
                "symbol": "apyUSD", "project": "apyx-protocol",
                "apy": round(apyx_pool.get("apy") or 0, 2),
                "apy30d": round(apyx_pool.get("apyMean30d") or 0, 2),
                "tvl_protocol": round(apyx_pool.get("tvlUsd") or 0),
                "url": proto_urls.get("apyx-protocol") or "https://app.apyx.fi",
                "logo": "https://apyx-token-logos.apxusd-supply-1337.workers.dev/apyusd-256.png",
            }
            # First-party yield: Apyx's own API is the source of truth for
            # apyUSD's APY (DefiLlama's independent calc runs ~1pp different
            # from what apyx.fi displays). DefiLlama stays as fallback.
            try:
                aj = get("https://api.apyx.fi/v1/protocol/apyUSD",
                         {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                                        "Chrome/126.0.0.0 Safari/537.36"})["data"]
                apyusd["apy"] = round(float(aj["apy"]), 2)
                apyusd["apy30d"] = round(float(aj["apy30d"]), 2)
                print(f"  apyx first-party APY: {apyusd['apy']}% (30d {apyusd['apy30d']}%)")
            except Exception as e:  # noqa: BLE001
                warn(f"apyx protocol api: {e} — using DefiLlama APY")

            # Preferred TVL: "Protocol Reserves" from Apyx's Accountable
            # proof-of-solvency feed (total reserves minus protocol-owned
            # liquidity and inventory — matches the figure the page displays).
            # The edge 403s non-browser user agents, hence the UA override.
            try:
                acc = get("https://api.accountable.apyx.fi/dashboard",
                          {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                                         "Chrome/126.0.0.0 Safari/537.36",
                           "Referer": "https://accountable.apyx.fi/"})
                rsv = acc["data"]["reserves"]
                apyusd["tvl_reserves"] = round(
                    rsv["total_reserves"]["value"] - rsv["pol"] - rsv["inventory"])
                print(f"  apyx protocol reserves: ${apyusd['tvl_reserves']:,.0f}")
            except Exception as e:  # noqa: BLE001
                warn(f"accountable reserves: {e} — falling back to DefiLlama TVL")
            try:
                supply = get("https://apyusd-supply.apxusd-supply-1337.workers.dev/")
                apyusd["tvl_solana"] = round(supply["circulatingSupply"])
            except Exception as e:  # noqa: BLE001
                warn(f"apyusd supply worker: {e}")
        else:
            warn("apyx pool not found on DefiLlama — apyUSD row will be missing")

        data["yield_products"] = {"apyusd": apyusd, "items": items}
        print(f"  yield products: {len(items)} + apyUSD "
              f"({apyusd['apy'] if apyusd else '—'}% APY)")
    except Exception as e:  # noqa: BLE001
        warn(f"yield products: {e}")

    # ------------------------------------------------------- solana news feed
    # RSS/Atom aggregation, server-side so the page stays keyless. Tag feeds
    # arrive pre-filtered; general feeds pass a Solana-ecosystem keyword gate.
    NEWS_FEEDS = [
        ("Cointelegraph", "https://cointelegraph.com/rss/tag/solana", False),
        ("Decrypt", "https://decrypt.co/feed", True),
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/", True),
        ("The Block", "https://www.theblock.co/rss.xml", True),
        ("Blockworks", "https://blockworks.co/feed", True),
    ]
    NEWS_KEYWORDS = ["solana", "jupiter", "jito", "pump.fun", "pumpfun", "firedancer",
                     "helius", "drift", "kamino", "marinade", "raydium", "phantom",
                     "anza", "alpenglow", "dfdv", "defi development", "apyx"]
    import re as _re
    import email.utils as _eut

    def _news_match(title: str) -> bool:
        low = title.lower()
        if any(k in low for k in NEWS_KEYWORDS):
            return True
        return bool(_re.search(r"\bSOL\b", title))  # the ticker, case-sensitive

    def _feed_items(source: str, url: str, filtered: bool) -> list[dict]:
        import xml.etree.ElementTree as ET
        raw = urllib.request.urlopen(
            urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml"}),
            timeout=45, context=_SSL_CTX).read()
        root = ET.fromstring(raw)
        out = []
        ATOM = "{http://www.w3.org/2005/Atom}"
        for it in root.findall(".//item"):          # RSS
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            pub = it.findtext("pubDate") or ""
            try:
                dt = _eut.parsedate_to_datetime(pub)
            except Exception:  # noqa: BLE001
                continue
            out.append({"t": title, "u": link, "s": source,
                        "d": dt.astimezone(timezone.utc).isoformat(timespec="seconds")})
        for it in root.findall(f".//{ATOM}entry"):  # Atom
            title = (it.findtext(f"{ATOM}title") or "").strip()
            le = it.find(f"{ATOM}link")
            link = (le.get("href") if le is not None else "").strip()
            pub = it.findtext(f"{ATOM}published") or it.findtext(f"{ATOM}updated") or ""
            try:
                dt = datetime.fromisoformat(pub.replace("Z", "+00:00"))
            except Exception:  # noqa: BLE001
                continue
            out.append({"t": title, "u": link, "s": source,
                        "d": dt.astimezone(timezone.utc).isoformat(timespec="seconds")})
        if filtered:
            out = [x for x in out if _news_match(x["t"])]
        return [x for x in out if x["t"] and x["u"].startswith("http")]

    by_source: dict[str, list] = {}
    seen_titles: set = set()

    def _collect(source: str, items: list) -> None:
        for x in items:
            key = _re.sub(r"\W+", "", x["t"].lower())[:70]
            if key in seen_titles:
                continue
            seen_titles.add(key)
            by_source.setdefault(source, []).append(x)

    for source, url, filtered in NEWS_FEEDS:
        try:
            _collect(source, _feed_items(source, url, filtered))
        except Exception as e:  # noqa: BLE001
            warn(f"news feed {source}: {e}")

    # SolanaFloor has no RSS, but its Directus CMS is publicly readable.
    try:
        sf = get("https://cms.solanafloor.com/items/articles"
                 "?limit=15&sort=-date_created&fields=title,slug,date_created,status",
                 {"User-Agent": "Mozilla/5.0"})
        _collect("SolanaFloor", [
            {"t": a["title"], "u": f"https://solanafloor.com/news/{a['slug']}",
             "s": "SolanaFloor", "d": a["date_created"][:19] + "+00:00"}
            for a in sf.get("data", [])
            if a.get("status") == "published" and a.get("title") and a.get("slug")])
    except Exception as e:  # noqa: BLE001
        warn(f"news feed SolanaFloor: {e}")

    # Cap each outlet so one prolific source can't crowd out the rest, and
    # drop anything older than two weeks — "trending" means recent.
    fresh_cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    news: list = []
    for source, items in by_source.items():
        items = [x for x in items if x["d"] >= fresh_cutoff]
        items.sort(key=lambda x: x["d"], reverse=True)
        news.extend(items[:8])
    news.sort(key=lambda x: x["d"], reverse=True)
    data["news"] = news[:34]
    mix = {s: sum(1 for x in data["news"] if x["s"] == s) for s in by_source}
    print(f"  news: keeping {len(data['news'])} · mix {mix}")

    # ------------------------------------------------ cross-asset price series
    # Blockworks prices only four of these tokens, so performance comes from
    # CoinGecko. Free tier rate-limits hard (~6 calls before 429), hence the
    # spacing and the carry-forward when a call still fails.
    PRICE_ASSETS = [
        ("Bitcoin", "bitcoin"), ("Solana", "solana"), ("Ethereum", "ethereum"),
        ("BNB", "binancecoin"), ("Avalanche", "avalanche-2"), ("Sui", "sui"),
        ("Tron", "tron"), ("Polygon", "polygon-ecosystem-token"),
        ("Arbitrum", "arbitrum"), ("Hyperliquid", "hyperliquid"),
    ]
    perf: dict = {}
    for i, (label, cg_id) in enumerate(PRICE_ASSETS):
        try:
            if i:
                time.sleep(12)  # stay under CoinGecko's free-tier limiter
            j = get(f"https://api.coingecko.com/api/v3/coins/{cg_id}"
                    "/market_chart?vs_currency=usd&days=365&interval=daily")
            # CoinGecko's daily points are 00:00 UTC snapshots, so the sample
            # stamped date D is really the close of D-1. Shift it back, or every
            # price sits a day late and month boundaries land on the wrong close.
            pts = [{"d": (datetime.fromtimestamp(t / 1000, tz=timezone.utc).date()
                          - timedelta(days=1)).isoformat(),
                    "v": v} for t, v in (j.get("prices") or [])]
            pts = [p for p in pts if p["d"] < today_utc]
            if pts:
                perf[label] = pts
        except Exception as e:  # noqa: BLE001
            old = (prev_compare.get("price_perf") or {}).get(label) or []
            if old:
                perf[label] = old
                warn(f"price {label}: {e} — reused previous series")
            else:
                warn(f"price {label}: {e}")
    if perf:
        compare["price_perf"] = perf
        print("  price performance: " + ", ".join(f"{k}:{len(v)}" for k, v in perf.items()))

    # --------------------------------------------------- derived demand ratios
    # Two composites Blockworks doesn't publish directly:
    #   rev_share        — each chain's cut of that day's REV across the set,
    #                      i.e. where blockspace demand actually pays out.
    #   tx_per_address   — successful transactions per active address, a read
    #                      on how intensively the average wallet uses a chain.
    try:
        rev_c = compare.get("rev") or {}
        if rev_c:
            by_day: dict = {}
            for chain, pts in rev_c.items():
                for p in pts:
                    by_day.setdefault(p["d"], {})[chain] = p["v"]
            share: dict = {}
            for d0, vals in by_day.items():
                tot = sum(v for v in vals.values() if v and v > 0)
                if tot <= 0:
                    continue
                for chain, v in vals.items():
                    if v and v > 0:
                        share.setdefault(chain, []).append({"d": d0, "v": v / tot * 100})
            for chain in share:
                share[chain].sort(key=lambda p: p["d"])
            if share:
                compare["rev_share"] = share
                print("  derived rev_share: " + ", ".join(f"{c}:{len(v)}" for c, v in share.items()))

        succ_c, addr_c = compare.get("succeeded") or {}, compare.get("active_addresses") or {}
        tpa: dict = {}
        for chain, pts in succ_c.items():
            addrs = {p["d"]: p["v"] for p in addr_c.get(chain, [])}
            out = [{"d": p["d"], "v": p["v"] / addrs[p["d"]]}
                   for p in pts if addrs.get(p["d"])]
            if out:
                tpa[chain] = out
        if tpa:
            compare["tx_per_address"] = tpa
            print("  derived tx_per_address: " + ", ".join(f"{c}:{len(v)}" for c, v in tpa.items()))
    except Exception as e:  # noqa: BLE001
        warn(f"derived demand ratios: {e}")

    # ------------------------------------------- SOL monthly return seasonality
    # Calendar-month returns since inception: last close of month N versus last
    # close of month N-1, so partial current months are excluded and every cell
    # is a completed month.
    try:
        if prices:
            import calendar as _cal
            month_close: dict = {}
            month_last_day: dict = {}
            for d0 in sorted(prices):
                month_close[d0[:7]] = prices[d0]     # last close seen per month
                month_last_day[d0[:7]] = d0
            # A month counts only once it has actually closed — i.e. we hold a
            # price for its final calendar day. Keying off "latest month in the
            # data" instead would drop a finished month whenever the upstream
            # series lags a day or two behind today.
            def _complete(m: str) -> bool:
                y, mo = int(m[:4]), int(m[5:])
                return month_last_day[m][8:] == f"{_cal.monthrange(y, mo)[1]:02d}"
            months = sorted(month_close)
            grid: dict = {}
            for i in range(1, len(months)):
                m = months[i]
                if not _complete(m):                 # still in progress
                    continue
                prev_m = months[i - 1]
                # only chain consecutive months, so a data gap can't fake a return
                py, pm = int(prev_m[:4]), int(prev_m[5:])
                if (py + (pm == 12), (pm % 12) + 1) != (int(m[:4]), int(m[5:])):
                    continue
                base = month_close[prev_m]
                if base:
                    grid.setdefault(m[:4], {})[int(m[5:])] = (month_close[m] / base - 1) * 100

            def _median(xs: list[float]) -> float:
                xs = sorted(xs)
                n = len(xs)
                return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

            stats_by_month: dict = {}
            for mo in range(1, 13):
                vals = [grid[y][mo] for y in grid if mo in grid[y]]
                if vals:
                    stats_by_month[mo] = {
                        "avg": sum(vals) / len(vals),
                        "med": _median(vals),
                        "pos": sum(1 for v in vals if v > 0),
                        "n": len(vals),
                    }
            data["monthly_returns"] = {"grid": grid, "stats": stats_by_month}
            best = max(stats_by_month.items(), key=lambda kv: kv[1]["med"])
            print(f"  monthly seasonality: {len(grid)} years, best median month "
                  f"= {best[0]} ({best[1]['med']:+.1f}%)")
    except Exception as e:  # noqa: BLE001
        warn(f"monthly seasonality: {e}")

    data["compare"] = compare
    data["daily"] = daily
    data["warnings"] = warnings
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"\nWrote {OUT} ({OUT.stat().st_size:,} bytes) — {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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
# DefiLlama's free API covers everything we ask of it except perps, which sits
# behind their paid plan. METERED — this key has a monthly allowance, and the
# Blockworks cap running out mid-September took most of the dashboard down with
# it. Budget: perps is the ONLY paid call, one per chain (11) per refresh, and
# the cron runs 4x daily => ~1,320 calls a month. Everything else on this file
# stays on the keyless api.llama.fi. Before adding another paid endpoint, work
# out its monthly cost the same way and say so in a comment. A single call to
# /overview/derivatives would be cheaper but breaks down by protocol rather than
# by chain, so it cannot answer "volume per chain" without us inventing the
# attribution ourselves.
DL_KEY = os.environ.get("DEFILLAMA_API_KEY", "").strip()
DL_PRO = "https://pro-api.llama.fi"
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
              "sui", "tron", "hyperevm", "polygon", "robinhood"]
# Robinhood Chain launched 2026-04-30, so its series simply start later than
# the rest; a chain that returns nothing is dropped from its card on its own.
LLAMA_SLUGS = {"solana": "Solana", "ethereum": "Ethereum", "base": "Base",
               "arbitrum": "Arbitrum", "bnb": "BSC", "avalanche": "Avalanche",
               "sui": "Sui", "tron": "Tron", "hyperevm": "Hyperliquid",
               "polygon": "Polygon", "robinhood": "Robinhood Chain"}

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
        except urllib.error.HTTPError as e:
            # The body says which limit was hit and for how long; without it a
            # rate-limited run reports a bare "429" and nothing to act on.
            try:
                detail = e.read(400).decode(errors="replace").strip()
            except Exception:  # noqa: BLE001
                detail = ""
            retry_after = e.headers.get("Retry-After") if e.headers else None
            last = RuntimeError(f"{e}" + (f" — {detail}" if detail else "")
                                + (f" (Retry-After {retry_after})" if retry_after else ""))
            if attempt < tries - 1:
                # A 429 is a window, not a blip: honour Retry-After when it is
                # short enough to be worth waiting out.
                wait = 2 * (attempt + 1)
                if e.code == 429 and (retry_after or "").isdigit():
                    wait = min(int(retry_after), 60)
                time.sleep(wait)
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


def _last_date(val) -> str | None:
    """Newest date in a series, or in the newest chain of a compare block."""
    if isinstance(val, list):
        return val[-1]["d"] if val and isinstance(val[-1], dict) and "d" in val[-1] else None
    if isinstance(val, dict):
        ds = [v[-1]["d"] for v in val.values()
              if isinstance(v, list) and v and isinstance(v[-1], dict) and "d" in v[-1]]
        return max(ds) if ds else None
    return None


def main() -> int:
    print("Refreshing Solana dashboard data...")
    # The last good file, when there is one. A rate-limited upstream still lets
    # this script finish and write a valid file — just one with whole cards
    # missing — so anything a run fails to fetch is carried over from here
    # rather than published as an absence. (Blockworks 429s every endpoint once
    # its quota is spent, which blanked nine of the fifteen cards on 2026-09-14.)
    prev_all: dict = {}
    try:
        prev_all = json.loads(OUT.read_text())
    except Exception:  # noqa: BLE001 - first run or unreadable
        pass
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
    # Coinbase, not Blockworks. The price series gates the whole deploy and
    # feeds the monthly-returns table, and Blockworks is a metered monthly
    # allowance — when it runs out mid-month every price on the site freezes,
    # which is what happened through September 2026. Coinbase is keyless,
    # unmetered and publishes a daily close for SOL-USD back to 2021-06-17.
    #
    # It is also the more trustworthy number: on the days Blockworks and
    # Coinbase disagreed, CoinGecko independently matched Coinbase to the cent
    # (2026-09-09: Coinbase 101.54, CoinGecko 101.57, Blockworks 103.53).
    #
    # Before 2021-06-17 Coinbase had not listed SOL, so the carried history
    # stands for those dates. That join is fixed in the past and never moves.
    CB_START = "2021-06-17"

    def coinbase_closes(since: str) -> dict[str, float]:
        """Daily SOL-USD closes from Coinbase, paginated.

        The candles endpoint caps at 300 rows per call, so walk forward in
        chunks. ~6 calls covers the whole listed history; they are keyless and
        unmetered, so rebuilding the series every run costs nothing and keeps
        one consistent source rather than splicing a tail onto a stale head.
        """
        out: dict[str, float] = {}
        start = date.fromisoformat(max(since, CB_START))
        today = date.today()
        while start <= today:
            end = min(start + timedelta(days=299), today)
            rows = get("https://api.exchange.coinbase.com/products/SOL-USD/candles"
                       f"?granularity=86400&start={start.isoformat()}T00:00:00Z"
                       f"&end={end.isoformat()}T00:00:00Z")
            for r in rows or []:
                # [time, low, high, open, close, volume]
                d0 = datetime.fromtimestamp(r[0], tz=timezone.utc).date().isoformat()
                if r[4]:
                    out[d0] = float(r[4])
            start = end + timedelta(days=1)
            time.sleep(0.4)          # public limit is 10/s; stay well under it
        return out

    prices: dict[str, float] = {}
    carried = {p["d"]: p["v"] for p in (prev_all.get("series") or {}).get("price") or []}
    try:
        cb = coinbase_closes(CB_START)
        if len(cb) < 500:
            raise RuntimeError(f"only {len(cb)} closes returned — looks truncated")
        # Everything Coinbase covers comes from Coinbase; older dates keep the
        # history already published. `update` order matters: Coinbase wins on
        # any date both hold.
        prices = {d: v for d, v in carried.items() if d < CB_START}
        prices.update(cb)
        print(f"  price series: {len(prices)} days (Coinbase from {CB_START}, "
              f"through {max(prices)})")
    except Exception as e:  # noqa: BLE001
        warn(f"price series (Coinbase): {e}")

    # Live quote, for the tile that shows the current price rather than a close.
    # Coinbase's ticker is keyless and is the same venue the series comes from,
    # so the tile and the chart cannot disagree about which exchange they mean.
    spot = None
    try:
        spot = float(get("https://api.exchange.coinbase.com/products/SOL-USD/ticker")["price"])
        print(f"  spot price: ${spot}")
    except Exception as e:  # noqa: BLE001
        warn(f"spot price (Coinbase): {e}")

    # The publish check that gates the whole deploy hangs off the price series.
    # Carrying the previous run's keeps the site shipping through an upstream
    # outage instead of freezing everything — including the DefiLlama series,
    # which are perfectly healthy.
    if not prices:
        prices = carried
        if prices:
            warn(f"price series: reused the previous run's ({len(prices)} days, "
                 f"through {max(prices)})")
    cg_24h = None
    if spot is None:
        try:
            j = get("https://api.coingecko.com/api/v3/simple/price"
                    "?ids=solana&vs_currencies=usd&include_24hr_change=true")["solana"]
            spot = j["usd"]
            cg_24h = j.get("usd_24h_change")
            print(f"  spot price (CoinGecko): ${spot}")
        except Exception as e:  # noqa: BLE001
            warn(f"spot price fallback: {e}")

    # 24h return from the rolling-24h OHLCV open vs the live spot quote.
    try:
        o = bw("v1/assets/solana/ohlcv")
        cur = spot or o.get("close")
        if o.get("open") and cur:
            stats["return_24h"] = (cur / o["open"] - 1) * 100
            print(f"  24h return: {stats['return_24h']:+.2f}%")
    except Exception as e:  # noqa: BLE001
        warn(f"ohlcv 24h: {e}")
    # CoinGecko hands back a 24h change with the quote, which is the same
    # measure the tile wants and the only one available when Blockworks is out.
    if stats.get("return_24h") is None and cg_24h is not None:
        stats["return_24h"] = cg_24h
        warn(f"24h return: taken from the CoinGecko quote ({cg_24h:+.2f}%)")

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
    # DEX volume and stablecoin supply now come from DefiLlama, further down —
    # same numbers for the tile and the cross-chain card, from one fetch.

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

    # Average TPS across the year so far, measured against elapsed wall-clock
    # rather than a nominal 365 days.
    if txns and stats.get("ytd_transactions"):
        days = len(ytd(txns))
        if days:
            stats["avg_tps_ytd"] = stats["ytd_transactions"] / (days * 86400)

    # Volume-weighted, not a mean of daily averages.
    if stats.get("ytd_fees_usd") and stats.get("ytd_transactions"):
        stats["avg_fee_ytd"] = stats["ytd_fees_usd"] / stats["ytd_transactions"]

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
    prev_compare: dict = prev_all.get("compare") or {}

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

    # ------------------------------------- DEX volume + stablecoins (DefiLlama)
    # Both used to come from Blockworks, against a monthly request quota that ran
    # out mid-September. DefiLlama is keyless and, on the evidence, the better
    # source for these two anyway: it covers Tron and Polygon, which Blockworks
    # has no DEX series for, and it carries stablecoin supply for all eleven
    # chains where Blockworks had eight — three of them (Base, Arbitrum,
    # Avalanche) frozen since May. It is also a day fresher. Solana's own DEX
    # volume agreed with Blockworks to within 1-7% daily; the other chains read
    # higher, because DefiLlama tracks more venues per chain. Whole histories are
    # replaced rather than spliced, so no series has a seam in it.
    def llama_chain_series(key: str, label: str, fetch, slugs=None) -> dict[str, float]:
        """Fill compare[key] from one DefiLlama call per chain. Returns Solana's
        own dated series, so the tiles read the same numbers as the card.
        `slugs` overrides the default names where a dimension spells a chain
        differently (perps calls Hyperliquid "Hyperliquid L1")."""
        out, solana = {}, {}
        for chain, slug in (slugs or LLAMA_SLUGS).items():
            try:
                s = fetch(urllib.parse.quote(slug))
                pts = [{"d": d, "v": s[d]} for d in sorted(s) if since <= d < today_utc]
                if pts:
                    out[chain] = pts
                if chain == "solana":
                    solana = {d: v for d, v in s.items() if d < today_utc}
            except Exception as e:  # noqa: BLE001 - one chain missing is survivable
                warn(f"{label} {chain}: {e}")
            time.sleep(1.5)   # firing eleven at once gets some back empty
        if out:
            compare[key] = out
            print(f"  compare {label}: " + ", ".join(f"{c}:{len(v)}" for c, v in out.items()))
        return solana

    def _dex_volume(slug: str) -> dict[str, float]:
        j = get(f"https://api.llama.fi/overview/dexs/{slug}?excludeTotalDataChart=false"
                "&excludeTotalDataChartBreakdown=true&dataType=dailyVolume")
        return {datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(): v
                for t, v in (j.get("totalDataChart") or []) if v}

    def _stablecoins(slug: str) -> dict[str, float]:
        out = {}
        for r in get(f"https://stablecoins.llama.fi/stablecoincharts/{slug}"):
            tot = r.get("totalCirculatingUSD")
            # Every peg in one number: a chain's dollar stablecoins are the bulk
            # of it, but EUR and gold pegs are supply on the chain too.
            v = sum(tot.values()) if isinstance(tot, dict) else tot
            if v:
                out[datetime.fromtimestamp(int(r["date"]), tz=timezone.utc).date().isoformat()] = v
        return out

    # Perps: DefiLlama's own per-chain attribution, which is the whole point of
    # paying for it — their breakdown endpoint splits by protocol, and deciding
    # which chain each of forty perp venues belongs to is not a judgement this
    # script should be making. Reads ~71-81% of the Blockworks Solana figure it
    # replaces (they track more venues), so the whole history is swapped rather
    # than spliced. Their derivatives slug for Hyperliquid is "Hyperliquid L1".
    def _perps(slug: str) -> dict[str, float]:
        if not DL_KEY:
            raise RuntimeError("DEFILLAMA_API_KEY is not set")
        j = get(f"{DL_PRO}/{DL_KEY}/api/overview/derivatives/{slug}"
                "?excludeTotalDataChart=false&excludeTotalDataChartBreakdown=true"
                "&dataType=dailyVolume")
        return {datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(): v
                for t, v in (j.get("totalDataChart") or []) if v}

    # Fees, three ways. DefiLlama separates what users pay the *chain* from what
    # they pay the *apps* on it, and splits each into gross fees and the slice
    # actually retained as revenue. `/summary/fees/{chain}` treats the chain
    # itself as a protocol (category "Chain"); `/overview/fees/{chain}` is every
    # protocol deployed on it. All on the keyless host.
    def _chain_fees(slug: str, data_type: str = "dailyFees") -> dict[str, float]:
        # The chain's protocol slug is its name lowercased and hyphenated —
        # "Robinhood Chain" is "robinhood-chain", and an unhyphenated space
        # fails before the request is even sent.
        # llama_chain_series hands these over percent-encoded for the path-style
        # endpoints; this one wants a hyphenated protocol slug instead, so undo
        # that first — "Robinhood%20Chain" has to become "robinhood-chain".
        name = urllib.parse.unquote(slug).lower().replace(" ", "-")
        j = get(f"https://api.llama.fi/summary/fees/{name}"
                f"?excludeTotalDataChartBreakdown=true&dataType={data_type}")
        return {datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(): v
                for t, v in (j.get("totalDataChart") or []) if v}

    def _chain_revenue(slug: str) -> dict[str, float]:
        return _chain_fees(slug, "dailyRevenue")

    def _app_fees(slug: str) -> dict[str, float]:
        j = get(f"https://api.llama.fi/overview/fees/{slug}?excludeTotalDataChart=false"
                "&excludeTotalDataChartBreakdown=true&dataType=dailyFees")
        return {datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(): v
                for t, v in (j.get("totalDataChart") or []) if v}

    dex = llama_chain_series("dex_volume", "dex volume", _dex_volume)
    stables = llama_chain_series("stablecoin_supply", "stablecoin supply", _stablecoins)
    # Perps and RWA are the only metered calls we make. DefiLlama publishes them
    # once a day; the cron runs four times. So skip the fetch when the carried
    # block already holds yesterday — the history lives in data.json and is
    # carried forward regardless, which makes a skipped run cost nothing and
    # lose nothing. Self-healing: a failed or missed day leaves the block stale,
    # so the next run picks it up. Takes Pro spend from ~1,440 calls a month to
    # ~360 without changing a single published number.
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    def needs_refetch(block_key: str) -> bool:
        prev_block = (prev_all.get("compare") or {}).get(block_key) or {}
        if not prev_block:
            return True
        newest = max((pts[-1]["d"] for pts in prev_block.values() if pts), default="")
        return newest < yesterday

    def local_series(key: str, label: str,
                     require_cover: bool = False) -> tuple[bool, dict[str, float]]:
        """Use a capture of DefiLlama's own front end, if one is current.

        tools/llama_local.py writes these from the __NEXT_DATA__ payload the
        perps and RWA pages server-render. Each capture carries the entire
        history, so this replaces the block wholesale rather than splicing —
        mixing two sources inside one chain's series would put a visible step
        in the chart, and the chains do not all agree between them (Ethereum
        perps reads ~0.87 of the paid endpoint; the other ten match to four
        decimals).

        Returns (used, solana) — `used` says whether the capture was taken, kept
        separate from the Solana series because a block can legitimately be
        current and still not break Solana out.
        """
        path = Path(__file__).resolve().parent / "local" / f"{key}.json"
        if not path.exists():
            return False, {}
        try:
            blob = json.loads(path.read_text())
            chains = blob.get("chains") or {}
            # A capture that has gone stale is worse than no capture: it would
            # pin the card to an old day and look live. Fall through to the API
            # instead and let the warning say why.
            if (blob.get("last") or "") < yesterday:
                warn(f"{label}: local capture ends {blob.get('last')} "
                     f"(want {yesterday}) — falling back")
                return False, {}
            out = {}
            for chain, s in chains.items():
                pts = [{"d": d, "v": v} for d, v in sorted(s.items())
                       if since <= d < today_utc]
                if pts:
                    out[chain] = pts
            if not out:
                return False, {}
            # The RWA page groups everything outside its top-N into "Others",
            # so a capture of it covers five of the ten chains that card shows.
            # Taking it anyway would quietly halve the card, which looks like a
            # data change rather than a source change. Refuse unless the capture
            # covers everything already published.
            if require_cover:
                have = set((prev_all.get("compare") or {}).get(key) or {})
                short = have - set(out)
                if short:
                    warn(f"{label}: local capture omits {', '.join(sorted(short))} "
                         f"— falling back rather than dropping chains")
                    return False, {}
            compare[key] = out
            print(f"  compare {label} (local capture {blob.get('last')}): "
                  + ", ".join(f"{c}:{len(v)}" for c, v in out.items()))
            return True, {d: v for d, v in (chains.get("solana") or {}).items()
                          if d < today_utc}
        except Exception as e:  # noqa: BLE001 - a bad capture must not stop the run
            warn(f"{label}: local capture unreadable ({e}) — falling back")
            return False, {}

    used_local, perps = local_series("perps_volume", "perps volume")
    if used_local:
        pass          # a current capture beats the metered call; nothing to do
    elif not DL_KEY:
        warn("perps volume: DEFILLAMA_API_KEY is not set — skipped (paid endpoint)")
    elif not needs_refetch("perps_volume"):
        print("  perps volume: already current — skipped (metered endpoint)")
    else:
        perps = llama_chain_series("perps_volume", "perps volume", _perps,
                                   slugs={**LLAMA_SLUGS, "hyperevm": "Hyperliquid L1"})

    llama_chain_series("chain_fees", "chain fees", _chain_fees)
    llama_chain_series("chain_revenue", "chain revenue", _chain_revenue)
    llama_chain_series("app_fees", "app fees", _app_fees)

    # RWA: one Pro call covers every chain at once — a rare case where the paid
    # endpoint is cheaper than the free pattern, so take it. Note the RWA paths
    # sit at the Pro host root, NOT under /api like the rest of the Pro surface.
    if local_series("rwa_aum", "rwa aum", require_cover=True)[0]:
        pass          # a current capture beats the metered call
    elif DL_KEY and not needs_refetch("rwa_aum"):
        print("  rwa aum: already current — skipped (metered endpoint)")
    elif DL_KEY:
        try:
            rows = get(f"{DL_PRO}/{DL_KEY}/rwa/chart/chain-breakdown")
            by: dict = {}
            for r in rows:
                ts = r.get("timestamp")
                if not ts:
                    continue
                d0 = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
                if d0 < since or d0 >= today_utc:
                    continue
                for name, v in r.items():
                    if name == "timestamp" or not isinstance(v, (int, float)) or v <= 0:
                        continue
                    by.setdefault(name, {})[d0] = v
            want = {v: k for k, v in LLAMA_SLUGS.items()}
            out = {}
            for name, series in by.items():
                chain = want.get(name)
                if chain and series:
                    out[chain] = [{"d": d, "v": series[d]} for d in sorted(series)]
            if out:
                compare["rwa_aum"] = out
                print("  compare rwa aum: " + ", ".join(f"{c}:{len(v)}" for c, v in out.items()))
        except Exception as e:  # noqa: BLE001
            warn(f"rwa aum: {e}")

    if perps:
        y = ytd(perps)
        stats["ytd_perps_volume"] = sum(y.values())
        daily["perps_volume"] = perps[max(perps)]
        daily.pop("perps_volume_as_of", None)
        data["series"]["perps"] = [{"d": d, "v": perps[d]} for d in sorted(perps) if d >= since]
        print(f"  perps volume: YTD ${stats['ytd_perps_volume']:,.0f} over {len(y)} days")

    if dex:
        y = ytd(dex)
        stats["ytd_dex_volume"] = sum(y.values())
        stats["ytd_dex_volume_days"] = len(y)
        if y:
            stats["avg_daily_dex_volume_ytd"] = stats["ytd_dex_volume"] / len(y)
        daily["dex_volume"] = dex[max(dex)]
        data["series"]["ytd_dex_volume"] = [{"d": d, "v": dex[d]} for d in sorted(dex) if d >= since]
        print(f"  dex volume: YTD ${stats['ytd_dex_volume']:,.0f} over {len(y)} days")
    if stables:
        d0 = max(stables)
        stats["stablecoin_supply"] = stables[d0]
        stats["stablecoin_supply_as_of"] = d0
        ytd_open = stables.get(PREV_YEAR_END) or at_or_before(stables, date(YEAR - 1, 12, 31), 30)
        if ytd_open:
            stats["stablecoin_supply_ytd_change"] = ((stables[d0] / ytd_open) - 1) * 100
        data["series"]["stablecoin_supply"] = [{"d": d, "v": stables[d]}
                                               for d in sorted(stables) if d >= since]
        print(f"  stablecoin supply: ${stats['stablecoin_supply']:,.0f} ({d0})")

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

    # --------------------------------------------------- app revenue (DefiLlama)
    # Revenue earned by the applications running on a chain, which is a
    # different thing from the chain's own REV: REV is what users pay the
    # network, this is what they pay the protocols on top of it. Blockworks
    # carries the metric for only a couple of chains, DefiLlama for all of them.
    # Requests are spaced — firing all eleven at once gets some back empty.
    try:
        out = {}
        for chain, slug in LLAMA_SLUGS.items():
            try:
                j = get("https://api.llama.fi/overview/fees/" + urllib.parse.quote(slug)
                        + "?excludeTotalDataChart=false"
                        + "&excludeTotalDataChartBreakdown=true&dataType=dailyRevenue")
                pts = [{"d": datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(),
                        "v": v} for t, v in (j.get("totalDataChart") or []) if v]
                pts = [p for p in pts if since <= p["d"] < today_utc]
                if pts:
                    out[chain] = pts
            except Exception as e:  # noqa: BLE001 - one chain missing is survivable
                warn(f"app revenue {chain}: {e}")
            time.sleep(1.5)
        if out:
            compare["app_revenue"] = out
            print("  compare app revenue: "
                  + ", ".join(f"{c}:{len(v)}" for c, v in out.items()))
    except Exception as e:  # noqa: BLE001
        warn(f"app revenue: {e}")

    try:
        out = {}
        for chain, slug in LLAMA_SLUGS.items():
            # "Robinhood Chain" is the first slug here with a space in it, and
            # an unencoded one fails before the request is even sent.
            rows = get("https://api.llama.fi/v2/historicalChainTvl/"
                       + urllib.parse.quote(slug))
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
            # CoinGecko appends a live "now" sample after the 00:00 snapshots, so
            # the last date arrives twice — once as yesterday's close, once as
            # today's intraday price wearing yesterday's date. Keep the close;
            # the intraday story belongs to the price chart, which has a real
            # timestamp for it. A repeated date also miscounts the days behind
            # a week-to-date or month-to-date figure.
            seen: set = set()
            pts = [p for p in pts if not (p["d"] in seen or seen.add(p["d"]))]
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

    # --------------------------------------------------------- DFDV share price
    # For the "Levered SOL" overlay: DFDV's own API carries SOL and holdings but
    # not its share price, so closes come from Nasdaq (keyless, but it wants a
    # browser agent). Only what the overlay plots is kept — daily closes from
    # the anchor onwards, nothing earlier.
    try:
        # SOL's 2026 low ($62.42 close) and its lowest since Dec 2023, so it is
        # the natural base for "how much of the recovery did the levered
        # instrument capture".
        anchor = "2026-06-06"
        # Fetch everything Nasdaq holds, not just the stretch since the anchor.
        # Keeping only the post-anchor closes meant YTD, 1Y and 5Y on the levered
        # chart all clamped to 2026-06-06 and rendered identically — the control
        # looked broken because there was no earlier data to re-anchor to. The
        # series starts 2023-07-25 (as Janover), so 5Y and All bottom out there.
        start = "2023-01-01"
        u = ("https://api.nasdaq.com/api/quote/DFDV/historical?assetclass=stocks"
             f"&fromdate={start}&todate={date.today().isoformat()}&limit=3000")
        j = get(u, {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
        rows = ((j.get("data") or {}).get("tradesTable") or {}).get("rows") or []
        closes = {}
        for r in rows:
            m, dd, y = r["date"].split("/")
            closes[f"{y}-{m}-{dd}"] = float(r["close"].replace("$", "").replace(",", ""))
        # The anchor is a SOL date and can land on a weekend, when DFDV has no
        # close. Carry the last close at or before it so both sides are measured
        # from the same moment — starting DFDV at the next trading day would
        # hand SOL a free run of however many days the market was shut.
        # Every close, in order. The chart picks its own base off whichever
        # anchor the selected range resolves to, and needs the closes either
        # side of it to do that.
        pts = [{"d": k, "v": round(v, 4)} for k, v in sorted(closes.items())]
        if len(pts) < 5:
            raise RuntimeError(f"only {len(pts)} closes since {anchor}")
        # Today has no close until the bell, so the chart would sit a day behind
        # SOL. Carry the last trade as a provisional point instead, flagged so
        # the caption can say it is intraday.
        live = False
        try:
            q = get("https://api.nasdaq.com/api/quote/DFDV/info?assetclass=stocks",
                    {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"})
            pd_ = ((q.get("data") or {}).get("primaryData") or {})
            px = float(str(pd_.get("lastSalePrice", "")).replace("$", "").replace(",", ""))
            today = date.today().isoformat()
            if px > 0 and today > pts[-1]["d"]:
                pts.append({"d": today, "v": round(px, 4)})
                live = True
        except Exception as e:  # noqa: BLE001 - the closes alone are still usable
            warn(f"DFDV live quote: {e}")
        data["dfdv"] = {"anchor": anchor, "points": pts, "intraday": live}
        print(f"  DFDV closes: {len(pts)} days, {pts[0]['d']} ${pts[0]['v']} "
              f"-> {pts[-1]['d']} ${pts[-1]['v']}")
    except Exception as e:  # noqa: BLE001
        warn(f"DFDV share price: {e}")
        try:
            if prev_d := json.loads(OUT.read_text()).get("dfdv"):
                data["dfdv"] = prev_d
        except Exception:  # noqa: BLE001 - first run or unreadable
            pass

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
            # The month in progress, kept out of `grid` on purpose: it belongs on
            # the page as month-to-date, but folding a partial month into the
            # averages, medians and hit rates below would corrupt every one of
            # them. Consumers that want only closed months read `grid` and are
            # unaffected by this.
            mtd = None
            if months:
                cur = months[-1]
                prev_m = months[-2] if len(months) > 1 else None
                py, pm = (int(prev_m[:4]), int(prev_m[5:])) if prev_m else (0, 0)
                consecutive = prev_m and (py + (pm == 12), (pm % 12) + 1) == (
                    int(cur[:4]), int(cur[5:]))
                if not _complete(cur) and consecutive and month_close[prev_m]:
                    mtd = {"m": cur, "as_of": month_last_day[cur],
                           "v": (month_close[cur] / month_close[prev_m] - 1) * 100}
                    print(f"  month to date: {cur} {mtd['v']:+.1f}% "
                          f"through {mtd['as_of']}")

            data["monthly_returns"] = {"grid": grid, "stats": stats_by_month, "mtd": mtd}
            best = max(stats_by_month.items(), key=lambda kv: kv[1]["med"])
            print(f"  monthly seasonality: {len(grid)} years, best median month "
                  f"= {best[0]} ({best[1]['med']:+.1f}%)")
    except Exception as e:  # noqa: BLE001
        warn(f"monthly seasonality: {e}")

    data["compare"] = compare

    # ------------------------------------------------- keep the last good data
    # Only dated series are carried: every card prints the date of its own last
    # point, so a stale curve reads as stale. Point-in-time stats carry no date
    # and would simply look current, so those stay blank when a fetch fails.
    # Long enough to bridge a monthly quota: Blockworks stopped answering on
    # 2026-09-14 with its series ending 09-12, and the cap resets on 10-01.
    # At ten days the carry would have expired on 09-23 and taken nine cards
    # with it — the exact outage it was written to prevent, just later.
    carry_cut = (date.today() - timedelta(days=30)).isoformat()
    for kind, fresh in (("compare", compare), ("series", data["series"])):
        for key, val in (prev_all.get(kind) or {}).items():
            if fresh.get(key):
                continue
            was = _last_date(val)
            if was and was >= carry_cut:
                fresh[key] = val
                warn(f"{kind}.{key}: nothing fetched this run — kept the previous "
                     f"series (through {was})")

    # Year-to-date totals are just sums of a series we already carried, so
    # recompute them from it rather than shipping a null tile. Derived from the
    # carried data, not carried themselves: nothing here can be staler than the
    # series it is summed from.
    if stats.get("ytd_transactions") is None:
        ser = data["series"].get("ytd_transactions") or []
        ytd_pts = [p for p in ser if p["d"] >= YTD_START.isoformat()]
        if ytd_pts:
            stats["ytd_transactions"] = sum(p["v"] for p in ytd_pts)
            stats["ytd_transactions_days"] = len(ytd_pts)
            stats["avg_tps_ytd"] = stats["ytd_transactions"] / (len(ytd_pts) * 86400)
            warn(f"ytd_transactions: recomputed from the carried series "
                 f"({len(ytd_pts)} days through {ytd_pts[-1]['d']})")

    # The Daily view prints "latest complete day · <date>" above these tiles, so
    # a carried block reads as dated in exactly the way a carried series does.
    # Without this the tiles sat empty for the whole Blockworks outage while the
    # cards beside them showed the same days' numbers quite happily.
    if not daily.get("as_of"):
        old_daily = prev_all.get("daily") or {}
        if old_daily.get("as_of", "") >= carry_cut:
            # setdefault, not update: this run may already have produced some of
            # these from a source that is still up, and the previous block must
            # not overwrite them. That is what left the Perps tile reading the
            # old Blockworks figure while the card beside it read DefiLlama's.
            for k, v in old_daily.items():
                daily.setdefault(k, v)
            warn(f"daily: nothing fetched this run — kept the previous block "
                 f"(through {old_daily['as_of']})")

    # Total fees is the one input with no series behind it — it only ever
    # existed as a scalar — so it cannot be recomputed, only carried. Tie it to
    # the daily block's date, which is the period it belongs to.
    if stats.get("ytd_fees_usd") is None:
        prev_stats = prev_all.get("stats") or {}
        prev_as_of = (prev_all.get("daily") or {}).get("as_of", "")
        if prev_stats.get("ytd_fees_usd") and prev_as_of >= carry_cut:
            stats["ytd_fees_usd"] = prev_stats["ytd_fees_usd"]
            if prev_stats.get("avg_fee_ytd"):
                stats["avg_fee_ytd"] = prev_stats["avg_fee_ytd"]
            warn(f"ytd_fees_usd: kept the previous value (through {prev_as_of})")

    # Each Daily tile reads the newest point of its own series. Normally those
    # land together; through an outage they don't, so a tile fed by a series
    # that stopped earlier than the block's `as_of` carries its own date and the
    # card prints it. An empty tile is no more honest than a dated one, and it
    # is a good deal less useful.
    def fill_daily(field, series_key, source=None):
        pts = source if source is not None else (data["series"].get(series_key) or [])
        if daily.get(field) is not None or not pts:
            return None
        tail = pts[-1]
        if tail["d"] < carry_cut:
            return None
        daily[field] = tail["v"]
        if tail["d"] != daily.get("as_of"):
            daily[f"{field}_as_of"] = tail["d"]
        return tail["d"]

    filled = {}
    for field, key in (("traders", "traders"), ("perps_volume", "perps"),
                       ("tokenized_equity_volume", "tokenized_equity_volume")):
        got = fill_daily(field, key)
        if got:
            filled[field] = got
    # Solana's own REV lives in the cross-chain block rather than in `series`.
    got = fill_daily("revenue_usd", None, (compare.get("rev") or {}).get("solana") or [])
    if got:
        filled["revenue_usd"] = got
        px = prices.get(got) or (prices.get(max(prices)) if prices else None)
        if px:
            daily["revenue_sol"] = daily["revenue_usd"] / px
    if stats.get("tokenized_equity_supply") is None:
        sup = data["series"].get("tokenized_equity_supply") or []
        if sup and sup[-1]["d"] >= carry_cut:
            stats["tokenized_equity_supply"] = sup[-1]["v"]
            filled["tokenized_equity_supply"] = sup[-1]["d"]
    # The YTD snapshot sums the same carried series the Daily tiles read, so it
    # can be rebuilt the same way rather than shipping half a panel of dashes.
    def ytd_sum(stat, series_key, source=None, mean=False):
        if stats.get(stat) is not None:
            return None
        pts = source if source is not None else (data["series"].get(series_key) or [])
        y = [p for p in pts if p["d"] >= YTD_START.isoformat()]
        if not y or y[-1]["d"] < carry_cut:
            return None
        total = sum(p["v"] for p in y)
        stats[stat] = total / len(y) if mean else total
        return f"{y[-1]['d']}, {len(y)}d"

    sol_rev = (compare.get("rev") or {}).get("solana") or []
    for stat, key, src, mean in (
            ("ytd_revenue_usd", None, sol_rev, False),
            ("avg_daily_traders_ytd", "traders", None, True),
            ("ytd_perps_volume", "perps", None, False),
            ("ytd_tokenized_equity_volume", "tokenized_equity_volume", None, False)):
        got = ytd_sum(stat, key, src, mean)
        if got:
            filled[stat] = got
    # REV is carried in dollars; the SOL figure is what it bought at each close.
    if stats.get("ytd_revenue_sol") is None and stats.get("ytd_revenue_usd") is not None and prices:
        last_px = prices.get(max(prices))
        tot = sum(p["v"] / (prices.get(p["d"]) or last_px) for p in sol_rev
                  if p["d"] >= YTD_START.isoformat() and (prices.get(p["d"]) or last_px))
        if tot:
            stats["ytd_revenue_sol"] = tot

    if filled:
        warn("tiles filled from carried series: "
             + ", ".join(f"{k} ({v})" for k, v in sorted(filled.items())))

    data["daily"] = daily
    data["warnings"] = warnings

    # ------------------------------------------------- ship display values only
    # This file is fetched by the browser, so whatever it holds is public. Keep
    # it to what the page actually draws, at the precision it draws at: the
    # licensed feeds are for stateofsol.com to render, not for us to republish
    # as a dataset. `succeeded` only ever fed the server-side ratios above and
    # is never read by the page, so it does not ship at all.
    compare.pop("succeeded", None)

    # The cross-chain cards offer nothing longer than 5y, so history past that
    # is shipped to every visitor and drawn for none of them. Six years leaves
    # room for a custom start date and stops the file growing without bound.
    horizon = (date.today() - timedelta(days=366 * 6)).isoformat()
    for chains in compare.values():
        for chain, pts in chains.items():
            chains[chain] = [p for p in pts if p["d"] >= horizon]

    # decimals per series, matched to how each one is labelled on the page
    PLACES = {
        "rev": 0, "dex_volume": 0, "defi_tvl": 0, "stablecoin_supply": 0,
        "tokenized_equity_volume": 0, "transactions": 0, "active_addresses": 0,
        "perps_volume": 0, "chain_fees": 0, "chain_revenue": 0, "app_fees": 0, "rwa_aum": 0,
        "app_revenue": 0,
        "rev_share": 2, "tx_per_address": 2, "fsr": 2,
        # fee_vol is a dollar amount on the same scale as fee_median — sub-cent
        # on every chain but Ethereum — so two decimals flattened six of the
        # eight series to zero and quantised the other two into stair steps.
        "fee_vol": 7, "fee_median": 7, "price_perf": 4,
    }

    def round_pts(pts: list, places: int) -> list:
        for p in pts:
            v = p.get("v")
            if isinstance(v, float):
                p["v"] = round(v, places) if places else round(v)
        return pts

    for key, chains in compare.items():
        places = PLACES.get(key, 4)
        for pts in chains.values():
            round_pts(pts, places)
    for key, pts in data.get("series", {}).items():
        if isinstance(pts, list):
            round_pts(pts, 0 if key.endswith(("transactions", "volume", "supply", "tvl"))
                      else 4)

    data["_notice"] = (
        "Display data for stateofsol.com, derived from Blockworks, DefiLlama and "
        "CoinGecko and rounded to the precision the page renders. Not a "
        "redistribution of any provider's dataset; please license source data "
        "from the providers directly.")

    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"\nWrote {OUT} ({OUT.stat().st_size:,} bytes) — {len(warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

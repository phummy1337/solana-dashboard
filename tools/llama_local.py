#!/usr/bin/env python3
"""Turn a saved DefiLlama __NEXT_DATA__ payload into a compact series file.

DefiLlama's perps and RWA pages server-render their entire history into the
HTML — every load carries all ~2,000 days, not a delta. So a capture taken on
any day is a complete history, and a missed day costs nothing: the next capture
backfills it. That is why this reads a whole payload and rewrites the series
wholesale rather than appending.

The raw payload is 4.2 MB of every chain DefiLlama tracks. The dashboard shows
eleven. This writes only those, so what lands in the repo stays small enough to
read in a diff and git's delta compression keeps the daily churn cheap.

Usage:
    python3 tools/llama_local.py perps      ~/Documents/defillama_data/next_data_latest.json
    python3 tools/llama_local.py rwa-chains ~/Documents/defillama_data
    python3 tools/llama_local.py rwa        ~/Documents/defillama_data/rwa_next_data_latest.json

rwa-chains reads one capture per chain (rwa_chain_<slug>.json) and is what the
RWA card uses; plain `rwa` reads the all-chains page, which buckets everything
outside its top-N into "Others" and so only yields five of the ten.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "local"

# Our chain keys mapped to the dimension names these pages use. Mostly the same
# spelling the rest of the script uses, with two exceptions worth stating: the
# perps page calls Hyperliquid's chain "Hyperliquid L1" (the bare name is the
# protocol), and Robinhood is "Robinhood Chain".
PERPS_DIMS = {
    "solana": "Solana", "ethereum": "Ethereum", "base": "Base",
    "arbitrum": "Arbitrum", "bnb": "BSC", "avalanche": "Avalanche",
    "sui": "Sui", "tron": "Tron", "hyperevm": "Hyperliquid L1",
    "polygon": "Polygon", "robinhood": "Robinhood Chain",
}

# The all-chains RWA page groups to a top-N and buckets the rest into "Others",
# so only five of our ten are broken out there. Kept for reference; the
# rwa-chains mode below is what actually feeds the card.
RWA_DIMS = {
    "ethereum": "Ethereum", "bnb": "BSC", "solana": "Solana",
    "avalanche": "Avalanche", "arbitrum": "Arbitrum",
}

# Each chain has its own RWA page carrying that chain's full history under a
# "Total Active AUM" column, which is the way to get all ten without the top-N
# bucketing. Keyed by the slug in /rwa/chain/{slug}.
RWA_CHAIN_SLUGS = {
    "ethereum": "ethereum", "bnb": "bsc", "solana": "solana",
    "avalanche": "avalanche", "arbitrum": "arbitrum", "base": "base",
    "polygon": "polygon", "tron": "tron", "sui": "sui",
    "robinhood": "robinhood-chain",
}
RWA_TOTAL_DIM = "Total Active AUM"


def _iso(ts: float) -> str:
    # These payloads are inconsistent about the unit: the perps chart ships
    # milliseconds, some of the others seconds. Anything past 1e11 can only be
    # milliseconds — seconds that large would be the year 5138.
    return datetime.fromtimestamp(ts / 1000 if ts > 1e11 else ts,
                                  timezone.utc).date().isoformat()


def _chart(payload: dict) -> tuple[list[str], list[list]]:
    """Pull (dimensions, rows) out of a __NEXT_DATA__ blob.

    The perps page hands it over as pageProps.chartData; the RWA page wraps the
    same shape in initialChartDatasets, one entry per grouping. Take the
    chain-grouped one explicitly — the platform-grouped entry comes first and
    has an identical shape, so a naive search silently returns issuer names
    where chain names are expected.
    """
    for entry in (payload.get("props", {}).get("pageProps", {})
                  .get("initialChartDatasets") or []):
        if isinstance(entry, dict) and entry.get("groupBy") == "chain":
            ds = entry.get("dataset") or {}
            if ds.get("dimensions") and ds.get("source"):
                return ds["dimensions"], ds["source"]

    found: list[tuple[list, list]] = []

    def walk(node, depth=0):
        if depth > 8 or found:
            return
        if isinstance(node, dict):
            dims, src = node.get("dimensions"), node.get("source")
            if (isinstance(dims, list) and isinstance(src, list) and dims and src
                    and str(dims[0]).lower() in ("timestamp", "date", "time")):
                found.append((dims, src))
                return
            for v in node.values():
                walk(v, depth + 1)
        elif isinstance(node, list):
            for v in node:
                walk(v, depth + 1)

    walk(payload)
    if not found:
        raise SystemExit("no dimensions/source chart found in this payload — "
                         "is it the right page?")
    return found[0]


def extract(kind: str, path: Path) -> dict:
    dim_map = PERPS_DIMS if kind == "perps" else RWA_DIMS
    dims, rows = _chart(json.loads(path.read_text()))
    index = {name: i for i, name in enumerate(dims)}

    missing = [k for k, name in dim_map.items() if name not in index]
    if missing:
        print(f"  !! not present on this page: {', '.join(sorted(missing))}",
              file=sys.stderr)

    out: dict[str, dict[str, float]] = {}
    for key, name in dim_map.items():
        i = index.get(name)
        if i is None:
            continue
        series = {}
        for row in rows:
            v = row[i] if i < len(row) else None
            # A zero here means "no venue reported", not "no volume" — keeping
            # it would draw a floor line across years the chain did not exist.
            if isinstance(v, (int, float)) and v > 0:
                series[_iso(row[0])] = float(v)
        if series:
            out[key] = series

    if not out:
        raise SystemExit("matched no chains — the dimension names likely changed")
    return out


def extract_rwa_chains(folder: Path) -> dict:
    """Build the RWA series from one capture per chain.

    The all-chains page buckets everything outside its top-N into "Others", so
    it can only ever produce five of the ten chains this card shows. Each
    chain's own page carries that chain's full history instead, under a single
    "Total Active AUM" column — ten captures, no bucketing.

    A chain whose capture is absent is simply left out; refresh_data.py refuses
    a capture that drops chains it is already publishing, so a partial folder
    falls back rather than silently shrinking the card.
    """
    out: dict[str, dict[str, float]] = {}
    for key, slug in RWA_CHAIN_SLUGS.items():
        path = folder / f"rwa_chain_{slug}.json"
        if not path.exists():
            print(f"  .. no capture for {key} ({path.name})", file=sys.stderr)
            continue
        dims, rows = _chart(json.loads(path.read_text()))
        if RWA_TOTAL_DIM not in dims:
            print(f"  !! {key}: no '{RWA_TOTAL_DIM}' column", file=sys.stderr)
            continue
        i = dims.index(RWA_TOTAL_DIM)
        series = {}
        for row in rows:
            v = row[i] if i < len(row) else None
            if isinstance(v, (int, float)) and v > 0:
                series[_iso(row[0])] = float(v)
        if series:
            out[key] = series

    if not out:
        raise SystemExit(f"no rwa_chain_*.json captures found in {folder}")
    return out


def main() -> None:
    if len(sys.argv) != 3 or sys.argv[1] not in ("perps", "rwa", "rwa-chains"):
        raise SystemExit(__doc__)
    kind, path = sys.argv[1], Path(sys.argv[2]).expanduser()
    if not path.exists():
        raise SystemExit(f"no such path: {path}")

    series = extract_rwa_chains(path) if kind == "rwa-chains" else extract(kind, path)
    dates = [d for s in series.values() for d in s]
    payload = {
        "source": "defillama front end (__NEXT_DATA__)",
        "captured": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "first": min(dates),
        "last": max(dates),
        "chains": {k: dict(sorted(v.items())) for k, v in sorted(series.items())},
    }

    OUT_DIR.mkdir(exist_ok=True)
    dest = OUT_DIR / ("perps_volume.json" if kind == "perps" else "rwa_aum.json")
    dest.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=False))

    print(f"{dest.relative_to(REPO)}  {dest.stat().st_size // 1024} KB")
    print(f"  {payload['first']} -> {payload['last']}")
    for k, v in payload["chains"].items():
        print(f"  {k:<11} {len(v):>5} days   last {max(v)} = {v[max(v)]:,.0f}")


if __name__ == "__main__":
    main()

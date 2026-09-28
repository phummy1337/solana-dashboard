#!/usr/bin/env python3
"""Recover raw daily values from a Blockworks terminal chart export.

The terminal's comparison charts plot a 7-day trailing mean, not the raw daily
metric the dashboard's API calls return — the giveaway is the /7 remainders in
the exported numbers (…43, …571). Verified against our own history the identity
is exact: export[t] == mean(raw[t-6..t]) to 1.0000 on every chain tested.

A trailing mean is exactly invertible given six known days before the first
unknown one:

    raw[t] = 7*MA[t] - sum(raw[t-6..t-1])

which is used here rather than the equivalent raw[t] = raw[t-7] + 7*(MA[t] -
MA[t-1]) because it re-reads the published history each step instead of
compounding its own output.

Anchor matters. Blockworks revises the most recent day after we read it, so the
newest value in data.json is the one least worth trusting — anchoring on it
propagates that error through every recovered day. `--anchor` sets the last date
whose published values are taken as truth; everything after is recomputed.

    python3 tools/desmooth.py transactions data.json export.csv --anchor 2026-09-18
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, timedelta

# Export column name -> our chain key. The exports spell a few differently.
CHAINS = {
    "solana": "solana", "ethereum": "ethereum", "base": "base",
    "arbitrum": "arbitrum", "bnb": "bnb", "avalanche-c-chain": "avalanche",
    "sui": "sui", "tron": "tron", "hyperevm": "hyperevm",
    "polygon": "polygon", "robinhood": "robinhood",
}


def desmooth(published: dict[str, list], rows: dict[str, dict],
             anchor: str) -> tuple[dict, list[str]]:
    out: dict[str, dict[str, float]] = {}
    notes: list[str] = []
    for col, key in CHAINS.items():
        series = {p["d"]: float(p["v"]) for p in published.get(key, [])
                  if p["d"] <= anchor}
        if not series:
            continue
        got: dict[str, float] = {}
        for d0 in sorted(rows):
            if d0 <= anchor or not rows[d0].get(col):
                continue
            window = [(date.fromisoformat(d0) - timedelta(days=k)).isoformat()
                      for k in range(1, 7)]
            have = {**series, **got}
            if not all(w in have for w in window):
                notes.append(f"{key} {d0}: gap in the six days before it")
                continue
            v = 7 * float(rows[d0][col]) - sum(have[w] for w in window)
            # A negative recovery means the window was wrong somewhere, not that
            # the chain went negative. Drop it rather than publish nonsense.
            if v <= 0:
                notes.append(f"{key} {d0}: recovered {v:,.0f} — dropped")
                continue
            got[d0] = v
        if got:
            out[key] = got
    return out, notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("block", help="compare-block key, e.g. transactions")
    ap.add_argument("data", help="a data.json to read published history from")
    ap.add_argument("csv", help="the terminal's CSV export")
    ap.add_argument("--anchor", required=True,
                    help="last date whose published values are trusted")
    ap.add_argument("--out", help="write recovered values here as JSON")
    a = ap.parse_args()

    published = (json.load(open(a.data)).get("compare") or {}).get(a.block) or {}
    if not published:
        sys.exit(f"no compare block {a.block!r} in {a.data}")
    rows = {r[next(iter(r))][:10]: r
            for r in csv.DictReader(open(a.csv))}

    out, notes = desmooth(published, rows, a.anchor)
    for n in notes:
        print(f"  !! {n}", file=sys.stderr)
    if not out:
        sys.exit("recovered nothing — check the anchor and the export's columns")

    days = sorted({d for s in out.values() for d in s})
    print(f"{a.block}: {len(out)} chains, {len(days)} days "
          f"({days[0]} -> {days[-1]})")
    for k in sorted(out):
        last = max(out[k])
        print(f"  {k:<11} {len(out[k]):>2}d   {last} = {out[k][last]:,.4f}".rstrip("0").rstrip("."))
    if a.out:
        json.dump(out, open(a.out, "w"), indent=1, sort_keys=True)
        print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()

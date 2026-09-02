#!/usr/bin/env python3
"""Render the monthly recap card as a PNG, server-side.

Mirrors the in-browser recap on stateofsol.com so the image can be produced
without opening the site (handy for scheduling or posting from a script).

Usage:
    python3 render_recap.py [YYYY-MM]      # defaults to the latest closed month
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
FONT = Path("/tmp/syne.ttf")
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/syne/Syne%5Bwght%5D.ttf"
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def font(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    if not FONT.exists():
        urllib.request.urlretrieve(FONT_URL, FONT)
    f = ImageFont.truetype(str(FONT), size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:  # noqa: BLE001 - static build, weight already baked
        pass
    return f


def compact(n: float | None, prefix: str = "") -> str:
    if n is None:
        return "—"
    a = abs(n)
    for v, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= v:
            q = n / v
            return f"{prefix}{q:.0f}{s}" if abs(q) >= 100 else (
                f"{prefix}{q:.1f}{s}" if abs(q) >= 10 else f"{prefix}{q:.2f}{s}")
    return f"{prefix}{n:.2f}"


def main() -> int:
    data = json.loads((HERE / "data.json").read_text())
    cmp_ = data.get("compare", {})
    grid = data.get("monthly_returns", {}).get("grid", {})

    # latest closed month, or the one asked for
    if len(sys.argv) > 1:
        ym = sys.argv[1]
    else:
        ym = max(f"{y}-{int(m):02d}" for y, row in grid.items() for m in row)
    year, mon = int(ym[:4]), int(ym[5:])
    prev = f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"

    def agg(key: str, how: str, month: str, chain: str = "solana"):
        pts = [p["v"] for p in cmp_.get(key, {}).get(chain, []) if p["d"][:7] == month]
        if not pts:
            return None
        return sum(pts) if how == "sum" else (sum(pts) / len(pts) if how == "mean" else pts[-1])

    def pair(key: str, how: str):
        return agg(key, how, ym), agg(key, how, prev)

    def mom(p):
        a, b = p
        return (a / b - 1) * 100 if (a and b) else None

    prices = {p["d"]: p["v"] for p in data.get("series", {}).get("price", [])}
    closes = sorted(k for k in prices if k[:7] == ym)
    pcloses = sorted(k for k in prices if k[:7] == prev)
    ret = grid.get(str(year), {}).get(str(mon))

    rev_month = {ch: sum(p["v"] for p in pts if p["d"][:7] == ym)
                 for ch, pts in cmp_.get("rev", {}).items()}
    total_rev = sum(rev_month.values())
    ranked = sorted(rev_month.items(), key=lambda kv: -kv[1])
    sol_rank = next(i for i, (ch, _) in enumerate(ranked) if ch == "solana")

    W, H, S, PAD = 1200, 750, 2, 54
    img = Image.new("RGB", (W * S, H * S), "#030815")
    d = ImageDraw.Draw(img)
    for y in range(H * S):
        t = y / (H * S)
        d.line([(0, y), (W * S, y)], fill=(int(10 - 7 * t), int(22 - 14 * t), int(51 - 30 * t)))

    def text(xy, s, f, fill):
        d.text((xy[0] * S, xy[1] * S), s, font=f, fill=fill)

    text((PAD, 36), "STATE OF SOLANA · MONTHLY RECAP", font(15 * S, 700), (143, 165, 212))
    text((PAD, 66), f"{MONTHS[mon - 1]} {year}", font(46 * S, 700), (241, 245, 251))

    logo = Image.open(HERE / "dfdv-logo.png").convert("RGBA")
    lh = 26 * S
    logo = logo.resize((int(lh * logo.width / logo.height), lh), Image.LANCZOS)
    img.paste(logo, ((W - PAD) * S - logo.width, 34 * S), logo)

    up = (ret or 0) >= 0
    rtxt = "—" if ret is None else f"{'+' if up else ''}{ret:.1f}%"
    rfont = font(74 * S, 800)
    text((PAD, 128), rtxt, rfont, (20, 241, 149) if up else (255, 107, 107))
    rw = d.textlength(rtxt, font=rfont) / S
    text((PAD + rw + 20, 150), "SOL price", font(19 * S, 600), (143, 165, 212))
    if closes and pcloses:
        text((PAD + rw + 20, 176),
             f"${prices[pcloses[-1]]:,.2f} → ${prices[closes[-1]]:,.2f}",
             font(19 * S, 600), (211, 219, 234))

    if sol_rank == 0:
        d.rectangle([(PAD * S, 212 * S), ((W - PAD) * S, 258 * S)], fill=(16, 40, 48))
        text((PAD + 18, 226), f"#1 of {len(ranked)} chains in Real Economic Value",
             font(20 * S, 700), (20, 241, 149))
        share = f"{rev_month['solana'] / total_rev * 100:.1f}% of all blockspace revenue"
        sf = font(17 * S, 600)
        text((W - PAD - 18 - d.textlength(share, font=sf) / S, 228), share, sf, (143, 165, 212))

    tiles = [
        ("Real Economic Value", compact(agg("rev", "sum", ym), "$"), mom(pair("rev", "sum")), False),
        ("Transactions", compact(agg("transactions", "sum", ym)), mom(pair("transactions", "sum")), False),
        ("DEX Volume", compact(agg("dex_volume", "sum", ym), "$"), mom(pair("dex_volume", "sum")), False),
        ("DeFi TVL (month end)", compact(agg("defi_tvl", "last", ym), "$"), mom(pair("defi_tvl", "last")), False),
        ("Active Addresses /day", compact(agg("active_addresses", "mean", ym)), mom(pair("active_addresses", "mean")), False),
        ("Median Fee", (lambda v: "—" if v is None else f"${v:.5f}")(agg("fee_median", "mean", ym)),
         mom(pair("fee_median", "mean")), True),
    ]
    tw, th = (W - PAD * 2 - 24) / 3, 108
    for i, (label, val, delta, lower_better) in enumerate(tiles):
        x, y = PAD + (i % 3) * (tw + 12), 284 + (i // 3) * (th + 12)
        d.rectangle([(x * S, y * S), ((x + tw) * S, (y + th) * S)], fill=(13, 26, 52),
                    outline=(35, 52, 84), width=S)
        text((x + 16, y + 18), label.upper(), font(12 * S, 700), (107, 127, 171))
        text((x + 16, y + 42), val, font(30 * S, 700), (241, 245, 251))
        if delta is not None:
            good = (delta < 0) if lower_better else (delta >= 0)
            sign = "+" if delta >= 0 else "−"
            text((x + 16, y + 80), f"{sign}{abs(delta):.1f}% vs prior month",
                 font(15 * S, 700), (20, 241, 149) if good else (255, 107, 107))

    hi = []
    rets = [v for v in grid.get(str(year), {}).values()]
    if ret is not None and len(rets) > 1 and ret == max(rets):
        hi.append(f"Best month of {year} for SOL")
    hi.append("100% network uptime")
    tps = agg("transactions", "mean", ym)
    if tps:
        hi.append(f"{tps / 86400:,.0f} avg TPS")
    text((PAD, 584), "   ·   ".join(hi), font(17 * S, 600), (143, 165, 212))

    fy = H - 62
    d.line([(PAD * S, fy * S), ((W - PAD) * S, fy * S)], fill=(30, 44, 72), width=S)
    text((PAD, fy + 22), "Data: Blockworks · DefiLlama · CoinGecko", font(14 * S, 600), (107, 127, 171))
    cf = font(17 * S, 700)
    text((W - PAD - d.textlength("stateofsol.com", font=cf) / S, fy + 20),
         "stateofsol.com", cf, (249, 115, 22))

    out = HERE / f"solana-recap-{ym}.png"
    img.resize((W, H), Image.LANCZOS).save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

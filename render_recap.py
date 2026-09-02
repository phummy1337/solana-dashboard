#!/usr/bin/env python3
"""Render the monthly Solana recap card as a shareable PNG.

On-chain figures come from data.json (the same series the dashboard uses), so
they are reproducible. MILESTONES is hand-curated per month — edit it before
publishing, and keep "announced" separate from "live".

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

INK, MUTE, DIM = (241, 245, 251), (143, 165, 212), (107, 127, 171)
MINT, RED, ORANGE = (20, 241, 149), (255, 107, 107), (249, 115, 22)

# Curated headlines. Each entry: (text, kind) where kind tints the marker —
# "live" for something shipped, "soon" for announced-but-not-yet-live.
MILESTONES: dict[str, list[tuple[str, str]]] = {
    "2026-08": [
        ("First binding on-chain governance vote passes: SOL disinflation doubles "
         "to 30%/yr, pulling terminal 1.5% inflation forward to about 2029", "live"),
        ("Mainnet slot times cut 400ms → 350ms → 300ms — the first block-time "
         "reduction since genesis, with 200ms already on testnet", "live"),
        ("US spot SOL ETFs post their strongest month; Bitwise BSOL tops $1B AUM "
         "with 96% of holdings staked", "live"),
        ("Real-world assets on Solana hit a $4B all-time high; #1 chain for "
         "30-day tokenized Treasury inflows", "live"),
        ("Western Union launches its Solana-based stablecoin card across "
         "37 markets", "live"),
        ("Charles Schwab announces spot SOL trading for about 40M brokerage accounts "
         "(planned, not yet live)", "soon"),
    ],
}


def font(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    if not FONT.exists():
        urllib.request.urlretrieve(FONT_URL, FONT)
    f = ImageFont.truetype(str(FONT), size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:  # noqa: BLE001 - static build, weight already baked in
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

    ym = sys.argv[1] if len(sys.argv) > 1 else max(
        f"{y}-{int(m):02d}" for y, row in grid.items() for m in row)
    year, mon = int(ym[:4]), int(ym[5:])
    prev = f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"

    def agg(key, how, month, chain="solana"):
        pts = [p["v"] for p in cmp_.get(key, {}).get(chain, []) if p["d"][:7] == month]
        if not pts:
            return None
        return sum(pts) if how == "sum" else (sum(pts) / len(pts) if how == "mean" else pts[-1])

    def mom(key, how):
        a, b = agg(key, how, ym), agg(key, how, prev)
        return (a / b - 1) * 100 if (a and b) else None

    prices = {p["d"]: p["v"] for p in data.get("series", {}).get("price", [])}
    month_px = [(k, prices[k]) for k in sorted(prices) if k[:7] == ym]
    pcloses = sorted(k for k in prices if k[:7] == prev)
    ret = grid.get(str(year), {}).get(str(mon))

    tx = {ch: sum(p["v"] for p in pts if p["d"][:7] == ym)
          for ch, pts in cmp_.get("transactions", {}).items()}
    sol_tx = tx.get("solana", 0)
    other_tx = sum(v for ch, v in tx.items() if ch != "solana")

    rev = {ch: sum(p["v"] for p in pts if p["d"][:7] == ym)
           for ch, pts in cmp_.get("rev", {}).items()}
    rev_rank = sorted(rev.items(), key=lambda kv: -kv[1])
    sol_rev_rank = next(i for i, (ch, _) in enumerate(rev_rank) if ch == "solana") + 1

    W, S, PAD = 1200, 2, 56
    # Pre-measure the milestone block so the card is exactly as tall as its
    # content — a fixed height left a large dead band under short months.
    _probe = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    _mf = font(17 * S, 600)
    def _wrap(txt, limit=W - PAD * 2 - 30):
        out, line = [], ""
        for w in txt.split():
            trial = f"{line} {w}".strip()
            if _probe.textlength(trial, font=_mf) / S > limit:
                out.append(line); line = w
            else:
                line = trial
        out.append(line)
        return out
    _wrapped = [(_wrap(t), k) for t, k in MILESTONES.get(ym, [])]
    _mheight = sum(len(ls) * 25 + 14 for ls, _ in _wrapped)
    H = 640 + _mheight + 96
    img = Image.new("RGB", (W * S, H * S), "#030815")
    d = ImageDraw.Draw(img)
    for y in range(H * S):
        t = y / (H * S)
        d.line([(0, y), (W * S, y)], fill=(int(10 - 7 * t), int(22 - 14 * t), int(51 - 30 * t)))

    def text(xy, s, f, fill):
        d.text((xy[0] * S, xy[1] * S), s, font=f, fill=fill)

    def width(s, f):
        return d.textlength(s, font=f) / S

    def box(x, y, w, h, fill=(13, 26, 52), outline=(35, 52, 84)):
        d.rectangle([(x * S, y * S), ((x + w) * S, (y + h) * S)], fill=fill,
                    outline=outline, width=S)

    # ---------------------------------------------------------------- header
    text((PAD, 34), "STATE OF SOLANA · MONTHLY RECAP", font(15 * S, 700), MUTE)
    text((PAD, 62), f"{MONTHS[mon - 1]} {year}", font(48 * S, 700), INK)
    logo = Image.open(HERE / "dfdv-logo.png").convert("RGBA")
    lh = 28 * S
    logo = logo.resize((int(lh * logo.width / logo.height), lh), Image.LANCZOS)
    img.paste(logo, ((W - PAD) * S - logo.width, 34 * S), logo)

    # ------------------------------------------------------ price + sparkline
    up = (ret or 0) >= 0
    rtxt = "—" if ret is None else f"{'+' if up else ''}{ret:.1f}%"
    rf = font(72 * S, 800)
    text((PAD, 132), rtxt, rf, MINT if up else RED)
    rw = width(rtxt, rf)
    text((PAD + rw + 18, 150), "SOL price", font(18 * S, 600), MUTE)
    if month_px and pcloses:
        text((PAD + rw + 18, 174),
             f"${prices[pcloses[-1]]:,.2f} → ${month_px[-1][1]:,.2f}",
             font(18 * S, 600), (211, 219, 234))
        lo, hi = min(v for _, v in month_px), max(v for _, v in month_px)
        text((PAD + rw + 18, 198), f"range ${lo:,.0f}–${hi:,.0f}", font(15 * S, 600), DIM)

    if len(month_px) > 2:
        sx, sy, sw, sh = W - PAD - 340, 132, 340, 86
        lo, hi = min(v for _, v in month_px), max(v for _, v in month_px)
        span = (hi - lo) or 1
        pts = [(sx + i / (len(month_px) - 1) * sw, sy + sh - (v - lo) / span * sh)
               for i, (_, v) in enumerate(month_px)]
        d.line([(x * S, y * S) for x, y in pts], fill=MINT, width=3 * S, joint="curve")
        d.ellipse([((pts[-1][0] - 4) * S, (pts[-1][1] - 4) * S),
                   ((pts[-1][0] + 4) * S, (pts[-1][1] + 4) * S)], fill=MINT)

    # --------------------------------------------- hero: transactions vs rest
    y0 = 254
    text((PAD, y0), "NON-VOTE TRANSACTIONS THIS MONTH", font(14 * S, 700), DIM)
    label_w, bar_h = 210, 52
    bar_x = PAD + label_w
    bar_w = W - PAD * 2 - label_w - 150
    peak = max(sol_tx, other_tx) or 1

    def bar(y, label, value, fill, val_colour):
        lf = font(17 * S, 800)
        text((PAD, y + 16), label, lf, INK)
        w = max(4, bar_w * value / peak)
        d.rectangle([(bar_x * S, y * S), ((bar_x + w) * S, (y + bar_h) * S)], fill=fill)
        text((bar_x + w + 14, y + 12), compact(value), font(25 * S, 800), val_colour)

    by = y0 + 32
    bar(by, "SOLANA", sol_tx, (20, 241, 149), INK)
    by2 = by + bar_h + 14
    bar(by2, f"ALL {len(tx) - 1} OTHERS", other_tx, (60, 78, 120), MUTE)

    if other_tx:
        cap = f"Solana settled {sol_tx / other_tx:.1f}× more transactions than every other major chain combined"
        text((PAD, by2 + bar_h + 20), cap, font(17 * S, 600), MINT)

    # ------------------------------------------------------------ stat tiles
    ty = by2 + bar_h + 58
    tiles = [
        ("Real Economic Value", compact(agg("rev", "sum", ym), "$"), mom("rev", "sum"), False,
         f"#{sol_rev_rank} of {len(rev)} chains"),
        ("DEX Volume", compact(agg("dex_volume", "sum", ym), "$"), mom("dex_volume", "sum"), False, ""),
        ("Active Addresses /day", compact(agg("active_addresses", "mean", ym)),
         mom("active_addresses", "mean"), False, ""),
        ("Median Fee", (lambda v: "—" if v is None else f"${v:.5f}")(agg("fee_median", "mean", ym)),
         mom("fee_median", "mean"), True, ""),
    ]
    tw, th = (W - PAD * 2 - 36) / 4, 116
    for i, (label, val, delta, lower_better, extra) in enumerate(tiles):
        x = PAD + i * (tw + 12)
        box(x, ty, tw, th)
        text((x + 14, ty + 16), label.upper(), font(11 * S, 700), DIM)
        text((x + 14, ty + 38), val, font(28 * S, 700), INK)
        if delta is not None:
            good = (delta < 0) if lower_better else (delta >= 0)
            text((x + 14, ty + 78), f"{'+' if delta >= 0 else '−'}{abs(delta):.1f}% MoM",
                 font(14 * S, 700), MINT if good else RED)
        if extra:
            ef = font(14 * S, 700)
            text((x + tw - 14 - width(extra, ef), ty + 78), extra, ef, MINT)

    # ------------------------------------------------------------ milestones
    my = ty + th + 44
    text((PAD, my), "WHAT HAPPENED", font(14 * S, 700), DIM)
    my += 30
    for lines, kind in _wrapped:
        colour = MINT if kind == "live" else ORANGE
        d.ellipse([((PAD + 3) * S, (my + 9) * S), ((PAD + 11) * S, (my + 17) * S)], fill=colour)
        for j, ln in enumerate(lines):
            text((PAD + 26, my + j * 25), ln, _mf, INK if j == 0 else (196, 208, 230))
        my += len(lines) * 25 + 14

    # ---------------------------------------------------------------- footer
    fy = H - 66
    d.line([(PAD * S, fy * S), ((W - PAD) * S, fy * S)], fill=(30, 44, 72), width=S)
    text((PAD, fy + 22), "On-chain data: Blockworks · DefiLlama · CoinGecko",
         font(14 * S, 600), DIM)
    cf = font(17 * S, 700)
    text((W - PAD - width("stateofsol.com", cf), fy + 20), "stateofsol.com", cf, ORANGE)

    out = HERE / f"solana-recap-{ym}.png"
    img.resize((W, H), Image.LANCZOS).save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    if ym not in MILESTONES:
        print(f"  note: no curated milestones for {ym} — add them to MILESTONES")
    return 0


if __name__ == "__main__":
    sys.exit(main())

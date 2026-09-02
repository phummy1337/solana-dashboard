#!/usr/bin/env python3
"""Render the monthly Solana recap card as a shareable PNG.

Every figure comes from data.json — the same series the dashboard uses — so
the card is reproducible and matches the site.

Type follows the dashboard's own convention: Syne for headings and labels,
a neutral sans (Inter) for numerals, because Syne's heavy cuts are very wide
and its figures wander at large sizes.

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
FONTS = {
    "syne": (Path("/tmp/syne.ttf"),
             "https://github.com/google/fonts/raw/main/ofl/syne/Syne%5Bwght%5D.ttf"),
    "inter": (Path("/tmp/inter.ttf"),
              "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bopsz,wght%5D.ttf"),
}
MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
MON3 = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

INK, MUTE, DIM = (241, 245, 251), (143, 165, 212), (107, 127, 171)
MINT, RED, ORANGE = (20, 241, 149), (255, 107, 107), (249, 115, 22)
PANEL, EDGE = (13, 26, 52), (35, 52, 84)
CHAIN_LABEL = {"solana": "Solana", "ethereum": "Ethereum", "base": "Base",
               "arbitrum": "Arbitrum", "bnb": "BNB", "avalanche": "Avalanche",
               "sui": "Sui", "tron": "Tron", "hyperevm": "Hyperliquid",
               "polygon": "Polygon"}

_cache: dict = {}


def _font(name: str, size: int, axes: list[int]) -> ImageFont.FreeTypeFont:
    key = (name, size, tuple(axes))
    if key in _cache:
        return _cache[key]
    path, url = FONTS[name]
    if not path.exists():
        urllib.request.urlretrieve(url, path)
    f = ImageFont.truetype(str(path), size)
    try:
        f.set_variation_by_axes(axes)
    except Exception:  # noqa: BLE001 - static build; weight already baked in
        pass
    _cache[key] = f
    return f


def f_syne(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    return _font("syne", size, [weight])


def f_num(size: int, weight: int = 700) -> ImageFont.FreeTypeFont:
    # Inter is variable on (opsz, wght); pin optical size so weights stay stable
    return _font("inter", size, [32, weight])


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

    def series(key, chain="solana"):
        return [(p["d"], p["v"]) for p in cmp_.get(key, {}).get(chain, [])
                if p["d"][:7] == ym]

    def agg(key, how, month=None, chain="solana"):
        m = month or ym
        pts = [p["v"] for p in cmp_.get(key, {}).get(chain, []) if p["d"][:7] == m]
        if not pts:
            return None
        return sum(pts) if how == "sum" else (sum(pts) / len(pts) if how == "mean" else pts[-1])

    def mom(key, how):
        a, b = agg(key, how), agg(key, how, prev)
        return (a / b - 1) * 100 if (a and b) else None

    prices = {p["d"]: p["v"] for p in data.get("series", {}).get("price", [])}
    month_px = [(k, prices[k]) for k in sorted(prices) if k[:7] == ym]
    pcloses = sorted(k for k in prices if k[:7] == prev)
    ret = grid.get(str(year), {}).get(str(mon))

    tx = {ch: sum(p["v"] for p in pts if p["d"][:7] == ym)
          for ch, pts in cmp_.get("transactions", {}).items()}
    sol_tx, other_tx = tx.get("solana", 0), sum(v for c, v in tx.items() if c != "solana")
    rev = {ch: sum(p["v"] for p in pts if p["d"][:7] == ym)
           for ch, pts in cmp_.get("rev", {}).items()}
    rev_rank = sorted(rev.items(), key=lambda kv: -kv[1])
    rev_total = sum(rev.values()) or 1

    # Layout is a fixed stack, so derive the canvas height from it rather than
    # hard-coding one that leaves a dead band above the footer.
    PY_, PH_, RH_, QH_, SH_, GAP = 132, 208, 248, 226, 104, 16
    W, S, PAD = 1200, 2, 56
    H = PY_ + PH_ + GAP + RH_ + GAP + QH_ + GAP + SH_ + 74
    img = Image.new("RGB", (W * S, H * S), "#030815")
    d = ImageDraw.Draw(img)
    for y in range(H * S):
        t = y / (H * S)
        d.line([(0, y), (W * S, y)], fill=(int(10 - 7 * t), int(22 - 14 * t), int(51 - 30 * t)))

    def text(xy, s, f, fill, anchor=None):
        d.text((xy[0] * S, xy[1] * S), s, font=f, fill=fill, anchor=anchor)

    def wide(s, f):
        return d.textlength(s, font=f) / S

    def panel(x, y, w, h, title):
        d.rectangle([(x * S, y * S), ((x + w) * S, (y + h) * S)],
                    fill=PANEL, outline=EDGE, width=S)
        text((x + 18, y + 15), title.upper(), f_syne(13 * S, 700), DIM)

    # ---------------------------------------------------------------- header
    text((PAD, 34), "STATE OF SOLANA · MONTHLY RECAP", f_syne(15 * S, 700), MUTE)
    text((PAD, 60), f"{MONTHS[mon - 1]} {year}", f_syne(46 * S, 700), INK)
    logo = Image.open(HERE / "dfdv-logo.png").convert("RGBA")
    lh = 28 * S
    logo = logo.resize((int(lh * logo.width / logo.height), lh), Image.LANCZOS)
    img.paste(logo, ((W - PAD) * S - logo.width, 36 * S), logo)

    # ------------------------------------------------ 1. price, full width
    py, ph = PY_, PH_
    panel(PAD, py, W - PAD * 2, ph, "SOL price")
    up = (ret or 0) >= 0
    rtxt = "—" if ret is None else f"{'+' if up else ''}{ret:.1f}%"
    rf = f_num(56 * S, 800)
    text((PAD + 18, py + 44), rtxt, rf, MINT if up else RED)
    rw = wide(rtxt, rf)
    if month_px and pcloses:
        text((PAD + 26 + rw, py + 52),
             f"${prices[pcloses[-1]]:,.2f}  →  ${month_px[-1][1]:,.2f}",
             f_num(20 * S, 600), (211, 219, 234))
        lo, hi = min(v for _, v in month_px), max(v for _, v in month_px)
        text((PAD + 26 + rw, py + 80), f"low ${lo:,.0f}    high ${hi:,.0f}",
             f_num(16 * S, 500), DIM)
    if len(month_px) > 2:
        cx, cy = PAD + 500, py + 38
        cw, chh = W - PAD * 2 - 520, ph - 62
        lo, hi = min(v for _, v in month_px), max(v for _, v in month_px)
        span = (hi - lo) or 1
        pts = [(cx + i / (len(month_px) - 1) * cw, cy + chh - (v - lo) / span * chh)
               for i, (_, v) in enumerate(month_px)]
        d.polygon([(x * S, y * S) for x, y in pts] +
                  [((cx + cw) * S, (cy + chh) * S), (cx * S, (cy + chh) * S)],
                  fill=(11, 56, 58))
        d.line([(x * S, y * S) for x, y in pts], fill=MINT, width=3 * S, joint="curve")
        d.ellipse([((pts[-1][0] - 5) * S, (pts[-1][1] - 5) * S),
                   ((pts[-1][0] + 5) * S, (pts[-1][1] + 5) * S)], fill=MINT)

    # ---------------------- 2 & 3. transactions vs peers | REV leaderboard
    ry, rh = py + ph + GAP, RH_
    hw = (W - PAD * 2 - 16) / 2

    panel(PAD, ry, hw, rh, "Non-vote transactions")
    peak = max(sol_tx, other_tx) or 1
    bx, bw, bh = PAD + 18, hw - 36, 46
    for i, (label, val, fill, col) in enumerate([
            ("Solana", sol_tx, MINT, INK),
            (f"All {len(tx) - 1} other chains", other_tx, (60, 78, 120), MUTE)]):
        by = ry + 62 + i * (bh + 44)
        text((bx, by - 22), label, f_syne(15 * S, 700), INK)
        w = max(6, bw * val / peak)
        d.rectangle([(bx * S, by * S), ((bx + w) * S, (by + bh) * S)], fill=fill)
        vf = f_num(24 * S, 800)
        vw = wide(compact(val), vf)
        inside = w > vw + 30
        text((bx + w - vw - 14 if inside else bx + w + 12, by + 12), compact(val), vf,
             (4, 30, 22) if inside else col)
    if other_tx:
        text((bx, ry + rh - 36), f"{sol_tx / other_tx:.1f}× every other chain combined",
             f_syne(16 * S, 700), MINT)

    x2 = PAD + hw + 16
    panel(x2, ry, hw, rh, f"Real economic value · share of {len(rev)} chains")
    top = rev_rank[:5]
    pk = top[0][1] or 1
    for i, (ch, v) in enumerate(top):
        by = ry + 54 + i * 37
        text((x2 + 18, by + 3), CHAIN_LABEL.get(ch, ch), f_syne(14 * S, 700),
             INK if ch == "solana" else MUTE)
        bx2, bw2 = x2 + 132, hw - 262
        w = max(4, bw2 * v / pk)
        d.rectangle([(bx2 * S, (by + 2) * S), ((bx2 + w) * S, (by + 22) * S)],
                    fill=MINT if ch == "solana" else (60, 78, 120))
        text((x2 + hw - 18, by + 3), f"{v / rev_total * 100:.1f}%", f_num(15 * S, 700),
             MINT if ch == "solana" else MUTE, anchor="ra")

    # ------------------ 4 & 5. daily DEX volume | monthly returns this year
    qy, qh = ry + rh + GAP, QH_
    panel(PAD, qy, hw, qh, "Daily DEX volume")
    dex = series("dex_volume")
    if dex:
        vals = [v for _, v in dex]
        pk = max(vals) or 1
        gx, gy, gw, gh = PAD + 18, qy + 48, hw - 36, qh - 104
        bwid = gw / len(vals)
        for i, v in enumerate(vals):
            hgt = max(2, gh * v / pk)
            d.rectangle([((gx + i * bwid + 1) * S, (gy + gh - hgt) * S),
                         ((gx + (i + 1) * bwid - 1) * S, (gy + gh) * S)], fill=MINT)
        text((gx, qy + qh - 44), compact(sum(vals), "$"), f_num(22 * S, 700), INK)
        text((gx, qy + qh - 20), "month total", f_syne(13 * S, 600), DIM)
        text((gx + gw, qy + qh - 42), f"peak day {compact(pk, '$')}",
             f_num(15 * S, 600), DIM, anchor="ra")

    panel(x2, qy, hw, qh, f"SOL monthly return · {year}")
    rows = grid.get(str(year), {})
    months = sorted(int(k) for k in rows)
    if months:
        mx = max(abs(rows[str(m)]) for m in months) or 1
        gx, gy, gw, gh = x2 + 18, qy + 50, hw - 36, qh - 118
        mid = gy + gh / 2
        d.line([(gx * S, mid * S), ((gx + gw) * S, mid * S)], fill=EDGE, width=S)
        bwid = gw / len(months)
        for i, m in enumerate(months):
            v = rows[str(m)]
            hgt = (gh / 2) * abs(v) / mx
            top_y, bot_y = (mid - hgt, mid) if v >= 0 else (mid, mid + hgt)
            cur = (m == mon)
            col = (MINT if v >= 0 else RED) if cur else ((16, 120, 90) if v >= 0 else (128, 58, 64))
            d.rectangle([((gx + i * bwid + 4) * S, top_y * S),
                         ((gx + (i + 1) * bwid - 4) * S, bot_y * S)], fill=col)
            text((gx + (i + 0.5) * bwid, gy + gh + 10), MON3[m - 1], f_syne(12 * S, 700),
                 INK if cur else DIM, anchor="ma")
        text((gx, qy + qh - 44), f"{MON3[mon - 1]} {rtxt}", f_num(22 * S, 700),
             MINT if up else RED)
        if ret is not None and ret == max(rows.values()):
            text((gx + gw, qy + qh - 42), f"best month of {year}", f_syne(14 * S, 700),
                 MINT, anchor="ra")

    # -------------------------------------------------------- 6. stat strip
    sy, sh = qy + qh + GAP, SH_
    stats = [
        ("Real economic value", compact(agg("rev", "sum"), "$"), mom("rev", "sum"), False),
        ("Active addresses /day", compact(agg("active_addresses", "mean")),
         mom("active_addresses", "mean"), False),
        ("Transactions /second", f"{(agg('transactions', 'mean') or 0) / 86400:,.0f}",
         mom("transactions", "mean"), False),
        ("Median fee", (lambda v: "—" if v is None else f"${v:.5f}")(agg("fee_median", "mean")),
         mom("fee_median", "mean"), True),
        ("DeFi TVL", compact(agg("defi_tvl", "last"), "$"), mom("defi_tvl", "last"), False),
    ]
    sw = (W - PAD * 2 - 4 * 12) / 5
    for i, (label, val, delta, lower_better) in enumerate(stats):
        x = PAD + i * (sw + 12)
        d.rectangle([(x * S, sy * S), ((x + sw) * S, (sy + sh) * S)],
                    fill=PANEL, outline=EDGE, width=S)
        text((x + 14, sy + 14), label.upper(), f_syne(11 * S, 700), DIM)
        text((x + 14, sy + 38), val, f_num(25 * S, 700), INK)
        if delta is not None:
            good = (delta < 0) if lower_better else (delta >= 0)
            text((x + 14, sy + 74), f"{'+' if delta >= 0 else '−'}{abs(delta):.1f}% MoM",
                 f_num(14 * S, 700), MINT if good else RED)

    # ---------------------------------------------------------------- footer
    fy = H - 58
    d.line([(PAD * S, fy * S), ((W - PAD) * S, fy * S)], fill=(30, 44, 72), width=S)
    text((PAD, fy + 20), "Data: Blockworks · DefiLlama · CoinGecko", f_syne(14 * S, 600), DIM)
    cf = f_syne(17 * S, 700)
    text((W - PAD - wide("stateofsol.com", cf), fy + 18), "stateofsol.com", cf, ORANGE)

    out = HERE / f"solana-recap-{ym}.png"
    img.resize((W, H), Image.LANCZOS).save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

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

import calendar
import json
import ssl
import sys
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

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
PANEL, EDGE, SLATE = (13, 26, 52), (35, 52, 84), (60, 78, 120)
CHAIN_COLOR = {"solana": (20, 241, 149), "ethereum": (143, 165, 212),
               "base": (59, 130, 246), "arbitrum": (168, 85, 247),
               "bnb": (240, 185, 11), "avalanche": (232, 65, 66),
               "sui": (56, 189, 248), "tron": (251, 146, 60),
               "hyperevm": (14, 165, 160), "polygon": (232, 121, 249)}
ASSET_COLOR = {"Solana": (20, 241, 149), "Bitcoin": (247, 147, 26),
               "Ethereum": (143, 165, 212), "BNB": (240, 185, 11),
               "Avalanche": (232, 65, 66), "Sui": (56, 189, 248),
               "Tron": (251, 146, 60), "Polygon": (232, 121, 249),
               "Arbitrum": (168, 85, 247), "Hyperliquid": (14, 165, 160)}
CHAIN_SHORT = {"solana": "SOL", "ethereum": "ETH", "base": "BASE",
               "arbitrum": "ARB", "bnb": "BNB", "avalanche": "AVAX",
               "sui": "SUI", "tron": "TRX", "hyperevm": "HYPE",
               "polygon": "POL"}
CHAIN_LABEL = {"solana": "Solana", "ethereum": "Ethereum", "base": "Base",
               "arbitrum": "Arbitrum", "bnb": "BNB", "avalanche": "Avalanche",
               "sui": "Sui", "tron": "Tron", "hyperevm": "Hyperliquid",
               "polygon": "Polygon"}
# CoinGecko asset name -> ticker for the performance panel
TICKER = {"Solana": "SOL", "Bitcoin": "BTC", "Ethereum": "ETH", "BNB": "BNB",
          "Avalanche": "AVAX", "Sui": "SUI", "Tron": "TRX", "Polygon": "POL",
          "Arbitrum": "ARB", "Hyperliquid": "HYPE"}

def developments(ym: str) -> list[tuple]:
    """The month's headlines, from the same highlights.json the dashboard reads.

    Everything else on this card is computed from data.json; these are written
    by hand. Only entries carrying a short "card" headline are drawn, so the
    site can run a longer list than fits here, and a month nobody has written
    up simply drops the section.
    """
    path = HERE / "highlights.json"
    if not path.exists():
        return []
    h = json.loads(path.read_text())
    cats = h.get("cats", {})
    out = []
    for it in h.get("months", {}).get(ym, {}).get("items", []):
        if not it.get("card"):
            continue
        d = it["d"]
        when = f"{int(d[8:])} {MON3[int(d[5:7]) - 1]}" if len(d) > 7 else MON3[int(d[5:7]) - 1]
        hexc = cats.get(it.get("cat"), "#8FA5D4").lstrip("#")
        colour = tuple(int(hexc[i:i + 2], 16) for i in (0, 2, 4))
        out.append((when, it.get("cat", ""), it["card"], it.get("b", ""), colour))
    return out

_cache: dict = {}


def _font(name: str, size: int, axes: list[int]) -> ImageFont.FreeTypeFont:
    key = (name, size, tuple(axes))
    if key in _cache:
        return _cache[key]
    path, url = FONTS[name]
    if not path.exists():
        # The stock context has no root bundle on a fresh macOS Python, so this
        # only ever worked off a warm /tmp. Verify against certifi like
        # refresh_data.py does.
        with urllib.request.urlopen(url, context=_SSL_CTX, timeout=180) as r:
            path.write_bytes(r.read())      # a few MB of variable font
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
    return _font("inter", size, [32, weight])


def compact(n: float | None, prefix: str = "") -> str:
    if n is None:
        return "—"
    a = abs(n)
    # 0.9995 claims a value that would round up into the unit: 999,585 belongs
    # in "1.00M", not the "1000K" a plain `a >= v` produces.
    for v, s in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if a >= v * 0.9995:
            q = n / v
            return f"{prefix}{q:.0f}{s}" if abs(q) >= 100 else (
                f"{prefix}{q:.1f}{s}" if abs(q) >= 10 else f"{prefix}{q:.2f}{s}")
    return f"{prefix}{n:.2f}"


def pct(v: float) -> str:
    return f"{'+' if v >= 0 else ''}{v:.1f}%"


def ordinal(n: int) -> str:
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


# Widths have to be known before the canvas exists, to size the card.
_MEAS = ImageDraw.Draw(Image.new("RGB", (1, 1)))


def wrap(s: str, f: ImageFont.FreeTypeFont, maxw: float, scale: int = 1) -> list[str]:
    lines, cur = [], ""
    for word in s.split():
        trial = f"{cur} {word}".strip()
        if cur and _MEAS.textlength(trial, font=f) / scale > maxw:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def main() -> int:
    data = json.loads((HERE / "data.json").read_text())
    cmp_ = data.get("compare", {})
    grid = data.get("monthly_returns", {}).get("grid", {})

    ym = sys.argv[1] if len(sys.argv) > 1 else max(
        f"{y}-{int(m):02d}" for y, row in grid.items() for m in row)
    year, mon = int(ym[:4]), int(ym[5:])
    prev = f"{year - 1}-12" if mon == 1 else f"{year}-{mon - 1:02d}"
    days_in_month = calendar.monthrange(year, mon)[1]
    month_secs = days_in_month * 86400

    def agg(key, how, month=None, chain="solana"):
        m = month or ym
        pts = [p["v"] for p in cmp_.get(key, {}).get(chain, []) if p["d"][:7] == m]
        if not pts:
            return None
        return sum(pts) if how == "sum" else (sum(pts) / len(pts) if how == "mean" else pts[-1])

    def mom(key, how):
        a, b = agg(key, how), agg(key, how, prev)
        return (a / b - 1) * 100 if (a and b) else None

    def month_total(key):
        return {ch: sum(p["v"] for p in pts if p["d"][:7] == ym)
                for ch, pts in cmp_.get(key, {}).items()}

    # ---- asset returns for the month, all from one price source (CoinGecko)
    def last_in(pts, m):
        vals = [p["v"] for p in pts if p["d"][:7] == m]
        return vals[-1] if vals else None

    perf = {}
    for asset, pts in cmp_.get("price_perf", {}).items():
        a, b = last_in(pts, prev), last_in(pts, ym)
        if a and b:
            perf[asset] = (b / a - 1) * 100
    perf_rank = sorted(perf.items(), key=lambda kv: -kv[1])
    sol_ret = perf.get("Solana")
    sol_pts = cmp_.get("price_perf", {}).get("Solana", [])
    sol_open, sol_close = last_in(sol_pts, prev), last_in(sol_pts, ym)

    # The site's seasonality grid runs on Blockworks prices, which sit ~1% off
    # CoinGecko at any given month boundary. Reading the year's bars out of it
    # would print two different Augusts on the same card, so rebuild the bars
    # from the very series the headline and the peer ranking use.
    def year_returns(pts, yr):
        close, last_day = {}, {}
        for p in pts:
            close[p["d"][:7]], last_day[p["d"][:7]] = p["v"], p["d"]
        out = {}
        for m, v in close.items():
            y, mo = int(m[:4]), int(m[5:])
            if y != yr or last_day[m][8:] != f"{calendar.monthrange(y, mo)[1]:02d}":
                continue                                  # wrong year, or still open
            base = close.get(f"{y - 1}-12" if mo == 1 else f"{y}-{mo - 1:02d}")
            if base:
                out[str(mo)] = (v / base - 1) * 100
        return out

    # CoinGecko's free tier only reaches back 365 days; fall back to the grid if
    # that window somehow misses the month being recapped.
    cg_rows = year_returns(sol_pts, year)
    year_rows = cg_rows if str(mon) in cg_rows else grid.get(str(year), {})

    tx = month_total("transactions")
    sol_tx, other_tx = tx.get("solana", 0), sum(v for c, v in tx.items() if c != "solana")
    rev = month_total("rev")
    rev_rank = sorted(rev.items(), key=lambda kv: -kv[1])
    rev_total = sum(rev.values()) or 1
    dex = month_total("dex_volume")
    dex_rank = sorted(dex.items(), key=lambda kv: -kv[1])

    PY_, PH_, RH_, QH_, SH_, GAP = 132, 246, 268, 244, 104, 16
    W, S, PAD = 1200, 2, 56

    # Headlines only — three across, sized to whichever one needs two lines.
    # Chronological, with anything dated to the month as a whole ("Aug") last.
    devs = sorted(developments(ym),
                  key=lambda e: int(e[0].split()[0]) if e[0][0].isdigit() else 99)[:6]
    DEV_CW = (W - PAD * 2 - 36 - 36) / 3
    dev_heads = [wrap(hd, f_syne(18 * S, 700), DEV_CW - 15, S)[:2] for _, _, hd, _, _ in devs]
    DEV_ROW = 24 + 23 * max((len(h) for h in dev_heads), default=1) + 10
    DH_ = (46 + DEV_ROW * ((len(devs) + 2) // 3) + 6 + GAP) if devs else 0

    H = PY_ + DH_ + PH_ + GAP + RH_ + GAP + QH_ + GAP + SH_ + 74

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
        text((x + 18, y + 15), title.upper(), f_syne(14 * S, 700), DIM)

    def rect(x, y, w, h, fill):
        d.rectangle([(x * S, y * S), ((x + w) * S, (y + h) * S)], fill=fill)

    def fade(c, a=0.5):
        """Half-strength colour: the peers stay legible, Solana stays loud."""
        return tuple(int(PANEL[i] + (c[i] - PANEL[i]) * a) for i in range(3))


    # ---------------------------------------------------------------- header
    text((PAD, 28), "StateOfSOL.com", f_syne(20 * S, 700), MUTE)
    # 42, not 46: the long title has to clear the logo, which starts at x≈814
    text((PAD, 60), f"{MONTHS[mon - 1]} {year} Solana Recap", f_syne(42 * S, 700), INK)
    logo = Image.open(HERE / "dfdv-logo.png").convert("RGBA")
    lh = 28 * S
    logo = logo.resize((int(lh * logo.width / logo.height), lh), Image.LANCZOS)
    img.paste(logo, ((W - PAD) * S - logo.width, 36 * S), logo)

    # ------------------------------------------------- 0. the month's headlines
    if devs:
        panel(PAD, PY_, W - PAD * 2, DH_ - GAP, f"Top developments · {MONTHS[mon - 1]}")
        hf, tf = f_syne(18 * S, 700), f_syne(12 * S, 700)
        for i, (when, cat, _head, _body, col) in enumerate(devs):
            cx = PAD + 18 + (i % 3) * (DEV_CW + 18)
            cy = PY_ + 46 + (i // 3) * DEV_ROW
            rect(cx, cy, 3, DEV_ROW - 16, col)               # accent rule
            tx0, ty = cx + 15, cy
            text((tx0, ty), f"{when} · {cat}".upper(), tf, col)
            ty += 20
            for ln in dev_heads[i]:
                text((tx0, ty), ln, hf, INK)
                ty += 23

    # ------------------------- 1. SOL vs peers: the month's return, ranked
    py, ph = PY_ + DH_, PH_
    panel(PAD, py, W - PAD * 2, ph, f"{MONTHS[mon - 1]} price performance vs peers")
    if sol_ret is not None:
        up = sol_ret >= 0
        rank = [a for a, _ in perf_rank].index("Solana") + 1
        # (text, font, colour, advance to the next line)
        col = [(pct(sol_ret), f_num(58 * S, 800), MINT if up else RED, 68)]
        if sol_open and sol_close:
            col.append((f"${sol_open:,.2f} → ${sol_close:,.2f}", f_num(19 * S, 600),
                        (211, 219, 234), 30))
        col.append((f"{ordinal(rank)} best of {len(perf_rank)} majors", f_syne(16 * S, 700),
                    MINT if rank <= 3 else MUTE, 0))
        ty = py + 40 + (ph - 40 - (sum(a for *_, a in col) + 21)) / 2
        for s, f, c, adv in col:
            text((PAD + 18, ty), s, f, c)
            ty += adv

    if perf_rank:
        gx, gw = PAD + 250, W - PAD * 2 - 268
        gy, gh = py + 56, ph - 118
        mx = max(abs(v) for _, v in perf_rank) or 1
        zero = gy + gh * (max(v for _, v in perf_rank) / (mx * 2) + 0.5) \
            if min(v for _, v in perf_rank) < 0 else gy + gh
        zero = min(max(zero, gy + 24), gy + gh)
        slot = gw / len(perf_rank)
        for i, (asset, v) in enumerate(perf_rank):
            bw_ = slot * 0.52
            bx = gx + i * slot + (slot - bw_) / 2
            span = (zero - gy - 22) if v >= 0 else (gy + gh - zero)
            hgt = max(3, span * abs(v) / mx)
            top_y = zero - hgt if v >= 0 else zero
            is_sol = asset == "Solana"
            ac = ASSET_COLOR.get(asset, SLATE)
            rect(bx, top_y, bw_, hgt, ac if is_sol else fade(ac))
            text((bx + bw_ / 2, top_y - 18), pct(v), f_num(14 * S, 700),
                 MINT if is_sol else MUTE, anchor="ma")
            text((bx + bw_ / 2, gy + gh + 8), TICKER.get(asset, asset[:4]),
                 f_syne(14 * S, 700), INK if is_sol else DIM, anchor="ma")
        d.line([(gx * S, zero * S), ((gx + gw) * S, zero * S)], fill=EDGE, width=S)

    # ------------------ 2 & 3. transactions (+ implied TPS) | REV with Other
    ry, rh = py + ph + GAP, RH_
    hw = (W - PAD * 2 - GAP) / 2

    panel(PAD, ry, hw, rh, "Non-vote transactions")
    peak = max(sol_tx, other_tx) or 1
    bx, bw_, bh = PAD + 18, hw - 36, 42
    for i, (label, val, fill, col) in enumerate([
            ("Solana", sol_tx, MINT, INK),
            (f"All {len(tx) - 1} other chains", other_tx, SLATE, MUTE)]):
        by = ry + 58 + i * 96
        text((bx, by - 22), label, f_syne(16 * S, 700), INK)
        w = max(6, bw_ * val / peak)
        if i == 0:
            rect(bx, by, w, bh, fill)
        else:
            # stack the rest so the bar shows who the "others" actually are
            seg_x = bx
            for ch, cv in sorted(((c, v) for c, v in tx.items() if c != "solana"),
                                 key=lambda kv: -kv[1]):
                sw_ = w * cv / (val or 1)
                rect(seg_x, by, max(1, sw_), bh, fade(CHAIN_COLOR.get(ch, SLATE)))
                seg_x += sw_
        vf = f_num(24 * S, 800)
        vw = wide(compact(val), vf)
        # never sit the value on the stacked bar — it crosses several colours
        inside = i == 0 and w > vw + 28
        text((bx + w - vw - 12 if inside else bx + w + 12, by + 10), compact(val), vf,
             (4, 30, 22) if inside else INK)
        text((bx, by + bh + 5), f"{val / month_secs:,.0f} TPS implied",
             f_num(15 * S, 600), MINT if i == 0 else DIM)
    if other_tx:
        text((bx, ry + rh - 30), f"{sol_tx / other_tx:.1f}× every other chain combined",
             f_syne(16 * S, 700), MINT)

    x2 = PAD + hw + GAP
    panel(x2, ry, hw, rh, "Real economic value")
    top_rev = rev_rank[:4]
    other_rev = sum(v for _, v in rev_rank[4:])
    rows = [(CHAIN_LABEL.get(ch, ch), v, ch == "solana", CHAIN_COLOR.get(ch, SLATE))
            for ch, v in top_rev]
    if other_rev:
        rows.append((f"Other ({len(rev_rank) - 4})", other_rev, False, SLATE))
    pk = max(v for _, v, _, _ in rows) or 1
    for i, (name, v, is_sol, colr) in enumerate(rows):
        by = ry + 52 + i * 37
        text((x2 + 18, by + 3), name, f_syne(15 * S, 700), INK if is_sol else MUTE)
        bx2, bw2 = x2 + 132, hw - 300
        rect(bx2, by + 2, max(4, bw2 * v / pk), 20, colr if is_sol else fade(colr))
        text((x2 + hw - 76, by + 3), compact(v, "$"), f_num(16 * S, 700),
             INK if is_sol else MUTE, anchor="ra")
        text((x2 + hw - 18, by + 3), f"{v / rev_total * 100:.0f}%", f_num(15 * S, 600),
             MINT if is_sol else DIM, anchor="ra")
    text((x2 + 18, ry + rh - 32), f"Solana leads all {len(rev_rank)} chains this month"
         if rev_rank and rev_rank[0][0] == "solana" else
         f"Solana ranks #{[c for c, _ in rev_rank].index('solana') + 1} of {len(rev_rank)}",
         f_syne(16 * S, 700), MINT)

    # ------------------- 4 & 5. DEX volume by chain | monthly returns, labelled
    qy, qh = ry + rh + GAP, QH_
    panel(PAD, qy, hw, qh, f"Spot DEX volume · {MONTHS[mon - 1]}")
    if dex_rank:
        gx, gy = PAD + 18, qy + 54
        gw, gh = hw - 36, qh - 122
        mxd = dex_rank[0][1] or 1
        slot = gw / len(dex_rank)
        for i, (ch, v) in enumerate(dex_rank):
            bw2 = slot * 0.56
            bxx = gx + i * slot + (slot - bw2) / 2
            hgt = max(3, gh * v / mxd)
            is_sol = ch == "solana"
            cc = CHAIN_COLOR.get(ch, SLATE)
            rect(bxx, gy + gh - hgt, bw2, hgt, cc if is_sol else fade(cc))
            text((bxx + bw2 / 2, gy + gh - hgt - 18), compact(v, "$"), f_num(13 * S, 700),
                 MINT if is_sol else (211, 219, 234), anchor="ma")
            text((bxx + bw2 / 2, gy + gh + 8), CHAIN_SHORT.get(ch, ch[:4].upper()),
                 f_syne(13 * S, 700), INK if is_sol else DIM, anchor="ma")
        share = dex_rank[0][1] / (sum(dex.values()) or 1) * 100
        if dex_rank[0][0] == "solana":
            text((gx, qy + qh - 32), f"{share:.0f}% of tracked DEX volume",
                 f_syne(16 * S, 700), MINT)

    panel(x2, qy, hw, qh, f"SOL monthly return · {year}")
    yr_rows = year_rows
    months = sorted(int(k) for k in yr_rows)
    if months:
        mx = max(abs(yr_rows[str(m)]) for m in months) or 1
        gx, gy = x2 + 18, qy + 58
        gw, gh = hw - 36, qh - 132
        mid = gy + gh / 2
        d.line([(gx * S, mid * S), ((gx + gw) * S, mid * S)], fill=EDGE, width=S)
        slot = gw / len(months)
        for i, m in enumerate(months):
            v = yr_rows[str(m)]
            hgt = (gh / 2 - 14) * abs(v) / mx
            top_y, bot_y = (mid - hgt, mid) if v >= 0 else (mid, mid + hgt)
            cur = (m == mon)
            col = (MINT if v >= 0 else RED) if cur else ((16, 120, 90) if v >= 0 else (128, 58, 64))
            rect(gx + i * slot + 5, top_y, slot - 10, bot_y - top_y, col)
            text((gx + (i + 0.5) * slot, (top_y - 17) if v >= 0 else (bot_y + 3)),
                 pct(v), f_num(13 * S, 700), MINT if cur else DIM, anchor="ma")
            text((gx + (i + 0.5) * slot, gy + gh + 8), MON3[m - 1], f_syne(13 * S, 700),
                 INK if cur else DIM, anchor="ma")
        if yr_rows.get(str(mon)) == max(yr_rows.values()):
            text((gx, qy + qh - 32), f"best month of {year}", f_syne(16 * S, 700), MINT)

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
        text((x + 14, sy + 14), label.upper(), f_syne(12 * S, 700), DIM)
        text((x + 14, sy + 38), val, f_num(27 * S, 700), INK)
        if delta is not None:
            good = (delta < 0) if lower_better else (delta >= 0)
            text((x + 14, sy + 74), f"{'+' if delta >= 0 else '−'}{abs(delta):.1f}% MoM",
                 f_num(15 * S, 700), MINT if good else RED)

    # ---------------------------------------------------------------- footer
    fy = H - 58
    d.line([(PAD * S, fy * S), ((W - PAD) * S, fy * S)], fill=(30, 44, 72), width=S)
    text((PAD, fy + 20), "Data: Blockworks · DefiLlama · CoinGecko", f_syne(15 * S, 600), DIM)
    cf = f_syne(18 * S, 700)
    text((W - PAD - wide("stateofsol.com", cf), fy + 18), "stateofsol.com", cf, ORANGE)

    out = HERE / f"solana-recap-{ym}.png"
    img.resize((W, H), Image.LANCZOS).save(out, optimize=True)
    print(f"wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

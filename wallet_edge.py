"""
wallet_edge.py — does a Polymarket wallet actually beat the prices it pays?

THE ONLY QUESTION THAT MATTERS
  A contract bought at 70c should win ~70% of the time. That is what the
  price means. So "this wallet made money" proves nothing — buying
  favourites makes money most days and loses it all on the rare miss.

  The real test:  observed win rate  vs  the win rate their own entry
  prices implied. The gap is the edge, and it is measurable per wallet,
  per category, from public on-chain history.

WHAT THIS GUARDS AGAINST
  - survivorship: the leaderboard only shows winners. So this scores ANY
    wallet you point it at, and reports the confidence interval, not just
    the point estimate.
  - open positions: unrealised marks flatter. Only RESOLVED positions count.
  - one lucky market: results are broken down by category and by size.
  - small samples: a wallet with 30 trades cannot be distinguished from
    luck and the tool says so instead of ranking it.

    python3 wallet_edge.py --wallet 0x496f...
    python3 wallet_edge.py --leaderboard --window 30d --top 20
    python3 wallet_edge.py --wallet 0x... --kalshi
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics as st
import time
from collections import defaultdict
from pathlib import Path

import requests

DATA = Path.home() / "trading-lab" / "data" / "polymarket"
DATA.mkdir(parents=True, exist_ok=True)
DATA_API = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "trading-lab research"}

# crude category tagging from market titles; Kalshi analogue where one exists
CATEGORIES = [
    ("weather", r"temperature|highest temp|rainfall|snow|hurricane|degrees",
     "KXHIGH* (7 US cities)"),
    ("crypto", r"bitcoin|btc|ethereum|eth|solana|xrp|dogecoin|crypto",
     "KXBTCD / KXETHD / KXSOLE"),
    ("sports", r"\bwin on\b|vs\.|spread:|o/u|match|game|nba|nfl|mlb|soccer|"
     r"premier league|la liga|tennis|open:", "Kalshi sports series"),
    ("politics", r"election|president|senate|congress|nominee|primary|"
     r"prime minister|parliament|out as", "KXPRES* / KX*ELECT*"),
    ("econ", r"fed |interest rate|inflation|cpi|unemployment|gdp|recession",
     "KXFEDDECISION / KXCPI"),
    ("company", r"iphone|apple|tesla|openai|ipo|acquisition|launch a token|"
     r"airdrop|earnings", "KXIPHONERELEASE etc"),
    ("entertainment", r"movie|film|oscar|grammy|netflix|box office|album|"
     r"bond|rotten tomatoes", "KXRT / KXNETFLIX*"),
]


def categorise(title):
    import re
    t = (title or "").lower()
    for name, pat, kalshi in CATEGORIES:
        if re.search(pat, t):
            return name, kalshi
    return "other", ""


def get(url, **params):
    r = requests.get(url, params=params or None, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def positions(wallet, limit=500, max_pages=20):
    """Every position, open and closed. The API paginates by offset."""
    out, offset = [], 0
    for _ in range(max_pages):
        try:
            d = get(f"{DATA_API}/positions", user=wallet, limit=limit,
                    offset=offset, sortBy="CURRENT", sortDirection="DESC")
        except Exception as e:
            if not out:
                raise
            break
        rows = d if isinstance(d, list) else d.get("data", [])
        if not rows:
            break
        out += rows
        offset += len(rows)
        if len(rows) < limit:
            break
        time.sleep(0.2)
    return out


def leaderboard(window="30d", limit=25):
    for url, p in ((f"{DATA_API}/v1/leaderboard", {"window": window, "limit": limit}),
                   (f"{DATA_API}/leaderboard", {"window": window, "limit": limit})):
        try:
            d = get(url, **p)
            rows = d if isinstance(d, list) else (d.get("data") or [])
            if rows:
                return rows
        except Exception:
            continue
    return []


def f(d, *names, default=None):
    for n in names:
        v = d.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def analyse(rows):
    """Score resolved positions against the prices paid."""
    res = []
    for p in rows:
        cur = f(p, "curPrice", default=None)
        avg = f(p, "avgPrice", default=None)
        size = f(p, "size", default=0.0)
        if cur is None or avg is None or size <= 0:
            continue
        # resolved when the mark is pinned to 0 or 1
        if not (cur <= 0.001 or cur >= 0.999):
            continue
        won = cur >= 0.999
        title = p.get("title") or p.get("slug") or ""
        cat, kal = categorise(title)
        staked = avg * size
        returned = size if won else 0.0
        res.append({"title": title[:70], "cat": cat, "kalshi": kal,
                    "avg": avg, "size": size, "won": int(won),
                    "staked": staked, "returned": returned,
                    "pnl": returned - staked,
                    "outcome": p.get("outcome", "")})
    return res


def wilson(k, n, z=1.96):
    """Confidence interval on a win rate — the honest version for small n."""
    if n == 0:
        return 0.0, 1.0
    ph = k / n
    d = 1 + z * z / n
    c = ph + z * z / (2 * n)
    m = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n))
    return (c - m) / d, (c + m) / d


def report(rows, label, min_n=30):
    if not rows:
        print(f"{label}: no RESOLVED positions found")
        return None
    n = len(rows)
    wins = sum(r["won"] for r in rows)
    staked = sum(r["staked"] for r in rows)
    ret = sum(r["returned"] for r in rows)
    # what their own prices implied
    implied = sum(r["avg"] * r["size"] for r in rows) / sum(r["size"] for r in rows)
    actual = sum(r["won"] * r["size"] for r in rows) / sum(r["size"] for r in rows)

    print(f"\n{'='*76}\n{label}\n{'='*76}")
    print(f"  resolved positions   {n:,}")
    print(f"  staked               ${staked:,.2f}")
    print(f"  returned             ${ret:,.2f}")
    print(f"  P&L                  ${ret-staked:+,.2f}   "
          f"({(ret/staked-1)*100 if staked else 0:+.1f}% on stake)")
    print(f"\n  THE TEST — did they beat the prices they paid?")
    print(f"    avg price paid        {implied*100:6.2f}%  "
          f"(what the market said would happen)")
    print(f"    actually happened     {actual*100:6.2f}%")
    edge = (actual - implied) * 100
    print(f"    edge                  {edge:+6.2f} points")

    lo, hi = wilson(wins, n)
    print(f"    win rate {wins}/{n} = {wins/n*100:.1f}%, "
          f"95% CI [{lo*100:.1f}%, {hi*100:.1f}%]")
    if n < min_n:
        print(f"\n  TOO FEW. {n} resolved positions cannot separate skill from")
        print(f"  luck. Need {min_n}+ before this number means anything.")
    elif lo * 100 > implied * 100:
        print(f"\n  Beat their own entry prices with the interval clear of it.")
        print(f"  That is a real edge on this history.")
    else:
        print(f"\n  The interval includes the implied rate. Cannot distinguish")
        print(f"  from simply buying favourites and getting normal outcomes.")
    return {"n": n, "edge": edge, "pnl": ret - staked, "lo": lo,
            "implied": implied, "actual": actual}


def by_category(rows):
    cats = defaultdict(list)
    for r in rows:
        cats[r["cat"]].append(r)
    print(f"\n{'='*76}\nBY NICHE — where does the edge actually live?\n{'='*76}")
    print(f"{'niche':<15}{'n':>6}{'staked':>12}{'P&L':>12}{'paid':>8}"
          f"{'hit':>8}{'edge':>8}  kalshi equivalent")
    out = []
    for c, rs in sorted(cats.items(), key=lambda kv: -len(kv[1])):
        n = len(rs)
        stk = sum(r["staked"] for r in rs)
        pnl = sum(r["pnl"] for r in rs)
        sz = sum(r["size"] for r in rs)
        imp = sum(r["avg"] * r["size"] for r in rs) / sz
        act = sum(r["won"] * r["size"] for r in rs) / sz
        mark = "" if n >= 30 else "  (too few)"
        print(f"{c:<15}{n:>6}{stk:>12,.0f}{pnl:>+12,.0f}{imp*100:>7.1f}%"
              f"{act*100:>7.1f}%{(act-imp)*100:>+8.1f}  "
              f"{rs[0]['kalshi'][:26]}{mark}")
        out.append((c, n, pnl, (act - imp) * 100))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wallet")
    ap.add_argument("--leaderboard", action="store_true")
    ap.add_argument("--window", default="30d")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-n", type=int, default=30)
    a = ap.parse_args()

    if a.leaderboard:
        lb = leaderboard(a.window, a.top)
        if not lb:
            print("leaderboard endpoint did not answer")
            return
        print(f"screening {len(lb)} wallets from the {a.window} leaderboard")
        print("NOTE this list is survivorship by construction — it shows the")
        print("winners. The test below is whether they beat their own prices.\n")
        print(f"{'#':>3} {'wallet':<16}{'resolved':>10}{'P&L':>12}"
              f"{'paid':>8}{'hit':>8}{'edge':>9}{'verdict':>14}")
        results = []
        for i, w in enumerate(lb[:a.top], 1):
            addr = w.get("proxyWallet") or w.get("wallet") or w.get("address")
            if not addr:
                continue
            try:
                rs = analyse(positions(addr, limit=500, max_pages=6))
            except Exception as e:
                print(f"{i:>3} {addr[:16]:<16}  error {str(e)[:30]}")
                continue
            if not rs:
                print(f"{i:>3} {addr[:16]:<16}{0:>10}   no resolved positions")
                continue
            n = len(rs)
            sz = sum(r["size"] for r in rs)
            imp = sum(r["avg"] * r["size"] for r in rs) / sz
            act = sum(r["won"] * r["size"] for r in rs) / sz
            pnl = sum(r["pnl"] for r in rs)
            lo, _ = wilson(sum(r["won"] for r in rs), n)
            v = ("too few" if n < a.min_n else
                 "REAL EDGE" if lo > imp else "not distinguishable")
            print(f"{i:>3} {addr[:16]:<16}{n:>10}{pnl:>+12,.0f}"
                  f"{imp*100:>7.1f}%{act*100:>7.1f}%{(act-imp)*100:>+9.1f}"
                  f"{v:>14}")
            results.append((addr, n, pnl, (act - imp) * 100, v))
            time.sleep(0.4)
        real = [r for r in results if r[4] == "REAL EDGE"]
        print(f"\n{len(real)} of {len(results)} wallets beat their own entry")
        print("prices by a margin wider than chance.")
        if real:
            print("\nWorth a closer look:")
            for addr, n, pnl, e, _ in real:
                print(f"  python3 wallet_edge.py --wallet {addr}")
        return

    if not a.wallet:
        ap.print_help()
        return

    rows = analyse(positions(a.wallet))
    r = report(rows, f"WALLET {a.wallet}", a.min_n)
    if not rows:
        return
    by_category(rows)

    print(f"\n{'='*76}\nBY PRICE PAID — are they just buying favourites?\n{'='*76}")
    print(f"{'price band':<14}{'n':>6}{'paid':>8}{'hit':>8}{'edge':>9}{'P&L':>12}")
    bands = [(0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]
    for lo, hi in bands:
        rs = [x for x in rows if lo <= x["avg"] < hi]
        if not rs:
            continue
        sz = sum(x["size"] for x in rs)
        imp = sum(x["avg"] * x["size"] for x in rs) / sz
        act = sum(x["won"] * x["size"] for x in rs) / sz
        print(f"{f'{lo*100:.0f}-{hi*100:.0f}c':<14}{len(rs):>6}{imp*100:>7.1f}%"
              f"{act*100:>7.1f}%{(act-imp)*100:>+9.1f}"
              f"{sum(x['pnl'] for x in rs):>+12,.0f}")

    out = DATA / f"wallet_{a.wallet[:10]}.csv"
    with out.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"\nwrote {out}")
    print("\nBefore copying anyone: you see positions AFTER entry, at worse")
    print("prices, and only the survivors are visible. A real edge here is")
    print("a reason to TEST their rule, not to mirror their book.")


if __name__ == "__main__":
    main()

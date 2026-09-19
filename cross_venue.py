"""
cross_venue.py — find the same event priced differently on Polymarket
and Kalshi, and rank Polymarket traders by realised profit.

Lives in ~/trading-lab. Two modes.

  --spreads   Match Polymarket markets to Kalshi markets and report price
              gaps. A gap is a CANDIDATE, never a trade: two venues can
              price the same headline differently because their
              settlement rules differ, and that difference is not an
              edge, it is a different contract.

  --whales    Polymarket is on-chain, so wallet positions and realised
              P&L are public. Rank traders by profit, then show what the
              top wallets currently hold. Kalshi exposes nothing like
              this — that asymmetry is the point.

Reads only. Polymarket blocks US IPs from PLACING orders; reading is
open and keyless. Execution stays on Kalshi.

    python3 cross_venue.py --spreads
    python3 cross_venue.py --spreads --min-gap 5
    python3 cross_venue.py --whales
    python3 cross_venue.py --whales --window 30d --top 15
    python3 cross_venue.py --wallet 0xABC...
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import time
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path

import requests

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
OUT = Path.home() / "trading-lab" / "data" / "cross_venue"
OUT.mkdir(parents=True, exist_ok=True)

STOP = {"will", "the", "be", "on", "in", "at", "a", "an", "of", "to", "by",
        "for", "or", "and", "is", "are", "before", "after", "than", "this",
        "that", "price", "above", "below", "market", "2026", "2027"}


# ------------------------------------------------------------- polymarket

def pm_markets(limit=500, max_pages=20, verbose=True):
    """Open Polymarket markets. v1 stopped at 100 rows because Gamma caps
    `limit` well below 500 and v1 treated a short page as the end of the
    list. Detect the server's real page size from the first response and
    page on that."""
    out, offset, page_size = [], 0, None
    for page in range(max_pages):
        r = requests.get(f"{GAMMA}/markets", params={
            "closed": "false", "active": "true", "limit": limit,
            "offset": offset, "order": "volumeNum", "ascending": "false"},
            timeout=30)
        r.raise_for_status()
        b = r.json()
        if not b:
            break
        if page_size is None:
            page_size = len(b)          # what the server actually returns
        out += b
        offset += len(b)
        if verbose and page % 4 == 3:
            print(f"    {len(out)} markets...", flush=True)
        if len(b) < page_size:
            break
        time.sleep(0.15)
    rows = []
    for m in out:
        try:
            prices = m.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            yes = float(prices[0]) * 100 if prices else None
        except Exception:
            yes = None
        if yes is None:
            continue
        rows.append({
            "venue": "polymarket",
            "id": m.get("conditionId") or m.get("id"),
            "slug": m.get("slug", ""),
            "title": m.get("question") or m.get("title") or "",
            "yes": yes,
            "spread": float(m.get("spread") or 0) * 100,
            "vol": float(m.get("volumeNum") or 0),
            "liq": float(m.get("liquidityNum") or 0),
            "end": (m.get("endDate") or "")[:10],
        })
    return rows


def pm_leaderboard(window="30d", limit=25):
    """Traders ranked by realised profit. Endpoint path has moved before
    (used to be a standalone lb-api host), so try the known variants and
    say plainly which one answered."""
    attempts = [
        (f"{DATA}/leaderboard", {"window": window, "limit": limit, "orderBy": "profit"}),
        (f"{DATA}/leaderboard", {"period": window, "limit": limit}),
        (f"{DATA}/v1/leaderboard", {"window": window, "limit": limit}),
        (f"{GAMMA}/leaderboard", {"window": window, "limit": limit}),
    ]
    for url, params in attempts:
        try:
            r = requests.get(url, params=params, timeout=25)
            if not r.ok:
                continue
            d = r.json()
            rows = d if isinstance(d, list) else (d.get("data") or d.get("leaderboard") or [])
            if rows:
                return rows, url
        except Exception:
            continue
    return [], None


def pm_positions(wallet, limit=100):
    r = requests.get(f"{DATA}/positions",
                     params={"user": wallet, "limit": limit,
                             "sortBy": "CURRENT", "sortDirection": "DESC"},
                     timeout=25)
    if not r.ok:
        return []
    d = r.json()
    return d if isinstance(d, list) else d.get("data", [])


# ----------------------------------------------------------------- kalshi

def kalshi_get(ep, **params):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from dotenv import load_dotenv
    load_dotenv(Path.home() / "trading-lab" / ".env")
    base = os.environ["KALSHI_BASE_PROD"].rstrip("/")
    prefix = "/" + base.split("://", 1)[1].split("/", 1)[1]
    pk = serialization.load_pem_private_key(
        Path(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"])).read_bytes(),
        password=None)
    ts = str(int(time.time() * 1000))
    sig = pk.sign((ts + "GET" + prefix + ep).encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.DIGEST_LENGTH),
                  hashes.SHA256())
    r = requests.get(base + ep,
                     headers={"KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"],
                              "KALSHI-ACCESS-TIMESTAMP": ts,
                              "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()},
                     params=params or None, timeout=25)
    r.raise_for_status()
    return r.json()


KALSHI_SEEDS = ["KXFEDDECISION", "KXBTCD", "KXETHD", "KXBTC", "KXBTCY",
                "KXRATECUTCOUNT", "KXSPACEXCOUNT", "KXIPHONERELEASE",
                "KXAAAGASD", "KXAAAGASW", "KXAAAGASM", "KXLLM1",
                "KXBTCMAXMON", "KXACQANNOUNCEEBAY", "KXH100MS", "KXBTC2026200"]


def kalshi_markets(seeds=None):
    rows = []
    for s in (seeds or KALSHI_SEEDS):
        try:
            d = kalshi_get("/markets", series_ticker=s, status="open", limit=1000)
        except Exception as e:
            print(f"  ! {s}: {e}")
            continue
        for m in d.get("markets", []):
            bid = float(m.get("yes_bid_dollars") or 0) * 100
            ask = float(m.get("yes_ask_dollars") or 0) * 100
            vol = float(m.get("volume_24h_fp") or 0)
            if ask <= 0:
                continue
            rows.append({
                "venue": "kalshi",
                "ticker": m["ticker"],
                "title": (m.get("title") or "") + " " + (m.get("subtitle") or ""),
                "bid": bid, "ask": ask, "yes": (bid + ask) / 2,
                "spread": ask - bid, "vol": vol,
                "end": (m.get("close_time") or "")[:10],
                "rules": (m.get("rules_primary") or "")[:200],
            })
        time.sleep(0.05)
    return rows


# ---------------------------------------------------------------- matching

def keywords(t):
    w = re.findall(r"[a-z0-9$.,]+", t.lower())
    return {x for x in w if x not in STOP and len(x) > 2}

POLARITY = {
 "up":{"above","over","higher","increase","increases","hike","hikes","rise","rises","up","greater","exceed","exceeds","more"},
 "down":{"below","under","lower","decrease","decreases","cut","cuts","fall","falls","down","less","drop","drops","decline"},
 "flat":{"unchanged","hold","holds","steady","same","maintain"},
 "negate":{"not","no","never","fail","fails","without"}}

DATEWORDS = {"jan","feb","mar","apr","may","jun","jul","aug","sep","sept","oct","nov","dec",
 "january","february","march","april","june","july","august","september","october","november",
 "december","meeting","date","end","close","resolves","year","month","week","day","days","bps","bp"}

def polarity(t):
    w=set(re.findall(r"[a-z]+",t.lower()))
    flat=bool(re.search(r"\bno\s+(change|increase|decrease|hike|cut|move)",t.lower()))
    out=set()
    for c,ws in POLARITY.items():
        if c!="negate" and (w&ws): out.add(c)
    if flat: out.add("flat"); out.discard("up"); out.discard("down")
    return out, bool(w&POLARITY["negate"]) and not flat

def polarity_conflict(a,b):
    pa,na=polarity(a); pb,nb=polarity(b)
    for x,y in (("up","down"),("up","flat"),("down","flat")):
        if (x in pa and y in pb) or (y in pa and x in pb): return True
    return na!=nb

def subjects(t):
    out=set()
    for w in re.findall(r"[A-Za-z][A-Za-z'\-]+",t):
        lw=w.lower()
        if lw in STOP or lw in DATEWORDS or len(lw)<3: continue
        out.add(lw)
    return out

def price_levels(t):
    out=set()
    for m in re.finditer(r"\$\s?([\d,]+(?:\.\d+)?)",t): out.add(m.group(1).replace(",",""))
    for m in re.finditer(r"\b(\d+\.\d+)\b",t): out.add(m.group(1))
    return out

def match_score(a,b):
    if polarity_conflict(a,b): return 0.0
    sa,sb=subjects(a),subjects(b)
    if not sa or not sb: return 0.0
    shared=sa&sb
    pa,pb=price_levels(a),price_levels(b)
    shared_lvl=pa&pb
    # SUBJECT GATE
    if not shared: return 0.0
    if len(shared)<2 and not shared_lvl: return 0.0
    if len(shared)>=2 and len(shared)/min(len(sa),len(sb))<0.34: return 0.0
    jac=len(shared)/len(sa|sb)
    lvl=len(shared_lvl)/max(len(pa|pb),1) if (pa or pb) else 0.0
    seq=SequenceMatcher(None,a.lower()[:90],b.lower()[:90]).ratio()
    return 0.55*jac+0.25*lvl+0.20*seq

def find_spreads(pm, kal, min_gap, min_score, min_vol):
    pairs = []
    kal = [k for k in kal if k["vol"] >= min_vol]
    for p in pm:
        if p["vol"] < min_vol:
            continue
        best, bs = None, 0.0
        for k in kal:
            sc = match_score(p["title"], k["title"])
            if sc > bs:
                best, bs = k, sc
        if not best or bs < min_score:
            continue
        gap = p["yes"] - best["yes"]
        if abs(gap) < min_gap:
            continue
        # different expiry = different bet, not a mispricing
        if not p["end"] or not best["end"] or p["end"] != best["end"]:
            continue
        pairs.append({"score": bs, "gap": gap, "pm": p, "k": best})
    pairs.sort(key=lambda x: -abs(x["gap"]))
    return pairs


# ------------------------------------------------------------------ report

def run_spreads(a):
    print("pulling Polymarket (keyless)...")
    pm = pm_markets()
    print(f"  {len(pm)} open markets")
    print("pulling Kalshi (signed)...")
    kal = kalshi_markets()
    print(f"  {len(kal)} open markets\n")

    pairs = find_spreads(pm, kal, a.min_gap, a.min_score, a.min_vol)
    if not pairs:
        print("No cross-venue pairs cleared the filters.")
        print("Either the venues do not list the same events right now, or")
        print("the matcher is too strict. Try --min-score 0.25 --min-gap 3.")
        return

    print("=" * 84)
    print(f"{len(pairs)} CANDIDATE PAIRS — verify every one by hand")
    print("=" * 84)
    for x in pairs[:a.top]:
        p, k = x["pm"], x["k"]
        side = "Polymarket higher" if x["gap"] > 0 else "Kalshi higher"
        print(f"\nmatch confidence {x['score']:.2f}   gap {x['gap']:+.1f}c   {side}")
        print(f"  PM  {p['yes']:>5.1f}c  vol ${p['vol']:>12,.0f}  ends {p['end']}")
        print(f"      {p['title'][:76]}")
        print(f"  KAL {k['yes']:>5.1f}c (bid {k['bid']:.0f}/ask {k['ask']:.0f})  "
              f"vol {k['vol']:>10,.0f}  ends {k['end']}")
        print(f"      {k['title'][:76]}")
        print(f"      {k['ticker']}")
        if k["rules"]:
            print(f"      rule: {k['rules'][:110]}")

    stamp = time.strftime("%Y%m%d_%H%M")
    f = OUT / f"spreads_{stamp}.json"
    f.write_text(json.dumps(pairs, indent=2, default=str))
    print(f"\nwrote {f}")

    print("\n" + "=" * 84)
    print("BEFORE TREATING ANY OF THESE AS AN EDGE")
    print("=" * 84)
    print("  1. Read both settlement rules. Different source, different cut")
    print("     time, or different rounding means it is not the same bet and")
    print("     the gap is not an edge.")
    print("  2. Check the end dates match. A gap across different expiries is")
    print("     just the time value of the event.")
    print("  3. You can only trade one leg. US IPs cannot place Polymarket")
    print("     orders, so this is a signal about Kalshi mispricing, not a")
    print("     hedged arbitrage. An unhedged leg carries full event risk.")
    print("  4. Kalshi's spread and fees come out of the gap before you see")
    print("     a cent of it.")


def run_whales(a):
    print(f"pulling Polymarket leaderboard ({a.window})...")
    rows, url = pm_leaderboard(a.window, a.top)
    if not rows:
        print("Leaderboard endpoint did not answer on any known path.")
        print("Polymarket has moved this surface before. Check")
        print("  https://docs.polymarket.com  for the current route,")
        print("then pass a wallet directly:  --wallet 0x...")
        return
    print(f"  answered by {url}\n")

    print("=" * 84)
    print(f"TOP TRADERS BY PROFIT — {a.window}")
    print("=" * 84)
    wallets = []
    for i, r in enumerate(rows[:a.top], 1):
        w = r.get("proxyWallet") or r.get("wallet") or r.get("address") or ""
        name = r.get("name") or r.get("pseudonym") or w[:12]
        profit = r.get("profit") or r.get("pnl") or r.get("amount") or 0
        vol = r.get("volume") or 0
        try:
            profit = float(profit)
            vol = float(vol)
        except Exception:
            pass
        wallets.append(w)
        print(f"{i:>3}. {str(name)[:26]:<27} profit ${profit:>13,.0f}  "
              f"vol ${vol:>13,.0f}  {w[:14]}")

    print("\n" + "=" * 84)
    print("WHAT THE TOP WALLETS HOLD RIGHT NOW")
    print("=" * 84)
    themes = defaultdict(lambda: {"n": 0, "usd": 0.0})
    for w in wallets[:a.depth]:
        if not w:
            continue
        pos = pm_positions(w, limit=40)
        if not pos:
            continue
        realised = sum(float(x.get("cashPnl") or 0) for x in pos)
        wipeouts = sum(1 for x in pos if float(x.get("curPrice") or 0) == 0)
        warn = ""
        if realised < 0:
            warn = f"   <<< open book is {realised:+,.0f}, ranking says otherwise"
        elif wipeouts >= len(pos) * 0.4 and len(pos) >= 5:
            warn = f"   <<< {wipeouts}/{len(pos)} positions worth zero"
        print(f"\n{w[:16]}...  {len(pos)} positions{warn}")
        for p in pos[:8]:
            title = (p.get("title") or p.get("slug") or "")[:56]
            val = float(p.get("currentValue") or 0)
            sz = float(p.get("size") or 0)
            avg = float(p.get("avgPrice") or 0) * 100
            cur = float(p.get("curPrice") or 0) * 100
            pnl = float(p.get("cashPnl") or 0)
            out = p.get("outcome", "")
            print(f"   ${val:>10,.0f}  {out:<4} {sz:>9,.0f} @ {avg:>5.1f}c "
                  f"now {cur:>5.1f}c  pnl ${pnl:>+10,.0f}  {title}")
            for t in keywords(title):
                themes[t]["n"] += 1
                themes[t]["usd"] += val
        time.sleep(0.2)

    if themes:
        print("\n" + "=" * 84)
        print("CROWDED THEMES AMONG WINNERS — repeated conviction")
        print("=" * 84)
        top = sorted(themes.items(), key=lambda kv: -kv[1]["usd"])[:20]
        for t, v in top:
            if v["n"] < 2:
                continue
            print(f"  {t:<24} {v['n']:>3} positions  ${v['usd']:>12,.0f}")

    print("\n" + "=" * 84)
    print("READ THIS BEFORE COPYING ANYONE")
    print("=" * 84)
    print("  A leaderboard is survivorship. The wallets shown are the ones")
    print("  that won over this window; the ones that took the same bets and")
    print("  lost are not on the list, so the strategy looks better than it is.")
    print("  You also see positions AFTER they were entered, at a worse price.")
    print("  Treat this as a source of IDEAS to test, never as a signal to")
    print("  follow. Test any theme the way you tested the others.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreads", action="store_true")
    ap.add_argument("--whales", action="store_true")
    ap.add_argument("--wallet")
    ap.add_argument("--min-gap", type=float, default=4.0,
                    help="minimum price gap in cents")
    ap.add_argument("--min-score", type=float, default=0.35,
                    help="minimum match confidence 0-1")
    ap.add_argument("--min-vol", type=float, default=1000)
    ap.add_argument("--window", default="30d")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--depth", type=int, default=5,
                    help="how many top wallets to inspect")
    a = ap.parse_args()

    if a.wallet:
        pos = pm_positions(a.wallet)
        print(f"{len(pos)} positions for {a.wallet}\n")
        for p in pos[:40]:
            print(f"  ${float(p.get('currentValue') or 0):>10,.0f}  "
                  f"{p.get('outcome',''):<4} pnl ${float(p.get('cashPnl') or 0):>+10,.0f}"
                  f"  {(p.get('title') or '')[:60]}")
        return
    if a.spreads:
        run_spreads(a)
    if a.whales:
        run_whales(a)
    if not (a.spreads or a.whales):
        ap.print_help()


if __name__ == "__main__":
    main()

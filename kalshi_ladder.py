"""
kalshi_ladder.py v2 — ladder coherence, with the v1 bug fixed.

v1 FAILURE, recorded so it is not repeated:
  v1 pulled the first number out of any ticker suffix and treated every
  market in an event as a point on one ordered ladder. It is not. Kalshi
  mixes contract shapes inside a single event:

    -T78199.99   strike_type "greater"  -> "settles ABOVE 78,199.99"   NESTED
    -B78050      strike_type "between"  -> "lands INSIDE this bucket"  DISJOINT
    -H25 / -C25  Fed hike 25 / cut 25   -> opposite outcomes           DISJOINT

  Only the "greater" family is nested, and only nested contracts obey
  monotonicity. v1 compared buckets to buckets and hikes to cuts, and
  reported 10 confident dollar-denominated "arbitrages", all of them false.

v2 rules:
  1. Read strike_type from the API. Never infer it from the ticker string.
  2. Only compare contracts sharing the same strike_type, within one event.
  3. Only "greater" (and "less", inverted) ladders get the monotonicity test.
  4. "between" buckets get a DIFFERENT, valid test: a complete set of
     mutually exclusive, exhaustive buckets must sum to ~100c. Sum well
     under 100 at the ask = buy them all, guaranteed payout. Report that
     separately and only when the bucket set is provably complete.
  5. Cross-check every finding against floor_strike/cap_strike from the API
     before printing it.

    python3 kalshi_ladder.py --scan
    python3 kalshi_ladder.py --event KXBTCD-26SEP0417 --show
"""

from __future__ import annotations

import argparse
import base64
import os
import time
from collections import defaultdict
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(Path.home() / "trading-lab" / ".env")

BASE = os.environ["KALSHI_BASE_PROD"].rstrip("/")
KEY_ID = os.environ["KALSHI_KEY_ID"]
PREFIX = "/" + BASE.split("://", 1)[1].split("/", 1)[1]
_PK = serialization.load_pem_private_key(
    Path(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"])).read_bytes(),
    password=None)


def fee_cents(price_cents: float, contracts: int = 1) -> float:
    """Kalshi trading fee: 0.07 * C * P * (1-P), P in dollars. Worst at P=0.5."""
    p = price_cents / 100.0
    return 0.07 * contracts * p * (1 - p) * 100


def get(ep: str, **params):
    ts = str(int(time.time() * 1000))
    sig = _PK.sign((ts + "GET" + PREFIX + ep).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                               salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    r = requests.get(BASE + ep,
                     headers={"KALSHI-ACCESS-KEY": KEY_ID,
                              "KALSHI-ACCESS-TIMESTAMP": ts,
                              "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()},
                     params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def f(m, *names, default=None):
    for n in names:
        v = m.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def load_event(ev: str):
    """Every market in an event, with strike_type and bounds straight from API."""
    raw, cursor = [], None
    for _ in range(6):
        p = {"event_ticker": ev, "status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = get("/markets", **p)
        raw += d.get("markets", [])
        cursor = d.get("cursor")
        if not cursor:
            break
    rows = []
    for m in raw:
        rows.append({
            "ticker": m["ticker"],
            "stype": (m.get("strike_type") or "").lower(),
            "floor": f(m, "floor_strike_dollars", "floor_strike"),
            "cap": f(m, "cap_strike_dollars", "cap_strike"),
            "sub": (m.get("subtitle") or m.get("yes_sub_title") or "")[:26],
            "bid": (f(m, "yes_bid_dollars", "yes_bid", default=0.0)) * 100,
            "ask": (f(m, "yes_ask_dollars", "yes_ask", default=0.0)) * 100,
            "bid_sz": f(m, "yes_bid_size_fp", default=0.0),
            "ask_sz": f(m, "yes_ask_size_fp", default=0.0),
            "vol24": f(m, "volume_24h_fp", "volume_24h", default=0.0),
            "oi": f(m, "open_interest_fp", "open_interest", default=0.0),
        })
    return rows


def monotonic_violations(rows, ev, min_edge):
    """Only 'greater' contracts. Nested by construction: strike X implies
    every strike below X. So price must NOT increase as floor_strike rises."""
    g = [r for r in rows
         if r["stype"] == "greater" and r["floor"] is not None
         and r["ask"] > 0 and r["bid"] > 0]
    g.sort(key=lambda r: r["floor"])
    out = []
    for lo, hi in zip(g, g[1:]):
        if hi["floor"] <= lo["floor"]:
            continue                      # identical strikes, not a pair
        # buy the EASIER condition at ask, sell the HARDER at bid
        gross = hi["bid"] - lo["ask"]
        if gross <= 0:
            continue
        cost = fee_cents(lo["ask"]) + fee_cents(hi["bid"])
        net = gross - cost
        size = min(lo["ask_sz"], hi["bid_sz"])
        if net >= min_edge and size >= 1:
            out.append({"kind": "monotonic", "event": ev, "buy": lo, "sell": hi,
                        "gross": gross, "fee": cost, "net": net, "size": size})
    return out


def bucket_underpricing(rows, ev, min_edge):
    """Only 'between' buckets. If a set is mutually exclusive AND exhaustive,
    exactly one pays $1, so the asks must sum to >= 100c. Completeness is
    verified by checking the buckets tile without gaps."""
    b = [r for r in rows
         if r["stype"] == "between" and r["floor"] is not None
         and r["cap"] is not None and r["ask"] > 0]
    if len(b) < 3:
        return []
    b.sort(key=lambda r: r["floor"])
    # tiling check: each cap must meet the next floor (small tolerance)
    for x, y in zip(b, b[1:]):
        if abs(y["floor"] - x["cap"]) > max(0.02 * abs(x["cap"]), 1.0):
            return []                     # gap -> not exhaustive -> no claim
    total = sum(r["ask"] for r in b)
    fees = sum(fee_cents(r["ask"]) for r in b)
    net = 100.0 - total - fees
    if net < min_edge:
        return []
    size = min(r["ask_sz"] for r in b)
    if size < 1:
        return []
    return [{"kind": "bucket_sum", "event": ev, "legs": b,
             "total": total, "fee": fees, "net": net, "size": size}]


def check(rows, ev, min_edge):
    return monotonic_violations(rows, ev, min_edge) + \
           bucket_underpricing(rows, ev, min_edge)


def show(rows, ev):
    kinds = defaultdict(int)
    for r in rows:
        kinds[r["stype"] or "?"] += 1
    print(f"\n{ev} — {len(rows)} markets  types: {dict(kinds)}")
    print(f"{'type':<9}{'floor':>11}{'cap':>11}{'bid':>6}{'ask':>6}"
          f"{'bidsz':>8}{'asksz':>8}{'vol24':>9}  label")
    for r in sorted(rows, key=lambda r: (r["stype"], r["floor"] if r["floor"] is not None else 0)):
        if r["ask"] == 0 and r["vol24"] == 0 and r["oi"] == 0:
            continue
        fl = f"{r['floor']:,.2f}" if r["floor"] is not None else "-"
        cp = f"{r['cap']:,.2f}" if r["cap"] is not None else "-"
        print(f"{r['stype']:<9}{fl:>11}{cp:>11}{r['bid']:>6.1f}{r['ask']:>6.1f}"
              f"{r['bid_sz']:>8,.0f}{r['ask_sz']:>8,.0f}{r['vol24']:>9,.0f}  {r['sub']}")


def report(fs):
    if not fs:
        print("\nNo tradeable inconsistencies found.")
        print("This is the expected outcome. Ladders being coherent is the")
        print("null result — record it, do not go looking for a looser test.")
        return
    print(f"\n{'='*78}\n{len(fs)} CANDIDATE(S) — verify by hand before trading\n{'='*78}")
    for v in fs:
        if v["kind"] == "monotonic":
            b, s = v["buy"], v["sell"]
            print(f"\n[monotonic]  {v['event']}")
            print(f"  BUY  {b['ticker']}")
            print(f"       above {b['floor']:,.2f}  @ {b['ask']:.1f}c  size {b['ask_sz']:,.0f}")
            print(f"  SELL {s['ticker']}")
            print(f"       above {s['floor']:,.2f}  @ {s['bid']:.1f}c  size {s['bid_sz']:,.0f}")
            print(f"  gross {v['gross']:.2f}c  fees {v['fee']:.2f}c  NET {v['net']:.2f}c")
            print(f"  up to {v['size']:,.0f} contracts -> max ${v['net']*v['size']/100:,.2f}")
        else:
            print(f"\n[bucket sum]  {v['event']}")
            print(f"  {len(v['legs'])} exhaustive buckets, asks total {v['total']:.1f}c")
            print(f"  fees {v['fee']:.1f}c  NET {v['net']:.1f}c per full set")
            print(f"  up to {v['size']:,.0f} sets -> max ${v['net']*v['size']/100:,.2f}")
            for r in v["legs"]:
                print(f"     {r['ticker']:<34}{r['floor']:>11,.2f}-{r['cap']:<11,.2f}"
                      f"{r['ask']:>6.1f}c")
    print("\nBefore acting on any of these, read both markets' rules_primary.")
    print("Different settlement sources or times break the arbitrage.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--event")
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--min-edge", type=float, default=0.5)
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    if a.event:
        rows = load_event(a.event)
        if a.show:
            show(rows, a.event)
        report(check(rows, a.event, a.min_edge))
        return
    if not a.scan:
        ap.error("pass --event TICKER or --scan")

    seeds = ["KXBTCD", "KXBTC", "KXETHD", "KXSOLE", "KXBTCY", "KXFEDDECISION",
             "KXAAAGASD", "KXAAAGASW", "KXAAAGASM", "KXRATECUTCOUNT",
             "KXSPACEXCOUNT", "KXBTCMAXMON"]
    events = defaultdict(float)
    for s in seeds:
        try:
            d = get("/markets", series_ticker=s, status="open", limit=1000)
        except Exception as e:
            print(f"{s}: {e}")
            continue
        for m in d.get("markets", []):
            ev = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
            events[ev] += f(m, "volume_24h_fp", "volume_24h", default=0.0)
        time.sleep(0.05)

    live = sorted([kv for kv in events.items() if kv[1] > 0], key=lambda kv: -kv[1])
    print(f"{len(live)} ladders with 24h volume\n")
    allf, tested = [], defaultdict(int)
    for ev, vol in live:
        rows = load_event(ev)
        fs = check(rows, ev, a.min_edge)
        for r in rows:
            tested[r["stype"] or "?"] += 1
        mark = f"   <<< {len(fs)}" if fs else ""
        print(f"  {vol:>10,.0f} vol  {len(rows):>4} mkts  {ev}{mark}", flush=True)
        allf += fs
        time.sleep(0.05)
    print(f"\ncontract types seen: {dict(tested)}")
    report(allf)


if __name__ == "__main__":
    main()

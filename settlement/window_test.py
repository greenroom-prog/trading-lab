"""
window_test.py — the 30-45 second window. Is anyone slow there?

THE THESIS
  Between T-45s and T-30s the settling average is already 15-30 seconds
  locked. From measured volatility (sigma ~4 $/sqrt-s) the remaining move
  has sd $6.50-$11.80, so a strike $25 away flips 1.7% of the time at
  T-45s and 0.01% at T-30s.

  If the market has not yet priced that certainty — if a strike that is
  effectively decided still trades at, say, 92c — the gap is readable
  without predicting anything.

WHAT IS RECORDED, every second in the window
  - the locked-in average and how far it can still move
  - for EVERY strike: our probability, the market's price, the distance
  - whether a trade was AVAILABLE (real resting size, real profit after
    Kalshi's fee)
  - and afterwards, whether it would have WON

The last part is what makes this a test rather than an observation. Every
flagged opportunity is scored against the actual settlement.

    python3 window_test.py --series KXBTCD --secs 4200 --max-wait 70
    python3 window_test.py --score          # score everything logged
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import math
import statistics as st
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import settle_model as sm
import live_edge as le
import final_window as fw

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)
OUT = DATA / "window_test.csv"

HDR = ["t", "event", "secs_left", "locked", "spot", "avg60s", "centre",
       "band", "ticker", "strike", "distance", "z", "our_p", "mkt_mid",
       "side", "cost_c", "fee_c", "net_c", "size", "profit_if_right",
       "settled", "won"]


def write(rows):
    exists = OUT.exists()
    with OUT.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HDR)
        if not exists:
            w.writeheader()
        w.writerows(rows)


def scan_strikes(legs, centre, band, min_z, min_net, mode="certain"):
    """Every strike, with our probability and what the market charges.

    TWO MODES, because the opportunity has a different shape at each range.

    mode='certain' (the final minute): find strikes we are >min_z sd sure
    about that the market still charges for. Tested 2026-09-01: nothing,
    every second. Market makers price certainty instantly.

    mode='edge' (2-5 minutes out): nothing is certain — the band is $30-70
    — so certainty is the wrong test. Instead compare our probability to
    the market price and take the difference, after fees. This is where
    the Brier comparison showed the model marginally ahead.
    """
    out = []
    for m in legs:
        if (m.get("strike_type") or "").lower() != "greater":
            continue
        try:
            k = float(m.get("floor_strike_dollars") or m.get("floor_strike"))
        except (TypeError, ValueError):
            continue
        bid = float(m.get("yes_bid_dollars") or 0) * 100
        ask = float(m.get("yes_ask_dollars") or 0) * 100
        bsz = float(m.get("yes_bid_size_fp") or 0)
        asz = float(m.get("yes_ask_size_fp") or 0)
        if ask <= 0:
            continue
        dist = centre - k
        z = abs(dist) / band if band and band > 0 else 99.0
        p = 1.0 - 0.5 * (1 + math.erf((k - centre) /
                                      (band * math.sqrt(2)))) if band else \
            (1.0 if dist > 0 else 0.0)
        mid = (bid + ask) / 2
        if mode == "certain":
            if z < min_z:
                continue
            if dist > 0:
                cost, side, size = ask, "YES", asz
            else:
                cost, side, size = 100 - bid, "NO", bsz
            if cost >= 100 or size < 1:
                continue
            fee = sm.fee_cents(cost)
            net = 100 - cost - fee
            if net < min_net:
                continue
        else:
            # edge mode: our probability vs what it costs to take that side
            pc = p * 100
            yes_net = pc - ask - sm.fee_cents(ask) if (ask > 0 and asz >= 1) else None
            no_cost = 100 - bid
            no_net = ((100 - pc) - no_cost - sm.fee_cents(no_cost)
                      if (bid > 0 and bsz >= 1) else None)
            side, net, cost, size = None, None, None, None
            for sd_, n_, c_, s_ in (("YES", yes_net, ask, asz),
                                    ("NO", no_net, no_cost, bsz)):
                if n_ is not None and (net is None or n_ > net):
                    side, net, cost, size = sd_, n_, c_, s_
            if net is None or net < min_net:
                continue
            fee = sm.fee_cents(cost)
        out.append({"ticker": m["ticker"], "strike": k, "distance": dist,
                    "z": z, "our_p": p * 100, "mkt_mid": mid, "side": side,
                    "cost_c": cost, "fee_c": fee, "net_c": net,
                    "size": size, "profit_if_right": net * size / 100.0})
    return out


async def watch(a):
    eng = le.Engine(a.series)
    picked = le.pick_event(a.series, a.max_wait)
    if not picked:
        f_, t = le.soonest_event(a.series)
        print(f"next settlement {t/60:.0f} min out; raise --max-wait")
        return
    eng.event, eng.legs, t_left = picked
    eng.close_iso = eng.legs[0].get("close_time", "")
    print(f"{eng.event}  settles in {t_left/60:.1f} min")
    print(f"testing the T-{a.hi:.0f}s to T-{a.lo:.0f}s window   log -> {OUT}\n")

    ws = None
    for u in le.WS_CANDIDATES:
        try:
            ws = await le.dial(u)
            break
        except Exception:
            continue
    if ws is None:
        print("no websocket host answered")
        return
    idx = {"KXBTCD": "BRTI", "KXETHD": "ETHUSD_RTI"}.get(a.series, "BRTI")
    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
        "channels": ["cfbenchmarks_value"], "index_ids": [idx]}}))

    end = time.time() + a.secs
    last_book, seen, logged = 0.0, set(), []
    try:
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") != "cfbenchmarks_value":
                continue
            m = msg.get("msg", {})
            try:
                spot = float(json.loads(m.get("data", "{}"))["value"])
            except Exception:
                continue
            av = m.get("avg_60s_data") or {}
            eng.on_index(spot, av.get("value"), av.get("window_size"),
                         time.time())
            left = le.secs_to(eng.close_iso)
            if left is None:
                continue
            if left <= 0:
                print("\nsettled")
                break
            if left > a.hi:
                k = int(left) // 60
                if k not in seen:
                    seen.add(k)
                    print(f"  T-{left:.0f}s  spot {spot:,.2f}  waiting")
                continue
            if left < a.lo:
                continue

            if time.time() - last_book > 1.0:
                try:
                    d = le.rest_get("/markets", event_ticker=eng.event,
                                    status="open", limit=1000)
                    if d.get("markets"):
                        eng.legs = d["markets"]
                except Exception:
                    pass
                last_book = time.time()

            sigma = eng.ticks.sigma_1s()
            if sigma is None:
                print(f"  T-{left:.0f}s  volatility not measured yet")
                continue
            centre, band = fw.implied_settlement(eng.avg60, eng.window_n,
                                                 spot, sigma, left)
            if centre is None or band is None:
                continue
            if left > 60:
                # the settlement window has not opened; the rolling average
                # is not the settling quantity. Use spot and the full
                # horizon uncertainty, with the measured scale correction.
                centre = spot
                band = sm.sd_of_settlement(sigma, left)
                if band is None:
                    continue
            hits = scan_strikes(eng.legs, centre, band, a.min_z, a.min_net,
                                mode=a.mode)
            stamp = datetime.now().isoformat(timespec="milliseconds")
            rows = [{"t": stamp, "event": eng.event,
                     "secs_left": round(left, 1), "locked": round(60 - left, 1),
                     "spot": spot, "avg60s": eng.avg60,
                     "centre": round(centre, 2), "band": round(band, 3),
                     **{k: h[k] for k in
                        ("ticker", "strike", "distance", "z", "our_p",
                         "mkt_mid", "side", "cost_c", "fee_c", "net_c",
                         "size", "profit_if_right")},
                     "settled": "", "won": ""} for h in hits]
            if rows:
                write(rows)
                logged += rows
                b = max(hits, key=lambda h: h["profit_if_right"])
                print(f"T-{left:5.1f}s band ${band:6.2f}  {len(hits)} candidate(s)"
                      f"  best {b['side']} {b['ticker'][-11:]} "
                      f"${b['distance']:+.0f} away ({b['z']:.1f} sd) "
                      f"cost {b['cost_c']:.0f}c net {b['net_c']:.1f}c "
                      f"x{b['size']:,.0f}")
            else:
                if int(left) % 15 == 0:
                    print(f"T-{left:5.1f}s band ${band:6.2f}  nothing over "
                          f"{a.min_net:.0f}c")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
    if logged:
        print(f"\n{len(logged)} candidates recorded. Scoring:")
    score()


def score():
    """Score every logged candidate against what actually settled."""
    if not OUT.exists():
        print("no window_test.csv yet")
        return
    rows = list(csv.DictReader(OUT.open()))
    if not rows:
        print("no candidates were ever recorded.")
        print("That is a result: in the windows watched, the market had")
        print("already priced every strike we were confident about.")
        return

    # settled value per event = last avg60s seen in any log
    settle = {}
    for pat in ("window_test.csv", "final_window_*.csv", "live_edge_*.csv"):
        for f in glob.glob(str(DATA / pat)):
            for r in csv.DictReader(open(f)):
                e, t = r.get("event"), r.get("secs_left")
                v = r.get("avg60s")
                if not e or not v:
                    continue
                try:
                    t = float(t)
                    v = float(v)
                except (TypeError, ValueError):
                    continue
                cur = settle.get(e)
                if cur is None or t < cur[0]:
                    settle[e] = (t, v)

    wins = losses = 0
    pnl = 0.0
    by_z = defaultdict(lambda: [0, 0])
    unscored = 0
    for r in rows:
        ev = r["event"]
        if ev not in settle:
            unscored += 1
            continue
        fin = settle[ev][1]
        k = float(r["strike"])
        yes = fin > k
        won = (r["side"] == "YES" and yes) or (r["side"] == "NO" and not yes)
        r["settled"] = round(fin, 2)
        r["won"] = int(won)
        net = float(r["net_c"])
        cost = float(r["cost_c"])
        pnl += (net if won else -cost) / 100.0
        wins += won
        losses += (not won)
        zb = int(float(r["z"]))
        by_z[min(zb, 6)][0 if won else 1] += 1

    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HDR)
        w.writeheader()
        w.writerows(rows)

    n = wins + losses
    print("\n" + "=" * 74)
    print(f"SCORED {n} candidate(s) against actual settlements")
    print("=" * 74)
    if unscored:
        print(f"  {unscored} not yet scoreable (settlement unknown)")
    if n == 0:
        return
    print(f"  won {wins}  lost {losses}   hit rate {wins/n*100:.1f}%")
    print(f"  P&L at ONE contract each: ${pnl:+.2f}")
    print(f"\n  {'confidence':>12}{'won':>7}{'lost':>7}{'rate':>9}")
    for z in sorted(by_z):
        w_, l_ = by_z[z]
        tot = w_ + l_
        print(f"  {f'{z}-{z+1} sd':>12}{w_:>7}{l_:>7}{w_/tot*100:>8.1f}%")
    print("\n  A losing trade costs the full stake, so at 95c a single loss")
    print("  undoes twenty wins. Hit rate must be read against the price.")
    print(f"  {n} observations is not a sample. Keep running.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--secs", type=int, default=4200)
    ap.add_argument("--max-wait", type=float, default=70)
    ap.add_argument("--hi", type=float, default=45, help="window start")
    ap.add_argument("--lo", type=float, default=30, help="window end")
    ap.add_argument("--min-z", type=float, default=3.0,
                    help="how many sd from the strike before we call it")
    ap.add_argument("--min-net", type=float, default=2.0,
                    help="minimum net cents after fees")
    ap.add_argument("--mode", choices=["certain", "edge"], default=None,
                    help="certain: final-minute lock-in. edge: probability "
                         "vs price. Defaults by window.")
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args()
    if a.mode is None:
        a.mode = "certain" if a.hi <= 60 else "edge"
    if a.score:
        score()
    else:
        asyncio.run(watch(a))


if __name__ == "__main__":
    main()

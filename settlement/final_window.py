"""
final_window.py — is the last-60-seconds gap actually TRADEABLE?

WHAT WE KNOW
  Inside the final 60 seconds the exchange broadcasts the running 60-second
  average, which IS the settlement value. On 2026-09-01 the model scored
  Brier 0.0000 there while the market scored 0.0496 — the market was still
  pricing uncertainty in a number already visible on Kalshi's own feed.

  That is an information-latency gap, not forecasting skill. This file
  asks the only question that matters next: could you have been filled,
  and would it have paid after costs?

WHAT IT MEASURES, per second inside the window
  - the settlement value implied by the running average so far
  - which strikes are ALREADY DECIDED (the remaining seconds cannot move
    the average past them) and what the market still charges for them
  - the resting size on the profitable side
  - net profit after Kalshi's fee, at the size actually available

A "decided" strike is one where the average would need the remaining
seconds to move further than physically plausible given measured
volatility. The threshold is measured, not assumed.

    python3 final_window.py --series KXBTCD --secs 900
    python3 final_window.py --report
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics as st
import time
from datetime import datetime
from pathlib import Path

import settle_model as sm
import live_edge as le

DATA = Path(__file__).resolve().parent / "data"
DATA.mkdir(exist_ok=True)
OUT = DATA / f"final_window_{datetime.now():%Y%m%d}.csv"

HDR = ["t", "event", "secs_left", "obs", "spot", "avg60s", "implied_settle",
       "band", "ticker", "strike", "decided", "side", "bid", "ask",
       "bid_sz", "ask_sz", "cost_c", "fee_c", "net_c", "max_contracts",
       "max_profit"]


def implied_settlement(avg60, obs, spot, sigma, secs_left, window=60):
    """Best estimate of the FINAL 60-second average, and how far it can
    still move.

    THE BUG THIS FIXES
      Kalshi's avg_60s is a ROLLING mean of the last 60 seconds, always.
      v1 read its window_size field (which sits at 60 once the feed is
      warm) as "60 seconds of the settlement window are locked in", and
      so reported a band of +/-0.00 at T-84s. Nothing is locked at T-84s.
      The settlement window has not started. Every opportunity that
      version flagged was an artefact.

      What is actually locked at time T is the part of the settlement
      window already elapsed:  locked = 60 - T  seconds, and only once
      T < 60.

    Before the window opens the rolling average is not the settling
    quantity at all, so spot is the estimate and the full averaging
    uncertainty applies.
    """
    if secs_left is None or spot is None:
        return None, None
    T = max(float(secs_left), 0.0)

    if T >= window:
        # settlement window has not begun; nothing is locked
        if sigma is None:
            return spot, None
        pre = max(T - window, 0.0)
        sumsq = window * (window + 1) * (2 * window + 1) / 6.0
        var = pre + sumsq / (window ** 2)
        return spot, sigma * math.sqrt(var)

    # WEIGHTS MUST SUM TO EXACTLY `window`.
    # A float `locked` (59.1) with an int-rounded `rem` (1) summed to
    # 60.1, and dividing by 60 inflated the estimate. On 2026-09-01 that
    # 0.1s mismatch put the centre 128 dollars above BOTH inputs, flipped
    # every near-money strike, and manufactured a "$34,000 opportunity".
    rem_f = max(min(T, window), 0.0)          # seconds still to come
    locked = window - rem_f                   # seconds already inside
    rem = int(math.ceil(rem_f))               # whole seconds, for variance
    # the rolling average currently covers the last 60s, of which `locked`
    # seconds are inside the settlement window. Approximate the locked
    # portion's mean with the rolling average — the best available proxy —
    # and carry the remainder as still-unknown.
    if avg60 is None:
        return spot, None
    centre = (avg60 * locked + spot * rem_f) / window
    # a weighted mean of two values can never sit outside them
    lo, hi = min(avg60, spot), max(avg60, spot)
    if not (lo - 1e-6 <= centre <= hi + 1e-6):
        raise AssertionError(
            f"blend {centre:.2f} outside inputs [{lo:.2f}, {hi:.2f}] "
            f"- weights locked={locked} rem={rem_f} window={window}")
    if sigma is None or rem <= 0:
        return centre, 0.0
    sumsq = rem * (rem + 1) * (2 * rem + 1) / 6.0
    band = sigma * math.sqrt(sumsq) / window
    return centre, band


def decided_strikes(legs, centre, band, z=4.0):
    """Strikes the remaining seconds cannot plausibly reach.

    z=4 means the average would need a 4-sigma move in the seconds left.
    Deliberately strict: a wrong 'decided' call is a full loss.
    """
    out = []
    for m in legs:
        stype = (m.get("strike_type") or "").lower()
        if stype != "greater":
            continue
        fl = le._f = None
        try:
            fl = float(m.get("floor_strike_dollars") or m.get("floor_strike"))
        except (TypeError, ValueError):
            continue
        bid = float(m.get("yes_bid_dollars") or 0) * 100
        ask = float(m.get("yes_ask_dollars") or 0) * 100
        bsz = float(m.get("yes_bid_size_fp") or 0)
        asz = float(m.get("yes_ask_size_fp") or 0)
        margin = centre - fl
        if band is not None and band > 0 and abs(margin) < z * band:
            continue                      # still genuinely uncertain
        if margin > 0:
            # settles YES. Profit if we can buy YES below 100.
            if ask <= 0 or ask >= 100 or asz < 1:
                continue
            cost, side, sz = ask, "YES", asz
        else:
            # settles NO. Buying NO costs (100 - bid).
            if bid <= 0 or bid <= 1 or bsz < 1:
                continue
            cost, side, sz = 100 - bid, "NO", bsz
        fee = sm.fee_cents(cost)
        net = 100 - cost - fee
        out.append({"ticker": m["ticker"], "strike": fl, "side": side,
                    "bid": bid, "ask": ask, "bid_sz": bsz, "ask_sz": asz,
                    "cost_c": cost, "fee_c": fee, "net_c": net,
                    "max_contracts": sz, "max_profit": net * sz / 100.0,
                    "margin": margin})
    return out


def write(rows):
    exists = OUT.exists()
    with OUT.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HDR)
        if not exists:
            w.writeheader()
        w.writerows(rows)


async def watch(a):
    eng = le.Engine(a.series)
    picked = le.pick_event(a.series, a.max_wait)
    if not picked:
        found, t = le.soonest_event(a.series)
        print(f"next settlement {t/60:.0f} min out; raise --max-wait to wait")
        return
    eng.event, eng.legs, t_left = picked
    eng.close_iso = eng.legs[0].get("close_time", "")
    print(f"{eng.event}  settles in {t_left/60:.1f} min")
    print(f"watching the final {a.window}s.  log -> {OUT}\n")

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
    last_book = 0.0
    printed = set()
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
            if left is None or left <= 0:
                print("\nsettled")
                break
            if left > a.window:
                if int(left) % 30 == 0 and int(left) not in printed:
                    printed.add(int(left))
                    print(f"  T-{left:.0f}s  spot {spot:,.2f}  waiting for the "
                          f"final {a.window}s")
                continue

            if time.time() - last_book > a.refresh:
                try:
                    d = le.rest_get("/markets", event_ticker=eng.event,
                                    status="open", limit=1000)
                    if d.get("markets"):
                        eng.legs = d["markets"]
                except Exception:
                    pass
                last_book = time.time()

            sigma = eng.ticks.sigma_1s()
            centre, band = implied_settlement(eng.avg60, eng.window_n,
                                              spot, sigma, left)
            if centre is None:
                continue
            if band is None:
                continue
            hits = decided_strikes(eng.legs, centre, band, z=a.z)
            stamp = datetime.now().isoformat(timespec="milliseconds")
            locked = max(60 - left, 0)
            rows = [{"t": stamp, "event": eng.event,
                     "secs_left": round(left, 1), "obs": round(locked, 1),
                     "spot": spot, "avg60s": eng.avg60,
                     "implied_settle": round(centre, 2),
                     "band": round(band, 3) if band is not None else "",
                     **{k: h[k] for k in
                        ("ticker", "strike", "side", "bid", "ask",
                         "bid_sz", "ask_sz", "cost_c", "fee_c", "net_c",
                         "max_contracts", "max_profit")},
                     "decided": 1} for h in hits]
            if rows:
                write(rows)
                best = max(hits, key=lambda h: h["max_profit"])
                print(f"T-{left:4.0f}s locked {locked:>2.0f}s  settle "
                      f"{centre:,.2f} +/-{band:,.2f}   "
                      f"{len(hits)} decided   best: {best['side']} "
                      f"{best['ticker'][-12:]} at {best['cost_c']:.0f}c "
                      f"net {best['net_c']:.1f}c x{best['max_contracts']:,.0f} "
                      f"= ${best['max_profit']:,.2f}")
            elif int(left) % 10 == 0:
                print(f"T-{left:4.0f}s locked {locked:>2.0f}s  settle "
                      f"{centre:,.2f} +/-{band:,.2f}   nothing decided+cheap")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
    report()


def report(path=None):
    p = Path(path) if path else OUT
    if not p.exists():
        print("no data yet")
        return
    rows = list(csv.DictReader(p.open()))
    if not rows:
        print("empty log")
        return
    print(f"\n{'='*74}\n{len(rows)} decided-and-mispriced observations\n{'='*74}")
    profits = [float(r["max_profit"]) for r in rows]
    nets = [float(r["net_c"]) for r in rows]
    szs = [float(r["max_contracts"]) for r in rows]
    print(f"  net per contract: median {st.median(nets):.1f}c  "
          f"max {max(nets):.1f}c")
    print(f"  size available:   median {st.median(szs):,.0f}  "
          f"max {max(szs):,.0f}")
    print(f"  best single opportunity: ${max(profits):,.2f}")
    by_t = {}
    for r in rows:
        b = int(float(r["secs_left"]) // 10 * 10)
        by_t.setdefault(b, []).append(float(r["max_profit"]))
    print(f"\n  {'T-window':>10}{'n':>6}{'median $':>12}{'max $':>12}")
    for b in sorted(by_t):
        v = by_t[b]
        print(f"  {f'{b}-{b+10}s':>10}{len(v):>6}{st.median(v):>12,.2f}"
              f"{max(v):>12,.2f}")
    print("\n  These are OPPORTUNITIES OBSERVED, not trades taken. Whether")
    print("  the resting size is still there when an order arrives is a")
    print("  different question, and only a live order answers it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--secs", type=int, default=900)
    ap.add_argument("--window", type=int, default=90)
    ap.add_argument("--refresh", type=float, default=1.0)
    ap.add_argument("--max-wait", type=float, default=40)
    ap.add_argument("--z", type=float, default=4.0)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--file")
    a = ap.parse_args()
    if a.report:
        report(a.file)
    else:
        asyncio.run(watch(a))


if __name__ == "__main__":
    main()

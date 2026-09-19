"""
late_reversal.py — inside the last minute, how often does it flip?

THE QUESTION THAT MATTERS FOR TRADING
  At T-60s (or T-30s) the settlement average is partly locked. If the
  estimate says a strike settles YES, how often does it actually settle
  NO? That flip rate IS the risk of acting on a late read.

  Two distinct things are measured, because they are not the same:

  1. DIRECTIONAL FLIP — did price direction reverse between T-60 and
     settlement? A coin flip is 50%.

  2. STRIKE FLIP — the one that costs money. Given the estimate at time T
     put the settlement on one side of a strike, how often did the final
     value land on the other side? This depends on how CLOSE the estimate
     was to the strike, so it is reported by distance in dollars.

  Measured from your own logged settlements. Where there are too few, it
  says so rather than producing a number.

    python3 late_reversal.py
    python3 late_reversal.py --at 60 30 15
"""

from __future__ import annotations

import argparse
import csv
import glob
import math
import statistics as st
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"


def load_sessions():
    """One record per settlement event: the path of (secs_left, spot,
    avg60s, implied_settle) plus the final settled value."""
    ev = defaultdict(dict)
    for pat in ("live_edge_*.csv", "final_window_*.csv"):
        for f in glob.glob(str(DATA / pat)):
            for r in csv.DictReader(open(f)):
                e = r.get("event")
                t = r.get("secs_left")
                if not e or t in (None, ""):
                    continue
                try:
                    t = float(t)
                except ValueError:
                    continue
                d = ev[e]
                if t in d:
                    continue
                def g(k):
                    v = r.get(k)
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return None
                d[t] = {"spot": g("spot"), "avg60s": g("avg60s"),
                        "centre": g("implied_settle") or g("centre")}
    out = []
    for e, d in ev.items():
        pts = sorted(d.items(), key=lambda kv: -kv[0])
        if len(pts) < 5:
            continue
        # settled value: the last avg60s observed, the exchange's number
        final = None
        for t, v in sorted(d.items(), key=lambda kv: kv[0]):
            if v["avg60s"]:
                final = v["avg60s"]
                break
        if final is None:
            continue
        out.append({"event": e, "path": pts, "final": final,
                    "t_last": min(d)})
    return out


def at_time(path, target):
    """The observation closest to `target` seconds left, or None."""
    best, bd = None, 1e9
    for t, v in path:
        d = abs(t - target)
        if d < bd:
            best, bd = v, d
    return best if bd <= 15 else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", nargs="*", type=float, default=[60, 45, 30, 15])
    ap.add_argument("--bands", nargs="*", type=float,
                    default=[10, 25, 50, 100, 250])
    a = ap.parse_args()

    ss = load_sessions()
    if not ss:
        print("no logged settlements found in data/. Run live_edge.py or")
        print("final_window.py through a few settlements first.")
        return

    print(f"{len(ss)} logged settlement(s)\n")
    for s in ss:
        print(f"  {s['event']:<24} settled {s['final']:,.2f}  "
              f"{len(s['path'])} samples down to T-{s['t_last']:.0f}s")

    print("\n" + "=" * 76)
    print("1. DIRECTIONAL FLIP — did it reverse between T and settlement?")
    print("=" * 76)
    print(f"{'from':>8}{'sessions':>10}{'flipped':>10}{'rate':>9}   note")
    for T in a.at:
        flips, n = 0, 0
        for s in ss:
            here = at_time(s["path"], T)
            earlier = at_time(s["path"], T + 30)
            if not here or not earlier or not here["spot"] or not earlier["spot"]:
                continue
            trend = here["spot"] - earlier["spot"]
            after = s["final"] - here["spot"]
            if abs(trend) < 1e-9:
                continue
            n += 1
            if trend * after < 0:
                flips += 1
        if n == 0:
            print(f"{T:>7.0f}s{'--':>10}{'--':>10}{'--':>9}   no paired samples")
            continue
        rate = flips / n * 100
        se = math.sqrt(0.25 / n) * 100
        note = ("too few to read" if n < 20 else
                "coin flip" if abs(rate - 50) < 2 * se else
                "reverses more" if rate > 50 else "trends more")
        print(f"{T:>7.0f}s{n:>10}{flips:>10}{rate:>8.1f}%   {note}")

    print("\n" + "=" * 76)
    print("2. STRIKE FLIP — the one that costs money")
    print("=" * 76)
    print("  If at time T the estimate sat D dollars above a strike, how")
    print("  often did the settlement land BELOW it anyway?\n")
    print(f"{'from':>8}{'distance':>12}{'cases':>8}{'flipped':>9}{'rate':>8}")
    for T in a.at:
        for i, band in enumerate(a.bands):
            lo = a.bands[i - 1] if i else 0.0
            cases, flips = 0, 0
            for s in ss:
                here = at_time(s["path"], T)
                if not here or not here["centre"]:
                    continue
                est, fin = here["centre"], s["final"]
                # every hypothetical strike at distance lo..band from est
                for sign in (1, -1):
                    for d in (lo + (band - lo) * k / 8 for k in range(1, 9)):
                        strike = est - sign * d      # est is above (sign=1)
                        cases += 1
                        pred = est > strike
                        act = fin > strike
                        if pred != act:
                            flips += 1
            if cases == 0:
                continue
            rate = flips / cases * 100
            print(f"{T:>7.0f}s{f'${lo:.0f}-{band:.0f}':>12}{cases:>8}"
                  f"{flips:>9}{rate:>7.1f}%")

    print("\n" + "=" * 76)
    print("HOW TO READ THIS")
    print("=" * 76)
    print("  The flip rate is the chance a late read is WRONG. A 5% flip")
    print("  rate at $50 means: acting on that read wins 19 times and loses")
    print("  once, and the loss is the whole stake.")
    print(f"  Everything here rests on {len(ss)} settlement(s). That is not")
    print("  enough to trade on. Twenty would begin to be.")


if __name__ == "__main__":
    main()

"""
oil_event_study.py — measure how crude reacts to diplomacy vs escalation.

The claim under test: US-Iran DIPLOMACY headlines drive crude down, and
ESCALATION headlines drive it up, by amounts large enough to trade.

Method, in the order that kills it cheapest:

  1. Baseline first. Compute the distribution of ALL daily crude moves in
     the window. If event-day moves are not distinguishable from the
     everyday tail, the claim is dead and nothing else matters.
  2. Event days only then. Mean and median move by event class.
  3. Permutation test. Shuffle the event labels across all trading days
     1,000 times. If randomly chosen days produce the same separation as
     the real event days, the pattern is noise wearing a story.
  4. Direction accuracy. What fraction of diplomacy days were actually
     down, escalation days actually up. A coin flip is 50%.
  5. Next-day drift. Does the move continue, reverse, or stop? This is the
     only part that is tradeable at retail speed, because the event-day
     move happens before you can act on the headline.

Point 5 is the one that usually kills these. The predecessor project's
best strategy died on exactly this: filling at a price you only know
after the fact.

    python3 oil_event_study.py
    python3 oil_event_study.py --events my_events.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import random
import statistics as st
from datetime import datetime, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path.home() / "trading-lab" / ".env")
FRED_KEY = os.getenv("FRED_KEY")

# US-Iran 2026 headline events. class: D = diplomacy/de-escalation,
# E = escalation/conflict. Dates are the day the news broke.
# EDIT THIS LIST — it is the input the whole study depends on, and a
# mislabeled or misdated event silently corrupts the result.
EVENTS = [
    ("2026-02-28", "E", "US/Israel airstrikes on Iran begin; Hormuz closed"),
    ("2026-03-06", "E", "Trump: no deal except unconditional surrender"),
    ("2026-03-19", "E", "US aerial campaign to reopen Hormuz begins"),
    ("2026-03-23", "E", "Tehran denies negotiating, no shipping restore"),
    ("2026-03-24", "D", "US 15-point proposal, one-month ceasefire push"),
    ("2026-04-05", "E", "New Hormuz deadline, infrastructure threats"),
    ("2026-04-07", "D", "Two-week ceasefire agreed"),
    ("2026-04-08", "D", "Ceasefire confirmed, safe passage announced"),
    ("2026-04-11", "D", "Islamabad direct talks begin"),
    ("2026-04-13", "E", "Islamabad talks fail; US naval blockade ordered"),
    ("2026-05-04", "D", "Operation Project Freedom temporary halt"),
    ("2026-06-14", "D", "Memorandum of understanding announced"),
    ("2026-07-15", "E", "Conflict resumes; Iran strikes 3 vessels"),
    ("2026-08-28", "D", "US-Venezuela oil deal announced"),
]


def fred(series_id, start="2025-06-01"):
    if not FRED_KEY:
        raise SystemExit("FRED_KEY missing from .env")
    r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": series_id, "api_key": FRED_KEY,
                             "file_type": "json", "observation_start": start},
                     timeout=30)
    r.raise_for_status()
    return [(o["date"], float(o["value"]))
            for o in r.json()["observations"] if o["value"] not in (".", "")]


def pct(a, b):
    return (b - a) / a * 100 if a else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", help="CSV: date,class,label (overrides built-in)")
    ap.add_argument("--series", default="DCOILWTICO", help="FRED series")
    ap.add_argument("--trials", type=int, default=1000)
    a = ap.parse_args()

    events = EVENTS
    if a.events:
        with open(a.events) as f:
            events = [(r[0], r[1].upper(), r[2]) for r in csv.reader(f) if r]

    print(f"pulling {a.series} from FRED...")
    series = fred(a.series)
    dates = [d for d, _ in series]
    px = {d: v for d, v in series}
    idx = {d: i for i, d in enumerate(dates)}
    print(f"{len(dates)} daily observations, {dates[0]} to {dates[-1]}")
    print(f"latest close ${series[-1][1]:.2f}\n")

    # daily returns for every trading day
    rets = {}
    for i in range(1, len(dates)):
        rets[dates[i]] = pct(px[dates[i-1]], px[dates[i]])
    allr = list(rets.values())

    print("=" * 72)
    print("STEP 1 — BASELINE: what a normal day looks like")
    print("=" * 72)
    absr = sorted(abs(x) for x in allr)
    print(f"  n = {len(allr)} days")
    print(f"  mean daily move   {st.mean(allr):+.2f}%")
    print(f"  stdev             {st.pstdev(allr):.2f}%")
    print(f"  median |move|     {absr[len(absr)//2]:.2f}%")
    print(f"  90th pct |move|   {absr[int(len(absr)*0.90)]:.2f}%")
    print(f"  99th pct |move|   {absr[int(len(absr)*0.99)]:.2f}%")
    print(f"  largest |move|    {absr[-1]:.2f}%")
    print("\n  Any event effect must clear this to be interesting.")

    # resolve each event to the next available trading day
    def nearest(d):
        dt = datetime.fromisoformat(d)
        for k in range(0, 6):
            cand = (dt + timedelta(days=k)).strftime("%Y-%m-%d")
            if cand in rets:
                return cand
        return None

    rows = []
    for d, cls, label in events:
        td = nearest(d)
        if not td:
            print(f"  ! no trading day near {d} ({label})")
            continue
        i = idx[td]
        nxt = pct(px[dates[i]], px[dates[i+1]]) if i + 1 < len(dates) else None
        w1 = pct(px[dates[i]], px[dates[min(i+5, len(dates)-1)]])
        rows.append({"date": td, "cls": cls, "label": label,
                     "move": rets[td], "next": nxt, "wk": w1})

    print("\n" + "=" * 72)
    print("STEP 2 — EVENT DAYS")
    print("=" * 72)
    print(f"{'date':<12}{'cls':<5}{'day%':>8}{'next%':>8}{'+5d%':>8}  headline")
    for r in sorted(rows, key=lambda r: r["date"]):
        nx = f"{r['next']:+.2f}" if r["next"] is not None else "   -"
        print(f"{r['date']:<12}{r['cls']:<5}{r['move']:>+8.2f}{nx:>8}"
              f"{r['wk']:>+8.2f}  {r['label'][:38]}")

    D = [r["move"] for r in rows if r["cls"] == "D"]
    E = [r["move"] for r in rows if r["cls"] == "E"]
    if not D or not E:
        print("\nneed both classes to compare")
        return

    sep = st.mean(E) - st.mean(D)
    print(f"\n  diplomacy  n={len(D):<3} mean {st.mean(D):+.2f}%  "
          f"median {st.median(D):+.2f}%  down {sum(1 for x in D if x<0)}/{len(D)}")
    print(f"  escalation n={len(E):<3} mean {st.mean(E):+.2f}%  "
          f"median {st.median(E):+.2f}%  up   {sum(1 for x in E if x>0)}/{len(E)}")
    print(f"  separation (E - D) = {sep:+.2f} percentage points")

    print("\n" + "=" * 72)
    print(f"STEP 3 — PERMUTATION TEST ({a.trials} shuffles)")
    print("=" * 72)
    pool = allr
    hits = 0
    for _ in range(a.trials):
        s = random.sample(pool, len(D) + len(E))
        fd, fe = s[:len(D)], s[len(D):]
        if abs(st.mean(fe) - st.mean(fd)) >= abs(sep):
            hits += 1
    p = hits / a.trials
    print(f"  random day-sets matched or beat the real separation "
          f"{hits}/{a.trials} times")
    print(f"  p = {p:.3f}")
    print("  " + ("PASS — separation is unlikely to be chance."
                  if p < 0.05 else
                  "FAIL — random days do this too. The story is not the signal."))

    print("\n" + "=" * 72)
    print("STEP 4 — THE TRADEABILITY QUESTION")
    print("=" * 72)
    nd = [r["next"] for r in rows if r["cls"] == "D" and r["next"] is not None]
    ne = [r["next"] for r in rows if r["cls"] == "E" and r["next"] is not None]
    if nd and ne:
        print(f"  NEXT day after diplomacy : mean {st.mean(nd):+.2f}%  "
              f"({sum(1 for x in nd if x<0)}/{len(nd)} continued down)")
        print(f"  NEXT day after escalation: mean {st.mean(ne):+.2f}%  "
              f"({sum(1 for x in ne if x>0)}/{len(ne)} continued up)")
        print(f"  next-day separation = {st.mean(ne)-st.mean(nd):+.2f}pp "
              f"vs {sep:+.2f}pp on the day itself")
    print("\n  This is the number that decides everything. The event-day move")
    print("  happens on the headline, before you can act. Only the NEXT-day")
    print("  drift is available to you. If it is near zero, the pattern is")
    print("  real and untradeable — which is a finding, not a failure.")

    print("\n" + "=" * 72)
    print("KILL CRITERIA — decide BEFORE looking again")
    print("=" * 72)
    print("  Dead if: permutation p >= 0.05")
    print("  Dead if: next-day separation < 1.0pp (below round-trip costs)")
    print("  Dead if: direction accuracy on next-day < 60%")
    print("  Dead if: the result rests on fewer than 5 events per class")
    print(f"\n  Current event count: D={len(D)}, E={len(E)}")


if __name__ == "__main__":
    main()

"""
oil_nextday_test.py — test the ONLY number that is tradeable.

The event-day move happens on the headline. You cannot act on it.
What you can act on is the NEXT day. This tests that, and only that.

    python3 oil_nextday_test.py --events iran_events.csv
"""
import argparse, csv, os, random, statistics as st
from datetime import datetime, timedelta
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv(Path.home()/"trading-lab"/".env")
FRED_KEY = os.getenv("FRED_KEY")


def fred(sid, start="2025-06-01"):
    r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                     params={"series_id": sid, "api_key": FRED_KEY,
                             "file_type": "json", "observation_start": start},
                     timeout=30)
    r.raise_for_status()
    return [(o["date"], float(o["value"]))
            for o in r.json()["observations"] if o["value"] not in (".", "")]


ap = argparse.ArgumentParser()
ap.add_argument("--events", required=True)
ap.add_argument("--trials", type=int, default=10000)
ap.add_argument("--cost", type=float, default=0.5,
                help="round-trip cost in percent")
a = ap.parse_args()

series = fred("DCOILWTICO")
dates = [d for d, _ in series]
px = dict(series)
idx = {d: i for i, d in enumerate(dates)}
rets = {dates[i]: (px[dates[i]] - px[dates[i-1]]) / px[dates[i-1]] * 100
        for i in range(1, len(dates))}

with open(a.events) as f:
    events = [(r[0], r[1].upper(), r[2]) for r in csv.reader(f) if r]


def nearest(d):
    dt = datetime.fromisoformat(d)
    for k in range(6):
        c = (dt + timedelta(days=k)).strftime("%Y-%m-%d")
        if c in rets:
            return c
    return None


D, E, rows = [], [], []
for d, cls, label in events:
    td = nearest(d)
    if not td:
        continue
    i = idx[td]
    if i + 1 >= len(dates):
        continue
    nxt = rets[dates[i+1]]
    rows.append((td, cls, rets[td], nxt, label))
    (D if cls == "D" else E).append(nxt)

print("=" * 70)
print("NEXT-DAY MOVES ONLY — the part you can actually trade")
print("=" * 70)
print(f"{'event date':<13}{'cls':<5}{'day%':>8}{'NEXT day%':>11}  headline")
for td, cls, day, nxt, label in rows:
    print(f"{td:<13}{cls:<5}{day:>+8.2f}{nxt:>+11.2f}  {label[:34]}")

sep = st.mean(E) - st.mean(D)
print(f"\n  after diplomacy : mean {st.mean(D):+.2f}%  "
      f"{sum(1 for x in D if x < 0)}/{len(D)} fell")
print(f"  after escalation: mean {st.mean(E):+.2f}%  "
      f"{sum(1 for x in E if x > 0)}/{len(E)} rose")
print(f"  separation = {sep:+.2f} percentage points")

acc = (sum(1 for x in D if x < 0) + sum(1 for x in E if x > 0)) / len(rows) * 100
print(f"  direction correct on {acc:.0f}% of events")

pool = list(rets.values())
hits = 0
for _ in range(a.trials):
    s = random.sample(pool, len(D) + len(E))
    fake = st.mean(s[len(D):]) - st.mean(s[:len(D)])
    if abs(fake) >= abs(sep):
        hits += 1
p = hits / a.trials

print("\n" + "=" * 70)
print(f"PERMUTATION TEST ON NEXT-DAY MOVES ({a.trials:,} shuffles)")
print("=" * 70)
print(f"  random days matched or beat it {hits:,}/{a.trials:,} times")
print(f"  p = {p:.4f}")

print("\n" + "=" * 70)
print("VERDICT")
print("=" * 70)
fails = []
if p >= 0.05:
    fails.append(f"permutation p = {p:.4f}, needed < 0.05")
if abs(sep) < 1.0:
    fails.append(f"separation {abs(sep):.2f}pp, needed > 1.0pp")
if acc < 60:
    fails.append(f"direction accuracy {acc:.0f}%, needed >= 60%")
if min(len(D), len(E)) < 5:
    fails.append(f"only {min(len(D), len(E))} events in smallest class, needed 5")

if fails:
    print("  DEAD. Failed:")
    for x in fails:
        print(f"    - {x}")
    print("\n  The event-day effect is real. The next-day drift is not")
    print("  separable from noise. Real pattern, no tradeable residue.")
else:
    print("  SURVIVES all four criteria.")
    net = abs(sep) - a.cost
    print(f"  edge {abs(sep):.2f}pp - costs {a.cost:.2f}pp = {net:+.2f}pp net")
    print("\n  NOT a green light. Still untested: whether you can identify")
    print("  the event class in real time, and whether it holds out of sample.")

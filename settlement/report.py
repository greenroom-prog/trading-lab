"""
report.py — did the model beat the MARKET, judged fairly?

WHY THE OBVIOUS SCORING IS WRONG
  v1 asked "did the flagged strike settle the way the model implied?" and
  reported 4 of 6. That punishes the model for the price moving. On
  2026-09-01 BTC sat near 78,150 five minutes out and settled at 77,902.
  A model saying "88% above 78,099" at T-300s was not wrong — it was
  right given what was knowable then.

THE FAIR TEST
  At every instant the model and the market both state a probability with
  the SAME information. Score both against what happened (Brier score,
  lower is better). Whoever is closer, over thousands of paired
  observations, is the better forecaster. That is the only comparison
  that means anything.

    python3 report.py                      # newest log
    python3 report.py data/live_edge_*.csv  # combine sessions
"""
import csv, glob, math, statistics as st, sys
from collections import defaultdict
from pathlib import Path

paths = sys.argv[1:] or [max(
    glob.glob(str(Path(__file__).parent / "data" / "live_edge_*.csv")),
    key=lambda p: Path(p).stat().st_mtime)]

rows = []
for p in paths:
    rows += list(csv.DictReader(open(p)))
if not rows:
    sys.exit("no rows")


def f(r, k):
    try:
        return float(r[k])
    except (ValueError, KeyError, TypeError):
        return None


# group by settlement event so multiple sessions can be combined
events = defaultdict(list)
for r in rows:
    events[r.get("event", "?")].append(r)

print(f"{len(rows):,} rows across {len(events)} settlement(s)\n")
_srcs = set()

tot_m, tot_k, n_pairs = [], [], 0
per_event = []

for ev, rs in events.items():
    # THE SETTLED VALUE MUST NOT COME FROM THE MODEL.
    # v2 used the model's own final `centre`, so the model was partly
    # graded against its own estimate — a circularity that flatters it.
    # The contract settles on the 60-second average of the index, which
    # the exchange broadcasts directly: use the last logged avg60s.
    last = min(rs, key=lambda r: f(r, "secs_left") or 9e9)
    settle = f(last, "avg60s")
    src = "avg60s (exchange)"
    if settle is None:
        settle = f(last, "centre")
        src = "model centre — CIRCULAR, treat with suspicion"
    t_end = f(last, "secs_left")
    if settle is None:
        continue

    mb, kb = [], []
    for r in rs:
        label = r.get("label", "")
        if not label.startswith(">"):
            continue
        try:
            strike = float(label[1:].replace(",", ""))
        except ValueError:
            continue
        m = f(r, "model_pc")
        bid, ask = f(r, "bid"), f(r, "ask")
        if m is None or bid is None or ask is None or ask <= 0:
            continue
        mkt = (bid + ask) / 2
        y = 1.0 if settle > strike else 0.0
        # ONLY strikes where the outcome was genuinely in doubt. A strike
        # $8,000 away is priced 0 or 100 by everyone and scoring it just
        # buries the real comparison under thousands of trivial wins.
        if not (2 <= mkt <= 98):
            continue
        mb.append((m / 100 - y) ** 2)
        kb.append((mkt / 100 - y) ** 2)
    if not mb:
        continue
    per_event.append((ev, settle, t_end, len(mb), st.mean(mb), st.mean(kb)))
    _srcs.add(src)
    tot_m += mb
    tot_k += kb
    n_pairs += len(mb)

print("Scoring ONLY strikes the market priced between 2c and 98c — the")
print("ones whose outcome was actually uncertain.\n")
print(f"{'event':<26}{'settled':>12}{'pairs':>8}{'model':>9}{'market':>9}"
      f"{'winner':>9}")
for ev, s, t, n, m, k in per_event:
    print(f"{ev[:25]:<26}{s:>12,.2f}{n:>8,}{m:>9.4f}{k:>9.4f}"
          f"{('model' if m < k else 'market'):>9}")

if not tot_m:
    sys.exit("\nno scoreable rows")

print(f"\nsettlement value taken from: {', '.join(sorted(_srcs))}")
M, K = st.mean(tot_m), st.mean(tot_k)
print(f"\n{'='*72}")
print(f"BRIER SCORE over {n_pairs:,} paired observations   (lower is better)")
print(f"{'='*72}")
print(f"  model  {M:.4f}")
print(f"  market {K:.4f}")
diff = K - M
print(f"  difference {diff:+.4f}  ({'model' if diff > 0 else 'market'} better)")

# is the difference real, or could it be luck? paired bootstrap.
import random
random.seed(0)
d = [a - b for a, b in zip(tot_m, tot_k)]
obs = st.mean(d)
boots = []
for _ in range(2000):
    s_ = [random.choice(d) for _ in range(len(d))]
    boots.append(st.mean(s_))
lo, hi = sorted(boots)[50], sorted(boots)[-50]
print(f"  95% interval on the difference: {-hi:+.4f} to {-lo:+.4f}")
if lo < 0 < hi:
    print("\n  The interval spans zero. There is NO measurable difference")
    print("  between the model and the market. No edge demonstrated.")
elif hi < 0:
    print("\n  The model is measurably better than the market on this data.")
    print("  Necessary but not sufficient: check it survives costs, and")
    print("  that it holds across many more settlements.")
else:
    print("\n  The MARKET is measurably better than the model. There is no")
    print("  edge here, and trading this would lose money.")

# where in the window does the model do best?
print(f"\n{'='*72}")
print("BY TIME TO SETTLEMENT")
print(f"{'='*72}")
buckets = defaultdict(lambda: {"m": [], "k": []})
for ev, rs in events.items():
    last = min(rs, key=lambda r: f(r, "secs_left") or 9e9)
    settle = f(last, "avg60s") or f(last, "centre")
    if settle is None:
        continue
    for r in rs:
        label = r.get("label", "")
        if not label.startswith(">"):
            continue
        try:
            strike = float(label[1:].replace(",", ""))
        except ValueError:
            continue
        m, bid, ask, t = f(r, "model_pc"), f(r, "bid"), f(r, "ask"), f(r, "secs_left")
        if None in (m, bid, ask, t) or ask <= 0:
            continue
        mkt = (bid + ask) / 2
        if not (2 <= mkt <= 98):
            continue
        y = 1.0 if settle > strike else 0.0
        b = ("0-30s" if t <= 30 else "30-60s" if t <= 60 else
             "60-120s" if t <= 120 else "120-300s")
        buckets[b]["m"].append((m / 100 - y) ** 2)
        buckets[b]["k"].append((mkt / 100 - y) ** 2)

for b in ("0-30s", "30-60s", "60-120s", "120-300s"):
    v = buckets.get(b)
    if not v or not v["m"]:
        continue
    m, k = st.mean(v["m"]), st.mean(v["k"])
    print(f"  {b:<10}n={len(v['m']):>6,}  model {m:.4f}  market {k:.4f}  "
          f"{'model' if m < k else 'market':>6} better")

print("\nOne or two settlements cannot settle this. Collect 20+ before")
print("treating any of it as a finding.")

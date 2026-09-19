"""Engine tests — no network. Drives the same code path the live runner
uses, from captured ticks."""
import json, math, random, sys, pathlib, csv
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import settle_model as sm
import live_edge as le

FIX = pathlib.Path(__file__).parent / "fixtures"
# Absolute sandbox paths made these tests pass only on the machine that
# wrote them. Everything derives from the repo root now.
ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = ROOT / "data"
TMP.mkdir(parents=True, exist_ok=True)
fails = []
def check(l, ok, d=""):
    fails.append(not ok); print(f"  {'PASS' if ok else 'FAIL'}  {l}" + (f"   {d}" if d else ""))

legs = json.loads((FIX / "btc_ladder.json").read_text())["markets"]

print("1. centre() — outside the window, spot is the forecast")
e = le.Engine("KXBTCD")
e.legs = legs
e.on_index(79000, 78990, 60, 0)
check("T=300 -> spot", e.centre(300) == 79000)
check("T=61 -> spot", e.centre(61) == 79000)

print("\n2. centre() — inside the window, observed part is locked in")
e.on_index(79000, 78900, 30, 1)
c = e.centre(30)
want = (78900 * 30 + 79000 * 30) / 60
check("half-observed blends 50/50", abs(c - want) < 1e-9, f"{c:.1f} vs {want:.1f}")
e.on_index(79000, 78900, 60, 2)
check("fully observed -> the average itself", abs(e.centre(0) - 78900) < 1e-9)
e.on_index(79000, 78900, 0, 3)
check("window not started -> spot", e.centre(60) == 79000)

print("\n3. Engine refuses to price until volatility is measured")
e2 = le.Engine("KXBTCD"); e2.legs = legs
for i in range(5):
    e2.on_index(79000 + i, 79000, 0, i)
check("no price on 5 ticks", e2.price(300) is None)
random.seed(8)
q = 79000.0
for i in range(5, 50):
    q += random.gauss(0, 3)
    e2.on_index(q, q, 0, i)
check("still no price at 50 ticks", e2.price(300) is None)
for i in range(50, 220):
    q += random.gauss(0, 3)
    e2.on_index(q, q, 0, i)
r = e2.price(200)
check("prices once warmed up and settled",
      r is not None and not r.get("abstain"), (r or {}).get("abstain", ""))

print("\n4. full replay path on REAL captured ticks, 400 cycles")
random.seed(3)
ticks = json.loads((FIX / "brti_ticks.json").read_text())
base = [float(json.loads(m["msg"]["data"])["value"]) for m in ticks]
sigma_real = 3.4
e3 = le.Engine("KXBTCD", out=TMP / "test_replay.csv")
e3.legs = legs; e3.event = "TEST"
if e3.out.exists(): e3.out.unlink()
p = base[-1]
priced = 0
left = 400
for i in range(400):
    p += random.gauss(0, sigma_real)
    win = min(max(60 - left, 0), 60)
    e3.on_index(p, p - 2, win, i)
    r = e3.price(left)
    if r and r.get("rows"):
        e3.log(r); priced += 1
    left -= 1
# the gate abstains beyond MAX_TRUSTED_HORIZON, so only the final window
# is priced. That is the whole point of the change.
# two gates now bite: ~90 ticks of warmup, then the 5-minute horizon.
check("prices only after warmup AND inside the trusted horizon",
      150 < priced <= sm.MAX_TRUSTED_HORIZON + 5,
      f"{priced} priced of 400 cycles")
rows = list(csv.DictReader(e3.out.open()))
check("one CSV row per strike per priced cycle",
      len(rows) == priced * len(legs), f"{len(rows)} rows")
check("all header fields present", set(rows[0]) == set(le.HDR))

print("\n5. sd must SHRINK monotonically as settlement approaches")
sds = [float(r["sd_settle"]) for r in rows if r["ticker"] == legs[0]["ticker"]]
drops = sum(1 for a, b in zip(sds, sds[1:]) if b <= a + 1e-9)
# sigma is re-estimated each tick, so single steps can rise; what must
# hold is the trend and the endpoint.
check("sd falls sharply over the session", sds[-1] < sds[0] * 0.15,
      f"{sds[0]:.1f} -> {sds[-1]:.2f}")
# sd = sigma * sqrt(T). Per step sqrt(T) decays by 1/(2T), which is tiny
# when T is large, so sigma's own estimation noise can push sd up early.
# Near settlement the decay dominates and sd must fall every step. That
# is the property worth asserting, not a blanket percentage.
secs = [float(r["secs_left"]) for r in rows if r["ticker"] == legs[0]["ticker"]]
late = [(a, b) for (t0, a), (_, b) in
        zip(zip(secs, sds), zip(secs[1:], sds[1:])) if t0 <= 60]
check("sd strictly falls inside the final 60s",
      all(b <= a + 1e-9 for a, b in late), f"{len(late)} late steps")
check("no sd increases by more than 5% in one step",
      all(b <= a * 1.05 + 1e-9 for a, b in zip(sds, sds[1:])))
check("sd never negative or absurd", all(0 <= x < 1e5 for x in sds))

print("\n6. model stays coherent every single cycle")
bad = 0
for i in range(0, len(rows), len(legs)):
    chunk = rows[i:i + len(legs)]
    probs = [float(c["model_pc"]) for c in chunk]
    if not all(a >= b - 1e-6 for a, b in zip(probs, probs[1:])):
        bad += 1
check("probabilities monotonic in every cycle", bad == 0, f"{bad} bad cycles")

print("\n7. no fabricated edges when the model matches the market")
e4 = le.Engine("KXBTCD", out=TMP / "t2.csv")
e4.legs = legs
for i in range(200):
    e4.on_index(78150 + random.gauss(0, 3), 78150, 0, i)
r = e4.price(300)
flagged = [x for x in r["rows"] if x["flag"]]
check("centre near the market implies few large edges",
      all(abs(x["net"]) < 60 for x in r["rows"] if x["net"] is not None),
      f"{len(flagged)} flagged, max {max((abs(x['net']) for x in r['rows'] if x['net'] is not None), default=0):.1f}c")

print("\n8. mis-set centre is caught by the detector")
e5 = le.Engine("KXBTCD", out=TMP / "t3.csv")
e5.legs = legs
for i in range(200):
    e5.on_index(78500 + random.gauss(0, 3), 78500, 0, i)
r5 = e5.price(300)
check("far-off centre produces flagged strikes",
      any(x["flag"] for x in r5["rows"]))

print("\n" + ("ALL TESTS PASS" if not any(fails) else f"{sum(fails)} FAILURES"))
sys.exit(1 if any(fails) else 0)

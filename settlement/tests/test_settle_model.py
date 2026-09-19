"""Tests for settle_model. Fixtures are REAL payloads captured from the
live stream and the live REST API during this session."""
import json, math, random, statistics as st, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import settle_model as sm

FIX = pathlib.Path(__file__).parent / "fixtures"
fails = []


def check(label, ok, detail=""):
    fails.append(not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"   {detail}" if detail else ""))


print("1. TickStats.sigma_1s recovers a KNOWN volatility")
random.seed(1)
TRUE = 3.0                      # dollars per second
ts = sm.TickStats()
p = 79000.0
for i in range(400):
    p += random.gauss(0, TRUE)
    ts.add(p, i)
got = ts.sigma_1s()
check("recovered sigma within 10%", abs(got - TRUE) / TRUE < 0.10,
      f"true {TRUE:.2f} got {got:.2f}")

print("\n2. refuses to guess on thin data")
thin = sm.TickStats()
for i in range(5):
    thin.add(79000 + i, i)
check("sigma is None under the warmup threshold", thin.sigma_1s() is None)
mid = sm.TickStats()
random.seed(4)
q = 79000.0
for i in range(40):
    q += random.gauss(0, 3)
    mid.add(q, i)
check("40 ticks still not enough", mid.sigma_1s() is None,
      f"needs {mid.warmup_left()} more")
check("sigma_settled says why", sm.sigma_settled(mid)[0] is False,
      sm.sigma_settled(mid)[1])
for i in range(40, 200):
    q += random.gauss(0, 3)
    mid.add(q, i)
check("200 ticks -> trusted", sm.sigma_settled(mid)[0] is True)
check("sd_of_settlement passes the None through",
      sm.sd_of_settlement(None, 300) is None)

print("\n3. sd of the 60s AVERAGE is smaller than sd of spot")
sig = 3.0
sd_spot = sig * math.sqrt(300)
sd_avg = sm.sd_of_settlement(sig, 300)
check("averaging shrinks uncertainty", sd_avg < sd_spot,
      f"spot {sd_spot:.1f} vs settle {sd_avg:.1f}")
check("at T=60 only the averaging term remains (raw)",
      abs(sm.sd_raw(sig, 60) - sig * math.sqrt(20)) < 1e-9)
check("sd shrinks as the window fills",
      sm.sd_of_settlement(sig, 30, observed_in_window=30)
      < sm.sd_of_settlement(sig, 60))
check("sd -> ~0 at settlement",
      sm.sd_of_settlement(sig, 0, observed_in_window=60) < 1e-3)

print("\n4. MONTE CARLO — does sd_of_settlement match simulated reality?")
random.seed(7)
for T in (600, 300, 120):
    finals = []
    for _ in range(3000):
        x = 79000.0
        for _ in range(T - 60):
            x += random.gauss(0, sig)
        win = []
        for _ in range(60):
            x += random.gauss(0, sig)
            win.append(x)
        finals.append(st.mean(win))
    emp = st.pstdev(finals)
    pred = sm.sd_raw(sig, T)          # random-walk maths, uncorrected
    check(f"T={T}s RAW sd matches random-walk simulation",
          abs(emp - pred) / emp < 0.06, f"sim {emp:.1f} vs raw {pred:.1f}")

print("\n4b. the MEASURED correction — BTC is not a random walk")
print("    (ratios from 590 live BRTI samples, 2026-08-31)")
for T, want in sm._SCALE_POINTS:
    got = sm.horizon_scale(T)
    check(f"scale at {T}s", abs(got - want) < 1e-9, f"{got:.2f}")
check("correction shrinks sd at long horizons",
      sm.sd_of_settlement(sig, 120) < sm.sd_raw(sig, 120),
      f"{sm.sd_of_settlement(sig,120):.1f} vs raw {sm.sd_raw(sig,120):.1f}")
check("sd never rises as settlement approaches",
      all(sm.sd_of_settlement(sig, b, observed_in_window=max(60-b,0))
          <= sm.sd_of_settlement(sig, a, observed_in_window=max(60-a,0)) + 1e-9
          for a, b in zip([300,240,180,120,60,30,10],
                          [240,180,120,60,30,10,1])))

print("\n4c. the model ABSTAINS where it was never measured")
for secs, sd_, expect in ((2700, 50, False), (600, 30, False),
                          (300, 20, True), (60, 8, True), (0, 5, False)):
    ok, why = sm.tradeable_now(secs, sd_, 79000)
    check(f"T={secs}s -> {'trade' if expect else 'abstain'}", ok == expect, why)
check("no volatility measured -> abstain",
      sm.tradeable_now(120, None, 79000)[0] is False)

print("\n5. probabilities behave")
check("P(>strike) at the centre is 0.5", abs(sm.prob_above(79000, 79000, 50) - 0.5) < 1e-9)
ps = [sm.prob_above(k, 79000, 50) for k in (78800, 78900, 79000, 79100, 79200)]
check("monotone decreasing in strike", all(a >= b for a, b in zip(ps, ps[1:])),
      " ".join(f"{x:.2f}" for x in ps))
check("far above -> ~0", sm.prob_above(80000, 79000, 50) < 1e-4)
check("far below -> ~1", sm.prob_above(78000, 79000, 50) > 0.9999)

print("\n6. fees follow Kalshi's published formula")
check("50c costs more than 99c", sm.fee_cents(50) > sm.fee_cents(99))
check("fee rounds up to the cent", sm.fee_cents(50) == 0.02)

print("\n7. REAL LADDER — the BTC ladder captured from the live API")
lad = json.loads((FIX / "btc_ladder.json").read_text())["markets"]
# real market implies settlement was very near 78,150 (T78099 at 98.5c,
# T78199 at 32.5c, T78299 at 1.5c)
rows = sm.price_ladder(lad, centre=78150.0, sd=60.0)
print(sm.render(rows, 78150.0, 60.0, 45, 8.5))
co = sm.coherence(rows)
check("model probabilities are monotonic", co["model_monotonic"])
check("market quotes are monotonic", co["market_monotonic"])
check("every strike priced", len(rows) == len(lad), f"{len(rows)}/{len(lad)}")
flagged = [r for r in rows if r["flag"]]
check("edges are modest when the model agrees with the market",
      all(abs(r["net"]) < 40 for r in rows if r["net"] is not None),
      f"{len(flagged)} flagged")

print("\n8. a DELIBERATELY wrong centre must produce big edges (detector works)")
rows_bad = sm.price_ladder(lad, centre=78400.0, sd=60.0)
big = [r for r in rows_bad if r["net"] is not None and r["net"] > 20]
check("wrong centre produces large flagged edges", len(big) >= 1,
      f"{len(big)} strikes over 20c")

print("\n9. size floor blocks unfillable strikes")
rows_sz = sm.price_ladder(lad, centre=78150.0, sd=60.0, size_floor=1e9)
check("nothing tradeable when no size clears the floor",
      all(r["net"] is None for r in rows_sz))

print("\n10. REAL BRTI TICKS — end-to-end from the captured stream")
ticks = json.loads((FIX / "brti_ticks.json").read_text())
ts2 = sm.TickStats()
for i, m in enumerate(ticks):
    v = float(json.loads(m["msg"]["data"])["value"])
    ts2.add(v, i * 5)          # captured at ~5s spacing
check("12 captured ticks is correctly NOT enough to price",
      ts2.sigma_1s() is None, f"needs {ts2.warmup_left()} more")

# extend the real series with matched-volatility ticks so the full path
# can be exercised; the first 12 values are real, the rest are simulated
# at the volatility those real ticks imply.
real = [float(json.loads(m["msg"]["data"])["value"]) for m in ticks]
step = st.pstdev([b - a for a, b in zip(real, real[1:])]) / math.sqrt(5)
random.seed(21)
p2 = real[-1]
for i in range(len(ticks), 260):
    p2 += random.gauss(0, step * math.sqrt(5))
    ts2.add(p2, i * 5)
sig2 = ts2.sigma_1s()
check("sigma available once warmed up", sig2 is not None,
      f"{sig2:.3f} $/s^0.5, seeded from real ticks" if sig2 else "")
stable, why = sm.sigma_settled(ts2)
check("estimate reads as settled", stable, why)
last_avg = float(ticks[-1]["msg"]["avg_60s_data"]["value"])
sd2 = sm.sd_of_settlement(sig2, 300)
check("sd is finite and sane", sd2 is not None and 0 < sd2 < 5000,
      f"sd {sd2:,.1f} around avg {last_avg:,.2f}" if sd2 else "")

print("\n" + ("ALL TESTS PASS" if not any(fails) else f"{sum(fails)} FAILURES"))
sys.exit(1 if any(fails) else 0)

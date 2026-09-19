import sys, math, json, random, pathlib, statistics as st
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import final_window as fw, settle_model as sm
fails=[]
def check(l, ok, d=""):
    fails.append(not ok); print(f"  {'PASS' if ok else 'FAIL'}  {l}"+(f"   {d}" if d else ""))

print("1. NOTHING is locked before the window opens (the v1 bug)")
for T in (300, 120, 84, 61):
    c,b = fw.implied_settlement(avg60=78000, obs=60, spot=78100, sigma=4,
                                secs_left=T)
    check(f"T-{T}s: estimate is SPOT, band is wide",
          abs(c-78100)<1e-9 and b>1.0, f"centre {c:.0f} band {b:.2f}")
print("   v1 reported band +/-0.00 at T-84s and flagged trades on it.")

print("\n2. inside the window, locked fraction grows with time")
prev=None
for T in (55,45,30,15,5,0):
    c,b = fw.implied_settlement(78000, 60, 78100, 4, secs_left=T)
    locked = 60-T
    check(f"T-{T}s: {locked}s locked, band {b:.3f}",
          prev is None or b<=prev+1e-9, f"centre {c:.2f}")
    prev=b
check("band -> 0 at settlement", prev==0.0)

print("\n3. MONTE CARLO — is the band honest?")
random.seed(5)
sig=4.0
for obs in (20,40,55):
    rem=60-obs
    finals=[]
    for _ in range(4000):
        x=78000.0; acc=[78000.0]*obs
        for _ in range(rem):
            x+=random.gauss(0,sig); acc.append(x)
        finals.append(st.mean(acc))
    emp=st.pstdev(finals)
    pred=fw.implied_settlement(78000,60,78000,sig,secs_left=60-obs)[1]
    check(f"obs={obs}s band matches simulation", abs(emp-pred)/max(emp,1e-9)<0.10,
          f"sim {emp:.3f} vs model {pred:.3f}")

print("\n3b. REGRESSION — the blend that manufactured $34,000")
c,b = fw.implied_settlement(77412.0545, 60, 77402.54, 4.0, secs_left=0.9)
check("centre stays inside its inputs", 77402.54 <= c <= 77412.06,
      f"{c:,.2f}  (the bug gave 77,540.92)")
check("band is tiny with 59s locked", b < 0.2, f"{b:.3f}")
random.seed(11); bad = 0
for _ in range(5000):
    a = random.uniform(70000, 80000)
    sp = a + random.uniform(-500, 500)
    T = random.uniform(0, 60)
    try:
        cc, _ = fw.implied_settlement(a, 60, sp, random.uniform(0.5, 20),
                                      secs_left=T)
    except AssertionError:
        bad += 1
        continue
    if not (min(a, sp) - 1e-6 <= cc <= max(a, sp) + 1e-6):
        bad += 1
check("5,000 fuzz cases stay inside their inputs", bad == 0, f"{bad} violations")

print("\n4. decided_strikes only fires when the outcome is locked")
legs=[{"ticker":"T77900","strike_type":"greater","floor_strike":"77900",
       "yes_bid_dollars":"0.90","yes_ask_dollars":"0.92",
       "yes_bid_size_fp":"500","yes_ask_size_fp":"400"},
      {"ticker":"T78100","strike_type":"greater","floor_strike":"78100",
       "yes_bid_dollars":"0.40","yes_ask_dollars":"0.42",
       "yes_bid_size_fp":"300","yes_ask_size_fp":"300"},
      {"ticker":"T78300","strike_type":"greater","floor_strike":"78300",
       "yes_bid_dollars":"0.05","yes_ask_dollars":"0.07",
       "yes_bid_size_fp":"600","yes_ask_size_fp":"600"}]
# settle 78,000 with a tiny band: 77,900 is decided YES, 78,300 decided NO,
# 78,100 is only $100 away
hits=fw.decided_strikes(legs, centre=78000, band=5.0, z=4.0)
names={h["ticker"]:h for h in hits}
check("77900 decided YES", "T77900" in names and names["T77900"]["side"]=="YES")
check("78300 decided NO", "T78300" in names and names["T78300"]["side"]=="NO")
check("78100 NOT flagged (only 20 sigma? no - 100/5=20, it IS decided)",
      "T78100" in names)

print("\n5. a WIDE band must decide nothing")
hits2=fw.decided_strikes(legs, centre=78000, band=200.0, z=4.0)
check("wide band -> no decided strikes", len(hits2)==0, f"{len(hits2)} flagged")

print("\n6. economics are computed correctly")
h=names["T77900"]
check("buys YES at the ask", h["cost_c"]==92.0)
check("net = 100 - cost - fee", abs(h["net_c"]-(100-92-sm.fee_cents(92)))<1e-9,
      f"net {h['net_c']:.2f}c")
check("max profit = net x size", abs(h["max_profit"]-h["net_c"]*400/100)<1e-9,
      f"${h['max_profit']:.2f} on {h['max_contracts']:.0f}")
hn=names["T78300"]
check("NO side costs 100-bid", hn["cost_c"]==95.0, f"{hn['cost_c']}c")

print("\n7. refuses strikes with no size behind them")
nosize=[dict(legs[0], yes_ask_size_fp="0")]
check("zero ask size -> skipped", len(fw.decided_strikes(nosize,78000,5.0))==0)

print("\n8. refuses when there is no profit left")
rich=[dict(legs[0], yes_ask_dollars="1.00")]
check("ask at 100c -> skipped", len(fw.decided_strikes(rich,78000,5.0))==0)

print("\n" + ("ALL TESTS PASS" if not any(fails) else f"{sum(fails)} FAILURES"))
sys.exit(1 if any(fails) else 0)

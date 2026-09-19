"""
Test suite. Every load-bearing number is derived twice, by different
routes. A test that re-runs the implementation is not a test.

Run:  python3 test_carry.py
Exit: 0 all pass, 1 any fail.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import economics as ec
import ruin
import book

FAIL = []


def check(name: str, a, b, tol=1e-6):
    ok = abs(a - b) <= tol
    print(f"  {'PASS' if ok else 'FAIL'}  {name}   {a!r} vs {b!r}")
    if not ok:
        FAIL.append(name)


print("=" * 68)
print("1. gross_return: formula vs. explicit cashflow arithmetic")
print("=" * 68)
for p in (0.982, 0.793, 0.426, 0.55):
    shares = 1_000_000.0
    cost = shares * p                      # path B: money in
    profit = shares * 1.0 - cost           # path B: money out
    check(f"p={p}", ec.gross_return(p), profit / cost)

print()
print("=" * 68)
print("2. loss_to_gain: formula vs. count of wins one loss erases")
print("=" * 68)
for p in (0.982, 0.90, 0.70):
    r = ec.gross_return(p)
    wins_needed = 1.0 / r                  # path B: lose 1 unit, win r each
    check(f"p={p}", ec.loss_to_gain(p), wins_needed, tol=1e-9)

print()
print("=" * 68)
print("3. kelly_fraction: closed form vs. numeric argmax of E[log wealth]")
print("=" * 68)
for p, q in ((0.982, 0.9850), (0.982, 0.9900), (0.70, 0.80), (0.50, 0.52)):
    b = ec.gross_return(p)
    best_f, best_v = 0.0, -1e18
    f = 0.0
    while f < 0.999:                        # path B: brute-force the max
        v = q * math.log(1 + f * b) + (1 - q) * math.log(1 - f)
        if v > best_v:
            best_v, best_f = v, f
        f += 0.00005
    check(f"p={p} q={q}", ec.kelly_fraction(p, q), best_f, tol=5e-4)

print()
print("=" * 68)
print("4. kelly sign flips exactly at q == p (the fragility claim)")
print("=" * 68)
check("f*(q=p) == 0", ec.kelly_fraction(0.982, 0.982), 0.0, tol=1e-12)
print(f"  f*(q=0.9850) = {ec.kelly_fraction(0.982,0.9850):+.4f}")
print(f"  f*(q=0.9820) = {ec.kelly_fraction(0.982,0.9820):+.4f}")
print(f"  f*(q=0.9780) = {ec.kelly_fraction(0.982,0.9780):+.4f}   <- 0.7pp error, sign flipped")

print()
print("=" * 68)
print("5. measurability: power calc vs. CI-width  (independent routes)")
print("=" * 68)
# Route A: two-proportion power calculation (two arms, (z_a+z_b)^2).
n_pow = ec.n_required(0.982, 0.975)
# Route B: sample size at which a ONE-arm 95% CI half-width equals the edge.
edge_pp = abs(ec.edge_pp(0.982, 0.975))
n_ci = 1
while ec.ci_halfwidth_pp(0.982, n_ci) > edge_pp:
    n_ci = int(n_ci * 1.02) + 1
# These answer different questions, so a raw ratio test would be junk.
# The ratio is analytically predictable; THAT is the cross-check.
za, zb = 1.959963985, 0.841621234
va, vb = 0.018 * 0.982, 0.025 * 0.975
predicted_ratio = ((za + zb) ** 2 * (va + vb)) / (za ** 2 * va)
observed_ratio = n_pow / n_ci
print(f"  power calc (2-arm)  n = {n_pow}")
print(f"  CI-width   (1-arm)  n = {n_ci}")
print(f"  ratio predicted analytically = {predicted_ratio:.3f}")
check("ratio matches analytic prediction", observed_ratio, predicted_ratio,
      tol=0.05)
print(f"  -> both routes say the measurable-edge threshold is O(10^3-10^4) "
      f"settlements.")

print()
print("=" * 68)
print("6. ruin: Monte Carlo vs. exact Binomial  (independent routes)")
print("=" * 68)
for p, q, f, cycles in ((0.982, 0.985, 0.167, 26),
                        (0.982, 0.985, 0.05, 26),
                        (0.95, 0.96, 0.20, 52)):
    res = ruin.assess(p, q, f, cycles, ruin_level=0.5, trials=100_000)
    print(f"  p={p} q={q} f={f} cycles={cycles}")
    print("   ", str(res).replace("\n", "\n    "))
    print(f"  {'PASS' if res.agree else 'FAIL'}  MC and DP agree within 0.5pp")
    if not res.agree:
        FAIL.append(f"ruin p={p} f={f}")

print()
print("=" * 68)
print("7. book: fill VWAP vs. hand-computed weighted average")
print("=" * 68)
levels = [(0.982, 50_000.0), (0.984, 120_000.0), (0.988, 400_000.0)]
fl = book.simulate_fill(levels, 200_000.0)
# path B: work out consumed shares per level by hand
s1 = 50_000.0                                  # 49_100 notional
s2 = 120_000.0                                 # 118_080 notional
spent12 = 0.982 * s1 + 0.984 * s2
s3 = (200_000.0 - spent12) / 0.988
manual_vwap = 200_000.0 / (s1 + s2 + s3)
check("vwap 200k", fl.vwap, manual_vwap, tol=1e-9)
print(f"  top of book 0.9820 -> realised VWAP {fl.vwap:.5f} "
      f"(slippage {fl.slippage_pp:.3f}pp)")

print()
print("=" * 68)
print("8. annualize: compounding vs. log-space")
print("=" * 68)
for r, d in ((0.018330, 42), (0.26103, 120), (1.34742, 200)):
    check(f"r={r} d={d}", ec.annualize(r, d),
          math.exp(math.log1p(r) * 365.0 / d) - 1.0, tol=1e-9)

print()
print("=" * 68)
print("9. observed fixture — the three visible positions")
print("=" * 68)
fx = json.loads((Path(__file__).parent / "fixtures" /
                 "observed_positions.json").read_text())
total_cost = 0.0
for pos in fx["positions"]:
    p = ec.Position(label=pos["label"], side=pos["side"], p=pos["price"],
                    shares=pos["shares"], fee_bps=0.0)
    rep = p.report()
    total_cost += rep["capital_committed"]
    print(f"  {rep['side']:>3} @ {rep['entry_price']:.3f}  "
          f"{rep['shares']:>12,.1f} sh  "
          f"cap ${rep['capital_committed']:>12,.0f}  "
          f"ret {rep['gross_return_pct']:>7.2f}%  "
          f"L:G {rep['loss_to_gain']:>6.2f}:1   {rep['label'][:34]}")
print(f"\n  cost basis of the 3 visible legs: ${total_cost:,.0f}")
print(f"  stated Positions Value:           ${fx['_profile_stats']['positions_value_usd']:,.0f}")
print(f"  unexplained:                      ${fx['_profile_stats']['positions_value_usd']-total_cost:,.0f}"
      "   (positions below the fold, and/or badge = mark not entry)")
print(f"  stated Biggest Win:               ${fx['_profile_stats']['biggest_win_usd']:,.0f}")
leg1 = fx["positions"][0]
leg1_profit = leg1["shares"] * (1 - leg1["price"])
print(f"  leg-1 profit if it resolves:      ${leg1_profit:,.0f}"
      f"   <- {leg1_profit/fx['_profile_stats']['biggest_win_usd']:.2f}x his biggest win EVER")

print()
print("=" * 68)
print("  simplex coherence of legs 1+2")
print("=" * 68)
p_cut = 1 - fx["positions"][0]["price"]
p_hike = fx["positions"][1]["price"]
p_hold = 1 - p_cut - p_hike
print(f"  P(cut)={p_cut:.3%}  P(hike)={p_hike:.3%}  P(hold)={p_hold:.3%}  "
      f"sum={p_cut+p_hike+p_hold:.4%}")

print()
print("=" * 68)
if FAIL:
    print(f"FAILED: {len(FAIL)} -> {FAIL}")
    sys.exit(1)
print("ALL PASS")
sys.exit(0)

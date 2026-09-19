#!/usr/bin/env python3
"""
One trade in, one verdict out.

    python3 verdict.py --price 0.982 --days 42 --capital 50000 --fee-bps 0
    python3 verdict.py --price 0.982 --days 42 --capital 3033269 --fee-bps 0 \
                       --true-prob 0.985 --cycles 26

Answers, in order:
    1. What does this pay, annualised, and what does one loss cost.
    2. What true probability would you need. Can you measure that? (gate)
    3. At your capital and your claimed edge, what size, and what is
       P(ruin) at that size.

The gate in step 2 is the one that matters and it is the one every
screenshot of a big carry book leaves out.
"""

from __future__ import annotations

import argparse

import economics as ec
import ruin as rn


def line(ch="-", n=70):
    print(ch * n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--price", type=float, required=True,
                    help="entry price of the side you buy, 0-1")
    ap.add_argument("--days", type=float, required=True)
    ap.add_argument("--capital", type=float, required=True,
                    help="your total bankroll, not the position size")
    ap.add_argument("--fee-bps", type=float, required=True,
                    help="taker fee in bps. Look it up. 0 is a valid "
                         "answer but must be stated, not defaulted.")
    ap.add_argument("--true-prob", type=float,
                    help="your estimate of P(resolves your way)")
    ap.add_argument("--cycles", type=int, default=26,
                    help="how many times you intend to run this per horizon")
    ap.add_argument("--settlements", type=int, default=20,
                    help="settlements you have actually observed")
    ap.add_argument("--ruin-level", type=float, default=0.5)
    a = ap.parse_args()

    p = ec.net_price(a.price, a.fee_bps)
    r = ec.gross_return(p)
    ann = ec.annualize(r, a.days)

    line("=")
    print("1. WHAT IT PAYS")
    line("=")
    print(f"  entry (after {a.fee_bps:.0f}bps fee) : {p:.5f}")
    print(f"  gross return per cycle     : {r*100:>8.3f}%")
    print(f"  annualised over {a.days:.0f}d cycles : {ann*100:>8.2f}%")
    print(f"  one loss erases            : {ec.loss_to_gain(p):>8.1f} wins")
    print(f"  breakeven true probability : {ec.breakeven_q(p)*100:>8.3f}%")

    line("=")
    print("2. CAN YOU MEASURE THE EDGE?  <-- the gate")
    line("=")
    for edge in (0.2, 0.5, 1.0):
        q_hi = min(p + edge / 100.0, 0.999999)
        n = ec.n_required(p, max(p - edge / 100.0, 1e-6))
        ratio = ec.measurement_to_edge_ratio(p, q_hi, a.settlements)
        verdict = "UNMEASURABLE" if ratio > 1 else "measurable"
        print(f"  edge {edge:>4.1f}pp -> needs {n:>8,} settlements. "
              f"At n={a.settlements} your CI is "
              f"{ratio:>6.1f}x wider than the edge.  {verdict}")
    print(f"\n  95% CI half-width on the loss rate at n={a.settlements}: "
          f"±{ec.ci_halfwidth_pp(p, a.settlements):.2f}pp")
    print(f"  The whole edge you are hunting is under 1pp wide.")
    print(f"  -> Any 'edge' you claim here is ASSERTED, not measured,")
    print(f"     unless it is structural (maker rebate, resolution")
    print(f"     mechanics, fee capture) rather than forecast-based.")

    if a.true_prob is None:
        line("=")
        print("3. SIZING — pass --true-prob to compute. Refusing to")
        print("   default it: assuming your own edge is how this ends.")
        line("=")
        return 0

    q = a.true_prob
    f_kelly = ec.kelly_fraction(p, q)

    line("=")
    print("3. SIZING AND RUIN")
    line("=")
    print(f"  your true-prob estimate    : {q*100:.4f}%")
    print(f"  edge over the quote        : {ec.edge_pp(p,q):+.4f}pp")
    print(f"  full Kelly fraction        : {f_kelly*100:+.2f}% of bankroll")

    if f_kelly <= 0:
        print("\n  VERDICT: negative EV at your own estimate. Size is zero.")
        return 0

    print("\n  FRAGILITY — what a small error in your estimate does:")
    for d in (-1.0, -0.7, -0.5, -0.3, 0.0, +0.3):
        qq = min(max(q + d / 100.0, 1e-6), 0.999999)
        ff = ec.kelly_fraction(p, qq)
        tag = "  <-- SIGN FLIP" if ff <= 0 < f_kelly else ""
        print(f"    q {d:+.1f}pp = {qq*100:7.4f}%  ->  f* = {ff*100:+7.2f}%{tag}")

    print("\n  RUIN at each sizing (ruin = bankroll below "
          f"{a.ruin_level:.0%} at any point in {a.cycles} cycles):")
    print(f"    {'fraction':>9} {'position$':>12} {'P(ruin)':>9} "
          f"{'p5 mult':>9} {'median':>8}")
    for mult, name in ((1.0, "full"), (0.5, "half"), (0.25, "quarter")):
        f = f_kelly * mult
        if f <= 0 or f >= 1:
            continue
        res = rn.assess(p, q, f, a.cycles, a.ruin_level, trials=60_000)
        print(f"    {name:>9} {a.capital*f:>12,.0f} "
              f"{res.p_ruin_ever_exact:>8.3%} "
              f"{res.p5_terminal_multiple:>9.3f} "
              f"{res.median_terminal_multiple:>8.3f}")

    safe = rn.max_safe_fraction(p, q, a.cycles, 0.01, a.ruin_level)
    print(f"\n  largest fraction with P(ruin) <= 1%: {safe*100:.2f}% "
          f"= ${a.capital*safe:,.0f}")
    print(f"  profit per cycle at that size      : "
          f"${a.capital*safe*r:,.0f}")
    print(f"  profit over {a.cycles} cycles (no loss)  : "
          f"${a.capital*safe*r*a.cycles:,.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

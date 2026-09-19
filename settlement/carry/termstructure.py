#!/usr/bin/env python3
"""
Term structure of near-certainty yield.

THE PIVOT THIS FILE EXISTS TO JUSTIFY
    Forecast edge at 98.2c needs ~12,605 settlements to measure.
    This needs ONE snapshot. That is the entire argument for changing
    angle. If the test below cannot kill the idea in one night, the
    pivot is worthless and this file should be deleted.

THE CLAIM
    Prediction venues quote probability in cents. They do not quote
    yield. A 98.2c contract resolving in 5 days and one resolving in 200
    days are visually identical and are 276.6% vs 3.4% annualised.
    If participants price in cents rather than in yield, annualised
    return will be systematically higher at short maturities.

THE NULL — this is what makes it a test and not a hope
    H0: within a price band, annualised yield has NO relationship to
        days-to-resolution. The market equalises yield; prices at short
        maturity are pushed toward 1.0 by exactly enough to flatten it.
    If H0 survives, the angle is dead. Say so and move on.

THE CONFOUND — the ugly candidate, checked explicitly
    Short-dated markets are near resolution, so their probabilities are
    genuinely more extreme. Price SHOULD converge to 1.0 as T -> 0. That
    is rational, not an inefficiency. So the test reports BOTH:
        (a) does price converge as T falls          [rational, expected]
        (b) is that convergence enough to flatten yield  [the real question]
    Only a "yes to (a), no to (b)" is a finding. Yes to both kills it.

TWO INDEPENDENT PATHS on the slope, because one is not a finding:
    Path A: OLS of annualised yield on log(days), bootstrap CI.
    Path B: Spearman rank correlation. Non-parametric, assumes no
            functional form, cannot inherit path A's linearity error.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict

import numpy as np
from scipy import stats

import economics as ec


# ----------------------------------------------------------------------

@dataclass
class Fit:
    n: int
    price_band: tuple[float, float]
    # Path A
    ols_slope: float
    ols_slope_ci: tuple[float, float]
    ols_r2: float
    # Path B
    spearman_rho: float
    spearman_p: float
    # the confound check
    price_vs_days_rho: float
    price_vs_days_p: float
    # summary
    median_yield_short: float
    median_yield_long: float
    verdict: str

    def show(self) -> str:
        lo, hi = self.price_band
        return "\n".join([
            f"price band {lo:.3f}-{hi:.3f}   n = {self.n}",
            f"  PATH A  OLS  yield ~ log(days)",
            f"          slope = {self.ols_slope:+.3f}  "
            f"95% CI [{self.ols_slope_ci[0]:+.3f}, {self.ols_slope_ci[1]:+.3f}]"
            f"   R2 = {self.ols_r2:.3f}",
            f"  PATH B  Spearman rho(yield, days) = {self.spearman_rho:+.3f}"
            f"   p = {self.spearman_p:.2e}",
            f"  CONFOUND  rho(price, days) = {self.price_vs_days_rho:+.3f}"
            f"   p = {self.price_vs_days_p:.2e}",
            f"            (negative = price converges to 1 as T falls."
            f" Rational. Expected.)",
            f"  median annualised yield   short half = "
            f"{self.median_yield_short*100:>8.1f}%",
            f"                             long half = "
            f"{self.median_yield_long*100:>8.1f}%",
            f"  VERDICT: {self.verdict}",
        ])


def annualised_yields(prices: np.ndarray, days: np.ndarray) -> np.ndarray:
    r = (1.0 - prices) / prices
    return (1.0 + r) ** (365.0 / days) - 1.0


def fit(prices, days, price_band=(0.92, 0.995), min_days=1.0,
        boot: int = 2000, seed: int = 11) -> Fit | None:
    """Run both paths plus the confound check on one snapshot."""
    prices = np.asarray(prices, float)
    days = np.asarray(days, float)

    m = ((prices >= price_band[0]) & (prices <= price_band[1])
         & (days >= min_days) & np.isfinite(prices) & np.isfinite(days))
    p, d = prices[m], days[m]
    if len(p) < 12:
        return None

    y = annualised_yields(p, d)
    x = np.log(d)

    # PATH A -- OLS with bootstrap CI on the slope
    lr = stats.linregress(x, y)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(x), size=(boot, len(x)))
    slopes = np.array([stats.linregress(x[i], y[i]).slope for i in idx])
    ci = (float(np.percentile(slopes, 2.5)), float(np.percentile(slopes, 97.5)))

    # PATH B -- rank correlation, no functional form assumed
    rho, pval = stats.spearmanr(d, y)

    # CONFOUND -- does price converge as T falls?
    crho, cp = stats.spearmanr(d, p)

    cut = np.median(d)
    short_y = float(np.median(y[d <= cut]))
    long_y = float(np.median(y[d > cut]))

    # verdict logic, stated before seeing any real data
    slope_neg = ci[1] < 0                      # CI excludes zero, negative
    rank_neg = (rho < 0) and (pval < 0.01)
    if slope_neg and rank_neg:
        v = ("ALIVE - yield is systematically higher at short maturity. "
             "Both paths agree. Next: is it capturable after depth+fees?")
    elif slope_neg != rank_neg:
        v = ("PATHS DISAGREE - do not act. One method is wrong or the "
             "relationship is non-monotone. Investigate before believing "
             "either.")
    else:
        v = ("DEAD - H0 survives. The market equalises yield across "
             "maturity. This angle is closed; do not spend more on it.")

    return Fit(
        n=int(len(p)), price_band=price_band,
        ols_slope=float(lr.slope), ols_slope_ci=ci,
        ols_r2=float(lr.rvalue ** 2),
        spearman_rho=float(rho), spearman_p=float(pval),
        price_vs_days_rho=float(crho), price_vs_days_p=float(cp),
        median_yield_short=short_y, median_yield_long=long_y,
        verdict=v,
    )


# ----------------------------------------------------------------------
# KNOWN-CASE VERIFICATION
# A method that cannot recover an answer it was handed is disqualified,
# however plausible it looks on real data.
# ----------------------------------------------------------------------

def known_cases(seed: int = 3) -> int:
    rng = np.random.default_rng(seed)
    n = 400
    days = rng.uniform(2, 300, n)
    fails = []

    print("=" * 70)
    print("KNOWN-CASE A: prices drawn INDEPENDENT of maturity")
    print("  (the 'everyone thinks in cents' world)")
    print("  method MUST report ALIVE")
    print("=" * 70)
    prices = np.clip(rng.normal(0.965, 0.012, n), 0.90, 0.999)
    f = fit(prices, days)
    print(f.show())
    if not f.verdict.startswith("ALIVE"):
        fails.append("A")

    print()
    print("=" * 70)
    print("KNOWN-CASE B: prices set so annualised yield is CONSTANT")
    print("  (the 'market prices yield' world)")
    print("  method MUST report DEAD")
    print("=" * 70)
    target = 0.25                                   # 25% annualised, flat
    r_needed = (1 + target) ** (days / 365.0) - 1.0
    prices_b = np.clip(1.0 / (1.0 + r_needed), 0.90, 0.999)
    prices_b = np.clip(prices_b + rng.normal(0, 0.0015, n), 0.90, 0.999)
    f = fit(prices_b, days)
    print(f.show())
    if not f.verdict.startswith("DEAD"):
        fails.append("B")

    print()
    print("=" * 70)
    print("KNOWN-CASE C: PARTIAL rational convergence")
    print("  price rises as T falls, but only halfway to flat yield.")
    print("  This is the realistic middle. Method MUST report ALIVE and")
    print("  the confound check MUST fire (rho(price,days) < 0).")
    print("=" * 70)
    prices_c = np.clip(0.5 * prices_b + 0.5 * 0.965
                       + rng.normal(0, 0.0015, n), 0.90, 0.999)
    f = fit(prices_c, days)
    print(f.show())
    if not f.verdict.startswith("ALIVE"):
        fails.append("C-verdict")
    if not (f.price_vs_days_rho < 0 and f.price_vs_days_p < 0.01):
        fails.append("C-confound")

    print()
    print("=" * 70)
    if fails:
        print(f"KNOWN-CASE FAILURES: {fails}")
        print("The method cannot recover answers it was handed.")
        print("DO NOT run it on real data.")
        return 1
    print("KNOWN CASES PASS - method recovers all three planted answers.")
    return 0


# ----------------------------------------------------------------------

def from_scan_json(path: str) -> tuple[np.ndarray, np.ndarray]:
    """Eat scan.py --json output."""
    rows = json.loads(open(path).read())
    p = np.array([r["vwap_at_size"] for r in rows], float)
    d = np.array([r["days_to_resolution"] for r in rows], float)
    return p, d


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--known-cases", action="store_true",
                    help="verify the method before trusting it")
    ap.add_argument("--scan-json", help="output of scan.py --json")
    ap.add_argument("--bands", type=float, nargs="*",
                    default=[0.90, 0.95, 0.975, 0.99, 0.999])
    a = ap.parse_args()

    if a.known_cases or not a.scan_json:
        rc = known_cases()
        if not a.scan_json:
            print("\nNo --scan-json supplied. Run:")
            print("  python3 scan.py --venue kalshi --band 0.85 0.999 "
                  "--capital 5000 --fee-bps <x> --json snap.json")
            print("  python3 termstructure.py --scan-json snap.json")
            return rc
        if rc:
            return rc

    p, d = from_scan_json(a.scan_json)
    print(f"\nloaded {len(p)} rows from {a.scan_json}\n")
    for lo, hi in zip(a.bands, a.bands[1:]):
        f = fit(p, d, (lo, hi))
        if f is None:
            print(f"price band {lo:.3f}-{hi:.3f}: too few rows, skipped")
            continue
        print(f.show())
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())

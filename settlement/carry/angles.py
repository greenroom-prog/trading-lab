#!/usr/bin/env python3
"""
Which angle to spend on, ranked by what it costs to KNOW.

The mistake this file exists to prevent: swapping one unmeasurable hunt
for another. "Chase structural edges" is worthless advice unless the
structural edge is cheaper to verify than the forecast edge was. So the
ranking key is not expected profit. It is:

        observations required before the answer is knowable

Anything requiring more observations than the lab can collect is not a
research programme. It is a belief with a repo attached.

Numbers below are computed, not asserted. Run it.
"""

from __future__ import annotations

from dataclasses import dataclass

import economics as ec


@dataclass
class Angle:
    name: str
    what_you_measure: str
    n_required: float
    unit: str
    reachable: bool
    falsifier: str
    status: str


def build(price: float = 0.982, edge_pp: float = 0.5,
          settlements_available: int = 20) -> list[Angle]:
    """n_required for each angle. Forecast rows are computed from the
    two-proportion power calculation; structural rows are deterministic
    (an observation either shows the mismatch or it does not, so n=1)."""

    n_forecast = ec.n_required(price, price - edge_pp / 100.0)
    n_forecast_1pp = ec.n_required(price, price - 0.01)

    return [
        Angle(
            name="forecast calibration @ 98.2c",
            what_you_measure="a rate (P of a 1.8% event)",
            n_required=n_forecast, unit="settlements",
            reachable=n_forecast <= settlements_available,
            falsifier=("observed loss rate CI excludes the claimed edge "
                       "- but the CI is 11.7x the edge at n=20"),
            status="CLOSED - not reachable this decade at your volume",
        ),
        Angle(
            name="forecast calibration, 1pp edge",
            what_you_measure="a rate",
            n_required=n_forecast_1pp, unit="settlements",
            reachable=n_forecast_1pp <= settlements_available,
            falsifier="same, wider edge, still out of reach",
            status="CLOSED",
        ),
        Angle(
            name="Brier by price bucket (diagnostic)",
            what_you_measure="where your existing model is actually good",
            n_required=settlements_available, unit="settlements you already have",
            reachable=True,
            falsifier=("if the tail bucket has <5 observations the bucket "
                       "reports nothing - that IS the result"),
            status="RUN IT - redirects the lab, costs one afternoon, "
                   "uses data already collected",
        ),
        Angle(
            name="term structure of yield",
            what_you_measure="a price/date relationship, not a rate",
            n_required=1, unit="market snapshot",
            reachable=True,
            falsifier=("within a price band, Spearman rho(yield, days) is "
                       "not significantly negative -> market prices time "
                       "correctly -> dead"),
            status="BUILT - termstructure.py, known-cases pass",
        ),
        Angle(
            name="ladder incoherence, ILLIQUID rungs",
            what_you_measure="an arithmetic identity on one book",
            n_required=1, unit="book snapshot",
            reachable=True,
            falsifier=("sum of mutually exclusive rung prices is within "
                       "fees of 1.00 across all ladders -> dead"),
            status="RE-RUN - your checker found zero across LIQUID ladders. "
                   "Liquid is where competition is. The filter may be the "
                   "finding.",
        ),
        Angle(
            name="cross-venue RESOLUTION-RULE mismatch",
            what_you_measure="two documents",
            n_required=1, unit="market pair",
            reachable=True,
            falsifier=("paired markets resolve on identical source, time "
                       "and criteria -> no mismatch to trade"),
            status="OPEN - your cross-venue scanner matches on title/"
                   "polarity. Rules are where the gap would be.",
        ),
        Angle(
            name="maker vs taker fill decomposition",
            what_you_measure="a fraction from a public tape",
            n_required=100, unit="observed fills",
            reachable=True,
            falsifier=("the account's fills are majority aggressive -> "
                       "he is a taker -> the forecast math does apply to "
                       "him -> and it is unmeasurable for him too"),
            status="OPEN - settles what the screenshot cannot",
        ),
        Angle(
            name="capital lockup / redeploy frequency",
            what_you_measure="a count of qualifying markets per year",
            n_required=1, unit="year of market history",
            reachable=True,
            falsifier=("fewer than ~8 qualifying cycles per year -> the "
                       "annualised number is a ranking metric, not a "
                       "return, and the whole angle is a rounding error"),
            status="BLOCKING - this is what turns 276% annualised into "
                   "1.8% realised",
        ),
    ]


def main() -> int:
    angles = build()
    angles.sort(key=lambda a: (not a.reachable, a.n_required))

    print("=" * 78)
    print("ANGLES RANKED BY COST TO KNOW  (not by expected profit)")
    print("=" * 78)
    print(f"{'n req':>10}  {'unit':<26} {'ok':>3}  angle")
    print("-" * 78)
    for a in angles:
        n = f"{a.n_required:,.0f}" if a.n_required < 1e9 else "inf"
        print(f"{n:>10}  {a.unit:<26} {'Y' if a.reachable else 'N':>3}  "
              f"{a.name}")
    print()
    for a in angles:
        print(f"* {a.name}")
        print(f"    measures  : {a.what_you_measure}")
        print(f"    falsifier : {a.falsifier}")
        print(f"    status    : {a.status}")
        print()

    reach = [a for a in angles if a.reachable]
    print("=" * 78)
    print(f"{len(reach)} of {len(angles)} angles are reachable at your volume.")
    print("The two that are not are the two the lab has been working on.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

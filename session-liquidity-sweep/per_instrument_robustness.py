#!/usr/bin/env python3
"""
per_instrument_robustness.py - is the fixed_1.5R result (+0.118R vs the
-0.117R baseline) a portfolio-wide effect, or a couple of instruments
carrying it?

Same entries/stops/signals as everywhere else in this chain - only asks
"how do baseline/1R/1.5R/2R break down by instrument" and "does removing
any single instrument collapse the 1.5R edge (leave-one-out)".

Does NOT change any rule. Does NOT decide the target is 1.5R. Just tests
whether that number is broad-based or an artifact.

USAGE
    python3 per_instrument_robustness.py            # auto-discovers *-15m.csv
    python3 per_instrument_robustness.py *.csv
"""
import sys
import glob
import numpy as np
import pandas as pd

from diagnostic_backtest import run_engine, load_csv, stats_block
from model_comparison import simulate_model


MODELS = [
    ("baseline", dict(mode="original")),
    ("fixed_1R", dict(mode="fixed_r", fixed_r=1.0)),
    ("fixed_1_5R", dict(mode="fixed_r", fixed_r=1.5)),
    ("fixed_2R", dict(mode="fixed_r", fixed_r=2.0)),
]


def run_all(dfs, model_kwargs, subset=None):
    """subset: list of instrument names to include, or None for all."""
    all_t = []
    for instrument, df in dfs.items():
        if subset is not None and instrument not in subset:
            continue
        t, _ = simulate_model(df, instrument, **model_kwargs)
        if not t.empty:
            all_t.append(t)
    return pd.concat(all_t, ignore_index=True) if all_t else pd.DataFrame(columns=["r_multiple", "mfe_r", "mae_r"])


if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("*-15m.csv"))
    if not files:
        raise SystemExit("No CSV files found/given.")

    dfs = {}
    for path in files:
        instrument = path.split("-15m")[0].split("-5m")[0]
        dfs[instrument] = run_engine(load_csv(path))
    instruments = list(dfs.keys())

    print("===== PER-INSTRUMENT, BASELINE vs FIXED TARGETS =====")
    for model_name, kwargs in MODELS:
        print(f"\n--- {model_name} ---")
        print(f"{'instrument':15s}  {'n':>3s}  {'win_rate':>8s}  {'expectancy':>10s}  {'total_R':>8s}  {'max_dd_r':>8s}  {'streak':>6s}")
        for instrument in instruments:
            t, unresolved = simulate_model(dfs[instrument], instrument, **kwargs)
            if t.empty:
                print(f"{instrument:15s}  {0:>3d}  {'--':>8s}  {'--':>10s}  {'--':>8s}  {'--':>8s}  {'--':>6s}")
                continue
            s = stats_block(t)
            total_r = t["r_multiple"].sum()
            print(f"{instrument:15s}  {s['n']:>3d}  {s['win_rate']:8.3f}  {s['expectancy']:10.3f}  {total_r:8.3f}  "
                  f"{s['max_drawdown_r']:8.3f}  {s['max_losing_streak']:6d}")

    # full-set 1.5R reference
    full_15r = run_all(dfs, dict(mode="fixed_r", fixed_r=1.5))
    full_stats = stats_block(full_15r)
    print(f"\n===== FULL SET fixed_1.5R reference: n={full_stats['n']}  expectancy={full_stats['expectancy']:.3f}  "
          f"total_R={full_15r['r_multiple'].sum():.3f} =====")

    print("\n===== LEAVE-ONE-INSTRUMENT-OUT, fixed_1.5R =====")
    print(f"{'excluded':15s}  {'n':>3s}  {'win_rate':>8s}  {'expectancy':>10s}  {'total_R':>8s}  {'delta_vs_full':>13s}")
    for excluded in instruments:
        subset = [i for i in instruments if i != excluded]
        t = run_all(dfs, dict(mode="fixed_r", fixed_r=1.5), subset=subset)
        if t.empty:
            print(f"{excluded:15s}  (no trades left)")
            continue
        s = stats_block(t)
        delta = s["expectancy"] - full_stats["expectancy"]
        print(f"{excluded:15s}  {s['n']:>3d}  {s['win_rate']:8.3f}  {s['expectancy']:10.3f}  {t['r_multiple'].sum():8.3f}  {delta:13.3f}")

    print("\nReading guide: if expectancy stays positive and delta is small for every excluded instrument,")
    print("the 1.5R edge is broad-based. If excluding one instrument flips expectancy negative or the delta")
    print("is large, that one instrument was carrying the result - not a portfolio-wide effect.")

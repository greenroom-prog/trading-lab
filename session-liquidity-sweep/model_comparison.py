#!/usr/bin/env python3
"""
model_comparison.py - retrospective exit-model comparison. Same entries,
same stops (both already verified), same signals - ONLY the exit rule
changes between models. This is the direct test of "would a bigger/
different target have worked", run AFTER the excursion analysis, not
before, so it isn't curve-fit off a single aggregate number.

Models:
  1. baseline   - current rule: next untouched liquidity level (reference,
                  from diagnostic_backtest.py's own run - not resimulated
                  here, just quoted for comparison)
  2. fixed_1R   - target = entry +/- 1.0R
  3. fixed_1_5R - target = entry +/- 1.5R
  4. fixed_2R   - target = entry +/- 2.0R
  5. be_at_1R   - original target level, but stop moves to breakeven once
                  price reaches +1R in favor (tests whether protecting
                  winners that stall changes anything)

Not implemented yet (need swing-structure detection / partial-position
accounting, deliberately deferred): partial-exit-then-trail, trail-behind-
structure. Build those only if these simpler models show something worth
chasing.

A trade that never resolves before the data runs out is marked
"unresolved" and excluded from that model's stats (reported separately -
NOT silently dropped).

USAGE
    python3 model_comparison.py            # auto-discovers *-15m.csv here
    python3 model_comparison.py *.csv
"""
import sys
import glob
import numpy as np
import pandas as pd

from diagnostic_backtest import run_engine, load_csv, stats_block


def simulate_model(df, instrument, mode, fixed_r=None, be_at=None):
    trades = []
    unresolved = 0
    in_trade = False
    direction = entry_i = entry_price = stop = target = orig_risk = None

    for i in range(len(df)):
        if not in_trade:
            signal_long = df["signal_enter_long"].iat[i]
            signal_short = df["signal_enter_short"].iat[i]
            if (signal_long or signal_short) and i + 1 < len(df):
                in_trade = True
                direction = "long" if signal_long else "short"
                entry_i = i + 1
                entry_price = df["open"].iat[i + 1]
                stop = df["stop_level"].iat[i]
                orig_risk = abs(entry_price - stop)
                if orig_risk <= 0:
                    in_trade = False
                    continue
                if mode == "fixed_r":
                    target = entry_price + fixed_r * orig_risk if direction == "long" else entry_price - fixed_r * orig_risk
                else:  # original target, possibly with breakeven stop
                    target = df["target_level"].iat[i]
                be_armed = False
            continue

        hi, lo = df["high"].iat[i], df["low"].iat[i]
        fav = (hi - entry_price) / orig_risk if direction == "long" else (entry_price - lo) / orig_risk

        if be_at is not None and not be_armed and fav >= be_at:
            stop = entry_price
            be_armed = True

        hit_stop = (lo <= stop) if direction == "long" else (hi >= stop)
        hit_target = (hi >= target) if direction == "long" else (lo <= target)

        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target
            pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
            r_multiple = pnl / orig_risk
            trades.append({"instrument": instrument, "direction": direction, "r_multiple": r_multiple,
                            "mfe_r": np.nan, "mae_r": np.nan})
            in_trade = False
        elif i == len(df) - 1:
            unresolved += 1
            in_trade = False

    return pd.DataFrame(trades), unresolved


if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("*-15m.csv"))
    if not files:
        raise SystemExit("No CSV files found/given.")

    dfs = {}
    for path in files:
        instrument = path.split("-15m")[0].split("-5m")[0]
        dfs[instrument] = run_engine(load_csv(path))

    models = [
        ("fixed_1R",   dict(mode="fixed_r", fixed_r=1.0)),
        ("fixed_1_5R", dict(mode="fixed_r", fixed_r=1.5)),
        ("fixed_2R",   dict(mode="fixed_r", fixed_r=2.0)),
        ("be_at_1R",   dict(mode="original", be_at=1.0)),
        ("original_replay", dict(mode="original")),  # sanity check: should match diagnostic_backtest's -0.117R
    ]

    print("model               n  unresolved  win_rate  expectancy  profit_factor  median_r  max_dd_r  max_losing_streak")
    for name, kwargs in models:
        all_t, total_unresolved = [], 0
        for instrument, df in dfs.items():
            t, unresolved = simulate_model(df, instrument, **kwargs)
            if not t.empty:
                all_t.append(t)
            total_unresolved += unresolved
        combined = pd.concat(all_t, ignore_index=True) if all_t else pd.DataFrame(columns=["r_multiple"])
        s = stats_block(combined)
        if s.get("n", 0) == 0:
            print(f"{name:18s}  0  {total_unresolved:10d}  (no resolved trades)")
            continue
        print(f"{name:18s} {s['n']:2d}  {total_unresolved:10d}  {s['win_rate']:8.3f}  {s['expectancy']:10.3f}  "
              f"{s['profit_factor']:13.3f}  {s['median_r']:8.3f}  {s['max_drawdown_r']:8.3f}  {s['max_losing_streak']:17d}")

    print("\nCompare 'original_replay' above to diagnostic_backtest.py's OVERALL block (n=38, expectancy -0.117R).")
    print("If they don't match, something in this script's replay diverges from the real engine - flag it, don't trust the other rows yet.")

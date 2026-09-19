#!/usr/bin/env python3
"""
path_analysis.py - the shape of the move, not just the final result.

Two separate questions, both off the same unmodified, already-verified
engine (no rule changes):

1. FIXED-HORIZON PATH FROM ENTRY (all trades, win or lose):
   MFE/MAE measured over exactly N bars after entry (N = 1,2,3,5,10),
   regardless of when/whether the trade actually exited. This shows
   whether losers crash immediately (supporting "bad entry, not bad
   management") and whether winners' favorable move builds gradually or
   all at once.

2. POST-EXIT CONTINUATION (trades that hit the ORIGINAL target only):
   MFE/MAE for the N bars AFTER the target was hit, measured from the
   same entry/risk basis. This is the direct test of the same-bar
   censoring problem: does price that reached the target keep moving in
   the same direction (target too conservative) or reverse immediately
   (move has poor persistence, a bigger target would not have helped)?

Does not change any entry, stop, or target rule. Does not decide
anything - just reports the path.

USAGE
    python3 path_analysis.py            # auto-discovers *-15m.csv here
    python3 path_analysis.py *.csv
"""
import sys
import glob
import numpy as np
import pandas as pd

from diagnostic_backtest import run_engine, load_csv

HORIZONS = [1, 2, 3, 5, 10]


def window_mfe_mae(high, low, entry_price, risk, direction, start, end):
    """MFE/MAE in R over bars [start, end] inclusive, clipped to array bounds.
    Returns (mfe, mae, bars_available) - bars_available < requested means
    the window ran off the end of the data (flag it, don't hide it)."""
    end = min(end, len(high) - 1)
    if start > end or risk <= 0:
        return np.nan, np.nan, 0
    seg_hi, seg_lo = high[start:end + 1], low[start:end + 1]
    if direction == "long":
        mfe = (seg_hi.max() - entry_price) / risk
        mae = (seg_lo.min() - entry_price) / risk
    else:
        mfe = (entry_price - seg_lo.min()) / risk
        mae = (entry_price - seg_hi.max()) / risk
    return mfe, mae, end - start + 1


def analyze(df, instrument):
    high, low = df["high"].to_numpy(), df["low"].to_numpy()
    n = len(df)
    rows = []
    in_trade = False
    direction = entry_i = entry_price = stop = target = risk = None

    for i in range(n):
        if not in_trade:
            signal_long = df["signal_enter_long"].iat[i]
            signal_short = df["signal_enter_short"].iat[i]
            if (signal_long or signal_short) and i + 1 < n:
                in_trade = True
                direction = "long" if signal_long else "short"
                entry_i = i + 1
                entry_price = df["open"].iat[i + 1]
                stop = df["stop_level"].iat[i]
                target = df["target_level"].iat[i]
                risk = abs(entry_price - stop)
            continue

        hi, lo = high[i], low[i]
        hit_stop = (lo <= stop) if direction == "long" else (hi >= stop)
        hit_target = (hi >= target) if direction == "long" else (lo <= target)

        if hit_stop or hit_target:
            exit_i = i
            reason = "stop" if hit_stop else "target"
            exit_price = stop if hit_stop else target
            pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
            r_multiple = pnl / risk if risk > 0 else np.nan

            row = {"instrument": instrument, "direction": direction, "exit_reason": reason,
                   "r_multiple": r_multiple, "entry_i": entry_i, "exit_i": exit_i}

            for h in HORIZONS:
                mfe, mae, avail = window_mfe_mae(high, low, entry_price, risk, direction,
                                                  entry_i, entry_i + h - 1)
                row[f"mfe_{h}bar"], row[f"mae_{h}bar"], row[f"bars_avail_{h}bar"] = mfe, mae, avail

            if reason == "target":
                for h in HORIZONS:
                    mfe, mae, avail = window_mfe_mae(high, low, entry_price, risk, direction,
                                                      exit_i + 1, exit_i + h)
                    row[f"post_target_mfe_{h}bar"] = mfe
                    row[f"post_target_mae_{h}bar"] = mae
                    row[f"post_target_bars_avail_{h}bar"] = avail
            else:
                for h in HORIZONS:
                    row[f"post_target_mfe_{h}bar"] = np.nan
                    row[f"post_target_mae_{h}bar"] = np.nan
                    row[f"post_target_bars_avail_{h}bar"] = 0

            rows.append(row)
            in_trade = False

    return pd.DataFrame(rows)


if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("*-15m.csv"))
    if not files:
        raise SystemExit("No CSV files found/given.")

    all_rows = []
    for path in files:
        instrument = path.split("-15m")[0].split("-5m")[0]
        df = run_engine(load_csv(path))
        t = analyze(df, instrument)
        if not t.empty:
            all_rows.append(t)

    if not all_rows:
        raise SystemExit("No trades - nothing to analyze.")

    combined = pd.concat(all_rows, ignore_index=True)
    combined.to_csv("path_analysis_full.csv", index=False)
    print(f"Full path log ({len(combined)} rows) written to path_analysis_full.csv")

    winners = combined[combined["r_multiple"] > 0]
    losers = combined[combined["r_multiple"] <= 0]

    print("\n===== FIXED-HORIZON PATH FROM ENTRY (mean R at each horizon) =====")
    print(f"{'horizon':>8s}  {'all_mfe':>8s}  {'all_mae':>8s}  {'win_mfe':>8s}  {'win_mae':>8s}  {'loss_mfe':>8s}  {'loss_mae':>8s}")
    for h in HORIZONS:
        print(f"{h:>7d}b  {combined[f'mfe_{h}bar'].mean():8.3f}  {combined[f'mae_{h}bar'].mean():8.3f}  "
              f"{winners[f'mfe_{h}bar'].mean():8.3f}  {winners[f'mae_{h}bar'].mean():8.3f}  "
              f"{losers[f'mfe_{h}bar'].mean():8.3f}  {losers[f'mae_{h}bar'].mean():8.3f}")

    target_hits = combined[combined["exit_reason"] == "target"]
    print(f"\n===== POST-TARGET CONTINUATION (n={len(target_hits)} trades that hit the original target) =====")
    print("Positive post_target_mfe with post_target_mae near zero = move kept going, target was conservative.")
    print("post_target_mae going meaningfully negative fast = move reverses right after target, bigger target would give it back.")
    print(f"{'horizon':>8s}  {'avg_post_mfe':>13s}  {'avg_post_mae':>13s}  {'avg_bars_avail':>15s}")
    for h in HORIZONS:
        avail_col = f"post_target_bars_avail_{h}bar"
        full = target_hits[target_hits[avail_col] == h]  # only rows where the full window existed (no end-of-data clipping)
        if full.empty:
            print(f"{h:>7d}b  (no trades with {h} full bars of data after target)")
            continue
        print(f"{h:>7d}b  {full[f'post_target_mfe_{h}bar'].mean():13.3f}  {full[f'post_target_mae_{h}bar'].mean():13.3f}  "
              f"{len(full):15d}")

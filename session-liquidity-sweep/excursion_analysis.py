#!/usr/bin/env python3
"""
excursion_analysis.py - what does price actually do AFTER entry, before we
decide the target is the problem.

Reuses the exact same, already-verified sweep engine from
diagnostic_backtest.py (no rule changes - same entries, same stops, same
targets). Adds per-bar excursion tracking so we can answer:

  - Of trades that eventually LOSE, how many were positive first, and how
    far did they get before reversing to the stop?
  - Of trades that eventually WIN, how far past the actual target did price
    tend to go (would a bigger target have worked, or does it never get
    there)?
  - How long (bars) does it take to reach the best point in the trade,
    versus how long it takes to hit the stop or the target?

This does NOT change the target rule, the stop rule, or drop any trade.
It's strictly "what happened after entry," split by eventual outcome.

USAGE
    python3 excursion_analysis.py            # auto-discovers *-15m.csv here
    python3 excursion_analysis.py *.csv
"""
import sys
import glob
import numpy as np
import pandas as pd

from diagnostic_backtest import run_engine, load_csv

THRESHOLDS = [0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0]


def simulate_with_excursion(df, instrument):
    trades = []
    in_trade = False
    direction = entry_i = entry_price = stop = target = tag = None
    risk = mfe_r = mae_r = bars_to_mfe = None

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
                target = df["target_level"].iat[i]
                tag = df["entry_tag"].iat[i]
                risk = abs(entry_price - stop)
                mfe_r, mae_r, bars_to_mfe = 0.0, 0.0, 0
            continue

        hi, lo = df["high"].iat[i], df["low"].iat[i]
        if direction == "long":
            fav = (hi - entry_price) / risk if risk > 0 else 0.0
            adv = (lo - entry_price) / risk if risk > 0 else 0.0
        else:
            fav = (entry_price - lo) / risk if risk > 0 else 0.0
            adv = (entry_price - hi) / risk if risk > 0 else 0.0

        if fav > mfe_r:
            mfe_r = fav
            bars_to_mfe = i - entry_i
        mae_r = min(mae_r, adv)

        hit_stop = (lo <= stop) if direction == "long" else (hi >= stop)
        hit_target = (hi >= target) if direction == "long" else (lo <= target)

        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target
            reason = "stop" if hit_stop else "target"
            pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
            r_multiple = pnl / risk if risk > 0 else np.nan
            outcome = "win" if r_multiple > 0 else "loss"

            row = {
                "instrument": instrument, "setup": tag, "direction": direction,
                "outcome": outcome, "exit_reason": reason, "r_multiple": r_multiple,
                "risk_abs": risk,
                "bars_held": i - entry_i,
                "bars_to_stop": (i - entry_i) if reason == "stop" else np.nan,
                "bars_to_target": (i - entry_i) if reason == "target" else np.nan,
                "mfe_r": mfe_r, "mae_r": mae_r, "bars_to_mfe": bars_to_mfe,
            }
            for th in THRESHOLDS:
                row[f"reached_{th}R"] = mfe_r >= th
            trades.append(row)
            in_trade = False

    return pd.DataFrame(trades)


def threshold_table(t, label):
    print(f"\n--- % of trades reaching each MFE threshold before exit: {label} (n={len(t)}) ---")
    if t.empty:
        print("  (no trades)")
        return
    for th in THRESHOLDS:
        pct = t[f"reached_{th}R"].mean() * 100
        print(f"  +{th}R : {pct:5.1f}%")


def timing_table(t, label):
    if t.empty:
        return
    print(f"\n--- timing: {label} (n={len(t)}) ---")
    print(f"  avg bars to MFE:        {t['bars_to_mfe'].mean():.2f}  (median {t['bars_to_mfe'].median():.1f})")
    print(f"  avg bars held total:    {t['bars_held'].mean():.2f}")
    to_stop = t[t['exit_reason'] == 'stop']['bars_to_stop']
    to_target = t[t['exit_reason'] == 'target']['bars_to_target']
    if len(to_stop):
        print(f"  avg bars entry->stop:   {to_stop.mean():.2f}  (n={len(to_stop)})")
    if len(to_target):
        print(f"  avg bars entry->target: {to_target.mean():.2f}  (n={len(to_target)})")


if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("*-15m.csv"))
    if not files:
        raise SystemExit("No CSV files found/given.")

    all_trades = []
    for path in files:
        instrument = path.split("-15m")[0].split("-5m")[0]
        df = load_csv(path)
        df = run_engine(df)
        trades = simulate_with_excursion(df, instrument)
        if not trades.empty:
            all_trades.append(trades)

    if not all_trades:
        raise SystemExit("No trades - nothing to analyze.")

    combined = pd.concat(all_trades, ignore_index=True)
    combined.to_csv("excursion_trades_full.csv", index=False)
    print(f"Full excursion log ({len(combined)} rows) written to excursion_trades_full.csv")

    winners = combined[combined["outcome"] == "win"]
    losers = combined[combined["outcome"] == "loss"]

    print(f"\n===== OVERALL: {len(combined)} trades, {len(winners)} eventual winners, {len(losers)} eventual losers =====")
    threshold_table(combined, "ALL TRADES")
    threshold_table(winners, "EVENTUAL WINNERS")
    threshold_table(losers, "EVENTUAL LOSERS (the key question: did losers go positive first?)")

    timing_table(combined, "ALL TRADES")
    timing_table(winners, "EVENTUAL WINNERS")
    timing_table(losers, "EVENTUAL LOSERS")

    print("\n===== AVG MFE ACTUALLY CAPTURED vs REACHED (by outcome) =====")
    for name, g in [("winners", winners), ("losers", losers)]:
        if len(g):
            print(f"  {name}: avg final R = {g['r_multiple'].mean():.3f}   avg MFE reached = {g['mfe_r'].mean():.3f}   "
                  f"gap = {(g['mfe_r'].mean() - g['r_multiple'].mean()):.3f}")

    print("\n===== BY INSTRUMENT: winners' avg MFE reached vs avg final R captured =====")
    for name, g in combined.groupby("instrument"):
        w = g[g["outcome"] == "win"]
        if len(w):
            print(f"  {name:15s} n={len(w):2d}  avg_final_R={w['r_multiple'].mean():6.3f}  avg_MFE={w['mfe_r'].mean():6.3f}  "
                  f"left_on_table={(w['mfe_r'].mean()-w['r_multiple'].mean()):6.3f}")

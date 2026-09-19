#!/usr/bin/env python3
"""
entry_quality_variants.py - Experiment 2. Same qualifying sweep, same
displacement detection, same stop, same target (next liquidity level).
ONLY the entry trigger changes. Tests whether entry quality improves
expectancy monotonically - it does not hunt for a profitable variant.

Modes:
  A. current       - existing rule: enter on 50-79% retracement into the
                      sweep-to-displacement leg (baseline, unchanged).
  B. confirmation   - after the displacement candle closes, require the
                      NEXT candle to also close in the trade direction
                      before entering (no retracement wait - enters at
                      the open two bars after displacement, only if that
                      confirmation candle held).
  C. retest         - enter only when price pulls back into the
                      displacement candle's OWN body (a tighter, more
                      specific "retest" than the broad leg-fib zone).
  D. strong_disp    - same entry as A (50-79% retracement), but the
                      displacement candle must be >= 2.0x ATR to even
                      qualify as a setup (vs 1.5x baseline) - fewer,
                      stronger setups only.

All four keep the identical sweep/reclaim detection and the identical
stop and target rules - isolating entry timing/quality as the only
variable, per the "does entry quality improve expectancy monotonically"
question. Not a parameter sweep - four fixed, conceptually distinct
entry rules, run once each.

USAGE
    python3 entry_quality_variants.py            # auto-discovers *-15m.csv
    python3 entry_quality_variants.py *.csv
"""
import sys
import glob
import numpy as np
import pandas as pd

from diagnostic_backtest import (
    ASIA_START, ASIA_END, LONDON_START, LONDON_END,
    SWEEP_RECLAIM_BARS, DISP_LOOKBACK_BARS, DISP_ATR_LEN,
    RETR_MIN_PCT, STOP_BUFFER_ATR,
    _atr, _in_session, load_csv, stats_block,
)

MODES = ["current", "confirmation", "retest", "strong_disp"]


def run_engine_mode(df, mode):
    df = df.copy()
    df["atr"] = _atr(df, DISP_ATR_LEN)
    ct = df["date"].dt.tz_convert("America/Chicago")
    minute_of_day = (ct.dt.hour * 60 + ct.dt.minute).to_numpy()

    asia_session = _in_session(minute_of_day, *ASIA_START, *ASIA_END)
    london_session = _in_session(minute_of_day, *LONDON_START, *LONDON_END)

    calendar_day = ct.dt.date.to_numpy()
    tmp = pd.DataFrame({"day": calendar_day, "high": df["high"], "low": df["low"]})
    prev_daily_high = tmp.groupby("day")["high"].max().shift(1)
    prev_daily_low = tmp.groupby("day")["low"].min().shift(1)
    df["pd_high"] = pd.Series(calendar_day).map(prev_daily_high).to_numpy()
    df["pd_low"] = pd.Series(calendar_day).map(prev_daily_low).to_numpy()

    n = len(df)
    high, low, close, open_, atr = (df[c].to_numpy() for c in ["high", "low", "close", "open", "atr"])
    asia_high = np.full(n, np.nan); asia_low = np.full(n, np.nan)
    london_high = np.full(n, np.nan); london_low = np.full(n, np.nan)
    enter_long = np.zeros(n, bool); enter_short = np.zeros(n, bool)
    stop_level = np.full(n, np.nan); target_level = np.full(n, np.nan)

    disp_mult = 2.0 if mode == "strong_disp" else 1.5

    def fresh():
        return {"pending": False, "swept": False, "held": False, "disp_done": False,
                "entry_done": False, "invalid": False, "pierce_bar": -1, "swept_bar": -1,
                "extreme": np.nan, "disp_bar": -1, "disp_extreme": np.nan, "disp_open": np.nan,
                "confirm_checked": False, "confirm_bar": -1}

    al, ah, ll, lh = fresh(), fresh(), fresh(), fresh()
    cur_ah = cur_al = cur_lh = cur_ll = np.nan

    for i in range(n):
        new_asia = asia_session[i] and (i == 0 or not asia_session[i - 1])
        new_london = london_session[i] and (i == 0 or not london_session[i - 1])
        if new_asia:
            cur_ah, cur_al = high[i], low[i]
            al, ah = fresh(), fresh()
        elif asia_session[i]:
            cur_ah, cur_al = max(cur_ah, high[i]), min(cur_al, low[i])
        asia_high[i], asia_low[i] = cur_ah, cur_al

        if new_london:
            cur_lh, cur_ll = high[i], low[i]
            ll, lh = fresh(), fresh()
        elif london_session[i]:
            cur_lh, cur_ll = max(cur_lh, high[i]), min(cur_ll, low[i])
        london_high[i], london_low[i] = cur_lh, cur_ll

        def bullish(level, ready, s, tag):
            if not ready or np.isnan(level):
                return
            if not s["swept"] and not s["held"] and not s["invalid"]:
                if not s["pending"] and low[i] < level:
                    s["pending"], s["pierce_bar"], s["extreme"] = True, i, low[i]
                elif s["pending"]:
                    s["extreme"] = min(s["extreme"], low[i])
                    if close[i] > level:
                        s["swept"], s["pending"], s["swept_bar"] = True, False, i
                    elif i - s["pierce_bar"] > SWEEP_RECLAIM_BARS:
                        s["held"], s["pending"] = True, False
            if s["swept"] and not s["disp_done"] and not s["invalid"]:
                if i - s["pierce_bar"] <= DISP_LOOKBACK_BARS:
                    body = close[i] - open_[i]
                    if body > 0 and not np.isnan(atr[i]) and body >= disp_mult * atr[i]:
                        s["disp_done"], s["disp_bar"], s["disp_extreme"], s["disp_open"] = True, i, high[i], open_[i]
                else:
                    s["invalid"] = True
            if s["disp_done"] and not s["entry_done"] and not s["invalid"]:
                leg_high, leg_low = s["disp_extreme"], s["extreme"]
                if low[i] < leg_low:
                    s["invalid"] = True
                    return
                if mode in ("current", "strong_disp"):
                    zone_upper = leg_high - (leg_high - leg_low) * RETR_MIN_PCT
                    if low[i] <= zone_upper:
                        s["entry_done"] = True
                        enter_long[i] = True
                        stop_level[i] = leg_low - STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)
                elif mode == "confirmation":
                    if not s["confirm_checked"] and i == s["disp_bar"] + 1:
                        s["confirm_checked"] = True
                        if close[i] > open_[i]:
                            s["confirm_bar"] = i
                        else:
                            s["invalid"] = True
                    elif s["confirm_checked"] and s["confirm_bar"] > 0 and i == s["confirm_bar"] + 1:
                        s["entry_done"] = True
                        enter_long[i] = True
                        stop_level[i] = leg_low - STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)
                elif mode == "retest":
                    retest_top = s["disp_extreme"] if s["disp_open"] > s["disp_extreme"] else close[s["disp_bar"]]
                    # retest zone = the displacement candle's own body (open..close), not the whole leg
                    disp_close = close[s["disp_bar"]]
                    body_top, body_bot = max(s["disp_open"], disp_close), min(s["disp_open"], disp_close)
                    if i > s["disp_bar"] and body_bot <= low[i] <= body_top:
                        s["entry_done"] = True
                        enter_long[i] = True
                        stop_level[i] = leg_low - STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)

        bullish(asia_low[i], not asia_session[i], al, "asia_low")
        bullish(london_low[i], not london_session[i], ll, "london_low")

        def bearish(level, ready, s, tag):
            if not ready or np.isnan(level):
                return
            if not s["swept"] and not s["held"] and not s["invalid"]:
                if not s["pending"] and high[i] > level:
                    s["pending"], s["pierce_bar"], s["extreme"] = True, i, high[i]
                elif s["pending"]:
                    s["extreme"] = max(s["extreme"], high[i])
                    if close[i] < level:
                        s["swept"], s["pending"], s["swept_bar"] = True, False, i
                    elif i - s["pierce_bar"] > SWEEP_RECLAIM_BARS:
                        s["held"], s["pending"] = True, False
            if s["swept"] and not s["disp_done"] and not s["invalid"]:
                if i - s["pierce_bar"] <= DISP_LOOKBACK_BARS:
                    body = open_[i] - close[i]
                    if body > 0 and not np.isnan(atr[i]) and body >= disp_mult * atr[i]:
                        s["disp_done"], s["disp_bar"], s["disp_extreme"], s["disp_open"] = True, i, low[i], open_[i]
                else:
                    s["invalid"] = True
            if s["disp_done"] and not s["entry_done"] and not s["invalid"]:
                leg_low, leg_high = s["disp_extreme"], s["extreme"]
                if high[i] > leg_high:
                    s["invalid"] = True
                    return
                if mode in ("current", "strong_disp"):
                    zone_lower = leg_low + (leg_high - leg_low) * RETR_MIN_PCT
                    if high[i] >= zone_lower:
                        s["entry_done"] = True
                        enter_short[i] = True
                        stop_level[i] = leg_high + STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)
                elif mode == "confirmation":
                    if not s["confirm_checked"] and i == s["disp_bar"] + 1:
                        s["confirm_checked"] = True
                        if close[i] < open_[i]:
                            s["confirm_bar"] = i
                        else:
                            s["invalid"] = True
                    elif s["confirm_checked"] and s["confirm_bar"] > 0 and i == s["confirm_bar"] + 1:
                        s["entry_done"] = True
                        enter_short[i] = True
                        stop_level[i] = leg_high + STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)
                elif mode == "retest":
                    disp_close = close[s["disp_bar"]]
                    body_top, body_bot = max(s["disp_open"], disp_close), min(s["disp_open"], disp_close)
                    if i > s["disp_bar"] and body_bot <= high[i] <= body_top:
                        s["entry_done"] = True
                        enter_short[i] = True
                        stop_level[i] = leg_high + STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)

        bearish(asia_high[i], not asia_session[i], ah, "asia_high")
        bearish(london_high[i], not london_session[i], lh, "london_high")

        if enter_long[i]:
            cands = [v for v in (asia_high[i], london_high[i], df["pd_high"].iat[i]) if not np.isnan(v) and v > close[i]]
            target_level[i] = min(cands) if cands else close[i] + 2 * (close[i] - stop_level[i])
        if enter_short[i]:
            cands = [v for v in (asia_low[i], london_low[i], df["pd_low"].iat[i]) if not np.isnan(v) and v < close[i]]
            target_level[i] = max(cands) if cands else close[i] - 2 * (stop_level[i] - close[i])

    df["signal_enter_long"], df["signal_enter_short"] = enter_long, enter_short
    df["stop_level"], df["target_level"] = stop_level, target_level
    return df


def simulate(df, instrument):
    trades = []
    in_trade = False
    direction = entry_price = stop = target = None

    for i in range(len(df)):
        if not in_trade:
            sl, ss = df["signal_enter_long"].iat[i], df["signal_enter_short"].iat[i]
            if (sl or ss) and i + 1 < len(df):
                in_trade = True
                direction = "long" if sl else "short"
                entry_price = df["open"].iat[i + 1]
                stop = df["stop_level"].iat[i]
                target = df["target_level"].iat[i]
                risk = abs(entry_price - stop)
            continue
        hi, lo = df["high"].iat[i], df["low"].iat[i]
        hit_stop = (lo <= stop) if direction == "long" else (hi >= stop)
        hit_target = (hi >= target) if direction == "long" else (lo <= target)
        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target
            pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
            r_multiple = pnl / risk if risk > 0 else np.nan
            trades.append({"instrument": instrument, "direction": direction, "r_multiple": r_multiple,
                            "mfe_r": np.nan, "mae_r": np.nan})
            in_trade = False
    return pd.DataFrame(trades)


if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("*-15m.csv"))
    if not files:
        raise SystemExit("No CSV files found/given.")

    print(f"{'mode':14s}  {'n':>4s}  {'win_rate':>8s}  {'expectancy':>10s}  {'profit_factor':>13s}  {'max_dd_r':>8s}  {'streak':>6s}")
    for mode in MODES:
        all_t = []
        for path in files:
            instrument = path.split("-15m")[0].split("-5m")[0]
            df = run_engine_mode(load_csv(path), mode)
            t = simulate(df, instrument)
            if not t.empty:
                all_t.append(t)
        combined = pd.concat(all_t, ignore_index=True) if all_t else pd.DataFrame(columns=["r_multiple", "mfe_r", "mae_r"])
        s = stats_block(combined)
        if s.get("n", 0) == 0:
            print(f"{mode:14s}   0  (no trades)")
            continue
        print(f"{mode:14s} {s['n']:>4d}  {s['win_rate']:8.3f}  {s['expectancy']:10.3f}  "
              f"{s['profit_factor']:13.3f}  {s['max_drawdown_r']:8.3f}  {s['max_losing_streak']:6d}")
        combined.to_csv(f"entry_variant_{mode}_trades.csv", index=False)
        if mode == "current":
            print(f"  >>> CHECKSUM: 'current' should be ~n=408, expectancy~-0.149 (diagnostic_backtest.py's real baseline).")
            print(f"  >>> Got n={s['n']}, expectancy={s['expectancy']:.3f}. {'MATCH - trust the rest of this table.' if abs(s['expectancy'] - (-0.149)) < 0.02 and abs(s['n'] - 408) <= 5 else 'MISMATCH - stop, do not trust confirmation/retest/strong_disp below.'}")

    print("\nReading guide: does expectancy move monotonically better from current -> confirmation -> retest -> strong_disp?")
    print("If none beat current meaningfully, entry-quality tweaks aren't the fix either - the sequence experiment is next.")

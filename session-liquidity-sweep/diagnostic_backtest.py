#!/usr/bin/env python3
"""
diagnostic_backtest.py - autopsy, not optimization.

Re-runs the same sweep -> displacement -> retracement engine already traced
and verified (session_liquidity_sweep.pine / backtest_session_sweep.py), but
logs everything about every trade instead of just entry/exit/R: MAE, MFE,
session and day-of-week/hour at entry, sweep distance, displacement size (in
ATR units), retracement depth actually achieved, and time from sweep to
entry. Then breaks the combined trade set down by instrument, setup,
direction, session, day of week and hour, and reports win rate, avg
winner, avg loser, expectancy, profit factor, median R, max drawdown, max
losing streak, and MFE/MAE distributions per bucket.

Deliberately does NOT change any rule or threshold from the run that
produced -0.12R. No trade is dropped for being inconvenient. The point is
to find out WHERE the number comes from before touching anything.

USAGE
    python3 diagnostic_backtest.py            # auto-discovers *-15m.csv here
    python3 diagnostic_backtest.py *.csv      # or name files explicitly
"""
import sys
import glob
import numpy as np
import pandas as pd

ASIA_START, ASIA_END = (19, 0), (4, 0)
LONDON_START, LONDON_END = (2, 0), (11, 0)
NY_OPEN = (9, 30)
LONDON_NY_OVERLAP_START, LONDON_NY_OVERLAP_END = (7, 0), (11, 0)

SWEEP_RECLAIM_BARS = 5
DISP_LOOKBACK_BARS = 3
DISP_ATR_LEN = 14
DISP_ATR_MULT = 1.5
RETR_MIN_PCT = 0.50
RETR_MAX_PCT = 0.79
STOP_BUFFER_ATR = 0.10


def _true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def _atr(df, length):
    return _true_range(df).rolling(length, min_periods=length).mean()


def _in_session(minute_of_day, start_h, start_m, end_h, end_m):
    s, e = start_h * 60 + start_m, end_h * 60 + end_m
    if s <= e:
        return (minute_of_day >= s) & (minute_of_day < e)
    return (minute_of_day >= s) | (minute_of_day < e)


def load_csv(path):
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    return df.sort_values("date").reset_index(drop=True)


def run_engine(df):
    """Same rules as before, PLUS extra tracking columns for the autopsy."""
    df = df.copy()
    df["atr"] = _atr(df, DISP_ATR_LEN)

    ct = df["date"].dt.tz_convert("America/Chicago")
    minute_of_day = (ct.dt.hour * 60 + ct.dt.minute).to_numpy()
    calendar_day = ct.dt.date.to_numpy()

    asia_session = _in_session(minute_of_day, *ASIA_START, *ASIA_END)
    london_session = _in_session(minute_of_day, *LONDON_START, *LONDON_END)

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
    entry_tag = np.array([""] * n, dtype=object)

    # ---- autopsy fields, all keyed to the signal (entry) bar ----
    sweep_distance = np.full(n, np.nan)       # how far price wicked past the level
    disp_size_atr = np.full(n, np.nan)        # displacement candle body, in ATR units
    retr_depth_pct = np.full(n, np.nan)       # how deep into the leg price had retraced at entry
    bars_sweep_to_entry = np.full(n, np.nan)
    bars_pierce_to_sweep = np.full(n, np.nan)
    asia_high_at_sig = np.full(n, np.nan); asia_low_at_sig = np.full(n, np.nan)
    london_high_at_sig = np.full(n, np.nan); london_low_at_sig = np.full(n, np.nan)
    pd_high_at_sig = np.full(n, np.nan); pd_low_at_sig = np.full(n, np.nan)

    def fresh():
        return {"pending": False, "swept": False, "held": False, "disp_done": False,
                "entry_done": False, "invalid": False, "pierce_bar": -1, "swept_bar": -1,
                "extreme": np.nan, "disp_bar": -1, "disp_extreme": np.nan, "disp_body": np.nan}

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
                    if body > 0 and not np.isnan(atr[i]) and body >= DISP_ATR_MULT * atr[i]:
                        s["disp_done"], s["disp_bar"], s["disp_extreme"], s["disp_body"] = True, i, high[i], body
                else:
                    s["invalid"] = True
            if s["disp_done"] and not s["entry_done"] and not s["invalid"]:
                leg_high, leg_low = s["disp_extreme"], s["extreme"]
                zone_upper = leg_high - (leg_high - leg_low) * RETR_MIN_PCT
                if low[i] < leg_low:
                    s["invalid"] = True
                elif low[i] <= zone_upper:
                    s["entry_done"] = True
                    enter_long[i] = True
                    stop_level[i] = leg_low - STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)
                    entry_tag[i] = f"sweep_{tag}"
                    sweep_distance[i] = level - s["extreme"]
                    disp_atr_at_disp = atr[s["disp_bar"]]
                    disp_size_atr[i] = s["disp_body"] / disp_atr_at_disp if disp_atr_at_disp and not np.isnan(disp_atr_at_disp) else np.nan
                    retr_depth_pct[i] = (leg_high - low[i]) / (leg_high - leg_low) if leg_high != leg_low else np.nan
                    bars_sweep_to_entry[i] = i - s["swept_bar"]
                    bars_pierce_to_sweep[i] = s["swept_bar"] - s["pierce_bar"]
                    asia_high_at_sig[i], asia_low_at_sig[i] = asia_high[i], asia_low[i]
                    london_high_at_sig[i], london_low_at_sig[i] = london_high[i], london_low[i]
                    pd_high_at_sig[i], pd_low_at_sig[i] = df["pd_high"].iat[i], df["pd_low"].iat[i]

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
                    if body > 0 and not np.isnan(atr[i]) and body >= DISP_ATR_MULT * atr[i]:
                        s["disp_done"], s["disp_bar"], s["disp_extreme"], s["disp_body"] = True, i, low[i], body
                else:
                    s["invalid"] = True
            if s["disp_done"] and not s["entry_done"] and not s["invalid"]:
                leg_low, leg_high = s["disp_extreme"], s["extreme"]
                zone_lower = leg_low + (leg_high - leg_low) * RETR_MIN_PCT
                if high[i] > leg_high:
                    s["invalid"] = True
                elif high[i] >= zone_lower:
                    s["entry_done"] = True
                    enter_short[i] = True
                    stop_level[i] = leg_high + STOP_BUFFER_ATR * (atr[i] if not np.isnan(atr[i]) else 0.0)
                    entry_tag[i] = f"sweep_{tag}"
                    sweep_distance[i] = s["extreme"] - level
                    disp_atr_at_disp = atr[s["disp_bar"]]
                    disp_size_atr[i] = s["disp_body"] / disp_atr_at_disp if disp_atr_at_disp and not np.isnan(disp_atr_at_disp) else np.nan
                    retr_depth_pct[i] = (high[i] - leg_low) / (leg_high - leg_low) if leg_high != leg_low else np.nan
                    bars_sweep_to_entry[i] = i - s["swept_bar"]
                    bars_pierce_to_sweep[i] = s["swept_bar"] - s["pierce_bar"]
                    asia_high_at_sig[i], asia_low_at_sig[i] = asia_high[i], asia_low[i]
                    london_high_at_sig[i], london_low_at_sig[i] = london_high[i], london_low[i]
                    pd_high_at_sig[i], pd_low_at_sig[i] = df["pd_high"].iat[i], df["pd_low"].iat[i]

        bearish(asia_high[i], not asia_session[i], ah, "asia_high")
        bearish(london_high[i], not london_session[i], lh, "london_high")

        if enter_long[i]:
            cands = [v for v in (asia_high[i], london_high[i], df["pd_high"].iat[i]) if not np.isnan(v) and v > close[i]]
            target_level[i] = min(cands) if cands else close[i] + 2 * (close[i] - stop_level[i])
        if enter_short[i]:
            cands = [v for v in (asia_low[i], london_low[i], df["pd_low"].iat[i]) if not np.isnan(v) and v < close[i]]
            target_level[i] = max(cands) if cands else close[i] - 2 * (stop_level[i] - close[i])

    df["signal_enter_long"], df["signal_enter_short"] = enter_long, enter_short
    df["stop_level"], df["target_level"], df["entry_tag"] = stop_level, target_level, entry_tag
    df["sweep_distance"] = sweep_distance
    df["disp_size_atr"] = disp_size_atr
    df["retr_depth_pct"] = retr_depth_pct
    df["bars_sweep_to_entry"] = bars_sweep_to_entry
    df["bars_pierce_to_sweep"] = bars_pierce_to_sweep
    df["asia_high_sig"], df["asia_low_sig"] = asia_high_at_sig, asia_low_at_sig
    df["london_high_sig"], df["london_low_sig"] = london_high_at_sig, london_low_at_sig
    df["pd_high_sig"], df["pd_low_sig"] = pd_high_at_sig, pd_low_at_sig
    return df


def in_window(hour, minute, start, end):
    t, s, e = hour * 60 + minute, start[0] * 60 + start[1], end[0] * 60 + end[1]
    return (s <= t < e) if s <= e else (t >= s or t < e)


def simulate_with_autopsy(df, instrument):
    trades = []
    in_trade = False
    direction = entry_i = entry_price = stop = target = tag = None
    sig_i = None

    for i in range(len(df)):
        if not in_trade:
            signal_long = df["signal_enter_long"].iat[i]
            signal_short = df["signal_enter_short"].iat[i]
            if (signal_long or signal_short) and i + 1 < len(df):
                in_trade = True
                direction = "long" if signal_long else "short"
                sig_i = i
                entry_i = i + 1
                entry_price = df["open"].iat[i + 1]
                stop = df["stop_level"].iat[i]
                target = df["target_level"].iat[i]
                tag = df["entry_tag"].iat[i]
                risk = abs(entry_price - stop)
                mfe_r, mae_r = 0.0, 0.0
            continue

        hi, lo = df["high"].iat[i], df["low"].iat[i]
        if direction == "long":
            fav = (hi - entry_price) / risk if risk > 0 else 0.0
            adv = (lo - entry_price) / risk if risk > 0 else 0.0
        else:
            fav = (entry_price - lo) / risk if risk > 0 else 0.0
            adv = (entry_price - hi) / risk if risk > 0 else 0.0
        mfe_r = max(mfe_r, fav)
        mae_r = min(mae_r, adv)

        hit_stop = (lo <= stop) if direction == "long" else (hi >= stop)
        hit_target = (hi >= target) if direction == "long" else (lo <= target)

        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target
            reason = "stop" if hit_stop else "target"
            pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
            r_multiple = pnl / risk if risk > 0 else np.nan

            entry_dt = df["date"].iat[entry_i]
            ct = entry_dt.tz_convert("America/Chicago")

            trades.append({
                "instrument": instrument,
                "setup": tag,
                "direction": direction,
                "day_of_week": ct.strftime("%A"),
                "hour_ct": ct.hour,
                "was_london": in_window(ct.hour, ct.minute, LONDON_START, LONDON_END),
                "was_london_ny_overlap": in_window(ct.hour, ct.minute, LONDON_NY_OVERLAP_START, LONDON_NY_OVERLAP_END),
                "was_ny_open_plus": ct.hour * 60 + ct.minute >= NY_OPEN[0] * 60 + NY_OPEN[1],
                "entry_date": entry_dt, "exit_date": df["date"].iat[i],
                "entry": entry_price, "stop": stop, "target": target, "exit": exit_price,
                "exit_reason": reason, "r_multiple": r_multiple,
                "bars_held": i - entry_i,
                "bars_to_stop": (i - entry_i) if reason == "stop" else np.nan,
                "bars_to_target": (i - entry_i) if reason == "target" else np.nan,
                "mfe_r": mfe_r, "mae_r": mae_r,
                "sweep_distance": df["sweep_distance"].iat[sig_i],
                "disp_size_atr": df["disp_size_atr"].iat[sig_i],
                "retr_depth_pct": df["retr_depth_pct"].iat[sig_i],
                "bars_sweep_to_entry": df["bars_sweep_to_entry"].iat[sig_i],
                "bars_pierce_to_sweep": df["bars_pierce_to_sweep"].iat[sig_i],
                "asia_high": df["asia_high_sig"].iat[sig_i], "asia_low": df["asia_low_sig"].iat[sig_i],
                "london_high": df["london_high_sig"].iat[sig_i], "london_low": df["london_low_sig"].iat[sig_i],
                "pd_high": df["pd_high_sig"].iat[sig_i], "pd_low": df["pd_low_sig"].iat[sig_i],
                "closed_back_inside_range": True,  # definitional for a confirmed sweep - see notes
            })
            in_trade = False

    return pd.DataFrame(trades)


def stats_block(t):
    if t.empty:
        return {"n": 0}
    wins = t[t["r_multiple"] > 0]
    losses = t[t["r_multiple"] <= 0]
    gross_win = wins["r_multiple"].sum()
    gross_loss = losses["r_multiple"].sum()
    equity = t["r_multiple"].cumsum()
    running_max = equity.cummax()
    max_dd = (equity - running_max).min()
    streak = max_streak = 0
    for r in t["r_multiple"]:
        streak = streak + 1 if r <= 0 else 0
        max_streak = max(max_streak, streak)
    return {
        "n": len(t),
        "win_rate": len(wins) / len(t),
        "avg_winner": wins["r_multiple"].mean() if len(wins) else np.nan,
        "avg_loser": losses["r_multiple"].mean() if len(losses) else np.nan,
        "expectancy": t["r_multiple"].mean(),
        "profit_factor": (gross_win / abs(gross_loss)) if gross_loss != 0 else np.nan,
        "median_r": t["r_multiple"].median(),
        "max_drawdown_r": max_dd,
        "max_losing_streak": max_streak,
        "mfe_mean": t["mfe_r"].mean(), "mfe_median": t["mfe_r"].median(),
        "mae_mean": t["mae_r"].mean(), "mae_median": t["mae_r"].median(),
    }


def print_breakdown(all_trades, by):
    print(f"\n--- by {by} ---")
    rows = []
    for key, g in all_trades.groupby(by):
        rows.append({by: key, **stats_block(g)})
    out = pd.DataFrame(rows).set_index(by)
    with pd.option_context("display.float_format", "{:.3f}".format, "display.width", 160):
        print(out)


if __name__ == "__main__":
    files = sys.argv[1:] if len(sys.argv) > 1 else sorted(glob.glob("*-15m.csv"))
    if not files:
        raise SystemExit("No CSV files found/given. Usage: python3 diagnostic_backtest.py [files...]")

    all_trades = []
    for path in files:
        instrument = path.split("-15m")[0].split("-5m")[0]
        df = load_csv(path)
        df = run_engine(df)
        trades = simulate_with_autopsy(df, instrument)
        if not trades.empty:
            all_trades.append(trades)
        print(f"{instrument}: {len(trades)} trades")

    if not all_trades:
        raise SystemExit("No trades across any file - nothing to autopsy.")

    combined = pd.concat(all_trades, ignore_index=True)
    combined.to_csv("diagnostic_trades_full.csv", index=False)
    print(f"\nFull per-trade log ({len(combined)} rows, every field) written to diagnostic_trades_full.csv")

    print("\n===== OVERALL =====")
    for k, v in stats_block(combined).items():
        print(f"  {k}: {v}")

    for dim in ["instrument", "setup", "direction", "day_of_week", "hour_ct",
                "was_london", "was_london_ny_overlap", "was_ny_open_plus"]:
        print_breakdown(combined, dim)

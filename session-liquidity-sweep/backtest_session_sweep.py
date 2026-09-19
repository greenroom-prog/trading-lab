#!/usr/bin/env python3
"""
backtest_session_sweep.py - standalone backtest, no framework required.

Runs the exact same sweep -> displacement -> retracement engine as the
TradingView/MT5 indicator, directly against a price file, and simulates
trades with a fixed stop (sweep extreme) and target (next untouched
liquidity level).

USAGE
    python3 backtest_session_sweep.py /path/to/DOT_USD-5m.feather
    python3 backtest_session_sweep.py /path/to/some_data.csv

Accepts .feather or .csv. Expects columns: date/time, open, high, low, close
(volume optional) - if your file uses different column names, the script
will print what it found and exit rather than silently guessing wrong.

OUTPUT
    Prints a summary (trade count, win rate, avg R, max losing streak,
    expectancy) and writes a full trade log to <input>_trades.csv next to
    the input file, so you can inspect every single signal by hand.

STATUS: unverified - not run yet. Same sweep/displacement/retracement state
machine already traced against the gold example in the indicator's README.
"""
import sys
import os
import pandas as pd
import numpy as np

ASIA_START, ASIA_END = (19, 0), (4, 0)
LONDON_START, LONDON_END = (2, 0), (11, 0)

SWEEP_RECLAIM_BARS = 5
DISP_LOOKBACK_BARS = 3
DISP_ATR_LEN = 14
DISP_ATR_MULT = 1.5
RETR_MIN_PCT = 0.50
RETR_MAX_PCT = 0.79
STOP_BUFFER_ATR = 0.10


def load_price_file(path: str) -> pd.DataFrame:
    if path.endswith(".feather"):
        df = pd.read_feather(path)
    elif path.endswith(".csv"):
        df = pd.read_csv(path)
    else:
        raise SystemExit(f"Don't know how to read {path} - expected .feather or .csv")

    cols = {c.lower(): c for c in df.columns}
    date_col = cols.get("date") or cols.get("time") or cols.get("timestamp")
    needed = ["open", "high", "low", "close"]
    missing = [c for c in needed if c not in cols]
    if date_col is None or missing:
        print(f"Found columns: {list(df.columns)}")
        raise SystemExit(
            f"Expected a date/time column plus open/high/low/close. "
            f"Missing: {missing + ([] if date_col else ['date/time'])}. "
            f"Edit load_price_file() to match your file's actual column names."
        )

    df = df.rename(columns={date_col: "date", cols["open"]: "open", cols["high"]: "high",
                             cols["low"]: "low", cols["close"]: "close"})
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.sort_values("date").reset_index(drop=True)
    return df


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


def run_sweep_engine(df: pd.DataFrame) -> pd.DataFrame:
    """Same state machine as the Pine/MQL5 versions. Returns df with
    signal_enter_long/short, stop_level, target_level, entry_tag columns."""
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
    sweep_bull = np.zeros(n, bool); sweep_bear = np.zeros(n, bool)
    held_bull = np.zeros(n, bool); held_bear = np.zeros(n, bool)
    enter_long = np.zeros(n, bool); enter_short = np.zeros(n, bool)
    stop_level = np.full(n, np.nan); target_level = np.full(n, np.nan)
    entry_tag = np.array([""] * n, dtype=object)

    def fresh():
        return {"pending": False, "swept": False, "held": False, "disp_done": False,
                "entry_done": False, "invalid": False, "pierce_bar": -1, "extreme": np.nan,
                "disp_extreme": np.nan}

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
                        s["swept"], s["pending"] = True, False
                        sweep_bull[i] = True
                    elif i - s["pierce_bar"] > SWEEP_RECLAIM_BARS:
                        s["held"], s["pending"] = True, False
                        held_bull[i] = True
            if s["swept"] and not s["disp_done"] and not s["invalid"]:
                if i - s["pierce_bar"] <= DISP_LOOKBACK_BARS:
                    if close[i] > open_[i] and not np.isnan(atr[i]) and (close[i] - open_[i]) >= DISP_ATR_MULT * atr[i]:
                        s["disp_done"], s["disp_extreme"] = True, high[i]
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
                        s["swept"], s["pending"] = True, False
                        sweep_bear[i] = True
                    elif i - s["pierce_bar"] > SWEEP_RECLAIM_BARS:
                        s["held"], s["pending"] = True, False
                        held_bear[i] = True
            if s["swept"] and not s["disp_done"] and not s["invalid"]:
                if i - s["pierce_bar"] <= DISP_LOOKBACK_BARS:
                    if close[i] < open_[i] and not np.isnan(atr[i]) and (open_[i] - close[i]) >= DISP_ATR_MULT * atr[i]:
                        s["disp_done"], s["disp_extreme"] = True, low[i]
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
    return df


def simulate_trades(df: pd.DataFrame) -> pd.DataFrame:
    """Walk forward bar by bar. One open trade at a time. A trade exits on
    whichever of stop/target is touched first; if a single bar's range
    contains both, the stop is assumed to hit first (the conservative
    assumption - you cannot know intrabar sequencing from OHLC alone)."""
    # Fill convention: a signal confirmed on bar i's CLOSE is only knowable
    # after that bar closes, so the earliest realistic fill is bar i+1's
    # OPEN - never bar i's own close (that would be the exact mistake the
    # EDGAR README says killed the predecessor's best strategy).
    trades = []
    in_trade = False
    direction = entry_i = entry_price = stop = target = tag = None

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
            continue

        hi, lo = df["high"].iat[i], df["low"].iat[i]
        hit_stop = (lo <= stop) if direction == "long" else (hi >= stop)
        hit_target = (hi >= target) if direction == "long" else (lo <= target)

        if hit_stop or hit_target:
            exit_price = stop if hit_stop else target  # stop wins ties, see docstring
            reason = "stop" if hit_stop else "target"
            risk = abs(entry_price - stop)
            pnl = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
            r_multiple = pnl / risk if risk > 0 else np.nan
            trades.append({
                "entry_date": df["date"].iat[entry_i], "exit_date": df["date"].iat[i],
                "direction": direction, "tag": tag, "entry": entry_price, "stop": stop,
                "target": target, "exit": exit_price, "exit_reason": reason,
                "r_multiple": r_multiple, "bars_held": i - entry_i,
            })
            in_trade = False

    return pd.DataFrame(trades)


def summarize(trades: pd.DataFrame):
    if trades.empty:
        print("No trades were generated. Either the setup never fired on this "
              "data/timeframe, or something upstream is wrong - check that the "
              "'date' column actually parsed as tz-aware UTC and that session "
              "boxes are forming.")
        return
    wins = trades[trades["r_multiple"] > 0]
    losses = trades[trades["r_multiple"] <= 0]
    win_rate = len(wins) / len(trades)
    avg_r = trades["r_multiple"].mean()
    expectancy = win_rate * wins["r_multiple"].mean() if len(wins) else 0
    expectancy += (1 - win_rate) * losses["r_multiple"].mean() if len(losses) else 0

    streak = max_streak = 0
    for r in trades["r_multiple"]:
        streak = streak + 1 if r <= 0 else 0
        max_streak = max(max_streak, streak)

    print(f"Trades:              {len(trades)}")
    print(f"Win rate:            {win_rate:.1%}")
    print(f"Avg R:               {avg_r:+.2f}")
    print(f"Expectancy (R/trade):{expectancy:+.2f}")
    print(f"Max losing streak:   {max_streak}")
    print(f"By setup:")
    print(trades.groupby("tag")["r_multiple"].agg(["count", "mean"]))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(f"Usage: python3 {sys.argv[0]} /path/to/pricefile.feather")

    path = sys.argv[1]
    df = load_price_file(path)
    print(f"Loaded {len(df)} rows, {df['date'].min()} -> {df['date'].max()}")

    df = run_sweep_engine(df)
    trades = simulate_trades(df)

    out_path = os.path.splitext(path)[0] + "_trades.csv"
    trades.to_csv(out_path, index=False)
    print(f"Full trade log written to {out_path}\n")

    summarize(trades)

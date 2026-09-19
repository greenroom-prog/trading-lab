"""
SessionLiquiditySweep - Freqtrade port of session_liquidity_sweep.pine

Same core setups as the TradingView/MT5 indicator, adapted for crypto (24/7
market, so Setup C - the NY cash-open opening range - is dropped rather than
faked; there's no cash open to anchor it to):

  Setup A (traded): sweep of Asia/London high or low -> displacement candle
  (>= N x ATR) -> retracement into a fib zone -> entry, with a stop at the
  sweep extreme and a target at the next untouched opposite-side liquidity
  level (Asia/London opposite extreme, then prior-day extreme).

  Setup B (labeled, not traded by default): the same break, but held instead
  of swept (no reclaim within the window) - tagged in the dataframe so you
  can inspect it, but does not generate a trade signal here. Add one if the
  validation pass on Setup A says continuation is worth trading separately.

STATUS: unverified. This has not been run through `freqtrade backtesting`
from the environment it was written in - no freqtrade instance is available
there. The bar-by-bar sweep/displacement/retracement logic is a direct,
line-by-line port of the already-traced Pine Script version (same variable
names translated to snake_case, same order of operations) rather than a
fresh re-derivation, specifically so a bug fixed once doesn't need
re-discovering per platform. Known, deliberate differences from the Pine
version:

  - ATR here is a simple rolling-mean True Range (see `_atr`), not Wilder's
    smoothed average that Pine's ta.atr() uses. Close enough for a first
    pass; swap in `talib.abstract.ATR` if TA-Lib is installed and you want
    an exact match.
  - Setup C is not ported (no NY cash open in crypto).
  - Sessions are computed in Central Time by converting Freqtrade's UTC
    'date' column with pandas' real IANA timezone database - this is more
    reliable than the manual UTC-offset arithmetic the MT5 version needed,
    since Freqtrade/pandas always store OHLCV timestamps in UTC.

Run it, read the errors, fix them - same as the other two files.
"""
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd

from freqtrade.strategy import IStrategy, IntParameter, DecimalParameter
from freqtrade.persistence import Trade


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _atr(df: pd.DataFrame, length: int) -> pd.Series:
    return _true_range(df).rolling(length, min_periods=length).mean()


def _in_session(minute_of_day: np.ndarray, start_h: int, start_m: int, end_h: int, end_m: int) -> np.ndarray:
    s = start_h * 60 + start_m
    e = end_h * 60 + end_m
    if s <= e:
        return (minute_of_day >= s) & (minute_of_day < e)
    return (minute_of_day >= s) | (minute_of_day < e)


class SessionLiquiditySweep(IStrategy):
    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = True
    use_exit_signal = True
    use_custom_stoploss = True
    exit_profit_only = False
    process_only_new_candles = True
    startup_candle_count = 400

    stoploss = -0.15
    minimal_roi = {"0": 10}

    sweep_reclaim_bars = IntParameter(2, 15, default=5, space="buy")
    disp_lookback_bars = IntParameter(1, 10, default=3, space="buy")
    disp_atr_mult = DecimalParameter(0.5, 3.0, default=1.5, space="buy", decimals=2)
    retr_min_pct = DecimalParameter(0.30, 0.70, default=0.50, space="buy", decimals=2)
    retr_max_pct = DecimalParameter(0.60, 0.95, default=0.79, space="buy", decimals=2)
    stop_buffer_atr = DecimalParameter(0.0, 0.5, default=0.10, space="buy", decimals=2)
    disp_atr_len = 14

    ASIA_START, ASIA_END = (19, 0), (4, 0)
    LONDON_START, LONDON_END = (2, 0), (11, 0)

    plot_config = {
        "main_plot": {
            "asia_high": {"color": "blue"},
            "asia_low": {"color": "blue"},
            "london_high": {"color": "orange"},
            "london_low": {"color": "orange"},
            "pd_high": {"color": "gray"},
            "pd_low": {"color": "gray"},
        },
        "subplots": {
            "sweep": {
                "sweep_bull": {"color": "green", "type": "bar"},
                "sweep_bear": {"color": "red", "type": "bar"},
            }
        },
    }

    def populate_indicators(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        df = dataframe.copy()
        df["atr"] = _atr(df, self.disp_atr_len)

        ct = df["date"].dt.tz_convert("America/Chicago")
        minute_of_day = (ct.dt.hour * 60 + ct.dt.minute).to_numpy()
        calendar_day = ct.dt.date.to_numpy()

        asia_session = _in_session(minute_of_day, *self.ASIA_START, *self.ASIA_END)
        london_session = _in_session(minute_of_day, *self.LONDON_START, *self.LONDON_END)

        tmp = pd.DataFrame({"day": calendar_day, "high": df["high"], "low": df["low"]})
        daily_high = tmp.groupby("day")["high"].max()
        daily_low = tmp.groupby("day")["low"].min()
        prev_daily_high = daily_high.shift(1)
        prev_daily_low = daily_low.shift(1)
        df["pd_high"] = pd.Series(calendar_day).map(prev_daily_high).to_numpy()
        df["pd_low"] = pd.Series(calendar_day).map(prev_daily_low).to_numpy()

        n = len(df)
        high = df["high"].to_numpy()
        low = df["low"].to_numpy()
        close = df["close"].to_numpy()
        open_ = df["open"].to_numpy()
        atr = df["atr"].to_numpy()

        asia_high = np.full(n, np.nan)
        asia_low = np.full(n, np.nan)
        london_high = np.full(n, np.nan)
        london_low = np.full(n, np.nan)

        sweep_bull = np.zeros(n, dtype=bool)
        sweep_bear = np.zeros(n, dtype=bool)
        held_bull = np.zeros(n, dtype=bool)
        held_bear = np.zeros(n, dtype=bool)
        enter_long = np.zeros(n, dtype=bool)
        enter_short = np.zeros(n, dtype=bool)
        stop_level = np.full(n, np.nan)
        target_level = np.full(n, np.nan)
        entry_tag = np.array([""] * n, dtype=object)

        sweep_min = int(self.sweep_reclaim_bars.value)
        disp_lookback = int(self.disp_lookback_bars.value)
        disp_mult = float(self.disp_atr_mult.value)
        retr_min = float(self.retr_min_pct.value)
        retr_max = float(self.retr_max_pct.value)
        stop_buf = float(self.stop_buffer_atr.value)

        def fresh_state():
            return {
                "pending": False, "swept": False, "held": False,
                "disp_done": False, "entry_done": False, "invalid": False,
                "pierce_bar": -1, "extreme": np.nan,
                "disp_bar": -1, "disp_extreme": np.nan,
            }

        al = fresh_state()
        ah = fresh_state()
        ll = fresh_state()
        lh = fresh_state()

        cur_asia_high = np.nan
        cur_asia_low = np.nan
        cur_london_high = np.nan
        cur_london_low = np.nan

        for i in range(n):
            new_asia = asia_session[i] and (i == 0 or not asia_session[i - 1])
            new_london = london_session[i] and (i == 0 or not london_session[i - 1])

            if new_asia:
                cur_asia_high, cur_asia_low = high[i], low[i]
                al, ah = fresh_state(), fresh_state()
            elif asia_session[i]:
                cur_asia_high = max(cur_asia_high, high[i])
                cur_asia_low = min(cur_asia_low, low[i])
            asia_high[i], asia_low[i] = cur_asia_high, cur_asia_low

            if new_london:
                cur_london_high, cur_london_low = high[i], low[i]
                ll, lh = fresh_state(), fresh_state()
            elif london_session[i]:
                cur_london_high = max(cur_london_high, high[i])
                cur_london_low = min(cur_london_low, low[i])
            london_high[i], london_low[i] = cur_london_high, cur_london_low

            def process_bullish(level, level_ready, s, tag):
                if not level_ready or np.isnan(level):
                    return
                if not s["swept"] and not s["held"] and not s["invalid"]:
                    if not s["pending"] and low[i] < level:
                        s["pending"] = True
                        s["pierce_bar"] = i
                        s["extreme"] = low[i]
                    elif s["pending"]:
                        s["extreme"] = min(s["extreme"], low[i])
                        if close[i] > level:
                            s["swept"] = True
                            s["pending"] = False
                            sweep_bull[i] = True
                        elif i - s["pierce_bar"] > sweep_min:
                            s["held"] = True
                            s["pending"] = False
                            held_bull[i] = True

                if s["swept"] and not s["disp_done"] and not s["invalid"]:
                    bars_since = i - s["pierce_bar"]
                    if bars_since <= disp_lookback:
                        body_ok = close[i] > open_[i] and not np.isnan(atr[i]) and (close[i] - open_[i]) >= disp_mult * atr[i]
                        if body_ok:
                            s["disp_done"] = True
                            s["disp_bar"] = i
                            s["disp_extreme"] = high[i]
                    else:
                        s["invalid"] = True

                if s["disp_done"] and not s["entry_done"] and not s["invalid"]:
                    leg_high, leg_low = s["disp_extreme"], s["extreme"]
                    zone_upper = leg_high - (leg_high - leg_low) * retr_min
                    if low[i] < leg_low:
                        s["invalid"] = True
                    elif low[i] <= zone_upper:
                        s["entry_done"] = True
                        enter_long[i] = True
                        stop_level[i] = leg_low - stop_buf * (atr[i] if not np.isnan(atr[i]) else 0.0)
                        entry_tag[i] = f"sweep_{tag}"

            process_bullish(asia_low[i], not asia_session[i], al, "asia_low")
            process_bullish(london_low[i], not london_session[i], ll, "london_low")

            def process_bearish(level, level_ready, s, tag):
                if not level_ready or np.isnan(level):
                    return
                if not s["swept"] and not s["held"] and not s["invalid"]:
                    if not s["pending"] and high[i] > level:
                        s["pending"] = True
                        s["pierce_bar"] = i
                        s["extreme"] = high[i]
                    elif s["pending"]:
                        s["extreme"] = max(s["extreme"], high[i])
                        if close[i] < level:
                            s["swept"] = True
                            s["pending"] = False
                            sweep_bear[i] = True
                        elif i - s["pierce_bar"] > sweep_min:
                            s["held"] = True
                            s["pending"] = False
                            held_bear[i] = True

                if s["swept"] and not s["disp_done"] and not s["invalid"]:
                    bars_since = i - s["pierce_bar"]
                    if bars_since <= disp_lookback:
                        body_ok = close[i] < open_[i] and not np.isnan(atr[i]) and (open_[i] - close[i]) >= disp_mult * atr[i]
                        if body_ok:
                            s["disp_done"] = True
                            s["disp_bar"] = i
                            s["disp_extreme"] = low[i]
                    else:
                        s["invalid"] = True

                if s["disp_done"] and not s["entry_done"] and not s["invalid"]:
                    leg_low, leg_high = s["disp_extreme"], s["extreme"]
                    zone_lower = leg_low + (leg_high - leg_low) * retr_min
                    if high[i] > leg_high:
                        s["invalid"] = True
                    elif high[i] >= zone_lower:
                        s["entry_done"] = True
                        enter_short[i] = True
                        stop_level[i] = leg_high + stop_buf * (atr[i] if not np.isnan(atr[i]) else 0.0)
                        entry_tag[i] = f"sweep_{tag}"

            process_bearish(asia_high[i], not asia_session[i], ah, "asia_high")
            process_bearish(london_high[i], not london_session[i], lh, "london_high")

            if enter_long[i]:
                candidates = [lv for lv in (asia_high[i], london_high[i], df["pd_high"].iat[i]) if not np.isnan(lv) and lv > close[i]]
                target_level[i] = min(candidates) if candidates else close[i] + 2 * (close[i] - stop_level[i])
            if enter_short[i]:
                candidates = [lv for lv in (asia_low[i], london_low[i], df["pd_low"].iat[i]) if not np.isnan(lv) and lv < close[i]]
                target_level[i] = max(candidates) if candidates else close[i] - 2 * (stop_level[i] - close[i])

        df["asia_high"], df["asia_low"] = asia_high, asia_low
        df["london_high"], df["london_low"] = london_high, london_low
        df["sweep_bull"], df["sweep_bear"] = sweep_bull, sweep_bear
        df["held_bull"], df["held_bear"] = held_bull, held_bear
        df["signal_enter_long"], df["signal_enter_short"] = enter_long, enter_short
        df["stop_level"], df["target_level"] = stop_level, target_level
        df["entry_tag"] = entry_tag
        return df

    def populate_entry_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        dataframe["enter_long"] = 0
        dataframe["enter_short"] = 0
        dataframe["enter_tag"] = ""

        long_mask = dataframe["signal_enter_long"]
        short_mask = dataframe["signal_enter_short"]
        dataframe.loc[long_mask, "enter_long"] = 1
        dataframe.loc[long_mask, "enter_tag"] = dataframe.loc[long_mask, "entry_tag"]
        dataframe.loc[short_mask, "enter_short"] = 1
        dataframe.loc[short_mask, "enter_tag"] = dataframe.loc[short_mask, "entry_tag"]
        return dataframe

    def populate_exit_trend(self, dataframe: pd.DataFrame, metadata: dict) -> pd.DataFrame:
        return dataframe

    def _entry_row(self, pair: str, open_date_utc: datetime):
        df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
        if df is None or df.empty:
            return None
        idx = df["date"].searchsorted(open_date_utc, side="right") - 1
        if idx < 0 or idx >= len(df):
            return None
        return df.iloc[idx]

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                         current_rate: float, current_profit: float, **kwargs) -> Optional[float]:
        row = self._entry_row(pair, trade.open_date_utc)
        if row is None or pd.isna(row.get("stop_level")):
            return None
        stop_price = float(row["stop_level"])
        if trade.is_short:
            return -(stop_price - current_rate) / current_rate
        return (stop_price / current_rate) - 1

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime,
                     current_rate: float, current_profit: float, **kwargs):
        row = self._entry_row(pair, trade.open_date_utc)
        if row is None or pd.isna(row.get("target_level")):
            return None
        target = float(row["target_level"])
        if not trade.is_short and current_rate >= target:
            return "target_hit"
        if trade.is_short and current_rate <= target:
            return "target_hit"
        return None

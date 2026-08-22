"""
C-011 BACKTEST — FOR EXTERNAL AUDIT
====================================

Please attack this code. I believe the strategy is dead, but I want to know
whether my TESTING is wrong, not just my strategy.

STRATEGY
--------
Universe: SPY only.
Baseline exposure 1.0x, always invested.
  - After TWO consecutive down days, and the Fed funds rate has NOT risen
    more than 0.10 over the prior 63 calendar days -> 1.5x
  - After any up day greater than +1% -> 0.5x
  - Otherwise -> 1.0x

WHAT I ALREADY BELIEVE I FOUND
------------------------------
1. Using daily close-to-close returns with the signal computed from the same
   close, the strategy returns ~1834% (2000-2026) vs ~743% buy-and-hold.
2. That result is an artifact. Shifting to open-to-open execution collapses it
   to 767% vs 740% buy-and-hold — i.e. no edge.
3. On minute data (Aug 2024 - Aug 2026), entering at 15:55 instead of the
   official close costs 0.3 bps on average but 7.05 bps in ABSOLUTE terms.
   You pay the absolute, not the mean. That kills it: 1.25x returns 27.9%,
   1.5x returns 31.1%, vs buy-and-hold 36.8%.
4. The per-trade signal itself IS real: the overnight gap after two down days
   averages 21.4 bps (entering 15:55) vs 5.72 bps on all days, n=80.

QUESTIONS FOR YOU
-----------------
Q1. Is there a bug in this code that makes the DEAD result wrong — i.e. am I
    killing a strategy that actually works?
Q2. Is there a bug that made the ORIGINAL result look good — beyond the
    execution issue I already found?
Q3. Is my slippage treatment right? I use mean absolute deviation between the
    15:55 price and the close as a per-adjustment cost. Is that too harsh,
    too lenient, or the wrong model entirely?
Q4. The FRED DFF series is published with a lag. I shift it 2 business days.
    Is that enough? Should I use the target rate instead of the effective rate?
Q5. Given the finding in (4) — a real 21.4 bps overnight signal that I cannot
    harvest because repositioning costs 7 bps each way — is there a structure
    that DOES harvest it? Options? Futures? Something I am not seeing?

DATA SOURCES
------------
- SPY daily: yfinance, auto-adjusted close.
- SPY minute: Polygon/Massive, 2024-08 to 2026-08, regular + extended hours.
- Fed funds: FRED series DFF.
"""
import os
import datetime as dt

import numpy as np
import pandas as pd
import yfinance as yf
from dotenv import load_dotenv
from fredapi import Fred

load_dotenv()


# ----------------------------------------------------------------------
# TEST 1 — daily bars, close-to-close (the version I believe is WRONG)
# ----------------------------------------------------------------------
def test_daily_close_to_close():
    ff = Fred(api_key=os.getenv("FRED_KEY")).get_series("DFF", "2000-01-01")
    spy = yf.download("SPY", start="2000-01-01", progress=False)["Close"].squeeze()
    r = spy.pct_change().dropna()

    # Fed series shifted 2 business days for publication lag
    rate = ff.reindex(r.index).ffill().shift(2)
    tightening = (rate > rate.shift(63) + 0.1).fillna(False)

    neg = r < 0
    down_signal = (neg.shift(1) & neg.shift(2) & ~tightening).fillna(False)
    up_signal = (r.shift(1) > 0.01).fillna(False)

    w = pd.Series(1.0, index=r.index)
    w[down_signal] = 1.5
    w[up_signal] = 0.5

    turnover_cost = w.diff().abs().fillna(0) * 0.0002
    margin_cost = (w - 1).clip(lower=0) * 0.04 / 252
    net = r * w - turnover_cost - margin_cost

    eq = (1 + net).cumprod()
    print("TEST 1  close-to-close")
    print(f"  strategy    {eq.iloc[-1] - 1:,.2f}")
    print(f"  buy & hold  {(1 + r).prod() - 1:,.2f}")
    print(f"  max DD      {(eq / eq.cummax() - 1).min():.3f}")


# ----------------------------------------------------------------------
# TEST 2 — daily bars, open-to-open (the version I believe is RIGHT)
# ----------------------------------------------------------------------
def test_daily_open_to_open():
    ff = Fred(api_key=os.getenv("FRED_KEY")).get_series("DFF", "2000-01-01")
    d = yf.download("SPY", start="2000-01-01", progress=False)
    c, o = d["Close"].squeeze(), d["Open"].squeeze()
    r = c.pct_change().dropna()

    rate = ff.reindex(r.index).ffill().shift(2)
    tightening = (rate > rate.shift(63) + 0.1).fillna(False)

    neg = r < 0
    down_signal = (neg.shift(1) & neg.shift(2) & ~tightening).fillna(False)
    up_signal = (r.shift(1) > 0.01).fillna(False)

    w = pd.Series(1.0, index=r.index)
    w[down_signal] = 1.5
    w[up_signal] = 0.5

    # signal known at close t -> position held from open t+1 to open t+2
    w_lagged = w.shift(1).fillna(1.0)
    open_to_open = (o.shift(-1) / o - 1).reindex(r.index)

    net = (open_to_open * w_lagged
           - w_lagged.diff().abs().fillna(0) * 0.0002
           - (w_lagged - 1).clip(lower=0) * 0.04 / 252)

    print("TEST 2  open-to-open")
    print(f"  strategy    {(1 + net.dropna()).prod() - 1:,.2f}")
    print(f"  buy & hold  {(1 + r).prod() - 1:,.2f}")


# ----------------------------------------------------------------------
# TEST 3 — minute bars, 15:55 entry (most realistic version I could build)
# ----------------------------------------------------------------------
def test_minute_1555():
    d = pd.read_parquet("data/raw/spy_1min.parquet")
    d["date"] = d.ts.dt.date
    d["t"] = d.ts.dt.time

    rth = d[(d.t >= dt.time(9, 30)) & (d.t <= dt.time(16, 0))]
    daily = rth.groupby("date").agg(o=("open", "first"), c=("close", "last"))
    p1555 = d[d.t == dt.time(15, 55)].set_index("date").close.rename("p1555")
    df = daily.join(p1555).dropna()

    r = df.c.pct_change()
    neg = r < 0
    down_signal = (neg.shift(1) & neg.shift(2)).fillna(False)   # no Fed gate: window too short
    up_signal = (r.shift(1) > 0.01).fillna(False)

    # slippage = mean ABSOLUTE deviation of the 15:55 price from the close
    slip = (df.c / df.p1555 - 1).abs().mean()
    print(f"TEST 3  minute bars, 15:55 entry   (slippage {slip*10000:.2f} bps)")

    for lev in (1.25, 1.5, 2.0):
        w = pd.Series(1.0, index=df.index)
        w[down_signal] = lev
        w[up_signal] = 0.5
        net = (r * w
               - w.diff().abs().fillna(0) * (0.0002 + slip)
               - (w - 1).clip(lower=0) * 0.05 / 252)
        print(f"  {lev}x        {(1 + net.fillna(0)).prod() - 1:.4f}")
    print(f"  buy & hold  {(1 + r.fillna(0)).prod() - 1:.4f}")

    # the raw signal, unlevered — is the overnight edge itself real?
    entry_gap = df.o / df.p1555.shift(1) - 1
    print(f"  overnight gap after 2 down days: "
          f"{entry_gap[down_signal].mean()*10000:.2f} bps  (n={int(down_signal.sum())})")
    print(f"  overnight gap all days:          "
          f"{entry_gap.mean()*10000:.2f} bps")


if __name__ == "__main__":
    test_daily_close_to_close()
    print()
    test_daily_open_to_open()
    print()
    test_minute_1555()

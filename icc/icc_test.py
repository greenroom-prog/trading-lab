#!/usr/bin/env python3
"""
icc_test.py — tests the ICC / market-structure-shift claim cluster.

Claims under test (from data/claims/, C1-C6):
  C3  trend = strict HH/HL sequence on body closes
  C1  structure marked on body CLOSES, not wicks
  C4  entry = 1h structure shift aligned with 4h trend
  C2  session filter: London + New York only
  C6  1h/4h beats 15m

Method:
  - Swing points from body closes (C1), lookback is the free parameter.
  - 4h trend must be HH/HL (or LH/LL) over the lookback window.
  - On 1h, inside a 4h correction, entry fires when a structure shift
    completes IN THE DIRECTION OF THE 4H TREND.
  - Stop: below the higher low that formed the shift (his own
    invalidation rule). Structural, so it self-scales per instrument.
  - Exits tested as a grid: 2R, 3R, trailing-below-last-HL, time exit.
    He never states one. We do not guess; we measure.

EXECUTION DISCIPLINE — the rule that killed the predecessor strategy:
  Signal is computed on bar t's CLOSE. Fill happens at bar t+1's OPEN.
  Never at t's close. That price is not knowable when the signal fires.

Benchmark: random entry, same instrument, same session filter, same
count, same holding distribution. An edge that does not beat that is
not an edge — it is exposure.

Usage:
    python3 icc_test.py --symbol GC=F
    python3 icc_test.py --symbol GC=F --lookback 5 --entry-tf 15m
    python3 icc_test.py --all
"""

import argparse
import warnings

import numpy as np
import pandas as pd
import yfinance as yf

warnings.filterwarnings("ignore")

SYMBOLS = {
    "gold": "GC=F",
    "spy": "SPY",
    "btc": "BTC-USD",
    "us30": "YM=F",     # Dow futures. ^DJI has no intraday.
}

# Session windows in UTC. C2 says out-of-session is a no-go but never
# names the sessions; London+NY taken from context (he cites both).
LONDON = (7, 16)
NEWYORK = (12, 21)


def fetch(symbol, interval, period):
    df = yf.download(symbol, interval=interval, period=period,
                     progress=False, auto_adjust=False)
    if df.empty:
        raise SystemExit(f"no data for {symbol} @ {interval}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[["Open", "High", "Low", "Close"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df


def in_session(idx):
    h = idx.hour
    return ((h >= LONDON[0]) & (h < LONDON[1])) | \
           ((h >= NEWYORK[0]) & (h < NEWYORK[1]))


def swings(close, lb):
    """Swing highs/lows on CLOSES only — C1. A swing high is a close
    higher than lb closes either side. Confirmed lb bars late, which is
    correct: 'you never see structure until it's done.'"""
    hi = np.zeros(len(close), dtype=bool)
    lo = np.zeros(len(close), dtype=bool)
    c = close.values
    for i in range(lb, len(c) - lb):
        w = c[i - lb:i + lb + 1]
        if c[i] == w.max() and (w == c[i]).sum() == 1:
            hi[i] = True
        if c[i] == w.min() and (w == c[i]).sum() == 1:
            lo[i] = True
    return hi, lo


def trend_state(close, lb):
    """+1 uptrend (HH and HL), -1 downtrend (LH and LL), 0 neither.
    C3: 'anything that breaks these rules means it is not a trend.'
    Shifted by lb so state is only known once the swing is confirmed —
    no look-ahead."""
    hi, lo = swings(close, lb)
    c = close.values
    state = np.zeros(len(c))
    last_h = last_l = prev_h = prev_l = np.nan
    for i in range(len(c)):
        if hi[i]:
            prev_h, last_h = last_h, c[i]
        if lo[i]:
            prev_l, last_l = last_l, c[i]
        if not any(np.isnan(x) for x in (last_h, prev_h, last_l, prev_l)):
            if last_h > prev_h and last_l > prev_l:
                state[i] = 1
            elif last_h < prev_h and last_l < prev_l:
                state[i] = -1
    s = pd.Series(state, index=close.index).shift(lb)
    return s.fillna(0)


def entries(htf_close, ltf, lb):
    """C4: 1h structure shift in the direction of the confirmed 4h trend.
    The shift is the moment ltf trend_state flips INTO agreement with
    the htf trend after having been against it (the correction)."""
    htf_state = trend_state(htf_close, lb)
    ltf_state = trend_state(ltf["Close"], lb)

    aligned = htf_state.reindex(ltf.index, method="ffill")
    prev = ltf_state.shift(1)

    long = (aligned == 1) & (ltf_state == 1) & (prev != 1)
    short = (aligned == -1) & (ltf_state == -1) & (prev != -1)

    sess = pd.Series(in_session(ltf.index), index=ltf.index)  # C2
    return (long & sess).values, (short & sess).values


def run(entries_long, entries_short, ltf, lb, exit_mode, rr=2.0, hold=24):
    """Fills at NEXT bar open. Structural stop below the swing low that
    preceded entry."""
    o, h, l, c = (ltf[x].values for x in ("Open", "High", "Low", "Close"))
    _, lo = swings(ltf["Close"], lb)
    trades = []

    for i in range(lb, len(c) - 1):
        direction = 1 if entries_long[i] else (-1 if entries_short[i] else 0)
        if direction == 0:
            continue

        fill = o[i + 1]                       # NEXT bar open. Non-negotiable.

        # structural stop: most recent confirmed swing extreme
        window = slice(max(0, i - 50), i + 1)
        if direction == 1:
            cand = ltf["Close"].values[window][lo[window]]
            stop = cand[-1] if len(cand) else l[window].min()
        else:
            hi_, _ = swings(ltf["Close"], lb)
            cand = ltf["Close"].values[window][hi_[window]]
            stop = cand[-1] if len(cand) else h[window].max()

        risk = abs(fill - stop)
        if risk <= 0 or not np.isfinite(risk):
            continue
        target = fill + direction * risk * rr

        exit_px, bars = None, 0
        trail = stop
        for j in range(i + 1, min(i + 1 + hold, len(c))):
            bars = j - i
            if direction == 1:
                if l[j] <= trail:
                    exit_px = trail
                    break
                if exit_mode in ("rr",) and h[j] >= target:
                    exit_px = target
                    break
                if exit_mode == "trail" and lo[j]:
                    trail = max(trail, c[j])
            else:
                if h[j] >= trail:
                    exit_px = trail
                    break
                if exit_mode in ("rr",) and l[j] <= target:
                    exit_px = target
                    break
                if exit_mode == "trail" and hi_[j] if exit_mode == "trail" else False:
                    trail = min(trail, c[j])
        if exit_px is None:
            exit_px = c[min(i + hold, len(c) - 1)]

        trades.append({
            "r": direction * (exit_px - fill) / risk,
            "bars": bars,
            "dir": direction,
        })
    return pd.DataFrame(trades)


def random_benchmark(ltf, n, lb, exit_mode, rr, hold, seed=0):
    """Same session filter, same trade count, random timing. This is the
    only comparison that matters — beating buy-and-hold is not the test."""
    rng = np.random.default_rng(seed)
    sess = np.where(in_session(ltf.index))[0]
    sess = sess[(sess > lb) & (sess < len(ltf) - hold - 1)]
    rs = []
    for _ in range(200):
        picks = rng.choice(sess, size=min(n, len(sess)), replace=False)
        el = np.zeros(len(ltf), dtype=bool)
        es = np.zeros(len(ltf), dtype=bool)
        for p in picks:
            (el if rng.random() < 0.5 else es)[p] = True
        t = run(el, es, ltf, lb, exit_mode, rr, hold)
        if len(t):
            rs.append(t["r"].mean())
    return np.array(rs)


def report(sym, lb, entry_tf, exit_mode, rr, hold):
    htf = fetch(sym, "1h", "730d")
    htf4 = htf.resample("4h").agg({"Open": "first", "High": "max",
                                   "Low": "min", "Close": "last"}).dropna()
    ltf = fetch(sym, entry_tf, "60d" if entry_tf == "15m" else "730d")

    el, es = entries(htf4["Close"], ltf, lb)
    t = run(el, es, ltf, lb, exit_mode, rr, hold)

    print(f"\n{'=' * 58}")
    print(f"  {sym}  entry_tf={entry_tf}  lookback={lb}  exit={exit_mode}"
          f"{f'@{rr}R' if exit_mode == 'rr' else ''}")
    print(f"{'=' * 58}")
    if len(t) < 20:
        print(f"  only {len(t)} trades — insufficient. widen period or lookback.")
        return
    mean_r = t["r"].mean()
    wr = (t["r"] > 0).mean()
    print(f"  trades        {len(t)}")
    print(f"  win rate      {wr:.1%}")
    print(f"  mean R        {mean_r:+.3f}")
    print(f"  total R       {t['r'].sum():+.1f}")
    print(f"  median bars   {t['bars'].median():.0f}")

    bench = random_benchmark(ltf, len(t), lb, exit_mode, rr, hold)
    if len(bench):
        pct = (bench < mean_r).mean()
        print(f"\n  random entry  {bench.mean():+.3f} mean R "
              f"(sd {bench.std():.3f}, n=200)")
        print(f"  percentile    {pct:.1%}")
        verdict = ("SURVIVES — beats random at p<0.05" if pct > 0.95
                   else "KILLED — indistinguishable from random entry")
        print(f"\n  {verdict}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbol", default="GC=F")
    ap.add_argument("--lookback", type=int, default=3)
    ap.add_argument("--entry-tf", default="1h")
    ap.add_argument("--exit", default="rr", choices=["rr", "trail", "time"])
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--hold", type=int, default=24)
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    syms = list(SYMBOLS.values()) if a.all else [a.symbol]
    for s in syms:
        try:
            report(s, a.lookback, a.entry_tf, a.exit, a.rr, a.hold)
        except SystemExit as e:
            print(f"\n  {s}: {e}")


if __name__ == "__main__":
    main()

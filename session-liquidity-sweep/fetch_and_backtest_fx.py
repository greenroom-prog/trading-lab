#!/usr/bin/env python3
"""
fetch_and_backtest_fx.py - pull real gold/FX/index data and backtest all 7
instruments from the actual plan in one run.

Data source: Yahoo Finance (via the `yfinance` package), free, no API key.
KNOWN LIMITATION, stated up front: Yahoo only serves ~60 days of intraday
(sub-daily) history. This run will therefore have far fewer than the
100-200 samples per setup/instrument the validation plan calls for - it's
a first look at whether the setup fires and roughly how it behaves, not a
final answer. Getting to real sample size needs a data vendor with deeper
intraday history (a broker export, Polygon, Dukascopy tick data, etc).

USAGE
    python3 fetch_and_backtest_fx.py            # 15m candles, 60 days
    python3 fetch_and_backtest_fx.py 5m 60
"""
import sys
import time
import pandas as pd
import yfinance as yf

sys.path.insert(0, ".")
from backtest_session_sweep import run_sweep_engine, simulate_trades, summarize

INTERVAL = sys.argv[1] if len(sys.argv) > 1 else "15m"
PERIOD_DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 60

INSTRUMENTS = {
    "GOLD_XAUUSD": "GC=F",
    "YEN_USDJPY": "JPY=X",
    "EURO_EURUSD": "EURUSD=X",
    "POUND_GBPUSD": "GBPUSD=X",
    "AUSSIE_AUDUSD": "AUDUSD=X",
    "SP500_ES": "ES=F",
    "DOW_YM": "YM=F",
}

all_trades = []

for name, ticker in INSTRUMENTS.items():
    print(f"\n===== {name} ({ticker}) =====")
    try:
        raw = yf.download(ticker, period=f"{PERIOD_DAYS}d", interval=INTERVAL,
                           progress=False, auto_adjust=False)
    except Exception as e:
        print(f"  download failed: {e}")
        continue

    if raw.empty:
        print("  no data returned - ticker may be wrong or interval unsupported for this symbol")
        continue

    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)

    df = raw.reset_index().rename(columns={raw.index.name or "Datetime": "date"})
    df.columns = [c.lower() for c in df.columns]
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df[["date", "open", "high", "low", "close"]].dropna().sort_values("date").reset_index(drop=True)

    csv_path = f"{name}-{INTERVAL}.csv"
    df.to_csv(csv_path, index=False)
    print(f"  {len(df)} rows, {df['date'].min()} -> {df['date'].max()} (saved to {csv_path})")

    if len(df) < 200:
        print("  too little data to bother running the engine on - skipping")
        continue

    df = run_sweep_engine(df)
    trades = simulate_trades(df)
    if not trades.empty:
        trades["instrument"] = name
        all_trades.append(trades)
        trades.to_csv(f"{name}-{INTERVAL}_trades.csv", index=False)
    summarize(trades)
    time.sleep(1)

if all_trades:
    combined = pd.concat(all_trades, ignore_index=True)
    combined.to_csv(f"ALL_INSTRUMENTS-{INTERVAL}_trades.csv", index=False)
    print(f"\n===== COMBINED ACROSS ALL {len(all_trades)} INSTRUMENTS =====")
    summarize(combined)
    print("\nBy instrument:")
    print(combined.groupby("instrument")["r_multiple"].agg(["count", "mean"]))
else:
    print("\nNo trades were generated on any instrument in this window.")

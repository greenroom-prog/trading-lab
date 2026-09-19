#!/usr/bin/env python3
"""
fetch_dukascopy.py - pull deep intraday history (years, not ~60 days) for
the actual 7 instruments, free, no account, no API key.

Replaces fetch_and_backtest_fx.py's Yahoo pull with Dukascopy, which has
15m bars natively (no resampling needed) and, for the indices, real
E-mini futures proxies (E_SandP-500, E_D&J-Ind) instead of Yahoo's
ES=F/YM=F or a generic CFD.

Output format matches the earlier Yahoo CSVs exactly (date,open,high,low,close)
so every existing script (backtest_session_sweep.py, diagnostic_backtest.py,
excursion_analysis.py, path_analysis.py, model_comparison.py,
per_instrument_robustness.py) runs against these files unchanged.

USAGE
    python3 fetch_dukascopy.py            # 15m bars, 2 years back
    python3 fetch_dukascopy.py 730        # interval fixed at 15m, N days back
"""
import sys
import time as time_mod
from datetime import datetime, timedelta

import pandas as pd
import dukascopy_python
from dukascopy_python.instruments import (
    INSTRUMENT_FX_MAJORS_AUD_USD,
    INSTRUMENT_FX_MAJORS_EUR_USD,
    INSTRUMENT_FX_MAJORS_GBP_USD,
    INSTRUMENT_FX_MAJORS_USD_JPY,
    INSTRUMENT_FX_METALS_XAU_USD,
    INSTRUMENT_IDX_AMERICA_E_SANDP_500,
    INSTRUMENT_IDX_AMERICA_E_D_J_IND,
)

DAYS_BACK = int(sys.argv[1]) if len(sys.argv) > 1 else 730  # ~2 years

INSTRUMENTS = {
    "GOLD_XAUUSD": INSTRUMENT_FX_METALS_XAU_USD,
    "YEN_USDJPY": INSTRUMENT_FX_MAJORS_USD_JPY,
    "EURO_EURUSD": INSTRUMENT_FX_MAJORS_EUR_USD,
    "POUND_GBPUSD": INSTRUMENT_FX_MAJORS_GBP_USD,
    "AUSSIE_AUDUSD": INSTRUMENT_FX_MAJORS_AUD_USD,
    "SP500_ES": INSTRUMENT_IDX_AMERICA_E_SANDP_500,   # E-mini S&P 500 proxy, not a generic CFD
    "DOW_YM": INSTRUMENT_IDX_AMERICA_E_D_J_IND,        # E-mini Dow proxy
}

end = datetime.utcnow()
start = end - timedelta(days=DAYS_BACK)

for name, symbol in INSTRUMENTS.items():
    print(f"\n===== {name} ({symbol}) =====")
    try:
        df = dukascopy_python.fetch(
            symbol,
            dukascopy_python.INTERVAL_MIN_15,
            dukascopy_python.OFFER_SIDE_BID,
            start, end,
        )
    except Exception as e:
        print(f"  fetch failed: {e}")
        continue

    if df.empty:
        print("  no data returned - check the instrument code or date range")
        continue

    df = df.reset_index().rename(columns={"timestamp": "date"})
    df.columns = [c.lower() for c in df.columns]
    keep = ["date", "open", "high", "low", "close"]
    missing = [c for c in keep if c not in df.columns]
    if missing:
        print(f"  unexpected columns, got {list(df.columns)} - skipping")
        continue
    df = df[keep].dropna().sort_values("date").reset_index(drop=True)

    csv_path = f"{name}-15m.csv"
    df.to_csv(csv_path, index=False)
    print(f"  {len(df)} rows, {df['date'].min()} -> {df['date'].max()} (saved to {csv_path})")
    time_mod.sleep(1)  # be polite between symbols

print("\nDone. These files overwrite the old ~60-day Yahoo CSVs of the same name -")
print("everything downstream (diagnostic_backtest.py, excursion_analysis.py, path_analysis.py,")
print("model_comparison.py, per_instrument_robustness.py) reads *-15m.csv, so just re-run them.")

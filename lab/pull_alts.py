"""Pull hourly OHLCV for a basket of alts from Coinbase.
Run from ~/trading-lab:  python3 lab/pull_alts.py
"""
import time
import ccxt
import pandas as pd

SYMBOLS = ['SOL', 'XRP', 'ETH', 'SUI', 'XLM', 'HBAR', 'ONDO',
           'LINK', 'AVAX', 'DOGE', 'ADA', 'DOT', 'ALGO']
START = '2026-02-01T00:00:00Z'
HOUR_MS = 3600000

ex = ccxt.coinbase()

for sym in SYMBOLS:
    try:
        since = ex.parse8601(START)
        rows = []
        while True:
            batch = ex.fetch_ohlcv(f'{sym}/USD', '1h', since=since, limit=300)
            if not batch:
                break
            rows += batch
            since = batch[-1][0] + HOUR_MS
            if since > ex.milliseconds():
                break
            time.sleep(0.25)
        df = pd.DataFrame(rows, columns=['ts', 'open', 'high', 'low', 'close', 'volume'])
        df = df.drop_duplicates('ts')
        df['date'] = pd.to_datetime(df.ts, unit='ms')
        df.to_csv(f'data/raw/cb_{sym.lower()}_1h.csv', index=False)
        print(sym, len(df))
    except Exception as exc:
        print(sym, 'FAIL', str(exc)[:70])

"""Pull SPY minute bars from Massive/Polygon and save to parquet.

Run from ~/trading-lab:
    python3 lab/pull_minute.py

Answers one question: can the overnight edge be captured with a fill in the
final minutes of the session, rather than at the official close?
"""
import os
import time

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("POLYGON_KEY")
if not KEY:
    raise SystemExit("POLYGON_KEY missing from .env")

TICKER = "SPY"
START, END = "2023-01-01", "2026-08-18"
OUT = f"data/raw/{TICKER.lower()}_1min.parquet"

url = (f"https://api.polygon.io/v2/aggs/ticker/{TICKER}"
       f"/range/1/minute/{START}/{END}")
params = {"apiKey": KEY, "limit": 50000, "sort": "asc"}

rows = []
page = 0
while url:
    resp = requests.get(url, params=params)
    if resp.status_code == 429:
        print("rate limited, waiting 60s")
        time.sleep(60)
        continue
    resp.raise_for_status()
    data = resp.json()
    rows += data.get("results", [])
    page += 1
    print(f"page {page}  total bars {len(rows)}")
    url = data.get("next_url")
    params = {"apiKey": KEY}
    if url:
        time.sleep(0.2)

df = pd.DataFrame(rows)
df["ts"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
df = df.rename(columns={"o": "open", "h": "high", "l": "low",
                        "c": "close", "v": "volume", "n": "trades"})
df = df[["ts", "open", "high", "low", "close", "volume", "trades"]]

os.makedirs("data/raw", exist_ok=True)
df.to_parquet(OUT, index=False)
print(f"\nsaved {len(df):,} bars to {OUT}")
print(f"range {df.ts.min()} -> {df.ts.max()}")

"""C-009 — SPY exposure by prior-day move, gated by Fed policy. Paper only.

Rules
-----
Prior session fell >1% AND Fed not tightening  -> 1.5x
Prior session rose >1%                         -> 0.5x
Otherwise                                      -> 1.0x

Timing
------
The edge lives in the overnight gap, so the position must be in place
BEFORE the close. Run this between 3:30 and 3:55pm ET.

Usage
-----
    python3 lab/c009.py             dry run, prints the plan
    python3 lab/c009.py --execute   sends the order
"""
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from dotenv import load_dotenv
from fredapi import Fred

TICKER = "SPY"
DOWN, UP = -0.01, 0.01
LEV_DOWN, LEV_UP, LEV_BASE = 1.5, 0.5, 1.0
MAX_EXPOSURE = 1.5
TOLERANCE = 0.05
LOG = "data/c009_log.csv"

load_dotenv()
KEY, SECRET, FRED_KEY = (os.getenv("ALPACA_KEY"),
                         os.getenv("ALPACA_SECRET"),
                         os.getenv("FRED_KEY"))
if not all([KEY, SECRET, FRED_KEY]):
    sys.exit("missing ALPACA_KEY / ALPACA_SECRET / FRED_KEY in .env")

execute = "--execute" in sys.argv
client = TradingClient(KEY, SECRET, paper=True)

acct = client.get_account()
if not acct.account_number.startswith("PA"):
    sys.exit("REFUSING: not a paper account.")
if acct.trading_blocked:
    sys.exit("REFUSING: trading blocked.")

# --- signal: prior completed session ------------------------------------
close = yf.download(TICKER, period="10d", progress=False)["Close"].squeeze()
returns = close.pct_change().dropna()
sig_date = returns.index[-1].date()
sig_ret = float(returns.iloc[-1])
price = float(close.iloc[-1])

# --- Fed gate: is the funds rate rising vs 63 days ago? -----------------
ff = Fred(api_key=FRED_KEY).get_series("DFF", "2024-01-01")
tightening = bool(ff.iloc[-1] > ff.iloc[-64] + 0.1) if len(ff) > 64 else False

if sig_ret < DOWN and not tightening:
    target, why = LEV_DOWN, "down day, Fed not tightening"
elif sig_ret < DOWN and tightening:
    target, why = LEV_BASE, "down day but Fed TIGHTENING - no leverage"
elif sig_ret > UP:
    target, why = LEV_UP, "up day, reduce"
else:
    target, why = LEV_BASE, "normal"

if target > MAX_EXPOSURE:
    sys.exit(f"REFUSING: {target} exceeds cap {MAX_EXPOSURE}")

equity = float(acct.equity)
try:
    held = float(client.get_open_position(TICKER).qty)
except Exception:
    held = 0.0

want = round(equity * target / price, 4)
delta = round(want - held, 4)

print(f"{sig_date}  SPY {sig_ret*100:+.2f}%  ${price:,.2f}  "
      f"fed_tightening={tightening}")
print(f"target {target}x  ({why})")
print(f"held {held} -> want {want}  delta {delta:+}")

if abs(delta) * price < equity * TOLERANCE:
    action = "hold"
    print("no action, within tolerance")
elif not execute:
    action = "dry_run"
    print("DRY RUN — add --execute to send")
else:
    order = client.submit_order(MarketOrderRequest(
        symbol=TICKER, qty=abs(delta),
        side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
        time_in_force=TimeInForce.DAY))
    action = "executed"
    print(f"SENT {order.side} {order.qty}  id {order.id}")

os.makedirs("data", exist_ok=True)
pd.DataFrame([{
    "logged_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "signal_date": sig_date.isoformat(),
    "signal_return_pct": round(sig_ret * 100, 3),
    "fed_tightening": tightening,
    "price": round(price, 2),
    "equity": round(equity, 2),
    "target_exposure": target,
    "reason": why,
    "held_shares": held,
    "target_shares": want,
    "delta_shares": delta,
    "action": action,
}]).to_csv(LOG, mode="a", header=not os.path.exists(LOG), index=False)
print(f"logged {LOG}")

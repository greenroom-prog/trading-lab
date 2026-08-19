"""Verify the Alpaca paper connection before any strategy code runs.

Run from ~/trading-lab:
    python3 lab/check_broker.py

Reads credentials from .env. Places no orders. Prints account state and
refuses to continue if the account is not a paper account.
"""
import os
import sys

from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("ALPACA_KEY")
SECRET = os.getenv("ALPACA_SECRET")

if not KEY or not SECRET:
    sys.exit("ALPACA_KEY / ALPACA_SECRET missing from .env")

client = TradingClient(KEY, SECRET, paper=True)
acct = client.get_account()

print("=" * 46)
print("  ALPACA PAPER CONNECTION")
print("=" * 46)
print(f"account number   {acct.account_number}")
print(f"status           {acct.status}")
print(f"cash             ${float(acct.cash):,.2f}")
print(f"equity           ${float(acct.equity):,.2f}")
print(f"buying power     ${float(acct.buying_power):,.2f}")
print(f"multiplier       {acct.multiplier}x")
print(f"pattern day trdr {acct.pattern_day_trader}")
print(f"trading blocked  {acct.trading_blocked}")
print("=" * 46)

mult = float(acct.multiplier)
if mult < 1.5:
    print("WARNING: multiplier below 1.5x — C-007 cannot run at full size.")
else:
    print(f"Margin available: {mult}x is enough for C-007's 1.5x.")

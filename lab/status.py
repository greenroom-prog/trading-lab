"""Plain-English status of the trading lab.

Run any time:
    python3 lab/status.py

Reads the paper log and today's market data and says, in words, what the
system currently thinks. Nothing here places an order or touches money.
"""
import os

import pandas as pd
import yfinance as yf

LOG = "data/paper_log.csv"
THRESHOLD = -0.01
LEVERAGE = 1.5


def line(char="-", n=52):
    print(char * n)


def main() -> None:
    line("=")
    print("  TRADING LAB — STATUS")
    line("=")

    data = yf.download("SPY", period="10d", progress=False)
    close = data["Close"].squeeze()
    returns = close.pct_change().dropna()
    last_date = returns.index[-1].date()
    last_return = float(returns.iloc[-1]) * 100
    price = float(close.iloc[-1])

    print(f"\nLast session   {last_date}")
    print(f"SPY closed     ${price:,.2f}")
    print(f"SPY moved      {last_return:+.2f}%")

    if last_return / 100 < THRESHOLD:
        print(f"\nSIGNAL FIRED — SPY fell more than {abs(THRESHOLD)*100:.0f}%.")
        print(f"The rule says: hold {LEVERAGE}x next session.")
    else:
        print(f"\nNo signal. SPY did not fall more than {abs(THRESHOLD)*100:.0f}%.")
        print("The rule says: hold 1.0x — normal exposure.")

    line()
    if os.path.exists(LOG):
        log = pd.read_csv(LOG)
        fired = int((log["target_exposure_next_session"] > 1.0).sum())
        print(f"Paper log      {len(log)} sessions recorded")
        print(f"Signal fired   {fired} times")
        if len(log):
            print(f"First entry    {log['signal_date'].iloc[0]}")
            print(f"Latest entry   {log['signal_date'].iloc[-1]}")
    else:
        print("Paper log      not started — run lab/paper.py")

    line()
    print("STATUS: paper only. No broker connected. No money at risk.")
    line("=")


if __name__ == "__main__":
    main()

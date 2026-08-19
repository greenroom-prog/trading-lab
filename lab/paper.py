"""C-007 paper trading log.

Hold SPY permanently. On the day after SPY falls more than 1%, hold 1.5x.
Otherwise hold 1.0x.

Run once per day after the US close:
    python3 lab/paper.py

Appends one row per session to data/paper_log.csv. No broker, no orders,
no money. This exists to compare what the rule says against what actually
happens, before any capital is involved.
"""
import os
from datetime import datetime, timezone

import pandas as pd
import yfinance as yf

TICKER = "SPY"
THRESHOLD = -0.01
LEVERAGE = 1.5
LOG = "data/paper_log.csv"


def main() -> None:
    data = yf.download(TICKER, period="10d", progress=False)
    close = data["Close"].squeeze()
    returns = close.pct_change().dropna()

    last_date = returns.index[-1]
    last_return = float(returns.iloc[-1])

    # The signal fires for TOMORROW based on TODAY's return.
    target = LEVERAGE if last_return < THRESHOLD else 1.0

    row = {
        "logged_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "signal_date": last_date.date().isoformat(),
        "signal_close": round(float(close.iloc[-1]), 2),
        "signal_return_pct": round(last_return * 100, 3),
        "target_exposure_next_session": target,
    }

    os.makedirs("data", exist_ok=True)
    header = not os.path.exists(LOG)
    pd.DataFrame([row]).to_csv(LOG, mode="a", header=header, index=False)

    print(f"{row['signal_date']}  {TICKER} {row['signal_return_pct']:+.2f}%"
          f"  -> next session exposure {target}x")
    print(f"appended to {LOG}")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Borrow check on EDGAR forced/pressured names.

THE ONLY TEST IN THE ESTATE THAT CANNOT LOSE.
    The EDGAR finding is validated and directionally SHORT. Whether the
    shares can be borrowed has never been confirmed against a broker -
    shorts.py only INFERS it from short interest and volume.

    Shortable   -> the trade re-opens, and the liquidity study was
                   measuring the wrong constraint.
    Not         -> the short thesis is closed. Stop funding it.

    Both answers are worth more than another backtest.

    python3 ops/borrow_check.py --top 25
    python3 ops/borrow_check.py --tickers LSTA,AAPL,XYZ

HTTP LAYER IS UNTESTED
    Written with no route to Alpaca or to :8000. Endpoints and field
    names are recalled, not verified. Run --probe first. An empty result
    and a renamed field look identical from here.
"""

from __future__ import annotations

import argparse
import os
import sys

import requests

EDGAR_API = os.environ.get("EDGAR_API", "http://localhost:8000")
ALPACA_BASE = os.environ.get("ALPACA_BASE",
                             "https://paper-api.alpaca.markets")
TIMEOUT = 20


def alpaca_headers() -> dict:
    k = os.environ.get("APCA_API_KEY_ID")
    s = os.environ.get("APCA_API_SECRET_KEY")
    if not (k and s):
        raise SystemExit(
            "APCA_API_KEY_ID / APCA_API_SECRET_KEY not in the environment.\n"
            "  export them, or source the .env that holds them.")
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def probe() -> int:
    """Assert both dependencies before trusting any output."""
    rc = 0
    try:
        r = requests.get(f"{EDGAR_API}/health", timeout=TIMEOUT)
        print(f"  EDGAR API {EDGAR_API}/health -> HTTP {r.status_code}")
        if r.status_code != 200:
            rc = 1
    except Exception as e:
        print(f"  EDGAR API unreachable: {type(e).__name__} {e}")
        print("    start it:  cd edgar && python3 api/server.py")
        rc = 2

    try:
        h = alpaca_headers()
        r = requests.get(f"{ALPACA_BASE}/v2/assets/AAPL", headers=h,
                         timeout=TIMEOUT)
        print(f"  Alpaca /v2/assets/AAPL -> HTTP {r.status_code}")
        if r.status_code == 200:
            j = r.json()
            want = ("shortable", "easy_to_borrow", "tradable", "status")
            missing = [k for k in want if k not in j]
            if missing:
                print(f"    DRIFTED: missing {missing}; "
                      f"present {sorted(j)[:12]}")
                rc = 1
            else:
                print(f"    fields ok: shortable={j['shortable']} "
                      f"easy_to_borrow={j['easy_to_borrow']}")
        else:
            rc = 1
    except SystemExit:
        raise
    except Exception as e:
        print(f"  Alpaca unreachable: {type(e).__name__} {e}")
        rc = 2
    return rc


def forced_tickers(top: int) -> list[str]:
    """Pull recent filings, keep forced/pressured. One call, scored."""
    r = requests.get(f"{EDGAR_API}/watch",
                     params={"days": 5, "score": "true"}, timeout=120)
    r.raise_for_status()
    out, seen = [], set()
    for f in r.json().get("filings", []):
        t = f.get("ticker")
        sol = (f.get("solvency") or f.get("condition") or "").lower()
        if t and t not in seen and sol in ("forced", "pressured"):
            seen.add(t)
            out.append(t)
        if len(out) >= top:
            break
    return out


def check(tickers: list[str]) -> list[dict]:
    h = alpaca_headers()
    rows = []
    for t in tickers:                       # sequential on purpose
        try:
            r = requests.get(f"{ALPACA_BASE}/v2/assets/{t}",
                             headers=h, timeout=TIMEOUT)
            if r.status_code == 404:
                rows.append({"ticker": t, "status": "not_at_broker",
                             "shortable": None, "etb": None})
                continue
            j = r.json()
            rows.append({"ticker": t, "status": j.get("status"),
                         "shortable": j.get("shortable"),
                         "etb": j.get("easy_to_borrow"),
                         "tradable": j.get("tradable")})
        except Exception as e:
            rows.append({"ticker": t, "status": f"error:{type(e).__name__}",
                         "shortable": None, "etb": None})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--tickers", help="comma list, skips the EDGAR call")
    a = ap.parse_args()

    if a.probe:
        return probe()

    if a.tickers:
        tick = [t.strip().upper() for t in a.tickers.split(",") if t.strip()]
    else:
        print(f"pulling forced/pressured names from {EDGAR_API} ...")
        tick = forced_tickers(a.top)

    if not tick:
        print("No forced/pressured tickers returned.")
        print("If that is surprising, run --probe: an empty list and a")
        print("renamed field look identical from here.")
        return 0

    print(f"checking borrow on {len(tick)} names\n")
    rows = check(tick)

    print(f"{'ticker':<8}{'tradable':>9}{'shortable':>11}{'easy_borrow':>13}"
          f"  {'status'}")
    for r in rows:
        print(f"{r['ticker']:<8}{str(r.get('tradable')):>9}"
              f"{str(r.get('shortable')):>11}{str(r.get('etb')):>13}"
              f"  {r.get('status')}")

    n = len(rows)
    short = sum(1 for r in rows if r.get("shortable") is True)
    etb = sum(1 for r in rows if r.get("etb") is True)
    absent = sum(1 for r in rows if r.get("status") == "not_at_broker")

    print("\n" + "=" * 62)
    print(f"  shortable at broker : {short}/{n}  ({short/n:.0%})")
    print(f"  easy to borrow      : {etb}/{n}  ({etb/n:.0%})")
    print(f"  not carried at all  : {absent}/{n}")
    print("=" * 62)

    if short == 0:
        print("\nVERDICT: the EDGAR short thesis is CLOSED at this broker.")
        print("  Record it in KILLS.md. Stop funding execution work.")
        print("  The FINDING survives - pivot it to information, not a trade.")
    elif etb / n >= 0.5:
        print("\nVERDICT: borrow is broadly available. The short thesis")
        print("  RE-OPENS. Next gate: does the tradeable subset that is")
        print("  ALSO borrowable retain the signal? Re-run the liquidity")
        print("  study restricted to these names before anything else.")
    else:
        print("\nVERDICT: partial. Borrow exists on a minority.")
        print("  That minority IS your universe - re-run the liquidity")
        print("  study on it. Do not assume it inherits the full result;")
        print("  it is a different sample, selected on a new criterion.")

    print("\nCAVEAT: `easy_to_borrow` is a broker list, not a locate, and")
    print("it moves intraday. A borrow that exists today may not exist")
    print("on the day a filing fires. Re-run before sizing anything.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

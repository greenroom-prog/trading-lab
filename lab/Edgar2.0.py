"""Fundamentals engine — read SEC XBRL facts and score financial condition.

Reads structured financial data straight from data.sec.gov. No key, no
scraping, no narrative. It reports what the numbers say and nothing more.

Usage
-----
    python3 lab/fundamentals.py LSTA
    python3 lab/fundamentals.py LSTA NUVL FBRX
    python3 lab/fundamentals.py --file tickers.txt

Output per company
------------------
    cash, quarterly burn, runway in quarters
    debt, leverage, whether operations cover costs
    revenue trend, margin trend
    a plain-language read of what the numbers imply

This engine does NOT predict prices. It answers: what condition is this
company in, and what is it therefore likely to be forced to do?
"""
import argparse
import json
import os
import sys
import time

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

UA = os.getenv("EDGAR_UA")
if not UA:
    raise SystemExit("EDGAR_UA missing from .env")

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

# XBRL tag names vary by filer. Try each in order until one has data.
TAGS = {
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndDueFromBanks",
    ],
    "short_investments": [
        "ShortTermInvestments",
        "MarketableSecuritiesCurrent",
        "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    ],
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ],
    "net_income": ["NetIncomeLoss", "ProfitLoss"],
    "operating_cash_flow": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    ],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "long_debt": [
        "LongTermDebtNoncurrent",
        "LongTermDebt",
        "DebtInstrumentCarryingAmount",
    ],
    "short_debt": ["LongTermDebtCurrent", "ShortTermBorrowings", "DebtCurrent"],
    "interest_expense": ["InterestExpense", "InterestExpenseDebt"],
    "shares": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "operating_income": ["OperatingIncomeLoss"],
}


def load_ticker_map():
    r = requests.get(TICKER_MAP_URL, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return {v["ticker"].upper(): (v["cik_str"], v["title"])
            for v in r.json().values()}


def get_facts(cik):
    r = requests.get(FACTS_URL.format(cik=cik), headers=HEADERS, timeout=60)
    if r.status_code != 200:
        return None
    return r.json()


def latest(facts, tag_list, want_quarterly=False):
    """Return (value, end_date, tag) for the first tag with usable data."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    dei = facts.get("facts", {}).get("dei", {})
    for tag in tag_list:
        node = gaap.get(tag) or dei.get(tag)
        if not node:
            continue
        for unit_name, entries in node["units"].items():
            rows = entries
            if want_quarterly:
                # keep only ~quarterly periods (80-100 days)
                rows = [e for e in entries
                        if e.get("start") and e.get("end")
                        and 80 <= (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).days <= 100]
            if not rows:
                continue
            row = sorted(rows, key=lambda x: x["end"])[-1]
            return row["val"], row["end"], tag
    return None, None, None


def series(facts, tag_list, want_quarterly=True, n=8):
    """Return the last n periodic values as a DataFrame."""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tag_list:
        node = gaap.get(tag)
        if not node:
            continue
        for unit_name, entries in node["units"].items():
            rows = entries
            if want_quarterly:
                rows = [e for e in entries
                        if e.get("start") and e.get("end")
                        and 80 <= (pd.Timestamp(e["end"]) - pd.Timestamp(e["start"])).days <= 100]
            if not rows:
                continue
            df = pd.DataFrame(rows).drop_duplicates("end").sort_values("end")
            return df.tail(n)[["end", "val"]].reset_index(drop=True), tag
    return None, None


def analyze(ticker, tmap):
    if ticker not in tmap:
        return {"ticker": ticker, "error": "ticker not in SEC map"}
    cik, name = tmap[ticker]
    facts = get_facts(cik)
    if facts is None:
        return {"ticker": ticker, "error": "no XBRL facts"}

    out = {"ticker": ticker, "name": name, "cik": cik}

    cash, cash_date, _ = latest(facts, TAGS["cash"])
    st_inv, _, _ = latest(facts, TAGS["short_investments"])
    liquid = (cash or 0) + (st_inv or 0)

    ocf_series, _ = series(facts, TAGS["operating_cash_flow"])
    ni_series, _ = series(facts, TAGS["net_income"])
    rev_series, _ = series(facts, TAGS["revenue"])

    assets, _, _ = latest(facts, TAGS["assets"])
    liab, _, _ = latest(facts, TAGS["liabilities"])
    equity, _, _ = latest(facts, TAGS["equity"])
    ld, _, _ = latest(facts, TAGS["long_debt"])
    sd, _, _ = latest(facts, TAGS["short_debt"])
    debt = (ld or 0) + (sd or 0)
    interest, _, _ = latest(facts, TAGS["interest_expense"])
    op_inc, _, _ = latest(facts, TAGS["operating_income"], want_quarterly=True)

    out["as_of"] = cash_date
    out["cash"] = liquid
    out["assets"] = assets
    out["liabilities"] = liab
    out["equity"] = equity
    out["debt"] = debt

    # --- burn and runway -------------------------------------------------
    burn = None
    if ocf_series is not None and len(ocf_series):
        recent = ocf_series.val.tail(4)
        burn = float(recent.mean())
        out["avg_quarterly_ocf"] = burn
        if burn < 0 and liquid:
            out["runway_quarters"] = round(liquid / abs(burn), 1)
        elif burn >= 0:
            out["runway_quarters"] = "n/a - cash generative"

    # --- revenue trend ---------------------------------------------------
    if rev_series is not None and len(rev_series) >= 2:
        out["revenue_latest"] = float(rev_series.val.iloc[-1])
        out["revenue_yoy"] = (round(
            (rev_series.val.iloc[-1] / rev_series.val.iloc[-5] - 1) * 100, 1)
            if len(rev_series) >= 5 and rev_series.val.iloc[-5] else None)

    # --- leverage --------------------------------------------------------
    if equity and equity != 0:
        out["debt_to_equity"] = round(debt / equity, 2)
    if liab and assets:
        out["liab_to_assets"] = round(liab / assets, 2)
    if interest and op_inc:
        out["interest_coverage"] = round(op_inc / interest, 2) if interest else None

    out["operating_income"] = op_inc

    # --- the read --------------------------------------------------------
    flags = []
    rq = out.get("runway_quarters")
    if isinstance(rq, (int, float)):
        if rq < 4:
            flags.append(f"FORCED: {rq}q runway - dilution, sale or deal likely")
        elif rq < 8:
            flags.append(f"PRESSURED: {rq}q runway")
        else:
            flags.append(f"funded: {rq}q runway")
    if out.get("debt_to_equity") is not None and out["debt_to_equity"] > 2:
        flags.append(f"levered: debt/equity {out['debt_to_equity']}")
    if out.get("liab_to_assets") is not None and out["liab_to_assets"] > 0.9:
        flags.append("liabilities near or above assets")
    if out.get("interest_coverage") is not None and out["interest_coverage"] < 1.5:
        flags.append(f"interest coverage {out['interest_coverage']} - debt service strain")
    if op_inc is not None and op_inc < 0:
        flags.append("operations lose money")
    elif op_inc is not None and op_inc > 0:
        flags.append("operations profitable")
    out["read"] = flags
    return out


def fmt(v):
    if v is None:
        return "-"
    if isinstance(v, (int, float)):
        return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--file", help="file with one ticker per line")
    ap.add_argument("--csv", help="write results to this csv")
    args = ap.parse_args()

    tickers = [t.upper() for t in args.tickers]
    if args.file:
        tickers += [l.strip().upper() for l in open(args.file) if l.strip()]
    if not tickers:
        raise SystemExit("give at least one ticker")

    print("loading SEC ticker map...")
    tmap = load_ticker_map()

    rows = []
    for t in tickers:
        res = analyze(t, tmap)
        rows.append(res)
        print("\n" + "=" * 60)
        if "error" in res:
            print(f"{t}  ERROR: {res['error']}")
            continue
        print(f"{res['ticker']}  {res['name']}")
        print(f"as of {res['as_of']}")
        print("-" * 60)
        print(f"  cash + short inv   {fmt(res.get('cash'))}")
        print(f"  quarterly OCF      {fmt(res.get('avg_quarterly_ocf'))}")
        print(f"  runway (quarters)  {fmt(res.get('runway_quarters'))}")
        print(f"  revenue (latest q) {fmt(res.get('revenue_latest'))}")
        print(f"  revenue YoY %      {fmt(res.get('revenue_yoy'))}")
        print(f"  operating income   {fmt(res.get('operating_income'))}")
        print(f"  total debt         {fmt(res.get('debt'))}")
        print(f"  debt / equity      {fmt(res.get('debt_to_equity'))}")
        print(f"  liab / assets      {fmt(res.get('liab_to_assets'))}")
        print(f"  interest coverage  {fmt(res.get('interest_coverage'))}")
        print("-" * 60)
        for f in res.get("read", []):
            print(f"  * {f}")
        time.sleep(0.15)

    if args.csv:
        pd.DataFrame(rows).to_csv(args.csv, index=False)
        print(f"\nwrote {args.csv}")


if __name__ == "__main__":
    main()

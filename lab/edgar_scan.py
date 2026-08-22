"""EDGAR tender offer scanner — READ ONLY.

Pulls recent Schedule TO filings (cash tender offers) from SEC EDGAR and
records what it finds. Places no trades, makes no recommendations.

The only question this answers right now: how many cash tender offers
actually get filed, and how many are small enough to matter to us?

Run from ~/trading-lab:
    python3 lab/edgar_scan.py
    python3 lab/edgar_scan.py --days 365

SEC requires a real User-Agent with contact info. Set EDGAR_UA in .env, e.g.
    EDGAR_UA=trading-lab yourname@example.com
SEC asks for max 10 requests/second; this stays well under.
"""
import argparse
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()

UA = os.getenv("EDGAR_UA")
if not UA:
    raise SystemExit(
        "EDGAR_UA missing from .env\n"
        "Add a line like:  EDGAR_UA=trading-lab yourname@example.com"
    )

HEADERS = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate"}
SEARCH = "https://efts.sec.gov/LATEST/search-index?q="
FTS = "https://efts.sec.gov/LATEST/search-index"
OUT = "data/raw/edgar_tender_offers.csv"

# Schedule TO-T  = third party tender offer
# Schedule TO-I  = issuer tender offer (company buying its own shares)
# SC 14D9        = target board's recommendation
FORMS = ["SC TO-T", "SC TO-I", "SC 14D9"]


def full_text_search(form, start, end, page=0):
    """Query EDGAR full-text search for one form type in a date range."""
    url = "https://efts.sec.gov/LATEST/search-index?q=&dateRange=custom"
    params = {
        "q": '"tender offer"',
        "forms": form,
        "startdt": start,
        "enddt": end,
        "from": page * 10,
    }
    r = requests.get("https://efts.sec.gov/LATEST/search-index",
                     params=params, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        # fall back to the public endpoint
        r = requests.get("https://www.sec.gov/cgi-bin/srqsb", headers=HEADERS)
    return r


def browse_edgar(form, start, end, count=100):
    """Use the classic full-index browse endpoint — stable and unauthenticated."""
    url = "https://www.sec.gov/cgi-bin/browse-edgar"
    params = {
        "action": "getcompany",
        "type": form,
        "dateb": "",
        "owner": "include",
        "count": count,
        "action": "getcompany",
    }
    return requests.get(url, params=params, headers=HEADERS, timeout=30)


def fetch_daily_index(day):
    """Fetch one day's full filing index from EDGAR."""
    q = (day.month - 1) // 3 + 1
    url = (f"https://www.sec.gov/Archives/edgar/daily-index/"
           f"{day.year}/QTR{q}/form.{day.strftime('%Y%m%d')}.idx")
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200:
        return None
    return r.text


def parse_index(text):
    """Parse the fixed-width form index into rows."""
    rows = []
    started = False
    for line in text.splitlines():
        if line.startswith("---"):
            started = True
            continue
        if not started or not line.strip():
            continue
        form = line[:12].strip()
        company = line[12:74].strip()
        cik = line[74:86].strip()
        date = line[86:98].strip()
        path = line[98:].strip()
        rows.append((form, company, cik, date, path))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90,
                    help="how many calendar days back to scan")
    args = ap.parse_args()

    end = datetime.now()
    start = end - timedelta(days=args.days)

    print(f"scanning EDGAR daily indexes {start.date()} -> {end.date()}")
    print(f"looking for: {', '.join(FORMS)}\n")

    hits, scanned, missing = [], 0, 0
    day = start
    while day <= end:
        if day.weekday() < 5:                      # weekdays only
            text = fetch_daily_index(day)
            scanned += 1
            if text is None:
                missing += 1
            else:
                for form, company, cik, date, path in parse_index(text):
                    if any(form.startswith(f) for f in FORMS):
                        hits.append({
                            "form": form,
                            "company": company,
                            "cik": cik,
                            "filed": date,
                            "url": f"https://www.sec.gov/Archives/{path}",
                        })
            time.sleep(0.15)                       # stay well under SEC limits
            if scanned % 20 == 0:
                print(f"  {day.date()}  scanned {scanned} days, {len(hits)} filings")
        day += timedelta(days=1)

    df = pd.DataFrame(hits)
    os.makedirs("data/raw", exist_ok=True)

    print(f"\nscanned {scanned} trading days ({missing} unavailable)")
    if df.empty:
        print("no tender offer filings found in this window")
        return

    df = df.drop_duplicates(subset=["cik", "form", "filed"])
    df.to_csv(OUT, index=False)

    print(f"found {len(df)} filings, saved to {OUT}\n")
    print("by form type:")
    print(df.form.value_counts().to_string())
    print(f"\nunique companies: {df.cik.nunique()}")
    print(f"rate: {len(df)/max(scanned,1)*21:.1f} filings per trading month")
    print("\nmost recent 10:")
    print(df.sort_values("filed", ascending=False)
            .head(10)[["filed", "form", "company"]].to_string(index=False))


if __name__ == "__main__":
    main()

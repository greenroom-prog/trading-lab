"""
gas_collector.py — build the daily AAA gas-price record that does not
currently exist anywhere, then price the Kalshi ladder against it.

Why this exists: Kalshi runs daily markets on AAA state gas averages.
AAA publishes the number daily but only shows today, yesterday, a week
ago, a month ago, and a year ago. Nobody sells the daily series. Without
it you cannot say whether a strike is mispriced, only whether it feels
mispriced.

    python3 gas_collector.py --collect            # run daily, appends
    python3 gas_collector.py --collect --state TX
    python3 gas_collector.py --stats              # distribution so far
    python3 gas_collector.py --price KXAAAGASDCA  # ladder vs history
    python3 gas_collector.py --backfill           # seed from AAA anchors

Storage: data/gas/aaa_daily.csv, append-only, deduped on (date, state).
"""

from __future__ import annotations

import argparse
import base64
import csv
import os
import re
import statistics as st
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

ROOT = Path.home() / "trading-lab" / "data" / "gas"
ROOT.mkdir(parents=True, exist_ok=True)
CSV = ROOT / "aaa_daily.csv"
FIELDS = ["date", "state", "regular", "mid", "premium", "diesel", "source"]

AAA = "https://gasprices.aaa.com/?state={state}"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research/1.0"}


# ------------------------------------------------------------ collection

def scrape_aaa(state: str):
    """Return today's row plus the yesterday/week/month anchors AAA shows.

    Parses the state average table. Returns None on any parse failure
    rather than a partial row — a wrong number here silently corrupts
    every statistic downstream.
    """
    r = requests.get(AAA.format(state=state), headers=UA, timeout=30)
    r.raise_for_status()
    html = r.text

    # the "Price as of M/D/YY" stamp tells us which day this reading is
    m = re.search(r"Price as of\s*(\d{1,2}/\d{1,2}/\d{2})", html)
    if not m:
        return None, []
    asof = datetime.strptime(m.group(1), "%m/%d/%y").date()

    # CRITICAL: "Current Avg." appears in the state table AND in every
    # metro table below it. Truncate at the metro heading first, or the
    # parser silently returns a city's price as the state average.
    cut = re.search(r"metro average prices", html, re.I)
    scope = html[:cut.start()] if cut else html

    def grab(label):
        """Pull the 4 grade prices from one labelled row in the state table."""
        i = scope.find(label)
        if i < 0:
            return None
        chunk = scope[i:i + 400]
        nums = re.findall(r"\$(\d+\.\d{3,4})", chunk)
        return [float(x) for x in nums[:4]] if len(nums) >= 4 else None

    cur = grab("Current Avg.")
    yes = grab("Yesterday Avg.")
    wk = grab("Week Ago Avg.")
    mo = grab("Month Ago Avg.")
    if not cur:
        return None, []

    today = {"date": asof.isoformat(), "state": state,
             "regular": cur[0], "mid": cur[1], "premium": cur[2],
             "diesel": cur[3], "source": "aaa_current"}

    anchors = []
    for label, offset, vals in (("aaa_yesterday", 1, yes),
                                ("aaa_weekago", 7, wk),
                                ("aaa_monthago", 30, mo)):
        if vals:
            anchors.append({"date": (asof - timedelta(days=offset)).isoformat(),
                            "state": state, "regular": vals[0], "mid": vals[1],
                            "premium": vals[2], "diesel": vals[3],
                            "source": label})
    return today, anchors


def load():
    if not CSV.exists():
        return []
    with CSV.open() as f:
        return list(csv.DictReader(f))


def save(rows):
    """Append-only with dedup. A same-day 'aaa_current' reading always
    wins over an anchor estimate for that date."""
    existing = load()
    rank = {"aaa_current": 3, "aaa_yesterday": 2,
            "aaa_weekago": 1, "aaa_monthago": 0}
    best = {}
    for r in existing + rows:
        k = (r["date"], r["state"])
        if k not in best or rank.get(r["source"], 0) > rank.get(best[k]["source"], 0):
            best[k] = r
    out = sorted(best.values(), key=lambda r: (r["state"], r["date"]))
    with CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    return len(out) - len(existing)


def collect(states):
    for s in states:
        try:
            today, anchors = scrape_aaa(s)
        except Exception as e:
            print(f"{s}: fetch failed — {e}")
            continue
        if not today:
            print(f"{s}: parse failed. AAA changed their page; fix the parser "
                  f"before trusting anything downstream.")
            continue
        added = save([today] + anchors)
        print(f"{s}  {today['date']}  regular ${today['regular']:.4f}  "
              f"(+{added} new rows)")
        time.sleep(1)


# ------------------------------------------------------------ statistics

def series_for(state):
    rows = [r for r in load() if r["state"] == state]
    rows.sort(key=lambda r: r["date"])
    return [(r["date"], float(r["regular"]), r["source"]) for r in rows]


def daily_changes(s):
    """Only consecutive-calendar-day pairs. Gaps are skipped, not
    interpolated — an interpolated change is a fabricated observation."""
    out = []
    for (d1, p1, _), (d2, p2, _) in zip(s, s[1:]):
        a = datetime.fromisoformat(d1).date()
        b = datetime.fromisoformat(d2).date()
        if (b - a).days == 1:
            out.append((d2, (p2 - p1) * 100, b.weekday()))   # cents
    return out


def stats(state):
    s = series_for(state)
    if len(s) < 2:
        print(f"{state}: {len(s)} observations. Need history before this "
              f"says anything. Run --collect daily.")
        return
    ch = daily_changes(s)
    print(f"{state}: {len(s)} readings, {s[0][0]} to {s[-1][0]}")
    print(f"latest ${s[-1][1]:.4f}\n")
    if len(ch) < 5:
        print(f"only {len(ch)} consecutive-day changes — too few for a "
              f"distribution. Keep collecting.")
        return

    vals = [c for _, c, _ in ch]
    a = sorted(vals)
    print("DAILY CHANGE, cents per gallon")
    print(f"  n            {len(vals)}")
    print(f"  mean         {st.mean(vals):+.3f}")
    print(f"  median       {st.median(vals):+.3f}")
    print(f"  stdev        {st.pstdev(vals):.3f}")
    print(f"  min / max    {a[0]:+.3f} / {a[-1]:+.3f}")
    print(f"  up days      {sum(1 for v in vals if v > 0)}/{len(vals)}")

    print("\nBY WEEKDAY (the weekend/Monday question)")
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by = defaultdict(list)
    for _, c, wd in ch:
        by[wd].append(c)
    for wd in range(7):
        v = by.get(wd, [])
        if not v:
            print(f"  {names[wd]}   no data")
            continue
        flag = "" if len(v) >= 5 else "   <- too few to trust"
        print(f"  {names[wd]}  n={len(v):<3} mean {st.mean(v):+.3f}c  "
              f"median {st.median(v):+.3f}c{flag}")
    if min(len(v) for v in by.values()) < 5:
        print("\n  Weekday means with n<5 are noise. Do not trade them.")


def prob_above(state, threshold, current):
    """Empirical probability tomorrow's reading exceeds a threshold.
    Returns None when there is not enough history to answer."""
    ch = daily_changes(series_for(state))
    if len(ch) < 20:
        return None, len(ch)
    need = (threshold - current) * 100          # cents required
    hits = sum(1 for _, c, _ in ch if c >= need)
    return hits / len(ch), len(ch)


# ------------------------------------------------------- kalshi pricing

def kalshi_get(ep, **params):
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from dotenv import load_dotenv
    load_dotenv(Path.home() / "trading-lab" / ".env")
    base = os.environ["KALSHI_BASE_PROD"].rstrip("/")
    prefix = "/" + base.split("://", 1)[1].split("/", 1)[1]
    pk = serialization.load_pem_private_key(
        Path(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"])).read_bytes(),
        password=None)
    ts = str(int(time.time() * 1000))
    sig = pk.sign((ts + "GET" + prefix + ep).encode(),
                  padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                              salt_length=padding.PSS.DIGEST_LENGTH),
                  hashes.SHA256())
    r = requests.get(base + ep,
                     headers={"KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"],
                              "KALSHI-ACCESS-TIMESTAMP": ts,
                              "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()},
                     params=params or None, timeout=25)
    r.raise_for_status()
    return r.json()


def price_ladder(series_ticker, state):
    d = kalshi_get("/markets", series_ticker=series_ticker,
                   status="open", limit=1000)
    mk = d.get("markets", [])
    if not mk:
        print(f"no open markets in {series_ticker}")
        return

    s = series_for(state)
    if not s:
        print(f"no {state} history collected yet — run --collect first")
        return
    current = s[-1][1]
    print(f"{state} latest reading ${current:.4f}  ({s[-1][0]})\n")

    # settlement rules first — the arbitrage killer
    rules = (mk[0].get("rules_primary") or "")[:300]
    print("SETTLEMENT RULE (verify this matches the AAA number above):")
    print("  " + rules.replace("\n", " ") + "\n")

    rows = []
    for m in mk:
        fl = m.get("floor_strike_dollars") or m.get("floor_strike")
        if fl in (None, ""):
            continue
        bid = float(m.get("yes_bid_dollars") or 0) * 100
        ask = float(m.get("yes_ask_dollars") or 0) * 100
        rows.append({"ticker": m["ticker"], "strike": float(fl),
                     "bid": bid, "ask": ask,
                     "mid": (bid + ask) / 2 if ask else 0,
                     "vol": float(m.get("volume_24h_fp") or 0),
                     "sub": (m.get("subtitle") or "")[:22]})
    rows.sort(key=lambda r: r["strike"])

    print(f"{'strike':>10}{'needs':>9}{'bid':>7}{'ask':>7}{'mkt%':>7}"
          f"{'hist%':>8}{'edge':>8}{'vol':>9}")
    enough = None
    for r in rows:
        need = (r["strike"] - current) * 100
        p, n = prob_above(state, r["strike"], current)
        enough = n
        if p is None:
            hp, edge = "  --", "    --"
        else:
            hp = f"{p*100:>6.1f}"
            edge = f"{p*100 - r['mid']:>+7.1f}"
        print(f"{r['strike']:>10.4f}{need:>+9.2f}c{r['bid']:>6.1f}{r['ask']:>7.1f}"
              f"{r['mid']:>7.1f}{hp}{edge}{r['vol']:>9,.0f}")

    if enough is not None and enough < 20:
        print(f"\n  Only {enough} consecutive-day changes collected. The hist%"
              f"\n  column is blank on purpose — {enough} observations cannot"
              f"\n  price a daily distribution. Collect for ~4 weeks first.")
    else:
        print("\n  edge = historical probability minus market price, in points.")
        print("  Positive means the market looks cheap on that strike.")
        print("  This is NOT a signal: the history is short, it ignores the")
        print("  weekday effect, and it assumes tomorrow resembles the sample.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--price", metavar="SERIES")
    ap.add_argument("--state", default="CA")
    ap.add_argument("--states", nargs="*",
                    default=["CA", "TX", "NJ", "IL", "FL"])
    a = ap.parse_args()

    if a.collect:
        collect(a.states)
    if a.stats:
        stats(a.state)
    if a.price:
        price_ladder(a.price, a.state)
    if not (a.collect or a.stats or a.price):
        ap.print_help()


if __name__ == "__main__":
    main()

"""
cli_gap.py — does Kalshi lag the CLI publication?

The CLI product carries an issuanceTime. Kalshi settles on it. If the
market has not repriced in the minutes after publication, that window is
readable without racing anyone: the settling number is already public.

WHAT THIS MEASURES, precisely:
  For each city, the timestamp of the FINAL (YESTERDAY) CLI issuance, and
  the market price for that same target date sampled repeatedly around it.
  If price moves to ~0/100 only AFTER publication, the lag is real and its
  length is the number that matters.

WHAT IT CANNOT MEASURE:
  Whether you could have filled. That needs resting size on the book at
  the moment, which --watch records.

    python3 cli_gap.py --times          # when does each CLI actually post
    python3 cli_gap.py --watch --mins 45
    python3 cli_gap.py --report
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import statistics as st
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path.home() / "trading-lab" / "data" / "weather"
ROOT.mkdir(parents=True, exist_ok=True)
WATCH = ROOT / "gap_watch.csv"
UA = {"User-Agent": "(trading-lab research)"}
NWS = "https://api.weather.gov"

CITIES = {
    "NY":   {"pil": "CLINYC", "loc": "NYC", "series": "KXHIGHNY",   "expect": "CENTRAL PARK"},
    "CHI":  {"pil": "CLIMDW", "loc": "MDW", "series": "KXHIGHCHI",  "expect": "MIDWAY"},
    "MIA":  {"pil": "CLIMIA", "loc": "MIA", "series": "KXHIGHMIA",  "expect": "MIAMI"},
    "AUS":  {"pil": "CLIAUS", "loc": "AUS", "series": "KXHIGHAUS",  "expect": "BERGSTROM"},
    "DEN":  {"pil": "CLIDEN", "loc": "DEN", "series": "KXHIGHDEN",  "expect": "DENVER"},
    "LAX":  {"pil": "CLILAX", "loc": "LAX", "series": "KXHIGHLAX",  "expect": "LOS ANGELES"},
    "PHIL": {"pil": "CLIPHL", "loc": "PHL", "series": "KXHIGHPHIL", "expect": "PHILADELPHIA"},
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}


def parse_cli(text, expect=None):
    m = re.search(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})", text or "")
    day = None
    if m and m.group(1) in MONTHS:
        day = f"{int(m.group(3))}-{MONTHS[m.group(1)]:02d}-{int(m.group(2)):02d}"
    mx = None
    for mm in re.finditer(r"MAXIMUM\s+(-?\d+|MM)", text or ""):
        if mm.group(1) != "MM":
            mx = int(mm.group(1))
            break
    if day is None or mx is None:
        return None
    return {"date": day, "max_f": mx,
            "is_final": bool(re.search(r"\bYESTERDAY\b", text[:1500])),
            "station_ok": (expect.upper() in text[:600].upper()) if expect else None}


def cli_products(loc, n=20):
    r = requests.get(f"{NWS}/products/types/CLI/locations/{loc}",
                     headers=UA, timeout=30)
    r.raise_for_status()
    return r.json().get("@graph", [])[:n]


def cli_text(pid):
    r = requests.get(f"{NWS}/products/{pid}", headers=UA, timeout=30)
    r.raise_for_status()
    return r.json().get("productText", "")


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
    r = requests.get(base + ep, headers={
        "KALSHI-ACCESS-KEY": os.environ["KALSHI_KEY_ID"],
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()},
        params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def fnum(m, *names):
    for n in names:
        v = m.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return 0.0


def market_snapshot(series):
    """Every open strike with its quote and resting size, right now."""
    try:
        d = kalshi_get("/markets", series_ticker=series, status="open", limit=1000)
    except Exception:
        return []
    out = []
    for m in d.get("markets", []):
        out.append({
            "ticker": m["ticker"], "event": m.get("event_ticker", ""),
            "stype": (m.get("strike_type") or "").lower(),
            "floor": fnum(m, "floor_strike_dollars", "floor_strike"),
            "cap": fnum(m, "cap_strike_dollars", "cap_strike"),
            "bid": fnum(m, "yes_bid_dollars") * 100,
            "ask": fnum(m, "yes_ask_dollars") * 100,
            "bid_sz": fnum(m, "yes_bid_size_fp"),
            "ask_sz": fnum(m, "yes_ask_size_fp"),
            "vol24": fnum(m, "volume_24h_fp"),
        })
    return out


# ---------------------------------------------------------------- --times

def cmd_times(a):
    """When does each city's FINAL CLI actually post? Needed before you can
    watch the window — guessing the time wastes the whole morning."""
    print("Recent FINAL (YESTERDAY) CLI issuances — UTC\n")
    print(f"{'city':<6}{'pil':<9}{'covers':<12}{'max':>5}   issued (UTC)      "
          f"local hint")
    print("-" * 76)
    stats = defaultdict(list)
    for code, c in CITIES.items():
        if a.city and code != a.city.upper():
            continue
        try:
            prods = cli_products(c["loc"], 20)
        except Exception as e:
            print(f"{code:<6}{c['pil']:<9}  error {e}")
            continue
        shown = 0
        for p in prods:
            if shown >= a.n:
                break
            try:
                d = parse_cli(cli_text(p["id"]), c["expect"])
            except Exception:
                continue
            time.sleep(0.25)
            if not d or not d["is_final"]:
                continue
            iso = p.get("issuanceTime", "")
            try:
                t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                hh = t.strftime("%H:%M")
                stats[code].append(t.hour * 60 + t.minute)
            except Exception:
                hh = iso[11:16]
            print(f"{code:<6}{c['pil']:<9}{d['date']:<12}{d['max_f']:>5}   "
                  f"{iso[:16]:<18}{hh} UTC")
            shown += 1
        print()

    if stats:
        print("=" * 76)
        print("TYPICAL FINAL-CLI POSTING TIME (UTC)")
        print("=" * 76)
        for code, mins in sorted(stats.items()):
            if not mins:
                continue
            med = st.median(mins)
            lo, hi = min(mins), max(mins)
            print(f"  {code:<6}median {int(med)//60:02d}:{int(med)%60:02d}   "
                  f"range {lo//60:02d}:{lo%60:02d}-{hi//60:02d}:{hi%60:02d}   "
                  f"n={len(mins)}")
        print("\n  Run --watch starting ~15 min before the median for a city.")


# ---------------------------------------------------------------- --watch

def cmd_watch(a):
    """Poll the CLI feed and the market together. Records every sample so
    the before/after comparison is measured, not remembered."""
    codes = [a.city.upper()] if a.city else list(CITIES)
    end = time.time() + a.mins * 60
    seen_final = {}
    rows = []
    print(f"watching {', '.join(codes)} for {a.mins} min, sampling every {a.every}s")
    print("Ctrl-C to stop early; data is written continuously.\n")

    try:
        while time.time() < end:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for code in codes:
                c = CITIES[code]
                # 1. has the final CLI posted?
                published, cli_val, cli_date, cli_time = 0, "", "", ""
                if code in seen_final:
                    published = 1
                    cli_val, cli_date, cli_time = seen_final[code]
                else:
                    try:
                        for p in cli_products(c["loc"], 3):
                            d = parse_cli(cli_text(p["id"]), c["expect"])
                            time.sleep(0.2)
                            if d and d["is_final"] and d["station_ok"]:
                                seen_final[code] = (d["max_f"], d["date"],
                                                    p.get("issuanceTime", ""))
                                published = 1
                                cli_val, cli_date, cli_time = seen_final[code]
                                print(f"  *** {code} CLI PUBLISHED {d['date']} "
                                      f"max {d['max_f']}F at {p.get('issuanceTime','')[:16]}")
                                break
                    except Exception:
                        pass
                # 2. market snapshot
                for m in market_snapshot(c["series"]):
                    rows.append({
                        "sampled_at": now, "city": code, "ticker": m["ticker"],
                        "event": m["event"], "stype": m["stype"],
                        "floor": m["floor"], "cap": m["cap"],
                        "bid": m["bid"], "ask": m["ask"],
                        "bid_sz": m["bid_sz"], "ask_sz": m["ask_sz"],
                        "vol24": m["vol24"],
                        "cli_published": published, "cli_max": cli_val,
                        "cli_date": cli_date, "cli_issued": cli_time,
                    })
            _flush(rows)
            print(f"  {now[11:19]}  {len(rows)} samples  "
                  f"published: {sorted(seen_final)}", flush=True)
            time.sleep(a.every)
    except KeyboardInterrupt:
        print("\nstopped")
    _flush(rows)
    print(f"\nwrote {WATCH}")


def _flush(rows):
    if not rows:
        return
    fields = ["sampled_at", "city", "ticker", "event", "stype", "floor", "cap",
              "bid", "ask", "bid_sz", "ask_sz", "vol24",
              "cli_published", "cli_max", "cli_date", "cli_issued"]
    exists = WATCH.exists()
    with WATCH.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        if not exists:
            w.writeheader()
        w.writerows(rows)
    rows.clear()


# --------------------------------------------------------------- --report

def cmd_report(a):
    if not WATCH.exists():
        print("no watch data — run --watch during a morning window")
        return
    rows = list(csv.DictReader(WATCH.open()))
    if not rows:
        print("watch file empty")
        return

    print(f"{len(rows)} samples\n")
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)

    for city, rs in sorted(by_city.items()):
        pub = [r for r in rs if r["cli_published"] == "1"]
        if not pub:
            print(f"{city}: CLI never published during the window — no test")
            continue
        cli_max = float(pub[0]["cli_max"])
        cli_date = pub[0]["cli_date"]
        issued = pub[0]["cli_issued"]
        print("=" * 78)
        print(f"{city}   settled {cli_max:.0f}F for {cli_date}   "
              f"CLI issued {issued[:16]}")
        print("=" * 78)

        # the strike the CLI value lands in
        def contains(r):
            try:
                fl = float(r["floor"] or 0)
                cp = float(r["cap"] or 0)
            except ValueError:
                return False
            if r["stype"] == "between":
                return fl <= cli_max <= cp
            if r["stype"] == "greater":
                return cli_max >= fl + 1
            if r["stype"] == "less":
                return cli_max <= cp - 1
            return False

        winners = {r["ticker"] for r in rs if contains(r)}
        if not winners:
            print("  could not identify the winning strike")
            continue

        for tk in sorted(winners):
            samples = sorted([r for r in rs if r["ticker"] == tk],
                             key=lambda r: r["sampled_at"])
            before = [r for r in samples if r["cli_published"] == "0"]
            after = [r for r in samples if r["cli_published"] == "1"]
            print(f"\n  WINNING STRIKE {tk}")
            print(f"  {'time':<10}{'state':<12}{'bid':>6}{'ask':>6}"
                  f"{'asksz':>9}")
            for r in samples[-24:]:
                state = "published" if r["cli_published"] == "1" else "pre"
                print(f"  {r['sampled_at'][11:19]:<10}{state:<12}"
                      f"{float(r['bid']):>6.0f}{float(r['ask']):>6.0f}"
                      f"{float(r['ask_sz']):>9,.0f}")
            if before and after:
                b_ask = float(before[-1]["ask"])
                a_ask = float(after[-1]["ask"])
                a_sz = float(after[0]["ask_sz"])
                print(f"\n  last ask BEFORE publication : {b_ask:.0f}c")
                print(f"  ask at FIRST sample after    : {float(after[0]['ask']):.0f}c"
                      f"  (size {a_sz:,.0f})")
                print(f"  ask at LAST sample           : {a_ask:.0f}c")
                gap = 100 - float(after[0]["ask"])
                if gap > 3 and a_sz >= 1:
                    print(f"\n  GAP: winning strike still offered at "
                          f"{float(after[0]['ask']):.0f}c after publication.")
                    print(f"  Theoretical {gap:.0f}c per contract on "
                          f"{a_sz:,.0f} available.")
                    print("  Confirm the timestamps by hand before believing it.")
                else:
                    print("\n  No gap. Market had already repriced.")
            else:
                print("\n  Window did not span the publication — need samples "
                      "both before and after.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--times", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--city")
    ap.add_argument("--mins", type=int, default=45)
    ap.add_argument("--every", type=int, default=20)
    ap.add_argument("--n", type=int, default=4)
    a = ap.parse_args()
    if a.times:
        cmd_times(a)
    elif a.watch:
        cmd_watch(a)
    elif a.report:
        cmd_report(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

"""
cli_data.py — fetch the number that ACTUALLY settles Kalshi weather markets.

Everything before this was calibrated against the wrong target. Kalshi
settles on the NWS Daily Climate Report (CLI), read next morning, at a
specific station. Not reanalysis. Not the raw METAR max. Not the city.

  KXHIGHNY   -> CLINYC  Central Park          (KNYC)
  KXHIGHCHI  -> CLIMDW  Chicago Midway        (KMDW)
  KXHIGHMIA  -> CLIMIA  Miami Intl            (KMIA)
  KXHIGHAUS  -> CLIAUS  Austin-BERGSTROM      (KAUS)   <- not Camp Mabry
  KXHIGHDEN  -> CLIDEN  Denver Intl           (KDEN)
  KXHIGHLAX  -> CLILAX  Los Angeles Intl      (KLAX)
  KXHIGHPHIL -> CLIPHL  Philadelphia Intl     (KPHL)

Two sources:
  live    api.weather.gov  — last ~7 days only
  history mesonet.agron.iastate.edu AFOS archive — years of raw CLI text

    python3 cli_data.py --latest
    python3 cli_data.py --history --city MIA --days 400
    python3 cli_data.py --history-all --days 400
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path.home() / "trading-lab" / "data" / "weather"
ROOT.mkdir(parents=True, exist_ok=True)
OBS = ROOT / "cli_observed.csv"

UA = {"User-Agent": "(trading-lab research, kalshi pricing)"}
NWS = "https://api.weather.gov"
IEM = "https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py"

# pil, expected station string in the product header, lat, lon
CITIES = {
    "NY":   {"pil": "CLINYC", "expect": "CENTRAL PARK",     "lat": 40.779, "lon": -73.969},
    "CHI":  {"pil": "CLIMDW", "expect": "MIDWAY",           "lat": 41.786, "lon": -87.752},
    "MIA":  {"pil": "CLIMIA", "expect": "MIAMI",            "lat": 25.788, "lon": -80.317},
    "AUS":  {"pil": "CLIAUS", "expect": "BERGSTROM",        "lat": 30.183, "lon": -97.680},
    "DEN":  {"pil": "CLIDEN", "expect": "DENVER",           "lat": 39.862, "lon": -104.673},
    "LAX":  {"pil": "CLILAX", "expect": "LOS ANGELES",      "lat": 33.938, "lon": -118.389},
    "PHIL": {"pil": "CLIPHL", "expect": "PHILADELPHIA",     "lat": 39.873, "lon": -75.227},
}

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY",
     "AUGUST", "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"])}


# ------------------------------------------------------------------ parsing

def parse_cli(text: str, expect_station: str | None = None):
    """Pull (summary_date, max_f, is_final, station_ok) out of CLI text.

    is_final: True when the block is labelled YESTERDAY (the morning
    issuance, which is the settlement value). Afternoon issuances are
    labelled TODAY and are PRELIMINARY — they get revised.
    """
    if not text:
        return None

    # "...THE CENTRAL PARK NY CLIMATE SUMMARY FOR AUGUST 22 2026..."
    m = re.search(r"CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})", text)
    summary_date = None
    if m:
        mon, dd, yy = m.group(1), int(m.group(2)), int(m.group(3))
        if mon in MONTHS:
            summary_date = f"{yy}-{MONTHS[mon]:02d}-{dd:02d}"

    station_ok = None
    if expect_station:
        hdr = text[:600].upper()
        station_ok = expect_station.upper() in hdr

    # is this the YESTERDAY (final) block or TODAY (preliminary)?
    is_final = bool(re.search(r"\bYESTERDAY\b", text[:1500]))

    # first integer after MAXIMUM, skipping MM (missing)
    mx = None
    for mm in re.finditer(r"MAXIMUM\s+(-?\d+|MM)", text):
        v = mm.group(1)
        if v != "MM":
            mx = int(v)
            break

    if mx is None or summary_date is None:
        return None
    return {"date": summary_date, "max_f": mx,
            "is_final": is_final, "station_ok": station_ok}


# ------------------------------------------------------------------- live

def latest_from_nws(loc: str, expect: str, tries: int = 6):
    """Most recent CLI issuances from api.weather.gov (~7 day window).
    Walks back until it finds a FINAL (YESTERDAY) issuance."""
    r = requests.get(f"{NWS}/products/types/CLI/locations/{loc}",
                     headers=UA, timeout=30)
    r.raise_for_status()
    graph = r.json().get("@graph", [])
    out = []
    for item in graph[:tries]:
        pid = item.get("id")
        if not pid:
            continue
        p = requests.get(f"{NWS}/products/{pid}", headers=UA, timeout=30)
        if not p.ok:
            continue
        d = parse_cli(p.json().get("productText", ""), expect)
        if d:
            d["issued"] = item.get("issuanceTime", "")
            d["office"] = item.get("issuingOffice", "")
            out.append(d)
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------- history

def history_from_iem(pil: str, start: date, end: date, expect: str):
    """Raw CLI text archive. Returns final (YESTERDAY) readings only."""
    r = requests.get(IEM, params={
        "pil": pil,
        "sdate": start.strftime("%Y-%m-%d"),
        "edate": end.strftime("%Y-%m-%d"),
        "limit": 9999, "fmt": "text"}, timeout=180)
    r.raise_for_status()
    body = r.text
    # products are separated by the start-of-product control char or a
    # blank line followed by the WMO header
    chunks = re.split(r"\n(?=\d{3}\s*\n[A-Z]{4}\d{2} K[A-Z]{3})", body)
    if len(chunks) < 2:
        chunks = re.split(r"\x01", body)
    seen = {}
    bad_station = 0
    for c in chunks:
        d = parse_cli(c, expect)
        if not d:
            continue
        if d["station_ok"] is False:
            bad_station += 1
            continue
        if not d["is_final"]:
            continue          # preliminary; not what settles
        # later issuance for a date wins (corrections)
        seen[d["date"]] = d["max_f"]
    return seen, bad_station, len(chunks)


# -------------------------------------------------------------------- store

def save(rows):
    existing = {}
    if OBS.exists():
        with OBS.open() as fh:
            for r in csv.DictReader(fh):
                existing[(r["city"], r["date"])] = r
    for r in rows:
        existing[(r["city"], r["date"])] = r
    out = sorted(existing.values(), key=lambda r: (r["city"], r["date"]))
    with OBS.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["city", "pil", "date", "max_f", "source"])
        w.writeheader()
        w.writerows(out)
    return len(out)


# -------------------------------------------------------------------- modes

def cmd_latest(a):
    print("Live CLI — the value Kalshi settles on\n")
    print(f"{'city':<6}{'pil':<9}{'summary date':<14}{'max F':>7}"
          f"{'final':>8}{'station':>9}  issued")
    print("-" * 78)
    rows = []
    for code, c in CITIES.items():
        if a.city and code != a.city.upper():
            continue
        try:
            got = latest_from_nws(c["pil"][3:], c["expect"])
        except Exception as e:
            print(f"{code:<6}{c['pil']:<9}  error: {e}")
            continue
        if not got:
            print(f"{code:<6}{c['pil']:<9}  no products returned")
            continue
        for d in got[:3]:
            ok = "OK" if d["station_ok"] else "MISMATCH"
            fin = "FINAL" if d["is_final"] else "prelim"
            print(f"{code:<6}{c['pil']:<9}{d['date']:<14}{d['max_f']:>7}"
                  f"{fin:>8}{ok:>9}  {d['issued'][:16]}")
            if d["is_final"] and d["station_ok"]:
                rows.append({"city": code, "pil": c["pil"], "date": d["date"],
                             "max_f": d["max_f"], "source": "nws_api"})
        print()
    if rows:
        n = save(rows)
        print(f"saved {len(rows)} final readings, {n} total in {OBS}")


def cmd_history(a):
    end = date.today()
    start = end - timedelta(days=a.days)
    codes = [a.city.upper()] if a.city else list(CITIES)
    allrows = []
    for code in codes:
        c = CITIES[code]
        print(f"{code} {c['pil']} {start} to {end}...", flush=True)
        try:
            seen, bad, nchunks = history_from_iem(c["pil"], start, end, c["expect"])
        except Exception as e:
            print(f"  failed: {e}")
            continue
        print(f"  {nchunks} products parsed | {len(seen)} final daily highs"
              + (f" | {bad} rejected on station mismatch" if bad else ""))
        if seen:
            vals = sorted(seen.items())
            print(f"  range {vals[0][0]} to {vals[-1][0]}, "
                  f"highs {min(v for _, v in vals)}-{max(v for _, v in vals)}F")
            allrows += [{"city": code, "pil": c["pil"], "date": d,
                         "max_f": v, "source": "iem_afos"}
                        for d, v in vals]
        time.sleep(1.0)
    if allrows:
        n = save(allrows)
        print(f"\nsaved {len(allrows)} rows, {n} total -> {OBS}")
    else:
        print("\nnothing retrieved")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--latest", action="store_true")
    ap.add_argument("--history", action="store_true")
    ap.add_argument("--city")
    ap.add_argument("--days", type=int, default=400)
    a = ap.parse_args()
    if a.latest:
        cmd_latest(a)
    elif a.history:
        cmd_history(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

"""
feeds.py — read the same numbers Kalshi settles on, from the same sources.

One class per settlement source, all with the same shape:

    .name          human label
    .kalshi_series which Kalshi series settle on it
    .value(key)    -> {"value":..., "as_of":..., "source":..., "raw":...}
    .available()   -> (bool, reason)

Sources are ranked by the 24h Kalshi volume settling on them (sources.py):

  CF Benchmarks   3,028,130   crypto ladders          free REST
  Fed / FRED      1,707,080   rate decisions          free, key
  AAA fuel          181,034   gas ladders             scrape
  IMF PortWatch     166,609   Hormuz / shipping       free
  Netflix Top 10    273,548   ranking markets         free
  BLS               187,105   CPI / jobs              free, key
  EIA                 3,450   energy weekly           free, key
  NWS CLI           (weather) temperature             free  -> cli_data.py

Not built, deliberately: WTI settlement, metals fix, Billboard, Luminate,
Rotten Tomatoes. Those are vendor or closed feeds — see sources.py.

    python3 feeds.py --check              # which feeds answer right now
    python3 feeds.py --value cfb BTC
    python3 feeds.py --value aaa US
    python3 feeds.py --value portwatch hormuz
    python3 feeds.py --snapshot           # everything, timestamped, to disk
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path.home() / "trading-lab" / "data" / "feeds"
ROOT.mkdir(parents=True, exist_ok=True)
SNAP = ROOT / "snapshots.csv"
UA = {"User-Agent": "trading-lab research (contact via kalshi account)"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _env(name):
    from dotenv import load_dotenv
    load_dotenv(Path.home() / "trading-lab" / ".env")
    return os.getenv(name)


class Feed:
    name = "base"
    kalshi_series: list[str] = []
    keys: list[str] = []

    def available(self):
        return True, ""

    def value(self, key):
        raise NotImplementedError


# ------------------------------------------------- CF Benchmarks  3,028,130

class CFBenchmarks(Feed):
    """The index that settles every Kalshi crypto ladder.

    Rules read: "the simple average of the sixty seconds of CF Benchmarks'
    Bitcoin Real-Time Index (BRTI) before 5 PM EDT". So the settlement
    value is a 60-second mean of BRTI, not a spot print. Reading BRTI live
    is reading the settling input directly.
    """
    name = "CF Benchmarks"
    kalshi_series = ["KXBTCD", "KXBTC", "KXETHD", "KXSOLE", "KXBTCY",
                     "KXXRPD", "KXBNB", "KXHYPE", "KXBTCMAXMON", "KXBTCMINY"]
    keys = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "LTC", "ADA"]
    BASE = "https://www.cfbenchmarks.com/api/v1"
    RTI = {"BTC": "BRTI", "ETH": "ETHUSD_RTI", "SOL": "SOLUSD_RTI",
           "XRP": "XRPUSD_RTI", "BNB": "BNBUSD_RTI", "DOGE": "DOGEUSD_RTI",
           "LTC": "LTCUSD_RTI", "ADA": "ADAUSD_RTI"}

    def value(self, key):
        """Correct endpoint is /api/v1/latest_values (v1 guessed /values/{sym}
        and got 404). Public read; licensed feeds need a key."""
        sym = self.RTI.get(key.upper(), key)
        errs = []
        for url, params in (
            (f"{self.BASE}/latest_values", {"id": sym}),
            (f"{self.BASE}/latest_values", None),
            (f"{self.BASE}/values", {"id": sym}),
        ):
            try:
                r = requests.get(url, params=params, headers=UA, timeout=20)
                if not r.ok:
                    errs.append(f"{r.status_code} {url}")
                    continue
                d = r.json()
                # latest_values returns a list; find our index id
                node = d
                if isinstance(d, list):
                    node = next((x for x in d
                                 if str(_dig(x, ("id", "indexId"))) == sym), None)
                elif isinstance(d, dict) and isinstance(d.get("data"), list):
                    node = next((x for x in d["data"]
                                 if str(_dig(x, ("id", "indexId"))) == sym), None)
                if node is None:
                    errs.append(f"{sym} not in response from {url}")
                    continue
                v = _dig(node, ("value", "price", "level", "indexValue"))
                t = _dig(node, ("time", "timestamp", "asOf", "ts"))
                if v is not None:
                    if isinstance(t, (int, float)) and t > 1e11:
                        t = datetime.utcfromtimestamp(t / 1000).isoformat()
                    return {"value": float(v), "as_of": str(t or _now()),
                            "source": url, "raw": node}
            except Exception as e:
                errs.append(f"{type(e).__name__} {url}")
        raise RuntimeError(
            f"CF Benchmarks REST failed for {sym}: {errs[:2]}. "
            "Better path: Kalshi's own WebSocket channel 'cfbenchmarks_value' "
            "carries the index value AND the trailing 60s average — which is "
            "the settlement input. Uses your existing Kalshi credentials. "
            "See docs.kalshi.com/websockets/cfbenchmarks-value")


# ---------------------------------------------------- Fed / FRED  1,707,080

class FedRates(Feed):
    """Effective fed funds rate and the target band, from FRED. The FOMC
    decision itself is a scheduled announcement; FRED carries the number
    the day after. For the decision itself the source is the statement."""
    name = "Federal Reserve (FRED)"
    kalshi_series = ["KXFEDDECISION", "KXFED", "KXRATECUTCOUNT", "FEDHIKE"]
    keys = ["EFFR", "DFEDTARU", "DFEDTARL", "DGS10", "DGS2", "BAMLH0A0HYM2"]

    def available(self):
        return (bool(_env("FRED_KEY")), "FRED_KEY missing from .env")

    def value(self, key):
        k = _env("FRED_KEY")
        if not k:
            raise RuntimeError("FRED_KEY missing")
        r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                         params={"series_id": key.upper(), "api_key": k,
                                 "file_type": "json", "sort_order": "desc",
                                 "limit": 1}, timeout=25)
        r.raise_for_status()
        o = r.json()["observations"][0]
        return {"value": float(o["value"]), "as_of": o["date"],
                "source": f"FRED {key.upper()}", "raw": o}


# ----------------------------------------------------------- AAA    181,034

class AAAFuel(Feed):
    """Rules read: "average regular gas prices for United States ...
    according to AAA". The base series is the NATIONAL average — the
    state series (KXAAAGASDCA etc) are separate markets.
    No public API; the value is on the page."""
    name = "AAA fuel prices"
    kalshi_series = ["KXAAAGASD", "KXAAAGASW", "KXAAAGASM", "KXAAAGASDCA",
                     "KXAAAGASDTX", "KXAAAGASDNJ", "KXAAAGASDIL",
                     "KXAAAGASDFL", "KXDIESELW"]
    keys = ["US", "CA", "TX", "NJ", "IL", "FL", "NY"]

    def value(self, key):
        state = key.upper()
        url = "https://gasprices.aaa.com/" + ("" if state == "US"
                                              else f"?state={state}")
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        h = r.text
        cut = re.search(r"metro average prices", h, re.I)
        scope = h[:cut.start()] if cut else h
        i = scope.find("Current Avg.")
        nums = re.findall(r"\$(\d+\.\d{3,4})", scope[i:i + 400]) if i >= 0 else []
        if not nums:
            m = re.search(r"Today's AAA National Average\s*\$?(\d+\.\d{3,4})", h)
            nums = [m.group(1)] if m else []
        if not nums:
            raise RuntimeError("AAA page format changed — fix the parser "
                               "before trusting anything downstream")
        d = re.search(r"Price as of\s*(\d{1,2}/\d{1,2}/\d{2})", h)
        return {"value": float(nums[0]),
                "as_of": d.group(1) if d else _now(),
                "source": url, "raw": {"grades": nums[:4]}}


# ---------------------------------------------------- IMF PortWatch 166,609

class PortWatch(Feed):
    """Rules read: "according to the IMF PortWatch". Daily transit counts
    through chokepoints including Hormuz — the feed behind KXHORMUZNORM."""
    name = "IMF PortWatch"
    kalshi_series = ["KXHORMUZNORM"]
    keys = ["hormuz", "suez", "panama", "bab-el-mandeb", "gibraltar"]
    ARC = ("https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/"
           "Daily_Chokepoints_Data/FeatureServer/0/query")

    def value(self, key):
        want = key.lower().replace("_", "-")
        r = requests.get(self.ARC, params={
            "where": "1=1", "outFields": "*", "f": "json",
            "orderByFields": "date DESC", "resultRecordCount": 400},
            headers=UA, timeout=40)
        r.raise_for_status()
        feats = r.json().get("features", [])
        if not feats:
            raise RuntimeError("PortWatch returned no rows")
        for f in feats:
            a = f.get("attributes", {})
            name = str(a.get("portname") or a.get("chokepoint")
                       or a.get("NAME") or "").lower()
            if want in name.replace(" ", "-"):
                v = (a.get("n_total") or a.get("n_all")
                     or a.get("transits") or a.get("n_cargo"))
                ts = a.get("date")
                if isinstance(ts, (int, float)):
                    ts = datetime.utcfromtimestamp(ts / 1000).date().isoformat()
                if v is not None:
                    return {"value": float(v), "as_of": str(ts),
                            "source": "IMF PortWatch daily chokepoints",
                            "raw": a}
        names = sorted({str(f["attributes"].get("portname", "")) for f in feats})
        raise RuntimeError(f"'{key}' not found. Available: {names[:12]}")


# -------------------------------------------------------- Netflix   273,548

class NetflixTop10(Feed):
    """v1 scraped netflix.com/tudum/top10, which renders client-side and
    returned no titles. Netflix publishes the same rankings as a CSV."""
    name = "Netflix Top 10"
    kalshi_series = ["KXNETFLIXRANKMOVIE", "KXNETFLIXRANKSHOW"]
    keys = ["films-en", "tv-en", "films-non-en", "tv-non-en"]
    CSV = "https://www.netflix.com/tudum/top10/data/all-weeks-global.tsv"

    def value(self, key):
        cat = {"films-en": "films", "tv-en": "tv",
               "films-non-en": "films", "tv-non-en": "tv"}.get(key, "films")
        eng = "non-English" if "non-en" in key else "English"
        r = requests.get(self.CSV, headers=UA, timeout=45)
        r.raise_for_status()
        lines = r.text.splitlines()
        if len(lines) < 2:
            raise RuntimeError("Netflix TSV empty — check the URL")
        hdr = lines[0].split("\t")
        rows = [dict(zip(hdr, l.split("\t"))) for l in lines[1:5000]]
        want = f"{cat.upper()} ({eng})" if eng == "non-English" else cat
        week = max(r_.get("week", "") for r_ in rows)
        top = [r_ for r_ in rows if r_.get("week") == week
               and cat in (r_.get("category", "").lower())
               and (("non-english" in r_.get("category", "").lower())
                    == ("non-en" in key))]
        top.sort(key=lambda r_: int(r_.get("weekly_rank") or 99))
        if not top:
            raise RuntimeError(f"no rows for {key}; categories seen: "
                               f"{sorted({r_.get('category','') for r_ in rows})[:6]}")
        return {"value": len(top), "as_of": week,
                "source": self.CSV,
                "raw": {"top": [(r_.get("weekly_rank"), r_.get("show_title"))
                                for r_ in top[:10]]}}


# ------------------------------------------------------------ BLS   187,105

class BLSData(Feed):
    name = "BLS"
    kalshi_series = ["KXCPI", "KXCPIYOY", "KXU3", "KXPAYROLLS"]
    keys = ["CUUR0000SA0", "LNS14000000", "CES0000000001"]

    def value(self, key):
        body = {"seriesid": [key], "latest": True}
        k = _env("BLS_KEY")
        if k:
            body["registrationkey"] = k
        r = requests.post(
            "https://api.bls.gov/publicAPI/v2/timeseries/data/",
            json=body, headers={**UA, "Content-Type": "application/json"},
            timeout=30)
        r.raise_for_status()
        d = r.json()
        try:
            s = d["Results"]["series"][0]["data"][0]
        except (KeyError, IndexError):
            raise RuntimeError(f"BLS: {d.get('message') or 'no data'}")
        return {"value": float(s["value"]),
                "as_of": f"{s['year']}-{s['period'][1:]}",
                "source": f"BLS {key}", "raw": s}


# ------------------------------------------------------------- EIA    3,450

class EIAData(Feed):
    name = "EIA"
    kalshi_series = ["KXNATGASD", "KXWTI", "KXWTIMAX"]
    keys = ["RWTC", "RBRTE", "GASREGW"]

    def available(self):
        return (bool(_env("EIA_KEY")), "EIA_KEY missing from .env")

    def value(self, key):
        k = _env("EIA_KEY")
        r = requests.get("https://api.eia.gov/v2/petroleum/pri/spt/data/",
                         params={"api_key": k, "frequency": "daily",
                                 "data[0]": "value",
                                 "facets[series][]": key.upper(),
                                 "sort[0][column]": "period",
                                 "sort[0][direction]": "desc", "length": 1},
                         timeout=30)
        r.raise_for_status()
        rows = r.json().get("response", {}).get("data", [])
        if not rows:
            raise RuntimeError(f"EIA returned nothing for {key}")
        return {"value": float(rows[0]["value"]), "as_of": rows[0]["period"],
                "source": f"EIA {key}", "raw": rows[0]}


# ------------------------------------------------------------- helpers

def _dig(d, names):
    """First matching key anywhere in a nested dict/list."""
    if isinstance(d, dict):
        for n in names:
            if n in d and not isinstance(d[n], (dict, list)):
                return d[n]
        for v in d.values():
            got = _dig(v, names)
            if got is not None:
                return got
    elif isinstance(d, list):
        for v in d:
            got = _dig(v, names)
            if got is not None:
                return got
    return None


FEEDS = {"cfb": CFBenchmarks(), "fred": FedRates(), "aaa": AAAFuel(),
         "portwatch": PortWatch(), "netflix": NetflixTop10(),
         "bls": BLSData(), "eia": EIAData()}


def cmd_check(a):
    print(f"{'feed':<12}{'source':<26}{'status':<10}  detail")
    print("-" * 78)
    for kid, f in FEEDS.items():
        ok, why = f.available()
        if not ok:
            print(f"{kid:<12}{f.name:<26}{'BLOCKED':<10}  {why}")
            continue
        probe = f.keys[0]
        try:
            v = f.value(probe)
            print(f"{kid:<12}{f.name:<26}{'LIVE':<10}  "
                  f"{probe}={v['value']:,.4f} as of {str(v['as_of'])[:19]}")
        except Exception as e:
            print(f"{kid:<12}{f.name:<26}{'FAILED':<10}  {str(e)[:46]}")
        time.sleep(0.3)
    print("\nBLOCKED = needs a key in .env. FAILED = endpoint moved or the "
          "parser needs fixing.")


def cmd_value(a):
    f = FEEDS.get(a.feed)
    if not f:
        raise SystemExit(f"unknown feed. known: {', '.join(FEEDS)}")
    v = f.value(a.key)
    print(json.dumps({"feed": f.name, "key": a.key, **{
        k: v[k] for k in ("value", "as_of", "source")}}, indent=2))
    print(f"\nsettles: {', '.join(f.kalshi_series[:8])}")


def cmd_snapshot(a):
    rows = []
    now = _now()
    for kid, f in FEEDS.items():
        ok, _ = f.available()
        if not ok:
            continue
        for key in f.keys[:a.per_feed]:
            try:
                v = f.value(key)
                rows.append({"sampled_at": now, "feed": kid, "key": key,
                             "value": v["value"], "as_of": v["as_of"],
                             "source": str(v["source"])[:80]})
                print(f"  {kid:<10}{key:<16}{v['value']:>14,.4f}  {v['as_of']}")
            except Exception as e:
                print(f"  {kid:<10}{key:<16}{'--':>14}  {str(e)[:44]}")
            time.sleep(0.3)
    if rows:
        exists = SNAP.exists()
        with SNAP.open("a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            if not exists:
                w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} values appended to {SNAP}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--value", nargs=2, metavar=("FEED", "KEY"))
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--per-feed", type=int, default=3)
    a = ap.parse_args()
    if a.check:
        cmd_check(a)
    elif a.value:
        a.feed, a.key = a.value
        cmd_value(a)
    elif a.snapshot:
        cmd_snapshot(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

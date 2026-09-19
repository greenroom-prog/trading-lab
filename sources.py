"""
sources.py — enumerate every data source Kalshi settles on.

The settlement source defines what data is even relevant. Guessing it cost
us a whole calibration run (reanalysis instead of NWS CLI) and a gas
collector pointed at state averages when the market settles on the US
national figure.

This reads rules_primary across every series with real volume, extracts
the named source, and groups markets by it. The output is the list of
feeds worth connecting to, ranked by how much money settles on each.

    python3 sources.py --scan              # all categories
    python3 sources.py --scan --min-vol 0  # include dormant series
    python3 sources.py --show              # re-read the saved report
    python3 sources.py --detail CLI        # every market on one source
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import requests

ROOT = Path.home() / "trading-lab" / "data" / "kalshi"
ROOT.mkdir(parents=True, exist_ok=True)
OUT = ROOT / "settlement_sources.json"
CSV_OUT = ROOT / "settlement_sources.csv"

CATEGORIES = ["Crypto", "Economics", "Financials", "Politics", "Climate",
              "Companies", "Commodities", "Culture", "Health", "World",
              "Technology", "Science", "Transportation", "Entertainment"]

# Named entities that appear as settlement authorities, mapped to whether
# we have a programmatic path to the same number.
KNOWN = [
    (r"national weather service|NWS|climate report|CLI[A-Z]{3}",
     "NWS Climate Report (CLI)", "api.weather.gov + IEM archive", "YES"),
    (r"the weather company|weather\.com|weather underground",
     "The Weather Company", "commercial feed only", "PARTIAL"),
    (r"\bAAA\b",
     "AAA fuel prices", "gasprices.aaa.com scrape", "SCRAPE"),
    (r"CF Benchmarks|BRTI|BRR",
     "CF Benchmarks crypto index", "docs.cfbenchmarks.com REST", "YES"),
    (r"\bPyth\b", "Pyth oracle", "hermes.pyth.network", "YES"),
    (r"coinbase", "Coinbase", "Coinbase Advanced Trade API", "YES"),
    (r"federal reserve|FOMC|federal open market",
     "Federal Reserve / FOMC", "federalreserve.gov + FRED", "YES"),
    (r"bureau of labor statistics|\bBLS\b|consumer price index|\bCPI\b",
     "BLS", "api.bls.gov", "YES"),
    (r"bureau of economic analysis|\bBEA\b|gross domestic product",
     "BEA", "apps.bea.gov/api", "YES"),
    (r"energy information administration|\bEIA\b",
     "EIA", "api.eia.gov", "YES"),
    (r"\bFINRA\b", "FINRA", "public files", "PARTIAL"),
    (r"securities and exchange commission|\bSEC\b|EDGAR",
     "SEC EDGAR", "data.sec.gov (already built)", "YES"),
    (r"rotten tomatoes", "Rotten Tomatoes", "no public API", "NO"),
    (r"box office mojo|the-numbers|domestic box office",
     "Box office reporting", "scrape", "SCRAPE"),
    (r"billboard", "Billboard", "no public API", "NO"),
    (r"nielsen", "Nielsen", "commercial", "NO"),
    (r"associated press|\bAP\b race call", "AP race calls", "AP API (paid)", "NO"),
    (r"\bSpaceX\b|launch", "SpaceX / launch manifest", "scrape", "SCRAPE"),
    (r"steam|steamdb", "Steam", "steamapi", "YES"),
    (r"\bIMDb\b", "IMDb", "commercial", "NO"),
    (r"lmarena|chatbot arena|llm arena|top-ranked LLM",
     "LLM leaderboard", "scrape", "SCRAPE"),
    (r"\bNOAA\b|national hurricane center|\bNHC\b",
     "NOAA / NHC", "nhc.noaa.gov feeds", "YES"),
    (r"johns hopkins|\bCDC\b", "CDC", "data.cdc.gov", "YES"),
    (r"\bUSGS\b", "USGS", "earthquake.usgs.gov", "YES"),
    (r"polymarket", "Polymarket", "public API", "YES"),
    (r"\bOPIS\b", "OPIS", "commercial", "NO"),
    (r"PortWatch|\bIMF\b", "IMF PortWatch", "portwatch.imf.org (free)", "YES"),
    (r"netflix", "Netflix Top 10", "top10.netflix.com (free)", "YES"),
    (r"youtube|\bviews\b", "YouTube", "YouTube Data API", "YES"),
    (r"\bWTI\b|west texas intermediate|crude oil",
     "WTI crude settlement", "EIA / CME vendor", "PARTIAL"),
    (r"silver|\bgold\b|LBMA|precious metal",
     "Metals fix", "LBMA / vendor", "PARTIAL"),
    (r"luminate|album equivalent|\bMRC\b",
     "Luminate music data", "commercial", "NO"),
    (r"steam charts|concurrent players", "Steam", "steamapi", "YES"),
    (r"\bS&P\b|standard & poor|\bCME\b|\bCBOE\b|\bNasdaq\b|\bNYSE\b",
     "Exchange / index provider", "vendor data", "PARTIAL"),
]


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
        params=params or None, timeout=25)
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


def classify(rules: str):
    """Which known source does this rule name? Returns every match, because
    some rules name a primary and a fallback."""
    hits = []
    low = rules or ""
    for pat, name, access, have in KNOWN:
        if re.search(pat, low, re.I):
            hits.append((name, access, have))
    return hits or [("UNCLASSIFIED", "read the rule", "?")]


def extract_phrase(rules: str):
    """The 'according to X' clause, verbatim — the authoritative bit."""
    m = re.search(r"according to ([^,.]{3,60})", rules or "", re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"as (?:reported|published|determined) by ([^,.]{3,60})",
                  rules or "", re.I)
    return m.group(1).strip() if m else ""


def scan(min_vol):
    seen_series = {}
    for cat in CATEGORIES:
        try:
            d = kalshi_get("/series", category=cat)
        except Exception as e:
            print(f"  {cat}: {e}")
            continue
        ser = d.get("series") or []
        ser = [s for s in ser if not (s.get("ticker") or "").startswith("KXMVE")]
        print(f"{cat}: {len(ser)} series", flush=True)
        for s in ser:
            t = s.get("ticker")
            if t:
                seen_series[t] = {"category": cat, "title": s.get("title", "")}
        time.sleep(0.05)

    print(f"\n{len(seen_series)} series total; reading rules...\n")
    rows = []
    for i, (tk, meta) in enumerate(sorted(seen_series.items())):
        try:
            d = kalshi_get("/markets", series_ticker=tk, status="open", limit=1)
        except Exception:
            continue
        try:
            full = kalshi_get("/markets", series_ticker=tk, status="open", limit=1000)
            mkts = full.get("markets", [])
        except Exception:
            mkts = d.get("markets", [])
        if not mkts:
            continue
        tot_vol = sum(fnum(x, "volume_24h_fp") for x in mkts)
        tot_oi = sum(fnum(x, "open_interest_fp") for x in mkts)
        if tot_vol < min_vol and tot_oi < min_vol:
            continue
        # take rules from the most active market, not an arbitrary one
        m = max(mkts, key=lambda x: fnum(x, "volume_24h_fp"))
        rules = m.get("rules_primary") or ""
        for name, access, have in classify(rules):
            rows.append({
                "series": tk, "category": meta["category"],
                "title": (meta["title"] or m.get("title", ""))[:60],
                "source": name, "access": access, "have": have,
                "phrase": extract_phrase(rules),
                "n_markets": len(mkts), "vol24": tot_vol, "oi": tot_oi,
                "rule": rules[:300],
            })
        if i % 25 == 0:
            print(f"  {i} series scanned, {len(rows)} rows", flush=True)
        time.sleep(0.05)

    OUT.write_text(json.dumps(rows, indent=2))
    if rows:
        with CSV_OUT.open("w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    print(f"\nwrote {OUT}")
    show()


def show():
    if not OUT.exists():
        print("no scan yet — run --scan")
        return
    rows = json.loads(OUT.read_text())
    if not rows:
        print("scan produced nothing")
        return

    agg = defaultdict(lambda: {"vol": 0.0, "oi": 0.0, "n": 0,
                               "series": set(), "access": "", "have": ""})
    for r in rows:
        a = agg[r["source"]]
        a["vol"] += r["vol24"]
        a["oi"] += r["oi"]
        a["n"] += r["n_markets"]
        a["series"].add(r["series"])
        a["access"] = r["access"]
        a["have"] = r["have"]

    print("\n" + "=" * 94)
    print("SETTLEMENT SOURCES BY 24H VOLUME — what Kalshi actually reads")
    print("=" * 94)
    print(f"{'source':<32}{'have':<9}{'series':>7}{'mkts':>7}"
          f"{'vol24':>12}{'OI':>12}  access")
    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["vol"]):
        print(f"{name:<32}{a['have']:<9}{len(a['series']):>7}{a['n']:>7}"
              f"{a['vol']:>12,.0f}{a['oi']:>12,.0f}  {a['access']}")

    print("\n" + "=" * 94)
    print("CONNECTABLE — sources with a free programmatic feed")
    print("=" * 94)
    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["vol"]):
        if a["have"] == "YES" and a["vol"] > 0:
            print(f"  {name:<32}{a['vol']:>12,.0f} vol   {a['access']}")

    print("\n" + "=" * 94)
    print("NOT CONNECTABLE — do not build here")
    print("=" * 94)
    for name, a in sorted(agg.items(), key=lambda kv: -kv[1]["vol"]):
        if a["have"] in ("NO", "PARTIAL") and a["vol"] > 0:
            print(f"  {name:<32}{a['vol']:>12,.0f} vol   {a['access']}")

    unc = agg.get("UNCLASSIFIED")
    if unc and unc["vol"] > 0:
        print("\n" + "=" * 94)
        print(f"UNCLASSIFIED — {len(unc['series'])} series, "
              f"{unc['vol']:,.0f} vol. Read these rules by hand:")
        print("=" * 94)
        for r in sorted([x for x in rows if x["source"] == "UNCLASSIFIED"],
                        key=lambda x: -x["vol24"])[:15]:
            print(f"  {r['vol24']:>10,.0f}  {r['series']:<22}{r['title'][:40]}")
            if r["phrase"]:
                print(f"              'according to {r['phrase']}'")


def detail(key):
    rows = json.loads(OUT.read_text())
    hits = [r for r in rows if key.lower() in r["source"].lower()]
    if not hits:
        print(f"no source matching '{key}'")
        return
    print(f"{len(hits)} series settling on '{key}'\n")
    for r in sorted(hits, key=lambda x: -x["vol24"]):
        print(f"{r['vol24']:>10,.0f} vol  {r['series']:<22}{r['title'][:44]}")
        print(f"            {r['rule'][:150]}")
        print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--detail")
    ap.add_argument("--min-vol", type=float, default=1.0)
    a = ap.parse_args()
    if a.scan:
        scan(a.min_vol)
    elif a.detail:
        detail(a.detail)
    else:
        show()


if __name__ == "__main__":
    main()

"""
kalshi_flow.py v3 — follow the money via series, not the raw market firehose.

v1/v2 lesson: /markets?status=open returns ~1.2M rows, ~99.5% of which are
KXMVECROSSCATEGORY auto-generated parlay combinations at zero volume. Paging
past them never reaches the real markets. So: enumerate SERIES by category,
then pull markets per series. Two orders of magnitude less data, all of it real.

    python3 kalshi_flow.py
    python3 kalshi_flow.py --categories Crypto Economics
"""

from __future__ import annotations

import argparse
import base64
import csv
import os
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(Path.home() / "trading-lab" / ".env")

BASE = os.environ["KALSHI_BASE_PROD"].rstrip("/")
KEY_ID = os.environ["KALSHI_KEY_ID"]
PREFIX = "/" + BASE.split("://", 1)[1].split("/", 1)[1]
_PK = serialization.load_pem_private_key(
    Path(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"])).read_bytes(),
    password=None,
)

OUT = Path.home() / "trading-lab" / "data" / "kalshi"
OUT.mkdir(parents=True, exist_ok=True)

CATEGORIES = ["Crypto", "Economics", "Financials", "Politics", "Climate",
              "Companies", "Culture", "Commodities", "Health", "World",
              "Technology", "Sports"]


def get(endpoint: str, **params):
    ts = str(int(time.time() * 1000))
    sig = _PK.sign(
        (ts + "GET" + PREFIX + endpoint).encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    r = requests.get(
        BASE + endpoint,
        headers={
            "KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
        },
        params=params or None,
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def series_in(category: str):
    try:
        d = get("/series", category=category)
    except Exception as e:
        print(f"  ! {category}: {e}")
        return []
    ser = d.get("series") or []
    return [s for s in ser
            if not (s.get("ticker") or "").startswith("KXMVE")]


def markets_for(series_ticker: str):
    out, cursor = [], None
    for _ in range(10):                     # a series never has 10k open markets
        p = {"series_ticker": series_ticker, "status": "open", "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        try:
            d = get("/markets", **p)
        except Exception:
            break
        b = d.get("markets", [])
        out.extend(b)
        cursor = d.get("cursor")
        if not cursor or not b:
            break
    return out


def _f(m,*names,default=0.0):
    for n in names:
        v=m.get(n)
        if v not in (None,""):
            try: return float(v)
            except (TypeError,ValueError): pass
    return default


def enrich(m: dict, cat: str) -> dict:
    yb=_f(m,"yes_bid_dollars","yes_bid")*100
    ya=_f(m,"yes_ask_dollars","yes_ask")*100
    last=_f(m,"last_price_dollars","last_price")*100
    mid=(yb+ya)/2 if ya else last
    try:
        days=(datetime.fromisoformat(m.get("close_time","").replace("Z","+00:00"))
              -datetime.now(timezone.utc)).total_seconds()/86400
    except Exception:
        days=None
    return {
        "ticker":m.get("ticker"),
        "category":cat,
        "series":m.get("series_ticker") or m["ticker"].split("-")[0],
        "event":m.get("event_ticker") or m["ticker"].rsplit("-",1)[0],
        "title":(m.get("title") or "")[:60],
        "subtitle":(m.get("subtitle") or "")[:28],
        "yes_bid":round(yb,1),
        "yes_ask":round(ya,1),
        "mid":round(mid,1),
        "spread":round(ya-yb,1) if ya else None,
        "bid_size":_f(m,"yes_bid_size_fp"),
        "ask_size":_f(m,"yes_ask_size_fp"),
        "volume":_f(m,"volume_fp","volume"),
        "volume_24h":_f(m,"volume_24h_fp","volume_24h"),
        "open_interest":_f(m,"open_interest_fp","open_interest"),
        "days_to_close":round(days,3) if days is not None else None,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", nargs="*", default=CATEGORIES)
    a = ap.parse_args()

    rows = []
    for cat in a.categories:
        try:
            ser = series_in(cat)
        except Exception as e:
            print(f"{cat}: skipped ({e})", flush=True)
            continue
        print(f"{cat}: {len(ser)} series", flush=True)
        for s in ser:
            t = s.get("ticker")
            if not t:
                continue
            for m in markets_for(t):
                rows.append(enrich(m, cat))
            time.sleep(0.05)
        print(f"   -> {len(rows):,} markets so far", flush=True)

    if not rows:
        print("\nno markets returned — /series may need different category names.")
        print("run: python3 -c \"from kalshi_flow import get;print(get('/series'))\"")
        return

    live = [r for r in rows if r["volume_24h"] > 0]
    print(f"\n{len(rows):,} open markets | {len(live):,} traded in last 24h\n")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    path = OUT / f"markets_{stamp}.csv"
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("=" * 84)
    print("CATEGORY BY 24H VOLUME")
    print("=" * 84)
    cats = defaultdict(lambda: {"n": 0, "v24": 0, "oi": 0})
    for r in rows:
        c = cats[r["category"]]
        c["n"] += 1
        c["v24"] += r["volume_24h"]
        c["oi"] += r["open_interest"]
    for name, c in sorted(cats.items(), key=lambda kv: -kv[1]["v24"]):
        print(f"{name:<16}{c['n']:>7} mkts{c['v24']:>14,.0f} vol24h{c['oi']:>14,.0f} OI")

    print("\n" + "=" * 84)
    print("SERIES BY 24H VOLUME — the families money keeps returning to")
    print("=" * 84)
    fam = defaultdict(lambda: {"n": 0, "v24": 0, "oi": 0, "cat": ""})
    for r in rows:
        f = fam[r["series"]]
        f["n"] += 1
        f["v24"] += r["volume_24h"]
        f["oi"] += r["open_interest"]
        f["cat"] = r["category"]
    for name, f in sorted(fam.items(), key=lambda kv: -kv[1]["v24"])[:30]:
        print(f"{name:<26}{f['cat']:<13}{f['n']:>6}{f['v24']:>13,.0f}{f['oi']:>13,.0f}")

    print("\n" + "=" * 84)
    print("TOP MARKETS BY 24H VOLUME")
    print("=" * 84)
    for r in sorted(rows, key=lambda r: -r["volume_24h"])[:30]:
        print(f"{r['volume_24h']:>11,.0f} {r['mid']:>5.1f}c sp{r['spread'] or 0:>3} "
              f"{str(r['days_to_close']):>8}d {r['ticker'][:32]:<33}{r['title'][:30]}")

    print("\n" + "=" * 84)
    print("LIQUID AND TIGHT (vol24h>500, spread<=2) — where $25 can trade")
    print("=" * 84)
    tight = [r for r in rows if r["volume_24h"] > 500 and (r["spread"] or 99) <= 2]
    for r in sorted(tight, key=lambda r: -r["volume_24h"])[:30]:
        print(f"{r['volume_24h']:>11,.0f} {r['mid']:>5.1f}c sp{r['spread']:>2} "
              f"{str(r['days_to_close']):>8}d {r['ticker'][:32]:<33}{r['title'][:30]}")
    print(f"\n{len(tight)} of {len(rows)} clear that bar.")

    print("\n" + "=" * 84)
    print("LADDERS — one event, 5+ strikes (structural-edge surface)")
    print("=" * 84)
    ev = defaultdict(lambda: {"n": 0, "v24": 0, "cat": ""})
    for r in rows:
        e = ev[r["event"]]
        e["n"] += 1
        e["v24"] += r["volume_24h"]
        e["cat"] = r["category"]
    for k, v in sorted([kv for kv in ev.items() if kv[1]["n"] >= 5],
                       key=lambda kv: -kv[1]["v24"])[:20]:
        print(f"{v['v24']:>11,.0f}  {v['n']:>4} strikes  {v['cat']:<12}{k}")

    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()

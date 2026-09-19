"""
weather_edge.py — price Kalshi temperature ladders against an ensemble
forecast, and measure whether the forecast is calibrated before trading it.

THE IDEA, and why it might work:
  A Kalshi temperature market asks "will the high be in bucket X". Pricing
  that needs a DISTRIBUTION over tomorrow's high. The public forecast
  everyone reads (NWS, phone apps) is a POINT estimate — one number. An
  ensemble runs the model ~30 times from slightly different starting
  states and returns a spread, which IS the distribution.

  If the market prices off the point forecast and the ensemble disagrees
  about the spread, the strikes away from the centre are mispriced.

  That is a hypothesis. It is not established. Which is why --verify
  exists and why nothing here places an order.

ORDER OF WORK — do not skip:
  1.  --scan     see which Kalshi weather markets exist and are liquid
  2.  --collect  log ensemble forecast + market price daily (takes weeks)
  3.  --verify   did the ensemble actually predict the outcome? calibration
  4.  --price    only meaningful AFTER verify shows the forecast is honest

    python3 weather_edge.py --scan
    python3 weather_edge.py --price --city NY
    python3 weather_edge.py --collect
    python3 weather_edge.py --verify
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
LOG = ROOT / "forecast_log.csv"
FIELDS = ["logged_at", "target_date", "city", "ticker", "strike_low",
          "strike_high", "market_mid", "market_bid", "market_ask",
          "ens_prob", "ens_mean", "ens_sd", "ens_n", "point_forecast",
          "actual_high"]

ENSEMBLE = "https://ensemble-api.open-meteo.com/v1/ensemble"
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
NWS = "https://api.weather.gov"
UA = {"User-Agent": "trading-lab research (contact via kalshi account)"}

# Kalshi US temperature cities. Coordinates are the official observation
# station, because that is what settles the contract — NOT the city centre.
# A wrong station is a silently wrong forecast.
# Coordinates are the OBSERVING STATION named in each CLI product, which
# is what settles the contract. AUS is Austin-BERGSTROM (CLIAUS), not
# Camp Mabry (CLIATT) — a separate product and a different thermometer.
CITIES = {
    "NY":   {"name": "Central Park (CLINYC)", "lat": 40.779, "lon": -73.969,
             "series": "KXHIGHNY", "unit": "F", "pil": "CLINYC"},
    "CHI":  {"name": "Chicago Midway (CLIMDW)", "lat": 41.786, "lon": -87.752,
             "series": "KXHIGHCHI", "unit": "F", "pil": "CLIMDW"},
    "MIA":  {"name": "Miami Intl (CLIMIA)", "lat": 25.788, "lon": -80.317,
             "series": "KXHIGHMIA", "unit": "F", "pil": "CLIMIA"},
    "AUS":  {"name": "Austin-Bergstrom (CLIAUS)", "lat": 30.183, "lon": -97.680,
             "series": "KXHIGHAUS", "unit": "F", "pil": "CLIAUS"},
    "DEN":  {"name": "Denver Intl (CLIDEN)", "lat": 39.862, "lon": -104.673,
             "series": "KXHIGHDEN", "unit": "F", "pil": "CLIDEN"},
    "LAX":  {"name": "Los Angeles Intl (CLILAX)", "lat": 33.938, "lon": -118.389,
             "series": "KXHIGHLAX", "unit": "F", "pil": "CLILAX"},
    "PHIL": {"name": "Philadelphia Intl (CLIPHL)", "lat": 39.873, "lon": -75.227,
             "series": "KXHIGHPHIL", "unit": "F", "pil": "CLIPHL"},
}



MONTHS = {"JAN":1,"FEB":2,"MAR":3,"APR":4,"MAY":5,"JUN":6,
          "JUL":7,"AUG":8,"SEP":9,"OCT":10,"NOV":11,"DEC":12}


def event_date(event_ticker, close_time=""):
    """Local target date from the event ticker, e.g. KXHIGHLAX-26AUG30
    -> 2026-08-30. close_time is UTC and rolls past midnight for western
    cities, which mapped every market to the FOLLOWING day and produced
    fake edges. Ticker is unambiguous; use it."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})", event_ticker or "")
    if m:
        yy, mon, dd = m.groups()
        if mon in MONTHS:
            return f"20{yy}-{MONTHS[mon]:02d}-{int(dd):02d}"
    return close_time[:10] if close_time else None



CALIB = ROOT / "calibration_cli.json"   # built from actual settlement values


def load_calibration():
    """Per-city bias, residual sd and BEST MODEL, measured against the NWS
    CLI value that actually settles the contract (calibrate2.py).

    The earlier table was measured against reanalysis, not settlement.
    It said Austin was +1.97F warm; against settlement Austin is -1.77F
    cold. Correcting with the old table moved the forecast 3.7F the
    wrong way."""
    if not CALIB.exists():
        return {}
    try:
        return json.loads(CALIB.read_text())
    except Exception:
        return {}


def _phi(x):
    """Standard normal CDF."""
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def prob_bucket_calibrated(mean, sigma, lo, hi):
    """P(rounded high lands in [lo,hi]) under N(mean, sigma).
    Bucket edges are widened by 0.5F because Kalshi settles on the
    ROUNDED integer high: a 79.6F reading settles the '80' bucket."""
    if sigma <= 0:
        sigma = 0.5
    a = (lo - 0.5 - mean) / sigma if lo is not None else None
    b = (hi + 0.5 - mean) / sigma if hi is not None else None
    if a is None and b is None:
        return None
    if a is None:
        return _phi(b)
    if b is None:
        return 1.0 - _phi(a)
    return max(0.0, _phi(b) - _phi(a))


# ------------------------------------------------------------------ kalshi

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


def f(m, *names, default=None):
    for n in names:
        v = m.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def kalshi_weather_markets(series):
    """Open markets in a temperature series, with strike bounds read from
    the API (floor/cap), never parsed out of the ticker."""
    try:
        d = kalshi_get("/markets", series_ticker=series, status="open", limit=1000)
    except Exception as e:
        return [], str(e)
    out = []
    for m in d.get("markets", []):
        bid = (f(m, "yes_bid_dollars", "yes_bid", default=0.0)) * 100
        ask = (f(m, "yes_ask_dollars", "yes_ask", default=0.0)) * 100
        out.append({
            "ticker": m["ticker"],
            "event": m.get("event_ticker", ""),
            "stype": (m.get("strike_type") or "").lower(),
            "floor": f(m, "floor_strike_dollars", "floor_strike"),
            "cap": f(m, "cap_strike_dollars", "cap_strike"),
            "sub": (m.get("subtitle") or m.get("yes_sub_title") or ""),
            "title": m.get("title", ""),
            "bid": bid, "ask": ask,
            "bid_sz": int(f(m, "yes_bid_size_fp", default=0.0)),
            "ask_sz": int(f(m, "yes_ask_size_fp", default=0.0)),
            "mid": (bid + ask) / 2 if ask else 0.0,
            "spread": ask - bid if ask else None,
            "vol24": f(m, "volume_24h_fp", "volume_24h", default=0.0),
            "oi": f(m, "open_interest_fp", "open_interest", default=0.0),
            "close": m.get("close_time", ""),
            "rules": (m.get("rules_primary") or "")[:400],
        })
    return out, None


# ---------------------------------------------------------------- forecast

def ensemble_daily_max(lat, lon, unit="F", days=3, model="gfs_seamless"):
    """Every member's forecast of the daily maximum.

    ONE model family only. Blending gfs+icon+ecmwf measures disagreement
    BETWEEN models on top of genuine forecast uncertainty, which inflates
    the spread and makes tail strikes look cheap. sd was 4.2F on a
    next-day LA forecast; real one-day error is 2-3F."""
    r = requests.get(ENSEMBLE, params={
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m",
        "models": model,
        "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
        "forecast_days": days, "timezone": "auto"}, timeout=45)
    r.raise_for_status()
    d = r.json()
    h = d.get("hourly", {})
    times = h.get("time", [])
    if not times:
        return {}
    member_keys = [k for k in h if k.startswith("temperature_2m")]
    by_day = defaultdict(lambda: defaultdict(list))
    for k in member_keys:
        vals = h[k]
        for t, v in zip(times, vals):
            if v is None:
                continue
            by_day[t[:10]][k].append(v)
    out = {}
    for day, members in by_day.items():
        maxes = [max(v) for v in members.values() if v]
        if len(maxes) >= 3:
            out[day] = maxes
    return out


def nws_point_forecast(lat, lon):
    """The headline number most people see. Used as a comparison, not as
    the model — a point forecast cannot price a bucket."""
    try:
        p = requests.get(f"{NWS}/points/{lat},{lon}", headers=UA, timeout=25)
        p.raise_for_status()
        url = p.json()["properties"]["forecast"]
        fc = requests.get(url, headers=UA, timeout=25)
        fc.raise_for_status()
        out = {}
        for per in fc.json()["properties"]["periods"]:
            if per.get("isDaytime"):
                out[per["startTime"][:10]] = per["temperature"]
        return out
    except Exception:
        return {}


def actual_high(lat, lon, day, unit="F"):
    """Observed daily max from the reanalysis archive, for --verify."""
    try:
        r = requests.get(ARCHIVE, params={
            "latitude": lat, "longitude": lon,
            "start_date": day, "end_date": day,
            "daily": "temperature_2m_max",
            "temperature_unit": "fahrenheit" if unit == "F" else "celsius",
            "timezone": "auto"}, timeout=30)
        r.raise_for_status()
        v = r.json()["daily"]["temperature_2m_max"]
        return float(v[0]) if v and v[0] is not None else None
    except Exception:
        return None


# ------------------------------------------------------------------ pricing

def prob_in_bucket(members, lo, hi):
    """Share of ensemble members whose forecast max lands in [lo, hi].
    Kalshi temperature buckets are inclusive integer ranges."""
    if not members:
        return None
    n = len(members)
    if lo is None and hi is None:
        return None
    hits = 0
    for v in members:
        r = round(v)
        if lo is not None and r < lo:
            continue
        if hi is not None and r > hi:
            continue
        hits += 1
    return hits / n


def fee_cents(price_c, contracts=1):
    import math
    p = price_c / 100
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100


# -------------------------------------------------------------------- modes

def cmd_scan(a):
    print("Kalshi temperature series — what exists and what trades\n")
    print(f"{'city':<6}{'series':<14}{'mkts':>6}{'vol24':>10}{'OI':>10}"
          f"{'tight':>7}  next event")
    print("-" * 78)
    any_found = False
    for code, c in CITIES.items():
        mk, err = kalshi_weather_markets(c["series"])
        if err:
            print(f"{code:<6}{c['series']:<14}  error: {err[:40]}")
            continue
        if not mk:
            print(f"{code:<6}{c['series']:<14}{0:>6}  (no open markets)")
            continue
        any_found = True
        vol = sum(m["vol24"] for m in mk)
        oi = sum(m["oi"] for m in mk)
        tight = sum(1 for m in mk if m["ask"] > 0 and (m["spread"] or 99) <= 2)
        ev = sorted({m["event"] for m in mk})
        print(f"{code:<6}{c['series']:<14}{len(mk):>6}{vol:>10,.0f}{oi:>10,.0f}"
              f"{tight:>7}  {ev[0] if ev else ''}")
        time.sleep(0.1)
    if not any_found:
        print("\nNo open temperature markets found under these series names.")
        print("Kalshi renames series. Run kalshi_flow.py --categories Climate")
        print("and look for the current temperature tickers.")
    else:
        print("\n'tight' counts strikes with a two-sided quote at <=2c wide.")
        print("Those are the only ones a small account can trade.")




def strike_bounds(m):
    """Inclusive integer bounds on the SETTLED high for one contract.

    The rules are strict inequalities on whole degrees:
      "greater than 79F ... resolves Yes"  -> settles Yes on 80 or above
      "less than 97F    ... resolves Yes"  -> settles Yes on 96 or below

    v4 used floor=79 directly, which priced P(T > 78.5) and overlapped the
    78-79 bucket. Model probabilities then summed to 123-129% against a
    market summing to 102, and every 'edge' was inflated by the overlap.
    """
    st_ = (m.get("stype") or "").lower()
    floor, cap = m.get("floor"), m.get("cap")
    if st_ == "greater":
        return (floor + 1 if floor is not None else None), None
    if st_ == "less":
        return None, (cap - 1 if cap is not None else None)
    return floor, cap          # 'between' bounds are already inclusive


def partition_legs(legs):
    """Kalshi weather ladders mix contract shapes in one event:

        B80.5  strike_type 'between'  floor 80 cap 81   a bucket
        T83    strike_type 'greater'  floor 83          everything above

    The 'greater' leg CONTAINS every bucket above its floor. Summing the
    model probability across all of them double-counts and produced totals
    of 123-129% against a market that sums to 102%.

    Keep the mutually exclusive set: all 'between' buckets, plus the
    'less' tail, plus ONLY the 'greater' leg whose floor sits above every
    bucket (the open top of the ladder). Any 'greater' leg that overlaps a
    bucket is priced but excluded from the coherence total.
    """
    buckets = [m for m in legs if m["stype"] == "between"
               and m["floor"] is not None and m["cap"] is not None]
    lows = [m for m in legs if m["stype"] == "less" and m["cap"] is not None]
    highs = [m for m in legs if m["stype"] == "greater" and m["floor"] is not None]

    top_cap = max((m["cap"] for m in buckets), default=None)
    bot_floor = min((m["floor"] for m in buckets), default=None)

    keep = list(buckets)
    for m in lows:
        _, hi = strike_bounds(m)
        if bot_floor is None or (hi is not None and hi < bot_floor):
            keep.append(m)
    for m in highs:
        lo, _ = strike_bounds(m)
        if top_cap is None or (lo is not None and lo > top_cap):
            keep.append(m)

    overlapping = [m for m in legs if m not in keep]
    return keep, overlapping


def orderbook(ticker):
    """Resting size on each side. A 20c edge on 3 contracts is not a trade."""
    try:
        d = kalshi_get(f"/markets/{ticker}/orderbook", depth=8)
    except Exception:
        return None
    ob = d.get("orderbook") or {}
    def side(k):
        rows = ob.get(k) or []
        out = []
        for r in rows:
            try:
                out.append((float(r[0]), float(r[1])))
            except Exception:
                pass
        return out
    return {"yes": side("yes"), "no": side("no")}


def cmd_price(a):
    cal = load_calibration()
    if not cal:
        print("NO CALIBRATION TABLE. Run:  python3 calibrate.py --build --days 365")
        print("Without it these are raw model numbers and they are biased.\n")

    codes = [a.city.upper()] if a.city else list(CITIES)
    for code in codes:
        c = CITIES.get(code)
        if not c:
            continue
        mk, err = kalshi_weather_markets(c["series"])
        if err or not mk:
            print(f"\n{code}: no open markets ({err or 'empty'})")
            continue
        k = cal.get(code, {})
        bias = k.get("bias_f", 0.0)
        sigma = k.get("resid_sd_f", 0.0)
        within1 = k.get("within_1F_corrected")
        model = k.get("model", "gfs_seamless")
        try:
            ens = ensemble_daily_max(c["lat"], c["lon"], c["unit"], model=model)
        except Exception as e:
            print(f"{code}: ensemble failed {e}")
            continue
        pt = nws_point_forecast(c["lat"], c["lon"])

        events = defaultdict(list)
        for m in mk:
            events[m["event"]].append(m)

        for ev, legs in sorted(events.items()):
            day = event_date(ev, legs[0]["close"] if legs else "")
            members = ens.get(day)
            if not members:
                continue
            try:
                horizon = (datetime.fromisoformat(day).date()
                           - datetime.now().date()).days
            except Exception:
                horizon = None
            if horizon is not None and horizon < 0:
                continue

            raw_mean = st.mean(members)
            raw_sd = st.pstdev(members) if len(members) > 1 else 0.0
            use_bias, bias_src = bias, "annual"
            mb = k.get("bias_by_month", {})
            mkey = str(int(day[5:7])) if day else None
            if mkey and mkey in mb and abs(mb[mkey] - bias) > 1.0:
                use_bias, bias_src = mb[mkey], f"month {mkey}"
            adj_mean = raw_mean - use_bias
            # use the measured residual sd; fall back to ensemble spread
            use_sd = sigma if sigma > 0 else max(raw_sd, 1.0)

            print("\n" + "=" * 96)
            print(f"{c['name']}   target {day}"
                  + (f"   ({horizon}d ahead)" if horizon is not None else ""))
            print(f"  raw ensemble  {raw_mean:.1f}F  own spread {raw_sd:.2f}F"
                  f"   ({len(members)} members)")
            if k:
                print(f"  calibrated    {adj_mean:.1f}F  sigma {use_sd:.2f}F"
                      f"   (model {model}, bias {use_bias:+.2f}F [{bias_src}]"
                      f" removed, from {k.get('n')} settled days)")
                if within1 is not None:
                    tag = ("TRADEABLE" if within1 >= 50 else
                           "marginal" if within1 >= 40 else "TOO COARSE")
                    print(f"  vs actual settlement: within 1F on {within1:.1f}%"
                          f" of {k.get('n')} days  -> {tag}")
            else:
                print("  NOT CALIBRATED. Run:")
            print("     python3 cli_data.py --history --days 400")
            print("     python3 calibrate2.py --build")
            if day in pt:
                print(f"  NWS headline  {pt[day]}F   (what the market likely prices off)")
            print("=" * 96)
            print(f"{'strike':>12}{'bid':>6}{'ask':>6}{'mid':>7}"
                  f"{'model%':>8}{'raw%':>7}{'edge':>7}{'net':>7}"
                  f"{'bidsz':>8}{'asksz':>8}{'vol24':>9}")

            excl_set = {id(m) for m in partition_legs(legs)[0]}
            hits = []
            for m in sorted(legs, key=lambda x: (x["floor"] if x["floor"]
                                                 is not None else -999)):
                lo, hi = strike_bounds(m)
                p_cal = prob_bucket_calibrated(adj_mean, use_sd, lo, hi)
                p_raw = prob_in_bucket(members, lo, hi)
                if p_cal is None:
                    continue
                pc = p_cal * 100
                rawc = (p_raw * 100) if p_raw is not None else float("nan")
                label = (f"{lo:.0f}-{hi:.0f}" if lo is not None and hi is not None
                         else (f">{lo:.0f}" if lo is not None else f"<{hi:.0f}"))
                if m["ask"] > 0:
                    edge = pc - m["ask"]
                    net = edge - fee_cents(m["ask"])
                else:
                    edge = net = float("nan")
                is_excl = id(m) in excl_set
                flag = ""
                if not is_excl:
                    flag = "  (overlaps)"
                elif net == net and net >= a.min_edge:
                    flag = "  <<<"
                    hits.append((m, lo, hi, pc, net))
                print(f"{label:>12}{m['bid']:>6.0f}{m['ask']:>6.0f}{m['mid']:>7.1f}"
                      f"{pc:>8.1f}{rawc:>7.1f}{edge:>+7.1f}{net:>+7.1f}"
                      f"{m['bid_sz'] if 'bid_sz' in m else 0:>8}"
                      f"{m['ask_sz'] if 'ask_sz' in m else 0:>8}"
                      f"{m['vol24']:>9,.0f}{flag}")

            # coherence on the MUTUALLY EXCLUSIVE set only
            excl, overlap = partition_legs(legs)
            tot = 0.0
            for m in excl:
                lo, hi = strike_bounds(m)
                v = prob_bucket_calibrated(adj_mean, use_sd, lo, hi)
                if v:
                    tot += v
            mkt_tot = sum(m["mid"] for m in excl if m["ask"] > 0)
            print(f"\n  exclusive set: {len(excl)} legs"
                  + (f" ({len(overlap)} overlapping leg(s) excluded from the total)"
                     if overlap else ""))
            print(f"  model sums to {tot*100:.1f}   market mids sum to {mkt_tot:.1f}")
            if abs(tot * 100 - 100) > 8:
                print("  WARNING even the exclusive set does not sum to ~100.")
                print("  Sigma or bias is off. Do not trust these edges.")

            if hits and a.depth:
                print("\n  BOOK DEPTH on flagged strikes:")
                for m, lo, hi, pc, net in hits[:4]:
                    ob = orderbook(m["ticker"])
                    if not ob:
                        print(f"    {m['ticker']}: book unavailable")
                        continue
                    yes = ob["yes"][:4]
                    print(f"    {m['ticker']}  model {pc:.0f}c vs ask {m['ask']:.0f}c"
                          f"  net {net:+.1f}c")
                    if yes:
                        lvl = "  ".join(f"{p:.0f}c x{q:,.0f}" for p, q in yes)
                        print(f"      resting: {lvl}")
                    time.sleep(0.15)

            if hits:
                print(f"\n  {len(hits)} strike(s) over {a.min_edge:.0f}c net.")
                if within1 is not None and within1 < 50:
                    print("  BUT this city is TOO COARSE historically. A forecast")
                    print("  that lands within 1F on {:.0f}% of days cannot price"
                          .format(within1))
                    print("  a 1F bucket. These edges are model error, not edge.")
                else:
                    print("  Still unproven. --collect and --verify decide it.")
            else:
                print(f"\n  nothing over {a.min_edge:.0f}c net.")


def _load_log():
    if not LOG.exists():
        return []
    with LOG.open() as fh:
        return list(csv.DictReader(fh))


def cmd_collect(a):
    """Log today's ensemble probability beside today's market price, for
    every liquid strike. This is the dataset --verify needs."""
    rows = _load_log()
    seen = {(r["logged_at"][:10], r["ticker"]) for r in rows}
    today = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0

    for code, c in CITIES.items():
        mk, err = kalshi_weather_markets(c["series"])
        if err or not mk:
            continue
        try:
            ens = ensemble_daily_max(c["lat"], c["lon"], c["unit"], model=model)
        except Exception as e:
            print(f"{code}: ensemble failed {e}")
            continue
        pt = nws_point_forecast(c["lat"], c["lon"])

        for m in mk:
            if m["ask"] <= 0:
                continue
            day = event_date(m["event"], m["close"])
            members = ens.get(day)
            if not members:
                continue
            if (today[:10], m["ticker"]) in seen:
                continue
            lo, hi = strike_bounds(m)
            p = prob_in_bucket(members, lo, hi)
            if p is None:
                continue
            rows.append({
                "logged_at": today, "target_date": day, "city": code,
                "ticker": m["ticker"],
                "strike_low": lo if lo is not None else "",
                "strike_high": hi if hi is not None else "",
                "market_mid": round(m["mid"], 1),
                "market_bid": round(m["bid"], 1),
                "market_ask": round(m["ask"], 1),
                "ens_prob": round(p * 100, 2),
                "ens_mean": round(st.mean(members), 2),
                "ens_sd": round(st.pstdev(members), 3) if len(members) > 1 else 0,
                "ens_n": len(members),
                "point_forecast": pt.get(day, ""),
                "actual_high": "",
            })
            added += 1
        print(f"{code}: logged", flush=True)
        time.sleep(0.3)

    with LOG.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"\n+{added} rows, {len(rows)} total -> {LOG}")


def cmd_verify(a):
    """Fill in actual outcomes, then answer the only question that matters:
    is the ensemble probability calibrated? When it says 30%, does it happen
    30% of the time?"""
    rows = _load_log()
    if not rows:
        print("no log yet — run --collect daily first")
        return

    filled = 0
    cache = {}
    today = datetime.now(timezone.utc).date()
    for r in rows:
        if r.get("actual_high"):
            continue
        d = r.get("target_date") or ""
        if not d:
            continue
        try:
            if datetime.fromisoformat(d).date() >= today:
                continue          # not resolved yet
        except Exception:
            continue
        c = CITIES.get(r["city"])
        if not c:
            continue
        key = (r["city"], d)
        if key not in cache:
            cache[key] = actual_high(c["lat"], c["lon"], d, c["unit"])
            time.sleep(0.3)
        if cache[key] is not None:
            r["actual_high"] = round(cache[key], 1)
            filled += 1

    with LOG.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    print(f"filled {filled} outcomes\n")

    done = [r for r in rows if r.get("actual_high") not in (None, "")]
    if len(done) < 30:
        print(f"only {len(done)} resolved observations. Need 100+ before the")
        print("calibration table means anything. Keep running --collect.")
        return

    buckets = defaultdict(lambda: {"n": 0, "hit": 0})
    for r in rows:
        if not r.get("actual_high"):
            continue
        try:
            actual = round(float(r["actual_high"]))
            p = float(r["ens_prob"])
        except Exception:
            continue
        lo = float(r["strike_low"]) if r["strike_low"] else None
        hi = float(r["strike_high"]) if r["strike_high"] else None
        hit = True
        if lo is not None and actual < lo:
            hit = False
        if hi is not None and actual > hi:
            hit = False
        b = min(int(p // 10) * 10, 90)
        buckets[b]["n"] += 1
        buckets[b]["hit"] += 1 if hit else 0

    print("=" * 68)
    print("CALIBRATION — does the ensemble mean what it says?")
    print("=" * 68)
    print(f"{'forecast':>12}{'n':>7}{'actual':>10}{'error':>9}")
    errs = []
    for b in sorted(buckets):
        v = buckets[b]
        if v["n"] < 5:
            continue
        actual = v["hit"] / v["n"] * 100
        mid = b + 5
        errs.append(abs(actual - mid) * v["n"])
        print(f"{b:>4}-{b+10:<7}{v['n']:>7}{actual:>9.1f}%{actual-mid:>+9.1f}")
    tot = sum(v["n"] for v in buckets.values() if v["n"] >= 5)
    if tot:
        print(f"\n  weighted mean absolute error: {sum(errs)/tot:.1f} points")
        print("  Under ~5 points is usable. Over ~10 and the probabilities are")
        print("  wrong, so every 'edge' computed from them is wrong too.")

    print("\n" + "=" * 68)
    print("ENSEMBLE vs MARKET — who is closer to the outcome?")
    print("=" * 68)
    e_err, m_err = [], []
    for r in rows:
        if not r.get("actual_high"):
            continue
        try:
            actual = round(float(r["actual_high"]))
            p = float(r["ens_prob"]) / 100
            mid = float(r["market_mid"]) / 100
        except Exception:
            continue
        lo = float(r["strike_low"]) if r["strike_low"] else None
        hi = float(r["strike_high"]) if r["strike_high"] else None
        y = 1.0
        if lo is not None and actual < lo:
            y = 0.0
        if hi is not None and actual > hi:
            y = 0.0
        e_err.append((p - y) ** 2)
        m_err.append((mid - y) ** 2)
    if e_err:
        eb, mb = st.mean(e_err), st.mean(m_err)
        print(f"  ensemble Brier score  {eb:.4f}")
        print(f"  market   Brier score  {mb:.4f}   (lower is better)")
        if eb < mb:
            print(f"\n  Ensemble beats the market by {mb-eb:.4f}. That is the")
            print("  precondition for an edge — necessary, not sufficient.")
            print("  Next: check the edge survives spread and fees, on the")
            print("  strikes that actually have a two-sided quote.")
        else:
            print("\n  The market is at least as good as the ensemble.")
            print("  There is no edge here. That is a finding — record it.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--price", action="store_true")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--city")
    ap.add_argument("--depth", action="store_true",
                    help="pull the order book on flagged strikes")
    ap.add_argument("--min-edge", type=float, default=5.0,
                    help="net cents per contract to flag")
    a = ap.parse_args()
    if a.scan:
        cmd_scan(a)
    elif a.price:
        cmd_price(a)
    elif a.collect:
        cmd_collect(a)
    elif a.verify:
        cmd_verify(a)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

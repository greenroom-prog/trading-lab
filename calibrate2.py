"""
calibrate2.py — calibrate the forecast against the ACTUAL SETTLEMENT VALUE.

calibrate.py compared Open-Meteo forecasts to Open-Meteo reanalysis.
Neither is what settles a Kalshi contract. This compares forecasts to the
NWS Daily Climate Report high collected by cli_data.py — the number the
contract pays on.

Requires: data/weather/cli_observed.csv  (python3 cli_data.py --history)

    python3 calibrate2.py --build
    python3 calibrate2.py --show
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

ROOT = Path.home() / "trading-lab" / "data" / "weather"
OBS = ROOT / "cli_observed.csv"
TABLE = ROOT / "calibration_cli.json"
HIST_FC = "https://historical-forecast-api.open-meteo.com/v1/forecast"

# coordinates of the OBSERVING STATION named in each CLI product
CITIES = {
    "NY":   {"name": "Central Park (CLINYC)",       "lat": 40.779, "lon": -73.969},
    "CHI":  {"name": "Chicago Midway (CLIMDW)",     "lat": 41.786, "lon": -87.752},
    "MIA":  {"name": "Miami Intl (CLIMIA)",         "lat": 25.788, "lon": -80.317},
    "AUS":  {"name": "Austin-Bergstrom (CLIAUS)",   "lat": 30.183, "lon": -97.680},
    "DEN":  {"name": "Denver Intl (CLIDEN)",        "lat": 39.862, "lon": -104.673},
    "LAX":  {"name": "Los Angeles Intl (CLILAX)",   "lat": 33.938, "lon": -118.389},
    "PHIL": {"name": "Philadelphia Intl (CLIPHL)",  "lat": 39.873, "lon": -75.227},
}


def load_observed():
    if not OBS.exists():
        raise SystemExit("no cli_observed.csv — run: python3 cli_data.py --history")
    by = {}
    with OBS.open() as fh:
        for r in csv.DictReader(fh):
            try:
                by.setdefault(r["city"], {})[r["date"]] = float(r["max_f"])
            except (TypeError, ValueError):
                continue
    return by


def past_forecasts(lat, lon, start, end, model="gfs_seamless"):
    r = requests.get(HIST_FC, params={
        "latitude": lat, "longitude": lon,
        "start_date": start, "end_date": end,
        "daily": "temperature_2m_max", "models": model,
        "temperature_unit": "fahrenheit", "timezone": "auto"}, timeout=90)
    r.raise_for_status()
    d = r.json().get("daily", {})
    return {t: v for t, v in zip(d.get("time", []),
                                 d.get("temperature_2m_max", []))
            if v is not None}


def build(models):
    obs = load_observed()
    out = {}
    for code, c in CITIES.items():
        o = obs.get(code, {})
        if len(o) < 100:
            print(f"{code}: only {len(o)} settlement values, skipping")
            continue
        days = sorted(o)
        start, end = days[0], days[-1]
        print(f"{code} {c['name']}  {len(o)} settled days {start}..{end}", flush=True)

        best = None
        for model in models:
            try:
                fc = past_forecasts(c["lat"], c["lon"], start, end, model)
            except Exception as e:
                print(f"   {model}: fetch failed {e}")
                continue
            pairs = [(d, fc[d], o[d]) for d in sorted(set(fc) & set(o))]
            if len(pairs) < 100:
                print(f"   {model}: only {len(pairs)} paired days")
                continue
            errs = [f - a for _, f, a in pairs]
            bias = st.mean(errs)
            resid = [e - bias for e in errs]
            sd = st.pstdev(resid)
            mae = st.mean(abs(e) for e in errs)
            # after removing bias, how often within 1F of the settled value
            w1 = sum(1 for e in resid if abs(e) <= 1.0) / len(resid) * 100
            w2 = sum(1 for e in resid if abs(e) <= 2.0) / len(resid) * 100
            rec = {"model": model, "n": len(pairs), "bias_f": round(bias, 3),
                   "resid_sd_f": round(sd, 3), "mae_f": round(mae, 3),
                   "within_1F_corrected": round(w1, 1),
                   "within_2F_corrected": round(w2, 1),
                   "start": pairs[0][0], "end": pairs[-1][0]}
            by_month = {}
            for d, f_, a_ in pairs:
                by_month.setdefault(int(d[5:7]), []).append(f_ - a_)
            rec["bias_by_month"] = {str(m): round(st.mean(v), 2)
                                    for m, v in sorted(by_month.items())
                                    if len(v) >= 8}
            print(f"   {model:<16} bias {bias:+.2f}F  sd {sd:.2f}F  "
                  f"MAE {mae:.2f}F  within1F {w1:.0f}%")
            if best is None or rec["resid_sd_f"] < best["resid_sd_f"]:
                best = rec
            time.sleep(0.4)
        if best:
            best["name"] = c["name"]
            best["lat"], best["lon"] = c["lat"], c["lon"]
            out[code] = best
    if out:
        TABLE.write_text(json.dumps(out, indent=2))
        print(f"\nwrote {TABLE}")
        show()


def show():
    if not TABLE.exists():
        print("no table — run --build")
        return
    t = json.loads(TABLE.read_text())
    print("\n" + "=" * 92)
    print("CALIBRATION AGAINST ACTUAL SETTLEMENT VALUES (NWS CLI)")
    print("=" * 92)
    print(f"{'city':<6}{'model':<16}{'n':>5}{'bias':>8}{'sd':>7}{'MAE':>7}"
          f"{'w/in 1F':>9}{'w/in 2F':>9}")
    for code, v in sorted(t.items(), key=lambda kv: -kv[1]["within_1F_corrected"]):
        print(f"{code:<6}{v['model']:<16}{v['n']:>5}{v['bias_f']:>+8.2f}"
              f"{v['resid_sd_f']:>7.2f}{v['mae_f']:>7.2f}"
              f"{v['within_1F_corrected']:>8.0f}%{v['within_2F_corrected']:>8.0f}%")

    print("\n" + "=" * 92)
    print("CAN IT PRICE A 1-2F BUCKET?")
    print("=" * 92)
    for code, v in sorted(t.items(), key=lambda kv: -kv[1]["within_1F_corrected"]):
        w = v["within_1F_corrected"]
        verdict = ("TRADEABLE" if w >= 50 else
                   "marginal" if w >= 40 else "TOO COARSE — skip")
        print(f"  {code:<6}{v['name']:<32} within 1F {w:>5.1f}%   {verdict}")
    print("\n  These percentages are measured against the number that actually")
    print("  settles the contract, after removing each city's constant bias.")

    print("\n" + "=" * 92)
    print("SEASONAL BIAS — where a single annual correction is not enough")
    print("=" * 92)
    for code, v in sorted(t.items()):
        b = v["bias_f"]
        off = {m: x for m, x in v.get("bias_by_month", {}).items()
               if abs(x - b) > 1.5}
        if off:
            print(f"  {code}: annual {b:+.2f}F, but months "
                  + ", ".join(f"{m}={x:+.1f}" for m, x in off.items()))
    print("\n  Use the monthly figure for those months, not the annual one.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--models", nargs="*",
                    default=["gfs_seamless", "ecmwf_ifs025", "icon_seamless"])
    a = ap.parse_args()
    if a.build:
        build(a.models)
    elif a.show:
        show()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()

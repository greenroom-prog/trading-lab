"""
live_edge.py — stream the settlement input, price the ladder, log every
comparison. Does not trade.

    python3 live_edge.py --series KXBTCD --secs 300
    python3 live_edge.py --replay tests/fixtures/brti_ticks.json   # no network

Each cycle:
  1. take the newest BRTI value and its live 60-second average
  2. estimate sigma from the tick stream (never assumed)
  3. compute sd of the settling average given seconds remaining
  4. price every strike, compare to the live book, subtract fees
  5. write a row per strike to data/live_edge_<date>.csv

The log is the point. One session proves nothing; a few hundred rows
with outcomes attached tells you whether the model beats the market.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

import settle_model as sm

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)


def _env():
    from dotenv import load_dotenv
    for p in (ROOT / ".env", Path.home() / "trading-lab" / ".env"):
        if p.exists():
            load_dotenv(p)
            return
    load_dotenv()


_env()
REST = os.environ.get("KALSHI_BASE_PROD", "").rstrip("/")
KEY_ID = os.environ.get("KALSHI_KEY_ID", "")
WS_PATH = "/trade-api/ws/v2"
_WS = [os.getenv("KALSHI_WS_URL"),
       "wss://external-api-ws.kalshi.com" + WS_PATH,
       "wss://api.elections.kalshi.com" + WS_PATH,
       "wss://external-api.kalshi.com" + WS_PATH]
WS_CANDIDATES = [u for u in _WS if u]

_PK = None
if os.environ.get("KALSHI_PRIVATE_KEY_PATH"):
    from cryptography.hazmat.primitives import serialization
    p = Path(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"]))
    if p.exists():
        _PK = serialization.load_pem_private_key(p.read_bytes(), password=None)


def sign(method, path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    ts = str(int(time.time() * 1000))
    sig = _PK.sign((ts + method + path).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                               salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID, "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def rest_get(ep, **params):
    prefix = "/" + REST.split("://", 1)[1].split("/", 1)[0 + 1]
    r = requests.get(REST + ep, headers=sign("GET", prefix + ep),
                     params=params or None, timeout=20)
    r.raise_for_status()
    return r.json()


def now():
    return datetime.now(timezone.utc)


def secs_to(close_iso):
    try:
        t = datetime.fromisoformat(close_iso.replace("Z", "+00:00"))
        return (t - now()).total_seconds()
    except Exception:
        return None


def soonest_event(series):
    """The next settling event in a series, with its strikes."""
    d = rest_get("/markets", series_ticker=series, status="open", limit=1000)
    ev = {}
    for m in d.get("markets", []):
        ev.setdefault(m.get("event_ticker", "?"), []).append(m)
    best, best_t = None, None
    for k, legs in ev.items():
        t = secs_to(legs[0].get("close_time", ""))
        if t is None or t <= 0:
            continue
        if best_t is None or t < best_t:
            best, best_t = (k, legs), t
    return best, best_t


HDR = ["t", "series", "event", "ticker", "label", "secs_left", "spot",
       "avg60s", "centre", "sigma_1s", "sd_settle", "model_pc",
       "bid", "ask", "bid_sz", "ask_sz", "yes_net", "no_net", "side", "net"]


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HDR)
        if not exists:
            w.writeheader()
        w.writerows(rows)


class Engine:
    """Holds tick state and prices a ladder on demand. Network-free, so it
    can be driven by the live socket or by a replay file."""

    def __init__(self, series, min_edge=2.0, out=None):
        self.series = series
        self.min_edge = min_edge
        self.ticks = sm.TickStats()
        self.spot = None
        self.avg60 = None
        self.window_n = 0
        self.legs = []
        self.event = ""
        self.close_iso = ""
        self.out = out or (DATA / f"live_edge_{datetime.now():%Y%m%d}.csv")
        self.cycles = 0
        self.abstained = ""

    def on_index(self, spot, avg60, window_n, t):
        self.spot = float(spot)
        self.avg60 = float(avg60) if avg60 is not None else None
        self.window_n = int(window_n or 0)
        self.ticks.add(self.spot, t)

    def centre(self, secs_left):
        """Best estimate of the settling average.

        Outside the window the average has not started, so spot is the
        forecast. Inside it, blend what is already averaged with the
        expected remainder — the observed part cannot change.
        """
        if self.avg60 is None or secs_left is None or secs_left > 60:
            return self.spot
        obs = min(self.window_n, 60)
        rem = max(60 - obs, 0)
        if obs == 0:
            return self.spot
        return (self.avg60 * obs + self.spot * rem) / 60.0

    def price(self, secs_left, enforce_gate=True):
        sigma = self.ticks.sigma_1s()
        if sigma is None or self.spot is None or not self.legs:
            return None
        obs = min(self.window_n, 60) if (secs_left or 0) <= 60 else 0
        sd = sm.sd_of_settlement(sigma, secs_left or 0, observed_in_window=obs)
        c = self.centre(secs_left)
        stable, sreason = sm.sigma_settled(self.ticks)
        if enforce_gate and not stable:
            self.abstained = sreason
            return {"rows": [], "centre": c, "sd": sd, "sigma": sigma,
                    "secs": secs_left, "abstain": sreason}
        ok, why = sm.tradeable_now(secs_left, sd, c)
        if enforce_gate and not ok:
            self.abstained = why
            return {"rows": [], "centre": c, "sd": sd, "sigma": sigma,
                    "secs": secs_left, "abstain": why}
        self.abstained = ""
        rows = sm.price_ladder(self.legs, c, sd, min_edge=self.min_edge)
        return {"rows": rows, "centre": c, "sd": sd, "sigma": sigma,
                "secs": secs_left, "abstain": ""}

    def log(self, res):
        if not res:
            return
        stamp = now().isoformat(timespec="milliseconds")
        out = []
        for r in res["rows"]:
            out.append({"t": stamp, "series": self.series, "event": self.event,
                        "ticker": r["ticker"], "label": r["label"],
                        "secs_left": round(res["secs"] or 0, 1),
                        "spot": self.spot, "avg60s": self.avg60,
                        "centre": round(res["centre"], 2),
                        "sigma_1s": round(res["sigma"], 4),
                        "sd_settle": round(res["sd"], 2) if res["sd"] else "",
                        "model_pc": round(r["model_pc"], 2),
                        "bid": r["bid"], "ask": r["ask"],
                        "bid_sz": r["bid_sz"], "ask_sz": r["ask_sz"],
                        "yes_net": (round(r["buy_yes_net"], 2)
                                    if r["buy_yes_net"] is not None else ""),
                        "no_net": (round(r["buy_no_net"], 2)
                                   if r["buy_no_net"] is not None else ""),
                        "side": r["side"] or "",
                        "net": round(r["net"], 2) if r["net"] is not None else ""})
        write_rows(self.out, out)
        self.cycles += 1


# ------------------------------------------------------------------- live

async def dial(url):
    import websockets
    hdrs = sign("GET", WS_PATH)
    common = dict(ping_interval=20, ping_timeout=20, max_size=8 << 20)
    last = None
    for kw in ("additional_headers", "extra_headers"):
        try:
            return await websockets.connect(url, **{kw: hdrs}, **common)
        except TypeError as e:
            last = e
    raise RuntimeError(f"websockets header kwarg rejected: {last}")


def pick_event(series, max_wait_min):
    """The next event settling within max_wait_min. Nothing else matters —
    the model is only trusted close to settlement."""
    d = rest_get("/markets", series_ticker=series, status="open", limit=1000)
    ev = {}
    for m in d.get("markets", []):
        ev.setdefault(m.get("event_ticker", "?"), []).append(m)
    best = None
    for k, legs in ev.items():
        t = secs_to(legs[0].get("close_time", ""))
        if t is None or t <= 0:
            continue
        if t <= max_wait_min * 60 and (best is None or t < best[2]):
            best = (k, legs, t)
    return best


async def live(a):
    import websockets
    eng = Engine(a.series, a.min_edge)
    picked = pick_event(a.series, a.max_wait)
    if not picked:
        found, t_left = soonest_event(a.series)
        if not found:
            print(f"no open event in {a.series}")
            return
        print(f"Next {a.series} settlement is {t_left/60:.0f} min away.")
        print(f"The model is only accurate inside "
              f"{sm.MAX_TRUSTED_HORIZON/60:.0f} minutes of settlement, so "
              f"there is nothing to do yet.")
        print(f"Come back in {max(t_left - a.warmup - 60, 0)/60:.0f} min, or "
              f"raise --max-wait to sit and wait.")
        return
    eng.event, eng.legs, t_left = picked
    eng.close_iso = eng.legs[0].get("close_time", "")
    print(f"{eng.event}  {len(eng.legs)} strikes  settles in {t_left/60:.1f} min")
    print(f"warming up volatility, then pricing inside the final "
          f"{sm.MAX_TRUSTED_HORIZON/60:.0f} minutes")
    print(f"logging to {eng.out}\n")

    idx = {"KXBTCD": "BRTI", "KXBTC": "BRTI", "KXETHD": "ETHUSD_RTI",
           "KXSOLE": "SOLUSD_RTI", "KXXRPD": "XRPUSD_RTI"}.get(a.series, "BRTI")

    ws = None
    for u in WS_CANDIDATES:
        try:
            ws = await dial(u)
            print(f"stream: {u}\n")
            break
        except Exception as e:
            print(f"  {u} -> {type(e).__name__}")
    if ws is None:
        print("no websocket host answered")
        return

    await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {
        "channels": ["cfbenchmarks_value"], "index_ids": [idx]}}))
    end = time.time() + a.secs
    last_refresh = 0.0
    last_print = 0.0
    try:
        while time.time() < end:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=20)
            except asyncio.TimeoutError:
                continue
            msg = json.loads(raw)
            if msg.get("type") != "cfbenchmarks_value":
                continue
            m = msg.get("msg", {})
            try:
                spot = float(json.loads(m.get("data", "{}"))["value"])
            except Exception:
                continue
            av = (m.get("avg_60s_data") or {})
            eng.on_index(spot, av.get("value"), av.get("window_size"),
                         time.time())

            if time.time() - last_refresh > a.refresh:
                try:
                    d = rest_get("/markets", event_ticker=eng.event,
                                 status="open", limit=1000)
                    if d.get("markets"):
                        eng.legs = d["markets"]
                except Exception as e:
                    print(f"  book refresh failed: {e}")
                last_refresh = time.time()

            left = secs_to(eng.close_iso)
            res = eng.price(left)
            if res and res.get("rows"):
                eng.log(res)
            if res and time.time() - last_print >= a.every:
                last_print = time.time()
                if res.get("abstain"):
                    print(f"  T-{(left or 0):.0f}s  holding: {res['abstain']}"
                          f"   (spot {eng.spot:,.2f})")
                else:
                    print(sm.render(res["rows"], res["centre"], res["sd"],
                                    res["secs"] or 0, res["sigma"]))
                    print()
            if left is not None and left <= 0:
                print("event closed")
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            await ws.close()
        except Exception:
            pass
    print(f"\n{eng.cycles} cycles logged to {eng.out}")


# ----------------------------------------------------------------- replay

def replay(a):
    """Drive the same Engine from a captured tick file and a captured
    ladder. No network. This is how the logic is tested."""
    ticks = json.loads(Path(a.replay).read_text())
    lad_path = Path(a.ladder or (ROOT / "tests/fixtures/btc_ladder.json"))
    legs = json.loads(lad_path.read_text())["markets"]
    eng = Engine(a.series, a.min_edge, out=DATA / "replay_edge.csv")
    eng.legs = legs
    eng.event = "REPLAY"
    left = a.secs_left
    for i, msg in enumerate(ticks):
        m = msg.get("msg", {})
        spot = float(json.loads(m["data"])["value"])
        av = m.get("avg_60s_data") or {}
        eng.on_index(spot, av.get("value"), av.get("window_size"), i * 5)
        res = eng.price(left)
        if res:
            eng.log(res)
            if i % max(len(ticks) // 4, 1) == 0:
                print(sm.render(res["rows"], res["centre"], res["sd"],
                                res["secs"], res["sigma"]))
                print()
        left = max(left - 5, 0)
    print(f"replayed {len(ticks)} ticks, {eng.cycles} priced cycles "
          f"-> {eng.out}")
    return eng


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXBTCD")
    ap.add_argument("--secs", type=int, default=1200)
    ap.add_argument("--every", type=float, default=10)
    ap.add_argument("--refresh", type=float, default=5)
    ap.add_argument("--min-edge", type=float, default=2.0)
    ap.add_argument("--replay")
    ap.add_argument("--ladder")
    ap.add_argument("--secs-left", type=int, default=300)
    ap.add_argument("--max-wait", type=float, default=20,
                    help="minutes; ignore events settling further out")
    ap.add_argument("--warmup", type=float, default=120,
                    help="seconds of ticks needed before pricing")
    a = ap.parse_args()
    if a.replay:
        replay(a)
    else:
        asyncio.run(live(a))


if __name__ == "__main__":
    main()

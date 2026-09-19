"""
kalshi_stream.py — the four live channels, in one place.

Kalshi's WebSocket carries things the REST API does not:

  cfbenchmarks_value   the crypto index PLUS its trailing 60-second average.
                       The rules settle on "the simple average of the sixty
                       seconds before 5 PM" — so this channel carries the
                       settlement input itself, live, while the contract
                       still trades. 3.0M vol settles on this.
  trade                every public fill: price, size, side, timestamp.
                       Aggression shows here before it shows in the quote.
  orderbook_delta      snapshot then ordered mutations. What is actually
                       fillable, not what is merely quoted.
  market_lifecycle_v2  markets opening, closing, settling — the moment it
                       happens.

Auth: the connection itself is signed even for public channels. Sign GET
on the exact path /trade-api/ws/v2, no query string, RSA-PSS, same scheme
as REST.

    pip install websockets
    python3 kalshi_stream.py --index BTC ETH
    python3 kalshi_stream.py --trades --series KXBTCD
    python3 kalshi_stream.py --book KXBTCD-26AUG3117-T78999.99
    python3 kalshi_stream.py --lifecycle
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

load_dotenv(Path.home() / "trading-lab" / ".env")

ROOT = Path.home() / "trading-lab" / "data" / "stream"
ROOT.mkdir(parents=True, exist_ok=True)

REST = os.environ["KALSHI_BASE_PROD"].rstrip("/")
KEY_ID = os.environ["KALSHI_KEY_ID"]
WS_PATH = "/trade-api/ws/v2"

# The WS host is NOT the REST host. Docs give demo as
# external-api-ws.demo.kalshi.co, so prod carries the same -ws infix.
# Deriving it from the REST base gave 404. Try the known hosts in order
# and remember whichever answers; override with KALSHI_WS_URL in .env.
_WS_CANDIDATES = [
    os.getenv("KALSHI_WS_URL"),
    "wss://external-api-ws.kalshi.com" + WS_PATH,
    "wss://api.elections.kalshi.com" + WS_PATH,
    "wss://external-api.kalshi.com" + WS_PATH,
    "wss://trading-api.kalshi.com" + WS_PATH,
]
_WS_CANDIDATES = [u for u in _WS_CANDIDATES if u]
WS_URL = _WS_CANDIDATES[0]
_PK = serialization.load_pem_private_key(
    Path(os.path.expanduser(os.environ["KALSHI_PRIVATE_KEY_PATH"])).read_bytes(),
    password=None)


def sign(method: str, path: str):
    ts = str(int(time.time() * 1000))
    sig = _PK.sign((ts + method + path).encode(),
                   padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                               salt_length=padding.PSS.DIGEST_LENGTH),
                   hashes.SHA256())
    return {"KALSHI-ACCESS-KEY": KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode()}


def rest_get(ep, **params):
    prefix = "/" + REST.split("://", 1)[1].split("/", 1)[1]
    r = requests.get(REST + ep, headers=sign("GET", prefix + ep),
                     params=params or None, timeout=25)
    r.raise_for_status()
    return r.json()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(path: Path, row: dict):
    exists = path.exists()
    with path.open("a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


# --------------------------------------------------------------- connection

async def connect():
    """Walk the candidate hosts once, keep the one that upgrades.

    Header kwarg was renamed in websockets 14 (extra_headers ->
    additional_headers); coinbase-advanced-py pins 13.1, so try both.
    """
    global WS_URL
    import websockets
    common = dict(ping_interval=20, ping_timeout=20, max_size=8 * 1024 * 1024)
    errors = []
    for url in _WS_CANDIDATES:
        hdrs = sign("GET", WS_PATH)      # fresh timestamp per attempt
        for kw in ("additional_headers", "extra_headers"):
            try:
                ws = await websockets.connect(url, **{kw: hdrs}, **common)
                if url != WS_URL:
                    print(f"  using {url}")
                WS_URL = url
                return ws
            except TypeError:
                continue                 # wrong kwarg for this version
            except Exception as e:
                code = getattr(e, "status_code", None) or getattr(
                    getattr(e, "response", None), "status_code", "")
                errors.append(f"{url.split('//')[1].split('/')[0]} -> "
                              f"{type(e).__name__}{f' {code}' if code else ''}")
                break
    raise RuntimeError("no WebSocket host accepted the connection:\n   "
                       + "\n   ".join(errors)
                       + "\n   Set KALSHI_WS_URL in .env if you know the host.")


async def run(channels, params_extra, handler, secs):
    import websockets
    backoff = 1
    end = time.time() + secs if secs else None
    while end is None or time.time() < end:
        try:
            ws = await connect()
        except Exception as e:
            print(f"connect failed ({type(e).__name__}: {e}); retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1
        try:
            # websockets 13.x: awaiting connect() returns a protocol that is
            # NOT an async context manager (14.x is). Close it explicitly.
            sub = {"id": 1, "cmd": "subscribe",
                   "params": {"channels": channels, **params_extra}}
            await ws.send(json.dumps(sub))
            print(f"subscribed {channels} -> {WS_URL}\n", flush=True)
            while end is None or time.time() < end:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "error":
                    print("SERVER ERROR:", json.dumps(msg)[:300])
                    continue
                if msg.get("type") in ("subscribed", "ok"):
                    continue
                handler(msg)
        except websockets.ConnectionClosed as e:
            print(f"\nconnection closed ({e.code}); reconnecting")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except KeyboardInterrupt:
            return
        finally:
            try:
                await ws.close()
            except Exception:
                pass


# ------------------------------------------------- cfbenchmarks_value (3.0M)

def index_handler(state):
    path = ROOT / f"cfb_{datetime.now():%Y%m%d}.csv"

    def h(msg):
        if msg.get("type") != "cfbenchmarks_value":
            return
        m = msg.get("msg", {})
        idx = m.get("index_id", "?")
        try:
            raw = json.loads(m.get("data", "{}"))
        except Exception:
            raw = {}
        spot = raw.get("value")
        a60 = (m.get("avg_60s_data") or {}).get("value")
        n60 = (m.get("avg_60s_data") or {}).get("window_size")
        recv = m.get("received_at")
        if spot is None:
            return
        spot = float(spot)
        a60f = float(a60) if a60 is not None else None
        prev = state["last"].get(idx)
        state["last"][idx] = spot
        state["n"][idx] += 1
        drift = (spot - a60f) if a60f is not None else None
        if state["n"][idx] % state["every"] == 0:
            d = f"{drift:+8.2f}" if drift is not None else "      --"
            print(f"{now()[11:23]}  {idx:<14}spot {spot:>12,.2f}   "
                  f"avg60s {a60f if a60f else 0:>12,.2f} (n={n60})   "
                  f"spot-avg {d}   ticks {state['n'][idx]:,}")
        log(path, {"t": now(), "index": idx, "spot": spot,
                   "avg_60s": a60f, "window": n60, "received_at": recv})
    return h


# -------------------------------------------------------------- trade tape

def trade_handler(state):
    path = ROOT / f"trades_{datetime.now():%Y%m%d}.csv"

    def h(msg):
        if msg.get("type") != "trade":
            return
        m = msg.get("msg", {})
        tk = m.get("market_ticker", "?")
        cnt = float(m.get("count_fp") or m.get("count") or 0)
        yes = float(m.get("yes_price_dollars") or 0) * 100
        no = float(m.get("no_price_dollars") or 0) * 100
        side = m.get("taker_side", "?")
        s = state["agg"][tk]
        s["n"] += 1
        s["vol"] += cnt
        s[side] = s.get(side, 0) + cnt
        notional = cnt * (yes if side == "yes" else no) / 100
        print(f"{now()[11:23]}  {tk[:34]:<35}{side:<4}{cnt:>8,.0f} @ "
              f"{yes:>5.1f}c  ${notional:>9,.2f}   "
              f"[yes {s.get('yes',0):,.0f} / no {s.get('no',0):,.0f}]")
        log(path, {"t": now(), "ticker": tk, "side": side, "count": cnt,
                   "yes_c": yes, "no_c": no, "notional": notional})
    return h


# ---------------------------------------------------------- orderbook_delta

def book_handler(state):
    """Kalshi sends BIDS ONLY on both sides: a yes bid at P implies a no ask
    at 100-P. Maintain the local book from snapshot + ordered deltas, and
    stop trusting it if a sequence number is skipped."""
    path = ROOT / f"book_{datetime.now():%Y%m%d}.csv"

    def apply(book, side, price, delta):
        book[side][price] = book[side].get(price, 0) + delta
        if book[side][price] <= 0:
            book[side].pop(price, None)

    def show(tk, book, tag):
        yes = sorted(book["yes"].items(), key=lambda kv: -kv[0])[:3]
        no = sorted(book["no"].items(), key=lambda kv: -kv[0])[:3]
        ystr = " ".join(f"{p:.0f}c x{q:,.0f}" for p, q in yes) or "-"
        nstr = " ".join(f"{p:.0f}c x{q:,.0f}" for p, q in no) or "-"
        best_yes = yes[0][0] if yes else 0
        best_no = no[0][0] if no else 0
        spread = 100 - best_yes - best_no if (yes and no) else None
        print(f"{now()[11:23]} {tag:<9}{tk[:30]:<31}"
              f"YES {ystr:<28} NO {nstr:<28}"
              + (f" spread {spread:.0f}c" if spread is not None else ""))
        log(path, {"t": now(), "ticker": tk, "event": tag,
                   "best_yes": best_yes, "best_no": best_no,
                   "spread": spread if spread is not None else "",
                   "yes_depth": json.dumps(yes), "no_depth": json.dumps(no)})

    def h(msg):
        t = msg.get("type")
        m = msg.get("msg", {})
        tk = m.get("market_ticker", "?")
        seq = msg.get("seq")
        if t == "orderbook_snapshot":
            book = {"yes": {}, "no": {}}
            for side in ("yes", "no"):
                for lvl in (m.get(f"{side}_dollars") or m.get(side) or []):
                    try:
                        p = float(lvl[0]) * (100 if "." in str(lvl[0]) else 1)
                        q = float(lvl[1])
                        book[side][p] = q
                    except Exception:
                        pass
            state["books"][tk] = book
            state["seq"][tk] = seq
            show(tk, book, "SNAPSHOT")
        elif t == "orderbook_delta":
            last = state["seq"].get(tk)
            if last is not None and seq is not None and seq != last + 1:
                print(f"  !! sequence gap on {tk}: {last} -> {seq}. "
                      f"Local book is stale; waiting for a fresh snapshot.")
                state["books"].pop(tk, None)
                state["seq"][tk] = seq
                return
            state["seq"][tk] = seq
            book = state["books"].get(tk)
            if book is None:
                return
            price = m.get("price_dollars")
            price = float(price) * 100 if price is not None else float(m.get("price", 0))
            delta = float(m.get("delta_fp") or m.get("delta") or 0)
            apply(book, m.get("side", "yes"), price, delta)
            show(tk, book, "delta")
    return h


# ------------------------------------------------------ market_lifecycle_v2

def life_handler(state):
    path = ROOT / f"lifecycle_{datetime.now():%Y%m%d}.csv"

    def h(msg):
        if "lifecycle" not in (msg.get("type") or ""):
            return
        m = msg.get("msg", {})
        tk = m.get("market_ticker") or m.get("ticker", "?")
        ev = (m.get("event_type") or m.get("status") or msg.get("type"))
        print(f"{now()[11:23]}  {str(ev):<22}{tk[:44]:<45}"
              f"{(m.get('title') or '')[:34]}")
        log(path, {"t": now(), "event": str(ev), "ticker": tk,
                   "title": (m.get("title") or "")[:80],
                   "raw": json.dumps(m)[:300]})
    return h


# -------------------------------------------------------------------- modes

def markets_in(series_list, min_vol=0.0, cap=60):
    out = []
    for s in series_list:
        try:
            d = rest_get("/markets", series_ticker=s, status="open", limit=1000)
        except Exception as e:
            print(f"  {s}: {e}")
            continue
        rows = [(float(m.get("volume_24h_fp") or 0), m["ticker"])
                for m in d.get("markets", [])]
        rows.sort(reverse=True)
        out += [t for v, t in rows if v >= min_vol][:cap]
    return out[:cap]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", nargs="*", metavar="SYM",
                    help="cfbenchmarks_value, e.g. --index BTC ETH")
    ap.add_argument("--trades", action="store_true")
    ap.add_argument("--book", nargs="*", metavar="TICKER")
    ap.add_argument("--lifecycle", action="store_true")
    ap.add_argument("--series", nargs="*", default=["KXBTCD"],
                    help="series to resolve tickers from, for --trades/--book")
    ap.add_argument("--min-vol", type=float, default=100.0)
    ap.add_argument("--secs", type=int, default=300)
    ap.add_argument("--every", type=int, default=5,
                    help="print 1 in N index ticks")
    a = ap.parse_args()

    RTI = {"BTC": "BRTI", "ETH": "ETHUSD_RTI", "SOL": "SOLUSD_RTI",
           "XRP": "XRPUSD_RTI", "BNB": "BNBUSD_RTI", "DOGE": "DOGEUSD_RTI"}

    if a.index is not None:
        ids = [RTI.get(x.upper(), x) for x in (a.index or ["BTC"])]
        st = {"last": {}, "n": defaultdict(int), "every": a.every}
        print(f"index feed: {ids}\n"
              f"spot-avg is how far the live index sits from its trailing\n"
              f"60s mean — the settlement window is exactly that average.\n")
        asyncio.run(run(["cfbenchmarks_value"], {"index_ids": ids},
                        index_handler(st), a.secs))
        return

    if a.trades:
        tks = markets_in(a.series, a.min_vol)
        if not tks:
            print("no markets matched")
            return
        print(f"trade tape on {len(tks)} markets from {a.series}\n")
        st = {"agg": defaultdict(lambda: {"n": 0, "vol": 0.0})}
        asyncio.run(run(["trade"], {"market_tickers": tks},
                        trade_handler(st), a.secs))
        return

    if a.book is not None:
        tks = a.book or markets_in(a.series, a.min_vol, cap=8)
        if not tks:
            print("no markets matched")
            return
        print(f"orderbook on {len(tks)} markets\n")
        st = {"books": {}, "seq": {}}
        asyncio.run(run(["orderbook_delta"], {"market_tickers": tks},
                        book_handler(st), a.secs))
        return

    if a.lifecycle:
        print("market lifecycle — opens, closes, settlements\n")
        asyncio.run(run(["market_lifecycle_v2"], {}, life_handler({}), a.secs))
        return

    ap.print_help()


if __name__ == "__main__":
    main()

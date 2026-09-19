#!/usr/bin/env python3
"""
Near-certainty carry scanner. Polymarket + Kalshi.

    python3 scan.py --probe
    python3 scan.py --venue polymarket --band 0.92 0.995 --capital 25000
    python3 scan.py --venue kalshi --band 0.92 0.995 --capital 25000 --json out.json

WHAT THIS RANKS BY
    Not price. Not raw spread. Depth-adjusted annualised return, after
    the fee you supply, at the size YOU can actually fill. A 98.4c quote
    with $600 behind it ranks below a 97.5c quote with $80k behind it,
    which is the whole point.

STATUS OF THE HTTP LAYER — READ THIS
    The math modules (economics / ruin / book) are unit-tested against
    fixtures and verified by two independent derivations each.
    THIS FILE'S NETWORK LAYER IS NOT TESTED. It was written in a sandbox
    with no route to either venue. Endpoint paths and JSON field names
    are recalled, not verified, and both venues change them without
    notice.

    Run --probe FIRST. It asserts the shape of every response it depends
    on and dies loudly on any mismatch. A scanner that silently returns
    an empty list because a field was renamed is the exact failure mode
    that ends projects.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, asdict

import requests

import economics as ec
import book as bk
import ruin as rn

POLY_GAMMA = "https://gamma-api.polymarket.com/markets"
POLY_CLOB_BOOK = "https://clob.polymarket.com/book"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

TIMEOUT = 20
UA = {"User-Agent": "carry-scanner/1.0 (research; contact via operator)"}


# ----------------------------------------------------------------------
# Probe — run this before trusting anything downstream
# ----------------------------------------------------------------------

def probe() -> int:
    """Assert every endpoint and field name this scanner depends on.
    Returns 0 all good, 1 something drifted, 2 could not reach."""
    problems, unreachable = [], []

    def head(name, url, params=None, want_keys=()):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
        except Exception as e:
            unreachable.append(f"{name}: {type(e).__name__} {e}")
            return None
        if r.status_code != 200:
            problems.append(f"{name}: HTTP {r.status_code}")
            return None
        try:
            j = r.json()
        except Exception:
            problems.append(f"{name}: response was not JSON")
            return None
        sample = j[0] if isinstance(j, list) and j else j
        if isinstance(sample, dict):
            missing = [k for k in want_keys if k not in sample]
            if missing:
                problems.append(f"{name}: missing fields {missing} "
                                f"(present: {sorted(sample)[:14]})")
        print(f"  {name}: HTTP 200, "
              f"{len(j) if isinstance(j, list) else 'obj'} records")
        return j

    print("PROBE: polymarket")
    head("gamma/markets", POLY_GAMMA,
         {"closed": "false", "limit": 3},
         want_keys=("id", "question", "clobTokenIds", "outcomePrices", "endDate"))

    print("PROBE: kalshi")
    head("kalshi/markets", f"{KALSHI_BASE}/markets",
         {"status": "open", "limit": 3},
         want_keys=("markets",))

    print()
    if unreachable:
        print("UNREACHABLE:")
        for u in unreachable:
            print("  ", u)
        return 2
    if problems:
        print("DRIFTED — do not run the scanner until these are fixed:")
        for p in problems:
            print("  ", p)
        return 1
    print("PROBE OK — endpoints and field names match expectations.")
    return 0


# ----------------------------------------------------------------------
# Candidate
# ----------------------------------------------------------------------

@dataclass
class Candidate:
    venue: str
    market: str
    outcome: str
    top_of_book: float
    vwap_at_size: float
    days_to_resolution: float
    notional_tested: float
    filled: bool
    quoted_annual_pct: float
    realised_annual_pct: float
    return_lost_to_depth_pct: float
    loss_to_gain: float
    breakeven_true_prob_pct: float
    max_notional_at_10pct_annual: float
    edge_needed_pp: float
    settlements_to_prove_edge: int
    ci_halfwidth_at_n20_pp: float
    measurement_over_edge: float
    url: str | None = None


def evaluate(venue: str, market: str, outcome: str,
             levels: list[tuple[float, float]], days: float,
             capital: float, fee_bps: float,
             assumed_edge_pp: float = 0.5) -> Candidate | None:
    """Turn a raw book into a ranked candidate, or None if unusable."""
    if not levels or days <= 0:
        return None
    s = bk.summarise(levels, capital, days)
    p_eff = ec.net_price(s["vwap"], fee_bps)
    if not (0.0 < p_eff < 1.0):
        return None

    q_assumed = min(p_eff + assumed_edge_pp / 100.0, 0.99999)
    n_needed = ec.n_required(p_eff, p_eff - assumed_edge_pp / 100.0)

    return Candidate(
        venue=venue,
        market=market,
        outcome=outcome,
        top_of_book=s["top_of_book"],
        vwap_at_size=s["vwap"],
        days_to_resolution=days,
        notional_tested=capital,
        filled=s["complete_fill"],
        quoted_annual_pct=s["quoted_annual_pct"],
        realised_annual_pct=round(
            ec.annualize(ec.gross_return(p_eff), days) * 100, 2),
        return_lost_to_depth_pct=s["return_lost_to_depth_pct"],
        loss_to_gain=round(ec.loss_to_gain(p_eff), 2),
        breakeven_true_prob_pct=round(p_eff * 100, 3),
        max_notional_at_10pct_annual=bk.max_notional_at_return(
            levels, 0.10, days),
        edge_needed_pp=assumed_edge_pp,
        settlements_to_prove_edge=n_needed,
        ci_halfwidth_at_n20_pp=round(ec.ci_halfwidth_pp(p_eff, 20), 2),
        measurement_over_edge=round(
            ec.measurement_to_edge_ratio(p_eff, q_assumed, 20), 1),
    )


# ----------------------------------------------------------------------
# Venue adapters — thin, so a field rename is a one-line fix
# ----------------------------------------------------------------------

def poly_books(band, limit, days_field="endDate"):
    """Yield (market, outcome, levels, days). UNTESTED — see module docstring."""
    from datetime import datetime, timezone
    r = requests.get(POLY_GAMMA, params={"closed": "false", "limit": limit},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    for m in r.json():
        try:
            tokens = json.loads(m["clobTokenIds"]) if isinstance(
                m["clobTokenIds"], str) else m["clobTokenIds"]
            outcomes = json.loads(m["outcomes"]) if isinstance(
                m.get("outcomes"), str) else m.get("outcomes", ["Yes", "No"])
            end = datetime.fromisoformat(
                m[days_field].replace("Z", "+00:00"))
            days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
            if days <= 0:
                continue
        except Exception:
            continue
        for tok, name in zip(tokens, outcomes):
            try:
                b = requests.get(POLY_CLOB_BOOK, params={"token_id": tok},
                                 headers=UA, timeout=TIMEOUT).json()
                asks = sorted(
                    ((float(a["price"]), float(a["size"])) for a in b["asks"]),
                    key=lambda x: x[0])
            except Exception:
                continue
            if not asks or not (band[0] <= asks[0][0] <= band[1]):
                continue
            yield m.get("question", "?"), name, asks, days


def kalshi_books(band, limit):
    """Yield (market, outcome, levels, days). UNTESTED — see module docstring."""
    from datetime import datetime, timezone
    r = requests.get(f"{KALSHI_BASE}/markets",
                     params={"status": "open", "limit": limit},
                     headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    for m in r.json().get("markets", []):
        try:
            end = datetime.fromisoformat(
                m["close_time"].replace("Z", "+00:00"))
            days = (end - datetime.now(timezone.utc)).total_seconds() / 86400
            if days <= 0:
                continue
            ob = requests.get(
                f"{KALSHI_BASE}/markets/{m['ticker']}/orderbook",
                headers=UA, timeout=TIMEOUT).json()["orderbook"]
        except Exception:
            continue
        # Kalshi quotes in cents, bids only; the yes-ask is 100 - no-bid.
        for side, other in (("yes", "no"), ("no", "yes")):
            rows = ob.get(other) or []
            asks = sorted((((100 - int(px)) / 100.0, float(sz))
                           for px, sz in rows), key=lambda x: x[0])
            if not asks or not (band[0] <= asks[0][0] <= band[1]):
                continue
            yield m.get("title", m["ticker"]), side.upper(), asks, days


# ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--venue", choices=("polymarket", "kalshi"))
    ap.add_argument("--band", nargs=2, type=float, default=[0.92, 0.995])
    ap.add_argument("--capital", type=float, default=10_000.0)
    ap.add_argument("--fee-bps", type=float, required=False,
                    help="REQUIRED for a scan. No default: a guessed fee "
                         "schedule is a silently wrong answer.")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--edge-pp", type=float, default=0.5,
                    help="Edge in pp you claim to have over the quote.")
    ap.add_argument("--json")
    a = ap.parse_args()

    if a.probe:
        return probe()
    if not a.venue:
        ap.error("--venue required (or use --probe)")
    if a.fee_bps is None:
        ap.error("--fee-bps required. Look it up on the venue, do not guess.")

    src = poly_books(a.band, a.limit) if a.venue == "polymarket" \
        else kalshi_books(a.band, a.limit)

    out = []
    for market, outcome, levels, days in src:
        c = evaluate(a.venue, market, outcome, levels, days,
                     a.capital, a.fee_bps, a.edge_pp)
        if c:
            out.append(c)

    out.sort(key=lambda c: (c.filled, c.realised_annual_pct), reverse=True)

    if not out:
        print("No candidates in band. If that is surprising, run --probe: "
              "an empty list and a renamed field look identical from here.")
        return 0

    print(f"{'ann%':>7} {'quoted':>7} {'lost':>6} {'vwap':>7} "
          f"{'fill':>5} {'maxNtnl@10%':>12} {'L:G':>7} {'n_req':>7}  market")
    for c in out[:40]:
        print(f"{c.realised_annual_pct:>7.1f} {c.quoted_annual_pct:>7.1f} "
              f"{c.return_lost_to_depth_pct:>6.1f} {c.vwap_at_size:>7.4f} "
              f"{'Y' if c.filled else 'N':>5} "
              f"{c.max_notional_at_10pct_annual:>12,.0f} "
              f"{c.loss_to_gain:>7.1f} {c.settlements_to_prove_edge:>7,}  "
              f"{c.market[:44]} [{c.outcome}]")

    if a.json:
        with open(a.json, "w") as f:
            json.dump([asdict(c) for c in out], f, indent=2)
        print(f"\nwrote {len(out)} candidates -> {a.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

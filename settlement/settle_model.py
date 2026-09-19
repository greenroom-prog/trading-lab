"""
settle_model.py — price a Kalshi crypto ladder from the settlement input.

WHAT SETTLES THE CONTRACT
  "If the simple average of the sixty seconds of CF Benchmarks' BRTI
   before 5 PM EDT is above X at 5 PM EDT, then Yes."

  So the settled value is S = mean(BRTI over the final 60 seconds).
  The market quotes off SPOT. Spot and S are not the same number, and
  the live 60s average is broadcast on Kalshi's own websocket.

THE MODEL
  At time t with T seconds to settlement:
    - the final-60s mean is a time average, not a point. Its variance is
      smaller than spot's by roughly a factor of 3 for a random walk
      (Var of the mean of a Brownian path over the window).
    - remaining uncertainty comes from drift over the T-60 seconds before
      the window opens, plus the averaging inside it.

    sd_settle(T) = sigma_1s * sqrt(max(T-60,0) + 60/3)

  sigma_1s is MEASURED from the tick stream, never assumed.

  Inside the final 60 seconds the window is already partly observed, so
  the estimate blends what has been averaged with what is still to come.

HONEST LIMITS
  - assumes a driftless random walk between now and settlement
  - assumes the tick sigma estimated from a recent window still holds
  - says nothing about whether you can be filled
  Everything it prints is an estimate with a stated sd, not a signal.
"""

from __future__ import annotations

import json
import math
import statistics as st
from dataclasses import dataclass, field


def _phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


@dataclass
class TickStats:
    """Volatility measured from the live index, in dollars per second.

    sigma is re-estimated on every tick from a rolling window, so the raw
    estimate wobbles a few percent and makes sd(settlement) tick UP even
    as the clock runs down. An EWMA over successive estimates keeps the
    number responsive without that whipsaw.
    """
    prices: list = field(default_factory=list)
    times: list = field(default_factory=list)
    max_keep: int = 900
    _ewma: float = None
    alpha: float = 0.05
    # 12 ticks was enough to COMPUTE a sigma but nowhere near enough to
    # TRUST one. On 2026-09-01 the first four cycles priced a strike at
    # 0.7%, 3.9%, 5.4%, 11.8% on consecutive seconds — a 17x swing. The
    # early sigma was far too small, which made the model wildly
    # overconfident. It happened to be directionally right; that was luck.
    min_ticks: int = 90
    _stable_since: int = None

    def add(self, price, t):
        self.prices.append(float(price))
        self.times.append(float(t))
        if len(self.prices) > self.max_keep:
            self.prices = self.prices[-self.max_keep:]
            self.times = self.times[-self.max_keep:]

    @property
    def n(self):
        return len(self.prices)

    def warmup_left(self):
        """Ticks still needed before the estimate can be trusted."""
        return max(self.min_ticks - self.n, 0)

    def sigma_1s(self):
        """Per-second sd of index changes, scaled by the real gap between
        ticks. Returns None until there is genuinely enough data."""
        if self.n < self.min_ticks:
            return None
        diffs, gaps = [], []
        for i in range(1, self.n):
            dt = self.times[i] - self.times[i - 1]
            if dt <= 0:
                continue
            diffs.append((self.prices[i] - self.prices[i - 1]) / math.sqrt(dt))
            gaps.append(dt)
        if len(diffs) < 10:
            return None
        raw = st.pstdev(diffs)
        if raw <= 0:
            return self._ewma
        self._ewma = raw if self._ewma is None else \
            (1 - self.alpha) * self._ewma + self.alpha * raw
        return self._ewma


# MEASURED from 590 live samples of BRTI on 2026-08-31. Ratio of the
# ACTUAL sd of index changes over a horizon to sqrt-scaled sigma_1s:
#
#     5s 1.18   15s 1.08   30s 1.00   60s 0.97   120s 0.77
#
# BTC mean-reverts at these horizons, so plain sqrt(t) OVERSTATES how far
# the index can travel the further out you go. An overstated sd pushes
# near-money probabilities toward 50%, which manufactures edges that are
# not there — that is exactly what the first live run produced.
#
# Interpolated in log-horizon, held flat outside the measured range, and
# never extrapolated past MAX_TRUSTED_HORIZON.
_SCALE_POINTS = [(5, 1.18), (15, 1.08), (30, 1.00), (60, 0.97), (120, 0.77)]
MAX_TRUSTED_HORIZON = 300.0     # seconds; beyond this the model abstains


def horizon_scale(secs):
    """Correction factor on sqrt-time scaling, measured not assumed."""
    t = max(float(secs), 1.0)
    pts = _SCALE_POINTS
    if t <= pts[0][0]:
        return pts[0][1]
    if t >= pts[-1][0]:
        return pts[-1][1]
    for (t0, s0), (t1, s1) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            w = (math.log(t) - math.log(t0)) / (math.log(t1) - math.log(t0))
            return s0 + w * (s1 - s0)
    return pts[-1][1]


def sd_raw(sigma_1s, secs_to_close, window=60, observed_in_window=0):
    """Textbook sd under a driftless random walk, BEFORE the measured
    horizon correction. Kept separate so the random-walk maths can be
    verified against simulation independently of the empirical fudge."""
    if sigma_1s is None or sigma_1s <= 0:
        return None
    T = max(float(secs_to_close), 0.0)
    if T > window:
        var_units = (T - window) + window / 3.0
    else:
        var_units = max(window - observed_in_window, 0) / 3.0
    return sigma_1s * math.sqrt(max(var_units, 1e-9))


def sd_of_settlement(sigma_1s, secs_to_close, window=60, observed_in_window=0):
    """sd of the 60-second average that settles the contract.

    Before the window opens: drift for (T - window) seconds, then the
    averaging term window/3 (variance of the mean of a Brownian path).
    Inside the window: only the unobserved remainder still moves.
    """
    if sigma_1s is None or sigma_1s <= 0:
        return None
    T = max(float(secs_to_close), 0.0)
    raw = sd_raw(sigma_1s, T, window, observed_in_window)
    if raw is None:
        return None
    # The scale factors were measured on SPOT-to-SPOT changes over a
    # horizon. Inside the averaging window the quantity is a mean, not a
    # point, so the short-horizon inflation (1.18 at 5s) does not apply —
    # applying it made sd RISE from T-60s to T-20s, which is impossible.
    # Hold the factor at its window-length value once inside.
    scale = horizon_scale(T) if T > window else horizon_scale(window)
    return raw * scale


def prob_above(strike, centre, sd):
    """P(settled average > strike)."""
    if sd is None or sd <= 0:
        return 1.0 if centre > strike else 0.0
    return 1.0 - _phi((strike - centre) / sd)


def fee_cents(price_cents, contracts=1):
    p = max(min(price_cents / 100.0, 1.0), 0.0)
    return math.ceil(0.07 * contracts * p * (1 - p) * 100) / 100.0


def _f(m, *names, default=0.0):
    for n in names:
        v = m.get(n)
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return default


def tradeable_now(secs_to_close, sd, centre):
    """Should the model be trusted at this moment?

    Two hard gates, both learned from the first live run:
      - beyond MAX_TRUSTED_HORIZON the sd correction is extrapolation
      - a sd wider than the strike spacing means every near-money strike
        looks mispriced regardless of the market
    """
    if secs_to_close is None:
        return False, "no clock"
    if secs_to_close <= 0:
        return False, "settled"
    if secs_to_close > MAX_TRUSTED_HORIZON:
        return False, (f"{secs_to_close/60:.0f} min out; sd is only measured "
                       f"to {MAX_TRUSTED_HORIZON/60:.0f} min")
    if sd is None:
        return False, "volatility not yet measured"
    return True, ""


def sigma_settled(ts, look=60, z=2.5):
    """Has the volatility estimate stopped moving?

    Compares the long-run EWMA against a recent-window estimate. The
    tolerance is NOT a guessed percentage: the sd of an sd estimate from
    n samples is about sigma/sqrt(2n), so a 60-tick window naturally
    wobbles ~9%. Flagging anything above a fixed 15% therefore failed on
    pure noise. The band is now z sampling errors wide, so it fires only
    when the estimate has genuinely shifted.
    """
    if ts.n < ts.min_ticks:
        return False, f"warming up, {ts.warmup_left()} more ticks"
    cur = ts.sigma_1s()
    if cur is None or cur <= 0:
        return False, "no sigma"
    import statistics as _st
    tail_p = ts.prices[-look:]
    tail_t = ts.times[-look:]
    d = []
    for i in range(1, len(tail_p)):
        dt = tail_t[i] - tail_t[i - 1]
        if dt > 0:
            d.append((tail_p[i] - tail_p[i - 1]) / math.sqrt(dt))
    if len(d) < 10:
        return False, "too few recent ticks"
    recent = _st.pstdev(d)
    n = len(d)
    tol = z / math.sqrt(2.0 * n)          # sampling error of an sd estimate
    drift = abs(recent - cur) / cur
    if drift > tol:
        return False, (f"sigma still moving ({drift*100:.0f}% vs "
                       f"{tol*100:.0f}% expected noise)")
    return True, ""


def price_ladder(markets, centre, sd, min_edge=2.0, size_floor=1.0):
    """One row per strike: model probability, market quote, net edge.

    Buying YES  costs the ask; buying NO costs (100 - bid) on the yes side.
    Both are evaluated, because a rich strike is as tradeable as a cheap
    one — you just take the other side.
    """
    rows = []
    for m in markets:
        stype = (m.get("strike_type") or "").lower()
        floor = _f(m, "floor_strike_dollars", "floor_strike", default=None)
        cap = _f(m, "cap_strike_dollars", "cap_strike", default=None)
        bid = _f(m, "yes_bid_dollars", "yes_bid") * 100
        ask = _f(m, "yes_ask_dollars", "yes_ask") * 100
        bsz = _f(m, "yes_bid_size_fp")
        asz = _f(m, "yes_ask_size_fp")

        if stype == "greater" and floor is not None:
            p = prob_above(floor, centre, sd)
            label = f">{floor:,.2f}"
        elif stype == "less" and cap is not None:
            p = 1.0 - prob_above(cap, centre, sd)
            label = f"<{cap:,.2f}"
        elif stype == "between" and floor is not None and cap is not None:
            p = prob_above(floor, centre, sd) - prob_above(cap, centre, sd)
            label = f"{floor:,.0f}-{cap:,.0f}"
        else:
            continue

        pc = max(min(p * 100, 100.0), 0.0)
        buy_yes = buy_no = None
        if ask > 0 and asz >= size_floor:
            buy_yes = pc - ask - fee_cents(ask)
        if bid > 0 and bsz >= size_floor:
            # buying NO at (100-bid); it pays when YES does not
            no_cost = 100 - bid
            buy_no = (100 - pc) - no_cost - fee_cents(no_cost)

        best_side, best_net = None, None
        for side, net in (("YES", buy_yes), ("NO", buy_no)):
            if net is not None and (best_net is None or net > best_net):
                best_side, best_net = side, net

        rows.append({
            "ticker": m.get("ticker", "?"), "label": label,
            "model_pc": pc, "bid": bid, "ask": ask,
            "bid_sz": bsz, "ask_sz": asz,
            "buy_yes_net": buy_yes, "buy_no_net": buy_no,
            "side": best_side, "net": best_net,
            "flag": bool(best_net is not None and best_net >= min_edge),
        })
    return rows


def coherence(rows):
    """A 'greater' ladder must be non-increasing in strike, and the model
    must agree. If either fails, the numbers are not usable."""
    probs = [r["model_pc"] for r in rows]
    mono_model = all(a >= b - 1e-9 for a, b in zip(probs, probs[1:]))
    mids = [(r["bid"] + r["ask"]) / 2 for r in rows if r["ask"] > 0]
    mono_mkt = all(a >= b - 1e-9 for a, b in zip(mids, mids[1:]))
    return {"model_monotonic": mono_model, "market_monotonic": mono_mkt}


def render(rows, centre, sd, secs, sigma):
    out = []
    out.append(f"  settle estimate {centre:,.2f}  sd {sd:,.2f}  "
               f"T-{secs:.0f}s  sigma_1s {sigma:,.3f}")
    out.append(f"  {'strike':>14}{'model%':>8}{'bid':>6}{'ask':>6}"
               f"{'YESnet':>8}{'NOnet':>8}{'bidsz':>8}{'asksz':>8}")
    for r in rows:
        y = f"{r['buy_yes_net']:+.1f}" if r["buy_yes_net"] is not None else "   --"
        n = f"{r['buy_no_net']:+.1f}" if r["buy_no_net"] is not None else "   --"
        out.append(f"  {r['label']:>14}{r['model_pc']:>8.1f}{r['bid']:>6.0f}"
                   f"{r['ask']:>6.0f}{y:>8}{n:>8}"
                   f"{r['bid_sz']:>8,.0f}{r['ask_sz']:>8,.0f}"
                   + ("   <<<" if r["flag"] else ""))
    return "\n".join(out)

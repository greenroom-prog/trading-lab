"""
Near-certainty carry economics.

Pure math. No network, no I/O, no venue assumptions. Every input is
explicit; nothing is defaulted that could silently be wrong.

CONVENTION
    p  = probability-price of the side you BUY, in (0, 1).
         A binary contract pays 1.0 if it resolves your way, 0.0 if not.
    q  = your estimate of the TRUE probability it resolves your way.
    r  = gross return on committed capital if it resolves your way.

The whole strategy in one line: you are selling insurance at price p and
collecting r = (1-p)/p. At p = 0.982 that is 1.83% for a 54.6:1 downside.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, asdict


# ----------------------------------------------------------------------
# Single-position economics
# ----------------------------------------------------------------------

def gross_return(p: float) -> float:
    """Return on committed capital if the contract resolves your way."""
    _check_price(p)
    return (1.0 - p) / p


def loss_to_gain(p: float) -> float:
    """How many wins one loss erases. p/(1-p)."""
    _check_price(p)
    return p / (1.0 - p)


def breakeven_q(p: float) -> float:
    """True probability required for zero EV. It is exactly p."""
    _check_price(p)
    return p


def annualize(r: float, days: float) -> float:
    """Compound a per-cycle return r over 365/days cycles."""
    if days <= 0:
        raise ValueError("days_to_resolution must be > 0")
    return (1.0 + r) ** (365.0 / days) - 1.0


def net_price(p: float, fee_bps: float, side_is_taker: bool = True) -> float:
    """Effective entry price after taker fee, expressed in bps of notional.

    fee_bps MUST be supplied. Venue fee schedules change; a hardcoded
    default is a silent wrong answer. Pass 0.0 explicitly if there is
    genuinely no fee, and record why.
    """
    if fee_bps is None:
        raise ValueError("fee_bps must be supplied explicitly, not defaulted")
    if not side_is_taker:
        return p
    return p * (1.0 + fee_bps / 10_000.0)


def kelly_fraction(p: float, q: float) -> float:
    """Kelly-optimal fraction of bankroll.

    f* = q - (1-q) * p/(1-p)

    Negative means the bet is negative-EV at your own estimate: the
    correct size is zero (or the other side).
    """
    _check_price(p)
    _check_price(q, name="q")
    b = gross_return(p)
    return (b * q - (1.0 - q)) / b


def edge_pp(p: float, q: float) -> float:
    """Edge in percentage points. Kelly crosses zero at exactly q == p."""
    return (q - p) * 100.0


# ----------------------------------------------------------------------
# Measurability — the part that decides whether any of this is knowable
# ----------------------------------------------------------------------

def ci_halfwidth_pp(p: float, n: int, z: float = 1.96) -> float:
    """95% CI half-width on an observed loss rate, in percentage points.

    Normal approximation. At tiny p and small n this UNDERSTATES the true
    interval, which makes it a conservative test: if the edge is already
    swamped under this approximation it is swamped under the exact one.
    """
    _check_price(p)
    if n <= 0:
        raise ValueError("n must be > 0")
    loss_rate = 1.0 - p
    return z * math.sqrt(loss_rate * (1.0 - loss_rate) / n) * 100.0


def n_required(p_a: float, p_b: float, alpha: float = 0.05,
               power: float = 0.80) -> int:
    """Settlements per arm to distinguish loss rate (1-p_a) from (1-p_b).

    Two-proportion normal approximation. This is the number that decides
    whether a tail edge is empirically knowable at all.
    """
    from statistics import NormalDist
    nd = NormalDist()
    z_a = nd.inv_cdf(1.0 - alpha / 2.0)
    z_b = nd.inv_cdf(power)
    a, b = 1.0 - p_a, 1.0 - p_b
    delta = abs(a - b)
    if delta == 0:
        return math.inf
    num = (z_a + z_b) ** 2 * (a * (1 - a) + b * (1 - b))
    return math.ceil(num / delta ** 2)


def measurement_to_edge_ratio(p: float, q: float, n: int) -> float:
    """How many times wider your measurement error is than your edge.

    > 1 means you cannot tell your edge from zero at this sample size.
    This is the single number that kills or clears the strategy.
    """
    e = abs(edge_pp(p, q))
    if e == 0:
        return math.inf
    return ci_halfwidth_pp(p, n) / e


# ----------------------------------------------------------------------
# Position report
# ----------------------------------------------------------------------

@dataclass
class Position:
    label: str
    side: str
    p: float                 # entry price (or mark — caller must know which)
    shares: float
    days_to_resolution: float | None = None
    fee_bps: float | None = None
    q: float | None = None   # your true-probability estimate, if any

    def report(self) -> dict:
        p_eff = net_price(self.p, self.fee_bps) if self.fee_bps is not None else self.p
        cost = self.shares * p_eff
        payoff = self.shares * 1.0
        profit = payoff - cost
        r = profit / cost

        out = {
            "label": self.label,
            "side": self.side,
            "entry_price": round(self.p, 6),
            "effective_price": round(p_eff, 6),
            "shares": self.shares,
            "capital_committed": round(cost, 2),
            "profit_if_right": round(profit, 2),
            "loss_if_wrong": round(cost, 2),
            "gross_return_pct": round(r * 100, 4),
            "loss_to_gain": round(loss_to_gain(p_eff), 2),
            "breakeven_true_prob_pct": round(breakeven_q(p_eff) * 100, 3),
        }

        if self.days_to_resolution:
            out["days_to_resolution"] = self.days_to_resolution
            out["annualized_pct"] = round(
                annualize(r, self.days_to_resolution) * 100, 2)

        if self.q is not None:
            f = kelly_fraction(p_eff, self.q)
            out["your_true_prob_pct"] = round(self.q * 100, 3)
            out["edge_pp"] = round(edge_pp(p_eff, self.q), 4)
            out["kelly_fraction"] = round(f, 5)
            out["kelly_verdict"] = (
                "NEGATIVE EV AT YOUR OWN ESTIMATE — size is zero"
                if f <= 0 else f"{f*100:.1f}% of bankroll (full Kelly)"
            )
        return out


def _check_price(x: float, name: str = "p") -> None:
    if not (0.0 < x < 1.0):
        raise ValueError(f"{name} must be strictly between 0 and 1, got {x}")


__all__ = [
    "Position", "gross_return", "loss_to_gain", "breakeven_q", "annualize",
    "net_price", "kelly_fraction", "edge_pp", "ci_halfwidth_pp",
    "n_required", "measurement_to_edge_ratio",
]

"""
Risk of ruin for repeated near-certainty carry.

Two independent derivations of the same quantity, because one is not a
finding.

    Path A: Monte Carlo over sampled cycle paths.
    Path B: exact forward DP over the (cycle, loss-count) lattice with
            absorbing ruin states. Deterministic, no sampling.

BUG HISTORY (kept deliberately — this is the kill record for this file):
    v1 Path B computed P(loss count at HORIZON >= k*). That is
    ruin-at-the-end, not ruin-ever. Terminal wealth is order-independent;
    ruin is not. A path that takes six early losses and then recovers is
    ruined, and v1 scored it survived. Path A caught it: MC 3.91% vs
    "exact" 1.73% at p=0.95, f=0.20, 52 cycles. Had both paths been
    written the same way, the bug would have shipped agreeing with itself.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.stats import binom

from economics import gross_return


@dataclass
class RuinResult:
    p_ruin_ever_mc: float
    p_ruin_ever_exact: float
    p_ruin_at_horizon: float
    agree: bool
    median_terminal_multiple: float
    p5_terminal_multiple: float
    mean_terminal_multiple: float
    expected_losses: float
    losses_to_ruin_at_horizon: int | None

    def __str__(self) -> str:
        flag = "" if self.agree else "   <-- PATHS DISAGREE, DO NOT TRUST"
        return (
            f"P(ruin ever)  MC={self.p_ruin_ever_mc:.4%}  "
            f"DP={self.p_ruin_ever_exact:.4%}{flag}\n"
            f"P(ruin at horizon only) = {self.p_ruin_at_horizon:.4%}\n"
            f"terminal multiple:  p5={self.p5_terminal_multiple:.3f}  "
            f"median={self.median_terminal_multiple:.3f}  "
            f"mean={self.mean_terminal_multiple:.3f}\n"
            f"expected losses = {self.expected_losses:.2f}   "
            f"(horizon ruin needs {self.losses_to_ruin_at_horizon})"
        )


def wealth(p: float, f: float, wins: int, losses: int) -> float:
    """Wealth multiple after `wins` wins and `losses` losses. Order does
    not matter for the VALUE; it does matter for whether ruin was touched
    on the way, which is why exact_ruin_ever walks the lattice."""
    return (1.0 + f * gross_return(p)) ** wins * (1.0 - f) ** losses


def losses_to_ruin(p: float, f: float, cycles: int,
                   ruin_level: float) -> int | None:
    """Smallest loss count k that ruins you even with every other cycle
    won. Closed form; monotone in k."""
    for k in range(cycles + 1):
        if wealth(p, f, cycles - k, k) < ruin_level:
            return k
    return None


def exact_ruin_ever(p: float, q: float, f: float, cycles: int,
                    ruin_level: float) -> float:
    """PATH B. Forward DP over the (m, j) lattice: m cycles elapsed, j of
    them losses. Wealth at (m, j) is deterministic. States below
    ruin_level are absorbing and their mass is banked, never propagated.

    Exact to floating point. No sampling.
    """
    if not (0.0 <= f < 1.0):
        raise ValueError("f must be in [0, 1)")
    ruined = 0.0
    layer = np.array([1.0])          # layer[j] = P(at cycle m, j losses, alive)
    for m in range(1, cycles + 1):
        nxt = np.zeros(m + 1)
        nxt[:m] += layer * q                 # win  -> loss count unchanged
        nxt[1:] += layer * (1.0 - q)         # loss -> loss count + 1
        for j in range(m + 1):
            if nxt[j] > 0.0 and wealth(p, f, m - j, j) < ruin_level:
                ruined += nxt[j]
                nxt[j] = 0.0
        layer = nxt
    return float(ruined)


def ruin_at_horizon(p: float, q: float, f: float, cycles: int,
                    ruin_level: float) -> float:
    """P(below ruin_level at the END). Strictly <= ruin-ever. Reported
    separately so the gap between them stays visible rather than
    conflated."""
    k = losses_to_ruin(p, f, cycles, ruin_level)
    if k is None:
        return 0.0
    return float(binom.sf(k - 1, cycles, 1.0 - q))


def monte_carlo(p: float, q: float, f: float, cycles: int,
                ruin_level: float, trials: int = 200_000,
                seed: int = 7) -> tuple[float, np.ndarray]:
    """PATH A."""
    b = gross_return(p)
    rng = np.random.default_rng(seed)
    wins = rng.random((trials, cycles)) < q
    step = np.where(wins, 1.0 + f * b, 1.0 - f)
    paths = np.cumprod(step, axis=1)
    return float((paths < ruin_level).any(axis=1).mean()), paths[:, -1]


def assess(p: float, q: float, f: float, cycles: int,
           ruin_level: float = 0.5, trials: int = 200_000,
           tol: float = 0.005) -> RuinResult:
    mc, terminal = monte_carlo(p, q, f, cycles, ruin_level, trials)
    dp = exact_ruin_ever(p, q, f, cycles, ruin_level)
    return RuinResult(
        p_ruin_ever_mc=mc,
        p_ruin_ever_exact=dp,
        p_ruin_at_horizon=ruin_at_horizon(p, q, f, cycles, ruin_level),
        agree=abs(mc - dp) <= tol,
        median_terminal_multiple=float(np.median(terminal)),
        p5_terminal_multiple=float(np.percentile(terminal, 5)),
        mean_terminal_multiple=float(terminal.mean()),
        expected_losses=cycles * (1.0 - q),
        losses_to_ruin_at_horizon=losses_to_ruin(p, f, cycles, ruin_level),
    )


def max_safe_fraction(p: float, q: float, cycles: int,
                      max_p_ruin: float = 0.01,
                      ruin_level: float = 0.5) -> float:
    """Largest f whose exact P(ruin ever) stays under max_p_ruin.

    Bisects on the DP only — bisecting a Monte Carlo estimate is
    bisecting noise.
    """
    lo, hi = 0.0, 0.99
    if exact_ruin_ever(p, q, hi, cycles, ruin_level) <= max_p_ruin:
        return hi
    for _ in range(40):
        mid = (lo + hi) / 2.0
        if exact_ruin_ever(p, q, mid, cycles, ruin_level) <= max_p_ruin:
            lo = mid
        else:
            hi = mid
    return lo


__all__ = ["assess", "exact_ruin_ever", "ruin_at_horizon", "monte_carlo",
           "losses_to_ruin", "max_safe_fraction", "wealth", "RuinResult"]

"""
Depth-constrained fill.

The reason this file exists: the EDGAR liquidity study found the signal
lived almost entirely in untradeable names. The same failure is available
here in a nastier form — a 98.2c quote with $400 of depth behind it looks
identical on a screen to one with $3M behind it.

A quote is not a price. VWAP over consumed depth is a price.

Book format (both venues normalise to this):
    levels = [(price, size_in_shares), ...]  best price first
"""

from __future__ import annotations

from dataclasses import dataclass

from economics import annualize, gross_return


@dataclass
class Fill:
    requested_notional: float
    filled_notional: float
    shares: float
    vwap: float
    top_of_book: float
    slippage_pp: float
    levels_consumed: int
    unfilled_notional: float

    @property
    def complete(self) -> bool:
        return self.unfilled_notional <= 1e-6


def simulate_fill(levels: list[tuple[float, float]],
                  notional: float) -> Fill:
    """Walk the book spending `notional` dollars. Returns the real fill."""
    if not levels:
        raise ValueError("empty book — cannot fill against nothing")
    remaining = float(notional)
    shares = 0.0
    spent = 0.0
    consumed = 0
    for price, size in levels:
        if remaining <= 1e-9:
            break
        level_notional = price * size
        take = min(level_notional, remaining)
        shares += take / price
        spent += take
        remaining -= take
        consumed += 1
    vwap = spent / shares if shares else float("nan")
    return Fill(
        requested_notional=float(notional),
        filled_notional=spent,
        shares=shares,
        vwap=vwap,
        top_of_book=levels[0][0],
        slippage_pp=(vwap - levels[0][0]) * 100.0,
        levels_consumed=consumed,
        unfilled_notional=remaining,
    )


def book_depth_notional(levels: list[tuple[float, float]],
                        worst_price: float) -> float:
    """Total dollars available at or better than worst_price."""
    return sum(pr * sz for pr, sz in levels if pr <= worst_price)


def max_notional_at_return(levels: list[tuple[float, float]],
                           min_annual_return: float,
                           days: float,
                           step: float = 250.0,
                           cap: float = 5_000_000.0) -> float:
    """Largest notional whose depth-adjusted VWAP still clears the
    required annualised return.

    Coarse ladder rather than a solve: the book is a step function, so a
    continuous root-find would report a precision the data cannot support
    (see: precision theater).
    """
    best = 0.0
    n = step
    while n <= cap:
        f = simulate_fill(levels, n)
        if not f.complete:
            break
        r = annualize(gross_return(f.vwap), days)
        if r < min_annual_return:
            break
        best = n
        n += step
    return best


def summarise(levels: list[tuple[float, float]], notional: float,
              days: float) -> dict:
    f = simulate_fill(levels, notional)
    quoted_r = annualize(gross_return(f.top_of_book), days)
    real_r = annualize(gross_return(f.vwap), days) if f.shares else float("nan")
    return {
        "top_of_book": round(f.top_of_book, 4),
        "vwap": round(f.vwap, 4),
        "slippage_pp": round(f.slippage_pp, 3),
        "shares": round(f.shares, 2),
        "filled_notional": round(f.filled_notional, 2),
        "unfilled_notional": round(f.unfilled_notional, 2),
        "complete_fill": f.complete,
        "quoted_annual_pct": round(quoted_r * 100, 2),
        "realised_annual_pct": round(real_r * 100, 2),
        "return_lost_to_depth_pct": round((quoted_r - real_r) * 100, 2),
    }


__all__ = ["simulate_fill", "book_depth_notional", "max_notional_at_return",
           "summarise", "Fill"]

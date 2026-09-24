"""Black-Scholes implied volatility -- for MEASUREMENT, never for a decision.

Used by the structure shadow to stamp each entry with the long option's
implied volatility, so the go-live report can ask whether expensive options
(high IV) should be avoided or sized down. Nothing trades on this number.

European Black-Scholes on NSE stock options, which are European-exercise. No
dividends: a dividend inside the window is rare on these ~30-day holds and
moves the answer by far less than the bid-ask does. Standard library only --
`math.erf` gives the normal CDF, so the Pi needs no scipy.

RETIRES WHEN: the broker's own IV/greeks feed is recorded at entry instead.
"""
from __future__ import annotations

import math
from typing import Optional

#: Bisection bounds, annualised. 1% to 500% covers every listed option; a
#: price outside the range the model can reach returns None, never a clamp.
IV_LO, IV_HI = 0.01, 5.0
IV_TOL = 1e-5
IV_MAX_ITER = 200


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(kind: str, spot: float, strike: float, t_years: float,
             rate: float, sigma: float) -> float:
    """Black-Scholes price of a European CE or PE."""
    if t_years <= 0 or sigma <= 0:
        intrinsic = spot - strike if kind == 'CE' else strike - spot
        return max(0.0, intrinsic)
    sq = sigma * math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * t_years) / sq
    d2 = d1 - sq
    disc = strike * math.exp(-rate * t_years)
    if kind == 'CE':
        return spot * _ncdf(d1) - disc * _ncdf(d2)
    return disc * _ncdf(-d2) - spot * _ncdf(-d1)


def implied_vol(kind: str, price: float, spot: float, strike: float,
                t_years: float, rate: float) -> Optional[float]:
    """Annualised IV (0.32 = 32%) that reproduces `price`, or None.

    None -- not a guess -- when the inputs cannot carry a volatility: a
    non-positive price/spot/strike/time, an unknown option type, or a price
    at or below the discounted intrinsic (no time value left to explain) or
    beyond what any volatility produces. Price is monotonic in sigma, so
    bisection always converges inside the bracket.
    """
    try:
        price, spot, strike, t_years = float(price), float(spot), float(strike), float(t_years)
    except (TypeError, ValueError):
        return None
    if kind not in ('CE', 'PE') or min(price, spot, strike, t_years) <= 0:
        return None
    lo_p = bs_price(kind, spot, strike, t_years, rate, IV_LO)
    hi_p = bs_price(kind, spot, strike, t_years, rate, IV_HI)
    if not (lo_p < price < hi_p):
        return None
    lo, hi = IV_LO, IV_HI
    for _ in range(IV_MAX_ITER):
        mid = 0.5 * (lo + hi)
        if bs_price(kind, spot, strike, t_years, rate, mid) < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < IV_TOL:
            break
    return 0.5 * (lo + hi)

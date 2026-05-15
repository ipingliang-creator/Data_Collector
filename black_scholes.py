"""Black-Scholes pricing + IV solver for European puts.

Pure-Python implementation (`math.erf` for the normal CDF) so we
don't pull in scipy just to compute IV. Used by the IV backfill to
recover historical implied vol from historical option closes.

Conventions
-----------
- `S`: spot price of the underlying
- `K`: strike price
- `T`: time to expiry in **years** (e.g. 30 days → 30/365 ≈ 0.0822)
- `r`: continuously-compounded risk-free rate, as a fraction (0.045 = 4.5%)
- `sigma`: implied volatility, as a fraction per year (0.30 = 30%)
- `q`: continuously-compounded dividend yield (default 0; we don't have
  per-ticker dividend yields wired up yet — this is a known simplification)

Sign convention for `solve_put_iv`: returns IV as a positive fraction.
"""
from __future__ import annotations

import math


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_put_price(
    S: float, K: float, T: float, r: float, sigma: float, *, q: float = 0.0
) -> float:
    """Black-Scholes-Merton European put price.

    Edge cases handled:
    - `T <= 0`: returns the intrinsic value `max(K - S, 0)`.
    - `sigma <= 0`: returns the discounted intrinsic value
      `max(K * exp(-rT) - S * exp(-qT), 0)`.
    """
    if T <= 0:
        return max(K - S, 0.0)
    if sigma <= 0:
        return max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    sqrt_t = math.sqrt(T)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * math.exp(-q * T) * _norm_cdf(-d1)


def solve_put_iv(
    S: float,
    K: float,
    T: float,
    r: float,
    market_price: float,
    *,
    q: float = 0.0,
    low: float = 1e-4,
    high: float = 5.0,
    tol: float = 1e-5,
    max_iter: int = 100,
) -> float | None:
    """Solve for implied volatility given an observed put market price.

    Bisection on `[low, high]`. Returns the IV (fraction) that makes
    `bs_put_price(S, K, T, r, σ) == market_price` within `tol`. Returns
    `None` when:
    - `market_price` is below intrinsic (impossible market → bad input)
    - `market_price` exceeds the high-σ bound's BS price (option price
      too rich for any IV in `[low, high]` — likely bad input)
    - `T <= 0` (no time value left; IV is undefined)

    Bisection is slower than Newton but doesn't need vega and is robust
    around the corners (vega → 0 as σ → 0 for deep ITM/OTM puts).
    """
    if T <= 0 or market_price < 0:
        return None
    intrinsic = max(K * math.exp(-r * T) - S * math.exp(-q * T), 0.0)
    if market_price < intrinsic - tol:
        return None  # impossible — market below discounted intrinsic
    p_low = bs_put_price(S, K, T, r, low, q=q)
    p_high = bs_put_price(S, K, T, r, high, q=q)
    if market_price < p_low - tol or market_price > p_high + tol:
        return None  # outside our σ search band

    lo, hi = low, high
    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        price_mid = bs_put_price(S, K, T, r, mid, q=q)
        if abs(price_mid - market_price) < tol:
            return mid
        if price_mid < market_price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)

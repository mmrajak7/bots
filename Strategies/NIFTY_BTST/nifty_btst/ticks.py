"""Tick rounding in integer arithmetic (copied from ETF_PI/neo/tick.py, which
the audit marked reuse-as-is). NFO options tick at Rs 0.05; NEO rejects a
price off the grid with `16283 : Order price is not a multiple of tick size`,
and float arithmetic (160.65 * 20 = 3212.9999) is how a price leaves it."""
from __future__ import annotations

import math


def round_tick(price: float, tick: float = 0.05, mode: str = 'nearest') -> float:
    if price is None or tick <= 0 or price <= 0:
        return 0.0
    mult = round(1.0 / tick)
    ticks = price * mult
    if mode == 'down':
        n = math.floor(ticks + 1e-9)
    elif mode == 'up':
        n = math.ceil(ticks - 1e-9)
    else:
        n = int(math.floor(ticks + 0.5))
    return n / mult


def price_str(price: float, tick: float = 0.05, mode: str = 'nearest') -> str:
    """The wire format: two decimals, on the tick grid."""
    return '%.2f' % round_tick(price, tick, mode)

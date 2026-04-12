"""
neo.tick — Tick-size rounding utilities.

Uses integer arithmetic to avoid floating-point precision bugs.
NFO tick size: 0.05 (all NIFTY options).

Adapted from Scalper core/tick_utils.py.
"""

import math

NFO_TICK = 0.05


def round_to_tick_str(price, tick_size=NFO_TICK, direction='down'):
    """Round price to tick size, return formatted string.

    Args:
        price: Raw price (float).
        tick_size: Tick increment (default 0.05 for NFO).
        direction: 'down' (floor) or 'up' (ceil).

    Returns:
        Price as string (e.g., "116.40").
    """
    if price is None or tick_size <= 0:
        return "0.00"
    multiplier = round(1 / tick_size)
    price_in_ticks = price * multiplier
    if direction == 'up':
        ticks = math.ceil(price_in_ticks)
    else:
        ticks = math.floor(price_in_ticks)
    return f"{ticks / multiplier:.2f}"


def round_to_tick(price, tick_size=NFO_TICK, direction='nearest'):
    """Round price to tick size, return float.

    Args:
        price: Raw price (float).
        tick_size: Tick increment (default 0.05 for NFO).
        direction: 'down' (floor), 'up' (ceil), or 'nearest' (round).

    Returns:
        Price as float.
    """
    if price is None or tick_size <= 0:
        return 0.0
    multiplier = round(1 / tick_size)
    price_in_ticks = price * multiplier
    if direction == 'down':
        ticks = math.floor(price_in_ticks)
    elif direction == 'up':
        ticks = math.ceil(price_in_ticks)
    else:
        ticks = round(price_in_ticks)
    return ticks / multiplier


def format_price(price):
    """Format price rounded down to NFO tick size."""
    return round_to_tick_str(price, NFO_TICK, 'down')

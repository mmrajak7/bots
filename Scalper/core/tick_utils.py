"""
Tick Size Utilities for Options/Equity Price Rounding

Created in Session 31 to fix floating-point precision bugs.
Uses integer arithmetic to avoid issues like:
    math.floor(116.415/0.05)*0.05 = 116.39999999999999

NSE Options tick size: 0.05
NSE Equity tick size: 0.05
"""

import math
from typing import Tuple


# Tick sizes by exchange segment
TICK_SIZES = {
    'nse_fo': 0.05,      # NSE F&O (Options, Futures) - tick size 0.05
    'bse_fo': 0.05,      # BSE F&O - tick size 0.05
    'nse_cm': 0.05,      # NSE Cash Market (Equity)
    'bse_cm': 0.01,      # BSE Cash Market
    'cde_fo': 0.0025,    # Currency derivatives
    # Aliases for NEO API variations (returns 'NFO'/'BFO' in some endpoints)
    'nfo': 0.05,         # Alias for nse_fo
    'bfo': 0.05,         # Alias for bse_fo
    'ncm': 0.05,         # Alias for nse_cm
    'bcm': 0.01,         # Alias for bse_cm
    'cds': 0.0025,       # Alias for cde_fo
    'default': 0.05,     # Default - safer at 0.05
}


def get_tick_size(exchange_segment: str = 'nse_fo') -> float:
    """Get tick size for exchange segment."""
    if not exchange_segment:
        return TICK_SIZES['default']
    return TICK_SIZES.get(exchange_segment.lower(), TICK_SIZES['default'])


def round_to_tick(price: float, tick_size: float = 0.05, direction: str = 'nearest') -> float:
    """
    Round price to tick size using integer arithmetic, returning float.

    Args:
        price: The price to round
        tick_size: Tick size (default 0.05 for options)
        direction: 'down' (floor), 'up' (ceil), or 'nearest' (round)

    Returns:
        Price as float rounded to tick size
    """
    if price is None or tick_size <= 0:
        return 0.0

    multiplier = round(1 / tick_size)
    price_in_ticks = price * multiplier

    if direction == 'down':
        ticks = math.floor(price_in_ticks)
    elif direction == 'up':
        ticks = math.ceil(price_in_ticks)
    else:  # nearest
        ticks = round(price_in_ticks)

    return ticks / multiplier


def round_to_tick_str(price: float, tick_size: float = 0.05, direction: str = 'down') -> str:
    """
    Round price to tick size using integer arithmetic.

    Args:
        price: The price to round
        tick_size: Tick size (default 0.05 for options)
        direction: 'down' (floor) or 'up' (ceil)

    Returns:
        Price as formatted string (e.g., "116.40")

    Example:
        >>> round_to_tick_str(116.42, 0.05, 'down')
        '116.40'
        >>> round_to_tick_str(116.42, 0.05, 'up')
        '116.45'
    """
    if price is None or tick_size <= 0:
        return "0.00"

    # Use integer arithmetic to avoid floating-point errors
    # multiplier = 20 for tick_size 0.05 (1/0.05 = 20)
    multiplier = round(1 / tick_size)
    price_in_ticks = price * multiplier

    if direction == 'down':
        ticks = math.floor(price_in_ticks)  # Floor toward negative infinity
    else:  # 'up'
        ticks = math.ceil(price_in_ticks)

    # Convert back and format as string
    result = ticks / multiplier
    return f"{result:.2f}"


def round_trigger_price(price: float, exchange_segment: str = 'nse_fo') -> str:
    """
    Round trigger price to nearest tick size for the exchange.

    For trigger prices, we round to nearest (not floor/ceil).

    Args:
        price: The trigger price
        exchange_segment: Exchange segment for tick size lookup (e.g., 'nse_fo', 'nse_cm')

    Returns:
        Price as formatted string

    Example:
        >>> round_trigger_price(116.42, 'nse_fo')
        '116.40'
        >>> round_trigger_price(116.43, 'nse_fo')
        '116.45'
    """
    if price is None:
        return "0.00"

    tick_size = get_tick_size(exchange_segment)
    if tick_size <= 0:
        return "0.00"

    multiplier = round(1 / tick_size)
    price_in_ticks = price * multiplier
    ticks = round(price_in_ticks)  # Round to nearest

    result = ticks / multiplier
    return f"{result:.2f}"


def format_price(price: float, exchange_segment: str = 'nse_fo') -> str:
    """
    Format price as string, rounded to tick size for the exchange.

    Args:
        price: The price to format
        exchange_segment: Exchange segment for tick size lookup (e.g., 'nse_fo', 'nse_cm')

    Returns:
        Formatted price string rounded to tick size

    Example:
        >>> format_price(116.42, 'nse_fo')
        '116.40'
        >>> format_price(116.42, 'bse_cm')
        '116.42'
    """
    if price is None:
        return "0.00"

    tick_size = get_tick_size(exchange_segment)
    return round_to_tick_str(price, tick_size, direction='down')


def calculate_sl_limit_price(
    trigger_price: float,
    transaction_type: str,
    exchange_segment: str = 'nse_fo',
    buffer_percent: float = 0.5
) -> Tuple[str, str]:
    """
    Calculate SL-L limit price with proper tick size handling.

    For options (nse_fo, bse_fo): Returns SL-L order with limit price
    For equity (nse_cm): Returns SL-M order (market) with price "0"

    Args:
        trigger_price: The stop loss trigger price
        transaction_type: 'S' (Sell) or 'B' (Buy)
        exchange_segment: Exchange segment for tick size lookup
        buffer_percent: Buffer as percentage (default 0.5%)

    Returns:
        Tuple of (order_type, limit_price_str)
        - ("SL", "116.40") for options
        - ("SL-M", "0") for equity

    Example:
        >>> calculate_sl_limit_price(117.0, 'S', 'nse_fo')
        ('SL', '116.40')
    """
    if trigger_price is None or trigger_price <= 0:
        return ("SL-M", "0")

    segment_lower = exchange_segment.lower() if exchange_segment else 'nse_fo'

    # For equity, use SL-M (market) - no limit price needed
    if segment_lower in ('nse_cm', 'bse_cm'):
        return ("SL-M", "0")

    # For options/futures, calculate limit price with buffer
    tick_size = get_tick_size(segment_lower)

    # Buffer: minimum 0.5 or buffer_percent% of trigger
    buffer = max(0.5, trigger_price * (buffer_percent / 100))

    if transaction_type == 'S':
        # SELL SL: Limit price BELOW trigger - round DOWN to ensure fill
        raw_limit = trigger_price - buffer
        limit_price = round_to_tick_str(raw_limit, tick_size, direction='down')
    else:
        # BUY SL: Limit price ABOVE trigger - round UP to ensure fill
        raw_limit = trigger_price + buffer
        limit_price = round_to_tick_str(raw_limit, tick_size, direction='up')

    return ("SL", limit_price)


# Test the functions if run directly
if __name__ == "__main__":
    print("Tick Utils Test")
    print("=" * 40)

    # Test case from Session 30/31: trigger 117.0, buffer ~0.585
    trigger = 117.0
    buffer = max(0.5, trigger * 0.005)  # 0.585
    raw_limit = trigger - buffer  # 116.415

    print(f"Trigger: {trigger}")
    print(f"Buffer: {buffer}")
    print(f"Raw limit: {raw_limit}")
    print(f"round_to_tick_str({raw_limit}, 0.05, 'down'): {round_to_tick_str(raw_limit, 0.05, 'down')}")
    print()

    # Test calculate_sl_limit_price
    order_type, limit = calculate_sl_limit_price(117.0, 'S', 'nse_fo')
    print(f"calculate_sl_limit_price(117.0, 'S', 'nse_fo'): ({order_type}, {limit})")

    order_type, limit = calculate_sl_limit_price(117.0, 'B', 'nse_fo')
    print(f"calculate_sl_limit_price(117.0, 'B', 'nse_fo'): ({order_type}, {limit})")

    order_type, limit = calculate_sl_limit_price(117.0, 'S', 'nse_cm')
    print(f"calculate_sl_limit_price(117.0, 'S', 'nse_cm'): ({order_type}, {limit})")

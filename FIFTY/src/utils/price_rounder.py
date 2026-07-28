"""Price Rounding Utilities for NSE Tick Size"""

import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union
import math


# Tick sizes the exchange actually publishes. Used to sanity-bound anything
# parsed out of a broker error string - see parse_required_tick().
KNOWN_TICK_SIZES = (0.01, 0.05, 0.10, 0.25, 0.50, 1.00, 2.00, 2.50, 5.00, 10.00)


def parse_required_tick(error_text: str) -> Optional[float]:
    """Extract the tick size Zerodha demands from a rejection message.

    Zerodha phrases this at least two ways:
        "Trigger price should be a multiple of tick size 0.50."
        "... tick size is 0.05"
    and NSE has digit-LEADING symbols (20MICRONS, 3MINDIA, 360ONE, 5PAISA),
    so a naive "first number after 'tick size'" parse can capture the symbol's
    digits instead - e.g. "Tick size for 20MICRONS is 0.05" -> 20. Acting on
    that would silently re-round an order to a multiple of 20.

    So: scan every number after "tick size" and accept only the first that is a
    real exchange tick. Returns None when nothing plausible is found, and the
    caller must then fall through to its non-retry path rather than guess.
    """
    if not error_text:
        return None
    m = re.search(r'tick size(.*)$', error_text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    # TWO independent guards, because either alone is defeatable:
    #  - the lookarounds skip numbers glued to letters, so a symbol token is
    #    never read as a tick ("20MICRONS", "360ONE", "5PAISA");
    #  - the whitelist rejects any standalone value that isn't a real tick.
    # 5PAISA is why both are needed: its leading "5" IS a valid tick value, so
    # the whitelist alone would happily return 5.0 for a 0.01-tick scrip.
    for token in re.findall(r'(?<![\w.])(\d+(?:\.\d+)?)(?!\w)', m.group(1)):
        try:
            value = float(token)
        except ValueError:
            continue
        if any(abs(value - t) < 1e-9 for t in KNOWN_TICK_SIZES):
            return value
    return None


class PriceRounder:
    """Utility class for rounding prices to NSE tick size"""

    @staticmethod
    def get_nse_tick_size(price: float) -> float:
        """
        Get NSE tick size based on price range

        NSE Tick Size Rules:
        - Price < Rs.1000: tick size = 0.05
        - Price >= Rs.1000: tick size = 0.10
        """
        if price < 1000:
            return 0.05
        else:
            return 0.10

    @staticmethod
    def round_to_tick(price: Union[float, Decimal], tick_size: float = None) -> float:
        """Round price to nearest tick size (auto-detects NSE tick size based on price)"""
        if price is None or price <= 0:
            raise ValueError(f"Invalid price: {price}")

        if tick_size is None:
            tick_size = PriceRounder.get_nse_tick_size(float(price))

        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))

        num_ticks = (price_decimal / tick_decimal).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        rounded_price = float(num_ticks * tick_decimal)

        return rounded_price

    @staticmethod
    def round_down_to_tick(price: Union[float, Decimal], tick_size: float = None) -> float:
        """Round price DOWN to nearest tick size (for stop loss)"""
        if tick_size is None:
            tick_size = PriceRounder.get_nse_tick_size(float(price))
        return math.floor(price / tick_size) * tick_size

    @staticmethod
    def round_up_to_tick(price: Union[float, Decimal], tick_size: float = None) -> float:
        """Round price UP to nearest tick size (for entry prices)"""
        if tick_size is None:
            tick_size = PriceRounder.get_nse_tick_size(float(price))
        return math.ceil(price / tick_size) * tick_size


# Convenience functions
def round_price(price: float) -> float:
    """Round price to NSE tick size"""
    return PriceRounder.round_to_tick(price)


def round_price_down(price: float) -> float:
    """Round price down to NSE tick size"""
    return PriceRounder.round_down_to_tick(price)


def round_price_up(price: float) -> float:
    """Round price up to NSE tick size"""
    return PriceRounder.round_up_to_tick(price)

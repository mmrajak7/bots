"""Price Rounding Utilities for NSE Tick Size (0.05)"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Union


class PriceRounder:
    """Utility class for rounding prices to NSE tick size"""

    @staticmethod
    def round_to_tick(price: Union[float, Decimal], tick_size: float = 0.05) -> float:
        """
        Round price to nearest tick size (default 0.05 for NSE)

        Args:
            price: Price to round
            tick_size: Tick size (default 0.05)

        Returns:
            Rounded price

        Examples:
            >>> PriceRounder.round_to_tick(2497.87)
            2497.85
            >>> PriceRounder.round_to_tick(1234.53)
            1234.55
            >>> PriceRounder.round_to_tick(100.02)
            100.00
        """
        if price is None or price <= 0:
            raise ValueError(f"Invalid price: {price}")

        # Convert to Decimal for precise rounding
        price_decimal = Decimal(str(price))
        tick_decimal = Decimal(str(tick_size))

        # Round to nearest tick
        num_ticks = (price_decimal / tick_decimal).quantize(Decimal('1'), rounding=ROUND_HALF_UP)
        rounded_price = float(num_ticks * tick_decimal)

        return rounded_price

    @staticmethod
    def round_down_to_tick(price: Union[float, Decimal], tick_size: float = 0.05) -> float:
        """
        Round price DOWN to nearest tick size

        Useful for stop loss prices (conservative rounding)
        """
        import math
        return math.floor(price / tick_size) * tick_size

    @staticmethod
    def round_up_to_tick(price: Union[float, Decimal], tick_size: float = 0.05) -> float:
        """
        Round price UP to nearest tick size

        Useful for entry prices (conservative rounding)
        """
        import math
        return math.ceil(price / tick_size) * tick_size


# Convenience functions
def round_price(price: float) -> float:
    """Round price to NSE tick size (0.05)"""
    return PriceRounder.round_to_tick(price)


def round_price_down(price: float) -> float:
    """Round price down to NSE tick size"""
    return PriceRounder.round_down_to_tick(price)


def round_price_up(price: float) -> float:
    """Round price up to NSE tick size"""
    return PriceRounder.round_up_to_tick(price)

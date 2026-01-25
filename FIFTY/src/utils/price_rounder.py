"""Price Rounding Utilities for NSE Tick Size"""

from decimal import Decimal, ROUND_HALF_UP
from typing import Union
import math


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

"""
Supertrend Indicator Calculator

Calculates Supertrend indicator from OHLC candle data.
Uses ATR (Average True Range) based calculation.

Parameters:
- Period: 10 (default)
- Multiplier: 3 (default)
"""

import logging
from dataclasses import dataclass
from typing import List, Optional, Dict, Any
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class SupertrendResult:
    """Result of Supertrend calculation."""
    direction: str  # 'UP' or 'DOWN'
    value: float    # Supertrend line value
    close: float    # Last close price


def calculate_atr(candles: List[Dict], period: int = 10) -> List[float]:
    """
    Calculate Average True Range (ATR).

    Args:
        candles: List of OHLC dicts with 'high', 'low', 'close' keys
        period: ATR period (default 10)

    Returns:
        List of ATR values (first period-1 values are 0)
    """
    if len(candles) < period:
        return [0.0] * len(candles)

    tr_values = []

    for i, candle in enumerate(candles):
        high = candle['high']
        low = candle['low']

        if i == 0:
            # First candle: TR = High - Low
            tr = high - low
        else:
            prev_close = candles[i - 1]['close']
            # TR = max(High - Low, |High - Prev Close|, |Low - Prev Close|)
            tr = max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
        tr_values.append(tr)

    # Calculate ATR using simple moving average
    atr_values = [0.0] * len(candles)

    # First ATR = average of first 'period' TR values
    if len(tr_values) >= period:
        atr_values[period - 1] = sum(tr_values[:period]) / period

        # Subsequent ATR = ((prev ATR * (period - 1)) + current TR) / period
        for i in range(period, len(tr_values)):
            atr_values[i] = ((atr_values[i - 1] * (period - 1)) + tr_values[i]) / period

    return atr_values


def calculate_supertrend(
    candles: List[Dict],
    period: int = 10,
    multiplier: float = 3.0
) -> Optional[SupertrendResult]:
    """
    Calculate Supertrend indicator.

    Args:
        candles: List of OHLC dicts with 'high', 'low', 'close', 'open' keys
                 Must be in chronological order (oldest first)
        period: ATR period (default 10)
        multiplier: ATR multiplier (default 3.0)

    Returns:
        SupertrendResult with direction ('UP' or 'DOWN') and value,
        or None if not enough data
    """
    min_candles = period + 1
    if len(candles) < min_candles:
        logger.warning(f"Not enough candles for Supertrend: {len(candles)} < {min_candles}")
        return None

    # Calculate ATR
    atr_values = calculate_atr(candles, period)

    # Initialize arrays
    n = len(candles)
    upper_band = [0.0] * n
    lower_band = [0.0] * n
    supertrend = [0.0] * n
    direction = [1] * n  # 1 = UP, -1 = DOWN

    for i in range(n):
        high = candles[i]['high']
        low = candles[i]['low']
        close = candles[i]['close']
        atr = atr_values[i]

        # Calculate basic upper and lower bands
        hl2 = (high + low) / 2
        basic_upper = hl2 + (multiplier * atr)
        basic_lower = hl2 - (multiplier * atr)

        if i == 0:
            upper_band[i] = basic_upper
            lower_band[i] = basic_lower
            supertrend[i] = basic_upper
            direction[i] = -1  # Start with DOWN
        else:
            prev_close = candles[i - 1]['close']
            prev_upper = upper_band[i - 1]
            prev_lower = lower_band[i - 1]
            prev_st = supertrend[i - 1]

            # Final upper band
            if basic_upper < prev_upper or prev_close > prev_upper:
                upper_band[i] = basic_upper
            else:
                upper_band[i] = prev_upper

            # Final lower band
            if basic_lower > prev_lower or prev_close < prev_lower:
                lower_band[i] = basic_lower
            else:
                lower_band[i] = prev_lower

            # Supertrend and direction
            if prev_st == prev_upper:
                # Was in downtrend
                if close > upper_band[i]:
                    # Switch to uptrend
                    supertrend[i] = lower_band[i]
                    direction[i] = 1
                else:
                    supertrend[i] = upper_band[i]
                    direction[i] = -1
            else:
                # Was in uptrend
                if close < lower_band[i]:
                    # Switch to downtrend
                    supertrend[i] = upper_band[i]
                    direction[i] = -1
                else:
                    supertrend[i] = lower_band[i]
                    direction[i] = 1

    # Return the last value
    last_idx = n - 1
    return SupertrendResult(
        direction='UP' if direction[last_idx] == 1 else 'DOWN',
        value=supertrend[last_idx],
        close=candles[last_idx]['close']
    )


def get_supertrend_direction(
    candles: List[Dict],
    period: int = 10,
    multiplier: float = 3.0
) -> Optional[str]:
    """
    Get just the Supertrend direction.

    Args:
        candles: OHLC candle data
        period: ATR period
        multiplier: ATR multiplier

    Returns:
        'UP', 'DOWN', or None if calculation fails
    """
    result = calculate_supertrend(candles, period, multiplier)
    return result.direction if result else None


# Test
if __name__ == "__main__":
    # Sample data for testing
    sample_candles = [
        {'open': 100, 'high': 105, 'low': 98, 'close': 103},
        {'open': 103, 'high': 108, 'low': 101, 'close': 107},
        {'open': 107, 'high': 110, 'low': 105, 'close': 106},
        {'open': 106, 'high': 109, 'low': 104, 'close': 108},
        {'open': 108, 'high': 112, 'low': 106, 'close': 111},
        {'open': 111, 'high': 115, 'low': 109, 'close': 114},
        {'open': 114, 'high': 118, 'low': 112, 'close': 116},
        {'open': 116, 'high': 120, 'low': 114, 'close': 119},
        {'open': 119, 'high': 122, 'low': 117, 'close': 121},
        {'open': 121, 'high': 125, 'low': 118, 'close': 123},
        {'open': 123, 'high': 126, 'low': 120, 'close': 122},
        {'open': 122, 'high': 124, 'low': 118, 'close': 119},
    ]

    result = calculate_supertrend(sample_candles)
    if result:
        print(f"Supertrend Direction: {result.direction}")
        print(f"Supertrend Value: {result.value:.2f}")
        print(f"Last Close: {result.close:.2f}")

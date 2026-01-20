"""
Conviction bar detection logic.
Identifies bars with strong directional commitment.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Tuple

from .bar import Bar
from ..utils.config import ConvictionParams


class ConvictionType(Enum):
    """Type of conviction detected."""
    NONE = "NONE"
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


@dataclass
class ConvictionResult:
    """
    Result of conviction bar analysis.

    Attributes:
        bar: The analyzed bar
        conviction_type: Type of conviction (NONE, BULLISH, BEARISH)
        is_valid: Whether this is a valid conviction bar
        skip_reason: Reason for skipping if not valid
        metrics: Dictionary of calculated metrics
    """
    bar: Bar
    conviction_type: ConvictionType
    is_valid: bool
    skip_reason: Optional[str] = None
    metrics: dict = None

    def __post_init__(self):
        if self.metrics is None:
            self.metrics = {}


def is_bullish_conviction(bar: Bar, params: ConvictionParams) -> Tuple[bool, Optional[str]]:
    """
    Check if bar qualifies as bullish conviction.

    A bullish conviction bar must:
    1. Be a green bar (close > open)
    2. Have range >= min_range_points and <= max_range_points
    3. Have body ratio >= min_body_ratio
    4. Have close position >= min_close_position_bull (close near high)

    Args:
        bar: Bar to analyze
        params: Conviction parameters

    Returns:
        Tuple of (is_conviction, skip_reason if not)
    """
    # Must be a green/bullish bar
    if not bar.is_bullish:
        return False, "Not a bullish bar"

    # Minimum range check
    if bar.range < params.min_range_points:
        return False, f"Range too small ({bar.range:.2f} < {params.min_range_points})"

    # Maximum range check
    if bar.range > params.max_range_points:
        return False, f"Range too large ({bar.range:.2f} > {params.max_range_points})"

    # Body ratio check
    if bar.body_ratio < params.min_body_ratio:
        return False, f"Body ratio too small ({bar.body_ratio:.2%} < {params.min_body_ratio:.2%})"

    # Close position check (must close near high)
    if bar.close_position < params.min_close_position_bull:
        return False, f"Close not high enough ({bar.close_position:.2%} < {params.min_close_position_bull:.2%})"

    return True, None


def is_bearish_conviction(bar: Bar, params: ConvictionParams) -> Tuple[bool, Optional[str]]:
    """
    Check if bar qualifies as bearish conviction.

    A bearish conviction bar must:
    1. Be a red bar (close < open)
    2. Have range >= min_range_points and <= max_range_points
    3. Have body ratio >= min_body_ratio
    4. Have close position <= max_close_position_bear (close near low)

    Args:
        bar: Bar to analyze
        params: Conviction parameters

    Returns:
        Tuple of (is_conviction, skip_reason if not)
    """
    # Must be a red/bearish bar
    if not bar.is_bearish:
        return False, "Not a bearish bar"

    # Minimum range check
    if bar.range < params.min_range_points:
        return False, f"Range too small ({bar.range:.2f} < {params.min_range_points})"

    # Maximum range check
    if bar.range > params.max_range_points:
        return False, f"Range too large ({bar.range:.2f} > {params.max_range_points})"

    # Body ratio check
    if bar.body_ratio < params.min_body_ratio:
        return False, f"Body ratio too small ({bar.body_ratio:.2%} < {params.min_body_ratio:.2%})"

    # Close position check (must close near low)
    if bar.close_position > params.max_close_position_bear:
        return False, f"Close not low enough ({bar.close_position:.2%} > {params.max_close_position_bear:.2%})"

    return True, None


def detect_conviction(bar: Bar, params: ConvictionParams) -> ConvictionResult:
    """
    Detect if a bar is a conviction bar.

    Checks for both bullish and bearish conviction.
    A bar can only be one type of conviction (not both).

    Args:
        bar: Bar to analyze
        params: Conviction parameters

    Returns:
        ConvictionResult with analysis details
    """
    metrics = {
        'range': bar.range,
        'body': bar.body,
        'body_ratio': bar.body_ratio,
        'close_position': bar.close_position,
        'is_bullish': bar.is_bullish,
        'is_bearish': bar.is_bearish,
    }

    # Check bullish conviction
    is_bull, bull_reason = is_bullish_conviction(bar, params)
    if is_bull:
        return ConvictionResult(
            bar=bar,
            conviction_type=ConvictionType.BULLISH,
            is_valid=True,
            skip_reason=None,
            metrics=metrics,
        )

    # Check bearish conviction
    is_bear, bear_reason = is_bearish_conviction(bar, params)
    if is_bear:
        return ConvictionResult(
            bar=bar,
            conviction_type=ConvictionType.BEARISH,
            is_valid=True,
            skip_reason=None,
            metrics=metrics,
        )

    # Not a conviction bar
    reason = bull_reason if bar.is_bullish else bear_reason if bar.is_bearish else "Doji bar"
    return ConvictionResult(
        bar=bar,
        conviction_type=ConvictionType.NONE,
        is_valid=False,
        skip_reason=reason,
        metrics=metrics,
    )


def get_conviction_strength(result: ConvictionResult) -> str:
    """
    Get human-readable strength description.

    Args:
        result: ConvictionResult to describe

    Returns:
        Strength description string
    """
    if not result.is_valid:
        return "NO_CONVICTION"

    body_ratio = result.metrics['body_ratio']
    close_position = result.metrics['close_position']

    if result.conviction_type == ConvictionType.BULLISH:
        if body_ratio >= 0.70 and close_position >= 0.85:
            return "VERY_STRONG_BULL"
        elif body_ratio >= 0.60 and close_position >= 0.80:
            return "STRONG_BULL"
        else:
            return "MODERATE_BULL"
    else:
        if body_ratio >= 0.70 and close_position <= 0.15:
            return "VERY_STRONG_BEAR"
        elif body_ratio >= 0.60 and close_position <= 0.20:
            return "STRONG_BEAR"
        else:
            return "MODERATE_BEAR"

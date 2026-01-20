"""
Follow-through validation logic.
Validates that conviction bars have proper follow-through confirmation.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from .bar import Bar
from .conviction import ConvictionType
from ..utils.config import FollowthroughParams


class FollowthroughType(Enum):
    """Classification of follow-through bar behavior."""
    STRONG_CONFIRMATION = "STRONG_CONFIRMATION"
    MODERATE_CONFIRMATION = "MODERATE_CONFIRMATION"
    NEUTRAL = "NEUTRAL"
    DENIAL = "DENIAL"
    TRAP = "TRAP"


@dataclass
class FollowthroughResult:
    """
    Result of follow-through analysis.

    Attributes:
        signal_bar: The original conviction/signal bar
        ft_bar: The follow-through bar
        ft_type: Type of follow-through detected
        is_confirmed: Whether follow-through confirms the signal
        description: Human-readable description
    """
    signal_bar: Bar
    ft_bar: Bar
    ft_type: FollowthroughType
    is_confirmed: bool
    description: str


def validate_bullish_followthrough(
    signal_bar: Bar,
    ft_bar: Bar,
    params: FollowthroughParams
) -> FollowthroughResult:
    """
    Validate follow-through for a bullish conviction bar.

    Rules:
    - TRAP: FT bar low goes below signal bar low - buffer (trap)
    - DENIAL: FT bar closes below signal bar midpoint
    - STRONG_CONFIRMATION: FT bar closes above signal bar high
    - MODERATE_CONFIRMATION: FT bar closes at or above signal bar midpoint
    - NEUTRAL: Otherwise

    Args:
        signal_bar: The conviction bar
        ft_bar: The follow-through bar
        params: Follow-through parameters

    Returns:
        FollowthroughResult with analysis
    """
    signal_midpoint = signal_bar.midpoint

    # Check for trap (FT bar takes out signal bar's low)
    trap_level = signal_bar.low - params.trap_buffer
    if ft_bar.low < trap_level:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.TRAP,
            is_confirmed=False,
            description=f"TRAP: FT low {ft_bar.low:.2f} broke below signal low {signal_bar.low:.2f} - {params.trap_buffer:.2f}",
        )

    # Check for denial (FT bar closes below midpoint)
    if ft_bar.close < signal_midpoint:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.DENIAL,
            is_confirmed=False,
            description=f"DENIAL: FT close {ft_bar.close:.2f} below midpoint {signal_midpoint:.2f}",
        )

    # Check for strong confirmation (FT bar closes above signal high)
    if ft_bar.close > signal_bar.high:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.STRONG_CONFIRMATION,
            is_confirmed=True,
            description=f"STRONG: FT close {ft_bar.close:.2f} above signal high {signal_bar.high:.2f}",
        )

    # Check for moderate confirmation (FT bar closes at/above midpoint)
    if ft_bar.close >= signal_midpoint:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.MODERATE_CONFIRMATION,
            is_confirmed=True,
            description=f"MODERATE: FT close {ft_bar.close:.2f} above midpoint {signal_midpoint:.2f}",
        )

    # Neutral (shouldn't reach here given above conditions)
    return FollowthroughResult(
        signal_bar=signal_bar,
        ft_bar=ft_bar,
        ft_type=FollowthroughType.NEUTRAL,
        is_confirmed=False,
        description="NEUTRAL: No clear direction",
    )


def validate_bearish_followthrough(
    signal_bar: Bar,
    ft_bar: Bar,
    params: FollowthroughParams
) -> FollowthroughResult:
    """
    Validate follow-through for a bearish conviction bar.

    Rules:
    - TRAP: FT bar high goes above signal bar high + buffer (trap)
    - DENIAL: FT bar closes above signal bar midpoint
    - STRONG_CONFIRMATION: FT bar closes below signal bar low
    - MODERATE_CONFIRMATION: FT bar closes at or below signal bar midpoint
    - NEUTRAL: Otherwise

    Args:
        signal_bar: The conviction bar
        ft_bar: The follow-through bar
        params: Follow-through parameters

    Returns:
        FollowthroughResult with analysis
    """
    signal_midpoint = signal_bar.midpoint

    # Check for trap (FT bar takes out signal bar's high)
    trap_level = signal_bar.high + params.trap_buffer
    if ft_bar.high > trap_level:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.TRAP,
            is_confirmed=False,
            description=f"TRAP: FT high {ft_bar.high:.2f} broke above signal high {signal_bar.high:.2f} + {params.trap_buffer:.2f}",
        )

    # Check for denial (FT bar closes above midpoint)
    if ft_bar.close > signal_midpoint:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.DENIAL,
            is_confirmed=False,
            description=f"DENIAL: FT close {ft_bar.close:.2f} above midpoint {signal_midpoint:.2f}",
        )

    # Check for strong confirmation (FT bar closes below signal low)
    if ft_bar.close < signal_bar.low:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.STRONG_CONFIRMATION,
            is_confirmed=True,
            description=f"STRONG: FT close {ft_bar.close:.2f} below signal low {signal_bar.low:.2f}",
        )

    # Check for moderate confirmation (FT bar closes at/below midpoint)
    if ft_bar.close <= signal_midpoint:
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.MODERATE_CONFIRMATION,
            is_confirmed=True,
            description=f"MODERATE: FT close {ft_bar.close:.2f} below midpoint {signal_midpoint:.2f}",
        )

    # Neutral
    return FollowthroughResult(
        signal_bar=signal_bar,
        ft_bar=ft_bar,
        ft_type=FollowthroughType.NEUTRAL,
        is_confirmed=False,
        description="NEUTRAL: No clear direction",
    )


def validate_followthrough(
    signal_bar: Bar,
    ft_bar: Bar,
    conviction_type: ConvictionType,
    params: FollowthroughParams
) -> FollowthroughResult:
    """
    Validate follow-through based on conviction type.

    Args:
        signal_bar: The conviction bar
        ft_bar: The follow-through bar
        conviction_type: Type of conviction (BULLISH or BEARISH)
        params: Follow-through parameters

    Returns:
        FollowthroughResult with analysis
    """
    if conviction_type == ConvictionType.BULLISH:
        return validate_bullish_followthrough(signal_bar, ft_bar, params)
    elif conviction_type == ConvictionType.BEARISH:
        return validate_bearish_followthrough(signal_bar, ft_bar, params)
    else:
        # Should not happen, but handle gracefully
        return FollowthroughResult(
            signal_bar=signal_bar,
            ft_bar=ft_bar,
            ft_type=FollowthroughType.NEUTRAL,
            is_confirmed=False,
            description="No conviction to validate",
        )


def meets_minimum_confirmation(
    ft_result: FollowthroughResult,
    min_level: str
) -> bool:
    """
    Check if follow-through meets minimum confirmation level.

    Args:
        ft_result: Follow-through result to check
        min_level: Minimum required level ("MODERATE" or "STRONG")

    Returns:
        True if meets minimum level
    """
    if min_level == "STRONG":
        return ft_result.ft_type == FollowthroughType.STRONG_CONFIRMATION
    elif min_level == "MODERATE":
        return ft_result.ft_type in [
            FollowthroughType.STRONG_CONFIRMATION,
            FollowthroughType.MODERATE_CONFIRMATION,
        ]
    else:
        return ft_result.is_confirmed

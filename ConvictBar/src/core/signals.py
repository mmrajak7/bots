"""
Signal generation and trade level calculation.
Handles entry, stop-loss, target, and trailing stop calculations.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Tuple

from .bar import Bar
from .conviction import ConvictionType
from ..utils.config import (
    EntryParams,
    StopLossParams,
    TargetParams,
    TrailingParams,
)


class Direction(Enum):
    """Trade direction."""
    LONG = "LONG"
    SHORT = "SHORT"


class SignalType(Enum):
    """Type of signal generated."""
    CONVICTION_DETECTED = "CONVICTION_DETECTED"
    FOLLOWTHROUGH_CONFIRMED = "FOLLOWTHROUGH_CONFIRMED"
    ENTRY_TRIGGERED = "ENTRY_TRIGGERED"
    ENTRY_FILLED = "ENTRY_FILLED"
    SL_HIT = "SL_HIT"
    TARGET_HIT = "TARGET_HIT"
    FT_FAILURE_EXIT = "FT_FAILURE_EXIT"
    TIME_EXIT = "TIME_EXIT"
    ORDER_EXPIRED = "ORDER_EXPIRED"
    TRADE_SKIPPED = "TRADE_SKIPPED"
    TRAP_DETECTED = "TRAP_DETECTED"


@dataclass
class TradeLevels:
    """
    Calculated trade levels for a potential trade.

    Attributes:
        direction: Trade direction (LONG or SHORT)
        entry_price: Price to enter the trade
        stop_loss: Initial stop loss price
        target: Target price
        sl_distance: Points between entry and SL
        target_distance: Points between entry and target
        risk_reward: Risk-reward ratio
        sl_capped: Whether SL was capped at max
    """
    direction: Direction
    entry_price: float
    stop_loss: float
    target: float
    sl_distance: float
    target_distance: float
    risk_reward: float
    sl_capped: bool = False


@dataclass
class PendingOrder:
    """
    A pending entry order waiting to be filled.

    Attributes:
        direction: Trade direction
        entry_price: Entry trigger price
        stop_loss: Initial stop loss
        target: Target price
        signal_bar: The conviction bar that generated this signal
        signal_bar_number: Bar number of signal bar
        ft_bar: The follow-through bar
        created_at_bar: Bar number when order was created
        valid_until_bar: Last bar number the order is valid for
        is_active: Whether the order is still active
    """
    direction: Direction
    entry_price: float
    stop_loss: float
    target: float
    signal_bar: Bar
    signal_bar_number: int
    ft_bar: Bar
    created_at_bar: int
    valid_until_bar: int
    is_active: bool = True

    def is_valid_at_bar(self, bar_number: int) -> bool:
        """Check if order is still valid at given bar number."""
        return self.is_active and bar_number <= self.valid_until_bar

    def cancel(self) -> None:
        """Cancel the pending order."""
        self.is_active = False


@dataclass
class Position:
    """
    An active trade position.

    Attributes:
        direction: Trade direction
        entry_price: Actual entry price (with slippage)
        entry_time: Time of entry
        entry_bar_number: Bar number of entry
        initial_stop_loss: Initial SL (before any trailing)
        current_stop_loss: Current SL (may be trailed)
        target: Target price
        signal_bar: The conviction bar
        lots: Number of lots
        sl_moved_to_breakeven: Whether SL has been moved to BE
    """
    direction: Direction
    entry_price: float
    entry_time: datetime
    entry_bar_number: int
    initial_stop_loss: float
    current_stop_loss: float
    target: float
    signal_bar: Bar
    lots: int = 1
    sl_moved_to_breakeven: bool = False

    @property
    def risk_points(self) -> float:
        """Initial risk in points."""
        return abs(self.entry_price - self.initial_stop_loss)

    def current_profit(self, current_price: float) -> float:
        """Current unrealized profit in points."""
        if self.direction == Direction.LONG:
            return current_price - self.entry_price
        else:
            return self.entry_price - current_price

    def profit_in_r(self, current_price: float) -> float:
        """Current profit expressed as multiple of risk (R)."""
        if self.risk_points == 0:
            return 0.0
        return self.current_profit(current_price) / self.risk_points


def calculate_entry_price(
    signal_bar: Bar,
    direction: Direction,
    params: EntryParams
) -> float:
    """
    Calculate entry trigger price.

    Entry is at break of signal bar's extreme + buffer.

    Args:
        signal_bar: The conviction bar
        direction: Trade direction
        params: Entry parameters

    Returns:
        Entry trigger price
    """
    if direction == Direction.LONG:
        return signal_bar.high + params.entry_buffer
    else:
        return signal_bar.low - params.entry_buffer


def calculate_stop_loss(
    signal_bar: Bar,
    entry_price: float,
    direction: Direction,
    params: StopLossParams
) -> Tuple[float, float, bool]:
    """
    Calculate stop loss price.

    SL is placed beyond signal bar's opposite extreme.
    SL is capped at max_sl_points if too wide.

    Args:
        signal_bar: The conviction bar
        entry_price: Entry price
        direction: Trade direction
        params: Stop loss parameters

    Returns:
        Tuple of (stop_loss_price, sl_distance, was_capped)
    """
    if direction == Direction.LONG:
        raw_sl = signal_bar.low - params.sl_buffer
        sl_distance = entry_price - raw_sl
    else:
        raw_sl = signal_bar.high + params.sl_buffer
        sl_distance = raw_sl - entry_price

    # Check if SL needs to be capped
    was_capped = False
    if sl_distance > params.max_sl_points:
        was_capped = True
        if direction == Direction.LONG:
            raw_sl = entry_price - params.max_sl_points
        else:
            raw_sl = entry_price + params.max_sl_points
        sl_distance = params.max_sl_points

    # Apply minimum SL
    if sl_distance < params.min_sl_points:
        if direction == Direction.LONG:
            raw_sl = entry_price - params.min_sl_points
        else:
            raw_sl = entry_price + params.min_sl_points
        sl_distance = params.min_sl_points

    return raw_sl, sl_distance, was_capped


def calculate_target(
    signal_bar: Bar,
    entry_price: float,
    direction: Direction,
    params: TargetParams
) -> Tuple[float, float]:
    """
    Calculate target price.

    Target is signal bar range * multiplier, with minimum applied.

    Args:
        signal_bar: The conviction bar
        entry_price: Entry price
        direction: Trade direction
        params: Target parameters

    Returns:
        Tuple of (target_price, target_distance)
    """
    projected_move = signal_bar.range * params.target_multiplier
    target_distance = max(projected_move, params.min_target_points)

    if direction == Direction.LONG:
        target = entry_price + target_distance
    else:
        target = entry_price - target_distance

    return target, target_distance


def calculate_trade_levels(
    signal_bar: Bar,
    conviction_type: ConvictionType,
    entry_params: EntryParams,
    sl_params: StopLossParams,
    target_params: TargetParams
) -> Optional[TradeLevels]:
    """
    Calculate all trade levels for a signal.

    Args:
        signal_bar: The conviction bar
        conviction_type: Type of conviction
        entry_params: Entry parameters
        sl_params: Stop loss parameters
        target_params: Target parameters

    Returns:
        TradeLevels object or None if trade should be skipped
    """
    direction = Direction.LONG if conviction_type == ConvictionType.BULLISH else Direction.SHORT

    # Calculate entry
    entry = calculate_entry_price(signal_bar, direction, entry_params)

    # Calculate stop loss
    sl, sl_distance, sl_capped = calculate_stop_loss(signal_bar, entry, direction, sl_params)

    # Skip if SL is too wide and skip flag is set
    if sl_distance > sl_params.max_sl_points and sl_params.skip_if_sl_exceeds_max:
        return None

    # Calculate target
    target, target_distance = calculate_target(signal_bar, entry, direction, target_params)

    # Calculate risk-reward
    rr = target_distance / sl_distance if sl_distance > 0 else 0

    return TradeLevels(
        direction=direction,
        entry_price=entry,
        stop_loss=sl,
        target=target,
        sl_distance=sl_distance,
        target_distance=target_distance,
        risk_reward=rr,
        sl_capped=sl_capped,
    )


def update_trailing_stop(
    position: Position,
    current_price: float,
    params: TrailingParams
) -> float:
    """
    Update trailing stop based on current price.

    Rules:
    - Move to breakeven at breakeven_at_rr (default 1R)
    - Start trailing at trail_at_rr (default 2R)

    Args:
        position: Current position
        current_price: Current market price
        params: Trailing stop parameters

    Returns:
        New stop loss price (may be unchanged)
    """
    if not params.enable_trailing:
        return position.current_stop_loss

    profit_r = position.profit_in_r(current_price)
    current_sl = position.current_stop_loss

    if position.direction == Direction.LONG:
        # Move to breakeven at 1R
        if profit_r >= params.breakeven_at_rr and not position.sl_moved_to_breakeven:
            new_sl = position.entry_price + params.breakeven_buffer
            if new_sl > current_sl:
                position.sl_moved_to_breakeven = True
                return new_sl

        # Trail at 2R - trail by 1R below current price
        if profit_r >= params.trail_at_rr:
            trail_sl = current_price - position.risk_points
            if trail_sl > current_sl:
                return trail_sl

    else:  # SHORT
        # Move to breakeven at 1R
        if profit_r >= params.breakeven_at_rr and not position.sl_moved_to_breakeven:
            new_sl = position.entry_price - params.breakeven_buffer
            if new_sl < current_sl:
                position.sl_moved_to_breakeven = True
                return new_sl

        # Trail at 2R
        if profit_r >= params.trail_at_rr:
            trail_sl = current_price + position.risk_points
            if trail_sl < current_sl:
                return trail_sl

    return current_sl


def check_ft_failure_exit(
    position: Position,
    current_bar: Bar,
) -> Tuple[bool, Optional[str]]:
    """
    Check if position should exit due to follow-through failure.

    Exit if current bar closes against position beyond signal bar midpoint.
    Only checked on first bar after entry (FROZEN decision).

    Args:
        position: Current position
        current_bar: Current bar

    Returns:
        Tuple of (should_exit, reason_if_exit)
    """
    signal_midpoint = position.signal_bar.midpoint

    if position.direction == Direction.LONG:
        if current_bar.close < signal_midpoint:
            return True, f"FT failure: close {current_bar.close:.2f} below midpoint {signal_midpoint:.2f}"
    else:
        if current_bar.close > signal_midpoint:
            return True, f"FT failure: close {current_bar.close:.2f} above midpoint {signal_midpoint:.2f}"

    return False, None

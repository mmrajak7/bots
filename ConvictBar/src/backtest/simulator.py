"""
Trade simulator for backtesting.
Handles order fills, position exits, and P&L calculation.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, List

from ..core.bar import Bar
from ..core.signals import Direction, PendingOrder, Position
from ..utils.config import SimulationParams


class ExitReason(Enum):
    """Reason for trade exit."""
    STOP_LOSS = "STOP_LOSS"
    TARGET = "TARGET"
    FT_FAILURE = "FT_FAILURE"
    TIME_EXIT = "TIME_EXIT"
    MANUAL = "MANUAL"


@dataclass
class TradeResult:
    """
    Complete result of a closed trade.

    Attributes:
        trade_id: Unique trade identifier
        date: Trading date
        direction: Trade direction
        signal_bar_time: Time of signal bar
        signal_bar_number: Bar number of signal bar
        entry_time: Time of entry
        entry_bar_number: Bar number at entry
        exit_time: Time of exit
        exit_bar_number: Bar number at exit
        entry_price: Actual entry price (with slippage)
        exit_price: Actual exit price (with slippage)
        stop_loss: Initial stop loss
        target: Target price
        exit_reason: Why the trade was closed
        pnl_points: P&L in points
        pnl_amount: P&L in currency (after commissions)
        bars_held: Number of bars position was held
        lots: Number of lots traded
        signal_bar_range: Range of signal bar
        signal_bar_body_ratio: Body ratio of signal bar
        signal_bar_close_position: Close position of signal bar
    """
    trade_id: int
    date: datetime
    direction: Direction
    signal_bar_time: datetime
    signal_bar_number: int
    entry_time: datetime
    entry_bar_number: int
    exit_time: datetime
    exit_bar_number: int
    entry_price: float
    exit_price: float
    stop_loss: float
    target: float
    exit_reason: ExitReason
    pnl_points: float
    pnl_amount: float
    bars_held: int
    lots: int = 1
    signal_bar_range: float = 0.0
    signal_bar_body_ratio: float = 0.0
    signal_bar_close_position: float = 0.0

    def to_dict(self) -> dict:
        """Convert to dictionary for CSV/JSON export."""
        return {
            'trade_id': self.trade_id,
            'date': self.date.strftime('%Y-%m-%d'),
            'direction': self.direction.value,
            'signal_bar_time': self.signal_bar_time.strftime('%H:%M'),
            'signal_bar_number': self.signal_bar_number,
            'entry_time': self.entry_time.strftime('%H:%M'),
            'entry_bar_number': self.entry_bar_number,
            'exit_time': self.exit_time.strftime('%H:%M'),
            'exit_bar_number': self.exit_bar_number,
            'entry_price': round(self.entry_price, 2),
            'exit_price': round(self.exit_price, 2),
            'stop_loss': round(self.stop_loss, 2),
            'target': round(self.target, 2),
            'exit_reason': self.exit_reason.value,
            'pnl_points': round(self.pnl_points, 2),
            'pnl_amount': round(self.pnl_amount, 2),
            'bars_held': self.bars_held,
            'lots': self.lots,
            'signal_bar_range': round(self.signal_bar_range, 2),
            'signal_bar_body_ratio': round(self.signal_bar_body_ratio, 4),
            'signal_bar_close_position': round(self.signal_bar_close_position, 4),
        }


class TradeSimulator:
    """
    Simulates trade execution with slippage and commission.
    """

    def __init__(self, params: SimulationParams, lot_size: int = 75):
        """
        Initialize simulator.

        Args:
            params: Simulation parameters
            lot_size: Contract lot size (for P&L calculation)
        """
        self.params = params
        self.lot_size = lot_size
        self.trade_counter = 0

    def check_entry_fill(
        self,
        order: PendingOrder,
        bar: Bar
    ) -> Optional[Position]:
        """
        Check if pending order would be filled on this bar.

        For LONG: Entry triggered if bar.high >= entry_price
        For SHORT: Entry triggered if bar.low <= entry_price

        Args:
            order: Pending order to check
            bar: Current bar

        Returns:
            Position if filled, None otherwise
        """
        if not order.is_active:
            return None

        filled = False

        if order.direction == Direction.LONG:
            if bar.high >= order.entry_price:
                filled = True
                # Apply slippage (buy higher)
                fill_price = order.entry_price + self.params.slippage_points
        else:  # SHORT
            if bar.low <= order.entry_price:
                filled = True
                # Apply slippage (sell lower)
                fill_price = order.entry_price - self.params.slippage_points

        if filled:
            order.cancel()  # Order is now filled
            return Position(
                direction=order.direction,
                entry_price=fill_price,
                entry_time=bar.datetime,
                entry_bar_number=bar.bar_number,
                initial_stop_loss=order.stop_loss,
                current_stop_loss=order.stop_loss,
                target=order.target,
                signal_bar=order.signal_bar,
                lots=1,  # Fixed lots for now
            )

        return None

    def check_exit(
        self,
        position: Position,
        bar: Bar,
        is_hard_close: bool = False
    ) -> Optional[TradeResult]:
        """
        Check if position should be closed on this bar.

        Exit priority (if both SL and target hit on same bar):
        - FROZEN: Assume SL hit first (pessimistic)

        Args:
            position: Current position
            bar: Current bar
            is_hard_close: Force close due to end of day

        Returns:
            TradeResult if closed, None if still open
        """
        # Hard close (end of day)
        if is_hard_close:
            exit_price = bar.close
            if position.direction == Direction.LONG:
                exit_price -= self.params.slippage_points
            else:
                exit_price += self.params.slippage_points
            return self._create_result(position, exit_price, bar, ExitReason.TIME_EXIT)

        # Check SL first (pessimistic assumption per FROZEN decision)
        if position.direction == Direction.LONG:
            # SL hit if bar.low <= stop_loss
            if bar.low <= position.current_stop_loss:
                exit_price = position.current_stop_loss - self.params.slippage_points
                return self._create_result(position, exit_price, bar, ExitReason.STOP_LOSS)

            # Target hit if bar.high >= target
            if bar.high >= position.target:
                # Conservative: use target minus slippage
                exit_price = position.target - self.params.slippage_points
                return self._create_result(position, exit_price, bar, ExitReason.TARGET)

        else:  # SHORT
            # SL hit if bar.high >= stop_loss
            if bar.high >= position.current_stop_loss:
                exit_price = position.current_stop_loss + self.params.slippage_points
                return self._create_result(position, exit_price, bar, ExitReason.STOP_LOSS)

            # Target hit if bar.low <= target
            if bar.low <= position.target:
                exit_price = position.target + self.params.slippage_points
                return self._create_result(position, exit_price, bar, ExitReason.TARGET)

        return None

    def close_position(
        self,
        position: Position,
        bar: Bar,
        exit_reason: ExitReason,
        exit_price: Optional[float] = None
    ) -> TradeResult:
        """
        Close a position manually (e.g., FT failure exit).

        Args:
            position: Position to close
            bar: Current bar
            exit_reason: Reason for exit
            exit_price: Optional specific exit price (defaults to bar.close)

        Returns:
            TradeResult for the closed trade
        """
        if exit_price is None:
            exit_price = bar.close

        # Apply slippage
        if position.direction == Direction.LONG:
            exit_price -= self.params.slippage_points
        else:
            exit_price += self.params.slippage_points

        return self._create_result(position, exit_price, bar, exit_reason)

    def _create_result(
        self,
        position: Position,
        exit_price: float,
        exit_bar: Bar,
        exit_reason: ExitReason
    ) -> TradeResult:
        """
        Create trade result from closed position.

        Args:
            position: Closed position
            exit_price: Exit price
            exit_bar: Bar at exit
            exit_reason: Reason for exit

        Returns:
            TradeResult object
        """
        self.trade_counter += 1

        # Calculate P&L
        if position.direction == Direction.LONG:
            pnl_points = exit_price - position.entry_price
        else:
            pnl_points = position.entry_price - exit_price

        # P&L in currency (after commission)
        # Commission charged on both entry and exit
        total_commission = self.params.commission_per_lot * position.lots * 2
        pnl_amount = (pnl_points * self.lot_size * position.lots) - total_commission

        bars_held = exit_bar.bar_number - position.entry_bar_number

        return TradeResult(
            trade_id=self.trade_counter,
            date=position.entry_time,
            direction=position.direction,
            signal_bar_time=position.signal_bar.datetime,
            signal_bar_number=position.signal_bar.bar_number,
            entry_time=position.entry_time,
            entry_bar_number=position.entry_bar_number,
            exit_time=exit_bar.datetime,
            exit_bar_number=exit_bar.bar_number,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop_loss=position.initial_stop_loss,
            target=position.target,
            exit_reason=exit_reason,
            pnl_points=pnl_points,
            pnl_amount=pnl_amount,
            bars_held=bars_held,
            lots=position.lots,
            signal_bar_range=position.signal_bar.range,
            signal_bar_body_ratio=position.signal_bar.body_ratio,
            signal_bar_close_position=position.signal_bar.close_position,
        )


@dataclass
class DailyRiskManager:
    """
    Manages daily risk limits.

    Tracks:
    - Number of trades taken
    - Daily P&L
    - Consecutive losses
    """
    max_trades_per_day: int
    max_daily_loss_points: float
    max_consecutive_losses: int
    trades_today: int = 0
    pnl_today: float = 0.0
    consecutive_losses: int = 0

    def can_trade(self) -> tuple:
        """
        Check if trading is allowed.

        Returns:
            Tuple of (can_trade: bool, reason: str or None)
        """
        if self.trades_today >= self.max_trades_per_day:
            return False, f"Daily trade limit reached ({self.max_trades_per_day})"

        if self.pnl_today <= -self.max_daily_loss_points:
            return False, f"Daily loss limit reached ({self.pnl_today:.2f} points)"

        if self.consecutive_losses >= self.max_consecutive_losses:
            return False, f"Consecutive loss limit reached ({self.consecutive_losses})"

        return True, None

    def record_trade(self, result: TradeResult) -> None:
        """
        Record a completed trade.

        Args:
            result: TradeResult of completed trade
        """
        self.trades_today += 1
        self.pnl_today += result.pnl_points

        if result.pnl_points < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    def reset(self) -> None:
        """Reset daily counters (call at start of each day)."""
        self.trades_today = 0
        self.pnl_today = 0.0
        self.consecutive_losses = 0  # Reset daily to allow fresh start

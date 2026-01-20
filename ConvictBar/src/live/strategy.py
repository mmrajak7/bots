"""
Live strategy engine for ConvictBar forward test.
Processes real-time bars and generates paper trading signals.
"""

import logging
import json
from datetime import datetime, date, time
from pathlib import Path
from threading import Lock
from typing import Optional, List, Dict
from dataclasses import dataclass, field

from ..core.bar import Bar, DayData
from ..core.conviction import detect_conviction, ConvictionType
from ..core.followthrough import validate_followthrough, meets_minimum_confirmation
from ..core.signals import (
    Direction,
    PendingOrder,
    Position,
    calculate_trade_levels,
    update_trailing_stop,
    check_ft_failure_exit,
)
from ..utils.config import StrategyConfig, load_config
from ..utils.time_utils import is_entry_window
from ..backtest.simulator import TradeResult, ExitReason
from .alerts import TelegramAlerts


logger = logging.getLogger(__name__)

# Paths for state persistence
SCRIPT_DIR = Path(__file__).resolve().parent
CONVICTBAR_DIR = SCRIPT_DIR.parent.parent
STATE_FILE = CONVICTBAR_DIR / 'state' / 'live_state.json'


@dataclass
class LiveState:
    """Current state of the live strategy."""
    date: Optional[date] = None
    signal_bar: Optional[Bar] = None
    conviction_type: Optional[ConvictionType] = None
    awaiting_followthrough: bool = False
    pending_order: Optional[PendingOrder] = None
    position: Optional[Position] = None
    bars_since_entry: int = 0

    # Day tracking
    day_bars: List[Bar] = field(default_factory=list)
    day_trades: List[TradeResult] = field(default_factory=list)
    signals_detected: int = 0
    orders_expired: int = 0
    trades_filled: int = 0
    daily_pnl: float = 0.0

    # Session tracking
    prev_close: Optional[float] = None
    gap: Optional[float] = None
    bar1_checked: bool = False
    day_skipped: bool = False
    skip_reason: str = ""

    def reset_signal(self) -> None:
        """Reset signal detection state."""
        self.signal_bar = None
        self.conviction_type = None
        self.awaiting_followthrough = False

    def reset_order(self) -> None:
        """Reset pending order."""
        self.pending_order = None

    def reset_day(self, new_date: date) -> None:
        """Reset state for new trading day."""
        self.date = new_date
        self.signal_bar = None
        self.conviction_type = None
        self.awaiting_followthrough = False
        self.pending_order = None
        self.position = None
        self.bars_since_entry = 0
        self.day_bars = []
        self.day_trades = []
        self.signals_detected = 0
        self.orders_expired = 0
        self.trades_filled = 0
        self.daily_pnl = 0.0
        self.bar1_checked = False
        self.day_skipped = False
        self.skip_reason = ""


class LiveStrategy:
    """
    Live strategy engine for forward testing.

    Receives completed bars from ticker, applies strategy logic,
    and sends alerts via Telegram. Paper trading only.
    """

    def __init__(
        self,
        config: Optional[StrategyConfig] = None,
        alerts: Optional[TelegramAlerts] = None,
        persist_state: bool = True,
    ):
        """
        Initialize live strategy.

        Args:
            config: Strategy configuration
            alerts: Telegram alerts instance
            persist_state: Whether to persist state to disk
        """
        self.config = config or load_config()
        self.alerts = alerts or TelegramAlerts()
        self.persist_state = persist_state

        self.state = LiveState()
        self._lock = Lock()

        # Risk tracking
        self._consecutive_losses = 0

        # Create state directory
        if self.persist_state:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def _check_gap(self, first_bar: Bar) -> tuple:
        """
        Check opening gap.

        Returns:
            (can_trade, reason)
        """
        if self.state.prev_close is None:
            return True, None

        gap = first_bar.open - self.state.prev_close
        self.state.gap = gap
        abs_gap = abs(gap)

        if abs_gap >= self.config.gap.large_gap_threshold:
            if self.config.gap.skip_if_gap_large:
                return False, f"Large gap ({gap:.2f} points)"

        return True, None

    def _check_bar1_filters(self, bar: Bar) -> tuple:
        """
        Check Bar 1 filters.

        Returns:
            (can_trade, reason)
        """
        if not self.config.bar1_filter.enabled:
            return True, None

        # Exhaustion filter
        if bar.range > self.config.bar1_filter.max_bar1_range:
            return False, f"Bar 1 exhaustion: range {bar.range:.2f} > {self.config.bar1_filter.max_bar1_range}"

        # Indecision filter
        if bar.body_ratio < self.config.bar1_filter.min_bar1_body_ratio:
            return False, f"Bar 1 indecision: body_ratio {bar.body_ratio:.2%} < {self.config.bar1_filter.min_bar1_body_ratio:.2%}"

        return True, None

    def _can_trade(self) -> tuple:
        """
        Check risk limits.

        Returns:
            (can_trade, reason)
        """
        # Max trades per day
        if self.state.trades_filled >= self.config.risk.max_trades_per_day:
            return False, f"Max trades reached ({self.config.risk.max_trades_per_day})"

        # Max daily loss
        if self.state.daily_pnl <= -self.config.risk.max_daily_loss_points:
            return False, f"Daily loss limit ({self.config.risk.max_daily_loss_points} pts)"

        # Consecutive losses
        if self._consecutive_losses >= self.config.risk.max_consecutive_losses:
            return False, f"Consecutive losses ({self.config.risk.max_consecutive_losses})"

        return True, None

    def _record_trade(self, result: TradeResult) -> None:
        """Record trade result and update risk tracking."""
        self.state.day_trades.append(result)
        self.state.daily_pnl += result.pnl_points

        if result.pnl_points > 0:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1

    def _create_trade_result(
        self,
        position: Position,
        exit_price: float,
        exit_bar: Bar,
        exit_reason: ExitReason,
    ) -> TradeResult:
        """Create trade result from position."""
        # Apply slippage
        slippage = self.config.simulation.slippage_points
        if position.direction == Direction.LONG:
            actual_exit = exit_price - slippage
            pnl = actual_exit - position.entry_price
        else:
            actual_exit = exit_price + slippage
            pnl = position.entry_price - actual_exit

        return TradeResult(
            entry_time=position.entry_bar.datetime,
            exit_time=exit_bar.datetime,
            direction=position.direction,
            entry_price=position.entry_price,
            exit_price=actual_exit,
            stop_loss=position.current_stop_loss,
            target=position.target,
            exit_reason=exit_reason,
            pnl_points=pnl,
            bars_held=self.state.bars_since_entry,
        )

    def on_bar_complete(self, bar: Bar) -> None:
        """
        Process a completed bar.

        Called by ticker when a 5-minute bar closes.

        Args:
            bar: Completed bar
        """
        with self._lock:
            try:
                self._process_bar(bar)
            except Exception as e:
                logger.error(f"Error processing bar: {e}", exc_info=True)
                self.alerts.send_error(str(e), f"Bar: {bar.datetime}")

    def _process_bar(self, bar: Bar) -> None:
        """Internal bar processing logic."""
        current_date = bar.datetime.date()

        # Check for new day
        if self.state.date != current_date:
            # Send previous day summary if we have trades
            if self.state.date and (self.state.day_trades or self.state.signals_detected > 0):
                self.alerts.send_daily_summary(
                    datetime.combine(self.state.date, time(15, 30)),
                    self.state.day_trades,
                    self.state.daily_pnl,
                    self.state.signals_detected,
                    self.state.orders_expired,
                )

            # Store previous close for gap calculation
            if self.state.day_bars:
                self.state.prev_close = self.state.day_bars[-1].close

            # Reset for new day
            self.state.reset_day(current_date)
            self._consecutive_losses = 0  # Reset daily to allow fresh start
            logger.info(f"New trading day: {current_date}")

        # Add bar to day's bars
        self.state.day_bars.append(bar)

        # Check Bar 1 filters on first bar
        if not self.state.bar1_checked:
            self.state.bar1_checked = True

            # Check gap
            can_trade, gap_reason = self._check_gap(bar)
            if not can_trade:
                self.state.day_skipped = True
                self.state.skip_reason = gap_reason
                logger.info(f"Day skipped: {gap_reason}")
                self.alerts.send_day_skipped(gap_reason, bar)
                return

            # Check Bar 1 filters
            can_trade, bar1_reason = self._check_bar1_filters(bar)
            if not can_trade:
                self.state.day_skipped = True
                self.state.skip_reason = bar1_reason
                logger.info(f"Day skipped: {bar1_reason}")
                self.alerts.send_day_skipped(bar1_reason, bar)
                return

        # Skip all processing if day is skipped
        if self.state.day_skipped:
            return

        # === 1. Check for position exit ===
        if self.state.position is not None:
            self.state.bars_since_entry += 1

            # Check hard close time
            hard_close_time = time(15, 20)
            is_hard_close = bar.datetime.time() >= hard_close_time

            # Check FT failure exit (first bar after entry only)
            if self.state.bars_since_entry == 1:
                should_exit, exit_reason = check_ft_failure_exit(
                    self.state.position, bar
                )
                if should_exit:
                    result = self._create_trade_result(
                        self.state.position,
                        bar.close,
                        bar,
                        ExitReason.FT_FAILURE,
                    )
                    self._record_trade(result)
                    logger.info(f"FT failure exit: {result.pnl_points:.2f} pts")
                    self.alerts.send_exit(result, bar)
                    self.state.position = None
                    self.state.bars_since_entry = 0
                    self._save_state()
                    return

            # Update trailing stop
            if self.config.trailing.enable_trailing:
                old_sl = self.state.position.current_stop_loss
                new_sl = update_trailing_stop(
                    self.state.position,
                    bar.close,
                    self.config.trailing,
                )
                if new_sl != old_sl:
                    self.state.position.current_stop_loss = new_sl
                    reason = "Breakeven" if abs(new_sl - self.state.position.entry_price) < 3 else "Trailing"
                    logger.info(f"Trailing update: {old_sl:.2f} -> {new_sl:.2f}")
                    self.alerts.send_trailing_update(
                        self.state.position,
                        old_sl,
                        new_sl,
                        bar.close,
                        reason,
                    )

            # Check SL/Target hit
            exit_result = self._check_exit(self.state.position, bar, is_hard_close)
            if exit_result:
                self._record_trade(exit_result)
                logger.info(f"Exit: {exit_result.exit_reason.value}, {exit_result.pnl_points:.2f} pts")
                self.alerts.send_exit(exit_result, bar)
                self.state.position = None
                self.state.bars_since_entry = 0
                self._save_state()
                return

        # === 2. Check for pending order fill ===
        if self.state.pending_order is not None:
            if not self.state.pending_order.is_valid_at_bar(bar.bar_number):
                # Order expired
                logger.info(f"Order expired: entry {self.state.pending_order.entry_price:.2f} not reached")
                self.alerts.send_order_expired(self.state.pending_order, bar)
                self.state.orders_expired += 1
                self.state.reset_order()
            else:
                # Check for fill
                position = self._check_entry_fill(self.state.pending_order, bar)
                if position:
                    self.state.position = position
                    self.state.bars_since_entry = 0
                    self.state.trades_filled += 1
                    logger.info(f"Entry filled at {position.entry_price:.2f}")
                    self.alerts.send_entry_filled(position, bar)
                    self.state.reset_order()
                    self.state.reset_signal()
                    self._save_state()

        # === 3. Check for new signals (only if no position/order) ===
        if self.state.position is None and self.state.pending_order is None:
            if is_entry_window(bar.datetime):
                can_trade, risk_reason = self._can_trade()

                if not can_trade:
                    if self.state.awaiting_followthrough:
                        logger.info(f"Trade skipped: {risk_reason}")
                        self.alerts.send_trade_skipped(risk_reason, bar)
                        self.state.reset_signal()
                else:
                    # Check for follow-through validation
                    if self.state.awaiting_followthrough and self.state.signal_bar:
                        ft_result = validate_followthrough(
                            self.state.signal_bar,
                            bar,
                            self.state.conviction_type,
                            self.config.followthrough,
                        )

                        if ft_result.is_confirmed and meets_minimum_confirmation(
                            ft_result,
                            self.config.followthrough.min_confirmation_level
                        ):
                            # Calculate trade levels
                            levels = calculate_trade_levels(
                                self.state.signal_bar,
                                self.state.conviction_type,
                                self.config.entry,
                                self.config.stop_loss,
                                self.config.target,
                            )

                            if levels is None:
                                logger.info("Trade skipped: SL too wide")
                                self.alerts.send_trade_skipped("SL too wide", bar)
                                self.state.reset_signal()
                            else:
                                # Create pending order
                                self.state.pending_order = PendingOrder(
                                    direction=levels.direction,
                                    entry_price=levels.entry_price,
                                    stop_loss=levels.stop_loss,
                                    target=levels.target,
                                    signal_bar=self.state.signal_bar,
                                    signal_bar_number=self.state.signal_bar.bar_number,
                                    ft_bar=bar,
                                    created_at_bar=bar.bar_number,
                                    valid_until_bar=bar.bar_number + self.config.entry.order_valid_bars,
                                )

                                self.state.signals_detected += 1
                                logger.info(
                                    f"Entry signal: {levels.direction.value} "
                                    f"Entry:{levels.entry_price:.2f} SL:{levels.stop_loss:.2f} TGT:{levels.target:.2f}"
                                )
                                self.alerts.send_entry_signal(
                                    self.state.pending_order,
                                    ft_result.ft_type.value,
                                )
                                self.state.reset_signal()
                                self._save_state()

                        elif ft_result.ft_type.value == "TRAP":
                            logger.info(f"Trap detected: {ft_result.description}")
                            self.state.reset_signal()

                        elif ft_result.ft_type.value in ["DENIAL", "NEUTRAL"]:
                            logger.info(f"FT failed: {ft_result.description}")
                            self.state.reset_signal()

                    # Check for new conviction bar
                    if not self.state.awaiting_followthrough:
                        conviction = detect_conviction(bar, self.config.conviction)

                        if conviction.is_valid:
                            self.state.signal_bar = bar
                            self.state.conviction_type = conviction.conviction_type
                            self.state.awaiting_followthrough = True

                            logger.info(
                                f"Conviction detected: {conviction.conviction_type.value} "
                                f"Range:{bar.range:.2f} BR:{bar.body_ratio:.2%}"
                            )
                            self.alerts.send_conviction_detected(
                                bar,
                                conviction.conviction_type.value,
                                f"Range:{bar.range:.2f} BR:{bar.body_ratio:.2%}",
                            )
                            self._save_state()

    def _check_entry_fill(self, order: PendingOrder, bar: Bar) -> Optional[Position]:
        """Check if pending order gets filled."""
        filled = False

        if order.direction == Direction.LONG:
            # Long entry: price must go above entry
            if bar.high >= order.entry_price:
                filled = True
        else:
            # Short entry: price must go below entry
            if bar.low <= order.entry_price:
                filled = True

        if filled:
            # Apply slippage
            slippage = self.config.simulation.slippage_points
            if order.direction == Direction.LONG:
                fill_price = order.entry_price + slippage
            else:
                fill_price = order.entry_price - slippage

            return Position(
                direction=order.direction,
                entry_price=fill_price,
                entry_bar=bar,
                initial_stop_loss=order.stop_loss,
                current_stop_loss=order.stop_loss,
                target=order.target,
                signal_bar=order.signal_bar,
            )

        return None

    def _check_exit(
        self,
        position: Position,
        bar: Bar,
        is_hard_close: bool,
    ) -> Optional[TradeResult]:
        """Check for position exit."""
        sl = position.current_stop_loss
        tgt = position.target

        # Check SL and target
        sl_hit = False
        tgt_hit = False

        if position.direction == Direction.LONG:
            sl_hit = bar.low <= sl
            tgt_hit = bar.high >= tgt
        else:
            sl_hit = bar.high >= sl
            tgt_hit = bar.low <= tgt

        # Determine exit
        if sl_hit and tgt_hit:
            # Both hit - assume SL first (pessimistic)
            exit_price = sl
            exit_reason = ExitReason.STOP_LOSS
        elif sl_hit:
            exit_price = sl
            exit_reason = ExitReason.STOP_LOSS
        elif tgt_hit:
            exit_price = tgt
            exit_reason = ExitReason.TARGET
        elif is_hard_close:
            exit_price = bar.close
            exit_reason = ExitReason.TIME_EXIT
        else:
            return None

        return self._create_trade_result(position, exit_price, bar, exit_reason)

    def _save_state(self) -> None:
        """Save current state to disk."""
        if not self.persist_state:
            return

        try:
            state_data = {
                'date': self.state.date.isoformat() if self.state.date else None,
                'awaiting_followthrough': self.state.awaiting_followthrough,
                'signals_detected': self.state.signals_detected,
                'orders_expired': self.state.orders_expired,
                'trades_filled': self.state.trades_filled,
                'daily_pnl': self.state.daily_pnl,
                'consecutive_losses': self._consecutive_losses,
                'has_position': self.state.position is not None,
                'has_pending_order': self.state.pending_order is not None,
            }

            with open(STATE_FILE, 'w') as f:
                json.dump(state_data, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_state(self) -> bool:
        """Load state from disk."""
        if not self.persist_state or not STATE_FILE.exists():
            return False

        try:
            with open(STATE_FILE) as f:
                state_data = json.load(f)

            # Only restore if same day
            saved_date = state_data.get('date')
            if saved_date and saved_date == date.today().isoformat():
                self._consecutive_losses = state_data.get('consecutive_losses', 0)
                logger.info(f"Restored state from {STATE_FILE}")
                return True

        except Exception as e:
            logger.error(f"Failed to load state: {e}")

        return False

    def start(self) -> None:
        """Start the strategy."""
        self._load_state()
        logger.info("Live strategy started")

    def stop(self) -> None:
        """Stop the strategy."""
        self._save_state()

        # Send final summary if we have data
        if self.state.date and (self.state.day_trades or self.state.signals_detected > 0):
            self.alerts.send_daily_summary(
                datetime.now(),
                self.state.day_trades,
                self.state.daily_pnl,
                self.state.signals_detected,
                self.state.orders_expired,
            )

        logger.info("Live strategy stopped")

    def get_status(self) -> dict:
        """Get current strategy status."""
        with self._lock:
            return {
                'date': self.state.date.isoformat() if self.state.date else None,
                'bars_today': len(self.state.day_bars),
                'has_position': self.state.position is not None,
                'has_pending_order': self.state.pending_order is not None,
                'awaiting_followthrough': self.state.awaiting_followthrough,
                'signals_detected': self.state.signals_detected,
                'trades_filled': self.state.trades_filled,
                'daily_pnl': self.state.daily_pnl,
                'day_skipped': self.state.day_skipped,
                'skip_reason': self.state.skip_reason,
            }

"""
Backtest runner - main orchestration of the backtesting process.
"""

from dataclasses import dataclass, field
from datetime import datetime, date, time
from typing import List, Optional, Dict
from pathlib import Path

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
from ..utils.time_utils import is_entry_window, is_before_hard_close, get_last_entry_bar
from .simulator import TradeSimulator, TradeResult, DailyRiskManager, ExitReason
from .data_loader import DataLoader


@dataclass
class SignalLog:
    """Log entry for a signal or event."""
    timestamp: datetime
    bar_number: int
    event_type: str
    direction: Optional[str] = None
    details: str = ""


@dataclass
class BacktestState:
    """Current state of the backtest for a single day."""
    signal_bar: Optional[Bar] = None
    conviction_type: Optional[ConvictionType] = None
    awaiting_followthrough: bool = False
    pending_order: Optional[PendingOrder] = None
    position: Optional[Position] = None
    bars_since_entry: int = 0

    def reset_signal(self) -> None:
        """Reset signal detection state."""
        self.signal_bar = None
        self.conviction_type = None
        self.awaiting_followthrough = False

    def reset_order(self) -> None:
        """Reset pending order."""
        self.pending_order = None

    def reset_all(self) -> None:
        """Reset all state for new day."""
        self.signal_bar = None
        self.conviction_type = None
        self.awaiting_followthrough = False
        self.pending_order = None
        self.position = None
        self.bars_since_entry = 0


@dataclass
class BacktestResult:
    """Complete backtest results."""
    trade_results: List[TradeResult] = field(default_factory=list)
    signal_logs: List[SignalLog] = field(default_factory=list)
    days_processed: int = 0
    days_with_signals: int = 0
    days_with_trades: int = 0
    total_signals: int = 0
    orders_expired: int = 0
    trades_skipped: int = 0
    config: Optional[Dict] = None


class BacktestRunner:
    """
    Main backtest orchestrator.

    Processes historical data day by day, detecting signals,
    managing orders and positions, and recording results.
    """

    def __init__(self, config: Optional[StrategyConfig] = None):
        """
        Initialize backtest runner.

        Args:
            config: Strategy configuration (loads default if None)
        """
        self.config = config or load_config()
        self.simulator = TradeSimulator(
            params=self.config.simulation,
            lot_size=self.config.instrument.lot_size,
        )
        self.risk_manager = DailyRiskManager(
            max_trades_per_day=self.config.risk.max_trades_per_day,
            max_daily_loss_points=self.config.risk.max_daily_loss_points,
            max_consecutive_losses=self.config.risk.max_consecutive_losses,
        )
        self.state = BacktestState()
        self.result = BacktestResult()

    def _log_signal(
        self,
        bar: Bar,
        event_type: str,
        direction: Optional[str] = None,
        details: str = ""
    ) -> None:
        """Add entry to signal log."""
        self.result.signal_logs.append(SignalLog(
            timestamp=bar.datetime,
            bar_number=bar.bar_number,
            event_type=event_type,
            direction=direction,
            details=details,
        ))

    def _check_gap(self, day: DayData) -> tuple:
        """
        Check gap and determine if we should trade.

        Args:
            day: Day data with gap information

        Returns:
            Tuple of (can_trade: bool, reason: str or None)
        """
        if day.gap is None:
            return True, None

        abs_gap = abs(day.gap)

        if abs_gap >= self.config.gap.large_gap_threshold:
            if self.config.gap.skip_if_gap_large:
                return False, f"Large gap ({day.gap:.2f} points)"

        return True, None

    def _process_bar(self, bar: Bar, day: DayData) -> Optional[TradeResult]:
        """
        Process a single bar.

        This is the main logic loop for each bar.

        Args:
            bar: Current bar
            day: Current day data

        Returns:
            TradeResult if a trade was closed, None otherwise
        """
        result = None

        # === 1. Check for position exit ===
        if self.state.position is not None:
            self.state.bars_since_entry += 1

            # Check hard close
            hard_close_time = time(15, 20)
            is_hard_close = bar.datetime.time() >= hard_close_time

            # Check FT failure exit (only on first bar after entry - FROZEN)
            if self.state.bars_since_entry == 1:
                should_exit, exit_reason = check_ft_failure_exit(
                    self.state.position, bar
                )
                if should_exit:
                    result = self.simulator.close_position(
                        self.state.position,
                        bar,
                        ExitReason.FT_FAILURE,
                    )
                    self._log_signal(bar, "FT_FAILURE_EXIT", details=exit_reason)
                    self.risk_manager.record_trade(result)
                    self.state.position = None
                    self.state.bars_since_entry = 0
                    return result

            # Update trailing stop (on bar close - FROZEN)
            if self.config.trailing.enable_trailing:
                new_sl = update_trailing_stop(
                    self.state.position,
                    bar.close,
                    self.config.trailing,
                )
                if new_sl != self.state.position.current_stop_loss:
                    self.state.position.current_stop_loss = new_sl
                    self._log_signal(
                        bar, "TRAILING_UPDATE",
                        direction=self.state.position.direction.value,
                        details=f"SL moved to {new_sl:.2f}"
                    )

            # Check SL/Target/Time exit
            result = self.simulator.check_exit(
                self.state.position,
                bar,
                is_hard_close=is_hard_close,
            )

            if result:
                self._log_signal(
                    bar,
                    result.exit_reason.value,
                    direction=self.state.position.direction.value,
                    details=f"Exit at {result.exit_price:.2f}, PnL: {result.pnl_points:.2f}"
                )
                self.risk_manager.record_trade(result)
                self.state.position = None
                self.state.bars_since_entry = 0
                return result

        # === 2. Check for pending order fill ===
        if self.state.pending_order is not None:
            if not self.state.pending_order.is_valid_at_bar(bar.bar_number):
                # Order expired
                self._log_signal(
                    bar, "ORDER_EXPIRED",
                    direction=self.state.pending_order.direction.value,
                    details=f"Entry {self.state.pending_order.entry_price:.2f} not reached"
                )
                self.result.orders_expired += 1
                self.state.reset_order()
            else:
                # Check for fill
                position = self.simulator.check_entry_fill(
                    self.state.pending_order, bar
                )
                if position:
                    self.state.position = position
                    self.state.bars_since_entry = 0
                    self._log_signal(
                        bar, "ENTRY_FILLED",
                        direction=position.direction.value,
                        details=f"Filled at {position.entry_price:.2f}"
                    )
                    self.state.reset_order()
                    self.state.reset_signal()

        # === 3. Check for new signals (only if no position/order) ===
        if self.state.position is None and self.state.pending_order is None:
            # Check if in entry window
            if is_entry_window(bar.datetime):
                # Check risk limits
                can_trade, risk_reason = self.risk_manager.can_trade()

                if not can_trade:
                    if self.state.awaiting_followthrough:
                        self._log_signal(bar, "TRADE_SKIPPED", details=risk_reason)
                        self.result.trades_skipped += 1
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
                                self._log_signal(
                                    bar, "TRADE_SKIPPED",
                                    details="SL too wide"
                                )
                                self.result.trades_skipped += 1
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

                                self._log_signal(
                                    bar, "ENTRY_SIGNAL",
                                    direction=levels.direction.value,
                                    details=f"Entry:{levels.entry_price:.2f} SL:{levels.stop_loss:.2f} TGT:{levels.target:.2f}"
                                )
                                self.result.total_signals += 1
                                self.state.reset_signal()

                        elif ft_result.ft_type.value == "TRAP":
                            self._log_signal(bar, "TRAP_DETECTED", details=ft_result.description)
                            self.state.reset_signal()

                        elif ft_result.ft_type.value in ["DENIAL", "NEUTRAL"]:
                            self._log_signal(bar, "FT_FAILED", details=ft_result.description)
                            self.state.reset_signal()

                    # Check for new conviction bar (only if not already tracking one)
                    if not self.state.awaiting_followthrough:
                        conviction = detect_conviction(bar, self.config.conviction)

                        if conviction.is_valid:
                            self.state.signal_bar = bar
                            self.state.conviction_type = conviction.conviction_type
                            self.state.awaiting_followthrough = True

                            self._log_signal(
                                bar, "CONVICTION_DETECTED",
                                direction=conviction.conviction_type.value,
                                details=f"Range:{bar.range:.2f} BR:{bar.body_ratio:.2%} CP:{bar.close_position:.2%}"
                            )

        return result

    def _process_day(self, day: DayData) -> List[TradeResult]:
        """
        Process a single trading day.

        Args:
            day: Day data to process

        Returns:
            List of trade results for this day
        """
        results = []

        # Reset daily state
        self.state.reset_all()
        self.risk_manager.reset()

        # Check gap
        can_trade, gap_reason = self._check_gap(day)
        if not can_trade:
            self._log_signal(
                day.first_bar, "DAY_SKIPPED",
                details=gap_reason
            )
            return results

        # Check Bar 1 filters
        if self.config.bar1_filter.enabled and day.first_bar:
            # Exhaustion filter: skip if Bar 1 range too large
            if day.first_bar.range > self.config.bar1_filter.max_bar1_range:
                self._log_signal(
                    day.first_bar, "DAY_SKIPPED",
                    details=f"Bar 1 exhaustion: range {day.first_bar.range:.2f} > {self.config.bar1_filter.max_bar1_range}"
                )
                return results

            # Indecision filter: skip if Bar 1 body ratio too small
            if day.first_bar.body_ratio < self.config.bar1_filter.min_bar1_body_ratio:
                self._log_signal(
                    day.first_bar, "DAY_SKIPPED",
                    details=f"Bar 1 indecision: body_ratio {day.first_bar.body_ratio:.2%} < {self.config.bar1_filter.min_bar1_body_ratio:.2%}"
                )
                return results

        day_had_signal = False
        day_had_trade = False

        # Process each bar
        for bar in day.bars:
            result = self._process_bar(bar, day)
            if result:
                results.append(result)
                day_had_trade = True

            if self.state.signal_bar is not None or self.state.pending_order is not None:
                day_had_signal = True

        # Force close any open position at end of day
        if self.state.position is not None and day.last_bar:
            result = self.simulator.close_position(
                self.state.position,
                day.last_bar,
                ExitReason.TIME_EXIT,
            )
            self._log_signal(
                day.last_bar, "HARD_CLOSE",
                direction=self.state.position.direction.value,
                details=f"EOD exit at {result.exit_price:.2f}"
            )
            results.append(result)
            self.risk_manager.record_trade(result)
            day_had_trade = True
            self.state.position = None

        if day_had_signal:
            self.result.days_with_signals += 1
        if day_had_trade:
            self.result.days_with_trades += 1

        return results

    def run(self, days: List[DayData]) -> BacktestResult:
        """
        Run backtest over multiple days.

        Args:
            days: List of DayData objects to process

        Returns:
            BacktestResult with all trades and metrics
        """
        print(f"Starting backtest: {len(days)} days")

        self.result = BacktestResult()
        all_results = []

        for i, day in enumerate(days):
            if i % 20 == 0:
                print(f"Processing day {i+1}/{len(days)}: {day.date.strftime('%Y-%m-%d')}")

            day_results = self._process_day(day)
            all_results.extend(day_results)
            self.result.days_processed += 1

        self.result.trade_results = all_results

        print(f"\nBacktest complete:")
        print(f"  Days processed: {self.result.days_processed}")
        print(f"  Days with signals: {self.result.days_with_signals}")
        print(f"  Days with trades: {self.result.days_with_trades}")
        print(f"  Total trades: {len(all_results)}")
        print(f"  Orders expired: {self.result.orders_expired}")
        print(f"  Trades skipped: {self.result.trades_skipped}")

        return self.result


def run_backtest(
    start_date: date,
    end_date: date,
    config: Optional[StrategyConfig] = None
) -> BacktestResult:
    """
    Convenience function to run a complete backtest.

    Args:
        start_date: Start date
        end_date: End date
        config: Strategy configuration (loads default if None)

    Returns:
        BacktestResult
    """
    if config is None:
        config = load_config()

    # Load data
    loader = DataLoader(use_cache=True)
    days = loader.load_data(
        instrument_token=config.instrument.instrument_token,
        start_date=start_date,
        end_date=end_date,
        timeframe="5minute",
    )

    if not days:
        raise RuntimeError("No data loaded")

    # Run backtest
    runner = BacktestRunner(config)
    result = runner.run(days)
    result.config = {
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'symbol': config.instrument.symbol,
    }

    return result

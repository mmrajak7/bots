#!/usr/bin/env python3
"""
Expiry Day Iron Condor Trading System - Main Entry Point

Scheduled via cron to run every minute from 9:25 AM to 3:20 PM on trading days.

Usage:
    python run.py

Cron example (every minute from 9:25 to 15:20):
    25-59 9 * * 1-5 cd /path/to/expiry && python run.py >> logs/cron.log 2>&1
    0-20 10-15 * * 1-5 cd /path/to/expiry && python run.py >> logs/cron.log 2>&1

@file        run.py
@description Main entry point for expiry day trading system
"""

import sys
import json
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import dataclass

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger


@dataclass
class QuoteWrapper:
    """Wrapper for quote data with bid/ask/ltp."""
    bid: float
    ask: float
    ltp: float

# Configure logging
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logger.remove()
logger.add(
    LOG_DIR / "expiry.log",
    rotation="10 MB",
    retention="30 days",
    level="INFO",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} | {message}"
)
logger.add(sys.stderr, level="INFO")


def load_config() -> Dict[str, Any]:
    """Load configuration from config.json."""
    config_path = Path(__file__).parent / "config.json"

    if not config_path.exists():
        raise FileNotFoundError(f"Config not found: {config_path}")

    with open(config_path, 'r') as f:
        config = json.load(f)

    # Resolve relative paths
    base_dir = Path(__file__).parent

    paths = config.get('paths', {})
    for key, value in paths.items():
        if isinstance(value, str) and not Path(value).is_absolute():
            paths[key] = str((base_dir / value).resolve())

    return config


def create_kite_client(config: Dict[str, Any]) -> Any:
    """
    Create Kite client using shared token.

    Args:
        config: Configuration dictionary

    Returns:
        Kite client wrapper
    """
    # Force IPv4: Kite's IP whitelist holds only the shared home IPv4;
    # over IPv6 order placement is rejected with PermissionException.
    import socket as _socket
    _orig_getaddrinfo = _socket.getaddrinfo
    if getattr(_socket.getaddrinfo, '__name__', '') != '_ipv4_only_getaddrinfo':
        def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)
        _socket.getaddrinfo = _ipv4_only_getaddrinfo

    from kiteconnect import KiteConnect

    token_path = Path(config['paths']['kite_token'])

    if not token_path.exists():
        raise FileNotFoundError(f"Kite token not found: {token_path}")

    with open(token_path, 'r') as f:
        token_data = json.load(f)

    api_key = token_data.get('api_key')
    access_token = token_data.get('access_token')

    if not api_key or not access_token:
        raise ValueError("Invalid token file - missing api_key or access_token")

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    # Verify session
    try:
        profile = kite.profile()
        logger.info(f"Kite authenticated: {profile.get('user_id')}")
    except Exception as e:
        raise ConnectionError(f"Kite authentication failed: {e}")

    return kite


class ExpiryTradingSystem:
    """
    Main trading system orchestrator.

    Handles the complete flow:
    1. Check if today is expiry
    2. Execute entry at 9:27-9:30
    3. Monitor position every minute
    4. Exit on target/SL/time
    """

    def __init__(self, config: Dict[str, Any]):
        """
        Initialize trading system.

        Args:
            config: Configuration dictionary
        """
        self.config = config
        self.mode = config.get('mode', 'paper')

        # Initialize components
        from src.state_manager import StateManager, TradingStatus, Position, LegPosition
        from src.state_manager import save_trade_record
        from src.strategy import ExpiryChecker, StrategyCalculator, calculate_premium_from_quotes
        from src.position_manager import PositionManager, ExitReason
        from src.telegram_alerts import TelegramAlerts
        from src.chart_generator import ChartGenerator, ChartData

        self.state_manager = StateManager(Path(config['paths']['state']))
        self.expiry_checker = ExpiryChecker(
            Path(config['paths']['instruments']),
            config.get('instrument', 'NIFTY')
        )
        self.strategy = StrategyCalculator(config)
        self.position_manager = PositionManager(config)
        self.telegram = TelegramAlerts(config)
        self.chart_generator = ChartGenerator(Path(config['paths']['state']).parent / "charts")

        # Kite client (created on demand)
        self._kite: Any = None
        self._order_executor: Any = None

        # Chart data for P&L tracking
        self._chart_data: Optional[ChartData] = None

        # Store classes for later use
        self._TradingStatus = TradingStatus
        self._Position = Position
        self._LegPosition = LegPosition
        self._ExitReason = ExitReason
        self._ChartData = ChartData
        self._save_trade_record = save_trade_record
        self._calculate_premium_from_quotes = calculate_premium_from_quotes

    @property
    def kite(self) -> Any:
        """Get Kite client (lazy initialization)."""
        if self._kite is None:
            self._kite = create_kite_client(self.config)
        return self._kite

    @property
    def order_executor(self) -> Any:
        """Get order executor (lazy initialization)."""
        if self._order_executor is None:
            from src.order_executor import OrderExecutor
            self._order_executor = OrderExecutor(self.kite, self.config)
        return self._order_executor

    def run(self) -> None:
        """
        Main execution entry point.

        Called every minute by cron.
        """
        logger.info("=" * 60)
        logger.info(f"Expiry Trading System - {datetime.now().strftime('%H:%M:%S')}")
        logger.info("=" * 60)

        try:
            state = self.state_manager.load()

            # Check if already processed today
            if state.status in [self._TradingStatus.EXIT_COMPLETE, self._TradingStatus.SKIPPED]:
                logger.info(f"Today already processed: {state.status.value}")
                return

            if state.status == self._TradingStatus.ERROR:
                logger.warning(f"Previous error state: {state.error_message}")
                # Allow retry after error

            # Check current time
            now = datetime.now()
            current_time = now.time()

            # Before market open - skip
            if current_time < dt_time(9, 25):
                logger.info("Before 9:25 AM - skipping")
                return

            # After hard exit time - should have exited
            hard_exit = dt_time(15, 20)
            if current_time > hard_exit:
                logger.info("After 3:20 PM - skipping")
                if state.status == self._TradingStatus.POSITION_OPEN:
                    logger.warning("Position still open after hours - forcing exit")
                    self._force_exit("AFTER_HOURS")
                return

            # Check if expiry day (only on first run)
            if state.status == self._TradingStatus.IDLE:
                self._check_expiry_day()
                return

            # If not expiry day, skip
            if not state.is_expiry:
                logger.info("Not expiry day - skipping")
                self.state_manager.update_status(self._TradingStatus.SKIPPED)
                return

            # Handle based on current status
            if state.status == self._TradingStatus.WAITING_ENTRY:
                self._handle_entry()
            elif state.status == self._TradingStatus.POSITION_OPEN:
                self._handle_monitoring()
            elif state.status == self._TradingStatus.ENTERING:
                logger.warning("Entry in progress - waiting")
            elif state.status == self._TradingStatus.EXITING:
                logger.warning("Exit in progress - waiting")

        except Exception as e:
            logger.exception(f"Unhandled error: {e}")
            self.state_manager.set_error(str(e))
            self.telegram.send_error_alert(str(e), "run()")

    def _check_expiry_day(self) -> None:
        """Check if today is expiry day and prepare for trading."""
        is_expiry, expiry_date = self.expiry_checker.is_expiry_today()

        if not is_expiry:
            logger.info("Not expiry day - will skip")
            self.state_manager.set_expiry_day(False)
            self.state_manager.update_status(self._TradingStatus.SKIPPED)
            return

        logger.info(f"Today is expiry day: {expiry_date}")
        self.state_manager.set_expiry_day(True)
        self.state_manager.update_status(self._TradingStatus.WAITING_ENTRY)

    def _handle_entry(self) -> None:
        """Handle entry window."""
        now = datetime.now()
        strategy_config = self.config.get('strategy', {})

        # Parse entry times from config
        entry_start_str = strategy_config.get('entry_start_time', '09:27')
        entry_end_str = strategy_config.get('entry_end_time', '09:30')

        entry_start_parts = entry_start_str.split(':')
        entry_end_parts = entry_end_str.split(':')

        entry_start = dt_time(int(entry_start_parts[0]), int(entry_start_parts[1]))
        entry_end = dt_time(int(entry_end_parts[0]), int(entry_end_parts[1]))

        if now.time() < entry_start:
            logger.info(f"Waiting for entry window ({entry_start_str})")
            return

        if now.time() > entry_end:
            logger.warning("Entry window missed - skipping today")
            self.state_manager.update_status(self._TradingStatus.SKIPPED)
            return

        logger.info("Entry window - executing entry")
        self.state_manager.update_status(self._TradingStatus.ENTERING)

        try:
            self._execute_entry()
        except Exception as e:
            logger.exception(f"Entry failed: {e}")
            self.state_manager.set_error(f"Entry failed: {e}")
            self.telegram.send_error_alert(str(e), "Entry")

    def _execute_entry(self) -> None:
        """Execute iron condor entry."""
        # Get market data with error handling
        try:
            ltp_data = self.kite.ltp(["NSE:NIFTY 50", "NSE:INDIA VIX"])
            spot = ltp_data.get("NSE:NIFTY 50", {}).get("last_price")
            vix = ltp_data.get("NSE:INDIA VIX", {}).get("last_price")

            if not spot or not vix:
                raise ValueError(f"Invalid LTP data: spot={spot}, vix={vix}")
        except Exception as e:
            raise ValueError(f"Failed to fetch market data: {e}")

        logger.info(f"Market: NIFTY={spot}, VIX={vix}")

        # Get expiry date and option chain
        is_expiry, expiry_date = self.expiry_checker.is_expiry_today()
        if not is_expiry or expiry_date is None:
            raise ValueError("Not expiry day - cannot execute entry")

        option_chain = self.expiry_checker.get_option_chain(expiry_date)
        lot_size = self.expiry_checker.get_lot_size(expiry_date)

        # Build iron condor setup
        setup = self.strategy.build_iron_condor(spot, vix, option_chain, expiry_date)

        if setup is None:
            raise ValueError("Could not build iron condor - strikes not found")

        # Get quotes for premium calculation
        symbols = [
            f"NFO:{setup.short_ce.symbol}",
            f"NFO:{setup.short_pe.symbol}",
            f"NFO:{setup.long_ce.symbol}",
            f"NFO:{setup.long_pe.symbol}"
        ]
        raw_quotes = self.kite.quote(symbols)

        # Convert quotes to our format (option quotes have depth)
        formatted_quotes: Dict[str, QuoteWrapper] = {}
        for key, data in raw_quotes.items():
            depth = data.get('depth', {})
            buy = depth.get('buy', [{}])
            sell = depth.get('sell', [{}])
            formatted_quotes[key] = QuoteWrapper(
                bid=buy[0].get('price', 0) if buy else 0,
                ask=sell[0].get('price', 0) if sell else 0,
                ltp=data.get('last_price', 0)
            )

        # Calculate premiums from bid/ask
        setup = self._calculate_premium_from_quotes(setup, formatted_quotes)

        logger.info(f"Setup: ATM={setup.atm_strike}, Wing={setup.wing_distance}, Credit={setup.total_credit:.2f}")

        # Execute orders
        num_lots = self.config.get('num_lots', 1)
        success, legs = self.order_executor.execute_iron_condor_entry(setup, num_lots)

        if not success:
            # Check if there was a rollback (partial fill scenario)
            rollback_msg = self.order_executor.get_last_rollback_message()
            if rollback_msg:
                # Send critical alert for rollback
                self.telegram.send_error_alert(
                    f"PARTIAL FILL ROLLBACK\n\n{rollback_msg}",
                    "Entry - CRITICAL"
                )
            raise ValueError("Order execution failed")

        # Calculate targets
        target_pnl, stop_loss_pnl = self.strategy.calculate_target_and_sl(
            setup.total_credit, num_lots, lot_size
        )

        # Create position
        position = self._Position(
            entry_time=datetime.now().strftime("%H:%M:%S"),
            spot_at_entry=spot,
            vix_at_entry=vix,
            wing_distance=setup.wing_distance,
            legs=legs,
            total_credit=setup.total_credit,
            target_pnl=target_pnl,
            stop_loss_pnl=stop_loss_pnl,
            lot_size=lot_size,
            num_lots=num_lots
        )

        # Save position
        self.state_manager.set_position(position)

        # Initialize chart data and persist for future invocations
        self._chart_data = self._ChartData(
            entry_time=datetime.now(),
            spot_at_entry=spot,
            wing_distance=setup.wing_distance,
            target_pnl=target_pnl,
            stop_loss_pnl=stop_loss_pnl,
            total_credit=setup.total_credit
        )
        # Save initial chart data to file
        self.chart_generator.save_pnl_history(self._chart_data)

        # Send telegram alert
        self.telegram.send_entry_alert(position, {
            'spot': spot,
            'vix': vix,
            'wing_distance': setup.wing_distance
        })

        logger.info("Entry complete!")

    def _handle_monitoring(self) -> None:
        """Monitor open position."""
        state = self.state_manager.state
        position = state.position

        if not position:
            logger.error("No position found in POSITION_OPEN state")
            self.state_manager.set_error("Position missing")
            return

        # Get current quotes
        symbols = [f"NFO:{leg.symbol}" for leg in position.legs]
        symbols.extend(["NSE:NIFTY 50", "NSE:INDIA VIX"])

        try:
            raw_quotes = self.kite.quote(symbols)
        except Exception as e:
            logger.error(f"Quote fetch failed: {e}")
            return

        # Format quotes - handle both option quotes (with depth) and index quotes
        quotes: Dict[str, QuoteWrapper] = {}
        for key, data in raw_quotes.items():
            ltp = data.get('last_price', 0)

            if 'depth' in data and data['depth']:
                # Option quotes have depth with bid/ask
                depth = data.get('depth', {})
                buy = depth.get('buy', [{}])
                sell = depth.get('sell', [{}])
                quotes[key] = QuoteWrapper(
                    bid=buy[0].get('price', 0) if buy else 0,
                    ask=sell[0].get('price', 0) if sell else 0,
                    ltp=ltp
                )
            else:
                # Index quotes (NIFTY, VIX) don't have depth - use LTP for all
                quotes[key] = QuoteWrapper(
                    bid=ltp,
                    ask=ltp,
                    ltp=ltp
                )

        # Calculate MTM P&L
        mtm_pnl, snapshot = self.position_manager.calculate_mtm_pnl(position, quotes)

        logger.info(f"MTM P&L: Rs.{mtm_pnl:,.0f} ({snapshot.pnl_pct:.1f}% of target)")

        # Update state
        self.state_manager.update_pnl(mtm_pnl, snapshot.vix)

        # Update chart data - try to load existing history first
        if self._chart_data is None:
            # Try to load persisted P&L history from previous invocations
            self._chart_data = self.chart_generator.load_pnl_history()

            if self._chart_data is None:
                # No history found, create fresh chart data
                self._chart_data = self._ChartData(
                    entry_time=datetime.strptime(position.entry_time, "%H:%M:%S").replace(
                        year=datetime.now().year,
                        month=datetime.now().month,
                        day=datetime.now().day
                    ),
                    spot_at_entry=position.spot_at_entry,
                    wing_distance=position.wing_distance,
                    target_pnl=position.target_pnl,
                    stop_loss_pnl=position.stop_loss_pnl,
                    total_credit=position.total_credit
                )
                logger.info("Created new P&L chart data")
            else:
                logger.info(f"Loaded P&L history with {len(self._chart_data.pnl_history)} points")

        # Add current P&L point and auto-save to file
        self.chart_generator.add_pnl_point(
            self._chart_data, mtm_pnl, snapshot.spot
        )

        # Garbage-book guard (2026-07-24 incident). mtm_pnl is a mid-price of
        # each leg's top-of-book; an unformed/one-sided/crossed/abnormally-wide
        # book makes it garbage. If ANY option leg is unreliable, FREEZE the
        # value-based exits (TARGET_HIT / STOP_LOSS) and let only the
        # time-based TIME_EXIT (must-exit) through. Thresholds mirror
        # src/utils/quote_reliability.py (SNAIL is a separate import root here).
        def _leg_book_reliable(qw: QuoteWrapper) -> bool:
            bid, ask = qw.bid, qw.ask
            if bid <= 0 or ask <= 0 or bid > ask:
                return False
            width = ask - bid
            mid = (bid + ask) / 2.0
            return width <= max(0.30, 0.25 * mid) + 1e-9

        books_reliable = all(
            _leg_book_reliable(quotes[f"NFO:{leg.symbol}"])
            for leg in position.legs
            if quotes.get(f"NFO:{leg.symbol}") is not None
        )

        # Check exit conditions
        exit_signal = self.position_manager.check_exit_conditions(position, mtm_pnl)

        _value_exits = (self._ExitReason.TARGET_HIT, self._ExitReason.STOP_LOSS)
        if (exit_signal.should_exit and exit_signal.reason in _value_exits
                and not books_reliable):
            logger.warning(
                f"{exit_signal.reason.value} suppressed - option books UNRELIABLE; "
                f"freezing value-based exit this poll (TIME_EXIT still armed)"
            )
        elif exit_signal.should_exit and exit_signal.reason is not None:
            logger.info(f"Exit signal: {exit_signal.reason.value} - {exit_signal.message}")
            self._execute_exit(exit_signal.reason, quotes)
            return

        # Check wing approach (logged only, no Telegram - only entry/exit alerts)
        wing_approach = self.position_manager.check_wing_approach(position, snapshot)
        if wing_approach:
            direction, proximity = wing_approach
            logger.warning(f"Wing approach: {direction} at {proximity:.0f}%")
            # Wing alerts disabled - only entry/exit alerts sent

        # Check VIX spike (logged only, no Telegram - only entry/exit alerts)
        vix_spike = self.position_manager.check_vix_spike(snapshot.vix, position.vix_at_entry)
        if vix_spike:
            logger.warning(f"VIX spike: +{vix_spike:.1f}")
            # VIX alerts disabled - only entry/exit alerts sent

        # P&L chart updates disabled - only entry/exit alerts sent
        # Chart still generated locally for logging purposes
        if self._chart_data:
            self.chart_generator.generate_pnl_chart(
                self._chart_data, mtm_pnl, snapshot.spot
            )

    def _execute_exit(self, reason: Any, quotes: Dict) -> None:
        """Execute position exit."""
        self.state_manager.update_status(self._TradingStatus.EXITING)

        state = self.state_manager.state
        position = state.position

        if position is None:
            logger.error("No position to exit")
            self.state_manager.set_error("No position to exit")
            return

        # Execute exit orders
        success, net_exit_debit = self.order_executor.execute_exit(position.legs, quotes)

        if not success:
            logger.error("Exit execution had issues")

        # Calculate P&L
        entry_credit = position.total_credit
        total_qty = position.num_lots * position.lot_size

        # Entry value (net credit received)
        entry_value = entry_credit * total_qty

        # Gross P&L = Entry Credit - Exit Net Debit
        # If we received 200 on entry and paid 150 net on exit, gross = 200 - 150 = 50 profit
        # If we received 200 on entry and paid 250 net on exit, gross = 200 - 250 = -50 loss
        gross_pnl = entry_value - net_exit_debit

        # For charges, calculate total exit turnover from leg exit prices
        exit_turnover = sum(leg.exit_price * total_qty for leg in position.legs if leg.exit_price > 0)

        # Charges (based on total turnover, not net)
        charges = self.position_manager.calculate_charges(position, exit_turnover)

        # Net P&L
        net_pnl = gross_pnl - charges

        logger.info(
            f"P&L Calc: Entry={entry_value:.0f}, NetExitDebit={net_exit_debit:.0f}, "
            f"Gross={gross_pnl:.0f}, Charges={charges:.0f}, Net={net_pnl:.0f}"
        )

        # Save trade record
        self._save_trade_record(
            Path(self.config['paths']['trades']),
            state,
            net_pnl,
            gross_pnl,
            charges
        )

        # Get reason string
        reason_str = reason.value if hasattr(reason, 'value') else str(reason)

        # Update state
        self.state_manager.set_exit(reason_str, net_pnl)

        # Generate summary chart
        if self._chart_data:
            self.chart_generator.generate_summary_chart(
                self._chart_data, net_pnl, reason_str
            )

        # Send exit alert
        self.telegram.send_exit_alert(position, reason, net_pnl, gross_pnl, charges)

    def _force_exit(self, reason: str) -> None:
        """Force exit position (e.g., after hours)."""
        try:
            # Get quotes
            state = self.state_manager.state
            position = state.position

            if not position:
                return

            symbols = [f"NFO:{leg.symbol}" for leg in position.legs]
            raw_quotes = self.kite.quote(symbols)

            # Format quotes (option quotes have depth)
            quotes: Dict[str, QuoteWrapper] = {}
            for key, data in raw_quotes.items():
                depth = data.get('depth', {})
                buy = depth.get('buy', [{}])
                sell = depth.get('sell', [{}])
                quotes[key] = QuoteWrapper(
                    bid=buy[0].get('price', 0) if buy else 0,
                    ask=sell[0].get('price', 0) if sell else 0,
                    ltp=data.get('last_price', 0)
                )

            self._execute_exit(self._ExitReason.TIME_EXIT, quotes)

        except Exception as e:
            logger.error(f"Force exit failed: {e}")
            self.state_manager.set_error(f"Force exit failed: {e}")


def main() -> None:
    """Main entry point."""
    try:
        config = load_config()
        system = ExpiryTradingSystem(config)
        system.run()
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()

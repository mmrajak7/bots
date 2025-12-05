"""
SNAIL Monitor Workflow

Main monitoring loop for active positions.

@file        monitor_workflow.py
@description Position monitoring workflow
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 9.3
"""

import sys
import time
import signal
from datetime import datetime, date, time as dt_time
from typing import Optional, Dict, Any, Callable
from dataclasses import dataclass
from enum import Enum
from loguru import logger

from src.services.position_monitor import PositionMonitor, MonitorSnapshot, get_position_monitor
from src.services.exit_manager import ExitManager, ExitReason, get_exit_manager
from src.services.entry_manager import EntryManager, get_entry_manager
from src.services.claude_advisor import ClaudeAdvisor, get_claude_advisor
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.telegram_bot import TelegramBot, get_telegram_bot, CallbackAction, UserResponse
from src.utils.db import (
    get_active_position,
    get_position_legs,
    get_latest_pnl_snapshot,
    get_today_market_data,
    capture_day_open,
    update_day_high_low,
    check_system_ready
)
from src.utils.helpers import is_trading_day, is_market_open
from src.utils.config import get_trading_config, get_monitoring_config, load_config


# =============================================================================
# CONSTANTS
# =============================================================================

# Monitoring intervals
MAIN_LOOP_INTERVAL = 60      # seconds
ACTIVE_POSITION_INTERVAL = 30 # seconds when position exists
GAP_CHECK_TIME = dt_time(9, 16)
MARKET_CLOSE_TIME = dt_time(15, 30)


# =============================================================================
# ENUMS
# =============================================================================

class MonitorWorkflowState(Enum):
    """Monitor workflow states."""
    IDLE = "idle"
    MONITORING = "monitoring"
    GAP_CHECK = "gap_check"
    EXIT_TRIGGERED = "exit_triggered"
    ENTRY_CHECK = "entry_check"
    STOPPED = "stopped"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class MonitorLoopStats:
    """
    Monitor loop statistics.

    Attributes:
        start_time: When monitoring started
        iterations: Number of loop iterations
        position_checks: Number of position checks
        alerts_sent: Number of alerts sent
        exits_triggered: Number of exits triggered
    """
    start_time: datetime
    iterations: int = 0
    position_checks: int = 0
    alerts_sent: int = 0
    exits_triggered: int = 0


# =============================================================================
# MONITOR WORKFLOW CLASS
# =============================================================================

class MonitorWorkflow:
    """
    Main monitoring workflow for SNAIL.

    Responsibilities:
    - Monitor active positions
    - Check for entry opportunities
    - Handle gap opens
    - Trigger exits when needed
    - Handle stop loss with Claude advisory

    Attributes:
        position_monitor: Position monitor service
        exit_manager: Exit manager service
        entry_manager: Entry manager service
        claude_advisor: Claude advisor service
        telegram: Telegram alerts
        config: Configuration
    """

    def __init__(
        self,
        position_monitor: Optional[PositionMonitor] = None,
        exit_manager: Optional[ExitManager] = None,
        entry_manager: Optional[EntryManager] = None,
        claude_advisor: Optional[ClaudeAdvisor] = None,
        telegram: Optional[TelegramAlerts] = None,
        telegram_bot: Optional[TelegramBot] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize monitor workflow.

        Args:
            position_monitor: Position monitor
            exit_manager: Exit manager
            entry_manager: Entry manager
            claude_advisor: Claude advisor
            telegram: Telegram alerts
            telegram_bot: Telegram bot for polling
            config: Configuration
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()
        self.monitoring_config = get_monitoring_config()

        self.position_monitor = position_monitor or get_position_monitor()
        self.exit_manager = exit_manager or get_exit_manager()
        self.entry_manager = entry_manager or get_entry_manager()
        self.claude_advisor = claude_advisor or get_claude_advisor()
        self.telegram = telegram or get_telegram()

        # Telegram bot for polling (hybrid architecture)
        try:
            self.telegram_bot = telegram_bot or get_telegram_bot()
        except ValueError as e:
            logger.warning(f"Telegram bot not initialized: {e}")
            self.telegram_bot = None

        self._state = MonitorWorkflowState.IDLE
        self._running = False
        self._stats = MonitorLoopStats(start_time=datetime.now())
        self._previous_close = None

        # Pending user decisions (waiting for button press)
        self._pending_stop_loss_decision = False
        self._pending_friday_decision = False
        self._pending_vix_decision = False

        # Signal handling
        self._setup_signal_handlers()

        logger.info("Monitor workflow initialized")

    def _setup_signal_handlers(self):
        """Setup graceful shutdown handlers."""
        def handler(signum, frame):
            logger.info(f"Received signal {signum}, stopping...")
            self.stop()

        signal.signal(signal.SIGINT, handler)
        signal.signal(signal.SIGTERM, handler)

    @property
    def state(self) -> MonitorWorkflowState:
        """Get current workflow state."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Check if workflow is running."""
        return self._running

    # =========================================================================
    # DAY OPEN CAPTURE & GAP DETECTION (TDD Section 4.3)
    # =========================================================================

    def _capture_day_open_and_check_gap(self) -> Optional[Dict[str, Any]]:
        """
        Capture day open at first monitor run (9:16 AM) and check for gaps.

        TDD Section 4.3: Day Open Capture & Gap Detection

        Called at 9:16 AM (1 minute after market opens) to:
        1. Capture the day's opening price to market_data table
        2. Detect gaps that could affect open positions
        3. Alert on significant gaps

        Gap Detection Thresholds (if position exists):
        - <0.5% from previous close: Normal, no action
        - 0.5-1%: Log only (info level)
        - >=1%: Claude advisory
        - Beyond wing strike: CRITICAL alert + Claude advisory (user decides)

        Returns:
            Gap information dict if significant gap detected, None otherwise
        """
        try:
            from src.api.kite_client import get_kite_client

            # Get current NIFTY price
            kite = get_kite_client()
            nifty_spot = kite.get_nifty_spot()

            # Check if day open already captured
            market_data = get_today_market_data()

            if market_data and market_data.day_open is not None:
                # Day open already captured, just update high/low
                update_day_high_low(nifty_spot)
                return None

            # Capture day open
            capture_day_open(nifty_spot)
            logger.info(f"Day open captured at 9:16 AM: {nifty_spot}")

            # Gap detection only if position exists
            position = get_active_position()
            if not position:
                return None

            # Calculate gap from previous close
            if not market_data or not market_data.previous_close:
                logger.warning("No previous close available for gap detection")
                return None

            previous_close = market_data.previous_close
            gap_percent = abs(nifty_spot - previous_close) / previous_close if previous_close > 0 else 0
            gap_direction = "UP" if nifty_spot > previous_close else "DOWN"
            gap_points = abs(nifty_spot - previous_close)

            # Check if gap is beyond wing (CRITICAL)
            upper_wing = position.atm_strike + position.wing_distance
            lower_wing = position.atm_strike - position.wing_distance
            beyond_wing = nifty_spot > upper_wing or nifty_spot < lower_wing

            gap_info = {
                'nifty_spot': nifty_spot,
                'previous_close': previous_close,
                'gap_percent': gap_percent,
                'gap_points': gap_points,
                'gap_direction': gap_direction,
                'beyond_wing': beyond_wing,
                'upper_wing': upper_wing,
                'lower_wing': lower_wing
            }

            if beyond_wing:
                # CRITICAL: Gap beyond wing - position at max loss
                logger.error(f"CRITICAL: Gap beyond wing! NIFTY={nifty_spot}, Wings={lower_wing}/{upper_wing}")

                breached_wing = upper_wing if nifty_spot > upper_wing else lower_wing
                distance_beyond = abs(nifty_spot - breached_wing)

                self.telegram.send(
                    f"🚨 *CRITICAL: Gap Beyond Wing!*\n\n"
                    f"NIFTY opened at ₹{nifty_spot:,.0f}\n"
                    f"Gap: {gap_direction} {gap_percent:.1%} ({gap_points:.0f} pts)\n"
                    f"Position wings: {lower_wing} - {upper_wing}\n"
                    f"Distance beyond wing: {distance_beyond:.0f} pts\n\n"
                    f"⚠️ *Position is at MAX LOSS*\n\n"
                    f"Analyzing with Claude..."
                )

                # Get Claude advisory (user decides, NO auto-exit)
                advisory = self.claude_advisor.get_gap_open_advisory(
                    gap_size=distance_beyond,
                    gap_direction=gap_direction,
                    opened_beyond_wing=True
                )

                gap_info['claude_decision'] = advisory.decision.value if advisory else 'UNKNOWN'
                gap_info['claude_reasoning'] = advisory.reasoning[:300] if advisory else ''
                gap_info['severity'] = 'CRITICAL'

                # Send Claude's analysis
                self.telegram.send(
                    f"🤖 *Claude Analysis:*\n\n"
                    f"{advisory.reasoning[:500] if advisory else 'Analysis unavailable'}\n\n"
                    f"Recommendation: *{gap_info['claude_decision']}*\n\n"
                    f"_User decision required - use /exit or /hold_"
                )

                self._stats.alerts_sent += 1
                return gap_info

            elif gap_percent >= 0.01:  # 1% or more
                # Significant gap - Claude advisory
                logger.warning(f"Significant gap at open: {gap_percent:.1%} {gap_direction}")

                self.telegram.send(
                    f"⚠️ *Significant Gap Detected*\n\n"
                    f"NIFTY opened at ₹{nifty_spot:,.0f}\n"
                    f"Previous close: ₹{previous_close:,.0f}\n"
                    f"Gap: {gap_direction} {gap_percent:.1%} ({gap_points:.0f} pts)\n\n"
                    f"Requesting Claude analysis..."
                )

                # Get Claude advisory
                advisory = self.claude_advisor.get_gap_open_advisory(
                    gap_size=gap_points,
                    gap_direction=gap_direction,
                    opened_beyond_wing=False
                )

                gap_info['claude_decision'] = advisory.decision.value if advisory else 'UNKNOWN'
                gap_info['claude_reasoning'] = advisory.reasoning[:300] if advisory else ''
                gap_info['severity'] = 'HIGH'

                self.telegram.send(
                    f"🤖 *Claude's Take:*\n{advisory.reasoning[:400] if advisory else 'Analysis unavailable'}"
                )

                self._stats.alerts_sent += 1
                return gap_info

            elif gap_percent >= 0.005:  # 0.5-1%
                # Moderate gap - log only
                logger.info(f"Moderate gap at open: {gap_percent:.1%} {gap_direction}")
                gap_info['severity'] = 'MODERATE'
                return gap_info

            else:
                # Normal gap (<0.5%)
                logger.debug(f"Normal opening: {gap_percent:.2%} {gap_direction}")
                gap_info['severity'] = 'NORMAL'
                return None

        except Exception as e:
            logger.error(f"Day open capture / gap check error: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _check_gap_open(self) -> Optional[Dict[str, Any]]:
        """
        Legacy method - redirects to new comprehensive implementation.

        Returns:
            Gap information if gap detected, None otherwise
        """
        return self._capture_day_open_and_check_gap()

    # =========================================================================
    # USER RESPONSE HANDLING
    # =========================================================================

    def _process_user_responses(self) -> Optional[bool]:
        """
        Process pending user responses from Telegram.

        Returns:
            True if exit action taken, False if hold, None if no responses
        """
        if not self.telegram_bot:
            return None

        responses = self.telegram_bot.get_pending_responses()

        for response in responses:
            logger.info(f"Processing user response: {response.action.value} for {response.alert_type}")

            # Handle stop loss decision
            if response.alert_type == "stop_loss":
                self._pending_stop_loss_decision = False

                if response.action == CallbackAction.EXIT:
                    position = get_active_position()
                    if position:
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.STOP_LOSS,
                            position=position
                        )
                        return result.success
                elif response.action == CallbackAction.HOLD:
                    logger.info("User chose HOLD at stop loss")
                    self.telegram.send("*Position HELD* per your decision. Monitoring continues.")
                    return False
                elif response.action == CallbackAction.ADJUST:
                    logger.info("User requested adjustment at stop loss")
                    self.telegram.send("*ADJUSTMENT requested*. Manual intervention required.")
                    return False

            # Handle Friday decision
            elif response.alert_type == "friday":
                self._pending_friday_decision = False

                if response.action == CallbackAction.EXIT:
                    position = get_active_position()
                    if position:
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.FRIDAY_CLOSE,
                            position=position
                        )
                        return result.success
                elif response.action == CallbackAction.HOLD:
                    logger.info("User chose HOLD over weekend")
                    self.telegram.send("*Position HELD* for weekend carry. Next check Monday.")
                    return False

            # Handle VIX warning decision
            elif response.alert_type == "vix_warning":
                self._pending_vix_decision = False

                if response.action == CallbackAction.EXIT:
                    position = get_active_position()
                    if position:
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.VIX_BREACH,
                            position=position
                        )
                        return result.success
                elif response.action == CallbackAction.HOLD:
                    logger.info("User chose HOLD despite VIX warning")
                    self.telegram.send("*Position HELD* despite VIX warning. Monitoring closely.")
                    return False

            # Handle exit confirmation
            elif response.alert_type == "exit_confirm":
                if response.action == CallbackAction.CONFIRM_YES:
                    position = get_active_position()
                    if position:
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.MANUAL,
                            position=position
                        )
                        return result.success
                elif response.action == CallbackAction.CONFIRM_NO:
                    self.telegram.send("*Exit cancelled.* Position unchanged.")
                    return False

            # Handle status/position/pnl requests
            elif response.alert_type in ["status_request", "position_request", "pnl_request"]:
                self._send_status_update()
                return None

        return None

    def _send_status_update(self) -> None:
        """Send current status update to Telegram."""
        position = get_active_position()

        if not position:
            self.telegram.send("""📊 *System Status*

• Position: None
• State: Waiting for entry opportunity
• Market: Open""" if is_market_open() else """📊 *System Status*

• Position: None
• State: Idle
• Market: Closed""")
            return

        # Get latest P&L snapshot
        snapshot = get_latest_pnl_snapshot(position.id)

        if snapshot:
            pnl_emoji = "🟢" if snapshot.current_pnl >= 0 else "🔴"
            pnl_sign = "+" if snapshot.current_pnl >= 0 else ""

            msg = f"""📊 *System Status*

• Position: Active Iron Fly
• ATM Strike: {position.atm_strike}
• Expiry: {position.expiry_date}

{pnl_emoji} *P&L: {pnl_sign}₹{snapshot.current_pnl:,.2f}* ({snapshot.pnl_percent:+.1f}%)

• NIFTY: ₹{snapshot.nifty_spot:,.2f}
• VIX: {snapshot.vix:.2f}

_Last updated: {datetime.now().strftime('%H:%M:%S')}_"""
        else:
            msg = f"""📊 *System Status*

• Position: Active Iron Fly
• ATM Strike: {position.atm_strike}
• Expiry: {position.expiry_date}

_Fetching P&L data..._"""

        self.telegram.send(msg)

    # =========================================================================
    # STOP LOSS HANDLING
    # =========================================================================

    def _handle_stop_loss(self, snapshot: MonitorSnapshot) -> bool:
        """
        Handle stop loss condition with Claude advisory and user decision.

        Uses hybrid architecture: sends alert with buttons and waits for user action.

        Args:
            snapshot: Current position snapshot

        Returns:
            True if exit was triggered
        """
        position = get_active_position()
        if not position:
            return False

        # Skip if already waiting for user decision
        if self._pending_stop_loss_decision:
            logger.debug("Waiting for user stop loss decision...")
            return False

        # Get Claude stop loss advisory
        advisory = self.claude_advisor.get_stop_loss_advisory()

        # Calculate loss percentage
        loss_percent = abs(snapshot.pnl_percentage) if snapshot.pnl_percentage < 0 else 0

        # Send decision buttons via Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_stop_loss_decision(
                current_pnl=snapshot.current_pnl,
                loss_percent=loss_percent,
                claude_advice=advisory.reasoning[:500] if advisory else "Analysis unavailable",
                position_id=position.id
            )
            self._pending_stop_loss_decision = True
            logger.info("Stop loss decision sent to user via Telegram")
            return False
        else:
            # Fallback: use Claude's recommendation directly
            if advisory and advisory.action_required:
                logger.info("Claude recommends exit at stop loss level (no Telegram bot)")
                result = self.exit_manager.execute_exit(
                    reason=ExitReason.STOP_LOSS,
                    position=position
                )
                return result.success

        # Claude recommends hold
        logger.info("Claude recommends holding at stop loss level")
        return False

    # =========================================================================
    # FRIDAY HANDLING
    # =========================================================================

    def _check_friday_close(self) -> bool:
        """
        Check and handle Friday close logic with user decision.

        Returns:
            True if exit was triggered
        """
        today = date.today()

        # Check if Friday
        if today.weekday() != 4:
            return False

        position = get_active_position()
        if not position:
            return False

        # Skip if already waiting for decision
        if self._pending_friday_decision:
            logger.debug("Waiting for user Friday decision...")
            return False

        # Check if expiry is next week or later
        from datetime import timedelta
        if position.expiry_date and position.expiry_date <= today + timedelta(days=2):
            return False  # Position expires this week, let it run

        # Get Friday exit time from config
        friday_exit_time = self.trading_config.get('exit', {}).get('friday_check_time', '15:00')
        current_time_str = datetime.now().strftime('%H:%M')

        if current_time_str >= friday_exit_time:
            # Get Claude decision
            advisory = self.claude_advisor.get_friday_decision()

            # Get current P&L
            snapshot = get_latest_pnl_snapshot(position.id)
            current_pnl = snapshot.current_pnl if snapshot else 0

            # Calculate DTE
            dte = (position.expiry_date - today).days if position.expiry_date else 0

            # Send decision buttons via Telegram bot
            if self.telegram_bot:
                self.telegram_bot.send_friday_decision(
                    current_pnl=current_pnl,
                    dte=dte,
                    claude_advice=advisory.reasoning[:500] if advisory else "Analysis unavailable",
                    position_id=position.id
                )
                self._pending_friday_decision = True
                logger.info("Friday decision sent to user via Telegram")
                return False
            else:
                # Fallback: use Claude's recommendation
                if advisory and advisory.action_required:
                    logger.info("Friday close: Exiting position (no Telegram bot)")
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.FRIDAY_CLOSE,
                        position=position
                    )
                    return result.success

        return False

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    def _single_iteration(self) -> None:
        """Execute single monitoring iteration."""
        self._stats.iterations += 1
        current_time = datetime.now()

        try:
            # Check system status (TDD Section 4.3)
            # Monitor can run in 'normal' or 'monitoring_paused' states
            is_ready, status_msg = check_system_ready(['normal', 'monitoring_paused'])
            if not is_ready:
                # System in error/maintenance state - skip this iteration
                if self._stats.iterations % 10 == 1:  # Log every 10 iterations
                    logger.warning(f"Monitor skipping iteration: {status_msg}")
                self._state = MonitorWorkflowState.IDLE
                return

            # Process any pending user responses from Telegram
            response_result = self._process_user_responses()
            if response_result is True:
                # Exit was triggered by user action
                self._stats.exits_triggered += 1
                return

            # Check if market is open
            if not is_market_open():
                self._state = MonitorWorkflowState.IDLE
                return

            # Check for active position
            position = get_active_position()

            if position:
                self._state = MonitorWorkflowState.MONITORING
                self._stats.position_checks += 1

                # Gap check at market open (9:16)
                if current_time.time() >= GAP_CHECK_TIME and current_time.time() < dt_time(9, 20):
                    if not hasattr(self, '_gap_checked_today') or self._gap_checked_today != date.today():
                        self._state = MonitorWorkflowState.GAP_CHECK
                        gap_info = self._check_gap_open()
                        self._gap_checked_today = date.today()

                        if gap_info and gap_info.get('beyond_wing'):
                            # Handle based on Claude decision
                            from src.api.claude_client import ClaudeDecision
                            if gap_info.get('claude_decision') == ClaudeDecision.EXIT.value:
                                self.exit_manager.execute_exit(
                                    reason=ExitReason.GAP_OPEN,
                                    position=position
                                )
                                return

                # Take position snapshot
                snapshot = self.position_monitor.take_snapshot()

                if snapshot:
                    # Check stop loss (50% max loss)
                    stop_loss_pct = self.trading_config.get('exit', {}).get('stop_loss_pct', 50)

                    if snapshot.pnl_percentage < 0 and abs(snapshot.pnl_percentage) >= stop_loss_pct:
                        if self._handle_stop_loss(snapshot):
                            self._stats.exits_triggered += 1
                            return

                # Friday close check
                if self._check_friday_close():
                    self._stats.exits_triggered += 1
                    return

            else:
                # No active position - check for entry opportunity
                self._state = MonitorWorkflowState.ENTRY_CHECK

                # Only check for entry during entry window
                entry_start = self.trading_config.get('entry', {}).get('start_time', '09:20')
                entry_end = self.trading_config.get('entry', {}).get('end_time', '14:30')

                current_time_str = current_time.strftime('%H:%M')

                if entry_start <= current_time_str <= entry_end:
                    # Check entry conditions
                    conditions = self.entry_manager.check_entry_conditions()

                    if conditions.can_enter:
                        logger.info("Entry conditions met, initiating entry workflow")
                        from src.workflows.entry_workflow import EntryWorkflow

                        entry_workflow = EntryWorkflow()
                        entry_workflow.run()

        except Exception as e:
            logger.error(f"Monitor iteration error: {e}")
            import traceback
            traceback.print_exc()

    def run(self) -> None:
        """
        Run the main monitoring loop.

        Runs until stop() is called or signal received.
        Starts Telegram polling for user commands and decisions.
        """
        logger.info("Starting monitor workflow loop")
        self._running = True
        self._stats = MonitorLoopStats(start_time=datetime.now())

        # Start Telegram polling
        if self.telegram_bot:
            self.telegram_bot.start_polling()
            logger.info("Telegram polling started")

        try:
            while self._running:
                self._single_iteration()

                # Determine sleep interval
                position = get_active_position()
                if position and is_market_open():
                    interval = ACTIVE_POSITION_INTERVAL
                else:
                    interval = MAIN_LOOP_INTERVAL

                # Interruptible sleep
                for _ in range(interval):
                    if not self._running:
                        break
                    time.sleep(1)

        except Exception as e:
            logger.error(f"Monitor workflow error: {e}")

        finally:
            # Stop Telegram polling
            if self.telegram_bot and self.telegram_bot.is_running:
                self.telegram_bot.stop_polling()
                logger.info("Telegram polling stopped")

            self._state = MonitorWorkflowState.STOPPED
            self._running = False
            logger.info("Monitor workflow stopped")

    def stop(self) -> None:
        """Stop the monitoring loop gracefully."""
        logger.info("Stopping monitor workflow...")
        self._running = False

    def run_once(self) -> None:
        """
        Run a single monitoring iteration.

        Used for cron-based monitoring instead of continuous loop.
        """
        logger.info("Running single monitor iteration")
        self._stats = MonitorLoopStats(start_time=datetime.now())
        self._single_iteration()
        logger.info("Monitor iteration complete")

    def get_stats(self) -> Dict[str, Any]:
        """
        Get monitoring statistics.

        Returns:
            Statistics dictionary
        """
        runtime = datetime.now() - self._stats.start_time

        return {
            'state': self._state.value,
            'running': self._running,
            'start_time': self._stats.start_time.isoformat(),
            'runtime_seconds': runtime.total_seconds(),
            'iterations': self._stats.iterations,
            'position_checks': self._stats.position_checks,
            'alerts_sent': self._stats.alerts_sent,
            'exits_triggered': self._stats.exits_triggered
        }


# =============================================================================
# STANDALONE EXECUTION
# =============================================================================

def run_monitor_workflow() -> None:
    """Run monitor workflow as standalone function."""
    workflow = MonitorWorkflow()
    workflow.run()


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    from dotenv import load_dotenv
    from datetime import timedelta

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Monitor Workflow")
    print("=" * 60)

    try:
        workflow = MonitorWorkflow()

        print("\n[1] Checking current state...")
        position = get_active_position()
        print(f"    Active position: {'Yes' if position else 'No'}")
        print(f"    Market open: {is_market_open()}")
        print(f"    Trading day: {is_trading_day()}")

        print("\n[2] Running single iteration...")
        workflow._single_iteration()
        print(f"    State: {workflow.state.value}")

        print("\n[3] Stats:")
        stats = workflow.get_stats()
        for key, value in stats.items():
            print(f"    {key}: {value}")

        print(f"\n{'='*60}")
        print("Monitor workflow check complete")
        print("Run with --loop flag for continuous monitoring")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

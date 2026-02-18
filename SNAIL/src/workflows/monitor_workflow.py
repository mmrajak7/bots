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
from typing import Optional, Dict, Any
from dataclasses import dataclass
from enum import Enum
from loguru import logger

from src.services.position_monitor import PositionMonitor, MonitorSnapshot, get_position_monitor
from src.services.exit_manager import ExitManager, ExitReason, get_exit_manager
from src.services.entry_manager import EntryManager, get_entry_manager
from src.services.claude_advisor import ClaudeAdvisor, get_claude_advisor
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.telegram_bot import TelegramBot, get_telegram_bot, CallbackAction
from src.utils.db import (
    get_active_position,
    get_latest_pnl_snapshot,
    get_today_market_data,
    capture_day_open,
    update_day_high_low,
    check_system_ready,
    is_on_cooldown,
    set_cooldown,
    get_cooldown_remaining,
    has_pending_decision,
    set_pending_decision,
    clear_pending_decision,
    # Trailing profit functions
    get_trailing_state,
    activate_trailing,
    update_trailing_peak,
    reset_trailing_state,
    # Exit in progress guard
    is_exit_in_progress
)
from src.utils.helpers import is_trading_day, is_market_open
from src.utils.config import get_trading_config, get_monitoring_config, load_config, is_bot_enabled


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

        # NOTE: Pending user decisions are now tracked in DB via has_pending_decision()
        # This survives process restarts (cron-based execution)

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

            if nifty_spot is None:
                logger.warning("Cannot capture day open: NIFTY spot price unavailable")
                return None

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

                gap_info['severity'] = 'CRITICAL'

                # Get Claude advisory (if enabled)
                use_claude = self.trading_config.get('entry', {}).get('use_claude_advisory', False)
                if use_claude:
                    advisory = self.claude_advisor.get_gap_open_advisory(
                        gap_size=distance_beyond,
                        gap_direction=gap_direction,
                        opened_beyond_wing=True
                    )
                    gap_info['claude_decision'] = advisory.decision.value if advisory else 'UNKNOWN'
                    gap_info['claude_reasoning'] = advisory.reasoning[:300] if advisory else ''
                    claude_text = f"\n\n🤖 *Analysis:* {advisory.reasoning[:500] if advisory else 'N/A'}"
                else:
                    gap_info['claude_decision'] = 'UNKNOWN'
                    gap_info['claude_reasoning'] = ''
                    claude_text = ""

                self.telegram.send(
                    f"🚨 *CRITICAL: Gap Beyond Wing!*\n\n"
                    f"NIFTY opened at ₹{nifty_spot:,.0f}\n"
                    f"Gap: {gap_direction} {gap_percent:.1%} ({gap_points:.0f} pts)\n"
                    f"Position wings: {lower_wing} - {upper_wing}\n"
                    f"Distance beyond wing: {distance_beyond:.0f} pts\n\n"
                    f"⚠️ *Position is at MAX LOSS*"
                    f"{claude_text}\n\n"
                    f"_User decision required - use /exit or /hold_"
                )

                self._stats.alerts_sent += 1
                return gap_info

            elif gap_percent >= 0.01:  # 1% or more
                # Significant gap
                logger.warning(f"Significant gap at open: {gap_percent:.1%} {gap_direction}")

                gap_info['severity'] = 'HIGH'

                # Get Claude advisory (if enabled)
                use_claude = self.trading_config.get('entry', {}).get('use_claude_advisory', False)
                claude_text = ""
                if use_claude:
                    advisory = self.claude_advisor.get_gap_open_advisory(
                        gap_size=gap_points,
                        gap_direction=gap_direction,
                        opened_beyond_wing=False
                    )
                    gap_info['claude_decision'] = advisory.decision.value if advisory else 'UNKNOWN'
                    gap_info['claude_reasoning'] = advisory.reasoning[:300] if advisory else ''
                    claude_text = f"\n\n🤖 *Analysis:* {advisory.reasoning[:500] if advisory else 'N/A'}"
                else:
                    gap_info['claude_decision'] = 'UNKNOWN'
                    gap_info['claude_reasoning'] = ''

                self.telegram.send(
                    f"⚠️ *Significant Gap Detected*\n\n"
                    f"NIFTY opened at ₹{nifty_spot:,.0f}\n"
                    f"Previous close: ₹{previous_close:,.0f}\n"
                    f"Gap: {gap_direction} {gap_percent:.1%} ({gap_points:.0f} pts)"
                    f"{claude_text}"
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

        Checks both:
        1. In-memory queue (when telegram_bot runs in same process)
        2. Shared file queue (when telegram_poller daemon is separate process)

        Returns:
            True if exit action taken, False if hold, None if no responses
        """
        # First check shared file queue (from telegram_poller daemon)
        from src.api.response_handler import TelegramResponseHandler
        callbacks = TelegramResponseHandler.read_callbacks()

        for cb in callbacks:
            action_str = cb.get('action', '')
            alert_type = cb.get('alert_type', 'unknown')

            logger.info(f"Processing callback from shared queue: {action_str} for {alert_type}")

            # Convert to UserResponse-like handling
            position = get_active_position()

            if alert_type == "stop_loss":
                if position:
                    clear_pending_decision('stop_loss', position.id)
                else:
                    logger.warning("Stop loss callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    return False
                if action_str == "exit":
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.STOP_LOSS,
                        position=position
                    )
                    return result.success
                elif action_str == "hold":
                    # Set 8-hour cooldown to suppress alerts and Claude API calls
                    set_cooldown('stop_loss_hold', 8 * 3600)
                    logger.info("User chose HOLD at stop loss - 8hr cooldown set (from callback)")
                    self.telegram.send("*Position HELD* per your decision.\n⏰ Alerts suppressed for 8 hours.")
                    return False
                elif action_str == "adjust":
                    logger.info("User requested adjustment at stop loss (from callback)")
                    self.telegram.send("*ADJUSTMENT requested*. Manual intervention required.")
                    return False

            elif alert_type == "friday":
                if position:
                    clear_pending_decision('friday', position.id)
                else:
                    logger.warning("Friday callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    return False
                if action_str == "exit":
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.FRIDAY_CLOSE,
                        position=position
                    )
                    return result.success
                elif action_str == "hold":
                    logger.info("User chose HOLD over weekend (from callback)")
                    self.telegram.send("*Position HELD* for weekend carry. Next check Monday.")
                    return False

            elif alert_type == "vix_warning":
                if position:
                    clear_pending_decision('vix_warning', position.id)
                else:
                    logger.warning("VIX warning callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    return False
                if action_str == "exit":
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.VIX_BREACH,
                        position=position
                    )
                    return result.success
                elif action_str == "hold":
                    # Set 8-hour cooldown to suppress alerts and Claude API calls
                    set_cooldown('vix_hold', 8 * 3600)
                    logger.info("User chose HOLD despite VIX warning - 8hr cooldown set (from callback)")
                    self.telegram.send("*Position HELD* despite VIX warning.\n⏰ Alerts suppressed for 8 hours.")
                    return False

            elif alert_type == "wing_approach":
                if position:
                    clear_pending_decision('wing_approach', position.id)
                else:
                    logger.warning("Wing approach callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    return False
                if action_str == "exit":
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.WING_BREACH,
                        position=position
                    )
                    return result.success
                elif action_str == "hold":
                    # Set 8-hour cooldown to suppress alerts and Claude API calls
                    set_cooldown('wing_hold', 8 * 3600)
                    logger.info("User chose HOLD at wing approach - 8hr cooldown set (from callback)")
                    self.telegram.send("*Position HELD* at wing approach.\n⏰ Alerts suppressed for 8 hours.")
                    return False
                elif action_str == "adjust":
                    logger.info("User requested adjustment at wing approach (from callback)")
                    self.telegram.send("*ADJUSTMENT requested* at wing approach. Manual intervention required.")
                    return False

            elif alert_type == "exit_confirm":
                if action_str == "yes":
                    position = get_active_position()
                    if position:
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.MANUAL,
                            position=position
                        )
                        return result.success
                elif action_str == "no":
                    self.telegram.send("*Exit cancelled.* Position unchanged.")
                    return False

            elif alert_type == "manual_hold":
                logger.info("User confirmed HOLD via /hold command")
                return False

        # Then check in-memory queue (for backward compat when bot runs in same process)
        if not self.telegram_bot:
            return None

        responses = self.telegram_bot.get_pending_responses()
        position = get_active_position()

        for response in responses:
            logger.info(f"Processing user response: {response.action.value} for {response.alert_type}")

            # Handle stop loss decision
            if response.alert_type == "stop_loss":
                if position:
                    clear_pending_decision('stop_loss', position.id)
                else:
                    logger.warning("Stop loss callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    continue

                if response.action == CallbackAction.EXIT:
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.STOP_LOSS,
                        position=position
                    )
                    return result.success
                elif response.action == CallbackAction.HOLD:
                    # Set 8-hour cooldown to suppress alerts and Claude API calls
                    set_cooldown('stop_loss_hold', 8 * 3600)
                    logger.info("User chose HOLD at stop loss - 8hr cooldown set")
                    self.telegram.send("*Position HELD* per your decision.\n⏰ Alerts suppressed for 8 hours.")
                    return False
                elif response.action == CallbackAction.ADJUST:
                    logger.info("User requested adjustment at stop loss")
                    self.telegram.send("*ADJUSTMENT requested*. Manual intervention required.")
                    return False

            # Handle Friday decision
            elif response.alert_type == "friday":
                if position:
                    clear_pending_decision('friday', position.id)
                else:
                    logger.warning("Friday callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    continue

                if response.action == CallbackAction.EXIT:
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
                if position:
                    clear_pending_decision('vix_warning', position.id)
                else:
                    logger.warning("VIX warning callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    continue

                if response.action == CallbackAction.EXIT:
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.VIX_BREACH,
                        position=position
                    )
                    return result.success
                elif response.action == CallbackAction.HOLD:
                    # Set 8-hour cooldown to suppress alerts and Claude API calls
                    set_cooldown('vix_hold', 8 * 3600)
                    logger.info("User chose HOLD despite VIX warning - 8hr cooldown set")
                    self.telegram.send("*Position HELD* despite VIX warning.\n⏰ Alerts suppressed for 8 hours.")
                    return False

            # Handle wing approach decision
            elif response.alert_type == "wing_approach":
                if position:
                    clear_pending_decision('wing_approach', position.id)
                else:
                    logger.warning("Wing approach callback received but no active position")
                    self.telegram.send("⚠️ No active position found. Alert may be stale.")
                    continue

                if response.action == CallbackAction.EXIT:
                    result = self.exit_manager.execute_exit(
                        reason=ExitReason.WING_BREACH,
                        position=position
                    )
                    return result.success
                elif response.action == CallbackAction.HOLD:
                    # Set 8-hour cooldown to suppress alerts and Claude API calls
                    set_cooldown('wing_hold', 8 * 3600)
                    logger.info("User chose HOLD at wing approach - 8hr cooldown set")
                    self.telegram.send("*Position HELD* at wing approach.\n⏰ Alerts suppressed for 8 hours.")
                    return False
                elif response.action == CallbackAction.ADJUST:
                    logger.info("User requested adjustment at wing approach")
                    self.telegram.send("*ADJUSTMENT requested* at wing approach. Manual intervention required.")
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
    # VIX WARNING HANDLING
    # =========================================================================

    def _handle_vix_warning(self, snapshot: MonitorSnapshot) -> bool:
        """
        Handle VIX warning (16-20) with decision buttons.

        Args:
            snapshot: Current position snapshot

        Returns:
            True if exit was triggered
        """
        position = get_active_position()
        if not position:
            return False

        # Skip if user previously chose HOLD (8-hour cooldown active)
        if is_on_cooldown('vix_hold'):
            remaining = get_cooldown_remaining('vix_hold')
            if remaining:
                hrs = remaining // 3600
                mins = (remaining % 3600) // 60
                logger.debug(f"VIX warning alert suppressed - HOLD cooldown active ({hrs}h {mins}m remaining)")
            return False

        # Skip if already waiting for user decision (persisted in DB)
        if has_pending_decision('vix_warning', position.id):
            logger.debug("Waiting for user VIX warning decision...")
            return False

        # Get Claude VIX advisory (if enabled)
        use_claude = self.trading_config.get('entry', {}).get('use_claude_advisory', False)
        advice_text = "VIX elevated. Monitor closely."
        if use_claude:
            advisory = self.claude_advisor.get_vix_spike_advisory(snapshot.india_vix, 0)
            if advisory:
                advice_text = advisory.reasoning[:1000]

        # Send decision buttons via Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_vix_warning_decision(
                current_vix=snapshot.india_vix,
                claude_advice=advice_text,
                position_id=position.id
            )
            set_pending_decision('vix_warning', position.id)
            logger.info(f"VIX warning decision sent to user (VIX={snapshot.india_vix:.2f})")
            return False
        else:
            # Fallback: just log, don't auto-exit (VIX < 20 is warning only)
            logger.warning(f"VIX warning at {snapshot.india_vix:.2f} (no Telegram bot for buttons)")
            return False

    # =========================================================================
    # WING APPROACH HANDLING
    # =========================================================================

    def _handle_wing_approach(self, snapshot: MonitorSnapshot, direction: str, proximity_pct: float) -> bool:
        """
        Handle wing approach with decision buttons.

        Args:
            snapshot: Current position snapshot
            direction: Wing direction (CE/PE)
            proximity_pct: How close to wing (0-100%)

        Returns:
            True if exit was triggered
        """
        position = get_active_position()
        if not position:
            return False

        # Skip if user previously chose HOLD (8-hour cooldown active)
        if is_on_cooldown('wing_hold'):
            remaining = get_cooldown_remaining('wing_hold')
            if remaining:
                hrs = remaining // 3600
                mins = (remaining % 3600) // 60
                logger.debug(f"Wing approach alert suppressed - HOLD cooldown active ({hrs}h {mins}m remaining)")
            return False

        # Skip if already waiting for user decision (persisted in DB)
        if has_pending_decision('wing_approach', position.id):
            logger.debug("Waiting for user wing approach decision...")
            return False

        # Get Claude wing approach advisory (if enabled)
        use_claude = self.trading_config.get('entry', {}).get('use_claude_advisory', False)
        advice_text = f"Price approaching {direction} wing."
        if use_claude:
            advisory = self.claude_advisor.get_wing_approach_advisory(direction)
            if advisory:
                advice_text = advisory.reasoning[:1000]

        # Send decision buttons via Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_wing_approach_decision(
                direction=direction,
                proximity_percent=proximity_pct,
                claude_advice=advice_text,
                position_id=position.id
            )
            set_pending_decision('wing_approach', position.id)
            logger.info(f"Wing approach decision sent to user ({direction}, {proximity_pct:.0f}%)")
            return False
        else:
            # Fallback: just log, don't auto-exit
            logger.warning(f"Wing approach {direction} at {proximity_pct:.0f}% (no Telegram bot for buttons)")
            return False

    # =========================================================================
    # STOP LOSS HANDLING
    # =========================================================================

    def _handle_stop_loss(self, snapshot: MonitorSnapshot) -> bool:
        """
        Handle stop loss condition with user decision buttons.

        Uses hybrid architecture: sends alert with buttons and waits for user action.

        Args:
            snapshot: Current position snapshot

        Returns:
            True if exit was triggered
        """
        position = get_active_position()
        if not position:
            return False

        # Skip if user previously chose HOLD (8-hour cooldown active)
        if is_on_cooldown('stop_loss_hold'):
            remaining = get_cooldown_remaining('stop_loss_hold')
            if remaining:
                hrs = remaining // 3600
                mins = (remaining % 3600) // 60
                logger.debug(f"Stop loss alert suppressed - HOLD cooldown active ({hrs}h {mins}m remaining)")
            return False

        # Skip if already waiting for user decision (persisted in DB)
        if has_pending_decision('stop_loss', position.id):
            logger.debug("Waiting for user stop loss decision...")
            return False

        # Get Claude stop loss advisory (if enabled)
        use_claude = self.trading_config.get('entry', {}).get('use_claude_advisory', False)
        advice_text = "Stop loss level reached. Review position and decide."
        if use_claude:
            advisory = self.claude_advisor.get_stop_loss_advisory()
            if advisory:
                advice_text = advisory.reasoning[:1000]

        # Calculate loss percentage
        loss_percent = abs(snapshot.pnl_percentage) if snapshot.pnl_percentage < 0 else 0

        # Send decision buttons via Telegram bot
        if self.telegram_bot:
            self.telegram_bot.send_stop_loss_decision(
                current_pnl=snapshot.current_pnl,
                loss_percent=loss_percent,
                claude_advice=advice_text,
                position_id=position.id
            )
            set_pending_decision('stop_loss', position.id)
            logger.info("Stop loss decision sent to user via Telegram")
            return False
        else:
            # No Telegram bot - log and continue holding
            logger.warning("Stop loss level reached (no Telegram bot for buttons)")
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

        # Skip if already waiting for decision (persisted in DB)
        if has_pending_decision('friday', position.id):
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
            # Get Claude decision (if enabled)
            use_claude = self.trading_config.get('entry', {}).get('use_claude_advisory', False)
            advice_text = "Friday close. Consider weekend risk vs theta opportunity."
            if use_claude:
                advisory = self.claude_advisor.get_friday_decision()
                if advisory:
                    advice_text = advisory.reasoning[:1000]

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
                    claude_advice=advice_text,
                    position_id=position.id
                )
                set_pending_decision('friday', position.id)
                logger.info("Friday decision sent to user via Telegram")
                return False
            else:
                # No Telegram bot - log and continue holding
                logger.warning("Friday close time reached (no Telegram bot for buttons)")
                return False

        return False

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    def _single_iteration(self) -> None:
        """Execute single monitoring iteration."""
        self._stats.iterations += 1
        current_time = datetime.now()

        try:
            # =================================================================
            # BOT ENABLED CHECK (KILL SWITCH)
            # =================================================================
            # Check if bot is disabled via config or /stop command
            # This is the master kill switch to immediately stop all trading
            if not is_bot_enabled():
                # Log once per minute (every ~2 iterations at 30s interval)
                if self._stats.iterations % 2 == 1:
                    logger.warning("Monitor: Bot is DISABLED (kill switch active). Skipping all actions.")
                self._state = MonitorWorkflowState.STOPPED
                # Still process user commands (so /resume works)
                self._process_user_responses()
                return

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
                logger.info("Monitor: Market closed, skipping")
                self._state = MonitorWorkflowState.IDLE
                return

            # Check for active position
            position = get_active_position()

            if not position:
                logger.info("Monitor: No active position found")
                return

            # Active position found
            logger.info(f"Monitor: Position {position.id} found, taking snapshot...")
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
                            result = self.exit_manager.execute_exit(
                                reason=ExitReason.GAP_OPEN,
                                position=position
                            )
                            if result.success:
                                self._stats.exits_triggered += 1
                                return
                            else:
                                logger.error(f"Gap open exit failed: {result.error}")

            # Take position snapshot
            snapshot = self.position_monitor.take_snapshot()

            if snapshot:
                # Save snapshot to database for status/pnl commands
                self.position_monitor.save_snapshot_to_db(snapshot)

                # Calculate correct P&L percentage using margin deployed
                # (snapshot.pnl_percentage may have rounding issues)
                if position.margin_deployed and position.margin_deployed > 0:
                    current_pnl_pct = round((snapshot.current_pnl / position.margin_deployed) * 100, 4)
                else:
                    current_pnl_pct = snapshot.pnl_percentage

                logger.info(f"Monitor: P&L snapshot saved: ₹{snapshot.current_pnl:,.0f} ({current_pnl_pct:+.2f}% ROM)")

                # =============================================================
                # EXIT CHECKS — Each block is independently protected.
                # A failure in one check must NEVER skip safety-critical
                # checks below (stop loss, VIX breach, expiry).
                # =============================================================

                # ----- HARD CAP PROFIT TARGET (absolute INR amount, scaled by lots) -----
                try:
                    profit_target_per_lot = self.trading_config.get('exit', {}).get('profit_target_amount', 0)
                    if profit_target_per_lot > 0:
                        # Scale target by number of lots in the open position
                        from src.utils.calculations import NIFTY_LOT_SIZE
                        num_lots = max(1, position.lot_size // NIFTY_LOT_SIZE)
                        profit_target_amount = profit_target_per_lot * num_lots
                        logger.debug(f"Hard cap target: ₹{profit_target_per_lot:,.0f}/lot × {num_lots} lots = ₹{profit_target_amount:,.0f}")
                    else:
                        profit_target_amount = 0
                    if profit_target_amount > 0 and snapshot.current_pnl >= profit_target_amount:
                        logger.info(
                            f"HARD CAP TP HIT! P&L: ₹{snapshot.current_pnl:,.0f} >= ₹{profit_target_amount:,.0f} ({num_lots} lots × ₹{profit_target_per_lot:,.0f})"
                        )
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.PROFIT_TARGET,
                            position=position
                        )
                        if result.success:
                            reset_trailing_state(position.id)
                            self._stats.exits_triggered += 1
                            return
                        else:
                            logger.error(f"Hard cap TP exit failed: {result.error}")
                except Exception as e:
                    logger.error(f"Hard cap TP check error (continuing to next check): {e}")

                # ----- TRAILING PROFIT CHECK (VIX-Adaptive) -----
                trailing_state_cached = None  # Cache for reuse in fixed TP check
                trailing_enabled = False      # Safe default if trailing check throws
                try:
                    trailing_config = self.trading_config.get('exit', {}).get('trailing', {})
                    trailing_enabled = trailing_config.get('enabled', False)

                    if trailing_enabled:
                        activation_pct = trailing_config.get('activation_pct', 2.5)
                        lock_breakeven_at = trailing_config.get('lock_breakeven_at', 3.0)
                        default_trail_pct = trailing_config.get('trail_pct', 1.0)
                        min_holding_minutes = trailing_config.get('min_holding_minutes', 60)
                        expiry_day_trail_pct = trailing_config.get('expiry_day_trail_pct', 0.5)
                        above_target_trail_pct = trailing_config.get('above_target_trail_pct', None)

                        # ---- Determine effective trail width ----
                        # Priority: expiry_day > above_target > vix_adaptive > default
                        trail_pct = default_trail_pct

                        # VIX-adaptive trail width
                        vix_adaptive = trailing_config.get('vix_adaptive', {})
                        current_vix = snapshot.india_vix if snapshot.india_vix else None
                        vix_regime = "unknown"
                        if vix_adaptive.get('enabled', False) and current_vix is not None:
                            low_vix_threshold = vix_adaptive.get('low_vix_threshold', 13)
                            if current_vix < low_vix_threshold:
                                trail_pct = vix_adaptive.get('low_vix_trail_pct', 1.5)
                                vix_regime = f"low (VIX {current_vix:.1f} < {low_vix_threshold})"
                            else:
                                trail_pct = vix_adaptive.get('normal_trail_pct', 1.0)
                                vix_regime = f"normal (VIX {current_vix:.1f} >= {low_vix_threshold})"

                        # Above-target tightening: lock gains once past profit target
                        profit_target_pct = self.trading_config.get('exit', {}).get('profit_target_pct', 3)
                        is_above_target = above_target_trail_pct is not None and current_pnl_pct >= profit_target_pct
                        if is_above_target:
                            trail_pct = above_target_trail_pct
                            vix_regime = f"TIGHT (above {profit_target_pct}% target)"

                        # Expiry day: use tightest of expiry trail and current trail
                        is_expiry_day = position.expiry_date and position.expiry_date == date.today()
                        effective_trail_pct = min(expiry_day_trail_pct, trail_pct) if is_expiry_day else trail_pct

                        position_age_minutes = (datetime.now() - position.entry_time).total_seconds() / 60

                        # ALWAYS read trailing state - exit checks must run regardless of position age
                        # (trailing may have been activated before a config change increased min_holding_minutes)
                        trailing_state_cached = get_trailing_state(position.id)
                        trailing_active = trailing_state_cached.get('trailing_active', False)
                        current_peak_pct = trailing_state_cached.get('peak_pnl_pct')
                        current_floor_pct = trailing_state_cached.get('trailing_floor_pct')
                        breakeven_locked = trailing_state_cached.get('breakeven_locked', False)

                        if trailing_active:
                            # ---- ACTIVE TRAILING: peak update + exit check ----
                            # Runs regardless of position age (trailing was already activated)
                            if current_peak_pct is None:
                                logger.warning(f"Trailing active but peak is None - initializing to {current_pnl_pct:.2f}%")
                                current_peak_pct = current_pnl_pct

                            if current_pnl_pct > 0 and current_pnl_pct > current_peak_pct:
                                new_floor_pct, new_breakeven_locked = update_trailing_peak(
                                    position_id=position.id,
                                    new_peak_pct=current_pnl_pct,
                                    new_peak_amount=snapshot.current_pnl,
                                    trail_pct=effective_trail_pct,
                                    lock_breakeven_at=lock_breakeven_at,
                                    current_breakeven_locked=breakeven_locked,
                                    current_floor_pct=current_floor_pct
                                )

                                if new_breakeven_locked and not breakeven_locked:
                                    self.telegram.send(
                                        f"🔒 *Breakeven LOCKED*\n\n"
                                        f"P&L: ₹{snapshot.current_pnl:,.0f} ({current_pnl_pct:.2f}% ROM)\n"
                                        f"Lock at: {lock_breakeven_at}% | Floor: {new_floor_pct:.2f}%\n\n"
                                        f"_Profit protected - no loss possible_"
                                    )

                                # Log trail tightening when crossing target threshold
                                if is_above_target and current_floor_pct is not None and new_floor_pct > current_floor_pct:
                                    logger.info(
                                        f"Trail TIGHTENED above target: trail={effective_trail_pct:.1f}%, "
                                        f"floor {current_floor_pct:.2f}% -> {new_floor_pct:.2f}%"
                                    )

                                current_floor_pct = new_floor_pct
                                breakeven_locked = new_breakeven_locked

                            if current_floor_pct is None:
                                logger.warning("Trailing active but floor is None - skipping exit check")
                            elif round(current_pnl_pct, 2) <= round(current_floor_pct, 2):
                                if is_exit_in_progress(position.id):
                                    logger.debug(
                                        "Trailing stop condition met but exit already in progress - skipping"
                                    )
                                else:
                                    pnl_inr = snapshot.current_pnl
                                    peak_inr = (current_peak_pct / 100 * position.margin_deployed) if position.margin_deployed else 0
                                    logger.warning(
                                        f"TRAILING STOP HIT! P&L: {current_pnl_pct:.2f}% (₹{pnl_inr:,.0f}) "
                                        f"<= floor {current_floor_pct:.2f}% "
                                        f"(peak was {current_peak_pct:.2f}%, ₹{peak_inr:,.0f})"
                                    )
                                    self.telegram.send(
                                        f"📉 *Trailing Stop TRIGGERED*\n\n"
                                        f"P&L: ₹{pnl_inr:,.0f} ({current_pnl_pct:.2f}% ROM)\n"
                                        f"Floor: {current_floor_pct:.2f}%\n"
                                        f"Peak was: {current_peak_pct:.2f}% (₹{peak_inr:,.0f})\n\n"
                                        f"_Executing exit..._"
                                    )
                                    result = self.exit_manager.execute_exit(
                                        reason=ExitReason.TRAILING_STOP,
                                        position=position
                                    )
                                    if result.success:
                                        reset_trailing_state(position.id)
                                        self._stats.exits_triggered += 1
                                        return
                                    else:
                                        logger.error(f"Trailing stop exit failed: {result.error}")

                        elif position_age_minutes >= min_holding_minutes:
                            # ---- ACTIVATION CHECK (age-gated) ----
                            # Only activate trailing for mature positions
                            if current_pnl_pct > 0 and current_pnl_pct >= activation_pct:
                                floor_pct = activate_trailing(
                                    position_id=position.id,
                                    current_pnl_pct=current_pnl_pct,
                                    current_pnl_amount=snapshot.current_pnl,
                                    trail_pct=effective_trail_pct,
                                    lock_breakeven_at=lock_breakeven_at
                                )
                                pnl_inr = snapshot.current_pnl
                                vix_info = f"VIX {current_vix:.1f} | " if current_vix else ""
                                self.telegram.send(
                                    f"📈 *Trailing Profit ACTIVATED*\n\n"
                                    f"P&L: ₹{pnl_inr:,.0f} ({current_pnl_pct:.2f}% ROM)\n"
                                    f"Peak: {current_pnl_pct:.2f}% | Floor: {floor_pct:.2f}%\n"
                                    f"{vix_info}Trail: {effective_trail_pct:.1f}% ({vix_regime})\n"
                                    f"{'🔒 Expiry day - tighter trail' if is_expiry_day else ''}\n\n"
                                    f"_Will exit if P&L drops below floor_"
                                )
                                trailing_active = True
                                current_peak_pct = current_pnl_pct
                                current_floor_pct = floor_pct

                        else:
                            # Position too young for trailing activation
                            if current_pnl_pct >= activation_pct:
                                logger.debug(
                                    f"Trailing would activate but position too young "
                                    f"({position_age_minutes:.0f}/{min_holding_minutes} min)"
                                )
                except Exception as e:
                    logger.error(f"Trailing profit check error (continuing to next check): {e}")

                # ----- FIXED PROFIT TARGET CHECK -----
                try:
                    profit_target_pct = self.trading_config.get('exit', {}).get('profit_target_pct', 3)
                    auto_exit_on_tp = self.trading_config.get('exit', {}).get('auto_exit_on_tp', True)

                    use_fixed_tp = not trailing_enabled or not (trailing_state_cached or {}).get('trailing_active', False)

                    if use_fixed_tp and current_pnl_pct >= profit_target_pct:
                        logger.info(f"PROFIT TARGET HIT! P&L: {current_pnl_pct:.2f}% >= {profit_target_pct}% target")
                        if auto_exit_on_tp:
                            logger.info("Auto-exit on TP enabled - executing exit...")
                            result = self.exit_manager.execute_exit(
                                reason=ExitReason.PROFIT_TARGET,
                                position=position
                            )
                            if result.success:
                                self._stats.exits_triggered += 1
                                return
                            else:
                                logger.error(f"Profit target exit failed: {result.error}")
                        else:
                            logger.info("Auto-exit on TP disabled - sending advisory alert")
                            self.telegram.send(
                                f"🎯 *Profit Target Reached!*\n\n"
                                f"P&L: ₹{snapshot.current_pnl:,.0f} ({current_pnl_pct:+.1f}%)\n"
                                f"Target: {profit_target_pct}%\n\n"
                                f"_Use /exit to close position_"
                            )
                except Exception as e:
                    logger.error(f"Fixed TP check error (continuing to next check): {e}")

                # ----- STOP LOSS CHECK -----
                try:
                    stop_loss_pct = self.trading_config.get('exit', {}).get('stop_loss_pct', 5)
                    auto_exit_on_sl = self.trading_config.get('exit', {}).get('auto_exit_on_sl', True)

                    if current_pnl_pct < 0 and abs(current_pnl_pct) >= stop_loss_pct:
                        loss_pct = abs(current_pnl_pct)
                        logger.warning(f"STOP LOSS HIT! Loss: {loss_pct:.2f}% >= {stop_loss_pct}% threshold")
                        if auto_exit_on_sl:
                            logger.info("Auto-exit on SL enabled - executing exit...")
                            result = self.exit_manager.execute_exit(
                                reason=ExitReason.STOP_LOSS,
                                position=position
                            )
                            if result.success:
                                self._stats.exits_triggered += 1
                                return
                            else:
                                logger.error(f"Stop loss exit failed: {result.error}")
                        else:
                            if self._handle_stop_loss(snapshot):
                                self._stats.exits_triggered += 1
                                return
                except Exception as e:
                    logger.error(f"Stop loss check error (continuing to next check): {e}")

                # ----- VIX HARD EXIT CHECK (VIX > 20 = immediate exit) -----
                try:
                    vix_hard_exit = self.trading_config.get('exit', {}).get('vix_hard_exit', 20)

                    if snapshot.india_vix > vix_hard_exit:
                        logger.warning(f"VIX BREACH! VIX={snapshot.india_vix:.2f} > {vix_hard_exit} threshold - HARD EXIT")
                        result = self.exit_manager.execute_exit(
                            reason=ExitReason.VIX_BREACH,
                            position=position
                        )
                        if result.success:
                            self._stats.exits_triggered += 1
                            return
                        else:
                            logger.error(f"VIX breach exit failed: {result.error}")

                    # VIX warning (16-20) - send decision buttons
                    vix_config = self.trading_config.get('entry', {}).get('vix_range', {})
                    vix_max = vix_config.get('max', 16)

                    if vix_max < snapshot.india_vix < vix_hard_exit:
                        if self._handle_vix_warning(snapshot):
                            self._stats.exits_triggered += 1
                            return
                except Exception as e:
                    logger.error(f"VIX check error (continuing to next check): {e}")

                # ----- WING APPROACH CHECK -----
                try:
                    from src.utils.calculations import is_approaching_wing
                    approaching, direction = is_approaching_wing(
                        spot_price=snapshot.nifty_spot,
                        atm_strike=position.atm_strike,
                        wing_distance=position.wing_distance,
                        threshold_pct=0.75
                    )

                    if approaching:
                        wing_strike = position.atm_strike + (position.wing_distance if direction == 'CE' else -position.wing_distance)
                        distance_to_wing = abs(snapshot.nifty_spot - wing_strike)
                        wing_proximity = ((position.wing_distance - distance_to_wing) / position.wing_distance) * 100 if position.wing_distance > 0 else 0

                        if self._handle_wing_approach(snapshot, direction, wing_proximity):
                            self._stats.exits_triggered += 1
                            return
                except Exception as e:
                    logger.error(f"Wing approach check error (continuing to next check): {e}")

                # ----- FRIDAY CLOSE CHECK -----
                try:
                    if self._check_friday_close():
                        self._stats.exits_triggered += 1
                        return
                except Exception as e:
                    logger.error(f"Friday close check error (continuing to next check): {e}")

                # ----- EXPIRY DAY CHECK (auto-exit at 3:20 PM) -----
                try:
                    today = date.today()
                    if position.expiry_date and position.expiry_date == today:
                        current_time_str = datetime.now().strftime('%H:%M')
                        expiry_exit_time = self.trading_config.get('exit', {}).get('expiry_exit_time', '15:20')

                        if current_time_str >= expiry_exit_time:
                            logger.warning(f"EXPIRY DAY EXIT: Today is expiry ({position.expiry_date}), time={current_time_str}")
                            result = self.exit_manager.execute_exit(
                                reason=ExitReason.EXPIRY,
                                position=position
                            )
                            if result.success:
                                self._stats.exits_triggered += 1
                                return
                            else:
                                logger.error(f"Expiry day exit failed: {result.error}")
                except Exception as e:
                    logger.error(f"Expiry day check error: {e}")

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

"""
SNAIL Exit Manager

Manages Iron Fly position exit including profit target, stop loss, and manual exits.

@file        exit_manager.py
@description Iron Fly exit management service
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 4.2
"""

from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from enum import Enum
from loguru import logger

from src.api.kite_client import SNAILKiteClient, Quote, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.api.claude_client import SNAILClaudeClient, MarketContext, ClaudeDecision, get_claude_client
from src.utils.order_helpers import (
    execute_iron_fly_exit,
    IronFlyOrders,
    ExecutedOrder,
    OrderExecutionError
)
from src.utils.scaled_execution import (
    ScaledOrderExecutor,
    ExecutionResult as ScaledExecutionResult,
    should_use_scaled_execution
)
from src.utils.calculations import calculate_position_pnl, calculate_transaction_charges
from src.utils.db import (
    Position,
    PositionLeg,
    get_active_position,
    get_position_legs,
    update_position_status,
    update_leg_exit_price,
    record_failed_exit,
    save_order,
    Order,
    set_cooldown,
    clear_cooldown,
    clear_all_pending_decisions,
    # Exit in progress guard (prevents duplicate exits)
    set_exit_in_progress,
    clear_exit_in_progress,
    # Trailing state cleanup (defense-in-depth on all exit paths)
    reset_trailing_state
)
from src.utils.config import get_trading_config, load_config


# =============================================================================
# ENUMS
# =============================================================================

class ExitReason(Enum):
    """Reasons for position exit."""
    PROFIT_TARGET = "profit_target"
    STOP_LOSS = "stop_loss"
    TRAILING_STOP = "trailing_stop"  # Trailing profit triggered exit
    FRIDAY_CLOSE = "friday_close"
    EXPIRY = "expiry"
    MANUAL = "manual"
    WING_BREACH = "wing_breach"
    GAP_OPEN = "gap_open"
    CLAUDE_ADVISORY = "claude_advisory"
    VIX_BREACH = "vix_breach"  # TDD Section 5.3: VIX > 20 is HARD exit trigger

    def to_db_value(self) -> str:
        """
        Map ExitReason to valid database exit_reason value.

        The database has a CHECK constraint that only allows specific values.
        This maps newer exit reasons to compatible DB values.
        """
        # DB CHECK constraint allows: profit_target, stop_loss, friday_exit,
        # vix_spike, manual, timeout, adjustment, expiry_day
        mapping = {
            ExitReason.PROFIT_TARGET: "profit_target",
            ExitReason.STOP_LOSS: "stop_loss",
            ExitReason.TRAILING_STOP: "profit_target",  # Trailing is a type of profit exit
            ExitReason.FRIDAY_CLOSE: "friday_exit",     # DB uses friday_exit
            ExitReason.EXPIRY: "expiry_day",            # DB uses expiry_day
            ExitReason.MANUAL: "manual",
            ExitReason.WING_BREACH: "stop_loss",        # Protective exit
            ExitReason.GAP_OPEN: "stop_loss",           # Protective exit
            ExitReason.CLAUDE_ADVISORY: "manual",       # AI-assisted = manual decision
            ExitReason.VIX_BREACH: "vix_spike",         # DB uses vix_spike
        }
        return mapping.get(self, "manual")


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ExitCondition:
    """
    Exit condition check result.

    Attributes:
        should_exit: Whether position should be exited
        reason: Exit reason
        current_pnl: Current P&L
        pnl_percentage: P&L as % of margin deployed (ROM)
        details: Additional details
    """
    should_exit: bool
    reason: Optional[ExitReason] = None
    current_pnl: float = 0.0
    pnl_percentage: float = 0.0
    details: str = ""


@dataclass
class ExitResult:
    """
    Exit execution result.

    Attributes:
        success: Whether exit was successful
        reason: Exit reason
        realized_pnl: Final realized P&L
        charges: Transaction charges
        orders: Executed orders
        error: Error message if failed
    """
    success: bool
    reason: Optional[ExitReason] = None
    realized_pnl: float = 0.0
    charges: float = 0.0
    orders: Optional[IronFlyOrders] = None
    error: str = ""


# =============================================================================
# EXIT MANAGER CLASS
# =============================================================================

class ExitManager:
    """
    Manages Iron Fly position exit.

    Responsibilities:
    - Monitor exit conditions (profit target, stop loss)
    - Execute exit orders
    - Record exit to database
    - Set cooldown after exit
    - Send alerts

    Attributes:
        kite: Kite client
        telegram: Telegram client
        claude: Claude client
        config: Trading configuration
    """

    def __init__(
        self,
        kite: Optional[SNAILKiteClient] = None,
        telegram: Optional[TelegramAlerts] = None,
        claude: Optional[SNAILClaudeClient] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize exit manager.

        Args:
            kite: Kite client
            telegram: Telegram client
            claude: Claude client
            config: Configuration
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()

        self.kite = kite or get_kite_client(self.config)
        self.telegram = telegram or get_telegram()
        self.claude = claude or get_claude_client()

        logger.info("Exit manager initialized")

    # =========================================================================
    # QUOTE FETCHING
    # =========================================================================

    def get_position_quotes(self, position: Position, legs: List[PositionLeg]) -> Dict[str, Quote]:
        """
        Fetch current quotes for position legs.

        Args:
            position: Active position
            legs: Position legs

        Returns:
            Dict of quotes by leg type
        """
        instruments = []
        leg_map = {}

        for leg in legs:
            inst = f"NFO:{leg.tradingsymbol}"
            instruments.append(inst)
            leg_map[inst] = leg.leg_type

        raw_quotes = self.kite.quote(instruments)

        quotes = {}
        for inst, quote in raw_quotes.items():
            leg_type = leg_map.get(inst)
            if leg_type:
                quotes[leg_type] = quote

        return quotes

    # =========================================================================
    # CONDITION CHECKS
    # =========================================================================

    def check_exit_conditions(
        self,
        position: Optional[Position] = None,
        legs: Optional[List[PositionLeg]] = None
    ) -> ExitCondition:
        """
        Check if position should be exited.

        Checks:
        1. Profit target (50% of max profit)
        2. Stop loss (50% of max loss - advisory)
        3. Friday close (before expiry week)
        4. Expiry day (auto-exit)

        Args:
            position: Position to check (fetched if None)
            legs: Position legs (fetched if None)

        Returns:
            ExitCondition result
        """
        # Get position if not provided
        if position is None:
            position = get_active_position()
            if position is None:
                return ExitCondition(
                    should_exit=False,
                    details="No active position"
                )

        if legs is None:
            legs = get_position_legs(position.id)

        # Get current quotes
        quotes = self.get_position_quotes(position, legs)

        # Calculate current P&L
        entry_straddle = 0.0
        entry_wing = 0.0
        current_straddle_ask = 0.0
        current_wing_bid = 0.0

        for leg in legs:
            quote = quotes.get(leg.leg_type)
            if not quote:
                continue

            # Side is determined by leg_type: straddle_* = SHORT, wing_* = LONG
            if leg.leg_type.startswith('straddle'):
                entry_straddle += leg.entry_price
                current_straddle_ask += quote.ask
            else:
                entry_wing += leg.entry_price
                current_wing_bid += quote.bid

        current_pnl, pnl_pct = calculate_position_pnl(
            entry_straddle_credit=entry_straddle,
            entry_wing_debit=entry_wing,
            current_straddle_ask=current_straddle_ask,
            current_wing_bid=current_wing_bid,
            quantity=position.lot_size,
            margin_deployed=position.margin_deployed
        )

        # Check profit target (% of margin deployed - Return on Margin)
        # e.g., profit_target_pct=5 means exit when profit is 5% of margin deployed
        profit_target_pct = self.trading_config.get('exit', {}).get('profit_target_pct', 5)
        if pnl_pct >= profit_target_pct:
            return ExitCondition(
                should_exit=True,
                reason=ExitReason.PROFIT_TARGET,
                current_pnl=current_pnl,
                pnl_percentage=pnl_pct,
                details=f"Profit target reached ({pnl_pct:.1f}% ROM >= {profit_target_pct}% target)"
            )

        # Check VIX > 20 - HARD exit trigger (TDD Section 5.3)
        india_vix = self.kite.get_india_vix()
        vix_hard_exit_threshold = self.trading_config.get('exit', {}).get('vix_hard_exit', 20)

        if india_vix is not None and india_vix > vix_hard_exit_threshold:
            logger.warning(f"VIX breach detected! VIX={india_vix:.2f} > {vix_hard_exit_threshold}")
            return ExitCondition(
                should_exit=True,
                reason=ExitReason.VIX_BREACH,
                current_pnl=current_pnl,
                pnl_percentage=pnl_pct,
                details=f"VIX HARD EXIT: {india_vix:.2f} > {vix_hard_exit_threshold} threshold"
            )

        # Check stop loss (% of margin deployed - advisory only)
        # e.g., stop_loss_pct=10 means alert when loss is 10% of margin deployed
        stop_loss_pct = self.trading_config.get('exit', {}).get('stop_loss_pct', 10)
        loss_pct = abs(pnl_pct) if pnl_pct < 0 else 0

        if loss_pct >= stop_loss_pct:
            # This is advisory - Claude decides
            return ExitCondition(
                should_exit=False,  # Don't auto-exit, need Claude decision
                reason=ExitReason.STOP_LOSS,
                current_pnl=current_pnl,
                pnl_percentage=pnl_pct,
                details=f"Stop loss level reached ({loss_pct:.1f}% of margin) - Claude advisory needed"
            )

        # Check Friday close
        today = date.today()
        if today.weekday() == 4:  # Friday
            # If position expiry is next week or later
            if position.expiry_date and position.expiry_date > today + timedelta(days=2):
                current_time = datetime.now().strftime("%H:%M")
                friday_exit_time = self.trading_config.get('exit', {}).get('friday_exit_time', "15:15")

                if current_time >= friday_exit_time:
                    return ExitCondition(
                        should_exit=True,
                        reason=ExitReason.FRIDAY_CLOSE,
                        current_pnl=current_pnl,
                        pnl_percentage=pnl_pct,
                        details="Friday close before weekend"
                    )

        # Check expiry day (NIFTY typically expires on Tuesday)
        # TDD Section 6.1.3: Expiry day must exit by 3:20 PM
        if position.expiry_date and position.expiry_date == today:
            current_time = datetime.now().strftime("%H:%M")
            expiry_exit_time = self.trading_config.get('exit', {}).get('expiry_exit_time', "15:20")

            if current_time >= expiry_exit_time:
                logger.warning(f"EXPIRY DAY EXIT: Today is expiry ({position.expiry_date}), time={current_time}")
                return ExitCondition(
                    should_exit=True,
                    reason=ExitReason.EXPIRY,
                    current_pnl=current_pnl,
                    pnl_percentage=pnl_pct,
                    details=f"Expiry day auto-exit (expiry={position.expiry_date}, time={current_time})"
                )

        # Check if expiry is tomorrow (DTE = 1) - alert but don't auto-exit
        if position.expiry_date and (position.expiry_date - today).days == 1:
            logger.info(f"Expiry tomorrow ({position.expiry_date}). DTE=1, monitoring closely.")

        # No exit condition met
        return ExitCondition(
            should_exit=False,
            current_pnl=current_pnl,
            pnl_percentage=pnl_pct,
            details="No exit conditions met"
        )

    # =========================================================================
    # EXIT EXECUTION
    # =========================================================================

    def execute_exit(
        self,
        reason: ExitReason,
        position: Optional[Position] = None,
        require_confirmation: bool = False
    ) -> ExitResult:
        """
        Execute position exit.

        Args:
            reason: Reason for exit
            position: Position to exit (fetched if None)
            require_confirmation: Whether to require Telegram confirmation

        Returns:
            ExitResult with execution details
        """
        # Get position
        if position is None:
            position = get_active_position()
            if position is None:
                return ExitResult(
                    success=False,
                    error="No active position to exit"
                )

        # =====================================================================
        # ENTRY IN PROGRESS GUARD
        # =====================================================================
        # Block exits while entry is executing to prevent race conditions
        from src.utils.config import is_entry_in_progress
        if is_entry_in_progress():
            logger.warning(
                f"EXIT BLOCKED: Entry in progress. Ignoring {reason.value} trigger."
            )
            return ExitResult(
                success=False,
                error="Entry in progress - exit blocked"
            )

        legs = get_position_legs(position.id)
        if not legs:
            return ExitResult(
                success=False,
                error="No legs found for position"
            )

        # =====================================================================
        # EXIT IN PROGRESS GUARD (CRITICAL BUG FIX)
        # =====================================================================
        # This guard prevents the catastrophic bug where trailing stop or other
        # exit triggers keep firing repeatedly while an exit is in progress.
        # The atomic set_exit_in_progress() returns False if already in progress.
        if not set_exit_in_progress(position.id):
            logger.warning(
                f"EXIT BLOCKED: Exit already in progress or position not active for {position.id}. "
                f"Ignoring duplicate {reason.value} trigger."
            )
            return ExitResult(
                success=False,
                error="Exit already in progress or position not active - duplicate trigger blocked"
            )

        # Re-verify position is still active after acquiring lock (defense-in-depth)
        # The position object passed in may be stale - re-fetch from DB to confirm
        fresh_position = get_active_position()
        if fresh_position is None or fresh_position.id != position.id:
            logger.warning(
                f"EXIT BLOCKED: Position {position.id} is no longer active after lock acquired. "
                f"Stale position object detected for {reason.value} trigger."
            )
            clear_exit_in_progress(position.id)
            return ExitResult(
                success=False,
                error="Position no longer active - stale object detected"
            )

        logger.info(f"Executing exit for position {position.id}, reason: {reason.value}")

        # Set entry cooldown EARLY (before placing orders) to prevent re-entry race
        # If exit fails, cooldown is harmless (blocks entry for a few hours, not days)
        try:
            today = datetime.now()
            if today.weekday() != 4:  # Skip on Friday (weekend is buffer)
                cooldown_hours = self.trading_config.get('exit', {}).get('cooldown_hours', 24)
                set_cooldown('entry', cooldown_hours * 3600)
                logger.info(f"Entry cooldown set early ({cooldown_hours}h) before exit execution")
        except Exception as e:
            logger.warning(f"Failed to set early cooldown (non-fatal): {e}")

        # Notify user that exit is starting
        # Wrapped in try/except: Telegram failure must NEVER block exit execution
        try:
            reason_emoji = {
                ExitReason.PROFIT_TARGET: "🎯",
                ExitReason.STOP_LOSS: "🛑",
                ExitReason.TRAILING_STOP: "📉",
                ExitReason.MANUAL: "👤",
                ExitReason.FRIDAY_CLOSE: "📅",
                ExitReason.VIX_BREACH: "⚡",
                ExitReason.WING_BREACH: "🚨",
                ExitReason.EXPIRY: "⏰",
                ExitReason.GAP_OPEN: "📉",
                ExitReason.CLAUDE_ADVISORY: "🤖"
            }
            emoji = reason_emoji.get(reason, "🔄")
            self.telegram.send(
                f"{emoji} *Exit Started*\n\n"
                f"Reason: {reason.value.replace('_', ' ').title()}\n"
                f"Placing LIMIT orders for all 4 legs...\n\n"
                f"_Will notify on completion or if action needed._"
            )
        except Exception as e:
            logger.warning(f"Pre-exit Telegram notification failed (non-blocking): {e}")

        try:
            # Get current quotes
            quotes = self.get_position_quotes(position, legs)

            # Build symbols dict
            symbols = {leg.leg_type: leg.tradingsymbol for leg in legs}

            # Determine if urgent exit (VIX breach, expiry, etc.)
            urgent_reasons = [ExitReason.VIX_BREACH, ExitReason.EXPIRY, ExitReason.WING_BREACH]
            is_urgent = reason in urgent_reasons

            # Check if we need scaled execution (quantity > freeze limit)
            from src.utils.config import is_paper_trading

            if should_use_scaled_execution(position.lot_size, self.config):
                # Use scaled execution for large orders
                logger.info(f"Using scaled exit for {position.lot_size} contracts")

                scaled_executor = ScaledOrderExecutor(
                    kite=self.kite,
                    config=self.config,
                    paper_trading=is_paper_trading()
                )

                scaled_result = scaled_executor.execute_exit(
                    symbols=symbols,
                    total_quantity=position.lot_size,
                    quotes=quotes,
                    instrument="NIFTY",
                    urgent=is_urgent
                )

                if not scaled_result.success:
                    error_msg = "; ".join(scaled_result.errors) if scaled_result.errors else "Scaled exit failed"
                    # For exit, even partial failure is critical
                    logger.critical(f"Scaled exit failed: {error_msg}")
                    return ExitResult(success=False, error=error_msg)

                # Check if full exit achieved
                if scaled_result.total_filled_qty != position.lot_size:
                    remaining = position.lot_size - scaled_result.total_filled_qty
                    logger.critical(
                        f"INCOMPLETE EXIT: {remaining} contracts still open! "
                        f"Manual intervention required!"
                    )
                    self.telegram.send(
                        f"🚨 *CRITICAL: Incomplete Exit!*\n\n"
                        f"Only {scaled_result.total_filled_qty}/{position.lot_size} closed.\n"
                        f"*{remaining} contracts still open!*\n\n"
                        f"⚠️ Manual exit required immediately!"
                    )

                # Convert scaled result to IronFlyOrders for compatibility
                orders = self._convert_scaled_exit_result_to_orders(
                    scaled_result=scaled_result,
                    symbols=symbols,
                    quantity=scaled_result.total_filled_qty
                )

                # Log warnings from scaled execution
                for warning in scaled_result.warnings:
                    logger.warning(f"Scaled exit warning: {warning}")

            else:
                # Use standard single-batch execution
                slippage_ticks = self.trading_config.get('exit', {}).get('slippage_ticks', 2)

                orders = execute_iron_fly_exit(
                    kite=self.kite,
                    symbols=symbols,
                    quotes=quotes,
                    quantity=position.lot_size,
                    slippage_ticks=slippage_ticks
                )

            # Position Verification after Exit (TDD Section 6.2)
            # Verify exit by checking ORDER completion status, not net positions
            # (Net positions may include other manual trades in same symbols)
            logger.info("Verifying all 4 exit orders completed...")
            exit_verified = True
            verification_issues = []

            # Verify each order completed successfully
            exit_order_list = [
                ('straddle_ce', orders.straddle_ce),
                ('straddle_pe', orders.straddle_pe),
                ('wing_ce', orders.wing_ce),
                ('wing_pe', orders.wing_pe),
            ]

            for leg_type, order in exit_order_list:
                if order.status != 'COMPLETE':
                    exit_verified = False
                    verification_issues.append(
                        f"{leg_type}: Order {order.order_id} status={order.status}"
                    )
                elif order.quantity != position.lot_size:
                    exit_verified = False
                    verification_issues.append(
                        f"{leg_type}: Partial fill {order.quantity}/{position.lot_size}"
                    )

            # Verify we have all 4 legs
            if len(symbols) != 4:
                exit_verified = False
                verification_issues.append(f"Expected 4 legs, found {len(symbols)}")

            if not exit_verified:
                issue_text = "; ".join(verification_issues)
                logger.error(f"EXIT VERIFICATION FAILED: {issue_text}")

                # ISSUE-008: Record failed exit to database
                # Determine which legs closed vs failed based on verification
                legs_closed = []
                legs_failed = {}
                for leg_type in symbols.keys():
                    is_failed = any(leg_type in issue for issue in verification_issues)
                    if is_failed:
                        legs_failed[leg_type] = "Position still open after exit order"
                    else:
                        legs_closed.append(leg_type)

                record_failed_exit(
                    position_id=position.id,
                    legs_closed=legs_closed,
                    legs_failed=legs_failed,
                    error=issue_text,
                    partial=len(legs_closed) > 0
                )

                self.telegram.send(
                    f"[CRITICAL] *Exit Verification Failed!*\n\n"
                    f"Exit orders executed but positions may remain:\n"
                    f"{''.join(f'- {i}' + chr(10) for i in verification_issues)}\n"
                    f"[!] *Manual intervention required!*\n\n"
                    f"Check Kite positions immediately.\n"
                    f"Position marked as '{('partial_exit' if legs_closed else 'exit_failed')}' in DB."
                )

                # Run reconciliation after exit failure
                try:
                    from src.utils.reconciliation import reconcile_positions
                    reconcile_positions(kite=self.kite, telegram=self.telegram)
                except Exception as recon_err:
                    logger.error(f"Post-exit-failure reconciliation error: {recon_err}")

                # Return partial success - orders placed but not verified
                return ExitResult(
                    success=False,
                    error=f"Exit verification failed: {issue_text}. Manual check required.",
                    orders=orders
                )

            # Calculate realized P&L (only if exit verified)
            entry_credit = position.entry_premium
            exit_debit = (
                (orders.straddle_ce.fill_price + orders.straddle_pe.fill_price) -
                (orders.wing_ce.fill_price + orders.wing_pe.fill_price)
            ) * position.lot_size

            realized_pnl = entry_credit - exit_debit

            # Calculate charges (4 orders: buy back straddle CE+PE, sell wings CE+PE)
            sell_value = (orders.wing_ce.fill_price + orders.wing_pe.fill_price) * position.lot_size
            buy_value = (orders.straddle_ce.fill_price + orders.straddle_pe.fill_price) * position.lot_size
            charges = calculate_transaction_charges(buy_value, sell_value, num_orders=4)

            # Update position in database
            # CRITICAL: Orders are already filled at this point. If DB write fails,
            # position is closed on exchange but still 'active' in DB.
            # We must record this failure and alert the user.
            try:
                self._record_exit(
                    position=position,
                    legs=legs,
                    orders=orders,
                    reason=reason,
                    realized_pnl=realized_pnl
                )
            except Exception as record_err:
                logger.critical(
                    f"CRITICAL: _record_exit() failed AFTER orders filled! "
                    f"Position {position.id} is CLOSED on exchange but DB not updated. "
                    f"Error: {record_err}"
                )
                # Record as failed exit so position isn't stuck as 'active'
                try:
                    record_failed_exit(
                        position_id=position.id,
                        legs_closed=[leg.leg_type for leg in legs],
                        legs_failed={},
                        error=f"DB record_exit failed: {record_err}",
                        partial=False
                    )
                except Exception:
                    logger.critical(f"record_failed_exit also failed for position {position.id}")
                # Alert user - this is a critical situation
                try:
                    self.telegram.send(
                        f"🚨 *CRITICAL: Exit DB Recording Failed!*\n\n"
                        f"Position {position.id} is CLOSED on exchange\n"
                        f"but database was NOT updated.\n\n"
                        f"Error: {str(record_err)[:200]}\n\n"
                        f"⚠️ *Manual DB correction required!*"
                    )
                except Exception:
                    pass  # Don't let Telegram failure compound the issue
                # Still return success - the EXIT itself worked, only DB recording failed
                return ExitResult(
                    success=True,
                    reason=reason,
                    realized_pnl=realized_pnl,
                    orders=orders,
                    error=f"Exit orders filled but DB recording failed: {record_err}"
                )

            # Clean up trailing state (defense-in-depth)
            # Ensures ALL exit paths clear trailing_active, peak, floor
            # even if the caller forgets to call reset_trailing_state()
            reset_trailing_state(position.id)

            # Clear garbage-book confirmation counters for this position so a
            # stale streak can't influence any future position reusing the id.
            try:
                from src.utils.quote_reliability import confirm_reset_position
                confirm_reset_position(position.id)
            except Exception as e:
                logger.debug(f"Confirm-counter cleanup skipped (non-fatal): {e}")

            # Cooldown already set early (before exit orders placed)
            # No need to set again here - early set prevents re-entry race

            # Clear hold cooldowns (no longer relevant after position exit)
            for cooldown_type in ['wing_hold', 'stop_loss_hold', 'vix_hold']:
                clear_cooldown(cooldown_type)
            logger.info("Cleared hold cooldowns after position exit")

            # Clear pending decisions (user can't respond to alerts for closed position)
            clear_all_pending_decisions(position.id)
            logger.info("Cleared pending decisions after position exit")

            # Send exit alert
            self._send_exit_alert(
                position=position,
                reason=reason,
                realized_pnl=realized_pnl,
                charges=charges.total
            )

            logger.info(f"Exit complete! Realized P&L: {realized_pnl:.2f}")

            return ExitResult(
                success=True,
                reason=reason,
                realized_pnl=realized_pnl,
                charges=charges.total,
                orders=orders
            )

        except OrderExecutionError as e:
            logger.error(f"Exit order execution failed: {e}")
            self.telegram.send_error_alert(
                error=str(e),
                module="exit_manager",
                function="execute_exit"
            )
            return ExitResult(success=False, error=str(e))

        except Exception as e:
            logger.error(f"Exit failed: {e}")
            import traceback
            traceback.print_exc()
            return ExitResult(success=False, error=str(e))

        finally:
            # CRITICAL: Always clear exit_in_progress flag, regardless of success/failure
            # This ensures the position isn't permanently locked if exit fails
            clear_exit_in_progress(position.id)
            logger.debug(f"Exit in progress flag cleared for position {position.id}")

    def execute_manual_exit(self) -> ExitResult:
        """Execute manual exit requested by user."""
        return self.execute_exit(ExitReason.MANUAL)

    def execute_stop_loss_exit(
        self,
        position: Position,
        require_claude: bool = True
    ) -> ExitResult:
        """
        Execute stop loss exit with optional Claude advisory.

        Args:
            position: Position to exit
            require_claude: Whether to get Claude decision first

        Returns:
            ExitResult
        """
        if require_claude:
            # Get Claude advisory
            legs = get_position_legs(position.id)
            quotes = self.get_position_quotes(position, legs)

            # Calculate current P&L for context
            # Side is determined by leg_type: straddle_* = SHORT, wing_* = LONG
            entry_straddle = sum(leg.entry_price for leg in legs if leg.leg_type.startswith('straddle'))
            entry_wing = sum(leg.entry_price for leg in legs if leg.leg_type.startswith('wing'))
            current_straddle = sum(quotes[leg.leg_type].ask for leg in legs if leg.leg_type.startswith('straddle'))
            current_wing = sum(quotes[leg.leg_type].bid for leg in legs if leg.leg_type.startswith('wing'))

            current_pnl, pnl_pct = calculate_position_pnl(
                entry_straddle, entry_wing, current_straddle, current_wing,
                position.lot_size, position.margin_deployed
            )

            # Get NIFTY and VIX
            nifty_spot = self.kite.get_nifty_spot()
            india_vix = self.kite.get_india_vix()

            context = MarketContext(
                nifty_spot=nifty_spot,
                india_vix=india_vix,
                atm_strike=position.atm_strike,
                wing_distance=position.wing_distance,
                position_pnl=current_pnl,
                dte=(position.expiry_date - date.today()).days if position.expiry_date else 0
            )

            response = self.claude.get_stop_loss_advisory(context)

            if response.decision == ClaudeDecision.HOLD:
                logger.info("Claude advises HOLD at stop loss level")
                self.telegram.send(
                    f"⚠️ Stop loss level reached but Claude advises HOLD:\n{response.reasoning[:200]}..."
                )
                return ExitResult(
                    success=False,
                    reason=ExitReason.STOP_LOSS,
                    error="Claude advises holding position"
                )

        return self.execute_exit(ExitReason.STOP_LOSS, position)

    def _convert_scaled_exit_result_to_orders(
        self,
        scaled_result: ScaledExecutionResult,
        symbols: Dict[str, str],
        quantity: int
    ) -> IronFlyOrders:
        """
        Convert ScaledExecutionResult to IronFlyOrders for compatibility.

        EXIT transaction types are REVERSED from entry:
        - Straddle: BUY (closing short)
        - Wings: SELL (closing long)

        Args:
            scaled_result: Result from ScaledOrderExecutor.execute_exit()
            symbols: Mapping of leg_type to tradingsymbol
            quantity: Actual closed quantity per leg

        Returns:
            IronFlyOrders compatible with existing recording flow
        """
        leg_fills = scaled_result.leg_fills

        def create_executed_order(leg_type: str, transaction_type: str) -> ExecutedOrder:
            """Create ExecutedOrder from scaled result data."""
            fill_price = leg_fills.get(leg_type, 0.0)
            return ExecutedOrder(
                order_id=f"SCALED_EXIT_{leg_type}_{datetime.now().strftime('%Y%m%d%H%M%S')}",
                tradingsymbol=symbols[leg_type],
                transaction_type=transaction_type,
                quantity=quantity,
                order_price=fill_price,
                fill_price=fill_price,
                slippage=0.0,
                status='COMPLETE'
            )

        # EXIT: Transaction types are REVERSED from entry
        return IronFlyOrders(
            straddle_ce=create_executed_order('straddle_ce', 'BUY'),   # Close short
            straddle_pe=create_executed_order('straddle_pe', 'BUY'),   # Close short
            wing_ce=create_executed_order('wing_ce', 'SELL'),          # Close long
            wing_pe=create_executed_order('wing_pe', 'SELL')           # Close long
        )

    # =========================================================================
    # DATABASE RECORDING
    # =========================================================================

    def _record_exit(
        self,
        position: Position,
        legs: List[PositionLeg],
        orders: IronFlyOrders,
        reason: ExitReason,
        realized_pnl: float
    ) -> None:
        """Record exit to database."""
        # Update position status (schema expects lowercase: 'closed')
        # Use to_db_value() to map enum to valid DB CHECK constraint value
        update_position_status(
            position_id=position.id,
            status='closed',
            exit_time=datetime.now(),
            exit_reason=reason.to_db_value(),
            realized_pnl=realized_pnl
        )

        # Record exit orders
        exit_orders = [
            ('straddle_ce', orders.straddle_ce),
            ('straddle_pe', orders.straddle_pe),
            ('wing_ce', orders.wing_ce),
            ('wing_pe', orders.wing_pe),
        ]

        for leg_type, order in exit_orders:
            # Find matching leg
            matching_leg = next((pos_leg for pos_leg in legs if pos_leg.leg_type == leg_type), None)
            if matching_leg:
                order_record = Order(
                    id=0,
                    position_id=position.id,
                    kite_order_id=order.order_id,
                    order_type='LIMIT',
                    transaction_type=order.transaction_type,
                    tradingsymbol=order.tradingsymbol,
                    strike=matching_leg.strike,
                    option_type=matching_leg.option_type,
                    price=order.order_price,
                    quantity=order.quantity,
                    status=order.status,
                    fill_price=order.fill_price,
                    slippage=order.slippage
                )
                save_order(order_record)

                # Record exit price on the leg itself
                if order.fill_price is not None:
                    update_leg_exit_price(matching_leg.id, order.fill_price)

    def _send_exit_alert(
        self,
        position: Position,
        reason: ExitReason,
        realized_pnl: float,
        charges: float
    ) -> None:
        """Send exit alert via Telegram."""
        net_pnl = realized_pnl - charges
        exit_time = datetime.now()

        # Calculate P&L percentage as ROM (Return on Margin)
        # This is consistent with how all internal logic (trailing, TP, SL) calculates %
        # BUG FIX: was using max_profit which showed inflated % (e.g. 7.8% instead of 1.5%)
        if position.margin_deployed and position.margin_deployed > 0:
            pnl_percent = (net_pnl / position.margin_deployed) * 100
        else:
            pnl_percent = 0

        self.telegram.send_exit_alert(
            exit_reason=reason.to_db_value(),
            net_pnl=net_pnl,
            pnl_percent=pnl_percent,
            entry_time=position.entry_time or exit_time,
            exit_time=exit_time
        )


# =============================================================================
# SINGLETON INSTANCE
# =============================================================================

_exit_manager: Optional[ExitManager] = None


def get_exit_manager(config: Optional[Dict] = None) -> ExitManager:
    """Get or create singleton exit manager."""
    global _exit_manager

    if _exit_manager is None:
        _exit_manager = ExitManager(config=config)

    return _exit_manager


def reset_exit_manager() -> None:
    """Reset singleton exit manager."""
    global _exit_manager
    _exit_manager = None


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    import sys
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Exit Manager Test")
    print("=" * 60)

    try:
        manager = ExitManager()

        # Check for active position
        print("\n[1] Checking for active position...")
        position = get_active_position()

        if position:
            print(f"    Found position ID: {position.id}")
            print(f"    Status: {position.status}")
            print(f"    Entry premium: {position.entry_premium:.2f}")

            # Check exit conditions
            print("\n[2] Checking exit conditions...")
            condition = manager.check_exit_conditions(position)
            print(f"    Should exit: {condition.should_exit}")
            print(f"    Reason: {condition.reason}")
            print(f"    Current P&L: {condition.current_pnl:.2f}")
            print(f"    P&L %: {condition.pnl_percentage:.1f}%")
            print(f"    Details: {condition.details}")

        else:
            print("    No active position found")

        print("\n" + "=" * 60)
        print("Exit manager test complete!")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

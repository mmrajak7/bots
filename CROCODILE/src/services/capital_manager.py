"""Capital Management and Position Sizing Service

OPTIMIZED VERSION - Implements intelligent compounding position sizing

Key Optimizations:
1. Position sizing: available_margin / remaining_slots (not fixed 20%)
2. Ensures full capital deployment across max_positions slots
3. Natural compounding: Zerodha API includes realized P&L in net margin

With max_positions=5:
- 0 open: 20% per position (5 slots available)
- 1 open: 25% per position (4 slots available)
- 2 open: 33% per position (3 slots available)
- 3 open: 50% per position (2 slots available)
- 4 open: 100% per position (1 slot available)

Result: Full capital always deployed, exponential growth via compounding
"""

from datetime import date, datetime
from typing import Dict, Tuple, Optional
from math import floor
from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy import func

from src.models.database import CapitalLedger, OpenPosition, OpenOrder, ClosedPosition, OrderStatus, PositionStatus, get_session
from src.utils.config_manager import config
from src.utils.price_rounder import round_price
from src.utils.timezone_helper import ist_now_naive
from src.api.kite_trade_client import KiteTradeClient


class CapitalManager:
    """
    Manages capital allocation, position sizing, and margin tracking

    Implements OPTIMIZED position sizing:
    - Intelligent slot-based sizing: available / remaining_slots
    - Test mode (1 qty) vs production mode
    - Margin checking and alerts
    - Daily capital ledger updates
    - Drawdown calculation with automatic position size reduction

    Compounding Logic:
    - Zerodha net margin automatically includes all realized P&L
    - Position sizing based on available margin ensures profit reinvestment
    - No manual P&L tracking needed - API does it naturally!
    """

    def __init__(self):
        """Initialize capital manager"""
        self.kite_client = KiteTradeClient()
        self.test_mode = config.is_test_mode()
        self.position_size_pct = config.get('trading.position_size_pct', 20) / 100  # Convert to decimal
        self.min_position_value = config.get('trading.min_position_value', 10000)
        self.min_margin_threshold = config.get('margin.minimum_required', 50000)
        self.max_positions = config.get('risk_management.max_positions', None)  # None = unlimited
        self.max_pending_orders = config.get('risk_management.max_pending_orders', None)  # Separate pending limit
        self.enable_slot_based_sizing = config.get('trading.enable_slot_based_sizing', True)

        # Bot instance identifier
        self.bot_instance_id = config.get_bot_instance_id()

        # Capital allocation configuration (NEW - for multi-bot architecture)
        cap_alloc = config.get('trading.capital_allocation', {})
        self.allocated_capital_amount = cap_alloc.get('allocated_capital_amount')
        self.allocated_capital_percent = cap_alloc.get('allocated_capital_percent')
        self.reserve_buffer_percent = cap_alloc.get('reserve_buffer_percent', 0) / 100.0

        logger.info(
            f"Capital Manager initialized - "
            f"Bot ID: {self.bot_instance_id}, "
            f"Test Mode: {self.test_mode}, "
            f"Max Positions: {self.max_positions if self.max_positions else 'Unlimited'}, "
            f"Max Pending Orders: {self.max_pending_orders if self.max_pending_orders else 'Unlimited'}, "
            f"Slot-based Sizing: {self.enable_slot_based_sizing}"
        )

        if self.allocated_capital_amount or self.allocated_capital_percent:
            logger.info(
                f"Capital Allocation Configured - "
                f"Amount: {f'Rs.{self.allocated_capital_amount:,.0f}' if self.allocated_capital_amount else 'N/A'}, "
                f"Percent: {f'{self.allocated_capital_percent}%' if self.allocated_capital_percent else 'N/A'}, "
                f"Buffer: {self.reserve_buffer_percent * 100:.1f}%"
            )
        else:
            logger.info("No capital allocation limits - using full account margin")

    def fetch_margin_from_zerodha(self, session: Optional[Session] = None) -> Dict[str, float]:
        """
        Fetch margin from Zerodha and apply capital allocation limits

        Returns:
            Dict with margin details:
            - net: Total account margin
            - available: Available for trading (respecting allocation limits)
            - utilised: Currently deployed by THIS BOT
            - allocated: Allocated capital for this bot (if configured)
            - allocation_method: 'amount'/'percent'/None
            - reserve_buffer: Reserve buffer amount
            - bot_instance_id: Bot identifier
        """
        try:
            response = self.kite_client.get_margins()

            if not response or 'data' not in response:
                raise Exception("Invalid margin API response")

            equity_margin = response['data'].get('equity', {})
            net_margin = equity_margin.get('net', 0.0)
            zerodha_available = equity_margin.get('available', {}).get('live_balance', 0.0)
            zerodha_utilised = equity_margin.get('utilised', {}).get('debits', 0.0)

            # Apply capital allocation limits
            if self.allocated_capital_amount is not None:
                # Option 1: Fixed amount allocation (PRIORITY)
                allocated_capital = self.allocated_capital_amount
                allocation_method = 'amount'
                logger.info(f"Using FIXED capital allocation: Rs.{allocated_capital:,.2f}")

            elif self.allocated_capital_percent is not None:
                # Option 2: Percentage allocation
                allocated_capital = net_margin * (self.allocated_capital_percent / 100.0)
                allocation_method = 'percent'
                logger.info(
                    f"Using PERCENTAGE capital allocation: "
                    f"{self.allocated_capital_percent}% of Rs.{net_margin:,.2f} = Rs.{allocated_capital:,.2f}"
                )

            else:
                # No allocation limit - use full account margin (backward compatible)
                allocated_capital = None
                allocation_method = None
                logger.info("No capital allocation limit - using full account margin")

            # Calculate available margin for this bot
            if allocated_capital is not None:
                # Get currently deployed capital by THIS BOT from database
                deployed_by_bot = self._get_deployed_capital_by_bot(session)

                # Apply reserve buffer
                reserve_buffer = allocated_capital * self.reserve_buffer_percent
                effective_allocation = allocated_capital - reserve_buffer

                # Available = Allocated - Deployed - Buffer
                available_for_bot = max(0, effective_allocation - deployed_by_bot)

                logger.info(
                    f"Capital Allocation Breakdown:\n"
                    f"  Total Account Margin: Rs.{net_margin:,.2f}\n"
                    f"  Allocated to Bot ({self.bot_instance_id}): Rs.{allocated_capital:,.2f}\n"
                    f"  Reserve Buffer ({self.reserve_buffer_percent*100:.1f}%): Rs.{reserve_buffer:,.2f}\n"
                    f"  Effective Allocation: Rs.{effective_allocation:,.2f}\n"
                    f"  Deployed by Bot: Rs.{deployed_by_bot:,.2f}\n"
                    f"  Available for Bot: Rs.{available_for_bot:,.2f}"
                )

                # Return constrained margin
                return {
                    'net': net_margin,
                    'available': available_for_bot,  # LIMITED to bot's allocation!
                    'utilised': deployed_by_bot,  # Bot's deployed capital only
                    'allocated': allocated_capital,
                    'allocation_method': allocation_method,
                    'reserve_buffer': reserve_buffer,
                    'bot_instance_id': self.bot_instance_id,
                    # Keep Zerodha values for reference
                    'zerodha_available': zerodha_available,
                    'zerodha_utilised': zerodha_utilised
                }
            else:
                # No allocation limit - return full account margin
                logger.info(f"Fetched margin from Zerodha: Net=Rs.{net_margin:.2f}, Available=Rs.{zerodha_available:.2f}")
                return {
                    'net': net_margin,
                    'available': zerodha_available,
                    'utilised': zerodha_utilised,
                    'allocated': None,
                    'allocation_method': None,
                    'reserve_buffer': 0,
                    'bot_instance_id': self.bot_instance_id,
                    'zerodha_available': zerodha_available,
                    'zerodha_utilised': zerodha_utilised
                }

        except Exception as e:
            logger.error(f"Failed to fetch margin from Zerodha: {e}")
            raise

    def _get_deployed_capital_by_bot(self, session: Optional[Session] = None) -> float:
        """
        Get total capital deployed by THIS BOT INSTANCE

        Returns sum of:
        - Capital in OPEN positions (by this bot)
        - Capital reserved in PENDING orders (by this bot)

        Args:
            session: Database session

        Returns:
            Total deployed capital for this bot
        """
        from sqlalchemy import func

        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            # Sum capital in open positions (filtered by bot_instance_id)
            deployed_in_positions = session.query(
                func.sum(OpenPosition.capital_deployed)
            ).filter_by(
                status=PositionStatus.OPEN,
                bot_instance_id=self.bot_instance_id
            ).scalar() or 0.0

            # Sum capital in pending orders (filtered by bot_instance_id)
            deployed_in_orders = session.query(
                func.sum(OpenOrder.capital_deployed)
            ).filter(
                OpenOrder.status.in_([OrderStatus.PENDING]),
                OpenOrder.bot_instance_id == self.bot_instance_id
            ).scalar() or 0.0

            total_deployed = deployed_in_positions + deployed_in_orders

            logger.debug(
                f"Deployed Capital (Bot: {self.bot_instance_id}): "
                f"Positions=Rs.{deployed_in_positions:,.2f}, "
                f"Orders=Rs.{deployed_in_orders:,.2f}, "
                f"Total=Rs.{total_deployed:,.2f}"
            )

            return total_deployed

        finally:
            if close_session:
                session.close()

    def check_margin_threshold(self, available_margin: float) -> Tuple[bool, Optional[str]]:
        """
        Check if available margin is above minimum threshold

        Args:
            available_margin: Available margin amount

        Returns:
            (is_sufficient, alert_message)
        """
        if available_margin < self.min_margin_threshold:
            alert_msg = (
                f"⚠️ **LOW MARGIN ALERT**\n"
                f"Available: Rs.{available_margin:,.2f}\n"
                f"Threshold: Rs.{self.min_margin_threshold:,.2f}\n"
                f"⚡ Action: Add funds before market open"
            )
            logger.warning(f"Margin below threshold: Rs.{available_margin:.2f} < Rs.{self.min_margin_threshold:.2f}")
            return False, alert_msg

        return True, None

    def get_drawdown_adjusted_position_size_pct(self, session: Session) -> Tuple[float, str]:
        """
        Get position size percentage adjusted for current drawdown

        Risk Management Rules:
        - Normal (DD < 5%): 20% position sizing (default)
        - Caution (5% <= DD < 10%): 10% position sizing (REDUCED)
        - Critical (DD >= 10%): 5% position sizing (HEAVILY REDUCED)

        Args:
            session: Database session

        Returns:
            (adjusted_position_size_pct, reason)
        """
        # Get current drawdown status
        dd_status = self.get_drawdown_status(session)
        dd_pct = dd_status['drawdown_pct']

        # Get thresholds from config
        dd_caution = config.get('risk_management.drawdown_caution', 5)
        dd_critical = config.get('risk_management.drawdown_critical', 10)

        # Default position sizing
        base_position_pct = self.position_size_pct  # 20% (0.20)

        # Apply drawdown-based reduction
        if dd_pct >= dd_critical:
            # CRITICAL: Reduce to 5%
            adjusted_pct = base_position_pct * 0.25  # 20% * 0.25 = 5%
            reason = f"CRITICAL drawdown ({dd_pct:.2f}%) - Position sizing reduced to 5%"
            logger.warning(reason)
            return adjusted_pct, reason

        elif dd_pct >= dd_caution:
            # CAUTION: Reduce to 10%
            adjusted_pct = base_position_pct * 0.50  # 20% * 0.50 = 10%
            reason = f"CAUTION drawdown ({dd_pct:.2f}%) - Position sizing reduced to 10%"
            logger.warning(reason)
            return adjusted_pct, reason

        else:
            # HEALTHY: Normal sizing
            return base_position_pct, "Normal position sizing (20%)"

    def calculate_position_size(
        self,
        entry_price: float,
        available_margin: float,
        session: Optional[Session] = None
    ) -> Tuple[int, float, Optional[str]]:
        """
        Calculate position size using OPTIMIZED slot-based sizing

        OPTIMIZATION: Instead of fixed 20%, use available_margin / remaining_slots
        This ensures full capital deployment and better compounding

        Args:
            entry_price: Entry price per share (rounded to 0.05)
            available_margin: Available margin for this position
            session: Database session (needed for drawdown check and slot count)

        Returns:
            (quantity, capital_deployed, drawdown_alert_message)

        Example with max_positions=5:
        - 0 open: position_size = available / 5 = 20%
        - 1 open: position_size = available / 4 = 25%
        - 2 open: position_size = available / 3 = 33%
        - 3 open: position_size = available / 2 = 50%
        - 4 open: position_size = available / 1 = 100%
        """
        # Round entry price to tick size
        entry_price_rounded = round_price(entry_price)

        # TEST MODE: Always 1 quantity
        if self.test_mode:
            quantity = 1
            capital_deployed = entry_price_rounded * quantity
            logger.info(f"TEST MODE: Quantity=1, Capital=Rs.{capital_deployed:.2f}")
            return quantity, capital_deployed, None

        # PRODUCTION MODE: Get drawdown-adjusted position sizing
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            # Get drawdown-adjusted position size percentage (for drawdown reduction)
            adjusted_position_pct, dd_reason = self.get_drawdown_adjusted_position_size_pct(session)

            # OPTIMIZATION: Calculate position size using slot-based logic
            if self.enable_slot_based_sizing and self.max_positions is not None:
                # Get current position count (for remaining slots calculation)
                current_positions, pending_orders = self.get_current_position_count(session)

                # Calculate remaining slots based on OPEN POSITIONS only
                # Pending orders may not fill, so we don't count them for slot-based sizing
                # This ensures better capital utilization when pending orders exist
                remaining_slots = self.max_positions - current_positions

                if remaining_slots <= 0:
                    logger.error("No remaining slots - should not reach here (limit checked earlier)")
                    return 0, 0.0, None

                # OPTIMIZED FORMULA: available_margin / remaining_slots
                # This ensures full capital deployment across all slots
                position_value_optimal = available_margin / remaining_slots

                # Apply drawdown adjustment if needed
                position_value = position_value_optimal * (adjusted_position_pct / self.position_size_pct)

                logger.info(
                    f"Slot-based sizing: {remaining_slots} slots remaining (Open={current_positions}, Pending={pending_orders}), "
                    f"Optimal=Rs.{position_value_optimal:.2f} per slot, "
                    f"After DD adjustment ({adjusted_position_pct*100:.0f}%): Rs.{position_value:.2f}"
                )

            else:
                # LEGACY: Fixed percentage sizing (fallback)
                position_value = available_margin * adjusted_position_pct
                logger.info(f"Fixed % sizing: {adjusted_position_pct*100:.0f}% of Rs.{available_margin:.2f}")

            # Calculate quantity (always floor)
            quantity = floor(position_value / entry_price_rounded)

            # Ensure minimum position value
            capital_deployed = quantity * entry_price_rounded

            # Prepare alert message if position sizing was reduced due to drawdown
            dd_alert_message = None
            if adjusted_position_pct < self.position_size_pct:
                dd_alert_message = (
                    f"📉 *Position Sizing Reduced*\n"
                    f"{dd_reason}\n"
                    f"Original: {self.position_size_pct * 100:.0f}% → Adjusted: {adjusted_position_pct * 100:.0f}%"
                )

            if capital_deployed < self.min_position_value:
                logger.warning(
                    f"Position value Rs.{capital_deployed:.2f} below minimum Rs.{self.min_position_value:.2f}"
                )
                # Try with minimum
                quantity = floor(self.min_position_value / entry_price_rounded)
                capital_deployed = quantity * entry_price_rounded

                if capital_deployed > available_margin:
                    logger.error(
                        f"Insufficient margin: need Rs.{capital_deployed:.2f}, have Rs.{available_margin:.2f}"
                    )
                    return 0, 0.0, dd_alert_message

            logger.info(
                f"Position size calculated: Qty={quantity}, "
                f"Price=Rs.{entry_price_rounded:.2f}, "
                f"Capital=Rs.{capital_deployed:.2f} ({(capital_deployed/available_margin)*100:.1f}% of available) "
                f"[{dd_reason}]"
            )

            return quantity, capital_deployed, dd_alert_message

        except Exception as e:
            logger.error(f"Error calculating position size: {e}")
            if close_session:
                session.rollback()
            raise
        finally:
            if close_session:
                session.close()

    def get_available_margin_for_new_position(self, session: Optional[Session] = None) -> float:
        """
        Get available margin for new position (accounts for already deployed capital)

        Dynamic position sizing: Each new position takes 20% of REMAINING margin

        Args:
            session: Database session (optional)

        Returns:
            Available margin for new position
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            # Fetch current margin from Zerodha
            margin_data = self.fetch_margin_from_zerodha()

            # Determine base margin (respects capital allocation if set)
            if margin_data.get('allocated') is not None:
                # Using capital allocation - use effective allocation as base
                effective_allocation = margin_data['allocated'] - margin_data.get('reserve_buffer', 0)
                base_margin = effective_allocation
            else:
                # Not using capital allocation - use total account margin
                base_margin = margin_data['net']

            # Calculate deployed capital from open positions
            # CRITICAL: Use row-level locking to prevent race conditions
            # with_for_update() locks rows until transaction commits
            open_positions = session.query(OpenPosition).filter_by(
                status=PositionStatus.OPEN
            ).with_for_update().all()
            deployed_from_positions = sum(pos.capital_deployed for pos in open_positions)

            # Calculate reserved capital from pending orders
            # CRITICAL: Lock pending orders to prevent concurrent capital allocation
            pending_orders = session.query(OpenOrder).filter_by(
                status=OrderStatus.PENDING
            ).with_for_update().all()
            reserved_from_orders = sum(order.capital_deployed for order in pending_orders)

            # Total deployed = positions + pending orders
            total_deployed = deployed_from_positions + reserved_from_orders

            # Available margin = Base - Deployed
            available_margin = base_margin - total_deployed

            logger.info(
                f"Margin calculation: Base=Rs.{base_margin:.2f}, "
                f"Deployed (positions)=Rs.{deployed_from_positions:.2f}, "
                f"Reserved (orders)=Rs.{reserved_from_orders:.2f}, "
                f"Available=Rs.{available_margin:.2f}"
            )

            return max(0.0, available_margin)  # Never negative

        except Exception as e:
            logger.error(f"Error getting available margin: {e}")
            if close_session:
                session.rollback()
            raise
        finally:
            if close_session:
                session.close()

    # ========== P&L AND DRAWDOWN CALCULATION HELPERS ==========

    def _calculate_realized_pnl_month(self, session: Session) -> float:
        """
        Calculate realized P&L for current month from closed positions.

        Returns:
            Total realized P&L (positive = profit, negative = loss)
        """
        today = date.today()
        first_of_month = today.replace(day=1)

        result = session.query(func.sum(ClosedPosition.net_pnl)).filter(
            ClosedPosition.bot_instance_id == self.bot_instance_id,
            ClosedPosition.exit_date >= first_of_month
        ).scalar()

        return float(result) if result else 0.0

    def _calculate_realized_pnl_today(self, session: Session) -> float:
        """
        Calculate realized P&L for today from closed positions.

        Returns:
            Today's realized P&L
        """
        today = date.today()

        result = session.query(func.sum(ClosedPosition.net_pnl)).filter(
            ClosedPosition.bot_instance_id == self.bot_instance_id,
            ClosedPosition.exit_date == today
        ).scalar()

        return float(result) if result else 0.0

    def _calculate_unrealized_pnl(self, open_positions: list, session: Session) -> float:
        """
        Calculate unrealized P&L for open positions.

        Uses entry_price as fallback if LTP not available (market closed).
        During market hours, attempts to fetch current LTP.

        Args:
            open_positions: List of OpenPosition objects
            session: Database session

        Returns:
            Total unrealized P&L
        """
        if not open_positions:
            return 0.0

        total_unrealized = 0.0

        # Check if market is open (rough check: 9:15 AM - 3:30 PM IST)
        now = ist_now_naive()
        market_open = now.hour >= 9 and (now.hour < 15 or (now.hour == 15 and now.minute <= 30))

        for pos in open_positions:
            try:
                current_price = pos.entry_price  # Default to entry (no unrealized)

                if market_open:
                    # Try to fetch LTP during market hours
                    try:
                        instrument_token = self.kite_client.get_instrument_token(pos.script)
                        current_price = self.kite_client.get_instrument_ltp(instrument_token)
                    except Exception as e:
                        logger.debug(f"Could not fetch LTP for {pos.script}: {e}, using entry price")
                        current_price = pos.entry_price

                # Calculate unrealized P&L for this position
                unrealized = (current_price - pos.entry_price) * pos.quantity
                total_unrealized += unrealized

            except Exception as e:
                logger.warning(f"Error calculating unrealized P&L for {pos.script}: {e}")

        return total_unrealized

    def _detect_capital_allocation_change(self, session: Session) -> Tuple[bool, Optional[float], Optional[float]]:
        """
        Detect if allocated capital in config differs from last recorded value.

        Returns:
            (changed, old_value, new_value)
        """
        # Get current config value
        current_allocated = self.allocated_capital_amount
        if current_allocated is None:
            return False, None, None

        # Get last recorded allocated_capital from ledger
        prev_ledger = session.query(CapitalLedger).filter(
            CapitalLedger.bot_instance_id == self.bot_instance_id,
            CapitalLedger.allocated_capital.isnot(None)
        ).order_by(CapitalLedger.date.desc()).first()

        if prev_ledger is None:
            # First run, no previous record
            return False, None, current_allocated

        old_allocated = prev_ledger.allocated_capital

        # Check if changed (allow small tolerance for float comparison)
        if old_allocated and abs(current_allocated - old_allocated) > 1.0:
            return True, old_allocated, current_allocated

        return False, old_allocated, current_allocated

    def update_capital_ledger(self, session: Optional[Session] = None) -> CapitalLedger:
        """
        Update daily capital ledger with current state (bot-specific)

        FIXED: Uses P&L-based drawdown calculation instead of Zerodha margin.

        Drawdown is now calculated as:
            Total P&L (month) = Realized P&L + Unrealized P&L
            Drawdown % = -Total P&L / Allocated Capital (only if P&L is negative)

        Capital change detection:
            If allocated_capital in config changes, baseline is reset and
            Telegram alert is sent.

        Args:
            session: Database session (optional)

        Returns:
            Updated CapitalLedger entry
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            today = date.today()

            # Get allocated capital from config (THIS IS THE BASE FOR ALL CALCULATIONS)
            allocated_capital = self.allocated_capital_amount
            if allocated_capital is None:
                # Fallback to Zerodha margin if no allocation configured
                margin_data = self.fetch_margin_from_zerodha(session)
                allocated_capital = margin_data['net']
                logger.warning("No allocated_capital configured - using Zerodha net margin")
            else:
                # Still fetch margin data for account reference
                margin_data = self.fetch_margin_from_zerodha(session)

            # Get or create today's ledger entry (FILTERED BY BOT)
            ledger = session.query(CapitalLedger).filter_by(
                date=today,
                bot_instance_id=self.bot_instance_id
            ).first()

            # Get open positions (FILTERED BY BOT)
            open_positions = session.query(OpenPosition).filter_by(
                status=PositionStatus.OPEN,
                bot_instance_id=self.bot_instance_id
            ).all()
            deployed_from_positions = sum(pos.capital_deployed for pos in open_positions)

            # Get pending orders (FILTERED BY BOT)
            pending_orders = session.query(OpenOrder).filter_by(
                status=OrderStatus.PENDING,
                bot_instance_id=self.bot_instance_id
            ).all()
            reserved_from_orders = sum(order.capital_deployed for order in pending_orders)

            # Total deployed capital (by this bot)
            deployed_capital = deployed_from_positions + reserved_from_orders
            free_capital = allocated_capital - deployed_capital
            num_open = len(open_positions)

            # === P&L CALCULATIONS (KEY FIX) ===
            realized_pnl_today = self._calculate_realized_pnl_today(session)
            realized_pnl_month = self._calculate_realized_pnl_month(session)
            unrealized_pnl = self._calculate_unrealized_pnl(open_positions, session)
            total_pnl_month = realized_pnl_month + unrealized_pnl

            # Current capital = allocated + total P&L this month
            current_capital = allocated_capital + total_pnl_month

            # === CAPITAL CHANGE DETECTION ===
            capital_changed, old_capital, new_capital = self._detect_capital_allocation_change(session)
            reset_reason = None

            if capital_changed:
                reset_reason = f"Capital allocation changed: Rs.{old_capital:,.0f} → Rs.{new_capital:,.0f}"
                logger.info(reset_reason)

                # Send Telegram alert for capital change
                from src.reporting.telegram_client import telegram
                telegram.send_alert(
                    f"💰 **Capital Allocation Changed**\n"
                    f"Old: Rs.{old_capital:,.0f}\n"
                    f"New: Rs.{new_capital:,.0f}\n"
                    f"📊 Drawdown baseline reset to 0%",
                    critical=False
                )

            if ledger is None:
                # Create new entry
                ledger = CapitalLedger(
                    date=today,
                    bot_instance_id=self.bot_instance_id,
                    opening_capital=allocated_capital,  # Use allocated, not Zerodha margin
                    deployed_capital=deployed_capital,
                    free_capital=free_capital,
                    num_open_positions=num_open,
                    total_capital=current_capital,
                    realized_pnl_today=realized_pnl_today,
                    unrealized_pnl=unrealized_pnl,
                    # Capital allocation tracking
                    allocated_capital=allocated_capital,
                    allocation_method='amount' if self.allocated_capital_amount else 'percent',
                    total_account_margin=margin_data['net'],
                    reserve_buffer=margin_data.get('reserve_buffer', 0)
                )

                # === BASELINE RESET LOGIC ===
                # Reset happens on: 1) First run OR 2) Month change OR 3) Capital allocation change
                prev_ledger = session.query(CapitalLedger).filter(
                    CapitalLedger.date < today,
                    CapitalLedger.bot_instance_id == self.bot_instance_id
                ).order_by(CapitalLedger.date.desc()).first()

                need_baseline_reset = False

                if prev_ledger is None:
                    # First run ever
                    need_baseline_reset = True
                    reset_reason = "First capital ledger entry"
                    logger.info(reset_reason)
                elif prev_ledger.date.month != today.month or prev_ledger.date.year != today.year:
                    # Month changed (handles weekends/holidays on 1st)
                    need_baseline_reset = True
                    reset_reason = f"Month changed: {prev_ledger.date.strftime('%Y-%m')} → {today.strftime('%Y-%m')}"
                    logger.info(reset_reason)
                elif capital_changed:
                    # Capital allocation changed
                    need_baseline_reset = True
                    # reset_reason already set above

                if need_baseline_reset:
                    # Reset baseline to ALLOCATED CAPITAL (not Zerodha margin)
                    ledger.starting_capital_month = allocated_capital
                    ledger.monthly_drawdown_pct = 0.0
                    logger.info(f"Monthly baseline reset to Rs.{allocated_capital:,.0f} ({reset_reason})")
                else:
                    # Within same month - carry forward previous baseline
                    if prev_ledger and prev_ledger.starting_capital_month:
                        ledger.starting_capital_month = prev_ledger.starting_capital_month
                    else:
                        ledger.starting_capital_month = allocated_capital
                        logger.warning("No previous baseline found - using allocated capital")

                session.add(ledger)
            else:
                # Update existing entry
                ledger.deployed_capital = deployed_capital
                ledger.free_capital = free_capital
                ledger.num_open_positions = num_open
                ledger.total_capital = current_capital
                ledger.realized_pnl_today = realized_pnl_today
                ledger.unrealized_pnl = unrealized_pnl
                # Update capital allocation tracking
                ledger.allocated_capital = allocated_capital
                ledger.allocation_method = 'amount' if self.allocated_capital_amount else 'percent'
                ledger.total_account_margin = margin_data['net']
                ledger.reserve_buffer = margin_data.get('reserve_buffer', 0)
                ledger.updated_at = ist_now_naive()

                # Check for capital change on existing entry
                if capital_changed:
                    ledger.starting_capital_month = allocated_capital
                    ledger.monthly_drawdown_pct = 0.0
                    logger.info(f"Baseline reset due to capital change: Rs.{allocated_capital:,.0f}")

            # === DRAWDOWN CALCULATION (KEY FIX) ===
            # Drawdown = -Total P&L / Allocated Capital (only if P&L is negative)
            if allocated_capital > 0:
                if total_pnl_month < 0:
                    # We have losses - calculate drawdown
                    dd_pct = (abs(total_pnl_month) / allocated_capital) * 100
                    ledger.monthly_drawdown_pct = round(dd_pct, 2)
                else:
                    # Profit or break-even - no drawdown
                    ledger.monthly_drawdown_pct = 0.0

            session.commit()

            # Build log message
            log_msg = (
                f"Capital ledger updated (Bot: {self.bot_instance_id}): "
                f"Allocated=Rs.{allocated_capital:,.0f}, "
                f"Deployed=Rs.{deployed_capital:,.0f}, "
                f"P&L(Month)=Rs.{total_pnl_month:+,.0f}, "
                f"DD={ledger.monthly_drawdown_pct:.2f}%"
            )

            if num_open > 0:
                log_msg += f", Positions={num_open}"

            logger.info(log_msg)

            return ledger

        except Exception as e:
            logger.error(f"Failed to update capital ledger: {e}")
            session.rollback()
            raise
        finally:
            if close_session:
                session.close()

    def get_drawdown_status(self, session: Optional[Session] = None) -> Dict[str, any]:
        """
        Get current drawdown status and risk alerts (bot-specific)

        Returns:
            Dict with drawdown info and alert status
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            today = date.today()
            ledger = session.query(CapitalLedger).filter_by(
                date=today,
                bot_instance_id=self.bot_instance_id
            ).first()

            if not ledger:
                return {
                    'drawdown_pct': 0.0,
                    'alert_level': 'NONE',
                    'alert_message': None
                }

            dd_pct = ledger.monthly_drawdown_pct
            dd_caution = config.get('risk_management.drawdown_caution', 5)
            dd_critical = config.get('risk_management.drawdown_critical', 10)

            alert_level = 'HEALTHY'
            alert_message = None

            if dd_pct >= dd_critical:
                alert_level = 'CRITICAL'
                alert_message = (
                    f"🔴🔴🔴 **CRITICAL: {dd_pct:.2f}% Drawdown!**\n"
                    f"📊 Starting Capital: Rs.{ledger.starting_capital_month:,.2f}\n"
                    f"💰 Current Equity: Rs.{ledger.total_capital:,.2f}\n"
                    f"📉 Drawdown: {dd_pct:.2f}%\n"
                    f"🚨 IMMEDIATE REVIEW REQUIRED\n"
                    f"🛠️ Consider stopping bot or reducing position sizing"
                )
            elif dd_pct >= dd_caution:
                alert_level = 'CAUTION'
                alert_message = (
                    f"⚠️ **CAUTION: {dd_pct:.2f}% Drawdown Reached**\n"
                    f"📊 Starting Capital: Rs.{ledger.starting_capital_month:,.2f}\n"
                    f"💰 Current Equity: Rs.{ledger.total_capital:,.2f}\n"
                    f"📉 Drawdown: {dd_pct:.2f}%\n"
                    f"📋 Review trades and consider reducing position size"
                )

            return {
                'drawdown_pct': dd_pct,
                'starting_capital': ledger.starting_capital_month,
                'current_capital': ledger.total_capital,
                'alert_level': alert_level,
                'alert_message': alert_message
            }

        except Exception as e:
            logger.error(f"Error getting drawdown status: {e}")
            if close_session:
                session.rollback()
            raise
        finally:
            if close_session:
                session.close()

    def get_current_position_count(self, session: Session) -> Tuple[int, int]:
        """
        Get current count of open positions and pending orders FOR THIS BOT

        Uses row-level locking for consistency with margin calculations

        Args:
            session: Database session

        Returns:
            (open_positions_count, pending_orders_count)
        """
        # Use with_for_update() for consistency with get_available_margin_for_new_position()
        # This ensures position count and margin are calculated atomically
        # IMPORTANT: Filter by bot_instance_id to only count THIS bot's positions
        open_positions = session.query(OpenPosition).filter_by(
            status=PositionStatus.OPEN,
            bot_instance_id=self.bot_instance_id
        ).with_for_update().count()

        pending_orders = session.query(OpenOrder).filter_by(
            status=OrderStatus.PENDING,
            bot_instance_id=self.bot_instance_id
        ).with_for_update().count()

        logger.debug(
            f"Position count (Bot: {self.bot_instance_id}): "
            f"Open={open_positions}, Pending Orders={pending_orders}"
        )

        return open_positions, pending_orders

    def check_position_limit(
        self,
        current_positions: int,
        pending_orders: int
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """
        Check if we can take new position based on SEPARATE position and pending order limits

        NEW LOGIC (Hybrid Approach):
        - Open positions limited by max_positions (default: 5)
        - Pending orders limited SEPARATELY by max_pending_orders (default: 3)
        - This allows more signals to be placed without blocking on unfilled orders

        Args:
            current_positions: Current open positions count
            pending_orders: Current pending orders count

        Returns:
            (can_take, rejection_reason, alert_message)
            - can_take: True if within limits
            - rejection_reason: Reason if rejected (None if can take)
            - alert_message: Warning alert if approaching limit (None otherwise)
        """
        alert_message = None

        # Check 1: Open position limit (max_positions)
        if self.max_positions is not None:
            if current_positions >= self.max_positions:
                rejection_reason = (
                    f"Open position limit reached: {current_positions}/{self.max_positions} "
                    f"(Pending orders: {pending_orders})"
                )
                logger.warning(f"Open position limit hit: {rejection_reason}")
                return False, rejection_reason, None

            # Check if approaching open position limit (>= 80%)
            position_pct = (current_positions / self.max_positions) * 100
            if position_pct >= 80:
                alert_message = (
                    f"⚠️ **POSITION LIMIT WARNING**\n"
                    f"📊 Open: {current_positions}/{self.max_positions} ({position_pct:.0f}%)\n"
                    f"⏳ Pending: {pending_orders}"
                    f"{f'/{self.max_pending_orders}' if self.max_pending_orders else ''}\n"
                    f"💡 Consider reviewing positions"
                )
                logger.warning(f"Approaching position limit: {current_positions}/{self.max_positions}")

        # Check 2: Pending order limit (max_pending_orders) - SEPARATE from positions
        if self.max_pending_orders is not None:
            if pending_orders >= self.max_pending_orders:
                rejection_reason = (
                    f"Pending order limit reached: {pending_orders}/{self.max_pending_orders} "
                    f"(Open positions: {current_positions}/{self.max_positions if self.max_positions else '∞'})"
                )
                logger.warning(f"Pending order limit hit: {rejection_reason}")
                return False, rejection_reason, None

        # Both limits OK - can place new order
        logger.debug(
            f"Position check passed: Open={current_positions}/{self.max_positions if self.max_positions else '∞'}, "
            f"Pending={pending_orders}/{self.max_pending_orders if self.max_pending_orders else '∞'}"
        )

        return True, None, alert_message

    def can_take_new_position(self, entry_price: float, session: Optional[Session] = None) -> Tuple[bool, str, int, float]:
        """
        Check if we can take a new position and calculate sizing

        Validates:
        1. Position count limit (if configured)
        2. Margin availability
        3. Minimum margin threshold
        4. Position sizing constraints

        Args:
            entry_price: Proposed entry price
            session: Database session (optional)

        Returns:
            (can_take, reason, quantity, capital_deployed)
        """
        close_session = False
        if session is None:
            session = get_session()
            close_session = True

        try:
            # Step 1: Check position limit (if configured)
            current_positions, pending_orders = self.get_current_position_count(session)
            can_take_limit, rejection_reason, alert_message = self.check_position_limit(
                current_positions, pending_orders
            )

            # Send warning alert if approaching limit (non-blocking)
            if alert_message:
                try:
                    from src.reporting.telegram_client import telegram
                    telegram.send_alert(alert_message, critical=False)
                except Exception as e:
                    logger.error(f"Failed to send position limit warning alert: {e}")

            # Reject if limit reached
            if not can_take_limit:
                return False, rejection_reason, 0, 0.0

            # Step 2: Get available margin
            available_margin = self.get_available_margin_for_new_position(session)

            # Step 3: Check minimum margin threshold
            is_sufficient, _ = self.check_margin_threshold(available_margin)

            if not is_sufficient:
                return False, f"Insufficient margin: Rs.{available_margin:.2f}", 0, 0.0

            # Step 4: Calculate position size (with drawdown adjustment)
            quantity, capital_deployed, dd_alert = self.calculate_position_size(
                entry_price, available_margin, session
            )

            # Send drawdown alert if position sizing was reduced
            if dd_alert:
                try:
                    from src.reporting.telegram_client import telegram
                    telegram.send_alert(dd_alert, critical=False)
                except Exception as e:
                    logger.error(f"Failed to send drawdown alert: {e}")

            if quantity == 0:
                return False, f"Cannot size position with available margin Rs.{available_margin:.2f}", 0, 0.0

            return True, "OK", quantity, capital_deployed

        finally:
            if close_session:
                session.close()


# Singleton instance
capital_manager = CapitalManager()

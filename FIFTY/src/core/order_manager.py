"""
Order Manager - Handles entry order placement and monitoring

Responsibilities:
- Place GTT entry orders for approved signals
- Monitor GTT for triggers/fills
- Create positions when filled
- Cancel unfilled orders at month end
"""

import re
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from loguru import logger

from src.api.dual_kite_client import get_kite_client
from src.models.database import (
    get_session, SignalQueue, OpenOrder, OpenPosition, ClosedPosition,
    SignalStatus, OrderStatus, PositionStatus, ist_now_naive
)
from src.telegram.bot import telegram
from src.utils.config_manager import config
from src.utils.timezone_helper import today_ist, now_ist, in_time_window
from src.utils.price_rounder import round_price, round_price_down, PriceRounder

# Order cutoff time - no orders after 3:29 PM (safety feature from CROCODILE)
ORDER_CUTOFF_TIME = "15:29"

# FIX RC-002: Lock for duplicate checking to prevent race conditions
import threading
_order_placement_lock = threading.Lock()


class OrderManager:
    """Manages entry orders for FIFTY bot"""

    def __init__(self):
        """Initialize order manager"""
        self.kite = get_kite_client()
        self.order_tag = config.get('kite_trade.order_tag', 'fifty')

        logger.info("Order manager initialized")

    def _validate_order_params(
        self,
        script: str,
        entry_price: float,
        quantity: int,
        ltp: float
    ) -> Tuple[bool, str, Optional[float]]:
        """
        Comprehensive 6-step pre-order validation (ported from CROCODILE).

        Validates:
        1. LTP sanity checks (not None, > 0, >= 0.05)
        2. Entry price sanity checks (> 0)
        3. Price relationship sanity (not 10x different)
        4. Tick size validation
        5. Quantity validation (> 0)
        6. Minimum order value (Rs.10)

        Args:
            script: Stock symbol
            entry_price: Entry price (rounded)
            quantity: Order quantity
            ltp: Current LTP

        Returns:
            (is_valid, error_message, corrected_price)
            corrected_price is only set if tick size was corrected
        """
        # === VALIDATION 1: LTP sanity checks ===
        if ltp is None:
            return False, f"LTP is None for {script}", None

        if ltp <= 0:
            return False, f"Invalid LTP (≤0) for {script}: Rs.{ltp}", None

        if ltp < 0.05:
            return False, f"LTP below minimum tick size for {script}: Rs.{ltp}", None

        # === VALIDATION 2: Entry price sanity checks ===
        if entry_price <= 0:
            return False, f"Invalid entry price (≤0) for {script}: Rs.{entry_price}", None

        # === VALIDATION 3: Price relationship sanity check ===
        # If LTP is 10x higher or lower than entry price, something is wrong
        price_ratio = ltp / entry_price if entry_price > 0 else 0
        if price_ratio > 10 or price_ratio < 0.1:
            return False, (
                f"Suspicious price difference for {script}: "
                f"LTP=Rs.{ltp:.2f}, Entry=Rs.{entry_price:.2f} "
                f"(ratio: {price_ratio:.2f}x)"
            ), None

        # === VALIDATION 4: Tick size validation ===
        tick_size = PriceRounder.get_nse_tick_size(entry_price)
        remainder = abs(entry_price % tick_size)
        # Check if remainder is close to 0 OR close to tick_size (due to float precision)
        is_valid_tick = (remainder < 0.001) or (abs(remainder - tick_size) < 0.001)

        corrected_price = None
        if not is_valid_tick:
            # Try to correct the price
            corrected_price = PriceRounder.round_to_tick(entry_price, tick_size=tick_size)
            logger.warning(
                f"Entry price not multiple of tick size for {script}: "
                f"Rs.{entry_price} → Rs.{corrected_price} (tick: {tick_size})"
            )

        # === VALIDATION 5: Quantity validation ===
        if quantity <= 0:
            return False, f"Invalid quantity (≤0) for {script}: {quantity}", None

        # === VALIDATION 6: Minimum order value ===
        price_to_use = corrected_price if corrected_price else entry_price
        min_order_value = 10  # Rs.10 minimum (NSE requirement)
        order_value = price_to_use * quantity
        if order_value < min_order_value:
            return False, (
                f"Order value too small for {script}: "
                f"Rs.{order_value:.2f} (min: Rs.{min_order_value})"
            ), None

        # All validations passed
        logger.debug(
            f"Pre-order validation PASSED for {script}: "
            f"LTP=Rs.{ltp:.2f}, Entry=Rs.{price_to_use:.2f}, "
            f"Qty={quantity}, Value=Rs.{order_value:.2f}"
        )

        return True, "Validation passed", corrected_price

    def _is_past_order_cutoff(self) -> bool:
        """
        Check if current time is past order cutoff.

        Safety feature from CROCODILE - prevents orders near market close.

        Returns:
            True if past cutoff (orders should NOT be placed)
        """
        current = now_ist()
        cutoff_hour, cutoff_min = map(int, ORDER_CUTOFF_TIME.split(':'))

        if current.hour > cutoff_hour:
            return True
        if current.hour == cutoff_hour and current.minute >= cutoff_min:
            return True

        return False

    def _check_portfolio_drawdown(self) -> Tuple[bool, float, str]:
        """
        FIX RM-001: Check if portfolio drawdown exceeds maximum allowed.

        Returns:
            (is_ok, current_drawdown_pct, message)
            is_ok = True means drawdown is acceptable, False means halt new entries
        """
        max_drawdown_pct = config.get('risk.max_drawdown_percent', 15)
        initial_capital = config.get('trading.initial_capital', 100000)

        session = get_session()
        try:
            # Get all closed trades P&L
            closed_trades = session.query(ClosedPosition).all()
            realized_pnl = sum(t.net_pnl for t in closed_trades)

            # Get unrealized P&L from open positions
            unrealized_pnl = 0
            positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            for pos in positions:
                try:
                    instrument_token = self.kite.get_instrument_token(pos.script)
                    ltp = self.kite.get_instrument_ltp(instrument_token)
                    if ltp and ltp > 0:
                        unrealized_pnl += (ltp - pos.entry_price) * pos.quantity
                except Exception:
                    # Conservative: assume worst case (initial SL hit)
                    sl_loss = (pos.entry_price - pos.initial_sl) * pos.quantity
                    unrealized_pnl -= sl_loss

            # Calculate total P&L and drawdown
            total_pnl = realized_pnl + unrealized_pnl
            current_drawdown_pct = 0

            if total_pnl < 0:
                current_drawdown_pct = abs(total_pnl) / initial_capital * 100

            if current_drawdown_pct >= max_drawdown_pct:
                return False, current_drawdown_pct, f"Drawdown {current_drawdown_pct:.1f}% >= max {max_drawdown_pct}%"

            return True, current_drawdown_pct, "OK"

        finally:
            session.close()

    def _check_nifty_filter_at_entry(self) -> Tuple[bool, str]:
        """
        FIX TR-002: Re-check NIFTY weekly filter before placing entry order.

        Signal may have been approved during bullish but market could turn bearish
        before the entry is actually placed.

        Returns:
            (is_ok, message)
        """
        if not config.get('nifty_filter.enabled', True):
            return True, "Filter disabled"

        try:
            from src.core.signal_processor import signal_processor
            is_bullish = signal_processor.check_nifty_weekly_filter()

            if not is_bullish:
                return False, "NIFTY weekly turned bearish since approval"

            return True, "NIFTY weekly still bullish"

        except Exception as e:
            # FIX TR-M2: Block on error (conservative)
            return False, f"NIFTY filter check failed: {e}"

    def place_entry_gtt(self, signal_id: int, script: str, entry_price: float) -> Optional[str]:
        """
        Place GTT entry order for approved signal

        Args:
            signal_id: Signal queue ID
            script: Stock symbol
            entry_price: Entry price (SuperTrend or user-revised)

        Returns:
            GTT ID if successful, None otherwise
        """
        # ORDER CUTOFF CHECK (safety feature from CROCODILE)
        if self._is_past_order_cutoff():
            logger.warning(f"Order cutoff reached ({ORDER_CUTOFF_TIME}) - rejecting entry for {script}")
            telegram.send_alert(
                f"<b>Entry Blocked - Past Cutoff</b>\n\n"
                f"{script} @ {entry_price:,.2f}\n"
                f"No orders after {ORDER_CUTOFF_TIME}"
            )
            return None

        # FIX RM-001: Check portfolio drawdown before new entry
        drawdown_ok, current_dd, dd_msg = self._check_portfolio_drawdown()
        if not drawdown_ok:
            logger.warning(f"Entry blocked for {script}: Portfolio drawdown limit reached - {dd_msg}")
            telegram.send_alert(
                f"<b>Entry Blocked - Drawdown Limit</b>\n\n"
                f"{script} @ {entry_price:,.2f}\n"
                f"Portfolio drawdown: {current_dd:.1f}%\n"
                f"New entries halted until drawdown recovers.",
                critical=True
            )
            return None

        # FIX TR-002: Re-check NIFTY filter at entry time
        nifty_ok, nifty_msg = self._check_nifty_filter_at_entry()
        if not nifty_ok:
            logger.warning(f"Entry blocked for {script}: {nifty_msg}")
            telegram.send_alert(
                f"<b>Entry Blocked - Market Condition</b>\n\n"
                f"{script} @ {entry_price:,.2f}\n"
                f"{nifty_msg}\n"
                f"Signal will need re-approval if market turns bullish."
            )
            # Mark signal as HOLD so it can be re-notified
            session = get_session()
            try:
                signal = session.query(SignalQueue).filter(SignalQueue.id == signal_id).first()
                if signal:
                    signal.status = SignalStatus.HOLD
                    signal.notes = f"Auto-hold: {nifty_msg}"
                    session.commit()
            finally:
                session.close()
            return None

        # FIX RC-002: Acquire lock to prevent race condition in duplicate check
        with _order_placement_lock:
            return self._place_entry_gtt_locked(signal_id, script, entry_price)

    def _place_entry_gtt_locked(self, signal_id: int, script: str, entry_price: float) -> Optional[str]:
        """Internal method - must be called with _order_placement_lock held"""
        session = get_session()
        try:
            # DUPLICATE POSITION CHECK (safety feature from CROCODILE)
            # Check if we already have an open position in this script
            existing_position = session.query(OpenPosition).filter(
                OpenPosition.script == script,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if existing_position:
                logger.warning(
                    f"Already have open position in {script}: "
                    f"Entry={existing_position.entry_price:.2f}, Qty={existing_position.quantity}"
                )
                telegram.send_alert(
                    f"<b>Entry Blocked - Duplicate Position</b>\n\n"
                    f"{script}: Already holding position\n"
                    f"Entry: {existing_position.entry_price:,.2f}\n"
                    f"Qty: {existing_position.quantity}"
                )
                return None

            # IDEMPOTENCY CHECK 1: Check if order already exists for this signal
            # FIX ARCH-C1: Also check PLACING status (order being placed right now)
            existing_order = session.query(OpenOrder).filter(
                OpenOrder.signal_id == signal_id,
                OpenOrder.status.in_([OrderStatus.PENDING, OrderStatus.PLACING])
            ).first()

            if existing_order:
                logger.warning(
                    f"Order already exists for signal {signal_id}: "
                    f"GTT {existing_order.gtt_id}, status={existing_order.status.value}, skipping duplicate"
                )
                return existing_order.gtt_id if existing_order.gtt_id else "PLACING"

            # IDEMPOTENCY CHECK 2: Check if ANY pending/placing order exists for this script
            # (prevents multiple GTTs on same script from different signals)
            existing_script_order = session.query(OpenOrder).filter(
                OpenOrder.script == script,
                OpenOrder.status.in_([OrderStatus.PENDING, OrderStatus.PLACING])
            ).first()

            if existing_script_order:
                logger.warning(
                    f"Pending order already exists for {script}: "
                    f"GTT {existing_script_order.gtt_id}, skipping"
                )
                telegram.send_alert(
                    f"<b>Entry Blocked - Pending Order Exists</b>\n\n"
                    f"{script}: Already have pending entry GTT\n"
                    f"GTT ID: {existing_script_order.gtt_id}\n"
                    f"Trigger: {existing_script_order.trigger_price:,.2f}"
                )
                return None

            # IDEMPOTENCY CHECK 3: Check if there's already a pending GTT for this script
            # This catches cases where DB commit failed after GTT was placed
            try:
                existing_gtts = self.kite.get_gtt_orders()
                for gtt in existing_gtts:
                    # FIX LOG-1: Safe access to orders list (could be empty)
                    orders = gtt.get('orders') or []
                    transaction_type = orders[0].get('transaction_type') if orders else None

                    if (gtt.get('condition', {}).get('tradingsymbol') == script and
                        gtt.get('status') == 'active' and
                        transaction_type == 'BUY'):

                        gtt_id = str(gtt.get('id'))
                        logger.warning(
                            f"Found orphaned GTT for {script}: {gtt_id}, "
                            f"recovering instead of creating duplicate"
                        )
                        # Recover: Create the DB record for this orphaned GTT
                        self._recover_orphaned_gtt(signal_id, script, gtt, session)
                        return gtt_id

            except Exception as e:
                logger.warning(f"Could not check existing GTTs: {e}")

            # Get signal
            signal = session.query(SignalQueue).filter(
                SignalQueue.id == signal_id
            ).first()

            if not signal:
                logger.error(f"Signal {signal_id} not found")
                return None

            # FIX TR-004: Use pre-calculated quantity from signal for consistency
            # Only recalculate if not stored (backward compatibility)
            if signal.calculated_quantity and signal.calculated_quantity > 0:
                quantity = signal.calculated_quantity
                logger.debug(f"Using pre-calculated quantity for {script}: {quantity}")
            else:
                # Fallback: calculate now (for old signals or manual entries)
                per_trade = config.get('trading.per_trade_amount', 20000)
                quantity = int(per_trade / entry_price) if entry_price > 0 else 1
                logger.debug(f"Calculated quantity for {script}: {quantity}")

            # FIX TR-C3: Validate minimum quantity
            if quantity <= 0:
                logger.error(
                    f"Calculated quantity is 0 for {script} "
                    f"(per_trade={per_trade}, entry={entry_price})"
                )
                telegram.send_alert(
                    f"<b>Entry Blocked - Zero Quantity</b>\n\n"
                    f"{script}: Entry {entry_price:,.2f} exceeds per-trade {per_trade:,.0f}\n"
                    f"Increase per_trade_amount or reject this signal.",
                    critical=True
                )
                return None

            if config.is_test_mode():
                quantity = 1

            # Round price to tick size
            trigger_price = round_price(entry_price)
            limit_price = round_price(entry_price)

            # Get instrument token
            instrument_token = self.kite.get_instrument_token(script)

            # Get current LTP for GTT
            try:
                ltp = self.kite.get_instrument_ltp(instrument_token)
            except Exception:
                ltp = entry_price

            # === COMPREHENSIVE PRE-ORDER VALIDATION (from CROCODILE) ===
            is_valid, validation_msg, corrected_price = self._validate_order_params(
                script=script,
                entry_price=trigger_price,
                quantity=quantity,
                ltp=ltp
            )

            if not is_valid:
                logger.error(f"Pre-order validation failed for {script}: {validation_msg}")
                telegram.send_alert(
                    f"<b>Entry GTT Blocked</b>\n\n"
                    f"{script}: {validation_msg}",
                    critical=True
                )
                return None

            # Use corrected price if tick size was adjusted
            if corrected_price:
                logger.info(f"Using corrected price for {script}: {trigger_price} → {corrected_price}")
                trigger_price = corrected_price
                limit_price = corrected_price

            # Prepare GTT payload
            payload = {
                'type': 'single',
                'condition': {
                    'exchange': 'NSE',
                    'tradingsymbol': script,
                    'trigger_values': [trigger_price],
                    'last_price': ltp
                },
                'orders': [{
                    'exchange': 'NSE',
                    'tradingsymbol': script,
                    'transaction_type': 'BUY',
                    'quantity': quantity,
                    'order_type': 'LIMIT',
                    'product': 'CNC',
                    'price': limit_price
                }],
                'expires_at': self._get_gtt_expiry()
            }

            # FIX ARCH-C1: Create order record BEFORE API call (idempotency)
            # This ensures no duplicate orders if crash occurs after API success but before DB commit
            capital_deployed = quantity * entry_price
            order = OpenOrder(
                signal_id=signal_id,
                gtt_id=None,  # Will be set after API success
                script=script,
                exchange='NSE',
                trigger_price=trigger_price,
                limit_price=limit_price,
                quantity=quantity,
                capital_deployed=capital_deployed,
                status=OrderStatus.PLACING,  # Mark as PLACING before API call
                placed_at=ist_now_naive()
            )
            session.add(order)
            session.flush()  # Get order.id without committing
            logger.debug(f"Created PLACING record for {script}, order_id={order.id}")

            # Place GTT (with tick size and "price too close" error retry)
            max_retries = 3  # Increased for price adjustment retries
            gtt_id = None
            price_adjusted = False  # Track if we adjusted for "too close" error

            for attempt in range(max_retries):
                try:
                    # FIX SYS-M1: Re-fetch LTP on retry to ensure current price
                    if attempt > 0:
                        try:
                            fresh_ltp = self.kite.get_instrument_ltp(instrument_token)
                            payload['condition']['last_price'] = fresh_ltp
                            logger.debug(f"Refreshed LTP for {script}: {fresh_ltp}")
                        except Exception as ltp_error:
                            logger.warning(f"Could not refresh LTP on retry: {ltp_error}")

                    result = self.kite.place_gtt_order(payload)
                    gtt_id = str(result.get('trigger_id'))
                    break  # Success

                except Exception as gtt_error:
                    error_msg = str(gtt_error).lower()

                    # Check if it's a "price too close to LTP" error
                    # Zerodha requires trigger to be >0.25% away from LTP
                    price_too_close = (
                        "too close" in error_msg or
                        "0.25%" in error_msg or
                        "difference should be more than" in error_msg
                    )

                    if attempt < max_retries - 1 and price_too_close and not price_adjusted:
                        # AUTO-ADJUST: Reduce entry price by 0.5% to get away from LTP
                        # This ensures we're well below the 0.25% threshold
                        adjustment_pct = 0.005  # 0.5%
                        old_trigger = trigger_price
                        trigger_price = round_price_down(trigger_price * (1 - adjustment_pct))
                        limit_price = trigger_price

                        logger.warning(
                            f"GTT 'price too close' for {script}. "
                            f"Auto-adjusting entry: {old_trigger:.2f} -> {trigger_price:.2f} (-0.5%)"
                        )

                        # Update payload
                        payload['condition']['trigger_values'] = [trigger_price]
                        payload['orders'][0]['price'] = limit_price

                        # Update order record with new price
                        order.trigger_price = trigger_price
                        order.limit_price = limit_price

                        price_adjusted = True
                        continue  # Retry with adjusted price

                    # Check if it's a tick size error (can retry)
                    if attempt < max_retries - 1 and "tick size" in error_msg:
                        # Extract tick size from error message
                        tick_match = re.search(r'tick size.*?is\s+(0\.\d+)', str(gtt_error), re.IGNORECASE)

                        if tick_match:
                            required_tick = float(tick_match.group(1))
                            logger.warning(
                                f"GTT tick size error for {script}. "
                                f"Zerodha requires {required_tick}, retrying..."
                            )

                            # Re-round prices with correct tick size
                            trigger_price = PriceRounder.round_to_tick(trigger_price, tick_size=required_tick)
                            limit_price = trigger_price

                            # Update payload
                            payload['condition']['trigger_values'] = [trigger_price]
                            payload['orders'][0]['price'] = limit_price

                            logger.info(f"Corrected GTT prices for {script}: {trigger_price}")
                            continue  # Retry

                    # Not a recoverable error or already retried
                    # Mark as REJECTED and rollback
                    order.status = OrderStatus.REJECTED
                    order.last_error = str(gtt_error)
                    session.commit()
                    raise

            if not gtt_id:
                order.status = OrderStatus.REJECTED
                order.last_error = "GTT placement failed after retries"
                session.commit()
                raise Exception("GTT placement failed after retries")

            # Success - update order record with GTT ID and change status to PENDING
            order.gtt_id = gtt_id
            order.status = OrderStatus.PENDING
            order.trigger_price = trigger_price  # May have been corrected
            order.limit_price = limit_price
            logger.info(f"GTT placed for {script}: ID={gtt_id}, Trigger={trigger_price}, Qty={quantity}")

            # Update signal status
            signal.status = SignalStatus.ENTERED
            session.commit()

            # Send confirmation (with adjustment note if applicable)
            adjustment_note = ""
            if price_adjusted:
                adjustment_note = f"\n(Auto-adjusted from {entry_price:,.2f} - LTP too close)"

            telegram.send_alert(
                f"<b>Entry GTT Placed</b>\n\n"
                f"{script} @ {trigger_price:,.2f}{adjustment_note}\n"
                f"Qty: {quantity} | Value: {capital_deployed:,.0f}"
            )

            return gtt_id

        except Exception as e:
            logger.error(f"Failed to place entry GTT for {script}: {e}")
            session.rollback()
            return None
        finally:
            session.close()

    def check_order_fills(self) -> List[Dict[str, Any]]:
        """
        Check pending orders for fills

        Returns:
            List of filled orders
        """
        filled_orders = []
        session = get_session()

        try:
            # Get pending orders
            pending = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING,
                OpenOrder.position_created == False
            ).all()

            if not pending:
                return []

            # Get all GTTs from Zerodha
            try:
                gtts = self.kite.get_gtt_orders()
            except Exception as e:
                logger.error(f"Failed to fetch GTTs: {e}")
                return []

            gtt_map = {str(g.get('id')): g for g in gtts}

            for order in pending:
                gtt_id = order.gtt_id
                gtt = gtt_map.get(gtt_id)

                if gtt is None:
                    # FIX TR-H1: GTT not found - could be triggered, cancelled, or expired
                    # Don't assume triggered - verify with order check first
                    logger.warning(f"GTT {gtt_id} for {order.script} not found in GTT list - investigating")
                    fill_result = self._check_if_gtt_triggered(order, session)
                    if fill_result:
                        filled_orders.append(fill_result)
                    else:
                        # GTT missing but no fill found
                        # Only alert once per order (use last_error as dedup marker)
                        dedup_marker = f"GTT_MISSING_{gtt_id}"
                        if order.last_error != dedup_marker:
                            logger.warning(
                                f"GTT {gtt_id} for {order.script} missing with no fill detected. "
                                f"May have been cancelled/expired. Needs manual verification."
                            )
                            order.last_error = dedup_marker
                            session.commit()
                            telegram.send_alert(
                                f"<b>GTT Status Unknown</b>\n\n"
                                f"{order.script}: GTT {gtt_id} not found\n"
                                f"No fill detected. Please verify in Zerodha.\n"
                                f"Use /sync to check status."
                            )
                    continue

                gtt_status = gtt.get('status', '')

                if gtt_status == 'triggered':
                    # GTT triggered - check order status
                    orders = gtt.get('orders', [])
                    if orders:
                        gtt_order = orders[0]
                        order_result = gtt_order.get('result', {})
                        order_id = order_result.get('order_id')

                        if order_id:
                            fill_result = self._process_triggered_gtt(order, order_id)
                            if fill_result:
                                filled_orders.append(fill_result)

                elif gtt_status == 'cancelled':
                    order.status = OrderStatus.CANCELLED
                    session.commit()
                    telegram.send_alert(f"Entry GTT cancelled: {order.script}")

                elif gtt_status == 'rejected':
                    order.status = OrderStatus.REJECTED
                    order.last_error = gtt.get('meta', {}).get('rejection_reason', 'Unknown')
                    session.commit()
                    telegram.send_alert(
                        f"Entry GTT rejected: {order.script}\n"
                        f"Reason: {order.last_error}",
                        critical=True
                    )

        except Exception as e:
            logger.error(f"Error checking order fills: {e}")
            session.rollback()

        finally:
            session.close()

        return filled_orders

    def _check_if_gtt_triggered(self, order: OpenOrder, session) -> Optional[Dict[str, Any]]:
        """
        Check if entry GTT was triggered when it's no longer in get_gtts().

        Checks two sources:
        1. Today's orders — for same-day fills
        2. OpenPosition table — for prior-day fills (entry GTT already gone from Kite)

        Args:
            order: The OpenOrder to check (bound to caller's session)
            session: Caller's DB session (avoids dual-session conflicts)
        """
        try:
            # Check 1: Today's orders for a matching BUY fill
            try:
                all_orders = self.kite.get_all_orders()
                for zerodha_order in all_orders:
                    if (zerodha_order.get('tradingsymbol') == order.script and
                        zerodha_order.get('transaction_type') == 'BUY' and
                        zerodha_order.get('status') == 'COMPLETE' and
                        zerodha_order.get('quantity') == order.quantity):
                        return self._process_triggered_gtt(order, zerodha_order.get('order_id'))
            except Exception as e:
                logger.warning(f"Could not check today's orders for {order.script}: {e}")

            # Check 2: Position already exists (filled on a prior day)
            existing_position = session.query(OpenPosition).filter(
                OpenPosition.script == order.script,
                OpenPosition.status == PositionStatus.OPEN
            ).first()

            if existing_position:
                # Position exists — mark stale order as FILLED (DB only, no GTT changes)
                order.status = OrderStatus.FILLED
                order.position_created = True
                order.fill_price = existing_position.entry_price
                order.filled_at = ist_now_naive()
                session.commit()
                logger.info(
                    f"Stale order reconciled: {order.script} — "
                    f"position already exists (id={existing_position.id})"
                )
                return {
                    'script': order.script,
                    'fill_price': existing_position.entry_price,
                    'quantity': existing_position.quantity,
                    'position_id': existing_position.id,
                    'reconciled': True
                }

            return None

        except Exception as e:
            logger.error(f"Error checking GTT trigger: {e}")
            return None

    def _process_triggered_gtt(self, order: OpenOrder, order_id: str) -> Optional[Dict[str, Any]]:
        """
        Process triggered GTT and create position.
        All DB operations in single transaction (SYS-C1 fix).
        Handles partial fills (TR-H2 fix).
        """
        session = get_session()
        try:
            # Refresh order in this session
            order = session.query(OpenOrder).filter(OpenOrder.id == order.id).first()
            if not order:
                logger.error(f"Order not found in session")
                return None

            # Get order details
            order_status = self.kite.get_order_status(order_id)

            status = order_status.get('status', '')
            if status not in ('COMPLETE', 'CANCELLED'):
                # Still pending or partially filled
                if status == 'OPEN' or status == 'PENDING':
                    return None

            filled_qty = int(order_status.get('filled_quantity', 0))
            ordered_qty = int(order_status.get('quantity', order.quantity))

            if filled_qty == 0:
                logger.warning(f"GTT triggered but no fills for {order.script}")
                return None

            fill_price = float(order_status.get('average_price', order.limit_price))

            # Handle partial fill (TR-H2 fix)
            is_partial_fill = filled_qty < ordered_qty
            if is_partial_fill:
                logger.warning(
                    f"Partial fill for {order.script}: {filled_qty}/{ordered_qty} shares @ {fill_price}"
                )
                telegram.send_alert(
                    f"PARTIAL FILL: {order.script}\n"
                    f"Filled: {filled_qty}/{ordered_qty}\n"
                    f"Unfilled portion will not be tracked.",
                    critical=True
                )

            logger.info(f"Order filled: {order.script} @ {fill_price} x {filled_qty}")

            # Update order - ALL IN SAME SESSION
            order.status = OrderStatus.FILLED
            order.triggered_at = ist_now_naive()
            order.filled_at = ist_now_naive()
            order.fill_price = fill_price
            order.position_created = True

            # Create position - SAME SESSION (SYS-C1 fix)
            position = self._create_position_from_order(
                order, fill_price, filled_qty, session
            )

            if position is None:
                session.rollback()
                return None

            # Commit all changes together
            session.commit()

            # Send alert
            partial_note = f"\n(Partial: {filled_qty}/{ordered_qty})" if is_partial_fill else ""
            telegram.send_alert(
                f"<b>Position Opened</b>\n\n"
                f"{order.script} @ {fill_price:,.2f}\n"
                f"Qty: {filled_qty}{partial_note}\n"
                f"Initial SL: {position.initial_sl:,.2f}"
            )

            return {
                'script': order.script,
                'fill_price': fill_price,
                'quantity': filled_qty,
                'position_id': position.id,
                'partial_fill': is_partial_fill
            }

        except Exception as e:
            logger.error(f"Error processing triggered GTT: {e}")
            session.rollback()
            telegram.send_alert(
                f"ERROR processing fill for {order.script}: {e}",
                critical=True
            )
            return None
        finally:
            session.close()

    def _create_position_from_order(
        self,
        order: OpenOrder,
        fill_price: float,
        fill_qty: int,
        session
    ) -> Optional[OpenPosition]:
        """
        Create position record from filled order.
        Uses provided session for transaction integrity (SYS-C1 fix).

        Args:
            order: The OpenOrder record
            fill_price: Actual fill price
            fill_qty: Actual filled quantity (may be partial)
            session: Database session to use

        Returns:
            OpenPosition if successful, None otherwise
        """
        try:
            # Calculate 20% initial SL
            sl_percent = config.get('trading.initial_sl_percent', 20)
            initial_sl = round_price_down(fill_price * (1 - sl_percent / 100))

            # Use ACTUAL filled quantity for capital calculation
            capital_deployed = fill_price * fill_qty

            position = OpenPosition(
                signal_id=order.signal_id,
                script=order.script,
                entry_date=today_ist(),
                entry_price=fill_price,
                quantity=fill_qty,  # Use actual filled qty, not ordered
                capital_deployed=capital_deployed,
                initial_sl=initial_sl,
                current_sl=initial_sl,
                highest_sl=initial_sl,
                sl_movements=0,
                status=PositionStatus.OPEN,
                created_at=ist_now_naive()
            )

            session.add(position)
            session.flush()  # Get position.id without committing

            # Place protective GTT - pass session for transaction integrity
            from src.core.exit_manager import exit_manager
            gtt_id = exit_manager.place_sl_gtt(position, session=session)

            if gtt_id is None:
                # FIX TR-G1: Position without SL is CRITICAL - mark it clearly
                logger.critical(f"POSITION {order.script} CREATED WITHOUT SL GTT - UNPROTECTED!")
                position.gtt_verified = False
                # Send critical alert for immediate attention
                from src.telegram.bot import telegram
                telegram.send_alert(
                    f"<b>CRITICAL: UNPROTECTED POSITION</b>\n\n"
                    f"{order.script} @ {fill_price:,.2f}\n"
                    f"Qty: {fill_qty}\n\n"
                    f"SL GTT placement FAILED!\n"
                    f"Position has NO stop-loss protection.\n"
                    f"Run /sync or check manually IMMEDIATELY.",
                    critical=True
                )
            else:
                position.gtt_verified = True

            return position

        except Exception as e:
            logger.error(f"Error creating position from order: {e}")
            return None

    def cancel_unfilled_entry_gtts(self) -> int:
        """Cancel all unfilled entry GTTs (month-end cleanup)"""
        session = get_session()
        cancelled = 0

        try:
            pending = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()

            for order in pending:
                try:
                    if order.gtt_id:
                        self.kite.cancel_gtt_order(order.gtt_id)

                    order.status = OrderStatus.EXPIRED
                    cancelled += 1

                    # Update signal
                    signal = session.query(SignalQueue).filter(
                        SignalQueue.id == order.signal_id
                    ).first()
                    if signal:
                        signal.status = SignalStatus.EXPIRED

                except Exception as e:
                    logger.error(f"Failed to cancel GTT {order.gtt_id}: {e}")
                    order.last_error = str(e)

            session.commit()
            logger.info(f"Cancelled {cancelled} unfilled entry GTTs")

        except Exception as e:
            logger.error(f"Error cancelling unfilled GTTs: {e}")
            session.rollback()

        finally:
            session.close()

        return cancelled

    def _get_gtt_expiry(self) -> str:
        """Get GTT expiry date (1 year from now)"""
        expiry = now_ist() + timedelta(days=365)
        return expiry.strftime('%Y-%m-%d %H:%M:%S')

    def _recover_orphaned_gtt(
        self,
        signal_id: int,
        script: str,
        gtt_data: Dict[str, Any],
        session
    ) -> None:
        """
        Recover an orphaned GTT by creating the missing DB record.

        This handles the case where a GTT was placed but the DB commit failed.
        """
        try:
            gtt_id = str(gtt_data.get('id'))
            condition = gtt_data.get('condition', {})
            orders = gtt_data.get('orders', [{}])

            trigger_price = condition.get('trigger_values', [0])[0]
            limit_price = orders[0].get('price', trigger_price)
            quantity = orders[0].get('quantity', 1)
            capital_deployed = quantity * trigger_price

            # Create the missing order record
            order = OpenOrder(
                signal_id=signal_id,
                gtt_id=gtt_id,
                script=script,
                exchange='NSE',
                trigger_price=trigger_price,
                limit_price=limit_price,
                quantity=quantity,
                capital_deployed=capital_deployed,
                status=OrderStatus.PENDING,
                placed_at=ist_now_naive()
            )
            session.add(order)

            # Update signal status
            signal = session.query(SignalQueue).filter(
                SignalQueue.id == signal_id
            ).first()
            if signal:
                signal.status = SignalStatus.ENTERED

            session.commit()

            logger.info(f"Recovered orphaned GTT {gtt_id} for {script}")

            telegram.send_alert(
                f"<b>Recovered Orphaned GTT</b>\n\n"
                f"{script} @ {trigger_price:,.2f}\n"
                f"GTT ID: {gtt_id}\n"
                f"(DB record was missing, now recovered)"
            )

        except Exception as e:
            logger.error(f"Failed to recover orphaned GTT: {e}")
            session.rollback()


# Singleton instance
order_manager = OrderManager()

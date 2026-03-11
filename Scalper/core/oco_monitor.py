"""
NEO Trade Terminal - OCO Position Monitor

Monitors positions with SL/Target orders.
Auto-cancels opposite leg when one is hit.
Keyed by entry_order_id to support multiple brackets on same symbol.
Since NEO doesn't have native OCO, we simulate it.
"""

import threading
import time
import math
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
import logging

from core.tick_utils import calculate_sl_limit_price, round_trigger_price
from core.order_tracker import OrderType, get_order_tracker

logger = logging.getLogger(__name__)


@dataclass
class OCOPair:
    """Represents an OCO pair (SL + Target).

    Target can be either:
    - A limit order (target_order_id is set)
    - LTP monitoring (target_order_id is None, target_price is used)
    """
    entry_order_id: str  # CRITICAL: Unique key for this OCO pair
    symbol: str  # Trading symbol (for display/logging)
    sl_order_id: str
    target_order_id: Optional[str]  # None = LTP monitoring mode (no limit order)
    sl_trigger: float
    target_price: Optional[float]  # Target price (for LTP monitoring or limit order)
    quantity: int
    side: str  # 'LONG' or 'SHORT'
    entry_price: float
    created_at: float


class OCOMonitor:
    """
    Monitors OCO (One-Cancels-Other) order pairs.
    When SL or Target is hit, cancels the other order automatically.

    CRITICAL: Keyed by entry_order_id (not symbol) to support multiple
    bracket orders on the same symbol at different levels.
    """

    def __init__(self, neo_client, telegram: Optional[Any] = None,
                 sound: Optional[Any] = None, pnl_tracker: Optional[Any] = None,
                 cancel_mgr: Optional[Any] = None,
                 position_tracker: Optional[Any] = None):
        self.client = neo_client
        self.telegram = telegram
        self.sound = sound
        self.pnl_tracker = pnl_tracker  # For recording realized P&L on SL/Target hit
        self.cancel_mgr = cancel_mgr  # Centralized cancel manager (race condition prevention)
        self.pos_tracker = position_tracker  # For locking positions during exit
        self.order_tracker = get_order_tracker()  # S35: Order tracking

        self.oco_pairs: Dict[str, OCOPair] = {}  # keyed by entry_order_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._check_interval = 1  # seconds

        # CRITICAL: Health monitoring for daemon thread
        self._last_heartbeat: float = 0.0
        self._heartbeat_timeout: float = 10.0  # Alert if no heartbeat for 10 seconds
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = 5  # Alert after 5 consecutive errors

        # LTP getter callback for target monitoring (when no target order placed)
        self._get_ltp: Optional[Callable[[str], Optional[float]]] = None

        # Callbacks
        self.on_sl_hit: Optional[Callable] = None
        self.on_target_hit: Optional[Callable] = None
        self.on_health_alert: Optional[Callable] = None  # Called when thread appears unhealthy

    def set_pnl_tracker(self, tracker):
        """Set the P&L tracker (can be set after initialization)."""
        self.pnl_tracker = tracker

    def set_cancel_mgr(self, mgr):
        """Set the cancel manager (can be set after initialization)."""
        self.cancel_mgr = mgr

    def set_position_tracker(self, tracker):
        """Set the position tracker (can be set after initialization)."""
        self.pos_tracker = tracker

    def set_ltp_getter(self, get_ltp_func: Callable[[str], Optional[float]]):
        """
        Set LTP getter function for target monitoring.

        When target_order_id is None, OCO monitor will check LTP vs target_price
        and exit at MARKET when target is reached.

        Args:
            get_ltp_func: Function that takes symbol and returns LTP or None
        """
        self._get_ltp = get_ltp_func

    def add_oco_pair(self, entry_order_id: str, symbol: str, sl_order_id: str,
                     target_order_id: Optional[str], sl_trigger: float,
                     target_price: Optional[float], quantity: int, side: str,
                     entry_price: float = 0):
        """
        Register an OCO pair for monitoring.

        CRITICAL: Uses entry_order_id as key (not symbol) to support multiple
        bracket orders on the same symbol at different entry levels.

        Target can be monitored in two ways:
        - If target_order_id is set: Monitor the limit order status
        - If target_order_id is None and target_price is set: Monitor LTP vs target_price

        Args:
            entry_order_id: Entry order ID (UNIQUE key for this OCO pair)
            symbol: Trading symbol of the position
            sl_order_id: SL order ID
            target_order_id: Target order ID (None for LTP monitoring mode)
            sl_trigger: SL trigger price
            target_price: Target price (used for LTP monitoring if target_order_id is None)
            quantity: Position quantity
            side: 'LONG' or 'SHORT'
            entry_price: Entry price for P&L calculation
        """
        with self._lock:
            # Check for existing pair - warn if overwriting
            if entry_order_id in self.oco_pairs:
                old_pair = self.oco_pairs[entry_order_id]
                logger.warning(f"[OCO] OVERWRITING existing pair for entry={entry_order_id}! "
                              f"Old: SL={old_pair.sl_order_id} TGT={old_pair.target_order_id}")
                # Note: This is expected when SL is modified (new order ID)
                # but could indicate a bug if neither order ID matches

            # S35: Validate side parameter
            validated_side = (side.upper() if side else 'LONG')
            if validated_side not in ('LONG', 'SHORT'):
                logger.warning(f"[OCO] Invalid side '{side}' for {symbol}, defaulting to LONG")
                validated_side = 'LONG'

            pair = OCOPair(
                entry_order_id=entry_order_id,
                symbol=symbol,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,
                sl_trigger=sl_trigger,
                target_price=target_price,
                quantity=quantity,
                side=validated_side,
                entry_price=entry_price,
                created_at=time.time()
            )
            self.oco_pairs[entry_order_id] = pair
            logger.info(f"[OCO] Registered: {symbol} (entry={entry_order_id}) SL:{sl_order_id} TGT:{target_order_id}")

    def remove_pair(self, entry_order_id: str):
        """Remove OCO pair from monitoring by entry_order_id."""
        with self._lock:
            if entry_order_id in self.oco_pairs:
                pair = self.oco_pairs[entry_order_id]
                del self.oco_pairs[entry_order_id]
                logger.info(f"[OCO] Removed: {pair.symbol} (entry={entry_order_id})")

    def remove_order(self, order_id: str):
        """Remove OCO pair by order ID (finds pair containing this order)."""
        with self._lock:
            entry_to_remove = None
            for entry_order_id, pair in self.oco_pairs.items():
                if pair.sl_order_id == order_id or pair.target_order_id == order_id:
                    entry_to_remove = entry_order_id
                    break
            if entry_to_remove:
                pair = self.oco_pairs[entry_to_remove]
                del self.oco_pairs[entry_to_remove]
                logger.info(f"[OCO] Removed by order ID {order_id}: {pair.symbol}")

    def remove_pairs_by_symbol(self, symbol: str):
        """Remove all OCO pairs for a symbol."""
        with self._lock:
            entries_to_remove = [
                entry_id for entry_id, pair in self.oco_pairs.items()
                if pair.symbol == symbol
            ]
            for entry_id in entries_to_remove:
                del self.oco_pairs[entry_id]
            if entries_to_remove:
                logger.info(f"[OCO] Removed {len(entries_to_remove)} pairs for {symbol}")

    def update_sl_order(self, entry_order_id: str, new_sl_order_id: str,
                        new_sl_trigger: float = None):
        """Update SL order ID (after trail modification)."""
        with self._lock:
            if entry_order_id in self.oco_pairs:
                self.oco_pairs[entry_order_id].sl_order_id = new_sl_order_id
                if new_sl_trigger is not None:
                    self.oco_pairs[entry_order_id].sl_trigger = new_sl_trigger

    def start(self):
        """Start monitoring thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("[OCO] Monitor started")

    def stop(self):
        """Stop monitoring thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("[OCO] Monitor stopped")

    def _monitor_loop(self):
        """Main monitoring loop with health tracking."""
        while self._running:
            try:
                # Update heartbeat BEFORE processing
                self._last_heartbeat = time.time()

                self._check_orders()

                # Reset error counter on successful cycle
                self._consecutive_errors = 0

            except Exception as e:
                logger.error(f"[OCO] Error in monitor loop: {e}")
                self._consecutive_errors += 1

                # Alert if too many consecutive errors
                if self._consecutive_errors >= self._max_consecutive_errors:
                    logger.error(f"[OCO] CRITICAL: {self._consecutive_errors} consecutive errors!")
                    if self.telegram:
                        self.telegram.send(
                            f"🚨 OCO Monitor: {self._consecutive_errors} consecutive errors!\n"
                            f"Last error: {e}\n"
                            f"OCO cancellations may not be working!"
                        )
                    if self.on_health_alert:
                        self.on_health_alert('OCO', f'Consecutive errors: {self._consecutive_errors}')
                    # Reset counter to avoid spamming
                    self._consecutive_errors = 0

            time.sleep(self._check_interval)

    def is_healthy(self) -> bool:
        """Check if monitor thread is healthy (recent heartbeat)."""
        if not self._running:
            return True  # Not running is not unhealthy
        if self._last_heartbeat == 0:
            return True  # Just started
        return (time.time() - self._last_heartbeat) < self._heartbeat_timeout

    def get_health_status(self) -> Dict[str, Any]:
        """Get detailed health status."""
        return {
            'running': self._running,
            'last_heartbeat': self._last_heartbeat,
            'seconds_since_heartbeat': time.time() - self._last_heartbeat if self._last_heartbeat else 0,
            'consecutive_errors': self._consecutive_errors,
            'is_healthy': self.is_healthy(),
            'active_pairs': len(self.oco_pairs)
        }

    def _check_orders(self):
        """Check status of all OCO pairs.

        CRITICAL: Avoids deadlock by:
        1. Collecting events while holding lock (fast)
        2. Releasing lock before calling external handlers (which acquire other locks)
        3. Re-acquiring lock to clean up pairs
        """
        if not self.oco_pairs:
            return

        try:
            # Get all orders (outside lock - slow API call)
            order_report = self.client.order_report()
            orders_list = order_report.get('data', []) if order_report else []
            # S27/S35: Filter out orders with None/missing/empty nOrdNo to prevent bad keys in dict
            orders = {o.get('nOrdNo'): o for o in orders_list if o.get('nOrdNo') and str(o.get('nOrdNo')).strip()}

            # Step 1: Collect events while holding lock (fast, no external calls)
            sl_hits = []  # List of (pair, sl_order, is_double_exit) tuples
            target_hits = []  # List of (pair, target_order, is_double_exit) tuples
            target_ltp_hits = []  # List of (pair, ltp) - target reached via LTP monitoring (no limit order)
            sl_failures = []  # List of (pair, sl_order, rejection_reason) tuples - CRITICAL
            pairs_to_remove = []
            ltp_check_pairs = []  # S37: Pairs needing LTP check (done outside lock)
            double_exit_alerts = []  # S37: Telegram alerts to send outside lock

            with self._lock:
                for entry_order_id, pair in self.oco_pairs.items():
                    sl_order = orders.get(pair.sl_order_id)
                    target_order = orders.get(pair.target_order_id)

                    sl_status = sl_order.get('ordSt', '').lower() if sl_order else 'unknown'
                    target_status = target_order.get('ordSt', '').lower() if target_order else 'unknown'

                    sl_filled = sl_status in ['complete', 'traded', 'filled']
                    target_filled = target_status in ['complete', 'traded', 'filled']

                    # CRITICAL: Check for double-fill race condition
                    # If both SL AND Target filled (extremely rare but possible in fast markets)
                    if sl_filled and target_filled:
                        logger.error(f"[OCO] CRITICAL: DOUBLE EXIT DETECTED for {pair.symbol} (entry={entry_order_id})! "
                                    f"Both SL and Target filled!")
                        # Record only SL P&L (protective order) - don't double-count
                        sl_hits.append((pair, sl_order, True))  # True = double_exit flag
                        pairs_to_remove.append(entry_order_id)

                        # S37: Collect telegram alerts to send OUTSIDE lock
                        double_exit_alerts.append((pair, sl_order, target_order))
                        continue

                    # Check if SL hit (normal case)
                    if sl_filled:
                        sl_hits.append((pair, sl_order, False))  # False = normal exit
                        pairs_to_remove.append(entry_order_id)

                    # Check if Target hit (normal case - limit order filled)
                    elif target_filled:
                        target_hits.append((pair, target_order, False))
                        pairs_to_remove.append(entry_order_id)

                    # Check if Target reached via LTP monitoring (no target order placed)
                    # S37: Collect pairs needing LTP check, will check OUTSIDE lock
                    elif pair.target_order_id is None and pair.target_price and self._get_ltp:
                        ltp_check_pairs.append((entry_order_id, pair))

                    # CRITICAL: Check if SL cancelled/rejected - attempt recovery!
                    elif sl_status in ['cancelled', 'rejected'] and not target_filled:
                        rej_reason = sl_order.get('rejRsn') or sl_order.get('text') or sl_order.get('remarks') or 'Unknown'
                        # Log full order details for debugging
                        logger.error(f"[OCO] CRITICAL: SL cancelled/rejected for {pair.symbol} (entry={entry_order_id})")
                        logger.error(f"[OCO] SL Order Details: ordSt={sl_status}, rejRsn={sl_order.get('rejRsn')}, "
                                    f"text={sl_order.get('text')}, remarks={sl_order.get('remarks')}, "
                                    f"trgPrc={sl_order.get('trgPrc')}, prc={sl_order.get('prc')}, "
                                    f"qty={sl_order.get('qty')}, exSeg={sl_order.get('exSeg')}, "
                                    f"prd={sl_order.get('prd')}, ordTyp={sl_order.get('ordTyp')}")
                        sl_failures.append((pair, sl_order, rej_reason))
                        # Do NOT remove pair yet - will attempt recovery in step 2

                    elif target_status in ['cancelled', 'rejected'] and not sl_filled:
                        # S37: Don't remove the pair - clear target fields but keep SL monitoring
                        # Otherwise SL hits would go unrecorded (no P&L, no cleanup)
                        logger.info(f"[OCO] Target cancelled/rejected for {pair.symbol} - keeping SL monitoring")
                        pair.target_order_id = None
                        pair.target_price = None

            # S37: LTP checks OUTSIDE lock (calls external _get_ltp)
            for entry_order_id, pair in ltp_check_pairs:
                try:
                    ltp = self._get_ltp(pair.symbol)
                    if ltp is not None and ltp > 0:
                        target_reached = False
                        if pair.side == 'LONG' and ltp >= pair.target_price:
                            target_reached = True
                        elif pair.side == 'SHORT' and ltp <= pair.target_price:
                            target_reached = True

                        if target_reached:
                            logger.info(f"[OCO] TARGET REACHED via LTP: {pair.symbol} LTP={ltp:.2f} >= Target={pair.target_price:.2f}")
                            target_ltp_hits.append((pair, ltp))
                            pairs_to_remove.append(entry_order_id)
                except Exception as e:
                    logger.warning(f"[OCO] LTP check failed for {pair.symbol}: {e}")

            # S37: Send double-exit alerts OUTSIDE lock
            for pair, sl_order, target_order in double_exit_alerts:
                if self.telegram:
                    sl_avg = sl_order.get('avgPrc')
                    tgt_avg = target_order.get('avgPrc')
                    try:
                        sl_price = float(sl_avg) if sl_avg is not None else 0.0
                    except (ValueError, TypeError):
                        sl_price = 0.0
                    try:
                        tgt_price = float(tgt_avg) if tgt_avg is not None else 0.0
                    except (ValueError, TypeError):
                        tgt_price = 0.0
                    self.telegram.send(
                        f"CRITICAL: DOUBLE EXIT on {pair.symbol}!\n"
                        f"Both SL ({sl_price:.2f}) and Target ({tgt_price:.2f}) filled!\n"
                        f"Check broker positions immediately!"
                    )

            # Step 2: Handle events OUTSIDE lock (may call external components)
            for pair, sl_order, is_double_exit in sl_hits:
                self._handle_sl_hit(pair, sl_order, is_double_exit)

            for pair, target_order, is_double_exit in target_hits:
                self._handle_target_hit(pair, target_order, is_double_exit)

            # Handle target reached via LTP monitoring (cancel SL, exit at MARKET)
            for pair, ltp in target_ltp_hits:
                self._handle_target_ltp_hit(pair, ltp)

            # Step 2.5: CRITICAL - Handle SL failures with recovery attempt
            for pair, sl_order, rej_reason in sl_failures:
                recovery_success = self._handle_sl_failure(pair, sl_order, rej_reason)
                if not recovery_success:
                    # Recovery failed - remove pair and alert
                    pairs_to_remove.append(pair.entry_order_id)

            # Step 3: Clean up pairs (re-acquire lock)
            with self._lock:
                for entry_order_id in pairs_to_remove:
                    if entry_order_id in self.oco_pairs:
                        del self.oco_pairs[entry_order_id]

        except Exception as e:
            logger.error(f"[OCO] Check error: {e}")

    def _cancel_order_with_retry(self, order_id: str, order_type: str, max_retries: int = 3) -> bool:
        """Cancel order with retry logic and exponential backoff.

        Uses centralized cancel manager if available to prevent race conditions
        with Position Tracker.

        Args:
            order_id: Order ID to cancel
            order_type: 'SL' or 'Target' for logging
            max_retries: Max number of retry attempts

        Returns:
            True if cancelled successfully, False otherwise
        """
        # Use centralized cancel manager if available (prevents race conditions)
        if self.cancel_mgr:
            success, msg = self.cancel_mgr.cancel_order(
                order_id=order_id,
                reason=f"OCO {order_type}",
                max_retries=max_retries
            )
            if success:
                logger.info(f"[OCO] Cancelled {order_type} order {order_id}: {msg}")
            else:
                logger.error(f"[OCO] Failed to cancel {order_type} order {order_id}: {msg}")
            return success

        # Fallback: direct cancel with retry
        for attempt in range(max_retries):
            try:
                self.client.cancel_order(order_id=order_id)
                logger.info(f"[OCO] Cancelled {order_type} order {order_id}")
                return True
            except Exception as e:
                wait_time = (2 ** attempt) * 0.5  # Exponential backoff: 0.5s, 1s, 2s
                logger.warning(f"[OCO] Cancel {order_type} attempt {attempt + 1} failed: {e}. Retrying in {wait_time}s...")
                if attempt < max_retries - 1:
                    time.sleep(wait_time)

        logger.error(f"[OCO] CRITICAL: Failed to cancel {order_type} order {order_id} after {max_retries} attempts!")
        return False

    def _check_order_status(self, order_id: str) -> str:
        """
        Check the current status of an order.

        Args:
            order_id: Order ID to check

        Returns:
            Order status string (lowercase), or 'unknown' if not found
        """
        try:
            order_report = self.client.order_report()
            orders_list = order_report.get('data', []) if order_report else []
            for order in orders_list:
                if order.get('nOrdNo') == order_id:
                    return order.get('ordSt', 'unknown').lower()
            return 'unknown'
        except Exception as e:
            logger.warning(f"[OCO] Error checking order status for {order_id}: {e}")
            return 'unknown'

    def _handle_sl_failure(self, pair: OCOPair, sl_order: Dict[str, Any],
                           rej_reason: str) -> bool:
        """Handle SL order rejection/cancellation - attempt recovery.

        CRITICAL: Position is UNPROTECTED when SL fails. Must attempt to re-place
        and alert the user immediately.

        Args:
            pair: OCO pair with failed SL
            sl_order: Failed SL order details
            rej_reason: Rejection reason from exchange

        Returns:
            True if recovery successful (new SL placed), False otherwise
        """
        logger.error(f"[OCO] SL FAILURE for {pair.symbol} (entry={pair.entry_order_id}): {rej_reason}")

        # Alert immediately - position is unprotected!
        if self.sound:
            try:
                self.sound.play('error')
            except Exception:
                pass

        if self.telegram:
            self.telegram.send(
                f"🚨 CRITICAL: SL REJECTED for {pair.symbol}!\n"
                f"Reason: {rej_reason}\n"
                f"Position is UNPROTECTED!\n"
                f"Attempting to re-place SL..."
            )

        # Attempt to re-place SL
        sl_tag = None  # S37: Initialize before try block to prevent UnboundLocalError
        try:
            # Get position details from tracker for exchange_segment and product
            position = None
            if self.pos_tracker:
                position = self.pos_tracker.get_position(pair.entry_order_id)

            if not position:
                logger.error(f"[OCO] Cannot recover SL - position not found in tracker: {pair.entry_order_id}")
                if self.telegram:
                    self.telegram.send(f"❌ SL recovery FAILED for {pair.symbol} - position not found!")
                return False

            # Determine exit type based on position side
            exit_type = 'S' if pair.side == 'LONG' else 'B'

            # CRITICAL: ALL OPTIONS (nse_fo, bse_fo) do NOT allow SL-M orders - must use SL-L
            # S29: Kotak NEO rejects SL-M for ALL options, not just BSE
            # S31: Fixed floating-point precision bug using tick_utils
            sl_order_type, sl_limit_price = calculate_sl_limit_price(
                trigger_price=pair.sl_trigger,
                transaction_type=exit_type,
                exchange_segment=position.exchange_segment,
                buffer_percent=0.5
            )
            if sl_order_type == "SL":
                logger.info(f"[OCO] Options detected - using SL-L: trigger={pair.sl_trigger}, limit={sl_limit_price}")

            # S35: Generate unique tag for SL order using tracker
            sl_tag = self.order_tracker.generate_tag(OrderType.SL_RECREATE, pair.symbol)
            self.order_tracker.register_order(
                tag=sl_tag,
                symbol=pair.symbol,
                transaction_type=exit_type,
                quantity=pair.quantity,
                api_order_type=sl_order_type,
                price_type=OrderType.SL_RECREATE
            )

            # Re-place SL order
            # S27: Use explicit None check for product - empty string is falsy
            position_product = position.product if hasattr(position, 'product') and position.product is not None else 'MIS'
            sl_response = self.client.place_order(
                exchange_segment=position.exchange_segment,
                product=position_product,
                price=sl_limit_price,
                order_type=sl_order_type,
                quantity=str(pair.quantity),
                validity="DAY",
                trading_symbol=pair.symbol,
                transaction_type=exit_type,
                amo="NO",
                trigger_price=round_trigger_price(pair.sl_trigger, position.exchange_segment),
                tag=sl_tag  # S35: Use tracker-generated tag
            )

            new_sl_order_id = sl_response.get('nOrdNo') if sl_response else None

            if new_sl_order_id:
                # S35: Confirm order with tracker
                self.order_tracker.confirm_order(sl_tag, new_sl_order_id, sl_response)
                logger.info(f"[OCO] SL RECOVERED for {pair.symbol}: {new_sl_order_id}")

                # Update OCO pair with new SL order ID
                with self._lock:
                    if pair.entry_order_id in self.oco_pairs:
                        self.oco_pairs[pair.entry_order_id].sl_order_id = new_sl_order_id

                # Update position tracker
                if self.pos_tracker:
                    self.pos_tracker.set_sl_order(pair.entry_order_id, new_sl_order_id, pair.sl_trigger)

                if self.telegram:
                    self.telegram.send(f"✅ SL RECOVERED for {pair.symbol} @ {pair.sl_trigger} -> {new_sl_order_id}")

                if self.sound:
                    try:
                        self.sound.play('order_placed')
                    except Exception:
                        pass

                return True
            else:
                # S35: Mark order as failed with tracker
                self.order_tracker.mark_failed(sl_tag, "No order ID returned")
                logger.error(f"[OCO] SL recovery FAILED for {pair.symbol} - no order ID returned")
                if self.telegram:
                    self.telegram.send(f"❌ SL recovery FAILED for {pair.symbol} - no order ID!\nPOSITION IS UNPROTECTED!")
                return False

        except Exception as e:
            # S35: Mark order as failed with tracker
            # S37: Only mark if sl_tag was assigned (may fail before tag generation)
            if sl_tag:
                self.order_tracker.mark_failed(sl_tag, str(e))
            logger.error(f"[OCO] SL recovery exception for {pair.symbol}: {e}")
            if self.telegram:
                self.telegram.send(f"❌ SL recovery FAILED for {pair.symbol}: {e}\nPOSITION IS UNPROTECTED!")
            return False

    def _handle_sl_hit(self, pair: OCOPair, sl_order: Dict[str, Any],
                       is_double_exit: bool = False):
        """Handle SL order hit - cancel target and record P&L.

        Args:
            pair: OCO pair that triggered
            sl_order: SL order details
            is_double_exit: If True, both SL and Target filled (race condition)
        """
        if is_double_exit:
            logger.error(f"[OCO] SL HIT (DOUBLE EXIT) for {pair.symbol} (entry={pair.entry_order_id})")
        else:
            logger.info(f"[OCO] SL HIT for {pair.symbol} (entry={pair.entry_order_id})")

        # CRITICAL: Lock position to prevent Trail Manager from modifying during exit
        if self.pos_tracker:
            self.pos_tracker.lock_for_exit(pair.entry_order_id)

        # Cancel target order with retry (skip if double exit - target already filled)
        # S35-H1: Also skip if target_order_id is None (LTP monitoring mode - no target order exists)
        if is_double_exit:
            logger.warning(f"[OCO] Skipping target cancel for {pair.symbol} - already filled (double exit)")
            cancelled = True  # Consider it "handled"
        elif pair.target_order_id is None:
            logger.info(f"[OCO] No target order to cancel for {pair.symbol} (LTP monitoring mode)")
            cancelled = True  # No order to cancel
        else:
            cancelled = self._cancel_order_with_retry(pair.target_order_id, "Target")
            if not cancelled:
                # Check if target was already filled (race condition detected late)
                target_status = self._check_order_status(pair.target_order_id)
                if target_status in ['complete', 'traded', 'filled']:
                    logger.error(f"[OCO] LATE DOUBLE EXIT: Target {pair.target_order_id} already filled!")
                    if self.telegram:
                        self.telegram.send(
                            f"🚨 DOUBLE EXIT DETECTED (late): {pair.symbol}\n"
                            f"SL hit AND Target was already filled!\n"
                            f"Check broker positions!"
                        )
                    cancelled = True  # Don't alert about orphan - it's a double exit
                else:
                    # CRITICAL: Target order is now orphan - notify immediately
                    logger.error(f"[OCO] ORPHAN TARGET ORDER: {pair.target_order_id} for {pair.symbol}")
                    if self.telegram:
                        self.telegram.send(
                            f"CRITICAL: Failed to cancel target order {pair.target_order_id} "
                            f"for {pair.symbol}. Manual intervention required!"
                        )

        # Calculate P&L
        # S26/S35: Use explicit None checks and safe float/int conversion
        avg_prc = sl_order.get('avgPrc')
        try:
            fill_price = float(avg_prc) if avg_prc is not None else float(pair.sl_trigger or 0)
        except (ValueError, TypeError):
            fill_price = float(pair.sl_trigger or 0)
        fld_qty = sl_order.get('fldQty')
        try:
            filled_qty = int(float(fld_qty)) if fld_qty is not None else pair.quantity
        except (ValueError, TypeError):
            filled_qty = pair.quantity

        if pair.side == 'LONG':
            pnl = (fill_price - pair.entry_price) * filled_qty
            pnl_pct = ((fill_price - pair.entry_price) / pair.entry_price * 100) if pair.entry_price else 0
        else:
            pnl = (pair.entry_price - fill_price) * filled_qty
            pnl_pct = ((pair.entry_price - fill_price) / pair.entry_price * 100) if pair.entry_price else 0

        # Record exit with P&L tracker
        if self.pnl_tracker and fill_price > 0:
            try:
                self.pnl_tracker.record_exit(
                    symbol=pair.symbol,
                    exit_price=fill_price,
                    exit_qty=filled_qty,
                    order_id=pair.sl_order_id,
                    exit_type='SL'
                )
                logger.info(f"[OCO] P&L recorded for SL hit: {pair.symbol} = {pnl:+.2f}")
            except Exception as e:
                logger.warning(f"[OCO] Failed to record P&L: {e}")

        # Notify
        if self.sound:
            self.sound.play('sl_hit')

        if self.telegram:
            self.telegram.notify_sl_hit(
                symbol=pair.symbol,
                exit_price=fill_price,
                pnl=pnl,
                pnl_pct=pnl_pct
            )

        # Callback
        if self.on_sl_hit:
            self.on_sl_hit(pair.symbol, pair.entry_order_id, fill_price, pnl)

    def _handle_target_hit(self, pair: OCOPair, target_order: Dict[str, Any],
                          is_double_exit: bool = False):
        """Handle target order hit - cancel SL and record P&L.

        Args:
            pair: OCO pair that triggered
            target_order: Target order details
            is_double_exit: If True, both SL and Target filled (race condition)
        """
        if is_double_exit:
            logger.error(f"[OCO] TARGET HIT (DOUBLE EXIT) for {pair.symbol} (entry={pair.entry_order_id})")
        else:
            logger.info(f"[OCO] TARGET HIT for {pair.symbol} (entry={pair.entry_order_id})")

        # CRITICAL: Lock position to prevent Trail Manager from modifying during exit
        if self.pos_tracker:
            self.pos_tracker.lock_for_exit(pair.entry_order_id)

        # Cancel SL order with retry (skip if double exit - SL already filled)
        if is_double_exit:
            logger.warning(f"[OCO] Skipping SL cancel for {pair.symbol} - already filled (double exit)")
            cancelled = True  # Consider it "handled"
        else:
            cancelled = self._cancel_order_with_retry(pair.sl_order_id, "SL")
            if not cancelled:
                # Check if SL was already filled (race condition detected late)
                sl_status = self._check_order_status(pair.sl_order_id)
                if sl_status in ['complete', 'traded', 'filled']:
                    logger.error(f"[OCO] LATE DOUBLE EXIT: SL {pair.sl_order_id} already filled!")
                    if self.telegram:
                        self.telegram.send(
                            f"🚨 DOUBLE EXIT DETECTED (late): {pair.symbol}\n"
                            f"Target hit AND SL was already filled!\n"
                            f"Check broker positions!"
                        )
                    cancelled = True  # Don't alert about orphan - it's a double exit
                else:
                    # CRITICAL: SL order is now orphan - notify immediately
                    logger.error(f"[OCO] ORPHAN SL ORDER: {pair.sl_order_id} for {pair.symbol}")
                    if self.telegram:
                        self.telegram.send(
                            f"CRITICAL: Failed to cancel SL order {pair.sl_order_id} "
                            f"for {pair.symbol}. Manual intervention required!"
                        )

        # Calculate P&L
        # S26/S35: Use explicit None checks and safe float/int conversion
        avg_prc = target_order.get('avgPrc')
        try:
            fill_price = float(avg_prc) if avg_prc is not None else float(pair.target_price or 0)
        except (ValueError, TypeError):
            fill_price = float(pair.target_price or 0)
        fld_qty = target_order.get('fldQty')
        try:
            filled_qty = int(float(fld_qty)) if fld_qty is not None else pair.quantity
        except (ValueError, TypeError):
            filled_qty = pair.quantity

        if pair.side == 'LONG':
            pnl = (fill_price - pair.entry_price) * filled_qty
            pnl_pct = ((fill_price - pair.entry_price) / pair.entry_price * 100) if pair.entry_price else 0
        else:
            pnl = (pair.entry_price - fill_price) * filled_qty
            pnl_pct = ((pair.entry_price - fill_price) / pair.entry_price * 100) if pair.entry_price else 0

        # Record exit with P&L tracker
        if self.pnl_tracker and fill_price > 0:
            try:
                self.pnl_tracker.record_exit(
                    symbol=pair.symbol,
                    exit_price=fill_price,
                    exit_qty=filled_qty,
                    order_id=pair.target_order_id,
                    exit_type='TARGET'
                )
                logger.info(f"[OCO] P&L recorded for TARGET hit: {pair.symbol} = {pnl:+.2f}")
            except Exception as e:
                logger.warning(f"[OCO] Failed to record P&L: {e}")

        # Notify
        if self.sound:
            self.sound.play('target_hit')

        if self.telegram:
            self.telegram.notify_target_hit(
                symbol=pair.symbol,
                exit_price=fill_price,
                pnl=pnl,
                pnl_pct=pnl_pct
            )

        # Callback
        if self.on_target_hit:
            self.on_target_hit(pair.symbol, pair.entry_order_id, fill_price, pnl)

    def _handle_target_ltp_hit(self, pair: OCOPair, ltp: float):
        """
        Handle target reached via LTP monitoring (no limit order was placed).

        Actions:
        1. Cancel SL order
        2. Place MARKET exit order
        3. Record P&L
        4. Notify
        """
        logger.info(f"[OCO] TARGET REACHED (LTP): {pair.symbol} LTP={ltp:.2f} Target={pair.target_price:.2f}")

        # CRITICAL: Lock position to prevent Trail Manager from modifying during exit
        if self.pos_tracker:
            self.pos_tracker.lock_for_exit(pair.entry_order_id)

        # Step 1: Cancel SL order
        sl_cancelled = self._cancel_order_with_retry(pair.sl_order_id, "SL")
        if not sl_cancelled:
            # Check if SL was already filled (race condition)
            sl_status = self._check_order_status(pair.sl_order_id)
            if sl_status in ['complete', 'traded', 'filled']:
                logger.error(f"[OCO] SL already filled while target LTP reached: {pair.symbol}")
                if self.telegram:
                    self.telegram.send(
                        f"🚨 DOUBLE EXIT (SL+LTP): {pair.symbol}\n"
                        f"SL filled while LTP hit target!\n"
                        f"Check broker positions!"
                    )
                return  # SL already exited, don't place another exit order
            else:
                logger.error(f"[OCO] Failed to cancel SL {pair.sl_order_id}, but proceeding with exit")

        # Step 2: Place MARKET exit order
        exit_type = 'S' if pair.side == 'LONG' else 'B'

        # Get position details for order placement
        # S35: Use getattr for safety in case position is dict or missing attributes
        position = self.pos_tracker.get_position(pair.entry_order_id) if self.pos_tracker else None
        # S37: Fix operator precedence - add explicit parentheses
        exchange_segment = (getattr(position, 'exchange_segment', None) or 'nse_fo') if position else 'nse_fo'
        product = (getattr(position, 'product', None) or 'MIS') if position else 'MIS'

        try:
            # S35: Generate unique tag for target exit order using tracker
            exit_tag = self.order_tracker.generate_tag(OrderType.TARGET, pair.symbol)
            self.order_tracker.register_order(
                tag=exit_tag,
                symbol=pair.symbol,
                transaction_type=exit_type,
                quantity=pair.quantity,
                api_order_type="MKT",
                price_type=OrderType.TARGET
            )

            logger.info(f"[OCO] Placing MARKET exit: {pair.symbol} {exit_type} qty={pair.quantity}")

            exit_response = self.client.place_order(
                exchange_segment=exchange_segment,
                product=product,
                price="0",  # Market order
                order_type="MKT",
                quantity=str(pair.quantity),
                validity="DAY",
                trading_symbol=pair.symbol,
                transaction_type=exit_type,
                amo="NO",
                tag=exit_tag  # S35: Use tracker-generated tag
            )

            exit_order_id = exit_response.get('nOrdNo') if exit_response else None
            if exit_order_id:
                # S35: Confirm order with tracker
                self.order_tracker.confirm_order(exit_tag, exit_order_id, exit_response)
                logger.info(f"[OCO] MARKET exit placed: {pair.symbol} -> {exit_order_id}")
            else:
                err_msg = exit_response.get('errMsg', 'Unknown') if exit_response else 'No response'
                # S35: Mark order as failed with tracker
                self.order_tracker.mark_failed(exit_tag, err_msg)
                logger.error(f"[OCO] MARKET exit failed: {pair.symbol} - {err_msg}")
                if self.telegram:
                    self.telegram.send(
                        f"🚨 TARGET EXIT FAILED: {pair.symbol}\n"
                        f"LTP hit target but exit order failed!\n"
                        f"Error: {err_msg}\n"
                        f"Manual exit required!"
                    )
                return

        except Exception as e:
            # S35: Mark order as failed with tracker
            self.order_tracker.mark_failed(exit_tag, str(e))
            logger.error(f"[OCO] MARKET exit error: {pair.symbol} - {e}")
            if self.telegram:
                self.telegram.send(
                    f"🚨 TARGET EXIT ERROR: {pair.symbol}\n"
                    f"Error: {e}\n"
                    f"Manual exit required!"
                )
            return

        # Step 3: Calculate and record P&L (use LTP as exit price)
        fill_price = ltp  # Will be updated when order fills, but use LTP for now
        filled_qty = pair.quantity

        if pair.side == 'LONG':
            pnl = (fill_price - pair.entry_price) * filled_qty
            pnl_pct = ((fill_price - pair.entry_price) / pair.entry_price * 100) if pair.entry_price else 0
        else:
            pnl = (pair.entry_price - fill_price) * filled_qty
            pnl_pct = ((pair.entry_price - fill_price) / pair.entry_price * 100) if pair.entry_price else 0

        # Record with P&L tracker
        if self.pnl_tracker and fill_price > 0:
            try:
                self.pnl_tracker.record_exit(
                    symbol=pair.symbol,
                    exit_price=fill_price,
                    exit_qty=filled_qty,
                    order_id=exit_order_id,
                    exit_type='TARGET_LTP'
                )
                logger.info(f"[OCO] P&L recorded for TARGET_LTP: {pair.symbol} = {pnl:+.2f}")
            except Exception as e:
                logger.warning(f"[OCO] Failed to record P&L: {e}")

        # Step 4: Notify
        if self.sound:
            self.sound.play('target_hit')

        if self.telegram:
            self.telegram.notify_target_hit(
                symbol=pair.symbol,
                exit_price=fill_price,
                pnl=pnl,
                pnl_pct=pnl_pct
            )

        # Callback
        if self.on_target_hit:
            self.on_target_hit(pair.symbol, pair.entry_order_id, fill_price, pnl)

    def get_active_pairs(self) -> List[Dict[str, Any]]:
        """Get list of active OCO pairs."""
        with self._lock:
            return [
                {
                    'entry_order_id': p.entry_order_id,
                    'symbol': p.symbol,
                    'sl_order': p.sl_order_id,
                    'target_order': p.target_order_id,
                    'sl_price': p.sl_trigger,
                    'target_price': p.target_price,
                    'side': p.side,
                    'entry_price': p.entry_price,
                }
                for p in self.oco_pairs.values()
            ]

    def get_pair(self, entry_order_id: str) -> Optional[Dict[str, Any]]:
        """Get OCO pair by entry_order_id."""
        with self._lock:
            pair = self.oco_pairs.get(entry_order_id)
            if not pair:
                return None
            return {
                'entry_order_id': pair.entry_order_id,
                'symbol': pair.symbol,
                'sl_order': pair.sl_order_id,
                'target_order': pair.target_order_id,
                'sl_price': pair.sl_trigger,
                'target_price': pair.target_price,
                'side': pair.side,
                'entry_price': pair.entry_price,
            }

    def get_pairs_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Get all OCO pairs for a symbol."""
        with self._lock:
            return [
                {
                    'entry_order_id': p.entry_order_id,
                    'symbol': p.symbol,
                    'sl_order': p.sl_order_id,
                    'target_order': p.target_order_id,
                    'sl_price': p.sl_trigger,
                    'target_price': p.target_price,
                    'side': p.side,
                    'entry_price': p.entry_price,
                }
                for p in self.oco_pairs.values()
                if p.symbol == symbol
            ]

    def is_monitoring(self, entry_order_id: str) -> bool:
        """Check if entry_order_id is being monitored."""
        with self._lock:
            return entry_order_id in self.oco_pairs

    def is_monitoring_symbol(self, symbol: str) -> bool:
        """Check if any position for symbol is being monitored."""
        with self._lock:
            return any(p.symbol == symbol for p in self.oco_pairs.values())

    def update_sl_order_by_symbol(self, symbol: str, new_sl_order_id: str,
                                   new_sl_trigger: float = None):
        """Update SL order for all OCO pairs with matching symbol."""
        with self._lock:
            for entry_id, pair in self.oco_pairs.items():
                if pair.symbol == symbol:
                    pair.sl_order_id = new_sl_order_id
                    if new_sl_trigger is not None:
                        pair.sl_trigger = new_sl_trigger
                    logger.info(f"[OCO] Updated SL order for {symbol}: {new_sl_order_id}")

    def clear_all(self):
        """Clear all OCO pairs."""
        with self._lock:
            self.oco_pairs.clear()
            logger.info("[OCO] Cleared all pairs")

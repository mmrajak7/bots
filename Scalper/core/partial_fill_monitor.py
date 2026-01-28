"""
NEO Trade Terminal - Partial Fill Monitor

Automatically monitors entry orders for partial fills and adjusts
SL/Target order quantities to match the actual filled quantity.

CRITICAL: Without this, a partial entry fill with full SL quantity
can result in exiting more than you own = naked short position.
"""

import threading
import time
import math
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging

from core.tick_utils import calculate_sl_limit_price, round_trigger_price, format_price
from core.order_tracker import OrderType, get_order_tracker

logger = logging.getLogger(__name__)


class EntryOrderStatus(Enum):
    """Entry order fill status."""
    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


@dataclass
class MonitoredEntry:
    """Entry order being monitored for partial fills."""
    symbol: str
    entry_order_id: str
    expected_qty: int
    filled_qty: int = 0
    pending_qty: int = 0
    avg_price: float = 0.0

    # Associated protection orders (PLACED orders)
    sl_order_id: Optional[str] = None
    sl_price: Optional[float] = None
    sl_qty: int = 0  # Current SL order quantity
    target_order_id: Optional[str] = None
    target_price: Optional[float] = None

    # PENDING protection orders (to place when entry fills)
    sl_price_pending: Optional[float] = None  # SL price to place on fill
    target_price_pending: Optional[float] = None  # Target price to place on fill
    instrument_token: str = ""  # For trail manager registration

    # Position details
    side: str = "LONG"  # LONG or SHORT
    exchange_segment: str = "nse_fo"
    product: str = "MIS"

    # State
    status: EntryOrderStatus = EntryOrderStatus.PENDING
    sl_synced: bool = True  # Is SL qty synced with filled qty?
    last_check: float = 0.0
    adjustments_made: int = 0
    created_at: float = field(default_factory=time.time)

    # CRITICAL: Backup tracking for recovery
    sl_order_id_backup: Optional[str] = None  # Original SL ID if cancel succeeded but recreate failed
    sl_retry_count: int = 0  # Number of SL placement retries
    unprotected_since: Optional[float] = None  # Timestamp when position became unprotected


class PartialFillMonitor:
    """
    Monitors entry orders for partial fills and automatically
    adjusts SL/Target quantities to prevent naked positions.

    FLOW:
    1. GUI registers entry order after placement
    2. Monitor polls order status every 500ms
    3. On partial fill detection:
       a. Calculate filled qty
       b. If SL qty > filled qty, adjust SL order
       c. If Target qty > filled qty, adjust Target order
    4. On complete fill, stop monitoring that entry
    5. On cancel/reject, clean up associated orders
    """

    def __init__(self, neo_client, order_manager=None,
                 position_tracker=None, oco_monitor=None,
                 trail_manager=None, telegram=None, sound=None):
        self.client = neo_client
        self.order_mgr = order_manager
        self.pos_tracker = position_tracker
        self.oco_monitor = oco_monitor
        self.trail_mgr = trail_manager
        self.telegram = telegram
        self.sound = sound
        self.order_tracker = get_order_tracker()  # S35: Order tracking

        self.monitored_entries: Dict[str, MonitoredEntry] = {}  # keyed by entry_order_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._check_interval = 0.5  # Check every 500ms for fast detection

        # CRITICAL: Track unprotected positions (filled but no SL)
        # CRITICAL FIX (S24): Key by entry_order_id, NOT symbol!
        # Multiple entries for same symbol can exist (e.g., BUY and SELL orders)
        # Keying by symbol causes entries to overwrite each other, losing sl_price_pending
        self.unprotected_positions: Dict[str, MonitoredEntry] = {}  # entry_order_id -> entry
        self._watchdog_interval = 1.0  # Check unprotected positions every 1 second (was 5s - too slow)
        self._last_watchdog_check = 0.0
        self._max_sl_retries = 5  # Max retries before giving up (increased from 3)

        # Callbacks for GUI updates
        self.on_partial_fill: Optional[Callable] = None
        self.on_complete_fill: Optional[Callable] = None
        self.on_sl_adjusted: Optional[Callable] = None
        self.on_critical_error: Optional[Callable] = None
        self.on_unprotected_position: Optional[Callable] = None  # Called when position is unprotected
        self.on_order_rejected: Optional[Callable] = None  # Called when entry order is rejected
        self.on_protection_orders_placed: Optional[Callable] = None  # Called when SL/Target placed

    def register_entry(self, symbol: str, entry_order_id: str, expected_qty: int,
                       side: str, exchange_segment: str = "nse_fo",
                       product: str = "MIS",
                       sl_order_id: str = None, sl_price: float = None,
                       target_order_id: str = None, target_price: float = None,
                       sl_price_pending: float = None, target_price_pending: float = None,
                       instrument_token: str = "") -> bool:
        """
        Register an entry order for monitoring until filled/cancelled.

        Call this IMMEDIATELY after placing entry order.

        Args:
            symbol: Trading symbol
            entry_order_id: Entry order ID from broker
            expected_qty: Total expected quantity
            side: 'LONG' or 'SHORT'
            exchange_segment: Exchange segment
            product: Product type (MIS/NRML)
            sl_order_id: Associated SL order ID (if already placed)
            sl_price: SL trigger price (if already placed)
            target_order_id: Associated Target order ID (if already placed)
            target_price: Target price (if already placed)
            sl_price_pending: SL price TO PLACE when entry fills
            target_price_pending: Target price TO PLACE when entry fills
            instrument_token: For trail manager registration

        Returns:
            True if registered successfully
        """
        with self._lock:
            entry = MonitoredEntry(
                symbol=symbol,
                entry_order_id=entry_order_id,
                expected_qty=expected_qty,
                side=side.upper(),
                exchange_segment=exchange_segment,
                product=product,
                sl_order_id=sl_order_id,
                sl_price=sl_price,
                sl_qty=expected_qty if sl_order_id else 0,
                target_order_id=target_order_id,
                target_price=target_price,
                sl_price_pending=sl_price_pending,
                target_price_pending=target_price_pending,
                instrument_token=instrument_token,
                pending_qty=expected_qty
            )
            self.monitored_entries[entry_order_id] = entry
            pending_str = ""
            if sl_price_pending:
                pending_str += f" sl_pending={sl_price_pending}"
            if target_price_pending:
                pending_str += f" tgt_pending={target_price_pending}"
            logger.info(f"[PARTIAL] Registered: {symbol} order={entry_order_id} qty={expected_qty}{pending_str}")
            return True

    def update_sl_order(self, entry_order_id: str, sl_order_id: str,
                        sl_price: float, sl_qty: int = None):
        """Update SL order details after it's placed or modified."""
        with self._lock:
            entry = self.monitored_entries.get(entry_order_id)
            if entry:
                entry.sl_order_id = sl_order_id
                entry.sl_price = sl_price
                if sl_qty is not None:
                    entry.sl_qty = sl_qty
                else:
                    entry.sl_qty = entry.filled_qty if entry.filled_qty > 0 else entry.expected_qty
                logger.info(f"[PARTIAL] SL updated: {entry.symbol} sl_order={sl_order_id}")

    def update_target_order(self, entry_order_id: str, target_order_id: str,
                            target_price: float):
        """Update Target order details after it's placed."""
        with self._lock:
            entry = self.monitored_entries.get(entry_order_id)
            if entry:
                entry.target_order_id = target_order_id
                entry.target_price = target_price
                logger.info(f"[PARTIAL] Target updated: {entry.symbol} target_order={target_order_id}")

    def unregister_entry(self, entry_order_id: str):
        """Remove entry from monitoring (on full fill or cancel)."""
        with self._lock:
            if entry_order_id in self.monitored_entries:
                entry = self.monitored_entries[entry_order_id]
                logger.info(f"[PARTIAL] Unregistered: {entry.symbol} order={entry_order_id}")
                del self.monitored_entries[entry_order_id]

    def get_entry_by_symbol(self, symbol: str) -> Optional[MonitoredEntry]:
        """Get monitored entry by symbol (for GUI lookups)."""
        with self._lock:
            for entry in self.monitored_entries.values():
                if entry.symbol == symbol:
                    return entry
            return None

    def cancel_pending_entry(self, symbol: str) -> Optional[str]:
        """
        Cancel pending entry order for a symbol.
        Called when user wants to exit/cancel before entry fills.

        Args:
            symbol: Trading symbol

        Returns:
            Cancelled order ID if found and cancelled, None otherwise
        """
        entry = self.get_entry_by_symbol(symbol)
        if not entry:
            return None

        if entry.status != EntryOrderStatus.PENDING:
            logger.info(f"[PARTIAL] Entry not pending, status={entry.status}: {symbol}")
            return None

        try:
            # Cancel the entry order
            self.client.cancel_order(order_id=entry.entry_order_id)
            logger.info(f"[PARTIAL] Cancelled pending entry: {symbol} order={entry.entry_order_id}")

            # Cleanup
            self.unregister_entry(entry.entry_order_id)
            return entry.entry_order_id

        except Exception as e:
            logger.error(f"[PARTIAL] Failed to cancel entry {entry.entry_order_id}: {e}")
            return None

    def get_all_pending_entries(self) -> List[MonitoredEntry]:
        """Get all entries that are still pending (not filled)."""
        with self._lock:
            return [e for e in self.monitored_entries.values()
                    if e.status == EntryOrderStatus.PENDING]

    def start(self):
        """Start the partial fill monitoring thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("[PARTIAL] Monitor started")

    def stop(self):
        """Stop the monitoring thread."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("[PARTIAL] Monitor stopped")

    def _monitor_loop(self):
        """Main monitoring loop - checks all registered entries."""
        while self._running:
            try:
                self._check_all_entries()

                # CRITICAL: Watchdog checks for unprotected positions and retries SL
                self._watchdog_check_unprotected()

            except Exception as e:
                logger.error(f"[PARTIAL] Monitor error: {e}", exc_info=True)

            time.sleep(self._check_interval)

    def _check_all_entries(self):
        """Check all monitored entries for partial fills.

        CRITICAL FIX (S21-H1): Restructured to make API calls OUTSIDE the lock.
        This prevents UI freeze when GUI thread tries to access partial_fill_monitor
        while slow API calls (modify_order, order_report) are in progress.

        Pattern follows oco_monitor._check_orders:
        1. Collect pending actions while holding lock (fast)
        2. Release lock and execute API calls (slow but non-blocking)
        3. Fire callbacks outside lock
        """
        if not self.monitored_entries:
            return

        # Get all orders in one API call (OUTSIDE lock - slow)
        try:
            order_report = self.client.order_report()
            orders_list = order_report.get('data', []) if order_report else []
            orders_map = {o.get('nOrdNo'): o for o in orders_list}
        except Exception as e:
            logger.warning(f"[PARTIAL] Failed to fetch orders: {e}")
            return

        # === PHASE 1: Collect pending actions while holding lock (fast) ===
        entries_to_remove = []
        sl_adjustments = []  # List of (entry, new_qty, sl_order_data)
        pending_protection = []  # List of (entry, filled_qty, avg_price)
        cancel_protection = []  # List of entry
        callbacks_to_fire = []  # List of (callback_type, args)

        with self._lock:
            for entry_order_id, entry in self.monitored_entries.items():
                order = orders_map.get(entry_order_id)
                if not order:
                    # Order not found - might be new, skip for now
                    continue

                entry.last_check = time.time()

                # Get fill status
                filled_qty = int(order.get('fldQty', 0) or 0)
                total_qty = int(order.get('qty', 0) or 0)
                status = order.get('ordSt', '').lower()
                avg_price = float(order.get('avgPrc', 0) or 0)

                # Determine order status
                if status in ['complete', 'traded', 'filled']:
                    new_status = EntryOrderStatus.COMPLETE
                elif status in ['cancelled', 'canceled']:
                    new_status = EntryOrderStatus.CANCELLED
                elif status in ['rejected']:
                    new_status = EntryOrderStatus.REJECTED
                elif filled_qty > 0 and filled_qty < total_qty:
                    new_status = EntryOrderStatus.PARTIAL
                else:
                    new_status = EntryOrderStatus.PENDING

                # Track changes
                old_filled = entry.filled_qty
                entry.filled_qty = filled_qty
                entry.pending_qty = total_qty - filled_qty
                entry.avg_price = avg_price
                old_status = entry.status
                entry.status = new_status

                # Collect pending actions based on status transitions
                if new_status == EntryOrderStatus.COMPLETE:
                    # Fully filled
                    logger.info(f"[PARTIAL] Complete fill: {entry.symbol} qty={filled_qty} @ {avg_price}")

                    # If SL/Target were pending (not yet placed), queue placement
                    # S26: Use explicit None checks - price of 0 is valid (rare but possible)
                    if entry.sl_price_pending is not None or entry.target_price_pending is not None:
                        pending_protection.append((entry, filled_qty, avg_price))

                    # Verify SL qty if SL was already placed
                    elif entry.sl_order_id and entry.sl_qty != filled_qty:
                        sl_order = orders_map.get(entry.sl_order_id)
                        sl_adjustments.append((entry, filled_qty, sl_order, orders_map))

                    entries_to_remove.append(entry_order_id)

                    if self.on_complete_fill:
                        callbacks_to_fire.append(('complete_fill', (entry.symbol, filled_qty, avg_price, entry)))

                elif new_status == EntryOrderStatus.PARTIAL:
                    # Partial fill detected!
                    if filled_qty > old_filled:
                        logger.warning(f"[PARTIAL] Partial fill: {entry.symbol} "
                                      f"filled={filled_qty}/{total_qty}")

                        # CRITICAL: Queue SL quantity adjustment
                        if entry.sl_order_id:
                            sl_order = orders_map.get(entry.sl_order_id)
                            sl_adjustments.append((entry, filled_qty, sl_order, orders_map))

                        if self.on_partial_fill:
                            callbacks_to_fire.append(('partial_fill', (entry.symbol, filled_qty, total_qty, avg_price)))

                elif new_status in [EntryOrderStatus.CANCELLED, EntryOrderStatus.REJECTED]:
                    # Entry cancelled/rejected - extract and log rejection reason
                    rej_reason = order.get('rejRsn') or order.get('rejectionReason') or \
                                 order.get('text') or order.get('remarks') or \
                                 order.get('errMsg') or order.get('message') or ''

                    # Log full order details for debugging
                    logger.warning(f"[PARTIAL] Order {new_status.name}: {entry.symbol} "
                                  f"order_id={entry_order_id}")
                    if rej_reason:
                        logger.warning(f"[PARTIAL] REJECTION REASON: {rej_reason}")

                    # Log full order data for analysis
                    logger.info(f"[PARTIAL] Full order data: symbol={entry.symbol}, "
                               f"status={status}, filled={filled_qty}/{total_qty}, "
                               f"rejRsn={order.get('rejRsn')}, text={order.get('text')}, "
                               f"remarks={order.get('remarks')}, ordSt={order.get('ordSt')}")

                    if filled_qty > 0:
                        # Partial fill before cancel - SL should match filled qty
                        logger.warning(f"[PARTIAL] Entry cancelled with partial fill: "
                                      f"{entry.symbol} filled={filled_qty}")
                        if entry.sl_order_id and entry.sl_qty != filled_qty:
                            sl_order = orders_map.get(entry.sl_order_id)
                            sl_adjustments.append((entry, filled_qty, sl_order, orders_map))
                    else:
                        # No fill - queue SL and Target cancellation
                        logger.info(f"[PARTIAL] Entry cancelled (no fill): {entry.symbol}")
                        cancel_protection.append(entry)

                    # Queue callback
                    if self.on_order_rejected:
                        callbacks_to_fire.append(('order_rejected', (entry.symbol, rej_reason or status)))

                    entries_to_remove.append(entry_order_id)

        # === PHASE 2: Execute API calls OUTSIDE lock (slow but non-blocking) ===

        # Place pending protection orders
        for entry, filled_qty, avg_price in pending_protection:
            try:
                self._place_pending_protection_orders(entry, filled_qty, avg_price)
            except Exception as e:
                logger.error(f"[PARTIAL] Failed to place protection orders for {entry.symbol}: {e}")

        # Adjust SL quantities
        for entry, new_qty, sl_order, orders_map_ref in sl_adjustments:
            try:
                self._adjust_sl_quantity(entry, new_qty, orders_map_ref)
            except Exception as e:
                logger.error(f"[PARTIAL] Failed to adjust SL qty for {entry.symbol}: {e}")

        # Cancel protection orders for cancelled entries
        for entry in cancel_protection:
            try:
                self._cancel_protection_orders(entry)
            except Exception as e:
                logger.error(f"[PARTIAL] Failed to cancel protection orders for {entry.symbol}: {e}")

        # === PHASE 3: Fire callbacks OUTSIDE lock ===
        for callback_type, args in callbacks_to_fire:
            try:
                if callback_type == 'complete_fill' and self.on_complete_fill:
                    self.on_complete_fill(*args)
                elif callback_type == 'partial_fill' and self.on_partial_fill:
                    self.on_partial_fill(*args)
                elif callback_type == 'order_rejected' and self.on_order_rejected:
                    self.on_order_rejected(*args)
            except Exception as e:
                logger.error(f"[PARTIAL] Callback {callback_type} failed: {e}")

        # Clean up completed/cancelled entries
        for entry_id in entries_to_remove:
            self.unregister_entry(entry_id)

    def _verify_sl_qty_modification(self, order_id: str, expected_qty: int,
                                     max_retries: int = 2) -> Optional[int]:
        """
        Verify SL order qty modification took effect at broker.

        CRITICAL: Prevents mismatch between code state and broker reality.

        Args:
            order_id: SL order ID
            expected_qty: Expected quantity after modification
            max_retries: Number of times to retry verification (allows for broker delay)

        Returns:
            Actual order quantity at broker, or None if verification failed
        """
        for attempt in range(max_retries):
            try:
                # Small delay to allow broker to process modification
                if attempt > 0:
                    time.sleep(0.3)

                order_report = self.client.order_report()
                orders = order_report.get('data', []) if order_report else []

                for o in orders:
                    if o.get('nOrdNo') == order_id:
                        actual_qty = int(o.get('qty', 0) or 0)
                        status = o.get('ordSt', '').lower()

                        # If order is no longer pending, qty verification is moot
                        if status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                            logger.warning(f"[PARTIAL] SL order {order_id} is {status}, "
                                         f"verification moot")
                            return expected_qty  # Assume intended qty was used

                        if actual_qty == expected_qty:
                            logger.debug(f"[PARTIAL] Verified SL qty: {order_id} = {actual_qty}")
                            return actual_qty
                        else:
                            # Qty differs - try again (broker might be slow)
                            logger.warning(f"[PARTIAL] SL qty verification attempt {attempt + 1}: "
                                         f"expected={expected_qty}, actual={actual_qty}")
                            continue

                logger.warning(f"[PARTIAL] SL order {order_id} not found in order report")
                return None

            except Exception as e:
                logger.error(f"[PARTIAL] SL qty verification error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(0.3)

        return None

    def _adjust_sl_quantity(self, entry: MonitoredEntry, new_qty: int,
                            orders_map: Dict[str, Any]) -> bool:
        """
        Adjust SL order quantity to match filled entry quantity.

        CRITICAL: This prevents naked shorts from SL triggering for more
        than the actual position size.

        Returns:
            True if adjustment successful
        """
        if not entry.sl_order_id or new_qty <= 0:
            return False

        # Check if adjustment is needed
        if entry.sl_qty == new_qty:
            entry.sl_synced = True
            return True

        logger.info(f"[PARTIAL] Adjusting SL qty: {entry.symbol} "
                   f"{entry.sl_qty} -> {new_qty}")

        # First, check SL order status
        sl_order = orders_map.get(entry.sl_order_id)
        if sl_order:
            sl_status = sl_order.get('ordSt', '').lower()

            # If SL already filled/cancelled, can't modify
            if sl_status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                logger.warning(f"[PARTIAL] SL order {entry.sl_order_id} "
                              f"already {sl_status}, cannot adjust")
                entry.sl_synced = False
                return False

        # Try to modify SL quantity
        try:
            # Get order_type, price, trigger_price from sl_order (required by NEO API)
            sl_order_type = sl_order.get('prcTp', sl_order.get('orderType', 'SL')) if sl_order else 'SL'
            sl_order_price = sl_order.get('prc', sl_order.get('price', '0')) if sl_order else '0'
            sl_trigger_price = sl_order.get('trgPrc', sl_order.get('triggerPrice', str(entry.sl_price))) if sl_order else str(entry.sl_price)

            self.client.modify_order(
                order_id=entry.sl_order_id,
                order_type=sl_order_type,
                price=str(sl_order_price),
                quantity=str(new_qty),
                trigger_price=str(sl_trigger_price),
                validity="DAY"
            )

            # CRITICAL: Verify the modification actually took effect at broker
            verified_qty = self._verify_sl_qty_modification(entry.sl_order_id, new_qty)

            if verified_qty is None:
                # Verification failed - could be timeout or order state changed
                logger.error(f"[PARTIAL] SL qty modification verification FAILED for {entry.symbol}")
                entry.sl_synced = False
                return self._recreate_sl_order(entry, new_qty)

            if verified_qty != new_qty:
                # Broker has different qty than expected
                logger.error(f"[PARTIAL] SL qty MISMATCH for {entry.symbol}: "
                           f"expected={new_qty}, actual={verified_qty}")
                entry.sl_qty = verified_qty  # Update to actual broker qty
                entry.sl_synced = (verified_qty == entry.filled_qty)

                if self.telegram:
                    self.telegram.send(
                        f"⚠️ SL QTY MISMATCH: {entry.symbol}\n"
                        f"Expected: {new_qty}, Actual: {verified_qty}\n"
                        f"Filled qty: {entry.filled_qty}"
                    )
                return entry.sl_synced

            # Verified successfully
            old_qty = entry.sl_qty
            entry.sl_qty = new_qty
            entry.sl_synced = True
            entry.adjustments_made += 1

            logger.info(f"[PARTIAL] SL qty adjusted: {entry.symbol} "
                       f"{old_qty} -> {new_qty} (verified)")

            # Notify
            if self.on_sl_adjusted:
                self.on_sl_adjusted(entry.symbol, old_qty, new_qty)

            if self.telegram:
                self.telegram.send(
                    f"PARTIAL FILL: {entry.symbol}\n"
                    f"Entry filled: {new_qty}/{entry.expected_qty}\n"
                    f"SL adjusted to {new_qty} qty"
                )

            return True

        except Exception as modify_e:
            logger.warning(f"[PARTIAL] SL modify failed: {modify_e}, "
                          f"attempting cancel + recreate")

            # Fallback: Cancel old SL and create new one
            return self._recreate_sl_order(entry, new_qty)

    def _recreate_sl_order(self, entry: MonitoredEntry, new_qty: int) -> bool:
        """
        Create new SL first, then cancel old one (if new SL succeeds).

        Used when modify_order fails (some brokers don't support qty modification).

        CRITICAL FIX: Place new SL FIRST, then cancel old. This prevents
        the position from being unprotected even momentarily.
        """
        old_sl_id = entry.sl_order_id

        try:
            # Determine transaction type for SL (opposite of position)
            sl_txn_type = 'S' if entry.side == 'LONG' else 'B'

            # CRITICAL: ALL OPTIONS (nse_fo, bse_fo) do NOT allow SL-M orders - must use SL-L
            # S29: Kotak NEO rejects SL-M for ALL options, not just BSE
            # S31: Fixed floating-point precision bug using tick_utils
            sl_order_type, sl_limit_price = calculate_sl_limit_price(
                trigger_price=entry.sl_price,
                transaction_type=sl_txn_type,
                exchange_segment=entry.exchange_segment,
                buffer_percent=0.5
            )
            if sl_order_type == "SL":
                logger.info(f"[PARTIAL] Options detected - using SL-L for recreation: trigger={entry.sl_price}, limit={sl_limit_price}")

            # S35: Generate unique tag for SL order using tracker
            sl_tag = self.order_tracker.generate_tag(OrderType.SL_RECREATE, entry.symbol)
            self.order_tracker.register_order(
                tag=sl_tag,
                symbol=entry.symbol,
                transaction_type=sl_txn_type,
                quantity=new_qty,
                api_order_type=sl_order_type,
                price_type=OrderType.SL_RECREATE
            )

            # CRITICAL: Create new SL FIRST (before cancelling old)
            # This ensures position is never unprotected
            new_sl_response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product=entry.product,
                price=sl_limit_price,
                order_type=sl_order_type,
                quantity=str(new_qty),
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=sl_txn_type,
                amo="NO",
                trigger_price=round_trigger_price(entry.sl_price, entry.exchange_segment),
                tag=sl_tag  # S35: Use tracker-generated tag
            )

            new_sl_id = new_sl_response.get('nOrdNo') if new_sl_response else None

            if new_sl_id:
                # S35: Confirm order with tracker
                self.order_tracker.confirm_order(sl_tag, new_sl_id, new_sl_response)
                logger.info(f"[PARTIAL] New SL placed: {new_sl_id} qty={new_qty}")

                # NOW cancel old SL (after new one is confirmed)
                try:
                    self.client.cancel_order(order_id=old_sl_id)
                    logger.info(f"[PARTIAL] Cancelled old SL: {old_sl_id}")
                except Exception as cancel_err:
                    # Old SL cancel failed but we have new SL - this is acceptable
                    # The old SL might already be filled/cancelled
                    logger.warning(f"[PARTIAL] Old SL cancel failed (may be already executed): {cancel_err}")

                # S27: CRITICAL - Wrap state modifications in lock to prevent race with watchdog
                with self._lock:
                    old_qty = entry.sl_qty
                    entry.sl_order_id = new_sl_id
                    entry.sl_order_id_backup = None  # Clear backup - we have a new SL
                    entry.sl_qty = new_qty
                    entry.sl_synced = True
                    entry.adjustments_made += 1
                    entry.unprotected_since = None  # No longer unprotected

                    # Remove from unprotected list if present (keyed by entry_order_id)
                    if entry.entry_order_id in self.unprotected_positions:
                        del self.unprotected_positions[entry.entry_order_id]

                logger.info(f"[PARTIAL] SL recreated: {entry.symbol} "
                           f"new_order={new_sl_id} qty={new_qty}")

                # Update position tracker if available
                if self.pos_tracker:
                    self.pos_tracker.update_sl_order(
                        entry.entry_order_id, new_sl_id, entry.sl_price
                    )

                # Update OCO monitor if available
                if self.oco_monitor:
                    self.oco_monitor.update_sl_order(
                        entry.entry_order_id, new_sl_id, entry.sl_price
                    )

                if self.on_sl_adjusted:
                    self.on_sl_adjusted(entry.symbol, old_qty, new_qty)

                return True
            else:
                # CRITICAL: SL recreation failed - position is unprotected!
                self.order_tracker.mark_failed(sl_tag, "No order ID returned")
                self._mark_position_unprotected(entry, new_qty, "SL recreation returned no order ID")
                return False

        except Exception as e:
            logger.error(f"[PARTIAL] SL recreate failed: {e}")
            self.order_tracker.mark_failed(sl_tag, str(e))
            self._mark_position_unprotected(entry, new_qty, str(e))
            return False

    def _mark_position_unprotected(self, entry: MonitoredEntry, qty: int, reason: str):
        """Mark a position as unprotected and track for recovery."""
        entry.sl_synced = False
        entry.sl_order_id = None  # Clear - no valid SL exists
        entry.unprotected_since = time.time()
        entry.sl_retry_count += 1

        # Add to unprotected tracking (with lock for thread safety)
        # CRITICAL FIX (S24): Key by entry_order_id to prevent symbol collision
        with self._lock:
            self.unprotected_positions[entry.entry_order_id] = entry

        # DIAGNOSTIC: Log sl_price_pending to debug watchdog recovery issues
        logger.error(f"[PARTIAL] CRITICAL: Position UNPROTECTED - {entry.symbol} "
                    f"qty={qty}, reason={reason}, retries={entry.sl_retry_count}, "
                    f"sl_price_pending={entry.sl_price_pending}, side={entry.side}, "
                    f"entry_order_id={entry.entry_order_id}")

        if self.on_critical_error:
            self.on_critical_error(
                entry.symbol,
                f"SL failed ({reason}). Position has {qty} qty but NO SL protection!"
            )

        if self.on_unprotected_position:
            self.on_unprotected_position(entry.symbol, qty, reason)

        if self.telegram:
            self.telegram.send(
                f"🚨 CRITICAL: SL FAILED for {entry.symbol}!\n"
                f"Position qty: {qty}\n"
                f"Reason: {reason}\n"
                f"SL: NONE - UNPROTECTED!\n"
                f"Retry #{entry.sl_retry_count} will be attempted..."
            )

        if self.sound:
            self.sound.play('error')

    def _watchdog_check_unprotected(self):
        """
        Watchdog: Check for unprotected positions and attempt recovery.

        Called periodically from the monitor loop.
        """
        current_time = time.time()

        # Only run every _watchdog_interval seconds
        if current_time - self._last_watchdog_check < self._watchdog_interval:
            return

        self._last_watchdog_check = current_time

        with self._lock:
            if not self.unprotected_positions:
                return

            logger.warning(f"[WATCHDOG] Checking {len(self.unprotected_positions)} unprotected positions")

            # CRITICAL FIX (S24): Iterate by entry_order_id, not symbol
            for entry_order_id, entry in list(self.unprotected_positions.items()):
                symbol = entry.symbol  # For logging

                # Check if max retries exceeded
                if entry.sl_retry_count >= self._max_sl_retries:
                    unprotected_duration = current_time - (entry.unprotected_since or current_time)
                    logger.error(f"[WATCHDOG] {symbol}: Max retries ({self._max_sl_retries}) exceeded! "
                                f"Unprotected for {unprotected_duration:.0f}s")

                    # Send periodic reminder (every 30 seconds)
                    if int(unprotected_duration) % 30 == 0:
                        if self.telegram:
                            self.telegram.send(
                                f"🚨🚨 URGENT: {symbol} STILL UNPROTECTED!\n"
                                f"Duration: {unprotected_duration:.0f}s\n"
                                f"Manual intervention required!\n"
                                f"Place SL manually or exit position!"
                            )
                    continue

                # Attempt to place SL
                logger.info(f"[WATCHDOG] Attempting SL recovery for {symbol} "
                           f"(retry {entry.sl_retry_count + 1}/{self._max_sl_retries})")

                success = self._retry_sl_placement(entry)

                if success:
                    logger.info(f"[WATCHDOG] SL recovery SUCCESS for {symbol}")
                    del self.unprotected_positions[entry_order_id]

                    if self.telegram:
                        self.telegram.send(
                            f"✅ SL RECOVERED for {symbol}!\n"
                            f"Position now protected."
                        )
                else:
                    # NOTE: Retry count already incremented inside _retry_sl_placement
                    # Removed duplicate increment here (S25 fix for PFM-8)
                    logger.warning(f"[WATCHDOG] SL recovery FAILED for {symbol} "
                                  f"(retry {entry.sl_retry_count}/{self._max_sl_retries})")

    def _check_existing_sl_for_symbol(self, symbol: str, side: str) -> bool:
        """
        Check if there's already an SL order for the given symbol.

        CRITICAL FIX (T7): Prevents watchdog from placing duplicate SL if user already
        placed one manually.

        Args:
            symbol: Trading symbol
            side: Position side ('LONG' or 'SHORT')

        Returns:
            True if SL order exists for this symbol, False otherwise
        """
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            # Determine expected SL transaction type (opposite of position)
            sl_txn_type = 'S' if side == 'LONG' else 'B'

            for order in orders:
                order_symbol = order.get('trdSym', '') or order.get('tradingSymbol', '')
                order_type = order.get('ordTyp', '').upper()
                txn_type = order.get('trnsTp', '').upper()
                status = order.get('ordSt', '').lower()

                # Check if it's an SL order for this symbol
                if (order_symbol == symbol and
                    order_type in ['SL-M', 'SL', 'SL-L'] and
                    txn_type == sl_txn_type and
                    status in ['open', 'pending', 'trigger pending', 'after market order req received']):
                    logger.info(f"[WATCHDOG] Found existing SL order for {symbol}: {order.get('nOrdNo')}")
                    return True

            return False

        except Exception as e:
            logger.error(f"[WATCHDOG] Error checking existing SL orders: {e}")
            # On error, be conservative - return False to allow SL placement
            return False

    def _get_broker_position_qty(self, symbol: str, side: str) -> int:
        """Get actual position quantity from broker for a symbol."""
        try:
            positions = self.client.positions()
            pos_list = positions.get('data', []) if positions else []

            for pos in pos_list:
                pos_symbol = pos.get('trdSym', '') or pos.get('tradingSymbol', '')
                if pos_symbol == symbol:
                    buy_qty = int(float(pos.get('flBuyQty', 0) or 0))
                    sell_qty = int(float(pos.get('flSellQty', 0) or 0))
                    net_qty = buy_qty - sell_qty
                    # For LONG, net_qty should be positive; for SHORT, negative
                    if side == 'LONG' and net_qty > 0:
                        return net_qty
                    elif side == 'SHORT' and net_qty < 0:
                        return abs(net_qty)
            return 0
        except Exception as e:
            logger.error(f"[WATCHDOG] Error fetching broker position: {e}")
            return 0

    def _retry_sl_placement(self, entry: MonitoredEntry) -> bool:
        """Attempt to place SL for an unprotected position."""
        # CRITICAL FIX: Use sl_price OR sl_price_pending (pending price is set before SL placement)
        # S26: Use explicit None checks - price of 0 is valid (rare but possible)
        sl_price_to_use = entry.sl_price if entry.sl_price is not None else entry.sl_price_pending

        # S31: If filled_qty is 0 (WebSocket missed fill), check broker positions
        filled_qty = entry.filled_qty
        if filled_qty <= 0:
            broker_qty = self._get_broker_position_qty(entry.symbol, entry.side)
            if broker_qty > 0:
                logger.info(f"[WATCHDOG] {entry.symbol}: WebSocket missed fill, "
                           f"broker shows qty={broker_qty}")
                entry.filled_qty = broker_qty
                filled_qty = broker_qty

        if sl_price_to_use is None or filled_qty <= 0:
            logger.error(f"[WATCHDOG] Cannot retry SL - missing price or qty for {entry.symbol} "
                        f"(sl_price={entry.sl_price}, sl_price_pending={entry.sl_price_pending}, qty={filled_qty})")
            return False

        # CRITICAL FIX (T7): Check if user already placed SL manually before watchdog places another
        # This prevents duplicate SL orders when user reacts faster than watchdog
        if self._check_existing_sl_for_symbol(entry.symbol, entry.side):
            logger.info(f"[WATCHDOG] {entry.symbol}: SL already exists (user placed manually), "
                       f"marking as protected")
            entry.sl_synced = True
            entry.unprotected_since = None
            return True

        try:
            sl_txn_type = 'S' if entry.side == 'LONG' else 'B'

            # CRITICAL: ALL OPTIONS (nse_fo, bse_fo) do NOT allow SL-M orders - must use SL-L
            # S29: Kotak NEO rejects SL-M for ALL options, not just BSE
            # S31: Fixed floating-point precision bug using tick_utils
            sl_order_type, sl_limit_price = calculate_sl_limit_price(
                trigger_price=sl_price_to_use,
                transaction_type=sl_txn_type,
                exchange_segment=entry.exchange_segment,
                buffer_percent=0.5
            )
            if sl_order_type == "SL":
                logger.info(f"[WATCHDOG] Options detected - using SL-L: trigger={sl_price_to_use}, limit={sl_limit_price}")

            # S35: Generate unique tag for SL order using tracker
            sl_tag = self.order_tracker.generate_tag(OrderType.SL_RECREATE, entry.symbol)
            self.order_tracker.register_order(
                tag=sl_tag,
                symbol=entry.symbol,
                transaction_type=sl_txn_type,
                quantity=filled_qty,
                api_order_type=sl_order_type,
                price_type=OrderType.SL_RECREATE
            )

            new_sl_response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product=entry.product,
                price=sl_limit_price,
                order_type=sl_order_type,
                quantity=str(filled_qty),  # Use local var (may be from broker check)
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=sl_txn_type,
                amo="NO",
                trigger_price=round_trigger_price(sl_price_to_use, entry.exchange_segment),
                tag=sl_tag  # S35: Use tracker-generated tag
            )

            new_sl_id = new_sl_response.get('nOrdNo') if new_sl_response else None

            if new_sl_id:
                # S35: Confirm order with tracker
                self.order_tracker.confirm_order(sl_tag, new_sl_id, new_sl_response)
                entry.sl_order_id = new_sl_id
                entry.sl_price = sl_price_to_use  # Set actual price used
                entry.sl_qty = filled_qty  # Use local var (may be from broker check)
                entry.sl_synced = True
                entry.unprotected_since = None

                logger.info(f"[WATCHDOG] SL placed: {entry.symbol} "
                           f"order={new_sl_id} price={sl_price_to_use} qty={filled_qty}")

                # Update trackers
                if self.pos_tracker:
                    self.pos_tracker.update_sl_order(entry.entry_order_id, new_sl_id, sl_price_to_use)

                if self.oco_monitor and entry.target_order_id:
                    self.oco_monitor.update_sl_order(entry.entry_order_id, new_sl_id, sl_price_to_use)

                return True
            else:
                self.order_tracker.mark_failed(sl_tag, "No order ID returned")
                entry.sl_retry_count += 1
                return False

        except Exception as e:
            logger.error(f"[WATCHDOG] SL retry failed: {e}")
            self.order_tracker.mark_failed(sl_tag, str(e))
            entry.sl_retry_count += 1
            return False

    def get_unprotected_positions(self) -> List[Dict[str, Any]]:
        """Get list of unprotected positions for GUI display."""
        with self._lock:
            result = []
            # CRITICAL FIX (S24): Iterate by entry_order_id, get symbol from entry
            for entry_order_id, entry in self.unprotected_positions.items():
                duration = time.time() - (entry.unprotected_since or time.time())
                result.append({
                    'symbol': entry.symbol,
                    'entry_order_id': entry_order_id,
                    'qty': entry.filled_qty,
                    'sl_price': entry.sl_price,
                    'sl_price_pending': entry.sl_price_pending,  # For debugging
                    'unprotected_duration': duration,
                    'retry_count': entry.sl_retry_count,
                    'max_retries': self._max_sl_retries
                })
            return result

    def _place_pending_protection_orders(self, entry: MonitoredEntry,
                                          filled_qty: int, avg_price: float):
        """
        Place SL and Target orders that were pending until entry filled.

        Called when entry order fills and we have sl_price_pending/target_price_pending.
        """
        logger.info(f"[PARTIAL] Placing pending protection orders for {entry.symbol}")

        # S31: Brief delay after fill detection to allow broker to process
        # Watchdog succeeds 2s later when initial fails - timing issue
        time.sleep(0.3)

        sl_order_id = None
        target_order_id = None
        is_long = entry.side == 'LONG'
        exit_type = 'S' if is_long else 'B'

        # Place SL order with retry logic (CRITICAL)
        if entry.sl_price_pending:
            max_sl_retries = 3
            sl_placed = False

            # CRITICAL: ALL OPTIONS (nse_fo, bse_fo) do NOT allow SL-M orders - must use SL-L
            # S29: Kotak NEO rejects SL-M for ALL options, not just BSE
            # S31: Fixed floating-point precision bug using tick_utils
            sl_order_type, sl_limit_price = calculate_sl_limit_price(
                trigger_price=entry.sl_price_pending,
                transaction_type=exit_type,
                exchange_segment=entry.exchange_segment,
                buffer_percent=0.5
            )
            if sl_order_type == "SL":
                logger.info(f"[PARTIAL] Options detected - using SL-L: trigger={entry.sl_price_pending}, limit={sl_limit_price}")

            for attempt in range(max_sl_retries):
                try:
                    # S35: Generate unique tag for SL order using tracker
                    sl_tag = self.order_tracker.generate_tag(OrderType.SL, entry.symbol)
                    self.order_tracker.register_order(
                        tag=sl_tag,
                        symbol=entry.symbol,
                        transaction_type=exit_type,
                        quantity=filled_qty,
                        api_order_type=sl_order_type,
                        price_type=OrderType.SL
                    )

                    sl_response = self.client.place_order(
                        exchange_segment=entry.exchange_segment,
                        product=entry.product,
                        price=sl_limit_price,
                        order_type=sl_order_type,
                        quantity=str(filled_qty),
                        validity="DAY",
                        trading_symbol=entry.symbol,
                        transaction_type=exit_type,
                        amo="NO",
                        trigger_price=round_trigger_price(entry.sl_price_pending, entry.exchange_segment),
                        tag=sl_tag  # S35: Use tracker-generated tag
                    )
                    sl_order_id = sl_response.get('nOrdNo') if sl_response else None

                    if sl_order_id:
                        # S35: Confirm order with tracker
                        self.order_tracker.confirm_order(sl_tag, sl_order_id, sl_response)
                        entry.sl_order_id = sl_order_id
                        entry.sl_price = entry.sl_price_pending
                        entry.sl_qty = filled_qty
                        logger.info(f"[PARTIAL] SL placed: {entry.symbol} @ {entry.sl_price_pending} -> {sl_order_id}")
                        sl_placed = True
                        break
                    else:
                        # S31: Log actual API response for debugging
                        # S35: Mark order as failed with tracker
                        err_msg = sl_response.get('errMsg', 'Unknown') if sl_response else 'No response'
                        stat_msg = sl_response.get('stat', '') if sl_response else ''
                        self.order_tracker.mark_failed(sl_tag, f"{err_msg} | {stat_msg}")
                        logger.warning(f"[PARTIAL] SL placement attempt {attempt+1}/{max_sl_retries} failed for {entry.symbol}: {err_msg} | {stat_msg}")
                        logger.debug(f"[PARTIAL] SL response: {sl_response}")
                        if attempt < max_sl_retries - 1:
                            time.sleep(0.5 * (attempt + 1))  # Backoff: 0.5s, 1s

                except Exception as e:
                    # S35: Mark order as failed with tracker
                    self.order_tracker.mark_failed(sl_tag, str(e))
                    logger.error(f"[PARTIAL] SL placement attempt {attempt+1} error: {e}")
                    if attempt < max_sl_retries - 1:
                        time.sleep(0.5 * (attempt + 1))

            if not sl_placed:
                logger.error(f"[PARTIAL] CRITICAL: SL placement FAILED after {max_sl_retries} attempts for {entry.symbol}!")
                # Mark as unprotected for watchdog recovery
                self._mark_position_unprotected(entry, filled_qty, f"Initial SL placement failed after {max_sl_retries} attempts")
                if self.telegram:
                    self.telegram.send(f"🚨 CRITICAL: SL placement FAILED for {entry.symbol} after {max_sl_retries} retries! Position UNPROTECTED!")
                if self.on_critical_error:
                    self.on_critical_error(entry.symbol, "SL placement failed on entry fill")

        # Store Target price for monitoring (DON'T place limit order to avoid margin issues)
        # Target will be monitored and exited at MARKET when LTP reaches target
        if entry.target_price_pending:
            entry.target_price = entry.target_price_pending
            logger.info(f"[PARTIAL] Target monitoring enabled: {entry.symbol} @ {entry.target_price:.2f} (will exit at MARKET when reached)")
            target_order_id = None  # No limit order placed

        # Register with position tracker
        if self.pos_tracker and (sl_order_id or target_order_id):
            self.pos_tracker.add_position(
                symbol=entry.symbol,
                entry_order_id=entry.entry_order_id,
                exchange_segment=entry.exchange_segment,
                quantity=filled_qty,
                side=entry.side,
                entry_price=avg_price,
                product=entry.product  # For SL recovery
            )
            if sl_order_id:
                self.pos_tracker.set_sl_order(entry.entry_order_id, sl_order_id, entry.sl_price)
            if target_order_id:
                self.pos_tracker.set_target_order(entry.entry_order_id, target_order_id, entry.target_price)

        # Register with OCO monitor
        # Note: target_order_id may be None for LTP-based target monitoring
        # (TARGET limit order not placed to avoid margin issues)
        if self.oco_monitor and sl_order_id:
            self.oco_monitor.add_oco_pair(
                entry_order_id=entry.entry_order_id,
                symbol=entry.symbol,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,  # May be None for LTP monitoring
                sl_trigger=entry.sl_price,
                target_price=entry.target_price,  # Required for LTP monitoring
                quantity=filled_qty,
                side=entry.side,
                entry_price=avg_price
            )
            if target_order_id:
                logger.info(f"[PARTIAL] OCO pair registered: {entry.symbol} (SL + Target limit order)")
            elif entry.target_price:
                logger.info(f"[PARTIAL] OCO pair registered: {entry.symbol} (SL + Target LTP monitor @ {entry.target_price:.2f})")
            else:
                logger.info(f"[PARTIAL] OCO pair registered: {entry.symbol} (SL only, no target)")

        # Register with trail manager for Trail BE/+10/+25 buttons
        if self.trail_mgr and sl_order_id:
            self.trail_mgr.add_position(
                entry_order_id=entry.entry_order_id,
                symbol=entry.symbol,
                exchange_segment=entry.exchange_segment,
                entry_price=avg_price,
                quantity=filled_qty,
                side=entry.side,
                sl_price=entry.sl_price,
                sl_order_id=sl_order_id,
                instrument_token=entry.instrument_token,
                target_order_id=target_order_id,
                product=entry.product  # S23-C1: Pass product for SL recreation
            )
            logger.info(f"[PARTIAL] Trail manager registered: {entry.symbol} (entry={entry.entry_order_id})")

        # Notify via callback if registered
        if self.on_protection_orders_placed:
            self.on_protection_orders_placed(
                entry.symbol, sl_order_id, target_order_id,
                entry.sl_price, entry.target_price, filled_qty
            )

        # Send Telegram notification
        if self.telegram:
            msg = f"✅ {entry.symbol} FILLED @ {avg_price:.2f}\n"
            msg += f"Qty: {filled_qty}\n"
            if sl_order_id:
                msg += f"SL: {entry.sl_price}\n"
            if target_order_id:
                msg += f"Target: {entry.target_price}"
            self.telegram.send(msg)

    def _cancel_protection_orders(self, entry: MonitoredEntry):
        """Cancel SL and Target orders when entry is cancelled with no fill."""
        if entry.sl_order_id:
            try:
                self.client.cancel_order(order_id=entry.sl_order_id)
                logger.info(f"[PARTIAL] Cancelled orphan SL: {entry.sl_order_id}")
            except Exception as e:
                logger.warning(f"[PARTIAL] Failed to cancel SL: {e}")

        if entry.target_order_id:
            try:
                self.client.cancel_order(order_id=entry.target_order_id)
                logger.info(f"[PARTIAL] Cancelled orphan Target: {entry.target_order_id}")
            except Exception as e:
                logger.warning(f"[PARTIAL] Failed to cancel Target: {e}")

        # Clean up trackers
        if self.pos_tracker:
            self.pos_tracker.close_position(entry.entry_order_id)

        if self.oco_monitor:
            self.oco_monitor.remove_pair(entry.entry_order_id)

    def force_sync_all(self):
        """
        Force synchronization of all monitored entries.

        Call this on startup or after network recovery.
        """
        logger.info("[PARTIAL] Force syncing all entries...")
        self._check_all_entries()

    def get_monitored_entries(self) -> List[Dict[str, Any]]:
        """Get list of all monitored entries for GUI display."""
        with self._lock:
            return [
                {
                    'symbol': e.symbol,
                    'entry_order_id': e.entry_order_id,
                    'expected_qty': e.expected_qty,
                    'filled_qty': e.filled_qty,
                    'pending_qty': e.pending_qty,
                    'avg_price': e.avg_price,
                    'status': e.status.value,
                    'sl_order_id': e.sl_order_id,
                    'sl_qty': e.sl_qty,
                    'sl_synced': e.sl_synced,
                    'side': e.side,
                    'adjustments': e.adjustments_made
                }
                for e in self.monitored_entries.values()
            ]

    def get_unsynced_entries(self) -> List[MonitoredEntry]:
        """Get entries where SL qty doesn't match filled qty (potential risk)."""
        with self._lock:
            return [e for e in self.monitored_entries.values() if not e.sl_synced]

    def has_unsynced_entries(self) -> bool:
        """Check if any entries have mismatched SL quantities."""
        with self._lock:
            return any(not e.sl_synced for e in self.monitored_entries.values())

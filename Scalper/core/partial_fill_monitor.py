"""
NEO Trade Terminal - Partial Fill Monitor

Automatically monitors entry orders for partial fills and adjusts
SL/Target order quantities to match the actual filled quantity.

CRITICAL: Without this, a partial entry fill with full SL quantity
can result in exiting more than you own = naked short position.
"""

import threading
import time
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
import logging

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

        self.monitored_entries: Dict[str, MonitoredEntry] = {}  # keyed by entry_order_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._check_interval = 0.5  # Check every 500ms for fast detection

        # CRITICAL: Track unprotected positions (filled but no SL)
        self.unprotected_positions: Dict[str, MonitoredEntry] = {}  # symbol -> entry
        self._watchdog_interval = 1.0  # Check unprotected positions every 1 second (was 5s - too slow)
        self._last_watchdog_check = 0.0
        self._max_sl_retries = 5  # Max retries before giving up (increased from 3)

        # Callbacks for GUI updates
        self.on_partial_fill: Optional[Callable] = None
        self.on_complete_fill: Optional[Callable] = None
        self.on_sl_adjusted: Optional[Callable] = None
        self.on_critical_error: Optional[Callable] = None
        self.on_unprotected_position: Optional[Callable] = None  # Called when position is unprotected

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
        """Check all monitored entries for partial fills."""
        if not self.monitored_entries:
            return

        # Get all orders in one API call
        try:
            order_report = self.client.order_report()
            orders_list = order_report.get('data', []) if order_report else []
            orders_map = {o.get('nOrdNo'): o for o in orders_list}
        except Exception as e:
            logger.warning(f"[PARTIAL] Failed to fetch orders: {e}")
            return

        # Process each monitored entry
        entries_to_remove = []

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

                # Handle status transitions
                if new_status == EntryOrderStatus.COMPLETE:
                    # Fully filled
                    logger.info(f"[PARTIAL] Complete fill: {entry.symbol} qty={filled_qty} @ {avg_price}")

                    # If SL/Target were pending (not yet placed), place them now
                    if entry.sl_price_pending or entry.target_price_pending:
                        self._place_pending_protection_orders(entry, filled_qty, avg_price)

                    # Verify SL qty if SL was already placed
                    elif entry.sl_order_id and entry.sl_qty != filled_qty:
                        self._adjust_sl_quantity(entry, filled_qty, orders_map)

                    entries_to_remove.append(entry_order_id)

                    if self.on_complete_fill:
                        self.on_complete_fill(entry.symbol, filled_qty, avg_price, entry)

                elif new_status == EntryOrderStatus.PARTIAL:
                    # Partial fill detected!
                    if filled_qty > old_filled:
                        logger.warning(f"[PARTIAL] Partial fill: {entry.symbol} "
                                      f"filled={filled_qty}/{total_qty}")

                        # CRITICAL: Adjust SL quantity to match filled qty
                        if entry.sl_order_id:
                            self._adjust_sl_quantity(entry, filled_qty, orders_map)

                        if self.on_partial_fill:
                            self.on_partial_fill(entry.symbol, filled_qty, total_qty, avg_price)

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
                            self._adjust_sl_quantity(entry, filled_qty, orders_map)
                    else:
                        # No fill - cancel SL and Target if they exist
                        logger.info(f"[PARTIAL] Entry cancelled (no fill): {entry.symbol}")
                        self._cancel_protection_orders(entry)

                    # Notify via callback if registered
                    if hasattr(self, 'on_order_rejected') and self.on_order_rejected:
                        self.on_order_rejected(entry.symbol, rej_reason or status)

                    entries_to_remove.append(entry_order_id)

        # Clean up completed/cancelled entries outside lock
        for entry_id in entries_to_remove:
            self.unregister_entry(entry_id)

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
            self.client.modify_order(
                order_id=entry.sl_order_id,
                quantity=str(new_qty)
            )

            old_qty = entry.sl_qty
            entry.sl_qty = new_qty
            entry.sl_synced = True
            entry.adjustments_made += 1

            logger.info(f"[PARTIAL] SL qty adjusted: {entry.symbol} "
                       f"{old_qty} -> {new_qty} (success)")

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

            # CRITICAL: Create new SL FIRST (before cancelling old)
            # This ensures position is never unprotected
            new_sl_response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product=entry.product,
                price="0",
                order_type="SL-M",
                quantity=str(new_qty),
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=sl_txn_type,
                amo="NO",
                trigger_price=str(entry.sl_price),
                tag='SL'
            )

            new_sl_id = new_sl_response.get('nOrdNo') if new_sl_response else None

            if new_sl_id:
                logger.info(f"[PARTIAL] New SL placed: {new_sl_id} qty={new_qty}")

                # NOW cancel old SL (after new one is confirmed)
                try:
                    self.client.cancel_order(order_id=old_sl_id)
                    logger.info(f"[PARTIAL] Cancelled old SL: {old_sl_id}")
                except Exception as cancel_err:
                    # Old SL cancel failed but we have new SL - this is acceptable
                    # The old SL might already be filled/cancelled
                    logger.warning(f"[PARTIAL] Old SL cancel failed (may be already executed): {cancel_err}")
                old_qty = entry.sl_qty
                entry.sl_order_id = new_sl_id
                entry.sl_order_id_backup = None  # Clear backup - we have a new SL
                entry.sl_qty = new_qty
                entry.sl_synced = True
                entry.adjustments_made += 1
                entry.unprotected_since = None  # No longer unprotected

                # Remove from unprotected list if present
                if entry.symbol in self.unprotected_positions:
                    del self.unprotected_positions[entry.symbol]

                logger.info(f"[PARTIAL] SL recreated: {entry.symbol} "
                           f"new_order={new_sl_id} qty={new_qty}")

                # Update position tracker if available
                if self.pos_tracker:
                    self.pos_tracker.update_sl_order(
                        entry.symbol, new_sl_id, entry.sl_price
                    )

                # Update OCO monitor if available
                if self.oco_monitor:
                    self.oco_monitor.update_sl_order(
                        entry.symbol, new_sl_id, entry.sl_price
                    )

                if self.on_sl_adjusted:
                    self.on_sl_adjusted(entry.symbol, old_qty, new_qty)

                return True
            else:
                # CRITICAL: SL recreation failed - position is unprotected!
                self._mark_position_unprotected(entry, new_qty, "SL recreation returned no order ID")
                return False

        except Exception as e:
            logger.error(f"[PARTIAL] SL recreate failed: {e}")
            self._mark_position_unprotected(entry, new_qty, str(e))
            return False

    def _mark_position_unprotected(self, entry: MonitoredEntry, qty: int, reason: str):
        """Mark a position as unprotected and track for recovery."""
        entry.sl_synced = False
        entry.sl_order_id = None  # Clear - no valid SL exists
        entry.unprotected_since = time.time()
        entry.sl_retry_count += 1

        # Add to unprotected tracking
        self.unprotected_positions[entry.symbol] = entry

        logger.error(f"[PARTIAL] CRITICAL: Position UNPROTECTED - {entry.symbol} "
                    f"qty={qty}, reason={reason}, retries={entry.sl_retry_count}")

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

            for symbol, entry in list(self.unprotected_positions.items()):
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
                    del self.unprotected_positions[symbol]

                    if self.telegram:
                        self.telegram.send(
                            f"✅ SL RECOVERED for {symbol}!\n"
                            f"Position now protected."
                        )
                else:
                    logger.warning(f"[WATCHDOG] SL recovery FAILED for {symbol}")

    def _retry_sl_placement(self, entry: MonitoredEntry) -> bool:
        """Attempt to place SL for an unprotected position."""
        if not entry.sl_price or entry.filled_qty <= 0:
            logger.error(f"[WATCHDOG] Cannot retry SL - missing price or qty for {entry.symbol}")
            return False

        try:
            sl_txn_type = 'S' if entry.side == 'LONG' else 'B'

            new_sl_response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product=entry.product,
                price="0",
                order_type="SL-M",
                quantity=str(entry.filled_qty),
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=sl_txn_type,
                amo="NO",
                trigger_price=str(entry.sl_price),
                tag='SL_RECOVERY'
            )

            new_sl_id = new_sl_response.get('nOrdNo') if new_sl_response else None

            if new_sl_id:
                entry.sl_order_id = new_sl_id
                entry.sl_qty = entry.filled_qty
                entry.sl_synced = True
                entry.unprotected_since = None

                logger.info(f"[WATCHDOG] SL placed: {entry.symbol} "
                           f"order={new_sl_id} price={entry.sl_price}")

                # Update trackers
                if self.pos_tracker:
                    self.pos_tracker.update_sl_order(entry.symbol, new_sl_id, entry.sl_price)

                if self.oco_monitor and entry.target_order_id:
                    self.oco_monitor.update_sl_order(entry.symbol, new_sl_id, entry.sl_price)

                return True
            else:
                entry.sl_retry_count += 1
                return False

        except Exception as e:
            logger.error(f"[WATCHDOG] SL retry failed: {e}")
            entry.sl_retry_count += 1
            return False

    def get_unprotected_positions(self) -> List[Dict[str, Any]]:
        """Get list of unprotected positions for GUI display."""
        with self._lock:
            result = []
            for symbol, entry in self.unprotected_positions.items():
                duration = time.time() - (entry.unprotected_since or time.time())
                result.append({
                    'symbol': symbol,
                    'qty': entry.filled_qty,
                    'sl_price': entry.sl_price,
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

        sl_order_id = None
        target_order_id = None
        is_long = entry.side == 'LONG'
        exit_type = 'S' if is_long else 'B'

        # Place SL order with retry logic (CRITICAL)
        if entry.sl_price_pending:
            max_sl_retries = 3
            sl_placed = False

            for attempt in range(max_sl_retries):
                try:
                    sl_response = self.client.place_order(
                        exchange_segment=entry.exchange_segment,
                        product=entry.product,
                        price="0",
                        order_type="SL-M",
                        quantity=str(filled_qty),
                        validity="DAY",
                        trading_symbol=entry.symbol,
                        transaction_type=exit_type,
                        amo="NO",
                        trigger_price=str(entry.sl_price_pending),
                        tag='SL'
                    )
                    sl_order_id = sl_response.get('nOrdNo') if sl_response else None

                    if sl_order_id:
                        entry.sl_order_id = sl_order_id
                        entry.sl_price = entry.sl_price_pending
                        entry.sl_qty = filled_qty
                        logger.info(f"[PARTIAL] SL placed: {entry.symbol} @ {entry.sl_price_pending} -> {sl_order_id}")
                        sl_placed = True
                        break
                    else:
                        logger.warning(f"[PARTIAL] SL placement attempt {attempt+1}/{max_sl_retries} failed for {entry.symbol}")
                        if attempt < max_sl_retries - 1:
                            import time
                            time.sleep(0.5 * (attempt + 1))  # Backoff: 0.5s, 1s

                except Exception as e:
                    logger.error(f"[PARTIAL] SL placement attempt {attempt+1} error: {e}")
                    if attempt < max_sl_retries - 1:
                        import time
                        time.sleep(0.5 * (attempt + 1))

            if not sl_placed:
                logger.error(f"[PARTIAL] CRITICAL: SL placement FAILED after {max_sl_retries} attempts for {entry.symbol}!")
                # Mark as unprotected for watchdog recovery
                self._mark_position_unprotected(entry, filled_qty, f"Initial SL placement failed after {max_sl_retries} attempts")
                if self.telegram:
                    self.telegram.send(f"🚨 CRITICAL: SL placement FAILED for {entry.symbol} after {max_sl_retries} retries! Position UNPROTECTED!")
                if self.on_critical_error:
                    self.on_critical_error(entry.symbol, "SL placement failed on entry fill")

        # Place Target order
        if entry.target_price_pending:
            try:
                target_response = self.client.place_order(
                    exchange_segment=entry.exchange_segment,
                    product=entry.product,
                    price=str(entry.target_price_pending),
                    order_type="L",
                    quantity=str(filled_qty),
                    validity="DAY",
                    trading_symbol=entry.symbol,
                    transaction_type=exit_type,
                    amo="NO",
                    tag='TARGET'
                )
                target_order_id = target_response.get('nOrdNo') if target_response else None

                if target_order_id:
                    entry.target_order_id = target_order_id
                    entry.target_price = entry.target_price_pending
                    logger.info(f"[PARTIAL] Target placed: {entry.symbol} @ {entry.target_price_pending} -> {target_order_id}")
                else:
                    logger.warning(f"[PARTIAL] Target placement failed for {entry.symbol}")
                    # Notify user - position has SL but no Target
                    if self.telegram:
                        self.telegram.send(
                            f"⚠️ TARGET FAILED for {entry.symbol}\n"
                            f"Position has SL @ {entry.sl_price} but NO Target\n"
                            f"Manual target placement may be needed"
                        )
                    if self.sound:
                        self.sound.play('error')

            except Exception as e:
                logger.error(f"[PARTIAL] Target placement error: {e}")
                # Notify user on exception as well
                if self.telegram:
                    self.telegram.send(
                        f"⚠️ TARGET ERROR for {entry.symbol}: {e}\n"
                        f"Position has SL but NO Target"
                    )
                if self.sound:
                    self.sound.play('error')

        # Register with position tracker
        if self.pos_tracker and (sl_order_id or target_order_id):
            self.pos_tracker.add_position(
                symbol=entry.symbol,
                exchange_segment=entry.exchange_segment,
                quantity=filled_qty,
                side=entry.side,
                entry_price=avg_price
            )
            if sl_order_id:
                self.pos_tracker.set_sl_order(entry.symbol, sl_order_id, entry.sl_price)
            if target_order_id:
                self.pos_tracker.set_target_order(entry.symbol, target_order_id, entry.target_price)

        # Register with OCO monitor
        if self.oco_monitor and sl_order_id and target_order_id:
            self.oco_monitor.add_oco_pair(
                position_symbol=entry.symbol,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,
                sl_trigger=entry.sl_price,
                target_price=entry.target_price,
                quantity=filled_qty,
                side=entry.side,
                entry_price=avg_price
            )
            logger.info(f"[PARTIAL] OCO pair registered: {entry.symbol}")

        # Register with trail manager for Trail BE/+10/+25 buttons
        if self.trail_mgr and sl_order_id:
            self.trail_mgr.add_position(
                symbol=entry.symbol,
                exchange_segment=entry.exchange_segment,
                entry_price=avg_price,
                quantity=filled_qty,
                side=entry.side,
                sl_price=entry.sl_price,
                sl_order_id=sl_order_id,
                instrument_token=entry.instrument_token,
                target_order_id=target_order_id
            )
            logger.info(f"[PARTIAL] Trail manager registered: {entry.symbol}")

        # Notify via callback if registered
        if hasattr(self, 'on_protection_orders_placed') and self.on_protection_orders_placed:
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
            self.pos_tracker.close_position(entry.symbol)

        if self.oco_monitor:
            self.oco_monitor.remove_oco_pair(entry.symbol)

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

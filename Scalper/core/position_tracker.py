"""
NEO Trade Terminal - Position Tracker

Tracks per-position state including SL/Target order IDs.
Keyed by entry_order_id to support multiple brackets on same symbol.
Ensures proper cleanup and prevents ghost orders.
"""

import threading
import copy
from typing import Dict, Optional, Any, List, Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    """Tracked position with SL/Target order state."""
    symbol: str
    entry_order_id: str  # CRITICAL: Unique identifier for this position entry
    exchange_segment: str
    quantity: int
    side: str  # 'LONG' or 'SHORT'
    entry_price: float
    entry_time: datetime = field(default_factory=datetime.now)
    product: str = "MIS"  # Product type (MIS/NRML) for SL recovery

    # Order tracking
    sl_order_id: Optional[str] = None
    sl_price: Optional[float] = None
    target_order_id: Optional[str] = None
    target_price: Optional[float] = None

    # State
    is_active: bool = True
    pnl: float = 0.0
    ltp: float = 0.0

    # CRITICAL: Race condition prevention
    # Set to True when OCO/Exit is processing - Trail Manager should NOT modify
    locked_for_exit: bool = False
    exit_started_at: Optional[float] = None  # timestamp when exit started


class PositionTracker:
    """
    Tracks all open positions with their SL/Target orders.

    CRITICAL: Keyed by entry_order_id (not symbol) to support multiple
    bracket orders on the same symbol at different levels.

    Provides methods for proper cleanup on exit.
    """

    def __init__(self, neo_client, order_manager=None, cancel_mgr=None):
        self.client = neo_client
        self.order_mgr = order_manager
        self.cancel_mgr = cancel_mgr  # Centralized cancel manager (race condition prevention)
        self.positions: Dict[str, TrackedPosition] = {}  # keyed by entry_order_id
        self._lock = threading.Lock()

        # Callbacks for cross-component notifications
        self.on_position_removed: Optional[Callable] = None  # Called when position is removed
        self.oco_monitor = None  # Reference to OCO monitor for cleanup

    def set_cancel_mgr(self, mgr):
        """Set the cancel manager (can be set after initialization)."""
        self.cancel_mgr = mgr

    def add_position(self, symbol: str, entry_order_id: str, exchange_segment: str,
                     quantity: int, side: str, entry_price: float,
                     product: str = "MIS") -> TrackedPosition:
        """
        Add a new position to track.

        Args:
            symbol: Trading symbol
            entry_order_id: Entry order ID (UNIQUE key for this position)
            exchange_segment: Exchange segment
            quantity: Position quantity (positive)
            side: 'LONG' or 'SHORT'
            entry_price: Entry price
            product: Product type (MIS/NRML) for SL recovery

        Returns:
            TrackedPosition instance
        """
        with self._lock:
            pos = TrackedPosition(
                symbol=symbol,
                entry_order_id=entry_order_id,
                exchange_segment=exchange_segment,
                quantity=abs(quantity),
                side=side.upper(),
                entry_price=entry_price,
                product=product
            )
            self.positions[entry_order_id] = pos
            logger.info(f"[TRACKER] Added position: {symbol} {side} {quantity} @ {entry_price} (entry={entry_order_id})")
            return pos

    def get_position(self, entry_order_id: str) -> Optional[TrackedPosition]:
        """Get tracked position by entry_order_id."""
        with self._lock:
            return self.positions.get(entry_order_id)

    def get_positions_by_symbol(self, symbol: str) -> List[TrackedPosition]:
        """Get all tracked positions for a symbol (may be multiple)."""
        with self._lock:
            return [p for p in self.positions.values() if p.symbol == symbol and p.is_active]

    def get_first_position_by_symbol(self, symbol: str) -> Optional[TrackedPosition]:
        """Get first tracked position for a symbol (backward compatibility)."""
        positions = self.get_positions_by_symbol(symbol)
        return positions[0] if positions else None

    def get_all_positions_snapshot(self) -> Dict[str, TrackedPosition]:
        """
        Get a DEEP COPY snapshot of all positions for safe iteration.

        Returns deep copies of TrackedPosition objects - safe to read and modify
        without affecting the original tracker state. Essential for concurrent
        iteration while OCO monitor or other threads may modify positions.

        Use this instead of directly accessing self.positions.items()
        when iterating from code that may run concurrently with OCO monitor.
        """
        with self._lock:
            # Return deep copies to prevent external code from modifying originals
            return {k: copy.copy(v) for k, v in self.positions.items()}

    def get_all_active_positions(self) -> List[TrackedPosition]:
        """Get all active positions as a list (thread-safe snapshot)."""
        with self._lock:
            return [p for p in self.positions.values() if p.is_active]

    def set_sl_order(self, entry_order_id: str, sl_order_id: str, sl_price: float):
        """
        Record SL order for position.

        Args:
            entry_order_id: Entry order ID (position key)
            sl_order_id: SL order ID from broker
            sl_price: SL trigger price
        """
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos:
                pos.sl_order_id = sl_order_id
                pos.sl_price = sl_price
                logger.info(f"[TRACKER] SL set: {pos.symbol} (entry={entry_order_id}) -> {sl_order_id} @ {sl_price}")

    def set_target_order(self, entry_order_id: str, target_order_id: str, target_price: float):
        """
        Record Target order for position.

        Args:
            entry_order_id: Entry order ID (position key)
            target_order_id: Target order ID from broker
            target_price: Target price
        """
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos:
                pos.target_order_id = target_order_id
                pos.target_price = target_price
                logger.info(f"[TRACKER] Target set: {pos.symbol} (entry={entry_order_id}) -> {target_order_id} @ {target_price}")

    def update_sl_order(self, entry_order_id: str, new_sl_order_id: str = None,
                        new_sl_price: float = None):
        """Update SL order after modification."""
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos:
                if new_sl_order_id:
                    pos.sl_order_id = new_sl_order_id
                # CRITICAL FIX (S25 PT-9): Use 'is not None' to allow price=0
                # 0 can be valid (e.g., free options, initial state)
                if new_sl_price is not None:
                    pos.sl_price = new_sl_price

    def clear_cancelled_orders(self, cancelled_order_ids: set):
        """
        Thread-safe clearing of SL/Target order references that were cancelled.

        Iterates through all positions and clears any SL/Target order IDs
        that are in the cancelled set. This is the proper way to handle
        cancel-all scenarios instead of modifying snapshot copies.

        Args:
            cancelled_order_ids: Set of order IDs that were cancelled
        """
        with self._lock:
            for entry_id, pos in self.positions.items():
                if pos.sl_order_id in cancelled_order_ids:
                    logger.info(f"[TRACKER] Clearing cancelled SL order {pos.sl_order_id} for {pos.symbol}")
                    pos.sl_order_id = None
                    pos.sl_price = None
                if pos.target_order_id in cancelled_order_ids:
                    logger.info(f"[TRACKER] Clearing cancelled Target order {pos.target_order_id} for {pos.symbol}")
                    pos.target_order_id = None
                    pos.target_price = None

    def get_all_entry_ids(self) -> list:
        """
        Get all tracked entry order IDs (thread-safe).

        Returns:
            List of entry order IDs currently being tracked
        """
        with self._lock:
            return list(self.positions.keys())

    # ==================== Race Condition Prevention ====================

    def lock_for_exit(self, entry_order_id: str) -> bool:
        """
        Lock position for exit processing - prevents Trail Manager from modifying.

        CRITICAL: Call this BEFORE cancelling orders in OCO or Exit handlers.

        Args:
            entry_order_id: Entry order ID (position key)

        Returns:
            True if locked successfully, False if already locked or position not found
        """
        import time
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return False
            if pos.locked_for_exit:
                logger.warning(f"[TRACKER] {pos.symbol} (entry={entry_order_id}) already locked for exit")
                return False

            pos.locked_for_exit = True
            pos.exit_started_at = time.time()
            logger.info(f"[TRACKER] Locked {pos.symbol} (entry={entry_order_id}) for exit")
            return True

    def unlock_for_exit(self, entry_order_id: str):
        """
        Unlock position after exit processing completes or fails.

        Args:
            entry_order_id: Entry order ID (position key)
        """
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos:
                pos.locked_for_exit = False
                pos.exit_started_at = None
                logger.info(f"[TRACKER] Unlocked {pos.symbol} (entry={entry_order_id}) from exit")

    def is_locked_for_exit(self, entry_order_id: str) -> bool:
        """
        Check if position is locked for exit (Trail Manager should NOT modify).

        Args:
            entry_order_id: Entry order ID (position key)

        Returns:
            True if locked, False otherwise
        """
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return False
            return pos.locked_for_exit

    def cancel_sl_order(self, entry_order_id: str) -> Optional[str]:
        """
        Cancel SL order for position.
        Uses centralized cancel manager to prevent race conditions with OCO monitor.

        Args:
            entry_order_id: Entry order ID (position key)

        Returns:
            Cancelled order ID or None
        """
        # Step 1: Get order_id and clear state inside lock (fast)
        order_id = None
        symbol = None
        use_cancel_mgr = False

        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos and pos.sl_order_id:
                order_id = pos.sl_order_id
                symbol = pos.symbol
                use_cancel_mgr = self.cancel_mgr is not None
                # Clear state immediately to prevent double-cancel attempts
                pos.sl_order_id = None
                pos.sl_price = None

        if not order_id:
            return None

        # Step 2: Make API calls outside lock (slow, doesn't block other operations)
        if use_cancel_mgr:
            success, msg = self.cancel_mgr.cancel_order(
                order_id=order_id,
                reason=f"Tracker SL for {symbol}"
            )
            logger.info(f"[TRACKER] SL cancel via manager: {order_id} - {msg}")
            return order_id if success else None

        # Fallback: direct cancel with status check
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []
            order_status = None
            for o in orders:
                if o.get('nOrdNo') == order_id:
                    order_status = o.get('ordSt', '').lower()
                    break

            # If order already completed/cancelled, nothing to do
            if order_status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                logger.info(f"[TRACKER] SL {order_id} already {order_status}")
                return order_id

            # Order still pending - cancel it
            self.client.cancel_order(order_id=order_id)
            logger.info(f"[TRACKER] Cancelled SL: {symbol} -> {order_id}")
            return order_id

        except Exception as e:
            # State already cleared, just log the error
            logger.warning(f"[TRACKER] SL cancel issue for {order_id}: {e}")
            return None

    def cancel_target_order(self, entry_order_id: str) -> Optional[str]:
        """
        Cancel Target order for position.
        Uses centralized cancel manager to prevent race conditions with OCO monitor.

        Args:
            entry_order_id: Entry order ID (position key)

        Returns:
            Cancelled order ID or None
        """
        # Step 1: Get order_id and clear state inside lock (fast)
        order_id = None
        symbol = None
        use_cancel_mgr = False

        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos and pos.target_order_id:
                order_id = pos.target_order_id
                symbol = pos.symbol
                use_cancel_mgr = self.cancel_mgr is not None
                # Clear state immediately to prevent double-cancel attempts
                pos.target_order_id = None
                pos.target_price = None

        if not order_id:
            return None

        # Step 2: Make API calls outside lock (slow, doesn't block other operations)
        if use_cancel_mgr:
            success, msg = self.cancel_mgr.cancel_order(
                order_id=order_id,
                reason=f"Tracker Target for {symbol}"
            )
            logger.info(f"[TRACKER] Target cancel via manager: {order_id} - {msg}")
            return order_id if success else None

        # Fallback: direct cancel with status check
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []
            order_status = None
            for o in orders:
                if o.get('nOrdNo') == order_id:
                    order_status = o.get('ordSt', '').lower()
                    break

            # If order already completed/cancelled, nothing to do
            if order_status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                logger.info(f"[TRACKER] Target {order_id} already {order_status}")
                return order_id

            # Order still pending - cancel it
            self.client.cancel_order(order_id=order_id)
            logger.info(f"[TRACKER] Cancelled Target: {symbol} -> {order_id}")
            return order_id

        except Exception as e:
            # State already cleared, just log the error
            logger.warning(f"[TRACKER] Target cancel issue for {order_id}: {e}")
            return None

    def cancel_all_orders_for_position(self, entry_order_id: str) -> Dict[str, Any]:
        """
        Cancel both SL and Target orders for position.

        Args:
            entry_order_id: Entry order ID (position key)

        Returns:
            Dict with cancelled order IDs
        """
        result = {
            'sl_cancelled': None,
            'target_cancelled': None
        }

        result['sl_cancelled'] = self.cancel_sl_order(entry_order_id)
        result['target_cancelled'] = self.cancel_target_order(entry_order_id)

        return result

    def reduce_position_qty(self, entry_order_id: str, exit_qty: int) -> int:
        """
        Reduce tracked position quantity after partial exit.

        Args:
            entry_order_id: Entry order ID (position key)
            exit_qty: Quantity exited

        Returns:
            Remaining quantity
        """
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos:
                pos.quantity = max(0, pos.quantity - abs(exit_qty))
                logger.info(f"[TRACKER] Qty reduced: {pos.symbol} (entry={entry_order_id}) -> {pos.quantity} remaining")
                return pos.quantity
            return 0

    def modify_sl_quantity(self, entry_order_id: str, new_qty: int) -> Dict[str, Any]:
        """
        Modify SL order quantity after partial exit.
        Falls back to cancel + recreate if modify fails.

        Args:
            entry_order_id: Entry order ID (position key)
            new_qty: New quantity for SL

        Returns:
            Dict with 'success', 'action' ('modified'/'cancelled'/'failed'), 'sl_price'
        """
        # Step 1: Get necessary data inside lock (fast)
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos or not pos.sl_order_id:
                return {'success': False, 'action': 'failed', 'error': 'No SL order to modify'}

            old_sl_price = pos.sl_price
            old_sl_id = pos.sl_order_id
            symbol = pos.symbol

        # Step 2: Make API calls OUTSIDE lock (slow - don't block other operations)
        # First try to modify
        try:
            # Fetch SL order to get order_type, price, trigger_price (required by NEO API)
            orders = self.client.order_report()
            order_list = orders.get('data', []) if orders else []
            sl_order = None
            for order in order_list:
                if order.get('nOrdNo') == old_sl_id:
                    sl_order = order
                    break

            if not sl_order:
                raise Exception(f"SL order {old_sl_id} not found in order book")

            sl_order_type = sl_order.get('prcTp', sl_order.get('orderType', 'SL'))
            sl_order_price = sl_order.get('prc', sl_order.get('price', '0'))
            sl_trigger_price = sl_order.get('trgPrc', sl_order.get('triggerPrice', str(old_sl_price)))

            self.client.modify_order(
                order_id=old_sl_id,
                order_type=sl_order_type,
                price=str(sl_order_price),
                quantity=str(new_qty),
                trigger_price=str(sl_trigger_price),
                validity="DAY"
            )
            logger.info(f"[TRACKER] SL qty modified: {symbol} -> {new_qty}")
            return {'success': True, 'action': 'modified', 'sl_price': old_sl_price}

        except Exception as e:
            logger.warning(f"[TRACKER] Modify failed: {e}, attempting cancel + recreate")

            # Fallback: Cancel the old SL order
            try:
                self.client.cancel_order(order_id=old_sl_id)
                logger.info(f"[TRACKER] Cancelled old SL: {old_sl_id}")

                # Step 3: Update state inside lock after API call
                with self._lock:
                    pos = self.positions.get(entry_order_id)
                    if pos:
                        pos.sl_order_id = None
                        # Don't clear sl_price - GUI will need it to recreate

                return {
                    'success': False,
                    'action': 'cancelled',
                    'sl_price': old_sl_price,
                    'message': f'SL cancelled - recreate with qty {new_qty}'
                }

            except Exception as cancel_e:
                logger.error(f"[TRACKER] Failed to cancel old SL: {cancel_e}")
                return {
                    'success': False,
                    'action': 'failed',
                    'error': f'Modify failed and cancel failed: {cancel_e}',
                    'sl_price': old_sl_price
                }

    def close_position(self, entry_order_id: str) -> Dict[str, Any]:
        """
        Close position tracking - cancel all orders and remove.

        Args:
            entry_order_id: Entry order ID (position key)

        Returns:
            Dict with cleanup results
        """
        result = self.cancel_all_orders_for_position(entry_order_id)

        with self._lock:
            if entry_order_id in self.positions:
                pos = self.positions[entry_order_id]
                pos.is_active = False
                symbol = pos.symbol
                del self.positions[entry_order_id]
                logger.info(f"[TRACKER] Position closed: {symbol} (entry={entry_order_id})")

        result['position_removed'] = True
        return result

    def get_all_active(self) -> List[TrackedPosition]:
        """Get all active tracked positions."""
        with self._lock:
            return [p for p in self.positions.values() if p.is_active]

    def sync_with_broker_positions(self, broker_positions: List[Dict[str, Any]]):
        """
        Sync tracked positions with broker positions.
        Removes tracking for positions that no longer exist.

        CRITICAL FIX (S10-H1): API calls are now made OUTSIDE the lock to prevent
        blocking other position tracker operations during slow network calls.

        Args:
            broker_positions: List of positions from broker API
        """
        broker_symbols = set()
        for pos in broker_positions:
            symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
            qty = int(float(pos.get('qty', 0) or 0))
            if qty != 0:
                broker_symbols.add(symbol)

        # Phase 1: Collect entries to remove and their order IDs inside lock (fast)
        orders_to_cancel = []  # List of (entry_order_id, symbol, sl_order_id, target_order_id)
        with self._lock:
            for entry_order_id, pos in self.positions.items():
                if pos.symbol not in broker_symbols:
                    orders_to_cancel.append({
                        'entry_order_id': entry_order_id,
                        'symbol': pos.symbol,
                        'sl_order_id': pos.sl_order_id,
                        'target_order_id': pos.target_order_id
                    })

        if not orders_to_cancel:
            return

        # Phase 2: Make API calls OUTSIDE lock (slow, doesn't block other operations)
        for order_data in orders_to_cancel:
            entry_order_id = order_data['entry_order_id']
            symbol = order_data['symbol']
            logger.info(f"[TRACKER] Position no longer at broker: {symbol} (entry={entry_order_id})")

            # Cancel SL order
            if order_data['sl_order_id']:
                try:
                    self.client.cancel_order(order_id=order_data['sl_order_id'])
                    logger.info(f"[TRACKER] Cancelled orphan SL {order_data['sl_order_id']} for {symbol}")
                except Exception as e:
                    logger.warning(f"[TRACKER] Failed to cancel orphan SL {order_data['sl_order_id']}: {e}")

            # Cancel Target order
            if order_data['target_order_id']:
                try:
                    self.client.cancel_order(order_id=order_data['target_order_id'])
                    logger.info(f"[TRACKER] Cancelled orphan Target {order_data['target_order_id']} for {symbol}")
                except Exception as e:
                    logger.warning(f"[TRACKER] Failed to cancel orphan Target {order_data['target_order_id']}: {e}")

            # Notify OCO monitor (may acquire OCO lock, so must be outside position lock)
            if self.oco_monitor:
                try:
                    self.oco_monitor.remove_pair(entry_order_id)
                    logger.info(f"[TRACKER] Notified OCO to remove pair for entry={entry_order_id}")
                except Exception as e:
                    logger.warning(f"[TRACKER] Failed to notify OCO: {e}")

            # Fire callback (may acquire other locks, so must be outside position lock)
            if self.on_position_removed:
                try:
                    self.on_position_removed(symbol, entry_order_id)
                except Exception as e:
                    logger.warning(f"[TRACKER] Position removed callback failed: {e}")

        # Phase 3: Delete entries from positions dict (re-acquire lock)
        with self._lock:
            for order_data in orders_to_cancel:
                entry_order_id = order_data['entry_order_id']
                if entry_order_id in self.positions:
                    del self.positions[entry_order_id]

    def has_sl(self, entry_order_id: str) -> bool:
        """Check if position has SL order."""
        with self._lock:
            pos = self.positions.get(entry_order_id)
            return pos is not None and pos.sl_order_id is not None

    def has_target(self, entry_order_id: str) -> bool:
        """Check if position has Target order."""
        with self._lock:
            pos = self.positions.get(entry_order_id)
            return pos is not None and pos.target_order_id is not None

    def get_sl_price(self, entry_order_id: str) -> Optional[float]:
        """Get current SL price for position."""
        with self._lock:
            pos = self.positions.get(entry_order_id)
            return pos.sl_price if pos else None

    def clear_all(self):
        """Clear all tracked positions (for reset).

        CRITICAL FIX (S10-H1): API calls are now made OUTSIDE the lock to prevent
        blocking other position tracker operations during slow network calls.
        """
        # Phase 1: Collect all order IDs to cancel inside lock (fast)
        orders_to_cancel = []  # List of (symbol, sl_order_id, target_order_id)
        with self._lock:
            for entry_order_id, pos in self.positions.items():
                orders_to_cancel.append({
                    'symbol': pos.symbol,
                    'sl_order_id': pos.sl_order_id,
                    'target_order_id': pos.target_order_id
                })

        # Phase 2: Make API calls OUTSIDE lock (slow, doesn't block other operations)
        cancel_results = {'success': 0, 'failed': 0}
        for order_data in orders_to_cancel:
            symbol = order_data['symbol']
            if order_data['sl_order_id']:
                try:
                    self.client.cancel_order(order_id=order_data['sl_order_id'])
                    logger.info(f"[TRACKER] Cancelled SL {order_data['sl_order_id']} for {symbol}")
                    cancel_results['success'] += 1
                except Exception as e:
                    logger.warning(f"[TRACKER] Failed to cancel SL {order_data['sl_order_id']}: {e}")
                    cancel_results['failed'] += 1
            if order_data['target_order_id']:
                try:
                    self.client.cancel_order(order_id=order_data['target_order_id'])
                    logger.info(f"[TRACKER] Cancelled Target {order_data['target_order_id']} for {symbol}")
                    cancel_results['success'] += 1
                except Exception as e:
                    logger.warning(f"[TRACKER] Failed to cancel Target {order_data['target_order_id']}: {e}")
                    cancel_results['failed'] += 1

        # Phase 3: Clear positions inside lock
        with self._lock:
            self.positions.clear()

        logger.info(f"[TRACKER] Cleared all positions. Cancels: {cancel_results['success']} ok, {cancel_results['failed']} failed")

    # ==================== Symbol-based lookups (for GUI compatibility) ====================

    def has_sl_for_symbol(self, symbol: str) -> bool:
        """Check if any position for symbol has SL order."""
        positions = self.get_positions_by_symbol(symbol)
        return any(p.sl_order_id for p in positions)

    def has_target_for_symbol(self, symbol: str) -> bool:
        """Check if any position for symbol has Target order."""
        positions = self.get_positions_by_symbol(symbol)
        return any(p.target_order_id for p in positions)

    def get_entry_order_ids_for_symbol(self, symbol: str) -> List[str]:
        """Get all entry_order_ids for a symbol."""
        with self._lock:
            return [entry_id for entry_id, p in self.positions.items()
                    if p.symbol == symbol and p.is_active]

    def cancel_all_orders_for_symbol(self, symbol: str) -> Dict[str, Any]:
        """Cancel all SL/Target orders for all positions of a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        results = {'sl_cancelled': [], 'target_cancelled': []}
        for entry_id in entry_ids:
            result = self.cancel_all_orders_for_position(entry_id)
            if result['sl_cancelled']:
                results['sl_cancelled'].append(result['sl_cancelled'])
            if result['target_cancelled']:
                results['target_cancelled'].append(result['target_cancelled'])
        return results

    def close_positions_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Close all positions for a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        results = []
        for entry_id in entry_ids:
            result = self.close_position(entry_id)
            results.append(result)
        return results

    def update_sl_order_by_symbol(self, symbol: str, new_sl_order_id: str = None,
                                   new_sl_price: float = None):
        """Update SL order for all positions of a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        for entry_id in entry_ids:
            self.update_sl_order(entry_id, new_sl_order_id, new_sl_price)

    def reduce_position_qty_by_symbol(self, symbol: str, exit_qty: int) -> int:
        """Reduce quantity for first position of a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        if entry_ids:
            return self.reduce_position_qty(entry_ids[0], exit_qty)
        return 0

    def lock_for_exit_by_symbol(self, symbol: str) -> bool:
        """Lock all positions for a symbol for exit."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        success = False
        for entry_id in entry_ids:
            if self.lock_for_exit(entry_id):
                success = True
        return success

    def set_sl_order_by_symbol(self, symbol: str, sl_order_id: str, sl_price: float):
        """Set SL order for first position of a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        if entry_ids:
            self.set_sl_order(entry_ids[0], sl_order_id, sl_price)

    def set_target_order_by_symbol(self, symbol: str, target_order_id: str, target_price: float):
        """Set Target order for first position of a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        if entry_ids:
            self.set_target_order(entry_ids[0], target_order_id, target_price)

    def cancel_sl_order_by_symbol(self, symbol: str) -> Optional[str]:
        """Cancel SL order for first position of a symbol."""
        entry_ids = self.get_entry_order_ids_for_symbol(symbol)
        if entry_ids:
            return self.cancel_sl_order(entry_ids[0])
        return None

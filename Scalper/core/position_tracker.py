"""
NEO Trade Terminal - Position Tracker

Tracks per-position state including SL/Target order IDs.
Ensures proper cleanup and prevents ghost orders.
"""

import threading
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


@dataclass
class TrackedPosition:
    """Tracked position with SL/Target order state."""
    symbol: str
    exchange_segment: str
    quantity: int
    side: str  # 'LONG' or 'SHORT'
    entry_price: float
    entry_time: datetime = field(default_factory=datetime.now)

    # Order tracking
    sl_order_id: Optional[str] = None
    sl_price: Optional[float] = None
    target_order_id: Optional[str] = None
    target_price: Optional[float] = None

    # State
    is_active: bool = True
    pnl: float = 0.0
    ltp: float = 0.0


class PositionTracker:
    """
    Tracks all open positions with their SL/Target orders.
    Provides methods for proper cleanup on exit.
    """

    def __init__(self, neo_client, order_manager=None):
        self.client = neo_client
        self.order_mgr = order_manager
        self.positions: Dict[str, TrackedPosition] = {}
        self._lock = threading.Lock()

    def add_position(self, symbol: str, exchange_segment: str,
                     quantity: int, side: str, entry_price: float) -> TrackedPosition:
        """
        Add a new position to track.

        Args:
            symbol: Trading symbol
            exchange_segment: Exchange segment
            quantity: Position quantity (positive)
            side: 'LONG' or 'SHORT'
            entry_price: Entry price

        Returns:
            TrackedPosition instance
        """
        with self._lock:
            pos = TrackedPosition(
                symbol=symbol,
                exchange_segment=exchange_segment,
                quantity=abs(quantity),
                side=side.upper(),
                entry_price=entry_price
            )
            self.positions[symbol] = pos
            logger.info(f"[TRACKER] Added position: {symbol} {side} {quantity} @ {entry_price}")
            return pos

    def get_position(self, symbol: str) -> Optional[TrackedPosition]:
        """Get tracked position by symbol."""
        with self._lock:
            return self.positions.get(symbol)

    def set_sl_order(self, symbol: str, sl_order_id: str, sl_price: float):
        """
        Record SL order for position.

        Args:
            symbol: Trading symbol
            sl_order_id: SL order ID from broker
            sl_price: SL trigger price
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos:
                pos.sl_order_id = sl_order_id
                pos.sl_price = sl_price
                logger.info(f"[TRACKER] SL set: {symbol} -> {sl_order_id} @ {sl_price}")

    def set_target_order(self, symbol: str, target_order_id: str, target_price: float):
        """
        Record Target order for position.

        Args:
            symbol: Trading symbol
            target_order_id: Target order ID from broker
            target_price: Target price
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos:
                pos.target_order_id = target_order_id
                pos.target_price = target_price
                logger.info(f"[TRACKER] Target set: {symbol} -> {target_order_id} @ {target_price}")

    def update_sl_order(self, symbol: str, new_sl_order_id: str = None,
                        new_sl_price: float = None):
        """Update SL order after modification."""
        with self._lock:
            pos = self.positions.get(symbol)
            if pos:
                if new_sl_order_id:
                    pos.sl_order_id = new_sl_order_id
                if new_sl_price:
                    pos.sl_price = new_sl_price

    def cancel_sl_order(self, symbol: str) -> Optional[str]:
        """
        Cancel SL order for position.
        Handles race condition where OCO monitor may have already cancelled the order.

        Args:
            symbol: Trading symbol

        Returns:
            Cancelled order ID or None
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos and pos.sl_order_id:
                order_id = pos.sl_order_id

                # Check order status first to avoid race condition with OCO monitor
                try:
                    order_report = self.client.order_report()
                    orders = order_report.get('data', []) if order_report else []
                    order_status = None
                    for o in orders:
                        if o.get('nOrdNo') == order_id:
                            order_status = o.get('ordSt', '').lower()
                            break

                    # If order already completed/cancelled, just cleanup tracker state
                    if order_status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                        logger.info(f"[TRACKER] SL {order_id} already {order_status}, cleaning up")
                        pos.sl_order_id = None
                        pos.sl_price = None
                        return order_id

                    # Order still pending - cancel it
                    self.client.cancel_order(order_id=order_id)
                    logger.info(f"[TRACKER] Cancelled SL: {symbol} -> {order_id}")
                    pos.sl_order_id = None
                    pos.sl_price = None
                    return order_id

                except Exception as e:
                    # On any error, still clear tracker state to prevent stale references
                    logger.warning(f"[TRACKER] SL cancel issue for {order_id}: {e} - clearing state")
                    pos.sl_order_id = None
                    pos.sl_price = None
            return None

    def cancel_target_order(self, symbol: str) -> Optional[str]:
        """
        Cancel Target order for position.
        Handles race condition where OCO monitor may have already cancelled the order.

        Args:
            symbol: Trading symbol

        Returns:
            Cancelled order ID or None
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos and pos.target_order_id:
                order_id = pos.target_order_id

                # Check order status first to avoid race condition with OCO monitor
                try:
                    order_report = self.client.order_report()
                    orders = order_report.get('data', []) if order_report else []
                    order_status = None
                    for o in orders:
                        if o.get('nOrdNo') == order_id:
                            order_status = o.get('ordSt', '').lower()
                            break

                    # If order already completed/cancelled, just cleanup tracker state
                    if order_status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                        logger.info(f"[TRACKER] Target {order_id} already {order_status}, cleaning up")
                        pos.target_order_id = None
                        pos.target_price = None
                        return order_id

                    # Order still pending - cancel it
                    self.client.cancel_order(order_id=order_id)
                    logger.info(f"[TRACKER] Cancelled Target: {symbol} -> {order_id}")
                    pos.target_order_id = None
                    pos.target_price = None
                    return order_id

                except Exception as e:
                    # On any error, still clear tracker state to prevent stale references
                    logger.warning(f"[TRACKER] Target cancel issue for {order_id}: {e} - clearing state")
                    pos.target_order_id = None
                    pos.target_price = None
            return None

    def cancel_all_orders_for_position(self, symbol: str) -> Dict[str, Any]:
        """
        Cancel both SL and Target orders for position.

        Args:
            symbol: Trading symbol

        Returns:
            Dict with cancelled order IDs
        """
        result = {
            'sl_cancelled': None,
            'target_cancelled': None
        }

        result['sl_cancelled'] = self.cancel_sl_order(symbol)
        result['target_cancelled'] = self.cancel_target_order(symbol)

        return result

    def reduce_position_qty(self, symbol: str, exit_qty: int) -> int:
        """
        Reduce tracked position quantity after partial exit.

        Args:
            symbol: Trading symbol
            exit_qty: Quantity exited

        Returns:
            Remaining quantity
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos:
                pos.quantity = max(0, pos.quantity - abs(exit_qty))
                logger.info(f"[TRACKER] Qty reduced: {symbol} -> {pos.quantity} remaining")
                return pos.quantity
            return 0

    def modify_sl_quantity(self, symbol: str, new_qty: int) -> Dict[str, Any]:
        """
        Modify SL order quantity after partial exit.
        Falls back to cancel + recreate if modify fails.

        Args:
            symbol: Trading symbol
            new_qty: New quantity for SL

        Returns:
            Dict with 'success', 'action' ('modified'/'cancelled'/'failed'), 'sl_price'
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos or not pos.sl_order_id:
                return {'success': False, 'action': 'failed', 'error': 'No SL order to modify'}

            old_sl_price = pos.sl_price
            old_sl_id = pos.sl_order_id

            # First try to modify
            try:
                self.client.modify_order(
                    order_id=pos.sl_order_id,
                    quantity=str(new_qty)
                )
                logger.info(f"[TRACKER] SL qty modified: {symbol} -> {new_qty}")
                return {'success': True, 'action': 'modified', 'sl_price': old_sl_price}

            except Exception as e:
                logger.warning(f"[TRACKER] Modify failed: {e}, attempting cancel + recreate")

                # Fallback: Cancel the old SL order
                try:
                    self.client.cancel_order(order_id=old_sl_id)
                    logger.info(f"[TRACKER] Cancelled old SL: {old_sl_id}")
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

    def close_position(self, symbol: str) -> Dict[str, Any]:
        """
        Close position tracking - cancel all orders and remove.

        Args:
            symbol: Trading symbol

        Returns:
            Dict with cleanup results
        """
        result = self.cancel_all_orders_for_position(symbol)

        with self._lock:
            if symbol in self.positions:
                self.positions[symbol].is_active = False
                del self.positions[symbol]
                logger.info(f"[TRACKER] Position closed: {symbol}")

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

        Args:
            broker_positions: List of positions from broker API
        """
        broker_symbols = set()
        for pos in broker_positions:
            symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
            qty = int(pos.get('qty', 0))
            if qty != 0:
                broker_symbols.add(symbol)

        with self._lock:
            # Find positions that are closed at broker
            closed_symbols = []
            for symbol in self.positions.keys():
                if symbol not in broker_symbols:
                    closed_symbols.append(symbol)

            # Clean up closed positions
            for symbol in closed_symbols:
                logger.info(f"[TRACKER] Position no longer at broker: {symbol}")
                # Cancel any pending orders
                pos = self.positions[symbol]
                if pos.sl_order_id:
                    try:
                        self.client.cancel_order(order_id=pos.sl_order_id)
                    except:
                        pass
                if pos.target_order_id:
                    try:
                        self.client.cancel_order(order_id=pos.target_order_id)
                    except:
                        pass
                del self.positions[symbol]

    def has_sl(self, symbol: str) -> bool:
        """Check if position has SL order."""
        with self._lock:
            pos = self.positions.get(symbol)
            return pos is not None and pos.sl_order_id is not None

    def has_target(self, symbol: str) -> bool:
        """Check if position has Target order."""
        with self._lock:
            pos = self.positions.get(symbol)
            return pos is not None and pos.target_order_id is not None

    def get_sl_price(self, symbol: str) -> Optional[float]:
        """Get current SL price for position."""
        with self._lock:
            pos = self.positions.get(symbol)
            return pos.sl_price if pos else None

    def clear_all(self):
        """Clear all tracked positions (for reset)."""
        with self._lock:
            # Cancel all pending orders first
            for symbol, pos in self.positions.items():
                if pos.sl_order_id:
                    try:
                        self.client.cancel_order(order_id=pos.sl_order_id)
                    except:
                        pass
                if pos.target_order_id:
                    try:
                        self.client.cancel_order(order_id=pos.target_order_id)
                    except:
                        pass
            self.positions.clear()
            logger.info("[TRACKER] Cleared all positions")

"""
NEO Trade Terminal - OCO Position Monitor

Monitors positions with SL/Target orders.
Auto-cancels opposite leg when one is hit.
Since NEO doesn't have native OCO, we simulate it.
"""

import threading
import time
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass
import logging

logger = logging.getLogger(__name__)


@dataclass
class OCOPair:
    """Represents an OCO pair (SL + Target)."""
    position_symbol: str
    sl_order_id: str
    target_order_id: str
    sl_trigger: float
    target_price: float
    quantity: int
    side: str  # 'LONG' or 'SHORT'
    entry_price: float
    created_at: float


class OCOMonitor:
    """
    Monitors OCO (One-Cancels-Other) order pairs.
    When SL or Target is hit, cancels the other order automatically.
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

        self.oco_pairs: Dict[str, OCOPair] = {}  # keyed by position symbol
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._check_interval = 1  # seconds

        # CRITICAL: Health monitoring for daemon thread
        self._last_heartbeat: float = 0.0
        self._heartbeat_timeout: float = 10.0  # Alert if no heartbeat for 10 seconds
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = 5  # Alert after 5 consecutive errors

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

    def add_oco_pair(self, position_symbol: str, sl_order_id: str,
                     target_order_id: str, sl_trigger: float,
                     target_price: float, quantity: int, side: str,
                     entry_price: float = 0):
        """
        Register an OCO pair for monitoring.

        NOTE: Uses position_symbol as key. In options trading, each strike/expiry
        has a unique trading symbol (e.g., NIFTY23JAN25000CE), so collisions are
        rare. The broker consolidates positions in the same symbol into one net
        position, so only one OCO pair per symbol is valid at any time.

        Args:
            position_symbol: Trading symbol of the position
            sl_order_id: SL order ID
            target_order_id: Target order ID
            sl_trigger: SL trigger price
            target_price: Target price
            quantity: Position quantity
            side: 'LONG' or 'SHORT'
            entry_price: Entry price for P&L calculation
        """
        with self._lock:
            # Check for existing pair - warn if overwriting
            if position_symbol in self.oco_pairs:
                old_pair = self.oco_pairs[position_symbol]
                logger.warning(f"[OCO] OVERWRITING existing pair for {position_symbol}! "
                              f"Old: SL={old_pair.sl_order_id} TGT={old_pair.target_order_id}")
                # Note: This is expected when SL is modified (new order ID)
                # but could indicate a bug if neither order ID matches

            pair = OCOPair(
                position_symbol=position_symbol,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,
                sl_trigger=sl_trigger,
                target_price=target_price,
                quantity=quantity,
                side=side.upper(),
                entry_price=entry_price,
                created_at=time.time()
            )
            self.oco_pairs[position_symbol] = pair
            logger.info(f"[OCO] Registered: {position_symbol} SL:{sl_order_id} TGT:{target_order_id}")

    def remove_oco_pair(self, position_symbol: str):
        """Remove OCO pair from monitoring."""
        with self._lock:
            if position_symbol in self.oco_pairs:
                del self.oco_pairs[position_symbol]
                logger.info(f"[OCO] Removed: {position_symbol}")

    def remove_order(self, order_id: str):
        """Remove OCO pair by order ID (finds pair containing this order)."""
        with self._lock:
            symbol_to_remove = None
            for symbol, pair in self.oco_pairs.items():
                if pair.sl_order_id == order_id or pair.target_order_id == order_id:
                    symbol_to_remove = symbol
                    break
            if symbol_to_remove:
                del self.oco_pairs[symbol_to_remove]
                logger.info(f"[OCO] Removed by order ID {order_id}: {symbol_to_remove}")

    def remove_pair_by_symbol(self, symbol: str):
        """Remove OCO pair directly by symbol."""
        with self._lock:
            if symbol in self.oco_pairs:
                del self.oco_pairs[symbol]
                logger.info(f"[OCO] Removed pair for: {symbol}")

    def update_sl_order(self, position_symbol: str, new_sl_order_id: str,
                        new_sl_trigger: float = None):
        """Update SL order ID (after trail modification)."""
        with self._lock:
            if position_symbol in self.oco_pairs:
                self.oco_pairs[position_symbol].sl_order_id = new_sl_order_id
                if new_sl_trigger is not None:
                    self.oco_pairs[position_symbol].sl_trigger = new_sl_trigger

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
        """Check status of all OCO pairs."""
        if not self.oco_pairs:
            return

        try:
            # Get all orders
            order_report = self.client.order_report()
            orders_list = order_report.get('data', []) if order_report else []
            orders = {o.get('nOrdNo'): o for o in orders_list}

            with self._lock:
                pairs_to_remove = []

                for symbol, pair in self.oco_pairs.items():
                    sl_order = orders.get(pair.sl_order_id)
                    target_order = orders.get(pair.target_order_id)

                    sl_status = sl_order.get('ordSt', '').lower() if sl_order else 'unknown'
                    target_status = target_order.get('ordSt', '').lower() if target_order else 'unknown'

                    # Check if SL hit
                    if sl_status in ['complete', 'traded', 'filled']:
                        self._handle_sl_hit(pair, sl_order)
                        pairs_to_remove.append(symbol)

                    # Check if Target hit
                    elif target_status in ['complete', 'traded', 'filled']:
                        self._handle_target_hit(pair, target_order)
                        pairs_to_remove.append(symbol)

                    # Check if either cancelled externally
                    elif sl_status in ['cancelled', 'rejected'] and target_status not in ['complete', 'traded', 'filled']:
                        logger.info(f"[OCO] SL cancelled/rejected for {symbol}")
                        pairs_to_remove.append(symbol)

                    elif target_status in ['cancelled', 'rejected'] and sl_status not in ['complete', 'traded', 'filled']:
                        logger.info(f"[OCO] Target cancelled/rejected for {symbol}")
                        pairs_to_remove.append(symbol)

                for symbol in pairs_to_remove:
                    if symbol in self.oco_pairs:
                        del self.oco_pairs[symbol]

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

    def _handle_sl_hit(self, pair: OCOPair, sl_order: Dict[str, Any]):
        """Handle SL order hit - cancel target and record P&L."""
        logger.info(f"[OCO] SL HIT for {pair.position_symbol}")

        # CRITICAL: Lock position to prevent Trail Manager from modifying during exit
        if self.pos_tracker:
            self.pos_tracker.lock_for_exit(pair.position_symbol)

        # Cancel target order with retry
        cancelled = self._cancel_order_with_retry(pair.target_order_id, "Target")
        if not cancelled:
            # CRITICAL: Target order is now orphan - notify immediately
            logger.error(f"[OCO] ORPHAN TARGET ORDER: {pair.target_order_id} for {pair.position_symbol}")
            if self.telegram:
                self.telegram.send(
                    f"CRITICAL: Failed to cancel target order {pair.target_order_id} "
                    f"for {pair.position_symbol}. Manual intervention required!"
                )

        # Calculate P&L
        fill_price = float(sl_order.get('avgPrc', pair.sl_trigger) or pair.sl_trigger)
        filled_qty = int(sl_order.get('fldQty', pair.quantity) or pair.quantity)

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
                    symbol=pair.position_symbol,
                    exit_price=fill_price,
                    exit_qty=filled_qty,
                    order_id=pair.sl_order_id,
                    exit_type='SL'
                )
                logger.info(f"[OCO] P&L recorded for SL hit: {pair.position_symbol} = {pnl:+.2f}")
            except Exception as e:
                logger.warning(f"[OCO] Failed to record P&L: {e}")

        # Notify
        if self.sound:
            self.sound.play('sl_hit')

        if self.telegram:
            self.telegram.notify_sl_hit(
                symbol=pair.position_symbol,
                exit_price=fill_price,
                pnl=pnl,
                pnl_pct=pnl_pct
            )

        # Callback
        if self.on_sl_hit:
            self.on_sl_hit(pair.position_symbol, fill_price, pnl)

    def _handle_target_hit(self, pair: OCOPair, target_order: Dict[str, Any]):
        """Handle target order hit - cancel SL and record P&L."""
        logger.info(f"[OCO] TARGET HIT for {pair.position_symbol}")

        # CRITICAL: Lock position to prevent Trail Manager from modifying during exit
        if self.pos_tracker:
            self.pos_tracker.lock_for_exit(pair.position_symbol)

        # Cancel SL order with retry
        cancelled = self._cancel_order_with_retry(pair.sl_order_id, "SL")
        if not cancelled:
            # CRITICAL: SL order is now orphan - notify immediately
            logger.error(f"[OCO] ORPHAN SL ORDER: {pair.sl_order_id} for {pair.position_symbol}")
            if self.telegram:
                self.telegram.send(
                    f"CRITICAL: Failed to cancel SL order {pair.sl_order_id} "
                    f"for {pair.position_symbol}. Manual intervention required!"
                )

        # Calculate P&L
        fill_price = float(target_order.get('avgPrc', pair.target_price) or pair.target_price)
        filled_qty = int(target_order.get('fldQty', pair.quantity) or pair.quantity)

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
                    symbol=pair.position_symbol,
                    exit_price=fill_price,
                    exit_qty=filled_qty,
                    order_id=pair.target_order_id,
                    exit_type='TARGET'
                )
                logger.info(f"[OCO] P&L recorded for TARGET hit: {pair.position_symbol} = {pnl:+.2f}")
            except Exception as e:
                logger.warning(f"[OCO] Failed to record P&L: {e}")

        # Notify
        if self.sound:
            self.sound.play('target_hit')

        if self.telegram:
            self.telegram.notify_target_hit(
                symbol=pair.position_symbol,
                exit_price=fill_price,
                pnl=pnl,
                pnl_pct=pnl_pct
            )

        # Callback
        if self.on_target_hit:
            self.on_target_hit(pair.position_symbol, fill_price, pnl)

    def get_active_pairs(self) -> List[Dict[str, Any]]:
        """Get list of active OCO pairs."""
        with self._lock:
            return [
                {
                    'symbol': p.position_symbol,
                    'sl_order': p.sl_order_id,
                    'target_order': p.target_order_id,
                    'sl_price': p.sl_trigger,
                    'target_price': p.target_price,
                    'side': p.side,
                    'entry_price': p.entry_price,
                }
                for p in self.oco_pairs.values()
            ]

    def get_pair(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get OCO pair for a symbol."""
        with self._lock:
            pair = self.oco_pairs.get(symbol)
            if not pair:
                return None
            return {
                'symbol': pair.position_symbol,
                'sl_order': pair.sl_order_id,
                'target_order': pair.target_order_id,
                'sl_price': pair.sl_trigger,
                'target_price': pair.target_price,
                'side': pair.side,
                'entry_price': pair.entry_price,
            }

    def is_monitoring(self, symbol: str) -> bool:
        """Check if symbol is being monitored."""
        with self._lock:
            return symbol in self.oco_pairs

    def clear_all(self):
        """Clear all OCO pairs."""
        with self._lock:
            self.oco_pairs.clear()
            logger.info("[OCO] Cleared all pairs")

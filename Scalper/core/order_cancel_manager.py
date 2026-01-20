"""
NEO Trade Terminal - Order Cancel Manager

Centralized order cancellation with:
- Race condition prevention between OCO Monitor and Position Tracker
- Status check before cancel (avoid cancelling filled orders)
- Retry logic with exponential backoff
- Thread-safe operation tracking

This prevents the scenario where both OCO Monitor and Position Tracker
try to cancel the same order simultaneously, causing errors or double attempts.
"""

import threading
import time
from typing import Dict, Set, Optional, Tuple
from datetime import datetime, timedelta
import logging

logger = logging.getLogger(__name__)


class OrderCancelManager:
    """
    Thread-safe centralized order cancellation.

    Prevents race conditions by:
    1. Tracking orders currently being cancelled
    2. Caching recently cancelled orders (avoid re-cancel attempts)
    3. Checking order status before attempting cancel
    """

    # Order statuses that indicate order is already terminal
    TERMINAL_STATUSES = {
        'complete', 'traded', 'filled', 'cancelled', 'canceled', 'rejected'
    }

    def __init__(self, neo_client):
        self.client = neo_client
        self._lock = threading.Lock()

        # Orders currently being cancelled (prevents simultaneous attempts)
        self._in_progress: Set[str] = set()

        # Recently cancelled orders with timestamp (cache for 60 seconds)
        self._recently_cancelled: Dict[str, datetime] = {}
        self._cache_ttl_seconds = 60

        # Statistics
        self.stats = {
            'attempts': 0,
            'success': 0,
            'already_terminal': 0,
            'in_progress_skip': 0,
            'recently_cancelled_skip': 0,
            'failed': 0
        }

    def cancel_order(self, order_id: str, reason: str = "",
                     max_retries: int = 3) -> Tuple[bool, str]:
        """
        Cancel an order with race condition protection.

        Args:
            order_id: Order ID to cancel
            reason: Reason for cancellation (for logging)
            max_retries: Maximum retry attempts

        Returns:
            Tuple of (success: bool, message: str)
        """
        if not order_id:
            return False, "No order ID provided"

        with self._lock:
            self.stats['attempts'] += 1

            # Clean up old cache entries
            self._cleanup_cache()

            # Check if already recently cancelled
            if order_id in self._recently_cancelled:
                self.stats['recently_cancelled_skip'] += 1
                return True, f"Order {order_id} already cancelled recently"

            # Check if cancellation is in progress
            if order_id in self._in_progress:
                self.stats['in_progress_skip'] += 1
                return False, f"Order {order_id} cancellation already in progress"

            # Mark as in progress
            self._in_progress.add(order_id)

        try:
            # Check order status first (outside lock to avoid blocking)
            status_result = self._check_order_status(order_id)
            if status_result:
                status = status_result.lower()
                if status in self.TERMINAL_STATUSES:
                    with self._lock:
                        self._in_progress.discard(order_id)
                        self._recently_cancelled[order_id] = datetime.now()
                        self.stats['already_terminal'] += 1
                    return True, f"Order {order_id} already {status}"

            # Attempt cancellation with retry
            success, message = self._cancel_with_retry(order_id, reason, max_retries)

            with self._lock:
                self._in_progress.discard(order_id)
                if success:
                    self._recently_cancelled[order_id] = datetime.now()
                    self.stats['success'] += 1
                else:
                    self.stats['failed'] += 1

            return success, message

        except Exception as e:
            with self._lock:
                self._in_progress.discard(order_id)
                self.stats['failed'] += 1
            return False, f"Cancel error: {e}"

    def _check_order_status(self, order_id: str) -> Optional[str]:
        """Check current order status."""
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            for order in orders:
                if order.get('nOrdNo') == order_id:
                    return order.get('ordSt', '')

            return None  # Order not found

        except Exception as e:
            logger.warning(f"[CANCEL] Failed to check status for {order_id}: {e}")
            return None

    def _cancel_with_retry(self, order_id: str, reason: str,
                           max_retries: int) -> Tuple[bool, str]:
        """Cancel order with exponential backoff retry."""
        last_error = ""

        for attempt in range(max_retries):
            try:
                self.client.cancel_order(order_id=order_id)
                log_reason = f" ({reason})" if reason else ""
                logger.info(f"[CANCEL] Order {order_id} cancelled{log_reason}")
                return True, "Cancelled successfully"

            except Exception as e:
                last_error = str(e)
                error_lower = last_error.lower()

                # Check if error indicates order already terminal
                if any(term in error_lower for term in
                       ['already', 'completed', 'cancelled', 'rejected', 'not found']):
                    logger.info(f"[CANCEL] Order {order_id} already terminal: {last_error}")
                    return True, f"Already terminal: {last_error}"

                if attempt < max_retries - 1:
                    delay = 0.5 * (2 ** attempt)  # 0.5s, 1s, 2s
                    logger.warning(f"[CANCEL] Retry {attempt + 1}/{max_retries} "
                                 f"for {order_id}: {last_error}, waiting {delay}s")
                    time.sleep(delay)

        logger.error(f"[CANCEL] Failed after {max_retries} attempts: {order_id} - {last_error}")
        return False, f"Failed after {max_retries} attempts: {last_error}"

    def _cleanup_cache(self):
        """Remove expired entries from recently cancelled cache."""
        now = datetime.now()
        expired = [
            order_id for order_id, ts in self._recently_cancelled.items()
            if now - ts > timedelta(seconds=self._cache_ttl_seconds)
        ]
        for order_id in expired:
            del self._recently_cancelled[order_id]

    def is_cancellation_in_progress(self, order_id: str) -> bool:
        """Check if an order is currently being cancelled."""
        with self._lock:
            return order_id in self._in_progress

    def was_recently_cancelled(self, order_id: str) -> bool:
        """Check if an order was recently cancelled."""
        with self._lock:
            self._cleanup_cache()
            return order_id in self._recently_cancelled

    def get_stats(self) -> Dict[str, int]:
        """Get cancellation statistics."""
        with self._lock:
            return self.stats.copy()

    def reset_stats(self):
        """Reset statistics."""
        with self._lock:
            for key in self.stats:
                self.stats[key] = 0

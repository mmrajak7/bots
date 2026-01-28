"""
NEO Trade Terminal - WebSocket Handler

Handles live market data and order updates via WebSocket.
"""

from typing import Callable, List, Dict, Optional, Any
import threading
import logging

logger = logging.getLogger(__name__)


class WebSocketHandler:
    """Handles WebSocket connections for live data."""

    def __init__(self, neo_client):
        self.client = neo_client
        self.subscribed_tokens: List[Dict[str, str]] = []
        self._subscribed_indices: List[str] = []  # Track subscribed indices for reconnection
        self.callbacks: Dict[str, List[Callable]] = {
            'ltp_update': [],
            'order_update': [],
            'position_update': [],
            'error': [],
            'close': [],
            'open': [],
            'reconnecting': []  # New event for reconnection attempts
        }
        self._running = False
        self._connected = False
        self._lock = threading.Lock()

        # Reconnection settings
        self._reconnect_enabled = True
        self._reconnect_attempts = 0
        self._max_reconnect_attempts = 5
        self._reconnect_delay_base = 2.0  # seconds (exponential backoff)
        self._reconnect_thread: Optional[threading.Thread] = None
        self._stop_reconnect = threading.Event()

    def setup_callbacks(self):
        """Setup NEO websocket callbacks."""
        if not self.client:
            return

        # NEO API uses callback methods on client
        self.client.on_message = self._on_message
        self.client.on_error = self._on_error
        self.client.on_close = self._on_close
        self.client.on_open = self._on_open

        logger.info("WebSocket callbacks configured")

    def subscribe_ltp(self, tokens: List[Dict[str, str]]) -> bool:
        """
        Subscribe to live LTP feed for tokens.

        Args:
            tokens: List of {'instrument_token': str, 'exchange_segment': str}

        Returns:
            True if subscription successful
        """
        if not self.client:
            return False

        try:
            self.client.subscribe(
                instrument_tokens=tokens,
                isIndex=False,
                isDepth=False
            )
            with self._lock:
                self.subscribed_tokens.extend(tokens)
            logger.info(f"Subscribed to {len(tokens)} instruments")
            return True
        except Exception as e:
            logger.error(f"LTP subscription failed: {e}")
            return False

    def subscribe_index(self, indices: List[str]) -> bool:
        """
        Subscribe to index LTP feed.

        Args:
            indices: List of index names like ['NIFTY', 'BANKNIFTY']

        Returns:
            True if subscription successful
        """
        if not self.client:
            return False

        # Index tokens mapping
        # NSE indices use nse_cm, BSE indices use bse_cm
        index_tokens = {
            'NIFTY': {'instrument_token': '26000', 'exchange_segment': 'nse_cm'},
            'BANKNIFTY': {'instrument_token': '26009', 'exchange_segment': 'nse_cm'},
            'FINNIFTY': {'instrument_token': '26037', 'exchange_segment': 'nse_cm'},
            'SENSEX': {'instrument_token': '1', 'exchange_segment': 'bse_cm'},
            'BANKEX': {'instrument_token': '12', 'exchange_segment': 'bse_cm'},
        }

        tokens = []
        for idx in indices:
            if idx.upper() in index_tokens:
                tokens.append(index_tokens[idx.upper()])

        if tokens:
            try:
                self.client.subscribe(
                    instrument_tokens=tokens,
                    isIndex=True,
                    isDepth=False
                )
                # Track subscribed indices for reconnection
                with self._lock:
                    self._subscribed_indices = [idx.upper() for idx in indices]
                logger.info(f"Subscribed to indices: {indices}")
                return True
            except Exception as e:
                logger.error(f"Index subscription failed: {e}")

        return False

    def subscribe_order_feed(self) -> bool:
        """
        Subscribe to order status updates.

        Returns:
            True if subscription successful
        """
        if not self.client:
            return False

        try:
            self.client.subscribe_to_orderfeed()
            logger.info("Subscribed to order feed")
            return True
        except Exception as e:
            logger.error(f"Order feed subscription failed: {e}")
            return False

    def register_callback(self, event_type: str, callback: Callable):
        """
        Register callback for specific event type.

        Args:
            event_type: One of 'ltp_update', 'order_update', 'position_update', 'error', 'close', 'open'
            callback: Callback function to invoke
        """
        if event_type in self.callbacks:
            with self._lock:
                self.callbacks[event_type].append(callback)
            logger.debug(f"Registered callback for {event_type}")

    def unregister_callback(self, event_type: str, callback: Callable):
        """
        Unregister a callback.

        Args:
            event_type: Event type
            callback: Callback to remove
        """
        if event_type in self.callbacks:
            with self._lock:
                if callback in self.callbacks[event_type]:
                    self.callbacks[event_type].remove(callback)

    def _on_message(self, message: Any):
        """Handle incoming websocket message."""
        try:
            # Parse message and route to appropriate callbacks
            if isinstance(message, dict):
                # Check message type - NEO uses 'lp' for last price, 'ltp' for some
                if 'lp' in message or 'ltp' in message or 'last_price' in message:
                    self._dispatch('ltp_update', message)

                elif 'order' in message or 'ordSt' in message or 'nOrdNo' in message:
                    self._dispatch('order_update', message)

                elif 'position' in message:
                    self._dispatch('position_update', message)

                else:
                    # Generic message - might still contain price data
                    # NEO sometimes sends data with just 'tk' (token) and 'lp' (last price)
                    if 'tk' in message:
                        self._dispatch('ltp_update', message)
                    else:
                        logger.debug(f"[WS] Unknown message format: {list(message.keys())}")

            elif isinstance(message, list):
                # Batch of updates
                for msg in message:
                    self._on_message(msg)

        except Exception as e:
            logger.error(f"Error processing websocket message: {e}")

    def _dispatch(self, event_type: str, data: Any):
        """Dispatch event to registered callbacks."""
        with self._lock:
            callbacks = self.callbacks.get(event_type, [])[:]

        for callback in callbacks:
            try:
                callback(data)
            except Exception as e:
                logger.error(f"Callback error for {event_type}: {e}")

    def _on_error(self, error: Any):
        """Handle websocket error."""
        logger.error(f"WebSocket error: {error}")
        self._dispatch('error', error)

    def _on_close(self, message: Any):
        """Handle websocket close."""
        logger.info(f"WebSocket closed: {message}")
        # S27: Protect state modifications with lock for thread safety
        with self._lock:
            was_connected = self._connected
            self._running = False
            self._connected = False
        self._dispatch('close', message)

        # Attempt reconnection if enabled and was previously connected
        if self._reconnect_enabled and was_connected:
            self._start_reconnect()

    def _on_open(self, message: Any):
        """Handle websocket open."""
        logger.info(f"WebSocket connected: {message}")
        # S27: Protect state modifications with lock for thread safety
        with self._lock:
            self._running = True
            self._connected = True
        self._dispatch('open', message)

    def unsubscribe_all(self):
        """Unsubscribe from all feeds."""
        if self.subscribed_tokens and self.client:
            try:
                self.client.un_subscribe(instrument_tokens=self.subscribed_tokens)
                with self._lock:
                    self.subscribed_tokens.clear()
                logger.info("Unsubscribed from all instruments")
            except Exception as e:
                logger.error(f"Unsubscribe failed: {e}")

    def is_connected(self) -> bool:
        """Check if websocket is connected."""
        # S27: Read with lock for thread safety
        with self._lock:
            return self._connected

    def get_subscribed_count(self) -> int:
        """Get count of subscribed instruments."""
        with self._lock:
            return len(self.subscribed_tokens)

    def _start_reconnect(self):
        """Start reconnection attempt in background thread."""
        # S26: Protect thread creation with lock to prevent race condition
        with self._lock:
            if self._reconnect_thread and self._reconnect_thread.is_alive():
                return  # Already reconnecting

            self._stop_reconnect.clear()
            self._reconnect_thread = threading.Thread(
                target=self._reconnect_loop,
                daemon=True,
                name="ws-reconnect"
            )
            self._reconnect_thread.start()

    def _reconnect_loop(self):
        """Reconnection loop with exponential backoff."""
        import time

        self._reconnect_attempts = 0
        saved_tokens = []
        saved_indices = []

        # Save currently subscribed tokens and indices for resubscription
        with self._lock:
            saved_tokens = self.subscribed_tokens.copy()
            saved_indices = self._subscribed_indices.copy()

        while not self._stop_reconnect.is_set() and self._reconnect_attempts < self._max_reconnect_attempts:
            self._reconnect_attempts += 1
            delay = self._reconnect_delay_base * (2 ** (self._reconnect_attempts - 1))
            delay = min(delay, 60)  # Cap at 60 seconds

            logger.info(f"[WS] Reconnect attempt {self._reconnect_attempts}/{self._max_reconnect_attempts} "
                       f"in {delay:.1f}s...")
            self._dispatch('reconnecting', {
                'attempt': self._reconnect_attempts,
                'max_attempts': self._max_reconnect_attempts,
                'delay': delay
            })

            # Wait before reconnecting
            if self._stop_reconnect.wait(timeout=delay):
                return  # Stop requested

            try:
                # Re-setup callbacks
                self.setup_callbacks()

                # Resubscribe to order feed
                self.subscribe_order_feed()

                # Resubscribe to indices (CRITICAL: was missing, causing index updates to stop)
                if saved_indices:
                    self.subscribe_index(saved_indices)

                # Resubscribe to saved tokens if any
                # CRITICAL FIX (S25 WS-7): Clear subscribed_tokens before resubscribing
                # to prevent duplicates (subscribe_ltp extends the list)
                if saved_tokens:
                    with self._lock:
                        self.subscribed_tokens.clear()
                    self.subscribe_ltp(saved_tokens)

                # CRITICAL: Verify connection was actually established
                # S31: Poll for connection with timeout instead of fixed wait
                import time as time_mod
                max_wait = 5.0  # Max 5 seconds to wait for connection
                poll_interval = 0.2
                elapsed = 0.0

                while elapsed < max_wait:
                    time_mod.sleep(poll_interval)
                    elapsed += poll_interval
                    if self._connected:
                        logger.info(f"[WS] Reconnection successful - verified after {elapsed:.1f}s")
                        self._reconnect_attempts = 0
                        return

                # Timed out waiting for connection
                logger.warning(f"[WS] Reconnect attempt {self._reconnect_attempts} - "
                             f"subscription succeeded but connection not verified after {max_wait}s")
                # Continue to next attempt

            except Exception as e:
                logger.warning(f"[WS] Reconnect attempt {self._reconnect_attempts} failed: {e}")

        # Max attempts reached
        if self._reconnect_attempts >= self._max_reconnect_attempts:
            logger.error(f"[WS] Max reconnection attempts ({self._max_reconnect_attempts}) reached. Giving up.")
            self._dispatch('error', {
                'type': 'reconnect_failed',
                'message': f'WebSocket reconnection failed after {self._max_reconnect_attempts} attempts'
            })

    def stop_reconnect(self):
        """Stop ongoing reconnection attempts."""
        self._stop_reconnect.set()
        if self._reconnect_thread and self._reconnect_thread.is_alive():
            self._reconnect_thread.join(timeout=2)

    def set_reconnect_enabled(self, enabled: bool):
        """Enable or disable automatic reconnection."""
        self._reconnect_enabled = enabled
        if not enabled:
            self.stop_reconnect()


class LTPCache:
    """Thread-safe cache for LTP data."""

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def update(self, token: str, data: Dict[str, Any]):
        """Update cache with new LTP data."""
        from datetime import datetime
        with self._lock:
            if token not in self._cache:
                self._cache[token] = {}
            self._cache[token].update(data)
            # Use provided timestamp or current time as fallback
            self._cache[token]['last_update'] = data.get('timestamp') or datetime.now()

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        """Get cached data for token."""
        with self._lock:
            return self._cache.get(token, {}).copy()

    def get_ltp(self, token: str) -> Optional[float]:
        """Get LTP for token."""
        with self._lock:
            data = self._cache.get(token, {})
            # S26: Use explicit None check - price of 0 is valid (rare but possible)
            ltp = data.get('ltp')
            return ltp if ltp is not None else data.get('last_price')

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached data."""
        with self._lock:
            return {k: v.copy() for k, v in self._cache.items()}

    def clear(self):
        """Clear cache."""
        with self._lock:
            self._cache.clear()

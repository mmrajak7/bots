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
        self.callbacks: Dict[str, List[Callable]] = {
            'ltp_update': [],
            'order_update': [],
            'position_update': [],
            'error': [],
            'close': [],
            'open': []
        }
        self._running = False
        self._connected = False
        self._lock = threading.Lock()

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
                # Check message type
                if 'ltp' in message or 'last_price' in message:
                    self._dispatch('ltp_update', message)

                elif 'order' in message or 'ordSt' in message or 'nOrdNo' in message:
                    self._dispatch('order_update', message)

                elif 'position' in message:
                    self._dispatch('position_update', message)

                else:
                    # Generic LTP update (NEO format)
                    self._dispatch('ltp_update', message)

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
        self._running = False
        self._connected = False
        self._dispatch('close', message)

    def _on_open(self, message: Any):
        """Handle websocket open."""
        logger.info(f"WebSocket connected: {message}")
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
        return self._connected

    def get_subscribed_count(self) -> int:
        """Get count of subscribed instruments."""
        with self._lock:
            return len(self.subscribed_tokens)


class LTPCache:
    """Thread-safe cache for LTP data."""

    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def update(self, token: str, data: Dict[str, Any]):
        """Update cache with new LTP data."""
        with self._lock:
            if token not in self._cache:
                self._cache[token] = {}
            self._cache[token].update(data)
            self._cache[token]['last_update'] = data.get('timestamp')

    def get(self, token: str) -> Optional[Dict[str, Any]]:
        """Get cached data for token."""
        with self._lock:
            return self._cache.get(token, {}).copy()

    def get_ltp(self, token: str) -> Optional[float]:
        """Get LTP for token."""
        with self._lock:
            data = self._cache.get(token, {})
            return data.get('ltp') or data.get('last_price')

    def get_all(self) -> Dict[str, Dict[str, Any]]:
        """Get all cached data."""
        with self._lock:
            return {k: v.copy() for k, v in self._cache.items()}

    def clear(self):
        """Clear cache."""
        with self._lock:
            self._cache.clear()

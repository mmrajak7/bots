"""
WebSocket ticker for real-time market data.
Builds 5-minute bars from tick data.
"""

import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from typing import Optional, Callable, List, Dict

try:
    from kiteconnect import KiteTicker, KiteConnect
except ImportError:
    KiteTicker = None
    KiteConnect = None

from ..core.bar import Bar
from ..utils.time_utils import get_bar_number, MARKET_OPEN, MARKET_CLOSE


# Paths
SCRIPT_DIR = Path(__file__).resolve().parent
CONVICTBAR_DIR = SCRIPT_DIR.parent.parent
BOTS_DIR = CONVICTBAR_DIR.parent
DATA_DIR = BOTS_DIR / 'data'
TOKEN_FILE = DATA_DIR / 'kite_access_token.json'

# Logging
logger = logging.getLogger(__name__)


class BarBuilder:
    """
    Builds OHLCV bars from tick data.
    Accumulates ticks and emits completed bars on bar boundary.
    """

    def __init__(
        self,
        timeframe_minutes: int = 5,
        on_bar_complete: Optional[Callable[[Bar], None]] = None
    ):
        """
        Initialize bar builder.

        Args:
            timeframe_minutes: Bar timeframe in minutes
            on_bar_complete: Callback when a bar completes
        """
        self.timeframe = timeframe_minutes
        self.on_bar_complete = on_bar_complete

        self._lock = Lock()
        self._current_bar_start: Optional[datetime] = None
        self._open: Optional[float] = None
        self._high: Optional[float] = None
        self._low: Optional[float] = None
        self._close: Optional[float] = None
        self._volume: int = 0
        self._tick_count: int = 0

    def _get_bar_start(self, dt: datetime) -> datetime:
        """Get the start time of the bar containing this timestamp."""
        # Round down to bar boundary
        minutes = dt.hour * 60 + dt.minute
        bar_minutes = (minutes // self.timeframe) * self.timeframe
        bar_hour, bar_min = divmod(bar_minutes, 60)
        return dt.replace(hour=bar_hour, minute=bar_min, second=0, microsecond=0)

    def process_tick(self, ltp: float, timestamp: datetime, volume: int = 0) -> Optional[Bar]:
        """
        Process a tick and potentially emit a completed bar.

        Args:
            ltp: Last traded price
            timestamp: Tick timestamp
            volume: Tick volume (optional)

        Returns:
            Completed Bar if a bar boundary was crossed, None otherwise
        """
        completed_bar = None
        tick_bar_start = self._get_bar_start(timestamp)

        with self._lock:
            # Check if we're in a new bar
            if self._current_bar_start is not None and tick_bar_start != self._current_bar_start:
                # Complete the previous bar
                if self._open is not None:
                    completed_bar = Bar(
                        datetime=self._current_bar_start,
                        open=self._open,
                        high=self._high,
                        low=self._low,
                        close=self._close,
                        volume=self._volume,
                        bar_number=get_bar_number(self._current_bar_start),
                    )

                # Reset for new bar
                self._current_bar_start = tick_bar_start
                self._open = ltp
                self._high = ltp
                self._low = ltp
                self._close = ltp
                self._volume = volume
                self._tick_count = 1
            else:
                # Same bar, update OHLC
                self._current_bar_start = tick_bar_start
                if self._open is None:
                    self._open = ltp
                    self._high = ltp
                    self._low = ltp
                else:
                    self._high = max(self._high, ltp)
                    self._low = min(self._low, ltp)
                self._close = ltp
                self._volume += volume
                self._tick_count += 1

        # Call callback if bar completed
        if completed_bar and self.on_bar_complete:
            self.on_bar_complete(completed_bar)

        return completed_bar

    def get_current_bar(self) -> Optional[dict]:
        """Get current incomplete bar data."""
        with self._lock:
            if self._open is None:
                return None
            return {
                'bar_start': self._current_bar_start,
                'open': self._open,
                'high': self._high,
                'low': self._low,
                'close': self._close,
                'volume': self._volume,
                'tick_count': self._tick_count,
            }

    def reset(self):
        """Reset bar builder state."""
        with self._lock:
            self._current_bar_start = None
            self._open = None
            self._high = None
            self._low = None
            self._close = None
            self._volume = 0
            self._tick_count = 0


class LiveTicker:
    """
    WebSocket ticker for real-time NIFTY data.
    Manages connection, reconnection, and bar building.
    """

    def __init__(
        self,
        instrument_tokens: List[int],
        on_tick: Optional[Callable[[dict], None]] = None,
        on_bar: Optional[Callable[[Bar], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        """
        Initialize ticker.

        Args:
            instrument_tokens: List of tokens to subscribe
            on_tick: Callback for each tick
            on_bar: Callback when a 5-min bar completes
            on_connect: Callback when connected
            on_disconnect: Callback when disconnected
        """
        if KiteTicker is None:
            raise ImportError("kiteconnect not installed")

        self.tokens = instrument_tokens
        self.on_tick_callback = on_tick
        self.on_bar_callback = on_bar
        self.on_connect_callback = on_connect
        self.on_disconnect_callback = on_disconnect

        self.ticker: Optional[KiteTicker] = None
        self.kite: Optional[KiteConnect] = None
        self.connected = False
        self.last_tick_time: Optional[datetime] = None

        # Bar builders for each token
        self.bar_builders: Dict[int, BarBuilder] = {}
        for token in instrument_tokens:
            self.bar_builders[token] = BarBuilder(
                timeframe_minutes=5,
                on_bar_complete=self._on_bar_complete
            )

    def _load_credentials(self) -> dict:
        """Load API credentials from token file."""
        if not TOKEN_FILE.exists():
            raise FileNotFoundError(f"Token file not found: {TOKEN_FILE}")

        with open(TOKEN_FILE) as f:
            return json.load(f)

    def _on_ticks(self, ws, ticks: List[dict]):
        """Handle incoming ticks."""
        for tick in ticks:
            token = tick.get('instrument_token')
            ltp = tick.get('last_price')
            timestamp = tick.get('exchange_timestamp') or datetime.now()
            volume = tick.get('volume_traded', 0)

            if token and ltp:
                self.last_tick_time = datetime.now()

                # Process through bar builder
                if token in self.bar_builders:
                    self.bar_builders[token].process_tick(ltp, timestamp, volume)

                # Call user tick callback
                if self.on_tick_callback:
                    self.on_tick_callback(tick)

    def _on_bar_complete(self, bar: Bar):
        """Called when bar builder emits a completed bar."""
        logger.info(f"Bar complete: {bar}")
        if self.on_bar_callback:
            self.on_bar_callback(bar)

    def _on_connect(self, ws, response):
        """Handle WebSocket connect."""
        logger.info("WebSocket connected")
        self.connected = True

        # Subscribe to tokens in full mode for complete tick data
        ws.subscribe(self.tokens)
        ws.set_mode(ws.MODE_FULL, self.tokens)

        if self.on_connect_callback:
            self.on_connect_callback()

    def _on_close(self, ws, code, reason):
        """Handle WebSocket close."""
        logger.warning(f"WebSocket closed: {code} - {reason}")
        self.connected = False

        if self.on_disconnect_callback:
            self.on_disconnect_callback()

    def _on_error(self, ws, code, reason):
        """Handle WebSocket error."""
        logger.error(f"WebSocket error: {code} - {reason}")

    def _on_reconnect(self, ws, attempts_count):
        """Handle reconnection attempt."""
        logger.info(f"WebSocket reconnecting, attempt {attempts_count}")

    def start(self) -> bool:
        """
        Start the WebSocket connection.

        Returns:
            True if connected successfully
        """
        try:
            creds = self._load_credentials()
            api_key = creds['api_key']
            access_token = creds['access_token']

            # Validate token first
            self.kite = KiteConnect(api_key=api_key)
            self.kite.set_access_token(access_token)

            try:
                profile = self.kite.profile()
                logger.info(f"Authenticated as: {profile.get('user_name', 'Unknown')}")
            except Exception as e:
                logger.error(f"Token validation failed: {e}")
                return False

            # Create ticker
            self.ticker = KiteTicker(api_key, access_token)
            self.ticker.on_ticks = self._on_ticks
            self.ticker.on_connect = self._on_connect
            self.ticker.on_close = self._on_close
            self.ticker.on_error = self._on_error
            self.ticker.on_reconnect = self._on_reconnect

            # Connect in threaded mode
            self.ticker.connect(threaded=True)

            # Wait for connection
            for _ in range(15):
                if self.connected:
                    return True
                time.sleep(1)

            logger.error("WebSocket connection timeout")
            return False

        except Exception as e:
            logger.error(f"Failed to start ticker: {e}")
            return False

    def stop(self):
        """Stop the WebSocket connection."""
        if self.ticker:
            self.ticker.close()
            self.ticker = None
        self.connected = False
        logger.info("Ticker stopped")

    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self.connected

    def get_last_tick_time(self) -> Optional[datetime]:
        """Get time of last received tick."""
        return self.last_tick_time

    def get_current_bar(self, token: int) -> Optional[dict]:
        """Get current incomplete bar for a token."""
        if token in self.bar_builders:
            return self.bar_builders[token].get_current_bar()
        return None

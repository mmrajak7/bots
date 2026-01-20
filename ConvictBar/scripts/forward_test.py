#!/usr/bin/env python3
"""
ConvictBar Forward Test Script.

Paper trading with live WebSocket data and Telegram alerts.
"""

import sys
import signal
import logging
import time
from datetime import datetime, time as dt_time
from pathlib import Path

# Add project root to path
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.live.ticker import LiveTicker
from src.live.strategy import LiveStrategy
from src.live.alerts import TelegramAlerts
from src.utils.config import load_config


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(PROJECT_ROOT / 'logs' / 'forward_test.log'),
    ]
)
logger = logging.getLogger(__name__)


class ForwardTest:
    """
    Forward test runner.

    Connects to live WebSocket, processes bars, and generates paper trades.
    """

    def __init__(self):
        """Initialize forward test components."""
        # Create logs directory
        (PROJECT_ROOT / 'logs').mkdir(exist_ok=True)
        (PROJECT_ROOT / 'state').mkdir(exist_ok=True)

        # Load config
        self.config = load_config()
        logger.info(f"Loaded config for {self.config.instrument.symbol}")

        # Initialize alerts
        self.alerts = TelegramAlerts()

        # Initialize strategy
        self.strategy = LiveStrategy(
            config=self.config,
            alerts=self.alerts,
            persist_state=True,
        )

        # Initialize ticker
        self.ticker = LiveTicker(
            instrument_tokens=[self.config.instrument.instrument_token],
            on_bar=self._on_bar_complete,
            on_connect=self._on_connect,
            on_disconnect=self._on_disconnect,
        )

        self.running = False

    def _on_bar_complete(self, bar):
        """Handle completed bar from ticker."""
        logger.info(f"Bar complete: {bar.datetime} O:{bar.open:.2f} H:{bar.high:.2f} L:{bar.low:.2f} C:{bar.close:.2f}")
        self.strategy.on_bar_complete(bar)

    def _on_connect(self):
        """Handle WebSocket connect."""
        logger.info("WebSocket connected")
        self.alerts.send_connection_status(True)

    def _on_disconnect(self):
        """Handle WebSocket disconnect."""
        logger.warning("WebSocket disconnected")
        self.alerts.send_connection_status(False)

    def _is_market_hours(self) -> bool:
        """Check if current time is within market hours."""
        now = datetime.now().time()
        market_open = dt_time(9, 15)
        market_close = dt_time(15, 30)
        return market_open <= now <= market_close

    def _wait_for_market(self) -> None:
        """Wait until market opens."""
        while not self._is_market_hours():
            now = datetime.now()
            market_open = now.replace(hour=9, minute=15, second=0)

            if now.time() < dt_time(9, 15):
                wait_seconds = (market_open - now).total_seconds()
                logger.info(f"Market closed. Waiting {wait_seconds/60:.1f} minutes for market open...")
                time.sleep(min(wait_seconds, 300))  # Check every 5 minutes max
            else:
                # After market close, wait until next day
                logger.info("Market closed for the day. Exiting.")
                return

    def start(self) -> None:
        """Start forward test."""
        logger.info("=" * 50)
        logger.info("ConvictBar Forward Test Starting")
        logger.info("=" * 50)

        # Send startup alert
        self.alerts.send_startup([self.config.instrument.instrument_token])

        # Start strategy
        self.strategy.start()

        # Connect to WebSocket
        logger.info("Connecting to WebSocket...")
        if not self.ticker.start():
            logger.error("Failed to connect to WebSocket")
            self.alerts.send_error("WebSocket connection failed", "Startup")
            return

        self.running = True
        logger.info("Forward test running. Press Ctrl+C to stop.")

        # Main loop
        try:
            while self.running:
                # Check if market is open
                if not self._is_market_hours():
                    logger.info("Market closed, stopping ticker...")
                    break

                # Check connection health
                if not self.ticker.is_connected():
                    logger.warning("Connection lost, waiting for reconnect...")

                # Log status periodically
                status = self.strategy.get_status()
                if status['bars_today'] > 0 and status['bars_today'] % 6 == 0:  # Every 30 minutes
                    logger.info(f"Status: {status}")

                time.sleep(30)  # Check every 30 seconds

        except KeyboardInterrupt:
            logger.info("Interrupted by user")

        self.stop()

    def stop(self) -> None:
        """Stop forward test."""
        self.running = False

        # Stop ticker
        logger.info("Stopping ticker...")
        self.ticker.stop()

        # Stop strategy (saves state and sends summary)
        logger.info("Stopping strategy...")
        self.strategy.stop()

        # Send shutdown alert
        self.alerts.send_shutdown("User requested stop")

        logger.info("Forward test stopped")


def main():
    """Main entry point."""
    # Create forward test instance
    ft = ForwardTest()

    # Handle signals
    def signal_handler(sig, frame):
        logger.info("Received signal, stopping...")
        ft.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start forward test
    ft.start()


if __name__ == '__main__':
    main()

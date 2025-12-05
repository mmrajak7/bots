#!/usr/bin/env python3
"""
SNAIL Telegram Polling Daemon

Run as a systemd service for continuous Telegram command handling.
Handles user responses to trading alerts (HOLD, EXIT, ADJUST).

Usage:
    python scripts/telegram_poller.py

Systemd service:
    sudo systemctl start snail-telegram
    sudo systemctl status snail-telegram
"""

import os
import sys
import time
import signal
from pathlib import Path
from datetime import datetime

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Load environment variables
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / '.env')

from loguru import logger


def setup_logging():
    """Configure logging for the polling daemon."""
    log_file = PROJECT_ROOT / "logs" / "telegram_poller.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # Remove default handler
    logger.remove()

    # Add file handler with rotation
    logger.add(
        str(log_file),
        rotation="10 MB",
        retention="7 days",
        format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {message}",
        level="INFO"
    )

    # Add stdout for systemd journal
    logger.add(
        sys.stdout,
        format="{time:HH:mm:ss} | {level: <8} | {message}",
        level="INFO"
    )


def main():
    """Run Telegram polling loop."""
    setup_logging()

    logger.info("=" * 50)
    logger.info("SNAIL Telegram Polling Daemon Starting")
    logger.info(f"Project root: {PROJECT_ROOT}")
    logger.info("=" * 50)

    # Import after path setup
    from src.api.telegram_bot import TelegramBot

    # Create bot instance
    bot = TelegramBot()

    # Handle shutdown signals
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, shutting down...")
        bot.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    try:
        logger.info("Starting polling loop...")

        # Run polling (blocking call)
        bot.run_polling()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
        bot.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        logger.exception("Traceback:")
        raise
    finally:
        logger.info("Telegram poller stopped")


if __name__ == '__main__':
    main()

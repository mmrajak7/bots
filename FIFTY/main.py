#!/usr/bin/env python3
"""
FIFTY Bot - Monthly CSP Trading Bot with Telegram Approval Workflow

Entry point for the bot. Designed to run via cron every 5 minutes.

Usage:
    python main.py              # Normal run
    python main.py --init       # Initialize database only
    python main.py --test       # Test Telegram connection
"""

import os
import sys
import argparse
import atexit
from datetime import datetime
from pathlib import Path

# Set working directory to bot root
os.chdir(Path(__file__).parent)
sys.path.insert(0, str(Path(__file__).parent))

# Ensure logs directory exists BEFORE anything else (fixes cron redirection issue)
Path("logs").mkdir(exist_ok=True)

from loguru import logger


# ============================================================================
# CONCURRENT RUN LOCK (SYS-H4 fix)
# ============================================================================

LOCK_FILE = Path(__file__).parent / "data" / ".fifty_lock"


class ProcessLock:
    """File-based lock to prevent concurrent cron runs"""

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self.locked = False

    def acquire(self) -> bool:
        """
        Acquire the lock.
        Returns True if lock acquired, False if another process holds it.
        """
        try:
            # Create data directory if needed
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)

            if self.lock_path.exists():
                # Check if the process is still running
                try:
                    with open(self.lock_path, 'r') as f:
                        pid = int(f.read().strip())

                    # Check if process exists (works on Windows and Unix)
                    if self._is_process_running(pid):
                        logger.warning(f"Another instance running (PID {pid}), skipping")
                        return False
                    else:
                        # Stale lock file, remove it
                        logger.info(f"Removing stale lock (PID {pid} not running)")
                        self.lock_path.unlink()
                except (ValueError, OSError) as e:
                    logger.warning(f"Error reading lock file: {e}, removing")
                    self.lock_path.unlink()

            # Create lock file with our PID
            with open(self.lock_path, 'w') as f:
                f.write(str(os.getpid()))

            self.locked = True
            return True

        except Exception as e:
            logger.error(f"Failed to acquire lock: {e}")
            return False

    def release(self):
        """Release the lock"""
        if self.locked and self.lock_path.exists():
            try:
                self.lock_path.unlink()
                self.locked = False
            except Exception as e:
                logger.warning(f"Failed to release lock: {e}")

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running"""
        try:
            if sys.platform == 'win32':
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                os.kill(pid, 0)
                return True
        except (OSError, PermissionError):
            return False

    def __enter__(self):
        return self.acquire()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


# Global lock instance
_process_lock = ProcessLock(LOCK_FILE)


def setup_logging():
    """Configure logging"""
    from src.utils.config_manager import config

    log_dir = config.get('logging.log_dir', 'logs')
    os.makedirs(log_dir, exist_ok=True)

    log_level = config.get('logging.level', 'INFO')
    log_format = config.get(
        'logging.format',
        '{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}'
    )

    # Remove default handler
    logger.remove()

    # Add console handler
    logger.add(
        sys.stderr,
        format=log_format,
        level=log_level,
        colorize=True
    )

    # Add file handler
    log_file = f"{log_dir}/fifty_{datetime.now().strftime('%Y-%m-%d')}.log"
    logger.add(
        log_file,
        format=log_format,
        level=log_level,
        rotation="1 day",
        retention="45 days",
        compression="gz"
    )

    logger.info("Logging configured")


def init_database():
    """Initialize database tables"""
    from src.models.database import init_database as db_init
    logger.info("Initializing database...")
    db_init()
    logger.info("Database initialized successfully")


def test_telegram():
    """Test Telegram connection"""
    from src.telegram.bot import telegram

    logger.info("Testing Telegram connection...")

    if not telegram.enabled:
        logger.error("Telegram is not configured. Check config.yaml for bot_token and chat_id")
        return False

    result = telegram.send_alert("FIFTY Bot: Test message - connection OK")

    if result:
        logger.info("Telegram test successful")
        return True
    else:
        logger.error("Telegram test failed")
        return False


def test_kite():
    """Test Kite API connections"""
    from src.api.dual_kite_client import get_kite_client

    logger.info("Testing Kite API connections...")

    try:
        kite = get_kite_client()

        # Test read connection
        logger.info("Testing read connection...")
        if kite.validate_read_connection():
            logger.info("Read connection: OK")
        else:
            logger.error("Read connection: FAILED")
            return False

        # Test trade connection
        logger.info("Testing trade connection...")
        if kite.validate_trade_connection():
            logger.info("Trade connection: OK")
        else:
            logger.warning("Trade connection: FAILED (may need token generation)")

        return True

    except Exception as e:
        logger.error(f"Kite API test failed: {e}")
        return False


def run_bot():
    """Run the bot orchestrator"""
    from src.core.orchestrator import orchestrator

    logger.info("=" * 50)
    logger.info("FIFTY Bot Starting")
    logger.info("=" * 50)

    try:
        orchestrator.run()
    except Exception as e:
        logger.error(f"Bot run failed: {e}")
        raise


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='FIFTY Trading Bot')
    parser.add_argument('--init', action='store_true', help='Initialize database only')
    parser.add_argument('--test', action='store_true', help='Test Telegram connection')
    parser.add_argument('--test-kite', action='store_true', help='Test Kite API connections')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')
    parser.add_argument('--force', action='store_true', help='Force run even if another instance exists')

    args = parser.parse_args()

    # Setup logging first
    setup_logging()

    if args.debug:
        logger.remove()
        logger.add(sys.stderr, level="DEBUG")

    # Initialize database (always needed)
    init_database()

    if args.init:
        logger.info("Database initialized. Exiting.")
        return 0

    if args.test:
        return 0 if test_telegram() else 1

    if args.test_kite:
        return 0 if test_kite() else 1

    # Normal run - acquire lock (SYS-H4 fix)
    if not args.force:
        if not _process_lock.acquire():
            logger.warning("Another instance is running. Use --force to override.")
            return 2  # Special exit code for lock conflict

        # FIX SYS-H5: Only use atexit for cleanup (covers all exit paths including signals)
        # Removed duplicate release from finally block to avoid double-release attempts
        atexit.register(_process_lock.release)

    try:
        run_bot()
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1
    # FIX SYS-H5: Removed finally block with release() - atexit handles all cases


if __name__ == '__main__':
    sys.exit(main())

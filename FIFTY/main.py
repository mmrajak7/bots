#!/usr/bin/env python3
"""
FIFTY Bot - Monthly CSP Trading Bot with Telegram Approval Workflow

Entry point for the bot. Supports both cron mode and daemon mode.

Usage:
    python main.py              # Normal run (cron mode - single execution)
    python main.py --daemon     # Daemon mode (24/7 long-polling service)
    python main.py --init       # Initialize database only
    python main.py --test       # Test Telegram connection
"""

import os
import sys
import argparse
import atexit
import signal
import time
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
    """Run the bot orchestrator (single execution for cron mode)"""
    from src.core.orchestrator import orchestrator

    logger.info("=" * 50)
    logger.info("FIFTY Bot Starting (Cron Mode)")
    logger.info("=" * 50)

    try:
        orchestrator.run()
    except Exception as e:
        logger.error(f"Bot run failed: {e}")
        raise


# ============================================================================
# DAEMON MODE - 24/7 Long-Polling Service
# ============================================================================

_daemon_running = True


def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global _daemon_running
    logger.info(f"Received signal {signum}, shutting down daemon...")
    _daemon_running = False


def run_daemon():
    """
    Run the bot in daemon mode - 24/7 long-polling service.

    - Telegram commands processed instantly (30s long-polling)
    - Trading tasks run at scheduled time windows
    - Graceful shutdown on SIGINT/SIGTERM
    """
    global _daemon_running

    from src.core.orchestrator import orchestrator
    from src.telegram.approval_handler import approval_handler
    from src.telegram.commands import command_handler
    from src.telegram.bot import telegram
    from src.utils.timezone_helper import now_ist, in_time_window, is_market_day_ist
    from src.models.database import is_kill_switch_active

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("=" * 50)
    logger.info("FIFTY Bot Starting (DAEMON MODE)")
    logger.info("24/7 Telegram service with instant response")
    logger.info("=" * 50)

    telegram.send_alert("FIFTY Bot Daemon Started (24/7 mode)")

    # Track last scheduled task run
    last_scheduled_run = None
    SCHEDULED_INTERVAL = 300  # Run scheduled tasks every 5 minutes

    try:
        while _daemon_running:
            current = now_ist()

            # 1. Process Telegram updates with long-polling (instant response)
            # This blocks for up to 30 seconds waiting for updates
            try:
                actions = approval_handler.process_updates_long_poll(timeout=30)

                for action in actions:
                    action_type = action.get('type')

                    if action_type == 'signal_approved':
                        _handle_signal_approved_daemon(action, orchestrator)

                    elif action_type == 'emergency_exit':
                        _handle_emergency_exit_daemon(action, orchestrator)

                    elif action_type == 'command':
                        command = action.get('command')
                        # Handle commands with parameters
                        if command == 'import':
                            command_handler.execute_command(command, script=action.get('script'))
                        elif command == 'report':
                            command_handler.execute_command(command, report_type=action.get('report_type'))
                        else:
                            command_handler.execute_command(command)

            except Exception as e:
                logger.error(f"Error processing Telegram updates: {e}")
                time.sleep(5)  # Back off on error

            # 2. Run scheduled tasks every 5 minutes (similar to cron)
            now_ts = time.time()
            if last_scheduled_run is None or (now_ts - last_scheduled_run) >= SCHEDULED_INTERVAL:
                try:
                    _run_scheduled_tasks(orchestrator, current)
                    last_scheduled_run = now_ts
                except Exception as e:
                    logger.error(f"Error running scheduled tasks: {e}")

    except Exception as e:
        logger.error(f"Daemon error: {e}")
        telegram.send_alert(f"FIFTY Daemon Error: {str(e)}", critical=True)
        raise
    finally:
        logger.info("Daemon shutting down...")
        telegram.send_alert("FIFTY Bot Daemon Stopped")


def _handle_signal_approved_daemon(action: dict, orchestrator) -> None:
    """Handle approved signal in daemon mode"""
    from src.telegram.bot import telegram
    from src.models.database import is_kill_switch_active

    # FIX SAFE-3: Check kill switch before processing
    if is_kill_switch_active():
        logger.warning("Kill switch active - ignoring signal approval")
        telegram.send_alert("Kill switch is ACTIVE - signal approval ignored")
        return

    signal_id = action.get('signal_id')
    script = action.get('script')
    entry_price = action.get('entry_price')

    logger.info(f"Processing approved signal: {script} @ {entry_price}")

    try:
        orchestrator._lazy_load_processors()
        orchestrator.order_manager.place_entry_gtt(signal_id, script, entry_price)
    except Exception as e:
        logger.error(f"Failed to place entry order for {script}: {e}")
        telegram.send_alert(f"Failed to place entry for {script}: {str(e)}", critical=True)


def _handle_emergency_exit_daemon(action: dict, orchestrator) -> None:
    """Handle emergency exit in daemon mode - NOTE: Emergency exits bypass kill switch"""
    from src.telegram.bot import telegram

    # NOTE: Emergency exits should ALWAYS work even with kill switch active
    # This is intentional - user needs to be able to exit positions in emergency

    position_id = action.get('position_id')
    script = action.get('script')

    logger.warning(f"Processing emergency exit: {script}")

    try:
        orchestrator._lazy_load_processors()
        orchestrator.exit_manager.emergency_exit(position_id)
    except Exception as e:
        logger.error(f"Failed emergency exit for {script}: {e}")
        telegram.send_alert(f"Failed emergency exit for {script}: {str(e)}", critical=True)


def _run_scheduled_tasks(orchestrator, current) -> None:
    """
    Run scheduled tasks (same as cron mode but within daemon).
    Only runs tasks appropriate for current time window.
    """
    from src.utils.timezone_helper import in_time_window, is_market_hours, is_friday, is_market_day_ist
    from src.models.database import is_kill_switch_active, get_bot_state, set_bot_state
    from src.utils.timezone_helper import today_ist

    # Skip if kill switch active
    if is_kill_switch_active():
        return

    # Skip if not market day (for trading tasks, not commands)
    is_market = is_market_day_ist()

    orchestrator._lazy_load_processors()

    # Early token generation (8:50-9:00)
    if is_market and in_time_window(current, "08:50", "09:00"):
        orchestrator._ensure_token_ready()

    # Morning startup (9:00-9:05)
    if is_market and in_time_window(current, "09:00", "09:05"):
        orchestrator._morning_startup()

    # Signal processing during market hours
    if is_market and is_market_hours(current):
        orchestrator._process_signals()
        orchestrator._send_pending_notifications()
        orchestrator._monitor_orders()
        orchestrator._monitor_positions_for_drops()

    # Hold signal re-notification (9:30-9:35)
    if is_market and in_time_window(current, "09:30", "09:35"):
        orchestrator._resend_hold_signals()

    # EOD GTT Update (last trading day, 15:50-15:55)
    if is_market and in_time_window(current, "15:50", "15:55") and orchestrator._is_last_trading_day():
        orchestrator._update_monthly_trailing_sl()

    # Recovery checks (16:00-16:05)
    if is_market and in_time_window(current, "16:00", "16:05"):
        orchestrator._run_recovery_checks()

    # Weekly report (Friday 16:15-16:20)
    if is_market and is_friday() and in_time_window(current, "16:15", "16:20"):
        orchestrator._send_weekly_report()

    # Monthly report (last trading day, 16:20-16:25)
    if is_market and in_time_window(current, "16:20", "16:25") and orchestrator._is_last_trading_day():
        orchestrator._send_monthly_report()

    # Month-end cleanup (last trading day, 16:25-16:30)
    if is_market and in_time_window(current, "16:25", "16:30") and orchestrator._is_last_trading_day():
        orchestrator._month_end_cleanup()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='FIFTY Trading Bot')
    parser.add_argument('--init', action='store_true', help='Initialize database only')
    parser.add_argument('--test', action='store_true', help='Test Telegram connection')
    parser.add_argument('--test-kite', action='store_true', help='Test Kite API connections')
    parser.add_argument('--daemon', action='store_true', help='Run in daemon mode (24/7 long-polling)')
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

    # Normal/Daemon run - acquire lock (SYS-H4 fix)
    if not args.force:
        if not _process_lock.acquire():
            logger.warning("Another instance is running. Use --force to override.")
            return 2  # Special exit code for lock conflict

        # FIX SYS-H5: Only use atexit for cleanup (covers all exit paths including signals)
        atexit.register(_process_lock.release)

    try:
        if args.daemon:
            run_daemon()
        else:
            run_bot()
        return 0
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        return 1


if __name__ == '__main__':
    sys.exit(main())

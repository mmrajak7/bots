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
import queue
import threading
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
# FIX OP-004: Track in-progress API calls for graceful shutdown
_shutdown_in_progress = False
_active_api_operations = 0
_api_operation_lock = None  # Initialized in run_daemon


_action_queue: queue.Queue = queue.Queue()
_telegram_thread: threading.Thread | None = None


def _signal_handler(signum, frame):
    """Handle shutdown signals gracefully"""
    global _daemon_running, _shutdown_in_progress
    logger.info(f"Received signal {signum}, initiating graceful shutdown...")
    _shutdown_in_progress = True
    _daemon_running = False


def _write_heartbeat():
    """Write heartbeat file for watchdog monitoring"""
    try:
        heartbeat_file = Path(__file__).parent / "data" / ".heartbeat"
        heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
        heartbeat_file.write_text(str(time.time()))
    except Exception:
        pass  # Don't crash on heartbeat failure


def _send_daemon_startup_message():
    """Send professional service startup message"""
    from src.telegram.bot import telegram
    from src.models.database import get_session, OpenPosition, OpenOrder, ClosedPosition, PositionStatus, OrderStatus
    from src.utils.timezone_helper import today_ist
    from src.utils.config_manager import config

    try:
        session = get_session()
        try:
            today = today_ist()
            day_name = today.strftime('%A')
            date_str = today.strftime('%d %b %Y')

            # Get capital info
            initial_capital = config.get('trading.initial_capital', 100000)

            # Get open positions
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            position_scripts = {p.script for p in open_positions}
            num_positions = len(open_positions)
            deployed_capital = sum(p.capital_deployed for p in open_positions)
            available_capital = initial_capital - deployed_capital

            # Get pending GTT entry orders (exclude scripts with positions - stale entries)
            all_orders = session.query(OpenOrder).filter(
                OpenOrder.status == OrderStatus.PENDING
            ).all()
            pending_orders = len([o for o in all_orders if o.script not in position_scripts])

            # Get realized P&L
            all_trades = session.query(ClosedPosition).all()
            realized_pnl = sum(t.net_pnl for t in all_trades)
            total_return_pct = (realized_pnl / initial_capital * 100) if initial_capital > 0 else 0

            # Build compact message like CROCODILE
            pnl_sign = "+" if realized_pnl >= 0 else ""

            message = (
                f"🌅 <b>FIFTY Service Started</b>\n\n"
                f"📅 {date_str} ({day_name})\n"
                f"💰 Rs.{available_capital:,.0f} / {initial_capital:,.0f}\n"
                f"📊 {num_positions} positions | {pending_orders} GTT pending\n"
                f"📈 P&L: {pnl_sign}{realized_pnl:,.0f} ({pnl_sign}{total_return_pct:.2f}%)\n"
                f"✅ Ready"
            )

            telegram.send_alert(message)
            logger.info("Service startup message sent")

        finally:
            session.close()

    except Exception as e:
        logger.warning(f"Could not send service startup message: {e}")
        telegram.send_alert("🌅 <b>FIFTY Service Started</b>")


def _telegram_poll_loop():
    """
    Background thread: continuously long-polls Telegram for updates.

    Parsed actions are placed on _action_queue for the main thread to process.
    This thread performs NO business logic — only polling and queue insertion.
    """
    from src.telegram.approval_handler import approval_handler

    logger.info("Telegram poll thread started")
    consecutive_errors = 0
    MAX_BACKOFF = 60  # Cap backoff at 60 seconds

    while _daemon_running:
        try:
            actions = approval_handler.process_updates_long_poll(timeout=30)
            consecutive_errors = 0  # Reset on success

            for action in actions:
                _action_queue.put(action)

        except Exception as e:
            consecutive_errors += 1
            backoff = min(5 * consecutive_errors, MAX_BACKOFF)
            logger.error(f"Telegram poll error ({consecutive_errors}): {e}, backing off {backoff}s")
            # Sleep in 1s increments so we can exit quickly on shutdown
            for _ in range(backoff):
                if not _daemon_running:
                    break
                time.sleep(1)

    logger.info("Telegram poll thread stopped")


def _start_telegram_thread() -> threading.Thread:
    """Start (or restart) the Telegram polling background thread."""
    t = threading.Thread(target=_telegram_poll_loop, name="telegram-poll", daemon=True)
    t.start()
    return t


def _drain_action_queue(orchestrator, command_handler) -> None:
    """
    Process all pending actions from the Telegram poll thread.

    Runs in the main thread to avoid thread-safety issues with
    orchestrator / order_manager / exit_manager.
    """
    while True:
        try:
            action = _action_queue.get_nowait()
        except queue.Empty:
            break

        action_type = action.get('type')

        try:
            if action_type == 'signal_approved':
                _handle_signal_approved_daemon(action, orchestrator)

            elif action_type == 'emergency_exit':
                _handle_emergency_exit_daemon(action, orchestrator)

            elif action_type == 'command':
                command = action.get('command')
                if command in ('import', 'release', 'fix'):
                    command_handler.execute_command(command, script=action.get('script'))
                elif command == 'report':
                    command_handler.execute_command(command, report_type=action.get('report_type'))
                else:
                    command_handler.execute_command(command)
        except Exception as e:
            logger.error(f"Error processing queued action {action_type}: {e}")


def run_daemon():
    """
    Run the bot in daemon mode - 24/7 long-polling service.

    Architecture:
      - Background thread: Telegram long-polling (always responsive)
      - Main thread: scheduled tasks + drains action queue
      - Communication: thread-safe queue.Queue

    Telegram commands are processed instantly even while scheduled tasks
    (signal processing, order monitoring) are running.
    """
    global _daemon_running, _telegram_thread

    from src.core.orchestrator import orchestrator
    from src.telegram.commands import command_handler
    from src.telegram.bot import telegram
    from src.utils.timezone_helper import now_ist

    # Setup signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    logger.info("=" * 50)
    logger.info("FIFTY Bot Starting (DAEMON MODE)")
    logger.info("24/7 Telegram service with instant response")
    logger.info("=" * 50)

    # Note: Startup message is sent by morning_startup at 09:00 after token validation
    # No separate daemon startup message to avoid duplicates

    # Start Telegram polling in background thread
    _telegram_thread = _start_telegram_thread()
    logger.info("Telegram background thread launched")

    # Track last scheduled task run
    last_scheduled_run = None
    SCHEDULED_INTERVAL = 300  # Run scheduled tasks every 5 minutes

    try:
        while _daemon_running:
            current = now_ist()

            # 1. Drain queued Telegram actions (non-blocking)
            _drain_action_queue(orchestrator, command_handler)

            # Write heartbeat for watchdog monitoring
            _write_heartbeat()

            # 2. Run scheduled tasks every 5 minutes (similar to cron)
            now_ts = time.time()
            if last_scheduled_run is None or (now_ts - last_scheduled_run) >= SCHEDULED_INTERVAL:
                try:
                    _run_scheduled_tasks(orchestrator, current)
                    last_scheduled_run = now_ts
                except Exception as e:
                    logger.error(f"Error running scheduled tasks: {e}")

            # 3. Monitor telegram thread health - restart if crashed
            if _telegram_thread is not None and not _telegram_thread.is_alive():
                logger.warning("Telegram thread died - restarting")
                _telegram_thread = _start_telegram_thread()

            # 4. Sleep in 1s increments, draining queue between sleeps
            #    This keeps action processing latency under ~1s
            for _ in range(5):
                if not _daemon_running:
                    break
                time.sleep(1)
                _drain_action_queue(orchestrator, command_handler)

    except Exception as e:
        logger.error(f"Daemon error: {e}")
        telegram.send_alert(f"FIFTY Daemon Error: {str(e)}", critical=True)
        raise
    finally:
        logger.info("Daemon shutting down...")
        # Wait for telegram thread to finish (it checks _daemon_running)
        if _telegram_thread is not None and _telegram_thread.is_alive():
            _telegram_thread.join(timeout=35)  # Slightly longer than poll timeout
        from src.utils.config_manager import config as _cfg
        if _cfg.get('telegram.daemon_lifecycle_alerts', False):
            telegram.send_alert("FIFTY Bot Daemon Stopped")
        else:
            logger.info("Daemon stop message suppressed (telegram.daemon_lifecycle_alerts=false)")


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

    FIX OPS-H1: Each task is wrapped in try/catch to prevent one failure
    from blocking subsequent tasks.
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

    def _safe_run(task_name: str, task_func):
        """Run a task with error isolation"""
        try:
            task_func()
        except Exception as e:
            logger.error(f"Scheduled task '{task_name}' failed: {e}")
            # Don't crash daemon - just log and continue

    # Early token generation (8:50-9:00)
    if is_market and in_time_window(current, "08:50", "09:00"):
        _safe_run("token_generation", orchestrator._ensure_token_ready)

    # Morning startup (9:00-9:05)
    if is_market and in_time_window(current, "09:00", "09:05"):
        _safe_run("morning_startup", orchestrator._morning_startup)

    # Regime upkeep: breadth capture / downtime backfill / state machine.
    # Internally throttled and time-budgeted; runs on every cycle from market
    # open onward. (Was previously only wired into orchestrator.run(), the
    # cron-mode path, which the daemon's process lock makes unreachable - so
    # breadth never ran at all.)
    #
    # Starts at 09:15, NOT at 00:00: is_market is only a DATE check, and the
    # shared read token expires 06:00 with regen at ~08:50. Running before then
    # would fail every call for ~30 cycles and could leave the SHARED historical
    # circuit breaker open right when morning startup needs it. The window still
    # covers post-close hours, which is when a multi-session backfill heals.
    if is_market and in_time_window(current, "09:15", "23:59"):
        _safe_run("regime", orchestrator._update_regime)

    # Signal processing during market hours
    if is_market and is_market_hours(current):
        _safe_run("process_signals", orchestrator._process_signals)
        _safe_run("send_notifications", orchestrator._send_pending_notifications)
        _safe_run("monitor_orders", orchestrator._monitor_orders)
        # Same dead-cron-path bug as the regime block above: _monitor_sl_hits was
        # only ever called from orchestrator.run(), which the daemon's process
        # lock makes unreachable - so intraday SL-hit detection never ran. The
        # position still exited (the GTT fires broker-side regardless); what was
        # missing was the bot NOTICING, so the slot stayed marked OPEN and the
        # exit alert waited until _reconcile_positions() at 16:00.
        _safe_run("monitor_sl_hits", orchestrator._monitor_sl_hits)
        _safe_run("monitor_drops", orchestrator._monitor_positions_for_drops)

    # Hold signal re-notification (9:30-9:35)
    if is_market and in_time_window(current, "09:30", "09:35"):
        _safe_run("resend_holds", orchestrator._resend_hold_signals)

    # EOD GTT Update (last trading day, 15:50-15:55)
    if is_market and in_time_window(current, "15:50", "15:55") and orchestrator._is_last_trading_day():
        _safe_run("monthly_sl_trail", orchestrator._update_monthly_trailing_sl)

    # Recovery checks (16:00-16:05)
    if is_market and in_time_window(current, "16:00", "16:05"):
        _safe_run("recovery_checks", orchestrator._run_recovery_checks)

    # FIX ER-001: Immediate recovery for late-day positions (15:30-16:00)
    # Positions opened after 15:00 need immediate recovery check, not waiting until next day
    if is_market and in_time_window(current, "15:30", "16:00"):
        _safe_run("late_day_recovery", orchestrator._run_recovery_checks)

    # Weekly report (Friday 16:15-16:20)
    if is_market and is_friday() and in_time_window(current, "16:15", "16:20"):
        _safe_run("weekly_report", orchestrator._send_weekly_report)

    # Monthly report (last trading day, 16:20-16:25)
    if is_market and in_time_window(current, "16:20", "16:25") and orchestrator._is_last_trading_day():
        _safe_run("monthly_report", orchestrator._send_monthly_report)

    # Month-end cleanup (last trading day, 16:25-16:30)
    if is_market and in_time_window(current, "16:25", "16:30") and orchestrator._is_last_trading_day():
        _safe_run("month_cleanup", orchestrator._month_end_cleanup)


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

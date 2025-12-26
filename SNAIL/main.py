#!/usr/bin/env python3
"""
SNAIL - Systematic NIFTY Automated Iron-fly Leverager

Main entry point for the SNAIL trading system.

@file        main.py
@description Main application entry point
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
"""

import sys
import os
import argparse
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from functools import wraps

# Platform-specific file locking
if os.name == 'nt':  # Windows
    import msvcrt
else:  # Unix/Linux/Mac
    import fcntl

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from loguru import logger

from src.utils.config import load_config, validate_config, PROJECT_ROOT
from src.utils.helpers import setup_logging, is_trading_day, is_market_open
from src.utils.db import init_database


# =============================================================================
# FILE LOCKING (ISSUE-013: Prevent concurrent cron job execution)
# =============================================================================

LOCK_FILE = PROJECT_ROOT / "data" / ".snail.lock"


@contextmanager
def file_lock(lock_path: Path = LOCK_FILE, timeout: float = 0):
    """
    Context manager for file-based locking to prevent concurrent execution.

    Cross-platform: Uses fcntl on Unix/Linux, msvcrt on Windows.

    Args:
        lock_path: Path to lock file
        timeout: Timeout in seconds (0 = non-blocking, fail immediately)

    Yields:
        Lock file handle

    Raises:
        BlockingIOError: If lock cannot be acquired (another process has it)
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = open(lock_path, 'w')
    try:
        if os.name == 'nt':  # Windows
            # Windows file locking using msvcrt
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:  # Unix/Linux/Mac
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        lock_file.write(f"PID: {os.getpid()}\nTime: {datetime.now().isoformat()}\n")
        lock_file.flush()
        yield lock_file
    finally:
        if os.name == 'nt':  # Windows
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass  # Already unlocked or file issue
        else:  # Unix/Linux/Mac
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def with_file_lock(func):
    """
    Decorator to ensure function runs with exclusive file lock.
    Prevents concurrent execution of entry/exit/monitor commands.
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            with file_lock():
                return func(*args, **kwargs)
        except BlockingIOError:
            logger.warning(f"Could not acquire lock - another SNAIL process is running")
            print("⚠️ Another SNAIL process is running. Skipping to prevent conflicts.")
            return 0
    return wrapper


# =============================================================================
# CONSTANTS
# =============================================================================

VERSION = "1.0.0"
BANNER = """
+---------------------------------------------------------------+
|                                                               |
|   SNAIL - Systematic NIFTY Automated Iron-fly Leverager      |
|                                                               |
|   Version: {version:<52}|
|                                                               |
+---------------------------------------------------------------+
""".format(version=VERSION)


# =============================================================================
# COMMAND HANDLERS
# =============================================================================

def cmd_run(args):
    """Run the main trading loop."""
    from src.workflows.daily_startup import DailyStartup
    from src.workflows.monitor_workflow import MonitorWorkflow

    logger.info("Starting SNAIL trading system...")

    # Run startup
    startup = DailyStartup()
    result = startup.run()

    if not result.success:
        logger.error("Startup failed! Check configuration and try again.")
        for check in result.checks:
            if not check.passed and check.critical:
                logger.error(f"  - {check.name}: {check.message}")
        return 1

    # Send morning summary
    startup.send_morning_summary(result)

    logger.info("Startup complete, beginning monitoring...")

    # Start monitor workflow
    monitor = MonitorWorkflow()

    try:
        monitor.run()
    except KeyboardInterrupt:
        logger.info("Received interrupt, shutting down...")
        monitor.stop()

    # Send daily summary on exit
    from src.workflows.daily_summary import DailySummary
    summary = DailySummary()
    summary.send_daily_summary()

    return 0


def cmd_startup(args):
    """Run daily startup checks only."""
    from src.workflows.daily_startup import run_daily_startup

    result = run_daily_startup()

    print("\n[STARTUP CHECKS]:")
    for check in result.checks:
        status = "[OK]" if check.passed else ("[FAIL]" if check.critical else "[WARN]")
        print(f"   {status} {check.name}: {check.message}")

    if result.warnings:
        print("\n[WARNINGS]:")
        for warning in result.warnings:
            print(f"   - {warning}")

    print(f"\n{'='*60}")
    print(f"Ready to trade: {result.ready_to_trade}")

    return 0 if result.success else 1


def cmd_entry(args):
    """Check entry conditions or execute entry."""
    from src.services.entry_manager import get_entry_manager
    from src.services.claude_advisor import get_claude_advisor
    from src.utils.config import get_trading_config
    from src.utils.market_events_scraper import scrape_and_save_all

    # IMPORTANT: Check time window BEFORE acquiring lock (ISSUE-FIX: lock contention)
    # This prevents entry from blocking on lock when outside entry window
    config = get_trading_config()
    entry_start = config.get('entry', {}).get('window', {}).get('start', '09:30')
    entry_end = config.get('entry', {}).get('window', {}).get('end', '15:10')
    current_time = datetime.now().strftime('%H:%M')

    if current_time < entry_start or current_time > entry_end:
        logger.debug(f"Outside entry window ({entry_start}-{entry_end}), skipping")
        return 0

    # Now acquire lock for actual entry operations
    logger.info(f"Entry attempt at {current_time} - acquiring lock...")
    return _cmd_entry_with_lock(args, config)


@with_file_lock
def _cmd_entry_with_lock(args, config):
    """Entry logic that requires exclusive lock."""
    from src.services.entry_manager import get_entry_manager
    from src.services.claude_advisor import get_claude_advisor
    from src.utils.market_events_scraper import scrape_and_save_all

    # Scrape latest news and events first
    try:
        events_count, news_count = scrape_and_save_all()
        logger.info(f"Scraped {events_count} events, {news_count} news items")
    except Exception as e:
        logger.warning(f"News scraping failed (non-critical): {e}")

    manager = get_entry_manager()
    conditions = manager.check_entry_conditions()

    print("\n[ENTRY CONDITIONS]:")
    print(f"   Can enter: {conditions.can_enter}")
    print(f"   Reason: {conditions.reason}")

    if conditions.can_enter:
        print(f"\n   NIFTY Forward: ₹{conditions.nifty_forward:,.0f}")
        print(f"   NIFTY Spot: ₹{conditions.nifty_spot:,.0f}")
        if conditions.nifty_futures > 0:
            print(f"   NIFTY Futures: ₹{conditions.nifty_futures:,.0f}")
        print(f"   VIX: {conditions.india_vix:.2f}")
        print(f"   ATM Strike: {conditions.atm_strike}")
        print(f"   Wing Distance: {conditions.wing_distance}")
        print(f"   Expiry: {conditions.expiry} (DTE: {conditions.dte})")

        if args.execute:
            # Use existing pre-entry advisory which:
            # 1. Fetches real option quotes
            # 2. Checks R:R filter
            # 3. Sends Telegram with quotes + events + news
            # 4. Waits for user response (ENTER/SKIP)
            advisor = get_claude_advisor()
            advisory_result = advisor.get_pre_entry_advisory(
                nifty_spot=conditions.nifty_spot,
                india_vix=conditions.india_vix,
                atm_strike=conditions.atm_strike,
                dte=conditions.dte,
                expiry_date=conditions.expiry,
                nifty_forward=conditions.nifty_forward
            )

            if advisory_result.action_required:
                # User said SKIP, timeout, or R:R filter not met
                print(f"\n⏭️ Entry skipped: {advisory_result.suggested_action}")
                return 0

            # User said ENTER - refresh conditions before executing (ISSUE-002)
            # User may have taken 1-5 minutes to respond, market may have moved
            print("\n🔄 Refreshing conditions before entry...")
            fresh_conditions = manager.check_entry_conditions()

            if not fresh_conditions.can_enter:
                print(f"⚠️ Conditions changed during wait: {fresh_conditions.reason}")
                logger.warning(f"Entry aborted - conditions changed: {fresh_conditions.reason}")
                return 0

            # Check if ATM strike changed significantly
            if fresh_conditions.atm_strike != conditions.atm_strike:
                print(f"⚠️ ATM strike changed: {conditions.atm_strike} → {fresh_conditions.atm_strike}")
                logger.warning(f"ATM strike changed during wait, proceeding with new strike")

            # Use fresh conditions for entry
            print(f"\n🚀 Executing entry with fresh conditions...")
            print(f"   ATM: {fresh_conditions.atm_strike}, Wing: {fresh_conditions.wing_distance}")

            result = manager.execute_entry(
                conditions=fresh_conditions,
                require_claude_approval=False
            )

            if result.success:
                print(f"[OK] Entry successful! Position ID: {result.position_id}")
            else:
                print(f"[FAIL] Entry failed: {result.error}")
                return 1

    return 0


@with_file_lock
def cmd_exit(args):
    """Execute position exit."""
    from src.services.exit_manager import get_exit_manager, ExitReason
    from src.utils.db import get_active_position

    position = get_active_position()

    if not position:
        print("No active position to exit.")
        return 0

    print(f"\n[ACTIVE POSITION]: #{position.id}")
    print(f"   Status: {position.status}")
    print(f"   Entry premium: Rs.{position.entry_premium:,.2f}")

    if args.force or input("\nConfirm exit? (y/N): ").lower() == 'y':
        manager = get_exit_manager()
        result = manager.execute_exit(reason=ExitReason.MANUAL, position=position)

        if result.success:
            print(f"[OK] Exit successful! Realized P&L: Rs.{result.realized_pnl:,.2f}")
        else:
            print(f"[FAIL] Exit failed: {result.error}")
            return 1

    return 0


def cmd_status(args):
    """Show current system status."""
    from src.utils.db import get_active_position, get_position_legs
    from src.api.kite_client import get_kite_client
    from src.utils.calculations import calculate_position_pnl

    print("\n" + "=" * 60)
    print("SNAIL System Status")
    print("=" * 60)

    # Market status
    print(f"\n[MARKET]:")
    print(f"   Trading day: {is_trading_day()}")
    print(f"   Market open: {is_market_open()}")

    try:
        kite = get_kite_client()
        kite.ensure_authenticated()

        nifty = kite.get_nifty_spot()
        vix = kite.get_india_vix()
        margin = kite.get_available_margin()

        print(f"   NIFTY: Rs.{nifty:,.2f}")
        print(f"   VIX: {vix:.2f}")
        print(f"   Margin: Rs.{margin:,.2f}")
    except Exception as e:
        print(f"   (Market data unavailable: {e})")

    # Position status
    position = get_active_position()

    print(f"\n[POSITION]:")
    if position:
        print(f"   ID: {position.id}")
        print(f"   Status: {position.status}")
        print(f"   ATM: {position.atm_strike}")
        print(f"   Wing distance: {position.wing_distance}")
        print(f"   Entry premium: Rs.{position.entry_premium:,.2f}")
        print(f"   Expiry: {position.expiry_date}")

        # Calculate current P&L
        try:
            legs = get_position_legs(position.id)
            # Would need to fetch quotes for actual P&L
            print(f"   Legs: {len(legs)}")
        except Exception as e:
            logger.debug(f"Could not fetch position legs: {e}")
    else:
        print("   No active position")

    print("\n" + "=" * 60)
    return 0


def cmd_summary(args):
    """Generate and show daily/weekly summary."""
    from src.workflows.daily_summary import DailySummary

    summary_gen = DailySummary()

    if args.weekly:
        summary = summary_gen.generate_weekly_summary()
        print(f"\n[WEEKLY SUMMARY] ({summary.week_start} to {summary.week_end})")
        print(f"   Total P&L: Rs.{summary.total_pnl:,.2f}")
        print(f"   Winning days: {summary.winning_days}")
        print(f"   Losing days: {summary.losing_days}")
        print(f"   Trades: {summary.trades_count}")

        if args.send:
            summary_gen.send_weekly_summary(summary)
            print("\n[OK] Weekly summary sent to Telegram")
    else:
        summary = summary_gen.generate_daily_summary()
        print(f"\n[DAILY SUMMARY] ({summary.date})")
        print(f"   Has position: {summary.has_position}")
        print(f"   Day P&L change: Rs.{summary.day_pnl_change:,.2f}")
        print(f"   Orders: {summary.orders_executed}")
        print(f"   Trades: {len(summary.trades_today)}")

        if args.send:
            summary_gen.send_daily_summary(summary)
            print("\n[OK] Daily summary sent to Telegram")

    return 0


@with_file_lock
def cmd_monitor(args):
    """Run a single monitoring iteration (for cron)."""
    from src.workflows.monitor_workflow import MonitorWorkflow
    from src.utils.config import get_monitoring_config
    from src.api.response_handler import CALLBACK_QUEUE_FILE
    from pathlib import Path

    # Check if there are pending user callbacks
    queue_file = Path(CALLBACK_QUEUE_FILE)
    has_pending_callbacks = queue_file.exists() and queue_file.stat().st_size > 10

    # Graceful exit if outside monitor window (for cron)
    # BUT always run if there are pending user callbacks to process
    config = get_monitoring_config()
    monitor_start = config.get('start_time', '09:16')
    monitor_end = config.get('end_time', '15:26')
    current_time = datetime.now().strftime('%H:%M')

    if current_time < monitor_start or current_time > monitor_end:
        if has_pending_callbacks:
            # Process pending callbacks even outside monitor window
            logger.info("Processing pending user callbacks outside monitor window")
            monitor = MonitorWorkflow()
            monitor._process_user_responses()
            return 0
        else:
            logger.debug(f"Outside monitor window ({monitor_start}-{monitor_end}), skipping")
            return 0

    monitor = MonitorWorkflow()
    monitor.run_once()

    return 0


def cmd_test(args):
    """Run system tests."""
    print("\n" + "=" * 60)
    print("SNAIL System Tests")
    print("=" * 60)

    tests_passed = 0
    tests_failed = 0

    # Test 1: Configuration
    print("\n[1] Testing configuration...")
    try:
        config = load_config()
        result = validate_config(config)
        if result['errors']:
            print(f"    [FAIL] Errors: {result['errors']}")
            tests_failed += 1
        else:
            print("    [OK] Configuration valid")
            tests_passed += 1
    except Exception as e:
        print(f"    [FAIL] Failed: {e}")
        tests_failed += 1

    # Test 2: Database
    print("\n[2] Testing database...")
    try:
        from src.utils.db import get_db_session
        with get_db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
        print("    [OK] Database connected")
        tests_passed += 1
    except Exception as e:
        print(f"    [FAIL] Failed: {e}")
        tests_failed += 1

    # Test 3: Kite Auth
    print("\n[3] Testing Kite authentication...")
    try:
        from src.api.kite_client import get_kite_client
        kite = get_kite_client()
        kite.ensure_authenticated()
        profile = kite.profile()
        print(f"    [OK] Authenticated as {profile.get('user_name', 'Unknown')}")
        tests_passed += 1
    except Exception as e:
        print(f"    [FAIL] Failed: {e}")
        tests_failed += 1

    # Test 4: Telegram
    print("\n[4] Testing Telegram...")
    try:
        from src.api.telegram_alerts import get_telegram
        telegram = get_telegram()
        if telegram.test_connection():
            print("    [OK] Telegram connected")
            tests_passed += 1
        else:
            print("    [WARN] Telegram test failed")
            tests_failed += 1
    except Exception as e:
        print(f"    [FAIL] Failed: {e}")
        tests_failed += 1

    # Test 5: Claude
    print("\n[5] Testing Claude API...")
    try:
        from src.api.claude_client import get_claude_client
        claude = get_claude_client()
        if claude.test_connection():
            print("    [OK] Claude API connected")
            tests_passed += 1
        else:
            print("    [WARN] Claude test failed")
            tests_failed += 1
    except Exception as e:
        print(f"    [FAIL] Failed: {e}")
        tests_failed += 1

    print("\n" + "=" * 60)
    print(f"Tests: {tests_passed} passed, {tests_failed} failed")
    print("=" * 60)

    return 0 if tests_failed == 0 else 1


def cmd_cooldown(args):
    """Manage cooldowns."""
    from src.utils.db import is_on_cooldown, clear_cooldown, get_cooldown_remaining

    if args.clear:
        # Clear cooldown
        cooldown_type = args.clear if args.clear != 'all' else None
        count = clear_cooldown(cooldown_type)
        if count > 0:
            print(f"[OK] Cleared {count} cooldown(s)")
        else:
            print("No active cooldowns to clear")
    else:
        # Show cooldown status
        print("\n[COOLDOWN STATUS]:")
        cooldown_types = ['entry', 'user_skip']
        any_active = False
        for ct in cooldown_types:
            remaining = get_cooldown_remaining(ct)
            if remaining is not None:
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                time_str = f"{hours}h {minutes}m" if hours > 0 else f"{minutes}m"
                print(f"   {ct}: ACTIVE ({time_str} remaining)")
                any_active = True
            else:
                print(f"   {ct}: inactive")

        if not any_active:
            print("\n   No active cooldowns")

    return 0


def cmd_init(args):
    """Initialize SNAIL system (database, directories)."""
    print("\n" + "=" * 60)
    print("SNAIL System Initialization")
    print("=" * 60)

    # Create directories
    print("\n[1] Creating directories...")
    dirs = ['data', 'logs', 'logs/claude', 'config/prompts']
    for d in dirs:
        path = PROJECT_ROOT / d
        path.mkdir(parents=True, exist_ok=True)
        print(f"    [OK] {d}/")

    # Initialize database
    print("\n[2] Initializing database...")
    try:
        init_database()
        print("    [OK] Database initialized")
    except Exception as e:
        print(f"    [FAIL] Failed: {e}")
        return 1

    # Check .env
    print("\n[3] Checking environment...")
    env_file = PROJECT_ROOT / '.env'
    if env_file.exists():
        print("    [OK] .env file found")
    else:
        print("    [WARN] .env file not found - create from .env.example")

    print("\n" + "=" * 60)
    print("Initialization complete!")
    print("=" * 60)

    return 0


# =============================================================================
# MAIN
# =============================================================================

def main():
    """Main entry point."""
    # Load environment (check multiple locations)
    env_paths = [
        PROJECT_ROOT / 'config' / 'creds.env',
        PROJECT_ROOT / '.env',
        PROJECT_ROOT / 'creds.env',
    ]
    for env_path in env_paths:
        if env_path.exists():
            load_dotenv(env_path)
            break

    # Parse arguments
    parser = argparse.ArgumentParser(
        description="SNAIL - Systematic NIFTY Automated Iron-fly Leverager",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Commands:
  run       Run the main trading loop
  startup   Run daily startup checks
  entry     Check/execute entry conditions
  exit      Exit active position
  status    Show system status
  summary   Generate trading summary
  test      Run system tests
  init      Initialize system
        """
    )

    parser.add_argument('-v', '--version', action='version', version=f'SNAIL {VERSION}')
    parser.add_argument('-q', '--quiet', action='store_true', help='Suppress banner')
    parser.add_argument('--debug', action='store_true', help='Enable debug logging')

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Run command
    run_parser = subparsers.add_parser('run', help='Run trading loop')

    # Startup command
    startup_parser = subparsers.add_parser('startup', help='Run startup checks')

    # Entry command
    entry_parser = subparsers.add_parser('entry', help='Check/execute entry')
    entry_parser.add_argument('--execute', action='store_true', help='Execute entry')
    entry_parser.add_argument('--skip-claude', action='store_true', help='Skip Claude approval')

    # Exit command
    exit_parser = subparsers.add_parser('exit', help='Exit position')
    exit_parser.add_argument('--force', action='store_true', help='Skip confirmation')

    # Status command
    status_parser = subparsers.add_parser('status', help='Show status')

    # Summary command
    summary_parser = subparsers.add_parser('summary', help='Generate summary')
    summary_parser.add_argument('--weekly', action='store_true', help='Weekly summary')
    summary_parser.add_argument('--send', action='store_true', help='Send to Telegram')

    # Monitor command (single iteration for cron)
    monitor_parser = subparsers.add_parser('monitor', help='Run single monitor iteration')

    # Test command
    test_parser = subparsers.add_parser('test', help='Run tests')

    # Init command
    init_parser = subparsers.add_parser('init', help='Initialize system')

    # Cooldown command
    cooldown_parser = subparsers.add_parser('cooldown', help='Manage cooldowns')
    cooldown_parser.add_argument('--clear', nargs='?', const='all', metavar='TYPE',
                                  help='Clear cooldown (all, entry, user_skip)')

    args = parser.parse_args()

    # Show banner
    if not args.quiet:
        print(BANNER)

    # Setup logging
    log_level = "DEBUG" if args.debug else "INFO"
    log_file = PROJECT_ROOT / "logs" / "snail.log"
    setup_logging(log_level=log_level, log_file=log_file)

    # Dispatch command
    commands = {
        'run': cmd_run,
        'startup': cmd_startup,
        'entry': cmd_entry,
        'exit': cmd_exit,
        'status': cmd_status,
        'summary': cmd_summary,
        'monitor': cmd_monitor,
        'test': cmd_test,
        'init': cmd_init,
        'cooldown': cmd_cooldown,
    }

    if args.command is None:
        parser.print_help()
        return 0

    handler = commands.get(args.command)
    if handler:
        return handler(args)
    else:
        parser.print_help()
        return 1


if __name__ == '__main__':
    sys.exit(main())

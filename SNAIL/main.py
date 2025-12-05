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

import os
import sys
import argparse
import time
import signal
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Optional

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from dotenv import load_dotenv
from loguru import logger

from src.utils.config import load_config, validate_config, PROJECT_ROOT
from src.utils.helpers import setup_logging, is_trading_day, is_market_open
from src.utils.db import init_database


# =============================================================================
# CONSTANTS
# =============================================================================

VERSION = "1.0.0"
BANNER = """
╔═══════════════════════════════════════════════════════════════╗
║                                                               ║
║   ███████╗███╗   ██╗ █████╗ ██╗██╗                           ║
║   ██╔════╝████╗  ██║██╔══██╗██║██║                           ║
║   ███████╗██╔██╗ ██║███████║██║██║                           ║
║   ╚════██║██║╚██╗██║██╔══██║██║██║                           ║
║   ███████║██║ ╚████║██║  ██║██║███████╗                      ║
║   ╚══════╝╚═╝  ╚═══╝╚═╝  ╚═╝╚═╝╚══════╝                      ║
║                                                               ║
║   Systematic NIFTY Automated Iron-fly Leverager              ║
║   Version: {version:<52}║
║                                                               ║
╚═══════════════════════════════════════════════════════════════╝
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

    print("\n📋 Startup Checks:")
    for check in result.checks:
        status = "✅" if check.passed else ("❌" if check.critical else "⚠️")
        print(f"   {status} {check.name}: {check.message}")

    if result.warnings:
        print("\n⚠️ Warnings:")
        for warning in result.warnings:
            print(f"   - {warning}")

    print(f"\n{'='*60}")
    print(f"Ready to trade: {result.ready_to_trade}")

    return 0 if result.success else 1


def cmd_entry(args):
    """Check entry conditions or execute entry."""
    from src.services.entry_manager import get_entry_manager

    manager = get_entry_manager()
    conditions = manager.check_entry_conditions()

    print("\n📊 Entry Conditions:")
    print(f"   Can enter: {conditions.can_enter}")
    print(f"   Reason: {conditions.reason}")

    if conditions.can_enter:
        print(f"\n   NIFTY: ₹{conditions.nifty_spot:,.2f}")
        print(f"   VIX: {conditions.india_vix:.2f}")
        print(f"   ATM Strike: {conditions.atm_strike}")
        print(f"   Expiry: {conditions.expiry} (DTE: {conditions.dte})")

        if args.execute:
            print("\n🚀 Executing entry...")
            result = manager.execute_entry(
                conditions=conditions,
                require_claude_approval=not args.skip_claude
            )

            if result.success:
                print(f"✅ Entry successful! Position ID: {result.position_id}")
            else:
                print(f"❌ Entry failed: {result.error}")
                return 1

    return 0


def cmd_exit(args):
    """Execute position exit."""
    from src.services.exit_manager import get_exit_manager, ExitReason
    from src.utils.db import get_active_position

    position = get_active_position()

    if not position:
        print("No active position to exit.")
        return 0

    print(f"\n📊 Active Position: #{position.id}")
    print(f"   Strategy: {position.strategy}")
    print(f"   Entry credit: ₹{position.entry_credit:,.2f}")

    if args.force or input("\nConfirm exit? (y/N): ").lower() == 'y':
        manager = get_exit_manager()
        result = manager.execute_exit(reason=ExitReason.MANUAL, position=position)

        if result.success:
            print(f"✅ Exit successful! Realized P&L: ₹{result.realized_pnl:,.2f}")
        else:
            print(f"❌ Exit failed: {result.error}")
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
    print(f"\n📈 Market:")
    print(f"   Trading day: {is_trading_day()}")
    print(f"   Market open: {is_market_open()}")

    try:
        kite = get_kite_client()
        kite.ensure_authenticated()

        nifty = kite.get_nifty_spot()
        vix = kite.get_india_vix()
        margin = kite.get_available_margin()

        print(f"   NIFTY: ₹{nifty:,.2f}")
        print(f"   VIX: {vix:.2f}")
        print(f"   Margin: ₹{margin:,.2f}")
    except Exception as e:
        print(f"   (Market data unavailable: {e})")

    # Position status
    position = get_active_position()

    print(f"\n📊 Position:")
    if position:
        print(f"   ID: {position.id}")
        print(f"   Strategy: {position.strategy}")
        print(f"   ATM: {position.atm_strike}")
        print(f"   Wing distance: {position.wing_distance}")
        print(f"   Entry credit: ₹{position.entry_credit:,.2f}")
        print(f"   Expiry: {position.expiry}")

        # Calculate current P&L
        try:
            legs = get_position_legs(position.id)
            # Would need to fetch quotes for actual P&L
            print(f"   Legs: {len(legs)}")
        except:
            pass
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
        print(f"\n📊 Weekly Summary ({summary.week_start} to {summary.week_end})")
        print(f"   Total P&L: ₹{summary.total_pnl:,.2f}")
        print(f"   Winning days: {summary.winning_days}")
        print(f"   Losing days: {summary.losing_days}")
        print(f"   Trades: {summary.trades_count}")

        if args.send:
            summary_gen.send_weekly_summary(summary)
            print("\n✅ Weekly summary sent to Telegram")
    else:
        summary = summary_gen.generate_daily_summary()
        print(f"\n📊 Daily Summary ({summary.date})")
        print(f"   Has position: {summary.has_position}")
        print(f"   Day P&L change: ₹{summary.day_pnl_change:,.2f}")
        print(f"   Orders: {summary.orders_executed}")
        print(f"   Trades: {len(summary.trades_today)}")

        if args.send:
            summary_gen.send_daily_summary(summary)
            print("\n✅ Daily summary sent to Telegram")

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
            print(f"    ❌ Errors: {result['errors']}")
            tests_failed += 1
        else:
            print("    ✅ Configuration valid")
            tests_passed += 1
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        tests_failed += 1

    # Test 2: Database
    print("\n[2] Testing database...")
    try:
        from src.utils.db import get_db_session
        with get_db_session() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
        print("    ✅ Database connected")
        tests_passed += 1
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        tests_failed += 1

    # Test 3: Kite Auth
    print("\n[3] Testing Kite authentication...")
    try:
        from src.api.kite_client import get_kite_client
        kite = get_kite_client()
        kite.ensure_authenticated()
        profile = kite.profile()
        print(f"    ✅ Authenticated as {profile.get('user_name', 'Unknown')}")
        tests_passed += 1
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        tests_failed += 1

    # Test 4: Telegram
    print("\n[4] Testing Telegram...")
    try:
        from src.api.telegram_alerts import get_telegram
        telegram = get_telegram()
        if telegram.test_connection():
            print("    ✅ Telegram connected")
            tests_passed += 1
        else:
            print("    ⚠️ Telegram test failed")
            tests_failed += 1
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        tests_failed += 1

    # Test 5: Claude
    print("\n[5] Testing Claude API...")
    try:
        from src.api.claude_client import get_claude_client
        claude = get_claude_client()
        if claude.test_connection():
            print("    ✅ Claude API connected")
            tests_passed += 1
        else:
            print("    ⚠️ Claude test failed")
            tests_failed += 1
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        tests_failed += 1

    print("\n" + "=" * 60)
    print(f"Tests: {tests_passed} passed, {tests_failed} failed")
    print("=" * 60)

    return 0 if tests_failed == 0 else 1


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
        print(f"    ✅ {d}/")

    # Initialize database
    print("\n[2] Initializing database...")
    try:
        init_database()
        print("    ✅ Database initialized")
    except Exception as e:
        print(f"    ❌ Failed: {e}")
        return 1

    # Check .env
    print("\n[3] Checking environment...")
    env_file = PROJECT_ROOT / '.env'
    if env_file.exists():
        print("    ✅ .env file found")
    else:
        print("    ⚠️ .env file not found - create from .env.example")

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

    # Test command
    test_parser = subparsers.add_parser('test', help='Run tests')

    # Init command
    init_parser = subparsers.add_parser('init', help='Initialize system')

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
        'test': cmd_test,
        'init': cmd_init,
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

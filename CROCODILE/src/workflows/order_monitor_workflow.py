"""Order Monitoring Workflow - Every 5 Minutes (9:15 AM - 3:45 PM)

This workflow performs four critical functions:
1. Monitor pending entry orders for fills
2. Check GTT status for SL hits and close positions immediately
3. Manage stale pending orders (cancel orders unlikely to fill) - NEW
4. After market close (3:30 PM): Cancel unfulfilled orders and release capital

The GTT status check is essential for real-time position management -
it detects when stop-loss is hit and frees up position slots immediately,
instead of waiting for Same-Day Recovery at 4 PM.

The stale order management (Step 3) improves capital efficiency by:
- Cancelling orders pending > 2 hours (configurable)
- Cancelling orders where price moved > 2% away from limit (configurable)
- Freeing capital for higher-probability opportunities
"""

import sys
from pathlib import Path
from datetime import time as dt_time, datetime
import calendar
import pytz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from src.utils.config_manager import config
from src.utils.timezone_helper import now_ist
from src.services.order_monitor import order_monitor
from src.reporting.telegram_client import telegram

# Market hours (IST)
MARKET_START_HOUR = 9
MARKET_START_MIN = 15
MARKET_END_HOUR = 15
MARKET_END_MIN = 45

# Market close time
MARKET_CLOSE_TIME = dt_time(15, 30)  # 3:30 PM IST

# EOD Order Cleanup start time
# Exchange doesn't allow cancellations during 15:30-15:40 post-market window
# So we delay EOD cleanup until 15:40 when cancellations are accepted
EOD_CLEANUP_START_TIME = dt_time(15, 40)  # 3:40 PM IST


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  ORDER MONITOR WORKFLOW - Orders, GTT & Stale Order Check   *
***************************************************************"""
    logger.info(banner)


def get_current_datetime():
    """Get current IST date and time"""
    now = datetime.now(pytz.timezone('Asia/Kolkata'))
    weekday = calendar.day_name[now.weekday()]
    curr_date = now.date()
    curr_time = now.time()
    return weekday, curr_date, curr_time, now


def check_weekend(weekday):
    """Exit if weekend"""
    if weekday in ['Saturday', 'Sunday']:
        logger.info(f"Weekend ({weekday}) - Exiting")
        sys.exit(0)


def check_market_hours(now):
    """Check if current time is within market hours"""
    curr_time = now.replace(second=0, microsecond=0)
    start_time = now.replace(hour=MARKET_START_HOUR, minute=MARKET_START_MIN, second=0, microsecond=0)
    end_time = now.replace(hour=MARKET_END_HOUR, minute=MARKET_END_MIN, second=0, microsecond=0)

    if curr_time < start_time or curr_time > end_time:
        logger.info(f"Outside market hours ({curr_time.strftime('%H:%M')}) - Exiting")
        return False
    return True


def is_after_market_close() -> bool:
    """Check if current time is after market close (3:30 PM IST)"""
    current_time = now_ist().time()
    return current_time >= MARKET_CLOSE_TIME


def is_eod_cleanup_time() -> bool:
    """Check if current time is suitable for EOD order cleanup (3:40 PM IST onwards)

    Exchange doesn't allow order cancellations during 15:30-15:40 post-market window.
    We delay EOD cleanup until 15:40 when cancellations are accepted.
    """
    current_time = now_ist().time()
    return current_time >= EOD_CLEANUP_START_TIME


def monitor_orders():
    """
    Monitor pending orders for fills AND check GTT status for SL hits

    Run every 5 minutes during market hours.
    After market close (3:30 PM): Also cancel unfulfilled orders.
    """
    print_workflow_banner()
    logger.info("Order monitoring workflow started")

    # Get current date/time
    weekday, curr_date, curr_time, now = get_current_datetime()
    logger.info(f"Date: {curr_date} ({weekday}) | Time: {curr_time.strftime('%H:%M:%S')} IST")

    # Check if weekend
    check_weekend(weekday)

    # Check market hours
    if not check_market_hours(now):
        sys.exit(0)

    try:
        # Check if after market close
        after_market_close = is_after_market_close()
        if after_market_close:
            logger.info("Running AFTER market close - will include EOD order cleanup")

        # Part 1: Monitor pending entry orders
        logger.info("[STEP 1/4] Checking pending entry orders...")
        order_stats = order_monitor.monitor_all_pending_orders()

        logger.info(
            f"Entry order monitoring: Pending={order_stats['pending']}, "
            f"Filled={order_stats['filled']}, Cancelled={order_stats['cancelled']}, "
            f"StillPending={order_stats['still_pending']}"
        )

        # Part 2: Check GTT status for SL hits (real-time position closure)
        logger.info("[STEP 2/4] Checking GTT status for SL hits...")
        gtt_stats = order_monitor.check_gtt_triggered_positions()

        logger.info(
            f"GTT status check: Positions checked={gtt_stats['positions_checked']}, "
            f"SL hits={gtt_stats['sl_hits_detected']}, "
            f"Positions closed={gtt_stats['positions_closed']}"
        )

        # Part 3: Manage stale pending orders (cancel orders unlikely to fill)
        # Only run during market hours (not after close - EOD cleanup handles that)
        stale_stats = None
        if not after_market_close:
            logger.info("[STEP 3/4] Checking for stale pending orders...")
            stale_stats = order_monitor.manage_stale_pending_orders()

            stale_cancelled = stale_stats['stale_by_time'] + stale_stats['stale_by_price']
            if stale_cancelled > 0:
                logger.info(
                    f"Stale order cleanup: Cancelled={stale_cancelled} "
                    f"(Time={stale_stats['stale_by_time']}, Price={stale_stats['stale_by_price']}), "
                    f"Capital freed=Rs.{stale_stats['capital_released']:.2f}"
                )
            else:
                logger.info(f"Stale order check: {stale_stats['pending_checked']} orders checked, none stale")
        else:
            logger.info("[STEP 3/4] Skipped stale order check (after market close)")

        # Part 4: EOD Order Cleanup (only after 3:40 PM when exchange allows cancellations)
        # Exchange doesn't allow cancellations during 15:30-15:40 post-market window
        eod_stats = None
        eod_cleanup_ready = is_eod_cleanup_time()
        if eod_cleanup_ready:
            logger.info("[STEP 4/4] EOD Order Cleanup - Cancelling unfulfilled orders...")
            eod_stats = order_monitor.cancel_unfulfilled_orders()

            if eod_stats['pending_found'] > 0:
                logger.info(
                    f"EOD Cleanup: Found={eod_stats['pending_found']}, "
                    f"Cancelled={eod_stats['cancelled']}, "
                    f"PartialFills={eod_stats['partial_fills']}, "
                    f"CapitalReleased=Rs.{eod_stats['capital_released']:.2f}"
                )
            else:
                logger.info("EOD Cleanup: No pending orders to cancel")
        elif after_market_close:
            logger.info("[STEP 4/4] Skipped EOD cleanup (waiting until 15:40 for exchange to allow cancellations)")
        else:
            logger.info("[STEP 4/4] Skipped EOD cleanup (market still open)")

        # Log combined summary
        summary = (
            f"Order monitoring workflow complete: "
            f"Orders (Filled={order_stats['filled']}, Pending={order_stats['still_pending']}), "
            f"GTT (SL Hits={gtt_stats['sl_hits_detected']}, Closed={gtt_stats['positions_closed']})"
        )
        if stale_stats and (stale_stats['stale_by_time'] + stale_stats['stale_by_price']) > 0:
            summary += f", Stale (Cancelled={stale_stats['stale_by_time'] + stale_stats['stale_by_price']}, Freed=Rs.{stale_stats['capital_released']:.2f})"
        if eod_stats and eod_stats['pending_found'] > 0:
            summary += f", EOD (Cancelled={eod_stats['cancelled']}, Released=Rs.{eod_stats['capital_released']:.2f})"

        logger.info(summary)

        # Note: Detailed alerts for filled orders and SL hits are sent from order_monitor.py
        # No need for generic alert here

        return True

    except Exception as e:
        logger.error(f"Order monitoring failed: {e}", exc_info=True)
        error_msg = f"Order monitoring error: {str(e)}"
        telegram.send_alert(error_msg, critical=False)
        return False


if __name__ == "__main__":
    # ===== EARLY EXIT CHECK =====
    # Check market hours BEFORE any setup to exit gracefully if outside trading hours
    now = datetime.now(pytz.timezone('Asia/Kolkata'))
    weekday = calendar.day_name[now.weekday()]

    # Exit silently on weekends
    if weekday in ['Saturday', 'Sunday']:
        sys.exit(0)

    # Exit silently before market hours
    curr_time = now.replace(second=0, microsecond=0)
    start_time = now.replace(hour=MARKET_START_HOUR, minute=MARKET_START_MIN, second=0, microsecond=0)
    end_time = now.replace(hour=MARKET_END_HOUR, minute=MARKET_END_MIN, second=0, microsecond=0)

    if curr_time < start_time or curr_time > end_time:
        sys.exit(0)
    # ===== END EARLY EXIT CHECK =====

    # Setup logging
    logger.add(
        "logs/crocodile_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="45 days",
        level=config.get('logging.level', 'INFO')
    )

    success = monitor_orders()
    sys.exit(0 if success else 1)

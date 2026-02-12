"""Signal Processing Workflow - Every 2 Minutes (9:15 AM - 3:30 PM)"""

import sys
from pathlib import Path
from datetime import date, datetime
import calendar
import pytz

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from src.utils.config_manager import config
from src.services.entry_manager import entry_manager
from src.reporting.telegram_client import telegram
from src.models.database import get_session, OpenOrder, OpenPosition, ClosedPosition, OrderStatus, PositionStatus, ProcessedSignal
from src.api.broker_factory import get_broker
from src.utils.timezone_helper import ist_now_naive
from src.utils.cost_calculator import cost_calculator

# Market hours (IST)
MARKET_START_HOUR = 9
MARKET_START_MIN = 15
MARKET_END_HOUR = 15
MARKET_END_MIN = 30


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  SIGNAL PROCESSOR WORKFLOW - Process CSV Signals & Orders   *
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


def reconcile_positions_with_zerodha():
    """
    Reconcile DB open positions with actual Zerodha holdings.

    Called at script startup to detect and close any positions that were
    stopped out (GTT triggered) when the bot was not running.

    Checks:
    1. Holdings API (quantity + t1_quantity for pending settlement)
    2. Day positions (bought today, not yet in holdings)
    3. Today's sell orders (for exit price of stopped out positions)
    """
    logger.info("=" * 60)
    logger.info("STARTUP RECONCILIATION: Syncing positions with Zerodha...")
    logger.info("=" * 60)

    session = get_session()
    broker = get_broker()

    try:
        # Get underlying kite client for holdings
        kite = broker._get_kite()

        # 1. Get holdings (including T1 pending settlement)
        holdings = kite.holdings()
        holdings_map = {}
        for h in holdings:
            symbol = h.get('tradingsymbol', '')
            qty = h.get('quantity', 0)
            t1_qty = h.get('t1_quantity', 0)
            total_qty = qty + t1_qty
            if total_qty > 0:
                holdings_map[symbol] = total_qty

        # 2. Get day positions (bought today, pending settlement tomorrow)
        positions = kite.positions()
        day_pos_map = {}
        for p in positions.get('net', []):
            symbol = p.get('tradingsymbol', '')
            qty = p.get('quantity', 0)
            if qty > 0:  # Only buys (positive qty)
                day_pos_map[symbol] = qty

        # 3. Get today's sell orders (for exit prices)
        orders = kite.orders()
        today_sells = {}
        for o in orders:
            if o.get('transaction_type') == 'SELL' and o.get('status') == 'COMPLETE':
                symbol = o.get('tradingsymbol', '')
                today_sells[symbol] = o.get('average_price', 0)

        # 4. Get DB open positions for this bot
        bot_id = config.get_bot_instance_id()
        db_positions = session.query(OpenPosition).filter_by(
            bot_instance_id=bot_id,
            status=PositionStatus.OPEN
        ).all()

        if not db_positions:
            logger.info("No open positions in DB to reconcile")
            return

        logger.info(f"Checking {len(db_positions)} DB positions against Zerodha...")

        closed_count = 0
        kept_count = 0

        for p in db_positions:
            total_hld = holdings_map.get(p.script, 0)
            day_qty = day_pos_map.get(p.script, 0)

            # Position is active if in holdings OR bought today
            if total_hld > 0 or day_qty > 0:
                kept_count += 1
                continue

            # Position NOT in Zerodha - needs to be closed
            sold_today = p.script in today_sells

            if sold_today:
                exit_price = today_sells[p.script]
                exit_reason = 'GTT_SL_TRIGGERED'
            else:
                # Sold on prior day - use entry as fallback
                exit_price = p.entry_price
                exit_reason = 'STALE_POSITION_SYNC'

            # Calculate P&L
            pnl_details = cost_calculator.calculate_pnl(
                entry_price=p.entry_price,
                exit_price=exit_price,
                quantity=p.quantity
            )

            # Create closed position record
            closed = ClosedPosition(
                bot_instance_id=p.bot_instance_id,
                script=p.script,
                timeframe=p.timeframe,
                entry_date=p.entry_date,
                entry_price=p.entry_price,
                quantity=p.quantity,
                capital_deployed=p.capital_deployed,
                exit_date=date.today(),
                exit_price=exit_price,
                exit_reason=exit_reason,
                gross_pnl=pnl_details['gross_pnl'],
                transaction_costs=pnl_details['total_costs'],
                cost_breakdown=pnl_details.get('cost_breakdown'),
                net_pnl=pnl_details['net_pnl'],
                pnl_percent=pnl_details['pnl_percent'],
                days_held=pnl_details['days_held'],
                sl_movements=p.sl_movements if hasattr(p, 'sl_movements') else 0,
                highest_sl_achieved=p.highest_sl if hasattr(p, 'highest_sl') else None,
                entry_atr=p.entry_atr if hasattr(p, 'entry_atr') else None,
                closed_at=ist_now_naive()
            )

            session.add(closed)
            session.delete(p)

            logger.warning(
                f"RECONCILED: {p.script}({p.timeframe}) - "
                f"Exit={exit_price:.2f}, Net P&L={pnl_details['net_pnl']:.2f}, Reason={exit_reason}"
            )
            closed_count += 1

        session.commit()

        if closed_count > 0:
            logger.warning(f"Reconciliation complete: Closed {closed_count} stale positions, {kept_count} active")
            telegram.send_alert(
                f"🔄 **Startup Position Reconciliation**\n\n"
                f"Closed {closed_count} stale position(s) not found in Zerodha.\n"
                f"Active positions: {kept_count}",
                critical=False
            )
        else:
            logger.info(f"Reconciliation complete: All {kept_count} positions verified active")

    except Exception as e:
        logger.error(f"Error reconciling positions: {e}", exc_info=True)
        session.rollback()
    finally:
        session.close()


def reconcile_processing_signals():
    """
    Reconcile signals stuck in PROCESSING status

    Called at script startup before processing new signals.
    Checks if orders were actually placed on Zerodha for signals
    that were marked PROCESSING but script crashed before completion.
    """
    logger.info("="*60)
    logger.info("STARTUP RECONCILIATION: Checking for stuck signals...")
    logger.info("="*60)

    session = get_session()
    kite_client = get_broker()

    try:
        # Find all signals in PROCESSING status
        processing_signals = session.query(ProcessedSignal).filter(
            ProcessedSignal.processing_status == 'PROCESSING'
        ).all()

        if not processing_signals:
            logger.info("No signals in PROCESSING status - all clear")
            return

        logger.warning(f"Found {len(processing_signals)} signals in PROCESSING status")

        reconciled = []

        for signal in processing_signals:
            logger.info(f"Reconciling: {signal.script} ({signal.timeframe}) from {signal.date}")

            # Check if order exists on Zerodha
            try:
                zerodha_orders = kite_client.get_all_orders()

                # Look for matching order
                matching_orders = [
                    o for o in zerodha_orders
                    if signal.script in o.get('tradingsymbol', '')
                    and str(signal.date) in o.get('order_timestamp', '')
                ]

                if matching_orders:
                    # Order WAS placed on Zerodha
                    order = matching_orders[0]
                    order_id = order['order_id']

                    logger.info(f"✅ Found order on Zerodha: {order_id}")

                    # Update signal to SUCCESS
                    signal.processing_status = 'SUCCESS'
                    signal.entry_successful = True
                    signal.order_id = order_id
                    signal.completed_at = ist_now_naive()

                    # Also save to OpenOrder table if not exists
                    existing_order = session.query(OpenOrder).filter(
                        OpenOrder.order_id == order_id
                    ).first()

                    if not existing_order:
                        # Save order to database
                        open_order = OpenOrder(
                            script=signal.script,
                            timeframe=signal.timeframe,
                            order_id=order_id,
                            order_type=order.get('order_type', 'LIMIT'),
                            order_price=order.get('price'),
                            quantity=order.get('quantity'),
                            capital_deployed=order.get('price', 0) * order.get('quantity', 0),
                            status=OrderStatus.PENDING if order['status'] == 'OPEN' else OrderStatus.FILLED,
                            placed_at=ist_now_naive()
                        )
                        session.add(open_order)

                    reconciled.append({
                        'script': signal.script,
                        'status': 'RECOVERED',
                        'order_id': order_id
                    })
                else:
                    # Order NOT found on Zerodha
                    logger.warning(f"❌ Order NOT found on Zerodha for {signal.script}")

                    # Update signal to FAILED
                    signal.processing_status = 'FAILED'
                    signal.entry_successful = False
                    signal.rejection_reason = 'Order not found during reconciliation'
                    signal.completed_at = ist_now_naive()

                    reconciled.append({
                        'script': signal.script,
                        'status': 'MARKED_FAILED',
                        'order_id': None
                    })

                session.commit()

            except Exception as e:
                logger.error(f"Error reconciling {signal.script}: {e}")
                reconciled.append({
                    'script': signal.script,
                    'status': 'ERROR',
                    'error': str(e)
                })

        # Send Telegram alert about reconciliation
        if reconciled:
            alert_msg = "<b>⚠️ Reconciliation Complete</b>\n\n"
            alert_msg += f"Found {len(processing_signals)} signals in PROCESSING state:\n\n"

            for item in reconciled:
                if item['status'] == 'RECOVERED':
                    alert_msg += f"✅ {item['script']}: Order recovered (ID: {item['order_id']})\n"
                elif item['status'] == 'MARKED_FAILED':
                    alert_msg += f"❌ {item['script']}: No order found, marked as FAILED\n"
                else:
                    alert_msg += f"⚠️ {item['script']}: Reconciliation error\n"

            telegram.send_alert(alert_msg, critical=True)

        logger.info(f"Reconciliation complete: {len(reconciled)} signals processed")
        logger.info("="*60)

    except Exception as e:
        logger.error(f"Error during reconciliation: {e}", exc_info=True)
    finally:
        session.close()


def process_signals():
    """
    Process signals from CSV file

    Run every 2 minutes during market hours
    """
    print_workflow_banner()
    logger.info("Signal processing workflow started")

    # Get current date/time
    weekday, curr_date, curr_time, now = get_current_datetime()
    logger.info(f"Date: {curr_date} ({weekday}) | Time: {curr_time.strftime('%H:%M:%S')} IST")

    # Check if weekend
    check_weekend(weekday)

    # Check market hours
    if not check_market_hours(now):
        sys.exit(0)

    try:
        # ====== STEP 1: POSITION RECONCILIATION (SYNC WITH ZERODHA) ======
        # Detect and close positions that were stopped out while bot was down
        reconcile_positions_with_zerodha()

        # ====== STEP 2: SIGNAL RECONCILIATION (IDEMPOTENCY LAYER) ======
        reconcile_processing_signals()

        # ====== STEP 3: PROCESS NEW SIGNALS ======
        stats = entry_manager.process_all_signals()

        logger.info(
            f"Signal processing complete: Total={stats['total']}, "
            f"Processed={stats['processed']}, Success={stats['success']}, "
            f"Failed={stats['failed']}, Skipped={stats['skipped']}"
        )

        # Send alert only if new entries were successful
        if stats['success'] > 0:
            # Query today's orders from database for detailed alert
            session = get_session()
            try:
                today = date.today()
                today_start = datetime.combine(today, datetime.min.time())
                today_end = datetime.combine(today, datetime.max.time())

                todays_orders = session.query(OpenOrder).filter(
                    OpenOrder.placed_at >= today_start,
                    OpenOrder.placed_at <= today_end,
                    OpenOrder.status != OrderStatus.CANCELLED
                ).all()

                if todays_orders:
                    # Build compact table format
                    total_capital = sum(o.capital_deployed for o in todays_orders)

                    lines = [
                        f"✅ *ORDERS PLACED* (+{stats['success']} => {len(todays_orders)} today) | Rs.{total_capital:,.0f}",
                        "<pre>",
                        f"{'Script':<14} {'Qty':>4} {'Price':>8} {'Capital':>8}",
                        "-" * 38
                    ]

                    for order in todays_orders:
                        script_tf = f"{order.script}({order.timeframe})"
                        capital_k = order.capital_deployed / 1000
                        lines.append(
                            f"{script_tf:<14} {order.quantity:>4} {order.order_price:>8.2f} {capital_k:>7.1f}K"
                        )

                    lines.append("</pre>")
                    alert_msg = "\n".join(lines)
                else:
                    # Fallback if no orders found in DB
                    alert_msg = f"✅ *ORDERS PLACED* | {stats['success']} signal(s) processed"
            finally:
                session.close()

            telegram.send_alert(alert_msg, critical=False)

        return True

    except Exception as e:
        logger.error(f"Signal processing failed: {e}", exc_info=True)
        error_msg = f"⚠️ Signal processing error: {str(e)}"
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

    # Exit silently before/after market hours
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

    success = process_signals()
    sys.exit(0 if success else 1)

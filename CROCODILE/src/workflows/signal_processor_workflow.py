"""Signal Processing Workflow - Every 2 Minutes (9:15 AM - 3:30 PM)"""

import sys
from pathlib import Path
from datetime import date, datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from loguru import logger

from src.utils.config_manager import config
from src.services.entry_manager import entry_manager
from src.reporting.telegram_client import telegram
from src.models.database import get_session, OpenOrder, OrderStatus, ProcessedSignal
from src.api.kite_trade_client import KiteTradeClient
from src.utils.timezone_helper import ist_now_naive


def print_workflow_banner():
    """Print a clear banner to identify workflow start in logs"""
    banner = """
***************************************************************
*  SIGNAL PROCESSOR WORKFLOW - Process CSV Signals & Orders   *
***************************************************************"""
    logger.info(banner)


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
    kite_client = KiteTradeClient()

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

    try:
        # ====== STEP 1: RECONCILIATION (IDEMPOTENCY LAYER) ======
        reconcile_processing_signals()

        # ====== STEP 2: PROCESS NEW SIGNALS ======
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
                    OpenOrder.status == OrderStatus.PENDING
                ).all()

                if todays_orders:
                    # Build detailed order table
                    lines = [
                        f"<b>✅ ORDERS PLACED SUCCESSFULLY ({len(todays_orders)}/{stats['total']})</b>\n",
                        "<pre>"
                    ]

                    # Header
                    lines.append(
                        f"{'Script':<12} {'TF':<3} {'Type':<6} {'Price':>9} {'Qty':>4} {'Capital':>10}"
                    )
                    lines.append("-" * 50)

                    total_capital = 0

                    # Order rows
                    for order in todays_orders:
                        price_str = f"Rs.{order.order_price:,.2f}"
                        capital_str = f"Rs.{order.capital_deployed:,.2f}"

                        lines.append(
                            f"{order.script:<12} {order.timeframe:<3} {order.order_type:<6} "
                            f"{price_str:>9} {order.quantity:>4} {capital_str:>10}"
                        )
                        total_capital += order.capital_deployed

                    lines.append("</pre>")
                    lines.append(f"\n<b>Total Capital Reserved: Rs.{total_capital:,.2f}</b>")
                    lines.append(f"\n⏳ Orders placed - monitoring for fills")

                    alert_msg = "\n".join(lines)
                else:
                    # Fallback if no orders found in DB
                    alert_msg = (
                        f"<b>📊 New Entry Orders Placed</b>\n"
                        f"✅ {stats['success']} signal(s) processed successfully\n"
                        f"⏳ Orders placed - monitoring for fills"
                    )
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
    # Setup logging
    logger.add(
        "logs/crocodile_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="45 days",
        level=config.get('logging.level', 'INFO')
    )

    success = process_signals()
    sys.exit(0 if success else 1)

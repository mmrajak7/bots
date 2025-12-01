"""
Display all database tables in formatted table view
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from tabulate import tabulate

from src.models.database import (
    OpenPosition, OpenOrder, ClosedPosition,
    CapitalLedger, GTTUpdateLog, TransactionHistory,
    PositionStatus, OrderStatus, get_engine
)


def show_all_tables():
    """Display all database tables in formatted view"""

    engine = get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # ===== 1. OPEN POSITIONS =====
        logger.info("\n" + "=" * 120)
        logger.info("OPEN POSITIONS")
        logger.info("=" * 120)

        open_positions = session.query(OpenPosition).all()

        if open_positions:
            data = []
            for pos in open_positions:
                data.append([
                    pos.id,
                    pos.script,
                    pos.timeframe,
                    pos.status.value,
                    pos.entry_date,
                    f"{pos.entry_price:.2f}",
                    pos.quantity,
                    f"{pos.current_sl:.2f}",
                    f"{pos.initial_sl:.2f}",
                    pos.sl_movements,
                    pos.current_gtt_id,
                    pos.days_held
                ])

            headers = ['ID', 'Script', 'TF', 'Status', 'Entry Date', 'Entry', 'Qty',
                      'Current SL', 'Initial SL', 'SL Moves', 'GTT ID', 'Days']
            print(tabulate(data, headers=headers, tablefmt='grid'))
        else:
            print("No open positions")

        # ===== 2. CLOSED POSITIONS =====
        logger.info("\n" + "=" * 120)
        logger.info("CLOSED POSITIONS")
        logger.info("=" * 120)

        closed_positions = session.query(ClosedPosition).order_by(
            ClosedPosition.closed_at.desc()
        ).limit(20).all()

        if closed_positions:
            data = []
            for pos in closed_positions:
                data.append([
                    pos.id,
                    pos.script,
                    pos.timeframe,
                    pos.entry_date,
                    pos.exit_date,
                    f"{pos.entry_price:.2f}",
                    f"{pos.exit_price:.2f}",
                    pos.quantity,
                    f"{pos.net_pnl:.2f}",
                    f"{pos.pnl_percent:.2f}%",
                    pos.exit_reason,
                    pos.days_held,
                    str(pos.closed_at)[:19]
                ])

            headers = ['ID', 'Script', 'TF', 'Entry Date', 'Exit Date', 'Entry',
                      'Exit', 'Qty', 'Net P&L', 'P&L %', 'Reason', 'Days', 'Closed At']
            print(tabulate(data, headers=headers, tablefmt='grid'))
        else:
            print("No closed positions")

        # ===== 3. OPEN ORDERS =====
        logger.info("\n" + "=" * 120)
        logger.info("OPEN ORDERS")
        logger.info("=" * 120)

        open_orders = session.query(OpenOrder).order_by(
            OpenOrder.placed_at.desc()
        ).limit(20).all()

        if open_orders:
            data = []
            for order in open_orders:
                data.append([
                    order.id,
                    order.script,
                    order.order_id,
                    order.order_type,
                    order.status.value,
                    order.quantity,
                    order.filled_qty,
                    f"{order.order_price:.2f}" if order.order_price else 'MARKET',
                    f"{order.fill_price:.2f}" if order.fill_price else '-',
                    str(order.placed_at)[:19],
                    order.rejection_reason or '-'
                ])

            headers = ['ID', 'Script', 'Order ID', 'Type', 'Status', 'Qty',
                      'Filled', 'Order Price', 'Fill Price', 'Placed At', 'Rejection']
            print(tabulate(data, headers=headers, tablefmt='grid'))
        else:
            print("No open orders")

        # ===== 4. GTT UPDATE LOG (Recent 20) =====
        logger.info("\n" + "=" * 120)
        logger.info("GTT UPDATE LOG (Recent 20)")
        logger.info("=" * 120)

        gtt_logs = session.query(GTTUpdateLog).order_by(
            GTTUpdateLog.created_at.desc()
        ).limit(20).all()

        if gtt_logs:
            data = []
            for log in gtt_logs:
                data.append([
                    log.id,
                    log.position_id,
                    log.script,
                    log.update_date,
                    f"{log.old_sl:.2f}" if log.old_sl else '-',
                    f"{log.new_sl:.2f}",
                    log.old_gtt_id or '-',
                    log.new_gtt_id or '-',
                    log.status,
                    str(log.created_at)[:19]
                ])

            headers = ['ID', 'Pos ID', 'Script', 'Date', 'Old SL', 'New SL',
                      'Old GTT', 'New GTT', 'Status', 'Created At']
            print(tabulate(data, headers=headers, tablefmt='grid'))
        else:
            print("No GTT update logs")

        # ===== 5. CAPITAL LEDGER (Recent 10) =====
        logger.info("\n" + "=" * 120)
        logger.info("CAPITAL LEDGER (Recent 10 Days)")
        logger.info("=" * 120)

        capital_ledger = session.query(CapitalLedger).order_by(
            CapitalLedger.date.desc()
        ).limit(10).all()

        if capital_ledger:
            data = []
            for ledger in capital_ledger:
                data.append([
                    ledger.id,
                    ledger.date,
                    f"{ledger.opening_capital:.2f}",
                    f"{ledger.deployed_capital:.2f}",
                    f"{ledger.free_capital:.2f}",
                    f"{ledger.realized_pnl_today:.2f}",
                    f"{ledger.unrealized_pnl:.2f}",
                    f"{ledger.total_capital:.2f}",
                    ledger.num_open_positions,
                    ledger.num_entries_today,
                    ledger.num_exits_today
                ])

            headers = ['ID', 'Date', 'Opening', 'Deployed', 'Free', 'Realized P&L',
                      'Unrealized', 'Total', 'Open Pos', 'Entries', 'Exits']
            print(tabulate(data, headers=headers, tablefmt='grid'))
        else:
            print("No capital ledger entries")

        # ===== 6. TRANSACTION HISTORY (Recent 20) =====
        logger.info("\n" + "=" * 120)
        logger.info("TRANSACTION HISTORY (Recent 20)")
        logger.info("=" * 120)

        transactions = session.query(TransactionHistory).order_by(
            TransactionHistory.created_at.desc()
        ).limit(20).all()

        if transactions:
            data = []
            for txn in transactions:
                data.append([
                    txn.id,
                    txn.transaction_type.value,
                    txn.script,
                    txn.transaction_date,
                    f"{txn.price:.2f}",
                    txn.quantity,
                    f"{txn.value:.2f}",
                    txn.order_id or '-',
                    txn.gtt_id or '-',
                    str(txn.created_at)[:19]
                ])

            headers = ['ID', 'Type', 'Script', 'Date', 'Price', 'Qty',
                      'Value', 'Order ID', 'GTT ID', 'Created At']
            print(tabulate(data, headers=headers, tablefmt='grid'))
        else:
            print("No transaction history")

        # ===== SUMMARY =====
        logger.info("\n" + "=" * 120)
        logger.info("DATABASE SUMMARY")
        logger.info("=" * 120)

        summary_data = [
            ['Open Positions', len(open_positions)],
            ['  - OPEN', len([p for p in open_positions if p.status == PositionStatus.OPEN])],
            ['  - CLOSED_SL', len([p for p in open_positions if p.status == PositionStatus.CLOSED_SL])],
            ['  - CLOSED_MANUAL', len([p for p in open_positions if p.status == PositionStatus.CLOSED_MANUAL])],
            ['Closed Positions', session.query(ClosedPosition).count()],
            ['Open Orders', len(open_orders)],
            ['  - PENDING', len([o for o in open_orders if o.status == OrderStatus.PENDING])],
            ['  - FILLED', len([o for o in open_orders if o.status == OrderStatus.FILLED])],
            ['  - CANCELLED', len([o for o in open_orders if o.status == OrderStatus.CANCELLED])],
            ['GTT Update Logs', session.query(GTTUpdateLog).count()],
            ['Capital Ledger Entries', session.query(CapitalLedger).count()],
            ['Transaction History', session.query(TransactionHistory).count()],
        ]

        print(tabulate(summary_data, headers=['Table', 'Count'], tablefmt='grid'))
        print("\n")

    finally:
        session.close()


if __name__ == "__main__":
    # Setup logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<level>{message}</level>",
        level="INFO"
    )

    show_all_tables()

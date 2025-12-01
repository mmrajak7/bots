"""
Check database status - see what happened to all positions
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models.database import (
    OpenPosition, OpenOrder, ClosedPosition,
    PositionStatus, OrderStatus, get_engine
)


def check_database_status():
    """Check all tables to see current state"""

    logger.info("=" * 80)
    logger.info("DATABASE STATUS CHECK")
    logger.info("=" * 80)

    engine = get_engine()
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Check open positions
        logger.info("\n[1] OPEN POSITIONS")
        logger.info("-" * 80)
        open_positions = session.query(OpenPosition).all()
        logger.info(f"Total: {len(open_positions)}")

        for pos in open_positions:
            logger.info(
                f"  {pos.script:<15} | Status: {pos.status.value:<15} | "
                f"Qty: {pos.quantity:>3} | Entry: {pos.entry_price:>8.2f} | "
                f"SL: {pos.current_sl:>8.2f} | GTT: {pos.current_gtt_id}"
            )

        # Check closed positions
        logger.info("\n[2] CLOSED POSITIONS (Recent 10)")
        logger.info("-" * 80)
        closed_positions = session.query(ClosedPosition).order_by(
            ClosedPosition.closed_at.desc()
        ).limit(10).all()
        logger.info(f"Total: {len(closed_positions)}")

        for pos in closed_positions:
            logger.info(
                f"  {pos.script:<15} | Exit Reason: {pos.exit_reason:<15} | "
                f"Entry: {pos.entry_price:>8.2f} | Exit: {pos.exit_price:>8.2f} | "
                f"P&L: {pos.net_pnl:>8.2f} | Closed: {pos.closed_at}"
            )

        # Check open orders
        logger.info("\n[3] OPEN ORDERS")
        logger.info("-" * 80)
        open_orders = session.query(OpenOrder).all()
        logger.info(f"Total: {len(open_orders)}")

        for order in open_orders:
            logger.info(
                f"  {order.script:<15} | Order ID: {order.order_id} | "
                f"Status: {order.status.value:<12} | Qty: {order.quantity:>3} | "
                f"Placed: {order.placed_at}"
            )

        # Summary
        logger.info("\n" + "=" * 80)
        logger.info("SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Open positions:   {len(open_positions)}")
        logger.info(f"Closed positions: {session.query(ClosedPosition).count()}")
        logger.info(f"Open orders:      {len(open_orders)}")
        logger.info("=" * 80)

    finally:
        session.close()


if __name__ == "__main__":
    # Setup logging
    logger.remove()
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )

    check_database_status()

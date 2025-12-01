"""
Comprehensive Database Verification and Sync
Cross-checks trading.db with Zerodha actuals and updates discrepancies
"""

import sys
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.kite_trade_client import KiteTradeClient
from src.models.database import (
    OpenPosition, OpenOrder, ClosedPosition, CapitalLedger,
    PositionStatus, OrderStatus, get_engine
)
from src.utils.config_manager import config
from src.utils.timezone_helper import today_ist


def verify_and_sync_database():
    """
    Comprehensive verification:
    1. Check open positions vs Zerodha positions
    2. Check open orders vs Zerodha orders
    3. Check GTT orders match
    4. Update database where needed
    """
    logger.info("=" * 80)
    logger.info("DATABASE VERIFICATION & SYNC - Starting")
    logger.info("=" * 80)

    try:
        # Initialize Kite client
        logger.info("\n[1/5] Initializing Kite Trade client...")
        kite = KiteTradeClient()

        # Initialize database session
        engine = get_engine()
        Session = sessionmaker(bind=engine)
        session = Session()

        stats = {
            'positions_checked': 0,
            'positions_synced': 0,
            'orders_checked': 0,
            'orders_synced': 0,
            'gtts_checked': 0,
            'gtts_synced': 0,
            'discrepancies': []
        }

        try:
            # ===== STEP 1: Fetch Zerodha Data =====
            logger.info("\n[2/5] Fetching data from Zerodha...")

            zerodha_positions = kite.get_positions()
            zerodha_net_positions = zerodha_positions.get('net', [])
            zerodha_day_positions = zerodha_positions.get('day', [])

            # Filter for CNC positions with non-zero quantity
            zerodha_holdings = [
                pos for pos in zerodha_net_positions
                if pos.get('product') == 'CNC' and pos.get('quantity', 0) != 0
            ]

            logger.info(f"  ✓ Zerodha net positions (CNC): {len(zerodha_holdings)}")

            zerodha_orders = kite.get_all_orders()
            # Filter for bot orders only (tag='croc')
            bot_tag = config.get('kite.order_tag', 'croc')
            zerodha_bot_orders = [
                order for order in zerodha_orders
                if order.get('tag') == bot_tag
            ]
            logger.info(f"  ✓ Zerodha bot orders (tag={bot_tag}): {len(zerodha_bot_orders)}")

            zerodha_gtts = kite.get_gtt_orders()
            zerodha_active_gtts = [
                gtt for gtt in zerodha_gtts
                if gtt.get('status') == 'active'
            ]
            logger.info(f"  ✓ Zerodha active GTTs: {len(zerodha_active_gtts)}")

            # ===== STEP 2: Query Database =====
            logger.info("\n[3/5] Querying database...")

            db_open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()
            logger.info(f"  ✓ DB open positions: {len(db_open_positions)}")

            db_open_orders = session.query(OpenOrder).filter(
                OpenOrder.status.in_([OrderStatus.PENDING, OrderStatus.FILLED])
            ).all()
            logger.info(f"  ✓ DB open orders: {len(db_open_orders)}")

            # ===== STEP 3: Cross-Check Positions =====
            logger.info("\n[4/5] Cross-checking positions...")
            logger.info("-" * 80)

            # Create mapping of Zerodha positions by symbol
            zerodha_pos_map = {
                pos['tradingsymbol']: pos
                for pos in zerodha_holdings
            }

            # Create mapping of DB positions by script
            db_pos_map = {
                pos.script: pos
                for pos in db_open_positions
            }

            # Check each DB position against Zerodha
            for db_pos in db_open_positions:
                stats['positions_checked'] += 1
                script = db_pos.script
                zerodha_pos = zerodha_pos_map.get(script)

                if not zerodha_pos:
                    # Position in DB but not on Zerodha - might be closed
                    discrepancy = (
                        f"⚠️ Position {script} exists in DB but NOT on Zerodha "
                        f"(DB qty: {db_pos.quantity}, Entry: {db_pos.entry_price})"
                    )
                    stats['discrepancies'].append(discrepancy)
                    logger.warning(discrepancy)
                else:
                    # Position exists on both - check quantity
                    zerodha_qty = zerodha_pos.get('quantity', 0)
                    db_qty = db_pos.quantity

                    if zerodha_qty != db_qty:
                        discrepancy = (
                            f"⚠️ Quantity mismatch for {script}: "
                            f"DB={db_qty}, Zerodha={zerodha_qty}"
                        )
                        stats['discrepancies'].append(discrepancy)
                        logger.warning(discrepancy)
                    else:
                        logger.info(f"  ✓ {script}: Quantity matches ({db_qty} shares)")

            # Check for positions on Zerodha but not in DB
            for script in zerodha_pos_map:
                if script not in db_pos_map:
                    zerodha_pos = zerodha_pos_map[script]
                    discrepancy = (
                        f"⚠️ Position {script} exists on Zerodha but NOT in DB "
                        f"(Zerodha qty: {zerodha_pos.get('quantity')}, "
                        f"Avg price: {zerodha_pos.get('average_price')})"
                    )
                    stats['discrepancies'].append(discrepancy)
                    logger.warning(discrepancy)

            # ===== STEP 4: Cross-Check Orders =====
            logger.info("\n[4/5] Cross-checking orders...")
            logger.info("-" * 80)

            # Create mapping of Zerodha orders by order_id
            zerodha_order_map = {
                order['order_id']: order
                for order in zerodha_bot_orders
            }

            for db_order in db_open_orders:
                stats['orders_checked'] += 1
                order_id = db_order.order_id
                zerodha_order = zerodha_order_map.get(order_id)

                if not zerodha_order:
                    discrepancy = (
                        f"⚠️ Order {order_id} ({db_order.script}) exists in DB but NOT on Zerodha"
                    )
                    stats['discrepancies'].append(discrepancy)
                    logger.warning(discrepancy)
                else:
                    # Check status match
                    zerodha_status = zerodha_order.get('status', '').upper()
                    db_status = db_order.status.value

                    # Map Zerodha status to our enum
                    status_mapping = {
                        'COMPLETE': 'FILLED',
                        'OPEN': 'PENDING',
                        'TRIGGER PENDING': 'PENDING',
                        'CANCELLED': 'CANCELLED',
                        'REJECTED': 'REJECTED'
                    }

                    expected_status = status_mapping.get(zerodha_status, zerodha_status)

                    if expected_status != db_status:
                        discrepancy = (
                            f"⚠️ Order {order_id} status mismatch: "
                            f"DB={db_status}, Zerodha={zerodha_status}"
                        )
                        stats['discrepancies'].append(discrepancy)
                        logger.warning(discrepancy)

                        # Update status if needed
                        if expected_status == 'FILLED' and db_status == 'PENDING':
                            db_order.status = OrderStatus.FILLED
                            db_order.filled_qty = zerodha_order.get('filled_quantity', 0)
                            db_order.fill_price = zerodha_order.get('average_price')
                            db_order.filled_at = datetime.now()
                            stats['orders_synced'] += 1
                            logger.info(f"  ✓ Updated order {order_id} to FILLED")
                        elif expected_status == 'CANCELLED':
                            db_order.status = OrderStatus.CANCELLED
                            stats['orders_synced'] += 1
                            logger.info(f"  ✓ Updated order {order_id} to CANCELLED")
                        elif expected_status == 'REJECTED':
                            db_order.status = OrderStatus.REJECTED
                            db_order.rejection_reason = zerodha_order.get('status_message', 'Unknown')
                            stats['orders_synced'] += 1
                            logger.info(f"  ✓ Updated order {order_id} to REJECTED")
                    else:
                        logger.info(f"  ✓ Order {order_id} ({db_order.script}): Status matches ({db_status})")

            # ===== STEP 5: Sync GTT Orders =====
            logger.info("\n[5/5] Syncing GTT orders...")
            logger.info("-" * 80)

            # Create mapping of active GTTs by symbol
            gtt_map = {}
            for gtt in zerodha_active_gtts:
                symbol = gtt.get('condition', {}).get('tradingsymbol', '').upper()
                trigger_id = gtt.get('id')
                if symbol and trigger_id:
                    gtt_map[symbol] = str(trigger_id)

            for db_pos in db_open_positions:
                stats['gtts_checked'] += 1
                script = db_pos.script.upper()
                zerodha_gtt_id = gtt_map.get(script)
                db_gtt_id = db_pos.current_gtt_id

                if zerodha_gtt_id:
                    if db_gtt_id != zerodha_gtt_id:
                        logger.info(f"  ✓ Updating GTT for {db_pos.script}: {db_gtt_id} → {zerodha_gtt_id}")
                        db_pos.current_gtt_id = zerodha_gtt_id
                        stats['gtts_synced'] += 1
                    else:
                        logger.info(f"  ✓ {db_pos.script}: GTT ID matches ({zerodha_gtt_id})")
                else:
                    if db_gtt_id:
                        discrepancy = (
                            f"⚠️ {db_pos.script} has GTT in DB ({db_gtt_id}) but NO active GTT on Zerodha"
                        )
                        stats['discrepancies'].append(discrepancy)
                        logger.warning(discrepancy)

            # Commit all changes
            if stats['orders_synced'] > 0 or stats['gtts_synced'] > 0:
                session.commit()
                logger.info(f"\n✅ Database updated: {stats['orders_synced']} orders, {stats['gtts_synced']} GTTs")
            else:
                logger.info("\n✓ No database updates needed")

            # ===== SUMMARY =====
            logger.info("\n" + "=" * 80)
            logger.info("VERIFICATION SUMMARY")
            logger.info("=" * 80)
            logger.info(f"Positions checked:  {stats['positions_checked']}")
            logger.info(f"Orders checked:     {stats['orders_checked']}")
            logger.info(f"GTTs checked:       {stats['gtts_checked']}")
            logger.info(f"Orders synced:      {stats['orders_synced']}")
            logger.info(f"GTTs synced:        {stats['gtts_synced']}")
            logger.info(f"Discrepancies:      {len(stats['discrepancies'])}")

            if stats['discrepancies']:
                logger.info("\n⚠️ DISCREPANCIES FOUND:")
                logger.info("-" * 80)
                for disc in stats['discrepancies']:
                    logger.info(disc)
            else:
                logger.info("\n✅ No discrepancies found - database is in sync!")

            logger.info("=" * 80)

            return stats

        finally:
            session.close()

    except Exception as e:
        logger.error(f"Verification failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    # Setup logging
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )
    logger.add(
        "logs/db_verify_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG"
    )

    try:
        stats = verify_and_sync_database()

        # Exit with appropriate code
        if len(stats['discrepancies']) == 0:
            logger.info("\n✅ SUCCESS: Database is fully synchronized")
            sys.exit(0)
        else:
            logger.warning(f"\n⚠️ WARNING: {len(stats['discrepancies'])} discrepancies found")
            sys.exit(1)

    except Exception as e:
        logger.error(f"\n❌ FAILED: {e}")
        sys.exit(1)

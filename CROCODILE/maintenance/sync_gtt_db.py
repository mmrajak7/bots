"""
Sync GTT Database - Update current_gtt_id in trading.db
Fetches active GTT orders from Zerodha and updates OpenPosition records
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.api.kite_trade_client import KiteTradeClient
from src.models.database import OpenPosition, PositionStatus, get_engine
from src.utils.config_manager import config


def sync_gtt_with_database():
    """
    Fetch all GTT orders from Zerodha and update database

    Returns:
        Dict with sync statistics
    """
    logger.info("=" * 70)
    logger.info("GTT Database Sync - Starting")
    logger.info("=" * 70)

    try:
        # Initialize Kite client
        logger.info("Initializing Kite Trade client...")
        kite = KiteTradeClient()

        # Fetch all GTT orders from Zerodha
        logger.info("Fetching GTT orders from Zerodha...")
        gtt_orders = kite.get_gtt_orders()

        logger.info(f"Found {len(gtt_orders)} active GTT orders on Zerodha")

        if not gtt_orders:
            logger.warning("No GTT orders found on Zerodha!")
            return {
                'total_gtt_orders': 0,
                'open_positions': 0,
                'matched': 0,
                'updated': 0,
                'unmatched_gtts': 0,
                'unmatched_positions': 0
            }

        # Display GTT orders for debugging
        logger.info("\nActive GTT Orders from Zerodha:")
        logger.info("-" * 70)
        for gtt in gtt_orders:
            trigger_id = gtt.get('id')
            symbol = gtt.get('condition', {}).get('tradingsymbol', 'UNKNOWN')
            trigger_price = gtt.get('condition', {}).get('trigger_values', [None])[0]
            status = gtt.get('status', 'UNKNOWN')
            logger.info(f"GTT ID: {trigger_id} | Symbol: {symbol} | Trigger: {trigger_price} | Status: {status}")
        logger.info("-" * 70)

        # Query all open positions from database
        logger.info("\nQuerying open positions from database...")
        engine = get_engine()
        Session = sessionmaker(bind=engine)
        session = Session()

        try:
            open_positions = session.query(OpenPosition).filter(
                OpenPosition.status == PositionStatus.OPEN
            ).all()

            logger.info(f"Found {len(open_positions)} open positions in database")

            if not open_positions:
                logger.warning("No open positions found in database!")
                return {
                    'total_gtt_orders': len(gtt_orders),
                    'open_positions': 0,
                    'matched': 0,
                    'updated': 0,
                    'unmatched_gtts': len(gtt_orders),
                    'unmatched_positions': 0
                }

            # Display open positions
            logger.info("\nOpen Positions in Database:")
            logger.info("-" * 70)
            for pos in open_positions:
                logger.info(
                    f"Position ID: {pos.id} | Script: {pos.script} | "
                    f"Current GTT: {pos.current_gtt_id} | Entry: {pos.entry_price} | SL: {pos.current_sl}"
                )
            logger.info("-" * 70)

            # Create a mapping of tradingsymbol -> GTT ID
            gtt_map = {}
            for gtt in gtt_orders:
                # Only include active GTTs
                if gtt.get('status') == 'active':
                    symbol = gtt.get('condition', {}).get('tradingsymbol', '').upper()
                    trigger_id = gtt.get('id')
                    if symbol and trigger_id:
                        gtt_map[symbol] = trigger_id

            logger.info(f"\nBuilt GTT mapping for {len(gtt_map)} active GTTs")

            # Match and update positions
            matched = 0
            updated = 0
            unmatched_positions = []

            logger.info("\nMatching and updating positions...")
            logger.info("-" * 70)

            for pos in open_positions:
                script_upper = pos.script.upper()

                # Try to find matching GTT
                gtt_id = gtt_map.get(script_upper)

                if gtt_id:
                    matched += 1
                    old_gtt_id = pos.current_gtt_id

                    # Update if different
                    if old_gtt_id != str(gtt_id):
                        pos.current_gtt_id = str(gtt_id)
                        updated += 1
                        logger.info(
                            f"✓ Updated {pos.script}: GTT ID {old_gtt_id} → {gtt_id}"
                        )
                    else:
                        logger.info(
                            f"✓ {pos.script}: GTT ID already correct ({gtt_id})"
                        )
                else:
                    unmatched_positions.append(pos.script)
                    logger.warning(
                        f"✗ {pos.script}: No matching GTT found on Zerodha!"
                    )

            # Commit changes
            if updated > 0:
                session.commit()
                logger.info(f"\n✅ Database updated: {updated} position(s) updated")
            else:
                logger.info("\n✓ No updates needed - all GTT IDs are already correct")

            # Find unmatched GTTs (GTTs that don't match any position)
            matched_symbols = set(pos.script.upper() for pos in open_positions if pos.script.upper() in gtt_map)
            unmatched_gtts = [
                f"{symbol} (GTT ID: {gtt_id})"
                for symbol, gtt_id in gtt_map.items()
                if symbol not in matched_symbols
            ]

            # Summary
            logger.info("\n" + "=" * 70)
            logger.info("SYNC SUMMARY")
            logger.info("=" * 70)
            logger.info(f"Total GTT orders on Zerodha: {len(gtt_orders)}")
            logger.info(f"Active GTT orders: {len(gtt_map)}")
            logger.info(f"Open positions in DB: {len(open_positions)}")
            logger.info(f"Matched positions: {matched}")
            logger.info(f"Updated positions: {updated}")
            logger.info(f"Unmatched positions: {len(unmatched_positions)}")
            logger.info(f"Unmatched GTTs: {len(unmatched_gtts)}")

            if unmatched_positions:
                logger.warning(f"\n⚠️ Positions without GTT on Zerodha:")
                for script in unmatched_positions:
                    logger.warning(f"  • {script}")

            if unmatched_gtts:
                logger.warning(f"\n⚠️ GTTs without matching position in DB:")
                for gtt_info in unmatched_gtts:
                    logger.warning(f"  • {gtt_info}")

            logger.info("=" * 70)

            return {
                'total_gtt_orders': len(gtt_orders),
                'active_gtts': len(gtt_map),
                'open_positions': len(open_positions),
                'matched': matched,
                'updated': updated,
                'unmatched_gtts': len(unmatched_gtts),
                'unmatched_positions': len(unmatched_positions)
            }

        finally:
            session.close()

    except Exception as e:
        logger.error(f"GTT sync failed: {e}", exc_info=True)
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
        "logs/gtt_sync_{time:YYYY-MM-DD}.log",
        rotation="1 day",
        retention="7 days",
        level="DEBUG"
    )

    try:
        stats = sync_gtt_with_database()

        # Exit with appropriate code
        if stats['matched'] == stats['open_positions'] and stats['unmatched_positions'] == 0:
            logger.info("\n✅ SUCCESS: All positions have matching GTTs")
            sys.exit(0)
        elif stats['updated'] > 0:
            logger.info(f"\n✅ SUCCESS: Database updated ({stats['updated']} changes)")
            sys.exit(0)
        else:
            logger.warning("\n⚠️ WARNING: Some positions don't have matching GTTs")
            sys.exit(1)

    except Exception as e:
        logger.error(f"\n❌ FAILED: {e}")
        sys.exit(1)

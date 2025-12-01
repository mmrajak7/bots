"""
Investigate Zerodha Positions - Detailed Analysis
"""

import sys
from pathlib import Path
import json

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from src.api.kite_trade_client import KiteTradeClient


def investigate_positions():
    """Get detailed information about actual Zerodha positions"""

    logger.info("=" * 80)
    logger.info("ZERODHA POSITIONS - Detailed Investigation")
    logger.info("=" * 80)

    try:
        # Initialize Kite client
        kite = KiteTradeClient()

        # Get positions
        logger.info("\n[1] Fetching Positions...")
        positions = kite.get_positions()

        net_positions = positions.get('net', [])
        day_positions = positions.get('day', [])

        logger.info(f"\nTotal net positions: {len(net_positions)}")
        logger.info(f"Total day positions: {len(day_positions)}")

        # Display all net positions
        logger.info("\n" + "=" * 80)
        logger.info("NET POSITIONS (All)")
        logger.info("=" * 80)

        for i, pos in enumerate(net_positions, 1):
            logger.info(f"\n[{i}] {pos.get('tradingsymbol', 'UNKNOWN')}")
            logger.info(f"    Exchange:         {pos.get('exchange')}")
            logger.info(f"    Product:          {pos.get('product')}")
            logger.info(f"    Quantity:         {pos.get('quantity')} (Overnight: {pos.get('overnight_quantity')})")
            logger.info(f"    Average Price:    {pos.get('average_price')}")
            logger.info(f"    Last Price:       {pos.get('last_price')}")
            logger.info(f"    P&L:              {pos.get('pnl')} (Unrealized: {pos.get('unrealised')})")
            logger.info(f"    Value:            {pos.get('value')}")
            logger.info(f"    Buy/Sell:         Buy={pos.get('buy_quantity')}, Sell={pos.get('sell_quantity')}")

        # Filter for non-zero positions
        logger.info("\n" + "=" * 80)
        logger.info("NON-ZERO NET POSITIONS")
        logger.info("=" * 80)

        non_zero = [p for p in net_positions if p.get('quantity', 0) != 0]
        logger.info(f"\nFound {len(non_zero)} non-zero positions")

        for i, pos in enumerate(non_zero, 1):
            qty = pos.get('quantity')
            position_type = "LONG" if qty > 0 else "SHORT"
            logger.info(
                f"[{i}] {pos.get('tradingsymbol'):<15} | "
                f"{position_type:<6} | Qty: {qty:>4} | "
                f"Avg: {pos.get('average_price'):>8.2f} | "
                f"LTP: {pos.get('last_price'):>8.2f} | "
                f"P&L: {pos.get('pnl'):>8.2f}"
            )

        # Get all orders
        logger.info("\n" + "=" * 80)
        logger.info("TODAY'S ORDERS")
        logger.info("=" * 80)

        all_orders = kite.get_all_orders()
        logger.info(f"\nTotal orders today: {len(all_orders)}")

        for i, order in enumerate(all_orders, 1):
            logger.info(
                f"[{i}] {order.get('tradingsymbol'):<15} | "
                f"{order.get('transaction_type'):<4} | "
                f"Qty: {order.get('quantity'):>4} | "
                f"Status: {order.get('status'):<12} | "
                f"Tag: {order.get('tag') or 'None':<8} | "
                f"Order ID: {order.get('order_id')}"
            )

        # Get GTT orders
        logger.info("\n" + "=" * 80)
        logger.info("GTT ORDERS")
        logger.info("=" * 80)

        gtt_orders = kite.get_gtt_orders()
        logger.info(f"\nTotal GTT orders: {len(gtt_orders)}")

        for i, gtt in enumerate(gtt_orders, 1):
            symbol = gtt.get('condition', {}).get('tradingsymbol', 'UNKNOWN')
            trigger_price = gtt.get('condition', {}).get('trigger_values', [None])[0]
            status = gtt.get('status')
            gtt_id = gtt.get('id')

            logger.info(
                f"[{i}] {symbol:<15} | "
                f"Trigger: {trigger_price:>8} | "
                f"Status: {status:<10} | "
                f"GTT ID: {gtt_id}"
            )

        # Summary
        logger.info("\n" + "=" * 80)
        logger.info("SUMMARY")
        logger.info("=" * 80)
        logger.info(f"Net positions (all):      {len(net_positions)}")
        logger.info(f"Non-zero positions:       {len(non_zero)}")
        logger.info(f"Today's orders:           {len(all_orders)}")
        logger.info(f"GTT orders:               {len(gtt_orders)}")
        logger.info("=" * 80)

    except Exception as e:
        logger.error(f"Investigation failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    # Setup logging
    logger.remove()  # Remove default handler
    logger.add(
        sys.stderr,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level="INFO"
    )

    investigate_positions()

"""Sector Concentration Filter - Prevents excessive exposure to a single sector"""

import csv
import os
from typing import Dict, Optional, Set, Tuple

from loguru import logger
from sqlalchemy.orm import Session

from src.models.database import OpenPosition, OpenOrder, OrderStatus
from src.utils.config_manager import config
from src.reporting.telegram_client import telegram


class SectorFilter:
    """
    Enforces sector concentration limits by checking open positions + pending orders.

    Uses a static CSV (data/sector_map.csv) for symbol-to-sector mapping.
    Stocks not in the file are allowed through with a Telegram alert (once per session).
    """

    def __init__(self):
        self.bot_instance_id = config.get_bot_instance_id()
        self.max_per_sector = config.get('risk_management.max_stocks_per_sector', 2)

        csv_path = config.get('signals.sector_map_file', 'data/sector_map.csv')
        # Resolve relative path from project root (same pattern as other CSV files)
        if not os.path.isabs(csv_path):
            csv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), csv_path)
        self.csv_path = csv_path

        self.sector_map: Dict[str, str] = {}
        self._alerted_symbols: Set[str] = set()  # Track alerted unknowns to avoid spam
        self._load_sector_map()

        logger.info(
            f"Sector Filter initialized: {len(self.sector_map)} stocks mapped, "
            f"max {self.max_per_sector} per sector"
        )

    def _load_sector_map(self) -> None:
        """Load symbol-to-sector mapping from CSV file."""
        if not os.path.exists(self.csv_path):
            logger.warning(f"Sector map file not found: {self.csv_path} - all stocks will be allowed")
            return

        try:
            with open(self.csv_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    symbol = row.get('Symbol', '').strip().upper()
                    sector = row.get('Sector', '').strip()
                    if symbol and sector:
                        self.sector_map[symbol] = sector
        except Exception as e:
            logger.error(f"Error loading sector map from {self.csv_path}: {e}")

    def get_sector(self, symbol: str) -> Optional[str]:
        """Get sector for a symbol. Returns None if not mapped."""
        return self.sector_map.get(symbol.upper().strip())

    def check_sector_limit(self, symbol: str, session: Session) -> Tuple[bool, str]:
        """
        Check if adding this symbol would breach the sector concentration limit.

        Counts unique stocks across open positions (OPEN) + pending orders (PENDING)
        in the same sector, filtered by bot_instance_id.

        Args:
            symbol: Stock symbol to check
            session: SQLAlchemy session

        Returns:
            (is_blocked, reason) - is_blocked=True means signal should be held for retry
        """
        # Feature disabled if max_per_sector is None or <= 0
        if not self.max_per_sector or self.max_per_sector <= 0:
            return False, ""

        sector = self.get_sector(symbol)

        if sector is None:
            # Unknown stock - allow through but alert ONCE per session to avoid spam
            if symbol not in self._alerted_symbols:
                self._alerted_symbols.add(symbol)
                logger.warning(f"Stock '{symbol}' not in sector map - allowing trade, sending alert")
                telegram.send_alert(
                    f"⚠️ <b>UNKNOWN SECTOR</b>\n\n"
                    f"<b>Stock:</b> {symbol}\n"
                    f"Not found in <code>data/sector_map.csv</code>\n\n"
                    f"Trade allowed. Please add this stock to the sector map.",
                    critical=False
                )
            else:
                logger.debug(f"Stock '{symbol}' not in sector map - already alerted, allowing trade")
            return False, ""

        # Collect unique symbols from open positions in this sector
        open_positions = (
            session.query(OpenPosition.script)
            .filter(
                OpenPosition.bot_instance_id == self.bot_instance_id,
                OpenPosition.status == 'OPEN'
            )
            .all()
        )

        # Collect unique symbols from pending orders in this sector
        pending_orders = (
            session.query(OpenOrder.script)
            .filter(
                OpenOrder.bot_instance_id == self.bot_instance_id,
                OpenOrder.status == OrderStatus.PENDING
            )
            .all()
        )

        # Deduplicate: same stock in both position AND order counts as 1 sector slot
        sector_stocks: Set[str] = set()

        for (script,) in open_positions:
            if self.get_sector(script) == sector and script != symbol:
                sector_stocks.add(script)

        for (script,) in pending_orders:
            if self.get_sector(script) == sector and script != symbol:
                sector_stocks.add(script)

        sector_count = len(sector_stocks)

        if sector_count >= self.max_per_sector:
            stocks_list = ', '.join(sorted(sector_stocks))
            reason = (
                f"Sector concentration limit: {sector} has {sector_count}/{self.max_per_sector} "
                f"slots filled ({stocks_list})"
            )
            logger.info(
                f"⏸️  Sector limit reached for {symbol} ({sector}): "
                f"{sector_count} existing - {stocks_list}"
            )
            return True, reason

        return False, ""


# Singleton instance
sector_filter = SectorFilter()

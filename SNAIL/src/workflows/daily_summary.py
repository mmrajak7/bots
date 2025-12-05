"""
SNAIL Daily Summary Workflow

End-of-day summary generation and reporting.

@file        daily_summary.py
@description Daily summary generation
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 9.4
"""

import sys
from datetime import datetime, date, timedelta
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from loguru import logger

from src.api.kite_client import SNAILKiteClient, get_kite_client
from src.api.telegram_alerts import TelegramAlerts, get_telegram
from src.utils.db import (
    get_db_session,
    get_active_position,
    get_position_legs,
    Position,
    PnLSnapshot
)
from src.utils.calculations import calculate_position_pnl
from src.utils.config import get_trading_config, load_config


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class DailySummaryData:
    """
    Daily summary data.

    Attributes:
        date: Summary date
        has_position: Whether position was active
        position_id: Position ID if active
        starting_pnl: P&L at market open
        ending_pnl: P&L at market close
        day_pnl_change: Change in P&L during day
        high_pnl: Highest P&L of the day
        low_pnl: Lowest P&L of the day
        nifty_open: NIFTY opening price
        nifty_close: NIFTY closing price
        nifty_change: NIFTY change
        vix_open: VIX at open
        vix_close: VIX at close
        orders_executed: Number of orders
        alerts_sent: Number of alerts
        trades_today: List of trades
    """
    date: date
    has_position: bool = False
    position_id: Optional[int] = None
    starting_pnl: float = 0.0
    ending_pnl: float = 0.0
    day_pnl_change: float = 0.0
    high_pnl: float = 0.0
    low_pnl: float = 0.0
    nifty_open: float = 0.0
    nifty_close: float = 0.0
    nifty_change: float = 0.0
    vix_open: float = 0.0
    vix_close: float = 0.0
    orders_executed: int = 0
    alerts_sent: int = 0
    trades_today: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class WeeklySummaryData:
    """
    Weekly summary data.

    Attributes:
        week_start: Start of week
        week_end: End of week
        total_pnl: Total P&L for week
        winning_days: Number of profitable days
        losing_days: Number of losing days
        trades_count: Total trades
        largest_win: Largest winning trade
        largest_loss: Largest losing trade
    """
    week_start: date
    week_end: date
    total_pnl: float = 0.0
    winning_days: int = 0
    losing_days: int = 0
    trades_count: int = 0
    largest_win: float = 0.0
    largest_loss: float = 0.0


# =============================================================================
# DAILY SUMMARY CLASS
# =============================================================================

class DailySummary:
    """
    Generates daily trading summaries.

    Features:
    - Position P&L tracking
    - Market data summary
    - Trade history
    - Weekly rollup
    - Telegram reporting

    Attributes:
        kite: Kite client
        telegram: Telegram alerts
        config: Configuration
    """

    def __init__(
        self,
        kite: Optional[SNAILKiteClient] = None,
        telegram: Optional[TelegramAlerts] = None,
        config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize daily summary.

        Args:
            kite: Kite client
            telegram: Telegram alerts
            config: Configuration
        """
        self.config = config or load_config()
        self.trading_config = get_trading_config()

        self.kite = kite or get_kite_client(self.config)
        self.telegram = telegram or get_telegram()

        logger.info("Daily summary initialized")

    # =========================================================================
    # DATA COLLECTION
    # =========================================================================

    def _get_pnl_snapshots_for_date(self, target_date: date) -> List[Dict[str, Any]]:
        """Get P&L snapshots for a specific date."""
        try:
            with get_db_session() as conn:
                cursor = conn.cursor()

                start = datetime.combine(target_date, datetime.min.time())
                end = datetime.combine(target_date, datetime.max.time())

                cursor.execute('''
                    SELECT timestamp, nifty_spot, india_vix, unrealized_pnl,
                           straddle_value, wing_value
                    FROM pnl_snapshots
                    WHERE timestamp BETWEEN ? AND ?
                    ORDER BY timestamp
                ''', (start.isoformat(), end.isoformat()))

                return [
                    {
                        'timestamp': row[0],
                        'nifty_spot': row[1],
                        'india_vix': row[2],
                        'unrealized_pnl': row[3],
                        'straddle_value': row[4],
                        'wing_value': row[5]
                    }
                    for row in cursor.fetchall()
                ]

        except Exception as e:
            logger.error(f"Error getting PnL snapshots: {e}")
            return []

    def _get_orders_for_date(self, target_date: date) -> List[Dict[str, Any]]:
        """Get orders executed on a specific date."""
        try:
            with get_db_session() as conn:
                cursor = conn.cursor()

                start = datetime.combine(target_date, datetime.min.time())
                end = datetime.combine(target_date, datetime.max.time())

                cursor.execute('''
                    SELECT order_id, tradingsymbol, transaction_type, quantity,
                           price, fill_price, status, created_at
                    FROM orders
                    WHERE created_at BETWEEN ? AND ?
                    ORDER BY created_at
                ''', (start.isoformat(), end.isoformat()))

                return [
                    {
                        'order_id': row[0],
                        'tradingsymbol': row[1],
                        'transaction_type': row[2],
                        'quantity': row[3],
                        'price': row[4],
                        'fill_price': row[5],
                        'status': row[6],
                        'created_at': row[7]
                    }
                    for row in cursor.fetchall()
                ]

        except Exception as e:
            logger.error(f"Error getting orders: {e}")
            return []

    def _get_positions_for_date(self, target_date: date) -> List[Dict[str, Any]]:
        """Get positions active on a specific date."""
        try:
            with get_db_session() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    SELECT id, strategy, status, entry_time, exit_time,
                           entry_credit, realized_pnl, exit_reason
                    FROM positions
                    WHERE DATE(entry_time) <= ? AND
                          (exit_time IS NULL OR DATE(exit_time) >= ?)
                    ORDER BY entry_time
                ''', (target_date.isoformat(), target_date.isoformat()))

                return [
                    {
                        'id': row[0],
                        'strategy': row[1],
                        'status': row[2],
                        'entry_time': row[3],
                        'exit_time': row[4],
                        'entry_credit': row[5],
                        'realized_pnl': row[6],
                        'exit_reason': row[7]
                    }
                    for row in cursor.fetchall()
                ]

        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []

    # =========================================================================
    # SUMMARY GENERATION
    # =========================================================================

    def generate_daily_summary(
        self,
        target_date: Optional[date] = None
    ) -> DailySummaryData:
        """
        Generate daily summary.

        Args:
            target_date: Date to summarize (default today)

        Returns:
            DailySummaryData
        """
        if target_date is None:
            target_date = date.today()

        logger.info(f"Generating daily summary for {target_date}")

        summary = DailySummaryData(date=target_date)

        # Get P&L snapshots
        snapshots = self._get_pnl_snapshots_for_date(target_date)

        if snapshots:
            summary.has_position = True

            # Find first and last snapshots
            first = snapshots[0]
            last = snapshots[-1]

            summary.starting_pnl = first['unrealized_pnl']
            summary.ending_pnl = last['unrealized_pnl']
            summary.day_pnl_change = summary.ending_pnl - summary.starting_pnl

            # Find high and low
            pnls = [s['unrealized_pnl'] for s in snapshots]
            summary.high_pnl = max(pnls)
            summary.low_pnl = min(pnls)

            # Market data
            summary.nifty_open = first['nifty_spot']
            summary.nifty_close = last['nifty_spot']
            summary.nifty_change = summary.nifty_close - summary.nifty_open
            summary.vix_open = first['india_vix']
            summary.vix_close = last['india_vix']

        # Get orders
        orders = self._get_orders_for_date(target_date)
        summary.orders_executed = len(orders)

        # Get positions and build trades
        positions = self._get_positions_for_date(target_date)
        for pos in positions:
            summary.position_id = pos['id']

            if pos['exit_time'] and date.fromisoformat(pos['exit_time'].split('T')[0]) == target_date:
                summary.trades_today.append({
                    'type': 'exit',
                    'position_id': pos['id'],
                    'reason': pos['exit_reason'],
                    'realized_pnl': pos['realized_pnl']
                })

            if pos['entry_time'] and date.fromisoformat(pos['entry_time'].split('T')[0]) == target_date:
                summary.trades_today.append({
                    'type': 'entry',
                    'position_id': pos['id'],
                    'entry_credit': pos['entry_credit']
                })

        return summary

    def generate_weekly_summary(
        self,
        week_end: Optional[date] = None
    ) -> WeeklySummaryData:
        """
        Generate weekly summary.

        Args:
            week_end: End of week (default today)

        Returns:
            WeeklySummaryData
        """
        if week_end is None:
            week_end = date.today()

        # Get week start (Monday)
        week_start = week_end - timedelta(days=week_end.weekday())

        logger.info(f"Generating weekly summary for {week_start} to {week_end}")

        summary = WeeklySummaryData(
            week_start=week_start,
            week_end=week_end
        )

        # Aggregate daily summaries
        current = week_start
        daily_pnls = []

        while current <= week_end:
            daily = self.generate_daily_summary(current)

            if daily.has_position or daily.trades_today:
                if daily.day_pnl_change > 0:
                    summary.winning_days += 1
                elif daily.day_pnl_change < 0:
                    summary.losing_days += 1

                daily_pnls.append(daily.day_pnl_change)
                summary.trades_count += len(daily.trades_today)

            current += timedelta(days=1)

        if daily_pnls:
            summary.total_pnl = sum(daily_pnls)
            summary.largest_win = max(daily_pnls) if any(p > 0 for p in daily_pnls) else 0
            summary.largest_loss = min(daily_pnls) if any(p < 0 for p in daily_pnls) else 0

        return summary

    # =========================================================================
    # REPORTING
    # =========================================================================

    def send_daily_summary(self, summary: Optional[DailySummaryData] = None) -> None:
        """
        Send daily summary via Telegram.

        Args:
            summary: Summary data (generated if None)
        """
        if summary is None:
            summary = self.generate_daily_summary()

        # Build position status string
        position_status = None
        if summary.has_position:
            position_status = f"P&L: {summary.ending_pnl:+,.2f} (Day: {summary.day_pnl_change:+,.2f})"

        # Send using correct signature
        self.telegram.send_daily_summary(
            date_str=summary.date.strftime('%Y-%m-%d'),
            total_pnl=summary.ending_pnl,
            trades=len(summary.trades_today),
            has_position=summary.has_position,
            position_status=position_status
        )

    def send_weekly_summary(self, summary: Optional[WeeklySummaryData] = None) -> None:
        """
        Send weekly summary via Telegram.

        Args:
            summary: Summary data (generated if None)
        """
        if summary is None:
            summary = self.generate_weekly_summary()

        # Build weekly message
        message = (
            f"📊 *Weekly Summary*\n"
            f"Week: {summary.week_start} to {summary.week_end}\n\n"
            f"💰 Total P&L: ₹{summary.total_pnl:,.2f}\n"
            f"📈 Winning days: {summary.winning_days}\n"
            f"📉 Losing days: {summary.losing_days}\n"
            f"🔄 Trades: {summary.trades_count}\n\n"
            f"Best day: ₹{summary.largest_win:,.2f}\n"
            f"Worst day: ₹{summary.largest_loss:,.2f}"
        )

        self.telegram.send(message)


# =============================================================================
# STANDALONE EXECUTION
# =============================================================================

def run_daily_summary(send_telegram: bool = True) -> DailySummaryData:
    """
    Run daily summary as standalone function.

    Args:
        send_telegram: Whether to send Telegram notification

    Returns:
        DailySummaryData
    """
    summary_gen = DailySummary()
    summary = summary_gen.generate_daily_summary()

    if send_telegram:
        summary_gen.send_daily_summary(summary)

    return summary


def run_weekly_summary(send_telegram: bool = True) -> WeeklySummaryData:
    """
    Run weekly summary as standalone function.

    Args:
        send_telegram: Whether to send Telegram notification

    Returns:
        WeeklySummaryData
    """
    summary_gen = DailySummary()
    summary = summary_gen.generate_weekly_summary()

    if send_telegram:
        summary_gen.send_weekly_summary(summary)

    return summary


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':
    from dotenv import load_dotenv

    load_dotenv()

    logger.remove()
    logger.add(sys.stderr, level="INFO")

    print("\n" + "=" * 60)
    print("SNAIL Daily Summary")
    print("=" * 60)

    try:
        summary_gen = DailySummary()

        # Generate daily summary
        print("\n[1] Generating daily summary...")
        daily = summary_gen.generate_daily_summary()

        print(f"\n    Date: {daily.date}")
        print(f"    Has position: {daily.has_position}")
        if daily.has_position:
            print(f"    Position ID: {daily.position_id}")
            print(f"    Starting P&L: ₹{daily.starting_pnl:,.2f}")
            print(f"    Ending P&L: ₹{daily.ending_pnl:,.2f}")
            print(f"    Day change: ₹{daily.day_pnl_change:,.2f}")
            print(f"    High/Low: ₹{daily.high_pnl:,.2f} / ₹{daily.low_pnl:,.2f}")
            print(f"    NIFTY change: {daily.nifty_change:+.2f}")

        print(f"    Orders: {daily.orders_executed}")
        print(f"    Trades: {len(daily.trades_today)}")

        # Generate weekly summary
        print("\n[2] Generating weekly summary...")
        weekly = summary_gen.generate_weekly_summary()

        print(f"\n    Week: {weekly.week_start} to {weekly.week_end}")
        print(f"    Total P&L: ₹{weekly.total_pnl:,.2f}")
        print(f"    Winning days: {weekly.winning_days}")
        print(f"    Losing days: {weekly.losing_days}")
        print(f"    Trades: {weekly.trades_count}")

        print(f"\n{'='*60}")
        print("Daily summary complete!")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

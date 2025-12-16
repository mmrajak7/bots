"""
Database module for Z-Score Trading Bot
Tracks orders and positions separately from other bots on same Zerodha account
"""

import os
import sqlite3
from datetime import datetime, date
from typing import Optional, List, Dict
from dataclasses import dataclass
import logging

BOT_ID = "ZSCORE_V1"  # Unique identifier for this bot


@dataclass
class Order:
    id: int = 0
    bot_id: str = BOT_ID
    order_id: str = ""  # Kite order ID
    order_type: str = ""  # ENTRY or EXIT
    symbol: str = ""
    exchange: str = "NFO"
    qty: int = 0
    side: str = ""  # BUY or SELL
    order_status: str = ""  # PENDING, COMPLETE, REJECTED, CANCELLED
    expected_price: float = 0.0
    fill_price: float = 0.0
    slippage: float = 0.0
    created_at: str = ""
    updated_at: str = ""
    error_message: str = ""
    paper_trade: bool = True


@dataclass
class Position:
    id: int = 0
    bot_id: str = BOT_ID
    trade_date: str = ""
    trade_group_id: str = ""  # Links CE+PE legs of a straddle
    symbol: str = ""
    instrument_token: int = 0
    qty: int = 0
    lot_size: int = 0
    entry_order_id: str = ""
    exit_order_id: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    entry_time: str = ""
    exit_time: str = ""
    entry_spot: float = 0.0
    exit_spot: float = 0.0
    entry_z_score: float = 0.0
    entry_basis: float = 0.0
    fut_used: str = ""
    stop_loss: float = 0.0
    target: float = 0.0
    exit_deadline: str = ""
    exit_reason: str = ""
    pnl: float = 0.0
    pnl_pct: float = 0.0
    status: str = "OPEN"  # OPEN, CLOSED, ERROR
    paper_trade: bool = True


@dataclass
class DailySummary:
    id: int = 0
    bot_id: str = BOT_ID
    trade_date: str = ""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    charges: float = 0.0  # Estimated
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    paper_trade: bool = True


class TradingDB:
    """SQLite database for tracking orders and positions"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        # Create directory if path has a directory component
        db_dir = os.path.dirname(db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        """Initialize database tables"""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Orders table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                order_id TEXT,
                order_type TEXT,
                symbol TEXT,
                exchange TEXT,
                qty INTEGER,
                side TEXT,
                order_status TEXT,
                expected_price REAL,
                fill_price REAL,
                slippage REAL,
                created_at TEXT,
                updated_at TEXT,
                error_message TEXT,
                paper_trade INTEGER
            )
        """)

        # Positions table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                trade_date TEXT,
                trade_group_id TEXT,
                symbol TEXT,
                instrument_token INTEGER,
                qty INTEGER,
                lot_size INTEGER,
                entry_order_id TEXT,
                exit_order_id TEXT,
                entry_price REAL,
                exit_price REAL,
                entry_time TEXT,
                exit_time TEXT,
                entry_spot REAL,
                exit_spot REAL,
                entry_z_score REAL,
                entry_basis REAL,
                fut_used TEXT,
                stop_loss REAL,
                target REAL,
                exit_deadline TEXT,
                exit_reason TEXT,
                pnl REAL,
                pnl_pct REAL,
                status TEXT,
                paper_trade INTEGER
            )
        """)

        # Migration: Add trade_group_id column if not exists (for existing DBs)
        try:
            cursor.execute("ALTER TABLE positions ADD COLUMN trade_group_id TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Daily summary table - UNIQUE on (bot_id, trade_date) for multi-bot support
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                total_trades INTEGER,
                wins INTEGER,
                losses INTEGER,
                gross_pnl REAL,
                charges REAL,
                net_pnl REAL,
                max_drawdown REAL,
                paper_trade INTEGER,
                UNIQUE(bot_id, trade_date)
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_bot_id ON orders(bot_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_orders_order_id ON orders(order_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_bot_id ON positions(bot_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_date ON positions(trade_date)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_group ON positions(trade_group_id)")

        conn.commit()
        conn.close()
        logging.info(f"Database initialized: {self.db_path}")

    # ==================== ORDER METHODS ====================

    def create_order(self, order: Order) -> int:
        """Create a new order record, return ID"""
        conn = self._get_conn()
        cursor = conn.cursor()

        now = datetime.now().isoformat()
        order.created_at = now
        order.updated_at = now

        cursor.execute("""
            INSERT INTO orders (bot_id, order_id, order_type, symbol, exchange, qty, side,
                               order_status, expected_price, fill_price, slippage,
                               created_at, updated_at, error_message, paper_trade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (order.bot_id, order.order_id, order.order_type, order.symbol, order.exchange,
              order.qty, order.side, order.order_status, order.expected_price, order.fill_price,
              order.slippage, order.created_at, order.updated_at, order.error_message,
              1 if order.paper_trade else 0))

        order_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return order_id

    def update_order(self, order_id: str, fill_price: float, status: str, error: str = ""):
        """Update order after execution"""
        conn = self._get_conn()
        cursor = conn.cursor()

        # First get expected_price to calculate slippage
        cursor.execute("SELECT expected_price FROM orders WHERE order_id = ? AND bot_id = ?",
                      (order_id, BOT_ID))
        row = cursor.fetchone()
        expected_price = row['expected_price'] if row else 0
        slippage = fill_price - expected_price if fill_price > 0 else 0

        cursor.execute("""
            UPDATE orders SET fill_price = ?, order_status = ?, error_message = ?,
                             slippage = ?, updated_at = ?
            WHERE order_id = ? AND bot_id = ?
        """, (fill_price, status, error, slippage, datetime.now().isoformat(), order_id, BOT_ID))

        conn.commit()
        conn.close()

    def get_order(self, order_id: str) -> Optional[Order]:
        """Get order by Kite order ID"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("SELECT * FROM orders WHERE order_id = ? AND bot_id = ?",
                      (order_id, BOT_ID))
        row = cursor.fetchone()
        conn.close()

        if row:
            data = dict(row)
            data['paper_trade'] = bool(data.get('paper_trade', 0))
            return Order(**data)
        return None

    # ==================== POSITION METHODS ====================

    def create_position(self, pos: Position) -> int:
        """Create a new position record"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            INSERT INTO positions (bot_id, trade_date, trade_group_id, symbol, instrument_token, qty, lot_size,
                                  entry_order_id, exit_order_id, entry_price, exit_price,
                                  entry_time, exit_time, entry_spot, exit_spot,
                                  entry_z_score, entry_basis, fut_used, stop_loss, target,
                                  exit_deadline, exit_reason, pnl, pnl_pct, status, paper_trade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (pos.bot_id, pos.trade_date, pos.trade_group_id, pos.symbol, pos.instrument_token, pos.qty, pos.lot_size,
              pos.entry_order_id, pos.exit_order_id, pos.entry_price, pos.exit_price,
              pos.entry_time, pos.exit_time, pos.entry_spot, pos.exit_spot,
              pos.entry_z_score, pos.entry_basis, pos.fut_used, pos.stop_loss, pos.target,
              pos.exit_deadline, pos.exit_reason, pos.pnl, pos.pnl_pct, pos.status,
              1 if pos.paper_trade else 0))

        pos_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return pos_id

    def get_open_position(self) -> Optional[Position]:
        """Get current open position for this bot"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM positions
            WHERE bot_id = ? AND status = 'OPEN'
            ORDER BY id DESC LIMIT 1
        """, (BOT_ID,))

        row = cursor.fetchone()
        conn.close()

        if row:
            data = dict(row)
            data['paper_trade'] = bool(data.get('paper_trade', 0))
            # Handle missing trade_group_id for legacy records
            if 'trade_group_id' not in data:
                data['trade_group_id'] = ''
            return Position(**data)
        return None

    def get_all_open_positions(self) -> List[Position]:
        """Get all open positions for this bot (for straddle: CE and PE)"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM positions
            WHERE bot_id = ? AND status = 'OPEN'
            ORDER BY id ASC
        """, (BOT_ID,))

        rows = cursor.fetchall()
        conn.close()

        positions = []
        for row in rows:
            data = dict(row)
            data['paper_trade'] = bool(data.get('paper_trade', 0))
            # Handle missing trade_group_id for legacy records
            if 'trade_group_id' not in data:
                data['trade_group_id'] = ''
            positions.append(Position(**data))
        return positions

    def get_positions_by_group(self, trade_group_id: str) -> List[Position]:
        """Get all positions with the same trade_group_id (both legs of straddle)"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM positions
            WHERE bot_id = ? AND trade_group_id = ?
            ORDER BY id ASC
        """, (BOT_ID, trade_group_id))

        rows = cursor.fetchall()
        conn.close()

        positions = []
        for row in rows:
            data = dict(row)
            data['paper_trade'] = bool(data.get('paper_trade', 0))
            if 'trade_group_id' not in data:
                data['trade_group_id'] = ''
            positions.append(Position(**data))
        return positions

    def close_position(self, position_id: int, exit_price: float, exit_spot: float,
                       exit_reason: str, exit_order_id: str):
        """Close a position with exit details"""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Get entry price to calculate P&L
        cursor.execute("SELECT entry_price, qty FROM positions WHERE id = ?", (position_id,))
        row = cursor.fetchone()

        if row:
            entry_price = row['entry_price']
            qty = row['qty']
            pnl = (exit_price - entry_price) * qty
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0

            cursor.execute("""
                UPDATE positions SET
                    exit_order_id = ?,
                    exit_price = ?,
                    exit_time = ?,
                    exit_spot = ?,
                    exit_reason = ?,
                    pnl = ?,
                    pnl_pct = ?,
                    status = 'CLOSED'
                WHERE id = ?
            """, (exit_order_id, exit_price, datetime.now().isoformat(), exit_spot,
                  exit_reason, pnl, pnl_pct, position_id))

        conn.commit()
        conn.close()

    def mark_position_error(self, position_id: int, error_reason: str):
        """Mark a position as having an error (e.g., exit order failed)"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            UPDATE positions SET
                exit_reason = ?,
                status = 'ERROR'
            WHERE id = ?
        """, (error_reason, position_id))

        conn.commit()
        conn.close()

    def get_today_positions(self) -> List[Position]:
        """Get all positions for today"""
        conn = self._get_conn()
        cursor = conn.cursor()

        today = date.today().isoformat()
        cursor.execute("""
            SELECT * FROM positions
            WHERE bot_id = ? AND trade_date = ?
            ORDER BY id
        """, (BOT_ID, today))

        rows = cursor.fetchall()
        conn.close()

        positions = []
        for row in rows:
            data = dict(row)
            data['paper_trade'] = bool(data.get('paper_trade', 0))
            positions.append(Position(**data))
        return positions

    def get_today_stats(self) -> Dict:
        """Get today's trading statistics.

        Counts straddles (linked by trade_group_id) as 1 trade, not 2.
        """
        conn = self._get_conn()
        cursor = conn.cursor()

        today = date.today().isoformat()

        # Count unique trade groups (straddle = 1 trade, not 2)
        # Empty trade_group_id counts as individual trades
        cursor.execute("""
            SELECT
                COUNT(DISTINCT CASE
                    WHEN trade_group_id IS NOT NULL AND trade_group_id != ''
                    THEN trade_group_id
                    ELSE id  -- Each position without group is a separate trade
                END) as total_trades,
                COALESCE(SUM(pnl), 0) as gross_pnl,
                COUNT(CASE WHEN status = 'OPEN' THEN 1 END) as open_positions
            FROM positions
            WHERE bot_id = ? AND trade_date = ?
        """, (BOT_ID, today))

        row = cursor.fetchone()

        # Calculate wins/losses by trade group
        cursor.execute("""
            SELECT
                SUM(CASE WHEN group_pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN group_pnl <= 0 AND has_closed > 0 THEN 1 ELSE 0 END) as losses
            FROM (
                SELECT
                    CASE
                        WHEN trade_group_id IS NOT NULL AND trade_group_id != ''
                        THEN trade_group_id
                        ELSE CAST(id AS TEXT)
                    END as trade_group,
                    SUM(pnl) as group_pnl,
                    SUM(CASE WHEN status = 'CLOSED' THEN 1 ELSE 0 END) as has_closed
                FROM positions
                WHERE bot_id = ? AND trade_date = ?
                GROUP BY trade_group
            )
        """, (BOT_ID, today))

        wl_row = cursor.fetchone()
        conn.close()

        result = {'total_trades': 0, 'wins': 0, 'losses': 0, 'gross_pnl': 0, 'open_positions': 0}
        if row:
            result.update(dict(row))
        if wl_row:
            result['wins'] = wl_row['wins'] or 0
            result['losses'] = wl_row['losses'] or 0

        return result

    # ==================== DAILY SUMMARY ====================

    def save_daily_summary(self, summary: DailySummary):
        """Save or update daily summary for this bot"""
        conn = self._get_conn()
        cursor = conn.cursor()

        # Use INSERT ... ON CONFLICT for proper upsert with composite unique key
        cursor.execute("""
            INSERT INTO daily_summary
            (bot_id, trade_date, total_trades, wins, losses, gross_pnl, charges, net_pnl,
             max_drawdown, paper_trade)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bot_id, trade_date) DO UPDATE SET
                total_trades = excluded.total_trades,
                wins = excluded.wins,
                losses = excluded.losses,
                gross_pnl = excluded.gross_pnl,
                charges = excluded.charges,
                net_pnl = excluded.net_pnl,
                max_drawdown = excluded.max_drawdown,
                paper_trade = excluded.paper_trade
        """, (summary.bot_id, summary.trade_date, summary.total_trades, summary.wins,
              summary.losses, summary.gross_pnl, summary.charges, summary.net_pnl,
              summary.max_drawdown, 1 if summary.paper_trade else 0))

        conn.commit()
        conn.close()

    def get_weekly_summary(self) -> List[DailySummary]:
        """Get last 7 days summary"""
        conn = self._get_conn()
        cursor = conn.cursor()

        cursor.execute("""
            SELECT * FROM daily_summary
            WHERE bot_id = ?
            ORDER BY trade_date DESC
            LIMIT 7
        """, (BOT_ID,))

        rows = cursor.fetchall()
        conn.close()

        summaries = []
        for row in rows:
            data = dict(row)
            data['paper_trade'] = bool(data.get('paper_trade', 0))
            summaries.append(DailySummary(**data))
        return summaries

"""
NEO Trade Terminal - Trade Logger

Logs all trades to file for audit and analysis.
"""

import os
import json
import csv
import threading
from datetime import datetime
from typing import Dict, Any, Optional, List
import logging

logger = logging.getLogger(__name__)


class TradeLogger:
    """Logs all trading activity for audit and analysis."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.log_dir = config.get('paths', {}).get('trade_log', 'logs')
        os.makedirs(self.log_dir, exist_ok=True)

        self._current_date = datetime.now().strftime('%Y-%m-%d')
        self._log_file = os.path.join(self.log_dir, f"trades_{self._current_date}.csv")
        self._json_log = os.path.join(self.log_dir, f"trades_{self._current_date}.json")

        # Thread safety lock for file operations
        self._lock = threading.Lock()

        # Initialize CSV file with headers if it doesn't exist
        self._init_csv()

        # In-memory trade list for the day
        self.trades: List[Dict[str, Any]] = []
        self._load_existing_trades()

    def _init_csv(self):
        """Initialize CSV file with headers."""
        if not os.path.exists(self._log_file):
            headers = [
                'timestamp', 'order_id', 'symbol', 'exchange', 'action',
                'quantity', 'price', 'order_type', 'product', 'status',
                'fill_price', 'fill_time', 'tag', 'pnl', 'notes'
            ]
            with open(self._log_file, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(headers)

    def _load_existing_trades(self):
        """Load existing trades from JSON file."""
        if os.path.exists(self._json_log):
            try:
                with open(self._json_log, 'r') as f:
                    self.trades = json.load(f)
            except Exception as e:
                logger.warning(f"Failed to load existing trades: {e}")
                self.trades = []

    def _check_date_rollover(self):
        """Check if date has changed and rollover log files."""
        today = datetime.now().strftime('%Y-%m-%d')
        if today != self._current_date:
            self._current_date = today
            self._log_file = os.path.join(self.log_dir, f"trades_{today}.csv")
            self._json_log = os.path.join(self.log_dir, f"trades_{today}.json")
            self._init_csv()
            self.trades = []

    def log_order_placed(self, symbol: str, action: str, quantity: int,
                         price: float, order_type: str, product: str,
                         order_id: str = None, tag: str = None,
                         exchange: str = 'NFO'):
        """
        Log an order placement.

        Args:
            symbol: Trading symbol
            action: B or S
            quantity: Order quantity
            price: Order price
            order_type: L, MKT, SL, SL-M
            product: MIS, NRML
            order_id: Order ID from broker
            tag: Order tag
            exchange: Exchange (NFO, BFO, etc.)
        """
        self._check_date_rollover()

        trade = {
            'timestamp': datetime.now().isoformat(),
            'order_id': order_id or '',
            'symbol': symbol,
            'exchange': exchange,
            'action': 'BUY' if action == 'B' else 'SELL',
            'quantity': quantity,
            'price': price,
            'order_type': order_type,
            'product': product,
            'status': 'PLACED',
            'fill_price': None,
            'fill_time': None,
            'tag': tag or '',
            'pnl': None,
            'notes': ''
        }

        self.trades.append(trade)
        self._write_csv(trade)
        self._save_json()

        logger.info(f"[LOG] Order placed: {symbol} {trade['action']} {quantity} @ {price}")

    def log_order_filled(self, order_id: str, fill_price: float,
                         fill_time: datetime = None):
        """
        Log order fill.

        Args:
            order_id: Order ID
            fill_price: Fill price
            fill_time: Fill timestamp
        """
        self._check_date_rollover()

        # Find and update the order
        found = False
        for trade in self.trades:
            if trade['order_id'] == order_id:
                trade['status'] = 'FILLED'
                trade['fill_price'] = fill_price
                trade['fill_time'] = (fill_time or datetime.now()).isoformat()
                found = True
                break

        if not found:
            logger.warning(f"[LOG] Order {order_id} not found in trades, cannot update fill")

        self._save_json()
        logger.info(f"[LOG] Order filled: {order_id} @ {fill_price}")

    def log_order_rejected(self, order_id: str, reason: str):
        """
        Log order rejection.

        Args:
            order_id: Order ID
            reason: Rejection reason
        """
        self._check_date_rollover()

        # Find and update the order
        found = False
        for trade in self.trades:
            if trade['order_id'] == order_id:
                trade['status'] = 'REJECTED'
                trade['notes'] = reason
                found = True
                break

        if not found:
            logger.debug(f"[LOG] Order {order_id} not found in trades (may be manual order)")

        self._save_json()
        logger.info(f"[LOG] Order rejected: {order_id} - {reason}")

    def log_order_cancelled(self, order_id: str):
        """
        Log order cancellation.

        Args:
            order_id: Order ID
        """
        self._check_date_rollover()

        found = False
        for trade in self.trades:
            if trade['order_id'] == order_id:
                trade['status'] = 'CANCELLED'
                found = True
                break

        if not found:
            logger.debug(f"[LOG] Order {order_id} not found in trades (may be manual order)")

        self._save_json()
        logger.info(f"[LOG] Order cancelled: {order_id}")

    def log_position_exit(self, symbol: str, entry_price: float,
                          exit_price: float, quantity: int, pnl: float,
                          exit_type: str = 'MANUAL'):
        """
        Log position exit with P&L.

        Args:
            symbol: Trading symbol
            entry_price: Entry price
            exit_price: Exit price
            quantity: Position quantity
            pnl: Realized P&L
            exit_type: MANUAL, SL, TARGET
        """
        self._check_date_rollover()

        trade = {
            'timestamp': datetime.now().isoformat(),
            'order_id': '',
            'symbol': symbol,
            'exchange': '',
            'action': 'EXIT',
            'quantity': quantity,
            'price': entry_price,
            'order_type': '',
            'product': '',
            'status': 'EXIT',
            'fill_price': exit_price,
            'fill_time': datetime.now().isoformat(),
            'tag': exit_type,
            'pnl': pnl,
            'notes': f"Entry: {entry_price}, Exit: {exit_price}"
        }

        self.trades.append(trade)
        self._write_csv(trade)
        self._save_json()

        logger.info(f"[LOG] Position exit: {symbol} P&L: {pnl}")

    def log_sl_hit(self, symbol: str, entry_price: float,
                   exit_price: float, quantity: int, pnl: float):
        """Log stop loss hit."""
        self.log_position_exit(symbol, entry_price, exit_price, quantity, pnl, 'SL')

    def log_target_hit(self, symbol: str, entry_price: float,
                       exit_price: float, quantity: int, pnl: float):
        """Log target hit."""
        self.log_position_exit(symbol, entry_price, exit_price, quantity, pnl, 'TARGET')

    def _write_csv(self, trade: Dict[str, Any]):
        """Append trade to CSV file (thread-safe)."""
        with self._lock:
            try:
                with open(self._log_file, 'a', newline='') as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        trade['timestamp'],
                        trade['order_id'],
                        trade['symbol'],
                        trade['exchange'],
                        trade['action'],
                        trade['quantity'],
                        trade['price'],
                        trade['order_type'],
                        trade['product'],
                        trade['status'],
                        trade['fill_price'] or '',
                        trade['fill_time'] or '',
                        trade['tag'],
                        trade['pnl'] or '',
                        trade['notes']
                    ])
            except Exception as e:
                logger.error(f"Failed to write CSV: {e}")

    def _save_json(self):
        """Save trades to JSON file atomically (write to temp, then rename). Thread-safe."""
        temp_file = self._json_log + '.tmp'
        with self._lock:
            try:
                # Write to temp file first
                with open(temp_file, 'w') as f:
                    json.dump(self.trades, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())  # Force write to disk

                # Atomic rename (overwrites existing)
                os.replace(temp_file, self._json_log)

            except Exception as e:
                logger.error(f"Failed to save JSON: {e}")
                # Clean up temp file if it exists
                try:
                    if os.path.exists(temp_file):
                        os.remove(temp_file)
                except OSError as cleanup_err:
                    # S28: Log instead of silently ignoring
                    logger.debug(f"[LOG] Failed to cleanup temp file: {cleanup_err}")

    def get_daily_summary(self) -> Dict[str, Any]:
        """
        Get daily trading summary.

        Returns:
            Summary dict with stats
        """
        total_trades = 0
        winners = 0
        losers = 0
        total_pnl = 0.0

        for trade in self.trades:
            if trade['status'] == 'EXIT' and trade['pnl'] is not None:
                total_trades += 1
                # S27: Safe float conversion
                try:
                    pnl = float(trade['pnl'])
                except (ValueError, TypeError):
                    pnl = 0.0
                total_pnl += pnl
                if pnl >= 0:
                    winners += 1
                else:
                    losers += 1

        win_rate = (winners / total_trades * 100) if total_trades > 0 else 0

        return {
            'date': self._current_date,
            'total_trades': total_trades,
            'winners': winners,
            'losers': losers,
            'win_rate': win_rate,
            'total_pnl': total_pnl
        }

    def get_trades_for_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Get all trades for a specific symbol."""
        return [t for t in self.trades if t['symbol'] == symbol]

    def get_all_trades(self) -> List[Dict[str, Any]]:
        """Get all trades for the day."""
        return self.trades.copy()

    def export_to_csv(self, filepath: str = None) -> str:
        """
        Export trades to CSV.

        Args:
            filepath: Custom file path (optional)

        Returns:
            Path to exported file
        """
        if filepath is None:
            filepath = os.path.join(self.log_dir, f"trades_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")

        headers = ['timestamp', 'symbol', 'action', 'quantity', 'price',
                   'fill_price', 'status', 'pnl', 'tag', 'notes']

        with open(filepath, 'w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(self.trades)

        return filepath

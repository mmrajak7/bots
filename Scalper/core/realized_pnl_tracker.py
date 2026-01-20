"""
NEO Trade Terminal - Realized P&L Tracker

Tracks realized P&L from closed trades by maintaining entry price records
and calculating P&L when positions are exited.

CRITICAL: The broker API doesn't reliably provide realized P&L.
We must track this ourselves to ensure accurate daily loss circuit breaker.
"""

import threading
from typing import Dict, Optional, Any, List
from dataclasses import dataclass, field
from datetime import datetime, date
import json
import os
import logging

logger = logging.getLogger(__name__)


@dataclass
class PositionEntry:
    """Records entry details for P&L calculation on exit."""
    symbol: str
    side: str  # 'LONG' or 'SHORT'
    entry_price: float
    entry_qty: int
    remaining_qty: int
    entry_time: datetime = field(default_factory=datetime.now)
    entry_order_id: Optional[str] = None

    # For scaling - track multiple entries at different prices
    # Using weighted average for simplicity
    total_value: float = 0.0  # entry_price * entry_qty (cumulative)

    # Slippage tracking
    expected_entry_price: Optional[float] = None  # Limit price or expected fill
    entry_slippage: float = 0.0  # actual - expected (positive = worse)


@dataclass
class RealizedTrade:
    """Records a realized (closed) trade."""
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_percent: float
    exit_time: datetime = field(default_factory=datetime.now)
    exit_order_id: Optional[str] = None
    exit_type: str = ""  # SL, TARGET, EXIT, MANUAL

    # Slippage tracking
    expected_exit_price: Optional[float] = None  # SL trigger or target price
    exit_slippage: float = 0.0  # actual - expected (positive = worse for exits)
    total_slippage: float = 0.0  # entry_slippage + exit_slippage


class RealizedPnLTracker:
    """
    Tracks realized P&L by maintaining entry price records.

    FLOW:
    1. On entry order fill: record_entry(symbol, side, price, qty)
    2. On exit order fill: record_exit(symbol, price, qty) -> returns P&L
    3. get_realized_pnl_today() returns accurate realized P&L

    Handles:
    - Partial entries (scaling in)
    - Partial exits (scaling out)
    - Multiple positions in same symbol
    """

    def __init__(self, persist_path: str = "data/realized_pnl.json"):
        self.persist_path = persist_path
        self._lock = threading.Lock()

        # Active positions with entry details
        self.positions: Dict[str, PositionEntry] = {}

        # Today's realized trades
        self.realized_trades: List[RealizedTrade] = []
        self._realized_pnl_today: float = 0.0

        # Track by date for day rollover
        self._tracking_date: date = datetime.now().date()

        # Load any persisted state
        self._load_state()

    def record_entry(self, symbol: str, side: str, price: float, qty: int,
                     order_id: str = None, expected_price: float = None) -> float:
        """
        Record a position entry (or scaling in).

        Args:
            symbol: Trading symbol
            side: 'LONG' (buy) or 'SHORT' (sell)
            price: Entry/fill price (actual)
            qty: Filled quantity
            order_id: Entry order ID
            expected_price: Expected fill price (limit price) for slippage calc

        Returns:
            New average entry price
        """
        with self._lock:
            self._check_day_rollover()

            # Calculate entry slippage
            entry_slippage = 0.0
            if expected_price is not None and expected_price > 0:
                # For LONG: positive slippage = filled higher than expected (bad)
                # For SHORT: positive slippage = filled lower than expected (bad)
                if side.upper() == 'LONG':
                    entry_slippage = price - expected_price
                else:
                    entry_slippage = expected_price - price

                if abs(entry_slippage) > 0.01:  # Only log if meaningful
                    logger.info(f"[PNL] Entry slippage for {symbol}: {entry_slippage:+.2f} "
                               f"(expected={expected_price}, actual={price})")

            existing = self.positions.get(symbol)

            if existing:
                # Scaling into existing position
                if existing.side != side.upper():
                    # Opposite direction = closing position, not entry
                    logger.warning(f"[PNL] record_entry called with opposite side "
                                  f"for {symbol}. Use record_exit instead.")
                    return existing.entry_price

                # Add to position (weighted average)
                old_value = existing.total_value
                new_value = price * qty
                existing.total_value = old_value + new_value
                existing.remaining_qty += qty
                existing.entry_qty += qty
                existing.entry_price = existing.total_value / existing.entry_qty
                # Accumulate slippage
                existing.entry_slippage += entry_slippage

                logger.info(f"[PNL] Scaled into {symbol}: +{qty} @ {price}, "
                           f"new avg={existing.entry_price:.2f}, total={existing.remaining_qty}")

                return existing.entry_price
            else:
                # New position
                entry = PositionEntry(
                    symbol=symbol,
                    side=side.upper(),
                    entry_price=price,
                    entry_qty=qty,
                    remaining_qty=qty,
                    entry_order_id=order_id,
                    total_value=price * qty,
                    expected_entry_price=expected_price,
                    entry_slippage=entry_slippage
                )
                self.positions[symbol] = entry

                logger.info(f"[PNL] New entry: {symbol} {side} {qty} @ {price}")
                self._save_state()

                return price

    def record_exit(self, symbol: str, exit_price: float, exit_qty: int,
                    order_id: str = None, exit_type: str = "EXIT",
                    expected_price: float = None) -> Dict[str, Any]:
        """
        Record a position exit (full or partial).

        Args:
            symbol: Trading symbol
            exit_price: Exit/fill price (actual)
            exit_qty: Exited quantity
            order_id: Exit order ID
            exit_type: Type of exit (SL, TARGET, EXIT, MANUAL)
            expected_price: Expected fill price (SL trigger or target) for slippage

        Returns:
            Dict with 'pnl', 'pnl_percent', 'remaining_qty', 'fully_closed', 'slippage'
        """
        with self._lock:
            self._check_day_rollover()

            entry = self.positions.get(symbol)

            if not entry:
                logger.warning(f"[PNL] No entry record for {symbol}, "
                              f"cannot calculate P&L")
                return {
                    'pnl': 0.0,
                    'pnl_percent': 0.0,
                    'remaining_qty': 0,
                    'fully_closed': True,
                    'error': 'No entry record'
                }

            # Calculate P&L for this exit
            if entry.side == 'LONG':
                # LONG: profit when exit > entry
                pnl = (exit_price - entry.entry_price) * exit_qty
            else:
                # SHORT: profit when entry > exit
                pnl = (entry.entry_price - exit_price) * exit_qty

            pnl_percent = ((exit_price - entry.entry_price) / entry.entry_price * 100) \
                if entry.entry_price > 0 else 0
            if entry.side == 'SHORT':
                pnl_percent = -pnl_percent

            # Calculate exit slippage
            exit_slippage = 0.0
            if expected_price is not None and expected_price > 0:
                # For LONG exits (selling): slippage = expected - actual (filled lower = bad)
                # For SHORT exits (buying): slippage = actual - expected (filled higher = bad)
                if entry.side == 'LONG':
                    exit_slippage = expected_price - exit_price
                else:
                    exit_slippage = exit_price - expected_price

                if abs(exit_slippage) > 0.01:
                    logger.info(f"[PNL] Exit slippage for {symbol}: {exit_slippage:+.2f} "
                               f"(expected={expected_price}, actual={exit_price})")

            # Total slippage for this trade (entry + exit)
            total_slippage = entry.entry_slippage + exit_slippage

            # Update remaining quantity
            entry.remaining_qty -= exit_qty
            fully_closed = entry.remaining_qty <= 0

            # Record realized trade with slippage
            trade = RealizedTrade(
                symbol=symbol,
                side=entry.side,
                entry_price=entry.entry_price,
                exit_price=exit_price,
                quantity=exit_qty,
                pnl=pnl,
                pnl_percent=pnl_percent,
                exit_order_id=order_id,
                exit_type=exit_type,
                expected_exit_price=expected_price,
                exit_slippage=exit_slippage,
                total_slippage=total_slippage
            )
            self.realized_trades.append(trade)
            self._realized_pnl_today += pnl

            slip_str = f", slippage={total_slippage:+.2f}" if abs(total_slippage) > 0.01 else ""
            logger.info(f"[PNL] Exit recorded: {symbol} {exit_qty} @ {exit_price}, "
                       f"P&L={pnl:+.2f} ({pnl_percent:+.2f}%), "
                       f"type={exit_type}{slip_str}, daily_total={self._realized_pnl_today:+.2f}")

            # Remove position if fully closed
            if fully_closed:
                del self.positions[symbol]
                logger.info(f"[PNL] Position fully closed: {symbol}")

            self._save_state()

            return {
                'pnl': pnl,
                'pnl_percent': pnl_percent,
                'remaining_qty': max(0, entry.remaining_qty),
                'fully_closed': fully_closed,
                'entry_price': entry.entry_price,
                'exit_price': exit_price,
                'exit_slippage': exit_slippage,
                'total_slippage': total_slippage
            }

    def get_realized_pnl_today(self) -> float:
        """Get total realized P&L for today."""
        with self._lock:
            self._check_day_rollover()
            return self._realized_pnl_today

    def get_total_slippage_today(self) -> float:
        """Get total slippage cost for today."""
        with self._lock:
            return sum(t.total_slippage for t in self.realized_trades)

    def get_entry_price(self, symbol: str) -> Optional[float]:
        """Get average entry price for a symbol (for P&L display)."""
        with self._lock:
            entry = self.positions.get(symbol)
            return entry.entry_price if entry else None

    def get_position_details(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get full position details including entry info."""
        with self._lock:
            entry = self.positions.get(symbol)
            if not entry:
                return None
            return {
                'symbol': entry.symbol,
                'side': entry.side,
                'entry_price': entry.entry_price,
                'entry_qty': entry.entry_qty,
                'remaining_qty': entry.remaining_qty,
                'entry_time': entry.entry_time.isoformat()
            }

    def get_all_entries(self) -> List[Dict[str, Any]]:
        """Get all tracked position entries."""
        with self._lock:
            return [
                {
                    'symbol': e.symbol,
                    'side': e.side,
                    'entry_price': e.entry_price,
                    'remaining_qty': e.remaining_qty
                }
                for e in self.positions.values()
            ]

    def get_realized_trades_today(self) -> List[Dict[str, Any]]:
        """Get list of all realized trades today."""
        with self._lock:
            self._check_day_rollover()
            return [
                {
                    'symbol': t.symbol,
                    'side': t.side,
                    'entry_price': t.entry_price,
                    'exit_price': t.exit_price,
                    'quantity': t.quantity,
                    'pnl': t.pnl,
                    'pnl_percent': t.pnl_percent,
                    'exit_type': t.exit_type,
                    'exit_time': t.exit_time.isoformat()
                }
                for t in self.realized_trades
            ]

    def sync_with_broker_positions(self, broker_positions: List[Dict[str, Any]]):
        """
        Sync tracked entries with broker positions.

        Call this on startup to reconcile any positions opened
        outside this terminal or carried over.
        """
        with self._lock:
            for pos in broker_positions:
                symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))

                if qty == 0:
                    continue

                side = 'LONG' if qty > 0 else 'SHORT'
                avg_price = float(pos.get('averagePrice', pos.get('avgPrc', 0)) or 0)

                if symbol not in self.positions and avg_price > 0:
                    # Position exists at broker but not tracked - add it
                    entry = PositionEntry(
                        symbol=symbol,
                        side=side,
                        entry_price=avg_price,
                        entry_qty=abs(qty),
                        remaining_qty=abs(qty),
                        total_value=avg_price * abs(qty)
                    )
                    self.positions[symbol] = entry
                    logger.info(f"[PNL] Synced from broker: {symbol} {side} "
                               f"{abs(qty)} @ {avg_price}")

            self._save_state()

    def remove_position(self, symbol: str):
        """Remove a position without recording exit P&L (use sparingly)."""
        with self._lock:
            if symbol in self.positions:
                del self.positions[symbol]
                logger.info(f"[PNL] Position removed (no P&L): {symbol}")
                self._save_state()

    def _check_day_rollover(self):
        """Reset realized P&L if it's a new trading day."""
        today = datetime.now().date()
        if today != self._tracking_date:
            logger.info(f"[PNL] New day detected: {self._tracking_date} -> {today}")
            self._tracking_date = today
            self._realized_pnl_today = 0.0
            self.realized_trades.clear()
            # Keep positions - they may be overnight
            self._save_state()

    def reset_daily(self):
        """Manual reset of daily P&L (usually auto-resets on new day)."""
        with self._lock:
            self._realized_pnl_today = 0.0
            self.realized_trades.clear()
            self._tracking_date = datetime.now().date()
            logger.info("[PNL] Daily P&L reset manually")
            self._save_state()

    def _save_state(self):
        """Persist tracker state to file atomically (write to temp, then rename)."""
        temp_file = None
        try:
            state = {
                'date': self._tracking_date.isoformat(),
                'realized_pnl': self._realized_pnl_today,
                'positions': {
                    sym: {
                        'symbol': e.symbol,
                        'side': e.side,
                        'entry_price': e.entry_price,
                        'entry_qty': e.entry_qty,
                        'remaining_qty': e.remaining_qty,
                        'total_value': e.total_value
                    }
                    for sym, e in self.positions.items()
                },
                'trades': [
                    {
                        'symbol': t.symbol,
                        'side': t.side,
                        'entry_price': t.entry_price,
                        'exit_price': t.exit_price,
                        'quantity': t.quantity,
                        'pnl': t.pnl,
                        'exit_type': t.exit_type,
                        'exit_time': t.exit_time.isoformat()
                    }
                    for t in self.realized_trades
                ]
            }

            os.makedirs(os.path.dirname(self.persist_path), exist_ok=True)

            # CRITICAL: Write to temp file first, then atomic rename
            temp_file = self.persist_path + '.tmp'
            with open(temp_file, 'w') as f:
                json.dump(state, f, indent=2)
                f.flush()
                os.fsync(f.fileno())  # Force write to disk

            # Atomic rename (prevents corruption on crash)
            os.replace(temp_file, self.persist_path)

        except Exception as e:
            logger.warning(f"[PNL] Failed to save state: {e}")
            # Clean up temp file if exists
            try:
                if temp_file and os.path.exists(temp_file):
                    os.remove(temp_file)
            except:
                pass

    def _load_state(self):
        """Load persisted state from file."""
        if not os.path.exists(self.persist_path):
            return

        try:
            with open(self.persist_path, 'r') as f:
                state = json.load(f)

            saved_date = date.fromisoformat(state.get('date', '2000-01-01'))
            today = datetime.now().date()

            # Only restore if same day
            if saved_date == today:
                self._tracking_date = saved_date
                self._realized_pnl_today = state.get('realized_pnl', 0.0)

                # Restore positions
                for sym, data in state.get('positions', {}).items():
                    self.positions[sym] = PositionEntry(
                        symbol=data['symbol'],
                        side=data['side'],
                        entry_price=data['entry_price'],
                        entry_qty=data['entry_qty'],
                        remaining_qty=data['remaining_qty'],
                        total_value=data.get('total_value', data['entry_price'] * data['entry_qty'])
                    )

                # Restore trades
                for t in state.get('trades', []):
                    self.realized_trades.append(RealizedTrade(
                        symbol=t['symbol'],
                        side=t['side'],
                        entry_price=t['entry_price'],
                        exit_price=t['exit_price'],
                        quantity=t['quantity'],
                        pnl=t['pnl'],
                        pnl_percent=0,
                        exit_type=t.get('exit_type', 'EXIT'),
                        exit_time=datetime.fromisoformat(t['exit_time'])
                    ))

                logger.info(f"[PNL] Restored state: realized={self._realized_pnl_today:.2f}, "
                           f"positions={len(self.positions)}, trades={len(self.realized_trades)}")
            else:
                # Different day - start fresh but keep positions
                self._tracking_date = today
                for sym, data in state.get('positions', {}).items():
                    self.positions[sym] = PositionEntry(
                        symbol=data['symbol'],
                        side=data['side'],
                        entry_price=data['entry_price'],
                        entry_qty=data['entry_qty'],
                        remaining_qty=data['remaining_qty'],
                        total_value=data.get('total_value', data['entry_price'] * data['entry_qty'])
                    )
                logger.info(f"[PNL] New day, carried over {len(self.positions)} positions")

        except Exception as e:
            logger.warning(f"[PNL] Failed to load state: {e}")

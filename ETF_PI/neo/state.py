"""
neo.state — State persistence between cron runs.

Manages NeoPosition tracking and daily circuit breaker state.
All state persisted atomically to neo/data/state.json.
"""

import os
import json
import logging
import datetime
import pytz

log = logging.getLogger('neo.state')
IST = pytz.timezone('Asia/Kolkata')


class NeoPosition:
    """Represents a Neo bridge position (entry → fill → TP → exit)."""

    __slots__ = (
        'key', 'symbol', 'qty', 'entry_price', 'tp_price',
        'signal_tf', 'max_hold_mins', 'entry_time', 'status',
        'entry_filled', 'entry_order_id', 'entry_tag',
        'tp_order_id', 'tp_tag', 'exit_order_id', 'exit_tag',
        'exit_type', 'exit_time',
        'lot_size', 'tick_size',
    )

    def __init__(self, **kw):
        self.key = kw.get('key', '')
        self.symbol = kw.get('symbol', '')
        self.qty = int(kw.get('qty', 0))
        self.entry_price = float(kw.get('entry_price', 0))
        self.tp_price = float(kw.get('tp_price', 0))
        self.signal_tf = kw.get('signal_tf', '')
        self.max_hold_mins = int(kw.get('max_hold_mins', 120))
        self.entry_time = kw.get('entry_time', '')
        self.status = kw.get('status', 'pending_entry')
        self.entry_filled = bool(kw.get('entry_filled', False))
        self.entry_order_id = kw.get('entry_order_id')
        self.entry_tag = kw.get('entry_tag')
        self.tp_order_id = kw.get('tp_order_id')
        self.tp_tag = kw.get('tp_tag')
        self.exit_order_id = kw.get('exit_order_id')
        self.exit_tag = kw.get('exit_tag')
        self.exit_type = kw.get('exit_type')
        self.exit_time = kw.get('exit_time')
        self.lot_size = int(kw.get('lot_size', 0))
        self.tick_size = float(kw.get('tick_size', 0.05))

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class StateManager:
    """Manages neo/data/state.json — positions, daily P&L, circuit breaker."""

    def __init__(self, data_dir):
        self._path = os.path.join(data_dir, 'state.json')
        self.positions = {}        # key -> NeoPosition
        self.daily_pnl = 0.0
        self.trade_count = 0
        self.circuit_breaker_active = False
        self.circuit_breaker_reason = ''
        self._date = ''            # track day resets
        self._load()

    def save(self):
        """Atomic write: tmp + os.replace."""
        data = {
            'date': self._date,
            'daily_pnl': self.daily_pnl,
            'trade_count': self.trade_count,
            'circuit_breaker_active': self.circuit_breaker_active,
            'circuit_breaker_reason': self.circuit_breaker_reason,
            'positions': {k: p.to_dict() for k, p in self.positions.items()},
        }
        tmp = self._path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._path)

    def _load(self):
        today = datetime.datetime.now(IST).strftime('%Y-%m-%d')

        if not os.path.exists(self._path):
            self._date = today
            return

        try:
            with open(self._path) as f:
                data = json.load(f)
        except Exception:
            self._date = today
            return

        saved_date = data.get('date', '')

        # Reset daily counters on new day
        if saved_date != today:
            self._date = today
            # Keep positions for stale cleanup, reset counters
            for k, pd in data.get('positions', {}).items():
                self.positions[k] = NeoPosition.from_dict(pd)
            return

        self._date = today
        self.daily_pnl = float(data.get('daily_pnl', 0))
        self.trade_count = int(data.get('trade_count', 0))
        self.circuit_breaker_active = bool(data.get('circuit_breaker_active', False))
        self.circuit_breaker_reason = data.get('circuit_breaker_reason', '')
        for k, pd in data.get('positions', {}).items():
            self.positions[k] = NeoPosition.from_dict(pd)

    def clean_stale(self, cancel_fn=None):
        """Close positions from previous days. Cancel exchange orders first (H2 fix).

        Args:
            cancel_fn: Callable(order_id) to cancel an exchange order. Best-effort.
        """
        today = datetime.datetime.now(IST).strftime('%Y-%m-%d')
        stale = []
        for k, pos in self.positions.items():
            if not pos.entry_time:
                continue
            try:
                entry_date = pos.entry_time[:10]  # YYYY-MM-DD from ISO
            except Exception:
                entry_date = ''
            if entry_date and entry_date < today and pos.status in ('pending_entry', 'open', 'pending_exit'):
                stale.append(k)

        for k in stale:
            pos = self.positions[k]
            # Cancel live exchange orders before marking stale
            if cancel_fn:
                for oid in (pos.entry_order_id, pos.tp_order_id):
                    if oid and oid not in ('LOG_ONLY', None):
                        try:
                            cancel_fn(oid)
                        except Exception as e:
                            log.warning(f"Stale cancel failed {oid}: {e}")
            pos.status = 'closed'
            pos.exit_type = 'STALE'
            pos.exit_time = datetime.datetime.now(IST).isoformat()
            log.info(f"Stale closed: {k}")

        if stale:
            self.save()
            log.info(f"Cleaned {len(stale)} stale positions")

    def get_open_positions(self):
        """Return dict of positions with status 'open' or 'pending_entry'."""
        return {k: p for k, p in self.positions.items()
                if p.status in ('pending_entry', 'open', 'pending_exit')}

    def get_open_count(self):
        return sum(1 for p in self.positions.values()
                   if p.status in ('pending_entry', 'open', 'pending_exit'))

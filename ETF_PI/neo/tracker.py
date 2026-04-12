"""
neo.tracker — Order tag tracking, JSON-persisted.

Generates unique tags per order, tracks confirmation, verifies in order book.
Persisted to neo/data/tracker.json between cron runs.

Adapted from Scalper core/order_tracker.py.
"""

import os
import json
import time
import logging
from enum import Enum
from typing import Optional, Dict

from neo import fields

log = logging.getLogger('neo.tracker')


class OrderType(Enum):
    """Order types with unique tag prefixes."""
    ENTRY = "ENT"
    TARGET = "TGT"
    EXIT = "EXT"


class TrackedOrder:
    """Represents a tracked order."""

    __slots__ = ('tag', 'symbol', 'transaction_type', 'quantity',
                 'order_type', 'price_type', 'placed_at',
                 'order_id', 'status')

    def __init__(self, **kw):
        self.tag = kw.get('tag', '')
        self.symbol = kw.get('symbol', '')
        self.transaction_type = kw.get('transaction_type', '')
        self.quantity = int(kw.get('quantity', 0))
        self.order_type = kw.get('order_type', 'L')
        self.price_type = kw.get('price_type', 'ENT')
        if isinstance(self.price_type, OrderType):
            self.price_type = self.price_type.value
        self.placed_at = kw.get('placed_at', '')
        self.order_id = kw.get('order_id')
        self.status = kw.get('status', 'pending')

    def to_dict(self):
        return {s: getattr(self, s) for s in self.__slots__}

    @classmethod
    def from_dict(cls, d):
        return cls(**d)


class OrderTracker:
    """Tracks all orders by unique tags. Persisted to JSON."""

    def __init__(self, data_dir):
        self._path = os.path.join(data_dir, 'tracker.json')
        self._orders = {}           # tag -> TrackedOrder
        self._id_to_tag = {}        # order_id -> tag
        self._counter = 0
        self._load()

    def generate_tag(self, order_type):
        """Generate unique tag: {TYPE}_{TIMESTAMP}_{COUNTER}, max 20 chars."""
        self._counter += 1
        ts = int(time.time() * 1000) % 10000000000
        tag = f"{order_type.value}_{ts}_{self._counter:03d}"
        if len(tag) > 20:
            tag = tag[:20]
        return tag

    def register_order(self, tag, symbol, txn_type, qty, order_type_str, price_type):
        """Register an order we're about to place. Call BEFORE placing."""
        order = TrackedOrder(
            tag=tag, symbol=symbol, transaction_type=txn_type,
            quantity=qty, order_type=order_type_str, price_type=price_type,
            placed_at=time.time(),
        )
        self._orders[tag] = order
        log.info(f"[TRACKER] Registered: {tag} {symbol} {txn_type} qty={qty}")
        return order

    def confirm_order(self, tag, order_id, response=None):
        """Confirm order was placed. Call after receiving order_id."""
        if tag not in self._orders:
            log.warning(f"[TRACKER] Confirm failed — tag not found: {tag}")
            return False
        order = self._orders[tag]
        order.order_id = order_id
        order.status = 'confirmed'
        self._id_to_tag[order_id] = tag
        log.info(f"[TRACKER] Confirmed: {tag} -> {order_id}")
        return True

    def mark_failed(self, tag, reason=''):
        if tag in self._orders:
            self._orders[tag].status = 'failed'
            log.warning(f"[TRACKER] Failed: {tag} — {reason}")

    def get_order_by_tag(self, tag):
        return self._orders.get(tag)

    def get_order_by_id(self, order_id):
        tag = self._id_to_tag.get(order_id)
        return self._orders.get(tag) if tag else None

    def is_our_order(self, order_id):
        return order_id in self._id_to_tag

    def verify_order_by_tag(self, client, tag, max_age_seconds=15):
        """Search order_report for our tag. Returns order_id or None.

        Critical for recovering from 'error from core' responses where
        the order was placed but the response was garbled.
        """
        order = self._orders.get(tag)
        if not order:
            return None

        try:
            report = client.order_report()
            orders = report.get('data', []) if report else []

            # First pass: match by tag
            for o in orders:
                if fields.get_order_tag(o) == tag:
                    oid = fields.get_order_id(o)
                    status = fields.get_order_status(o)
                    if oid and status not in ('rejected', 'cancelled'):
                        self.confirm_order(tag, oid)
                        return oid

            # Second pass: match by criteria (strict)
            now = time.time()
            for o in orders:
                if (fields.get_symbol(o) == order.symbol and
                        o.get('trnsTp') == order.transaction_type and
                        int(o.get('qty', 0) or 0) == order.quantity):
                    status = fields.get_order_status(o)
                    if status in ('rejected', 'cancelled'):
                        continue
                    oid = fields.get_order_id(o)
                    if oid:
                        log.info(f"[TRACKER] Verified by criteria: {tag} -> {oid}")
                        self.confirm_order(tag, oid)
                        return oid

            log.warning(f"[TRACKER] Order not found in book: {tag}")
            return None
        except Exception as e:
            log.error(f"[TRACKER] Verify error: {e}")
            return None

    def cleanup_old(self, max_age_hours=24):
        cutoff = time.time() - (max_age_hours * 3600)
        old = [t for t, o in self._orders.items() if float(o.placed_at) < cutoff]
        for t in old:
            order = self._orders.pop(t, None)
            if order and order.order_id:
                self._id_to_tag.pop(order.order_id, None)
        if old:
            log.info(f"[TRACKER] Cleaned {len(old)} old orders")

    def save(self):
        data = {
            'counter': self._counter,
            'orders': {t: o.to_dict() for t, o in self._orders.items()},
        }
        tmp = self._path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._path)

    def _load(self):
        if not os.path.exists(self._path):
            return
        try:
            with open(self._path) as f:
                data = json.load(f)
            self._counter = int(data.get('counter', 0))
            for t, od in data.get('orders', {}).items():
                order = TrackedOrder.from_dict(od)
                self._orders[t] = order
                if order.order_id:
                    self._id_to_tag[order.order_id] = t
        except Exception as e:
            log.warning(f"Tracker load failed: {e}")

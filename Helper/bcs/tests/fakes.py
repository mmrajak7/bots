"""Offline test doubles for the live-order path — replay harness, tier 1.

Required by `feedback_live_automation_bar`: no auto-order code goes live
without an offline replay of the whole exit path. It matters more than usual
here because **all three trade books are currently closed**, so across the
sessions before go-live no open position will ever exercise these fixes on the
Pi. There will be no live paper record. This harness is the only evidence that
will exist.

Deliberately under `tests/` so production code can never import it. That is a
real constraint in a live-money package, not tidiness.

Contents
--------
`FakeBroker`   the complete Kite surface `spread_monitor` touches — verified by
               AST-walk, not assumed: 7 methods and 2 constants. Fills mutate
               `positions()`, so `reconcile_after_close` reads a genuinely
               derived state rather than a hand-written one. That is what makes
               the B10/B11 tests honest instead of circular.
`FakeClock`    `time()`/`sleep()` that advance a counter instead of blocking, so
               a 6.5-hour session replays in milliseconds.
`MemoryStore`  the 12 store members `spread_monitor` calls, recording every
               mutation for assertions.
`TelegramSpy`  collects messages. `conftest.py` already blocks real sends at the
               HTTP layer; this exists so assertions can read the text.
"""
from __future__ import annotations

import itertools
from datetime import datetime


# ── Broker ───────────────────────────────────────────────────────────────────

def always_complete(order):
    """Default fill policy: everything fills in full at the limit price."""
    return 'COMPLETE', order['quantity'], order['price']


def never_fills(order):
    return 'OPEN', 0, 0.0


def rejects(order):
    return 'REJECTED', 0, 0.0


def partial(fraction):
    """Fill `fraction` of the quantity, then sit CANCELLED with the residue.

    The B10 shape: `status == 'PARTIAL'` is not actually what Kite emits — a
    partially filled order that stops is CANCELLED with a non-zero
    `filled_quantity`. Both are modelled; see `partial_status` below.
    """
    def _policy(order):
        q = int(order['quantity'] * fraction)
        return 'CANCELLED', q, order['price']
    return _policy


def partial_status(fraction):
    """Emit the literal 'PARTIAL' status the buggy success-check accepts."""
    def _policy(order):
        q = int(order['quantity'] * fraction)
        return 'PARTIAL', q, order['price']
    return _policy


class FakeBroker:
    """Stands in for `KiteConnect`. Only the surface the monitor actually uses.

    Every method can be made to raise by setting `<name>_raises` to an
    exception instance — that is how the B9 auth-death and B12 renamed-symbol
    fixtures are driven.
    """

    #: Constants read off the client. Exactly the two that are referenced.
    VARIETY_REGULAR = 'regular'
    ORDER_TYPE_LIMIT = 'LIMIT'

    def __init__(self, spots=None, books=None, positions=None,
                 fill_policy=None, tag='BCS_MON'):
        self.spots = dict(spots or {})            # {'NSE:X': 100.0}
        self.books = dict(books or {})            # {'NFO:XCE': {...}}
        self._positions = list(positions or [])   # [{tradingsymbol, quantity, ...}]
        self.fill_policy = fill_policy or always_complete
        self.tag = tag
        self.order_book = []
        self.placed = []          # every place_order kwargs dict, in order
        self.cancelled = []
        self._ids = itertools.count(1000)

        self.ltp_raises = None
        self.quote_raises = None
        self.positions_raises = None
        self.orders_raises = None
        self.place_order_raises = None
        self.cancel_order_raises = None

    # -- auth ---------------------------------------------------------------
    def set_access_token(self, token):
        self.access_token = token

    # -- market data --------------------------------------------------------
    def ltp(self, symbols):
        if self.ltp_raises:
            raise self.ltp_raises
        out = {}
        for s in symbols:
            if s not in self.spots:
                raise KeyError(s)      # what a renamed spot_symbol looks like
            out[s] = {'last_price': self.spots[s]}
        return out

    def quote(self, keys):
        if self.quote_raises:
            raise self.quote_raises
        out = {}
        for k in keys:
            b = self.books.get(k)
            if b is None:
                raise KeyError(k)
            out[k] = self._as_quote(b)
        return out

    @staticmethod
    def _as_quote(b):
        """Turn a compact {bid,ask,...} fixture into Kite's quote shape."""
        return {
            'depth': {
                'buy': ([{'price': b['bid'], 'quantity': b.get('bid_qty', 100)}]
                        if b.get('bid') else []),
                'sell': ([{'price': b['ask'], 'quantity': b.get('ask_qty', 100)}]
                         if b.get('ask') else []),
            },
            'last_price': b.get('ltp', 0),
            'ohlc': {'close': b.get('prev_close', 0)},
            'last_trade_time': b.get('last_trade_time'),
        }

    # -- account ------------------------------------------------------------
    def positions(self):
        if self.positions_raises:
            raise self.positions_raises
        return {'net': [dict(p) for p in self._positions]}

    def orders(self):
        if self.orders_raises:
            raise self.orders_raises
        return [dict(o) for o in self.order_book]

    # -- orders -------------------------------------------------------------
    def place_order(self, **kw):
        if self.place_order_raises:
            raise self.place_order_raises
        self.placed.append(dict(kw))
        oid = str(next(self._ids))
        status, filled, avg = self.fill_policy(kw)
        self.order_book.append({
            'order_id': oid,
            'tradingsymbol': kw['tradingsymbol'],
            'transaction_type': kw['transaction_type'],
            'quantity': kw['quantity'],
            'price': kw['price'],
            'tag': kw.get('tag'),
            'status': status,
            'filled_quantity': filled,
            'average_price': avg,
            'status_message': '',
        })
        if filled:
            self._apply_fill(kw['tradingsymbol'], kw['transaction_type'], filled,
                             avg)
        return oid

    def cancel_order(self, variety=None, order_id=None):
        if self.cancel_order_raises:
            raise self.cancel_order_raises
        self.cancelled.append(order_id)
        for o in self.order_book:
            if o['order_id'] == str(order_id) and o['status'] not in (
                    'COMPLETE', 'REJECTED'):
                o['status'] = 'CANCELLED'
        return order_id

    # -- internals ----------------------------------------------------------
    def _apply_fill(self, symbol, txn, qty, price):
        """A fill MOVES the position. Without this, `verify_positions` and
        `reconcile_after_close` would read whatever the fixture author wrote
        rather than what the orders actually did — and the flipped-leg and
        partial-close tests would be asserting against their own premise."""
        delta = qty if txn == 'BUY' else -qty
        for p in self._positions:
            if p['tradingsymbol'] == symbol:
                p['quantity'] += delta
                return
        self._positions.append({'tradingsymbol': symbol, 'quantity': delta,
                                'average_price': price})

    # -- assertions helpers -------------------------------------------------
    def net_qty(self, symbol):
        for p in self._positions:
            if p['tradingsymbol'] == symbol:
                return p['quantity']
        return 0

    def orders_for(self, symbol):
        return [o for o in self.placed if o['tradingsymbol'] == symbol]


# ── Clock ────────────────────────────────────────────────────────────────────

class FakeClock:
    """`time()`/`sleep()` that advance a counter instead of blocking.

    `wait_for_fill` polls for up to ORDER_WAIT_SEC and `monitor_all` sleeps
    POLL_INTERVAL_SEC every cycle; against the real clock a single replay of a
    trading session would take a trading session.
    """

    #: A stuck clock makes `wait_for_fill`'s `while time.time() < deadline`
    #: spin forever, so a broken FakeClock WEDGES the suite instead of failing
    #: it — found the hard way while mutation-testing this file. A test that
    #: can hang is worse than one that fails: CI reports nothing at all.
    MAX_SLEEPS = 100_000

    def __init__(self, start=1_600_000_000.0):
        self.now = float(start)
        self.slept = []

    def time(self):
        return self.now

    def sleep(self, sec):
        self.slept.append(sec)
        if len(self.slept) > self.MAX_SLEEPS:
            raise AssertionError(
                'FakeClock slept %d times without the caller finishing — the '
                'clock is not advancing, or the code under test cannot '
                'terminate. Failing loudly instead of hanging.'
                % len(self.slept))
        self.now += sec

    def advance(self, sec):
        self.now += sec

    def install(self, monkeypatch, module):
        """Patch `module.time.time` and `module.time.sleep` in place."""
        monkeypatch.setattr(module.time, 'time', self.time)
        monkeypatch.setattr(module.time, 'sleep', self.sleep)
        return self


# ── Store ────────────────────────────────────────────────────────────────────

class MemoryStore:
    """The 12 store members `spread_monitor` calls, enumerated by AST-walk.

    Duck-typed rather than subclassing the real store, so a test can never
    accidentally touch a real JSON file or Drive.
    """

    def __init__(self, trades=None, active_alerts=None):
        self.trades = [dict(t) for t in (trades or [])]
        self._active = list(active_alerts or [])
        self._drive_enabled = False
        self.calls = []          # [(method, args, kwargs)]

    # -- recording ----------------------------------------------------------
    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))

    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    # -- reads --------------------------------------------------------------
    def get_open_trades(self):
        return [dict(t) for t in self.trades if t.get('status') == 'open']

    def get_closing_trades(self):
        return [dict(t) for t in self.trades if t.get('status') == 'closing']

    def find_open_trade(self, stock=None, trade_id=None):  # real: (stock, trade_id=None)
        for t in self.trades:
            if t.get('status') != 'open':
                continue
            if trade_id is not None and t.get('id') == trade_id:
                return dict(t)
            if stock is not None and t.get('stock') == stock:
                return dict(t)
        return None

    def list_trades(self):
        self._rec('list_trades')
        return [dict(t) for t in self.trades]

    def get_active(self):
        return list(self._active)

    def maybe_sync(self, force=False):
        self._rec('maybe_sync', force)

    # -- writes -------------------------------------------------------------
    def _find(self, tid):
        for t in self.trades:
            if t.get('id') == tid:
                return t
        raise KeyError(tid)

    def update_trade_fields(self, trade_id, **fields):
        self._rec('update_trade_fields', trade_id, **fields)
        self._find(trade_id).update(fields)

    def update_trade_exit(self, trade_id, exit_data):
        # Signature MIRRORS bcs/trade_store.py:413 — a positional dict, not
        # kwargs. A double that accepts a looser signature than the real thing
        # lets a genuine TypeError pass in tests and blow up in production.
        self._rec('update_trade_exit', trade_id, exit_data)
        t = self._find(trade_id)
        t.update(exit_data)
        t['status'] = 'closed'

    def begin_close(self, trade_id, reason):
        self._rec('begin_close', trade_id, reason)
        self._find(trade_id)['status'] = 'closing'
        return True

    def recover_closing_trade(self, trade_id):
        self._rec('recover_closing_trade', trade_id)
        self._find(trade_id)['status'] = 'open'

    def set_trade_status(self, trade_id, status, **extra_fields):
        self._rec('set_trade_status', trade_id, status, **extra_fields)
        t = self._find(trade_id)
        t['status'] = status
        t.update(extra_fields)


# ── Telegram ─────────────────────────────────────────────────────────────────

class TelegramSpy:
    def __init__(self):
        self.sent = []

    def __call__(self, msg):
        self.sent.append(msg)
        return True

    def install(self, monkeypatch, module):
        monkeypatch.setattr(module, 'send_telegram', self)
        return self

    def containing(self, needle):
        return [m for m in self.sent if needle in m]

    def any(self, needle):
        return bool(self.containing(needle))

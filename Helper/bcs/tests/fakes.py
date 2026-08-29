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
from datetime import date, datetime, timedelta

from common import store_contract


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


class HedgeInvariantViolation(BaseException):
    """A close sequence tried to leave the book LESS hedged than it found it.

    **The invariant (D2):** while a SHORT leg still carries open quantity, its
    LONG hedge is never sold. Selling it converts a bounded spread into an
    unbounded naked short — and in the incident that produced this class, into
    a naked short under a record marked `closed`: not in `get_open_trades()`,
    not in `get_frozen_trades()`, no stop, no monitor, nothing that would ever
    look at it again.

    Asserted at the BROKER, not at the call site, so every fixture and every
    replay checks it for free and a future edit to any close path trips it
    without anyone having to remember to test for it (M14 §4 argues for exactly
    this placement).

    `BaseException`, not `Exception`, deliberately: `close_spread` and
    `close_fh_position` wrap the whole sequence in `except Exception`, so an
    ordinary exception here would be swallowed, logged as "EXCEPTION during
    close" and presented to the test as a tidy freeze — hiding the violation
    behind the very handler that exists to contain surprises. Same reasoning as
    the production-write rails in `conftest.py`
    ([[feedback_tests_must_not_touch_production]]).
    """


class ReduceOnlyViolation(BaseException):
    """A RECOVERY order tried to move a position AWAY from zero.

    **M14 §4, the invariant the whole recovery sweep rests on:** the sweep can
    never increase the absolute quantity of any leg. Every order it places
    moves a position toward zero.

    The design names it "implement first, test hardest" for a specific reason.
    Recovery is the only automation in this system that places orders at a
    position the machine has already failed to close once, without a human in
    the loop, on a record whose state it inferred rather than observed. The
    action space is what keeps that safe, and this is the wall around it. An
    entry-shaped order on that path — most temptingly "re-buy the long to
    restore the hedge" — is rejected categorically: it increases premium at
    risk, re-opens a position a stop already wanted closed, and makes the
    invariant untestable.

    **`reduce_only` is opt-IN per fixture, exactly like `hedge_pairs`**, and
    for the same reason: an ENTRY legitimately increases a position, so an
    always-on rule would condemn `bcs/entry_executor.py` for doing its job.
    The sweep's tests arm it; `test_the_recovery_sweep_arms_the_invariant`
    keeps them honest about arming it.

    `BaseException`, not `Exception` — `close_spread` and `close_fh_position`
    wrap their sequences in `except Exception`, so an ordinary exception here
    would be swallowed and presented to the test as a tidy freeze, hiding the
    violation behind the handler that exists to contain surprises. Same
    reasoning as its sibling above.
    """


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
                 fill_policy=None, tag='BCS_MON', hedge_pairs=None,
                 reduce_only=False):
        #: M14 §4. When armed, every order must move a position toward zero.
        #: Opt-in because an ENTRY legitimately opens one; see
        #: `ReduceOnlyViolation`.
        self.reduce_only = reduce_only
        self.reduce_only_violations = []
        #: [(short_symbol, long_symbol)] — the hedge relationships this fixture
        #: wants policed. Declared rather than inferred, because an ENTRY
        #: legitimately buys the long while the short is open; only a CLOSE
        #: sequence is bound by the invariant. See `HedgeInvariantViolation`.
        self.hedge_pairs = [tuple(p) for p in (hedge_pairs or [])]
        self.hedge_violations = []
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
        self._check_hedge_invariant(kw)
        self._check_reduce_only(kw)
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

    # -- invariants ---------------------------------------------------------
    def _check_hedge_invariant(self, kw):
        """Refuse, loudly, to sell a hedge that is still hedging something.

        Fires on the ORDER, before it is recorded or filled, so `placed` never
        contains the forbidden order and a test cannot accidentally assert
        against a book the invariant already condemned.
        """
        if not self.hedge_pairs or kw.get('transaction_type') != 'SELL':
            return
        sym = kw.get('tradingsymbol')
        for short_sym, long_sym in self.hedge_pairs:
            if sym != long_sym:
                continue
            short_qty = self.net_qty(short_sym)
            if short_qty < 0:
                msg = (f"HEDGE INVARIANT VIOLATED: SELL {sym} x "
                       f"{kw.get('quantity')} while {short_sym} is still SHORT "
                       f"{short_qty}. That leaves a NAKED SHORT — the close "
                       f"sequence has made the book LESS hedged than it found "
                       f"it.")
                self.hedge_violations.append(msg)
                raise HedgeInvariantViolation(msg)

    def _check_reduce_only(self, kw):
        """M14 §4 — refuse an order that moves a position AWAY from zero.

        Fires on the ORDER, before it is recorded or filled, so `placed` never
        contains the forbidden order and a test cannot assert against a book
        the invariant already condemned. Same placement as the hedge check.

        The rule is on the ABSOLUTE quantity, which is what makes it one rule
        instead of two: a BUY is reducing when the position is short and
        opening when it is flat or long, and the mirror holds for a SELL. A
        BUY against a flat book is the "re-buy the long to restore the hedge"
        order the design rejects categorically, and it is caught here by the
        same arithmetic that catches an oversized buyback.

        **Over-closing is a violation too.** Buying back 700 against a short of
        200 leaves the book LONG 500 — a new position, opened by a routine
        whose entire licence is that it only ever removes them. `close_leg`
        re-reads the position and sizes to it, so this can only fire if that
        stops being true, which is exactly when it needs to.
        """
        if not self.reduce_only:
            return
        sym = kw.get('tradingsymbol')
        qty = int(kw.get('quantity') or 0)
        before = self.net_qty(sym)
        after = before + (qty if kw.get('transaction_type') == 'BUY' else -qty)
        if abs(after) <= abs(before):
            return
        msg = (f"REDUCE-ONLY INVARIANT VIOLATED: {kw.get('transaction_type')} "
               f"{sym} x {qty} moves the position from {before} to {after} — "
               f"|{before}| -> |{after}|, AWAY from zero. The recovery sweep "
               f"may only ever close.")
        self.reduce_only_violations.append(msg)
        raise ReduceOnlyViolation(msg)

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

    #: The WALL CLOCK the module sees, which is a separate thing from
    #: `time.time()` and was the more important of the two all along.
    #:
    #: Found 2026-08-24, at 18:28: six B10/B11 tests had been passing all day
    #: and started failing in the evening with "ORDER CUTOFF: 18:28 > 15:20".
    #: `close_leg` refuses to place orders after LAST_ORDER_TIME and reads
    #: `datetime.now()` to decide, so patching only `time.time()` left the
    #: cutoff — and `is_market_settled`, and `is_spread_settled`, and
    #: `is_expiry_day` — reading the real clock. The suite was green only
    #: during market hours. A test that depends on when you run it is not a
    #: test.
    #:
    #: Mid-session on purpose: inside market hours, past every open buffer,
    #: well before the order cutoff. A test that wants a different instant
    #: passes `at=`.
    DEFAULT_DT = datetime(2026, 9, 15, 11, 0, 0)

    def __init__(self, start=1_600_000_000.0, at=None):
        self.now = float(start)
        self.dt = at or self.DEFAULT_DT
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
        self.dt += timedelta(seconds=sec)

    def advance(self, sec):
        self.now += sec
        self.dt += timedelta(seconds=sec)

    def install(self, monkeypatch, module):
        """Patch the module's `time` AND its wall clock.

        Subclassing `datetime` keeps `.combine`, `.strptime` and arithmetic
        working; only `.now()` is redirected.
        """
        monkeypatch.setattr(module.time, 'time', self.time)
        monkeypatch.setattr(module.time, 'sleep', self.sleep)
        clock = self

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock.dt

        class _D(date):
            @classmethod
            def today(cls):
                return clock.dt.date()

        if hasattr(module, 'datetime'):
            monkeypatch.setattr(module, 'datetime', _DT)
        if hasattr(module, 'date'):
            monkeypatch.setattr(module, 'date', _D)
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
        self._corrupt = {}
        self.calls = []          # [(method, args, kwargs)]

    # -- recording ----------------------------------------------------------
    def _rec(self, name, *a, **k):
        self.calls.append((name, a, k))

    def called(self, name):
        return [c for c in self.calls if c[0] == name]

    # -- reads --------------------------------------------------------------
    # LIVE REFERENCES, exactly like the real stores. The first version copied,
    # which made the fake SAFER than production and therefore blind to a whole
    # class of bug: `_load_all_trades` relies on `tagged = dict(t)` to keep its
    # tags out of the persisted dicts, and a mutation removing that copy
    # survived a test built on the copying fake. A fake that cannot reproduce
    # production's aliasing cannot test the code that guards against it.
    def get_open_trades(self):
        return [t for t in self.trades if t.get('status') == 'open']

    def get_closing_trades(self):
        return [t for t in self.trades if t.get('status') == 'closing']

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

    # -- B7 quarantine surface (mirrors LockedStoreMixin) --------------------
    #
    # `alert_store_corruption` swallows AttributeError per store, so a double
    # missing these would make the monitor log a warning and carry on — the
    # test would pass while proving nothing about the quarantine path.

    def read_corruption_marker(self):
        return dict(self._corrupt)

    def corruption_due_for_alert(self):
        return dict(self._corrupt)

    def note_corruption_alerted(self):
        self._rec('note_corruption_alerted')

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

    # ── The state machine, pinned to the STRICTEST real store ───────────────
    #
    # `feedback_fake_must_not_be_safer_than_production`, sixth instance, and
    # this one cost more than the others: `update_trade_exit` below set
    # `status='closed'` UNCONDITIONALLY, so every replay booked an exit that
    # the real `ZebraStore.mark_exited` would have refused outright. The
    # `begin_close -> update_trade_exit` cycle — the entire live close path —
    # was green in the harness and raised on every call in production.
    #
    # The three real stores disagree about how strict they are, so the fake
    # takes the STRICTEST of them, per method:
    #
    #   begin_close           bcs: from 'open' only     zebra: from 'entered' only
    #   update_trade_exit     bcs: NO CHECK AT ALL      zebra: 'entered'/'closing'
    #   recover_closing_trade bcs: from 'closing' only  zebra: from 'closing' only
    #
    # 'open' and 'entered' are the same fact in two vocabularies (bcs vs
    # zebra), so both are accepted; nothing else is. Refusing what only the
    # LAXEST store permits is the point — a fake calibrated to the laxest
    # store proves nothing about the strictest one, which is the one on the
    # money path.
    #
    # `test_store_contract.py` drives this table against the real stores and
    # fails if the fake ever drifts back to being more permissive.

    #: THE SAME TABLE PRODUCTION USES, imported rather than restated.
    #:
    #: These were four literal tuples, kept in step with the real stores by
    #: `test_store_contract.py` noticing when they drifted. That worked, and it
    #: is one copy more than the invariant needs: `common/store_contract.py`
    #: now holds the rules and every store -- including this fake -- asks it.
    #: A fake cannot be laxer than production when both read one table.
    #:
    #: `ANY_ROLES`, because this double stands in for the cohort store as well
    #: as for the BCS family: `bcs/tests/replay.py` hands it to
    #: `_open_zebra_store`, so it must recognise 'entered' and 'exited' too.
    ROLES = store_contract.ANY_ROLES

    def _allows(self, method, status):
        return store_contract.allows(method, status, self.ROLES)

    def update_trade_exit(self, trade_id, exit_data):
        # Signature MIRRORS bcs/trade_store.py:413 — a positional dict, not
        # kwargs. A double that accepts a looser signature than the real thing
        # lets a genuine TypeError pass in tests and blow up in production.
        self._rec('update_trade_exit', trade_id, exit_data)
        t = self._find(trade_id)
        if not self._allows(store_contract.UPDATE_TRADE_EXIT,
                            t.get('status')):
            # ValueError, matching every real store since 2026-08-30. A
            # double-book is the thing the status check exists to stop, so
            # it must be as loud here as it is in production.
            raise ValueError(store_contract.refusal(
                store_contract.UPDATE_TRADE_EXIT, t.get('status'),
                trade_id, self.ROLES))
        t.update(exit_data)
        t['status'] = 'closed'

    def begin_close(self, trade_id, reason):
        self._rec('begin_close', trade_id, reason)
        t = self._find(trade_id)
        if not self._allows(store_contract.BEGIN_CLOSE, t.get('status')):
            # False, not an exception: "somebody else got there first" is the
            # normal answer in both real stores and the caller branches on it.
            return False
        t['status'] = 'closing'
        t['close_reason'] = reason
        return True

    def recover_closing_trade(self, trade_id):
        self._rec('recover_closing_trade', trade_id)
        t = self._find(trade_id)
        if not self._allows(store_contract.RECOVER_CLOSING,
                            t.get('status')):
            return False
        t['status'] = 'open'
        t.pop('close_reason', None)
        return True

    def begin_recovery(self, trade_id, reason):
        """M14 - mirrors the real stores: ONLY from `partial_close`.

        A fake that accepted this from any state would certify a sweep that
        re-locks an open trade or orders on a booked one. `test_store_contract`
        pins the whole table; this is the half that has to be right for the
        pin to mean anything.
        """
        self._rec('begin_recovery', trade_id, reason)
        t = self._find(trade_id)
        if not self._allows(store_contract.BEGIN_RECOVERY, t.get('status')):
            return False
        t['status'] = 'closing'
        t['close_reason'] = reason
        return True

    def get_frozen_trades(self):
        return [t for t in self.trades if t.get('status') == 'partial_close']

    def get_residue_trades(self):
        #: The fake's book is the BCS family's vocabulary, so 'closed' is the
        #: terminal status here. The contract test pins that the real stores
        #: agree with this on their own names.
        return [t for t in self.trades
                if t.get('status') == 'closed'
                and (t.get('reconcile_residue') or {}).get('state') == 'open']

    def get_entry_residue_trades(self):
        #: NO status filter, matching every real store: an entry residue is a
        #: leg the entry left behind, which can sit on a record in any state --
        #: including one that never became a position because nothing filled.
        return [t for t in self.trades
                if (t.get('entry_residue') or {}).get('state') == 'open']

    def set_trade_status(self, trade_id, status, **extra_fields):
        self._rec('set_trade_status', trade_id, status, **extra_fields)
        t = self._find(trade_id)
        t['status'] = status
        t.update(extra_fields)


# ── Telegram ─────────────────────────────────────────────────────────────────

class TelegramSpy:
    """Records what the monitor tried to send, THROUGH the real alert policy.

    `sent` holds only what `bcs.alert_policy` would actually deliver, and
    `suppressed` holds what it withheld, because a spy that captures
    everything is a fake that is LAXER than production — the exact shape
    `feedback_fake_must_not_be_safer_than_production` was written about, only
    inverted. A test asserting "the owner was told" must fail when the policy
    would have silenced the message.

    `offered` keeps every (class, message) pair regardless, so a test can still
    tell "the monitor never noticed" apart from "the monitor noticed and the
    policy withheld it" — two states that both leave `sent` empty and need
    opposite fixes (feedback_never_asked_is_not_failed).
    """

    def __init__(self):
        self.sent = []
        self.suppressed = []
        self.offered = []

    def __call__(self, msg, alert_class=None):
        from bcs import alert_policy
        self.offered.append((alert_class, msg))
        ok, _why = alert_policy.should_send(alert_class)
        if not ok:
            self.suppressed.append((alert_class, msg))
            return True
        self.sent.append(msg)
        return True

    def offered_containing(self, needle):
        return [m for _c, m in self.offered if needle in m]

    def install(self, monkeypatch, module):
        monkeypatch.setattr(module, 'send_telegram', self)
        return self

    def containing(self, needle):
        return [m for m in self.sent if needle in m]

    def any(self, needle):
        return bool(self.containing(needle))

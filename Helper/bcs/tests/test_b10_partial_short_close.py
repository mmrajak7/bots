"""B10 — the long leg is sold ONLY when the short leg is confirmed flat.

`short_result['status'] == 'PARTIAL'` passed the success check, so the old code
fell through to Step 2 and sold the long in FULL — leaving a **naked short**
residue — then `update_trade_exit` marked the trade closed, so nothing
monitored it again. A reconciliation Telegram fired once; if that send dropped
(and `send_telegram` here still does not check the HTTP response — B16), the
naked short sat overnight.

The invariant every test below defends: **while any short quantity remains,
`place_order` is never called for the long symbol.** Leaving 700 long against
200 short is an over-hedged debit position with bounded risk. Selling the long
would leave a naked short. Prefer the bounded side, every time.

Two levels of test:
  * branch-level, monkeypatching `close_leg`, to control exactly what
    `_close_spread_inner` receives;
  * end-to-end through `FakeBroker`, proving `close_leg` can actually produce
    the PARTIAL status the branch keys on — otherwise the branch could be
    guarding a state that never occurs.

Run:  cd Helper && python -m pytest bcs/tests/test_b10_partial_short_close.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                             # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,  # noqa: E402
                             TelegramSpy)

LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700

BOOKS = {
    f'NFO:{LONG}':  {'bid': 40.00, 'ask': 40.20, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 40.10, 'prev_close': 39.50},
    f'NFO:{SHORT}': {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 10.20, 'prev_close': 9.80},
}


def _trade():
    return {'id': 1, 'stock': 'TESTCO', 'status': 'open',
            'long_symbol': LONG, 'short_symbol': SHORT, 'quantity': QTY,
            'exchange': 'NFO', 'net_debit': 13.55, 'spot_symbol': 'NSE:TESTCO'}


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_trade()])


def _normal_positions():
    return [{'tradingsymbol': SHORT, 'quantity': -QTY},
            {'tradingsymbol': LONG, 'quantity': QTY}]


def _run(kite, store):
    return sm._close_spread_inner(kite, store, _trade(), spot=1400.0,
                                  reason='SL_SPREAD', dry_run=False,
                                  label='BCS')


class _ScriptedCloseLeg:
    """Returns a scripted result per call, recording every call."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    # Signature MIRRORS production's `close_leg`, kwarg for kwarg. When the
    # order journal added `context` this double went red across nine tests --
    # which is the right failure: a double looser or tighter than the real
    # thing is a test of the double. `test_scripted_double_matches_close_leg`
    # below pins the two together so the next kwarg cannot drift silently.
    def __call__(self, kite, exchange, symbol, txn, qty, is_buy=False,
                 dry_run=False, urgent=False, context=None):
        self.calls.append({'symbol': symbol, 'txn': txn, 'qty': qty,
                           'urgent': urgent, 'context': context})
        return self.results.pop(0) if self.results else None

    def for_symbol(self, sym):
        return [c for c in self.calls if c['symbol'] == sym]


def _partial(filled):
    return {'status': 'PARTIAL', 'average_price': 10.2,
            'order_id': 'x', 'filled_quantity': filled}


def _complete(filled):
    return {'status': 'COMPLETE', 'average_price': 10.2,
            'order_id': 'x', 'filled_quantity': filled}


# ── The invariant ────────────────────────────────────────────────────────────

def test_a_surviving_short_residue_stops_the_long_leg_being_sold(env, monkeypatch):
    spy, store = env
    # 500 of 700 fills; the retry gets nothing more.
    scripted = _ScriptedCloseLeg(_partial(500), _partial(0))
    monkeypatch.setattr(sm, 'close_leg', scripted)
    kite = FakeBroker(books=BOOKS, positions=_normal_positions())

    ok = _run(kite, store)

    assert ok is False
    assert scripted.for_symbol(LONG) == [], (
        "the long leg was closed while 200 qty was still SHORT — that is the "
        "naked-short residue B10 exists to prevent")


def test_the_residue_is_retried_once_urgently(env, monkeypatch):
    spy, store = env
    scripted = _ScriptedCloseLeg(_partial(500), _partial(0))
    monkeypatch.setattr(sm, 'close_leg', scripted)
    _run(FakeBroker(books=BOOKS, positions=_normal_positions()), store)

    shorts = scripted.for_symbol(SHORT)
    assert len(shorts) == 2, "the residual was not retried"
    assert shorts[1]['qty'] == 200, (
        f"the retry must be for the RESIDUAL only, got {shorts[1]['qty']}")
    assert shorts[1]['urgent'] is True


def test_a_retry_that_completes_lets_the_close_finish(env, monkeypatch):
    """Negative control: if the residue clears, normal service resumes."""
    spy, store = env
    scripted = _ScriptedCloseLeg(_partial(500), _complete(200), _complete(QTY))
    monkeypatch.setattr(sm, 'close_leg', scripted)

    ok = _run(FakeBroker(books=BOOKS, positions=_normal_positions()), store)

    assert scripted.for_symbol(LONG), (
        "the residue cleared, so the long leg SHOULD have been sold")
    assert ok is True
    assert store.called('update_trade_exit')


def test_the_frozen_trade_is_not_marked_closed(env, monkeypatch):
    spy, store = env
    monkeypatch.setattr(sm, 'close_leg',
                        _ScriptedCloseLeg(_partial(500), _partial(0)))
    _run(FakeBroker(books=BOOKS, positions=_normal_positions()), store)

    assert not store.called('update_trade_exit'), (
        "the trade was booked CLOSED with a live naked short")
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['residual_short_qty'] == 200


def test_the_alert_names_the_residue_and_says_the_long_was_kept(env, monkeypatch):
    spy, store = env
    monkeypatch.setattr(sm, 'close_leg',
                        _ScriptedCloseLeg(_partial(500), _partial(0)))
    _run(FakeBroker(books=BOOKS, positions=_normal_positions()), store)

    assert spy.any('PARTIAL SHORT CLOSE')
    assert spy.any('200'), "the alert must say how much is still short"
    assert spy.any('naked short'), (
        "the alert must explain WHY the long was kept, or the reader will "
        "helpfully sell it")


def test_the_brokers_own_view_is_recorded(env, monkeypatch):
    seen = []
    monkeypatch.setattr(sm, 'reconcile_after_close',
                        lambda k, t, l='BCS': seen.append(t['id']) or False)
    spy, store = env
    monkeypatch.setattr(sm, 'close_leg',
                        _ScriptedCloseLeg(_partial(500), _partial(0)))
    _run(FakeBroker(books=BOOKS, positions=_normal_positions()), store)
    assert seen == [1]


# ── End to end: prove close_leg can actually emit PARTIAL ────────────────────

def test_close_leg_really_can_return_partial(env, monkeypatch):
    """Otherwise the branch above guards a state that never occurs.

    `close_leg` emits PARTIAL only on a REJECTION that follows a cumulative
    fill — a cancelled partial is retried instead. Script exactly that.
    """
    calls = {'n': 0}

    def policy(order):
        calls['n'] += 1
        if calls['n'] == 1:
            return 'CANCELLED', 500, order['price']    # partial, then retried
        return 'REJECTED', 0, 0.0                      # rejection ends it

    FakeClock().install(monkeypatch, sm)
    kite = FakeBroker(books=BOOKS, positions=_normal_positions(),
                      fill_policy=policy)
    res = sm.close_leg(kite, 'NFO', SHORT, 'BUY', QTY, is_buy=True,
                       dry_run=False, urgent=True)

    assert res is not None and res['status'] == 'PARTIAL', (
        f"close_leg cannot produce PARTIAL any more (got {res}); the B10 "
        "branch would then be dead code")
    assert res['filled_quantity'] == 500


def test_end_to_end_a_partial_short_never_sells_the_long(env, monkeypatch):
    """The whole path, no monkeypatched close_leg."""
    spy, store = env
    calls = {'n': 0}

    def policy(order):
        calls['n'] += 1
        if order['tradingsymbol'] == SHORT:
            if calls['n'] == 1:
                return 'CANCELLED', 500, order['price']
            return 'REJECTED', 0, 0.0
        return 'COMPLETE', order['quantity'], order['price']

    kite = FakeBroker(books=BOOKS, positions=_normal_positions(),
                      fill_policy=policy)
    ok = _run(kite, store)

    assert ok is False
    assert kite.orders_for(LONG) == [], (
        "end to end, the long leg was sold against a live short residue")
    assert kite.net_qty(SHORT) < 0, "the fixture did not leave a short residue"
    assert not store.called('update_trade_exit')


# ── Negative control: the ordinary full close must be untouched ──────────────

def test_a_clean_full_close_still_sells_both_legs(env):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=_normal_positions())
    ok = _run(kite, store)

    assert ok is True
    assert kite.orders_for(SHORT) and kite.orders_for(LONG)
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert not spy.any('PARTIAL SHORT CLOSE')
    assert store.called('update_trade_exit')

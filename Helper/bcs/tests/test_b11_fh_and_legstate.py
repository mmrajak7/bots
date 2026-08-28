"""B11, the two halves the first mutation run found untested.

Writing the BCS tests first left two guards green under mutation — not because
they were unreachable, but because nothing exercised them at all:

* `_close_fh_inner`'s `all_flat` had the same `>= 0` defect on all FOUR legs,
  and Fallen Hero is the worse case: the short call is the NAKED leg, so a flip
  there is an uncovered position the monitor would mark closed and stop
  watching.
* `_leg_state`, the startup verification helper, reported a FLIPPED leg as
  "MISSING" — which sends the reader hunting for an unfilled order instead of
  looking at a live position facing the wrong way.

Run:  cd Helper && python -m pytest bcs/tests/test_b11_fh_and_legstate.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                             # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,  # noqa: E402
                             TelegramSpy)

SC, SP, LP, LC = ('T26SEP3000CE', 'T26SEP2600PE',
                  'T26SEP2550PE', 'T26SEP3200CE')
QTY = 400

BOOKS = {f'NFO:{s}': {'bid': 10.0, 'ask': 10.2, 'bid_qty': 800,
                      'ask_qty': 800, 'ltp': 10.1, 'prev_close': 10.0}
         for s in (SC, SP, LP, LC)}


def _fh(with_long_call=True):
    t = {'id': 7, 'stock': 'TESTCO', 'status': 'open', 'quantity': QTY,
         'exchange': 'NFO', 'short_call_symbol': SC, 'short_put_symbol': SP,
         'long_put_symbol': LP, 'spot_symbol': 'NSE:TESTCO',
         'total_credit': 97.75, 'breakeven': 3097.75}
    t['long_call_symbol'] = LC if with_long_call else None
    return t


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_fh()])


def _pos(sc=-QTY, sp=-QTY, lp=QTY, lc=QTY):
    return [{'tradingsymbol': SC, 'quantity': sc},
            {'tradingsymbol': SP, 'quantity': sp},
            {'tradingsymbol': LP, 'quantity': lp},
            {'tradingsymbol': LC, 'quantity': lc}]


def _run(kite, store):
    return sm._close_fh_inner(kite, store, _fh(), spot=3050.0,
                              reason='SL_SPOT', dry_run=False)


# ── FH: every leg can flip, and each must be caught ──────────────────────────

@pytest.mark.parametrize('kwargs,needle', [
    ({'sc': 1200}, 'SHORT CALL'),    # the naked leg flipped long — worst case
    ({'sp': 400}, 'SHORT PUT'),
    ({'lp': -400}, 'LONG PUT'),
    ({'lc': -400}, 'LONG CALL'),
])
def test_a_flipped_fh_leg_places_no_orders(env, kwargs, needle):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=_pos(**kwargs))
    ok = _run(kite, store)

    assert ok is False
    assert kite.placed == [], "orders were placed on top of a flipped FH leg"
    assert not store.called('update_trade_exit'), "booked closed while flipped"
    assert store.trades[0]['status'] == 'partial_close'
    assert spy.any(needle), f"the alert does not name the {needle} leg"


def test_a_flipped_fh_short_call_says_the_leg_is_naked(env):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=_pos(sc=1200))
    _run(kite, store)
    assert spy.any('NAKED'), (
        "an FH short call facing the wrong way is an uncovered position; the "
        "alert must say so")
    assert spy.any('+1200')


def _tagged_fh_fills(kite):
    """The four BCS_MON fills a real all-flat FH close would have left.

    See the BCS twin in `test_b11_flipped_position.py`: since 2026-08-27 the
    all-flat branch books only when OUR OWN orders can price every leg, and a
    fixture with an empty order book now exercises the REFUSAL instead of the
    flipped-vs-flat question this file is about.
    """
    for i, (sym, txn) in enumerate(((SC, 'BUY'), (SP, 'BUY'),
                                    (LP, 'SELL'), (LC, 'SELL'))):
        kite.order_book.append({
            'order_id': str(900 + i), 'tradingsymbol': sym,
            'transaction_type': txn, 'status': 'COMPLETE',
            'average_price': 10.0 + i, 'tag': 'BCS_MON',
            'order_timestamp': '2026-09-21 14:30:0%d' % i})
    return kite


def test_a_genuinely_flat_fh_book_is_still_marked_closed(env):
    """Negative control: all four legs at exactly zero."""
    spy, store = env
    kite = _tagged_fh_fills(
        FakeBroker(books=BOOKS, positions=_pos(sc=0, sp=0, lp=0, lc=0)))
    ok = _run(kite, store)

    assert ok is True
    assert kite.placed == []
    assert not spy.any('FLIPPED')
    assert store.called('update_trade_exit')


def test_a_normal_fh_book_still_closes(env):
    """Negative control: the ordinary case must not be swallowed."""
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=_pos())
    ok = _run(kite, store)

    assert not spy.any('FLIPPED')
    assert kite.orders_for(SC), "the naked short call was never bought back"
    assert ok is True


# ── _leg_state ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('qty,want_long,expected', [
    (700, True, 'OK'),
    (-700, False, 'OK'),
    (0, True, 'MISSING'),
    (0, False, 'MISSING'),
    (-700, True, 'FLIPPED -700'),      # long leg gone short
    (2100, False, 'FLIPPED +2100'),    # short leg gone long — Feb-2026
])
def test_leg_state_classifies_every_case(qty, want_long, expected):
    positions = [{'tradingsymbol': 'X', 'quantity': qty}]
    assert sm._leg_state(positions, 'X', want_long=want_long) == expected


def test_an_absent_symbol_is_missing_not_flipped():
    assert sm._leg_state([], 'X', want_long=True) == 'MISSING'


def test_flipped_is_never_reported_as_missing():
    """The distinction is the whole point of the helper.

    'MISSING' and 'FLIPPED' need opposite responses — one is an order that did
    not fill, the other is a live position facing the wrong way. Reporting the
    second as the first is what the startup check used to do.
    """
    st = sm._leg_state([{'tradingsymbol': 'X', 'quantity': 2100}],
                       'X', want_long=False)
    assert st != 'MISSING'
    assert st.startswith('FLIPPED')
    assert '+2100' in st, "the signed quantity must survive into the report"


# ── B10 (FH twin): a partial short-call close must not sell the hedge ────────
#
# Added after the B10 mutation run skipped the FH anchor and I noticed the FH
# branch had no coverage at all — the same gap this file was created to close.
# This is the worst instance of the B10 shape anywhere in the module: selling
# the long call while part of the short call is open REMOVES THE HEDGE FROM A
# NAKED SHORT.

class _ScriptedCloseLeg:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    # Mirrors production's `close_leg` signature -- see the note on the
    # twin in test_b10_partial_short_close.py.
    def __call__(self, kite, exchange, symbol, txn, qty, is_buy=False,
                 dry_run=False, urgent=False, context=None, attempts=None,
                 allow_pay_through=True):
        self.calls.append({'symbol': symbol, 'txn': txn, 'qty': qty,
                           'context': context})
        return self.results.pop(0) if self.results else None

    def for_symbol(self, sym):
        return [c for c in self.calls if c['symbol'] == sym]


def _p(filled):
    return {'status': 'PARTIAL', 'average_price': 10.2, 'order_id': 'x',
            'filled_quantity': filled}


def _c(filled):
    return {'status': 'COMPLETE', 'average_price': 10.2, 'order_id': 'x',
            'filled_quantity': filled}


def test_a_partial_short_call_does_not_sell_the_long_call(env, monkeypatch):
    spy, store = env
    scripted = _ScriptedCloseLeg(_p(300), _p(0))     # 300 of 400, retry gets 0
    monkeypatch.setattr(sm, 'close_leg', scripted)

    ok = _run(FakeBroker(books=BOOKS, positions=_pos()), store)

    assert ok is False
    assert scripted.for_symbol(LC) == [], (
        "the long call was sold while 100 qty of the short call was still "
        "open — that strips the hedge off a naked short")
    assert not store.called('update_trade_exit')
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['residual_short_call_qty'] == 100


def test_the_fh_partial_alert_explains_the_hedge(env, monkeypatch):
    spy, store = env
    monkeypatch.setattr(sm, 'close_leg', _ScriptedCloseLeg(_p(300), _p(0)))
    _run(FakeBroker(books=BOOKS, positions=_pos()), store)

    assert spy.any('PARTIAL SHORT CALL')
    assert spy.any('hedge'), (
        "the alert must say the long call is the hedge, or the reader will "
        "sell it to 'tidy up'")


def test_the_fh_residual_is_retried_for_the_remainder_only(env, monkeypatch):
    spy, store = env
    scripted = _ScriptedCloseLeg(_p(300), _p(0))
    monkeypatch.setattr(sm, 'close_leg', scripted)
    _run(FakeBroker(books=BOOKS, positions=_pos()), store)

    sc_calls = scripted.for_symbol(SC)
    assert len(sc_calls) == 2, "the short-call residual was not retried"
    assert sc_calls[1]['qty'] == 100, (
        f"retry must cover the residual only, got {sc_calls[1]['qty']}")


def test_an_fh_retry_that_clears_lets_the_close_continue(env, monkeypatch):
    """Negative control: residue cleared, so the hedge SHOULD be sold."""
    spy, store = env
    scripted = _ScriptedCloseLeg(_p(300), _c(100), _c(QTY), _c(QTY), _c(QTY))
    monkeypatch.setattr(sm, 'close_leg', scripted)

    _run(FakeBroker(books=BOOKS, positions=_pos()), store)

    assert scripted.for_symbol(LC), (
        "the short call is flat, so the long-call hedge should have been sold")

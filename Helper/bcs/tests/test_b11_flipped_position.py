"""B11 — a FLIPPED leg is not a flat leg.

`_close_spread_inner` tested `short_qty >= 0 and long_qty <= 0` for "both legs
already flat". A short leg at **+2100** — four 700-lot BUYs against −700, the
literal Feb-2026 ICICIBANK shape — satisfies `>= 0`. So the trade was booked
CLOSED with a live naked long, `reconcile_after_close` was never reached on
that branch, and nothing looks at a closed trade ever again.

These are BEHAVIOURAL, not wiring tests — the first in this package that drive
`_close_spread_inner` end to end. `test_exit_guards.py` explicitly punted on
this ("needs a full order/fill/store simulation"); `bcs/tests/fakes.py` is that
simulation. Because FakeBroker's fills MOVE its positions, an assertion that no
order was placed is an assertion about what the code actually did, not about
what the fixture author wrote down.

Run:  cd Helper && python -m pytest bcs/tests/test_b11_flipped_position.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                          # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,  # noqa: E402
                             TelegramSpy)

LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700

#: A clean, tight, two-sided book on both legs — so the close path is exercised
#: on its merits and any refusal comes from the guard under test, not from a
#: quote the reliability gate would reject anyway.
BOOKS = {
    f'NFO:{LONG}':  {'bid': 40.00, 'ask': 40.20, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 40.10, 'prev_close': 39.50},
    f'NFO:{SHORT}': {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 10.20, 'prev_close': 9.80},
}


def _trade():
    return {'id': 1, 'stock': 'TESTCO', 'status': 'open',
            'long_symbol': LONG, 'short_symbol': SHORT,
            'quantity': QTY, 'exchange': 'NFO', 'net_debit': 13.55,
            'spot_symbol': 'NSE:TESTCO'}


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    store = MemoryStore(trades=[_trade()])
    return spy, store


def _run(kite, store, reason='SL_SPREAD'):
    return sm._close_spread_inner(kite, store, _trade(), spot=1400.0,
                                  reason=reason, dry_run=False, label='BCS')


# ── The Feb-2026 shape ───────────────────────────────────────────────────────

def test_a_flipped_short_leg_places_no_orders(env):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': 2100},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    ok = _run(kite, store)

    assert ok is False
    assert kite.placed == [], (
        "orders were placed on top of a flipped leg — that is the "
        "amplification that turned a stop into a four-fill loss")


def test_a_flipped_short_leg_is_not_booked_as_closed(env):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': 2100},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    _run(kite, store)

    assert not store.called('update_trade_exit'), (
        "the trade was marked CLOSED with a live naked long")
    assert store.called('set_trade_status'), "the trade was not frozen"
    assert store.trades[0]['status'] == 'partial_close'


def test_a_flipped_short_leg_alerts_with_the_signed_quantity(env):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': 2100},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    _run(kite, store)

    assert spy.any('FLIPPED'), "no flipped-position alert"
    assert spy.any('+2100'), (
        "the alert must carry the signed quantity — 'positions missing' sends "
        "the reader looking for an unfilled order instead of a naked leg")


def test_a_flipped_leg_records_the_brokers_own_view(env, monkeypatch):
    """`reconcile_after_close` was never reached on the old branch."""
    seen = []
    monkeypatch.setattr(sm, 'reconcile_after_close',
                        lambda k, t, l='BCS': seen.append(t['id']) or False)
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': 2100},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    _run(kite, store)
    assert seen == [1], "reconcile_after_close was not called on the flip"


# ── The other three flip shapes ──────────────────────────────────────────────

@pytest.mark.parametrize('short_q,long_q,needle', [
    (2100, QTY, 'SHORT'),        # short flipped long  (Feb-2026)
    (700, QTY, 'SHORT'),         # short flipped, smaller
    (-QTY, -700, 'LONG'),        # long flipped short
    (350, -350, 'SHORT'),        # both flipped
])
def test_every_flip_shape_is_caught(env, short_q, long_q, needle):
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': short_q},
                                 {'tradingsymbol': LONG, 'quantity': long_q}])
    ok = _run(kite, store)
    assert ok is False
    assert kite.placed == []
    assert spy.any(needle)


# ── Negative controls: normal and genuinely-flat books must still work ───────

def test_a_normal_book_still_closes(env):
    """The guard must not swallow the ordinary case.

    Without this, a fix that returned False unconditionally would pass every
    test above while disabling every exit in the system.
    """
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    ok = _run(kite, store)

    assert ok is True
    assert kite.orders_for(SHORT), "the short leg was never bought back"
    assert kite.orders_for(LONG), "the long leg was never sold"
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert store.called('update_trade_exit'), "a clean close was not booked"


def test_a_genuinely_flat_book_is_still_marked_closed(env):
    """Both legs at exactly zero — the case the old branch was written for."""
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': 0},
                                 {'tradingsymbol': LONG, 'quantity': 0}])
    ok = _run(kite, store)

    assert ok is True
    assert kite.placed == [], "orders were placed against a flat book"
    assert store.called('update_trade_exit')
    assert 'ALREADY_FLAT' in str(store.called('update_trade_exit'))


def test_a_partially_exited_book_is_not_treated_as_flipped(env):
    """Short already bought back, long still held. Legitimate, not a flip —
    the long must still be sold."""
    spy, store = env
    kite = FakeBroker(books=BOOKS, positions=[{'tradingsymbol': SHORT, 'quantity': 0},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    ok = _run(kite, store)

    assert not spy.any('FLIPPED'), "a normal partial exit was called a flip"
    assert kite.orders_for(LONG), "the remaining long leg was not sold"
    assert ok is True

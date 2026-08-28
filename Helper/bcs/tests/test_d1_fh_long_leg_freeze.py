"""D1 — an FH long leg that did not sell must FREEZE, never book.

`_close_fh_inner` closes four legs in risk order: buy back the short call, sell
the long call, buy back the short put, sell the long put. Two of those four —
both LONG legs — used to log a warning on failure and **fall through** to the
booking arithmetic with `fills[<leg>]` still sitting at its `0.0` seed. The
result was the worst state this system knows how to produce:

  * a live long option at the broker under a record marked CLOSED, so nothing
    monitors it, nothing re-alerts it, and its stops are dead;
  * `close_cost = SC + SP - LC - LP` computed with a long leg at 0.00, which is
    not a missing price but the WORST price a long option can fetch — so the
    booked P&L is wrong, on a sale that never happened.

The `fh_unpriced` guard immediately below those steps catches `None` and
structurally cannot catch this: `0.0 is not None`. Same family as N1 and the
`exit_value` key mismatch, both already fixed on the BCS path — this was the
copy nobody opened ([[feedback_copy_pasted_modules_fix_once]]).

The negative control matters as much as the regressions: an FH close where the
book genuinely pays 0.00 for every leg must still book normally. Over-correcting
"never trust a zero" into "never book a zero" would refuse real closes, which is
the inverse-review failure `feedback_guards_need_the_inverse_review` is about.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_d1_fh_long_leg_freeze.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import alert_policy                                      # noqa: E402
from bcs import spread_monitor as sm                              # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,   # noqa: E402
                             TelegramSpy)

SC, SP, LP, LC = ('T26SEP3000CE', 'T26SEP2600PE',
                  'T26SEP2550PE', 'T26SEP3200CE')
QTY = 400
CREDIT = 97.75

BOOKS = {f'NFO:{s}': {'bid': 10.0, 'ask': 10.2, 'bid_qty': 800,
                      'ask_qty': 800, 'ltp': 10.1, 'prev_close': 10.0}
         for s in (SC, SP, LP, LC)}

#: Distinct per leg so a test can prove WHICH price landed on the record.
FILL = {SC: 12.0, LC: 3.0, SP: 8.0, LP: 2.0}


def _fh(with_long_call=True):
    t = {'id': 7, 'stock': 'TESTCO', 'status': 'open', 'quantity': QTY,
         'exchange': 'NFO', 'short_call_symbol': SC, 'short_put_symbol': SP,
         'long_put_symbol': LP, 'spot_symbol': 'NSE:TESTCO',
         'total_credit': CREDIT, 'breakeven': 3097.75}
    t['long_call_symbol'] = LC if with_long_call else None
    return t


def _pos(sc=-QTY, sp=-QTY, lp=QTY, lc=QTY):
    return [{'tradingsymbol': SC, 'quantity': sc},
            {'tradingsymbol': SP, 'quantity': sp},
            {'tradingsymbol': LP, 'quantity': lp},
            {'tradingsymbol': LC, 'quantity': lc}]


def _ok(price, filled=QTY):
    return {'status': 'COMPLETE', 'average_price': price,
            'order_id': 'x', 'filled_quantity': filled}


def _rejected():
    """What `close_leg` really returns on a rejection.

    `average_price: 0.0` with `filled_quantity: 0` — the literal zero that D1
    let through. Nothing filled; the zero is the absence of a price.
    """
    return {'status': 'REJECTED', 'average_price': 0.0,
            'order_id': 'x', 'filled_quantity': 0}


class _LegScript:
    """A `close_leg` stand-in scripted per symbol.

    Mirrors production's signature (see the twin in
    `test_b11_fh_and_legstate.py`); anything not scripted fills at its FILL
    price, so a test only has to say what goes WRONG.
    """

    def __init__(self, **failures):
        self.failures = dict(failures)     # symbol -> result (or None)
        self.calls = []

    def __call__(self, kite, exchange, symbol, txn, qty, is_buy=False,
                 dry_run=False, urgent=False, context=None, attempts=None,
                 allow_pay_through=True):
        self.calls.append({'symbol': symbol, 'txn': txn, 'qty': qty})
        if symbol in self.failures:
            return self.failures[symbol]
        return _ok(FILL[symbol])

    def for_symbol(self, sym):
        return [c for c in self.calls if c['symbol'] == sym]


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_fh()])


def _run(store, script, monkeypatch, positions=None, with_long_call=True):
    monkeypatch.setattr(sm, 'close_leg', script)
    kite = FakeBroker(books=BOOKS, positions=positions or _pos())
    return sm._close_fh_inner(kite, store, _fh(with_long_call),
                              spot=3050.0, reason='SL_SPOT', dry_run=False)


# ── The reported defect: the long PUT (Step 4) ──────────────────────────────

@pytest.mark.parametrize('failure', [None, _rejected()],
                         ids=['unfilled', 'rejected'])
def test_a_failed_long_put_sell_freezes_and_does_not_book(env, monkeypatch,
                                                          failure):
    spy, store = env
    ok = _run(store, _LegScript(**{LP: failure}), monkeypatch)

    assert ok is False
    assert not store.called('update_trade_exit'), (
        "the trade was booked CLOSED while the long put is still live at the "
        "broker — D1")
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['close_failed_leg'] == 'long_put'


def test_a_failed_long_put_never_lands_a_zero_price_on_the_record(env,
                                                                  monkeypatch):
    """`0.0` is not an observation. The record must simply not carry one."""
    spy, store = env
    _run(store, _LegScript(**{LP: None}), monkeypatch)

    rec = store.trades[0]
    assert 'long_put_fill' not in rec, (
        f"a price was recorded for a leg that never sold: "
        f"long_put_fill={rec.get('long_put_fill')!r}")
    assert 'exit' not in rec or rec.get('exit') is None
    assert 'pnl_per_share' not in rec, "P&L computed from an unsold leg"


def test_the_fills_that_WERE_observed_survive_the_freeze(env, monkeypatch):
    """After the freeze the record is the only place those prices exist.

    The BCS twin carries `short_fill` through its `close_failed_leg='long'`
    freeze for exactly this reason. Losing them means a human finishing the
    close by hand cannot price the half that already traded.

    The status assertion is load-bearing: `MemoryStore.update_trade_exit`
    folds the exit dict onto the trade, exactly as the real stores do, so
    these three keys are ALSO present on a wrongly-booked record. Without it
    this test passes against the defect it exists to pin.
    """
    spy, store = env
    _run(store, _LegScript(**{LP: None}), monkeypatch)

    rec = store.trades[0]
    assert rec['status'] == 'partial_close'
    assert not store.called('update_trade_exit')
    assert rec['short_call_fill'] == FILL[SC]
    assert rec['long_call_fill'] == FILL[LC]
    assert rec['short_put_fill'] == FILL[SP]


def test_the_long_put_freeze_alert_is_SAFETY_and_says_the_leg_is_live(env,
                                                                     monkeypatch):
    spy, store = env
    _run(store, _LegScript(**{LP: None}), monkeypatch)

    classes = [c for c, m in spy.offered if 'LONG PUT CLOSE FAILED' in m]
    assert classes == [alert_policy.SAFETY], (
        f"a live option under a closed record is a needs-a-human state; "
        f"classes were {classes}")
    assert spy.any('LONG PUT CLOSE FAILED')
    assert spy.any(LP), "the alert must name the symbol still at the broker"
    assert spy.any('NOTHING was booked')


# ── The sibling: the long CALL (Step 2) ─────────────────────────────────────

def test_a_failed_long_call_sell_freezes_and_does_not_book(env, monkeypatch):
    spy, store = env
    ok = _run(store, _LegScript(**{LC: None}), monkeypatch)

    assert ok is False
    assert not store.called('update_trade_exit')
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['close_failed_leg'] == 'long_call'
    assert 'long_call_fill' not in store.trades[0]
    assert store.trades[0]['short_call_fill'] == FILL[SC]


def test_a_failed_long_call_stops_the_sequence(env, monkeypatch):
    """No more orders after a refusal.

    Stopping is safe here: the short put is left against the long put, i.e.
    the ORIGINAL bull put spread, bounded by its width. Continuing would mean
    firing further orders into a book that has just refused one — the shape
    the FLIPPED guard and the 2026-02-18 incident both argue against.
    """
    spy, store = env
    script = _LegScript(**{LC: None})
    _run(store, script, monkeypatch)

    assert script.for_symbol(SP) == [], "the short put was traded after a refusal"
    assert script.for_symbol(LP) == [], "the long put was traded after a refusal"


def test_the_long_call_freeze_alert_names_what_is_still_open(env, monkeypatch):
    spy, store = env
    _run(store, _LegScript(**{LC: None}), monkeypatch)

    assert spy.any('LONG CALL CLOSE FAILED')
    assert spy.any('Still open')
    assert spy.any('SHORT PUT'), (
        "freezing at Step 2 leaves the short put open — the reader has to be "
        "told, or they cannot judge the risk they are being handed")


def test_an_unhedged_short_put_at_the_long_call_freeze_is_called_out(env,
                                                                    monkeypatch):
    """The one genuinely uncomfortable combination, said out loud.

    If the long put was ALREADY flat before this close began (closed
    externally), freezing at Step 2 leaves the short put with no hedge. This
    code did not create that state — the book was anomalous on arrival — but
    the alert must not describe it as if it were the ordinary spread.
    """
    spy, store = env
    _run(store, _LegScript(**{LC: None}), monkeypatch,
         positions=_pos(lp=0))

    assert spy.any('UNHEDGED')


def test_a_three_leg_fh_has_no_long_call_step(env, monkeypatch):
    """Negative control for the parametrised leg: no long call, no freeze."""
    spy, store = env
    script = _LegScript()
    ok = _run(store, script, monkeypatch, positions=_pos(lc=0),
              with_long_call=False)

    assert ok is True
    assert script.for_symbol(LC) == []
    assert store.called('update_trade_exit')


# ── Negative controls: a real close must still book ─────────────────────────

def test_a_normal_fh_close_still_books(env, monkeypatch):
    spy, store = env
    ok = _run(store, _LegScript(), monkeypatch)

    assert ok is True
    assert store.called('update_trade_exit')
    exit_data = store.called('update_trade_exit')[0][1][1]
    assert exit_data['close_cost'] == pytest.approx(
        FILL[SC] + FILL[SP] - FILL[LC] - FILL[LP])
    assert exit_data['pnl_per_share'] == pytest.approx(
        CREDIT - (FILL[SC] + FILL[SP] - FILL[LC] - FILL[LP]))


def test_a_genuinely_observed_zero_still_books_normally(env, monkeypatch):
    """THE over-correction guard.

    Every leg really transacted, and every one of them really printed 0.00 —
    a worthless FH at expiry. That is an OBSERVED price, so the full credit is
    banked and the trade closes. 'Never trust a zero' would refuse a real
    close here, which is the same disease as booking one.
    """
    spy, store = env
    script = _LegScript(**{s: _ok(0.0) for s in (SC, LC, SP, LP)})
    ok = _run(store, script, monkeypatch)

    assert ok is True, "a real close at 0.00 was refused"
    assert store.called('update_trade_exit')
    exit_data = store.called('update_trade_exit')[0][1][1]
    assert exit_data['close_cost'] == 0.0
    assert exit_data['pnl_per_share'] == pytest.approx(CREDIT)
    assert store.trades[0]['status'] != 'partial_close'


def test_an_unpriceable_leg_still_refuses_rather_than_freezing(env, monkeypatch):
    """The pre-existing `fh_unpriced` path is untouched.

    `close_leg` reporting a leg CLOSED with `average_price: None` means the leg
    is FLAT and nothing of ours priced it — nothing is at risk, so that case
    stays at 'closing' for `_refuse_unpriced_close` rather than being frozen at
    partial_close by the new code. Two different states, two different answers.
    """
    spy, store = env
    script = _LegScript(**{LP: _ok(None)})
    ok = _run(store, script, monkeypatch)

    assert ok is False
    assert not store.called('update_trade_exit')
    assert store.trades[0]['status'] != 'partial_close'
    assert spy.any('CANNOT BE PRICED')

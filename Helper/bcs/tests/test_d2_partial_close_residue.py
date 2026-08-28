"""D2/D3 — a PARTIALLY closed leg is not a closed leg.

`close_leg` can return `'PARTIAL'`, and every caller's success check reads
`if not result or result['status'] not in ('COMPLETE', 'PARTIAL')` — so a
partial fill passes as success and the sequence walks on to the next leg with
part of this one still live at the broker.

**D2, the reason this file exists.** FH Step 1 (short call) had the B10
retry-then-freeze. **Step 3 (short put) had none.** So a short-put buyback
filling 300 of 400 fell through, and Step 4 sold the **FULL** long put:

    100 naked short puts, live, under a record marked `status: 'closed'`

which is the worst state this system can reach. It is not in
`get_open_trades()`, not in `get_frozen_trades()`, it has no stop, no monitor,
and nothing will ever look at it again. It also falsified the premise M14's
recovery design was written on — that our own close sequence cannot manufacture
a naked short (true for BCS, false for FH).

**D3, the same bug on the LONG legs** (FH Steps 2/4 and the BCS long sale). The
risk is bounded — it is a long option — but the residue is just as invisible
and the booked P&L is computed on the FULL quantity, i.e. the record lies about
both what is closed and what it made.

**THE GOVERNING INVARIANT, which D2 violated:** a close sequence must never
leave the book LESS hedged than it found it. It is asserted structurally in
`FakeBroker` rather than only at the call sites, so every fixture and every
replay checks it without anyone remembering to.

Negative controls carry as much weight as the regressions here: a COMPLETE fill
must still book, and a residue that the retry clears must still let the close
finish. "Never continue after a partial" would refuse real closes, which is the
inverse-review failure `feedback_guards_need_the_inverse_review` is about.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_d2_partial_close_residue.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import alert_policy                                       # noqa: E402
from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock,                # noqa: E402
                             HedgeInvariantViolation, MemoryStore,
                             TelegramSpy)
from bcs.tests.replay import Tick, run_session                     # noqa: E402

# ── Fallen Hero fixture (four legs) ─────────────────────────────────────────
SC, LC = 'T26SEP3000CE', 'T26SEP3200CE'
SP, LP = 'T26SEP2600PE', 'T26SEP2550PE'
QTY = 400
CREDIT = 97.75

FH_BOOKS = {f'NFO:{s}': {'bid': 10.0, 'ask': 10.2, 'bid_qty': 800,
                         'ask_qty': 800, 'ltp': 10.1, 'prev_close': 10.0}
            for s in (SC, LC, SP, LP)}

#: Distinct per leg so a test can prove WHICH price reached the record.
FILL = {SC: 12.0, LC: 3.0, SP: 8.0, LP: 2.0}

# ── BCS fixture (two legs) ──────────────────────────────────────────────────
B_LONG, B_SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
B_QTY = 700

BCS_BOOKS = {
    f'NFO:{B_LONG}':  {'bid': 40.00, 'ask': 40.20, 'bid_qty': 1400,
                       'ask_qty': 1400, 'ltp': 40.10, 'prev_close': 39.50},
    f'NFO:{B_SHORT}': {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400,
                       'ask_qty': 1400, 'ltp': 10.20, 'prev_close': 9.80},
}


def _fh(with_long_call=True):
    t = {'id': 7, 'stock': 'TESTCO', 'status': 'open', 'quantity': QTY,
         'exchange': 'NFO', 'short_call_symbol': SC, 'short_put_symbol': SP,
         'long_put_symbol': LP, 'spot_symbol': 'NSE:TESTCO',
         'total_credit': CREDIT, 'breakeven': 3097.75}
    t['long_call_symbol'] = LC if with_long_call else None
    return t


def _bcs():
    return {'id': 1, 'stock': 'TESTCO', 'status': 'open',
            'long_symbol': B_LONG, 'short_symbol': B_SHORT,
            'quantity': B_QTY, 'exchange': 'NFO', 'net_debit': 13.55,
            'spot_symbol': 'NSE:TESTCO'}


def _fh_pos(sc=-QTY, sp=-QTY, lp=QTY, lc=QTY):
    return [{'tradingsymbol': SC, 'quantity': sc},
            {'tradingsymbol': LC, 'quantity': lc},
            {'tradingsymbol': SP, 'quantity': sp},
            {'tradingsymbol': LP, 'quantity': lp}]


def _bcs_pos():
    return [{'tradingsymbol': B_SHORT, 'quantity': -B_QTY},
            {'tradingsymbol': B_LONG, 'quantity': B_QTY}]


def _complete(filled, price=10.2):
    return {'status': 'COMPLETE', 'average_price': price,
            'order_id': 'x', 'filled_quantity': filled}


def _partial(filled, price=10.2):
    return {'status': 'PARTIAL', 'average_price': price,
            'order_id': 'x', 'filled_quantity': filled}


class _LegScript:
    """A `close_leg` stand-in scripted PER SYMBOL, results popped in order.

    Anything unscripted fills in full at its `FILL` price, so a test only has
    to say what goes wrong. The signature mirrors production's kwarg for kwarg
    — a double looser than the real thing is a test of the double
    (`test_scripted_double_matches_close_leg` pins the twin in
    `test_b10_partial_short_close.py`).
    """

    def __init__(self, **scripts):
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.calls = []

    def __call__(self, kite, exchange, symbol, txn, qty, is_buy=False,
                 dry_run=False, urgent=False, context=None, attempts=None,
                 allow_pay_through=True):
        self.calls.append({'symbol': symbol, 'txn': txn, 'qty': qty,
                           'urgent': urgent})
        queued = self.scripts.get(symbol)
        if queued:
            return queued.pop(0)
        return _complete(qty, FILL.get(symbol, 10.2))

    def for_symbol(self, sym):
        return [c for c in self.calls if c['symbol'] == sym]


@pytest.fixture
def fh_env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_fh()])


@pytest.fixture
def bcs_env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_bcs()])


def _run_fh(store, script, monkeypatch, positions=None, with_long_call=True,
            kite=None):
    monkeypatch.setattr(sm, 'close_leg', script)
    kite = kite or FakeBroker(books=FH_BOOKS, positions=positions or _fh_pos())
    return sm._close_fh_inner(kite, store, _fh(with_long_call), spot=3050.0,
                              reason='SL_SPOT', dry_run=False)


def _run_bcs(store, script, monkeypatch, kite=None):
    if script is not None:
        monkeypatch.setattr(sm, 'close_leg', script)
    kite = kite or FakeBroker(books=BCS_BOOKS, positions=_bcs_pos())
    return sm._close_spread_inner(kite, store, _bcs(), spot=1400.0,
                                  reason='SL_SPREAD', dry_run=False,
                                  label='BCS')


# ══ D2 — the reported defect: a partial SHORT PUT buyback ═══════════════════

def test_a_partial_short_put_does_not_sell_the_long_put(fh_env, monkeypatch):
    """The headline. 300 of 400 bought back, retry gets nothing.

    Selling the long put here leaves 100 NAKED SHORT PUTS. Being over-hedged
    (400 long against 100 short) is the bounded side and we take it every time.
    """
    spy, store = fh_env
    script = _LegScript(**{SP: [_partial(300), _partial(0)]})
    ok = _run_fh(store, script, monkeypatch)

    assert ok is False
    assert script.for_symbol(LP) == [], (
        "the long put was sold while 100 qty of the short put was still open "
        "— that manufactures a naked short")


def test_the_partially_closed_trade_is_not_marked_closed(fh_env, monkeypatch):
    """A naked short under a CLOSED record is invisible to every engine."""
    spy, store = fh_env
    _run_fh(store, _LegScript(**{SP: [_partial(300), _partial(0)]}),
            monkeypatch)

    assert not store.called('update_trade_exit'), (
        "the trade was booked CLOSED with a live short-put residue — D2")
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['close_failed_leg'] == 'short_put'


def test_the_short_put_residual_quantity_is_recorded(fh_env, monkeypatch):
    """Whoever picks this up — a human or M14's sweep — needs the number."""
    spy, store = fh_env
    _run_fh(store, _LegScript(**{SP: [_partial(300), _partial(0)]}),
            monkeypatch)

    assert store.trades[0]['residual_short_put_qty'] == 100


def test_the_short_put_residue_is_retried_for_the_remainder_only(fh_env,
                                                                 monkeypatch):
    spy, store = fh_env
    script = _LegScript(**{SP: [_partial(300), _partial(0)]})
    _run_fh(store, script, monkeypatch)

    sp_calls = script.for_symbol(SP)
    assert len(sp_calls) == 2, "the short-put residual was not retried"
    assert sp_calls[1]['qty'] == 100, (
        f"the retry must cover the residual only, got {sp_calls[1]['qty']}")
    assert sp_calls[1]['urgent'] is True


def test_the_short_put_freeze_alert_is_SAFETY_and_names_the_residue(
        fh_env, monkeypatch):
    spy, store = fh_env
    _run_fh(store, _LegScript(**{SP: [_partial(300), _partial(0)]}),
            monkeypatch)

    classes = [c for c, m in spy.offered if 'PARTIAL SHORT PUT' in m]
    assert classes == [alert_policy.SAFETY], (
        f"a live naked-short residue is a needs-a-human state; classes were "
        f"{classes}")
    assert spy.any('100'), "the alert must say how much is still short"
    assert spy.any(SP), "the alert must name the symbol still at the broker"
    assert spy.any('hedge'), (
        "the alert must explain WHY the long put was kept, or the reader will "
        "helpfully sell it and create the exact position this prevents")


def test_the_fills_that_were_observed_survive_the_short_put_freeze(fh_env,
                                                                   monkeypatch):
    """After the freeze the record is the only place those prices exist.

    The status assertion is load-bearing: `MemoryStore.update_trade_exit`
    folds the exit dict onto the trade exactly as the real stores do, so these
    keys are ALSO present on a wrongly-booked record.
    """
    spy, store = fh_env
    _run_fh(store, _LegScript(**{SP: [_partial(300), _partial(0)]}),
            monkeypatch)

    rec = store.trades[0]
    assert rec['status'] == 'partial_close'
    assert not store.called('update_trade_exit')
    assert rec['short_call_fill'] == FILL[SC]
    assert rec['long_call_fill'] == FILL[LC]
    assert 'long_put_fill' not in rec, "a price for a leg that never sold"


def test_the_brokers_own_view_is_recorded_at_the_short_put_freeze(fh_env,
                                                                  monkeypatch):
    """Orders went out, so the broker-side audit runs — as it does at the
    short-call residue freeze."""
    seen = []
    monkeypatch.setattr(sm, 'reconcile_after_close',
                        lambda k, t, l='FH': seen.append(t['id']) or False)
    spy, store = fh_env
    _run_fh(store, _LegScript(**{SP: [_partial(300), _partial(0)]}),
            monkeypatch)
    assert seen == [7]


def test_a_short_put_retry_that_clears_lets_the_close_finish(fh_env,
                                                             monkeypatch):
    """Negative control: residue cleared, so the hedge SHOULD be sold."""
    spy, store = fh_env
    script = _LegScript(**{SP: [_partial(300), _complete(100)]})
    ok = _run_fh(store, script, monkeypatch)

    assert script.for_symbol(LP), (
        "the residue cleared, so the long put should have been sold")
    assert ok is True
    assert store.called('update_trade_exit')


def test_a_complete_short_put_fill_still_books_normally(fh_env, monkeypatch):
    """THE negative control. 'Never continue after a partial' must not become
    'never continue'."""
    spy, store = fh_env
    script = _LegScript()
    ok = _run_fh(store, script, monkeypatch)

    assert ok is True
    assert script.for_symbol(SP) and script.for_symbol(LP)
    assert store.called('update_trade_exit')
    exit_data = store.called('update_trade_exit')[0][1][1]
    assert exit_data['close_cost'] == pytest.approx(
        FILL[SC] + FILL[SP] - FILL[LC] - FILL[LP])
    assert not spy.any('PARTIAL SHORT PUT')


def test_end_to_end_a_partial_short_put_never_sells_the_long_put(fh_env,
                                                                 monkeypatch):
    """The whole path, no monkeypatched `close_leg`.

    Otherwise the branch could be guarding a state `close_leg` never produces.
    A cancelled partial is retried; PARTIAL is emitted only when a rejection
    follows a cumulative fill, so script exactly that on the short put.
    """
    spy, store = fh_env
    calls = {'n': 0}

    def policy(order):
        if order['tradingsymbol'] == SP:
            calls['n'] += 1
            if calls['n'] == 1:
                return 'CANCELLED', 300, order['price']
            return 'REJECTED', 0, 0.0
        return 'COMPLETE', order['quantity'], order['price']

    kite = FakeBroker(books=FH_BOOKS, positions=_fh_pos(),
                      fill_policy=policy,
                      hedge_pairs=[(SC, LC), (SP, LP)])
    ok = sm._close_fh_inner(kite, store, _fh(), spot=3050.0, reason='SL_SPOT',
                            dry_run=False)

    assert ok is False
    assert kite.orders_for(LP) == [], (
        "end to end, the long put was sold against a live short-put residue")
    assert kite.net_qty(SP) < 0, "the fixture did not leave a short residue"
    assert kite.hedge_violations == []
    assert not store.called('update_trade_exit')


# ══ The structural invariant, asserted at the broker ════════════════════════

def test_the_broker_refuses_to_sell_a_hedge_over_a_live_short():
    """Positive control for the tripwire itself.

    Without this the invariant could be inert — a guard nobody can observe is
    decorative (`feedback_a_second_guard_you_cannot_observe_is_decorative`).
    """
    kite = FakeBroker(books=FH_BOOKS, positions=_fh_pos(sp=-100),
                      hedge_pairs=[(SP, LP)])

    with pytest.raises(HedgeInvariantViolation) as exc:
        kite.place_order(variety='regular', exchange='NFO', tradingsymbol=LP,
                         transaction_type='SELL', quantity=QTY,
                         product='NRML', order_type='LIMIT', price=2.0)

    assert 'NAKED SHORT' in str(exc.value)
    assert kite.placed == [], (
        "the forbidden order was recorded anyway — a later assertion on "
        "`placed` would be reading a book the invariant condemned")


def test_the_broker_allows_the_hedge_sale_once_the_short_is_flat():
    """Negative control: the invariant must not block a legitimate close."""
    kite = FakeBroker(books=FH_BOOKS, positions=_fh_pos(sp=0),
                     hedge_pairs=[(SP, LP)])
    kite.place_order(variety='regular', exchange='NFO', tradingsymbol=LP,
                     transaction_type='SELL', quantity=QTY, product='NRML',
                     order_type='LIMIT', price=2.0)
    assert len(kite.placed) == 1
    assert kite.hedge_violations == []


def test_buying_back_the_short_is_never_blocked():
    """Only the HEDGE SALE is forbidden. Reducing the short is the fix."""
    kite = FakeBroker(books=FH_BOOKS, positions=_fh_pos(sp=-100),
                      hedge_pairs=[(SP, LP)])
    kite.place_order(variety='regular', exchange='NFO', tradingsymbol=SP,
                     transaction_type='BUY', quantity=100, product='NRML',
                     order_type='LIMIT', price=8.0)
    assert kite.net_qty(SP) == 0


def test_the_violation_cannot_be_swallowed_by_a_close_handler():
    """`close_spread` and `close_fh_position` wrap the sequence in
    `except Exception`. An ordinary exception here would be caught, logged as
    'EXCEPTION during close' and handed to the test as a tidy freeze — the
    violation hidden by the handler meant to contain surprises."""
    assert issubclass(HedgeInvariantViolation, BaseException)
    assert not issubclass(HedgeInvariantViolation, Exception)


def test_every_replay_polices_the_invariant(monkeypatch):
    """Wired at the harness, not per fixture, so it cannot be forgotten.

    A guard that each new test has to opt into is a guard that the next test
    will not have.
    """
    day = date(2026, 9, 15)
    trade = {'id': 1, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
             'long_symbol': B_LONG, 'short_symbol': B_SHORT,
             'spot_symbol': 'NSE:TESTCO', 'exchange': 'NFO',
             'quantity': B_QTY, 'lot_size': B_QTY, 'lots': 1,
             'entry_long_price': 21.20, 'entry_short_price': 7.65,
             'net_debit': 13.55, 'spread_width': 50, 'target_spot': 1435.0,
             'sl_spot': 1319.0, 'sl_spread': 6.78, 'entry_spot': 1360.0,
             'expiry': '2026-09-29'}
    long_book = {'bid': 40.00, 'bid_qty': 1400, 'ask': 40.20, 'ask_qty': 1400,
                 'ltp': 40.10, 'prev_close': 21.0}
    short_book = {'bid': 10.05, 'bid_qty': 1400, 'ask': 10.30, 'ask_qty': 1400,
                  'ltp': 10.20, 'prev_close': 7.6}
    ticks = [Tick(t, 1400.0, long_book, short_book)
             for t in ('11:00:00', '11:10:00', '11:20:00')]

    _, kite, _, _ = run_session(
        monkeypatch, sm, trade, ticks, day, positions=_bcs_pos())

    assert kite.hedge_pairs == [(B_SHORT, B_LONG)], (
        "the replay harness stopped policing the hedge invariant")
    assert kite.hedge_violations == []


# ══ D3 — a partial LONG sale, both engines ═════════════════════════════════

def test_a_partial_bcs_long_sale_freezes_instead_of_booking(bcs_env,
                                                            monkeypatch):
    """Bounded risk — it is a long option — but the record would say CLOSED
    with 200 qty live, and the P&L would be computed on the full 700."""
    spy, store = bcs_env
    script = _LegScript(**{B_LONG: [_partial(500), _partial(0)]})
    ok = _run_bcs(store, script, monkeypatch)

    assert ok is False
    assert not store.called('update_trade_exit'), (
        "the trade was booked CLOSED with 200 qty of the long leg still live "
        "— and its P&L computed on all 700 — D3")
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['residual_long_qty'] == 200
    assert store.trades[0]['close_failed_leg'] == 'long'


def test_the_bcs_long_residue_keeps_both_observed_fills(bcs_env, monkeypatch):
    spy, store = bcs_env
    _run_bcs(store, _LegScript(**{B_LONG: [_partial(500), _partial(0)]}),
             monkeypatch)

    rec = store.trades[0]
    assert rec['short_fill'] == pytest.approx(10.2)
    assert rec['long_fill'] == pytest.approx(10.2)
    assert 'pnl_per_share' not in rec, "P&L computed on a half-sold leg"


def test_the_bcs_long_residue_is_retried_once_urgently(bcs_env, monkeypatch):
    spy, store = bcs_env
    script = _LegScript(**{B_LONG: [_partial(500), _partial(0)]})
    _run_bcs(store, script, monkeypatch)

    longs = script.for_symbol(B_LONG)
    assert len(longs) == 2, "the long residual was not retried"
    assert longs[1]['qty'] == 200
    assert longs[1]['urgent'] is True, (
        "the short is already flat, so the escalation invariant applies")


def test_the_bcs_long_freeze_alert_is_SAFETY_and_names_the_residue(bcs_env,
                                                                   monkeypatch):
    spy, store = bcs_env
    _run_bcs(store, _LegScript(**{B_LONG: [_partial(500), _partial(0)]}),
             monkeypatch)

    classes = [c for c, m in spy.offered if 'PARTIAL LONG CLOSE' in m]
    assert classes == [alert_policy.SAFETY], f"classes were {classes}"
    assert spy.any('200') and spy.any(B_LONG)
    assert spy.any('nothing is naked'), (
        "the reader must be told the risk class, not just that something "
        "went wrong")


def test_a_bcs_long_retry_that_clears_still_books(bcs_env, monkeypatch):
    """Negative control."""
    spy, store = bcs_env
    script = _LegScript(**{B_LONG: [_partial(500), _complete(200)]})
    ok = _run_bcs(store, script, monkeypatch)

    assert ok is True
    assert store.called('update_trade_exit')


def test_a_clean_bcs_close_is_untouched(bcs_env, monkeypatch):
    spy, store = bcs_env
    kite = FakeBroker(books=BCS_BOOKS, positions=_bcs_pos(),
                      hedge_pairs=[(B_SHORT, B_LONG)])
    ok = _run_bcs(store, None, monkeypatch, kite=kite)

    assert ok is True
    assert kite.net_qty(B_SHORT) == 0 and kite.net_qty(B_LONG) == 0
    assert kite.hedge_violations == []
    assert store.called('update_trade_exit')
    assert not spy.any('PARTIAL LONG CLOSE')


def test_a_partial_long_put_sale_freezes_instead_of_booking(fh_env,
                                                            monkeypatch):
    """FH Step 4. Every short leg is flat by now, so nothing is naked — but
    nothing would watch the residue either, and the P&L would be wrong."""
    spy, store = fh_env
    ok = _run_fh(store, _LegScript(**{LP: [_partial(300), _partial(0)]}),
                 monkeypatch)

    assert ok is False
    assert not store.called('update_trade_exit')
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['residual_long_put_qty'] == 100
    assert store.trades[0]['short_put_fill'] == FILL[SP]


def test_a_partial_long_call_sale_freezes_and_stops_the_sequence(fh_env,
                                                                 monkeypatch):
    """FH Step 2. Stopping is safe: the put spread is left intact and bounded
    by its width. Continuing would mean firing more orders into a book that
    has just half-refused one."""
    spy, store = fh_env
    script = _LegScript(**{LC: [_partial(300), _partial(0)]})
    ok = _run_fh(store, script, monkeypatch)

    assert ok is False
    assert store.trades[0]['status'] == 'partial_close'
    assert store.trades[0]['residual_long_call_qty'] == 100
    assert script.for_symbol(SP) == [], "the short put was traded after a refusal"
    assert script.for_symbol(LP) == []
    assert spy.any('SHORT PUT'), (
        "freezing at Step 2 leaves the put spread open — the reader has to be "
        "told what they were handed")


def test_a_three_leg_fh_short_put_residue_still_keeps_the_long_put(fh_env,
                                                                   monkeypatch):
    """The 3-leg variant has no long call, so Step 3 is the FIRST short leg
    the sequence would strip a hedge from. Parametrised leg coverage, not a
    duplicate: a fix applied only to the 4-leg path is the copy-you-did-not-
    open shape all over again."""
    spy, store = fh_env
    script = _LegScript(**{SP: [_partial(300), _partial(0)]})
    ok = _run_fh(store, script, monkeypatch, positions=_fh_pos(lc=0),
                 with_long_call=False)

    assert ok is False
    assert script.for_symbol(LP) == []
    assert store.trades[0]['residual_short_put_qty'] == 100

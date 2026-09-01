"""D4 + the 2026-08-28 scope change — Fallen Hero is WATCHED, never traded.

Two things ship together here because the second is only reachable once the
first is settled.

**FH IS TRADED BY HAND** (owner, 2026-08-28: *"FH is done manually — only
monitoring we need"*). Until today FH orders were inert for one reason only:
the crontab carries `--dry-run`. That is a property of a flag, not of the
design — the day the exit bridge arms for BCS, `_close_fh_inner`'s four order
steps would have come along for the ride, on the one structure in the fleet
carrying a NAKED SHORT CALL. `close_fh_position` now refuses any close with a
leg live at the broker, above the close lock and above every order, and tells
the owner what to do in Kite instead.

**D4 — an already-flat FH leg used to book at `0.0`, in the FLATTERING
direction.** `fills` is seeded with `0.0`, so a skipped leg, a failed leg and a
genuine zero fill all read alike (the same seed ambiguity as D1/D2). A skipped
SHORT leg therefore contributed a buyback cost of zero — the cheapest buyback
that exists — with no note, no marker and `update_trade_exit` called:
`close_cost 7.00 / pnl 90.75` where the truth was `15.00 / 82.75`, Rs 3,200 of
P&L that did not exist. Worse, `pnl_per_share` could exceed `total_credit`
(102.75 against 97.75) — a number the structure cannot produce.

The answer, in the owner's order:

  1. RECOVER the price when it is knowable — `find_recoverable_fill`, ORDER_TAG
     only, so a stranger's fill is never adopted.
  2. When it is not, book APPROXIMATE with a visible marker that PROPAGATES.
     Not `_refuse_unpriced_close`: the position genuinely is flat, nothing is
     at risk, and refusing would strand ordinary closes on a harmless state.
  3. CLAMP to `total_credit`. Mathematically achievable, therefore a clamp and
     not a refusal (CLAUDE.md, "Valuation bounds"). Applied at BOTH ends — in
     the monitor and at `FallenHeroStore.update_trade_exit` — because with FH
     manual the store is the only booking path with a live caller, and an
     impossible number must be unreachable by every route, including a
     hand-built `exit_data`.

The negative controls carry as much weight as the regressions: a genuinely
observed 0.00 fill must still book as EXACT, and a hand-written exit that omits
per-leg detail must NOT be tarred as approximate. Over-correcting "never trust
a zero" into "never book a zero" refuses real closes —
`feedback_guards_need_the_inverse_review`.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_d4_fh_manual_and_bounds.py -v
"""
import ast
import inspect
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                              # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,   # noqa: E402
                             TelegramSpy)
from fallen_hero import trade_store as fh_ts                       # noqa: E402

SC, SP, LP, LC = ('T26SEP3000CE', 'T26SEP2600PE',
                  'T26SEP2550PE', 'T26SEP3200CE')
QTY = 400
CREDIT = 97.75

BOOKS = {f'NFO:{s}': {'bid': 10.0, 'ask': 10.2, 'bid_qty': 800,
                      'ask_qty': 800, 'ltp': 10.1, 'prev_close': 10.0}
         for s in (SC, SP, LP, LC)}

#: Distinct per leg so a test can prove WHICH price landed on the record.
#: These are the numbers from the D4 report: SC 12.00 + SP 8.00 - LC 3.00 -
#: LP 2.00 = 15.00 close cost, 97.75 - 15.00 = 82.75 truthful P&L. Skip the
#: short put and the arithmetic becomes 7.00 / 90.75 — the reported defect.
FILL = {SC: 12.0, LC: 3.0, SP: 8.0, LP: 2.0}
TRUE_COST = FILL[SC] + FILL[SP] - FILL[LC] - FILL[LP]      # 15.00
TRUE_PNL = CREDIT - TRUE_COST                              # 82.75


def _fh(with_long_call=True, **over):
    t = {'id': 7, 'stock': 'TESTCO', 'status': 'open', 'quantity': QTY,
         'exchange': 'NFO', 'short_call_symbol': SC, 'short_put_symbol': SP,
         'long_put_symbol': LP, 'spot_symbol': 'NSE:TESTCO',
         'total_credit': CREDIT, 'breakeven': 3097.75, 'sl_spot': 2850.0,
         'expiry': '2026-09-29', 'long_put_strike': 2550,
         'short_put_strike': 2600, 'short_call_strike': 3000}
    t['long_call_symbol'] = LC if with_long_call else None
    t.update(over)
    return t


def _pos(sc=-QTY, sp=-QTY, lp=QTY, lc=QTY):
    return [{'tradingsymbol': SC, 'quantity': sc},
            {'tradingsymbol': SP, 'quantity': sp},
            {'tradingsymbol': LP, 'quantity': lp},
            {'tradingsymbol': LC, 'quantity': lc}]


def _ok(price, filled=QTY):
    return {'status': 'COMPLETE', 'average_price': price,
            'order_id': 'x', 'filled_quantity': filled}


class _LegScript:
    """A `close_leg` stand-in; anything unscripted fills at its FILL price."""

    def __init__(self, **failures):
        self.failures = dict(failures)
        self.calls = []

    def __call__(self, kite, exchange, symbol, txn, qty, is_buy=False,
                 dry_run=False, urgent=False, context=None, attempts=None,
                 allow_pay_through=True, paper_passthrough=False):
        self.calls.append({'symbol': symbol, 'txn': txn, 'qty': qty})
        if symbol in self.failures:
            return self.failures[symbol]
        return _ok(FILL[symbol])


def _tagged(symbol, txn, price, tag=None):
    """A COMPLETE order in today's book, tagged by THIS system."""
    return {'order_id': 'r1', 'tradingsymbol': symbol,
            'transaction_type': txn, 'status': 'COMPLETE',
            'average_price': price, 'order_timestamp': '2026-08-28 10:00:00',
            'tag': sm.ORDER_TAG if tag is None else tag}


@pytest.fixture(autouse=True)
def _fresh_alert_dedup():
    """`_fh_manual_alerted` is a module global keyed (id, reason, date), so
    without this the FIRST test to fire a trigger silences every later one and
    they pass on an empty inbox. That is the shape
    `feedback_never_asked_is_not_failed` describes: "we never alerted" and "the
    alert was suppressed" both leave `sent` empty and need opposite fixes."""
    #: `getattr`, not attribute access: a fixture that ERRORS at setup makes
    #: every test in the file report "error" instead of its own assertion,
    #: which destroys the pre-fix evidence this file exists to produce. The
    #: dict's EXISTENCE is pinned by its own test below, where a failure says
    #: what actually broke.
    getattr(sm, '_fh_manual_alerted', {}).clear()
    yield
    getattr(sm, '_fh_manual_alerted', {}).clear()


@pytest.fixture
def env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    spy = TelegramSpy().install(monkeypatch, sm)
    return spy, MemoryStore(trades=[_fh()])


def _run_inner(store, script, monkeypatch, positions=None, order_book=(),
               with_long_call=True):
    monkeypatch.setattr(sm, 'close_leg', script)
    kite = FakeBroker(books=BOOKS, positions=positions or _pos())
    kite.order_book = list(order_book)
    ok = sm._close_fh_inner(kite, store, _fh(with_long_call), spot=3050.0,
                            reason='SL_SPOT', dry_run=False)
    return ok, kite


def _booked(store):
    calls = store.called('update_trade_exit')
    assert calls, 'nothing was booked'
    return calls[0][1][1]


# ══ PART 1 — the monitor places no FH order ════════════════════════════════
#
# The scope change. These are the tests that must not be allowed to rot: they
# are the difference between "FH orders are off" and "FH orders are off
# because a flag is set".

def test_the_dedup_ledger_exists():
    """Pinned separately so the autouse fixture above can stay tolerant."""
    assert isinstance(sm._fh_manual_alerted, dict)


def test_the_monitor_places_no_fh_order_when_a_leg_is_live(env, monkeypatch):
    """The whole point. A live FH leg + a fired trigger => ZERO orders."""
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos())

    out = sm.close_fh_position(kite, _fh(), spot=3050.0, reason='SL_SPOT',
                               dry_run=False)

    assert out == 'MANUAL', (
        'FH must report a third outcome — nothing was attempted, so this is '
        'neither a close nor a failed close')
    assert kite.placed == [], (
        'the monitor placed an FH order. FH is traded BY HAND — this is the '
        'naked-short-call structure and the one that must never be automated')
    assert not store.called('begin_close'), (
        'the record was moved to "closing" for a close that will never happen')
    assert not store.called('update_trade_exit')
    assert store.trades[0]['status'] == 'open', (
        'a hand-traded position must stay OPEN and keep being monitored')


def test_the_refusal_does_not_depend_on_dry_run(env, monkeypatch):
    """`dry_run=False` is the armed case, and it is the one asserted above.

    Stated separately because "it did not place an order" is trivially true
    under `--dry-run`, and that is precisely the property being replaced.
    """
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    for dry in (True, False):
        kite = FakeBroker(books=BOOKS, positions=_pos())
        assert sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT',
                                    dry_run=dry) == 'MANUAL'
        assert kite.placed == []


def test_the_alert_names_the_legs_the_quantities_and_the_order(env,
                                                               monkeypatch):
    """An alert that says "close it by hand" without saying WHAT is a nag."""
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos())
    sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT', dry_run=False)

    msg = '\n'.join(spy.offered_containing('by hand') or spy.sent)
    for sym in (SC, LC, SP, LP):
        assert sym in msg, f'{sym} is missing from the manual-close alert'
    assert str(QTY) in msg, 'the alert does not say how many'
    # Naked short first: selling its hedge before it is flat is the one
    # sequencing error that manufactures unbounded risk.
    assert msg.index(SC) < msg.index(LC) < msg.index(SP) < msg.index(LP), (
        'the hand-close sequence is out of risk order')


def test_the_quantities_come_from_the_broker_not_the_record(env, monkeypatch):
    """A leg half-closed by hand already must not be re-quoted at full size."""
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos(sp=-100))
    sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT', dry_run=False)
    msg = '\n'.join(spy.offered_containing(SP))
    assert f'{SP}  x 100' in msg, msg


def test_the_owner_is_not_told_seven_hundred_times_an_hour(env, monkeypatch):
    """The record stays open, so the trigger re-fires every poll."""
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos())
    for _ in range(5):
        sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT', dry_run=False)
    assert len(spy.offered_containing('traded BY HAND')) == 1


def test_a_different_trigger_is_a_different_alert(env, monkeypatch):
    """The SL nag must not silence the expiry-day one; they are not the
    same news and the second is the delivery-margin one."""
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos())
    sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT', dry_run=False)
    sm.close_fh_position(kite, _fh(), 3050.0, 'EXPIRY_FORCE_CLOSE',
                         dry_run=False)
    assert len(spy.offered_containing('traded BY HAND')) == 2


def test_no_route_from_close_fh_position_to_an_order(env, monkeypatch):
    """Structural, not behavioural: the wrapper contains no order call and
    reaches `_close_fh_inner` only after the live-leg refusal.

    `arm_time_stop`'s docstring records why this shape of test earns its keep —
    a mutation reverting two of three call sites survived the entire suite
    because nothing pinned where the single entry point was.
    """
    src = inspect.getsource(sm.close_fh_position)
    tree = ast.parse(src.lstrip())
    fn = tree.body[0]
    #: The docstring EXPLAINS begin_close and close_leg, so a substring scan
    #: over the source would find them in prose. Search the code.
    names = {n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    attrs = {n.func.attr for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert 'close_leg' not in names and 'place_order' not in attrs, (
        'the FH wrapper can place an order')
    assert 'begin_close' not in attrs, (
        'the close lock is back. With no orders it buys nothing and costs a '
        'record parked at "closing" every time the booking is refused')
    assert '_fh_manual_close_required' in names
    assert '_close_fh_inner' in names
    # and the refusal is reached FIRST
    body = src[src.index('):') :]
    assert body.index('_fh_manual_close_required') < body.index(
        '_close_fh_inner(')


def test_a_flat_position_is_booked_not_refused(env, monkeypatch):
    """The one thing that still reaches `_close_fh_inner` in production: no
    legs at the broker, so nothing to trade — only something to record."""
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos(0, 0, 0, 0))
    kite.order_book = [_tagged(SC, 'BUY', FILL[SC]),
                       _tagged(SP, 'BUY', FILL[SP]),
                       _tagged(LP, 'SELL', FILL[LP]),
                       _tagged(LC, 'SELL', FILL[LC])]
    out = sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT', dry_run=False)
    assert out is True
    assert kite.placed == []
    rec = _booked(store)
    assert rec['pnl_per_share'] == pytest.approx(TRUE_PNL)


def test_a_manual_close_is_not_a_failed_close_in_the_time_stop_ladder():
    """`'MANUAL'` walks the same escalate-to-a-human ladder — the ladder's
    product is pressure on a person, which is exactly what is wanted — but it
    must never be described as an order that did not fill."""
    state = {'date': '2026-08-28', 'state': 'armed', 'attempts': 0,
             'next_attempt_after': 0.0}
    trade, store = _fh(), MemoryStore(trades=[_fh()])
    sent = []
    import bcs.spread_monitor as m
    old = m.send_telegram
    m.send_telegram = lambda msg, alert_class=None: sent.append(msg) or True
    try:
        for _ in range(sm.TIME_STOP_MAX_ATTEMPTS):
            out = sm.record_time_stop_result(state, 'MANUAL', trade, store,
                                             'FH', now=0.0)
        assert out == 'escalated'
        blob = '\n'.join(sent)
        assert 'none filled' not in blob, (
            'the escalation describes an order this system never places')
        assert 'CLOSE IT BY HAND' in blob
    finally:
        m.send_telegram = old


# ══ PART 2 — D4, inside the booking arithmetic ═════════════════════════════

def test_a_skipped_leg_does_not_book_at_zero(env, monkeypatch):
    """THE reported defect. Short put flat on arrival, nothing to price it."""
    spy, store = env
    ok, _ = _run_inner(store, _LegScript(), monkeypatch,
                       positions=_pos(sp=0))
    assert ok is True
    rec = _booked(store)
    assert rec['short_put_fill'] is None, (
        f"a leg nobody transacted was recorded at "
        f"{rec['short_put_fill']!r} — 0.0 is not an observation, it is the "
        f"CHEAPEST buyback there is")


def test_a_skipped_leg_is_marked_approximate(env, monkeypatch):
    spy, store = env
    _run_inner(store, _LegScript(), monkeypatch, positions=_pos(sp=0))
    rec = _booked(store)
    assert rec.get('pnl_approximate') is True
    assert rec.get('unpriced_legs') == ['short_put']
    assert 'APPROXIMATE' in rec.get('notes', '')


def test_a_recoverable_tagged_fill_is_used_and_is_exact(env, monkeypatch):
    """Our own order closed it earlier: the price is KNOWN, not approximate."""
    spy, store = env
    _run_inner(store, _LegScript(), monkeypatch, positions=_pos(sp=0),
               order_book=[_tagged(SP, 'BUY', FILL[SP])])
    rec = _booked(store)
    assert rec['short_put_fill'] == pytest.approx(FILL[SP])
    assert rec['close_cost'] == pytest.approx(TRUE_COST)
    assert rec['pnl_per_share'] == pytest.approx(TRUE_PNL)
    assert 'pnl_approximate' not in rec, (
        'a recovered fill is an OBSERVED price; marking it approximate would '
        'teach the reader to ignore the marker')


def test_a_strangers_fill_is_not_adopted(env, monkeypatch):
    """`find_recoverable_fill` is ORDER_TAG-only on purpose — a manual close
    at a price the operator chose must not be booked as ours. Under the
    2026-08-28 decision this is now the NORMAL case for FH, since the owner's
    own orders carry no tag."""
    spy, store = env
    _run_inner(store, _LegScript(), monkeypatch, positions=_pos(sp=0),
               order_book=[_tagged(SP, 'BUY', 99.0, tag='someone-else')])
    rec = _booked(store)
    assert rec['short_put_fill'] is None
    assert rec.get('pnl_approximate') is True
    assert 'untagged' in rec.get('notes', '')


def test_the_approximation_is_flagged_to_the_owner_not_only_to_the_log(
        env, monkeypatch):
    spy, store = env
    _run_inner(store, _LegScript(), monkeypatch, positions=_pos(sp=0))
    assert spy.offered_containing('APPROXIMATE'), (
        'the close alert reports a P&L it knows to be optimistic, without '
        'saying so')


def test_an_absent_long_call_is_worth_zero_not_unknown(env, monkeypatch):
    """A 3-leg FH has no long call. Absent and unpriceable must not share a
    literal — the all-flat branch says the same in as many words."""
    spy, store = env
    store.trades = [_fh(with_long_call=False)]
    _run_inner(store, _LegScript(), monkeypatch,
               positions=_pos(lc=0), with_long_call=False)
    rec = _booked(store)
    assert rec['long_call_fill'] == 0.0
    assert 'pnl_approximate' not in rec


# ── the clamp ───────────────────────────────────────────────────────────────

def test_pnl_can_never_exceed_total_credit(env, monkeypatch):
    """The impossible number from the report: 102.75 against a credit of
    97.75. Reached by skipping the short put on a book whose longs are worth
    more than the remaining short."""
    spy, store = env
    monkeypatch.setitem(FILL, SC, 1.0)      # both longs now dominate
    try:
        _run_inner(store, _LegScript(), monkeypatch, positions=_pos(sp=0))
        rec = _booked(store)
        assert rec['pnl_per_share'] <= CREDIT, (
            f"{rec['pnl_per_share']} exceeds the structure's mathematical "
            f"maximum of {CREDIT}")
        assert rec['pnl_per_share'] == pytest.approx(CREDIT)
        assert rec['total_pnl'] == pytest.approx(CREDIT * QTY)
    finally:
        FILL[SC] = 12.0


def test_the_clamp_says_so_on_the_record_and_in_the_log(env, monkeypatch,
                                                        capsys):
    spy, store = env
    monkeypatch.setitem(FILL, SC, 1.0)
    try:
        _run_inner(store, _LegScript(), monkeypatch, positions=_pos(sp=0))
    finally:
        FILL[SC] = 12.0
    rec = _booked(store)
    assert rec.get('pnl_clamped_from') is not None, (
        'the pre-clamp figure is the only evidence that a leg was mispriced; '
        'a clamp that fires silently hides the bug that made it necessary')
    assert rec['pnl_clamped_from'] > CREDIT
    assert rec.get('pnl_approximate') is True
    assert 'IMPOSSIBLE P&L CLAMPED' in capsys.readouterr().out


def test_the_clamp_is_inert_on_an_ordinary_close(env, monkeypatch):
    """It must not shave a legitimate P&L. Every leg observed, nothing bound."""
    spy, store = env
    _run_inner(store, _LegScript(), monkeypatch)
    rec = _booked(store)
    assert rec['pnl_per_share'] == pytest.approx(TRUE_PNL)
    assert 'pnl_clamped_from' not in rec
    assert 'pnl_approximate' not in rec


# ── the negative control that stops over-correction ─────────────────────────

def test_a_genuinely_observed_zero_fill_still_books_normally(env, monkeypatch):
    """A worthless leg really does close at 0.00, and that is a PRICE.

    "Never trust a zero" must not become "never book a zero": that refuses
    real closes, which is the inverse-review failure
    `feedback_guards_need_the_inverse_review` is about.
    """
    spy, store = env
    script = _LegScript(**{SP: _ok(0.0)})
    ok, _ = _run_inner(store, script, monkeypatch)
    assert ok is True
    rec = _booked(store)
    assert rec['short_put_fill'] == 0.0, 'an observed zero was thrown away'
    assert 'pnl_approximate' not in rec, (
        'a transacted 0.00 is exact; calling it approximate is the '
        'over-correction')
    assert rec['close_cost'] == pytest.approx(
        FILL[SC] + 0.0 - FILL[LC] - FILL[LP])


def test_every_leg_observed_is_never_approximate(env, monkeypatch):
    spy, store = env
    _run_inner(store, _LegScript(), monkeypatch)
    rec = _booked(store)
    assert 'pnl_approximate' not in rec and 'unpriced_legs' not in rec


# ══ PART 3 — the marker PROPAGATES through the real store ══════════════════
#
# With FH manual, `FallenHeroStore.update_trade_exit` is the only booking path
# with a live caller. Everything above is worth nothing if the marker stops at
# the monitor's dict.

@pytest.fixture
def fh_store(tmp_path, monkeypatch):
    """The REAL `FallenHeroStore`, redirected entirely into tmp_path."""
    monkeypatch.setattr(fh_ts, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(fh_ts, 'LOCAL_TRADES_FILE',
                        tmp_path / 'fallen_hero_trades.json')
    monkeypatch.setattr(fh_ts, 'LOCK_FILE', tmp_path / 'fh.lock')
    (tmp_path / 'fallen_hero_trades.json').write_text(
        json.dumps([dict(_fh(), version=1, lot_size=QTY, lots=1)]),
        encoding='utf-8')
    s = fh_ts.FallenHeroStore(config={'google_drive': {'enabled': False}})
    s.initialize()
    return s


def _exit(**over):
    d = {'exit_date': '2026-08-28T10:00:00', 'exit_reason': 'SL_SPOT',
         'exit_spot': 3050.0, 'close_cost': TRUE_COST,
         'pnl_per_share': TRUE_PNL, 'total_pnl': TRUE_PNL * QTY}
    d.update(over)
    return d


def test_the_marker_reaches_the_top_level_of_the_record(fh_store):
    """A marker only findable by opening a nested dict is one most readers
    miss — `list_trades`, `journal_report` and the dashboard all scan trades."""
    fh_store.update_trade_exit(7, _exit(pnl_approximate=True,
                                        unpriced_legs=['short_put']))
    t = fh_store.load_trades()[0]
    assert t['exit_approximate'] is True
    assert fh_ts.exit_is_approximate(t) is True


def test_an_exact_close_carries_no_marker(fh_store):
    fh_store.update_trade_exit(7, _exit())
    t = fh_store.load_trades()[0]
    assert 'exit_approximate' not in t
    assert fh_ts.exit_is_approximate(t) is False


def test_the_marker_survives_the_round_trip_to_disk(fh_store, tmp_path):
    fh_store.update_trade_exit(7, _exit(pnl_approximate=True))
    raw = json.loads((tmp_path / 'fallen_hero_trades.json').read_text('utf-8'))
    assert raw[0]['exit_approximate'] is True
    assert raw[0]['exit']['pnl_approximate'] is True


def test_list_trades_shows_the_caveat_on_the_total(fh_store, capsys):
    """The total is only as exact as its least exact term, so the caveat rides
    on the bottom line — a reader who looks at nothing else still sees it."""
    fh_store.update_trade_exit(7, _exit(pnl_approximate=True))
    fh_store.list_trades()
    out = capsys.readouterr().out
    assert 'approximate' in out
    assert 'closed~' in out


# ── the store is the choke point: it bounds a HAND-BUILT exit too ───────────

def test_the_store_clamps_an_impossible_hand_written_pnl(fh_store):
    """With FH manual there is no code between a human and the book. The
    clamp lives at the write boundary so it does not depend on which code —
    or which person — produced the number."""
    fh_store.update_trade_exit(7, _exit(pnl_per_share=102.75,
                                        total_pnl=102.75 * QTY))
    t = fh_store.load_trades()[0]
    assert t['exit']['pnl_per_share'] == pytest.approx(CREDIT)
    assert t['exit']['pnl_clamped_from'] == pytest.approx(102.75)
    assert t['exit']['total_pnl'] == pytest.approx(CREDIT * QTY), (
        'total_pnl still contradicts the per-share figure it derives from')
    assert t['exit_approximate'] is True


def test_the_store_leaves_a_legitimate_pnl_alone(fh_store):
    fh_store.update_trade_exit(7, _exit())
    ex = fh_store.load_trades()[0]['exit']
    assert ex['pnl_per_share'] == pytest.approx(TRUE_PNL)
    assert 'pnl_clamped_from' not in ex


def test_a_pnl_exactly_at_the_credit_is_not_clamped(fh_store):
    """Every leg expiring worthless REACHES the bound. A boundary that
    refuses its own achievable value is a bug, not a guard."""
    fh_store.update_trade_exit(7, _exit(pnl_per_share=CREDIT,
                                        total_pnl=CREDIT * QTY))
    ex = fh_store.load_trades()[0]['exit']
    assert ex['pnl_per_share'] == pytest.approx(CREDIT)
    assert 'pnl_clamped_from' not in ex


def test_an_explicit_none_leg_price_marks_the_record(fh_store):
    fh_store.update_trade_exit(7, _exit(short_put_fill=None,
                                        short_call_fill=12.0))
    t = fh_store.load_trades()[0]
    assert t['exit_approximate'] is True
    assert t['exit']['unpriced_legs'] == ['short_put_fill']


def test_a_hand_written_exit_without_leg_detail_is_not_tarred(fh_store):
    """MISSING is not UNKNOWN. A human recording "I closed the lot for Rs X"
    has an exact number and no per-leg breakdown; inferring doubt from silence
    would print a caveat on every hand-written record, which is how a caveat
    stops being read."""
    fh_store.update_trade_exit(7, _exit())
    t = fh_store.load_trades()[0]
    assert 'exit_approximate' not in t
    assert 'unpriced_legs' not in t['exit']


def test_bound_fh_exit_never_mutates_its_input():
    src = _exit(pnl_per_share=102.75)
    out = fh_ts.bound_fh_exit(_fh(), src)
    assert src['pnl_per_share'] == 102.75 and out['pnl_per_share'] == CREDIT


def test_bound_fh_exit_survives_a_record_it_cannot_measure():
    """No credit on the record, or no per-share figure in the exit. Refusing
    the whole write over a missing optional field would strand a close the
    owner has already made at the broker."""
    assert fh_ts.bound_fh_exit({}, _exit())['pnl_per_share'] == TRUE_PNL
    assert 'pnl_per_share' not in fh_ts.bound_fh_exit(
        _fh(), {'exit_reason': 'SL_SPOT'})
    assert fh_ts.bound_fh_exit(_fh(total_credit=None),
                               _exit())['pnl_per_share'] == TRUE_PNL


def test_an_unreadable_position_book_is_not_a_flat_position(env, monkeypatch):
    """"I cannot see it" and "there is nothing there" are opposite facts.

    An empty leg list one branch further down means FLAT, which would send a
    dead-token cycle into the booking path. A rate-limited `kite.positions()`
    is the live shape here — the 2026-08-27 incident — and the honest response
    is to tell the owner the trigger fired and say the quantities are
    unverified.
    """
    spy, store = env
    monkeypatch.setattr(sm, 'get_fh_store', lambda: store)
    kite = FakeBroker(books=BOOKS, positions=_pos())
    kite.positions_raises = Exception('Too many requests')

    out = sm.close_fh_position(kite, _fh(), 3050.0, 'SL_SPOT', dry_run=False)

    assert out == 'MANUAL'
    assert kite.placed == []
    assert not store.called('update_trade_exit'), (
        'an unreadable book was read as a flat position and BOOKED')
    msg = '\n'.join(spy.offered_containing('BY HAND'))
    assert 'UNVERIFIED' in msg

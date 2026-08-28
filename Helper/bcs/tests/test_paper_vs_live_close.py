"""C5 + N1 + N2 — one scenario, three sides of it.

The owner's decision of 2026-08-27 is that paper trading keeps running through
go-live and every paper position resolves NATURALLY: "not the 8 cohort
positions, not any other". So open paper records WILL exist on the day the
switches flip, and all three defects below are what happens to one of them at
that moment.

**C5 — the money path could not tell a paper record from a real one.**
`zebra/scanner.py` stamped `paper: True` on every signal and nothing ever
flipped it; the string `paper` did not appear anywhere in `bcs/spread_monitor
.py`, `bcs/zebra_adapter.py` or `bcs/entry_executor.py`. Meanwhile
`_paper_auto_close`'s first line read `if not cfg.PAPER_MODE: return None`, so
flipping the mode removed the booking engine from the WHOLE store at once. Net
effect at arming time: every open paper position loses zebra and is adopted by
the live exit bridge.

**N1 — and what the bridge does with one is book a max loss.** A paper record
has no legs at any broker, so its close takes the ALREADY-FLAT branch, where
`_find_last_fill_price` found nothing and returned 0.0. `0.0` is not `None`, so
the adapter's None-guard never saw it, and `_apply_exit` computed
`pnl_per_share = 0.0 - debit`: a silent -100% from a price nobody observed.
Worse, that path used to fall back to ANY matching order in the account, so a
stranger's fill could be booked as this trade's exit.

**N2 — the interlock's `expiry` omission.** Audited here and left alone; see
`test_only_the_terminal_settle_may_close_on_expiry` for the argument and for
the invariant that keeps it safe.

Run:  cd Helper && python -m pytest bcs/tests/test_paper_vs_live_close.py -v
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                              # noqa: E402
from bcs import zebra_adapter as za                               # noqa: E402
from bcs.tests.fakes import (FakeBroker, FakeClock, MemoryStore,   # noqa: E402
                             TelegramSpy)
from zebra import config as zcfg                                  # noqa: E402
from zebra import monitor as zmonitor                             # noqa: E402
from zebra.trade_store import ZebraStore, is_paper_record         # noqa: E402

COHORT = zcfg.COHORT_START
LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700

#: Tight two-sided books on both legs, so any refusal in these tests comes from
#: the guard under test and never from the reliability gate.
BOOKS = {
    f'NFO:{LONG}':  {'bid': 40.00, 'ask': 40.20, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 40.10, 'prev_close': 39.50},
    f'NFO:{SHORT}': {'bid': 10.05, 'ask': 10.30, 'bid_qty': 1400,
                     'ask_qty': 1400, 'ltp': 10.20, 'prev_close': 9.80},
}


def _record(**over):
    """An entered cohort BCS in ZEBRA's field names — the shape on the Pi."""
    t = {
        'id': 419, 'version': 4, 'status': 'entered',
        'cohort': COHORT, 'structure': 'bcs',
        'stock': 'TESTCO', 'timeframe': 'monthly', 'direction': 'CE',
        'st_value': 1400.0, 'st_direction': 'DOWN',
        'signal_price': 1350.0, 'signal_gap_pct': 3.7,
        'long_symbol': LONG, 'short_symbol': SHORT,
        'long_strike': 1340, 'short_strike': 1390,
        'debit': 13.55, 'width': 50, 'quantity': QTY, 'lot_size': QTY,
        'lots': 1, 'long_ask_entry': 21.20, 'short_bid_entry': 7.65,
        'entry_spot': 1360.0, 'entry_date': '2026-08-20',
        'expiry': '2026-09-29', 'tp_spot': 1400.0, 'sl_spot': 1319.0,
        'debit_sl_value': 6.78, 'capital': 9485.0,
        'paper': True,
    }
    t.update(over)
    return t


@pytest.fixture
def zstore(tmp_path, monkeypatch):
    """A REAL `ZebraStore` on a tempfile, Drive off.

    Every path constant is redirected, not just LOG_DIR: `zebra/config.py`
    derives LOCAL_FILE and LOCK_FILE at IMPORT.
    """
    d = tmp_path / 'zebra'
    d.mkdir()
    monkeypatch.setattr(zcfg, 'LOG_DIR', d)
    monkeypatch.setattr(zcfg, 'LOCAL_FILE', d / 'zebra_trades.json')
    monkeypatch.setattr(zcfg, 'LOCK_FILE', d / 'zebra_trades.lock')

    def _seed(records):
        (d / 'zebra_trades.json').write_text(json.dumps(records))
        s = ZebraStore(config={'google_drive': {'enabled': False}})
        s.initialize()
        assert not s._drive_enabled, 'this test must never reach Drive'
        return s

    return _seed


@pytest.fixture
def env(monkeypatch):
    """A fake clock, a Telegram recorder, and a FRESH escalation dedup.

    `_unpriced_close_alerted` is module state (like `expiry_trades`), so
    without this the second test in the file silently observes the first
    test's dedup and asserts nothing.
    """
    FakeClock().install(monkeypatch, sm)
    monkeypatch.setattr(sm, '_unpriced_close_alerted', {})
    return TelegramSpy().install(monkeypatch, sm)


def _fills(kite, *rows):
    """Append COMPLETE orders to the fake account's order book.

    rows: (symbol, txn_type, price, tag)
    """
    for i, (sym, txn, px, tag) in enumerate(rows):
        kite.order_book.append({
            'order_id': str(800 + i), 'tradingsymbol': sym,
            'transaction_type': txn, 'status': 'COMPLETE',
            'average_price': px, 'tag': tag,
            'order_timestamp': '2026-09-21 14:30:0%d' % i})
    return kite


def _flat(**kw):
    """A broker that reports both legs at exactly zero."""
    return FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': 0},
                                 {'tradingsymbol': LONG, 'quantity': 0}],
                      **kw)


def _monitor_trade(**over):
    """One record as the monitor reads it (i.e. through `map_trade`)."""
    return za.map_trade(_record(**over))


# ═══ C5.1 — the live entry path is the only thing that clears the flag ══════

def test_a_signal_promoted_without_a_broker_fill_stays_paper(zstore):
    """`mark_entered_bcs` is reached by BOTH the paper pipeline and the
    auto-entry executor. Only the second may clear the flag."""
    s = zstore([{'id': 1, 'status': 'triggered', 'stock': 'TESTCO',
                 'direction': 'CE', 'st_value': 1400.0, 'timeframe': 'monthly',
                 'paper': True}])
    t = s.mark_entered_bcs(1, {
        'long_strike': 1340, 'short_strike': 1390, 'long_symbol': LONG,
        'short_symbol': SHORT, 'debit': 13.55, 'width': 50.0,
        'lot_size': QTY, 'lots': 1, 'expiry': '2026-09-29',
        'entry_spot': 1360.0})
    assert t['paper'] is True
    assert is_paper_record(t) is True


def test_a_position_actually_placed_at_the_broker_is_not_paper(zstore):
    """`placed_at_broker` is stamped by `_auto_enter_bcs` after
    `entry_executor.open_spread` reports filled lots on a non-dry run. It is
    the ONLY way a record ever stops being paper."""
    s = zstore([{'id': 1, 'status': 'triggered', 'stock': 'TESTCO',
                 'direction': 'CE', 'st_value': 1400.0, 'timeframe': 'monthly',
                 'paper': True}])
    t = s.mark_entered_bcs(1, {
        'long_strike': 1340, 'short_strike': 1390, 'long_symbol': LONG,
        'short_symbol': SHORT, 'debit': 13.55, 'width': 50.0,
        'lot_size': QTY, 'lots': 1, 'expiry': '2026-09-29',
        'entry_spot': 1360.0, 'placed_at_broker': True})
    assert t['paper'] is False
    assert is_paper_record(t) is False


def test_the_auto_entry_path_stamps_placed_at_broker_only_when_not_dry():
    """The dry stub in `wait_for_fill` reports COMPLETE at 0.0, so a DRY RUN
    reaches `mark_entered_bcs` with lots_filled set and nothing placed.
    Stamping that record live would hand the money path a phantom position —
    the same failure this flag exists to prevent, inverted."""
    src = inspect.getsource(zmonitor._auto_enter_bcs)
    assert "filled['placed_at_broker'] = not dry_run" in src, (
        'the live-entry stamp is missing, or is no longer conditioned on '
        'dry_run — a dry run must never produce a record the order path '
        'believes it owns')


def test_every_bcs_entry_path_stamps_the_flag():
    """Both writers go through `_bcs_entry_fields`, so the key cannot be
    forgotten by one of them. That is the whole point of the shared builder —
    `structure` was added to it for exactly this reason."""
    fields = ZebraStore._bcs_entry_fields.__func__ \
        if hasattr(ZebraStore._bcs_entry_fields, '__func__') \
        else ZebraStore._bcs_entry_fields
    src = inspect.getsource(fields)
    assert "'paper'" in src, (
        'paper is no longer stamped in the ONE shared entry builder; a second '
        'copy of the rule is how this codebase loses fixes')


# ═══ C5.2 — the ORDER path refuses a paper record; the WATCH path does not ══

def test_paper_records_stay_VISIBLE_to_the_monitor():
    """The obvious implementation — filter them out of `get_open_trades` —
    was tried and reverted the same day, and this test is the reason.

    That method is the WATCH path. `--list` reads it (hiding the cohort there
    reproduces `b3fabf6`: "Open: 0" with eight positions live), and so does the
    dry-run evidence week: `journal_report --compare` works by having the
    monitor poll the paper cohort, journal what it WOULD have done, and set
    that beside zebra's real booking. A monitor that cannot see the cohort has
    nothing to compare — and that comparison gates arming anything.
    """
    class FakeZebra:
        def get_entered(self):
            return [_record(id=1, stock='PAPER', paper=True),
                    _record(id=2, stock='REAL', paper=False)]
    got = za.ZebraStoreAdapter(FakeZebra()).get_open_trades()
    assert [t['stock'] for t in got] == ['PAPER', 'REAL']
    assert got[0]['paper'] is True, (
        'map_trade dropped the flag — the guards downstream read it off the '
        'MAPPED record')


def test_the_order_path_itself_refuses_a_paper_record(env):
    """THE gate. Every route to a real closing order goes through
    `close_spread`, so this is where a paper record is stopped — before the
    vet, before the close lock, before any order."""
    spy = env
    kite = FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    out = sm.close_spread(kite, _monitor_trade(), spot=1400.0, reason='TP',
                          dry_run=False, store=store)

    assert out == 'ABORT'
    assert kite.placed == [], 'real orders were placed for a paper position'
    assert store.called('begin_close') == [], (
        'the close lock was taken on a record that must never be closed here')
    assert spy.any('paper trade')


def test_a_dry_run_still_walks_the_close_so_the_journal_records_it(env):
    """The refusal is scoped to ARMED runs, and this is why.

    A dry run cannot place an order, so refusing there buys no safety at all —
    and it costs the evidence the whole plan depends on. `journal_report
    --compare` works by letting the monitor walk the close and journal the
    orders it WOULD have sent, then setting those beside zebra's real paper
    booking. Abort early and there is nothing to compare, on the one book the
    comparison exists for.
    """
    spy = env
    kite = FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    out = sm.close_spread(kite, _monitor_trade(), spot=1400.0, reason='TP',
                          dry_run=True, store=store)

    assert out != 'ABORT'
    assert kite.placed == [], 'a dry run placed a real order'
    assert store.called('update_trade_exit') == [], (
        'a dry run booked an exit')


def test_the_paper_guard_sits_on_the_only_route_to_an_order():
    """ONE guard, at the boundary the dangerous caller uses — pinned here so
    the "one" stays true.

    A second copy of the check was written into
    `ZebraStoreAdapter.update_trade_exit` on 2026-08-27 and taken straight back
    out: it reads the SAME flag on the SAME record downstream of this one, so
    while this guard stands the second can never be observed failing, which is
    the shape `feedback_a_second_guard_you_cannot_observe_is_decorative`
    describes. What made removing it safe is the call graph, so the call graph
    is what gets asserted — from the source, not from memory.

    `arm_time_stop`'s docstring records the precedent: a mutation reverting two
    of its three call sites survived the whole suite because nothing pinned
    where the single entry point was.
    """
    src = inspect.getsource(sm)
    tree = ast.parse(src)
    callers = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ('_close_spread_inner',
                                         '_close_fh_inner')):
                callers.setdefault(node.func.id, set()).add(fn.name)

    # TWO routes now, and that is allowed — but only because each carries its
    # own guard. M14's recovery sweep cannot go through `close_spread`: that
    # wrapper re-verifies the trigger and re-locks from 'open', both of which
    # are wrong for a record already frozen at 'partial_close'. So the rule is
    # not "one route" but "every route is guarded", and the test says so.
    #
    # `_finish_flat` is exempt from carrying its own copy for a structural
    # reason, asserted below rather than assumed: its ONLY caller is
    # `_recover_one`, which guards before reaching it.
    guarded_routes = {'close_spread': sm.close_spread,
                      '_recover_one': sm._recover_one}
    assert callers['_close_spread_inner'] == set(guarded_routes) | {
        '_finish_flat'}, (
        'a new caller reaches the spread close (%s). Give it its own paper '
        'guard and add it here, or route it through an existing guarded one.'
        % (callers['_close_spread_inner'],))
    assert callers['_close_fh_inner'] == {'close_fh_position'}

    # `_finish_flat` is reachable ONLY from the guarded sweep entry point.
    finish_flat_callers = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == '_finish_flat'):
                finish_flat_callers.add(fn.name)
    assert finish_flat_callers == {'_recover_one'}, (
        '_finish_flat has a new caller (%s) and carries no paper guard of its '
        'own' % (finish_flat_callers,))

    # And every guarded route actually carries the guard, before any order.
    for name, fn in guarded_routes.items():
        body = inspect.getsource(fn)
        assert '_record_says_paper' in body, (
            '%s reaches the order path with no paper guard' % name)

    # In `close_spread` the refusal must precede the close lock, or a paper
    # record is moved to 'closing' before being refused.
    body = inspect.getsource(sm.close_spread)
    assert body.index('_record_says_paper') < body.index('begin_close'), (
        'the paper refusal now sits AFTER the close lock — a paper record '
        'would be moved to "closing" before being refused')

    # Same property on the sweep, whose lock is `begin_recovery`, and whose
    # guard must also precede CLASSIFICATION — a paper record should not even
    # be described in terms of an action it can never take.
    body = inspect.getsource(sm._recover_one)
    assert body.index('_record_says_paper') < body.index('begin_recovery'), (
        'the sweep takes the recovery lock on a paper record before refusing '
        'it')
    assert body.index('_record_says_paper') < body.index('classify_frozen')


def test_the_two_paper_predicates_default_in_opposite_directions():
    """Deliberate, and the asymmetry must survive a "unifying" refactor.

    `is_paper_record` answers "may the money engine own this zebra record?" —
    absence means paper, because refusing keeps zebra booking it and the
    record still has an engine. `_record_says_paper` runs on all FOUR books and
    the bcs / bear_put / fallen_hero stores have never carried the key at all;
    defaulting those to paper would refuse to close live spreads on three
    books at once. Each defaults toward the engine that will actually act.
    """
    assert is_paper_record({}) is True
    assert sm._record_says_paper({}) is False
    # A BCS-store record, which has no `paper` key by construction.
    assert sm._record_says_paper({'id': 1, 'stock': 'ICICIBANK'}) is False
    # And they agree wherever the flag is actually present.
    for v in (True, False):
        assert is_paper_record({'paper': v}) is sm._record_says_paper(
            {'paper': v})


# ═══ C5.3 — a paper record keeps a booking engine after paper_mode: false ═══

def test_a_paper_record_still_books_when_paper_mode_is_off(zstore,
                                                           monkeypatch):
    """THE ARMING-DAY CASE. Before this, `_paper_auto_close` returned at its
    first line the moment the mode flipped, so every open paper position lost
    zebra — while the adapter (correctly) refuses to give it to the bridge.
    Two engines, zero coverage, and nothing in either log looks wrong."""
    s = zstore([_record(paper=True)])
    monkeypatch.setattr(zcfg, 'PAPER_MODE', False)
    monkeypatch.setattr(zcfg, 'EXITS_MANAGED_EXTERNALLY', True)
    monkeypatch.setattr(zmonitor, '_send_telegram', lambda m, **k: True)

    out = zmonitor._paper_auto_close(s, s.find(419), 40.00, 'tp', spot=1401.0)

    assert out is not None, (
        'a paper position was left with no booking engine at all on the day '
        'the switches flipped')
    assert s.find(419)['status'] == 'exited'
    assert s.find(419)['pnl'] > 0


def test_a_live_record_is_still_never_booked_in_paper(zstore, monkeypatch):
    """The other direction, unchanged: once the bridge owns a position, zebra
    must not book it — the record would leave `get_entered()` and a LIVE
    position would go unwatched."""
    s = zstore([_record(paper=False)])
    monkeypatch.setattr(zcfg, 'PAPER_MODE', True)
    monkeypatch.setattr(zcfg, 'EXITS_MANAGED_EXTERNALLY', True)
    monkeypatch.setattr(zmonitor, '_send_telegram', lambda m, **k: True)

    assert zmonitor._paper_auto_close(s, s.find(419), 40.00, 'tp',
                                      spot=1401.0) is None
    assert s.find(419)['status'] == 'entered'


def test_zebra_stands_down_only_for_records_the_order_path_will_act_on(
        monkeypatch):
    """They MUST agree. `_exits_external` is a ONE-SIDED stand-down: zebra
    stops acting on its own config. If it stands down for a record the order
    path then refuses to close, that position has NO exit engine at all — and
    nothing anywhere looks wrong, which is the silent state this whole
    interlock exists to prevent.

    So the condition zebra stands down on must be the same condition the money
    path acts on. Pinned as an identity, not as two independent assertions.
    """
    monkeypatch.setattr(zcfg, 'EXITS_MANAGED_EXTERNALLY', True)
    for paper in (True, False):
        rec = za.map_trade(_record(paper=paper))
        order_path_will_act = not sm._record_says_paper(rec)
        assert zmonitor._exits_external(rec) is order_path_will_act


# ═══ C5.4 + N1 — an unobserved fill is UNKNOWN, never zero ══════════════════

def test_only_our_own_orders_may_price_a_recovered_fill():
    """The fallback used to be `tagged_best or any_best`: with no tagged order
    it adopted the most recent matching order in the ACCOUNT. `kite.orders()`
    is TODAY only, so a manual trade this morning outranks the real close from
    yesterday — and a paper record has no orders of its own at all, so the
    fallback is the ONLY thing it could ever find."""
    kite = _flat()
    _fills(kite, (SHORT, 'BUY', 9.99, 'SOMEONE_ELSE'))
    rec = sm.find_recoverable_fill(kite, SHORT, 'BUY')
    assert rec['price'] is None, (
        "a stranger's fill was adopted as this trade's exit price")
    assert rec['untagged'] == 1
    assert sm._find_last_fill_price(kite, SHORT, 'BUY') is None


def test_a_tagged_fill_is_still_recovered():
    """Negative control — the guard must not break the case it was built for."""
    kite = _flat()
    _fills(kite, (SHORT, 'BUY', 9.99, 'SOMEONE_ELSE'),
           (SHORT, 'BUY', 10.20, sm.ORDER_TAG))
    assert sm._find_last_fill_price(kite, SHORT, 'BUY') == pytest.approx(10.20)


def test_an_already_flat_close_with_no_fills_books_NOTHING(env):
    """N1, the headline. Pre-fix this booked `exit_spread: 0.0` and therefore
    `pnl_per_share = 0.0 - 13.55` — a full max loss on a structure whose max
    loss IS the debit, computed from a price that was never observed."""
    spy = env
    kite = _flat()
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    out = sm._close_spread_inner(kite, store, _monitor_trade(), spot=1400.0,
                                 reason='TP', dry_run=False, label='BCS')

    assert out is False
    assert store.called('update_trade_exit') == [], (
        'a max loss was booked from a price nobody ever observed')
    assert kite.placed == []
    assert spy.any('CANNOT BE PRICED')


def test_an_already_flat_close_refuses_a_strangers_fill(env):
    """The compounding case: orders for these symbols DO exist in the account,
    none of them ours. Refusing is the whole point — booking them would record
    somebody else's execution as this trade's exit."""
    spy = env
    kite = _flat()
    _fills(kite, (SHORT, 'BUY', 30.00, None), (LONG, 'SELL', 5.00, None))
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    out = sm._close_spread_inner(kite, store, _monitor_trade(), spot=1400.0,
                                 reason='TP', dry_run=False, label='BCS')

    assert out is False
    assert store.called('update_trade_exit') == []
    assert spy.any('NONE tagged')


def test_an_already_flat_close_with_our_fills_still_books(env):
    """Negative control. The refusal must not swallow the case this branch was
    written for — a position closed by another process of OURS."""
    kite = _flat()
    _fills(kite, (SHORT, 'BUY', 10.20, sm.ORDER_TAG),
           (LONG, 'SELL', 50.20, sm.ORDER_TAG))
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    out = sm._close_spread_inner(kite, store, _monitor_trade(), spot=1400.0,
                                 reason='TP', dry_run=False, label='BCS')

    assert out is True
    booked = store.called('update_trade_exit')
    assert booked, 'a priced, flat close was not booked'
    data = booked[0][1][1]
    assert data['exit_spread'] == pytest.approx(40.00)
    assert data['pnl_per_share'] == pytest.approx(26.45)
    assert data['total_pnl'] > 0


def test_the_refused_record_is_left_recoverable_not_frozen(env):
    """It stays at 'closing': inside OPEN_STATUSES (capital keeps counting it,
    the scanner keeps blocking its stock), surfaced by `get_closing_trades`,
    and un-frozen by the crash-recovery sweep at the next process start —
    which is also what rate-limits the escalation to about once per run."""
    kite = _flat()
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    sm._close_spread_inner(kite, store, _monitor_trade(), spot=1400.0,
                           reason='TP', dry_run=False, label='BCS')

    assert store.called('update_trade_exit') == []
    assert store.called('set_trade_status') == [], (
        'the record was frozen at partial_close — that state means LIVE LEGS '
        'and needs a human at the broker, which is not what happened here')
    assert store.called('recover_closing_trade') == []


def test_the_escalation_is_deduped_per_trade_per_day(env):
    """It sits on a 5-second poll loop. One Telegram per day per trade, and
    the record is left recoverable so the next PROCESS start retries — that is
    what keeps an unresolvable position visible without becoming noise."""
    spy = env
    store = MemoryStore(trades=[_monitor_trade(status='open')])
    for _ in range(3):
        sm._close_spread_inner(_flat(), store, _monitor_trade(), spot=1400.0,
                               reason='TP', dry_run=False, label='BCS')
    assert len(spy.containing('CANNOT BE PRICED')) == 1


def test_the_fh_twin_refuses_too(env):
    """[[feedback_copy_pasted_modules_fix_once]] — the last two times a rule
    was fixed on one of these two paths and not the other, the untested twin
    shipped broken. Four legs, same rule."""
    spy = env
    SC, SP, LP = 'T26SEP3000CE', 'T26SEP2600PE', 'T26SEP2550PE'
    books = {f'NFO:{s}': {'bid': 10.0, 'ask': 10.2, 'bid_qty': 800,
                          'ask_qty': 800, 'ltp': 10.1, 'prev_close': 10.0}
             for s in (SC, SP, LP)}
    kite = FakeBroker(books=books,
                      positions=[{'tradingsymbol': s, 'quantity': 0}
                                 for s in (SC, SP, LP)])
    fh = {'id': 7, 'stock': 'TESTCO', 'status': 'open', 'quantity': 400,
          'exchange': 'NFO', 'short_call_symbol': SC, 'short_put_symbol': SP,
          'long_put_symbol': LP, 'long_call_symbol': None,
          'spot_symbol': 'NSE:TESTCO', 'total_credit': 97.75,
          'breakeven': 3097.75}
    store = MemoryStore(trades=[dict(fh)])

    out = sm._close_fh_inner(kite, store, dict(fh), spot=2700.0,
                             reason='SL_SPOT', dry_run=False)

    assert out is False
    assert store.called('update_trade_exit') == [], (
        'the FH twin still books a fabricated close cost')
    assert spy.any('CANNOT BE PRICED')


def test_a_zero_exit_is_a_real_price_when_it_was_observed(env):
    """The mirror of N1, and the reason the fix is "unknown != zero" rather
    than "reject zero". Long 10.00 / short 10.00 nets exactly 0.00, which is a
    genuine (terrible) close and must book as -debit, not as P&L zero."""
    kite = _flat()
    _fills(kite, (SHORT, 'BUY', 10.00, sm.ORDER_TAG),
           (LONG, 'SELL', 10.00, sm.ORDER_TAG))
    store = MemoryStore(trades=[_monitor_trade(status='open')])

    sm._close_spread_inner(kite, store, _monitor_trade(), spot=1400.0,
                           reason='SL_SPREAD', dry_run=False, label='BCS')

    data = store.called('update_trade_exit')[0][1][1]
    assert data['exit_spread'] == pytest.approx(0.0)
    assert data['pnl_per_share'] == pytest.approx(-13.55), (
        'an observed zero was reported as zero P&L — the old `if exit_net` '
        'test could not tell a real 0.00 from a missing price'
    )


def test_no_call_site_turns_a_missing_fill_back_into_zero():
    """The defect was a fallback literal, so pin the literal.

    `_find_last_fill_price` returning `Optional[float]` is only worth anything
    while no caller writes `or 0` / `or 0.0` over the top of it — which is the
    single most tempting way to "fix" the type error this change introduces.
    """
    src = inspect.getsource(sm)
    for bad in ('_find_last_fill_price(kite, symbol, txn_type) or 0',
                'find_recoverable_fill(kite, symbol, txn_type) or 0'):
        assert bad not in src
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.BoolOp) or not isinstance(node.op, ast.Or):
            continue
        first = node.values[0]
        if (isinstance(first, ast.Call)
                and isinstance(first.func, ast.Name)
                and first.func.id in ('_find_last_fill_price',
                                      'find_recoverable_fill')):
            raise AssertionError(
                'a caller coerced a missing fill back to a number — an '
                'unobserved price is UNKNOWN, and this is exactly how N1 got '
                'in')


# ═══ The two together: arming day, one paper and one live position ══════════

def test_paper_books_through_zebra_and_live_books_through_the_bridge(
        zstore, env, monkeypatch):
    """The scenario the whole change exists for.

    Two cohort positions, both triggering a take-profit, with
    `paper_mode: false` and exits handed to the bridge. They must resolve
    through DIFFERENT engines, and neither may touch the other's.
    """
    spy = env
    s = zstore([_record(id=419, stock='PAPERCO', paper=True),
                _record(id=420, stock='REALCO', paper=False)])
    monkeypatch.setattr(zcfg, 'PAPER_MODE', False)
    monkeypatch.setattr(zcfg, 'EXITS_MANAGED_EXTERNALLY', True)
    monkeypatch.setattr(zmonitor, '_send_telegram', lambda m, **k: True)
    bridge = za.ZebraStoreAdapter(s)

    # -- the monitor WATCHES both (--list, and the dry-run journal) --------
    assert [t['id'] for t in bridge.get_open_trades()] == [419, 420]

    # -- but it may only TRADE one of them ---------------------------------
    paper_trade = bridge.get_open_trades()[0]
    kite0 = FakeBroker(books=BOOKS,
                       positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                  {'tradingsymbol': LONG, 'quantity': QTY}])
    assert sm.close_spread(kite0, paper_trade, spot=1401.0, reason='TP',
                           dry_run=False, store=bridge) == 'ABORT'
    assert kite0.placed == []
    assert s.find(419)['status'] == 'entered'

    # -- the paper one books in zebra, with no broker involved at all ------
    booked = zmonitor._paper_auto_close(s, s.find(419), 40.00, 'tp',
                                        spot=1401.0)
    assert booked is not None
    assert s.find(419)['status'] == 'exited'
    assert s.find(419)['exit_reason'] == 'paper:tp'
    assert s.find(419)['pnl'] > 0

    # -- the live one closes for real and books through the bridge ---------
    live = [t for t in bridge.get_open_trades() if t['id'] == 420][0]
    kite = FakeBroker(books=BOOKS,
                      positions=[{'tradingsymbol': SHORT, 'quantity': -QTY},
                                 {'tradingsymbol': LONG, 'quantity': QTY}])
    ok = sm._close_spread_inner(kite, bridge, live, spot=1401.0, reason='TP',
                                dry_run=False, label='BCS')

    assert ok is True
    assert kite.orders_for(SHORT), 'the short leg was never bought back'
    assert kite.orders_for(LONG), 'the long leg was never sold'
    t = s.find(420)
    assert t['status'] == 'exited'
    assert t['exit_reason'] == 'tp'          # no `paper:` prefix — real orders
    assert t['pnl'] > 0

    # -- and the paper record's only Telegram says nothing was placed ------
    paperco = spy.containing('PAPERCO')
    assert paperco, 'the refusal was silent — the bridge being handed a paper '\
                    'record is a wiring fault somebody has to hear about'
    assert all('No order placed' in m for m in paperco)


# ═══ N2 — the `expiry` omission, verified and deliberately left alone ═══════

def test_expiry_is_still_absent_from_the_managed_set():
    """Audited 2026-08-27 (N2) and LEFT AS IT IS.

    The omission looks like a hole — the one exit kind that fires on expiry day
    escaping the backstop that declines a paper close while the bridge owns the
    position. It is not one, and the reason is that `'expiry'` does not reach
    `_paper_auto_close` on expiry DAY at all. Its only caller,
    `_settle_if_expired`, refuses unless `today > exp`: strictly PAST expiry,
    when the contracts have auto-exercised and there is no option left for the
    order path to close. Declining there would protect nothing and would strand
    the record at `entered` for good, banning its stock from the scanner by
    dedup.
    """
    assert 'expiry' not in zmonitor.EXTERNALLY_MANAGED_EXITS


def test_the_terminal_settle_never_fires_on_expiry_day(zstore, monkeypatch):
    """The invariant the omission rests on. Expiry day itself still trades —
    and on expiry day the bridge's own `EXPIRY_FORCE_CLOSE` owns the close."""
    s = zstore([_record(paper=False, expiry='2026-09-29')])
    monkeypatch.setattr(zmonitor, '_send_telegram', lambda m, **k: True)
    monkeypatch.setattr(zcfg, 'PAPER_MODE', True)

    on_the_day = datetime(2026, 9, 29).date()
    assert zmonitor._settle_if_expired(s, s.find(419), spot=1401.0,
                                       today=on_the_day, dry_run=True) is False
    assert s.find(419)['status'] == 'entered'


def test_only_the_terminal_settle_may_close_on_expiry():
    """What makes the omission safe is that there is exactly ONE caller. A
    second `_paper_auto_close(..., 'expiry', ...)` — especially one on expiry
    DAY, where the legs are still live — would turn the documented carve-out
    into the hole it currently is not. Read the source, not a memory of it.
    """
    tree = ast.parse(inspect.getsource(zmonitor))
    sites = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == '_paper_auto_close'):
                for arg in node.args:
                    if isinstance(arg, ast.Str) and arg.s == 'expiry':
                        sites.append(fn.name)
    assert sites == ['_settle_if_expired'], (
        'a second expiry close appeared (%s). EXTERNALLY_MANAGED_EXITS omits '
        "'expiry' on the strength of there being exactly one caller, and that "
        'caller firing only strictly PAST expiry. Either revert the new call '
        'site or add expiry to the managed set.' % (sites,))

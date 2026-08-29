"""An ORPHAN LONG from a failed entry round was alert-once, then invisible.

`bcs/entry_executor.py` never unwinds an orphan and never will: placing a
corrective order through the book that has just failed to fill is the
amplification that turned a Feb-2026 stop into a four-fill loss. So it REPORTS
the leg and stops. That was right, and incomplete — "reports" meant ONE
Telegram, after which the leg existed in no store at all. The frozen sweep, the
post-close residue sweep, the startup verification and `--list` all read
RECORDS, so every one of them missed it.

It is the entry-side twin of S3, and S3 was judged worth building. The two
share one machine (`ResidueKind`) because everything between detection and
resolution is identical — persist on the record, re-read the broker, two DATED
consecutive flat reads outside the opening window, one nag a day, and never an
order.

Five ways an entry leaves something behind, all covered below:

  1. a round bought its long and could not sell its short (`orphan`);
  2. a leg filled ODD-SIZED (`partials`) — those shares are held;
  3. complete spreads filled and the DEBIT could not be computed, so no record
     was written. Every leg is unaccounted for, not just an orphan;
  4. complete spreads filled and the STORE refused the record;
  5. the order path RAISED, so even the report of what filled is gone. That
     fifth branch was found by the source check in this file, not by a review.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_entry_residue.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.fakes import FakeBroker, FakeClock, MemoryStore, TelegramSpy  # noqa: E402
from zebra import monitor as zmon                                  # noqa: E402

LONG = 'TESTCO26SEP1340CE'
SHORT = 'TESTCO26SEP1390CE'
LOT = 700


def _signal(**over):
    """The record an entry was attempted for. `triggered` when nothing filled,
    `entered` when some spreads did — an entry residue can sit on either, which
    is why its lister has no status filter."""
    t = {'id': 1, 'stock': 'TESTCO', 'status': 'triggered',
         'long_symbol': LONG, 'short_symbol': SHORT, 'exchange': 'NFO',
         'spot_symbol': 'NSE:TESTCO', 'quantity': LOT}
    t.update(over)
    return t


def _pos(symbol, qty):
    return {'tradingsymbol': symbol, 'quantity': qty}


def _out(**over):
    o = {'stock': 'TESTCO', 'lots_requested': 2, 'lots_filled': 0,
         'long_fills': [], 'short_fills': [], 'orphan': None,
         'partials': [], 'problems': []}
    o.update(over)
    return o


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    FakeClock().install(monkeypatch, sm)
    sm._RECOVERY_NAGGED.clear()
    spy = TelegramSpy().install(monkeypatch, sm)
    monkeypatch.setattr(zmon, '_send_telegram', lambda m, **k: True)
    return spy


@pytest.fixture
def spy(_env):
    return _env


def _books(store, label='COHORT'):
    return [(label, store, True)]


# -- what counts as a residue ------------------------------------------------

def test_an_orphan_long_is_recorded():
    """THE DEFECT. The executor reports it, declines to unwind it, and until
    now nothing wrote it down."""
    trade = _signal()
    store = MemoryStore(trades=[trade])
    out = _out(orphan={'symbol': LONG, 'qty': LOT, 'fill': 21.2})

    assert zmon._record_entry_residue(store, trade, out, 'no spread') is True
    res = store.trades[0]['entry_residue']
    assert res['state'] == 'open'
    assert res['legs'] == {LONG: LOT}
    assert res['watch'] == [LONG]
    assert LONG in res['detail']


def test_a_partial_fill_is_a_residue_too():
    """`wait_for_fill` documents returning a CANCELLED order carrying
    `filled_quantity > 0`. Those shares are held."""
    trade = _signal()
    store = MemoryStore(trades=[trade])
    out = _out(partials=[{'symbol': SHORT, 'qty': 300, 'fill': 7.6,
                          'round': 1}])

    assert zmon._record_entry_residue(store, trade, out, 'partial') is True
    assert store.trades[0]['entry_residue']['legs'] == {SHORT: 300}


def test_filled_spreads_with_no_record_name_BOTH_legs():
    """Case 3 and 4. Naming only the orphan would understate what is at the
    broker: the spreads themselves are the unaccounted position."""
    trade = _signal()
    store = MemoryStore(trades=[trade])
    bcs = {'long_symbol': LONG, 'short_symbol': SHORT, 'lot_size': LOT}
    out = _out(lots_filled=2)

    assert zmon._record_entry_residue(
        store, trade, out, 'debit uncomputable',
        extra=zmon._filled_legs(bcs, out)) is True
    assert store.trades[0]['entry_residue']['legs'] == {LONG: 2 * LOT,
                                                        SHORT: -2 * LOT}


def test_a_clean_entry_records_nothing():
    """The negative control. Without it every test here passes just as well
    when the writer records unconditionally, and the sweep then nags daily
    about a book that is exactly as intended."""
    trade = _signal()
    store = MemoryStore(trades=[trade])
    assert zmon._record_entry_residue(store, trade, _out(lots_filled=2),
                                      'clean') is False
    assert 'entry_residue' not in store.trades[0]


def test_a_dry_run_records_nothing():
    """A dry run places nothing, so there is nothing at the broker to chase.
    Recording one would manufacture an incident and then nag daily until
    somebody went looking for a leg that never existed."""
    trade = _signal()
    store = MemoryStore(trades=[trade])
    out = _out(orphan={'symbol': LONG, 'qty': LOT, 'fill': 21.2})
    assert zmon._record_entry_residue(store, trade, out, 'x',
                                      dry_run=True) is False
    assert 'entry_residue' not in store.trades[0]


def test_a_failed_write_alerts_rather_than_going_quiet(monkeypatch):
    """An accounting failure on top of a live leg must not be silent. It also
    must not raise: this runs immediately after real orders."""
    trade = _signal()

    class _Broken(MemoryStore):
        def update_trade_fields(self, *a, **k):
            raise RuntimeError('store is gone')

    sent = []
    monkeypatch.setattr(zmon, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    out = _out(orphan={'symbol': LONG, 'qty': LOT, 'fill': 21.2})
    assert zmon._record_entry_residue(_Broken(trades=[trade]), trade, out,
                                      'x') is False
    assert sent and 'Nothing will chase it' in sent[0]


# -- every entry branch that can leave something behind ----------------------

@pytest.mark.parametrize('branch', [
    'nothing_filled', 'unpriceable', 'store_refused', 'recorded_with_orphan',
    'executor_raised'])
def test_every_entry_branch_that_can_orphan_a_leg_records_it(branch):
    """Read off the source, over every post-order exit from `_auto_enter_bcs`.

    Behavioural coverage of these needs a broker, a capital plan, a vet and a
    store all standing up at once; what actually has to hold is that no RETURN
    path from that function can drop a leg on the floor, and that is a
    property of the branch structure. A missed branch is the whole defect
    reappearing on the one path nobody exercised — and this test found one
    (`executor_raised`) that the design had not.
    """
    import inspect
    # `_auto_enter_bcs` is the function that calls the executor; `_enter_as_bcs`
    # only decides whether to. Named explicitly rather than searched for, so
    # that moving the order call to another function fails this test loudly
    # instead of silently checking nothing.
    lines = inspect.getsource(zmon._auto_enter_bcs).splitlines()
    # Only the returns AFTER the orders go out. Everything above `open_spread`
    # is a refusal that placed nothing, and demanding a residue check there
    # would assert the opposite of `feedback_no_rush_to_enter`.
    placed = next(i for i, l in enumerate(lines) if 'ee.open_spread(' in l)
    returns = [i for i, l in enumerate(lines)
               if i > placed and l.strip() in ('return None', 'return fresh')]
    assert len(returns) >= 5, (
        'expected the five post-order exits from _auto_enter_bcs; found %d'
        % len(returns))
    for i in returns:
        window = '\n'.join(lines[max(placed, i - 18):i])
        assert '_record_entry_residue' in window, (
            'a return at line %d of _auto_enter_bcs leaves after orders went '
            'out without recording what may be at the broker:\n%s'
            % (i, window))


def test_the_branch_where_the_report_itself_is_lost_still_records():
    """`open_spread` documents that it returns what filled whatever happens,
    so its caller's `except` means the failure was OUTSIDE that guard -- and
    `out` is gone, taking any orphan or partial with it.

    Nothing can say what is at the broker from here. The SWEEP can, so the
    intended legs are recorded and it asks: if nothing filled they read flat
    and the incident resolves itself in two confirmations. This branch was
    found by the source check above, not by a review.
    """
    trade = _signal()
    store = MemoryStore(trades=[trade])
    bcs = {'long_symbol': LONG, 'short_symbol': SHORT, 'lot_size': LOT}
    assert zmon._record_entry_residue(
        store, trade, {}, 'the order path RAISED',
        extra={bcs['long_symbol']: 0, bcs['short_symbol']: 0}) is True
    assert set(store.trades[0]['entry_residue']['watch']) == {LONG, SHORT}


def test_the_executor_still_never_unwinds():
    """The residue is BOOKKEEPING. If recording it ever turned into acting on
    it, this change would have introduced the exact amplification the entry
    path was designed to refuse."""
    import inspect
    from bcs import entry_executor as ee
    src = inspect.getsource(zmon._record_entry_residue)
    for forbidden in ('place_limit_order', 'open_leg', 'close_leg',
                      'cancel_order'):
        assert forbidden not in src
    assert 'never unwinds' in inspect.getdoc(ee) or \
        'NOT unwound' in inspect.getsource(ee)


# -- the sweep ---------------------------------------------------------------

def _seed(trade, legs):
    trade['entry_residue'] = {
        'state': 'open', 'detected_at': '2026-09-15T11:00:00',
        'resolved_at': None, 'watch': sorted(legs),
        'legs': dict(legs), 'label': 'COHORT',
        'detail': '; '.join('%s x%s' % kv for kv in sorted(legs.items())),
    }
    return trade


def test_the_sweep_finds_a_record_that_is_not_closed():
    """The difference from the post-close twin, stated as a test. A record at
    `triggered` never became a position, so no status filter can find it --
    and that is exactly the case where nothing filled but a leg was bought."""
    trade = _seed(_signal(status='triggered'), {LONG: LOT})
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(LONG, LOT)])

    assert sm.sweep_entry_residue(kite, _books(store)) == 1


def test_it_watches_the_ORPHAN_leg_not_the_records_declared_legs():
    """The record declares the spread that was INTENDED. The orphan is the
    half that did not happen, so re-deriving the legs from the record would
    ask the broker about a short leg that was never sold — and find it flat,
    and resolve an incident about a long that is still there.
    """
    trade = _seed(_signal(), {LONG: LOT})
    store = MemoryStore(trades=[trade])
    # The SHORT is flat (it never filled). The LONG is live.
    kite = FakeBroker(positions=[_pos(LONG, LOT)])
    assert sm.sweep_entry_residue(kite, _books(store)) == 1
    assert store.trades[0]['entry_residue']['state'] == 'open'


def test_it_resolves_itself_when_the_broker_goes_flat(monkeypatch, spy):
    """Self-resolution read from the BROKER, not from our own record — and on
    two DATED consecutive flat reads, because one successful-but-wrong
    `positions()` would otherwise resolve a terminal incident for good."""
    monkeypatch.setattr(sm, 'is_market_settled', lambda *a, **k: True)
    trade = _seed(_signal(), {LONG: LOT})
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[])

    assert sm.sweep_entry_residue(kite, _books(store)) == 1     # 1st flat read
    assert store.trades[0]['entry_residue']['state'] == 'open'
    assert sm.sweep_entry_residue(kite, _books(store)) == 0     # 2nd confirms
    assert store.trades[0]['entry_residue']['state'] == 'resolved'


def test_it_does_not_resolve_in_the_opening_window(monkeypatch):
    """Two reads five seconds apart do not defeat a CORRELATED burst, and the
    sync window at the open is exactly that."""
    monkeypatch.setattr(sm, 'is_market_settled', lambda *a, **k: False)
    trade = _seed(_signal(), {LONG: LOT})
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[])
    for _ in range(4):
        assert sm.sweep_entry_residue(kite, _books(store)) == 1
    assert store.trades[0]['entry_residue']['state'] == 'open'


def test_the_sweep_places_no_order():
    """The rule both species are bound by. `orders_allowed` is in the books
    tuple and no value of it authorises anything here."""
    import inspect
    src = inspect.getsource(sm.sweep_reconcile_residue)
    for forbidden in ('place_limit_order', 'close_spread', 'close_leg',
                      'begin_close', 'update_trade_exit'):
        assert forbidden not in src


def test_the_nag_names_the_ENTRY_story_not_the_close_one(spy):
    """Two species, two next actions. Telling the owner a leg is 'still live
    on a closed trade' when an ENTRY left it sends them to the wrong screen
    and the wrong decision."""
    trade = _seed(_signal(), {LONG: LOT})
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(LONG, LOT)])

    sm.sweep_entry_residue(kite, _books(store))
    assert spy.any('AN ENTRY LEFT A LEG NOTHING IS MANAGING')
    assert not spy.any('booked closed')
    assert spy.any('no stop, no trail and no target applies to it')


def test_the_two_species_do_not_silence_each_other(spy):
    """The nag key carries the incident's field. One record can carry both —
    an entry orphan and, later, a post-close residue — and a key that named
    only the record would let the first suppress the second for the day.
    """
    trade = _seed(_signal(status='closed'), {LONG: LOT})
    trade['reconcile_residue'] = {
        'state': 'open', 'detected_at': '2026-09-15T11:00:00',
        'resolved_at': None, 'watch': [SHORT], 'legs': {SHORT: -LOT},
        'label': 'COHORT', 'detail': SHORT,
    }
    store = MemoryStore(trades=[trade])
    kite = FakeBroker(positions=[_pos(LONG, LOT), _pos(SHORT, -LOT)])

    sm.sweep_reconcile_residue(kite, _books(store))
    sm.sweep_entry_residue(kite, _books(store))
    assert spy.any('A LEG IS STILL LIVE ON A CLOSED TRADE')
    assert spy.any('AN ENTRY LEFT A LEG NOTHING IS MANAGING')


def test_both_sweeps_run_from_the_poll_loop():
    """A sweep nobody calls is indistinguishable from one that does not exist
    — which is how eight live positions once answered `Open: 0`."""
    import inspect
    src = inspect.getsource(sm.monitor_all)
    assert 'sweep_reconcile_residue(' in src
    assert 'sweep_entry_residue(' in src


def test_every_book_can_answer_the_question():
    """All four stores implement the lister, including the three that cannot
    currently hold one. `hasattr` is False otherwise, and the sweep would log
    "cannot list residues" for three books on every five-second poll — noise
    that buries the line that matters."""
    from bcs.trade_store import TradeStore
    from bear_put.trade_store import BearPutStore
    from fallen_hero.trade_store import FallenHeroStore
    from zebra.trade_store import ZebraStore
    from bcs.zebra_adapter import ZebraStoreAdapter
    from bcs.tests.fakes import MemoryStore as _Fake
    for cls in (TradeStore, BearPutStore, FallenHeroStore, ZebraStore,
                ZebraStoreAdapter, _Fake):
        assert hasattr(cls, 'get_entry_residue_trades'), cls.__name__


def test_the_lister_has_no_status_filter():
    """The one place the two species genuinely differ. A status filter here
    would hide the case the whole item is about: nothing filled, the record
    never became a position, and a long leg is at the broker."""
    trade = _seed(_signal(status='triggered'), {LONG: LOT})
    store = MemoryStore(trades=[trade, _seed(_signal(id=2, status='closed'),
                                             {SHORT: -LOT})])
    assert {t['id'] for t in store.get_entry_residue_trades()} == {1, 2}

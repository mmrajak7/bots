"""Claude exit vetting on the ORDER path (P1.4).

`zebra/monitor.py` has gated every price-driven exit through `_exit_cleared`
since the NHPC loss. `bcs/spread_monitor.py` — the only code that can place a
real order — never had any vetting at all. So the exit bridge created a hazard
of its own: pointing the order path at the cohort WITHOUT carrying the gate
across would remove a control that exists today, on the one path that can lose
real money.

Two properties are asserted here, and the second matters more than the first
(`feedback_guards_need_the_inverse_review` — every guard in this file was
designed against a FALSE fire, none against blocking a REAL exit):

1. a `wait` / `hold` verdict stops the orders;
2. a BROKEN vetting layer does not. It fails OPEN to the deterministic guards,
   the same contract the kill switch has.
"""
from datetime import date

import pytest

from bcs import exit_vet
from bcs import spread_monitor as sm
from bcs.tests.replay import Tick, run_session


# -- reason -> kind ----------------------------------------------------------

@pytest.mark.parametrize('reason,kind', [
    ('SL_SPOT', 'spot_sl'),
    ('SL_SPREAD', 'debit_sl'),
    ('SL_TRAIL', 'trail'),
    ('TP', 'tp'),
])
def test_every_price_driven_reason_maps_to_a_vet_kind(reason, kind):
    assert exit_vet.vet_kind(reason) == kind


def test_the_four_kinds_are_exactly_the_ones_the_vet_knows():
    """A kind this module invented would be vetted against a marker nothing
    else reads, and would silently never be answered."""
    from zebra import vet as vet_mod
    assert set(exit_vet.VET_KIND.values()) == set(vet_mod.EXIT_KINDS)


def test_the_expiry_force_close_is_not_vetted():
    """Calendar-driven, matching zebra's ungated TIME. There is no suspect
    price for an agent to judge, and holding one on a pending verdict trades a
    known delivery-margin deadline for an unknown wait."""
    assert exit_vet.vet_kind('EXPIRY_FORCE_CLOSE') is None


# -- quote translation -------------------------------------------------------

_TRADE = {'id': 1, 'stock': 'X', '_store_type': 'zebra',
          'long_symbol': 'X26SEP100CE', 'short_symbol': 'X26SEP110CE'}


def test_a_healthy_quote_translates_to_a_reliable_book():
    q = exit_vet.as_vet_quote(_TRADE, {
        'spread': 6.20,
        'unreliable': None,
        'long': {'bid': 40.0, 'ask': 40.2, 'ltp': 40.1},
        'short': {'bid': 33.8, 'ask': 34.0, 'ltp': 33.9},
    })
    assert q['reliable'] is True
    assert q['mid'] == 6.20
    assert q['legs']['long']['symbol'] == 'X26SEP100CE'
    assert q['legs']['long']['bid'] == 40.0
    assert q['legs']['short']['spread_pct'] is not None


def test_an_unreliable_quote_stays_unreliable():
    q = exit_vet.as_vet_quote(_TRADE, {'spread': None,
                                       'unreliable': 'long width 133%',
                                       'long': {}, 'short': {}})
    assert q['reliable'] is False
    assert q['reason'] == 'long width 133%'


def test_no_quote_at_all_reads_as_unreliable_not_as_fine():
    """The default must be "look at it". An exit with no book is the most
    suspicious kind there is, and `needs_exit_vet` keys off exactly this."""
    q = exit_vet.as_vet_quote(_TRADE, None)
    assert q['reliable'] is False
    assert q['reason'] == 'no_quote'
    from zebra import vet as vet_mod
    needed, _why = vet_mod.needs_exit_vet(_TRADE, 'tp', q)
    assert needed is True


def test_a_translated_quote_is_never_marked_floored():
    """`get_spread_value` REFUSES a below-floor valuation rather than clamping
    it, so a floored mid cannot reach the vet from this path. Saying otherwise
    would tell the agent a real quote was an estimate."""
    q = exit_vet.as_vet_quote(_TRADE, {'spread': 1.0, 'unreliable': None,
                                       'long': {'bid': 1, 'ask': 1.1},
                                       'short': {'bid': 0.1, 'ask': 0.2}})
    assert q['floored'] is False


# -- the gate ----------------------------------------------------------------

class _Gate:
    """Stands in for `zebra.monitor._exit_cleared`."""

    def __init__(self, answer=True, boom=None):
        self.answer, self.boom, self.calls = answer, boom, []

    def __call__(self, store, trade, kind, quote, spot, dry_run=False,
                 **kw):
        # **kw, because `exit_cleared` wraps this call in `except Exception ->
        # True`. A stub with a narrower signature than production turns a
        # TypeError into a SILENT FAIL-OPEN: every test in this file passed
        # while asserting the opposite of what it meant. That is not only a
        # test problem -- the same mismatch in production disables vetting on
        # the live order path and logs it as "EXIT VET ERROR ... proceeding".
        # `test_the_real_gate_accepts_what_this_module_sends` pins it there.
        self.calls.append({'store': store, 'trade': trade, 'kind': kind,
                           'quote': quote, 'spot': spot, 'dry_run': dry_run,
                           **kw})
        if self.boom:
            raise self.boom
        return self.answer


@pytest.fixture
def gate(monkeypatch):
    import zebra.monitor as zm
    g = _Gate()
    monkeypatch.setattr(zm, '_exit_cleared', g)
    return g


class _Store:
    def __init__(self):
        self.raw = object()


def test_the_real_gate_accepts_what_this_module_sends():
    """A signature mismatch here disables vetting SILENTLY in production.

    `exit_cleared` wraps the gate call in `except Exception -> True`, which is
    the right contract for a dead vetting layer and the wrong one for a typo:
    a keyword the real `zebra.monitor._exit_cleared` does not accept raises
    TypeError, is caught, and every live value stop then fires unvetted while
    the log says "EXIT VET ERROR ... proceeding on the deterministic guards".
    That reads exactly like a Claude outage.

    So the call is checked against the real signature, without running it.
    """
    import inspect
    import re
    from zebra import monitor
    sig = inspect.signature(monitor._exit_cleared)
    src = inspect.getsource(exit_vet.exit_cleared)
    call = src[src.index('_gate('):]
    call = call[:call.index(')')]
    sent = set(re.findall(r'(\w+)=', call))
    assert sent <= set(sig.parameters), (
        'bcs/exit_vet.py sends %r; zebra.monitor._exit_cleared takes %r'
        % (sorted(sent), sorted(sig.parameters)))


def test_the_order_path_never_blocks_in_line_for_a_verdict():
    """M12 is a CRON optimisation and a five-second-poll pessimisation.

    zebra looks at the marker once every 5 minutes, so waiting 2 minutes
    in-line converts ~3 minutes of a fired-but-unfilled stop into nothing.
    This engine looks again in 5 seconds; blocking its loop would stop
    watching every OTHER position on all four books to buy nothing.
    """
    import inspect
    src = inspect.getsource(exit_vet.exit_cleared)
    assert 'incycle_wait=0' in src


def test_a_held_verdict_stops_the_exit(gate):
    gate.answer = False
    assert exit_vet.exit_cleared(_Store(), _TRADE, 'TP', None, 100.0) is False


def test_a_cleared_verdict_lets_the_exit_through(gate):
    assert exit_vet.exit_cleared(_Store(), _TRADE, 'TP', None, 100.0) is True


def test_a_broken_vetting_layer_still_lets_the_stop_fire(gate):
    """THE INVERSE TEST, and the one that matters more.

    Every guard in the order path was reviewed against the incident that
    caused it; none against refusing a GENUINE exit, which looks perfectly
    healthy in every log. A dead vetting layer must never strand a live stop.
    """
    gate.boom = RuntimeError('vet subsystem is down')
    said = []
    assert exit_vet.exit_cleared(_Store(), _TRADE, 'SL_SPREAD', None, 100.0,
                                 log=said.append) is True
    assert any('EXIT VET ERROR' in m for m in said), \
        'failing open silently is how a dead guard looks deployed'


def test_the_gate_gets_the_raw_store_not_the_adapter(gate):
    """`exit_gate` writes vet markers through zebra's own store methods
    (`set_alert_flag_daily`, `find`). The adapter does not have them."""
    st = _Store()
    exit_vet.exit_cleared(st, _TRADE, 'TP', None, 100.0)
    assert gate.calls[0]['store'] is st.raw


def test_the_other_three_books_are_not_vetted(gate):
    """bcs / bear_put / fallen_hero have no vet marker schema. Inventing one
    for them is not this change; calling the gate anyway would write markers
    into a store nothing reads them back from."""
    for st in ('bcs', 'bps', 'fh'):
        t = dict(_TRADE, _store_type=st)
        assert exit_vet.exit_cleared(_Store(), t, 'TP', None, 100.0) is True
    assert gate.calls == []


def test_the_expiry_force_close_never_reaches_the_gate(gate):
    assert exit_vet.exit_cleared(_Store(), _TRADE, 'EXPIRY_FORCE_CLOSE', None,
                                 100.0) is True
    assert gate.calls == []


def test_dry_run_does_not_touch_the_gate(gate):
    """Dry run means "monitor everything, change nothing", and `exit_gate` is
    not read-only: it writes markers to the live store and spawns agents.
    While the monitor is in dry run, zebra still owns these exits, so vetting
    here would also race zebra's gate over one shared marker."""
    said = []
    assert exit_vet.exit_cleared(_Store(), _TRADE, 'TP', None, 100.0,
                                 dry_run=True, log=said.append) is True
    assert gate.calls == []
    assert any('EXIT VET skipped' in m for m in said), \
        'a skipped gate that says nothing is indistinguishable from a gate ' \
        'that ran and cleared'


# -- end to end, through the real monitor_all --------------------------------

_DAY = date(2026, 9, 15)
_L, _S = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
_QTY = 700

COHORT_TRADE = {
    'id': 419, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
    'cohort': '2026-08-14', 'structure': 'bcs', 'direction': 'CE',
    'long_symbol': _L, 'short_symbol': _S, 'spot_symbol': 'NSE:TESTCO',
    'exchange': 'NFO', 'quantity': _QTY, 'lot_size': _QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': 1435.0, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'entry_spot': 1360.0, 'expiry': '2026-09-29',
    'spot_sl_enabled': False, 'trail_policy': 'gain_anchored',
    'time_policy': 'sessions_before_expiry', 'time_stop_sessions': 5,
}

_LONG_BOOK = {'bid': 100.00, 'bid_qty': 1400, 'ask': 100.20, 'ask_qty': 1400,
              'ltp': 100.10, 'prev_close': 21.0}
_SHORT_BOOK = {'bid': 52.00, 'bid_qty': 1400, 'ask': 52.20, 'ask_qty': 1400,
               'ltp': 52.10, 'prev_close': 7.6}
def _pos():
    """A FRESH position list per replay.

    `TickBroker` mutates the dicts it is given as legs fill, so a module-level
    list is drained by the first test that closes and every later replay then
    starts with both legs already flat -- it reports "closed by another
    process", places nothing, and an assertion on `kite.placed` fails for a
    reason that has nothing to do with the code under test. Same family as
    `feedback_fake_must_not_be_safer_than_production`: shared mutable state in
    the harness inventing behaviour production does not have.
    """
    return [{'tradingsymbol': _S, 'quantity': -_QTY},
            {'tradingsymbol': _L, 'quantity': _QTY}]

#: Spot through target_spot 1435 — a TP, which is spot-driven and needs no
#: settled book, so the only thing between the trigger and the orders is the
#: vet.
_TP_TICKS = [Tick(t, 1440.0, _LONG_BOOK, _SHORT_BOOK, 'spot through target')
             for t in ('11:00:00', '11:00:06', '11:00:12', '11:00:30',
                       '11:01:00', '11:02:00')]


def _run_tp(monkeypatch, dry_run=False):
    trade = dict(COHORT_TRADE)
    return run_session(monkeypatch, sm, trade, _TP_TICKS, _DAY, _pos(),
                       dry_run=dry_run, cohort=[trade])


def test_a_held_verdict_places_no_order_at_all(monkeypatch, gate):
    """Not "an intent with no result" — no intent. The trade is still open and
    the trigger re-arms, which is what the caller's ABORT branch is for."""
    gate.answer = False
    _clock, kite, _store, _spy = _run_tp(monkeypatch)
    quoted = {q.split(':')[-1] for q in kite.quoted}
    assert _L in quoted and _S in quoted, (
        'the position was never quoted, so it was never monitored — this '
        'test would pass for the wrong reason')
    assert gate.calls, 'the vet was never consulted on a firing TP'
    assert kite.placed == [], (
        'a held exit still placed orders: %r' % (kite.placed,))


def test_the_same_tp_DOES_close_once_the_vet_clears(monkeypatch, gate):
    """Negative control. Without it, the test above passes just as well when
    TP is broken for everyone."""
    gate.answer = True
    _clock, kite, _store, _spy = _run_tp(monkeypatch)
    assert kite.placed, 'TP no longer closes a cohort position at all'


def test_the_vet_judges_the_book_the_trigger_fired_on(monkeypatch, gate):
    """Translated, never re-fetched. A second fetch that happened to look
    clean would skip vetting on a trigger that fired off a dirty book."""
    _clock, _kite, _store, _spy = _run_tp(monkeypatch)
    q = gate.calls[0]['quote']
    assert q['legs']['long']['bid'] == _LONG_BOOK['bid']
    assert q['legs']['short']['ask'] == _SHORT_BOOK['ask']
    assert gate.calls[0]['kind'] == 'tp'


def test_dry_run_never_consults_the_gate_end_to_end(monkeypatch, gate):
    _clock, kite, _store, _spy = _run_tp(monkeypatch, dry_run=True)
    quoted = {q.split(':')[-1] for q in kite.quoted}
    assert _L in quoted, 'the dry run did not even watch the position'
    assert gate.calls == [], (
        'dry run consulted the vet: that writes markers to the live store and '
        'spawns agents, and races zebra which still owns these exits')


def test_a_held_verdict_leaves_no_intent_in_the_journal(monkeypatch, gate):
    """The journal is the artifact, not the fake broker.

    `feedback_live_automation_bar` asks for evidence, and an INTENT with no
    RESULT is the specific shape that means an order may be live at the broker
    while this system believes nothing happened. A vetoed exit must leave
    NEITHER line -- the veto happens before `place_limit_order` is reached at
    all.
    """
    from bcs import order_journal
    gate.answer = False
    _run_tp(monkeypatch)
    assert order_journal.read_day() == [], (
        'a held exit wrote to the order journal: %r' % (order_journal.read_day(),))


def test_the_journal_DOES_record_the_cleared_exit(monkeypatch, gate):
    """Negative control for the assertion above, which is otherwise satisfied
    by a journal that never records anything."""
    from bcs import order_journal
    gate.answer = True
    _run_tp(monkeypatch)
    recs = order_journal.read_day()
    assert [r for r in recs if r.get('kind') == 'intent'], \
        'the cleared exit left no intent either -- the journal is dead'
    assert order_journal.unresolved() == [], \
        'an intent with no result: an order may be live with nothing recording it'


def test_a_dead_vet_layer_still_closes_the_position_end_to_end(monkeypatch,
                                                               gate):
    """The inverse test, driven through the real loop rather than the unit.

    `feedback_guards_need_the_inverse_review`: every guard on this path was
    designed against a FALSE fire and none against blocking a REAL exit, which
    looks perfectly healthy in every log. A vetting layer that is down must
    cost nothing but a log line.
    """
    gate.boom = RuntimeError('vet subsystem is down')
    _c, kite, _s, _spy = _run_tp(monkeypatch)
    assert kite.placed, (
        'the stop did not fire with the vetting layer dead -- an additive '
        'layer became load-bearing')

"""M14 steps 7 and 8 - clearing an incident by hand, and seeing that it happened.

The sweep stops at `escalated` on purpose. Clearance is explicit and human, and
until these verbs existed there was no way to perform it: an unpriced refusal
left a record frozen precisely because this system could not price it, and no
automated path ever will.

Both verbs print the LIVE broker book first and REFUSE when reality contradicts
the verb. That is the point of them. The operator is about to assert something
about the world - "it is flat", "it is intact" - and the whole reason the record
is frozen is that it cannot be trusted to say which.

Step 8 is the other half: the sweep emits structured `EVENT name k=v` lines, and
the digest parses them BY NAME. "223 degraded events" tells a reader to stop
reading; "1 recovery exhausted" tells them what to do.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m14_clearance_and_reporting.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                               # noqa: E402
from bcs.tests.fakes import FakeBroker, MemoryStore, TelegramSpy   # noqa: E402
from bcs.tests.test_d2_partial_close_residue import (              # noqa: E402
    B_LONG, B_QTY, B_SHORT, BCS_BOOKS)
from zebra import engine_log as el                                 # noqa: E402


def _frozen(**over):
    t = {'id': 7, 'stock': 'TESTCO', 'status': 'partial_close',
         'long_symbol': B_LONG, 'short_symbol': B_SHORT, 'quantity': B_QTY,
         'exchange': 'NFO', 'net_debit': 13.55, 'spread_width': 50,
         'spot_symbol': 'NSE:TESTCO',
         'close_failure': {'frozen_at': '2026-08-28T10:00:00',
                           'cause': 'unfilled', 'leg': 'long',
                           'reason': 'SL_SPREAD', 'state': 'escalated',
                           'attempts': 3, 'next_attempt_after': None,
                           'recovery_fills': {}}}
    t.update(over)
    return t


@pytest.fixture(autouse=True)
def _spy(monkeypatch):
    return TelegramSpy().install(monkeypatch, sm)


@pytest.fixture
def wired(monkeypatch):
    """`--book-frozen bcs:7` and friends resolved to a fake store."""
    store = MemoryStore(trades=[_frozen()])
    monkeypatch.setattr(sm, '_frozen_book',
                        lambda name: ('BCS', store)
                        if name == 'bcs' else (None, None))
    return store


def _kite(positions):
    return FakeBroker(books=BCS_BOOKS, positions=positions, reduce_only=True)


# ══ the reference parser refuses to guess ═══════════════════════════════════

@pytest.mark.parametrize('ref', ['', None, '7', 'bcs', 'nosuch:7', 'bcs:x'])
def test_a_reference_it_cannot_resolve_is_refused_not_guessed(ref, wired,
                                                              capsys):
    """These verbs write to the money book. Guessing which store the operator
    meant is not a convenience worth having."""
    assert sm._parse_frozen_ref(ref) is None


def test_a_good_reference_resolves(wired):
    label, store, tid = sm._parse_frozen_ref('bcs:7')
    assert (label, tid) == ('BCS', 7) and store is wired


# ══ --book-frozen ═══════════════════════════════════════════════════════════

def test_a_flat_book_is_booked_at_the_operators_prices(wired, capsys):
    rc = sm.book_frozen(_kite([]), 'bcs:7', short_price=10.0, long_price=40.0)
    assert rc == 0
    exit_data = wired.called('update_trade_exit')[0][1][1]
    assert exit_data['short_fill'] == 10.0 and exit_data['long_fill'] == 40.0
    assert exit_data['exit_spread'] == pytest.approx(30.0)
    assert exit_data['total_pnl'] == pytest.approx((30.0 - 13.55) * B_QTY)
    assert wired.trades[0]['close_failure']['state'] == 'resolved'


def test_it_REFUSES_while_a_leg_is_still_live(wired, capsys):
    """Booking a position that is not flat writes an exit for quantity still
    at risk - the D2/D3 lie, typed in by hand instead of computed."""
    rc = sm.book_frozen(_kite([{'tradingsymbol': B_LONG, 'quantity': B_QTY}]),
                        'bcs:7', short_price=10.0, long_price=40.0)
    assert rc == 1
    assert not wired.called('update_trade_exit')
    assert 'REFUSING' in capsys.readouterr().out


def test_it_REFUSES_when_the_broker_cannot_be_read(wired, capsys):
    """Booking against an unknown book is guessing."""
    kite = _kite([])
    kite.positions_raises = Exception('Too many requests')
    rc = sm.book_frozen(kite, 'bcs:7', short_price=10.0, long_price=40.0)
    assert rc == 1 and not wired.called('update_trade_exit')


def test_it_REFUSES_to_invent_a_price(wired, capsys):
    """No prices given, nothing booked. The record is frozen precisely because
    this system could not price it."""
    rc = sm.book_frozen(_kite([]), 'bcs:7')
    assert rc == 1
    assert not wired.called('update_trade_exit')
    assert 'will not invent one' in capsys.readouterr().out


def test_a_leg_the_operator_did_not_price_is_UNKNOWN_not_zero(wired):
    """`bound_bcs_exit` reads the None and marks the whole figure approximate,
    which is exactly right for a half-hand-booked exit. Zero would be a price
    nobody transacted at."""
    sm.book_frozen(_kite([]), 'bcs:7', long_price=40.0)
    exit_data = wired.called('update_trade_exit')[0][1][1]
    assert exit_data['long_fill'] == 40.0
    assert exit_data['short_fill'] is None


def test_an_id_that_is_not_frozen_is_refused(wired, capsys):
    wired.trades[0]['status'] = 'open'
    assert sm.book_frozen(_kite([]), 'bcs:7', short_price=1.0) == 1
    assert 'not frozen' in capsys.readouterr().out


# ══ --reopen-frozen ═════════════════════════════════════════════════════════

def test_an_intact_spread_goes_back_under_the_monitor(wired, capsys):
    rc = sm.reopen_frozen(
        _kite([{'tradingsymbol': B_SHORT, 'quantity': -B_QTY},
               {'tradingsymbol': B_LONG, 'quantity': B_QTY}]), 'bcs:7')
    assert rc == 0
    assert wired.trades[0]['status'] == 'open'
    assert wired.trades[0]['close_failure'] is None


def test_a_HALF_closed_spread_is_refused(wired, capsys):
    """Re-monitoring it would price a position that is not there."""
    rc = sm.reopen_frozen(_kite([{'tradingsymbol': B_LONG,
                                  'quantity': B_QTY}]), 'bcs:7')
    assert rc == 1
    assert wired.trades[0]['status'] == 'partial_close'
    assert 'REFUSING' in capsys.readouterr().out


def test_a_FLIPPED_spread_is_refused(wired, capsys):
    """A leg live the wrong way must never go back into the open book."""
    rc = sm.reopen_frozen(
        _kite([{'tradingsymbol': B_SHORT, 'quantity': +B_QTY},
               {'tradingsymbol': B_LONG, 'quantity': B_QTY}]), 'bcs:7')
    assert rc == 1 and wired.trades[0]['status'] == 'partial_close'


def test_a_flat_book_is_refused_by_reopen(wired):
    """That is `--book-frozen`'s job, and saying so beats reopening a trade
    with no legs into the monitored book."""
    assert sm.reopen_frozen(_kite([]), 'bcs:7') == 1


def test_the_cohort_reopens_to_ENTERED_not_OPEN(monkeypatch):
    """zebra's open state has a different name. Reopening to 'open' would put
    the record in a status its own store does not recognise."""
    store = MemoryStore(trades=[_frozen()])
    monkeypatch.setattr(sm, '_frozen_book',
                        lambda n: ('COHORT', store) if n == 'cohort'
                        else (None, None))
    sm.reopen_frozen(_kite([{'tradingsymbol': B_SHORT, 'quantity': -B_QTY},
                            {'tradingsymbol': B_LONG, 'quantity': B_QTY}]),
                     'cohort:7')
    assert store.trades[0]['status'] == 'entered'


# ══ --frozen listing ════════════════════════════════════════════════════════

def test_the_listing_names_every_frozen_record(wired, capsys):
    sm.list_frozen_trades()
    out = capsys.readouterr().out
    assert 'TESTCO' in out and 'unfilled' in out and 'escalated' in out
    assert '3/3' in out, 'the listing must show attempts spent'


def test_the_listing_says_so_when_there_is_nothing(monkeypatch, capsys):
    monkeypatch.setattr(sm, '_frozen_book',
                        lambda n: ('BCS', MemoryStore(trades=[])))
    sm.list_frozen_trades()
    assert 'No frozen trades' in capsys.readouterr().out


def test_the_cli_exposes_all_three_verbs():
    import inspect
    src = inspect.getsource(sm.main)
    for flag in ('--frozen', '--book-frozen', '--reopen-frozen',
                 '--short-price', '--long-price'):
        assert flag in src, flag
    assert src.index('args.book_frozen') < src.index('if args.list'), (
        'the writing verbs must be dispatched before the read-only listing')


# ══ step 8 · the EVENT grammar ══════════════════════════════════════════════

EVENT_ROWS = [
    ('10:00', '[10:00] EVENT frozen_seen cls=bounded id=1 store=BCS'),
    ('10:00', '[10:00] EVENT recovery_attempt cls=bounded id=1 n=1/3 store=BCS'),
    ('10:05', '[10:05] EVENT recovery_resolved attempts=1 id=1 store=BCS via=orders'),
    ('10:06', '[10:06] EVENT recovery_exhausted cause=unfilled id=7 store=COHORT'),
    ('10:07', '[10:07] ordinary prose, not an event'),
]


def test_structured_events_are_parsed_by_name_with_their_fields():
    got = el.parse_events(EVENT_ROWS)
    assert [n for n, _ in got] == ['frozen_seen', 'recovery_attempt',
                                   'recovery_resolved', 'recovery_exhausted']
    assert dict(got)['recovery_attempt']['n'] == '1/3'


def test_prose_is_not_mistaken_for_an_event():
    assert el.parse_events([('10:00', 'EVENTUALLY the close worked')]) == []


def test_the_summary_counts_by_name_and_tracks_each_incident():
    rec = el.recovery_summary(EVENT_ROWS)
    assert rec['counts']['recovery_attempt'] == 1
    assert rec['trades']['BCS#1'] == ['frozen_seen', 'recovery_attempt',
                                      'recovery_resolved']


def test_only_what_needs_a_human_becomes_a_flag():
    """A recovery that RESOLVED is good news. Flagging it would put good news
    in the list of things to read first, which is how the list stops working."""
    flags = el.recovery_flags(el.recovery_summary(EVENT_ROWS))
    assert any('EXHAUSTED' in f for f in flags)
    assert not any('resolved' in f.lower() for f in flags)


def test_the_paper_skip_is_reported_because_its_silence_would_be_ambiguous():
    """Not a problem - but it is the line that PROVES the paper guard ran, and
    its absence would be indistinguishable from the guard being gone."""
    rec = el.recovery_summary(
        [('10:00', '[10:00] EVENT frozen_paper_skipped id=9 store=COHORT')])
    assert any('paper record' in f for f in el.recovery_flags(rec))


def test_a_quiet_day_renders_no_section_at_all():
    """A heading that says 0 every day is a heading people stop reading."""
    assert el.render_recovery(el.recovery_summary([])) == []
    assert el.recovery_flags({}) == []


def test_the_section_marks_the_rows_that_need_a_human():
    lines = el.render_recovery(el.recovery_summary(EVENT_ROWS))
    exhausted = [l for l in lines if 'recovery_exhausted' in l][0]
    resolved = [l for l in lines if 'recovery_resolved' in l][0]
    assert '⚠' in exhausted and '⚠' not in resolved


def test_recovery_flags_come_before_the_catalogues_own():
    """A frozen position with dead stops outranks every
    degraded-and-still-recovering count."""
    a = {'problems': [], 'events': [], 'unwatched': [], 'stalls': [],
         'uncatalogued': [], 'uncatalogued_total': 0,
         'recovery': el.recovery_summary(EVENT_ROWS)}
    assert any('EXHAUSTED' in f for f in el.flags(a))


def test_the_retries_exhausted_pattern_survived_the_attempts_change():
    """M14 gave `close_leg` a caller-supplied ceiling, so the log now says
    "attempt(s)". The catalogue's `probe` still matched the changed wording -
    a probe pins the PHRASE, only the pattern pins the MATCH - so nothing
    caught this but a direct test."""
    import re
    ev = next(e for e in el.CATALOGUE if e.name == 'close_retries_exhausted')
    for line in ('    FAILED to close TESTCO26SEP1390CE after 3 attempts!',
                 '    FAILED to close TESTCO26SEP1390CE after 1 attempt(s)!'):
        assert re.search(ev.pattern, line), line

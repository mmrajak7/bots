"""Replay of 2026-07-24: the FALSE SL_SPREAD that cost Rs 7,297.

The second real-money loss, and a different shape from February. Nothing was
inverted and nothing was missing — the 86CE simply quoted bid 0.28 / ask 1.40,
a 1.12 width against a 0.84 mid, and the structure valued at 0.36 against a
1.41 debit. The 50%-of-debit stop fired, the position was closed, and the spot
thesis had not failed at all: NHPC was at 80.65 against an entry of 80.70, a
move of 0.06%.

Why it needs a REPLAY and not just the unit tests it already has
----------------------------------------------------------------
`test_exit_guards.py` proves that book yields no tradeable value, and that
corroboration catches the harder variant. Neither drives the loop. The four
checks that were supposed to stop this — the reliability gate, the intrinsic
floor, the debounce and the blind alert — all read ONE source, the option book,
and all four agreed with each other because the book was the thing that was
wrong. `spot_corroborates` is the independent witness, it was written for
exactly this, and until B8 it was never wired into `monitor_all` at all.

So the question this file asks is the one that was never asked before the
money went: with everything assembled, does the loop refuse it?

The 09:18 detail matters. `MARKET_OPEN_BUFFER_SEC` is 180s, so the spot buffer
had already lapsed — 09:18 is deliberately AFTER the buffer that would have
made this trivial. The spread-trigger buffer (900s) had not, which is why the
replay also runs the same book in the afternoon, where no open buffer helps and
only the book-level guards and corroboration remain.

Run:  cd Helper && python -m pytest bcs/tests/test_replay_jul2026_nhpc.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                     # noqa: E402
from bcs.tests.replay import Tick, run_session           # noqa: E402

DAY = date(2026, 7, 24)
LONG, SHORT = 'NHPC26AUG80CE', 'NHPC26AUG86CE'
QTY = 6900

TRADE = {
    'id': 1, 'status': 'open', 'stock': 'NHPC', 'version': 1,
    'long_symbol': LONG, 'short_symbol': SHORT,
    'spot_symbol': 'NSE:NHPC', 'exchange': 'NFO',
    'quantity': QTY, 'lot_size': QTY, 'lots': 1,
    'entry_long_price': 2.05, 'entry_short_price': 0.64,
    'net_debit': 1.41, 'spread_width': 6,
    'entry_spot': 80.70, 'sl_spot': 78.28, 'sl_spread': 0.71,
    'target_spot': 86.0, 'expiry': '2026-08-27',
}

#: The real 09:18:24 book. The 86CE's 0.28/1.40 is the whole incident: not
#: absent, not inverted, just 133% of its own mid.
HEALTHY_LONG = {'bid': 1.68, 'bid_qty': 1000, 'ask': 1.75, 'ask_qty': 1000,
                'ltp': 1.70, 'prev_close': 2.00}
JUNK_SHORT = {'bid': 0.28, 'bid_qty': 1000, 'ask': 1.40, 'ask_qty': 1000,
              'ltp': 0.30, 'prev_close': 0.60}
#: Same collapse, but a TIDY book. Reliability, floor, debounce and re-verify
#: all read the option book and would every one of them wave this through.
#: Only spot says otherwise.
TIDY_SHORT = {'bid': 1.30, 'bid_qty': 1000, 'ask': 1.34, 'ask_qty': 1000,
              'ltp': 1.32, 'prev_close': 0.60}
TIDY_LONG = {'bid': 1.66, 'bid_qty': 1000, 'ask': 1.70, 'ask_qty': 1000,
             'ltp': 1.68, 'prev_close': 2.00}

OPEN_POSITIONS = [{'tradingsymbol': SHORT, 'quantity': -QTY},
                  {'tradingsymbol': LONG, 'quantity': QTY}]


def _steady(at_list, spot, long, short, note=''):
    return [Tick(a, spot, long, short, note) for a in at_list]


# ── The morning, as it happened ──────────────────────────────────────────────

MORNING = (
    _steady(['09:16:00', '09:17:00'], 80.70, HEALTHY_LONG,
            {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.64, 'ask_qty': 1000,
             'ltp': 0.62, 'prev_close': 0.60}, 'healthy: spread ~1.04')
    + _steady(['09:18:24', '09:19:00', '09:20:00', '09:21:00', '09:22:00'],
              80.65, HEALTHY_LONG, JUNK_SHORT,
              'the junk book: 0.28/1.40, value 0.36 vs debit 1.41')
    + _steady(['09:25:00', '09:30:00'], 80.75, HEALTHY_LONG,
              {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.65, 'ask_qty': 1000,
               'ltp': 0.62, 'prev_close': 0.60}, 'recovered')
)


@pytest.fixture
def morning(monkeypatch):
    return run_session(monkeypatch, sm, TRADE, MORNING, DAY,
                       positions=[dict(p) for p in OPEN_POSITIONS])


def test_the_junk_book_closes_nothing(morning):
    clock, kite, store, spy = morning
    assert kite.placed == [], (
        "the loop closed on the 0.28/1.40 book — that is the Rs 7,297")
    assert not store.called('begin_close')
    assert store.trades[0]['status'] == 'open'


def test_the_position_survives_to_the_recovered_book(morning):
    clock, kite, store, spy = morning
    assert kite.net_qty(SHORT) == -QTY and kite.net_qty(LONG) == QTY
    assert len(kite.seen) == len(MORNING), (
        f"replay stopped after {len(kite.seen)} of {len(MORNING)} ticks")


def test_a_four_minute_blind_spell_stays_quiet(morning):
    """Deliberately NOT an alert.

    My first draft asserted the opposite and failed, which was the test being
    wrong rather than the code: SPREAD_BLIND_ALERT_SEC is 15 minutes, and the
    real junk window was about four. A monitor that Telegrammed every transient
    unreadable book would train the reader to ignore it -- which is exactly how
    the OI warning on COCHINSHIP got waved through.
    """
    clock, kite, store, spy = morning
    assert sm.SPREAD_BLIND_ALERT_SEC == 15 * 60
    assert spy.sent == [], f"alerted on a short blind spell: {spy.sent}"


def test_a_blind_spell_past_the_threshold_does_alert(monkeypatch):
    """The other half, and the one that matters: refusing to value a book is
    only safe if somebody is eventually told. A silent refusal and a working
    monitor look identical from outside.
    """
    long_blind = (
        _steady(['13:00:00'], 80.70, HEALTHY_LONG,
                {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.64, 'ask_qty': 1000,
                 'ltp': 0.62, 'prev_close': 0.60}, 'healthy')
        + _steady(['13:01:00', '13:10:00', '13:20:00', '13:25:00'],
                  80.65, HEALTHY_LONG, JUNK_SHORT,
                  'unreadable for 24 minutes, past the 15-minute threshold')
    )
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, long_blind, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    assert kite.placed == [], "a long blind spell must still not TRADE"
    assert spy.sent, (
        "value-blind for 24 minutes with no Telegram: from outside that is "
        "indistinguishable from a monitor that is working")
    assert spy.any('NHPC')


# ── The afternoon: no open buffer to hide behind ─────────────────────────────

AFTERNOON = (
    _steady(['13:00:00', '13:01:00'], 80.70, HEALTHY_LONG,
            {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.64, 'ask_qty': 1000,
             'ltp': 0.62, 'prev_close': 0.60}, 'healthy')
    + _steady(['13:02:00', '13:03:00', '13:04:00', '13:05:00'],
              80.65, HEALTHY_LONG, JUNK_SHORT, 'the junk book, mid-session')
    + _steady(['13:08:00'], 80.75, HEALTHY_LONG,
              {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.65, 'ask_qty': 1000,
               'ltp': 0.62, 'prev_close': 0.60}, 'recovered')
)


def test_the_same_book_is_refused_with_every_open_buffer_lapsed(monkeypatch):
    """09:18 is inside the 900s spread buffer, so the morning replay could pass
    on the clock alone. At 13:02 nothing is left but the book-level guards."""
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, AFTERNOON, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    assert kite.placed == [], "the wide book closed the trade mid-session"
    assert store.trades[0]['status'] == 'open'


# ── The harder shape: a TIDY book that collapses with spot flat ──────────────

TIDY = (
    _steady(['13:00:00', '13:01:00'], 80.70, HEALTHY_LONG,
            {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.64, 'ask_qty': 1000,
             'ltp': 0.62, 'prev_close': 0.60}, 'healthy, reference set')
    + _steady(['13:02:00', '13:03:00', '13:04:00', '13:05:00', '13:06:00'],
              80.65, TIDY_LONG, TIDY_SHORT,
              'tidy book, value collapses, spot moves 0.06%')
    + _steady(['13:10:00'], 80.75, HEALTHY_LONG,
              {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.65, 'ask_qty': 1000,
               'ltp': 0.62, 'prev_close': 0.60}, 'recovered')
)


def test_a_tidy_book_collapsing_on_flat_spot_is_still_refused(monkeypatch):
    """This is the one the option book cannot catch.

    Reliability passes (the book is tight), the floor passes (the spread is
    OTM), debounce passes (it persists), re-verify passes (it re-reads the same
    book). Four checks, one source, unanimous and wrong. Spot is the second
    source, and B8 is what connected it.

    MEASURED, 2026-08-24: disabling `spot_corroborates` ALONE makes this replay
    close the position. Not "one of several layers" — on this shape it is the
    only one. The wide-book replay above needs THREE guards removed before it
    closes; this needs one. That is the whole argument for B8 having been
    ranked CRITICAL despite a guard already existing for the wide-book case.
    """
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, TIDY, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    assert kite.placed == [], (
        "a 0.06% spot move closed the position; that is the NHPC signature and "
        "no real repricing of a vertical produces it")
    assert store.trades[0]['status'] == 'open'


def test_the_corroboration_veto_is_recorded_even_when_it_does_not_alert(monkeypatch):
    """The veto deliberately does not Telegram on its own -- it hands the
    position to the blindness tracker, which owns escalation and its 15-minute
    clock. But a veto that leaves no trace at all would make "why did TP not
    fire at 13:04" unanswerable, and that question has been asked twice.
    """
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, TIDY, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    src = (HELPER / 'bcs' / 'spread_monitor.py').read_text(encoding='utf-8')
    assert 'QUOTE GUARD' in src, (
        "the veto must name itself in the POLL log; the exit book is the only "
        "evidence that survives the cron process")
    assert kite.placed == []


# ── Negative control: spot ACTUALLY fails, and the stop must work ────────────

REAL_BREAK = (
    _steady(['13:00:00', '13:01:00'], 80.70, HEALTHY_LONG,
            {'bid': 0.60, 'bid_qty': 1000, 'ask': 0.64, 'ask_qty': 1000,
             'ltp': 0.62, 'prev_close': 0.60}, 'healthy, reference set')
    + _steady(['13:02:00', '13:03:00', '13:04:00', '13:05:00', '13:06:00',
               '13:07:00', '13:08:00'],
              75.10,
              {'bid': 0.32, 'bid_qty': 1000, 'ask': 0.36, 'ask_qty': 1000,
               'ltp': 0.34, 'prev_close': 2.00},
              {'bid': 0.03, 'bid_qty': 1000, 'ask': 0.05, 'ask_qty': 1000,
               'ltp': 0.04, 'prev_close': 0.60},
              'spot -6.9%: the thesis is dead and the book agrees')
)


def test_a_real_collapse_that_spot_corroborates_does_close(monkeypatch):
    """Without this, a monitor that never closes anything would pass every
    test above. Same trade, same harness, tidy two-sided book — but spot fell
    6.9%, so the value collapse is explained and the stop must work.
    """
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, REAL_BREAK, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    assert kite.placed, (
        "nothing closed on a 6.9% adverse move with a clean book — the guards "
        "are refusing everything, not refusing garbage")
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert store.called('update_trade_exit')


def test_the_short_leg_is_bought_back_before_the_long_is_sold(monkeypatch):
    """Reverse that order and the book is briefly a naked short, which is the
    margin spike the whole close sequence is arranged to avoid."""
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, REAL_BREAK, DAY,
        positions=[dict(p) for p in OPEN_POSITIONS])

    order = [o['tradingsymbol'] for o in kite.placed]
    assert order.index(SHORT) < order.index(LONG), (
        f"legs closed in the wrong order: {order}")
    assert kite.placed[order.index(SHORT)]['transaction_type'] == 'BUY'

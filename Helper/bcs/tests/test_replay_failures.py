"""Replays of the ways a session ENDS badly, other than a bad quote.

The two incident replays cover garbage books. These cover the rest of tier 2:
the token dying mid-session, a symbol that stops resolving, expiry day, and the
happy path. All four arrive with positions already open and the loop already
running, which is the only interesting version — a broker that fails on its
first call is a much easier problem than one that fails at 11:40.

The through-line is B9's distinction. An auth death is FLEET-WIDE: nothing will
work again this session, and every open position is unmonitored, so it must
stop the loop and shout. Anything else is LOCAL: one symbol, one trade, and
isolating it keeps the other positions watched. Collapsing the two is what made
a renamed symbol able to kill monitoring for an entire book.

Run:  cd Helper && python -m pytest bcs/tests/test_replay_failures.py -v
"""
import sys
from datetime import date
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                     # noqa: E402
from bcs.tests.replay import Tick, run_session           # noqa: E402

DAY = date(2026, 9, 15)
LONG, SHORT = 'TESTCO26SEP1340CE', 'TESTCO26SEP1390CE'
QTY = 700

TRADE = {
    'id': 1, 'status': 'open', 'stock': 'TESTCO', 'version': 1,
    'long_symbol': LONG, 'short_symbol': SHORT, 'spot_symbol': 'NSE:TESTCO',
    'exchange': 'NFO', 'quantity': QTY, 'lot_size': QTY, 'lots': 1,
    'entry_long_price': 21.20, 'entry_short_price': 7.65, 'net_debit': 13.55,
    'spread_width': 50, 'target_spot': 1435.0, 'sl_spot': 1319.0,
    'sl_spread': 6.78, 'entry_spot': 1360.0, 'expiry': '2026-09-29',
}

HEALTHY_LONG = {'bid': 40.00, 'bid_qty': 1400, 'ask': 40.20, 'ask_qty': 1400,
                'ltp': 40.10, 'prev_close': 21.0}
HEALTHY_SHORT = {'bid': 10.05, 'bid_qty': 1400, 'ask': 10.30, 'ask_qty': 1400,
                 'ltp': 10.20, 'prev_close': 7.6}

POSITIONS = [{'tradingsymbol': SHORT, 'quantity': -QTY},
             {'tradingsymbol': LONG, 'quantity': QTY}]


def _calm(times, spot=1400.0, long=None, short=None, note=''):
    return [Tick(t, spot, long or HEALTHY_LONG, short or HEALTHY_SHORT, note)
            for t in times]


CALM = _calm(['11:00:00', '11:10:00', '11:20:00', '11:30:00',
              '11:40:00', '11:50:00', '12:00:00'], note='nothing happening')


# ── The token dies at 11:25 ──────────────────────────────────────────────────

AUTH_DEATH = Exception('Incorrect `api_key` or `access_token`.')


@pytest.fixture
def token_died(monkeypatch):
    return run_session(monkeypatch, sm, TRADE, CALM, DAY,
                       positions=[dict(p) for p in POSITIONS],
                       faults=[('ltp', '11:25:00', AUTH_DEATH),
                               ('quote', '11:25:00', AUTH_DEATH)])


def test_a_dead_token_is_shouted_about(token_died):
    """Fleet-wide: nothing works again this session, and every open position
    is now unwatched. Silence here is the worst outcome in the file."""
    clock, kite, store, spy = token_died
    assert spy.sent, "the token died and the monitor said nothing"
    assert spy.containing('MONITOR') or spy.containing('UNMONITORED') \
        or spy.containing('token'), \
        f"the alert does not say monitoring stopped: {spy.sent}"


def test_a_dead_token_places_no_orders(token_died):
    clock, kite, store, spy = token_died
    assert kite.placed == [], (
        "orders were placed while the broker was rejecting every read")
    assert store.trades[0]['status'] == 'open'


def test_a_dead_token_is_classified_as_auth_not_as_a_bad_symbol():
    """The classification IS the fix. Both branches exist; picking the wrong
    one turns a fleet-wide outage into a per-trade warning nobody reads."""
    assert sm._is_auth_error(AUTH_DEATH) is True
    assert sm._is_auth_error(KeyError('NSE:RENAMEDCO')) is False


# ── A symbol stops resolving at 11:25 ────────────────────────────────────────

@pytest.fixture
def renamed(monkeypatch):
    return run_session(monkeypatch, sm, TRADE, CALM, DAY,
                       positions=[dict(p) for p in POSITIONS],
                       faults=[('ltp', '11:25:00', KeyError('NSE:TESTCO'))])


def test_a_renamed_symbol_does_not_trade(renamed):
    clock, kite, store, spy = renamed
    assert kite.placed == []
    assert store.trades[0]['status'] == 'open'


def test_a_renamed_symbol_alerts_faster_than_a_blind_book(renamed):
    """5 minutes, not 15. A spot-blind record has NO live trigger at all —
    SL_SPOT, SL_SPREAD and TP every one of them read spot — whereas a
    spread-blind one still has SL_SPOT."""
    clock, kite, store, spy = renamed
    assert sm.SPOT_BLIND_ALERT_SEC == 5 * 60
    assert sm.SPOT_BLIND_ALERT_SEC < sm.SPREAD_BLIND_ALERT_SEC
    assert spy.sent, "spot was unreadable for 35 minutes with no Telegram"


def test_the_session_runs_to_the_close_rather_than_dying(renamed):
    """A local fault must not end the loop. Two other books share it."""
    clock, kite, store, spy = renamed
    assert clock.dt.hour == 15 and clock.dt.minute >= 30, (
        "one unresolvable symbol ended the whole monitoring session")


# ── Expiry day ───────────────────────────────────────────────────────────────

def test_expiry_day_is_announced_at_startup(monkeypatch):
    """Stock options are physically settled; an ITM leg left open on expiry is
    a delivery obligation, not a P&L question."""
    expiring = dict(TRADE, expiry=DAY.isoformat())
    clock, kite, store, spy = run_session(
        monkeypatch, sm, expiring, _calm(['10:00:00', '10:05:00']), DAY,
        positions=[dict(p) for p in POSITIONS])
    assert spy.any('EXPIRY DAY'), f"no expiry warning: {spy.sent}"


def test_expiry_day_force_closes_before_the_cutoff(monkeypatch):
    """The ONE automated close this file does on expiry proximity."""
    expiring = dict(TRADE, expiry=DAY.isoformat())
    late = _calm(['15:00:00', '15:05:00', '15:10:00', '15:15:00', '15:20:00'],
                 note='expiry afternoon')
    clock, kite, store, spy = run_session(
        monkeypatch, sm, expiring, late, DAY,
        positions=[dict(p) for p in POSITIONS])

    assert kite.placed, "an expiring position was carried into settlement"
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert store.called('update_trade_exit')


def test_a_non_expiry_day_does_not_force_close(monkeypatch):
    """Negative control for both tests above."""
    late = _calm(['15:00:00', '15:10:00', '15:20:00'])
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, late, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed == []
    assert not spy.any('EXPIRY DAY')


# ── The happy path ───────────────────────────────────────────────────────────

def test_spot_reaching_target_closes_the_spread(monkeypatch):
    """TP is spot-based and deliberately NOT gated by the open buffer — but it
    still has to actually fire, and nothing else in this package drives it end
    to end."""
    itm_long = {'bid': 96.0, 'bid_qty': 1400, 'ask': 96.4, 'ask_qty': 1400,
                'ltp': 96.2, 'prev_close': 21.0}
    itm_short = {'bid': 47.0, 'bid_qty': 1400, 'ask': 47.4, 'ask_qty': 1400,
                 'ltp': 47.2, 'prev_close': 7.6}
    won = (_calm(['11:00:00'], spot=1400.0)
           + _calm(['11:05:00', '11:10:00', '11:15:00'], spot=1436.0,
                   long=itm_long, short=itm_short, note='TP: 1436 >= 1435'))

    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, won, DAY,
        positions=[dict(p) for p in POSITIONS])

    assert kite.placed, "spot passed the target and nothing closed"
    assert kite.net_qty(SHORT) == 0 and kite.net_qty(LONG) == 0
    assert store.called('update_trade_exit')
    order = [o['tradingsymbol'] for o in kite.placed]
    assert order.index(SHORT) < order.index(LONG), (
        f"the short leg must be bought back first, got {order}")


def test_spot_short_of_target_leaves_it_alone(monkeypatch):
    """Negative control: 1434.9 against a 1435.0 target."""
    near = _calm(['11:00:00', '11:05:00', '11:10:00'], spot=1434.9)
    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, near, DAY,
        positions=[dict(p) for p in POSITIONS])
    assert kite.placed == [], "closed 0.1 short of the target"


# ── Dry run ──────────────────────────────────────────────────────────────────

def test_dry_run_reaches_the_same_decision_and_places_nothing(monkeypatch):
    """The plan's pre-arm check is a --dry-run cron against the live book, so
    dry-run has to make the SAME decisions — otherwise the rehearsal proves
    nothing about the performance."""
    itm_long = {'bid': 96.0, 'bid_qty': 1400, 'ask': 96.4, 'ask_qty': 1400,
                'ltp': 96.2, 'prev_close': 21.0}
    itm_short = {'bid': 47.0, 'bid_qty': 1400, 'ask': 47.4, 'ask_qty': 1400,
                 'ltp': 47.2, 'prev_close': 7.6}
    won = (_calm(['11:00:00'], spot=1400.0)
           + _calm(['11:05:00', '11:10:00'], spot=1436.0,
                   long=itm_long, short=itm_short))

    clock, kite, store, spy = run_session(
        monkeypatch, sm, TRADE, won, DAY,
        positions=[dict(p) for p in POSITIONS], dry_run=True)

    assert kite.placed == [], "a dry run reached the broker"
    assert spy.any('TP') or spy.any('TESTCO'), (
        "a dry run that decides nothing rehearses nothing")

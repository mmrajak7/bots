"""One engine per trade (P1.5).

Both engines can reach a cohort position now. `zebra/monitor.py` has watched
it since 2026-08-14 and books paper exits; `bcs/spread_monitor.py` can place
real orders and, since the bridge, can see it. Two closers on one position is
not a tidiness problem:

* zebra booking a paper exit on a position the order path holds for real
  removes the record from `get_entered()`, so `ZebraStoreAdapter.get_open_trades`
  stops returning it and a LIVE position goes unwatched — with nothing in
  either log reporting a fault. That is the failure shape that has actually
  cost money here;
* both engines raising vet requests against ONE shared marker per (trade,
  kind) double-increments its defer count and escalates to the human twice as
  fast.

`exits_managed_externally` is the switch, and it is thrown in the SAME step
`--dry-run` comes off the monitor's crontab line. The tests below pin both
directions: what stands down, and — the one that matters more — what does not.

Run:  cd Helper && python -m pytest zebra/tests/test_exits_external.py -v
"""
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra.trade_store import ZebraStore   # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    return s


def _cohortise(store, yes=True):
    with store._mutate():
        store.find(1)['cohort'] = cfg.COHORT_START if yes else '1999-01-01'


# ── the predicate ───────────────────────────────────────────────────────────

def test_the_switch_is_off_by_default():
    """It must stay off until the monitor is armed. Turning it on alone leaves
    those positions with NO exit engine at all, and nothing in either log looks
    wrong — which is the direction that hides rather than shouts."""
    assert cfg._DEFAULTS['exits_managed_externally'] is False


def test_the_switch_is_read_strictly(monkeypatch):
    """`"exits_managed_externally": 0` must not be able to decide which process
    closes real positions. Same rule as PAPER_MODE."""
    monkeypatch.setitem(cfg._runtime, 'exits_managed_externally', 0)
    assert cfg._strict_bool('exits_managed_externally') is False
    monkeypatch.setitem(cfg._runtime, 'exits_managed_externally', 'true')
    assert cfg._strict_bool('exits_managed_externally') is False
    monkeypatch.setitem(cfg._runtime, 'exits_managed_externally', True)
    assert cfg._strict_bool('exits_managed_externally') is True


def test_the_constant_itself_is_built_by_strict_bool():
    """Asserted on the SOURCE, because no input distinguishes the two readings.

    `_strict_bool` is well covered, but the test above calls it directly, so
    swapping the module-level assignment to a plain `bool(_runtime.get(...))`
    changed nothing anywhere and the mutation survived. The constant is
    evaluated once at import from a file this process does not control, so the
    only place to pin the reading is where it is written.
    """
    import inspect
    src = inspect.getsource(cfg)
    assert ("EXITS_MANAGED_EXTERNALLY = _strict_bool('exits_managed_externally')"
            in src), (
        'the switch deciding which process closes real positions is no longer '
        "read strictly -- `\"exits_managed_externally\": 0` would now arm it")


def test_both_conditions_are_required(store, monkeypatch):
    """The switch AND the cohort. The monitor only ever loads cohort records,
    so the other 450 rows in this book have no other engine watching them."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    _cohortise(store, yes=False)
    assert monitor._exits_external(store.find(1)) is False
    _cohortise(store, yes=True)
    assert monitor._exits_external(store.find(1)) is True
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', False)
    assert monitor._exits_external(store.find(1)) is False


# ── the backstop, inside _paper_auto_close ──────────────────────────────────

@pytest.mark.parametrize('reason', ['tp', 'trail', 'spot_sl', 'debit_sl',
                                    'time'])
def test_no_exit_the_order_path_owns_is_booked_here(store, monkeypatch,
                                                    reason):
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    _cohortise(store)
    assert monitor._paper_auto_close(store, store.find(1), 12.0, reason,
                                     spot=110.0) is None
    assert store.find(1)['status'] == 'entered', (
        'a paper exit was booked on a position the order path holds — the '
        'record leaves get_entered() and the live position goes unwatched')


@pytest.mark.parametrize('reason', ['tp', 'trail', 'spot_sl', 'debit_sl',
                                    'time'])
def test_the_same_exit_books_normally_with_the_switch_off(store, monkeypatch,
                                                          reason):
    """Negative control. Without it every test above passes just as well when
    `_paper_auto_close` is broken outright."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', False)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    _cohortise(store)
    assert monitor._paper_auto_close(store, store.find(1), 12.0, reason,
                                     spot=110.0) is not None
    assert store.find(1)['status'] == 'exited'


def test_a_non_cohort_position_is_still_closed_by_zebra(store, monkeypatch):
    """The 450 back-ratio rows have no other engine. Standing down for them
    would strand them with nothing watching at all."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    _cohortise(store, yes=False)
    assert monitor._paper_auto_close(store, store.find(1), 12.0, 'tp',
                                     spot=110.0) is not None
    assert store.find(1)['status'] == 'exited'


def test_the_terminal_expiry_settle_is_never_declined(store, monkeypatch):
    """`expiry` is not an exit rule — it is the net that books a record whose
    expiry has PASSED and whose book has died. Nothing else can ever price
    that position, and leaving it `entered` bans its stock from the scanner
    for good. It stays with zebra whatever else moves."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    _cohortise(store)
    with store._mutate():
        store.find(1)['expiry'] = '2026-08-01'
    assert monitor._settle_if_expired(
        store, store.find(1), spot=118.0,
        today=datetime(2026, 8, 12).date(), dry_run=True) is True
    assert store.find(1)['status'] == 'exited'
    assert store.find(1)['exit_reason'] == 'paper:expiry'


def test_expiry_is_absent_from_the_managed_set():
    assert 'expiry' not in monitor.EXTERNALLY_MANAGED_EXITS
    assert monitor.EXTERNALLY_MANAGED_EXITS == {
        'tp', 'trail', 'spot_sl', 'debit_sl', 'time'}


# ── the cascade stands down before it spends a vet or an alert ──────────────

def _drive(store, monkeypatch, spot=150.0):
    """One `check_entered` cycle with a healthy book and spot through TP."""
    sent, vetted = [], []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'TESTCO': spot})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 30.0, 'reliable': True, 'reason': None,
                            'legs': {'long': {'symbol': 'L', 'bid': 40.0,
                                              'ask': 40.2},
                                     'short': {'symbol': 'S', 'bid': 10.0,
                                               'ask': 10.2}},
                            'floored': False})

    def _gate(st, trade, kind, quote, sp, dry_run=False):
        vetted.append(kind)
        return True
    monkeypatch.setattr(monitor, '_exit_cleared', _gate)
    monitor.check_entered(store, kite=None, dry_run=True)
    return sent, vetted


def test_a_firing_tp_is_measured_not_acted_on(store, monkeypatch):
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    _cohortise(store)
    sent, vetted = _drive(store, monkeypatch)
    assert store.find(1)['status'] == 'entered'
    assert vetted == [], (
        'zebra raised a vet request for an exit it will not take — two '
        'engines on one shared marker double-count its defers')
    assert not any('TP' in m for m in sent), \
        'zebra alerted on an exit the order path is about to announce itself'


def test_the_same_cycle_DOES_close_with_the_switch_off(store, monkeypatch):
    """Negative control for the cascade skip: proves the TP was genuinely
    firing and the test above is not passing on a stale spot."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', False)
    _cohortise(store)
    sent, vetted = _drive(store, monkeypatch)
    assert store.find(1)['status'] == 'exited'
    assert vetted == ['tp']


def test_measurement_keeps_accruing_while_stood_down(store, monkeypatch):
    """The peak, the corroboration reference and the forensic POLL line are
    this book's research record. Measurement is not an exit decision, and the
    cohort's whole purpose right now is to accumulate evidence."""
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    _cohortise(store)
    _drive(store, monkeypatch, spot=150.0)
    t = store.find(1)
    assert t.get('mfe_spot') is not None or t.get('mfe_value') is not None, \
        'the peak stopped being recorded when the engine stood down'


def test_the_stand_down_says_so(store, monkeypatch, caplog):
    """A position nothing acts on looks exactly like a quiet one. The log line
    is the only thing that distinguishes "held correctly" from "never loaded"
    (`feedback_never_asked_is_not_failed`)."""
    import logging
    monkeypatch.setattr(cfg, 'EXITS_MANAGED_EXTERNALLY', True)
    _cohortise(store)
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        _drive(store, monkeypatch)
    assert any('EXITS EXTERNAL' in r.message for r in caplog.records), \
        'the engine stood down silently'

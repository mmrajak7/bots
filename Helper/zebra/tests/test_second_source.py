"""Spot as a VETO, the open buffer, and expiry settlement.

Why a veto and not a stop, in one paragraph, because this WILL be re-litigated
from intuition otherwise. Measured over 147 records with candle coverage:
eventual winners take a median 2.74% adverse excursion before working, because
the scanner enters on a pullback TOWARD the ST line — the adverse move IS the
thesis. A 3% spot TRIGGER therefore cuts 31 of 78 winners (Rs 8.9L given up),
including the book's biggest at +155.4% (IDFCFIRSTB, MAE 4.43%). Reaping the
winners is the power-law rule inverted. A spot VETO cannot do that: it only
ever refuses an exit the option book asked for.

Run:  cd Helper && python -m pytest zebra/tests/test_second_source.py -v
"""
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'SPOT_VETO_ENABLED', True)
    s = ZebraStore()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    return s


def _ref(store, spot, value, age_sec=0.0):
    store.apply_mfe({1: {'corrob_spot': spot, 'corrob_value': value,
                         'corrob_t': time.time() - age_sec}})


# ── the veto ─────────────────────────────────────────────────────────────

def test_a_collapse_the_underlying_cannot_explain_is_vetoed(store):
    """The NHPC signature: value falls off a cliff while spot barely moves. No
    real repricing of a vertical produces that — value is a function of the
    underlying."""
    t = store.find(1)
    _ref(store, spot=96.5, value=10.0)
    ok, why, _ = monitor._spot_corroborates(store, t, spot=96.6, value=5.0,
                                            reliable=True)
    assert ok is False
    assert 'uncorroborated collapse' in why


def test_the_same_collapse_WITH_a_spot_move_is_allowed(store):
    """The guard must not block a real move. This is the companion that stops
    the test above from passing for the wrong reason."""
    t = store.find(1)
    _ref(store, spot=96.5, value=10.0)
    ok, why, _ = monitor._spot_corroborates(store, t, spot=91.0, value=5.0,
                                            reliable=True)
    assert ok is True and why == ''


def test_the_veto_is_one_way_a_rise_is_never_vetoed(store):
    """VETO-ONLY polarity. It can refuse an exit the book asked for; it can
    never ask for one. That is what makes a second source safe to add."""
    t = store.find(1)
    _ref(store, spot=96.5, value=10.0)
    ok, _, _ = monitor._spot_corroborates(store, t, spot=96.6, value=25.0,
                                          reliable=True)
    assert ok is True


def test_with_no_reference_yet_it_cannot_prove_anything(store):
    t = store.find(1)
    ok, _, patch = monitor._spot_corroborates(store, t, spot=96.5, value=10.0,
                                              reliable=True)
    assert ok is True
    assert patch == {'corrob_spot': 96.5, 'corrob_value': 10.0,
                     'corrob_t': pytest.approx(time.time(), abs=5)}


def test_a_stale_reference_proves_nothing(store):
    """An hour-old reference cannot speak to what just happened."""
    t = store.find(1)
    _ref(store, spot=96.5, value=10.0,
         age_sec=cfg.CORROBORATION_STALE_SEC + 60)
    ok, _, _ = monitor._spot_corroborates(store, t, spot=96.6, value=5.0,
                                          reliable=True)
    assert ok is True


def test_a_garbage_read_never_becomes_the_baseline(store):
    """If an unreliable reading could advance the reference, the NEXT genuine
    move would be judged against garbage — and the guard would veto the exit
    that ought to happen. Reliability gates the WRITE, not just the read."""
    t = store.find(1)
    _, _, patch = monitor._spot_corroborates(store, t, spot=96.5, value=0.4,
                                             reliable=False)
    assert patch is None


def test_the_reference_survives_the_cron_process_exiting(store, tmp_path):
    """zebra's process EXITS between 5-minute cycles. The live monitor keeps
    this reference in memory, which a long-lived process can afford; here an
    in-memory one would reset every cycle and never veto anything."""
    t = store.find(1)
    _, _, patch = monitor._spot_corroborates(store, t, spot=96.5, value=10.0,
                                             reliable=True)
    store.apply_mfe({1: patch})
    fresh = ZebraStore()                      # a different process would do this
    fresh._load_local()                       # ...and read the store off disk
    ok, why, _ = monitor._spot_corroborates(fresh, fresh.find(1), spot=96.6,
                                            value=5.0, reliable=True)
    assert ok is False, "the reference did not survive a new process"


def test_the_veto_holds_the_value_triggers_not_the_spot_ones(store, monkeypatch):
    """Scope check. A veto must not silence TP (spot-driven, and spot is the
    thing being trusted) nor the expiry nag (a calendar fact)."""
    src = monitor.check_entered.__doc__ or ''
    import inspect
    body = inspect.getsource(monitor.check_entered)
    veto_at = body.index('SPOT VETO')
    tp_at = body.index('── TP ')
    assert veto_at < tp_at, "veto must be evaluated before the triggers"
    assert 'debit_usable = False' in body[veto_at:veto_at + 400], \
        "the veto must gate debit_usable, which is what DEBIT-SL and TRAIL key off"


# ── the open buffer ──────────────────────────────────────────────────────

def test_value_triggers_are_dark_at_the_open(monkeypatch):
    """Both incidents that cost real money happened at the open on the first
    prints of the day. The live monitor has refused to act before 09:30 ever
    since; zebra's first cycle is 09:15 with no buffer, so BOTH debounce polls
    landed inside the window the live system will not trade in."""
    at_open = datetime(2026, 8, 12, 9, 16, tzinfo=cfg.IST)
    assert monitor._value_triggers_live(at_open) is False


def test_value_triggers_wake_after_the_buffer(monkeypatch):
    assert monitor._value_triggers_live(
        datetime(2026, 8, 12, 9, 31, tzinfo=cfg.IST)) is True
    assert monitor._value_triggers_live(
        datetime(2026, 8, 12, 14, 0, tzinfo=cfg.IST)) is True


# ── expiry settlement ────────────────────────────────────────────────────

def test_a_dark_book_settles_at_expiry_instead_of_orphaning(store, monkeypatch):
    """Every other exit needs a quote, so a position whose book dies never
    reaches one — it stays `entered` forever, and scanner dedup then bans its
    stock from the pipeline permanently."""
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    with store._mutate():
        store.find(1)['expiry'] = '2026-08-01'
    t = store.find(1)
    today = datetime(2026, 8, 12).date()
    assert monitor._settle_if_expired(store, t, spot=118.0, today=today,
                                      dry_run=True) is True
    done = store.find(1)
    assert done['status'] == 'exited'
    assert done['exit_reason'] == 'paper:expiry'
    assert done['exit_debit'] == 18.0        # spot 118 - long 100, capped at 40


def test_settlement_is_bounded_by_the_width(store, monkeypatch):
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    with store._mutate():
        store.find(1)['expiry'] = '2026-08-01'
    t = store.find(1)
    monitor._settle_if_expired(store, t, spot=400.0,
                               today=datetime(2026, 8, 12).date(), dry_run=True)
    assert store.find(1)['exit_debit'] == 40.0


def test_expiry_day_itself_still_trades(store):
    """Strictly PAST expiry. On expiry day the option is still live and the
    ordinary exits should get their chance."""
    with store._mutate():
        store.find(1)['expiry'] = '2026-08-12'
    assert monitor._settle_if_expired(store, store.find(1), spot=118.0,
                                      today=datetime(2026, 8, 12).date(),
                                      dry_run=True) is False


# ── the bound that PIIND needed ──────────────────────────────────────────

def test_a_booked_value_cannot_go_below_zero(store):
    """PIIND #50 booked exit_debit -30.04 on a debit of 242.11 = -112.4% on a
    -100%-capped structure. This is the last place every exit passes through,
    including `zebra close` where a human types the number."""
    store.mark_exited(1, 96.0, -5.0, 'paper:debit_sl')
    t = store.find(1)
    assert t['exit_debit'] == 0.0
    assert t['pnl_pct'] == -100.0, "a loss deeper than the max loss was booked"


def test_a_booked_value_cannot_exceed_the_width(store):
    store.mark_exited(1, 150.0, 99.0, 'paper:tp')
    assert store.find(1)['exit_debit'] == 40.0

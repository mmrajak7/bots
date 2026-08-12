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


# ── the feed going dark ──────────────────────────────────────────────────

def test_no_price_for_any_position_alerts_instead_of_going_quiet(store,
                                                                 monkeypatch):
    """`get_ltp` guards kite.ltp() but NOT the instrument-cache load beneath
    it, so a dead token raised straight out of check_entered. run_cycle logged
    one line and exit monitoring stopped on every open position — no Telegram,
    and the blind counter untouched because it lives in the per-trade loop
    that never ran."""
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)

    def dead_token(kite, stocks):
        raise Exception('TokenException: Incorrect `api_key` or `access_token`')
    monkeypatch.setattr(monitor, 'get_ltp', dead_token)
    monitor.check_entered(store, kite=None, dry_run=True)

    assert sent, "monitoring went blind without telling anyone"
    assert 'BLIND' in sent[0]
    assert 'kite_access_token' in sent[0], "the alert must name the likely cause"


def test_the_blind_alert_fires_once_a_day_not_once_a_cycle(store, monkeypatch):
    """The cron process EXITS between cycles, so an in-memory guard would
    re-alert every five minutes. 78 identical messages a day is how a reader
    learns to ignore the one that matters."""
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {})
    for _ in range(3):
        monitor.check_entered(store, kite=None, dry_run=True)
    assert len(sent) == 1, f"blind alert fired {len(sent)} times in one day"


def test_a_partial_feed_is_not_treated_as_blind(store, monkeypatch):
    """Companion to the two above: 'blind' means NO position can be priced.
    One missing symbol out of many is an ordinary skip, not a full stop."""
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'TESTCO': 105.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {'mid': 11.0,
                                                    'reliable': True,
                                                    'reason': None})
    monitor.check_entered(store, kite=None, dry_run=True)
    assert not any('BLIND' in m for m in sent)


# ── the exit book ────────────────────────────────────────────────────────

def test_the_exit_book_is_persisted_not_just_the_price(store, monkeypatch):
    """Entry books have been stored since fill pricing landed; exits kept only
    two scalars — so the one direction that has twice cost real money was the
    one direction with no evidence. An option book cannot be reconstructed
    after the fact."""
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    book = {'long': {'symbol': 'L', 'bid': 12.0, 'ask': 12.4, 'oi': 9000},
            'short': {'symbol': 'S', 'bid': 1.8, 'ask': 2.0, 'oi': 8000}}
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 105.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {'mid': 10.4,
                                                    'reliable': True,
                                                    'reason': None,
                                                    'legs': book,
                                                    'floored': False})
    monitor.check_entered(store, kite=None, dry_run=True)   # spot 105 >= tp 100
    t = store.find(1)
    assert t['status'] == 'exited'
    assert t['exit_legs'] == book, "the book we exited on was thrown away"


def test_a_failed_close_releases_the_flag_instead_of_disarming_the_exit(
        store, monkeypatch):
    """Only ValueError was caught, so a LockTimeout propagated with the
    consume-once flag ALREADY claimed: the exit was announced on Telegram,
    never booked, and that exit kind permanently disarmed for the position."""
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)

    def boom(*a, **k):
        raise OSError('disk went away mid-write')
    monkeypatch.setattr(store, 'mark_exited', boom)
    t = store.find(1)
    store.set_alert_flag(1, 'tp')
    monitor._paper_auto_close(store, t, 11.0, 'tp', 105.0)
    assert store.find(1).get('tp_alerted_at') is None, \
        "the exit kind was left permanently disarmed"
    assert store.find(1)['status'] == 'entered'


# ── the manual entry path ────────────────────────────────────────────────

def test_a_hand_entered_bcs_is_recorded_as_a_bcs(store):
    """`_apply_entry` stamped neither structure nor width, so every BCS
    entered through the CLI was valued at 2*long - short: debit SL disarmed,
    trail dead for want of a width, P&L doubled. LIVE-only, which is why paper
    never caught it."""
    t = store.find(1)
    assert t['structure'] == 'bcs'
    assert t['width'] == 40.0
    assert monitor._long_multiplier(t) == 1, "a BCS was valued as a zebra"


def test_the_zebra_path_stays_unstamped(store, tmp_path, monkeypatch):
    """Companion. Only 'bcs' is stamped — every reader keys on
    `structure != 'bcs'`, and the fix must not change what a zebra looks
    like."""
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'z2.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'z2.lock')
    s = ZebraStore()
    s.add_signal({'stock': 'OTHERCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 90.0, 'short_strike': 100.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 5.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'zebra'})
    t = s.find(1)
    assert t.get('structure') is None
    assert t.get('width') is None
    assert monitor._long_multiplier(t) == 2


# ── paper must not book a price it could not have traded at ──────────────

def test_paper_refuses_to_book_at_an_unreliable_mid(store, monkeypatch):
    """The reliability freeze covered DEBIT-SL and TRAIL only, so TP, SPOT-SL
    and TIME booked whatever a crossed book said — and in paper the booked
    number IS the result."""
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    store.set_alert_flag(1, 'tp')
    out = monitor._paper_auto_close(store, store.find(1), 11.0, 'tp', 105.0,
                                    reliable=False)
    assert out is None, "a price off an unreliable book was booked"
    assert store.find(1)['status'] == 'entered'
    assert store.find(1).get('tp_alerted_at') is None, \
        "the exit was announced, never booked, and left disarmed"


def test_paper_books_normally_on_a_reliable_mid(store, monkeypatch):
    """Companion, so the test above cannot pass by refusing everything."""
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    out = monitor._paper_auto_close(store, store.find(1), 11.0, 'tp', 105.0,
                                    reliable=True)
    assert out is not None and store.find(1)['status'] == 'exited'

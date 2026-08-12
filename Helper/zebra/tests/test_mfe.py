"""Max favourable excursion — peak capture and the give-back table.

The point of these tests is not that a max works. It is that the peak survives
the two ways this fleet has actually lost data:

  1. a garbage-high quote poisoning a MAX permanently (no later good read can
     undo a peak, unlike a stop, which self-corrects on the next poll);
  2. a value recorded in a place that never reaches Drive — the failure mode
     behind FIFTY's breadth capture, which looked deployed and captured zero
     sessions.

Run:  cd Helper && python -m pytest zebra/tests/test_mfe.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import mfe                      # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402

SIGNAL = {
    'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
    'st_value': 110.0, 'st_direction': 'UP',
    'signal_price': 100.0, 'signal_gap_pct': 4.0,
}
ENTRY = {'long_strike': 90.0, 'short_strike': 100.0,
         'long_symbol': 'TESTCO26SEP90CE', 'short_symbol': 'TESTCO26SEP100CE',
         'debit': 10.0, 'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30'}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    return s


@pytest.fixture
def entered(store):
    """One entered CE trade: entry spot 100, debit 10, TP at the ST line 110."""
    store.add_signal(dict(SIGNAL))
    store.mark_triggered(1, 100.0, 4.0, [])
    store.mark_entered(1, dict(ENTRY))
    return store


def poll(store, spot, mid=None, reliable=True):
    """One monitor poll's worth of capture + flush, then re-read the store."""
    patch = mfe.compute(store.find(1), spot, mid, reliable)
    if patch:
        store.apply_mfe({1: patch})
    return store.find(1)


# ── the basic measurement ────────────────────────────────────────────────
def test_peak_advances_on_favourable_moves_only(entered):
    t = poll(entered, 104.0, 12.0)
    assert t['mfe_spot'] == 104.0 and t['mfe_mid'] == 12.0

    t = poll(entered, 101.0, 10.5)          # pullback
    assert t['mfe_spot'] == 104.0, "peak retreated with the price"
    assert t['mfe_mid'] == 12.0

    t = poll(entered, 106.0, 13.0)
    assert t['mfe_spot'] == 106.0 and t['mfe_mid'] == 13.0


def test_pe_trade_peaks_downward(store):
    """A PE trade profits as the underlying FALLS. One sign flip covers both
    directions; getting it backwards would record every PE peak as its worst
    moment."""
    store.add_signal(dict(SIGNAL, direction='PE', st_direction='DOWN',
                          st_value=90.0))
    store.mark_triggered(1, 100.0, 4.0, [])
    store.mark_entered(1, dict(ENTRY))

    poll(store, 96.0, 11.0)
    t = poll(store, 99.0, 10.2)             # unfavourable for a PE
    assert t['mfe_spot'] == 96.0, "PE peak tracked the wrong direction"
    t = poll(store, 94.0, 12.0)
    assert t['mfe_spot'] == 94.0


def test_peak_seeds_at_entry_for_a_trade_that_never_went_favourable(entered):
    """MFE of a losing trade is zero excursion, not unknown — but `at` stays
    None, which is what tells the two apart."""
    t = poll(entered, 97.0, 8.0)
    assert t['mfe_spot'] == 100.0          # entry spot, not 97
    assert t['mfe_mid'] == 10.0            # entry debit, not 8
    assert t['mfe_spot_at'] is None and t['mfe_mid_at'] is None


def test_peak_timestamp_only_set_when_a_real_peak_lands(entered):
    poll(entered, 97.0, 8.0)
    t = poll(entered, 105.0, 12.0)
    assert t['mfe_spot_at'] is not None and t['mfe_mid_at'] is not None


# ── the guard that matters: one bad print must not poison a MAX ──────────
def test_single_garbage_high_mid_is_rejected(entered):
    """The ABB/NHPC shape. A tidy-looking book quotes an impossible price for
    one poll. A peak is permanent, so accepting it would corrupt the record and
    (once the trail consumes this field) arm a stop at a level never traded."""
    poll(entered, 104.0, 12.0)
    t = poll(entered, 104.0, 95.0)          # 8x the peak, one poll
    assert t['mfe_mid'] == 12.0, "a single absurd mid became the peak"

    t = poll(entered, 104.0, 12.5)          # book returns to sanity
    assert t['mfe_mid'] == 12.5


def test_a_sustained_jump_is_accepted_after_confirmation(entered):
    """The gate must not blind the peak to real news. A genuine move repeats,
    and what gets stored is the most conservative reading of the window."""
    poll(entered, 104.0, 12.0)
    t = poll(entered, 104.0, 30.0)
    assert t['mfe_mid'] == 12.0             # first sighting: candidate only
    t = poll(entered, 104.0, 31.0)
    assert t['mfe_mid'] == 30.0, "a confirmed move never landed"


def test_confirmation_window_stores_the_conservative_reading(entered):
    """Ascending order on purpose: with the readings the other way round the
    window minimum and the last reading are the same number, and the test
    cannot tell 'took the minimum' from 'took whatever came last'."""
    poll(entered, 104.0, 12.0)
    poll(entered, 104.0, 28.0)
    t = poll(entered, 104.0, 40.0)
    assert t['mfe_mid'] == 28.0, "stored the optimistic end of the window"


def test_stale_candidates_do_not_pair_up(entered, monkeypatch):
    """Two unrelated bad ticks hours apart must not confirm each other."""
    poll(entered, 104.0, 12.0)
    poll(entered, 104.0, 40.0)
    real_time = mfe.time.time
    monkeypatch.setattr(mfe.time, 'time',
                        lambda: real_time() + cfg.CONFIRM_STALE_SEC + 60)
    t = poll(entered, 104.0, 41.0)
    assert t['mfe_mid'] == 12.0, "a stale candidate validated a later one"


def test_garbage_spot_print_is_rejected(entered):
    """Spot LTP is real trades, but an outlier opening print is exactly why the
    live monitor's SL_SPOT needed a debounce."""
    poll(entered, 104.0, 12.0)
    t = poll(entered, 400.0, 12.0)
    assert t['mfe_spot'] == 104.0, "an absurd spot print became the peak"


def test_unreliable_book_never_moves_the_mid_peak(entered):
    """Same rule as the DEBIT-SL: a wide/crossed/one-sided book has no opinion
    worth recording. The underlying is still measurable on that poll."""
    t = poll(entered, 106.0, 99.0, reliable=False)
    assert t.get('mfe_mid') is None, "an unreliable book set the peak"
    assert t['mfe_spot'] == 106.0, "a dark option book blinded the spot peak"

    t = poll(entered, 106.0, 12.0, reliable=True)
    assert t['mfe_mid'] == 12.0, "the mid channel never recovered"


def test_missing_mid_still_records_the_spot_peak(entered):
    t = poll(entered, 107.0, None)
    assert t['mfe_spot'] == 107.0
    assert t.get('mfe_mid') is None


# ── persistence ──────────────────────────────────────────────────────────
def test_peak_survives_a_process_restart(entered, tmp_path):
    """The cron is a fresh process every 5 minutes; state held only in memory
    would reset the peak on every poll and always report the last quote."""
    poll(entered, 108.0, 14.0)
    fresh = ZebraStore(config={})
    fresh._load_local()
    t = fresh.find(1)
    assert t['mfe_spot'] == 108.0 and t['mfe_mid'] == 14.0


def test_peak_reaches_drive_on_exit(entered, monkeypatch):
    """MFE writes are local-only to avoid churning Drive every poll. That is
    only safe if the exit — the drive=True write — carries them. FIFTY's breadth
    capture ran 0 sessions while looking deployed; this is the same class."""
    uploaded = {}
    monkeypatch.setattr(entered, '_upload_to_drive',
                        lambda: uploaded.update(
                            {t['id']: dict(t) for t in entered._trades}))
    poll(entered, 109.0, 15.0)
    assert not uploaded, "an MFE poll pushed to Drive"

    entered.mark_exited(1, 103.0, 11.0, 'paper:tp')
    assert uploaded[1]['mfe_mid'] == 15.0, "the peak never reached Drive"
    assert uploaded[1]['mfe_spot'] == 109.0


def test_apply_mfe_refuses_foreign_keys(entered):
    """A whole-state patch is a footgun: one typo'd key would overwrite status
    or debit on a live position."""
    with pytest.raises(ValueError):
        entered.apply_mfe({1: {'status': 'exited'}})
    assert entered.find(1)['status'] == 'entered'


def test_a_whole_cycle_costs_one_store_write(entered, monkeypatch):
    """The store file is ~1 MB and this runs for every open position on every
    poll. A write per trade would rewrite ~20 MB a poll on a trending day and
    take the cross-process lock each time, on a Pi that also runs the
    live-money monitor."""
    for tid, stock in ((2, 'ACME'), (3, 'BETACO')):
        entered.add_signal(dict(SIGNAL, stock=stock))
        entered.mark_triggered(tid, 100.0, 4.0, [])
        entered.mark_entered(tid, dict(ENTRY))

    writes = []
    real_save = entered._save_local
    monkeypatch.setattr(entered, '_save_local',
                        lambda: (writes.append(1), real_save())[1])
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {s: 105.0 for s in stocks})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 13.0, 'reliable': True, 'reason': None})
    monitor.check_entered(entered, kite=None, dry_run=True)

    assert len(writes) == 1, f"{len(writes)} store writes for 3 trades"
    assert all(entered.find(i)['mfe_spot'] == 105.0 for i in (1, 2, 3))


def test_a_quiet_cycle_writes_nothing(entered, monkeypatch):
    """Once peaks stop advancing — the common case for most positions most of
    the time — the tracking must go completely silent."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 105.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 13.0, 'reliable': True, 'reason': None})
    monitor.check_entered(entered, kite=None, dry_run=True)

    writes = []
    real_save = entered._save_local
    monkeypatch.setattr(entered, '_save_local',
                        lambda: (writes.append(1), real_save())[1])
    monitor.check_entered(entered, kite=None, dry_run=True)
    assert writes == [], "an unchanged peak still rewrote the store"


# ── wiring: the monitor must actually call this ──────────────────────────
def test_monitor_poll_captures_a_peak(entered, monkeypatch):
    """The recurring bug in this fleet is code that is written, tested, and
    never reached. Drive the REAL check_entered, not the helper."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 105.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 13.0, 'reliable': True, 'reason': None})
    monitor.check_entered(entered, kite=None, dry_run=True)
    t = entered.find(1)
    assert t['mfe_spot'] == 105.0 and t['mfe_mid'] == 13.0


def test_capture_runs_before_the_exit_books(entered, monkeypatch):
    """A TP exit fires on the poll that is, by definition, near the high. If
    capture ran after the exit branches the best trades — the ones the
    give-back question is about — would record no peak at all."""
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 111.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 14.0, 'reliable': True, 'reason': None})
    monkeypatch.setattr(monitor, '_exit_cleared', lambda *a, **k: True)
    monitor.check_entered(entered, kite=None, dry_run=True)
    t = entered.find(1)
    assert t['status'] == 'exited', "TP did not fire — test asserts nothing"
    assert t['mfe_mid'] == 14.0, "the exit poll's peak was lost"
    assert t['mfe_spot'] == 111.0


def test_exit_upload_carries_the_peak(entered, monkeypatch):
    """The one that catches an unflushed patch. Peaks are batched in memory, and
    `_merge` gives DISK the tie on equal versions — so if the cycle's patch has
    not reached disk before mark_exited refreshes, mark_exited silently drops it
    and uploads a trade with no peak. A later flush would repair the LOCAL file
    and hide the loss completely; only the Drive copy shows it."""
    uploaded = {}
    monkeypatch.setattr(entered, '_upload_to_drive',
                        lambda: uploaded.update(
                            {t['id']: dict(t) for t in entered._trades}))
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': 111.0})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 14.0, 'reliable': True, 'reason': None})
    monkeypatch.setattr(monitor, '_exit_cleared', lambda *a, **k: True)
    monitor.check_entered(entered, kite=None, dry_run=True)

    assert uploaded[1]['status'] == 'exited'
    assert uploaded[1]['mfe_mid'] == 14.0, "the peak never reached Drive"
    assert uploaded[1]['mfe_spot'] == 111.0


def test_exit_above_the_recorded_peak_never_reports_negative_giveback():
    """The jump gate can legitimately refuse the exit poll's own mid — a big
    final-poll move has no following poll to confirm it. The booked exit price
    is stronger evidence than any quote, so it floors the peak."""
    r = mfe.excursion(_closed(mfe_mid=12.0, exit_debit=25.0, pnl=1500.0))
    assert r['peak_gain'] == 1500.0
    assert r['given_back'] == 0.0
    assert r['kept_pct'] == 100.0


# ── the analysis ─────────────────────────────────────────────────────────
def _closed(**over):
    t = {'id': 1, 'status': 'exited', 'stock': 'TESTCO', 'structure': 'bcs',
         'debit': 10.0, 'quantity': 100, 'entry_spot': 100.0, 'tp_spot': 110.0,
         'pnl': -400.0, 'exit_reason': 'debit_sl', 'mfe_mid': 18.0,
         'mfe_spot': 108.0}
    t.update(over)
    return t


def test_excursion_measures_what_was_handed_back():
    r = mfe.excursion(_closed())
    assert r['peak_gain'] == 800.0          # (18 - 10) * 100
    assert r['given_back'] == 1200.0        # peak 800 -> booked -400
    assert r['tp_progress'] == 0.8          # 8 of the 10 points to target


def test_unmeasured_trades_are_not_counted_as_zero_giveback():
    """Every trade closed before capture existed has no peak. Reporting those
    as zero would read as 'no leak', which is the opposite of unknown."""
    assert mfe.excursion(_closed(mfe_mid=None)) is None
    g = mfe.giveback([_closed(mfe_mid=None), _closed(id=2)])
    assert g['measured'] == 1 and g['unmeasured'] == 1


def test_giveback_isolates_the_winners_that_became_losers():
    rows = [
        _closed(id=1),                                   # 80% to TP, lost
        _closed(id=2, mfe_spot=102.0, pnl=-500.0),        # 20% to TP, lost
        _closed(id=3, pnl=700.0),                         # won
    ]
    g = mfe.giveback(rows, min_progress=0.7)
    assert g['reached_then_lost'] == 1
    assert g['reached_then_lost_pnl'] == -400.0
    assert g['was_ahead'] == 3


def test_open_trades_are_excluded_from_the_table():
    assert mfe.excursion(_closed(status='entered')) is None

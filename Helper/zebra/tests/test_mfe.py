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
from datetime import datetime
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import mfe                      # noqa: E402
from zebra import monitor                  # noqa: E402
from zebra import outcomes                 # noqa: E402
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


# ── gain-anchored trail ──────────────────────────────────────────────────
SHADOW = {'long_strike': 100.0, 'short_strike': 140.0, 'width': 40.0,
          'long_symbol': 'TESTCO26SEP100CE', 'short_symbol': 'TESTCO26SEP140CE',
          'debit': 10.0, 'lot_size': 100, 'expiry': '2026-09-30',
          'entry_spot': 100.0, 'debit_to_width_pct': 25.0,
          'short_extrinsic': 1.0, 'warnings': []}


@pytest.fixture
def bcs(entered):
    """A real BCS shadow via add_bcs_shadow — width 40, debit 10, max gain 30.

    Built through the production path on purpose. An earlier version of this
    fixture set `width` on the dict from find(): that mutation was silently
    erased by the next store write, because `_merge` gives DISK the tie on
    equal versions, and the trail tests then passed for the wrong reason.
    """
    shadow = entered.add_bcs_shadow(entered.find(1), dict(SHADOW))
    entered._shadow_id = shadow['id']
    return entered


def bpoll(store, spot, mid=None, reliable=True):
    """Capture + flush against the SHADOW, not the parent zebra."""
    tid = store._shadow_id
    patch = mfe.compute(store.find(tid), spot, mid, reliable)
    if patch:
        store.apply_mfe({tid: patch})
    return store.find(tid)


def test_zebra_has_no_trail(entered):
    """A back-ratio has two longs and no capped payoff, so 'fraction of max
    gain' is undefined. Zebra positions run to expiry untrailed by design."""
    poll(entered, 108.0, 20.0)
    assert mfe.trail_levels(entered.find(1)) is None


def _climb(store, *mids):
    """Walk the mid up in believable steps. The MFE jump gate refuses a peak
    more than 50% above the running peak on sight, so a test that leaps from
    10 to 20 in one poll measures the GATE, not the trail."""
    for m in mids:
        bpoll(store, 106.0, m)


def test_trail_arms_at_half_of_max_gain(bcs):
    _climb(bcs, 14.0, 20.0)                     # gain 10 of max 30 = 33%
    assert mfe.trail_levels(bcs.find(bcs._shadow_id))['armed'] is False
    _climb(bcs, 25.0)                           # gain 15 of 30 = 50%
    tl = mfe.trail_levels(bcs.find(bcs._shadow_id))
    assert tl['armed'] is True
    assert tl['peak_pct_of_max'] == 50.0
    assert tl['level'] == 17.5                 # debit 10 + half of peak gain 15


def test_trail_stays_armed_after_the_gain_evaporates(bcs):
    """The whole point. A position that WAS up half its max and has given it
    back is the case this exists for — arming off the LIVE gain would disarm at
    the moment it starts mattering."""
    _climb(bcs, 14.0, 20.0, 25.0)
    bpoll(bcs, 101.0, 11.0)                     # gain collapses to 1
    tl = mfe.trail_levels(bcs.find(bcs._shadow_id))
    assert tl['armed'] is True
    assert tl['level'] == 17.5, "the level followed the price down"


def test_trail_level_sits_above_the_entry_debit(bcs):
    """Bounds the LEVEL. It does NOT bound the fill — see
    test_a_gap_through_the_trail_books_a_loss, which is the same trail firing
    below its own level because the monitor books at `mid`, not at `level`."""
    for mid in (14.0, 20.0, 25.0, 35.0):
        bpoll(bcs, 106.0, mid)
        tl = mfe.trail_levels(bcs.find(bcs._shadow_id))
        if tl['armed']:
            assert tl['level'] > bcs.find(bcs._shadow_id)['debit']


def test_trail_is_not_the_live_monitors_2x_rule(bcs):
    """A fixed 2x-debit engage would arm at mid 20 here — 33% of max gain — and
    on a 45% d/w spread it would arm at 82%. Anchoring to max gain is what
    keeps the trigger meaning the same thing across spreads."""
    _climb(bcs, 14.0, 20.0)                     # exactly 2x the entry debit
    assert mfe.trail_levels(bcs.find(bcs._shadow_id))['armed'] is False


def test_degenerate_spread_has_no_trail():
    """debit at or above width: there is no gain to protect, and the level
    arithmetic would put the stop above the structure's maximum value."""
    assert mfe.trail_levels({'width': 10.0, 'debit': 10.0,
                             'mfe_mid': 12.0}) is None
    assert mfe.trail_levels({'width': 10.0, 'debit': 12.0,
                             'mfe_mid': 12.0}) is None


# ── trail: the monitor path ──────────────────────────────────────────────
def _wire(monkeypatch, spot, mid):
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'TRAIL_ENABLED', True)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'TESTCO': spot})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': mid, 'reliable': True, 'reason': None})
    monkeypatch.setattr(monitor, '_exit_cleared', lambda *a, **k: True)


def test_trail_exit_fires_and_books_a_profit(bcs, monkeypatch):
    for m in (14.0, 20.0, 25.0):                           # climb past 50%
        _wire(monkeypatch, 106.0, m)
        monitor.check_entered(bcs, kite=None, dry_run=True)
    assert mfe.trail_levels(bcs.find(bcs._shadow_id))['armed'] is True

    _wire(monkeypatch, 103.0, 17.0)                        # 17 <= level 17.5
    monitor.check_entered(bcs, kite=None, dry_run=True)    # confirm 1/2
    assert bcs.find(bcs._shadow_id)['status'] == 'entered', "trail fired on a single poll"
    monitor.check_entered(bcs, kite=None, dry_run=True)    # confirm 2/2

    t = bcs.find(bcs._shadow_id)
    assert t['status'] == 'exited'
    assert t['exit_reason'] == 'paper:trail'
    assert t['pnl'] > 0, "a trail exit booked a LOSS"


def test_a_gap_through_the_trail_books_a_loss(bcs, monkeypatch):
    """The trigger is `mid <= level`; the booking price is `mid`. Nothing keeps
    the two together, so a gap straight through the level books wherever the
    gap landed — here below the entry debit, i.e. a LOSS tagged `trail`.

    This is intended behaviour (a breached trail means get out, and refusing to
    exit would hold the exact give-back the trail exists to stop). What is NOT
    acceptable is scoring it a win, which is what the old reason-string-only
    map did — see the companion test in test_outcomes.py."""
    for m in (14.0, 20.0, 25.0):                           # arm the trail
        _wire(monkeypatch, 106.0, m)
        monitor.check_entered(bcs, kite=None, dry_run=True)
    assert mfe.trail_levels(bcs.find(bcs._shadow_id))['armed'] is True

    _wire(monkeypatch, 95.0, 2.0)                          # gap FAR below 17.5
    monitor.check_entered(bcs, kite=None, dry_run=True)    # confirm 1/2
    monitor.check_entered(bcs, kite=None, dry_run=True)    # confirm 2/2

    t = bcs.find(bcs._shadow_id)
    assert t['status'] == 'exited' and t['exit_reason'] == 'paper:trail'
    assert t['pnl'] < 0, "the gap case no longer books a loss — retune this test"
    assert outcomes.label_for_reason(t['exit_reason'], t['pnl']) == outcomes.MISS


def test_trail_never_fires_before_it_arms(bcs, monkeypatch):
    """Mid below what would be a trail level, but the peak never reached half
    of max gain — there is nothing to protect yet."""
    for m in (14.0, 20.0):
        _wire(monkeypatch, 106.0, m)
        monitor.check_entered(bcs, kite=None, dry_run=True)
    _wire(monkeypatch, 102.0, 12.0)
    for _ in range(3):
        monitor.check_entered(bcs, kite=None, dry_run=True)
    assert bcs.find(bcs._shadow_id)['status'] == 'entered'


def test_an_unreliable_book_cannot_fire_the_trail(bcs, monkeypatch):
    """Same rule as the DEBIT-SL. A garbage-low mid on a broken book would
    otherwise end a position that was working."""
    for m in (14.0, 20.0, 25.0):
        _wire(monkeypatch, 106.0, m)
        monitor.check_entered(bcs, kite=None, dry_run=True)
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': 0.4, 'reliable': False, 'reason': 'one-sided'})
    for _ in range(3):
        monitor.check_entered(bcs, kite=None, dry_run=True)
    assert bcs.find(bcs._shadow_id)['status'] == 'entered'


def test_trail_is_vetted_like_a_value_trigger(bcs):
    """It prices off the option book, so the pre-filter must flag it even when
    the book looks fine — the ABB case looked fine."""
    from zebra import vet
    interesting, why = vet.needs_exit_vet(
        bcs.find(bcs._shadow_id), 'trail', {'reliable': True},
        now=datetime(2026, 8, 12, 14, 0, tzinfo=cfg.IST))
    assert interesting, "a trail exit skipped vetting on a tidy-looking book"
    assert 'value-based' in why


def test_every_exit_kind_the_monitor_raises_can_record_a_verdict():
    """An exit kind the CLI's argparse rejects produces a gate that spawns an
    agent which cannot answer it: the agent exits having done nothing and the
    exit defers to the cap and escalates. Wired-but-inert, the usual shape.

    The kinds are read from the MONITOR's own `_exit_cleared` call sites, not
    from `vet.EXIT_KINDS`. Asserting the CLI accepts everything in EXIT_KINDS
    is circular — shrink that list and the test shrinks with it, which is
    exactly the drift this guards against.
    """
    import re
    import subprocess
    src = (HELPER / 'zebra' / 'monitor.py').read_text(encoding='utf-8')
    raised = set(re.findall(r"_exit_cleared\(store, trade, '(\w+)'", src))
    assert raised, "found no exit kinds in monitor.py — the pattern moved"

    out = subprocess.run([sys.executable, '-m', 'zebra', 'vet',
                          'exit-decide', '--help'],
                         cwd=str(HELPER), capture_output=True, text=True)
    for kind in sorted(raised):
        assert kind in out.stdout, \
            f"monitor raises '{kind}' but the CLI cannot record a verdict for it"


def test_trail_exit_scores_as_a_hit():
    """It always books a profit, so labelling it a MISS would punish the allow
    that worked and make the trail look like a source of losses."""
    from zebra import outcomes
    assert outcomes.label_for_reason('paper:trail') == outcomes.HIT


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

"""Swing-shortened TP and ST-attraction history (zebra/history.py).

Two features off one candle series:
  1. A swing level standing between spot and the ST magnet shortens the TP —
     the LUPIN case, where a PE signal had a prior swing LOW well above the ST
     line and price was far likelier to stall there than to run the distance.
  2. Whether a symbol actually GETS pulled back to its ST line, handed to the
     vetting agent so the magnet thesis stops being assumed.

Run:  cd Helper && python -m pytest zebra/tests/test_history.py -v
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg            # noqa: E402
from zebra import history                  # noqa: E402


def _c(lo, hi, close=None):
    return {'date': '2026-01-01', 'open': lo, 'high': hi, 'low': lo,
            'close': close if close is not None else (lo + hi) / 2,
            'volume': 1000}


def _flat(n, lo=100.0, hi=101.0):
    return [_c(lo, hi) for _ in range(n)]


@pytest.fixture(autouse=True)
def _clear_cache():
    history._attraction_cache.clear()
    yield
    history._attraction_cache.clear()


def _candles(monkeypatch, rows):
    monkeypatch.setattr(history, '_timeframe_candles', lambda *a, **k: rows)


# ── 1. swing-shortened TP ────────────────────────────────────────────────
def test_a_pe_signal_books_at_the_swing_low_in_the_way(monkeypatch):
    """LUPIN. Price 100 above a rising ST at 88, with a swing low at 94 in
    between: the fall stalls at its own support far more often than it runs
    the whole way to the magnet."""
    rows = _flat(6, 96, 98) + [_c(94, 97)] + _flat(6, 96, 99)
    _candles(monkeypatch, rows)
    r = history.swing_tp(None, 'LUPIN', 'weekly', 'PE', 100.0, 88.0)
    assert r is not None
    assert r['tp_spot'] == 94.0
    assert r['kind'] == 'swing_low'
    assert r['st_value'] == 88.0
    assert 0 < r['shortened_by_pct'] < 100


def test_a_ce_signal_books_at_the_swing_high(monkeypatch):
    """The mirror: price below ST rising to it, and a prior swing HIGH is
    resistance standing in the way."""
    rows = _flat(6, 101, 103) + [_c(102, 106)] + _flat(6, 101, 103)
    _candles(monkeypatch, rows)
    r = history.swing_tp(None, 'X', 'weekly', 'CE', 100.0, 112.0)
    assert r is not None and r['tp_spot'] == 106.0 and r['kind'] == 'swing_high'


def test_the_nearest_level_wins_not_the_deepest(monkeypatch):
    """Price meets them in order. The first one it reaches is the one that
    stops it, so booking at the deeper level assumes it breaks the first."""
    rows = (_flat(4, 96, 98) + [_c(85, 97)] + _flat(4, 96, 98)
            + [_c(90, 97)] + _flat(4, 96, 98))
    _candles(monkeypatch, rows)
    # ST 80 so BOTH levels clear the retained-distance floor and the choice is
    # genuinely about which one price meets first.
    r = history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 80.0)
    assert r['tp_spot'] == 90.0, "took the deeper swing, skipping the first one"


def test_a_level_beyond_the_st_line_is_ignored(monkeypatch):
    """It would LENGTHEN the target. This feature only ever shortens — a
    further target is a different trade nobody asked for."""
    rows = _flat(6, 96, 98) + [_c(80, 97)] + _flat(6, 96, 98)
    _candles(monkeypatch, rows)
    assert history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0) is None


def test_a_level_hugging_spot_is_not_a_target(monkeypatch):
    """Support half a percent below the entry print is noise, and booking
    there pays the round-trip spread for nothing."""
    rows = _flat(6, 99.9, 100.5) + [_c(99.7, 100.2)] + _flat(6, 99.9, 100.5)
    _candles(monkeypatch, rows)
    assert history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0) is None


def test_an_unconfirmed_pivot_at_the_right_edge_is_not_a_level(monkeypatch):
    """A low with nothing after it is the move still happening. Treating it as
    support is how a target lands inside the candle currently breaking it."""
    rows = _flat(8, 96, 98) + [_c(94, 97)]          # last candle, no bars after
    _candles(monkeypatch, rows)
    assert history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0) is None


def test_no_candles_keeps_the_st_line(monkeypatch):
    _candles(monkeypatch, [])
    assert history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0) is None


def test_the_feature_can_be_switched_off(monkeypatch):
    rows = _flat(6, 96, 98) + [_c(94, 97)] + _flat(6, 96, 98)
    _candles(monkeypatch, rows)
    monkeypatch.setattr(cfg, 'SWING_TP_ENABLED', False)
    assert history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0) is None


def test_only_recent_candles_are_searched(monkeypatch):
    """A swing from five years ago is archaeology, not support."""
    old = _flat(3, 96, 98) + [_c(94, 97)] + _flat(3, 96, 98)
    rows = old + _flat(cfg.SWING_LOOKBACK_CANDLES + 5, 99.0, 99.5)
    _candles(monkeypatch, rows)
    assert history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0) is None


@pytest.mark.parametrize('bad', [(None, 88.0), (100.0, None), ('x', 88.0)])
def test_unusable_inputs_never_raise(monkeypatch, bad):
    _candles(monkeypatch, _flat(20))
    assert history.swing_tp(None, 'X', 'weekly', 'PE', bad[0], bad[1]) is None


# ── 2. ST attraction ─────────────────────────────────────────────────────
def _trend(n, start, step, band=1.0):
    """A clean directional series, so ST sits predictably on one side."""
    out = []
    p = start
    for _ in range(n):
        out.append(_c(p - band, p + band, p))
        p += step
    return out


def test_a_symbol_that_returns_to_st_scores_high(monkeypatch):
    """Oscillation around the line is exactly the magnet the strategy bets on."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 1.0)   # =100%; ceiling has its own test
    rows = []
    for _ in range(12):
        rows += _trend(6, 100, 2.0) + _trend(6, 112, -2.0)
    _candles(monkeypatch, rows)
    r = history.attraction(None, 'X', 'weekly', 'PE')
    assert r is not None
    assert r['overall']['episodes'] >= 1
    assert r['gap_threshold_pct'] == cfg.ATTRACTION_GAP_PCT


def test_one_long_move_away_is_one_episode_not_many(monkeypatch):
    """A symbol that sat 5% from ST for ten candles must not report ten
    outcomes — the rate would then measure how long it lingered, not whether
    it came back."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 1.0)   # =100%; ceiling has its own test
    rows = _trend(20, 100, 0.2) + _trend(40, 104, 3.0)   # sustained departure
    _candles(monkeypatch, rows)
    r = history.attraction(None, 'X', 'weekly')
    assert r is not None
    # 40 qualifying candles must not become 40 episodes.
    assert r['overall']['episodes'] <= 40 // cfg.ATTRACTION_HORIZON_BARS + 2


def test_a_thin_sample_is_labelled_not_rounded(monkeypatch):
    """2 of 3 is not a 67% hit rate. Report the number AND that it is thin."""
    monkeypatch.setattr(cfg, 'ATTRACTION_MIN_EPISODES', 999)
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 0.05)
    rows = []
    for _ in range(14):
        rows += _trend(5, 100, 0.9) + _trend(5, 104, -0.9)
    _candles(monkeypatch, rows)
    r = history.attraction(None, 'X', 'weekly')
    assert r is not None, "fixture produced no episodes — cannot test labelling"
    assert r['sample'] == 'thin'
    assert 'too few' in r['verdict']


def test_too_little_history_returns_nothing_at_all(monkeypatch):
    _candles(monkeypatch, _flat(5))
    assert history.attraction(None, 'X', 'weekly') is None


def test_attraction_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(cfg, 'ATTRACTION_ENABLED', False)
    _candles(monkeypatch, _trend(60, 100, 1.0))
    assert history.attraction(None, 'X', 'weekly') is None


def test_a_statistics_failure_never_blocks_an_entry(monkeypatch):
    """The magnet stat is evidence, not a gate. If it explodes the signal must
    still be vettable."""
    def boom(*a, **k):
        raise RuntimeError('history exploded')
    monkeypatch.setattr(history, '_attraction', boom)
    assert history.attraction(None, 'X', 'weekly') is None


def test_the_result_is_cached_per_symbol_per_day(monkeypatch):
    calls = []
    monkeypatch.setattr(history, '_attraction',
                        lambda *a, **k: calls.append(1) or {'overall': {}})
    history.attraction(None, 'X', 'weekly')
    history.attraction(None, 'X', 'weekly')
    assert len(calls) == 1, "recomputed within the same day"


def test_a_touch_is_judged_on_the_wick(monkeypatch):
    """TP is a spot trigger checked intraday, not a close. Judging the return
    on closes would score a candle that traded through the level as a miss."""
    st, below = 100.0, {'supertrend': 100.0, 'close': 105.0,
                        'high': 106.0, 'low': 99.0, 'date': 'd'}
    assert history._touches(below, st, from_above=True) is True
    no_touch = dict(below, low=101.0)
    assert history._touches(no_touch, st, from_above=True) is False


# ── 3. wiring: the shortened TP must reach the record ────────────────────
# The recurring failure in this fleet is a feature that is built, tested and
# never reached by the path that actually runs.

def _store(tmp_path, monkeypatch):
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal({'stock': 'LUPIN', 'timeframe': 'weekly', 'direction': 'PE',
                  'st_value': 88.0, 'st_direction': 'UP',
                  'signal_price': 100.0, 'signal_gap_pct': 4.0})
    return s


SWING = {'tp_spot': 94.0, 'kind': 'swing_low', 'date': '2026-06-15',
         'bars_ago': 7, 'timeframe': 'weekly', 'gap_from_spot_pct': 6.0,
         'st_value': 88.0, 'shortened_by_pct': 50.0}
BCS_IN = {'long_strike': 100.0, 'short_strike': 90.0, 'width': 10.0,
          'long_symbol': 'A', 'short_symbol': 'B', 'debit': 4.0,
          'lot_size': 100, 'expiry': '2026-09-30', 'entry_spot': 100.0,
          'debit_to_width_pct': 40.0}


def test_the_bcs_path_books_at_the_shortened_tp(tmp_path, monkeypatch):
    """This is the path that actually trades."""
    s = _store(tmp_path, monkeypatch)
    t = s.mark_entered_bcs(1, dict(BCS_IN, swing_tp=SWING))
    assert t['tp_spot'] == 94.0
    assert t['tp_source'] == 'swing_low'
    assert t['tp_st_line'] == 88.0, "the target it would have used is not recorded"


def test_the_bcs_path_falls_back_to_the_st_line(tmp_path, monkeypatch):
    s = _store(tmp_path, monkeypatch)
    t = s.mark_entered_bcs(1, dict(BCS_IN, swing_tp=None))
    assert t['tp_spot'] == 88.0 and t['tp_source'] == 'st_line'


def test_the_zebra_path_books_at_the_shortened_tp_too(tmp_path, monkeypatch):
    """Both entry paths share _resolve_tp. They derived their TP independently
    before, which is exactly how one of them quietly misses a feature."""
    s = _store(tmp_path, monkeypatch)
    t = s.mark_entered(1, {'long_strike': 90.0, 'short_strike': 100.0,
                           'long_symbol': 'A', 'short_symbol': 'B',
                           'debit': 5.0, 'lot_size': 100, 'lots': 1,
                           'expiry': '2026-09-30', 'swing_tp': SWING})
    assert t['tp_spot'] == 94.0 and t['tp_source'] == 'swing_low'


def test_the_shortened_target_is_explained_on_the_ticket(tmp_path, monkeypatch):
    """A TP that silently differs from the ST line the signal was built on
    reads as a bug to whoever is holding the position."""
    from zebra import monitor
    line = monitor._swing_tp_line(SWING)
    assert '94' in line and 'swing low' in line and '2026-06-15' in line
    assert monitor._swing_tp_line(None) == ''


def test_the_swing_lookup_is_wired_into_the_entry_path():
    """Grep the ENTRY function specifically. A whole-file grep passes on the
    copy in _vet_context, so the TP could stop being shortened while the test
    stayed green — the exact shape of every wiring failure in this fleet."""
    import inspect
    from zebra import monitor
    src = inspect.getsource(monitor._enter_as_bcs)
    assert 'history.swing_tp(' in src, "the entry path no longer looks for a swing"
    assert "bcs['swing_tp']" in src


def test_attraction_is_wired_into_the_vet_context():
    src = (HELPER / 'zebra' / 'monitor.py').read_text(encoding='utf-8')
    assert 'history.attraction(' in src and "'st_attraction'" in src


# ── guards that measuring on real data forced ────────────────────────────
# Across 75 signal-like symbols in the cached universe: 57% had a swing in the
# way, shortening by as much as 82% of the journey; and an unbounded episode
# threshold made the median symbol look non-magnetic (24%) by counting moves
# the scanner would never have signalled.

def test_a_swing_that_eats_the_trade_does_not_move_the_tp(monkeypatch):
    """BHARTIARTL shape: a level 82% of the way back to spot. Keeping a fifth
    of the distance is not a shortened target, it is a much worse trade — and
    under BCS the short strike is still at the ST line, so max gain would need
    a move the TP is set never to wait for."""
    rows = _flat(6, 97.5, 99) + [_c(97.0, 98.5)] + _flat(6, 97.5, 99)
    _candles(monkeypatch, rows)
    r = history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0)
    assert r is not None, "the level must still be reported"
    assert r['applied'] is False
    assert r['tp_spot'] is None, "TP was moved despite failing the floor"
    assert r['level'] == 97.0
    assert 'retains only' in r['reason']


def test_a_rejected_swing_leaves_the_tp_alone_end_to_end(tmp_path, monkeypatch):
    """The not-applied shape must fall through every consumer untouched."""
    from zebra import monitor
    s = _store(tmp_path, monkeypatch)
    rejected = {'tp_spot': None, 'applied': False, 'reason': 'retains only 18%',
                'level': 97.0, 'kind': 'swing_low', 'date': '2026-06-15',
                'bars_ago': 7, 'timeframe': 'weekly', 'st_value': 88.0}
    t = s.mark_entered_bcs(1, dict(BCS_IN, swing_tp=rejected))
    assert t['tp_spot'] == 88.0 and t['tp_source'] == 'st_line'
    assert monitor._swing_tp_line(rejected) == '', "ticket announced a move that never happened"


def test_the_retained_floor_can_be_tuned(monkeypatch):
    rows = _flat(6, 97.5, 99) + [_c(97.0, 98.5)] + _flat(6, 97.5, 99)
    _candles(monkeypatch, rows)
    monkeypatch.setattr(cfg, 'SWING_MIN_RETAINED_PCT', 5.0)
    r = history.swing_tp(None, 'X', 'weekly', 'PE', 100.0, 88.0)
    assert r['applied'] is True and r['tp_spot'] == 97.0


def _stub_st(monkeypatch, close, st, n=60):
    """Hand-built ST series: the gap is then exactly what the test says it is,
    instead of whatever ATR mechanics happen to produce."""
    _candles(monkeypatch, _flat(n))          # length check only
    series = [{'date': 'd%d' % i, 'close': close, 'high': close + 1,
               'low': close - 1, 'supertrend': st, 'direction': 'UP'}
              for i in range(n)]
    import playbook.compute_st as cst
    monkeypatch.setattr(cst, 'compute_supertrend', lambda *a, **k: series)


def test_an_episode_outside_the_traded_band_is_not_counted(monkeypatch):
    """A symbol sitting 20% from its ST is not a setup this strategy takes, so
    its failure to return says nothing about the signal being vetted. Counting
    it is what made every symbol look non-magnetic (median 24% vs 69%)."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 0.08)
    monkeypatch.setattr(cfg, 'ATTRACTION_GAP_PCT', 3.0)
    _stub_st(monkeypatch, close=120.0, st=100.0)        # a 20% gap, always
    assert history.attraction(None, 'FAR', 'weekly') is None,         "counted a departure far outside the band the scanner would signal"


def test_an_episode_inside_the_band_IS_counted(monkeypatch):
    """The other half — without this the test above passes on a function that
    counts nothing at all."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 0.08)
    monkeypatch.setattr(cfg, 'ATTRACTION_GAP_PCT', 3.0)
    _stub_st(monkeypatch, close=105.0, st=100.0)        # a 5% gap, in band
    r = history.attraction(None, 'NEAR', 'weekly')
    assert r is not None and r['overall']['episodes'] > 0


def test_the_band_is_reported_so_the_rate_can_be_read(monkeypatch):
    """A touch rate with no stated band is uninterpretable — the same symbol
    scores 24% or 60% depending on which moves were counted."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 1.0)   # = 100%
    rows = []
    for _ in range(12):
        rows += _trend(6, 100, 2.0) + _trend(6, 112, -2.0)
    _candles(monkeypatch, rows)
    r = history.attraction(None, 'X', 'weekly')
    assert r['gap_band_pct'] == [cfg.ATTRACTION_GAP_PCT, 100.0]  # 1.0 -> 100%
    assert r['horizon_bars'] == cfg.ATTRACTION_HORIZON_BARS


def test_the_gap_band_is_in_the_same_units(monkeypatch):
    """WATCH_GAP_MAX is a FRACTION (0.05 = 5%); the episode gap is a PERCENT.
    Comparing them raw gives the band `3.0 <= gap <= 0.05`, which no candle can
    satisfy — attraction then returns None for every symbol, forever, with
    nothing in the logs to say the feature is dead. This is exactly how it
    shipped the first time; only running it over real candles caught it."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 0.05)
    monkeypatch.setattr(cfg, 'ATTRACTION_GAP_PCT', 3.0)
    rows = []
    for _ in range(14):
        rows += _trend(5, 100, 0.9) + _trend(5, 104, -0.9)
    _candles(monkeypatch, rows)
    r = history.attraction(None, 'X', 'weekly')
    assert r is not None, "the band excluded every candle — units are mismatched"
    assert r['gap_band_pct'] == [3.0, 5.0], r['gap_band_pct']
    assert r['overall']['episodes'] > 0


# ── the band is what the TRIGGER sees, not what the CLOSE says ───────────
# COALINDIA 2026-08-14: a signal fired at a 3.97% intraday gap and was vetted
# with NO magnet history, because 0 of its 147 weekly closes had ever landed in
# the 3-5% band. Re-measured over the 3Y window the live path holds, the
# close-based test left 71% of weekly symbols null-or-thin and 99.8% of monthly
# ones. Testing the candle's RANGE instead took weekly `usable` to 83%.

def _stub_range(monkeypatch, low, high, close, st, n=60):
    """A hand-built series with an explicit high/low, so the band test is
    exercised on the range rather than on whatever ATR happens to produce."""
    _candles(monkeypatch, _flat(n))              # length check only
    series = [{'date': 'd%d' % i, 'close': close, 'high': high, 'low': low,
               'supertrend': st, 'direction': 'UP'} for i in range(n)]
    import playbook.compute_st as cst
    monkeypatch.setattr(cst, 'compute_supertrend', lambda *a, **k: series)


def _band(monkeypatch):
    monkeypatch.setattr(cfg, 'ATTRACTION_GAP_PCT', 3.0)
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 0.05)          # ceiling = 5%


def test_a_candle_that_traded_through_the_band_counts(monkeypatch):
    """THE COALINDIA SHAPE. Close 6.5% above ST — outside the band, so the old
    test skipped it — but the low reached 4%, which is precisely where the
    scanner triggers. If this regresses, the section goes silent again."""
    _band(monkeypatch)
    _stub_range(monkeypatch, low=104.0, high=107.0, close=106.5, st=100.0)
    r = history.attraction(None, 'THROUGH', 'weekly')
    assert r is not None, "a candle that traded the entry band was not counted"
    assert r['overall']['episodes'] > 0


def test_a_candle_that_never_reached_the_band_is_still_skipped(monkeypatch):
    """The other half. Without it the test above passes on a function that
    counts every candle — which is the unbounded threshold this band replaced."""
    _band(monkeypatch)
    _stub_range(monkeypatch, low=100.5, high=102.0, close=101.0, st=100.0)
    assert history.attraction(None, 'NEAR', 'weekly') is None
    # ...and the far side of the band, which the ceiling owns.
    _stub_range(monkeypatch, low=118.0, high=122.0, close=120.0, st=100.0)
    history._attraction_cache.clear()
    assert history.attraction(None, 'FAR2', 'weekly') is None


def test_a_candle_that_also_reached_st_is_not_scored(monkeypatch):
    """Ordering inside a candle is unknowable, so this one cannot say whether
    the entry came before the touch or after it. 22.9% of episodes; counting
    them lifts the median rate 62.5% -> 66.7%, i.e. toward allowing."""
    _band(monkeypatch)
    _stub_range(monkeypatch, low=99.0, high=104.0, close=103.5, st=100.0)
    assert history.attraction(None, 'CROSSED', 'weekly') is None, \
        "scored an episode whose own candle had already reached ST"


def test_the_basis_is_stamped_on_the_result(monkeypatch):
    """Rates from before 2026-08-14 are close-based and run ~4 points higher.
    Comparing the two definitions without noticing is a silent regression."""
    _band(monkeypatch)
    _stub_range(monkeypatch, low=104.0, high=107.0, close=106.5, st=100.0)
    assert history.attraction(None, 'BASIS', 'weekly')['band_basis'] == 'candle_range'


def test_the_agent_is_told_the_rates_moved():
    """A doc quoting the old median against the new statistic teaches the agent
    to read every symbol as ~11 points more magnetic than it measured."""
    doc = (HELPER / 'zebra' / 'VETTING.md').read_text(encoding='utf-8')
    assert 'median 60%' in doc, "VETTING.md still quotes a close-based median"
    assert 'intraday' in doc, "the change of basis is not explained to the agent"
    assert '210 F&O' in doc, \
        "the median must state the universe it was measured on"


# ── measured on weekly only, and that is a DECISION not a failure ────────

def test_monthly_is_reported_as_not_measured_never_as_null(monkeypatch):
    """`null` means "we tried and could not say", which VETTING.md tells the
    agent to flag as a missing section. Reporting a deliberate omission that way
    would have it flag a non-problem on every monthly signal."""
    _candles(monkeypatch, _trend(60, 100, 1.0))
    r = history.attraction(None, 'X', 'monthly')
    assert r is not None, "a deliberate omission was reported as missing data"
    assert r['measured'] is False
    assert 'monthly' in r['why']


def test_weekly_is_still_measured(monkeypatch):
    """The other half — without it the gate could exclude everything."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 1.0)
    rows = []
    for _ in range(12):
        rows += _trend(6, 100, 2.0) + _trend(6, 112, -2.0)
    _candles(monkeypatch, rows)
    r = history.attraction(None, 'X', 'weekly')
    assert r is not None and r['measured'] is True
    assert r['overall']['episodes'] > 0


def test_the_agent_is_told_a_missing_monthly_section_is_deliberate():
    doc = (HELPER / 'zebra' / 'VETTING.md').read_text(encoding='utf-8')
    assert 'measured: false' in doc.lower(), \
        "the agent is never told what a deliberately absent section looks like"
    assert 'not a gap in the data' in doc.lower()


def test_the_agent_is_told_how_to_read_the_new_evidence():
    """Evidence the agent is never told to use is evidence it will not use.
    Both features exist to change a verdict, so both belong in its brief."""
    doc = (HELPER / 'zebra' / 'VETTING.md').read_text(encoding='utf-8')
    for key in ('st_attraction', 'touch_rate_pct', 'median_bars_to_touch',
                'swing_tp', 'applied', 'retained_pct', 'sample'):
        assert key in doc, f"VETTING.md never mentions {key}"
    # The two asymmetries that matter most.
    assert 'reason to veto' in doc, "a dead magnet must be stated as veto-worthy"
    assert 'not an all-clear' in doc, "a missing section must not read as a pass"


# ── velocity: the unit the OPTION lives in ───────────────────────────────
# Weekly bars cannot separate 18 sessions from 39 — both land in the same 4-8
# bar bucket, and on a 30-DTE structure those are opposite trades. Measured
# over 155 closed weekly trades, speed splits the `reliably magnetic` group in
# half: 62% wins / +26.5% median when fast, 53% / +2.9% when slow.

def _daily(monkeypatch, rows):
    """Populate the raw daily cache the velocity measure reads."""
    from playbook.magnet import scanner as mscan
    monkeypatch.setitem(mscan._raw_daily_cache, 'X', rows)


def test_velocity_is_reported_in_trading_days(monkeypatch):
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 1.0)
    rows = []
    for _ in range(12):
        rows += _trend(6, 100, 2.0) + _trend(6, 112, -2.0)
    _candles(monkeypatch, rows)
    _daily(monkeypatch, [dict(r, date='2020-01-%02d' % ((i % 28) + 1))
                         for i, r in enumerate(rows)])
    r = history.attraction(None, 'X', 'weekly')
    assert 'median_days_to_touch' in r
    assert r['day_horizon'] == cfg.ATTRACTION_HORIZON_DAYS


def test_room_is_the_option_clock_minus_what_the_symbol_needs(monkeypatch):
    """Arithmetic across two units — calendar DTE vs trading sessions — which
    is exactly the step a model quietly gets wrong if asked to do it."""
    base = {'measured': True, 'median_days_to_touch': 20.0}
    out = history._with_room(base, dte=42)          # 42 calendar ~ 30 sessions
    assert out['sessions_to_expiry'] == 30.0
    assert out['sessions_of_room'] == 10.0
    # the shape that loses money: the option dies before the stock arrives
    tight = history._with_room({'measured': True, 'median_days_to_touch': 39.0},
                               dte=21)
    assert tight['sessions_of_room'] < 0


def test_room_never_leaks_one_signals_expiry_into_the_cache(monkeypatch):
    """The cache is keyed by symbol and DAY; DTE belongs to the SIGNAL. Writing
    it into the cached dict would stamp the first caller's expiry onto every
    other signal on the same stock that day."""
    monkeypatch.setattr(cfg, 'WATCH_GAP_MAX', 1.0)
    rows = []
    for _ in range(12):
        rows += _trend(6, 100, 2.0) + _trend(6, 112, -2.0)
    _candles(monkeypatch, rows)
    _daily(monkeypatch, [dict(r, date='2020-01-%02d' % ((i % 28) + 1))
                         for i, r in enumerate(rows)])
    first = history.attraction(None, 'X', 'weekly', dte=42)
    second = history.attraction(None, 'X', 'weekly', dte=14)   # cached path
    if first.get('sessions_of_room') is not None:
        assert second['sessions_of_room'] != first['sessions_of_room'], \
            "the second signal inherited the first one's expiry"
    # and a caller with no DTE gets no room field rather than a stale one
    assert 'sessions_of_room' not in history.attraction(None, 'X', 'weekly')


def test_room_is_absent_rather_than_wrong_when_unmeasurable(monkeypatch):
    assert history._with_room(None, 30) is None
    unmeasured = {'measured': False, 'timeframe': 'monthly'}
    assert history._with_room(unmeasured, 30) == unmeasured
    no_days = {'measured': True, 'median_days_to_touch': None}
    assert 'sessions_of_room' not in history._with_room(no_days, 30)


def test_the_agent_is_warned_that_a_fast_rate_is_not_a_green_light():
    doc = (HELPER / 'zebra' / 'VETTING.md').read_text(encoding='utf-8')
    assert 'median_days_to_touch' in doc and 'sessions_of_room' in doc
    assert 'NOT a green light' in doc, \
        "the rate/speed trap is not stated to the agent"
    assert '+2.9%' in doc, "the measured split is not shown"


def test_velocity_is_wired_into_the_vet_context():
    """A measure the live path never passes DTE to reports no room at all."""
    src = (HELPER / 'zebra' / 'monitor.py').read_text(encoding='utf-8')
    assert "dte=analysis.get('dte')" in src, \
        "the entry path no longer hands the option clock to the magnet stat"


# ── 5. an unfinished episode is not a miss ───────────────────────────────
# 2026-08-17: HAVELLS was vetoed at 28.6% (2 of 7). The seventh episode had
# opened on 2026-08-03 and WAS the departure the signal was trying to trade —
# one week into an eight-week horizon, scored as a failure and then used as the
# reason not to take it. Read on its completed episodes it is 33.3% of 6.
#
# The bias only ever runs one way (down) and always lands on the newest
# episode, which is the one every live signal sits inside. Measured over the
# 210 F&O names: 37 carry one, population median 60.0% -> 62.5%.

def _bar(low, high, st=100.0, date='d'):
    return {'date': date, 'low': low, 'high': high, 'close': (low + high) / 2,
            'supertrend': st, 'direction': 'UP'}


#: Below ST and inside 3% — never opens an episode, never counts as a touch.
def _quiet(i):
    return _bar(98.5, 99.0, date='q%d' % i)


#: Below ST by 3.5–4.5% — the band the scanner actually signals on.
def _depart(i):
    return _bar(95.5, 96.5, date='x%d' % i)


#: Wick reaches the line. As a FORWARD bar this is the return; as an opener it
#: is ambiguous and skipped, which is why it never inflates the episode count.
def _touch(i):
    return _bar(99.0, 101.0, date='t%d' % i)


def _series(monkeypatch, bars):
    _candles(monkeypatch, _flat(len(bars) + 60))     # length check only
    import playbook.compute_st as cst
    monkeypatch.setattr(cst, 'compute_supertrend', lambda *a, **k: bars)


def _hit(i):
    """One episode that came back: departure, then the line."""
    return [_depart(i), _touch(i)]


def _miss(i):
    """One episode given its full horizon and still not back."""
    return [_depart(i)] + [_quiet(i * 100 + j)
                           for j in range(cfg.ATTRACTION_HORIZON_BARS)]


def _running(i, elapsed):
    """A departure with only `elapsed` bars behind it — not yet an outcome."""
    return [_depart(i)] + [_quiet(i * 100 + j) for j in range(elapsed)]


def test_an_unfinished_episode_is_not_scored_as_a_miss(monkeypatch):
    """THE HAVELLS SHAPE, rebuilt: two returns, four genuine misses, and one
    departure a week old. 2/6 = 33.3%, not 2/7 = 28.6%."""
    _band(monkeypatch)
    bars = _hit(1) + _hit(2) + _miss(3) + _miss(4) + _miss(5) + _miss(6) \
        + _running(7, elapsed=2)
    _series(monkeypatch, bars)
    r = history.attraction(None, 'HAVELLS', 'weekly')
    assert r is not None
    assert r['overall']['episodes'] == 6, \
        "the running departure is still in the denominator"
    assert r['overall']['touched'] == 2
    assert r['overall']['touch_rate_pct'] == 33.3


def test_the_running_departure_is_reported_not_silently_dropped(monkeypatch):
    """Dropping it from the RATE is correct; dropping it from the RECORD would
    hide that this symbol is mid-move, which is the one thing the signal being
    vetted most needs to know."""
    _band(monkeypatch)
    _series(monkeypatch, _hit(1) + _miss(2) + _miss(3) + _miss(4)
            + _running(5, elapsed=3))
    r = history.attraction(None, 'X', 'weekly')
    assert r['in_progress'] is not None, "the omission is invisible"
    assert r['in_progress']['episodes'] == 1
    assert r['in_progress']['bars_elapsed'] == 3
    assert r['in_progress']['of_horizon'] == cfg.ATTRACTION_HORIZON_BARS


def test_a_symbol_with_no_open_departure_says_so(monkeypatch):
    """The other side of the flag: `in_progress` must be None rather than a
    zero-filled dict, or every symbol reads as mid-move."""
    _band(monkeypatch)
    _series(monkeypatch, _hit(1) + _miss(2) + _miss(3) + _miss(4) + _miss(5))
    r = history.attraction(None, 'X', 'weekly')
    assert r['in_progress'] is None


def test_a_finished_miss_is_still_a_miss(monkeypatch):
    """The guard from the other side. Without it the fix above passes on a
    function that has simply stopped counting misses at all — which would make
    every symbol look magnetic and is a far worse failure than the one fixed."""
    _band(monkeypatch)
    _series(monkeypatch, _miss(1) + _miss(2) + _miss(3) + _miss(4) + _miss(5))
    r = history.attraction(None, 'X', 'weekly')
    assert r['overall']['episodes'] == 5
    assert r['overall']['touch_rate_pct'] == 0.0
    assert r['in_progress'] is None


def test_a_touch_at_the_very_edge_still_counts(monkeypatch):
    """An episode that came back with two bars of history behind it HAS an
    outcome — the touch happened. Only the ones with no outcome are excluded,
    and confusing the two would quietly delete the most recent winners."""
    _band(monkeypatch)
    _series(monkeypatch, _miss(1) + _miss(2) + _miss(3) + _miss(4) + _hit(9))
    r = history.attraction(None, 'X', 'weekly')
    assert r['overall']['episodes'] == 5, "a resolved touch was dropped as unfinished"
    assert r['overall']['touched'] == 1
    assert r['in_progress'] is None


def test_the_same_direction_split_uses_the_resolved_set_too(monkeypatch):
    """`same_direction` is what the agent quotes for a CE or PE signal. If it
    re-admits the running episode the headline is fixed and the number actually
    cited is not — HAVELLS' CE side was the 1-of-4 the veto leaned on."""
    _band(monkeypatch)
    _series(monkeypatch, _hit(1) + _hit(2) + _miss(3) + _running(4, elapsed=1))
    r = history.attraction(None, 'X', 'weekly', 'CE')   # CE = below ST, rising
    assert r['same_direction']['episodes'] == 3
    assert r['same_direction']['touched'] == 2


def test_dropping_the_unfinished_one_can_make_the_sample_thin(monkeypatch):
    """An honest consequence, pinned so nobody 'fixes' it back. WAAREEENER
    reads 3 episodes today and 2 once the running one is excluded — below
    `attraction_min_episodes`, so it must stop claiming a usable rate rather
    than keep one it no longer has."""
    _band(monkeypatch)
    monkeypatch.setattr(cfg, 'ATTRACTION_MIN_EPISODES', 3)
    _series(monkeypatch, _hit(1) + _miss(2) + _running(3, elapsed=2))
    r = history.attraction(None, 'WAAREEENER', 'weekly')
    assert r['overall']['episodes'] == 2
    assert r['sample'] == 'thin'
    assert 'too few' in r['verdict']


def test_every_episode_unfinished_reports_a_rate_of_nothing(monkeypatch):
    """The degenerate case must not raise and must not invent 0%. A symbol
    whose only departure is still running has no rate — `thin` short-circuits
    the verdict before the None rate is ever compared."""
    _band(monkeypatch)
    _series(monkeypatch, _running(1, elapsed=2))
    r = history.attraction(None, 'X', 'weekly')
    assert r is not None
    assert r['overall']['episodes'] == 0
    assert r['overall']['touch_rate_pct'] is None
    assert r['sample'] == 'thin' and 'too few' in r['verdict']
    assert r['in_progress']['episodes'] == 1


def test_the_agent_is_told_what_in_progress_means():
    """A field the agent has never been shown is a field it will read as a
    miss, which is the bug it was added to fix."""
    doc = (HELPER / 'zebra' / 'VETTING.md').read_text(encoding='utf-8')
    assert 'in_progress' in doc, "the new field is not explained to the agent"

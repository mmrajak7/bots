"""The registered 100-trade gate as `golive` now computes it (P5).

`docs/NAKED_100_TRADE_TEST.md` §4: the gap over the replay in standard
errors, the two regime conditions, the 100 -> 150 -> bad-weeks verdict path,
and the replay-history cache. Synthetic rows only -- nothing here touches the
network, the real candle cache or the real log dir.

Mostly about the boundaries the registration names (exactly 2 SE, exactly
p75, exactly 40 closes, a cell of fewer than 3 replay trades) and about the
one thing a gate must never do: turn "cannot be read" into PASS or FAIL.
"""
import json
from datetime import datetime, timedelta

import pytest

from zebra import golive


# -- gap in standard errors ----------------------------------------------------

def test_todays_numbers_clear_the_gap():
    """Pi, 2026-11-27: live +28.2 SE 7.2 vs replay +7.4 SE 3.6."""
    g = golive.gap_test(28.2, 7.2, 7.4, 3.6)
    assert g['gap'] == pytest.approx(20.8)
    assert g['need'] == pytest.approx(16.1, abs=0.05)
    assert g['met'] is True


def test_exactly_two_standard_errors_is_met_and_a_hair_less_is_not():
    # SE_gap = sqrt(3^2 + 4^2) = 5, need 10.
    assert golive.gap_test(10.0, 3.0, 0.0, 4.0)['met'] is True
    assert golive.gap_test(9.99, 3.0, 0.0, 4.0)['met'] is False


def test_a_missing_se_is_unknown_never_the_old_mean_rule():
    g = golive.gap_test(30.0, 5.0, 0.0, None)
    assert g['gap'] == 30.0 and g['met'] is None
    assert golive.gap_test(None, 1.0, 0.0, 1.0)['met'] is None


def test_selection_reports_both_standard_errors():
    t0 = datetime(2026, 9, 1, 9, 30)
    tests = [{'id': str(k), 'start': t0 + timedelta(hours=k), 'dir': 'CE', 'net_pct': v}
             for k, v in enumerate((10.0, 30.0))]
    s = golive.selection(tests, [{'net_pct': -10.0}, {'net_pct': 10.0}, {'net_pct': 0.0}])
    assert s['live_se'] == pytest.approx(10.0)          # sd 14.14 / sqrt 2
    assert s['replay_se'] == pytest.approx(10.0 / 3 ** 0.5)


# -- regime condition 1 ----------------------------------------------------------

def _nifty(closes):
    """{date string: close} -> candle list."""
    return [{'date': d, 'close': c} for d, c in sorted(closes.items())]


def test_a_week_is_up_when_its_last_close_beats_the_previous_weeks():
    w = golive.nifty_weeks(_nifty({'2026-09-04': 100.0, '2026-09-11': 101.0,
                                   '2026-09-18': 99.0, '2026-09-25': 99.0}))
    assert golive.nifty_week_up(w, '2026-09-09') is True
    assert golive.nifty_week_up(w, '2026-09-15') is False
    assert golive.nifty_week_up(w, '2026-09-22') is False        # flat is not up


def test_a_friday_holiday_reads_the_last_session_of_the_week():
    w = golive.nifty_weeks(_nifty({'2026-09-04': 100.0, '2026-09-10': 102.0,
                                   '2026-09-14': 90.0}))           # no 09-11 candle
    assert golive.nifty_week_up(w, '2026-09-08') is True


def test_a_week_the_series_has_not_finished_is_unknown():
    """A Wednesday candle must not stand in for Friday's close."""
    w = golive.nifty_weeks(_nifty({'2026-09-04': 100.0, '2026-09-09': 105.0}))
    assert golive.nifty_week_up(w, '2026-09-08') is None
    assert golive.nifty_week_up(golive.nifty_weeks(_nifty({'2026-09-11': 1.0})), '2026-09-08') is None
    assert golive.nifty_week_up(golive.nifty_weeks(None), '2026-09-08') is None


def _rows(n, side, day='2026-09-08'):
    t0 = datetime.strptime(day, '%Y-%m-%d').replace(hour=10)
    return [{'id': '%s%d' % (side, k), 'start': t0 + timedelta(minutes=k), 'dir': side,
             'net_pct': 1.0} for k in range(n)]


def test_regime_one_is_met_by_25_ce_alone_with_no_nifty_series():
    r = golive.regime_one(_rows(25, 'CE') + _rows(70, 'PE'), {})
    assert (r['ce'], r['unknown'], r['met']) == (25, 95, True)


def test_regime_one_is_unknown_while_the_missing_weeks_could_decide_it():
    r = golive.regime_one(_rows(24, 'CE') + _rows(70, 'PE'), {})
    assert r['met'] is None


def test_regime_one_is_not_met_when_no_reading_could_reach_25():
    w = golive.nifty_weeks(_nifty({'2026-09-04': 100.0, '2026-09-11': 90.0}))
    r = golive.regime_one(_rows(24, 'CE') + _rows(70, 'PE'), w)
    assert (r['up'], r['down'], r['unknown'], r['met']) == (0, 94, 0, False)


def test_regime_one_is_met_by_25_up_week_entries():
    w = golive.nifty_weeks(_nifty({'2026-09-04': 100.0, '2026-09-11': 110.0}))
    assert golive.regime_one(_rows(25, 'PE'), w)['met'] is True
    assert golive.regime_one(_rows(24, 'PE'), w)['met'] is False


# -- regime condition 2 ----------------------------------------------------------

def test_quantile_is_linear_interpolation():
    assert golive.quantile([1.0, 2.0, 3.0, 4.0, 5.0], 0.75) == pytest.approx(4.0)
    assert golive.quantile([0.0, 10.0], 0.75) == pytest.approx(7.5)
    assert golive.quantile([], 0.75) is None


def _hist_days(day_means, start='2026-01-01'):
    """One closed replay trade per day with the given net."""
    d0 = datetime.strptime(start, '%Y-%m-%d')
    return [{'entry_date': (d0 + timedelta(days=k)).strftime('%Y-%m-%d'), 'dir': 'CE',
             'net_pct': v} for k, v in enumerate(day_means)]


def test_regime_two_windows_are_runs_of_L_entry_days_before_the_test():
    # 5 prior days (1..5), then the test's 2 days (4, 4): L = 2, windows
    # (1,2) (2,3) (3,4) (4,5) -> means 1.5 2.5 3.5 4.5, p75 = 3.75.
    h = _hist_days([1, 2, 3, 4, 5, 4, 4])
    r = golive.regime_two(h, '2026-01-06', '2026-01-07')
    assert (r['days'], r['windows'], r['current']) == (2, 4, 4.0)
    assert r['p75'] == pytest.approx(3.75) and r['met'] is False
    assert r['rank'] == pytest.approx(75.0)


def test_regime_two_exactly_at_p75_is_met():
    h = _hist_days([1, 2, 3, 4, 5, 3.75, 3.75])
    assert golive.regime_two(h, '2026-01-06', '2026-01-07')['met'] is True


def test_regime_two_weights_the_window_by_trades_and_ignores_open_ones():
    h = _hist_days([0, 0, 0]) + [
        {'entry_date': '2026-01-04', 'dir': 'CE', 'net_pct': 10.0},
        {'entry_date': '2026-01-04', 'dir': 'PE', 'net_pct': -20.0},
        {'entry_date': '2026-01-04', 'dir': 'PE', 'net_pct': None}]
    r = golive.regime_two(h, '2026-01-04', '2026-01-04')
    assert r['current'] == pytest.approx(-5.0) and r['trades'] == 2


def test_regime_two_without_enough_history_is_unknown():
    h = _hist_days([1, 2, 3])
    assert golive.regime_two(h, '2026-01-02', '2026-01-03')['met'] is None
    assert golive.regime_two([], '2026-01-02', '2026-01-03')['met'] is None


# -- verdict path ----------------------------------------------------------------

def _prow(k, net_pct, resolved=True):
    start = datetime(2026, 9, 1, 9, 30) + timedelta(hours=k)
    return {'id': str(k), 'start': start, 'end': start + timedelta(days=2), 'capital': 100.0,
            'resolved': resolved, 'priced': resolved, 'partial': False, 'dir': 'CE',
            'pct': net_pct, 'net': net_pct, 'gross': net_pct, 'fees': 0.0, 'stress': 0.0, 'ctx': {}}


def _book(mean, n=100, start=0):
    return [_prow(start + k, mean + (20.0 if k % 2 else -20.0)) for k in range(n)]


def _sel(mean, se=0.0):
    return lambda tests: {'replay': mean, 'open': 0, 'n': 300, 'replay_se': se}


def _reg(one, two):
    return lambda tests: {'one': {'met': one}, 'two': {'met': two}}


def test_returns_and_gap_met_but_regime_two_unmet_EXTENDS_at_100():
    """Today's path: +28% clears every return bar, the window does not."""
    v = golive.verdict(_book(28.0), _sel(7.0), _reg(True, False))
    assert v['state'] == 'EXTENDED -> IN PROGRESS'
    assert 'regime 2' in v['why']


def test_an_unreadable_condition_that_decides_100_is_BLOCKED():
    assert golive.verdict(_book(28.0), _sel(7.0, None), _reg(True, True))['state'] == 'BLOCKED'
    assert golive.verdict(_book(28.0), _sel(7.0), _reg(None, True))['state'] == 'BLOCKED'
    assert golive.verdict(_book(28.0), _sel(7.0), None)['state'] == 'BLOCKED'


def test_a_definite_miss_extends_even_when_another_condition_is_unknown():
    v = golive.verdict(_book(28.0), _sel(7.0), _reg(None, False))
    assert v['state'].startswith('EXTENDED')


def test_fail_below_5_needs_no_regime_reading():
    assert golive.verdict(_book(3.0), _sel(0.0), None)['state'] == 'FAIL'


def test_a_gap_short_of_two_se_extends_at_100():
    # live SE ~2.0; replay +10, SE 5 -> need ~10.8, gap 5.
    v = golive.verdict(_book(15.0), _sel(10.0, 5.0), _reg(True, True))
    assert v['state'].startswith('EXTENDED') and 'gap' in v['why']


def test_at_150_regime_unmet_is_INCONCLUSIVE_and_opens_the_bad_weeks_stage():
    rows = _book(20.0, 150)
    seen = {}

    def bad(pop):
        seen['n'] = len(pop)
        return {'state': 'WAITING', 'why': 'x'}
    v = golive.verdict(rows + [_prow(200, None, resolved=False)], _sel(0.0), _reg(True, False), bad)
    assert v['state'] == 'INCONCLUSIVE -> BAD WEEKS WAITING' and v['stage'] == 'bad_weeks'
    assert seen['n'] == 151                       # the population continues past 150


def test_the_bad_weeks_stage_ends_in_pass_or_fail():
    rows = _book(20.0, 150)
    assert golive.verdict(rows, _sel(0.0), _reg(True, False),
                          lambda p: {'state': 'PASS', 'why': 'ok'})['state'] == 'PASS'
    assert golive.verdict(rows, _sel(0.0), _reg(False, False),
                          lambda p: {'state': 'FAIL', 'why': 'no'})['state'] == 'FAIL'
    assert golive.verdict(rows, _sel(0.0), _reg(True, False), None)['state'] == \
        'INCONCLUSIVE -> BAD WEEKS BLOCKED'


def test_at_150_all_met_passes_and_return_misses_fail():
    assert golive.verdict(_book(20.0, 150), _sel(0.0), _reg(True, True))['state'] == 'PASS'
    assert golive.verdict(_book(9.0, 150), _sel(0.0), _reg(True, True))['state'] == 'FAIL'
    assert golive.verdict(_book(20.0, 150), _sel(19.0, 5.0), _reg(True, True))['state'] == 'FAIL'
    carried = [_prow(k, 2000.0) for k in range(3)] + [_prow(k, -5.0) for k in range(3, 150)]
    assert golive.verdict(carried, _sel(-50.0), _reg(True, True))['state'] == 'FAIL'


def test_at_150_an_unknown_regime_is_BLOCKED_not_inconclusive():
    v = golive.verdict(_book(20.0, 150), _sel(0.0), _reg(None, True))
    assert v['state'] == 'BLOCKED'


def test_a_crashing_regime_reader_is_unknown_never_a_crash():
    def boom(tests):
        raise RuntimeError('bad file')
    assert golive.verdict(_book(28.0), _sel(7.0), boom)['state'] == 'BLOCKED'


# -- bad-weeks cells -------------------------------------------------------------

W = '2026-09-07'          # the test week (Monday)
H = '2026-08-03'          # a historical week


def _hist(test_ce=-10.0, test_pe=10.0, hist_ce=-5.0, hist_pe=5.0, opn=()):
    rows = []
    for d, side, v in ((W, 'CE', test_ce), (W, 'PE', test_pe), (H, 'CE', hist_ce), (H, 'PE', hist_pe)):
        rows += [{'entry_date': d, 'dir': side, 'net_pct': v}] * 3
    # A historical cell with only two trades: unclassified, counted nowhere.
    rows += [{'entry_date': '2026-08-10', 'dir': 'CE', 'net_pct': -50.0}] * 2
    return {'rows': rows, 'open': list(opn)}


def _pop(n_ce, ce_net, n_pe=10, pe_net=20.0, open_last=False, extra=()):
    t0 = datetime.strptime(W, '%Y-%m-%d').replace(hour=9, minute=30)
    out = [{'id': 'p%d' % k, 'start': t0 + timedelta(minutes=k), 'dir': 'PE', 'net_pct': pe_net}
           for k in range(n_pe)]
    out += [{'id': 'c%d' % k, 'start': t0 + timedelta(minutes=n_pe + k), 'dir': 'CE', 'net_pct': ce_net}
            for k in range(n_ce)]
    if open_last:
        out[-1] = dict(out[-1], net_pct=None)
    return out + list(extra)


def test_a_cell_needs_three_closed_replay_trades_and_zero_is_not_negative():
    rows = ([{'entry_date': W, 'dir': 'CE', 'net_pct': -1.0}] * 2
            + [{'entry_date': W, 'dir': 'PE', 'net_pct': 0.0}] * 3
            + [{'entry_date': H, 'dir': 'CE', 'net_pct': -1.0}] * 3)
    c = golive.classify_cells(rows)
    assert golive.cell_of(W, 'CE') not in c
    assert c[golive.cell_of(W, 'PE')]['neg'] is False
    assert c[golive.cell_of(H, 'CE')]['neg'] is True


def test_the_historical_share_counts_classified_trades_before_the_test_only():
    h = _hist()
    cells = golive.classify_cells(h['rows'])
    assert golive.historical_neg_share(h['rows'], cells, W) == pytest.approx(0.5)


def test_bad_weeks_passes_at_exactly_40_closed():
    # gap = -5 - (-10) = +5; mix = .5 x -5 + .5 x 20 = +7.5.
    b = golive.bad_weeks(_pop(40, -5.0), _hist(), min_n=0)
    assert b['state'] == 'PASS', b['why']
    assert (b['n_neg_closed'], b['prefix']) == (40, 50)
    assert b['gap'] == pytest.approx(5.0) and b['mix'] == pytest.approx(7.5)


def test_bad_weeks_waits_below_40_and_while_the_40th_is_open():
    assert golive.bad_weeks(_pop(39, -5.0), _hist(), min_n=0)['state'] == 'IN PROGRESS'
    assert golive.bad_weeks(_pop(40, -5.0, open_last=True), _hist(), min_n=0)['state'] == 'WAITING'


def test_the_prefix_is_fixed_by_entry_so_later_closes_cannot_stand_in():
    """The 40th bad-cell trade is open; ten LATER bad-cell closes exist. The
    stage still waits -- they entered after the prefix."""
    later = _pop(10, -5.0, n_pe=0)
    for k, r in enumerate(later):
        r['start'] = r['start'] + timedelta(hours=5)
        r['id'] = 'late%d' % k
    b = golive.bad_weeks(_pop(40, -5.0, open_last=True) + later, _hist(), min_n=0)
    assert b['state'] == 'WAITING' and b['prefix'] == 50


def test_the_prefix_is_never_shorter_than_the_first_150():
    b = golive.bad_weeks(_pop(40, -5.0), _hist(), min_n=60)
    assert b['prefix'] == 50                # only 50 exist: all of them
    b = golive.bad_weeks(_pop(40, -5.0, extra=[{'id': 'x', 'start': datetime(2026, 9, 20, 10),
                                                'dir': 'CE', 'net_pct': None}]), _hist(), min_n=51)
    assert b['state'] == 'WAITING' and b['prefix'] == 51


def test_bad_weeks_fails_on_a_negative_gap_or_a_non_positive_mix():
    b = golive.bad_weeks(_pop(40, -15.0), _hist(), min_n=0)      # gap -5
    assert b['state'] == 'FAIL' and 'selection gap' in b['why']
    b = golive.bad_weeks(_pop(40, -9.0, pe_net=5.0), _hist(), min_n=0)   # gap +1, mix -2
    assert b['state'] == 'FAIL' and 'mix-reweighted' in b['why']


def test_a_mix_with_no_good_cell_trade_is_stated_and_fails():
    b = golive.bad_weeks(_pop(40, -5.0, n_pe=0), _hist(), min_n=0)
    assert b['state'] == 'FAIL' and 'undefined' in b['why']


def test_unclassified_cells_count_nowhere():
    stray = [{'id': 'u%d' % k, 'start': datetime(2026, 9, 3, 10, k), 'dir': 'CE', 'net_pct': -90.0}
             for k in range(5)]                  # week 2026-36: no replay cell
    b = golive.bad_weeks(stray + _pop(40, -5.0), _hist(), min_n=0)
    assert b['state'] == 'PASS' and b['unclassified'] == 5
    assert b['mean_pos'] == pytest.approx(20.0)


def test_an_open_replay_trade_in_a_prefix_cell_waits():
    b = golive.bad_weeks(_pop(40, -5.0), _hist(opn=[{'entry_date': W, 'dir': 'CE'}]), min_n=0)
    assert b['state'] == 'WAITING'


def test_a_candle_cache_short_of_the_last_prefix_weeks_friday_is_blocked():
    """A cell read on Monday's replay alone can flip by Friday."""
    for end in ('2026-09-04', '2026-09-07', '2026-09-10'):
        h = dict(_hist(), cache_end=end)
        assert golive.bad_weeks(_pop(40, -5.0), h, min_n=0)['state'] == 'BLOCKED', end
    assert golive.bad_weeks(_pop(40, -5.0), dict(_hist(), cache_end='2026-09-11'),
                            min_n=0)['state'] == 'PASS'


def test_regime_item_three_is_reported_by_side_and_nifty_week():
    w = golive.nifty_weeks(_nifty({'2026-09-04': 100.0, '2026-09-11': 110.0}))
    rows = _rows(2, 'CE') + [dict(r, net_pct=-3.0) for r in _rows(1, 'PE')]
    got = {g[0]: g[1:] for g in golive.by_side_and_week(rows, w)}
    assert got['CE'][0] == 2 and got['PE'] == (1, -3.0, 0.0)
    assert got['NIFTY up'][0] == 3 and 'NIFTY down' not in got


def test_no_replay_history_is_blocked():
    assert golive.bad_weeks(_pop(40, -5.0), None)['state'] == 'BLOCKED'


# -- the replay-history cache ------------------------------------------------------

def _runner(calls):
    def run(since, until):
        calls.append((since, until))
        return [{'entry_date': '2026-01-05', 'dir': 'CE', 'net_pct': 3.0, 'closes': [1, 2]},
                {'entry_date': '2026-01-06', 'dir': 'PE', 'net_pct': None, 'reason': 'open'}]
    return run


def test_the_history_is_computed_once_per_cache_end(tmp_path):
    calls, f = [], tmp_path / 'h.json'
    h = golive.replay_history('2026-11-27', 200, path=f, run=_runner(calls))
    assert h['rows'] == [{'entry_date': '2026-01-05', 'dir': 'CE', 'net_pct': 3.0}]
    assert len(h['open']) == 1 and h['cached'] is False and h['cache_end'] == '2026-11-27'
    assert calls == [(golive.REGIME_HISTORY_FROM, '2026-11-27')]
    assert golive.replay_history('2026-11-27', 200, path=f, run=_runner(calls))['cached'] is True
    assert len(calls) == 1
    golive.replay_history('2026-11-28', 200, path=f, run=_runner(calls))      # new candles
    golive.replay_history('2026-11-28', 201, path=f, run=_runner(calls))      # new universe
    assert len(calls) == 3


def test_a_corrupt_cache_is_rebuilt_and_an_unwritable_one_is_survived(tmp_path):
    """Reads a file written by the code under test.

    RETIRES WHEN: never as a source guard -- it reads a DATA file this code writes, not source;
    it retires with that file format.
    """
    calls, f = [], tmp_path / 'h.json'
    f.write_text('{not json', encoding='utf-8')
    assert golive.replay_history('2026-11-27', 1, path=f, run=_runner(calls))['rows']
    assert json.loads(f.read_text(encoding='utf-8'))['key']['cache_end'] == '2026-11-27'
    bad = tmp_path / 'missing_dir' / 'h.json'
    assert golive.replay_history('2026-11-27', 1, path=bad, run=_runner(calls))['rows']


def test_no_cache_end_runs_nothing(tmp_path):
    calls = []
    assert golive.replay_history(None, 1, path=tmp_path / 'h.json', run=_runner(calls)) is None
    assert calls == []


# -- the printed block ---------------------------------------------------------------

def test_the_pack_prints_the_gate_lines_and_survives_no_candles(monkeypatch):
    from zebra import structure_shadow as ss
    monkeypatch.setattr(ss, '_load', lambda path=None: {'schema': ss.SCHEMA, 'shadows': {}})
    monkeypatch.setattr(golive.veto_naked, 'load_state', lambda: {'shadows': {}})
    monkeypatch.setattr(golive.replay, 'universe', lambda: [])
    monkeypatch.setattr(golive.replay, 'load_daily', lambda s: None)
    monkeypatch.setattr(golive, '_paths_marks', lambda paths_dir=None: {})

    def no_history(*a, **k):
        raise AssertionError('must not run the replay without a candle cache')
    monkeypatch.setattr(golive, 'replay_history', no_history)

    class Store:
        def load_trades(self):
            return []
    out = golive.report(Store(), now=datetime(2026, 11, 27, 16, 0))
    head = out[:out.index('1 STRUCTURE')]
    for line in ('GAP (gated)', 'REGIME 1 (not one market', 'REGIME 2 (not a lucky window',
                 'BAD WEEKS (only after INCONCLUSIVE', 'NIFTY series'):
        assert line in head, line
    assert 'VERDICT: IN PROGRESS' in head

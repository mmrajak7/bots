"""The replay behind the 100-trade test's SELECTION and VETTING sections.

The replay is a yardstick, so what these pin is that it measures the RULES
the live engine runs -- not a nicer version of them: completed candles only,
the live bands, the live drift multiplier, a gap booked at the open rather
than at the stop, the pessimistic reading of a bar that touched both.
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from zebra import config as cfg
from zebra import replay


def _days(n, start=date(2027, 1, 4)):
    """n weekday dates from `start`."""
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _bars(closes, start=date(2027, 1, 4), wiggle=0.004):
    """Candles around each close; a small alternating move so realised vol is
    never zero."""
    out = []
    for k, (d, c) in enumerate(zip(_days(len(closes), start), closes)):
        c = c * (1 + (wiggle if k % 2 else -wiggle))
        out.append({'date': d, 'open': c, 'high': c * 1.002, 'low': c * 0.998, 'close': c})
    return out


# -- the signal ------------------------------------------------------------------

@pytest.fixture
def flat_st(monkeypatch):
    """ST fixed at 100 on the weekly timeframe only."""
    monkeypatch.setattr(replay, 'st_as_of', lambda daily, tf: [100.0] * len(daily))
    monkeypatch.setattr(cfg, 'ENABLED_TIMEFRAMES', ['weekly'])
    monkeypatch.setattr(cfg, 'ENABLED_DIRECTIONS', ['CE', 'PE'])


def _sig_bars(extra):
    """Six sessions sitting 4.5% under the line (a fresh CE watch), then
    `extra` as (open, high, low, close)."""
    bars = [{'date': d, 'open': 95.5, 'high': 95.6, 'low': 95.4, 'close': 95.5}
            for d in _days(6)]
    for d, (o, h, l, c) in zip(_days(6 + len(extra))[6:], extra):
        bars.append({'date': d, 'open': o, 'high': h, 'low': l, 'close': c})
    return bars


def test_a_trade_to_the_trigger_level_fills_there(flat_st):
    s = replay.signals('X', _sig_bars([(95.5, 96.3, 95.4, 96.1)]), '2000-01-01', '2100-01-01')
    assert len(s) == 1 and s[0]['dir'] == 'CE' and s[0]['entry'] == pytest.approx(96.0)
    assert s[0]['target'] == 100.0


def test_an_open_already_inside_the_band_fills_at_the_open(flat_st):
    s = replay.signals('X', _sig_bars([(96.4, 96.8, 96.2, 96.5)]), '2000-01-01', '2100-01-01')
    assert s[0]['entry'] == pytest.approx(96.4)


def test_an_open_past_the_stale_floor_is_skipped(flat_st):
    """Opening 2.5% from the line is past stale_gap_min: too late, as live."""
    assert replay.signals('X', _sig_bars([(97.5, 98.0, 97.4, 97.8)]), '2000-01-01', '2100-01-01') == []


def test_a_drift_past_the_live_multiple_cancels_the_watch(flat_st):
    """7% away is past watch_gap_max x 1.2 = 6%: the watch dies, so a later
    return to the band is a NEW approach that must re-qualify."""
    s = replay.signals('X', _sig_bars([(93.0, 93.1, 92.9, 93.0), (95.9, 96.3, 95.8, 96.1)]),
                       '2000-01-01', '2100-01-01')
    assert s == []


def test_a_recent_touch_of_the_line_is_not_fresh(flat_st):
    """A touch inside the freshness window stops a watch from FORMING, so the
    later trade to the trigger level is not a signal."""
    bars = _sig_bars([(95.5, 96.3, 95.4, 96.1)])
    bars[1]['high'] = 99.5                  # within 1% of the line, before any watch formed
    assert replay.signals('X', bars, '2000-01-01', '2100-01-01') == []
    bars[1]['high'] = 95.6                  # control: the same bars without the touch
    assert len(replay.signals('X', bars, '2000-01-01', '2100-01-01')) == 1


def test_the_drift_multiple_is_the_live_monitors():
    """DRIFT_MULT is a copy of a literal in monitor.py; if that literal moves,
    this fails rather than the replay quietly measuring a different rule.

    RETIRES WHEN: the drift multiple becomes a config value that both
    monitor.py and replay.py read, so there is one definition to pin."""
    src = (Path(replay.__file__).parent / 'monitor.py').read_text(encoding='utf-8')
    assert 'gap > cfg.WATCH_GAP_MAX * %s' % replay.DRIFT_MULT in src


def test_st_uses_only_COMPLETED_periods():
    """A day inside month k sees month k-1's ST, never month k's forming one."""
    from playbook.compute_st import compute_supertrend
    closes, d, k = [], date(2025, 1, 1), 0
    bars = []
    while d < date(2026, 6, 30):
        if d.weekday() < 5:
            c = 100 + 10 * ((k // 15) % 2) + (k % 7)
            bars.append({'date': d.isoformat(), 'open': c, 'high': c + 2, 'low': c - 2, 'close': c})
            k += 1
        d += timedelta(days=1)
    st = replay.st_as_of(bars, 'monthly')
    upto_may = [b for b in bars if b['date'] < '2026-06-01']
    months = {}
    for b in upto_may:
        m = b['date'][:7]
        p = months.setdefault(m, dict(b))
        p['high'], p['low'], p['close'] = max(p['high'], b['high']), min(p['low'], b['low']), b['close']
    expect = compute_supertrend(list(months.values()), cfg.ST_PERIOD, cfg.ST_MULTIPLIER)[-1]['supertrend']
    june = [i for i, b in enumerate(bars) if b['date'][:7] == '2026-06']
    assert all(st[i] == pytest.approx(expect) for i in june)


# -- expiry ----------------------------------------------------------------------

def test_expiry_moved_from_thursday_to_tuesday_in_september_2025(monkeypatch):
    monkeypatch.setattr(replay, '_is_session', lambda d: d.weekday() < 5)
    assert replay.monthly_expiry(2025, 8) == date(2025, 8, 28)      # last Thursday
    assert replay.monthly_expiry(2025, 9) == date(2025, 9, 30)      # last Tuesday


def test_an_expiry_on_a_holiday_moves_back_to_the_session_before(monkeypatch):
    monkeypatch.setattr(replay, '_is_session', lambda d: d.weekday() < 5 and d != date(2026, 10, 27))
    assert replay.monthly_expiry(2026, 10) == date(2026, 10, 26)


def test_the_expiry_is_the_first_inside_the_live_dte_window(monkeypatch):
    monkeypatch.setattr(replay, '_is_session', lambda d: d.weekday() < 5)
    e = replay.expiry_for(date(2026, 9, 20))
    assert cfg.MIN_DTE <= (e - date(2026, 9, 20)).days <= cfg.MAX_DTE
    assert e == date(2026, 10, 27)                   # Sep 29 is only 9 days out


# -- the trade -------------------------------------------------------------------

@pytest.fixture
def sessions(monkeypatch):
    monkeypatch.setattr(replay, '_is_session', lambda d: d.weekday() < 5)


def _trade_bars(after):
    """25 quiet sessions at 96 (so realised vol exists), then `after` bars."""
    bars = _bars([96.0] * 25)
    for d, (o, h, l, c) in zip(_days(25 + len(after))[25:], after):
        bars.append({'date': d, 'open': o, 'high': h, 'low': l, 'close': c})
    return bars


EXP = date(2027, 3, 30)


def test_the_target_books_a_win(sessions):
    bars = _trade_bars([(96.2, 100.5, 96.0, 100.2)])
    r = replay.simulate(bars, 24, 'CE', 96.0, 100.0, EXP)
    assert r['reason'] == 'tp' and r['gross_pct'] > 0
    assert r['net_pct'] == pytest.approx(r['gross_pct'] - replay.COST_PCT)


def test_the_stop_books_AT_the_stop(sessions):
    bars = _trade_bars([(95.8, 96.0, 88.0, 89.0)])
    r = replay.simulate(bars, 24, 'CE', 96.0, 100.0, EXP)
    assert r['reason'] == 'stop' and r['gross_pct'] == pytest.approx(-100 * cfg.DEBIT_SL_PCT)


def test_a_gap_through_the_stop_books_at_the_open_not_the_level(sessions):
    bars = _trade_bars([(85.0, 86.0, 84.0, 85.5)])
    r = replay.simulate(bars, 24, 'CE', 96.0, 100.0, EXP)
    assert r['reason'] == 'stop_gap' and r['gross_pct'] < -100 * cfg.DEBIT_SL_PCT


def test_a_bar_touching_both_is_read_as_the_stop(sessions):
    bars = _trade_bars([(96.0, 101.0, 88.0, 95.0)])
    assert replay.simulate(bars, 24, 'CE', 96.0, 100.0, EXP)['reason'] == 'stop'


def test_time_books_the_close_before_expiry(sessions):
    bars = _trade_bars([(96.0, 96.3, 95.8, 96.0)] * 40)
    exp = date.fromisoformat(bars[40]['date'])
    r = replay.simulate(bars, 24, 'CE', 96.0, 100.0, exp)
    assert r['reason'] == 'time' and r['exit_date'] == replay.time_exit_date(exp).isoformat()


def test_an_unresolved_trade_is_open_not_scored(sessions):
    r = replay.simulate(_trade_bars([(96.0, 96.3, 95.8, 96.0)]), 24, 'CE', 96.0, 100.0, EXP)
    assert r['reason'] == 'open' and 'net_pct' not in r


def test_a_put_mirrors_a_call(sessions):
    bars = _trade_bars([(95.8, 96.1, 91.5, 91.8)])
    r = replay.simulate(bars, 24, 'PE', 96.0, 92.0, EXP)
    assert r['reason'] == 'tp' and r['gross_pct'] > 0


def test_one_open_position_per_stock(monkeypatch):
    monkeypatch.setattr(replay, 'load_daily', lambda s: _bars([96.0] * 80))
    monkeypatch.setattr(replay, 'signals', lambda s, d, a, b: [
        {'sym': s, 'tf': 'weekly', 'dir': 'CE', 'i': 30, 'date': d[30]['date'], 'entry': 96, 'target': 100},
        {'sym': s, 'tf': 'monthly', 'dir': 'CE', 'i': 32, 'date': d[32]['date'], 'entry': 96, 'target': 100}])
    monkeypatch.setattr(replay, 'simulate', lambda *a, **k: {'exit_date': '2099-01-01', 'reason': 'open'})
    assert len(replay.replay_window('2000-01-01', '2100-01-01', [('X', 1)])) == 1


# -- the cache refresh -------------------------------------------------------------

class _Kite:
    def __init__(self, bars, fail=()):
        self.bars, self.fail = bars, set(fail)

    def historical_data(self, token, start, end, interval):
        if token in self.fail:
            raise RuntimeError('Too many requests')
        return self.bars


def test_refresh_merges_keeps_old_history_and_never_writes_today(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, '_cache_dir', lambda: tmp_path)
    old = [{'date': '2020-01-%02d' % d, 'open': 1, 'high': 1, 'low': 1, 'close': 1} for d in range(1, 29)]
    (tmp_path / 'X.json').write_text(json.dumps(old))
    from datetime import datetime
    today = datetime.now().strftime('%Y-%m-%d')
    fresh = [{'date': date(2020, 1, 28), 'open': 2, 'high': 2, 'low': 2, 'close': 2},
             {'date': date.fromisoformat(today), 'open': 3, 'high': 3, 'low': 3, 'close': 3}]
    res = replay.refresh(_Kite(fresh, fail={2}), [('X', 1), ('Y', 2)], pause=0)
    assert res == {'updated': 1, 'failed': ['Y']}
    got = {c['date']: c['close'] for c in json.load(open(tmp_path / 'X.json'))}
    assert got['2020-01-01'] == 1            # old history kept
    assert got['2020-01-28'] == 2            # the fetched candle wins the overlap
    assert today not in got                  # the forming bar is never cached

"""Shadow measurements from the Fable enhancement review (2026-11-19).

Entry tags, the session-mark IV, the veto category / results-IV fields and the
week-clustered statistics in `golive`. All descriptive: the tests are mostly
about what they must NOT do -- look ahead, read a stale series as current,
re-stamp a closed row every cycle, or change the pre-registered gate.
"""
import ast
from datetime import date, datetime
from pathlib import Path

import pytest

from zebra import entry_tags as et
from zebra import golive
from zebra import structure_shadow as ss
from zebra import veto_naked as vn


def _daily(n=40, start=100.0, step=1.0, last='2026-11-13'):
    """n candles, one per weekday, ending on `last`, closes rising by `step`."""
    days, d = [], date.fromisoformat(last)
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d = date.fromordinal(d.toordinal() - 1)
    days.reverse()
    out = []
    for k, dd in enumerate(days):
        c = start + step * k
        out.append({'date': dd.isoformat(), 'open': c, 'high': c + 1.0, 'low': c - 1.0, 'close': c})
    return out


# -- compute -----------------------------------------------------------------

def test_tail5_points_toward_the_line_for_both_sides():
    dl = _daily()
    spot = dl[-1]['close'] + 1.0                   # entry the next session
    ce = et.compute('CE', spot, spot * 1.04, '2026-11-16', 2.0, 25.0, dl)
    pe = et.compute('PE', spot, spot * 0.96, '2026-11-16', 2.0, 25.0, dl)
    up = 100.0 * (spot / dl[-5]['close'] - 1)
    assert ce['tail5_pct'] == pytest.approx(up, abs=1e-3)       # a rise is TOWARD a CE line
    assert pe['tail5_pct'] == pytest.approx(-up, abs=1e-3)      # and AWAY from a PE line


def test_no_candle_from_the_entry_session_or_later_is_read():
    """The same number whether stamped at entry or derived later: the entry
    day's own candle (and anything after) must not move a tag."""
    dl = _daily(last='2026-11-13')
    before = et.compute('CE', 140.0, 146.0, '2026-11-16', 2.0, 25.0, dl)
    later = dl + [{'date': '2026-11-16', 'open': 1.0, 'high': 999.0, 'low': 1.0, 'close': 999.0},
                  {'date': '2026-11-17', 'open': 1.0, 'high': 999.0, 'low': 1.0, 'close': 999.0}]
    after = et.compute('CE', 140.0, 146.0, '2026-11-16', 2.0, 25.0, later)
    assert before == after


def test_gap_over_premium_is_the_distance_in_premiums():
    t = et.compute('CE', 100.0, 104.0, '2026-11-16', 2.0, None, _daily(last='2026-11-13'))
    assert t['gap_pct'] == pytest.approx(100 * 4 / 104, abs=1e-3)
    assert t['gap_over_premium'] == pytest.approx(t['gap_pct'] / 2.0, abs=1e-3)
    assert t['iv_over_rv20'] is None                  # no IV, no ratio -- never a guess


def test_a_stale_series_is_refused_not_used():
    t = et.compute('CE', 100.0, 104.0, '2026-11-16', 2.0, 25.0, _daily(last='2026-11-05'))
    assert t['why'] == 'stale_candles'
    assert t['tail5_pct'] is None and t['atr_pct'] is None and t['rv20_pct'] is None
    assert t['gap_over_premium'] is not None          # needs no candles, so it stands


def test_missing_inputs_say_why_and_never_raise():
    assert et.compute('CE', None, 104.0, '2026-11-16')['why'] == 'no_spot_or_target'
    assert et.compute('XX', 100.0, 104.0, '2026-11-16')['why'] == 'no_spot_or_target'
    assert et.compute('CE', 100.0, 104.0, '2026-11-16', daily=None)['why'] == 'no_candles'
    assert et.compute('CE', 100.0, 104.0, '2020-01-01', daily=_daily())['why'] == 'no_candles_before_entry'
    assert et.compute('CE', 100.0, 104.0, 'garbage', daily=_daily())['why'] == 'bad_entry_day'


# -- stamp_missing -------------------------------------------------------------

def _sh(status='open', **ctx):
    return {'stock': 'ACME', 'direction': 'CE', 'entry_spot': 140.0, 'target_spot': 146.0,
            'entry_date': '2026-11-16', 'context': dict({'premium_pct_spot': 2.0, 'iv_long': 25.0}, **ctx),
            'arms': {'naked_long': {'status': status}}}


def test_stamping_is_once_and_idempotent(monkeypatch):
    reads = []
    monkeypatch.setattr(et.replay, 'load_daily', lambda s: reads.append(s) or _daily())
    shadows = {'1': _sh(), '2': _sh(status='exited')}
    assert et.stamp_missing(shadows) == 2
    first = dict(shadows['1']['context']['tags'])
    assert et.stamp_missing(shadows) == 0 and len(reads) == 2
    assert shadows['1']['context']['tags'] == first


def test_a_cache_miss_retries_once_a_day_open_or_closed(monkeypatch):
    """A refreshed cache fills a closed row as well as an open one, but a row
    still waiting costs one file read a DAY, not one per 5-minute cycle."""
    reads = []
    box = {'dl': _daily(last='2026-11-05')}
    monkeypatch.setattr(et.replay, 'load_daily', lambda s: reads.append(s) or box['dl'])
    shadows = {'open': _sh(), 'closed': _sh(status='exited')}
    assert et.stamp_missing(shadows, today='2026-11-16') == 2
    assert et.stamp_missing(shadows, today='2026-11-16') == 0 and len(reads) == 2   # same day: no reread
    box['dl'] = _daily(last='2026-11-13')
    assert et.stamp_missing(shadows, today='2026-11-17') == 2
    assert all(v['context']['tags']['why'] is None for v in shadows.values())
    assert et.stamp_missing(shadows, today='2026-11-18') == 0                       # final once filled


def test_a_series_missing_recent_sessions_is_stale_not_shifted():
    """The scanner accepts a cache up to 5 days old. A series ending the
    Wednesday before a Monday entry would make tail5 an 8-session move."""
    t = et.compute('CE', 140.0, 146.0, '2026-11-16', 2.0, 25.0, _daily(last='2026-11-11'))
    assert t['why'] == 'stale_candles' and t['tail5_pct'] is None


def test_the_entry_days_own_candle_proves_continuity():
    """With the entry candle present the series is continuous by construction,
    whatever the holiday calendar says (it has missed real closures)."""
    dl = _daily(last='2026-11-16')
    t = et.compute('CE', 140.0, 146.0, '2026-11-16', 2.0, 25.0, dl)
    assert t['why'] is None and t['candle_through'] == '2026-11-13'


def test_an_unreadable_cache_is_a_tag_not_a_crash(monkeypatch):
    def boom(s):
        raise OSError('disk')
    monkeypatch.setattr(et.replay, 'load_daily', boom)
    shadows = {'1': _sh()}
    et.stamp_missing(shadows)
    assert shadows['1']['context']['tags']['why'] == 'no_candles'


def test_nothing_decides_on_the_tags():
    """Measurement only: no module but the stamper and the readers may name a
    tag field. A rule reading one mid-test would break rule 3 of the plan.

    RETIRES WHEN: the 100/150-trade review adopts or rejects E2/E3/E4 and a
    rule is deliberately built on a tag -- then this guard is replaced by
    that rule's own behavioural tests."""
    root = Path(__file__).resolve().parents[1]
    allowed = {'entry_tags.py'}
    for p in root.glob('*.py'):
        if p.name in allowed:
            continue
        src = p.read_text(encoding='utf-8')
        for field in ('tail5_pct', 'gap_over_premium', 'iv_over_rv20', 'veto_category',
                      'iv_after_results'):
            if field in src:
                assert p.name in ('veto_naked.py',), '%s names %s' % (p.name, field)


# -- the session-close mark carries IV -----------------------------------------

def test_the_close_mark_carries_iv_off_the_mid():
    sh = {'direction': 'CE', 'long_strike': 100.0, 'expiry': '2026-12-29', 'polls': 0,
          'target_spot': 110.0, 'entry_spot': 100.0,
          'arms': {'naked_long': {'status': 'open', 'entry_value': 4.0, 'peak': 4.0, 'exit': None}}}
    lq = {'bid': 4.0, 'ask': 4.2, 'reliable': True}
    ss.poll_one(sh, 100.0, lq, None, datetime(2026, 11, 19, 15, 25), date(2026, 11, 19))
    m = sh['arms']['naked_long']['marks']['2026-11-19']
    assert m['value'] == 4.0                                    # still the bid
    assert m['iv'] == ss.mark_iv(sh, lq, 100.0, date(2026, 11, 19)) and m['iv'] > 0


def test_mark_iv_refuses_a_one_sided_book():
    sh = {'direction': 'CE', 'long_strike': 100.0, 'expiry': '2026-12-29'}
    assert ss.mark_iv(sh, {'bid': 0, 'ask': 4.2}, 100.0, date(2026, 11, 19)) is None
    assert ss.mark_iv(sh, None, 100.0, date(2026, 11, 19)) is None
    assert ss.mark_iv({}, {'bid': 4, 'ask': 4.2}, 100.0, date(2026, 11, 19)) is None


# -- veto category / results -----------------------------------------------------

@pytest.mark.parametrize('reasons,cat', [
    (['This is a long-premium bet held straight through an earnings print'], 'EVENT'),
    (['A bear put spread straight through the print, needs 3% in two weeks'], 'EVENT'),
    (['Too expensive to pay. The gates would suppress this entry'], 'SPREAD'),
    (['Best case +24% against a stop that books -50%'], 'SPREAD'),
    (['The clock is the problem: the option expires before this stock arrives'], 'PACE'),
    (['CGPOWER has already fallen from 855 to 799 in four sessions'], 'TREND'),
    (['The at-money put quotes 52% wide. Not tradeable.'], 'LIQ'),
    (['The stock has returned to this line on only 3 of its last 8 moves'], 'MAGNET'),
    (['Nothing here.'], 'OTHER'),
    ([], 'OTHER'),
])
def test_veto_category(reasons, cat):
    assert vn.veto_category(reasons) == cat


def test_iv_after_results_is_the_first_mark_after_the_print():
    sh = {'results_dates': ['2026-11-12'], 'iv_after_results': None,
          'arms': {vn.ARM: {'marks': {'2026-11-11': {'iv': 40.0}, '2026-11-12': {'iv': 39.0},
                                      '2026-11-13': {'iv': 28.0}, '2026-11-14': {'iv': 27.0}}}}}
    assert vn.stamp_results_iv(sh) is True
    assert sh['iv_after_results'] == {'date': '2026-11-13', 'iv': 28.0, 'results_date': '2026-11-12'}
    assert vn.stamp_results_iv(sh) is False                     # once


def test_no_results_no_stamp():
    sh = {'results_dates': [], 'arms': {vn.ARM: {'marks': {'2026-11-13': {'iv': 28.0}}}}}
    assert vn.stamp_results_iv(sh) is False and sh.get('iv_after_results') is None


def test_category_waits_for_an_unreadable_decision(monkeypatch):
    box = {}
    monkeypatch.setattr(vn, '_decision', lambda i: box)
    sh = {'decision_id': 9, 'veto_category': None}
    assert vn.stamp_category(sh) is False and sh['veto_category'] is None
    box['reasons'] = ['Earnings print inside the window', 'second', 'third']
    assert vn.stamp_category(sh) is True
    assert sh['veto_category'] == 'EVENT' and sh['veto_reasons'] == ['Earnings print inside the window', 'second']


def test_results_in_window_reads_only_this_stock_and_window(monkeypatch):
    from zebra import events
    monkeypatch.setattr(events, 'load', lambda: {'events': [
        {'type': 'results', 'symbol': 'ACME', 'date': '2026-11-20'},
        {'type': 'results', 'symbol': 'ACME', 'date': '2026-12-30'},      # after expiry
        {'type': 'results', 'symbol': 'OTHER', 'date': '2026-11-20'},
        {'type': 'rbi_policy', 'symbol': None, 'date': '2026-11-21'}]})
    assert vn.results_in_window('ACME', '2026-11-16', '2026-12-29') == ['2026-11-20']


# -- golive statistics -----------------------------------------------------------

def test_clustered_se_exceeds_naive_when_weeks_move_together():
    vals = [50.0] * 10 + [-50.0] * 10
    wk = ['A'] * 10 + ['B'] * 10
    c = golive.clustered(vals, wk)
    naive = golive._stats(vals)['se']
    assert c['g'] == 2 and c['se'] > 2 * naive


def test_clustered_se_matches_naive_shape_when_every_trade_is_its_own_week():
    vals = [10.0, -5.0, 20.0, 3.0, -8.0]
    c = golive.clustered(vals, list('abcde'))
    assert c['se'] == pytest.approx(golive._stats(vals)['se'], rel=0.15)


def test_within_week_side_takes_the_market_out():
    """A CE trade in a rally week is compared only with that week's CE signals."""
    tests = [{'start': datetime(2026, 11, 3, 10), 'dir': 'CE', 'net_pct': 60.0},
             {'start': datetime(2026, 11, 4, 10), 'dir': 'CE', 'net_pct': 40.0},
             {'start': datetime(2026, 10, 6, 10), 'dir': 'PE', 'net_pct': -40.0},
             {'start': datetime(2026, 10, 7, 10), 'dir': 'CE', 'net_pct': 5.0}]   # no CE cell that week
    replayed = [{'entry_date': '2026-11-02', 'dir': 'CE', 'net_pct': 50.0},
                {'entry_date': '2026-10-05', 'dir': 'PE', 'net_pct': -50.0},
                {'entry_date': '2026-10-05', 'dir': 'PE', 'net_pct': None}]
    w = golive.within_week_side(tests, replayed)
    assert w['n'] == 3 and w['dropped'] == 1
    assert w['mean'] == pytest.approx((10.0 - 10.0 + 10.0) / 3)


def test_the_gate_itself_is_unchanged():
    """The clustered SE is printed beside the gate, never inside it.

    RETIRES WHEN: the owner decides whether the gate should use the clustered
    SE (docs/NAKED_100_TRADE_TEST.md §4), or the test reaches its verdict."""
    src = ast.get_source_segment(Path(golive.__file__).read_text(encoding='utf-8'),
                                 next(n for n in ast.parse(Path(golive.__file__).read_text(encoding='utf-8')).body
                                      if isinstance(n, ast.FunctionDef) and n.name == 'verdict'))
    assert 'clustered' not in src and 'within' not in src

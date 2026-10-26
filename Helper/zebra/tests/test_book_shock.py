"""The nightly NIFTY-shock line (zebra/book_shock.py, design F25 / rollout 2).

What matters here is the direction and the honesty of the number: a rally
must cost the PE book, the totals must never read zero when they are really
missing, and a value is never paired with a spot from a different moment.
"""
import json

import pytest

from zebra import book_shock as bs
from zebra import ivcalc

DAY = '2027-03-10'


def _candles(closes, start=1):
    return [{'date': '2027-01-%02d' % (start + i) if i < 31 - start else
             '2027-02-%02d' % (i - (31 - start) + 1), 'close': c}
            for i, c in enumerate(closes)]


def _nifty_and_stock(k, n=40):
    """NIFTY alternating +1%/-0.5%; the stock moves exactly k times that."""
    nx, sx = [100.0], [100.0]
    for i in range(n):
        r = 0.01 if i % 2 == 0 else -0.005
        nx.append(nx[-1] * (1 + r))
        sx.append(sx[-1] * (1 + k * r))
    return _candles(nx), _candles(sx)


def _shadow(direction='PE', qty=500, strike=1000.0, marks=None, status='open',
            exit_at=None, entry_date='2027-03-01', iv_long=25.0):
    a = {'status': status, 'entry_value': 30.0,
         'exit': {'at': exit_at + ' 10:00:00'} if exit_at else None}
    if marks is not None:
        a['marks'] = marks
    return {'stock': 'ABC', 'direction': direction, 'quantity': qty,
            'long_strike': strike, 'expiry': '2027-03-30', 'entry_date': entry_date,
            'entry_spot': 1000.0, 'context': {'iv_long': iv_long},
            'arms': {'naked_long': a}}


def _mark(value=30.0, spot=1000.0, day=DAY):
    m = {'at': day + ' 15:30:00', 'value': value}
    if spot is not None:
        m['spot'] = spot
    return {day: m}


NO_CLOSE = lambda stock, d: None


# -- beta --------------------------------------------------------------------

@pytest.mark.parametrize('k', [0.5, 1.0, 2.0])
def test_beta_recovers_the_slope(k):
    n, s = _nifty_and_stock(k)
    assert bs.beta(s, n, sessions=60, min_n=10) == pytest.approx(k, abs=1e-9)


def test_beta_matches_dates_not_positions():
    n, s = _nifty_and_stock(2.0)
    s_missing = s[:10] + s[11:]          # one stock candle missing
    b = bs.beta(s_missing, n, sessions=60, min_n=10)
    # a positional pairing would mis-align every return after the gap
    assert b == pytest.approx(2.0, rel=0.15)


def test_beta_needs_enough_returns_and_variance():
    n, s = _nifty_and_stock(1.0, n=5)
    assert bs.beta(s, n, sessions=60, min_n=10) is None
    flat = _candles([100.0] * 40)
    assert bs.beta(flat, flat, sessions=60, min_n=10) is None


# -- one position ------------------------------------------------------------

def test_a_rally_costs_a_PE_and_pays_a_CE():
    pe = bs.position_shock(_shadow('PE', marks=_mark()), _shadow('PE', marks=_mark())['arms']['naked_long'],
                           DAY, 3.0, 1.0, NO_CLOSE)
    sh = _shadow('CE', marks=_mark())
    ce = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert pe['why'] is None and ce['why'] is None
    assert pe['up'] < 0 < pe['down']
    assert ce['down'] < 0 < ce['up']


def test_the_value_is_repriced_from_the_IV_of_its_own_mark():
    sh = _shadow('PE', marks=_mark(value=30.0, spot=1000.0))
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['iv_basis'] == 'mark' and r['held'] == 30.0 * 500
    t = 20 / 365.0
    iv = ivcalc.implied_vol('PE', 30.0, 1000.0, 1000.0, t, 0.065)
    want = (ivcalc.bs_price('PE', 1030.0, 1000.0, t, 0.065, iv) - 30.0) * 500
    assert r['up'] == round(want, 0)


def test_beta_scales_the_stock_move_and_none_means_one_assumed():
    sh = _shadow('PE', marks=_mark())
    a = sh['arms']['naked_long']
    r1 = bs.position_shock(sh, a, DAY, 3.0, None, NO_CLOSE)
    r2 = bs.position_shock(sh, a, DAY, 3.0, 2.0, NO_CLOSE)
    assert r1['beta_assumed'] is True
    assert r2['up'] < r1['up']            # beta 2 -> a 6% stock move hurts more


def test_a_mark_without_spot_borrows_the_candle_close_of_that_day():
    sh = _shadow('PE', marks=_mark(spot=None))
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0,
                          lambda stock, d: 1000.0 if d == DAY else None)
    assert r['why'] is None


def test_a_mark_without_any_spot_is_unpriced_never_paired_with_entry_spot():
    sh = _shadow('PE', marks=_mark(spot=None))
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['why'] and r['up'] is None


def test_an_old_mark_is_labelled_stale():
    sh = _shadow('PE', marks=_mark(day='2027-03-08'))
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['basis'] == 'STALE close 2027-03-08'


def test_unmarked_position_uses_its_entry_book():
    sh = _shadow('PE', marks={}, entry_date=DAY)
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['basis'] == 'entry %s' % DAY and r['value'] == 30.0


def test_no_IV_from_the_mark_falls_back_to_entry_IV_then_unpriced():
    # a PE value below intrinsic carries no IV
    sh = _shadow('PE', strike=1100.0, marks=_mark(value=50.0, spot=1000.0))
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['iv_basis'] == 'entry' and r['why'] is None
    sh = _shadow('PE', strike=1100.0, marks=_mark(value=50.0, spot=1000.0), iv_long=None)
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['why'] == 'no IV from the mark or the entry'


def test_a_broken_record_is_unpriced_not_raised():
    sh = _shadow('PE', marks=_mark())
    sh['expiry'] = 'garbage'
    r = bs.position_shock(sh, sh['arms']['naked_long'], DAY, 3.0, 1.0, NO_CLOSE)
    assert r['why'].startswith('error')


# -- the book ----------------------------------------------------------------

def test_the_book_is_what_was_open_at_the_end_of_that_day():
    shadows = {
        '1': _shadow('PE', marks=_mark()),
        '2': _shadow('PE', marks=_mark(), status='exited', exit_at='2027-03-09'),
        '3': _shadow('PE', marks=_mark(), status='exited', exit_at='2027-03-11'),
        '4': _shadow('PE', marks=_mark(), entry_date='2027-03-11'),
    }
    d = bs.shock(shadows, DAY, 3.0, {}, NO_CLOSE)
    assert [r['id'] for r in d['rows']] == ['1', '3']
    assert d['up'] == sum(r['up'] for r in d['rows'])
    assert d['beta_assumed'] == 2


def test_unpriced_rows_stay_in_the_count_but_not_the_totals():
    shadows = {'1': _shadow('PE', marks=_mark()),
               '2': _shadow('PE', marks=_mark(spot=None))}
    d = bs.shock(shadows, DAY, 3.0, {'ABC': 1.0}, NO_CLOSE)
    assert d['open'] == 2 and d['priced'] == 1
    assert d['up'] == d['rows'][0]['up']


# -- reading and rendering ---------------------------------------------------

def test_analyse_reads_the_file_and_measures_beta(tmp_path):
    n, s = _nifty_and_stock(1.5)
    p = tmp_path / 'shadow.json'
    p.write_text(json.dumps({'schema': 1, 'shadows': {'7': _shadow('PE', marks=_mark())}}))
    load = lambda sym: n if sym == bs.NIFTY_CANDLES else s
    d = bs.analyse(DAY, state_file=p, load=load)
    assert d['priced'] == 1 and d['rows'][0]['beta'] == 1.5


def test_analyse_never_raises_and_renders_missing(tmp_path):
    p = tmp_path / 'shadow.json'
    p.write_text('{not json')
    d = bs.analyse(DAY, state_file=p, load=lambda s: [])
    assert d.get('error')
    out = '\n'.join(bs.render(d))
    assert 'MISSING' in out


def test_nothing_priced_never_reads_as_zero():
    d = bs.shock({'1': _shadow('PE', marks=_mark(spot=None))}, DAY, 3.0, {}, NO_CLOSE)
    out = '\n'.join(bs.render(d))
    assert 'not computable today' in out and 'Rs +0' not in out


def test_partial_book_is_flagged_and_totals_render():
    shadows = {'1': _shadow('PE', marks=_mark()),
               '2': _shadow('PE', marks=_mark(spot=None))}
    out = '\n'.join(bs.render(bs.shock(shadows, DAY, 3.0, {'ABC': 1.0}, NO_CLOSE)))
    assert 'PARTIAL: 1 of 2' in out and 'NIFTY +3% → Rs -' in out


def test_empty_book_says_so():
    out = '\n'.join(bs.render(bs.shock({}, DAY, 3.0, {}, NO_CLOSE)))
    assert 'no open naked_long position' in out


def test_the_digest_carries_and_renders_the_section(monkeypatch):
    from zebra import digest
    fake = bs.shock({'1': _shadow('PE', marks=_mark())}, DAY, 3.0, {'ABC': 1.0}, NO_CLOSE)
    monkeypatch.setattr(bs, 'analyse', lambda day: fake)
    d = digest.build(DAY)
    assert d['book_shock'] is fake
    assert '## NIFTY ±3% on the open book' in digest.render(d)


def test_rows_are_in_numeric_id_order():
    shadows = {str(i): _shadow('PE', marks=_mark()) for i in (1000, 823, 99)}
    assert [r['id'] for r in bs.shock(shadows, DAY, 3.0, {}, NO_CLOSE)['rows']] == ['99', '823', '1000']


def test_only_open_names_cost_a_candle_read(tmp_path):
    closed = dict(_shadow('PE', marks=_mark(), status='exited', exit_at='2027-03-01'), stock='GONE')
    p = tmp_path / 'shadow.json'
    p.write_text(json.dumps({'shadows': {'1': _shadow('PE', marks=_mark()), '2': closed}}))
    asked = []
    nifty, _ = _nifty_and_stock(1.0)
    bs.analyse(DAY, state_file=p,
               load=lambda sym: asked.append(sym) or (nifty if sym == bs.NIFTY_CANDLES else []))
    assert 'GONE' not in asked and 'ABC' in asked


def test_the_book_is_found_through_LOG_DIR_at_call_time(tmp_path, monkeypatch):
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    (tmp_path / 'shadow_structures.json').write_text(
        json.dumps({'shadows': {'5': _shadow('PE', marks=_mark())}}))
    d = bs.analyse(DAY, load=lambda sym: [])
    assert [r['id'] for r in d['rows']] == ['5']

"""ETF_PI/shared_kite.py: shared token only, option_buy's historical queue, old candle shape."""
import json
import os
import re
import sys
from pathlib import Path

import pytest

ETF = Path(__file__).resolve().parents[2] / 'ETF_PI'
sys.path.insert(0, str(ETF))

import shared_kite as S  # noqa: E402


class R:
    def __init__(self, code, body=None):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


@pytest.fixture
def tok(tmp_path, monkeypatch):
    p = tmp_path / 'kite_access_token.json'
    p.write_text(json.dumps({'api_key': 'k', 'access_token': 'a', 'user_id': 'U'}))
    monkeypatch.setattr(S, 'RATE_FILE', str(tmp_path / 'rate.json'))
    monkeypatch.setattr(S.time, 'sleep', lambda s: None)
    return str(p)


def test_same_queue_and_gap_as_option_buy():
    src = (ETF / 'option_buy.py').read_text(encoding='utf-8')
    gap = float(re.search(r'^HIST_MIN_GAP_S = ([0-9.]+)', src, re.M).group(1))
    assert S.HIST_MIN_GAP_S == gap
    assert "_RATE_FILE = os.path.join(tempfile.gettempdir(), 'ob_kite_rate.json')" in src
    assert "_kite_call('hist'" in src
    assert os.path.basename(S.RATE_FILE) == 'ob_kite_rate_hist.json'   # _rate_file('hist')


def test_candles_drop_oi_and_use_the_shared_token(tok, monkeypatch):
    seen = {}

    def get(url, params, headers, timeout):
        seen.update(url=url, auth=headers['Authorization'], params=params)
        return R(200, {'data': {'candles': [['2026-11-27T00:00:00+0530', 1, 2, 0.5, 1.5, 9, 0]]}})
    monkeypatch.setattr(S.requests, 'get', get)
    rows = S.candles(256265, 'day', '2026-11-20', '2026-11-27', path=tok)
    assert rows == [['2026-11-27T00:00:00+0530', 1, 2, 0.5, 1.5, 9]]
    assert seen['auth'] == 'token k:a' and seen['url'].endswith('/historical/256265/day')
    assert 'kite.zerodha.com' not in seen['url']        # never the web (enctoken) endpoint


def test_429_is_retried_then_succeeds(tok, monkeypatch):
    answers = [R(429), R(200, {'data': {'candles': []}})]
    monkeypatch.setattr(S.requests, 'get', lambda *a, **k: answers.pop(0))
    assert S.candles(1, 'day', 'a', 'b', path=tok) == [] and not answers


def test_non_200_is_empty_not_a_crash(tok, monkeypatch):
    monkeypatch.setattr(S.requests, 'get', lambda *a, **k: R(403))
    assert S.candles(1, 'day', 'a', 'b', path=tok) == []


def test_missing_token_names_the_fix(tmp_path):
    with pytest.raises(S.TokenMissing, match='kite_login'):
        S.load_token(str(tmp_path / 'nope.json'))


def test_queue_spaces_consecutive_calls(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'RATE_FILE', str(tmp_path / 'rate.json'))
    waits = []
    monkeypatch.setattr(S.time, 'sleep', waits.append)
    S._rate_wait(S.RATE_FILE)
    S._rate_wait(S.RATE_FILE)
    assert waits and 0 < waits[-1] <= S.HIST_MIN_GAP_S + 0.01


def test_etf_scripts_never_log_in():
    for name in ('tracker.py', 'extract_data.py'):
        text = (ETF / name).read_text(encoding='utf-8')
        for bad in ('get_enctoken', 'kite_trade', 'pyotp', '_kite\')', 'token.txt',
                    'kite.zerodha.com/oms'):
            assert bad not in text, '%s still has %s' % (name, bad)
        assert 'import shared_kite' in text


def test_daily_chunks_under_kites_2000_day_cap(tok, monkeypatch):
    from datetime import date
    spans = []
    monkeypatch.setattr(S, 'candles', lambda t, i, a, b, path=None: spans.append((a, b)) or [[a]])
    rows = S.daily(1, date(2021, 1, 1), date(2026, 11, 27), path=tok)
    assert len(spans) == 2 and spans[0][0] == '2021-01-01' and spans[-1][1] == '2026-11-27'
    assert all((date.fromisoformat(b) - date.fromisoformat(a)).days < 2000 for a, b in spans)
    assert date.fromisoformat(spans[1][0]) == date.fromisoformat(spans[0][1]).fromordinal(
        date.fromisoformat(spans[0][1]).toordinal() + 1)        # no gap, no overlap
    assert rows == [[spans[0][0]], [spans[1][0]]]

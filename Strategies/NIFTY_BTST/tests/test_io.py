"""Kite adapter, token, notifier, report and CLI — no network anywhere."""
import json
from datetime import date, datetime

import pytest
from kiteconnect import exceptions as kex

from conftest import APP, FakeMarket, T, default_premiums, sym
from nifty_btst import cli, config as config_mod, report
from nifty_btst.kite_io import KiteMarket, QuoteError, TokenError, load_token
from nifty_btst.notify import Notifier

TODAY = date(2026, 10, 26)


# -------------------------------------------------------------------- token
def _tok(tmp_path, gen):
    p = tmp_path / 'tok.json'
    p.write_text(json.dumps({'access_token': 'a', 'api_key': 'k', 'generated_at': gen}))
    return p


def test_token_ok_stale_missing(tmp_path):
    assert load_token(_tok(tmp_path, '2026-10-26T08:45:05'), TODAY)['api_key'] == 'k'
    with pytest.raises(TokenError, match='stale'):
        load_token(_tok(tmp_path, '2026-10-25T08:45:05'), TODAY)
    with pytest.raises(TokenError, match='not found'):
        load_token(tmp_path / 'none.json', TODAY)
    bad = tmp_path / 'bad.json'
    bad.write_text(json.dumps({'access_token': 'a'}))
    with pytest.raises(TokenError, match='no api_key'):
        load_token(bad, TODAY)


# --------------------------------------------------------------- KiteMarket
class FakeKite:
    def __init__(self):
        self.quote_fail = []
        self.quote_chunks = []
        self.inst_calls = 0

    def instruments(self, exch):
        self.inst_calls += 1
        return [
            {'name': 'NIFTY', 'segment': 'NFO-OPT', 'tradingsymbol': 'NIFTY26O2722850CE',
             'instrument_token': 11, 'strike': 22850.0, 'expiry': date(2026, 10, 27),
             'instrument_type': 'CE', 'lot_size': 65},
            {'name': 'NIFTY', 'segment': 'NFO-FUT', 'tradingsymbol': 'NIFTYFUT',
             'instrument_token': 12, 'strike': 0, 'expiry': date(2026, 10, 27),
             'instrument_type': 'FUT', 'lot_size': 65},
            {'name': 'BANKNIFTY', 'segment': 'NFO-OPT', 'tradingsymbol': 'BN',
             'instrument_token': 13, 'strike': 1.0, 'expiry': date(2026, 10, 27),
             'instrument_type': 'CE', 'lot_size': 30},
        ]

    def quote(self, keys):
        if self.quote_fail:
            raise self.quote_fail.pop(0)
        self.quote_chunks.append(len(keys))
        return {k: {'last_price': 100.0, 'depth': {'buy': [{'price': 99.9, 'quantity': 1}],
                                                   'sell': [{'price': 100.1, 'quantity': 1}]}}
                for k in keys}

    def historical_data(self, token, start, end, interval):
        return [{'date': datetime(2026, 10, 26, 9, 20), 'open': 1, 'high': 2,
                 'low': 0.5, 'close': 1.5}]


@pytest.fixture
def km(tmp_path, monkeypatch):
    monkeypatch.setattr('nifty_btst.kite_io.time.sleep', lambda s: None)
    return KiteMarket(FakeKite(), underlying='NIFTY', exchange='NFO',
                      cache_dir=tmp_path, today=TODAY, retries=3)


def test_options_filters_and_caches(km, tmp_path):
    opts = km.options()
    assert [o['tradingsymbol'] for o in opts] == ['NIFTY26O2722850CE']
    assert opts[0]['expiry'] == '2026-10-27'
    km2 = KiteMarket(FakeKite(), underlying='NIFTY', exchange='NFO',
                     cache_dir=tmp_path, today=TODAY)
    assert km2.options() == opts and km2.kite.inst_calls == 0     # from cache


def test_quote_chunks_and_retries(km):
    keys = ['NFO:S%d' % i for i in range(500)]
    km.kite.quote_fail = [kex.NetworkException('Too many requests')]
    books = km.quote(keys)
    assert len(books) == 500 and km.kite.quote_chunks == [450, 50]
    assert books['NFO:S1'].ask == 100.1


def test_quote_gives_up_after_retries(km):
    km.kite.quote_fail = [kex.NetworkException('x')] * 3
    with pytest.raises(QuoteError):
        km.quote(['NFO:A'])


def test_token_exception_is_fatal_not_retried(km):
    km.kite.quote_fail = [kex.TokenException('expired')]
    with pytest.raises(TokenError):
        km.quote(['NFO:A'])


def test_historical_is_tz_aware(km):
    rows = km.minute_candles(1, T('2026-10-26', '09:20:00'), T('2026-10-26', '10:00:00'))
    assert rows[0]['t'].utcoffset().total_seconds() == 19800
    assert km.day_close(256265, date(2026, 10, 26)) == 1.5
    assert km.day_close(256265, date(2026, 10, 27)) is None


# ---------------------------------------------------------------- notifier
def test_notifier_disabled_and_bad_config(tmp_path):
    assert Notifier(tmp_path / 'x.json', False, 'P').send('hi') is False
    bad = tmp_path / 'tg.json'
    bad.write_text('{}')
    n = Notifier(bad, True, 'P')
    assert n.send('hi') is False and n.sent == ['P\nhi']


def test_notifier_logs_non_200(tmp_path, monkeypatch):
    tg = tmp_path / 'tg.json'
    tg.write_text(json.dumps({'bot_token': 't', 'chat_id': 'c'}))

    class R:
        def __init__(self, code):
            self.status_code, self.text = code, 'reason'
    import requests
    monkeypatch.setattr(requests, 'post', lambda *a, **k: R(400))
    assert Notifier(tg, True, 'P').send('x') is False
    monkeypatch.setattr(requests, 'post', lambda *a, **k: R(200))
    assert Notifier(tg, True, 'P').send('x') is True


# ------------------------------------------------------------------ report
def test_report_renders_groups_and_flags():
    closed = [
        {'id': 1, 'net_pnl': 500.0, 'net_ex_brokerage': 540.0, 'cls': 'expiry_day',
         'side': 'CE', 'exit_reason': 'next_day_0925', 'dte_sessions': 4, 'slip_vs_trigger': 0.5},
        {'id': 2, 'net_pnl': -300.0, 'net_ex_brokerage': -260.0, 'cls': 'day_before',
         'side': 'PE', 'exit_reason': 'next_day_late', 'dte_sessions': 1, 'slip_vs_trigger': 1.5},
    ]
    text = report.render(closed, [])
    assert 'PF 1.67' in text and 'expiry_day' in text and 'LATE or SETTLED' in text and '#2' in text
    assert report.render([], []).startswith('No closed')


# --------------------------------------------------------------------- CLI
@pytest.fixture
def wired(tmp_path, monkeypatch):
    """CLI pointed at a temp state dir, a fresh token and a FakeMarket."""
    state = tmp_path / 'state'
    tok = tmp_path / 'tok.json'
    tok.write_text(json.dumps({'access_token': 'a', 'api_key': 'k',
                               'generated_at': '2026-10-26T08:45:00'}))
    raw = json.load(open(APP / 'btst_config.defaults.json'))
    raw['telegram_enabled'] = False
    defaults = tmp_path / 'd.json'
    defaults.write_text(json.dumps(raw))
    monkeypatch.setattr(config_mod, 'STATE_DIR', state)
    monkeypatch.setattr(config_mod, 'KITE_TOKEN_FILE', tok)
    monkeypatch.setattr(config_mod, 'DEFAULTS_FILE', defaults)
    monkeypatch.setattr(config_mod, 'OVERLAY_FILE', tmp_path / 'none.json')
    fm = FakeMarket()
    fm.today = date(2026, 10, 26)
    default_premiums(fm, '2026-10-27')
    monkeypatch.setattr(cli.KiteMarket, 'from_token', classmethod(lambda c, t, **k: fm))
    monkeypatch.setattr(cli.time, 'sleep', lambda s: None)
    clock = {'now': T('2026-10-26', '09:20:05')}
    monkeypatch.setattr(cli, 'now_ist', lambda: clock['now'])
    monkeypatch.setattr(config_mod, 'TELEGRAM_CONFIG', tmp_path / 'tg.json')
    return state, fm, clock, tok


def test_cli_run_once_marks_and_status_reports(wired, capsys):
    state, fm, clock, _ = wired
    assert cli.main(['run', '--once']) == 0
    book = json.loads((state / 'book.json').read_text())
    assert book['days']['2026-10-26']['mark_done']
    assert cli.main(['status']) == 0
    out = capsys.readouterr().out
    assert 'heartbeat' in out and sym('2026-10-27', 22850, 'CE') in out
    assert cli.main(['report']) == 0


def test_cli_run_refuses_stale_token(wired):
    state, fm, clock, tok = wired
    tok.write_text(json.dumps({'access_token': 'a', 'api_key': 'k',
                               'generated_at': '2026-10-25T08:45:00'}))
    assert cli.main(['run']) == 2


def test_cli_run_skips_a_closed_day_without_force(wired):
    state, fm, clock, _ = wired
    clock['now'] = T('2026-10-31', '09:20:00')     # Saturday
    assert cli.main(['run']) == 0
    assert not (state / 'book.json').exists()


def test_cli_after_close_sends_digest_once(wired):
    state, fm, clock, _ = wired
    clock['now'] = T('2026-10-26', '15:45:00')     # F&O closes 15:40
    assert cli.main(['run']) == 0
    book = json.loads((state / 'book.json').read_text())
    assert book['days']['2026-10-26']['eod_sent'] is True
    assert cli.main(['digest']) == 0


def test_cli_corrupt_book_stops(wired):
    state, fm, clock, _ = wired
    state.mkdir(parents=True, exist_ok=True)
    (state / 'book.json').write_text('garbage')
    assert cli.main(['run', '--once']) == 3


def test_cli_compare(wired, tmp_path, capsys):
    csv = tmp_path / 'v.csv'
    csv.write_text('Entry Date,Instrument,Entry Price,Exit Price,PnL\n'
                   '26 Oct 2026,NIFTY 22850 CE,153,160,400\n')
    assert cli.main(['compare', str(csv), '--since', '2026-10-01']) == 0
    assert 'VOLRIX-ONLY' in capsys.readouterr().out


def test_cli_bad_overlay_is_reported_not_crashed(wired, tmp_path, monkeypatch):
    ov = tmp_path / 'bad.json'
    ov.write_text(json.dumps({'stop_frac': 2.0}))
    monkeypatch.setattr(config_mod, 'OVERLAY_FILE', ov)
    assert cli.main(['run']) == 5

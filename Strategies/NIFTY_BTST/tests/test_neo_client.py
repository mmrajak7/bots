"""neo_client against a scripted HTTP session. No network."""
import json
import socket

import pytest
import requests

from nifty_btst import neo_client as nc
from nifty_btst.neo_client import (CredentialsError, NeoAuthError, NeoReader, NeoRejected,
                                   NeoTrader, NeoUnavailable, NeoUnclear, load_credentials,
                                   pin_ipv4, totp_now)

CREDS = {'consumer_key': 'ck', 'mobile_number': '+911234567890', 'ucc': 'U1',
         'mpin': '123456', 'totp_secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}


class R:
    def __init__(self, code=200, body=None, text=None):
        self.status_code = code
        self._body = body
        self.text = text if text is not None else (json.dumps(body) if body is not None else '')

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body


class FakeHTTP:
    """Scripted responses per (method, path-suffix). A list value is consumed
    in order; an Exception instance is raised."""

    def __init__(self):
        self.calls = []
        self.script = {}

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for key, val in self.script.items():
            if url.endswith(key):
                item = val.pop(0) if isinstance(val, list) else val
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError('unscripted %s %s' % (method, url))

    def post(self, url, **kw):
        return self._answer('POST', url, **kw)

    def get(self, url, **kw):
        return self._answer('GET', url, **kw)


def login_ok(http):
    http.script[nc.LOGIN_PATH] = R(200, {'data': {'token': 'VIEW', 'sid': 'S1'}})
    http.script[nc.VALIDATE_PATH] = R(200, {'data': {'token': 'TRADE', 'sid': 'S2',
                                                     'baseUrl': 'https://cis.example/',
                                                     'hsServerId': 'H1', 'greetingName': 'X'}})


@pytest.fixture
def http():
    return FakeHTTP()


def reader(http, trader=False):
    cls = NeoTrader if trader else NeoReader
    r = cls(CREDS, 'https://mis.example', timeout=5, login_timeout=5, session=http,
            totp_fn=lambda s: '000000')
    login_ok(http)
    r.login()
    return r


# ------------------------------------------------------------------ totp
def test_totp_matches_rfc6238_vector():
    # RFC 6238 appendix B, SHA-1, T=59 -> 94287082; last 6 digits
    assert totp_now('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', at=59) == '287082'
    assert totp_now('gezd gnbv gy3t qojq gezd gnbv gy3t qojq', at=59) == '287082'
    with pytest.raises(CredentialsError):
        totp_now('not*base32!', at=59)


# ------------------------------------------------------------ credentials
def test_load_credentials_validates(tmp_path):
    p = tmp_path / 'c.json'
    p.write_text(json.dumps(CREDS))
    assert load_credentials(p)['ucc'] == 'U1'
    p.write_text(json.dumps(dict(CREDS, mpin='')))
    with pytest.raises(CredentialsError, match='mpin'):
        load_credentials(p)
    p.write_text(json.dumps(dict(CREDS, mobile_number='9112')))
    with pytest.raises(CredentialsError, match='ISD'):
        load_credentials(p)
    p.write_text('{bad')
    with pytest.raises(CredentialsError, match='not JSON'):
        load_credentials(p)
    with pytest.raises(CredentialsError, match='unreadable'):
        load_credentials(tmp_path / 'none.json')


def test_pin_ipv4_is_idempotent():
    socket.getaddrinfo = nc._orig_getaddrinfo
    before = socket.getaddrinfo
    try:
        pin_ipv4()
        once = socket.getaddrinfo
        pin_ipv4()
        assert socket.getaddrinfo is once and once is not before
    finally:
        socket.getaddrinfo = before


# ----------------------------------------------------------------- login
def test_login_sends_the_documented_headers_and_bodies(http, caplog):
    r = reader(http)
    (m1, u1, k1), (m2, u2, k2) = http.calls
    assert not any('greeting' in rec.message or 'U1' in rec.message for rec in caplog.records)
    assert u1.endswith(nc.LOGIN_PATH) and k1['headers']['Authorization'] == 'ck'
    assert k1['headers']['neo-fin-key'] == 'neotradeapi'
    assert json.loads(k1['data']) == {'mobileNumber': '+911234567890', 'ucc': 'U1',
                                      'totp': '000000'}
    assert k2['headers']['sid'] == 'S1' and k2['headers']['Auth'] == 'VIEW'
    assert json.loads(k2['data']) == {'mpin': '123456'}
    assert r.base_url == 'https://cis.example' and r.token == 'TRADE' and r.sid == 'S2'
    assert r.ready


def test_login_failures_raise_auth_error(http):
    r = NeoReader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '1')
    http.script[nc.LOGIN_PATH] = R(401, {'status': 'error', 'message': 'Invalid TOTP'})
    with pytest.raises(NeoAuthError, match='Invalid TOTP'):
        r.login()
    http.script[nc.LOGIN_PATH] = requests.ConnectionError('down')
    with pytest.raises(NeoAuthError, match='ConnectionError'):
        r.login()
    http.script[nc.LOGIN_PATH] = R(200, {'data': {'token': 'VIEW', 'sid': 'S1'}})
    http.script[nc.VALIDATE_PATH] = R(200, {'data': {'token': 'T', 'sid': 'S'}})   # no baseUrl
    with pytest.raises(NeoAuthError, match='baseUrl'):
        r.login()
    assert not r.ready


# ----------------------------------------------------------------- reads
def test_reads_carry_session_headers_and_unwrap_rows(http):
    r = reader(http)
    http.script[nc.PATH_ORDER_BOOK] = R(200, {'stat': 'Ok', 'stCode': 200,
                                              'data': [{'nOrdNo': '1'}, {'nOrdNo': '2'}]})
    rows = r.order_book()
    assert [x['nOrdNo'] for x in rows] == ['1', '2']
    m, u, kw = http.calls[-1]
    assert kw['headers']['Auth'] == 'TRADE' and kw['headers']['Sid'] == 'S2'
    assert kw['params'] == {'sId': 'H1'} and kw['timeout'] == 5


def test_read_failures_are_unavailable_never_empty(http):
    r = reader(http)
    http.script[nc.PATH_POSITIONS] = [requests.Timeout('t'), R(502, None, 'Bad Gateway'),
                                      R(200, {'stat': 'Not_Ok', 'emsg': 'OMS down', 'stCode': 5000}),
                                      R(200, {'stat': 'Ok', 'data': 'weird'})]
    for _ in range(4):
        with pytest.raises(NeoUnavailable):
            r.positions()


def test_only_the_exact_empty_shape_is_an_empty_list(http):
    """Rewritten for NEO-03: `stat` used to default to Ok and any `emsg`
    containing 'no data' passed. Now only the configured exact shape, or
    Ok with a list, is believed; everything else is UNAVAILABLE."""
    r = reader(http)
    http.script[nc.PATH_POSITIONS] = R(200, {'stat': 'Not_Ok', 'emsg': 'No Data', 'stCode': 5203})
    assert r.positions() == []
    http.script[nc.PATH_ORDER_BOOK] = R(200, {'stat': 'Ok', 'stCode': 200, 'data': []})
    assert r.order_book() == []
    for body in ({'stat': 'Ok', 'stCode': 200},                       # Ok without data
                 {'message': 'gateway busy'},                          # no stat at all
                 {'fault': {'code': 900}},
                 {'Error Message': 'Complete the 2fa process'},
                 {'stat': 'Not_Ok', 'emsg': 'OMS down', 'stCode': 5000, 'data': []},
                 {'stat': 'Not_Ok', 'emsg': 'no data from adapter', 'stCode': 5000},
                 {'stat': 'Not_Ok', 'emsg': 'No Data', 'stCode': 5999},   # wrong code
                 {'stat': 'Ok', 'data': 'weird'}):
        http.script[nc.PATH_ORDER_BOOK] = R(200, body)
        with pytest.raises(NeoUnavailable):
            r.order_book()
    # the config can declare "Ok without data" as the empty shape once Stage 0 shows it
    r2 = NeoReader(CREDS, 'https://mis.example', 5, 5, session=http, totp_fn=lambda s: '1',
                   policy=nc.EnvelopePolicy(empty_ok_without_data=True))
    login_ok(http)
    r2.login()
    http.script[nc.PATH_ORDER_BOOK] = R(200, {'stat': 'Ok', 'stCode': 200})
    assert r2.order_book() == []


def test_documented_invalid_session_body_relogs_in_on_any_http_code(http):
    """NEO-11: `{"stat":"Not_Ok","emsg":"Invalid session","stCode":1003}`."""
    r = reader(http, trader=True)
    bad = {'stat': 'Not_Ok', 'emsg': 'Invalid session', 'stCode': 1003}
    http.script[nc.PATH_ORDER_BOOK] = [R(200, bad), R(200, {'stat': 'Ok', 'data': [{'nOrdNo': '1'}]})]
    assert r.order_book()[0]['nOrdNo'] == '1' and r.relogins == 1
    http.script[nc.PATH_POSITIONS] = [R(400, bad), R(200, {'stat': 'Ok', 'data': []})]
    assert r.positions() == [] and r.relogins == 2
    http.script[nc.PATH_PLACE] = [R(200, bad)]
    with pytest.raises(NeoUnclear, match='session rejected'):
        r.place(symbol='S', token='1', side='S', qty=65, price='1.00', tag='t',
                segment='nse_fo', product='NRML')
    assert r.relogins == 3 and r.ready


def test_transient_1005_on_a_mutation_is_unclear_not_rejected(http):
    """NEO-34: 1005 Internal Error is documented as transient."""
    t = reader(http, trader=True)
    http.script[nc.PATH_PLACE] = [R(200, {'stat': 'Not_Ok', 'emsg': 'Internal Error', 'stCode': 1005})]
    with pytest.raises(NeoUnclear, match='1005'):
        t.place(symbol='S', token='1', side='S', qty=65, price='1.00', tag='t',
                segment='nse_fo', product='NRML')


def test_persistent_403_logs_in_once_then_marks_the_session_dead(http):
    """NEO-25: a 403 that re-login does not cure must not cost a TOTP login
    per read; `ready` turns False so the caller's throttle applies."""
    r = reader(http)
    n0 = sum(1 for c in http.calls if c[1].endswith(nc.LOGIN_PATH))
    http.script[nc.PATH_ORDER_BOOK] = R(403, {'stat': 'Not_Ok'})
    with pytest.raises(NeoAuthError):
        r.order_book()
    assert sum(1 for c in http.calls if c[1].endswith(nc.LOGIN_PATH)) - n0 == 1
    assert not r.ready and r.session_dead_at is not None
    with pytest.raises(NeoAuthError, match='not logged in'):
        r.order_book()                                            # no login attempt at all
    assert sum(1 for c in http.calls if c[1].endswith(nc.LOGIN_PATH)) - n0 == 1


def test_read_403_relogins_once_then_gives_up(http):
    r = reader(http)
    http.script[nc.PATH_ORDER_BOOK] = [R(403, {'emsg': 'Invalid session'}),
                                       R(200, {'stat': 'Ok', 'data': [{'nOrdNo': '9'}]})]
    assert r.order_book()[0]['nOrdNo'] == '9' and r.relogins == 1
    http.script[nc.PATH_ORDER_BOOK] = [R(403, {}), R(403, {})]
    with pytest.raises(NeoAuthError, match='after re-login'):
        r.order_book()
    assert not r.ready


def test_order_by_number_and_history_and_limits(http):
    r = reader(http)
    http.script[nc.PATH_ORDER_BOOK] = R(200, {'stat': 'Ok', 'data': [{'nOrdNo': '5', 'ordSt': 'open'}]})
    assert r.order('5')['ordSt'] == 'open' and r.order('6') is None
    http.script[nc.PATH_ORDER_HISTORY] = R(200, {'stat': 'Ok', 'data': [{'ordSt': 'complete'}]})
    assert r.order_history('5')[0]['ordSt'] == 'complete'
    m, u, kw = http.calls[-1]
    assert json.loads(kw['data']['jData']) == {'nOrdNo': '5'}
    login_ok(http)
    r.login()
    http.script[nc.PATH_LIMITS] = [R(200, {'stat': 'Ok', 'Net': '12345.5'}),
                                   R(200, {'stat': 'Not_Ok', 'emsg': 'x'})]
    assert r.limits()['Net'] == '12345.5'
    with pytest.raises(NeoUnavailable):
        r.limits()
    http.script[nc.PATH_TRADES] = R(200, {'stat': 'Ok', 'data': []})
    assert r.trades() == []
    http.script[nc.PATH_POSITIONS] = R(200, {'stat': 'Ok', 'stCode': 200, 'data': []})
    assert r.raw_report(nc.PATH_POSITIONS)['stCode'] == 200


def test_scrip_master_paths_and_download(http):
    r = reader(http)
    http.script[nc.PATH_SCRIP_MASTER] = [R(200, {'data': {'filesPaths': ['https://f/nse_cm.csv',
                                                                            'https://f/nse_fo.csv']}}),
                                         R(200, {'data': {}}), R(500, None, 'x')]
    paths = r.scrip_master_paths()
    assert paths[1].endswith('nse_fo.csv')
    assert http.calls[-1][2]['headers']['Authorization'] == 'ck'
    with pytest.raises(NeoUnavailable, match='filesPaths'):
        r.scrip_master_paths()
    with pytest.raises(NeoUnavailable, match='HTTP 500'):
        r.scrip_master_paths()
    http.script['nse_fo.csv'] = [R(200, None, 'a,b\n1,2\n'), R(404, None, 'nope'),
                                 requests.ConnectionError('c')]
    assert r.fetch_text('https://f/nse_fo.csv') == 'a,b\n1,2\n'
    with pytest.raises(NeoUnavailable):
        r.fetch_text('https://f/nse_fo.csv')
    with pytest.raises(NeoUnavailable):
        r.fetch_text('https://f/nse_fo.csv')


# ------------------------------------------------------------- mutations
def test_place_sends_a_limit_day_order_and_returns_the_number(http):
    t = reader(http, trader=True)
    http.script[nc.PATH_PLACE] = R(200, {'nOrdNo': '260123000487752', 'stat': 'Ok', 'stCode': 200})
    no = t.place(symbol='NIFTY-X', token='99', side='B', qty=65, price='160.65',
                 tag='20261026-1-ENTRY-1', segment='nse_fo', product='NRML')
    assert no == '260123000487752'
    m, u, kw = http.calls[-1]
    j = json.loads(kw['data']['jData'])
    assert j == {'am': 'NO', 'dq': '0', 'es': 'nse_fo', 'mp': '0', 'pc': 'NRML', 'pf': 'N',
                 'pr': '160.65', 'pt': 'L', 'qt': '65', 'rt': 'DAY', 'tp': '0', 'ts': 'NIFTY-X',
                 'tt': 'B', 'ig': '20261026-1-ENTRY-1', 'tk': '99', 'os': 'NEOTRADEAPI'}
    assert kw['headers']['Content-Type'] == 'application/x-www-form-urlencoded'
    with pytest.raises(ValueError):
        t.place(symbol='x', token='1', side='X', qty=1, price='1', tag='t', segment='s', product='p')


@pytest.mark.parametrize('resp,exc,match', [
    (R(200, {'stat': 'Not_Ok', 'emsg': 'Insufficient balance.', 'stCode': 1004}), NeoRejected, '1004'),
    (requests.Timeout('t'), NeoUnclear, 'timed out'),
    (requests.ConnectionError('c'), NeoUnclear, 'ConnectionError'),
    (R(502, None, '<html>Bad Gateway</html>'), NeoUnclear, 'non-JSON'),
    (R(500, {'emsg': 'boom'}), NeoUnclear, 'HTTP 500'),
    (R(429, {'message': 'slow down'}), NeoRejected, '429'),       # R3-09: a throttle is NOT placed
    (R(200, {'stat': 'Ok'}), NeoUnclear, 'unrecognised'),
])
def test_place_failures_are_classified_and_never_retried(http, resp, exc, match):
    t = reader(http, trader=True)
    http.script[nc.PATH_PLACE] = [resp]
    before = len(http.calls)
    with pytest.raises(exc, match=match):
        t.place(symbol='s', token='1', side='S', qty=65, price='1.00', tag='t',
                segment='nse_fo', product='NRML')
    assert len(http.calls) == before + 1         # exactly one wire call


def test_place_403_relogins_but_stays_unclear(http):
    t = reader(http, trader=True)
    http.script[nc.PATH_PLACE] = [R(403, {'emsg': 'Invalid session'})]
    with pytest.raises(NeoUnclear, match='re-established'):
        t.place(symbol='s', token='1', side='B', qty=65, price='1.00', tag='t',
                segment='nse_fo', product='NRML')
    assert t.relogins == 1 and t.ready
    http.script[nc.PATH_PLACE] = [R(403, {})]
    http.script[nc.LOGIN_PATH] = R(401, {'message': 'Invalid TOTP'})
    with pytest.raises(NeoAuthError, match='re-login failed'):
        t.place(symbol='s', token='1', side='B', qty=65, price='1.00', tag='t',
                segment='nse_fo', product='NRML')


def test_cancel_shapes(http):
    t = reader(http, trader=True)
    http.script[nc.PATH_CANCEL] = [R(200, {'nOrdNo': '7', 'stat': 'Ok', 'stCode': 200}),
                                   R(200, {'stat': 'Not_Ok', 'emsg': 'already executed', 'stCode': 1006})]
    assert t.cancel('7') == '7'
    assert json.loads(http.calls[-1][2]['data']['jData']) == {'on': '7', 'am': 'NO'}
    with pytest.raises(NeoRejected) as ei:
        t.cancel('7')
    assert ei.value.code == '1006'


def test_reader_has_no_mutating_methods():
    assert not hasattr(NeoReader, 'place') and not hasattr(NeoReader, 'cancel')
    assert not hasattr(NeoReader, '_mutate')

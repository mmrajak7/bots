"""Contract tests from envelopes RECORDED on the live NEO API (Pi, 2026-11-27).

Each body below is what the API actually sent during the owner-approved order
probe and diagnostics (account fields scrubbed). The original fixtures in
test_neo_client.py were written from the docs, which show `emsg`; the live API
sends `errMsg`, and a cancel success carries `result`, not `nOrdNo`. These
tests pin the measured shapes so the client cannot drift back to the folklore.
"""
import logging

import pytest

from nifty_btst import neo_client as nc
from nifty_btst.neo_client import NeoRejected, NeoUnclear, neo_msg

from test_neo_client import R, FakeHTTP, reader  # noqa: F401  (fixture helpers)

# -- recorded bodies (scrubbed) -------------------------------------------------
EMPTY_BOOK = {'stat': 'Not_Ok', 'stCode': 5203, 'errMsg': 'No Data',
              'desc': 'data not found'}
CANCEL_UNKNOWN = {'stCode': 1018, 'errMsg': 'please provide valid internal order number',
                  'stat': 'please provide valid internal order number'}
PLACE_BAD_SYMBOL = {'stCode': 1007, 'errMsg': 'please provide valid symbol', 'stat': 'Not_Ok'}
CANCEL_OK = {'result': '261127000100001', 'stat': 'Ok', 'stCode': 200}
PLACE_OK = {'nOrdNo': '261127000100002', 'stat': 'Ok', 'stCode': 200}


@pytest.fixture
def http():
    return FakeHTTP()


def test_neo_msg_reads_every_seen_key():
    assert neo_msg(EMPTY_BOOK) == 'No Data'
    assert neo_msg({'emsg': 'a'}) == 'a'
    assert neo_msg({'message': 'm'}) == 'm'
    assert neo_msg(CANCEL_UNKNOWN) == 'please provide valid internal order number'
    assert neo_msg({'stat': 'please wait'}) == 'please wait'     # text carried in stat
    assert neo_msg({'stat': 'Not_Ok'}) == '' and neo_msg(None) == ''


@pytest.mark.parametrize('path,call', [
    (nc.PATH_ORDER_BOOK, 'order_book'), (nc.PATH_TRADES, 'trades'),
    (nc.PATH_POSITIONS, 'positions')])
def test_the_measured_empty_report_is_an_empty_book(http, path, call):
    r = reader(http)
    http.script[path] = R(200, EMPTY_BOOK)
    assert getattr(r, call)() == []


def test_errmsg_session_dead_text_is_recognised():
    pol = nc.EnvelopePolicy()
    assert pol.session_dead({'stat': 'Not_Ok', 'errMsg': 'Invalid session', 'stCode': 9})
    assert not pol.session_dead(EMPTY_BOOK)


def test_cancel_success_with_result_is_a_success(http):
    t = reader(http, trader=True)
    http.script[nc.PATH_CANCEL] = R(200, CANCEL_OK)
    assert t.cancel('261127000100001') == '261127000100001'


def test_cancel_with_neither_id_is_still_unclear(http):
    t = reader(http, trader=True)
    http.script[nc.PATH_CANCEL] = R(200, {'stat': 'Ok', 'stCode': 200})
    with pytest.raises(NeoUnclear):
        t.cancel('1')


def test_place_success_still_needs_nordno_not_result(http):
    """`result` is accepted for CANCEL only: a place answered with `result`
    and no `nOrdNo` is not a shape we have seen, so it stays UNCLEAR."""
    t = reader(http, trader=True)
    http.script[nc.PATH_PLACE] = [R(200, PLACE_OK), R(200, {'result': '9', 'stat': 'Ok'})]
    kw = dict(symbol='s', token='1', side='B', qty=65, price='1.00', tag='t',
              segment='nse_fo', product='NRML')
    assert t.place(**kw) == '261127000100002'
    with pytest.raises(NeoUnclear):
        t.place(**kw)


def test_place_bad_symbol_http_400_is_a_rejection(http):
    t = reader(http, trader=True)
    http.script[nc.PATH_PLACE] = R(400, PLACE_BAD_SYMBOL)
    with pytest.raises(NeoRejected) as ei:
        t.place(symbol='s', token='0', side='B', qty=0, price='0.05', tag='t',
                segment='nse_fo', product='NRML')
    assert ei.value.code == 1007 or str(ei.value.code) == '1007'


def test_cancel_unknown_order_with_text_in_stat_is_unclear_not_success(http, caplog):
    t = reader(http, trader=True)
    http.script[nc.PATH_CANCEL] = R(400, CANCEL_UNKNOWN)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(NeoUnclear, match='1018'):
            t.cancel('1')
    assert 'valid internal order number' in caplog.text


def test_a_401_on_place_logs_the_body_scrubbed_and_stays_unclear(http, caplog):
    t = reader(http, trader=True)
    token_like = 'A' * 40
    http.script[nc.PATH_PLACE] = [R(401, {'message': 'session rejected', 'sid': token_like})]
    with caplog.at_level(logging.WARNING):
        with pytest.raises(NeoUnclear, match='session rejected'):
            t.place(symbol='s', token='1', side='B', qty=65, price='1.00', tag='t',
                    segment='nse_fo', product='NRML')
    assert 'session rejected' in caplog.text
    assert token_like not in caplog.text            # long opaque runs are scrubbed
    place_posts = [c for c in http.calls if c[1].endswith(nc.PATH_PLACE)]
    assert len(place_posts) == 1                    # never re-sent

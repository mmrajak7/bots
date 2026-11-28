"""The conftest rails hold (A11): no real HOME, no network."""
import socket

import pytest

from kotak_neo import session as S


def test_home_is_a_throwaway(tmp_path_factory):
    assert '.kotak_neo' not in str(S.HOME) and not (S.HOME / 'LOGIN_HOST').exists()


def test_outbound_connect_raises():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match='reach the network'):
            s.connect(('93.184.216.34', 443))
    finally:
        s.close()


def test_a_default_client_cannot_log_in():
    """The A11 shape: a real NeoClient with no fakes finds no session and cannot refresh."""
    from kotak_neo.client import NeoClient
    c = NeoClient('tst')
    with pytest.raises(Exception) as e:
        c.funds()
    assert not isinstance(e.value, AssertionError)


# -- review X-K2 (2026-11-28): one view per order number --------------------------------

def _client_with_book(rows):
    from kotak_neo.client import NeoClient
    c = NeoClient('etf', exclusive=False)
    c._read = lambda method, path, jdata=None, **kw: {'stat': 'Ok', 'data': rows}
    return c


def _row(no, filled, st='complete', tag='etf-1', side='B', qty=65):
    return {'nOrdNo': no, 'ordSt': st, 'trdSym': 'NIFTYX', 'trnsTp': side, 'qty': str(qty),
            'fldQty': str(filled), 'avgPrc': '100.00', 'prc': '100.00', 'trgPrc': '0',
            'prcTp': 'L', 'rejRsn': '--', 'GuiOrdId': tag}


def test_xk2_a_duplicated_row_is_counted_once_everywhere():
    c = _client_with_book([_row('1', 65), _row('1', 65), _row('2', 0, 'open', 'etf-2', 'S')])
    assert c.own_order_net('NIFTYX') == 65
    assert len(c.orders_tagged('etf-1')) == 1
    assert len(c.order_book()) == 2


def test_xk2_the_copy_with_more_filled_wins():
    c = _client_with_book([_row('1', 0, 'open'), _row('1', 65)])
    (v,) = c.orders_tagged('etf-1')
    assert v.filled == 65 and v.status == 'complete'
    c = _client_with_book([_row('1', 65), _row('1', 0, 'open')])
    (v,) = c.orders_tagged('etf-1')
    assert v.filled == 65


def test_xk2_two_real_orders_are_both_counted():
    c = _client_with_book([_row('1', 65), _row('2', 65, tag='etf-2')])
    assert c.own_order_net('NIFTYX') == 130


def test_xk2_foreign_sells_are_deduped_too():
    c = _client_with_book([_row('9', 65, tag='', side='S'), _row('9', 65, tag='', side='S')])
    assert c.foreign_filled('NIFTYX', 'S') == 65

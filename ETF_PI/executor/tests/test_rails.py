"""The conftest rails hold (A11): no real HOME, no network."""
import socket

import pytest

from executor.tests.conftest import _KS as S


def test_home_is_a_throwaway(tmp_path_factory):
    assert '.kotak_neo' not in str(S.HOME) and not (S.HOME / 'LOGIN_HOST').exists()


def test_outbound_connect_raises():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError, match='reach the network'):
            s.connect(('93.184.216.34', 443))
    finally:
        s.close()



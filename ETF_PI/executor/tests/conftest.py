"""Suite-wide rail: NO REAL NEO OR NETWORK, EVER (A11, 2026-11-28: another bot's
test run on the Pi built a default NeoClient and LOGGED IN for real). Every test
gets a throwaway kotak_neo HOME (no real creds, session or LOGIN_HOST) and any
outbound socket connect raises. Telegram is muzzled separately (Tests/_muzzle)."""
import os
import socket
import sys

import pytest

ETF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ETF)
from executor import config as _C            # noqa: E402
from executor.broker import import_kotak_neo  # noqa: E402

_KS = import_kotak_neo(_C.load(local_path=os.path.join(ETF, 'no_such_overlay.yaml'))['bots_root'])[2]
_REAL_CONNECT = socket.socket.connect


def _no_network(self, address, *a, **kw):
    host = address[0] if isinstance(address, tuple) else address
    if host in ('127.0.0.1', '::1', 'localhost') or self.family == getattr(socket, 'AF_UNIX', None):
        return _REAL_CONNECT(self, address, *a, **kw)
    raise RuntimeError('test tried to reach the network (%r) -- tests use fakes only' % (address,))


@pytest.fixture(autouse=True)
def _rails(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(_KS, 'HOME', tmp_path_factory.mktemp('neo_home'))
    monkeypatch.setattr(socket.socket, 'connect', _no_network)
    yield

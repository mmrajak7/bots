"""Suite-wide rails.

1. Alerts queued by a bot's hot path are delivered INLINE in tests (production
   delivers them on a background thread, review R4), so a test that counts
   deliveries never races a thread. test_review_k covers the async path.
2. NO REAL NEO, EVER (A11, 2026-11-28: a zebra test run on the Pi built a default
   NeoClient, found the real session stale and LOGGED IN for real -- LOGIN_HOST
   allows it there). Every test gets a throwaway HOME (no real creds, session or
   LOGIN_HOST) and any outbound socket connect raises. Tests that need a HOME set
   their own on top; tests talk to fakes, never the network.
"""
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kotak_neo import session as S  # noqa: E402

_REAL_CONNECT = socket.socket.connect


def _no_network(self, address, *a, **kw):
    host = address[0] if isinstance(address, tuple) else address
    if host in ('127.0.0.1', '::1', 'localhost') or self.family == getattr(socket, 'AF_UNIX', None):
        return _REAL_CONNECT(self, address, *a, **kw)
    raise RuntimeError('test tried to reach the network (%r) -- tests use fakes only' % (address,))


@pytest.fixture(autouse=True)
def _rails(monkeypatch, tmp_path_factory):
    monkeypatch.setattr(S, 'ASYNC_ALERTS', False)
    monkeypatch.setattr(S, 'HOME', tmp_path_factory.mktemp('neo_home'))
    monkeypatch.setattr(socket.socket, 'connect', _no_network)
    S._dead_cache.clear()
    yield
    S._dead_cache.clear()

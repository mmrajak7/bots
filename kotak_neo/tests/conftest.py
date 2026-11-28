"""Suite-wide: alerts queued by a bot's hot path are delivered INLINE in tests
(production delivers them on a background thread, review R4), so a test that
counts deliveries never races a thread. test_review_k covers the async path."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kotak_neo import session as S  # noqa: E402


@pytest.fixture(autouse=True)
def _inline_alert_delivery(monkeypatch):
    monkeypatch.setattr(S, 'ASYNC_ALERTS', False)
    S._dead_cache.clear()
    yield
    S._dead_cache.clear()

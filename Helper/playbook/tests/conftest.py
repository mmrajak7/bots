"""Test-wide safety rails for the playbook package.

Same rule as `zebra/tests/conftest.py`: no test may send a real Telegram, and
no test may write production state. ST Watch's alert path is one function call
away from `requests.post`, and its dedup/history files live in `Helper/logs/`,
so both are railed here rather than per-file — a rail scoped to the file that
prompted it leaves the next test free to do it again.
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from playbook.st_watch import config as cfg  # noqa: E402


class RealTelegramAttempted(BaseException):
    """BaseException on purpose: `_send_telegram` wraps everything in
    `except Exception` and degrades to a warning, so a plain Exception would be
    swallowed and the rail would pass while proving nothing."""


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    import requests
    real_post = requests.post

    def guarded(url, *a, **k):
        if 'api.telegram.org' in str(url):
            raise RealTelegramAttempted(
                'a test tried to send a REAL Telegram: %s' % str(url)[:60])
        return real_post(url, *a, **k)

    monkeypatch.setattr(requests, 'post', guarded)


@pytest.fixture(autouse=True)
def _no_production_state(tmp_path_factory, monkeypatch):
    """Dedup state and alert history must never be the production files."""
    tmp = tmp_path_factory.mktemp('st_watch_rail')
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp)
    monkeypatch.setattr(cfg, 'STATE_FILE', tmp / 'st_watch_state.json')
    monkeypatch.setattr(cfg, 'ALERT_LOG_FILE', tmp / 'st_watch_alerts.json')
    return tmp

"""Test-wide safety rail for the LIVE-money package.

No test here may send a real Telegram. Reported by the owner 2026-08-12: the
zebra suite had been messaging his phone on every run (a VETOED alert for
TESTCO, the fixture symbol). `bcs/` had no conftest at all, so its only
protection was whatever each file happened to patch for itself — which is the
arrangement that failed next door.

Blocked at the NETWORK call rather than at the wrapper, so a future sender or a
copy-pasted requests.post cannot slip past it. It RAISES: a silent stub would
let "we never send anything" pass as healthy.
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))


class RealTelegramAttempted(BaseException):
    """Deliberately a BaseException, not an Exception.

    The production senders are wrapped in `except Exception` — correctly, since
    a Telegram failure must never make an agent think its verdict did not land.
    That same handler swallows a plain-Exception rail and logs it as a warning,
    so the suite passes, nobody sees it, and the messages keep arriving. A test
    rail that production error handling can catch is not a rail.
    """


@pytest.fixture(autouse=True)
def _no_telegram_http(monkeypatch):
    import requests
    real_post = requests.post

    def guarded(url, *a, **k):
        if 'api.telegram.org' in str(url):
            raise RealTelegramAttempted(
                'a test tried to send a REAL Telegram: %s' % str(url)[:60])
        return real_post(url, *a, **k)

    monkeypatch.setattr(requests, 'post', guarded)

"""Alert delivery: once each, never dropped, never two senders."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kotak_neo import alerts as A, session as S     # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(S, 'HOME', tmp_path / 'neo')
    return tmp_path / 'neo'


class Tg:
    def __init__(self, fail_after=None):
        self.sent, self.fail_after = [], fail_after

    def __call__(self, text):
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        self.sent.append(text)
        return True


def queue(n):
    for i in range(n):
        S._alert('latched', 'alert %d' % i, S._now())


def test_each_alert_is_delivered_once(home):
    queue(2)
    tg = Tg()
    assert A.flush(tg) == 2 and A.flush(tg) == 0
    assert len(tg.sent) == 2 and 'NEO SESSION [latched]' in tg.sent[0] and 'alert 0' in tg.sent[0]


def test_a_failed_send_is_kept_for_the_next_flush(home):
    queue(3)
    assert A.flush(Tg(fail_after=1)) == 1
    tg = Tg()
    assert A.flush(tg) == 2 and 'alert 1' in tg.sent[0]


def test_at_most_a_few_per_call(home):
    queue(A.MAX_PER_FLUSH + 3)
    assert A.flush(Tg()) == A.MAX_PER_FLUSH and A.flush(Tg()) == 3


def test_a_second_process_flushing_sends_nothing(home):
    queue(1)
    S._ensure_home()
    with S.file_lock('alerts.lock', 1.0):
        assert A.flush(Tg()) == 0


def test_no_alerts_file_and_a_broken_sender_never_raise(home):
    assert A.flush(Tg()) == 0
    queue(1)

    def boom(text):
        raise RuntimeError('x')
    assert A.flush(boom) == 0


def test_refresh_flushes_after_releasing_the_lock(home, monkeypatch):
    seen = []

    def flush():
        with S.file_lock('refresh.lock', 0.0) as got:     # the refresh lock must be free
            seen.append(got)
    monkeypatch.setattr(S, '_flush_alerts', flush)
    S.refresh(0, 't', _verify=lambda s: S.ALIVE, _login=lambda c, f: None,
              _creds=lambda p: {})
    assert seen == [True]

"""The CLI's --final alert."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kite_login import core as C  # noqa: E402
import kite_login.__main__ as CLI  # noqa: E402


@pytest.mark.parametrize('final,alerts', [(False, 0), (True, 1)])
def test_cli_alerts_only_on_the_final_slot(monkeypatch, final, alerts):
    sent = []
    monkeypatch.setattr(CLI.C, 'run', lambda aliases, cfg: [C.Result('personal', C.FAILED, 'x')])
    monkeypatch.setattr(CLI.H, 'refresh', lambda path, force=False: (True, 'ok'))
    monkeypatch.setattr(CLI, 'send_telegram', lambda text, cfg: sent.append(text) or True)
    rc = CLI.main(['morning'] + (['--final'] if final else []))
    assert rc == 1 and len(sent) == alerts
    if alerts:
        assert 'personal token: FAILED' in sent[0]


def test_cli_good_morning_is_silent_and_zero(monkeypatch):
    sent = []
    monkeypatch.setattr(CLI.C, 'run', lambda aliases, cfg: [C.Result('personal', C.NOOP, ''),
                                                            C.Result('company', C.LOGGED_IN, '')])
    monkeypatch.setattr(CLI.H, 'refresh', lambda path, force=False: (True, 'ok'))
    monkeypatch.setattr(CLI, 'send_telegram', lambda text, cfg: sent.append(text) or True)
    assert CLI.main(['morning', '--final']) == 0 and not sent


@pytest.mark.parametrize('final,alerts', [(False, 0), (True, 1)])
def test_a_crash_still_alerts_from_the_final_slot(monkeypatch, final, alerts):
    """Review 2026-11-27 #3: a crash before the report (lock dir unwritable, bad
    config) must not end the 08:36 slot silently."""
    sent = []

    def crash(aliases, cfg):
        raise PermissionError('lock dir')
    monkeypatch.setattr(CLI.C, 'run', crash)
    monkeypatch.setattr(CLI, 'send_telegram', lambda text, cfg: sent.append(text) or True)
    assert CLI.main(['morning'] + (['--final'] if final else [])) == 1
    assert len(sent) == alerts
    if alerts:
        assert 'crashed: PermissionError' in sent[0]

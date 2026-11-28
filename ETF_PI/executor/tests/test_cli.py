"""The CLI's own rules (executor/__main__.py). No network, no Telegram."""
import os
import sys

ETF = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ETF)
from Tests import _muzzle  # noqa: E402,F401
from executor import __main__ as M  # noqa: E402


def test_b2_no_stopped_flag_starts_at_once(tmp_path):
    sent = []
    assert M.wait_while_stopped(str(tmp_path), {'flag': False}, alert=sent.append,
                                sleep=lambda s: None) is True
    assert sent == []


def test_b2_stopped_survives_a_restart_idles_alerts_once_then_start_resumes(tmp_path):
    """The 08:00 reboot starts `run` while the owner's STOPPED flag is set: it idles
    (alerting once) and goes on only when `executor start` removes the flag."""
    flag = tmp_path / 'STOPPED'
    flag.write_text('x')
    sent, naps = [], []

    def nap(s):
        naps.append(s)
        if len(naps) == 3:
            flag.unlink()                             # the owner runs `executor start`
    assert M.wait_while_stopped(str(tmp_path), {'flag': False}, alert=sent.append,
                                sleep=nap) is True
    assert len(naps) == 3 and len(sent) == 1 and 'IDLE' in sent[0]


def test_b2_a_signal_while_stopped_exits_without_starting(tmp_path):
    (tmp_path / 'STOPPED').write_text('x')
    stop = {'flag': False}

    def nap(s):
        stop['flag'] = True                           # systemctl stop / reboot
    assert M.wait_while_stopped(str(tmp_path), stop, alert=lambda m: None, sleep=nap) is False

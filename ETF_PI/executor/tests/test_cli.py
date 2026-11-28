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


def test_a12_stop_placing_defaults_before_the_broker_square_off(tmp_path):
    import datetime as dt
    from executor import config as C
    cfg = C.load(local_path=str(tmp_path / 'none.yaml'))
    assert cfg['stop_placing_time'] < dt.time(15, 20) and cfg['stop_placing_time'] > cfg['eod_time_t']


def test_a12_stop_placing_before_eod_is_refused(tmp_path):
    import pytest
    from executor import config as C
    with pytest.raises(C.ConfigError, match='stop_placing'):
        C.load(local_path=str(tmp_path / 'none.yaml'), override={'stop_placing': '15:10:00'})


def test_a7_product_is_mis_only_and_the_bot_is_etf(tmp_path):
    import pytest
    from executor import config as C
    none = str(tmp_path / 'none.yaml')
    assert C.load(local_path=none)['product'] == 'MIS'
    for bad in ({'product': 'NRML'}, {'product': 'CNC'}, {'neo_bot': 'btst'}, {'neo_bot': 'etfx'}):
        with pytest.raises(C.ConfigError):
            C.load(local_path=none, override=bad)

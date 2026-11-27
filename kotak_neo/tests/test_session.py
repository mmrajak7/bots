"""kotak_neo.session: one session, single-flight refresh, the brakes, fail closed.

No network: the login, verify and credentials seams are fakes. The race and
kill tests use REAL processes, because the property under test is "two
processes never both log in", which a thread cannot prove.

Run:  cd BOTS && python -m pytest kotak_neo/tests -q
"""
import json
import multiprocessing as mp
import os
import socket
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from kotak_neo import session as S          # noqa: E402

CREDS = {'consumer_key': 'CK', 'mobile_number': '+911234567890', 'ucc': 'UCC1',
         'mpin': '654321', 'totp_secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}
SECRETS = ('TOKEN-', 'SID-', '654321', 'GEZDGNBV', 'UCC1')


def fake_creds(path):
    return dict(CREDS)


class Login:
    """Counts calls; returns a fresh session or raises a scripted failure."""

    def __init__(self, *fail):
        self.calls = 0
        self.fail = list(fail)

    def __call__(self, creds, cfg):
        self.calls += 1
        if self.fail:
            kind = self.fail.pop(0)
            if kind:
                raise S.LoginFailed(kind, 'scripted %s failure' % kind)
        return {'token': 'TOKEN-%d' % self.calls, 'sid': 'SID-%d' % self.calls,
                'base_url': 'https://cis.example', 'server_id': 'H1'}


def verify_by_gen(dead_upto):
    """DEAD for generations <= dead_upto, ALIVE above."""
    return lambda s: S.DEAD if s.generation <= dead_upto else S.ALIVE


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / 'neo'
    monkeypatch.setattr(S, 'HOME', h)
    h.mkdir()
    (h / 'LOGIN_HOST').write_text(socket.gethostname(), encoding='utf-8')
    return h


def publish(gen, day=None):
    """Put a session of generation `gen` on disk (as a login would)."""
    st = S.read_state()
    st['last_generation'] = gen - 1
    now = S._now() if day is None else S._now() - timedelta(days=day)
    S._publish(st, None, {'token': 'TOKEN-x', 'sid': 'SID-x', 'base_url': 'https://cis.example',
                          'server_id': None}, dict(CREDS), now)
    st = S.read_state()
    st['next_allowed_at'] = 0.0
    S._write_state(st)


def R(failed, **kw):
    kw.setdefault('_creds', fake_creds)
    return S.refresh(failed, 'test', **kw)


# ── current(): never logs in, fails closed ─────────────────────────────────

def test_no_session_fails_closed(home):
    with pytest.raises(S.SessionUnavailable, match='no published'):
        S.current()


@pytest.mark.parametrize('body', ['{not json', '[]', '{}',
                                  json.dumps({'schema': 1, 'generation': 0}),
                                  json.dumps({'schema': 9, 'generation': 3, 'trading_day': 'd',
                                              'created_at': 'c', 'base_url': 'b', 'token': 't',
                                              'sid': 's', 'consumer_key': 'k', 'ucc': 'u'})])
def test_a_broken_session_file_fails_closed(home, body):
    (home / 'session.json').write_text(body, encoding='utf-8')
    with pytest.raises(S.SessionUnavailable):
        S.current()


def test_yesterdays_session_is_used_not_refreshed(home):
    publish(4, day=1)
    s = S.current()
    assert s.generation == 4 and s.trading_day != S._now().strftime('%Y-%m-%d')


def test_session_repr_never_shows_the_token(home):
    publish(2)
    assert 'TOKEN' not in repr(S.current()) and 'SID' not in repr(S.current())


# ── refresh: the anti-in-fighting rules ─────────────────────────────────────

def test_a_newer_generation_is_adopted_without_verify_or_login(home):
    publish(6)
    lg = Login()
    res = R(5, _verify=lambda s: pytest.fail('must not verify'), _login=lg)
    assert res.outcome == S.ADOPTED and res.session.generation == 6 and lg.calls == 0


def test_an_alive_session_is_never_replaced(home):
    publish(5)
    lg = Login()
    res = R(5, _verify=lambda s: S.ALIVE, _login=lg)
    assert res.outcome == S.STILL_ALIVE and lg.calls == 0


def test_unreachable_neo_does_not_log_in(home):
    publish(5)
    lg = Login()
    assert R(5, _verify=lambda s: S.UNAVAILABLE, _login=lg).outcome == S.UNAVAILABLE
    assert lg.calls == 0


def test_a_dead_session_logs_in_once_and_publishes_the_next_generation(home):
    publish(5)
    lg = Login()
    res = R(5, _verify=verify_by_gen(5), _login=lg)
    assert res.outcome == S.LOGGED_IN and lg.calls == 1
    assert S.current().generation == 6 and S.current().token == 'TOKEN-1'
    if os.name != 'nt':
        assert oct(os.stat(home / 'session.json').st_mode & 0o777) == '0o600'
        assert oct(os.stat(home / 'state.json').st_mode & 0o777) == '0o600'


def test_no_session_at_all_logs_in(home):
    lg = Login()
    res = R(0, _verify=lambda s: pytest.fail('nothing to verify'), _login=lg)
    assert res.outcome == S.LOGGED_IN and S.current().generation == 1


def test_generation_survives_a_corrupt_session_file(home):
    publish(5)
    (home / 'session.json').write_text('garbage', encoding='utf-8')
    res = R(5, _verify=verify_by_gen(99), _login=Login())
    assert res.outcome == S.LOGGED_IN and S.current().generation == 6   # never back to 1


def test_a_corrupt_state_file_keeps_the_floor_and_imposes_a_cooldown(home):
    publish(7)
    (home / 'state.json').write_text('garbage', encoding='utf-8')
    lg = Login()
    res = R(7, _verify=verify_by_gen(7), _login=lg)
    assert res.outcome == S.COOLDOWN and lg.calls == 0
    assert S.read_state()['last_generation'] == 7


# ── the brakes ───────────────────────────────────────────────────────────────

def test_cooldown_between_two_logins(home):
    publish(1)
    lg = Login()
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.LOGGED_IN
    res = R(2, _verify=verify_by_gen(10), _login=lg)
    assert res.outcome == S.COOLDOWN and lg.calls == 1


def test_daily_cap_is_a_long_cooldown_and_an_alert_never_a_latch(home, monkeypatch):
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, max_logins_per_day=1, cooldown_sec=0.0))
    publish(1)
    lg = Login()
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.LOGGED_IN
    res = R(2, _verify=verify_by_gen(10), _login=lg)
    assert res.outcome == S.COOLDOWN and 'cap' in res.detail and lg.calls == 1
    st = S.read_state()
    assert st['latched'] is None
    assert st['next_allowed_at'] - time.time() > 1700
    assert 'daily_cap' in (home / 'alerts.jsonl').read_text(encoding='utf-8')


def test_a_refused_mpin_latches_and_nothing_logs_in_until_unblock(home, monkeypatch):
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, cooldown_sec=0.0))
    publish(1)
    lg = Login('mpin')
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.FAILED
    assert S.read_state()['latched'].startswith('mpin')
    res = R(1, _verify=verify_by_gen(10), _login=lg)
    assert res.outcome == S.BLOCKED and lg.calls == 1          # no second network login
    S.unblock('test')
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.LOGGED_IN


def test_a_credentials_problem_latches_without_a_network_call(home):
    publish(1)
    lg = Login()

    def bad(path):
        raise S.CredentialsError('missing mpin')
    res = R(1, _verify=verify_by_gen(10), _login=lg, _creds=bad)
    assert res.outcome == S.BLOCKED and lg.calls == 0 and S.read_state()['latched']


def test_network_failures_back_off_and_alert_once(home, monkeypatch):
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, cooldown_sec=10.0))
    publish(1)
    lg = Login('network', 'network')
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.FAILED
    first = S.read_state()['next_allowed_at']
    st = S.read_state(); st['next_allowed_at'] = 0; S._write_state(st)
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.FAILED
    second = S.read_state()['next_allowed_at']
    assert second - time.time() > first - time.time() + 5          # doubled
    alerts = (home / 'alerts.jsonl').read_text(encoding='utf-8').splitlines()
    assert len([a for a in alerts if 'login_failed' in a]) == 1
    assert S.read_state()['latched'] is None


def test_without_login_host_nothing_logs_in(home):
    (home / 'LOGIN_HOST').unlink()
    publish(1)
    lg = Login()
    read = []
    res = R(1, _verify=verify_by_gen(10), _login=lg, _creds=lambda p: read.append(p))
    assert res.outcome == S.HOST_REFUSED and lg.calls == 0 and read == []


def test_a_login_host_naming_another_machine_refuses(home):
    (home / 'LOGIN_HOST').write_text('the-pi', encoding='utf-8')
    assert S.login_host_ok() is (socket.gethostname() == 'the-pi')


def test_lock_held_elsewhere_times_out_without_logging_in(home):
    publish(1)
    lg = Login()
    with S._lock(1.0) as got:
        assert got
        res = R(1, wait_s=0.3, _verify=verify_by_gen(10), _login=lg)
    assert res.outcome == S.LOCK_TIMEOUT and lg.calls == 0


# ── scheduled login ─────────────────────────────────────────────────────────

def test_scheduled_is_a_noop_when_todays_session_verifies(home):
    publish(3)
    lg = Login()
    res = S.scheduled_login(_verify=lambda s: S.ALIVE, _login=lg, _creds=fake_creds)
    assert res.outcome == S.NOOP and lg.calls == 0


def test_scheduled_replaces_yesterdays_session_even_if_alive(home):
    publish(3, day=1)
    lg = Login()
    res = S.scheduled_login(_verify=lambda s: S.ALIVE, _login=lg, _creds=fake_creds)
    assert res.outcome == S.LOGGED_IN and S.current().generation == 4


# ── nothing secret anywhere ─────────────────────────────────────────────────

def test_no_secret_in_logs_files_or_status(home, caplog, monkeypatch):
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, cooldown_sec=0.0))
    caplog.set_level('DEBUG')
    publish(1)
    R(1, _verify=verify_by_gen(10), _login=Login('network'))
    R(1, _verify=verify_by_gen(10), _login=Login())
    texts = [caplog.text, json.dumps(S.status())]
    for name in ('refresh_log.jsonl', 'alerts.jsonl'):
        if (home / name).exists():
            texts.append((home / name).read_text(encoding='utf-8'))
    for t in texts:
        for sec in SECRETS:
            assert sec not in t, (sec, t[:200])


# ── real processes: single flight, and a kill mid-login ─────────────────────

def _race_worker(home, counter):
    S.HOME = Path(home)

    def login(creds, cfg):
        with open(counter, 'a') as f:
            f.write('login\n')
        time.sleep(0.5)
        return {'token': 'TOKEN-r', 'sid': 'SID-r', 'base_url': 'https://cis.example',
                'server_id': None}
    res = S.refresh(1, 'racer', _verify=verify_by_gen(1), _login=login, _creds=fake_creds)
    with open(counter + '.out', 'a') as f:
        f.write(res.outcome + '\n')


def test_eight_processes_that_see_one_death_log_in_once(home, tmp_path):
    publish(1)
    counter = str(tmp_path / 'logins')
    ctx = mp.get_context('spawn')
    ps = [ctx.Process(target=_race_worker, args=(str(home), counter)) for _ in range(8)]
    for p in ps:
        p.start()
    for p in ps:
        p.join(60)
    assert all(p.exitcode == 0 for p in ps)
    assert open(counter).read().count('login') == 1
    outs = open(counter + '.out').read().split()
    assert len(outs) == 8 and outs.count('LOGGED_IN') == 1
    assert set(outs) <= {'LOGGED_IN', 'ADOPTED'}
    assert S.current().generation == 2


def _hang_worker(home, started):
    S.HOME = Path(home)

    def login(creds, cfg):
        open(started, 'w').close()
        time.sleep(60)
    S.refresh(1, 'hanger', _verify=verify_by_gen(1), _login=login, _creds=fake_creds)


def test_a_process_killed_mid_login_leaves_its_cooldown_no_storm(home, tmp_path):
    publish(1)
    started = str(tmp_path / 'started')
    ctx = mp.get_context('spawn')
    p = ctx.Process(target=_hang_worker, args=(str(home), started))
    p.start()
    t0 = time.time()
    while not os.path.exists(started) and time.time() - t0 < 30:
        time.sleep(0.05)
    assert os.path.exists(started)
    p.kill()
    p.join(10)
    lg = Login()
    res = R(1, wait_s=5, _verify=verify_by_gen(1), _login=lg)
    assert res.outcome == S.COOLDOWN and lg.calls == 0          # lock released, attempt on disk
    st = S.read_state()
    assert st['last_attempt']['outcome'] == 'in_flight' and st['logins_today'] == 1


# ── the network functions against a fake HTTP ───────────────────────────────

class _R:
    def __init__(self, code, body):
        self.status_code, self._b = code, body
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        if self._b is None:
            raise ValueError
        return self._b


class _HTTP:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(url)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


CFG = dict(S.DEFAULTS)


def test_network_login_two_steps_ok():
    h = _HTTP(_R(200, {'data': {'token': 'v', 'sid': 's'}}),
              _R(200, {'data': {'token': 'T', 'sid': 'S', 'baseUrl': 'https://x/',
                                'hsServerId': 'H'}}))
    out = S._network_login(CREDS, CFG, http=h)
    assert out == {'token': 'T', 'sid': 'S', 'base_url': 'https://x', 'server_id': 'H'}
    assert h.calls[0].endswith(S.LOGIN_PATH) and h.calls[1].endswith(S.VALIDATE_PATH)


@pytest.mark.parametrize('answers,kind', [
    ((_R(503, None),), 'network'),
    ((_R(401, {'message': 'Invalid TOTP'}),), 'totp'),
    ((_R(200, {'data': {'token': 'v', 'sid': 's'}}), _R(401, {'message': 'Invalid MPIN'})), 'mpin'),
])
def test_network_login_classifies_failures(answers, kind):
    with pytest.raises(S.LoginFailed) as ei:
        S._network_login(CREDS, CFG, http=_HTTP(*answers))
    assert ei.value.kind == kind


def test_network_login_timeout_is_network():
    import requests
    with pytest.raises(S.LoginFailed) as ei:
        S._network_login(CREDS, CFG, http=_HTTP(requests.Timeout()))
    assert ei.value.kind == 'network'


SESS = S.Session(1, 'd', 'c', 'https://x', 'T', 'S', None, 'CK', 'U')


@pytest.mark.parametrize('answer,want', [
    (_R(200, {'stat': 'Ok', 'Net': '1'}), S.ALIVE),
    (_R(401, {'message': 'x'}), S.DEAD),
    (_R(200, {'stat': 'Not_Ok', 'stCode': 1003, 'errMsg': 'Invalid session'}), S.DEAD),
    (_R(200, {'stat': 'Not_Ok', 'errMsg': 'Invalid session'}), S.DEAD),
    (_R(502, None), S.UNAVAILABLE),
    (_R(200, {'stat': 'Not_Ok', 'stCode': 5203, 'errMsg': 'No Data'}), S.UNAVAILABLE),
])
def test_verify_classifies(answer, want):
    assert S._network_verify(SESS, CFG, http=_HTTP(answer)) == want


def test_totp_rfc_vector():
    assert S.totp_now('GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ', at=59) == '287082'

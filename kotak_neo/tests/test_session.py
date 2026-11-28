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

CREDS = {'consumer_key': 'CONSUMERKEY9', 'mobile_number': '+911234567890', 'ucc': 'UCC1',
         'mpin': '654321', 'totp_secret': 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'}
SECRETS = ('TOKEN-', 'SID-', '654321', 'GEZDGNBV', 'UCC1', '1234567890', 'CONSUMERKEY9')


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
                'base_url': 'https://cis.kotaksecurities.com', 'server_id': 'H1'}


def verify_by_gen(dead_upto):
    """DEAD for generations <= dead_upto, ALIVE above."""
    return lambda s: S.DEAD if s.generation <= dead_upto else S.ALIVE


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch):
    monkeypatch.setattr(S, '_flush_alerts', lambda: None)


@pytest.fixture(autouse=True)
def _no_cache():
    S._alive_cache.clear()
    yield
    S._alive_cache.clear()


@pytest.fixture
def fast(monkeypatch):
    """Floors off, cooldown 0: for tests about something other than timing."""
    monkeypatch.setattr(S, 'FLOORS', {})
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, cooldown_sec=0.0))


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
    S._publish(st, None, {'token': 'TOKEN-x', 'sid': 'SID-x', 'base_url': 'https://cis.kotaksecurities.com',
                          'server_id': None}, dict(CREDS), now)
    st = S.read_state()
    st['next_allowed_at'] = 0.0
    S._write_state(st)


def R(failed, **kw):
    kw.setdefault('_creds', fake_creds)
    kw.setdefault('_sleep', lambda sec: None)
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


def test_a_corrupt_state_file_is_rebuilt_once_then_logins_work(home, monkeypatch):
    """Review F1: it used to re-arm a cooldown on EVERY read, forever."""
    monkeypatch.setattr(S, 'FLOORS', {})
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, cooldown_sec=0.5))
    publish(7)
    (home / 'state.json').write_text('', encoding='utf-8')         # the power-cut shape
    lg = Login()
    res = R(7, _verify=verify_by_gen(7), _login=lg)
    assert res.outcome == S.COOLDOWN and lg.calls == 0
    st = json.loads((home / 'state.json').read_text(encoding='utf-8'))   # written back, valid
    assert st['last_generation'] == 7
    assert 'state_rebuilt' in (home / 'alerts.jsonl').read_text(encoding='utf-8')
    time.sleep(0.6)
    res = R(7, _verify=verify_by_gen(7), _login=lg)
    assert res.outcome == S.LOGGED_IN and S.current().generation == 8


@pytest.mark.parametrize('bad', ['{"next_allowed_at": "x"}', '{"latched": 5}', '[]'])
def test_a_wrong_typed_state_is_rebuilt_not_raised(home, bad):
    publish(3)
    (home / 'state.json').write_text(bad, encoding='utf-8')
    res = R(3, _verify=verify_by_gen(3), _login=Login())
    assert res.outcome == S.COOLDOWN          # one rebuild cooldown, not ERROR forever
    assert S.read_state().get('_corrupt') is None


# ── the brakes ───────────────────────────────────────────────────────────────

def test_cooldown_between_two_logins(home):
    publish(1)
    lg = Login()
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.LOGGED_IN
    res = R(2, _verify=verify_by_gen(10), _login=lg)
    assert res.outcome == S.COOLDOWN and lg.calls == 1


def test_daily_cap_cools_down_then_allows_one_login_per_period(home, monkeypatch):
    """Review F2: the cap used to re-arm itself until midnight."""
    monkeypatch.setattr(S, 'FLOORS', {})
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, max_logins_per_day=1, cooldown_sec=0.0,
                                            cap_cooldown_sec=0.5))
    publish(1)
    lg = Login()
    assert R(1, _verify=verify_by_gen(99), _login=lg).outcome == S.LOGGED_IN
    res = R(2, _verify=verify_by_gen(99), _login=lg)
    assert res.outcome == S.COOLDOWN and 'cap' in res.detail and res.retry_after > 0
    assert S.read_state()['latched'] is None
    assert 'daily_cap' in (home / 'alerts.jsonl').read_text(encoding='utf-8')
    time.sleep(0.6)
    assert R(2, _verify=verify_by_gen(99), _login=lg).outcome == S.LOGGED_IN   # one per period
    assert R(3, _verify=verify_by_gen(99), _login=lg).outcome == S.COOLDOWN    # re-armed
    S.unblock('test')
    assert S.read_state()['logins_today'] == 0
    assert R(3, _verify=verify_by_gen(99), _login=lg).outcome == S.LOGGED_IN


def test_network_failures_never_reached_neo_and_do_not_count(home, fast):
    publish(1)
    R(1, _verify=verify_by_gen(9), _login=Login('network'))
    assert S.read_state()['logins_today'] == 0


def test_day_rollover_resets_the_count(home, fast):
    publish(1)
    st = S.read_state(); st.update(day='2000-01-01', logins_today=99, cap_until=0.0)
    S._write_state(st)
    assert R(1, _verify=verify_by_gen(9), _login=Login()).outcome == S.LOGGED_IN
    assert S.read_state()['logins_today'] == 1


def test_a_refused_mpin_latches_and_nothing_logs_in_until_unblock(home, fast):
    publish(1)
    lg = Login('mpin')
    assert R(1, _verify=verify_by_gen(10), _login=lg).outcome == S.BLOCKED
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

def test_no_secret_in_logs_files_or_status(home, caplog, fast):
    """Review F7: the fakes never echoed a secret, so the old test proved nothing.
    Here NEO's reply ECHOES every credential value, through the real
    `_network_login`, and none may come out anywhere but the 0600 session file."""
    caplog.set_level('DEBUG')
    publish(1)
    echo = 'bad request for %s %s %s pin %s' % (CREDS['mobile_number'], CREDS['ucc'],
                                              CREDS['consumer_key'], CREDS['mpin'])

    def echoing_login(creds, cfg):
        return S._network_login(creds, cfg, http=_HTTP(
            _R(200, {'data': {'token': 'v', 'sid': 's'}}), _R(400, {'errMsg': echo})))
    R(1, _verify=verify_by_gen(10), _login=echoing_login)
    S.unblock('t')
    R(1, _verify=verify_by_gen(10), _login=Login('network'))
    S.unblock('t')
    R(1, _verify=verify_by_gen(10), _login=Login())
    texts = [caplog.text, json.dumps(S.status()),
             (home / 'state.json').read_text(encoding='utf-8')]
    for name in ('refresh_log.jsonl', 'alerts.jsonl'):
        if (home / name).exists():
            texts.append((home / name).read_text(encoding='utf-8'))
    for t in texts:
        for sec in SECRETS:
            assert sec not in t, (sec, t[:200])


# ── real processes: single flight, and a kill mid-login ─────────────────────

def _race_worker(home, counter):
    S.HOME = Path(home)
    while not os.path.exists(counter + '.go'):            # start barrier: all at once
        time.sleep(0.01)

    def login(creds, cfg):
        with open(counter, 'a') as f:
            f.write('login\n')
        time.sleep(0.5)
        return {'token': 'TOKEN-r', 'sid': 'SID-r', 'base_url': 'https://cis.kotaksecurities.com',
                'server_id': None}
    res = S.refresh(1, 'racer', _verify=verify_by_gen(1), _login=login, _creds=fake_creds,
                    _sleep=lambda sec: None)
    with open(counter + '.out', 'a') as f:
        f.write(res.outcome + '\n')


def test_eight_processes_that_see_one_death_log_in_once(home, tmp_path):
    publish(1)
    counter = str(tmp_path / 'logins')
    ctx = mp.get_context('spawn')
    ps = [ctx.Process(target=_race_worker, args=(str(home), counter)) for _ in range(8)]
    for p in ps:
        p.start()
    time.sleep(3)                                         # every worker reaches the barrier
    open(counter + '.go', 'w').close()
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
    S.refresh(1, 'hanger', _verify=verify_by_gen(1), _login=login, _creds=fake_creds,
              _sleep=lambda sec: None)


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
              _R(200, {'data': {'token': 'T', 'sid': 'S', 'baseUrl': 'https://x.kotaksecurities.com/',
                                'hsServerId': 'H'}}))
    out = S._network_login(CREDS, CFG, http=h)
    assert out == {'token': 'T', 'sid': 'S', 'base_url': 'https://x.kotaksecurities.com', 'server_id': 'H'}
    assert h.calls[0].endswith(S.LOGIN_PATH) and h.calls[1].endswith(S.VALIDATE_PATH)


@pytest.mark.parametrize('answers,kind', [
    ((_R(503, None),), 'network'),
    ((_R(401, {'message': 'Invalid TOTP'}),), 'refused'),
    ((_R(200, {'data': {'token': 'v', 'sid': 's'}}), _R(403, None)), 'refused'),
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


SESS = S.Session(1, 'd', 'c', 'https://x.kotaksecurities.com', 'T', 'S', None, 'CK', 'U')


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


# -- review F3/F4/F5/F6/F9/F10 ---------------------------------------------------

def test_an_unexpected_exception_in_login_is_a_failure_with_backoff(home):
    publish(1)

    def boom(creds, cfg):
        raise RuntimeError('library bug')
    res = R(1, _verify=verify_by_gen(9), _login=boom)
    assert res.outcome == S.FAILED and res.retry_after > 0
    st = S.read_state()
    assert st['last_attempt']['outcome'] == 'failed:error' and st['latched'] is None


def test_a_bad_totp_secret_inside_login_latches_as_credentials(home):
    publish(1)

    def bad(creds, cfg):
        raise S.CredentialsError('totp_secret is not valid base32')
    assert R(1, _verify=verify_by_gen(9), _login=bad).outcome == S.BLOCKED
    assert S.read_state()['latched'].startswith('credentials')


def test_a_publish_failure_after_login_is_recorded_and_backs_off(home, monkeypatch):
    publish(1)
    real = S._write_private

    def flaky(path, obj):
        if path.name == 'session.json':
            raise OSError(28, 'No space left on device')
        return real(path, obj)
    monkeypatch.setattr(S, '_write_private', flaky)
    res = R(1, _verify=verify_by_gen(9), _login=Login())
    assert res.outcome == S.FAILED and 'error' in res.detail
    assert S.read_state()['last_attempt']['outcome'] == 'failed:error'


def test_a_failed_write_leaves_no_tmp(home, monkeypatch):
    def boom(fd):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(S.os, 'fsync', boom)
    with pytest.raises(OSError):
        S._write_private(home / 'x.json', {'a': 1})
    assert not (home / 'x.json.tmp').exists()


def test_a_session_from_another_schema_is_never_replaced(home):
    (home / 'session.json').write_text(json.dumps({'schema': 2, 'generation': 9}),
                                       encoding='utf-8')
    lg = Login()
    res = R(9, _verify=lambda s: pytest.fail('no verify'), _login=lg)
    assert res.outcome == S.ERROR and 'upgrade this clone' in res.detail and lg.calls == 0


def test_three_refusals_latch_but_one_does_not(home, fast):
    publish(1)
    lg = Login('refused', 'refused', 'refused')
    assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == S.FAILED
    st = S.read_state(); st['next_allowed_at'] = 0; S._write_state(st)
    assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == S.FAILED
    assert S.read_state()['latched'] is None
    st = S.read_state(); st['next_allowed_at'] = 0; S._write_state(st)
    assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == S.BLOCKED


def test_a_single_dead_read_not_confirmed_does_not_log_in(home):
    publish(4)
    answers = [S.DEAD, S.ALIVE]
    lg = Login()
    res = R(4, _verify=lambda s: answers.pop(0), _login=lg)
    assert res.outcome == S.STILL_ALIVE and lg.calls == 0


def test_an_alive_verdict_is_cached_briefly(home):
    publish(4)
    calls = []
    R(4, _verify=lambda s: calls.append(1) or S.ALIVE, _login=Login())
    R(4, _verify=lambda s: calls.append(1) or S.ALIVE, _login=Login())
    assert len(calls) == 1


def test_scheduled_does_not_log_in_blind_when_neo_is_unreachable(home):
    publish(3)
    lg = Login()
    res = S.scheduled_login(_verify=lambda s: S.UNAVAILABLE, _login=lg, _creds=fake_creds,
                            _sleep=lambda x: None)
    assert res.outcome == S.UNAVAILABLE and lg.calls == 0


def test_config_is_clamped_and_login_url_pinned(home):
    (home / 'config.json').write_text(json.dumps({
        'cooldown_sec': 0, 'max_logins_per_day': 500, 'login_url': 'http://evil.example',
        'cap_cooldown_sec': 'x'}), encoding='utf-8')
    c = S.config()
    assert c['cooldown_sec'] == S.FLOORS['cooldown_sec']
    assert c['max_logins_per_day'] == S.CEILINGS['max_logins_per_day']
    assert c['login_url'] == S.DEFAULTS['login_url']
    assert c['cap_cooldown_sec'] == S.DEFAULTS['cap_cooldown_sec']


def test_a_session_pointing_off_kotak_fails_closed(home):
    publish(2)
    raw = json.loads((home / 'session.json').read_text(encoding='utf-8'))
    raw['base_url'] = 'https://evil.example'
    (home / 'session.json').write_text(json.dumps(raw), encoding='utf-8')
    with pytest.raises(S.SessionUnavailable, match='base_url'):
        S.current()


def test_a_login_returning_an_off_kotak_base_url_is_not_published(home, fast):
    publish(1)

    def evil(creds, cfg):
        return {'token': 'T', 'sid': 'S', 'base_url': 'https://evil.example', 'server_id': None}
    res = R(1, _verify=verify_by_gen(9), _login=evil)
    assert res.outcome == S.FAILED and S.current().generation == 1


# -- the second-login measurement ------------------------------------------------

@pytest.mark.parametrize('a_reads,verdict,published', [
    ([S.DEAD] * 3, 'KILLS', 2), ([S.ALIVE] * 3, 'BOTH', None), ([S.ALIVE, S.DEAD, S.DEAD], 'MIXED', None)])
def test_measure_second_login(home, a_reads, verdict, published):
    publish(1)
    reads = [S.ALIVE] + list(a_reads)        # first: A verifies before the test

    def verify(s):
        return reads.pop(0) if s.generation == 1 else S.ALIVE
    lg = Login()
    out = S.measure_second_login(_verify=verify, _login=lg, _creds=fake_creds,
                                 _sleep=lambda x: None)
    assert verdict in out['verdict'] and lg.calls == 1 and out.get('published') == published
    assert S.read_state()['logins_today'] == 1
    if published:
        assert S.current().generation == 2


def test_measure_refuses_without_a_live_session_or_headroom(home, monkeypatch):
    lg = Login()
    out = S.measure_second_login(_verify=lambda s: S.ALIVE, _login=lg, _creds=fake_creds)
    assert out['verdict'].startswith('REFUSED') and lg.calls == 0
    publish(1)
    monkeypatch.setattr(S, 'DEFAULTS', dict(S.DEFAULTS, max_logins_per_day=1))
    out = S.measure_second_login(_verify=lambda s: S.ALIVE, _login=lg, _creds=fake_creds)
    assert 'headroom' in out['verdict'] and lg.calls == 0


# ── B3 (review N6-N8, 2026-11-27): streaks are per day; a morning without a session alerts ──

def _alerts(home):
    p = home / 'alerts.jsonl'
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()]


def _as_yesterday():
    st = S.read_state()
    st['day'] = (S._now() - timedelta(days=1)).strftime('%Y-%m-%d')
    st['next_allowed_at'] = 0.0
    S._write_state(st)


def test_yesterdays_refusals_do_not_latch_this_morning(home, fast):
    publish(1, day=1)
    lg = Login('refused', 'refused')
    assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == S.FAILED
    st = S.read_state(); st['next_allowed_at'] = 0; S._write_state(st)
    assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == S.FAILED   # 2 refusals yesterday
    _as_yesterday()
    lg2 = Login('refused')                                                  # today's 1st
    assert R(1, _verify=verify_by_gen(9), _login=lg2).outcome == S.FAILED
    assert S.read_state()['latched'] is None
    assert S.read_state()['consecutive_refusals'] == 1


def test_negative_control_three_refusals_on_one_day_still_latch(home, fast):
    publish(1)
    lg = Login('refused', 'refused', 'refused')
    for want in (S.FAILED, S.FAILED, S.BLOCKED):
        assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == want
        st = S.read_state(); st['next_allowed_at'] = 0; S._write_state(st)


def test_the_first_failure_of_a_new_day_alerts_again(home, fast):
    publish(1, day=1)
    lg = Login('network', 'network')
    R(1, _verify=verify_by_gen(9), _login=lg)
    _as_yesterday()
    before = len([a for a in _alerts(home) if a['kind'] == 'login_failed'])
    R(1, _verify=verify_by_gen(9), _login=lg)
    after = len([a for a in _alerts(home) if a['kind'] == 'login_failed'])
    assert before == 1 and after == 2


def test_a_latch_survives_the_day_roll(home, fast):
    publish(1)
    assert R(1, _verify=verify_by_gen(9), _login=Login('mpin')).outcome == S.BLOCKED
    _as_yesterday()
    lg = Login()
    assert R(1, _verify=verify_by_gen(9), _login=lg).outcome == S.BLOCKED and lg.calls == 0


def test_a_morning_without_todays_session_alerts(home):
    publish(3, day=1)                                    # yesterday's session only
    res = S.scheduled_login(_verify=lambda s: S.ALIVE, _login=Login('network'),
                            _creds=fake_creds, _sleep=lambda x: None)
    assert res.outcome == S.FAILED                       # the replacement login failed
    kinds = [a['kind'] for a in _alerts(home)]
    assert 'no_session_today' in kinds


def test_a_good_morning_does_not_alert(home):
    publish(3)                                           # today's session verifies
    res = S.scheduled_login(_verify=lambda s: S.ALIVE, _login=Login(), _creds=fake_creds,
                            _sleep=lambda x: None)
    assert res.outcome == S.NOOP
    assert 'no_session_today' not in [a['kind'] for a in _alerts(home)]


# -- review 2026-11-28: K3 clock before the TOTP, K13 measure's failed login ------

def test_an_unsynced_clock_sends_nothing_and_says_clock(monkeypatch):
    monkeypatch.setattr(S, 'clock_synced', lambda: False)
    h = _HTTP()
    with pytest.raises(S.LoginFailed) as ei:
        S._network_login(CREDS, CFG, http=h)
    assert ei.value.kind == 'clock' and h.calls == []       # no TOTP went to NEO


@pytest.mark.parametrize('synced', [True, None])
def test_a_synced_or_unreadable_clock_does_not_block(monkeypatch, synced):
    monkeypatch.setattr(S, 'clock_synced', lambda: synced)
    h = _HTTP(_R(200, {'data': {'token': 'v', 'sid': 's'}}),
              _R(200, {'data': {'token': 'T', 'sid': 'S', 'baseUrl': 'https://x.kotaksecurities.com',
                                'hsServerId': 'H'}}))
    assert S._network_login(CREDS, CFG, http=h)['token'] == 'T'


def test_clock_failures_never_latch_the_day(home, fast):
    publish(1)
    lg = Login('clock', 'clock', 'clock', 'clock')
    for _ in range(4):
        res = R(1, _verify=verify_by_gen(1), _login=lg)
        assert res.outcome == S.FAILED
    st = S.read_state()
    assert not st['latched'] and st['consecutive_refusals'] == 0 and st['logins_today'] == 0


def test_negative_control_refusals_still_latch(home, fast):
    publish(1)
    lg = Login('refused', 'refused', 'refused')
    outs = [R(1, _verify=verify_by_gen(1), _login=lg).outcome for _ in range(3)]
    assert outs[-1] == S.BLOCKED and S.read_state()['latched']


def test_measure_second_login_books_a_failed_login_b(home):
    publish(1)
    out = S.measure_second_login(_verify=lambda s: S.ALIVE, _login=Login('refused'),
                                 _creds=fake_creds, _sleep=lambda x: None)
    st = S.read_state()
    assert out['verdict'].startswith('REFUSED: login B failed (refused)')
    assert st['last_attempt']['outcome'] == 'failed:refused'   # was stuck at in_flight
    assert st['consecutive_refusals'] == 1

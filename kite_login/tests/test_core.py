"""kite_login.core: NOOP on a good token, one login otherwise, never a wrong-account write."""
import json
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from kite_login import core as C  # noqa: E402

IST = C.IST
NOW = datetime(2026, 11, 30, 8, 40, 0, tzinfo=IST)
CREDS = {'personal': {'api_key': 'pkey1234', 'api_secret': 'psecret99', 'user_id': 'PU0001',
                      'password': 'ppass777', 'totp_secret': 'JBSW Y3DP EHPK 3PXP'},
         'company': {'api_key': 'ckey5678', 'api_secret': 'csecret88', 'user_id': 'CU0002',
                     'password': 'cpass666', 'totp_secret': 'JBSWY3DPEHPK3PXQ'}}


# -- fake Kite --------------------------------------------------------------------------
class Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body, headers or {}
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        if self._body is None:
            raise ValueError('no json')
        return self._body


class FakeKite:
    """Scripted Kite. `valid` = access tokens Kite accepts -> their user_id."""

    def __init__(self):
        self.valid = {}
        self.logins = 0
        self.issue = {}            # api_key -> (token, user_id it belongs to)
        self.profile_down = False
        self.password_refused = False
        self.calls = []

    def factory(self):
        kite = self

        class S:
            headers = {}

            def get(self, url, **kw):
                kite.calls.append(('GET', url))
                if url.endswith('/user/profile'):
                    if kite.profile_down:
                        raise ConnectionError('down')
                    tok = kw['headers']['Authorization'].split(':', 1)[1]
                    if tok in kite.valid:
                        return Resp(200, {'status': 'success', 'data': {'user_id': kite.valid[tok]}})
                    return Resp(403, {'status': 'error'})
                if '/connect/login' in url and kw.get('allow_redirects') is False:
                    return Resp(302, headers={'Location': 'http://127.0.0.1/cb?request_token=RT1&status=success'})
                return Resp(200, {})

            def post(self, url, data=None, **kw):
                kite.calls.append(('POST', url))
                if url.endswith('/api/login'):
                    if kite.password_refused:
                        # worst case: Kite echoes the submitted password back
                        return Resp(200, {'status': 'error',
                                          'message': 'Invalid password %s' % data['password']})
                    return Resp(200, {'status': 'success', 'data': {'request_id': 'RQ'}})
                if url.endswith('/api/twofa'):
                    assert data['twofa_value'] == '123456'
                    return Resp(200, {'status': 'success'})
                if url.endswith('/session/token'):
                    kite.logins += 1
                    tok, uid = kite.issue[data['api_key']]
                    kite.valid[tok] = uid
                    return Resp(200, {'status': 'success', 'data': {'access_token': tok}})
                raise AssertionError(url)
        return S()


@pytest.fixture
def env(tmp_path, monkeypatch):
    root = tmp_path / 'BOTS'
    (root / 'data').mkdir(parents=True)
    (root / 'data' / 'kite_credentials.json').write_text(json.dumps(CREDS))
    monkeypatch.setenv('KITE_LOGIN_HOME', str(tmp_path / 'home'))
    (tmp_path / 'home').mkdir()
    (tmp_path / 'home' / 'LOGIN_HOST').write_text(socket.gethostname())
    cfg = C.load_config()
    kite = FakeKite()
    kite.issue = {'pkey1234': ('NEWP', 'PU0001'), 'ckey5678': ('NEWC', 'CU0002')}
    return root, cfg, kite


def _run(env, aliases=None, now=NOW):
    root, cfg, kite = env
    return C.run(aliases, cfg, root, now_fn=lambda: now, session_factory=kite.factory,
                 totp_now=lambda s: '123456')


def _tok(root, rel):
    return json.loads((root / rel).read_text())


def _plant(root, rel, alias, token, born):
    c = CREDS[alias]
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(json.dumps({'access_token': token, 'api_key': c['api_key'],
                                        'user_id': c['user_id'], 'generated_at': born,
                                        'valid_until': '6:00 AM IST next day'}))


# -- the morning --------------------------------------------------------------------------
def test_fresh_morning_logs_in_both_and_keeps_each_files_old_shape(env):
    root, cfg, kite = env
    out = _run(env)
    assert [r.outcome for r in out] == [C.LOGGED_IN, C.LOGGED_IN]
    p = _tok(root, 'data/kite_access_token.json')
    c = _tok(root, 'FIFTY/data/kite_access_token.json')
    assert set(p) == set(c) == {'access_token', 'api_key', 'user_id', 'generated_at', 'valid_until'}
    assert p['access_token'] == 'NEWP' and p['user_id'] == 'PU0001'
    assert c['access_token'] == 'NEWC' and c['user_id'] == 'CU0002'
    # SNAIL wrote naive local time; FIFTY wrote +05:30. Readers parse each their own way.
    assert p['generated_at'] == '2026-11-30T08:40:00'
    assert c['generated_at'] == '2026-11-30T08:40:00+05:30'


def test_second_slot_is_a_noop_no_login(env):
    root, cfg, kite = env
    _run(env)
    out = _run(env, now=NOW + timedelta(minutes=6))
    assert [r.outcome for r in out] == [C.NOOP, C.NOOP]
    assert kite.logins == 2


def test_old_writers_token_from_today_is_adopted_not_replaced(env):
    """Cutover morning: SNAIL/FIFTY may have written first. Their token is kept."""
    root, cfg, kite = env
    _plant(root, 'data/kite_access_token.json', 'personal', 'SNAILTOK', '2026-11-30T08:39:00')
    kite.valid['SNAILTOK'] = 'PU0001'
    out = _run(env, ['personal'])
    assert out[0].outcome == C.NOOP and kite.logins == 0
    assert _tok(root, 'data/kite_access_token.json')['access_token'] == 'SNAILTOK'


def test_yesterdays_token_is_replaced(env):
    root, cfg, kite = env
    _plant(root, 'data/kite_access_token.json', 'personal', 'OLD', '2026-11-29T08:45:00')
    kite.valid['OLD'] = 'PU0001'          # even if Kite still answered, it dies at 06:00
    out = _run(env, ['personal'])
    assert out[0].outcome == C.LOGGED_IN
    assert _tok(root, 'data/kite_access_token.json')['access_token'] == 'NEWP'


def test_token_before_six_today_is_stale(env):
    root, cfg, kite = env
    _plant(root, 'data/kite_access_token.json', 'personal', 'NIGHT', '2026-11-30T05:59:00')
    kite.valid['NIGHT'] = 'PU0001'
    assert _run(env, ['personal'])[0].outcome == C.LOGGED_IN


def test_todays_token_kite_refuses_is_replaced(env):
    root, cfg, kite = env
    _plant(root, 'data/kite_access_token.json', 'personal', 'KILLED', '2026-11-30T08:30:00')
    out = _run(env, ['personal'])
    assert out[0].outcome == C.LOGGED_IN and kite.logins == 1
    assert _tok(root, 'data/kite_access_token.json')['access_token'] == 'NEWP'


def test_todays_token_unverifiable_is_left_alone(env):
    """Kite unreachable: a login could replace a good token. Report, don't guess."""
    root, cfg, kite = env
    _plant(root, 'data/kite_access_token.json', 'personal', 'MAYBE', '2026-11-30T08:30:00')
    kite.profile_down = True
    out = _run(env, ['personal'])
    assert out[0].outcome == C.UNVERIFIED and not out[0].ok
    assert kite.logins == 0
    assert _tok(root, 'data/kite_access_token.json')['access_token'] == 'MAYBE'


def test_company_token_never_lands_in_the_shared_file(env):
    """Credentials mixed up: Kite issues the token to another user -> NOT written."""
    root, cfg, kite = env
    kite.issue['pkey1234'] = ('WRONG', 'CU0002')
    _plant(root, 'data/kite_access_token.json', 'personal', 'OLD', '2026-11-29T08:45:00')
    out = _run(env, ['personal'])
    assert out[0].outcome == C.FAILED and 'NOT written' in out[0].detail
    assert _tok(root, 'data/kite_access_token.json')['access_token'] == 'OLD'


def test_token_of_another_account_in_the_file_is_not_trusted(env):
    root, cfg, kite = env
    _plant(root, 'data/kite_access_token.json', 'company', 'COMP', '2026-11-30T08:30:00')
    kite.valid['COMP'] = 'CU0002'
    out = _run(env, ['personal'])
    assert out[0].outcome == C.LOGGED_IN
    assert _tok(root, 'data/kite_access_token.json')['user_id'] == 'PU0001'


def test_no_login_host_no_login(env, tmp_path):
    root, cfg, kite = env
    (tmp_path / 'home' / 'LOGIN_HOST').unlink()
    out = _run(env)
    assert {r.outcome for r in out} == {C.HOST_REFUSED} and kite.logins == 0
    assert not (root / 'data' / 'kite_access_token.json').exists()


def test_wrong_host_name_no_login(env, tmp_path):
    (tmp_path / 'home' / 'LOGIN_HOST').write_text('some-other-box')
    root, cfg, kite = env
    assert {r.outcome for r in _run(env)} == {C.HOST_REFUSED}


def test_login_refused_never_leaks_the_password_and_other_account_still_runs(env):
    root, cfg, kite = env
    kite.password_refused = True
    out = _run(env)
    assert all(r.outcome == C.FAILED for r in out)
    assert 'ppass777' not in out[0].detail and '<password>' in out[0].detail
    log = (Path(C.home()) / 'login_log.jsonl').read_text()
    for c in CREDS.values():
        for k in ('password', 'api_secret', 'totp_secret'):
            assert c[k] not in log


def test_missing_credentials_file_fails_closed(env):
    root, cfg, kite = env
    (root / 'data' / 'kite_credentials.json').unlink()
    out = _run(env)
    assert all(r.outcome == C.FAILED and 'missing' in r.detail for r in out)


def test_missing_credential_key_named_not_valued(env):
    root, cfg, kite = env
    bad = json.loads(json.dumps(CREDS))
    bad['company']['totp_secret'] = ''
    (root / 'data' / 'kite_credentials.json').write_text(json.dumps(bad))
    out = {r.account: r for r in _run(env)}
    assert out['personal'].outcome == C.LOGGED_IN
    assert out['company'].outcome == C.FAILED and 'totp_secret' in out['company'].detail


def test_unknown_account_is_a_usage_error(env):
    with pytest.raises(ValueError):
        _run(env, ['nobody'])


def test_lock_held_elsewhere_refuses(env, monkeypatch):
    root, cfg, kite = env
    cfg = dict(cfg, lock_wait_s=0.2)
    with C.file_lock('login.lock', 0) as got:
        assert got
        if sys.platform == 'win32':
            pytest.skip('msvcrt locks are per-process; the flock half runs on the Pi')
        out = C.run(None, cfg, root, now_fn=lambda: NOW, session_factory=kite.factory,
                    totp_now=lambda s: '123456')
    assert all('lock' in r.detail for r in out) and kite.logins == 0


def test_write_is_atomic_no_temp_left(env):
    root, cfg, kite = env
    _run(env)
    assert not [p for p in (root / 'data').iterdir() if '.tmp.' in p.name]


# -- pieces -------------------------------------------------------------------------------
def test_session_start_rolls_at_six():
    assert C.session_start(datetime(2026, 11, 30, 5, 0, tzinfo=IST)).day == 29
    assert C.session_start(datetime(2026, 11, 30, 6, 0, tzinfo=IST)).day == 30


def test_generated_at_reads_both_old_styles():
    a = C.generated_at({'generated_at': '2026-11-27T08:45:04.784085'})
    b = C.generated_at({'generated_at': '2026-11-27T08:50:52.475606+05:30'})
    assert a.hour == 8 and a.minute == 45 and b.minute == 50 and a.tzinfo and b.tzinfo
    assert C.generated_at({'generated_at': 'garbage'}) is None


def test_status_is_offline_and_secret_free(env):
    root, cfg, kite = env
    _run(env)
    s = C.status(cfg, root, now=NOW)
    text = json.dumps(s)
    assert 'NEWP' not in text and 'NEWC' not in text
    assert s['accounts']['personal']['made_this_session'] is True
    assert s['login_host'] == 'yes'


def test_tracked_defaults_carry_no_account_ids():
    text = C.DEFAULTS_FILE.read_text()
    for needle in ('QSK', 'YL6', 'api_secret"', 'password"'):
        assert needle not in text

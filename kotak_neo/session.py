"""The ONE NEO session: published once, read by every bot, refreshed single-flight.

THE MODULE THAT LOGS IN. No other file in BOTS may call NEO's login endpoints
(`tests/test_login_policy.py` fails the commit otherwise). Owner, 2026-11-27:
one account; one morning login; whoever hits an auth failure refreshes; the
bots must never fight; the laptop never logs in.

FILES, in one directory OUTSIDE every git clone (`HOME`, default
`~/.kotak_neo/`, mode 0700) so the Desktop/BOTS clone and the ETF_PI clone see
the same lock and the same session (review F6):

  session.json       the published session (0600). Never logged, never committed.
  state.json         generation counter, cooldown, daily count, latch (0600).
  refresh.lock       the single-flight lock (flock).
  refresh_log.jsonl  every request and outcome. No secrets.
  alerts.jsonl       what needs a human (a Telegram sender reads it later).
  LOGIN_HOST         MUST exist and hold this machine's hostname for a login to
                     be attempted. Only the Pi has it; the laptop never logs in.
  config.json        optional overrides (credentials path, login URL, limits).

THE RULES (design + Fable review F5/F7/F8):
  1. `current()` never logs in. A missing or broken session fails CLOSED.
  2. A client asks `refresh(failed_generation=g)` with the generation its
     FAILED call used. If a newer one is already published -> ADOPTED, no login.
     That is what stops two bots that saw the same death logging in twice.
  3. Before any login the published session is VERIFIED with one read. Alive
     -> STILL_ALIVE, no login: one client's local trouble cannot kill the
     session every other bot is using. Network trouble -> UNAVAILABLE, no login.
  4. A refused ORDER is never evidence for a login (F5): the caller treats it
     as unclear and reconciles. Only a READ-proven death (rule 3) logs in.
  5. The attempt, the daily count and the next allowed time are WRITTEN BEFORE
     the login call (F7): a process killed mid-login leaves its cooldown behind,
     so a cron that restarts every minute cannot turn into a login storm.
  6. Brakes: a cooldown between logins; a daily cap that imposes a LONG cooldown
     and alerts (never a latch -- a latch at 09:00 with a position open would wait
     on a human, F8); a hard latch ONLY when retrying cannot help and could lock
     the account: a credentials problem or a refused MPIN. `unblock` clears it.
  7. Generation only ever rises; its source of truth is state.json, written
     before session.json, so a corrupt or deleted session never resets it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import socket
import struct
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, NamedTuple, Optional

from .envelope import is_ok, is_session_dead, neo_msg, scrub

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

#: Where everything lives. Tests point it at a temp dir; nothing else should.
HOME = Path(os.path.expanduser('~')) / '.kotak_neo'

DEFAULTS = {
    'credentials_file': str(Path(os.path.expanduser('~')) / 'Desktop' / 'BOTS' / 'data'
                            / 'neo_credentials.json'),
    'login_url': 'https://mis.kotaksecurities.com',
    'login_timeout_sec': 20.0,
    'verify_timeout_sec': 10.0,
    'lock_wait_sec': 75.0,          # a waiter gives up after this; it re-reads, never logs in
    'cooldown_sec': 60.0,           # between two login ATTEMPTS, success or not
    'max_logins_per_day': 8,
    'cap_cooldown_sec': 1800.0,     # what the cap imposes instead of a latch
    'network_backoff_max_sec': 300.0,
}

LOGIN_PATH = '/login/1.0/tradeApiLogin'
VALIDATE_PATH = '/login/1.0/tradeApiValidate'
VERIFY_PATH = '/quick/user/limits'
NEO_FIN_KEY = 'neotradeapi'
CRED_KEYS = ('consumer_key', 'mobile_number', 'ucc', 'mpin', 'totp_secret')
SESSION_KEYS = ('generation', 'trading_day', 'created_at', 'base_url', 'token', 'sid',
                'consumer_key', 'ucc')
SCHEMA = 1

# -- outcomes ------------------------------------------------------------------------
ADOPTED = 'ADOPTED'            # a newer generation was already published
STILL_ALIVE = 'STILL_ALIVE'    # the published session verifies; no login
LOGGED_IN = 'LOGGED_IN'        # this call logged in and published a new generation
NOOP = 'NOOP'                  # scheduled login: today's session already verifies
COOLDOWN = 'COOLDOWN'          # too soon after the last attempt (or the daily cap)
BLOCKED = 'BLOCKED'            # latched; only `unblock` clears it
UNAVAILABLE = 'UNAVAILABLE'    # network / NEO down; a login cannot fix it
LOCK_TIMEOUT = 'LOCK_TIMEOUT'  # another process held the lock past lock_wait_sec
HOST_REFUSED = 'HOST_REFUSED'  # this machine may not log in (no LOGIN_HOST match)
FAILED = 'FAILED'              # the login itself failed (detail says why)

ALIVE, DEAD = 'ALIVE', 'DEAD'


class SessionUnavailable(RuntimeError):
    """No usable published session. Callers fail closed."""


class CredentialsError(ValueError):
    pass


class LoginFailed(RuntimeError):
    """kind: 'network' (retry later), 'totp' (next window), 'mpin' and
    'credentials' (latch: retrying could lock the account)."""

    def __init__(self, kind: str, msg: str):
        super().__init__(msg)
        self.kind = kind


class Session(NamedTuple):
    generation: int
    trading_day: str
    created_at: str
    base_url: str
    token: str
    sid: str
    server_id: Optional[str]
    consumer_key: str
    ucc: str

    def __repr__(self) -> str:                      # never print a token
        return 'Session(generation=%d, trading_day=%s, base_url=%s)' % (
            self.generation, self.trading_day, self.base_url)


class Result(NamedTuple):
    outcome: str
    session: Optional[Session]
    detail: str = ''


# -- config / paths ----------------------------------------------------------------------
def config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    p = HOME / 'config.json'
    if p.exists():
        try:
            with open(p, encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            logger.error('kotak_neo config.json unreadable (%s) -- using defaults', e)
    return cfg


def _p(name: str) -> Path:
    return HOME / name


def _ensure_home() -> None:
    HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(str(HOME), 0o700)
    except OSError:
        pass


def _now() -> datetime:
    return datetime.now(IST)


def _write_private(path: Path, obj: Any) -> None:
    """Atomic write, 0600 from creation (never world-readable for an instant)."""
    _ensure_home()
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(str(tmp), str(path))
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass


def _append(name: str, row: Dict[str, Any]) -> None:
    try:
        _ensure_home()
        with open(str(_p(name)), 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, sort_keys=True) + '\n')
    except OSError as e:
        logger.error('kotak_neo: could not append %s: %s', name, e)


# -- the lock ---------------------------------------------------------------------------
@contextmanager
def _lock(wait_s: float) -> Iterator[bool]:
    """Exclusive, cross-process, released by the kernel if the holder dies.
    Yields False when it could not be had within `wait_s`. POSIX flock on the
    Pi; msvcrt on Windows exists ONLY so the test suite runs on the laptop
    (which can never log in: no LOGIN_HOST)."""
    _ensure_home()
    fd = os.open(str(_p('refresh.lock')), os.O_RDWR | os.O_CREAT, 0o600)
    got = False
    try:
        deadline = time.time() + max(0.0, wait_s)
        while True:
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.1)
        yield got
    finally:
        if got:
            try:
                if os.name == 'nt':
                    import msvcrt
                    os.lseek(fd, 0, 0)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)


# -- state ------------------------------------------------------------------------------
def _fresh_state() -> Dict[str, Any]:
    return {'schema': SCHEMA, 'last_generation': 0, 'day': '', 'logins_today': 0,
            'next_allowed_at': 0.0, 'latched': None, 'consecutive_failures': 0,
            'last_attempt': None}


def read_state() -> Dict[str, Any]:
    """state.json. Missing -> fresh. Unreadable -> fresh BUT with the
    generation floor taken from the session (rule 7) and a cooldown, so a
    corrupt state can neither reset the counter nor unleash a login."""
    p = _p('state.json')
    st = _fresh_state()
    if not p.exists():
        return st
    try:
        with open(str(p), encoding='utf-8') as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raise ValueError('not an object')
        st.update(raw)
        return st
    except (OSError, ValueError) as e:
        logger.error('kotak_neo state.json unreadable (%s): rebuilding with a cooldown', e)
        cur = _read_session_raw()
        st['last_generation'] = int((cur or {}).get('generation') or 0)
        st['next_allowed_at'] = time.time() + float(config()['cooldown_sec'])
        return st


def _write_state(st: Dict[str, Any]) -> None:
    _write_private(_p('state.json'), st)


def _roll_day(st: Dict[str, Any], now: datetime) -> None:
    day = now.strftime('%Y-%m-%d')
    if st.get('day') != day:
        st['day'] = day
        st['logins_today'] = 0


# -- the published session ----------------------------------------------------------------
def _read_session_raw() -> Optional[Dict[str, Any]]:
    try:
        with open(str(_p('session.json')), encoding='utf-8') as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else None
    except (OSError, ValueError):
        return None


def current() -> Session:
    """The published session. NEVER logs in. Raises SessionUnavailable on a
    missing, unreadable or malformed file (fails closed). A session from an
    earlier trading day is returned as is: stale is not dead -- NEO decides."""
    p = _p('session.json')
    if not p.exists():
        raise SessionUnavailable('no published NEO session (%s)' % p)
    raw = _read_session_raw()
    if raw is None:
        raise SessionUnavailable('NEO session file unreadable')
    missing = [k for k in SESSION_KEYS if not raw.get(k)]
    if missing:
        raise SessionUnavailable('NEO session file missing %s' % ', '.join(missing))
    if raw.get('schema') != SCHEMA:
        raise SessionUnavailable('NEO session schema %r' % raw.get('schema'))
    try:
        gen = int(raw['generation'])
    except (TypeError, ValueError):
        raise SessionUnavailable('NEO session generation not an integer')
    if gen < 1:
        raise SessionUnavailable('NEO session generation %d' % gen)
    return Session(gen, str(raw['trading_day']), str(raw['created_at']),
                   str(raw['base_url']).rstrip('/'), str(raw['token']), str(raw['sid']),
                   raw.get('server_id'), str(raw['consumer_key']), str(raw['ucc']))


def _current_or_none() -> Optional[Session]:
    try:
        return current()
    except SessionUnavailable:
        return None


def _publish(st: Dict[str, Any], cur: Optional[Session], fresh: Dict[str, Any],
             creds: Dict[str, str], now: datetime) -> Session:
    gen = max(int(st.get('last_generation') or 0), cur.generation if cur else 0) + 1
    st['last_generation'] = gen
    _write_state(st)                                  # FIRST: the counter survives
    _write_private(_p('session.json'), {
        'schema': SCHEMA, 'generation': gen, 'trading_day': now.strftime('%Y-%m-%d'),
        'created_at': now.isoformat(timespec='seconds'), 'base_url': fresh['base_url'],
        'token': fresh['token'], 'sid': fresh['sid'], 'server_id': fresh.get('server_id'),
        'consumer_key': creds['consumer_key'], 'ucc': creds['ucc']})
    return current()


# -- credentials, TOTP, IPv4 -----------------------------------------------------------------
def load_credentials(path: Any) -> Dict[str, str]:
    try:
        with open(str(path), encoding='utf-8') as f:
            raw = json.load(f)
    except OSError as exc:
        raise CredentialsError('NEO credentials unreadable at %s: %s' % (path, type(exc).__name__))
    except ValueError:
        raise CredentialsError('NEO credentials at %s are not JSON' % path)
    if not isinstance(raw, dict):
        raise CredentialsError('NEO credentials file must hold a JSON object')
    out = {}
    for k in CRED_KEYS:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            raise CredentialsError('NEO credentials: %s missing or blank' % k)
        out[k] = v.strip()
    if not out['mobile_number'].startswith('+'):
        raise CredentialsError('mobile_number must carry the ISD code (+91...)')
    return out


def totp_now(secret_b32: str, at: Optional[float] = None, step: int = 30,
             digits: int = 6) -> str:
    """RFC 6238 TOTP (SHA-1) in the stdlib."""
    s = secret_b32.strip().replace(' ', '').upper()
    s += '=' * (-len(s) % 8)
    try:
        key = base64.b32decode(s)
    except Exception:
        raise CredentialsError('totp_secret is not valid base32')
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    off = digest[-1] & 0x0F
    code = struct.unpack('>I', digest[off:off + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0,
               flags: int = 0) -> Any:
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def pin_ipv4() -> None:
    """Resolve to IPv4 only, process-wide (the whitelisted static IP is the
    home NAT's IPv4; a session must come from it to place orders)."""
    if socket.getaddrinfo is not _ipv4_only:
        socket.getaddrinfo = _ipv4_only


# -- the network calls ----------------------------------------------------------------------
def _http():
    import requests
    return requests.Session()


def _json(r: Any) -> Optional[Dict[str, Any]]:
    try:
        d = r.json()
    except ValueError:
        return None
    return d if isinstance(d, dict) else None


def _network_login(creds: Dict[str, str], cfg: Dict[str, Any], http: Any = None) -> Dict[str, Any]:
    """TOTP login then MPIN validate. Returns {token, sid, base_url, server_id}.
    Raises LoginFailed with a kind. Never logs a secret."""
    import requests
    http = http or _http()
    pin_ipv4()
    url = str(cfg['login_url']).rstrip('/')
    t = float(cfg['login_timeout_sec'])
    hdr = {'Authorization': creds['consumer_key'], 'neo-fin-key': NEO_FIN_KEY,
           'Content-Type': 'application/json'}

    def call(path: str, headers: Dict[str, str], body: Dict[str, str], kind: str) -> Dict[str, Any]:
        try:
            r = http.post(url + path, headers=headers, data=json.dumps(body), timeout=t)
        except requests.RequestException as exc:
            raise LoginFailed('network', '%s: %s' % (path, type(exc).__name__))
        if r.status_code >= 500 or r.status_code == 429:
            raise LoginFailed('network', '%s: HTTP %s' % (path, r.status_code))
        d = _json(r)
        inner = (d or {}).get('data')
        if r.status_code != 200 or not isinstance(inner, dict):
            raise LoginFailed(kind, '%s: HTTP %s %s' % (path, r.status_code,
                                                        scrub(neo_msg(d) or r.text, 160)))
        return inner

    s1 = call(LOGIN_PATH, hdr, {'mobileNumber': creds['mobile_number'], 'ucc': creds['ucc'],
                                'totp': totp_now(creds['totp_secret'])}, 'totp')
    if not s1.get('token') or not s1.get('sid'):
        raise LoginFailed('totp', 'tradeApiLogin returned no token/sid')
    hdr2 = dict(hdr)
    hdr2.update({'sid': str(s1['sid']), 'Auth': str(s1['token'])})
    s2 = call(VALIDATE_PATH, hdr2, {'mpin': creds['mpin']}, 'mpin')
    if not s2.get('token') or not s2.get('sid') or not s2.get('baseUrl'):
        raise LoginFailed('mpin', 'tradeApiValidate returned no token/sid/baseUrl')
    return {'token': str(s2['token']), 'sid': str(s2['sid']),
            'base_url': str(s2['baseUrl']).rstrip('/'), 'server_id': s2.get('hsServerId')}


def _network_verify(s: Session, cfg: Dict[str, Any], http: Any = None) -> str:
    """ONE cheap read with this session. ALIVE / DEAD / UNAVAILABLE."""
    import requests
    http = http or _http()
    pin_ipv4()
    hdr = {'Auth': s.token, 'Sid': s.sid, 'neo-fin-key': NEO_FIN_KEY,
           'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
    params = {'sId': str(s.server_id)} if s.server_id else {}
    try:
        r = http.post(s.base_url + VERIFY_PATH, headers=hdr, params=params,
                      data={'jData': json.dumps({'seg': 'ALL', 'exch': 'ALL', 'prod': 'ALL'})},
                      timeout=float(cfg['verify_timeout_sec']))
    except requests.RequestException:
        return UNAVAILABLE
    d = _json(r)
    if is_session_dead(r.status_code, d):
        return DEAD
    if r.status_code == 200 and is_ok(d):
        return ALIVE
    return UNAVAILABLE


# -- host binding ----------------------------------------------------------------------------
def login_host_ok() -> bool:
    """A login is attempted only where LOGIN_HOST names this machine."""
    try:
        with open(str(_p('LOGIN_HOST')), encoding='utf-8') as f:
            want = f.read().strip()
    except OSError:
        return False
    return bool(want) and want == socket.gethostname()


# -- the one login path --------------------------------------------------------------------
def _alert(kind: str, text: str, now: datetime) -> None:
    logger.error('NEO SESSION ALERT [%s] %s', kind, text)
    _append('alerts.jsonl', {'at': now.isoformat(timespec='seconds'), 'kind': kind,
                             'text': text})


def _log(requester: str, outcome: str, detail: str, now: datetime,
         failed_generation: Optional[int], generation: Optional[int]) -> None:
    _append('refresh_log.jsonl', {'at': now.isoformat(timespec='seconds'),
                                  'requester': requester, 'outcome': outcome,
                                  'detail': detail[:300],
                                  'failed_generation': failed_generation,
                                  'generation': generation})


def _login_locked(st: Dict[str, Any], cur: Optional[Session], requester: str,
                  now: datetime, cfg: Dict[str, Any], login_fn: Callable,
                  creds_fn: Callable) -> Result:
    """The brakes, then ONE login. Called with the lock held."""
    t = time.time()
    if st.get('latched'):
        return Result(BLOCKED, cur, 'latched: %s -- python -m kotak_neo unblock'
                      % st['latched'])
    if t < float(st.get('next_allowed_at') or 0):
        return Result(COOLDOWN, cur, 'next login allowed in %.0fs'
                      % (float(st['next_allowed_at']) - t))
    _roll_day(st, now)
    if int(st.get('logins_today') or 0) >= int(cfg['max_logins_per_day']):
        st['next_allowed_at'] = t + float(cfg['cap_cooldown_sec'])
        _write_state(st)
        _alert('daily_cap', '%d NEO logins today (cap %d): no login for %.0f min. If no bot '
               'refreshed, a login in the NEO app may be killing the bots\' session.'
               % (st['logins_today'], cfg['max_logins_per_day'],
                  float(cfg['cap_cooldown_sec']) / 60), now)
        return Result(COOLDOWN, cur, 'daily login cap reached')
    if not login_host_ok():
        return Result(HOST_REFUSED, cur, 'this machine may not log in (LOGIN_HOST)')
    try:
        creds = creds_fn(cfg['credentials_file'])
    except CredentialsError as e:
        st['latched'] = 'credentials: %s' % e
        _write_state(st)
        _alert('latched', 'NEO credentials problem: %s' % e, now)
        return Result(BLOCKED, cur, str(e))

    # Rule 5: the attempt is on disk BEFORE the network call.
    st['logins_today'] = int(st.get('logins_today') or 0) + 1
    st['next_allowed_at'] = t + float(cfg['cooldown_sec'])
    st['last_attempt'] = {'at': now.isoformat(timespec='seconds'), 'requester': requester,
                          'outcome': 'in_flight'}
    _write_state(st)
    try:
        fresh = login_fn(creds, cfg)
    except LoginFailed as e:
        st['consecutive_failures'] = int(st.get('consecutive_failures') or 0) + 1
        st['last_attempt']['outcome'] = 'failed:%s' % e.kind
        if e.kind in ('mpin', 'credentials'):
            st['latched'] = '%s: %s' % (e.kind, e)
            _alert('latched', 'NEO login refused (%s): %s. No more logins until '
                   '`python -m kotak_neo unblock`.' % (e.kind, e), now)
        else:
            if e.kind == 'network':                    # doubling backoff, capped
                back = min(float(cfg['cooldown_sec']) * (2 ** (st['consecutive_failures'] - 1)),
                           float(cfg['network_backoff_max_sec']))
                st['next_allowed_at'] = t + max(back, float(cfg['cooldown_sec']))
            if st['consecutive_failures'] == 1:        # once per failing streak
                _alert('login_failed', 'NEO login failed (%s): %s' % (e.kind, e), now)
        _write_state(st)
        return Result(FAILED, cur, '%s: %s' % (e.kind, e))
    st['consecutive_failures'] = 0
    st['last_attempt']['outcome'] = 'ok'
    new = _publish(st, cur, fresh, creds, now)
    return Result(LOGGED_IN, new, 'generation %d' % new.generation)


def refresh(failed_generation: int, requester: str, *, wait_s: Optional[float] = None,
            _verify: Optional[Callable] = None, _login: Optional[Callable] = None,
            _creds: Optional[Callable] = None) -> Result:
    """Called by a client whose READ found the session dead.

    `failed_generation` is the generation the failed call USED (rule 2).
    Never raises. The `_verify/_login/_creds` seams exist for tests.
    """
    now = _now()
    cfg = config()
    verify = _verify or (lambda s: _network_verify(s, cfg))
    login_fn = _login or _network_login
    creds_fn = _creds or load_credentials
    try:
        with _lock(float(cfg['lock_wait_sec'] if wait_s is None else wait_s)) as got:
            if not got:
                res = Result(LOCK_TIMEOUT, _current_or_none(), 'another process is refreshing')
            else:
                st = read_state()
                cur = _current_or_none()
                if cur is not None and cur.generation > int(failed_generation):
                    res = Result(ADOPTED, cur, 'generation %d already published' % cur.generation)
                else:
                    v = verify(cur) if cur is not None else DEAD
                    if v == ALIVE:
                        res = Result(STILL_ALIVE, cur, 'published session verifies')
                    elif v == UNAVAILABLE:
                        res = Result(UNAVAILABLE, cur, 'verify read failed: NEO unreachable')
                    else:
                        res = _login_locked(st, cur, requester, now, cfg, login_fn, creds_fn)
    except Exception as e:                       # never raise into a trading loop
        logger.error('kotak_neo refresh failed: %s', e, exc_info=True)
        res = Result(UNAVAILABLE, _current_or_none(), 'refresh raised: %s' % type(e).__name__)
    _log(requester, res.outcome, res.detail, now, failed_generation,
         res.session.generation if res.session else None)
    if res.outcome in (LOGGED_IN, ADOPTED, STILL_ALIVE):
        logger.info('NEO session %s for %s (%s)', res.outcome, requester, res.detail)
    else:
        logger.warning('NEO session %s for %s (%s)', res.outcome, requester, res.detail)
    return res


def scheduled_login(*, _verify: Optional[Callable] = None, _login: Optional[Callable] = None,
                    _creds: Optional[Callable] = None) -> Result:
    """The morning cron (run twice, 08:40 and 08:55). NOOP when TODAY's session
    verifies; otherwise one login under every brake. A session from an earlier
    day is replaced here -- before the market, when nothing holds an order."""
    now = _now()
    cfg = config()
    verify = _verify or (lambda s: _network_verify(s, cfg))
    try:
        with _lock(float(cfg['lock_wait_sec'])) as got:
            if not got:
                res = Result(LOCK_TIMEOUT, _current_or_none(), 'another process is refreshing')
            else:
                st = read_state()
                cur = _current_or_none()
                today = now.strftime('%Y-%m-%d')
                if cur is not None and cur.trading_day == today and verify(cur) == ALIVE:
                    res = Result(NOOP, cur, "today's session verifies")
                else:
                    res = _login_locked(st, cur, 'scheduled', now, cfg,
                                        _login or _network_login, _creds or load_credentials)
    except Exception as e:
        logger.error('kotak_neo scheduled login failed: %s', e, exc_info=True)
        res = Result(UNAVAILABLE, _current_or_none(), 'raised: %s' % type(e).__name__)
    _log('scheduled', res.outcome, res.detail, now, None,
         res.session.generation if res.session else None)
    return res


def unblock(who: str) -> Dict[str, Any]:
    """Owner action: clear a latch and the cooldown. Logged."""
    now = _now()
    with _lock(float(config()['lock_wait_sec'])) as got:
        if not got:
            raise RuntimeError('lock busy; try again')
        st = read_state()
        was = st.get('latched')
        st['latched'] = None
        st['next_allowed_at'] = 0.0
        st['consecutive_failures'] = 0
        _write_state(st)
    _log(who, 'UNBLOCKED', 'was: %s' % was, now, None, None)
    return {'was': was}


def status() -> Dict[str, Any]:
    """Safe summary: no token, no sid, no ucc."""
    st = read_state()
    cur = _current_or_none()
    return {'home': str(HOME), 'generation': cur.generation if cur else None,
            'trading_day': cur.trading_day if cur else None,
            'created_at': cur.created_at if cur else None,
            'base_url': cur.base_url if cur else None,
            'last_generation': st.get('last_generation'), 'logins_today': st.get('logins_today'),
            'day': st.get('day'), 'latched': st.get('latched'),
            'next_allowed_in_sec': max(0, round(float(st.get('next_allowed_at') or 0)
                                                - time.time())),
            'last_attempt': st.get('last_attempt'), 'login_host_ok': login_host_ok()}

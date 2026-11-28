"""The ONE NEO session: published once, read by every bot, refreshed single-flight.

THE MODULE THAT LOGS IN. No other file in BOTS may call NEO's login endpoints
(`tests/test_login_policy.py` fails the commit otherwise). Owner, 2026-11-27:
one account; one morning login; whoever hits an auth failure refreshes; the
bots must never fight; the laptop never logs in.

FILES, in one directory OUTSIDE every git clone (`HOME`, default
`~/.kotak_neo/`, mode 0700), so the Desktop/BOTS clone and the ETF_PI clone see
the same lock and the same session:

  session.json       the published session (0600). Never logged, never committed.
  state.json         generation counter, cooldown, daily count, latch (0600).
  refresh.lock       the single-flight lock (flock).
  refresh_log.jsonl  every request and outcome. No secrets.
  alerts.jsonl       what needs a human (the alert sender reads it -- K3).
  LOGIN_HOST         MUST exist and hold this machine's hostname for a login to
                     be attempted. Only the Pi has it; the laptop never logs in.
  config.json        optional overrides, CLAMPED to safe floors.

THE RULES:
  1. `current()` never logs in. A missing or broken session fails CLOSED.
  2. A client asks `refresh(failed_generation=g)` with the generation its FAILED
     call used. A newer one already published -> ADOPTED, no login.
  3. Before any login the published session is VERIFIED, and a DEAD verdict is
     CONFIRMED by a second read a moment later (one 401 has been seen 5 s after a
     fresh login, 2026-11-27). Alive -> STILL_ALIVE; unreachable -> UNAVAILABLE.
     Neither logs in. A session file written by a NEWER schema (another clone)
     is never replaced: -> ERROR, "upgrade this clone".
  4. A refused ORDER is never evidence for a login; callers reconcile.
  5. The attempt, the count and the next allowed time are WRITTEN BEFORE the
     login call: kill -9 mid-login leaves its cooldown behind.
  6. Brakes: a cooldown between logins; a daily cap that imposes a 30-minute
     cooldown and then allows ONE login per cooldown (never a latch -- a latch at
     09:00 with a position open waits on a human); a hard latch only where
     retrying could lock the account or cannot help: credentials problem, a
     refused MPIN, or 3 consecutive refused logins. `unblock` clears all of it.
  7. Generation only rises; its source of truth is state.json, written before
     session.json. A corrupt state.json is rebuilt ONCE under the lock (floor from
     the session, one cooldown, an alert) and written back.
  8. No secret leaves the 0600 files: credential values are replaced in every
     message built from a login reply, and login bodies are logged scrubbed.

Review of the first cut (Fable, 2026-11-27, F1-F12) is what rules 3, 6, 7 and
8 now say; each fix has a test that fails on the old behaviour.
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
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, NamedTuple, Optional
from urllib.parse import urlparse

from .envelope import is_ip_refusal, is_ok, is_session_dead, neo_msg, scrub, stcode

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


def _user_home() -> Path:
    """The account's real home, not $HOME (cron and sudo can differ)."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path(os.path.expanduser('~'))


#: Where everything lives. Tests point it at a temp dir; nothing else should.
HOME = _user_home() / '.kotak_neo'

DEFAULTS = {
    'credentials_file': str(_user_home() / 'Desktop' / 'BOTS' / 'data' / 'neo_credentials.json'),
    'login_url': 'https://mis.kotaksecurities.com',
    'login_timeout_sec': 20.0,
    'verify_timeout_sec': 10.0,
    'verify_confirm_sec': 3.0,      # gap before the second, confirming DEAD read
    'verify_cache_sec': 15.0,       # an ALIVE verdict is reused this long, per process
    'lock_wait_sec': 75.0,          # a waiter gives up after this; it re-reads, never logs in
    'cooldown_sec': 60.0,           # between two login ATTEMPTS, success or not
    'max_logins_per_day': 8,
    'cap_cooldown_sec': 1800.0,     # what the cap imposes; then ONE login per period
    'network_backoff_max_sec': 300.0,
    'max_refusals': 3,              # consecutive refused logins (not network) -> latch
    'require_clock_sync': True,     # Linux: no TOTP until timedatectl says synced (K3)
    # The public IPv4 whitelisted on the NEO dashboard. Set ONLY in the Pi's
    # ~/.kotak_neo/config.json (never in git). Empty = the morning check is off.
    'expected_public_ipv4': '',
}
#: Floors a config.json cannot go below (a typo must not unleash a storm).
FLOORS = {'cooldown_sec': 30.0, 'cap_cooldown_sec': 600.0, 'lock_wait_sec': 1.0,
          'login_timeout_sec': 5.0, 'verify_timeout_sec': 2.0}
CEILINGS = {'max_logins_per_day': 12, 'max_refusals': 5}
ALLOWED_HOST_SUFFIX = '.kotaksecurities.com'

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
COOLDOWN = 'COOLDOWN'          # too soon (see Result.retry_after)
BLOCKED = 'BLOCKED'            # latched; only `unblock` clears it
UNAVAILABLE = 'UNAVAILABLE'    # network / NEO down; a login cannot fix it
LOCK_TIMEOUT = 'LOCK_TIMEOUT'  # another process held the lock past lock_wait_sec
HOST_REFUSED = 'HOST_REFUSED'  # this machine may not log in (no LOGIN_HOST match)
FAILED = 'FAILED'              # the login itself failed (detail says why)
ERROR = 'ERROR'                # a bug or a state this code must not touch (detail)

ALIVE, DEAD = 'ALIVE', 'DEAD'
LATCH_KINDS = ('mpin', 'credentials')


class SessionUnavailable(RuntimeError):
    """No usable published session. Callers fail closed."""


class CredentialsError(ValueError):
    pass


class LoginFailed(RuntimeError):
    """kind: 'network' (never reached NEO), 'clock' (the system clock is not
    NTP-synchronised, so the TOTP would be wrong: nothing sent), 'refused' (NEO
    said no, cause not proven), 'mpin' (NEO named the MPIN), 'credentials'
    (our file/secret is bad)."""

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

    __str__ = __repr__


class Result(NamedTuple):
    outcome: str
    session: Optional[Session]
    detail: str = ''
    retry_after: float = 0.0        # seconds; meaningful on COOLDOWN


# -- config / paths ----------------------------------------------------------------------
def _host_ok(url: Any) -> bool:
    u = urlparse(str(url))
    return u.scheme == 'https' and bool(u.hostname) and (
        u.hostname.endswith(ALLOWED_HOST_SUFFIX))


def config() -> Dict[str, Any]:
    """DEFAULTS + config.json, clamped. A bad value is refused, loudly."""
    cfg = dict(DEFAULTS)
    p = HOME / 'config.json'
    if p.exists():
        try:
            with open(str(p), encoding='utf-8') as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                cfg.update({k: v for k, v in raw.items() if k in DEFAULTS})
        except (OSError, ValueError) as e:
            logger.error('kotak_neo config.json unreadable (%s) -- using defaults', e)
    for k, d in DEFAULTS.items():
        if isinstance(d, (int, float)) and not isinstance(d, bool):
            try:
                v = float(cfg[k])
            except (TypeError, ValueError):
                logger.error('kotak_neo config %s=%r is not a number -- default', k, cfg[k])
                v = float(d)
            if k in FLOORS and v < FLOORS[k]:
                logger.error('kotak_neo config %s=%s below floor %s -- floor used', k, v, FLOORS[k])
                v = FLOORS[k]
            if k in CEILINGS and v > CEILINGS[k]:
                logger.error('kotak_neo config %s=%s above ceiling %s -- ceiling used',
                             k, v, CEILINGS[k])
                v = CEILINGS[k]
            cfg[k] = int(v) if isinstance(d, int) else v
    if not _host_ok(cfg['login_url']):
        logger.error('kotak_neo config login_url %r is not https://*%s -- default used',
                     cfg['login_url'], ALLOWED_HOST_SUFFIX)
        cfg['login_url'] = DEFAULTS['login_url']
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


def _fsync_dir(path: Path) -> None:
    if os.name == 'nt':
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_private(path: Path, obj: Any) -> None:
    """Atomic, durable, 0600 from creation. A failed write leaves no .tmp."""
    _ensure_home()
    tmp = path.with_name(path.name + '.tmp')
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(obj, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(path))
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    _fsync_dir(path.parent)


def _append(name: str, row: Dict[str, Any]) -> None:
    try:
        _ensure_home()
        fd = os.open(str(_p(name)), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, 'a', encoding='utf-8') as f:
            f.write(json.dumps(row, sort_keys=True) + '\n')
    except OSError as e:
        logger.error('kotak_neo: could not append %s: %s', name, e)


# -- locks ------------------------------------------------------------------------------
@contextmanager
def file_lock(name: str, wait_s: float) -> Iterator[bool]:
    """Exclusive, cross-process lock on HOME/<name>, released by the kernel if
    the holder dies. Yields False when it could not be had within `wait_s`.
    POSIX flock on the Pi; msvcrt on Windows exists ONLY so the suite runs on
    the laptop (which can never log in: no LOGIN_HOST). The flock half is
    proven by running the suite on the Pi."""
    _ensure_home()
    fd = os.open(str(_p(name)), os.O_RDWR | os.O_CREAT, 0o600)
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
                time.sleep(0.05)
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


def _lock(wait_s: float):
    """The single-flight refresh lock."""
    return file_lock('refresh.lock', wait_s)


# -- state ------------------------------------------------------------------------------
_STATE_NUM = ('last_generation', 'logins_today', 'next_allowed_at', 'consecutive_failures',
              'consecutive_refusals', 'cap_until')


def _fresh_state() -> Dict[str, Any]:
    return {'schema': SCHEMA, 'last_generation': 0, 'day': '', 'logins_today': 0,
            'next_allowed_at': 0.0, 'latched': None, 'consecutive_failures': 0,
            'consecutive_refusals': 0, 'cap_until': 0.0, 'last_attempt': None}


def _parse_state(raw: Any) -> Dict[str, Any]:
    """Raises ValueError on any shape this code cannot trust."""
    if not isinstance(raw, dict):
        raise ValueError('not an object')
    st = _fresh_state()
    st.update(raw)
    for k in _STATE_NUM:
        v = st.get(k)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError('%s is %r' % (k, v))
    st['last_generation'] = int(st['last_generation'])
    st['logins_today'] = int(st['logins_today'])
    if st['latched'] is not None and not isinstance(st['latched'], str):
        raise ValueError('latched is %r' % st['latched'])
    if not isinstance(st.get('day'), str):
        raise ValueError('day is %r' % st.get('day'))
    return st


def read_state() -> Dict[str, Any]:
    """state.json for DISPLAY. Never writes. Broken -> a fresh-looking dict with
    `_corrupt` set (the locked path, `_load_state_locked`, repairs it)."""
    p = _p('state.json')
    if not p.exists():
        return _fresh_state()
    try:
        with open(str(p), encoding='utf-8') as f:
            return _parse_state(json.load(f))
    except (OSError, ValueError) as e:
        st = _fresh_state()
        st['_corrupt'] = str(e)
        return st


def _load_state_locked(cfg: Dict[str, Any], now: datetime) -> Dict[str, Any]:
    """Rule 7. Call ONLY under the lock. A broken state.json is rebuilt ONCE:
    generation floor from the session, one cooldown, an alert, written back --
    so the next call proceeds normally instead of re-arming forever."""
    st = read_state()
    why = st.pop('_corrupt', None)
    if why is None:
        return st
    raw = _read_session_raw() or {}
    try:
        st['last_generation'] = int(raw.get('generation') or 0)
    except (TypeError, ValueError):
        st['last_generation'] = 0
    st['next_allowed_at'] = time.time() + float(cfg['cooldown_sec'])
    _write_state(st)
    _alert('state_rebuilt', 'kotak_neo state.json was unreadable (%s); rebuilt with '
           'generation floor %d and one cooldown' % (scrub(why, 120), st['last_generation']),
           now)
    return st


def _write_state(st: Dict[str, Any]) -> None:
    _write_private(_p('state.json'), {k: v for k, v in st.items() if not k.startswith('_')})


def _roll_day(st: Dict[str, Any], now: datetime) -> None:
    day = now.strftime('%Y-%m-%d')
    if st.get('day') != day:
        st['day'] = day
        st['logins_today'] = 0
        st['cap_until'] = 0.0
        # Streaks are per day (review N6/N7, 2026-11-27): yesterday's refusal
        # must not make today's two morning crons the 3rd strike and LATCH
        # before the market, and the first failure of a day must alert again.
        # A latch itself is NOT cleared here: it already alerted and needs
        # the owner (`unblock`).
        st['consecutive_failures'] = 0
        st['consecutive_refusals'] = 0


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
    if raw.get('schema') != SCHEMA:
        raise SessionUnavailable('NEO session schema %r (this code reads %d)'
                                 % (raw.get('schema'), SCHEMA))
    missing = [k for k in SESSION_KEYS if not raw.get(k)]
    if missing:
        raise SessionUnavailable('NEO session file missing %s' % ', '.join(missing))
    try:
        gen = int(raw['generation'])
    except (TypeError, ValueError):
        raise SessionUnavailable('NEO session generation not an integer')
    if gen < 1:
        raise SessionUnavailable('NEO session generation %d' % gen)
    if not _host_ok(raw['base_url']):
        raise SessionUnavailable('NEO session base_url is not https://*%s' % ALLOWED_HOST_SUFFIX)
    return Session(gen, str(raw['trading_day']), str(raw['created_at']),
                   str(raw['base_url']).rstrip('/'), str(raw['token']), str(raw['sid']),
                   raw.get('server_id'), str(raw['consumer_key']), str(raw['ucc']))


def _current_or_none() -> Optional[Session]:
    try:
        return current()
    except SessionUnavailable:
        return None


def _foreign_schema() -> Optional[Any]:
    """A session.json that parses but was written by ANOTHER schema (another
    clone at another commit). Never replaced by this code (rule 3)."""
    raw = _read_session_raw()
    if raw is not None and 'schema' in raw and raw.get('schema') != SCHEMA:
        return raw.get('schema')
    return None


def _publish(st: Dict[str, Any], cur: Optional[Session], fresh: Dict[str, Any],
             creds: Dict[str, str], now: datetime) -> Session:
    if not _host_ok(fresh.get('base_url')):
        raise LoginFailed('refused', 'login returned a baseUrl outside https://*%s'
                          % ALLOWED_HOST_SUFFIX)
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


def redact(text: Any, creds: Optional[Dict[str, str]]) -> str:
    """Rule 8: every credential VALUE replaced, then long opaque runs cut."""
    t = str(text)
    for k, v in (creds or {}).items():
        if v and len(v) >= 3:
            t = t.replace(v, '<%s>' % k)
            if k == 'mobile_number':
                t = t.replace(v.lstrip('+'), '<%s>' % k)
    return scrub(t)


_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0,
               flags: int = 0) -> Any:
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def pin_ipv4() -> None:
    """Resolve to IPv4 only, PROCESS-WIDE (Kite and Telegram calls in the same
    process too). The whitelisted static IP is the home NAT's IPv4; a session
    must come from it to place orders."""
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


_MPIN_WORDS = ('mpin', ' pin')


def _clock_synced() -> Optional[bool]:
    """True/False from systemd's `NTPSynchronized` on Linux (the Pi), None
    where it cannot be read (the laptop): None never blocks a login."""
    if not sys.platform.startswith('linux'):
        return None
    try:
        out = subprocess.run(['timedatectl', 'show', '-p', 'NTPSynchronized', '--value'],
                             capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    v = (out.stdout or '').strip().lower()
    return True if v == 'yes' else False if v == 'no' else None


#: replaced in tests
clock_synced: Callable[[], Optional[bool]] = _clock_synced


def _network_login(creds: Dict[str, str], cfg: Dict[str, Any], http: Any = None) -> Dict[str, Any]:
    """TOTP login then MPIN validate. Returns {token, sid, base_url, server_id}.
    Raises LoginFailed. Messages carry status, stCode and a REDACTED text only."""
    import requests
    http = http or _http()
    pin_ipv4()
    url = str(cfg['login_url']).rstrip('/')
    t = float(cfg['login_timeout_sec'])
    hdr = {'Authorization': creds['consumer_key'], 'neo-fin-key': NEO_FIN_KEY,
           'Content-Type': 'application/json'}

    def call(path: str, headers: Dict[str, str], body: Dict[str, str], step: int) -> Dict[str, Any]:
        try:
            r = http.post(url + path, headers=headers, data=json.dumps(body), timeout=t,
                          allow_redirects=False)
        except requests.RequestException as exc:
            raise LoginFailed('network', 'step %d: %s' % (step, type(exc).__name__))
        if r.status_code >= 500 or r.status_code == 429:
            raise LoginFailed('network', 'step %d: HTTP %s' % (step, r.status_code))
        d = _json(r)
        inner = (d or {}).get('data')
        if r.status_code == 200 and isinstance(inner, dict):
            return inner
        msg = redact(neo_msg(d) or (r.text or '')[:160], creds)
        kind = 'refused'
        if step == 2 and any(w in (' ' + msg.lower()) for w in _MPIN_WORDS):
            kind = 'mpin'                                 # NEO named the MPIN: latch
        raise LoginFailed(kind, 'step %d: HTTP %s stCode %s %s'
                          % (step, r.status_code, stcode(d), msg))

    if cfg.get('require_clock_sync', True) and clock_synced() is False:
        # the Pi reboots at 08:00 and has no RTC: a TOTP from an unsynced clock
        # is refused, and three refusals latch the day (review K3, 2026-11-28).
        # Nothing is sent, so it never counts toward the cap or the latch.
        raise LoginFailed('clock', 'system clock not NTP-synchronised (timedatectl); '
                                   'the TOTP would be refused -- nothing sent')
    s1 = call(LOGIN_PATH, hdr, {'mobileNumber': creds['mobile_number'], 'ucc': creds['ucc'],
                                'totp': totp_now(creds['totp_secret'])}, 1)
    if not s1.get('token') or not s1.get('sid'):
        raise LoginFailed('refused', 'step 1 returned no token/sid')
    hdr2 = dict(hdr)
    hdr2.update({'sid': str(s1['sid']), 'Auth': str(s1['token'])})
    s2 = call(VALIDATE_PATH, hdr2, {'mpin': creds['mpin']}, 2)
    if not s2.get('token') or not s2.get('sid') or not s2.get('baseUrl'):
        raise LoginFailed('refused', 'step 2 returned no token/sid/baseUrl')
    return {'token': str(s2['token']), 'sid': str(s2['sid']),
            'base_url': str(s2['baseUrl']).rstrip('/'), 'server_id': s2.get('hsServerId')}


def _network_verify(s: Session, cfg: Dict[str, Any], http: Any = None) -> str:
    """ONE cheap read with this session. ALIVE / DEAD / UNAVAILABLE. Any
    non-ALIVE answer logs NEO's body (scrubbed): the real dead-session body has
    not been measured yet, and an unrecognised one must be visible."""
    import requests
    http = http or _http()
    pin_ipv4()
    hdr = {'Auth': s.token, 'Sid': s.sid, 'neo-fin-key': NEO_FIN_KEY,
           'accept': 'application/json', 'Content-Type': 'application/x-www-form-urlencoded'}
    params = {'sId': str(s.server_id)} if s.server_id else {}
    try:
        r = http.post(s.base_url + VERIFY_PATH, headers=hdr, params=params,
                      data={'jData': json.dumps({'seg': 'ALL', 'exch': 'ALL', 'prod': 'ALL'})},
                      timeout=float(cfg['verify_timeout_sec']), allow_redirects=False)
    except requests.RequestException as e:
        logger.warning('NEO verify gen %d: %s', s.generation, type(e).__name__)
        return UNAVAILABLE
    d = _json(r)
    if r.status_code == 200 and is_ok(d):
        return ALIVE
    if is_ip_refusal(d):
        # K5 (review 2026-11-28): the IP, not the session -- never DEAD, so no
        # login (which cannot whitelist an IP, and refused logins latch the
        # day). Queued under the lock; `_run` delivers it after the lock.
        ip_alert('session verify refused: stCode %s %s' % (stcode(d), neo_msg(d)[:120]),
                 flush=False)
        verdict = UNAVAILABLE
    else:
        verdict = DEAD if is_session_dead(r.status_code, d) else UNAVAILABLE
    logger.warning('NEO verify gen %d: %s HTTP %s body %s', s.generation, verdict,
                   r.status_code, scrub(getattr(r, 'text', '') or ''))
    return verdict


#: generation -> time of the last ALIVE verdict, per process (rule: a 5-s loop
#: must not hit the verify endpoint every poll while something else is wrong).
_alive_cache: Dict[int, float] = {}


def _verified(s: Session, verify: Callable, cfg: Dict[str, Any], sleep: Callable) -> str:
    """Rule 3: ALIVE (maybe cached), UNAVAILABLE, or a CONFIRMED DEAD."""
    t = _alive_cache.get(s.generation)
    if t is not None and time.time() - t < float(cfg['verify_cache_sec']):
        return ALIVE
    v = verify(s)
    if v == DEAD:
        sleep(float(cfg['verify_confirm_sec']))
        v2 = verify(s)
        if v2 != DEAD:
            logger.warning('NEO verify gen %d: DEAD then %s -- not confirmed, no login',
                           s.generation, v2)
            v = v2
    if v == ALIVE:
        _alive_cache[s.generation] = time.time()
    return v


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
                             'text': text[:500]})


def _log(requester: str, outcome: str, detail: str, now: datetime,
         failed_generation: Optional[int], generation: Optional[int]) -> None:
    _append('refresh_log.jsonl', {'at': now.isoformat(timespec='seconds'),
                                  'requester': requester[:80], 'outcome': outcome,
                                  'detail': detail[:300],
                                  'failed_generation': failed_generation,
                                  'generation': generation})


def _login_locked(st: Dict[str, Any], cur: Optional[Session], requester: str,
                  cfg: Dict[str, Any], login_fn: Callable, creds_fn: Callable) -> Result:
    """The brakes, then ONE login. Called with the lock held."""
    now = _now()                                     # after any lock wait
    t = time.time()
    if st.get('latched'):
        return Result(BLOCKED, cur, 'latched: %s -- python -m kotak_neo unblock'
                      % st['latched'])
    if t < float(st.get('next_allowed_at') or 0):
        wait = float(st['next_allowed_at']) - t
        return Result(COOLDOWN, cur, 'next login allowed in %.0fs' % wait, wait)
    if not login_host_ok():
        return Result(HOST_REFUSED, cur, 'this machine may not log in (LOGIN_HOST)')
    _roll_day(st, now)
    cap = int(cfg['max_logins_per_day'])
    if st['logins_today'] >= cap:
        if float(st.get('cap_until') or 0) <= 0:
            # First time over the cap: impose the long cooldown, alert once.
            st['cap_until'] = st['next_allowed_at'] = t + float(cfg['cap_cooldown_sec'])
            _write_state(st)
            _alert('daily_cap', '%d NEO logins today (cap %d): next login in %.0f min, then '
                   'one per %.0f min. If no bot refreshed, a login in the NEO app may be '
                   'killing the bots\' session.'
                   % (st['logins_today'], cap, float(cfg['cap_cooldown_sec']) / 60,
                      float(cfg['cap_cooldown_sec']) / 60), now)
            return Result(COOLDOWN, cur, 'daily login cap reached',
                          float(cfg['cap_cooldown_sec']))
        # The cap cooldown has been served: allow ONE login, re-arm after it.
        st['cap_until'] = 0.0
    try:
        creds = creds_fn(cfg['credentials_file'])
    except CredentialsError as e:
        st['latched'] = 'credentials: %s' % e
        _write_state(st)
        _alert('latched', 'NEO credentials problem: %s' % e, now)
        return Result(BLOCKED, cur, str(e))

    # Rule 5: the attempt is on disk BEFORE the network call.
    st['logins_today'] = int(st['logins_today']) + 1
    st['next_allowed_at'] = t + float(cfg['cooldown_sec'])
    st['last_attempt'] = {'at': now.isoformat(timespec='seconds'), 'requester': requester[:80],
                          'outcome': 'in_flight'}
    _write_state(st)
    try:
        fresh = login_fn(creds, cfg)
        new = _publish(st, cur, fresh, creds, now)
    except LoginFailed as e:
        return _on_failure(st, cur, e.kind, redact(e, creds), cfg, now, t)
    except CredentialsError as e:                    # e.g. a bad totp_secret inside login
        return _on_failure(st, cur, 'credentials', redact(e, creds), cfg, now, t)
    except Exception as e:                           # bug, disk full at publish, ...
        logger.error('kotak_neo login/publish raised', exc_info=True)
        return _on_failure(st, cur, 'error', '%s: %s' % (type(e).__name__, redact(e, creds)),
                           cfg, now, t)
    st['consecutive_failures'] = 0
    st['consecutive_refusals'] = 0
    st['last_attempt']['outcome'] = 'ok'
    _write_state(st)
    return Result(LOGGED_IN, new, 'generation %d' % new.generation)


def _on_failure(st: Dict[str, Any], cur: Optional[Session], kind: str, msg: str,
                cfg: Dict[str, Any], now: datetime, t: float) -> Result:
    st['consecutive_failures'] = int(st.get('consecutive_failures') or 0) + 1
    st['last_attempt']['outcome'] = 'failed:%s' % kind
    if kind in ('network', 'clock'):
        # Never reached NEO: it does not count toward the daily cap.
        st['logins_today'] = max(0, int(st['logins_today']) - 1)
    else:
        st['consecutive_refusals'] = int(st.get('consecutive_refusals') or 0) + 1
    if kind in LATCH_KINDS or st['consecutive_refusals'] >= int(cfg['max_refusals']):
        st['latched'] = '%s: %s' % (kind, msg[:200])
        _write_state(st)
        _alert('latched', 'NEO login refused (%s, %d in a row): %s. No more logins until '
               '`python -m kotak_neo unblock`.' % (kind, st['consecutive_refusals'], msg), now)
        return Result(BLOCKED, cur, '%s: %s' % (kind, msg))
    back = min(float(cfg['cooldown_sec']) * (2 ** (st['consecutive_failures'] - 1)),
               float(cfg['network_backoff_max_sec']))
    st['next_allowed_at'] = t + max(back, float(cfg['cooldown_sec']))
    _write_state(st)
    if st['consecutive_failures'] == 1:              # once per failing streak
        _alert('login_failed', 'NEO login failed (%s): %s' % (kind, msg), now)
    return Result(FAILED, cur, '%s: %s' % (kind, msg),
                  float(st['next_allowed_at']) - t)


def _decide_locked(cfg: Dict[str, Any], verify: Callable, sleep: Callable,
                   failed_generation: Optional[int], scheduled: bool, requester: str,
                   login_fn: Callable, creds_fn: Callable) -> Result:
    now = _now()
    st = _load_state_locked(cfg, now)
    foreign = _foreign_schema()
    if foreign is not None:
        return Result(ERROR, None, 'session.json has schema %r; this clone reads %d -- '
                      'upgrade this clone, never replace its session' % (foreign, SCHEMA))
    cur = _current_or_none()
    if not scheduled and cur is not None and cur.generation > int(failed_generation or 0):
        return Result(ADOPTED, cur, 'generation %d already published' % cur.generation)
    if cur is not None:
        if scheduled and cur.trading_day != now.strftime('%Y-%m-%d'):
            pass                                     # an earlier day's session: replace pre-market
        else:
            v = _verified(cur, verify, cfg, sleep)
            if v == ALIVE:
                return Result(NOOP if scheduled else STILL_ALIVE, cur,
                              'published session verifies')
            if v == UNAVAILABLE:
                return Result(UNAVAILABLE, cur, 'verify read failed: NEO unreachable or '
                              'answered an unknown body (see the log)')
    return _login_locked(st, cur, requester, cfg, login_fn, creds_fn)


def _run(requester: str, failed_generation: Optional[int], scheduled: bool,
         wait_s: Optional[float], _verify, _login, _creds, _sleep) -> Result:
    cfg = config()
    verify = _verify or (lambda s: _network_verify(s, cfg))
    try:
        fg = None if failed_generation is None else int(failed_generation)
    except (TypeError, ValueError):
        fg = 0
    try:
        with _lock(float(cfg['lock_wait_sec'] if wait_s is None else wait_s)) as got:
            if not got:
                res = Result(LOCK_TIMEOUT, _current_or_none(), 'another process is refreshing',
                             1.0)
            else:
                res = _decide_locked(cfg, verify, _sleep or time.sleep, fg, scheduled,
                                     requester, _login or _network_login,
                                     _creds or load_credentials)
    except Exception as e:                           # never raise into a trading loop
        logger.error('kotak_neo refresh failed', exc_info=True)
        res = Result(ERROR, _current_or_none(), 'refresh raised: %s' % type(e).__name__)
    _log(requester, res.outcome, res.detail, _now(), fg,
         res.session.generation if res.session else None)
    lvl = logging.INFO if res.outcome in (LOGGED_IN, ADOPTED, STILL_ALIVE, NOOP) \
        else logging.WARNING
    logger.log(lvl, 'NEO session %s for %s (%s)', res.outcome, requester, res.detail)
    _flush_alerts()
    return res


def _flush_alerts() -> None:
    """Deliver queued alerts to Telegram, OUTSIDE the lock. Tests replace it."""
    try:
        from . import alerts
        alerts.flush()
    except Exception as e:                           # never into a trading loop
        logger.error('kotak_neo alert flush failed: %s', e)


def refresh(failed_generation: int, requester: str, *, wait_s: Optional[float] = None,
            _verify: Optional[Callable] = None, _login: Optional[Callable] = None,
            _creds: Optional[Callable] = None, _sleep: Optional[Callable] = None) -> Result:
    """Called by a client whose READ found the session dead.

    `failed_generation` is the generation the failed call USED (rule 2).
    Never raises. The underscore seams exist for tests.
    """
    return _run(requester, failed_generation, False, wait_s, _verify, _login, _creds, _sleep)


IP_ALERT_EVERY_SEC = 900.0


def ip_alert(why: str, flush: bool = True) -> None:
    """Owner's backup plan until a static IP exists (2026-11-27): when NEO
    refuses orders because of the IP, ALERT and let the owner trade by hand.
    At most one alert per IP_ALERT_EVERY_SEC; delivered at once unless
    `flush=False` (a caller holding the refresh lock: `_run` delivers it)."""
    try:
        stamp = _p('ip_alert.last')
        now = time.time()
        try:
            last = float(stamp.read_text(encoding='utf-8').strip() or 0)
        except (OSError, ValueError):
            last = 0.0
        if now - last < IP_ALERT_EVERY_SEC:
            return
        _ensure_home()
        stamp.write_text(str(now), encoding='utf-8')
        _alert('static_ip', '%s -- NEO is refusing orders from this IP. Manage open trades '
               'MANUALLY in the NEO app, then fix the whitelist (NEO dashboard > Trade API; '
               'one change per 7 days).' % why, _now())
        if flush:
            _flush_alerts()
    except Exception as e:                           # never into a trading loop
        logger.error('kotak_neo ip_alert failed: %s', e)


def public_ipv4(timeout: float = 8.0) -> Optional[str]:
    """This machine's public IPv4 as the internet sees it, or None."""
    import requests
    pin_ipv4()
    for url in ('https://api.ipify.org', 'https://ifconfig.me/ip'):
        try:
            r = requests.get(url, timeout=timeout)
            ip = (r.text or '').strip()
            if r.status_code == 200 and ip.count('.') == 3:
                return ip
        except requests.RequestException:
            continue
    return None


def check_public_ip(_ip: Optional[Callable] = None) -> Optional[str]:
    """Morning check: alert if the public IPv4 is not the whitelisted one.
    Returns a problem string, or None when fine / the check is off."""
    want = str(config().get('expected_public_ipv4') or '').strip()
    if not want:
        return None
    got = (_ip or public_ipv4)()
    if got is None:
        logger.warning('public IP check: could not read the public IPv4 of this machine')
        return None
    if got != want:
        why = 'public IPv4 changed from the whitelisted one (now %s)' % got
        ip_alert(why)
        return why
    return None


def scheduled_login(*, _verify: Optional[Callable] = None, _login: Optional[Callable] = None,
                    _creds: Optional[Callable] = None,
                    _sleep: Optional[Callable] = None) -> Result:
    """The morning cron (08:40 and 08:55). NOOP when today's session verifies;
    UNAVAILABLE when NEO cannot be reached (no blind login); a session from an
    earlier day is replaced here, before the market, when nothing holds an order."""
    try:
        check_public_ip()
    except Exception as e:
        logger.error('public IP check failed: %s', e)
    res = _run('scheduled', None, True, None, _verify, _login, _creds, _sleep)
    _alert_if_no_session_today(res, _now())
    return res


def _alert_if_no_session_today(res: Result, now: datetime) -> None:
    """A morning cron that ends WITHOUT today's session tells the owner now,
    not at 09:15 through a bot (review N8). Every outcome counts -- UNAVAILABLE,
    COOLDOWN, LOCK_TIMEOUT, ERROR, HOST_REFUSED, FAILED, BLOCKED -- except the
    two that mean today's session exists. Bots keep reading the older session
    ("stale is not dead"), so the alert says what that means."""
    today = now.strftime('%Y-%m-%d')
    if res.outcome in (NOOP, LOGGED_IN, ADOPTED, STILL_ALIVE) and res.session is not None             and res.session.trading_day == today:
        return
    held = res.session.trading_day if res.session is not None else 'none'
    _alert('no_session_today',
           'NEO morning login ended %s with NO session for %s (published: %s): %s. '
           'Bots will run on the older session if it still verifies, else fail closed. '
           'Check `python -m kotak_neo status`; the 08:55 cron retries.'
           % (res.outcome, today, held, res.detail[:200]), now)
    _flush_alerts()                                  # _run already flushed: send this one too


def unblock(who: str) -> Dict[str, Any]:
    """Owner action: clear a latch, the cooldowns and today's count. Logged."""
    with _lock(float(config()['lock_wait_sec'])) as got:
        if not got:
            raise RuntimeError('lock busy; try again')
        st = read_state()
        st.pop('_corrupt', None)
        was = st.get('latched')
        st.update(latched=None, next_allowed_at=0.0, cap_until=0.0, consecutive_failures=0,
                  consecutive_refusals=0, logins_today=0)
        _write_state(st)
    _log(who, 'UNBLOCKED', 'was: %s' % was, _now(), None, None)
    return {'was': was}


def status() -> Dict[str, Any]:
    """Safe summary: no token, no sid, no ucc."""
    st = read_state()
    cur = _current_or_none()
    return {'home': str(HOME), 'generation': cur.generation if cur else None,
            'trading_day': cur.trading_day if cur else None,
            'created_at': cur.created_at if cur else None,
            'base_url': cur.base_url if cur else None,
            'state_corrupt': st.get('_corrupt'),
            'last_generation': st.get('last_generation'), 'logins_today': st.get('logins_today'),
            'day': st.get('day'), 'latched': st.get('latched'),
            'next_allowed_in_sec': max(0, round(float(st.get('next_allowed_at') or 0)
                                                - time.time())),
            'last_attempt': st.get('last_attempt'), 'login_host_ok': login_host_ok()}


def measure_second_login(*, _verify: Optional[Callable] = None, _login: Optional[Callable] = None,
                         _creds: Optional[Callable] = None,
                         _sleep: Optional[Callable] = None) -> Dict[str, Any]:
    """THE open question (DESIGN section 10): does a second NEO login kill a
    session created earlier? Run AFTER HOURS with no bot trading.

    Under the refresh lock and every brake: needs LOGIN_HOST, a published
    session that verifies (A), and two logins of headroom under the daily cap.
    Logs in ONCE more (B, never published), then reads with A twice a few
    seconds apart, and with B. If A died, B is published so the bots are left
    with a working session. Places no order. Returns the verdict and evidence;
    every login is counted in state.json like any other.
    """
    cfg = config()
    verify = _verify or (lambda s: _network_verify(s, cfg))
    sleep = _sleep or time.sleep
    login_fn = _login or _network_login
    now = _now()
    out: Dict[str, Any] = {'at': now.isoformat(timespec='seconds')}
    with _lock(float(cfg['lock_wait_sec'])) as got:
        if not got:
            out['verdict'] = 'REFUSED: lock busy'
            return out
        st = _load_state_locked(cfg, now)
        if st.get('latched'):
            out['verdict'] = 'REFUSED: latched (%s)' % st['latched']
            return out
        if not login_host_ok():
            out['verdict'] = 'REFUSED: this machine may not log in'
            return out
        _roll_day(st, now)
        if st['logins_today'] + 2 > int(cfg['max_logins_per_day']):
            out['verdict'] = 'REFUSED: not enough daily login headroom'
            return out
        a = _current_or_none()
        if a is None or verify(a) != ALIVE:
            out['verdict'] = 'REFUSED: no published session that verifies (run `login` first)'
            return out
        creds = (_creds or load_credentials)(cfg['credentials_file'])
        st['logins_today'] += 1
        st['next_allowed_at'] = time.time() + float(cfg['cooldown_sec'])
        st['last_attempt'] = {'at': now.isoformat(timespec='seconds'),
                              'requester': 'measure-second-login', 'outcome': 'in_flight'}
        _write_state(st)
        try:
            fresh = login_fn(creds, cfg)              # session B
        except LoginFailed as exc:
            # the same bookkeeping as any login (review K13, 2026-11-28): a
            # stuck `in_flight` and uncounted refusals would hide the failure
            _on_failure(st, a, exc.kind, str(exc), cfg, now, time.time())
            out['verdict'] = 'REFUSED: login B failed (%s): %s' % (exc.kind, exc)
            _log('measure-second-login', 'FAILED', out['verdict'], now, None, None)
            _flush_alerts()
            return out
        st['last_attempt']['outcome'] = 'ok'
        _write_state(st)
        b = Session(0, now.strftime('%Y-%m-%d'), now.isoformat(timespec='seconds'),
                    fresh['base_url'], fresh['token'], fresh['sid'], fresh.get('server_id'),
                    creds['consumer_key'], creds['ucc'])
        reads = []
        for i in range(3):
            if i:
                sleep(float(cfg['verify_confirm_sec']))
            reads.append(verify(a))
        out['a_after_b'] = reads
        out['b'] = verify(b)
        _alive_cache.pop(a.generation, None)
        if all(r == DEAD for r in reads):
            out['verdict'] = 'SECOND LOGIN KILLS THE FIRST'
            if out['b'] == ALIVE:
                new = _publish(st, a, fresh, creds, now)
                out['published'] = new.generation
        elif all(r == ALIVE for r in reads):
            out['verdict'] = 'BOTH SESSIONS LIVE (a second login does not kill the first)'
        else:
            out['verdict'] = 'MIXED -- inconclusive, re-measure'
    _log('measure-second-login', 'MEASURED', out['verdict'], now, None, out.get('published'))
    _flush_alerts()
    return out

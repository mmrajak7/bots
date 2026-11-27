"""kite_login.core -- the ONE place any bot logs in to Kite (owner, 2026-11-27).

Until 2026-11-27 two retired bots made the tokens every other bot reads:
SNAIL's 08:45 cron wrote the personal (market-data) token and the FIFTY daemon
wrote the company token at 08:50. This module replaces both, writing the SAME
files with the SAME keys and the SAME `generated_at` style, so none of the
~120 readers changes.

Rules (each one is a failure we refuse to repeat):
  * a login happens ONLY on the machine named in ~/.kite_login/LOGIN_HOST
    (the Pi). The laptop has no such file and can never log in.
  * a token generated today that Kite still accepts is NEVER replaced: a login
    while a good token exists buys nothing and risks killing it.
  * a token from today that cannot be CHECKED (network / Kite down) is left
    alone too -- we report it, we do not guess.
  * the token file is replaced atomically (temp + rename): a bot never reads
    half a file.
  * Kite's answer is checked against the credentials' user_id before the file
    is written, and two accounts whose credential files name the same user are
    both refused: the company token must never land in the shared market-data
    file (the company account has no quote permission -- every bot goes blind).
  * secrets (password, TOTP secret, api_secret, access_token) never reach a log.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

BOTS_ROOT = Path(__file__).resolve().parents[1]
DEFAULTS_FILE = Path(__file__).resolve().parent / 'config.defaults.json'
IST = timezone(timedelta(hours=5, minutes=30))

KITE_WEB = 'https://kite.zerodha.com'
KITE_API = 'https://api.kite.trade'
HTTP_TIMEOUT_S = 30
MAX_REDIRECTS = 10
CRED_KEYS = ('api_key', 'api_secret', 'user_id', 'password', 'totp_secret')

# Outcomes of one account's run.
NOOP = 'NOOP'                   # today's token verified; nothing done
LOGGED_IN = 'LOGGED_IN'         # a new token was made, checked and written
UNVERIFIED = 'UNVERIFIED'       # today's token exists but Kite could not be asked
HOST_REFUSED = 'HOST_REFUSED'   # a login was needed on a machine that may not log in
FAILED = 'FAILED'               # credentials / login / write failed
GOOD = {NOOP, LOGGED_IN}


class LoginError(Exception):
    """A Kite login step failed. The message never carries a secret."""


@dataclass
class Result:
    account: str
    outcome: str
    detail: str = ''

    @property
    def ok(self) -> bool:
        return self.outcome in GOOD


# -- config -----------------------------------------------------------------------------
def load_config(path: Path = DEFAULTS_FILE) -> Dict[str, Any]:
    with open(str(path), encoding='utf-8') as f:
        cfg = json.load(f)
    if not isinstance(cfg.get('accounts'), dict) or not cfg['accounts']:
        raise ValueError('%s: no accounts' % path)
    for alias, acc in cfg['accounts'].items():
        src = acc.get('credentials') or {}
        if (acc.get('generated_at') not in ('naive', 'aware') or not acc.get('token_file')
                or src.get('format') not in ('env', 'json') or not src.get('file')):
            raise ValueError('%s: account %r needs token_file, generated_at naive|aware and '
                             'credentials {format: env|json, file}' % (path, alias))
    return cfg


def resolve(rel: str, root: Path = BOTS_ROOT) -> Path:
    p = Path(rel)
    return p if p.is_absolute() else root / p


def home() -> Path:
    """State dir, outside every git clone. KITE_LOGIN_HOME overrides (tests)."""
    return Path(os.environ.get('KITE_LOGIN_HOME') or Path.home() / '.kite_login')


def read_env(path: Path) -> Dict[str, str]:
    """KEY=VALUE lines (comments, blanks, `export`, surrounding quotes handled)."""
    out = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        if k.startswith('export '):
            k = k[len('export '):].strip()
        out[k] = v.strip().strip('"').strip("'")
    return out


def load_credentials(source: Mapping[str, str], alias: str,
                     root: Path = BOTS_ROOT) -> Dict[str, str]:
    """The account's credentials from the file they have always lived in (owner
    2026-11-27: SNAIL's creds.env, FIFTY's kite_credentials.json -- no copy).
    LoginError names what is wrong, never a value."""
    path = resolve(source['file'], root)
    try:
        if source['format'] == 'env':
            env = read_env(path)
            prefix = source.get('prefix', '')
            acc = {k: env.get(prefix + k.upper(), '') for k in CRED_KEYS}
        elif source['format'] == 'json':
            with open(str(path), encoding='utf-8') as f:
                acc = json.load(f)
        else:
            raise LoginError('%r: unknown credentials format %r' % (alias, source['format']))
    except FileNotFoundError:
        raise LoginError('%r credentials file missing: %s' % (alias, path))
    except (OSError, ValueError) as e:
        raise LoginError('%r credentials file unreadable: %s (%s)' % (alias, path, type(e).__name__))
    if not isinstance(acc, dict):
        raise LoginError('%r credentials file is not an object: %s' % (alias, path))
    missing = [k for k in CRED_KEYS if not str(acc.get(k) or '').strip()]
    if missing:
        raise LoginError('%r credentials missing: %s' % (alias, ', '.join(missing)))
    creds = {k: str(acc[k]).strip() for k in CRED_KEYS}
    creds['totp_secret'] = creds['totp_secret'].replace(' ', '')
    return creds


def redact(text: str, creds: Optional[Mapping[str, str]]) -> str:
    """Strip every credential value from a message (exception texts can carry URLs)."""
    out = str(text)
    for k in ('password', 'totp_secret', 'api_secret', 'api_key'):
        v = (creds or {}).get(k)
        if v and len(v) >= 4:
            out = out.replace(v, '<%s>' % k)
    return out[:300]


# -- host guard + lock --------------------------------------------------------------------
def login_host_ok() -> Tuple[bool, str]:
    p = home() / 'LOGIN_HOST'
    try:
        want = p.read_text(encoding='utf-8').strip()
    except OSError:
        return False, 'no %s (only the Pi may log in)' % p
    have = socket.gethostname()
    if want != have:
        return False, 'LOGIN_HOST names %r, this machine is %r' % (want, have)
    return True, ''


@contextmanager
def file_lock(name: str, wait_s: float) -> Iterator[bool]:
    """Exclusive cross-process lock on home()/name; the kernel frees it if the
    holder dies. msvcrt exists only so the suite runs on the laptop."""
    d = home()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(str(d / name), os.O_RDWR | os.O_CREAT, 0o600)
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


# -- token file ---------------------------------------------------------------------------
def session_start(now: datetime) -> datetime:
    """Kite tokens die at 06:00 IST: the current token must be made after this."""
    now = now.astimezone(IST)
    six = now.replace(hour=6, minute=0, second=0, microsecond=0)
    return six if now >= six else six - timedelta(days=1)


def read_token(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(str(path), encoding='utf-8') as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) and d.get('access_token') else None


def generated_at(token: Mapping[str, Any]) -> Optional[datetime]:
    """The token's birth in IST. A naive stamp is the Pi's local time = IST."""
    try:
        t = datetime.fromisoformat(str(token.get('generated_at', '')))
    except ValueError:
        return None
    return t.replace(tzinfo=IST) if t.tzinfo is None else t.astimezone(IST)


def is_current(token: Optional[Mapping[str, Any]], creds: Mapping[str, str],
               now: datetime) -> Tuple[bool, str]:
    """Made this Kite session (after 06:00 IST) for THIS account and api key?"""
    if token is None:
        return False, 'no token file'
    born = generated_at(token)
    if born is None:
        return False, 'token has no readable generated_at'
    if token.get('user_id') != creds['user_id'] or token.get('api_key') != creds['api_key']:
        return False, 'token belongs to another account/app'
    if born < session_start(now):
        return False, 'token is from %s (before 06:00 today)' % born.strftime('%Y-%m-%d %H:%M')
    return True, 'made %s' % born.strftime('%H:%M')


def token_doc(creds: Mapping[str, str], access_token: str, style: str,
              now: datetime) -> Dict[str, str]:
    """Byte-for-byte the shape the old writers produced (same keys, same stamp style)."""
    ist = now.astimezone(IST)
    stamp = ist.replace(tzinfo=None).isoformat() if style == 'naive' else ist.isoformat()
    return {'access_token': access_token, 'api_key': creds['api_key'],
            'user_id': creds['user_id'], 'generated_at': stamp,
            'valid_until': '6:00 AM IST next day'}


def write_atomic(path: Path, doc: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.%s.tmp.%d' % (path.name, os.getpid()))
    try:
        with open(str(tmp), 'w', encoding='utf-8') as f:
            json.dump(doc, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if os.name != 'nt':
            os.chmod(str(tmp), 0o644)  # same mode the old writers left; readers run as trustit
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():               # only after a failure: never leave half a temp behind
            try:
                tmp.unlink()
            except OSError:
                pass


# -- Kite HTTP ----------------------------------------------------------------------------
def _default_session() -> Any:
    # Chrome TLS fingerprint: Zerodha's bot detection rejects plain requests (Apr 2026).
    from curl_cffi import requests as curl_requests
    return curl_requests.Session(impersonate='chrome120')


def check_profile(creds: Mapping[str, str], access_token: str,
                  session_factory: Callable[[], Any] = _default_session) -> Tuple[str, str]:
    """Ask Kite whether the token works. ('ok', user_id) / ('invalid', why) /
    ('unknown', why) -- 'unknown' means Kite could not be asked, NOT that it said no."""
    try:
        r = session_factory().get(
            KITE_API + '/user/profile', timeout=HTTP_TIMEOUT_S,
            headers={'X-Kite-Version': '3',
                     'Authorization': 'token %s:%s' % (creds['api_key'], access_token)})
    except Exception as e:                       # network, DNS, TLS
        return 'unknown', type(e).__name__
    if r.status_code == 200:
        try:
            return 'ok', str((r.json().get('data') or {}).get('user_id', ''))
        except ValueError:
            return 'unknown', 'profile answer not JSON'
    if r.status_code in (401, 403):
        return 'invalid', 'HTTP %d' % r.status_code
    return 'unknown', 'HTTP %d' % r.status_code


def _json_ok(resp: Any, step: str) -> Dict[str, Any]:
    if resp.status_code != 200:
        raise LoginError('%s: HTTP %d' % (step, resp.status_code))
    try:
        body = resp.json()
    except ValueError:
        raise LoginError('%s: answer not JSON' % step)
    if body.get('status') != 'success':
        raise LoginError('%s: %s' % (step, str(body.get('message', 'refused'))[:120]))
    return body


def _request_token(location: str) -> Optional[str]:
    if 'request_token=' not in location:
        return None
    return (parse_qs(urlparse(location).query).get('request_token') or [None])[0]


def kite_http_login(creds: Mapping[str, str],
                    session_factory: Callable[[], Any] = _default_session,
                    totp_now: Optional[Callable[[str], str]] = None) -> str:
    """Password + TOTP login, no browser. Returns the access token.
    The SNAIL/FIFTY flow that worked daily until 2026-11-27, merged."""
    if totp_now is None:
        import pyotp
        totp_now = lambda secret: pyotp.TOTP(secret).now()   # noqa: E731
    s = session_factory()
    login_url = '%s/connect/login?v=3&api_key=%s' % (KITE_WEB, creds['api_key'])

    r = s.get(login_url, allow_redirects=True, timeout=HTTP_TIMEOUT_S)
    if r.status_code != 200:
        raise LoginError('login page: HTTP %d' % r.status_code)
    # Headers Zerodha's CAPTCHA/bot detection requires (Apr 2026).
    s.headers.update({'X-Requested-With': 'XMLHttpRequest', 'Origin': KITE_WEB,
                      'Referer': login_url})

    body = _json_ok(s.post(KITE_WEB + '/api/login', timeout=HTTP_TIMEOUT_S,
                           data={'user_id': creds['user_id'], 'password': creds['password']}),
                    'password')
    request_id = (body.get('data') or {}).get('request_id')
    if not request_id:
        raise LoginError('password: no request_id')

    _json_ok(s.post(KITE_WEB + '/api/twofa', timeout=HTTP_TIMEOUT_S,
                    data={'user_id': creds['user_id'], 'request_id': request_id,
                          'twofa_value': totp_now(creds['totp_secret']), 'twofa_type': 'totp'}),
             'totp')

    # Revisit the login URL: the redirect chain ends at the app callback with request_token.
    request_token, url = None, login_url
    for _ in range(MAX_REDIRECTS):
        r = s.get(url, allow_redirects=False, timeout=HTTP_TIMEOUT_S)
        if r.status_code not in (301, 302, 303, 307, 308):
            break
        location = r.headers.get('Location', '')
        request_token = _request_token(location)
        if request_token or not location or '127.0.0.1' in location or 'localhost' in location:
            break
        url = location
    if not request_token:
        raise LoginError('no request_token in the redirect chain')

    checksum = hashlib.sha256(
        (creds['api_key'] + request_token + creds['api_secret']).encode()).hexdigest()
    body = _json_ok(s.post(KITE_API + '/session/token', timeout=HTTP_TIMEOUT_S,
                           data={'api_key': creds['api_key'], 'request_token': request_token,
                                 'checksum': checksum}),
                    'session token')
    token = (body.get('data') or {}).get('access_token')
    if not token:
        raise LoginError('session token: no access_token')
    return str(token)


# -- one account --------------------------------------------------------------------------
def _log_event(row: Dict[str, Any]) -> None:
    try:
        d = home()
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(str(d / 'login_log.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(row) + '\n')
    except OSError as e:
        logger.error('kite_login: could not append login_log.jsonl: %s', e)


def ensure_account(alias: str, cfg: Mapping[str, Any], root: Path = BOTS_ROOT,
                   now_fn: Callable[[], datetime] = lambda: datetime.now(IST),
                   session_factory: Callable[[], Any] = _default_session,
                   totp_now: Optional[Callable[[str], str]] = None,
                   host_ok: Callable[[], Tuple[bool, str]] = login_host_ok) -> Result:
    """NOOP if today's token works, else ONE login. Caller holds the lock."""
    acc = cfg['accounts'][alias]
    path = resolve(acc['token_file'], root)
    creds: Optional[Dict[str, str]] = None
    try:
        creds = load_credentials(acc['credentials'], alias, root)
        token = read_token(path)
        current, why = is_current(token, creds, now_fn())
        if current and token is not None:
            verdict, info = check_profile(creds, token['access_token'], session_factory)
            if verdict == 'ok' and info == creds['user_id']:
                return Result(alias, NOOP, 'token %s, Kite accepts it' % why)
            if verdict == 'unknown':
                # Not a "no": a login now could replace a good token. Report, retry later.
                return Result(alias, UNVERIFIED, 'token %s but Kite could not be asked (%s)'
                              % (why, info))
            why = 'Kite refused today\'s token (%s)' % info if verdict == 'invalid' \
                else 'Kite says the token is another user\'s'
        ok, refusal = host_ok()
        if not ok:
            return Result(alias, HOST_REFUSED, '%s; %s' % (why, refusal))
        logger.info('kite_login %s: logging in (%s)', alias, why)
        access_token = kite_http_login(creds, session_factory, totp_now)
        verdict, info = check_profile(creds, access_token, session_factory)
        if verdict == 'ok' and info != creds['user_id']:
            raise LoginError('Kite issued the token to another user -- credentials mixed up; '
                             'file NOT written')
        if verdict == 'invalid':
            raise LoginError('Kite refused the token it just issued (%s); file NOT written' % info)
        write_atomic(path, token_doc(creds, access_token, acc['generated_at'], now_fn()))
        tail = '' if verdict == 'ok' else ' (written; profile check: %s)' % info
        return Result(alias, LOGGED_IN, 'new token -> %s%s' % (acc['token_file'], tail))
    except LoginError as e:
        return Result(alias, FAILED, redact(str(e), creds))
    except Exception as e:                       # never let one account sink the other
        return Result(alias, FAILED, redact('%s: %s' % (type(e).__name__, e), creds))


def shared_user_ids(cfg: Mapping[str, Any], root: Path = BOTS_ROOT) -> Dict[str, str]:
    """{alias: reason} for accounts whose credential files name the SAME Kite user.

    The per-account profile check cannot see a swapped file: if SNAIL's creds.env
    held the company credentials, Kite would answer with the user_id that file
    claims, and the company token (no quote permission) would land in the shared
    market-data file. Two accounts that are one user means one file is wrong, so
    neither logs in. Every configured account is compared, not just the requested
    ones. A file that cannot be read is left to ensure_account to report."""
    users: Dict[str, List[str]] = {}
    for alias, acc in cfg['accounts'].items():
        try:
            uid = load_credentials(acc['credentials'], alias, root)['user_id'].strip().upper()
        except Exception:
            continue
        users.setdefault(uid, []).append(alias)
    out: Dict[str, str] = {}
    for uid, aliases in users.items():
        if len(aliases) > 1:
            for a in aliases:
                out[a] = ('credentials of %s are the SAME Kite user -- one file holds the wrong '
                          'account; no login, nothing written' % ' and '.join(sorted(aliases)))
    return out


def run(aliases: Optional[List[str]] = None, cfg: Optional[Dict[str, Any]] = None,
        root: Path = BOTS_ROOT, **kw: Any) -> List[Result]:
    """Every requested account, one at a time, under one lock."""
    cfg = cfg or load_config()
    aliases = aliases or list(cfg['accounts'])
    unknown = [a for a in aliases if a not in cfg['accounts']]
    if unknown:
        raise ValueError('unknown account(s): %s (known: %s)'
                         % (', '.join(unknown), ', '.join(cfg['accounts'])))
    with file_lock('login.lock', float(cfg.get('lock_wait_s', 120))) as got:
        if not got:
            return [Result(a, FAILED, 'another kite_login run holds the lock') for a in aliases]
        clash = shared_user_ids(cfg, root)
        out = []
        for a in aliases:
            if a in clash:
                res = Result(a, FAILED, clash[a])
            else:
                res = ensure_account(a, cfg, root, **kw)
            logger.info('kite_login %s: %s %s', a, res.outcome, res.detail)
            _log_event({'at': datetime.now(IST).isoformat(timespec='seconds'), 'account': a,
                        'outcome': res.outcome, 'detail': res.detail})
            out.append(res)
        return out


def status(cfg: Optional[Dict[str, Any]] = None, root: Path = BOTS_ROOT,
           now: Optional[datetime] = None) -> Dict[str, Any]:
    """Offline summary -- no network, never a secret."""
    cfg = cfg or load_config()
    now = now or datetime.now(IST)
    ok, why = login_host_ok()
    out: Dict[str, Any] = {'login_host': 'yes' if ok else 'no (%s)' % why, 'accounts': {}}
    for alias, acc in cfg['accounts'].items():
        token = read_token(resolve(acc['token_file'], root))
        born = generated_at(token) if token else None
        out['accounts'][alias] = {
            'token_file': acc['token_file'],
            'generated_at': born.isoformat(timespec='minutes') if born else None,
            'made_this_session': bool(born and born >= session_start(now)),
        }
    return out

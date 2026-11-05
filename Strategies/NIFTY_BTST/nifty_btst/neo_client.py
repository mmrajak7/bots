"""Kotak NEO Trade API, REST v2, over `requests`. Thin and explicit.

Why direct REST and not the SDK: the Pi's Python is unknown and `kotakneoapi`
3.x needs >= 3.10; the archived 2.0 SDK issues HTTP calls with no timeout;
and on a money path we must own timeouts and retries ourselves. The request
shapes below are taken from the official docs (Kotak-Neo/Kotak-Neo `docs/`)
and cross-checked against the 2.0.0 SDK source and our own Scalper logs
(`docs/NEO_AUDIT.md`, Q2 and Q5).

THE ONE RULE OF THIS MODULE: a mutating call (place, cancel) is NEVER retried
here, and its failure is reported as one of exactly three things —
  * `NeoRejected`  — the broker answered and said no; the order does NOT exist
  * `NeoUnclear`   — timeout / 5xx / garbage / 429: the order MAY exist
  * `NeoAuthError` — the session is dead (401/403); re-login was attempted
The caller decides what to do, and for `NeoUnclear` the only correct answer is
to search the order book before sending anything else (audit bug 3).

Read calls raise `NeoUnavailable` on any failure. An unreadable book is an
ERROR, never an empty book (audit bug 11).

Two classes, deliberately: `NeoReader` can log in and read; only `NeoTrader`
can place or cancel. The Stage-0 probe is built on `NeoReader` and a test
asserts it has no order-placing path.

Secrets (consumer key, mobile, ucc, mpin, totp secret) live in an untracked
JSON file under `BOTS/data/`, never in this repo, and are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import socket
import struct
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests  # type: ignore[import-untyped,unused-ignore]

logger = logging.getLogger(__name__)

LOGIN_PATH = '/login/1.0/tradeApiLogin'
VALIDATE_PATH = '/login/1.0/tradeApiValidate'
NEO_FIN_KEY = 'neotradeapi'
ORDER_SOURCE = 'NEOTRADEAPI'

PATH_PLACE = '/quick/order/rule/ms/place'
PATH_CANCEL = '/quick/order/cancel'
PATH_ORDER_BOOK = '/quick/user/orders'
PATH_ORDER_HISTORY = '/quick/order/history'
PATH_TRADES = '/quick/user/trades'
PATH_POSITIONS = '/quick/user/positions'
PATH_LIMITS = '/quick/user/limits'
PATH_SCRIP_MASTER = '/script-details/1.0/masterscrip/file-paths'
PATH_HOLDINGS = '/portfolio/v1/holdings'

#: documented placement rejections (place_order.md / troubleshooting.md, the
#: static-IP page: 100008 = wrong IP). Any OTHER `Not_Ok` on a mutation is
#: UNCLEAR: the OMS may have taken it (R2-13). HTTP 429 is a definite refusal
#: too (R3-09): the gateway throttled it before the OMS.
DOCUMENTED_REJECTIONS = (1004, 1006, 1007, 1009, 100008)

CRED_KEYS = ('consumer_key', 'mobile_number', 'ucc', 'mpin', 'totp_secret')

#: session-dead markers in an `emsg` (REST docs show `{"stat":"Not_Ok",
#: "emsg":"Invalid session","stCode":1003}` with the HTTP code unstated)
SESSION_DEAD_EMSG = ('invalid session', 'session expired', 'session ip')


class EnvelopePolicy:
    """What a report envelope must look like to be BELIEVED. Everything else
    is an error, never an empty book (NEO-03). The empty shape is UNVERIFIED
    until Stage 0 prints it; the defaults are the folklore shape and the
    config (`neo.empty_report_*`) pins what the probe observes."""

    def __init__(self, empty_stcode: Optional[int] = 5203, empty_emsg: str = 'No Data',
                 empty_ok_without_data: bool = False,
                 auth_stcodes: Tuple[int, ...] = (1003,),
                 transient_stcodes: Tuple[int, ...] = (1005,),
                 rejected_stcodes: Tuple[int, ...] = DOCUMENTED_REJECTIONS,
                 empty_per_report: Optional[Dict[str, Tuple[Optional[int], str]]] = None):
        self.empty_stcode = empty_stcode
        self.empty_emsg = empty_emsg.strip().lower()
        self.empty_ok_without_data = empty_ok_without_data
        self.auth_stcodes = tuple(auth_stcodes)
        self.transient_stcodes = tuple(transient_stcodes)
        self.rejected_stcodes = tuple(rejected_stcodes)
        #: report name -> (stCode or None, emsg): one empty shape PER report,
        #: because the three reports need not agree (R2-05)
        self.empty_per_report = {k: (v[0], v[1].strip().lower())
                                 for k, v in (empty_per_report or {}).items()}

    @staticmethod
    def _code(data: Dict[str, Any]) -> Optional[int]:
        try:
            return int(str(data.get('stCode')))
        except (TypeError, ValueError):
            return None

    def session_dead(self, data: Dict[str, Any]) -> bool:
        if str(data.get('stat', '')).strip().lower() != 'not_ok':
            return False
        emsg = str(data.get('emsg') or data.get('message') or '').lower()
        return self._code(data) in self.auth_stcodes or any(m in emsg for m in SESSION_DEAD_EMSG)

    def transient(self, data: Dict[str, Any]) -> bool:
        return self._code(data) in self.transient_stcodes

    def documented_rejection(self, data: Dict[str, Any]) -> bool:
        return self._code(data) in self.rejected_stcodes

    def is_empty(self, data: Dict[str, Any], report: str = '') -> bool:
        """The one Not_Ok shape that means 'nothing to report'. Exact, and
        per report when the config names one."""
        if str(data.get('stat', '')).strip().lower() != 'not_ok':
            return False
        code, emsg_want = self.empty_per_report.get(report, (self.empty_stcode, self.empty_emsg))
        emsg = str(data.get('emsg') or '').strip().lower()
        code_ok = code is None or self._code(data) == code
        return code_ok and emsg == emsg_want


# ----------------------------------------------------------------- errors
class NeoError(RuntimeError):
    pass


class NeoAuthError(NeoError):
    """Session rejected (401/403) or login failed."""


class NeoUnavailable(NeoError):
    """A READ failed. The book is unknown, not empty."""


class NeoUnclear(NeoError):
    """A MUTATING call had no clear answer: the order may exist."""


class NeoRejected(NeoError):
    """A MUTATING call was answered with `stat: Not_Ok`: nothing was placed."""

    def __init__(self, code: Any, msg: str):
        super().__init__('stCode %s: %s' % (code, msg))
        self.code = str(code)
        self.msg = msg


class CredentialsError(ValueError):
    pass


# ------------------------------------------------------------------ totp
def totp_now(secret_b32: str, at: Optional[float] = None, step: int = 30,
             digits: int = 6) -> str:
    """RFC 6238 TOTP (SHA-1), in the stdlib: no `pyotp` on the Pi needed."""
    s = secret_b32.strip().replace(' ', '').upper()
    s += '=' * (-len(s) % 8)
    try:
        key = base64.b32decode(s)
    except Exception:
        raise CredentialsError('totp_secret is not valid base32')
    counter = int((time.time() if at is None else at) // step)
    digest = hmac.new(key, struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10 ** digits)).zfill(digits)


# ------------------------------------------------------------------ ipv4
_orig_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host: Any, port: Any, family: int = 0, type: int = 0,
                           proto: int = 0, flags: int = 0) -> Any:
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


def pin_ipv4() -> None:
    """Resolve every host to IPv4 only, process-wide. Idempotent.

    Static IP is mandatory for NEO's order endpoints and the session must be
    created from the whitelisted IP. A dual-stack home box rotates its IPv6;
    the whitelisted address is the shared IPv4 behind the NAT. Called by the
    live CLI before login, never at import (an import-time side effect on
    `socket` would reach the paper process too).
    """
    if socket.getaddrinfo is not _ipv4_only_getaddrinfo:
        socket.getaddrinfo = _ipv4_only_getaddrinfo


# ------------------------------------------------------------ credentials
def load_credentials(path: Any) -> Dict[str, str]:
    """The five secrets, from an untracked file. Missing or blank = refuse."""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except OSError as exc:
        raise CredentialsError('NEO credentials unreadable at %s: %s' % (path, exc))
    except ValueError as exc:
        raise CredentialsError('NEO credentials at %s are not JSON: %s' % (path, exc))
    if not isinstance(raw, dict):
        raise CredentialsError('NEO credentials file must hold a JSON object')
    out: Dict[str, str] = {}
    for k in CRED_KEYS:
        v = raw.get(k)
        if not isinstance(v, str) or not v.strip():
            raise CredentialsError('NEO credentials: %s missing or blank' % k)
        out[k] = v.strip()
    if not out['mobile_number'].startswith('+'):
        raise CredentialsError('mobile_number must carry the ISD code (+91...)')
    return out


# ---------------------------------------------------------------- client
def _json_or_none(resp: Any) -> Optional[Dict[str, Any]]:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


class NeoReader:
    """Login plus every read endpoint. Cannot place or cancel anything."""

    def __init__(self, creds: Dict[str, str], login_url: str,
                 timeout: float, login_timeout: float,
                 session: Optional[Any] = None,
                 totp_fn: Callable[[str], str] = totp_now,
                 policy: Optional[EnvelopePolicy] = None):
        self.creds = creds
        self.login_url = login_url.rstrip('/')
        self.timeout = timeout
        self.login_timeout = login_timeout
        self.http = session or requests.Session()
        self._totp = totp_fn
        self.policy = policy or EnvelopePolicy()
        #: set when a re-login did not restore the session: `ready` turns
        #: False so the caller's 60 s throttle applies (NEO-25)
        self.session_dead_at: Optional[float] = None
        self.base_url: Optional[str] = None
        self.sid: Optional[str] = None
        self.token: Optional[str] = None
        self.server_id: Optional[str] = None
        self.logged_in_at: Optional[float] = None
        self.relogins = 0

    # -- session -------------------------------------------------------
    @property
    def ready(self) -> bool:
        return bool(self.base_url and self.sid and self.token)

    def login(self) -> Dict[str, Any]:
        """TOTP login, then MPIN validate. Returns a SAFE summary (no tokens)."""
        hdr = {'Authorization': self.creds['consumer_key'],
               'neo-fin-key': NEO_FIN_KEY, 'Content-Type': 'application/json'}
        body = {'mobileNumber': self.creds['mobile_number'],
                'ucc': self.creds['ucc'], 'totp': self._totp(self.creds['totp_secret'])}
        step1 = self._login_call(self.login_url + LOGIN_PATH, hdr, body, 'tradeApiLogin')
        view_token, view_sid = step1.get('token'), step1.get('sid')
        if not view_token or not view_sid:
            raise NeoAuthError('tradeApiLogin returned no token/sid')
        hdr2 = dict(hdr)
        hdr2.update({'sid': str(view_sid), 'Auth': str(view_token)})
        step2 = self._login_call(self.login_url + VALIDATE_PATH, hdr2,
                                 {'mpin': self.creds['mpin']}, 'tradeApiValidate')
        token, sid, base = step2.get('token'), step2.get('sid'), step2.get('baseUrl')
        if not token or not sid or not base:
            raise NeoAuthError('tradeApiValidate returned no token/sid/baseUrl')
        self.token, self.sid = str(token), str(sid)
        self.base_url = str(base).rstrip('/')
        self.server_id = step2.get('hsServerId')
        self.logged_in_at = time.time()
        self.session_dead_at = None
        logger.info('NEO login ok: baseUrl=%s', self.base_url)    # no name, no ucc
        return {'baseUrl': self.base_url, 'ucc': step2.get('ucc'),
                'greetingName': step2.get('greetingName')}

    def _login_call(self, url: str, hdr: Dict[str, str], body: Dict[str, str],
                    what: str) -> Dict[str, Any]:
        try:
            r = self.http.post(url, headers=hdr, data=json.dumps(body),
                               timeout=self.login_timeout)
        except requests.RequestException as exc:
            raise NeoAuthError('%s: %s' % (what, type(exc).__name__))
        data = _json_or_none(r)
        if r.status_code != 200 or data is None:
            raise NeoAuthError('%s: HTTP %s %s' % (what, r.status_code,
                                                   (data or {}).get('message', r.text[:120])))
        inner = data.get('data')
        if not isinstance(inner, dict):
            raise NeoAuthError('%s: %s' % (what, data.get('message') or data.get('emsg')
                                           or 'no data in response'))
        return inner

    def _headers(self) -> Dict[str, str]:
        if not self.ready:
            raise NeoAuthError('not logged in')
        return {'Auth': str(self.token), 'Sid': str(self.sid),
                'neo-fin-key': NEO_FIN_KEY, 'accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'}

    def _params(self) -> Dict[str, str]:
        # The 2.0 SDK sends sId=hsServerId; the docs do not mention it. Sent
        # only when login returned one, so its absence changes nothing.
        return {'sId': str(self.server_id)} if self.server_id else {}

    # -- transport -----------------------------------------------------
    def _read(self, method: str, path: str, jdata: Optional[Dict[str, Any]] = None,
              relogin: bool = True) -> Dict[str, Any]:
        """A READ. Any failure is NeoUnavailable; a dead session is re-logged
        in ONCE and the read repeated."""
        url = str(self.base_url) + path
        try:
            if method == 'GET':
                r = self.http.get(url, headers=self._headers(), params=self._params(),
                                  timeout=self.timeout)
            else:
                r = self.http.post(url, headers=self._headers(), params=self._params(),
                                   data={'jData': json.dumps(jdata or {})},
                                   timeout=self.timeout)
        except requests.RequestException as exc:
            raise NeoUnavailable('%s %s: %s' % (method, path, type(exc).__name__))
        data = _json_or_none(r)
        dead = r.status_code in (401, 403) or (data is not None and self.policy.session_dead(data))
        if dead:
            # The documented invalid-session body (stCode 1003) can come with
            # HTTP 200 or 400; the status code alone does not decide (NEO-11).
            if relogin:
                self._relogin()
                return self._read(method, path, jdata, relogin=False)
            self._mark_dead()
            raise NeoAuthError('%s %s: session still rejected after re-login (HTTP %s)'
                               % (method, path, r.status_code))
        if r.status_code != 200 or data is None:
            raise NeoUnavailable('%s %s: HTTP %s %s' % (method, path, r.status_code,
                                                        r.text[:160]))
        return data

    def _mark_dead(self) -> None:
        self.session_dead_at = time.time()
        self.base_url = self.sid = self.token = None

    def _relogin(self) -> None:
        self.relogins += 1
        logger.warning('NEO session rejected; re-login #%d', self.relogins)
        self.base_url = self.sid = self.token = None
        try:
            self.login()
        except NeoAuthError:
            self._mark_dead()
            raise

    def _rows(self, data: Dict[str, Any], what: str) -> List[Dict[str, Any]]:
        """Unwrap a report envelope. FAIL CLOSED: only `stat: Ok` with a list,
        or the one configured empty shape, is believed. A missing `stat`, a
        `message`/`fault` body, an unexpected `Not_Ok`, or `Ok` without
        `data` (unless the config says that is the empty shape) is an ERROR:
        the book is unknown, never empty (NEO-03)."""
        if 'stat' not in data:
            raise NeoUnavailable('%s: no stat in response %r' % (what, str(data)[:120]))
        stat = str(data.get('stat', '')).strip().lower()
        rows = data.get('data')
        if stat == 'ok':
            if isinstance(rows, list):
                if any(not isinstance(r, dict) for r in rows):
                    # A row that is not an object is not a report we understand;
                    # dropping it would read the book as emptier than it is (R2-22).
                    raise NeoUnavailable('%s: non-object rows in data: %r' % (what, str(rows)[:120]))
                return rows
            if rows is None and self.policy.empty_ok_without_data:
                return []
            raise NeoUnavailable('%s: Ok without a data list (%s)' % (what, type(rows).__name__))
        if self.policy.is_empty(data, what):
            return []
        raise NeoUnavailable('%s: %s (stCode %s)' % (
            what, data.get('emsg') or data.get('message') or stat or 'unrecognised',
            data.get('stCode')))

    # -- reads ---------------------------------------------------------
    def raw_report(self, path: str, jdata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """The envelope as the API sent it (stat / stCode / emsg / data). For
        the Stage-0 probe, which has to SEE the empty-book shape."""
        return self._read('POST' if jdata is not None else 'GET', path, jdata)

    def order_book(self) -> List[Dict[str, Any]]:
        return self._rows(self._read('GET', PATH_ORDER_BOOK), 'order book')

    def order(self, order_no: str) -> Optional[Dict[str, Any]]:
        """One order by number, from the whole book. None = not in the book
        (the per-id endpoint's empty shape is undocumented; the book is not)."""
        for row in self.order_book():
            if str(row.get('nOrdNo', '')) == str(order_no):
                return row
        return None

    def order_history(self, order_no: str) -> List[Dict[str, Any]]:
        data = self._read('POST', PATH_ORDER_HISTORY, {'nOrdNo': str(order_no)})
        return self._rows(data, 'order history')

    def trades(self) -> List[Dict[str, Any]]:
        return self._rows(self._read('GET', PATH_TRADES), 'trade book')

    def positions(self) -> List[Dict[str, Any]]:
        return self._rows(self._read('GET', PATH_POSITIONS), 'positions')

    def holdings(self) -> List[Dict[str, Any]]:
        """Portfolio holdings: Kotak's positions doc points here for carried
        positions. Whether an NRML option ever appears is UNVERIFIED until
        Stage 3; read-only, and only consulted when the config enables it."""
        return self._rows(self._read('GET', PATH_HOLDINGS), 'holdings')

    def limits(self, segment: str = 'ALL', exchange: str = 'ALL',
               product: str = 'ALL') -> Dict[str, Any]:
        data = self._read('POST', PATH_LIMITS, {'seg': segment, 'exch': exchange,
                                                'prod': product})
        if str(data.get('stat', 'Ok')).lower() != 'ok':
            raise NeoUnavailable('limits: %s' % (data.get('emsg') or 'Not_Ok'))
        return data

    def scrip_master_paths(self) -> List[str]:
        """Links to the per-segment scrip-master CSVs. Uses the consumer key,
        as the SDK does, and needs no trade session."""
        url = str(self.base_url) + PATH_SCRIP_MASTER
        hdr = {'Authorization': self.creds['consumer_key'], 'accept': 'application/json'}
        try:
            r = self.http.get(url, headers=hdr, timeout=self.timeout)
        except requests.RequestException as exc:
            raise NeoUnavailable('scrip master paths: %s' % type(exc).__name__)
        data = _json_or_none(r)
        if r.status_code != 200 or data is None:
            raise NeoUnavailable('scrip master paths: HTTP %s' % r.status_code)
        paths = (data.get('data') or {}).get('filesPaths')
        if not isinstance(paths, list) or not paths:
            raise NeoUnavailable('scrip master paths: no filesPaths in response')
        return [str(p) for p in paths]

    def fetch_text(self, url: str, timeout: float = 60.0) -> str:
        try:
            r = self.http.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise NeoUnavailable('download %s: %s' % (url, type(exc).__name__))
        if r.status_code != 200:
            raise NeoUnavailable('download %s: HTTP %s' % (url, r.status_code))
        return str(r.text)


class NeoTrader(NeoReader):
    """Adds the two mutating calls. Never retries either."""

    def _mutate(self, path: str, jdata: Dict[str, Any], what: str) -> Dict[str, Any]:
        url = str(self.base_url) + path
        try:
            r = self.http.post(url, headers=self._headers(), params=self._params(),
                               data={'jData': json.dumps(jdata)}, timeout=self.timeout)
        except requests.Timeout:
            raise NeoUnclear('%s: timed out after %.0fs' % (what, self.timeout))
        except requests.RequestException as exc:
            raise NeoUnclear('%s: %s' % (what, type(exc).__name__))
        data = _json_or_none(r)
        if r.status_code in (401, 403) or (data is not None and self.policy.session_dead(data)):
            # Re-login so the NEXT call works; this one stays unclear — the
            # caller searches the book before deciding (design section 4).
            try:
                self._relogin()
            except NeoAuthError as exc:
                raise NeoAuthError('%s: HTTP %s and re-login failed: %s'
                                   % (what, r.status_code, exc))
            raise NeoUnclear('%s: session rejected (HTTP %s), re-established'
                             % (what, r.status_code))
        if r.status_code == 429:
            # throttled at the gateway: documented as not placed (R3-09)
            raise NeoRejected('429', 'rate limited (HTTP 429)')
        if data is None:
            raise NeoUnclear('%s: HTTP %s non-JSON body %r' % (what, r.status_code,
                                                               r.text[:120]))
        stat = str(data.get('stat', '')).strip().lower()
        if (stat == 'not_ok' and self.policy.documented_rejection(data)
                and not self.policy.transient(data) and not self.policy.session_dead(data)):
            # the BODY is classified before the status (R4-20): a documented
            # refusal is a refusal whatever HTTP code carries it
            raise NeoRejected(data.get('stCode'), str(data.get('emsg') or data.get('message')))
        if r.status_code != 200:
            raise NeoUnclear('%s: HTTP %s %s' % (what, r.status_code,
                                                 data.get('emsg') or data.get('message')))
        if stat == 'ok' and data.get('nOrdNo'):
            return data
        if stat == 'not_ok':
            if self.policy.session_dead(data):
                raise NeoUnclear('%s: session rejected (%s)' % (what, data.get('emsg')))
            if self.policy.documented_rejection(data) and not self.policy.transient(data):
                raise NeoRejected(data.get('stCode'), str(data.get('emsg') or data.get('message')))
            # Anything else — 1005 "Internal Error", an OMS gateway timeout, a
            # code nobody has seen — is UNCLEAR: the book decides (R2-13).
            raise NeoUnclear('%s: unrecognised Not_Ok stCode %s %s' % (
                what, data.get('stCode'), data.get('emsg') or data.get('message')))
        raise NeoUnclear('%s: unrecognised response %r' % (what, str(data)[:200]))

    def place(self, *, symbol: str, token: str, side: str, qty: int, price: str,
              tag: str, segment: str, product: str) -> str:
        """LIMIT, DAY, no AMO. Returns nOrdNo. `price` is already a tick-
        rounded string. Market orders are not offered: the broker converts
        them to protected limits anyway, so we price our own."""
        if side not in ('B', 'S'):
            raise ValueError('side must be B or S')
        jdata = {'am': 'NO', 'dq': '0', 'es': segment, 'mp': '0', 'pc': product,
                 'pf': 'N', 'pr': price, 'pt': 'L', 'qt': str(int(qty)), 'rt': 'DAY',
                 'tp': '0', 'ts': symbol, 'tt': side, 'ig': tag, 'tk': str(token),
                 'os': ORDER_SOURCE}
        data = self._mutate(PATH_PLACE, jdata, 'place %s %s' % (side, symbol))
        return str(data['nOrdNo'])

    def cancel(self, order_no: str) -> str:
        data = self._mutate(PATH_CANCEL, {'on': str(order_no), 'am': 'NO'},
                            'cancel %s' % order_no)
        return str(data['nOrdNo'])

"""NeoClient: every NEO read and order, for every bot, on the ONE shared session.

Moved in behaviour from BTST's audited `neo_client.py` + `broker.py` (error
classes, typed views, "the book is unknown, never empty"), with the live
contract measured on 2026-11-27 and the review's rules:

  * Reads raise `Unavailable` (book unknown, NEVER empty) or `Auth`. A read that
    finds the session dead asks `session.refresh(failed_generation)` ONCE and
    repeats the read once on the session it gets back.
  * Mutations are NEVER re-sent. 401/403/dead on a mutation -> `Unclear`, NEO's
    body logged scrubbed, and NO login (a refused order is not evidence of a
    dead session -- the 13:32 401 that a re-login did not fix). The caller
    resolves the order from the book by its TAG.
  * Every order carries the bot's tag prefix (`zbr-`, `btst-`, `etf-`) and
    CLAIMS its contract in `owners.json`: one bot per contract, so one bot's
    reconcile can never adopt, sell or kill on another bot's position.
  * SL-M is refused here, before NEO: RMS rejects it for options (measured).
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import session as S
from .envelope import is_ok, is_session_dead, neo_msg, scrub, stcode

logger = logging.getLogger(__name__)

PATH_PLACE = '/quick/order/rule/ms/place'
PATH_CANCEL = '/quick/order/cancel'
PATH_ORDER_BOOK = '/quick/user/orders'
PATH_TRADES = '/quick/user/trades'
PATH_POSITIONS = '/quick/user/positions'
PATH_LIMITS = '/quick/user/limits'
PATH_SCRIP_MASTER = '/script-details/1.0/masterscrip/file-paths'
ORDER_SOURCE = 'NEOTRADEAPI'

#: The measured empty report (all three reports, 2026-11-27).
EMPTY_STCODE, EMPTY_MSG = 5203, 'no data'
#: Placement refusals that are DEFINITE (not placed): documented + measured.
REJECTED_STCODES = (1004, 1006, 1007, 1009, 100008)
TRANSIENT_STCODES = (1005,)

TERMINAL = ('complete', 'rejected', 'cancelled')
LIVE_STATES = ('put order req received', 'validation pending', 'open pending', 'open',
               'modified', 'trigger pending', 'after market order req received', 'pending',
               'modify pending', 'cancel pending')
QTY_FIELDS = ('flBuyQty', 'flSellQty', 'cfBuyQty', 'cfSellQty')
BOT_RE = re.compile(r'^[a-z]{2,6}$')


# -- errors -------------------------------------------------------------------------------
class NeoError(RuntimeError):
    pass


class Unavailable(NeoError):
    """A read failed: the book is UNKNOWN (never treat as empty)."""


class Unclear(NeoError):
    """A mutation MAY have reached the exchange. Resolve by tag; never re-send."""


class Rejected(NeoError):
    """Definitely not placed / not cancelled."""

    def __init__(self, code: Any, msg: str):
        super().__init__('stCode %s: %s' % (code, msg))
        self.code, self.msg = str(code), msg


class Auth(NeoError):
    """No usable session and the shared refresh could not provide one."""


class NotOwner(NeoError):
    """Another bot holds this contract. Nothing was sent."""


# -- views --------------------------------------------------------------------------------
def _f(v: Any, d: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return d


def _i(v: Any, d: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return d


class RowError(ValueError):
    pass


def _strict_int(r: Dict[str, Any], k: str) -> int:
    v = r.get(k)
    try:
        n = int(float(str(v).strip()))
    except (TypeError, ValueError):
        raise RowError('order row %s=%r is not a number' % (k, v))
    if n < 0:
        raise RowError('order row %s=%r is negative' % (k, v))
    return n


@dataclass(frozen=True)
class OrderView:
    order_no: str
    status: str            # lower-cased ordSt
    symbol: str
    side: str              # 'B' / 'S'
    qty: int
    filled: int
    avg_price: float
    price: float
    trigger: float
    kind: str              # prcTp: L / SL / ...
    rej_reason: str
    tag: str               # GuiOrdId
    placed_at: str
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def known_status(self) -> bool:
        return self.status in TERMINAL or self.status in LIVE_STATES

    def bot(self) -> str:
        """The bot that tagged this order ('' if untagged / not ours)."""
        m = re.match(r'^([a-z]{2,6})-', self.tag or '')
        return m.group(1) if m else ''

    @classmethod
    def from_neo(cls, r: Dict[str, Any]) -> 'OrderView':
        """STRICT on the numbers money depends on (review F5): a row whose
        quantity or filled quantity cannot be read is an ERROR, never a zero --
        a `complete` order read as `filled 0` would be sold a second time.
        Quantities are SHARES (measured: 1 lot of NIFTY 65 -> qty 65)."""
        rej = str(r.get('rejRsn') or '').strip()
        qty, fld = _strict_int(r, 'qty'), _strict_int(r, 'fldQty')
        if fld > qty:
            raise RowError('order %s filled %d > qty %d' % (r.get('nOrdNo'), fld, qty))
        return cls(order_no=str(r.get('nOrdNo', '')),
                   status=str(r.get('ordSt', '')).strip().lower(),
                   symbol=str(r.get('trdSym') or r.get('sym') or ''),
                   side=str(r.get('trnsTp', '')).upper()[:1], qty=qty,
                   filled=fld, avg_price=_f(r.get('avgPrc')),
                   price=_f(r.get('prc')), trigger=_f(r.get('trgPrc')),
                   kind=str(r.get('prcTp') or ''), rej_reason='' if rej == '--' else rej,
                   tag=str(r.get('GuiOrdId') or ''), placed_at=str(r.get('ordDtTm') or ''),
                   raw=r)


@dataclass(frozen=True)
class PositionView:
    symbol: str
    product: str
    net_qty: int
    lot_size: int = 0
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)

    @classmethod
    def from_neo(cls, r: Dict[str, Any]) -> 'PositionView':
        if not any(r.get(k) not in (None, '') for k in QTY_FIELDS):
            raise RowError('position row has no quantity field')
        v = {}
        for k in QTY_FIELDS:
            x = r.get(k)
            try:
                v[k] = 0 if x in (None, '') else int(float(str(x).strip()))
            except (TypeError, ValueError):
                raise RowError('position row %s=%r is not a number' % (k, x))
        return cls(symbol=str(r.get('trdSym') or r.get('sym') or ''),
                   product=str(r.get('prod') or ''),
                   net_qty=v['flBuyQty'] + v['cfBuyQty'] - v['flSellQty'] - v['cfSellQty'],
                   lot_size=_i(r.get('lotSz')), raw=r)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str            # NEO trading symbol (from symbols.py, never built by hand)
    token: str             # NEO instrument token
    side: str              # 'B' / 'S'
    qty: int               # SHARES, a whole number of lots
    price: str             # tick-rounded string, the limit
    tag: str               # from NeoClient.new_tag()
    kind: str = 'L'        # 'L' limit, 'SL' stop-limit (SL-M is refused for options)
    trigger: str = '0'     # SL only


# -- ownership ----------------------------------------------------------------------------
def _owners_path():
    return S.HOME / 'owners.json'


@contextmanager
def _owners() -> Iterator[Dict[str, Any]]:
    """{neo symbol: {bot, since}}, read-modify-written under its own lock.
    Unreadable -> Unavailable (fail CLOSED for anything that adds exposure)."""
    with S.file_lock('owners.lock', 10.0) as got:
        if not got:
            raise Unavailable('owners.json lock busy')
        try:
            with open(str(_owners_path()), encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError('not an object')
        except FileNotFoundError:
            data = {}
        except (OSError, ValueError) as e:
            raise Unavailable('owners.json unreadable (%s) -- no order placed' % e)
        before = json.dumps(data, sort_keys=True)
        yield data
        if json.dumps(data, sort_keys=True) != before:
            S._write_private(_owners_path(), data)


def _holder(v: Any) -> Optional[str]:
    if isinstance(v, dict):
        return v.get('bot')
    return v or None


def owner_of(symbol: str) -> Optional[str]:
    with _owners() as o:
        return _holder(o.get(symbol))


def owners() -> Dict[str, Any]:
    with _owners() as o:
        return dict(o)


def assign(symbol: str, bot: Optional[str]) -> None:
    """Owner/CLI: set (or with None clear) the holder of `symbol`."""
    with _owners() as o:
        if bot is None:
            o.pop(symbol, None)
        else:
            o[symbol] = {'bot': bot, 'since': S._now().isoformat(timespec='seconds'),
                         'assigned': True}


# -- the client ---------------------------------------------------------------------------
class NeoClient:
    """One per bot process. Cheap: it re-reads the published session per call."""

    def __init__(self, bot: str, *, segment: str = 'nse_fo', product: str = 'NRML',
                 timeout: float = 10.0, http: Any = None,
                 current_fn: Callable[[], S.Session] = None,
                 refresh_fn: Callable[..., S.Result] = None,
                 clock: Callable[[], datetime] = None):
        if not BOT_RE.match(bot or ''):
            raise ValueError('bot must be 2-6 lowercase letters, got %r' % bot)
        self.bot = bot
        self.segment = segment
        self.product = product
        self.timeout = timeout
        self._http = http
        self._current = current_fn or S.current
        self._refresh = refresh_fn or S.refresh
        self._clock = clock or S._now
        self._seq = itertools.count(1)

    # -- plumbing ------------------------------------------------------------
    @property
    def http(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
        return self._http

    def session(self) -> S.Session:
        try:
            return self._current()
        except S.SessionUnavailable as e:
            raise Auth(str(e))

    @staticmethod
    def _headers(s: S.Session) -> Dict[str, str]:
        return {'Auth': s.token, 'Sid': s.sid, 'neo-fin-key': S.NEO_FIN_KEY,
                'accept': 'application/json',
                'Content-Type': 'application/x-www-form-urlencoded'}

    @staticmethod
    def _params(s: S.Session) -> Dict[str, str]:
        return {'sId': str(s.server_id)} if s.server_id else {}

    @staticmethod
    def _json(r: Any) -> Optional[Dict[str, Any]]:
        try:
            d = r.json()
        except ValueError:
            return None
        return d if isinstance(d, dict) else None

    def _send(self, s: S.Session, method: str, path: str, jdata: Optional[Dict[str, Any]]):
        S.pin_ipv4()
        url = s.base_url + path
        if method == 'GET':
            return self.http.get(url, headers=self._headers(s), params=self._params(s),
                                 timeout=self.timeout, allow_redirects=False)
        return self.http.post(url, headers=self._headers(s), params=self._params(s),
                              data={'jData': json.dumps(jdata or {})}, timeout=self.timeout,
                              allow_redirects=False)

    def _read(self, method: str, path: str, jdata: Optional[Dict[str, Any]] = None,
              retried: bool = False) -> Dict[str, Any]:
        import requests
        s = self.session()
        try:
            r = self._send(s, method, path, jdata)
        except requests.RequestException as e:
            raise Unavailable('%s %s: %s' % (method, path, type(e).__name__))
        d = self._json(r)
        if is_session_dead(r.status_code, d):
            if retried:
                raise Auth('%s %s: session still rejected after refresh' % (method, path))
            res = self._refresh(s.generation, self.bot)
            if res.outcome in (S.ADOPTED, S.LOGGED_IN):
                return self._read(method, path, jdata, retried=True)
            if res.outcome in (S.BLOCKED, S.HOST_REFUSED, S.ERROR):
                raise Auth('session refresh %s: %s' % (res.outcome, res.detail))
            raise Unavailable('%s %s: session refresh %s (%s)' % (method, path, res.outcome,
                                                                   res.detail))
        if r.status_code != 200 or d is None:
            raise Unavailable('%s %s: HTTP %s %s' % (method, path, r.status_code,
                                                     scrub(getattr(r, 'text', ''), 160)))
        return d

    def _rows(self, d: Dict[str, Any], what: str) -> List[Dict[str, Any]]:
        """FAIL CLOSED: Ok with a list, or the measured empty shape. Else unknown."""
        if 'stat' not in d:
            raise Unavailable('%s: no stat' % what)
        if is_ok(d):
            rows = d.get('data')
            if isinstance(rows, list) and all(isinstance(x, dict) for x in rows):
                return rows
            raise Unavailable('%s: Ok without a list of rows' % what)
        if stcode(d) == EMPTY_STCODE and neo_msg(d).strip().lower() == EMPTY_MSG:
            return []
        raise Unavailable('%s: %s (stCode %s)' % (what, neo_msg(d) or 'Not_Ok', stcode(d)))

    # -- reads -----------------------------------------------------------------
    def _views(self, path: str, what: str) -> List[OrderView]:
        try:
            return [OrderView.from_neo(r) for r in self._rows(self._read('GET', path), what)]
        except RowError as e:
            raise Unavailable('%s: %s' % (what, e))

    def order_book(self) -> List[OrderView]:
        return self._views(PATH_ORDER_BOOK, 'order book')

    def trades(self) -> List[OrderView]:
        return self._views(PATH_TRADES, 'trade book')

    def net_held(self, symbol: str) -> int:
        """This account's net quantity in `symbol` (shares). Raises Unavailable."""
        return sum(p.net_qty for p in self.positions() if p.symbol == symbol)

    def live_orders(self, symbol: str, side: Optional[str] = None) -> List[OrderView]:
        return [o for o in self.order_book() if o.symbol == symbol and not o.terminal
                and (side is None or o.side == side)]

    def positions(self) -> List[PositionView]:
        out = []
        for r in self._rows(self._read('GET', PATH_POSITIONS), 'positions'):
            try:
                out.append(PositionView.from_neo(r))
            except RowError as e:
                raise Unavailable('positions: %s' % e)
        return out

    def funds(self, field_name: str = 'Net') -> float:
        """Available rupees. Raises Unavailable when it cannot be read (one
        failure mode, so no caller can mistake a None for a number)."""
        d = self._read('POST', PATH_LIMITS, {'seg': 'ALL', 'exch': 'ALL', 'prod': 'ALL'})
        if not is_ok(d):
            raise Unavailable('limits: %s' % (neo_msg(d) or 'Not_Ok'))
        try:
            return float(str(d.get(field_name)).replace(',', ''))
        except (TypeError, ValueError):
            raise Unavailable('limits has no usable %r' % field_name)

    def orders_tagged(self, tag: str) -> List[OrderView]:
        return [o for o in self.order_book() if o.tag == tag]

    def scrip_master_paths(self) -> List[str]:
        """Links to the per-segment scrip-master CSVs (consumer key auth, as
        the SDK and BTST do). Every failure is Unavailable."""
        import requests
        s = self.session()
        S.pin_ipv4()
        try:
            r = self.http.get(s.base_url + PATH_SCRIP_MASTER,
                              headers={'Authorization': s.consumer_key,
                                       'accept': 'application/json'},
                              timeout=self.timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise Unavailable('scrip master paths: %s' % type(e).__name__)
        d = self._json(r)
        data = (d or {}).get('data')
        paths = data.get('filesPaths') if isinstance(data, dict) else None
        if r.status_code != 200 or not isinstance(paths, list) or not paths:
            raise Unavailable('scrip master paths: HTTP %s' % r.status_code)
        return [str(p) for p in paths]

    def fetch_text(self, url: str, timeout: float = 60.0) -> str:
        import requests
        from urllib.parse import urlparse
        if urlparse(url).scheme != 'https':
            raise Unavailable('download refused: not https')
        try:
            r = self.http.get(url, timeout=timeout, allow_redirects=False)
        except requests.RequestException as e:
            raise Unavailable('download: %s' % type(e).__name__)
        if r.status_code != 200:
            raise Unavailable('download: HTTP %s' % r.status_code)
        return str(r.text)

    # -- tags & ownership ---------------------------------------------------------
    def new_tag(self) -> str:
        """`<bot>-<MMDDHHMMSS>-<pid36><n>`. The pid part makes it unique ACROSS
        processes of one bot (review F3: zebra's cron and monitor collided in
        one second and two orders merged under one tag)."""
        pid, out = os.getpid() % 46656, ''
        for _ in range(3):
            pid, d = divmod(pid, 36)
            out = '0123456789abcdefghijklmnopqrstuvwxyz'[d] + out
        return '%s-%s-%s%d' % (self.bot, self._clock().strftime('%m%d%H%M%S'), out,
                               next(self._seq))

    def claim(self, symbol: str) -> bool:
        """Take `symbol` for this bot, or raise NotOwner. Returns True when the
        claim is NEW (so a definite rejection can hand it straight back).

        A NEW claim on a symbol the account already holds with no owner (e.g.
        BTST's overnight lot from before ownership existed) is refused: that
        position belongs to someone, and it is not provably this bot."""
        with _owners() as o:
            holder = _holder(o.get(symbol))
            if holder and holder != self.bot:
                raise NotOwner('%s is held by %s' % (symbol, holder))
            if holder is None:
                held = self.net_held(symbol)          # Unavailable -> nothing placed
                if held != 0:
                    raise NotOwner('%s: account holds %d with no owner -- '
                                   '`python -m kotak_neo owners` to assign it' % (symbol, held))
                o[symbol] = {'bot': self.bot, 'since': self._clock().isoformat(timespec='seconds')}
            return holder is None

    def release_if_flat(self, symbol: str) -> bool:
        """Give `symbol` up only when the account is FLAT in it and this bot has
        no live order on it (review F9). True when released."""
        if self.net_held(symbol) != 0:
            return False
        if any(o.bot() == self.bot for o in self.live_orders(symbol)):
            return False
        with _owners() as o:
            if _holder(o.get(symbol)) == self.bot:
                del o[symbol]
                return True
        return False

    def release(self, symbol: str) -> None:
        """Unconditional release -- ONLY after a definite rejection of a new
        claim's first order, when nothing was placed."""
        with _owners() as o:
            if _holder(o.get(symbol)) == self.bot:
                del o[symbol]

    # -- mutations --------------------------------------------------------------
    def place(self, req: OrderRequest, reduce_only: bool = False) -> str:
        """Send ONE order. Returns NEO's order number -- which proves only that
        NEO accepted the request: RMS can reject it a moment later (measured).
        The outcome comes from `orders.resolve(tag)`."""
        if req.side not in ('B', 'S'):
            raise ValueError('side must be B or S')
        if req.kind not in ('L', 'SL'):
            raise ValueError('order kind %r refused: SL-M is rejected by RMS for options; '
                             'use SL (trigger + limit)' % req.kind)
        if not req.tag.startswith(self.bot + '-'):
            raise ValueError('tag %r must start with %r (use new_tag())' % (req.tag, self.bot + '-'))
        if int(req.qty) <= 0:
            raise ValueError('qty must be positive')
        if req.kind == 'SL' and _f(req.trigger) <= 0:
            raise ValueError('an SL order needs a trigger')
        if reduce_only:
            # An EXIT. Never blocked by an unreadable owners file (review F7):
            # the chase caps it at what the account holds. Still refused if the
            # file is readable and names another bot.
            if req.side != 'S':
                raise ValueError('reduce_only is for sells of a long')
            new_claim = False
            try:
                holder = owner_of(req.symbol)
            except Unavailable as e:
                holder = None
                logger.warning('owners.json unreadable for an exit sell (%s) -- proceeding', e)
            if holder and holder != self.bot:
                raise NotOwner('%s is held by %s' % (req.symbol, holder))
        else:
            new_claim = self.claim(req.symbol)
        jdata = {'am': 'NO', 'dq': '0', 'es': self.segment, 'mp': '0', 'pc': self.product,
                 'pf': 'N', 'pr': req.price, 'pt': req.kind, 'qt': str(int(req.qty)),
                 'rt': 'DAY', 'tp': req.trigger if req.kind == 'SL' else '0',
                 'ts': req.symbol, 'tt': req.side, 'ig': req.tag, 'tk': str(req.token),
                 'os': ORDER_SOURCE}
        try:
            d = self._mutate(PATH_PLACE, jdata,
                             'place %s %s %s' % (req.kind, req.side, req.symbol), ('nOrdNo',))
        except Rejected:
            if new_claim:               # definitely nothing placed: hand the contract back
                self.release(req.symbol)
            raise
        return str(d['nOrdNo'])

    def cancel(self, order_no: str) -> None:
        self._mutate(PATH_CANCEL, {'on': str(order_no), 'am': 'NO'}, 'cancel %s' % order_no,
                     ('nOrdNo', 'result'))

    def _mutate(self, path: str, jdata: Dict[str, Any], what: str,
                id_keys: Tuple[str, ...]) -> Dict[str, Any]:
        import requests
        s = self.session()
        try:
            r = self._send(s, 'POST', path, jdata)
        except requests.Timeout:
            raise Unclear('%s: timed out after %.0fs' % (what, self.timeout))
        except requests.RequestException as e:
            raise Unclear('%s: %s' % (what, type(e).__name__))
        d = self._json(r)
        body = scrub(getattr(r, 'text', '') or '')
        if is_session_dead(r.status_code, d):
            # Rule: NEVER a login on mutation evidence. Unclear; the book decides.
            logger.warning('%s: HTTP %s session rejected (gen %d) -- unclear, no login; '
                           'body %s', what, r.status_code, s.generation, body)
            raise Unclear('%s: session rejected (HTTP %s)' % (what, r.status_code))
        if r.status_code == 429:
            raise Rejected('429', 'rate limited (HTTP 429)')
        if d is None:
            raise Unclear('%s: HTTP %s non-JSON body %s' % (what, r.status_code, body[:120]))
        code = stcode(d)
        if not is_ok(d) and code in REJECTED_STCODES and code not in TRANSIENT_STCODES:
            raise Rejected(code, neo_msg(d))
        if r.status_code != 200:
            logger.warning('%s: HTTP %s body %s', what, r.status_code, body)
            raise Unclear('%s: HTTP %s stCode %s %s' % (what, r.status_code, code, neo_msg(d)))
        if is_ok(d) and any(d.get(k) for k in id_keys):
            return d
        raise Unclear('%s: unrecognised reply stCode %s %s' % (what, code, neo_msg(d) or body[:120]))

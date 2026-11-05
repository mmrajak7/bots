"""The broker as the live engine sees it, and the fake used by rehearsal/tests.

`Broker` normalises NEO's order book and positions into small typed views so
the engine never reads a raw field name. Numbers from NEO arrive as STRINGS
("65", "160.65", "0.00") and the parsers here accept that.

Errors mirror `neo_client`: reads raise `BrokerUnavailable` (the book is
unknown, never empty); place/cancel raise `BrokerRejected` (definitely not
placed), `BrokerUnclear` (may be placed), `BrokerAuth` (session dead).

`FakeBroker` is HOSTILE on purpose (review NEO-23). Beyond fills after a
delay, scripted rejections, timeouts that did or did not reach the exchange
and partial fills on cancel, it can: show a placed order only after a
visibility lag (SDK issue #38); reflect fills in positions only after a lag;
drop yesterday's orders from the DAY book and mass-cancel what was open at the
close; omit the carry-forward position row (the REST doc's "only positions
with actual trades for the day"); answer `cancel pending` for a while; and
persist itself to disk so a rehearsal survives a restart and an overnight
(NEO-12). Every stored number is a string, like the real API.
"""
from __future__ import annotations

import itertools
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .neo_client import NeoAuthError, NeoRejected, NeoTrader, NeoUnavailable, NeoUnclear

logger = logging.getLogger(__name__)

#: order statuses that mean "nothing more will happen to this order"
TERMINAL = ('complete', 'rejected', 'cancelled')
#: statuses we treat as "resting / in flight" (docs + our own logs); anything
#: not terminal is live, including strings we have never seen — but a string
#: outside BOTH lists is reported, because a real terminal status we do not
#: know (an "expired"?) would block an exit all day (R2-24)
LIVE_STATES = ('put order req received', 'validation pending', 'open pending',
               'open', 'modified', 'trigger pending', 'after market order req received',
               'pending', 'modify pending', 'cancel pending')
#: identity keys that must never reach a log, Telegram or the probe output
IDENTITY_KEYS = ('actid', 'usrid', 'ucc', 'clientid', 'custid', 'accountid', 'usercode',
                 'greetingname', 'mobile')
QTY_FIELDS = ('flBuyQty', 'flSellQty', 'cfBuyQty', 'cfSellQty')


def redact_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """A raw broker row with identity keys replaced (R2-18)."""
    return {k: ('<redacted>' if any(x in k.lower().replace('_', '') for x in IDENTITY_KEYS) else v)
            for k, v in row.items()}


class BrokerError(RuntimeError):
    pass


class BrokerUnavailable(BrokerError):
    pass


class BrokerUnclear(BrokerError):
    pass


class BrokerRejected(BrokerError):
    def __init__(self, code: str, msg: str):
        super().__init__('%s: %s' % (code, msg))
        self.code, self.msg = code, msg


class BrokerAuth(BrokerError):
    pass


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return default


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


_NEO_TIME_FORMATS = ('%d-%b-%Y %H:%M:%S', '%Y/%m/%d %H:%M:%S', '%d-%m-%Y %H:%M:%S')


def parse_neo_time(s: str, tz: Any) -> Optional[datetime]:
    for fmt in _NEO_TIME_FORMATS:
        try:
            return datetime.strptime(s.strip(), fmt).replace(tzinfo=tz)
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class OrderView:
    order_no: str
    status: str            # lower-cased ordSt
    symbol: str            # trdSym
    side: str              # 'B' / 'S'
    qty: int
    filled: int
    avg_price: float
    price: float
    rej_reason: str
    tag: str               # GuiOrdId ('' when the broker did not echo it)
    placed_at: str         # ordDtTm as given
    product: str
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL

    @property
    def live(self) -> bool:
        return not self.terminal

    @property
    def known_status(self) -> bool:
        return self.status in TERMINAL or self.status in LIVE_STATES

    @classmethod
    def from_neo(cls, r: Dict[str, Any]) -> 'OrderView':
        rej = str(r.get('rejRsn') or '').strip()
        return cls(order_no=str(r.get('nOrdNo', '')), status=str(r.get('ordSt', '')).strip().lower(),
                   symbol=str(r.get('trdSym') or r.get('sym') or ''),
                   side=str(r.get('trnsTp', '')).upper()[:1], qty=_i(r.get('qty')),
                   filled=_i(r.get('fldQty')), avg_price=_f(r.get('avgPrc')),
                   price=_f(r.get('prc')), rej_reason='' if rej == '--' else rej,
                   tag=str(r.get('GuiOrdId') or ''), placed_at=str(r.get('ordDtTm') or ''),
                   product=str(r.get('prod') or ''), raw=r)


class RowError(ValueError):
    """A broker row the code cannot read. Raised, never read as flat (R2-22)."""


@dataclass(frozen=True)
class PositionView:
    symbol: str
    product: str
    net_qty: int           # (flBuyQty + cfBuyQty) - (flSellQty + cfSellQty), in SHARES
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, hash=False)
    lot_size: int = 0      # lotSz when the row carries it

    @classmethod
    def from_neo(cls, r: Dict[str, Any], qty_unit: str = 'shares') -> 'PositionView':
        # Quantities only. The broker's average is NOT read anywhere: SDK
        # issue #32 says carry-forward rows may omit it, and our own fill is
        # the price that matters. A row with NO quantity field at all is an
        # error, not a flat position (R2-22). `positions.md` says F&O values
        # may be in lots: `qty_unit: lots` multiplies by the row's `lotSz`,
        # and a missing `lotSz` is then an error too (measurement item 6).
        present = [k for k in QTY_FIELDS if k in r and r.get(k) not in (None, '')]
        if not present:
            raise RowError('position row has no quantity field: %s' % sorted(r.keys())[:12])
        vals = {}
        for k in QTY_FIELDS:
            v = r.get(k)
            if v in (None, ''):
                vals[k] = 0
                continue
            try:
                vals[k] = int(float(str(v).strip()))
            except (TypeError, ValueError):
                raise RowError('position row %s=%r is not a number' % (k, v))
        net = vals['flBuyQty'] + vals['cfBuyQty'] - vals['flSellQty'] - vals['cfSellQty']
        lot = _i(r.get('lotSz'))
        if qty_unit == 'lots':
            if lot <= 0:
                raise RowError('qty_unit is lots but the row has no lotSz')
            net *= lot
        return cls(symbol=str(r.get('trdSym') or r.get('sym') or ''),
                   product=str(r.get('prod') or ''), net_qty=net, raw=r, lot_size=lot)


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    token: str
    side: str
    qty: int
    price: str
    tag: str


class Broker:
    name = 'abstract'

    def ensure_session(self) -> None:
        raise NotImplementedError

    def place(self, req: OrderRequest) -> str:
        raise NotImplementedError

    def cancel(self, order_no: str) -> None:
        raise NotImplementedError

    def order_book(self) -> List[OrderView]:
        raise NotImplementedError

    def order(self, order_no: str) -> Optional[OrderView]:
        for o in self.order_book():
            if o.order_no == str(order_no):
                return o
        return None

    def positions(self) -> List[PositionView]:
        raise NotImplementedError

    def trades(self) -> List[OrderView]:
        raise NotImplementedError

    def available_funds(self) -> Optional[float]:
        """Rupees available for a new BUY, or None when the limits response
        has no usable figure (the caller FAILS CLOSED on None)."""
        raise NotImplementedError

    def holdings(self, force: bool = False) -> Optional[List[PositionView]]:
        """Portfolio holdings as a POSITIVE source for a carried lot, or None
        when the broker offers no usable holdings read. Unverified until
        Stage 3; never a reason to sell on its own absence. `force` reads it
        even when the engine would not (the probe's measurement, R3-17)."""
        return None

    def net_long(self, symbol: str, product: str) -> int:
        return sum(p.net_qty for p in self.positions()
                   if p.symbol == symbol and (not product or p.product == product))

    def position_row(self, symbol: str, product: str) -> Optional[PositionView]:
        """The row for `symbol`, or None when the report has NO row for it —
        which is not the same as a row showing zero (R2-01/02/03)."""
        rows = [p for p in self.positions()
                if p.symbol == symbol and (not product or p.product == product)]
        if not rows:
            return None
        return PositionView(symbol=symbol, product=product, net_qty=sum(p.net_qty for p in rows),
                            raw=rows[0].raw, lot_size=rows[0].lot_size)


# ------------------------------------------------------------- real NEO
class NeoBroker(Broker):
    name = 'neo'

    def __init__(self, trader: NeoTrader, segment: str, product: str,
                 limits_net_field: str, qty_unit: str = 'shares',
                 holdings_enabled: bool = False):
        self.t = trader
        self.segment = segment
        self.product = product
        self.limits_net_field = limits_net_field
        self.qty_unit = qty_unit
        self.holdings_enabled = holdings_enabled

    def ensure_session(self) -> None:
        if not self.t.ready:
            try:
                self.t.login()
            except NeoAuthError as exc:
                raise BrokerAuth(str(exc))

    def place(self, req: OrderRequest) -> str:
        try:
            return self.t.place(symbol=req.symbol, token=req.token, side=req.side,
                                qty=req.qty, price=req.price, tag=req.tag,
                                segment=self.segment, product=self.product)
        except NeoRejected as exc:
            raise BrokerRejected(exc.code, exc.msg)
        except NeoUnclear as exc:
            raise BrokerUnclear(str(exc))
        except NeoAuthError as exc:
            raise BrokerAuth(str(exc))

    def cancel(self, order_no: str) -> None:
        try:
            self.t.cancel(order_no)
        except NeoRejected as exc:
            raise BrokerRejected(exc.code, exc.msg)
        except NeoUnclear as exc:
            raise BrokerUnclear(str(exc))
        except NeoAuthError as exc:
            raise BrokerAuth(str(exc))

    def _read(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except NeoUnavailable as exc:
            raise BrokerUnavailable(str(exc))
        except NeoAuthError as exc:
            raise BrokerAuth(str(exc))

    def order_book(self) -> List[OrderView]:
        return [OrderView.from_neo(r) for r in self._read(self.t.order_book)]

    def positions(self) -> List[PositionView]:
        out = []
        for r in self._read(self.t.positions):
            try:
                out.append(PositionView.from_neo(r, self.qty_unit))
            except RowError as exc:
                prod = str(r.get('prod') or '')
                if prod and prod != self.product:
                    # another product's row with a placeholder value must not
                    # make OUR report unavailable (R3-21)
                    logger.warning('positions: skipping %s row %s: %s', prod,
                                   r.get('trdSym') or r.get('sym'), exc)
                    continue
                raise BrokerUnavailable('positions: %s' % exc)
        return out

    def holdings(self, force: bool = False) -> Optional[List[PositionView]]:
        if not self.holdings_enabled and not force:
            return None
        out = []
        for r in self._read(self.t.holdings):
            sym = str(r.get('trdSym') or r.get('sym') or r.get('symbol') or '')
            qty = r.get('quantity', r.get('qty', r.get('holdingQty')))
            if not sym or qty in (None, ''):
                continue
            try:
                out.append(PositionView(symbol=sym, product=self.product,
                                        net_qty=int(float(str(qty))), raw=r))
            except (TypeError, ValueError):
                raise BrokerUnavailable('holdings: quantity %r unreadable' % qty)
        return out

    def trades(self) -> List[OrderView]:
        return [OrderView.from_neo(r) for r in self._read(self.t.trades)]

    def available_funds(self) -> Optional[float]:
        data = self._read(self.t.limits)
        v = data.get(self.limits_net_field)
        if v is None and isinstance(data.get('data'), dict):
            v = data['data'].get(self.limits_net_field)
        if v is None:
            logger.error('limits response has no %r; keys: %s', self.limits_net_field,
                         sorted(data.keys())[:30])
            return None
        try:
            return float(str(v).replace(',', ''))
        except ValueError:
            logger.error('limits %r is not a number: %r', self.limits_net_field, v)
            return None


# --------------------------------------------------------------- fake
class FakeBroker(Broker):
    """In-memory broker with scripted faults and realistic lags. See the
    module docstring. `path` persists it across processes."""
    name = 'fake'

    def __init__(self, clock: Callable[[], datetime], fill_delay: float = 0.0,
                 funds: float = 1e7, path: Optional[Path] = None):
        self.clock = clock
        self.fill_delay = fill_delay
        self.funds = funds
        self.path = Path(path) if path else None
        self.orders: List[Dict[str, Any]] = []
        self.ext_positions: Dict[str, int] = {}      # lots we did not create (carried)
        self.ext_positions_day: Dict[str, str] = {}  # the day they were put there
        self.sent: List[OrderRequest] = []
        self._seq = itertools.count(1)
        self.logged_in = False
        self.login_fail = False
        # scripted faults, consumed in order
        self.place_faults: List[Any] = []     # 'timeout_reached' | 'timeout_lost' | '403' | '502' | 'auth' | ('reject', code, msg) | ('placed_rejected', reason)
        self.cancel_faults: List[Any] = []    # 'timeout' | ('reject', code, msg) | 'partial'
        self.read_faults: List[Any] = []      # 'unavailable' consumed by every read
        self.funds_none = False
        self.fill_price_override: Optional[float] = None
        self.hold_fills = False               # orders never fill until released
        self.exchange_cancelled: List[str] = []
        # hostile behaviours (review NEO-23); all off by default
        self.visibility_lag_sec = 0.0         # a placed order shows up only after this
        self.positions_lag_sec = 0.0          # a fill shows in positions only after this
        self.carry_forward_rows = True        # False: yesterday's lots have no position row
        self.daily_book_reset = True          # yesterday's orders leave the DAY book
        self.cancel_pending_sec = 0.0         # 'cancel pending' before 'cancelled'
        self.blank_book = False               # the book answers Ok with nothing in it
        self.holdings_source: Optional[Dict[str, int]] = None   # None = no holdings read
        self.qty_unit = 'shares'              # 'lots': rows report in lots (measurement 6)
        self.lot_size = 65
        if self.path and self.path.exists():
            self._load()

    # -- persistence -----------------------------------------------------
    def _save(self) -> None:
        if not self.path:
            return
        data: Dict[str, Any] = {'orders': self.orders, 'ext_positions': self.ext_positions,
                'ext_positions_day': self.ext_positions_day, 'seq': next(self._seq) - 1}
        self._seq = itertools.count(data['seq'] + 1)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix='.fake.', suffix='.tmp')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, default=str)
        os.replace(tmp, self.path)

    def _load(self) -> None:
        assert self.path is not None
        with open(self.path, encoding='utf-8') as f:
            data = json.load(f)
        self.orders = list(data.get('orders') or [])
        self.ext_positions = {k: int(v) for k, v in (data.get('ext_positions') or {}).items()}
        self.ext_positions_day = dict(data.get('ext_positions_day') or {})
        self._seq = itertools.count(int(data.get('seq') or 0) + 1)

    # -- helpers ---------------------------------------------------------
    def _placed(self, o: Dict[str, Any]) -> datetime:
        v = o['_placed']
        return v if isinstance(v, datetime) else datetime.fromisoformat(str(v))

    def _today(self) -> date:
        return self.clock().date()

    def _settle(self) -> None:
        now = self.clock()
        for o in self.orders:
            placed = self._placed(o)
            if o['ordSt'] in TERMINAL:
                continue
            if self.daily_book_reset and placed.date() < now.date():
                o['ordSt'] = 'cancelled'                  # exchange mass-cancel at the close
                o['_cancelled_at'] = now.isoformat()
                continue
            if o['ordSt'] == 'cancel pending':
                at = datetime.fromisoformat(str(o['_cancel_at']))
                if (now - at).total_seconds() >= self.cancel_pending_sec:
                    o['ordSt'] = 'cancelled'
                continue
            if self.hold_fills:
                continue
            if o['nOrdNo'] in self.exchange_cancelled:
                o['ordSt'] = 'cancelled'
                continue
            if (now - placed).total_seconds() >= self.fill_delay:
                price = self.fill_price_override if self.fill_price_override is not None \
                    else float(o['prc'])
                o.update({'ordSt': 'complete', 'fldQty': o['qty'], 'unFldSz': '0',
                          'avgPrc': '%.2f' % price, '_filled_at': now.isoformat()})
        self._save()

    def _visible(self, o: Dict[str, Any]) -> bool:
        now = self.clock()
        if (now - self._placed(o)).total_seconds() < self.visibility_lag_sec:
            return False
        if self.daily_book_reset and self._placed(o).date() < now.date():
            return False
        return True

    def _net(self, lagged: bool = True) -> Dict[str, Dict[str, int]]:
        """symbol -> {'fl': today's net, 'cf': carried net}."""
        now = self.clock()
        net: Dict[str, Dict[str, int]] = {}
        for s, q in self.ext_positions.items():
            d = self.ext_positions_day.get(s, '')
            bucket = 'cf' if d < now.date().isoformat() else 'fl'
            net.setdefault(s, {'fl': 0, 'cf': 0})[bucket] += q
        for o in self.orders:
            f = _i(o.get('fldQty'))
            if not f:
                continue
            filled_at = o.get('_filled_at')
            if lagged and filled_at and self.positions_lag_sec:
                if (now - datetime.fromisoformat(str(filled_at))).total_seconds() < self.positions_lag_sec:
                    continue
            bucket = 'cf' if self._placed(o).date() < now.date() else 'fl'
            net.setdefault(o['trdSym'], {'fl': 0, 'cf': 0})[bucket] += (
                f if o['trnsTp'] == 'B' else -f)
        return net

    def true_net(self, symbol: str) -> int:
        """What the exchange really holds (tests read this; the engine cannot)."""
        n = self._net(lagged=False).get(symbol, {'fl': 0, 'cf': 0})
        return n['fl'] + n['cf']

    def _take(self, faults: List[Any]) -> Any:
        return faults.pop(0) if faults else None

    def set_external_position(self, symbol: str, qty: int, day: Optional[date] = None) -> None:
        self.ext_positions[symbol] = qty
        self.ext_positions_day[symbol] = (day or self._today()).isoformat()
        self._save()

    # -- Broker ------------------------------------------------------------
    def ensure_session(self) -> None:
        if self.login_fail:
            raise BrokerAuth('fake login refused')
        self.logged_in = True

    def _new_order(self, req: OrderRequest) -> Dict[str, Any]:
        no = '2611%08d' % next(self._seq)
        now = self.clock()
        o = {'nOrdNo': no, 'ordSt': 'open', 'trdSym': req.symbol, 'trnsTp': req.side,
             'qty': str(req.qty), 'fldQty': '0', 'unFldSz': str(req.qty),
             'avgPrc': '0.00', 'prc': req.price, 'rejRsn': '--', 'GuiOrdId': req.tag,
             'ordDtTm': now.strftime('%d-%b-%Y %H:%M:%S'), 'prod': 'NRML',
             'tok': req.token, '_placed': now.isoformat()}
        self.orders.append(o)
        self._save()
        return o

    def place(self, req: OrderRequest) -> str:
        self.sent.append(req)
        fault = self._take(self.place_faults)
        if fault == '403':
            raise BrokerUnclear('place: HTTP 403 (session re-established)')
        if fault == 'auth':
            raise BrokerAuth('place: HTTP 403 and re-login failed')
        if fault == '502':
            raise BrokerUnclear('place: HTTP 502 Bad Gateway')
        if fault == 'timeout_lost':
            raise BrokerUnclear('place: timed out (never reached the exchange)')
        if fault == 'timeout_reached':
            self._new_order(req)                 # it DID reach the exchange
            raise BrokerUnclear('place: timed out (reached the exchange)')
        if isinstance(fault, tuple) and fault[0] == 'reject':
            raise BrokerRejected(str(fault[1]), str(fault[2]))
        if isinstance(fault, tuple) and fault[0] == 'placed_rejected':
            o = self._new_order(req)
            o.update({'ordSt': 'rejected', 'rejRsn': str(fault[1])})
            self._save()
            return str(o['nOrdNo'])
        return str(self._new_order(req)['nOrdNo'])

    def cancel(self, order_no: str) -> None:
        fault = self._take(self.cancel_faults)
        if fault == 'timeout':
            raise BrokerUnclear('cancel: timed out')
        if isinstance(fault, tuple) and fault[0] == 'reject':
            raise BrokerRejected(str(fault[1]), str(fault[2]))
        self._settle()
        for o in self.orders:
            if o['nOrdNo'] == str(order_no):
                if o['ordSt'] in TERMINAL:
                    raise BrokerRejected('1006', 'Order already cancelled or executed')
                if fault == 'partial':
                    half = max(1, _i(o['qty']) // 2)
                    o.update({'fldQty': str(half), 'avgPrc': o['prc'],
                              'unFldSz': str(_i(o['qty']) - half),
                              '_filled_at': self.clock().isoformat()})
                if self.cancel_pending_sec > 0:
                    o['ordSt'] = 'cancel pending'
                    o['_cancel_at'] = self.clock().isoformat()
                else:
                    o['ordSt'] = 'cancelled'
                self._save()
                return
        raise BrokerRejected('1006', 'Order not found')

    def _check_read(self) -> None:
        if self._take(self.read_faults) == 'unavailable':
            raise BrokerUnavailable('fake: book unreadable')

    def _public(self, o: Dict[str, Any]) -> Dict[str, Any]:
        return {k: v for k, v in o.items() if not k.startswith('_')}

    def order_book(self) -> List[OrderView]:
        self._check_read()
        self._settle()
        if self.blank_book:
            return []
        return [OrderView.from_neo(self._public(o)) for o in self.orders if self._visible(o)]

    def positions(self) -> List[PositionView]:
        self._check_read()
        self._settle()
        out = []
        for s, n in self._net().items():
            fl, cf = n['fl'], n['cf']
            if not self.carry_forward_rows and fl == 0:
                continue                              # no trade today: no row (REST doc)
            if fl == 0 and cf == 0:
                continue
            div = self.lot_size if self.qty_unit == 'lots' else 1
            raw = {'trdSym': s, 'prod': 'NRML', 'flBuyQty': str(max(fl, 0) // div),
                   'flSellQty': str(max(-fl, 0) // div), 'cfBuyQty': str(max(cf, 0) // div),
                   'cfSellQty': str(max(-cf, 0) // div), 'lotSz': str(self.lot_size),
                   'actId': 'ACCOUNTID'}
            out.append(PositionView.from_neo(raw, 'shares'))   # as a naive reader would
        return out

    def holdings(self, force: bool = False) -> Optional[List[PositionView]]:
        if self.holdings_source is None:
            return None
        self._check_read()
        return [PositionView(symbol=s, product='NRML', net_qty=q, raw={'symbol': s, 'quantity': str(q)})
                for s, q in self.holdings_source.items()]

    def trades(self) -> List[OrderView]:
        self._check_read()
        self._settle()
        return [OrderView.from_neo(self._public(o)) for o in self.orders
                if _i(o.get('fldQty')) > 0 and self._visible(o)]

    def available_funds(self) -> Optional[float]:
        self._check_read()
        return None if self.funds_none else self.funds

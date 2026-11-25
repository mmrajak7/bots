"""In-memory fake of the Kite calls broker.Api needs. Used by the tests and by dry runs on the PC.

Behaviour knobs let a test plant: rejections, a network failure after the order reached the exchange (the
duplicate-order trap), fills (full / partial / none), and per-day order books.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import util
from .broker import NetworkError, Rejected


@dataclass
class FakeOrder:
    order_id: str
    tag: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    price: float
    status: str = "OPEN"
    filled_quantity: int = 0
    average_price: float = 0.0
    status_message: str = ""


@dataclass
class FakeKite:
    user_id: str = "YL6478"
    cash: float = 600000.0
    ltp: Dict[str, float] = field(default_factory=dict)            # "NSE:SYM" -> price
    upper: Dict[str, float] = field(default_factory=dict)
    holdings_qty: Dict[str, int] = field(default_factory=dict)       # isin -> qty (non-book shares too)
    reject_next: Optional[str] = None                                 # message for the next place()
    async_reject_next: Optional[str] = None                           # next place() returns an id; the order-book row
    #                                                                   is REJECTED with this text (Kite's usual path)
    no_circuit: bool = False                                          # quotes carry no circuit limits
    not_listed: List[str] = field(default_factory=list)                # tradingsymbols missing from instruments()
    fail_writes: int = 0                                              # next N modify/cancel calls raise NetworkError
    lose_reply_next: bool = False                                     # order IS placed, then NetworkError
    raw_error_next: Optional[BaseException] = None                    # order IS placed, then this raw exception
    fail_reads: int = 0                                               # next N read calls raise NetworkError
    on_place: Optional[Callable[[FakeOrder], None]] = None
    orders_: List[FakeOrder] = field(default_factory=list)
    trades_: List[Dict[str, Any]] = field(default_factory=list)
    calls: List[str] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1001))
    _tids: Any = field(default_factory=lambda: itertools.count(9001))

    def _r(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_reads > 0:
            self.fail_reads -= 1
            raise NetworkError(f"planted read failure in {name}")

    # reads
    def profile(self) -> Dict[str, Any]:
        self._r("profile")
        return {"user_id": self.user_id}

    def holdings(self) -> List[Dict[str, Any]]:
        self._r("holdings")
        return [{"isin": i, "quantity": q, "t1_quantity": 0} for i, q in self.holdings_qty.items()]

    def margins(self) -> Dict[str, Any]:
        self._r("margins")
        return {"equity": {"net": self.cash, "available": {"live_balance": self.cash, "cash": self.cash}}}

    def quote(self, keys: List[str]) -> Dict[str, Any]:
        self._r("quote")
        out = {}
        for k in keys:
            if k in self.ltp:
                p = self.ltp[k]
                out[k] = {"last_price": p, "last_trade_time": f"{util.today().isoformat()} 14:45:00"}
                if not self.no_circuit:
                    out[k]["upper_circuit_limit"] = self.upper.get(k, round(p * 1.2, 2))
                    out[k]["lower_circuit_limit"] = round(p * 0.8, 2)
        return out

    def orders(self) -> List[Dict[str, Any]]:
        self._r("orders")
        return [{"order_id": o.order_id, "tag": o.tag, "tradingsymbol": o.tradingsymbol, "status": o.status,
                 "quantity": o.quantity, "filled_quantity": o.filled_quantity,
                 "pending_quantity": o.quantity - o.filled_quantity if o.status == "OPEN" else 0,
                 "price": o.price, "average_price": o.average_price, "transaction_type": o.transaction_type,
                 "status_message": o.status_message}
                for o in self.orders_]

    def trades(self) -> List[Dict[str, Any]]:
        self._r("trades")
        return list(self.trades_)

    def instruments(self, exchange: str) -> List[Dict[str, Any]]:
        self._r("instruments")
        return [{"tradingsymbol": k.split(":")[1], "exchange": "NSE", "tick_size": 0.05, "instrument_token": n}
                for n, k in enumerate(self.ltp, 1) if k.split(":")[1] not in self.not_listed]

    def historical(self, token: int, frm: str, to: str, interval: str) -> List[Dict[str, Any]]:
        self._r("historical")
        return []

    # writes
    def place(self, **kw: Any) -> str:
        self.calls.append("place")
        if self.reject_next:
            msg, self.reject_next = self.reject_next, None
            raise Rejected(msg)
        o = FakeOrder(str(next(self._ids)), kw["tag"], kw["tradingsymbol"], kw["transaction_type"],
                      int(kw["quantity"]), float(kw["price"]))
        self.orders_.append(o)
        if self.async_reject_next:
            o.status, o.status_message, self.async_reject_next = "REJECTED", self.async_reject_next, None
        if self.on_place:
            self.on_place(o)
        if self.raw_error_next is not None:
            e, self.raw_error_next = self.raw_error_next, None
            raise e
        if self.lose_reply_next:
            self.lose_reply_next = False
            raise NetworkError("planted: reply lost after the order reached the exchange")
        return o.order_id

    def modify(self, order_id: str, price: float) -> str:
        self.calls.append("modify")
        self._w("modify")
        o = self._get(order_id)
        if o.status != "OPEN":
            raise RuntimeError(f"order {order_id} is {o.status}")
        o.price = float(price)
        return order_id

    def cancel(self, order_id: str) -> str:
        self.calls.append("cancel")
        self._w("cancel")
        o = self._get(order_id)
        if o.status not in ("COMPLETE", "CANCELLED", "REJECTED"):
            o.status = "CANCELLED"
        return order_id

    def _w(self, name: str) -> None:
        if self.fail_writes > 0:
            self.fail_writes -= 1
            raise NetworkError(f"planted write failure in {name}")

    # test helpers
    def _get(self, oid: str) -> FakeOrder:
        for o in self.orders_:
            if o.order_id == oid:
                return o
        raise RuntimeError(f"no order {oid}")

    def fill(self, oid: str, qty: Optional[int] = None, price: Optional[float] = None) -> None:
        o = self._get(oid)
        q = o.quantity - o.filled_quantity if qty is None else qty
        px = o.price if price is None else price
        o.average_price = ((o.average_price * o.filled_quantity) + px * q) / (o.filled_quantity + q)
        o.filled_quantity += q
        if o.filled_quantity >= o.quantity:
            o.status = "COMPLETE"
        self.trades_.append({"trade_id": str(next(self._tids)), "order_id": oid, "quantity": q, "average_price": px,
                             "tradingsymbol": o.tradingsymbol, "exchange_timestamp": "2026-12-01 15:12:00"})

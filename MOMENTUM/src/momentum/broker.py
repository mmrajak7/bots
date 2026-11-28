"""The ONE choke point to Kite (build spec §C3; frozen design §6.1, §6.6, E01, E02, E12).

Two layers:
  KiteApi  - thin wrapper over kiteconnect: IPv4 only, <= kite_rps requests/s, READ calls retried (1, 2, 4 s).
             WRITE calls are never retried here: a lost reply on place_order could otherwise create a duplicate.
             A write that raises a network error is reported as TIMEOUT and settled by the reconcile (by tag).
  Broker   - the order gate. Every write follows journal-before-call:
               1 journal: no record for this key, no live intent on the (position, leg)
               2 broker:  today's orders() carry no tag with the (position, leg) prefix
               3 INTENT (fsync)  4 SENT (fsync)  5 the call  6 RESULT (fsync)
             Dry mode, KILL, or no covering ARM -> refuse before step 3 (DRY_* record only).
Only LIMIT / regular / CNC / DAY / NSE orders exist in this code: AMO, MARKET, SL and GTT are not expressible.
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Protocol

from . import ledger as L
from .journal import Journal

log = logging.getLogger("momentum.broker")

TAG_RE = re.compile(r"^M\d{4}[SBURPX]\d{2}A[1-9A-Z]$")
ATTEMPT_CHARS = "123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # attempt 1..35 (a sell locked for weeks keeps going)
MAX_ATTEMPT = len(ATTEMPT_CHARS)
LEG_LETTER = {"sell": "S", "buy": "B", "topup": "U", "residual": "R", "piece": "P", "test": "X"}

# Kite order states that can never trade again. Every other state (OPEN, OPEN PENDING, MODIFY PENDING, VALIDATION
# PENDING, PUT ORDER REQ RECEIVED, TRIGGER PENDING, ...) is treated as LIVE: it is cancelled at 15:28 / on KILL and it
# blocks a second order on the same (position, leg).
DEAD_ORDER_STATES = frozenset({"COMPLETE", "CANCELLED", "REJECTED"})

_ipv4_patched = False


def order_state(o: Dict[str, Any]) -> str:
    return str(o.get("status") or "").upper()


def order_is_live(o: Dict[str, Any]) -> bool:
    return order_state(o) not in DEAD_ORDER_STATES


def reject_text(o: Dict[str, Any]) -> str:
    """The exchange / RMS reason of a REJECTED order-book row (Kite: status_message, status_message_raw)."""
    return f"{o.get('status_message') or ''} {o.get('status_message_raw') or ''}".strip()


def patch_ipv4() -> None:
    """Kite's IP whitelist holds the home IPv4 only (same fix as FIFTY/src/api/dual_kite_client.py:31-44)."""
    global _ipv4_patched
    if _ipv4_patched:
        return
    orig = socket.getaddrinfo

    def v4(host: Any, port: Any, family: int = 0, type: int = 0, proto: int = 0, flags: int = 0) -> Any:
        return orig(host, port, socket.AF_INET, type, proto, flags)
    socket.getaddrinfo = v4  # type: ignore[assignment]
    _ipv4_patched = True


def make_tag(yymm: str, leg: str, ordinal: int, attempt: int) -> str:
    if not (1 <= ordinal <= 99):
        raise ValueError(f"ordinal {ordinal} out of 1..99")
    if not (1 <= attempt <= MAX_ATTEMPT):
        raise ValueError(f"attempt {attempt} out of 1..{MAX_ATTEMPT} (owner hold)")
    tag = f"M{yymm}{LEG_LETTER[leg]}{ordinal:02d}A{ATTEMPT_CHARS[attempt - 1]}"
    assert TAG_RE.match(tag), tag
    return tag


def tag_prefix(tag: str) -> str:
    """M2612B07A1 -> M2612B07: identifies the (position, leg)."""
    return tag[:-2]


class BrokerRefused(Exception):
    """Raised before anything is journalled as an order: the caller defers the leg (no lock try)."""


class Api(Protocol):
    def profile(self) -> Dict[str, Any]: ...
    def holdings(self) -> List[Dict[str, Any]]: ...
    def margins(self) -> Dict[str, Any]: ...
    def quote(self, keys: List[str]) -> Dict[str, Any]: ...
    def orders(self) -> List[Dict[str, Any]]: ...
    def trades(self) -> List[Dict[str, Any]]: ...
    def instruments(self, exchange: str) -> List[Dict[str, Any]]: ...
    def historical(self, token: int, frm: str, to: str, interval: str) -> List[Dict[str, Any]]: ...
    def place(self, **kw: Any) -> str: ...
    def modify(self, order_id: str, price: float) -> str: ...
    def cancel(self, order_id: str) -> str: ...


class NetworkError(Exception):
    """No definite answer from Kite. For a WRITE the order may or may not exist: TIMEOUT, settled by tag."""


class AuthError(NetworkError):
    """A READ refused for the session (token replaced / expired). A NetworkError subclass on purpose: every stage
    already fails closed on NetworkError (skip, alert, next session) instead of crashing, so the 15:28 cancel and
    the 15:35 reconcile still run on a resume once a fresh token is read (review 11-28 M5/O9)."""


class Rejected(Exception):
    """A definite Kite rejection of a write (Kite's own error_type). Only these become REJECTED_*."""


class KiteApi:
    """kiteconnect wrapper. Imported lazily so tests and the dev PC never need a token."""

    def __init__(self, api_key: str, access_token: str, rps: int, retry_s: List[int]) -> None:
        patch_ipv4()
        from kiteconnect import KiteConnect  # noqa: WPS433 (lazy on purpose)
        self.k = KiteConnect(api_key=api_key)
        self.k.set_access_token(access_token)
        self.min_gap = 1.0 / max(1, rps)
        self.retry_s = list(retry_s)
        self._last = 0.0
        self._lock = threading.Lock()

    def _pace(self) -> None:
        with self._lock:
            wait = self._last + self.min_gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def _read(self, fn: Callable[..., Any], *a: Any, **k: Any) -> Any:
        """Retried on anything that is not a definite Kite refusal. requests' own exceptions pass through
        kiteconnect unwrapped (kiteconnect/connect.py:914-916), so they are caught generically here."""
        from kiteconnect import exceptions as kx
        errs: List[str] = []
        for delay in [0] + self.retry_s:
            if delay:
                time.sleep(delay)
            self._pace()
            try:
                return fn(*a, **k)
            except (kx.TokenException, kx.PermissionException) as e:
                raise AuthError(f"{type(e).__name__}: {e}") from e   # retrying cannot help; the caller fails closed
            except kx.InputException:
                raise                                   # a bad request (our bug): never retried, never hidden
            except Exception as e:  # noqa: BLE001 - network / gateway / parse errors: retry
                errs.append(f"{type(e).__name__}: {e}")
        raise NetworkError("; ".join(errs))

    def _write(self, fn: Callable[..., Any], *a: Any, **k: Any) -> Any:
        """Never retried. Only Kite's own error types are a definite rejection; anything else = unknown outcome."""
        from kiteconnect import exceptions as kx
        self._pace()
        try:
            return fn(*a, **k)
        except (kx.NetworkException, kx.DataException, kx.GeneralException) as e:
            # GeneralException is Kite's server-side error (HTTP 5xx): the order may still exist -> unknown outcome,
            # settled by its exact tag (review 11-28 C1), never a final rejection
            raise NetworkError(f"{type(e).__name__}: {e}") from e
        except (kx.OrderException, kx.InputException, kx.TokenException, kx.PermissionException) as e:
            raise Rejected(f"{type(e).__name__}: {e}") from e
        except Exception as e:  # noqa: BLE001 - requests.Timeout / ConnectionError / anything else
            raise NetworkError(f"{type(e).__name__}: {e}") from e

    def profile(self) -> Dict[str, Any]:
        return self._read(self.k.profile)

    def holdings(self) -> List[Dict[str, Any]]:
        return self._read(self.k.holdings)

    def margins(self) -> Dict[str, Any]:
        return self._read(self.k.margins)

    def quote(self, keys: List[str]) -> Dict[str, Any]:
        return self._read(self.k.quote, keys)

    def orders(self) -> List[Dict[str, Any]]:
        return self._read(self.k.orders)

    def trades(self) -> List[Dict[str, Any]]:
        return self._read(self.k.trades)

    def instruments(self, exchange: str) -> List[Dict[str, Any]]:
        return self._read(self.k.instruments, exchange)

    def historical(self, token: int, frm: str, to: str, interval: str) -> List[Dict[str, Any]]:
        return self._read(self.k.historical_data, token, frm, to, interval)

    def place(self, **kw: Any) -> str:
        k = self.k
        return self._write(k.place_order, variety=k.VARIETY_REGULAR, exchange=k.EXCHANGE_NSE,
                           transaction_type=kw["transaction_type"], tradingsymbol=kw["tradingsymbol"],
                           quantity=kw["quantity"], product=k.PRODUCT_CNC, order_type=k.ORDER_TYPE_LIMIT,
                           price=kw["price"], validity=k.VALIDITY_DAY, tag=kw["tag"])

    def modify(self, order_id: str, price: float) -> str:
        return self._write(self.k.modify_order, variety=self.k.VARIETY_REGULAR, order_id=order_id, price=price)

    def cancel(self, order_id: str) -> str:
        return self._write(self.k.cancel_order, variety=self.k.VARIETY_REGULAR, order_id=order_id)


@dataclass
class Gate:
    """Conditions checked immediately before every broker write."""
    live: bool                       # config mode == live
    armed: Callable[[], bool]        # ARM covers the plan month today
    killed: Callable[[], bool]       # KILL file present


class Broker:
    def __init__(self, api: Api, journal: Journal, gate: Gate) -> None:
        self.api, self.j, self.gate = api, journal, gate

    # ---------------------------------------------------------------- guards
    def _refuse_reason(self) -> Optional[str]:
        if self.gate.killed():
            return "KILL"
        if not self.gate.live:
            return "DRY_MODE"
        if not self.gate.armed():
            return "NOT_ARMED"
        return None

    def _today_tags(self) -> Dict[str, Dict[str, Any]]:
        return {o["tag"]: o for o in self.api.orders() if o.get("tag")}

    # ---------------------------------------------------------------- writes
    def place_buy(self, *, intent_id: str, key: Dict[str, Any], tag: str, isin: str, tradingsymbol: str, qty: int,
                  limit: float, ltp_1510: float, cap_price: float, max_rupees: int) -> L.Intent:
        if not TAG_RE.match(tag):
            raise BrokerRefused(f"bad tag {tag}")
        if qty <= 0:
            raise BrokerRefused(f"qty {qty}")
        if qty * limit > max_rupees + 1e-6:
            raise BrokerRefused(f"qty x limit {qty * limit:.2f} > max rupees {max_rupees}")
        if limit <= 0 or limit > cap_price + 1e-6 or qty * cap_price > max_rupees + 1e-6:
            raise BrokerRefused(f"limit {limit} / cap {cap_price} x qty {qty} outside the envelope ({max_rupees})")
        return self._place("BUY", intent_id=intent_id, key=key, tag=tag, isin=isin, tradingsymbol=tradingsymbol,
                           qty=qty, limit=limit, extra={"ltp_1510_paise": L.paise(ltp_1510),
                                                        "cap_price_paise": L.paise(cap_price),
                                                        "max_rupees": max_rupees})

    def place_sell(self, *, intent_id: str, key: Dict[str, Any], tag: str, isin: str, tradingsymbol: str, qty: int,
                   limit: float, ltp: float, floor_price: float, max_qty: int) -> L.Intent:
        """A book sell (January spec §8): never more than the position's ledger shares (max_qty, checked again here
        from the journal) and never priced under its floor (the ladder's lowest step)."""
        if not TAG_RE.match(tag):
            raise BrokerRefused(f"bad tag {tag}")
        if qty <= 0 or qty > max_qty:
            raise BrokerRefused(f"sell qty {qty} outside 1..{max_qty}")
        held = L.replay(self.j.read()).positions[key["position_id"]].held_qty
        if qty > held:
            raise BrokerRefused(f"sell qty {qty} > ledger shares {held} of {key['position_id']}")
        if limit <= 0 or floor_price <= 0 or limit < floor_price - 1e-6:
            raise BrokerRefused(f"sell limit {limit} under its floor {floor_price}")
        return self._place("SELL", intent_id=intent_id, key=key, tag=tag, isin=isin, tradingsymbol=tradingsymbol,
                           qty=qty, limit=limit, extra={"ltp_paise": L.paise(ltp),
                                                        "floor_price_paise": L.paise(floor_price)})

    def _place(self, side: str, *, intent_id: str, key: Dict[str, Any], tag: str, isin: str, tradingsymbol: str,
               qty: int, limit: float, extra: Dict[str, Any]) -> L.Intent:
        """Frozen design §6.6 order of operations, the same for both sides: journal checks, the order book's tags,
        INTENT + fsync, SENT + fsync, the call (never retried), the result."""
        # step 1: the journal
        lg = L.replay(self.j.read())
        if intent_id in lg.intents:
            raise BrokerRefused(f"intent {intent_id} already journalled (status {lg.intents[intent_id].status})")
        live = lg.open_intents(key["position_id"])
        if live:
            raise BrokerRefused(f"live intent on {key['position_id']}: {[i.intent_id for i in live]}")
        if lg.unresolved:
            raise BrokerRefused(f"unresolved intents block every order: {lg.unresolved}")
        why = self._refuse_reason()
        if why:
            if self.j.dry:   # a dry run records what it would have sent (DRY_INTENT, ignored by the live replay)
                self.j.append("INTENT", intent_id=intent_id, key=key, tag=tag, isin=isin,
                              tradingsymbol=tradingsymbol, side=side, qty=qty, limit_paise=L.paise(limit),
                              dry_reason=why)
            raise BrokerRefused(why)
        # step 2: the broker's own order book (a tag with this (position, leg) prefix placed today)
        pre = tag_prefix(tag)
        # A REJECTED order never lived, so it does not block the one corrected retry (E12); any other status does.
        clash = [t for t, o in self._today_tags().items() if t.startswith(pre) and order_state(o) != "REJECTED"]
        if clash:
            raise BrokerRefused(f"order book already has {clash} for {pre}")
        # steps 3-6
        self.j.append("INTENT", intent_id=intent_id, key=key, tag=tag, isin=isin, tradingsymbol=tradingsymbol,
                      side=side, qty=qty, limit_paise=L.paise(limit), **extra,
                      order_type="LIMIT", variety="regular", product="CNC", validity="DAY", exchange="NSE")
        self.j.append("SENT", intent_id=intent_id)
        try:
            oid = self.api.place(transaction_type=side, tradingsymbol=tradingsymbol, quantity=qty, price=limit,
                                 tag=tag)
        except Rejected as e:
            # A refusal is final only if no order carries the tag (review 11-28 C1: an exception for an order Kite
            # did create must not leave a filled order unbooked and the leg open for a second buy). A token /
            # permission refusal is decided before the order system, so it is final at once (no read on a dead token).
            if classify_reject(str(e)) == "REJECTED_AUTH":
                self.j.append("RESULT", intent_id=intent_id, outcome="REJECTED_AUTH", order_id=None,
                              message=str(e)[:500])
                return L.replay(self.j.read()).intents[intent_id]
            try:
                found: Optional[Dict[str, Any]] = self._today_tags().get(tag)
                unreadable = ""
            except Exception as e2:  # noqa: BLE001 - cannot prove absence: unknown outcome, settled by tag
                found, unreadable = None, f"{type(e2).__name__}: {str(e2)[:200]}"
            if found is not None:
                self.j.append("RESULT", intent_id=intent_id, outcome="ACK", order_id=str(found["order_id"]),
                              message=f"refusal text but the tag is in the order book: {str(e)[:400]}")
            elif unreadable:
                self.j.append("RESULT", intent_id=intent_id, outcome="TIMEOUT", order_id=None,
                              message=f"refusal {str(e)[:250]}; order book unreadable: {unreadable}")
            else:
                self.j.append("RESULT", intent_id=intent_id, outcome=classify_reject(str(e)), order_id=None,
                              message=str(e)[:500])
        except Exception as e:  # noqa: BLE001 - NetworkError or anything unproven: the order may exist
            self.j.append("RESULT", intent_id=intent_id, outcome="TIMEOUT", order_id=None,
                          message=f"{type(e).__name__}: {str(e)[:480]}")
        else:
            self.j.append("RESULT", intent_id=intent_id, outcome="ACK", order_id=str(oid), message="")
        return L.replay(self.j.read()).intents[intent_id]

    def modify_price(self, intent_id: str, order_id: str, step_no: int, new_limit: float) -> bool:
        why = self._refuse_reason()
        if why:                                  # KILL, dry mode or no ARM: a re-price is a broker write like any other
            raise BrokerRefused(why)
        rec = [r for r in self.j.read() if r["type"] == "INTENT" and r["intent_id"] == intent_id]
        if not rec or new_limit <= 0:
            raise BrokerRefused(f"re-price {new_limit} of {intent_id}: no INTENT")
        if rec[-1].get("side") == "SELL":
            if L.paise(new_limit) < int(rec[-1]["floor_price_paise"]):
                raise BrokerRefused(f"re-price {new_limit} under the floor of {intent_id} "
                                    f"({int(rec[-1]['floor_price_paise']) / 100})")
        elif "cap_price_paise" not in rec[-1] or L.paise(new_limit) > int(rec[-1]["cap_price_paise"]):
            raise BrokerRefused(f"re-price {new_limit} above the 15:10 cap of {intent_id} "
                                f"({int(rec[-1].get('cap_price_paise', 0)) / 100})")
        self.j.append("MODIFY_INTENT", intent_id=intent_id, order_id=order_id, step_no=step_no,
                      new_limit_paise=L.paise(new_limit))
        self.j.append("MODIFY_SENT", intent_id=intent_id, order_id=order_id, step_no=step_no)
        try:
            self.api.modify(order_id, new_limit)
        except Exception as e:  # noqa: BLE001
            self.j.append("MODIFY_RESULT", intent_id=intent_id, order_id=order_id, step_no=step_no,
                          new_limit_paise=L.paise(new_limit), outcome="FAILED", message=str(e)[:500])
            return False
        self.j.append("MODIFY_RESULT", intent_id=intent_id, order_id=order_id, step_no=step_no,
                      new_limit_paise=L.paise(new_limit), outcome="OK", message="")
        return True

    def cancel(self, intent_id: str, order_id: str, reason: str) -> bool:
        """Cancelling is always allowed (it can only reduce exposure), KILL included."""
        self.j.append("CANCEL_INTENT", intent_id=intent_id, order_id=order_id, reason=reason)
        self.j.append("CANCEL_SENT", intent_id=intent_id, order_id=order_id)
        try:
            self.api.cancel(order_id)
        except Exception as e:  # noqa: BLE001
            self.j.append("CANCEL_RESULT", intent_id=intent_id, order_id=order_id, outcome="FAILED",
                          message=str(e)[:500])
            return False
        self.j.append("CANCEL_RESULT", intent_id=intent_id, order_id=order_id, outcome="OK", message="")
        return True


def classify_reject(msg: str) -> str:
    """Map a Kite rejection text to the spec's classes. Texts are UNVERIFIED (U5, U27): unknown -> REJECTED_OTHER,
    which goes to the owner (the safe default)."""
    m = msg.lower()
    if "tokenexception" in m or "permissionexception" in m or "api_key" in m or "access_token" in m:
        return "REJECTED_AUTH"          # the session died (FIFTY re-login?): nothing was sent; next session, no hold
    if any(w in m for w in ("tpin", "ddpi", "edis", "e-dis", "power of attorney", "authoris", "authoriz")):
        return "REJECTED_DDPI"          # a sell the depository did not authorise: every sell waits, the owner is told
    if "circuit" in m or "price band" in m or ("outside" in m and "range" in m):
        return "REJECTED_BAND"
    if ("market" in m and ("closed" in m or "not open" in m)) or "after market order" in m or "amo" in m.split():
        return "REJECTED_CLOSED"
    # Zerodha prefixes most OMS refusals with "RMS:" (scrip blocks, surveillance rules ...): only a funds / margin
    # text is the account-wide RMS class; any other "RMS:" text is about the stock -> OTHER (review 11-28 T4)
    if "margin" in m or "insufficient" in m or "funds" in m:
        return "REJECTED_RMS"
    return "REJECTED_OTHER"

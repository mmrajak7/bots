"""Corporate actions on book names (January spec §7: the frozen design §6.8 cut to what Dec 2026 - Mar 2027 needs).

  ca-scan (every evening)  NSE corporate actions, ex-date from today - lookback to today + ahead, parsed by the
                           research parser (nse_ca.events_from). For every name the book holds:
                             split / bonus with a clean whole multiple -> overlay PENDING_CA (the name's orders wait)
                             anything else (demerger, rights, scheme, capital reduction, consolidation, unparsed)
                               -> OWNER_HOLD + alert: the owner decides; the code never guesses a new quantity
  credit check (08:35 and   Kite holdings vs book x multiple: credited -> CA_CREDIT (quantity x multiple, cost
  before the 14:45 plan)    unchanged; the new ISIN when the split changed it, matched by the trading symbol);
                           not yet -> wait (alert after ca_lag_sessions); anything else -> OWNER_HOLD + alert.
Dividends are ignored, as tested. One NOTE ca_event per (ISIN, ex-date) so a re-scan never acts twice.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import broker as B
from . import ledger as L
from . import util

log = logging.getLogger("momentum.corpact")


def book_positions(lg: L.Ledger) -> List[L.Position]:
    """Positions whose shares are (or may soon be) in the demat: held, or a buy that has filled part."""
    return [p for p in lg.positions.values() if p.held_qty > 0]


def first_buy_day(recs: List[Dict[str, Any]], position_id: str) -> Optional[str]:
    """The day the position's first buy filled (journal FILL time). Shares bought ON or after an ex-date are
    already the new shares: no credit will come for them."""
    days = [r["ts"][:10] for r in recs if r["type"] == "FILL" and r.get("position_id") == position_id
            and r.get("side", "BUY") == "BUY"]
    return min(days) if days else None


def held_before(recs: List[Dict[str, Any]], p: L.Position, ex: str) -> bool:
    d = first_buy_day(recs, p.position_id)
    return d is not None and d < ex


def events_seen(recs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {r["ca_event"]: r for r in recs if r["type"] == "NOTE" and "ca_event" in r}


def scan(env: Any, M_CA: Any, get: Optional[Callable[[str], Tuple[int, Optional[bytes], str]]] = None
         ) -> Dict[str, Any]:
    """-> summary. A failed download is an alert (once a day), never an order block by itself: a missed split
    shows up as a holdings mismatch at the next check, which blocks that name."""
    from . import engine as E
    cfg = env.cfg
    today = util.today()
    a = today - dt.timedelta(days=int(cfg["ca_lookback_days"]))
    b = today + dt.timedelta(days=int(cfg["ca_ahead_days"]))
    status, body, err = (get or M_CA.get)(M_CA.url_for(a, b))
    try:
        if status != 200 or body is None:
            raise ValueError(err or f"HTTP {status}")
        records = M_CA.validate_body(body, a, b)
    except ValueError as e:
        recs = env.journal.read()
        if not any(r["type"] == "ALERT" and r.get("code") == "CA_SCAN" and r["ts"][:10] == today.isoformat()
                   for r in recs):
            env.alert("CA_SCAN", f"NSE corporate actions {a}..{b} not read ({str(e)[:150]}): splits / bonuses on "
                      "book names are caught only by the holdings checks until the next scan")
        return {"ok": False, "problem": str(e)[:200]}
    util.write_json_atomic(env.paths.snapshots / f"ca_{today.isoformat()}.json", records)
    fac, zero, owner = M_CA.events_from(records)
    recs = env.journal.read()
    lg = L.replay(recs)
    seen = events_seen(recs)
    by_isin: Dict[str, List[L.Position]] = {}
    for p in book_positions(lg):
        by_isin.setdefault(p.isin, []).append(p)
    acted: List[str] = []
    for ev in sorted({id(e): e for e in fac + zero + owner}.values(), key=lambda e: (e.ex, e.isin)):
        ps = [p for p in by_isin.get(ev.isin, []) if held_before(recs, p, ev.ex)]
        if not ps:                             # not held, or bought on / after the ex-date: nothing to do
            continue
        eid = f"{ev.isin}|{ev.ex}"
        if eid in seen:
            continue
        mult = round(1 / ev.factor) if ev.factor > 0 else 0
        clean = (set(ev.kinds) <= {"split", "bonus"} and ev.factor < 1 and mult >= 2
                 and abs(1 / ev.factor - mult) < 1e-9 and len(ps) == 1)
        for p in ps:
            if clean:                              # the overlay goes on near the ex-date (activate_due, below)
                env.notify(f"CA {p.symbol}: {'/'.join(ev.kinds)} x{mult}, ex {ev.ex}: from the ex-date its orders "
                           f"wait until Kite shows {p.held_qty * mult} shares (booked by code)")
            else:
                E.hold_leg(env, p, "OWNER_HOLD", f"corporate action {'/'.join(ev.kinds)} ex {ev.ex} "
                           f"({E.quote_text(' | '.join(ev.subjects))[:100]}): the code does not handle it")
                env.alert("CA_OWNER", f"{p.symbol}: {'/'.join(ev.kinds)} ex {ev.ex}: held for you (sell it, or "
                          f"fix the quantity with 'ca {p.isin} <new qty>' once Kite shows it)")
        # review 11-28: the NOTE (which makes a re-scan skip this event) only AFTER the holds: a crash in between
        # re-acts on the next scan instead of losing the hold for good
        env.journal.append("NOTE", ca_event=eid, isin=ev.isin, symbol=ev.symbol, ex=ev.ex, kinds=list(ev.kinds),
                           factor=ev.factor, mult=mult if clean else None, subjects=list(ev.subjects)[:4])
        acted.append(eid)
    activate_due(env)
    return {"ok": True, "records": len(records), "acted": acted}


def next_weekday(d: dt.date) -> dt.date:
    n = d + dt.timedelta(days=1)
    while n.weekday() >= 5:
        n += dt.timedelta(days=1)
    return n


def activate_due(env: Any) -> List[str]:
    """PENDING_CA goes on the evening before the ex-date (or later): before it the name sells its old shares at
    the old price as tested; from the ex-date until the credit its orders wait (the sell would be the old quantity
    at the new price). Run by the scan and by the morning / afternoon credit check. -> position ids switched on."""
    recs = env.journal.read()
    lg = L.replay(recs)
    horizon = next_weekday(util.today()).isoformat()
    on_for = {str(r.get("reason", "")).split()[1] for r in recs if r["type"] == "OVERLAY"
              and r["overlay"] == "PENDING_CA" and r["on"] and str(r.get("reason", "")).startswith("ca ")}
    out = []
    for eid, ev in events_seen(recs).items():
        if not ev.get("mult") or eid in on_for or str(ev.get("ex", "")) > horizon:
            continue
        for p in lg.positions.values():
            if p.isin == ev["isin"] and p.held_qty > 0 and held_before(recs, p, str(ev["ex"])):
                env.journal.append("OVERLAY", position_id=p.position_id, overlay="PENDING_CA", on=True,
                                   reason=f"ca {eid} x{ev['mult']}")
                out.append(p.position_id)
    return out


def credit_check(env: Any, rows: List[Dict[str, Any]]) -> List[str]:
    """Kite holdings rows (isin, tradingsymbol, quantity, t1_quantity) against every PENDING_CA position.
    -> lines describing what happened."""
    from . import engine as E
    activate_due(env)
    if not rows:                                   # holdings unreadable (or empty): never judge a credit on it
        return []
    recs = env.journal.read()
    lg = L.replay(recs)
    seen = events_seen(recs)
    qty = {str(r["isin"]): int(r.get("quantity", 0)) + int(r.get("t1_quantity", 0)) for r in rows}
    sym = {str(r.get("tradingsymbol", "")): r for r in rows}
    book_isins = {p.isin for p in lg.positions.values()}
    out: List[str] = []
    for p in lg.positions.values():
        if "PENDING_CA" not in p.overlays:
            continue
        on = [r for r in recs if r["type"] == "OVERLAY" and r["position_id"] == p.position_id
              and r["overlay"] == "PENDING_CA" and r["on"]][-1]
        eid = str(on["reason"]).split()[1]
        ev = seen.get(eid, {})
        mult = int(ev.get("mult") or 0)
        if mult < 2:
            E.hold_leg(env, p, "OWNER_HOLD", f"PENDING_CA {eid} has no usable multiple")
            continue
        expected = p.held_qty * mult
        have = qty.get(p.isin, 0)
        new_isin = None
        if have == 0:                              # the split may have changed the ISIN: match by trading symbol
            r = sym.get(E.tradingsymbol(p.symbol, p.series)) or sym.get(p.symbol)
            if r is not None and str(r["isin"]) != p.isin and str(r["isin"]) not in book_isins:
                new_isin, have = str(r["isin"]), int(r.get("quantity", 0)) + int(r.get("t1_quantity", 0))
        if have == expected:
            env.journal.append("CA_CREDIT", position_id=p.position_id, ca_id=eid, factor_num=mult, factor_den=1,
                               qty_before=p.held_qty, qty_after=expected, new_isin=new_isin,
                               isin_before=p.isin)
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="PENDING_CA", on=False,
                               reason=f"credited {expected}")
            msg = f"CA {p.symbol}: credited, book now {expected} shares" + (f" (new ISIN {new_isin})"
                                                                             if new_isin else "")
            env.notify(msg)
            out.append(msg)
        elif have == p.held_qty or (have == 0 and new_isin is None):
            # not credited yet; or (review 11-28 F4) a split changing the ISIN in transit: the old ISIN debited, the
            # new one not credited yet (Zerodha shows neither for a day or two): keep waiting, CA_LATE after the lag
            ex = str(ev.get("ex", ""))
            late = ex and (util.today() - dt.date.fromisoformat(ex)).days > int(env.cfg["ca_lag_days"])
            if late and not any(r["type"] == "ALERT" and r.get("code") == "CA_LATE" and r["ts"][:10]
                                == util.today().isoformat() and p.symbol in r.get("text", "") for r in recs):
                env.alert("CA_LATE", f"{p.symbol}: ex {ex}, Kite still shows {have} (expected {expected}): its "
                          "orders keep waiting")
            out.append(f"{p.symbol}: waiting for the credit ({have} of {expected})")
        else:
            E.hold_leg(env, p, "OWNER_HOLD", f"corporate action {eid}: Kite shows {have}, the book expects "
                       f"{p.held_qty} before or {expected} after the credit")
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="PENDING_CA", on=False,
                               reason="mismatch: owner")
            env.alert("CA_MISMATCH", f"{p.symbol}: Kite {have} vs book {p.held_qty} x{mult} = {expected}: held for "
                      f"you; fix with 'ca {p.isin} <qty>' once you have checked Kite")
            out.append(f"{p.symbol}: mismatch")
    return out


def owner_set_qty(env: Any, isin: str, new_qty: int, new_isin: Optional[str] = None) -> str:
    """Owner command `ca <isin> <qty> [<new isin>]`: book a corporate-action credit the code could not (journalled);
    the optional new ISIN moves the book to it (review 11-28 F4: a split that changed the ISIN)."""
    lg = L.replay(env.journal.read())
    ps = [p for p in lg.positions.values() if p.isin == isin and p.held_qty > 0]
    if len(ps) != 1 or new_qty <= 0:
        return f"ca {isin}: needs exactly one held book position and a positive quantity"
    if new_isin is not None and (len(new_isin) != 12 or not new_isin.isalnum() or new_isin == isin
                                 or any(q.isin == new_isin for q in lg.positions.values())):
        return f"ca {isin}: new ISIN {new_isin} must be 12 letters/digits, different, and not already in the book"
    p = ps[0]
    env.journal.append("CA_CREDIT", position_id=p.position_id, ca_id="owner", factor_num=new_qty,
                       factor_den=p.held_qty, qty_before=p.held_qty, qty_after=new_qty, new_isin=new_isin,
                       isin_before=p.isin)
    for ov in ("PENDING_CA",):
        if ov in p.overlays:
            env.journal.append("OVERLAY", position_id=p.position_id, overlay=ov, on=False, reason="owner ca")
    return (f"ca {p.symbol}: book quantity {p.held_qty} -> {new_qty} (owner)"
            + (f", ISIN {isin} -> {new_isin}" if new_isin else "") + "; other holds stay until released")


def holdings_rows(api: Any) -> List[Dict[str, Any]]:
    try:
        return list(api.holdings())
    except B.NetworkError as e:
        log.warning("holdings unreadable for the CA credit check: %s", e)
        return []

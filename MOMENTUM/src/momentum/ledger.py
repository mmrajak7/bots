"""Ledger = a pure replay of the journal (build spec §C10, §F; frozen design §7).

Money is integer paise everywhere in the ledger (no float drift). The ledger file is only a cache of this replay; the
16:00 watch checks replay == file (spec §C11) and any difference stops new orders.

States (December spec §F; January spec §3):
  position: PLANNED -> HELD (first fill) ; PLANNED -> gone when the leg ends SKIPPED/EXPIRED with no fill ;
            HELD -> CLOSED when every share is sold
  leg:      BUY_GATED -> PENDING_BUY (BUY_AMOUNT at the gate) | EXPIRED ;
            PENDING_BUY -> NONE (filled) | SKIPPED | EXPIRED ;  NONE -> SELLING (plan) ; SELLING -> CLOSED (sold out)
  overlays: VET_DEFERRED, OWNER_HOLD, AUDIT_HOLD, SHARED_NAME_HOLD, PRICE_FROZEN
  intent:   INTENT_WRITTEN -> SENT -> ACK -> OPEN -> PARTIAL -> COMPLETE | CANCELLED ; TIMEOUT -> FOUND | ABSENT ;
            REJECTED_{BAND,CLOSED,RMS,OTHER}
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Set

LEG_STATES = frozenset({"BUY_GATED", "PENDING_BUY", "NONE", "SKIPPED", "EXPIRED", "SELLING", "CLOSED"})
OVERLAYS = frozenset({"VET_DEFERRED", "OWNER_HOLD", "AUDIT_HOLD", "SHARED_NAME_HOLD", "PRICE_FROZEN"})
LEG_MOVES = {
    "BUY_GATED": {"EXPIRED"},                   # -> PENDING_BUY only through BUY_AMOUNT (the gate sets the amount)
    "PENDING_BUY": {"NONE", "SKIPPED", "EXPIRED"},
    "SELLING": {"CLOSED"},                      # normally by code when held_qty reaches 0 (a sell FILL)
}
SELL_REASONS = frozenset({"RANK", "NO_SCORE", "NO_CHAIN", "TEST"})
TERMINAL_INTENT = frozenset({"COMPLETE", "CANCELLED", "ABSENT", "REJECTED_BAND", "REJECTED_CLOSED", "REJECTED_RMS",
                             "REJECTED_OTHER", "REJECTED_AUTH", "REJECTED"})
LIVE_INTENT = frozenset({"INTENT_WRITTEN", "SENT", "ACK", "OPEN", "PARTIAL", "TIMEOUT", "FOUND"})


class LedgerError(Exception):
    pass


def paise(rupees: Any) -> int:
    """Rupees (int / float / str) -> integer paise, rounded half-up on the third decimal."""
    from decimal import ROUND_HALF_UP, Decimal
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass
class Intent:
    intent_id: str
    position_id: str
    tag: str
    isin: str
    tradingsymbol: str
    side: str
    qty: int
    limit_paise: int
    status: str = "INTENT_WRITTEN"
    order_id: Optional[str] = None
    filled_qty: int = 0
    date: str = ""
    modifies: int = 0
    cancel_requested: bool = False


@dataclass
class Position:
    position_id: str
    plan_id: Dict[str, Any]
    isin: str
    symbol: str
    series: str
    rank: int
    earmark_paise: int
    leg: str = "PENDING_BUY"
    overlays: Set[str] = field(default_factory=set)
    filled_qty: int = 0
    filled_paise: int = 0
    charges_paise: int = 0
    tries_used: int = 0
    defers_in_row: int = 0
    sessions_seen: List[str] = field(default_factory=list)
    sold_qty: int = 0
    sold_paise: int = 0
    sell_charges_paise: int = 0
    ca_qty_delta: int = 0                      # shares credited by splits / bonuses (CA_CREDIT)
    sell_reason: Optional[str] = None
    sell_no: int = 0                            # the sell's number inside its plan (tag ordinal, unique per plan)
    sell_plan: Optional[Dict[str, Any]] = None  # plan_id of the plan that ordered the sell
    sell_sessions: List[str] = field(default_factory=list)
    test: bool = False                          # TEST lot (D51): outside book cash and NAV

    @property
    def held_qty(self) -> int:
        return self.filled_qty + self.ca_qty_delta - self.sold_qty

    @property
    def state(self) -> str:
        if self.held_qty > 0:
            return "HELD"
        if self.filled_qty > 0:
            return "CLOSED"
        return "PLANNED" if self.leg in ("PENDING_BUY", "BUY_GATED") else "GONE"

    @property
    def remaining_paise(self) -> int:
        return max(0, self.earmark_paise - self.filled_paise)


@dataclass
class Ledger:
    opened: bool = False
    cash_open_paise: int = 0
    units: int = 0
    cash_paise: int = 0
    test_cash_paise: int = 0                    # TEST lot money (D51): never part of book cash
    positions: Dict[str, Position] = field(default_factory=dict)
    intents: Dict[str, Intent] = field(default_factory=dict)
    plans: Dict[str, Dict[str, Any]] = field(default_factory=dict)          # plan key -> PLAN_FINAL record
    trade_ids: Set[str] = field(default_factory=set)
    unresolved: List[str] = field(default_factory=list)                     # intents needing the owner

    def open_intents(self, position_id: Optional[str] = None) -> List[Intent]:
        return [i for i in self.intents.values() if i.status in LIVE_INTENT
                and (position_id is None or i.position_id == position_id)]

    def earmarked_paise(self) -> int:
        """Cash promised to buy legs still pending (part of book cash, not free)."""
        return sum(p.remaining_paise for p in self.positions.values() if p.leg == "PENDING_BUY")

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["trade_ids"] = sorted(self.trade_ids)
        for p in d["positions"].values():
            p["overlays"] = sorted(p["overlays"])
        return d


def plan_key(plan_id: Dict[str, Any]) -> str:
    return f"{plan_id['month']}|{plan_id['signal_day']}|r{plan_id['data_rev']}"


def replay(recs: List[Dict[str, Any]]) -> Ledger:
    """Rebuild the ledger from live (non-DRY) journal records. Raises on an impossible transition."""
    lg = Ledger()
    for r in recs:
        t = r["type"]
        if t.startswith("DRY_"):
            continue
        if t == "BOOK_OPEN":
            if lg.opened:
                raise LedgerError(f"seq {r['seq']}: second BOOK_OPEN")
            lg.opened, lg.cash_open_paise, lg.units = True, int(r["cash_paise"]), int(r["units"])
            lg.cash_paise = lg.cash_open_paise
        elif t == "PLAN_FINAL":
            k = plan_key(r["plan_id"])
            if k in lg.plans:
                raise LedgerError(f"seq {r['seq']}: PLAN_FINAL {k} twice")
            lg.plans[k] = r
            test = r["plan_id"]["month"] == "TEST"
            for b in r["buys"]:
                if b["position_id"] in lg.positions:
                    raise LedgerError(f"seq {r['seq']}: position {b['position_id']} already exists")
                leg = b.get("leg", "PENDING_BUY")
                if leg not in ("PENDING_BUY", "BUY_GATED"):
                    raise LedgerError(f"seq {r['seq']}: new buy {b['position_id']} with leg {leg}")
                lg.positions[b["position_id"]] = Position(
                    position_id=b["position_id"], plan_id=r["plan_id"], isin=b["isin"], symbol=b["symbol"],
                    series=b["series"], rank=int(b["rank"]), earmark_paise=int(b["earmark_paise"]), leg=leg,
                    test=test)
            for x in r.get("sells", []):
                p = _pos(lg, {"seq": r["seq"], "position_id": x["position_id"]})
                if p.leg != "NONE" or p.held_qty <= 0 or x["reason"] not in SELL_REASONS:
                    raise LedgerError(f"seq {r['seq']}: sell of {p.position_id} (leg {p.leg}, held {p.held_qty}, "
                                      f"reason {x['reason']})")
                p.leg, p.sell_reason, p.sell_no, p.sell_plan = "SELLING", x["reason"], int(x["sell_no"]), r["plan_id"]
            for h in r.get("holds", []):
                lg.positions[h["position_id"]].overlays.add(h["overlay"])
        elif t == "LEG":
            p = _pos(lg, r)
            if r["from"] != p.leg or r["to"] not in LEG_MOVES.get(p.leg, set()):
                raise LedgerError(f"seq {r['seq']}: leg {p.position_id} {r['from']}->{r['to']} invalid (is {p.leg})")
            p.leg = r["to"]
        elif t == "BUY_AMOUNT":
            p = _pos(lg, r)
            if p.leg != "BUY_GATED" or int(r["earmark_paise"]) < 0:
                raise LedgerError(f"seq {r['seq']}: BUY_AMOUNT for {p.position_id} in leg {p.leg}")
            p.earmark_paise, p.leg = int(r["earmark_paise"]), "PENDING_BUY"
        elif t == "CA_CREDIT":
            p = _pos(lg, r)
            if int(r["qty_before"]) != p.held_qty or int(r["qty_after"]) <= 0:
                raise LedgerError(f"seq {r['seq']}: CA_CREDIT {p.position_id} before {r['qty_before']} != held "
                                  f"{p.held_qty}")
            p.ca_qty_delta += int(r["qty_after"]) - int(r["qty_before"])
        elif t == "SELL_SESSION":
            p = _pos(lg, r)
            if r["date"] in p.sell_sessions:
                raise LedgerError(f"seq {r['seq']}: sell session {r['date']} counted twice for {p.position_id}")
            p.sell_sessions.append(r["date"])
        elif t == "OVERLAY":
            p = _pos(lg, r)
            if r["overlay"] not in OVERLAYS:
                raise LedgerError(f"seq {r['seq']}: unknown overlay {r['overlay']}")
            (p.overlays.add if r["on"] else p.overlays.discard)(r["overlay"])
            # defers_in_row is NOT reset when VET_DEFERRED is lifted for the next session: only a session with an
            # order (or an owner release) ends the streak, so 2 vet deferrals in a row reach OWNER_HOLD (spec §H).
            if r["overlay"] in ("OWNER_HOLD",) and not r["on"]:
                p.defers_in_row = 0
        elif t == "SESSION_USED":
            p = _pos(lg, r)
            if r["date"] in p.sessions_seen:
                raise LedgerError(f"seq {r['seq']}: session {r['date']} counted twice for {p.position_id}")
            p.sessions_seen.append(r["date"])
            if r["lock_try"]:
                p.tries_used += 1
            if r.get("deferred_by_vet"):
                p.defers_in_row += 1
            elif r["lock_try"] or r.get("reset_defer"):
                p.defers_in_row = 0
        elif t == "INTENT":
            if r["intent_id"] in lg.intents:
                raise LedgerError(f"seq {r['seq']}: intent {r['intent_id']} written twice")
            lg.intents[r["intent_id"]] = Intent(
                intent_id=r["intent_id"], position_id=r["key"]["position_id"], tag=r["tag"], isin=r["isin"],
                tradingsymbol=r["tradingsymbol"], side=r["side"], qty=int(r["qty"]), limit_paise=int(r["limit_paise"]),
                date=r["ts"][:10])
        elif t == "SENT":
            i = _int(lg, r)
            if i.status != "INTENT_WRITTEN":
                raise LedgerError(f"seq {r['seq']}: SENT for {i.intent_id} in {i.status}")
            i.status = "SENT"
        elif t == "RESULT":
            i = _int(lg, r)
            out = r["outcome"]
            if out == "ACK":
                i.status, i.order_id = "ACK", r["order_id"]
            elif out == "TIMEOUT":
                i.status = "TIMEOUT"
            elif out.startswith("REJECTED"):
                i.status = out
            else:
                raise LedgerError(f"seq {r['seq']}: unknown outcome {out}")
        elif t == "ORPHAN":
            i = _int(lg, r)
            if r["resolution"] == "ABSENT":
                i.status = "ABSENT"
            elif r["resolution"] == "FOUND":
                i.status, i.order_id = "FOUND", r["order_id"]
            elif r["resolution"] == "OWNER":
                if i.intent_id not in lg.unresolved:
                    lg.unresolved.append(i.intent_id)
            elif r["resolution"] == "CLOSED":          # owner entered the fills from the contract note
                i.status = "CANCELLED"
            else:
                raise LedgerError(f"seq {r['seq']}: bad orphan resolution {r['resolution']}")
            if r["resolution"] in ("ABSENT", "FOUND", "CLOSED") and i.intent_id in lg.unresolved:
                lg.unresolved.remove(i.intent_id)
        elif t == "MODIFY_RESULT":
            i = _int(lg, r)
            if r["outcome"] == "OK":
                i.limit_paise, i.modifies = int(r["new_limit_paise"]), i.modifies + 1
        elif t == "CANCEL_INTENT":
            _int(lg, r).cancel_requested = True
        elif t == "ORDER_STATUS":
            i = _int(lg, r)
            st = r["status"]
            if st in ("OPEN", "PARTIAL", "COMPLETE", "CANCELLED") or st.startswith("REJECTED"):
                i.status = st
                i.filled_qty = int(r["filled_qty"])
            else:
                raise LedgerError(f"seq {r['seq']}: unknown order status {st}")
        elif t == "FILL":
            if r["trade_id"] in lg.trade_ids:
                raise LedgerError(f"seq {r['seq']}: trade {r['trade_id']} counted twice")
            lg.trade_ids.add(r["trade_id"])
            p = _pos(lg, r)
            q, ch = int(r["qty"]), int(r["charges_paise"])
            value = q * int(r["price_paise"])
            side = r.get("side", "BUY")              # December FILL records carry no side: they are buys
            if side == "SELL":
                if q > p.held_qty:
                    raise LedgerError(f"seq {r['seq']}: sell fill {q} > held {p.held_qty} on {p.position_id}")
                p.sold_qty += q
                p.sold_paise += value
                p.sell_charges_paise += ch
                delta = value - ch
                if p.held_qty == 0 and p.leg == "SELLING":
                    p.leg = "CLOSED"
            elif side == "BUY":
                p.filled_qty += q
                p.filled_paise += value
                p.charges_paise += ch
                delta = -(value + ch)
            else:
                raise LedgerError(f"seq {r['seq']}: bad fill side {side}")
            if p.test:
                lg.test_cash_paise += delta
            else:
                lg.cash_paise += delta
    for p in lg.positions.values():
        if p.leg not in LEG_STATES:
            raise LedgerError(f"position {p.position_id}: bad leg {p.leg}")
    return lg


def _pos(lg: Ledger, r: Dict[str, Any]) -> Position:
    try:
        return lg.positions[r["position_id"]]
    except KeyError as e:
        raise LedgerError(f"seq {r['seq']}: unknown position {r.get('position_id')}") from e


def _int(lg: Ledger, r: Dict[str, Any]) -> Intent:
    try:
        return lg.intents[r["intent_id"]]
    except KeyError as e:
        raise LedgerError(f"seq {r['seq']}: unknown intent {r.get('intent_id')}") from e

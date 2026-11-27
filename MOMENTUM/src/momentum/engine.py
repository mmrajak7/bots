"""Buy engine for the December slice (build spec §C9, §C10, §C11; frozen design §6.1, §6.3, §6.5, §6.6, §7).

One afternoon process per order session, under the single order lock:
  stage PLAN    14:45  ingest owner taps, snapshot quotes + facts, step plan, Tier A vet, apply the verdict
  stage START          "starting" message must be delivered in automatic mode (D36)
  stage PLACE   15:10  per intent in rank order; first-order probe; nothing placed after last_place_at (15:15)
  stage LADDER  15:15 / 15:20 / 15:25  MODIFY the same order id, quantity unchanged, capped at ltp_1510 x 1.010
  stage CANCEL  15:28  cancel unfilled remainders
  stage RECON   15:35  order book + trades -> ORDER_STATUS / FILL; lock tries; leg transitions
Every stage writes a STAGE record when done; a re-run (cron --resume, reboot) skips done stages, never places a new
order after 15:15, and never repeats an order (broker.py guard). Money is integer paise.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
import shutil
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import broker as B
from . import control as C
from . import ledger as L
from . import util
from . import vet as V
from .journal import Journal
from .telegram import new_approval_id

log = logging.getLogger("momentum.engine")


@dataclass
class Env:
    cfg: Dict[str, Any]
    paths: Any
    journal: Journal
    api: Any                                    # broker.Api (KiteApi or FakeKite)
    broker: B.Broker
    notify: Callable[..., bool]                 # notify(text, buttons=None) -> delivered?
    vet_fn: Callable[..., V.Verdict]            # vet_fn(step, vet_input, code_confirms_skip) -> Verdict
    sleep_until: Callable[[dt.datetime], None]  # blocks until the time (tests: no-op that moves the clock)
    arm: Optional[C.Arm] = None
    dry: bool = False                           # dry run: price and record every order, send none
    fo_bhav_fn: Optional[Callable[[str], Set[str]]] = None   # NSE F&O bhavcopy of a day -> stock futures
    fo_classify_fn: Optional[Callable[..., Any]] = None      # F&O vet CLI call (tests inject; None = the real CLI)
    fo_scan_fn: Optional[Callable[[], Dict[str, Any]]] = None  # circular scan (tests inject; None = NSE)
    alerts: List[str] = field(default_factory=list)

    def alert(self, code: str, text: str) -> None:
        self.journal.append("ALERT", code=code, text=text)
        self.alerts.append(f"{code}: {text}")
        self.notify(f"ALERT {code}: {text}")


# ---------------------------------------------------------------- helpers
def tick_down(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_FLOOR) * tick


def dec(x: Any) -> Decimal:
    return Decimal(str(x))


def estimate_buy_charges_paise(cfg: Dict[str, Any], value_paise: int) -> int:
    r = cfg["charges_rates"]
    v = Decimal(value_paise)
    stt = v * dec(r["stt_buy"])
    txn = v * dec(r["exchange_txn"])
    sebi = v * dec(r["sebi"])
    stamp = v * dec(r["stamp_buy"])
    gst = (dec(r["brokerage"]) * 100 + txn + sebi) * dec(r["gst_on"])
    return int((stt + txn + sebi + stamp + gst).to_integral_value())


def session_used(env: "Env", position_id: str, day: str, lock_try: bool, reason: str, **extra: Any) -> None:
    """One SESSION_USED per (position, day): a re-run after a crash must not count a session twice."""
    p = L.replay(env.journal.read()).positions[position_id]
    if day in p.sessions_seen:
        return
    env.journal.append("SESSION_USED", position_id=position_id, date=day, lock_try=lock_try, reason=reason, **extra)


def killed_now(env: "Env") -> bool:
    """KILL (file, or the owner's Telegram `kill`): cancel the book's open orders by tag at once."""
    if not C.killed(env.paths.kill_file):
        return False
    if not any(r["type"] == "NOTE" and r.get("kill_handled") == util.today().isoformat() for r in env.journal.read()):
        try:
            n = kill_cancel(env)
        except B.NetworkError as e:          # not marked handled: the next check (ladder slot, 15:28) tries again
            env.alert("KILL", f"KILL seen but the order book is unreadable ({str(e)[:120]}); retrying; no new orders")
            return True
        env.journal.append("NOTE", kill_handled=util.today().isoformat(), cancelled=n)
        env.alert("KILL", f"KILL seen: {n} open book orders cancelled; no new orders")
    return True


def close_leg(env: "Env", position_id: str, reason: str) -> str:
    """End a pending buy leg: NONE when shares were bought (the position is HELD at the filled quantity, spec §C9),
    SKIPPED when nothing was bought (the earmark returns to book cash). -> the new leg state."""
    p = L.replay(env.journal.read()).positions[position_id]
    to = "NONE" if p.filled_qty > 0 else "SKIPPED"
    env.journal.append("LEG", position_id=position_id, reason=reason, **{"from": "PENDING_BUY", "to": to})
    return to


_QUOTE_OK = re.compile(r"[^A-Za-z0-9 .,:/%()&+-]")
QUOTE_MAX = 120                 # characters of outside free text shown to the vet per row


def quote_text(s: Any) -> str:
    """Outside free text (NSE corporate-action subjects) as inert quoted data for the vet: a short, plain character
    set, no line breaks, braces or quotes, so it cannot pose as JSON structure or as an instruction block."""
    return _QUOTE_OK.sub(" ", str(s))[:QUOTE_MAX].strip()


def stage_done(recs: List[Dict[str, Any]], day: str, stage: str) -> bool:
    return any(r["type"] == "STAGE" and r.get("date") == day and r.get("stage") == stage for r in recs)


def tradingsymbol(symbol: str, series: str) -> str:
    return symbol if series == "EQ" else f"{symbol}-{series}"


def current_plan(lg: L.Ledger) -> Optional[Dict[str, Any]]:
    return max(lg.plans.values(), key=lambda r: r["seq"]) if lg.plans else None


def eligible_legs(lg: L.Ledger) -> List[L.Position]:
    """Pending buy legs with no hold overlay and room left, in rank order."""
    out = [p for p in lg.positions.values() if p.leg == "PENDING_BUY" and not p.overlays and p.remaining_paise > 0]
    return sorted(out, key=lambda p: p.rank)


def ordinal(position_id: str) -> int:
    return int(position_id.split("-")[1])


def intent_id(plan_id: Dict[str, Any], position_id: str, attempt: int) -> str:
    return f"{plan_id['month']}|{plan_id['signal_day']}|r{plan_id['data_rev']}|{position_id}|buy|{attempt}"


def attempts_so_far(lg: L.Ledger, position_id: str) -> int:
    return sum(1 for i in lg.intents.values() if i.position_id == position_id)


# ---------------------------------------------------------------- owner inbox
def ingest_inbox(env: Env) -> None:
    """Apply owner taps / commands written by the poller (one-shot per approval id). A file that cannot be applied
    is reported and moved aside: one bad file must never stop every later order step."""
    inbox = env.paths.inbox
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    for f in sorted(inbox.glob("*.json")):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(item, dict):
                raise ValueError("not an object")
            ingest_item(env, f.name, item)
        except (ValueError, KeyError, TypeError, AttributeError, L.LedgerError) as e:
            env.alert("INBOX_BAD", f"inbox file {f.name} not applied ({type(e).__name__}: {str(e)[:120]}); moved "
                      "to inbox/done, send the command again")
        shutil.move(str(f), str(done / f.name))


def ingest_item(env: Env, name: str, item: Dict[str, Any]) -> None:
    recs = env.journal.read()
    if item.get("kind") == "tap":
        aid = item["approval_id"]
        opened = {r["approval_id"]: r for r in recs if r["type"] == "APPROVAL_OPEN"}
        ap = opened.get(aid)
        if ap is None:
            env.notify(f"tap {aid}: unknown approval (ignored)")
        elif any(r["type"] == "OWNER_TAP" and r["approval_id"] == aid for r in recs):
            env.notify(f"tap {aid}: already done")
        elif item["choice"] not in ap["choices"]:
            env.notify(f"tap {aid}: choice {item['choice']} not offered")
        else:
            env.journal.append("OWNER_TAP", inbox_file=name, approval_id=aid, ref_type=ap["ref_type"],
                               ref=ap["ref"], choice=item["choice"])
            apply_tap(env, ap, item["choice"])
    elif item.get("kind") == "cmd":
        env.journal.append("OWNER_CMD", inbox_file=name, cmd=str(item["cmd"]), args=str(item.get("args", "")))
        apply_cmd(env, str(item["cmd"]), str(item.get("args", "")))
    else:
        raise ValueError(f"unknown kind {item.get('kind')!r}")


def apply_tap(env: Env, ap: Dict[str, Any], choice: str) -> None:
    lg = L.replay(env.journal.read())
    if ap["ref_type"] == "leg":
        p = lg.positions.get(ap["ref"])
        if p is None or p.leg != "PENDING_BUY":
            env.notify(f"{ap['ref']}: no pending buy (tap {choice} has no effect)")
            return
        if choice == "buy":
            for ov in sorted(p.overlays & {"OWNER_HOLD", "SHARED_NAME_HOLD", "VET_DEFERRED"}):
                env.journal.append("OVERLAY", position_id=p.position_id, overlay=ov, on=False, reason="owner buy")
            env.notify(f"{p.symbol}: released, bought from the next 14:45 step plan")
        elif choice == "skip":
            to = close_leg(env, p.position_id, "owner skip")
            env.notify(f"{p.symbol}: {'closed at the filled quantity' if to == 'NONE' else 'skipped'}; the unused "
                       "cash stays in the book")
    elif ap["ref_type"] == "flag":
        pid = ap.get("position_id")
        p = lg.positions.get(pid) if pid else None
        if choice == "real" and p is not None:
            recs = env.journal.read()
            aid_of = {r["ref"]: r["approval_id"] for r in recs if r["type"] == "APPROVAL_OPEN"
                      and r["ref_type"] == "flag"}
            real = {r["approval_id"] for r in recs if r["type"] == "OWNER_TAP" and r["choice"] == "real"}
            open_flags = [f for f in ap.get("other_open_flags", []) if aid_of.get(f) not in real]
            if open_flags:
                env.notify(f"flag {ap['ref']} cleared; {p.symbol} stays held until {open_flags} are cleared too")
            elif "AUDIT_HOLD" in p.overlays:
                env.journal.append("OVERLAY", position_id=pid, overlay="AUDIT_HOLD", on=False, reason="owner real")
                if "PRICE_FROZEN" in p.overlays:
                    env.journal.append("OVERLAY", position_id=pid, overlay="PRICE_FROZEN", on=False,
                                       reason="owner real")
            env.notify(f"flag {ap['ref']}: cleared as a real move (applies from the next 14:45 step plan)")
        elif choice == "hold":
            env.notify(f"flag {ap['ref']}: the name stays held (AUDIT_HOLD)")
        elif choice == "data_error":
            for q in lg.positions.values():
                if q.leg == "PENDING_BUY" and "OWNER_HOLD" not in q.overlays:
                    env.journal.append("OVERLAY", position_id=q.position_id, overlay="OWNER_HOLD", on=True,
                                       reason=f"owner: data error on {ap['ref']}")
            env.alert("DATA_ERROR", f"owner marked {ap['ref']} a data error: every pending buy is held; fix the "
                      "data at the PC, then release with 'release <isin>' or a fresh plan")
    elif ap["ref_type"] == "plan":
        env.notify(f"plan {ap['ref']}: {choice} recorded (the evening job finalises an approved plan)")


def apply_cmd(env: Env, cmd: str, args: str) -> None:
    lg = L.replay(env.journal.read())
    if cmd in ("hold", "release"):
        hits = [p for p in lg.positions.values() if p.isin == args.strip().upper() and p.leg == "PENDING_BUY"]
        if not hits:
            env.notify(f"{cmd} {args}: no pending book leg with that ISIN")
            return
        for p in hits:
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="OWNER_HOLD", on=(cmd == "hold"),
                               reason=f"owner {cmd}")
        env.notify(f"{cmd} {args}: done")
    elif cmd == "resolve":
        parts = args.split()
        if len(parts) != 2 or parts[1] not in ("found", "absent"):
            env.notify("usage: resolve <intent_id> found|absent")
            return
        i = lg.intents.get(parts[0])
        if i is None or i.intent_id not in lg.unresolved:
            env.notify(f"resolve: {parts[0]} is not an unresolved intent")
            return
        if parts[1] == "found" and i.date != util.today().isoformat():
            env.notify("an order from an earlier day cannot be read back: use 'filled <intent_id> <qty> <avg_price>'")
            return
        env.journal.append("ORPHAN", intent_id=i.intent_id, resolution=parts[1].upper(), order_id=None,
                           reads=["owner"])
        env.notify(f"{i.intent_id}: resolved {parts[1]}")
    elif cmd == "filled":
        parts = args.split()
        try:
            iid, qty, px = parts[0], int(parts[1]), Decimal(parts[2])
            if qty < 0 or px <= 0:
                raise ValueError
        except (IndexError, ValueError, ArithmeticError):
            env.notify("usage: filled <intent_id> <qty> <avg_price>")
            return
        i = lg.intents.get(iid)
        if i is None or iid not in lg.unresolved:
            env.notify(f"filled: {iid} is not an unresolved intent")
            return
        if qty > i.qty:
            env.notify(f"filled: {qty} > the order quantity {i.qty}")
            return
        if qty:
            pp = L.paise(px)
            env.journal.append("FILL", position_id=i.position_id, order_id=i.order_id or "", trade_id=f"owner:{iid}",
                               qty=qty, price_paise=pp, exch_ts="owner", charges_basis="estimate",
                               charges_paise=estimate_buy_charges_paise(env.cfg, qty * pp))
        env.journal.append("ORPHAN", intent_id=iid, resolution="CLOSED", order_id=i.order_id, reads=["owner filled"])
        env.notify(f"{iid}: {qty} filled at {px} recorded; the leg continues for any remainder")
    elif cmd == "status":
        env.notify(status_text(lg))
    elif cmd == "ack":
        env.notify("three-way problems acknowledged: orders may resume (the next preflight re-checks everything else)")


def open_approval(env: Env, ref_type: str, ref: str, choices: Dict[str, str], text: str, **extra: Any) -> str:
    aid = new_approval_id()
    env.journal.append("APPROVAL_OPEN", approval_id=aid, ref_type=ref_type, ref=ref, choices=sorted(choices), **extra)
    env.notify(text, buttons=[(label, f"a:{aid}:{c}") for c, label in choices.items()])
    return aid


def hold_leg(env: Env, p: L.Position, overlay: str, reason: str) -> None:
    env.journal.append("OVERLAY", position_id=p.position_id, overlay=overlay, on=True, reason=reason)
    open_approval(env, "leg", p.position_id, {"buy": "Buy", "skip": "Skip"},
                  f"HOLD {p.symbol} ({overlay}): {reason}", position_id=p.position_id)


def status_text(lg: L.Ledger) -> str:
    held = sum(1 for p in lg.positions.values() if p.state == "HELD")
    pend = sum(1 for p in lg.positions.values() if p.leg == "PENDING_BUY")
    holds = [f"{p.symbol}:{'/'.join(sorted(p.overlays))}" for p in lg.positions.values() if p.overlays]
    return (f"book cash Rs {lg.cash_paise / 100:,.0f}; held {held}; pending buys {pend}; holds {holds or 'none'}; "
            f"unresolved {lg.unresolved or 'none'}")


# ---------------------------------------------------------------- 14:45 step plan + vet
@dataclass
class StepPlan:
    step: str
    plan_id: Dict[str, Any]
    legs: List[L.Position]
    attempts: Dict[str, int]
    step_plan_hash: str
    facts_hash: str
    quotes: Dict[str, Dict[str, Any]]
    vet_input: Dict[str, Any]


def decision_buys(env: Env, plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """position_id -> the decision file's buy row (signal close, CA rows ahead). Hash-checked against PLAN_FINAL."""
    p = env.paths.decisions / plan["decision_file"]
    text = p.read_text(encoding="utf-8")
    if util.sha256_text(text) != plan["decision_sha256"]:
        raise RuntimeError(f"decision file {p.name} changed since PLAN_FINAL")
    return {b["position_id"]: b for b in json.loads(text)["buys"]}


def build_step_plan(env: Env, lg: L.Ledger, holdings_open: Dict[str, int]) -> Optional[StepPlan]:
    plan = current_plan(lg)
    if plan is None:
        return None
    day = util.today().isoformat()
    dbuy = decision_buys(env, plan)
    for p in eligible_legs(lg):            # OQ13: no buy on the name's CA ex-date (code rule; one session, no try)
        if any(r["ex_date"] == day for r in dbuy.get(p.position_id, {}).get("ca_rows", [])):
            session_used(env, p.position_id, day, False, "CA ex-date today")
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="VET_DEFERRED", on=True,
                               reason="CA ex-date today")
            env.notify(f"DEFER {p.symbol}: corporate action ex-date today; next session")
    lg = L.replay(env.journal.read())
    legs = eligible_legs(lg)
    if not legs:
        return None
    pid = plan["plan_id"]
    step = "BUY_1510" if not any(p.sessions_seen for p in legs) else "RETRY_BUY"
    attempts = {p.position_id: attempts_so_far(lg, p.position_id) + 1 for p in legs}
    intents = [{"intent_id": intent_id(pid, p.position_id, attempts[p.position_id]), "isin": p.isin, "leg": "buy",
                "max_qty": None, "max_rupees": p.remaining_paise // 100,
                "price_rule": "BUY_1510(1.005,1.010)"} for p in legs]
    sp_obj = {"plan_id": pid, "step": step, "intents": sorted(intents, key=lambda x: x["intent_id"])}
    sph = util.sha256_obj(sp_obj)
    keys = [f"NSE:{tradingsymbol(p.symbol, p.series)}" for p in legs]
    q = env.api.quote(keys)
    book_isins = {p.isin for p in lg.positions.values()}
    open_orders = [o for o in env.api.orders() if B.TAG_RE.match(str(o.get("tag") or "")) and B.order_is_live(o)]
    facts = {"isins": {}}
    for p in legs:
        facts["isins"][p.isin] = {
            "holdings_qty": int(holdings_open.get(p.isin, 0)), "series": p.series,
            "overlay": sorted(p.overlays),
            "open_book_orders": sorted([{"tag": o["tag"], "status": str(o["status"]),
                                         "pending_qty": int(o.get("pending_quantity") or 0)}
                                        for o in open_orders if o.get("tradingsymbol") == tradingsymbol(p.symbol,
                                                                                                        p.series)],
                                       key=lambda x: x["tag"])}
    for isin in sorted(book_isins - set(facts["isins"])):
        facts["isins"][isin] = {"holdings_qty": int(holdings_open.get(isin, 0))}
    fh = util.sha256_obj(facts)
    vin = {"schema": "mom.vet.v1", "step": step, "plan_id": pid, "today": util.today().isoformat(),
           "step_plan_hash": sph, "facts_hash": fh,
           "book": {"cash_rupees": lg.cash_paise // 100, "earmarked_rupees": lg.earmarked_paise() // 100},
           "intents": [], "freshness": {"plan_status": "FINAL", "decision_file": plan["decision_file"]},
           "nse_text": [{"isin": p.isin, "field": "ca_purpose", "ex_date": r["ex_date"],
                         "quoted": quote_text(r.get("purpose", ""))}
                        for p in legs for r in dbuy.get(p.position_id, {}).get("ca_rows", [])]}
    for p in legs:
        k = f"NSE:{tradingsymbol(p.symbol, p.series)}"
        qq = q.get(k, {})
        db = dbuy.get(p.position_id, {})
        sc = db.get("signal_close_paise")
        ltp = qq.get("last_price")
        vin["intents"].append({
            "signal_close": sc / 100 if sc else None,
            "gap_pct": round((float(ltp) / (sc / 100) - 1) * 100, 2) if sc and ltp else None,
            "ca_rows": [{"ex_date": r["ex_date"]} for r in db.get("ca_rows", [])],   # free text: nse_text only
            "intent_id": intent_id(pid, p.position_id, attempts[p.position_id]), "isin": p.isin, "symbol": p.symbol,
            "series": p.series, "rank": p.rank, "max_rupees": p.remaining_paise // 100,
            "price_rule": "BUY_1510(1.005,1.010)", "ltp_1445": qq.get("last_price"),
            "upper_circuit": qq.get("upper_circuit_limit"), "lower_circuit": qq.get("lower_circuit_limit"),
            "lock_tries_used": p.tries_used, "book_qty": p.filled_qty,
            "non_book_qty": max(0, int(holdings_open.get(p.isin, 0)) - p.filled_qty),
            "quote_missing": not qq})
    return StepPlan(step, pid, legs, attempts, sph, fh, q, vin)


def code_skip_reasons(sp: StepPlan) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {}
    for it in sp.vet_input["intents"]:
        out[it["intent_id"]] = "SKIP_NO_INSTRUMENT" if it["quote_missing"] else None
    return out


def apply_verdict(env: Env, sp: StepPlan, vd: V.Verdict, day: str) -> List[L.Position]:
    """-> the legs cleared to GO. Vet failure: no order this step, no lock try (D-L9)."""
    env.journal.append("VET", step=sp.step, step_plan_hash=sp.step_plan_hash, facts_hash=sp.facts_hash,
                       raw_sha256=vd.raw_sha256, valid=vd.valid, problem=vd.problem,
                       actions={k: list(v) for k, v in vd.actions.items()})
    if not vd.valid:
        env.alert("VET_DOWN", f"{sp.step}: {vd.problem}; no order this session (no lock try)")
        for p in sp.legs:
            session_used(env, p.position_id, day, False, "vet unavailable")
        return []
    go: List[L.Position] = []
    for p in sp.legs:
        iid = intent_id(sp.plan_id, p.position_id, sp.attempts[p.position_id])
        a, code, note = vd.actions.get(iid, ("HOLD", "HOLD_OTHER", "no verdict"))
        if a == "GO":
            go.append(p)
        elif a == "SKIP":
            session_used(env, p.position_id, day, False, code)
            close_leg(env, p.position_id, f"vet {code}")
            env.alert("VET_SKIP", f"{p.symbol}: {code} {note}")
        elif a == "DEFER":
            session_used(env, p.position_id, day, False, code, deferred_by_vet=True)
            if p.defers_in_row + 1 >= int(env.cfg["defer_max"]):
                hold_leg(env, p, "OWNER_HOLD", f"vet deferred {p.defers_in_row + 1} sessions in a row ({code})")
            else:
                env.journal.append("OVERLAY", position_id=p.position_id, overlay="VET_DEFERRED", on=True,
                                   reason=code)
                env.notify(f"DEFER {p.symbol}: {code} {note}")
        else:
            session_used(env, p.position_id, day, False, code)
            hold_leg(env, p, "OWNER_HOLD", f"vet {code}: {note}")
    return go


def clear_vet_deferrals(env: Env, lg: L.Ledger, day: str) -> None:
    """A VET_DEFERRED overlay lasts one session: lifted at the next session's step plan (re-vetted fresh). One set
    TODAY stays (a re-run of the plan stage after a crash must not re-vet a pair the vet already deferred, F29)."""
    recs = env.journal.read()
    for p in lg.positions.values():
        if "VET_DEFERRED" in p.overlays and p.leg == "PENDING_BUY":
            on = [r for r in recs if r["type"] == "OVERLAY" and r["position_id"] == p.position_id
                  and r["overlay"] == "VET_DEFERRED" and r["on"]]
            if on and on[-1]["ts"][:10] == day:
                continue
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="VET_DEFERRED", on=False,
                               reason="next session")


def shared_name_holds(env: Env, holdings_open: Dict[str, int]) -> None:
    """D38: a pending plan buy whose ISIN the account already holds beyond the book's own quantity (FIFTY's or the
    owner's shares) is held for the owner's Buy / Skip. Once the owner chose Buy it is not held again."""
    recs = env.journal.read()
    lg = L.replay(recs)
    owner_ok = {r["position_id"] for r in recs if r["type"] == "OVERLAY" and r["overlay"] == "SHARED_NAME_HOLD"
                and not r["on"] and r.get("reason") == "owner buy"}
    for p in lg.positions.values():
        extra = int(holdings_open.get(p.isin, 0)) - p.filled_qty
        if (p.leg == "PENDING_BUY" and extra > 0 and "SHARED_NAME_HOLD" not in p.overlays
                and p.position_id not in owner_ok):
            hold_leg(env, p, "SHARED_NAME_HOLD", f"account holds {extra} shares outside the book")


# ---------------------------------------------------------------- 15:10 place
def instruments_ticks(env: Env) -> Dict[str, Decimal]:
    """tradingsymbol -> tick size from today's Kite NSE instruments. Read ONCE per day (the dump is large and Kite
    asks for one download a day) at the 14:45 step plan and kept in state/snapshots, so 15:10 and the ladder do not
    wait on it. A symbol missing here is not tradable under that name today (series change, suspension, rename)."""
    f = env.paths.snapshots / f"ticks_{util.today().isoformat()}.json"
    if f.exists():
        try:
            return {k: dec(v) for k, v in json.loads(f.read_text(encoding="utf-8")).items()}
        except (ValueError, ArithmeticError) as e:
            log.warning("tick cache %s unreadable (%s): reading instruments again", f.name, e)
    raw = {str(r["tradingsymbol"]): str(r["tick_size"]) for r in env.api.instruments("NSE")
           if r.get("exchange", "NSE") == "NSE" and r.get("tick_size")}
    if raw:
        util.write_json_atomic(f, raw)
    return {k: dec(v) for k, v in raw.items()}


def book_reject(env: Env, i: L.Intent, o: Dict[str, Any]) -> str:
    """An order Kite accepted (order id returned) and the exchange / RMS then REJECTED: the usual Kite rejection
    path. The order-book reason is classified and journalled. -> REJECTED_{BAND,CLOSED,RMS,AUTH,OTHER}."""
    text = B.reject_text(o)
    status = B.classify_reject(text)
    env.journal.append("ORDER_STATUS", intent_id=i.intent_id, order_id=str(o["order_id"]), status=status,
                       filled_qty=0, pending_qty=0, avg_price_paise=0, message=text[:300])
    return status


def reject_streak(lg: L.Ledger, position_id: str, status: str) -> int:
    """How many of the position's latest attempts in a row ended with this status."""
    ints = sorted((i for i in lg.intents.values() if i.position_id == position_id),
                  key=lambda i: int(i.intent_id.rsplit("|", 1)[1]))
    n = 0
    for i in reversed(ints):
        if i.status != status:
            break
        n += 1
    return n


def place_all(env: Env, sp: StepPlan, go: List[L.Position], day: str) -> List[str]:
    """-> intent ids ACKed. Nothing after last_place_at. The first order is a probe (spec §C9): it must be accepted
    AND stand un-rejected in the order book before the others go."""
    placed: List[str] = []
    if not go:
        return placed
    cutoff = util.at_today(env.cfg["last_place_at"])
    try:
        ticks = instruments_ticks(env)
        q = env.api.quote([f"NSE:{tradingsymbol(p.symbol, p.series)}" for p in go])
    except B.NetworkError as e:
        env.alert("KITE_DOWN", f"no instruments / quotes at {env.cfg['place_at']} ({str(e)[:150]}): no buy placed "
                  "today (next session, no lock try)")
        return placed
    cap = env.arm.cap_rupees if env.arm else (int(env.cfg["arm_cap_rupees_default"]) if env.dry else 0)
    recs = env.journal.read()
    lg0 = L.replay(recs)
    # the ARM cap is per SESSION: a resumed process counts what an earlier run of today already sent
    spent = sum(int(r["max_rupees"]) for r in recs if r["type"] == "INTENT" and r["ts"][:10] == day
                and "max_rupees" in r)
    has_intent_today = {i.position_id for i in lg0.intents.values() if i.date == day}
    probe_ok = False
    for n, p in enumerate(go):
        if p.position_id in has_intent_today:      # resumed run: this leg already has today's order (E12)
            continue
        if util.now() >= cutoff:
            env.alert("PLACE_LATE", f"{len(go) - n} buys not placed: past {env.cfg['last_place_at']} (next session)")
            break
        ts = tradingsymbol(p.symbol, p.series)
        tick = ticks.get(ts)
        if tick is None or tick <= 0:
            hold_leg(env, p, "OWNER_HOLD", f"{ts} is not in today's Kite instruments (series change, suspension or "
                     "rename?)")
            continue
        qq = q.get(f"NSE:{ts}")
        if not qq or not qq.get("last_price"):
            env.alert("NO_QUOTE", f"{p.symbol}: no quote at 15:10 (next session)")
            continue
        if not qq.get("upper_circuit_limit"):
            env.alert("NO_CIRCUIT", f"{p.symbol}: the quote has no upper circuit limit: not placed (the limit "
                      "cannot be clamped), next session")
            continue
        ltp = dec(qq["last_price"])
        upper = dec(qq["upper_circuit_limit"])
        limit = tick_down(min(ltp * dec(env.cfg["buy_ltp_mult"]), upper), tick)
        cap_price = tick_down(min(ltp * dec(env.cfg["buy_cap_mult"]), upper), tick)
        max_rupees = p.remaining_paise // 100
        qty = int((Decimal(max_rupees) / (ltp * dec(env.cfg["buy_cap_mult"]))).to_integral_value(rounding=ROUND_FLOOR))
        if qty <= 0 or limit <= 0:
            to = close_leg(env, p.position_id, "qty 0")
            env.alert("QTY0", f"{p.symbol}: LTP {ltp} > Rs {max_rupees}/{env.cfg['buy_cap_mult']}: "
                      + ("skipped" if to == "SKIPPED" else f"closed at the {p.filled_qty} shares held")
                      + ", cash stays (D-L11)")
            continue
        if spent + max_rupees > cap:
            env.alert("ARM_CAP", f"{p.symbol}: session cap Rs {cap} reached (next session)")
            continue
        att = sp.attempts[p.position_id]
        if att > 9:
            hold_leg(env, p, "OWNER_HOLD", "attempt > 9")
            continue
        iid = intent_id(sp.plan_id, p.position_id, att)
        try:
            it = env.broker.place_buy(
                intent_id=iid, key={"plan_id": sp.plan_id, "position_id": p.position_id, "leg": "buy",
                                    "attempt": att},
                tag=B.make_tag(sp.plan_id["month"][2:4] + sp.plan_id["month"][5:7], "buy", ordinal(p.position_id),
                               att),
                isin=p.isin, tradingsymbol=ts, qty=qty, limit=float(limit), ltp_1510=float(ltp),
                cap_price=float(cap_price), max_rupees=max_rupees)
        except B.BrokerRefused as e:
            if env.dry and str(e) == "DRY_MODE":
                env.journal.append("NOTE", dry_order={"intent_id": iid, "symbol": ts, "qty": qty, "limit": str(limit),
                                                      "ltp": str(ltp), "cap_price": str(cap_price),
                                                      "max_rupees": max_rupees})
                spent += max_rupees
                probe_ok = True
                continue
            env.alert("REFUSED", f"{p.symbol}: {e}")
            if not probe_ok or str(e) in ("KILL", "NOT_ARMED"):
                break
            continue
        except B.NetworkError as e:                # the order-book read BEFORE the order failed: nothing was sent
            env.alert("KITE_DOWN", f"{p.symbol}: order book unreadable before the order ({str(e)[:150]}); the "
                      "remaining buys wait for the next session")
            break
        spent += max_rupees
        status = it.status
        if status != "ACK":
            after_reject(env, p, status, it.intent_id)
        if not probe_ok:
            in_book = probe_row(env, it.tag) if status == "ACK" else None
            state = B.order_state(in_book) if in_book is not None else "not in the order book"
            if status == "ACK" and state == "REJECTED" and in_book is not None:
                status = book_reject(env, it, in_book)
                after_reject(env, p, status, it.intent_id)
            if status != "ACK" or state not in PROBE_GOOD_STATES:
                env.alert("PROBE_FAILED", f"first order {it.tag}: reply {status}, order book: {state}; the other "
                          "buys wait for the next session")
                break
            probe_ok = True
        if status == "ACK":
            placed.append(iid)
    return placed


PROBE_GOOD_STATES = ("OPEN", "COMPLETE")     # the exchange has the order (resting or filled)


def probe_row(env: Env, tag: str) -> Optional[Dict[str, Any]]:
    """The probe order's row once it has SETTLED (OPEN / COMPLETE / REJECTED / CANCELLED). Right after the order id
    returns the row can still be in VALIDATION PENDING / PUT ORDER REQ RECEIVED and turn REJECTED a moment later,
    so it is re-read up to probe_reads times, probe_wait_s apart. Still unsettled (or unreadable) -> returned as it
    is / None, and the caller fails the probe."""
    row: Optional[Dict[str, Any]] = None
    for k in range(int(env.cfg["probe_reads"])):
        if k:
            env.sleep_until(util.now() + dt.timedelta(seconds=int(env.cfg["probe_wait_s"])))
        try:
            row = {o.get("tag"): o for o in env.api.orders()}.get(tag)
        except B.NetworkError as e:
            log.warning("probe: order book unreadable: %s", e)
            continue
        if row is not None and (B.order_state(row) in PROBE_GOOD_STATES or not B.order_is_live(row)):
            return row
    return row


def after_reject(env: Env, p: L.Position, status: str, iid: str) -> None:
    """What a failed placement means for the leg (spec §C9 rejections; frozen design §6.1):
    BAND  -> band_max in a row -> OWNER_HOLD (first setting 1 = hold at once, as before the review; the design's
             same-session corrected retry is not built, review N03)
    RMS   -> next session, no lock try; rms_max in a row -> OWNER_HOLD (E20)
    CLOSED / AUTH -> next session;  OTHER (or unknown) -> OWNER_HOLD;  TIMEOUT -> settled by tag at the reconcile."""
    n = reject_streak(L.replay(env.journal.read()), p.position_id, status)
    if status == "TIMEOUT":
        env.alert("TIMEOUT", f"{p.symbol}: no reply for {iid}; the reconcile settles it by tag")
    elif status == "REJECTED_CLOSED":
        env.alert("CLOSED", f"{p.symbol}: market closed reply; next session")
    elif status == "REJECTED_AUTH":
        env.alert("AUTH", f"{p.symbol}: Kite refused the session (token replaced?); nothing sent; next session")
    elif status == "REJECTED_RMS" and n < int(env.cfg["rms_max"]):
        env.alert("RMS", f"{p.symbol}: margin / RMS rejection ({n} in a row); next session, no lock try")
    elif status == "REJECTED_BAND" and n < int(env.cfg["band_max"]):
        env.alert("BAND", f"{p.symbol}: price-band rejection; the next session re-reads the circuit limits")
    else:
        hold_leg(env, p, "OWNER_HOLD", f"order rejected ({status}, {n} in a row)")


# ---------------------------------------------------------------- ladder + cancel
LADDER_STATES = ("ACK", "OPEN", "PARTIAL", "FOUND")


def live_orders_by_intent(env: Env, any_status: bool = False) -> Dict[str, Dict[str, Any]]:
    """Today's intents whose exact tag is in the order book. any_status=True (cancel) also takes an intent the
    journal still shows as SENT / TIMEOUT: a live order is cancelled whatever our record of its reply says."""
    lg = L.replay(env.journal.read())
    by_tag = {o.get("tag"): o for o in env.api.orders()}
    out = {}
    for i in lg.intents.values():
        if i.date == util.today().isoformat() and (any_status or i.status in LADDER_STATES):
            o = by_tag.get(i.tag)
            if o is not None:
                out[i.intent_id] = o
    return out


def ladder_step(env: Env, step_no: int) -> None:
    recs = env.journal.read()
    caps = {r["intent_id"]: r["cap_price_paise"] for r in recs if r["type"] == "INTENT" and "cap_price_paise" in r}
    lg = L.replay(recs)
    open_ = {iid: o for iid, o in live_orders_by_intent(env).items() if B.order_state(o) == "OPEN"}
    if not open_:
        return
    syms = {iid: lg.intents[iid].tradingsymbol for iid in open_}
    q = env.api.quote([f"NSE:{s}" for s in syms.values()])
    ticks = instruments_ticks(env)
    failed: List[str] = []
    for iid, o in open_.items():
        ts = syms[iid]
        qq = q.get(f"NSE:{ts}") or {}
        tick = ticks.get(ts)
        if not qq.get("last_price") or not qq.get("upper_circuit_limit") or tick is None or iid not in caps:
            continue                               # nothing safe to re-price from: the order keeps its price
        new = tick_down(min(dec(qq["last_price"]) * dec(env.cfg["buy_ltp_mult"]), Decimal(caps[iid]) / 100,
                            dec(qq["upper_circuit_limit"])), tick)
        if new <= dec(o.get("price") or 0):
            continue
        try:
            if not env.broker.modify_price(iid, str(o["order_id"]), step_no, float(new)):
                failed.append(ts)
        except B.BrokerRefused as e:
            log.warning("ladder %s: %s refused: %s", step_no, iid, e)
            failed.append(ts)
    if failed:
        env.alert("MODIFY_FAILED", f"ladder step {step_no}: re-price not done for {failed}; those orders keep "
                  "their last price")


def cancel_unfilled(env: Env) -> None:
    failed: List[str] = []
    for iid, o in live_orders_by_intent(env, any_status=True).items():
        if B.order_is_live(o) and not env.broker.cancel(iid, str(o["order_id"]),
                                                        f"end of buy window ({env.cfg['cancel_at']})"):
            failed.append(str(o.get("tag")))
    if failed:
        env.alert("CANCEL_FAILED", f"cancel not confirmed for {failed}: a DAY order ends at the close; the "
                  "reconcile reads what filled")


# ---------------------------------------------------------------- reconcile
def settle_orphans(env: Env, day: str, final: bool) -> None:
    """An intent with no usable reply: FOUND only by its exact tag in the order book (any time); at the reconcile
    (final) INTENT without SENT -> ABSENT (the call was never made) and SENT / TIMEOUT without a tag -> the owner.
    Run after placing, before each ladder slot and before the 15:28 cancel (so a crash-time order is still
    re-priced and cancelled), and again at the reconcile."""
    lg = L.replay(env.journal.read())
    by_tag = {o.get("tag"): o for o in env.api.orders() if o.get("tag")}
    for i in list(lg.intents.values()):
        if i.date != day or i.status not in ("INTENT_WRITTEN", "SENT", "TIMEOUT"):
            continue
        if i.tag in by_tag:
            env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="FOUND",
                               order_id=str(by_tag[i.tag]["order_id"]), reads=["orders"])
        elif final and i.status == "INTENT_WRITTEN":         # crash between INTENT and SENT: the call was never made
            env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="ABSENT", order_id=None, reads=["orders"])
        elif final and i.intent_id not in lg.unresolved:
            env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="OWNER", order_id=None, reads=["orders"])
            env.alert("ORPHAN", f"{i.intent_id}: no reply and no order with tag {i.tag}; reply "
                      f"'resolve {i.intent_id} found|absent'")


def settle_stale(env: Env, day: str) -> None:
    """An intent from an EARLIER day still in a live state (e.g. the 15:35 reconcile never ran): Kite's order book is
    today-only, so the fills cannot be read back. It becomes an owner item (blocks every order until resolved):
    `filled <intent_id> <qty> <avg_price>` from the contract note, or `resolve <intent_id> absent`."""
    lg = L.replay(env.journal.read())
    for i in lg.intents.values():
        if i.date < day and i.status in L.LIVE_INTENT and i.intent_id not in lg.unresolved:
            env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="OWNER", order_id=i.order_id,
                               reads=["stale: order book is today-only"])
            env.alert("STALE_INTENT", f"{i.intent_id} ({i.tradingsymbol}) from {i.date} was never reconciled. "
                      f"Check the contract note, then send 'filled {i.intent_id} <qty> <avg_price>' or "
                      f"'resolve {i.intent_id} absent'. All book orders wait until then.")


def reconcile(env: Env, day: str) -> None:
    settle_orphans(env, day, final=True)
    orders = env.api.orders()
    trades = env.api.trades()
    by_oid = {str(o["order_id"]): o for o in orders}
    lg = L.replay(env.journal.read())
    newly_rejected: List[Tuple[L.Intent, str]] = []
    for i in lg.intents.values():
        if i.date != day or not i.order_id:
            continue
        o = by_oid.get(i.order_id)
        if o is None:                              # an acknowledged order the order book does not show: never guess
            if i.status in L.LIVE_INTENT and i.intent_id not in lg.unresolved:
                env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="OWNER", order_id=i.order_id,
                                   reads=["orders: order id not in today's book"])
                env.alert("ORDER_MISSING", f"{i.intent_id}: order {i.order_id} is not in today's order book. Check "
                          f"Kite, then 'filled {i.intent_id} <qty> <avg_price>' or 'resolve {i.intent_id} absent'")
            continue
        st = B.order_state(o)
        filled = int(o.get("filled_quantity") or 0)
        if st == "REJECTED":
            mapped = i.status if i.status.startswith("REJECTED_") else B.classify_reject(B.reject_text(o))
        elif st in ("COMPLETE", "CANCELLED"):
            mapped = st
        else:                                      # still live after the cancel time
            mapped = "PARTIAL" if filled > 0 else "OPEN"
            env.alert("ORDER_STATUS", f"{i.intent_id}: order still {st} at the reconcile (cancel not confirmed?); "
                      "fills so far are recorded; check the order in Kite")
        if (mapped, filled) != (i.status, i.filled_qty):
            env.journal.append("ORDER_STATUS", intent_id=i.intent_id, order_id=i.order_id, status=mapped,
                               filled_qty=filled, pending_qty=int(o.get("pending_quantity") or 0),
                               avg_price_paise=L.paise(o.get("average_price") or 0),
                               message=B.reject_text(o)[:300] if st == "REJECTED" else "")
            if mapped.startswith("REJECTED_") and not i.status.startswith("REJECTED"):
                newly_rejected.append((i, mapped))
        # Quantity already booked for this order (real trades AND any stand-in fill from an earlier reconcile). The
        # order book's filled_quantity is the ceiling: a real trade that arrives after a stand-in was booked must not
        # be counted again.
        prior = [r for r in env.journal.read() if r["type"] == "FILL" and r.get("order_id") == i.order_id]
        booked_qty = sum(int(r["qty"]) for r in prior)
        booked_value = sum(int(r["qty"]) * int(r["price_paise"]) for r in prior)
        for t in trades:
            if str(t.get("order_id")) != i.order_id or str(t["trade_id"]) in lg.trade_ids:
                continue
            px = L.paise(t["average_price"])
            q = int(t["quantity"])
            if booked_qty + q > filled:
                env.alert("RECON_TRADES", f"{i.intent_id}: trade {t['trade_id']} ({q} @ {px / 100}) would exceed the "
                          f"order's filled {filled} (already booked {booked_qty}, incl. a stand-in?): NOT booked; "
                          "check the contract note")
                lg.trade_ids.add(str(t["trade_id"]))
                continue
            env.journal.append("FILL", position_id=i.position_id, order_id=i.order_id, trade_id=str(t["trade_id"]),
                               qty=q, price_paise=px, exch_ts=str(t.get("exchange_timestamp", "")),
                               charges_paise=estimate_buy_charges_paise(env.cfg, q * px), charges_basis="estimate")
            lg.trade_ids.add(str(t["trade_id"]))
            booked_qty += q
            booked_value += q * px
        seen_qty, seen_value = booked_qty, booked_value
        short = filled - seen_qty                  # the order book shows more filled than trades() lists (U28)
        synth_id = f"order:{i.order_id}:{filled}"
        if short > 0 and synth_id not in lg.trade_ids:
            avg = L.paise(o.get("average_price") or 0)
            px = (avg * filled - seen_value) // short if avg * filled > seen_value else avg
            if px > 0:
                env.journal.append("FILL", position_id=i.position_id, order_id=i.order_id, trade_id=synth_id,
                                   qty=short, price_paise=px, exch_ts="order book", charges_basis="estimate",
                                   charges_paise=estimate_buy_charges_paise(env.cfg, short * px))
                lg.trade_ids.add(synth_id)
            env.alert("RECON_TRADES", f"{i.intent_id}: order book filled {filled} but trades() lists {seen_qty}; "
                      + (f"{short} shares booked at the order's average price" if px > 0 else "NOT booked (no "
                         "average price): enter by hand") + "; check the contract note")
    for i, mapped in newly_rejected:
        after_reject(env, L.replay(env.journal.read()).positions[i.position_id], mapped, i.intent_id)
    # sessions + leg transitions for legs that had an order today
    lg = L.replay(env.journal.read())
    for p in lg.positions.values():
        if p.leg != "PENDING_BUY" or day in p.sessions_seen:
            continue
        todays = [i for i in lg.intents.values() if i.position_id == p.position_id and i.date == day
                  and i.status not in ("ABSENT",)]
        if not todays:
            continue
        complete = any(i.status == "COMPLETE" for i in todays)
        rejected_only = all(i.status.startswith("REJECTED") for i in todays)
        lock_try = (not complete) and not rejected_only and not any(i.status in ("TIMEOUT", "SENT") for i in todays)
        session_used(env, p.position_id, day, lock_try,
                     "filled" if complete else ("rejected" if rejected_only else "unfilled"), reset_defer=True)
        lg2 = L.replay(env.journal.read())
        p2 = lg2.positions[p.position_id]
        if complete:
            env.journal.append("LEG", position_id=p.position_id, reason="filled",
                               **{"from": "PENDING_BUY", "to": "NONE"})
        elif p2.tries_used >= 1 + int(env.cfg["lock_tries"]):
            close_leg(env, p.position_id, f"{p2.tries_used} tries used")
            env.alert("BUY_WINDOW_END", f"{p.symbol}: window over; held {p2.filled_qty} shares, "
                      f"Rs {p2.remaining_paise / 100:,.0f} back to book cash")


# ---------------------------------------------------------------- the afternoon process
# ---------------------------------------------------------------- F&O day-1 backstop (frozen design D47)
NIFTY50_TOKEN = 256265


def kite_fut_names(env: Env) -> Set[str]:
    """Underlying names with futures in today's Kite NFO dump (probe 2026-11-27: equal to the F&O bhavcopy's stock
    futures, 218/218, plus 6 index names that are never EQ symbols)."""
    return {str(i["name"]) for i in env.api.instruments("NFO")
            if i.get("instrument_type") == "FUT" and i.get("segment", "NFO-FUT") == "NFO-FUT" and i.get("name")}


def fo_day1_names(env: Env, m: str) -> Tuple[Set[str], str, bool]:
    """The research F&O membership of month m: names with stock futures on m's FIRST session. -> (names, source,
    frozen now). Frozen once per month in snapshots/: a name that gains futures later in the month is still a valid
    research buy, so a deferred buy must never be judged by a later day's list. Before m starts (a dry rehearsal)
    today's Kite list is used and nothing is frozen. Raises when no list can be had (the caller fails closed)."""
    f = env.paths.snapshots / f"fo_day1_{m}.json"
    if f.exists():
        raw = json.loads(f.read_text(encoding="utf-8"))
        return set(raw["names"]), str(raw["source"]), False
    today = util.today().isoformat()
    if today[:7] < m:
        return kite_fut_names(env), f"Kite NFO {today} (before {m}: not frozen)", False
    rows = env.api.historical(NIFTY50_TOKEN, f"{m}-01", today, "day")
    days = sorted({str(r["date"])[:10] for r in rows if str(r["date"])[:7] == m})
    first = days[0] if days and days[0] < today else today
    if first == today:
        names, src = kite_fut_names(env), f"Kite NFO instruments {today}"
    else:
        if env.fo_bhav_fn is None:
            raise RuntimeError("no F&O bhavcopy reader for a late first run")
        names, src = set(env.fo_bhav_fn(first)), f"NSE F&O bhavcopy {first} (first run after day 1)"
    if len(names) < int(env.cfg["fo_min_stock_futures"]):
        raise RuntimeError(f"{src}: only {len(names)} names")
    util.write_once(f, json.dumps({"month": m, "first_session": first, "source": src, "names": sorted(names),
                                   "made_at": util.now().isoformat(timespec="seconds")}, indent=1) + "\n")
    return names, src, True


def fo_day1_guard(env: Env) -> Optional[str]:
    """Before the step plan: a pending buy that has stock futures on m's first session is closed (research never buys
    an F&O member). -> a problem text when the list cannot be had (no orders this session, no lock try)."""
    lg = L.replay(env.journal.read())
    plan = current_plan(lg)
    pend = sorted((p for p in lg.positions.values() if p.leg == "PENDING_BUY"), key=lambda p: p.rank)
    if plan is None or not pend:
        return None
    m = plan["plan_id"]["month"]
    try:
        names, src, frozen_now = fo_day1_names(env, m)
    except Exception as e:  # noqa: BLE001 - any failure: fail closed for this session
        return f"F&O day-1 list for {m} unavailable ({type(e).__name__}: {str(e)[:120]})"
    for p in pend:
        if p.symbol in names:
            to = close_leg(env, p.position_id, f"F&O member of {m} ({src})")
            env.alert("FO_ENTRANT", f"{p.symbol} has stock futures ({src}): the research excludes F&O members of "
                      f"{m}, so it is not bought ({to}); its earmark stays in book cash")
    if frozen_now:
        try:
            dec = json.loads((env.paths.decisions / plan["decision_file"]).read_text(encoding="utf-8"))
            excluded = list((dec.get("fo_vet") or {}).get("entrants") or [])
        except (OSError, ValueError):
            excluded = []
        stale = sorted(s for s in excluded if s not in names)
        if stale:
            env.alert("FO_FALSE_EXCLUSION", f"the {m} band excluded {stale} as F&O entrants, but they have no "
                      f"futures on day 1 ({src}): the band was built on a stale circular; nothing to undo now")
        env.journal.append("NOTE", text=f"F&O day-1 list {m}: {len(names)} names from {src}")
    return None


def afternoon(env: Env, holdings_open: Dict[str, int], preflight: Callable[[], List[str]]) -> None:
    day = util.today().isoformat()
    recs = env.journal.read()
    last_place = util.at_today(env.cfg["last_place_at"])
    if not stage_done(recs, day, "PLAN") and util.now() >= last_place:
        # the first run of the day came after the last placing time (Pi was down): no plan, no vet call, no order
        env.journal.append("STAGE", date=day, stage="PLAN", result="too late", go=[], step_plan_hash=None,
                           facts_hash=None)
        env.alert("PLAN_LATE", f"the afternoon step first ran after {env.cfg['last_place_at']}: no buy today "
                  "(next session, no lock try)")
        recs = env.journal.read()
    if not stage_done(recs, day, "PLAN"):
        env.sleep_until(util.at_today(env.cfg["step_plan_at"]))
        ingest_inbox(env)
        settle_stale(env, day)
        problems = preflight()
        if killed_now(env):
            problems.append("KILL")
        if not problems:
            fp = fo_day1_guard(env)
            if fp:
                problems.append(fp)
        if problems:
            env.alert("PREFLIGHT", "; ".join(problems) + " -> no orders today")
            env.journal.append("STAGE", date=day, stage="PLAN", result="preflight failed")
            env.journal.append("STAGE", date=day, stage="PLACE", result="skipped")
            env.journal.append("STAGE", date=day, stage="CANCEL", result="skipped")
            env.journal.append("STAGE", date=day, stage="LADDER", result="skipped")
            reconcile_stage(env, day)
            return
        ingest_inbox(env)
        lg = L.replay(env.journal.read())
        clear_vet_deferrals(env, lg, day)
        shared_name_holds(env, holdings_open)
        lg = L.replay(env.journal.read())
        sp = build_step_plan(env, lg, holdings_open)
        go: List[str] = []
        if sp is not None:
            try:
                instruments_ticks(env)             # today's tick sizes, read now so 15:10 does not wait on the dump
            except B.NetworkError as e:
                log.warning("instruments not read at the step plan (%s); 15:10 reads them", e)
            vd = env.vet_fn(sp.step, sp.vet_input, code_skip_reasons(sp))
            ok = apply_verdict(env, sp, vd, day)
            go = [p.position_id for p in ok]
        env.journal.append("STAGE", date=day, stage="PLAN", result="ok", go=go,
                           step_plan_hash=sp.step_plan_hash if sp else None, facts_hash=sp.facts_hash if sp else None)
        recs = env.journal.read()
    plan_rec = [r for r in recs if r["type"] == "STAGE" and r.get("date") == day and r.get("stage") == "PLAN"][-1]
    go_ids: List[str] = plan_rec.get("go") or []

    def cleared() -> List[L.Position]:
        lg_ = L.replay(env.journal.read())
        return [lg_.positions[x] for x in go_ids if lg_.positions[x].leg == "PENDING_BUY"
                and not lg_.positions[x].overlays]
    if not stage_done(recs, day, "PLACE"):
        if util.now() >= last_place:
            env.journal.append("STAGE", date=day, stage="PLACE", result="too late (resume after 15:15)")
            if go_ids:
                env.alert("PLACE_LATE", f"the process resumed after {env.cfg['last_place_at']}: no new buy placed "
                          "today (next session)")
        else:
            legs = cleared()
            total = sum(p.remaining_paise for p in legs) // 100
            started = True
            if legs and env.arm is not None and env.arm.mode != "automatic":
                # approve-each needs an owner Approve bound to the step plan (frozen design §9): not built in the
                # December slice. Placing without it would be a silent upgrade to automatic, so nothing is placed.
                env.alert("ARM_MODE", f"ARM mode {env.arm.mode} is not built in this slice: no order placed. "
                          "Write an ARM with mode automatic to trade.")
                started = False
            elif legs and env.arm is not None:
                started = env.notify(f"starting BUY step: {len(legs)} buys, Rs {total:,}")
                env.journal.append("STEP_START", step="BUY", step_plan_hash=plan_rec.get("step_plan_hash"),
                                   telegram_ok=started)
                if not started:
                    env.journal.append("NOTE", text="starting message not delivered: automatic step did not start")
            if legs and started:
                env.sleep_until(util.at_today(env.cfg["place_at"]))
                if killed_now(env):
                    env.journal.append("STAGE", date=day, stage="PLACE", result="killed")
                    reconcile_stage(env, day)
                    return
                legs = cleared()
                total = sum(p.remaining_paise for p in legs) // 100
                # G17: the cash guard is read at 15:10, right before the first order (FIFTY trades from 15:00)
                try:
                    avail: Optional[float] = available_cash(env.api.margins())
                except B.NetworkError as e:
                    avail = None
                    env.alert("KITE_DOWN", f"margins unreadable at {env.cfg['place_at']} ({str(e)[:150]}): no buy "
                              "(next session)")
                if avail is not None and avail < total:
                    env.alert("CASH", f"Kite available Rs {avail:,.0f} < book buys Rs {total:,}: no buy (D09)")
                elif avail is not None:
                    place_all(env, rebuild_attempts(L.replay(env.journal.read()), go_ids), legs, day)
                    try:
                        settle_orphans(env, day, final=False)
                    except B.NetworkError as e:
                        log.warning("orphans not settled after placing (%s); the ladder / reconcile settle them", e)
            env.journal.append("STAGE", date=day, stage="PLACE", result="ok")
        recs = env.journal.read()
    if not stage_done(recs, day, "LADDER"):
        for k, hhmm in enumerate(env.cfg["ladder"], 1):
            t = util.at_today(hhmm)
            if util.now() > t + dt.timedelta(minutes=int(env.cfg["ladder_grace_min"])):
                continue                                   # that ladder slot has passed (resume)
            env.sleep_until(t)
            if killed_now(env):
                break
            try:
                settle_orphans(env, day, final=False)      # an order placed just before a crash is re-priced too
                ladder_step(env, k)
            except B.NetworkError as e:                    # a failed read must never cost the 15:28 cancel
                env.alert("LADDER_SKIPPED", f"{hhmm}: Kite read failed ({str(e)[:150]}); orders keep their price")
        env.journal.append("STAGE", date=day, stage="LADDER", result="ok")
        recs = env.journal.read()
    if not stage_done(recs, day, "CANCEL"):
        env.sleep_until(util.at_today(env.cfg["cancel_at"]))
        killed_now(env)
        for attempt in (1, 2):
            try:
                settle_orphans(env, day, final=False)
                cancel_unfilled(env)
                break
            except B.NetworkError as e:
                if attempt == 2:
                    env.alert("CANCEL_FAILED", f"order book unreadable at {env.cfg['cancel_at']} ({str(e)[:150]}): "
                              "open book orders NOT cancelled by code; DAY orders end at the close; check Kite")
        env.journal.append("STAGE", date=day, stage="CANCEL", result="ok")
    reconcile_stage(env, day)


def reconcile_stage(env: Env, day: str) -> None:
    recs = env.journal.read()
    if stage_done(recs, day, "RECON"):
        return
    env.sleep_until(util.at_today(env.cfg["reconcile_at"]))
    reconcile(env, day)
    env.journal.append("STAGE", date=day, stage="RECON", result="ok")
    lg = L.replay(env.journal.read())
    env.notify("BUY step done. " + status_text(lg))


def rebuild_attempts(lg: L.Ledger, go_ids: List[str]) -> StepPlan:
    plan = current_plan(lg)
    assert plan is not None
    att = {x: attempts_so_far(lg, x) + 1 for x in go_ids}
    return StepPlan("BUY", plan["plan_id"], [lg.positions[x] for x in go_ids], att, "", "", {}, {})


def available_cash(m: Dict[str, Any]) -> float:
    """Spec L1 N1: the smaller of equity.available.live_balance and equity.net (UNVERIFIED which field is right)."""
    eq = m.get("equity", {})
    vals = [eq.get("net"), eq.get("available", {}).get("live_balance")]
    vals = [float(v) for v in vals if v is not None]
    return min(vals) if vals else 0.0


def kill_cancel(env: Env) -> int:
    """KILL: cancel every live order whose tag is a momentum tag of this journal (never FIFTY's)."""
    n = 0
    lg = L.replay(env.journal.read())
    by_tag = {i.tag: i for i in lg.intents.values()}
    for o in env.api.orders():
        tag = str(o.get("tag") or "")
        if B.TAG_RE.match(tag) and B.order_is_live(o):
            if tag in by_tag:
                if env.broker.cancel(by_tag[tag].intent_id, str(o["order_id"]), "KILL"):
                    n += 1
                else:
                    env.alert("KILL_CANCEL_FAILED", f"order {o['order_id']} tag {tag}: cancel not confirmed; cancel "
                              "it in Kite")
            else:
                env.alert("KILL_UNTRACKED", f"open order {o['order_id']} tag {tag} not in the journal: cancel by hand")
    return n


def three_way(env: Env, holdings_open: Dict[str, int], day: str) -> List[str]:
    """16:00 check (spec §C11; frozen design §7.4). Per book ISIN, Kite holdings now must be the opening snapshot,
    with or without today's book fills: whether a same-day CNC buy already shows in holdings() is UNVERIFIED (U6;
    the design expects it only the next morning), so both are accepted here and the next morning's opening snapshot
    is the hard check (holdings >= book). Also: no live book order after the close; book cash not negative.
    -> problems (each also an ALERT; any problem blocks new orders until the owner sends `ack`)."""
    lg = L.replay(env.journal.read())
    probs: List[str] = []
    now_h = {str(r["isin"]): int(r.get("quantity", 0)) + int(r.get("t1_quantity", 0)) for r in env.api.holdings()}
    fills_today: Dict[str, int] = {}
    recs = env.journal.read()
    for r in recs:
        if r["type"] == "FILL" and r["ts"][:10] == day:
            isin = lg.positions[r["position_id"]].isin
            fills_today[isin] = fills_today.get(isin, 0) + int(r["qty"])
    book_qty: Dict[str, int] = {}
    for p in lg.positions.values():
        book_qty[p.isin] = book_qty.get(p.isin, 0) + p.filled_qty
    names = {p.isin: p.symbol for p in lg.positions.values()}
    for isin, bq in sorted(book_qty.items()):
        today = fills_today.get(isin, 0)
        if bq == 0 and today == 0:
            continue
        opening = holdings_open.get(isin, 0)
        have = now_h.get(isin, 0)
        if have not in (opening, opening + today):
            probs.append(f"{names[isin]}: Kite {have} vs opening {opening} (+ today's book fills {today})")
        if have < bq - today:
            probs.append(f"{names[isin]}: Kite {have} < book {bq - today} held before today")
    open_book = [o for o in env.api.orders() if B.TAG_RE.match(str(o.get("tag") or "")) and B.order_is_live(o)]
    if open_book:
        probs.append(f"open book orders after the close: {[o['tag'] for o in open_book]}")
    if lg.cash_paise < 0:
        probs.append(f"book cash negative: {lg.cash_paise / 100:,.2f}")
    for x in probs:
        env.alert("THREE_WAY", x)
    env.journal.append("NOTE", three_way=day, problems=probs)
    return probs


def holdings_short(lg: L.Ledger, holdings_open: Dict[str, int]) -> List[str]:
    """Morning hard check (frozen design §7.4 'Kite >= ledger'): the opening holdings must cover every share the
    book says it owns. -> one line per short ISIN."""
    book_qty: Dict[str, int] = {}
    names: Dict[str, str] = {}
    for p in lg.positions.values():
        book_qty[p.isin] = book_qty.get(p.isin, 0) + p.filled_qty
        names[p.isin] = p.symbol
    return [f"{names[i]}: Kite holds {holdings_open.get(i, 0)} < book {q}" for i, q in sorted(book_qty.items())
            if q > 0 and holdings_open.get(i, 0) < q]

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
import shutil
from dataclasses import dataclass, field
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

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
        n = kill_cancel(env)
        env.journal.append("NOTE", kill_handled=util.today().isoformat(), cancelled=n)
        env.alert("KILL", f"KILL seen: {n} open book orders cancelled; no new orders")
    return True


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
    """Apply owner taps / commands written by the poller (one-shot per approval id)."""
    inbox = env.paths.inbox
    done = inbox / "done"
    done.mkdir(parents=True, exist_ok=True)
    recs = env.journal.read()
    opened = {r["approval_id"]: r for r in recs if r["type"] == "APPROVAL_OPEN"}
    tapped = {r["approval_id"] for r in recs if r["type"] == "OWNER_TAP"}
    for f in sorted(inbox.glob("*.json")):
        try:
            item = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            env.alert("INBOX_BAD", f"unreadable inbox file {f.name}")
            shutil.move(str(f), str(done / f.name))
            continue
        if item.get("kind") == "tap":
            aid = item["approval_id"]
            ap = opened.get(aid)
            if ap is None:
                env.notify(f"tap {aid}: unknown approval (ignored)")
            elif aid in tapped:
                env.notify(f"tap {aid}: already done")
            elif item["choice"] not in ap["choices"]:
                env.notify(f"tap {aid}: choice {item['choice']} not offered")
            else:
                env.journal.append("OWNER_TAP", inbox_file=f.name, approval_id=aid, ref_type=ap["ref_type"],
                                   ref=ap["ref"], choice=item["choice"])
                tapped.add(aid)
                apply_tap(env, ap, item["choice"])
        elif item.get("kind") == "cmd":
            env.journal.append("OWNER_CMD", inbox_file=f.name, cmd=item["cmd"], args=item.get("args", ""))
            apply_cmd(env, item["cmd"], item.get("args", ""))
        shutil.move(str(f), str(done / f.name))


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
            env.journal.append("LEG", position_id=p.position_id, reason="owner skip", **{"from": "PENDING_BUY",
                                                                                        "to": "SKIPPED"})
            env.notify(f"{p.symbol}: skipped; its cash stays in the book")
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
    open_orders = [o for o in env.api.orders() if B.TAG_RE.match(str(o.get("tag") or ""))
                   and str(o.get("status")).upper() in ("OPEN", "TRIGGER PENDING", "PUT ORDER REQ RECEIVED")]
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
           "nse_text": [{"isin": p.isin, "field": "ca_purpose", "quoted": r["purpose"]}
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
            "ca_rows": db.get("ca_rows", []),
            "intent_id": intent_id(pid, p.position_id, attempts[p.position_id]), "isin": p.isin, "symbol": p.symbol,
            "series": p.series, "rank": p.rank, "max_rupees": p.remaining_paise // 100,
            "price_rule": "BUY_1510(1.005,1.010)", "ltp_1445": qq.get("last_price"),
            "upper_circuit": qq.get("upper_circuit_limit"), "lower_circuit": qq.get("lower_circuit_limit"),
            "lock_tries_used": p.tries_used, "non_book_qty": int(holdings_open.get(p.isin, 0)),
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
            env.journal.append("LEG", position_id=p.position_id, reason=f"vet {code}",
                               **{"from": "PENDING_BUY", "to": "SKIPPED"})
            session_used(env, p.position_id, day, False, code)
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


def clear_vet_deferrals(env: Env, lg: L.Ledger) -> None:
    """A VET_DEFERRED overlay lasts one session: lifted at the next step plan (re-vetted fresh)."""
    for p in lg.positions.values():
        if "VET_DEFERRED" in p.overlays and p.leg == "PENDING_BUY":
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="VET_DEFERRED", on=False,
                               reason="next session")


# ---------------------------------------------------------------- 15:10 place
def instruments_ticks(env: Env) -> Dict[str, Decimal]:
    return {r["tradingsymbol"]: dec(r.get("tick_size") or "0.05") for r in env.api.instruments("NSE")
            if r.get("exchange", "NSE") == "NSE"}


def place_all(env: Env, sp: StepPlan, go: List[L.Position], day: str) -> List[str]:
    """-> intent ids ACKed. Nothing after last_place_at. The first order is a probe (spec §C9)."""
    placed: List[str] = []
    if not go:
        return placed
    cutoff = util.at_today(env.cfg["last_place_at"])
    ticks = instruments_ticks(env)
    q = env.api.quote([f"NSE:{tradingsymbol(p.symbol, p.series)}" for p in go])
    cap = env.arm.cap_rupees if env.arm else (int(env.cfg["arm_cap_rupees_default"]) if env.dry else 0)
    spent = 0
    probe_ok = False
    for n, p in enumerate(go):
        if util.now() >= cutoff:
            env.alert("PLACE_LATE", f"{len(go) - n} buys not placed: past {env.cfg['last_place_at']} (next session)")
            break
        ts = tradingsymbol(p.symbol, p.series)
        qq = q.get(f"NSE:{ts}")
        if not qq or not qq.get("last_price"):
            env.alert("NO_QUOTE", f"{p.symbol}: no quote at 15:10 (next session)")
            continue
        ltp = dec(qq["last_price"])
        tick = ticks.get(ts)
        if tick is None:
            env.alert("NO_INSTRUMENT", f"{ts} not in today's instruments (next session)")
            continue
        upper = dec(qq.get("upper_circuit_limit") or ltp * 2)
        limit = tick_down(min(ltp * dec(env.cfg["buy_ltp_mult"]), upper), tick)
        cap_price = tick_down(min(ltp * dec(env.cfg["buy_cap_mult"]), upper), tick)
        max_rupees = p.remaining_paise // 100
        qty = int((Decimal(max_rupees) / (ltp * dec(env.cfg["buy_cap_mult"]))).to_integral_value(rounding=ROUND_FLOOR))
        if qty <= 0:
            env.journal.append("LEG", position_id=p.position_id, reason="qty 0",
                               **{"from": "PENDING_BUY", "to": "SKIPPED"})
            env.alert("QTY0", f"{p.symbol}: LTP {ltp} > Rs {max_rupees}/1.010: skipped, cash stays (D-L11)")
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
            if not probe_ok:
                break
            continue
        spent += max_rupees
        if it.status == "ACK":
            placed.append(iid)
        else:
            handle_reject(env, p, it.status, it.intent_id)
        if not probe_ok:
            tags = {o.get("tag") for o in env.api.orders()}
            if it.status != "ACK" or it.tag not in tags:
                env.alert("PROBE_FAILED", f"first order {it.tag} status {it.status}, in order book: "
                          f"{it.tag in tags}; the other buys wait for the next session")
                break
            probe_ok = True
    return placed


def handle_reject(env: Env, p: L.Position, status: str, iid: str) -> None:
    if status == "TIMEOUT":
        env.alert("TIMEOUT", f"{p.symbol}: no reply for {iid}; the reconcile settles it by tag")
    elif status == "REJECTED_CLOSED":
        env.alert("CLOSED", f"{p.symbol}: market closed reply; next session")
    elif status == "REJECTED_RMS":
        env.alert("RMS", f"{p.symbol}: margin/RMS rejection; next session")
    elif status in ("REJECTED_BAND", "REJECTED_OTHER"):
        hold_leg(env, p, "OWNER_HOLD", f"order rejected ({status})")


# ---------------------------------------------------------------- ladder + cancel
def live_orders_by_intent(env: Env) -> Dict[str, Dict[str, Any]]:
    lg = L.replay(env.journal.read())
    by_tag = {o.get("tag"): o for o in env.api.orders()}
    out = {}
    for i in lg.intents.values():
        if i.status in ("ACK", "OPEN", "PARTIAL", "FOUND") and i.date == util.today().isoformat():
            o = by_tag.get(i.tag)
            if o is not None:
                out[i.intent_id] = o
    return out


def ladder_step(env: Env, step_no: int) -> None:
    recs = env.journal.read()
    caps = {r["intent_id"]: (r["cap_price_paise"], r["ltp_1510_paise"]) for r in recs if r["type"] == "INTENT"}
    lg = L.replay(recs)
    live = live_orders_by_intent(env)
    open_ = {iid: o for iid, o in live.items() if str(o.get("status")).upper() == "OPEN"}
    if not open_:
        return
    syms = {iid: lg.intents[iid].tradingsymbol for iid in open_}
    q = env.api.quote([f"NSE:{s}" for s in syms.values()])
    ticks = instruments_ticks(env)
    for iid, o in open_.items():
        ts = syms[iid]
        qq = q.get(f"NSE:{ts}") or {}
        if not qq.get("last_price"):
            continue
        cap_paise, _ltp0 = caps[iid]
        upper = dec(qq.get("upper_circuit_limit") or qq["last_price"] * 2)
        new = tick_down(min(dec(qq["last_price"]) * dec(env.cfg["buy_ltp_mult"]), Decimal(cap_paise) / 100, upper),
                        ticks.get(ts, Decimal("0.05")))
        if new <= dec(o.get("price") or 0):
            continue
        env.broker.modify_price(iid, str(o["order_id"]), step_no, float(new))


def cancel_unfilled(env: Env) -> None:
    for iid, o in live_orders_by_intent(env).items():
        if str(o.get("status")).upper() == "OPEN":
            env.broker.cancel(iid, str(o["order_id"]), "end of buy window (15:28)")


# ---------------------------------------------------------------- reconcile
def settle_orphans(env: Env, day: str, final: bool) -> None:
    """INTENT without SENT -> the call was never made; SENT/TIMEOUT -> FOUND by exact tag, else (final) owner.
    Run right after placing (so the ladder and the 15:28 cancel see a FOUND order) and again at the reconcile."""
    lg = L.replay(env.journal.read())
    by_tag = {o.get("tag"): o for o in env.api.orders() if o.get("tag")}
    for i in list(lg.intents.values()):
        if i.date != day:
            continue
        if i.status == "INTENT_WRITTEN" and final:           # crash between INTENT and SENT: the call was never made
            env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="ABSENT" if i.tag not in by_tag
                               else "FOUND", order_id=str(by_tag[i.tag]["order_id"]) if i.tag in by_tag else None,
                               reads=["orders"])
        elif i.status in ("SENT", "TIMEOUT"):
            if i.tag in by_tag:
                env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="FOUND",
                                   order_id=str(by_tag[i.tag]["order_id"]), reads=["orders"])
            elif final and i.intent_id not in lg.unresolved:
                env.journal.append("ORPHAN", intent_id=i.intent_id, resolution="OWNER", order_id=None,
                                   reads=["orders"])
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
    for i in lg.intents.values():
        if i.date != day or not i.order_id or i.order_id not in by_oid:
            continue
        o = by_oid[i.order_id]
        st = str(o.get("status")).upper()
        filled = int(o.get("filled_quantity") or 0)
        mapped = {"COMPLETE": "COMPLETE", "CANCELLED": "CANCELLED", "REJECTED": "REJECTED", "OPEN": "OPEN"}.get(st)
        if mapped is None:
            env.alert("ORDER_STATUS", f"{i.intent_id}: unexpected status {st}")
            continue
        if mapped == "OPEN" and filled > 0:
            mapped = "PARTIAL"
        if mapped == "CANCELLED" and filled > 0:
            mapped = "CANCELLED"
        if (mapped, filled) != (i.status, i.filled_qty):
            env.journal.append("ORDER_STATUS", intent_id=i.intent_id, order_id=i.order_id, status=mapped,
                               filled_qty=filled, pending_qty=int(o.get("pending_quantity") or 0),
                               avg_price_paise=L.paise(o.get("average_price") or 0))
        for t in trades:
            if str(t.get("order_id")) != i.order_id or str(t["trade_id"]) in lg.trade_ids:
                continue
            px = L.paise(t["average_price"])
            q = int(t["quantity"])
            env.journal.append("FILL", position_id=i.position_id, order_id=i.order_id, trade_id=str(t["trade_id"]),
                               qty=q, price_paise=px, exch_ts=str(t.get("exchange_timestamp", "")),
                               charges_paise=estimate_buy_charges_paise(env.cfg, q * px), charges_basis="estimate")
            lg.trade_ids.add(str(t["trade_id"]))
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
            env.journal.append("LEG", position_id=p.position_id, reason=f"{p2.tries_used} tries used",
                               **{"from": "PENDING_BUY", "to": "NONE" if p2.filled_qty else "SKIPPED"})
            env.alert("BUY_WINDOW_END", f"{p.symbol}: window over; held {p2.filled_qty} shares, "
                      f"Rs {p2.remaining_paise / 100:,.0f} back to book cash")


# ---------------------------------------------------------------- the afternoon process
def afternoon(env: Env, holdings_open: Dict[str, int], preflight: Callable[[], List[str]]) -> None:
    day = util.today().isoformat()
    recs = env.journal.read()
    if not stage_done(recs, day, "PLAN"):
        env.sleep_until(util.at_today(env.cfg["step_plan_at"]))
        ingest_inbox(env)
        settle_stale(env, day)
        problems = preflight()
        if killed_now(env):
            problems.append("KILL")
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
        clear_vet_deferrals(env, lg)
        lg = L.replay(env.journal.read())
        sp = build_step_plan(env, lg, holdings_open)
        go: List[str] = []
        if sp is not None:
            vd = env.vet_fn(sp.step, sp.vet_input, code_skip_reasons(sp))
            ok = apply_verdict(env, sp, vd, day)
            go = [p.position_id for p in ok]
        env.journal.append("STAGE", date=day, stage="PLAN", result="ok", go=go,
                           step_plan_hash=sp.step_plan_hash if sp else None, facts_hash=sp.facts_hash if sp else None)
        recs = env.journal.read()
    plan_rec = [r for r in recs if r["type"] == "STAGE" and r.get("date") == day and r.get("stage") == "PLAN"][-1]
    go_ids: List[str] = plan_rec.get("go") or []
    if not stage_done(recs, day, "PLACE"):
        if util.now() >= util.at_today(env.cfg["last_place_at"]):
            env.journal.append("STAGE", date=day, stage="PLACE", result="too late (resume after 15:15)")
            if go_ids:
                env.alert("PLACE_LATE", "the process resumed after 15:15: no buy placed today (next session)")
        else:
            lg = L.replay(env.journal.read())
            go = [lg.positions[x] for x in go_ids if lg.positions[x].leg == "PENDING_BUY"
                  and not lg.positions[x].overlays]
            total = sum(p.remaining_paise for p in go) // 100
            started = True
            if go and env.arm is not None and env.arm.mode == "automatic":
                started = env.notify(f"starting BUY step: {len(go)} buys, Rs {total:,}")
                env.journal.append("STEP_START", step="BUY", step_plan_hash=plan_rec.get("step_plan_hash"),
                                   telegram_ok=started)
            if go and started:
                avail = available_cash(env.api.margins())
                if avail < total:
                    env.alert("CASH", f"Kite available Rs {avail:,.0f} < book buys Rs {total:,}: no buy (D09)")
                else:
                    env.sleep_until(util.at_today(env.cfg["place_at"]))
                    if killed_now(env):
                        env.journal.append("STAGE", date=day, stage="PLACE", result="killed")
                        reconcile_stage(env, day)
                        return
                    lg = L.replay(env.journal.read())
                    sp = rebuild_attempts(lg, go_ids)
                    place_all(env, sp, [lg.positions[x] for x in go_ids if lg.positions[x].leg == "PENDING_BUY"
                                        and not lg.positions[x].overlays], day)
                    settle_orphans(env, day, final=False)
            elif go and not started:
                env.journal.append("NOTE", text="starting message not delivered: automatic step did not start")
            env.journal.append("STAGE", date=day, stage="PLACE", result="ok")
        recs = env.journal.read()
    if not stage_done(recs, day, "LADDER"):
        for k, hhmm in enumerate(env.cfg["ladder"], 1):
            t = util.at_today(hhmm)
            if util.now() > t + dt.timedelta(minutes=4):
                continue                                   # that ladder slot has passed (resume)
            env.sleep_until(t)
            if killed_now(env):
                break
            ladder_step(env, k)
        env.journal.append("STAGE", date=day, stage="LADDER", result="ok")
        recs = env.journal.read()
    if not stage_done(recs, day, "CANCEL"):
        env.sleep_until(util.at_today(env.cfg["cancel_at"]))
        killed_now(env)
        cancel_unfilled(env)
        env.journal.append("STAGE", date=day, stage="CANCEL", result="ok")
    reconcile_stage(env, day)


def reconcile_stage(env: Env, day: str) -> None:
    recs = env.journal.read()
    if stage_done(recs, day, "RECON"):
        return
    env.sleep_until(util.at_today("15:35"))
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
    """KILL: cancel every open order whose tag is a momentum tag (never FIFTY's)."""
    n = 0
    lg = L.replay(env.journal.read())
    by_tag = {i.tag: i for i in lg.intents.values()}
    for o in env.api.orders():
        tag = str(o.get("tag") or "")
        if B.TAG_RE.match(tag) and str(o.get("status")).upper() == "OPEN":
            iid = by_tag[tag].intent_id if tag in by_tag else f"untracked:{tag}"
            if tag in by_tag:
                env.broker.cancel(iid, str(o["order_id"]), "KILL")
                n += 1
            else:
                env.alert("KILL_UNTRACKED", f"open order {o['order_id']} tag {tag} not in the journal: cancel by hand")
    return n


def three_way(env: Env, holdings_open: Dict[str, int], day: str) -> List[str]:
    """16:00 check (spec §C11): for every book ISIN, Kite holdings now == opening snapshot + today's book fills, and
    holdings >= book quantity; book cash <= Kite available + today's book buys; no open book order after the close.
    -> problems (each also an ALERT; any problem blocks new orders until the owner resolves it)."""
    lg = L.replay(env.journal.read())
    probs: List[str] = []
    now_h = {str(r["isin"]): int(r.get("quantity", 0)) + int(r.get("t1_quantity", 0)) for r in env.api.holdings()}
    fills_today: Dict[str, int] = {}
    recs = env.journal.read()
    for r in recs:
        if r["type"] == "FILL" and r["ts"][:10] == day:
            isin = lg.positions[r["position_id"]].isin
            fills_today[isin] = fills_today.get(isin, 0) + int(r["qty"])
    for p in lg.positions.values():
        if p.filled_qty == 0 and p.isin not in fills_today:
            continue
        want = holdings_open.get(p.isin, 0) + fills_today.get(p.isin, 0)
        have = now_h.get(p.isin, 0)
        if have != want:
            probs.append(f"{p.symbol}: Kite {have} vs opening {holdings_open.get(p.isin, 0)} + book fills "
                         f"{fills_today.get(p.isin, 0)}")
        if have < p.filled_qty:
            probs.append(f"{p.symbol}: Kite {have} < book {p.filled_qty}")
    open_book = [o for o in env.api.orders() if B.TAG_RE.match(str(o.get("tag") or ""))
                 and str(o.get("status")).upper() == "OPEN"]
    if open_book:
        probs.append(f"open book orders after the close: {[o['tag'] for o in open_book]}")
    if lg.cash_paise < 0:
        probs.append(f"book cash negative: {lg.cash_paise / 100:,.2f}")
    for x in probs:
        env.alert("THREE_WAY", x)
    env.journal.append("NOTE", three_way=day, problems=probs)
    return probs

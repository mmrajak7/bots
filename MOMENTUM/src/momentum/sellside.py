"""Sell side of the afternoon process (January spec §2, §4.2, §5, §8; owner D49: sells before the buys, same window).

  14:45 PLAN     sell intents and the gated buys join the buy step plan: ONE Tier A vet over both sides
  14:46 START    "starting SELL step" must be delivered (D36), retried each minute until 2 minutes before 14:50
  14:50 SELL     LIMIT CNC sells, worst rank first, first-order probe; nothing new after last_sell_place_at
  14:55 / 15:00 / 15:05  MODIFY down to fresh LTP x sell_ltp_mult, never under the floor (LTP_1450 x sell_floor_mult,
                 lower circuit), tick-rounded UP so the price never crosses its bound
  15:06 GATE     sell fills booked; gate OPEN when every sell of the plan is done or cannot trade today for an
                 outside reason (lower circuit, no trade today, not listed); else CLOSED (D32: the gated buys go anyway
                 on the gate_wait_sessions-th session after the rebalance session); the buy amounts from the cash
Unfilled sells rest until the 15:28 cancel and are re-placed next session as attempt + 1. A sell is never skipped on
opinion: the vet can defer or hold it, never drop it. Money is integer paise.
"""
from __future__ import annotations

import datetime as dt
import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any, Dict, List, Optional, Tuple

from . import broker as B
from . import ledger as L
from . import util
from . import vet as V

log = logging.getLogger("momentum.sellside")


# ---------------------------------------------------------------- helpers
def tick_up(price: Decimal, tick: Decimal) -> Decimal:
    return (price / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def dec(x: Any) -> Decimal:
    return Decimal(str(x))


def leg_attempts(lg: L.Ledger, position_id: str, leg: str) -> int:
    return sum(1 for i in lg.intents.values() if i.position_id == position_id and f"|{leg}|" in i.intent_id)


def sell_attempts(lg: L.Ledger, p: L.Position) -> int:
    """Attempts of THIS sell leg: the intents under the plan that ordered it (a position can have more than one sell
    leg in its life: the TEST lot sells 1 share twice)."""
    pre = sell_intent_id(p, 0)[:-1]
    return sum(1 for i in lg.intents.values() if i.intent_id.startswith(pre))


def sell_intent_id(p: L.Position, attempt: int) -> str:
    sp = p.sell_plan or p.plan_id
    return f"{sp['month']}|{sp['signal_day']}|r{sp['data_rev']}|{p.position_id}|sell|{attempt}"


def sell_tag(p: L.Position, attempt: int) -> str:
    """M + yymm of the plan that ordered the sell + S + the sell's number in that plan (unique per plan; position
    numbers are not, two months' positions can share one) + A + attempt. TEST sells: leg letter X, yymm of the day."""
    sp = p.sell_plan or p.plan_id
    ym = sp["signal_day"][2:4] + sp["signal_day"][5:7] if sp["month"] == "TEST" else sp["month"][2:4] + sp["month"][5:7]
    return B.make_tag(ym, "test" if p.test else "sell", p.sell_no, attempt)


def sell_qty(p: L.Position) -> int:
    return max(0, min(p.held_qty, p.sell_target - p.sold_qty))


def open_sell_legs(lg: L.Ledger) -> List[L.Position]:
    """SELLING legs with shares left to sell, in the order they sell (plan, then the plan's sell number)."""
    out = [p for p in lg.positions.values() if p.leg == "SELLING" and sell_qty(p) > 0]
    return sorted(out, key=lambda p: (L.plan_key(p.sell_plan or p.plan_id), p.sell_no))


def sellable_legs(lg: L.Ledger) -> List[L.Position]:
    return [p for p in open_sell_legs(lg) if not p.overlays and not lg.open_intents(p.position_id)]


def gated_legs(lg: L.Ledger, plan_id: Optional[Dict[str, Any]]) -> List[L.Position]:
    if plan_id is None:
        return []
    return sorted((p for p in lg.positions.values() if p.leg == "BUY_GATED" and p.plan_id == plan_id),
                  key=lambda p: p.rank)


def book_held(lg: L.Ledger) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for p in lg.positions.values():
        if p.held_qty:
            out[p.isin] = out.get(p.isin, 0) + p.held_qty
    return out


def estimate_sell_charges_paise(cfg: Dict[str, Any], value_paise: int, first_sell_of_isin_today: bool) -> int:
    """STT (delivery, both sides), exchange, SEBI, GST; DP charge once per ISIN per sell day. Estimate only: the
    contract note is the truth (monthly true-up)."""
    r = cfg["charges_rates"]
    v = Decimal(value_paise)
    stt = v * dec(r.get("stt_sell", r["stt_buy"]))
    txn = v * dec(r["exchange_txn"])
    sebi = v * dec(r["sebi"])
    gst = (dec(r["brokerage"]) * 100 + txn + sebi) * dec(r["gst_on"])
    dp = dec(r.get("dp_per_sell_scrip", "0")) * 100 if first_sell_of_isin_today else Decimal(0)
    return int((stt + txn + sebi + gst + dp).to_integral_value())


def live_symbol(p: L.Position, ticks: Dict[str, Any]) -> str:
    """The name's trading symbol today: its plan series first, else the same symbol in another series (NSE moved it
    to BE / BZ or back to EQ). Unknown -> the plan's symbol (the caller treats it as not listed)."""
    from . import engine as E
    for s in [p.series] + [x for x in ("EQ", "BE", "BZ") if x != p.series]:
        ts = E.tradingsymbol(p.symbol, s)
        if ts in ticks:
            return ts
    return E.tradingsymbol(p.symbol, p.series)


def traded_today(q: Dict[str, Any]) -> bool:
    t = str(q.get("last_trade_time") or "")
    return t[:10] == util.today().isoformat()


# ---------------------------------------------------------------- 14:45: sells + gated buys in the step plan
def extend_step_plan(env: Any, lg: L.Ledger, holdings_open: Dict[str, int], sp: Any) -> Any:
    """Add the sell intents and the gated buys (an envelope, January spec §4.2) to the buy step plan (or make one).
    -> StepPlan or None when nothing is due. Hashes are recomputed over both sides."""
    from . import engine as E
    plan = E.current_plan(lg)
    sells = sellable_legs(lg)
    gated = [p for p in gated_legs(lg, plan["plan_id"] if plan else None) if not p.overlays]
    if sp is None and not sells and not gated:
        return None
    try:
        ticks_now = E.instruments_ticks(env)
    except B.NetworkError:
        ticks_now = {}
    sym_of = {p.position_id: (live_symbol(p, ticks_now) if p.leg == "SELLING" else E.tradingsymbol(p.symbol, p.series))
              for p in sells + gated}
    keys = sorted({f"NSE:{s}" for s in sym_of.values()})
    q = env.api.quote(keys) if keys else {}
    pid = plan["plan_id"] if plan else (sells[0].sell_plan if sells else None)
    if sp is None:
        sp = E.StepPlan("REBALANCE", pid, [], {}, "", "", {}, {
            "schema": "mom.vet.v1", "step": "REBALANCE", "plan_id": pid, "today": util.today().isoformat(),
            "book": {"cash_rupees": lg.cash_paise // 100, "earmarked_rupees": lg.earmarked_paise() // 100},
            "intents": [], "freshness": {"plan_status": "FINAL", "decision_file": plan["decision_file"] if plan
                                         else None}, "nse_text": []})
    sp.quotes.update(q)
    held = book_held(lg)
    rows: List[Dict[str, Any]] = []
    sp.sell_legs, sp.gated, sp.envelopes = sells, gated, {}
    for p in sells:
        att = sell_attempts(lg, p) + 1
        sp.attempts[p.position_id] = att
        qq = q.get(f"NSE:{sym_of[p.position_id]}", {})
        rows.append({"intent_id": sell_intent_id(p, att), "leg": "sell", "isin": p.isin, "symbol": p.symbol,
                     "series": p.series, "trading_symbol_today": sym_of[p.position_id], "rank": p.rank,
                     "sell_reason": p.sell_reason, "qty": sell_qty(p),
                     "book_qty": p.held_qty, "holdings_qty": int(holdings_open.get(p.isin, 0)),
                     "non_book_qty": max(0, int(holdings_open.get(p.isin, 0)) - held.get(p.isin, 0)),
                     "price_rule": f"SELL_1450({env.cfg['sell_ltp_mult']},{env.cfg['sell_floor_mult']})",
                     "ltp_1445": qq.get("last_price"), "lower_circuit": qq.get("lower_circuit_limit"),
                     "traded_today": traded_today(qq) if qq else None, "sessions_tried": len(p.sell_sessions),
                     "quote_missing": not qq})
    if gated:
        proceeds = Decimal(0)
        for p in sells:
            qq = q.get(f"NSE:{sym_of[p.position_id]}", {})
            if qq.get("last_price"):
                proceeds += dec(qq["last_price"]) * sell_qty(p) * 100
        free = Decimal(lg.cash_paise - lg.earmarked_paise()) + proceeds
        each = free / len(gated) * dec(env.cfg["buy_envelope_slack"])
        for p in gated:
            att = E.attempts_so_far(lg, p.position_id) + 1
            sp.attempts[p.position_id] = att
            env_rupees = max(0, int(each / 100))
            sp.envelopes[p.position_id] = env_rupees
            qq = q.get(f"NSE:{E.tradingsymbol(p.symbol, p.series)}", {})
            rows.append({"intent_id": E.intent_id(p.plan_id, p.position_id, att), "leg": "buy", "isin": p.isin,
                         "symbol": p.symbol, "series": p.series, "rank": p.rank, "max_rupees": env_rupees,
                         "price_rule": "BUY_1510(1.005,1.010)", "gated_on_sells": True,
                         "ltp_1445": qq.get("last_price"), "upper_circuit": qq.get("upper_circuit_limit"),
                         "lower_circuit": qq.get("lower_circuit_limit"), "book_qty": p.held_qty,
                         "non_book_qty": max(0, int(holdings_open.get(p.isin, 0)) - held.get(p.isin, 0)),
                         "quote_missing": not qq})
    vin = sp.vet_input
    for r in vin["intents"]:
        r.setdefault("leg", "buy")
    vin["intents"].extend(rows)
    vin["step"] = sp.step = "REBALANCE" if (sells or gated) else sp.step
    plan_obj = {"plan_id": pid, "step": sp.step,
                "intents": sorted(({k: r.get(k) for k in ("intent_id", "isin", "leg", "max_rupees", "qty",
                                                          "price_rule")} for r in vin["intents"]),
                                  key=lambda x: x["intent_id"])}
    facts = {"buy_facts": sp.facts_hash, "rows": sorted(({k: r.get(k) for k in ("intent_id", "holdings_qty",
                                                                                "book_qty", "non_book_qty", "series",
                                                                                "traded_today")}
                                                         for r in rows), key=lambda x: x["intent_id"])}
    sp.step_plan_hash = vin["step_plan_hash"] = util.sha256_obj(plan_obj)
    sp.facts_hash = vin["facts_hash"] = util.sha256_obj(facts)
    return sp


def code_skip_reasons_all(sp: Any) -> Dict[str, Optional[str]]:
    return {it["intent_id"]: ("SKIP_NO_INSTRUMENT" if it.get("quote_missing") else None)
            for it in sp.vet_input["intents"]}


def apply_side_verdicts(env: Any, sp: Any, vd: V.Verdict, day: str) -> Tuple[List[str], List[str]]:
    """Sells and gated buys (buys already pending go through engine.apply_verdict). -> (sell go ids, gated go ids).
    A sell is never skipped: SKIP / missing quote = not sellable today (no hold); DEFER = one session
    (OWNER_HOLD after defer_max in a row); HOLD = OWNER_HOLD. A gated buy: DEFER = one session, HOLD/SKIP = owner."""
    from . import engine as E
    if not vd.valid:
        for p in sp.sell_legs:
            sell_session(env, p.position_id, day, "vet unavailable")
        return [], []
    go_s: List[str] = []
    go_b: List[str] = []
    rows = {r["intent_id"]: r for r in sp.vet_input["intents"]}
    for p in sp.sell_legs:
        iid = sell_intent_id(p, sp.attempts[p.position_id])
        a, code, note = vd.actions.get(iid, ("HOLD", "HOLD_OTHER", "no verdict"))
        if rows[iid]["quote_missing"] or a == "SKIP":
            sell_session(env, p.position_id, day, "no quote / not listed at 14:45")
            env.alert("NO_QUOTE", f"SELL {p.symbol}: no quote at {env.cfg['step_plan_at']}: next session")
        elif a == "GO":
            go_s.append(p.position_id)
        elif a == "DEFER":
            res = [str(r.get("result", "")) for r in env.journal.read() if r["type"] == "SELL_SESSION"
                   and r["position_id"] == p.position_id]
            n = 0
            for x in reversed(res):            # only the deferrals in a row
                if not x.startswith("vet DEFER"):
                    break
                n += 1
            sell_session(env, p.position_id, day, f"vet DEFER {code}")
            if n + 1 >= int(env.cfg["defer_max"]):
                E.hold_leg(env, p, "OWNER_HOLD", f"sell deferred by the vet {n + 1} sessions ({code}: {note})")
            else:
                env.journal.append("OVERLAY", position_id=p.position_id, overlay="VET_DEFERRED", on=True,
                                   reason=code)
                env.notify(f"DEFER SELL {p.symbol}: {code} {note}")
        else:
            sell_session(env, p.position_id, day, f"vet {code}")
            E.hold_leg(env, p, "OWNER_HOLD", f"sell held by the vet {code}: {note}")
    for p in sp.gated:
        iid = E.intent_id(p.plan_id, p.position_id, sp.attempts[p.position_id])
        a, code, note = vd.actions.get(iid, ("HOLD", "HOLD_OTHER", "no verdict"))
        if a == "GO":
            go_b.append(p.position_id)
        elif a == "DEFER" or rows[iid]["quote_missing"]:
            env.journal.append("OVERLAY", position_id=p.position_id, overlay="VET_DEFERRED", on=True, reason=code)
            env.notify(f"DEFER {p.symbol}: {code} {note}")
        else:
            E.hold_leg(env, p, "OWNER_HOLD", f"vet {a} {code}: {note}")
    return go_s, go_b


def sell_session(env: Any, position_id: str, day: str, result: str) -> None:
    """One SELL_SESSION per (sell leg, day): a re-run must not count a session twice."""
    p = L.replay(env.journal.read()).positions[position_id]
    if day not in p.sell_sessions:
        env.journal.append("SELL_SESSION", position_id=position_id, date=day, result=result)


# ---------------------------------------------------------------- 14:50: place
def place_sells(env: Any, go_ids: List[str], day: str, holdings_open: Dict[str, int]) -> List[str]:
    """-> intent ids ACKed. The first sell is a probe: it must stand OPEN or COMPLETE in the order book before the
    next goes. A depository refusal (DDPI / TPIN) stops every sell and tells the owner."""
    from . import engine as E
    placed: List[str] = []
    lg0 = L.replay(env.journal.read())
    legs = [lg0.positions[x] for x in go_ids if x in lg0.positions]
    legs = [p for p in legs if p.leg == "SELLING" and not p.overlays and sell_qty(p) > 0
            and not any(i.date == day for i in lg0.intents.values() if i.position_id == p.position_id
                        and "|sell|" in i.intent_id)]
    if not legs:
        return placed
    cutoff = util.at_today(env.cfg["last_sell_place_at"])
    try:
        ticks = E.instruments_ticks(env)
        q = env.api.quote([f"NSE:{E.tradingsymbol(p.symbol, p.series)}" for p in legs])
    except B.NetworkError as e:
        env.alert("KITE_DOWN", f"no instruments / quotes at {env.cfg['sell_place_at']} ({str(e)[:150]}): no sell "
                  "placed today (next session)")
        return placed
    held = book_held(lg0)
    snap = [r for r in env.journal.read() if r["type"] == "HOLDINGS_SNAPSHOT" and r["ts"][:10] == day
            and "settled" in r]
    settled = {k: int(v) for k, v in snap[-1]["settled"].items()} if snap else None
    probe_ok = False
    probes = 0
    syms = {p.position_id: live_symbol(p, ticks) for p in legs}
    if any(syms[p.position_id] != E.tradingsymbol(p.symbol, p.series) for p in legs):
        q.update(env.api.quote([f"NSE:{s}" for s in syms.values()]))
    for n, p in enumerate(legs):
        if util.now() >= cutoff:
            env.alert("SELL_LATE", f"{len(legs) - n} sells not placed: past {env.cfg['last_sell_place_at']} "
                      "(next session)")
            break
        ts = syms[p.position_id]
        tick = ticks.get(ts)
        qq = q.get(f"NSE:{ts}") or {}
        if ts != E.tradingsymbol(p.symbol, p.series) and tick is not None:
            env.alert("SERIES_CHANGED", f"{p.symbol}: trades today as {ts} (book series {p.series}); sold as {ts}")
        if tick is None or tick <= 0:
            sell_session(env, p.position_id, day, "not in today's instruments")
            env.alert("SELL_NOT_LISTED", f"{ts} is not in today's Kite instruments (suspended / renamed?): the sell "
                      "waits; it does not hold the buys")
            continue
        if not qq.get("last_price") or not qq.get("lower_circuit_limit"):
            sell_session(env, p.position_id, day, "no quote / no circuit at 14:50")
            env.alert("NO_QUOTE", f"SELL {p.symbol}: no quote or lower circuit at {env.cfg['sell_place_at']} "
                      "(next session)")
            continue
        if held.get(p.isin, 0) > int(holdings_open.get(p.isin, 0)):
            E.hold_leg(env, p, "OWNER_HOLD", f"Kite holds {holdings_open.get(p.isin, 0)} < book "
                       f"{held.get(p.isin, 0)}: no sell until the owner checks")
            continue
        if settled is not None and not p.test and sell_qty(p) > int(settled.get(p.isin, 0)):
            # review L4: a book sell only from SETTLED shares (a T1 sale gets no same-day credit and risks a short
            # delivery); the TEST lot is bought days before its sells
            sell_session(env, p.position_id, day, "shares not settled yet")
            env.alert("SELL_UNSETTLED", f"{p.symbol}: only {settled.get(p.isin, 0)} settled shares for a sell of "
                      f"{sell_qty(p)}: next session")
            continue
        if int(holdings_open.get(p.isin, 0)) > held.get(p.isin, 0):
            env.alert("SHARED_SELL", f"{p.symbol}: the account holds {int(holdings_open[p.isin]) - held[p.isin]} "
                      "shares outside the book; the book sells only its own (D38)")
        ltp, lower = dec(qq["last_price"]), dec(qq["lower_circuit_limit"])
        limit = tick_up(max(ltp * dec(env.cfg["sell_ltp_mult"]), lower), tick)
        floor = tick_up(max(ltp * dec(env.cfg["sell_floor_mult"]), lower), tick)
        att = sell_attempts(L.replay(env.journal.read()), p) + 1
        if att > B.MAX_ATTEMPT:
            E.hold_leg(env, p, "OWNER_HOLD", f"sell attempt > {B.MAX_ATTEMPT}: sell it by hand, then 'sold <isin> "
                       "<qty> <avg_price>'")
            continue
        iid = sell_intent_id(p, att)
        qty = sell_qty(p)
        try:
            it = env.broker.place_sell(
                intent_id=iid, key={"plan_id": p.sell_plan, "position_id": p.position_id, "leg": "sell",
                                    "attempt": att},
                tag=sell_tag(p, att), isin=p.isin, tradingsymbol=ts, qty=qty, limit=float(limit), ltp=float(ltp),
                floor_price=float(floor), max_qty=p.held_qty)
        except B.BrokerRefused as e:
            if env.dry and str(e) == "DRY_MODE":
                env.journal.append("NOTE", dry_order={"intent_id": iid, "side": "SELL", "symbol": ts, "qty": qty,
                                                      "limit": str(limit), "ltp": str(ltp), "floor": str(floor)})
                probe_ok = True
                continue
            env.alert("REFUSED", f"SELL {p.symbol}: {e}")
            if not probe_ok or str(e) in ("KILL", "NOT_ARMED"):
                break
            continue
        except B.NetworkError as e:
            env.alert("KITE_DOWN", f"SELL {p.symbol}: order book unreadable before the order ({str(e)[:150]}); the "
                      "remaining sells wait for the next session")
            break
        status = it.status
        if status == "REJECTED_DDPI":
            ddpi_alert(env, it)
            break
        if status != "ACK":
            after_sell_reject(env, p, status, it.intent_id)
        if not probe_ok:
            in_book = E.probe_row(env, it.tag) if status == "ACK" else None
            state = B.order_state(in_book) if in_book is not None else "not in the order book"
            if status == "ACK" and state == "REJECTED" and in_book is not None:
                status = E.book_reject(env, it, in_book)
                if status == "REJECTED_DDPI":
                    ddpi_alert(env, it)
                    break
                after_sell_reject(env, p, status, it.intent_id)
            if status != "ACK" or state not in E.PROBE_GOOD_STATES:
                probes += 1
                if status in ("REJECTED_BAND", "REJECTED_OTHER") and probes < int(env.cfg["probe_max"]):
                    env.alert("PROBE_MOVED", f"first sell {it.tag} rejected ({status}); that name waits, the next "
                              "sell is the probe")
                    continue
                env.alert("PROBE_FAILED", f"first sell {it.tag}: reply {status}, order book: {state}; the other "
                          "sells wait for the next session")
                break
            probe_ok = True
        if status == "ACK":
            placed.append(iid)
    return placed


def ddpi_alert(env: Any, it: L.Intent) -> None:
    env.alert("DDPI", f"{it.tradingsymbol}: Kite / the depository refused the sell ({it.intent_id}): DDPI or TPIN "
              "authorisation is missing. Every book sell waits. Check DDPI in Zerodha Console; the code retries "
              "next session.")


def after_sell_reject(env: Any, p: L.Position, status: str, iid: str) -> None:
    """BAND / CLOSED / AUTH -> next session; RMS (for a sell: 'insufficient holdings') and OTHER -> OWNER_HOLD
    (the shares or the scrip have a problem a retry will not fix); TIMEOUT -> settled by tag."""
    from . import engine as E
    if status == "TIMEOUT":
        env.alert("TIMEOUT", f"SELL {p.symbol}: no reply for {iid}; settled by tag")
    elif status in ("REJECTED_BAND", "REJECTED_CLOSED", "REJECTED_AUTH"):
        env.alert(status[9:], f"SELL {p.symbol}: {status}; next session")
    elif status == "REJECTED_DDPI":
        pass                                    # ddpi_alert, once, by the caller
    else:
        E.hold_leg(env, p, "OWNER_HOLD", f"sell rejected ({status})")


# ---------------------------------------------------------------- 14:55 / 15:00 / 15:05: ladder
def sell_ladder_step(env: Any, step_no: int) -> None:
    from . import engine as E
    recs = env.journal.read()
    floors = {r["intent_id"]: int(r["floor_price_paise"]) for r in recs if r["type"] == "INTENT"
              and r.get("side") == "SELL" and "floor_price_paise" in r}
    lg = L.replay(recs)
    open_ = {iid: o for iid, o in E.live_orders_by_intent(env).items()
             if iid in floors and B.order_state(o) == "OPEN"}
    if not open_:
        return
    syms = {iid: lg.intents[iid].tradingsymbol for iid in open_}
    q = env.api.quote([f"NSE:{s}" for s in syms.values()])
    ticks = E.instruments_ticks(env)
    failed: List[str] = []
    for iid, o in open_.items():
        ts = syms[iid]
        qq = q.get(f"NSE:{ts}") or {}
        tick = ticks.get(ts)
        if not qq.get("last_price") or not qq.get("lower_circuit_limit") or tick is None:
            continue
        new = tick_up(max(dec(qq["last_price"]) * dec(env.cfg["sell_ltp_mult"]), Decimal(floors[iid]) / 100,
                          dec(qq["lower_circuit_limit"])), tick)
        if new >= dec(o.get("price") or 0):
            continue                            # a sell is only ever re-priced DOWN toward the market
        try:
            if not env.broker.modify_price(iid, str(o["order_id"]), step_no, float(new)):
                failed.append(ts)
        except B.BrokerRefused as e:
            log.warning("sell ladder %s: %s refused: %s", step_no, iid, e)
            failed.append(ts)
    if failed:
        env.alert("MODIFY_FAILED", f"sell ladder step {step_no}: re-price not done for {failed}")


def sell_step(env: Any, day: str, go_ids: List[str], holdings_open: Dict[str, int]) -> None:
    """The SELL stage: start message, place at sell_place_at, ladder. A resumed run never places a sell twice
    (broker guard + one intent per leg per day) and skips ladder slots that have passed."""
    from . import engine as E
    if go_ids and util.now() < util.at_today(env.cfg["last_sell_place_at"]):
        started = env.notify(f"starting SELL step: {len(go_ids)} sells")
        give_up = util.at_today(env.cfg["sell_place_at"]) - dt.timedelta(minutes=2)
        while not started and util.now() < give_up:
            env.sleep_until(min(util.now() + dt.timedelta(seconds=60), give_up))
            started = env.notify(f"starting SELL step: {len(go_ids)} sells")
        env.journal.append("STEP_START", step="SELL", step_plan_hash=None, telegram_ok=started)
        if started:
            env.sleep_until(util.at_today(env.cfg["sell_place_at"]))
            if not E.killed_now(env):
                place_sells(env, go_ids, day, holdings_open)
                try:
                    E.settle_orphans(env, day, final=False)
                except B.NetworkError as e:
                    log.warning("orphans not settled after the sells (%s)", e)
        else:
            env.journal.append("NOTE", text="starting message not delivered: SELL step did not start")
    elif go_ids:
        env.alert("SELL_LATE", f"the afternoon reached the sell step after {env.cfg['last_sell_place_at']}: no sell "
                  "today (next session)")
    for k, hhmm in enumerate(env.cfg["sell_ladder"], 1):
        t = util.at_today(hhmm)
        if util.now() > t + dt.timedelta(minutes=int(env.cfg["ladder_grace_min"])):
            continue
        env.sleep_until(t)
        if E.killed_now(env):
            break
        try:
            E.settle_orphans(env, day, final=False)
            sell_ladder_step(env, k)
        except B.NetworkError as e:
            env.alert("LADDER_SKIPPED", f"sell {hhmm}: Kite read failed ({str(e)[:150]}); orders keep their price")
    env.journal.append("STAGE", date=day, stage="SELL", result="ok")


# ---------------------------------------------------------------- 15:06: gate + buy amounts
def gate_stage(env: Any, day: str, envelopes: Dict[str, int]) -> None:
    """D32 gate (frozen design §6.3, judged at gate_at). Writes GATE and, when the buys may go, one BUY_AMOUNT per
    gated buy of the plan (held ones too: their share stays earmarked until released or expired)."""
    from . import engine as E
    env.sleep_until(util.at_today(env.cfg["gate_at"]))
    done = [r for r in env.journal.read() if r["type"] == "GATE" and r["date"] == day
            and r.get("result") in ("OPEN", "FORCED")]
    if done:                                   # resumed after a crash inside the gate: finish the SAME judgement
        lg = L.replay(env.journal.read())
        for pid, a in (done[-1].get("amounts") or {}).items():
            if lg.positions[pid].leg == "BUY_GATED":
                env.journal.append("BUY_AMOUNT", position_id=pid, earmark_paise=int(a), date=day,
                                   basis="gate resumed")
        env.journal.append("STAGE", date=day, stage="GATE", result="ok (resumed)")
        return
    try:
        E.settle_orphans(env, day, final=False)
        E.sync_orders(env, day, final=False)          # sell fills so far: the cash the buys may use
    except B.NetworkError as e:
        env.alert("KITE_DOWN", f"order book unreadable at the gate ({str(e)[:150]}): the gated buys wait (no "
                  "session counted)")
        pl = E.current_plan(L.replay(env.journal.read()))
        env.journal.append("GATE", date=day, plan=L.plan_key(pl["plan_id"]) if pl else None, result="UNREAD",
                           reasons=[str(e)[:200]])
        env.journal.append("STAGE", date=day, stage="GATE", result="unread")
        return
    lg = L.replay(env.journal.read())
    plan = E.current_plan(lg)
    gated = gated_legs(lg, plan["plan_id"] if plan else None)
    if not gated:
        if any(i.date == day and "|sell|" in i.intent_id for i in lg.intents.values()):
            # a sell ran today (a TEST sell): keep the sale-credit fields for the record (January spec §11)
            env.journal.append("GATE", date=day, plan=None, result="NO_GATED_BUYS", reasons=[],
                               margins=margins_log(env))
        env.journal.append("STAGE", date=day, stage="GATE", result="no gated buys")
        return
    pk = L.plan_key(plan["plan_id"])
    margins = margins_log(env)
    reasons: List[str] = []
    mine = [p for p in lg.positions.values() if p.sell_plan == plan["plan_id"] and p.leg == "SELLING"
            and sell_qty(p) > 0]
    if mine:
        try:
            ticks = E.instruments_ticks(env)
            q = env.api.quote([f"NSE:{live_symbol(p, ticks)}" for p in mine])
        except B.NetworkError as e:
            ticks, q = {}, {}
            reasons.append(f"quotes unreadable at the gate ({str(e)[:100]})")
        for p in mine:
            ts = live_symbol(p, ticks)
            qq = q.get(f"NSE:{ts}") or {}
            if ticks and ts not in ticks:
                continue                                   # not listed today: outside the engine
            if qq and (not traded_today(qq) or (qq.get("lower_circuit_limit") and qq.get("last_price")
                                                and dec(qq["last_price"]) <= dec(qq["lower_circuit_limit"]))):
                continue                                   # locked at the lower circuit / no trade today
            why = ("held: " + "/".join(sorted(p.overlays))) if p.overlays else f"{sell_qty(p)} shares unsold"
            reasons.append(f"{p.symbol} {why}")
    recs = env.journal.read()
    closed_before = len({r["date"] for r in recs if r["type"] == "GATE" and r.get("plan") == pk
                         and r.get("result") in ("CLOSED", "UNREAD") and r["date"] != day})
    if reasons and closed_before < int(env.cfg["gate_wait_sessions"]):
        env.journal.append("GATE", date=day, plan=pk, result="CLOSED", reasons=reasons, margins=margins)
        env.alert("BUY_GATED", f"{len(gated)} buys wait for the sells ({'; '.join(reasons)[:300]}); gated session "
                  f"{closed_before + 1} of {env.cfg['gate_wait_sessions']} (then they go with the cash there)")
        env.journal.append("STAGE", date=day, stage="GATE", result="closed")
        return
    from .evening import earmark_paise
    free = lg.cash_paise - lg.earmarked_paise()
    em = earmark_paise(max(0, free), len(gated), Decimal(env.cfg["charges_reserve"]))
    if em < 100:
        env.journal.append("GATE", date=day, plan=pk, result="NO_CASH", reasons=reasons, margins=margins)
        env.alert("IDLE_BUYS", f"{len(gated)} gated buys but only Rs {free / 100:,.0f} free: no buy (tested F3)")
        env.journal.append("STAGE", date=day, stage="GATE", result="no cash")
        return
    amounts = {}
    for p in gated:
        a = em
        if p.position_id in envelopes:                     # never above what the vet saw at 14:45
            a = min(a, int(envelopes[p.position_id]) * 100)
        amounts[p.position_id] = a
    # the judgement first, then its amounts: a crash in between resumes from this record (judged once)
    env.journal.append("GATE", date=day, plan=pk, result="OPEN" if not reasons else "FORCED", reasons=reasons,
                       margins=margins, amounts=amounts)
    for p in gated:
        env.journal.append("BUY_AMOUNT", position_id=p.position_id, earmark_paise=amounts[p.position_id], date=day,
                           basis=f"gate {'OPEN' if not reasons else 'FORCED'}: free {free} / {len(gated)}")
    if reasons:
        env.alert("GATE_FORCED", f"sells still open after {closed_before} gated sessions ({'; '.join(reasons)[:200]}):"
                  f" the {len(gated)} buys go with the cash there (D32)")
    env.journal.append("STAGE", date=day, stage="GATE", result="ok")


MARGIN_FIELDS = (("net",), ("available", "live_balance"), ("available", "cash"), ("utilised", "holding_sales"),
                 ("utilised", "debits"), ("utilised", "delivery"))


def margins_log(env: Any) -> Dict[str, Any]:
    """The fields that may carry the same-day sale credit (UNVERIFIED which): kept on every GATE record so the TEST
    sells and January show which one moves (January spec §11)."""
    try:
        eq = env.api.margins().get("equity", {})
    except B.NetworkError as e:
        return {"error": str(e)[:120]}
    out: Dict[str, Any] = {}
    for path in MARGIN_FIELDS:
        v: Any = eq
        for k in path:
            v = v.get(k) if isinstance(v, dict) else None
        out[".".join(path)] = v
    return out


# ---------------------------------------------------------------- 15:10 sizing: the extra share (D52)
def size_buys(env: Any, legs: List[L.Position], q: Dict[str, Any], ticks: Dict[str, Decimal]
              ) -> Tuple[Dict[str, Tuple[int, Decimal, Decimal, Decimal, int]], List[Tuple[str, str, int]]]:
    """-> position_id -> (qty, ltp, limit, cap_price, max_rupees for the broker). floor(amount / cap price) for each
    buy, then one more share for the buys with the largest fraction of a share left over, paid from the summed
    leftovers of the buys placed together, never above amount x (1 + extra_share_max_over) for one name. The total
    never exceeds the sum of the amounts (January spec §6). Legs without a usable quote are left out (the caller
    alerts as before). -> (sizes, earmark moves (giver, receiver, paise)): the share's cost above the receiver's own
    amount is moved out of the givers' earmarks, so a giver that does not fill cannot spend it again on a retry."""
    from . import engine as E
    out: Dict[str, Tuple[int, Decimal, Decimal, Decimal, int]] = {}
    rem: Dict[str, Decimal] = {}
    over = dec(env.cfg["extra_share_max_over"])
    for p in legs:
        ts = E.tradingsymbol(p.symbol, p.series)
        qq = q.get(f"NSE:{ts}") or {}
        tick = ticks.get(ts)
        if tick is None or tick <= 0 or not qq.get("last_price") or not qq.get("upper_circuit_limit"):
            continue
        ltp, upper = dec(qq["last_price"]), dec(qq["upper_circuit_limit"])
        limit = E.tick_down(min(ltp * dec(env.cfg["buy_ltp_mult"]), upper), tick)
        cap = E.tick_down(min(ltp * dec(env.cfg["buy_cap_mult"]), upper), tick)
        amount = Decimal(p.remaining_paise // 100)
        qty = int((amount / (ltp * dec(env.cfg["buy_cap_mult"]))).to_integral_value(rounding=ROUND_FLOOR))
        if qty <= 0 or limit <= 0 or cap <= 0:
            out[p.position_id] = (0, ltp, limit, cap, int(amount))
            continue
        out[p.position_id] = (qty, ltp, limit, cap, int(amount))
        if not p.test:                                 # the TEST lot buys exactly its quantity
            rem[p.position_id] = amount - qty * cap
    left = {k: v for k, v in rem.items()}                # leftover each buy can still give, rupees
    moves: List[Tuple[str, str, int]] = []
    for pid in sorted(rem, key=lambda x: -(rem[x] / out[x][3])):
        qty, ltp, limit, cap, amount = out[pid]
        need = cap - left[pid]                         # what the others must give for one more share
        others = sorted((k for k in left if k != pid and left[k] > 0), key=lambda k: -left[k])
        if need <= 0 or sum((left[k] for k in others), Decimal(0)) < need:
            continue
        if (qty + 1) * cap > Decimal(amount) * (1 + over):
            continue
        got = Decimal(0)
        for k in others:
            take = min(left[k], need - got)
            # whole paise, rounded UP for the receiver so it can always pay; never more than the giver has left
            pz = min(int((take * 100).to_integral_value(rounding=ROUND_CEILING)), int(left[k] * 100))
            if pz <= 0:
                continue
            moves.append((k, pid, pz))
            left[k] -= Decimal(pz) / 100
            got += Decimal(pz) / 100
            if got >= need:
                break
        if got < need:                                 # rounding left it short: undo, no extra share
            for m_ in [m_ for m_ in moves if m_[1] == pid]:
                left[m_[0]] += Decimal(m_[2]) / 100
                moves.remove(m_)
            continue
        left[pid] = Decimal(0)
        new_max = int((Decimal(amount) + got).to_integral_value(rounding=ROUND_FLOOR))
        if (qty + 1) * cap > new_max:
            new_max = int(((qty + 1) * cap).to_integral_value(rounding=ROUND_CEILING))
        out[pid] = (qty + 1, ltp, limit, cap, new_max)
    return out, moves

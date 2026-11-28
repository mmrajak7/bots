"""TEST lot (owner D51, January spec §9): prove the sell path on real orders before the first book sells.

  buy day (config test_lot.buy_day, Wed 9 Dec 2026)   PLAN_FINAL ("TEST", day, 0): buy `qty` shares of one name
  each sell day (test_lot.sell_days, 14 + 17 Dec)     PLAN_FINAL ("TEST", day, 0): sell 1 share of it
Both run through the normal afternoon process (vet, probe, ladder, gate, cancel, reconcile). The TEST money is outside
book cash (ledger test_cash_paise); a TEST plan is never the current plan (engine.current_plan). A sell that does not
fill retries every session by itself (the spare day). Name: chosen by code from the current month's frozen band,
middle half by liquidity rank, LTP in [ltp_min, ltp_max], EQ series listed today, held by nobody in the account.
"""
from __future__ import annotations

import json
import logging
from decimal import ROUND_CEILING, Decimal
from typing import Any, Dict, List, Optional

from . import ledger as L
from . import util

log = logging.getLogger("momentum.testlot")


def test_positions(lg: L.Ledger) -> List[L.Position]:
    return sorted((p for p in lg.positions.values() if p.test), key=lambda p: p.position_id)


def pick_name(env: Any, lg: L.Ledger, holdings: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """-> {symbol, isin, ltp} or None (alerted)."""
    from . import engine as E
    cfg = env.cfg["test_lot"]
    plan = E.current_plan(lg)
    if plan is None:
        env.alert("TEST_LOT", "no rebalance plan on file: no band to pick the TEST name from")
        return None
    pid = plan["plan_id"]
    band_file = env.paths.bands / f"b150_{pid['month']}_r{pid['data_rev']}.json"
    try:
        names = json.loads(band_file.read_text(encoding="utf-8"))["names"]
    except (OSError, ValueError, KeyError) as e:
        env.alert("TEST_LOT", f"band file {band_file.name} unreadable ({e}): no TEST buy")
        return None
    book = {p.isin for p in lg.positions.values()}
    lo, hi = int(cfg["band_rank_from"]), int(cfg["band_rank_to"])
    cands = [n for n in names if lo <= int(n["rank"]) <= hi and n["isin"] not in book
             and int(holdings.get(n["isin"], 0)) == 0]
    if not cands:
        env.alert("TEST_LOT", "no band name fits the TEST rules: no TEST buy")
        return None
    listed = E.instruments_ticks(env)
    cands = [n for n in cands if n["symbol"] in listed]            # plain symbol listed = EQ series today
    q = env.api.quote([f"NSE:{n['symbol']}" for n in cands[:200]]) if cands else {}
    for n in cands:
        qq = q.get(f"NSE:{n['symbol']}") or {}
        ltp = qq.get("last_price")
        if ltp and Decimal(str(cfg["ltp_min"])) <= Decimal(str(ltp)) <= Decimal(str(cfg["ltp_max"])):
            return {"symbol": n["symbol"], "isin": n["isin"], "ltp": float(ltp), "band_rank": int(n["rank"])}
    env.alert("TEST_LOT", "no band name in the TEST price range today: no TEST buy")
    return None


def write_plan(env: Any, day: str, buys: List[Dict[str, Any]], sells: List[Dict[str, Any]]) -> None:
    plan_id = {"month": "TEST", "signal_day": day, "data_rev": 0}
    dec = {"plan_id": plan_id, "status": "FINAL", "made_at": util.now().isoformat(), "buys": buys, "sells": sells,
           "holds": [], "kind": "TEST lot (D51)"}
    text = json.dumps(dec, indent=1, sort_keys=True) + "\n"
    name = f"TEST_{day}_r0.json"
    f = env.paths.decisions / name
    if f.exists():                              # a crash between the file and its PLAN_FINAL: use the file as written
        text = f.read_text(encoding="utf-8")
        dec = json.loads(text)
        buys, sells = dec["buys"], dec["sells"]
    else:
        util.write_once(f, text)
    env.journal.append("PLAN_FINAL", plan_id=plan_id, decision_file=name, decision_sha256=util.sha256_text(text),
                       audit_status="PASSED", holds=[],
                       buys=[{k: b[k] for k in ("position_id", "isin", "symbol", "series", "rank", "earmark_paise")}
                             for b in buys],
                       sells=[{k: x[k] for k in ("position_id", "reason", "sell_no", "rank", "qty")} for x in sells])


def run(env: Any, holdings: Dict[str, int]) -> str:
    """The test-plan job (cron 14:35 on weekdays): acts only on the configured days. -> what it did."""
    cfg = env.cfg["test_lot"]
    day = util.today().isoformat()
    lg = L.replay(env.journal.read())
    if any(r["plan_id"]["month"] == "TEST" and r["plan_id"]["signal_day"] == day for r in lg.plans.values()):
        return "TEST plan for today already written"
    tp = test_positions(lg)
    if day == cfg["buy_day"]:
        if tp:
            return "TEST lot already bought"
        n = pick_name(env, lg, holdings)
        if n is None:
            return "no TEST name"
        qty = int(cfg["qty"])
        # enough for qty shares at the 15:10 cap price (LTP x 1.010) with room for a 1% move by then
        em = int((Decimal(str(n["ltp"])) * qty * Decimal("1.010") * Decimal("1.02") * 100)
                 .to_integral_value(rounding=ROUND_CEILING))
        write_plan(env, day, [{"position_id": "TEST-01", "isin": n["isin"], "symbol": n["symbol"], "series": "EQ",
                               "rank": 0, "earmark_paise": em, "signal_close_paise": int(n["ltp"] * 100),
                               "ca_rows": [], "band_rank": n["band_rank"]}], [])
        env.notify(f"TEST lot: buying {qty} {n['symbol']} at 15:10 (about Rs {em / 100:,.0f}, outside the book)")
        return f"TEST buy plan {n['symbol']}"
    if day in cfg["sell_days"]:
        if not tp:
            env.alert("TEST_LOT", f"sell day {day} but no TEST lot was bought")
            return "no TEST lot"
        p = tp[0]
        if p.leg == "SELLING":
            return "the previous TEST sell is still working (it retries each session)"
        if p.leg != "NONE" or p.held_qty <= 0:
            env.alert("TEST_LOT", f"TEST lot {p.symbol} is {p.leg} with {p.held_qty} shares: no TEST sell today")
            return "TEST lot not sellable"
        k = 2 + sum(1 for r in lg.plans.values() if r["plan_id"]["month"] == "TEST" and r.get("sells"))
        write_plan(env, day, [], [{"position_id": p.position_id, "reason": "TEST", "sell_no": k, "rank": 0,
                                   "qty": 1}])
        env.notify(f"TEST lot: selling 1 {p.symbol} at 14:50 (sell {k - 1} of {len(cfg['sell_days'])})")
        return f"TEST sell plan {p.symbol} #{k - 1}"
    return "not a TEST day"


def result_text(env: Any, day: str) -> Optional[str]:
    """After the 15:35 reconcile on a TEST sell day: what the sell proved (§13.4 PASS fields + the sale-credit
    margins logged at the gate). None when no TEST sell ran today."""
    recs = env.journal.read()
    lg = L.replay(recs)
    its = [i for i in lg.intents.values() if i.date == day and lg.positions[i.position_id].test
           and "|sell|" in i.intent_id]
    if not its:
        return None
    gates = [r for r in recs if r["type"] == "GATE" and r["date"] == day]
    fills = [r for r in recs if r["type"] == "FILL" and r.get("side") == "SELL" and r["ts"][:10] == day
             and lg.positions[r["position_id"]].test]
    parts = [f"{i.tag} {i.status} filled {i.filled_qty}/{i.qty}" for i in its]
    px = ", ".join(f"{f['qty']} @ {f['price_paise'] / 100}" for f in fills) or "no fill"
    m = gates[-1].get("margins") if gates else None
    ok = any(i.status == "COMPLETE" for i in its)
    return (f"TEST SELL {'PASS' if ok else 'NOT DONE'}: {'; '.join(parts)}; fills {px}; ledger TEST shares "
            f"{[p.held_qty for p in test_positions(lg)]}; margins at the gate {m}")

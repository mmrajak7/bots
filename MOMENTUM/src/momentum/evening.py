"""Signal-day evening: refresh -> sessions check -> F&O members -> rebuild -> decide -> audit -> decision file -> FINAL.

Build spec §C4-§C7; frozen design §3.2-§3.5, §4.1, E21. December slice: the book starts from cash (month 1 = ranked
top N, equal earmarks). A book that already holds names needs decide_live with the ledger mapping (frozen design §4.1),
which is the January build: this module refuses such a book rather than guessing.
Fail closed everywhere: any failed step -> no final plan, ALERT, the next market evening re-runs with the SAME signal
day (G21).
"""
from __future__ import annotations

import datetime as dt
import json
import logging
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import audit as A
from . import ledger as L
from . import pipeline as P
from . import util
from .engine import Env, open_approval

log = logging.getLogger("momentum.evening")

NIFTY50_TOKEN = 256265          # Kite instrument token of the NIFTY 50 index (UNVERIFIED on this key: checked by T6)


class EveningError(Exception):
    pass


def kite_sessions(api: Any, frm: dt.date, to: dt.date) -> List[str]:
    rows = api.historical(NIFTY50_TOKEN, frm.isoformat(), to.isoformat(), "day")
    out = sorted({str(r["date"])[:10] for r in rows})
    if not out:
        raise EveningError("Kite returned no NIFTY 50 daily candles")
    return out


def kite_close_fn(api: Any) -> Callable[[str, str, str, str], Dict[str, float]]:
    tokens: Dict[str, int] = {}

    def closes(symbol: str, series: str, a: str, b: str) -> Dict[str, float]:
        if not tokens:
            for r in api.instruments("NSE"):
                tokens[r["tradingsymbol"]] = int(r["instrument_token"])
        ts = symbol if series == "EQ" else f"{symbol}-{series}"
        if ts not in tokens:
            raise EveningError(f"{ts} not in Kite instruments")
        rows = api.historical(tokens[ts], a, b, "day")
        return {str(r["date"])[:10]: float(r["close"]) for r in rows}
    return closes


def earmark_paise(cash_paise: int, n: int, reserve: Decimal) -> int:
    """G7: floor(cash / (N x (1 + reserve))) in whole rupees (5,00,000 / 20 / 1.003 -> 24,925)."""
    rupees = (Decimal(cash_paise) / 100 / (Decimal(n) * (1 + reserve))).to_integral_value(rounding=ROUND_FLOOR)
    return int(rupees) * 100


def decision_path(env: Env, m: str, signal_day: str, rev: int) -> Path:
    return env.paths.decisions / f"{m}_{signal_day}_r{rev}.json"


def next_session(day: dt.date, holidays: Set[str]) -> dt.date:
    """The next weekday after `day` that is not in the holiday file (= first session of the plan month)."""
    n = day + dt.timedelta(days=1)
    while n.weekday() >= 5 or n.isoformat() in holidays:
        n += dt.timedelta(days=1)
    return n


def fo_entrants(env: Env, M: P.Modules, m: str, sd: str, first_session: str, fo_today: Set[str]) -> Dict[str, Any]:
    """The evening F&O vet (fo_vet.py). Failed before `fo_vet_give_up` -> EveningError (the next 30-min run retries);
    failed at / after it -> FINAL anyway with ALERT FO_VET_FAILED (owner D47; the day-1 backstop still guards)."""
    from . import fo_vet as FV
    cfg = env.cfg
    # past the give-up time, or a catch-up run on a later day (review 11-28: the morning catch-up must reach FINAL)
    final = util.now() >= util.at_today(cfg["fo_vet_give_up"]) or util.today().isoformat() > sd
    p = Path(M.J.ANNOUNCED_FILE)
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    try:
        eq = FV.eq_symbols(Path(cfg["_nse_data"]), sd)
        fv = FV.run_check(cfg, env.paths.fo_vet, env.paths.fo_circulars, env.paths.vet, m, sd, first_session,
                          fo_today, eq, rows, final, classify=env.fo_classify_fn, do_scan=env.fo_scan_fn)
    except Exception as e:  # noqa: BLE001 - a bug in this step is a failed vet, never a lost plan (D47)
        log.exception("F&O vet step crashed")
        fv = {"status": "failed", "entrants": [], "problems": [f"{type(e).__name__}: {str(e)[:200]}"], "info": [],
              "final": final, "fresh": True, "file": None, "sha256": None, "first_session": first_session}
    if fv["fresh"]:
        env.journal.append("NOTE", text=f"F&O vet {m}: {fv['status']}; entrants {fv['entrants']}; "
                           f"problems {fv['problems'][:5]}", fo_vet_file=fv["file"] if fv["final"] else None)
        for line in fv["info"]:
            env.notify(f"F&O INFO {m}: {line}")
    if fv["status"] != "ok":
        if not fv["final"]:
            raise EveningError(f"F&O vet failed ({'; '.join(fv['problems'])[:300]}); retrying until "
                               f"{cfg['fo_vet_give_up']}, then the plan goes FINAL with an alert")
        if fv["fresh"]:
            env.alert("FO_VET_FAILED", f"{m}: {'; '.join(fv['problems'])[:300]} -> the plan goes FINAL without it "
                      "(D47); the day-1 F&O check before the buys still applies")
    if fv["fresh"] and fv["entrants"]:
        env.notify(f"F&O ENTRANTS {m}: {fv['entrants']} start F&O on {first_session}: excluded from the band "
                   "(research membership)")
    return fv


def run_signal_evening(env: Env, M: P.Modules, m: str, signal_day: dt.date, skip_fetch: bool = False,
                       first_session: Optional[str] = None) -> Dict[str, Any]:
    """Everything up to and including PLAN_FINAL. -> summary dict. Raises EveningError on a fail-closed stop."""
    cfg, paths, j = env.cfg, env.paths, env.journal
    sd = signal_day.isoformat()
    lg = L.replay(j.read())
    if any(k.startswith(f"{m}|") for k in lg.plans):
        return {"status": "already final", "plan": [k for k in lg.plans if k.startswith(f"{m}|")]}
    if any(p.leg in ("SELLING", "PENDING_BUY") and lg.open_intents(p.position_id) for p in lg.positions.values()):
        raise EveningError("a book order is still live (reconcile not done?): no decision on an unsettled book")
    # A re-run finalises the latest decision for this signal day unless its F&O inputs changed since (the owner added
    # an entrant after reading the circulars): then the decision is rebuilt as the next data revision.
    rev = 0
    revs = sorted(int(f.stem.rsplit("_r", 1)[1]) for f in paths.decisions.glob(f"{m}_{sd}_r*.json"))
    if revs:
        dp_last = decision_path(env, m, sd, revs[-1])
        text = dp_last.read_text(encoding="utf-8")
        dec_old = json.loads(text)
        if M is None or dec_old.get("announced_sha256") == P.announced_sha(M):
            return finalise(env, dec_old, text, dp_last)
        rev = revs[-1] + 1
        env.alert("PLAN_REBUILD", f"fo_announced.json changed after decision r{revs[-1]}: rebuilding as r{rev}")
    # 1. sessions (Kite) and the signal-day check (§3.3)
    prev = P.month_add(m, -1)
    frm = dt.date.fromisoformat(P.month_add(prev, -1) + "-01")
    sess = kite_sessions(env.api, frm, signal_day)
    util.write_json_atomic(paths.sessions, {"fetched_at": util.now().isoformat(), "sessions": sess})
    prev_sess = [d for d in sess if d.startswith(prev)]
    if not prev_sess or prev_sess[-1] != sd:
        raise EveningError(f"signal day {sd} is not Kite's last {prev} session ({prev_sess[-3:]})")
    # 2. NSE data (CM, CA current + next month, indices), then the F&O list of the signal day
    w = P.Window(m, signal_day, paths.derived / f"{m}_{sd}")
    fo_zip = Path(cfg["_nse_data"]) / "fo_bhav_live" / f"{sd}.zip"
    fo_all = P.fo_url_fetch(M, sd, fo_zip)
    # research membership of m = futures on m's first session: a stock whose last contract expires ON the signal day
    # (monthly expiry = signal day) is not a member of m; one starting on the first session is (the F&O vet, below)
    fo_today = P.fo_continuing(fo_zip, sd)
    leavers = sorted(fo_all - fo_today)
    if leavers and not any(r["type"] == "NOTE" and r.get("fo_leavers_of") == sd for r in j.read()):
        env.journal.append("NOTE", fo_leavers_of=sd, text=f"F&O leavers on the signal day {sd} (last expiry today, "
                           f"not members of {m}): {leavers}")
        env.notify(f"F&O INFO {m}: {leavers} leave F&O with today's expiry: not excluded (research membership)")
    members = sorted(fo_today | P.announced_additions(M, m))
    P.configure(M, w, members, sess)
    unmarked = P.unmark_sessions(M, prev_sess)
    if unmarked:
        env.journal.append("NOTE", text=f"re-requesting NSE files for Kite sessions marked missing: {unmarked}")
    if not skip_fetch:
        P.refresh(M)
    missing = [d for d in prev_sess if P.cm_status(M, d) != "ok"]
    if missing:
        raise EveningError(f"Kite sessions without an ok CM file: {missing}")
    # 2b. stocks whose F&O contracts start on m's first session (D47): research counts them as members of m
    fv = fo_entrants(env, M, m, sd, first_session or next_session(signal_day, set()).isoformat(), fo_today)
    extra = set(fv["entrants"]) - set(members)
    if extra:
        members = sorted(set(members) | extra)
        P.configure(M, w, members, sess)
    # 3. rebuild + freeze band
    times = P.rebuild(M)
    band_sha, mem_sha = P.freeze_band(M, w, paths.bands, members, rev)
    c = P.load_context(M, w)
    if not c.ca_ok:
        raise EveningError("P3-CA verdict NO: " + " | ".join(c.ca_lines[:5]))
    # 4. decide. Month 1 (nothing held, nothing pending) = decide_from_cash with the evening earmark (December spec);
    #    a live book = decide_live on the ledger (frozen design §4.1, January spec §4.1), after plan expiry (E26)
    expiring = plan_expiry(lg)
    holdings, pending = live_book(lg, expiring)
    first_month = not holdings and not pending
    if first_month:
        d = P.decide_from_cash(M, c, m, lg.cash_paise / 100, int(cfg["n_hold"]))
        d.update(sells=[], written_off=[])
        if len(d["buys"]) != int(cfg["n_hold"]):
            raise EveningError(f"{len(d['buys'])} buys, expected {cfg['n_hold']}")
    else:
        d = P.decide_live(M, c, m, holdings, pending, int(cfg["n_hold"]))
    if d["signal_day"] != sd:
        raise EveningError(f"panel signal day {d['signal_day']} != {sd}")
    # 5. audit layers 1-2 (a sell is a touched name too: a data error must not sell a name, FD §3.5)
    touched = {b["chain"] for b in d["buys"]} | {x["chain"] for x in d["sells"] if x["chain"] is not None}
    top = [r["chain"] for r in d["ranked"][: int(cfg["audit_top_n"])]]
    ca_raw = M.CA.load_raw() or []
    res = A.run(c, m, sd, touched, top, cfg, kite_close_fn(env.api), ca_raw,
                lambda ch, s: P.series_name(M, c.p, ch, s))
    util.write_json_atomic(paths.audit / f"{m}_{sd}_r{rev}.json", res.to_json())
    for f in res.flags:
        j.append("AUDIT_FLAG", flag_id=f.flag_id, isin=f.isin, symbol=f.symbol, layer=f.layer, check=f.check,
                 value=f.value, threshold=f.threshold, kite_value=f.kite_value, touched=f.touched, detail=f.detail)
    if res.status == "FAILED":
        j.append("AUDIT_RESULT", status="FAILED", problem=res.problem, counts=res.counts)
        raise EveningError(f"audit FAILED: {res.problem}")
    # 6. decision file (write-once)
    # month 1: the earmark is set tonight (empty book). Later months: the buys wait for the sells (BUY_GATED) and
    # their amount is set at the 15:06 gate from the cash the sells brought (January spec §4.2, D49)
    em = earmark_paise(lg.cash_paise, int(cfg["n_hold"]), Decimal(cfg["charges_reserve"])) if first_month else 0
    yymm = m[2:4] + m[5:7]
    s_idx = c.mk.sig(m)
    if any(p.position_id.startswith(f"{yymm}-") for p in lg.positions.values()):
        raise EveningError(f"positions {yymm}-* already exist: a plan for {m} was finalised before")
    buys = [{"position_id": f"{yymm}-{k:02d}", "isin": b["isin"], "symbol": b["symbol"], "series": b["series"],
             "rank": b["rank"], "score_milli": b["score_milli"], "chain": b["chain"], "earmark_paise": em,
             "leg": "PENDING_BUY" if first_month else "BUY_GATED",
             "signal_close_paise": L.paise(float(c.p.close[b["chain"], s_idx])),
             "ca_rows": ca_rows_ahead(ca_raw, b["symbol"], b["isin"], sd, P.month_add(m, 1) + "-01")}
            for k, b in enumerate(sorted(d["buys"], key=lambda x: x["rank"]), 1)]
    # worst rank first (unranked last): the order the 14:50 step sells in
    sells = []
    for k, x in enumerate(sorted(d["sells"], key=lambda x: (x["rank"] is None, -(x["rank"] or 0))), 1):
        pos = lg.positions[x["position_id"]]
        sells.append({"position_id": x["position_id"], "isin": pos.isin, "symbol": pos.symbol, "series": pos.series,
                      "reason": x["reason"], "rank": x["rank"], "score_milli": x["score_milli"], "chain": x["chain"],
                      "sell_no": k, "qty": pos.held_qty,
                      "signal_close_paise": L.paise(float(c.p.close[x["chain"], s_idx])) if x["chain"] is not None
                      else None})
    touched_flags: Dict[Any, List[A.Flag]] = {}        # keyed by research chain: a ledger ISIN may be older
    for f in res.flags:
        if f.touched:
            touched_flags.setdefault(f.chain, []).append(f)
    holds = []
    for b in buys:
        if b["chain"] in touched_flags:
            holds.append({"position_id": b["position_id"], "overlay": "AUDIT_HOLD", "reason": "audit flag"})
        if b["series"] != "EQ":
            holds.append({"position_id": b["position_id"], "overlay": "OWNER_HOLD", "reason": f"{b['series']} series"})
    for x in sells:
        if x["reason"] in ("NO_SCORE", "NO_CHAIN"):            # D40: only a RANK sell is automatic
            holds.append({"position_id": x["position_id"], "overlay": "AUDIT_HOLD",
                          "reason": f"sell reason {x['reason']}: sell or data fix (owner)"})
        elif x["chain"] is not None and x["chain"] in touched_flags:
            holds.append({"position_id": x["position_id"], "overlay": "AUDIT_HOLD", "reason": "audit flag"})
    for w_ in d["written_off"]:                                # D-L19: no row for > DEAD_GAP sessions
        holds.append({"position_id": w_["position_id"], "overlay": "OWNER_HOLD",
                      "reason": f"no NSE row since {w_['last_row']}: written off as tested; the shares are yours "
                                "to decide"})
    status = "PASSED_WITH_HOLDS" if holds else "PASSED"
    plan_id = {"month": m, "signal_day": sd, "data_rev": rev}
    decision = {"plan_id": plan_id, "status": "FINAL", "made_at": util.now().isoformat(),
                "code_manifest": cfg.get("_code_manifest_sha"), "band_sha256": band_sha, "members_sha256": mem_sha,
                "announced_sha256": P.announced_sha(M), "members": members,
                "fo_vet": {"status": fv["status"], "file": fv["file"], "sha256": fv["sha256"],
                           "entrants": fv["entrants"], "first_session": fv["first_session"],
                           "problems": fv["problems"][:10]},
                "kite_sessions_prev_month": prev_sess, "signal_day_verified": True, "ledger_cash_paise": lg.cash_paise,
                "audit": res.to_json(), "buys": buys, "sells": sells, "expire": expiring,
                "written_off": d["written_off"], "holds": holds, "ranked": d["ranked"], "timings": times}
    text = json.dumps(decision, indent=1, sort_keys=True) + "\n"
    dp = decision_path(env, m, sd, rev)
    util.write_once(dp, text)
    j.append("AUDIT_RESULT", status=status, problem="", counts=res.counts)
    return finalise(env, decision, text, dp)


def finalise(env: Env, decision: Dict[str, Any], text: str, dp: Path) -> Dict[str, Any]:
    """FINAL = verified signal day + audit not FAILED + (F&O stamp OR owner Approve bound to plan_id + sha) (G10)."""
    j, paths = env.journal, env.paths
    plan_id, buys, holds = decision["plan_id"], decision["buys"], decision["holds"]
    m, sha = plan_id["month"], util.sha256_text(text)
    if util.now() > util.at_today(env.cfg["plan_final"]) and util.now().date().isoformat() == plan_id["signal_day"]:
        raise EveningError(f"past {env.cfg['plan_final']}: no final plan tonight (E21)")
    fv = decision.get("fo_vet")
    # D47: a decision with an F&O vet result needs no stamp or tap (a failed vet was alerted when it failed); only a
    # decision made before D47 (no fo_vet block) or the 'approve' fallback switch keeps the old owner gate
    gate = fv is None or (fv.get("status") != "ok" and env.cfg["fo_vet_fail_action"] == "approve")
    if gate and P.fo_checked_stamp(paths.control, m) is None and not plan_approved(env, plan_id, sha):
        if not any(r["type"] == "ALERT" and r.get("code") == "NO_FO_STAMP" and m in r.get("text", "")
                   for r in j.read()):
            env.alert("NO_FO_STAMP", f"control/fo_checked.json has no stamp for {m}: plan needs the owner's Approve")
        ask_plan_approval(env, plan_id, sha, len(buys))
        return {"status": "waiting for owner approval", "decision": str(dp)}
    flags = [f for f in decision["audit"]["flags"] if f["touched"]]
    sells = decision.get("sells", [])
    lg = L.replay(j.read())
    for pid in decision.get("expire", []):     # E26: last month's never-placed buys end when this plan is final
        p = lg.positions[pid]
        if p.leg in ("BUY_GATED", "PENDING_BUY"):
            j.append("LEG", position_id=pid, reason=f"plan expiry ({m} decided)", **{"from": p.leg, "to": "EXPIRED"})
    j.append("PLAN_FINAL", plan_id=plan_id, decision_file=dp.name, decision_sha256=sha,
             audit_status=decision["audit"]["status"] if not holds else "PASSED_WITH_HOLDS",
             buys=[{k: b[k] for k in ("position_id", "isin", "symbol", "series", "rank", "earmark_paise")}
                   | {"leg": b.get("leg", "PENDING_BUY")} for b in buys],
             sells=[{k: x[k] for k in ("position_id", "reason", "sell_no", "rank", "qty")} for x in sells],
             holds=[{"position_id": h["position_id"], "overlay": h["overlay"]} for h in holds])
    by_chain = {b["chain"]: b for b in buys + sells if b.get("chain") is not None}
    by_isin = {b["isin"]: b for b in buys + sells}
    for f in flags:
        b = by_chain.get(f.get("chain")) or by_isin.get(f["isin"])
        if b is None:                           # never raise after PLAN_FINAL: the buttons would be lost
            env.alert("FLAG_UNMATCHED", f"audit flag {f['flag_id']} on {f['symbol']} matches no plan row")
            continue
        others = [x["flag_id"] for x in flags if x["isin"] == f["isin"] and x["flag_id"] != f["flag_id"]]
        open_approval(env, "flag", f["flag_id"], {"real": "Real - clear", "hold": "Hold name",
                                                  "data_error": "Data error - stop plan"},
                      f"AUDIT FLAG {f['symbol']} {f['check']}: {f['value']} (tol {f['threshold']}) {f['detail']}"
                      f"{' Kite ' + f['kite_value'] if f.get('kite_value') else ''}", position_id=b["position_id"],
                      other_open_flags=others)
    buy_ids = {b["position_id"] for b in buys}
    for h in holds:
        if h["position_id"] not in buy_ids:     # a sell or written-off hold: its own message and the 08:35 reminder
            x = next((x for x in sells if x["position_id"] == h["position_id"]), None)
            name = x["symbol"] if x else h["position_id"]
            isin = lg.positions[h["position_id"]].isin
            env.alert("PLAN_HOLD", f"{name} ({h['overlay']}): {h.get('reason') or 'held at the plan'}; reply "
                      f"'release {isin}' to let the code sell it, or leave it held")
            continue
        if h["overlay"] == "OWNER_HOLD":       # a buy held at the plan (BE/BZ series, G14) gets its Buy / Skip
            b = next(x for x in buys if x["position_id"] == h["position_id"])
            open_approval(env, "leg", b["position_id"], {"buy": "Buy", "skip": "Skip"},
                          f"HOLD {b['symbol']} (OWNER_HOLD): {h.get('reason') or 'held at the plan'}",
                          position_id=b["position_id"])
    em = buys[0]["earmark_paise"] // 100 if buys else 0
    amount = f"Rs {em:,}" if em else "the sale money (set at 15:06)"
    sold = ", ".join(f"{x['symbol']}({x['reason']}{' r' + str(x['rank']) if x['rank'] else ''})" for x in sells)
    env.notify(f"PLAN {m} r{plan_id['data_rev']} FINAL: sells {len(sells)}{': ' + sold if sells else ''}; "
               f"{len(buys)} buys x {amount}; expired {len(decision.get('expire', []))}; holds {len(holds)}; "
               f"signal day {plan_id['signal_day']} verified on Kite")
    return {"status": "FINAL", "decision": str(dp), "buys": len(buys), "sells": len(sells), "holds": len(holds),
            "flags": len(flags)}


def plan_expiry(lg: L.Ledger) -> List[str]:
    """E26 (frozen design §5.5): when the next plan is decided, every buy that was never placed ends: BUY_GATED legs
    and PENDING_BUY legs with no order ever (held for the owner, deferred, gated). A buy with an order inside its
    lock-try window stays pending (tested: pending buys carry across a rebalance). -> position ids."""
    out = []
    for p in lg.positions.values():
        if p.test:
            continue
        if p.leg == "BUY_GATED" or (p.leg == "PENDING_BUY" and not any(i.position_id == p.position_id
                                                                         for i in lg.intents.values())):
            out.append(p.position_id)
    return sorted(out)


def live_book(lg: L.Ledger, expiring: List[str]) -> Tuple[List[P.LiveHolding], List[P.LivePending]]:
    """Ledger -> decide_live input (frozen design §4.1 mapping). A partly filled buy still inside its window is ONE
    pending buy (not held); a SELLING leg is `selling`; the TEST lot is outside the book."""
    hold: List[P.LiveHolding] = []
    pend: List[P.LivePending] = []
    for p in lg.positions.values():
        if p.test or p.position_id in expiring:
            continue
        if p.leg == "PENDING_BUY":
            pend.append(P.LivePending(p.position_id, p.isin))
        elif p.leg in ("NONE", "SELLING") and p.held_qty > 0:
            hold.append(P.LiveHolding(p.position_id, p.isin, "selling" if p.leg == "SELLING" else "held",
                                      p.plan_id["signal_day"]))
    return hold, pend


def ca_rows_ahead(ca_raw: List[Dict[str, Any]], symbol: str, isin: str, after: str, before: str) -> List[Dict[str, str]]:
    """NSE CA records for the name with an ex-date after the signal day and before the next month (shown to the vet;
    an ex-date on an order day defers that buy one session by code, OQ13)."""
    out = []
    for r in ca_raw:
        if r.get("symbol") != symbol and r.get("isin") != isin:
            continue
        try:
            ex = dt.datetime.strptime(str(r.get("exDate")), "%d-%b-%Y").date().isoformat()
        except ValueError:
            continue
        if after < ex < before:
            out.append({"ex_date": ex, "purpose": str(r.get("subject", ""))[:120]})
    return sorted(out, key=lambda x: x["ex_date"])


def plan_approved(env: Env, plan_id: Dict[str, Any], sha: str) -> bool:
    recs = env.journal.read()
    ref = f"{L.plan_key(plan_id)}|{sha[:16]}"
    ids = {r["approval_id"] for r in recs if r["type"] == "APPROVAL_OPEN" and r["ref"] == ref}
    return any(r["type"] == "OWNER_TAP" and r["approval_id"] in ids and r["choice"] == "approve" for r in recs)


def ask_plan_approval(env: Env, plan_id: Dict[str, Any], sha: str, n: int) -> None:
    recs = env.journal.read()
    ref = f"{L.plan_key(plan_id)}|{sha[:16]}"
    if any(r["type"] == "APPROVAL_OPEN" and r["ref"] == ref for r in recs):
        return
    open_approval(env, "plan", ref, {"approve": "Approve", "reject": "Reject"},
                  f"NO F&O STAMP {plan_id['month']}: approve plan {L.plan_key(plan_id)} ({n} buys)?")


def book_open(env: Env) -> None:
    """The book's opening cash is written ONCE and is permanent. A live book opens only under an ARM whose session
    cap covers it (review 11-28 T1: an overlay that lost book_cash_open must not open a bigger book unseen).
    Units = cash / NAV, so the first NAV report reads nav_open."""
    if any(r["type"] == "BOOK_OPEN" for r in env.journal.read()):
        return
    rupees, nav = int(env.cfg["book_cash_open"]), int(env.cfg["nav_open"])
    if not env.dry:
        if env.arm is None:
            raise EveningError(f"no ARM: the live book (Rs {rupees:,}) is not opened")
        if rupees > env.arm.cap_rupees:
            raise EveningError(f"book_cash_open Rs {rupees:,} is above the ARM session cap Rs {env.arm.cap_rupees:,}: "
                               "the live book is not opened (check config/momentum.json)")
    if rupees % nav:
        raise EveningError(f"book_cash_open {rupees} is not a whole number of units at NAV {nav}")
    env.journal.append("BOOK_OPEN", cash_paise=rupees * 100, units=rupees // nav, nav_per_unit_paise=nav * 100,
                       config_commit=env.cfg.get("_code_manifest_sha"))


def is_signal_day(day: dt.date, holidays: Set[str]) -> bool:
    """Probable last session of the month: a weekday not in the holiday file whose next weekday non-holiday is in a new
    month. Being wrong costs nothing: the Kite check in run_signal_evening is the truth."""
    if day.weekday() >= 5 or day.isoformat() in holidays:
        return False
    n = day + dt.timedelta(days=1)
    while n.weekday() >= 5 or n.isoformat() in holidays:
        n += dt.timedelta(days=1)
    return n.month != day.month

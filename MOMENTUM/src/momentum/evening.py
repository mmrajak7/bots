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
    if any(p.state != "GONE" for p in lg.positions.values()):
        raise EveningError("the book holds or is buying names: decide_live with the ledger mapping is the January "
                           "build (frozen design §4.1); this evening refuses")
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
    # 4. decide (empty book)
    d = P.decide_from_cash(M, c, m, lg.cash_paise / 100, int(cfg["n_hold"]))
    if d["signal_day"] != sd:
        raise EveningError(f"panel signal day {d['signal_day']} != {sd}")
    if len(d["buys"]) != int(cfg["n_hold"]):
        raise EveningError(f"{len(d['buys'])} buys, expected {cfg['n_hold']}")
    # 5. audit layers 1-2
    touched = {b["chain"] for b in d["buys"]}
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
    em = earmark_paise(lg.cash_paise, int(cfg["n_hold"]), Decimal(cfg["charges_reserve"]))
    yymm = m[2:4] + m[5:7]
    s_idx = c.mk.sig(m)
    buys = [{"position_id": f"{yymm}-{k:02d}", "isin": b["isin"], "symbol": b["symbol"], "series": b["series"],
             "rank": b["rank"], "score_milli": b["score_milli"], "chain": b["chain"], "earmark_paise": em,
             "signal_close_paise": L.paise(float(c.p.close[b["chain"], s_idx])),
             "ca_rows": ca_rows_ahead(ca_raw, b["symbol"], b["isin"], sd, P.month_add(m, 1) + "-01")}
            for k, b in enumerate(sorted(d["buys"], key=lambda x: x["rank"]), 1)]
    touched_flags: Dict[str, List[A.Flag]] = {}
    for f in res.flags:
        if f.touched:
            touched_flags.setdefault(f.isin, []).append(f)
    holds = []
    for b in buys:
        if b["isin"] in touched_flags:
            holds.append({"position_id": b["position_id"], "overlay": "AUDIT_HOLD", "reason": "audit flag"})
        if b["series"] != "EQ":
            holds.append({"position_id": b["position_id"], "overlay": "OWNER_HOLD", "reason": f"{b['series']} series"})
    status = "PASSED_WITH_HOLDS" if holds else "PASSED"
    plan_id = {"month": m, "signal_day": sd, "data_rev": rev}
    decision = {"plan_id": plan_id, "status": "FINAL", "made_at": util.now().isoformat(),
                "code_manifest": cfg.get("_code_manifest_sha"), "band_sha256": band_sha, "members_sha256": mem_sha,
                "announced_sha256": P.announced_sha(M), "members": members,
                "fo_vet": {"status": fv["status"], "file": fv["file"], "sha256": fv["sha256"],
                           "entrants": fv["entrants"], "first_session": fv["first_session"],
                           "problems": fv["problems"][:10]},
                "kite_sessions_prev_month": prev_sess, "signal_day_verified": True, "ledger_cash_paise": lg.cash_paise,
                "audit": res.to_json(), "buys": buys, "holds": holds, "ranked": d["ranked"], "timings": times}
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
    j.append("PLAN_FINAL", plan_id=plan_id, decision_file=dp.name, decision_sha256=sha,
             audit_status=decision["audit"]["status"] if not holds else "PASSED_WITH_HOLDS",
             buys=[{k: b[k] for k in ("position_id", "isin", "symbol", "series", "rank", "earmark_paise")}
                   for b in buys],
             holds=[{"position_id": h["position_id"], "overlay": h["overlay"]} for h in holds])
    by_isin = {b["isin"]: b for b in buys}
    for f in flags:
        b = by_isin[f["isin"]]
        others = [x["flag_id"] for x in flags if x["isin"] == f["isin"] and x["flag_id"] != f["flag_id"]]
        open_approval(env, "flag", f["flag_id"], {"real": "Real - clear", "hold": "Hold name",
                                                  "data_error": "Data error - stop plan"},
                      f"AUDIT FLAG {f['symbol']} {f['check']}: {f['value']} (tol {f['threshold']}) {f['detail']}"
                      f"{' Kite ' + f['kite_value'] if f.get('kite_value') else ''}", position_id=b["position_id"],
                      other_open_flags=others)
    for h in holds:                             # a name held at the plan (BE/BZ series, G14) gets its Buy / Skip
        if h["overlay"] == "OWNER_HOLD":
            b = next(x for x in buys if x["position_id"] == h["position_id"])
            open_approval(env, "leg", b["position_id"], {"buy": "Buy", "skip": "Skip"},
                          f"HOLD {b['symbol']} (OWNER_HOLD): {h.get('reason') or 'held at the plan'}",
                          position_id=b["position_id"])
    em = buys[0]["earmark_paise"] // 100 if buys else 0
    env.notify(f"PLAN {m} r{plan_id['data_rev']} FINAL: {len(buys)} buys x Rs {em:,}; holds {len(holds)}; "
               f"signal day {plan_id['signal_day']} verified on Kite")
    return {"status": "FINAL", "decision": str(dp), "buys": len(buys), "holds": len(holds), "flags": len(flags)}


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

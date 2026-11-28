"""Replay test of the LIVE engine against the backtest (frozen design §4.3 F4; January spec §10 'replay test').

The live state machine -- signal evening (decide_live, PLAN_FINAL, expiry), afternoon (vet, sells, gate, buy amounts,
buys, ladder, cancel, reconcile), ledger replay -- runs session by session over a window of the rehearsal panel
against a simulated broker that fills by the TESTED rule: at the session's close price, a buy only when
`enter_ok[c, t]` and a sell only when `exit_ok[c, t]` (nonfo_momentum.py:407-409). The tested Book runs over the same
window. Compared month by month at every rebalance: the holdings / selling / pending sets the decision starts from,
and the decision itself (sells, buys).

Known, documented live deviations print as explained differences, never as failures:
  D-L19  a stopped name: research freezes it from its last row (look-ahead); live holds it until DEAD_GAP sessions
  D32    gated buys wait for the sells (at most 2 sessions); research buys the same session
  D-L11  whole shares: a name priced above its amount gets 0 shares (live) vs fractional units (research)
Run:  python -I tests/replay_sim.py 2020-01 2021-12      (default: the whole 2020-01..2026-11)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "src"))
sys.path.insert(0, str(HOME / "tests"))

from momentum import audit as A  # noqa: E402
from momentum import broker as B  # noqa: E402
from momentum import config as CF  # noqa: E402
from momentum import control as C  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import evening as EV  # noqa: E402
from momentum import journal as J  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import pipeline as P  # noqa: E402
from momentum import util  # noqa: E402
from momentum import vet as V  # noqa: E402
from momentum.fakebroker import FakeKite  # noqa: E402

NSE = (HOME / "../../NSE_DATA").resolve()
REH = NSE / "rehearsal_2026_11"


# ---------------------------------------------------------------- a fast in-memory journal (same contract)
class MemJournal(J.Journal):
    def __init__(self, path: Path) -> None:      # noqa: D107 - no file at all
        self.path, self.dry, self.recs = path, False, []

    def read(self) -> List[Dict[str, Any]]:
        return list(self.recs)

    def append(self, type_: str, **fields: Any) -> Dict[str, Any]:
        if type_ not in J.RECORD_TYPES:
            raise J.JournalError(f"unknown record type {type_}")
        rec = {"seq": len(self.recs) + 1, "ts": util.now().isoformat(timespec="seconds"), "type": type_}
        rec.update(json.loads(json.dumps(fields)))           # the same JSON round trip as the file
        self.recs.append(rec)
        return rec


# ---------------------------------------------------------------- incremental replay (the journal only grows)
_cache: Dict[str, Any] = {"n": 0, "last": None, "lg": None}
_orig_replay = L.replay


def fast_replay(recs: List[Dict[str, Any]]) -> L.Ledger:
    """== L.replay(recs) for an append-only journal: the cached prefix is replayed once, a pickle copy is handed out
    so a caller's mutation never leaks into the cache. The full replay is re-checked every 500 calls."""
    import pickle
    n = len(recs)
    fast_replay.calls = getattr(fast_replay, "calls", 0) + 1
    c = _cache
    if c["lg"] is not None and n >= c["n"] and c["n"] > 0 and recs[c["n"] - 1] is c["last"]:
        if n == c["n"]:
            return pickle.loads(c["blob"])
        lg = pickle.loads(c["blob"])
        lg = L.replay_into(lg, recs[c["n"]:])
    else:
        lg = _orig_replay(recs)
    c["n"], c["last"], c["lg"], c["blob"] = n, recs[-1] if recs else None, True, pickle.dumps(lg)
    if fast_replay.calls % 500 == 0 and lg.to_json() != _orig_replay(recs).to_json():
        raise AssertionError(f"incremental replay != full replay at {n} records")
    return lg


# ---------------------------------------------------------------- the simulated broker: tested fills
class PanelKite(FakeKite):
    """Quotes and fills from the panel at session t: LTP = the close; a name the tested rule cannot buy (locked up)
    or sell (locked down / no row) does not fill and shows its circuit at the LTP."""

    def set_session(self, mk: Any, t: int, sym_chain: Dict[str, int]) -> None:
        self.mk, self.t, self.sym_chain = mk, t, dict(sym_chain)
        self.ltp, self.upper, self.lower = {}, {}, {}
        self.orders_.clear()
        self.trades_.clear()
        for ts, ch in self.sym_chain.items():
            if not mk.p.has_row[ch, t]:
                continue                                         # no row: no quote, not listed today
            px = float(mk.p.close[ch, t])
            k = f"NSE:{ts}"
            self.ltp[k] = px
            self.upper[k] = px if not mk.enter_ok[ch, t] else round(px * 1.2, 2)
            self.lower[k] = px if not mk.exit_ok[ch, t] else round(px * 0.8, 2)

    def place(self, **kw: Any) -> str:
        oid = super().place(**kw)
        o = self._get(oid)
        ch = self.sym_chain.get(o.tradingsymbol)
        ok = ch is not None and bool((self.mk.enter_ok if o.transaction_type == "BUY" else self.mk.exit_ok)[ch, self.t])
        if ok:
            px = float(self.mk.p.close[ch, self.t])              # the tested fill: the close
            o.status, o.filled_quantity, o.average_price = "COMPLETE", o.quantity, px
            self.trades_.append({"trade_id": f"t{oid}", "order_id": oid, "quantity": o.quantity,
                                 "average_price": px, "exchange_timestamp": "sim"})
        return oid


def go_vet(step, vin, skips):  # noqa: ANN001, ANN201
    return V.Verdict(True, "", {i["intent_id"]: ("GO", "GO_OK", "") for i in vin["intents"]}, "sim")


def activity(recs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the live machine actually did: proves the hard paths were exercised, not only the easy ones."""
    from collections import Counter
    gates = Counter(r["result"] for r in recs if r["type"] == "GATE")
    fills = Counter(r.get("side", "BUY") for r in recs if r["type"] == "FILL")
    return {"buy_fills": fills["BUY"], "sell_fills": fills["SELL"], "gates": dict(gates),
            "lock_tries": sum(1 for r in recs if r["type"] == "SESSION_USED" and r.get("lock_try")),
            "sell_unfilled_sessions": sum(1 for r in recs if r["type"] == "SELL_SESSION"
                                          and str(r.get("result", "")).startswith("unfilled")),
            "skipped_buys": sum(1 for r in recs if r["type"] == "LEG" and r.get("to") == "SKIPPED"),
            "expired": sum(1 for r in recs if r["type"] == "LEG" and r.get("to") == "EXPIRED"),
            "earmark_moves": sum(1 for r in recs if r["type"] == "EARMARK_MOVE"),
            "plans": sum(1 for r in recs if r["type"] == "PLAN_FINAL")}


# ---------------------------------------------------------------- the run
def run(first: str = "2020-01", last: str = "2026-11", verbose: bool = True) -> Dict[str, Any]:
    t_start = time.time()
    M = P.import_research(HOME / "pipeline", NSE)
    c = P.load_context(M, P.Window("2026-12", dt.date(2026, 11, 23), REH))
    mk, N = c.mk, M.N
    months = N.months_in((first, last))

    # ---- the tested Book over the window, its state + decision captured at every rebalance
    tested: Dict[str, Dict[str, Any]] = {}

    class TBook(N.Book):            # type: ignore[misc, name-defined]
        def _decide(self, m: str) -> Tuple[List[int], List[int], bool]:
            s, b, o = super()._decide(m)
            tested[m] = {"held": sorted(q.c for q in self.pos.values() if q.state == "held"),
                         "selling": sorted(q.c for q in self.pos.values() if q.state == "selling"),
                         "frozen": sorted(q.c for q in self.pos.values() if q.state == "frozen"),
                         "pending": sorted(x[0] for x in self.pending_buys), "sells": sorted(s), "buys": sorted(b)}
            return s, b, o
    spec = N.Spec(start=first, end=last)
    TBook(mk, c.sc, spec, mk.G).run()

    # ---- the live machine
    tmp = Path(tempfile.mkdtemp(prefix="momsim_"))
    cfg = CF.load(HOME)
    cfg["_nse_data"] = str(tmp)
    cfg["book_cash_open"] = int(spec.account)
    cfg["arm_cap_rupees_default"] = int(spec.account * 2)
    paths = CF.paths(tmp, tmp / "state")
    paths.ensure()
    j = MemJournal(paths.journal)
    k = PanelKite()
    k.cash = 1e12                                                # the cash guard is tested elsewhere
    k.sessions = list(mk.sessions)
    k.fo_futures = ["NIFTY"] + [f"FO{x:03d}" for x in range(1, 201)]
    arm = C.Arm("SIM", tuple(months), "automatic", int(spec.account * 2), dt.date(2000, 1, 1), dt.date(2099, 1, 1))
    clock = {"t": dt.datetime(2020, 1, 1, tzinfo=util.IST)}
    util.set_clock(lambda: clock["t"])

    def until(t: dt.datetime) -> None:
        if t > clock["t"]:
            clock["t"] = t
    env = E.Env(cfg, paths, j, k, B.Broker(k, j, B.Gate(live=True, armed=lambda: True, killed=lambda: False)),
                lambda text, buttons=None: True, go_vet, until, arm=arm)
    saved = [(L, "replay", L.replay)]
    L.replay = fast_replay
    # pipeline stubs: the decision uses the REAL rehearsal context; nothing is fetched or rebuilt
    stubs = [(EV, "kite_sessions", lambda api, a, b: [x for x in mk.sessions if a.isoformat() <= x <= b.isoformat()]),
             (P, "fo_url_fetch", lambda M_, d, dest: set()), (P, "fo_continuing", lambda z, sd: set()),
             (P, "announced_additions", lambda M_, m: set()), (P, "announced_sha", lambda M_: "sim"),
             (P, "configure", lambda *a, **kw: None), (P, "unmark_sessions", lambda M_, s: []),
             (P, "refresh", lambda M_: {}), (P, "cm_status", lambda M_, d: "ok"), (P, "rebuild", lambda M_: {}),
             (P, "freeze_band", lambda *a, **kw: ("band", "mem")), (P, "load_context", lambda M_, w: c),
             (EV, "fo_entrants", lambda *a, **kw: {"status": "ok", "file": "f", "sha256": "s", "entrants": [],
                                                    "first_session": "", "problems": []}),
             (EV, "kite_close_fn", lambda api: None), (A, "run", lambda *a, **kw: A.AuditResult("PASSED")),
             (M.CA, "load_raw", lambda *a, **kw: [])]
    for obj, name, val in stubs:
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)
    live: Dict[str, Dict[str, Any]] = {}
    sym_chain: Dict[str, int] = {}
    try:
        def at(day: str, hhmm: str) -> None:
            d = dt.date.fromisoformat(day)
            h, mi = int(hhmm[:2]), int(hhmm[3:])
            clock["t"] = dt.datetime(d.year, d.month, d.day, h, mi, tzinfo=util.IST)

        at(mk.sessions[mk.sig(months[0])], "20:00")
        EV.book_open(env)
        sig_of = {mk.sessions[mk.sig(m)]: m for m in months}
        t_from, t_to = mk.sig(months[0]), mk.last(months[-1])
        for t in range(t_from, t_to + 1):
            day = mk.sessions[t]
            lg = L.replay(j.read())
            for p in lg.positions.values():                     # every book name is quotable today
                ch = P.chain_for(mk, p.isin, t)
                if ch is not None:
                    sym_chain[E.tradingsymbol(p.symbol, p.series)] = ch
            k.set_session(mk, t, sym_chain)
            if any(p.leg in ("PENDING_BUY", "BUY_GATED", "SELLING") for p in lg.positions.values()):
                at(day, "14:40")
                held = E.book_held(lg)
                k.holdings_qty = dict(held)
                E.afternoon(env, held, lambda: [])
            if day in sig_of:
                m = sig_of[day]
                at(day, "20:30")
                lg = L.replay(j.read())
                hold, pend = EV.live_book(lg, EV.plan_expiry(lg))
                state = {"held": sorted(P.chain_for(mk, h.isin, t) for h in hold if h.state == "held"),
                         "selling": sorted(P.chain_for(mk, h.isin, t) for h in hold if h.state == "selling"),
                         "pending": sorted(P.chain_for(mk, b.isin, t) for b in pend)}
                res = EV.run_signal_evening(env, M, m, dt.date.fromisoformat(day), skip_fetch=True,
                                            first_session=mk.sessions[t + 1] if t + 1 < len(mk.sessions) else day)
                fin = [r for r in j.read() if r["type"] == "PLAN_FINAL" and r["plan_id"]["month"] == m]
                dec = json.loads((paths.decisions / fin[-1]["decision_file"]).read_text(encoding="utf-8")) if fin \
                    else {}
                for b in dec.get("buys", []):
                    sym_chain[E.tradingsymbol(b["symbol"], b["series"])] = b["chain"]
                state["sells"] = sorted(x["chain"] for x in dec.get("sells", []) if x.get("chain") is not None)
                state["buys"] = sorted(b["chain"] for b in dec.get("buys", []))
                state["status"] = res.get("status")
                live[m] = state
    finally:
        for obj, name, val in reversed(saved):
            setattr(obj, name, val)
        util.set_clock(None)

    # ---- compare
    rows = []
    for m in months:
        tm, lv = tested.get(m), live.get(m)
        if tm is None or lv is None:
            rows.append({"m": m, "ok": False, "why": f"missing tested={tm is not None} live={lv is not None}"})
            continue
        same_state = (set(tm["held"]) | set(tm["frozen"]), set(tm["selling"]), set(tm["pending"])) == \
                     (set(lv["held"]), set(lv["selling"]), set(lv["pending"]))
        same_dec = (tm["sells"], tm["buys"]) == (lv["sells"], lv["buys"])
        why = ""
        if not same_state:
            why += (f" state tested held/frozen/selling/pending {tm['held']}/{tm['frozen']}/{tm['selling']}/"
                    f"{tm['pending']} live {lv['held']}/{lv['selling']}/{lv['pending']}")
        if not same_dec:
            why += f" decision tested {tm['sells']}/{tm['buys']} live {lv['sells']}/{lv['buys']}"
        rows.append({"m": m, "ok": same_state and same_dec, "why": why.strip(), "frozen": bool(tm["frozen"])})
    lg = _orig_replay(j.read())
    out = {"months": len(rows), "equal": sum(r["ok"] for r in rows), "rows": rows, "secs": round(time.time() - t_start),
           "journal": len(j.recs), "cash_rupees": lg.cash_paise // 100,
           "held_now": sum(1 for p in lg.positions.values() if p.held_qty > 0),
           "alerts": sorted({r["code"] for r in j.read() if r["type"] == "ALERT"}),
           "stats": activity(j.read())}
    if verbose:
        print(f"REPLAY {first}..{last}: {out['equal']}/{out['months']} months equal, journal {out['journal']} "
              f"records, {out['secs']}s, book cash Rs {out['cash_rupees']:,}, held {out['held_now']}")
        print("alert codes seen:", out["alerts"])
        print("activity:", out["stats"])
        for r in rows:
            if not r["ok"]:
                print(f"  {r['m']}: {r['why']}{' (tested has a frozen name: D-L19)' if r.get('frozen') else ''}")
    return out


if __name__ == "__main__":
    a = sys.argv[1:]
    run(*(a[:2] if len(a) >= 2 else []))

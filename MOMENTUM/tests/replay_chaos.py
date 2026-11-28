"""Chaos replay: the live engine over years of the rehearsal panel with random faults, checking invariants every
session (January spec §10; frozen design §4.3 G5 in bulk).

Faults (seeded): partial fills, a lost reply after the order reached the exchange, Kite read failures, Telegram
sends failing, the vet down, and a CRASH before any journal append (the process dies; the next cron run resumes the
same stage the same day). After every session:
  1. the journal replays (a replay error would brick the book)
  2. the ledger's shares == the simulated broker's shares, per trading symbol (never a lost or double fill)
  3. the broker never holds a negative quantity (never an oversell) and book cash never goes below zero
  4. no tag is used twice in one day's order book (never a duplicate order)
  5. no intent left unresolved for the owner (the code settles every crash by tag)
Months whose decision differs from the tested Book are counted (faults delay trades, so some must differ) but only
the invariants fail the run.
Run:  python -I tests/replay_chaos.py 2020-01 2022-12 7
"""
from __future__ import annotations

import random
import sys
from pathlib import Path
from typing import Any, Dict, List

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "src"))
sys.path.insert(0, str(HOME / "tests"))

import replay_sim as R  # noqa: E402
from momentum import broker as B  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import vet as V  # noqa: E402


class SimCrash(Exception):
    """The process died here."""


RATES = {"crash": 0.004, "partial": 0.15, "lost_reply": 0.05, "read_fail": 0.01, "tg_fail": 0.05, "vet_down": 0.03}


def main(first: str = "2020-01", last: str = "2022-12", seed: int = 7) -> Dict[str, Any]:
    rng = random.Random(seed)
    state = {"armed": False, "crashes": 0, "faults": {k: 0 for k in RATES}, "violations": [], "days": 0}

    def hit(kind: str) -> bool:
        if state["armed"] and rng.random() < RATES[kind]:
            state["faults"][kind] += 1
            return True
        return False

    # ---- journal: a crash before an append
    orig_append = R.MemJournal.append

    def append(self, type_, **fields):  # noqa: ANN001, ANN202
        if hit("crash"):
            state["crashes"] += 1
            raise SimCrash(type_)
        return orig_append(self, type_, **fields)
    R.MemJournal.append = append

    # ---- broker: partial fills, lost replies, read failures; it keeps its OWN holdings (the truth)
    orig_place = R.PanelKite.place
    orig_set = R.PanelKite.set_session

    def set_session(self, mk, t, sym_chain):  # noqa: ANN001, ANN202
        if not hasattr(self, "truth"):
            self.truth = {}
        orig_set(self, mk, t, sym_chain)

    def place(self, **kw):  # noqa: ANN003, ANN202
        lose = hit("lost_reply")
        oid = orig_place(self, **kw)
        o = self._get(oid)
        if o.status == "COMPLETE" and o.quantity > 1 and hit("partial"):
            q = rng.randint(1, o.quantity - 1)
            o.status, o.filled_quantity = "OPEN", q
            self.trades_[-1]["quantity"] = q
        if o.filled_quantity:
            sign = 1 if o.transaction_type == "BUY" else -1
            self.truth[o.tradingsymbol] = self.truth.get(o.tradingsymbol, 0) + sign * o.filled_quantity
        if lose:
            raise B.NetworkError("chaos: reply lost after the order reached the exchange")
        return oid

    orig_r = R.PanelKite._r

    def _r(self, name):  # noqa: ANN001, ANN202
        if name in ("orders", "trades", "quote", "margins", "holdings") and hit("read_fail"):
            raise B.NetworkError(f"chaos read failure {name}")
        return orig_r(self, name)
    R.PanelKite.place, R.PanelKite.set_session, R.PanelKite._r = place, set_session, _r

    def vet(step, vin, skips):  # noqa: ANN001, ANN202
        if hit("vet_down"):
            return V.Verdict(False, "chaos: vet down", {}, "")
        return R.go_vet(step, vin, skips)
    R.go_vet_chaos = vet

    # ---- the day driver: every stage call is retried after a crash, like the next cron slot
    orig_afternoon, orig_evening = E.afternoon, R.EV.run_signal_evening

    def retried(fn, *a, **kw):  # noqa: ANN001, ANN202
        for _ in range(12):
            try:
                return fn(*a, **kw)
            except SimCrash:
                continue
            except B.NetworkError:
                continue                                    # a read failure that escaped: the next slot retries
        state["armed"] = False                              # the box comes back healthy
        try:
            return fn(*a, **kw)
        finally:
            state["armed"] = True

    def afternoon(env, held, pre):  # noqa: ANN001, ANN202
        env.notify = lambda text, buttons=None: not hit("tg_fail")
        env.vet_fn = vet
        state["armed"] = True
        try:
            retried(orig_afternoon, env, held, pre)
            # the cron's resume slots (every 5 min to 15:55, 16:10, 16:40): a stage that gave up on a Kite outage
            # (the reconcile) is retried, as on the Pi
            day = R.util.today().isoformat()
            for hhmm in ["15:40", "15:45", "15:50", "15:55", "16:10", "16:40"]:
                if E.stage_done(env.journal.read(), day, "RECON"):
                    break
                env.sleep_until(R.util.at_today(hhmm))
                retried(orig_afternoon, env, held, pre)
        finally:
            check(env)
            state["armed"] = False

    def evening(env, *a, **kw):  # noqa: ANN001, ANN202
        state["armed"] = True
        try:
            return retried(orig_evening, env, *a, **kw)
        finally:
            state["armed"] = False

    def check(env) -> None:  # noqa: ANN001
        state["days"] += 1
        recs = env.journal.recs
        try:
            lg = R._orig_replay(recs)
        except L.LedgerError as e:
            state["violations"].append(f"replay raised: {e}")
            return
        mine: Dict[str, int] = {}
        for p in lg.positions.values():
            if p.held_qty:
                ts = E.tradingsymbol(p.symbol, p.series)
                mine[ts] = mine.get(ts, 0) + p.held_qty
        truth = {k: v for k, v in getattr(env.api, "truth", {}).items() if v}
        if mine != truth:
            diff = {k: (mine.get(k, 0), truth.get(k, 0)) for k in set(mine) | set(truth)
                    if mine.get(k, 0) != truth.get(k, 0)}
            state["violations"].append(f"{recs[-1]['ts'][:10]} ledger != broker {diff}")
            env.api.truth = dict(mine) | {k: v for k, v in truth.items() if k not in mine}   # report once
        if any(v < 0 for v in truth.values()):
            state["violations"].append(f"{recs[-1]['ts'][:10]} broker short {truth}")
        if lg.cash_paise < 0:
            state["violations"].append(f"{recs[-1]['ts'][:10]} book cash {lg.cash_paise}")
        tags = [o.tag for o in env.api.orders_]
        if len(tags) != len(set(tags)):
            state["violations"].append(f"{recs[-1]['ts'][:10]} duplicate tags {sorted(tags)}")
        if lg.unresolved:
            state["violations"].append(f"{recs[-1]['ts'][:10]} unresolved {lg.unresolved}")
            for iid in list(lg.unresolved):                 # the owner answers from the broker so the run goes on
                i = lg.intents[iid]
                env.journal.recs.append({"seq": len(recs) + 1, "ts": recs[-1]["ts"], "type": "ORPHAN",
                                         "intent_id": iid, "resolution": "ABSENT", "order_id": None, "reads": []})
    E.afternoon, R.EV.run_signal_evening = afternoon, evening
    try:
        out = R.run(first, last, verbose=False)
    finally:
        E.afternoon, R.EV.run_signal_evening = orig_afternoon, orig_evening
        R.MemJournal.append = orig_append
        R.PanelKite.place, R.PanelKite.set_session, R.PanelKite._r = orig_place, orig_set, orig_r
    print(f"CHAOS {first}..{last} seed {seed}: {state['days']} order days, crashes {state['crashes']}, faults "
          f"{state['faults']}")
    print(f"  decisions equal to the tested Book: {out['equal']}/{out['months']} (faults delay trades); "
          f"activity {out['stats']}")
    print(f"  INVARIANT VIOLATIONS: {len(state['violations'])}")
    for v in state["violations"][:25]:
        print("   ", v)
    return {"violations": state["violations"], "out": out, "state": state}


if __name__ == "__main__":
    a = sys.argv[1:]
    main(*(a[:2] if len(a) >= 2 else []), *([int(a[2])] if len(a) >= 3 else []))

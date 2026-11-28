"""The signal-day evening on a HELD book (January spec §4.1, §4.2, E26), on the real rehearsal data.

Only the network / build steps are stubbed (Kite sessions, NSE fetch, F&O lists, panel rebuild, band freeze, audit);
the decision (decide_live on the rehearsal panel), the decision rows, the holds, plan expiry and finalise run for
real, and the journal they write is replayed by the real ledger."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "src"))
sys.path.insert(0, str(HOME / "tests"))

from momentum import audit as A  # noqa: E402
from momentum import broker as B  # noqa: E402
from momentum import config as CF  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import evening as EV  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import pipeline as P  # noqa: E402
from momentum import util  # noqa: E402
from momentum.fakebroker import FakeKite  # noqa: E402
from momentum.journal import Journal  # noqa: E402

NSE = (HOME / "../../NSE_DATA").resolve()
REH = NSE / "rehearsal_2026_11"
M_PLAN, SD = "2026-12", dt.date(2026, 11, 23)
NOV = {"month": "2026-11", "signal_day": "2026-10-30", "data_rev": 0}


@unittest.skipUnless((REH / "panel.npz").exists(), "rehearsal data not on this machine")
class TestHeldBookEvening(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.M = P.import_research(HOME / "pipeline", NSE)
        cls.c = P.load_context(cls.M, P.Window(M_PLAN, SD, REH))
        d = P.decide_from_cash(cls.M, cls.c, M_PLAN, 1.0, 20)
        cls.top = d["buys"]
        cls.ranked = d["ranked"]
        s = cls.c.mk.sig(M_PLAN)
        cls.isin_of = {r["chain"]: cls.c.p.isins[int(cls.c.p.isin_idx[r["chain"], s])] for r in cls.ranked}
        cls.nov_sessions = [x for x in cls.c.mk.sessions if x.startswith("2026-11")]

    def setUp(self) -> None:
        self.saved: List[Any] = []
        util.set_clock(lambda: dt.datetime(2026, 11, 23, 20, 30, tzinfo=util.IST))
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = CF.load(HOME)
        self.cfg["_nse_data"] = str(self.tmp)
        self.paths = CF.paths(self.tmp, self.tmp / "state")
        self.paths.ensure()
        self.j = Journal(self.paths.journal)
        self.sent: List[str] = []
        self.stub()

    def tearDown(self) -> None:
        for obj, name, val in reversed(self.saved):
            setattr(obj, name, val)
        util.set_clock(None)

    def patch(self, obj: Any, name: str, val: Any) -> None:
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, val)

    def stub(self) -> None:
        c = self.c
        self.patch(EV, "kite_sessions", lambda api, a, b: ["2026-10-30"] + self.nov_sessions)
        self.patch(P, "fo_url_fetch", lambda M, d, dest: set())
        self.patch(P, "fo_continuing", lambda z, sd: set())
        self.patch(P, "announced_additions", lambda M, m: set())
        self.patch(P, "announced_sha", lambda M: "sha-x")
        self.patch(P, "configure", lambda *a, **k: None)
        self.patch(P, "unmark_sessions", lambda M, s: [])
        self.patch(P, "refresh", lambda M: {})
        self.patch(P, "cm_status", lambda M, d: "ok")
        self.patch(P, "rebuild", lambda M: {})
        self.patch(P, "freeze_band", lambda *a, **k: ("band", "mem"))
        self.patch(P, "load_context", lambda M, w: c)
        self.patch(EV, "fo_entrants", lambda *a, **k: {"status": "ok", "file": "f", "sha256": "s", "entrants": [],
                                                        "first_session": "2026-11-24", "problems": []})
        self.patch(EV, "kite_close_fn", lambda api: None)
        self.patch(A, "run", lambda *a, **k: A.AuditResult("PASSED"))
        self.patch(self.M.CA, "load_raw", lambda *a, **k: [])

    def env(self) -> E.Env:
        k = FakeKite()

        def notify(text, buttons=None):  # noqa: ANN001
            self.sent.append(text)
            return True
        return E.Env(self.cfg, self.paths, self.j, k, B.Broker(k, self.j, B.Gate(live=True, armed=lambda: True,
                                                                                    killed=lambda: False)),
                     notify, lambda *a: None, lambda t: None)

    def november_book(self, chains: List[int], pending: List[int] = ()) -> None:
        """A planted November plan: `chains` bought (10 shares each), `pending` never placed."""
        self.j.append("BOOK_OPEN", cash_paise=20_000_000, units=2000, nav_per_unit_paise=10000, config_commit="t")
        rows = [{"position_id": f"2611-{k:02d}", "isin": self.isin_of[ch], "symbol": f"N{ch}", "series": "EQ",
                 "rank": k, "earmark_paise": 990_000} for k, ch in enumerate(list(chains) + list(pending), 1)]
        self.j.append("PLAN_FINAL", plan_id=NOV, decision_file="n", decision_sha256="x", audit_status="PASSED",
                      holds=[], buys=rows)
        for r in rows[:len(chains)]:
            self.j.append("FILL", position_id=r["position_id"], order_id="o" + r["position_id"],
                          trade_id="t" + r["position_id"], qty=10, price_paise=90_000, charges_paise=100,
                          exch_ts="x")
            self.j.append("LEG", position_id=r["position_id"], reason="filled", **{"from": "PENDING_BUY", "to": "NONE"})

    def run_evening(self) -> Dict[str, Any]:
        return EV.run_signal_evening(self.env(), self.M, M_PLAN, SD, skip_fetch=True, first_session="2026-11-24")

    def final(self) -> Dict[str, Any]:
        return [r for r in self.j.read() if r["type"] == "PLAN_FINAL"][-1]

    def test_three_low_ranks_are_sold_and_three_buys_wait_for_the_sale_money(self) -> None:
        top = [b["chain"] for b in self.top]
        low = [self.ranked[60]["chain"], self.ranked[70]["chain"], self.ranked[90]["chain"]]
        self.november_book(top[:17] + low)
        res = self.run_evening()
        self.assertEqual((res["status"], res["sells"], res["buys"]), ("FINAL", 3, 3))
        f = self.final()
        self.assertEqual(sorted(x["position_id"] for x in f["sells"]), ["2611-18", "2611-19", "2611-20"])
        self.assertEqual([x["rank"] for x in f["sells"]], [91, 71, 61])          # worst rank sells first
        self.assertEqual({x["reason"] for x in f["sells"]}, {"RANK"})
        self.assertEqual({(b["leg"], b["earmark_paise"]) for b in f["buys"]}, {("BUY_GATED", 0)})
        self.assertEqual(sorted(b["isin"] for b in f["buys"]), sorted(self.isin_of[ch] for ch in top[17:]))
        lg = L.replay(self.j.read())
        self.assertEqual(sorted(p.position_id for p in lg.positions.values() if p.leg == "SELLING"),
                         ["2611-18", "2611-19", "2611-20"])
        self.assertEqual(lg.earmarked_paise(), 0)
        dec = json.loads((self.paths.decisions / f["decision_file"]).read_text(encoding="utf-8"))
        self.assertEqual([x["qty"] for x in dec["sells"]], [10, 10, 10])
        self.assertTrue(any("sells 3" in t and "sale money" in t for t in self.sent))
        # a second run that evening changes nothing
        self.assertEqual(self.run_evening()["status"], "already final")

    def test_never_placed_buy_expires_and_its_slot_is_refilled(self) -> None:
        top = [b["chain"] for b in self.top]
        self.november_book(top[:19], pending=[top[19]])
        self.run_evening()
        lg = L.replay(self.j.read())
        self.assertEqual(lg.positions["2611-20"].leg, "EXPIRED")
        f = self.final()
        self.assertEqual(f["sells"], [])
        self.assertEqual([b["isin"] for b in f["buys"]], [self.isin_of[top[19]]])   # the best name not held

    def test_placed_pending_buy_stays_and_keeps_its_slot(self) -> None:
        top = [b["chain"] for b in self.top]
        self.november_book(top[:19], pending=[top[19]])
        self.j.append("INTENT", intent_id="x|2611-20|buy|1", key={"plan_id": NOV, "position_id": "2611-20",
                                                                   "leg": "buy", "attempt": 1},
                      tag="M2611B20A1", isin=self.isin_of[top[19]], tradingsymbol="N", side="BUY", qty=1,
                      limit_paise=1)
        self.j.append("RESULT", intent_id="x|2611-20|buy|1", outcome="REJECTED_OTHER", order_id=None, message="m")
        self.run_evening()
        lg = L.replay(self.j.read())
        self.assertEqual(lg.positions["2611-20"].leg, "PENDING_BUY")
        f = self.final()
        self.assertEqual((f["sells"], f["buys"]), ([], []))

    def test_no_score_sell_waits_for_the_owner(self) -> None:
        top = [b["chain"] for b in self.top]
        sc, s = self.c.sc, self.c.mk.sig(M_PLAN)
        p = self.c.p
        scored = {r["chain"] for r in self.ranked}
        ns = next(ch for ch in range(p.C) if p.has_row[ch, s - 21:s + 1].all() and ch not in scored
                  and sc.score("BLEND", ch, M_PLAN, "E") is None)
        self.isin_of[ns] = self.c.mk.isin_at(ns, s)
        self.november_book(top[:19] + [ns])
        self.run_evening()
        f = self.final()
        self.assertEqual([(x["position_id"], x["reason"]) for x in f["sells"]], [("2611-20", "NO_SCORE")])
        lg = L.replay(self.j.read())
        self.assertIn("AUDIT_HOLD", lg.positions["2611-20"].overlays)
        self.assertTrue(any("PLAN_HOLD" in t and "release " + self.isin_of[ns] in t for t in self.sent))

    def test_live_order_at_the_evening_refuses(self) -> None:
        top = [b["chain"] for b in self.top]
        self.november_book(top[:19], pending=[top[19]])
        self.j.append("INTENT", intent_id="x|2611-20|buy|1", key={"plan_id": NOV, "position_id": "2611-20",
                                                                   "leg": "buy", "attempt": 1},
                      tag="M2611B20A1", isin="I", tradingsymbol="N", side="BUY", qty=1, limit_paise=1)
        with self.assertRaises(EV.EveningError):
            self.run_evening()


if __name__ == "__main__":
    unittest.main()

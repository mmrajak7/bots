"""TEST lot end to end on the fake broker (D51, January spec §9): 9 Dec buy 2, 14 Dec sell 1, 17 Dec sell 1, through
the real afternoon process; the book's cash is never touched."""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from momentum import engine as E  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import testlot as T  # noqa: E402
from test_engine import Harness  # noqa: E402
from test_sells import fill_all  # noqa: E402


class TestLot(Harness):
    prices = {1: 2000.0, 2: 500.0}

    def setUp(self) -> None:
        super().setUp()
        self.run_day(fill=fill_all(self.k))                  # December buys done on 1 Dec
        names = [{"symbol": f"B{k}", "isin": f"INEB{k:08d}", "rank": k} for k in range(1, 151)]
        (self.paths.bands / "b150_2026-12_r0.json").write_text(json.dumps({"month": "2026-12", "names": names}),
                                                               encoding="utf-8")
        for k in range(1, 151):
            self.k.ltp[f"NSE:B{k}"] = 100.0 if k < 50 else 640.0   # ranks 38..49 too cheap: B50 is picked
        self.book_cash = L.replay(self.j.read()).cash_paise

    def holdings(self) -> dict:
        lg = L.replay(self.j.read())
        return {p.isin: p.held_qty for p in lg.positions.values() if p.held_qty}

    def session(self, d: dt.date, fill=None) -> L.Ledger:  # noqa: ANN001
        self.new_day(d)
        env = self.env()
        T.run(env, self.holdings())
        self.k.on_place = fill
        E.afternoon(self.env(), self.holdings(), lambda: [])
        self.k.on_place = None
        return L.replay(self.j.read())

    def test_buy_then_two_one_share_sells(self) -> None:
        lg = self.session(dt.date(2026, 12, 9), fill=fill_all(self.k))
        t = lg.positions["TEST-01"]
        self.assertEqual((t.symbol, t.held_qty, t.leg, t.test), ("B50", 2, "NONE", True))
        self.assertEqual(lg.cash_paise, self.book_cash)               # the book's cash is untouched
        self.assertLess(lg.test_cash_paise, 0)
        buy = [o for o in self.k.orders_ if o.tradingsymbol == "B50"][0]
        self.assertEqual((buy.tag, buy.quantity), ("M2612X01A1", 2))
        self.assertEqual(E.current_plan(lg)["plan_id"]["month"], "2026-12")     # TEST is never the current plan
        lg = self.session(dt.date(2026, 12, 14), fill=fill_all(self.k))
        t = lg.positions["TEST-01"]
        self.assertEqual((t.held_qty, t.leg), (1, "NONE"))
        sell = [o for o in self.k.orders_ if o.transaction_type == "SELL"][0]
        self.assertEqual((sell.tag, sell.quantity), ("M2612X02A1", 1))
        notes = [r for r in self.j.read() if r["type"] == "NOTE" and r.get("test_result") == "2026-12-14"]
        self.assertTrue(notes and notes[0]["text"].startswith("TEST SELL PASS"))
        self.assertIn("margins at the gate {", notes[0]["text"])
        lg = self.session(dt.date(2026, 12, 17), fill=fill_all(self.k))
        t = lg.positions["TEST-01"]
        self.assertEqual((t.held_qty, t.leg, t.state), (0, "CLOSED", "CLOSED"))
        self.assertEqual([o.tag for o in self.k.orders_ if o.transaction_type == "SELL"], ["M2612X03A1"])
        self.assertEqual(lg.cash_paise, self.book_cash)

    def test_a_rise_by_1510_still_buys_exactly_qty(self) -> None:
        # review 11-28 T5: plan at 14:35 at 640, the price is 5% higher by 15:10: still 2 shares, never 3
        self.new_day(dt.date(2026, 12, 9))
        T.run(self.env(), self.holdings())
        self.k.ltp["NSE:B50"] = 672.0
        self.k.on_place = fill_all(self.k)
        E.afternoon(self.env(), self.holdings(), lambda: [])
        self.k.on_place = None
        buy = [o for o in self.k.orders_ if o.tradingsymbol == "B50"][0]
        self.assertEqual(buy.quantity, 2)
        self.k.ltp["NSE:B50"] = 640.0

    def test_unfilled_test_sell_retries_next_session_by_itself(self) -> None:
        self.session(dt.date(2026, 12, 9), fill=fill_all(self.k))
        self.session(dt.date(2026, 12, 14), fill=None)                    # nothing fills
        lg = self.session(dt.date(2026, 12, 15), fill=fill_all(self.k))   # not a TEST day: the leg retries anyway
        self.assertEqual([o.tag for o in self.k.orders_ if o.transaction_type == "SELL"], ["M2612X02A2"])
        self.assertEqual(lg.positions["TEST-01"].held_qty, 1)

    def test_not_a_test_day_writes_nothing(self) -> None:
        self.new_day(dt.date(2026, 12, 10))
        self.assertEqual(T.run(self.env(), {}), "not a TEST day")

    def test_rerun_on_the_buy_day_writes_one_plan(self) -> None:
        self.new_day(dt.date(2026, 12, 9))
        T.run(self.env(), self.holdings())
        self.assertEqual(T.run(self.env(), self.holdings()), "TEST plan for today already written")


if __name__ == "__main__":
    unittest.main()

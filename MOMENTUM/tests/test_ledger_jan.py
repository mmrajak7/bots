"""Ledger records of the January slice (January spec §3): sells ordered by a plan, sell fills, CLOSED, gated buys and
their amount, split / bonus credits, the TEST lot's own cash, and the invariants that stop a wrong book."""
import datetime as dt
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from momentum import ledger as L  # noqa: E402
from momentum import util  # noqa: E402
from momentum.journal import Journal  # noqa: E402

DEC = {"month": "2026-12", "signal_day": "2026-11-30", "data_rev": 0}
JAN = {"month": "2027-01", "signal_day": "2026-12-31", "data_rev": 0}
TEST = {"month": "TEST", "signal_day": "2026-12-09", "data_rev": 0}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        util.set_clock(lambda: dt.datetime(2026, 12, 1, 15, 10, tzinfo=util.IST))
        self.j = Journal(Path(tempfile.mkdtemp()) / "journal.jsonl")
        self.j.append("BOOK_OPEN", cash_paise=20_000_000, units=2000, nav_per_unit_paise=10000, config_commit="t")
        self.j.append("PLAN_FINAL", plan_id=DEC, decision_file="d", decision_sha256="x", audit_status="PASSED",
                      holds=[], buys=[{"position_id": f"2612-{k:02d}", "isin": f"INE{k:09d}", "symbol": f"S{k}",
                                       "series": "EQ", "rank": k, "earmark_paise": 997_000} for k in (1, 2)])
        for k in (1, 2):                            # 10 shares at Rs 900 each, charges Rs 10
            self.j.append("FILL", position_id=f"2612-{k:02d}", order_id=f"o{k}", trade_id=f"t{k}", qty=10,
                          price_paise=90_000, charges_paise=1_000, exch_ts="x")
            self.j.append("LEG", position_id=f"2612-{k:02d}", reason="filled", **{"from": "PENDING_BUY", "to": "NONE"})

    def tearDown(self) -> None:
        util.set_clock(None)

    def jan_plan(self, sells=None, buys=None, holds=()) -> None:
        self.j.append("PLAN_FINAL", plan_id=JAN, decision_file="j", decision_sha256="y", audit_status="PASSED",
                      holds=list(holds), sells=sells if sells is not None else
                      [{"position_id": "2612-02", "reason": "RANK", "sell_no": 1, "rank": 55}],
                      buys=buys if buys is not None else
                      [{"position_id": "2701-01", "isin": "INE900000001", "symbol": "NEW", "series": "EQ",
                        "rank": 3, "earmark_paise": 0, "leg": "BUY_GATED"}])

    def lg(self) -> L.Ledger:
        return L.replay(self.j.read())


class TestSells(Base):
    def test_december_book(self) -> None:
        lg = self.lg()
        self.assertEqual(lg.cash_paise, 20_000_000 - 2 * (900_000 + 1_000))
        self.assertEqual([p.held_qty for p in lg.positions.values()], [10, 10])

    def test_plan_orders_a_sell_and_a_gated_buy(self) -> None:
        self.jan_plan()
        lg = self.lg()
        p = lg.positions["2612-02"]
        self.assertEqual((p.leg, p.sell_reason, p.sell_no, p.state), ("SELLING", "RANK", 1, "HELD"))
        n = lg.positions["2701-01"]
        self.assertEqual((n.leg, n.state, n.earmark_paise), ("BUY_GATED", "PLANNED", 0))
        self.assertEqual(lg.earmarked_paise(), 0)            # a gated buy holds no cash until the gate

    def test_partial_then_full_sell_closes_and_books_cash(self) -> None:
        self.jan_plan()
        c0 = self.lg().cash_paise
        self.j.append("FILL", side="SELL", position_id="2612-02", order_id="s1", trade_id="ts1", qty=4,
                      price_paise=95_000, charges_paise=500, exch_ts="x")
        p = self.lg().positions["2612-02"]
        self.assertEqual((p.held_qty, p.leg, p.state), (6, "SELLING", "HELD"))
        self.j.append("FILL", side="SELL", position_id="2612-02", order_id="s1", trade_id="ts2", qty=6,
                      price_paise=95_000, charges_paise=700, exch_ts="x")
        lg = self.lg()
        p = lg.positions["2612-02"]
        self.assertEqual((p.held_qty, p.leg, p.state), (0, "CLOSED", "CLOSED"))
        self.assertEqual(lg.cash_paise, c0 + 10 * 95_000 - 1_200)
        self.assertEqual((p.sold_qty, p.sold_paise, p.sell_charges_paise), (10, 950_000, 1_200))

    def test_oversell_breaks_the_replay(self) -> None:
        self.jan_plan()
        self.j.append("FILL", side="SELL", position_id="2612-02", order_id="s1", trade_id="ts1", qty=11,
                      price_paise=95_000, charges_paise=0, exch_ts="x")
        with self.assertRaises(L.LedgerError):
            self.lg()

    def test_sell_of_a_name_not_held_breaks_the_replay(self) -> None:
        with self.assertRaises(L.LedgerError):
            self.jan_plan(sells=[{"position_id": "2612-09", "reason": "RANK", "sell_no": 1}])
            self.lg()

    def test_sell_with_an_unknown_reason_breaks_the_replay(self) -> None:
        self.jan_plan(sells=[{"position_id": "2612-01", "reason": "OPINION", "sell_no": 1}])
        with self.assertRaises(L.LedgerError):
            self.lg()

    def test_sell_session_counted_once(self) -> None:
        self.jan_plan()
        self.j.append("SELL_SESSION", position_id="2612-02", date="2027-01-01", result="unfilled")
        self.assertEqual(self.lg().positions["2612-02"].sell_sessions, ["2027-01-01"])
        self.j.append("SELL_SESSION", position_id="2612-02", date="2027-01-01", result="unfilled")
        with self.assertRaises(L.LedgerError):
            self.lg()


class TestGatedBuys(Base):
    def test_buy_amount_opens_the_leg(self) -> None:
        self.jan_plan()
        self.j.append("BUY_AMOUNT", position_id="2701-01", earmark_paise=1_000_000, date="2027-01-01", basis="gate")
        n = self.lg().positions["2701-01"]
        self.assertEqual((n.leg, n.earmark_paise, n.remaining_paise), ("PENDING_BUY", 1_000_000, 1_000_000))
        self.assertEqual(self.lg().earmarked_paise(), 1_000_000)

    def test_buy_amount_twice_breaks_the_replay(self) -> None:
        self.jan_plan()
        for _ in range(2):
            self.j.append("BUY_AMOUNT", position_id="2701-01", earmark_paise=1, date="2027-01-01", basis="gate")
        with self.assertRaises(L.LedgerError):
            self.lg()

    def test_gated_buy_expires(self) -> None:
        self.jan_plan()
        self.j.append("LEG", position_id="2701-01", reason="plan expiry", **{"from": "BUY_GATED", "to": "EXPIRED"})
        self.assertEqual(self.lg().positions["2701-01"].state, "GONE")

    def test_new_buy_with_a_bad_leg_breaks_the_replay(self) -> None:
        self.jan_plan(buys=[{"position_id": "2701-01", "isin": "I", "symbol": "N", "series": "EQ", "rank": 1,
                             "earmark_paise": 0, "leg": "SELLING"}])
        with self.assertRaises(L.LedgerError):
            self.lg()


class TestCorporateActions(Base):
    def test_bonus_credit_raises_held_quantity_and_the_sell_can_sell_it(self) -> None:
        self.j.append("CA_CREDIT", position_id="2612-01", ca_id="bonus 1:1", factor_num=2, factor_den=1,
                      qty_before=10, qty_after=20)
        p = self.lg().positions["2612-01"]
        self.assertEqual((p.held_qty, p.filled_qty), (20, 10))
        self.jan_plan(sells=[{"position_id": "2612-01", "reason": "RANK", "sell_no": 1}], buys=[])
        self.j.append("FILL", side="SELL", position_id="2612-01", order_id="s", trade_id="ts", qty=20,
                      price_paise=45_000, charges_paise=0, exch_ts="x")
        self.assertEqual(self.lg().positions["2612-01"].leg, "CLOSED")

    def test_credit_from_a_wrong_base_breaks_the_replay(self) -> None:
        self.j.append("CA_CREDIT", position_id="2612-01", ca_id="split", factor_num=5, factor_den=1,
                      qty_before=9, qty_after=45)
        with self.assertRaises(L.LedgerError):
            self.lg()


class TestTestLot(Base):
    def test_test_lot_never_touches_book_cash(self) -> None:
        c0 = self.lg().cash_paise
        self.j.append("PLAN_FINAL", plan_id=TEST, decision_file="t", decision_sha256="z", audit_status="PASSED",
                      holds=[], buys=[{"position_id": "TEST-01", "isin": "INE7", "symbol": "TST", "series": "EQ",
                                       "rank": 0, "earmark_paise": 300_000}])
        self.j.append("FILL", position_id="TEST-01", order_id="x1", trade_id="tx1", qty=2, price_paise=100_000,
                      charges_paise=100, exch_ts="x")
        lg = self.lg()
        self.assertEqual(lg.cash_paise, c0)
        self.assertEqual(lg.test_cash_paise, -200_100)
        self.assertTrue(lg.positions["TEST-01"].test)


if __name__ == "__main__":
    unittest.main()

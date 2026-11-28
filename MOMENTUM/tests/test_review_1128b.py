"""Second three-way review of 11-28 (trader / architect / flow), the findings that are not gate tests (those are in
test_sells.TestReview1128Gate):
  F2  a split / bonus credit booked after the opening snapshot moves the snapshot with it (no false hold / three-way)
  F3  a hold written by PLAN_FINAL (no OVERLAY record) is reminded each morning
  F4  an ISIN-changing split in transit (Kite shows neither ISIN) waits instead of an owner mismatch; `ca` can move
      the book to a new ISIN
"""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "src"))
sys.path.insert(0, str(HOME / "tests"))

from momentum import corpact as CA  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import util  # noqa: E402
import test_corpact as TC  # noqa: E402  (modules, not classes: the harnesses' own tests are not run twice)
import test_engine as TE  # noqa: E402
import test_sells as TS  # noqa: E402

M, rec, fill_all = TC.M, TC.rec, TS.fill_all

PLAN2 = {"month": "2026-12", "signal_day": "2026-11-30", "data_rev": 1}
SPLIT = "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"


class TestF2CreditAfterTheSnapshot(TC.CAHarness):
    def test_three_way_accepts_a_credit_booked_in_the_afternoon(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        self.new_day(dt.date(2026, 12, 16))
        opening = {"INE000000001": 12, "INE000000002": 50}       # 08:35: the credit does not show yet
        CA.credit_check(self.env(), self.rows())
        CA.credit_check(self.env(), self.rows(SYM2=100))          # 14:40: booked
        self.assertEqual(L.replay(self.j.read()).positions["2612-02"].held_qty, 100)
        self.k.holdings_qty.update({"INE000000001": 12, "INE000000002": 100})
        self.assertEqual(E.three_way(self.env(), opening, "2026-12-16"), [])

    def test_a_snapshot_that_already_shows_the_credit_is_not_moved_twice(self) -> None:
        recs = [{"seq": 1, "ts": "2026-12-16T08:35:00", "type": "HOLDINGS_SNAPSHOT"},
                {"seq": 2, "ts": "2026-12-16T08:35:01", "type": "CA_CREDIT", "position_id": "x",
                 "isin_before": "A", "qty_before": 50, "qty_after": 100, "new_isin": None}]
        self.assertEqual(E.with_todays_credits(recs, {"A": 100}, "2026-12-16"), {"A": 100})
        self.assertEqual(E.with_todays_credits(recs, {"A": 50}, "2026-12-16"), {"A": 100})
        recs[1]["new_isin"] = "B"
        self.assertEqual(E.with_todays_credits(recs, {"A": 50}, "2026-12-16"), {"A": 0, "B": 100})
        self.assertEqual(E.with_todays_credits(recs, {"B": 100}, "2026-12-16"), {"B": 100})
        self.assertEqual(E.with_todays_credits(recs, {"C": 7}, "2026-12-16"), {"A": 0, "B": 100, "C": 7})  # transit
        self.assertEqual(E.with_todays_credits(recs, {"A": 50}, "2026-12-17"), {"A": 50})   # another day


class TestF2SellAfterTheCredit(TS.SellHarness):
    def test_the_credited_sell_goes_with_all_its_shares(self) -> None:
        div = rec("INE999999999", "OTHER", "2027-01-05", "Dividend - Rs 2 Per Share")
        body = json.dumps([div, rec("INE000000002", "SYM2", "2026-12-30", "Bonus 1:1")]).encode()
        CA.scan(self.env(), M.CA, get=lambda u: (200, body, ""))
        CA.activate_due(self.env())
        rows = [{"isin": "INE000000001", "tradingsymbol": "SYM1", "quantity": 12, "t1_quantity": 0},
                {"isin": "INE000000002", "tradingsymbol": "SYM2", "quantity": 100, "t1_quantity": 0}]
        CA.credit_check(self.env(), rows)
        lg = self.afternoon(fill=fill_all(self.k))                # the afternoon still gets the 08:35 snapshot (50)
        sells = [(o.tag, o.quantity) for o in self.k.orders_ if o.transaction_type == "SELL"]
        self.assertEqual(sells, [("M2701S01A1", 100)])
        self.assertNotIn("OWNER_HOLD", lg.positions["2612-02"].overlays)


class TestF3PlanHoldReminder(TE.Harness):
    def test_a_plan_time_hold_is_reminded(self) -> None:
        buys = [{"position_id": "2612-09", "isin": "INE000000009", "symbol": "SYM9", "series": "BE", "rank": 9,
                 "earmark_paise": 1_000_000, "signal_close_paise": 100_000, "ca_rows": []}]
        text = json.dumps({"buys": buys}) + "\n"
        (self.paths.decisions / "d2.json").write_text(text, encoding="utf-8", newline="\n")
        self.j.append("PLAN_FINAL", plan_id=PLAN2, decision_file="d2.json", decision_sha256=util.sha256_text(text),
                      audit_status="PASSED_WITH_HOLDS", holds=[{"position_id": "2612-09", "overlay": "OWNER_HOLD"}],
                      buys=[{k: b[k] for k in ("position_id", "isin", "symbol", "series", "rank", "earmark_paise")}
                            for b in buys])
        self.tg_ok = False                                # the plan-time buttons were not delivered
        E.open_approval(self.env(), "leg", "2612-09", {"buy": "Buy", "skip": "Skip"},
                        "HOLD SYM9 (OWNER_HOLD): BE series", position_id="2612-09")
        self.tg_ok = True
        self.new_day(dt.date(2026, 12, 2))
        self.assertEqual(E.remind_holds(self.env()), 1)


class TestF4SplitInTransit(TC.CAHarness):
    def test_neither_isin_showing_waits_then_the_new_isin_is_booked(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", SPLIT))
        self.new_day(dt.date(2026, 12, 16))
        CA.credit_check(self.env(), self.rows(SYM2=0))            # in transit
        p = L.replay(self.j.read()).positions["2612-02"]
        self.assertIn("PENDING_CA", p.overlays)
        self.assertNotIn("OWNER_HOLD", p.overlays)
        self.assertEqual(E.holdings_short(L.replay(self.j.read()), {"INE000000001": 12}), [])
        self.k.holdings_qty.clear()
        self.k.holdings_qty.update({"INE000000001": 12})
        self.assertEqual(E.three_way(self.env(), {"INE000000001": 12, "INE000000002": 50}, "2026-12-16"), [])
        self.new_day(dt.date(2026, 12, 17))
        CA.credit_check(self.env(), self.rows(SYM2=0, INE00000002B=500))
        lg = L.replay(self.j.read())
        p = lg.positions["2612-02"]
        self.assertEqual((p.held_qty, p.isin, p.overlays), (500, "INE00000002B", set()))
        self.assertEqual(E.holdings_short(lg, {"INE000000001": 12, "INE00000002B": 500}), [])

    def test_owner_ca_can_move_the_book_to_a_new_isin(self) -> None:
        msg = CA.owner_set_qty(self.env(), "INE000000002", 500, "INE00000002B")
        self.assertIn("INE00000002B", msg)
        p = L.replay(self.j.read()).positions["2612-02"]
        self.assertEqual((p.held_qty, p.isin), (500, "INE00000002B"))
        self.assertIn("must be", CA.owner_set_qty(self.env(), "INE000000001", 24, "BAD"))



class TestF4NewIsinBetweenTheSnapshotAndTheAfternoon(TC.CAHarness):
    def test_transit_at_0835_credit_at_1440_no_short_no_three_way(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", SPLIT))
        self.new_day(dt.date(2026, 12, 16))
        self.j.append("HOLDINGS_SNAPSHOT", holdings={"INE000000001": 12}, settled={"INE000000001": 12})  # transit
        CA.credit_check(self.env(), self.rows(SYM2=0, INE00000002B=500))                              # 14:40
        lg = L.replay(self.j.read())
        h = E.with_todays_credits(self.j.read(), {"INE000000001": 12}, "2026-12-16")
        self.assertEqual(h.get("INE00000002B"), 500)
        self.assertEqual(E.holdings_short(lg, h), [])
        self.k.holdings_qty.clear()
        self.k.holdings_qty.update({"INE000000001": 12, "INE00000002B": 500})
        self.assertEqual(E.three_way(self.env(), {"INE000000001": 12}, "2026-12-16"), [])


if __name__ == "__main__":
    unittest.main()

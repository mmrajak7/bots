"""Corporate actions on book names (January spec §7) with the real research parser (pipeline/nse_ca.py).

Book: SYM1 12 shares (INE000000001), SYM2 50 shares (INE000000002), bought 1 Dec."""
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
from momentum import pipeline as P  # noqa: E402
from momentum import util  # noqa: E402
from test_sells import JAN, SellHarness, fill_all  # noqa: E402
from test_engine import Harness  # noqa: E402

NSE = (HOME / "../../NSE_DATA").resolve()
M = P.import_research(HOME / "pipeline", NSE)


def rec(isin: str, sym: str, ex: str, subject: str) -> dict:
    d = dt.date.fromisoformat(ex)
    return {"symbol": sym, "series": "EQ", "isin": isin, "exDate": d.strftime("%d-%b-%Y"), "subject": subject}


DIV = rec("INE999999999", "OTHER", "2026-12-12", "Dividend - Rs 2 Per Share")      # a window is never empty


class CAHarness(Harness):
    prices = {1: 2000.0, 2: 500.0}

    def setUp(self) -> None:
        super().setUp()
        self.run_day(fill=fill_all(self.k))
        self.new_day(dt.date(2026, 12, 10))

    def scan(self, *records: dict) -> dict:
        body = json.dumps([DIV, *records]).encode()
        return CA.scan(self.env(), M.CA, get=lambda u: (200, body, ""))

    def rows(self, **q: int) -> list:
        base = {"INE000000001": ("SYM1", 12), "INE000000002": ("SYM2", 50)}
        out = []
        for isin, (sym, n) in base.items():
            out.append({"isin": isin, "tradingsymbol": sym, "quantity": q.get(sym, n), "t1_quantity": 0})
        for k, v in q.items():
            if k.startswith("INE"):
                out.append({"isin": k, "tradingsymbol": "SYM2", "quantity": v, "t1_quantity": 0})
        return [r for r in out if r["quantity"]]

    def lg(self) -> L.Ledger:
        return L.replay(self.j.read())

    def codes(self) -> list:
        return [r["code"] for r in self.j.read() if r["type"] == "ALERT"]

    def test_bonus_waits_then_the_credit_is_booked(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        p = self.lg().positions["2612-02"]
        self.assertNotIn("PENDING_CA", p.overlays)          # 5 days before the ex-date the name still trades as is
        self.new_day(dt.date(2026, 12, 14))                  # the evening before: its orders start to wait
        CA.activate_due(self.env())
        self.assertIn("PENDING_CA", self.lg().positions["2612-02"].overlays)
        self.new_day(dt.date(2026, 12, 15))
        self.assertEqual(CA.credit_check(self.env(), self.rows()), ["SYM2: waiting for the credit (50 of 100)"])
        CA.credit_check(self.env(), self.rows(SYM2=100))
        p = self.lg().positions["2612-02"]
        self.assertEqual((p.held_qty, p.overlays), (100, set()))
        self.assertEqual(p.filled_paise, self.lg().positions["2612-02"].filled_paise)   # cost unchanged

    def test_split_with_a_new_isin_follows_the_shares(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15",
                      "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share"))
        self.new_day(dt.date(2026, 12, 16))
        CA.credit_check(self.env(), self.rows(SYM2=0, INE00000002B=500))
        p = self.lg().positions["2612-02"]
        self.assertEqual((p.held_qty, p.isin, p.overlays), (500, "INE00000002B", set()))
        self.assertEqual(E.holdings_short(self.lg(), {"INE000000001": 12, "INE00000002B": 500}), [])

    def test_demerger_is_held_for_the_owner(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Demerger"))
        p = self.lg().positions["2612-02"]
        self.assertIn("OWNER_HOLD", p.overlays)
        self.assertNotIn("PENDING_CA", p.overlays)
        self.assertIn("CA_OWNER", self.codes())

    def test_consolidation_is_held_for_the_owner(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15",
                      "Consolidation Of Equity Shares From Re 1 Per Share To Rs 10 Per Share"))
        self.assertIn("OWNER_HOLD", self.lg().positions["2612-02"].overlays)

    def test_dividend_and_other_names_do_nothing(self) -> None:
        r = self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Interim Dividend - Rs 5 Per Share"))
        self.assertEqual(r["acted"], [])
        self.assertEqual(self.lg().positions["2612-02"].overlays, set())

    def test_a_rescan_never_acts_twice(self) -> None:
        r1 = self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        self.new_day(dt.date(2026, 12, 15))
        r2 = self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        CA.activate_due(self.env())
        self.assertEqual((len(r1["acted"]), r2["acted"]), (1, []))
        ons = [r for r in self.j.read() if r["type"] == "OVERLAY" and r["overlay"] == "PENDING_CA" and r["on"]]
        self.assertEqual(len(ons), 1)

    def test_unreadable_holdings_judge_nothing(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        self.new_day(dt.date(2026, 12, 15))
        self.assertEqual(CA.credit_check(self.env(), []), [])
        self.assertEqual(self.lg().positions["2612-02"].overlays, {"PENDING_CA"})

    def test_mismatch_goes_to_the_owner(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        self.new_day(dt.date(2026, 12, 15))
        CA.credit_check(self.env(), self.rows(SYM2=75))
        p = self.lg().positions["2612-02"]
        self.assertEqual((p.held_qty, p.overlays), (50, {"OWNER_HOLD"}))
        self.assertIn("CA_MISMATCH", self.codes())

    def test_owner_ca_command(self) -> None:
        E.apply_cmd(self.env(), "ca", "INE000000002 100")
        self.assertEqual(self.lg().positions["2612-02"].held_qty, 100)

    def test_failed_download_is_one_alert_a_day(self) -> None:
        for _ in range(2):
            r = CA.scan(self.env(), M.CA, get=lambda u: (0, None, "timeout"))
        self.assertFalse(r["ok"])
        self.assertEqual(self.codes().count("CA_SCAN"), 1)

    def test_three_way_accepts_a_credit_after_the_opening(self) -> None:
        self.scan(rec("INE000000002", "SYM2", "2026-12-15", "Bonus 1:1"))
        self.new_day(dt.date(2026, 12, 15))
        CA.activate_due(self.env())
        self.k.holdings_qty.update({"INE000000001": 12, "INE000000002": 100})
        probs = E.three_way(self.env(), {"INE000000001": 12, "INE000000002": 50}, "2026-12-15")
        self.assertEqual(probs, [])


class TestPendingCAHoldsTheSell(SellHarness):
    def test_sell_waits_for_the_credit_and_the_gate_stays_closed(self) -> None:
        div = rec("INE999999999", "OTHER", "2027-01-05", "Dividend - Rs 2 Per Share")
        body = json.dumps([div, rec("INE000000002", "SYM2", "2026-12-30", "Bonus 1:1")]).encode()
        CA.scan(self.env(), M.CA, get=lambda u: (200, body, ""))
        lg = self.afternoon(fill=fill_all(self.k))
        self.assertNotIn("SELL", [o.transaction_type for o in self.k.orders_])
        self.assertEqual(self.gate()["result"], "CLOSED")
        self.assertEqual(lg.positions["2701-01"].leg, "BUY_GATED")


if __name__ == "__main__":
    unittest.main()

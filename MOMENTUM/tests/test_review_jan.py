"""Planted cases from the independent review of the January sell side (2026-11-28): C1, H1-H3, M2-M4, L1, and the
corporate-action timing found while fixing them."""
import datetime as dt
import json
import sys
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "src"))
sys.path.insert(0, str(HOME / "tests"))

from momentum import cli  # noqa: E402
from momentum import corpact as CA  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import sellside as SS  # noqa: E402
from momentum import util  # noqa: E402
from test_corpact import DIV, M, rec  # noqa: E402
from test_engine import PLAN, Harness  # noqa: E402
from test_sells import JAN, SellHarness, fill_all, fill_only  # noqa: E402


class TestC1OrderDays(SellHarness):
    def test_afternoon_and_morning_have_work_with_only_sells_and_gated_buys(self) -> None:
        lg = L.replay(self.j.read())
        self.assertEqual({p.leg for p in lg.positions.values()}, {"NONE", "SELLING", "BUY_GATED"})
        self.assertTrue(cli.order_work(lg))
        self.assertNotIn("PENDING_BUY", {p.leg for p in lg.positions.values()})


class TestH1OwnerFilledOnASell(SellHarness):
    def make_owner_item(self) -> str:
        self.afternoon(fill=None)                              # the sell rests, is cancelled, reconciled
        lg = L.replay(self.j.read())
        iid = next(i.intent_id for i in lg.intents.values() if i.side == "SELL")
        self.j.append("ORPHAN", intent_id=iid, resolution="OWNER", order_id=None, reads=["planted"])
        return iid

    def test_filled_books_a_sell_as_a_sell(self) -> None:
        iid = self.make_owner_item()
        c0 = L.replay(self.j.read()).cash_paise
        E.apply_cmd(self.env(), "filled", f"{iid} 50 497.5")
        lg = L.replay(self.j.read())
        p = lg.positions["2612-02"]
        self.assertEqual((p.held_qty, p.leg), (0, "CLOSED"))
        self.assertGreater(lg.cash_paise, c0)

    def test_filled_never_books_more_than_the_book_holds(self) -> None:
        iid = self.make_owner_item()
        lg = L.replay(self.j.read())
        lg.intents[iid].qty = 60                                # what a wrong owner reply would claim
        E.apply_cmd(self.env(), "filled", f"{iid} 50 497.5")      # 50 is fine
        self.j.append("ORPHAN", intent_id=iid, resolution="OWNER", order_id=None, reads=["planted"])
        L.replay(self.j.read())                                   # the replay still works


class TestH2GatedFOEntrant(SellHarness):
    def test_gated_buy_that_joins_fo_on_day_1_expires(self) -> None:
        self.k.fo_futures.append("SYM3")
        self.afternoon(fill=fill_all(self.k))
        lg = L.replay(self.j.read())
        self.assertEqual(lg.positions["2701-01"].leg, "EXPIRED")
        self.assertNotIn("SYM3", [o.tradingsymbol for o in self.k.orders_])


class TestH3SeriesChangeAndHandSale(SellHarness):
    def test_name_moved_to_be_is_sold_as_be(self) -> None:
        self.k.ltp["NSE:SYM2-BE"] = self.k.ltp.pop("NSE:SYM2")
        self.afternoon(fill=fill_all(self.k))
        sell = [o for o in self.k.orders_ if o.transaction_type == "SELL"]
        self.assertEqual([o.tradingsymbol for o in sell], ["SYM2-BE"])
        self.assertIn("SERIES_CHANGED", [r["code"] for r in self.j.read() if r["type"] == "ALERT"])

    def test_owner_sold_by_hand(self) -> None:
        E.apply_cmd(self.env(), "sold", "INE000000002 50 480")
        p = L.replay(self.j.read()).positions["2612-02"]
        self.assertEqual((p.held_qty, p.leg), (0, "CLOSED"))
        E.apply_cmd(self.env(), "sold", "INE000000002 50 480")       # again: refused, nothing booked twice
        self.assertEqual(L.replay(self.j.read()).positions["2612-02"].held_qty, 0)


class TestM2ExtraShareAcrossSessions(Harness):
    """A buy gave its leftover to another's extra share and did not fill: its retry must not spend it again."""
    prices = {1: 1800.0, 2: 990.0}

    def test_total_spend_never_exceeds_the_amounts(self) -> None:
        lg0 = L.replay(self.j.read())
        total = sum(p.earmark_paise for p in lg0.positions.values())
        self.run_day(fill=lambda o: fill_all(self.k)(o) if o.tradingsymbol == "SYM2" else None)
        moves = [r for r in self.j.read() if r["type"] == "EARMARK_MOVE"]
        self.assertEqual([(r["from"], r["to"]) for r in moves], [("2612-01", "2612-02")])   # SYM2 got a 25th share, SYM1 paid; SYM1 does not fill
        self.new_day(dt.date(2026, 12, 2))
        lg = self.run_day(fill=fill_all(self.k))
        spent = sum(p.filled_paise for p in lg.positions.values())
        self.assertLessEqual(spent, total)
        self.assertEqual(sum(p.earmark_paise for p in lg.positions.values()), total)   # moved, never created


class TestM4SharedGatedBuy(SellHarness):
    def test_gated_buy_of_a_name_held_outside_the_book_is_held(self) -> None:
        h = dict(self.held)
        h["INE000000003"] = 5
        self.afternoon(fill=fill_all(self.k), holdings=h)
        lg = L.replay(self.j.read())
        self.assertIn("SHARED_NAME_HOLD", lg.positions["2701-01"].overlays)
        self.assertNotIn("SYM3", [o.tradingsymbol for o in self.k.orders_])


class TestL1GateJudgedOnce(SellHarness):
    def test_resume_inside_the_gate_uses_the_recorded_amounts(self) -> None:
        day = "2027-01-01"
        self.j.append("GATE", date=day, plan=L.plan_key(JAN), result="OPEN", reasons=[], margins={},
                      amounts={"2701-01": 123_400})
        SS.gate_stage(self.env(), day, {})
        p = L.replay(self.j.read()).positions["2701-01"]
        self.assertEqual((p.leg, p.earmark_paise), ("PENDING_BUY", 123_400))


class TestSaleMoneyAndSettlement(SellHarness):
    def test_buys_go_when_kite_does_not_show_the_sale_money_yet(self) -> None:
        c0 = L.replay(self.j.read()).cash_paise / 100
        self.k.cash = c0 - 500.0              # the book's cash before the sale; Kite's field has not caught up
        lg = self.afternoon(fill=fill_all(self.k))
        buy = L.replay(self.j.read()).positions["2701-01"].earmark_paise / 100
        self.assertGreater(buy, self.k.cash)  # without the sale money the guard would have refused
        self.assertEqual(lg.positions["2701-01"].leg, "NONE")

    def test_without_the_sale_money_the_cash_guard_still_holds(self) -> None:
        self.k.cash = 1000.0
        lg = self.afternoon(fill=fill_only(self.k, "BUY"))      # the sell did not fill: no sale money
        self.assertNotEqual(lg.positions["2701-01"].leg, "NONE")
        self.assertNotIn("BUY", [o.transaction_type for o in self.k.orders_])

    def test_unsettled_shares_are_not_sold(self) -> None:
        self.j.append("HOLDINGS_SNAPSHOT", holdings=dict(self.held),
                      settled={"INE000000001": 12, "INE000000002": 0})
        self.afternoon(fill=fill_all(self.k))
        self.assertNotIn("SELL", [o.transaction_type for o in self.k.orders_])
        self.assertIn("SELL_UNSETTLED", [r["code"] for r in self.j.read() if r["type"] == "ALERT"])


class TestCATiming(SellHarness):
    def test_announced_split_does_not_stop_a_sell_before_its_ex_date(self) -> None:
        div = rec("INE999999999", "OTHER", "2027-01-05", "Dividend - Rs 2 Per Share")
        body = json.dumps([div, rec("INE000000002", "SYM2", "2027-01-20", "Bonus 1:1")]).encode()
        CA.scan(self.env(), M.CA, get=lambda u: (200, body, ""))
        lg = self.afternoon(fill=fill_all(self.k))
        self.assertEqual(lg.positions["2612-02"].leg, "CLOSED")       # sold on 1 Jan, 19 days before the ex-date


class TestPreMergeReview(SellHarness):
    """Independent review 2 (pre-merge)."""

    def test_split_that_went_ex_before_the_buy_does_nothing(self) -> None:
        div = rec("INE999999999", "OTHER", "2027-01-05", "Dividend - Rs 2 Per Share")
        body = json.dumps([div, rec("INE000000002", "SYM2", "2026-11-30", "Bonus 1:1")]).encode()   # ex before 1 Dec
        CA.scan(self.env(), M.CA, get=lambda u: (200, body, ""))
        CA.activate_due(self.env())
        self.assertNotIn("PENDING_CA", L.replay(self.j.read()).positions["2612-02"].overlays)

    def test_credit_while_selling_sells_the_credited_shares(self) -> None:
        self.j.append("CA_CREDIT", position_id="2612-02", ca_id="b", factor_num=2, factor_den=1, qty_before=50,
                      qty_after=100)
        p = L.replay(self.j.read()).positions["2612-02"]
        self.assertEqual((p.held_qty, p.sell_target), (100, 100))
        lg = self.afternoon(fill=fill_all(self.k), holdings={"INE000000001": 12, "INE000000002": 100})
        self.assertEqual((lg.positions["2612-02"].held_qty, lg.positions["2612-02"].leg), (0, "CLOSED"))

    def test_decision_that_no_longer_fits_is_refused(self) -> None:
        from momentum import evening as EV
        lg = L.replay(self.j.read())
        self.assertFalse(EV.decision_fits(lg, {"sells": [{"position_id": "2612-01", "qty": 999}], "buys": []}))
        self.assertTrue(EV.decision_fits(lg, {"sells": [{"position_id": "2612-01", "qty": 12}], "buys": []}))

    def test_ledger_file_from_older_code_is_not_damage(self) -> None:
        new = L.replay(self.j.read()).to_json()
        old = json.loads(json.dumps(new))
        for p in old["positions"].values():
            p.pop("sold_qty")
        old.pop("test_cash_paise")
        self.assertTrue(cli.same_ledger(old, new))
        old["cash_paise"] += 1
        self.assertFalse(cli.same_ledger(old, new))


class TestBuyOnlyDayStartsAt1446(Harness):
    def test_telegram_down_1505_to_1509_still_buys(self) -> None:
        """On main the START went at ~14:46; the sell / gate stages must not delay it on a buy-only day."""
        def notify_window(text, buttons=None):  # noqa: ANN001, ANN202
            t = util.now().strftime("%H:%M")
            self.sent.append((text, buttons))
            return not ("15:05" <= t <= "15:09")
        env = self.env()
        env.notify = notify_window
        self.k.on_place = fill_all(self.k)
        E.afternoon(env, {}, lambda: [])
        self.assertEqual(self.k.calls.count("place"), 2)
        starts = [r for r in self.j.read() if r["type"] == "STEP_START" and r.get("step") == "DAY"]
        self.assertTrue(starts and starts[0]["telegram_ok"] and starts[0]["ts"][11:16] < "14:50")


class TestEarmarkMoveOnlyForPlacedOrders(Harness):
    prices = {1: 1800.0, 2: 990.0}

    def test_refused_receiver_moves_nothing(self) -> None:
        self.k.reject_next = "OrderException: something about the scrip"      # the first buy (SYM1) is refused
        self.run_day(fill=None)
        moves = [r for r in self.j.read() if r["type"] == "EARMARK_MOVE"]
        placed = {o.tradingsymbol for o in self.k.orders_}
        for m_ in moves:                         # every move belongs to an order that exists
            self.assertIn(L.replay(self.j.read()).positions[m_["to"]].symbol, placed)


if __name__ == "__main__":
    unittest.main()

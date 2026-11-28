"""Sell side of the afternoon on the fake broker (January spec §2, §5, §6, §8; D49, D52).

The book holds SYM1 (12 shares) and SYM2 (50 shares: the D52 extra share) from December. The January plan sells SYM2 and buys SYM3, which
waits for the sale money (BUY_GATED) until the 15:06 gate."""
import datetime as dt
import json
import sys
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from momentum import broker as B  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import sellside as SS  # noqa: E402
from momentum import util  # noqa: E402
from momentum import vet as V  # noqa: E402
from test_engine import Harness  # noqa: E402

JAN = {"month": "2027-01", "signal_day": "2026-12-31", "data_rev": 0}


def fill_all(k):  # noqa: ANN001, ANN201
    def f(o):  # noqa: ANN001, ANN202
        o.status, o.filled_quantity, o.average_price = "COMPLETE", o.quantity, o.price
        k.trades_.append({"trade_id": f"t{o.order_id}", "order_id": o.order_id, "quantity": o.quantity,
                          "average_price": o.price, "exchange_timestamp": "x"})
    return f


def fill_only(k, side):  # noqa: ANN001, ANN201
    def f(o):  # noqa: ANN001, ANN202
        if o.transaction_type == side:
            fill_all(k)(o)
    return f


class SellHarness(Harness):
    prices = {1: 2000.0, 2: 500.0, 3: 1000.0}

    def setUp(self) -> None:
        super().setUp()
        self.run_day(fill=fill_all(self.k))               # 1 Dec: both December buys fill
        lg = L.replay(self.j.read())
        self.held = {p.isin: p.held_qty for p in lg.positions.values()}
        self.assertEqual(self.held, {"INE000000001": 12, "INE000000002": 50})
        self.new_day(dt.date(2027, 1, 1))
        buys = [{"position_id": "2701-01", "isin": "INE000000003", "symbol": "SYM3", "series": "EQ", "rank": 5,
                 "earmark_paise": 0, "leg": "BUY_GATED", "signal_close_paise": 100_000, "ca_rows": []}]
        text = json.dumps({"buys": buys, "sells": [{"position_id": "2612-02"}]}) + "\n"
        (self.paths.decisions / "j.json").write_text(text, encoding="utf-8", newline="\n")
        self.j.append("PLAN_FINAL", plan_id=JAN, decision_file="j.json", decision_sha256=util.sha256_text(text),
                      audit_status="PASSED", holds=[],
                      sells=[{"position_id": "2612-02", "reason": "RANK", "sell_no": 1, "rank": 61, "qty": 50}],
                      buys=[{k: b[k] for k in ("position_id", "isin", "symbol", "series", "rank", "earmark_paise",
                                              "leg")} for b in buys])

    def afternoon(self, fill=None, holdings=None) -> L.Ledger:  # noqa: ANN001
        self.k.on_place = fill
        E.afternoon(self.env(), dict(self.held) if holdings is None else holdings, lambda: [])
        self.k.on_place = None
        return L.replay(self.j.read())

    def gate(self) -> dict:
        return [r for r in self.j.read() if r["type"] == "GATE"][-1]

    def alerts(self) -> list:
        return [r["code"] for r in self.j.read() if r["type"] == "ALERT"]


class TestRebalance(SellHarness):
    def test_sell_fills_then_the_buy_goes_with_the_sale_money(self) -> None:
        cash0 = L.replay(self.j.read()).cash_paise
        lg = self.afternoon(fill=fill_all(self.k))
        p2 = lg.positions["2612-02"]
        self.assertEqual((p2.leg, p2.held_qty, p2.state), ("CLOSED", 0, "CLOSED"))
        sell = next(o for o in self.k.orders_ if o.transaction_type == "SELL")
        self.assertEqual((sell.tag, sell.quantity), ("M2701S01A1", 50))
        self.assertEqual(Decimal(str(sell.price)), Decimal("497.50"))       # max(500 x 0.995, lower circuit)
        self.assertEqual(self.gate()["result"], "OPEN")
        n = lg.positions["2701-01"]
        self.assertEqual((n.leg, n.state), ("NONE", "HELD"))
        proceeds = 50 * 49_750
        free = cash0 + proceeds - SS.estimate_sell_charges_paise(self.cfg, proceeds, True)
        self.assertLessEqual(n.earmark_paise, free)
        self.assertGreater(n.earmark_paise, 0.99 * free)
        self.assertEqual(n.filled_qty, int(n.earmark_paise / 100 // (1000 * 1.01)))
        self.assertGreater(lg.cash_paise, 0)
        # sells first: the SELL order is placed before the BUY
        self.assertEqual([o.transaction_type for o in self.k.orders_], ["SELL", "BUY"])

    def test_rerun_after_the_sell_places_nothing_twice(self) -> None:
        self.afternoon(fill=fill_all(self.k))
        n = len(self.k.orders_)
        self.afternoon(fill=fill_all(self.k))
        self.assertEqual(len(self.k.orders_), n)

    def test_unfilled_sell_closes_the_gate_then_retries_then_the_buys_go(self) -> None:
        lg = self.afternoon(fill=None)                       # nothing fills; SYM2 trades above the lower circuit
        self.assertEqual(self.gate()["result"], "CLOSED")
        self.assertIn("BUY_GATED", self.alerts())
        self.assertEqual(lg.positions["2701-01"].leg, "BUY_GATED")
        self.assertEqual(lg.positions["2612-02"].sell_sessions, ["2027-01-01"])
        self.assertTrue(all(o.status == "CANCELLED" for o in self.k.orders_))   # 15:28 cancel took the sell
        self.assertNotIn("BUY", [o.transaction_type for o in self.k.orders_])
        self.new_day(dt.date(2027, 1, 4))
        self.afternoon(fill=None)
        self.assertEqual([o.tag for o in self.k.orders_ if o.transaction_type == "SELL"], ["M2701S01A2"])
        self.assertEqual(self.gate()["result"], "CLOSED")                  # gated session 2 of 2
        self.new_day(dt.date(2027, 1, 5))
        lg = self.afternoon(fill=fill_only(self.k, "BUY"))                 # session 3: the buys go anyway (D32)
        self.assertEqual(self.gate()["result"], "FORCED")
        self.assertIn("GATE_FORCED", self.alerts())
        self.assertEqual(lg.positions["2701-01"].leg, "NONE")
        self.assertEqual(lg.positions["2612-02"].leg, "SELLING")           # the sell carries on (tested)

    def test_sell_locked_at_the_lower_circuit_does_not_hold_the_buys(self) -> None:
        self.k.lower["NSE:SYM2"] = 500.0                      # LTP = lower circuit
        lg = self.afternoon(fill=fill_only(self.k, "BUY"))
        self.assertEqual(self.gate()["result"], "OPEN")
        self.assertEqual(lg.positions["2701-01"].leg, "NONE")
        self.assertEqual(lg.positions["2612-02"].leg, "SELLING")

    def test_no_trade_today_does_not_hold_the_buys(self) -> None:
        self.k.stale.append("NSE:SYM2")
        self.afternoon(fill=fill_only(self.k, "BUY"))
        self.assertEqual(self.gate()["result"], "OPEN")

    def test_vet_defer_on_the_sell_holds_the_gate(self) -> None:
        def vet(step, vin, skips):  # noqa: ANN001, ANN202
            return V.Verdict(True, "", {i["intent_id"]: (("DEFER", "DEFER_DATA_STALE", "x") if i.get("leg") == "sell"
                                                         else ("GO", "GO_OK", "")) for i in vin["intents"]}, "s")
        self.vet = vet
        lg = self.afternoon(fill=fill_all(self.k))
        self.assertEqual([o.transaction_type for o in self.k.orders_], [])
        self.assertIn("VET_DEFERRED", lg.positions["2612-02"].overlays)
        self.assertEqual(self.gate()["result"], "CLOSED")

    def test_vet_sees_both_sides_in_one_call(self) -> None:
        seen = []

        def vet(step, vin, skips):  # noqa: ANN001, ANN202
            seen.append((step, sorted((i.get("leg"), i["isin"]) for i in vin["intents"])))
            return V.Verdict(True, "", {i["intent_id"]: ("GO", "GO_OK", "") for i in vin["intents"]}, "s")
        self.vet = vet
        self.afternoon(fill=fill_all(self.k))
        self.assertEqual(seen, [("REBALANCE", [("buy", "INE000000003"), ("sell", "INE000000002")])])

    def test_buy_amount_never_exceeds_the_vetted_envelope(self) -> None:
        self.k.ltp["NSE:SYM2"] = 500.0
        rec = []
        orig = SS.gate_stage

        def gate(env, day, envelopes):  # noqa: ANN001, ANN202
            rec.append(dict(envelopes))
            return orig(env, day, {k: 1000 for k in envelopes})          # plant a small vetted envelope
        SS.gate_stage = gate
        try:
            lg = self.afternoon(fill=fill_all(self.k))
        finally:
            SS.gate_stage = orig
        self.assertTrue(rec[0])
        self.assertEqual(lg.positions["2701-01"].earmark_paise, 100_000)


class TestSellSafety(SellHarness):
    def test_kite_holding_less_than_the_book_never_sells(self) -> None:
        h = dict(self.held)
        h["INE000000002"] = 40
        lg = self.afternoon(fill=fill_all(self.k), holdings=h)
        self.assertNotIn("SELL", [o.transaction_type for o in self.k.orders_])
        self.assertIn("OWNER_HOLD", lg.positions["2612-02"].overlays)

    def test_shared_name_is_told_before_the_sell_and_only_book_shares_sell(self) -> None:
        h = dict(self.held)
        h["INE000000002"] = 60
        self.afternoon(fill=fill_all(self.k), holdings=h)
        self.assertIn("SHARED_SELL", self.alerts())
        self.assertEqual(next(o for o in self.k.orders_ if o.transaction_type == "SELL").quantity, 50)

    def test_ddpi_refusal_stops_every_sell_and_tells_the_owner(self) -> None:
        self.k.reject_next = "OrderException: Order rejected: TPIN authorisation pending for the scrip"
        lg = self.afternoon(fill=None)
        self.assertIn("DDPI", self.alerts())
        self.assertEqual(lg.positions["2612-02"].leg, "SELLING")
        self.assertEqual(lg.positions["2612-02"].overlays, set())           # retried next session, not held
        self.assertEqual(self.gate()["result"], "CLOSED")

    def test_refused_sell_whose_tag_is_in_the_book_is_adopted(self) -> None:
        def on_place(o):  # noqa: ANN001, ANN202
            if o.transaction_type == "SELL":
                self.k.raw_error_next = B.Rejected("OrderException: something odd")
                fill_all(self.k)(o)
        lg = self.afternoon(fill=on_place)
        self.assertEqual(lg.positions["2612-02"].leg, "CLOSED")          # the fill was booked, never sold twice
        self.assertEqual(sum(1 for o in self.k.orders_ if o.transaction_type == "SELL"), 1)

    def test_lost_reply_is_settled_by_tag_and_never_sent_twice(self) -> None:
        def on_place(o):  # noqa: ANN001, ANN202
            if o.transaction_type == "SELL":
                self.k.lose_reply_next = True
                fill_all(self.k)(o)
        lg = self.afternoon(fill=on_place)
        self.assertEqual(lg.positions["2612-02"].leg, "CLOSED")
        self.assertEqual(sum(1 for o in self.k.orders_ if o.transaction_type == "SELL"), 1)

    def test_ladder_moves_the_sell_down_but_never_under_its_floor(self) -> None:
        lg0 = L.replay(self.j.read())
        orig = self.k.quote

        def falling(keys):  # noqa: ANN001, ANN202
            if util.now() >= util.at_today("14:55"):
                self.k.ltp["NSE:SYM2"] = 480.0                 # -4%: the ladder may only reach the floor (495)
            return orig(keys)
        self.k.quote = falling
        self.afternoon(fill=None)
        mods = [r for r in self.j.read() if r["type"] == "MODIFY_RESULT" and r["outcome"] == "OK"]
        self.assertTrue(mods)
        self.assertEqual(min(int(r["new_limit_paise"]) for r in mods), 49_500)    # 500 x 0.990
        self.assertTrue(lg0)

    def test_broker_refuses_more_than_the_ledger_holds(self) -> None:
        p = L.replay(self.j.read()).positions["2612-02"]
        with self.assertRaises(B.BrokerRefused):
            self.env().broker.place_sell(intent_id="x", key={"position_id": p.position_id}, tag="M2701S01A1",
                                         isin=p.isin, tradingsymbol="SYM2", qty=51, limit=497.5, ltp=500.0,
                                         floor_price=495.0, max_qty=51)

    def test_sell_fill_charges_include_one_dp_charge(self) -> None:
        self.afternoon(fill=fill_all(self.k))
        f = [r for r in self.j.read() if r["type"] == "FILL" and r.get("side") == "SELL"]
        self.assertEqual(len(f), 1)
        self.assertGreaterEqual(f[0]["charges_paise"], 1593)                  # Rs 15.93 DP + STT etc.


class TestCarriedBuyTags(Harness):
    prices = {1: 2000.0, 2: 500.0}

    def test_december_buy_retrying_in_january_keeps_its_own_tag(self) -> None:
        """A December buy still inside its lock-try window when January starts (E26 keeps it) and January's new
        position with the same number must not share a tag prefix: the order-book guard would refuse one."""
        def only1(o):  # noqa: ANN001, ANN202
            if o.tradingsymbol == "SYM1":
                fill_all(self.k)(o)
        self.run_day(fill=only1)                          # SYM2 (2612-02) unfilled: still pending
        self.new_day(dt.date(2027, 1, 1))
        buys = [{"position_id": "2701-02", "isin": "INE000000009", "symbol": "SYM1", "series": "EQ", "rank": 2,
                 "earmark_paise": 1_000_000, "signal_close_paise": 200_000, "ca_rows": []}]
        text = json.dumps({"buys": buys}) + "\n"
        (self.paths.decisions / "j.json").write_text(text, encoding="utf-8", newline="\n")
        self.j.append("PLAN_FINAL", plan_id=JAN, decision_file="j.json", decision_sha256=util.sha256_text(text),
                      audit_status="PASSED", holds=[], sells=[],
                      buys=[{k: b[k] for k in ("position_id", "isin", "symbol", "series", "rank", "earmark_paise")}
                            for b in buys])
        self.run_day(fill=None)
        tags = sorted(o.tag for o in self.k.orders_)
        self.assertEqual(tags, ["M2612B02A2", "M2701B02A1"])


class TestExtraShare(unittest.TestCase):
    cfg = {"buy_ltp_mult": "1.005", "buy_cap_mult": "1.010", "extra_share_max_over": "0.25"}

    class Env:
        def __init__(self, cfg):  # noqa: ANN001
            self.cfg = cfg

    def leg(self, pid: str, sym: str, rupees: int) -> L.Position:
        return L.Position(pid, {}, "I" + pid, sym, "EQ", 1, rupees * 100)

    def sizes(self, legs, prices):  # noqa: ANN001, ANN201
        q = {f"NSE:{s}": {"last_price": p, "upper_circuit_limit": p * 1.2} for s, p in prices.items()}
        ticks = {s: Decimal("0.05") for s in prices}
        return SS.size_buys(self.Env(self.cfg), legs, q, ticks)

    def test_largest_leftover_gets_the_extra_share_and_the_total_stays_inside(self) -> None:
        legs = [self.leg("a", "A", 10_000), self.leg("b", "B", 10_000), self.leg("c", "C", 10_000)]
        out = self.sizes(legs, {"A": 1800.0, "B": 1050.0, "C": 3000.0})
        # cap prices 1818 / 1060.5 / 3030 -> floor 5 / 9 / 3, leftovers 910 / 455.5 / 910 (0.50 / 0.43 / 0.30 of a
        # share), pool 2275.5: A (largest fraction) gets a 6th share, the 457.5 left pays no other
        self.assertEqual({k: v[0] for k, v in out.items()}, {"a": 6, "b": 9, "c": 3})
        spend = sum(v[0] * v[3] for v in out.values())
        self.assertLessEqual(spend, 30_000)

    def test_no_name_goes_more_than_25_percent_over(self) -> None:
        out = self.sizes([self.leg("a", "A", 10_000), self.leg("b", "B", 10_000)], {"A": 6000.0, "B": 100.0})
        self.assertEqual(out["a"][0], 1)                     # pool 3,941 < one 6,060 share (and 2 x 6,060 > 12,500)
        self.assertLessEqual(sum(v[0] * v[3] for v in out.values()), 20_000)

    def test_a_lone_buy_never_gets_more_than_its_amount(self) -> None:
        out = self.sizes([self.leg("a", "A", 10_000)], {"A": 1800.0})
        self.assertEqual(out["a"][0], 5)


if __name__ == "__main__":
    unittest.main()

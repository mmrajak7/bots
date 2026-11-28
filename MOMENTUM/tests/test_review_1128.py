"""Go-live review 2026-11-28 fixes: C1 (refused but placed), T4 (probe moves on a stock rejection), T1 (book size)."""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_engine import Harness  # noqa: E402

from momentum import broker as B  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import evening as EV  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum.fakebroker import FakeOrder  # noqa: E402


def fill_now(k):  # noqa: ANN001, ANN201
    def f(o):  # noqa: ANN001, ANN202
        o.status, o.filled_quantity, o.average_price = "COMPLETE", o.quantity, o.price
        k.trades_.append({"trade_id": f"t{o.order_id}", "order_id": o.order_id, "quantity": o.quantity,
                          "average_price": o.price, "exchange_timestamp": "x"})
    return f


class TestRefusedButPlaced(Harness):
    def test_refusal_with_the_tag_in_the_book_is_adopted(self) -> None:
        """C1: Kite raised after creating (and filling) the order: booked, no hold, no second buy offered."""
        fill = fill_now(self.k)

        def on_place(o):  # noqa: ANN001
            fill(o)
            if o.tradingsymbol == "SYM1":
                self.k.raw_error_next = B.Rejected("OrderException: something went wrong")
        lg = self.run_day(fill=on_place)
        p = lg.positions["2612-01"]
        kite_qty = sum(o.filled_quantity for o in self.k.orders_ if o.tradingsymbol == "SYM1")
        self.assertGreater(kite_qty, 0)
        self.assertEqual(p.filled_qty, kite_qty)
        self.assertNotIn("OWNER_HOLD", p.overlays)
        self.assertEqual(self.k.calls.count("place"), 2)

    def test_order_appearing_after_the_refusal_is_found_and_booked(self) -> None:
        """C1, late: the tag was absent at the refusal and shows up later: FOUND by settle_orphans, fills booked."""
        env = self.env()
        self.k.reject_next = "OrderException: planted"
        self.run_day()
        lg = L.replay(self.j.read())
        i = next(i for i in lg.intents.values() if i.position_id == "2612-01")
        self.assertTrue(i.status.startswith("REJECTED"))
        late = FakeOrder("555", i.tag, "SYM1", "BUY", i.qty, i.limit_paise / 100)
        late.status, late.filled_quantity, late.average_price = "COMPLETE", i.qty, i.limit_paise / 100
        self.k.orders_.append(late)
        self.k.trades_.append({"trade_id": "late1", "order_id": "555", "quantity": i.qty,
                               "average_price": i.limit_paise / 100, "exchange_timestamp": "x"})
        E.reconcile(env, self.day.isoformat())
        lg = L.replay(self.j.read())
        self.assertEqual(lg.intents[i.intent_id].order_id, "555")
        self.assertEqual(lg.positions["2612-01"].filled_qty, i.qty)
        self.assertEqual(lg.unresolved, [])

    def test_plain_refusal_stays_final_and_is_no_owner_item(self) -> None:
        self.k.reject_next = "OrderException: planted"
        lg = self.run_day()
        i = next(i for i in lg.intents.values() if i.position_id == "2612-01")
        self.assertTrue(i.status.startswith("REJECTED"))
        self.assertEqual(lg.unresolved, [])

    def test_refusal_with_unreadable_book_is_timeout(self) -> None:
        """Absence cannot be proven -> unknown outcome (settled by tag), never a final rejection."""
        self.k.reject_next = "OrderException: planted"
        orig = self.k.orders
        state = {"raised": False}

        def orders():  # noqa: ANN202
            if "place" in self.k.calls and not state["raised"]:     # the first read after the refused call
                state["raised"] = True
                raise B.NetworkError("planted")
            return orig()
        self.k.orders = orders
        self.run_day()
        lg = L.replay(self.j.read())
        i = next(i for i in lg.intents.values() if i.position_id == "2612-01")
        self.assertNotIn(i.status, ("REJECTED_OTHER", "REJECTED_RMS", "REJECTED_BAND"))

    def test_kite_general_exception_is_unknown_outcome(self) -> None:
        from kiteconnect import exceptions as kx
        api = B.KiteApi.__new__(B.KiteApi)
        api.min_gap, api._last = 0.0, 0.0
        api._lock = __import__("threading").Lock()

        def boom(**kw):  # noqa: ANN003, ANN202
            raise kx.GeneralException("upstream 502")
        with self.assertRaises(B.NetworkError):
            api._write(boom)

        def order_exc(**kw):  # noqa: ANN003, ANN202
            raise kx.OrderException("Invalid quantity")
        with self.assertRaises(B.Rejected):
            api._write(order_exc)


class TestStartMessageRetry(Harness):
    def test_one_failed_send_does_not_cost_the_session(self) -> None:
        """O12: the starting message is retried each minute until 2 minutes before 15:10."""
        sends = {"n": 0}
        env = self.env()
        orig = env.notify

        def flaky(text, buttons=None):  # noqa: ANN001, ANN202
            if text.startswith("starting"):
                sends["n"] += 1
                if sends["n"] <= 2:
                    return False
            return orig(text, buttons)
        env.notify = flaky
        E.afternoon(env, {}, lambda: [])
        self.assertEqual(sends["n"], 3)
        self.assertIn("place", self.k.calls)

    def test_auth_error_on_a_read_is_a_network_error(self) -> None:
        """M5: a dead token on a read fails the stage closed (NetworkError handlers), never a crash."""
        from kiteconnect import exceptions as kx
        api = B.KiteApi.__new__(B.KiteApi)
        api.min_gap, api._last, api.retry_s = 0.0, 0.0, [0]
        api._lock = __import__("threading").Lock()

        def dead():  # noqa: ANN202
            raise kx.TokenException("Incorrect api_key or access_token")
        with self.assertRaises(B.NetworkError):
            api._read(dead)


class TestHoldsNeverSilent(Harness):
    def test_untapped_hold_is_reminded_and_release_clears_audit_hold(self) -> None:
        """M1: a lost button is re-sent each morning; 'release <isin>' clears AUDIT_HOLD (it used to say 'done' and
        change nothing)."""
        env = self.env()
        p = L.replay(self.j.read()).positions["2612-01"]
        self.j.append("OVERLAY", position_id=p.position_id, overlay="AUDIT_HOLD", on=True, reason="audit")
        self.tg_ok = False                                      # the flag button never reached the owner
        E.open_approval(env, "flag", "F1", {"real": "Real - clear", "hold": "Hold name"}, "AUDIT FLAG SYM1 x",
                        position_id=p.position_id, other_open_flags=[])
        self.tg_ok = True
        self.sent.clear()
        self.assertEqual(E.remind_holds(env), 1)
        self.assertTrue(self.sent[0][0].startswith("REMINDER AUDIT FLAG SYM1"))
        self.assertEqual(len(self.sent[0][1]), 2)
        E.apply_cmd(env, "release", p.isin)
        lg = L.replay(self.j.read())
        self.assertNotIn("AUDIT_HOLD", lg.positions["2612-01"].overlays)
        self.assertIn("changed", self.sent[-1][0])
        self.sent.clear()
        E.apply_cmd(env, "release", p.isin)
        self.assertIn("nothing to change", self.sent[-1][0])
        self.assertEqual(E.remind_holds(env), 0)

    def test_hold_without_any_button_gets_a_release_line(self) -> None:
        env = self.env()
        self.j.append("OVERLAY", position_id="2612-02", overlay="AUDIT_HOLD", on=True, reason="audit")
        self.sent.clear()
        self.assertEqual(E.remind_holds(env), 1)
        self.assertIn("release INE000000002", self.sent[0][0])


class TestMorningCash(Harness):
    def test_short_account_is_told_at_0835(self) -> None:
        from momentum import cli
        env = self.env()
        self.k.cash = 1000.0
        cli.morning_cash_check(env)
        self.assertTrue(any("ALERT CASH_AM" in t for t, _ in self.sent))
        self.sent.clear()
        self.k.cash = 10_000_000.0
        cli.morning_cash_check(env)
        self.assertFalse(any("CASH_AM" in t for t, _ in self.sent))


class TestMissingQuote(Harness):
    def run_missing(self, listed: bool):  # noqa: ANN201
        from momentum import vet as V
        orig = self.k.quote
        state = {"first": True}

        def quote(keys):  # noqa: ANN001, ANN202
            out = orig(keys)
            if state["first"]:                       # only the 14:45 read misses SYM1
                state["first"] = False
                out.pop("NSE:SYM1", None)
            return out
        self.k.quote = quote
        if not listed:
            self.k.not_listed.append("SYM1")

        def vet(step, vin, skips):  # noqa: ANN001, ANN202
            return V.Verdict(True, "", {i["intent_id"]: (("SKIP", "SKIP_NO_INSTRUMENT", "") if i["quote_missing"]
                                                         else ("GO", "GO_OK", "")) for i in vin["intents"]}, "sha")
        self.vet = vet
        return self.run_day()

    def test_listed_name_with_a_missing_quote_waits_a_session(self) -> None:
        lg = self.run_missing(listed=True)
        p = lg.positions["2612-01"]
        self.assertEqual((p.leg, p.tries_used), ("PENDING_BUY", 0))
        self.assertTrue(any("ALERT NO_QUOTE" in t for t, _ in self.sent))

    def test_name_gone_from_the_instruments_is_skipped(self) -> None:
        lg = self.run_missing(listed=False)
        self.assertEqual(lg.positions["2612-01"].leg, "SKIPPED")


class TestRejectClasses(unittest.TestCase):
    def test_rms_prefix_alone_is_stock_specific(self) -> None:
        self.assertEqual(B.classify_reject("RMS:Blocked for trading in this scrip"), "REJECTED_OTHER")
        self.assertEqual(B.classify_reject("RMS:Margin Exceeds, Required:9970, Available:12"), "REJECTED_RMS")
        self.assertEqual(B.classify_reject("RMS:Rule: Check circuit limit including square off order exceeds"),
                         "REJECTED_BAND")


class TestProbeBound(Harness):
    n = 3
    prices = {1: 2000.0, 2: 500.0, 3: 300.0}

    def test_at_most_probe_max_probes(self) -> None:
        def reject_all(o):  # noqa: ANN001
            o.status, o.status_message = "REJECTED", "RMS:Blocked for trading"
        lg = self.run_day(fill=reject_all)
        self.assertEqual(self.k.calls.count("place"), int(self.cfg["probe_max"]))
        self.assertEqual(lg.positions["2612-03"].tries_used, 0)
        self.assertTrue(any("PROBE_FAILED" in t for t, _ in self.sent))


class TestBookOpen(Harness):
    def setUp(self) -> None:
        super().setUp()
        self.j.path.write_text("", encoding="utf-8")           # a fresh journal: no BOOK_OPEN yet

    def arm(self, cap: int):  # noqa: ANN201
        from momentum import control as C
        return C.Arm(phase="P0", plan_months=("2026-12",), mode="automatic", cap_rupees=cap,
                     valid_from=dt.date(2026, 11, 30), valid_to=dt.date(2026, 12, 31))

    def test_dry_opens_with_units_from_cash(self) -> None:
        env = self.env()
        env.dry = True
        EV.book_open(env)
        r = [r for r in self.j.read() if r["type"] == "BOOK_OPEN"][0]
        self.assertEqual((r["cash_paise"], r["units"]), (200_000 * 100, 2000))

    def test_live_needs_an_arm_covering_the_book(self) -> None:
        env = self.env()
        env.dry = False
        env.arm = None
        with self.assertRaises(EV.EveningError):
            EV.book_open(env)
        env.arm = self.arm(150_000)
        with self.assertRaises(EV.EveningError):
            EV.book_open(env)
        self.assertFalse(any(r["type"] == "BOOK_OPEN" for r in self.j.read()))
        env.arm = self.arm(220_000)
        EV.book_open(env)
        self.assertTrue(any(r["type"] == "BOOK_OPEN" for r in self.j.read()))


if __name__ == "__main__":
    unittest.main()

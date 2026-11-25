"""Buy-engine tests on the fake broker (build spec §K T4: planted buy-path cases)."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from momentum import broker as B  # noqa: E402
from momentum import config as CF  # noqa: E402
from momentum import control as C  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import util  # noqa: E402
from momentum import vet as V  # noqa: E402
from momentum.fakebroker import FakeKite  # noqa: E402
from momentum.journal import Journal  # noqa: E402

HOME = Path(__file__).resolve().parents[1]
PLAN = {"month": "2026-12", "signal_day": "2026-11-30", "data_rev": 0}
ARM = C.Arm("P0", ("2026-12",), "automatic", 550000, dt.date(2026, 11, 30), dt.date(2026, 12, 31))


class Clock:
    def __init__(self, day: dt.date) -> None:
        self.t = dt.datetime(day.year, day.month, day.day, 14, 40, tzinfo=util.IST)

    def __call__(self) -> dt.datetime:
        return self.t

    def until(self, t: dt.datetime) -> None:
        if t > self.t:
            self.t = t


def go_vet(step, vin, skips):  # noqa: ANN001
    return V.Verdict(True, "", {i["intent_id"]: ("GO", "GO_OK", "") for i in vin["intents"]}, "sha")


class Harness(unittest.TestCase):
    n = 2
    prices = {1: 2000.0, 2: 500.0}
    ca_rows: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = CF.load(HOME)
        self.paths = CF.paths(self.tmp, self.tmp / "state")
        self.paths.ensure()
        self.j = Journal(self.paths.journal)
        self.k = FakeKite(ltp={f"NSE:SYM{i}": p for i, p in self.prices.items()})
        self.sent = []
        self.tg_ok = True
        self.vet = go_vet
        self.day = dt.date(2026, 12, 1)
        self.new_day(self.day)
        self.j.append("BOOK_OPEN", cash_paise=50_000_000, units=5000, nav_per_unit_paise=10000, config_commit="t")
        buys = [{"position_id": f"2612-{i:02d}", "isin": f"INE{i:09d}", "symbol": f"SYM{i}", "series": "EQ",
                 "rank": i, "earmark_paise": 2_492_500, "signal_close_paise": int(self.prices[i] * 100),
                 "ca_rows": self.ca_rows.get(i, [])} for i in range(1, self.n + 1)]
        text = json.dumps({"buys": buys}) + "\n"
        (self.paths.decisions / "d.json").write_text(text, encoding="utf-8", newline="\n")
        self.j.append("PLAN_FINAL", plan_id=PLAN, decision_file="d.json", decision_sha256=util.sha256_text(text),
                      audit_status="PASSED", holds=[], buys=[{k: b[k] for k in ("position_id", "isin", "symbol",
                                                                               "series", "rank", "earmark_paise")}
                                                            for b in buys])

    def new_day(self, d: dt.date) -> None:
        self.clock = Clock(d)
        util.set_clock(self.clock)
        self.k.orders_.clear()
        self.k.trades_.clear()

    def tearDown(self) -> None:
        util.set_clock(None)

    def env(self) -> E.Env:
        def notify(text, buttons=None):  # noqa: ANN001
            self.sent.append((text, buttons))
            return self.tg_ok
        gate = B.Gate(live=True, armed=lambda: True, killed=lambda: C.killed(self.paths.kill_file))
        return E.Env(self.cfg, self.paths, self.j, self.k, B.Broker(self.k, self.j, gate), notify,
                     lambda s, vin, sk: self.vet(s, vin, sk), self.clock.until, arm=ARM)

    def run_day(self, fill=None) -> L.Ledger:  # noqa: ANN001
        if fill:
            self.k.on_place = fill
        E.afternoon(self.env(), {}, lambda: [])
        self.k.on_place = None
        return L.replay(self.j.read())


class TestHappy(Harness):
    def test_fill_and_rerun(self) -> None:
        env = self.env()

        def place_and_fill(o):  # noqa: ANN001
            o.status, o.filled_quantity, o.average_price = "COMPLETE", o.quantity, o.price
            self.k.trades_.append({"trade_id": f"t{o.order_id}", "order_id": o.order_id, "quantity": o.quantity,
                                   "average_price": o.price, "exchange_timestamp": "x"})
        self.k.on_place = place_and_fill
        E.afternoon(env, {}, lambda: [])
        lg = L.replay(self.j.read())
        self.assertEqual(sorted(p.leg for p in lg.positions.values()), ["NONE", "NONE"])
        self.assertEqual(self.k.calls.count("place"), 2)
        p1 = lg.positions["2612-01"]
        self.assertEqual(p1.filled_qty, 12)                  # floor(24925 / (2000 x 1.01)) = 12
        self.assertLess(lg.cash_paise, 50_000_000)
        E.afternoon(self.env(), {}, lambda: [])              # re-run the same day: nothing new
        self.assertEqual(self.k.calls.count("place"), 2)
        self.new_day(dt.date(2026, 12, 2))                   # next session: no pending legs -> nothing
        E.afternoon(self.env(), {}, lambda: [])
        self.assertEqual(self.k.calls.count("place"), 2)


class TestReview(Harness):
    """Cases from the 11-25 independent review."""

    def test_kill_mid_ladder_cancels_tracked_order(self) -> None:
        def kill_after_place(o):  # noqa: ANN001
            self.paths.kill_file.write_text("{}")
        self.run_day(fill=kill_after_place)
        self.assertTrue(all(o.status == "CANCELLED" for o in self.k.orders_))
        self.assertEqual(self.k.calls.count("modify"), 0)
        self.assertTrue(any("KILL" in a for a in self.env().alerts) or any("KILL" in t for t, _ in self.sent))

    def test_dry_prices_every_leg_sends_none(self) -> None:
        env = self.env()
        env.dry, env.arm = True, None
        env.broker.gate.live = False
        E.afternoon(env, {}, lambda: [])
        self.assertNotIn("place", self.k.calls)
        dry = [r for r in self.j.read() if r["type"] == "NOTE" and "dry_order" in r]
        self.assertEqual(len(dry), 2)

    def test_stale_intent_blocks_then_owner_fills(self) -> None:
        def die(o):  # noqa: ANN001  - the order is live, then the Pi dies before 15:28/15:35
            raise SystemExit("power cut")
        with self.assertRaises(SystemExit):
            self.run_day(fill=die)
        self.new_day(dt.date(2026, 12, 2))
        self.run_day()
        lg = L.replay(self.j.read())
        self.assertEqual(len(lg.unresolved), 1)
        self.assertEqual(self.k.calls.count("place"), 1)          # nothing new while unresolved
        iid = lg.unresolved[0]
        (self.paths.inbox / "f.json").write_text(json.dumps({"kind": "cmd", "cmd": "filled", "args": f"{iid} 12 2001"}))
        E.ingest_inbox(self.env())
        lg = L.replay(self.j.read())
        self.assertEqual(lg.unresolved, [])
        self.assertEqual(lg.positions["2612-01"].filled_qty, 12)

    def test_rerun_after_crash_between_vet_and_stage(self) -> None:
        calls = []

        def vet_then_crash(step, vin, sk):  # noqa: ANN001
            calls.append(1)
            if len(calls) == 1:
                return V.Verdict(True, "", {i["intent_id"]: ("DEFER", "DEFER_PRICE_GAP", "") for i in vin["intents"]},
                                 "s")
            return go_vet(step, vin, sk)
        self.vet = vet_then_crash
        orig = E.apply_verdict

        def crash_after(*a, **k):  # noqa: ANN001
            r = orig(*a, **k)
            raise SystemExit("crash before STAGE")
        E.apply_verdict = crash_after
        try:
            with self.assertRaises(SystemExit):
                self.run_day()
        finally:
            E.apply_verdict = orig
        self.run_day()                                            # resume: must not poison the journal
        lg = L.replay(self.j.read())
        self.assertTrue(all(p.sessions_seen.count("2026-12-01") == 1 for p in lg.positions.values()))


class TestThreeWay(Harness):
    def test_three_way_ok_then_manual_sell_flagged(self) -> None:
        def fill(o):  # noqa: ANN001
            o.status, o.filled_quantity, o.average_price = "COMPLETE", o.quantity, o.price
            self.k.trades_.append({"trade_id": f"t{o.order_id}", "order_id": o.order_id, "quantity": o.quantity,
                                   "average_price": o.price, "exchange_timestamp": "x"})
            self.k.holdings_qty[f"INE{int(o.tradingsymbol[3:]):09d}"] = o.quantity
        self.run_day(fill=fill)
        self.assertEqual(E.three_way(self.env(), {}, "2026-12-01"), [])
        self.k.holdings_qty["INE000000001"] -= 5                 # someone sold book shares by hand
        self.assertEqual(len(E.three_way(self.env(), {}, "2026-12-01")), 2)


class TestCAToday(Harness):
    ca_rows = {2: [{"ex_date": "2026-12-01", "purpose": "Bonus 1:1"}]}

    def test_ca_exdate_defers_that_buy_only(self) -> None:
        lg = self.run_day()
        self.assertEqual(self.k.calls.count("place"), 1)
        p2 = lg.positions["2612-02"]
        self.assertEqual((p2.tries_used, p2.leg), (0, "PENDING_BUY"))
        self.new_day(dt.date(2026, 12, 2))
        self.run_day()
        self.assertEqual(self.k.calls.count("place"), 3)          # SYM1 retry + SYM2 now


class TestFailures(Harness):
    def test_vet_down_no_order_no_try(self) -> None:
        self.vet = lambda s, v, k: V.Verdict(False, "timeout", {}, "")
        lg = self.run_day()
        self.assertNotIn("place", self.k.calls)
        self.assertTrue(all(p.tries_used == 0 and p.leg == "PENDING_BUY" for p in lg.positions.values()))

    def test_telegram_down_no_start(self) -> None:
        self.tg_ok = False
        self.run_day()
        self.assertNotIn("place", self.k.calls)

    def test_lost_reply_found_by_tag(self) -> None:
        self.k.lose_reply_next = True
        lg = self.run_day()
        # the lost reply is on the probe order: the other buys wait (probe rule); nothing is ever duplicated
        self.assertEqual(len(self.k.orders_), 1)
        i1 = [i for i in lg.intents.values() if i.position_id == "2612-01"]
        self.assertEqual(len(i1), 1)
        self.assertNotEqual(i1[0].status, "TIMEOUT")         # FOUND by tag at the reconcile
        self.assertEqual(i1[0].order_id, self.k.orders_[0].order_id)
        self.assertEqual(lg.positions["2612-02"].tries_used, 0)
        self.assertEqual(lg.unresolved, [])
        self.assertEqual(self.k.orders_[0].status, "CANCELLED")   # FOUND before 15:28, so it was cancelled

    def test_probe_failure_stops_rest(self) -> None:
        self.k.reject_next = "Some unknown rejection"
        lg = self.run_day()
        self.assertEqual(self.k.calls.count("place"), 1)
        self.assertIn("OWNER_HOLD", lg.positions["2612-01"].overlays)
        self.assertEqual(lg.positions["2612-02"].leg, "PENDING_BUY")

    def test_unfilled_ladder_cancel_and_window(self) -> None:
        lg = self.run_day()
        self.assertEqual(self.k.calls.count("modify"), 0)    # ltp unchanged: the limit is already the best price
        self.assertEqual(self.k.calls.count("cancel"), 2)
        self.assertTrue(all(p.tries_used == 1 for p in lg.positions.values()))
        for k in range(2, 7):                                # sessions 2..6
            self.new_day(self.day + dt.timedelta(days=k))
            lg = self.run_day()
        self.assertTrue(all(p.leg == "SKIPPED" for p in lg.positions.values()), [p.tries_used for p in
                                                                                 lg.positions.values()])
        self.assertEqual(lg.cash_paise, 50_000_000)
        self.assertEqual(lg.earmarked_paise(), 0)

    def test_ladder_reprices_up_to_cap(self) -> None:
        def bump(o):  # noqa: ANN001
            self.k.ltp["NSE:SYM1"] = 2010.0
            self.k.ltp["NSE:SYM2"] = 520.0                     # +4%: capped at ltp_1510 x 1.010
        self.k.on_place = bump
        E.afternoon(self.env(), {}, lambda: [])
        self.k.on_place = None
        recs = self.j.read()
        mods = [r for r in recs if r["type"] == "MODIFY_RESULT" and r["outcome"] == "OK"]
        self.assertTrue(mods)
        caps = {r["intent_id"]: r["cap_price_paise"] for r in recs if r["type"] == "INTENT"}
        for r in mods:
            self.assertLessEqual(r["new_limit_paise"], caps[r["intent_id"]])

    def test_partial_fill_remainder(self) -> None:
        def half(o):  # noqa: ANN001
            if o.tradingsymbol == "SYM2":
                o.filled_quantity, o.average_price = 20, o.price
                self.k.trades_.append({"trade_id": f"t{o.order_id}", "order_id": o.order_id, "quantity": 20,
                                       "average_price": o.price, "exchange_timestamp": "x"})
        lg = self.run_day(fill=half)
        p2 = lg.positions["2612-02"]
        self.assertEqual((p2.filled_qty, p2.leg, p2.tries_used), (20, "PENDING_BUY", 1))
        self.new_day(dt.date(2026, 12, 2))
        self.run_day()
        ints = [i for i in L.replay(self.j.read()).intents.values() if i.position_id == "2612-02"]
        self.assertEqual(len(ints), 2)
        self.assertLessEqual(ints[1].qty * ints[1].limit_paise, p2.remaining_paise)

    def test_qty_zero_skipped(self) -> None:
        self.k.ltp["NSE:SYM1"] = 30000.0
        lg = self.run_day()
        self.assertEqual(lg.positions["2612-01"].leg, "SKIPPED")

    def test_kill_cancels_by_tag_only(self) -> None:
        self.run_day()
        self.new_day(dt.date(2026, 12, 2))
        from momentum.fakebroker import FakeOrder
        self.k.orders_.append(FakeOrder("555", "fifty", "XYZ", "BUY", 1, 10.0))
        self.k.orders_.append(FakeOrder("556", "M2612B01A9", "SYM1", "BUY", 1, 10.0))
        n = E.kill_cancel(self.env())
        self.assertEqual(n, 0)                                # 556 is not in the journal: alert, never cancelled
        self.assertEqual([o.status for o in self.k.orders_], ["OPEN", "OPEN"])

    def test_owner_hold_and_buy_tap(self) -> None:
        env = self.env()
        lg = L.replay(self.j.read())
        E.hold_leg(env, lg.positions["2612-01"], "OWNER_HOLD", "test")
        aid = [r for r in self.j.read() if r["type"] == "APPROVAL_OPEN"][-1]["approval_id"]
        (self.paths.inbox / "1.json").write_text(json.dumps({"kind": "tap", "approval_id": aid, "choice": "buy"}))
        (self.paths.inbox / "2.json").write_text(json.dumps({"kind": "tap", "approval_id": aid, "choice": "skip"}))
        E.ingest_inbox(env)
        lg = L.replay(self.j.read())
        self.assertNotIn("OWNER_HOLD", lg.positions["2612-01"].overlays)
        self.assertEqual(lg.positions["2612-01"].leg, "PENDING_BUY")      # second tap ignored ("already done")
        self.assertTrue(any("already done" in t for t, _ in self.sent))


if __name__ == "__main__":
    unittest.main()

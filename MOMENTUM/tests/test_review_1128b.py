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


class TestM1PowerCutBeforeTheCall(TE.Harness):
    def test_sent_with_no_order_after_the_close_is_absent_not_an_owner_item(self) -> None:
        orig = self.k.place

        def cut(**kw):  # noqa: ANN003, ANN202
            raise SystemExit("power cut between SENT and the call")
        self.k.place = cut
        with self.assertRaises(SystemExit):
            self.run_day()
        self.k.place = orig
        self.clock.t = util.at_today("15:40")              # the 15:40 cron slot resumes the same day
        self.run_day()
        lg = L.replay(self.j.read())
        self.assertEqual(lg.unresolved, [])
        self.assertIn("ORPHAN_ABSENT", [r["code"] for r in self.j.read() if r["type"] == "ALERT"])
        self.assertTrue(any(i.status == "ABSENT" for i in lg.intents.values()))

    def test_an_order_that_reached_kite_is_found_not_absent(self) -> None:
        def lost(o):  # noqa: ANN001, ANN202
            raise SystemExit("power cut after the call")
        with self.assertRaises(SystemExit):
            self.run_day(fill=lost)
        self.k.on_place = None
        self.clock.t = util.at_today("15:40")
        self.run_day()
        lg = L.replay(self.j.read())
        self.assertEqual(lg.unresolved, [])
        self.assertFalse(any(i.status == "ABSENT" for i in lg.intents.values()))


class TestM2SignalDay(unittest.TestCase):
    """Review 11-28 M2: the Pi off the whole signal evening, or a closure the holiday file missed on the last weekday."""

    def setUp(self) -> None:
        import argparse
        import shutil
        import tempfile
        from momentum import cli
        self.cli, self.argparse = cli, argparse
        self.home = Path(tempfile.mkdtemp())
        (self.home / "config").mkdir()
        shutil.copy(HOME / "config" / "momentum.defaults.json", self.home / "config" / "momentum.defaults.json")
        (self.home / "hol.json").write_text(json.dumps({"2026": [{"date": "2026-12-25", "name": "x"}], "2027": []}))
        (self.home / "config" / "momentum.json").write_text(json.dumps({"mode": "live", "holiday_file": "hol.json"}))
        (self.home / "control").mkdir()
        (self.home / "control" / "ARM.json").write_text(json.dumps(
            {"phase": "P0", "plan_months": ["2026-12", "2027-01"], "mode": "automatic",
             "buy_cap_rupees_per_session": 220000, "valid_from": "2026-11-30", "valid_to": "2027-03-31"}))

    def tearDown(self) -> None:
        util.set_clock(None)

    def at(self, y: int, mo: int, d: int, h: int = 9, mi: int = 10) -> None:
        util.set_clock(lambda: dt.datetime(y, mo, d, h, mi, tzinfo=util.IST))

    def ctx(self):  # noqa: ANN201
        return self.cli.Ctx(self.argparse.Namespace(home=str(self.home), dry=False))

    def test_pi_off_on_the_signal_evening_the_morning_finds_the_day(self) -> None:
        import test_core
        self.at(2026, 12, 2)
        self.assertEqual(self.cli.missed_signal_day(self.ctx()), dt.date(2026, 11, 30))   # December plan missing
        test_core.opened(self.ctx().journal)                                             # the December plan
        self.at(2026, 12, 31, 21, 0)
        self.assertIsNone(self.cli.missed_signal_day(self.ctx()))          # the signal evening itself: not missed
        self.at(2027, 1, 1)
        self.assertEqual(self.cli.missed_signal_day(self.ctx()), dt.date(2026, 12, 31))

    def test_kite_last_session_earlier_repoints_the_pending_day(self) -> None:
        from momentum import evening as EV
        from momentum import pipeline as P
        from momentum.fakebroker import FakeKite
        self.at(2026, 12, 31, 20, 0)
        x = self.ctx()
        orig = (self.cli.check_manifest, P.import_research, EV.book_open, EV.run_signal_evening)
        x.token = lambda: object()
        x.api = lambda tok: FakeKite()
        x.notify = lambda text, buttons=None: True

        def mismatch(*a, **kw):  # noqa: ANN002, ANN003, ANN202
            raise EV.SignalDayMismatch("signal day 2026-12-31 is not Kite's last", "2026-12-30")
        self.cli.check_manifest, P.import_research = (lambda h, c: []), (lambda *a: None)
        EV.book_open, EV.run_signal_evening = (lambda env: None), mismatch
        try:
            rc = self.cli.cmd_evening(x, self.argparse.Namespace(pending_only=False, as_of_signal=None,
                                                                 skip_fetch=True))
        finally:
            self.cli.check_manifest, P.import_research, EV.book_open, EV.run_signal_evening = orig
        self.assertEqual(rc, 3)
        pend = json.loads((x.paths.refresh / "pending_signal.json").read_text(encoding="utf-8"))
        self.assertEqual(pend["signal_day"], "2026-12-30")
        self.assertIn("SIGNAL_DAY_MOVED", [r["code"] for r in x.journal.read() if r["type"] == "ALERT"])


class TestM3KillWithADamagedJournal(TestM2SignalDay):
    def test_kill_cancel_works_from_kite_when_the_journal_is_torn(self) -> None:
        from momentum.fakebroker import FakeKite
        self.at(2026, 12, 1, 15, 12)
        x = self.ctx()
        x.journal.append("NOTE", text="x")
        with open(x.paths.journal, "a", encoding="utf-8") as f:
            f.write('{"seq": 2, "ty')                             # a torn last line
        k = FakeKite(ltp={"NSE:A": 100.0})
        for tag in ("M2612B01A1", "FIFTY123"):
            k.place(tag=tag, tradingsymbol="A", transaction_type="BUY", quantity=1, price=100.0)
        Ctx = self.cli.Ctx
        orig = (Ctx.token, Ctx.api, Ctx.notify)
        sent = []
        Ctx.token, Ctx.api = (lambda self: object()), (lambda self, tok: k)
        Ctx.notify = lambda self, text, buttons=None: sent.append(text) or True
        try:
            rc = self.cli.main(["kill-cancel", "--home", str(self.home)])
        finally:
            Ctx.token, Ctx.api, Ctx.notify = orig
        self.assertEqual(rc, 0)
        self.assertEqual([(o.tag, o.status) for o in k.orders_], [("M2612B01A1", "CANCELLED"), ("FIFTY123", "OPEN")])
        self.assertTrue(any("journal unreadable" in t for t in sent))


class TestM6FastTelegramInTheOrderWindow(unittest.TestCase):
    def test_one_short_try_when_fast_full_retries_otherwise(self) -> None:
        from momentum import telegram as TG
        calls = []

        class Sess:
            def post(self, url, data=None, files=None, timeout=None):  # noqa: ANN001, ANN201
                calls.append(timeout)
                raise OSError("telegram hangs")
        b = TG.Bot("t", "c", 3, 0, session=Sess())
        b.fast = True
        self.assertFalse(b.send("x"))
        self.assertEqual(calls, [8])
        calls.clear()
        b.fast = False
        self.assertFalse(b.send("x"))
        self.assertEqual(calls, [30, 30, 30])


class TestSoldClosesAPendingBuy(TE.Harness):
    def test_sold_on_a_partly_bought_leg_never_buys_the_rest(self) -> None:
        def half(o):  # noqa: ANN001, ANN202
            q = o.quantity // 2
            o.status, o.filled_quantity, o.average_price = "CANCELLED", q, o.price
            self.k.trades_.append({"trade_id": f"t{o.order_id}", "order_id": o.order_id, "quantity": q,
                                   "average_price": o.price, "exchange_timestamp": "x"})
        lg = self.run_day(fill=half)
        p = lg.positions["2612-01"]
        self.assertEqual(p.leg, "PENDING_BUY")
        E.apply_cmd(self.env(), "sold", f"{p.isin} {p.held_qty} 2000")
        lg = L.replay(self.j.read())
        self.assertEqual((lg.positions["2612-01"].leg, lg.positions["2612-01"].held_qty), ("NONE", 0))


class TestL4ConfigOverlay(unittest.TestCase):
    def test_unknown_overlay_key_is_refused_and_nested_blocks_merge(self) -> None:
        import shutil
        import tempfile
        from momentum import config as CF
        home = Path(tempfile.mkdtemp())
        (home / "config").mkdir()
        shutil.copy(HOME / "config" / "momentum.defaults.json", home / "config" / "momentum.defaults.json")
        (home / "config" / "momentum.json").write_text(json.dumps({"test_lot": {"qty": 3}}))
        cfg = CF.load(home)
        self.assertEqual(cfg["test_lot"]["qty"], 3)
        self.assertEqual(cfg["test_lot"]["buy_day"], "2026-12-09")            # the rest of the block is kept
        (home / "config" / "momentum.json").write_text(json.dumps({"gate_wait_sesions": 3}))
        with self.assertRaises(CF.ConfigError):
            CF.load(home)


class TestL3TapReappliedAfterACrash(TE.Harness):
    def test_a_tap_journalled_before_a_crash_is_applied_on_the_rerun(self) -> None:
        p = L.replay(self.j.read()).positions["2612-01"]
        E.hold_leg(self.env(), p, "OWNER_HOLD", "planted")
        aid = [r for r in self.j.read() if r["type"] == "APPROVAL_OPEN"][-1]["approval_id"]
        (self.paths.inbox / "t1.json").write_text(json.dumps({"kind": "tap", "approval_id": aid, "choice": "buy"}))
        orig = E.apply_tap
        E.apply_tap = lambda env, ap, choice: (_ for _ in ()).throw(SystemExit("crash after OWNER_TAP"))
        try:
            with self.assertRaises(SystemExit):
                E.ingest_inbox(self.env())
        finally:
            E.apply_tap = orig
        self.assertIn("OWNER_HOLD", L.replay(self.j.read()).positions["2612-01"].overlays)
        E.ingest_inbox(self.env())                                            # the file is still in the inbox
        self.assertNotIn("OWNER_HOLD", L.replay(self.j.read()).positions["2612-01"].overlays)


class TestL8RejectHandledAfterACrash(TE.Harness):
    def test_a_rejection_journalled_before_a_crash_is_still_held(self) -> None:
        orig_recon = E.reconcile_stage

        def recon(env, day):  # noqa: ANN001, ANN202
            for o in self.k.orders_:                        # the order book shows the orders REJECTED by 15:35
                if not o.filled_quantity:
                    o.status, o.status_message = "REJECTED", "RMS:Margin Exceeds"
            return orig_recon(env, day)
        E.reconcile_stage = recon
        self.addCleanup(lambda: setattr(E, "reconcile_stage", orig_recon))
        orig = E.after_reject
        calls = []

        def crash_once(env, p, status, iid):  # noqa: ANN001, ANN202
            calls.append(iid)
            if len(calls) == 1 and self.clock() >= util.at_today("15:35"):
                raise SystemExit("crash after ORDER_STATUS")
            return orig(env, p, status, iid)
        E.after_reject = crash_once
        try:
            try:
                self.run_day()
            except SystemExit:
                pass
            self.clock.t = util.at_today("15:40")
            self.run_day()
        finally:
            E.after_reject = orig
        notes = [r for r in self.j.read() if r["type"] == "NOTE" and r.get("reject_handled")]
        sync_rej = [r for r in self.j.read() if r["type"] == "ORDER_STATUS" and r.get("via") == "sync"
                    and str(r["status"]).startswith("REJECTED_")]
        self.assertTrue(sync_rej, "the planted rejection never reached the sync path: the test proves nothing")
        self.assertEqual(len(notes), len(sync_rej))


if __name__ == "__main__":
    unittest.main()

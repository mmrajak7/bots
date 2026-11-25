"""Core tests: journal durability rules, ledger replay, the broker's duplicate-order guard (spec §K T4 subset)."""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from momentum import broker as B  # noqa: E402
from momentum import control as C  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import util  # noqa: E402
from momentum.fakebroker import FakeKite  # noqa: E402
from momentum.journal import Journal, JournalError  # noqa: E402

T0 = dt.datetime(2026, 12, 1, 15, 10, tzinfo=util.IST)
PLAN = {"month": "2026-12", "signal_day": "2026-11-30", "data_rev": 0}


def opened(j: Journal, n: int = 2) -> None:
    j.append("BOOK_OPEN", cash_paise=50_000_000, units=5000, nav_per_unit_paise=10000, config_commit="test")
    j.append("PLAN_FINAL", plan_id=PLAN, decision_file="d.json", decision_sha256="x", audit_status="PASSED",
             buys=[{"position_id": f"2612-{k:02d}", "isin": f"INE00000{k:04d}", "symbol": f"SYM{k}",
                    "series": "EQ", "rank": k, "earmark_paise": 2_492_500} for k in range(1, n + 1)], holds=[])


def intent_args(k: int = 1, attempt: int = 1, qty: int = 10, limit: float = 2000.0) -> dict:
    pid = f"2612-{k:02d}"
    return dict(intent_id=f"2026-12|2026-11-30|r0|{pid}|buy|{attempt}",
                key={"plan_id": PLAN, "position_id": pid, "leg": "buy", "attempt": attempt},
                tag=B.make_tag("2612", "buy", k, attempt), isin=f"INE00000{k:04d}", tradingsymbol=f"SYM{k}",
                qty=qty, limit=limit, ltp_1510=1990.0, cap_price=2009.9, max_rupees=24925)


class Base(unittest.TestCase):
    def setUp(self) -> None:
        util.set_clock(lambda: T0)
        self.tmp = Path(tempfile.mkdtemp())
        self.j = Journal(self.tmp / "journal.jsonl")
        self.k = FakeKite()
        self.armed, self.kill = True, False
        self.b = B.Broker(self.k, self.j, B.Gate(live=True, armed=lambda: self.armed, killed=lambda: self.kill))

    def tearDown(self) -> None:
        util.set_clock(None)


class TestJournal(Base):
    def test_seq_and_torn_line(self) -> None:
        self.j.append("NOTE", text="a")
        self.j.append("NOTE", text="b")
        self.assertEqual([r["seq"] for r in self.j.read()], [1, 2])
        with open(self.j.path, "a", encoding="utf-8") as f:
            f.write('{"seq": 3, "type": "NO')               # torn write
        with self.assertRaises(JournalError):
            Journal(self.j.path)

    def test_unknown_type_refused(self) -> None:
        with self.assertRaises(JournalError):
            self.j.append("WHATEVER")


class TestLedger(Base):
    def test_fill_moves_cash(self) -> None:
        opened(self.j)
        self.j.append("INTENT", **{k: v for k, v in dict(
            intent_id="i1", key={"position_id": "2612-01"}, tag="M2612B01A1", isin="INE000000001",
            tradingsymbol="SYM1", side="BUY", qty=10, limit_paise=200000).items()})
        self.j.append("FILL", position_id="2612-01", order_id="1", trade_id="t1", qty=10, price_paise=199500,
                      charges_paise=3000)
        lg = L.replay(self.j.read())
        self.assertEqual(lg.cash_paise, 50_000_000 - 1_995_000 - 3000)
        self.assertEqual(lg.positions["2612-01"].state, "HELD")
        self.assertEqual(lg.positions["2612-01"].remaining_paise, 2_492_500 - 1_995_000)

    def test_double_trade_refused(self) -> None:
        opened(self.j)
        for _ in range(2):
            self.j.append("FILL", position_id="2612-01", order_id="1", trade_id="t1", qty=1, price_paise=100,
                          charges_paise=0)
        with self.assertRaises(L.LedgerError):
            L.replay(self.j.read())

    def test_bad_leg_move_refused(self) -> None:
        opened(self.j)
        self.j.append("LEG", position_id="2612-01", **{"from": "NONE", "to": "PENDING_BUY"}, reason="x")
        with self.assertRaises(L.LedgerError):
            L.replay(self.j.read())


class TestBrokerGuard(Base):
    def test_place_once(self) -> None:
        opened(self.j)
        i = self.b.place_buy(**intent_args())
        self.assertEqual(i.status, "ACK")
        with self.assertRaises(B.BrokerRefused):           # same intent again
            self.b.place_buy(**intent_args())
        with self.assertRaises(B.BrokerRefused):           # a new attempt while the first is live
            self.b.place_buy(**intent_args(attempt=2))
        self.assertEqual(self.k.calls.count("place"), 1)

    def test_lost_reply_never_duplicates(self) -> None:
        """The order reaches the exchange but the reply is lost: TIMEOUT, and no second order on a re-run."""
        opened(self.j)
        self.k.lose_reply_next = True
        i = self.b.place_buy(**intent_args())
        self.assertEqual(i.status, "TIMEOUT")
        with self.assertRaises(B.BrokerRefused):
            self.b.place_buy(**intent_args(attempt=2))
        self.assertEqual(len(self.k.orders_), 1)

    def test_raw_requests_timeout_is_timeout_not_reject(self) -> None:
        """kiteconnect lets requests' exceptions through unwrapped; the order may exist -> TIMEOUT, never REJECTED."""
        import requests
        opened(self.j)
        self.k.raw_error_next = requests.exceptions.ReadTimeout("read timed out")
        self.assertEqual(self.b.place_buy(**intent_args()).status, "TIMEOUT")
        self.k.raw_error_next = RuntimeError("something odd")
        self.assertEqual(self.b.place_buy(**intent_args(k=2)).status, "TIMEOUT")

    def test_order_book_tag_blocks_even_with_empty_journal(self) -> None:
        """A wiped / restored journal must not lead to a second order: the tag in today's order book blocks it."""
        opened(self.j)
        self.k.orders_.append(__import__("momentum.fakebroker", fromlist=["FakeOrder"]).FakeOrder(
            "77", "M2612B01A1", "SYM1", "BUY", 10, 2000.0))
        with self.assertRaises(B.BrokerRefused):
            self.b.place_buy(**intent_args(attempt=2))
        self.assertNotIn("place", self.k.calls)

    def test_kill_and_arm(self) -> None:
        opened(self.j)
        self.kill = True
        with self.assertRaises(B.BrokerRefused):
            self.b.place_buy(**intent_args())
        self.kill, self.armed = False, False
        with self.assertRaises(B.BrokerRefused):
            self.b.place_buy(**intent_args())
        self.assertNotIn("place", self.k.calls)
        self.assertEqual(L.replay(self.j.read()).intents, {})

    def test_rupee_cap(self) -> None:
        opened(self.j)
        with self.assertRaises(B.BrokerRefused):
            self.b.place_buy(**intent_args(qty=13, limit=2000.0))      # 26,000 > 24,925

    def test_reject_classified(self) -> None:
        opened(self.j)
        self.k.reject_next = "Insufficient funds. Required margin is 24000"
        self.assertEqual(self.b.place_buy(**intent_args()).status, "REJECTED_RMS")

    def test_tags(self) -> None:
        self.assertEqual(B.make_tag("2612", "buy", 7, 1), "M2612B07A1")
        with self.assertRaises(ValueError):
            B.make_tag("2612", "buy", 7, 10)


class TestControl(Base):
    def test_arm(self) -> None:
        p = self.tmp / "ARM.json"
        self.assertIsNone(C.read_arm(p)[0])
        good = {"phase": "P0", "plan_months": ["2026-12"], "mode": "automatic",
                "buy_cap_rupees_per_session": 550000, "valid_from": "2026-11-30", "valid_to": "2026-12-31"}
        p.write_text(json.dumps(good))
        a, why = C.read_arm(p)
        self.assertTrue(a and a.covers("2026-12", dt.date(2026, 12, 1)), why)
        self.assertFalse(a.covers("2027-01", dt.date(2026, 12, 1)))
        p.write_text(json.dumps({**good, "extra": 1}))
        self.assertIsNone(C.read_arm(p)[0])
        p.write_text(json.dumps({**good, "buy_cap_rupees_per_session": "550000"}))
        self.assertIsNone(C.read_arm(p)[0])

    def test_token(self) -> None:
        p = self.tmp / "tok.json"
        base = {"api_key": "k", "access_token": "a", "user_id": "QSK814", "valid_until": "x"}
        p.write_text(json.dumps({**base, "generated_at": "2026-12-01T08:50:52.1+05:30"}))
        self.assertIsNotNone(C.read_token(p, "QSK814")[0])
        p.write_text(json.dumps({**base, "generated_at": "2026-11-30T08:50:52+05:30"}))     # yesterday
        self.assertIsNone(C.read_token(p, "QSK814")[0])
        p.write_text(json.dumps({**base, "generated_at": "2026-12-01T05:59:00+05:30"}))     # before 06:00
        self.assertIsNone(C.read_token(p, "QSK814")[0])
        p.write_text(json.dumps({**base, "user_id": "YL6478", "generated_at": "2026-12-01T08:50:00+05:30"}))
        self.assertIsNone(C.read_token(p, "QSK814")[0])
        p.write_text('{"api_key": "k", "access_')                                         # torn write
        self.assertIsNone(C.read_token(p, "QSK814")[0])

    def test_yl6478_naive_timestamp_is_ist(self) -> None:
        """The personal account's token file (Helper bots) writes generated_at without a zone: read as IST."""
        p = self.tmp / "tok.json"
        p.write_text(json.dumps({"api_key": "k", "access_token": "a", "user_id": "YL6478", "valid_until": "x",
                                 "generated_at": "2026-12-01T08:45:05.239900"}))
        self.assertIsNotNone(C.read_token(p, "YL6478")[0])
        self.assertIsNone(C.read_token(p, "QSK814")[0])

    def test_fifty_kill_unreadable_is_active(self) -> None:
        self.assertTrue(C.fifty_kill_switch(self.tmp / "nope.db")[0])


if __name__ == "__main__":
    unittest.main()

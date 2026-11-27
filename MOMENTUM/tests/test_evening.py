"""Evening helpers: signal-day guess, earmark, F&O additions filter (the 11-25 dry-run bug), vet answer validation."""
import datetime as dt
import json
import sys
import tempfile
import types
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from momentum import evening as EV  # noqa: E402
from momentum import pipeline as P  # noqa: E402
from momentum import vet as V  # noqa: E402


class TestEvening(unittest.TestCase):
    def test_signal_day(self) -> None:
        hol = {"2026-12-25"}
        self.assertTrue(EV.is_signal_day(dt.date(2026, 11, 30), hol))        # Mon; next session 1 Dec
        self.assertFalse(EV.is_signal_day(dt.date(2026, 11, 27), hol))       # Fri; next session 30 Nov
        self.assertTrue(EV.is_signal_day(dt.date(2027, 1, 29), hol))         # Fri; next session Mon 1 Feb
        self.assertTrue(EV.is_signal_day(dt.date(2026, 12, 31), hol))
        self.assertFalse(EV.is_signal_day(dt.date(2026, 11, 29), hol))       # Sunday

    def test_earmark(self) -> None:
        self.assertEqual(EV.earmark_paise(50_000_000, 20, Decimal("0.003")), 2_492_500)

    def test_announced_only_this_month(self) -> None:
        tmp = Path(tempfile.mkdtemp()) / "fo_announced.json"
        tmp.write_text(json.dumps([
            {"symbol": "OLDIN", "member_month": "2023-03"},                  # old addition (may have left F&O since)
            {"symbol": "NEWIN", "member_month": "2026-12"},
            {"symbol": "LATER", "member_month": "2027-01"},
            {"symbol": "WD", "member_month": "2026-12", "withdrawn": True}]))
        M = types.SimpleNamespace(J=types.SimpleNamespace(ANNOUNCED_FILE=tmp))
        self.assertEqual(P.announced_additions(M, "2026-12"), {"NEWIN"})

    def test_month_add(self) -> None:
        self.assertEqual(P.month_add("2026-12", 1), "2027-01")
        self.assertEqual(P.month_add("2027-01", -1), "2026-12")


HOME = Path(__file__).resolve().parents[1]
NSE = (HOME / "../../NSE_DATA").resolve()


@unittest.skipUnless((NSE / "srs_base").exists(), "NSE data not on this machine")
class TestKiteWeekendSessions(unittest.TestCase):
    def test_weekend_kite_session_is_requested(self) -> None:
        """DRY-1 2026-11-27: Kite had the Sunday muhurat session 2026-11-08; the research fetcher's date list (weekdays
        + frozen calendar + EXTRA_WEEKEND to 2024) never asked NSE for it -> 'Kite sessions without an ok CM file'."""
        M = P.import_research(HOME / "pipeline", NSE)
        saved = (M.NF.END, list(M.NF.EXTRA_WEEKEND))
        try:
            w = P.Window("2026-12", dt.date(2026, 11, 27), Path(tempfile.mkdtemp()) / "d")
            self.assertNotIn("2026-11-08", M.NF.dates(set()))
            P.configure(M, w, [], ["2026-11-06", "2026-11-08", "2026-11-09"])
            ds = M.NF.dates(set())
            self.assertIn("2026-11-08", ds)
            self.assertNotIn("2026-11-07", ds)                # a weekend day Kite did not trade stays out
            self.assertIn("2023-11-12", ds)                   # the hand list is kept
        finally:
            M.NF.END, M.NF.EXTRA_WEEKEND = saved


class TestVetValidate(unittest.TestCase):
    ids = ["a", "b"]

    def test_good_and_off_menu(self) -> None:
        ans = {"step_plan_hash": "s", "facts_hash": "f", "orders": [
            {"intent_id": "a", "action": "GO", "reason_code": "GO_OK", "note": ""},
            {"intent_id": "b", "action": "GO", "reason_code": "SKIP_QTY0", "note": ""}]}
        acts, probs = V.validate(ans, self.ids, "s", "f", {})
        self.assertEqual(acts["a"][0], "GO")
        self.assertEqual(acts["b"][0], "HOLD")

    def test_hash_mismatch_all_hold(self) -> None:
        ans = {"step_plan_hash": "x", "facts_hash": "f", "orders": []}
        acts, _ = V.validate(ans, self.ids, "s", "f", {})
        self.assertTrue(all(a[0] == "HOLD" for a in acts.values()))

    def test_missing_duplicate_and_unconfirmed_skip(self) -> None:
        ans = {"step_plan_hash": "s", "facts_hash": "f", "orders": [
            {"intent_id": "a", "action": "SKIP", "reason_code": "SKIP_SUSPENDED", "note": ""}]}
        acts, _ = V.validate(ans, self.ids, "s", "f", {"a": None})
        self.assertEqual(acts["a"][0], "HOLD")                                # code did not confirm the skip
        self.assertEqual(acts["b"][0], "HOLD")                                # missing entry
        ans["orders"] = ans["orders"] * 2
        acts, _ = V.validate(ans, self.ids, "s", "f", {"a": "SKIP_SUSPENDED"})
        self.assertEqual(acts["a"][0], "HOLD")                                # duplicate entry

    def test_cli_wrapper_shapes(self) -> None:
        self.assertEqual(V.parse_cli_output(json.dumps({"structured_output": {"ok": True}})), {"ok": True})
        self.assertEqual(V.parse_cli_output(json.dumps({"result": "```json\n{\"ok\": true}\n```"})), {"ok": True})
        with self.assertRaises(ValueError):
            V.parse_cli_output(json.dumps({"is_error": True, "result": "boom"}))


if __name__ == "__main__":
    unittest.main()

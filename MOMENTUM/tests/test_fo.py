"""F&O entrant vet + day-1 backstop (frozen design D47, owner 2026-11-27).

Fixtures: four real NSE F&O circulars (pdftotext -layout, signature block trimmed), one per table layout seen
2020-2026. The parser was also checked on the Pi against all 36 circulars cited by fo_announced.json: 36/36.
"""
import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from momentum import config as CF  # noqa: E402
from momentum import engine as E  # noqa: E402
from momentum import evening as EV  # noqa: E402
from momentum import fo_vet as F  # noqa: E402
from momentum import ledger as L  # noqa: E402
from momentum import util  # noqa: E402

from test_engine import HOME, Harness  # noqa: E402

FIX = Path(__file__).resolve().parent / "data" / "fo_circulars"
SD, FS = "2026-11-30", "2026-12-01"               # signal day, first session of 2026-12


def fix(ref: str) -> str:
    return (FIX / f"{ref}.txt").read_text(encoding="utf-8")


def intro_text(syms, eff="December 01, 2026", date="November 16, 2026"):  # noqa: ANN001, ANN201
    rows = "\n".join(f"       {k}      {s:<20}                  {s.title()} Limited" for k, s in enumerate(syms, 1))
    return (f"    Department: FUTURES & OPTIONS\n    Download Ref No: NSE/FAOP/77100       Date: {date}\n\n"
            "   All Members,\n\n   Introduction of Futures & Options Contracts on Two Individual Securities\n\n"
            "   members are hereby notified that the futures and options contracts on following securities would be\n"
            f"   available for trading w.e.f. {eff}, subject to fulfilment of eligibility criteria.\n\n"
            f"    Sr. No.   Symbol                                Security Name\n{rows}\n")


def cand(ref: str, text: str, date: str) -> dict:
    return {"ref": ref, "date": date, "title": F.circular_title(text), "text": text}


def answer(*rows):  # noqa: ANN002, ANN201
    return {"circulars": [{"ref": r, "kind": k, "effective_date": e, "symbols": list(s)} for r, k, e, s in rows]}


EQ = {f"EQ{k}" for k in range(2000)} | {"NEWCO", "OTHERCO", "COROMANDEL", "CUB", "DATAPATTNS", "KARURVYSYA",
                                        "NAVINFLUOR", "SYRMA"}
OCT = ("FAOP76899", "introduction", "2026-10-28",
       ["COROMANDEL", "CUB", "DATAPATTNS", "KARURVYSYA", "NAVINFLUOR", "SYRMA"])
FO_TODAY = set(OCT[3])


class TestParser(unittest.TestCase):
    def test_four_layouts(self) -> None:
        exp = {"FAOP43744": ("2020-03-27", ["GODREJPROP"]),                       # symbol in the title
               "FAOP47161": ("2021-02-26", ["ALKEM", "AUBANK", "DEEPAKNTR", "IRCTC", "NAM-INDIA"]),  # name | symbol
               "FAOP50414": ("2021-12-31", ["ABCAPITAL", "HONAUT"]),              # names in capitals too
               "FAOP76899": ("2026-10-28", OCT[3])}                               # symbol | name (2022+)
        for ref, (eff, syms) in exp.items():
            cr = F.code_read(fix(ref))
            self.assertEqual((cr["effective_date"], cr["symbols"]), (eff, sorted(syms)), ref)
        self.assertEqual(F.circular_date(fix("FAOP76899")), "2026-10-14")
        self.assertIn("Six Individual Securities", F.circular_title(fix("FAOP76899")))

    def test_not_an_introduction(self) -> None:
        self.assertIsNone(F.code_read("Revised quantity freeze limits for index derivatives w.e.f. May 1, 2026"))

    def test_text_checks(self) -> None:
        t = fix("FAOP47161")
        self.assertTrue(F.symbol_in_text("NAM-INDIA", t))
        self.assertFalse(F.symbol_in_text("NAM", t))                 # a prefix of NAM-INDIA is not a match
        self.assertTrue(F.date_in_text("2021-02-26", t))
        self.assertFalse(F.date_in_text("2021-02-27", t))

    def test_answer_key(self) -> None:
        rows = [{"symbol": "A", "source": "https://nsearchives.nseindia.com/content/circulars/FAOP1.pdf"},
                {"symbol": "B", "source": "https://nsearchives.nseindia.com/content/circulars/FAOP1.pdf"},
                {"symbol": "C", "source": "press"}]
        self.assertEqual(F.answer_key(rows), {"FAOP1": {"A", "B"}})


class TestDecide(unittest.TestCase):
    def setUp(self) -> None:
        self.new = intro_text(["NEWCO", "OTHERCO"])
        self.cands = [cand("FAOP76899", fix("FAOP76899"), "2026-10-14"), cand("FAOP77100", self.new, "2026-11-16")]
        self.key = {"FAOP76899": set(OCT[3])}

    def run_(self, ans, cands=None, fo_today=FO_TODAY, eq=EQ):  # noqa: ANN001, ANN201
        return F.decide(ans, cands or self.cands, SD, FS, fo_today, eq, self.key, "2026-08-01", 30)

    def test_entrant_on_the_first_session(self) -> None:
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-01", ["NEWCO", "OTHERCO"])))
        self.assertEqual((r["status"], r["entrants"], r["problems"]), ("ok", ["NEWCO", "OTHERCO"], []))

    def test_already_in_the_bhavcopy_or_not_eq(self) -> None:
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-01", ["NEWCO", "OTHERCO"])),
                      fo_today=FO_TODAY | {"NEWCO"}, eq=EQ - {"OTHERCO"})
        self.assertEqual((r["status"], r["entrants"]), ("ok", []))
        self.assertTrue(any("OTHERCO" in x and "not an EQ symbol" in x for x in r["info"]))

    def test_later_in_the_month_is_next_months_member(self) -> None:
        late = intro_text(["NEWCO", "OTHERCO"], eff="December 29, 2026")
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-29", ["NEWCO", "OTHERCO"])),
                      cands=[self.cands[0], cand("FAOP77100", late, "2026-11-16")])
        self.assertEqual((r["status"], r["entrants"]), ("ok", []))
        self.assertTrue(any("after the first session" in x for x in r["info"]))

    def test_invented_symbol_fails(self) -> None:
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-01", ["NEWCO", "OTHERCO", "GHOST"])))
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("GHOST" in p for p in r["problems"]))
        self.assertEqual(r["entrants"], ["NEWCO", "OTHERCO"])         # the code's own reading still excludes

    def test_vet_calls_an_introduction_other(self) -> None:
        r = self.run_(answer(OCT, ("FAOP77100", "other", None, [])))
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["entrants"], ["NEWCO", "OTHERCO"])         # exclusion side wins: never a silent buy

    def test_wrong_date_fails(self) -> None:
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-02", ["NEWCO", "OTHERCO"])))
        self.assertEqual(r["status"], "failed")

    def test_answer_key_mismatch_fails(self) -> None:
        self.key = {"FAOP76899": set(OCT[3]) | {"MISSING"}}
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-01", ["NEWCO", "OTHERCO"])))
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("answer key" in p for p in r["problems"]))

    def test_missing_and_unknown_answers_fail(self) -> None:
        r = self.run_({"circulars": [{"ref": "FAOP1", "kind": "other", "effective_date": None, "symbols": []}]})
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("no answer" in p for p in r["problems"]))
        self.assertEqual(self.run_("not json")["status"], "failed")

    def test_withdrawal_removes(self) -> None:
        wd = ("   All Members,\n\n   Withdrawal of introduction of Futures & Options contracts on NEWCO\n\n"
              "   the introduction of futures and options contracts on NEWCO stands withdrawn.\n")
        cands = self.cands + [cand("FAOP77200", wd, "2026-11-25")]
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-01", ["NEWCO", "OTHERCO"]),
                             ("FAOP77200", "withdrawal", None, ["NEWCO"])), cands=cands)
        self.assertEqual((r["status"], r["entrants"]), ("ok", ["OTHERCO"]))

    def test_liveness_needs_an_introduction(self) -> None:
        other = "   All Members,\n\n   Exclusion of something about securities\n"
        r = F.decide(answer(("FAOP9", "other", None, [])), [cand("FAOP9", other, "2026-11-01")], SD, FS, FO_TODAY,
                     EQ, {}, "2026-08-01", 30)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("scan looks broken" in p for p in r["problems"]))

    def test_started_but_not_in_the_bhavcopy_is_reported(self) -> None:
        r = self.run_(answer(OCT, ("FAOP77100", "introduction", "2026-12-01", ["NEWCO", "OTHERCO"])),
                      fo_today=FO_TODAY - {"CUB"})
        self.assertTrue(any("CUB" in x and "not in the signal-day" in x for x in r["info"]))


class FakeNSE:
    """RSS + FAOP<n>.pdf server. pdf bodies are '%PDF-' + text (to_text strips the marker)."""

    def __init__(self, top: int, pdfs: dict) -> None:
        self.top, self.pdfs, self.fail_at, self.calls = top, dict(pdfs), None, []

    def get(self, url: str):  # noqa: ANN201
        self.calls.append(url)
        if url.endswith(".xml"):
            items = "".join(f"<link>https://nsearchives.nseindia.com/content/circulars/CMPT{n}.pdf</link>"
                            for n in range(self.top - 4, self.top + 1))
            return 200, items.encode()
        n = int(url.rsplit("FAOP", 1)[1][:-4])
        if self.fail_at == n:
            raise F.FoVetError("planted network failure")
        if n in self.pdfs:
            return 200, b"%PDF-" + self.pdfs[n].encode()
        return 404, b""


CFG = {"fo_scan_seed": 77100, "fo_rss_url": "https://x/Circulars.xml", "fo_circular_base": "https://x/circulars/",
       "fo_scan_settle_days": 7, "fo_scan_pause_s": 0}


class TestScan(unittest.TestCase):
    def setUp(self) -> None:
        util.set_clock(lambda: dt.datetime(2026, 11, 30, 20, 0, tzinfo=util.IST))
        self.d = Path(tempfile.mkdtemp())
        self.nse = FakeNSE(77110, {77103: fix("FAOP76899")})

    def tearDown(self) -> None:
        util.set_clock(None)

    def scan(self):  # noqa: ANN201
        return F.scan(self.d, CFG, get=self.nse.get, to_text=lambda b: b[5:].decode())

    def test_scan_resume_and_settle(self) -> None:
        self.nse.fail_at = 77106
        r = self.scan()
        self.assertFalse(r["complete"])
        self.assertIn("planted", r["stopped"])
        self.assertEqual(F.load_scan(self.d, 0)["next_n"], 77106)
        self.nse.fail_at = None
        self.nse.pdfs[77104] = intro_text(["NEWCO"])                   # uploaded late: a recent 404 is asked again
        n_calls = len(self.nse.calls)
        self.scan()                                                   # same day: the miss is not asked again
        self.assertNotIn("https://x/circulars/FAOP77104.pdf", self.nse.calls[n_calls:])
        util.set_clock(lambda: dt.datetime(2026, 12, 1, 19, 5, tzinfo=util.IST))
        r = self.scan()
        self.assertTrue(r["complete"])
        st = F.load_scan(self.d, 0)
        self.assertEqual((st["next_n"], sorted(st["hits"])), (77111, ["77103", "77104"]))
        self.assertEqual(st["hits"]["77103"]["date"], "2026-10-14")
        self.assertEqual(F.candidates(self.d, "2026-08-01", "2026-11-30", 60)[0]["ref"], "FAOP77103")

    def test_html_200_is_not_a_circular(self) -> None:
        def get(url):  # noqa: ANN001, ANN202
            return (200, b"<html>error</html>") if "FAOP" in url else self.nse.get(url)
        r = F.scan(self.d, CFG, get=get, to_text=lambda b: b.decode())
        self.assertTrue(r["complete"])
        self.assertEqual(F.load_scan(self.d, 0)["hits"], {})

    def test_tampered_text_refused(self) -> None:
        self.scan()
        (self.d / "FAOP77103.txt").write_text("changed", encoding="utf-8")
        with self.assertRaises(F.FoVetError):
            F.candidates(self.d, "2026-08-01", "2026-11-30", 60)

    def test_candidate_limit(self) -> None:
        self.scan()
        with self.assertRaises(F.FoVetError):
            F.candidates(self.d, "2026-08-01", "2026-11-30", 0)


class TestRunCheck(unittest.TestCase):
    def setUp(self) -> None:
        util.set_clock(lambda: dt.datetime(2026, 11, 30, 20, 0, tzinfo=util.IST))
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = CF.load(HOME)
        self.scan_dir = self.tmp / "scan"
        self.nse = FakeNSE(77110, {77103: fix("FAOP76899"), 77105: intro_text(["NEWCO"], eff="December 1, 2026")})
        self.calls = 0

    def tearDown(self) -> None:
        util.set_clock(None)

    def check(self, ans, final=False):  # noqa: ANN001, ANN201
        def classify(cs):  # noqa: ANN001, ANN202
            self.calls += 1
            return ans(cs) if callable(ans) else ans
        cfg = dict(self.cfg, fo_scan_seed=77100)
        return F.run_check(cfg, self.tmp / "fo", self.scan_dir, self.tmp / "vet", "2026-12", SD, FS, FO_TODAY, EQ,
                           [], final, classify=classify,
                           do_scan=lambda: F.scan(self.scan_dir, dict(CFG), get=self.nse.get,
                                                  to_text=lambda b: b[5:].decode()))

    def good(self):  # noqa: ANN201
        return answer(("FAOP77103",) + OCT[1:], ("FAOP77105", "introduction", "2026-12-01", ["NEWCO"]))

    def test_write_once(self) -> None:
        r1 = self.check(self.good())
        self.assertEqual((r1["status"], r1["entrants"], r1["fresh"]), ("ok", ["NEWCO"], True))
        r2 = self.check(lambda cs: 1 / 0)                         # a re-run never asks the vet again
        self.assertEqual((r2["status"], r2["entrants"], r2["fresh"], self.calls), ("ok", ["NEWCO"], False, 1))
        self.assertEqual(r1["sha256"], r2["sha256"])

    def test_failed_is_written_only_on_the_final_attempt(self) -> None:
        r = self.check("garbage")
        self.assertEqual((r["status"], r["final"]), ("failed", False))
        self.assertFalse((self.tmp / "fo" / f"2026-12_{SD}.json").exists())
        r = self.check(lambda cs: (_ for _ in ()).throw(F.FoVetError("CLI down")), final=True)
        self.assertEqual((r["status"], r["final"]), ("failed", True))
        self.assertTrue((self.tmp / "fo" / f"2026-12_{SD}.json").exists())

    def test_incomplete_scan_fails(self) -> None:
        self.nse.fail_at = 77107
        r = self.check(self.good())
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("scan incomplete" in p for p in r["problems"]))
        self.assertEqual(self.calls, 0)


class TestEveningGate(unittest.TestCase):
    """fo_entrants(): retry before give-up, FINAL + ALERT after it (owner D47)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = dict(CF.load(HOME), _nse_data=str(self.tmp / "nse"))
        self.paths = CF.paths(self.tmp, self.tmp / "state")
        self.paths.ensure()
        self.alerts, self.sent = [], []

    def tearDown(self) -> None:
        util.set_clock(None)

    def env(self, classify):  # noqa: ANN001, ANN201
        from momentum.journal import Journal
        env = types_env(self.cfg, self.paths, Journal(self.paths.journal), self.sent)
        env.fo_classify_fn = classify
        env.fo_scan_fn = lambda: {"complete": True, "rss_max": 1, "stopped": ""}
        return env

    def call(self, at: str, classify):  # noqa: ANN001, ANN201
        util.set_clock(lambda: dt.datetime.fromisoformat(f"2026-11-30T{at}:00+05:30"))
        M = type("M", (), {"J": type("J", (), {"ANNOUNCED_FILE": str(self.tmp / "none.json")})})
        orig = F.eq_symbols
        F.eq_symbols = lambda nse, d: EQ
        try:
            return EV.fo_entrants(self.env(classify), M, "2026-12", SD, FS, FO_TODAY)
        finally:
            F.eq_symbols = orig

    def test_retry_then_final_with_alert(self) -> None:
        orig = F.candidates
        F.candidates = lambda d, a, b, n: [cand("FAOP76899", fix("FAOP76899"), "2026-10-14")]
        try:
            with self.assertRaises(EV.EveningError):
                self.call("20:00", lambda cs: "garbage")
            fv = self.call("21:30", lambda cs: "garbage")
            self.assertEqual((fv["status"], fv["final"]), ("failed", True))
            self.assertTrue(any("FO_VET_FAILED" in t for t in self.sent))
            ok = self.call("22:00", lambda cs: 1 / 0)              # written once: reused, no new call, no alert
            self.assertEqual((ok["status"], ok["fresh"]), ("failed", False))
        finally:
            F.candidates = orig

    def test_a_bug_in_the_step_never_loses_the_plan(self) -> None:
        orig = F.run_check
        F.run_check = lambda *a, **k: None["boom"]                   # TypeError inside the F&O step
        try:
            with self.assertRaises(EV.EveningError):              # before give-up: retried like any failure
                self.call("20:00", None)
            fv = self.call("21:31", None)
            self.assertEqual((fv["status"], fv["final"], fv["entrants"]), ("failed", True, []))
            self.assertTrue(any("FO_VET_FAILED" in t and "TypeError" in t for t in self.sent))
        finally:
            F.run_check = orig


def types_env(cfg, paths, journal, sent):  # noqa: ANN001, ANN201
    def notify(text, buttons=None):  # noqa: ANN001
        sent.append(text)
        return True
    return E.Env(cfg, paths, journal, None, None, notify, None, lambda t: None)


class TestDay1Backstop(Harness):
    def test_entrant_is_not_bought_and_the_list_is_frozen(self) -> None:
        self.k.fo_futures = self.k.fo_futures + ["SYM2"]
        lg = self.run_day()
        self.assertEqual(lg.positions["2612-02"].leg, "SKIPPED")
        self.assertEqual([o.tradingsymbol for o in self.k.orders_], ["SYM1"])
        self.assertTrue(any("FO_ENTRANT" in t and "SYM2" in t for t, _ in self.sent))
        frozen = json.loads((self.paths.snapshots / "fo_day1_2026-12.json").read_text(encoding="utf-8"))
        self.assertIn("SYM2", frozen["names"])
        self.assertEqual(frozen["first_session"], "2026-12-01")

    def test_later_listing_does_not_block_a_deferred_buy(self) -> None:
        self.run_day()                                            # day 1 freezes a list without SYM1 / SYM2
        self.k.fo_futures = self.k.fo_futures + ["SYM1", "SYM2"]   # both gain futures later in the month
        self.assertEqual(E.fo_day1_names(self.env(), "2026-12")[0] & {"SYM1", "SYM2"}, set())

    def test_no_list_no_orders_no_lock_try(self) -> None:
        self.k.nfo_fail = True
        lg = self.run_day()
        self.assertEqual(self.k.calls.count("place"), 0)
        self.assertTrue(any("PREFLIGHT" in t and "F&O day-1 list" in t for t, _ in self.sent))
        self.assertTrue(all(p.tries_used == 0 and p.leg == "PENDING_BUY" for p in lg.positions.values()))

    def test_late_first_run_reads_the_day1_bhavcopy(self) -> None:
        self.new_day(dt.date(2026, 12, 2))
        self.k.sessions = ["2026-12-01", "2026-12-02"]
        env = self.env()
        env.fo_bhav_fn = lambda d: {f"FO{k:03d}" for k in range(200)} | ({"SYM1"} if d == "2026-12-01" else set())
        E.afternoon(env, {}, lambda: [])
        lg = L.replay(self.j.read())
        self.assertEqual(lg.positions["2612-01"].leg, "SKIPPED")
        src = json.loads((self.paths.snapshots / "fo_day1_2026-12.json").read_text(encoding="utf-8"))["source"]
        self.assertIn("bhavcopy 2026-12-01", src)

    def test_before_the_month_is_not_frozen(self) -> None:
        self.new_day(dt.date(2026, 11, 30))
        names, src, frozen = E.fo_day1_names(self.env(), "2026-12")
        self.assertFalse(frozen)
        self.assertFalse((self.paths.snapshots / "fo_day1_2026-12.json").exists())

    def test_stale_exclusion_reported(self) -> None:
        """The guard reads the decision's fo_vet block when it freezes the day-1 list."""
        d = json.loads((self.paths.decisions / "d.json").read_text(encoding="utf-8"))
        d["fo_vet"] = {"entrants": ["GHOSTCO"]}
        (self.paths.decisions / "d.json").write_text(json.dumps(d) + "\n", encoding="utf-8", newline="\n")
        self.assertIsNone(E.fo_day1_guard(self.env()))
        self.assertTrue(any("FO_FALSE_EXCLUSION" in t and "GHOSTCO" in t for t, _ in self.sent))


class TestConfig(unittest.TestCase):
    def test_fo_keys_validated(self) -> None:
        for bad in ({"fo_vet_give_up": "23:45"}, {"fo_vet_fail_action": "maybe"}, {"fo_scan_seed": 0}):
            with self.assertRaises(CF.ConfigError):
                CF.load(HOME, overrides=bad)


if __name__ == "__main__":
    unittest.main()

"""The live engine against the backtest, in the suite on short windows (the full 2020-01..2026-11 runs:
`python -I tests/replay_sim.py` and `python -I tests/replay_chaos.py 2020-01 2026-11 <seed>`)."""
import sys
import unittest
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / "tests"))

import replay_sim as R  # noqa: E402


@unittest.skipUnless((R.REH / "panel.npz").exists(), "rehearsal data not on this machine")
class TestReplay(unittest.TestCase):
    def test_live_engine_equals_the_backtest_2024_2026(self) -> None:
        out = R.run("2024-01", "2026-11", verbose=False)
        self.assertEqual(out["equal"], out["months"], [r for r in out["rows"] if not r["ok"]])
        self.assertGreater(out["stats"]["sell_fills"], 100)
        self.assertEqual(out["alerts"], [])

    def test_chaos_keeps_every_invariant(self) -> None:
        import replay_chaos as RC
        r = RC.main("2021-01", "2022-06", 11)
        self.assertEqual(r["violations"], [])
        self.assertGreater(r["state"]["crashes"], 5)                 # the faults really happened
        self.assertGreaterEqual(r["state"]["faults"]["lost_reply"], 3)


if __name__ == "__main__":
    unittest.main()

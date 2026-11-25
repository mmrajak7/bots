"""Slippage sensitivity (owner 2026-11-20) for the FULL_ASRS framework, SRS and ASRS, top-10 picks/day.

Same rows, picks, sizing (10L capital, 10k risk, 5L notional cap) and costs as full_asrs.report(); only the slippage
per side changes: 0.02% (base, as reported), 0.05%, 0.10%.  Rows store cost per share at 0.02% slip, so
cost_new = cost + (slip - 0.0002) * (entry + exit) per share.

    python -I slip_test.py
"""
from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.argv = sys.argv[:1]                      # full_asrs parses --srs at import; keep it neutral here
import full_asrs as F  # noqa: E402
import stocks_asrs_check as S  # noqa: E402
import y2026_asrs as Y  # noqa: E402

BASE_SLIP = S.SLIP                            # 0.0002, what the rows were built with
SLIPS = [0.0002, 0.0005, 0.0010]


def extra_cost_ps(entry: float, exit_px: float, slip: float) -> float:
    """Additional cost per share when slippage per side is `slip` instead of the base."""
    return (slip - BASE_SLIP) * (entry + exit_px)


def load_days(rows_dir: Path) -> dict[str, list[tuple[str, list]]]:
    by_day: dict[str, list[tuple[str, list]]] = defaultdict(list)
    for s in json.loads(Y.UNIVERSE.read_text()):
        f = rows_dir / f"{F.fname(s)}.json"
        if f.exists():
            for r in json.loads(f.read_text()):
                by_day[r[0]].append((s, r))
    return by_day


def run_setup(by_day, slip: float) -> dict:
    days = sorted(by_day)
    trades = []                                # (d, R_net, Rs)
    for d in days:
        picks = sorted([(s, r) for s, r in by_day[d] if r[1]], key=lambda x: -x[1][2])[:F.TOP_N]
        for s, r in picks:
            if r[3] == 0:
                continue
            q = F.size(r[4], r[5])
            if q <= 0:
                continue
            cps = r[9] + extra_cost_ps(r[4], r[6], slip)
            rs = F.trade_rs(r[3], r[4], r[6], cps, q)
            rnet = r[8] - extra_cost_ps(r[4], r[6], slip) / r[7]
            trades.append((d, rnet, rs))
    pnl = defaultdict(float)
    for d, _, rs in trades:
        pnl[d] += rs
    daily = [(d, pnl.get(d, 0.0)) for d in days]

    def part(sel):
        ts = [t for t in trades if sel(t[0])]
        dp = [(d, p) for d, p in daily if sel(d)]
        dd = F.drawdown(dp)
        return dict(n=len(ts), r=st.mean(t[1] for t in ts), rs=sum(t[2] for t in ts), dd=dd["pct_cap"],
                    ci=Y.boot_ci([(t[0], t[1]) for t in ts]))
    return dict(all=part(lambda d: True), is_=part(lambda d: d <= F.IS_END), oos=part(lambda d: d > F.IS_END),
                y2025=part(lambda d: d.startswith("2025")), y2026=part(lambda d: d.startswith("2026")))


def main() -> None:
    print("Slippage per side | setup | ALL avg R [CI] | ALL P&L Rs | max DD % of 10L | 2020-23 R | 2024-26 R | "
          "2025 Rs | 2026 Rs")
    for name, d in (("SRS", F.BASE / "full_rows_srs"), ("ASRS", F.BASE / "full_rows")):
        by_day = load_days(d)
        for slip in SLIPS:
            x = run_setup(by_day, slip)
            a = x["all"]
            print(f"{slip * 100:.2f}% | {name:4s} | {a['r']:+.3f} [{a['ci'][0]:+.3f},{a['ci'][1]:+.3f}] | "
                  f"{a['rs']:+13,.0f} | {a['dd']:5.1f}% | {x['is_']['r']:+.3f} | {x['oos']['r']:+.3f} | "
                  f"{x['y2025']['rs']:+11,.0f} | {x['y2026']['rs']:+11,.0f}", flush=True)


if __name__ == "__main__":
    main()

"""Compounding + Indian FY tax on the FULL_ASRS top-10 framework (COMPOUNDING.md, frozen 2026-11-20).

Descriptive what-if, no verdict.  Same rows, picks and costs as full_asrs.report(); only the money changes:
FIXED5K (10L, Rs5k risk, Rs5L cap), COMPOUND (0.5% of start-of-day equity, cap = equity x 5 / 10) and
COMPOUND_TAX (COMPOUND with speculative-income tax at each FY end, 4-FY loss carry-forward).

    python -I compounding.py          # report, SRS then ASRS -- ONLY after the independent review
"""
from __future__ import annotations

import math
import statistics as st
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]                      # full_asrs parses --srs at import; neutral (rows dirs are explicit here)
import full_asrs as F  # noqa: E402
import slip_test as T  # noqa: E402

START_CAPITAL = 1_000_000.0
FIXED_RISK = 5_000.0
FIXED_CAP = 500_000.0                        # 10L x 5x MIS / 10 slots
COMPOUND_RISK_PCT = 0.005
LEVERAGE, SLOTS = 5.0, 10
TAX_RATE = 0.312                             # 30% slab + 4% cess, no surcharge
TAX_RATE_SURCHARGE = 0.3588                  # 30% x 1.15 surcharge x 1.04 cess
CARRY_YEARS = 4                              # speculative loss: set off in the next 4 FYs only
SLIPS = (0.0002, 0.0005)                     # per side; rows carry 0.0002
NOTIONAL_FLAG = 2_500_000.0                  # median notional above this in an FY = liquidity warning
DAYS_PER_YEAR = 365.25
MODES = ("FIXED5K", "COMPOUND", "COMPOUND_TAX")
OUT_FILE = F.OUT / "compounding_report.txt"


# ---------------------------------------------------------------- pure pieces
def fy_of(date_str: str) -> int:
    """Indian FY label = its start year: 2020-04-01..2021-03-31 -> 2020.  Raises ValueError on a bad date."""
    d = date.fromisoformat(date_str)
    return d.year if d.month >= 4 else d.year - 1


def fy_label(fy: int) -> str:
    return f"FY{fy}-{(fy + 1) % 100:02d}"


def apply_tax(fy_pnls: list[tuple[int, float]], rate: float, carry_years: int = CARRY_YEARS) -> list[dict]:
    """Per-FY speculative tax with loss carry-forward (COMPOUNDING.md 'Tax model').

    fy_pnls: (fy, net P&L) with strictly increasing fy (gaps allowed: an absent FY still ages a loss).
    Returns one dict per FY: fy, pnl, setoff, taxable, tax, carry (balance after this FY), expired.
    """
    if not 0.0 <= rate < 1.0:
        raise ValueError(f"tax rate {rate} outside [0, 1)")
    if carry_years < 0:
        raise ValueError(f"carry_years {carry_years} < 0")
    fys = [fy for fy, _ in fy_pnls]
    if any(b <= a for a, b in zip(fys, fys[1:])):
        raise ValueError(f"FYs not strictly increasing: {fys}")
    losses: list[list] = []                  # [origin fy, remaining amount > 0], oldest first
    out = []
    for fy, pnl in fy_pnls:
        if not math.isfinite(pnl):
            raise ValueError(f"non-finite P&L for {fy_label(fy)}")
        # a loss whose window ended in an FY absent from the list expires before it can be used here
        expired = sum(a for o, a in losses if fy - o > carry_years)
        losses = [[o, a] for o, a in losses if fy - o <= carry_years]
        setoff = 0.0
        if pnl > 0:
            left = pnl
            for lot in losses:               # oldest first
                use = min(lot[1], left)
                lot[1] -= use
                left -= use
                setoff += use
                if left <= 0:
                    break
            losses = [lot for lot in losses if lot[1] > 0]
        elif pnl < 0:
            losses.append([fy, -pnl])
        taxable = max(0.0, pnl - setoff)
        # end of this FY: a loss whose last usable FY is this one expires now
        expired += sum(a for o, a in losses if fy - o >= carry_years)
        losses = [[o, a] for o, a in losses if fy - o < carry_years]
        out.append(dict(fy=fy, pnl=pnl, setoff=setoff, taxable=taxable, tax=rate * taxable,
                        carry=sum(a for _, a in losses), expired=expired))
    return out


def qty_for(entry: float, stop: float, risk: float, cap: float) -> int:
    """full_asrs.size with explicit risk and cap: stop distance is |entry - stop| (fill to stop), not r[7]."""
    risk_ps = abs(entry - stop)
    if risk_ps <= 0 or entry <= 0 or risk <= 0 or cap <= 0:
        return 0
    return int(min(math.floor(risk / risk_ps), math.floor(cap / entry)))


def day_selection(cands: list[tuple[str, list]]) -> list[tuple[str, list]]:
    """Same expression as full_asrs.report(): picks by score desc, top N (stable sort -> same tie order)."""
    return sorted([(s, r) for s, r in cands if r[1]], key=lambda x: -x[1][2])[:F.TOP_N]


def simulate(by_day: dict[str, list[tuple[str, list]]], mode: str, slip: float, tax_rate: float | None = None,
             risk: float | None = None, carry_years: int = CARRY_YEARS) -> dict:
    """Day-by-day equity under one money rule.

    mode: FIXED5K | COMPOUND | COMPOUND_TAX.  tax_rate is required for COMPOUND_TAX and refused otherwise.
    risk: FIXED5K only, overrides FIXED_RISK (the 10k cross-check vs full_asrs).
    Returns daily [dict(d, start, tax, pnl, end)], trades [(d, sym, qty, notional, rs)], fy_tax (apply_tax rows
    or None), paid {fy: (date, tax)}, accrued (tax on the last FY, not paid), ruined_on (date or None),
    skipped_q0 (selected, triggered picks not traded because qty came out 0).
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    if (mode == "COMPOUND_TAX") != (tax_rate is not None):
        raise ValueError("tax_rate is required for COMPOUND_TAX and only for it")
    if risk is not None and mode != "FIXED5K":
        raise ValueError("risk override is for FIXED5K only")
    if not 0 <= slip < 0.01:
        raise ValueError(f"slip {slip} per side outside [0, 1%)")
    fixed_risk = FIXED_RISK if risk is None else risk
    eq = START_CAPITAL
    daily, trades = [], []
    fy_list: list[tuple[int, float]] = []
    paid: dict[int, tuple[str, float]] = {}
    cur_fy, fy_pnl = None, 0.0
    ruined_on = None
    skipped_q0 = 0

    def tax_on_last() -> float:
        return apply_tax(fy_list, tax_rate, carry_years)[-1]["tax"] if mode == "COMPOUND_TAX" else 0.0

    for d in sorted(by_day):
        fy = fy_of(d)
        tax_today = 0.0
        if cur_fy is not None and fy != cur_fy:
            fy_list.append((cur_fy, fy_pnl))
            tax_today = tax_on_last()
            if tax_today:
                paid[cur_fy] = (d, tax_today)
            eq -= tax_today                  # first session of the new FY, before its sizing
            fy_pnl = 0.0
        cur_fy = fy
        start = eq
        day_pnl = 0.0
        if ruined_on is None and start <= 0:
            ruined_on = d
        if ruined_on is None:
            if mode == "FIXED5K":
                r_amt, cap = fixed_risk, FIXED_CAP
            else:
                r_amt, cap = start * COMPOUND_RISK_PCT, start * LEVERAGE / SLOTS
            for s, r in day_selection(by_day[d]):
                if r[3] == 0:
                    continue
                q = qty_for(r[4], r[5], r_amt, cap)
                if q <= 0:
                    skipped_q0 += 1          # counted so a shrinking equity (near-ruin) is visible, not silent
                    continue
                cps = r[9] + T.extra_cost_ps(r[4], r[6], slip)
                rs = F.trade_rs(r[3], r[4], r[6], cps, q)
                trades.append((d, s, q, q * r[4], rs))
                day_pnl += rs
        eq = start + day_pnl
        fy_pnl += day_pnl
        daily.append(dict(d=d, start=start, tax=tax_today, pnl=day_pnl, end=eq))
    fy_tax = None
    accrued = 0.0
    if cur_fy is not None:
        fy_list.append((cur_fy, fy_pnl))
        if mode == "COMPOUND_TAX":
            fy_tax = apply_tax(fy_list, tax_rate, carry_years)
            accrued = fy_tax[-1]["tax"]      # no session after the last FY -> accrued, not paid
    return dict(daily=daily, trades=trades, fy_tax=fy_tax, paid=paid, accrued=accrued, ruined_on=ruined_on,
                skipped_q0=skipped_q0)


def drawdown_vs_peak(series: list[tuple[str, float]], start: float = START_CAPITAL) -> dict:
    """Max DD of end-of-day equity vs the running peak (initial peak = start, dated 'START').

    Max % and max Rs are tracked separately (different episodes under compounding), each with its own dates.
    """
    if start <= 0:
        raise ValueError("start equity must be > 0")
    peak, peak_d = start, "START"
    out = dict(pct=0.0, pct_peak=None, pct_trough=None, rs=0.0, rs_peak=None, rs_trough=None)
    for d, e in series:
        if e >= peak:
            peak, peak_d = e, d
            continue
        dd = peak - e
        pct = dd / peak * 100
        if pct > out["pct"]:
            out.update(pct=pct, pct_peak=peak_d, pct_trough=d)
        if dd > out["rs"]:
            out.update(rs=dd, rs_peak=peak_d, rs_trough=d)
    return out


def longest_underwater(series: list[tuple[str, float]], start: float = START_CAPITAL) -> tuple[int, str | None,
                                                                                               str | None]:
    """Longest run of consecutive sessions with end-of-day equity below the running peak."""
    peak, n, first, best = start, 0, None, (0, None, None)
    for d, e in series:
        if e >= peak:
            peak, n, first = e, 0, None
            continue
        n += 1
        first = first or d
        if n > best[0]:
            best = (n, first, d)
    return best


def cagr(final: float, start: float, first_d: str, last_d: str) -> float | None:
    """(final/start)^(1/years) - 1, years = calendar span / 365.25.  None = ruin or no span."""
    years = (date.fromisoformat(last_d) - date.fromisoformat(first_d)).days / DAYS_PER_YEAR
    if years <= 0 or final <= 0 or start <= 0:
        return None
    return (final / start) ** (1 / years) - 1


def fy_table(sim: dict) -> list[dict]:
    """FY rows: start, pnl, tax (on this FY), carry, end = start + pnl - tax, ret = end/start - 1, paid flag,
    partial flag (first session not in April or last session not in March)."""
    pnl = defaultdict(float)
    for x in sim["daily"]:
        pnl[fy_of(x["d"])] += x["pnl"]
    tax = {t["fy"]: t for t in (sim["fy_tax"] or [])}
    first_m = {}
    last_m = {}
    for x in sim["daily"]:
        fy = fy_of(x["d"])
        first_m.setdefault(fy, int(x["d"][5:7]))
        last_m[fy] = int(x["d"][5:7])
    rows, start = [], START_CAPITAL
    for fy in sorted(pnl):
        t = tax.get(fy)
        tx = t["tax"] if t else 0.0
        end = start + pnl[fy] - tx
        rows.append(dict(fy=fy, start=start, pnl=pnl[fy], tax=tx, carry=t["carry"] if t else 0.0, end=end,
                         ret=(end / start - 1) if start > 0 else None, paid=fy in sim["paid"],
                         partial=first_m[fy] != 4 or last_m[fy] != 3))   # stub FY: return not annualised
        start = end
    return rows


def notional_by_fy(trades: list[tuple]) -> dict[int, tuple[float, float, int]]:
    """fy -> (median notional, max notional, trades)."""
    by: dict[int, list[float]] = defaultdict(list)
    for d, _, _, notional, _ in trades:
        by[fy_of(d)].append(notional)
    return {fy: (st.median(v), max(v), len(v)) for fy, v in sorted(by.items())}


# ---------------------------------------------------------------- report
def _pct(x: float | None) -> str:
    return "ruin" if x is None else f"{x * 100:+.1f}%"


def variant_lines(label: str, sim: dict) -> list[str]:
    daily = sim["daily"]
    if not daily:
        return [f"--- {label}: no sessions"]
    series = [(x["d"], x["end"]) for x in daily]
    first, last = daily[0]["d"], daily[-1]["d"]
    final = daily[-1]["end"]
    acc = sim["accrued"]
    dd = drawdown_vs_peak(series)
    uw = longest_underwater(series)
    L = [f"--- {label} ---"]
    L.append("FY        | start equity   | P&L            | tax            | carry loss     | end equity     | return")
    for r in fy_table(sim):
        tag = "" if r["paid"] or not sim["fy_tax"] else (" (accrued)" if r["tax"] else "")
        tag += " (partial FY, not annualised)" if r["partial"] else ""
        L.append(f"{fy_label(r['fy'])} | {r['start']:14,.0f} | {r['pnl']:+14,.0f} | {r['tax']:14,.0f} | "
                 f"{r['carry']:14,.0f} | {r['end']:14,.0f} | {_pct(r['ret'])}{tag}")
    if sim["fy_tax"] is not None:
        L.append(f"final equity before accrued tax Rs {final:,.0f} (CAGR {_pct(cagr(final, START_CAPITAL, first, last))}) "
                 f"| accrued tax liability {fy_label(fy_of(last))} Rs {acc:,.0f} | after it Rs {final - acc:,.0f} "
                 f"(CAGR {_pct(cagr(final - acc, START_CAPITAL, first, last))})")
        L.append(f"tax paid total Rs {sum(t for _, t in sim['paid'].values()):,.0f}")
    else:
        L.append(f"final equity Rs {final:,.0f} (CAGR {_pct(cagr(final, START_CAPITAL, first, last))})")
    L.append(f"max DD {dd['pct']:.1f}% of peak ({dd['pct_peak']}->{dd['pct_trough']}) | max DD Rs {dd['rs']:,.0f} "
             f"({dd['rs_peak']}->{dd['rs_trough']}) | longest underwater {uw[0]} sessions ({uw[1]}..{uw[2]})")
    if sim["ruined_on"]:
        L.append(f"RUIN: equity <= 0 at the start of {sim['ruined_on']}; no trades after")
    L.append(f"selected triggered picks skipped for qty 0: {sim.get('skipped_q0', 0)}")
    L.append("notional per trade by FY: FY | trades | median Rs | max Rs")
    for fy, (med, mx, n) in notional_by_fy(sim["trades"]).items():
        flag = "  <-- LIQUIDITY FLAG: median > Rs25L" if med > NOTIONAL_FLAG else ""
        L.append(f"  {fy_label(fy)} | {n:6d} | {med:12,.0f} | {mx:12,.0f}{flag}")
    return L


def setup_lines(name: str, by_day: dict[str, list[tuple[str, list]]]) -> list[str]:
    days = sorted(by_day)
    L = [f"\n===== {name}: {days[0] if days else '-'}..{days[-1] if days else '-'} ({len(days)} sessions) ====="]
    for slip in SLIPS:
        runs = [("FIXED5K", None), ("COMPOUND", None), ("COMPOUND_TAX", TAX_RATE), ("COMPOUND_TAX", TAX_RATE_SURCHARGE)]
        for mode, rate in runs:
            label = (f"{name} | slip {slip * 100:.2f}%/side | {mode}"
                     + (f" @ {rate * 100:.2f}%" if rate is not None else ""))
            L += variant_lines(label, simulate(by_day, mode, slip, rate))
    return L


HEADER = [
    "COMPOUNDING what-if (COMPOUNDING.md, frozen 2026-11-20) -- DESCRIPTIVE, NO VERDICT.",
    "Top-10 'stocks in play' picks, SURVIVORSHIP-BIASED universe (today's F&O list), NEO cash intraday costs.",
    f"Start Rs {START_CAPITAL:,.0f}. FIXED5K: risk Rs {FIXED_RISK:,.0f}, cap Rs {FIXED_CAP:,.0f}. COMPOUND: risk "
    f"{COMPOUND_RISK_PCT:.1%} of start-of-day equity, cap = equity x {LEVERAGE:g} / {SLOTS}.",
    "Tax: speculative income, FY Apr-Mar, losses carried 4 FYs vs speculative gains only, oldest first. "
    "SIMPLIFICATION: whole FY tax deducted on the first session after 31 Mar (real life: quarterly advance tax).",
    "Return % = post-tax, equity-based. DD vs running peak of end-of-day equity (tax payments count).",
    "Tax NOT modelled: surcharge above 35.88% (25% surcharge over Rs2Cr income -> 39.0%; old regime "
    ">Rs5Cr -> 42.7%), lower slabs, "
    "advance-tax interest, audit; carry-forward assumes every return filed on time.",
]


def report() -> None:
    L = list(HEADER)
    for name, rows_dir in (("SRS", F.BASE / "full_rows_srs"), ("ASRS", F.BASE / "full_rows")):
        if not rows_dir.exists():
            raise SystemExit(f"rows dir missing: {rows_dir} (rebuild with full_asrs.py rows)")
        L += setup_lines(name, T.load_days(rows_dir))
    F.OUT.mkdir(exist_ok=True)
    OUT_FILE.write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


def main(argv: list[str]) -> None:
    if argv:
        raise SystemExit("usage: compounding.py   (no arguments)")
    report()


if __name__ == "__main__":
    main(_argv)

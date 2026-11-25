"""F&O joiner trait: the free tests F1-F5 (JOINER_TRAIT_DESIGN.md §3.0, frozen with the OWNER ANSWERS 2026-11-21).

    python -I joiner_free.py counts    # COUNTS / BOOLEANS ONLY (no R, no Rs) -- allowed before the review
    python -I joiner_free.py report    # F1-F5 + the F5 STOP rule (prints R) -- ONLY after the independent review

Rows already on disk only (full_rows_srs + surv_rows_srs, SRS sig_n=3); daily bars already on disk for turnover.
No download, no fetch.  Loaders, groups and statistics are imported from de_kill / survivorship / liq_side.

Frozen choices implemented here (design §3.0; module constants below):
- JOINER = a resolved symbol (survivorship.eligible_sets, renames followed) whose first member month J is after
  2020-01.  LEFT = its final member month is before the last membership month; STAYED = the rest (left-and-rejoined
  names are STAYED).  PRE-JOIN = de_kill.biased_group == "PRE-JOIN" (month(d) < J).
- K1 SELECTION (F1, F2, F5): exactly de_kill.k1_lines' BIASED top-10 -- today's 218, top 10 by score among ALL
  pick-flagged rows of the day (liq_side.top_by_score == survivorship.select_trades), triggered and sizeable; the
  PRE-JOIN filter is applied AFTER the top-10 cut.  Stored 0.02% costs unless re-costed.
- ALL TRIGGERED PICKS (F3, F4): every pick-flagged row with side != 0 and full_asrs.size > 0 (the condition
  select_trades uses to make a trade), no top-10 cut, no eligibility filter (PRE-JOIN / post-join is the group).
  Rows from survivorship.pit_files, so the 47 leavers (not in the 218) are included.
- F1: months before the join = J - month(d) (1 = the month just before J); <= 12 vs > 12.
- F2: longs vs shorts.  FLAG look-ahead iff OOS shorts mean R <= 0 while OOS longs mean R > 0.
- F3: PRE-JOIN triggered picks of LEFT joiners vs STAYED joiners.
- F4: each joiner's triggered picks by offset o = month(d) - J: BEFORE o in -13..-2, AFTER o in 0..+11 (o = -1
  dropped: the true join is inside it).  Calendar months taken at face value (a leaver's non-member months inside
  AFTER stay in; counted by `counts`).
- F5 (THE STOP): K1 PRE-JOIN trades re-costed per trade at TIERED slippage = DOUBLE_ENGINE §3 tiers on
  T = SMA20 daily turnover (close x volume) to d-1 (liq_side.turnover_sma): >= 1000 Cr 0.02%; 200-1000 Cr 0.05%;
  50-200 Cr 0.10%; < 50 Cr 0.15%; T unknown (< 20 bars) -> 0.15% (the worst tier; counted).  Re-costing is
  slip_test arithmetic via liq_side.recost.  Also printed at flat 0.05% and 0.10%.
  STOP iff OOS mean R at TIERED <= 0, or OOS has no trade. Owner 2026-11-21: REPORTED ONLY, it no longer gates the download.
- Every group: ALL / IS 2020-2023 / OOS 2024-2026, mean R [day-block 95% CI, 5,000 draws, seed 7]
  (de_kill.r_line).  ADDITION to the frozen design (flagged for the review): each test also prints group A - B
  with a de_kill.boot_diff paired day-block CI, tagged [printed, never read].  Only F5 has a decision; F1-F4 set
  the prior and are never read as confirmation.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]                         # survivorship / liq_side truncate sys.argv at import: capture ours first
import de_kill as K  # noqa: E402
import liq_side as L  # noqa: E402

V, F, T = K.V, K.F, L.T

START_MONTH = "2020-01"                      # first membership month: first member month > this -> JOINER
NEAR_MONTHS = 12                             # F1: <= 12 months before the join vs > 12
F4_BEFORE = (-13, -2)                        # F4: offsets month(d) - J, inclusive
F4_AFTER = (0, 11)
TIER_LOW = 50 * L.CR                         # the one tier DOUBLE_ENGINE §3 adds to the LIQ_SIDE tiers
SLIP_VLOW = 0.0015                           # < 50 Cr, and turnover unknown
FLAT_SLIPS = (0.0005, 0.0010)                # printed beside TIERED, never read
OOS_NAME = K.WINDOWS[2][0]
OUT_FILE = V.OUT / "joiner_free.txt"
Pair = Tuple[str, float]


# ---------------------------------------------------------------- small pure helpers
def mdiff(a: str, b: str) -> int:
    """Months from b to a ('YYYY-MM' or longer)."""
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7])


def tier_slip(t: Optional[float]) -> float:
    """TIERED (DOUBLE_ENGINE §3, an ASSUMPTION): liq_side's three tiers plus < 50 Cr (or unknown) at 0.15%."""
    if t is None or t < TIER_LOW:
        return SLIP_VLOW
    return L.slip_for_turnover(t)


def triggered(r: list) -> bool:
    """Pick-flagged, triggered and sizeable: the condition under which select_trades makes a trade."""
    return bool(r[1]) and r[3] != 0 and F.size(r[4], r[5]) > 0


# ---------------------------------------------------------------- joiners
def joiner_info(sets: Dict[str, set]) -> dict:
    months_of = K.member_months(sets)
    first = {s: ms[0] for s, ms in months_of.items()}
    last_month = max(sets)
    joiners = sorted(s for s, m in first.items() if m > START_MONTH)
    left = sorted(s for s in joiners if months_of[s][-1] < last_month)
    return dict(months_of=months_of, first=first, last_month=last_month, joiners=joiners, left=left,
                stayed=sorted(set(joiners) - set(left)))


# ---------------------------------------------------------------- K1 selection (F1, F2, F5)
def k1_prejoin_trades(by_day: Dict[str, List[Tuple[str, list]]], days: List[str], months_of: Dict[str, List[str]],
                      slip_of: Callable[[str, str], float]) -> List[tuple]:
    """The K1 BIASED top-10 (all pick-flagged rows of the day, top 10 by score), THEN only its PRE-JOIN picks, each
    costed at slip_of(symbol, day).  -> liq_side.run_days trades [(d, s, side, R, Rs)]."""
    def picks_of(d: str) -> List[Tuple[str, list, float]]:
        top = L.top_by_score([(s, r) for s, r in by_day.get(d, []) if r[1]])
        return [(s, r, slip_of(s, d)) for s, r in top if K.biased_group(s, d, months_of) == "PRE-JOIN"]
    return L.run_days(days, picks_of)[0]


def turnover_lookup(daily: Dict[str, List[dict]]) -> Callable[[str, str], Optional[float]]:
    dates = {s: [b["d"] for b in bars] for s, bars in daily.items()}

    def turn(s: str, d: str) -> Optional[float]:
        if s not in daily:
            raise KeyError(f"no daily bars for {s}: turnover cannot be guessed")
        return L.turnover_sma(daily[s], d, dates=dates[s])
    return turn


def tiered_slip_of(turn: Callable[[str, str], Optional[float]]) -> Callable[[str, str], float]:
    return lambda s, d: tier_slip(turn(s, d))


# ---------------------------------------------------------------- triggered picks (F3, F4)
def triggered_picks(by_day: Dict[str, List[Tuple[str, list]]], days: List[str], keep: Callable[[str, str], bool]
                    ) -> List[dict]:
    out = []
    for d in days:
        for s, r in by_day.get(d, []):
            if triggered(r) and keep(s, d):
                out.append(dict(d=d, s=s, side=r[3], r=r[8]))
    return out


def f4_offset_group(d: str, j: str) -> Optional[str]:
    o = mdiff(d, j)
    if F4_BEFORE[0] <= o <= F4_BEFORE[1]:
        return "BEFORE"
    if F4_AFTER[0] <= o <= F4_AFTER[1]:
        return "AFTER"
    return None


# ---------------------------------------------------------------- groups
def f1_groups(trades: Sequence[tuple], first: Dict[str, str]) -> Dict[str, List[Pair]]:
    g: Dict[str, List[Pair]] = {f"<= {NEAR_MONTHS} m": [], f"> {NEAR_MONTHS} m": []}
    for t in trades:
        dist = mdiff(first[t[1]], t[0])
        if dist < 1:
            raise ValueError(f"PRE-JOIN trade {t[1]} {t[0]} is not before its join month {first[t[1]]}")
        g[f"<= {NEAR_MONTHS} m" if dist <= NEAR_MONTHS else f"> {NEAR_MONTHS} m"].append((t[0], t[3]))
    return g


def f2_groups(trades: Sequence[tuple]) -> Dict[str, List[Pair]]:
    g: Dict[str, List[Pair]] = {sn: [] for _, sn in V.SIDES}
    names = dict(V.SIDES)
    for t in trades:
        if t[2] not in names:
            raise ValueError(f"trade side {t[2]} outside +-1")
        g[names[t[2]]].append((t[0], t[3]))
    return g


def f3_picks(pit_by, days, info) -> List[dict]:
    js = set(info["joiners"])
    return triggered_picks(pit_by, days, lambda s, d: s in js and
                           K.biased_group(s, d, info["months_of"]) == "PRE-JOIN")


def f3_groups(picks: Sequence[dict], left: set) -> Dict[str, List[Pair]]:
    g: Dict[str, List[Pair]] = {"LEFT": [], "STAYED": []}
    for p in picks:
        g["LEFT" if p["s"] in left else "STAYED"].append((p["d"], p["r"]))
    return g


def f4_picks(pit_by, days, info) -> List[dict]:
    js, first = set(info["joiners"]), info["first"]
    out = []
    for p in triggered_picks(pit_by, days, lambda s, d: s in js):
        g = f4_offset_group(p["d"], first[p["s"]])
        if g is not None:
            p["g"] = g
            out.append(p)
    return out


def f4_groups(picks: Sequence[dict]) -> Dict[str, List[Pair]]:
    g: Dict[str, List[Pair]] = {"BEFORE": [], "AFTER": []}
    for p in picks:
        g[p["g"]].append((p["d"], p["r"]))
    return g


# ---------------------------------------------------------------- decisions (pure)
def mean_of(pairs: Sequence[Pair]) -> Optional[float]:
    return K.mean_or_none([v for _, v in pairs])


def f2_flag(long_oos: Optional[float], short_oos: Optional[float]) -> Tuple[Optional[bool], str]:
    """(True / False / None = undefined, text).  FLAG iff OOS shorts <= 0 while OOS longs > 0."""
    if long_oos is None or short_oos is None:
        return None, "F2 flag UNDEFINED: an OOS side has no trade"
    if short_oos <= 0 < long_oos:
        return True, "FLAG look-ahead: OOS shorts <= 0 while OOS longs > 0"
    return False, "no look-ahead flag (OOS shorts > 0, or OOS longs <= 0)"


def f5_verdict(oos_tiered: Sequence[Pair]) -> Tuple[str, str]:
    """Frozen STOP rule.  STOP iff OOS mean R at TIERED <= 0; an empty OOS cannot pass (STOP, said so)."""
    m = mean_of(oos_tiered)
    if m is None:
        return "STOP", "no OOS PRE-JOIN trade at TIERED: the stop test cannot pass"
    if m <= 0:
        return "STOP", "OOS mean R at TIERED <= 0 (intraday SRS on joiners does not survive tiered slippage)"
    return "GO", "OOS mean R at TIERED > 0 (intraday SRS on joiners survives tiered slippage)"


def f2_flag_line(g2: Dict[str, List[Pair]]) -> str:
    """The F2 flag on the OOS window of f2_groups output."""
    return f2_flag(mean_of(windowed(g2["LONG"])[OOS_NAME]), mean_of(windowed(g2["SHORT"])[OOS_NAME]))[1]


def f5_section(by_day: Dict[str, List[Tuple[str, list]]], days: List[str], months_of: Dict[str, List[str]],
               turn: Callable[[str, str], Optional[float]]) -> Tuple[List[str], Tuple[str, str]]:
    """F5 lines + the frozen verdict.  The verdict reads ONLY the TIERED variant's OOS window; the stored and flat
    variants are printed beside it, never read."""
    L_ = ["F5 slippage (K1 top-10 PRE-JOIN re-costed per trade; THE STOP TEST)"]
    variants: List[Tuple[str, Callable[[str, str], float]]] = [("stored 0.02%", lambda s, d: T.BASE_SLIP),
                                                               ("TIERED", tiered_slip_of(turn))]
    variants += [(f"flat {x * 100:.2f}%", (lambda x: lambda s, d: x)(x)) for x in FLAT_SLIPS]
    oos_tiered: List[Pair] = []
    for name, slip_of in variants:
        w = windowed([(t[0], t[3]) for t in k1_prejoin_trades(by_day, days, months_of, slip_of)])
        if name == "TIERED":
            oos_tiered = w[OOS_NAME]
        for wn, _ in K.WINDOWS:
            tag = "READ (OOS)" if (name == "TIERED" and wn == OOS_NAME) else "printed, never read"
            L_.append("  " + K.r_line(f"{name:12s} {wn:13s}", w[wn]) + f" [{tag}]")
    v = f5_verdict(oos_tiered)
    L_.append(f"F5 VERDICT (frozen rule; owner 2026-11-21: reported only, no longer gates the download): {v[0]} -- {v[1]}")
    return L_, v


# ---------------------------------------------------------------- printing
def windowed(pairs: Sequence[Pair]) -> Dict[str, List[Pair]]:
    return {wn: [p for p in pairs if sel(p[0])] for wn, sel in K.WINDOWS}


def diff_line(label: str, a: Sequence[Pair], b: Sequence[Pair]) -> str:
    if not a or not b:
        return f"{label}: difference undefined (empty group) [printed, never read]"
    lo, hi = K.boot_diff(list(a), list(b))
    return f"{label}: {mean_of(a) - mean_of(b):+.3f}R CI[{lo:+.3f},{hi:+.3f}] [printed, never read]"


def group_lines(title: str, groups: Dict[str, List[Pair]], a: str, b: str) -> List[str]:
    L_ = [title]
    for wn, _ in K.WINDOWS:
        for g, pairs in groups.items():
            L_.append("  " + K.r_line(f"{g:9s} {wn:13s}", windowed(pairs)[wn]))
        L_.append("  " + diff_line(f"{a} - {b} {wn}", windowed(groups[a])[wn], windowed(groups[b])[wn]))
    return L_


def count_lines(title: str, groups: Dict[str, List[Pair]], stocks: Optional[Dict[str, set]] = None) -> List[str]:
    L_ = [title]
    for g, pairs in groups.items():
        w = windowed(pairs)
        L_.append(f"  {g:9s} " + "  ".join(f"{wn} {len(w[wn])}" for wn, _ in K.WINDOWS) +
                  (f"  stocks {len(stocks[g])}" if stocks else ""))
    return L_


# ---------------------------------------------------------------- loading
def load_context() -> dict:
    mem = V.load_membership()
    rmap = json.loads(V.MAP_FILE.read_text())
    sets = V.eligible_sets(mem, rmap)
    info = joiner_info(sets)
    pit_by = V.load_rows(V.pit_files(K.SRS_ROWS218, K.SRS_ROWSNEW, rmap, mem))
    bias_by = V.load_rows(V.biased_files(K.SRS_ROWS218))
    days = [d for d in sorted(pit_by) if K.FIRST_DAY <= d <= K.LAST_DAY]
    base = k1_prejoin_trades(bias_by, days, info["months_of"], lambda s, d: T.BASE_SLIP)
    daily, missing = K.load_daily(sorted({t[1] for t in base}))
    if missing:
        raise SystemExit(f"PRE-JOIN trade symbols without a daily raw file: {missing}")
    return dict(mem=mem, rmap=rmap, sets=sets, info=info, pit_by=pit_by, bias_by=bias_by, days=days, base=base,
                turn=turnover_lookup(daily))


# ---------------------------------------------------------------- counts (no R)
def counts_lines(ctx: dict) -> List[str]:
    info, days, base = ctx["info"], ctx["days"], ctx["base"]
    L_ = [f"joiners {len(info['joiners'])}: LEFT {len(info['left'])}, STAYED {len(info['stayed'])}; "
          f"sessions {len(days)} {days[0]}..{days[-1]}"]
    ref, _, _ = V.select_trades(ctx["bias_by"], days)
    ref_pj = [t for t in ref if K.biased_group(t[1], t[0], info["months_of"]) == "PRE-JOIN"]
    same = len(ref_pj) == len(base) and all(a[:3] == b[:3] and a[3] == b[3] for a, b in zip(ref_pj, base))
    L_.append(f"P-K1: base-cost PRE-JOIN trades == survivorship.select_trades PRE-JOIN (bit-exact R): {same}")
    L_ += count_lines("F1 K1 PRE-JOIN trades by distance to the join", f1_groups(base, info["first"]))
    L_ += count_lines("F2 K1 PRE-JOIN trades by side", f2_groups(base))
    p3 = f3_picks(ctx["pit_by"], days, info)
    st3 = {g: {p["s"] for p in p3 if (p["s"] in info["left"]) == (g == "LEFT")} for g in ("LEFT", "STAYED")}
    L_ += count_lines("F3 PRE-JOIN triggered picks, LEFT vs STAYED (stocks with >= 1 pick)",
                      f3_groups(p3, set(info["left"])), st3)
    p4 = f4_picks(ctx["pit_by"], days, info)
    st4 = {g: {p["s"] for p in p4 if p["g"] == g} for g in ("BEFORE", "AFTER")}
    L_ += count_lines("F4 joiners' triggered picks, BEFORE (J-13..J-2) vs AFTER (J..J+11)", f4_groups(p4), st4)
    mo = info["months_of"]
    nonmem = sum(1 for p in p4 if p["g"] == "AFTER" and p["d"][:7] not in mo[p["s"]])
    L_.append(f"  AFTER picks in a non-member month (left within 12 months): {nonmem}")
    first, lm = info["first"], info["last_month"]
    cb = sum(1 for s in info["joiners"] if mdiff(first[s], START_MONTH) + F4_BEFORE[0] < 0)
    ca = sum(1 for s in info["joiners"] if mdiff(first[s], lm) + F4_AFTER[1] > 0)
    L_.append(f"  joiners censored: BEFORE starts before {START_MONTH} {cb}; AFTER ends after {lm} {ca}")
    tiers: Counter = Counter()
    unknown = 0
    for t in base:
        x = ctx["turn"](t[1], t[0])
        unknown += x is None
        tiers[(K.WINDOWS[1][0] if t[0] <= K.IS_END else OOS_NAME, tier_slip(x))] += 1
    L_.append(f"F5 TIERED mix of K1 PRE-JOIN trades (slip per side: count): "
              f"{dict(sorted((f'{w} {s * 100:.2f}%', n) for (w, s), n in tiers.items()))}; turnover unknown {unknown}")
    return L_


def counts() -> None:
    print("\n".join(counts_lines(load_context())))


# ---------------------------------------------------------------- report (prints R: after the review only)
def report_lines(ctx: dict) -> List[str]:
    info, days, base = ctx["info"], ctx["days"], ctx["base"]
    L_ = ["Joiner free tests F1-F5 (JOINER_TRAIT_DESIGN.md §3.0). SRS. They set the prior; only F5 decides (STOP)."]
    L_ += counts_lines(ctx) + [""]
    L_ += group_lines("F1 distance (K1 top-10 PRE-JOIN, stored 0.02%)", f1_groups(base, info["first"]),
                      f"<= {NEAR_MONTHS} m", f"> {NEAR_MONTHS} m")
    g2 = f2_groups(base)
    L_ += group_lines("F2 side (K1 top-10 PRE-JOIN, stored 0.02%)", g2, "LONG", "SHORT")
    L_.append("  " + f2_flag_line(g2))
    L_ += group_lines("F3 failed joiners (ALL triggered PRE-JOIN picks, no slot cut, stored 0.02%)",
                      f3_groups(f3_picks(ctx["pit_by"], days, info), set(info["left"])), "LEFT", "STAYED")
    L_ += group_lines("F4 same stock before vs after the join (ALL triggered picks, stored 0.02%)",
                      f4_groups(f4_picks(ctx["pit_by"], days, info)), "AFTER", "BEFORE")
    L_ += f5_section(ctx["bias_by"], days, info["months_of"], ctx["turn"])[0]
    return L_


def report() -> None:
    L_ = report_lines(load_context())
    V.OUT.mkdir(exist_ok=True)
    OUT_FILE.write_text("\n".join(L_), encoding="utf-8")
    print("\n".join(L_))


COMMANDS: Dict[str, Callable[[], None]] = {"counts": counts, "report": report}


def main(argv: List[str]) -> None:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        raise SystemExit("usage: joiner_free.py counts|report")
    COMMANDS[argv[0]]()


if __name__ == "__main__":
    main(_argv)

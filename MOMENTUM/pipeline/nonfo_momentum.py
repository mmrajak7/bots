"""Momentum in liquid NON-F&O stocks (NONFO_MOMENTUM_DESIGN.md, FROZEN 2026-11-24 with the OWNER ANSWERS).

Commands (64-bit Python, `-I`):

    python -I nonfo_momentum.py signals             # M2/M3: P1-P11 + P12 list.  COUNTS / NAMES / BOOLEANS ONLY
    python -I nonfo_momentum.py ka0                 # KA-0 bridge on real data: the joiner MOM series, published 2.6/3.2/1.8
    python -I nonfo_momentum.py report --reviewed   # §5-§8 frozen report (RETURNS: only after the independent review)
    python -I nonfo_momentum.py forward YYYY-MM     # §9 forward paper file (refuses to overwrite; from 2026-12)

`counts` and `indices` of design §11 were done in M0/M1 (the session-scratchpad `counts.py`, `fetch_indices.py`);
their checks are re-run inside `signals` (P1-P3, P6, P7).  `checks` of §11 is `signals` (P1-P11 in one pass).

Three layers (so the count path cannot produce a return by construction):
1. RANKS   (`Scorer`)  -- §4 scores per arm and month from data to the signal day; gate-independent.
2. HOLDINGS (`Book` with the FLAT price index `flat_px`) -- which chain is bought / sold on which session (buffer,
   gate, lock deferrals, stops).  Every decision is price-free: buys need free cash > 0, and cash is > 0 iff a sale
   or a merger delivered money, which is the same under any positive price index (tested:
   test_schedule_is_price_free).
3. MONEY   (`Book` with the adjusted price index `G`) -- lots, daily equity, costs, FIFO tax.

Design traceability (section -> code):
- §1 universe: `load_bands` (B150 files of NONFO_SRS_DESIGN §1, via nonfo_srs.B150_DIR), `Market.eligible` (filter E:
  a price at the end of m-13), FO parallel universe `Market.fo_pool`; eligibility is for ENTRY only (`Book._rebalance`).
- §2 timing: signal day = last session before m (`Market.sig`), trade at the close of the first session of m;
  locked entry (close = high, day return >= +4.9%) retried up to 5 sessions, locked exit (close = low, <= -4.9%)
  deferred to the next unlocked session (joiner_positional._locked_up / _locked_down); 20 sessions without a row ->
  joiner_exit_events.json (+ the study's own EXTRA_EVENTS_FILE) -> swap / last close / -100% (`Market.next_stop`).
- §3 portfolio: N = 20, rank buffer 20/40 (`rebalance`), freed cash split equally, no re-weighting, no cap.
- §4 arms: `Scorer` (BLEND, M12, M6, M3, HI52, SMOOTH); `zcut`; reality check `reality_check`.
- §5 regime: `gate_trend`, `gate_breadth`, `gate_rebound` (verbatim from the M0 counts script), FLAT / NO-NEW
  (`Spec.gate_mode`), re-equalisation control (`Spec.reequal`), placebo `placebo_schedules`, `gate_rule` (g1-g5).
- §6 benchmarks: EW-E / EWB (`Spec.mode == "equal"`), C-RAND (`Spec.mode == "crand"`), `fund_series` (IDX-MATCH,
  IDX-OWNED: TRI less fund cost before the fund's launch, the real NAV after), FO.
- §7 costs / tax / scaling: flat 0.40% (+0.15% / 1.0% levels), `impact`, `tax_fy` + FIFO lots in `Book`,
  `capacity`, scaling table in `report`.
- §8 statistics: monthly calendar returns of the whole account, IS / OOS split, `paired_ci` (moving block,
  block 6, 5,000 draws, seed 7, monthly_orb.mbb_ci), Bonferroni 98.33% on S1, S2, g4; `read_rule`, `flags`.
- §9 forward paper: `forward_file`.
- §10 checks P1-P12: `signals`.
- §11 KA-0: `sleeve_series` (the new position stepping, set to 12-1 / top 30 / 6 sleeves / 126 sessions /
  joiner band) against joiner_positional's own MOM engine, month by month.

Every ambiguity resolved here is listed in the design file, section IMPLEMENTATION (deviation log).
Nothing in this module calls a broker or the network.
"""
from __future__ import annotations

import bisect
import ctypes
import hashlib
import json
import logging
import math
import os
import random
import statistics as st
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

import numpy as np
from numpy.typing import NDArray

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]                          # joiner_positional / nonfo_srs truncate sys.argv at import: ours first
sys.argv = sys.argv[:1]
import compounding as CMP  # noqa: E402
import full_asrs as FA  # noqa: E402
import joiner_positional as J  # noqa: E402
import monthly_orb as MO  # noqa: E402
import nonfo_srs as NS  # noqa: E402
import survivorship as SV  # noqa: E402

# F6: every study module imported (directly or through joiner_positional / nonfo_srs) -- hashed into the stamp
_STUDY_MODULES = tuple(sorted({Path(str(m.__file__)).resolve() for m in list(sys.modules.values())
                               if getattr(m, "__file__", None) and Path(str(m.__file__)).resolve().parent == HERE
                               and not Path(str(m.__file__)).name.startswith("test_")}))

LOG = logging.getLogger("nonfo_momentum")

# ---------------------------------------------------------------- paths (data outside the public repo)
NSE_DATA = NS.NSE
B150_DIR = NS.B150_DIR                                   # §1: b150_YYYY-MM.json, built by nonfo_srs.py band
INDEX_DIR = J.INDEX_DIR                                  # §6 / M1: {date: value} JSON per index / fund
VIX_FILE = HERE.parent / "FIFTY" / "data" / "backtest_px_universe" / "INDIA VIX.json"   # §5 regime map
SECTOR_FILE = HERE / "data" / "sector_map.json"          # P11
INDUSTRY_CSV = HERE / "data" / "nse_nifty500_industry.csv"   # P11 fallback
EXTRA_EVENTS_FILE = HERE / "data" / "nonfo_mom_exit_events.json"   # P9: this study's own stopped-name file
P10_EXPLAINED_FILE = HERE / "data" / "nonfo_mom_p10_explained.json"   # P10: {"SYM YYYY-MM": reason}
STUDY_DIR = NSE_DATA / "nonfo_mom"
FORWARD_DIR = STUDY_DIR / "forward"                      # §9 (outside the repo)
CHECKS_FILE = STUDY_DIR / "signals_checks.json"          # P1-P10 booleans, read by `report` for S6
KA0_FILE = STUDY_DIR / "ka0.json"                        # KA-0 result, read by `report`

# ---------------------------------------------------------------- frozen constants (design section in brackets)
ACCOUNT = 1_000_000.0                                    # §2 / §7: 10L
N_HOLD = 20                                              # §3
SELL_RANK = 40                                           # §3 rank buffer 20 / 40
N_PRINTED = (10, 30)                                     # §3 printed, never read (sell rank 2N: see deviation log)
SELL_RANKS_PRINTED = (20, 75)                            # §3 no buffer (20/20) and wide buffer (20/75)
HIST_MONTHS = 13                                         # §1 filter E: a price at the end of m-13
SKIP_MONTHS = 2                                          # §4 scores end at the end of m-2 (skip the last month)
LOOKBACK = {"M12": 12, "M6": 6, "M3": 3}                 # §4: 12-1, 6-1, 3-1 (price at end of m-13 / m-7 / m-4)
BLEND_PARTS = (12, 6)                                    # §4 BLEND = mean of z(12-1 / vol) and z(6-1 / vol)
Z_CUT = 3.0                                              # §4 each z cut at +-3
HI52_SESS = 252                                          # §4 HI52 window
SMOOTH_POOL = 50                                         # §4 SMOOTH: the 50 best of E by 12-1
PRIMARY = "BLEND"
REPORT_ARMS = ("M12", "M6", "M3", "HI52", "SMOOTH")       # §4 report only
ARMS = (PRIMARY,) + REPORT_ARMS
COST = 0.004                                             # §7 verdict cost per side
COST_LEVELS = (0.0015, 0.010)                            # §7 printed beside
LOCK_TRIES = J.LOCK_TRIES                                # §2 locked entry: up to 5 more sessions
DEAD_GAP = J.DEAD_GAP                                    # §2 20 sessions without a row
# §7 impact model (scaling table only; an assumption)
STAT_COST = 0.0015
HALF_SPREAD = ((50e7, 0.0010), (10e7, 0.0015), (0.0, 0.0025))   # (T60 floor in Rs, half-spread): >=50 Cr, 10-50, <10
SIGMA_N = 20
T60_N = 60
MAX_SIDE_COST = 0.50                                     # impact cap when T60 is ~0 (deviation log: keeps it finite)
SCALE_SIZES = (1e6, 2.5e6, 5e6, 1e7, 5e7, 1e8, 2.5e8)    # §7 10L .. 25 Cr
TV_SHARES = (0.05, 0.20)                                 # §7 share of trades above 5% / 20% of T60
TRADE_DAY_SHARE = 0.02                                   # §7 days to trade at 2% of T60 a day
CAP_MAX_DAYS = 5.0                                       # §7 capacity: worst trade <= 5 days ...
CAP_KEEP = 0.80                                          # ... AND net return keeps >= 80% of the 10L figure
CAP_FLAG = 1e7                                           # §8 flag `capacity < 1 Cr`
# §7 tax (recall; confirm with the CA -- P12).  Owner Q10: today's rates on every year, no surcharge.
CESS = 0.04
STCG_RATE = 0.20
LTCG_RATE = 0.125
LTCG_EXEMPT = 125_000.0
LT_MONTHS = 12                                           # long term iff sold MORE than 12 months after the buy
LOSS_CARRY_FY = 8
HIST_CHANGE_FY = 2024                                    # historical table: 15% / 10% / 1L before FY2024-25
HIST_STCG, HIST_LTCG, HIST_EXEMPT = 0.15, 0.10, 100_000.0
FUND_COST = 0.004                                        # §6 fund cost a year before each fund's launch (recall)
LIQUID_MONTHLY = 0.005                                   # §5 liquid fund printed beside the 0% cash (recall)
SESS_PER_MONTH = J.SESS_PER_MONTH
# §5 regime gates (verbatim from the M0 counts script header)
TREND_N = 200
IDX_MIN_HIST = 252                                       # the counts script skips a month with < 252 index rows
BREADTH_N, BREADTH_MIN_ROWS, BREADTH_OFF = 200, 190, 0.40
REBOUND_LONG, REBOUND_SHORT, REBOUND_JUMP = 252, 21, 0.08
MIN_EP_ALL, MIN_EP_OOS, MIN_OFF_OOS = 4, 2, 4            # §5 minimum to read a gate
READ_GATE = "G-TREND"                                    # owner Q6
GATES = ("G-TREND", "G-BREADTH", "G-REBOUND")
G1_CUT = 0.25                                            # §5 g1 OOS max DD at least a quarter smaller
G3_GIVEUP = 0.0015                                       # owner Q8: <= 0.15% a month
PLACEBO_SHUFFLES = 1000                                  # §5 g4
PLACEBO_MIN_OOS_OFF = 4
PLACEBO_MIN_KEPT = 200
# §8 statistics
IS_W = ("2020-01", "2023-12")
OOS_W = ("2024-01", "2026-10")
WHOLE = (IS_W[0], OOS_W[1])
BLOCK, BOOT_DRAWS, SEED = 6, 5000, 7
N_HYP = 3                                                # S1, S2, g4
LEVEL_READ = 1 - 0.05 / N_HYP                            # 98.33%
LEVEL_REPORT = 0.95
CRAND_DRAWS, CRAND_PCT = 1000, 95
FLAG_MONTHLY = 0.02                                      # `below 2%/month`
FLAG_DD = 35.0                                           # owner Q14
S5_DD_POINTS = 5.0
UNDERPOWER_S2 = 0.015
LOCKED_EXIT_SHARE = 0.01
DECAY_FRAC = 0.5
ONE_YEAR_SHARE = 0.5
BREADTH_BUCKETS = (0.40, 0.70)
VIX_BUCKETS = (15.0, 20.0)
POST_SWITCH_MONTHS = 2
FORWARD_FIRST = "2026-12"                                # §9
DUST_FRAC = 1e-6                                         # F3: a buy below 1e-6 x account is no buy (deviation log)
MUHURAT_RECALL = ("2019-10-27", "2020-11-14", "2021-11-04", "2022-10-24", "2023-11-12", "2024-11-01",
                  "2025-10-21")                          # F12: Diwali muhurat sessions (recall), checked in `signals`
# M0 / M1 known answers (design §1, §5, M1): a mismatch is a wiring error
P1_EXPECT = 12_300                                       # band names 2020-01..2026-10, all mapped
P2_MIN_E = 123
P3_MIN_OVERLAP = 104
EXPECT_OFF = {
    "G-TREND": ("2020-04", "2020-05", "2020-06", "2020-07", "2022-03", "2022-06", "2022-07", "2023-03", "2023-04",
                "2025-02", "2025-03", "2025-04", "2025-05", "2026-02", "2026-04", "2026-10"),
    "G-BREADTH": ("2020-04", "2020-05", "2020-06", "2022-07", "2025-03", "2025-04", "2025-05", "2026-04"),
    "G-REBOUND": ("2020-05", "2020-07"),
}
EXPECT_OFF_UNTIL = {"G-TREND": "2026-10", "G-BREADTH": "2026-10", "G-REBOUND": "2026-07"}
INDEX_FILES = {                                          # §6: file stem -> (label, kind)
    "mid150mom50_tri": ("Nifty Midcap150 Momentum 50", "TRI"), "mid150mom50_px": ("Nifty Midcap150 Momentum 50", "price"),
    "n200mom30_tri": ("Nifty200 Momentum 30", "TRI"), "n200mom30_px": ("Nifty200 Momentum 30", "price"),
    "sml250mq100_tri": ("Nifty Smallcap250 Momentum Quality 100", "TRI"),
    "sml250mq100_px": ("Nifty Smallcap250 Momentum Quality 100", "price"),
    "n500mom50_tri": ("Nifty500 Momentum 50", "TRI"), "n500mom50_px": ("Nifty500 Momentum 50", "price"),
    "midcap150_tri": ("Nifty Midcap 150", "TRI"), "midcap150_px": ("Nifty Midcap 150", "price"),
    "nifty50_tri": ("NIFTY 50", "TRI"), "nifty50_px": ("NIFTY 50", "price"),
    "fund_mid150mom50_direct": ("Edelweiss Nifty Midcap150 Momentum 50 Direct", "NAV"),
    "fund_uti_n200mom30_direct": ("UTI Nifty200 Momentum 30 Direct", "NAV"),
}
GATE_INDEX = "midcap150_px"                              # §5 G-TREND / G-REBOUND on the Midcap 150 price index
MATCH_TRI, MATCH_NAV = "mid150mom50_tri", "fund_mid150mom50_direct"      # IDX-MATCH (owner Q4)
OWNED_TRI, OWNED_NAV = "n200mom30_tri", "fund_uti_n200mom30_direct"      # IDX-OWNED (owner Q5)
PRINTED_INDICES = ("sml250mq100_tri", "n500mom50_tri", "midcap150_tri", "nifty50_tri", "sml250mq100_px",
                   "n500mom50_px", "midcap150_px", "nifty50_px", "mid150mom50_px", "n200mom30_px")
# index launch dates: (recall, UNVERIFIED -- P12).  Printed beside each line with the `backfilled index` flag.
LAUNCH_RECALL = {"Nifty200 Momentum 30": "2020-08 (recall)", "Nifty Midcap150 Momentum 50": "2022-08 (recall)",
                 "Nifty Smallcap250 Momentum Quality 100": "unknown (recall)", "Nifty500 Momentum 50": "unknown (recall)"}
RECALL_ITEMS = (                                         # P12: printed every run until confirmed
    "STCG 20% + 4% cess; LTCG 12.5% + cess above Rs 1.25L/FY; > 12 months = long term; losses carry 8 FYs; "
    "STCL sets off against STCG and LTCG, LTCL only against LTCG (CA)",
    "historical rates: 15% STCG / 10% LTCG above Rs 1L before FY2024-25 (the July-2024 change applied to the whole FY)",
    "fund cost 0.40% a year before each fund's launch (TRI less cost); liquid fund 0.5% a month",
    "statutory cost 0.15% a side inside the flat 0.40%; half-spreads 0.10 / 0.15 / 0.25%",
    "NSE momentum-index recipe (12 and 6 month return / volatility, z-scores cut at +-3); NSE does not skip the "
    "last month; index launch dates",
    "monthly trading taxed as capital gains, not business income (owner Q10)",
)

Window = Tuple[str, str]
Arr = NDArray[Any]
CostFn = Callable[[int, int, float], float]


# ---------------------------------------------------------------- small helpers
def madd(m: str, k: int) -> str:
    return J.madd(m, k)


def months_in(w: Window) -> List[str]:
    return J.month_range(w[0], w[1])


def in_w(m: str, w: Window) -> bool:
    return J.in_window(m, w)


def episodes(flags: Sequence[bool]) -> int:
    """Number of runs of consecutive True values (verbatim from the M0 counts script)."""
    n, prev = 0, False
    for f in flags:
        if f and not prev:
            n += 1
        prev = f
    return n


def add_months_date(d: str, k: int) -> date:
    """Calendar date k months after d; a day that does not exist is clamped to the month's last day."""
    x = date.fromisoformat(d)
    y, mo = divmod(x.month - 1 + k, 12)
    y += x.year
    mo += 1
    for day in (x.day, 30, 29, 28):
        try:
            return date(y, mo, min(x.day, day))
        except ValueError:
            continue
    raise ValueError(f"no date {k} months after {d}")


def is_long_term(buy: str, sell: str) -> bool:
    """§7 / P12: listed equity is long term iff held MORE than 12 months (sold after the same date a year on)."""
    return date.fromisoformat(sell) > add_months_date(buy, LT_MONTHS)


def fmt_p(x: Optional[float], w: int = 6) -> str:
    return " " * (w - 3) + "n/a" if x is None or x != x else f"{x:{w}.2%}"


def read_json(f: Path) -> Any:
    if not f.exists():
        raise SystemExit(f"missing {f}")
    return json.loads(f.read_text(encoding="utf-8"))


def write_json_atomic(f: Path, obj: Any) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    os.replace(tmp, f)


def peak_rss_mb() -> Optional[float]:
    """Peak working set of this process in MB (Windows kernel32 K32GetProcessMemoryInfo); None elsewhere."""
    if sys.platform != "win32":
        return None
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    try:
        k = ctypes.WinDLL("kernel32")
        k.GetCurrentProcess.restype = wintypes.HANDLE
        fn = k.K32GetProcessMemoryInfo
        fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        fn.restype = wintypes.BOOL
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        ok = fn(k.GetCurrentProcess(), ctypes.byref(c), c.cb)
    except (AttributeError, OSError) as e:
        LOG.warning("peak RSS unavailable: %s", e)
        return None
    return round(c.PeakWorkingSetSize / 2 ** 20, 1) if ok else None


# ---------------------------------------------------------------- data loading (§1, §6, M1)
def load_index(name: str, root: Optional[Path] = None) -> Dict[str, float]:
    f = (root or INDEX_DIR) / f"{name}.json"
    d = read_json(f)
    if not isinstance(d, dict) or not d:
        raise ValueError(f"{f}: expected a non-empty {{date: value}} object")
    out = {}
    for k, v in d.items():
        if len(k) != 10 or not isinstance(v, (int, float)) or not v > 0:
            raise ValueError(f"{f}: bad row {k}: {v}")
        out[k] = float(v)
    return out


def load_vix(f: Optional[Path] = None) -> Dict[str, float]:
    f = f or VIX_FILE
    if not f.exists():
        LOG.warning("India VIX file absent at %s: the VIX rows of the regime map are skipped", f)
        return {}
    d = read_json(f)
    return {str(b[0])[:10]: float(b[4]) for b in d["bars"]}


def load_events() -> Dict[str, Dict[str, Any]]:
    """§2 / P9: the joiner events file (read only: it feeds the published MOM series) plus this study's own file.
    An ISIN in both must agree, else it is an error (never silently overridden)."""
    ev = J.load_events()
    if EXTRA_EVENTS_FILE.exists():
        extra = read_json(EXTRA_EVENTS_FILE)
        for isin, x in extra.items():
            if not isinstance(x, dict) or x.get("type") not in J.EVENT_TYPES:
                raise ValueError(f"{EXTRA_EVENTS_FILE}: bad event for {isin}: {x}")
            if isin in ev and ev[isin].get("type") != x.get("type"):
                raise ValueError(f"{isin}: {EXTRA_EVENTS_FILE.name} type {x.get('type')} disagrees with "
                                 f"{J.EVENTS_FILE.name} type {ev[isin].get('type')}")
            ev.setdefault(isin, x)
    return ev


def load_sectors() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if INDUSTRY_CSV.exists():
        import csv
        with INDUSTRY_CSV.open(encoding="utf-8", errors="replace") as fh:
            for r in csv.DictReader(fh):
                if r.get("Symbol") and r.get("Industry"):
                    out[r["Symbol"].strip()] = r["Industry"].strip()
    if SECTOR_FILE.exists():
        out.update({str(k): str(v) for k, v in read_json(SECTOR_FILE).items()})
    return out


@dataclass
class BandFile:
    month: str
    rebalance: Optional[str]
    names: List[Tuple[int, str, str]]                    # (rank, symbol, isin)


def load_band_file(m: str, root: Optional[Path] = None) -> BandFile:
    """§1 reader of nonfo_srs's B150 files ({month, rebalance, names: [{symbol, isin, rank}]})."""
    f = (root or B150_DIR) / f"b150_{m}.json"
    d = read_json(f)
    names = [(int(x["rank"]), str(x["symbol"]), str(x["isin"])) for x in d["names"]]
    if [r for r, _s, _i in names] != list(range(1, len(names) + 1)):
        raise ValueError(f"{f}: ranks are not 1..{len(names)}")
    return BandFile(str(d["month"]), d.get("rebalance"), names)


# ---------------------------------------------------------------- the market: panel facts shared by every layer
@dataclass(frozen=True)
class Stop:
    """§2 a stopped name: t_gap = first session of a >= DEAD_GAP gap (joiner name_path's d), last = its last row."""
    t_gap: int
    last: int
    kind: str                                            # swap | close | dead | pending
    into: int = -1


class Market:
    """Panel facts every layer reads.  The panel's returns must already be corporate-action adjusted (ca_adjust)."""

    def __init__(self, p: J.Panel, bands: Dict[str, BandFile], members: Dict[str, Set[int]],
                 events: Dict[str, Dict[str, Any]]) -> None:
        self.p = p
        self.S = p.S
        self.sessions = p.sessions
        self.events = events
        self.members = members
        ret = p.ret
        self.G = p.G
        hr = p.has_row
        with np.errstate(invalid="ignore"):
            up = (p.close == p.high) & (ret >= J.LOCK_PCT)
            dn = (p.close == p.low) & (ret <= -J.LOCK_PCT)
        self.enter_ok = hr & ~up                         # joiner name_path entry test
        self.exit_ok = hr & ~dn                          # joiner name_path exit test
        self.lock_day = hr & (up | dn)
        del up, dn
        self.first_row = np.where(hr.any(axis=1), np.argmax(hr, axis=1), self.S)
        self._rows: Dict[int, Tuple[Arr, List[int]]] = {}
        self._isin_first: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
        for c in range(p.C):
            r = hr[c]
            if not r.any():
                continue
            u, first = np.unique(p.isin_idx[c, r], return_index=True)
            ts = np.nonzero(r)[0][first]
            for i, t in zip(u.tolist(), ts.tolist()):
                self._isin_first[i].append((c, t))
        self.bands = bands
        self.band: Dict[str, List[int]] = {}
        self.band_sym: Dict[str, Dict[int, str]] = {}
        self.band_unmapped: Dict[str, List[str]] = {}
        for m, bf in bands.items():
            s = self.sig(m)
            cs: List[int] = []
            syms: Dict[int, str] = {}
            miss: List[str] = []
            for _r, sym, isin in bf.names:
                ch = self.chain_of_isin(isin, s)
                if ch is None:
                    miss.append(sym)
                    continue
                if ch not in syms:
                    cs.append(ch)
                    syms[ch] = sym
            self.band[m], self.band_sym[m], self.band_unmapped[m] = cs, syms, miss
        self._t60: Dict[Tuple[int, int], float] = {}
        self._sig20: Dict[Tuple[int, int], float] = {}

    # -- calendar
    def sig(self, m: str) -> int:
        """§2 signal day of month m: the last panel session before m-01 (works for a month not yet in the panel)."""
        s = bisect.bisect_left(self.sessions, m + "-01") - 1
        if s < 0:
            raise ValueError(f"{m}: no session before it")
        return s

    def first(self, m: str) -> int:
        if m not in self.p.months:
            raise KeyError(f"month {m} has no session in the panel")
        return self.p.months[m][0]

    def last(self, m: str) -> int:
        if m not in self.p.months:
            raise KeyError(f"month {m} has no session in the panel")
        return self.p.months[m][1]

    # -- identity
    def chain_of_isin(self, isin: str, t: int) -> Optional[int]:
        """== joiner Panel.chain_of_isin (the lowest chain that carried the ISIN on a row at or before t), fast."""
        i = self.p.isin_of.get(isin)
        if i is None:
            return None
        hits = [c for c, t0 in self._isin_first.get(i, []) if t0 <= t]
        return min(hits) if hits else None

    def sym_at(self, c: int, t: int) -> str:
        lr = self.last_row(c, t)
        return self.p.syms[self.p.sym_idx[c, lr]] if lr >= 0 else f"#{c}"

    def isin_at(self, c: int, t: int) -> str:
        lr = self.last_row(c, t)
        return self.p.isins[self.p.isin_idx[c, lr]] if lr >= 0 else ""

    def _row_info(self, c: int) -> Tuple[Arr, List[int]]:
        if c not in self._rows:
            rows = np.nonzero(self.p.has_row[c])[0]
            cand: List[int] = []
            if len(rows):
                gaps = np.diff(rows) - 1
                cand = (rows[:-1][gaps >= DEAD_GAP] + 1).tolist()
                if rows[-1] + 1 < self.S:
                    cand.append(int(rows[-1]) + 1)
            self._rows[c] = (rows, cand)
        return self._rows[c]

    def last_row(self, c: int, t: int) -> int:
        rows, _ = self._row_info(c)
        k = int(np.searchsorted(rows, t, side="right")) - 1
        return int(rows[k]) if k >= 0 else -1

    def has_price_by(self, c: int, t: int) -> bool:
        """§1 'has a price at the end of month m-13' = a row on or before t (joiner mom_12_1's test)."""
        return bool(self.first_row[c] <= t)

    # -- stopped names (§2; the joiner name_path rule, one function for every layer)
    def stop_session(self, c: int, t0: int) -> Optional[int]:
        """First session t >= t0 with no row on c and no row in [t, t + DEAD_GAP)."""
        if t0 >= self.S:
            return None
        rows, cand = self._row_info(c)
        if not self.p.has_row[c, t0]:
            k = int(np.searchsorted(rows, t0))
            if k == len(rows) or rows[k] - t0 >= DEAD_GAP:
                return t0
        j = bisect.bisect_left(cand, t0)
        return cand[j] if j < len(cand) else None

    def next_stop(self, c: int, t0: int) -> Optional[Stop]:
        t = self.stop_session(c, t0)
        if t is None:
            return None
        last = self.last_row(c, t)
        isin = self.p.isins[self.p.isin_idx[c, last]] if last >= 0 else ""
        ev = self.events.get(isin, {})
        typ, into = ev.get("type"), ev.get("into_isin")
        if typ in J.MERGE_TYPES and into:
            a = self.chain_of_isin(into, last)
            if a is not None and self.has_price_by(a, last):
                return Stop(t, last, "swap", a)
        if typ in J.LAST_CLOSE_TYPES:
            return Stop(t, last, "close")
        return Stop(t, last, "pending" if self.S - t < DEAD_GAP else "dead")

    # -- universes (§1)
    def eligible(self, m: str) -> List[int]:
        """E: band names (rank order) with a price at the end of m-13."""
        a = madd(m, -HIST_MONTHS)
        ta = self.last(a)
        return [c for c in self.band[m] if self.has_price_by(c, ta)]

    def fo_pool(self, m: str) -> List[int]:
        """FO parallel universe (report only): point-in-time F&O members of m with the same history filter."""
        ta = self.last(madd(m, -HIST_MONTHS))
        s = self.sig(m)
        return sorted(c for c in self.members.get(m, set()) if self.has_price_by(c, ta) and self.p.has_row[c, s])

    def pool(self, name: str, m: str) -> List[int]:
        if name == "E":
            return self.eligible(m)
        if name == "BAND":
            return list(self.band[m])
        if name == "FO":
            return self.fo_pool(m)
        raise ValueError(f"unknown pool {name}")

    # -- liquidity (§7)
    def t60(self, c: int, s: int) -> float:
        """Median EQ traded value (Rs) of the T60_N sessions ending at s; a session without an EQ row counts 0."""
        k = (c, s)
        if k not in self._t60:
            lo = max(0, s - T60_N + 1)
            eq = (self.p.series[c, lo:s + 1] == J.EQ) & self.p.has_row[c, lo:s + 1]
            v = np.where(eq, np.nan_to_num(self.p.tv[c, lo:s + 1].astype(np.float64)), 0.0)
            v = np.concatenate([v, np.zeros(T60_N - len(v))])
            self._t60[k] = float(np.median(v))
        return self._t60[k]

    def sigma20(self, c: int, s: int) -> float:
        k = (c, s)
        if k not in self._sig20:
            lo = max(0, s - SIGMA_N + 1)
            r = self.p.ret[c, lo:s + 1][self.p.has_row[c, lo:s + 1]]
            self._sig20[k] = float(np.std(r, ddof=1)) if len(r) >= 2 else 0.0
        return self._sig20[k]


def flat_px(mk: Market) -> Arr:
    """The price-free valuation index (all 1.0, a read-only broadcast view: no memory).  Holdings decided under it
    equal the holdings under G (layer 2 of the header), and no number computed under it is a return."""
    return np.broadcast_to(np.float64(1.0), mk.G.shape)


# ---------------------------------------------------------------- §4 scores (layer 1)
def zcut(v: float, cut: float = Z_CUT) -> float:
    return max(-cut, min(cut, v))


def rank_key(score: Optional[float], sym: str) -> Tuple[float, str]:
    return (-score if score is not None and score == score else math.inf, sym)


class Scorer:
    """§4 scores known at the signal day of m (nothing after it is read: test_future_spike_changes_nothing)."""

    def __init__(self, mk: Market) -> None:
        self.mk = mk
        self._ret: Dict[Tuple[int, str, int], Optional[float]] = {}
        self._vol: Dict[Tuple[int, str, int], Optional[float]] = {}
        self._mom: Dict[Tuple[str, str, int], Tuple[float, float]] = {}
        self._order: Dict[Tuple[str, str, str], List[int]] = {}
        self._score: Dict[Tuple[str, str, str, int], Optional[float]] = {}

    def window(self, m: str, k: int) -> Tuple[int, int]:
        """(ta, tb): end of m-(k+1) to end of m-2 (the 12-1 window is the seen arm's, joiner mom_12_1)."""
        return self.mk.last(madd(m, -(k + 1))), self.mk.last(madd(m, -SKIP_MONTHS))

    def ret_k(self, c: int, m: str, k: int) -> Optional[float]:
        key = (c, m, k)
        if key not in self._ret:
            ta, tb = self.window(m, k)
            G = self.mk.G
            self._ret[key] = float(G[c, tb] / G[c, ta] - 1) if self.mk.has_price_by(c, ta) else None
        return self._ret[key]

    def vol_k(self, c: int, m: str, k: int) -> Optional[float]:
        """Daily volatility over the score window: sample std of the daily returns on the sessions with a row."""
        key = (c, m, k)
        if key not in self._vol:
            ta, tb = self.window(m, k)
            p = self.mk.p
            r = p.ret[c, ta + 1:tb + 1][p.has_row[c, ta + 1:tb + 1]]
            v = float(np.std(r, ddof=1)) if len(r) >= 2 else 0.0
            self._vol[key] = v if v > 0 else None
        return self._vol[key]

    def ratio(self, c: int, m: str, k: int) -> Optional[float]:
        r, v = self.ret_k(c, m, k), self.vol_k(c, m, k)
        return None if r is None or v is None else r / v

    def moments(self, pool: str, m: str, k: int) -> Tuple[float, float]:
        """Mean and population std of the return / vol ratio inside the pool (NaN if fewer than 2 defined)."""
        key = (pool, m, k)
        if key not in self._mom:
            xs = [x for c in self.mk.pool(pool, m) for x in [self.ratio(c, m, k)] if x is not None]
            self._mom[key] = (float(np.mean(xs)), float(np.std(xs))) if len(xs) >= 2 else (math.nan, math.nan)
        return self._mom[key]

    def blend(self, c: int, m: str, pool: str) -> Optional[float]:
        zs = []
        for k in BLEND_PARTS:
            x = self.ratio(c, m, k)
            mu, sd = self.moments(pool, m, k)
            if x is None or not sd > 0:
                return None
            zs.append(zcut((x - mu) / sd))
        return sum(zs) / len(zs)

    def hi52(self, c: int, m: str) -> Optional[float]:
        s = self.mk.sig(m)
        p = self.mk.p
        lo = max(0, s - HI52_SESS + 1)
        rows = p.has_row[c, lo:s + 1]
        if not rows.any() or not self.mk.has_price_by(c, s):
            return None
        g = self.mk.G[c, lo:s + 1][rows]
        return float(self.mk.G[c, s] / g.max())

    def smooth(self, c: int, m: str) -> Optional[float]:
        ta, tb = self.window(m, 12)
        p = self.mk.p
        rows = p.has_row[c, ta + 1:tb + 1]
        if not rows.any():
            return None
        r = p.ret[c, ta + 1:tb + 1][rows]
        return float((np.sum(r > 0) - np.sum(r < 0)) / len(r))

    def score(self, arm: str, c: int, m: str, pool: str) -> Optional[float]:
        key = (arm, m, pool, c)
        if key not in self._score:
            if arm == "BLEND":
                v = self.blend(c, m, pool)
            elif arm in LOOKBACK:
                v = self.ret_k(c, m, LOOKBACK[arm])
            elif arm == "HI52":
                v = self.hi52(c, m)
            elif arm == "SMOOTH":
                v = self.smooth(c, m)
            else:
                raise ValueError(f"unknown arm {arm}")
            self._score[key] = v
        return self._score[key]

    def sym(self, c: int, m: str) -> str:
        return self.mk.sym_at(c, self.mk.sig(m))

    def _key(self, arm: str, c: int, m: str, pool: str) -> Tuple[Any, ...]:
        if arm == "HI52":                                # ties (many names at 1.0): 12-1 return, then symbol
            r12 = self.ret_k(c, m, 12)
            return (rank_key(self.score(arm, c, m, pool), "")[0], -r12 if r12 is not None else math.inf,
                    self.sym(c, m))
        return rank_key(self.score(arm, c, m, pool), self.sym(c, m))

    def order(self, arm: str, m: str, pool: str, held: Iterable[int] = ()) -> List[int]:
        """Pool plus held names, best first; names without a score are left out (a held one is then sold)."""
        if arm == "SMOOTH":
            key = (arm, m, pool)
            if key not in self._order:
                base = self.order("M12", m, pool)[:SMOOTH_POOL]
                self._order[key] = sorted(base, key=lambda c: self._key("SMOOTH", c, m, pool))
            return list(self._order[key])                # held names outside the 50 have no rank: sold
        key = (arm, m, pool)
        if key not in self._order:
            cs = [c for c in self.mk.pool(pool, m) if self.score(arm, c, m, pool) is not None]
            self._order[key] = sorted(cs, key=lambda c: self._key(arm, c, m, pool))
        base = self._order[key]
        bs = set(base)
        extra = [c for c in set(held) if c not in bs and self.score(arm, c, m, pool) is not None]
        if not extra:
            return list(base)
        return sorted(base + extra, key=lambda c: self._key(arm, c, m, pool))


def rebalance(held: Sequence[int], order: Sequence[int], slots: int, sell_rank: int
              ) -> Tuple[List[int], List[int], List[int]]:
    """§3 rank buffer.  held: chains held now; order: pool + held, best first (unranked names absent).
    -> (keep, sell, buy).  A held name is kept iff its rank <= sell_rank; the free slots are filled with the
    best-ranked names not held (pool names: held names outside the pool are never buy candidates)."""
    if slots < 0 or sell_rank < 1:
        raise ValueError(f"bad slots {slots} / sell rank {sell_rank}")
    pos = {c: i + 1 for i, c in enumerate(order)}
    keep = [c for c in held if pos.get(c, math.inf) <= sell_rank]
    sell = [c for c in held if pos.get(c, math.inf) > sell_rank]
    hs = set(held)
    free = max(0, slots - len(keep))
    buy = [c for c in order if c not in hs][:free]
    return keep, sell, buy


# ---------------------------------------------------------------- §5 regime gates (verbatim from the counts script)
def _index_pos(dates: List[str], sd: str) -> int:
    i = bisect.bisect_right(dates, sd) - 1
    if i < 0:
        raise ValueError(f"index starts after {sd}")
    return i


def index_states(mk: Market, idx: Dict[str, float], ms: Sequence[str]) -> Tuple[Dict[str, bool], Dict[str, bool]]:
    """(G-TREND, G-REBOUND) OFF flags.  Close on the signal day (or the last index date before it).  A month with
    fewer than IDX_MIN_HIST index rows is left out (and then reported as undefined, never assumed ON)."""
    dates = sorted(idx)
    c = np.array([idx[d] for d in dates], dtype=float)
    g1: Dict[str, bool] = {}
    g3: Dict[str, bool] = {}
    for m in ms:
        sd = mk.sessions[mk.sig(m)]
        if sd > dates[-1]:
            continue
        i = _index_pos(dates, sd)
        if i < IDX_MIN_HIST:
            continue
        g1[m] = bool(c[i] < c[i - TREND_N + 1:i + 1].mean())
        g3[m] = bool(c[i] / c[i - REBOUND_LONG] - 1 < 0 and c[i] / c[i - REBOUND_SHORT] - 1 > REBOUND_JUMP)
    return g1, g3


def gate_trend(mk: Market, idx: Dict[str, float], ms: Sequence[str]) -> Dict[str, bool]:
    return index_states(mk, idx, ms)[0]


def gate_rebound(mk: Market, idx: Dict[str, float], ms: Sequence[str]) -> Dict[str, bool]:
    return index_states(mk, idx, ms)[1]


def breadth(mk: Market, m: str) -> Optional[float]:
    """Share of the month-m band (mapped names) above their own 200-session mean of the adjusted price, over names
    with a row on >= 190 of the last 200 sessions; None if no name qualifies."""
    s = mk.sig(m)
    if s < BREADTH_N - 1:
        return None
    G, hr = mk.G, mk.p.has_row
    h200 = [c for c in mk.band[m] if hr[c, s - BREADTH_N + 1:s + 1].sum() >= BREADTH_MIN_ROWS]
    if not h200:
        return None
    above = sum(1 for c in h200 if G[c, s] > G[c, s - BREADTH_N + 1:s + 1].mean())
    return above / len(h200)


def gate_breadth(mk: Market, ms: Sequence[str]) -> Dict[str, bool]:
    out = {}
    for m in ms:
        b = breadth(mk, m)
        out[m] = bool(b is not None and b < BREADTH_OFF)
    return out


def gate_minimum(off: Dict[str, bool], whole: Window = WHOLE, oos: Window = OOS_W) -> Tuple[bool, Dict[str, int]]:
    """§5 minimum to read a gate: >= 4 episodes in the whole window AND >= 2 episodes and >= 4 OFF months in OOS.
    A month without a state fails the minimum (never assumed ON)."""
    ms = months_in(whole)
    if any(m not in off for m in ms):
        return False, dict(missing=sum(m not in off for m in ms))
    fa = [off[m] for m in ms]
    fo = [off[m] for m in ms if in_w(m, oos)]
    c = dict(off_all=sum(fa), ep_all=episodes(fa), off_oos=sum(fo), ep_oos=episodes(fo))
    return c["ep_all"] >= MIN_EP_ALL and c["ep_oos"] >= MIN_EP_OOS and c["off_oos"] >= MIN_OFF_OOS, c


def placebo_schedules(real: Sequence[bool], seed: int = SEED, shuffles: int = PLACEBO_SHUFFLES
                      ) -> List[List[bool]]:
    """§5 g4 fake gates with the same number of OFF months and episodes: every circular shift of the real schedule
    that keeps the episode count (a shift can split an episode across the wrap), plus `shuffles` random placements
    of the real episode lengths (random order, random ON gaps, interior gaps >= 1), numpy seed `seed`."""
    real = list(real)
    n, k_off, k_ep = len(real), sum(real), episodes(real)
    out: List[List[bool]] = []
    for j in range(1, n):
        sh = real[-j:] + real[:-j]
        if episodes(sh) == k_ep:
            out.append(sh)
    lens, run = [], 0
    for f in real + [False]:
        if f:
            run += 1
        elif run:
            lens.append(run)
            run = 0
    if k_ep == 0:
        return out
    rng = np.random.default_rng(seed)
    n_on = n - k_off
    free = n_on - (k_ep - 1)
    if free < 0:
        raise ValueError("schedule cannot be re-placed")
    for _ in range(shuffles):
        order = rng.permutation(len(lens))
        bars = np.sort(rng.choice(free + k_ep, size=k_ep, replace=False))
        parts = np.diff(np.concatenate([[-1], bars, [free + k_ep]])) - 1      # k_ep + 1 parts summing to free
        gaps = [int(parts[0])] + [int(x) + 1 for x in parts[1:-1]] + [int(parts[-1])]
        sched: List[bool] = [False] * gaps[0]
        for i, e in enumerate(order):
            sched += [True] * lens[e] + [False] * gaps[i + 1]
        assert len(sched) == n and sum(sched) == k_off and episodes(sched) == k_ep
        out.append(sched)
    return out


# ---------------------------------------------------------------- §7 costs, tax
def half_spread(t60: float) -> float:
    for floor, hs in HALF_SPREAD:
        if t60 >= floor:
            return hs
    return HALF_SPREAD[-1][1]


def impact(order: float, t60: float, sigma: float) -> float:
    """§7 cost per side = 0.15% + half-spread + sigma x sqrt(order / T60), capped at MAX_SIDE_COST."""
    if order < 0 or sigma < 0:
        raise ValueError("order and sigma must be >= 0")
    if t60 <= 0:
        return MAX_SIDE_COST
    return min(MAX_SIDE_COST, STAT_COST + half_spread(t60) + sigma * math.sqrt(order / t60))


Rates = Callable[[int], Tuple[float, float, float]]


def rates_today(fy: int) -> Tuple[float, float, float]:
    """(STCG, LTCG incl. cess, LTCG exemption) -- owner Q10: today's rates on every year."""
    return STCG_RATE * (1 + CESS), LTCG_RATE * (1 + CESS), LTCG_EXEMPT


def rates_historical(fy: int) -> Tuple[float, float, float]:
    if fy < HIST_CHANGE_FY:
        return HIST_STCG * (1 + CESS), HIST_LTCG * (1 + CESS), HIST_EXEMPT
    return rates_today(fy)


@dataclass
class TaxRow:
    fy: int
    st_net: float
    lt_net: float
    st_taxable: float
    lt_taxable: float
    tax: float
    carry_after: float


def tax_fy(fy: int, st_net: float, lt_net: float, carry: List[List[Any]], rates: Rates
           ) -> Tuple[float, TaxRow]:
    """One FY of §7 tax.  carry: [[origin FY, 'st' | 'lt', amount > 0], ...] oldest first, mutated in place.
    Current-year set-off first (a short-term loss against long-term gains; a long-term loss never against
    short-term gains), then brought-forward losses oldest first (st against st then lt; lt against lt only), losses
    usable for LOSS_CARRY_FY years, then the LTCG exemption on what is left.  Returns (tax, row)."""
    if not (math.isfinite(st_net) and math.isfinite(lt_net)):
        raise ValueError(f"non-finite gains in FY{fy}")
    s, lt = st_net, lt_net
    if s < 0 < lt:
        use = min(-s, lt)
        s, lt = s + use, lt - use
    new = [[fy, "st", -s]] if s < 0 else []
    new += [[fy, "lt", -lt]] if lt < 0 else []
    s, lt = max(s, 0.0), max(lt, 0.0)
    carry[:] = [x for x in carry if fy - x[0] <= LOSS_CARRY_FY and x[2] > 0]
    for x in carry:
        if x[1] == "st":
            use = min(x[2], s)
            s -= use
            x[2] -= use
        use = min(x[2], lt)
        lt -= use
        x[2] -= use
    carry[:] = [x for x in carry if x[2] > 1e-9] + new
    r_st, r_lt, exempt = rates(fy)
    lt_tax = max(0.0, lt - exempt)
    tax = s * r_st + lt_tax * r_lt
    return tax, TaxRow(fy, st_net, lt_net, s, lt_tax, tax, sum(x[2] for x in carry))


def fund_after_tax(v0: float, v1: float, buy: str, sell: str, rates: Rates, account: float = ACCOUNT) -> float:
    """§7 the fund pays nothing until sold: one lot, liquidated at the end, taxed in the sale FY."""
    w = account * v1 / v0
    gain = w - account
    fy = CMP.fy_of(sell)
    lt = is_long_term(buy, sell)
    tax, _row = tax_fy(fy, 0.0 if lt else gain, gain if lt else 0.0, [], rates)
    return w - tax


# ---------------------------------------------------------------- the book (layers 2 and 3)
@dataclass
class Lot:
    t: int
    units: float
    basis: float


@dataclass
class Pos:
    pid: int
    c: int                                               # current chain (changes on a swap)
    e: int                                               # first entry session
    month: str                                           # buy month
    lots: List[Lot]
    state: str = "held"                                  # held | selling | frozen
    sell_t: int = -1
    frozen: float = 0.0
    release: int = -1
    stop: Optional[Stop] = None
    stop_seq: int = 0
    chains: List[int] = field(default_factory=list)

    @property
    def units(self) -> float:
        return sum(x.units for x in self.lots)


@dataclass
class PosRecord:
    """One closed (or still open) position: for P4, P5, P8, P9, the scaling table and the forward file."""
    c0: int
    chains: List[int]
    e: int
    x: int                                               # exit fill / stop last row / -1 open
    month: str
    how: str                                             # sold | close | dead | pending | swap-chain | open
    deferred: int = 0
    long_term: bool = False


@dataclass
class Trade:
    t: int
    c: int
    side: int                                            # +1 buy, -1 sell
    amount: float                                        # Rs gross
    cost: float                                          # fraction charged
    deferred: int = 0


@dataclass
class Spec:
    """One book run.  mode: buffer (the §3 rule) | equal (EW-E / EWB: re-formed monthly) | crand (§6)."""
    arm: str = PRIMARY
    pool: str = "E"
    mode: str = "buffer"
    n: int = N_HOLD
    sell_rank: int = SELL_RANK
    gate: Optional[Dict[str, bool]] = None               # month -> OFF
    gate_mode: str = "flat"                              # flat | nonew
    cost: Union[float, str] = COST                       # flat fraction per side, or "impact"
    dead_zero: bool = True
    taxed: bool = False
    rates: Rates = rates_today
    start: str = WHOLE[0]
    end: str = WHOLE[1]
    account: float = ACCOUNT
    reequal: Set[str] = field(default_factory=set)
    cash_monthly: float = 0.0
    crand_k: Optional[Dict[str, int]] = None
    seed: int = SEED
    liquidate: bool = False
    decide: Optional[str] = None                         # forward: return the month's decision after the run


@dataclass
class BookResult:
    spec: Spec
    t0: int
    eq: Arr                                       # equity at sessions t0 - 1 .. t_end (index 0 = start)
    monthly: Dict[str, float]
    buys: Dict[str, int]
    sells: Dict[str, int]
    held: Dict[str, List[int]]                           # chains held after each rebalance (orders included)
    weights: Dict[str, Tuple[float, float]]              # month -> (largest weight, effective N) after rebalance
    records: List[PosRecord]
    trades: List[Trade]
    skipped: int
    tax_rows: List[TaxRow]
    final_after_tax: Optional[float]
    decision: Optional[Dict[str, List[int]]] = None
    idle_buys: int = 0                                   # rebalances with open slots and no free cash
    eq_at: Optional[Arr] = None                          # taxed runs: equity net of paid AND accrued-unpaid tax


class Book:
    """Daily book (layers 2 and 3).  Order of work in each session t: stops, deferred sells, deferred buys, the
    month's rebalance (first session of a month: sells, tax payment, buys), cash yield, equity."""

    def __init__(self, mk: Market, sc: Scorer, spec: Spec, px: Arr) -> None:
        if spec.mode not in ("buffer", "equal", "crand"):
            raise ValueError(f"bad mode {spec.mode}")
        if spec.gate_mode not in ("flat", "nonew"):
            raise ValueError(f"bad gate mode {spec.gate_mode}")
        if spec.mode == "equal" and not isinstance(spec.cost, float):
            raise ValueError("equal mode needs a flat cost")
        self.mk, self.sc, self.spec, self.px = mk, sc, spec, px
        self.free = spec.account
        self.reserved = 0.0
        self.liability = 0.0
        self.pos: Dict[int, Pos] = {}
        self.pid = 0
        self.pending_buys: List[List[Any]] = []          # [chain, amount, order session, tries, month]
        self.at: Dict[int, List[Tuple[str, int, int]]] = defaultdict(list)   # session -> (what, pid, seq)
        self.records: List[PosRecord] = []
        self.trades: List[Trade] = []
        self.skipped = 0
        self.gains: Dict[int, List[float]] = defaultdict(lambda: [0.0, 0.0])  # fy -> [st net, lt net]
        self.carry: List[List[Any]] = []
        self.tax_rows: List[TaxRow] = []
        self.fy_open: Optional[int] = None
        self.rng = np.random.default_rng(spec.seed)
        self.idle_buys = 0
        self._acc_dirty = True
        self._acc = 0.0

    # -- costs
    def cost(self, c: int, t: int, amount: float) -> float:
        if isinstance(self.spec.cost, float):
            return self.spec.cost
        if self.spec.cost == "impact":
            s = self.mk.sig(self.mk.sessions[t][:7]) if t > 0 else 0
            return impact(amount * self.scale, self.mk.t60(c, s), self.mk.sigma20(c, s))
        raise ValueError(f"bad cost {self.spec.cost}")

    @property
    def scale(self) -> float:
        """Rs per unit of book money (1.0: the book is run in rupees)."""
        return 1.0

    # -- positions
    def _value(self, q: Pos, t: int) -> float:
        if q.state == "frozen":
            return q.frozen
        return q.units * float(self.px[q.c, t])

    def _register_stop(self, q: Pos, t_from: int, now: int) -> None:
        q.stop_seq += 1
        st_ = self.mk.next_stop(q.c, t_from)
        q.stop = st_
        if st_ is None:
            return
        if st_.kind == "close" or (st_.kind in ("dead", "pending") and not self.spec.dead_zero):
            when, what = st_.last, "close"
        elif st_.kind == "swap":
            when, what = st_.t_gap, "swap"
        else:
            when, what = st_.t_gap, "zero"
        if when <= now:
            self._stop_event(what, q, now)
        else:
            self.at[when].append((what, q.pid, q.stop_seq))

    def _buy(self, c: int, t: int, amount: float, month: str, deferred: int = 0) -> None:
        cst = self.cost(c, t, amount)
        units = amount * (1 - cst) / float(self.px[c, t])
        self.trades.append(Trade(t, c, 1, amount, cst, deferred))
        held = [q for q in self.pos.values() if q.c == c and q.state == "held"]
        if held:                                         # a top-up (equal mode / re-equalisation): one more lot
            held[0].lots.append(Lot(t, units, amount))
            return
        self.pid += 1
        q = Pos(self.pid, c, t, month, [Lot(t, units, amount)], chains=[c])
        self.pos[q.pid] = q
        self._register_stop(q, t + 1, t)

    def _realize(self, q: Pos, frac: float, t: int, proceeds: float) -> None:
        """FIFO: sell `frac` of the position's units; book each lot's gain in its FY bucket (taxed runs)."""
        self._acc_dirty = True
        want = q.units * frac
        d_sell = self.mk.sessions[t]
        fy = CMP.fy_of(d_sell)
        tot_units = want
        left = want
        keep: List[Lot] = []
        for lot in q.lots:
            if left <= 1e-15:
                keep.append(lot)
                continue
            use = min(lot.units, left)
            share = use / lot.units
            basis = lot.basis * share
            gain = proceeds * (use / tot_units) - basis
            lt = is_long_term(self.mk.sessions[lot.t], d_sell)
            self.gains[fy][1 if lt else 0] += gain
            left -= use
            if lot.units - use > 1e-12 * max(1.0, lot.units):
                keep.append(Lot(lot.t, lot.units - use, lot.basis - basis))
        q.lots = keep

    def _sell(self, q: Pos, t: int, frac: float = 1.0) -> float:
        value = q.units * frac * float(self.px[q.c, t])
        cst = self.cost(q.c, t, value)
        proceeds = value * (1 - cst)
        deferred = max(0, t - q.sell_t) if q.state == "selling" and frac >= 1.0 else 0
        self.trades.append(Trade(t, q.c, -1, value, cst, deferred))
        lt = is_long_term(self.mk.sessions[q.e], self.mk.sessions[t])
        self._realize(q, frac, t, proceeds)
        self.free += proceeds
        if frac >= 1.0 or not q.lots:
            self.records.append(PosRecord(q.chains[0], list(q.chains), q.e, t, q.month, "sold", deferred, lt))
            del self.pos[q.pid]
        return proceeds

    def _stop_event(self, what: str, q: Pos, t: int) -> None:
        if q.pid not in self.pos or q.state == "frozen":
            return
        st_ = q.stop
        assert st_ is not None
        if what == "close":
            value = q.units * float(self.px[q.c, t])
            cst = self.cost(q.c, t, value)
            q.frozen = value * (1 - cst)
            self.trades.append(Trade(t, q.c, -1, value, cst, 0))
            self._realize(q, 1.0, t, q.frozen)
            q.state = "frozen"
            q.release = st_.t_gap + DEAD_GAP
            self.at[q.release].append(("release", q.pid, q.stop_seq))
            self.records.append(PosRecord(q.chains[0], list(q.chains), q.e, t, q.month,
                                          "close" if st_.kind == "close" else st_.kind, 0,
                                          is_long_term(self.mk.sessions[q.e], self.mk.sessions[t])))
        elif what == "zero":
            q.frozen = 0.0
            q.lots = []                                  # no tax loss booked for a -100% name (deviation log)
            q.state = "frozen"
            q.release = st_.t_gap + DEAD_GAP
            self.at[q.release].append(("release", q.pid, q.stop_seq))
            self.records.append(PosRecord(q.chains[0], list(q.chains), q.e, st_.last, q.month, st_.kind))
        elif what == "swap":
            a = st_.into
            k = float(self.px[q.c, st_.last]) / float(self.px[a, st_.last])
            for lot in q.lots:
                lot.units *= k                           # a merger is not a transfer: basis and date carry over
            q.c = a
            q.chains.append(a)
            self._register_stop(q, t, t)
        else:
            raise ValueError(what)

    def _release(self, q: Pos) -> None:
        self.free += q.frozen
        del self.pos[q.pid]

    def _order_sell(self, q: Pos, t: int) -> None:
        if q.state != "held":
            return
        q.state, q.sell_t = "selling", t
        if self.mk.exit_ok[q.c, t]:
            self._sell(q, t)

    def _order_buys(self, cs: Sequence[int], t: int, month: str) -> int:
        """Split the free cash equally over `cs`; returns the number of buy orders placed (0 when no cash)."""
        if not cs:
            return 0
        amt = self.free / len(cs)
        if not amt >= DUST_FRAC * self.spec.account:     # F3: no cash, or only dust: no buy, the slot stays open
            self.idle_buys += 1
            return 0
        self.free = 0.0
        for c in cs:
            if self.mk.enter_ok[c, t]:
                self._buy(c, t, amt, month)
            else:
                self.reserved += amt
                self.pending_buys.append([c, amt, t, 1, month])
        return len(cs)

    # -- tax
    def _assess(self, fy: int) -> None:
        st_net, lt_net = self.gains.pop(fy, [0.0, 0.0])
        tax, row = tax_fy(fy, st_net, lt_net, self.carry, self.spec.rates)
        self.tax_rows.append(row)
        self.liability += tax
        self._acc_dirty = True

    def accrued(self) -> float:
        """Tax accrued but not yet assessed: the tax due on every open FY bucket of realised gains if that FY ended
        now (carry and exemption applied in FY order on a copy).  After-tax equity = equity (already net of the
        assessed-unpaid liability) - this.  Unrealised gains carry no accrual (owner D12; deviation log D12)."""
        if self._acc_dirty:
            carry = [list(x) for x in self.carry]
            self._acc = sum(tax_fy(fy, g[0], g[1], carry, self.spec.rates)[0] for fy, g in sorted(self.gains.items()))
            self._acc_dirty = False
        return self._acc

    def _pay_tax(self, t: int) -> None:
        """F3: the tax is funded by one pro-rata trim of the whole book -- the free cash (which holds the day's sale
        proceeds, before the replacement buys) and every exitable holding are cut by the same fraction q, so the
        replacement buys and the holdings shrink together.  q = tax / (free + exitable value x (1 - cost)), capped
        at 1; an unpaid remainder is carried as a liability (counted in equity) to the next rebalance."""
        if self.liability <= 0:
            return
        cst = self.spec.cost if isinstance(self.spec.cost, float) else MAX_SIDE_COST
        sellable = [q for q in self.pos.values() if q.state == "held" and self.mk.exit_ok[q.c, t]]
        gross = sum(self._value(q, t) for q in sellable)
        base = self.free + gross * (1 - cst)
        if base <= 0:
            return
        frac = min(1.0, self.liability / base)
        cash_part = self.free * frac
        self.free -= cash_part
        self.liability -= cash_part
        if gross > 0 and frac > 0:
            before = self.free
            for q in sellable:
                self._sell(q, t, frac)
            pay = min(self.free - before, self.liability)
            self.free -= pay
            self.liability -= pay
        if abs(self.liability) < 1e-9 * max(1.0, self.spec.account):
            self.liability = 0.0

    # -- the month
    def _slots_taken(self) -> int:
        return sum(1 for q in self.pos.values() if q.state in ("held", "frozen")) + len(self.pending_buys)

    def _held_chains(self) -> List[int]:
        return [q.c for q in self.pos.values() if q.state == "held"] + [b[0] for b in self.pending_buys]

    def _decide(self, m: str) -> Tuple[List[int], List[int], bool]:
        """(sell chains, buy chains, off) for month m, from the holdings now.  Price-free."""
        sp = self.spec
        if sp.gate is not None and m not in sp.gate:
            raise KeyError(f"gate schedule has no state for {m} (F10: never assumed ON)")
        off = bool(sp.gate[m]) if sp.gate is not None else False
        held = [q.c for q in self.pos.values() if q.state == "held"]
        # F7: a chain still being sold (deferred lower-circuit exit) or frozen (stopped) is never a buy candidate
        busy = {q.c for q in self.pos.values() if q.state != "held"} | {b[0] for b in self.pending_buys}
        frozen = sum(1 for q in self.pos.values() if q.state == "frozen") + len(self.pending_buys)
        if off and sp.gate_mode == "flat":
            return held, [], True
        if sp.mode == "crand":
            k = (sp.crand_k or {}).get(m, 0)
            open_ = max(0, sp.n - len(held) - frozen)
            n_sell = max(0, min(len(held), k - open_))
            idx = self.rng.permutation(len(held))[:n_sell]
            sells = [held[i] for i in sorted(idx.tolist())]
            hs = set(held) | busy
            pool = [c for c in self.mk.pool(sp.pool, m) if c not in hs]
            n_buy = min(len(pool), open_ + n_sell)
            bi = self.rng.permutation(len(pool))[:n_buy]
            buys = [pool[i] for i in bi.tolist()]
        else:
            hs_ = set(held)
            order = [c for c in self.sc.order(sp.arm, m, sp.pool, held) if c in hs_ or c not in busy]
            _keep, sells, buys = rebalance(held, order, sp.n - frozen, sp.sell_rank)
        if off and sp.gate_mode == "nonew":
            buys = []
        return sells, buys, off

    def _equalise(self, targets: Sequence[int], t: int, month: str) -> None:
        """Equal weight over `targets` at the close of t, cost-consistent (flat cost).  Held names outside the
        targets must already be ordered sold.  A locked-down name cannot be trimmed, a locked-up name cannot be
        topped up or bought (skipped this month, counted)."""
        cst = self.spec.cost
        assert isinstance(cst, float)
        held = {q.c: q for q in self.pos.values() if q.state == "held"}
        fixed_v = 0.0
        trim: List[Tuple[Pos, float, bool, bool]] = []
        top: List[Tuple[Pos, float, bool, bool]] = []
        new: List[int] = []
        for c in targets:
            q = held.get(c)
            if q is None:
                if self.mk.enter_ok[c, t]:
                    new.append(c)
                else:
                    self.skipped += 1
                continue
            v = self._value(q, t)
            can_sell, can_buy = bool(self.mk.exit_ok[c, t]), bool(self.mk.enter_ok[c, t])
            if can_sell or can_buy:
                (trim if can_sell else top).append((q, v, can_sell, can_buy))
            else:
                fixed_v += v
        parts = trim + top

        def cash_after(tg: float) -> float:
            x = self.free
            for _q, v, cs_, cb in parts:
                if v > tg and cs_:
                    x += (v - tg) * (1 - cst)
                elif v < tg and cb:
                    x -= (tg - v) / (1 - cst)
            return x - len(new) * tg / (1 - cst)

        if not parts and not new:
            return
        lo, hi = 0.0, (self.free + sum(v for _q, v, _a, _b in parts)) + 1.0
        if cash_after(lo) < 0:
            return
        for _ in range(200):
            mid = (lo + hi) / 2
            if cash_after(mid) >= 0:
                lo = mid
            else:
                hi = mid
        tg = lo
        for qp, v, cs_, cb in parts:
            if v > tg and cs_ and v > 0:
                self._sell(qp, t, (v - tg) / v)
            elif v < tg and cb:
                self.free -= (tg - v) / (1 - cst)
                self._buy(qp.c, t, (tg - v) / (1 - cst), month)
        for c in new:
            amt = tg / (1 - cst)
            self.free -= amt
            self._buy(c, t, amt, month)
        if abs(self.free) < 1e-9 * max(1.0, self.spec.account):
            self.free = 0.0

    def _rebalance(self, m: str, t: int, res: Dict[str, Any]) -> None:
        sp = self.spec
        fy = CMP.fy_of(self.mk.sessions[t])
        if sp.taxed and self.fy_open is not None and fy > self.fy_open:
            for f in range(self.fy_open, fy):
                self._assess(f)
        self.fy_open = fy if self.fy_open is None or fy > self.fy_open else self.fy_open
        if sp.mode == "equal":
            targets = self.mk.pool(sp.pool, m)
            ts = set(targets)
            for q in list(self.pos.values()):
                if q.state == "held" and q.c not in ts:
                    self._order_sell(q, t)
            res["sells"][m] = sum(1 for q in self.pos.values() if q.state == "selling")
            if sp.taxed:
                self._pay_tax(t)
            before = {q.c for q in self.pos.values() if q.state == "held"}
            self._equalise(targets, t, m)
            res["buys"][m] = len([c for c in targets if c not in before])
        else:
            sells, buys, _off = self._decide(m)
            sset = list(sells)
            for q in list(self.pos.values()):
                if q.state == "held" and q.c in sset:
                    sset.remove(q.c)
                    self._order_sell(q, t)
            res["sells"][m] = len(sells)
            if sp.taxed:
                self._pay_tax(t)
            if m in sp.reequal and isinstance(sp.cost, float):
                keep = [q.c for q in self.pos.values() if q.state == "held"]
                self._equalise(keep + list(buys), t, m)
                buys = []
            res["buys"][m] = self._order_buys(buys, t, m)
        held = self._held_chains()
        res["held"][m] = held
        vals = [self._value(q, t) for q in self.pos.values() if q.state == "held"] + [b[1] for b in self.pending_buys]
        tot = sum(vals)
        if tot > 0:
            w = [v / tot for v in vals]
            res["weights"][m] = (max(w), 1.0 / sum(x * x for x in w))

    def run(self) -> BookResult:
        mk, sp = self.mk, self.spec
        ms = months_in((sp.start, sp.end))
        t0 = mk.first(ms[0])
        t_end = mk.last(ms[-1])
        rebal = {mk.first(m): m for m in ms}
        month_end = {mk.last(m): m for m in ms}
        daily_y = (1 + sp.cash_monthly) ** (1 / SESS_PER_MONTH) - 1 if sp.cash_monthly else 0.0
        eq = np.empty(t_end - t0 + 2)
        eq[0] = sp.account
        eq_at = np.empty(t_end - t0 + 2) if sp.taxed else None
        if eq_at is not None:
            eq_at[0] = sp.account
        res: Dict[str, Any] = dict(buys={}, sells={}, held={}, weights={})
        monthly: Dict[str, float] = {}
        prev_me = sp.account
        for t in range(t0, t_end + 1):
            for what, pid, seq in self.at.pop(t, []):
                q = self.pos.get(pid)
                if q is None or q.stop_seq != seq:
                    continue
                if what == "release":
                    self._release(q)
                else:
                    self._stop_event(what, q, t)
            for q in [q for q in self.pos.values() if q.state == "selling"]:
                if t > q.sell_t and self.mk.exit_ok[q.c, t]:
                    self._sell(q, t)
            still = []
            for b in self.pending_buys:
                c, amt, t_ord, tries, month = b
                if t <= t_ord:
                    still.append(b)
                    continue
                if self.mk.enter_ok[c, t]:
                    self.reserved -= amt
                    self._buy(c, t, amt, month, deferred=t - t_ord)
                    continue
                b[3] = tries + 1
                if b[3] > LOCK_TRIES:
                    self.reserved -= amt
                    self.free += amt
                    self.skipped += 1
                    continue
                still.append(b)
            self.pending_buys = still
            if t in rebal:
                self._rebalance(rebal[t], t, res)
            if daily_y and self.free > 0:
                self.free *= 1 + daily_y
            v = self.free + self.reserved - self.liability + sum(self._value(q, t) for q in self.pos.values())
            eq[t - t0 + 1] = v
            if eq_at is not None:
                eq_at[t - t0 + 1] = v - self.accrued()
            if t in month_end:
                monthly[month_end[t]] = v / prev_me - 1 if prev_me > 0 else 0.0
                prev_me = v
        final = None
        if sp.liquidate:
            final = self._liquidate(t_end)
        for q in self.pos.values():
            if q.state != "frozen":
                self.records.append(PosRecord(q.chains[0], list(q.chains), q.e, -1, q.month, "open"))
        decision = None
        if sp.decide is not None:
            sells, buys, off = self._decide(sp.decide)
            decision = dict(sell=sells, buy=buys, off=[1] if off else [],
                            hold=[c for c in self._held_chains() if c not in sells] + buys)
        return BookResult(sp, t0, eq, monthly, res["buys"], res["sells"], res["held"], res["weights"], self.records,
                          self.trades, self.skipped, self.tax_rows, final, decision, self.idle_buys, eq_at)

    def _liquidate(self, t_end: int) -> float:
        """§7 liquidate everything at the end of the window (a locked-down name at its next exitable session in the
        panel, else its last close), then tax the final FY.  The book state is consumed."""
        for q in list(self.pos.values()):
            if q.state == "frozen":
                self.free += q.frozen
                del self.pos[q.pid]
                continue
            t = t_end
            while t < self.mk.S - 1 and not self.mk.exit_ok[q.c, t]:
                t += 1
            if not self.mk.p.has_row[q.c, t]:
                t = max(self.mk.last_row(q.c, t), q.e)
            q.state = "held"
            self._sell(q, t)
        self.free += self.reserved
        self.reserved = 0.0
        if self.spec.taxed:
            fy = CMP.fy_of(self.mk.sessions[t_end])
            if self.fy_open is not None:
                for f in range(self.fy_open, fy + 1):
                    self._assess(f)
            self.fy_open = fy + 1
        return self.free - self.liability


def simulate(mk: Market, sc: Scorer, spec: Spec, px: Arr) -> BookResult:
    return Book(mk, sc, spec, px).run()


# ---------------------------------------------------------------- KA-0: the sleeve bridge (design §11)
def position_net(mk: Market, c: int, f: int, hold: int, cost: float, dead_zero: bool, bounds: Sequence[int],
                 px: Arr) -> Optional[Tuple[List[float], int]]:
    """One fixed-hold position stepped with THIS module's primitives (Market.enter_ok / exit_ok / next_stop and
    units x price valuation) under the joiner's valuation convention: 1.0 of money before the entry, entry cost
    at the entry close, exit cost at the exit (or the merged name's last) close, a -100% name at 0 from the gap.
    -> (net value at each bound, end session), or None if the entry was skipped."""
    S = mk.S
    e = None
    for k in range(LOCK_TRIES + 1):
        t = f + k
        if t >= S:
            break
        if mk.enter_ok[c, t]:
            e = t
            break
    if e is None:
        return None
    units = (1 - cost) / float(px[c, e])
    segs: List[Tuple[int, int, float]] = []              # (from session, chain, units) -- valuation segments
    cur, seg_from = c, e
    x0 = e + hold
    t = e + 1
    end_kind, end_t = "open", 10 ** 9
    final_val = 0.0
    while True:
        stp = mk.stop_session(cur, t)
        xs = np.nonzero(mk.exit_ok[cur, max(x0, t):])[0]
        fill = int(xs[0]) + max(x0, t) if len(xs) else None
        if fill is not None and (stp is None or fill < stp):
            segs.append((seg_from, cur, units))
            end_kind, end_t = "sold", fill
            final_val = (1 - cost) * units * float(px[cur, fill])
            break
        if stp is None:
            segs.append((seg_from, cur, units))
            break
        sp_ = mk.next_stop(cur, stp)
        assert sp_ is not None
        if sp_.kind == "swap":
            segs.append((seg_from, cur, units))
            units = units * float(px[cur, sp_.last]) / float(px[sp_.into, sp_.last])
            cur, seg_from, t = sp_.into, stp, stp
            continue
        segs.append((seg_from, cur, units))
        if sp_.kind == "close" or not dead_zero:
            end_kind, end_t = "sold", sp_.last
            final_val = (1 - cost) * units * float(px[cur, sp_.last])
        else:
            end_kind, end_t = "zero", stp
        break

    def val(b: int) -> float:
        if b < e:
            return 1.0
        if end_kind == "sold" and b >= end_t:
            return final_val
        if end_kind == "zero" and b >= end_t:
            return 0.0
        k = bisect.bisect_right([s0 for s0, _c, _u in segs], b) - 1
        _s0, ch, u = segs[k]
        return u * float(px[ch, min(b, S - 1)])

    return [val(b) for b in bounds], end_t


def sleeve_series(mk: Market, picks: Dict[str, List[int]], hold: int, cost: float, dead_zero: bool,
                  last_month: str, px: Arr) -> Dict[str, Tuple[float, int]]:
    """KA-0 new side: equal-weight fixed-hold cohorts (entered names only), hold / 21 sleeves, month return = mean
    over live sleeves; a sleeve compounds the exiting cohort's tail and the entering cohort's first month."""
    sleeves = max(1, round(hold / SESS_PER_MONTH))
    acc: Dict[str, Dict[int, float]] = defaultdict(dict)
    for m in sorted(picks):
        f = mk.first(m)
        bounds = [(T, mk.last(T)) for T in J.month_range(m, last_month) if T in mk.p.months]
        bs = [b for _T, b in bounds]
        vals, ends = [], []
        for c in picks[m]:
            r = position_net(mk, c, f, hold, cost, dead_zero, bs, px)
            if r is not None:
                vals.append(r[0])
                ends.append(r[1])
        if not vals:
            continue
        end = max(ends)
        prev = 1.0
        k = (int(m[:4]) * 12 + int(m[5:7]) - 1) % sleeves
        for i, (T, _b) in enumerate(bounds):
            if i > 0 and bounds[i - 1][1] >= end:
                break
            if prev <= 0:
                break
            v = sum(x[i] for x in vals) / len(vals)
            r_ = v / prev - 1
            acc[T][k] = (1 + acc[T].get(k, 0.0)) * (1 + r_) - 1
            prev = v
    return {T: (sum(v.values()) / len(v), len(v)) for T, v in sorted(acc.items())}


def ka0_picks(mk: Market, sc: Scorer, joiner_bands: Dict[str, Tuple[List[int], Dict[int, str]]]
              ) -> Dict[str, List[int]]:
    """12-1 top 30 inside the joiner band, through THIS module's score (Scorer.ret_k), ties by the joiner symbol."""
    out = {}
    for m, (B, sym) in joiner_bands.items():
        ok = [c for c in B if sc.ret_k(c, m, 12) is not None]
        out[m] = sorted(ok, key=lambda c: rank_key(sc.ret_k(c, m, 12), sym[c]))[:J.N_P]
    return out


def ka0_reference(p: J.Panel, joiner_bands: Dict[str, Tuple[List[int], Dict[int, str]]], events: Dict[str, Dict[str, Any]],
                  last_month: str) -> Dict[str, Tuple[float, int]]:
    """KA-0 reference side: the joiner engine's own MOM path (mom_12_1, top_by, name_path, arm_series)."""
    paths: Dict[str, List[J.Path_]] = {}
    for m, (B, sym) in joiner_bands.items():
        mom = {c: J.mom_12_1(p, c, m) for c in B}
        e0 = p.months[m][0]
        paths[m] = [J.name_path(p, c, e0, J.HOLD, events) for c in J.top_by(B, mom, sym, J.N_P)]
    bounds = {m: J.bounds_from(p, m, last_month) for m in paths}
    return J.arm_series(paths, bounds, J.COST, True)


def joiner_bands_real(p: J.Panel, last_month: str) -> Dict[str, Tuple[List[int], Dict[int, str]]]:
    """The joiner report's B per entry month (J.report's own inputs: membership, ANNOUNCED block)."""
    sets, _u, _mo, last_mem = J.load_context(p)
    ann = J.load_announced()
    out = {}
    for m in J.entry_months(p, last_mem):
        if m > last_month:
            continue
        s = p.months[m][0] - 1
        blocked = J.announced_block(ann, J.sym_chain_at(p, s), p.sessions[s], m) if ann is not None else set()
        sg = J.month_signal(p, m, sets.get(m, set()), blocked)
        out[m] = (sg.B, sg.sym)
    return out


def ka0_compare(ref: Dict[str, Tuple[float, int]], new: Dict[str, Tuple[float, int]]) -> Tuple[bool, float, str]:
    if sorted(ref) != sorted(new):
        return False, math.inf, f"month sets differ: ref {len(ref)} new {len(new)}"
    d = max(abs(ref[T][0] - new[T][0]) for T in ref)
    n_ok = all(ref[T][1] == new[T][1] for T in ref)
    return d <= 1e-9 and n_ok, d, f"{len(ref)} months; live-sleeve counts equal {n_ok}"


# ---------------------------------------------------------------- §6 funds
def fund_series(index: Dict[str, float], nav: Optional[Dict[str, float]], cost: float) -> Dict[str, float]:
    """TRI less `cost` a year (accrued by calendar day) until the fund's first NAV date, then the real NAV."""
    dates = sorted(index)
    d0 = date.fromisoformat(dates[0])
    launch = min(nav) if nav else None
    out: Dict[str, float] = {}
    for d in dates:
        if launch is not None and d >= launch:
            break
        out[d] = index[d] / index[dates[0]] * (1 - cost) ** ((date.fromisoformat(d) - d0).days / 365.25)
    if launch is not None and nav:
        i = bisect.bisect_right(dates, launch) - 1
        if i < 0:
            raise ValueError("fund launches before its index starts")
        base = index[dates[i]] / index[dates[0]] * (1 - cost) ** ((date.fromisoformat(dates[i]) - d0).days / 365.25)
        for d in sorted(nav):
            out[d] = base * nav[d] / nav[launch]
    return out


def value_on(series: Dict[str, float], d: str) -> float:
    """Value on the last date on or before d (an index or NAV is aligned to a panel session this way)."""
    ds = sorted(series)
    i = bisect.bisect_right(ds, d) - 1
    if i < 0:
        raise ValueError(f"series starts after {d}")
    return series[ds[i]]


def series_monthly(mk: Market, series: Dict[str, float], w: Window) -> Dict[str, float]:
    out = {}
    for T in months_in(w):
        a = mk.sessions[mk.last(madd(T, -1))]
        b = mk.sessions[mk.last(T)]
        out[T] = value_on(series, b) / value_on(series, a) - 1
    return out


def series_dd(mk: Market, series: Dict[str, float], w: Window) -> float:
    """Max drawdown (%) of a daily series over window w, the start peak = its value at the end of the month before."""
    a = mk.sessions[mk.last(madd(w[0], -1))]
    b = mk.sessions[mk.last(w[1])]
    pts = [(d, v) for d, v in sorted(series.items()) if a < d <= b]
    return float(CMP.drawdown_vs_peak(pts, start=value_on(series, a))["pct"])


# ---------------------------------------------------------------- §8 statistics
def window_mean(x: Dict[str, float], w: Window) -> Optional[float]:
    v = [r for T, r in x.items() if in_w(T, w)]
    return sum(v) / len(v) if v else None


def diff(a: Dict[str, float], b: Dict[str, float], w: Window) -> List[float]:
    ms = [T for T in sorted(a) if T in b and in_w(T, w)]
    return [a[T] - b[T] for T in ms]


def paired_ci(vals: List[float], level: float) -> Tuple[float, float]:
    """§8 moving-block bootstrap of the mean (monthly_orb.mbb_ci), block 6, 5,000 draws, seed 7."""
    if not vals:
        return math.nan, math.nan
    return MO.mbb_ci(vals, block=BLOCK, n=BOOT_DRAWS, seed=SEED, level=level)


def reality_check(diffs: List[List[float]], level: float = LEVEL_REPORT, n: int = BOOT_DRAWS, seed: int = SEED
                  ) -> Tuple[int, float, float]:
    """§4: (index of the best mean, best mean, `level` percentile of the max of the centred bootstrap means).
    The same moving-block draws (monthly_orb.mbb_ci's scheme) for every series: paired."""
    if not diffs or any(len(d) != len(diffs[0]) for d in diffs) or not diffs[0]:
        raise ValueError("reality_check needs equal, non-empty series")
    m = len(diffs[0])
    b = min(BLOCK, m)
    nb = math.ceil(m / b)
    means = [sum(d) / m for d in diffs]
    cen = [[x - mu for x in d] for d, mu in zip(diffs, means)]
    rng = random.Random(seed)
    mx = []
    for _ in range(n):
        idx: List[int] = []
        for _ in range(nb):
            i = rng.randrange(m - b + 1)
            idx += list(range(i, i + b))
        idx = idx[:m]
        mx.append(max(sum(c[i] for i in idx) / m for c in cen))
    mx.sort()
    best = max(range(len(means)), key=lambda k: means[k])
    return best, means[best], mx[min(n - 1, int(level * n))]


def eq_dd(res: BookResult, mk: Market, w: Window, after_tax: bool = False) -> float:
    """§5 / §8 max drawdown (%) of DAILY equity inside window w; the start peak = equity at the end of the month
    before the window (or the account at the run's start).  after_tax: the taxed run's equity net of paid and
    accrued-unpaid tax (owner D12)."""
    arr = res.eq_at if after_tax else res.eq
    if arr is None:
        raise ValueError("after-tax drawdown needs a taxed run")
    t_a = mk.last(madd(w[0], -1))
    t_b = mk.last(w[1])
    lo = max(t_a, res.t0 - 1)
    base = arr[lo - res.t0 + 1]
    pts = [(mk.sessions[t], float(arr[t - res.t0 + 1])) for t in range(lo + 1, t_b + 1)]
    return float(CMP.drawdown_vs_peak(pts, start=float(base))["pct"]) if base > 0 else 100.0


def monthly_after_tax(res: BookResult, mk: Market) -> Dict[str, float]:
    """Month-end returns of the after-tax equity; with a liquidation, the last month ends at the after-tax
    liquidation wealth (so the final tax is inside the series)."""
    if res.eq_at is None:
        raise ValueError("needs a taxed run")
    ms = sorted(res.monthly)
    out: Dict[str, float] = {}
    prev = float(res.eq_at[0])
    for i, T in enumerate(ms):
        v = float(res.eq_at[mk.last(T) - res.t0 + 1])
        if i == len(ms) - 1 and res.final_after_tax is not None:
            v = res.final_after_tax
        out[T] = v / prev - 1 if prev > 0 else 0.0
        prev = v
    return out


def cagr(monthly: Dict[str, float], w: Window) -> float:
    v = [r for T, r in sorted(monthly.items()) if in_w(T, w)]
    g = 1.0
    for r in v:
        g *= 1 + r
    return g ** (12 / len(v)) - 1 if v and g > 0 else -1.0


def calmar(monthly: Dict[str, float], dd_pct: float, w: Window) -> float:
    return cagr(monthly, w) / (dd_pct / 100) if dd_pct > 0 else math.inf


def pctl(sorted_vals: Sequence[float], p: float) -> float:
    return J.pctl(sorted_vals, p)


def mde(vals: List[float], level: float) -> Optional[float]:
    """§8 'smallest monthly excess the test could have detected': the distance from the mean to the CI low (a true
    excess of this size puts the CI low at about 0)."""
    if len(vals) < 2:
        return None
    lo, _hi = paired_ci(vals, level)
    return sum(vals) / len(vals) - lo


# ---------------------------------------------------------------- §8 read rule, flags, gate rule
@dataclass
class ReadStats:
    s1_all: Optional[float]
    s1_lo: float
    s1_oos: Optional[float]
    s2_all: Optional[float]
    s2_lo: float
    s2_oos: Optional[float]
    s3_all: Optional[float]
    s3_p95_all: float
    s3_oos: Optional[float]
    s3_p95_oos: float
    s4_blend_all: float
    s4_match_all: float
    s4_owned_all: float
    s4_blend_oos: float
    s4_match_oos: float
    s4_owned_oos: float
    s5_dd_blend_oos: float
    s5_dd_match_oos: float
    s6_ok: bool
    fair_benchmark: bool = True
    s5_dd_blend_oos_at: float = math.inf                 # owner D12: S5 also on after-tax equity
    s5_dd_match_oos_at: float = 0.0


def _pos(x: Optional[float]) -> bool:
    return x is not None and x == x and x > 0


def read_lines(s: ReadStats) -> Dict[str, bool]:
    return {
        "S1": _pos(s.s1_all) and s.s1_lo > 0 and _pos(s.s1_oos),
        "S2": s.fair_benchmark and _pos(s.s2_all) and s.s2_lo > 0 and _pos(s.s2_oos),
        "S3": (s.s3_all is not None and s.s3_all > s.s3_p95_all and s.s3_oos is not None and s.s3_oos > s.s3_p95_oos),
        "S4": (s.fair_benchmark and s.s4_blend_all > s.s4_match_all and s.s4_blend_all > s.s4_owned_all
               and s.s4_blend_oos > s.s4_match_oos and s.s4_blend_oos > s.s4_owned_oos),
        "S5": (s.s5_dd_blend_oos <= s.s5_dd_match_oos + S5_DD_POINTS
               and s.s5_dd_blend_oos_at <= s.s5_dd_match_oos_at + S5_DD_POINTS),
        "S6": s.s6_ok,
    }


def read_rule(s: ReadStats) -> Tuple[str, Dict[str, bool]]:
    """§8: CANDIDATE iff S1-S6 all pass; else BAND BETA (S1 or S3 fails), INDEX DOES IT CHEAPER (S1, S3 pass and S2
    or S4 fails), else NOT SHOWN naming every failing line.  No fair benchmark caps the verdict at NOT SHOWN."""
    ok = read_lines(s)
    if all(ok.values()):
        return "CANDIDATE for forward paper", ok
    if not (ok["S1"] and ok["S3"]):
        return "BAND BETA: selection not shown", ok
    if not s.fair_benchmark:
        return "NOT SHOWN: no fair benchmark", ok
    if not (ok["S2"] and ok["S4"]):
        return "INDEX DOES IT CHEAPER", ok
    return "NOT SHOWN: " + ", ".join(k for k, v in ok.items() if not v), ok


@dataclass
class FlagStats:
    blend_oos: Optional[float]
    blend_is: Optional[float]
    dd_oos: float
    s2_pass: bool
    s4_pass: bool
    year_log_gain: Dict[str, float]
    lookback_distinguishable: bool
    gates_under_minimum: List[str]
    capacity: Optional[float]
    exits: int
    deferred_exits: int
    fo_within_ci: bool
    underpowered: bool


def flags(f: FlagStats) -> List[str]:
    out = ["backfilled index"]
    if f.blend_oos is None or f.blend_oos < FLAG_MONTHLY:
        out.append("below 2%/month")
    if f.dd_oos > FLAG_DD:
        out.append("DD > 35%")
    if f.s2_pass and not f.s4_pass:
        out.append("tax eats it")
    if f.blend_oos is None or f.blend_is is None or f.blend_oos < DECAY_FRAC * f.blend_is:
        out.append("decay")
    tot = sum(f.year_log_gain.values())
    if tot <= 0 or any(v > ONE_YEAR_SHARE * tot for v in f.year_log_gain.values()):
        out.append("one year" + ("" if tot > 0 else " (total log gain <= 0)"))
    if not f.lookback_distinguishable:
        out.append("lookback: nothing distinguishable")
    if f.gates_under_minimum:
        out.append("regime: too few switches (" + ", ".join(f.gates_under_minimum) + ")")
    if f.capacity is None or f.capacity < CAP_FLAG:
        out.append("capacity < 1 Cr")
    if f.exits and f.deferred_exits / f.exits > LOCKED_EXIT_SHARE:
        out.append("locked exits")
    if f.fo_within_ci:
        out.append("F&O does it too")
    if f.underpowered:
        out.append("underpowered: S2 could not have passed")
    return out


@dataclass
class GateStats:
    dd_oos_ungated: float
    dd_oos_gated: float
    calmar_oos_ungated: float
    calmar_oos_gated: float
    calmar_all_ungated: float
    calmar_all_gated: float
    giveup_monthly: float                                # geometric monthly, after-tax liquidation wealth
    real_cut: float
    placebo_cuts: List[float]
    minimum_ok: bool
    # owner D11 / D12 (2026-11-24): every drawdown line also after tax, g3 also as an arithmetic monthly mean.
    # Defaults FAIL the line, so a caller that forgets one cannot pass it.
    giveup_arith: float = math.inf
    dd_oos_ungated_at: float = 0.0
    dd_oos_gated_at: float = math.inf
    calmar_oos_ungated_at: float = math.inf
    calmar_oos_gated_at: float = -math.inf
    calmar_all_ungated_at: float = math.inf
    calmar_all_gated_at: float = -math.inf
    real_cut_at: float = -math.inf
    placebo_cuts_at: List[float] = field(default_factory=list)


def gate_rule(g: GateStats) -> Tuple[str, Dict[str, bool]]:
    """§5: GATE: CANDIDATE iff g1-g5; CANNOT BE READ if g5 fails; else NOT SHOWN.  g4 with fewer than 200 kept
    placebo schedules cannot be read and counts as failed."""
    kept, kept_at = sorted(g.placebo_cuts), sorted(g.placebo_cuts_at)
    g4_read = len(kept) >= PLACEBO_MIN_KEPT and len(kept_at) >= PLACEBO_MIN_KEPT
    ok = {
        "g1": (g.dd_oos_gated <= (1 - G1_CUT) * g.dd_oos_ungated
               and g.dd_oos_gated_at <= (1 - G1_CUT) * g.dd_oos_ungated_at),
        "g2": (g.calmar_oos_gated > g.calmar_oos_ungated and g.calmar_all_gated > g.calmar_all_ungated
               and g.calmar_oos_gated_at > g.calmar_oos_ungated_at
               and g.calmar_all_gated_at > g.calmar_all_ungated_at),
        "g3": g.giveup_monthly <= G3_GIVEUP and g.giveup_arith <= G3_GIVEUP,
        "g4": (g4_read and g.real_cut > pctl(kept, LEVEL_READ * 100)
               and g.real_cut_at > pctl(kept_at, LEVEL_READ * 100)),
        "g5": g.minimum_ok,
    }
    if not ok["g5"]:
        return "GATE: CANNOT BE READ", ok
    return ("GATE: CANDIDATE" if all(ok.values()) else "GATE: NOT SHOWN"), ok


def capacity(rows: Sequence[Tuple[float, float, Optional[float]]], base_net: Optional[float]) -> Optional[float]:
    """§7: rows (size, worst days, net monthly return), ascending sizes.  The largest size where the worst trade
    takes <= 5 days AND the net return keeps >= 80% of the 10L figure, counted contiguously from the smallest size
    (a size that fails stops the count).  None if the 10L figure is not positive."""
    if base_net is None or not base_net > 0:
        return None
    best = None
    for size, worst, net in rows:
        if worst <= CAP_MAX_DAYS and net is not None and net >= CAP_KEEP * base_net:
            best = size
        else:
            break
    return best


# ---------------------------------------------------------------- context loading
@dataclass
class Ctx:
    mk: Market
    sc: Scorer
    indices: Dict[str, Dict[str, float]]
    vix: Dict[str, float]
    sectors: Dict[str, str]
    fetch_log: Dict[str, Any]
    ca_lines: List[str]


def load_ctx(need_indices: bool = True, lean: bool = False) -> Ctx:
    t0 = time.time()
    p = J.Panel.load()
    res = J.ca_adjust(p)
    ok_ca, _l = J.p3_ca_verdict(p, res)
    if not ok_ca:
        raise SystemExit("P3-CA is NO (CA file incomplete or a known case fails): returns cannot be adjusted")
    _ = p.G
    LOG.info("panel %d x %d loaded, CA applied (%d rows, %d zeroed) in %.0fs", p.C, p.S, len(res.fixes),
             len(res.zeroed), time.time() - t0)
    ms = sorted(f.stem[5:] for f in B150_DIR.glob("b150_*.json"))
    bands = {m: load_band_file(m) for m in ms}
    sets, _u, _months_of, _last = J.load_context(p)
    mk = Market(p, bands, sets, load_events())
    if lean:                                         # report: not read after the Market is built (memory)
        for a in ("dv", "prev", "high", "low"):
            setattr(p, a, None)
    indices = {k: load_index(k) for k in INDEX_FILES} if need_indices else {}
    log = read_json(INDEX_DIR / "fetch_log.json") if need_indices else {}
    LOG.info("context ready in %.0fs; peak RSS %s MB", time.time() - t0, peak_rss_mb())
    return Ctx(mk, Scorer(mk), indices, load_vix(), load_sectors(), log,
               [f"CA rows {len(res.fixes)}, zeroed {len(res.zeroed)}"])


# ---------------------------------------------------------------- signals (P1-P12; counts / names / booleans)
SCHEDULE_ARMS: Tuple[Tuple[str, Dict[str, Any]], ...] = (
    ("BLEND", {}), ("M12", dict(arm="M12")), ("M6", dict(arm="M6")), ("M3", dict(arm="M3")),
    ("HI52", dict(arm="HI52")), ("SMOOTH", dict(arm="SMOOTH")),
    ("BLEND 20/20", dict(sell_rank=20)), ("BLEND 20/75", dict(sell_rank=75)),
    ("BLEND N10", dict(n=10, sell_rank=20)), ("BLEND N30", dict(n=30, sell_rank=60)),
    ("BLEND FO", dict(pool="FO")))


def schedule(ctx: Ctx, spec: Spec) -> BookResult:
    """Layer 2 only: the book under the flat price index (no number in it is a return)."""
    return simulate(ctx.mk, ctx.sc, spec, flat_px(ctx.mk))


def gate_schedules(ctx: Ctx, ms: Sequence[str]) -> Dict[str, Dict[str, bool]]:
    idx = ctx.indices[GATE_INDEX]
    g1, g3 = index_states(ctx.mk, idx, ms)
    return {"G-TREND": g1, "G-BREADTH": gate_breadth(ctx.mk, ms), "G-REBOUND": g3}


def p10_lines(ctx: Ctx, recs: Sequence[PosRecord]) -> Tuple[bool, List[str], Dict[str, Any]]:
    """P10: chained bhavcopy month-end to month-end returns of every held name against the Kite adjusted dailies on
    disk, within 1%, for the full months each name was held.  Prints counts and names only."""
    import gzip
    import survivorship as V
    mk = ctx.mk
    resolve = read_json(NS.RESOLVE_FILE) if NS.RESOLVE_FILE.exists() else {}
    explained = read_json(P10_EXPLAINED_FILE) if P10_EXPLAINED_FILE.exists() else {}
    by_chain: Dict[int, List[Tuple[int, int]]] = defaultdict(list)
    for r in recs:
        x = r.x if r.x >= 0 else mk.last(WHOLE[1])
        by_chain[r.c0].append((r.e, x))
    n = ok = 0
    miss: List[str] = []
    nofile: List[str] = []
    for c, spans in sorted(by_chain.items()):
        e0 = min(e for e, _x in spans)
        sym, isin = mk.sym_at(c, e0), mk.isin_at(c, e0)
        ds = (resolve.get(f"{sym}|{isin}") or {}).get("ds") or sym
        f = None
        hit = NS.raw_file(ds)
        if hit is not None:
            f = hit[0] / f"{FA.fname(ds)}.json.gz"
        else:
            f = V.data_file(sym)
        if f is None or not f.exists():
            nofile.append(sym)
            continue
        with gzip.open(f, "rt") as g:
            daily = json.load(g)["daily"]
        kc: Dict[str, float] = {}
        for b in sorted(daily, key=lambda r_: r_[0]):
            kc[str(b[0])[:7]] = float(b[4])
        todo: Set[str] = set()                           # a stock-month held by several arms is checked once
        for e, x in spans:
            for T in months_in((mk.sessions[e][:7], mk.sessions[x][:7])):
                a = madd(T, -1)
                if a not in mk.p.months or T not in mk.p.months:
                    continue
                if mk.last(a) >= e and mk.last(T) <= x and a in kc and T in kc:
                    todo.add(T)
        for T in sorted(todo):
            a = madd(T, -1)
            ta, tb = mk.last(a), mk.last(T)
            n += 1
            kr = kc[T] / kc[a] - 1
            br = float(mk.G[c, tb] / mk.G[c, ta] - 1)
            if abs(kr - br) <= J.ORACLE_TOL:
                ok += 1
            else:
                miss.append(f"{sym} {T}")
    unexplained = [u for u in miss if u not in explained]
    by_sym: Dict[str, List[str]] = defaultdict(list)
    for u in unexplained:
        sy_, mo = u.rsplit(" ", 1)
        by_sym[sy_].append(mo)
    lines = [f"P10 Kite oracle on held names: {ok} of {n} stock-months within {J.ORACLE_TOL:.0%}; mismatches "
             f"{len(miss)} (explained {len(miss) - len(unexplained)}, UNEXPLAINED {len(unexplained)} in "
             f"{len(by_sym)} names); held names without a Kite file on disk {len(nofile)}",
             "  unexplained (name x months: months): " + ("; ".join(
                 f"{k} x{len(v)}: {' '.join(v)}" for k, v in sorted(by_sym.items(), key=lambda kv: -len(kv[1])))
                 or "-"),
             f"  no Kite file: {' '.join(nofile) or '-'}"]
    return not unexplained, lines, dict(n=n, ok=ok, mismatches=miss, unexplained=unexplained, nofile=nofile)


def band_wide_stops(mk: Market, ms: Sequence[str]) -> List[Tuple[int, Stop]]:
    """F1: every stop inside the window of every chain that ever appears in a band file (from its first band month),
    following a swap into the acquirer and a re-listing after a gap.  C-RAND, the placebo books and every arm can
    hold only these chains (or an acquirer), so this list bounds every -100% the report can book."""
    t_end = mk.last(ms[-1])
    todo: Dict[int, int] = {}
    for m in ms:
        for c in mk.band.get(m, []):
            todo.setdefault(c, mk.first(m))
    out: List[Tuple[int, Stop]] = []
    seen: Set[Tuple[int, int]] = set()
    queue = sorted(todo.items())
    while queue:
        c, t = queue.pop()
        while t <= t_end:
            st_ = mk.next_stop(c, t)
            if st_ is None or st_.t_gap > t_end or (c, st_.t_gap) in seen:
                break
            seen.add((c, st_.t_gap))
            out.append((c, st_))
            if st_.kind == "swap":
                queue.append((st_.into, st_.t_gap))
            rows, _ = mk._row_info(c)
            k = int(np.searchsorted(rows, st_.t_gap))
            if k >= len(rows):
                break
            t = int(rows[k])                             # a re-listing: scan on from its first row back
    return sorted(out, key=lambda x: (x[1].t_gap, x[0]))


def ca_subjects(isin: str, sym: str, raw: Optional[List[Dict[str, Any]]], around: str, days: int = 120) -> List[str]:
    """NSE CA-file subjects of the ISIN or symbol with an ex-date within `days` of `around` (evidence for the owner)."""
    if not raw:
        return []
    import nse_ca as CA
    d0 = date.fromisoformat(around)
    out = []
    for x in raw:
        if x.get("isin") != isin and x.get("symbol") != sym:
            continue
        d = CA.parse_exdate(x["exDate"])
        if d is not None and abs((date.fromisoformat(d) - d0).days) <= days:
            out.append(f"{d} {CA.norm_subject(x['subject'])[:60]}")
    return sorted(set(out))


def signals_lines(ctx: Ctx, with_p10: bool = True) -> Tuple[List[str], Dict[str, Any]]:
    mk, out = ctx.mk, []
    checks: Dict[str, Any] = {}
    ms = months_in(WHOLE)
    # P1
    files = sorted(mk.bands)
    n_names = {m: len(mk.bands[m].names) for m in files}
    mapped = sum(len(mk.band[m]) for m in ms)
    unm = {m: v for m, v in mk.band_unmapped.items() if v}
    bad_reb = [m for m in files if mk.bands[m].rebalance != mk.sessions[mk.sig(m)]]       # F12
    p1 = (len(files) == len(months_in((WHOLE[0], files[-1]))) and all(v == J.N_B for v in n_names.values())
          and not unm and mapped == P1_EXPECT and not bad_reb)
    out.append(f"P1 band files {len(files)} ({files[0]}..{files[-1]}); names per file {sorted(set(n_names.values()))}; "
               f"2020-01..2026-10 mapped {mapped} of {sum(n_names[m] for m in ms)} (expect {P1_EXPECT}); unmapped "
               f"{unm or 0}; band rebalance day != panel signal session: {bad_reb or 0}: {'PASS' if p1 else 'FAIL'}")
    checks["P1"] = p1
    firsts = {mk.first(m): m for m in ms if m in mk.p.months}
    sigs_ = {mk.sig(m): m for m in ms}
    odd = [d for d in mk.sessions if d in MUHURAT_RECALL or date.fromisoformat(d).weekday() >= 5]
    for d in odd:
        t = mk.sessions.index(d)
        use = ([f"first session (rebalance / trade) of {firsts[t]}"] if t in firsts else []) + \
              ([f"signal day of {sigs_[t]}"] if t in sigs_ else [])
        out.append(f"F12 panel session {d} ({'muhurat (recall)' if d in MUHURAT_RECALL else 'weekend'}): "
                   f"{'; '.join(use) or 'not a rebalance or signal day'}")
    out.append("F12 muhurat dates (recall) not in the panel: "
               + (" ".join(d for d in MUHURAT_RECALL if d not in mk.sessions) or "-"))
    # P2
    E = {m: mk.eligible(m) for m in ms}
    mins = min(len(v) for v in E.values())
    p2 = mins == P2_MIN_E
    out.append(f"P2 E (a price at the end of m-13) per month: min {mins} (expect {P2_MIN_E}); yearly medians "
               + ", ".join(f"{y} {st.median([len(E[m]) for m in ms if m[:4] == y]):.0f}" for y in sorted({m[:4] for m in ms}))
               + f": {'PASS' if p2 else 'FAIL'}")
    for m in ms:
        ex = [mk.band_sym[m][c] for c in mk.band[m] if c not in set(E[m])]
        out.append(f"  {m} E {len(E[m]):3d}  excluded {len(ex):2d}: {' '.join(ex)}")
    checks["P2"] = p2
    # P3
    sets = mk.members
    ov = {}
    for m in ms:
        jb = set(J.month_signal(mk.p, m, sets.get(m, set()), set()).universe[:J.N_B])
        ov[m] = len(set(mk.band[m]) & jb)
    p3 = min(ov.values()) == P3_MIN_OVERLAP
    out.append(f"P3 overlap with the joiner band: min {min(ov.values())} (expect {P3_MIN_OVERLAP}); yearly medians "
               + ", ".join(f"{y} {st.median([ov[m] for m in ms if m[:4] == y]):.0f}" for y in sorted({m[:4] for m in ms}))
               + f": {'PASS' if p3 else 'FAIL'}")
    checks["P3"] = p3
    # P6
    p6 = True
    for k, (lab, kind) in INDEX_FILES.items():
        lg = ctx.fetch_log.get(k, {})
        ix = ctx.indices[k]
        ds = sorted(ix)
        cover = ds[0] <= mk.sessions[mk.sig(WHOLE[0])] or kind == "NAV"
        okk = bool(lg) and lg.get("first") == ds[0] and lg.get("last") == ds[-1] and lg.get("rows") == len(ds) and cover
        p6 = p6 and okk
        out.append(f"P6 {k:26s} {kind:5s} {ds[0]}..{ds[-1]} rows {len(ds):4d} launch "
                   f"{LAUNCH_RECALL.get(lab, '-') if kind != 'NAV' else ds[0] + ' (first NAV)'}; source "
                   f"{lg.get('source', 'MISSING')[:70]}: {'ok' if okk else 'FAIL'}")
    out.append(f"P6 verdict: {'PASS' if p6 else 'FAIL'}")
    checks["P6"] = p6
    # P7 gates
    gs = gate_schedules(ctx, ms)
    p7 = True
    for g in GATES:
        off = gs[g]
        until = EXPECT_OFF_UNTIL[g]
        got = tuple(m for m in ms if m <= until and off.get(m))
        undefined = [m for m in ms if m not in off]
        good = got == EXPECT_OFF[g] and not undefined
        p7 = p7 and good
        ok_min, cnt = gate_minimum(off)
        out.append(f"P7 {g:9s} OFF {' '.join(m for m in ms if off.get(m))}; counts {cnt}; minimum "
                   f"{'met' if ok_min else 'NOT met'}; equals the frozen count to {until}: {good}"
                   + (f"; UNDEFINED months {undefined}" if undefined else ""))
    a = [m for m in ms if m in gs["G-TREND"] and m in gs["G-BREADTH"]]
    out.append(f"P7 G-TREND and G-BREADTH agree in {sum(gs['G-TREND'][m] == gs['G-BREADTH'][m] for m in a)} of "
               f"{len(a)} months; verdict {'PASS' if p7 else 'FAIL'}")
    checks["P7"] = p7
    # P4 / P5 / P8 / P9 / P11 from the price-free schedules
    sched: Dict[str, BookResult] = {}
    for name, kw in SCHEDULE_ARMS:
        sched[name] = schedule(ctx, Spec(**kw))
    sched["BLEND G-TREND FLAT"] = schedule(ctx, Spec(gate=gs["G-TREND"]))
    sched["EW-E"] = schedule(ctx, Spec(mode="equal"))
    for name, r in sched.items():
        b = [r.buys.get(m, 0) for m in ms]
        sold = [x for x in r.records if x.how == "sold"]
        lt = sum(x.long_term for x in sold)
        defer = sum(1 for x in sold if x.deferred > 0)
        out.append(f"P4 {name:18s} buys per month: total {sum(b)} mean {sum(b) / len(b):.2f} max {max(b)}; months with 0 "
                   f"buys {sum(1 for x in b if x == 0)}; entry skips (locked 6 sessions) {r.skipped}; idle rebalances "
                   f"(open slots, no cash) {r.idle_buys}")
        out.append(f"P5 {name:18s} exits {len(sold)}; held > 12 months (LTCG) {lt} ({lt / len(sold):.1%}); exits deferred "
                   f"by a lower lock {defer}" if sold else f"P5 {name:18s} no exits")
    out.append("P4 BLEND buys per month: " + " ".join(f"{m[2:]}:{sched['BLEND'].buys.get(m, 0)}" for m in ms))
    checks["P4"] = checks["P5"] = True
    # P8
    for name in ("BLEND", "M12", "M6", "M3", "HI52", "SMOOTH"):
        r = sched[name]
        be = lock = joined = 0
        for x in r.records:
            t1 = x.x if x.x >= 0 else mk.last(WHOLE[1])
            cs = x.chains[-1]
            if np.isin(mk.p.series[cs, x.e:t1 + 1], (J.SERIES["BE"], J.SERIES["BZ"])).any():
                be += 1
            if mk.lock_day[cs, x.e:t1 + 1].any():
                lock += 1
            hold_ms = months_in((mk.sessions[x.e][:7], mk.sessions[t1][:7]))[1:]
            if any(x.c0 in mk.members.get(m, set()) for m in hold_ms):
                joined += 1
        out.append(f"P8 {name:6s} positions {len(r.records)}: traded BE/BZ while held {be}; hit a lock day while held "
                   f"{lock}; joined F&O while held {joined}")
    checks["P8"] = True
    # P9
    stopped: Dict[Tuple[int, str], str] = {}
    for name, r in sched.items():
        for x in r.records:
            if x.how in ("dead", "pending", "close", "last_close", "merged"):
                isin = mk.isin_at(x.chains[-1], x.x)
                stopped[(x.chains[-1], isin)] = x.how
    swaps = sum(1 for r in sched.values() for x in r.records if len(x.chains) > 1)
    unm9 = sorted({f"{mk.sym_at(c, mk.S - 1)}/{isin}" for (c, isin), how in stopped.items()
                   if how in ("dead", "pending") and isin not in mk.events})
    out.append(f"P9 stopped held names (the {len(sched)} schedule books): {len(stopped)} distinct; by kind "
               f"{dict(Counter(stopped.values()))}; swapped into an acquirer {swaps}; UNMAPPED (-100% in the read, need a "
               f"row in {EXTRA_EVENTS_FILE.name}) {len(unm9)}: {' '.join(unm9) or '-'}")
    bw = band_wide_stops(mk, ms)
    raw_ca: Optional[List[Dict[str, Any]]] = None
    try:
        import nse_ca as CA
        raw_ca = CA.load_raw(J.CA_ROOT)
    except (OSError, ValueError) as e:
        LOG.warning("CA file unreadable for the P9 evidence column: %s", e)
    bw_bad = []
    for c, st_ in bw:
        isin = mk.isin_at(c, st_.last)
        if st_.kind in ("dead", "pending") and isin not in mk.events:
            bw_bad.append((c, st_, isin))
    out.append(f"P9 band-wide (F1: every chain ever in a band file, its stops inside {WHOLE[0]}..{WHOLE[1]}): "
               f"{len(bw)} stops; by kind {dict(Counter(x.kind for _c, x in bw))}; dead / pending with no event row "
               f"{len(bw_bad)}")
    for c, st_, isin in bw_bad:
        sym = mk.sym_at(c, st_.last)
        subj = ca_subjects(isin, sym, raw_ca, mk.sessions[st_.last])
        out.append(f"  P9-F1 {sym}/{isin} last row {mk.sessions[st_.last]} gap from {mk.sessions[st_.t_gap]} {st_.kind}; "
                   f"NSE CA within 120 days: {' | '.join(subj) or '-'}")
    p9 = not unm9 and not bw_bad
    out.append(f"P9 verdict (schedule books AND band-wide): {'PASS' if p9 else 'FAIL'}")
    checks["P9"] = p9
    checks["P9_band_wide"] = [f"{mk.sym_at(c, x.last)}/{i} {mk.sessions[x.last]} {x.kind}" for c, x, i in bw_bad]
    # P10
    if with_p10:
        recs = [x for r in sched.values() for x in r.records]
        p10, l10, info = p10_lines(ctx, recs)
        out += l10
        checks["P10"] = p10
        checks["P10_info"] = dict(n=info["n"], ok=info["ok"], unexplained=info["unexplained"])
    # P11
    sec = ctx.sectors
    unknown_all = 0
    for m in ms:
        hold = sched["BLEND"].held.get(m, [])
        names = [mk.sym_at(c, mk.sig(m)) for c in hold]
        cnt = Counter(sec.get(s, "UNKNOWN") for s in names)
        unknown_all += cnt.get("UNKNOWN", 0)
        out.append(f"P11 {m} " + "; ".join(f"{k} {v}" for k, v in cnt.most_common()))
    out.append(f"P11 report only: name-months with no sector on disk {unknown_all}")
    for item in RECALL_ITEMS:
        out.append(f"P12 UNCONFIRMED (recall; owner / CA): {item}")
    return out, checks


def cmd_signals() -> None:
    ctx = load_ctx()
    t0 = time.time()
    lines, checks = signals_lines(ctx)
    for x in lines:
        print(x, flush=True)
    checks.update(panel_last=ctx.mk.sessions[-1], written=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  code=code_hash(), stamp=input_stamp())
    write_json_atomic(CHECKS_FILE, checks)
    print(f"signals: {time.time() - t0:.0f}s after load; peak RSS {peak_rss_mb()} MB; checks saved to {CHECKS_FILE}")


def code_hash() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]


def _sha_files(files: Iterable[Path]) -> str:
    h = hashlib.sha256()
    n = 0
    for p in sorted(files):
        h.update(p.name.encode())
        h.update(p.read_bytes())
        n += 1
    return f"{h.hexdigest()[:16]}:{n}"


def _stat(p: Path) -> str:
    return f"{p.stat().st_size}:{p.stat().st_mtime_ns}" if p.exists() else "absent"


def input_stamp() -> Dict[str, str]:
    """F6 freshness stamp: hashes of every study module, the events / P10 / CA / membership / sector / resolve
    files, the band files and the index files; size + mtime of the panel and the CA folder listing.  `signals`
    and `ka0` save it; `report` prints STALE (S6 fails; KA-0 refuses) on any difference."""
    out: Dict[str, str] = {f"code:{p.name}": _sha_files([p]) for p in _STUDY_MODULES}
    out["code:nonfo_momentum.py"] = _sha_files([Path(__file__).resolve()])
    for p in (J.EVENTS_FILE, EXTRA_EVENTS_FILE, P10_EXPLAINED_FILE, J.CA_ZERO_FILE, J.CA_OVERRIDE_FILE,
              J.ANNOUNCED_FILE, SV.MEM_FILE, SV.MAP_FILE, SECTOR_FILE, INDUSTRY_CSV, NS.RESOLVE_FILE, VIX_FILE):
        out[f"file:{p.name}"] = _sha_files([p]) if p.exists() else "absent"
    out["bands"] = _sha_files(B150_DIR.glob("b150_*.json")) if B150_DIR.exists() else "absent"
    out["indices"] = _sha_files(INDEX_DIR.glob("*.json")) if INDEX_DIR.exists() else "absent"
    out["panel"] = _stat(J.PANEL_FILE)
    ca = Path(J.CA_ROOT)
    out["ca_root"] = (hashlib.sha256("|".join(f"{p.name}:{_stat(p)}" for p in sorted(ca.glob("*"))).encode())
                      .hexdigest()[:16] if ca.exists() else "absent")
    return out


def stale_keys(saved: Optional[Dict[str, str]], now: Optional[Dict[str, str]] = None) -> List[str]:
    """Keys whose stamp differs (or is missing on either side); ['no stamp'] if nothing was saved."""
    if not saved:
        return ["no stamp"]
    now = input_stamp() if now is None else now
    return sorted(k for k in set(saved) | set(now) if saved.get(k) != now.get(k))


# ---------------------------------------------------------------- ka0 (real data)
KA0_PUBLISHED = {"whole": 0.026, "IS": 0.032, "OOS": 0.018}   # results/joiner_positional_report.txt, MOM


def cmd_ka0() -> None:
    p = J.Panel.load()
    J.ca_adjust(p)
    events = J.load_events()                         # the joiner's own events file only (the published series)
    jb = joiner_bands_real(p, OOS_W[1])
    mk = Market(p, {}, {}, events)
    sc = Scorer(mk)
    ref = ka0_reference(p, jb, events, OOS_W[1])
    means = {k: sum(r for T, (r, _n) in ref.items() if in_w(T, w)) / sum(1 for T in ref if in_w(T, w))
             for k, w in (("whole", WHOLE), ("IS", IS_W), ("OOS", OOS_W))}
    pub_ok = all(round(means[k], 3) == KA0_PUBLISHED[k] for k in KA0_PUBLISHED)
    print(f"KA-0 reference (joiner engine MOM) rounds to the published 2.6% / 3.2% / 1.8%: {pub_ok}")
    if not pub_ok:
        write_json_atomic(KA0_FILE, dict(ok=False, why="reference does not reproduce the published means"))
        raise SystemExit("KA-0 STOP: the joiner engine does not reproduce its own published series")
    new = sleeve_series(mk, ka0_picks(mk, sc, jb), J.HOLD, J.COST, True, OOS_W[1], mk.G)
    ok, d, why = ka0_compare(ref, new)
    picks_same = all(set(ka0_picks(mk, sc, {m: jb[m]})[m]) ==
                     set(J.top_by(jb[m][0], {c: J.mom_12_1(p, c, m) for c in jb[m][0]}, jb[m][1], J.N_P)) for m in jb)
    print(f"KA-0 new module (Scorer.ret_k + position_net + sleeve_series) vs the joiner MOM series: "
          f"{'MATCH' if ok else 'MISMATCH'}; max monthly abs difference {d:.3e}; {why}; picks identical {picks_same}")
    write_json_atomic(KA0_FILE, dict(ok=bool(ok and picks_same), max_abs_diff=d, months=len(ref), code=code_hash(),
                                     stamp=input_stamp(),
                                     written=datetime.now(timezone.utc).isoformat(timespec="seconds")))
    print(f"peak RSS {peak_rss_mb()} MB")


# ---------------------------------------------------------------- report (RETURNS -- after the review only)
def year_log_gain(monthly: Dict[str, float], w: Window) -> Dict[str, float]:
    out: Dict[str, float] = defaultdict(float)
    for T, r in monthly.items():
        if in_w(T, w):
            out[T[:4]] += math.log1p(r) if r > -1 else -math.inf
    return dict(out)


def report_lines(ctx: Ctx, checks: Dict[str, Any], ka0: Dict[str, Any], crand_draws: int = CRAND_DRAWS,
                 placebo_shuffles: int = PLACEBO_SHUFFLES, sizes: Sequence[float] = SCALE_SIZES) -> List[str]:
    """The frozen report (§5-§8).  Every read line, flag, report-only line; an undefined line is printed as such."""
    mk, sc = ctx.mk, ctx.sc
    G = mk.G
    out: List[str] = []
    ms = months_in(WHOLE)
    t_start = time.time()

    def run(**kw: Any) -> BookResult:
        return simulate(mk, sc, Spec(**kw), G)

    s6 = all(bool(checks.get(k)) for k in ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9", "P10"))
    stale = stale_keys(checks.get("stamp"))
    if checks.get("panel_last") != mk.sessions[-1]:
        stale.append("panel_last")
    if stale:
        out.append(f"S6: checks file STALE (changed since `signals`: {', '.join(stale)}): re-run `signals`; S6 fails")
        s6 = False
    out.append(f"data: S6 checks file {CHECKS_FILE.name}: " + ", ".join(f"{k} {checks.get(k)}" for k in
                                                                     ("P1", "P2", "P3", "P6", "P7", "P9", "P10")))
    out.append("KA-0: " + ", ".join(f"{k} {v}" for k, v in ka0.items() if k != "stamp"))
    gs = gate_schedules(ctx, ms)
    blend = run()
    LOG.info("BLEND done %.0fs", time.time() - t_start)
    ewe = run(mode="equal")
    match_s = fund_series(ctx.indices[MATCH_TRI], ctx.indices[MATCH_NAV], FUND_COST)
    owned_s = fund_series(ctx.indices[OWNED_TRI], ctx.indices[OWNED_NAV], FUND_COST)
    match_m = series_monthly(mk, match_s, WHOLE)
    owned_m = series_monthly(mk, owned_s, WHOLE)
    d1, d2 = diff(blend.monthly, ewe.monthly, WHOLE), diff(blend.monthly, match_m, WHOLE)
    mde1, mde2 = mde(d1, LEVEL_READ), mde(d2, LEVEL_READ)
    under = mde2 is None or mde2 > UNDERPOWER_S2
    out.append(f"POWER  sd(BLEND - EW-E) {fmt_p(st.stdev(d1))}  smallest detectable excess {fmt_p(mde1)}  |  "
               f"sd(BLEND - IDX-MATCH) {fmt_p(st.stdev(d2))}  smallest detectable excess {fmt_p(mde2)}"
               + ("  -> underpowered: S2 could not have passed" if under else ""))
    # S1 / S2
    lo1, hi1 = paired_ci(d1, LEVEL_READ)
    lo2, hi2 = paired_ci(d2, LEVEL_READ)
    # S3
    crand_k = blend.buys
    cr_all: List[float] = []
    cr_oos: List[float] = []
    cr_undef = 0
    for i in range(crand_draws):
        r = run(mode="crand", crand_k=crand_k, seed=SEED + i)
        wa, wo = window_mean(r.monthly, WHOLE), window_mean(r.monthly, OOS_W)
        if wa is None or wo is None:                     # F9: an undefined draw is dropped and counted, never 0
            cr_undef += 1
            continue
        cr_all.append(wa)
        cr_oos.append(wo)
    if not cr_all:
        raise ValueError("C-RAND: no draw has a defined mean")
    cr_all.sort()
    cr_oos.sort()
    LOG.info("C-RAND done %.0fs", time.time() - t_start)
    p95a, p95o = pctl(cr_all, CRAND_PCT), pctl(cr_oos, CRAND_PCT)
    # S4 after tax
    bt_all = run(taxed=True, liquidate=True)
    bt_oos = run(taxed=True, liquidate=True, start=OOS_W[0])

    def cagr_w(wealth: Optional[float], w: Window) -> float:
        n = len(months_in(w))
        return (wealth / ACCOUNT) ** (12 / n) - 1 if wealth and wealth > 0 else -1.0

    def fund_cagr(s: Dict[str, float], w: Window, rates: Rates = rates_today) -> float:
        a, b = mk.sessions[mk.last(madd(w[0], -1))], mk.sessions[mk.last(w[1])]
        return cagr_w(fund_after_tax(value_on(s, a), value_on(s, b), a, b, rates), w)

    s4 = dict(blend_all=cagr_w(bt_all.final_after_tax, WHOLE), blend_oos=cagr_w(bt_oos.final_after_tax, OOS_W),
              match_all=fund_cagr(match_s, WHOLE), match_oos=fund_cagr(match_s, OOS_W),
              owned_all=fund_cagr(owned_s, WHOLE), owned_oos=fund_cagr(owned_s, OOS_W))
    dd_b, dd_m = eq_dd(blend, mk, OOS_W), series_dd(mk, match_s, OOS_W)
    # owner D12: after tax = equity net of tax paid and accrued-unpaid; the fund realises nothing before the
    # sale, so its after-tax daily series equals its NAV series (its tax falls at the end: in S4)
    dd_b_at, dd_m_at = eq_dd(bt_all, mk, OOS_W, after_tax=True), dd_m
    d1o, d2o = diff(blend.monthly, ewe.monthly, OOS_W), diff(blend.monthly, match_m, OOS_W)
    rs = ReadStats(sum(d1) / len(d1), lo1, sum(d1o) / len(d1o) if d1o else None,
                   sum(d2) / len(d2), lo2, sum(d2o) / len(d2o) if d2o else None,
                   window_mean(blend.monthly, WHOLE), p95a, window_mean(blend.monthly, OOS_W), p95o,
                   s4["blend_all"], s4["match_all"], s4["owned_all"], s4["blend_oos"], s4["match_oos"],
                   s4["owned_oos"], dd_b, dd_m, s6, True, dd_b_at, dd_m_at)
    verdict, ok = read_rule(rs)
    out.append("\nmean monthly return, net 0.40%/side, dead = -100%, price return (whole / IS / OOS); OOS max DD daily")
    for name, x, dd in (("BLEND", blend.monthly, dd_b), ("EW-E", ewe.monthly, eq_dd(ewe, mk, OOS_W)),
                        ("IDX-MATCH", match_m, dd_m), ("IDX-OWNED", owned_m, series_dd(mk, owned_s, OOS_W))):
        out.append(f"  {name:10s} " + " / ".join(fmt_p(window_mean(x, w)) for w in (WHOLE, IS_W, OOS_W))
                   + f"   DD {dd:5.1f}%")
    out.append(f"  C-RAND p95 ({len(cr_all)} of {crand_draws} draws defined, undefined {cr_undef}; {N_HOLD} names, "
               f"BLEND's swaps per month) whole {fmt_p(p95a)} OOS {fmt_p(p95o)}")
    out.append(f"S1 BLEND - EW-E whole {fmt_p(rs.s1_all)} [{LEVEL_READ:.2%} CI {fmt_p(lo1)}, {fmt_p(hi1)}] OOS "
               f"{fmt_p(rs.s1_oos)}: {'pass' if ok['S1'] else 'FAIL'}")
    out.append(f"S2 BLEND - IDX-MATCH whole {fmt_p(rs.s2_all)} [{LEVEL_READ:.2%} CI {fmt_p(lo2)}, {fmt_p(hi2)}] OOS "
               f"{fmt_p(rs.s2_oos)}: {'pass' if ok['S2'] else 'FAIL'}")
    out.append(f"S3 BLEND whole {fmt_p(rs.s3_all)} vs C-RAND p95 {fmt_p(p95a)}; OOS {fmt_p(rs.s3_oos)} vs "
               f"{fmt_p(p95o)}: {'pass' if ok['S3'] else 'FAIL'}")
    out.append(f"S4 after-tax CAGR at 10L whole: BLEND {fmt_p(s4['blend_all'])} IDX-MATCH {fmt_p(s4['match_all'])} "
               f"IDX-OWNED {fmt_p(s4['owned_all'])}; OOS alone: {fmt_p(s4['blend_oos'])} / {fmt_p(s4['match_oos'])} / "
               f"{fmt_p(s4['owned_oos'])}: {'pass' if ok['S4'] else 'FAIL'}")
    out.append(f"S5 OOS max DD daily BLEND {dd_b:.1f}% vs IDX-MATCH {dd_m:.1f}% pre-tax; after tax {dd_b_at:.1f}% vs "
               f"{dd_m_at:.1f}% (+{S5_DD_POINTS:g} points allowed, both must hold): {'pass' if ok['S5'] else 'FAIL'}")
    out.append(f"S6 data checks P1-P10 and dead names classified: {'pass' if ok['S6'] else 'FAIL'}")
    out.append(f"\nVERDICT: {verdict}")
    # lookback table + reality check
    arms_r = {a: run(arm=a) for a in REPORT_ARMS}
    diffs = [diff(arms_r[a].monthly, blend.monthly, WHOLE) for a in REPORT_ARMS]
    out.append("\nlookback table (descriptive; picks nothing): arm - BLEND whole, 95% CI")
    for a, dv in zip(REPORT_ARMS, diffs):
        lo, hi = paired_ci(dv, LEVEL_REPORT)
        out.append(f"  {a:6s} {fmt_p(sum(dv) / len(dv))} [{fmt_p(lo)}, {fmt_p(hi)}]"
                   f"{'  indistinguishable' if lo <= 0 <= hi else ''}   whole/IS/OOS "
                   + " / ".join(fmt_p(window_mean(arms_r[a].monthly, w)) for w in (WHOLE, IS_W, OOS_W)))
    bi, bm, rc95 = reality_check(diffs)
    distinguishable = bm > rc95
    out.append(f"  reality check: best {REPORT_ARMS[bi]} - BLEND {fmt_p(bm)} vs 95th pct of the centred max {fmt_p(rc95)}"
               + ("" if distinguishable else "  -> lookback: nothing distinguishable"))
    # gate
    gated = run(gate=gs[READ_GATE])
    gt_all = run(gate=gs[READ_GATE], taxed=True, liquidate=True)
    n_all = len(ms)
    w_u, w_g = bt_all.final_after_tax, gt_all.final_after_tax
    if w_u is None or w_g is None or w_u <= 0 or w_g <= 0:
        raise ValueError("g3: a taxed liquidation is undefined")
    giveup = (w_u / ACCOUNT) ** (1 / n_all) - (w_g / ACCOUNT) ** (1 / n_all)
    mat_u, mat_g = monthly_after_tax(bt_all, mk), monthly_after_tax(gt_all, mk)
    giveup_ar = sum(mat_u[m] for m in ms) / n_all - sum(mat_g[m] for m in ms) / n_all
    dd_u, dd_g = dd_b, eq_dd(gated, mk, OOS_W)
    dd_u_at, dd_g_at = dd_b_at, eq_dd(gt_all, mk, OOS_W, after_tax=True)
    real_cut = 1 - dd_g / dd_u if dd_u > 0 else 0.0
    real_cut_at = 1 - dd_g_at / dd_u_at if dd_u_at > 0 else 0.0
    fakes = placebo_schedules([gs[READ_GATE][m] for m in ms], SEED, placebo_shuffles)
    cuts: List[float] = []
    cuts_at: List[float] = []
    for fk in fakes:
        sched = dict(zip(ms, fk))
        if sum(sched[m] for m in ms if in_w(m, OOS_W)) < PLACEBO_MIN_OOS_OFF:
            continue
        r = run(gate=sched)
        cuts.append(1 - eq_dd(r, mk, OOS_W) / dd_u if dd_u > 0 else 0.0)
        rt = run(gate=sched, taxed=True, liquidate=True)
        cuts_at.append(1 - eq_dd(rt, mk, OOS_W, after_tax=True) / dd_u_at if dd_u_at > 0 else 0.0)
    LOG.info("placebo done %.0fs", time.time() - t_start)
    min_ok, _cnt = gate_minimum(gs[READ_GATE])
    gst = GateStats(dd_u, dd_g, calmar(blend.monthly, dd_u, OOS_W), calmar(gated.monthly, dd_g, OOS_W),
                    calmar(blend.monthly, eq_dd(blend, mk, WHOLE), WHOLE),
                    calmar(gated.monthly, eq_dd(gated, mk, WHOLE), WHOLE), giveup, real_cut, cuts, min_ok,
                    giveup_ar, dd_u_at, dd_g_at,
                    calmar(mat_u, dd_u_at, OOS_W), calmar(mat_g, dd_g_at, OOS_W),
                    calmar(mat_u, eq_dd(bt_all, mk, WHOLE, after_tax=True), WHOLE),
                    calmar(mat_g, eq_dd(gt_all, mk, WHOLE, after_tax=True), WHOLE), real_cut_at, cuts_at)
    gv, gok = gate_rule(gst)
    p_pre = pctl(sorted(cuts), LEVEL_READ * 100) if cuts else math.nan
    p_at = pctl(sorted(cuts_at), LEVEL_READ * 100) if cuts_at else math.nan
    out.append(f"\nGATE {READ_GATE} FLAT (BLEND with vs without; every line pre-tax AND after tax, both must hold)")
    out.append(f"  g1 OOS DD pre-tax {dd_g:.1f}% vs {dd_u:.1f}%; after tax {dd_g_at:.1f}% vs {dd_u_at:.1f}%: {gok['g1']}")
    out.append(f"  g2 Calmar pre-tax OOS {gst.calmar_oos_gated:.2f} vs {gst.calmar_oos_ungated:.2f}, whole "
               f"{gst.calmar_all_gated:.2f} vs {gst.calmar_all_ungated:.2f}; after tax OOS {gst.calmar_oos_gated_at:.2f} "
               f"vs {gst.calmar_oos_ungated_at:.2f}, whole {gst.calmar_all_gated_at:.2f} vs "
               f"{gst.calmar_all_ungated_at:.2f}: {gok['g2']}")
    out.append(f"  g3 given up after costs and tax: geometric {fmt_p(giveup)} / arithmetic {fmt_p(giveup_ar)} a month "
               f"(both <= {G3_GIVEUP:.2%}): {gok['g3']}")
    out.append(f"  g4 placebo kept {len(cuts)} of {len(fakes)}: real cut pre-tax {real_cut:.1%} vs {LEVEL_READ:.2%} pct "
               f"{p_pre:.1%}; after tax {real_cut_at:.1%} vs {p_at:.1%}: {gok['g4']}"
               f"{'' if len(cuts) >= PLACEBO_MIN_KEPT else ' (CANNOT BE READ, < 200 kept)'}")
    out.append(f"  g5 minimum: {gok['g5']}")
    out.append(f"{gv}")
    for g in ("G-BREADTH", "G-REBOUND"):
        r = run(gate=gs[g])
        note = "  (one event, 2020: nothing concluded)" if g == "G-REBOUND" else ""
        out.append(f"  {g} FLAT (report only): whole/IS/OOS " + " / ".join(fmt_p(window_mean(r.monthly, w)) for w in
                                                                         (WHOLE, IS_W, OOS_W))
                   + f"  OOS DD {eq_dd(r, mk, OOS_W):.1f}%{note}")
    nonew = run(gate=gs[READ_GATE], gate_mode="nonew")
    reentries = {m for i, m in enumerate(ms) if i > 0 and gs[READ_GATE][ms[i - 1]] and not gs[READ_GATE][m]}
    reeq = run(reequal=reentries)
    liquid = run(gate=gs[READ_GATE], cash_monthly=LIQUID_MONTHLY)
    for name, r in (("G-TREND NO-NEW", nonew), ("ungated re-equalised on re-entries", reeq),
                    ("G-TREND FLAT cash at the liquid fund", liquid)):
        out.append(f"  {name} (report only): whole/IS/OOS " + " / ".join(fmt_p(window_mean(r.monthly, w)) for w in
                                                                        (WHOLE, IS_W, OOS_W))
                   + f"  OOS DD {eq_dd(r, mk, OOS_W):.1f}%")
    # construction, costs, second table, FO, EWB
    out.append("\nreport only: construction, costs, dead names, universes (whole / IS / OOS)")
    fo_within = False
    for name, kw in (("N10", dict(n=10, sell_rank=20)), ("N30", dict(n=30, sell_rank=60)),
                     ("buffer 20/20", dict(sell_rank=20)), ("buffer 20/75", dict(sell_rank=75)),
                     ("cost 0.15%", dict(cost=COST_LEVELS[0])), ("cost 1.0%", dict(cost=COST_LEVELS[1])),
                     ("dead at last price", dict(dead_zero=False)), ("FO universe", dict(pool="FO")),
                     ("EWB (whole band)", dict(mode="equal", pool="BAND"))):
        r = run(**kw)
        out.append(f"  {name:20s} " + " / ".join(fmt_p(window_mean(r.monthly, w)) for w in (WHOLE, IS_W, OOS_W)))
        if name == "FO universe":
            dfo = diff(r.monthly, blend.monthly, WHOLE)
            lo, hi = paired_ci(dfo, LEVEL_REPORT)
            fo_within = lo <= 0 <= hi
            out.append(f"    FO - BLEND {fmt_p(sum(dfo) / len(dfo))} [95% CI {fmt_p(lo)}, {fmt_p(hi)}]")
    out.append("\nreport only: indices (backfilled before each launch date)")
    for k in PRINTED_INDICES:
        lab, kind = INDEX_FILES[k]
        x = series_monthly(mk, ctx.indices[k], WHOLE)
        out.append(f"  {lab[:38]:38s} {kind:5s} launch {LAUNCH_RECALL.get(lab, '-'):18s} "
                   + " / ".join(fmt_p(window_mean(x, w)) for w in (WHOLE, IS_W, OOS_W)))
    # tax table
    bt_hist = run(taxed=True, liquidate=True, rates=rates_historical)
    out.append(f"\ntax: BLEND after-tax wealth from 10L, today's rates {bt_all.final_after_tax:,.0f}; historical rates "
               f"{bt_hist.final_after_tax:,.0f}; IDX-MATCH after tax {fund_cagr(match_s, WHOLE):.2%}/yr today, "
               f"{fund_cagr(match_s, WHOLE, rates_historical):.2%}/yr historical")
    for row in bt_all.tax_rows:
        out.append(f"  {CMP.fy_label(row.fy)} ST net {row.st_net:>12,.0f} LT net {row.lt_net:>12,.0f} taxable ST "
                   f"{row.st_taxable:>12,.0f} LT {row.lt_taxable:>12,.0f} tax {row.tax:>10,.0f} carry {row.carry_after:>10,.0f}")
    # scaling
    out.append("\nscaling (impact model; per name = account / 20): size, trades > 5% / > 20% of T60, days at 2% of T60 "
               "(median, worst), mean modelled cost per side, BLEND net whole, locked exits")
    rows: List[Tuple[float, float, Optional[float]]] = []
    base_net = None
    for size in sizes:
        rsz = run(cost="impact", account=size)
        tr = rsz.trades
        ratios: List[float] = []
        days: List[float] = []
        for trd in tr:
            s_ = mk.sig(mk.sessions[trd.t][:7])
            t6 = mk.t60(trd.c, s_)
            ratios.append(trd.amount / t6 if t6 > 0 else math.inf)
            days.append(trd.amount / (TRADE_DAY_SHARE * t6) if t6 > 0 else math.inf)
        net = window_mean(rsz.monthly, WHOLE)
        if base_net is None:
            base_net = net
        worst = max(days) if days else 0.0
        rows.append((size, worst, net))
        locked = sum(1 for trd in tr if trd.side < 0 and trd.deferred > 0)
        out.append(f"  {size / 1e5:8.0f}L  {sum(v > TV_SHARES[0] for v in ratios) / max(1, len(ratios)):6.1%} "
                   f"{sum(v > TV_SHARES[1] for v in ratios) / max(1, len(ratios)):6.1%}  "
                   f"{st.median(days) if days else 0:6.2f} {worst:8.2f}  "
                   f"{st.mean([trd.cost for trd in tr]) if tr else 0:6.2%}  "
                   f"{fmt_p(net)}  {locked}")
    cap = capacity(rows, base_net)
    out.append(f"  capacity: {'undefined (10L net <= 0)' if cap is None else f'{cap / 1e5:,.0f}L'}")
    # regime map
    out.append("\nregime map (descriptive): BLEND mean monthly return (months)")
    groups: Dict[str, List[float]] = defaultdict(list)
    for i, m in enumerate(ms):
        rv = blend.monthly[m]
        groups[f"G-TREND {'OFF' if gs[READ_GATE][m] else 'ON'}"].append(rv)
        b = breadth(mk, m)
        groups["breadth " + ("n/a" if b is None else "< 40%" if b < BREADTH_BUCKETS[0] else "40-70%"
                             if b <= BREADTH_BUCKETS[1] else "> 70%")].append(rv)
        sd = mk.sessions[mk.sig(m)]
        vx = None
        if ctx.vix:
            try:
                vx = value_on(ctx.vix, sd)
            except ValueError:
                vx = None
        groups["VIX " + ("n/a" if vx is None else "< 15" if vx < VIX_BUCKETS[0] else "15-20" if vx <= VIX_BUCKETS[1]
                         else "> 20")].append(rv)
        sw = [j for j in range(max(1, i - POST_SWITCH_MONTHS + 1), i + 1)
              if gs[READ_GATE][ms[j - 1]] and not gs[READ_GATE][ms[j]]]
        if sw:
            groups["first 2 months after OFF -> ON"].append(rv)
    for k in sorted(groups):
        v = groups[k]
        out.append(f"  {k:32s} {fmt_p(sum(v) / len(v))} ({len(v)})")
    # concentration
    out.append("\nBLEND per month after the rebalance: largest weight / effective N (report only)")
    out.append("  " + " ".join(f"{m[2:]}:{w[0]:.0%}/{w[1]:.0f}" for m, w in sorted(blend.weights.items())))
    # flags
    sold = [x for x in blend.records if x.how == "sold"]
    ok_g = {g: gate_minimum(gs[g])[0] for g in GATES}
    fl = flags(FlagStats(window_mean(blend.monthly, OOS_W), window_mean(blend.monthly, IS_W), dd_b, ok["S2"],
                         ok["S4"], year_log_gain(blend.monthly, WHOLE), distinguishable,
                         [g for g, v in ok_g.items() if not v], cap, len(sold), sum(1 for x in sold if x.deferred > 0),
                         fo_within, under))
    out.append("\nFLAGS: " + " | ".join(fl))
    out.append("\ncounts (BLEND, verdict run): entry skips (locked 6 sessions) "
               f"{blend.skipped}; idle rebalances (open slots, no cash) {blend.idle_buys}; exits {len(sold)}, deferred "
               f"by a lower lock {sum(1 for x in sold if x.deferred > 0)}; positions swapped into an acquirer "
               f"{sum(1 for x in blend.records if len(x.chains) > 1)}; merged / last close "
               f"{sum(1 for x in blend.records if x.how == 'close')}")
    dead_names = [f"{mk.sym_at(x.chains[-1], x.x)} {mk.sessions[x.x]}" for x in blend.records
                  if x.how in ("dead", "pending")]
    out.append(f"  -100% names (dead / pending): {len(dead_names)}: {'; '.join(dead_names) or '-'}")
    out.append(f"VERDICT: {verdict}   |   {gv}")
    out.append(f"report runtime {time.time() - t_start:.0f}s; peak RSS {peak_rss_mb()} MB")
    return out


def cmd_report() -> None:
    checks = read_json(CHECKS_FILE) if CHECKS_FILE.exists() else {}
    ka0 = read_json(KA0_FILE) if KA0_FILE.exists() else {}
    if not ka0.get("ok"):
        raise SystemExit(f"KA-0 has not passed ({KA0_FILE}): run `ka0` first (design §11: known answers first)")
    ka0_stale = ([] if ka0.get("code") == code_hash() else ["code"]) + stale_keys(ka0.get("stamp"))
    if ka0_stale:                                    # F5 / F6: a KA-0 of other code or other inputs proves nothing
        raise SystemExit(f"KA-0 STALE (changed since `ka0`: {', '.join(ka0_stale)}): re-run `ka0`, then `signals`")
    ctx = load_ctx(lean=True)
    for x in report_lines(ctx, checks, ka0):
        print(x, flush=True)


# ---------------------------------------------------------------- forward (§9)
def forward_payload(ctx: Ctx, m: str) -> Dict[str, Any]:
    """The month-m paper file: BLEND ranked list and planned 20, the five report-only lists, the three gate states.
    Price-free (layer 2).  Needs the band file of m and the panel / gate index through the band's rebalance day."""
    mk, sc = ctx.mk, ctx.sc
    if m not in mk.bands:
        raise SystemExit(f"no band file for {m}")
    s = mk.sig(m)
    reb = mk.bands[m].rebalance
    if reb != mk.sessions[s]:
        raise SystemExit(f"{m}: band rebalance day {reb} != the panel's last session before {m} ({mk.sessions[s]})")
    gidx = ctx.indices[GATE_INDEX]
    if reb not in gidx:
        raise SystemExit(f"{m}: the gate index has no close on {reb}")
    prev = madd(m, -1)
    states = {g: v.get(m) for g, v in gate_schedules(ctx, [m]).items()}
    lists: Dict[str, Any] = {}
    for a in ARMS:
        r = schedule(ctx, Spec(arm=a, end=prev, decide=m))
        assert r.decision is not None
        lists[a] = dict(ranked=[mk.sym_at(c, s) for c in sc.order(a, m, "E")],
                        hold=[mk.sym_at(c, s) for c in r.decision["hold"]],
                        sell=[mk.sym_at(c, s) for c in r.decision["sell"]],
                        buy=[mk.sym_at(c, s) for c in r.decision["buy"]])
    return dict(month=m, signal_day=reb, written_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                code=code_hash(), primary=PRIMARY, arms=lists,
                gates={g: (None if v is None else ("OFF" if v else "ON")) for g, v in states.items()})


def forward_file(payload: Dict[str, Any], out_dir: Path) -> Path:
    """Write once, never edit: exclusive create (an existing file is never overwritten)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    f = out_dir / f"forward_{payload['month']}.json"
    with open(f, "x", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    return f


def cmd_forward(m: str) -> None:
    if len(m) != 7 or m < FORWARD_FIRST:
        raise SystemExit(f"forward month must be YYYY-MM >= {FORWARD_FIRST}")
    ctx = load_ctx()
    f = forward_file(forward_payload(ctx, m), FORWARD_DIR)
    print(f"forward paper file written: {f}")


# ---------------------------------------------------------------- argv
USAGE = "usage: nonfo_momentum.py signals | ka0 | report --reviewed | forward YYYY-MM"


def main(argv: List[str]) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    if not argv:
        raise SystemExit(USAGE)
    cmd, rest = argv[0], argv[1:]
    if cmd == "signals" and not rest:
        cmd_signals()
    elif cmd == "ka0" and not rest:
        cmd_ka0()
    elif cmd == "report":
        if rest != ["--reviewed"]:
            raise SystemExit("report computes returns: run it only after the independent review, with --reviewed")
        cmd_report()
    elif cmd == "forward" and len(rest) == 1:
        cmd_forward(rest[0])
    else:
        raise SystemExit(USAGE)


if __name__ == "__main__":
    main(_argv)

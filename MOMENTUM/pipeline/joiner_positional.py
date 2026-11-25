"""Positional F&O-joiner study (JOINER_POSITIONAL_DESIGN.md, frozen with the OWNER ANSWERS 2026-11-21).

Protocol: code -> known-answer tests -> SEPARATE independent review -> run.  The count stages print counts /
booleans only; the two return commands refuse to run without --reviewed.

    python -I joiner_positional.py build              # NSE CM bhavcopy panel -> NSE_DATA/joiner_cache (data job)
    python -I joiner_positional.py probes             # P1-P4, P6, P7 + link / delivery census      (COUNTS ONLY)
    python -I joiner_positional.py gate               # question (A), §5.4, the STOP gate before returns (COUNTS ONLY)
    python -I joiner_positional.py dead               # §4.3 stopped names in B, for the events file      (COUNTS ONLY)
    python -I joiner_positional.py ca                 # §4.2 CA file -> data/joiner_ca_zero.json         (COUNTS ONLY)
    python -I joiner_positional.py oracle             # G3: P5 Kite oracle match rate              (COUNTS ONLY; G3)
    python -I joiner_positional.py g0 --reviewed      # G0 oracle ceiling + D2/D3/D4   (RETURNS: after the review)
    python -I joiner_positional.py report --reviewed  # question (B), §5.5 read rule   (RETURNS: after the review)

Data: CM bhavcopy (classic zip < 2024-07-08, UDiFF after), delivery from MTO (<= 2019-12) and sec_bhavdata_full,
all downloaded by nse_cm_fetch.py into NSE_DATA/cm_bhav (outside the public repo).  No Kite call anywhere; G0 and
the P5 oracle read the Kite dailies already on disk (survivorship.data_file).

Frozen choices implemented here (design section in brackets; module constants below):
- Identity [§4.3]: a CHAIN links rows by ISIN; if the ISIN is new, by the same symbol when the old ISIN stopped
  within LINK_GAP sessions (a face-value split issues a new ISIN); then by data/fo_rename_map.json.  Else a new chain.
- Returns [§4.2]: daily return = close / bhavcopy previous close - 1, EXCEPT on a split / bonus ex-date row from the
  NSE corporate-actions file (nse_ca.py; P3 found the previous close UNADJUSTED): there return = close / (the chain's
  prior close x factor) - 1, so an already-adjusted previous close is never adjusted twice.  Demergers / rights:
  ex-date return 0 (data/joiner_ca_zero.json, written by `ca`).  The return commands refuse unless P3 (raw) is YES or
  the CA file is complete AND the P3-CA known cases pass.  Price return, no dividends.
- Universe [§5.1] at the signal day s = last session before month m: an EQ row ON s, its ISIN starts INE, traded on
  >= 110 of the 126 sessions, not an F&O member of m, not ANNOUNCED.
- Score [§5.2]: TV = median daily traded value (Rs, CM file; a no-trade session counts 0), DV = median daily delivery
  value (deliverable qty x VWAP; sessions without a delivery file are skipped, a traded row missing from the delivery
  file is unknown, a BE / BZ row whose DELIV_QTY is '-' is 100% delivery (trade-for-trade), a no-trade session counts 0; DV needs >= 80% of the window's delivery sessions known).  Ranked
  over every chain whose latest window row is EQ with an INE ISIN ("ALL EQ stocks": ETFs excluded; members
  included), 1 = highest, ties by symbol (inside each rank, then on the score).  Score = the worse rank; ties by symbol.  B = 150 best, P = 30 best.
- Labels [§3, §5.4]: J = first member month after m; "joins within k" iff J <= m + k - 1 (a member in one of the k
  months m..m+k-1).  Complete iff the membership file reaches m + k - 1: 12 m to entries 2025-12, 6 m to 2026-06,
  exactly the design's §3 bounds; §1.4's "no 12-month joiner for entries 2022-03..2023-12" also requires it.
- ANNOUNCED [§3, P1]: data/fo_announced.json if present.  Absent -> (A) drops stock-months with 1 <= J - m <= 2
  before ranking, (B) keeps them and prints the bias count.  With the file, `report` FLAGS (owner D1) every pick whose
  circular is dated after the signal day but on or before its actual entry session (eligible by §3; count only).
- Positions [§3]: entry = close of the first session of m; locked (close = high and day return >= +4.9%, or no
  trade) -> next session, up to 5 more, else skipped (the cohort is the names that entered).  Hold = 126 sessions
  from the actual entry.  Locked exit (close = low and day return <= -4.9%, or no trade) -> next exitable session.
- Stopped names [§4.3]: no row for 20 sessions -> data/joiner_exit_events.json by ISIN: merger / scheme / voluntary
  delisting -> exit at the last traded close (or swap into `into_isin`); anything else -> -100% in the read, last
  traded close in the second table.  Fewer than 20 observable sessions at the data end -> `pending`, treated as dead.
- Calendar-time [§5.3]: each cohort is a buy-and-hold equal-weight basket (exited names sit in cash inside it);
  month T return of a cohort = its value at the last session of T / at the last session of T-1 (its first month is
  measured from 1.0 before the entry cost).  The money runs in hold/21 sleeves (6 for 126 sessions); a sleeve rolls
  into the cohort 6 months later, compounding the two in the roll month.  Arm month-T return = mean over live sleeves.
- Costs 0.40%/side (levels also at 0.15% and 1.0%).  Read rule §5.5, Bonferroni 3 hypotheses -> 98.33% CI, moving
  block bootstrap of months (monthly_orb.mbb_ci), block 6, 5,000 draws, seed 7.
"""
from __future__ import annotations

import csv
import io
import json
import math
import random
import statistics as st
import sys
import warnings
from bisect import bisect_left, bisect_right
from datetime import date as _date
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]                         # imported modules truncate sys.argv at import: capture ours first
sys.argv = sys.argv[:1]
import compounding as CMP  # noqa: E402
import full_asrs as F  # noqa: E402
import monthly_orb as MO  # noqa: E402
import nse_ca as CA  # noqa: E402
import nse_cm_fetch as NF  # noqa: E402
import survivorship as V  # noqa: E402

# ---------------------------------------------------------------- paths
CM_ROOT = NF.ROOT                                         # NSE_DATA/cm_bhav (the fetcher's folder; read only)
CACHE_DIR = NF.ROOT.parent / "joiner_cache"               # our derived panel, outside the repo and the fetcher's dirs
PANEL_FILE = CACHE_DIR / "panel.npz"
BUILD_INFO = CACHE_DIR / "build_info.json"
INDEX_DIR = NF.ROOT.parent / "indices"                    # optional {date: close} JSON per index (report only)
ANNOUNCED_FILE = HERE / "data" / "fo_announced.json"      # P1 (owner D1): [{symbol, announced, member_month, withdrawn?, source}]
EVENTS_FILE = HERE / "data" / "joiner_exit_events.json"   # §4.3, optional: {ISIN: {type, into_isin?, note}}
CA_ZERO_FILE = HERE / "data" / "joiner_ca_zero.json"      # §4.2, optional: [{isin, date, kind: demerger|rights}]
CA_ROOT = CA.ROOT                                         # NSE corporate-actions month files (outside the repo)
CA_OVERRIDE_FILE = HERE / "data" / "joiner_ca_override.json"   # §4.2 hand-kept fixes of CA-file errors, with evidence
OUT_DIR = HERE / "results"

# ---------------------------------------------------------------- frozen constants
SERIES = {"EQ": 1, "BE": 2, "BZ": 3}                      # kept series; EQ is the only eligible one
EQ = 1
T2T_SERIES = ("BE", "BZ")                                 # trade-for-trade: every trade is a delivery
WIN = 126                                                 # §5.2 score window (sessions)
MIN_TRADED = 110                                          # §5.1
GROW_LAG = 126                                            # §5.2 GROW: TV now / TV 126 sessions earlier
ATR_N = 14                                                # §5.2 VOL
MIN_DELIV_SHARE = 0.80                                    # DV defined iff >= 80% of the window's delivery sessions known
N_B, N_P, N_TOP5 = 150, 30, 5
HOLD = 126                                                # §3 primary hold (sessions)
HOLDS_PRINTED = (63, 252)                                 # printed, never read
SESS_PER_MONTH = 21                                       # hold sessions -> calendar-time sleeves
LOCK_PCT = 0.049
LOCK_TRIES = 5
DEAD_GAP = 20                                             # §4.3
LINK_GAP = 20                                             # identity: max sessions between an old ISIN and a new one
COST = 0.004                                              # §5.3 per side
COST_LEVELS = (0.0015, 0.010)                             # sensitivity, not a gate
FIRST_ENTRY = "2020-01"
IS_W = ("2020-01", "2023-12")
OOS_W = ("2024-01", "2026-10")
ERAS = (("2020-21 entries", "2020-01", "2021-12"), ("2024-25 entries", "2024-01", "2025-12"),
        ("2025 entries alone", "2025-01", "2025-12"))
GATE_ERA = ("2024-01", "2025-12")
GATE_PREC, GATE_MULT = 0.25, 2.0
LABEL_K = (6, 12)
GATE_K = 12
FALLBACK_DROP = 2                                         # P1 fallback: drop positives at J - m <= 2
N_HYP = 3                                                 # P, GROW, VOL
CI_LEVEL = 1 - 0.05 / N_HYP                               # 98.33%
BOOT_DRAWS, SEED, BLOCK = 5000, 7, 6
CRAND_DRAWS, CRAND_PCT = 1000, 95
CAPITAL, CAPITAL_NAMES = 5_000_000.0, 30                  # owner Q6: Rs 50 L across 30 names
STCG_CESS = 0.04                                          # owner D4: health and education cess on the STCG
POS_TV_MAX, BUILD_TV_DAY = 0.05, 0.01                     # §6 capacity report rule
CAP_LOSS_CARRY = 8                                        # capital losses: 8 FYs (recall; confirm with the CA)
MERGE_TYPES = ("merger", "scheme", "voluntary_delisting")            # exit at the last close (or swap into_isin)
# owner D3 2026-11-23: the name stopped on NSE but the equity lived on (SPICEJET, BESTAGRO): exit at the last NSE
# close, never swapped, never -100%
LAST_CLOSE_TYPES = MERGE_TYPES + ("last_close",)
# -100% in the read: write_off = equity extinguished by a scheme (LAKSHVILAS); a label only, booked like suspension
EVENT_TYPES = LAST_CLOSE_TYPES + ("suspension", "compulsory_delisting", "write_off")
P3_SPLIT = ("IRCTC", "2021-10-28", 5.0)                   # the design's known split (1:5)
# P3 on the CA path: known answers (symbol, CA ex-date, factor).  IRCTC split 10 -> 2; SRF bonus 4:1; GPIL split
# 10 -> 5 plus bonus 1:1 on one day; DUCON bonus 1:10 (the design's small bonus the gap detector cannot see).
P3_CA_CASES = (("IRCTC", "2021-10-28", 1 / 5), ("SRF", "2021-10-13", 1 / 5), ("GPIL", "2021-10-26", 1 / 4),
               ("DUCON", "2022-04-18", 10 / 11))
CA_SEARCH = 5                                             # identity: ISIN / symbol carried up to 5 sessions after
CA_LOOKBACK = LINK_GAP                                    # ISIN / symbol searched from 20 sessions before the ex-date
BONUS_FACTORS = {"1:10 bonus": 10 / 11, "1:5 bonus": 5 / 6, "1:4 bonus": 4 / 5, "1:2 bonus": 2 / 3}
ADJ_TOL = 0.005                                           # previous close differs from the prior close by > 0.5%
GAP_JUMP = 0.20                                           # §4.2 gap detector (cross-check)
ORACLE_TOL, ORACLE_PASS = 0.01, 0.99                      # P5
CA_SAMPLE = 30
CA_ZERO_KINDS = ("demerger", "rights")
FACTOR_LABELS = ((1 / 2, "1:2 split or 1:1 bonus"), (1 / 3, "1:3 split or 2:1 bonus"), (1 / 4, "1:4 split"),
                 (1 / 5, "1:5 split"), (1 / 10, "1:10 split"), (2 / 3, "1:2 bonus"), (3 / 4, "1:3 bonus"),
                 (4 / 5, "1:4 bonus"), (5 / 6, "1:5 bonus"), (10 / 11, "1:10 bonus"))
MAX_CM_GAPS = 5                                           # build refuses a panel with more missing CM sessions
SUCC_DAYS, SUCC_TOL = 5, 0.02                             # dead list: a possible unlinked successor chain
MONTHS_TO_JOIN = ((1, 3), (4, 6), (7, 12), (13, 24), (25, 999))

Row = Tuple[str, str, str, float, float, float, float, float, float]   # sym, series, isin, close, prev, h, l, qty, val


def stcg_rate(fy: int) -> float:
    """STCG on listed equity (recall, confirm with the CA): 15% to FY2023-24, 20% from FY2024-25 (the July-2024 change
    applied to the whole FY: conservative), plus the 4% cess (owner D4).  No surcharge (income dependent)."""
    return (0.15 if fy < 2024 else 0.20) * (1 + STCG_CESS)


def capital_per_name(sleeves: int, capital: float = CAPITAL, names: int = CAPITAL_NAMES) -> float:
    """Owner D2: the ACTUAL holding per name -- the capital runs in `sleeves` overlapping cohorts of `names` each
    (Rs 50 L / (6 x 30) ~ Rs 27.8K).  Capacity is still checked at capital / names (Rs 1.67 L)."""
    return capital / (sleeves * names)


# ---------------------------------------------------------------- month helpers
def madd(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) - 1 + k
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def mdiff(a: str, b: str) -> int:
    """Months from b to a."""
    return (int(a[:4]) - int(b[:4])) * 12 + int(a[5:7]) - int(b[5:7])


def month_range(a: str, b: str) -> List[str]:
    out, m = [], a
    while m <= b:
        out.append(m)
        m = madd(m, 1)
    return out


def in_window(m: str, w: Tuple[str, str]) -> bool:
    return w[0] <= m <= w[1]


# ---------------------------------------------------------------- parsing (downloaded files are untrusted text)
def _num(v: str) -> float:
    v = (v or "").strip()
    if v in ("", "-"):
        return math.nan
    return float(v)


CM_COLS = {
    "old": dict(sym="SYMBOL", series="SERIES", isin="ISIN", close="CLOSE", prev="PREVCLOSE", high="HIGH", low="LOW",
                qty="TOTTRDQTY", val="TOTTRDVAL"),
    "udiff": dict(sym="TckrSymb", series="SctySrs", isin="ISIN", close="ClsPric", prev="PrvsClsgPric", high="HghPric",
                  low="LwPric", qty="TtlTradgVol", val="TtlTrfVal"),
}


def parse_cm(text: str) -> List[Row]:
    """Both CM bhavcopy formats -> the same row tuple, kept series only.  A missing column raises (never guessed)."""
    rd = csv.reader(io.StringIO(text))
    hdr = [h.strip() for h in next(rd)]
    fmt = "old" if "SYMBOL" in hdr else "udiff" if "TckrSymb" in hdr else None
    if fmt is None:
        raise ValueError(f"unknown CM header {hdr[:6]}")
    cols = CM_COLS[fmt]
    missing = [c for c in cols.values() if c not in hdr]
    if missing:
        raise ValueError(f"CM {fmt} header lacks {missing}")
    ix = {k: hdr.index(c) for k, c in cols.items()}
    tp = hdr.index("FinInstrmTp") if fmt == "udiff" and "FinInstrmTp" in hdr else None
    out: List[Row] = []
    for r in rd:
        if len(r) < len(hdr) - 1:
            continue
        r = [x.strip() for x in r]
        if tp is not None and r[tp] not in ("STK", ""):
            continue
        if r[ix["series"]] not in SERIES:
            continue
        out.append((r[ix["sym"]], r[ix["series"]], r[ix["isin"]], _num(r[ix["close"]]), _num(r[ix["prev"]]),
                    _num(r[ix["high"]]), _num(r[ix["low"]]), _num(r[ix["qty"]]), _num(r[ix["val"]])))
    return out


def parse_full(text: str) -> Dict[Tuple[str, str], float]:
    """sec_bhavdata_full -> {(symbol, series): deliverable qty}; '-' (no delivery figure) -> NaN."""
    rd = csv.reader(io.StringIO(text))
    hdr = [h.strip() for h in next(rd)]
    for c in ("SYMBOL", "SERIES", "DELIV_QTY"):
        if c not in hdr:
            raise ValueError(f"full header lacks {c}")
    i_s, i_r, i_d = hdr.index("SYMBOL"), hdr.index("SERIES"), hdr.index("DELIV_QTY")
    out: Dict[Tuple[str, str], float] = {}
    for r in rd:
        if len(r) <= i_d:
            continue
        out[(r[i_s].strip(), r[i_r].strip())] = _num(r[i_d])
    return out


def parse_mto(text: str) -> Dict[Tuple[str, str], float]:
    """MTO .DAT -> {(symbol, series): deliverable qty} from record type 20 lines."""
    out: Dict[Tuple[str, str], float] = {}
    for line in text.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 6 and p[0] == "20":
            out[(p[2], p[3])] = _num(p[5])
    return out


# ---------------------------------------------------------------- identity chains
class Linker:
    """Day-by-day identity linking (§4.3): ISIN first; a NEW ISIN continues the chain that last carried the same
    symbol if that chain's ISIN is not trading today and it traded within LINK_GAP sessions (face-value split); then
    the F&O rename map (old symbol -> new symbol) under the same conditions.  Otherwise a new chain."""

    def __init__(self, rename_map: Optional[Dict[str, Optional[str]]] = None, link_gap: int = LINK_GAP):
        self.link_gap = link_gap
        self.isin_chain: Dict[str, int] = {}
        self.sym_chain: Dict[str, int] = {}
        self.last_t: List[int] = []
        self.last_isin: List[str] = []
        self.last_sym: List[str] = []
        self.events: List[Tuple[int, int, str, str, str]] = []   # (chain, t, kind, old, new)
        self.rev: Dict[str, List[str]] = defaultdict(list)
        for old, new in (rename_map or {}).items():
            if new is not None and new != old:
                self.rev[new].append(old)
        self.dupes = 0

    def _new(self, t: int, isin: str, sym: str) -> int:
        c = len(self.last_t)
        self.last_t.append(t)
        self.last_isin.append(isin)
        self.last_sym.append(sym)
        return c

    def _free(self, c: int, t: int, isins_today: Set[str], taken: Set[int]) -> bool:
        return c not in taken and t - self.last_t[c] <= self.link_gap and self.last_isin[c] not in isins_today

    def link_day(self, t: int, rows: Sequence[Row]) -> List[Tuple[int, Row]]:
        best: Dict[str, Row] = {}
        for r in rows:                                    # one row per ISIN: EQ > BE > BZ
            isin = r[2] or f"NOISIN:{r[0]}"
            if isin in best:
                self.dupes += 1
                if SERIES[r[1]] >= SERIES[best[isin][1]]:
                    continue
            best[isin] = r
        isins_today = set(best)
        taken: Set[int] = set()
        out: List[Tuple[int, Row]] = []
        pending: List[Tuple[str, Row]] = []
        for isin, r in sorted(best.items()):
            c = self.isin_chain.get(isin)
            if c is None or c in taken:
                pending.append((isin, r))
                continue
            taken.add(c)
            if self.last_sym[c] != r[0]:
                self.events.append((c, t, "rename", self.last_sym[c], r[0]))
            out.append((c, r))
        for isin, r in pending:
            sym = r[0]
            c = self.sym_chain.get(sym)
            kind = "isin_change"
            if c is None or not self._free(c, t, isins_today, taken) or self.last_sym[c] != sym:
                c, kind = None, ""
                for old in self.rev.get(sym, []):
                    k = self.sym_chain.get(old)
                    if k is not None and self._free(k, t, isins_today, taken) and self.last_sym[k] == old:
                        c, kind = k, "rename_map"
                        break
            if c is None:
                c = self._new(t, isin, sym)
            else:
                self.events.append((c, t, kind, f"{self.last_sym[c]}/{self.last_isin[c]}", f"{sym}/{isin}"))
            taken.add(c)
            self.isin_chain[isin] = c
            out.append((c, r))
        for c, r in out:
            self.last_t[c] = t
            self.last_isin[c] = r[2] or f"NOISIN:{r[0]}"
            self.last_sym[c] = r[0]
            self.sym_chain[r[0]] = c
        return out


# ---------------------------------------------------------------- the panel
class Panel:
    """Dense chain x session arrays.  close/prev/high/low NaN = no row; tv NaN = no row; dv: NaN = no row or delivery
    unknown (see has_row); traded = qty > 0; series 0 = no row."""

    def __init__(self, sessions: List[str], close: np.ndarray, prev: np.ndarray, high: np.ndarray, low: np.ndarray,
                 tv: np.ndarray, dv: np.ndarray, traded: np.ndarray, series: np.ndarray, isin_idx: np.ndarray,
                 sym_idx: np.ndarray, has_deliv: np.ndarray, isins: List[str], syms: List[str],
                 links: Optional[List[Tuple[int, int, str, str, str]]] = None):
        self.sessions = list(sessions)
        self.close, self.prev, self.high, self.low = close, prev, high, low
        self.tv, self.dv, self.traded, self.series = tv, dv, traded, series
        self.isin_idx, self.sym_idx, self.has_deliv = isin_idx, sym_idx, has_deliv
        self.isins, self.syms = list(isins), list(syms)
        self.links = list(links or [])
        self.C, self.S = close.shape
        self.has_row = ~np.isnan(close)
        self.sym_of = {s: i for i, s in enumerate(self.syms)}
        self.isin_of = {s: i for i, s in enumerate(self.isins)}
        self._ret: Optional[np.ndarray] = None
        self._G: Optional[np.ndarray] = None
        self.bad_prev = 0
        self.months = month_bounds(self.sessions)

    # -- the return side (never used by the count stages)
    @property
    def ret(self) -> np.ndarray:
        if self._ret is None:
            r = np.zeros((self.C, self.S))
            ok = self.has_row & (self.prev > 0) & np.isfinite(self.prev)
            first = np.argmax(self.has_row, axis=1)
            ok[np.arange(self.C), first] = False           # the first row's previous close is an issue price
            with np.errstate(invalid="ignore", divide="ignore"):
                r[ok] = self.close[ok] / self.prev[ok] - 1
            self.bad_prev = int((self.has_row & ~(self.prev > 0)).sum())
            self._ret = r
        return self._ret

    @property
    def G(self) -> np.ndarray:
        """Cumulative adjusted price index per chain (flat on no-row sessions)."""
        if self._G is None:
            self._G = np.cumprod(1 + self.ret, axis=1)
        return self._G

    def sym_at(self, c: int, t: int) -> str:
        return self.syms[self.sym_idx[c, t]] if self.has_row[c, t] else ""

    def last_row_at(self, c: int, t: int) -> int:
        """Index of the last row of chain c at or before t; -1 if none."""
        rows = np.nonzero(self.has_row[c, :t + 1])[0]
        return int(rows[-1]) if len(rows) else -1

    def chain_of_isin(self, isin: str, t: int) -> Optional[int]:
        i = self.isin_of.get(isin)
        if i is None:
            return None
        hit = np.nonzero(self.has_row[:, :t + 1].any(axis=1) & (self.isin_idx[:, :t + 1] == i).any(axis=1))[0]
        return int(hit[0]) if len(hit) else None

    def save(self, f: Path) -> None:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp.npz")
        np.savez_compressed(tmp, sessions=np.array(self.sessions), close=self.close, prev=self.prev, high=self.high,
                            low=self.low, tv=self.tv, dv=self.dv, traded=self.traded, series=self.series,
                            isin_idx=self.isin_idx, sym_idx=self.sym_idx, has_deliv=self.has_deliv,
                            isins=np.array(self.isins), syms=np.array(self.syms),
                            links=np.array([json.dumps(x) for x in self.links] or [""]))
        tmp.replace(f)

    @classmethod
    def load(cls, f: Path = PANEL_FILE) -> "Panel":
        if not f.exists():
            raise SystemExit(f"no panel at {f}: run `python -I joiner_positional.py build` first")
        z = np.load(f, allow_pickle=False)
        links = [tuple(json.loads(x)) for x in z["links"].tolist() if x]
        return cls([str(s) for s in z["sessions"]], z["close"], z["prev"], z["high"], z["low"], z["tv"], z["dv"],
                   z["traded"], z["series"], z["isin_idx"], z["sym_idx"], z["has_deliv"],
                   [str(s) for s in z["isins"]], [str(s) for s in z["syms"]], links)  # type: ignore[arg-type]


def month_bounds(sessions: Sequence[str]) -> Dict[str, Tuple[int, int]]:
    """{YYYY-MM: (first session index, last session index)}."""
    out: Dict[str, Tuple[int, int]] = {}
    for i, d in enumerate(sessions):
        m = d[:7]
        out[m] = (out[m][0], i) if m in out else (i, i)
    return out


class PanelBuilder:
    """Streams days in date order (memory: 32-bit Python) and scatters into dense arrays at the end."""

    def __init__(self, rename_map: Optional[Dict[str, Optional[str]]] = None):
        self.linker = Linker(rename_map)
        self.sessions: List[str] = []
        self.has_deliv: List[bool] = []
        self.isins: Dict[str, int] = {}
        self.syms: Dict[str, int] = {}
        self.cols: List[tuple] = []
        self.deliv_missing = 0
        self.deliv_t2t = 0                                # BE / BZ rows with DELIV_QTY '-': 100% delivery

    def _ix(self, d: Dict[str, int], s: str) -> int:
        if s not in d:
            d[s] = len(d)
        return d[s]

    def add_day(self, date: str, rows: Sequence[Row], deliv: Optional[Dict[Tuple[str, str], float]]) -> None:
        if self.sessions and date <= self.sessions[-1]:
            raise ValueError(f"days out of order: {date} after {self.sessions[-1]}")
        t = len(self.sessions)
        self.sessions.append(date)
        self.has_deliv.append(deliv is not None)
        cs, ser, isn, sym, px, tv, dv, tr = [], [], [], [], [], [], [], []
        for c, r in self.linker.link_day(t, rows):
            s_, se, isin, close, prev, high, low, qty, val = r
            traded = qty > 0
            d = math.nan
            if deliv is not None:
                key = (s_, se)
                dq = deliv.get(key, math.nan)
                if not traded:
                    d = 0.0
                elif math.isnan(dq) and key in deliv and se in T2T_SERIES:
                    # review 2026-11-23: sec_bhavdata_full prints DELIV_QTY '-' on every BE / BZ row (trade-for-trade:
                    # each trade settles by delivery; MTO shows them at 100%).  Unknown here dropped every name with a
                    # BE stretch in its window (SUZLON, ADANIGREEN, AWL, ...) to the bottom of the DV rank.
                    d = val
                    self.deliv_t2t += 1
                elif math.isnan(dq):
                    self.deliv_missing += 1
                else:
                    d = dq * val / qty
            cs.append(c)
            ser.append(SERIES[se])
            isn.append(self._ix(self.isins, isin or f"NOISIN:{s_}"))
            sym.append(self._ix(self.syms, s_))
            px.append((close, prev, high, low))
            tv.append(val if val == val else 0.0)
            dv.append(d)
            tr.append(traded)
        # compact per-day arrays (~60 bytes a row): the 32-bit process holds ~4 M rows plus the dense panel
        self.cols.append((np.array(cs, np.int32), np.array(ser, np.int8), np.array(isn, np.int32),
                          np.array(sym, np.int32), np.array(px, np.float64).reshape(-1, 4),
                          np.array(tv, np.float32), np.array(dv, np.float32), np.array(tr, bool)))

    def panel(self) -> Panel:
        C, S = len(self.linker.last_t), len(self.sessions)
        close, prev, high, low = (np.full((C, S), np.nan) for _ in range(4))
        tv, dv = np.full((C, S), np.nan, np.float32), np.full((C, S), np.nan, np.float32)
        traded = np.zeros((C, S), bool)
        series = np.zeros((C, S), np.int8)
        isin_idx, sym_idx = np.full((C, S), -1, np.int32), np.full((C, S), -1, np.int32)
        for t, (cs, ser, isn, sym, px, tv_, dv_, tr) in enumerate(self.cols):
            close[cs, t], prev[cs, t], high[cs, t], low[cs, t] = px[:, 0], px[:, 1], px[:, 2], px[:, 3]
            tv[cs, t], dv[cs, t], traded[cs, t], series[cs, t] = tv_, dv_, tr, ser
            isin_idx[cs, t], sym_idx[cs, t] = isn, sym
        self.cols = []
        return Panel(self.sessions, close, prev, high, low, tv, dv, traded, series, isin_idx, sym_idx,
                     np.array(self.has_deliv, dtype=bool), sorted(self.isins, key=self.isins.__getitem__),
                     sorted(self.syms, key=self.syms.__getitem__), self.linker.events)


def panel_from_days(days: Sequence[Tuple[str, Sequence[Row], Optional[Dict[Tuple[str, str], float]]]],
                    rename_map: Optional[Dict[str, Optional[str]]] = None) -> Panel:
    b = PanelBuilder(rename_map)
    for d, rows, deliv in days:
        b.add_day(d, rows, deliv)
    return b.panel()


# ---------------------------------------------------------------- membership by entity, labels
def member_chains(panel: Panel, mem: Dict[str, List[str]], look: int = 5
                  ) -> Tuple[Dict[str, Set[int]], Dict[str, List[str]]]:
    """F&O members of month m as chains: each member symbol is matched to the chain that carried it on the first CM
    session of m (else the nearest session within `look` before, then after).  Unmapped symbols are P6 failures."""
    sets: Dict[str, Set[int]] = {}
    unmapped: Dict[str, List[str]] = {}
    for m in sorted(mem):
        if m not in panel.months:
            continue
        f = panel.months[m][0]
        order = [f] + [f - k for k in range(1, look + 1)] + [f + k for k in range(1, look + 1)]
        cols: Dict[int, Dict[str, int]] = {}
        got: Set[int] = set()
        miss: List[str] = []
        for x in mem[m]:
            hit = None
            si = panel.sym_of.get(x)
            if si is not None:
                for t in order:
                    if not 0 <= t < panel.S:
                        continue
                    if t not in cols:
                        rows = np.nonzero(panel.has_row[:, t])[0]
                        cols[t] = {panel.syms[panel.sym_idx[k, t]]: int(k) for k in rows}
                    if x in cols[t]:
                        hit = cols[t][x]
                        break
            if hit is None:
                miss.append(x)
            else:
                got.add(hit)
        sets[m], unmapped[m] = got, miss
    return sets, unmapped


def member_months_of(sets: Dict[str, Set[int]]) -> Dict[int, List[str]]:
    out: Dict[int, List[str]] = defaultdict(list)
    for m in sorted(sets):
        for c in sets[m]:
            out[c].append(m)
    return dict(out)


def first_join_after(months_of_c: Sequence[str], m: str) -> Optional[str]:
    i = bisect_right(list(months_of_c), m)
    return months_of_c[i] if i < len(months_of_c) else None


def join_distance(months_of_c: Sequence[str], m: str) -> Optional[int]:
    """J - m in months for the first member month J after m; None if it never (re)joins inside the file."""
    j = first_join_after(months_of_c, m)
    return None if j is None else mdiff(j, m)


def joins_within(months_of_c: Sequence[str], m: str, k: int) -> bool:
    """"Joins within k months" = a member in one of the k months m..m+k-1, i.e. J <= m + k - 1 (the design's
    convention: §3 completeness to 2025-12 / 2026-06 and §1.4 'zero 12-month joiners for entries 2022-03..2023-12'
    both hold only under it)."""
    d = join_distance(months_of_c, m)
    return d is not None and 1 <= d <= k - 1


def label_complete(m: str, k: int, last_month: str) -> bool:
    """The k-month label of entry month m needs the membership of m + k - 1."""
    return mdiff(last_month, m) >= k - 1


def join_waves(sets: Dict[str, Set[int]]) -> Dict[str, Set[int]]:
    """{J: chains that are members in J and not in J-1} for J after the first membership month."""
    ms = sorted(sets)
    return {m: sets[m] - sets[p] for p, m in zip(ms, ms[1:]) if mdiff(m, p) == 1}


# ---------------------------------------------------------------- announced list (P1)
def load_announced(f: Optional[Path] = None) -> Optional[List[dict]]:
    """None if absent (the design's fallback applies).  Validated: symbol, announced (YYYY-MM-DD), member_month."""
    f = f if f is not None else ANNOUNCED_FILE
    if not f.exists():
        return None
    xs = json.loads(f.read_text())
    if not isinstance(xs, list):
        raise ValueError(f"{f}: expected a list")
    for x in xs:
        if not (isinstance(x, dict) and isinstance(x.get("symbol"), str) and len(str(x.get("announced"))) == 10
                and len(str(x.get("member_month"))) == 7
                and (x.get("withdrawn") is None or (len(str(x["withdrawn"])) == 10 and x["withdrawn"] >= x["announced"]))):
            raise ValueError(f"{f}: bad entry {x}")
    return xs


def announced_block(announced: Optional[List[dict]], sym_chain: Dict[str, int], signal_date: str, m: str) -> Set[int]:
    """Chains ineligible at the signal day: announced on or before it, not yet a member month, and not withdrawn on
    or before it (NSE withdrew some inclusions before the start: SUPREMEIND 2021, CASTROLIND / GLAND / ARE&M / FSL 2025)."""
    out: Set[int] = set()
    for x in announced or []:
        if x.get("withdrawn") and x["withdrawn"] <= signal_date:
            continue
        if x["announced"] <= signal_date and x["member_month"] >= m and x["symbol"] in sym_chain:
            out.add(sym_chain[x["symbol"]])
    return out


def announced_at_entry(announced: Optional[List[dict]], sym: str, signal_date: str, entry_date: str, m: str
                       ) -> Optional[dict]:
    """Owner D1 'flagged': the circular of `sym` if it is dated AFTER the signal day (so the name stayed eligible,
    §3 / §5.1) but on or before the actual entry session (entry day, or a locked-entry deferral), for a member month
    >= m, and not withdrawn by the entry.  Report-only: eligibility never changes."""
    for x in announced or []:
        if x["symbol"] != sym or not signal_date < x["announced"] <= entry_date or x["member_month"] < m:
            continue
        if x.get("withdrawn") and x["withdrawn"] <= entry_date:
            continue
        return x
    return None


def load_ca_zero(f: Optional[Path] = None) -> List[dict]:
    """§4.2 demergers / rights (written by `ca` from the CA file): the ex-date return of each is set to 0 (no factor
    guessed).  The path is resolved at call time, so a test that points CA_ZERO_FILE elsewhere never reads prod."""
    f = f if f is not None else CA_ZERO_FILE
    if not f.exists():
        return []
    xs = json.loads(f.read_text())
    for x in xs if isinstance(xs, list) else [None]:
        if not (isinstance(x, dict) and isinstance(x.get("isin"), str) and len(str(x.get("date"))) == 10
                and x.get("kind") in CA_ZERO_KINDS):
            raise ValueError(f"{f}: bad entry {x}")
    return xs


def apply_ca_zero(panel: Panel, entries: Sequence[dict]) -> List[str]:
    """Set the return of each listed (ISIN, ex-date) row to 0 and reset the price index.  Every entry must hit a row."""
    r = panel.ret
    out = []
    for x in entries:
        if x["date"] not in panel.sessions or x["isin"] not in panel.isin_of:
            raise ValueError(f"CA-zero entry matches no panel row: {x}")
        t, i = panel.sessions.index(x["date"]), panel.isin_of[x["isin"]]
        hit = np.nonzero(panel.has_row[:, t] & (panel.isin_idx[:, t] == i))[0]
        if not len(hit):
            raise ValueError(f"CA-zero entry matches no panel row: {x}")
        for c in hit:
            r[c, t] = 0.0
            out.append(f"{panel.sym_at(int(c), t)} {x['date']} {x['kind']}")
    panel._G = None
    return out


@dataclass
class CAFix:
    sym: str            # the CA file's symbol
    ex: str             # the CA file's ex-date
    factor: float       # product of the distinct subjects applied on this row
    c: int
    t: int              # the ex-date row (first row of the chain on or after the ex-date, however far)
    pr: int             # the chain's prior row
    how: str            # matched by isin / symbol
    prev: str           # bhavcopy previous close on that row: unadjusted / adjusted / other


@dataclass
class CAResult:
    complete: bool                                    # every month of the CA file on disk and valid
    n_records: int = 0
    n_factor: int = 0
    fixes: List[CAFix] = field(default_factory=list)
    unmatched: List[Tuple[str, str, str]] = field(default_factory=list)   # (symbol, ex-date, reason)
    beyond: int = 0                                   # ex-date after the last panel session
    zeroed: List[str] = field(default_factory=list)
    owner: List[str] = field(default_factory=list)    # unparsed / capital reduction / scheme / bonus debentures
    overrides: List[str] = field(default_factory=list)  # hand-kept CA-file corrections applied


def ca_locate(panel: Panel, isin: str, sym: str, ex: str) -> Tuple[Optional[Tuple[int, int, int, str]], str]:
    """The (chain, ex-date row, prior row, how) a corporate action applies to, or (None, reason).  ISIN first (the
    ISIN the chain carried from CA_LOOKBACK sessions before the ex-date: a split may issue a new ISIN), then the
    symbol; more than one candidate chain is ambiguous and never guessed."""
    t0 = bisect_left(panel.sessions, ex)
    if t0 >= panel.S:
        return None, "after the panel"
    lo, hi = max(0, t0 - CA_LOOKBACK), min(panel.S, t0 + CA_SEARCH + 1)
    hit: np.ndarray = np.array([], dtype=int)
    how = "isin"
    i = panel.isin_of.get(isin)
    if i is not None:
        hit = np.nonzero(((panel.isin_idx[:, lo:hi] == i) & panel.has_row[:, lo:hi]).any(axis=1))[0]
    if not len(hit):
        how = "symbol"
        j = panel.sym_of.get(sym)
        if j is not None:
            hit = np.nonzero(((panel.sym_idx[:, lo:hi] == j) & panel.has_row[:, lo:hi]).any(axis=1))[0]
    if not len(hit):
        return None, "no chain carries the ISIN or symbol near the ex-date"
    if len(hit) > 1:
        return None, f"ambiguous: {len(hit)} chains by {how}"
    c = int(hit[0])
    rows = t0 + np.nonzero(panel.has_row[c, t0:])[0]       # first row back carries the new basis, however far
    if not len(rows):
        return None, "no row on or after the ex-date"
    pr = panel.last_row_at(c, t0 - 1) if t0 > 0 else -1
    if pr < 0:
        return None, "no row before the ex-date on the chain"
    return (c, int(rows[0]), pr, how), ""


def apply_ca_factors(panel: Panel, events: Sequence[CA.CAEvent]) -> Tuple[List[CAFix], List[Tuple[str, str, str]], int]:
    """Set each split / bonus ex-date row's return to close / (prior close x factor) - 1.  Factors of distinct
    subjects landing on one row multiply; the same subject listed twice (e.g. under the old and the new ISIN)
    counts once."""
    per_row: Dict[Tuple[int, int], Dict[str, float]] = {}
    meta: Dict[Tuple[int, int], Tuple[str, str, int, str]] = {}
    unmatched: List[Tuple[str, str, str]] = []
    beyond = 0
    for ev in events:
        loc, why = ca_locate(panel, ev.isin, ev.symbol, ev.ex)
        if loc is None:
            if why == "after the panel":
                beyond += 1
            else:
                unmatched.append((ev.symbol, ev.ex, why))
            continue
        c, t, pr, how = loc
        per_row.setdefault((c, t), {}).update(dict(ev.parts))
        meta.setdefault((c, t), (ev.symbol, ev.ex, pr, how))
    r = panel.ret
    fixes: List[CAFix] = []
    for (c, t), parts in sorted(per_row.items()):
        f = float(np.prod(list(parts.values())))
        sym, ex, pr, how = meta[(c, t)]
        base = float(panel.close[c, pr])
        pv = float(panel.prev[c, t])
        cls = ("unadjusted" if abs(pv / base - 1) <= ADJ_TOL else "adjusted" if abs(pv / (base * f) - 1) <= ADJ_TOL
               else "other")
        r[c, t] = panel.close[c, t] / (base * f) - 1
        fixes.append(CAFix(sym, ex, f, c, t, pr, how, cls))
    panel._G = None
    return fixes, unmatched, beyond


CA_OVERRIDE_ACTIONS = ("drop", "reassign")


def load_ca_overrides(f: Optional[Path] = None) -> List[dict]:
    """Hand-kept corrections of NSE CA-file records: [{ca_symbol, ex, action: drop | reassign, to_symbol?, notes,
    applies_to?: factor (default) | zero}].  `reassign` moves the record to another symbol (the ISIN is cleared, so
    it is located by symbol: the CA file names a company by its CURRENT symbol / ISIN, the panel by the one it
    carried then)."""
    f = f if f is not None else CA_OVERRIDE_FILE
    if not f.exists():
        return []
    xs = json.loads(f.read_text(encoding="utf-8"))
    for x in xs if isinstance(xs, list) else [None]:
        if not (isinstance(x, dict) and isinstance(x.get("ca_symbol"), str) and len(str(x.get("ex"))) == 10
                and x.get("action") in CA_OVERRIDE_ACTIONS and isinstance(x.get("notes"), str)
                and (x["action"] == "drop" or isinstance(x.get("to_symbol"), str))
                and x.get("applies_to", "factor") in ("factor", "zero")):
            raise ValueError(f"{f}: bad override {x}")
    return xs


def apply_ca_overrides(events: Sequence[CA.CAEvent], overrides: Sequence[dict]) -> Tuple[List[CA.CAEvent], List[str]]:
    """Every override must hit exactly one event (a stale override is an error, never silently ignored)."""
    out = list(events)
    done = []
    for o in overrides:
        hit = [i for i, e in enumerate(out) if e.symbol == o["ca_symbol"] and e.ex == o["ex"]]
        if len(hit) != 1:
            raise ValueError(f"CA override hits {len(hit)} events (expected 1): {o}")
        e = out[hit[0]]
        if o["action"] == "drop":
            out.pop(hit[0])
        else:
            out[hit[0]] = CA.CAEvent("", o["to_symbol"], e.series, e.ex, e.factor, e.kinds, e.subjects, e.parts)
        done.append(f"{o['ca_symbol']} {o['ex']} {o['action']}{' -> ' + o['to_symbol'] if o['action'] == 'reassign' else ''}")
    return out, done


def ca_adjust(panel: Panel, root: Optional[Path] = None) -> CAResult:
    """§4.2 adjustment path: CA split / bonus factors (if the CA file is complete), then the demerger / rights zeros.
    Never touches panel.npz: the raw panel stays what NSE published (the raw P3 probe must stay honest)."""
    raw = CA.load_raw(root if root is not None else CA_ROOT)
    res = CAResult(complete=raw is not None)
    if raw is not None:
        fac, _zero, owner = CA.events_from(raw)
        fac, res.overrides = apply_ca_overrides(fac, [o for o in load_ca_overrides()
                                                      if o.get("applies_to", "factor") == "factor"])
        res.n_records, res.n_factor = len(raw), len(fac)
        res.fixes, res.unmatched, res.beyond = apply_ca_factors(panel, fac)
        res.owner = [f"{e.ex} {e.symbol} {'/'.join(sorted(set(e.kinds)))}: {' | '.join(e.subjects)}" for e in owner]
    res.zeroed = apply_ca_zero(panel, load_ca_zero())
    return res


def p3_ca_verdict(panel: Panel, res: CAResult, cases: Sequence[Tuple[str, str, float]] = P3_CA_CASES
                  ) -> Tuple[bool, List[str]]:
    """YES iff the CA file is complete and every known case (symbol, ex-date, factor) was found in it with that
    factor, applied to a chain carrying the symbol, and leaves no split-sized move (|adjusted ex-date move| <=
    GAP_JUMP).  Booleans only: no return is printed."""
    if not res.complete:
        return False, ["P3-CA: CA file incomplete or absent -> NO"]
    lines, ok = [], True
    r = panel.ret
    for sym, d, f in cases:
        fx = [x for x in res.fixes if x.sym == sym and x.ex == d]
        found = bool(fx) and abs(fx[0].factor / f - 1) < 0.01
        applied = bool(fx) and panel.sym_at(fx[0].c, fx[0].t) == sym
        calm = bool(fx) and abs(float(r[fx[0].c, fx[0].t])) <= GAP_JUMP
        good = found and applied and calm
        ok = ok and good
        lines.append(f"P3-CA {sym} {d}: factor {f:.4f} in the CA file {found}; applied to the {sym} chain {applied} "
                     f"(on {panel.sessions[fx[0].t] if fx else '-'}, raw previous close {fx[0].prev if fx else '-'}); "
                     f"no split-sized move left {calm}: {'YES' if good else 'NO'}")
    return ok, lines


def ret_hits(panel: Panel) -> List[Tuple[int, int, str]]:
    """Gap detector on the ADJUSTED returns: rows with |return| > GAP_JUMP, labelled by the nearest CA factor."""
    r = panel.ret
    cs, ts = np.nonzero(np.abs(r) > GAP_JUMP)
    return [(int(c), int(t), factor_label(float(1 + r[c, t]))) for c, t in zip(cs, ts)]


def ca_zero_entries(panel: Panel, zero: Sequence[CA.CAEvent]) -> Tuple[List[dict], List[Tuple[str, str, str]]]:
    """Demerger / rights events -> joiner_ca_zero.json entries keyed by the ISIN the matched ROW carries on the
    ex-date row (apply_ca_zero raises on an entry that hits no row, so only located rows are written)."""
    out: Dict[Tuple[str, str], dict] = {}
    miss: List[Tuple[str, str, str]] = []
    for ev in zero:
        loc, why = ca_locate(panel, ev.isin, ev.symbol, ev.ex)
        if loc is None:
            miss.append((ev.symbol, ev.ex, why))
            continue
        c, t, _pr, _how = loc
        key = (panel.isins[panel.isin_idx[c, t]], panel.sessions[t])
        kind = "demerger" if "demerger" in ev.kinds else "rights"
        if key in out and out[key]["kind"] == "demerger":
            continue
        out[key] = dict(isin=key[0], date=key[1], kind=kind, symbol=panel.sym_at(c, t), ca_symbol=ev.symbol,
                        ca_exdate=ev.ex, subject=" | ".join(ev.subjects))
    return sorted(out.values(), key=lambda x: (x["date"], x["isin"])), miss


def factor_label(ratio: float, tol: float = 0.01) -> str:
    """Name a previous-close / prior-close (or close / previous-close) ratio by the nearest corporate-action factor."""
    for f, name in FACTOR_LABELS:
        if abs(ratio / f - 1) < tol:
            return name
    return "no standard factor"


def load_events(f: Optional[Path] = None) -> Dict[str, dict]:
    f = f if f is not None else EVENTS_FILE
    if not f.exists():
        return {}
    ev = json.loads(f.read_text())
    for isin, x in ev.items():
        if not isinstance(x, dict) or x.get("type") not in EVENT_TYPES:
            raise ValueError(f"{f}: bad event for {isin}: {x}")
    return ev


# ---------------------------------------------------------------- the score (count path: no prices, no returns)
@dataclass
class MonthSig:
    m: str
    s: int
    pop: List[int]                       # rank population, ordered by score
    universe: List[int]                  # eligible, ordered by score
    tv: Dict[int, float]
    dv: Dict[int, float]
    sym: Dict[int, str]
    score: Dict[int, int]
    filters: Dict[str, int] = field(default_factory=dict)

    @property
    def B(self) -> List[int]:
        return self.universe[:N_B]

    @property
    def P(self) -> List[int]:
        return self.universe[:N_P]


def rank_desc(chains: Sequence[int], val: Dict[int, float], sym: Dict[int, str]) -> Dict[int, int]:
    """1 = highest; NaN last; ties by symbol."""
    def key(c: int) -> Tuple[float, str]:
        v = val[c]
        return (-v if v == v else math.inf, sym[c])
    return {c: i + 1 for i, c in enumerate(sorted(chains, key=key))}


def month_signal(panel: Panel, m: str, members: Set[int], blocked: Optional[Set[int]] = None) -> MonthSig:
    """Score and universe for entry month m from data to the last session before m (index s).  Never reads a
    column after s."""
    if m not in panel.months:
        raise KeyError(f"month {m} not in the panel")
    s = panel.months[m][0] - 1
    lo = s - WIN + 1
    if lo < 0:
        raise ValueError(f"{m}: needs {WIN} sessions before it, panel has {s + 1}")
    hr = panel.has_row[:, lo:s + 1]
    any_row = hr.any(axis=1)
    last_t = lo + (WIN - 1 - np.argmax(hr[:, ::-1], axis=1))
    ci = np.arange(panel.C)
    ser = panel.series[ci, last_t]
    isin = panel.isin_idx[ci, last_t]
    symi = panel.sym_idx[ci, last_t]
    ntr = panel.traded[:, lo:s + 1].sum(axis=1)
    tvw = np.where(hr, panel.tv[:, lo:s + 1], 0.0).astype(np.float64)
    TV = np.median(tvw, axis=1)
    dcols = np.nonzero(panel.has_deliv[lo:s + 1])[0] + lo
    if len(dcols):
        dvw = np.where(panel.has_row[:, dcols], panel.dv[:, dcols], 0.0).astype(np.float64)
        known = (~np.isnan(dvw)).sum(axis=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            DV = np.where(known >= MIN_DELIV_SHARE * len(dcols), np.nanmedian(dvw, axis=1), np.nan)
    else:
        DV = np.full(panel.C, np.nan)
    ine = np.array([x.startswith("INE") for x in panel.isins] + [False], dtype=bool)[isin]   # isin -1 -> False
    eq_any = any_row & (ser == EQ) & (ntr >= 1)
    pop_mask = eq_any & ine                         # "ALL EQ stocks": INE only (ETFs / InvITs / REITs are INF / other)
    pop = [int(c) for c in np.nonzero(pop_mask)[0]]
    sym = {c: panel.syms[symi[c]] for c in pop}
    tv = {c: float(TV[c]) for c in pop}
    dv = {c: float(DV[c]) for c in pop}
    r_tv, r_dv = rank_desc(pop, tv, sym), rank_desc(pop, dv, sym)
    score = {c: max(r_tv[c], r_dv[c]) for c in pop}
    order = sorted(pop, key=lambda c: (score[c], sym[c]))
    # §5.1 "Series EQ on the signal day" (review 2026-11-23): eligibility needs an EQ row ON s, not only the latest
    # window row EQ (a merged ex-member that stopped a few sessions before s was ranked into B and then skipped).
    # The rank population keeps the builder's reading (latest window row EQ).
    ok_tr = {c for c in pop if ntr[c] >= MIN_TRADED and panel.has_row[c, s] and int(panel.series[c, s]) == EQ}
    ok_mem = {c for c in pop if c not in members}
    ok_ann = {c for c in pop if c not in (blocked or set())}
    universe = [c for c in order if c in ok_tr and c in ok_mem and c in ok_ann]
    filters = dict(eq_any_isin=int(eq_any.sum()), pop=len(pop), traded_ge_110=len(ok_tr), not_member=len(ok_mem),
                   not_announced=len(ok_ann), universe=len(universe), dv_undefined=sum(1 for c in pop if dv[c] != dv[c]),
                   deliv_sessions=len(dcols), members_in_pop=len(pop) - len(ok_mem))
    return MonthSig(m, s, order, universe, tv, dv, sym, score, filters)


def sym_chain_at(panel: Panel, s: int) -> Dict[str, int]:
    """Symbol -> chain from each chain's latest row within the score window ending at s."""
    lo = max(0, s - WIN + 1)
    hr = panel.has_row[:, lo:s + 1]
    out: Dict[str, int] = {}
    for c in np.nonzero(hr.any(axis=1))[0]:
        t = lo + int(np.nonzero(hr[c])[0][-1])
        out[panel.syms[panel.sym_idx[c, t]]] = int(c)
    return out


def gate_signals(panel: Panel, sets: Dict[str, Set[int]], months_of: Dict[int, List[str]], months: Sequence[str],
                 announced: Optional[List[dict]]) -> Dict[str, MonthSig]:
    """Month signals for question (A).  With no ANNOUNCED list, the P1 fallback drops positives at J - m <= 2."""
    out: Dict[str, MonthSig] = {}
    for m in months:
        s = panel.months[m][0] - 1
        if announced is not None:
            blocked = announced_block(announced, sym_chain_at(panel, s), panel.sessions[s], m)
        else:
            blocked = {c for c, ms in months_of.items() if 1 <= (join_distance(ms, m) or 0) <= FALLBACK_DROP}
        out[m] = month_signal(panel, m, sets.get(m, set()), blocked)
    return out


# ---------------------------------------------------------------- question (A): counts only
def auc_lower_better(pos: Sequence[float], neg: Sequence[float]) -> Optional[float]:
    """P(a positive scores lower than a negative) + 0.5 P(tie).  None if either side is empty."""
    if not pos or not neg:
        return None
    ns = sorted(neg)
    tot = 0.0
    for p in pos:
        lo, hi = bisect_left(ns, p), bisect_right(ns, p)
        tot += (len(ns) - hi) + 0.5 * (hi - lo)
    return tot / (len(pos) * len(ns))


@dataclass
class EraCounts:
    months: List[str]
    k: int
    pos_P: int = 0
    n_P: int = 0
    pos_B: int = 0
    n_B: int = 0
    pos_U: int = 0
    n_U: int = 0
    auc_U: Optional[float] = None
    auc_B: Optional[float] = None

    def prec(self, which: str) -> Optional[float]:
        n = getattr(self, f"n_{which}")
        return getattr(self, f"pos_{which}") / n if n else None


def era_counts(sigs: Dict[str, MonthSig], months_of: Dict[int, List[str]], era: Tuple[str, str], k: int,
               last_month: str) -> EraCounts:
    """Pooled over stock-months of the era's entry months whose k-month label is complete."""
    ms = [m for m in sorted(sigs) if in_window(m, era) and label_complete(m, k, last_month)]
    e = EraCounts(ms, k)
    pu: List[float] = []
    nu: List[float] = []
    pb: List[float] = []
    nb: List[float] = []
    for m in ms:
        u = sigs[m].universe
        for i, c in enumerate(u):
            y = joins_within(months_of.get(c, []), m, k)
            pct = i / len(u)
            (pu if y else nu).append(pct)
            e.pos_U += y
            e.n_U += 1
            if i < N_B:
                (pb if y else nb).append(i / min(N_B, len(u)))
                e.pos_B += y
                e.n_B += 1
            if i < N_P:
                e.pos_P += y
                e.n_P += 1
    e.auc_U, e.auc_B = auc_lower_better(pu, nu), auc_lower_better(pb, nb)
    return e


def gate_verdict(e: EraCounts) -> Tuple[str, str]:
    """(A) passes iff precision@30 (12 m) >= 25% AND >= 2x the base rate of B (= precision@150)."""
    p30, base = e.prec("P"), e.prec("B")
    if p30 is None or base is None:
        return "UNDEFINED", "no complete stock-month in the gate era -> STOP"
    if p30 >= GATE_PREC and p30 >= GATE_MULT * base:
        return "PASS", f"precision@30 {p30:.1%} >= {GATE_PREC:.0%} and >= {GATE_MULT:g} x base {base:.1%}"
    why = []
    if p30 < GATE_PREC:
        why.append(f"precision@30 {p30:.1%} < {GATE_PREC:.0%}")
    if p30 < GATE_MULT * base:
        why.append(f"precision@30 {p30:.1%} < {GATE_MULT:g} x base {base:.1%}")
    return "FAIL", "; ".join(why) + " -> STOP (§2)"


def wave_recall(sigs: Dict[str, MonthSig], waves: Dict[str, Set[int]], offsets: Sequence[int] = (12, 6)
                ) -> List[Tuple[str, int, Dict[int, Tuple[int, int, int, int]]]]:
    """Per join wave J: {offset: (n joiners with a month signal, in U, in B, in P)} at m = J - offset."""
    out = []
    for J in sorted(waves):
        if not waves[J]:
            continue
        row: Dict[int, Tuple[int, int, int, int]] = {}
        for o in offsets:
            m = madd(J, -o)
            sg = sigs.get(m)
            if sg is None:
                continue
            U, B, P = set(sg.universe), set(sg.B), set(sg.P)
            js = waves[J]
            row[o] = (len(js), len(js & U), len(js & B), len(js & P))
        out.append((J, len(waves[J]), row))
    return out


# ---------------------------------------------------------------- arms (return path)
def window_rows(panel: Panel, c: int, lo: int, s: int) -> np.ndarray:
    return lo + np.nonzero(panel.has_row[c, lo:s + 1])[0]


def atr_pct(panel: Panel, c: int, s: int) -> Optional[float]:
    """Wilder ATR(14) on the window's rows back-adjusted to the basis of the latest row (split invariant), % of close."""
    rows = window_rows(panel, c, max(0, s - WIN + 1), s)
    if len(rows) < ATR_N + 1:
        return None
    G = panel.G
    L = rows[-1]
    bars = []
    for t in rows:
        k = G[c, t] / G[c, L] * panel.close[c, L] / panel.close[c, t]
        bars.append(dict(h=panel.high[c, t] * k, l=panel.low[c, t] * k, c=panel.close[c, t] * k))
    a = F.N.wilder_atr(bars, ATR_N)[-1]
    if a is None or not bars[-1]["c"] > 0:
        return None
    return a / bars[-1]["c"] * 100


def mom_12_1(panel: Panel, c: int, m: str) -> Optional[float]:
    """12-1 momentum known at the signal day of m: price at the end of m-2 over price at the end of m-13."""
    a, b = madd(m, -13), madd(m, -2)
    if a not in panel.months or b not in panel.months:
        return None
    ta, tb = panel.months[a][1], panel.months[b][1]
    if panel.last_row_at(c, ta) < 0:
        return None
    G = panel.G
    return float(G[c, tb] / G[c, ta] - 1)


def tv_growth(panel: Panel, c: int, s: int, tv_now: float) -> Optional[float]:
    lo = s - GROW_LAG - WIN + 1
    if lo < 0:
        return None
    hr = panel.has_row[c, lo:s - GROW_LAG + 1]
    prev = float(np.median(np.where(hr, panel.tv[c, lo:s - GROW_LAG + 1], 0.0)))
    return tv_now / prev if prev > 0 else None


def top_by(chains: Iterable[int], val: Dict[int, Optional[float]], sym: Dict[int, str], n: int,
           desc: bool = True) -> List[int]:
    """n chains with the highest (desc) value; undefined values never chosen; ties by symbol."""
    ok = [c for c in chains if val.get(c) is not None and val[c] == val[c]]
    return sorted(ok, key=lambda c: ((-val[c] if desc else val[c]), sym[c]))[:n]  # type: ignore[operator]


ARMS = ("P", "MOM", "EWB", "GROW", "VOL", "FOTAIL", "TOP5")


def month_arms(panel: Panel, sig: MonthSig, members: Set[int]) -> Dict[str, List[int]]:
    B = sig.B
    mom = {c: mom_12_1(panel, c, sig.m) for c in B}
    grow = {c: tv_growth(panel, c, sig.s, sig.tv[c]) for c in B}
    vol = {c: atr_pct(panel, c, sig.s) for c in B}
    fo = [c for c in sig.pop if c in members]
    return dict(P=sig.P, MOM=top_by(B, mom, sig.sym, N_P), EWB=list(B), GROW=top_by(B, grow, sig.sym, N_P),
                VOL=top_by(B, vol, sig.sym, N_P), FOTAIL=top_by(fo, {c: sig.tv[c] for c in fo}, sig.sym, N_P,
                                                                desc=False),
                TOP5=sig.universe[:N_TOP5])


# ---------------------------------------------------------------- one position
@dataclass
class Path_:
    c: int
    status: str            # held / merged / last_close / dead / pending / open / skipped
    e: int = -1            # entry session
    x0: int = -1           # scheduled exit
    x: int = -1            # exit session, or the last traded close (merged / dead / pending), or S-1 (open)
    d: int = -1            # first missing session of the death gap (dead / pending)
    tries: int = 0         # entry deferrals
    defer_exit: int = 0
    be: bool = False       # traded as BE / BZ during the hold
    swapped: bool = False  # continued in the acquirer (events file `into_isin`)
    g: Optional[np.ndarray] = None   # gross value relative to the entry close, sessions e..x (inclusive)

    def end(self, dead_zero: bool) -> int:
        """Last session on which the position's value can still change (open: never ends inside the panel)."""
        if self.status == "open":
            return 10 ** 9
        if self.status in ("dead", "pending") and dead_zero:
            return self.d
        return self.x


def _locked_up(panel: Panel, c: int, t: int) -> bool:
    return bool(panel.close[c, t] == panel.high[c, t] and panel.ret[c, t] >= LOCK_PCT)


def _locked_down(panel: Panel, c: int, t: int) -> bool:
    return bool(panel.close[c, t] == panel.low[c, t] and panel.ret[c, t] <= -LOCK_PCT)


def name_path(panel: Panel, c: int, e0: int, hold: int, events: Dict[str, dict]) -> Path_:
    """§3 / §4.3: deferred entry, fixed hold, deferred exit, stopped-name classification."""
    S = panel.S
    e, tries = e0, 0
    while True:
        if e >= S:
            return Path_(c, "skipped", tries=tries)
        if panel.has_row[c, e] and not _locked_up(panel, c, e):
            break
        tries += 1
        if tries > LOCK_TRIES:
            return Path_(c, "skipped", tries=tries)
        e += 1
    x0 = e + hold
    G = panel.G
    cur, base_t, base_v = c, e, 1.0          # value(t) = base_v * G[cur, t] / G[cur, base_t]
    vals = [1.0]                             # sessions e, e+1, ...
    status, x, d, last, be, swapped = "held", -1, -1, e, False, False
    t = e + 1
    while True:
        if t >= S:                           # the panel ends inside the hold (or inside an exit deferral)
            status, x = "open", S - 1
            break
        if panel.has_row[cur, t]:
            last = t
            be = be or int(panel.series[cur, t]) in (SERIES["BE"], SERIES["BZ"])
        else:
            end = min(S, t + DEAD_GAP)
            if not panel.has_row[cur, t:end].any():
                ev = events.get(panel.isins[panel.isin_idx[cur, last]], {})
                into = ev.get("into_isin")
                a = panel.chain_of_isin(into, last) if ev.get("type") in MERGE_TYPES and into else None
                if a is not None and panel.last_row_at(a, last) >= 0:
                    base_v = base_v * G[cur, last] / G[cur, base_t]
                    cur, base_t, swapped = a, last, True
                    last = panel.last_row_at(a, last)
                    continue                 # re-read session t on the acquirer
                if ev.get("type") in LAST_CLOSE_TYPES:
                    status, x = ("last_close" if ev["type"] == "last_close" else "merged"), last
                else:                        # unmapped, suspension, compulsory delisting
                    status, x, d = ("pending" if end - t < DEAD_GAP else "dead"), last, t
                break
        vals.append(base_v * G[cur, t] / G[cur, base_t])
        if t >= x0 and panel.has_row[cur, t] and not _locked_down(panel, cur, t):
            x = t
            break
        t += 1
    g = np.array(vals[:x - e + 1])
    return Path_(c, status, e, x0, x, d, tries, max(0, x - x0) if status == "held" else 0, be, swapped, g)


def net_at(p: Path_, b: int, cost: float, dead_zero: bool) -> float:
    """Net value of one position at session b (1.0 = the money set aside at the cohort's first session).
    Entry cost at the entry close, exit cost at the exit close; open positions are marked without the exit cost;
    a dead name is held at its last close until the gap starts, then 0 (read) or sold at its last close (table 2)."""
    if b < p.e:
        return 1.0
    assert p.g is not None
    if p.status == "open":
        return (1 - cost) * float(p.g[min(b, p.x) - p.e])
    if p.status in ("dead", "pending") and dead_zero:
        return 0.0 if b >= p.d else (1 - cost) * float(p.g[min(b, p.x) - p.e])
    if b < p.x:
        return (1 - cost) * float(p.g[b - p.e])
    return (1 - cost) ** 2 * float(p.g[p.x - p.e])


def hold_return(p: Path_, cost: float, dead_zero: bool) -> Optional[float]:
    """Net return of a finished position; None for skipped / still open."""
    if p.status in ("skipped", "open"):
        return None
    if p.status in ("dead", "pending") and dead_zero:
        return -1.0
    assert p.g is not None
    return (1 - cost) ** 2 * float(p.g[-1]) - 1


# ---------------------------------------------------------------- cohorts and the calendar-time portfolio
def cohort_returns(paths: Sequence[Path_], bounds: Sequence[Tuple[str, int]], cost: float, dead_zero: bool
                   ) -> Dict[str, float]:
    """{return month: cohort return}.  bounds = [(month, last session index)] from the entry month on.  Live through
    the month of the last position's end (an open position keeps it live to the last bound)."""
    ps = [p for p in paths if p.status != "skipped"]
    if not ps:
        return {}
    end = max(p.end(dead_zero) for p in ps)
    out: Dict[str, float] = {}
    prev = 1.0
    for i, (T, b) in enumerate(bounds):
        if i > 0 and bounds[i - 1][1] >= end:
            break
        if prev <= 0:
            break                                        # everything already lost: nothing left to return
        v = sum(net_at(p, b, cost, dead_zero) for p in ps) / len(ps)
        out[T] = v / prev - 1
        prev = v
    return out


def month_no(m: str) -> int:
    return int(m[:4]) * 12 + int(m[5:7]) - 1


def hold_months(hold: int) -> int:
    """Sleeves of the calendar-time portfolio: 63 -> 3, 126 -> 6, 252 -> 12."""
    return max(1, round(hold / SESS_PER_MONTH))


def calendar_time(cohorts: Dict[str, Dict[str, float]], sleeves: int) -> Dict[str, Tuple[float, int]]:
    """{month: (mean over live sleeves, number of live sleeves)}.  The money runs in `sleeves` equal sleeves; cohort m
    belongs to sleeve month_no(m) % sleeves, so a sleeve rolls from one cohort into the cohort `sleeves` months later.
    In the roll month the sleeve's return compounds the exiting cohort's tail and the entering cohort's first month
    (126 sessions are not exactly 6 months, so the two can overlap by a few sessions: an approximation, stated).
    Gives the design's "6 live cohorts" for the 6-month hold instead of 7 (the exiting cohort no longer counts as a
    full-weight cohort for its last session or two)."""
    acc: Dict[str, Dict[int, float]] = defaultdict(dict)
    for m, rets in cohorts.items():
        k = month_no(m) % sleeves
        for T, r in rets.items():
            acc[T][k] = (1 + acc[T].get(k, 0.0)) * (1 + r) - 1
    return {T: (sum(v.values()) / len(v), len(v)) for T, v in sorted(acc.items())}


def bounds_from(panel: Panel, m: str, last_month: str) -> List[Tuple[str, int]]:
    return [(T, panel.months[T][1]) for T in month_range(m, last_month) if T in panel.months]


def window_mean(ct: Dict[str, Tuple[float, int]], w: Tuple[str, str]) -> Optional[float]:
    v = [r for T, (r, _n) in ct.items() if in_window(T, w)]
    return sum(v) / len(v) if v else None


def diff_series(a: Dict[str, Tuple[float, int]], b: Dict[str, Tuple[float, int]], w: Tuple[str, str]) -> List[float]:
    """Paired monthly differences a - b over the months both arms have, in month order."""
    return [a[T][0] - b[T][0] for T in sorted(a) if T in b and in_window(T, w)]


def mean_diff(a: Dict[str, Tuple[float, int]], b: Dict[str, Tuple[float, int]], w: Tuple[str, str]
              ) -> Optional[float]:
    v = diff_series(a, b, w)
    return sum(v) / len(v) if v else None


def block_ci(vals: List[float]) -> Tuple[float, float]:
    """Moving-block bootstrap of the mean (monthly_orb.mbb_ci), block = hold months, Bonferroni level."""
    return MO.mbb_ci(vals, block=BLOCK, n=BOOT_DRAWS, seed=SEED, level=CI_LEVEL)


def max_dd_pct(ct: Dict[str, Tuple[float, int]], w: Tuple[str, str]) -> float:
    eq, series = 1.0, []
    for T in sorted(ct):
        if in_window(T, w):
            eq *= 1 + ct[T][0]
            series.append((T, eq))
    return float(CMP.drawdown_vs_peak(series, start=1.0)["pct"])


def crand_means(B_paths: Dict[str, List[Path_]], bounds: Dict[str, List[Tuple[str, int]]], cost: float,
                w: Tuple[str, str], sleeves: int, draws: int = CRAND_DRAWS, k: Optional[int] = None,
                seed: int = SEED) -> List[float]:
    """C-RAND: per draw, a random k-subset of B in every cohort (seed 7), aggregated exactly as calendar_time;
    returns the sorted calendar-time means over window w, one per draw."""
    k = N_P if k is None else k
    rng = np.random.default_rng(seed)
    months = sorted({T for m in bounds for T, _ in bounds[m] if in_window(T, w)})
    col = {T: i for i, T in enumerate(months)}
    grow = np.ones((draws, len(months), sleeves))           # 1 + sleeve return
    live_s = np.zeros((draws, len(months), sleeves), bool)
    for m in sorted(B_paths):
        ps = [p for p in B_paths[m] if p.status != "skipped"]
        if not ps:
            continue
        bd = bounds[m]
        vals = np.array([[net_at(p, b, cost, True) for _, b in bd] for p in ps])
        ends = np.array([p.end(True) for p in ps])
        kk = min(k, len(ps))
        pick = np.argsort(rng.random((draws, len(ps))), axis=1)[:, :kk]
        v = vals[pick].mean(axis=1)                                     # draws x bounds
        end = ends[pick].max(axis=1)
        prev = np.ones(draws)
        for i, (T, b) in enumerate(bd):
            live = (end > bd[i - 1][1]) if i > 0 else np.ones(draws, bool)
            live &= prev > 0
            r = np.where(live, v[:, i] / np.where(prev > 0, prev, 1) - 1, 0.0)
            if T in col:
                sl = month_no(m) % sleeves
                grow[:, col[T], sl] *= np.where(live, 1 + r, 1.0)
                live_s[:, col[T], sl] |= live
            prev = np.where(live, v[:, i], prev)
    n_live = live_s.sum(axis=2)
    with np.errstate(invalid="ignore"):
        monthly = np.where(n_live > 0, ((grow - 1) * live_s).sum(axis=2) / np.where(n_live > 0, n_live, 1), np.nan)
    return sorted(float(np.nanmean(row)) for row in monthly)


def pctl(sorted_vals: Sequence[float], p: float) -> float:
    i = min(len(sorted_vals) - 1, max(0, int(math.ceil(p / 100 * len(sorted_vals))) - 1))
    return sorted_vals[i]


# ---------------------------------------------------------------- read rule (§5.5)
@dataclass
class ArmStats:
    d_mom_all: Optional[float]
    ci_lo: float
    ci_hi: float
    d_mom_oos: Optional[float]
    d_ewb_is: Optional[float]
    d_ewb_oos: Optional[float]
    mean_all: Optional[float]
    crand_p95: float


def read_rule(a: ArmStats) -> Tuple[bool, List[str]]:
    def pos(x: Optional[float]) -> bool:
        return x is not None and x > 0
    checks = [("(a) whole: X - MOM > 0 with 98.33% CI low > 0", pos(a.d_mom_all) and a.ci_lo > 0),
              ("(b) OOS: X - MOM > 0", pos(a.d_mom_oos)),
              ("(c) X - EW-B > 0 in IS and in OOS", pos(a.d_ewb_is) and pos(a.d_ewb_oos)),
              ("(d) X above the 95th percentile of C-RAND",
               a.mean_all is not None and a.mean_all > a.crand_p95)]
    return all(ok for _, ok in checks), [f"{'ok  ' if ok else 'FAIL'} {name}" for name, ok in checks]


def candidate(gate_pass: bool, holds: Dict[str, bool], dd_oos: Dict[str, float], dd_mom_oos: float
              ) -> Tuple[bool, str]:
    """CANDIDATE iff (A) passed AND an arm holds AND that arm's OOS max drawdown is not worse than MOM's."""
    if not gate_pass:
        return False, "(A) did not pass"
    ok = [a for a, h in holds.items() if h and dd_oos[a] <= dd_mom_oos]
    if ok:
        return True, f"CANDIDATE via {', '.join(ok)}"
    if any(holds.values()):
        return False, "an arm holds but its OOS max drawdown is worse than MOM's"
    return False, "not shown"


# ---------------------------------------------------------------- tax (report only)
def post_tax_equity(ct: Dict[str, Tuple[float, int]], start: float = CAPITAL) -> Tuple[float, float, List[dict]]:
    """Rs view: compound the monthly series; STCG per FY on net FY P&L (losses carried CAP_LOSS_CARRY FYs via
    compounding.apply_tax), tax paid at the FY end.  Returns (pre-tax final, post-tax final, FY rows)."""
    pre = post = start
    fy_pnl: List[Tuple[int, float]] = []
    rows: List[dict] = []
    cur_fy, fy_start = None, post
    months = sorted(ct)
    for i, T in enumerate(months):
        fy = CMP.fy_of(f"{T}-15")
        if cur_fy is None:
            cur_fy, fy_start = fy, post
        r = ct[T][0]
        pre *= 1 + r
        post *= 1 + r
        nxt = CMP.fy_of(f"{months[i + 1]}-15") if i + 1 < len(months) else None
        if nxt != fy:
            fy_pnl.append((fy, post - fy_start))
            rec = CMP.apply_tax(fy_pnl, 0.0, CAP_LOSS_CARRY)[-1]
            tax = rec["taxable"] * stcg_rate(fy)
            post -= tax
            rows.append(dict(fy=fy, pnl=post + tax - fy_start, taxable=rec["taxable"], tax=tax, equity=post))
            cur_fy, fy_start = nxt, post
    return pre, post, rows


# ---------------------------------------------------------------- output
class Out:
    def __init__(self, name: str):
        self.name, self.lines = name, []  # type: str, List[str]

    def __call__(self, *a: object) -> None:
        s = " ".join(str(x) for x in a)
        print(s, flush=True)
        self.lines.append(s)

    def file_only(self, s: str) -> None:
        self.lines.append(s)

    def save(self) -> None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / f"joiner_positional_{self.name}.txt").write_text("\n".join(self.lines) + "\n", encoding="utf-8")


def fmt_p(x: Optional[float]) -> str:
    return "  n/a" if x is None else f"{x:6.1%}"


# ---------------------------------------------------------------- build (data job)
def download_complete(log: Path = NF.LOG) -> Tuple[bool, str]:
    """The fetcher is finished iff the last non-empty line of its log says RUN COMPLETE."""
    if not log.exists():
        return False, f"no fetch log at {log}"
    tail = [x for x in log.read_text(encoding="utf-8", errors="replace").splitlines() if x.strip()][-1:]
    if tail and "RUN COMPLETE" in tail[0]:
        return True, tail[0]
    return False, f"last log line: {tail[0] if tail else '(empty)'}"


def build() -> None:
    out = Out("build")
    done, why = download_complete()
    if not done:
        raise SystemExit(f"download not complete ({why}); build refuses a partial panel")
    man = json.loads(NF.MANIFEST.read_text())
    st_of: Dict[str, Dict[str, str]] = {k: {d: v[0] for d, v in man.get(k, {}).items()} for k in NF.TYPES}
    cm_ok = sorted([(d, k) for k in ("cm_old", "cm_udiff") for d, s in st_of[k].items() if s == "ok"])
    bad = sorted((d, k, s) for k in ("cm_old", "cm_udiff") for d, s in st_of[k].items()
                 if s in ("gap", "failed", "wrongdate"))
    out(f"CM sessions ok {len(cm_ok)}; CM gap/failed/wrongdate {len(bad)}: {bad[:20]}")
    if len(bad) > MAX_CM_GAPS:
        raise SystemExit(f"{len(bad)} missing CM sessions > {MAX_CM_GAPS}: fix the download first")
    rmap = json.loads(V.MAP_FILE.read_text())
    b = PanelBuilder(rmap)
    overlap: Counter = Counter()
    src: Counter = Counter()
    for i, (d, k) in enumerate(cm_ok):
        rows = parse_cm(NF._text(k, NF.path(k, d).read_bytes()))
        full = parse_full(NF._text("full", NF.path("full", d).read_bytes())) if st_of["full"].get(d) == "ok" else None
        mto = parse_mto(NF._text("mto", NF.path("mto", d).read_bytes())) if st_of["mto"].get(d) == "ok" else None
        if full is not None and mto is not None:
            for key, q in mto.items():
                if key[1] == "EQ" and key in full:
                    overlap["equal" if full[key] == q else "differ"] += 1
        deliv = full if full is not None else mto
        src["full" if full is not None else "mto" if mto is not None else "none"] += 1
        b.add_day(d, rows, deliv)
        if i % 250 == 0:
            print(f"  {d} chains {len(b.linker.last_t)}", flush=True)
    p = b.panel()
    p.save(PANEL_FILE)
    kinds = Counter(x[2] for x in p.links)
    info = dict(sessions=p.S, first=p.sessions[0], last=p.sessions[-1], chains=p.C, links=dict(kinds),
                delivery_source=dict(src), full_vs_mto_EQ=dict(overlap), deliv_row_missing=b.deliv_missing,
                deliv_t2t_full=b.deliv_t2t,
                dupes=b.linker.dupes, cm_missing=bad)
    BUILD_INFO.write_text(json.dumps(info, indent=1))
    out(json.dumps(info))
    out.save()


# ---------------------------------------------------------------- probes (counts only)
def prevclose_census(panel: Panel) -> Tuple[List[Tuple[int, int, float]], int]:
    """([(chain, t, previous close / prior row's close)] wherever they differ by more than ADJ_TOL, rows compared)."""
    idx = np.where(panel.has_row, np.arange(panel.S)[None, :], -1)
    li = np.maximum.accumulate(idx, axis=1)
    prior = np.full_like(li, -1)
    prior[:, 1:] = li[:, :-1]
    rows = panel.has_row & (prior >= 0)
    cs, ts = np.nonzero(rows)
    pc = panel.close[cs, prior[cs, ts]]
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = panel.prev[cs, ts] / pc
    hit = np.abs(ratio - 1) > ADJ_TOL
    return [(int(c), int(t), float(r)) for c, t, r in zip(cs[hit], ts[hit], ratio[hit])], int(len(cs))


def p3_verdict(panel: Panel, census: List[Tuple[int, int, float]], split: Tuple[str, str, float] = P3_SPLIT,
               search: int = 5) -> Tuple[bool, List[str]]:
    """YES iff the known split shows an adjusted previous close on ONE chain (the recalled ex-date +-`search`
    sessions: the date is from memory; the session found is printed), and at least one small-bonus factor appears
    among the adjustments (the named cases are listed for a by-hand check)."""
    lines = []
    sym, d, k = split
    ok_split = False
    t0 = bisect_left(panel.sessions, d)
    hits = [(c, t, r) for c, t, r in census if abs(t - t0) <= search and abs(r * k - 1) < 0.02
            and panel.sym_at(c, t) == sym]
    if hits:
        c, t, r = hits[0]
        pr = panel.last_row_at(c, t - 1)
        ok_split = True
        lines.append(f"P3 split {sym}: previous close adjusted by 1/{k:g} on {panel.sessions[t]} (recalled {d}); "
                     f"same chain carries {panel.sym_at(c, pr)} {panel.isins[panel.isin_idx[c, pr]]} on "
                     f"{panel.sessions[pr]} and {panel.isins[panel.isin_idx[c, t]]} after: YES")
    else:
        lines.append(f"P3 split {sym} around {d}: no previous close adjusted by 1/{k:g} on one chain within "
                     f"+-{search} sessions (unadjusted, or the identity chain broke): NO")
    bonus = {name: [(c, t) for c, t, r in census if abs(r / f - 1) < ADJ_TOL] for name, f in BONUS_FACTORS.items()}
    ok_bonus = bool(bonus["1:10 bonus"] or bonus["1:5 bonus"])
    for name, xs in bonus.items():
        ex = ", ".join(f"{panel.sym_at(c, t)} {panel.sessions[t]}" for c, t in xs[:8])
        lines.append(f"P3 {name} factor seen {len(xs)} times; first: {ex}")
    return ok_split and ok_bonus, lines


def gap_hits(panel: Panel) -> List[Tuple[int, int, str]]:
    """§4.2 gap detector by name: (chain, session, nearest corporate-action factor of close / previous close)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = panel.close / panel.prev
    cs, ts = np.nonzero(np.abs(np.nan_to_num(ratio, nan=1.0) - 1) > GAP_JUMP)
    return [(int(c), int(t), factor_label(float(ratio[c, t]))) for c, t in zip(cs, ts)]


def gap_jumps(panel: Panel) -> int:
    """§4.2 cross-check: rows whose close / previous close moves more than GAP_JUMP (a COUNT)."""
    with np.errstate(invalid="ignore", divide="ignore"):
        r = panel.close / panel.prev - 1
    return int((np.abs(np.nan_to_num(r)) > GAP_JUMP).sum())


def load_context(panel: Panel) -> Tuple[Dict[str, Set[int]], Dict[str, List[str]], Dict[int, List[str]], str]:
    mem = V.load_membership()
    sets, unmapped = member_chains(panel, mem)
    return sets, unmapped, member_months_of(sets), max(mem)


def entry_months(panel: Panel, last: str) -> List[str]:
    return [m for m in month_range(FIRST_ENTRY, last) if m in panel.months and panel.months[m][0] - WIN >= 0]


def probes() -> None:
    out = Out("probes")
    p = Panel.load()
    info = json.loads(BUILD_INFO.read_text()) if BUILD_INFO.exists() else {}
    out(f"panel {p.S} sessions {p.sessions[0]}..{p.sessions[-1]}, {p.C} chains")
    out(f"links: {info.get('links')}; ISIN dupes {info.get('dupes')}; CM missing {info.get('cm_missing')}")
    ann = load_announced()
    out(f"P1 ANNOUNCED list: {'%d entries' % len(ann) if ann is not None else 'ABSENT -> fallback: (A) drops positives at J - m <= 2; (B) states the bias'}")
    out(f"P2 delivery source per session: {info.get('delivery_source')}; full vs MTO EQ overlap {info.get('full_vs_mto_EQ')}; "
        f"traded rows missing from the delivery file {info.get('deliv_row_missing')}")
    by_y = Counter(d[:4] for d in p.sessions)
    dl_y = Counter(d[:4] for d, h in zip(p.sessions, p.has_deliv) if h)
    out("P2 sessions / with delivery, per year: " + ", ".join(f"{y} {by_y[y]}/{dl_y[y]}" for y in sorted(by_y)))
    census, n_cmp = prevclose_census(p)
    ok3, lines = p3_verdict(p, census)
    out(f"P3 previous close differs from the prior close on {len(census)} of {n_cmp} rows")
    for x in lines:
        out("  " + x)
    out(f"P3 verdict (raw bhavcopy previous close): {'YES' if ok3 else 'NO'}  (NO -> design §4.2: the CA file is the "
        f"primary source)")
    out(f"P3 gap detector: rows with a close / previous close move > {GAP_JUMP:.0%}: {gap_jumps(p)} (count only)")
    raw_hits = {(c, t) for c, t, lab in gap_hits(p) if lab != "no standard factor"}
    res = ca_adjust(p)
    if not res.complete:
        out(f"CA file: INCOMPLETE or absent at {CA_ROOT} -> the return commands refuse")
    else:
        prev_cls = Counter(x.prev for x in res.fixes)
        out(f"CA file: {res.n_records} records; equity split / bonus events {res.n_factor}; applied to rows "
            f"{len(res.fixes)} (by isin {sum(x.how == 'isin' for x in res.fixes)}, by symbol "
            f"{sum(x.how == 'symbol' for x in res.fixes)}); ex-date after the panel {res.beyond}; unmatched "
            f"{len(res.unmatched)} by reason {dict(Counter(w.split(':')[0] for _s, _d, w in res.unmatched))}")
        out(f"CA cross-check, bhavcopy previous close on the applied rows: {dict(prev_cls)} (adjusted = NSE had "
            f"already adjusted it; never adjusted twice: the base is the chain's prior close)")
        out(f"CA overrides applied ({CA_OVERRIDE_FILE.name}): {len(res.overrides)} {res.overrides}")
        far = [x for x in res.fixes if x.t - bisect_left(p.sessions, x.ex) > CA_SEARCH]
        out(f"CA applied to the first row back after a trading pause > {CA_SEARCH} sessions: {len(far)}: "
            + "; ".join(f"{x.sym} {x.ex} -> {p.sessions[x.t]}" for x in far))
        absent = [x for x in res.fixes
                  if abs(p.close[x.c, x.t] / p.close[x.c, x.pr] - 1) < abs(p.close[x.c, x.t] / p.close[x.c, x.pr] - x.factor)]
        out(f"CA applied rows whose raw step is nearer 1 than the factor (step absent: misfiled record, or a real move "
            f"masks it; by hand): {len(absent)}: " + "; ".join(f"{x.sym} {x.ex} x{x.factor:.4f}" for x in absent))
        on_row = {(x.c, x.t) for x in res.fixes}
        std = [(c, t) for c, t, r in census if factor_label(r) != "no standard factor" and (c, t) not in on_row]
        dbl = [(c, t) for c, t in std if any(x.c == c and abs(x.t - t) <= CA_SEARCH for x in res.fixes)]
        out(f"previous close already at a standard factor with no CA row on it (NSE adjusted, no CA record: the raw "
            f"previous close carries it): {len(std)}: " + "; ".join(f"{p.sym_at(c, t)} {p.sessions[t]}" for c, t in std)
            + f"; of them with a CA row on the same chain within {CA_SEARCH} sessions (double-adjust risk): {len(dbl)}: "
            + "; ".join(f"{p.sym_at(c, t)} {p.sessions[t]}" for c, t in dbl))
        out.file_only("CA unmatched: " + "; ".join(f"{a} {b} ({w})" for a, b, w in res.unmatched))
        out.file_only("CA applied rows with a previous close neither raw nor adjusted: " + "; ".join(
            f"{x.sym} {x.ex}" for x in res.fixes if x.prev == "other"))
    out(f"§4.2 demerger / rights ex-date returns set to 0 ({CA_ZERO_FILE.name}): {len(res.zeroed)}")
    ok_ca, ca_lines = p3_ca_verdict(p, res)
    for x in ca_lines:
        out("  " + x)
    out(f"P3 verdict (CA path): {'YES' if ok_ca else 'NO'}  (NO -> the return commands refuse)")
    if res.complete:
        fix_rows = {(x.c, x.t) for x in res.fixes}
        loud = [x for x in res.fixes if abs(float(p.ret[x.c, x.t])) > GAP_JUMP]
        out(f"CA applied rows still moving > {GAP_JUMP:.0%} after the factor (wrong date or factor? by hand): "
            f"{len(loud)}: " + "; ".join(f"{x.sym} {x.ex}" for x in loud))
        left = [(c, t, lab) for c, t, lab in ret_hits(p) if lab != "no standard factor"]
        out(f"gap detector at a standard factor: raw {len(raw_hits)}; after the CA path {len(left)} (of them on a CA "
            f"row {sum((c, t) in fix_rows for c, t, _l in left)}; no CA event: by hand): "
            + "; ".join(f"{p.sym_at(c, t)} {p.sessions[t]} {lab}" for c, t, lab in left))
        out(f"CA owner list (unparsed / capital reduction / scheme / bonus debentures; no factor applied): "
            f"{len(res.owner)}")
        for x in res.owner:
            out("  OWNER " + x)
    out("P4 AMFI market-cap lists: not on disk (descriptive only; the frozen score does not use market cap)")
    sets, unmapped, months_of, last = load_context(p)
    tot = sum(len(v) for v in unmapped.values())
    out(f"P6 F&O member symbols not matched to a CM chain: {tot} over {len(unmapped)} months; "
        f"{dict((m, v) for m, v in unmapped.items() if v)}")
    fo = set().union(*sets.values()) if sets else set()
    rng = random.Random(SEED)
    pool = [(c, t, r) for c, t, r in census if c not in fo]
    sample = rng.sample(pool, min(CA_SAMPLE, len(pool)))
    if res.complete:
        pool_ca = [x for x in res.fixes if x.c not in fo]
        smp = random.Random(SEED).sample(pool_ca, min(CA_SAMPLE, len(pool_ca)))
        out(f"§4.2 by-hand sample: {len(smp)} CA adjustments on never-F&O chains (seed {SEED}, from {len(pool_ca)}): "
            + "; ".join(f"{p.sym_at(x.c, x.t)} {p.sessions[x.t]} x{x.factor:.4f}" for x in smp))
    else:
        out(f"§4.2 by-hand sample: {len(sample)} adjustments on never-F&O chains (seed {SEED}; no CA file, so drawn "
            f"from the previous-close census): " + "; ".join(f"{p.sym_at(c, t)} {p.sessions[t]} {factor_label(r)}"
                                                             for c, t, r in sample))
    out(f"§4.2 census factors: {dict(Counter(factor_label(r) for _c, _t, r in census))}")
    hits = gap_hits(p)
    split_like = [(c, t, lab) for c, t, lab in hits if lab != "no standard factor"]
    out(f"§4.2 gap detector hits by name (full list in the file): {len(hits)}; at a standard split / bonus factor "
        f"(an UNADJUSTED corporate action?): {len(split_like)}: "
        + "; ".join(f"{p.sym_at(c, t)} {p.sessions[t]} {lab}" for c, t, lab in split_like))
    out.file_only("§4.2 all gap detector hits (demergers / rights go to %s): " % CA_ZERO_FILE.name
                  + "; ".join(f"{p.sym_at(c, t)} {p.sessions[t]} {lab}" for c, t, lab in hits))
    out("P7 per entry month: EQ any ISIN / rank pool (INE) / traded>=110 / not member / universe / |B| / |P| / "
        "members / DV undefined / delivery sessions in window")
    for m in entry_months(p, last):
        sg = month_signal(p, m, sets.get(m, set()))
        f = sg.filters
        out(f"  {m} {f['eq_any_isin']:5d} {f['pop']:5d} {f['traded_ge_110']:5d} {f['not_member']:5d} {f['universe']:5d} "
            f"{len(sg.B):4d} {len(sg.P):3d} {len(sets.get(m, ())):4d} {f['dv_undefined']:4d} {f['deliv_sessions']:4d}")
    out.save()


def gate() -> None:
    out = Out("gate")
    p = Panel.load()
    sets, unmapped, months_of, last = load_context(p)
    ann = load_announced()
    ms = entry_months(p, last)
    sigs = gate_signals(p, sets, months_of, ms, ann)
    out(f"(A) entry months {ms[0]}..{ms[-1]}; last membership month {last}; "
        f"ANNOUNCED {'file' if ann is not None else 'ABSENT: P1 fallback, positives at J - m <= 2 dropped'}")
    out("labels: joins within k iff the first member month J after m has J <= m + k - 1; complete iff membership "
        "reaches m + k - 1 (12 m: entries <= %s; 6 m: <= %s)" % (madd(last, -11), madd(last, -5)))
    verdict = None
    for name, a, b in ERAS:
        for k in LABEL_K:
            e = era_counts(sigs, months_of, (a, b), k, last)
            out(f"{name:20s} k={k:2d} months {len(e.months):2d}  prec@30 {fmt_p(e.prec('P'))} ({e.pos_P}/{e.n_P})  "
                f"prec@150 {fmt_p(e.prec('B'))} ({e.pos_B}/{e.n_B})  base U {fmt_p(e.prec('U'))} ({e.pos_U}/{e.n_U})  "
                f"AUC U {e.auc_U if e.auc_U is None else round(e.auc_U, 3)}  AUC B "
                f"{e.auc_B if e.auc_B is None else round(e.auc_B, 3)}")
            if (a, b) == GATE_ERA and k == GATE_K:
                verdict = gate_verdict(e)
    out("recall per join wave: J, joiners, then per offset (12, 6): with a signal / in U / in B / in P")
    for J, n, row in wave_recall(sigs, join_waves(sets)):
        out(f"  {J} {n:3d}  " + "  ".join(f"-{o}: {row[o][0]}/{row[o][1]}/{row[o][2]}/{row[o][3]}" for o in sorted(row,
                                                                                                         reverse=True)))
    out("per month P / B positives (12 m, complete labels only):")
    for m in ms:
        if label_complete(m, GATE_K, last):
            sg = sigs[m]
            out(f"  {m} P {sum(joins_within(months_of.get(c, []), m, GATE_K) for c in sg.P):2d}/{len(sg.P)}  "
                f"B {sum(joins_within(months_of.get(c, []), m, GATE_K) for c in sg.B):3d}/{len(sg.B)}")
    assert verdict is not None
    out(f"(A) GATE on {GATE_ERA[0]}..{GATE_ERA[1]} entries, 12 m: {verdict[0]} -- {verdict[1]}")
    out.save()


def stop_scan(panel: Panel, c: int, e: int, horizon: int) -> Tuple[str, int, int]:
    """Count-path classifier: ('continues' | 'stopped' | 'pending', last row, first missing session)."""
    last = e
    for t in range(e + 1, min(panel.S, e + horizon + 1)):
        if panel.has_row[c, t]:
            last = t
            continue
        end = min(panel.S, t + DEAD_GAP)
        if not panel.has_row[c, t:end].any():
            return ("pending" if end - t < DEAD_GAP else "stopped"), last, t
    return "continues", last, -1


def successors(panel: Panel, c: int, last: int, first: np.ndarray) -> List[str]:
    """Chains whose first row is within SUCC_DAYS after `last` with a previous close within SUCC_TOL of c's last close
    (a possible unlinked rename: listed for the by-hand events file, never linked automatically)."""
    out = []
    for k in np.nonzero((first > last) & (first <= last + SUCC_DAYS))[0]:
        t = int(first[k])
        if abs(panel.prev[k, t] / panel.close[c, last] - 1) < SUCC_TOL:
            out.append(f"{panel.sym_at(int(k), t)}/{panel.isins[panel.isin_idx[k, t]]} from {panel.sessions[t]}")
    return out


def dead() -> None:
    out = Out("dead")
    p = Panel.load()
    sets, _u, months_of, last = load_context(p)
    events = load_events()
    seen: Dict[int, Tuple[str, int, int, str]] = {}
    cnt: Counter = Counter()
    never: Dict[int, List[str]] = defaultdict(list)
    for m in entry_months(p, last):
        sg = month_signal(p, m, sets.get(m, set()))
        e = p.months[m][0]
        for c in sg.B:
            if not p.has_row[c, e:min(p.S, e + LOCK_TRIES + 1)].any():
                never[c].append(m)                     # name_path skips it: never entered, so never -100%
                continue
            k, lr, d = stop_scan(p, c, e, max((HOLD,) + HOLDS_PRINTED))
            if k == "continues":
                continue
            lr = p.last_row_at(c, lr)                  # identity from a real row, never from an empty slot
            isin = p.isins[p.isin_idx[c, lr]]
            cls = events.get(isin, {}).get("type", "UNMAPPED")
            cnt[(m[:4], k, cls)] += 1
            seen.setdefault(c, (k, lr, d, cls))
    out(f"stopped names inside B holds (any hold up to {max((HOLD,) + HOLDS_PRINTED)} sessions), stock-months by year / "
        f"kind / class: {dict(sorted(cnt.items()))}")
    out(f"distinct chains {len(seen)}; UNMAPPED {sum(1 for v in seen.values() if v[3] == 'UNMAPPED')} "
        f"(UNMAPPED = -100% in the read; add them to {EVENTS_FILE.name})")
    first = np.argmax(p.has_row, axis=1)
    for c, (k, lr, d, cls) in sorted(seen.items(), key=lambda x: x[1][1]):
        isin = p.isins[p.isin_idx[c, lr]]
        ev = events.get(isin, {})
        into = ev.get("into_isin")
        swap = "-"
        if into:
            a = p.chain_of_isin(into, lr)
            swap = (f"into {into} resolves at the last date: "
                    f"{a is not None and p.last_row_at(a, lr) >= 0}")
        out(f"  {p.sym_at(c, lr):14s} {isin} last {p.sessions[lr]} {k:8s} {cls:20s} {swap}; "
            f"possible successor: {successors(p, c, lr, first) or '-'}")
    out(f"in B but no row on the entry session or the {LOCK_TRIES} after it (skipped in the read, never -100%; a "
        f"stopped name was still ranked at the signal day): {len(never)}")
    for c, ms in sorted(never.items(), key=lambda x: x[1][0]):
        lr = p.last_row_at(c, p.months[ms[0]][0])
        out(f"  {p.sym_at(c, lr):14s} {p.isins[p.isin_idx[c, lr]]} last {p.sessions[lr]} entry months {ms}")
    unused = sorted(set(events) - {p.isins[p.isin_idx[c, v[1]]] for c, v in seen.items()})
    out(f"events file entries not on this list (kept, harmless): {len(unused)} {unused}")
    out.save()


# ---------------------------------------------------------------- ca (counts only): the demerger / rights zero list
def ca_cmd() -> None:
    out = Out("ca")
    p = Panel.load()
    raw = CA.load_raw(CA_ROOT)
    if raw is None:
        raise SystemExit(f"CA file incomplete or absent at {CA_ROOT}: run `python -I nse_ca.py fetch` first")
    _fac, zero, _owner = CA.events_from(raw)
    zero, done = apply_ca_overrides(zero, [o for o in load_ca_overrides() if o.get("applies_to") == "zero"])
    out(f"CA overrides applied to the zero list ({CA_OVERRIDE_FILE.name}): {done}")
    entries, miss = ca_zero_entries(p, zero)
    tmp = CA_ZERO_FILE.with_name(CA_ZERO_FILE.name + ".tmp")
    tmp.write_text(json.dumps(entries, indent=0), encoding="utf-8")
    tmp.replace(CA_ZERO_FILE)
    out(f"CA demerger / rights events {len(zero)}: written {len(entries)} to {CA_ZERO_FILE.name} "
        f"({dict(Counter(x['kind'] for x in entries))}); not located {len(miss)} by reason "
        f"{dict(Counter(w.split(':')[0] for _s, _d, w in miss))}")
    out.file_only("not located: " + "; ".join(f"{a} {b} ({w})" for a, b, w in miss))
    out.save()


# ---------------------------------------------------------------- P5 oracle (G3; counts only)
def oracle() -> None:
    import joiner_profile as JP
    out = Out("oracle")
    p = Panel.load()
    mem = V.load_membership()
    rmap = json.loads(V.MAP_FILE.read_text())
    syms = sorted({y for xs in mem.values() for y in (V.resolve(x, rmap) for x in xs) if y})
    res = ca_adjust(p)
    ok_ca, _l = p3_ca_verdict(p, res)
    out(f"§4.2 CA file complete {res.complete}: split / bonus rows adjusted {len(res.fixes)}; P3-CA "
        f"{'YES' if ok_ca else 'NO'}; demerger / rights ex-date returns set to 0: {len(res.zeroed)}")
    by_last: Dict[str, int] = {}
    for c in range(p.C):
        lr = p.last_row_at(c, p.S - 1)
        if lr >= 0:
            by_last.setdefault(p.sym_at(c, lr), c)
    G = p.G
    n = ok = 0
    miss: List[str] = []
    nochain: List[str] = []
    for s in syms:
        bars = JP.load_daily(s)
        ch = by_last.get(s)
        if not bars or ch is None:
            nochain.append(s)
            continue
        c = ch
        kc: Dict[str, float] = {}
        for b in bars:
            kc[b["d"][:7]] = b["c"]                     # last bar of each month
        ms = sorted(kc)
        for a, b2 in zip(ms, ms[1:]):
            if b2 not in p.months or a not in p.months or mdiff(b2, a) != 1:
                continue
            ta, tb = p.months[a][1], p.months[b2][1]
            if p.last_row_at(c, ta) < 0 or not p.has_row[c, p.months[b2][0]:tb + 1].any():
                continue
            kr = kc[b2] / kc[a] - 1
            br = G[c, tb] / G[c, ta] - 1
            n += 1
            if abs(kr - br) <= ORACLE_TOL:
                ok += 1
            else:
                miss.append(f"{s} {b2}")
    rate = ok / n if n else 0.0
    out(f"P5 Kite oracle: {ok} of {n} stock-months match within {ORACLE_TOL:.0%} ({rate:.2%}); pass >= "
        f"{ORACLE_PASS:.0%}: {'PASS' if rate >= ORACLE_PASS else 'FAIL'}")
    out(f"symbols without a Kite file or a chain: {nochain}")
    out(f"mismatched stock-months ({len(miss)}): {miss}")
    out.save()


# ---------------------------------------------------------------- the report (B) -- RETURNS, after the review only
def build_arm_paths(p: Panel, sigs: Dict[str, MonthSig], arms: Dict[str, Dict[str, List[int]]], hold: int,
                    events: Dict[str, dict]) -> Dict[str, Dict[str, List[Path_]]]:
    cache: Dict[Tuple[int, str], Path_] = {}
    out: Dict[str, Dict[str, List[Path_]]] = {a: {} for a in ARMS}
    for m, arm in arms.items():
        e0 = p.months[m][0]
        for a, cs in arm.items():
            ps = []
            for c in cs:
                if (c, m) not in cache:
                    cache[(c, m)] = name_path(p, c, e0, hold, events)
                ps.append(cache[(c, m)])
            out[a][m] = ps
    return out


def arm_series(paths: Dict[str, List[Path_]], bounds: Dict[str, List[Tuple[str, int]]], cost: float,
               dead_zero: bool, hold: int = 0) -> Dict[str, Tuple[float, int]]:
    return calendar_time({m: cohort_returns(ps, bounds[m], cost, dead_zero) for m, ps in paths.items()},
                         hold_months(hold or HOLD))


def report() -> None:
    out = Out("report")
    p = Panel.load()
    census, _n = prevclose_census(p)
    ok3, _l = p3_verdict(p, census)
    res = ca_adjust(p)
    ok_ca, _l2 = p3_ca_verdict(p, res)
    if not (ok3 or ok_ca):
        raise SystemExit("P3 is NO on the raw previous close and on the CA path (CA file incomplete, or a known case "
                         "fails): returns cannot be computed (design §4.2, G1)")
    out(f"§4.2 P3 raw {'YES' if ok3 else 'NO'}, CA path {'YES' if ok_ca else 'NO'}; split / bonus rows adjusted "
        f"{len(res.fixes)}; demerger / rights ex-date returns set to 0: {len(res.zeroed)}")
    sets, _u, months_of, last_mem = load_context(p)
    ann = load_announced()
    events = load_events()
    ms = [m for m in entry_months(p, last_mem) if m <= OOS_W[1]]
    sigs_a = gate_signals(p, sets, months_of, ms, ann)
    gv = gate_verdict(era_counts(sigs_a, months_of, GATE_ERA, GATE_K, last_mem))
    out(f"(A) recomputed: {gv[0]} -- {gv[1]}")
    sigs: Dict[str, MonthSig] = {}
    arms: Dict[str, Dict[str, List[int]]] = {}
    for m in ms:
        s = p.months[m][0] - 1
        blocked = announced_block(ann, sym_chain_at(p, s), p.sessions[s], m) if ann is not None else set()
        sigs[m] = month_signal(p, m, sets.get(m, set()), blocked)
        arms[m] = month_arms(p, sigs[m], sets.get(m, set()))
    bounds = {m: bounds_from(p, m, OOS_W[1]) for m in ms}
    W_ALL = (IS_W[0], OOS_W[1])
    if ann is None:
        near = {m: sum(1 <= (join_distance(months_of.get(c, []), m) or 0) <= FALLBACK_DROP for c in arms[m]["P"])
                for m in ms}
        out(f"BIAS (no ANNOUNCED list): P holds names that joined within {FALLBACK_DROP} months -- likely already "
            f"announced at entry; P stock-months affected {sum(near.values())} of {sum(len(arms[m]['P']) for m in ms)}")
    out("per month: P/MOM overlap, median TV (Rs Cr) P / MOM / EW-B")
    for m in ms:
        am = arms[m]
        med = [st.median([sigs[m].tv[c] for c in am[k]]) / 1e7 if am[k] else 0 for k in ("P", "MOM", "EWB")]
        out(f"  {m} overlap {len(set(am['P']) & set(am['MOM'])):2d}  TV {med[0]:7.1f} {med[1]:7.1f} {med[2]:7.1f}")

    W_IS, W_OOS = IS_W, OOS_W
    results: Dict[int, Dict[str, Dict[str, List[Path_]]]] = {}
    for hold in (HOLD,) + HOLDS_PRINTED:
        results[hold] = build_arm_paths(p, sigs, arms, hold, events)
    paths = results[HOLD]
    if ann is not None:
        for a in ARMS:
            hit = [(m, sigs[m].sym[x.c], p.sessions[x.e], y["announced"]) for m in ms for x in paths[a][m] if x.e >= 0
                   for y in [announced_at_entry(ann, sigs[m].sym[x.c], p.sessions[sigs[m].s], p.sessions[x.e], m)] if y]
            out(f"FLAG (owner D1): {a} entries whose circular is dated after the signal day but on or before the entry "
                f"session (eligible by §3, announced at entry; time of day unknown): {len(hit)}"
                + ("".join(f"; {m} {s} entry {e} circular {d}" for m, s, e, d in hit)))
    ct = {a: arm_series(paths[a], bounds, COST, True) for a in ARMS}
    out("live sleeves per return month (P): " + ", ".join(f"{T} {n}" for T, (_r, n) in ct["P"].items()))
    out(f"\nmean monthly calendar-time return, net {COST:.2%}/side, dead = -100%  (whole / IS / OOS)")
    for a in ARMS:
        out(f"  {a:7s} " + " / ".join(fmt_p(window_mean(ct[a], w)) for w in (W_ALL, W_IS, W_OOS)))
    for lvl in COST_LEVELS:
        out(f"levels at {lvl:.2%}/side: " + "  ".join(
            f"{a} {fmt_p(window_mean(arm_series(paths[a], bounds, lvl, True), W_ALL))}" for a in ("P", "MOM", "EWB")))
    cr = crand_means(paths["EWB"], bounds, COST, W_ALL, hold_months(HOLD))
    p95 = pctl(cr, CRAND_PCT)
    out(f"C-RAND: {CRAND_DRAWS} draws of {N_P} from B, seed {SEED}: 95th percentile {fmt_p(p95)}")
    holds: Dict[str, bool] = {}
    dd = {a: max_dd_pct(ct[a], W_OOS) for a in ARMS}
    for a in ("P", "GROW", "VOL"):
        lo, hi = block_ci(diff_series(ct[a], ct["MOM"], W_ALL))
        stt = ArmStats(mean_diff(ct[a], ct["MOM"], W_ALL), lo, hi, mean_diff(ct[a], ct["MOM"], W_OOS),
                       mean_diff(ct[a], ct["EWB"], W_IS), mean_diff(ct[a], ct["EWB"], W_OOS),
                       window_mean(ct[a], W_ALL), p95)
        h, why = read_rule(stt)
        holds[a] = h
        out(f"\nH-{a}: {'HOLDS' if h else 'not shown'}  {a} - MOM whole {fmt_p(stt.d_mom_all)} "
            f"[{CI_LEVEL:.2%} CI {fmt_p(lo)}, {fmt_p(hi)}]  OOS {fmt_p(stt.d_mom_oos)}  {a} - EW-B IS "
            f"{fmt_p(stt.d_ewb_is)} OOS {fmt_p(stt.d_ewb_oos)}  OOS max DD {dd[a]:.1f}% vs MOM {dd['MOM']:.1f}%")
        for w in why:
            out("    " + w)
    cand, why_c = candidate(gv[0] == "PASS", holds, dd, dd["MOM"])
    out(f"\nVERDICT: {'CANDIDATE' if cand else 'NOT A CANDIDATE'} -- {why_c}")
    if any(holds.values()):
        nj = {a: [hold_return(x, COST, True) for m in ms for x in paths[a][m]
                  if join_distance(months_of.get(x.c, []), m) is None] for a in ("P", "MOM")}
        mp = [v for v in nj["P"] if v is not None]
        mm = [v for v in nj["MOM"] if v is not None]
        if not mp or not mm or st.mean(mp) - st.mean(mm) <= 0:
            out("FLAG: needs the join to pay (NOT-JOINED P names do not beat NOT-JOINED MOM names)")

    out("\nsecond table: dead names at the last traded close")
    ct_last = {a: arm_series(paths[a], bounds, COST, False) for a in ARMS}
    for a in ARMS:
        out(f"  {a:7s} " + " / ".join(fmt_p(window_mean(ct_last[a], w)) for w in (W_ALL, W_IS, W_OOS)))
    for hold in HOLDS_PRINTED:
        cth = {a: arm_series(results[hold][a], bounds, COST, True, hold) for a in ("P", "MOM", "EWB")}
        out(f"hold {hold} (printed, never read): " + "  ".join(
            f"{a} {fmt_p(window_mean(cth[a], W_ALL))}" for a in cth) + f"  P - MOM OOS "
            f"{fmt_p((window_mean(cth['P'], W_OOS) or 0) - (window_mean(cth['MOM'], W_OOS) or 0))}")

    out("\nper arm: names, skipped (locked entry), entry deferrals, exit deferrals, BE/BZ during hold, dead, pending, "
        "merged, last_close (owner D3), swapped")
    for a in ARMS:
        xs = [x for m in ms for x in paths[a][m]]
        c = Counter(x.status for x in xs)
        out(f"  {a:7s} {len(xs):5d} {c['skipped']:4d} {sum(x.tries for x in xs):4d} {sum(x.defer_exit for x in xs):4d} "
            f"{sum(x.be for x in xs):4d} {c['dead']:4d} {c['pending']:3d} {c['merged']:3d} {c['last_close']:3d} {sum(x.swapped for x in xs):3d}")
        for x in xs:
            if x.status in ("dead", "pending"):
                out(f"      dead: {p.sym_at(x.c, x.x)} last {p.sessions[x.x]}")
    out("\nsplit by JOINED (a member after m, by the last membership month) / months to join -- report only")
    for a in ("P", "MOM", "EWB"):
        g: Dict[str, List[float]] = defaultdict(list)
        for m in ms:
            for x in paths[a][m]:
                r = hold_return(x, COST, True)
                if r is None:
                    continue
                j = first_join_after(months_of.get(x.c, []), m)
                if j is None:
                    g["never"].append(r)
                else:
                    k = mdiff(j, m)
                    g[next(f"{lo}-{hi}" for lo, hi in MONTHS_TO_JOIN if lo <= k <= hi)].append(r)
        out(f"  {a:5s} " + "  ".join(f"{b}: n {len(v)} mean {fmt_p(st.mean(v))}" for b, v in sorted(g.items()) if v))
    hr: List[Tuple[float, str, str]] = sorted(
        (v, p.sym_at(x.c, x.e), m) for m in ms for x in paths["P"][m] for v in [hold_return(x, COST, True)]
        if v is not None)
    vals = [v for v, _s, _m in hr]
    if len(vals) >= 2:
        q1, q2, q3 = st.quantiles(vals, n=4)
        out(f"\nP single-name hold returns: q1 {fmt_p(q1)} median {fmt_p(q2)} q3 {fmt_p(q3)}; share below -30% "
            f"{sum(v < -0.30 for v in vals) / len(vals):.1%}; worst 10: " +
            ", ".join(f"{s} {m} {v:.0%}" for v, s, m in hr[:10]))
    out(f"TOP5 variant (report only): whole {fmt_p(window_mean(ct['TOP5'], W_ALL))}")
    out(f"FO-TAIL (report only): whole {fmt_p(window_mean(ct['FOTAIL'], W_ALL))}")
    for f in sorted(INDEX_DIR.glob("*.json")) if INDEX_DIR.exists() else []:
        ix = json.loads(f.read_text())
        me: Dict[str, float] = {}
        for d in sorted(ix):
            me[d[:7]] = float(ix[d])
        mr = [me[b] / me[a] - 1 for a, b in zip(sorted(me), sorted(me)[1:]) if in_window(b, W_ALL)]
        out(f"index {f.stem}: mean monthly {fmt_p(st.mean(mr) if mr else None)}")
    if not INDEX_DIR.exists():
        out(f"indices: none on disk ({INDEX_DIR}); report-only line skipped")
    pos = CAPITAL / CAPITAL_NAMES
    big = sum(1 for m in ms for c in arms[m]["P"] if pos > POS_TV_MAX * sigs[m].tv[c])
    days = sorted(pos / (BUILD_TV_DAY * sigs[m].tv[c]) for m in ms for c in arms[m]["P"] if sigs[m].tv[c] > 0)
    if days:
        out(f"capacity at Rs {pos:,.0f} per name (owner: Rs 50 L / 30): P stock-months above {POS_TV_MAX:.0%} of TV "
            f"{big}; days to build at {BUILD_TV_DAY:.0%} of TV/day median {st.median(days):.2f} max {max(days):.2f}")
    held = capital_per_name(hold_months(HOLD))
    pre, post, fy = post_tax_equity(ct["P"])
    out(f"Rs 50 L in P at the ACTUAL holding of ~Rs {held:,.0f} per name ({hold_months(HOLD)} overlapping sleeves x "
        f"{CAPITAL_NAMES}; owner D2; the calendar-time series is the whole-account return, so the rupee lines are at "
        f"this size): pre-tax {pre:,.0f}  post-tax (STCG {stcg_rate(2023):.2%}/{stcg_rate(2024):.2%} incl. "
        f"{STCG_CESS:.0%} cess, {CAP_LOSS_CARRY}-FY carry) {post:,.0f}")
    for row in fy:
        out(f"  {CMP.fy_label(row['fy'])} P&L {row['pnl']:>14,.0f} taxable {row['taxable']:>14,.0f} "
            f"tax {row['tax']:>12,.0f}")
    out.save()


# ---------------------------------------------------------------- G0 (oracle ceiling on the Kite dailies on disk)
def g0_gap(joiner_ret: float, incumbent_rets: Sequence[float]) -> Optional[float]:
    return joiner_ret - st.mean(incumbent_rets) if incumbent_rets else None


def close_on_or_before(dates: List[str], bars: List[dict], d: str, max_days: int = 7) -> Optional[float]:
    i = bisect_right(dates, d) - 1
    if i < 0:
        return None
    if (_date.fromisoformat(d) - _date.fromisoformat(dates[i])).days > max_days:
        return None
    return float(bars[i]["c"])


def g0_event(daily: Dict[str, List[dict]], dates: Dict[str, List[str]], sym: str, start_d: str,
             peers: Iterable[str], hold: int = HOLD) -> Optional[Tuple[float, List[float]]]:
    """Joiner close-to-close over `hold` bars from its first bar on/after start_d, and each peer's return over the
    same calendar dates.  None if the joiner lacks the bars (censored)."""
    if sym not in daily:
        return None
    i = bisect_left(dates[sym], start_d)
    if i + hold >= len(dates[sym]) or dates[sym][i][:7] != start_d[:7]:
        return None
    d0, d1 = dates[sym][i], dates[sym][i + hold]
    jr = daily[sym][i + hold]["c"] / daily[sym][i]["c"] - 1
    pr = []
    for q in peers:
        if q == sym or q not in daily:
            continue
        a, b = close_on_or_before(dates[q], daily[q], d0), close_on_or_before(dates[q], daily[q], d1)
        if a and b:
            pr.append(b / a - 1)
    return jr, pr


def g0_stop(mean_gap_6: Optional[float], mean_gap_12: Optional[float]) -> bool:
    """STOP iff the mean gap over members is <= 0 at both -6 and -12 (a missing gap counts as <= 0)."""
    return (mean_gap_6 is None or mean_gap_6 <= 0) and (mean_gap_12 is None or mean_gap_12 <= 0)


def g0() -> None:
    import joiner_profile as JP
    import de_kill as K
    out = Out("g0")
    mem = V.load_membership()
    rmap = json.loads(V.MAP_FILE.read_text())
    sets = V.eligible_sets(mem, rmap)
    months = sorted(sets)
    months_of = K.member_months(sets)
    first = {s: ms[0] for s, ms in months_of.items()}
    daily = {s: b for s in sorted(first) for b in [JP.load_daily(s)] if b}
    dates = {s: [b["d"] for b in bars] for s, bars in daily.items()}

    def incumbents(xm: str) -> Set[str]:
        return {s for s in sets.get(xm, set()) if first[s] < xm or first[s] == months[0]}

    def line(name: str, evs: List[Tuple[str, float]], cens: int) -> Optional[float]:
        g = [v for _m, v in evs]
        gi = [v for m, v in evs if m <= IS_W[1]]
        go = [v for m, v in evs if m > IS_W[1]]
        mg = st.mean(g) if g else None
        out(f"{name:28s} n {len(g):3d} censored {cens:3d}  mean gap {fmt_p(mg)}  median "
            f"{fmt_p(st.median(g) if g else None)}  share > 0 {fmt_p(sum(v > 0 for v in g) / len(g) if g else None)}  "
            f"IS {fmt_p(st.mean(gi) if gi else None)} OOS {fmt_p(st.mean(go) if go else None)}")
        return mg

    joiners = sorted(s for s, m in first.items() if m > months[0])
    gaps: Dict[int, Optional[float]] = {}
    for k in (12, 6):
        evs, cens = [], 0
        for s in joiners:
            xm = madd(first[s], -k)
            if xm < months[0]:
                cens += 1
                continue
            r = g0_event(daily, dates, s, xm + "-01", incumbents(xm))
            gp = g0_gap(r[0], r[1]) if r else None
            if gp is None:
                cens += 1
            else:
                evs.append((xm, gp))
        gaps[k] = line(f"G0 true joiners at -{k} m", evs, cens)
    out(f"G0 STOP rule (mean gap <= 0 at both -6 and -12): {'STOP' if g0_stop(gaps[6], gaps[12]) else 'continue'}")
    evs, cens = [], 0
    for s in joiners:
        r = g0_event(daily, dates, s, first[s] + "-01", incumbents(first[s]))
        gp = g0_gap(r[0], r[1]) if r else None
        if gp is None:
            cens += 1
        else:
            evs.append((first[s], gp))
    line("D3 joiners after joining", evs, cens)
    evs, cens = [], 0
    for a, b in zip(months, months[1:]):
        for s in sets[a] - sets[b]:
            r = g0_event(daily, dates, s, b + "-01", incumbents(b))
            gp = g0_gap(r[0], r[1]) if r else None
            if gp is None:
                cens += 1
            else:
                evs.append((b, gp))
    line("D4 leavers after leaving", evs, cens)
    ann = load_announced()
    if ann is None:
        out("D2 announcement: skipped -- no ANNOUNCED list (P1)")
    else:
        evs, cens = [], 0
        for x in ann:
            s = V.resolve(x["symbol"], rmap)
            if s is None or s not in dates:
                cens += 1
                continue
            i = bisect_left(dates[s], x["announced"])
            r = g0_event(daily, dates, s, dates[s][i] if i < len(dates[s]) else "9999-12-31",
                         incumbents(x["member_month"])) if i < len(dates[s]) else None
            gp = g0_gap(r[0], r[1]) if r else None
            if gp is None:
                cens += 1
            else:
                evs.append((x["member_month"], gp))
        line("D2 bought on the circular", evs, cens)
    out.save()


# ---------------------------------------------------------------- argv
COMMANDS: Dict[str, Tuple[Callable[[], None], bool]] = {
    "build": (build, False), "probes": (probes, False), "gate": (gate, False), "dead": (dead, False), "ca": (ca_cmd, False),
    "oracle": (oracle, False), "g0": (g0, True), "report": (report, True)}
USAGE = ("usage: joiner_positional.py build|probes|gate|dead|ca|oracle  |  g0 --reviewed  |  report --reviewed")


def main(argv: List[str]) -> None:
    if not argv or argv[0] not in COMMANDS:
        raise SystemExit(USAGE)
    fn, returns = COMMANDS[argv[0]]
    rest = argv[1:]
    if returns:
        if rest != ["--reviewed"]:
            raise SystemExit(f"{argv[0]} computes returns: run it only after the independent review, with --reviewed")
    elif rest:
        raise SystemExit(USAGE)
    fn()


if __name__ == "__main__":
    main(_argv)

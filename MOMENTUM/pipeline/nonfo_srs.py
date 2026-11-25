"""SRS on liquid NON-F&O stocks: the honest test of finding 14 (NONFO_SRS_DESIGN.md, frozen 2026-11-21).

    python -I nonfo_srs.py oldfo      # B1b: 60 first-session F&O bhavcopies 2015-01..2019-12 -> labels only (NSE)
    python -I nonfo_srs.py band       # B2: B150 per month from the CM bhavcopy + label counts.  COUNTS ONLY
    python -I nonfo_srs.py resolve    # B3a: bhav symbol -> Kite data symbol (1 Kite instruments call).  COUNTS ONLY
    python -I nonfo_srs.py coverage   # B3b: dead-name coverage gate on NEVER (§7.1) -> READ WINDOW / STOP
    python -I nonfo_srs.py fetch      # B4: Kite fetch, new band names only; refuses on a gate STOP
    python -I nonfo_srs.py rows       # B5: rows (SRS, ASRS) for new names; features, lock post-pass facts, identity
    python -I nonfo_srs.py checks     # pre-run checks P1-P11: counts / booleans only
    python -I nonfo_srs.py report --after-review   # prints R.  ONLY after the independent review (§10 protocol)

Reused, never copied: full_asrs (fetch_parallel, load_stock, stock_rows, size, trade_rs, drawdown), survivorship
(membership, rename map, eligible sets, pit_files, load_rows, select_trades, check_returns / accept_returns),
de_kill (boot_diff, member_months), liq_side (turnover_sma, recost, top_by_score), joiner_free (tier_slip, mdiff),
fo_membership (url, stock_futures), nse_cm_fetch (get, write_atomic, path, ROOT), y2026_asrs (boot_ci,
full_sessions), slip_test (extra_cost_ps), stocks_asrs_check (trade, completed, cost_per_share).

Frozen choices made concrete here (every one is listed in the module constants or the docstring of its function):
- SESSIONS: from 2020-01-01 to LAST_DAY a session is a CM date of the htf calendar (= the row calendar plus 3 halt
  days; it holds the 4 full Saturday sessions and no muhurat); before 2020 and after LAST_DAY, CM dates on weekdays
  (drops the 2019-10-27 muhurat).  Every 60-session window, lock-day test, T60 and P-proxy counts these sessions.
- T60: the median of 60 values, one per session of the window; a session with no EQ row counts as 0 traded value.
- One stock across a rename or a split: its bhavcopy history is the rows of its symbol PLUS the rows of any other
  symbol carrying the same ISIN on that day (Store.linked).  A rename keeps the ISIN; a split keeps the symbol.
- Labels (§2) match a band stock to an F&O member by ANY link: symbol chain (NSE symbolchange.csv, changes
  effective after the observation date), ISIN (2018-12 on, from the CM bhavcopy), or resolved data symbol
  (survivorship rename map for 2020-26 members).  A missed link would put an ever-F&O stock into NEVER, the decisive
  arm; a false link only shrinks NEVER, so the errors are made in the safe direction.  J = FIRST member month;
  month J-1 unlabelled; a stock between two member spells is EX (the J-1 rule is literal: first join only).
- Voids (non-EQ day, locked entry) are applied AFTER the top-10 cut: the void pick keeps its slot, no backfill.
"""
from __future__ import annotations

import array
import bisect
import csv
import gzip
import io
import json
import math
import os
import pickle
import random
import shutil
import statistics as st
import sys
import time
import zipfile
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]                         # survivorship / full_asrs / liq_side truncate sys.argv at import
sys.argv = sys.argv[:1]
import de_kill as K  # noqa: E402
import full_asrs as F  # noqa: E402
import fo_membership as FM  # noqa: E402
import joiner_free as J  # noqa: E402
import liq_side as L  # noqa: E402
import nse_cm_fetch as C  # noqa: E402
import sim  # noqa: E402
import slip_test as T  # noqa: E402
import stocks_asrs_check as S  # noqa: E402
import survivorship as V  # noqa: E402
import y2026_asrs as Y  # noqa: E402


# ---------------------------------------------------------------- paths (durable, outside the public repo)
NSE = Path(os.environ.get("NSE_DATA", r"C:\Users\mail2\Documents\Projects\NSE_DATA"))  # env NSE_DATA: Pi portability
NONFO = NSE / "nonfo"                        # study outputs: store cache, bands, facts, labels, resolve, rows...
NONFO_RAW = NSE / "kite_raw_nonfo"           # B4: Kite raw files of the NEW band names (fetch_parallel's raw=)
DUR_RAW = NSE / "kite_raw"                   # B0 backup of the cached Kite data
RAW_DIRS: Tuple[Tuple[Path, bool], ...] = (  # (directory, merge y2026 cache?) in lookup order; durable copy first
    (DUR_RAW / "full_raw", True), (F.RAW, True), (DUR_RAW / "surv_raw", False), (V.SURV_RAW, False),
    (NONFO_RAW, False))
STORE_PKL = NONFO / "bhav_store.pkl"
BAND_FILE = NONFO / "band.json"
FACTS_FILE = NONFO / "facts.json"
B150_DIR = NONFO / "b150"
CAL_FILE = NONFO / "row_calendar.json"
SYMCHG = NONFO / "symbolchange.csv"
SYMCHG_SRC = F.BASE.parent / "nse_symchg" / "symbolchange.csv"   # fetched 2026-11-20 for SURVIVORSHIP
FO_OLD_DIR = NONFO / "fo_bhav_old"
FO_OLD_FILE = NONFO / "fo_membership_old.json"
KITE_FILE = NONFO / "kite_nse_symbols.json"
RESOLVE_FILE = NONFO / "resolve.json"
LABELS_FILE = NONFO / "labels.json"
NOBARS_FILE = NONFO / "nobars.json"
IDENT_FILE = NONFO / "identity.json"
FEAT_DIR = NONFO / "feat"
LOCK_DIR = NONFO / "lockpass"
ROWS_DIRS = {"SRS": NONFO / "rows_srs", "ASRS": NONFO / "rows_asrs"}
SIG_N = {"SRS": 3, "ASRS": 1}
REPORT_FILE = NONFO / "nonfo_srs_report.txt"

# ---------------------------------------------------------------- frozen parameters
OLD_FROM, OLD_TO = "2015-01", "2019-12"      # §2 left-censoring fix: labels only
BAND_FROM, BAND_TO = "2020-01", "2026-11"
N_BAND, WIN, MIN_EQ_DAYS = 150, 60, 50       # §1
KEEP_SERIES = {"EQ": 0, "BE": 1, "BZ": 2}    # EQ preferred when a symbol has several series on a day
TFT = (1, 2)                                 # trade-for-trade series codes (BE, BZ)
LOCK_BANDS = (0.05, 0.10, 0.20)              # DOUBLE_ENGINE §3
LOCK_TOL = 0.001                             # close / prevclose within 0.1% of the band (relative)
SURV_HL = 2.0                                # P-SURV: 60-session high / low >= 2
BE_AHEAD = 60                                # P-BE: EQ -> BE/BZ within the next 60 sessions (look-ahead)
INPLAY_VOL_MULT, INPLAY_VOL_N = 2.0, 20      # §7.1 in-play proxy
INPLAY_MIN_PX, INPLAY_RANGE_N, INPLAY_RANGE_PCT = 100.0, 14, 1.5
NEXT_OPEN_MAX = 10                           # locked long exit: next session open, searched up to 10 sessions
GATE_MAX_GONE = 0.05                         # §7.1: a year FAILS above 5% GONE NEVER member-days
GATE_START_BY = 2022                         # STOP if READ WINDOW starts after 2022-01
GATE_INPLAY_STOP = 0.10                      # STOP if GONE share of in-play proxy days 2024-26 > 10%
GATE_FIRST_YEAR, GATE_LAST_YEAR = 2020, 2026
LEVEL_VERDICT = 1 - 0.05 / 3                 # §7.2 Bonferroni: 98.33%
LEVEL_REPORT = 0.95
BOOT_N, BOOT_SEED = 5000, 7
RAND_DRAWS, RAND_SEED, RAND_K = 1000, 7, 10  # C-RAND
SHORT_PENALTY = 1.20                         # Q10: locked short exits at close x 1.20
NOTIONAL_CAP2 = 250_000.0                    # §3: the Rs2.5L column
FLAT_SLIPS = (0.0005, 0.0010, 0.0015)        # §6, printed beside, never in the verdict
DD_FLAG_PCT = 25.0
F4_SIGN = 1                                  # F4 (results/joiner_free.txt): BEFORE - AFTER = +0.132 > 0
IS_FROM, IS_END, OOS_FROM, LAST_DAY = "2020-01-01", F.IS_END, "2024-01-01", F.LAST_DAY
CAP_PX_FLOOR = N_MIN_PX = 100.0              # P7: the scan's close > 100 on Kite adjusted prices
JD = Dict[str, Any]
JL = List[Any]
Row = Tuple[int, int, int, float, float, float, float, float, float, float]   # di ser isin o h l c p q v
Pair = Tuple[str, float]


# ---------------------------------------------------------------- small helpers
def write_json(path: Path, obj: Any) -> None:
    """Atomic: <name>.tmp then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    if not path.exists():
        raise SystemExit(f"missing {path}: run the earlier stage first")
    return json.loads(path.read_text(encoding="utf-8"))


def month_add(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) - 1 + k
    return f"{y + mo // 12:04d}-{mo % 12 + 1:02d}"


def months(a: str, b: str) -> List[str]:
    out, m = [], a
    while m <= b:
        out.append(m)
        m = month_add(m, 1)
    return out


def weekday(d: str) -> int:
    return date(int(d[:4]), int(d[5:7]), int(d[8:10])).weekday()


def _f(x: str) -> float:
    try:
        return float(x)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- B1: CM bhavcopy parse (both formats)
OLD_COLS = ("SYMBOL", "SERIES", "ISIN", "OPEN", "HIGH", "LOW", "CLOSE", "PREVCLOSE", "TOTTRDQTY", "TOTTRDVAL")
UDIFF_COLS = ("TckrSymb", "SctySrs", "ISIN", "OpnPric", "HghPric", "LwPric", "ClsPric", "PrvsClsgPric",
              "TtlTradgVol", "TtlTrfVal")
BhavRow = Tuple[str, str, float, float, float, float, float, float, float]   # series isin o h l c prev qty value


def parse_cm_text(text: str) -> Dict[str, BhavRow]:
    """{symbol: (series, isin, open, high, low, close, prevclose, qty, traded value Rs)} for series EQ/BE/BZ.
    Format chosen by the header (old CM zip or UDiFF).  When a symbol has several kept series on one day, EQ wins,
    then BE, then BZ.  A header without the expected columns raises (never a silent empty day)."""
    rd = csv.reader(io.StringIO(text))
    head = [h.strip() for h in next(rd)]
    cols = OLD_COLS if head and head[0] == "SYMBOL" else UDIFF_COLS
    try:
        ix = [head.index(c) for c in cols]
    except ValueError as e:
        raise ValueError(f"bhavcopy header lacks a column: {e}; header {head[:12]}") from e
    out: Dict[str, BhavRow] = {}
    for r in rd:
        if len(r) <= max(ix):
            continue
        sym, ser = r[ix[0]].strip(), r[ix[1]].strip()
        if ser not in KEEP_SERIES:
            continue
        old = out.get(sym)
        if old is not None and KEEP_SERIES[old[0]] <= KEEP_SERIES[ser]:
            continue
        out[sym] = (ser, r[ix[2]].strip(), _f(r[ix[3]]), _f(r[ix[4]]), _f(r[ix[5]]), _f(r[ix[6]]), _f(r[ix[7]]),
                    _f(r[ix[8]]), _f(r[ix[9]]))
    return out


def parse_cm_bhav(zpath: Path) -> Dict[str, BhavRow]:
    """One CM bhavcopy zip (read in memory, never extracted) -> parse_cm_text."""
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        if len(names) != 1:
            raise ValueError(f"{zpath}: {len(names)} members")
        text = z.read(names[0]).decode("utf-8", errors="replace")
    return parse_cm_text(text)


def cm_sessions(cal: Set[str]) -> List[Tuple[str, Path]]:
    """Regular sessions with a valid CM file (manifest 'ok'); the rule is in the module docstring (SESSIONS)."""
    man = json.loads(C.MANIFEST.read_text())
    out = []
    for kind in ("cm_old", "cm_udiff"):
        for d, (stt, _size, _e) in man.get(kind, {}).items():
            if stt != "ok":
                continue
            if IS_FROM <= d <= LAST_DAY:
                if d not in cal:                     # muhurat / short special sessions are not in the calendar
                    continue
            elif weekday(d) >= 5:                    # before 2020 (no calendar) and after LAST_DAY: weekdays only
                continue
            p = C.path(kind, d)
            if not p.exists():
                raise SystemExit(f"manifest says ok but {p} is missing")
            out.append((d, p))
    return sorted(out)


# ---------------------------------------------------------------- the compact store (32-bit Python, low RAM)
class Hist:
    """Columnar per-symbol history: date index, series code, ISIN code, prices as float32, qty / value as float64."""
    __slots__ = ("di", "ser", "isin", "o", "h", "l", "c", "p", "q", "v")

    def __init__(self) -> None:
        self.di = array.array("i")
        self.ser = array.array("b")
        self.isin = array.array("i")
        self.o, self.h, self.l = array.array("f"), array.array("f"), array.array("f")
        self.c, self.p = array.array("f"), array.array("f")
        self.q, self.v = array.array("d"), array.array("d")


class Store:
    def __init__(self, dates: List[str]) -> None:
        self.dates = dates
        self.idx = {d: i for i, d in enumerate(dates)}
        self.syms: Dict[str, Hist] = {}
        self.isins: List[str] = []
        self.isin_code: Dict[str, int] = {}
        self.isin_syms: Dict[int, Set[str]] = defaultdict(set)

    def add_day(self, i: int, rows: Dict[str, BhavRow]) -> None:
        for sym, (ser, isin, o, h, lo, c, p, q, v) in rows.items():
            code = self.isin_code.get(isin)
            if code is None:
                code = self.isin_code[isin] = len(self.isins)
                self.isins.append(isin)
            self.isin_syms[code].add(sym)
            x = self.syms.get(sym)
            if x is None:
                x = self.syms[sym] = Hist()
            x.di.append(i)
            x.ser.append(KEEP_SERIES[ser])
            x.isin.append(code)
            x.o.append(o)
            x.h.append(h)
            x.l.append(lo)
            x.c.append(c)
            x.p.append(p)
            x.q.append(q)
            x.v.append(v)

    def row(self, s: str, k: int) -> Row:
        x = self.syms[s]
        return (x.di[k], x.ser[k], x.isin[k], x.o[k], x.h[k], x.l[k], x.c[k], x.p[k], x.q[k], x.v[k])

    def day(self, s: str, i: int) -> Optional[Row]:
        """The row of symbol s on session i (own symbol only)."""
        x = self.syms.get(s)
        if x is None:
            return None
        k = bisect.bisect_left(x.di, i)
        return self.row(s, k) if k < len(x.di) and x.di[k] == i else None

    def linked(self, s: str, isin: int, i0: int, i1: int) -> Dict[int, Row]:
        """{session: row} over sessions i0..i1 for ONE stock: rows of symbol s (any ISIN: survives a split), plus
        rows of any other symbol that carry ISIN `isin` that day (survives a rename).  Own symbol wins a clash."""
        out: Dict[int, Row] = {}
        for t in [s] + sorted(self.isin_syms.get(isin, set()) - {s}):
            x = self.syms.get(t)
            if x is None:
                continue
            a, b = bisect.bisect_left(x.di, i0), bisect.bisect_right(x.di, i1)
            for k in range(a, b):
                if t != s and x.isin[k] != isin:
                    continue
                if x.di[k] not in out:
                    out[x.di[k]] = self.row(t, k)
        return out


def build_store(sessions: List[Tuple[str, Path]]) -> Store:
    stv = Store([d for d, _ in sessions])
    for i, (d, p) in enumerate(sessions):
        stv.add_day(i, parse_cm_bhav(p))
        if i % 250 == 0:
            print(f"  parsed {i}/{len(sessions)} {d}", flush=True)
    return stv


def store_signature(sessions: List[Tuple[str, Path]]) -> List[Any]:
    return [len(sessions), sessions[0][0] if sessions else "", sessions[-1][0] if sessions else "",
            sum(p.stat().st_size for _, p in sessions)]


class _StoreUnpickler(pickle.Unpickler):
    """The cache is written by `python nonfo_srs.py band`, so its classes are recorded as __main__.Store / Hist; an
    importer (tests, checks via import) must map them back.  Only these two classes and builtins / array load."""
    def find_class(self, module: str, name: str) -> Any:
        if name in ("Store", "Hist") and module in ("__main__", "nonfo_srs"):
            return {"Store": Store, "Hist": Hist}[name]
        if module in ("builtins", "array", "collections", "copyreg", "_codecs"):
            return super().find_class(module, name)
        raise pickle.UnpicklingError(f"refusing {module}.{name} in the store cache")


def load_store(cal: Set[str]) -> Store:
    """The store from the pickle cache when its signature matches the CM files on disk, else rebuilt."""
    sessions = cm_sessions(cal)
    sig = store_signature(sessions)
    if STORE_PKL.exists():
        with STORE_PKL.open("rb") as fh:
            cached_sig, stv = _StoreUnpickler(fh).load()
        if cached_sig == sig:
            if isinstance(stv, Store):
                return stv
    print(f"building the bhavcopy store from {len(sessions)} sessions", flush=True)
    built = build_store(sessions)
    NONFO.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PKL.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump((sig, built), fh, protocol=4)
    os.replace(tmp, STORE_PKL)
    return built


def htf_calendar() -> Set[str]:
    return set(json.loads((F.BASE / "htf" / "prep.json").read_text())["cal"])


# ---------------------------------------------------------------- §1 / §4 base filters (pure)
def lock_day(c: float, h: float, lo: float, p: float) -> Tuple[int, float]:
    """(+1 upper lock / -1 lower lock / 0, band).  LOCK-DAY (DOUBLE_ENGINE §3): close = high and close / prevclose
    within 0.1% (relative) of 1.05 / 1.10 / 1.20, or close = low and within 0.1% of 0.95 / 0.90 / 0.80."""
    if p <= 0 or c <= 0:
        return 0, 0.0
    x = c / p
    for b in LOCK_BANDS:
        if c == h and abs(x / (1 + b) - 1) <= LOCK_TOL:
            return 1, b
        if c == lo and abs(x / (1 - b) - 1) <= LOCK_TOL:
            return -1, b
    return 0, 0.0


def row_lock(r: Row) -> Tuple[int, float]:
    return lock_day(r[6], r[4], r[5], r[7]) if r[1] == 0 else (0, 0.0)


def lock_prone(hist: Iterable[Row]) -> bool:
    """>= 1 lock day at +-5% or >= 2 lock days at any band in the window (EQ rows only)."""
    n, five = 0, False
    for r in hist:
        dirn, b = row_lock(r)
        if dirn:
            n += 1
            five = five or b == LOCK_BANDS[0]
    return five or n >= 2


def t60(values: Sequence[float], n: int = WIN) -> float:
    """Median of n per-session traded values; sessions without an EQ trade count 0 (values may be shorter)."""
    if len(values) > n:
        raise ValueError(f"{len(values)} values for a {n}-session window")
    return float(st.median(list(values) + [0.0] * (n - len(values))))


def eq_traded(r: Row) -> bool:
    return r[1] == 0 and r[8] > 0


def base_reason(isin: str, hist: Dict[int, Row], fo_sym: bool, fo_isin: bool) -> Optional[str]:
    """First failing base filter, or None (the stock is a candidate).  The rebalance-day EQ test is done by the
    caller (candidates are symbols with an EQ row that day).  Order: ISIN, 50-of-60, LOCK-PRONE, F&O."""
    if not isin.startswith("INE"):
        return "isin"
    if sum(eq_traded(r) for r in hist.values()) < MIN_EQ_DAYS:
        return "eq50"
    if lock_prone(hist.values()):
        return "lockprone"
    if fo_sym or fo_isin:
        return "fo"
    return None


def rebalance(stv: Store, month: str) -> Tuple[int, int]:
    """(R, i0): R = index of the last session before month-01, i0 = R - 59 (the 60-session window)."""
    r = bisect.bisect_left(stv.dates, month + "-01") - 1
    if r - WIN + 1 < 0:
        raise ValueError(f"{month}: fewer than {WIN} sessions before the month")
    return r, r - WIN + 1


def first_session(stv: Store, month: str) -> Optional[int]:
    i = bisect.bisect_left(stv.dates, month + "-01")
    return i if i < len(stv.dates) and stv.dates[i][:7] == month else None


def fo_isin_codes(stv: Store, syms: Iterable[str], idxs: Iterable[Optional[int]]) -> Set[int]:
    out: Set[int] = set()
    ii = [i for i in idxs if i is not None]
    for s in syms:
        for i in ii:
            r = stv.day(s, i)
            if r is not None:
                out.add(r[2])
    return out


def band(stv: Store, month: str, fo_syms: Set[str]) -> Tuple[List[JD], Counter[str]]:
    """B150 of `month`: base filters on the 60 sessions ending at the last session of month-1 (nothing later is
    read), rank by T60 descending, ties by symbol; the top 150.  -> (entries, first-failing-filter counts)."""
    r_i, i0 = rebalance(stv, month)
    fo_isins = fo_isin_codes(stv, fo_syms, (r_i, first_session(stv, month)))
    reasons: Counter[str] = Counter()
    cands: List[Tuple[float, str, str]] = []
    for s in sorted(stv.syms):
        row = stv.day(s, r_i)
        if row is None or row[1] != 0:
            continue
        isin = stv.isins[row[2]]
        hist = stv.linked(s, row[2], i0, r_i)
        why = base_reason(isin, hist, s in fo_syms, row[2] in fo_isins)
        reasons[why or "candidate"] += 1
        if why is None:
            cands.append((t60([r[9] if eq_traded(r) else 0.0 for r in hist.values()]), s, isin))
    cands.sort(key=lambda x: (-x[0], x[1]))
    out = [dict(sym=s, isin=i, t60=v, rank=k + 1) for k, (v, s, i) in enumerate(cands[:N_BAND])]
    reasons["band"] = len(out)
    return out, reasons


# ---------------------------------------------------------------- per-day facts for band stock-months
FACT_KEYS = ("eq", "lock", "hi", "lo", "close", "next_open", "psurv", "pbe", "inplay", "bclose_prev")


def entry_facts(stv: Store, s: str, isin: str, month: str) -> Dict[str, JL]:
    """{date: [eq, lock, hi, lo, close, next_open, psurv, pbe, inplay, bclose_prev]} for every session of `month`.
    eq: series EQ that day.  lock: +1 / -1 / 0 (LOCK-DAY).  next_open: open of the next EQ session (<= 10 later).
    psurv (point-in-time): >= 1 LOCK-DAY in the previous 60 sessions, or their high / low >= 2.
    pbe (look-ahead diagnostic): series BE or BZ on any of the next 60 sessions.
    inplay: qty >= 2 x mean qty of the previous 20 sessions (0 when absent), previous close > 100, mean
    (high - low) / close of the previous 14 sessions' EQ rows > 1.5%.  bclose_prev: previous session's close."""
    code = stv.isin_code[isin]
    days = [i for i, d in enumerate(stv.dates) if d[:7] == month]
    if not days:
        return {}
    a, b = max(0, days[0] - WIN), min(len(stv.dates) - 1, days[-1] + BE_AHEAD)
    h = stv.linked(s, code, a, b)
    out: Dict[str, JL] = {}
    for i in days:
        r = h.get(i)
        eq = r is not None and r[1] == 0
        lock = row_lock(r)[0] if r is not None else 0
        nxt = None
        for j in range(i + 1, min(i + NEXT_OPEN_MAX, b) + 1):
            x = h.get(j)
            if x is not None and x[1] == 0:
                nxt = x[3]
                break
        prev = [h[j] for j in range(max(a, i - WIN), i) if j in h]
        prev_eq = [x for x in prev if x[1] == 0]
        psurv = any(row_lock(x)[0] for x in prev_eq) or (
            bool(prev_eq) and min(x[5] for x in prev_eq) > 0
            and max(x[4] for x in prev_eq) / min(x[5] for x in prev_eq) >= SURV_HL)
        pbe = any(h[j][1] in TFT for j in range(i + 1, min(i + BE_AHEAD, b) + 1) if j in h)
        q20 = [h[j][8] if (j in h and h[j][1] == 0) else 0.0 for j in range(i - INPLAY_VOL_N, i)]
        rng = [(x[4] - x[5]) / x[6] for x in (h.get(j) for j in range(i - INPLAY_RANGE_N, i))
               if x is not None and x[1] == 0 and x[6] > 0]
        pc = h.get(i - 1)
        inplay = (eq and r is not None and len(q20) == INPLAY_VOL_N and st.mean(q20) > 0
                  and r[8] >= INPLAY_VOL_MULT * st.mean(q20) and pc is not None and pc[6] > INPLAY_MIN_PX
                  and bool(rng) and st.mean(rng) * 100 > INPLAY_RANGE_PCT)
        out[stv.dates[i]] = [int(eq), lock, r[4] if r else None, r[5] if r else None, r[6] if r else None, nxt,
                             int(psurv), int(pbe), int(inplay), pc[6] if pc else None]
    return out


# ---------------------------------------------------------------- symbol chains, labels (§2)
def load_symchange(path: Path = SYMCHG) -> Dict[str, List[Tuple[str, str]]]:
    """NSE symbolchange.csv -> {old: [(effective date YYYY-MM-DD, new), ...] sorted}."""
    out: Dict[str, List[Tuple[str, str]]] = defaultdict(list)
    with path.open(encoding="utf-8", errors="replace") as fh:
        rd = csv.reader(fh)
        next(rd)
        for r in rd:
            if len(r) < 4:
                continue
            old, new = r[1].strip(), r[2].strip()
            try:
                eff = datetime.strptime(r[3].strip(), "%d-%b-%Y").date().isoformat()
            except ValueError:
                continue
            if old and new and old != new:
                out[old].append((eff, new))
    return {k: sorted(v) for k, v in out.items()}


def chain_end(sym: str, as_of: str, sc: Dict[str, List[Tuple[str, str]]]) -> str:
    """Follow symbol changes effective AFTER as_of (the observation date), each from the previous change's date."""
    x, t, seen = sym, as_of, set()
    while True:
        nxt = [(e, n) for e, n in sc.get(x, []) if e > t]
        if not nxt:
            return x
        if x in seen:
            raise ValueError(f"symbol-change cycle at {x}")
        seen.add(x)
        t, x = nxt[0]


def member_keys(stv: Optional[Store], mem: Dict[str, List[str]], dates: Dict[str, str],
                sc: Dict[str, List[Tuple[str, str]]], rmap: Optional[Dict[str, Optional[str]]]) -> Dict[str, Set[str]]:
    """{month: keys of its F&O members}.  Keys: SYM:<chain end from the member date>, ISIN:<CM bhav ISIN that
    session> (when the store has the day), DATA:<survivorship.resolve> (when rmap is given and it resolves)."""
    out: Dict[str, Set[str]] = {}
    for m, xs in mem.items():
        d = dates[m]
        i = None
        if stv is not None:                  # the snapshot date may be a weekend session the store skips
            j = bisect.bisect_left(stv.dates, d)
            i = j if j < len(stv.dates) and stv.dates[j][:7] == m else None
        keys: Set[str] = set()
        for x in xs:
            keys.add("SYM:" + chain_end(x, d, sc))
            if stv is not None and i is not None:
                r = stv.day(x, i)
                if r is not None:
                    keys.add("ISIN:" + stv.isins[r[2]])
            if rmap is not None:
                y = V.resolve(x, rmap)
                if y is not None:
                    keys.add("DATA:" + y)
        out[m] = keys
    return out


def member_months_of(keys: Set[str], mk: Dict[str, Set[str]]) -> List[str]:
    return sorted(m for m, ks in mk.items() if keys & ks)


def label(member_months: Sequence[str], month: str) -> Optional[str]:
    """§2.  NEVER: no member month.  MEMBER: a member this month (cannot happen inside B150; P3 counts it).
    PRE-JOIN: first member month J after month, except month J-1 -> None (unlabelled, in BAND only).
    EX: a member in an earlier month and not now (also between two spells)."""
    if not member_months:
        return "NEVER"
    if month in member_months:
        return "MEMBER"
    j = member_months[0]
    if j > month:
        return None if month_add(j, -1) == month else "PRE-JOIN"
    return "EX"


def fo_dates_new() -> Dict[str, str]:
    """2020-01..2026-11 member month -> the date of its F&O snapshot (first session; fo_membership)."""
    return FM.first_sessions()


def load_old_membership() -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    x = read_json(FO_OLD_FILE)
    return x["members"], x["dates"]


def all_member_keys(stv: Optional[Store], sc: Dict[str, List[Tuple[str, str]]]) -> Dict[str, Set[str]]:
    """Member keys for 2015-01..2026-11: old bhavcopies (symbol chain + ISIN where the store has the day) and the
    2020-26 membership (+ DATA keys from the survivorship rename map)."""
    old, old_dates = load_old_membership()
    mk = member_keys(stv, old, old_dates, sc, None)
    new = V.load_membership()
    rmap = json.loads(V.MAP_FILE.read_text())
    mk.update(member_keys(stv, new, fo_dates_new(), sc, rmap))
    missing = [m for m in months(OLD_FROM, BAND_TO) if m not in mk]
    if missing:
        raise SystemExit(f"membership months missing: {missing[:6]} ({len(missing)})")
    return mk


def stock_id(ent: JD, res: Optional[Dict[str, JD]]) -> str:
    """One stock across its band entries: the resolved data symbol, else the GONE bhav symbol chain end."""
    if res is not None:
        x = res.get(ent_key(ent))
        if x is not None and x["ds"] is not None:
            return str(x["ds"])
    return "GONE:" + str(ent["chain"])


def ent_key(ent: JD) -> str:
    return f"{ent['sym']}|{ent['isin']}"


def entry_keys(ent: JD, res: Optional[Dict[str, JD]]) -> Set[str]:
    ks = {"SYM:" + ent["chain"], "ISIN:" + ent["isin"]}
    if res is not None:
        x = res.get(ent_key(ent))
        if x is not None and x["ds"] is not None:
            ks.add("DATA:" + x["ds"])
    return ks


def labels_by_month(bands: Dict[str, List[JD]], mk: Dict[str, Set[str]], res: Optional[Dict[str, JD]]
                    ) -> Tuple[Dict[str, Dict[str, Optional[str]]], Dict[str, List[str]]]:
    """-> ({month: {entry key: label}}, {stock id: member months}).  Keys of a stock = union over its entries."""
    keys: Dict[str, Set[str]] = defaultdict(set)
    for ents in bands.values():
        for e in ents:
            keys[stock_id(e, res)] |= entry_keys(e, res)
    mm = {sid: member_months_of(ks, mk) for sid, ks in keys.items()}
    out = {m: {ent_key(e): label(mm[stock_id(e, res)], m) for e in ents} for m, ents in bands.items()}
    return out, mm


# ---------------------------------------------------------------- B1b: old F&O bhavcopies (labels only)
def old_fo_candidates(month: str, cm_dates: Set[str]) -> List[str]:
    """Dates to try for the first session of `month`: CM session dates of that month when the CM store covers it,
    else the weekdays of days 1..10 (NSE 404s a holiday; the first OK is the first session)."""
    have = sorted(d for d in cm_dates if d[:7] == month)
    if have:
        return have[:3]
    y, mo = int(month[:4]), int(month[5:7])
    out = []
    for dd in range(1, 11):
        x = date(y, mo, dd)
        if x.weekday() < 5:
            out.append(x.isoformat())
    return out


def oldfo() -> None:
    """Fetch (resumable, 1 s pause, NSE archives) and build fo_membership_old.json {members, dates}."""
    FO_OLD_DIR.mkdir(parents=True, exist_ok=True)
    man = json.loads(C.MANIFEST.read_text()) if C.MANIFEST.exists() else {}
    cm_dates = {d for d, v in man.get("cm_old", {}).items() if v[0] == "ok"}
    members: Dict[str, List[str]] = {}
    dates: Dict[str, str] = {}
    failed = []
    for m in months(OLD_FROM, OLD_TO):
        got = sorted(FO_OLD_DIR.glob(f"{m}-*.zip"))
        if not got:
            for d in old_fo_candidates(m, cm_dates):
                code, body, err = C.get(FM.url(d))
                time.sleep(C.PAUSE)
                if code == 404:
                    continue
                if code != 200 or body is None:
                    print("FAILED", m, d, err, flush=True)
                    break
                f = FO_OLD_DIR / f"{d}.zip"
                C.write_atomic(f, body)
                if not zipfile.is_zipfile(f):
                    f.unlink()
                    print("NOT A ZIP", d, flush=True)
                    break
                got = [f]
                print("ok", d, len(body), flush=True)
                break
        if not got:
            failed.append(m)
            continue
        fs = FM.stock_futures(got[0])
        if len(fs) < 100:
            raise SystemExit(f"{got[0]}: only {len(fs)} stock futures: parse problem")
        members[m], dates[m] = sorted(fs), got[0].stem
    if failed:
        raise SystemExit(f"old F&O months without a file: {failed}")
    write_json(FO_OLD_FILE, dict(members=members, dates=dates))
    sizes = [len(v) for v in members.values()]
    ever = set().union(*map(set, members.values()))
    new = V.load_membership()
    left = set(members["2019-12"]) - set(new["2020-01"])
    print(f"old F&O: {len(members)} months, members per month {min(sizes)}..{max(sizes)}, ever {len(ever)}; "
          f"in 2019-12 but not 2020-01: {len(left)}; ever-old not in any 2020-26 month: "
          f"{len(ever - set().union(*map(set, new.values())))}")


# ---------------------------------------------------------------- B2: band command
def ensure_symchange() -> None:
    if not SYMCHG.exists():
        if not SYMCHG_SRC.exists():
            raise SystemExit(f"no symbolchange.csv at {SYMCHG_SRC}: download it from NSE first")
        NONFO.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SYMCHG_SRC, SYMCHG)


def build_bands(stv: Store, sc: Dict[str, List[Tuple[str, str]]]) -> Tuple[Dict[str, List[JD]], Dict[str, Counter[str]]]:
    mem = V.load_membership()
    bands: Dict[str, List[JD]] = {}
    reasons: Dict[str, Counter[str]] = {}
    for m in months(BAND_FROM, BAND_TO):
        ents, why = band(stv, m, set(mem[m]))
        r_i, _ = rebalance(stv, m)
        for e in ents:
            e["chain"] = chain_end(e["sym"], stv.dates[r_i], sc)
            e["rebal"] = stv.dates[r_i]
        bands[m], reasons[m] = ents, why
    return bands, reasons


def band_cmd() -> None:
    ensure_symchange()
    sc = load_symchange()
    stv = load_store(htf_calendar())
    print(f"store: {len(stv.dates)} sessions {stv.dates[0]}..{stv.dates[-1]}, {len(stv.syms)} symbols", flush=True)
    bands, reasons = build_bands(stv, sc)
    facts: Dict[str, Dict[str, Dict[str, JL]]] = {}
    for m, ents in bands.items():
        facts[m] = {ent_key(e): entry_facts(stv, e["sym"], e["isin"], m) for e in ents}
    write_json(BAND_FILE, dict(bands=bands, reasons={m: dict(c) for m, c in reasons.items()}))
    write_json(FACTS_FILE, facts)
    for m, ents in bands.items():
        write_json(B150_DIR / f"b150_{m}.json", dict(month=m, rebalance=ents[0]["rebal"] if ents else None,
                                                     names=[dict(symbol=e["sym"], isin=e["isin"], rank=e["rank"])
                                                            for e in ents]))
    mk = all_member_keys(stv, sc)
    print("\n".join(band_count_lines(bands, reasons, facts, mk, None)))


def band_count_lines(bands: Dict[str, List[JD]], reasons: Dict[str, Counter[str]], facts: Dict[str, Dict[str, Dict[str, JL]]],
                     mk: Dict[str, Set[str]], res: Optional[Dict[str, JD]]) -> List[str]:
    """B2 counts only: sizes, filters, T60 of ranks 1/75/150 per year, labels per month, lock days, voids,
    share of band stock-months already on disk."""
    labs, mm = labels_by_month(bands, mk, res)
    L_ = [f"B2 COUNTS ({'with' if res else 'before'} resolution). months {len(bands)}"]
    sizes = [len(v) for v in bands.values()]
    L_.append(f"  B150 size per month: min {min(sizes)} max {max(sizes)}; months != {N_BAND}: "
              f"{sum(s != N_BAND for s in sizes)}")
    tot: Counter[str] = Counter()
    for rc in reasons.values():
        tot.update(rc)
    L_.append(f"  first failing filter, summed over months (EQ on rebalance day = candidate pool): {dict(tot)}")
    ever = {stock_id(e, res) for v in bands.values() for e in v}
    L_.append(f"  ever-band stocks: {len(ever)}; bhav symbol|ISIN entries: "
              f"{len({ent_key(e) for v in bands.values() for e in v})}")
    for y in sorted({m[:4] for m in bands}):
        ms = [m for m in bands if m.startswith(y)]
        t = [[e["t60"] for e in bands[m]] for m in ms]
        def pick(k: int, t: List[List[float]] = t) -> float:
            return float(st.median(x[k] for x in t if len(x) > k)) / 1e7
        L_.append(f"  {y}: T60 Rs Cr, median over months, of rank 1 {pick(0):.1f} / 75 {pick(74):.1f} / "
                  f"150 {pick(149):.1f}")
    L_.append("  month | NEVER PRE-JOIN EX unlabelled(J-1) MEMBER | lock days | non-EQ days | in-play days")
    for m in sorted(bands):
        cm: Counter[Optional[str]] = Counter(labs[m].values())
        f = facts.get(m, {})
        lk = sum(1 for e in f.values() for v in e.values() if v[1])
        ne = sum(1 for e in f.values() for v in e.values() if not v[0])
        ip = sum(1 for e in f.values() for v in e.values() if v[8])
        L_.append(f"  {m} | {cm['NEVER']:3d} {cm['PRE-JOIN']:3d} {cm['EX']:3d} {cm[None]:3d} {cm['MEMBER']:3d} | {lk:4d} | "
                  f"{ne:4d} | {ip:4d}")
    by_y: Dict[str, Counter[Optional[str]]] = defaultdict(Counter)
    for m in bands:
        by_y[m[:4]].update(labs[m].values())
    for y, cy in sorted(by_y.items()):
        L_.append(f"  {y} stock-months: NEVER {cy['NEVER']} PRE-JOIN {cy['PRE-JOIN']} EX {cy['EX']} J-1 {cy[None]} "
                  f"MEMBER {cy['MEMBER']}")
    ever_label = Counter("NEVER" if not v else "EVER-F&O" for v in mm.values())
    L_.append(f"  stocks: {dict(ever_label)}")
    on_disk = sum(1 for v in bands.values() for e in v if raw_file(e["sym"]) is not None)
    L_.append(f"  band stock-months whose bhav symbol has a Kite raw file on disk: {on_disk} of {sum(sizes)}")
    return L_


# ---------------------------------------------------------------- data files
def raw_file(s: str) -> Optional[Tuple[Path, bool]]:
    """(directory, merge y2026?) of the first raw file of data symbol s in RAW_DIRS order, or None."""
    for d, y26 in RAW_DIRS:
        if (d / f"{F.fname(s)}.json.gz").exists():
            return d, y26
    return None


def cached_raw(s: str) -> bool:
    """A raw file from the cached (pre-study) fetches: full_raw / surv_raw, durable copy or scratchpad."""
    x = raw_file(s)
    return x is not None and x[0] != NONFO_RAW


# ---------------------------------------------------------------- B3a: resolution (DOUBLE_ENGINE §4 D3)
def kite_symbols(refresh: bool = False) -> Set[str]:
    """Today's Kite NSE tradingsymbols (ONE instruments call, cached with its date)."""
    if KITE_FILE.exists() and not refresh:
        return set(read_json(KITE_FILE)["symbols"])
    from kiteconnect import KiteConnect  # type: ignore[import-untyped]
    tok = json.loads((HERE.parent / "data/kite_access_token.json").read_text())
    k = KiteConnect(api_key=tok["api_key"])
    k.set_access_token(tok["access_token"])
    syms = sorted({i["tradingsymbol"] for i in k.instruments("NSE")})
    write_json(KITE_FILE, dict(fetched=datetime.now().isoformat(timespec="seconds"), symbols=syms))
    return set(syms)


ALIAS_SUFFIXES = ("-BE", "-BZ")


def on_kite(s: str, kite: Set[str]) -> Optional[str]:
    """Kite tradingsymbol for data symbol s: s itself, else a series-suffixed alias (HFCL -> HFCL-BE)."""
    if s in kite:
        return s
    for suf in ALIAS_SUFFIXES:
        if s + suf in kite:
            return s + suf
    return None


def resolve_one(sym: str, isin: str, as_of: str, kite: Set[str], latest_isin: Dict[str, str],
                sc: Dict[str, List[Tuple[str, str]]]) -> JD:
    """D3 order, frozen: (1) the same symbol on Kite today -> (2) ISIN -> today's symbol (latest CM bhavcopy, in
    place of EQUITY_L.csv: same mapping for every traded name) -> (3) symbolchange chain -> else GONE.
    suspect = step 1 hit although symbolchange says the symbol was renamed away after as_of (possible reuse; the
    identity check after the fetch decides)."""
    ce = chain_end(sym, as_of, sc)
    k = on_kite(sym, kite)
    if k is not None:
        return dict(ds=sym, kite=k, how="symbol", suspect=ce != sym)
    t = latest_isin.get(isin)
    if t is not None:
        k = on_kite(t, kite)
        if k is not None:
            return dict(ds=t, kite=k, how="isin", suspect=False)
    k = on_kite(ce, kite)
    if k is not None:
        return dict(ds=ce, kite=k, how="symbolchange", suspect=False)
    return dict(ds=None, kite=None, how="GONE", suspect=False)


def latest_isin_map(stv: Store) -> Dict[str, str]:
    i = len(stv.dates) - 1
    out: Dict[str, str] = {}
    for s in sorted(stv.syms):
        r = stv.day(s, i)
        if r is not None:
            out.setdefault(stv.isins[r[2]], s)
    return out


def load_bands() -> Dict[str, List[JD]]:
    return read_json(BAND_FILE)["bands"]          # type: ignore[no-any-return]


def resolve_cmd() -> None:
    ensure_symchange()
    sc = load_symchange()
    stv = load_store(htf_calendar())
    bands = load_bands()
    kite = kite_symbols()
    lat = latest_isin_map(stv)
    res: Dict[str, JD] = {}
    for m in sorted(bands):
        for e in bands[m]:
            k = ent_key(e)
            if k not in res:
                res[k] = dict(resolve_one(e["sym"], e["isin"], e["rebal"], kite, lat, sc), sym=e["sym"],
                              isin=e["isin"], first=m)
    write_json(RESOLVE_FILE, res)
    how = Counter(x["how"] for x in res.values())
    print(f"RESOLVE: {len(res)} band entries -> {dict(how)}; aliases {sum(x['kite'] not in (None, x['ds']) for x in res.values())}; "
          f"suspect symbol reuse {sum(x['suspect'] for x in res.values())}")
    print("  GONE entries: " + ", ".join(sorted(x["sym"] for x in res.values() if x["ds"] is None)))
    sus = sorted(x["sym"] for x in res.values() if x["suspect"])
    if sus:
        print("  suspect (step 1 hit, symbolchange says renamed away): " + ", ".join(sus))
    ds_months: Dict[Tuple[str, str], int] = Counter()
    for m, ents in bands.items():
        for e in ents:
            x = res[ent_key(e)]
            if x["ds"] is not None:
                ds_months[(m, x["ds"])] += 1
    dup = sum(1 for v in ds_months.values() if v > 1)
    print(f"  months with two band entries on one data symbol: {dup}")
    mk = all_member_keys(stv, sc)
    labs, mm = labels_by_month(bands, mk, res)
    write_json(LABELS_FILE, dict(labels=labs, member_months=mm))
    reasons = {m: Counter(c) for m, c in read_json(BAND_FILE)["reasons"].items()}
    facts = read_json(FACTS_FILE)
    print("\n".join(band_count_lines(bands, reasons, facts, mk, res)))
    print("\n".join(label_cross_check(bands, mk, res)))
    print("\n".join(old_unresolved_lines(stv, sc)))


def ever_pit() -> Set[str]:
    sets = V.eligible_sets(V.load_membership(), json.loads(V.MAP_FILE.read_text()))
    return set().union(*sets.values())


def label_cross_check(bands: Dict[str, List[JD]], mk: Dict[str, Set[str]], res: Dict[str, JD]) -> List[str]:
    """Must be 0: band stocks whose data symbol is an ever-PIT member (survivorship identities) labelled NEVER."""
    _, mm = labels_by_month(bands, mk, res)
    pit = ever_pit()
    bad = sorted(sid for sid, ms in mm.items() if not ms and sid in pit)
    return [f"  CROSS-CHECK ever-PIT data symbols labelled NEVER (must be 0): {len(bad)} {bad[:20]}"]


def old_unresolved_lines(stv: Store, sc: Dict[str, List[Tuple[str, str]]]) -> List[str]:
    """§2: old (2015-01..2019-12) F&O symbols whose symbol-chain end never appears in the CM bhavcopy from 2018-12
    on are UNRESOLVED: listed by name (they cannot be linked to a band stock by symbol; a band stock that is one
    of them under an unrecorded new symbol would wrongly read NEVER)."""
    old, dates = load_old_membership()
    seen: Dict[str, str] = {}
    for m in sorted(old):
        for x in old[m]:
            seen.setdefault(x, dates[m])
    last = {x: max(dates[m] for m in old if x in old[m]) for x in seen}
    un = sorted(x for x in seen if chain_end(x, last[x], sc) not in stv.syms)
    return [f"  OLD F&O symbols 2015-19: {len(seen)}; unresolved (chain end never in the CM bhavcopy): {len(un)} "
            f"{un}"]


# ---------------------------------------------------------------- B3b: coverage gate (§7.1)
def row_calendar() -> List[str]:
    """The C-FO session calendar (union of the PIT SRS row dates, == survivorship.report days); cached."""
    if CAL_FILE.exists():
        return read_json(CAL_FILE)            # type: ignore[no-any-return]
    _, _, rows218, rows_new = V.SETUPS[0]
    days: Set[str] = set()
    for _s, f in V.pit_files(rows218, rows_new, json.loads(V.MAP_FILE.read_text()), V.load_membership()):
        if f.exists():
            days.update(r[0] for r in json.loads(f.read_text()))
    cal = sorted(d for d in days if d <= LAST_DAY)
    write_json(CAL_FILE, cal)
    return cal


def gone_set(res: Dict[str, JD], post_fetch: bool) -> Dict[str, str]:
    """{entry key: reason} of band entries without usable data.  Pre-fetch: unresolved.  Post-fetch adds: no
    5-min bars (nobars.json), identity failure (identity.json), resolved but no raw file."""
    out = {k: "unresolved" for k, x in res.items() if x["ds"] is None}
    if post_fetch:
        nob = set(read_json(NOBARS_FILE)) if NOBARS_FILE.exists() else set()
        ident = read_json(IDENT_FILE) if IDENT_FILE.exists() else {}
        for k, x in res.items():
            if k in out:
                continue
            if x["ds"] in nob:
                out[k] = "nobars"
            elif not ident.get(k, {}).get("ok", True):
                out[k] = "identity"
            elif raw_file(x["ds"]) is None:
                out[k] = "no raw file"
    return out


def row_holes(bands: Dict[str, List[JD]], res: Dict[str, JD], gone: Dict[str, str], cal: List[str]
              ) -> Dict[Tuple[str, str], Set[str]]:
    """Owner 2026-11-23 (NONFO_SRS_DESIGN.md): ROW HOLES count as GONE in the gate.  A hole = a row-calendar session
    of a band month on which a resolved, non-GONE entry's SRS row file has no row (no Kite 5-min session: no trade
    is possible that day).  -> {(entry key, month): hole dates}.  A missing row file raises (run `rows` first)."""
    by_month: Dict[str, List[str]] = defaultdict(list)
    for d in cal:
        by_month[d[:7]].append(d)
    have: Dict[str, Set[str]] = {}
    out: Dict[Tuple[str, str], Set[str]] = {}
    for m, ents in bands.items():
        for e in ents:
            k = ent_key(e)
            if k in gone:
                continue
            ds = res[k]["ds"]
            if ds not in have:
                f = rows_file(ds, "SRS")
                if f is None:
                    raise SystemExit(f"row holes: {ds} has no SRS row file: run rows")
                have[ds] = {r[0] for r in json.loads(f.read_text())}
            miss = {d for d in by_month[m] if d not in have[ds]}
            if miss:
                out[(k, m)] = miss
    return out


def coverage(bands: Dict[str, List[JD]], labs: Dict[str, Dict[str, Optional[str]]],
             facts: Dict[str, Dict[str, Dict[str, JL]]], gone: Dict[str, str], cal: List[str],
             holes: Optional[Dict[Tuple[str, str], Set[str]]] = None) -> Dict[str, JD]:
    """Per year and label: member-days (row-calendar sessions of the month), GONE member-days, in-play proxy days
    (bhav facts on row-calendar sessions), GONE in-play days.  GONE = the whole month of a GONE entry, plus (post-
    fetch, owner 2026-11-23) the row-hole days of a non-GONE entry (`holes`, also counted alone in 'holes')."""
    by_month: Dict[str, List[str]] = defaultdict(list)
    for d in cal:
        by_month[d[:7]].append(d)
    holes = holes or {}
    out: Dict[str, JD] = {}
    for m, ents in bands.items():
        y = m[:4]
        for e in ents:
            k = ent_key(e)
            lab = labs[m][k]
            for grp in ("BAND", str(lab)):
                c = out.setdefault(f"{y}|{grp}", dict(days=0, gone=0, inplay=0, inplay_gone=0, holes=0,
                                                      gone_by=Counter()))
                n = len(by_month[m])
                ip_days = {d for d in by_month[m] if facts[m][k].get(d, [0] * 10)[8]}
                c["days"] += n
                c["inplay"] += len(ip_days)
                if k in gone:
                    c["gone"] += n
                    c["inplay_gone"] += len(ip_days)
                    c["gone_by"][e["sym"]] += n
                elif (k, m) in holes:
                    h = holes[(k, m)]
                    c["gone"] += len(h)
                    c["holes"] += len(h)
                    c["inplay_gone"] += len(h & ip_days)
                    c["gone_by"][e["sym"]] += len(h)
    return out


def gate(cov: Dict[str, JD]) -> JD:
    """§7.1.  A year FAILS if GONE > 5% of its NEVER member-days (a year with no NEVER member-day fails: nothing
    to read).  READ WINDOW = the longest run of consecutive passing years ending at 2026 (empty if 2026 fails).
    STOP if READ WINDOW starts after 2022 (or is empty), or GONE share of NEVER in-play days 2024-26 > 10%."""
    years = list(range(GATE_FIRST_YEAR, GATE_LAST_YEAR + 1))
    share: Dict[int, Optional[float]] = {}
    for y in years:
        c = cov.get(f"{y}|NEVER")
        share[y] = (c["gone"] / c["days"]) if c and c["days"] else None
    passing = {y: share[y] is not None and share[y] <= GATE_MAX_GONE for y in years}  # type: ignore[operator]
    run: List[int] = []
    for y in reversed(years):
        if not passing[y]:
            break
        run.insert(0, y)
    ip = [cov.get(f"{y}|NEVER") for y in (2024, 2025, 2026)]
    ipd = sum(c["inplay"] for c in ip if c)
    ipg = sum(c["inplay_gone"] for c in ip if c)
    ip_share = ipg / ipd if ipd else None
    stop = []
    if not run or run[0] > GATE_START_BY:
        stop.append(f"READ WINDOW {run[0] if run else 'empty'}..2026 starts after {GATE_START_BY}-01")
    if ip_share is None or ip_share > GATE_INPLAY_STOP:
        stop.append(f"GONE share of NEVER in-play proxy days 2024-26 = "
                    f"{'undefined' if ip_share is None else f'{ip_share:.1%}'} > {GATE_INPLAY_STOP:.0%}")
    return dict(share=share, passing=passing, read=run, inplay_share_2426=ip_share, stop=stop)


def read_window(g: JD) -> Tuple[str, str]:
    if not g["read"]:
        raise SystemExit("READ WINDOW is empty: the coverage gate failed")
    return f"{g['read'][0]}-01-01", LAST_DAY


def coverage_lines(cov: Dict[str, JD], g: JD, title: str) -> List[str]:
    L_ = [title, "  YEAR | label | member-days | GONE incl. row holes (share) [of which holes] | in-play days | "
                 "GONE in-play (share)"]
    for key in sorted(cov):
        c = cov[key]
        y, grp = key.split("|")
        L_.append(f"  {y} | {grp:8s} | {c['days']:7d} | {c['gone']:6d} ({c['gone'] / max(1, c['days']):.2%}) "
                  f"[{c.get('holes', 0)}] | "
                  f"{c['inplay']:6d} | {c['inplay_gone']:5d} ({c['inplay_gone'] / max(1, c['inplay']):.2%})")
    L_.append("  gate on NEVER: " + ", ".join(
        f"{y} {'-' if s is None else format(s, '.2%')} {'PASS' if g['passing'][y] else 'FAIL'}"
        for y, s in g["share"].items()))
    ips = g["inplay_share_2426"]
    L_.append(f"  READ WINDOW years: {g['read']}; GONE share of NEVER in-play days 2024-26: "
              f"{'-' if ips is None else format(ips, '.2%')}")
    L_.append("  " + ("STOP before the fetch, back to the owner: " + "; ".join(g["stop"]) if g["stop"]
                      else "GATE: GO (no stop condition)"))
    gb: Counter[str] = Counter()
    for key, c in cov.items():
        if key.endswith("|NEVER"):
            gb.update(c["gone_by"])
    L_.append(f"  GONE NEVER member-days by bhav symbol (top 30): {dict(gb.most_common(30))}")
    return L_


def coverage_context(post_fetch: bool) -> Tuple[Dict[str, JD], JD, Dict[str, JD], Dict[str, str]]:
    """Labels from labels.json (written by `resolve`), so neither coverage nor report loads the store."""
    bands, res, facts = load_bands(), read_json(RESOLVE_FILE), read_json(FACTS_FILE)
    labs = read_json(LABELS_FILE)["labels"]
    gone = gone_set(res, post_fetch)
    cal = row_calendar()
    holes = row_holes(bands, res, gone, cal) if post_fetch else None    # owner 2026-11-23: holes count as GONE
    cov = coverage(bands, labs, facts, gone, cal, holes)
    return cov, gate(cov), res, gone


def coverage_cmd() -> None:
    cov, g, _res, gone = coverage_context(post_fetch=NOBARS_FILE.exists() or IDENT_FILE.exists())
    print("\n".join(coverage_lines(cov, g, f"COVERAGE GATE (§7.1); GONE reasons {dict(Counter(gone.values()))}")))


# ---------------------------------------------------------------- B4: fetch
def empty_raw(f: Path) -> bool:
    with gzip.open(f, "rt") as g:
        x = json.load(g)
    return not x["daily"] or not x["m5"]


def record_nobars() -> Set[str]:
    """Hazard 2 (§9): fetch_parallel's sweep deletes a file with empty daily / m5 and would fetch it forever.
    Record every such data symbol in nobars.json (GONE) BEFORE each fetch call and after it returns."""
    nob: Set[str] = set(read_json(NOBARS_FILE)) if NOBARS_FILE.exists() else set()
    res = read_json(RESOLVE_FILE)
    by_fname = {F.fname(x["ds"]): x["ds"] for x in res.values() if x["ds"]}
    if NONFO_RAW.exists():
        for f in NONFO_RAW.glob("*.json.gz"):
            try:
                if empty_raw(f):
                    nob.add(by_fname.get(f.name[:-8], f.name[:-8]))
            except (OSError, ValueError, EOFError, KeyError):
                pass                                # a damaged file: fetch_parallel deletes and re-fetches it
    write_json(NOBARS_FILE, sorted(nob))
    return nob


def fetch_list(res: Dict[str, JD], gone: Dict[str, str], nob: Set[str]) -> Tuple[List[str], Dict[str, str]]:
    """New band data symbols to fetch: resolved, not GONE, no raw file anywhere, not recorded as nobars.
    Hazard 1 (§9): every name is on Kite by construction (resolve_one), so eq[symbol] cannot raise.
    P9: an ever-PIT member is never fetched (raises)."""
    pit = ever_pit()
    want: Dict[str, str] = {}
    for k, x in res.items():
        if k in gone or x["ds"] is None or x["ds"] in nob:
            continue
        if raw_file(x["ds"]) is None:
            want[x["ds"]] = x["kite"]
    bad = sorted(set(want) & pit)
    if bad:
        raise SystemExit(f"P9 violated: ever-PIT members would be re-fetched: {bad}")
    return sorted(want), {s: k for s, k in want.items() if k != s}


def fetch_cmd() -> None:
    cov, g, res, gone = coverage_context(post_fetch=False)
    print("\n".join(coverage_lines(cov, g, "COVERAGE GATE before the fetch (§7.1)")))
    if g["stop"]:
        raise SystemExit("fetch REFUSED: the coverage gate says STOP (back to the owner)")
    nob = record_nobars()
    todo, alias = fetch_list(res, gone, nob)
    print(f"fetch: {len(todo)} new names ({len(alias)} aliased) into {NONFO_RAW}; nobars so far {len(nob)}", flush=True)
    t0 = time.time()
    F.fetch_parallel(symbols=todo, raw=NONFO_RAW, m5_to=V.M5_TO, kite_alias=alias)
    nob = record_nobars()
    have = sum(1 for s in todo if (NONFO_RAW / f"{F.fname(s)}.json.gz").exists())
    print(f"fetch done in {(time.time() - t0) / 60:.1f} min: files {have} of {len(todo)}; nobars (GONE) {len(nob)} "
          f"{sorted(nob)[:30]}")


# ---------------------------------------------------------------- B5: rows, features, lock post-pass facts, identity
def rows_file(ds: str, setup: str) -> Optional[Path]:
    """Row file of data symbol ds: the cached PIT row folders first (ever-F&O names are never rebuilt), then ours."""
    if V.SETUPS[0][0] != "SRS" or V.SETUPS[1][0] != "ASRS":
        raise RuntimeError("survivorship.SETUPS order changed")
    full, surv = (V.SETUPS[0][2], V.SETUPS[0][3]) if setup == "SRS" else (V.SETUPS[1][2], V.SETUPS[1][3])
    for d in (full, surv, ROWS_DIRS[setup]):
        f = d / f"{F.fname(ds)}.json"
        if f.exists():
            return f
    return None


def feat_of(daily: List[JD], row_dates: Iterable[str]) -> Dict[str, JL]:
    """{d: [T, ATR%, Kite close]} of the last daily bar before d: T = liq_side.turnover_sma (SMA20 close x volume
    to d-1), ATR% = full_asrs.scan_series Wilder ATR(14) / close of d-1 x 100 (None when not yet defined)."""
    dates = [b["d"] for b in daily]
    sstats = F.scan_series(daily)
    out: Dict[str, JL] = {}
    for d in row_dates:
        j = bisect.bisect_left(dates, d) - 1
        x = sstats[dates[j]] if j >= 0 else None
        out[d] = [L.turnover_sma(daily, d, dates=dates), (x[1] / x[2] * 100) if x else None,
                  daily[j]["c"] if j >= 0 else None]
    return out


def bar_hl(b: Optional[JD]) -> Optional[List[float]]:
    return None if b is None else [b["h"], b["l"]]


def lock_record(day_bars: List[JD], d: str, sig_n: int) -> Optional[JD]:
    """The trade exactly as full_asrs.stock_rows builds it (fill_gaps -> completed -> trade), plus the bars the
    post-pass needs: the entry (trigger) bar, the bar after it, and the 15:10 bar (sim.EXIT_IDX) when held to it."""
    bars, _ = F.fill_gaps(day_bars)
    if not bars:
        return None
    bars = S.completed(bars, None)
    t = S.trade(bars, d, sig_n) if bars else None
    if not t or not t["trig"]:
        return None
    ei = next(i for i, b in enumerate(bars) if b["t"][11:] == t["entry_t"])
    xi = sim.EXIT_IDX
    held = t["reason"] != "stop" and len(bars) > xi and t["exit_t"] == bars[xi]["t"][11:]
    return dict(side=t["side"], entry=t["entry"], exit=t["exit"], reason=t["reason"], held=held,
                eb=bar_hl(bars[ei]), nb=bar_hl(bars[ei + 1] if ei + 1 < len(bars) else None),
                xb=bar_hl(bars[xi]) if held else None)


def identity_series(stv: Store, ent: JD, band_ms: List[str]) -> List[Tuple[str, float]]:
    """CM bhav closes of one band entry (linked history) on the rebalance day of every month from 2 before its first
    band month to 2 after its last (the SURVIVORSHIP Amendment-1 return check needs >= 3 pairs)."""
    code = stv.isin_code[ent["isin"]]
    out = []
    for m in months(month_add(band_ms[0], -2), min(month_add(band_ms[-1], 2), BAND_TO)):
        try:
            r_i, _ = rebalance(stv, m)
        except ValueError:
            continue
        h = stv.linked(ent["sym"], code, r_i, r_i)
        if r_i in h and h[r_i][1] == 0 and h[r_i][6] > 0:
            out.append((stv.dates[r_i], float(h[r_i][6])))
    return out


def band_months_of(bands: Dict[str, List[JD]]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = defaultdict(list)
    for m in sorted(bands):
        for e in bands[m]:
            out[ent_key(e)].append(m)
    return out


def rows_cmd() -> None:
    """New names: SRS + ASRS rows (full_asrs.stock_rows).  Every ever-band name and every ever-PIT name: feature
    file; every ever-band name: lock post-pass records on its band-month LOCK-DAYs and the identity check."""
    bands, res, facts = load_bands(), read_json(RESOLVE_FILE), read_json(FACTS_FILE)
    stv = load_store(htf_calendar())
    nob = record_nobars()
    gone = gone_set(res, post_fetch=False)
    bmo = band_months_of(bands)
    by_ds: Dict[str, List[str]] = defaultdict(list)
    for k, x in res.items():
        if k not in gone and x["ds"] is not None and x["ds"] not in nob:
            by_ds[x["ds"]].append(k)
    names = sorted(set(by_ds) | ever_pit())
    ident: Dict[str, JD] = read_json(IDENT_FILE) if IDENT_FILE.exists() else {}
    cnt: Counter[str] = Counter()
    t0 = time.time()
    for n_done, ds in enumerate(names):
        rf = raw_file(ds)
        if rf is None:
            cnt["no raw file"] += 1
            continue
        raw_dir, y26 = rf
        if raw_dir == NONFO_RAW:
            for setup, out_dir in ROWS_DIRS.items():
                f = out_dir / f"{F.fname(ds)}.json"
                if not f.exists():
                    write_json(f, F.stock_rows(ds, SIG_N[setup], raw=NONFO_RAW, y2026=False))
                    cnt[f"rows built {setup}"] += 1
        ff, lf = FEAT_DIR / f"{F.fname(ds)}.json", LOCK_DIR / f"{F.fname(ds)}.json"
        need_ident = [k for k in by_ds.get(ds, []) if k not in ident]
        if ff.exists() and (lf.exists() or ds not in by_ds) and not need_ident:
            continue
        srs = rows_file(ds, "SRS")
        if srs is None:
            cnt["no SRS row file"] += 1
            continue
        daily, m5 = F.load_stock(ds, raw=raw_dir, y2026=y26)
        if not ff.exists():
            write_json(ff, feat_of(daily, [r[0] for r in json.loads(srs.read_text())]))
        if ds in by_ds and not lf.exists():
            lock_days = sorted({d for k in by_ds[ds] for m in bmo[k] for d, v in facts[m][k].items() if v[1]})
            want = set(lock_days)
            sess = Y.full_sessions([b for b in m5 if b["t"][:10] in want]) if want else {}
            write_json(lf, {d: {setup: lock_record(sess[d], d, SIG_N[setup]) for setup in SIG_N}
                            for d in lock_days if d in sess})
        kc = {b["d"]: b["c"] for b in daily}
        for k in need_ident:
            ent = next(e for e in bands[bmo[k][0]] if ent_key(e) == k)
            rr = V.check_returns(identity_series(stv, ent, bmo[k]), kc)
            ok, why = V.accept_returns(rr, False)
            ident[k] = dict(ok=ok, why=why, pairs=rr["pairs"], agree=rr["agree"], ds=ds)
        if need_ident:
            write_json(IDENT_FILE, ident)
        if n_done < 20 or n_done % 100 == 0:
            print(f"  {n_done + 1}/{len(names)} {ds} {time.time() - t0:.0f}s", flush=True)
    fails = sorted(f"{k}->{v['ds']} ({v['why']})" for k, v in ident.items() if not v["ok"])
    print(f"rows: {dict(cnt)}; identity checked {len(ident)}, FAIL {len(fails)}: {fails[:30]}; "
          f"{(time.time() - t0) / 60:.1f} min")


# ---------------------------------------------------------------- §4 post-pass (pure)
# UNITS (review 2026-11-23): the rows and the lock records are in Kite ADJUSTED prices (splits / bonuses /
# dividends back-adjusted), the bhav facts in traded prices.  Every bhav price the post-pass compares with or books
# against a row is first multiplied by `scale` = Kite close(d-1) / bhav close(d-1) (kite_scale).  Without it, 107 of
# the 349 SRS lock-day trades (factor 0.06-2.2) could never match and a locked short was booked at bhav close x 1.20
# against a Kite-unit entry.  The d-1 ratio is the right factor on every day except an ex-date (d itself); the
# range guard in post_pass catches that case loudly.  A same-day factor from the next feature row was tried and is
# WRONG around sessions missing from the row calendar (2020-03-13 / 03-23 halts, 2024-05-18 Saturday).
LOCK_PX_TOL = 2e-4                           # a bar price "at" the day's extreme: within 0.02% ...
LOCK_TICK = 0.05                             # ... plus one tick (adjusted prices are rounded; erring = penalising)
SCALE_GUARD = 0.005                          # entry and exit must lie in the scaled bhav day range +- 0.5%
NEXT_OPEN_BAND = (0.6, 1.6)                  # locked long: next open / close outside this = an ex-date between


def at_px(x: float, ref: Optional[float]) -> bool:
    return ref is not None and ref > 0 and abs(x - ref) <= LOCK_PX_TOL * ref + LOCK_TICK


def frozen_at(hl: Optional[List[float]], ref: Optional[float]) -> bool:
    return hl is not None and hl[0] == hl[1] and at_px(hl[0], ref)


def kite_scale(fact: JL, feat_row: Optional[JL]) -> float:
    """Kite adjusted units per bhav unit on day d: Kite close(d-1) (feature row of d) / bhav close(d-1) (fact[9]).
    Raises when either is missing: a lock-day trade is never post-passed in mixed units."""
    if feat_row is None or feat_row[2] is None or fact[9] is None or fact[9] <= 0 or feat_row[2] <= 0:
        raise ValueError("no Kite / bhav close of d-1 to scale the lock-day facts")
    return float(feat_row[2]) / float(fact[9])


def scaled(fact: JL, scale: float) -> JL:
    """The fact with its price fields (hi, lo, close, next_open) in Kite units."""
    return [fact[0], fact[1]] + [None if v is None else v * scale for v in fact[2:6]] + list(fact[6:])


def void_series(fact: Optional[JL]) -> bool:
    """§4 Series: void unless the stock is in series EQ in the bhavcopy of day d (no row that day = void)."""
    return fact is None or not fact[0]


def locked_entry(side: int, fact: JL, lp: Optional[JD]) -> bool:
    """§4 Locked ENTRY: void if the trigger bar AND the next bar are frozen (H = L) at the day's extreme in the
    trade's direction on a LOCK-DAY: a long at the high of an upper-lock day, a short at the low of a lower-lock."""
    if lp is None or fact[1] == 0 or fact[1] != side:
        return False
    ref = fact[2] if side > 0 else fact[3]
    return frozen_at(lp["eb"], ref) and frozen_at(lp["nb"], ref)


def locked_exit(side: int, fact: JL, lp: Optional[JD]) -> Optional[Tuple[float, str]]:
    """§4 Locked exit (DOUBLE_ENGINE §3, Q10): held to 15:10 and the 15:10 bar frozen at the day's extreme AGAINST
    the position on a LOCK-DAY.  Short on an upper lock -> bhav CLOSE x 1.20.  Long on a lower lock -> the next
    session's open (none within 10 sessions -> bhav close, tagged).  `fact` must already be in row (Kite) units
    (post_pass scales it).  A next open outside 0.6-1.6 x the close means a split / bonus ex-date between d and
    that open (the d-1 scale no longer applies): ValueError, never a silent price."""
    if lp is None or not lp["held"] or fact[1] == 0 or fact[1] != -side:
        return None
    ref = fact[2] if side < 0 else fact[3]
    if not frozen_at(lp["xb"], ref):
        return None
    if side < 0:
        return fact[4] * SHORT_PENALTY, "locked short: close x 1.20"
    if fact[5] is None:
        return fact[4], "locked long: NO next open, bhav close used"
    if not NEXT_OPEN_BAND[0] <= fact[5] / fact[4] <= NEXT_OPEN_BAND[1]:
        raise ValueError(f"locked long: next open {fact[5]} vs close {fact[4]}: an ex-date before the next open?")
    return fact[5], "locked long: next open"


def repriced(r: JL, exit_px: float) -> JL:
    """The row with a new exit price: cost per share and r_net recomputed at the rows' base 0.02% slippage."""
    side, entry, risk = r[3], r[4], r[7]
    cps = S.cost_per_share(side, entry, exit_px)
    return [r[0], r[1], r[2], side, entry, r[5], exit_px, risk, (side * (exit_px - entry) - cps) / risk, cps]


def post_pass(r: JL, fact: Optional[JL], lp: Optional[JD], scale: float = 1.0) -> Tuple[JL, str]:
    """Band arms only.  -> (row, status).  A void keeps its slot as side 0 (no trade, no backfill).
    scale: Kite units per bhav unit on a LOCK-DAY (kite_scale); the bhav facts are scaled before any comparison
    and before the locked-exit price is booked.  Guard: the row's entry and exit must lie inside the scaled bhav
    day range (+-0.5%), else ValueError (an ex-date or a wrong factor is never post-passed silently)."""
    if r[3] == 0:
        return r, "untriggered"
    if void_series(fact):
        return r[:3] + [0] + r[4:], "void_series"
    assert fact is not None
    if fact[1] != 0:
        if lp is None:
            raise ValueError(f"{r[0]}: triggered on a LOCK-DAY but no lock post-pass record")
        if lp["side"] != r[3] or abs(lp["entry"] - r[4]) > 1e-6:
            raise ValueError(f"{r[0]}: lock record does not match the stored row")
        fs = scaled(fact, scale)
        lo_, hi_ = fs[3] * (1 - SCALE_GUARD), fs[2] * (1 + SCALE_GUARD)
        if not (lo_ <= r[4] <= hi_ and lo_ <= r[6] <= hi_):
            raise ValueError(f"{r[0]}: entry {r[4]} / exit {r[6]} outside the scaled bhav range "
                             f"[{fs[3]}, {fs[2]}] (scale {scale:.4f}): ex-date or wrong factor")
        if locked_entry(r[3], fs, lp):
            return r[:3] + [0] + r[4:], "void_locked_entry"
        x = locked_exit(r[3], fs, lp)
        if x is not None:
            return repriced(r, x[0]), x[1]
    return r, "ok"


# ---------------------------------------------------------------- money, slippage, statistics
def size_cap(entry: float, stop: float, cap: float) -> int:
    """full_asrs.size with a notional cap argument (the Rs2.5L column); == F.size at cap = F.NOTIONAL_CAP."""
    risk_ps = abs(entry - stop)
    if risk_ps <= 0 or entry <= 0:
        return 0
    return int(min(math.floor(F.RISK / risk_ps), math.floor(cap / entry)))


def tier_slip(t: Optional[float]) -> float:
    """§6 TIERED = joiner_free.tier_slip (DOUBLE_ENGINE §3): >= 1000 Cr 0.02%; 200-1000 0.05%; 50-200 0.10%;
    < 50 Cr or unknown 0.15%."""
    return float(J.tier_slip(t))


Trade = Tuple[str, str, int, float, float, float]      # d, s, side, R, Rs (cap 5L), Rs (cap 2.5L)


def make_trade(d: str, s: str, r: JL, slip: float) -> Optional[Trade]:
    """None unless triggered and sizeable (the select_trades condition).  Re-costing = liq_side.recost
    (slip_test.extra_cost_ps; bit-exact at the base 0.02%)."""
    if r[3] == 0:
        return None
    q = F.size(r[4], r[5])
    if q <= 0:
        return None
    rn, cps = L.recost(r, slip)
    return (d, s, r[3], rn, F.trade_rs(r[3], r[4], r[6], cps, q),
            F.trade_rs(r[3], r[4], r[6], cps, size_cap(r[4], r[5], NOTIONAL_CAP2)))


def _pct(ms: List[float], level: float) -> Tuple[float, float]:
    """Percentiles as y2026_asrs.boot_ci: ms[int(a n)], ms[int((1 - a) n) - 1], a = (1 - level) / 2, rounded to 9
    places first so that 0.95 gives exactly boot_ci's indices."""
    n = len(ms)
    a = (1 - level) / 2
    return ms[int(round(a * n, 9))], ms[int(round((1 - a) * n, 9)) - 1]


def boot(pairs: Sequence[Pair], level: float, n: Optional[int] = None, seed: int = BOOT_SEED
         ) -> Tuple[float, float]:
    """Day-block bootstrap of the mean at any level; at 95% it reproduces y2026_asrs.boot_ci exactly (days in
    first-appearance order, the same draws)."""
    if not pairs:
        raise ValueError("boot needs pairs")
    n = BOOT_N if n is None else n
    by: Dict[str, List[float]] = defaultdict(list)
    for d, v in pairs:
        by[d].append(v)
    days = list(by.values())
    rng = random.Random(seed)
    ms = []
    for _ in range(n):
        pick = [days[rng.randrange(len(days))] for _ in days]
        ms.append(sum(sum(v) for v in pick) / sum(len(v) for v in pick))
    ms.sort()
    return _pct(ms, level)


def paired_boot(a: Sequence[Pair], b: Sequence[Pair], level: float, n: Optional[int] = None, seed: int = BOOT_SEED
                ) -> Tuple[float, float]:
    """Paired day-block bootstrap of mean(a) - mean(b) at any level; at 95% it reproduces de_kill.boot_diff."""
    if not a or not b:
        raise ValueError("paired_boot needs both groups non-empty")
    n = BOOT_N if n is None else n
    agg: Dict[str, List[float]] = defaultdict(lambda: [0.0, 0, 0.0, 0])
    for d, v in a:
        agg[d][0] += v
        agg[d][1] += 1
    for d, v in b:
        agg[d][2] += v
        agg[d][3] += 1
    days = [agg[d] for d in sorted(agg)]
    rng = random.Random(seed)
    k = len(days)
    ms = []
    for _ in range(n):
        sa = na = sb = nb = 0.0
        for _ in range(k):
            x = days[rng.randrange(k)]
            sa += x[0]
            na += x[1]
            sb += x[2]
            nb += x[3]
        if na == 0 or nb == 0:
            raise ValueError("bootstrap draw with an empty group")
        ms.append(sa / na - sb / nb)
    ms.sort()
    return _pct(ms, level)


def stats(trades: Sequence[Trade], days: Sequence[str], level: float = LEVEL_REPORT) -> JD:
    """Per line (§5): trades, trades/day, win %, mean net R [CI], Rs P&L (5L and 2.5L caps), max DD % of 10L."""
    if not trades:
        return dict(n=0, days=len(days))
    pnl: Dict[str, float] = defaultdict(float)
    pnl2: Dict[str, float] = defaultdict(float)
    for t in trades:
        pnl[t[0]] += t[4]
        pnl2[t[0]] += t[5]
    dd = F.drawdown([(d, pnl.get(d, 0.0)) for d in days])
    dd2 = F.drawdown([(d, pnl2.get(d, 0.0)) for d in days])
    return dict(n=len(trades), days=len(days), win=sum(t[4] > 0 for t in trades) / len(trades),
                r=st.mean(t[3] for t in trades), ci=boot([(t[0], t[3]) for t in trades], level),
                rs=sum(t[4] for t in trades), rs2=sum(t[5] for t in trades), dd=dd["pct_cap"], dd2=dd2["pct_cap"],
                level=level)


def fmt(label_: str, x: JD, picks_day: Optional[float] = None) -> str:
    pk = "" if picks_day is None else f" | eligible picks/day {picks_day:.1f}"
    if x["n"] == 0:
        return f"{label_}: no trades{pk}"
    return (f"{label_}: {x['n']} trades ({x['n'] / max(1, x['days']):.2f}/day), win {x['win']:.0%}, "
            f"avg {x['r']:+.3f}R {x['level']:.2%}CI[{x['ci'][0]:+.3f},{x['ci'][1]:+.3f}] | Rs {x['rs']:+,.0f} "
            f"(cap 2.5L Rs {x['rs2']:+,.0f}) | max DD {x['dd']:.1f}% of 10L (2.5L {x['dd2']:.1f}%){pk}")


def rand_draws(names_of_day: Dict[str, List[str]], trig: Dict[Tuple[str, str], float], days: Sequence[str],
               draws: Optional[int] = None, seed: int = RAND_SEED, k: int = RAND_K) -> List[float]:
    """C-RAND: per draw, each day takes k names uniformly from that day's B150 list (sorted; pick flag ignored) and
    keeps their triggered trades (trig: (name, day) -> R).  -> sorted per-draw mean R over `days`."""
    draws = RAND_DRAWS if draws is None else draws
    rng = random.Random(seed)
    out = []
    for _ in range(draws):
        sm, n = 0.0, 0
        for d in days:
            pool = names_of_day.get(d, [])
            for s in rng.sample(pool, min(k, len(pool))):
                v = trig.get((s, d))
                if v is not None:
                    sm += v
                    n += 1
        out.append(sm / n if n else 0.0)
    return sorted(out)


SIDES = ((0, "BOTH"), (1, "LONG"), (-1, "SHORT"))     # owner 2026-11-21: "do both long and short", equal weight


def split_sides(trades: Sequence[Trade]) -> Dict[int, List[Trade]]:
    """{0: BOTH, 1: LONG, -1: SHORT} out of the SAME selection (never a per-side ranking).  Raises unless every
    trade is +-1 and LONG + SHORT == BOTH in count."""
    out: Dict[int, List[Trade]] = {0: list(trades), 1: [], -1: []}
    for t in trades:
        if t[2] not in (1, -1):
            raise ValueError(f"trade side {t[2]} outside +-1")
        out[t[2]].append(t)
    if len(out[1]) + len(out[-1]) != len(out[0]):
        raise ValueError("LONG + SHORT != BOTH")
    return out


def rand_draws_multi(names_of_day: Dict[str, List[str]], trig: Dict[Tuple[str, str], Tuple[float, int]],
                     days: Sequence[str], wins: Sequence[Tuple[str, Callable[[str], bool]]],
                     draws: Optional[int] = None, seed: int = RAND_SEED, k: int = RAND_K
                     ) -> Dict[Tuple[str, int], List[float]]:
    """C-RAND for every window x side in ONE pass per draw (the same draws for all cells): trig maps (name, day) ->
    (R, side).  -> {(window, side 0/1/-1): sorted per-draw mean R}.  Over the ALL window with side 0 it equals
    rand_draws(days) (same RNG sequence)."""
    draws = RAND_DRAWS if draws is None else draws
    rng = random.Random(seed)
    member = {d: [wn for wn, sel in wins if sel(d)] for d in days}
    out: Dict[Tuple[str, int], List[float]] = {(wn, sd): [] for wn, _ in wins for sd, _ in SIDES}
    for _ in range(draws):
        acc: Dict[Tuple[str, int], List[float]] = {key: [0.0, 0] for key in out}
        for d in days:
            pool = names_of_day.get(d, [])
            for s in rng.sample(pool, min(k, len(pool))):
                v = trig.get((s, d))
                if v is None:
                    continue
                for wn in member[d]:
                    for sd in (0, v[1]):
                        a = acc[(wn, sd)]
                        a[0] += v[0]
                        a[1] += 1
        for key, a in acc.items():
            out[key].append(a[0] / a[1] if a[1] else 0.0)
    return {key: sorted(v) for key, v in out.items()}


def pair_windows(kind: str, member_ms: Sequence[str], month: str, last_month: str) -> Optional[str]:
    """C-AFTER (joiner, J = first member month): BEFORE = J-13..J-2, AFTER = J..J+11.  C-EXIT (leaver: final member
    month < last_month): BEFORE = its last 12 member months, AFTER = the months after the final member month.
    The caller passes only band picks for BEFORE/AFTER band months and member picks for member months."""
    if not member_ms:
        return None
    if kind == "joiner":
        o = J.mdiff(month, member_ms[0])
        if -13 <= o <= -2:
            return "BEFORE"
        if 0 <= o <= 11:
            return "AFTER"
        return None
    if kind == "leaver":
        if member_ms[-1] >= last_month:
            return None
        if month in member_ms[-12:]:
            return "BEFORE"
        if month > member_ms[-1]:
            return "AFTER"
        return None
    raise ValueError(kind)


# ---------------------------------------------------------------- §7.3 read rule (pure)
R_LINES = ("R1 NEVER READ WINDOW: mean > 0 and 98.33% CI low > 0",
           "R2 NEVER OOS: mean > 0",
           "R3 NEVER - C-FO: READ WINDOW > 0 with 98.33% CI low > 0, and OOS > 0",
           "R4 BAND top 10 OOS: mean > 0 and 98.33% CI low > 0",
           "R5 OOS shorts > 0 in BAND top 10 and in NEVER",
           "R6 coverage gate held")


def read_rule(x: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    """-> (verdict, failing lines, flags).  A missing (None) input fails its line.  Keys: never_read, never_read_lo,
    never_oos, diff_read, diff_read_lo, diff_oos, band_oos, band_oos_lo (98.33% CIs), band_oos_short,
    never_oos_short, gate_ok, prejoin_read_lo (95%), band_read, rand_p95, atr_gaps, never_oos_tiered,
    band_oos_tiered, band_oos_dd, cafter_diff."""
    def pos(*keys: str) -> bool:
        return all(x.get(k) is not None and x[k] > 0 for k in keys)
    ok = [pos("never_read", "never_read_lo"), pos("never_oos"), pos("diff_read", "diff_read_lo", "diff_oos"),
          pos("band_oos", "band_oos_lo"), pos("band_oos_short", "never_oos_short"), x.get("gate_ok") is True]
    failing = [name for name, good in zip(R_LINES, ok) if not good]
    if not failing:
        verdict = "CANDIDATE for the live probe universe"
    elif not ok[0] and pos("prejoin_read_lo"):
        verdict = "LOOK-AHEAD: not a state"
    else:
        verdict = "NOT SHOWN"
    flags = []
    if x.get("band_read") is None or x.get("rand_p95") is None or not x["band_read"] > x["rand_p95"]:
        flags.append("band, not scan")
    gaps = x.get("atr_gaps") or []
    if gaps and all(g is not None and g <= 0 for g in gaps):
        flags.append("volatility, not F&O")
    for k in ("never_oos_tiered", "band_oos_tiered"):
        if x.get(k) is None or x[k] <= 0:
            flags.append(f"GROSS ONLY: gone at assumed slippage ({k})")
    if x.get("band_oos_dd") is not None and x["band_oos_dd"] > DD_FLAG_PCT:
        flags.append("DD")
    cd = x.get("cafter_diff")
    if cd is None or (cd > 0) != (F4_SIGN > 0):
        flags.append("MECHANISM NOT REPRODUCED")
    return verdict, failing, flags


# ---------------------------------------------------------------- report context (rows, features, post-pass)
def year_sel(y: str) -> Callable[[str], bool]:
    return lambda d: d.startswith(y)


def windows(g: JD, years: Sequence[str]) -> List[Tuple[str, Callable[[str], bool]]]:
    r0, r1 = read_window(g)
    out: List[Tuple[str, Callable[[str], bool]]] = [
        ("ALL", lambda d: True), ("READ WINDOW", lambda d: r0 <= d <= r1),
        ("IS", lambda d: IS_FROM <= d <= IS_END and r0 <= d <= r1), ("OOS", lambda d: OOS_FROM <= d <= LAST_DAY)]
    for y in years:
        out.append((y, year_sel(y)))
    return out


def load_kept(f: Path, keep: Callable[[JL], bool]) -> List[JL]:
    return [r for r in json.loads(f.read_text()) if keep(r)]


def report_ctx(setup: str) -> JD:
    """Everything the arms need, for one setup.  Memory-lean: PIT rows keep pick-flagged rows only; band rows keep
    band-day rows that are pick-flagged or triggered (C-RAND ignores the flag), already post-passed."""
    cal = row_calendar()
    cal_set = set(cal)
    by_month: Dict[str, List[str]] = defaultdict(list)
    for d in cal:
        by_month[d[:7]].append(d)
    bands, res, facts = load_bands(), read_json(RESOLVE_FILE), read_json(FACTS_FILE)
    labs = read_json(LABELS_FILE)["labels"]
    _cov, g, _res, gone = coverage_context(post_fetch=True)
    mem, rmap = V.load_membership(), json.loads(V.MAP_FILE.read_text())
    sets = V.eligible_sets(mem, rmap)
    eligible = V.make_eligible(mem, rmap)
    si = 0 if setup == "SRS" else 1
    pit_list = V.pit_files(V.SETUPS[si][2], V.SETUPS[si][3], rmap, mem)
    pit_by: Dict[str, List[Tuple[str, JL]]] = defaultdict(list)
    for s, f in pit_list:                                   # == survivorship.load_rows order (ties by load order)
        if f.exists():
            for r in load_kept(f, lambda r: bool(r[1])):
                pit_by[r[0]].append((s, r))
    band_ent: Dict[Tuple[str, str], JD] = {}              # (ds, month) -> entry
    names_of_day: Dict[str, List[str]] = defaultdict(list)  # C-RAND pools: all 150 names, GONE ones included
    for m, ents in bands.items():
        for e in ents:
            k = ent_key(e)
            ds = res[k]["ds"] if k not in gone else None
            name = ds if ds is not None else "GONE:" + e["sym"]
            if ds is not None:
                if (ds, m) in band_ent:
                    raise ValueError(f"two band entries map to {ds} in {m}")
                band_ent[(ds, m)] = e
            for d in by_month[m]:
                names_of_day[d].append(name)
    for d in names_of_day:
        names_of_day[d].sort()
    band_by: Dict[str, List[Tuple[str, JL]]] = defaultdict(list)
    status: Counter[str] = Counter()
    cases: List[str] = []
    ds_all = sorted({ds for ds, _m in band_ent})
    feat: Dict[Tuple[str, str], JL] = {}
    for ds in ds_all:
        rfile = rows_file(ds, setup)
        if rfile is None:
            raise SystemExit(f"band data symbol {ds} has no {setup} row file: run rows")
        mine = {m for (x, m) in band_ent if x == ds}
        lp_all = read_json(LOCK_DIR / f"{F.fname(ds)}.json")
        fd: Optional[Dict[str, JL]] = None                  # this name's feature file, read on its first lock day
        for r in load_kept(rfile, lambda r: r[0][:7] in mine and r[0] in cal_set and (bool(r[1]) or r[3] != 0)):
            e = band_ent[(ds, r[0][:7])]
            fact = facts[r[0][:7]][ent_key(e)].get(r[0])
            lp = (lp_all.get(r[0]) or {}).get(setup)
            sc = 1.0
            if r[3] != 0 and fact is not None and fact[0] and fact[1] != 0:
                if fd is None:
                    fd = read_json(FEAT_DIR / f"{F.fname(ds)}.json")
                assert fd is not None
                sc = kite_scale(fact, fd.get(r[0]))
            r2, stt = post_pass(r, fact, lp, sc)
            status[stt] += 1
            if stt.startswith("locked") or stt.startswith("void_locked"):
                cases.append(f"{r[0]} {ds} {'LONG' if r[3] > 0 else 'SHORT'}: {stt} (Kite/bhav {sc:.4f})")
            band_by[r[0]].append((ds, r2))
    need: Dict[str, Set[str]] = defaultdict(set)            # features only for the rows held in memory (RAM)
    for by in (pit_by, band_by):
        for d, xs in by.items():
            for s, _r in xs:
                need[s].add(d)
    for ds in sorted(need):
        ff = FEAT_DIR / f"{F.fname(ds)}.json"
        if not ff.exists():
            raise SystemExit(f"no feature file for {ds}: run rows")
        for d, v in read_json(ff).items():
            if d in need[ds]:
                feat[(ds, d)] = v
    return dict(setup=setup, cal=cal, g=g, bands=bands, res=res, labs=labs, gone=gone, sets=sets,
                eligible=eligible, pit_by=pit_by, band_by=band_by, band_ent=band_ent, names_of_day=names_of_day,
                status=status, cases=cases, feat=feat, facts=facts)


def band_label(ctx: JD, ds: str, d: str) -> Optional[str]:
    e = ctx["band_ent"][(ds, d[:7])]
    return ctx["labs"][d[:7]][ent_key(e)]           # type: ignore[no-any-return]


Pick = Tuple[str, str, JL]


def arm_picks(ctx: JD) -> Tuple[Dict[str, List[Pick]], Dict[str, Dict[str, int]]]:
    """-> ({arm: selected picks (d, s, row)}, {arm: eligible picks per day}).  Top-10 arms keep untriggered / void
    picks in their slot (make_trade drops them); 'all' arms keep every eligible pick-flagged row."""
    days = ctx["cal"]
    out: Dict[str, List[Pick]] = defaultdict(list)
    npk: Dict[str, Dict[str, int]] = defaultdict(dict)
    for d in days:
        fo = [(s, r) for s, r in ctx["pit_by"].get(d, []) if r[1] and ctx["eligible"](s, d)]
        bd = [(s, r) for s, r in ctx["band_by"].get(d, []) if r[1]]
        groups: Dict[str, List[Tuple[str, JL]]] = {"C-FO": fo, "BAND": bd, "UNION": fo + bd}
        for lab in ("NEVER", "PRE-JOIN", "EX"):
            groups[lab] = [(s, r) for s, r in bd if band_label(ctx, s, d) == lab]
        for half, lo, hi in (("BAND rank 1-75", 1, 75), ("BAND rank 76-150", 76, N_BAND)):
            groups[half] = [(s, r) for s, r in bd if lo <= ctx["band_ent"][(s, d[:7])]["rank"] <= hi]
        for name, cands in groups.items():
            npk[name][d] = len(cands)
        for name in ("C-FO", "BAND", "UNION"):
            out[name + " top10"] += [(d, s, r) for s, r in L.top_by_score(groups[name])]
        for name in ("C-FO", "BAND", "NEVER", "PRE-JOIN", "EX", "BAND rank 1-75", "BAND rank 76-150"):
            out[name + " all"] += [(d, s, r) for s, r in groups[name]]
    return out, npk


def slip_fn(ctx: JD, model: Any) -> Callable[[str, str], float]:
    if model == "TIERED":
        def tiered(s: str, d: str) -> float:
            v = ctx["feat"].get((s, d))
            if v is None:
                raise KeyError(f"no feature row for {s} {d}: run rows")
            return tier_slip(v[0])
        return tiered
    base = T.BASE_SLIP if model is None else float(model)
    return lambda s, d: base


def trades_of(picks: Sequence[Pick], slip: Callable[[str, str], float]) -> List[Trade]:
    out = []
    for d, s, r in picks:
        t = make_trade(d, s, r, slip(s, d))
        if t is not None:
            out.append(t)
    return out


def pairs(ts: Iterable[Trade], sel: Callable[[str], bool] = lambda d: True, side: int = 0) -> List[Pair]:
    return [(t[0], t[3]) for t in ts if sel(t[0]) and (side == 0 or t[2] == side)]


def diff_line(name: str, a: List[Pair], b: List[Pair], level: float, strict: bool = False
              ) -> Tuple[str, Optional[float], Optional[float]]:
    """mean(a) - mean(b) with a paired day-block CI.  Descriptive cells can be so sparse (one side, one year) that
    a draw leaves a group empty: the line then says so and the CI is undefined.  strict=True (the R3 read lines)
    re-raises instead: a read line is never silently undefined."""
    if not a or not b:
        return f"{name}: undefined (an empty group)", None, None
    dv = st.mean(v for _, v in a) - st.mean(v for _, v in b)
    try:
        lo, hi = paired_boot(a, b, level)
    except ValueError:
        if strict:
            raise
        return f"{name}: {dv:+.3f}R, CI undefined (too sparse) (n {len(a)} vs {len(b)})", dv, None
    return f"{name}: {dv:+.3f}R {level:.2%}CI[{lo:+.3f},{hi:+.3f}] (n {len(a)} vs {len(b)})", dv, lo


def confound_cells(ctx: JD, never: Sequence[Trade], cfo: Sequence[Trade]
                   ) -> Dict[Tuple[str, Optional[int], Optional[float]], List[Pair]]:
    """§5 confound table: pooled picks (NEVER 'N' + C-FO 'C') cut into ATR% terciles by POSITION in the pooled
    sort (ascending ATR%, ties by symbol and day; position i of n -> 1 + 3i // n, as de_kill.terciles) and into
    turnover tiers (tier_slip of T).  Keys: (group, tercile, None), (group, None, tier), (group, tercile, tier).
    A pick without ATR% is in no tercile cell."""
    feat = ctx["feat"]
    pool = [("N", t) for t in never] + [("C", t) for t in cfo]
    with_atr = sorted(((feat.get((t[1], t[0]), [None, None])[1], t[1], t[0], i) for i, (_, t) in enumerate(pool)
                       if feat.get((t[1], t[0]), [None, None])[1] is not None))
    terc = {x[3]: 1 + (3 * k) // len(with_atr) for k, x in enumerate(with_atr)}
    out: Dict[Tuple[str, Optional[int], Optional[float]], List[Pair]] = defaultdict(list)
    for i, (w, t) in enumerate(pool):
        tv = tier_slip(feat.get((t[1], t[0]), [None])[0])
        k = terc.get(i)
        out[(w, None, tv)].append((t[0], t[3]))
        if k is not None:
            out[(w, k, None)].append((t[0], t[3]))
            out[(w, k, tv)].append((t[0], t[3]))
    return out


SLIP_MODELS: Tuple[Any, ...] = (None, 0.0005, 0.0010, 0.0015, "TIERED")


def slip_name(m: Any) -> str:
    return "0.02%" if m is None else ("TIERED" if m == "TIERED" else f"{m * 100:.2f}%")


def report_setup(ctx: JD, read: bool) -> Tuple[List[str], Dict[str, Any]]:
    days, g = ctx["cal"], ctx["g"]
    W = windows(g, sorted({d[:4] for d in days}))
    wsel = dict(W)
    picks, npk = arm_picks(ctx)
    base = slip_fn(ctx, None)
    tr = {a: trades_of(p, base) for a, p in picks.items()}
    tag = "" if read else " [ASRS: printed, never read]"
    Lr = [f"\n=== {ctx['setup']}{tag} === sessions {len(days)}; post-pass {dict(ctx['status'])}"]
    for a in sorted(tr):
        parts = split_sides(tr[a])
        for side, sn in SIDES:
            for wn, sel in W:
                sd = [d for d in days if sel(d)]
                ts = [t for t in parts[side] if sel(t[0])]
                pk = npk[a.rsplit(" ", 1)[0]]
                Lr.append(fmt(f"{a:18s} {sn:5s} {wn:11s}", stats(ts, sd), st.mean(pk[d] for d in sd) if sd else 0.0))
    Lr.append("\nSLIPPAGE beside the read lines (§6; never in the verdict)")
    tiered: Dict[str, Optional[float]] = {}
    for a in ("NEVER all", "BAND top10", "C-FO all", "C-FO top10", "PRE-JOIN all", "EX all", "UNION top10"):
        for mdl in SLIP_MODELS[1:]:
            parts = split_sides(trades_of(picks[a], slip_fn(ctx, mdl)))
            for wn in ("READ WINDOW", "OOS"):
                for side, sn in SIDES:
                    sel = wsel[wn]
                    x = stats([t for t in parts[side] if sel(t[0])], [d for d in days if sel(d)])
                    Lr.append(fmt(f"{slip_name(mdl):6s} {a:12s} {sn:5s} {wn:11s}", x))
                    if mdl == "TIERED" and wn == "OOS" and side == 0:
                        tiered[a] = x.get("r")
                        if x.get("r") is None or x["r"] <= 0:
                            Lr.append(f"       GROSS ONLY: gone at assumed slippage ({a})")
    rd, oos = wsel["READ WINDOW"], wsel["OOS"]
    Lr.append("\nDIFFERENCES (ALL triggered eligible picks, paired by day)")
    l1, d_read, d_lo = diff_line("R3 NEVER - C-FO READ WINDOW", pairs(tr["NEVER all"], rd), pairs(tr["C-FO all"], rd),
                                 LEVEL_VERDICT, strict=True)
    l2, d_oos, _ = diff_line("R3 NEVER - C-FO OOS", pairs(tr["NEVER all"], oos), pairs(tr["C-FO all"], oos),
                             LEVEL_REPORT, strict=True)
    Lr += [l1, l2]
    for a in ("NEVER all", "PRE-JOIN all", "EX all", "BAND all"):
        for side, sn in SIDES:
            for wn, sel in W:
                Lr.append(diff_line(f"{a} - C-FO all {sn:5s} {wn} (report only)", pairs(tr[a], sel, side),
                                    pairs(tr["C-FO all"], sel, side), LEVEL_REPORT)[0])
    Lr.append(diff_line("BAND rank 1-75 - 76-150 READ WINDOW (report only)", pairs(tr["BAND rank 1-75 all"], rd),
                        pairs(tr["BAND rank 76-150 all"], rd), LEVEL_REPORT)[0])
    # proxies: with and without (report only)
    Lr.append("\nSURVEILLANCE PROXIES (report only): picks dropped after selection")
    for a in ("BAND top10", "NEVER all"):
        for fi, fname_ in ((6, "P-SURV"), (7, "P-BE")):
            def keep(t: Trade, fi: int = fi) -> bool:
                e = ctx["band_ent"][(t[1], t[0][:7])]
                v = ctx["facts"][t[0][:7]][ent_key(e)].get(t[0])
                return not (v and v[fi])
            for wn in ("READ WINDOW", "OOS"):
                sel = wsel[wn]
                ts = [t for t in tr[a] if sel(t[0]) and keep(t)]
                Lr.append(fmt(f"{a:10s} without {fname_:6s} {wn:11s}", stats(ts, [d for d in days if sel(d)])))
    # confound table
    Lr.append("\nCONFOUND (report only): NEVER vs C-FO inside ATR% terciles and turnover tiers, READ WINDOW")
    cells = confound_cells(ctx, [t for t in tr["NEVER all"] if rd(t[0])], [t for t in tr["C-FO all"] if rd(t[0])])
    gaps = []
    for k in (1, 2, 3):
        ln, dv, _ = diff_line(f"ATR tercile T{k}", cells[("N", k, None)], cells[("C", k, None)], LEVEL_REPORT)
        gaps.append(dv)
        Lr.append("  " + ln)
    for tv in sorted({key[2] for key in cells if key[2] is not None}):
        Lr.append("  " + diff_line(f"tier {tv * 100:.2f}%", cells[("N", None, tv)], cells[("C", None, tv)],
                                   LEVEL_REPORT)[0])
        for k in (1, 2, 3):
            a2, b2 = cells[("N", k, tv)], cells[("C", k, tv)]
            Lr.append(f"    tier {tv * 100:.2f}% x T{k}: NEVER n {len(a2)} "
                      f"{'-' if not a2 else format(st.mean(v for _, v in a2), '+.3f')} | C-FO n {len(b2)} "
                      f"{'-' if not b2 else format(st.mean(v for _, v in b2), '+.3f')}")
    # C-RAND
    trig: Dict[Tuple[str, str], Tuple[float, int]] = {}
    for d, xs in ctx["band_by"].items():
        for s, r in xs:
            t = make_trade(d, s, r, T.BASE_SLIP)
            if t is not None:
                trig[(s, d)] = (t[3], t[2])
    rnd = rand_draws_multi(ctx["names_of_day"], trig, days, W)
    Lr.append(f"\nC-RAND ({len(rnd[('ALL', 0)])} draws, seed 7; 10 names a day from B150, pick flag ignored) vs "
              "BAND top10, per side and window")
    for side, sn in SIDES:
        for wn, sel in W:
            dr = rnd[(wn, side)]
            bm = K.mean_or_none([v for _, v in pairs(tr["BAND top10"], sel, side)])
            Lr.append(f"  C-RAND {sn:5s} {wn:11s}: median {dr[len(dr) // 2]:+.3f}R, 95th "
                      f"{dr[int(0.95 * len(dr))]:+.3f}R | BAND top10 {'-' if bm is None else format(bm, '+.3f')}R")
    dr_read = rnd[("READ WINDOW", 0)]
    p95 = dr_read[int(0.95 * len(dr_read))]
    band_read = K.mean_or_none([v for _, v in pairs(tr["BAND top10"], rd)])
    # C-AFTER / C-EXIT
    months_of = K.member_months(ctx["sets"])
    last_month = max(ctx["sets"])
    groups: Dict[str, Dict[str, List[Trade]]] = {"C-AFTER": defaultdict(list), "C-EXIT": defaultdict(list)}
    # (kind, group, window taken from band picks, window taken from member picks).  Joiners as F4: first member
    # month after 2020-01 (joiner_free.START_MONTH).
    for kind, gname, w_band, w_mem in (("joiner", "C-AFTER", "BEFORE", "AFTER"),
                                       ("leaver", "C-EXIT", "AFTER", "BEFORE")):
        for src, want in (("BAND all", w_band), ("C-FO all", w_mem)):
            for t in tr[src]:
                ms = months_of.get(t[1], [])
                if kind == "joiner" and (not ms or ms[0] <= J.START_MONTH):
                    continue
                if pair_windows(kind, ms, t[0][:7], last_month) == want:
                    groups[gname][want].append(t)
    cafter = None
    Lr.append("\nC-AFTER / C-EXIT (descriptive): BEFORE - AFTER per side and window (window = trade date)")
    for gname, gg in groups.items():
        before, after = split_sides(gg["BEFORE"]), split_sides(gg["AFTER"])
        for side, sn in SIDES:
            for wn, sel in W:
                ln, dv, _ = diff_line(f"{gname} {sn:5s} {wn:11s} BEFORE - AFTER", pairs(before[side], sel),
                                      pairs(after[side], sel), LEVEL_REPORT)
                Lr.append("  " + ln)
                if gname == "C-AFTER" and side == 0 and wn == "ALL":
                    cafter = dv
    Lr.append(f"\nLOCK / VOID CASES ({len(ctx['cases'])}): " + "; ".join(ctx["cases"]))
    def m_(a: str, sel: Callable[[str], bool], side: int = 0) -> Optional[float]:
        return K.mean_or_none([v for _, v in pairs(tr[a], sel, side)])
    def lo_(a: str, sel: Callable[[str], bool], level: float) -> Optional[float]:
        p = pairs(tr[a], sel)
        return boot(p, level)[0] if p else None
    oos_days = [d for d in days if oos(d)]
    band_oos = [t for t in tr["BAND top10"] if oos(t[0])]
    x = dict(never_read=m_("NEVER all", rd), never_read_lo=lo_("NEVER all", rd, LEVEL_VERDICT),
             never_oos=m_("NEVER all", oos), diff_read=d_read, diff_read_lo=d_lo, diff_oos=d_oos,
             band_oos=m_("BAND top10", oos), band_oos_lo=lo_("BAND top10", oos, LEVEL_VERDICT),
             band_oos_short=m_("BAND top10", oos, -1), never_oos_short=m_("NEVER all", oos, -1),
             gate_ok=not g["stop"], prejoin_read_lo=lo_("PRE-JOIN all", rd, LEVEL_REPORT),
             band_read=band_read, rand_p95=p95, atr_gaps=gaps, never_oos_tiered=tiered.get("NEVER all"),
             band_oos_tiered=tiered.get("BAND top10"),
             band_oos_dd=stats(band_oos, oos_days)["dd"] if band_oos else None, cafter_diff=cafter)
    return Lr, x


def report_cmd(argv: List[str]) -> None:
    if "--after-review" not in argv:
        raise SystemExit("report prints R: run it only after the independent review, as "
                         "`nonfo_srs.py report --after-review`")
    out = ["Non-F&O SRS study (NONFO_SRS_DESIGN.md, frozen 2026-11-21).  0.02%/side stored costs unless stated; "
           "10L, risk 10k, cap 5L (+2.5L column).  Top-10 arms keep void picks in their slot."]
    verdict_in: Dict[str, Any] = {}
    for setup in ("SRS", "ASRS"):
        ctx = report_ctx(setup)
        if not p4_reproduces(ctx):
            raise SystemExit(f"P4 FAILED for {setup}: C-FO does not reproduce the reference; no report")
        if setup == "SRS":
            out += coverage_lines(*coverage_context(post_fetch=True)[:2], "COVERAGE GATE (post-fetch, §7.1 / P6)")
        lines, x = report_setup(ctx, read=setup == "SRS")
        out += lines
        if setup == "SRS":
            verdict_in = x
        del ctx
    v, failing, flags = read_rule(verdict_in)
    out.append(f"\nVERDICT (frozen §7.3, SRS both sides, 0.02%/side): {v}")
    if failing:
        out.append("  failing: " + "; ".join(failing))
    out.append("  flags: " + ("; ".join(flags) if flags else "none"))
    out.append("  label cap: CANDIDATE, never confirmed; a pass licenses ONE thing: measuring slippage at Rs100.")
    NONFO.mkdir(parents=True, exist_ok=True)
    REPORT_FILE.write_text("\n".join(out), encoding="utf-8")
    print("\n".join(out))


# ---------------------------------------------------------------- pre-run checks (counts / booleans only)
def pit_ctx(setup: str) -> JD:
    """The C-FO part of report_ctx only (no band rows): enough for P4 before the band rows exist."""
    mem, rmap = V.load_membership(), json.loads(V.MAP_FILE.read_text())
    si = 0 if setup == "SRS" else 1
    pit_by: Dict[str, List[Tuple[str, JL]]] = defaultdict(list)
    for s, f in V.pit_files(V.SETUPS[si][2], V.SETUPS[si][3], rmap, mem):
        if f.exists():
            for r in load_kept(f, lambda r: bool(r[1])):
                pit_by[r[0]].append((s, r))
    return dict(setup=setup, cal=row_calendar(), pit_by=pit_by, band_by={}, band_ent={}, labs={},
                eligible=V.make_eligible(mem, rmap))


def p4_reproduces(ctx: JD) -> bool:
    """P4: C-FO top 10 through survivorship.select_trades AND through arm_picks equals the reference daily file."""
    ref_f = V.OUT / f"surv_{ctx['setup'].lower()}_pit_daily.json"
    ref = [tuple(x) for x in json.loads(ref_f.read_text())]
    _, daily, _ = V.select_trades(ctx["pit_by"], ctx["cal"], ctx["eligible"])
    picks, _ = arm_picks(ctx)
    pnl: Dict[str, float] = defaultdict(float)
    for t in trades_of(picks["C-FO top10"], slip_fn(ctx, None)):
        pnl[t[0]] += t[4]
    mine = [(d, pnl.get(d, 0.0)) for d in ctx["cal"]]
    return p4_mismatches(daily, ref) == 0 and p4_mismatches(mine, ref) == 0


def p4_mismatches(daily: Sequence[Tuple[str, float]], ref: Sequence[Tuple[Any, ...]]) -> int:
    if len(daily) != len(ref):
        return max(len(daily), len(ref))
    return sum(1 for a, b in zip(daily, ref) if a[0] != b[0] or abs(a[1] - b[1]) >= 1e-6)


def checks_cmd() -> None:
    L_ = ["PRE-RUN CHECKS (counts / booleans only, no R)"]
    cal = row_calendar()
    stv = load_store(htf_calendar())
    man = json.loads(C.MANIFEST.read_text())
    for kind in ("cm_old", "cm_udiff"):
        L_.append(f"P1 manifest {kind}: {dict(Counter(v[0] for v in man.get(kind, {}).values()))}")
        oks = sorted(d for d, v in man.get(kind, {}).items() if v[0] == "ok")
        for d in (oks[:1] + oks[-1:]) if oks else []:
            rows = parse_cm_bhav(C.path(kind, d))
            L_.append(f"P1 parse {kind} {d}: kept rows {len(rows)}, EQ {sum(r[0] == 'EQ' for r in rows.values())}, "
                      f"INE {sum(r[1].startswith('INE') for r in rows.values())}")
    sess = set(stv.dates)
    miss = [d for d in cal if d not in sess]
    extra = [d for d in stv.dates if IS_FROM <= d <= LAST_DAY and d not in set(cal)]
    L_.append(f"P1 row calendar {len(cal)} sessions: without a CM file {len(miss)} {miss[:10]}; CM sessions not in "
              f"the row calendar {len(extra)} {extra[:10]}")
    L_.append("P1 UDiFF units: " + udiff_continuity(stv))
    bands, res = load_bands(), read_json(RESOLVE_FILE)
    lab = read_json(LABELS_FILE)
    labs, mm = lab["labels"], lab["member_months"]
    old, _ = load_old_membership()
    new = V.load_membership()
    removed = (set().union(*(set(old[m]) for m in old if m >= "2018-01"))) - set(new["2020-01"])
    sc = load_symchange()
    hit: Counter[str] = Counter()
    removed_ends = {chain_end(x, "2019-12-31", sc) for x in removed} | removed
    for m, ents in bands.items():
        for e in ents:
            if e["chain"] in removed_ends or e["sym"] in removed:
                hit[str(labs[m][ent_key(e)])] += 1
    L_.append(f"P2 F&O removals 2018-19 (in 2018-01..2019-12, not in 2020-01): {len(removed)} names; their band "
              f"stock-months by label {dict(hit)}; NEVER among them (must be 0): {hit['NEVER']}")
    sets = V.eligible_sets(new, json.loads(V.MAP_FILE.read_text()))
    bad_n = [m for m, v in bands.items() if len(v) != N_BAND]
    sym_hit = sum(1 for m, v in bands.items() for e in v if e["sym"] in set(new[m]))
    isin_hit = 0
    for m, v in bands.items():
        r_i, _ = rebalance(stv, m)
        fi = {stv.isins[c] for c in fo_isin_codes(stv, new[m], (r_i, first_session(stv, m)))}
        isin_hit += sum(1 for e in v if e["isin"] in fi)
    ds_hit = sum(1 for m, v in bands.items() for e in v
                 if res[ent_key(e)]["ds"] is not None and res[ent_key(e)]["ds"] in sets[m])
    member_lab = sum(1 for m in labs for x in labs[m].values() if x == "MEMBER")
    L_.append(f"P3 months without {N_BAND} names: {bad_n}; band names in fo_membership[m] by symbol {sym_hit}, by "
              f"ISIN {isin_hit}, by resolved data symbol {ds_hit}; MEMBER labels {member_lab} (all must be 0)")
    L_.append(f"P3 cross-check ever-PIT data symbols labelled NEVER (must be 0): "
              f"{sum(1 for s, ms in mm.items() if not ms and s in ever_pit())}")
    L_.append("P9 " + p9_line(res))
    L_.append("P10 " + p10_line(bands, read_json(FACTS_FILE)))
    L_.append("P11 MANUAL, OPEN: SEBI F&O criteria and the join-announcement lead must be read from the circulars; "
              "not automated, not filled from memory.")
    del stv                                   # free ~150 MB before the rows are loaded (32-bit Python)
    for setup in ("SRS", "ASRS"):             # P4 needs only the cached PIT rows
        L_.append(f"P4 C-FO {setup} reproduces surv_{setup.lower()}_pit_daily.json (select_trades AND arm_picks, "
                  f"0 mismatches): {p4_reproduces(pit_ctx(setup))}")
    L_.append("P5 " + p5_line())
    rows_ready = IDENT_FILE.exists() and FEAT_DIR.exists()
    if not rows_ready:
        L_.append("P6-P8: NOT RUN: rows / features / identity not built yet (stage B5 `rows`).")
    else:
        ctx = report_ctx("SRS")
        cov, g = coverage_context(post_fetch=True)[:2]
        L_ += coverage_lines(cov, g, "P6 coverage gate with identity failures and nobars added")
        L_ += p7_lines(ctx)
        L_ += p8_lines(ctx)
    print("\n".join(L_))


def udiff_continuity(stv: Store) -> str:
    """Traded value of the same EQ symbols on the last old-format day vs the first UDiFF day: the median ratio
    must sit in 0.2..5 (a lakh / crore unit change would show as ~100 or ~0.01)."""
    j = bisect.bisect_left(stv.dates, C.UDIFF_FROM)
    if j <= 0 or j >= len(stv.dates):
        return "NOT RUN (no sessions on both sides of 2024-07-08)"
    ratios = []
    for s in stv.syms:
        a, b = stv.day(s, j - 1), stv.day(s, j)
        if a and b and a[1] == 0 and b[1] == 0 and a[9] > 0 and b[9] > 0:
            ratios.append(b[9] / a[9])
    if not ratios:
        return "NOT RUN (no common symbols)"
    md = st.median(ratios)
    return f"{stv.dates[j - 1]} -> {stv.dates[j]} median value ratio {md:.2f} over {len(ratios)} symbols: " \
           f"{'PASS' if 0.2 <= md <= 5 else 'FAIL'}"


def p9_line(res: Dict[str, JD]) -> str:
    pit = ever_pit()
    band_pit = sorted({x["ds"] for x in res.values() if x["ds"] in pit})
    not_cached = [s for s in band_pit if not cached_raw(s)]
    in_new = [s for s in band_pit if (NONFO_RAW / f"{F.fname(s)}.json.gz").exists()]
    return (f"band names that are ever-PIT members {len(band_pit)}: without a cached raw file {len(not_cached)} "
            f"{not_cached[:10]}; also present in kite_raw_nonfo (re-fetched) {len(in_new)} (both must be 0)")


def p10_line(bands: Dict[str, List[JD]], facts: Dict[str, Dict[str, Dict[str, JL]]]) -> str:
    c: Dict[str, Counter[str]] = defaultdict(Counter)
    for m in bands:
        for v in facts[m].values():
            for x in v.values():
                c[m[:4]]["days"] += 1
                c[m[:4]]["lock"] += x[1] != 0
                c[m[:4]]["psurv"] += x[6]
                c[m[:4]]["pbe"] += x[7]
                c[m[:4]]["nonEQ"] += x[0] == 0
    return "band stock-days per year: " + "; ".join(f"{y} {dict(v)}" for y, v in sorted(c.items()))


def p5_line() -> str:
    """P5: rows of 6 cached names rebuilt from the DURABLE raw copy equal the cached row files (also proves B0)."""
    out = []
    for raw_dir, y26, rows_dir, k in ((DUR_RAW / "full_raw", True, V.SETUPS[0][2], 3),
                                      (DUR_RAW / "surv_raw", False, V.SETUPS[0][3], 3)):
        names = sorted(f.name[:-8] for f in raw_dir.glob("*.json.gz"))
        for fn in names[:: max(1, len(names) // k)][:k]:
            s = next((x for x in (fn, fn.replace("_", "&")) if (rows_dir / f"{F.fname(x)}.json").exists()), fn)
            ref = rows_dir / f"{F.fname(s)}.json"
            ok = ref.exists() and json.loads(json.dumps(F.stock_rows(s, 3, raw=raw_dir, y2026=y26))) == \
                json.loads(ref.read_text())
            out.append(f"{s} {ok}")
    return "rebuilt rows == cache: " + ", ".join(out)


def p7_lines(ctx: JD) -> List[str]:
    """P7: member-days (with a row) where the bhav close of d-1 > 100 but the Kite adjusted close of d-1 <= 100,
    per year and arm (band arms by label; C-FO over pick-flagged eligible rows only, the rows kept in memory)."""
    c: Dict[str, Counter[str]] = defaultdict(Counter)
    for d, xs in ctx["band_by"].items():
        for s, _r in xs:
            e = ctx["band_ent"][(s, d[:7])]
            fct = ctx["facts"][d[:7]][ent_key(e)].get(d)
            ft = ctx["feat"].get((s, d))
            if fct is None or ft is None or fct[9] is None or ft[2] is None:
                continue
            grp = str(ctx["labs"][d[:7]][ent_key(e)])
            hit = fct[9] > N_MIN_PX >= ft[2]
            for gname in ("BAND", grp):
                c[f"{d[:4]} {gname}"]["days"] += 1
                c[f"{d[:4]} {gname}"]["bhav>100, kite<=100"] += hit
    return ["P7 adjusted-price close>100 exposure (band rows kept: pick-flagged or triggered): " +
            "; ".join(f"{k} {dict(v)}" for k, v in sorted(c.items()))]


def p8_lines(ctx: JD) -> List[str]:
    _, npk = arm_picks(ctx)
    out = ["P8 eligible pick-flagged picks per day (mean / median over the row calendar):"]
    for a, v in sorted(npk.items()):
        vals = list(v.values())
        out.append(f"  {a:16s} mean {st.mean(vals):.1f} median {st.median(vals):.0f}")
    nev = st.mean(npk["NEVER"].values())
    if nev < 3:
        out.append(f"  WARNING: NEVER has {nev:.1f} < 3 eligible picks a day: the decisive arm is thin (§9 P8)")
    return out


# ---------------------------------------------------------------- argv
COMMANDS: Dict[str, Callable[[], None]] = {
    "oldfo": oldfo, "band": band_cmd, "resolve": resolve_cmd, "coverage": coverage_cmd, "fetch": fetch_cmd,
    "rows": rows_cmd, "checks": checks_cmd}


def main(argv: List[str]) -> None:
    if argv[:1] == ["report"] and len(argv) <= 2:
        report_cmd(argv[1:])
        return
    if len(argv) != 1 or argv[0] not in COMMANDS:
        raise SystemExit("usage: nonfo_srs.py oldfo|band|resolve|coverage|fetch|rows|checks|report --after-review")
    COMMANDS[argv[0]]()


if __name__ == "__main__":
    main(_argv)

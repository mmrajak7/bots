"""Survivorship-bias removal for the SRS / ASRS top-10 study (SURVIVORSHIP.md, frozen 2026-11-20).

    python -I survivorship.py fetch      # Kite daily + 5-min (2020-01..2026-11-19) for member symbols not in the 218
    python -I survivorship.py renames    # bhavcopy-vs-Kite price check -> data/fo_rename_map.json (+ evidence lines)
    python -I survivorship.py rows       # per-session rows, SRS (sig_n=3) and ASRS (sig_n=1), same pipeline
    python -I survivorship.py coverage   # member-days with / without data per year (counts only)
    python -I survivorship.py report     # PIT vs BIASED (run only after the independent review)
"""
from __future__ import annotations

import csv
import gzip
import io
import json
import statistics as st
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]                      # full_asrs parses --srs at import; neutral (sig_n is explicit here)
import full_asrs as F  # noqa: E402
import slip_test as T  # noqa: E402
import y2026_asrs as Y  # noqa: E402

MEM_FILE = HERE / "data" / "fo_membership.json"
MAP_FILE = HERE / "data" / "fo_rename_map.json"
BHAV_DIR = F.BASE.parent / "fo_bhav"
SURV_RAW = F.BASE / "surv_raw"
PX_CACHE = F.BASE / "surv_px.json"           # {"bhav": {date: {sym: px}}, "kite": {sym: {date: close}}}
OUT = HERE / "results"
M5_TO = date(2026, 11, 19)
SETUPS = (("SRS", 3, F.BASE / "full_rows_srs", F.BASE / "surv_rows_srs"),
          ("ASRS", 1, F.BASE / "full_rows", F.BASE / "surv_rows"))

# Kite lists HFCL only in the trade-for-trade series; fetched by that token, stored as HFCL.
KITE_ALIAS = {"HFCL": "HFCL-BE"}

# The 28 ever-members with no NSE tradingsymbol today -> candidate data symbol (None = no candidate).
# Source of the candidate: NSE equities symbolchange.csv (11-20) unless marked HYPOTHESIS / CONTROL.
CANDIDATES: Dict[str, Optional[str]] = {
    "AMARAJABAT": "ARE&M",        # symbolchange 27-OCT-2023
    "CADILAHC": "ZYDUSLIFE",      # 07-MAR-2022
    "CENTURYTEX": "ABREL",        # 10-OCT-2024
    "GMRINFRA": "GMRAIRPORT",     # 11-DEC-2024
    "GUJGASLTD": "GUJENERGY",     # 01-JUL-2026
    "HFCL": "HFCL",               # no rename; series moved to BE (KITE_ALIAS)
    "IBULHSGFIN": "SAMMAANCAP",   # 26-JUL-2024
    "ICICIPRULI": "ICICILIFE",    # 23-OCT-2026
    "INFRATEL": "INDUSTOWER",     # 18-DEC-2020
    "L&TFH": "LTF",               # 23-APR-2024
    "LTI": "LTM",                 # LTI -> LTIM 05-DEC-2022 -> LTM 27-FEB-2026
    "LTIM": "LTM",                # 27-FEB-2026
    "MCDOWELL-N": "UNITDSPR",     # 07-JUN-2024
    "MOTHERSUMI": "MOTHERSON",    # 09-JUN-2022
    "NIITTECH": "COFORGE",        # 20-AUG-2020
    "PVR": "PVRINOX",             # 12-MAY-2023
    "SRTRANSFIN": "SHRIRAMFIN",   # 20-DEC-2022
    "TATAGLOBAL": "TATACONSUM",   # 27-FEB-2020
    "TATAMOTORS": "TMPV",         # 24-OCT-2025 (after the CV demerger)
    "ZOMATO": "ETERNAL",          # 09-APR-2025
    "PEL": "PIRAMALFIN",          # HYPOTHESIS: merged into Piramal Finance (not in symbolchange)
    "MINDTREE": "LTM",            # HYPOTHESIS: LTIMindtree merger (symbolchange says LTI -> LTIM, not MINDTREE)
    "GSPL": "GSPLTRANS",          # HYPOTHESIS: GSPL merged into Gujarat Gas; transmission demerged to GSPLTRANS
    "HDFC": "HDFCBANK",           # CONTROL: acquirer merger, must be rejected
    "IDFC": "IDFCFIRSTB",         # CONTROL
    "EQUITAS": "EQUITASBNK",      # CONTROL
    "UJJIVAN": "UJJIVANSFB",      # CONTROL
    "TATAMTRDVR": "TMPV",         # CONTROL: DVR shares cancelled
}

MATCH_TOL = 0.02
SPLIT_FACTORS = (1.2, 1.25, 1.5, 2.0, 3.0, 4.0, 5.0, 10.0)
ACCEPT_FRAC = 0.80
TWIN_FRAC, TWIN_MIN = 0.80, 5
# Overrides of the return rule, each backed by NSE symbolchange.csv AND a price pattern explained in SURVIVORSHIP.md.
MANUAL_ACCEPT = {
    "NIITTECH": "NSE symbolchange 20-AUG-2020; the 2 disagreeing pairs are reciprocal (0.928, 1.077) around the "
                "single bhav date 2020-04-01 (COVID-crash futures basis); level ratio 5.03 -> 5.01 constant",
}


# ---------------------------------------------------------------- membership / renames
def load_membership() -> Dict[str, List[str]]:
    return json.loads(MEM_FILE.read_text())


def members(mem: Dict[str, List[str]], d: str) -> List[str]:
    """Membership snapshot of month(d).  A missing month is an error, never a guess."""
    m = d[:7]
    if m not in mem:
        raise KeyError(f"no F&O membership for month {m}")
    return mem[m]


def resolve(x: str, rmap: Dict[str, Optional[str]]) -> Optional[str]:
    """Follow the rename map to the final data symbol; None = GONE.  x not in the map -> x."""
    seen = set()
    while x in rmap:
        if x in seen:
            raise ValueError(f"rename cycle at {x}")
        seen.add(x)
        nxt = rmap[x]
        if nxt is None:
            return None
        if nxt == x:
            return x
        x = nxt
    return x


def eligible_sets(mem: Dict[str, List[str]], rmap: Dict[str, Optional[str]]) -> Dict[str, set]:
    out = {}
    for m, xs in mem.items():
        out[m] = {y for y in (resolve(x, rmap) for x in xs) if y is not None}
    return out


def make_eligible(mem: Dict[str, List[str]], rmap: Dict[str, Optional[str]]) -> Callable[[str, str], bool]:
    sets = eligible_sets(mem, rmap)

    def eligible(s: str, d: str) -> bool:
        m = d[:7]
        if m not in sets:
            raise KeyError(f"no F&O membership for month {m}")
        return s in sets[m]
    return eligible


# ---------------------------------------------------------------- price check
def _f(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def bhav_prices(zpath: Path) -> Dict[str, float]:
    """symbol -> price: UDiFF UndrlygPric (spot) when > 0, else nearest-expiry stock-future CLOSE (SETTLE if 0)."""
    with zipfile.ZipFile(zpath) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    best: Dict[str, Tuple[date, float]] = {}
    spot: Dict[str, float] = {}
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("INSTRUMENT") == "FUTSTK":
            sym, exp = r["SYMBOL"], datetime.strptime(r["EXPIRY_DT"], "%d-%b-%Y").date()
            px = _f(r.get("CLOSE")) or _f(r.get("SETTLE_PR"))
        elif r.get("FinInstrmTp") == "STF":
            sym, exp = r["TckrSymb"], datetime.strptime(r["XpryDt"], "%Y-%m-%d").date()
            px = _f(r.get("ClsPric")) or _f(r.get("SttlmPric"))
            u = _f(r.get("UndrlygPric"))
            if u > 0:
                spot[sym] = u
        else:
            continue
        if px > 0 and (sym not in best or exp < best[sym][0]):
            best[sym] = (exp, px)
    out = {s: v[1] for s, v in best.items()}
    out.update(spot)
    return out


RET_TOL, RET_MIN_PAIRS = 0.03, 3


def check_returns(bhav: List[Tuple[str, float]], kite: Dict[str, float]) -> dict:
    """AMENDMENT 1: Kite history is back-adjusted for dividends / demergers / splits, so price LEVELS differ by a
    slowly drifting factor.  Compare RETURNS between consecutive member dates instead: same company iff
    (bhav2/bhav1) / (kite2/kite1) is within RET_TOL of 1 (dividend months, basis) or of a clean split factor."""
    pts = [(d, px, kite[d]) for d, px in sorted(bhav) if kite.get(d)]
    res = dict(dates=len(pts), pairs=0, agree=0, split=0, bad=[], levels=[px / k for _, px, k in pts])
    for (d1, b1, k1), (d2, b2, k2) in zip(pts, pts[1:]):
        x = (b2 / b1) / (k2 / k1)
        res["pairs"] += 1
        if abs(x - 1) <= RET_TOL:
            res["agree"] += 1
        elif any(abs(x / k - 1) <= RET_TOL or abs(x * k - 1) <= RET_TOL for k in SPLIT_FACTORS):
            res["agree"] += 1
            res["split"] += 1
        else:
            res["bad"].append((d2, round(x, 3)))
    return res


def accept_returns(res: dict, shares_month: bool) -> Tuple[bool, str]:
    if shares_month:
        return False, "shares a membership month"
    if res["dates"] == 0:
        return False, "no Kite bar on any member date"
    if res["pairs"] < RET_MIN_PAIRS:
        # too few return pairs: fall back to the strict level rule on every date
        if all(abs(v - 1) <= MATCH_TOL for v in res["levels"]):
            return True, f"only {res['pairs']} pairs; all levels within {MATCH_TOL:.0%}"
        return False, f"only {res['pairs']} return pairs and levels differ"
    frac = res["agree"] / res["pairs"]
    if frac < ACCEPT_FRAC:
        return False, f"returns agree {frac:.0%} < {ACCEPT_FRAC:.0%}"
    return True, "ok"


def member_dates(mem: Dict[str, List[str]], dates: Dict[str, str]) -> Dict[str, List[str]]:
    """symbol -> its member dates (first session of each member month)."""
    out: Dict[str, List[str]] = defaultdict(list)
    for m, xs in sorted(mem.items()):
        for x in xs:
            out[x].append(dates[m])
    return out


def bhav_dates() -> Dict[str, str]:
    out = {}
    for f in sorted(BHAV_DIR.glob("*.zip")):
        out.setdefault(f.stem[:7], f.stem)
    return out


def data_file(s: str) -> Optional[Path]:
    for d in (F.RAW, SURV_RAW):
        f = d / f"{F.fname(s)}.json.gz"
        if f.exists():
            return f
    return None


def build_px_cache(mem: Dict[str, List[str]], symbols: List[str]) -> dict:
    """Bhav prices per member date + Kite daily close of every data symbol on those dates (cached)."""
    dates = bhav_dates()
    if sorted(dates) != sorted(mem):
        raise SystemExit("bhav zips and membership months differ")
    cache = json.loads(PX_CACHE.read_text()) if PX_CACHE.exists() else {"bhav": {}, "kite": {}}
    for m, d in sorted(dates.items()):
        if d not in cache["bhav"]:
            cache["bhav"][d] = bhav_prices(BHAV_DIR / f"{d}.zip")
    want = set(dates.values())
    for s in symbols:
        if s in cache["kite"]:
            continue
        f = data_file(s)
        if f is None:
            continue
        with gzip.open(f, "rt") as g:
            daily = json.load(g)["daily"]
        cache["kite"][s] = {r[0]: r[4] for r in daily if r[0] in want}
    tmp = PX_CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache))
    tmp.replace(PX_CACHE)
    return cache


def universe() -> List[str]:
    return json.loads(Y.UNIVERSE.read_text())


def fetch_list(mem: Dict[str, List[str]]) -> List[str]:
    uni = set(universe())
    ever = set().union(*map(set, mem.values()))
    want = set()
    for x in ever - uni:
        want.add(x if x not in CANDIDATES else CANDIDATES[x])
    want |= {v for v in CANDIDATES.values() if v}
    return sorted(s for s in want if s and s not in uni)


def renames() -> None:
    mem = load_membership()
    dates = bhav_dates()
    md = member_dates(mem, dates)
    months_of: Dict[str, set] = defaultdict(set)
    for m, xs in mem.items():
        for x in xs:
            months_of[x].add(m)
    syms = sorted(set(universe()) | set(fetch_list(mem)))
    cache = build_px_cache(mem, syms)
    bhav, kite = cache["bhav"], cache["kite"]

    def series(x):
        return [(d, bhav[d][x]) for d in md[x] if x in bhav[d]]

    def lv(res):
        v = res["levels"]
        return f"{v[0]:.3f}..{v[-1]:.3f}" if v else "-"

    print("RENAME CHECK (Amendment 1: returns between consecutive member dates) old -> candidate | member months | "
          "dates with Kite | return pairs agree/total (split) | level ratio first..last | verdict")
    rmap: Dict[str, Optional[str]] = {}
    for old, new in CANDIDATES.items():
        if new is None:
            rmap[old] = None
            print(f"{old:11s} -> None | no candidate -> GONE")
            continue
        res = check_returns(series(old), kite.get(new, {}))
        shares = bool(months_of[old] & months_of.get(new, set())) and old != new
        ok, why = accept_returns(res, shares)
        if not ok and old in MANUAL_ACCEPT:
            ok, why = True, "MANUAL: " + MANUAL_ACCEPT[old]
        rmap[old] = new if ok else None
        span = sorted(months_of[old])
        print(f"{old:11s} -> {new:11s} | {span[0]}..{span[-1]} ({len(span)}) | {res['dates']} | "
              f"{res['agree']}/{res['pairs']} ({res['split']}) | {lv(res)} | "
              f"{('ACCEPT' + (' [' + why + ']' if why != 'ok' else '')) if ok else 'REJECT (' + why + ')'}"
              + (f" | disagree {res['bad'][:4]}" if res["bad"] else ""), flush=True)
    MAP_FILE.write_text(json.dumps(rmap, indent=1, sort_keys=True))
    print(f"wrote {MAP_FILE}: {sum(v is not None for v in rmap.values())} renames, "
          f"{sum(v is None for v in rmap.values())} GONE")

    print("\nIDENTITY CHECK (resolved symbol, return rule) - only members with a disagreeing pair listed")
    ever = sorted(months_of)
    clean = nodata = fails = listed = 0
    for x in ever:
        y = resolve(x, rmap)
        if y is None:
            continue
        if y not in kite:
            nodata += 1
            print(f"  {x} -> {y}: NO DATA FILE")
            continue
        res = check_returns(series(x), kite[y])
        ok, why = accept_returns(res, False)
        fails += not ok
        if not res["bad"] and ok:
            clean += 1
            continue
        listed += ok
        print(f"  {x} -> {y}: {res['agree']}/{res['pairs']} ({res['split']} split) level {lv(res)} "
              f"disagree {res['bad'][:4]} -> {'ok' if ok else 'FAIL: ' + why}")
    print(f"  clean: {clean}; listed but ok: {listed}; FAIL: {fails}; no data file: {nodata}")

    print(f"\nTWIN CHECK (member vs every other data symbol, returns agree >= {TWIN_FRAC:.0%} over >= {TWIN_MIN} pairs)")
    twins = 0
    for x in ever:
        y0 = resolve(x, rmap)
        ser = series(x)
        for y, kc in kite.items():
            if y == y0:
                continue
            res = check_returns(ser, kc)
            if res["pairs"] >= TWIN_MIN and res["agree"] - res["split"] >= TWIN_FRAC * res["pairs"]:
                twins += 1
                print(f"  {x} (data {y0}) ~ {y}: {res['agree'] - res['split']}/{res['pairs']} pairs agree, "
                      f"level {lv(res)}, shares months {bool(months_of[x] & months_of.get(y, set()))}")
    print(f"  twin pairs: {twins}")


# ---------------------------------------------------------------- fetch / rows
def fetch() -> None:
    todo = fetch_list(load_membership())
    print("survivorship fetch list:", len(todo), flush=True)
    F.fetch_parallel(symbols=todo, raw=SURV_RAW, m5_to=M5_TO, kite_alias=KITE_ALIAS)
    empty = []
    for s in todo:
        f = SURV_RAW / f"{F.fname(s)}.json.gz"
        if not f.exists():
            empty.append((s, "missing"))
            continue
        with gzip.open(f, "rt") as g:
            x = json.load(g)
        if not x["daily"] or not x["m5"]:
            empty.append((s, f"daily {len(x['daily'])} m5 {len(x['m5'])}"))
    print("files without daily or 5-min data:", empty)


def rows() -> None:
    syms = [s for s in fetch_list(load_membership()) if (SURV_RAW / f"{F.fname(s)}.json.gz").exists()]
    print("symbols with a surv_raw file:", len(syms), flush=True)
    for name, sig_n, _, out in SETUPS:
        out.mkdir(parents=True, exist_ok=True)
        F.GAP_STATS.update(sessions=0, gapped=0, filled_bars=0)
        made = n_rows = n_trig = n_pick = 0
        for s in syms:
            f = out / f"{F.fname(s)}.json"
            if f.exists():
                continue
            r = F.stock_rows(s, sig_n, raw=SURV_RAW, y2026=False)
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(r))
            tmp.replace(f)
            made += 1
            n_rows += len(r)
            n_trig += sum(x[3] != 0 for x in r)
            n_pick += sum(bool(x[1]) for x in r)
        print(f"{name}: row files built {made} (total {len(list(out.glob('*.json')))}), sessions {n_rows}, "
              f"triggered {n_trig}, pick-flagged {n_pick}; gap stats {F.GAP_STATS}", flush=True)


# ---------------------------------------------------------------- selection / money (== full_asrs.report)
def load_rows(sym_files: List[Tuple[str, Path]]) -> Dict[str, List[Tuple[str, list]]]:
    by_day: Dict[str, List[Tuple[str, list]]] = defaultdict(list)
    for s, f in sym_files:
        if f.exists():
            for r in json.loads(f.read_text()):
                by_day[r[0]].append((s, r))
    return by_day


def biased_files(rows218: Path) -> List[Tuple[str, Path]]:
    return [(s, rows218 / f"{F.fname(s)}.json") for s in universe()]


def pit_files(rows218: Path, rows_new: Path, rmap: Dict[str, Optional[str]],
              mem: Dict[str, List[str]]) -> List[Tuple[str, Path]]:
    uni = universe()
    ever = set().union(*map(set, mem.values()))
    extra = sorted({y for y in (resolve(x, rmap) for x in ever) if y is not None} - set(uni))
    return biased_files(rows218) + [(s, rows_new / f"{F.fname(s)}.json") for s in extra]


def select_trades(by_day: Dict[str, List[Tuple[str, list]]], days: List[str],
                  eligible: Optional[Callable[[str, str], bool]] = None, slip: Optional[float] = None):
    """Top-N picks per day by score among ELIGIBLE stocks (filter before ranking), sized and costed exactly as
    full_asrs.report().  Returns (trades [(d, s, side, R, Rs)], daily [(d, Rs)], eligible picks per day).

    slip=None keeps the stored 0.02%/side costs untouched (the BIASED == FULL_ASRS path).  A slip value re-costs
    each trade exactly as slip_test.run_setup: cost/share += extra_cost_ps(entry, exit, slip), R -= extra / risk.
    The picks and qty do not depend on slip (Amendment 2)."""
    trades, npicks = [], {}
    for d in days:
        cand = [(s, r) for s, r in by_day.get(d, []) if r[1] and (eligible is None or eligible(s, d))]
        npicks[d] = len(cand)
        for s, r in sorted(cand, key=lambda x: -x[1][2])[:F.TOP_N]:
            if r[3] == 0:
                continue
            q = F.size(r[4], r[5])
            if q <= 0:
                continue
            if slip is None:
                trades.append((d, s, r[3], r[8], F.trade_rs(r[3], r[4], r[6], r[9], q)))
            else:
                extra = T.extra_cost_ps(r[4], r[6], slip)
                trades.append((d, s, r[3], r[8] - extra / r[7], F.trade_rs(r[3], r[4], r[6], r[9] + extra, q)))
    pnl: Dict[str, float] = defaultdict(float)
    for t in trades:
        pnl[t[0]] += t[4]
    daily = [(d, pnl.get(d, 0.0)) for d in days]
    return trades, daily, npicks


def biased_reproduces(rows218: Path, reported: Path) -> bool:
    by_day = load_rows(biased_files(rows218))
    _, daily, _ = select_trades(by_day, sorted(by_day))
    ref = [tuple(x) for x in json.loads(reported.read_text())]
    return len(ref) == len(daily) and all(a[0] == b[0] and abs(a[1] - b[1]) < 1e-6 for a, b in zip(daily, ref))


# ---------------------------------------------------------------- coverage (residual bias, counts only)
def coverage(mem: Dict[str, List[str]], rmap: Dict[str, Optional[str]], row_days: Dict[str, set],
             days: List[str]) -> Dict[str, dict]:
    """Per year: member-days, with a row, GONE (by old name), symbol with no rows at all (by name), no session."""
    by_month: Dict[str, List[str]] = defaultdict(list)
    for d in days:
        by_month[d[:7]].append(d)
    out: Dict[str, dict] = {}
    for m in sorted(by_month):
        y = m[:4]
        c = out.setdefault(y, dict(member_days=0, with_row=0, gone=0, gone_by={}, nodata=0, nodata_by={},
                                   no_session=0))
        ents: Dict[str, Optional[str]] = {}
        for x in members(mem, m + "-01"):
            r = resolve(x, rmap)
            ents[r if r is not None else "GONE:" + x] = r
        for key, r in ents.items():
            n = len(by_month[m])
            c["member_days"] += n
            if r is None:
                c["gone"] += n
                c["gone_by"][key[5:]] = c["gone_by"].get(key[5:], 0) + n
            elif r not in row_days:
                c["nodata"] += n
                c["nodata_by"][r] = c["nodata_by"].get(r, 0) + n
            else:
                hit = sum(d in row_days[r] for d in by_month[m])
                c["with_row"] += hit
                c["no_session"] += n - hit
    return out


DIRECTION = ("Direction (frozen): a missing member can only displace a lower-ranked pick when it would itself have "
             "been a top-10 pick, so its effect is bounded by its share of member-days. The GONE names are merger "
             "targets (HDFC, IDFC, MINDTREE, EQUITAS, UJJIVAN, TATAMTRDVR, GSPL) plus PEL (merged; GONE only because "
             "Kite has no history for it), not failures. The sign of the effect on R is NOT asserted.")


def coverage_lines(cov: Dict[str, dict]) -> List[str]:
    L = ["YEAR | member-days | with row | GONE | no data file | no session that day"]
    for y, c in cov.items():
        md = c["member_days"] or 1
        L.append(f"{y} | {c['member_days']} | {c['with_row']} ({c['with_row'] / md:.1%}) | {c['gone']} "
                 f"({c['gone'] / md:.2%}) | {c['nodata']} ({c['nodata'] / md:.2%}) | {c['no_session']} "
                 f"({c['no_session'] / md:.2%})")
    tot: Dict[str, int] = defaultdict(int)
    nd: Dict[str, int] = defaultdict(int)
    for c in cov.values():
        for k, v in c["gone_by"].items():
            tot[k] += v
        for k, v in c["nodata_by"].items():
            nd[k] += v
    L.append(f"GONE member-days by name: {dict(sorted(tot.items(), key=lambda kv: -kv[1]))}")
    L.append(f"resolved but no rows file: {dict(nd)}")
    return L


def row_days_of(by_day: Dict[str, List[Tuple[str, list]]]) -> Dict[str, set]:
    out: Dict[str, set] = defaultdict(set)
    for d, xs in by_day.items():
        for s, _ in xs:
            out[s].add(d)
    return dict(out)


def run_coverage() -> None:
    mem, rmap = load_membership(), json.loads(MAP_FILE.read_text())
    _, _, rows218, rows_new = SETUPS[1]
    by_day = load_rows(pit_files(rows218, rows_new, rmap, mem))
    print("\n".join(coverage_lines(coverage(mem, rmap, row_days_of(by_day), sorted(by_day)))))


def read_rule(name: str, pit_oos: dict) -> List[str]:
    """Frozen, descriptive: PIT becomes the reference; flag a non-positive (or empty) PIT OOS."""
    out = []
    if pit_oos.get("n", 0) == 0 or pit_oos["r"] <= 0:
        out.append(f"FLAG ({name}): PIT OOS avg R <= 0")
    out.append(f"READ ({name}, frozen, descriptive): PIT replaces BIASED as the reference number in STUDY_SUMMARY.md.")
    return out


# ---------------------------------------------------------------- report (NOT run before review)
def window_stats(trades, daily, sel) -> dict:
    ts = [t for t in trades if sel(t[0])]
    dp = [(d, p) for d, p in daily if sel(d)]
    if not ts:
        return dict(n=0, days=len(dp))
    dd = F.drawdown(dp)
    return dict(n=len(ts), days=len(dp), win=sum(t[4] > 0 for t in ts) / len(ts), r=st.mean(t[3] for t in ts),
                ci=Y.boot_ci([(t[0], t[3]) for t in ts]), rs=sum(t[4] for t in ts), dd=dd["dd"],
                dd_pct=dd["pct_cap"], peak=dd["peak"], trough=dd["trough"])


def fmt(label: str, x: dict) -> str:
    if x["n"] == 0:
        return f"{label}: no trades"
    return (f"{label}: {x['n']} trades ({x['n'] / x['days']:.1f}/day), win {x['win']:.0%}, avg {x['r']:+.3f}R "
            f"CI[{x['ci'][0]:+.3f},{x['ci'][1]:+.3f}] | P&L Rs {x['rs']:+,.0f} = {x['rs'] / F.CAPITAL:+.0%} of 10L | "
            f"MAX DD Rs {x['dd']:,.0f} = {x['dd_pct']:.1f}% of 10L {x['peak']}->{x['trough']}")


SIDES = ((1, "LONG"), (-1, "SHORT"))
SIDE_SLIPS = (None, 0.0005)                  # None = stored base 0.02%/side; 0.05% re-costed as slip_test


def side_split(trades, days: List[str], side: int):
    """The trades of one side out of the SAME top-10 selection (never a per-side ranking), and that side's own
    daily P&L zero-filled over the same session calendar (drawdown = the side traded alone, vs 10L)."""
    ts = [t for t in trades if t[2] == side]
    pnl: Dict[str, float] = defaultdict(float)
    for t in ts:
        pnl[t[0]] += t[4]
    return ts, [(d, pnl.get(d, 0.0)) for d in days]


def side_lines(name: str, variants: Dict[str, Tuple[list, list]], days: List[str], windows, slip_label: str
               ) -> List[str]:
    """Amendment 2 (pre-declared before the first run): by-side split, descriptive only, no FLAG / READ."""
    L = []
    years = sorted({d[:4] for d in days})
    for v, (trades, _) in variants.items():
        parts = {sd: side_split(trades, days, sd) for sd, _ in SIDES}
        if sum(len(p[0]) for p in parts.values()) != len(trades):
            raise ValueError(f"{name} {v}: long + short trades != all trades (side outside +-1)")
        for sd, sn in SIDES:
            ts, dl = parts[sd]
            for wn, sel in list(windows) + [(y, lambda d, y=y: d.startswith(y)) for y in years]:
                L.append(f"{slip_label} | {v:6s} | {sn:5s} | " + fmt(wn, window_stats(ts, dl, sel)))
    return L


def report() -> None:
    mem, rmap = load_membership(), json.loads(MAP_FILE.read_text())
    eligible = make_eligible(mem, rmap)
    windows = (("ALL      ", lambda d: True), ("2020-2023", lambda d: d <= F.IS_END),
               ("2024-2026", lambda d: d > F.IS_END))
    L = ["Survivorship study (SURVIVORSHIP.md): PIT = point-in-time F&O universe (monthly NSE bhavcopy snapshot, "
         "rename map), BIASED = today's 218. Top-10/day, capital 10L, risk 10k, notional cap 5L, NEO costs."]
    cov_lines: List[str] = []
    for name, _, rows218, rows_new in SETUPS:
        bias_by = load_rows(biased_files(rows218))
        pit_by = load_rows(pit_files(rows218, rows_new, rmap, mem))
        days = sorted(set(bias_by) | set(pit_by))
        res = {"BIASED": select_trades(bias_by, days), "PIT": select_trades(pit_by, days, eligible)}
        L.append(f"\n=== {name} === sessions {len(days)}")
        oos = {}
        for v, (trades, daily, npk) in res.items():
            for wn, sel in windows:
                x = window_stats(trades, daily, sel)
                if wn.startswith("2024"):
                    oos[v] = x
                L.append(f"{v:6s} " + fmt(wn, x))
            pv = list(npk.values())
            tpd = len(trades) / len(days)
            L.append(f"{v:6s} picks/day: eligible mean {st.mean(pv):.1f} median {st.median(pv):.0f}, "
                     f"traded {tpd:.1f}/day, days with 0 eligible picks {sum(p == 0 for p in pv)}")
        L.append("YEAR | variant | trades | avg R [day-block CI] | P&L Rs | maxDD % of 10L")
        for y in sorted({d[:4] for d in days}):
            for v, (trades, daily, _) in res.items():
                x = window_stats(trades, daily, lambda d, y=y: d.startswith(y))
                if x["n"] == 0:
                    L.append(f"{y} | {v} | 0")
                    continue
                L.append(f"{y} | {v:6s} | {x['n']:5d} | {x['r']:+.3f} [{x['ci'][0]:+.3f},{x['ci'][1]:+.3f}] | "
                         f"{x['rs']:+13,.0f} | {x['dd_pct']:5.1f}%")
        L += read_rule(name, oos["PIT"])
        L.append(f"\nBY SIDE ({name}; Amendment 2, pre-declared 2026-11-20 before the first run; a check of a SEEN "
                 "finding, descriptive, NOT a rule). Same top-10 selection split by side; max DD = that side traded "
                 "alone, vs 10L. slip | variant | side | window: ...")
        for slip in SIDE_SLIPS:
            lab = "0.02%" if slip is None else f"{slip * 100:.2f}%"
            if slip is None:
                vs = {v: res[v][:2] for v in ("BIASED", "PIT")}
            else:
                vs = {"BIASED": select_trades(bias_by, days, None, slip)[:2],
                      "PIT": select_trades(pit_by, days, eligible, slip)[:2]}
            L += side_lines(name, vs, days, windows, lab)
        if name == SETUPS[0][0]:
            cov_lines = coverage_lines(coverage(mem, rmap, row_days_of(pit_by), days))
        OUT.mkdir(exist_ok=True)
        (OUT / f"surv_{name.lower()}_pit_daily.json").write_text(json.dumps(res["PIT"][1]))
    L += ["\nRESIDUAL BIAS (member-days of the point-in-time universe without data)"] + cov_lines + [DIRECTION]
    (OUT / "survivorship.txt").write_text("\n".join(L), encoding="utf-8")
    print("\n".join(L))


COMMANDS = {"fetch": fetch, "renames": renames, "rows": rows, "coverage": run_coverage, "report": report}


def main(argv: List[str]) -> None:
    if len(argv) != 1 or argv[0] not in COMMANDS:
        raise SystemExit("usage: survivorship.py fetch|renames|rows|coverage|report")
    COMMANDS[argv[0]]()


if __name__ == "__main__":
    main(_argv)

"""NSE corporate-actions file: download (data job, owner approved 2026-11-21) and parse (JOINER_POSITIONAL_DESIGN.md §4.2).

Why: probe P3 found the bhavcopy previous close UNADJUSTED (IRCTC 1:5, SRF / GPIL / DUCON bonuses), so §4.2 makes the
NSE corporate-actions file the primary source of every split and bonus factor.  Demergers and rights get no factor:
their ex-date return is set to 0 (data/joiner_ca_zero.json, written by `joiner_positional.py ca`).

    python -I nse_ca.py fetch     # month chunks 2018-12..2026-11 -> NSE_DATA/corp_actions/YYYY-MM.json (resumable)
    python -I nse_ca.py verify    # one half-year pull vs the sum of its months (no silent row cap)
    python -I nse_ca.py report    # counts: rows per year, parsed kinds, the UNPARSED subjects (no prices)

Source: https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date=DD-MM-YYYY&to_date=DD-MM-YYYY
(301 -> www.nseindia.in; JSON list without cookies, probe 2026-11-21).  Fetch method as nse_cm_fetch.py: urllib, browser
User-Agent, 1 s pause, retries with backoff.  The body is untrusted: only parsed as JSON and checked field by field.

Factor convention (multiply the PRE-ex-date price by it to put it on the post-ex-date share basis):
    face value change "From Rs A To Rs B" (split or consolidation): B / A        (10 -> 2 : 0.2; 1 -> 10 : 10)
    "Bonus A:B" (A new shares for every B held):                     B / (A + B)  (4:1 : 0.2; 1:1 : 0.5; 3:4 : 4/7)
    several distinct events on one (ISIN, ex-date) multiply (GPIL 2021-10-26: split 10 -> 5 and bonus 1:1 = 0.25).
Anything that mentions a bonus / split / consolidation / face value but does not parse cleanly is UNPARSED and listed,
never guessed.  Bonus debentures / preference shares are not an equity factor and are listed as `other`.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ROOT = Path(os.environ.get("NSE_DATA", r"C:\Users\mail2\Documents\Projects\NSE_DATA")) / "corp_actions"  # env NSE_DATA
MANIFEST_NAME = "manifest.json"
FIRST_MONTH, LAST_MONTH = "2018-12", "2026-11"          # design §9 Q1 window
VERIFY_HALF = ("2021-07", "2021-12")                     # half-year used for the row-cap check
API = "https://www.nseindia.com/api/corporates-corporateActions?index=equities&from_date={a}&to_date={b}"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
PAUSE, RETRIES, BACKOFF = 1.0, 4, (3, 10, 30, 60)
MAX_CONSEC_FAIL = 5
REQUIRED = ("symbol", "series", "isin", "exDate", "subject")
EQUITY_SERIES = ("EQ", "BE", "BZ")                       # the panel's kept series
MON = {m: i + 1 for i, m in enumerate(["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                       "DEC"])}


# ---------------------------------------------------------------- months and dates
def months(a: str = FIRST_MONTH, b: str = LAST_MONTH) -> List[str]:
    y, m = int(a[:4]), int(a[5:])
    out = []
    while f"{y:04d}-{m:02d}" <= b:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def month_span(m: str) -> Tuple[dt.date, dt.date]:
    a = dt.date(int(m[:4]), int(m[5:]), 1)
    nxt = dt.date(a.year + (a.month == 12), a.month % 12 + 1, 1)
    return a, nxt - dt.timedelta(days=1)


def parse_exdate(s: str) -> Optional[str]:
    """'28-Oct-2021' -> '2021-10-28'; anything else -> None."""
    mt = re.fullmatch(r"\s*(\d{1,2})-([A-Za-z]{3})-(\d{4})\s*", s or "")
    if not mt or mt.group(2).upper() not in MON:
        return None
    try:
        return dt.date(int(mt.group(3)), MON[mt.group(2).upper()], int(mt.group(1))).isoformat()
    except ValueError:
        return None


def url_for(a: dt.date, b: dt.date) -> str:
    return API.format(a=a.strftime("%d-%m-%Y"), b=b.strftime("%d-%m-%Y"))


# ---------------------------------------------------------------- download
def validate_body(body: bytes, lo: Optional[dt.date] = None, hi: Optional[dt.date] = None) -> List[dict]:
    """The body must be a JSON list of records with the required string fields and an ex-date inside [lo, hi]."""
    xs = json.loads(body.decode("utf-8"))
    if not isinstance(xs, list):
        raise ValueError(f"expected a JSON list, got {type(xs).__name__}")
    if not xs:
        raise ValueError("empty list (every month has dividends: an empty answer is a failure, not a quiet month)")
    for x in xs:
        if not isinstance(x, dict) or any(not isinstance(x.get(k), str) for k in REQUIRED):
            raise ValueError(f"bad record {str(x)[:120]}")
        d = parse_exdate(x["exDate"])
        if d is None:
            raise ValueError(f"bad exDate {x['exDate']!r}")
        if lo is not None and hi is not None and not (lo.isoformat() <= d <= hi.isoformat()):
            raise ValueError(f"exDate {d} outside {lo}..{hi}")
    return xs


def get(u: str) -> Tuple[int, Optional[bytes], str]:
    req = urllib.request.Request(u, headers={"User-Agent": UA, "Accept": "application/json"})
    err = ""
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=90) as r:   # follows the 301 to nseindia.in
                return 200, r.read(), ""
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except Exception as e:                                    # timeouts, resets: retry, then report
            err = f"{type(e).__name__}: {e}"[:120]
        time.sleep(BACKOFF[attempt])
    return 0, None, err


def write_atomic(f: Path, data: bytes) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, f)


def load_manifest(root: Path = ROOT) -> Dict[str, list]:
    f = root / MANIFEST_NAME
    return json.loads(f.read_text()) if f.exists() else {}


def fetch(root: Path = ROOT) -> None:
    man = load_manifest(root)
    fails = 0
    for m in months():
        f = root / f"{m}.json"
        lo, hi = month_span(m)
        if man.get(m, [""])[0] == "ok" and f.exists():
            try:
                validate_body(f.read_bytes(), lo, hi)
                continue
            except ValueError:
                pass                                              # a damaged file is fetched again
        code, body, err = get(url_for(lo, hi))
        time.sleep(PAUSE)
        if code == 200 and body is not None:
            try:
                xs = validate_body(body, lo, hi)
            except ValueError as e:
                code, err = 0, f"invalid: {e}"[:120]
            else:
                write_atomic(f, body)
                man[m] = ["ok", len(xs), dt.datetime.now().isoformat(timespec="seconds")]
                write_atomic(root / MANIFEST_NAME, json.dumps(man, indent=0, sort_keys=True).encode())
                print(f"ok     {m} {len(xs)}", flush=True)
                fails = 0
                continue
        fails += 1
        man[m] = ["failed", 0, err]
        write_atomic(root / MANIFEST_NAME, json.dumps(man, indent=0, sort_keys=True).encode())
        print(f"FAILED {m} {err}", flush=True)
        if fails >= MAX_CONSEC_FAIL:
            raise SystemExit(f"{fails} consecutive failures: NSE looks throttled; stopped (resume later)")
    bad = [m for m in months() if man.get(m, [""])[0] != "ok"]
    print(f"months ok {len(months()) - len(bad)} of {len(months())}; not ok: {bad}")


def record_key(x: dict) -> Tuple[str, str, str, str]:
    return (x["isin"].strip(), x["symbol"].strip(), parse_exdate(x["exDate"]) or "", norm_subject(x["subject"]))


def verify(root: Path = ROOT) -> None:
    """Row-cap check: one half-year request must hold exactly the records of its months' files."""
    a, b = VERIFY_HALF
    lo, hi = month_span(a)[0], month_span(b)[1]
    code, body, err = get(url_for(lo, hi))
    if code != 200 or body is None:
        raise SystemExit(f"verify request failed: {err}")
    half = Counter(record_key(x) for x in validate_body(body, lo, hi))
    parts: Counter = Counter()
    for m in months(a, b):
        parts.update(record_key(x) for x in validate_body((root / f"{m}.json").read_bytes(), *month_span(m)))
    same = half == parts
    print(f"verify {a}..{b}: half-year {sum(half.values())} records, months {sum(parts.values())}; identical: {same}")
    if not same:
        print(f"  only in the half-year: {sum((half - parts).values())}; only in the months: {sum((parts - half).values())}")


def load_raw(root: Path = ROOT, first: str = FIRST_MONTH, last: str = LAST_MONTH) -> Optional[List[dict]]:
    """Every record of the window, or None unless EVERY month is on disk, valid and marked ok (a partial file must
    not pass for complete: a missed month would book its splits as real losses)."""
    man = load_manifest(root)
    out: List[dict] = []
    for m in months(first, last):
        f = root / f"{m}.json"
        if man.get(m, [""])[0] != "ok" or not f.exists():
            return None
        try:
            out.extend(validate_body(f.read_bytes(), *month_span(m)))
        except ValueError:
            return None
    return out


# ---------------------------------------------------------------- the subject parser
def norm_subject(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()


_RS = r"(?:rs\.?|re\.?|inr)\s*"
_AMT = r"(\d+(?:\.\d+)?)"
# "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Re 1/- Per Share", "Consolidation Of Equity Shares
# From Re 1 Per Share To Rs 10 Per Share": the factor is To / From either way.
_FV = re.compile(r"(?:face\s*value|consolidation)[^/]*?from\s*" + _RS + _AMT
                 + r"\s*/?-?\s*(?:per\s*(?:equity\s*)?share)?\s*(?:each\s*)?to\s*" + _RS + _AMT, re.I)
_NONEQ = r"(?:debentures?|ncds?|ncrps?|ncps?|ccps|preference|redeemable|bonds?)"
_BONUS_NONEQ = re.compile(r"\bbonus\b[^/:]{0,25}?" + _NONEQ, re.I)       # "Bonus Ncrps 1:2", "Bonus - 1 Debenture"
_BONUS = re.compile(r"\bbonus\s*(?:issue\s*)?(?:of\s*)?-?\s*(\d+)\s*:\s*(\d+)", re.I)   # "Bonus 1:1", "Bonus- 1:2"
_BONUS_WORD = re.compile(r"\bbonus\b", re.I)
_SPLITISH = re.compile(r"split|sub[\s-]*division|consolidat|face\s*value", re.I)
_DEMERGER = re.compile(r"de-?merger|spin[\s-]*off", re.I)
_RIGHTS = re.compile(r"\brights?\b", re.I)
_CAPRED = re.compile(r"(?:reduction|reduce)\s*(?:of|in)?\s*(?:share\s*)?capital|capital\s*reduction", re.I)
_SCHEME = re.compile(r"scheme\s*of\s*(?:arr?angement|amalgamation)|amalgamation|arr?angement", re.I)


@dataclass
class Parsed:
    factor: float = 1.0                                   # product of the price factors found (1.0 = none)
    kinds: List[str] = field(default_factory=list)        # split / consolidation / bonus / demerger / rights /
    #                                                       capital_reduction / scheme / bonus_nonequity / unparsed


def parse_subject(subject: str) -> Parsed:
    """One subject line -> its price factor and kinds.  Every clause is scanned (a '... Dividend / Bonus 1:1' line
    still yields the bonus); dividends, AGMs, buybacks, interest give factor 1 and no kind."""
    s = norm_subject(subject)
    p = Parsed()
    fv = list(_FV.finditer(s))
    for mt in fv:
        a, b = float(mt.group(1)), float(mt.group(2))
        if a <= 0 or b <= 0 or a == b:
            p.kinds.append("unparsed")
            continue
        p.factor *= b / a
        p.kinds.append("split" if b < a else "consolidation")
    noneq = bool(_BONUS_NONEQ.search(s))
    bon = [] if noneq else list(_BONUS.finditer(s))      # a bonus debenture / preference share is no equity factor
    for mt in bon:
        a, b = int(mt.group(1)), int(mt.group(2))
        if a <= 0 or b <= 0:
            p.kinds.append("unparsed")
            continue
        p.factor *= b / (a + b)
        p.kinds.append("bonus")
    if noneq:
        p.kinds.append("bonus_nonequity")
    if _BONUS_WORD.search(s) and not bon and not noneq:
        p.kinds.append("unparsed")                         # "Bonus" with no clean A:B ratio
    if _SPLITISH.search(s) and not fv:
        p.kinds.append("unparsed")                         # a split / consolidation / face value line without From/To
    if _DEMERGER.search(s):
        p.kinds.append("demerger")
    if _RIGHTS.search(s):                                  # equity, CCD, CCPS or warrant rights: all zeroed (§4.2)
        p.kinds.append("rights")
    if _CAPRED.search(s):
        p.kinds.append("capital_reduction")
    if _SCHEME.search(s) and not _DEMERGER.search(s):
        p.kinds.append("scheme")
    return p


FACTOR_KINDS = ("split", "consolidation", "bonus")
ZERO_KINDS = ("demerger", "rights")
OWNER_KINDS = ("unparsed", "capital_reduction", "scheme", "bonus_nonequity")


@dataclass
class CAEvent:
    isin: str
    symbol: str
    series: str
    ex: str                       # YYYY-MM-DD
    factor: float                 # product over the distinct subjects of this (ISIN, ex-date)
    kinds: List[str]
    subjects: List[str]
    parts: List[Tuple[str, float]] = field(default_factory=list)   # (normalised subject, its factor) if != 1


def events_from(records: Sequence[dict], series: Sequence[str] = EQUITY_SERIES
                ) -> Tuple[List[CAEvent], List[CAEvent], List[CAEvent]]:
    """(factor events, zero events (demerger / rights), owner list (unparsed / capital reduction / scheme / bonus
    debentures)) over equity-series records.  Exact repeats of (ISIN, symbol, ex-date, subject) count once; distinct
    subjects on one (ISIN, ex-date) multiply."""
    seen = set()
    grp: Dict[Tuple[str, str], List[Tuple[dict, Parsed]]] = {}
    for x in records:
        if x["series"].strip() not in series:
            continue
        k = record_key(x)
        if k in seen:
            continue
        seen.add(k)
        grp.setdefault((k[0], k[2]), []).append((x, parse_subject(x["subject"])))
    fac: List[CAEvent] = []
    zero: List[CAEvent] = []
    owner: List[CAEvent] = []
    for (isin, ex), xs in sorted(grp.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        f, kinds, subs, parts = 1.0, [], [], []
        for x, p in xs:
            f *= p.factor
            kinds.extend(p.kinds)
            if p.kinds:
                subs.append(norm_subject(x["subject"]))
            if abs(p.factor - 1) > 1e-12:
                parts.append((norm_subject(x["subject"]), p.factor))
        if not kinds:
            continue
        x0 = xs[0][0]
        ev = CAEvent(isin, x0["symbol"].strip(), x0["series"].strip(), ex, f, kinds, subs, parts)
        if any(k in FACTOR_KINDS for k in kinds) and abs(f - 1) > 1e-12:
            fac.append(ev)
        if any(k in ZERO_KINDS for k in kinds):
            zero.append(ev)
        if any(k in OWNER_KINDS for k in kinds):
            owner.append(ev)
    return fac, zero, owner


# ---------------------------------------------------------------- report (counts only)
def report(root: Path = ROOT) -> None:
    man = load_manifest(root)
    ms = months()
    ok = [m for m in ms if man.get(m, [""])[0] == "ok"]
    print(f"months ok {len(ok)} of {len(ms)}; missing {[m for m in ms if m not in ok]}")
    raw = load_raw(root)
    if raw is None:
        print("CA file incomplete: the adjustment path refuses (design §4.2: no partial CA file)")
        return
    by_y = Counter(parse_exdate(x["exDate"])[:4] for x in raw)        # type: ignore[index]
    print(f"records {len(raw)}; per year {dict(sorted(by_y.items()))}; series {dict(Counter(x['series'] for x in raw).most_common(8))}")
    fac, zero, owner = events_from(raw)
    kinds = Counter(k for e in fac + zero + owner for k in set(e.kinds))
    print(f"equity events: factor {len(fac)}, zero (demerger / rights) {len(zero)}, owner list {len(owner)}; kinds {dict(kinds)}")
    print(f"factor events per year {dict(sorted(Counter(e.ex[:4] for e in fac).items()))}")
    for e in owner:
        print(f"  OWNER {e.ex} {e.symbol:12s} {e.isin} {'/'.join(sorted(set(e.kinds)))}: {' | '.join(e.subjects)[:160]}")


if __name__ == "__main__":
    cmd = sys.argv[1:] or [""]
    if cmd == ["fetch"]:
        fetch()
    elif cmd == ["verify"]:
        verify()
    elif cmd == ["report"]:
        report()
    else:
        raise SystemExit("usage: nse_ca.py fetch | verify | report")

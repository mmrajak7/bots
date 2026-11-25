"""NSE cash-market daily bhavcopy archive downloader (data job only; owner approved 2026-11-21).

Design: JOINER_POSITIONAL_DESIGN.md §4.1 (files), §4.4 P2 (history depth), §9 Q1 (window 2018-12..2026-11).
Fetch method copied from fo_membership.py: urllib, browser User-Agent, 1 s pause between requests, retries.

Types (one subfolder each under NSE_DATA/cm_bhav/):
    cm_old   classic CM bhavcopy zip, dates < 2024-07-08     (has PREVCLOSE, ISIN)
    cm_udiff UDiFF CM bhavcopy zip, dates >= 2024-07-08      (has PrvsClsgPric, ISIN)
    full     sec_bhavdata_full CSV, every date               (has DELIV_QTY; NSE publishes it from ~2019-09)
    mto      MTO delivery .DAT, 2018-12..2019-12 only        (delivery fallback before `full` + overlap to reconcile)

Dates: every weekday 2018-12-01..2026-11-20, plus the Saturday/Sunday sessions in the htf calendar and the
known weekend special sessions (muhurat / DR drills). The CM file decides whether a date is a session.

Manifest statuses (manifest.json, written atomically after every file):
    ok          file on disk and valid
    holiday     CM 404 on a date NOT in the calendar (or weekday-probe era) -> never re-requested
    unpublished `full` 404 before the first `full` OK -> never re-requested
    gap         404 where a file was expected (calendar date, or full/mto 404 on a CM session) -> retried on resume
    failed      non-404 error after retries, or invalid content -> retried on resume
    wrongdate   NSE served a file whose in-file date is another day -> not saved, retried on resume
    skipped     not a session (CM was holiday), so the other types are not requested

    python -I nse_cm_fetch.py fetch     # resumable; per-file lines go to NSE_DATA/cm_bhav/fetch.log
    python -I nse_cm_fetch.py report    # counts per type x year from the manifest; prints counts only

Downloaded files are untrusted: they are only checked as zip/CSV text here, never extracted to disk or executed.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]
import full_asrs as F  # noqa: E402

ROOT = Path(os.environ.get("NSE_DATA", r"C:\Users\mail2\Documents\Projects\NSE_DATA")) / "cm_bhav"  # env NSE_DATA
MANIFEST = ROOT / "manifest.json"
LOG = ROOT / "fetch.log"
START, END = dt.date(2018, 12, 1), dt.date(2026, 11, 20)
CAL_FROM = "2020-01-01"            # before this the calendar does not exist: weekday probing
UDIFF_FROM = "2024-07-08"          # verified: old zip 404 on 2024-07-08, UDiFF OK; old OK on 2024-07-05
MTO_UNTIL = "2019-12-31"
# Weekend sessions verified by probe 2026-11-21 (all CM OK): muhurat 2019-10-27, 2020-11-14, 2023-11-12;
# DR drill Saturdays 2024-03-02, 2024-05-18. Weekday muhurat days are caught by the weekday sweep.
EXTRA_WEEKEND = ["2019-10-27", "2020-11-14", "2023-11-12", "2024-03-02", "2024-05-18"]
MIN_LINES = 500                    # a real day has ~1,700-3,000 rows; an error page or stub has few
PAUSE, RETRIES, BACKOFF = 1.0, 4, (3, 10, 30, 60)
MAX_CONSEC_FAIL, MAX_CONSEC_CAL_404 = 5, 5
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
BASE_URL = "https://nsearchives.nseindia.com"
TYPES = ["cm_old", "cm_udiff", "full", "mto"]


class Abort(RuntimeError):
    """Raised when the run looks throttled/broken: stop rather than record bad statuses."""


def calendar() -> set[str]:
    return set(json.loads((F.BASE / "htf" / "prep.json").read_text())["cal"])


def dates(cal: set[str]) -> list[str]:
    out = set(d for d in cal if START.isoformat() <= d <= END.isoformat())
    out.update(d for d in EXTRA_WEEKEND if START.isoformat() <= d <= END.isoformat())
    d = START
    while d <= END:
        if d.weekday() < 5:
            out.add(d.isoformat())
        d += dt.timedelta(days=1)
    return sorted(out)


def url(kind: str, d: str) -> str:
    y, m, dd = d[:4], int(d[5:7]), d[8:10]
    dmy = f"{dd}{d[5:7]}{y}"
    if kind == "cm_old":
        return f"{BASE_URL}/content/historical/EQUITIES/{y}/{MON[m - 1]}/cm{dd}{MON[m - 1]}{y}bhav.csv.zip"
    if kind == "cm_udiff":
        return f"{BASE_URL}/content/cm/BhavCopy_NSE_CM_0_0_0_{d.replace('-', '')}_F_0000.csv.zip"
    if kind == "full":
        return f"{BASE_URL}/products/content/sec_bhavdata_full_{dmy}.csv"
    if kind == "mto":
        return f"{BASE_URL}/archives/equities/mto/MTO_{dmy}.DAT"
    raise ValueError(kind)


def path(kind: str, d: str) -> Path:
    ext = {"cm_old": ".csv.zip", "cm_udiff": ".csv.zip", "full": ".csv", "mto": ".DAT"}[kind]
    return ROOT / kind / f"{d}{ext}"


def _text(kind: str, data: bytes) -> str:
    if kind in ("cm_old", "cm_udiff"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:   # read in memory only; never extract to disk
            if z.testzip() is not None:
                raise ValueError("corrupt zip member")
            names = z.namelist()
            if len(names) != 1 or not names[0].lower().endswith(".csv"):
                raise ValueError(f"unexpected zip members {names[:3]}")
            data = z.read(names[0])
    return data.decode("utf-8", errors="replace")


def validate(kind: str, d: str, data: bytes) -> None:
    """Raise ValueError unless `data` is the expected file for date `d` (header, row count, in-file date)."""
    text = _text(kind, data)
    lines = text.splitlines()
    if len(lines) < MIN_LINES:
        raise ValueError(f"only {len(lines)} lines")
    y, m, dd = d[:4], int(d[5:7]), d[8:10]
    head = lines[0].replace(" ", "")
    first = lines[1].replace(" ", "")
    if kind == "cm_old":
        want_head, want_date = "SYMBOL,SERIES,OPEN", f"{dd}-{MON[m - 1]}-{y}"
    elif kind == "cm_udiff":
        want_head, want_date = "TradDt,BizDt,Sgmt", d
    elif kind == "full":
        want_head, want_date = "SYMBOL,SERIES,DATE1,PREV_CLOSE", f"{dd}-{MON[m - 1].title()}-{y}"
    else:
        want_head, want_date = "SecurityWiseDeliveryPosition", f"{dd}{d[5:7]}{y}"
        first = lines[1] + lines[2].upper()
        want_date_alt = f"<{dd}-{MON[m - 1]}-{y}>"
        if want_date not in first and want_date_alt not in first:
            raise ValueError("in-file date mismatch")
        want_date = ""
    if not head.startswith(want_head):
        raise ValueError(f"header {lines[0][:40]!r}")
    if kind == "cm_old" and want_date.upper() not in first.upper():
        want_date = f"{dd}-{MON[m - 1]}-{y[2:]},"   # some 2020 files (e.g. 2020-07-13) use dd-Mon-yy
    if want_date and want_date.upper() not in first.upper():
        raise ValueError(f"in-file date mismatch {lines[1][:60]!r}")


def is_valid(kind: str, d: str) -> bool:
    f = path(kind, d)
    if not f.exists():
        return False
    try:
        validate(kind, d, f.read_bytes())
        return True
    except (ValueError, zipfile.BadZipFile, OSError):
        return False


def get(u: str) -> tuple[int, bytes | None, str]:
    """Return (http_status, body, error). 404 is returned at once; anything else is retried with backoff."""
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    err = ""
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                cl = r.headers.get("Content-Length")
                if cl is not None and cl.isdigit() and int(cl) != len(body):
                    raise ValueError(f"truncated {len(body)}/{cl}")
                return 200, body, ""
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None, ""
            err = f"HTTP {e.code}"
        except Exception as e:                       # timeouts, resets, truncation: retry then report
            err = f"{type(e).__name__}: {e}"[:120]
        time.sleep(BACKOFF[attempt])
    return 0, None, err


def write_atomic(f: Path, data: bytes) -> None:
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_name(f.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, f)


def save_manifest(man: dict) -> None:
    write_atomic(MANIFEST, json.dumps(man, indent=0, sort_keys=True).encode())


class Run:
    def __init__(self) -> None:
        ROOT.mkdir(parents=True, exist_ok=True)
        self.man: dict[str, dict[str, list]] = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
        self.log = LOG.open("a", encoding="utf-8")
        self.consec_fail = 0
        self.consec_cal404 = 0

    def note(self, msg: str) -> None:
        self.log.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        self.log.flush()

    def status(self, kind: str, d: str) -> str | None:
        rec = self.man.get(kind, {}).get(d)
        return rec[0] if rec else None

    def set(self, kind: str, d: str, st: str, size: int = 0, err: str = "") -> None:
        self.man.setdefault(kind, {})[d] = [st, size, err]
        save_manifest(self.man)
        self.note(f"{st:11s} {kind:8s} {d} {size} {err}")

    def fetch_one(self, kind: str, d: str) -> int:
        """Return 200 (ok), 404, -1 (wrong-date content, already recorded) or 0 (failed). Resumable: valid file on disk -> ok without a request."""
        if is_valid(kind, d):
            if self.status(kind, d) != "ok":
                self.set(kind, d, "ok", path(kind, d).stat().st_size)
            return 200
        code, body, err = get(url(kind, d))
        time.sleep(PAUSE)
        if code == 200 and body is not None:
            try:
                validate(kind, d, body)
            except (ValueError, zipfile.BadZipFile) as e:
                if "date mismatch" in str(e):     # NSE served another day's file under this name
                    self.consec_fail = 0
                    self.set(kind, d, "wrongdate", 0, str(e)[:120])
                    return -1
                code, err = 0, f"invalid: {e}"[:120]
            else:
                write_atomic(path(kind, d), body)
                self.consec_fail = 0
                return 200
        if code == 0:
            self.consec_fail += 1
            self.set(kind, d, "failed", 0, err)
            if self.consec_fail >= MAX_CONSEC_FAIL:
                raise Abort(f"{self.consec_fail} consecutive failures (last {kind} {d}: {err})")
        else:
            self.consec_fail = 0
        return code

    def run(self) -> None:
        cal = calendar()
        all_d = dates(cal)
        full_seen = any(v[0] == "ok" for v in self.man.get("full", {}).values())
        for d in all_d:
            in_cal = d in cal
            cm = "cm_old" if d < UDIFF_FROM else "cm_udiff"
            if self.status(cm, d) == "holiday":
                continue
            code = self.fetch_one(cm, d)
            if code == 200:
                self.set(cm, d, "ok", path(cm, d).stat().st_size)
                self.consec_cal404 = 0
            elif code == 404:
                if in_cal:                       # calendar says session, file missing: a real gap
                    self.set(cm, d, "gap")
                    self.consec_cal404 += 1
                    if self.consec_cal404 >= MAX_CONSEC_CAL_404:
                        raise Abort(f"{self.consec_cal404} consecutive 404s on calendar dates (last {d})")
                else:
                    self.set(cm, d, "holiday")
                    for k in (("full", "mto") if d <= MTO_UNTIL else ("full",)):
                        self.man.setdefault(k, {})[d] = ["skipped", 0, ""]
                    save_manifest(self.man)
                    continue
            # full: every session (or calendar date whose CM failed/gapped)
            if self.status("full", d) != "unpublished":
                fc = self.fetch_one("full", d)
                if fc == 200:
                    full_seen = True
                    self.set("full", d, "ok", path("full", d).stat().st_size)
                elif fc == 404:
                    self.set("full", d, "gap" if full_seen else "unpublished")
            if d <= MTO_UNTIL:
                mc = self.fetch_one("mto", d)
                if mc == 200:
                    self.set("mto", d, "ok", path("mto", d).stat().st_size)
                elif mc == 404:
                    self.set("mto", d, "gap")
        self.note("RUN COMPLETE")


def fetch() -> None:
    r = Run()
    try:
        r.run()
    except Abort as e:
        r.note(f"ABORT {e}")
        print("ABORT", e)
    except KeyboardInterrupt:
        r.note("INTERRUPTED")
        raise
    finally:
        r.log.close()
    report()


def report() -> None:
    man = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    cols = ["ok", "holiday", "unpublished", "gap", "wrongdate", "failed", "skipped"]
    print(f"{'type':9s} {'year':4s} " + " ".join(f"{c:>11s}" for c in cols) + f" {'MB':>8s}")
    total = 0
    for kind in TYPES:
        by_y: dict[str, dict[str, int]] = {}
        mb: dict[str, int] = {}
        for d, (st, size, _e) in man.get(kind, {}).items():
            by_y.setdefault(d[:4], {}).setdefault(st, 0)
            by_y[d[:4]][st] += 1
            mb[d[:4]] = mb.get(d[:4], 0) + size
        for y in sorted(by_y):
            total += mb[y]
            print(f"{kind:9s} {y:4s} " + " ".join(f"{by_y[y].get(c, 0):>11d}" for c in cols)
                  + f" {mb[y] / 1e6:>8.1f}")
    print(f"total {total / 1e6:.1f} MB")


if __name__ == "__main__":
    {"fetch": fetch, "report": report}.get(_argv[0] if len(_argv) == 1 else "", lambda: sys.exit(
        "usage: nse_cm_fetch.py fetch|report"))()

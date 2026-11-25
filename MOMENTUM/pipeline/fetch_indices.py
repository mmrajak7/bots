"""Stage M1 of NONFO_MOMENTUM_DESIGN.md §10: fetch index histories and fund NAVs.

Writes {YYYY-MM-DD: value} JSON to NSE_DATA/indices/ (the convention joiner_positional.INDEX_DIR expects):
  <key>_tri.json  total-return index (niftyindices, /BackPage/getTotalReturnIndexString)
  <key>_px.json   price index        (niftyindices, /BackPage/getHistoricaldatatabletoString)
  fund_<key>.json fund NAV           (mfapi.in full history, cross-checked against AMFI NAVAll.txt for the latest NAV)
and fetch_log.json (source, first/last date, row count per file) for check P6.

Why chunks: the site refuses ranges over 365 days (its JS, 28-08-2025).  Why a session cookie: the POST needs one.
Requests carry only index names and dates.  Usage: python -I fetch_indices.py [indices|gates|funds|all]
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Dict, List, Optional, Tuple

OUT = Path(os.environ.get("NSE_DATA", str(Path(__file__).resolve().parents[2] / "NSE_DATA"))) / "indices"  # env NSE_DATA
START, END = date(2019, 12, 1), date.today()
# The regime gates (§5) need 200 sessions (G-TREND) and 252 sessions (G-REBOUND) before the first 2020 signal day,
# so the two gate indices start earlier.
GATE_START = date(2018, 1, 1)
GATE_KEYS = ("midcap150", "nifty50")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
SITE = "https://www.niftyindices.com"
PAUSE_S = 1.0                    # polite gap between requests
CHUNK_DAYS = 360                 # under the site's 365-day limit
TIMEOUT_S = 60
RETRIES = 3

INDICES: Dict[str, str] = {      # file key -> niftyindices name
    "mid150mom50": "NIFTY MIDCAP150 MOMENTUM 50",
    "n200mom30": "NIFTY200 MOMENTUM 30",
    "sml250mq100": "NIFTY SMALLCAP250 MOMENTUM QUALITY 100",
    "n500mom50": "NIFTY500 MOMENTUM 50",
    "midcap150": "NIFTY MIDCAP 150",
    "nifty50": "NIFTY 50",
}
FUND_QUERIES: Dict[str, Tuple[str, List[str]]] = {   # key -> (mfapi search text, words the scheme name must contain)
    "uti_n200mom30_direct": ("UTI Nifty 200 Momentum 30", ["uti", "nifty 200 momentum 30", "direct", "growth"]),
    "mid150mom50_direct": ("Midcap150 Momentum 50", ["midcap", "momentum 50", "direct", "growth"]),
}


def log(msg: str) -> None:
    print(f"{datetime.now():%H:%M:%S} {msg}", flush=True)


def opener() -> urllib.request.OpenerDirector:
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    op.addheaders = [("User-Agent", UA)]
    op.open(f"{SITE}/reports/historical-data", timeout=TIMEOUT_S).read()     # sets the session cookie
    return op


def post(op: urllib.request.OpenerDirector, endpoint: str, name: str, a: date, b: date) -> list:
    cinfo = "{'name':'%s','startDate':'%s','endDate':'%s','indexName':'%s'}" % (
        name, a.strftime("%d-%b-%Y"), b.strftime("%d-%b-%Y"), name)
    body = json.dumps({"cinfo": cinfo}).encode()
    req = urllib.request.Request(f"{SITE}/BackPage/{endpoint}", data=body, method="POST", headers={
        "Content-Type": "application/json; charset=utf-8", "Origin": SITE, "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{SITE}/reports/historical-data"})
    last: Optional[Exception] = None
    for k in range(RETRIES):
        try:
            raw = op.open(req, timeout=TIMEOUT_S).read().decode("utf-8")
            data = json.loads(raw)
            if isinstance(data, dict) and "d" in data:          # older ASP.NET wrapping, kept defensively
                data = json.loads(data["d"])
            if not isinstance(data, list):
                raise ValueError(f"unexpected payload: {raw[:200]}")
            return data
        except Exception as e:                                   # network or format: retry, then fail loudly
            last = e
            log(f"  retry {k + 1}/{RETRIES} {endpoint} {name} {a}..{b}: {e}")
            time.sleep(PAUSE_S * 3 * (k + 1))
    raise RuntimeError(f"{endpoint} {name} {a}..{b} failed: {last}")


def to_float(s: str) -> Optional[float]:
    s = (s or "").replace(",", "").strip()
    return None if s in ("", "-") else float(s)


def fetch_index(op: urllib.request.OpenerDirector, name: str, tri: bool, start: date = START) -> Dict[str, float]:
    endpoint = "getTotalReturnIndexString" if tri else "getHistoricaldatatabletoString"
    dkey, vkey = ("Date", "TotalReturnsIndex") if tri else ("HistoricalDate", "CLOSE")
    out: Dict[str, float] = {}
    a = start
    while a <= END:
        b = min(a + timedelta(days=CHUNK_DAYS), END)
        for row in post(op, endpoint, name, a, b):
            v = to_float(str(row.get(vkey, "")))
            if v is not None and row.get(dkey):
                out[datetime.strptime(row[dkey].strip(), "%d %b %Y").date().isoformat()] = v
        time.sleep(PAUSE_S)
        a = b + timedelta(days=1)
    return dict(sorted(out.items()))


def get_json(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return json.loads(urllib.request.urlopen(req, timeout=TIMEOUT_S).read().decode("utf-8"))


def fetch_fund(query: str, must: List[str]) -> Tuple[Optional[dict], Dict[str, float]]:
    hits = get_json("https://api.mfapi.in/mf/search?q=" + urllib.request.quote(query))
    ok = [h for h in hits if all(w in h["schemeName"].lower() for w in must)
          and "idcw" not in h["schemeName"].lower() and "dividend" not in h["schemeName"].lower()]
    if len(ok) != 1:
        log(f"  fund '{query}': {len(ok)} matches, need exactly 1: {[h['schemeName'] for h in ok] or hits[:8]}")
        return None, {}
    meta = ok[0]
    full = get_json(f"https://api.mfapi.in/mf/{meta['schemeCode']}")
    nav = {datetime.strptime(r["date"], "%d-%m-%Y").date().isoformat(): float(r["nav"]) for r in full["data"]}
    return meta, dict(sorted(nav.items()))


def amfi_latest() -> Dict[str, Tuple[str, float]]:
    """schemeCode -> (date, NAV) from AMFI's official daily file, to cross-check mfapi's last NAV."""
    # Layout seen 2026-11-24: Code;ISIN growth;ISIN reinv;Name;Plan;Option;NAV;Date (8 fields). NAV and date are
    # the last two fields in both the old 6-field and the new 8-field layout, so read them from the end.
    req = urllib.request.Request("https://portal.amfiindia.com/spages/NAVAll.txt", headers={"User-Agent": UA})
    out: Dict[str, Tuple[str, float]] = {}
    for line in urllib.request.urlopen(req, timeout=TIMEOUT_S).read().decode("utf-8", "replace").splitlines():
        p = line.split(";")
        if len(p) >= 6 and p[0].strip().isdigit():
            try:
                out[p[0].strip()] = (datetime.strptime(p[-1].strip(), "%d-%b-%Y").date().isoformat(), float(p[-2]))
            except ValueError:
                pass
    if not out:
        raise RuntimeError("AMFI NAVAll.txt parsed to nothing: layout changed again?")
    return out


def save(key: str, series: Dict[str, float], source: str, logbook: dict) -> None:
    if not series:
        log(f"  {key}: EMPTY, not written")
        logbook[key] = {"source": source, "rows": 0}
        return
    (OUT / f"{key}.json").write_text(json.dumps(series, indent=0))
    ks = list(series)
    logbook[key] = {"source": source, "first": ks[0], "last": ks[-1], "rows": len(ks)}
    log(f"  {key}: {len(ks)} rows {ks[0]}..{ks[-1]}")


def main(what: str) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    lf = OUT / "fetch_log.json"
    logbook = json.loads(lf.read_text()) if lf.exists() else {}
    bad = 0
    if what in ("indices", "gates", "all"):
        op = opener()
        for key, name in INDICES.items():
            if what == "gates" and key not in GATE_KEYS:
                continue
            for tri in (True, False):
                k = f"{key}_{'tri' if tri else 'px'}"
                log(f"{k} <- {name}")
                try:
                    save(k, fetch_index(op, name, tri, GATE_START if key in GATE_KEYS else START), f"niftyindices {name} {'TRI' if tri else 'price'}", logbook)
                except RuntimeError as e:
                    log(f"  FAILED {e}")
                    bad += 1
                if not logbook.get(k, {}).get("rows"):
                    bad += 1
    if what in ("funds", "all"):
        latest = amfi_latest()
        for key, (q, must) in FUND_QUERIES.items():
            log(f"fund_{key} <- '{q}'")
            meta, nav = fetch_fund(q, must)
            if meta is None:
                bad += 1
                continue
            code = str(meta["schemeCode"])
            chk = latest.get(code)
            last = list(nav)[-1] if nav else None
            agree = bool(chk and last and chk[0] == last and abs(chk[1] - nav[last]) < 1e-6)
            log(f"  {meta['schemeName']} ({code}); AMFI latest {chk}; mfapi last {last} {nav.get(last)}; agree={agree}")
            save(f"fund_{key}", nav, f"mfapi.in {code} {meta['schemeName']}; AMFI latest {chk}; agree={agree}", logbook)
            if not agree:
                bad += 1
    lf.write_text(json.dumps(logbook, indent=1))
    log(f"done; problems: {bad}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "all"))

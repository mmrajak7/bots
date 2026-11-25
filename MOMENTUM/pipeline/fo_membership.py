"""Historical NSE F&O stock membership (survivorship study, owner OK 2026-11-20).

One F&O bhavcopy per month (first market session of the month, calendar from htf prep), 2020-01..2026-11.
The membership of a month = symbols with stock futures (FUTSTK / STF) in that bhavcopy.

    python -I fo_membership.py fetch    # downloads ~83 zips into the scratchpad fo_bhav/ (NSE archives)
    python -I fo_membership.py build    # -> data/fo_membership.json {YYYY-MM: [symbols]} + summary counts
"""
from __future__ import annotations

import csv
import io
import json
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_argv = sys.argv[1:]
sys.argv = sys.argv[:1]
import full_asrs as F  # noqa: E402

DL = F.BASE.parent / "fo_bhav"
OUT = HERE / "data" / "fo_membership.json"
NEW_FMT_FROM = "2024-07-08"
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def first_sessions() -> dict[str, str]:
    cal = json.loads((F.BASE / "htf" / "prep.json").read_text())["cal"]
    out: dict[str, str] = {}
    for d in cal:
        out.setdefault(d[:7], d)
    return out


def url(d: str) -> str:
    y, m, dd = d[:4], int(d[5:7]), d[8:10]
    if d >= NEW_FMT_FROM:
        return f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{d.replace('-', '')}_F_0000.csv.zip"
    return f"https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{y}/{MON[m - 1]}/fo{dd}{MON[m - 1]}{y}bhav.csv.zip"


def fetch() -> None:
    DL.mkdir(parents=True, exist_ok=True)
    for ym, d in sorted(first_sessions().items()):
        f = DL / f"{d}.zip"
        if f.exists() and zipfile.is_zipfile(f):
            continue
        req = urllib.request.Request(url(d), headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(3):
            try:
                data = urllib.request.urlopen(req, timeout=60).read()
                break
            except Exception as e:                      # transient: retry, then report loudly
                if attempt == 2:
                    print("FAILED", d, type(e).__name__, flush=True)
                    data = None
                time.sleep(3)
        if data:
            tmp = f.with_suffix(".tmp")
            tmp.write_bytes(data)
            if not zipfile.is_zipfile(tmp):
                print("NOT A ZIP", d, flush=True)
                tmp.unlink()
                continue
            tmp.replace(f)
            print("ok", d, len(data), flush=True)
        time.sleep(1.0)


def stock_futures(zpath: Path) -> set[str]:
    with zipfile.ZipFile(zpath) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8", errors="replace")
    rows = csv.DictReader(io.StringIO(text))
    out = set()
    for r in rows:
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("INSTRUMENT") == "FUTSTK":                       # old format
            out.add(r["SYMBOL"])
        elif r.get("FinInstrmTp") == "STF":                       # UDiFF format
            out.add(r["TckrSymb"])
    return out


def build() -> None:
    mem = {}
    for ym, d in sorted(first_sessions().items()):
        f = DL / f"{d}.zip"
        if not f.exists():
            raise SystemExit(f"missing {f}: run fetch")
        s = stock_futures(f)
        if len(s) < 100:
            raise SystemExit(f"{d}: only {len(s)} stock futures — parse problem")
        mem[ym] = sorted(s)
    OUT.write_text(json.dumps(mem, indent=0))
    ever = set().union(*map(set, mem.values()))
    import y2026_asrs as Y
    uni = set(json.loads(Y.UNIVERSE.read_text()))
    print(f"{len(mem)} months; members per month {min(map(len, mem.values()))}..{max(map(len, mem.values()))}; "
          f"ever-members {len(ever)}")
    if uni:
        print(f"ever-members not in today's 218: {len(ever - uni)}; today's 218 never in history: {len(uni - ever)}")


if __name__ == "__main__":
    {"fetch": fetch, "build": build}.get(_argv[0] if len(_argv) == 1 else "", lambda: sys.exit(
        "usage: fo_membership.py fetch|build"))()

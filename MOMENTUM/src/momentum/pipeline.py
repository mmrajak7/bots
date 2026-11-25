"""Live data pipeline = the tested research pipeline, unchanged, pointed at the plan month (build spec §C4, §C5).

The research modules (BOTS/SRS) are imported, never edited for behaviour. As `SRS/momentum_rehearse.py` proved on
2026-11-24 (bands 2020-01..2026-11 bit-equal, November decision equal), the window constants are moved IN MEMORY and
every DERIVED output (panel, store, band files) goes to state/derived, so the frozen study data is never touched.

Signal day = the last session of month m-1. END / LAST_MONTH / BAND_TO are set from the signal day, never from "today"
(spec G21: a re-run the next evening must not pull in the new month's first session).

F&O exclusion (frozen design §3.4, R11, D19): members_override[m] = stock-futures underlyings in the F&O bhavcopy of the
signal day + `fo_announced.json` additions effective by m's first session. It is injected BEFORE the band's top-150
cut through survivorship.load_membership (the single place the band build reads membership); nothing is dropped after.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import util

log = logging.getLogger("momentum.pipeline")


class PipelineError(Exception):
    pass


def month_add(m: str, k: int) -> str:
    y, mo = int(m[:4]), int(m[5:7]) + k
    y += (mo - 1) // 12
    mo = (mo - 1) % 12 + 1
    return f"{y:04d}-{mo:02d}"


@dataclass
class Modules:
    N: Any      # nonfo_momentum
    J: Any      # joiner_positional
    NS: Any     # nonfo_srs
    V: Any      # survivorship
    NF: Any     # nse_cm_fetch
    CA: Any     # nse_ca
    FI: Any     # fetch_indices
    FM: Any     # fo_membership


def import_research(srs_dir: Path, nse_data: Path) -> Modules:
    os.environ["NSE_DATA"] = str(nse_data)
    os.environ.setdefault("SRS_BASE", str(nse_data / "srs_base" / "srs_week"))
    if str(srs_dir) not in sys.path:
        sys.path.insert(0, str(srs_dir))
    argv, sys.argv = sys.argv, sys.argv[:1]       # some research modules read sys.argv at import
    try:
        import fetch_indices as FI
        import fo_membership as FM
        import nonfo_momentum as N
    finally:
        sys.argv = argv
    return Modules(N=N, J=N.J, NS=N.NS, V=N.SV, NF=N.J.NF, CA=N.J.CA, FI=FI, FM=FM)


def fo_url_fetch(M: Modules, d: str, dest: Path) -> Set[str]:
    """Download the F&O bhavcopy of session d (fo_membership.url logic) and return its stock-futures symbols."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not (dest.exists() and dest.stat().st_size > 0):
        req = urllib.request.Request(M.FM.url(d), headers={"User-Agent": "Mozilla/5.0"})
        last: Optional[Exception] = None
        for attempt in range(3):
            try:
                data = urllib.request.urlopen(req, timeout=60).read()
                break
            except Exception as e:  # noqa: BLE001 - network: retry, then fail loudly
                last = e
                time.sleep(2 * (attempt + 1))
        else:
            raise PipelineError(f"F&O bhavcopy {d} not available: {last}")
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
    syms = M.FM.stock_futures(dest)
    if len(syms) < 100:
        raise PipelineError(f"F&O bhavcopy {d}: only {len(syms)} stock futures (expected ~200): refused")
    return set(syms)


def announced_additions(M: Modules, m: str) -> Set[str]:
    """NEW F&O entrants for month m from fo_announced.json (list of {symbol, member_month, withdrawn?},
    joiner_positional.py:89). Only member_month == m: older additions are already in the signal-day F&O bhavcopy if
    they are still members, and names that later LEFT F&O must not be excluded (bug found by the 11-25 dry run:
    `<= m` excluded ZOMATO / DELTACORP / BSOFT ... and moved 17 names in the band)."""
    p = Path(M.J.ANNOUNCED_FILE)
    rows = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    if not isinstance(rows, list):
        raise PipelineError(f"{p}: expected a list")
    return {r["symbol"] for r in rows if r.get("member_month") == m and not r.get("withdrawn")}


def announced_sha(M: Modules) -> str:
    p = Path(M.J.ANNOUNCED_FILE)
    return util.sha256_text(p.read_text(encoding="utf-8")) if p.exists() else "absent"


def fo_checked_stamp(control_dir: Path, m: str) -> Optional[str]:
    """The owner's 'circulars read for month m' stamp, kept beside ARM (control/fo_checked.json {month: date}); the
    research file keeps its tested list format, so the stamp cannot live inside it."""
    p = control_dir / "fo_checked.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    v = raw.get(m) if isinstance(raw, dict) else None
    return v if isinstance(v, str) else None


@dataclass
class Window:
    plan_month: str          # m, e.g. 2026-12
    signal_day: dt.date      # last session of m-1
    derived: Path            # state/derived/<m>_<signal_day>


def configure(M: Modules, w: Window, members_m: List[str]) -> None:
    """Point the research modules at the window. In memory only (no module file is edited)."""
    m = w.plan_month
    M.NF.END = w.signal_day
    orig_months, orig_raw = M.CA.months, M.CA.load_raw
    last_ca = m                                   # CA files through the plan month (current AND next, §3.2)
    if not getattr(M.CA, "_mom_wrapped", False):
        M.CA.months = lambda a=M.CA.FIRST_MONTH, b=None: orig_months(a, b or M.CA._mom_last)      # type: ignore
        M.CA.load_raw = (lambda root=M.CA.ROOT, first=M.CA.FIRST_MONTH, last=None:               # type: ignore
                         orig_raw(root, first, last or M.CA._mom_last))
        M.CA._mom_wrapped = True
    M.CA._mom_last = last_ca
    d = w.derived
    d.mkdir(parents=True, exist_ok=True)
    M.FI.OUT = d / "indices"
    M.J.PANEL_FILE, M.J.BUILD_INFO, M.J.OUT_DIR = d / "panel.npz", d / "build_info.json", d
    base_mem = M.V.load_membership.__wrapped__() if hasattr(M.V.load_membership, "__wrapped__") \
        else M.V.load_membership()
    mem = dict(base_mem)
    mem[m] = sorted(members_m)

    def load_membership() -> Dict[str, List[str]]:
        return mem
    load_membership.__wrapped__ = getattr(M.V.load_membership, "__wrapped__", M.V.load_membership)  # type: ignore
    M.V.load_membership = load_membership
    M.NS.BAND_TO = m
    M.NS.STORE_PKL, M.NS.BAND_FILE, M.NS.FACTS_FILE, M.NS.B150_DIR = (
        d / "bhav_store.pkl", d / "band.json", d / "facts.json", d / "b150")
    # band counts need membership keys for every month incl. m (report lines only; not used by the decision)
    M.NS.all_member_keys = lambda *a, **k: {}
    M.NS.band_count_lines = lambda *a, **k: ["  (band counts skipped in the live build)"]


def refresh(M: Modules) -> Dict[str, Any]:
    """NSE downloads for the window. Statuses are read back from the manifests by the caller."""
    t = time.time()
    M.NF.fetch()
    M.CA.fetch()
    M.FI.main("all")
    return {"seconds": round(time.time() - t)}


def unmark_sessions(M: Modules, sessions: List[str]) -> List[str]:
    """The research fetcher writes a 404 on a date outside its calendar as 'holiday' and never asks again
    (nse_cm_fetch.Run.run). In the live path Kite's session list is the truth: any Kite session marked holiday / gap /
    failed / unpublished is removed from the manifest so the next fetch requests it again. -> dates unmarked."""
    if not M.NF.MANIFEST.exists():
        return []
    man = json.loads(M.NF.MANIFEST.read_text())
    done = []
    for d in sessions:
        for kind in ("cm_udiff", "cm_old", "full"):
            v = man.get(kind, {}).get(d)
            if v and v[0] in ("holiday", "gap", "failed", "unpublished", "skipped"):
                del man[kind][d]
                done.append(f"{kind}:{d}")
    if done:
        M.NF.save_manifest(man)
    return done


def cm_status(M: Modules, d: str) -> str:
    man = json.loads(M.NF.MANIFEST.read_text())
    for kind in ("cm_udiff", "cm_old"):
        v = man.get(kind, {}).get(d)
        if v:
            return v[0]
    return "absent"


def rebuild(M: Modules) -> Dict[str, Any]:
    t = time.time()
    M.J.build()
    t1 = time.time()
    M.NS.band_cmd()
    return {"panel_s": round(t1 - t), "band_s": round(time.time() - t1)}


@dataclass
class Context:
    ctx: Any
    mk: Any
    sc: Any
    p: Any
    car: Any
    ca_ok: bool
    ca_lines: List[str]


def load_context(M: Modules, w: Window) -> Context:
    N, J = M.N, M.J
    p = J.Panel.load(w.derived / "panel.npz")
    car = J.ca_adjust(p)
    ok_ca, lines = J.p3_ca_verdict(p, car)
    _ = p.G
    b150 = w.derived / "b150"
    ms = sorted(f.stem[5:] for f in b150.glob("b150_*.json"))
    bands = {mm: N.load_band_file(mm, b150) for mm in ms}
    sets, _u, _mo, _last = J.load_context(p)
    mk = N.Market(p, bands, sets, N.load_events())
    sc = N.Scorer(mk)
    ctx = N.Ctx(mk, sc, {}, {}, {}, {}, [])
    return Context(ctx=ctx, mk=mk, sc=sc, p=p, car=car, ca_ok=bool(ok_ca), ca_lines=list(lines))


def decide_from_cash(M: Modules, c: Context, m: str, cash: float, n: int) -> Dict[str, Any]:
    """Month-1 decision: an empty book (held = busy = {}, frozen = 0) calls Book._decide UNCHANGED, which returns the
    first n names of the BLEND order (nonfo_momentum.py:1245-1271, rebalance():708-721). Spec gate None -> off False."""
    N = M.N
    spec = N.Spec(account=cash)
    if spec.gate is not None or spec.arm != "BLEND" or spec.n != n:
        raise PipelineError(f"unexpected Spec defaults: gate={spec.gate} arm={spec.arm} n={spec.n}")
    book = N.Book(c.mk, c.sc, spec, c.mk.G)
    sells, buys, off = book._decide(m)
    if sells or off:
        raise PipelineError(f"empty book produced sells={sells} off={off}")
    s = c.mk.sig(m)
    order = c.sc.order(spec.arm, m, spec.pool, [])
    rank = {ch: k + 1 for k, ch in enumerate(order)}
    out = []
    for ch in buys:
        out.append({"chain": int(ch), "symbol": c.mk.sym_at(ch, s), "isin": c.p.isins[int(c.p.isin_idx[ch, s])],
                    "series": series_name(M, c.p, ch, s), "rank": rank[ch],
                    "score_milli": int(round(1000 * c.sc.score(spec.arm, ch, m, spec.pool)))})
    ranked = [{"chain": int(ch), "symbol": c.mk.sym_at(ch, s), "rank": k + 1,
               "score_milli": int(round(1000 * c.sc.score(spec.arm, ch, m, spec.pool)))}
              for k, ch in enumerate(order)]
    return {"signal_day": c.mk.sessions[s], "buys": out, "ranked": ranked}


def series_name(M: Modules, p: Any, ch: int, s: int) -> str:
    code = int(p.series[ch, s])
    names = {v: k for k, v in M.J.SERIES.items()}
    if code not in names:
        raise PipelineError(f"chain {ch}: unknown series code {code}")
    return names[code]


def freeze_band(M: Modules, w: Window, bands_dir: Path, members_m: List[str], rev: int = 0) -> Tuple[str, str]:
    """Write-once copies of the band and member list used live for month m, per data revision (E38).
    -> (band sha, members sha)."""
    src = w.derived / "b150" / f"b150_{w.plan_month}.json"
    text = src.read_text(encoding="utf-8")
    util.write_once(bands_dir / f"b150_{w.plan_month}_r{rev}.json", text)
    mtext = json.dumps({"month": w.plan_month, "signal_day": w.signal_day.isoformat(), "members": sorted(members_m)},
                       indent=1) + "\n"
    util.write_once(bands_dir / f"members_{w.plan_month}_r{rev}.json", mtext)
    return util.sha256_text(text), util.sha256_text(mtext)

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
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import util

log = logging.getLogger("momentum.pipeline")


class PipelineError(Exception):
    pass


MIN_STOCK_FUTURES = 100         # same floor as the research build (pipeline/fo_membership.py build): fewer = a parse problem


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
    try:
        syms = M.FM.stock_futures(dest)
    except Exception as e:  # noqa: BLE001 - an error page or a cut download was saved: never keep it as the cache
        dest.unlink(missing_ok=True)
        raise PipelineError(f"F&O bhavcopy {d}: unreadable download removed, fetched again on the next run "
                            f"({type(e).__name__}: {e})") from e
    if len(syms) < MIN_STOCK_FUTURES:
        dest.unlink(missing_ok=True)
        raise PipelineError(f"F&O bhavcopy {d}: only {len(syms)} stock futures (expected ~200): refused")
    return set(syms)


def fo_continuing(zpath: Path, signal_day: str) -> Set[str]:
    """Stock futures in the signal-day F&O bhavcopy with a contract expiring AFTER the signal day. When the monthly
    expiry IS the signal day, a stock leaving F&O still has a row that day but none on the next session, and the
    research membership (first-session bhavcopy, fo_membership.py) does not count it. Checked on the Pi 2026-11-28:
    continuing + vet entrants == fo_membership.json[m] exactly for 2025-08 (10 leavers), 2025-10, 2026-01, 2026-04."""
    import csv
    import io
    import zipfile
    with zipfile.ZipFile(zpath) as z:
        text = z.read(z.namelist()[0]).decode("utf-8", errors="replace")
    out: Set[str] = set()
    seen = 0
    for r in csv.DictReader(io.StringIO(text)):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        if r.get("FinInstrmTp") == "STF":                           # UDiFF (every live signal day)
            sym, exp = r.get("TckrSymb", ""), r.get("XpryDt", "")
        elif r.get("INSTRUMENT") == "FUTSTK":                       # old format: 31-Jul-2025
            sym = r.get("SYMBOL", "")
            try:
                exp = dt.datetime.strptime(r.get("EXPIRY_DT", ""), "%d-%b-%Y").date().isoformat()
            except ValueError:
                exp = ""
        else:
            continue
        if len(exp) != 10:
            raise PipelineError(f"F&O bhavcopy {zpath.name}: stock future {sym} without a readable expiry")
        seen += 1
        if exp > signal_day:
            out.add(sym)
    if len(out) < MIN_STOCK_FUTURES:
        raise PipelineError(f"F&O bhavcopy {zpath.name}: only {len(out)} stock futures continue after "
                            f"{signal_day} ({seen} rows)")
    return out


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


def configure(M: Modules, w: Window, members_m: List[str], kite_sessions: Sequence[str] = ()) -> None:
    """Point the research modules at the window. In memory only (no module file is edited).
    kite_sessions: Kite's session list is the truth for which days to request. The research fetcher only asks for
    weekdays + its frozen calendar + a hand list of weekend sessions (EXTRA_WEEKEND, ends 2024), so a weekend session
    after that (muhurat Sun 2026-11-08) was never requested and the evening stopped on a missing CM file."""
    m = w.plan_month
    M.NF.END = w.signal_day
    weekend = {d for d in kite_sessions if dt.date.fromisoformat(d).weekday() >= 5}
    M.NF.EXTRA_WEEKEND = sorted(set(M.NF.EXTRA_WEEKEND) | weekend)
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


@dataclass
class LiveHolding:
    """One live position as decide_live sees it (January spec §4.1). state: held | selling. bought: the first buy
    date (ISO), used to find the chain the ledger ISIN belonged to at the buy."""
    position_id: str
    isin: str
    state: str
    bought: str


@dataclass
class LivePending:
    position_id: str
    isin: str


def chain_for(mk: Any, isin: str, s: int) -> Optional[int]:
    """The research chain that carried the ISIN on a row at or before the signal session (Market.chain_of_isin, the
    lookup the research merger swap uses). None -> NO_CHAIN."""
    return mk.chain_of_isin(isin, s)


def decide_live(M: Modules, c: Context, m: str, holdings: Sequence[LiveHolding], pending: Sequence[LivePending],
                n: int) -> Dict[str, Any]:
    """Month m's decision from the REAL book (frozen design §4.1; January spec §4.1). The ledger is mapped onto a
    research Book state and Book._decide runs UNCHANGED (busy filter, frozen count incl. pending buys):
      held                               -> Pos(state held)
      selling                            -> Pos(state selling)       (busy: no slot, never a buy candidate)
      pending buy                        -> pending_buys entry       (counts in frozen)
      held, no CM row for > DEAD_GAP sessions up to the signal day -> left out, reported WRITTEN_OFF (D-L19: the
          research freezes from the last row with look-ahead and releases the slot at t_gap + DEAD_GAP; the live
          book cannot know a gap will last, so a shorter gap stays held)
      ISIN maps to no chain               -> left out (slot freed, FD §4.1), sell reason NO_CHAIN
    Sell reason: RANK when the chain is ranked (rank > sell_rank), NO_SCORE when it has no score (absent from the
    order). -> {signal_day, sells, buys, written_off, ranked}."""
    N = M.N
    spec = N.Spec(account=1.0)
    if spec.gate is not None or spec.arm != "BLEND" or spec.n != n or spec.mode != "buffer":
        raise PipelineError(f"unexpected Spec defaults: gate={spec.gate} arm={spec.arm} n={spec.n} mode={spec.mode}")
    mk = c.mk
    s = mk.sig(m)
    book = N.Book(mk, c.sc, spec, mk.G)
    by_chain: Dict[int, List[LiveHolding]] = {}
    no_chain: List[LiveHolding] = []
    written_off: List[Dict[str, Any]] = []
    pid = 0
    for h in holdings:
        if h.state not in ("held", "selling"):
            raise PipelineError(f"{h.position_id}: bad live state {h.state}")
        ch = chain_for(mk, h.isin, s)
        if ch is None:
            if h.state == "held":                  # a sell already under way needs no decision (it is no buy candidate)
                no_chain.append(h)
            continue
        last = mk.last_row(ch, s)
        if h.state == "held" and (last < 0 or s - last > N.DEAD_GAP):
            written_off.append({"position_id": h.position_id, "isin": h.isin, "chain": int(ch),
                                "last_row": mk.sessions[last] if last >= 0 else None})
            continue
        pid += 1
        book.pos[pid] = N.Pos(pid, ch, 0, h.bought[:7], [], state=h.state, chains=[ch])
        by_chain.setdefault(ch, []).append(h)
    for b in pending:
        ch = chain_for(mk, b.isin, s)
        if ch is None:                         # still a slot (tested: pending buys count); reported, never fatal
            written_off.append({"position_id": b.position_id, "isin": b.isin, "chain": None, "last_row": None,
                                "pending_no_chain": True})
            book.pending_buys.append([-1, 0.0, s, 1, m])
            continue
        book.pending_buys.append([ch, 0.0, s, 1, m])
    sells, buys, off = book._decide(m)
    if off:
        raise PipelineError("the gate is off in a gate-less Spec")
    held_chains = [q.c for q in book.pos.values() if q.state == "held"]
    order = c.sc.order(spec.arm, m, spec.pool, held_chains)
    rank = {ch: k + 1 for k, ch in enumerate(order)}

    def score(ch: int) -> Optional[int]:
        v = c.sc.score(spec.arm, ch, m, spec.pool)
        return None if v is None else int(round(1000 * v))
    out_sells: List[Dict[str, Any]] = []
    left = list(sells)
    for ch in sorted(set(sells)):
        for h in [h for h in by_chain.get(ch, []) if h.state == "held"]:
            if ch not in left:
                break
            left.remove(ch)
            out_sells.append({"position_id": h.position_id, "isin": h.isin, "chain": int(ch),
                              "symbol": mk.sym_at(ch, s), "reason": "RANK" if ch in rank else "NO_SCORE",
                              "rank": rank.get(ch), "score_milli": score(ch)})
    if left:
        raise PipelineError(f"sell chains not matched to a live position: {left}")
    for h in no_chain:
        out_sells.append({"position_id": h.position_id, "isin": h.isin, "chain": None, "symbol": None,
                          "reason": "NO_CHAIN", "rank": None, "score_milli": None})
    out_buys = [{"chain": int(ch), "symbol": mk.sym_at(ch, s), "isin": c.p.isins[int(c.p.isin_idx[ch, s])],
                 "series": series_name(M, c.p, ch, s), "rank": rank[ch], "score_milli": score(ch)} for ch in buys]
    ranked = [{"chain": int(ch), "symbol": mk.sym_at(ch, s), "rank": k + 1, "score_milli": score(ch)}
              for k, ch in enumerate(order)]
    return {"signal_day": mk.sessions[s], "sells": sorted(out_sells, key=lambda x: x["position_id"]),
            "buys": out_buys, "written_off": written_off, "ranked": ranked}


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

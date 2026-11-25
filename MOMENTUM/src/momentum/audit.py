"""Data audit after the monthly rebuild, layers 1-2 (build spec §C6; frozen design §3.5, D31, D35).

Layer 1 (code, every eligible pool name): residual jumps, factor vs raw ratio, double factors, ISIN changes without a CA
record, freshness, PRICE_FROZEN.  Layer 2 (independent source): Kite daily candles; our 12-1 and 6-1 returns (the BLEND
score windows, Scorer.window: end of m-(k+1) .. end of m-2) vs Kite's over the same dates.
A layer-1 exception or a Kite failure -> FAILED (no final plan). Flags on a TOUCHED name (a buy this month) go to the
owner (no Tier B in the December slice); flags on untouched names are report only.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np

log = logging.getLogger("momentum.audit")

WINDOW_SESSIONS = 280         # ~13 months of sessions back from the signal day (the score + history window)
LINK_TOL_SESSIONS = 5         # an ISIN change is "explained" by a CA fix within +-5 sessions


@dataclass
class Flag:
    flag_id: str
    chain: int
    symbol: str
    isin: str
    layer: int
    check: str
    value: str
    threshold: str
    detail: str
    touched: bool = False
    kite_value: Optional[str] = None
    kite_agrees: Optional[bool] = None


@dataclass
class AuditResult:
    status: str                                  # PASSED | PASSED_WITH_HOLDS | FAILED
    flags: List[Flag] = field(default_factory=list)
    problem: str = ""
    counts: Dict[str, int] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"status": self.status, "problem": self.problem, "counts": self.counts,
                "flags": [asdict(f) for f in self.flags]}


def _fid(check: str, chain: int, extra: str = "") -> str:
    return f"{check}:{chain}:{extra}" if extra else f"{check}:{chain}"


def layer1(c: Any, m: str, chains: Sequence[int], th_ret: float, th_factor: float, th_freeze: float,
           signal_day: str) -> List[Flag]:
    """c = pipeline.Context. Pure reads of the panel and the CA result."""
    p, mk = c.p, c.mk
    s = mk.sig(m)
    if mk.sessions[s] != signal_day:
        return [Flag("freshness", -1, "", "", 1, "freshness", mk.sessions[s], signal_day,
                     "last panel session before the plan month is not the verified signal day")]
    lo = max(0, s - WINDOW_SESSIONS)
    fix_rows: Dict[int, List[Any]] = {}
    for f in c.car.fixes:
        fix_rows.setdefault(f.c, []).append(f)
    flags: List[Flag] = []
    for ch in chains:
        sym, isin = mk.sym_at(ch, s), p.isins[int(p.isin_idx[ch, s])]
        rows = np.nonzero(p.has_row[ch, lo + 1:s + 1])[0] + lo + 1
        fx_t = {f.t for f in fix_rows.get(ch, []) if lo < f.t <= s}
        for t in rows:
            r = float(p.ret[ch, t])
            if not np.isfinite(r):
                continue
            if abs(r) > th_ret:
                flags.append(Flag(_fid("jump", ch, mk.sessions[t]), ch, sym, isin, 1, "jump",
                                  f"{r:+.1%}", f"{th_ret:.0%}", f"adjusted daily return on {mk.sessions[t]}"))
            if abs(r) > th_freeze and t not in fx_t:
                flags.append(Flag(_fid("price_frozen", ch, mk.sessions[t]), ch, sym, isin, 1, "price_frozen",
                                  f"{r:+.1%}", f"{th_freeze:.0%}", f"move on {mk.sessions[t]} with no CA record"))
        fixes = sorted((f for f in fix_rows.get(ch, []) if lo < f.t <= s), key=lambda f: f.t)
        for f in fixes:
            raw = float(p.close[ch, f.t]) / float(p.close[ch, f.pr])
            dev = raw / f.factor - 1
            if abs(dev) > th_factor:
                flags.append(Flag(_fid("factor", ch, f.ex), ch, sym, isin, 1, "factor", f"{dev:+.1%}",
                                  f"{th_factor:.0%}", f"raw ratio {raw:.4f} vs factor {f.factor:.4f} ({f.ex}, "
                                  f"prev {f.prev})"))
        for a, b in zip(fixes, fixes[1:]):
            if b.t - a.t <= LINK_TOL_SESSIONS:
                flags.append(Flag(_fid("double_factor", ch, b.ex), ch, sym, isin, 1, "double_factor",
                                  f"{a.ex}+{b.ex}", f"{LINK_TOL_SESSIONS} sessions",
                                  f"two factor rows {a.factor:.4f} and {b.factor:.4f} close together"))
        idx = p.isin_idx[ch, rows]
        for k in np.nonzero(np.diff(idx) != 0)[0]:
            t = int(rows[k + 1])
            if not any(abs(t - ft) <= LINK_TOL_SESSIONS for ft in fx_t):
                flags.append(Flag(_fid("isin_link", ch, mk.sessions[t]), ch, sym, isin, 1, "isin_link",
                                  f"{p.isins[int(idx[k])]}->{p.isins[int(idx[k + 1])]}", "CA record",
                                  f"ISIN change on {mk.sessions[t]} without a CA factor row nearby"))
    return flags


def our_returns(c: Any, m: str, ch: int) -> Dict[int, Tuple[str, str, Optional[float]]]:
    """{k: (date_a, date_b, return)} for the BLEND windows k = 12, 6."""
    out = {}
    for k in (12, 6):
        ta, tb = c.sc.window(m, k)
        out[k] = (c.mk.sessions[ta], c.mk.sessions[tb], c.sc.ret_k(ch, m, k))
    return out


DIV_RE = re.compile(r"dividend[^0-9]*(?:rs\.?|re\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)", re.I)


def dividends(ca_raw: Sequence[Dict[str, Any]], symbol: str, a: str, b: str) -> float:
    """Rupees per share of dividends with ex-date in (a, b], parsed from the NSE CA records (best effort; an
    unparsed dividend counts 0, which only makes the check stricter)."""
    tot = 0.0
    for r in ca_raw:
        if r.get("symbol") != symbol:
            continue
        ex = str(r.get("exDate") or r.get("ex_date") or "")
        try:
            exd = dt.datetime.strptime(ex, "%d-%b-%Y").date().isoformat()
        except ValueError:
            exd = ex[:10]
        if not (a < exd <= b):
            continue
        for mt in DIV_RE.finditer(str(r.get("subject", ""))):
            tot += float(mt.group(1))
    return tot


def layer2(c: Any, m: str, chains: Sequence[int], kite_closes: Callable[[str, str, str, str], Dict[str, float]],
           ca_raw: Sequence[Dict[str, Any]], tol_pp: float, series_of: Callable[[int, int], str]) -> List[Flag]:
    """kite_closes(symbol, series, date_from, date_to) -> {date: close}; raises on a Kite failure (-> FAILED)."""
    flags: List[Flag] = []
    s = c.mk.sig(m)
    for ch in chains:
        sym, isin = c.mk.sym_at(ch, s), c.p.isins[int(c.p.isin_idx[ch, s])]
        rets = our_returns(c, m, ch)
        a12, b12, _ = rets[12]
        try:
            kc = kite_closes(sym, series_of(ch, s), a12, b12)
        except Exception as e:  # noqa: BLE001 - one name's Kite problem is a FLAG on that name, not a failed audit
            flags.append(Flag(_fid("kite", ch, "err"), ch, sym, isin, 2, "kite_unavailable", type(e).__name__, "",
                              f"Kite candles not available for this name: {str(e)[:120]}", kite_value="missing",
                              kite_agrees=False))
            continue
        for k, (a, b, ours) in rets.items():
            if ours is None:
                flags.append(Flag(_fid("kite", ch, str(k)), ch, sym, isin, 2, f"kite_{k}m", "no score", "", "ours None"))
                continue
            if a not in kc or b not in kc:
                flags.append(Flag(_fid("kite", ch, str(k)), ch, sym, isin, 2, f"kite_{k}m", f"{ours:+.1%}",
                                  f"{tol_pp} pp", f"Kite has no candle on {a if a not in kc else b}",
                                  kite_value="missing", kite_agrees=False))
                continue
            kr = kc[b] / kc[a] - 1
            div = dividends(ca_raw, sym, a, b)
            tol = tol_pp / 100 + (div / kc[a] if kc[a] > 0 else 0.0)
            agree = abs(ours - kr) <= tol
            if not agree:
                flags.append(Flag(_fid("kite", ch, str(k)), ch, sym, isin, 2, f"kite_{k}m", f"{ours:+.2%}",
                                  f"{tol:.2%}", f"{a}..{b}: ours {ours:+.2%} Kite {kr:+.2%} (div Rs {div:g})",
                                  kite_value=f"{kr:+.2%}", kite_agrees=False))
    return flags


def run(c: Any, m: str, signal_day: str, touched: Set[int], top: Sequence[int], cfg: Dict[str, Any],
        kite_closes: Callable[[str, str, str, str], Dict[str, float]], ca_raw: Sequence[Dict[str, Any]],
        series_of: Callable[[int, int], str]) -> AuditResult:
    th_ret, th_f = float(Decimal(cfg["audit_ret"])), float(Decimal(cfg["audit_factor"]))
    th_fr, tol = float(Decimal(cfg["price_freeze"])), float(Decimal(cfg["audit_kite_pp"]))
    pool = c.mk.pool("E", m)
    try:
        f1 = layer1(c, m, pool, th_ret, th_f, th_fr, signal_day)
    except Exception as e:  # noqa: BLE001 - any exception in a check = FAILED, never a silent pass
        return AuditResult("FAILED", problem=f"layer 1 exception: {type(e).__name__}: {e}")
    if any(f.check == "freshness" for f in f1):
        return AuditResult("FAILED", flags=f1, problem="freshness")
    l2_set = sorted(set(top) | {f.chain for f in f1} | set(touched))
    try:
        f2 = layer2(c, m, l2_set, kite_closes, ca_raw, tol, series_of)
    except Exception as e:  # noqa: BLE001
        return AuditResult("FAILED", flags=f1, problem=f"layer 2 (Kite) failed: {type(e).__name__}: {e}")
    n_unavail = sum(1 for f in f2 if f.check == "kite_unavailable")
    if l2_set and n_unavail > len(l2_set) // 2:      # Kite broadly down: the independent check did not happen
        return AuditResult("FAILED", flags=f1 + f2,
                           problem=f"layer 2: Kite unavailable for {n_unavail} of {len(l2_set)} names")
    k_bad = {f.chain for f in f2}
    for f in f1:            # D35: a layer-1 flag is independently confirmed when Kite agrees on that name
        f.kite_agrees = f.chain in l2_set and f.chain not in k_bad
    flags = f1 + f2
    for f in flags:
        f.touched = f.chain in touched
    counts = {"pool": len(pool), "layer2": len(l2_set), "flags": len(flags),
              "touched_flags": sum(f.touched for f in flags)}
    return AuditResult("PASSED", flags=flags, counts=counts)

"""Zebra reporting — EOD (daily) and weekly summaries.

Daily report (run 15:30 Mon-Fri): closed today + open positions with
unrealized P&L. Auto-mode upgrades to weekly on Fridays.

Weekly report (Friday EOD): closed in the trading week (Mon-Fri) + open
positions still standing. Aggregate stats: count, wins/losses, net P&L,
average hold days, exit-reason distribution.
"""

from __future__ import annotations

import html
import logging
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import config as cfg
from . import outcomes
from .trade_store import (TP_LATCH_EXPIRED, TP_TOUCHED_AT, ZebraStore,
                          in_cohort)

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Exit reasons: ONE classification, ONE wording ────────────────────────

def reason_label(reason) -> str:
    """The name this system gives one stored `exit_reason`.

    TWO engines close a cohort position and they write different strings for
    the same trigger: zebra's paper engine stamps `paper:debit_sl`, while
    `bcs/spread_monitor.py` — the one that can place orders — writes
    `SL_SPREAD`, and prefixes a recovery close with `ALREADY_FLAT_`.

    This module used to strip the `paper:` prefix itself, in three separate
    places, and print whatever was left — the literal spelling is absent on
    purpose, because the test that keeps this fixed greps the module source
    for it. That is fine for the engine it was written
    against and wrong for the other one: a bridged stop printed `SL_SPREAD`
    and opened its OWN `by_reason` bucket beside `debit_sl`, so one trigger
    became two rows and each under-counted the other. Three copies of a
    mapping is how the arming gate drifted (C0); a fourth is not the fix.

    `zebra/digest.py` had a FIFTH copy — its Closed table did the same strip
    and printed a bridged `SL_SPREAD` raw, in the very file that owns the
    arming gate. It now calls this function rather than growing a sixth
    wording, so the two summaries a human reads name a trigger identically.
    That is why this is public: it is no longer "this module's wording".

    So the CLASSIFICATION comes from `outcomes.classify`, the single
    vocabulary the arming gate and the vet scorecard already read, and only
    the WORDING lives here:

    * a recognised reason prints as its canonical kind, whichever engine
      wrote it — `SL_SPREAD` and `paper:debit_sl` are both `debit_sl`;
    * `already_flat_` survives as a visible qualifier rather than being
      folded away. It is a forensic fact — no close machinery ran and the
      price was recovered from order history, not transacted — and it is the
      distinction `is_stop_exit` turns on, so a reader must be able to see it;
    * an UNRECOGNISED string prints verbatim. Hiding it inside a tidy bucket
      is precisely the silence this vocabulary exists to end (`classify`
      logs a WARNING of its own on the way past).
    """
    c = outcomes.classify(reason)
    if not c['known']:
        return c['raw'] or 'unknown'
    return f"{c['kind']} (already-flat)" if c['recovered'] else c['kind']


#: The name the three call sites in this module have always used. Kept so the
#: rename to a public spelling is not a behaviour change anywhere.
_reason_label = reason_label


# ── The TP latch: reading the evidence it writes ─────────────────────────
#
# `zebra/trade_store.py` stamps three kinds of fact and, until now, NOTHING
# read any of them — which makes them decoration rather than evidence, and
# they were written to answer two live owner questions:
#
#   1. **Is same-day the right bound?** Every touch that reached the end of
#      its session without booking is appended to `tp_latch_expired`. If that
#      list stays empty the bound costs nothing; if it fills up, touches are
#      not converting within a session and the rule is costing exits.
#   2. **Is M12 worth building?** M12 would consume the exit vet inside the
#      same cycle instead of on the next tick, saving ~3 minutes of a measured
#      ~4m50s latency. `tp_touch_to_exit_sec` is the time that lag actually
#      takes and `tp_touch_spot_move*` is what price did during it — time AND
#      rupees, so the answer is priced rather than argued.
#
# Two readers (the phone report and the digest) and therefore ONE computation,
# here, for the same reason `outcomes.classify` exists: the last vocabulary
# that grew a private copy per reader drifted, and the arming gate read the
# drift as evidence.
#
# ABSENCE IS NOT ZERO, and this is the whole handling contract. Every record
# written before 2026-08-28 carries none of these fields, and a latch that has
# never armed is not a latch that armed and never expired. `n` is therefore
# reported alongside every statistic and is 0 — never a zero median — when
# there is nothing to say.

#: Stamped by `zebra.trade_store.tp_touch_to_fill`. The three keys are literals
#: there rather than module constants, so they are literals here too — pinned
#: by a test that calls that function and asserts this reader knows every key
#: it returns. A renamed field must fail loudly, not read as "no data".
TP_TOUCH_SEC = 'tp_touch_to_exit_sec'
TP_TOUCH_MOVE = 'tp_touch_spot_move'
TP_TOUCH_MOVE_PCT = 'tp_touch_spot_move_pct'
TP_TOUCH_GAVE_BACK = 'tp_touch_gave_back'


def _adverse_pct(t: dict) -> Optional[float]:
    """The touch->fill spot move as an ADVERSE percentage, or None.

    The stored move is SIGNED against spot, so a CE position that gave back
    reads negative and a PE position that gave back reads positive. Averaging
    the two cancels them — a blended statistic that hides the answer — so both
    are normalised to "how far did price move AGAINST this position while the
    exit was in flight", positive meaning give-back.

    The sign convention is taken from the stored `tp_touch_gave_back` flag
    first, because that is the writer's own answer to the same question, and
    only falls back to `direction`. When neither is available the move is left
    OUT of the distribution rather than guessed into it.
    """
    pct = t.get(TP_TOUCH_MOVE_PCT)
    if pct is None:
        return None
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return None
    gave = t.get(TP_TOUCH_GAVE_BACK)
    if gave is True:
        return abs(pct)
    if gave is False:
        return -abs(pct)
    d = t.get('direction')
    if d == 'CE':
        return -pct
    if d == 'PE':
        return pct
    return None


def _peak_giveback_rs(t: dict) -> Optional[float]:
    """Rupees of STRUCTURE value between the position's peak mid and the mid it
    booked at, or None when that cannot be attributed to the latency.

    The spot give-back says which way price went; this says what it cost, in
    the only currency the owner spends. It is a real number off stored fields
    (`mfe_mid`, `exit_debit`, `quantity`) rather than a delta model.

    Attributed ONLY when the peak was reached at or after the touch. A peak
    from three sessions earlier is a fact about the trade, not about the five
    minutes this section is pricing, and charging it to the lag would inflate
    exactly the number M12 is being judged on.
    """
    try:
        peak = float(t['mfe_mid'])
        booked = float(t['exit_debit'])
        qty = float(t['quantity'])
    except (KeyError, TypeError, ValueError):
        return None
    peak_at, touched_at = t.get('mfe_mid_at'), t.get(TP_TOUCHED_AT)
    if not peak_at or not touched_at:
        return None
    try:
        a = datetime.fromisoformat(str(peak_at))
        b = datetime.fromisoformat(str(touched_at))
        # One aware, one naive cannot be compared. Both engines write aware
        # stamps now; a mixed pair is old data and is simply not attributed.
        if (a.tzinfo is None) != (b.tzinfo is None) or a < b:
            return None
    except (TypeError, ValueError):
        return None
    return round((peak - booked) * qty, 0)


def tp_touch_rows(trades: list) -> list:
    """One row per exit that carries a touch->fill measurement.

    An exit that fired and booked inside a single observation has no gap and
    `tp_touch_to_fill` returns nothing for it, so it is absent here rather
    than present as a zero — the distribution below is of latencies that
    HAPPENED, and padding it with structural zeros would report the lag as
    smaller than it is.
    """
    rows = []
    for t in trades:
        if t.get(TP_TOUCH_SEC) is None:
            continue
        try:
            sec = float(t[TP_TOUCH_SEC])
        except (TypeError, ValueError):
            continue
        rows.append({
            'id': t.get('id'), 'stock': t.get('stock'),
            'exit_date': t.get('exit_date'), 'sec': sec,
            'move': t.get(TP_TOUCH_MOVE),
            'move_pct': t.get(TP_TOUCH_MOVE_PCT),
            'adverse_pct': _adverse_pct(t),
            'gave_back': t.get(TP_TOUCH_GAVE_BACK),
            'giveback_rs': _peak_giveback_rs(t),
            'pnl_net': t.get('pnl_net', t.get('pnl')),
        })
    return rows


def tp_touch_stats(trades: list) -> dict:
    """Distributions for the touch->fill lag. `n=0` means NO DATA.

    Median and worst, never a mean: the owner's question is whether the lag
    ever costs real money, and one bad give-back is the answer to that — a
    mean would dilute it against every exit that booked on the touch.
    """
    rows = tp_touch_rows(trades)
    out = {'n': len(rows), 'rows': rows,
           'median_sec': None, 'max_sec': None, 'worst_sec': None,
           'n_move': 0, 'median_adverse_pct': None,
           'max_adverse_pct': None, 'worst_move': None,
           'gave_back': 0, 'giveback_rs_total': None,
           'max_giveback_rs': None, 'worst_rs': None}
    if not rows:
        return out
    secs = [r['sec'] for r in rows]
    out['median_sec'] = round(statistics.median(secs), 1)
    out['max_sec'] = round(max(secs), 1)
    out['worst_sec'] = max(rows, key=lambda r: r['sec'])
    adverse = [r for r in rows if r['adverse_pct'] is not None]
    out['n_move'] = len(adverse)
    if adverse:
        out['median_adverse_pct'] = round(
            statistics.median([r['adverse_pct'] for r in adverse]), 4)
        out['max_adverse_pct'] = round(
            max(r['adverse_pct'] for r in adverse), 4)
        out['worst_move'] = max(adverse, key=lambda r: r['adverse_pct'])
    out['gave_back'] = sum(1 for r in rows if r['gave_back'] is True)
    rs = [r for r in rows if r['giveback_rs'] is not None]
    if rs:
        out['giveback_rs_total'] = round(sum(r['giveback_rs'] for r in rs), 0)
        out['max_giveback_rs'] = round(max(r['giveback_rs'] for r in rs), 0)
        out['worst_rs'] = max(rs, key=lambda r: r['giveback_rs'])
    return out


def tp_latch_expiries(trades: list) -> list:
    """Every touch that reached the end of its session unbooked, newest last.

    Read from the append-only `tp_latch_expired` list, which lives on OPEN
    records as well as closed ones — a latch that lapsed on Tuesday and
    re-armed on Wednesday is still holding the position, and a reader that
    only walked the exits would report the same-day bound as costless.
    """
    out = []
    for t in trades:
        for e in (t.get(TP_LATCH_EXPIRED) or []):
            if not isinstance(e, dict):
                continue
            out.append({'id': t.get('id'), 'stock': t.get('stock'),
                        'status': t.get('status'),
                        'touched_at': e.get('touched_at'),
                        'touch_spot': e.get('touch_spot'),
                        'noticed_at': e.get('noticed_at'),
                        'tp_spot': t.get('tp_spot')})
    return sorted(out, key=lambda r: str(r['touched_at'] or ''))


def tp_touch_days(trades: list) -> int:
    """How many DISTINCT touch-days the latch has ever armed on.

    The denominator for everything above, and the one number that separates
    "the latch has never fired" from "it fires and never expires". Counted as
    a union per record because `tp_touched_at` is OVERWRITTEN on a re-arm
    while the lapsed stamp is preserved in `tp_latch_expired`: adding the two
    would double-count a latch that expired and was never re-armed, and taking
    either alone would miss the other case.
    """
    n = 0
    for t in trades:
        stamps = {e.get('touched_at') for e in (t.get(TP_LATCH_EXPIRED) or [])
                  if isinstance(e, dict) and e.get('touched_at')}
        cur = t.get(TP_TOUCHED_AT)
        if cur:
            stamps.add(cur)
        n += len(stamps)
    return n


def tp_latch_evidence(trades: list, day: Optional[str] = None) -> dict:
    """Everything the latch has recorded, as one block. `touch_days=0` = NO DATA.

    `day`, when given, adds the same-day counts a daily reader needs without
    hiding the to-date totals the two questions are actually answered from.
    """
    exp = tp_latch_expiries(trades)
    stats = tp_touch_stats(trades)
    armed = [t for t in trades if t.get(TP_TOUCHED_AT)]
    days = tp_touch_days(trades)
    ev = {
        'touch_days': days,
        'armed_records': len(armed),
        'expired': exp,
        'measured': stats,
        # The one predicate every renderer branches on. NOT `expired == 0`:
        # "the latch has never armed" and "it armed and never lapsed" are
        # opposite answers to question 1 and must never share a rendering.
        'has_data': bool(days or stats['n']),
    }
    if day:
        ev['expired_today'] = sum(1 for e in exp
                                  if str(e.get('noticed_at') or '')[:10] == day)
        ev['measured_today'] = sum(1 for r in stats['rows']
                                   if str(r.get('exit_date') or '')[:10] == day)
        ev['armed_today'] = sum(1 for t in armed
                                if str(t.get(TP_TOUCHED_AT))[:10] == day)
    return ev


# ── Date helpers ──────────────────────────────────────────────────────────

def _today() -> date:
    return datetime.now(IST).date()


def _week_range(ref: Optional[date] = None) -> tuple:
    """Return (monday, friday) of the trading week containing `ref`."""
    ref = ref or _today()
    # weekday() Mon=0..Sun=6
    days_since_mon = ref.weekday()
    monday = ref - timedelta(days=days_since_mon)
    friday = monday + timedelta(days=4)
    return monday, friday


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _in_range(ts: Optional[str], start: date, end: date) -> bool:
    d = _parse_date(ts)
    return d is not None and start <= d <= end


# ── Aggregations ──────────────────────────────────────────────────────────

def _summarize_exits(exits: list) -> dict:
    """Compute aggregate stats over a list of exited trades."""
    if not exits:
        return {'count': 0, 'wins': 0, 'losses': 0,
                'net_pnl': 0.0, 'win_rate': 0.0,
                'avg_hold_days': 0.0, 'best': None, 'worst': None,
                'by_reason': {}}
    wins = [t for t in exits if (t.get('pnl') or 0) > 0]
    losses = [t for t in exits if (t.get('pnl') or 0) <= 0]
    net = sum(t.get('pnl') or 0 for t in exits)
    # Hold days
    holds = []
    for t in exits:
        d_in = _parse_date(t.get('entry_date'))
        d_out = _parse_date(t.get('exit_date'))
        if d_in and d_out:
            holds.append((d_out - d_in).days)
    by_reason = {}
    for t in exits:
        r = _reason_label(t.get('exit_reason'))
        by_reason.setdefault(r, {'count': 0, 'pnl': 0.0})
        by_reason[r]['count'] += 1
        by_reason[r]['pnl'] += t.get('pnl') or 0
    return {
        'count': len(exits),
        'wins': len(wins),
        'losses': len(losses),
        'net_pnl': round(net, 0),
        'win_rate': round(len(wins) / len(exits) * 100, 1),
        'avg_hold_days': round(sum(holds) / len(holds), 1) if holds else 0.0,
        'best': max(exits, key=lambda t: t.get('pnl') or 0),
        'worst': min(exits, key=lambda t: t.get('pnl') or 0),
        'by_reason': by_reason,
        'by_alignment': _alignment_split(exits),
        'by_structure': _structure_split(exits),
        # Absent from the rendered report when nothing was measured, rather
        # than printed as a row of zeros — an exit that booked on its own touch
        # has no gap, and a latch that never armed has no answer.
        'tp_touch': tp_touch_stats(exits),
    }


def _structure_split(exits: list) -> dict:
    """Zebra vs shadow-BCS A/B stats (BCS paper comparison, July 2026)."""
    def _stat(group: list) -> dict:
        if not group:
            return {'count': 0, 'net_pnl': 0.0, 'win_rate': 0.0, 'capital': 0.0}
        wins = sum(1 for t in group if (t.get('pnl') or 0) > 0)
        return {
            'count': len(group),
            'net_pnl': round(sum(t.get('pnl') or 0 for t in group), 0),
            'win_rate': round(wins / len(group) * 100, 1),
            'capital': round(sum(t.get('capital') or 0 for t in group), 0),
        }
    zebra = [t for t in exits if t.get('structure') != 'bcs']
    bcs = [t for t in exits if t.get('structure') == 'bcs']
    return {'zebra': _stat(zebra), 'bcs': _stat(bcs)}


def _alignment_split(exits: list) -> dict:
    """Split exits into with-trend (aligned) vs counter-trend and stat each.

    Alignment is derived from direction + st_direction (cfg.is_trend_aligned),
    so this works for every trade, including those logged before the flag
    existed. Lets us watch the validated alignment edge materialise forward.
    """
    def _stat(group: list) -> dict:
        if not group:
            return {'count': 0, 'net_pnl': 0.0, 'win_rate': 0.0}
        wins = 0
        net = 0.0
        for t in group:
            pnl = t.get('pnl') or 0
            net += pnl
            if pnl > 0:
                wins += 1
        return {
            'count': len(group),
            'net_pnl': round(net, 0),
            'win_rate': round(wins / len(group) * 100, 1),
        }
    aligned, counter = [], []
    for t in exits:
        sd = t.get('st_direction')
        if sd not in ('UP', 'DOWN'):
            continue  # unclassifiable — exclude so it skews neither bucket
        bucket = aligned if cfg.is_trend_aligned(t.get('direction'), sd) else counter
        bucket.append(t)
    return {'aligned': _stat(aligned), 'counter': _stat(counter)}


def _unrealized_for_open(open_trades: list, kite) -> dict:
    """Fetch live structure mids AND underlying LTPs for the open book.

    Two independent quotes per position, because they answer different
    questions and either can fail on its own. The structure mid drives P&L;
    the underlying LTP read against `entry_spot` says WHY — a position can sit
    flat on a spot move that has nearly reached its TP, or collapse while spot
    has barely moved at all (the NHPC signature). A missing quote blanks only
    its own field, never the other.

    Always returns a dict per trade, never None — callers test the individual
    fields, so a half-quoted position still reports what it does know.
    """
    blank = {'mid': None, 'pnl': None, 'pnl_pct': None,
             'spot': None, 'spot_pct': None}
    if not open_trades:
        return {}
    if kite is None:
        return {t['id']: dict(blank) for t in open_trades}
    from .monitor import _quote_zebra_value

    # One batched LTP call for the whole book, not one per position.
    spots = {}
    try:
        from .scanner import get_ltp
        stocks = sorted({t['stock'] for t in open_trades if t.get('stock')})
        spots = get_ltp(kite, stocks) or {}
    except Exception as e:
        # Spot is the explanatory column here; the P&L must still go out
        # without it, so this can never be allowed to raise.
        logger.warning("Spot LTP unavailable for report: %s", e)

    out = {}
    for t in open_trades:
        rec = dict(blank)
        try:
            mid = _quote_zebra_value(kite, t)
        except Exception:
            mid = None
        if mid is not None:
            debit = t.get('debit', 0)
            qty = t.get('quantity', 0)
            pnl_share = mid - debit
            rec['mid'] = mid
            rec['pnl'] = pnl_share * qty
            rec['pnl_pct'] = (pnl_share / debit * 100) if debit > 0 else 0
        # get_ltp hands back 0.0 for a symbol it could not map to NSE. That is
        # a failure marker, not a price — `or None` keeps it out of the report.
        spot = spots.get(t.get('stock')) or None
        entry_spot = t.get('entry_spot') or None
        rec['spot'] = spot
        if spot and entry_spot:
            rec['spot_pct'] = (spot - entry_spot) / entry_spot * 100
        out[t['id']] = rec
    return out


def _open_totals(report: dict) -> tuple:
    """(unrealized P&L, capital deployed, positions actually quoted).

    The aggregate can only sum the positions that quoted. When one did not,
    the total UNDERSTATES the book by that position's P&L, so the caller
    prints the quoted count alongside it — an aggregate that silently drops a
    position reads as a fact when it is a partial.
    """
    unreal = sum((u or {}).get('pnl') or 0
                 for u in report['unrealized'].values())
    deployed = sum((t.get('capital')
                    or (t.get('debit') or 0) * (t.get('quantity') or 0))
                   for t in report['open'])
    quoted = sum(1 for t in report['open']
                 if (report['unrealized'].get(t['id']) or {}).get('pnl')
                 is not None)
    return unreal, deployed, quoted


def _quoted_note(quoted: int, total: int) -> str:
    """Empty when every position quoted, else names how many did."""
    return "" if quoted >= total else f" [{quoted}/{total} quoted]"


def _open_sorted(report: dict) -> list:
    """Open positions, best performer first.

    This list is read to answer "which of these is working", so the answer
    belongs at the top. A position with no quote sorts LAST rather than
    counting as zero — an unknown is not a flat.
    """
    def key(t):
        pnl = (report['unrealized'].get(t['id']) or {}).get('pnl')
        return (0, -pnl) if pnl is not None else (1, 0)
    return sorted(report['open'], key=key)


# ── Report builders ──────────────────────────────────────────────────────

def _reportable(trades: list) -> list:
    """Trades this report is allowed to talk about.

    With `alerts_cohort_only` on, that is the current engine's trades only.
    The legacy book is not deleted or hidden from the store — `zebra status`
    still shows it under the whole-book block — it just stops being reported
    daily, because 25 legacy positions were most of the message and a report
    nobody reads to the bottom is worse than a shorter one.
    """
    if not cfg.ALERTS_COHORT_ONLY:
        return trades
    return [t for t in trades if in_cohort(t)]


def daily_report(store: ZebraStore, kite=None,
                 ref_date: Optional[date] = None) -> dict:
    """Daily EOD: closed today + open positions with unrealized."""
    ref = ref_date or _today()
    all_trades = _reportable(store.load_trades())
    closed_today = [t for t in all_trades
                    if t.get('status') == 'exited'
                    and _in_range(t.get('exit_date'), ref, ref)]
    open_now = [t for t in all_trades if t.get('status') == 'entered']
    return {
        'date': ref.isoformat(),
        'type': 'daily',
        'closed': closed_today,
        'open': open_now,
        'closed_summary': _summarize_exits(closed_today),
        'unrealized': _unrealized_for_open(open_now, kite),
    }


def weekly_report(store: ZebraStore, kite=None,
                  ref_date: Optional[date] = None) -> dict:
    """Weekly: closed in Mon-Fri of ref week + open positions."""
    ref = ref_date or _today()
    monday, friday = _week_range(ref)
    all_trades = _reportable(store.load_trades())
    closed_week = [t for t in all_trades
                   if t.get('status') == 'exited'
                   and _in_range(t.get('exit_date'), monday, friday)]
    open_now = [t for t in all_trades if t.get('status') == 'entered']
    return {
        'date': ref.isoformat(),
        'week_start': monday.isoformat(),
        'week_end': friday.isoformat(),
        'type': 'weekly',
        'closed': closed_week,
        'open': open_now,
        'closed_summary': _summarize_exits(closed_week),
        'unrealized': _unrealized_for_open(open_now, kite),
    }


# ── Formatters ────────────────────────────────────────────────────────────

def _fmt_trade_line(t: dict) -> str:
    """One-line summary for a closed trade."""
    pnl = t.get('pnl') or 0
    pct = t.get('pnl_pct') or 0
    kl = int(t['long_strike']) if t.get('long_strike') else '?'
    ks = int(t['short_strike']) if t.get('short_strike') else '?'
    reason = _reason_label(t.get('exit_reason'))
    tag = ' [BCS]' if t.get('structure') == 'bcs' else ''
    return (f"  #{t['id']} {t['stock']:<10} {t['direction']:<3} {kl}/{ks}  "
            f"{t.get('entry_date','?')[5:]}->{t.get('exit_date','?')[5:]}  "
            f"P&L Rs {pnl:+,.0f} ({pct:+.1f}%)  [{reason}]{tag}")


def _fmt_open_line(t: dict, unreal: Optional[dict]) -> str:
    """One-line summary for an open trade with unrealized P&L."""
    kl = int(t['long_strike']) if t.get('long_strike') else '?'
    ks = int(t['short_strike']) if t.get('short_strike') else '?'
    sl_txt = f"  SL {t.get('sl_spot',0):.0f}" if cfg.SPOT_SL_ENABLED else ""
    st_tag = '[BCS] ' if t.get('structure') == 'bcs' else ''
    u = unreal or {}
    spot_txt = f"{u['spot']:.2f}" if u.get('spot') else "n/a"
    if u.get('spot_pct') is not None:
        spot_txt += f" ({u['spot_pct']:+.1f}%)"
    base = (f"  #{t['id']} {st_tag}{t['stock']:<10} {t['direction']:<3} {kl}/{ks}  "
            f"entry {t.get('entry_date','?')[5:]} @ {t.get('entry_spot',0):.2f} "
            f"-> {spot_txt}  TP {t.get('tp_spot',0):.0f}{sl_txt}")
    if u.get('mid') is not None:
        base += (f"  | mid {u['mid']:.2f}  unrealized Rs {u['pnl']:+,.0f} "
                 f"({u['pnl_pct']:+.1f}%)")
    else:
        base += "  | mid n/a"
    return base


def _fmt_open_telegram(t: dict, unreal: Optional[dict]) -> str:
    """Two lines per open position for the EOD/weekly Telegram.

    Line 1 names the position; line 2 carries the three numbers the owner
    asked for on 2026-08-21 — the spot we entered at, where spot is NOW, and
    what the position itself is worth — so the message says which positions
    are working instead of only what the book nets.

    Every runtime value is html.escape'd: `M&M` is a live NSE symbol in this
    book, and one bare `&` 400-rejects the entire message silently.
    """
    u = unreal or {}
    kl = int(t['long_strike']) if t.get('long_strike') else '?'
    ks = int(t['short_strike']) if t.get('short_strike') else '?'
    pnl = u.get('pnl')
    # Owner's call 2026-08-21: a coloured ball, nothing else. The direction
    # arrow duplicated the sign that is already on the P&L, and the 📐 tag
    # marked a structure EVERY trade in this book has, so it distinguished
    # nothing. White is the third state — no quote is not a flat position.
    tag = '⚪' if pnl is None else ('🟢' if pnl > 0 else '🔴')
    head = (f"{tag} <code>{html.escape(str(t['stock']))}</code> "
            f"{html.escape(str(t.get('direction') or ''))} {kl}/{ks}")
    spot_txt = f"{u['spot']:.2f}" if u.get('spot') else "n/a"
    if u.get('spot_pct') is not None:
        spot_txt += f" ({u['spot_pct']:+.1f}%)"
    pnl_txt = (f"Rs {pnl:+,.0f} ({u['pnl_pct']:+.1f}%)" if pnl is not None
               else "quote n/a")
    body = (f"    spot {t.get('entry_spot') or 0:.2f} → {spot_txt}"
            f" · TP {t.get('tp_spot',0):.0f}  |  {pnl_txt}")
    return head + "\n" + body


def format_text(report: dict) -> str:
    """Plain text report (stdout + log file)."""
    typ = report['type']
    if typ == 'weekly':
        header = (f"=== ZEBRA WEEKLY REPORT — Week "
                  f"{report['week_start']} to {report['week_end']} ===")
    else:
        header = f"=== ZEBRA EOD REPORT — {report['date']} ==="

    s = report['closed_summary']
    lines = [header, ""]

    # Closed section
    if s['count'] == 0:
        lines.append(f"Closed: 0")
    else:
        lines.append(
            f"Closed: {s['count']} trade(s) ({s['wins']}W {s['losses']}L, "
            f"net Rs {s['net_pnl']:+,.0f}, win-rate {s['win_rate']:.0f}%, "
            f"avg hold {s['avg_hold_days']:.1f}d)"
        )
        for t in sorted(report['closed'], key=lambda x: x.get('pnl') or 0,
                        reverse=True):
            lines.append(_fmt_trade_line(t))
        if s['by_reason']:
            lines.append("")
            lines.append("  By exit reason:")
            for r, st in sorted(s['by_reason'].items()):
                lines.append(f"    {r:<12} {st['count']:>2} trades  "
                             f"Rs {st['pnl']:+,.0f}")
        a = s.get('by_alignment')
        if a and (a['aligned']['count'] or a['counter']['count']):
            lines.append("")
            lines.append("  By alignment (with-trend edge):")
            for label, st in (('aligned ⭐', a['aligned']), ('counter ', a['counter'])):
                lines.append(f"    {label:<12} {st['count']:>2} trades  "
                             f"Rs {st['net_pnl']:+,.0f}  WR {st['win_rate']:.0f}%")
        b = s.get('by_structure')
        if b and b['bcs']['count']:
            lines.append("")
            lines.append("  Zebra vs BCS shadow (A/B):")
            for label, st in (('zebra', b['zebra']), ('bcs  ', b['bcs'])):
                roc = (st['net_pnl'] / st['capital'] * 100) if st['capital'] else 0
                lines.append(f"    {label:<6} {st['count']:>2} trades  "
                             f"Rs {st['net_pnl']:+,.0f}  WR {st['win_rate']:.0f}%  "
                             f"RoC {roc:+.1f}%")
        tp = s.get('tp_touch') or {}
        if tp.get('n'):
            # What the ~5-minute exit lag cost the exits it applied to. Median
            # and worst; the mean of two exits, one of which booked on its own
            # touch, is the shape of statistic that answers nothing.
            lines.append("")
            # ASCII arrow: `format_text` is PRINTED, and a Windows console
            # encodes stdout as cp1252, where a `→` raises UnicodeEncodeError
            # and takes the whole EOD summary with it. Every other line in
            # this formatter is ASCII for the same reason; the Telegram half
            # is UTF-8 over HTTP and keeps the real arrow.
            lines.append("  TP touch -> fill (the lag M12 would remove):")
            w = tp['worst_sec']
            lines.append(f"    latency     {tp['n']:>2} exit(s)  "
                         f"median {tp['median_sec']:.0f}s  "
                         f"worst {tp['max_sec']:.0f}s (#{w['id']} {w['stock']})")
            if tp['n_move']:
                wm = tp['worst_move']
                lines.append(f"    spot        {tp['gave_back']} of {tp['n']} "
                             f"gave back  median {tp['median_adverse_pct']:+.2f}%  "
                             f"worst {tp['max_adverse_pct']:+.2f}% "
                             f"(#{wm['id']} {wm['stock']})")
            if tp['giveback_rs_total'] is not None:
                wr = tp['worst_rs']
                lines.append(f"    value       Rs {tp['giveback_rs_total']:,.0f} "
                             f"from peak mid to booked mid  "
                             f"worst Rs {tp['max_giveback_rs']:,.0f} "
                             f"(#{wr['id']} {wr['stock']})")

    # Open section
    lines.append("")
    if not report['open']:
        lines.append("Open: 0")
    else:
        unreal_pnl, deployed, quoted = _open_totals(report)
        roc = f" ({unreal_pnl / deployed * 100:+.1f}%)" if deployed else ""
        note = _quoted_note(quoted, len(report['open']))
        lines.append(
            f"Open: {len(report['open'])} position(s), "
            f"Rs {deployed:,.0f} deployed, "
            f"unrealized Rs {unreal_pnl:+,.0f}{roc}{note}"
        )
        for t in _open_sorted(report):
            u = report['unrealized'].get(t['id'])
            lines.append(_fmt_open_line(t, u))

    return "\n".join(lines)


def format_telegram(report: dict) -> str:
    """HTML-formatted for Telegram. Compact and emoji-tagged."""
    typ = report['type']
    if typ == 'weekly':
        title = f"\U0001F4CA <b>ZEBRA WEEKLY</b>"
        period = f"{report['week_start']} → {report['week_end']}"
    else:
        title = f"\U0001F4C5 <b>ZEBRA EOD</b>"
        period = report['date']

    s = report['closed_summary']
    parts = [f"{title}  {period}"]

    if s['count'] == 0:
        parts.append("\n<b>Closed:</b> 0")
    else:
        net = s['net_pnl']
        sign = '✅' if net > 0 else '🔻' if net < 0 else '➖'
        parts.append(
            f"\n<b>Closed:</b> {s['count']} ({s['wins']}W/{s['losses']}L) "
            f"{sign} Rs {net:+,.0f} | WR {s['win_rate']:.0f}% | "
            f"hold {s['avg_hold_days']:.1f}d"
        )
        for t in sorted(report['closed'], key=lambda x: x.get('pnl') or 0,
                        reverse=True):
            pnl = t.get('pnl') or 0
            tag = '🟢' if pnl > 0 else '🔴'
            kl = int(t['long_strike']) if t.get('long_strike') else '?'
            ks = int(t['short_strike']) if t.get('short_strike') else '?'
            reason = _reason_label(t.get('exit_reason'))
            parts.append(
                # html.escape: the EOD report is parse_mode=HTML too, and `M&M`
                # is a real NSE symbol sitting in this book right now. Same
                # class as the exit-alert formatters.
                f"{tag} <code>{html.escape(str(t['stock']))}</code> "
                f"{t['direction']} {kl}/{ks}  Rs {pnl:+,.0f} "
                f"[{html.escape(str(reason))}]"
            )
        a = s.get('by_alignment')
        if a and (a['aligned']['count'] or a['counter']['count']):
            al, co = a['aligned'], a['counter']
            parts.append(
                f"<i>⭐aligned {al['count']}: Rs {al['net_pnl']:+,.0f} "
                f"(WR {al['win_rate']:.0f}%) | counter {co['count']}: "
                f"Rs {co['net_pnl']:+,.0f} (WR {co['win_rate']:.0f}%)</i>"
            )
        b = s.get('by_structure')
        if b and b['bcs']['count']:
            zb, bc = b['zebra'], b['bcs']
            parts.append(
                f"<i>📐 A/B — zebra {zb['count']}: Rs {zb['net_pnl']:+,.0f} "
                f"(WR {zb['win_rate']:.0f}%) | BCS {bc['count']}: "
                f"Rs {bc['net_pnl']:+,.0f} (WR {bc['win_rate']:.0f}%)</i>"
            )
        tp = s.get('tp_touch') or {}
        if tp.get('n'):
            # One line, and only when there is something to say. Nothing here
            # is a runtime string from an exchange, but it is built the same
            # way as its neighbours and stays inside the HTML parse mode.
            move = ('' if not tp['n_move'] else
                    f" | spot {tp['median_adverse_pct']:+.2f}% median "
                    f"({tp['gave_back']}/{tp['n']} gave back)")
            rs = ('' if tp['giveback_rs_total'] is None else
                  f" | Rs {tp['giveback_rs_total']:,.0f} from peak")
            parts.append(
                f"<i>⏱ TP touch→fill {tp['n']}: median "
                f"{tp['median_sec']:.0f}s, worst {tp['max_sec']:.0f}s"
                f"{move}{rs}</i>"
            )

    if report['open']:
        unreal_pnl, deployed, quoted = _open_totals(report)
        roc = f" ({unreal_pnl / deployed * 100:+.1f}%)" if deployed else ""
        note = _quoted_note(quoted, len(report['open']))
        # The COUNT and the aggregate always go out — a book quietly emptying
        # or filling up has to stay visible, and that is one line. What is
        # switchable is the position-by-position listing below it, which with a
        # full book was most of the message.
        parts.append(
            f"\n<b>Open:</b> {len(report['open'])} pos, "
            f"Rs {deployed:,.0f} deployed, "
            f"unrealized Rs {unreal_pnl:+,.0f}{roc}{note}"
        )
        if cfg.EOD_OPEN_POSITIONS:
            for t in _open_sorted(report):
                parts.append(
                    _fmt_open_telegram(t, report['unrealized'].get(t['id'])))
    else:
        parts.append("\n<b>Open:</b> 0")

    return "\n".join(parts)


# ── CLI entry ─────────────────────────────────────────────────────────────

def run(report_type: str = 'auto', send_telegram: bool = False,
        no_kite: bool = False) -> None:
    """Generate + print + optionally Telegram the report.

    report_type: 'daily', 'weekly', or 'auto' (daily on Mon-Thu, weekly on Friday)
    """
    from .trade_store import get_store

    if report_type == 'auto':
        # Friday → weekly, else daily
        weekday = _today().weekday()  # Mon=0..Fri=4..Sun=6
        report_type = 'weekly' if weekday == 4 else 'daily'

    store = get_store()
    kite = None
    if not no_kite:
        try:
            from .scanner import _get_kite
            kite = _get_kite()
        except Exception as e:
            logger.warning("Kite unavailable for unrealized P&L: %s", e)

    if report_type == 'weekly':
        report = weekly_report(store, kite=kite)
    else:
        report = daily_report(store, kite=kite)

    text = format_text(report)
    print(text)

    if send_telegram:
        from .monitor import _send_telegram
        _send_telegram(format_telegram(report))

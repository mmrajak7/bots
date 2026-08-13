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
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import config as cfg
from .trade_store import ZebraStore, in_cohort

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


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
        r = (t.get('exit_reason') or 'unknown').replace('paper:', '')
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
    """Optional: fetch live mids for open trades, compute unrealized P&L."""
    if not open_trades or kite is None:
        return {t['id']: None for t in open_trades}
    from .monitor import _quote_zebra_value
    out = {}
    for t in open_trades:
        try:
            mid = _quote_zebra_value(kite, t)
        except Exception:
            mid = None
        if mid is None:
            out[t['id']] = None
        else:
            debit = t.get('debit', 0)
            qty = t.get('quantity', 0)
            pnl_share = mid - debit
            pnl = pnl_share * qty
            pct = (pnl_share / debit * 100) if debit > 0 else 0
            out[t['id']] = {'mid': mid, 'pnl': pnl, 'pnl_pct': pct}
    return out


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
    reason = (t.get('exit_reason') or '').replace('paper:', '')
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
    base = (f"  #{t['id']} {st_tag}{t['stock']:<10} {t['direction']:<3} {kl}/{ks}  "
            f"entry {t.get('entry_date','?')[5:]} @ {t.get('entry_spot',0):.2f}  "
            f"TP {t.get('tp_spot',0):.0f}{sl_txt}")
    if unreal:
        base += f"  | mid {unreal['mid']:.2f}  unrealized Rs {unreal['pnl']:+,.0f} ({unreal['pnl_pct']:+.1f}%)"
    else:
        base += "  | mid n/a"
    return base


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

    # Open section
    lines.append("")
    if not report['open']:
        lines.append("Open: 0")
    else:
        unreal_pnl = sum(
            (u.get('pnl') or 0)
            for u in report['unrealized'].values() if u
        )
        lines.append(
            f"Open: {len(report['open'])} position(s), "
            f"unrealized Rs {unreal_pnl:+,.0f}"
        )
        for t in report['open']:
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
            tag = '✓' if pnl > 0 else '✗'
            kl = int(t['long_strike']) if t.get('long_strike') else '?'
            ks = int(t['short_strike']) if t.get('short_strike') else '?'
            reason = (t.get('exit_reason') or '').replace('paper:', '')
            st_tag = '📐' if t.get('structure') == 'bcs' else ''
            parts.append(
                # html.escape: the EOD report is parse_mode=HTML too, and `M&M`
                # is a real NSE symbol sitting in this book right now. Same
                # class as the exit-alert formatters.
                f"{tag}{st_tag} <code>{html.escape(str(t['stock']))}</code> "
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

    if report['open']:
        unreal_pnl = sum(
            (u.get('pnl') or 0)
            for u in report['unrealized'].values() if u
        )
        # The COUNT and the aggregate always go out — a book quietly emptying
        # or filling up has to stay visible, and that is one line. What is
        # switchable is the position-by-position listing below it, which with a
        # full book was most of the message.
        parts.append(
            f"\n<b>Open:</b> {len(report['open'])} pos, "
            f"unrealized Rs {unreal_pnl:+,.0f}"
        )
        if cfg.EOD_OPEN_POSITIONS:
            for t in report['open']:
                u = report['unrealized'].get(t['id'])
                kl = int(t['long_strike']) if t.get('long_strike') else '?'
                ks = int(t['short_strike']) if t.get('short_strike') else '?'
                st_tag = '📐' if t.get('structure') == 'bcs' else ''
                if u:
                    pnl = u['pnl']
                    tag = '↑' if pnl > 0 else '↓'
                    parts.append(
                        f"{tag}{st_tag} <code>{html.escape(str(t['stock']))}</code> "
                        f"{t['direction']} {kl}/{ks}  Rs {pnl:+,.0f} "
                        f"({u['pnl_pct']:+.0f}%)"
                    )
                else:
                    parts.append(
                        f"•{st_tag} <code>{html.escape(str(t['stock']))}</code> "
                        f"{t['direction']} {kl}/{ks}  (quote n/a)"
                    )
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

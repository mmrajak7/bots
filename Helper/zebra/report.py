"""Zebra reporting — EOD (daily) and weekly summaries.

Daily report (run 15:30 Mon-Fri): closed today + open positions with
unrealized P&L. Auto-mode upgrades to weekly on Fridays.

Weekly report (Friday EOD): closed in the trading week (Mon-Fri) + open
positions still standing. Aggregate stats: count, wins/losses, net P&L,
average hold days, exit-reason distribution.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import config as cfg
from .trade_store import ZebraStore

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
    }


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

def daily_report(store: ZebraStore, kite=None,
                 ref_date: Optional[date] = None) -> dict:
    """Daily EOD: closed today + open positions with unrealized."""
    ref = ref_date or _today()
    all_trades = store.load_trades()
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
    all_trades = store.load_trades()
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
    return (f"  #{t['id']} {t['stock']:<10} {t['direction']:<3} {kl}/{ks}  "
            f"{t.get('entry_date','?')[5:]}->{t.get('exit_date','?')[5:]}  "
            f"P&L Rs {pnl:+,.0f} ({pct:+.1f}%)  [{reason}]")


def _fmt_open_line(t: dict, unreal: Optional[dict]) -> str:
    """One-line summary for an open trade with unrealized P&L."""
    kl = int(t['long_strike']) if t.get('long_strike') else '?'
    ks = int(t['short_strike']) if t.get('short_strike') else '?'
    base = (f"  #{t['id']} {t['stock']:<10} {t['direction']:<3} {kl}/{ks}  "
            f"entry {t.get('entry_date','?')[5:]} @ {t.get('entry_spot',0):.2f}  "
            f"TP {t.get('tp_spot',0):.0f}  SL {t.get('sl_spot',0):.0f}")
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
            parts.append(
                f"{tag} <code>{t['stock']}</code> {t['direction']} "
                f"{kl}/{ks}  Rs {pnl:+,.0f} [{reason}]"
            )

    if report['open']:
        unreal_pnl = sum(
            (u.get('pnl') or 0)
            for u in report['unrealized'].values() if u
        )
        parts.append(
            f"\n<b>Open:</b> {len(report['open'])} pos, "
            f"unrealized Rs {unreal_pnl:+,.0f}"
        )
        for t in report['open']:
            u = report['unrealized'].get(t['id'])
            kl = int(t['long_strike']) if t.get('long_strike') else '?'
            ks = int(t['short_strike']) if t.get('short_strike') else '?'
            if u:
                pnl = u['pnl']
                tag = '↑' if pnl > 0 else '↓'
                parts.append(
                    f"{tag} <code>{t['stock']}</code> {t['direction']} "
                    f"{kl}/{ks}  Rs {pnl:+,.0f} ({u['pnl_pct']:+.0f}%)"
                )
            else:
                parts.append(
                    f"• <code>{t['stock']}</code> {t['direction']} {kl}/{ks}  "
                    f"(quote n/a)"
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

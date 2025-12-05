"""Report Formatter - Compact, Colored, Tabular Report Utilities

Provides consistent formatting for all Telegram reports:
- Same Day Recovery
- Daily Reconciliation
- Daily Summary

Design principles:
- Compact single-line summaries where possible
- Color emojis: Green for profit/good, Red for loss/bad, Yellow for warning
- Clean section separators
- Monospace-friendly formatting for alignment
"""

from typing import List, Dict, Optional, Union
from datetime import date, datetime


class ReportFormatter:
    """Utility class for formatting Telegram reports"""

    # Status indicators
    PROFIT = "+"
    LOSS = "-"

    # Emojis for visual indicators
    GREEN_CIRCLE = "🟢"
    RED_CIRCLE = "🔴"
    YELLOW_CIRCLE = "🟡"
    CHECK = "✓"
    CROSS = "✗"
    WARNING = "⚠"

    # Section dividers (compact)
    THIN_LINE = "─" * 28

    @staticmethod
    def format_currency(amount: float, show_sign: bool = False, compact: bool = True) -> str:
        """Format currency with optional sign and compact notation"""
        if compact and abs(amount) >= 100000:
            # Use lakhs for large amounts
            lakhs = amount / 100000
            if show_sign and amount >= 0:
                return f"+Rs.{lakhs:.2f}L"
            elif amount < 0:
                return f"-Rs.{abs(lakhs):.2f}L"
            return f"Rs.{lakhs:.2f}L"
        elif compact and abs(amount) >= 1000:
            # Use K for thousands
            k = amount / 1000
            if show_sign and amount >= 0:
                return f"+Rs.{k:.1f}K"
            elif amount < 0:
                return f"-Rs.{abs(k):.1f}K"
            return f"Rs.{k:.1f}K"
        else:
            if show_sign and amount >= 0:
                return f"+Rs.{amount:,.0f}"
            elif amount < 0:
                return f"-Rs.{abs(amount):,.0f}"
            return f"Rs.{amount:,.0f}"

    @staticmethod
    def pnl_emoji(amount: float) -> str:
        """Return appropriate emoji for P&L"""
        if amount > 0:
            return ReportFormatter.GREEN_CIRCLE
        elif amount < 0:
            return ReportFormatter.RED_CIRCLE
        return ReportFormatter.YELLOW_CIRCLE

    @staticmethod
    def pnl_with_emoji(amount: float, percent: Optional[float] = None, compact: bool = True) -> str:
        """Format P&L with emoji and optional percentage"""
        emoji = ReportFormatter.pnl_emoji(amount)
        amt_str = ReportFormatter.format_currency(amount, show_sign=True, compact=compact)
        if percent is not None:
            return f"{emoji} {amt_str} ({percent:+.1f}%)"
        return f"{emoji} {amt_str}"

    @staticmethod
    def status_indicator(is_good: bool, neutral: bool = False) -> str:
        """Return status indicator"""
        if neutral:
            return ReportFormatter.YELLOW_CIRCLE
        return ReportFormatter.GREEN_CIRCLE if is_good else ReportFormatter.RED_CIRCLE

    @staticmethod
    def format_header(title: str, emoji: str = "") -> str:
        """Format section header"""
        if emoji:
            return f"{emoji} *{title}*"
        return f"*{title}*"

    @staticmethod
    def format_metric_row(label: str, value: str, indent: int = 0) -> str:
        """Format a single metric row"""
        prefix = "  " * indent
        return f"{prefix}{label}: {value}"

    @staticmethod
    def format_position_row(
        script: str,
        timeframe: str,
        entry: float,
        qty: int,
        ltp: float,
        sl: float,
        days: int,
        pnl: float
    ) -> str:
        """Format a position in compact single-line format"""
        emoji = ReportFormatter.pnl_emoji(pnl)
        pnl_str = ReportFormatter.format_currency(pnl, show_sign=True)
        return f"{emoji} {script}({timeframe}) {qty}@{entry:.0f} LTP:{ltp:.0f} SL:{sl:.0f} {pnl_str}"

    @staticmethod
    def format_exit_row(
        script: str,
        timeframe: str,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        days: int
    ) -> str:
        """Format an exit in compact single-line format"""
        emoji = ReportFormatter.pnl_emoji(pnl)
        pnl_str = ReportFormatter.format_currency(pnl, show_sign=True)
        return f"{emoji} {script}({timeframe}) @{exit_price:.0f} {pnl_str} ({pnl_pct:+.1f}%) {days}d"

    @staticmethod
    def format_summary_box(items: List[Dict[str, str]]) -> str:
        """
        Format a compact summary box with key metrics

        Args:
            items: List of dicts with 'label' and 'value' keys

        Example output:
        Capital: Rs.5.2L | Deployed: Rs.2.1L | Free: Rs.3.1L
        """
        parts = [f"{item['label']}: {item['value']}" for item in items]
        return " | ".join(parts)

    @staticmethod
    def format_stats_line(
        wins: int,
        losses: int,
        total_pnl: float,
        win_rate: Optional[float] = None
    ) -> str:
        """Format win/loss stats in single line"""
        emoji = ReportFormatter.pnl_emoji(total_pnl)
        pnl_str = ReportFormatter.format_currency(total_pnl, show_sign=True)
        wr = f" WR:{win_rate:.0f}%" if win_rate is not None else ""
        return f"{emoji} W:{wins} L:{losses}{wr} Net:{pnl_str}"

    @staticmethod
    def format_recovery_summary(
        checks: int,
        issues: int,
        critical: int,
        warnings: int,
        fixes: int
    ) -> str:
        """Format recovery summary in compact format"""
        status_emoji = ReportFormatter.GREEN_CIRCLE if critical == 0 else ReportFormatter.RED_CIRCLE

        parts = [f"Checks:{checks}"]
        if issues > 0:
            parts.append(f"Issues:{issues}")
        if critical > 0:
            parts.append(f"Critical:{critical}")
        if warnings > 0:
            parts.append(f"Warnings:{warnings}")
        if fixes > 0:
            parts.append(f"Fixes:{fixes}")

        return f"{status_emoji} {' | '.join(parts)}"

    @staticmethod
    def format_date_header(report_date: date, title: str) -> str:
        """Format date header for reports"""
        date_str = report_date.strftime('%d %b %Y')
        return f"*{title}*\n{date_str}"

    @staticmethod
    def format_capital_summary(
        total: float,
        deployed: float,
        free: float,
        show_pct: bool = True
    ) -> str:
        """Format capital summary in compact format"""
        deployed_pct = (deployed / total * 100) if total > 0 else 0

        total_str = ReportFormatter.format_currency(total)
        deployed_str = ReportFormatter.format_currency(deployed)
        free_str = ReportFormatter.format_currency(free)

        if show_pct:
            return f"Total:{total_str} | Deployed:{deployed_str}({deployed_pct:.0f}%) | Free:{free_str}"
        return f"Total:{total_str} | Deployed:{deployed_str} | Free:{free_str}"

    @staticmethod
    def format_positions_compact(positions: List[Dict], max_show: int = 5) -> str:
        """
        Format positions list in compact format

        Args:
            positions: List of position dicts with keys: script, tf, entry, qty, ltp, sl, pnl, days
            max_show: Maximum positions to show (rest summarized)
        """
        if not positions:
            return "No open positions"

        lines = []

        # Sort by P&L (worst first for attention)
        sorted_pos = sorted(positions, key=lambda x: x.get('pnl', 0))

        for i, pos in enumerate(sorted_pos[:max_show]):
            emoji = ReportFormatter.pnl_emoji(pos.get('pnl', 0))
            pnl_str = ReportFormatter.format_currency(pos.get('pnl', 0), show_sign=True)

            line = (
                f"{emoji} {pos['script']}({pos['tf']}) "
                f"{pos['qty']}@{pos['entry']:.0f} "
                f"LTP:{pos.get('ltp', pos['entry']):.0f} "
                f"SL:{pos['sl']:.0f} "
                f"{pnl_str}"
            )
            lines.append(line)

        if len(positions) > max_show:
            remaining = len(positions) - max_show
            remaining_pnl = sum(p.get('pnl', 0) for p in sorted_pos[max_show:])
            pnl_str = ReportFormatter.format_currency(remaining_pnl, show_sign=True)
            lines.append(f"   +{remaining} more ({pnl_str})")

        return "\n".join(lines)

    @staticmethod
    def format_exits_compact(exits: List[Dict], max_show: int = 5) -> str:
        """Format exits list in compact format"""
        if not exits:
            return "No exits today"

        lines = []

        for i, exit in enumerate(exits[:max_show]):
            emoji = ReportFormatter.pnl_emoji(exit.get('pnl', 0))
            pnl_str = ReportFormatter.format_currency(exit.get('pnl', 0), show_sign=True)
            pct = exit.get('pnl_pct', 0)
            days = exit.get('days', 0)

            line = f"{emoji} {exit['script']}({exit['tf']}) @{exit['exit_price']:.0f} {pnl_str} ({pct:+.1f}%) {days}d"
            lines.append(line)

        if len(exits) > max_show:
            remaining = len(exits) - max_show
            remaining_pnl = sum(e.get('pnl', 0) for e in exits[max_show:])
            lines.append(f"   +{remaining} more ({ReportFormatter.format_currency(remaining_pnl, show_sign=True)})")

        return "\n".join(lines)

    @staticmethod
    def format_issues_compact(issues: List[str], issue_type: str = "Issues", max_show: int = 3) -> str:
        """Format issues/warnings list compactly"""
        if not issues:
            return ""

        emoji = ReportFormatter.WARNING if "warn" in issue_type.lower() else ReportFormatter.CROSS
        lines = [f"{emoji} *{issue_type}* ({len(issues)}):"]

        for issue in issues[:max_show]:
            # Truncate long issues
            if len(issue) > 60:
                issue = issue[:57] + "..."
            lines.append(f"  {issue}")

        if len(issues) > max_show:
            lines.append(f"  +{len(issues) - max_show} more")

        return "\n".join(lines)


# Singleton instance
fmt = ReportFormatter()

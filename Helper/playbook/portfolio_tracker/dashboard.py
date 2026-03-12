"""
Portfolio Dashboard — merges live holdings with decision tracker.

Pure computation: receives pre-fetched data, classifies by tier,
checks allocation caps, computes P&L. Caller handles I/O.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

CONFIG_FILE = Path(__file__).parent.parent.parent / 'config' / 'portfolio_config.json'


def _load_tier_config() -> dict:
    """Load tier classification from config."""
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {}


def classify_holding(symbol: str, config: dict = None) -> str:
    """Classify a holding into a tier.

    Returns: 'tier1_parking', 'tier2_etf', 'tier2_gold_silver',
             'tier2_reit', 'tier2_stock'
    """
    cfg = config or _load_tier_config()
    tiers = cfg.get('tier_classification', {})
    sym = symbol.upper()

    if sym in [s.upper() for s in tiers.get('tier1_parking', [])]:
        return 'tier1_parking'
    if sym in [s.upper() for s in tiers.get('tier2_etfs', [])]:
        return 'tier2_etf'
    if sym in [s.upper() for s in tiers.get('tier2_gold_silver', [])]:
        return 'tier2_gold_silver'
    if sym in [s.upper() for s in tiers.get('tier2_reits', [])]:
        return 'tier2_reit'
    return 'tier2_stock'


def generate_dashboard(holdings: list,
                       decisions: list = None,
                       bcs_trades: list = None,
                       fh_trades: list = None,
                       watchlist: list = None,
                       config: dict = None,
                       account_id: str = None) -> dict:
    """Generate portfolio dashboard from pre-fetched data.

    Args:
        holdings: List of Kite holdings dicts (from get_holdings MCP).
            Expected keys: tradingsymbol, quantity, average_price,
                          last_price, pnl, close_price
        decisions: List of decision dicts from DecisionStore.
        bcs_trades: List of BCS trades (open only).
        fh_trades: List of Fallen Hero trades (open only).
        watchlist: List of active watchlist items.
        config: Portfolio config dict.
        account_id: Filter to specific account (e.g. "QSK814").

    Returns:
        Dashboard dict with summary, tier allocation, enriched holdings,
        options positions, alerts.
    """
    cfg = config or _load_tier_config()
    decisions = decisions or []
    bcs_trades = bcs_trades or []
    fh_trades = fh_trades or []
    watchlist = watchlist or []

    # Filter decisions by account if specified
    if account_id:
        decisions = [d for d in decisions
                     if d.get('account_id') == account_id or d.get('account_id') is None]

    # Build decision lookup by symbol
    decision_by_symbol = {}
    for d in decisions:
        sym = d.get('symbol', '').upper()
        if d.get('status') == 'accepted':
            decision_by_symbol[sym] = d

    # Classify and enrich holdings
    enriched_holdings = []
    tier_amounts = {
        'tier1_parking': 0.0,
        'tier2_etf': 0.0,
        'tier2_gold_silver': 0.0,
        'tier2_reit': 0.0,
        'tier2_stock': 0.0,
    }
    total_value = 0.0
    total_invested = 0.0
    total_pnl = 0.0

    for h in holdings:
        sym = h.get('tradingsymbol', '').upper()
        qty = h.get('quantity', 0)
        avg_price = h.get('average_price', 0)
        last_price = h.get('last_price') or h.get('close_price', 0)
        pnl = h.get('pnl', 0)

        if qty <= 0:
            continue

        current_value = qty * last_price
        invested = qty * avg_price
        pnl_pct = ((last_price - avg_price) / avg_price * 100) if avg_price > 0 else 0

        tier = classify_holding(sym, cfg)
        tier_amounts[tier] += current_value
        total_value += current_value
        total_invested += invested
        total_pnl += pnl

        # Link to decision
        decision = decision_by_symbol.get(sym)
        days_held = None
        if decision and decision.get('buy_date'):
            try:
                buy_dt = datetime.strptime(decision['buy_date'], '%Y-%m-%d')
                days_held = (datetime.now() - buy_dt).days
            except (ValueError, TypeError):
                pass

        enriched = {
            'symbol': sym,
            'quantity': qty,
            'avg_price': round(avg_price, 2),
            'current_price': round(last_price, 2),
            'current_value': round(current_value, 2),
            'invested': round(invested, 2),
            'pnl': round(pnl, 2),
            'pnl_pct': round(pnl_pct, 2),
            'tier': tier,
            'decision_id': decision['id'] if decision else None,
            'verdict_at_buy': decision.get('verdict') if decision else None,
            'target': decision.get('target_price') if decision else None,
            'stop_loss': decision.get('stop_loss') if decision else None,
            'days_held': days_held,
        }
        enriched_holdings.append(enriched)

    # Sort by current value descending
    enriched_holdings.sort(key=lambda h: h['current_value'], reverse=True)

    # Tier allocation
    allocation = {}
    for tier, amount in tier_amounts.items():
        pct = (amount / total_value * 100) if total_value > 0 else 0
        allocation[tier] = {
            'amount': round(amount, 2),
            'pct': round(pct, 1),
        }

    # Allocation caps check
    caps = cfg.get('allocation_caps', {})
    alerts = {
        'sl_breached': [],
        'target_hit': [],
        'review_due': [],
        'cap_breached': [],
    }

    # Check per-holding caps
    max_stock_pct = caps.get('max_per_stock_pct', 3)
    max_etf_pct = caps.get('max_per_etf_pct', 20)
    for h in enriched_holdings:
        pct_of_portfolio = (h['current_value'] / total_value * 100) if total_value > 0 else 0
        h['portfolio_pct'] = round(pct_of_portfolio, 1)

        cap = max_etf_pct if h['tier'] in ('tier2_etf', 'tier2_gold_silver') else max_stock_pct
        if pct_of_portfolio > cap:
            alerts['cap_breached'].append(
                "%s at %.1f%% (cap: %d%%)" % (h['symbol'], pct_of_portfolio, cap)
            )

        # SL/Target alerts
        if h.get('stop_loss') and h['current_price'] <= h['stop_loss']:
            alerts['sl_breached'].append(
                "%s at Rs %.1f (SL: Rs %.1f)" % (h['symbol'], h['current_price'], h['stop_loss'])
            )
        if h.get('target') and h['current_price'] >= h['target']:
            alerts['target_hit'].append(
                "%s at Rs %.1f (Target: Rs %.1f)" % (h['symbol'], h['current_price'], h['target'])
            )

    # Review due check
    review_days = cfg.get('review_interval_days', 30)
    for d in decisions:
        if d.get('status') not in ('accepted', 'watching'):
            continue
        last_rev = d.get('last_reviewed')
        if last_rev:
            try:
                rev_dt = datetime.strptime(last_rev, '%Y-%m-%d')
                if (datetime.now() - rev_dt).days > review_days:
                    alerts['review_due'].append(
                        "%s (last reviewed %s)" % (d['symbol'], last_rev)
                    )
            except (ValueError, TypeError):
                pass
        else:
            # Never reviewed
            alerts['review_due'].append(
                "%s (never reviewed)" % d['symbol']
            )

    # Options positions
    open_bcs = [t for t in bcs_trades if t.get('status') == 'open']
    open_fh = [t for t in fh_trades if t.get('status') == 'open']

    # Active watchlist
    active_alerts = [w for w in watchlist if w.get('status') == 'watching']

    # Resolve account metadata
    acct_info = {}
    if account_id:
        accts = cfg.get('accounts', {})
        acct_info = accts.get(account_id, {'name': account_id, 'user_id': account_id})

    return {
        'generated_at': datetime.now().isoformat(),
        'account': {
            'id': account_id,
            'name': acct_info.get('name', 'All Accounts'),
            'purpose': acct_info.get('purpose', ''),
        },
        'portfolio_summary': {
            'total_value': round(total_value, 2),
            'total_invested': round(total_invested, 2),
            'total_pnl': round(total_pnl, 2),
            'total_pnl_pct': round(
                (total_pnl / total_invested * 100) if total_invested > 0 else 0, 2
            ),
            'num_holdings': len(enriched_holdings),
        },
        'tier_allocation': allocation,
        'holdings': enriched_holdings,
        'options_positions': {
            'bcs': [{'stock': t['stock'], 'status': t['status'],
                     'net_debit': t.get('net_debit'),
                     'expiry': t.get('expiry')} for t in open_bcs],
            'fallen_hero': [{'stock': t['stock'], 'status': t['status'],
                             'total_credit': t.get('total_credit'),
                             'expiry': t.get('expiry')} for t in open_fh],
        },
        'alerts': alerts,
        'watchlist_active': [{'symbol': w.get('symbol'), 'condition': w.get('condition'),
                              'target_price': w.get('target_price')}
                             for w in active_alerts],
    }


def format_dashboard_terminal(dashboard: dict) -> str:
    """Format dashboard for terminal display."""
    lines = []
    hr = "=" * 80

    acct = dashboard.get('account', {})
    acct_label = acct.get('name', 'All Accounts')
    if acct.get('id'):
        acct_label += f" ({acct['id']})"

    lines.append(hr)
    lines.append(f"  PORTFOLIO DASHBOARD — {acct_label}")
    lines.append(hr)

    # Summary
    summary = dashboard['portfolio_summary']
    lines.append(f"\n  Total Value:    Rs {summary['total_value']:>12,.0f}")
    lines.append(f"  Total Invested: Rs {summary['total_invested']:>12,.0f}")
    pnl = summary['total_pnl']
    lines.append(f"  Total P&L:      Rs {pnl:>+12,.0f} ({summary['total_pnl_pct']:+.1f}%)")
    lines.append(f"  Holdings:       {summary['num_holdings']}")
    lines.append("")

    # Tier allocation
    lines.append("  TIER ALLOCATION")
    lines.append("-" * 80)
    tier_labels = {
        'tier1_parking': 'Tier 1 (Parking)',
        'tier2_etf': 'Tier 2 (ETFs)',
        'tier2_gold_silver': 'Tier 2 (Gold/Silver)',
        'tier2_reit': 'Tier 2 (REITs)',
        'tier2_stock': 'Tier 2 (Stocks)',
    }
    for tier, data in dashboard['tier_allocation'].items():
        label = tier_labels.get(tier, tier)
        lines.append(f"  {label:<22} Rs {data['amount']:>12,.0f}  ({data['pct']:>5.1f}%)")
    lines.append("")

    # Holdings
    lines.append("  HOLDINGS")
    lines.append("-" * 80)
    lines.append(f"  {'Symbol':<14} {'Qty':>6} {'Avg':>8} {'CMP':>8} "
                 f"{'P&L':>10} {'P&L%':>7} {'Port%':>6}")
    for h in dashboard['holdings']:
        lines.append(f"  {h['symbol']:<14} {h['quantity']:>6} "
                     f"{h['avg_price']:>8.1f} {h['current_price']:>8.1f} "
                     f"{h['pnl']:>+10,.0f} {h['pnl_pct']:>+6.1f}% "
                     f"{h.get('portfolio_pct', 0):>5.1f}%")
    lines.append("")

    # Alerts
    alerts = dashboard['alerts']
    has_alerts = any(v for v in alerts.values())
    if has_alerts:
        lines.append("  ALERTS")
        lines.append("-" * 80)
        for alert_type, items in alerts.items():
            if items:
                label = alert_type.replace('_', ' ').upper()
                for item in items:
                    lines.append(f"  ! [{label}] {item}")
        lines.append("")

    # Options
    opts = dashboard['options_positions']
    if opts['bcs'] or opts['fallen_hero']:
        lines.append("  OPTIONS POSITIONS")
        lines.append("-" * 80)
        for t in opts['bcs']:
            lines.append(f"  BCS: {t['stock']} | Debit: Rs {t.get('net_debit', 0):.2f} | "
                         f"Expiry: {t.get('expiry', 'N/A')}")
        for t in opts['fallen_hero']:
            lines.append(f"  FH:  {t['stock']} | Credit: Rs {t.get('total_credit', 0):.2f} | "
                         f"Expiry: {t.get('expiry', 'N/A')}")
        lines.append("")

    lines.append(hr)
    return "\n".join(lines)

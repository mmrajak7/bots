"""
Watchlist Alert Checker — checks price conditions and fires Telegram alerts.

Called from spread_monitor's monitor_all() loop. Lightweight:
  - Single batch LTP call for all watched symbols
  - One-shot trigger: status flips to 'triggered', no repeat alerts
  - Periodic store refresh picks up Drive sync updates
"""

import logging
import time
from typing import Callable, Optional

from .watchlist import get_watchlist_store, WatchlistStore

logger = logging.getLogger(__name__)

# Refresh watchlist from disk every 60s (picks up Drive sync changes)
_DISK_REFRESH_INTERVAL = 60
_last_disk_refresh = 0.0


def check_watchlist_alerts(
    kite,
    log_fn: Callable[[str], None],
    telegram_fn: Callable[[str], None],
    store: Optional[WatchlistStore] = None,
):
    """Check all active watchlist alerts against current prices.

    Args:
        kite: Authenticated KiteConnect instance.
        log_fn: Function to log messages (spread_monitor's log()).
        telegram_fn: Function to send Telegram alerts (spread_monitor's send_telegram()).
        store: Optional WatchlistStore instance (uses singleton if None).
    """
    global _last_disk_refresh

    if store is None:
        store = get_watchlist_store()

    # Periodic refresh from disk (picks up Drive sync writes)
    now = time.time()
    if now - _last_disk_refresh >= _DISK_REFRESH_INTERVAL:
        store._load_local()
        _last_disk_refresh = now

    active = store.get_active()
    if not active:
        return

    # Batch LTP call — one API request for all watched symbols
    symbols = list({item['spot_symbol'] for item in active})
    try:
        ltp_data = kite.ltp(symbols)
    except Exception as e:
        log_fn(f"  Watchlist: LTP fetch failed: {e}")
        return

    triggered_count = 0
    for item in active:
        spot_sym = item['spot_symbol']
        if spot_sym not in ltp_data:
            continue

        price = ltp_data[spot_sym]['last_price']
        alert = item['alert']
        condition = alert['condition']
        trigger_price = alert['price']

        fired = False
        if condition == 'above' and price > trigger_price:
            fired = True
        elif condition == 'below' and price < trigger_price:
            fired = True

        if fired:
            triggered_count += 1
            store.trigger(item['id'], price)

            # Build Telegram message
            direction = "above" if condition == "above" else "below"
            plan_str = ""
            plan = item.get('plan')
            if plan:
                parts = []
                if plan.get('long_strike') and plan.get('short_strike'):
                    parts.append(f"{item.get('strategy', '?')} {plan['long_strike']}/{plan['short_strike']}")
                if plan.get('target_debit_pct'):
                    parts.append(f"Target debit <{plan['target_debit_pct']}%")
                if plan.get('notes'):
                    parts.append(plan['notes'])
                if parts:
                    plan_str = f"\nPlan: {' | '.join(parts)}"

            msg = (
                f"WATCHLIST ALERT: {item['stock']}\n"
                f"Price: Rs {price:,.2f} ({direction} {trigger_price:,.2f} trigger)\n\n"
                f"Thesis: {item.get('thesis', 'N/A')}\n"
                f"Strategy: {item.get('strategy', '?')}{plan_str}\n\n"
                f"Action: {alert.get('message', 'Review and enter if conditions hold.')}"
            )

            log_fn(f"\n  *** WATCHLIST ALERT: {item['stock']} @ {price:.2f} "
                   f"({direction} {trigger_price:.2f}) ***")
            telegram_fn(msg)

    if triggered_count > 0:
        log_fn(f"  Watchlist: {triggered_count} alert(s) triggered")

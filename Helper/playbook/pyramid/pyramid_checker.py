"""Pyramid month-end checker — update SLs, check level triggers, send alerts.

Monthly cadence, NOT integrated into spread_monitor's 5-second poll loop.
Run manually or via monthly cron: python -m playbook.pyramid check
"""

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

from .pyramid_store import PyramidStore

logger = logging.getLogger(__name__)

BOTS_ROOT = Path(__file__).resolve().parents[2].parent  # BOTS/


def _default_log(msg: str):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}")


def _default_telegram(msg: str):
    """Best-effort Telegram send. Never crashes."""
    try:
        config_path = BOTS_ROOT / 'data' / 'telegram_config.json'
        if not config_path.exists():
            return
        with open(config_path) as f:
            cfg = json.load(f)

        import requests
        requests.post(
            f"https://api.telegram.org/bot{cfg['bot_token']}/sendMessage",
            json={'chat_id': cfg['chat_id'], 'text': msg, 'parse_mode': 'HTML'},
            timeout=10,
        )
    except ImportError:
        logger.debug("Telegram skipped: 'requests' not installed")
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


def _prev_month(ref_date: Optional[datetime] = None) -> str:
    """Return YYYY-MM for the previous completed month."""
    ref = ref_date or datetime.now()
    first_of_month = ref.replace(day=1)
    last_of_prev = first_of_month - timedelta(days=1)
    return last_of_prev.strftime('%Y-%m')


def check_month_end(
    kite,
    store: PyramidStore,
    log_fn: Callable = _default_log,
    telegram_fn: Callable = _default_telegram,
    month: Optional[str] = None,
    force: bool = False,
):
    """Run month-end checks for all active pyramid positions.

    1. Determine target_month (default: previous completed month)
    2. For each active position:
       a. Idempotency gate: skip if already checked this month
       b. Fetch monthly candle for target_month
       c. Extract month's intraday low and close
       d. Update monthly low + SL via store
       e. Check pending levels: close > trigger_price → flag for add
    3. Send single Telegram summary
    """
    target_month = month or _prev_month()
    log_fn(f"Pyramid month-end check for {target_month}")

    active = store.get_active()
    if not active:
        log_fn("No active pyramid positions.")
        return

    sl_updates = []
    add_alerts = []
    sl_warnings = []  # Price within 5% of SL
    skipped = []
    errors = []

    for pos in active:
        symbol = pos['symbol']
        pos_id = pos['id']

        # Idempotency gate
        if not force and pos.get('last_checked_month') == target_month:
            skipped.append(symbol)
            log_fn(f"  #{pos_id} {symbol}: already checked for {target_month}, skipping")
            continue

        try:
            # Fetch monthly candle
            month_low, month_close = _fetch_month_candle(
                kite, pos['spot_symbol'], target_month
            )

            if month_low is None:
                log_fn(f"  #{pos_id} {symbol}: no data for {target_month}")
                errors.append(f"{symbol}: no data")
                continue

            log_fn(f"  #{pos_id} {symbol}: month_low={month_low:.2f}, "
                   f"close={month_close:.2f}")

            # Update monthly low + SL
            old_sl = pos.get('current_sl')
            store.update_monthly_low(pos_id, target_month, month_low)
            # Re-fetch position after update
            pos = store._find(pos_id)
            new_sl = pos.get('current_sl')

            if old_sl != new_sl:
                sl_updates.append({
                    'symbol': symbol,
                    'id': pos_id,
                    'old_sl': old_sl,
                    'new_sl': new_sl,
                    'month_low': month_low,
                })
                log_fn(f"    SL: {old_sl or '-'} -> {new_sl}")
            else:
                log_fn(f"    SL unchanged: {new_sl}")

            # SL proximity warning: close within 5% of SL
            if new_sl and month_close > 0:
                gap_pct = ((month_close - new_sl) / new_sl) * 100
                if 0 < gap_pct <= 5:
                    sl_warnings.append({
                        'symbol': symbol,
                        'id': pos_id,
                        'close': month_close,
                        'sl': new_sl,
                        'gap_pct': round(gap_pct, 1),
                    })
                    log_fn(f"    WARNING: close {month_close:.2f} is only "
                           f"{gap_pct:.1f}% above SL {new_sl:.2f}")

            # Check pending level triggers
            for lvl in pos['levels']:
                if lvl['status'] != 'pending':
                    continue
                tp = lvl.get('trigger_price')
                if tp and month_close > tp:
                    add_alerts.append({
                        'symbol': symbol,
                        'id': pos_id,
                        'level': lvl['level'],
                        'trigger_price': tp,
                        'month_close': month_close,
                        'budget': lvl['amount'],
                    })
                    log_fn(f"    L{lvl['level']} TRIGGERED: close {month_close:.2f} "
                           f"> trigger {tp:.2f} (budget Rs {lvl['amount']:,.0f})")

        except Exception as e:
            logger.exception("Error checking %s", symbol)
            errors.append(f"{symbol}: {e}")
            log_fn(f"  #{pos_id} {symbol}: ERROR - {e}")

    # Summary
    log_fn(f"\nMonth-end summary ({target_month}):")
    log_fn(f"  Checked: {len(active) - len(skipped)}, "
           f"Skipped: {len(skipped)}, Errors: {len(errors)}")
    log_fn(f"  SL updates: {len(sl_updates)}, Add alerts: {len(add_alerts)}, "
           f"SL warnings: {len(sl_warnings)}")

    # Telegram summary — send if anything noteworthy happened
    if sl_updates or add_alerts or sl_warnings:
        msg = _build_telegram_summary(
            target_month, sl_updates, add_alerts, sl_warnings, errors
        )
        telegram_fn(msg)


def check_sl_breaches(
    kite,
    store: PyramidStore,
    log_fn: Callable = _default_log,
    telegram_fn: Callable = _default_telegram,
):
    """Check current price vs SL for all active positions. Send alerts on breach."""
    active = store.get_active()
    if not active:
        log_fn("No active pyramid positions.")
        return

    # Use OHLC to get today's intraday low — LTP alone misses touch-and-bounce
    spot_symbols = [p['spot_symbol'] for p in active if p.get('current_sl')]
    if not spot_symbols:
        log_fn("No positions with active SL.")
        return

    try:
        ohlc_data = kite.ohlc(spot_symbols)
    except Exception as e:
        log_fn(f"OHLC fetch failed: {e}")
        return

    breaches = []
    for pos in active:
        sl = pos.get('current_sl')
        if not sl:
            continue

        spot_sym = pos['spot_symbol']
        ohlc_info = ohlc_data.get(spot_sym)
        if not ohlc_info:
            continue

        # Use today's intraday low for SL check, not just LTP
        day_low = ohlc_info.get('ohlc', {}).get('low', 0)
        price = ohlc_info.get('last_price', 0)
        check_price = day_low if day_low > 0 else price

        if check_price <= sl:
            breaches.append({
                'symbol': pos['symbol'],
                'id': pos['id'],
                'day_low': check_price,
                'ltp': price,
                'sl': sl,
                'invested': pos['total_invested'],
                'avg_cost': pos['avg_cost'],
            })
            log_fn(f"  SL BREACH #{pos['id']} {pos['symbol']}: "
                   f"day_low {check_price:.2f} <= SL {sl:.2f} (LTP={price:.2f})")

    if breaches:
        for b in breaches:
            pnl = round((b['ltp'] - b['avg_cost']) * (
                store._find(b['id'])['total_quantity']
            ), 2)
            msg = (
                f"PYRAMID SL BREACH\n"
                f"{b['symbol']} #{b['id']}\n"
                f"Day Low: {b['day_low']:.2f} <= SL: {b['sl']:.2f}\n"
                f"LTP: {b['ltp']:.2f}\n"
                f"Avg Cost: {b['avg_cost']:.2f}\n"
                f"Invested: Rs {b['invested']:,.0f}\n"
                f"Unrealized P&L: Rs {pnl:,.0f}\n"
                f"ACTION: EXIT immediately"
            )
            telegram_fn(msg)
    else:
        log_fn("No SL breaches.")


def _fetch_month_candle(kite, spot_symbol: str, target_month: str):
    """Fetch a single month's OHLC candle from Kite historical data.

    Returns (month_low, month_close) or (None, None) if no data.
    """
    # Parse target month
    year, month_num = target_month.split('-')
    year, month_num = int(year), int(month_num)

    # Date range: first to last day of target month
    from_date = f"{year}-{month_num:02d}-01"
    # Last day of month
    if month_num == 12:
        next_month_first = f"{year + 1}-01-01"
    else:
        next_month_first = f"{year}-{month_num + 1:02d}-01"

    to_date_dt = datetime.strptime(next_month_first, '%Y-%m-%d') - timedelta(days=1)
    to_date = to_date_dt.strftime('%Y-%m-%d')

    # Get instrument token from LTP call
    symbol_clean = spot_symbol  # e.g., "NSE:MARUTI"
    try:
        ltp_resp = kite.ltp([symbol_clean])
        if not ltp_resp:
            return None, None
        token = ltp_resp[symbol_clean]['instrument_token']
    except Exception:
        logger.warning("Could not get instrument token for %s", symbol_clean)
        return None, None

    # Fetch daily candles for the month
    try:
        candles = kite.historical_data(token, from_date, to_date, 'day')
    except Exception as e:
        logger.warning("Historical data fetch failed for %s: %s", symbol_clean, e)
        return None, None

    if not candles:
        return None, None

    # Extract month low (min of all daily lows) and close (last candle's close)
    month_low = min(c['low'] for c in candles)
    month_close = candles[-1]['close']

    return month_low, month_close


def _build_telegram_summary(target_month, sl_updates, add_alerts,
                            sl_warnings, errors):
    """Build a single Telegram message with all month-end updates."""
    lines = [f"<b>Pyramid Month-End: {target_month}</b>\n"]

    if sl_updates:
        lines.append("<b>SL Updates:</b>")
        for u in sl_updates:
            old = f"{u['old_sl']:.2f}" if u['old_sl'] else "-"
            lines.append(
                f"  {u['symbol']}: {old} -> {u['new_sl']:.2f} "
                f"(low={u['month_low']:.2f})"
            )
        lines.append("")

    if add_alerts:
        lines.append("<b>Level Triggers (ADD):</b>")
        for a in add_alerts:
            lines.append(
                f"  {a['symbol']} L{a['level']}: close {a['month_close']:.2f} "
                f"> trigger {a['trigger_price']:.2f}"
            )
            lines.append(f"    Budget: Rs {a['budget']:,.0f}")
        lines.append("")

    if sl_warnings:
        lines.append("<b>SL Proximity Warnings:</b>")
        for w in sl_warnings:
            lines.append(
                f"  {w['symbol']}: close {w['close']:.2f} is "
                f"{w['gap_pct']}% above SL {w['sl']:.2f}"
            )
        lines.append("")

    if errors:
        lines.append("<b>Errors:</b>")
        for e in errors:
            lines.append(f"  {e}")

    return "\n".join(lines)

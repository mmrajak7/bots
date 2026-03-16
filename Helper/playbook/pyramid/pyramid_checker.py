"""Active basket position checker — month-end ATR trail SL + daily breach detection.

ACTIVE BASKET ONLY. Core & Tactical use plain ST touch (no ATR, no breach monitor).

Two modes:
  - check:  Month-end (run 1st of each month). Computes ATR(14) trailing SL.
            SL = peak_price - 1.5 * ATR(14), only moves UP. No cost-cost floor.
  - breach: Daily (cron every 5 min during market hours). Checks intraday low vs SL.
            De-duplicated: alerts once per position per day, not every 5 minutes.
"""

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Callable, Optional

from .pyramid_store import PyramidStore

logger = logging.getLogger(__name__)

ATR_PERIOD = 14
ATR_TRAIL_MULT = 1.5

BOTS_ROOT = Path(__file__).resolve().parents[2].parent  # BOTS/
BREACH_ALERT_FILE = BOTS_ROOT / 'Helper' / 'logs' / 'pyramid_breach_alerts.json'


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


def _compute_atr(daily_candles: list, period: int = ATR_PERIOD) -> Optional[float]:
    """Compute ATR(period) from daily OHLC candles using Wilder's smoothing.

    Each candle: dict with 'high', 'low', 'close'.
    Returns ATR as a float, or None if insufficient data.
    """
    if len(daily_candles) < period + 1:
        return None

    true_ranges = []
    for i in range(1, len(daily_candles)):
        h = daily_candles[i]['high']
        l = daily_candles[i]['low']
        prev_c = daily_candles[i - 1]['close']
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    # Wilder's smoothed ATR: SMA seed, then EMA
    atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _candle_date(candle) -> date:
    """Extract date from a Kite candle (handles datetime objects and strings)."""
    d = candle.get('date', '')
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    # String: "2026-03-15T00:00:00+05:30" or "2026-03-15"
    return datetime.fromisoformat(str(d).replace('+05:30', '')).date()


def _fetch_daily_candles(kite, spot_symbol: str, num_days: int = 60) -> list:
    """Fetch recent daily candles for ATR computation.

    Handles Kite's 2000-day limit by fetching in chunks if needed.
    Returns list of dicts with date, open, high, low, close.
    """
    try:
        ltp_resp = kite.ltp([spot_symbol])
        if not ltp_resp:
            return []
        token = ltp_resp[spot_symbol]['instrument_token']
    except Exception:
        logger.warning("Could not get instrument token for %s", spot_symbol)
        return []

    to_date = datetime.now().strftime('%Y-%m-%d')
    # Cap at 1900 to stay within Kite's 2000-day limit with margin
    num_days = min(num_days, 1900)
    from_date = (datetime.now() - timedelta(days=num_days)).strftime('%Y-%m-%d')

    try:
        candles = kite.historical_data(token, from_date, to_date, 'day')
        return candles if candles else []
    except Exception as e:
        logger.warning("Daily data fetch failed for %s: %s", spot_symbol, e)
        return []


def _days_since_entry(pos: dict) -> int:
    """Compute calendar days from entry to today."""
    entry_str = pos.get('created_at', '')
    if not entry_str:
        return 60
    try:
        entry_dt = datetime.strptime(entry_str[:10], '%Y-%m-%d').date()
        return (date.today() - entry_dt).days
    except (ValueError, TypeError):
        return 60


def check_month_end(
    kite,
    store: PyramidStore,
    log_fn: Callable = _default_log,
    telegram_fn: Callable = _default_telegram,
    month: Optional[str] = None,
    force: bool = False,
):
    """Run month-end checks for all active positions.

    For each active position:
      1. Fetch monthly candle → record month's low (data tracking only)
      2. Fetch daily candles → compute ATR(14) + find peak since entry
      3. Trail SL = peak - 1.5 * ATR(14), only moves up
      4. Store peak_price for incremental tracking
    """
    target_month = month or _prev_month()
    log_fn(f"Position month-end check for {target_month}")

    active = store.get_active()
    if not active:
        log_fn("No active positions.")
        return

    sl_updates = []
    sl_warnings = []  # Price within 5% of SL
    skipped = []
    errors = []

    dirty = False  # Track if any position was modified

    for pos in active:
        symbol = pos['symbol']
        pos_id = pos['id']

        # Idempotency gate
        if not force and pos.get('last_checked_month') == target_month:
            skipped.append(symbol)
            log_fn(f"  #{pos_id} {symbol}: already checked for {target_month}, skipping")
            continue

        try:
            # Guard: skip if target_month is before entry_month
            entry_month = pos.get('entry_month', '2020-01')
            if target_month < entry_month:
                log_fn(f"  #{pos_id} {symbol}: target {target_month} < "
                       f"entry {entry_month}, skipping")
                skipped.append(symbol)
                continue

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

            # Record monthly low (data tracking only — does NOT set SL)
            # persist=False: batch save at end
            old_sl = pos.get('current_sl')
            store.record_monthly_low(pos_id, target_month, month_low,
                                     persist=False)
            dirty = True

            # Entry month: keep initial SL (touch_month_low), skip ATR trail
            if target_month == entry_month:
                log_fn(f"    Entry month — keeping initial SL, ATR trail skipped")
                pos = store._find(pos_id)
                new_sl = pos.get('current_sl')
                # Still check proximity
                if new_sl and month_close > 0:
                    gap_pct = ((month_close - new_sl) / new_sl) * 100
                    if 0 < gap_pct <= 5:
                        sl_warnings.append({
                            'symbol': symbol, 'id': pos_id,
                            'close': month_close, 'sl': new_sl,
                            'gap_pct': round(gap_pct, 1),
                        })
                continue

            # --- ATR(14) trailing SL computation (month 2+) ---
            hold_days = _days_since_entry(pos)
            fetch_days = max(60, hold_days + 30)
            daily = _fetch_daily_candles(kite, pos['spot_symbol'],
                                         num_days=fetch_days)
            atr_val = _compute_atr(daily, ATR_PERIOD) if daily else None

            if daily and atr_val:
                # Peak = highest close since entry
                entry_date_str = pos.get('created_at', '2020-01-01')[:10]
                try:
                    entry_dt = datetime.strptime(entry_date_str,
                                                 '%Y-%m-%d').date()
                except (ValueError, TypeError):
                    entry_dt = date(2020, 1, 1)

                closes_since_entry = [
                    c['close'] for c in daily
                    if _candle_date(c) >= entry_dt
                ]
                peak_from_data = (max(closes_since_entry)
                                  if closes_since_entry else month_close)

                # Use stored peak as floor (never loses the true peak)
                stored_peak = pos.get('peak_price', 0) or 0
                peak_price = max(peak_from_data, stored_peak, month_close)

                # Persist peak_price (persist=False: batch)
                store.update_peak(pos_id, peak_price, persist=False)

                # Trail = peak - 1.5 * ATR(14)
                atr_trail = round(peak_price - ATR_TRAIL_MULT * atr_val, 2)

                log_fn(f"    ATR(14)={atr_val:.2f}, peak={peak_price:.2f}, "
                       f"trail={atr_trail:.2f}")

                # SL = max(current_sl, atr_trail) — only moves up
                pos = store._find(pos_id)
                current = pos.get('current_sl') or 0
                if atr_trail > current:
                    store.update_sl(pos_id, atr_trail, persist=False)
                    log_fn(f"    ATR trail SL: {current:.2f} -> {atr_trail:.2f}")
            else:
                log_fn(f"    ATR computation skipped (insufficient daily data)")

            # Re-fetch position after all updates
            pos = store._find(pos_id)
            new_sl = pos.get('current_sl')

            if old_sl != new_sl:
                sl_updates.append({
                    'symbol': symbol,
                    'id': pos_id,
                    'old_sl': old_sl,
                    'new_sl': new_sl,
                    'month_low': month_low,
                    'atr': round(atr_val, 2) if atr_val else None,
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

        except Exception as e:
            logger.exception("Error checking %s", symbol)
            errors.append(f"{symbol}: {e}")
            log_fn(f"  #{pos_id} {symbol}: ERROR - {e}")

    # Single save + Drive upload for all position changes
    if dirty:
        store.flush()

    # Summary
    log_fn(f"\nMonth-end summary ({target_month}):")
    log_fn(f"  Checked: {len(active) - len(skipped)}, "
           f"Skipped: {len(skipped)}, Errors: {len(errors)}")
    log_fn(f"  SL updates: {len(sl_updates)}, "
           f"SL warnings: {len(sl_warnings)}")

    # Telegram summary
    if sl_updates or sl_warnings:
        msg = _build_telegram_summary(
            target_month, sl_updates, sl_warnings, errors
        )
        telegram_fn(msg)


def _load_breach_alerts() -> dict:
    """Load breach alert state: {position_id: last_alerted_date}."""
    if not BREACH_ALERT_FILE.exists():
        return {}
    try:
        with open(BREACH_ALERT_FILE) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_breach_alerts(state: dict):
    """Save breach alert state atomically."""
    BREACH_ALERT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = BREACH_ALERT_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2)
        f.flush()
    tmp.replace(BREACH_ALERT_FILE)


def _ist_now() -> datetime:
    """Get current datetime in IST (UTC+5:30).

    Works on Python 3.8+ without zoneinfo.
    """
    utc_now = datetime.utcnow()
    return utc_now + timedelta(hours=5, minutes=30)


def _is_market_hours() -> bool:
    """Check if within NSE trading hours (9:15 - 15:30 IST)."""
    from datetime import time as dt_time
    now_ist = _ist_now().time()
    return dt_time(9, 15) <= now_ist < dt_time(15, 30)


def check_sl_breaches(
    kite,
    store: PyramidStore,
    log_fn: Callable = _default_log,
    telegram_fn: Callable = _default_telegram,
):
    """Check current price vs SL for all active positions. Send alerts on breach.

    De-duplicated: each position is alerted at most once per calendar day.
    Skips silently outside market hours (9:15-15:30 IST).
    """
    # Skip outside market hours
    if not _is_market_hours():
        log_fn("Outside market hours, skipping breach check.")
        return

    active = store.get_active()
    if not active:
        log_fn("No active positions.")
        return

    # Use OHLC to get today's intraday low
    spot_symbols = [p['spot_symbol'] for p in active if p.get('current_sl')]
    if not spot_symbols:
        log_fn("No positions with active SL.")
        return

    try:
        ohlc_data = kite.ohlc(spot_symbols)
    except Exception as e:
        log_fn(f"OHLC fetch failed: {e}")
        return

    # Load de-duplication state
    alert_state = _load_breach_alerts()
    today = datetime.now().strftime('%Y-%m-%d')

    breaches = []
    for pos in active:
        sl = pos.get('current_sl')
        if not sl:
            continue

        spot_sym = pos['spot_symbol']
        ohlc_info = ohlc_data.get(spot_sym)
        if not ohlc_info:
            continue

        day_low = ohlc_info.get('ohlc', {}).get('low', 0)
        price = ohlc_info.get('last_price', 0)
        check_price = day_low if day_low > 0 else price

        if check_price <= sl:
            pos_key = str(pos['id'])

            # De-duplicate: skip if already alerted today
            if alert_state.get(pos_key) == today:
                log_fn(f"  SL BREACH #{pos['id']} {pos['symbol']}: "
                       f"already alerted today, skipping Telegram")
                continue

            # Use .get() with legacy fallbacks to handle old-schema positions
            entry_amt = pos.get('entry_amount') or pos.get('total_invested', 0)
            entry_px = pos.get('entry_price') or pos.get('avg_cost', 0)

            breaches.append({
                'symbol': pos['symbol'],
                'id': pos['id'],
                'day_low': check_price,
                'ltp': price,
                'sl': sl,
                'invested': entry_amt,
                'entry_price': entry_px,
            })
            log_fn(f"  SL BREACH #{pos['id']} {pos['symbol']}: "
                   f"day_low {check_price:.2f} <= SL {sl:.2f} (LTP={price:.2f})")

    if breaches:
        for b in breaches:
            p = store._find(b['id'])
            entry_qty = p.get('entry_quantity') or p.get('total_quantity', 0)
            pnl = round((b['ltp'] - b['entry_price']) * entry_qty, 2)
            msg = (
                f"🔴 SL BREACH\n"
                f"{b['symbol']} #{b['id']}\n"
                f"Day Low: {b['day_low']:.2f} <= SL: {b['sl']:.2f}\n"
                f"LTP: {b['ltp']:.2f}\n"
                f"Entry: {b['entry_price']:.2f}\n"
                f"Invested: Rs {b['invested']:,.0f}\n"
                f"Unrealized P&L: Rs {pnl:,.0f}\n"
                f"ACTION: EXIT immediately"
            )
            telegram_fn(msg)
            alert_state[str(b['id'])] = today

        _save_breach_alerts(alert_state)
    else:
        log_fn("No SL breaches.")


def _fetch_month_candle(kite, spot_symbol: str, target_month: str):
    """Fetch a single month's OHLC candle from Kite historical data.

    Returns (month_low, month_close) or (None, None) if no data.
    """
    year, month_num = target_month.split('-')
    year, month_num = int(year), int(month_num)

    from_date = f"{year}-{month_num:02d}-01"
    if month_num == 12:
        next_month_first = f"{year + 1}-01-01"
    else:
        next_month_first = f"{year}-{month_num + 1:02d}-01"

    to_date_dt = datetime.strptime(next_month_first, '%Y-%m-%d') - timedelta(days=1)
    to_date = to_date_dt.strftime('%Y-%m-%d')

    symbol_clean = spot_symbol
    try:
        ltp_resp = kite.ltp([symbol_clean])
        if not ltp_resp:
            return None, None
        token = ltp_resp[symbol_clean]['instrument_token']
    except Exception:
        logger.warning("Could not get instrument token for %s", symbol_clean)
        return None, None

    try:
        candles = kite.historical_data(token, from_date, to_date, 'day')
    except Exception as e:
        logger.warning("Historical data fetch failed for %s: %s", symbol_clean, e)
        return None, None

    if not candles:
        return None, None

    month_low = min(c['low'] for c in candles)
    month_close = candles[-1]['close']

    return month_low, month_close


def _build_telegram_summary(target_month, sl_updates, sl_warnings, errors):
    """Build a single Telegram message with month-end updates."""
    lines = [f"<b>Month-End Check: {target_month}</b>\n"]

    if sl_updates:
        lines.append("<b>SL Updates (ATR trail):</b>")
        for u in sl_updates:
            old = f"{u['old_sl']:.2f}" if u['old_sl'] else "-"
            atr_str = f", ATR={u['atr']:.2f}" if u.get('atr') else ""
            lines.append(
                f"  {u['symbol']}: {old} -> {u['new_sl']:.2f} "
                f"(low={u['month_low']:.2f}{atr_str})"
            )
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

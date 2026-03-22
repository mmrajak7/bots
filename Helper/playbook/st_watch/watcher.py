"""ST Watch core — compute ST, check gaps, send alerts.

Design:
- Permanent watchlist (Core + Tactical ETFs + REITs) from config
- Every symbol scanned on BOTH monthly and weekly ST
- Daily data fetched ONCE per symbol, reused for both aggregations
- ST values cached daily (don't change intraday)
- Hourly LTP check during market hours -> gap % -> threshold alerts
- Alert dedup: per (symbol, timeframe) per threshold, resets when gap widens
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta

import pytz

from . import config as cfg

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

BASKET_LABELS = {
    'core': 'Core',
    'tactical_etfs': 'Tactical ETF',
    'tactical_reits': 'Tactical REIT',
}


def _today_ist() -> str:
    """Current date in IST (handles servers running in UTC)."""
    return datetime.now(IST).strftime('%Y-%m-%d')


# ── Kite helpers ──────────────────────────────────────────────────────────

def _get_kite():
    """Initialize KiteConnect from shared token file."""
    from kiteconnect import KiteConnect

    if not cfg.KITE_TOKEN_FILE.exists():
        raise FileNotFoundError(f"Kite token not found: {cfg.KITE_TOKEN_FILE}")

    with open(cfg.KITE_TOKEN_FILE) as f:
        token_data = json.load(f)

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# ── Instrument cache ─────────────────────────────────────────────────────

_instrument_cache: dict = {}
_instrument_cache_loaded = False


def _load_instrument_cache(kite):
    """Load and cache NSE instrument list (once per session)."""
    global _instrument_cache, _instrument_cache_loaded
    if _instrument_cache_loaded:
        return
    logger.info("Loading NSE instrument list (one-time)...")
    instruments = kite.instruments('NSE')
    _instrument_cache = {inst['tradingsymbol']: inst['instrument_token']
                         for inst in instruments}
    _instrument_cache_loaded = True
    logger.info("Cached %d NSE instruments", len(_instrument_cache))


def _get_token(kite, symbol: str) -> int:
    """Get NSE instrument token for historical data fetch."""
    _load_instrument_cache(kite)
    token = _instrument_cache.get(symbol)
    if token is None:
        raise ValueError(f"Instrument token not found for {symbol}")
    return token


def _get_ltp(kite, symbols: list) -> dict:
    """Get LTP for multiple NSE symbols. Returns {symbol: price}."""
    if not symbols:
        return {}
    instruments = [f"NSE:{s}" for s in symbols]
    try:
        data = kite.ltp(instruments)
        result = {}
        for key, val in data.items():
            sym = key.replace('NSE:', '')
            result[sym] = val['last_price']
        return result
    except Exception as e:
        logger.error("LTP fetch failed: %s", e)
        return {}


# ── Daily data cache (fetched once per symbol, reused for M+W) ───────────

_daily_cache: dict = {}   # {symbol: {'data': [...], 'date': 'YYYY-MM-DD'}}


def _fetch_daily_data(kite, symbol: str) -> list:
    """Fetch 6Y daily OHLC from Kite. Cached per session per day."""
    today = _today_ist()
    cached = _daily_cache.get(symbol)
    if cached and cached.get('date') == today:
        return cached['data']

    token = _get_token(kite, symbol)
    now = datetime.now()
    start = now - timedelta(days=365 * cfg.DATA_YEARS)

    # 6Y needs 2 chunks (Kite limit: 2000 days)
    mid = now - timedelta(days=365 * 3)
    chunk1 = kite.historical_data(
        token, start.strftime('%Y-%m-%d'),
        mid.strftime('%Y-%m-%d'), 'day'
    )
    time.sleep(0.5)
    chunk2 = kite.historical_data(
        token, (mid + timedelta(days=1)).strftime('%Y-%m-%d'),
        now.strftime('%Y-%m-%d'), 'day'
    )
    time.sleep(0.5)
    daily_data = chunk1 + chunk2

    # Normalize dates to string
    normalized = []
    for c in daily_data:
        row = dict(c)
        if not isinstance(row['date'], str):
            row['date'] = row['date'].isoformat()
        normalized.append(row)

    _daily_cache[symbol] = {'data': normalized, 'date': today}
    return normalized


# ── ST computation (cached daily, keyed by symbol+timeframe) ─────────────

_st_cache: dict = {}  # {'NIFTYBEES_monthly': {st, direction, atr, ...}}


def _compute_st(daily_data: list, timeframe: str) -> dict:
    """Compute ST from daily data for a given timeframe. Returns latest ST row or {}."""
    from playbook.compute_st import compute_supertrend, aggregate_to_monthly, aggregate_to_weekly

    if timeframe == 'monthly':
        candles = aggregate_to_monthly(daily_data, exclude_current=True)
    elif timeframe == 'weekly':
        candles = aggregate_to_weekly(daily_data, exclude_current=True)
    else:
        return {}

    if len(candles) < cfg.ST_PERIOD + 1:
        return {}

    st_result = compute_supertrend(candles, cfg.ST_PERIOD, cfg.ST_MULTIPLIER)
    if not st_result:
        return {}

    latest = st_result[-1]
    return {
        'st': latest['supertrend'],
        'direction': latest['direction'],
        'atr': latest['atr'],
        'last_close': latest['close'],
    }


def compute_all_st(kite, symbols: list) -> dict:
    """Compute monthly + weekly ST for all symbols.

    Returns {cache_key: {st, direction, atr, symbol, timeframe, basket}}.
    Fetches daily data ONCE per symbol, computes both timeframes.
    """
    today = _today_ist()
    results = {}

    for sym_info in symbols:
        symbol = sym_info['symbol']
        basket = sym_info['basket']

        # Check if both timeframes already cached today
        m_key = f"{symbol}_monthly"
        w_key = f"{symbol}_weekly"
        m_cached = _st_cache.get(m_key, {}).get('date_computed') == today
        w_cached = _st_cache.get(w_key, {}).get('date_computed') == today

        if m_cached and w_cached:
            results[m_key] = _st_cache[m_key]
            results[w_key] = _st_cache[w_key]
            continue

        # Fetch daily data once
        try:
            daily_data = _fetch_daily_data(kite, symbol)
        except Exception as e:
            logger.error("%s: failed to fetch data: %s", symbol, e)
            continue

        if not daily_data:
            logger.warning("%s: no daily data returned", symbol)
            continue

        # Compute both timeframes
        for tf in cfg.TIMEFRAMES:
            cache_key = f"{symbol}_{tf}"
            if _st_cache.get(cache_key, {}).get('date_computed') == today:
                results[cache_key] = _st_cache[cache_key]
                continue

            st = _compute_st(daily_data, tf)
            if st:
                st['symbol'] = symbol
                st['timeframe'] = tf
                st['basket'] = basket
                st['date_computed'] = today
                _st_cache[cache_key] = st
                results[cache_key] = st
                logger.debug("%s %s: ST=%.2f dir=%s", symbol, tf, st['st'], st['direction'])
            else:
                logger.warning("%s: not enough %s candles for ST", symbol, tf)

    return results


# ── Alert state management ───────────────────────────────────────────────

def _load_state() -> dict:
    """Load alert state. Keys are 'SYMBOL_timeframe'."""
    if not cfg.STATE_FILE.exists():
        return {}
    try:
        with open(cfg.STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("State file corrupted (%s), resetting alert history", e)
        try:
            corrupt = cfg.STATE_FILE.with_suffix('.json.corrupt')
            cfg.STATE_FILE.rename(corrupt)
        except Exception:
            pass
        return {}


def _save_state(state: dict):
    """Persist alert state to disk (atomic write via temp file)."""
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cfg.STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(str(tmp), str(cfg.STATE_FILE))


def _find_alert_threshold(gap_pct: float, thresholds: list,
                          state_key: str, state: dict,
                          cooldown_hours: int):
    """Return the threshold to alert at, or None if no alert needed.

    Logic:
    1. Find the tightest threshold the gap has crossed
    2. Alert if it's tighter than the last alerted threshold
    3. Or same threshold but cooldown has elapsed
    """
    sym_state = state.get(state_key, {})
    last_threshold = sym_state.get('last_threshold')
    last_alert_time = sym_state.get('last_alert_time')

    # Find tightest matching threshold (smallest number the gap fits within)
    matched = None
    for t in sorted(thresholds):  # ascending: 1, 3, 5
        if gap_pct <= t:
            matched = t
            break

    if matched is None:
        return None  # gap wider than all thresholds

    # New tighter threshold crossed?
    if last_threshold is None or matched < last_threshold:
        return matched

    # Same threshold — check cooldown
    if matched == last_threshold and last_alert_time:
        try:
            last_dt = datetime.fromisoformat(last_alert_time)
            hours_since = (datetime.now(IST) - last_dt.replace(
                tzinfo=IST if last_dt.tzinfo is None else last_dt.tzinfo
            )).total_seconds() / 3600
            if hours_since >= cooldown_hours:
                return matched
        except (ValueError, TypeError):
            return matched

    return None


# ── Telegram ──────────────────────────────────────────────────────────────

_telegram_cfg = None
_telegram_cfg_loaded = False


def _send_telegram(msg: str):
    """Send Telegram alert. Best-effort: never blocks or crashes."""
    global _telegram_cfg, _telegram_cfg_loaded

    try:
        if not _telegram_cfg_loaded:
            if cfg.TELEGRAM_CONFIG.exists():
                with open(cfg.TELEGRAM_CONFIG) as f:
                    _telegram_cfg = json.load(f)
            _telegram_cfg_loaded = True

        if not _telegram_cfg:
            logger.info("Telegram not configured, skipping alert")
            return

        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
        if resp.ok:
            logger.info("Telegram alert sent")
        else:
            logger.warning("Telegram API returned %d: %s", resp.status_code, resp.text)
    except ImportError:
        logger.info("Telegram skipped: 'requests' not installed")
    except Exception as e:
        logger.warning("Telegram alert failed: %s", e)


def _format_alert(symbol: str, basket: str, timeframe: str,
                  ltp: float, st_val: float, gap_pct: float,
                  direction: str, prev_gap: float = None) -> str:
    """Format Telegram alert message with direction awareness."""
    abs_gap = abs(gap_pct)

    # Urgency based on proximity
    if abs_gap <= 1:
        urgency = "\U0001f534 ST TOUCH"
    elif abs_gap <= 3:
        urgency = "\U0001f7e1 NEAR ST"
    else:
        urgency = "\U0001f7e2 APPROACHING"

    # Direction context
    if direction == 'DOWN':
        urgency += " (from below)"

    basket_label = BASKET_LABELS.get(basket, basket.replace('_', ' ').title())
    tf_label = timeframe.capitalize()

    # Trend tracking (use abs for correct direction comparison)
    trend = ""
    if prev_gap is not None:
        if abs(prev_gap) > abs_gap:
            trend = f" (was {prev_gap:+.1f}%, closing in)"
        elif abs(prev_gap) < abs_gap:
            trend = f" (was {prev_gap:+.1f}%, widening)"

    msg = (
        f"<b>{urgency} -- {symbol}</b>\n"
        f"Basket: {basket_label} | {tf_label} ST\n"
        f"LTP: Rs {ltp:,.2f} | ST: Rs {st_val:,.2f}\n"
        f"Gap: {gap_pct:+.1f}%{trend}\n"
        f"Direction: {direction}"
    )

    # Action guidance based on direction and basket
    if abs_gap <= 1:
        if direction == 'UP':
            if basket.startswith('core'):
                msg += "\n\n<i>CORE: ST support touch. Buy on dip. Hold forever.</i>"
            elif 'reit' in basket:
                msg += f"\n\n<i>REIT: {tf_label} ST touch. Check for entry.</i>"
            else:
                msg += f"\n\n<i>TACTICAL: {tf_label} ST touch. Check for entry.</i>"
        else:
            msg += "\n\n<i>ST DOWN -- price approaching from below. Potential trend flip. Watch closely.</i>"

    return msg


# ── Alert history logging ────────────────────────────────────────────────

def _log_alert(symbol: str, basket: str, timeframe: str,
               ltp: float, st_val: float, gap_pct: float,
               direction: str, threshold: int, prev_gap: float = None):
    """Record alert in persistent history (local + Drive). Best-effort."""
    try:
        from .alert_store import get_alert_store
        store = get_alert_store()
        store.log_alert(symbol, basket, timeframe, ltp, st_val,
                        gap_pct, direction, threshold, prev_gap)
    except Exception as e:
        logger.warning("Failed to log alert to history: %s", e)


# ── Main scan logic ──────────────────────────────────────────────────────

def scan(dry_run: bool = False, symbol_filter: str = None) -> list:
    """Run one scan cycle: compute ST (monthly+weekly), check gaps, send alerts.

    Every symbol is checked against BOTH monthly and weekly ST.
    Returns list of result dicts, one per (symbol, timeframe) pair.
    """
    config = cfg.load_config()
    thresholds = sorted(config.get('alert_thresholds', [5, 3, 1]), reverse=True)
    cooldown = config.get('alert_cooldown_hours', 6)
    symbols = cfg.get_all_symbols(config)

    # Optional single-symbol filter
    if symbol_filter:
        symbols = [s for s in symbols if s['symbol'].upper() == symbol_filter.upper()]
        if not symbols:
            logger.error("Symbol %s not found in config", symbol_filter)
            return []

    if not symbols:
        logger.warning("No symbols configured in st_watch_config.json")
        return []

    logger.info("ST Watch: scanning %d symbols x %d timeframes",
                len(symbols), len(cfg.TIMEFRAMES))

    kite = _get_kite()
    state = _load_state()

    # Step 1: Compute ST for all symbols (both monthly + weekly, data fetched once)
    all_st = compute_all_st(kite, symbols)
    if not all_st:
        logger.error("No ST data computed for any symbol")
        return []

    # Step 2: Fetch LTP for all unique symbols with ST data
    unique_symbols = list({s['symbol'] for s in symbols if any(
        f"{s['symbol']}_{tf}" in all_st for tf in cfg.TIMEFRAMES
    )})
    ltps = _get_ltp(kite, unique_symbols)

    if not ltps:
        logger.error("LTP fetch returned nothing")
        return []

    if len(ltps) < len(unique_symbols):
        missing = set(unique_symbols) - set(ltps.keys())
        logger.warning("LTP missing for %d symbols: %s", len(missing), missing)

    # Step 3: Check gaps and alert (one result per symbol+timeframe)
    results = []
    for sym_info in symbols:
        symbol = sym_info['symbol']
        if symbol not in ltps:
            logger.warning("%s: LTP not available, skipping", symbol)
            continue
        ltp = ltps[symbol]

        for tf in cfg.TIMEFRAMES:
            cache_key = f"{symbol}_{tf}"
            st_entry = all_st.get(cache_key)
            if not st_entry:
                continue

            st_val = st_entry['st']
            direction = st_entry['direction']
            gap_pct = ((ltp - st_val) / st_val) * 100
            abs_gap = abs(gap_pct)

            prev_gap = state.get(cache_key, {}).get('prev_gap')

            result = {
                'symbol': symbol,
                'basket': sym_info['basket'],
                'timeframe': tf,
                'ltp': ltp,
                'st': st_val,
                'direction': direction,
                'gap_pct': round(gap_pct, 2),
                'prev_gap': prev_gap,
                'alerted': False,
            }

            # Reset alerts if gap widened above highest threshold
            if abs_gap > thresholds[0]:
                state[cache_key] = {'prev_gap': round(gap_pct, 2)}
                results.append(result)
                continue

            # Find the tightest threshold to alert at
            alert_at = _find_alert_threshold(
                abs_gap, thresholds, cache_key, state, cooldown
            )

            if alert_at is not None:
                msg = _format_alert(
                    symbol, sym_info['basket'], tf,
                    ltp, st_val, gap_pct, direction, prev_gap
                )
                if dry_run:
                    logger.info("DRY RUN alert:\n%s", msg)
                else:
                    _send_telegram(msg)
                    logger.info("Alert: %s %s gap=%.1f%% (threshold=%d%%)",
                                symbol, tf, gap_pct, alert_at)

                # Log to alert history (both dry-run and live)
                _log_alert(symbol, sym_info['basket'], tf,
                           ltp, st_val, gap_pct, direction, alert_at, prev_gap)

                state[cache_key] = {
                    'last_threshold': alert_at,
                    'last_alert_time': datetime.now(IST).isoformat(),
                    'prev_gap': round(gap_pct, 2),
                }
                result['alerted'] = True
                _save_state(state)  # persist after each alert
            else:
                # Update prev_gap even if not alerting
                if cache_key not in state:
                    state[cache_key] = {}
                state[cache_key]['prev_gap'] = round(gap_pct, 2)

            results.append(result)

    _save_state(state)
    return results


def print_status(results: list = None):
    """Print a formatted status table grouped by symbol."""
    if results is None:
        results = scan(dry_run=True)

    if not results:
        print("No data available. Run 'scan' first.")
        return

    # Sort: closest to ST first
    results.sort(key=lambda r: abs(r['gap_pct']))

    print(f"\n{'='*82}")
    print(f"  ST WATCH -- Core & Tactical Basket Monitor")
    print(f"  {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'='*82}")
    print(f"  {'Symbol':<14} {'Basket':<15} {'TF':<8} {'LTP':>9} {'ST':>9} {'Gap%':>7} {'Dir':>5}")
    print(f"  {'-'*74}")

    for r in results:
        gap = r['gap_pct']
        abs_gap = abs(gap)
        if abs_gap <= 1:
            marker = " <<<<<"
        elif abs_gap <= 3:
            marker = " <<<"
        elif abs_gap <= 5:
            marker = " <"
        else:
            marker = ""

        basket_label = BASKET_LABELS.get(r['basket'], r['basket'])

        print(f"  {r['symbol']:<14} {basket_label:<15} {r['timeframe']:<8} "
              f"{r['ltp']:>9,.2f} {r['st']:>9,.2f} {gap:>+6.1f}% {r['direction']:>5}{marker}")

    # Summary
    unique_count = len({r['symbol'] for r in results})
    within5 = [r for r in results if abs(r['gap_pct']) <= 5]
    within3 = [r for r in results if abs(r['gap_pct']) <= 3]
    touch = [r for r in results if abs(r['gap_pct']) <= 1]

    print(f"\n  Within 5%: {len(within5)} | Within 3%: {len(within3)} | Touch (<1%): {len(touch)}")
    print(f"  Total: {len(results)} checks ({unique_count} symbols x M+W)")
    print()

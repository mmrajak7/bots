"""Magnet scanner — polls Chartink for ST-approach signals, validates, adds to watchlist.

Signal validation rules:
1. Price is within 3% of ST (Chartink pre-filters, we verify)
2. Price is NOT already below 2% gap (too late — already in entry zone, we missed approach)
3. Price hasn't been in <2% territory in last 5 days (bounce, not fresh approach)
4. First-touch: not already watching/entered for this stock+timeframe
5. Stock is in F&O segment
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

from . import config as cfg

logger = logging.getLogger(__name__)

# ── Caches ────────────────────────────────────────────────────────────────
_st_cache: dict = {}               # {(stock, timeframe): {st, direction, atr, computed_at}}
_raw_daily_cache: dict = {}        # {symbol: [daily candles]} — reused by velocity
_instrument_cache: dict = {}       # {symbol: instrument_token}
_instrument_cache_loaded = False

# Daily skip cache — stocks skipped for reasons that won't change intraday
# (not_fresh, velocity_filter, st_computation_failed).
# Key: (stock, timeframe), Value: reason string.  Cleared on new day.
_skip_cache: dict = {}
_skip_cache_date: str = ''

# Data duration per timeframe (years of daily data to fetch)
_DATA_YEARS = {'monthly': 6, 'weekly': 3, 'daily': 1}


# ── Chartink Scraper ──────────────────────────────────────────────────────

def scan_chartink(scan_clause: str) -> List[str]:
    """Scrape Chartink screener API. Returns list of NSE symbols.

    Same pattern as opportunity.py — POST with CSRF token.
    """
    try:
        with requests.Session() as s:
            r = s.get('https://chartink.com/screener/', timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            csrf = soup.select_one("[name='csrf-token']")['content']
            s.headers['x-csrf-token'] = csrf

            r = s.post(cfg.CHARTINK_URL, data={'scan_clause': scan_clause},
                       timeout=15)
            data = r.json().get('data', [])
            symbols = [item['nsecode'] for item in data if 'nsecode' in item]
            return symbols

    except Exception as e:
        logger.error("Chartink scan failed: %s", e)
        return []


def run_all_scanners() -> List[dict]:
    """Run all configured Chartink scanners. Returns list of raw signals.

    Each signal: {'stock': str, 'timeframe': str}
    """
    all_signals = []
    for scanner in cfg.SCANNERS:
        symbols = scan_chartink(scanner['clause'])
        logger.info("Chartink [%s]: %d symbols", scanner['name'], len(symbols))
        for sym in symbols:
            all_signals.append({
                'stock': sym,
                'timeframe': scanner['timeframe'],
            })
        time.sleep(0.5)  # rate limit between scanners
    return all_signals


# ── Kite API Helpers ──────────────────────────────────────────────────────

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


def get_ltp(kite, symbols: List[str]) -> Dict[str, float]:
    """Get LTP for multiple symbols. Returns {symbol: price}."""
    if not symbols:
        return {}
    # Kite LTP accepts "NSE:SYMBOL" format
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


def get_instrument_token(kite, symbol: str) -> int:
    """Get NSE instrument token for historical data fetch. Uses cache."""
    _load_instrument_cache(kite)
    token = _instrument_cache.get(symbol)
    if token is None:
        raise ValueError(f"Instrument token not found for {symbol}")
    return token


# ── Supertrend Computation ────────────────────────────────────────────────

def _aggregate_to_monthly(daily_data: list) -> list:
    """Convert daily OHLC to monthly, excluding current incomplete month."""
    months = defaultdict(list)
    for candle in daily_data:
        dt = candle['date']
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace('+05:30', ''))
        month_key = (dt.year, dt.month)
        months[month_key].append(candle)

    # Exclude current month (incomplete)
    now = datetime.now()
    current_key = (now.year, now.month)

    monthly = []
    for key in sorted(months.keys()):
        if key == current_key:
            continue
        candles = months[key]
        monthly.append({
            'date': candles[0]['date'] if isinstance(candles[0]['date'], str)
                    else candles[0]['date'].isoformat(),
            'open': candles[0]['open'],
            'high': max(c['high'] for c in candles),
            'low': min(c['low'] for c in candles),
            'close': candles[-1]['close'],
            'volume': sum(c.get('volume', 0) for c in candles),
        })
    return monthly


def _aggregate_to_weekly(daily_data: list) -> list:
    """Convert daily OHLC to weekly, excluding current incomplete week."""
    weeks = defaultdict(list)
    for candle in daily_data:
        dt = candle['date']
        if isinstance(dt, str):
            dt = datetime.fromisoformat(dt.replace('+05:30', ''))
        week_key = dt.isocalendar()[:2]
        weeks[week_key].append(candle)

    # Exclude current week
    now = datetime.now()
    current_key = now.isocalendar()[:2]

    weekly = []
    for key in sorted(weeks.keys()):
        if key == current_key:
            continue
        candles = weeks[key]
        weekly.append({
            'date': candles[0]['date'] if isinstance(candles[0]['date'], str)
                    else candles[0]['date'].isoformat(),
            'open': candles[0]['open'],
            'high': max(c['high'] for c in candles),
            'low': min(c['low'] for c in candles),
            'close': candles[-1]['close'],
            'volume': sum(c.get('volume', 0) for c in candles),
        })
    return weekly


def compute_st_for_stock(kite, symbol: str, timeframe: str) -> dict:
    """Compute ST(10,3) for a stock. Uses cache if available.

    Returns: {'st': float, 'direction': 'UP'|'DOWN', 'atr': float}
    """
    cache_key = (symbol, timeframe)

    # Check cache — monthly ST is stable within the month, weekly within the week
    if cache_key in _st_cache:
        cached = _st_cache[cache_key]
        cached_date = cached.get('computed_at', '')[:10]
        today = datetime.now().strftime('%Y-%m-%d')
        if cached_date == today:
            return cached

    # Try backtest_cache first (pre-downloaded data)
    cache_file = cfg.BACKTEST_CACHE / f"{symbol}.json"
    daily_data = None

    # Determine data duration: 6Y monthly, 3Y weekly, 1Y daily
    data_years = _DATA_YEARS.get(timeframe, 6)

    if cache_file.exists():
        try:
            with open(cache_file) as f:
                daily_data = json.load(f)

            # Check cache freshness — reject if last data point is >5 days old
            if daily_data:
                last_date_str = daily_data[-1]['date']
                if not isinstance(last_date_str, str):
                    last_date_str = last_date_str.isoformat()
                last_date = datetime.fromisoformat(last_date_str.replace('+05:30', ''))
                days_stale = (datetime.now() - last_date).days
                if days_stale > 5:
                    logger.debug("%s: cache is %d days stale, fetching fresh data",
                                 symbol, days_stale)
                    daily_data = None  # force fresh fetch
                else:
                    cutoff = (datetime.now() - timedelta(days=365 * data_years)).strftime('%Y-%m-%d')
                    daily_data = [c for c in daily_data
                                  if (c['date'] if isinstance(c['date'], str)
                                      else c['date'].isoformat())[:10] >= cutoff]
        except Exception:
            daily_data = None

    if not daily_data:
        # Fetch from Kite API
        try:
            token = get_instrument_token(kite, symbol)
            now = datetime.now()
            start = now - timedelta(days=365 * data_years)

            if data_years > 5:
                # 6Y: 2 chunks to avoid 2000-day limit
                mid = now - timedelta(days=365 * 3)
                chunk1 = kite.historical_data(
                    token, start.strftime('%Y-%m-%d'),
                    mid.strftime('%Y-%m-%d'), 'day'
                )
                chunk2 = kite.historical_data(
                    token, (mid + timedelta(days=1)).strftime('%Y-%m-%d'),
                    now.strftime('%Y-%m-%d'), 'day'
                )
                daily_data = chunk1 + chunk2
            else:
                # 3Y or 1Y: single chunk (well within 2000-day limit)
                daily_data = kite.historical_data(
                    token, start.strftime('%Y-%m-%d'),
                    now.strftime('%Y-%m-%d'), 'day'
                )

            # Normalize date format
            for c in daily_data:
                if not isinstance(c['date'], str):
                    c['date'] = c['date'].isoformat()

        except Exception as e:
            logger.error("Historical data fetch failed for %s: %s", symbol, e)
            return {}

    # Cache raw daily data for velocity reuse
    _raw_daily_cache[symbol] = daily_data

    # Aggregate to timeframe (daily uses raw candles — no aggregation)
    if timeframe == 'monthly':
        candles = _aggregate_to_monthly(daily_data)
    elif timeframe == 'weekly':
        candles = _aggregate_to_weekly(daily_data)
    else:
        # Daily: use raw daily candles directly, exclude today (incomplete)
        today_str = datetime.now().strftime('%Y-%m-%d')
        candles = [c for c in daily_data
                   if (c['date'] if isinstance(c['date'], str)
                       else c['date'].isoformat())[:10] < today_str]

    if len(candles) < cfg.ST_PERIOD + 1:
        logger.warning("%s: not enough %s candles (%d)", symbol, timeframe, len(candles))
        return {}

    # Compute supertrend
    from playbook.compute_st import compute_supertrend
    st_data = compute_supertrend(candles, cfg.ST_PERIOD, cfg.ST_MULTIPLIER)

    if not st_data:
        return {}

    latest = st_data[-1]
    result = {
        'st': latest['supertrend'],
        'direction': latest['direction'],
        'atr': latest['atr'],
        'computed_at': datetime.now().isoformat(),
    }

    _st_cache[cache_key] = result
    return result


# ── Freshness Check ───────────────────────────────────────────────────────

def check_freshness(symbol: str, st_value: float,
                    timeframe: str) -> Tuple[bool, str]:
    """Check if the signal is a fresh approach, not a bounce from <2% zone.

    Returns: (is_fresh, reason)

    Rules:
    - If price is already below 2% gap → NOT fresh (too late)
    - If price was below 2% gap in the last 5 trading days → NOT fresh (bounce)

    Reuses _raw_daily_cache (populated by compute_st_for_stock) — zero API calls.
    """
    try:
        # Reuse daily data already fetched by compute_st_for_stock()
        daily = _raw_daily_cache.get(symbol)

        if not daily:
            return True, "no recent data, treating as fresh"

        # Check current price (last candle close or LTP)
        current_price = daily[-1]['close']
        current_gap = abs(current_price - st_value) / st_value

        if current_gap < cfg.ENTRY_GAP_MIN:
            return False, f"already at ST line (gap {current_gap:.1%}), too late"

        if current_gap < cfg.ENTRY_GAP:
            return False, f"already in entry zone (gap {current_gap:.1%} < 2%), missed approach"

        # Check last N trading days — was price below 2% gap recently?
        recent_days = daily[-cfg.FRESHNESS_DAYS:]
        for candle in recent_days:
            day_close = candle['close']
            day_low = candle['low']
            # Check if the low or close was within 2% of ST
            close_gap = abs(day_close - st_value) / st_value
            low_gap = abs(day_low - st_value) / st_value
            if close_gap < cfg.ENTRY_GAP or low_gap < cfg.ENTRY_GAP:
                dt = candle['date']
                if not isinstance(dt, str):
                    dt = dt.strftime('%Y-%m-%d')
                else:
                    dt = dt[:10]
                return False, (f"was in <2% zone on {dt} "
                               f"(close gap {close_gap:.1%}, low gap {low_gap:.1%}). "
                               f"Bounce, not fresh approach")

        return True, f"fresh approach, current gap {current_gap:.1%}"

    except Exception as e:
        logger.warning("Freshness check failed for %s: %s. Treating as fresh.", symbol, e)
        return True, f"freshness check error: {e}"


# ── Velocity Filter (Daily signals only) ──────────────────────────────────

def compute_daily_velocity(kite, symbol: str, st_value: float,
                           price: float, side: str) -> dict:
    """Compute approach velocity for daily ST signals.

    Measures how fast the gap is closing over the last 3-5 trading days.
    Returns dict with velocity metrics, or empty dict on failure.

    Backtest-validated filter: vel_3d < -0.5 AND momentum >= 60%
    yields 80% hit rate (touch+flip), 70% win rate, +10% avg PnL.

    Reuses raw daily data from compute_st_for_stock() cache (no extra API call).
    Uses today's ST value for all historical gap computations (approximation —
    daily ST shifts slightly each day, directionally correct for filtering).
    """
    try:
        # Reuse data already fetched by compute_st_for_stock()
        daily = _raw_daily_cache.get(symbol)
        if not daily:
            # Fallback: fetch from Kite (shouldn't happen if ST was computed first)
            token = get_instrument_token(kite, symbol)
            now = datetime.now()
            start = now - timedelta(days=15)
            daily = kite.historical_data(
                token, start.strftime('%Y-%m-%d'),
                now.strftime('%Y-%m-%d'), 'day'
            )

        if not daily or len(daily) < 4:
            return {}

        # Use last 10 entries only (velocity is recent momentum)
        daily = daily[-10:]

        # Compute gap series (last N days)
        gaps = []
        for candle in daily:
            p = candle['close']
            if st_value <= 0:
                continue
            if side == 'above' and p > st_value:
                gaps.append((candle['date'], (p - st_value) / st_value * 100))
            elif side == 'below' and p < st_value:
                gaps.append((candle['date'], (st_value - p) / st_value * 100))
            # else: wrong side of ST — skip day

        if len(gaps) < 2:
            return {}

        # 3-day velocity: gap change over last 3 entries
        lookback = min(3, len(gaps) - 1)
        gap_now = gaps[-1][1]
        gap_ago = gaps[-(lookback + 1)][1]
        velocity_3d = gap_now - gap_ago  # negative = approaching = good

        # 5-day velocity
        lookback_5 = min(5, len(gaps) - 1)
        gap_5d_ago = gaps[-(lookback_5 + 1)][1]
        velocity_5d = gap_now - gap_5d_ago

        # Momentum: % of last 5 days where gap was closing
        check_days = min(5, len(gaps) - 1)
        closing_days = 0
        for i in range(1, check_days + 1):
            if gaps[-i][1] < gaps[-(i + 1)][1]:
                closing_days += 1
        momentum_pct = closing_days / check_days * 100 if check_days > 0 else 0

        # Consecutive closing days
        consec = 0
        for i in range(1, len(gaps)):
            if gaps[-i][1] < gaps[-(i + 1)][1]:
                consec += 1
            else:
                break

        return {
            'velocity_3d': round(velocity_3d, 3),
            'velocity_5d': round(velocity_5d, 3),
            'momentum_pct': round(momentum_pct, 1),
            'consecutive_closing': consec,
            'gap_3d_ago': round(gap_ago, 2),
        }

    except Exception as e:
        logger.warning("Velocity computation failed for %s: %s", symbol, e)
        return {}


def check_daily_velocity(velocity: dict) -> Tuple[bool, str]:
    """Check if daily signal passes velocity filters.

    Backtest: fast approach + close gap + high momentum = 80% hit rate.
    Returns (passes, reason).
    """
    if not velocity:
        return False, "velocity computation failed"

    vel_3d = velocity['velocity_3d']
    momentum = velocity['momentum_pct']

    # Filter 1: 3-day velocity must be negative enough (gap is closing fast)
    vel_threshold = cfg.DAILY_VELOCITY_3D_MAX  # default -0.5
    if vel_3d > vel_threshold:
        return False, (f"velocity too slow ({vel_3d:+.2f}%/3d, "
                       f"need <{vel_threshold})")

    # Filter 2: Momentum — at least 60% of recent days must be closing the gap
    mom_threshold = cfg.DAILY_MOMENTUM_MIN  # default 60
    if momentum < mom_threshold:
        return False, (f"momentum too low ({momentum:.0f}%, "
                       f"need >={mom_threshold}%)")

    return True, (f"velocity OK: vel3d={vel_3d:+.2f}, "
                  f"mom={momentum:.0f}%, "
                  f"consec={velocity['consecutive_closing']}")


# ── Daily Skip Cache ─────────────────────────────────────────────────────
# Caches stocks rejected for reasons that won't change intraday:
#   not_fresh      — bounce in last 5 days (historical, frozen)
#   velocity_filter — daily close momentum (frozen once computed)
# NOT cached: st_computation_failed (transient API glitch), gap checks (price moves)


def _check_skip_cache(stock: str, timeframe: str):
    """Return cached skip reason if stock was already rejected today, else None."""
    global _skip_cache, _skip_cache_date
    today = datetime.now().strftime('%Y-%m-%d')
    if _skip_cache_date != today:
        _skip_cache = {}
        _skip_cache_date = today
    return _skip_cache.get((stock, timeframe))


def _add_to_skip_cache(stock: str, timeframe: str, reason: str):
    """Cache a skip decision for the rest of the day."""
    global _skip_cache, _skip_cache_date
    today = datetime.now().strftime('%Y-%m-%d')
    if _skip_cache_date != today:
        _skip_cache = {}
        _skip_cache_date = today
    _skip_cache[(stock, timeframe)] = reason


# ── Main Scan Pipeline ────────────────────────────────────────────────────

def validate_and_add_signals(store, kite=None, dry_run: bool = False) -> List[dict]:
    """Full scan pipeline: Chartink → validate → add to store.

    Returns list of newly added signals.
    """
    if kite is None:
        kite = _get_kite()

    # Step 1: Scan Chartink
    raw_signals = run_all_scanners()
    if not raw_signals:
        logger.info("No signals from Chartink")
        return []

    logger.info("Raw signals: %d", len(raw_signals))

    # Max open trades cap
    open_count = len(store.get_open())
    if open_count >= cfg.MAX_OPEN_TRADES:
        logger.info("Max open trades reached (%d/%d), skipping scan",
                     open_count, cfg.MAX_OPEN_TRADES)
        return []

    # Dedup raw signals (Chartink may return same stock in both scanners)
    seen = set()
    unique_signals = []
    for sig in raw_signals:
        key = (sig['stock'], sig['timeframe'])
        if key not in seen:
            seen.add(key)
            unique_signals.append(sig)

    # Step 2: Get LTP for all unique stocks
    unique_stocks = list({s['stock'] for s in unique_signals})
    ltps = get_ltp(kite, unique_stocks)

    added = []
    skipped_reasons = defaultdict(int)

    for sig in unique_signals:
        stock = sig['stock']
        timeframe = sig['timeframe']

        # Already watching/entered/cancelled-today for this stock?
        today_str = datetime.now().strftime('%Y-%m-%d')
        existing = [t for t in store._trades
                    if t['stock'] == stock
                    and (t['status'] in ('watching', 'entered')
                         or (t['status'] == 'cancelled' and t.get('exit_date') == today_str))]
        if existing:
            skipped_reasons['already_active'] += 1
            continue

        # Have LTP?
        price = ltps.get(stock)
        if not price:
            skipped_reasons['no_ltp'] += 1
            continue

        # Daily skip cache — avoid re-running expensive checks for stocks
        # already rejected today for reasons that won't change intraday
        cached_skip = _check_skip_cache(stock, timeframe)
        if cached_skip:
            skipped_reasons[cached_skip] += 1
            continue

        # Compute ST (not cached in skip_cache — failures could be transient API glitches)
        st_info = compute_st_for_stock(kite, stock, timeframe)
        if not st_info:
            skipped_reasons['st_computation_failed'] += 1
            continue

        st_val = st_info['st']
        gap = abs(price - st_val) / st_val

        # Signal acceptance range: gap must be between ENTRY_GAP (2%) and max_gap (3%)
        # For daily: same 3% range — velocity filter is the daily-specific quality gate
        # Lifecycle: signal at 2-3% (watching) → gap shrinks to <2% → monitor enters
        max_gap = cfg.DAILY_GAP_MAX if timeframe == 'daily' else cfg.SIGNAL_GAP_MAX

        # Verify gap is in valid range
        if gap > max_gap:
            skipped_reasons['gap_too_wide'] += 1
            continue

        if gap < cfg.ENTRY_GAP:
            skipped_reasons['already_past_entry'] += 1
            logger.info("SKIP %s (%s): already at %.1f%% gap, past 2%% entry zone",
                        stock, timeframe, gap * 100)
            continue

        # Freshness check
        is_fresh, reason = check_freshness(stock, st_val, timeframe)
        if not is_fresh:
            skipped_reasons['not_fresh'] += 1
            _add_to_skip_cache(stock, timeframe, 'not_fresh')
            logger.info("SKIP %s (%s): %s [cached for today]", stock, timeframe, reason)
            continue

        # Daily signals: velocity filter (backtest-validated, 80% hit rate)
        velocity = {}  # initialized for all timeframes; populated only for daily
        if timeframe == 'daily':
            side = 'above' if price > st_val else 'below'
            velocity = compute_daily_velocity(kite, stock, st_val, price, side)
            vel_ok, vel_reason = check_daily_velocity(velocity)
            if not vel_ok:
                skipped_reasons['velocity_filter'] += 1
                _add_to_skip_cache(stock, timeframe, 'velocity_filter')
                logger.info("SKIP %s (daily): %s [cached for today]", stock, vel_reason)
                continue
            logger.info("PASS %s (daily): %s", stock, vel_reason)

        # All checks passed — add signal
        if dry_run:
            vel_str = ""
            if timeframe == 'daily' and velocity:
                vel_str = (f" vel3d={velocity['velocity_3d']:+.2f} "
                           f"mom={velocity['momentum_pct']:.0f}%")
            logger.info("DRY RUN — would add: %s (%s) gap=%.1f%% ST=%.2f%s",
                        stock, timeframe, gap * 100, st_val, vel_str)
            added.append({'stock': stock, 'timeframe': timeframe,
                          'gap': gap, 'st': st_val, 'dry_run': True})
            continue

        # Build notes — include velocity for daily
        notes = f"Chartink {timeframe} scan, gap={gap:.1%}, ST dir={st_info['direction']}"
        if timeframe == 'daily' and velocity:
            notes += (f", vel3d={velocity['velocity_3d']:+.2f}, "
                      f"mom={velocity['momentum_pct']:.0f}%, "
                      f"consec={velocity['consecutive_closing']}")

        trade = store.add_signal({
            'stock': stock,
            'timeframe': timeframe,
            'st_value': st_val,
            'st_direction': st_info['direction'],
            'signal_price': price,
            'notes': notes,
        })
        added.append(trade)
        logger.info("ADDED #%d: %s (%s) gap=%.1f%% ST=%.2f dir=%s -> %s",
                     trade['id'], stock, timeframe, gap * 100, st_val,
                     st_info['direction'], trade['direction'])

    # Summary
    logger.info(
        "Scan complete: %d raw, %d unique, %d added, skipped: %s (skip_cache: %d entries)",
        len(raw_signals), len(unique_signals), len(added),
        dict(skipped_reasons) if skipped_reasons else 'none',
        len(_skip_cache),
    )
    return added

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
from pathlib import Path
from typing import Dict, List, Tuple

import requests
from bs4 import BeautifulSoup

from . import config as cfg

logger = logging.getLogger(__name__)

# ── Caches ────────────────────────────────────────────────────────────────
_st_cache: dict = {}               # {(stock, timeframe): {st, direction, atr, computed_at}}
_instrument_cache: dict = {}       # {symbol: instrument_token}
_instrument_cache_loaded = False


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
            'volume': sum(c['volume'] for c in candles),
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
            'volume': sum(c['volume'] for c in candles),
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
                    # Filter to last 6 years
                    cutoff = (datetime.now() - timedelta(days=365 * 6)).strftime('%Y-%m-%d')
                    daily_data = [c for c in daily_data
                                  if (c['date'] if isinstance(c['date'], str)
                                      else c['date'].isoformat())[:10] >= cutoff]
        except Exception:
            daily_data = None

    if not daily_data:
        # Fetch from Kite API (2 chunks to avoid 2000-day limit)
        try:
            token = get_instrument_token(kite, symbol)
            now = datetime.now()
            mid = now - timedelta(days=365 * 3)
            start = now - timedelta(days=365 * 6)

            chunk1 = kite.historical_data(
                token, start.strftime('%Y-%m-%d'),
                mid.strftime('%Y-%m-%d'), 'day'
            )
            chunk2 = kite.historical_data(
                token, (mid + timedelta(days=1)).strftime('%Y-%m-%d'),
                now.strftime('%Y-%m-%d'), 'day'
            )
            daily_data = chunk1 + chunk2

            # Normalize date format
            for c in daily_data:
                if not isinstance(c['date'], str):
                    c['date'] = c['date'].isoformat()

        except Exception as e:
            logger.error("Historical data fetch failed for %s: %s", symbol, e)
            return {}

    # Aggregate to timeframe
    if timeframe == 'monthly':
        candles = _aggregate_to_monthly(daily_data)
    else:
        candles = _aggregate_to_weekly(daily_data)

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

def check_freshness(kite, symbol: str, st_value: float,
                    timeframe: str) -> Tuple[bool, str]:
    """Check if the signal is a fresh approach, not a bounce from <2% zone.

    Returns: (is_fresh, reason)

    Rules:
    - If price is already below 2% gap → NOT fresh (too late)
    - If price was below 2% gap in the last 5 trading days → NOT fresh (bounce)
    """
    try:
        # Get last 10 days of daily data for freshness check
        token = get_instrument_token(kite, symbol)
        now = datetime.now()
        start = now - timedelta(days=15)  # 15 calendar days ≈ 10 trading days

        daily = kite.historical_data(
            token, start.strftime('%Y-%m-%d'),
            now.strftime('%Y-%m-%d'), 'day'
        )

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

        # Already watching/entered?
        existing = [t for t in store.get_open()
                    if t['stock'] == stock and t['timeframe'] == timeframe]
        if existing:
            skipped_reasons['already_active'] += 1
            continue

        # Have LTP?
        price = ltps.get(stock)
        if not price:
            skipped_reasons['no_ltp'] += 1
            continue

        # Compute ST
        st_info = compute_st_for_stock(kite, stock, timeframe)
        if not st_info:
            skipped_reasons['st_computation_failed'] += 1
            continue

        st_val = st_info['st']
        gap = abs(price - st_val) / st_val

        # Verify gap is in valid range (2% to 3%)
        if gap > cfg.SIGNAL_GAP_MAX:
            skipped_reasons['gap_too_wide'] += 1
            continue

        if gap < cfg.ENTRY_GAP:
            skipped_reasons['already_past_entry'] += 1
            logger.info("SKIP %s (%s): already at %.1f%% gap, past 2%% entry zone",
                        stock, timeframe, gap * 100)
            continue

        # Freshness check
        is_fresh, reason = check_freshness(kite, stock, st_val, timeframe)
        if not is_fresh:
            skipped_reasons['not_fresh'] += 1
            logger.info("SKIP %s (%s): %s", stock, timeframe, reason)
            continue

        # All checks passed — add signal
        if dry_run:
            logger.info("DRY RUN — would add: %s (%s) gap=%.1f%% ST=%.2f",
                        stock, timeframe, gap * 100, st_val)
            added.append({'stock': stock, 'timeframe': timeframe,
                          'gap': gap, 'st': st_val, 'dry_run': True})
            continue

        trade = store.add_signal({
            'stock': stock,
            'timeframe': timeframe,
            'st_value': st_val,
            'st_direction': st_info['direction'],
            'signal_price': price,
            'notes': f"Chartink {timeframe} scan, gap={gap:.1%}, "
                     f"ST direction={st_info['direction']}",
        })
        added.append(trade)
        logger.info("ADDED #%d: %s (%s) gap=%.1f%% ST=%.2f dir=%s -> %s",
                     trade['id'], stock, timeframe, gap * 100, st_val,
                     st_info['direction'], trade['direction'])

    # Summary
    logger.info(
        "Scan complete: %d raw, %d unique, %d added, skipped: %s",
        len(raw_signals), len(unique_signals), len(added),
        dict(skipped_reasons) if skipped_reasons else 'none'
    )
    return added

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
import os
import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import requests
from bs4 import BeautifulSoup

from . import config as cfg

try:
    from common import kite_errors
except Exception:                       # pragma: no cover - import safety
    kite_errors = None

logger = logging.getLogger(__name__)

# Kite's published per-second limits (kite.trade/docs/connect/v3/exceptions):
#   quote family (/quote, /quote/ltp, /quote/ohlc) — 1 req/s, batch 500/1000
#   historical candles                             — 3 req/s
# A 429 opens a 10-SECOND SLIDING COOLDOWN, and every request made during that
# window EXTENDS it. So a retry loop with no backoff does not merely fail, it
# lengthens the outage — which is why the sleeps below are not optional
# politeness.
# Backing off from a 429 lives in ONE place: `zebra.monitor._spot_ltps`. Only
# that caller knows the fetch is exit-critical and therefore worth 10 seconds;
# every other caller here is discretionary and must fail fast so it cannot
# queue ahead of the one that matters. A second retry loop in this module
# would be the 'copy you did not open' shape, on the path where it costs most.

# Historical candles are capped at 3 req/s. The scanner issues them in a tight
# loop over ~48 candidates with no pacing at all, which is how 48 of these
# failed with `Too many requests` on 2026-08-27. Pacing is cheap here — a scan
# is discretionary and runs after exit monitoring — and it also makes the
# ONE-TIME cache warm-up below safe: the first cycle of the day now fetches a
# full 6Y history per symbol, which would otherwise be the worst burst of all.
_HISTORICAL_MIN_INTERVAL_SEC = 1.0 / 3.0
_last_historical_at = 0.0


def _historical_throttle() -> None:
    """Block just long enough to stay inside Kite's 3 req/s historical cap."""
    global _last_historical_at
    wait = _last_historical_at + _HISTORICAL_MIN_INTERVAL_SEC - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_historical_at = time.monotonic()


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

    Tries ``aggregatedStockList`` first (from backtest/process) for correct
    F&O-filtered results.  Falls back to ``data`` key (screener/process
    format) if aggregatedStockList is absent.
    """
    try:
        with requests.Session() as s:
            r = s.get('https://chartink.com/screener/', timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            csrf_tag = soup.select_one("[name='csrf-token']")
            if not csrf_tag:
                logger.error("Chartink CSRF token not found")
                return []
            s.headers['x-csrf-token'] = csrf_tag['content']

            r = s.post(cfg.CHARTINK_URL, data={'scan_clause': scan_clause},
                       timeout=15)
            resp = r.json()

            # Try aggregatedStockList first (backtest/process endpoint)
            agg = resp.get('aggregatedStockList', [])
            if agg and isinstance(agg, list):
                if isinstance(agg[0], list):
                    flat = agg[0]
                elif isinstance(agg[0], str):
                    flat = agg
                else:
                    flat = None

                if flat and isinstance(flat[0], str):
                    if len(flat) % 3 != 0:
                        logger.warning("aggregatedStockList len %d not /3", len(flat))
                    symbols = [flat[i] for i in range(0, len(flat) - 2, 3)
                               if flat[i] and isinstance(flat[i], str)]
                    if symbols:
                        return symbols

            # Fallback to data key (works with screener/process endpoint)
            data = resp.get('data', [])
            if data and isinstance(data[0], dict):
                return [item['nsecode'] for item in data
                        if isinstance(item, dict) and 'nsecode' in item]

            logger.error("Chartink: no usable data in response (keys: %s)",
                         list(resp.keys()))
            return []

    except Exception as e:
        logger.error("Chartink scan failed: %s", e)
        return []


# Known Chartink → NSE symbol mismatches.
# Add entries here when the warning log reports unrecognized symbols.
_CHARTINK_TO_NSE = {
    'M_M': 'M&M',
    'M_MFIN': 'M&MFIN',
    'L_TFH': 'L&TFH',
    'NAM_INDIA': 'NAM-INDIA',
}


def _normalize_symbol(sym: str) -> str:
    """Fix Chartink symbol mismatches with NSE/Kite naming."""
    return _CHARTINK_TO_NSE.get(sym, sym)


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
                'stock': _normalize_symbol(sym),
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


# Chartink hands back the INDEX alongside its constituents. These are not
# equities, will never appear in the NSE instrument list, and there is no
# mapping to add — so they are noise, not warnings. Measured in the real log:
# NIFTY 4,472 + CNXMIDCAP 3,386 + BANKNIFTY 1,921 = 9,779 of 9,928 WARNING
# lines, which is how a warning channel becomes unreadable.
_KNOWN_NON_EQUITY = frozenset({'NIFTY', 'BANKNIFTY', 'CNXMIDCAP', 'FINNIFTY',
                               'MIDCPNIFTY', 'NIFTYNXT50', 'SENSEX'})


# The cause of the most recent `get_ltp` failure, or None. Read it with
# `last_ltp_error()` and clear it with `clear_ltp_error()` immediately BEFORE
# the call you intend to interrogate — a stale cause is worse than none, and
# `get_ltp` is monkeypatched in tests, so the slot cannot rely on being reset
# by the call itself.
#
# It exists because `get_ltp` has to keep returning a bare dict: it is the
# seam the whole test suite substitutes, and every caller but one is happy to
# treat "no price" as "no price". The ONE caller that must know why — the spot
# fetch for open positions in `zebra.monitor.check_entered` — reads this.
_last_ltp_error: Optional[Exception] = None


def last_ltp_error() -> Optional[Exception]:
    """The exception that stopped the last real `get_ltp`, or None."""
    return _last_ltp_error


def clear_ltp_error() -> None:
    """Forget the last failure. Call BEFORE a fetch you will interrogate."""
    global _last_ltp_error
    _last_ltp_error = None


def get_ltp(kite, symbols: List[str]) -> Dict[str, float]:
    """Get LTP for multiple symbols. Returns {symbol: price}.

    Wrapper over `get_ltp_ex`, which also returns WHY the call failed. The
    cause is additionally stashed in `last_ltp_error()`, because swallowing it
    here is what let `zebra.monitor` tell the owner his access token had
    expired when the real answer, one line above in the same log, was
    `Too many requests`.
    """
    global _last_ltp_error
    out, err = get_ltp_ex(kite, symbols)
    _last_ltp_error = err
    return out


def get_ltp_ex(kite, symbols: List[str]
               ) -> Tuple[Dict[str, float], Optional[Exception]]:
    """Get LTP for multiple symbols. Returns ``({symbol: price}, error)``.

    `error` is the exception that stopped the fetch, or None on success. It is
    RETURNED rather than logged-and-dropped so the caller can say what actually
    happened instead of guessing — see `common.kite_errors`.

    It does NOT retry. Backing off from a 429 is the caller's judgement call
    and lives in `zebra.monitor._spot_ltps`, the one caller whose failure stops
    exit monitoring.

    Validates symbols against Kite instrument cache first to avoid
    silent failures from Chartink symbols that don't match NSE exactly.
    Symbols not in NSE get price=0.0 so caller can distinguish from
    "valid but suspended" (which just won't appear in the result).
    """
    if not symbols:
        return {}, None
    # Ensure instrument cache is loaded so we can validate
    _load_instrument_cache(kite)

    # Split into valid/invalid NSE symbols
    valid = []
    result = {}
    unmapped = []
    for s in symbols:
        if s in _instrument_cache:
            valid.append(s)
        elif s in _KNOWN_NON_EQUITY:
            # Chartink returns the INDEX alongside its constituents. These are
            # not equities, will never be in the NSE instrument list, and there
            # is nothing to fix — but warning about them twice a cycle produced
            # 7,858 of the 9,928 WARNING lines in the real log, i.e. a warning
            # channel that is 79% noise is a warning channel nobody reads.
            logger.debug("SKIP %s: index symbol, not an equity", s)
        else:
            unmapped.append(s)
        if s not in _instrument_cache:
            result[s] = 0.0  # sentinel: distinguishes "bad symbol" from "no LTP"

    if unmapped:
        # ONE aggregated line for the symbols that genuinely need a mapping.
        # This is the actionable half of what used to be 9,928 warnings.
        logger.warning("SKIP %d symbol(s) not in the NSE instrument list "
                       "(Chartink mismatch — add to _CHARTINK_TO_NSE?): %s",
                       len(unmapped), ', '.join(sorted(unmapped)[:20]))

    if not valid:
        return result, None

    # Kite LTP accepts "NSE:SYMBOL" format. /quote/ltp batches up to 1000
    # instruments in ONE request, so this is a single call however long `valid`
    # gets — the per-second budget is spent on the number of CALLS, not names.
    instruments = [f"NSE:{s}" for s in valid]
    try:
        data = kite.ltp(instruments)
        for key, val in data.items():
            sym = key.replace('NSE:', '')
            result[sym] = val['last_price']
        return result, None
    except Exception as e:
        # Name the CAUSE in the log line, not just the message. This log was
        # the only record of the 2026-08-27 outage and it read
        # "LTP fetch failed: Too many requests" — accurate, while the alert
        # built on top of it said "token expired".
        cause = kite_errors.classify(e) if kite_errors else 'unclassified'
        logger.error("LTP fetch failed [%s]: %s", cause, e)
        return result, e


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


def _write_daily_cache(cache_file, symbol: str, daily_data: list) -> None:
    """Persist freshly fetched daily candles so the next cycle costs nothing.

    THE fix for the 2026-08-27 historical-quota burn. `compute_st_for_stock`
    has always READ `backtest_cache/<SYM>.json` and never written it, and the
    in-memory `_st_cache` is defeated by the cron process exiting between
    5-minute cycles. So every stale symbol re-fetched its whole history on
    EVERY cycle — ~48 symbols x 2 chunks against Kite's 3 req/s historical
    limit, in a ~20-second burst. The files on this box were last written
    2026-07-23, i.e. 35 days stale, which is past the reader's own 5-day
    freshness bar: every symbol, every cycle.

    TODAY'S BAR IS EXCLUDED. It is incomplete until the close, and this
    directory is shared with research and backtest code
    (`playbook/st_watch/regime_monitor.py` reads it) — writing a partial
    session into it would put a half-formed candle into a backtest. The
    caller's own aggregation already drops the current month/week/day, so
    nothing downstream loses anything.

    Best-effort and silent-on-failure by design: a cache write is an
    optimisation, and a read-only or full disk must never stop a scan.
    """
    try:
        today = datetime.now().strftime('%Y-%m-%d')
        complete = [c for c in daily_data if str(c['date'])[:10] < today]
        if len(complete) < cfg.ST_PERIOD + 1:
            return                       # not worth caching, and never trust it
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(complete, f)
        os.replace(str(tmp), str(cache_file))   # atomic: readers never see half
        logger.debug("Cached %d daily candles for %s", len(complete), symbol)
    except Exception as e:
        logger.debug("Could not cache daily data for %s: %s", symbol, e)


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
        # Fetch from Kite API.
        #
        # ALWAYS the full 6Y span, whatever this timeframe needs, so the
        # result can be written back to the shared cache below and reused by
        # every timeframe. A 3Y file read later by the MONTHLY path would
        # compute ST(10,3) off 3 years of monthly candles and silently
        # disagree with the 6Y answer — the exact error `feedback_st_computation`
        # exists to prevent. Costing one extra call on the first miss of the
        # day buys ~76 cycles of zero calls for that symbol.
        try:
            token = get_instrument_token(kite, symbol)
            now = datetime.now()
            fetch_years = max(_DATA_YEARS.values())
            start = now - timedelta(days=365 * fetch_years)

            # 2 chunks to stay inside Kite's 2000-day per-request cap on
            # daily candles.
            mid = now - timedelta(days=365 * 3)
            _historical_throttle()
            chunk1 = kite.historical_data(
                token, start.strftime('%Y-%m-%d'),
                mid.strftime('%Y-%m-%d'), 'day'
            )
            _historical_throttle()
            chunk2 = kite.historical_data(
                token, (mid + timedelta(days=1)).strftime('%Y-%m-%d'),
                now.strftime('%Y-%m-%d'), 'day'
            )
            daily_data = chunk1 + chunk2

            # Normalize date format
            for c in daily_data:
                if not isinstance(c['date'], str):
                    c['date'] = c['date'].isoformat()

            _write_daily_cache(cache_file, symbol, daily_data)

            # Now trim to what THIS timeframe asked for, so the ST computed
            # below is unchanged by the wider fetch.
            cutoff = (now - timedelta(days=365 * data_years)).strftime('%Y-%m-%d')
            daily_data = [c for c in daily_data if c['date'][:10] >= cutoff]

        except Exception as e:
            # Name the CAUSE. 48 of these lines on 2026-08-27 all said
            # "Too many requests" and nothing anywhere joined them up to the
            # blind-monitoring alert three hours later.
            cause = kite_errors.classify(e) if kite_errors else 'unclassified'
            logger.error("Historical data fetch failed for %s [%s]: %s",
                         symbol, cause, e)
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
                    timeframe: str,
                    entry_gap: Optional[float] = None,
                    entry_gap_min: Optional[float] = None,
                    freshness_days: Optional[int] = None) -> Tuple[bool, str]:
    """Check if the signal is a fresh approach, not a bounce from an ST touch.

    Returns: (is_fresh, reason)

    Rules:
    - If price is already in entry zone → too late
    - If price TOUCHED ST (gap < TOUCHED_THRESHOLD) in last N days → NOT fresh
      (we already played this move; pullback is not a new setup)

    Natural approaches that dip through 3-4% are NOT rejected — only actual
    near-ST touches (gap < 1%).

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

        # Thresholds are ARGUMENTS with magnet's config as the default, so a
        # caller can own its own band. zebra reuses this function and therefore
        # silently inherited magnet's numbers: it advertises a [3%, 5%] watch
        # band in zebra_config.json, but `cfg.ENTRY_GAP` here is 4%, so the
        # whole 3-4% band was rejected — and labelled `not_fresh`, which reads
        # as ordinary freshness filtering rather than a band clip. Editing
        # magnet_config.json, for a bot retired in May, moved zebra's LIVE
        # entry band. zebra now passes its own values explicitly.
        gap_min = cfg.ENTRY_GAP_MIN if entry_gap_min is None else entry_gap_min
        gap_floor = cfg.ENTRY_GAP if entry_gap is None else entry_gap
        days = cfg.FRESHNESS_DAYS if freshness_days is None else freshness_days

        if current_gap < gap_min:
            return False, f"already at ST line (gap {current_gap:.1%}), too late"

        if current_gap < gap_floor:
            return False, (f"already in entry zone "
                           f"(gap {current_gap:.1%} < {gap_floor:.1%}), "
                           f"missed approach")

        # Check last N trading days — did price TOUCH ST (gap < 1%)?
        # Only blocks genuine touches, not normal 3-4% dips in an approach.
        recent_days = daily[-days:]
        for candle in recent_days:
            day_close = candle['close']
            day_low = candle['low']
            day_high = candle['high']
            # Check if any intraday point came within TOUCHED_THRESHOLD of ST
            close_gap = abs(day_close - st_value) / st_value
            low_gap = abs(day_low - st_value) / st_value
            high_gap = abs(day_high - st_value) / st_value
            min_gap = min(close_gap, low_gap, high_gap)
            if min_gap < cfg.TOUCHED_THRESHOLD:
                dt = candle['date']
                if not isinstance(dt, str):
                    dt = dt.strftime('%Y-%m-%d')
                else:
                    dt = dt[:10]
                return False, (f"touched ST on {dt} "
                               f"(min gap {min_gap:.2%} < {cfg.TOUCHED_THRESHOLD:.1%}). "
                               f"Pullback from touch, not fresh approach")

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
            _historical_throttle()
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


# ── Watching Telegram (dedicated channel, from magnet_config.json) ────────

_watching_tg_cfg = None
_watching_tg_loaded = False


_SILENCED = True  # 2026-05-11: magnet deprecated, replaced by zebra package.


def _send_watching_alert(msg: str):
    """Send to the dedicated Watching channel. Best-effort.

    Bot token loaded from magnet_config.json 'telegram_watching' section.
    """
    if _SILENCED:
        return
    global _watching_tg_cfg, _watching_tg_loaded
    try:
        if not _watching_tg_loaded:
            import json as _json
            if cfg.CONFIG_FILE.exists():
                with open(cfg.CONFIG_FILE) as f:
                    file_cfg = _json.load(f)
                tg = file_cfg.get('telegram_watching', {})
                # Kill-switch: silence WATCHING alerts entirely
                if tg.get('enabled') is False:
                    _watching_tg_cfg = None
                elif tg.get('bot_token') and tg.get('chat_id'):
                    _watching_tg_cfg = tg
            _watching_tg_loaded = True

        if not _watching_tg_cfg:
            return  # silenced or not configured

        resp = requests.post(
            f"https://api.telegram.org/bot{_watching_tg_cfg['bot_token']}/sendMessage",
            json={'chat_id': _watching_tg_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
        bot_id = _watching_tg_cfg['bot_token'].split(':')[0]
        if resp.ok:
            logger.info("Watching alert sent (bot=%s): %s", bot_id, msg[:120])
        else:
            logger.warning("Watching alert FAILED (bot=%s) %d: %s",
                           bot_id, resp.status_code, resp.text)
    except Exception as e:
        logger.warning("Watching alert failed: %s", e)


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

    # Log capacity (scanner always discovers signals; monitor gates actual entries)
    entered_count = len(store.get_entered())
    if entered_count >= cfg.MAX_OPEN_TRADES:
        logger.info("At entry capacity (%d/%d) — scanning for watching signals only",
                     entered_count, cfg.MAX_OPEN_TRADES)

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

        # Dedup:
        # - currently watching/entered for this stock (any TF)
        # - exited or cancelled TODAY for this stock (any TF)
        # - EXITED within last FRESHNESS_DAYS for same stock+TF (post-TP pullback guard)
        today = datetime.now()
        today_str = today.strftime('%Y-%m-%d')
        cooldown_cutoff = (today - timedelta(days=cfg.FRESHNESS_DAYS)).strftime('%Y-%m-%d')
        existing = [t for t in store._trades
                    if t['stock'] == stock
                    and (
                        t['status'] in ('watching', 'entered')
                        or (t['status'] in ('cancelled', 'exited')
                            and t.get('exit_date') == today_str)
                        or (t['status'] == 'exited'
                            and t.get('timeframe') == timeframe
                            and t.get('exit_date', '') >= cooldown_cutoff)
                    )]
        if existing:
            # Log the cooldown reason distinctly so we can count post-TP blocks
            recent_tf_exit = [t for t in existing
                              if t['status'] == 'exited'
                              and t.get('timeframe') == timeframe
                              and t.get('exit_date', '') >= cooldown_cutoff
                              and t.get('exit_date') != today_str]
            if recent_tf_exit:
                skipped_reasons['post_exit_cooldown'] += 1
                logger.info("SKIP %s (%s): recent %s exit on %s (cooldown %dd)",
                            stock, timeframe, recent_tf_exit[0].get('exit_reason', 'exit'),
                            recent_tf_exit[0].get('exit_date', '?'), cfg.FRESHNESS_DAYS)
            else:
                skipped_reasons['already_active'] += 1
            continue

        # Have LTP?  0.0 = bad symbol (already logged in get_ltp), None = Kite didn't return
        price = ltps.get(stock)
        if not price:
            if price is None:
                logger.warning("SKIP %s (%s): valid NSE symbol but Kite returned no LTP (suspended?)", stock, timeframe)
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

        # Two-stage acceptance (addresses Chartink 5-10 min delivery lag):
        #   1. CHARTINK_GAP_MAX (8%): drop candidates that are already too far
        #      even in Chartink's wide net (e.g. daily spikes / stale screener hits)
        #   2. SIGNAL_GAP_MAX  (5%): only SAVE signals once Kite-LTP confirms
        #      they're inside the WATCH band. Surfaced 5-8% are logged and re-checked
        #      on the next scan (5 min later) as they approach.
        #   3. ENTRY_GAP      (4%): if already inside entry zone, too late
        #      (scanner doesn't create signals here — only the monitor enters)
        max_gap = cfg.DAILY_GAP_MAX if timeframe == 'daily' else cfg.SIGNAL_GAP_MAX
        chartink_ceiling = cfg.CHARTINK_GAP_MAX  # 8% outer gate

        if gap > chartink_ceiling:
            logger.info("SKIP %s (%s): gap %.1f%% > Chartink gate %.1f%%, stale/noisy",
                        stock, timeframe, gap * 100, chartink_ceiling * 100)
            skipped_reasons['gap_beyond_chartink'] += 1
            continue

        # Approach band (watch_max to chartink_max): surface, don't save yet.
        # DEBUG-level log because these fire in bulk (30-50 per scan).
        # Count is still surfaced in the scan summary.
        if gap > max_gap:
            logger.debug("APPROACHING %s (%s): gap %.1f%% in surface band %.1f-%.1f%%",
                         stock, timeframe, gap * 100,
                         max_gap * 100, chartink_ceiling * 100)
            skipped_reasons['approaching_watch_band'] += 1
            continue

        if gap < cfg.ENTRY_GAP:
            skipped_reasons['already_past_entry'] += 1
            logger.info("SKIP %s (%s): already at %.1f%% gap, past %.1f%% entry zone",
                        stock, timeframe, gap * 100, cfg.ENTRY_GAP * 100)
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

        # Telegram WATCHING alert suppressed here — confidence_tracker sends
        # its own WATCHING alert with 15M ST info when monitor delegates.
        # Keeping scanner silent avoids duplicate messages.

    # Summary
    logger.info(
        "Scan complete: %d raw, %d unique, %d added, skipped: %s (skip_cache: %d entries)",
        len(raw_signals), len(unique_signals), len(added),
        dict(skipped_reasons) if skipped_reasons else 'none',
        len(_skip_cache),
    )
    return added

"""Regime monitor — tracks PAUSE/UNPAUSE state for Active basket.

Signals (all based on daily closes):
  1. NIFTY RSI(14) > 50
  2. Market breadth > 40% (F&O stocks above their 50DMA)
  3. NIFTY close > its 50DMA

State machine:
  UNPAUSED -> PAUSED:   Instant when all 3 signals OFF (sig_count == 0)
  PAUSED   -> UNPAUSED: 2-of-3 ON for 7 CONSECUTIVE trading days

Alerts (Telegram):
  - PAUSE transition (instant)
  - UNPAUSE transition (day 7 sustained)
  - Count progress (daily during 7-day count, optional)
  - Count reset (signal dropped during count, optional)

Data sources:
  - NIFTY daily OHLC: Kite API (100 days for RSI + SMA)
  - Breadth: backtest_cache/*.json (800 F&O stocks, local computation)
  - State: logs/regime_state.json (persistent across runs)
"""

import json
import logging
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import pytz

from . import config as cfg

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')


# ── Computation helpers (pure functions, no I/O) ──────────────────────────


def compute_rsi(closes: list, period: int = 14) -> float:
    """Compute latest RSI from a list of close prices. Needs >= period+1 values."""
    if len(closes) < period + 1:
        return 0.0
    # Seed: average gain/loss over first 'period' changes
    gains = []
    losses = []
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        gains.append(max(ch, 0))
        losses.append(abs(min(ch, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    # Wilder smoothing for remaining data
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(ch, 0)) / period
        avg_loss = (avg_loss * (period - 1) + abs(min(ch, 0))) / period
    if avg_loss < 1e-10:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def compute_sma(closes: list, period: int = 50) -> float:
    """Compute latest SMA from a list of close prices. Needs >= period values."""
    if len(closes) < period:
        return 0.0
    return round(sum(closes[-period:]) / period, 2)


def compute_breadth(cache_dir: Path, target_date: date = None,
                    sma_period: int = 50) -> float:
    """Compute market breadth: % of F&O stocks with close > their 50DMA.

    Reads from backtest_cache/*.json (one file per stock, daily OHLC).
    Uses the latest available date <= target_date.
    Returns breadth as a percentage (0-100), or -1 if insufficient data.
    """
    if target_date is None:
        target_date = date.today()

    if not cache_dir.exists():
        logger.debug("Breadth cache dir not found: %s (server mode — use Chartink)", cache_dir)
        return -1.0

    stock_files = [f for f in os.listdir(cache_dir)
                   if f.endswith('.json') and not f.startswith('_')]

    above = 0
    total = 0

    for sf in stock_files:
        try:
            with open(cache_dir / sf) as f:
                raw = json.load(f)
        except Exception:
            continue

        # Parse and sort by date
        closes_by_date = []
        for c in raw:
            d = c['date']
            if isinstance(d, str):
                d = datetime.fromisoformat(d.replace('+05:30', '')).date()
            if d <= target_date:
                closes_by_date.append((d, float(c['close'])))

        closes_by_date.sort(key=lambda x: x[0])

        if len(closes_by_date) < sma_period:
            continue

        # Use last sma_period closes for SMA, last close for comparison
        recent_closes = [c for _, c in closes_by_date[-sma_period:]]
        sma = sum(recent_closes) / sma_period
        last_close = closes_by_date[-1][1]

        total += 1
        if last_close > sma:
            above += 1

    if total < 50:
        logger.warning("Breadth: only %d stocks with enough data (need 50+)", total)
        return -1.0

    return round(above / total * 100, 1)


# ── State persistence ────────────────────────────────────────────────────


def load_state() -> dict:
    """Load regime state from disk. Returns empty dict on first run."""
    if not cfg.REGIME_STATE_FILE.exists():
        return {}
    try:
        with open(cfg.REGIME_STATE_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Regime state corrupted (%s), starting fresh", e)
        return {}


def save_state(state: dict):
    """Persist regime state (atomic write)."""
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cfg.REGIME_STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(str(tmp), str(cfg.REGIME_STATE_FILE))


# ── NIFTY data from Kite API ─────────────────────────────────────────────


def _fetch_nifty_daily(kite, days: int = 100) -> list:
    """Fetch NIFTY 50 daily OHLC from Kite. Returns list of {date, close, ...}."""
    # NIFTY 50 index instrument token (stable across sessions)
    nifty_token = 256265

    now = datetime.now(IST)
    start = now - timedelta(days=days + 30)  # buffer for holidays

    data = kite.historical_data(
        nifty_token, start.strftime('%Y-%m-%d'),
        now.strftime('%Y-%m-%d'), 'day'
    )

    result = []
    for c in data:
        d = c['date']
        if isinstance(d, str):
            d = datetime.fromisoformat(d.replace('+05:30', '')).date()
        elif hasattr(d, 'date') and callable(d.date):
            d = d.date()  # datetime -> date
        result.append({
            'date': d,
            'close': float(c['close']),
            'high': float(c['high']),
            'low': float(c['low']),
        })

    result.sort(key=lambda x: x['date'])
    return result


def _nifty_from_cache() -> list:
    """Load NIFTY daily data from backtest_cache (fallback if Kite unavailable)."""
    if not cfg.NIFTY_CACHE_FILE.exists():
        return []
    try:
        with open(cfg.NIFTY_CACHE_FILE) as f:
            raw = json.load(f)
        result = []
        for c in raw:
            d = c['date']
            if isinstance(d, str):
                d = datetime.fromisoformat(d.replace('+05:30', '')).date()
            result.append({
                'date': d,
                'close': float(c['close']),
                'high': float(c['high']),
                'low': float(c['low']),
            })
        result.sort(key=lambda x: x['date'])
        return result
    except Exception as e:
        logger.error("Failed to load NIFTY cache: %s", e)
        return []


# ── Telegram ─────────────────────────────────────────────────────────────

_telegram_cfg = None
_telegram_cfg_loaded = False


def _send_telegram(msg: str):
    """Send regime alert via Telegram. Reuses st_watch telegram config."""
    global _telegram_cfg, _telegram_cfg_loaded

    try:
        if not _telegram_cfg_loaded:
            watch_cfg = cfg.load_config()
            tg = watch_cfg.get('telegram')
            if tg and tg.get('bot_token') and tg.get('chat_id'):
                _telegram_cfg = tg
            elif cfg.TELEGRAM_CONFIG.exists():
                with open(cfg.TELEGRAM_CONFIG) as f:
                    _telegram_cfg = json.load(f)
            _telegram_cfg_loaded = True

        if not _telegram_cfg:
            return

        import requests
        requests.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


# ── Core: compute signals + detect transitions ──────────────────────────


_nifty_history_cache = None   # cached daily OHLC (fetched once per session)
_breadth_cache = None         # {date_str: value} — backtest_cache fallback


def _get_live_breadth() -> float:
    """Get latest breadth from Chartink breadth tracker. Returns -1 if unavailable."""
    try:
        from .breadth_tracker import get_latest
        latest = get_latest()
        if latest:
            # Use if reading is from today and less than 60 min old
            now = datetime.now(IST)
            reading_date = latest.get('date', '')
            today = now.strftime('%Y-%m-%d')
            if reading_date == today:
                return latest['breadth_pct']
            # Yesterday's last reading is OK too (for pre-market / early checks)
            yesterday = (now - timedelta(days=1)).strftime('%Y-%m-%d')
            if reading_date == yesterday:
                logger.debug("Using yesterday's breadth: %.1f%%", latest['breadth_pct'])
                return latest['breadth_pct']
    except Exception as e:
        logger.debug("Live breadth unavailable: %s", e)
    return -1.0


def compute_signals(kite=None, live_nifty_ltp: float = None) -> dict:
    """Compute all 3 regime signals from current data.

    Returns dict with rsi, breadth, nifty_close, nifty_50dma, dma_gap_pct,
    signals dict, signals_on count, and the check_date.

    Uses Kite API for NIFTY data (with backtest_cache fallback).
    Uses backtest_cache for breadth (local computation, cached per day).

    If live_nifty_ltp is provided, it's appended as an intraday synthetic
    candle so RSI and DMA reflect the live price. Breadth still uses the
    last completed daily close (it doesn't change intraday).
    """
    global _nifty_history_cache, _breadth_cache
    rcfg = cfg.load_regime_config()

    # ── NIFTY historical data (fetch once per session) ──
    if _nifty_history_cache is None:
        nifty_data = []
        if kite:
            try:
                nifty_data = _fetch_nifty_daily(kite, rcfg['nifty_data_days'])
                logger.info("Fetched %d days of NIFTY data from Kite", len(nifty_data))
            except Exception as e:
                logger.warning("Kite NIFTY fetch failed (%s), using cache", e)

        if not nifty_data:
            nifty_data = _nifty_from_cache()
            if nifty_data:
                logger.info("Using cached NIFTY data (%d days)", len(nifty_data))
            else:
                logger.error("No NIFTY data available (Kite + cache both failed)")
                return {}

        _nifty_history_cache = nifty_data

    nifty_data = list(_nifty_history_cache)  # work on copy

    # ── Append live LTP as synthetic intraday candle ──
    if live_nifty_ltp and live_nifty_ltp > 0:
        today = datetime.now(IST).date()
        # Replace today's candle if it exists, else append
        if nifty_data and nifty_data[-1]['date'] == today:
            nifty_data[-1] = {**nifty_data[-1], 'close': live_nifty_ltp}
        else:
            nifty_data.append({
                'date': today, 'close': live_nifty_ltp,
                'high': live_nifty_ltp, 'low': live_nifty_ltp,
            })

    closes = [c['close'] for c in nifty_data]
    if len(closes) < rcfg['sma_period'] + 1:
        logger.error("Not enough NIFTY data: %d days (need %d+)",
                     len(closes), rcfg['sma_period'] + 1)
        return {}

    # Signal 1: RSI (uses live close if available)
    rsi = compute_rsi(closes, rcfg['rsi_period'])
    rsi_on = rsi > rcfg['rsi_threshold']

    # Signal 3: 50DMA (uses live close if available)
    sma50 = compute_sma(closes, rcfg['sma_period'])
    nifty_close = closes[-1]
    dma_gap = round((nifty_close - sma50) / sma50 * 100, 2) if sma50 else 0
    dma_on = nifty_close > sma50

    # check_date = last completed daily candle date (for state machine tracking)
    check_date = (_nifty_history_cache[-1]['date']
                  if _nifty_history_cache else date.today())

    # Signal 2: Breadth — prefer live Chartink, fall back to backtest_cache
    breadth = _get_live_breadth()
    if breadth < 0:
        # Fallback: backtest_cache (offline)
        if _breadth_cache is None:
            _breadth_cache = {}
        breadth_key = str(check_date)
        if breadth_key in _breadth_cache:
            breadth = _breadth_cache[breadth_key]
        else:
            breadth = compute_breadth(
                cfg.BACKTEST_CACHE_DIR, target_date=check_date,
                sma_period=rcfg['sma_period']
            )
            _breadth_cache[breadth_key] = breadth
            logger.info("Breadth fallback (cache): %.1f%% for %s", breadth, breadth_key)

    breadth_on = breadth > rcfg['breadth_threshold'] if breadth >= 0 else False

    signals_on = sum([rsi_on, breadth_on, dma_on])

    return {
        'check_date': str(check_date),
        'nifty_close': nifty_close,
        'nifty_50dma': sma50,
        'dma_gap_pct': dma_gap,
        'rsi': rsi,
        'breadth': breadth,
        'signals': {
            'rsi': {'value': rsi, 'on': rsi_on, 'threshold': rcfg['rsi_threshold']},
            'breadth': {'value': breadth, 'on': breadth_on, 'threshold': rcfg['breadth_threshold']},
            'dma': {'value': dma_gap, 'on': dma_on, 'threshold': 0},
        },
        'signals_on': signals_on,
        'is_live': live_nifty_ltp is not None,
    }


def _signal_icon(on: bool) -> str:
    return '\u2705' if on else '\u274c'


def check_regime(kite=None, dry_run: bool = False) -> dict:
    """Compute signals, update state machine, alert on transitions.

    Designed to be called every 30 minutes during market hours.

    Two layers:
    1. INTRADAY signal snapshot: recomputed every call using live NIFTY LTP.
       Detects regime-relevant changes as early as possible.
    2. OFFICIAL state update (history + consecutive count): once per calendar
       day only (based on the historical data date, not wall clock).

    Alerting: one Telegram alert per day per transition type. The first call
    that detects a change sends the alert; subsequent calls same day are silent.
    """
    rcfg = cfg.load_regime_config()
    state = load_state()

    # ── Fetch live NIFTY LTP if Kite available ──
    live_ltp = None
    if kite:
        try:
            ltp_data = kite.ltp(['NSE:NIFTY 50'])
            live_ltp = ltp_data.get('NSE:NIFTY 50', {}).get('last_price')
        except Exception as e:
            logger.debug("Live NIFTY LTP fetch failed: %s", e)

    signals = compute_signals(kite, live_nifty_ltp=live_ltp)

    if not signals:
        logger.error("Cannot compute regime — no signal data")
        return state

    check_date = signals['check_date']  # date of last completed daily candle
    today_str = datetime.now(IST).strftime('%Y-%m-%d')

    prev_status = state.get('status', 'UNPAUSED')
    prev_consecutive = state.get('consecutive_2of3', 0)
    sig_on = signals['signals_on']

    # ── State machine (evaluated every call for intraday detection) ──
    new_status = prev_status
    new_consecutive = prev_consecutive
    transition = None

    if prev_status == 'UNPAUSED':
        if sig_on == 0:
            new_status = 'PAUSED'
            new_consecutive = 0
            transition = 'PAUSE'
    else:  # PAUSED
        if sig_on >= 2:
            new_consecutive = prev_consecutive + 1
            if new_consecutive >= rcfg['unpause_days']:
                new_status = 'UNPAUSED'
                new_consecutive = 0
                transition = 'UNPAUSE'
        else:
            if prev_consecutive > 0:
                transition = 'COUNT_RESET'
            new_consecutive = 0

    count_progress = (
        prev_status == 'PAUSED'
        and new_status == 'PAUSED'
        and new_consecutive > 0
        and transition is None
    )

    # ── Breadth velocity (5-day change from history) ──
    history = state.get('history', [])
    breadth_vel = None
    if len(history) >= 5:
        old_breadth = history[-5].get('breadth', 0)
        new_breadth = signals['breadth']
        if old_breadth >= 0 and new_breadth >= 0:
            breadth_vel = round(new_breadth - old_breadth, 1)

    # ── Alerting: once per day per transition ──
    # last_alert_date tracks when we last sent a regime Telegram alert
    already_alerted_today = state.get('last_alert_date') == today_str

    rsi_s = signals['signals']['rsi']
    br_s = signals['signals']['breadth']
    dma_s = signals['signals']['dma']

    signal_line = (
        f"RSI {_signal_icon(rsi_s['on'])} {rsi_s['value']:.1f} "
        f"| Breadth {_signal_icon(br_s['on'])} {br_s['value']:.1f}% "
        f"| DMA {_signal_icon(dma_s['on'])} {dma_s['value']:+.1f}%"
    )
    nifty_line = (
        f"NIFTY {signals['nifty_close']:,.1f} "
        f"(50DMA {signals['nifty_50dma']:,.1f})"
    )
    live_tag = " <i>(live)</i>" if signals.get('is_live') else ""

    alert_msg = None

    if transition == 'PAUSE':
        alert_msg = (
            f"\u26d4 <b>REGIME PAUSED</b>{live_tag}\n"
            f"All 3 signals OFF \u2014 Active basket entries BLOCKED\n\n"
            f"{signal_line}\n{nifty_line}"
        )

    elif transition == 'UNPAUSE':
        vel_text = ""
        if breadth_vel is not None:
            if breadth_vel > 30:
                vel_text = f"\n\n\U0001f7e2 Breadth velocity: <b>+{breadth_vel} ppts/5d</b> (HIGH conviction)"
            elif breadth_vel > 15:
                vel_text = f"\n\n\U0001f7e1 Breadth velocity: <b>+{breadth_vel} ppts/5d</b> (MODERATE)"
            else:
                vel_text = f"\n\n\U0001f7e0 Breadth velocity: <b>+{breadth_vel} ppts/5d</b> (LOW \u2014 fewer positions)"
        alert_msg = (
            f"\u2705 <b>REGIME UNPAUSED</b>{live_tag}\n"
            f"2-of-3 sustained {rcfg['unpause_days']} days \u2014 Active entries RESUMED\n\n"
            f"{signal_line}\n{nifty_line}{vel_text}"
        )

    elif transition == 'COUNT_RESET' and rcfg.get('alert_on_count_reset'):
        alert_msg = (
            f"\U0001f504 <b>UNPAUSE RESET</b> Day {prev_consecutive}\u21920{live_tag}\n"
            f"Signal dropped below 2-of-3\n\n"
            f"{signal_line}\n{nifty_line}"
        )

    elif count_progress and rcfg.get('alert_on_count_progress'):
        alert_msg = (
            f"\U0001f504 <b>UNPAUSE Day {new_consecutive}/{rcfg['unpause_days']}</b>{live_tag}\n"
            f"2-of-3 signals ON ({sig_on}/3)\n\n"
            f"{signal_line}\n{nifty_line}"
        )

    should_send = alert_msg and not already_alerted_today

    if should_send:
        if dry_run:
            logger.info("DRY RUN regime alert:\n%s", alert_msg)
        else:
            _send_telegram(alert_msg)
            logger.info("Regime alert sent: %s",
                        transition or f"count day {new_consecutive}")
        state['last_alert_date'] = today_str
    elif alert_msg:
        logger.debug("Regime change detected but already alerted today")

    # ── Official state update: once per calendar day (history + consecutive) ──
    # This ensures the 7-day count advances only once per trading day.
    if state.get('last_check_date') != check_date:
        history.append({
            'date': check_date,
            'status': new_status,
            'rsi': signals['rsi'],
            'breadth': signals['breadth'],
            'dma_gap_pct': signals['dma_gap_pct'],
            'nifty_close': signals['nifty_close'],
            'signals_on': sig_on,
            'consecutive_2of3': new_consecutive,
        })
        if len(history) > 30:
            history = history[-30:]
        state['last_check_date'] = check_date
        state['consecutive_2of3'] = new_consecutive
        state['history'] = history

        if transition in ('PAUSE', 'UNPAUSE'):
            state['last_transition'] = {
                'date': check_date,
                'from': prev_status,
                'to': new_status,
            }

    # ── Always update live snapshot fields ──
    state.update({
        'status': new_status,
        'rsi': signals['rsi'],
        'breadth': signals['breadth'],
        'nifty_close': signals['nifty_close'],
        'nifty_50dma': signals['nifty_50dma'],
        'dma_gap_pct': signals['dma_gap_pct'],
        'signals_on': sig_on,
        'last_scan_time': datetime.now(IST).isoformat(),
    })

    save_state(state)
    logger.info("Regime: %s | sig=%d/3 | consec=%d | NIFTY=%.0f%s",
                new_status, sig_on, new_consecutive, signals['nifty_close'],
                ' (live)' if live_ltp else '')

    return state


# ── Display ──────────────────────────────────────────────────────────────


def print_regime_status(state: dict = None):
    """Print formatted regime status table."""
    import sys
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    if state is None:
        state = load_state()

    if not state:
        print("\nNo regime state found. Run 'regime --check' to compute.")
        return

    status = state.get('status', '?')
    status_icon = '\u2705' if status == 'UNPAUSED' else '\u26d4'

    print(f"\n{'='*60}")
    print(f"  Regime Monitor — Active Basket Filter")
    print(f"  Last check: {state.get('last_check_date', 'never')}")
    print(f"{'='*60}")
    print(f"\n  Status: {status_icon} <{status}>")
    print(f"  NIFTY:  {state.get('nifty_close', 0):,.1f}  "
          f"(50DMA: {state.get('nifty_50dma', 0):,.1f}, "
          f"gap: {state.get('dma_gap_pct', 0):+.1f}%)")
    print(f"  RSI(14): {state.get('rsi', 0):.1f}  (threshold: 50)")
    print(f"  Breadth: {state.get('breadth', 0):.1f}%  (threshold: 40%)")
    print(f"  Signals ON: {state.get('signals_on', 0)}/3")

    consec = state.get('consecutive_2of3', 0)
    if status == 'PAUSED' and consec > 0:
        print(f"\n  UNPAUSE progress: Day {consec}/7")

    lt = state.get('last_transition')
    if lt:
        print(f"\n  Last transition: {lt.get('from')} -> {lt.get('to')} "
              f"on {lt.get('date')}")

    # Velocity from history
    history = state.get('history', [])
    if len(history) >= 5:
        old = history[-5]
        new = history[-1]
        rsi_vel = round(new.get('rsi', 0) - old.get('rsi', 0), 1)
        br_vel = round(new.get('breadth', 0) - old.get('breadth', 0), 1)
        dma_vel = round(new.get('dma_gap_pct', 0) - old.get('dma_gap_pct', 0), 2)
        print(f"\n  Velocity (5-day):")
        print(f"    RSI:     {rsi_vel:+.1f}")
        print(f"    Breadth: {br_vel:+.1f} ppts")
        print(f"    DMA gap: {dma_vel:+.2f}%")

        if status == 'PAUSED':
            # Distance to thresholds
            print(f"\n  Distance to thresholds:")
            rsi_need = 50 - state.get('rsi', 0)
            br_need = 40 - state.get('breadth', 0)
            dma_need = -state.get('dma_gap_pct', 0) if state.get('dma_gap_pct', 0) < 0 else 0
            print(f"    RSI:     needs {rsi_need:+.1f} pts")
            print(f"    Breadth: needs {br_need:+.1f} ppts")
            if dma_need > 0:
                print(f"    DMA:     needs +{dma_need:.1f}% to cross above")
            else:
                print(f"    DMA:     already above")

    print()

"""Live breadth tracker — Chartink-powered intraday breadth monitoring.

Scrapes two Chartink scanners every 30 minutes:
  1. F&O stocks above their 50DMA  → "above" count
  2. Total F&O stocks              → "total" count
  Breadth = above / total × 100%

Stores timestamped readings for velocity/acceleration tracking.
Integrates with regime_monitor for live breadth signal.

Chartink query notes:
  {33489} = F&O segment filter (NSE futures & options eligible stocks)
  sma(close, 50) = 50-day simple moving average
"""

import json
import logging
import os
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import pytz

from . import config as cfg

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# ── Chartink scanner config ──────────────────────────────────────────────

CHARTINK_SCREENER_URL = 'https://chartink.com/screener/'
CHARTINK_SCAN_URL = 'https://chartink.com/backtest/process'

# F&O stocks where close > 50DMA (the "above" count)
SCAN_ABOVE_50DMA = (
    '( {33489} ( '
    ' latest close > latest sma( close , 50 )'
    ' ) )'
)

# All F&O stocks (the "total" count)
SCAN_ALL_FO = (
    '( {33489} ( '
    ' latest close > 0'
    ' ) )'
)

# ── Data file ────────────────────────────────────────────────────────────

BREADTH_DATA_FILE = cfg.LOG_DIR / 'breadth_readings.json'


def _load_readings() -> list:
    """Load breadth readings from disk."""
    if not BREADTH_DATA_FILE.exists():
        return []
    try:
        with open(BREADTH_DATA_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Breadth data corrupted (%s), starting fresh", e)
        return []


def _save_readings(readings: list):
    """Persist breadth readings (atomic write)."""
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = BREADTH_DATA_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(readings, f, indent=2, default=str)
    os.replace(str(tmp), str(BREADTH_DATA_FILE))


# ── Chartink scraper ─────────────────────────────────────────────────────


def _scan_chartink(scan_clause: str) -> list:
    """Scrape Chartink and return list of matching F&O stocks.

    Uses ``backtest/process`` endpoint which returns ``aggregatedStockList``
    — a flat array of [symbol, mcap, sector, ...] triplets.  This is the
    correctly-filtered F&O universe (~209 stocks).

    Returns list of dicts: [{'nsecode': str, 'mcap': str, 'chartink_sector': str}, ...]
    Empty list on failure.
    """
    try:
        import requests
        from bs4 import BeautifulSoup

        with requests.Session() as s:
            r = s.get(CHARTINK_SCREENER_URL, timeout=15)
            soup = BeautifulSoup(r.text, 'html.parser')
            csrf_tag = soup.select_one("[name='csrf-token']")
            if not csrf_tag:
                logger.error("Chartink CSRF token not found on page")
                return []
            s.headers['x-csrf-token'] = csrf_tag['content']

            r = s.post(CHARTINK_SCAN_URL,
                       data={'scan_clause': scan_clause},
                       timeout=15)
            resp = r.json()

            # Parse aggregatedStockList (triplets: [sym, mcap, sector, ...])
            agg = resp.get('aggregatedStockList', [])
            if not agg or not isinstance(agg, list):
                logger.error("Chartink: aggregatedStockList missing or empty")
                return []

            # backtest/process returns [[...], [...], ...] — one array per date.
            # Use first array only (most recent / current scan).
            if isinstance(agg[0], list):
                flat = agg[0]
            elif isinstance(agg[0], str):
                flat = agg  # flat array of strings directly
            else:
                logger.error("Chartink: unexpected aggregatedStockList format: %s",
                             type(agg[0]).__name__)
                return []

            if not flat or not isinstance(flat[0], str):
                logger.error("Chartink: aggregatedStockList[0] is empty or not strings")
                return []

            if len(flat) % 3 != 0:
                logger.warning("Chartink: aggregatedStockList length %d not divisible "
                               "by 3, last %d elements dropped", len(flat), len(flat) % 3)

            stocks = []
            for i in range(0, len(flat) - 2, 3):
                sym = flat[i]
                if sym and isinstance(sym, str):
                    stocks.append({
                        'nsecode': sym,
                        'mcap': flat[i + 1] or '',
                        'chartink_sector': flat[i + 2] or '',
                    })

            if not stocks:
                logger.error("Chartink: parsed 0 stocks from aggregatedStockList")
            return stocks

    except Exception as e:
        logger.error("Chartink breadth scan failed: %s", e)
        return []


def _compute_sector_breadth(above_stocks: list, total_stocks: list) -> tuple:
    """Compute per-sector breadth from Chartink results.

    Uses Chartink's native sector data (from aggregatedStockList) mapped
    through the CHARTINK_SECTOR_MAP bridge.  Falls back to static
    sector_mapping for stocks with unknown Chartink sectors.

    Returns:
        (sectors, unmapped) where:
        - sectors: sorted list of dicts [{sector, above, total, breadth_pct}, ...]
          Only sectors with >= 3 total stocks included (filter noise).
        - unmapped: sorted list of F&O stock symbols whose Chartink sector
          could not be mapped to any of our curated sector keys.
    """
    from playbook.sector_mapping import get_sector_from_chartink

    # Count total per sector + collect unmapped
    sector_total = {}
    unmapped = set()
    for item in total_stocks:
        sym = item.get('nsecode', '')
        if not sym:
            continue
        chartink_sec = item.get('chartink_sector', '')
        sector = get_sector_from_chartink(sym, chartink_sec)
        if sector == '_skip':
            continue  # indices (NIFTY, BANKNIFTY)
        if sector == 'other':
            unmapped.add(sym)
            continue
        sector_total[sector] = sector_total.get(sector, 0) + 1

    # Count above per sector
    sector_above = {}
    for item in above_stocks:
        sym = item.get('nsecode', '')
        if not sym:
            continue
        chartink_sec = item.get('chartink_sector', '')
        sector = get_sector_from_chartink(sym, chartink_sec)
        if sector in ('_skip', 'other'):
            continue
        sector_above[sector] = sector_above.get(sector, 0) + 1

    # Build sector breadth list
    result = []
    for sector, total in sector_total.items():
        if total < 3:
            continue
        above = sector_above.get(sector, 0)
        result.append({
            'sector': sector,
            'above': above,
            'total': total,
            'breadth_pct': round(above / total * 100, 1),
        })

    result.sort(key=lambda x: x['breadth_pct'], reverse=True)
    return result, sorted(unmapped)


# Human-readable sector labels
SECTOR_LABELS = {
    'nifty_it': 'IT',
    'nifty_psu_bank': 'PSU Bank',
    'nifty_pvt_bank': 'Pvt Bank',
    'nifty_bank': 'BFSI',
    'nifty_pharma': 'Pharma',
    'nifty_auto': 'Auto',
    'nifty_metal': 'Metal',
    'nifty_fmcg': 'FMCG',
    'nifty_energy': 'Energy',
    'nifty_realty': 'Realty',
    'nifty_infra': 'Infra',
    'nifty_media': 'Media',
}


def fetch_breadth() -> dict:
    """Run both Chartink scans and compute breadth %.

    Returns dict with above, total, breadth_pct, sector_breadth, timestamp.
    Returns None on failure.
    """
    above_stocks = _scan_chartink(SCAN_ABOVE_50DMA)
    time.sleep(0.5)  # rate limit between scans
    total_stocks = _scan_chartink(SCAN_ALL_FO)

    above = len(above_stocks)
    total = len(total_stocks)

    if total == 0:
        logger.error("Chartink breadth scan failed: total=0")
        return None

    # Exclude indices (NIFTY, BANKNIFTY) from stock counts
    from playbook.sector_mapping import get_sector_from_chartink, update_dynamic_cache
    dyn_map = {}
    skipped = 0
    for item in total_stocks:
        sym = item.get('nsecode', '')
        if not sym:
            continue
        sec = get_sector_from_chartink(sym, item.get('chartink_sector', ''))
        if sec == '_skip':
            skipped += 1
            continue
        if sec != 'other':
            dyn_map[sym] = sec
    update_dynamic_cache(dyn_map)

    # Adjust counts: exclude indices from denominator
    total = total - skipped
    # Recount above excluding indices
    skip_syms = {item.get('nsecode', '') for item in total_stocks
                 if get_sector_from_chartink(item.get('nsecode', ''),
                                             item.get('chartink_sector', '')) == '_skip'}
    above_index_count = sum(1 for item in above_stocks
                            if item.get('nsecode', '') in skip_syms)
    above = above - above_index_count

    if total <= 0:
        logger.error("Chartink breadth scan failed: total=%d after filtering", total)
        return None

    # Guard: above can never exceed total (race between two scans)
    above = min(above, total)

    breadth_pct = round(above / total * 100, 1)
    now = datetime.now(IST)

    # Sector breakdown + unmapped detection
    sector_breadth, unmapped = _compute_sector_breadth(above_stocks, total_stocks)

    reading = {
        'date': now.strftime('%Y-%m-%d'),
        'time': now.strftime('%H:%M'),
        'timestamp': now.isoformat(),
        'above': above,
        'total': total,
        'breadth_pct': breadth_pct,
        'sectors': sector_breadth,
        'unmapped': unmapped,
    }

    if unmapped:
        logger.info("Breadth: %d/%d = %.1f%% (%d sectors, %d unmapped: %s)",
                     above, total, breadth_pct, len(sector_breadth),
                     len(unmapped), ', '.join(unmapped[:5]))
    else:
        logger.info("Breadth: %d/%d = %.1f%% (%d sectors)", above, total, breadth_pct,
                    len(sector_breadth))
    return reading


# ── Storage + velocity/acceleration ──────────────────────────────────────


def record_reading(reading: dict) -> list:
    """Append a breadth reading to the data file. Returns updated list."""
    readings = _load_readings()
    readings.append(reading)

    # Keep last 30 days of readings (at 30-min intervals = ~390 per day)
    cutoff = (datetime.now(IST) - timedelta(days=30)).strftime('%Y-%m-%d')
    readings = [r for r in readings if r.get('date', '') >= cutoff]

    _save_readings(readings)
    return readings


def get_latest() -> dict:
    """Get the most recent breadth reading, or None."""
    readings = _load_readings()
    return readings[-1] if readings else None


def get_today_readings() -> list:
    """Get all readings from today."""
    today = datetime.now(IST).strftime('%Y-%m-%d')
    readings = _load_readings()
    return [r for r in readings if r.get('date') == today]


def compute_velocity(readings: list = None, window: int = 5) -> dict:
    """Compute breadth velocity and acceleration from recent readings.

    velocity = change over last N readings (in ppts)
    acceleration = velocity change (is it speeding up or slowing down)

    Returns dict with velocity, acceleration, readings_used, timespan.
    """
    if readings is None:
        readings = _load_readings()

    if len(readings) < 2:
        return {'velocity': 0, 'acceleration': 0, 'readings_used': 0}

    recent = readings[-min(window + 1, len(readings)):]
    latest = recent[-1]['breadth_pct']
    oldest = recent[0]['breadth_pct']
    velocity = round(latest - oldest, 1)

    # Acceleration: compare current velocity to previous velocity
    acceleration = 0.0
    if len(recent) >= 3:
        mid = len(recent) // 2
        vel_first_half = recent[mid]['breadth_pct'] - recent[0]['breadth_pct']
        vel_second_half = recent[-1]['breadth_pct'] - recent[mid]['breadth_pct']
        acceleration = round(vel_second_half - vel_first_half, 1)

    return {
        'velocity': velocity,
        'acceleration': acceleration,
        'readings_used': len(recent),
        'from': recent[0].get('time', '?'),
        'to': recent[-1].get('time', '?'),
        'from_breadth': oldest,
        'to_breadth': latest,
    }


def compute_daily_velocity(readings: list = None) -> dict:
    """Compute day-over-day breadth velocity using EOD readings.

    Groups readings by date, takes the last reading per day,
    computes multi-day velocity (5-day and 1-day).
    Discards readings with a different denominator (total) than the latest
    to avoid velocity distortion from denominator changes.
    """
    if readings is None:
        readings = _load_readings()

    if not readings:
        return {}

    # Group by date, take last reading per day
    by_date = {}
    for r in readings:
        d = r.get('date', '')
        by_date[d] = r  # last one wins

    daily = sorted(by_date.values(), key=lambda r: r.get('date', ''))

    if len(daily) < 2:
        return {'velocity_1d': 0, 'velocity_5d': 0}

    # Filter: only keep readings with same denominator as latest (±20%)
    # to avoid velocity distortion from denominator changes (e.g., 496→207)
    latest_total = daily[-1].get('total', 0)
    if latest_total:
        daily = [d for d in daily
                 if not d.get('total') or
                 abs(d['total'] - latest_total) / latest_total <= 0.2]

    if len(daily) < 2:
        return {'velocity_1d': 0, 'velocity_5d': 0}

    result = {
        'velocity_1d': round(daily[-1]['breadth_pct'] - daily[-2]['breadth_pct'], 1),
    }

    if len(daily) >= 6:
        result['velocity_5d'] = round(daily[-1]['breadth_pct'] - daily[-6]['breadth_pct'], 1)
    elif len(daily) >= 2:
        result['velocity_5d'] = round(
            daily[-1]['breadth_pct'] - daily[0]['breadth_pct'], 1)

    result['days'] = len(daily)
    return result


# ── Display ──────────────────────────────────────────────────────────────


def print_breadth_status():
    """Print breadth tracker status with velocity/acceleration."""
    import sys
    if sys.platform == 'win32':
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')

    readings = _load_readings()

    if not readings:
        print("\nNo breadth data. Run 'breadth --scan' to start tracking.")
        return

    latest = readings[-1]
    today_readings = [r for r in readings if r.get('date') == latest.get('date')]

    print(f"\n{'='*55}")
    print(f"  Breadth Tracker — F&O stocks above 50DMA")
    print(f"  Last scan: {latest.get('time', '?')} on {latest.get('date', '?')}")
    print(f"{'='*55}")

    # Current reading
    above = latest.get('above', 0)
    total = latest.get('total', 0)
    bpct = latest.get('breadth_pct', 0)
    bar = _breadth_bar(bpct)

    print(f"\n  Breadth: {bpct}%  ({above}/{total} stocks)")
    print(f"  {bar}")
    print(f"  Regime threshold: 40%  {'✅ ABOVE' if bpct > 40 else '❌ BELOW'}")

    # Intraday velocity (from today's readings)
    if len(today_readings) >= 2:
        intra_vel = compute_velocity(today_readings)
        print(f"\n  Intraday ({intra_vel['from']}\u2192{intra_vel['to']}):")
        print(f"    Velocity:     {intra_vel['velocity']:+.1f} ppts")
        print(f"    Acceleration: {intra_vel['acceleration']:+.1f} ppts")
        print(f"    Readings:     {intra_vel['readings_used']}")

    # Daily velocity
    daily_vel = compute_daily_velocity(readings)
    if daily_vel.get('days', 0) >= 2:
        print(f"\n  Daily velocity:")
        print(f"    1-day:  {daily_vel.get('velocity_1d', 0):+.1f} ppts")
        if 'velocity_5d' in daily_vel:
            print(f"    5-day:  {daily_vel['velocity_5d']:+.1f} ppts")

    # Sector breakdown (from latest reading)
    sectors = latest.get('sectors', [])
    if sectors:
        print(f"\n  Sector breadth:")
        print(f"  {'Sector':<12} {'Breadth':>8} {'Stocks':>8}")
        print(f"  {'-'*30}")
        for s in sectors:
            label = SECTOR_LABELS.get(s['sector'], s['sector'])
            print(f"  {label:<12} {s['breadth_pct']:>7.1f}% {s['above']:>3}/{s['total']:<3}")

    # Today's progression
    if len(today_readings) >= 2:
        print(f"\n  Today's readings:")
        print(f"  {'Time':<6} {'Breadth':>8} {'Change':>7}")
        print(f"  {'-'*24}")
        prev = None
        for r in today_readings:
            b = r.get('breadth_pct', 0)
            change = f"{b - prev:+.1f}" if prev is not None else "  —"
            print(f"  {r.get('time', '?'):<6} {b:>7.1f}% {change:>7}")
            prev = b

    print()


def _breadth_bar(pct: float, width: int = 40) -> str:
    """Visual bar for breadth %. Threshold marker at 40%."""
    filled = int(pct / 100 * width)
    threshold_pos = int(40 / 100 * width)  # 40% marker
    bar = list('\u2591' * width)
    for i in range(min(filled, width)):
        bar[i] = '\u2593'
    if threshold_pos < width:
        bar[threshold_pos] = '|'
    return '  [' + ''.join(bar) + f'] {pct:.1f}%'


# ── Telegram ─────────────────────────────────────────────────────────────

_telegram_cfg = None
_telegram_cfg_loaded = False


def _send_telegram(msg: str):
    """Send breadth alert via Telegram. Reuses st_watch config."""
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
        import requests as req
        req.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        logger.warning("Telegram failed: %s", e)


# ── Alert state (persisted in breadth data file header) ──────────────────

ALERT_STATE_FILE = cfg.LOG_DIR / 'breadth_alert_state.json'

# Breadth levels that trigger a one-time crossing alert
BREADTH_LEVELS = [20, 30, 40, 50]

# Minimum ppts change in a single reading to trigger velocity spike alert
VELOCITY_SPIKE_THRESHOLD = 3.0


def _load_alert_state() -> dict:
    if not ALERT_STATE_FILE.exists():
        return {}
    try:
        with open(ALERT_STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_alert_state(state: dict):
    cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ALERT_STATE_FILE.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(str(tmp), str(ALERT_STATE_FILE))


# ── Morning briefing ────────────────────────────────────────────────────


def build_morning_briefing(dry_run: bool = False) -> str:
    """Build and send the 9 AM morning briefing.

    Content: yesterday's breadth summary, velocity, acceleration,
    regime status, distance to thresholds.
    """
    readings = _load_readings()
    today = datetime.now(IST).strftime('%Y-%m-%d')
    today_disp = datetime.now(IST).strftime('%d-%b')

    # Get yesterday's readings (last reading per day, not today)
    by_date = {}
    for r in readings:
        d = r.get('date', '')
        if d < today:
            by_date.setdefault(d, []).append(r)

    if not by_date:
        return ""

    # Yesterday = most recent date before today
    yesterday = max(by_date.keys())
    y_readings = by_date[yesterday]
    y_last = y_readings[-1]
    y_first = y_readings[0]
    y_high = max(r['breadth_pct'] for r in y_readings)
    y_low = min(r['breadth_pct'] for r in y_readings)
    y_close = y_last['breadth_pct']
    y_change = round(y_close - y_first['breadth_pct'], 1)

    # Daily velocities
    daily_vel = compute_daily_velocity(readings)
    vel_1d = daily_vel.get('velocity_1d', 0)
    vel_5d = daily_vel.get('velocity_5d', 0)

    # Regime status
    from .regime_monitor import load_state
    regime = load_state()
    regime_status = regime.get('status', 'UNKNOWN')
    regime_icon = '\u2705' if regime_status == 'UNPAUSED' else '\u26d4'
    rsi = regime.get('rsi', 0)
    dma_gap = regime.get('dma_gap_pct', 0)
    consec = regime.get('consecutive_2of3', 0)

    # Distance to 40% threshold
    dist = round(40 - y_close, 1)
    if vel_1d > 0 and dist > 0:
        eta_days = round(dist / vel_1d)
        eta_text = f"~{eta_days}d at current pace"
    elif dist <= 0:
        eta_text = "already above"
    else:
        eta_text = "moving away"

    # Velocity emoji
    def _vel_icon(v):
        if v > 3:
            return '\U0001f7e2'  # green
        elif v > 0:
            return '\U0001f7e1'  # yellow
        elif v > -3:
            return '\U0001f7e0'  # orange
        else:
            return '\U0001f534'  # red

    msg = (
        f"\U0001f4ca <b>MORNING BRIEFING</b> | {today_disp}\n\n"
        f"{regime_icon} <b>Regime: {regime_status}</b>"
    )
    if regime_status == 'PAUSED' and consec > 0:
        msg += f" (UNPAUSE day {consec}/7)"
    lt = regime.get('last_transition')
    if lt:
        msg += f"\nSince {lt.get('date', '?')}"

    msg += (
        f"\nRSI {rsi:.0f} | Breadth {y_close:.1f}% | DMA {dma_gap:+.1f}%\n\n"
        f"<b>Breadth:</b> {y_close:.1f}% ({y_last.get('above', '?')}/{y_last.get('total', '?')})\n"
        f"Yesterday: {y_first['breadth_pct']:.1f}%\u2192{y_close:.1f}% "
        f"(H:{y_high:.1f} L:{y_low:.1f} \u0394:{y_change:+.1f})\n\n"
        f"<b>Velocity:</b>\n"
        f"  {_vel_icon(vel_1d)} 1-day: {vel_1d:+.1f} ppts\n"
        f"  {_vel_icon(vel_5d)} 5-day: {vel_5d:+.1f} ppts\n\n"
        f"<b>Distance to 40%:</b> {dist:+.1f} ppts ({eta_text})"
    )

    # Sector breakdown: current state + intraday rotation
    sectors_close = y_last.get('sectors', [])
    sectors_open = y_first.get('sectors', [])

    if sectors_close:
        # Compute sector velocity (open → close change during yesterday)
        open_map = {s['sector']: s['breadth_pct'] for s in sectors_open} if sectors_open else {}
        sector_vel = []
        for s in sectors_close:
            sec = s['sector']
            open_b = open_map.get(sec)
            vel = round(s['breadth_pct'] - open_b, 1) if open_b is not None else None
            sector_vel.append({**s, 'velocity': vel})

        # Top 3 by current breadth
        top3 = sector_vel[:3]
        bot3 = sector_vel[-3:]

        msg += "\n\n<b>Sectors \u2191 Top 3:</b>"
        for s in top3:
            label = SECTOR_LABELS.get(s['sector'], s['sector'])
            vel_str = f" ({s['velocity']:+.0f})" if s.get('velocity') is not None else ""
            msg += f"\n  \U0001f7e2 {label}: {s['breadth_pct']:.0f}% ({s['above']}/{s['total']}){vel_str}"

        msg += "\n<b>Sectors \u2193 Bottom 3:</b>"
        for s in bot3:
            label = SECTOR_LABELS.get(s['sector'], s['sector'])
            vel_str = f" ({s['velocity']:+.0f})" if s.get('velocity') is not None else ""
            msg += f"\n  \U0001f534 {label}: {s['breadth_pct']:.0f}% ({s['above']}/{s['total']}){vel_str}"

        # Sector movers: biggest intraday changes (rotation signal)
        movers = [s for s in sector_vel if s.get('velocity') is not None
                  and abs(s['velocity']) >= 5]
        movers.sort(key=lambda s: abs(s['velocity']), reverse=True)
        if movers:
            msg += "\n\n<b>Sector movers:</b>"
            for s in movers[:3]:
                label = SECTOR_LABELS.get(s['sector'], s['sector'])
                icon = '\U0001f4c8' if s['velocity'] > 0 else '\U0001f4c9'
                msg += f"\n  {icon} {label}: {s['velocity']:+.0f} ppts intraday"

    # Unmapped F&O stocks (not in sector mapping)
    unmapped = y_last.get('unmapped', [])
    if unmapped:
        msg += f"\n\n\u26a0\ufe0f <b>Unmapped ({len(unmapped)}):</b> "
        msg += ", ".join(unmapped[:10])
        if len(unmapped) > 10:
            msg += f" +{len(unmapped) - 10} more"

    if not dry_run:
        _send_telegram(msg)
    logger.info("Morning briefing sent")
    return msg


# ── Intraday breadth alerts ─────────────────────────────────────────────


def check_breadth_alerts(reading: dict, dry_run: bool = False) -> str:
    """Check for abrupt breadth changes and level crossings.

    Called after each breadth scan. Alerts on:
    1. Velocity spike: single reading changes ≥ 3 ppts
    2. Level crossing: breadth crosses 20/30/40/50% in either direction
    3. Momentum flip: velocity changes sign (sustained for 2+ readings)

    Returns alert message if sent, empty string if not.
    """
    if not reading:
        return ""

    state = _load_alert_state()
    today = reading.get('date', '')
    breadth = reading.get('breadth_pct', 0)
    now_time = reading.get('time', '')

    # Reset state on new day
    if state.get('date') != today:
        state = {'date': today, 'alerts_sent': [], 'last_breadth': None,
                 'last_total': None,
                 'velocity_sign_count': 0, 'last_velocity_sign': 0}

    # Detect denominator change (e.g., 496→207 from Chartink fix).
    # If total shifted by >20%, discard stale prev_breadth to avoid spurious alerts.
    curr_total = reading.get('total', 0)
    prev_total = state.get('last_total')
    if prev_total and curr_total and abs(curr_total - prev_total) / prev_total > 0.2:
        logger.warning("Breadth denominator changed %d→%d, resetting alert state",
                        prev_total, curr_total)
        state['last_breadth'] = None
        state['velocity_sign_count'] = 0
        state['last_velocity_sign'] = 0
        state['had_flip'] = False
        state['last_sectors'] = []

    alerts = []
    prev_breadth = state.get('last_breadth')

    if prev_breadth is not None:
        change = round(breadth - prev_breadth, 1)

        # 1. Velocity spike: ≥ 3 ppts in one reading
        if abs(change) >= VELOCITY_SPIKE_THRESHOLD:
            alert_key = f"spike_{now_time}"
            if alert_key not in state.get('alerts_sent', []):
                direction = '\U0001f4c8' if change > 0 else '\U0001f4c9'
                alerts.append((
                    alert_key,
                    f"{direction} <b>BREADTH SPIKE</b> {change:+.1f} ppts\n"
                    f"{prev_breadth:.1f}%\u2192{breadth:.1f}% "
                    f"({reading.get('above', '?')}/{reading.get('total', '?')}) at {now_time}"
                ))

        # 2. Level crossing (once per level per direction per day)
        for level in BREADTH_LEVELS:
            crossed_up = prev_breadth < level <= breadth
            crossed_down = prev_breadth >= level > breadth
            if crossed_up or crossed_down:
                direction_tag = "up" if crossed_up else "down"
                alert_key = f"level_{level}_{direction_tag}"
                if alert_key not in state.get('alerts_sent', []):
                    if crossed_up:
                        icon = '\u2705' if level == 40 else '\U0001f7e2'
                        label = "REGIME THRESHOLD" if level == 40 else f"{level}% LEVEL"
                        alerts.append((
                            alert_key,
                            f"{icon} <b>BREADTH CROSSED {label}</b> \u2191\n"
                            f"{prev_breadth:.1f}%\u2192{breadth:.1f}% at {now_time}"
                        ))
                    else:
                        icon = '\u26d4' if level == 40 else '\U0001f534'
                        label = "REGIME THRESHOLD" if level == 40 else f"{level}% LEVEL"
                        alerts.append((
                            alert_key,
                            f"{icon} <b>BREADTH BROKE {label}</b> \u2193\n"
                            f"{prev_breadth:.1f}%\u2192{breadth:.1f}% at {now_time}"
                        ))

        # 3. Momentum flip detection
        # Only alert when direction actually REVERSES (not on first readings)
        sign = 1 if change > 0.3 else (-1 if change < -0.3 else 0)
        last_sign = state.get('last_velocity_sign', 0)
        had_flip = state.get('had_flip', False)

        if sign != 0 and sign != last_sign and last_sign != 0:
            # Actual sign flip detected — start counting in new direction
            state['velocity_sign_count'] = 1
            state['had_flip'] = True
        elif sign != 0 and sign == last_sign and had_flip:
            # Same direction after a flip — increment count
            state['velocity_sign_count'] = state.get('velocity_sign_count', 0) + 1

        if sign != 0:
            state['last_velocity_sign'] = sign

        # Alert on 2nd consecutive reading AFTER a flip
        if state.get('velocity_sign_count') == 2 and had_flip:
            alert_key = f"flip_{sign}_{now_time}"
            if alert_key not in state.get('alerts_sent', []):
                if sign > 0:
                    alerts.append((
                        alert_key,
                        f"\U0001f504 <b>BREADTH TURNING UP</b>\n"
                        f"2 consecutive positive readings after decline. "
                        f"Now {breadth:.1f}%"
                    ))
                else:
                    alerts.append((
                        alert_key,
                        f"\U0001f504 <b>BREADTH TURNING DOWN</b>\n"
                        f"2 consecutive negative readings after rally. "
                        f"Now {breadth:.1f}%"
                    ))

    # 4. Sector rotation alert: detect big sector shifts between readings
    curr_sectors = reading.get('sectors', [])
    prev_sectors_map = {s['sector']: s['breadth_pct']
                        for s in state.get('last_sectors', [])}
    if curr_sectors and prev_sectors_map:
        for s in curr_sectors:
            sec = s['sector']
            prev_sec_b = prev_sectors_map.get(sec)
            if prev_sec_b is None:
                continue
            sec_change = round(s['breadth_pct'] - prev_sec_b, 1)
            # Alert if a sector shifts ≥ 10 ppts in a single reading
            if abs(sec_change) >= 10:
                label = SECTOR_LABELS.get(sec, sec)
                direction_tag = "up" if sec_change > 0 else "down"
                alert_key = f"sector_{sec}_{direction_tag}"
                if alert_key not in state.get('alerts_sent', []):
                    icon = '\U0001f4c8' if sec_change > 0 else '\U0001f4c9'
                    alerts.append((
                        alert_key,
                        f"{icon} <b>SECTOR SHIFT: {label}</b> {sec_change:+.0f} ppts\n"
                        f"{prev_sec_b:.0f}%\u2192{s['breadth_pct']:.0f}% "
                        f"({s['above']}/{s['total']}) at {now_time}"
                    ))

    # Send alerts
    sent_msg = ""
    for alert_key, msg in alerts:
        if dry_run:
            logger.info("DRY RUN breadth alert:\n%s", msg)
        else:
            _send_telegram(msg)
            logger.info("Breadth alert: %s", alert_key)
        state.setdefault('alerts_sent', []).append(alert_key)
        sent_msg = msg  # return last alert

    state['last_breadth'] = breadth
    state['last_total'] = curr_total
    state['last_sectors'] = curr_sectors
    _save_alert_state(state)
    return sent_msg


# ── Main entry point ────────────────────────────────────────────────────


def scan_and_record(dry_run: bool = False) -> dict:
    """Fetch live breadth from Chartink, record it, and check for alerts.

    Returns reading or None.
    """
    reading = fetch_breadth()
    if reading:
        record_reading(reading)
        check_breadth_alerts(reading, dry_run=dry_run)
    return reading

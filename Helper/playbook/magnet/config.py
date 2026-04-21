"""Magnet strategy configuration — constants, thresholds, Chartink scan clauses."""

from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()          # playbook/magnet/
PLAYBOOK_DIR = SCRIPT_DIR.parent                      # playbook/
PROJECT_ROOT = PLAYBOOK_DIR.parent                    # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                       # BOTS/
LOG_DIR = PROJECT_ROOT / 'logs'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'magnet_config.json'
LOCAL_FILE = LOG_DIR / 'magnet_trades.json'
KITE_TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
TELEGRAM_CONFIG = BOTS_ROOT / 'data' / 'telegram_config.json'
BACKTEST_CACHE = PLAYBOOK_DIR / 'backtest_cache'

# ── Chartink ──────────────────────────────────────────────────────────────
CHARTINK_SCAN_URL = 'https://chartink.com/screener/process'
CHARTINK_BACKTEST_URL = 'https://chartink.com/backtest/process'
CHARTINK_URL = CHARTINK_SCAN_URL  # both endpoints work; screener is proven

# Monthly: F&O stocks within +/-8.1% of Monthly ST(10,3)
# Wider net (was +/-5.1%) to catch candidates BEFORE Chartink's 5-10 min
# delivery delay pushes them past our entry window. Our own Kite-LTP monitor
# enforces the actual entry gate (3-4% band).
CHARTINK_MONTHLY = (
    '( {33489} ( '
    ' monthly close <=  monthly supertrend( 10 , 3 ) *  1.081'
    ' and  monthly close >=  monthly supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

# Weekly: F&O stocks within +/-8.1% of Weekly ST(10,3)
CHARTINK_WEEKLY = (
    '( {33489} ( '
    ' weekly close <=  weekly supertrend( 10 , 3 ) *  1.081'
    ' and  weekly close >=  weekly supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

# Daily: F&O stocks within +/-8.1% of Daily ST(10,3)
# Default timeframe on Chartink is daily — no prefix needed
CHARTINK_DAILY = (
    '( {33489} ( '
    ' close <=  supertrend( 10 , 3 ) *  1.081'
    ' and  close >=  supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

_ALL_SCANNERS = [
    {'name': 'monthly', 'clause': CHARTINK_MONTHLY, 'timeframe': 'monthly'},
    {'name': 'weekly',  'clause': CHARTINK_WEEKLY,  'timeframe': 'weekly'},
    {'name': 'daily',   'clause': CHARTINK_DAILY,   'timeframe': 'daily'},
]

# ── Defaults (overridden by magnet_config.json if present) ────────────────
# These are compile-time defaults. Runtime values come from _load_runtime().
_DEFAULTS = {
    # Gap cascade (surface → watch → enter → cost_sl → target → sl):
    #   Chartink scan (±8.1%) → chartink_gap_max (8%) → signal_gap_max (5%, WATCH)
    #   → entry_gap (4%) → entry_gap_min (3%) → cost_sl_gap (2%) → ST (target 0%)
    #   → hedge_gap (5.5%, reversal) → sl_gap (7%, thesis dead)
    'chartink_gap_max': 0.08,     # 8% — outer Kite-validated gate (Chartink's late-delivery buffer)
    'signal_gap_max': 0.05,       # 5% — WATCH band ceiling (create 'watching' signal)
    'entry_gap': 0.04,            # 4% — enter when Kite-LTP gap shrinks to this
    'entry_gap_min': 0.03,        # 3% — floor: cancel if gap falls below (too late, past entry zone)
    'cost_sl_gap': 0.02,           # 2% — move SL to cost+0.10 at halfway (scales with entry_gap)
    'hedge_gap': 0.055,           # 5.5% — add short leg when gap widens past watch band (reversal)
    'hedge_max_debit_ratio': 0.35,# max net debit / spread width to enter hedge
    'premium_sl_pct': 0.25,       # 25% — tighter SL, cut losers faster (was 40%)
    'sl_gap': 0.07,               # 7% — if gap widens past this, thesis dead (widened with entry)
    'tight_sl_pct': 0.025,         # 2.5% — ENTRY-anchored adverse move SL (fires BEFORE sl_gap). Synthetic futures need this tight.
    'sl_time_days': 5,            # exit if no touch within 5 trading days
    'freshness_days': 5,          # signal invalid if price was <2% within last N days
    'slippage_pct': 0.0,          # deprecated: now using tick-size rounding (±1 tick)
    'trail_pct': 0.50,            # trail at 50% of peak premium gains
    'trail_min_gain_pct': 0.15,   # trail only activates after 15% gain (avoids bid-ask noise)
    'entry_max_retries': 5,       # cancel signal after N failed option lookups
    'min_dte': 3,                 # minimum days to expiry (current month liquidity is good)
    'lots_per_trade': 1,          # lots per trade (start small)
    'max_open_trades': 10,        # max concurrent magnet trades
    'max_capital_per_trade': 50000,  # Rs 50K max capital at risk per trade (prevents outsized exposure)
    'scan_interval_sec': 300,     # 5 minutes between Chartink scans
    'monitor_interval_sec': 30,   # 30 seconds between LTP checks
    # ── Daily velocity filters (from backtest: 80% hit rate when all pass) ──
    'daily_velocity_3d_max': -0.5, # 3-day gap change must be < this (negative = closing)
    'daily_momentum_min': 60,      # % of last 5 days gap was closing (>=60%)
    'daily_gap_max': 0.03,         # same 3% scan range; velocity filter is the quality gate
    'enabled_timeframes': ['monthly', 'weekly', 'daily'],  # toggle scanners on/off
    'daily_eod_exit_time': [15, 15],   # [hour, minute] — force-exit daily trades (backtest: day1=+63L, day2+=-37L)
    'daily_premium_sl_pct': 0.25,      # 25% — tighter for intraday (backtest: +80L vs +63L at 40%)
    'use_confidence_tracker': False,   # True = delegate entry/exit to confidence tracker (pullback timing)
    'confidence_poll_sec': 300,        # 5 min — how often confidence tracker checks entry timing
    'spot_tracker_enabled': False,     # True = run spot 15M ST tracker (pullback→flip alerts). Default OFF (resource waste vs value ratio was poor).
    'st_period': 10,                   # Supertrend period
    'st_multiplier': 3,               # Supertrend multiplier
}

import json as _json
import logging as _logging

_cfg_logger = _logging.getLogger(__name__)

def _load_runtime() -> dict:
    """Load runtime config from magnet_config.json, merged with defaults."""
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_cfg = _json.load(f)
            # Merge file values over defaults (only known keys)
            for key in _DEFAULTS:
                if key in file_cfg:
                    cfg[key] = file_cfg[key]
            # Warn about unrecognized keys (typos in config)
            unknown = [k for k in file_cfg if k not in _DEFAULTS
                       and k not in ('google_drive', 'telegram_watching')]
            if unknown:
                _cfg_logger.warning("magnet_config.json: unknown keys ignored: %s", unknown)
        except Exception as e:
            _cfg_logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
    return cfg

_runtime = _load_runtime()

# ── Gap thresholds (as fraction, not percent) ─────────────────────────────
CHARTINK_GAP_MAX = _runtime.get('chartink_gap_max', 0.08)  # outer surface gate
SIGNAL_GAP_MAX = _runtime['signal_gap_max']
ENTRY_GAP = _runtime['entry_gap']
ENTRY_GAP_MIN = _runtime['entry_gap_min']
COST_SL_GAP = _runtime.get('cost_sl_gap', 0.01)
HEDGE_GAP = _runtime.get('hedge_gap', 0.03)
HEDGE_MAX_DEBIT_RATIO = _runtime.get('hedge_max_debit_ratio', 0.35)
PREMIUM_SL_PCT = _runtime.get('premium_sl_pct', 0.25)
SL_GAP = _runtime.get('sl_gap', 0.05)
TIGHT_SL_PCT = _runtime.get('tight_sl_pct', 0.025)  # entry-anchored adverse-move SL
SL_TIME_DAYS = _runtime['sl_time_days']
FRESHNESS_DAYS = _runtime['freshness_days']
TOUCHED_THRESHOLD = _runtime.get('touched_threshold', 0.01)  # 1% = ST touch (for freshness + cooldown)

# ── Invariant checks: catch mis-configured thresholds at startup ─────────
assert CHARTINK_GAP_MAX >= SIGNAL_GAP_MAX, (
    f"CHARTINK_GAP_MAX ({CHARTINK_GAP_MAX}) must be >= SIGNAL_GAP_MAX ({SIGNAL_GAP_MAX}) — "
    "else Chartink gate is tighter than WATCH band (no buffer for late-delivery)")
assert SL_GAP > SIGNAL_GAP_MAX, (
    f"SL_GAP ({SL_GAP}) must be > SIGNAL_GAP_MAX ({SIGNAL_GAP_MAX}) — "
    "else SL triggers inside watch band")
assert ENTRY_GAP < SIGNAL_GAP_MAX, (
    f"ENTRY_GAP ({ENTRY_GAP}) must be < SIGNAL_GAP_MAX ({SIGNAL_GAP_MAX}) — "
    "else no watch band")
assert ENTRY_GAP_MIN < ENTRY_GAP, (
    f"ENTRY_GAP_MIN ({ENTRY_GAP_MIN}) must be < ENTRY_GAP ({ENTRY_GAP}) — "
    "else no entry zone")
assert COST_SL_GAP < ENTRY_GAP, (
    f"COST_SL_GAP ({COST_SL_GAP}) must be < ENTRY_GAP ({ENTRY_GAP}) — "
    "breakeven SL must trigger after entry, not before")

# ── Trail ────────────────────────────────────────────────────────────────
TRAIL_PCT = _runtime.get('trail_pct', 0.50)
TRAIL_MIN_GAIN_PCT = _runtime.get('trail_min_gain_pct', 0.15)

# ── Option selection ──────────────────────────────────────────────────────
MIN_DTE = _runtime['min_dte']
ENTRY_MAX_RETRIES = _runtime.get('entry_max_retries', 5)

# ── Scan / monitor intervals ─────────────────────────────────────────────
SCAN_INTERVAL_SEC = _runtime['scan_interval_sec']
MONITOR_INTERVAL_SEC = _runtime['monitor_interval_sec']
MARKET_OPEN = (9, 15)        # 9:15 AM IST
MARKET_CLOSE = (15, 30)      # 3:30 PM IST

# ── Position sizing ──────────────────────────────────────────────────────
LOTS_PER_TRADE = _runtime['lots_per_trade']
MAX_OPEN_TRADES = _runtime['max_open_trades']
MAX_CAPITAL_PER_TRADE = _runtime.get('max_capital_per_trade', 50000)

# ── Supertrend parameters ───────────────────────────────────────────────
ST_PERIOD = _runtime.get('st_period', 10)
ST_MULTIPLIER = _runtime.get('st_multiplier', 3)

# ── Daily velocity thresholds ───────────────────────────────────────────
DAILY_VELOCITY_3D_MAX = _runtime.get('daily_velocity_3d_max', -0.5)
DAILY_MOMENTUM_MIN = _runtime.get('daily_momentum_min', 60)
DAILY_GAP_MAX = _runtime.get('daily_gap_max', 0.03)
ENABLED_TIMEFRAMES = _runtime.get('enabled_timeframes', ['monthly', 'weekly', 'daily'])
DAILY_PREMIUM_SL_PCT = _runtime.get('daily_premium_sl_pct', 0.25)
_eod = _runtime.get('daily_eod_exit_time', [15, 15])
DAILY_EOD_EXIT_HOUR = _eod[0]
DAILY_EOD_EXIT_MIN = _eod[1]
USE_CONFIDENCE_TRACKER = _runtime.get('use_confidence_tracker', False)
CONFIDENCE_POLL_SEC = _runtime.get('confidence_poll_sec', 300)
SPOT_TRACKER_ENABLED = _runtime.get('spot_tracker_enabled', False)

# Filter scanners to only enabled timeframes
SCANNERS = [s for s in _ALL_SCANNERS if s['timeframe'] in ENABLED_TIMEFRAMES]

PRODUCT = 'NRML'             # positional, not intraday
OPTION_TYPE_MAP = {
    # price > ST (above support) → expect decline to ST → buy PUT
    'above': 'PE',
    # price < ST (below resistance) → expect rally to ST → buy CALL
    'below': 'CE',
}

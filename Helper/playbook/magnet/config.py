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

# Monthly: F&O stocks within +/-3.1% of Monthly ST(10,3)
CHARTINK_MONTHLY = (
    '( {33489} ( '
    ' monthly close <=  monthly supertrend( 10 , 3 ) *  1.031'
    ' and  monthly close >=  monthly supertrend( 10 , 3 ) *  0.969'
    ' ) )'
)

# Weekly: F&O stocks within +/-3.1% of Weekly ST(10,3)
CHARTINK_WEEKLY = (
    '( {33489} ( '
    ' weekly close <=  weekly supertrend( 10 , 3 ) *  1.031'
    ' and  weekly close >=  weekly supertrend( 10 , 3 ) *  0.969'
    ' ) )'
)

# Daily: F&O stocks within +/-3.1% of Daily ST(10,3)
# Default timeframe on Chartink is daily — no prefix needed
CHARTINK_DAILY = (
    '( {33489} ( '
    ' close <=  supertrend( 10 , 3 ) *  1.031'
    ' and  close >=  supertrend( 10 , 3 ) *  0.969'
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
    'signal_gap_max': 0.03,       # 3% — Chartink fires within this range
    'entry_gap': 0.02,            # 2% — buy option when gap shrinks to this
    'entry_gap_min': 0.005,       # 0.5% — too close, already past entry zone
    'cost_sl_gap': 0.01,           # 1% — move SL to cost+0.10 when stock reaches this gap
    'hedge_gap': 0.03,            # 3% — add short leg when gap widens back to 3% (reversal)
    'hedge_max_debit_ratio': 0.35,# max net debit / spread width to enter hedge
    'premium_sl_pct': 0.40,       # 40% — exit if premium drops 40% (fallback when hedge not possible)
    'sl_gap': 0.05,               # 5% — if gap widens to this, thesis dead
    'sl_time_days': 5,            # exit if no touch within 5 trading days
    'freshness_days': 5,          # signal invalid if price was <2% within last N days
    'slippage_pct': 0.02,         # 2% of premium (buy: +slippage, sell: -slippage)
    'trail_pct': 0.50,            # trail at 50% of peak premium gains
    'min_dte': 10,                # minimum days to expiry for option selection
    'lots_per_trade': 1,          # lots per trade (start small)
    'max_open_trades': 10,        # max concurrent magnet trades
    'scan_interval_sec': 300,     # 5 minutes between Chartink scans
    'monitor_interval_sec': 30,   # 30 seconds between LTP checks
    # ── Daily velocity filters (from backtest: 80% hit rate when all pass) ──
    'daily_velocity_3d_max': -0.5, # 3-day gap change must be < this (negative = closing)
    'daily_momentum_min': 60,      # % of last 5 days gap was closing (>=60%)
    'daily_gap_max': 0.03,         # same 3% scan range; velocity filter is the quality gate
    'enabled_timeframes': ['monthly', 'weekly', 'daily'],  # toggle scanners on/off
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
            unknown = [k for k in file_cfg if k not in _DEFAULTS and k != 'google_drive']
            if unknown:
                _cfg_logger.warning("magnet_config.json: unknown keys ignored: %s", unknown)
        except Exception as e:
            _cfg_logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
    return cfg

_runtime = _load_runtime()

# ── Gap thresholds (as fraction, not percent) ─────────────────────────────
SIGNAL_GAP_MAX = _runtime['signal_gap_max']
ENTRY_GAP = _runtime['entry_gap']
ENTRY_GAP_MIN = _runtime['entry_gap_min']
COST_SL_GAP = _runtime.get('cost_sl_gap', 0.01)
HEDGE_GAP = _runtime.get('hedge_gap', 0.03)
HEDGE_MAX_DEBIT_RATIO = _runtime.get('hedge_max_debit_ratio', 0.35)
PREMIUM_SL_PCT = _runtime.get('premium_sl_pct', 0.40)
SL_GAP = _runtime.get('sl_gap', 0.05)
SL_TIME_DAYS = _runtime['sl_time_days']
FRESHNESS_DAYS = _runtime['freshness_days']

# ── Slippage ──────────────────────────────────────────────────────────────
SLIPPAGE_PCT = _runtime['slippage_pct']
TRAIL_PCT = _runtime.get('trail_pct', 0.50)

# ── Option selection ──────────────────────────────────────────────────────
MIN_DTE = _runtime['min_dte']

# ── Scan / monitor intervals ─────────────────────────────────────────────
SCAN_INTERVAL_SEC = _runtime['scan_interval_sec']
MONITOR_INTERVAL_SEC = _runtime['monitor_interval_sec']
MARKET_OPEN = (9, 15)        # 9:15 AM IST
MARKET_CLOSE = (15, 30)      # 3:30 PM IST

# ── Position sizing ──────────────────────────────────────────────────────
LOTS_PER_TRADE = _runtime['lots_per_trade']
MAX_OPEN_TRADES = _runtime['max_open_trades']

# ── Option selection ──────────────────────────────────────────────────────
# ── Supertrend parameters ───────────────────────────────────────────────
ST_PERIOD = 10
ST_MULTIPLIER = 3

# ── Daily velocity thresholds ───────────────────────────────────────────
DAILY_VELOCITY_3D_MAX = _runtime.get('daily_velocity_3d_max', -0.5)
DAILY_MOMENTUM_MIN = _runtime.get('daily_momentum_min', 60)
DAILY_GAP_MAX = _runtime.get('daily_gap_max', 0.03)
ENABLED_TIMEFRAMES = _runtime.get('enabled_timeframes', ['monthly', 'weekly', 'daily'])

# Filter scanners to only enabled timeframes
SCANNERS = [s for s in _ALL_SCANNERS if s['timeframe'] in ENABLED_TIMEFRAMES]

PRODUCT = 'NRML'             # positional, not intraday
OPTION_TYPE_MAP = {
    # price > ST (above support) → expect decline to ST → buy PUT
    'above': 'PE',
    # price < ST (below resistance) → expect rally to ST → buy CALL
    'below': 'CE',
}

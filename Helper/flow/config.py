"""Flow strategy configuration — constants, Chartink clauses, thresholds."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # flow/
PROJECT_ROOT = SCRIPT_DIR.parent                    # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                     # BOTS/
LOG_DIR = PROJECT_ROOT / 'logs'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'flow_config.json'
LOCAL_FILE = LOG_DIR / 'flow_trades.json'
KITE_TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
TELEGRAM_CONFIG = BOTS_ROOT / 'data' / 'telegram_config.json'
OPTIONS_CSV = PROJECT_ROOT / 'nse_stocks_options.csv'

# ── Chartink ──────────────────────────────────────────────────────────────
CHARTINK_URL = 'https://chartink.com/screener/process'

# UP: W+D+H+30M all ST UP (F&O universe {33489})
CHARTINK_UP = (
    '( {33489} ( '
    ' daily close >  daily supertrend( 10 , 3 )'
    ' and  weekly close >  weekly supertrend( 10 , 3 )'
    ' and  [0] 1 hour close >  [0] 1 hour supertrend( 10 , 3 )'
    ' and  [0] 30 minute close >  [0] 30 minute supertrend( 10 , 3 )'
    ' ) )'
)

# DOWN: W+D+H+30M all ST DOWN
CHARTINK_DOWN = (
    '( {33489} ( '
    ' daily close <  daily supertrend( 10 , 3 )'
    ' and  weekly close <  weekly supertrend( 10 , 3 )'
    ' and  [0] 1 hour close <  [0] 1 hour supertrend( 10 , 3 )'
    ' and  [0] 30 minute close <  [0] 30 minute supertrend( 10 , 3 )'
    ' ) )'
)

# ── Defaults (overridden by flow_config.json) ─────────────────────────────
_DEFAULTS = {
    # Alert zone: LTP within ±0.5% of 15M ST
    'alert_zone_pct': 0.005,
    # Take profit: 5% spot move from entry
    'tp_pct': 0.05,
    # Capital per trade (buy as many lots as fit)
    'capital_per_trade': 50000,
    # Minimum DTE for option selection
    'min_dte': 15,
    # Max bid-ask spread as % of mid-price
    'max_spread_pct': 0.10,
    # ST parameters (same across all TFs)
    'st_period': 10,
    'st_multiplier': 3,
    # Timing intervals (seconds)
    'chartink_interval_sec': 300,    # 5 min — watchlist refresh
    'ltp_check_interval_sec': 60,    # 1 min — proximity + TP check
    '15m_candle_data_days': 5,       # days of 15M data to fetch for ST
    # No re-entry same day after SL
    'no_reentry_same_day': True,
    # Pre-close alert time
    'pre_close_hour': 14,
    'pre_close_minute': 45,
}

# ── Market hours ──────────────────────────────────────────────────────────
MARKET_START = (9, 30)    # scan starts 9:30 AM
MARKET_END = (15, 0)      # scan ends 3:00 PM
MARKET_OPEN = (9, 15)     # exchange opens (for candle data)
MARKET_CLOSE = (15, 30)   # exchange closes

# ── Chartink symbol fixups (NSE mismatches) ───────────────────────────────
CHARTINK_TO_NSE = {
    'M_M': 'M&M',
    'M_MFIN': 'M&MFIN',
    'L_TFH': 'L&TFH',
    'NAM_INDIA': 'NAM-INDIA',
    'BAJAJ_AUTO': 'BAJAJ-AUTO',
}


def _load_runtime() -> dict:
    """Load runtime config from flow_config.json, merged with defaults."""
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_cfg = json.load(f)
            for key in _DEFAULTS:
                if key in file_cfg:
                    cfg[key] = file_cfg[key]
            unknown = [k for k in file_cfg if k not in _DEFAULTS
                       and k not in ('google_drive',)]
            if unknown:
                logger.warning("flow_config.json: unknown keys ignored: %s", unknown)
        except Exception as e:
            logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
    return cfg


_runtime = _load_runtime()

# ── Export runtime values ─────────────────────────────────────────────────
ALERT_ZONE_PCT = _runtime['alert_zone_pct']
TP_PCT = _runtime['tp_pct']
CAPITAL_PER_TRADE = _runtime['capital_per_trade']
MIN_DTE = _runtime['min_dte']
MAX_SPREAD_PCT = _runtime['max_spread_pct']
ST_PERIOD = _runtime['st_period']
ST_MULTIPLIER = _runtime['st_multiplier']
CHARTINK_INTERVAL_SEC = _runtime['chartink_interval_sec']
LTP_CHECK_INTERVAL_SEC = _runtime['ltp_check_interval_sec']
CANDLE_DATA_DAYS = _runtime['15m_candle_data_days']
NO_REENTRY_SAME_DAY = _runtime['no_reentry_same_day']
PRE_CLOSE_HOUR = _runtime['pre_close_hour']
PRE_CLOSE_MINUTE = _runtime['pre_close_minute']

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

SCANNERS = [
    {'name': 'monthly', 'clause': CHARTINK_MONTHLY, 'timeframe': 'monthly'},
    {'name': 'weekly',  'clause': CHARTINK_WEEKLY,  'timeframe': 'weekly'},
]

# ── Gap thresholds (as fraction, not percent) ─────────────────────────────
SIGNAL_GAP_MAX = 0.03       # 3% — Chartink fires within this range
ENTRY_GAP = 0.02            # 2% — buy option when gap shrinks to this
ENTRY_GAP_MIN = 0.005       # 0.5% — too close, already past entry zone
ADJ_GAP = 0.035             # 3.5% — sell OTM to create spread (damage control)
SL_GAP = 0.05               # 5% — if gap widens to this, thesis dead
SL_TIME_DAYS = 5             # exit if no touch within 5 trading days
FRESHNESS_DAYS = 5           # signal invalid if price was <2% within last N days

# ── Slippage ──────────────────────────────────────────────────────────────
SLIPPAGE_PCT = 0.02          # 2% of premium added to ask (buy) / removed from bid (sell)

# ── Supertrend ────────────────────────────────────────────────────────────
ST_PERIOD = 10
ST_MULTIPLIER = 3
KITE_HISTORY_YEARS = 6       # 6+ years for accurate monthly ST

# ── Scan / monitor intervals ─────────────────────────────────────────────
SCAN_INTERVAL_SEC = 300      # 5 minutes between Chartink scans
MONITOR_INTERVAL_SEC = 30    # 30 seconds between LTP checks
MARKET_OPEN = (9, 15)        # 9:15 AM IST
MARKET_CLOSE = (15, 30)      # 3:30 PM IST

# ── Position sizing (paper mode — notional) ───────────────────────────────
LOTS_PER_TRADE = 1           # 1 lot per trade (fixed, increase later)
MAX_OPEN_TRADES = 10         # max concurrent magnet trades

# ── Option selection ──────────────────────────────────────────────────────
PRODUCT = 'NRML'             # positional, not intraday
OPTION_TYPE_MAP = {
    # price > ST (above support) → expect decline to ST → buy PUT
    'above': 'PE',
    # price < ST (below resistance) → expect rally to ST → buy CALL
    'below': 'CE',
}

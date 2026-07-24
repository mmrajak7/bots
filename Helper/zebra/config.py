"""Zebra strategy configuration — paths, thresholds, Chartink scan clauses."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # zebra/
PROJECT_ROOT = SCRIPT_DIR.parent                    # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                     # BOTS/
LOG_DIR = PROJECT_ROOT / 'logs'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'zebra_config.json'
LOCAL_FILE = LOG_DIR / 'zebra_trades.json'
KITE_TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
TELEGRAM_CONFIG = BOTS_ROOT / 'data' / 'telegram_config.json'
OPTIONS_CSV = PROJECT_ROOT / 'nse_stocks_options.csv'

# ── Chartink scan clauses ─────────────────────────────────────────────────
# Both sides: price within ±X% of ST line, ST direction determines play.
# Width 8.1% mirrors magnet (catches candidates before Chartink delivery delay
# pushes them past entry window). Our own Kite-LTP check enforces actual gate.

CHARTINK_URL = 'https://chartink.com/screener/process'

CHARTINK_MONTHLY = (
    '( {33489} ( '
    ' monthly close <=  monthly supertrend( 10 , 3 ) *  1.081'
    ' and  monthly close >=  monthly supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

CHARTINK_WEEKLY = (
    '( {33489} ( '
    ' weekly close <=  weekly supertrend( 10 , 3 ) *  1.081'
    ' and  weekly close >=  weekly supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

_ALL_SCANNERS = [
    {'name': 'monthly', 'clause': CHARTINK_MONTHLY, 'timeframe': 'monthly'},
    {'name': 'weekly',  'clause': CHARTINK_WEEKLY,  'timeframe': 'weekly'},
]

# ── Defaults ──────────────────────────────────────────────────────────────
_DEFAULTS = {
    'paper_mode': True,          # PAPER: auto-enter on trigger + auto-close on exit signal
    'bcs_paper_enabled': True,   # Shadow BCS (buy ATM, sell strike nearest ST target)
                                 # paper-traded alongside every zebra entry for A/B
                                 # comparison (July 2026 slippage analysis). Only
                                 # active when paper_mode is also true.
    'alert_structures': ['bcs'], # Which structures' Telegram alerts fire
                                 # (ENTER + TP/SL/TIME). 2026-07-17: BCS is the
                                 # voice, zebra trades silently in the background.
                                 # Both keep auto-trading + appear in EOD reports
                                 # regardless. Set ['zebra','bcs'] or ['zebra'] to
                                 # change who talks.
    'watch_gap_max': 0.05,       # WATCH band ceiling (signal added to watchlist)
    'trigger_gap_max': 0.04,     # TRIGGER zone: run Zebra analyzer + alert
    'stale_gap_min': 0.03,       # Floor: skip if gap < this at trigger (too late)
    'freshness_days': 5,         # Skip if price touched ST in last N days (bounce)
    'min_dte': 15,
    'max_dte': 45,
    'min_leg_oi': 5000,
    'max_leg_spread_pct': 0.01,  # bid-ask spread cap per leg (1% of mid)
    'tp_target': 'st_line',       # 'st_line' or 'short_strike'
    'spot_sl_enabled': False,     # master switch for the adverse-spot SL (off: debit floor only)
    'spot_sl_pct': 0.03,          # adverse spot move from entry that triggers SL (only if enabled)
    'debit_sl_pct': 0.50,         # exit if option mid drops to this fraction of entry debit
    'time_sl_days_before_expiry': 3,
    'max_open_trades': 8,        # LIVE guidance only. PAPER intentionally does
                                 # NOT cap entries — capturing every signal keeps
                                 # the validation P&L unbiased (a cap would skew
                                 # which trades the track record contains).
    'max_watching_signals': 25,
    'scan_interval_sec': 300,    # 5 min between Chartink scans
    'monitor_interval_sec': 300, # 5 min between LTP/monitor checks
    'enabled_directions': ['CE', 'PE'],
    'enabled_timeframes': ['monthly', 'weekly'],
    'st_period': 10,
    'st_multiplier': 3,
}


def _load_runtime() -> dict:
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_cfg = json.load(f)
            for key in _DEFAULTS:
                if key in file_cfg:
                    cfg[key] = file_cfg[key]
            unknown = [k for k in file_cfg if k not in _DEFAULTS
                       and k not in ('google_drive', 'telegram')]
            if unknown:
                logger.warning("zebra_config.json: unknown keys ignored: %s", unknown)
        except Exception as e:
            logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
    return cfg


_runtime = _load_runtime()

# ── Exports ───────────────────────────────────────────────────────────────
PAPER_MODE = _runtime['paper_mode']
BCS_PAPER_ENABLED = _runtime['bcs_paper_enabled']
_raw_alerts = _runtime['alert_structures']
if isinstance(_raw_alerts, str):     # common JSON typo: "bcs" not ["bcs"]
    _raw_alerts = [_raw_alerts]
ALERT_STRUCTURES = [s for s in _raw_alerts if s in ('zebra', 'bcs')]
if list(_raw_alerts) != ALERT_STRUCTURES:
    logger.warning("alert_structures: unknown entries ignored in %s",
                   _raw_alerts)
if not ALERT_STRUCTURES and PAPER_MODE:
    logger.warning("alert_structures is empty — NO per-trade Telegram alerts "
                   "will fire in paper mode (EOD reports unaffected)")
if 'bcs' in ALERT_STRUCTURES \
        and 'zebra' not in ALERT_STRUCTURES and not BCS_PAPER_ENABLED:
    logger.warning("alert_structures=['bcs'] but bcs_paper_enabled=false — "
                   "no BCS trades exist to alert on; paper mode will be "
                   "silent. Enable bcs_paper_enabled or add 'zebra'.")
WATCH_GAP_MAX = _runtime['watch_gap_max']
TRIGGER_GAP_MAX = _runtime['trigger_gap_max']
STALE_GAP_MIN = _runtime['stale_gap_min']
FRESHNESS_DAYS = _runtime['freshness_days']
MIN_DTE = _runtime['min_dte']
MAX_DTE = _runtime['max_dte']
MIN_LEG_OI = _runtime['min_leg_oi']
MAX_LEG_SPREAD_PCT = _runtime['max_leg_spread_pct']
TP_TARGET = _runtime['tp_target']
SPOT_SL_ENABLED = _runtime['spot_sl_enabled']
SPOT_SL_PCT = _runtime['spot_sl_pct']
DEBIT_SL_PCT = _runtime['debit_sl_pct']
TIME_SL_DAYS = _runtime['time_sl_days_before_expiry']
MAX_OPEN_TRADES = _runtime['max_open_trades']
MAX_WATCHING_SIGNALS = _runtime['max_watching_signals']
SCAN_INTERVAL_SEC = _runtime['scan_interval_sec']
MONITOR_INTERVAL_SEC = _runtime['monitor_interval_sec']
ENABLED_DIRECTIONS = _runtime['enabled_directions']
ENABLED_TIMEFRAMES = _runtime['enabled_timeframes']
ST_PERIOD = _runtime['st_period']
ST_MULTIPLIER = _runtime['st_multiplier']

SCANNERS = [s for s in _ALL_SCANNERS if s['timeframe'] in ENABLED_TIMEFRAMES]

# ── Helpers ───────────────────────────────────────────────────────────────
def is_trend_aligned(direction: str, st_direction: str) -> bool:
    """True if a Zebra signal is a WITH-TREND pullback (the validated premium
    setup): CE into a rising ST, PE into a falling ST. Counter-trend signals
    still trade (the capped-loss structure keeps them net-positive) — this flag
    is a conviction tag, not a hard filter. Single source of truth so the
    scanner tag and any consumer never diverge.
    """
    return (direction == 'CE' and st_direction == 'UP') or \
           (direction == 'PE' and st_direction == 'DOWN')


# ── Market hours ──────────────────────────────────────────────────────────
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

# ── Quote-reliability guards (2026-07-24 NHPC false DEBIT-SL incident) ──────
# The DEBIT-SL is a VALUE trigger (structure mid <= 50% of entry debit); a
# single garbage opening book once booked a phantom -50% exit. Zebra polls
# every MONITOR_INTERVAL_SEC (~5 min), so require N consecutive RELIABLE
# triggering reads before the alert fires. An unreliable read FREEZES the
# counter (a flickering book must not indefinitely block a genuine exit); a
# reliable non-trigger read resets it; a streak with a poll gap wider than
# CONFIRM_STALE_SEC restarts from zero. Spot-based TP/SPOT-SL stay single-poll
# (spot LTP is real trades). Layered ON TOP of the existing intrinsic-floor guard.
DEBIT_SL_CONFIRM_POLLS = 2       # reliable triggering polls before DEBIT SL alert
CONFIRM_STALE_SEC = 15 * 60      # confirm streak restarts if the poll gap exceeds this
DEBIT_BLIND_CYCLES = 3           # consecutive unusable-quote cycles (~15 min) => one blind alert

# ── Invariant checks ──────────────────────────────────────────────────────
assert WATCH_GAP_MAX > TRIGGER_GAP_MAX, (
    f"WATCH_GAP_MAX ({WATCH_GAP_MAX}) must be > TRIGGER_GAP_MAX ({TRIGGER_GAP_MAX})")
assert TRIGGER_GAP_MAX > STALE_GAP_MIN, (
    f"TRIGGER_GAP_MAX ({TRIGGER_GAP_MAX}) must be > STALE_GAP_MIN ({STALE_GAP_MIN})")
assert MIN_DTE < MAX_DTE, f"MIN_DTE must be < MAX_DTE"
assert MIN_DTE >= 1, f"MIN_DTE must be >= 1"

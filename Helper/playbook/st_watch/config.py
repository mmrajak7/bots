"""ST Watch configuration — paths, symbol list, thresholds."""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()          # playbook/st_watch/
PLAYBOOK_DIR = SCRIPT_DIR.parent                      # playbook/
PROJECT_ROOT = PLAYBOOK_DIR.parent                    # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                       # BOTS/
LOG_DIR = PROJECT_ROOT / 'logs'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'st_watch_config.json'
STATE_FILE = LOG_DIR / 'st_watch_state.json'
ALERT_LOG_FILE = LOG_DIR / 'st_watch_alerts.json'
KITE_TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
TELEGRAM_CONFIG = BOTS_ROOT / 'data' / 'telegram_config.json'

# ── Regime monitor paths ─────────────────────────────────────────────────
REGIME_STATE_FILE = LOG_DIR / 'regime_state.json'
BACKTEST_CACHE_DIR = PLAYBOOK_DIR / 'backtest_cache'
NIFTY_CACHE_FILE = BACKTEST_CACHE_DIR / '_nifty50_daily.json'

# ── Regime defaults ──────────────────────────────────────────────────────
REGIME_DEFAULTS = {
    'enabled': True,
    'rsi_period': 14,
    'rsi_threshold': 50,
    'breadth_threshold': 40,
    'sma_period': 50,
    'unpause_days': 7,
    'nifty_data_days': 100,
    'alert_on_count_progress': True,
    'alert_on_count_reset': True,
}

# ── Supertrend parameters ────────────────────────────────────────────────
ST_PERIOD = 10
ST_MULTIPLIER = 3

# ── Data duration (always fetch 6Y — covers both monthly and weekly) ─────
DATA_YEARS = 6
TIMEFRAMES = ['monthly', 'weekly']

# ── Defaults ──────────────────────────────────────────────────────────────
_DEFAULTS = {
    'alert_thresholds': [3, 1],           # gap % levels that trigger alerts
    'alert_cooldown_hours': 6,           # don't re-alert same threshold within N hours
}


def load_config() -> dict:
    """Load config file, merged with defaults."""
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_cfg = json.load(f)
            for key in _DEFAULTS:
                if key in file_cfg:
                    cfg[key] = file_cfg[key]
            cfg['symbols'] = file_cfg.get('symbols', {})
            cfg['google_drive'] = file_cfg.get('google_drive', {})
            cfg['telegram'] = file_cfg.get('telegram', {})
        except Exception as e:
            logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
            cfg['symbols'] = {}
            cfg['google_drive'] = {}
            cfg['telegram'] = {}
    else:
        logger.warning("Config not found: %s", CONFIG_FILE)
        cfg['symbols'] = {}
        cfg['google_drive'] = {}
        cfg['telegram'] = {}
    return cfg


def load_regime_config() -> dict:
    """Load regime config section, merged with defaults."""
    cfg = dict(REGIME_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_cfg = json.load(f)
            regime = file_cfg.get('regime', {})
            for key in REGIME_DEFAULTS:
                if key in regime:
                    cfg[key] = regime[key]
        except Exception as e:
            logger.warning("Failed to load regime config: %s", e)
    return cfg


def get_all_symbols(cfg: dict) -> list:
    """Flatten symbol config into list of unique symbols with metadata.

    Every symbol is scanned on BOTH monthly and weekly timeframes.
    Returns one entry per symbol (timeframe expansion happens in watcher).
    Deduplicates if same symbol appears in multiple baskets.
    Symbols are uppercased to match Kite API expectations.
    """
    symbols = []
    seen = set()
    for basket, items in cfg.get('symbols', {}).items():
        for symbol, meta in items.items():
            sym_upper = symbol.upper()
            if sym_upper not in seen:
                seen.add(sym_upper)
                symbols.append({
                    'symbol': sym_upper,
                    'exchange': meta.get('exchange', 'NSE').upper(),
                    'basket': basket,
                })
    return symbols

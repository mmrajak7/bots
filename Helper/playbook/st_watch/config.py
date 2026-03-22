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
KITE_TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
TELEGRAM_CONFIG = BOTS_ROOT / 'data' / 'telegram_config.json'

# ── Supertrend parameters ────────────────────────────────────────────────
ST_PERIOD = 10
ST_MULTIPLIER = 3

# ── Data duration (always fetch 6Y — covers both monthly and weekly) ─────
DATA_YEARS = 6
TIMEFRAMES = ['monthly', 'weekly']

# ── Defaults ──────────────────────────────────────────────────────────────
_DEFAULTS = {
    'alert_thresholds': [5, 3, 1],      # gap % levels that trigger alerts
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
        except Exception as e:
            logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
            cfg['symbols'] = {}
    else:
        logger.warning("Config not found: %s", CONFIG_FILE)
        cfg['symbols'] = {}
    return cfg


def get_all_symbols(cfg: dict) -> list:
    """Flatten symbol config into list of unique symbols with metadata.

    Every symbol is scanned on BOTH monthly and weekly timeframes.
    Returns one entry per symbol (timeframe expansion happens in watcher).
    Deduplicates if same symbol appears in multiple baskets.
    """
    symbols = []
    seen = set()
    for basket, items in cfg.get('symbols', {}).items():
        for symbol, meta in items.items():
            if symbol not in seen:
                seen.add(symbol)
                symbols.append({
                    'symbol': symbol,
                    'exchange': meta.get('exchange', 'NSE'),
                    'basket': basket,
                })
    return symbols

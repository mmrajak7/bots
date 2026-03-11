"""Configuration loader for stock_analyzer package."""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Path resolution
PACKAGE_DIR = Path(__file__).resolve().parent
HELPER_DIR = PACKAGE_DIR.parent
BOTS_DIR = HELPER_DIR.parent
CONFIG_DIR = HELPER_DIR / "config"
DATA_DIR = HELPER_DIR / "data"
CACHE_DIR = DATA_DIR / "cache" / "stock_analyzer"
KITE_TOKEN_FILE = BOTS_DIR / "data" / "kite_access_token.json"

CONFIG_FILE = CONFIG_DIR / "stock_analyzer_config.json"

# Default config (used if config file missing)
DEFAULT_CONFIG = {
    "cache": {
        "enabled": True,
        "dir": str(CACHE_DIR),
        "ttl_screener_hours": 4,
        "ttl_trendlyne_hours": 4,
        "ttl_kite_hours": 1
    },
    "scoring_weights": {
        "fundamental": 0.35,
        "valuation": 0.25,
        "technical": 0.20,
        "momentum": 0.10,
        "risk": 0.10
    },
    "thresholds": {
        "verdict_buy_min": 70,
        "verdict_wait_min": 50,
        "verdict_sell_max": 35,
        "pe_cheap": 15,
        "pe_expensive": 40,
        "roce_strong": 20,
        "roce_weak": 10,
        "peg_undervalued": 1.0,
        "peg_overvalued": 2.0,
        "piotroski_strong": 7,
        "piotroski_weak": 3,
        "altman_safe": 2.99,
        "altman_distress": 1.81,
        "debt_equity_low": 0.5,
        "debt_equity_high": 1.5,
        "rsi_oversold": 30,
        "rsi_overbought": 70,
        "promoter_decline_warning_pct": 2.0,
        "promoter_low_warning_pct": 30.0,
        "pledge_warning_pct": 20.0
    },
    "scraping": {
        "screener_delay_sec": 2,
        "trendlyne_delay_sec": 3,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "request_timeout_sec": 15
    },
    "kite": {
        "lookback_days": 365,
        "rsi_period": 14,
        "macd_fast": 12,
        "macd_slow": 26,
        "macd_signal": 9,
        "dma_short": 50,
        "dma_long": 200,
        "volume_ma_period": 20,
        "bollinger_period": 20,
        "bollinger_std": 2
    },
    "financial_sectors": [
        "bank", "banking", "nbfc", "finance", "insurance",
        "financial services", "housing finance", "microfinance"
    ]
}


def load_config() -> dict:
    """Load config from JSON file, falling back to defaults."""
    config = DEFAULT_CONFIG.copy()
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r") as f:
                file_config = json.load(f)
            # Deep merge: file_config overrides defaults
            for section, values in file_config.items():
                if isinstance(values, dict) and section in config:
                    config[section].update(values)
                else:
                    config[section] = values
            logger.info(f"Loaded config from {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Failed to load config file, using defaults: {e}")
    else:
        logger.info("No config file found, using defaults")
        # Write defaults for user to customize
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with open(CONFIG_FILE, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
            logger.info(f"Created default config at {CONFIG_FILE}")
        except Exception as e:
            logger.warning(f"Could not write default config: {e}")
    return config


def get_kite_token() -> Optional[dict]:
    """Load Kite access token."""
    if not KITE_TOKEN_FILE.exists():
        logger.error(f"Kite token not found at {KITE_TOKEN_FILE}")
        return None
    try:
        with open(KITE_TOKEN_FILE) as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load Kite token: {e}")
        return None

"""
neo.config — Configuration loader and validation.

Reads strategy params from config_trade.yaml and secrets from neo_credentials.yaml.
Applies safe defaults, validates required fields.
"""

import os
import logging
import yaml

log = logging.getLogger('neo.config')

# Package root (neo/ directory)
_PKG_DIR = os.path.dirname(os.path.realpath(__file__))
# Project root (ETF_PI/ directory)
_PROJECT_DIR = os.path.dirname(_PKG_DIR)


class NeoConfig:
    """Loads and validates Neo bridge configuration."""

    def __init__(self, trade_config_path=None):
        if trade_config_path is None:
            trade_config_path = os.path.join(_PROJECT_DIR, 'config_trade.yaml')

        # Load strategy config
        self._trade = {}
        if os.path.exists(trade_config_path):
            with open(trade_config_path) as f:
                self._trade = yaml.safe_load(f) or {}

        # Load credentials from separate file
        creds_path = self._trade.get('neo_credentials_file',
                                     os.path.join(_PROJECT_DIR, 'neo_credentials.yaml'))
        if not os.path.isabs(creds_path):
            creds_path = os.path.join(_PROJECT_DIR, creds_path)

        self._creds = {}
        if os.path.exists(creds_path):
            with open(creds_path) as f:
                raw = yaml.safe_load(f) or {}
                self._creds = raw.get('neo_credentials', {})
        else:
            log.warning(f"Credentials file not found: {creds_path}")

        self._validate()

    # ----- Strategy params -----

    @property
    def enabled(self):
        return bool(self._trade.get('trade_enabled', False))

    @property
    def log_only(self):
        return bool(self._trade.get('log_only', True))

    @property
    def num_lots(self):
        return int(self._trade.get('num_lots', 1))

    @property
    def max_concurrent_positions(self):
        return int(self._trade.get('max_concurrent_positions', 3))

    @property
    def product(self):
        return str(self._trade.get('product', 'MIS'))

    @property
    def entry_tick_buffer(self):
        return float(self._trade.get('entry_tick_buffer', 0.15))

    @property
    def exit_tick_buffer(self):
        return float(self._trade.get('exit_tick_buffer', 0.10))

    @property
    def tp_order_enabled(self):
        return bool(self._trade.get('tp_order_enabled', True))

    @property
    def time_sl(self):
        return self._trade.get('time_sl', {})

    @property
    def max_loss_per_day(self):
        return float(self._trade.get('max_loss_per_day', 10000.0))

    @property
    def max_lots_per_order(self):
        return int(self._trade.get('max_lots_per_order', 5))

    @property
    def market_protection(self):
        return str(self._trade.get('market_protection', '0'))

    @property
    def log_dir(self):
        d = self._trade.get('log_dir', 'logs')
        if not os.path.isabs(d):
            d = os.path.join(_PROJECT_DIR, d)
        return d

    # ----- Neo credentials -----

    @property
    def consumer_key(self):
        return str(self._creds.get('consumer_key', ''))

    @property
    def mobile_number(self):
        return str(self._creds.get('mobile_number', ''))

    @property
    def ucc(self):
        return str(self._creds.get('ucc', ''))

    @property
    def mpin(self):
        return str(self._creds.get('mpin', ''))

    @property
    def totp_secret(self):
        return str(self._creds.get('totp_secret', ''))

    @property
    def static_ip(self):
        return str(self._creds.get('static_ip', ''))

    # ----- Data directory -----

    @property
    def data_dir(self):
        return os.path.join(_PKG_DIR, 'data')

    def _validate(self):
        """Log warnings for missing config. Don't crash — log_only mode needs no creds.

        SEBI April 2026 API Trading compliance checklist:
          1. Static IP whitelisting — checked below
          2. Rate limiting (10 OPS) — enforced in NeoTrader._rate_limit()
          3. Unique order tags — enforced in OrderTracker.generate_tag()
          4. Kill switch / circuit breaker — enforced in NeoTrader._update_circuit_breaker()
          5. Market protection — configured via market_protection param (='0' for LIMIT orders)
          6. All orders are LIMIT only — MARKET orders are never placed
        """
        if self.enabled and not self.log_only:
            missing = [f for f in ('consumer_key', 'mobile_number', 'ucc', 'mpin', 'totp_secret')
                       if not getattr(self, f)]
            if missing:
                log.error(f"Neo credentials missing: {', '.join(missing)} — "
                          "live trading will fail")

        if self.enabled and not self.static_ip:
            log.warning("SEBI MANDATE: static_ip not configured. "
                        "Order placement will be rejected by exchange. "
                        "Register at: Kotak Neo App > More > Trade API")

        os.makedirs(self.data_dir, exist_ok=True)
        os.makedirs(self.log_dir, exist_ok=True)

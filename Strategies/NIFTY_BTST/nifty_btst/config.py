"""Configuration: tracked defaults + optional untracked overlay.

`btst_config.defaults.json` is tracked and reaches the Pi with `git pull`.
`btst_config.json` (git-ignored) overrides individual keys on one box. The
overlay WINS, key by key; `fee_rates` merges one level deep so an overlay can
change one rate without restating the rest.

Every key the engine reads is validated here, at load, so a typo fails the
process at 09:00 rather than quietly changing a trading rule at 10:47.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, Optional

PKG_DIR = Path(__file__).resolve().parent
APP_DIR = PKG_DIR.parent                      # Strategies/NIFTY_BTST
BOTS_ROOT = APP_DIR.parents[1]                # BOTS
DEFAULTS_FILE = APP_DIR / 'btst_config.defaults.json'
OVERLAY_FILE = APP_DIR / 'btst_config.json'

KITE_TOKEN_FILE = Path(os.environ.get(
    'BTST_KITE_TOKEN_FILE', BOTS_ROOT / 'data' / 'kite_access_token.json'))
TELEGRAM_CONFIG = Path(os.environ.get(
    'BTST_TELEGRAM_CONFIG', BOTS_ROOT / 'data' / 'telegram_config.json'))
STATE_DIR = Path(os.environ.get('BTST_STATE_DIR', APP_DIR / 'state'))

_FEE_KEYS = ('brokerage_per_order', 'stt_sell_pct', 'exchange_pct',
             'sebi_pct', 'stamp_buy_pct', 'gst_pct')


class ConfigError(ValueError):
    pass


def _hms(value: str, key: str) -> dtime:
    try:
        h, m, s = (int(x) for x in str(value).split(':'))
        return dtime(h, m, s)
    except Exception:
        raise ConfigError('%s must be HH:MM:SS, got %r' % (key, value))


def _pos(raw: Dict[str, Any], key: str) -> float:
    try:
        v = float(raw[key])
    except (KeyError, TypeError, ValueError):
        raise ConfigError('%s missing or not a number' % key)
    if v <= 0:
        raise ConfigError('%s must be > 0, got %r' % (key, v))
    return v


def _strict_bool(raw: Dict[str, Any], key: str) -> bool:
    # A string "false" is truthy; a typo must not flip a switch.
    v = raw.get(key)
    if not isinstance(v, bool):
        raise ConfigError('%s must be true or false (JSON boolean), got %r'
                          % (key, v))
    return v


@dataclass(frozen=True)
class Config:
    underlying: str
    index_quote_symbol: str
    index_token: int
    option_exchange: str
    target_premium: float
    trigger_mult: float
    stop_frac: float
    lots: int
    strike_band_points: float
    market_open: dtime
    mark_time: dtime
    late_mark_after: dtime
    entry_start: dtime
    entry_end: dtime
    next_day_exit: dtime
    next_day_exit_grace_sec: int
    market_close: dtime
    poll_seconds: float
    poll_log_every_sec: int
    quote_retries: int
    quote_fail_alert_after: int
    max_spread_abs: float
    max_spread_pct: float
    fee_rates: Dict[str, float] = field(default_factory=dict)
    telegram_enabled: bool = True
    telegram_prefix: str = 'NIFTY-BTST (paper)'


def _merge(defaults: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(defaults)
    for k, v in overlay.items():
        if k == 'fee_rates' and isinstance(v, dict):
            out[k] = {**defaults.get(k, {}), **v}
        else:
            out[k] = v
    return out


def build(raw: Dict[str, Any]) -> Config:
    """Validate a merged dict into a Config. Separate from `load` for tests."""
    fees = raw.get('fee_rates') or {}
    for k in _FEE_KEYS:
        if k not in fees or float(fees[k]) < 0:
            raise ConfigError('fee_rates.%s missing or negative' % k)
    lots = int(_pos(raw, 'lots'))
    if lots != float(raw['lots']):
        raise ConfigError('lots must be a whole number')
    stop_frac = _pos(raw, 'stop_frac')
    if stop_frac >= 1:
        raise ConfigError('stop_frac must be < 1 (it is a fraction of entry)')
    trigger_mult = _pos(raw, 'trigger_mult')
    if trigger_mult <= 1:
        raise ConfigError('trigger_mult must be > 1')
    cfg = Config(
        underlying=str(raw['underlying']),
        index_quote_symbol=str(raw['index_quote_symbol']),
        index_token=int(raw['index_token']),
        option_exchange=str(raw['option_exchange']),
        target_premium=_pos(raw, 'target_premium'),
        trigger_mult=trigger_mult,
        stop_frac=stop_frac,
        lots=lots,
        strike_band_points=_pos(raw, 'strike_band_points'),
        market_open=_hms(raw['market_open'], 'market_open'),
        mark_time=_hms(raw['mark_time'], 'mark_time'),
        late_mark_after=_hms(raw['late_mark_after'], 'late_mark_after'),
        entry_start=_hms(raw['entry_start'], 'entry_start'),
        entry_end=_hms(raw['entry_end'], 'entry_end'),
        next_day_exit=_hms(raw['next_day_exit'], 'next_day_exit'),
        next_day_exit_grace_sec=int(_pos(raw, 'next_day_exit_grace_sec')),
        market_close=_hms(raw['market_close'], 'market_close'),
        poll_seconds=_pos(raw, 'poll_seconds'),
        poll_log_every_sec=int(_pos(raw, 'poll_log_every_sec')),
        quote_retries=int(_pos(raw, 'quote_retries')),
        quote_fail_alert_after=int(_pos(raw, 'quote_fail_alert_after')),
        max_spread_abs=_pos(raw, 'max_spread_abs'),
        max_spread_pct=_pos(raw, 'max_spread_pct'),
        fee_rates={k: float(fees[k]) for k in _FEE_KEYS},
        telegram_enabled=_strict_bool(raw, 'telegram_enabled'),
        telegram_prefix=str(raw.get('telegram_prefix', 'NIFTY-BTST (paper)')),
    )
    order = [cfg.market_open, cfg.mark_time, cfg.entry_start, cfg.entry_end,
             cfg.market_close]
    if order != sorted(order) or len(set(order)) != len(order):
        raise ConfigError('session times must satisfy market_open < mark_time '
                          '< entry_start < entry_end < market_close')
    if not (cfg.market_open <= cfg.next_day_exit < cfg.entry_end):
        raise ConfigError('next_day_exit must fall inside the session')
    if cfg.poll_seconds < 1:
        # Kite's quote endpoint allows ~1 req/s per API key, shared with
        # zebra and the BCS monitor on the same box.
        raise ConfigError('poll_seconds below 1 would exceed the Kite rate limit')
    return cfg


def load(defaults_path: Optional[Path] = None,
         overlay_path: Optional[Path] = None) -> Config:
    defaults_path = defaults_path or DEFAULTS_FILE
    overlay_path = overlay_path or OVERLAY_FILE
    with open(defaults_path, encoding='utf-8') as f:
        raw = json.load(f)
    if overlay_path.exists():
        with open(overlay_path, encoding='utf-8') as f:
            raw = _merge(raw, json.load(f))
    return build(raw)

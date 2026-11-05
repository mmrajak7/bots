"""Live-only configuration. The paper process never reads any of it.

Sources, merged in this order (later wins, `neo` one level deep):
  1. `btst_config.defaults.json`   tracked: the `execution` key and the `neo` block
  2. `btst_live_config.json`       untracked LIVE overlay: `execution`, `neo.*`
The paper overlay `btst_config.json` is NOT read here and the paper loader
ignores `execution` and `neo` entirely (`config.build` reads only its own
keys), so a typo made while arming cannot stop the paper trader (NEO-13) and
tuning the live poll cannot move the frozen paper test (NEO-22).

Hard limits that the design puts in CODE, not config (section 7), are module
constants here and are checked at live start:
  MAX_LIVE_LOTS = 1         the config's `lots` may not exceed it in live mode
  EXECUTION_MODES           paper | rehearsal | live
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import APP_DIR, BOTS_ROOT, DEFAULTS_FILE, ConfigError, _hms, _pos

EXECUTION_MODES = ('paper', 'rehearsal', 'live')
#: every key the neo block understands; anything else is reported by name in
#: the arming line (R3-19), because a stale or misspelt key is silently inert
KNOWN_NEO_KEYS = frozenset((
    'credentials_file', 'login_url', 'exchange_segment', 'product', 'http_timeout_sec',
    'login_timeout_sec', 'poll_seconds', 'entry_cap_pct', 'entry_timeout_sec',
    'max_entry_attempts', 'exit_chase_sec', 'exit_chase_fast_max', 'exit_retry_slow_sec',
    'unknown_recheck_sec', 'unknown_grace_sec', 'kill_drawdown_rs', 'max_premium_rs',
    'max_entry_orders_per_day', 'max_orders_per_day', 'funds_margin_pct', 'limits_net_field',
    'reconcile_sec', 'order_poll_sec', 'expiry_backstop_time', 'sell_refused_alert_sec',
    'exit_defer_alert_sec', 'brokerage_per_order', 'scrip_master_max_age_hours',
    'fake_fill_delay_sec', 'telegram_prefix_live', 'telegram_prefix_rehearsal', 'volrix_csv',
    'arm_max_days', 'empty_report_stcode', 'empty_report_emsg', 'empty_ok_without_data',
    'empty_report_order_book', 'empty_report_positions', 'empty_report_trade_book',
    'empty_report_holdings', 'auth_stcodes', 'transient_stcodes', 'rejected_stcodes',
    'holdings_source_enabled', 'position_qty_unit', 'positions_lag_sec', 'telegram_enabled',
    'retry_stcodes'))
#: paper rule keys that must not drift between the paper overlay and the
#: tracked defaults without the owner knowing (R3-07)
RULE_KEYS = ('target_premium', 'trigger_mult', 'stop_frac', 'lots', 'market_open', 'mark_time',
             'late_mark_after', 'entry_start', 'entry_end', 'next_day_exit',
             'next_day_exit_grace_sec', 'market_close', 'max_spread_abs', 'max_spread_pct')
MAX_LIVE_LOTS = 1
LIVE_OVERLAY_FILE = APP_DIR / 'btst_live_config.json'
QTY_UNITS = ('shares', 'lots')


@dataclass(frozen=True)
class NeoConfig:
    credentials_file: Path
    login_url: str
    exchange_segment: str
    product: str
    http_timeout_sec: float
    login_timeout_sec: float
    poll_seconds: float
    entry_cap_pct: float
    entry_timeout_sec: float
    max_entry_attempts: int
    exit_chase_sec: float
    exit_chase_fast_max: int
    exit_retry_slow_sec: float
    unknown_recheck_sec: float
    unknown_grace_sec: float
    kill_drawdown_rs: float
    max_premium_rs: float
    max_entry_orders_per_day: int
    max_orders_per_day: int
    funds_margin_pct: float
    limits_net_field: str
    reconcile_sec: float
    order_poll_sec: float
    expiry_backstop_time: dtime
    sell_refused_alert_sec: float
    exit_defer_alert_sec: float
    brokerage_per_order: float
    scrip_master_max_age_hours: float
    fake_fill_delay_sec: float
    telegram_prefix_live: str
    telegram_prefix_rehearsal: str
    volrix_csv: Optional[Path]
    arm_max_days: int
    empty_report_stcode: Optional[int]
    empty_report_emsg: str
    empty_ok_without_data: bool
    empty_per_report: Dict[str, Tuple[Optional[int], str]]
    auth_stcodes: Tuple[int, ...]
    transient_stcodes: Tuple[int, ...]
    rejected_stcodes: Tuple[int, ...]
    holdings_source_enabled: bool
    position_qty_unit: str
    positions_lag_sec: float
    telegram_enabled: bool
    retry_stcodes: Tuple[int, ...]
    unknown_keys: Tuple[str, ...]


def _nonneg(raw: Dict[str, Any], key: str) -> float:
    try:
        v = float(raw[key])
    except (KeyError, TypeError, ValueError):
        raise ConfigError('neo.%s missing or not a number' % key)
    if v < 0:
        raise ConfigError('neo.%s must be >= 0' % key)
    return v


def _int(raw: Dict[str, Any], key: str) -> int:
    v = _pos(raw, key)
    if v != int(v):
        raise ConfigError('neo.%s must be a whole number' % key)
    return int(v)


def _bool(raw: Dict[str, Any], key: str, default: bool) -> bool:
    v = raw.get(key, default)
    if not isinstance(v, bool):
        raise ConfigError('neo.%s must be true or false, got %r' % (key, v))
    return v


def _codes(raw: Dict[str, Any], key: str, default: List[int]) -> Tuple[int, ...]:
    v = raw.get(key, default)
    if not isinstance(v, list):
        raise ConfigError('neo.%s must be a list of integers' % key)
    try:
        return tuple(int(x) for x in v)
    except (TypeError, ValueError):
        raise ConfigError('neo.%s must be a list of integers' % key)


def _path(raw: Dict[str, Any], key: str) -> Optional[Path]:
    v = raw.get(key)
    if not v:
        return None
    p = Path(str(v))
    return p if p.is_absolute() else BOTS_ROOT / p


def build_neo(raw: Any) -> NeoConfig:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError('config has no "neo" block')
    cred = _path(raw, 'credentials_file')
    if cred is None:
        raise ConfigError('neo.credentials_file missing')
    stcode = raw.get('empty_report_stcode')
    try:
        empty_code = None if stcode in (None, '') else int(str(stcode))
    except (TypeError, ValueError):
        raise ConfigError('neo.empty_report_stcode must be an integer or null')
    per_report: Dict[str, Tuple[Optional[int], str]] = {}
    for name in ('order book', 'positions', 'trade book', 'holdings'):
        key = 'empty_report_' + name.replace(' ', '_')
        v = raw.get(key)
        if v is None:
            continue
        if not isinstance(v, dict) or 'emsg' not in v:
            raise ConfigError('neo.%s must be {"stcode": int|null, "emsg": str}' % key)
        try:
            c = v.get('stcode')
            per_report[name] = (None if c in (None, '') else int(str(c)), str(v['emsg']))
        except (TypeError, ValueError):
            raise ConfigError('neo.%s.stcode must be an integer or null' % key)
    cfg = NeoConfig(
        credentials_file=cred,
        login_url=str(raw.get('login_url', 'https://mis.kotaksecurities.com')),
        exchange_segment=str(raw.get('exchange_segment', 'nse_fo')),
        product=str(raw.get('product', 'NRML')),
        http_timeout_sec=_pos(raw, 'http_timeout_sec'),
        login_timeout_sec=_pos(raw, 'login_timeout_sec'),
        poll_seconds=_pos(raw, 'poll_seconds'),
        entry_cap_pct=_nonneg(raw, 'entry_cap_pct'),
        entry_timeout_sec=_pos(raw, 'entry_timeout_sec'),
        max_entry_attempts=_int(raw, 'max_entry_attempts'),
        exit_chase_sec=_pos(raw, 'exit_chase_sec'),
        exit_chase_fast_max=_int(raw, 'exit_chase_fast_max'),
        exit_retry_slow_sec=_pos(raw, 'exit_retry_slow_sec'),
        unknown_recheck_sec=_pos(raw, 'unknown_recheck_sec'),
        unknown_grace_sec=_pos(raw, 'unknown_grace_sec'),
        kill_drawdown_rs=_pos(raw, 'kill_drawdown_rs'),
        max_premium_rs=_pos(raw, 'max_premium_rs'),
        max_entry_orders_per_day=_int(raw, 'max_entry_orders_per_day'),
        max_orders_per_day=_int(raw, 'max_orders_per_day'),
        funds_margin_pct=_nonneg(raw, 'funds_margin_pct'),
        limits_net_field=str(raw.get('limits_net_field', 'Net')),
        reconcile_sec=_pos(raw, 'reconcile_sec'),
        order_poll_sec=_pos(raw, 'order_poll_sec'),
        expiry_backstop_time=_hms(raw.get('expiry_backstop_time', '09:30:00'),
                                  'neo.expiry_backstop_time'),
        sell_refused_alert_sec=_pos(raw, 'sell_refused_alert_sec'),
        exit_defer_alert_sec=_pos(raw, 'exit_defer_alert_sec'),
        brokerage_per_order=_nonneg(raw, 'brokerage_per_order'),
        scrip_master_max_age_hours=_pos(raw, 'scrip_master_max_age_hours'),
        fake_fill_delay_sec=_nonneg(raw, 'fake_fill_delay_sec'),
        telegram_prefix_live=str(raw.get('telegram_prefix_live', 'NIFTY-BTST (LIVE)')),
        telegram_prefix_rehearsal=str(raw.get('telegram_prefix_rehearsal',
                                              'NIFTY-BTST (REHEARSAL)')),
        volrix_csv=_path(raw, 'volrix_csv'),
        arm_max_days=_int(raw, 'arm_max_days'),
        empty_report_stcode=empty_code,
        empty_report_emsg=str(raw.get('empty_report_emsg', 'No Data')),
        empty_ok_without_data=_bool(raw, 'empty_ok_without_data', False),
        empty_per_report=per_report,
        auth_stcodes=_codes(raw, 'auth_stcodes', [1003]),
        transient_stcodes=_codes(raw, 'transient_stcodes', [1005]),
        rejected_stcodes=_codes(raw, 'rejected_stcodes', [1004, 1006, 1007, 1009]),
        holdings_source_enabled=_bool(raw, 'holdings_source_enabled', False),
        position_qty_unit=str(raw.get('position_qty_unit', 'shares')),
        positions_lag_sec=_nonneg(raw, 'positions_lag_sec'),
        telegram_enabled=_bool(raw, 'telegram_enabled', True),
        retry_stcodes=_codes(raw, 'retry_stcodes', [100008, 429]),
        unknown_keys=tuple(sorted(k for k in raw if k not in KNOWN_NEO_KEYS)),
    )
    if cfg.product != 'NRML':
        # An overnight hold under MIS is squared off by the broker at ~15:20.
        raise ConfigError('neo.product must be NRML for an overnight hold')
    if cfg.max_orders_per_day < cfg.max_entry_orders_per_day:
        raise ConfigError('neo.max_orders_per_day must be >= max_entry_orders_per_day')
    if cfg.exit_retry_slow_sec < cfg.exit_chase_sec:
        raise ConfigError('neo.exit_retry_slow_sec must be >= exit_chase_sec')
    if cfg.unknown_grace_sec < cfg.unknown_recheck_sec:
        raise ConfigError('neo.unknown_grace_sec must be >= unknown_recheck_sec')
    if cfg.position_qty_unit not in QTY_UNITS:
        raise ConfigError('neo.position_qty_unit must be one of %s' % (QTY_UNITS,))
    if cfg.poll_seconds < 1:
        raise ConfigError('neo.poll_seconds below 1 would exceed the Kite rate limit')
    return cfg


def rule_diffs(cfg: Any, other: Any = None) -> List[str]:
    """Rule values where `cfg` differs from `other`: a Config (the paper twin,
    R4-04) or a defaults JSON path (default: the tracked file). Information
    for the arming line; it blocks nothing."""
    out = []
    if other is None or isinstance(other, (str, Path)):
        with open(other or DEFAULTS_FILE, encoding='utf-8') as f:
            raw = json.load(f)
        values = {k: raw.get(k) for k in RULE_KEYS}
        label = 'tracked'
    else:
        values = {k: getattr(other, k, None) for k in RULE_KEYS}
        label = 'paper'
    for k in RULE_KEYS:
        want = values.get(k)
        have = getattr(cfg, k, None)
        if want is None or have is None:
            continue
        if isinstance(have, dtime):
            same = str(have) == (str(want) if isinstance(want, dtime) else str(_hms(want, k)))
        else:
            same = float(have) == float(want)
        if not same:
            out.append('%s=%s (%s %s)' % (k, have, label, want))
    return out


def load_live(defaults_path: Optional[Path] = None,
              overlay_path: Optional[Path] = None) -> Tuple[str, NeoConfig]:
    """(execution, NeoConfig) from the tracked defaults + the LIVE overlay."""
    defaults_path = defaults_path or DEFAULTS_FILE
    overlay_path = overlay_path or LIVE_OVERLAY_FILE
    with open(defaults_path, encoding='utf-8') as f:
        raw = json.load(f)
    execution = raw.get('execution', 'paper')
    neo = dict(raw.get('neo') or {}) if isinstance(raw.get('neo'), dict) else {}
    if overlay_path.exists():
        with open(overlay_path, encoding='utf-8') as f:
            ov = json.load(f)
        if not isinstance(ov, dict):
            raise ConfigError('%s must hold a JSON object' % overlay_path)
        if 'execution' in ov:
            execution = ov['execution']
        if 'neo' in ov:
            if not isinstance(ov['neo'], dict):
                raise ConfigError('%s: "neo" must be an object' % overlay_path)
            neo.update(ov['neo'])
    if execution not in EXECUTION_MODES:
        raise ConfigError('execution must be one of %s, got %r' % (EXECUTION_MODES, execution))
    return str(execution), build_neo(neo)

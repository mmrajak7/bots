"""Executor configuration: tracked defaults + the box's untracked overlay.

Spec: docs/EXECUTOR_SPEC.md (O9 arming, every numeric rule's knob).
WHY strict: a typo in the overlay (`mod: live`) must stop the daemon, not run it
with a default the owner did not mean.
"""
from __future__ import annotations

import datetime as _dt
import math
import os
from typing import Any, Dict

import yaml

HERE = os.path.dirname(os.path.realpath(__file__))
ETF_DIR = os.path.dirname(HERE)
DEFAULTS_PATH = os.path.join(HERE, 'defaults.yaml')
LOCAL_PATH = os.path.join(ETF_DIR, 'executor_local.yaml')
STATE_DIR = os.path.join(ETF_DIR, 'state', 'executor')
MODES = ('off', 'paper', 'live')


class ConfigError(ValueError):
    pass


def _read(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding='utf-8') as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError('%s: not a mapping' % path)
    return data


def _hhmm(v: Any, key: str) -> _dt.time:
    try:
        h, m = str(v).split(':')
        return _dt.time(int(h), int(m))
    except (ValueError, TypeError):
        raise ConfigError('%s=%r is not HH:MM' % (key, v))


def load(defaults_path: str = DEFAULTS_PATH, local_path: str = LOCAL_PATH,
         override: Dict[str, Any] = None) -> Dict[str, Any]:
    """Merged + validated config. Raises ConfigError on anything doubtful."""
    cfg = _read(defaults_path)
    if not cfg:
        raise ConfigError('executor defaults missing at %s' % defaults_path)
    for src, extra in (('local', _read(local_path)), ('override', override or {})):
        unknown = sorted(set(extra) - set(cfg))
        if unknown:
            raise ConfigError('%s config has unknown keys: %s' % (src, ', '.join(unknown)))
        cfg.update(extra)
    return validate(cfg)


def validate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    c = dict(cfg)
    if c['mode'] not in MODES:
        raise ConfigError('mode=%r: one of %s' % (c['mode'], MODES))
    lots = c['lots']
    if not isinstance(lots, int) or isinstance(lots, bool) or lots < 1:
        raise ConfigError('lots=%r must be a whole number >= 1' % lots)
    ceiling = int(math.floor(float(c['capital']) / float(c['capital_per_lot'])))
    if lots > ceiling:
        raise ConfigError('lots=%d is above the ceiling floor(%s / %s) = %d'
                          % (lots, c['capital'], c['capital_per_lot'], ceiling))
    c['lot_ceiling'] = ceiling
    lu = str(c.get('live_until') or '').strip()
    c['live_until_date'] = None
    if lu:
        try:
            c['live_until_date'] = _dt.date.fromisoformat(lu)
        except ValueError:
            raise ConfigError('live_until=%r is not YYYY-MM-DD' % lu)
    if c['mode'] == 'live' and c['live_until_date'] is None:
        raise ConfigError('mode=live needs live_until (YYYY-MM-DD)')
    c['last_entry_time'] = _hhmm(c['last_entry'], 'last_entry')
    c['eod_time_t'] = _hhmm(c['eod_time'], 'eod_time') if str(c.get('eod_time') or '').strip() else None
    if c['eod_time_t'] and c['eod_time_t'] <= c['last_entry_time']:
        raise ConfigError('eod_time %s must be after last_entry %s' % (c['eod_time'], c['last_entry']))
    for k in ('stop_placing', 'carry_resume'):
        try:
            c[k + '_time'] = _dt.time.fromisoformat(str(c[k]))
        except ValueError:
            raise ConfigError('%s=%r is not HH:MM:SS' % (k, c[k]))
    for k in ('intent_max_age_s', 'entry_reprice_s', 'floor_persist_s', 'exit_step_s',
              'price_stale_s', 'rest_poll_s', 'no_price_alert_s', 'no_price_flatten_s',
              'unclear_grace_s', 'loop_s', 'reconcile_s'):
        if float(c[k]) <= 0:
            raise ConfigError('%s must be > 0' % k)
    if not (0 < float(c['no_price_alert_s']) < float(c['no_price_flatten_s'])):
        raise ConfigError('no_price_alert_s must be below no_price_flatten_s')
    # K8: below the exit step, so one stuck refresh costs at most one chase step
    if not (0 <= float(c['neo_refresh_wait_s']) <= float(c['exit_step_s'])):
        raise ConfigError('neo_refresh_wait_s must be 0..exit_step_s (%s)' % c['exit_step_s'])
    steps = c['exit_steps_pct']
    if not isinstance(steps, list) or any(float(x) <= 0 for x in steps):
        raise ConfigError('exit_steps_pct must be a list of positive percents')
    if int(c['entry_attempts']) < 1:
        raise ConfigError('entry_attempts must be >= 1')
    if c['product'] not in ('NRML', 'MIS'):
        raise ConfigError('product=%r: NRML or MIS' % c['product'])
    if not str(c['neo_bot']).isalpha():
        raise ConfigError('neo_bot must be letters')
    if not c.get('bots_root'):
        here_bots = os.path.dirname(ETF_DIR)
        pi_default = '/home/trustit/Desktop/BOTS'
        c['bots_root'] = (here_bots if os.path.isdir(os.path.join(here_bots, 'kotak_neo'))
                          else pi_default)
    return c


def live_allowed(cfg: Dict[str, Any], today: _dt.date) -> bool:
    """O9: live only while today <= live_until."""
    return cfg['mode'] == 'live' and cfg['live_until_date'] is not None \
        and today <= cfg['live_until_date']

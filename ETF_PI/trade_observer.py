"""trade_observer.py — flight recorder for the trade lifecycle (ENTRY -> EXIT).

WHY THIS EXISTS
---------------
The bot's own ledger records where a trade started and where it ended. It does not
record whether the journey between those points matched the plan. That gap is not
academic: measured on the 151-trade ledger (2026-08-19), **16 trades filled more than
1% BELOW the level their own stop rule named**, giving up Rs6,103 — 37% of the book's
Rs16,510 gross P&L. The worst single case, NIFTY_CE_10minute_20260508, had a COST_SL
floor of 176.30 and filled at 153.10: -13.2%, -Rs1,508.

Nothing in the system reports that today. `_close_pos`'s FORENSICS line logs peak,
drift and hold time — not the distance between the level a rule named and the price
that was actually booked. The only way to find it is by hand, months later, which is
exactly how it WAS found.

So this module records the flight: the plan at entry, the trajectory every tick, and
at exit the arithmetic that says whether the rule that fired was entitled to fire, and
how far the fill landed from the level it pointed at.

DESIGN RULE: RECORD NUMBERS, DERIVE BOOLEANS OFFLINE
----------------------------------------------------
This module deliberately does NOT re-evaluate the exit conditions. Duplicating the
`monitor_positions()` decision ladder here would create a second copy of the trading
rules that can silently drift from the first — and a conformance checker that has
drifted from the code it checks is worse than none, because it reports confidently.

Instead each tick records the *distance* to every trigger (pure arithmetic over values
already present in the position dict). Every boolean the analysis needs — "was COST_SL
true here?", "did TIME_SL come due three ticks before it fired?" — is recoverable
exactly from those distances, offline, by a reader that can be fixed without touching
the bot.

SAFETY
------
Observation must never affect trading. Every public entry point is wrapped in
`_obs_safe`, which swallows and logs any exception, mirroring `_shadow_safe` in
option_buy.py. A broken recorder is acceptable; a broken exit path is not. Every call
site in option_buy.py is a BARE STATEMENT whose return value is never assigned or
branched on — enforced structurally by Tests/test_trade_observer.py.
"""
import os
import json
import datetime

import pytz

IST = pytz.timezone('Asia/Kolkata')
CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))

#: Master switch. Off => every hook is a no-op and no file is touched.
OBSERVER_ENABLED = True

#: Append-only, one JSON object per line. Not date-suffixed: a trade opened before
#: midnight-rollover edge cases and the replay tooling both prefer a single stream,
#: and the volume is trivial (~1.2 trades/day x ~25 ticks).
_OBS_FILENAME = 'trade_obs.jsonl'

#: Schema version, stamped on every record. A reader that meets a version it does not
#: know must say so rather than silently misinterpret fields — the failure mode this
#: whole module exists to prevent.
OBS_SCHEMA = 1

#: The trading thresholds this module needs in order to reproduce a rule's LEVEL.
#:
#: These MUST track option_buy.py's constants. They were hardcoded in the first cut,
#: which quietly recreated the exact failure this module's docstring warns about: a
#: second copy of the trading rules that can drift from the first. Change TRAIL_PCT
#: from 50 to 40 in option_buy and the observer would have gone on reporting a 50%
#: trail line, reporting overshoots against a level the bot never used.
#:
#: The defaults below are the values in force on 2026-08-21. option_buy calls
#: configure() at import so the live constants always win; the defaults exist only so
#: the analysis tools can compute historical levels without importing the bot.
_CFG = {
    'TRAIL_PCT': 50.0,
    'TRAIL_MIN_GAIN_PCT': 8.0,
    'PREMIUM_SL_PCT': 35.0,
    'ADV_SL_PEAK_PCT': 3.0,
    'ADV_SL_DRIFT_PCT': 8.0,
    'COST_SL_FLOOR_OFFSET': 0.10,
    'T1_GIVEBACK_KEEP_PCT': 50.0,
}


def configure(**kw):
    """Point the observer at the live trading constants. Never raises.

    Called once by option_buy at import. Unknown keys are ignored rather than rejected
    so that adding a constant to the bot can never break the recorder.
    """
    for k, v in (kw or {}).items():
        if k in _CFG:
            try:
                _CFG[k] = float(v)
            except (TypeError, ValueError):
                pass
    return dict(_CFG)


def set_stream(suffix=''):
    """Select this process's observer file: trade_obs<suffix>.jsonl. Never raises.

    One stream per underlying (2026-10-06): several bot processes appending to one file would
    interleave instruments and every analysis reading it would mix them. NIFTY passes '' and
    keeps the original file name.
    """
    global _OBS_FILENAME
    _OBS_FILENAME = f'trade_obs{suffix or ""}.jsonl'
    return _OBS_FILENAME


def _obs_path():
    return os.path.join(CURRENT_DIR, 'logs', _OBS_FILENAME)


def _log():
    """The bot's logger if we are running inside it, else a module-local one.

    Fetched lazily rather than imported: importing option_buy from here would be a
    circular import, and `logging.getLogger` returns the SAME configured object the
    bot set up in setup_logging(), so observer errors land in the normal day log.
    """
    import logging
    return logging.getLogger('option_buy')


def _obs_safe(fn):
    """Observation must NEVER affect trading.

    Same contract as option_buy._shadow_safe: gate on the feature flag in exactly one
    place, and turn any exception into a log line. A malformed position dict, a full
    disk, or a permissions change on logs/ must cost the experiment, never the book.
    """
    def wrapper(*args, **kwargs):
        if not OBSERVER_ENABLED:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            try:
                _log().error(f"OBS error in {fn.__name__} (ignored, trading "
                             f"unaffected): {e}")
            except Exception:
                pass
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _append(rec):
    """Append one record. Caller is already inside _obs_safe, so this may raise.

    Plain append, not tmp+rename: JSONL append of a single short line is atomic enough
    for this purpose on both ext4 and NTFS, and a torn final line costs one tick of
    observation. The replay reader skips unparseable lines and counts them, so a torn
    write is visible rather than silent.
    """
    path = _obs_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(rec, default=str) + '\n')


def _round(v, n=4):
    """Round for readability, pass non-numerics through untouched."""
    try:
        return round(float(v), n)
    except (TypeError, ValueError):
        return v


# ---------------------------------------------------------------------------
# The plan — recorded once, at entry
# ---------------------------------------------------------------------------

@_obs_safe
def on_open(key, pos, thresholds=None):
    """Snapshot the PLAN at the moment a position opens.

    `thresholds` is the live config in force for THIS trade (the caller passes the
    module constants). Stamping them per-trade rather than assuming them at read time
    is deliberate: these values change — TRAIL_MIN_GAIN_PCT went 15 -> 8, ADV_SL was
    added, MIN_T1_DISTANCE was added — and a replay that applied today's constants to
    an April trade would manufacture violations that never happened.
    """
    entry = float(pos.get('entry_premium', 0) or 0)
    t1 = float(pos.get('st_value', 0) or 0)
    t2 = float(pos.get('t2_value', 0) or 0)
    lot = int(pos.get('lot_size', 0) or 0)

    rec = {
        'schema': OBS_SCHEMA,
        'type': 'plan',
        'ts': datetime.datetime.now(IST).isoformat(),
        'key': key,
        'opt_type': pos.get('opt_type'),
        'signal_tf': pos.get('signal_tf'),
        'strike': pos.get('strike'),
        'symbol': pos.get('option_symbol'),
        'expiry': pos.get('expiry'),
        'entry_premium': _round(entry),
        'entry_spot': _round(pos.get('entry_spot')),
        'entry_time': pos.get('entry_time'),
        'lot_size': lot,
        'tick_size': pos.get('tick_size'),

        # The targets and floors this trade is being held against.
        't1': _round(t1),
        't2': _round(t2),
        't2_tf': pos.get('t2_tf') or '',
        'entry_gap_pct': _round(pos.get('entry_gap')),

        # Derived plan geometry — what the trade is risking to make.
        't1_distance': _round(t1 - entry) if t1 else None,
        't1_reward_pct': _round((t1 - entry) / entry * 100) if entry > 0 and t1 else None,
        'cost_floor_at_entry': _round(entry + _CFG['COST_SL_FLOOR_OFFSET']),
        'prem_sl_level': _round(entry * (1 - (thresholds or {}).get('PREMIUM_SL_PCT', 35) / 100)),

        # The config in force for this trade.
        'thresholds': thresholds or {},
    }
    _append(rec)
    _log().info(f"OBS PLAN {key} | entry={entry:.2f} T1={t1:.2f} "
                f"T2={t2:.2f} lot={lot}")


# ---------------------------------------------------------------------------
# The trajectory — recorded every tick the position is open
# ---------------------------------------------------------------------------

@_obs_safe
def on_tick(key, pos, curr, time_sl_cap=None, now=None, day_high=0):
    """Record one observation of an open position.

    Every field is a distance or a ratio, never a decision. `d_*` fields follow one
    convention throughout: **negative means the trigger is breached**. So `d_cost <= 0`
    means the cost floor was live at this tick, `d_t1 >= 0` means T1 was touched. The
    consistency matters — the whole point is that a reader can reconstruct the exact
    boolean ladder without knowing which side of each comparison the bot used.
    """
    now = now or datetime.datetime.now(IST)
    entry = float(pos.get('entry_premium', 0) or 0)
    if entry <= 0:
        return
    curr = float(curr or 0)
    peak = float(pos.get('peak_premium', entry) or entry)
    t1 = float(pos.get('st_value', 0) or 0)
    t2 = float(pos.get('t2_value', 0) or 0)
    lot = int(pos.get('lot_size', 0) or 0)

    peak_gain_pct = (peak - entry) / entry * 100
    drift_pct = (curr - entry) / entry * 100

    elapsed_min = None
    try:
        entry_dt = datetime.datetime.fromisoformat(pos.get('entry_time', ''))
        if entry_dt.tzinfo is None:
            entry_dt = IST.localize(entry_dt)
        elapsed_min = (now - entry_dt).total_seconds() / 60
    except Exception:
        # A broken entry_time is itself worth recording — monitor_positions treats it
        # as a TIME_SL trigger, so a reader needs to see that the clock was unreadable
        # rather than infer a missing field meant "not yet due".
        pass

    cost_level = float(pos.get('cost_sl_level', 0) or 0)
    cost_active = bool(pos.get('cost_sl_active'))

    # The trailing line, reproduced from the same arithmetic monitor_positions uses.
    # Recorded as a LEVEL, not as "is it hit" — see the module docstring.
    trail_line = None
    if entry > 0:
        trail_line = entry + (peak - entry) * (_CFG['TRAIL_PCT'] / 100.0)
        if cost_active and cost_level > trail_line:
            trail_line = cost_level

    prem_sl = entry * (1 - _CFG['PREMIUM_SL_PCT'] / 100.0)

    rec = {
        'schema': OBS_SCHEMA,
        'type': 'tick',
        'ts': now.isoformat(),
        'key': key,
        'ltp': _round(curr),
        'peak': _round(peak),
        'elapsed_min': _round(elapsed_min, 2),

        'drift_pct': _round(drift_pct, 2),
        'peak_gain_pct': _round(peak_gain_pct, 2),
        'unreal_rs': _round((curr - entry) * lot, 2) if lot else None,

        't1_hit': bool(pos.get('t1_hit')),
        'cost_sl_active': cost_active,
        'cost_sl_level': _round(cost_level),

        # Distance to each trigger. NEGATIVE = breached.
        'd_t1': _round(curr - t1) if t1 else None,
        'd_t2': _round(curr - t2) if t2 else None,
        'd_cost': _round(curr - cost_level) if cost_active and cost_level > 0 else None,
        'd_trail': _round(curr - trail_line) if trail_line is not None else None,
        'd_prem_sl': _round(curr - prem_sl),
        'trail_line': _round(trail_line),
        'trail_armed': bool(peak_gain_pct >= _CFG['TRAIL_MIN_GAIN_PCT']),

        # TIME_SL runway. Negative = the cap is already blown.
        'time_sl_cap': time_sl_cap,
        'time_left_min': (_round(time_sl_cap - elapsed_min, 2)
                          if (time_sl_cap and elapsed_min is not None) else None),

        # ADV_SL inputs, recorded explicitly because its two conditions are the pair
        # most often misremembered when reading a post-mortem.
        'adv_peak_below': bool(peak_gain_pct < _CFG['ADV_SL_PEAK_PCT']),
        'adv_drift_below': bool(drift_pct <= -_CFG['ADV_SL_DRIFT_PCT']),

        # THE DAY HIGH THE SCAN NEVER SAMPLED (2026-08-21).
        # `peak` above is built from once-a-minute polls and therefore understates what
        # actually traded. `day_high` comes from the quote's ohlc block and is the real
        # session high. When day_high >= t1 while t1_hit is still False, the premium
        # touched its target somewhere the scan did not look.
        # CAUTION FOR THE READER: the day high may predate the entry, so a single tick
        # cannot prove the touch happened while the position was held. Compare against
        # the day_high recorded on the FIRST tick of this position — a rise past T1
        # after that is a touch we were holding through. That comparison is left to
        # analyze_trade_obs.py, per this module's rule: record numbers, derive booleans
        # offline.
        'day_high': _round(day_high),
        'day_high_over_peak': _round(day_high - peak) if day_high else None,
        'day_high_ge_t1': bool(day_high and t1 and day_high >= t1),
    }
    _append(rec)


# ---------------------------------------------------------------------------
# The exit — recorded once, with the arithmetic that judges it
# ---------------------------------------------------------------------------

#: Which level each exit type pointed at. TIME_SL, ADV_SL and EOD are deliberately
#: absent: they are time/shape triggers with no price level, so "overshoot" is not a
#: meaningful question for them and inventing one would produce noise that looks like
#: a finding.
_LEVELLED_EXITS = ('TP', 'COST_SL', 'TRAIL', 'PREM_SL', 'T1_LOCK', 'T1_GIVEBACK')

#: The three exits that fire off the single stop floor under a position. They differ only in
#: which rule armed the floor (option_buy SL_COST / SL_T1_GIVEBACK / SL_T1_LOCK), so they all
#: measure against the same recorded level — but they are kept distinct here because the
#: overshoot on a T1_LOCK is money lost off a PROFIT TARGET, while the same overshoot on a
#: COST_SL is money lost off a break-even stop. Pooling them is what hid 13 profit-takes
#: inside the COST_SL bucket before 2026-09-01.
_FLOOR_EXITS = {'COST_SL': 'cost_sl_level',
                'T1_GIVEBACK': 't1_giveback_level',
                'T1_LOCK': 't1_lock_level'}


def _named_level(pos, exit_type, entry, t1, t2, peak, cfg=None):
    """The price level the firing rule pointed at, or (None, '') if it had none.

    Returns (level, description). This is the heart of the overshoot measurement: the
    gap between this number and the price actually booked is the money that leaks out
    between one poll and the next.

    `cfg` overrides the live thresholds — pass a trade's own stamped `thresholds` when
    replaying history, so an April trade is judged by April's constants.
    """
    c = dict(_CFG)
    c.update({k: v for k, v in (cfg or {}).items() if k in _CFG})
    if exit_type == 'TP':
        # T2 exit if the position had run past T1 to a live T2, else the T1 line.
        if t2 > 0 and pos.get('t1_hit'):
            return t2, 't2'
        return (t1, 't1') if t1 > 0 else (None, '')
    if exit_type in _FLOOR_EXITS:
        # All three read the same field: option_buy keeps ONE floor per position and
        # ratchets it upward, stamping `sl_reason` with whichever rule last raised it.
        lvl = float(pos.get('cost_sl_level', 0) or 0)
        return (lvl, _FLOOR_EXITS[exit_type]) if lvl > 0 else (None, '')
    if exit_type == 'TRAIL':
        trail = entry + (peak - entry) * (c['TRAIL_PCT'] / 100.0)
        cost_level = float(pos.get('cost_sl_level', 0) or 0)
        if pos.get('cost_sl_active') and cost_level > trail:
            return cost_level, 'cost_floor_over_trail'
        return trail, 'trail_%dpct_of_peak' % int(c['TRAIL_PCT'])
    if exit_type == 'PREM_SL':
        return (entry * (1 - c['PREMIUM_SL_PCT'] / 100.0),
                'prem_sl_%dpct' % int(c['PREMIUM_SL_PCT']))
    return None, ''


@_obs_safe
def on_exit(key, pos, exit_prem, exit_type, now=None):
    """Record the exit, and measure it against the level its own rule named.

    `overshoot_pct` is the number this module was built for. Convention: NEGATIVE means
    the fill landed WORSE than the level the rule pointed at — below the floor on a
    stop, below the target on a TP. On a stop that is money lost to poll granularity;
    on a TP it means the target was crossed and given back before the exit booked.
    """
    now = now or datetime.datetime.now(IST)
    entry = float(pos.get('entry_premium', 0) or 0)
    exit_prem = float(exit_prem or 0)
    t1 = float(pos.get('st_value', 0) or 0)
    t2 = float(pos.get('t2_value', 0) or 0)
    peak = float(pos.get('peak_premium', entry) or entry)
    lot = int(pos.get('lot_size', 0) or 0)

    level, level_desc = _named_level(pos, exit_type, entry, t1, t2, peak)

    overshoot_pct = overshoot_rs = None
    if level and level > 0 and exit_prem > 0:
        overshoot_pct = (exit_prem - level) / level * 100
        overshoot_rs = (exit_prem - level) * lot if lot else None

    hold_min = None
    try:
        entry_dt = datetime.datetime.fromisoformat(pos.get('entry_time', ''))
        if entry_dt.tzinfo is None:
            entry_dt = IST.localize(entry_dt)
        hold_min = (now - entry_dt).total_seconds() / 60
    except Exception:
        pass

    rec = {
        'schema': OBS_SCHEMA,
        'type': 'exit',
        'ts': now.isoformat(),
        'key': key,
        'exit_type': exit_type,
        'exit_premium': _round(exit_prem),
        'entry_premium': _round(entry),
        'peak': _round(peak),
        'lot_size': lot,
        'hold_min': _round(hold_min, 2),
        'pnl_rs': _round((exit_prem - entry) * lot, 2) if lot else None,
        'drift_pct': _round((exit_prem - entry) / entry * 100, 2) if entry > 0 else None,
        'peak_gain_pct': _round((peak - entry) / entry * 100, 2) if entry > 0 else None,

        # The judgement fields.
        'named_level': _round(level),
        'named_level_desc': level_desc,
        'overshoot_pct': _round(overshoot_pct, 3),
        'overshoot_rs': _round(overshoot_rs, 2),
        'has_level': exit_type in _LEVELLED_EXITS,

        't1': _round(t1),
        't2': _round(t2),
        't1_hit': bool(pos.get('t1_hit')),
        'cost_sl_active': bool(pos.get('cost_sl_active')),
        'cost_sl_level': _round(pos.get('cost_sl_level')),
    }
    _append(rec)

    if overshoot_pct is not None and overshoot_pct < -1.0:
        # The headline event. INFO, not DEBUG: this is the line a human should see the
        # same day rather than discover in a quarterly review.
        _log().warning(
            f"OBS OVERSHOOT {key} | {exit_type} named {level_desc}={level:.2f} "
            f"but filled {exit_prem:.2f} ({overshoot_pct:+.1f}%, "
            f"{overshoot_rs:+.0f} Rs vs the level the rule named)")
    else:
        _log().info(f"OBS EXIT {key} | {exit_type} @ {exit_prem:.2f} "
                    f"level={level_desc or 'n/a'} overshoot="
                    f"{'n/a' if overshoot_pct is None else f'{overshoot_pct:+.1f}%'}")

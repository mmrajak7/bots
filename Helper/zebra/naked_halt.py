"""The entry halt for the live naked book -- pause, kill, restart.

Design: `docs/NAKED_LIVE_DESIGN.md` F16, F27, G11, G13, G14, G16, Q7;
limits: `docs/NAKED_100_TRADE_TEST.md` section 11 (+ section 4, the
owner-override pilot's loss budget).

WHAT IT STOPS: NEW ENTRIES, and nothing else. Exits never read this file --
a halt that could stop an exit would turn a drawdown control into the way a
drawdown gets worse.

ONE FILE PER MODE. `logs/entry_halt.json` governs `naked_live: live`;
`logs/entry_halt.rehearsal.json` governs the rehearsal, whose fills are fake.
A rehearsal kill must never be waiting in the file the day `live` is armed,
and a rehearsal equity curve must never set the live peak.

State (G16 + what G11 needs to be computed at all):

    killed            bool -- set by a trigger or by hand (`zebra halt`)
    kill_code         one-time restart code, printed in the kill alert;
                      `zebra resume <code>` at the box, or Telegram
                      `/nresume [mode] <code>` (read by zebra's 5-minute
                      cycle while a kill is active, `naked_tg.py`) clears
    kill_kind         drawdown | slippage | loss_budget | manual
    reason, tripped_at
    pause_through     ISO date: no entries up to and including that session
    pause_reason      why the last pause tripped
    pause_since       timestamp of the last pause trip; only stops AFTER it
                      count towards the next one, so one cluster pauses once
    peak_equity, peak_at
    peak_reset_pending  set by a restart: the NEXT evaluation sets the peak to
                      equity at that moment (G11 "peak = equity at the
                      restart"; the CLI has no Kite marks to compute it with)
    slippage_since    set by a restart: only fills AFTER it count, so a
                      slippage kill needs a fresh full window to trip again
                      (no fill can arrive while killed, so without this the
                      same 20 fills would re-kill on every restart)
    last_marks        {id: last reliable BID}, the G11 fallback
    last_bucket, evaluated_at   the last sample; the 5-minute bucket and the
                      exact time
    history[]         every trip / restart / manual action, with sender + time

FAILS CLOSED. A file that exists but cannot be parsed is HALTED, and is never
overwritten by an evaluation (that would clear the kill it may hold). A MISSING
file is a fresh book -- except when the append-only journal
(`entry_halt[.rehearsal].history.jsonl`) shows a kill with no later restart:
deleting the state file is not a way to restart.

CALLER CONTRACT (P3/P4 must keep it):
  * `evaluate` runs from zebra's 5-minute cycle ONLY -- that cadence IS the
    G11 "sampled once per 5-minute bucket". The 5-second exit monitor must
    never call it: a drawdown sampled on every 5-second print kills on one
    thin bid, and rewrites this file 12 times a minute.
  * the ENTRY HOOK gates on the `allowed` returned by `evaluate()` in the SAME
    cycle, never on `entries_allowed()` alone: a file that says "clear" cannot
    tell that the evaluation behind it has stopped running.
    `entries_allowed()` is for display and the CLI.

Interpretations (flagged, owner-visible, change in config or here):
  * pause = the rest of the tripping session plus the next
    `naked_pause_sessions` sessions; counts VALUE stops only (`is_value_stop`),
    never TIME (P1 decision).
  * the slippage kill needs a FULL window (`naked_slippage_window` fills) --
    one bad first fill must not kill a book of one.
  * the loss budget is measured from `naked_capital`, NOT from the peak, and a
    restart does not reset it: it is a hard budget. Restarting with it still
    breached trips again on the next evaluation; raise the config instead.
  * an open position with no reliable mark yet is marked at its entry price
    and the result says so (`unmarked`).
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

from common.filelock import exclusive
from common.nse_holidays import next_session

from . import config as cfg
from .outcomes import is_value_stop
from .trade_store import is_naked

logger = logging.getLogger(__name__)

SCHEMA = 1
MODES = ('rehearsal', 'live')
KILL_KINDS = ('drawdown', 'slippage', 'loss_budget', 'manual')
BUCKET_MIN = 5
HISTORY_KEEP = 500          # in the state file; the journal keeps everything


class HaltUnreadable(RuntimeError):
    """The state file exists and cannot be trusted. Entries are halted."""


# -- paths ---------------------------------------------------------------------

def _check_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError('naked halt mode must be one of %s, got %r' % (MODES, mode))
    return mode


def state_path(mode: str) -> Path:
    _check_mode(mode)
    name = 'entry_halt.json' if mode == 'live' else 'entry_halt.rehearsal.json'
    return cfg.LOG_DIR / name


def journal_path(mode: str) -> Path:
    return state_path(mode).with_suffix('.history.jsonl')


def _lock_path(mode: str) -> Path:
    return state_path(mode).with_suffix('.lock')


def _fresh() -> dict:
    return {'schema': SCHEMA, 'killed': False, 'kill_code': None,
            'kill_kind': None, 'reason': None, 'tripped_at': None,
            'pause_through': None, 'pause_since': None, 'pause_reason': None,
            'peak_equity': None, 'peak_at': None, 'peak_reset_pending': False,
            'slippage_since': None, 'evaluated_at': None,
            'last_marks': {}, 'last_bucket': None, 'history': []}


# -- read / write ----------------------------------------------------------------

def _unresolved_kill_in_journal(mode: str) -> Optional[dict]:
    """The last kill in the journal if no restart follows it, else None."""
    p = journal_path(mode)
    if not p.exists():
        return None
    last = None
    with open(p, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            ev = json.loads(line)
            if ev.get('event') == 'kill':
                last = ev
            elif ev.get('event') == 'resume':
                last = None
    return last


def load(mode: str) -> dict:
    """The current state. Raises `HaltUnreadable` rather than guess."""
    p = state_path(mode)
    try:
        with open(p, encoding='utf-8') as f:
            s = json.load(f)
    except FileNotFoundError:
        try:
            ev = _unresolved_kill_in_journal(mode)
        except Exception as e:
            raise HaltUnreadable('halt file missing and its journal %s is '
                                 'unreadable (%s)' % (journal_path(mode), e))
        if ev:
            raise HaltUnreadable(
                'halt file %s is MISSING but the journal shows a %s kill at %s '
                'with no restart -- deleting the file does not restart; use '
                '`zebra resume <code>` with the code from that kill'
                % (p, ev.get('kind'), ev.get('at')))
        return _fresh()
    except Exception as e:
        raise HaltUnreadable('halt file %s unreadable (%s)' % (p, e))
    if not isinstance(s, dict) or s.get('schema') != SCHEMA \
            or not isinstance(s.get('killed'), bool) \
            or not isinstance(s.get('history'), list):
        raise HaltUnreadable('halt file %s has an unexpected shape' % p)
    base = _fresh()
    base.update(s)
    return base


def _save(mode: str, state: dict) -> None:
    p = state_path(mode)
    p.parent.mkdir(parents=True, exist_ok=True)
    state['history'] = state['history'][-HISTORY_KEEP:]
    tmp = str(p) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, str(p))


def _journal(mode: str, event: dict) -> None:
    p = journal_path(mode)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(event, sort_keys=True) + '\n')


def _record(mode: str, state: dict, event: dict) -> None:
    """History in the state AND the append-only journal. The journal is
    written FIRST: if the state write then fails, the journal still holds the
    kill, and a missing/unreadable state file stays halted."""
    _journal(mode, event)
    state['history'].append(event)


# -- the gate ----------------------------------------------------------------------

def entries_allowed(mode: str, today: date) -> tuple:
    """(allowed, reason). The ONE question the entry hook asks. Never raises."""
    try:
        s = load(mode)
    except HaltUnreadable as e:
        return False, 'HALTED (fails closed): %s' % e
    except Exception as e:                      # pragma: no cover - belt and braces
        return False, 'HALTED (fails closed): %s' % e
    if s['killed']:
        return False, 'KILLED (%s) at %s: %s -- restart: zebra resume <code>' % (
            s.get('kill_kind'), s.get('tripped_at'), s.get('reason'))
    pt = s.get('pause_through')
    if pt and today.isoformat() <= pt:
        return False, 'PAUSED through %s: %s' % (pt, s.get('pause_reason'))
    return True, 'clear'


# -- the book ------------------------------------------------------------------------

def book(trades: Iterable[dict], mode: str) -> list:
    """The live naked records this mode's halt governs. Rehearsal records
    (fake fills) only in rehearsal; real ones only in live."""
    _check_mode(mode)
    want = mode == 'rehearsal'
    return [t for t in trades
            if is_naked(t) and bool(t.get('rehearsal')) is want
            and t.get('status') != 'cancelled']


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def equity(records: list, marks: dict, last_marks: dict,
           capital: float) -> dict:
    """G11: capital + closed net + open (BID - entry) x qty.

    `marks`: {id: BID} from THIS poll, reliable books only (the caller drops
    unreliable ones). Missing -> `last_marks`, then the entry price. A closed
    record without `pnl_net` (costing failed) counts its gross `pnl`, and is
    reported, so a loss is never dropped from the curve.
    """
    closed = open_mtm = 0.0
    unmarked, uncosted = [], []
    for t in records:
        tid = str(t.get('id'))
        if t.get('status') == 'exited':
            v = _f(t.get('pnl_net'))
            if v is None:
                v = _f(t.get('pnl')) or 0.0
                uncosted.append(tid)
            closed += v
            continue
        entry = _f(t.get('debit'))
        qty = int(t.get('quantity') or 0)
        if entry is None:
            unmarked.append(tid)
            continue
        bid = _f(marks.get(tid))
        if bid is None:
            bid = _f(last_marks.get(tid))
        if bid is None:
            bid = entry
            unmarked.append(tid)
        open_mtm += (bid - entry) * qty
    return {'equity': round(capital + closed + open_mtm, 2),
            'closed': round(closed, 2), 'open_mtm': round(open_mtm, 2),
            'unmarked': unmarked, 'uncosted': uncosted}


def fill_slippages(records: list, since: Optional[str] = None) -> list:
    """G14: per-fill slippage as % of the ENTRY premium, in fill order.

    Buy: (fill - the PAPER ask). Sell: (touch bid - fill). Positive is worse
    than paper, which fills at the touch. Keys: entry `entry_long_price` vs
    `entry_signal_ask` -- the ask when the signal was taken, before the entry
    chase re-quoted (review M1, 2026-11-28: against the re-quoted ask an entry
    read ~0 by construction and diluted the mean to half its real size) --
    falling back to `long_ask_entry` on older records; exit `exit_debit` vs
    `exit_touch` (written by the P4 exit path). A fill without its touch is
    skipped -- it cannot be measured, and inventing a touch would invent the
    answer.
    """
    rows = []
    for t in records:
        prem = _f(t.get('debit'))
        if not prem or prem <= 0:
            continue
        fill = _f(t.get('entry_long_price'))
        touch = _f(t.get('entry_signal_ask'))
        if touch is None:
            touch = _f(t.get('long_ask_entry'))
        if fill is not None and touch is not None:
            rows.append(('%s %s' % (t.get('entry_date'), t.get('entry_time')),
                         (fill - touch) / prem * 100.0))
        if t.get('status') == 'exited':
            xf, xt = _f(t.get('exit_debit')), _f(t.get('exit_touch'))
            if xf is not None and xt is not None:
                rows.append(('%s %s' % (t.get('exit_date'), t.get('exit_time')),
                             (xt - xf) / prem * 100.0))
    rows.sort(key=lambda r: r[0])
    return [r[1] for r in rows if not since or r[0] > since]


def _value_stops_since(records: list, since: Optional[str], window_start: date) -> list:
    out = []
    for t in records:
        if t.get('status') != 'exited' or not is_value_stop(t.get('exit_reason')):
            continue
        d = str(t.get('exit_date') or '')
        if d < window_start.isoformat():
            continue
        ts = '%s %s' % (d, t.get('exit_time') or '00:00:00')
        if since and ts <= since:
            continue
        out.append(t)
    return out


def _window_start(today: date, sessions: int) -> date:
    """The first of the last `sessions` sessions, counting today."""
    d = today
    for _ in range(max(sessions - 1, 0)):
        d = next_session(d, forward=False)
    return d


def _pause_through(today: date, sessions: int) -> date:
    d = today
    for _ in range(sessions):
        d = next_session(d)
    return d


def _bucket(now: datetime) -> str:
    return now.strftime('%Y-%m-%d %H:') + '%02d' % (now.minute - now.minute % BUCKET_MIN)


def _ts(now: datetime) -> str:
    return now.strftime('%Y-%m-%d %H:%M:%S')


def _new_code() -> str:
    return secrets.token_hex(3).upper()


def _kill(mode: str, state: dict, kind: str, reason: str, now: datetime,
          sender: str) -> dict:
    code = _new_code()
    state.update(killed=True, kill_code=code, kill_kind=kind, reason=reason,
                 tripped_at=_ts(now))
    ev = {'event': 'kill', 'kind': kind, 'reason': reason, 'at': _ts(now),
          'by': sender, 'code': code}
    _record(mode, state, ev)
    logger.error('NAKED %s KILL (%s): %s -- restart code %s', mode.upper(),
                 kind, reason, code)
    return ev


# -- the evaluation --------------------------------------------------------------------

def evaluate(trades: Iterable[dict], marks: dict, now: datetime, mode: str,
             *, capital: Optional[float] = None) -> dict:
    """Update the peak and the triggers on this poll; persist; report.

    Call once per cycle while `naked_live` is not off. Returns
    ``{'allowed', 'reason', 'equity', 'peak', 'drawdown_pct', 'events',
    'unmarked', 'uncosted', 'slippage_mean', 'slippage_n'}``. ``events`` holds
    any kill / pause tripped by THIS call (the caller alerts on them; a kill
    event carries its restart code). Never raises: a failure halts.
    """
    capital = float(cfg.NAKED_CAPITAL if capital is None else capital)
    out = {'allowed': False, 'reason': '', 'equity': None, 'peak': None,
           'drawdown_pct': None, 'events': [], 'unmarked': [], 'uncosted': [],
           'slippage_mean': None, 'slippage_n': 0}
    try:
        with exclusive(_lock_path(mode)):
            try:
                state = load(mode)
            except HaltUnreadable as e:
                out['reason'] = 'HALTED (fails closed): %s' % e
                logger.error('naked halt: %s', e)
                return out                       # never overwrite what we cannot read
            _evaluate_locked(state, list(trades), marks or {}, now, mode,
                             capital, out)
            _save(mode, state)
    except Exception as e:
        logger.error('naked halt evaluation failed (%s) -- entries HALTED', e)
        out['allowed'] = False
        out['reason'] = 'HALTED (fails closed): evaluation failed: %s' % e
        return out
    return out


def _evaluate_locked(state: dict, trades: list, marks: dict, now: datetime,
                     mode: str, capital: float, out: dict) -> None:
    recs = book(trades, mode)
    open_ids = {str(t.get('id')) for t in recs if t.get('status') != 'exited'}
    lm = {k: v for k, v in (state.get('last_marks') or {}).items() if k in open_ids}
    for k, v in marks.items():
        if str(k) in open_ids and _f(v) is not None:
            lm[str(k)] = _f(v)
    state['last_marks'] = lm

    eq = equity(recs, {str(k): v for k, v in marks.items()}, lm, capital)
    e = eq['equity']
    out.update(equity=e, unmarked=eq['unmarked'], uncosted=eq['uncosted'])

    # Peak: running max, sampled per 5-minute bucket; a restart resets it to
    # equity NOW (G11 amended 10-26).
    if state.get('peak_reset_pending') or state.get('peak_equity') is None:
        if state.get('peak_reset_pending'):
            _record(mode, state, {'event': 'peak_reset', 'at': _ts(now),
                                  'from': state.get('peak_equity'), 'to': e})
        state.update(peak_equity=e, peak_at=_ts(now), peak_reset_pending=False)
    elif e > float(state['peak_equity']):
        state.update(peak_equity=e, peak_at=_ts(now))
    state['last_bucket'] = _bucket(now)
    state['evaluated_at'] = _ts(now)
    peak = float(state['peak_equity'])
    dd = (peak - e) / peak * 100.0 if peak > 0 else 0.0
    out.update(peak=peak, drawdown_pct=round(dd, 2))

    slips = fill_slippages(recs, state.get('slippage_since'))
    win = int(cfg.NAKED_SLIPPAGE_WINDOW)
    last = slips[-win:] if win > 0 else []
    if last:
        out.update(slippage_mean=round(sum(last) / len(last), 3), slippage_n=len(last))

    if not state['killed']:
        trip = None
        if dd >= float(cfg.NAKED_KILL_DRAWDOWN_PCT):
            trip = ('drawdown', 'equity Rs %s is %.1f%% below the peak Rs %s '
                    '(limit %s%%, open marks included)'
                    % (f'{e:,.0f}', dd, f'{peak:,.0f}', cfg.NAKED_KILL_DRAWDOWN_PCT))
        elif win > 0 and len(last) >= win \
                and out['slippage_mean'] > float(cfg.NAKED_SLIPPAGE_KILL_PCT):
            trip = ('slippage', 'fills averaged %.2f%% of premium worse than '
                    'the touch over the last %d (limit %s%%)'
                    % (out['slippage_mean'], len(last), cfg.NAKED_SLIPPAGE_KILL_PCT))
        elif float(cfg.NAKED_LOSS_BUDGET) > 0 \
                and capital - e >= float(cfg.NAKED_LOSS_BUDGET):
            trip = ('loss_budget', 'loss Rs %s (realised + open marks) reached '
                    'the budget Rs %s' % (f'{capital - e:,.0f}',
                                          f'{float(cfg.NAKED_LOSS_BUDGET):,.0f}'))
        if trip:
            out['events'].append(_kill(mode, state, trip[0], trip[1], now, 'auto'))

    today = now.date()
    pt = state.get('pause_through')
    if not (pt and today.isoformat() <= pt):
        start = _window_start(today, int(cfg.NAKED_PAUSE_WINDOW_SESSIONS))
        stops = _value_stops_since(recs, state.get('pause_since'), start)
        if int(cfg.NAKED_PAUSE_STOPS) > 0 and len(stops) >= int(cfg.NAKED_PAUSE_STOPS):
            through = _pause_through(today, int(cfg.NAKED_PAUSE_SESSIONS))
            reason = ('%d value stops since %s (%s) -- limit %d in %d sessions'
                      % (len(stops), start.isoformat(),
                         ', '.join('#%s %s' % (t.get('id'), t.get('stock')) for t in stops),
                         cfg.NAKED_PAUSE_STOPS, cfg.NAKED_PAUSE_WINDOW_SESSIONS))
            state.update(pause_through=through.isoformat(), pause_since=_ts(now),
                         pause_reason=reason)
            ev = {'event': 'pause', 'at': _ts(now), 'through': through.isoformat(),
                  'reason': reason, 'by': 'auto'}
            _record(mode, state, ev)
            out['events'].append(ev)
            logger.warning('NAKED %s PAUSE through %s: %s', mode.upper(),
                           through.isoformat(), reason)

    if state['killed']:
        out['reason'] = 'KILLED (%s): %s' % (state['kill_kind'], state['reason'])
    elif state.get('pause_through') and today.isoformat() <= state['pause_through']:
        out['reason'] = 'PAUSED through %s' % state['pause_through']
    else:
        out['allowed'] = True
        out['reason'] = 'clear'


# -- owner actions (CLI; zebra's cycle calls `resume` for Telegram /nresume) ---------

def halt(mode: str, reason: str, now: datetime, sender: str) -> dict:
    """Trip the kill by hand (G13: e.g. the test reads FAIL). Returns the kill
    event with its restart code. Works on an unreadable file too -- a halt
    must always be possible -- by setting it aside and starting a killed one."""
    if not reason or not reason.strip():
        raise ValueError('a manual halt needs a reason')
    with exclusive(_lock_path(mode)):
        try:
            state = load(mode)
        except HaltUnreadable as e:
            p = state_path(mode)
            if p.exists():
                aside = str(p) + '.unreadable.%s' % now.strftime('%Y%m%d%H%M%S')
                os.replace(str(p), aside)
                logger.error('naked halt: unreadable file set aside as %s', aside)
            state = _fresh()
            reason = '%s [previous state unreadable: %s]' % (reason, e)
        if state['killed']:
            raise ValueError('already killed (%s) at %s -- code unchanged'
                             % (state['kill_kind'], state['tripped_at']))
        ev = _kill(mode, state, 'manual', reason.strip(), now, sender)
        _save(mode, state)
    return ev


def resume(mode: str, code: str, now: datetime, sender: str) -> dict:
    """Clear a kill with its one-time code; the next evaluation resets the
    peak to equity at that moment. Never clears a PAUSE (that is automatic)
    and never changes a limit. Raises ValueError on a wrong code."""
    with exclusive(_lock_path(mode)):
        try:
            state = load(mode)
        except HaltUnreadable:
            # The one unreadable case a code can settle: the state file was
            # deleted under a kill. The journal holds that kill and its code;
            # a matching code restarts from a fresh state. A CORRUPT file is
            # not this case -- fix it by hand (`zebra halt` sets it aside).
            if state_path(mode).exists():
                raise
            jk = _unresolved_kill_in_journal(mode)
            if not jk:
                raise
            state = _fresh()
            state.update(killed=True, kill_code=jk.get('code'),
                         kill_kind=jk.get('kind'), reason=jk.get('reason'),
                         tripped_at=jk.get('at'))
        if not state['killed']:
            raise ValueError('not killed -- nothing to resume')
        if not code or code.strip().upper() != str(state.get('kill_code')):
            _record(mode, state, {'event': 'resume_refused', 'at': _ts(now),
                                  'by': sender})
            _save(mode, state)
            raise ValueError('wrong restart code')
        ev = {'event': 'resume', 'at': _ts(now), 'by': sender,
              'kind': state['kill_kind'], 'killed_at': state['tripped_at']}
        state.update(killed=False, kill_code=None, kill_kind=None,
                     reason=None, tripped_at=None, peak_reset_pending=True,
                     slippage_since=_ts(now))
        _record(mode, state, ev)
        _save(mode, state)
    logger.warning('NAKED %s RESUMED by %s; the peak resets on the next cycle',
                   mode.upper(), sender)
    return ev


def summary(mode: str) -> str:
    """One block for `zebra halt --status` and the digest."""
    try:
        s = load(mode)
    except HaltUnreadable as e:
        return 'naked %s: HALTED (fails closed) -- %s' % (mode, e)
    lines = ['naked %s halt: %s' % (mode, 'KILLED' if s['killed'] else 'not killed')]
    if s['killed']:
        lines.append('  kill: %s at %s -- %s' % (s['kill_kind'], s['tripped_at'], s['reason']))
        lines.append('  restart: python -m zebra resume <code> --mode %s '
                     '(the code is in the kill alert)' % mode)
    if s.get('pause_through'):
        lines.append('  pause through: %s -- %s' % (s['pause_through'], s.get('pause_reason')))
    if s.get('peak_equity') is not None:
        lines.append('  peak equity: Rs %s at %s%s' % (
            f"{float(s['peak_equity']):,.0f}", s['peak_at'],
            ' (resets next cycle)' if s.get('peak_reset_pending') else ''))
    for ev in s['history'][-5:]:
        lines.append('  %s %s %s' % (ev.get('at'), ev.get('event'),
                                     ev.get('kind') or ev.get('through') or ''))
    return '\n'.join(lines)

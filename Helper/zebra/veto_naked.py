"""SHADOW ONLY: what every VETOED signal would have paid as a real naked option.

Measures, never acts. Places no order, sends no alert, and never writes to the
trade store.

## Why this exists

The only outcome a veto has ever had is a daily-candle replay (`golive` section
8) and a spot-barrier label (`outcomes.veto_shadow`). Neither is an option
price. The 2026-11-05 review showed what that costs: refreshing the candle
cache added 13 setups and moved the vetoed mean by 8.6 points, and one reason
category by 25, because daily bars cannot say which of target and stop came
first. This book answers the same question on the same instrument as the arm
the 100-trade test scores.

## The one rule it must not break: it cannot touch the test

The test population is every `naked_long` row in `shadow_structures.json`. So
this is a SEPARATE file: a row in that file would enter the population and
change a gate that was fixed in advance. The file name is pinned by a test.

## Same exits, by construction

A row is an ordinary shadow holding one arm under the key `naked_long`, driven
by `structure_shadow.poll_one`. There is no second copy of the exit cascade to
drift: TP, stop, debounce, opening buffer, TIME and latches are the live arm's.

## Differences from an allowed trade, stated plainly

* Entry is the LIVE ask on the first cycle after the verdict, because an allowed
  trade also re-quotes after its verdict. The ask stored when the vet was
  requested is minutes old, and is kept only as `ask_at_request`.
* The contract is the one in the vet context, not re-picked at entry. Measured
  on 93 entered records with that context on 2026-11-05: the same target on 92
  (the one miss, INFY #989, re-evaluated its swing at entry) and the same symbol
  on 85 (the other 8 rolled to the next ATM strike between verdict and entry).
* Drift cancel is not replicated. Of 128 allowed signals on 2026-11-05, 21 (16%)
  never entered, so a similar share of these rows would not have traded.
* Forward only. A past veto has an unobserved head, so it is never backfilled.
* One setup, one row (stock, direction, ST line). A later re-veto of that line
  is recorded in `repeat_ids`, never scored again. The exception is a first row
  that opened late: it is not scored, so a clean re-veto takes over the setup.
* A veto that never found a usable price stays in `unopened` and is reported,
  never dropped silently: a dead book correlates with a bad outcome. So does one
  whose verdict landed after the session's last cycle or during an outage
  (`missed_session`): cycles stop at the close, and a row cannot open a day late.
"""
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from . import config as cfg
from . import strikes as strikes_mod
from . import structure_shadow as ss
from .trade_store import decided

logger = logging.getLogger(__name__)

STATE_FILE = cfg.LOG_DIR / 'shadow_veto_naked.json'
ARM = ss.MARKS_ARM

#: Reasons a veto can never open, so it stops being retried. The others (a quote
#: or a spot missing right now) retry for the rest of the session.
FINAL_REASONS = ('no_contract', 'bad_expiry', 'bad_direction', 'already_at_target',
                 'missed_session', 'bad_record')


def load_state() -> Dict[str, Any]:
    st = ss._load(STATE_FILE)
    st.setdefault('setups', {})
    st.setdefault('unopened', {})
    return st


def setup_key(t: Dict[str, Any]) -> Optional[str]:
    try:
        return '%s|%s|%.2f' % (t['stock'], t['direction'], float(t['st_value']))
    except (KeyError, TypeError, ValueError):
        return None


def _parse(s: Any) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(s)).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def contract(t: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The naked contract this signal would have bought, from its vet context.

    The target is the one a live entry would use: a swing target that APPLIED
    shortens it, as on `tp_spot`, otherwise the pair's own target. None if any
    field an entry cannot do without is missing.
    """
    ctx = (t.get('vet') or {}).get('context')
    if not isinstance(ctx, dict) or not isinstance(ctx.get('bcs'), dict):
        return None
    bcs = ctx['bcs']
    swing = ctx.get('swing_tp') or bcs.get('swing_tp') or {}
    target = swing.get('tp_spot') if swing.get('applied') else bcs.get('target_spot')
    out = {'symbol': bcs.get('long_symbol'), 'strike': bcs.get('long_strike'),
           'expiry': ctx.get('expiry'), 'target': target,
           'lot': ctx.get('lot_size') or bcs.get('lot_size'),
           'ask_at_request': bcs.get('long_ask')}
    if not (out['symbol'] and out['expiry'] and out['target'] and out['lot']):
        return None
    return out


def _spots(kite: Any, names: Iterable[str]) -> Dict[str, float]:
    from playbook.magnet.scanner import get_ltp
    return get_ltp(kite, sorted(names)) or {}


def _note_unopened(state: Dict[str, Any], tid: str, t: Dict[str, Any], reason: str, ts: str) -> None:
    u = state['unopened'].setdefault(tid, {'stock': t.get('stock'), 'first_at': ts,
                                           'attempts': 0})
    u.update(reason=reason, last_at=ts, attempts=u['attempts'] + 1)


def _candidates(state: Dict[str, Any], trades: List[Dict[str, Any]],
                vet_now: datetime) -> List[Dict[str, Any]]:
    """Today's vetoes that have no row yet. Records repeats and misses as it goes.

    Only vetoes since this book `started` are considered, so its first run does
    not report the whole history as missed.
    """
    from .vet import VETOED
    vetoes = [t for t in trades if isinstance(t.get('vet'), dict)
              and t['vet'].get('state') == VETOED]
    out = []
    seen = set()
    today = vet_now.date().isoformat()
    started = state.get('started') or today
    for t in sorted(vetoes, key=lambda x: str(x['vet'].get('decided_at'))):
        day = str(t['vet'].get('decided_at') or '')[:10]
        # `shadow_of`: one verdict covers both arms, so it never opens a row.
        if t.get('shadow_of') is not None or not day or day < started:
            continue
        tid, key = str(t.get('id')), setup_key(t)
        if tid in state['shadows'] or (state['unopened'].get(tid) or {}).get('reason') in FINAL_REASONS:
            continue
        first = state['setups'].get(key) if key else None
        first_row = state['shadows'].get(first)
        made = _parse(t['vet'].get('decided_at'))
        clean = (day == today and made is not None
                 and (vet_now - made).total_seconds() <= ss.MAX_OPEN_LAG_SEC)
        if first_row is not None and not (first_row.get('opened_late') and clean):
            reps = first_row.setdefault('repeat_ids', [])
            if tid not in reps:
                reps.append(tid)
            state['unopened'].pop(tid, None)
            continue
        if day != today:
            # An older veto has an unobserved head, so it can never open. Say so
            # once. A veto already noted keeps the reason it actually failed on.
            if tid not in state['unopened']:
                _note_unopened(state, tid, t, 'missed_session', today)
            continue
        # A second veto of a setup whose first is still waiting on a price: one
        # row only. If the first opens, the next cycle records this as a repeat.
        if key in seen:
            continue
        if key:
            seen.add(key)
        out.append(t)
    return out


def _open_one(state: Dict[str, Any], t: Dict[str, Any], c: Dict[str, Any], lq: Optional[Dict[str, Any]], spot: Optional[float],
              vet_now: datetime) -> bool:
    """Open the row, or note why not. True if a row opened."""
    tid, ts = str(t.get('id')), vet_now.strftime('%Y-%m-%d %H:%M:%S')
    direction = t.get('direction')
    if direction not in ('CE', 'PE'):
        _note_unopened(state, tid, t, 'bad_direction', ts)
        return False
    if ss._sessions_left(c['expiry'], vet_now.date()) is None:
        _note_unopened(state, tid, t, 'bad_expiry', ts)
        return False
    if not (spot and spot > 0):
        _note_unopened(state, tid, t, 'no_spot', ts)
        return False
    target = float(c['target'])
    if ss._tp_hit(direction, float(spot), target):
        # A signal already through its line would not enter, and the first poll
        # would book an instant TP: a win nobody could have taken.
        _note_unopened(state, tid, t, 'already_at_target', ts)
        return False
    ask = (lq or {}).get('ask') or 0
    if lq is None or not lq.get('reliable', False) or ask <= 0:
        _note_unopened(state, tid, t, 'no_quote', ts)
        return False
    bid = lq.get('bid') or 0
    qty, day = int(c['lot']), vet_now.date().isoformat()
    vet = t.get('vet') or {}
    made = _parse(vet.get('decided_at')) or vet_now
    book = {'quantity': qty, 'long_ask_entry': float(ask), 'entry_spot': float(spot),
            'expiry': str(c['expiry'])[:10], 'entry_date': day, 'direction': direction,
            'long_strike': c['strike'],
            'long_mid_entry': (bid + ask) / 2.0 if bid > 0 else None}
    state['shadows'][tid] = {
        'trade_id': tid, 'stock': t.get('stock'), 'direction': direction,
        'st_value': t.get('st_value'), 'timeframe': t.get('timeframe'),
        'long_symbol': c['symbol'], 'short_symbol': None, 'long_strike': c['strike'],
        'target_spot': target, 'expiry': str(c['expiry'])[:10], 'quantity': qty,
        'entry_spot': float(spot), 'entry_date': day,
        'long_ask_entry': float(ask), 'short_bid_entry': None, 'width': None,
        'ask_at_request': c['ask_at_request'],
        'decision_id': vet.get('decision_id'), 'decided_at': vet.get('decided_at'),
        'since': ts,
        # An outage between verdict and first cycle: the price is real, but it
        # is not the one an entry would have paid. Reported as PARTIAL.
        'opened_late': (vet_now - made).total_seconds() > ss.MAX_OPEN_LAG_SEC,
        'polls': 0, 'repeat_ids': [],
        'context': ss.entry_context(book, None, 'not_read'),
        'arms': {ARM: {'status': 'open', 'entry_value': round(float(ask), 4),
                       'fills': ss.ARMS[ARM]['fills'], 'peak': round(float(ask), 4),
                       'exit': None}},
    }
    key = setup_key(t)
    if key:
        state['setups'][key] = tid
    state['unopened'].pop(tid, None)
    return True


def poll(store: Any, kite: Any) -> Dict[str, Any]:
    """One cycle: open rows for new vetoes, advance the open ones. Never raises."""
    try:
        if not cfg.STRUCTURE_SHADOW_ENABLED:
            logger.info('SHADOW veto_naked: DISABLED (structure_shadow_enabled=false)')
            return {'disabled': True}
        from .vet import _now
        vet_now = _now().replace(tzinfo=None)    # the clock `decided_at` is stamped on
        ts = vet_now.strftime('%Y-%m-%d %H:%M:%S')
        now = datetime.now(cfg.IST)
        state = load_state()
        # Saved the moment it is set, even on an idle cycle: otherwise a veto
        # landing after the last cycle of the first day leaves no trace at all.
        new_book = 'started' not in state
        state.setdefault('started', vet_now.date().isoformat())
        if strikes_mod._cooldown_error() is not None:
            # `_quote_option` would refuse anyway, and an LTP sent during a 429
            # cooldown extends it. A skipped cycle costs a row one poll.
            logger.info('SHADOW veto_naked: standing down for the quote cooldown')
            if new_book:
                ss._save(state, STATE_FILE)
            return {'cooldown': True}
        cands = []
        for t in _candidates(state, decided(store.load_trades()), vet_now):
            # One bad record must not stop every other row being watched.
            try:
                c = contract(t)
            except Exception as e:
                logger.warning('veto_naked: bad record #%s: %s', t.get('id'), e)
                _note_unopened(state, str(t.get('id')), t, 'bad_record', ts)
                continue
            if c is None:
                _note_unopened(state, str(t.get('id')), t, 'no_contract', ts)
            else:
                cands.append((t, c))
        live = {}
        for k, v in state['shadows'].items():
            try:
                if v['arms'][ARM]['status'] == 'open':
                    v['stock'], v['long_symbol']     # the fields the loop below needs
                    live[k] = v
            except (KeyError, TypeError) as e:
                logger.warning('veto_naked: unreadable row #%s (%s) -- not polled', k, e)
        names = {str(t.get('stock')) for t, _ in cands} | {v['stock'] for v in live.values()}
        opened = closed = 0
        if names:
            try:
                ltps = _spots(kite, names)
            except Exception as e:
                logger.warning('veto_naked: spot fetch failed: %s', e)
                ltps = {}
            strikes_mod.prefetch_quotes(kite, [c['symbol'] for _, c in cands]
                                        + [v['long_symbol'] for v in live.values()])
            for t, c in cands:
                try:
                    lq = strikes_mod._quote_option(kite, c['symbol'])
                    opened += _open_one(state, t, c, lq, ltps.get(str(t.get('stock'))), vet_now)
                except Exception as e:
                    logger.warning('veto_naked: open failed for #%s: %s', t.get('id'), e)
                    _note_unopened(state, str(t.get('id')), t, 'error', ts)
            for tid, sh in live.items():
                try:
                    lq = strikes_mod._quote_option(kite, sh['long_symbol'])
                    ss.poll_one(sh, ltps.get(sh['stock']), lq, None, now, now.date())
                    closed += sh['arms'][ARM]['status'] != 'open'
                except Exception as e:
                    logger.warning('veto_naked: poll failed for #%s: %s', tid, e)
        if new_book or state['shadows'] or state['unopened']:
            ss._save(state, STATE_FILE)
        # Today's only: an older one can no longer open, so it is not waiting.
        waiting = sum(1 for u in state['unopened'].values()
                      if u.get('reason') not in FINAL_REASONS
                      and str(u.get('last_at'))[:10] == vet_now.date().isoformat())
        # Said every cycle, so a measurement that stopped is visible in the log.
        logger.info('SHADOW veto_naked: %d live, %d opened, %d closed this cycle, '
                    '%d waiting on a price', len(live) + opened, opened, closed, waiting)
        return {'opened': opened, 'live': len(live) + opened, 'closed': closed}
    except Exception as e:                          # never raise into a cycle
        logger.warning('veto_naked shadow failed: %s', e, exc_info=True)
        return {}

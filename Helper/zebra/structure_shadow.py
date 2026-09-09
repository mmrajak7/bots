"""SHADOW ONLY: what a DIFFERENT structure on the same signal would have paid.

Measures, never acts. Places no order, sends no Telegram, and never writes to
`zebra_trades.json`.

## Why this exists

The signal and the structure are two different bets, and only one of them has
ever been measured. Replaying the spot path of every triggered signal says the
magnet itself is worth **+1.02% of notional** over 30 sessions. Decomposed per
unit of notional, the Bull Call Spread built on top of it returns **-0.06%**:

    signal (delta-1)   win +4.55%   loss -8.38%   =>  +1.02%
    the BCS on it      win +0.55%   loss -1.55%   =>  -0.06%

It surrenders 88% of the win to avoid 82% of the loss, and the win was the
bigger number. The mechanism is one line: the short strike sits AT the ST
line, so the structure sells away the exact move it is betting on.

An exact replay on the cohort's own value paths -- real bid/ask, each arm on
its own stop -- put a naked ATM long at **+14.8% RoC against the spread's
+4.2%** over 19 realised positions. That is not yet a finding. Most of it sits
in seven positions too large for a Rs 25,000 slot, and inside the cap the
sample is twelve. So this module does the counting forward, on live signals,
before anything is rearranged around it.

## The questions the arms are chosen to separate

Each arm differs from its neighbour in exactly ONE respect, so a difference in
outcome has one candidate cause rather than three:

    naked_long   vs the real spread   ->  does the SHORT LEG cost more than it saves?
    naked_hold   vs naked_long        ->  does the -50% STOP earn its keep?
    naked_runner vs naked_hold        ->  does the TP CAP at the ST line cost us?
    spread_hold  vs the real spread   ->  the stop question again, on the live structure
    delta1       vs everything        ->  how much of the signal any of them keeps

`naked_runner` is the arm today's arithmetic points at, and it is the only one
that cannot be derived from the existing value paths -- because it is still
open after the real position has closed, and the paths stop there.

## Why it is a separate book rather than a field on the trade

A shadow OUTLIVES its parent. That is the entire point: the interesting half
of `naked_runner` happens after the spread has stopped out. Keeping it on the
trade record would mean writing to closed rows indefinitely, and the standing
instruction is that the books are not to be touched. So this owns one file,
writes it atomically, and nothing else reads or writes that file.

Two writers are possible in principle (an overrunning cycle overlapping the
next). The write is atomic, so the failure mode is a lost MEASUREMENT, never a
corrupt file and never a lost trade. A cycle runs 18-46s against a 5-minute
interval, so it is unlikely as well as harmless.

## Five ways a shadow flatters itself, and what is done about each

**Booking a price nobody could trade at.** Every arm prices at the side it
would actually trade against -- sell the long at the BID, buy the short back at
the ASK -- and REFUSES to book on an unreliable book, deferring to the next
poll exactly as the live engine does. `zebra.mfe` and `zebra.spot_shadow`
already learned this one: the clamps that return 0.0 or `width` with
`reliable=True` are what turn the cohort's overnight measure from +1.3% into
-12.1%. A spread arm is CLAMPED to [0, width] -- without the floor a wide book
records a P&L past -100% on a -100%-capped structure, which is how PIIND #50
read -112.4%.

**Losing the observation instead of the trade.** TP fires on a spot level and
TIME on the calendar, so either can arrive on a poll where the option book is
unusable. Booking `value=None` there writes an exit with no P&L, and a reader
that skips those loses the whole observation -- silently, and in the
optimistic direction, because an unpriceable book correlates with a bad
outcome. So a trigger LATCHES and the booking waits for a price, with expiry
as the backstop; anything that reaches expiry unpriced is reported in its own
column rather than dropped. `naked_runner` is the arm this protects: its ONLY
exit is TIME, so one bad book on the deadline poll used to erase the very arm
this module exists to measure.

**Being compared on the wrong axis.** Arms do not hold for the same length of
time -- `naked_runner` has no TP, so it runs ~28 days against the spread's ~5
-- and they do not pay the same fees, because charges follow TURNOVER and a
naked arm buys the whole ATM premium where the spread pays a net debit about
half that. Holding period is surfaced per arm, fees come from the real model
per leg, and an arm whose fee cannot be computed reports UNCOSTED rather than
free.

**Acting on a print the engine itself would not act on.** Value stops stand
down for `VALUE_TRIGGER_OPEN_BUFFER_SEC` after the open -- both incidents that
cost real money were on the first prints of a session. Spot triggers stand
down inside the cash closing auction, where spot is frozen by design.

**A head that was never observed.** A shadow opened after its parent entered
has missed part of the path, so its stop may already have fired unseen.
`since` and `opened_late` are stamped and the reader marks those PARTIAL
rather than counting them.

## Contract

`poll()` is called once per cycle from the OBSERVATION block of `run_cycle`,
after everything that trades. It never raises. It returns a small summary dict
for logging; the caller may ignore it.

SAFE-TO-RERUN: re-running a cycle re-prices only arms that are still open, and
a booked exit is never re-priced or reopened, so a replayed session converges
on the same book rather than accumulating.

RETIRES WHEN: the arms have enough closed observations to answer whether the
short leg and the TP cap pay for themselves -- or the structure is changed on
the strength of them, at which point the new structure becomes the control and
this file's arms are rechosen around it.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from typing import Optional

from common import market_session
from common.nse_holidays import sessions_between

from . import config as cfg
from . import strikes as strikes_mod
from .trade_store import in_cohort

logger = logging.getLogger(__name__)

SCHEMA = 1
STATE_FILE = cfg.LOG_DIR / 'shadow_structures.json'

#: One arm per question. `legs` is what the arm actually holds, `tp` whether it
#: takes the ST-line target, `stop_frac` the fraction of entry value at which
#: it gives up (None = no value stop). `fills` is the round-trip order count,
#: carried so the reader can cost each arm honestly rather than assuming the
#: spread's four.
#: `reference` marks an arm that is NOT a proposal in this book. delta1 is a
#: cash/futures position, so the option fee model does not describe it and
#: netting it at option rates would invent a cost it would not pay. It is here
#: to size the prize, not to be traded.
ARMS = {
    'naked_long':   {'legs': ('long',),         'tp': True,  'stop_frac': 0.50, 'fills': 2},
    'naked_hold':   {'legs': ('long',),         'tp': True,  'stop_frac': None, 'fills': 2},
    'naked_runner': {'legs': ('long',),         'tp': False, 'stop_frac': None, 'fills': 2},
    'spread_hold':  {'legs': ('long', 'short'), 'tp': True,  'stop_frac': None, 'fills': 4},
    'delta1':       {'legs': (),                'tp': True,  'stop_frac': None, 'fills': 0,
                     'reference': True},
}


# -- state -------------------------------------------------------------------

def _load() -> dict:
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get('shadows'), dict):
            raise ValueError('unexpected shape')
        return d
    except FileNotFoundError:
        return {'schema': SCHEMA, 'shadows': {}}
    except Exception as e:
        # A measurement file must never be able to stop a cycle, and it holds
        # nothing that cannot be re-derived going forward. Move it aside rather
        # than lose every future observation to a parse error on the old one.
        logger.warning('shadow state unreadable (%s) -- starting a new one', e)
        try:
            os.replace(str(STATE_FILE), str(STATE_FILE) + '.unreadable')
        except OSError:
            pass
        return {'schema': SCHEMA, 'shadows': {}}


def _save(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_FILE) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, str(STATE_FILE))


# -- pricing -----------------------------------------------------------------

def arm_value(arm: dict, lq: Optional[dict], sq: Optional[dict],
              width: Optional[float] = None) -> tuple:
    """(value, quality). Value is what CLOSING this arm would pay, per share.

    Priced at the side actually traded against: the long is sold at the BID,
    the short bought back at the ASK. A `None` value means DO NOT BOOK -- defer
    to the next poll, exactly as the live engine does on an unusable book.

    CLAMPED to the structure's mathematical bounds, which is a different act
    from refusing a quote and gets the opposite treatment (see the valuation
    table in CLAUDE.md). A vertical cannot be worth less than 0 -- expiry is
    always available and costs nothing -- nor more than its width. Without the
    lower bound a wide book books a P&L past -100% on a -100%-capped structure,
    which is exactly how PIIND #50 recorded -112.4%.

    The INTRINSIC FLOOR is deliberately not replicated here. That one is a
    heuristic estimate of fair value and its rule is REFUSE, not clamp;
    clamping to it would invent a fill. The garbage-quote case it guards is
    already caught upstream by each leg's `reliable` flag, and this arm defers
    on an unreliable leg rather than valuing it.
    """
    legs = arm['legs']
    if not legs:                                    # delta1 is priced on spot
        return (None, 'spot_arm')
    if lq is None or not lq.get('reliable', False):
        return (None, 'long_' + ((lq or {}).get('unreliable_reason') or 'no_quote'))
    bid = lq.get('bid') or 0
    if bid <= 0:
        # A zero bid is a real price -- the sale raises nothing -- but a MISSING
        # one is not, and `_quote_option` returns 0 for both. This arm defers
        # rather than book a total loss it cannot tell from a dead feed.
        return (None, 'long_no_bid')
    if 'short' not in legs:
        # A long option's floor is 0 and it has no ceiling. `bid` is already
        # non-negative here, so there is nothing to clamp.
        return (float(bid), 'ok')
    if sq is None or not sq.get('reliable', False):
        return (None, 'short_' + ((sq or {}).get('unreliable_reason') or 'no_quote'))
    ask = sq.get('ask') or 0
    if ask <= 0:
        return (None, 'short_no_ask')
    v = float(bid) - float(ask)
    v = max(0.0, v)
    if width and width > 0:
        v = min(v, float(width))
    return (v, 'ok')


def delta1_mark(direction: str, entry_spot: float, spot: float) -> float:
    """Mark a delta-1 position, direction-adjusted.

    A PE signal is SHORT the underlying: it gains as spot falls. Marking the
    arm at raw spot would report every PE the wrong way round -- a profitable
    fall booked as a loss, and `peak` tracking the worst point instead of the
    best. Mirroring about the entry keeps one `(value - entry) / entry` formula
    correct for both directions, which is the same sign-flip `spot_shadow`
    already isolates into a single line for the same reason.
    """
    return float(spot) if direction == 'CE' else 2.0 * float(entry_spot) - float(spot)


def _tp_hit(direction: str, spot: float, target: float) -> bool:
    return spot >= target if direction == 'CE' else spot <= target


def _within_open_buffer(now: datetime) -> bool:
    open_h, open_m = cfg.MARKET_OPEN
    since = (now.hour * 3600 + now.minute * 60 + now.second) \
        - (open_h * 3600 + open_m * 60)
    return 0 <= since < cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC


def _sessions_left(expiry, today: date) -> Optional[int]:
    try:
        return sessions_between(today, date.fromisoformat(str(expiry)[:10]))
    except Exception:
        return None


# -- lifecycle ---------------------------------------------------------------

def entry_value(arm: dict, trade: dict) -> Optional[float]:
    """What this arm PAID at entry, per share, from the trade's own entry book.

    Taken from the stored entry books rather than re-quoted, so an arm's P&L is
    measured against the price its parent actually paid at the same instant.
    """
    if not arm['legs']:
        return trade.get('entry_spot') or trade.get('trigger_spot')
    la = trade.get('long_ask_entry')
    if not la or la <= 0:
        return None
    if 'short' not in arm['legs']:
        return float(la)
    d = trade.get('debit')
    return float(d) if d and d > 0 else None


#: How far behind its parent a shadow may open and still be counted as a
#: complete observation. A position enters in `check_watching`, which runs
#: AFTER the exit phase, so the shadow legitimately opens on the NEXT cycle --
#: about five minutes later. Anything beyond one open-buffer's worth is a real
#: unobserved head, whether or not it happens to fall on the same DAY: a
#: shadow opened at 14:00 on a position that entered at 09:30 has missed four
#: and a half hours, and a date comparison calls that complete.
MAX_OPEN_LAG_SEC = 900


def opened_late(trade: dict, ts: str) -> bool:
    """Did this shadow start materially after its parent entered?

    Unknown entry time falls back to the DATE, and an unparseable `ts` counts
    as late -- the safe direction, because a shadow wrongly marked partial is
    excluded from a count while one wrongly marked complete corrupts it.
    """
    day = str(trade.get('entry_date'))[:10]
    tm = trade.get('entry_time')
    try:
        if not tm:
            return day != ts[:10]
        entered_at = datetime.strptime('%s %s' % (day, str(tm)[:8]),
                                       '%Y-%m-%d %H:%M:%S')
        seen_at = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
    except (TypeError, ValueError):
        return True
    return (seen_at - entered_at).total_seconds() > MAX_OPEN_LAG_SEC


def open_shadows(state: dict, entered: list, ts: str) -> int:
    """Open a shadow for every entered COHORT position that has none.

    Cohort only: the pre-cohort book is a different strategy, and mixing the
    two is exactly what made the last naked-long answer point both ways at once
    (it wins on cohort records, loses badly on the older ITM-long ones).
    """
    n = 0
    for t in entered:
        tid = str(t.get('id'))
        if tid in state['shadows'] or not in_cohort(t):
            continue
        if not (t.get('long_symbol') and t.get('tp_spot') and t.get('expiry')):
            continue
        # `_tp_hit` and `delta1_mark` both treat "not CE" as PE, so an unknown
        # direction would be silently shadowed as a bear position. Refuse it
        # instead of measuring the wrong side of the market.
        if t.get('direction') not in ('CE', 'PE'):
            logger.warning('shadow: #%s has direction %r — not shadowed',
                           tid, t.get('direction'))
            continue
        # TIME is the ONLY exit `naked_runner` has, and it is derived from the
        # expiry. An unparseable expiry makes `_sessions_left` return None, so
        # that arm would never terminate -- an immortal shadow reported as
        # "still being measured" for ever. Refuse it at the door instead.
        if _sessions_left(t.get('expiry'), date.today()) is None:
            logger.warning('shadow: #%s has unusable expiry %r — not shadowed',
                           tid, t.get('expiry'))
            continue
        arms = {}
        for key, arm in ARMS.items():
            ev = entry_value(arm, t)
            if not ev or ev <= 0:
                continue
            arms[key] = {'status': 'open', 'entry_value': round(float(ev), 4),
                         'fills': arm['fills'], 'peak': round(float(ev), 4),
                         'exit': None}
        if not arms:
            continue
        state['shadows'][tid] = {
            'stock': t.get('stock'), 'direction': t.get('direction'),
            'long_symbol': t.get('long_symbol'),
            'short_symbol': t.get('short_symbol'),
            'target_spot': t.get('tp_spot'), 'expiry': str(t.get('expiry'))[:10],
            'quantity': t.get('quantity'), 'entry_spot': t.get('entry_spot'),
            'entry_date': str(t.get('entry_date'))[:10],
            # The ENTRY book, per leg, so fees can be costed on the turnover
            # each arm actually transacts. A naked arm buys the whole ATM
            # premium where the spread buys a NET debit roughly half the size,
            # so halving the spread's fee to model a 2-fill arm understates it
            # -- STT and the percentage charges follow turnover, not fills.
            'long_ask_entry': t.get('long_ask_entry'),
            'short_bid_entry': t.get('short_bid_entry'),
            'width': t.get('width'),
            'debit_to_width_pct': t.get('debit_to_width_pct'),
            # A shadow opened after its parent entered has an UNOBSERVED HEAD:
            # its stop may already have fired where nothing was watching, so
            # the reader must be able to mark it PARTIAL rather than count it.
            'since': ts,
            'opened_late': opened_late(t, ts),
            'polls': 0, 'arms': arms,
        }
        n += 1
    return n


def _close(sh: dict, key: str, reason: str, value: Optional[float],
           spot: Optional[float], ts: str, lq: Optional[dict] = None,
           sq: Optional[dict] = None) -> None:
    a = sh['arms'][key]
    if a['status'] != 'open':
        return                        # a booked exit is NEVER re-priced
    ev = a['entry_value']
    a['status'] = 'exited'
    a.pop('pending', None)
    a['exit'] = {
        'reason': reason, 'at': ts,
        'spot': None if spot is None else round(float(spot), 2),
        'value': None if value is None else round(float(value), 4),
        'pnl_pct': None if value is None else round(100.0 * (value - ev) / ev, 2),
        # An exit that never found a price. It is NOT dropped -- an unpriceable
        # book correlates with a bad outcome, so silently discarding these
        # biases the very count this module exists to keep. The reader shows
        # them in their own column.
        'unpriced': value is None,
        'polls': sh.get('polls', 0),
        # The exit BOOK, not just the scalar. `exit_debit` alone is what left
        # the live engine unable to reconstruct the one direction that has
        # twice cost real money; an option book cannot be rebuilt after the
        # fact, and this is also what lets fees be costed exactly per leg.
        'legs': {
            'long': None if lq is None else {'bid': lq.get('bid'),
                                             'ask': lq.get('ask')},
            'short': None if sq is None else {'bid': sq.get('bid'),
                                              'ask': sq.get('ask')},
        },
    }


def poll_one(sh: dict, spot: Optional[float], lq: Optional[dict],
             sq: Optional[dict], now: datetime, today: date) -> None:
    """Advance one shadow by one poll. Mutates `sh`; touches nothing else."""
    ts = now.strftime('%Y-%m-%d %H:%M:%S')
    sh['polls'] = sh.get('polls', 0) + 1
    direction = sh.get('direction')
    # Spot is frozen BY DESIGN inside the cash closing auction, so a spot
    # trigger read there is a statement about a price that cannot print.
    spot_live = (spot is not None and spot > 0
                 and not market_session.cash_price_is_frozen(now))
    value_armed = not _within_open_buffer(now)
    left = _sessions_left(sh.get('expiry'), today)

    for key, arm in ARMS.items():
        a = sh['arms'].get(key)
        if not a or a['status'] != 'open':
            continue
        entry_spot = sh.get('entry_spot')
        v = None
        if not arm['legs'] and spot_live and entry_spot:
            v = delta1_mark(direction, entry_spot, spot)
        if arm['legs']:
            v = arm_value(arm, lq, sq, sh.get('width'))[0]
        if v is not None and v > a.get('peak', 0):
            a['peak'] = round(float(v), 4)

        expired = left is not None and left <= 0

        # A TRIGGER THAT FIRED BUT COULD NOT BE PRICED IS NOT AN EXIT YET.
        #
        # TP and TIME both fire on something other than the option book -- a
        # spot level and the calendar -- so either can arrive on a poll where
        # the book is unusable. Booking `value=None` there records an exit with
        # no P&L, and a reader that skips those silently loses the whole
        # observation. That would have hit `naked_runner` hardest, whose ONLY
        # exit is TIME: one bad book on the deadline poll and the arm this
        # module exists to measure vanishes from its own scorecard.
        #
        # So the trigger LATCHES and the booking waits for a price, which is
        # the same rule the live engine already runs on ("paper never books a
        # price it could not have transacted at"). The latch is not released
        # if the condition later stops holding: the trigger did fire, and
        # re-deciding it on a later print would be a different rule.
        pend = a.get('pending')
        if pend:
            if v is not None:
                _close(sh, key, pend['reason'], v, spot, ts, lq, sq)
            elif expired:
                # Backstop. Past its own contract there is no future poll that
                # can price it, so record it UNPRICED rather than leave an arm
                # open for ever pretending it is still being measured.
                _close(sh, key, pend['reason'], None, spot, ts, lq, sq)
            continue

        # TIME first, because it is a statement about the CALENDAR rather than
        # about a price: it has to fire on a session where nothing quotes, or a
        # blind spell at expiry leaves an arm open past its own contract.
        if left is not None and left <= cfg.TIME_SL_DAYS:
            if v is not None:
                _close(sh, key, 'time', v, spot, ts, lq, sq)
            elif expired:
                _close(sh, key, 'time', None, spot, ts, lq, sq)
            else:
                a['pending'] = {'reason': 'time', 'since': ts}
            continue
        if arm['tp'] and spot_live and _tp_hit(direction, spot, sh['target_spot']):
            if v is not None:
                _close(sh, key, 'tp', v, spot, ts, lq, sq)
            else:
                a['pending'] = {'reason': 'tp', 'since': ts}
            continue
        # The stop is the one trigger that CANNOT arrive unpriced: it is
        # defined on `v`, so reaching here at all means there was a price.
        if (arm['stop_frac'] is not None and value_armed and v is not None
                and v <= a['entry_value'] * arm['stop_frac']):
            _close(sh, key, 'stop', v, spot, ts, lq, sq)


def poll(store, kite, ltps: Optional[dict] = None) -> dict:
    """One cycle of shadow measurement. Never raises.

    Polls EVERY open shadow, including those whose real position has already
    closed -- which is the only way `naked_runner` can be measured at all.
    """
    try:
        if not cfg.STRUCTURE_SHADOW_ENABLED:
            # SAID OUT LOUD, every cycle. A measurement that stops silently is
            # worse than one that never started: the gap is invisible in the
            # output and the count simply reads short months later. The vetting
            # banner in this engine was logged before its handler existed and
            # so never reached the cron log at all -- state lines have to be
            # greppable in production, not merely present in the source.
            logger.info('SHADOW structures: DISABLED '
                        '(structure_shadow_enabled=false) — nothing measured')
            return {'disabled': True}
        state = _load()
        # WHOLE BOOK: a shadow outlives its parent, so the parent must be
        # findable whatever status it now carries. `open_shadows` is what
        # scopes new shadows to the cohort.
        trades = store.load_trades()
        by_id = {str(t.get('id')): t for t in trades}
        entered = [t for t in trades if t.get('status') == 'entered']
        now = datetime.now(cfg.IST)
        ts = now.strftime('%Y-%m-%d %H:%M:%S')
        opened = open_shadows(state, entered, ts)

        live = {k: v for k, v in state['shadows'].items()
                if any(a['status'] == 'open' for a in v['arms'].values())}
        if not live:
            if opened:
                _save(state)
            return {'opened': opened, 'live': 0}

        # Spot: reuse whatever the exit phase already fetched and pay only for
        # the names it did not need -- the ORPHANS, whose parent has closed.
        ltps = dict(ltps or {})
        missing = sorted({v['stock'] for v in live.values()
                          if not (ltps.get(v['stock']) or 0) > 0})
        if missing:
            try:
                from playbook.magnet.scanner import get_ltp
                ltps.update(get_ltp(kite, missing) or {})
            except Exception as e:
                logger.debug('shadow spot fetch failed for %s: %s', missing, e)

        today = now.date()
        closed = 0
        for tid, sh in live.items():
            try:
                lq = sq = None
                if sh.get('long_symbol'):
                    lq = strikes_mod._quote_option(kite, sh['long_symbol'])
                needs_short = any(
                    'short' in ARMS[k]['legs'] and a['status'] == 'open'
                    for k, a in sh['arms'].items() if k in ARMS)
                if sh.get('short_symbol') and needs_short:
                    sq = strikes_mod._quote_option(kite, sh['short_symbol'])
                before = sum(1 for a in sh['arms'].values() if a['status'] == 'open')
                poll_one(sh, ltps.get(sh['stock']), lq, sq, now, today)
                closed += before - sum(1 for a in sh['arms'].values()
                                       if a['status'] == 'open')
                # The parent is gone and this shadow is still running -- the
                # state the whole module exists to reach.
                sh['orphan'] = by_id.get(tid, {}).get('status') != 'entered'
            except Exception as e:
                logger.debug('shadow poll failed for #%s: %s', tid, e)
        _save(state)
        summary = {'opened': opened, 'live': len(live), 'closed_arms': closed,
                   'orphans': sum(1 for v in live.values() if v.get('orphan'))}
        logger.info('SHADOW structures: %d live (%d orphaned), %d opened, '
                    '%d arm(s) closed this cycle',
                    summary['live'], summary['orphans'], opened, closed)
        return summary
    except Exception as e:                          # never raise into a cycle
        logger.warning('structure shadow failed: %s', e, exc_info=True)
        return {}


# -- backfill ----------------------------------------------------------------

def _obs_leg(o: dict, side: str) -> Optional[dict]:
    """A leg quote rebuilt from one persisted POLL observation.

    Reliability is taken from the observation's OWN `q` -- the engine's
    post-gate verdict at that poll -- rather than re-derived here. The paths do
    not store depth, so a local re-derivation would fail every leg for want of
    `bid_qty`; and re-deriving a guard beside the one that shipped is how this
    codebase grows two spellings of the same rule.

    `q == 'ok'` is the SPREAD's verdict, so applying it to a single leg is
    stricter than necessary. That is the safe direction: a backfilled arm never
    books on a book the live engine mistrusted.
    """
    b, a = o.get('%s_bid' % side), o.get('%s_ask' % side)
    if b is None and a is None:
        return None
    return {'bid': b or 0, 'ask': a or 0, 'reliable': o.get('q') == 'ok',
            'unreliable_reason': o.get('q') or 'unknown'}


def backfill(store, paths_dir=None) -> dict:
    """Seed shadows for OPEN cohort positions by replaying their value paths.

    Only positions still `entered`, and only where the stored path starts on or
    before the entry date -- so coverage is COMPLETE and the shadow carries no
    unobserved head. A closed position is deliberately excluded: its path stops
    at its own exit, which is precisely where `naked_runner` gets interesting,
    so backfilling one would bake in an unobserved TAIL and quietly answer the
    question this module exists to ask.

    SAFE-TO-RERUN: skips any position that already has a shadow, and a booked
    arm exit is never re-priced, so a second run adds nothing.
    """
    import glob
    from collections import defaultdict

    d = paths_dir or (cfg.LOG_DIR / 'eod')
    obs = defaultdict(list)
    for f in sorted(glob.glob(str(d / 'paths_*.json'))):
        try:
            with open(f, encoding='utf-8') as fh:
                day = json.load(fh)
            for tid, t in (day.get('trades') or {}).items():
                obs[str(tid)].extend(t.get('obs') or [])
        except Exception as e:
            logger.warning('shadow backfill: unreadable %s: %s', f, e)
    for k in obs:
        obs[k].sort(key=lambda o: o.get('ts') or '')

    state = _load()
    seeded, skipped = [], []
    for t in store.load_trades():        # WHOLE BOOK: open_shadows scopes it
        if t.get('status') != 'entered' or not in_cohort(t):
            continue
        tid = str(t.get('id'))
        if tid in state['shadows']:
            continue
        o = obs.get(tid) or []
        entry_day = str(t.get('entry_date'))[:10]
        if not o or (o[0].get('ts') or '')[:10] > entry_day:
            skipped.append((tid, t.get('stock'),
                            'no path coverage from entry'))
            continue
        if not open_shadows(state, [t], o[0]['ts']):
            skipped.append((tid, t.get('stock'), 'could not be shadowed'))
            continue
        sh = state['shadows'][tid]
        sh['opened_late'] = False        # coverage is complete from entry
        sh['backfilled'] = True
        for ob in o:
            try:
                now = datetime.strptime(ob['ts'], '%Y-%m-%d %H:%M:%S')
            except (TypeError, ValueError, KeyError):
                continue
            poll_one(sh, ob.get('spot'), _obs_leg(ob, 'long'),
                     _obs_leg(ob, 'short'), now, now.date())
        seeded.append((tid, t.get('stock'), len(o)))
    if seeded:
        _save(state)
    return {'seeded': seeded, 'skipped': skipped}


# -- reader ------------------------------------------------------------------

def _days_held(sh: dict, exit_at: str) -> Optional[int]:
    """Calendar days from the shadow opening to this arm's exit.

    Surfaced because the arms do NOT hold for comparable periods and RoC alone
    hides it: `naked_runner` has no TP, so it runs to its TIME stop -- roughly
    28 days against the spread's ~5. A per-trade return that takes five times
    as long to earn is not the same return, and this book's own measured edge
    is VELOCITY (winners at +14.7% per slot-session), so an arm compared
    without its duration is being flattered.
    """
    try:
        a = datetime.strptime(sh['since'], '%Y-%m-%d %H:%M:%S')
        b = datetime.strptime(exit_at, '%Y-%m-%d %H:%M:%S')
        return max(0, (b - a).days)
    except (KeyError, TypeError, ValueError):
        return None


def arm_fees(arm: dict, sh: dict, a: dict) -> Optional[float]:
    """Round-trip charges for one arm, from the REAL fee model.

    Built from the per-leg entry and exit books rather than scaled off the
    spread's total, because charges follow TURNOVER, not fill count: a naked
    arm buys the whole ATM premium where the spread pays a net debit about half
    that, so a naked round trip is NOT simply half a spread's. `None` for a
    reference arm, which is not an option position and would be given a cost it
    would never pay.
    """
    if arm.get('reference'):
        return None
    try:
        from . import fees as fees_mod
        q = int(sh.get('quantity') or 0)
        if q <= 0:
            return None
        ex = a.get('exit') or {}
        legs = ex.get('legs') or {}
        # Built here rather than through `fees._leg_orders`, whose signature
        # takes a `structure` and a `debit` this arm does not have and whose
        # body happens to ignore them. `estimate` is the public surface and the
        # order shape is its documented input; depending on the private
        # builder's argument order for a call that passes it dummies is a
        # break waiting for the next edit of that file.
        pairs = [('long', 'BUY', sh.get('long_ask_entry'), 'entry'),
                 ('long', 'SELL', (legs.get('long') or {}).get('bid'), 'exit')]
        if 'short' in arm['legs']:
            pairs += [('short', 'SELL', sh.get('short_bid_entry'), 'entry'),
                      ('short', 'BUY', (legs.get('short') or {}).get('ask'), 'exit')]
        # ALL FOUR (or two) legs, or nothing. A partial order list costs a
        # half round trip and returns a number that looks like a full one --
        # and an understated fee reads as free money in the net column. An
        # exit booked before `legs` was persisted, or an unpriced exit, has to
        # come back UNKNOWN rather than cheap.
        if any(px is None for _, _, px, _ in pairs):
            return None
        orders = [{'leg': leg, 'side': side, 'price': float(px), 'qty': q,
                   'when': when} for leg, side, px, when in pairs]
        return float(fees_mod.estimate(orders).get('total') or 0.0)
    except Exception as e:
        logger.debug('shadow fee estimate failed: %s', e)
        return None


def scorecard() -> dict:
    """Per-arm results, for `python -m zebra shadow`. Read-only.

    UNPRICED exits are returned in their own bucket, never dropped. An
    unpriceable book correlates with a bad outcome, so quietly discarding those
    rows would bias the count in the optimistic direction -- the same way
    dropping the value-bound clamps turned the overnight measure from -12.1%
    into +1.3%.
    """
    state = _load()
    out, unpriced, pending, still_open = {}, {}, {}, {}
    for key, arm in ARMS.items():
        rows, un, pend, op = [], [], 0, 0
        for tid, sh in state['shadows'].items():
            a = sh['arms'].get(key)
            if not a:
                continue
            if a['status'] == 'open':
                pend += 1 if a.get('pending') else 0
                op += 1
                continue
            ex = a.get('exit') or {}
            if ex.get('pnl_pct') is None:
                un.append({'id': tid, 'stock': sh['stock'],
                           'reason': ex.get('reason'), 'at': ex.get('at')})
                continue
            fee = arm_fees(arm, sh, a)
            gross = (ex['value'] - a['entry_value']) * (sh.get('quantity') or 0)
            rows.append({
                'id': tid, 'stock': sh['stock'],
                'pnl_pct': ex['pnl_pct'], 'reason': ex['reason'],
                'capital': (a['entry_value'] or 0) * (sh.get('quantity') or 0),
                'pnl': gross,
                # None means UNCOSTED, never "cost nothing". A reference arm
                # is genuinely uncosted by this model; an option arm with no
                # exit book simply is not known, and the reader must not add
                # its gross into a net total as though the fee were zero.
                'net': (gross if arm.get('reference')
                        else (None if fee is None else gross - fee)),
                'fees': fee,
                'days': _days_held(sh, ex.get('at')),
                'partial': bool(sh.get('opened_late')),
                'fills': a.get('fills'),
                'reference': bool(arm.get('reference')),
            })
        out[key], unpriced[key], pending[key], still_open[key] = rows, un, pend, op
    return {'arms': out, 'unpriced': unpriced, 'pending': pending,
            'still_open': still_open,
            'open': sum(1 for sh in state['shadows'].values()
                        if any(a['status'] == 'open' for a in sh['arms'].values())),
            'shadows': len(state['shadows'])}


def censored(sc: dict, key: str) -> bool:
    """Is this arm's win rate still a statement about its FAST exits only?

    THE ARMS DO NOT CENSOR ALIKE, and that difference manufactures a win rate.
    An arm with a stop closes on both sides. An arm WITHOUT one closes only on
    TP -- which arrives in about 4 days -- or on TIME, about 28. So at any
    snapshot before the first TIME exits land, its closed set is nearly all
    winners and its losers are still sitting open. Driving the 23 closed cohort
    positions through this machinery showed exactly that: `naked_long` (with a
    stop) 57.9% wins over 19, `naked_hold` (same arm, no stop) 91.7% over 12,
    `spread_hold` 100% over 12 -- and `naked_runner`, whose only exit is TIME,
    resolved NOTHING at all.

    This engine has already been fooled by this once, on its own book: seven
    wins from seven closes, read as a 100% strategy, when the losers were
    simply still open. So the reader says CENSORED while any position of this
    arm is unresolved, rather than printing a number that looks finished.
    """
    return (sc.get('still_open', {}).get(key) or 0) > 0

"""One definition of which engine may BOOK an exit, and which states are illegal.

WHY THIS EXISTS
---------------
Two processes can close a BCS-cohort position: `zebra/monitor.py`, which books
paper exits at the structure mid, and `bcs/spread_monitor.py`, the only code in
the fleet that can place a real order. Four switches decide between them --
`paper_mode` and `exits_managed_externally` in `config/zebra_config*.json`,
`auto_entry` in the same place, and `--dry-run`, which lives on a crontab line
and in no config file at all. Until now the combinations were governed by a
hand-maintained table in `CLAUDE.md`, and that table was **documentation doing
a validator's job**: it went stale (its two-engine row was narrowed by the C5
record-level paper gate and nobody moved it), neither engine could consult it,
and its worst state -- NO ENGINE AT ALL -- is by construction the one that
looks healthy from every log.

So the table is not restated here. It is DERIVED, from one invariant:

    EVERY COHORT RECORD MUST HAVE EXACTLY ONE ENGINE THAT CAN BOOK ITS EXIT.

`booking_engines()` answers that per record class from the four switches, using
the same predicates the engines themselves use, and `classify()` turns the
answer into a verdict. A row can no longer drift out of step with the code,
because there are no rows.

WHAT "REFUSE" MEANS, AND WHAT IT MUST NOT MEAN
----------------------------------------------
It does NOT mean refusing to start. Every illegal state here is either "no
engine" or "two engines", and killing a process in either one makes it worse:
the no-engine state is fixed by an engine standing UP, and a monitor that exits
on a bad config abandons the stops on the other three books as well.

`check()` therefore REFUSES THE ARMING, not the process: it returns a verdict
the caller announces loudly (log + a SAFETY Telegram) and, where the caller has
a switch of its own to withhold, it withholds it. Every fault names the single
switch that closes it.

WHY `common/`
-------------
Neither engine may own it. `zebra/monitor.py` imports `bcs.entry_executor` and
`bcs/zebra_adapter.py` imports `zebra.config`, so either home would deepen an
existing cycle -- and more to the point, a shared invariant living inside one of
the two things it constrains is how the `exits_managed_externally` stand-down
became one-sided in the first place. This module imports NEITHER engine: it
takes the switch values as arguments and returns a verdict. The engines read
their own switches and pass them in, which also makes every state in here
reachable from a test without monkeypatching a config file.
"""
from __future__ import annotations

from typing import Optional, Tuple

#: The two engines, named as a human would search the crontab for them.
ZEBRA = 'zebra/monitor.py'
MONITOR = 'bcs/spread_monitor.py'

#: The two classes of cohort record. They are governed by DIFFERENT rules, and
#: conflating them is what made the CLAUDE.md table wrong: a paper record is
#: always safe, and every illegal state below is about the other one.
PAPER_RECORD = 'paper'      # `paper: True` -- its legs never reached a broker
LIVE_RECORD = 'live'        # `paper: False` -- real legs at the broker
#: `paper` MISSING or not a bool. Not a shade of the other two -- a record the
#: two engines classify DIFFERENTLY, which is the one thing the invariant
#: cannot survive. See `booking_engines`.
UNSTAMPED_RECORD = 'unstamped'

NO_ENGINE = 'no_engine'
TWO_ENGINES = 'two_engines'
#: The monitor's arming could not be read (no usable heartbeat) while LIVE
#: cohort records are open. Not "no engine" -- we do not know -- and that is
#: exactly why it must not be silence.
UNVERIFIED = 'unverified_engine'
#: Distinct from TWO_ENGINES because the fix is different: two engines is a
#: switch problem, an unstamped record is a DATA problem and no switch closes
#: it.
UNSTAMPED = 'unstamped_record'


def booking_engines(*, paper_mode: bool, exits_external: bool,
                    dry_run: Optional[bool], record_is_paper: bool,
                    in_cohort: bool = True) -> Tuple[str, ...]:
    """Which engines could book THIS record's exit, given these switches.

    The two predicates below are the deployed ones, restated in terms of the
    switches rather than of a trade dict so that this module needs neither
    engine imported. They are pinned to the real functions by
    `common/tests/test_arming.py`, which drives `zebra.monitor._exits_external`
    and `zebra.monitor._paper_auto_close`'s own gate against the same inputs --
    a restatement that can drift is worth less than no restatement at all.

    `dry_run=None` means the monitor's arming is UNKNOWN to the caller (zebra
    learns it from a heartbeat file that may be missing). The monitor is then
    reported as POSSIBLY booking, and `classify` declines to raise a no-engine
    fault that turns on the unknown rather than certifying either verdict.
    """
    engines = []
    if record_is_paper is None:
        # THE UNSTAMPED RECORD, added 2026-08-31.
        #
        # The two engines read the flag with OPPOSITE defaults, each correctly
        # and each pinned by `test_the_two_paper_predicates_default_in_
        # opposite_directions`:
        #
        #   zebra/trade_store.is_paper_record   `trade.get('paper', True)`
        #       -> absent means PAPER, because the BCS-family stores hold real
        #          positions and carry no `paper` key at all;
        #   bcs/spread_monitor._record_says_paper  `trade.get('paper') is True`
        #       -> absent means LIVE, for the same reason read from the other
        #          side.
        #
        # Individually right; jointly, on ONE zebra cohort record that has
        # lost its key, zebra books it at mid AND the monitor places real
        # orders for it. Worse, `_exits_external` requires
        # `not is_paper_record`, so it reads the record as paper and the
        # stand-down switch CANNOT close the hole -- there is no combination
        # of the four switches that makes this record single-engine.
        #
        # It is modelled here rather than assumed away because the model's own
        # record axis was a single bool, so the cross-product test could not
        # express the state, and both preflights would have classified the
        # same record into different populations while each reported OK.
        #
        # Reachable without any code defect: a Drive `_merge` against an old
        # version, a snapshot restore, or a hand repair during an incident --
        # and this fleet performed exactly that class of store surgery on
        # 2026-08-30. Nothing validates the key on read.
        engines.append(ZEBRA)
        if dry_run is not True:
            engines.append(MONITOR)
        return tuple(engines)
    # zebra books at MID, and only a record whose legs never reached a broker
    # may be booked at a price nobody transacted. `paper_mode` does NOT appear
    # here: the mode says how NEW entries are made, the record says whether a
    # broker was ever involved, and only the second can license a mid close.
    # See `zebra.monitor._paper_auto_close`.
    stood_down = exits_external and in_cohort and not record_is_paper
    if record_is_paper and not stood_down:
        engines.append(ZEBRA)
    # The monitor places real orders, so it refuses a paper record outright
    # (`spread_monitor.close_spread`) and books nothing at all in dry run.
    if not record_is_paper and dry_run is not True:
        engines.append(MONITOR)
    return tuple(engines)


def _fault(state, record_class, detail, fix):
    return {'state': state, 'record_class': record_class,
            'detail': detail, 'fix': fix}


def classify(*, paper_mode: bool, exits_external: bool, auto_entry: bool,
             dry_run: Optional[bool], population=None) -> dict:
    """The verdict on one set of switches. Never raises.

    `population` is the set of record classes actually OPEN in the cohort book
    right now -- e.g. `{PAPER_RECORD}` for today's eight paper positions. It
    decides FAULT vs LATENT, and nothing else:

    * a class that is present and has != 1 booking engine is a **fault** --
      real positions are stranded or double-owned at this instant;
    * a class that is absent gets the identical finding as a **latent**
      warning -- the configuration WOULD strand one, and the moment a live
      record appears (`zebra enter` on a hand-placed trade is exactly how)
      it becomes a fault with no switch having moved.

    That distinction is the difference between an alarm and noise. Today's
    deployed state (paper_mode on, monitor in dry run) cannot book a LIVE
    cohort record and there are none, so calling it ILLEGAL on every startup
    would train the reader to swipe past the one message that matters.

    `None` means the caller does not know, and is read as BOTH PRESENT: an
    unknown population must not be able to certify safety.

    Returns::

        {'legal': bool,          # False iff a PRESENT class has != 1 engine
         'faults': [...],        # each naming the single switch that fixes it
         'latent': [...],        # the same findings for absent classes
         'warnings': [...],      # legal but misleading, e.g. an inert switch
         'engines': {PAPER_RECORD: (...), LIVE_RECORD: (...)},
         'summary': 'one line'}
    """
    # `None` = the caller does not know, and is read as EVERY class present,
    # unstamped included. An unknown population must not be able to certify
    # safety, and the unstamped class is the one a caller is least likely to
    # have looked for.
    present = ({PAPER_RECORD, LIVE_RECORD, UNSTAMPED_RECORD}
               if population is None else set(population))
    _flag = {PAPER_RECORD: True, LIVE_RECORD: False, UNSTAMPED_RECORD: None}
    engines = {
        cls: booking_engines(paper_mode=paper_mode,
                             exits_external=exits_external, dry_run=dry_run,
                             record_is_paper=_flag[cls])
        for cls in (PAPER_RECORD, LIVE_RECORD, UNSTAMPED_RECORD)
    }
    faults, latent, warnings = [], [], []

    if dry_run is None and LIVE_RECORD in present:
        # THE STATE THAT LOOKED HEALTHY FROM EVERY LOG (found 2026-08-31).
        #
        # With `dry_run=None`, `booking_engines` lists the monitor as POSSIBLY
        # booking, so a live record came back with exactly one engine and was
        # certified `ARMING: OK` -- while the reason the heartbeat was
        # unreadable may be that the monitor is dead. The `dry_run is None`
        # branch further down never ran, because a one-engine class never
        # reaches it, so the finding was not even recorded as latent.
        #
        # The scenario is not hypothetical: it is the arming order's own
        # intermediate step. A hand-placed live trade is filed while
        # `exits_managed_externally` is still false, and the monitor is
        # crash-looping on a dead Kite token -- it `sys.exit`s in `load_kite`
        # BEFORE writing its first beat, so there is no heartbeat at all.
        # zebra then declines to book the live record (correctly, it can only
        # book at mid) and, because `alert_if_exit_engine_down` fires only
        # from the stood-down branch, says nothing. Nothing books, nothing
        # alerts, every log reads OK.
        #
        # A present live record plus an unverifiable peer is therefore a
        # FAULT. It is not the no-engine fault -- there may well be an engine
        # -- so it names its own state and its own fix.
        faults.append(_fault(
            UNVERIFIED, LIVE_RECORD,
            'LIVE cohort records are open and the arming of the monitor '
            'could NOT be read (no usable exit-engine heartbeat), so '
            'nothing here can confirm any engine is able to book them. '
            'zebra cannot: it books at the structure mid.',
            'Check that `bcs.spread_monitor --cron` is running and writing '
            'its heartbeat — a dead Kite token makes it exit before the '
            'first beat. Until it beats, treat these positions as UNWATCHED '
            'and manage them by hand.'))

    for cls in (PAPER_RECORD, LIVE_RECORD, UNSTAMPED_RECORD):
        eng = engines[cls]
        if cls == UNSTAMPED_RECORD:
            # ALWAYS a finding, even at one engine. Under `--dry-run` the
            # monitor books nothing, so the count falls to one -- but that one
            # is zebra, booking a record that MAY have real legs at the
            # structure mid. The invariant is "exactly one engine", and the
            # unstated half of it is "and it is the right one for what this
            # record IS". An unstamped record makes that unanswerable, so
            # counting engines cannot clear it.
            found = _fault(
                UNSTAMPED, cls,
                'a cohort record has no usable `paper` flag, so the two '
                'engines DISAGREE about what it is: zebra reads a missing '
                'flag as paper (and would book it at the structure mid), the '
                'monitor reads it as live (and would place real orders). '
                'Engines that could act: %s. No switch closes this -- '
                '`exits_managed_externally` also reads it as paper, so the '
                'stand-down cannot fire.' % (' and '.join(eng) or 'none'),
                'Find the record and restore its `paper` flag to the truth '
                'about its legs: True if they never reached a broker, False '
                'if they did. Check the broker before deciding — guessing '
                'here books a real position at a fictional price, or leaves '
                'a real one unwatched.')
            # Reported ONLY when one is actually open, never as a latent.
            # The other two classes have latents because a SWITCH decides
            # them, so "what would happen if such a record appeared" is a
            # real question about the current configuration. This one is
            # decided by DATA: no switch makes it safe and no switch makes it
            # unsafe, so a standing latent would say nothing except "a record
            # could become corrupt", which is true of every record always and
            # is exactly the line a reader learns to skim past.
            if cls in present:
                faults.append(found)
            continue
        if len(eng) == 1:
            continue
        if not eng:
            if cls == LIVE_RECORD and dry_run is None:
                # Unreachable: with `dry_run=None` the monitor is always
                # listed, so a live record never has an empty engine list.
                # Kept as a named no-op rather than deleted, because the
                # ABSENT case is now handled above by the UNVERIFIED fault and
                # a future change to `booking_engines` must land somewhere
                # deliberate instead of falling into the no-engine text, which
                # would tell the reader the monitor is in dry run when nobody
                # knows what it is in.
                continue
            found = _fault(
                NO_ENGINE, cls,
                'NO engine can book a %s cohort record: zebra %s, and the '
                'monitor %s' % (
                    cls,
                    'has stood down (exits_managed_externally=true)'
                    if exits_external
                    else 'books only records whose legs never reached a '
                         'broker, and this is not one',
                    'is in DRY RUN' if dry_run
                    else 'refuses paper records outright'),
                # ONE switch, named -- and deliberately not "either of two".
                # zebra cannot be the answer for a live record whatever
                # `exits_managed_externally` says: booking a position that has
                # real legs at the structure MID is a fiction, not a close.
                # Offering it as an alternative is how an operator ends up
                # flipping the switch that makes the log look healthy.
                'Take --dry-run off the `bcs.spread_monitor --cron` crontab '
                'line. This is the only fix: zebra books at the structure '
                'mid, which is not a price a record with real legs could have '
                'transacted at, so it must never be the engine for one.')
        else:
            # UNREACHABLE from the predicates above, and that is the point.
            # zebra books only paper records and the monitor refuses them, so
            # no switch combination puts two engines on one record --
            # `test_no_switch_combination_produces_two_booking_engines`
            # asserts it over the whole cross product. This branch is not a
            # guard nobody can observe failing: it is what turns a future
            # change to either predicate into a named state instead of a
            # silent one, which is exactly how the two-engine row in CLAUDE.md
            # went stale without anybody noticing.
            found = _fault(
                TWO_ENGINES, cls,
                'TWO engines can book a %s cohort record (%s), so one '
                'position can be closed twice' % (cls, ' and '.join(eng)),
                'Set exits_managed_externally=true so zebra stands down for '
                'the records the order path owns.')
        (faults if cls in present else latent).append(found)

    # -- Legal, but not what the operator probably thinks --------------------
    if auto_entry and paper_mode:
        # `zebra/monitor.py` consults auto-entry only under `if not
        # cfg.PAPER_MODE`, so the switch is INERT here. Silence would leave
        # someone believing entries are armed when nothing can place one.
        warnings.append(
            'auto_entry is ON but paper_mode is true, which makes it INERT: '
            'the live entry path is only consulted when paper_mode is false.')
    if not exits_external and dry_run is False:
        # Both engines then run the whole cascade over the same records: two
        # vet requests against ONE shared marker per (trade, kind), a
        # double-incremented defer count, and two Telegrams per trigger.
        # Booking is still single, so this is not a fault.
        warnings.append(
            'exits_managed_externally is false while the monitor is armed: '
            'both engines evaluate every cohort trigger, which double-spends '
            'vet markers and sends two alerts per trigger. Booking itself is '
            'still single-engine.')
    if dry_run is None:
        warnings.append(
            "the monitor's arming is UNKNOWN here (no readable heartbeat), so "
            'this verdict cannot speak for live records.')

    if faults:
        summary = '; '.join(f['detail'] for f in faults)
    else:
        def _who(cls):
            if cls == LIVE_RECORD and dry_run is None:
                return 'UNKNOWN'
            eng = engines[cls]
            who = eng[0] if len(eng) == 1 else ('NONE' if not eng
                                                else ' and '.join(eng))
            return who if cls in present else who + ' (none open)'
        # UNSTAMPED is deliberately absent from the healthy summary: when
        # none are open it is not a state the book is in, and naming it every
        # cycle is how the reader learns to skim the line that matters. It
        # appears as a latent finding instead, which `describe` prints.
        summary = '; '.join('%s records -> %s' % (cls, _who(cls))
                            for cls in (PAPER_RECORD, LIVE_RECORD))
    return {'legal': not faults, 'faults': faults, 'latent': latent,
            'warnings': warnings, 'engines': engines, 'summary': summary}


def describe(state: dict) -> str:
    """The verdict as the block a human reads in a startup log."""
    lines = ['ARMING: ' + ('OK' if state['legal'] else 'ILLEGAL'),
             '  ' + state['summary']]
    for f in state['faults']:
        lines.append('  *** %s (%s records) ***'
                     % (f['state'].upper(), f['record_class']))
        lines.append('      ' + f['detail'])
        lines.append('      FIX: ' + f['fix'])
    for f in state.get('latent') or ():
        # Printed every time, and never as an alarm. This is the state the
        # book is ONE record away from, which is worth knowing on the day
        # somebody files that record -- and is not worth a red Telegram on a
        # book that does not contain one.
        lines.append('  latent %s: %s (no %s records are open)'
                     % (f['state'], f['detail'], f['record_class']))
    for w in state['warnings']:
        lines.append('  note: ' + w)
    return '\n'.join(lines)


def telegram_text(state: dict, engine: str) -> Optional[str]:
    """The SAFETY message for an illegal state, or None when there is none.

    Names the engine that noticed. Both engines send their own: the states here
    are precisely the ones where the OTHER engine may be absent, so a single
    sender would go quiet exactly when it matters.
    """
    if state['legal']:
        return None
    body = '\n'.join('%s\nFIX: %s' % (f['detail'], f['fix'])
                     for f in state['faults'])
    return ('\U0001F534 ILLEGAL ARMING STATE\n'
            'Noticed by %s.\n%s' % (engine, body))


def check(*, paper_mode: bool, exits_external: bool, auto_entry: bool,
          dry_run: Optional[bool], engine: str, population=None, log=None,
          telegram=None) -> dict:
    """`classify`, announced. Returns the verdict; NEVER raises, never exits.

    Refusing the PROCESS is the one response this must not have. A no-engine
    state is fixed by an engine standing up, and a monitor that dies on a bad
    config takes the stops on bcs / bear_put / fallen_hero down with it -- so
    the announcement IS the action, and any caller with a switch of its own to
    withhold reads `legal` and withholds it.
    """
    state = classify(paper_mode=paper_mode, exits_external=exits_external,
                     auto_entry=auto_entry, dry_run=dry_run,
                     population=population)
    if log is not None:
        for line in describe(state).splitlines():
            log(line)
    msg = telegram_text(state, engine)
    if msg and telegram is not None:
        try:
            telegram(msg)
        except Exception:            # pragma: no cover - never fatal
            # An unsendable alarm must not become a crash in a process that is
            # already in a state it cannot fix.
            pass
    return state

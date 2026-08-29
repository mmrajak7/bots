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

NO_ENGINE = 'no_engine'
TWO_ENGINES = 'two_engines'


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
    present = ({PAPER_RECORD, LIVE_RECORD} if population is None
               else set(population))
    engines = {
        cls: booking_engines(paper_mode=paper_mode,
                             exits_external=exits_external, dry_run=dry_run,
                             record_is_paper=(cls == PAPER_RECORD))
        for cls in (PAPER_RECORD, LIVE_RECORD)
    }
    faults, latent, warnings = [], [], []

    for cls in (PAPER_RECORD, LIVE_RECORD):
        eng = engines[cls]
        if len(eng) == 1:
            continue
        if not eng:
            if cls == LIVE_RECORD and dry_run is None:
                # Not a finding at all: the caller could not see the monitor's
                # arming, and an alarm raised on an unknown is one the reader
                # learns to discount. It comes back as a warning instead.
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

"""M7 - the money path must not read the BOX's timezone.

Every session gate here (market open, the order cutoffs, the settle buffers)
and every date stamp (the token's freshness, the daily nag keys, the log file
name) read `datetime.now()` / `date.today()` — local time on whatever machine
the process happens to be on. `zebra/` next door has been IST-aware since it
was written, so the TWO ENGINES managing the same positions disagreed about
what day it was on any box not set to IST.

Latent, not harmless. The Pi is IST today; if it ever is not, the failure is a
monitor that believes the market is shut, or a token it reads as stale, or a
daily nag that fires twice — silently, at the open, which is when both of this
account's real-money losses happened.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest bcs/tests/test_m7_ist_clock.py -v
"""
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from bcs import spread_monitor as sm


def test_the_offset_is_indias():
    assert sm.IST == timezone(timedelta(hours=5, minutes=30))


def test_it_agrees_with_zebras_definition():
    """Two engines, one exchange. A second offset that drifted from the first
    would put them a day apart on exactly the dates that matter."""
    from zebra import config as zcfg
    assert sm.IST == zcfg.IST


def test_the_clock_is_IST_regardless_of_the_box(monkeypatch):
    """The property, driven rather than asserted about: pin an absolute
    instant and check the wall time is India's, not the runner's."""
    utc_noon = datetime(2026, 9, 15, 12, 0, 0, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return utc_noon.astimezone(tz) if tz else utc_noon.replace(tzinfo=None)

    monkeypatch.setattr(sm, 'datetime', _DT)
    assert sm.now_ist() == datetime(2026, 9, 15, 17, 30, 0)
    assert sm.today_ist() == date(2026, 9, 15)


def test_the_date_rolls_on_ISTs_midnight_not_the_boxs(monkeypatch):
    """19:00 UTC is already tomorrow in India. A box on UTC would stamp the
    daily-nag key and the log file with YESTERDAY's date for the first four
    and a half hours of every Indian day."""
    evening_utc = datetime(2026, 9, 15, 19, 0, 0, tzinfo=timezone.utc)

    class _DT(datetime):
        @classmethod
        def now(cls, tz=None):
            return evening_utc.astimezone(tz) if tz else evening_utc.replace(tzinfo=None)

    monkeypatch.setattr(sm, 'datetime', _DT)
    assert sm.today_ist() == date(2026, 9, 16)


def test_the_clock_is_NAIVE():
    """Naive on purpose. Every comparison in the module is naive-vs-naive
    (`datetime.combine(today, MARKET_OPEN)`, the `.time()` gates), so an aware
    return would raise `TypeError: can't compare offset-naive and
    offset-aware` AT A SESSION GATE — a timezone tidy-up becoming an outage on
    the money path.
    """
    assert sm.now_ist().tzinfo is None
    # And it must survive the comparison it exists for.
    assert isinstance(
        sm.now_ist() >= datetime.combine(sm.today_ist(), sm.MARKET_OPEN), bool)


def test_the_replay_harness_can_still_drive_it(monkeypatch):
    """`ReplayClock` substitutes this module's `datetime` with a subclass whose
    `.now()` IGNORES its tz argument. A helper that reached the clock any other
    way — `date.today()`, `time.time()` — would make every replay run on the
    wall clock while looking pinned."""
    from bcs.tests.fakes import FakeClock

    FakeClock().install(monkeypatch, sm)
    pinned = sm.now_ist()
    assert sm.now_ist() == pinned
    assert sm.today_ist() == pinned.date()


#: Every module that reasons about EXCHANGE time — a session gate, a date
#: stamp on a shared record, an age measured against a broker timestamp.
#: DISCOVERED per module by its own IST helper, so adding a fifth needs no
#: edit here beyond the tuple.
IST_MODULES = (
    ('bcs.spread_monitor', 'now_ist'),
    ('bcs.entry_executor', None),        # uses sm.now_ist()
    ('bcs.order_journal', '_now_ist'),
    ('zebra.strikes', '_now_ist'),
)


@pytest.mark.parametrize('modname,helper', IST_MODULES,
                         ids=[m for m, _ in IST_MODULES])
def test_no_module_on_the_exchange_path_reads_the_box_clock(modname, helper):
    """THE GUARD'S OWN BOUNDARY WAS THE BUG.

    The first version parsed `bcs/spread_monitor.py` and nothing else, so
    "every bare `datetime.now()` is now `now_ist()`" was true of one file
    while three live mixed-clock reads sat one module over:

      * `bcs/entry_executor.py` compared the BOX clock against
        `LAST_ORDER_TIME`, an IST time-of-day, on the LIVE ENTRY ORDER PATH.
        On a UTC box that cutoff never fires.
      * `zebra/strikes._ltp_fresh` — the same-named TWIN of the function the
        sweep had just fixed, docstring "Mirrors bcs.get_option_depth" —
        still aged an exchange timestamp against the box clock.
      * `bcs/order_journal` stamped its intents and chose its day file on the
        box clock, so an intent would read 04:05 beside a monitor log line
        saying 09:35 — the forensic hazard the sweep's own message described.

    A guard scoped to the file its author happened to be editing is the
    module-boundary version of `feedback_the_copy_you_did_not_open`.
    """
    import ast
    import importlib

    mod = importlib.import_module(modname)
    tree = ast.parse(Path(mod.__file__).read_text(encoding='utf-8'))
    skip = (0, 0)
    if helper:
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == helper), None)
        assert fn is not None, '%s has no %s' % (modname, helper)
        skip = (fn.lineno, fn.end_lineno)
    bad = [n.lineno for n in ast.walk(tree)
           if isinstance(n, ast.Call)
           and isinstance(n.func, ast.Attribute) and n.func.attr == 'now'
           and getattr(n.func.value, 'id', None) == 'datetime'
           and not n.args and not n.keywords
           and not (skip[0] <= n.lineno <= skip[1])]
    assert not bad, (
        '%s reads the box clock at line(s) %s — every date decision in it is '
        'an EXCHANGE fact' % (modname, bad))


def test_every_ist_helper_agrees_on_the_offset():
    """Four definitions of "now" across three packages. One disagreeing by an
    hour would put the engines a session apart on exactly the dates that
    matter, and each is defined locally to avoid a circular import — which is
    precisely how they would drift."""
    import importlib

    from zebra import config as zcfg

    assert sm.IST == zcfg.IST
    from bcs import order_journal
    assert order_journal._IST == sm.IST
    for modname, helper in IST_MODULES:
        if not helper:
            continue
        mod = importlib.import_module(modname)
        a, b = getattr(mod, helper)(), sm.now_ist()
        assert abs((a - b).total_seconds()) < 5, modname


def test_a_stamp_this_module_writes_reads_back_as_the_same_instant():
    """The regression the sweep CREATED. Every stamp is naive IST now, and
    `datetime.fromisoformat(s).timestamp()` interprets a naive value in the
    BOX's zone — so `recovery_gate` compared an IST-derived epoch against
    `time.time()` and, on a UTC box, read a freeze as 5.5 hours in the FUTURE.
    `age` goes negative and the bounded class's 300s recovery grace becomes
    about five and a half hours: the M14 sweep dark for most of a session, on
    exactly the positions it exists to finish."""
    import time

    assert abs(time.time() - sm.ist_epoch(sm.now_ist().isoformat())) < 5


def test_no_naive_wall_clock_reads_are_left_on_the_session_gates():
    """DISCOVERY, not a list. The whole defect was that these reads were
    scattered, so a test naming the ones already fixed would pass at every
    moment of the bug's life.

    NOTHING may read the box clock — not the gates, not the date stamps, and
    not the log/journal timestamps either. The first version of this exempted
    "timestamps an operator reads beside the log", which sounded reasonable
    and was the loophole the two residual bugs lived in. The operator reads
    them on the Pi, which is IST; a log stamped in a different zone from the
    exchange is a forensic hazard, not a convenience. One module, one clock.
    """
    import ast

    # Parsed, not text-scanned. A `code = strip comment lines` pass still sees
    # inside DOCSTRINGS, so the first version of this guard failed on the
    # sentence in `now_ist`'s own docstring explaining why it does not use
    # `date.today()` — a text proxy flagging the documentation of the fix.
    tree = ast.parse(Path(sm.__file__).read_text(encoding='utf-8'))
    now_ist_fn = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.FunctionDef) and n.name == 'now_ist')
    now_ist_lines = (now_ist_fn.lineno, now_ist_fn.end_lineno)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute) and f.attr == 'today'
                and getattr(f.value, 'id', None) == 'date'):
            bad.append(('date.today()', node.lineno))
        # ANY bare `datetime.now()`, not just `.time()`. The first version of
        # this guard looked for exactly two shapes — `date.today()` and
        # `datetime.now().time()` — and the SAME COMMIT that added it left two
        # bare `datetime.now()` calls feeding session logic:
        #
        #   `is_spread_settled`  compared an IST `settle_time` against the BOX
        #                        clock, so on a UTC box the value stops (
        #                        SL_SPREAD, SL_TRAIL) stayed dark until 14:46
        #                        IST while `is_market_open()` — now correct —
        #                        reported a healthy running loop. WORSE than
        #                        before the fix, which is what a half-migrated
        #                        clock buys.
        #   `_ltp_fresh`         subtracted an exchange IST timestamp from the
        #                        box clock, so every print read fresh (or
        #                        every print read stale) depending on sign.
        #
        # Both passed the two-shape guard. A guard that enumerates the shapes
        # it already knows about passes at every moment of the bug's life —
        # `feedback_a_guard_can_pin_the_wrong_thing`. The property is that
        # this module has ONE clock, so the rule is: no bare `datetime.now()`
        # anywhere except inside `now_ist` itself.
        if (isinstance(f, ast.Attribute) and f.attr == 'now'
                and getattr(f.value, 'id', None) == 'datetime'
                and not node.args and not node.keywords
                and not (now_ist_lines[0] <= node.lineno <= now_ist_lines[1])):
            bad.append(('datetime.now()', node.lineno))
    assert not bad, (
        'the box clock is being read again at %s — use now_ist()/today_ist()'
        % ', '.join('%s line %d' % b for b in bad))

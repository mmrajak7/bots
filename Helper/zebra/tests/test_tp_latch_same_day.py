"""M13a — the take-profit latch expires at the end of the trading day it was
armed on.

Owner, 2026-08-28: *"TP latch should be for same day"*.

WHY THIS EXISTS. M13 made the first observed touch arm the exit PERMANENTLY,
which closed the COFORGE #436 leak (target hit at 09:25, vet allowed at 09:27,
spot gone by the next actionable poll, nothing booked). The implementing agent
flagged the tail of that rule: a touch followed by a permanently unusable book
exits on the first usable one, *days later if need be*. The owner has now bounded
it — a Monday touch must not book on Thursday's first print.

WHAT IS PINNED HERE
-------------------
  * a latch stamped on an earlier trading day reads as UNLATCHED
  * a latch stamped on the SAME trading day still arms, all session
  * the day is the **IST** date, not the host's and not UTC
  * the whole rule runs off an injected clock, so this file passes at 02:00 on
    a Sunday — the standing lesson `feedback_pin_the_wall_clock_in_tests`
  * expiry is evaluated on READ. Nothing sweeps, so a missed cron cycle, a
    weekend or a dead process cannot leave a latch armed
  * BOTH engines inherit it from the one shared function, including
    `bcs/spread_monitor.py`'s status-line tag
  * an expired-unbooked latch is RECORDED — that is the evidence that says
    whether same-day is the right rule

Run:  cd Helper && python -m pytest zebra/tests/test_tp_latch_same_day.py -v
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                       # noqa: E402
from zebra import monitor                             # noqa: E402
from zebra import trade_store as ts                   # noqa: E402
from zebra.trade_store import ZebraStore              # noqa: E402

IST = cfg.IST
UTC = timezone.utc

#: A Sunday, 02:00 IST — outside market hours, outside the working week, and
#: on the wrong side of midnight for a UTC host. Every clock in this file is
#: derived from a constant like this one; nothing reads the wall clock.
SUNDAY_0200 = datetime(2026, 8, 30, 2, 0, tzinfo=IST)

#: An ordinary session.
FRI = datetime(2026, 8, 28, 9, 25, tzinfo=IST)
FRI_LATE = datetime(2026, 8, 28, 15, 29, tzinfo=IST)
THU = datetime(2026, 8, 27, 9, 25, tzinfo=IST)

#: The field name as a literal. Not imported, so this file fails on BEHAVIOUR
#: pre-fix rather than dying at collection.
EXPIRED = 'tp_latch_expired'

TP = 100.0
SL = 90.0


def _stamped(when, spot=1934.2):
    """A record latched at `when`."""
    return {ts.TP_TOUCHED_AT: when.isoformat(), ts.TP_TOUCH_SPOT: spot}


class _FrozenClock:
    """`datetime` with `.now()` pinned, installed into a module.

    A SUBCLASS, so `.fromisoformat`, `.combine` and arithmetic keep working —
    the same trick `bcs/tests/replay.py` uses. `.now(tz)` honours the argument:
    a fake that answers naive to `now(IST)` would hide exactly the tz bug this
    file exists to stop.
    """

    def __init__(self, monkeypatch, at, *modules):
        self.at = at
        self._mp = monkeypatch
        clock = self

        class _DT(datetime):
            @classmethod
            def now(cls, tz=None):
                cur = clock.at
                return cur.astimezone(tz) if tz is not None else \
                    cur.astimezone().replace(tzinfo=None)

        class _D(date):
            @classmethod
            def today(cls):
                return clock.at.astimezone().date()

        for mod in modules:
            monkeypatch.setattr(mod, 'datetime', _DT)
            if hasattr(mod, 'date'):
                monkeypatch.setattr(mod, 'date', _D)

    def advance(self, **kw):
        self.at = self.at + timedelta(**kw)
        return self.at


# ── The decision itself, on an injected clock ───────────────────────────────

def test_a_latch_from_a_previous_day_reads_as_unlatched():
    """The whole owner decision in one line. Thursday's touch, Friday's poll."""
    rec = _stamped(THU)
    assert ts.tp_latched(rec, now=FRI) is False
    assert ts.tp_latch(rec, False, 1900.0, now=FRI)['armed'] is False, (
        "yesterday's touch armed today's exit — a Monday touch would book on "
        "Thursday's first usable print")


def test_a_latch_from_the_same_day_still_arms():
    """The thing M13 was built for must survive the bounding of it."""
    rec = _stamped(FRI)
    assert ts.tp_latched(rec, now=FRI_LATE) is True
    out = ts.tp_latch(rec, False, 1900.0, now=FRI_LATE)
    assert out['armed'] is True and out['latched'] is True
    assert out['patch'] == {}, 'the first touch of the day was re-stamped'


def test_the_boundary_is_the_IST_DATE_not_UTC():
    """Two instants that a UTC date rule reads backwards.

    01:30 IST on the 28th is 20:00 UTC on the 27th — a UTC rule expires a latch
    that is hours old. 00:30 IST on the 29th is 19:00 UTC on the 28th — a UTC
    rule keeps a latch that belongs to the NEXT trading day.
    """
    now = datetime(2026, 8, 28, 10, 0, tzinfo=IST)
    late_last_night = _stamped(datetime(2026, 8, 27, 20, 0, tzinfo=UTC))
    assert ts.tp_latched(late_last_night, now=now) is True, (
        'the day boundary is being read in UTC — 01:30 IST is the same '
        'trading day as 10:00 IST')
    tomorrow = _stamped(datetime(2026, 8, 28, 19, 0, tzinfo=UTC))
    assert ts.tp_latched(tomorrow, now=now) is False


def test_the_stamp_is_written_with_its_offset():
    """A naive stamp is a day boundary nobody can settle later. What is written
    has to say which clock it was written on."""
    patch = ts.tp_latch({}, True, 1934.2, now=FRI)['patch']
    written = datetime.fromisoformat(patch[ts.TP_TOUCHED_AT])
    assert written.tzinfo is not None, 'the touch was stamped with no offset'
    assert written.astimezone(IST).date() == FRI.date()


def test_a_naive_caller_clock_still_lands_on_the_right_day():
    """`bcs/spread_monitor.py` passes `now=datetime.now()` — naive local — and
    that file may not be edited. The store has to normalise it.

    The naive input here is the SAME INSTANT as the aware one, expressed on
    whatever clock this host runs on, so the assertion holds on an IST box, a
    UTC box and a laptop in California.
    """
    naive_now = FRI.astimezone().replace(tzinfo=None)
    naive_yesterday = THU.astimezone().replace(tzinfo=None)

    fresh = ts.tp_latch({}, True, 1934.2, now=naive_now)
    assert fresh['new_touch'] is True
    assert ts.tp_latched(fresh['patch'], now=FRI) is True, (
        'a latch armed off the naive engine clock did not read back on the '
        'IST clock — the two engines would disagree about the same record')

    stale = _stamped(datetime.fromisoformat(naive_yesterday.isoformat()))
    assert ts.tp_latched(stale, now=naive_now) is False


def test_a_naive_stamp_written_EARLIER_TODAY_still_arms():
    """M13 shipped this morning and wrote naive stamps. A position latched by
    it before this change must not be disarmed by the change — the bound is on
    the DAY, and today is still today.

    Built from the same instant expressed both ways, so it holds on any host.
    """
    legacy = {ts.TP_TOUCHED_AT: FRI.astimezone().replace(tzinfo=None).isoformat(),
              ts.TP_TOUCH_SPOT: 1934.2}
    assert ts.tp_latched(legacy, now=FRI_LATE) is True
    assert ts.tp_latch(legacy, False, 1900.0, now=FRI_LATE)['armed'] is True


def test_the_order_path_re_arm_keeps_the_lapsed_touch_in_ONE_write():
    """`bcs/spread_monitor.py` writes `patch` and nothing else — it may not be
    edited, and it never sees `expired_patch`. So when a touch on a NEW day
    overwrites a lapsed stamp, the evidence has to ride the same write or the
    order path silently destroys it."""
    stale = _stamped(THU)
    out = ts.tp_latch(stale, True, 1950.0, now=FRI)
    assert out['new_touch'] is True and out['expired'] is True
    assert out['expired_patch'] == {}, (
        'the lapse was returned on a key the order path does not read, on the '
        'one cycle that overwrites the stamp it describes')
    assert out['patch'][ts.TP_TOUCHED_AT].startswith('2026-08-28')
    assert out['patch'][EXPIRED][0]['touched_at'] == stale[ts.TP_TOUCHED_AT]
    assert out['patch'][EXPIRED][0]['touch_spot'] == 1934.2


def test_an_unreadable_stamp_reads_as_unlatched():
    """Safe direction: a stamp that cannot be dated cannot be proved to be
    today's, and falling back to the live comparison costs an opportunity —
    honouring it forever is what the owner just ruled out."""
    assert ts.tp_latched({ts.TP_TOUCHED_AT: 'yesterday-ish'}, now=FRI) is False
    assert ts.tp_latched({ts.TP_TOUCHED_AT: True}, now=FRI) is False


def test_absence_still_means_not_yet_touched():
    """462 records predate the field and 13 of them are in the cohort."""
    assert ts.tp_latched({}, now=FRI) is False
    assert ts.tp_latched({'stock': 'OLD', 'status': 'entered'}, now=FRI) is False


# ── The default clock: no argument, no wall clock ───────────────────────────

def test_the_default_clock_is_IST_and_the_test_passes_at_0200_on_a_sunday(
        monkeypatch):
    """Nothing above proves the DEFAULT path is IST — every call passes `now`.
    This one pins the module clock instead, on a Sunday at 02:00, which is when
    CI runs and when a naive-local rule quietly changes its answer."""
    _FrozenClock(monkeypatch, SUNDAY_0200, ts)
    assert ts.tp_latched(_stamped(SUNDAY_0200 - timedelta(hours=1))) is True
    assert ts.tp_latched(_stamped(FRI_LATE)) is False, (
        "Friday's unbooked touch was still armed on Sunday morning")


def test_expiry_needs_nothing_to_RUN(monkeypatch):
    """Evaluated on read, never by a sweep. A latch armed on Friday afternoon
    is dead on Monday even though the process died on Friday and nothing
    touched the record over the weekend — a missed cron cycle must not be able
    to leave an exit armed."""
    rec = _stamped(FRI_LATE)
    frozen = dict(rec)                       # nothing may mutate the record
    _FrozenClock(monkeypatch, datetime(2026, 8, 31, 9, 20, tzinfo=IST), ts)
    assert ts.tp_latched(rec) is False
    assert rec == frozen, 'expiry mutated the record it was only asked about'


# ── The session it WAS armed for ────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    """One entered CE position: TP 100, spot SL 90, debit 10 on a 40-wide."""
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    s = ZebraStore()
    s.add_signal({'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': TP, 'st_direction': 'UP',
                  'signal_price': 96.0, 'signal_gap_pct': 4.0})
    s.mark_entered(1, {'long_strike': 100.0, 'short_strike': 140.0,
                       'long_symbol': 'L', 'short_symbol': 'S', 'debit': 10.0,
                       'lot_size': 100, 'lots': 1, 'expiry': '2026-09-30',
                       'structure': 'bcs'})
    with s._mutate():
        t = s.find(1)
        t['tp_spot'] = TP
        t['sl_spot'] = SL
        t['debit_sl_value'] = 5.0
    return s


def _cycle(store, monkeypatch, spot, mid=30.0, reliable=True, gate=True):
    """One `check_entered` pass. `gate=False` is a vet DEFER."""
    sent = []
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'TESTCO': spot})
    monkeypatch.setattr(monitor, '_structure_quote',
                        lambda kite, t, spot=None: {
                            'mid': mid, 'reliable': reliable,
                            'reason': None if reliable else 'wide book',
                            'legs': {'long': {'symbol': 'L', 'bid': 40.0,
                                              'ask': 40.2},
                                     'short': {'symbol': 'S', 'bid': 10.0,
                                               'ask': 10.2}},
                            'floored': False})
    monkeypatch.setattr(monitor, '_exit_cleared', lambda *a, **k: gate)
    monitor.check_entered(store, kite=None, dry_run=True)
    return sent


def test_a_touch_survives_the_vet_defer_and_the_cron_gap_it_was_built_for(
        store, monkeypatch):
    """COFORGE #436 replayed on a pinned clock. 09:25 touch, vet defers, the
    next actionable poll is 5 minutes later with spot back below target — and
    the exit still books, because it is the same session."""
    clock = _FrozenClock(monkeypatch, FRI, ts)
    _cycle(store, monkeypatch, spot=150.0, gate=False)
    assert store.find(1)[ts.TP_TOUCHED_AT], 'the touch was not persisted'

    clock.advance(minutes=5)                 # the cron gap
    _cycle(store, monkeypatch, spot=90.5, gate=True)
    t = store.find(1)
    assert t['status'] == 'exited', (
        'the same-session latch was lost — this is the COFORGE leak M13 '
        'exists to close and the expiry must not reopen it')
    assert t['exit_reason'] == 'paper:tp'


def test_a_touch_at_1529_that_never_booked_is_dead_the_next_morning(
        store, monkeypatch):
    """The boundary case, stated in the owner's own terms. The book stays
    unusable to the close, so the exit is never booked; the next session opens
    with the trigger gone, not with a day-old exit waiting to fire."""
    clock = _FrozenClock(monkeypatch, FRI_LATE, ts)
    _cycle(store, monkeypatch, spot=150.0, mid=30.0, reliable=False)
    assert store.find(1)['status'] == 'entered', 'an unreliable book booked'
    assert store.find(1)[ts.TP_TOUCHED_AT], 'the touch was never recorded'

    clock.at = datetime(2026, 8, 31, 9, 20, tzinfo=IST)   # Monday
    _cycle(store, monkeypatch, spot=90.0, mid=30.0, reliable=True)
    t = store.find(1)
    assert t['status'] == 'entered', (
        "Friday's unbooked touch booked on Monday's first usable print")
    assert t[ts.TP_TOUCHED_AT], (
        'the stale stamp was deleted — expiry is a READ rule and the forensic '
        'record must survive it')


def test_the_expired_latch_is_recorded_as_evidence(store, monkeypatch):
    """Whether same-day is the right rule is an empirical question, and the
    only thing that can answer it is a count of the exits it gave up."""
    clock = _FrozenClock(monkeypatch, FRI_LATE, ts)
    _cycle(store, monkeypatch, spot=150.0, mid=30.0, reliable=False)

    clock.at = datetime(2026, 8, 31, 9, 20, tzinfo=IST)
    _cycle(store, monkeypatch, spot=90.0)
    lapsed = store.find(1).get(EXPIRED)
    assert lapsed, (
        'a take-profit touch expired unbooked and left no trace — that is the '
        'one number the owner needs to judge this rule')
    assert lapsed[0]['touch_spot'] == 150.0
    assert lapsed[0]['touched_at'].startswith('2026-08-28')

    clock.advance(minutes=5)
    _cycle(store, monkeypatch, spot=90.0)
    assert len(store.find(1)[EXPIRED]) == 1, (
        'the same lapsed latch was recorded once per poll — an evidence log '
        'that grows every five minutes is not evidence')


def test_a_new_touch_after_an_expiry_re_arms_and_keeps_the_old_one(
        store, monkeypatch):
    """The next session is a fresh session: a touch in it arms normally. The
    superseded stamp is kept, because it is the measurement."""
    clock = _FrozenClock(monkeypatch, FRI, ts)
    _cycle(store, monkeypatch, spot=150.0, mid=30.0, reliable=False)

    clock.at = datetime(2026, 8, 31, 9, 20, tzinfo=IST)
    _cycle(store, monkeypatch, spot=90.0)                  # notices the lapse
    _cycle(store, monkeypatch, spot=155.0, mid=30.0, reliable=False)  # touch
    t = store.find(1)
    assert t[ts.TP_TOUCHED_AT].startswith('2026-08-31'), 'the re-arm was lost'
    assert t[ts.TP_TOUCH_SPOT] == 155.0
    assert t[EXPIRED][0]['touch_spot'] == 150.0

    clock.advance(minutes=5)
    _cycle(store, monkeypatch, spot=91.0)
    assert store.find(1)['status'] == 'exited', 'the re-armed latch did not fire'


def test_the_poll_line_stops_claiming_an_expired_latch_is_armed(
        store, monkeypatch, caplog):
    """In paper the log is the entire forensic record. A position whose latch
    lapsed overnight must not still read `[TP-LATCHED]`."""
    import logging
    clock = _FrozenClock(monkeypatch, FRI, ts)
    _cycle(store, monkeypatch, spot=150.0, mid=30.0, reliable=False)

    clock.at = datetime(2026, 8, 31, 9, 20, tzinfo=IST)
    with caplog.at_level(logging.INFO, logger='zebra.monitor'):
        _cycle(store, monkeypatch, spot=90.0)
    polls = [r.message for r in caplog.records if r.message.startswith('POLL')]
    assert polls, 'no poll line at all'
    assert not any('TP-LATCHED' in m for m in polls), (
        'the log says a lapsed take-profit is still armed')
    assert any('LATCH EXPIRED' in r.message for r in caplog.records), (
        'the lapse passed silently — the one event this rule gives up an exit '
        'for must be visible')


# ── One decision, two engines ───────────────────────────────────────────────

def test_both_engines_read_the_same_expiry(monkeypatch):
    """`bcs/spread_monitor.py` is the engine that places real orders and it
    delegates to the SAME function. Pinned here because a rule honoured by one
    engine and not the other is this codebase's most repeated defect."""
    from bcs import spread_monitor as sm
    _FrozenClock(monkeypatch, FRI_LATE, ts, sm)

    stale = dict(_stamped(THU), id=419, stock='TESTCO', paper=True)
    fresh = dict(_stamped(FRI), id=420, stock='TESTCO', paper=True)

    assert ts.tp_latched(stale) is False
    assert sm.tp_armed(stale, False, 1900.0, store=None, dry_run=True) is False, (
        'the order path would have closed a position on a touch from the '
        'previous session')
    assert sm._tp_latch_tag(stale) == '', (
        "the order path's status line still calls a lapsed latch armed")

    assert ts.tp_latched(fresh) is True
    assert sm.tp_armed(fresh, False, 1900.0, store=None, dry_run=True) is True
    assert sm._tp_latch_tag(fresh) == ' [TP-LATCHED]'


def test_a_stale_latch_does_not_report_a_multi_day_touch_to_fill_gap():
    """The latency measurement exists to price the 5-minute lag. A record that
    books the morning after an expired touch would report a 17-hour "give
    back" and make the distribution unreadable."""
    stale = dict(_stamped(THU), tp_spot=1931.91)
    assert ts.tp_touch_to_fill(stale, 1900.0, rising=True, now=FRI) == {}
    live = dict(_stamped(FRI), tp_spot=1931.91)
    out = ts.tp_touch_to_fill(live, 1900.0, rising=True,
                              now=FRI + timedelta(seconds=300))
    assert out['tp_touch_to_exit_sec'] == pytest.approx(300.0, abs=1)


# ── Scope, unchanged ────────────────────────────────────────────────────────

def test_the_expiry_did_not_grow_a_latch_on_any_stop(store, monkeypatch):
    """M13 pinned the scope: only the take-profit latches. Restated here
    because the expiry work touches the same block, and a stop that acquired a
    latch on the way through would be the measured spot-stop finding inverted.
    """
    clock = _FrozenClock(monkeypatch, FRI, ts)
    monkeypatch.setattr(cfg, 'SPOT_SL_ENABLED', True)
    with store._mutate():
        store.find(1)['tp_spot'] = 500.0

    _cycle(store, monkeypatch, spot=85.0, gate=False)       # through spot SL
    _cycle(store, monkeypatch, spot=95.0, mid=4.0, gate=False)  # under debit SL
    clock.advance(minutes=5)
    _cycle(store, monkeypatch, spot=95.0, mid=30.0, gate=True)  # recovered
    t = store.find(1)
    assert t['status'] == 'entered', 'a recovered stop still fired'
    assert not any(k.startswith('sl_touched') or k.startswith('spot_sl_touched')
                   for k in t), 'a stop grew a latch'

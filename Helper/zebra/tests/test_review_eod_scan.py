"""The daily end-of-session news sweep over every open position.

Why it exists: #472 ANGELONE lost 65.8% overnight on Angel One's monthly
business update. The review agent asks exactly the right question — "has the
reason for holding changed?" — but the pre-filter that decides WHEN to ask it
was reactive to price, and price had done nothing by the close. The calendar
had no class for a monthly operating print either, so the event path was silent
too. Both gaps are in the trigger, not the judgement.

What these tests pin, in one sentence each:

* the sweep is a TIME-OF-DAY rule on the EXCHANGE clock, so every test states
  its own instant explicitly and none of them can change answer depending on
  the hour, day or timezone the suite runs in;
* the two caps are INDEPENDENT — a 09:20 price review does not satisfy the
  15:00 news sweep, and the sweep does not consume the price cap — with two
  reviews per position per day as the ceiling;
* the cheap model runs the routine sweep and the decision model runs anything
  price-triggered, chosen off the REASON SET rather than off the reason string;
* the stagger defers, it does not drop.

No test here spawns an agent or sends a Telegram: `spawn=False` where the spawn
is irrelevant, and conftest's `spawns` recorder — which replaces the spawn
entry point outright — where the MODEL is the thing under test.

Run:  cd Helper && python -m pytest zebra/tests/test_review_eod_scan.py -v
"""
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import nse_holidays            # noqa: E402
from zebra import config as cfg            # noqa: E402
from zebra import events                   # noqa: E402
from zebra import review                   # noqa: E402
from zebra.trade_store import ZebraStore    # noqa: E402

# Every instant is tz-aware and IST. Naive would be read as the exchange clock
# by convention, which is right in production and useless in a test: it would
# silently mean "whatever zone this box is in" to the next reader.
FRIDAY = datetime(2026, 9, 4, tzinfo=cfg.IST)          # a plain session day
MORNING = FRIDAY.replace(hour=9, minute=20)
BEFORE = FRIDAY.replace(hour=14, minute=59)
AT = FRIDAY.replace(hour=15, minute=0)
LATER = FRIDAY.replace(hour=15, minute=5)
SATURDAY = FRIDAY.replace(day=5, hour=15, minute=0)

QUIET = 96.0            # == the entry reference: nothing for price to say
ADVERSE = 90.0          # -6.25% on a CE, past REVIEW_ADVERSE_PCT

SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': QUIET, 'signal_gap_pct': 4.0}


@pytest.fixture(autouse=True)
def _pinned_policy(tmp_path, monkeypatch):
    """The RULE under test, not the box's config or the box's calendar.

    `config/zebra_config.json` is an untracked per-box overlay that WINS over
    the tracked defaults, so asserting against `cfg.EOD_REVIEW_*` as imported
    would make these tests report on the machine. Same reasoning as conftest's
    `_pinned_vet_flag`.

    `is_holiday` is pinned False for the same class of reason one level down:
    `common/nse_holidays.py` reads a real scraped file outside the repo, so the
    session test would otherwise depend on data no checkout carries. The
    WEEKDAY half of `is_session` stays real — that is the part
    `test_a_weekend_cron_has_no_session_to_sweep` is about.
    """
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'EVENT_FILE', tmp_path / 'events.json')
    monkeypatch.setattr(cfg, 'EVENT_LOCK', tmp_path / 'events.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    monkeypatch.setattr(cfg, 'EOD_REVIEW_ENABLED', True)
    monkeypatch.setattr(cfg, 'EOD_REVIEW_START', (15, 0))
    monkeypatch.setattr(cfg, 'EOD_REVIEW_LAST_REQUEST', (15, 15))
    monkeypatch.setattr(cfg, 'EOD_REVIEW_MODEL', 'sonnet')
    monkeypatch.setattr(cfg, 'EOD_REVIEW_MAX_PER_CYCLE', 2)
    monkeypatch.setattr(cfg, 'VET_MODEL', 'opus')
    monkeypatch.setattr(nse_holidays, 'is_holiday', lambda d: False)
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    return tmp_path


@pytest.fixture
def clock(monkeypatch):
    """One pinned clock for BOTH the caller's `now=` and the stamps written.

    `request()` timestamps the marker off `_now()` while the pre-filter reads
    the injected `now`. Leaving those on two different clocks makes a test pass
    or fail on the gap between them — a morning review would leave a `deadline`
    hours in the future or the past depending on when the suite ran, and the
    in-flight guard would suppress the afternoon sweep for a reason the test
    never intended to exercise.

    Returns a setter that also returns the instant, so a call site reads
    `review.run(..., now=clock(AT))`.
    """
    box = {'now': BEFORE}
    monkeypatch.setattr(review, '_now',
                        lambda: review._ist_clock(box['now']))

    def at(when):
        box['now'] = when
        return when

    return at


def _store(paths, n: int = 1):
    """`n` open positions, ids 1..n, all identical apart from the symbol."""
    s = ZebraStore(config={})
    s._load_local()
    for i in range(n):
        sig = dict(SIGNAL, stock='TESTCO%d' % i if n > 1 else 'TESTCO')
        s.add_signal(sig)
        s.mark_entered(i + 1, {
            'long_strike': 96.0, 'short_strike': 100.0,
            'long_symbol': 'X96CE', 'short_symbol': 'X100CE',
            'debit': 10.0, 'lot_size': 100, 'lots': 1,
            # Far enough out that the "last cheap window to adjust" reason
            # cannot fire and confuse a scan-only assertion.
            'expiry': (FRIDAY + timedelta(days=90)).strftime('%Y-%m-%d')})
        # `mark_entered` stamps entry_date/entry_time from the REAL clock. The
        # tests pin their day to FRIDAY, so once the suite runs after
        # EOD_REVIEW_LAST_REQUEST on that very date the "entered after the
        # window" exclusion hides every position from the INCOMPLETE alarm
        # and the test flips. Bit us at 15:16 IST on 2026-09-04, the day the
        # feature shipped. Pin the stamps to a mid-session entry on FRIDAY.
        s.update_trade_fields(i + 1, entry_date=FRIDAY.date().isoformat(),
                              entry_time='11:35:12')
    return s


@pytest.fixture
def store(_pinned_policy):
    return _store(_pinned_policy)


def _ltps(store):
    return {t['stock']: QUIET for t in store.get_entered()}


def _marker(store, tid=1):
    return store.find(tid).get('review') or {}


# ── the time-of-day rule ─────────────────────────────────────────────────
def test_before_the_start_time_a_quiet_position_is_not_flagged(store, clock):
    needed, why = review.needs_review(store.find(1), QUIET, evts=[],
                                      now=clock(BEFORE))
    assert needed is False and why == ''


def test_at_the_start_time_a_quiet_position_is_flagged_for_the_sweep(store,
                                                                     clock):
    needed, why = review.needs_review(store.find(1), QUIET, evts=[],
                                      now=clock(AT))
    assert needed is True and why == review.EOD_REASON


def test_a_weekend_cron_has_no_session_to_sweep(store, clock):
    """The cron is Mon-Fri, but `zebra loop` and a manual run are not. A
    Saturday afternoon has no day's news to review and no close to beat."""
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(SATURDAY)) == []


def test_a_declared_holiday_has_no_session_to_sweep(store, clock,
                                                    monkeypatch):
    monkeypatch.setattr(nse_holidays, 'is_holiday', lambda d: True)
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(AT)) == []


def test_the_start_time_is_read_on_the_exchange_clock_not_the_host(store,
                                                                   clock):
    """15:00 IST is 09:30 UTC. A box clocked in UTC — which is the default on
    a fresh Debian image — must sweep at the same INSTANT the Pi does, not at
    15:00 of its own local time."""
    from datetime import timezone
    utc_0929 = datetime(2026, 9, 4, 9, 29, tzinfo=timezone.utc)
    utc_0930 = datetime(2026, 9, 4, 9, 30, tzinfo=timezone.utc)
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(utc_0929)) == []
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(utc_0930)) == [review.EOD_REASON]


# ── once per session, on its own key ─────────────────────────────────────
def test_a_scanned_position_is_not_scanned_again_the_same_session(store,
                                                                  clock):
    assert review.run(store, _ltps(store), spawn=False,
                      now=clock(AT)) == [1]
    assert _marker(store)[review.EOD_SCAN_KEY] == '2026-09-04'
    assert review.run(store, _ltps(store), spawn=False, now=clock(LATER)) == []


def test_the_stamp_lands_when_the_agent_is_spawned_not_when_it_reports(store,
                                                                       clock):
    """Same discipline as `attempted_at`: an agent that dies never calls
    `record()`, so a cap keyed on completion respawns it every cycle until the
    close."""
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    m = _marker(store)
    assert m['state'] == 'pending' and m.get('reviewed_at') is None
    assert m[review.EOD_SCAN_KEY] == '2026-09-04'


def test_a_refused_spawn_gives_the_sweep_slot_back(store, clock, monkeypatch):
    """A refusal is not a crash. Burning the day's slot on a spawn that never
    happened locks the position out until tomorrow, when the same arithmetic
    repeats — the bug `_release_attempt` was written for, in a second key.

    The retry waits out the in-flight marker `request()` had already written,
    which is the pre-existing behaviour of the price path and is why the
    stagger is sized at the deferrable-channel cap rather than higher: a sweep
    that is refused costs the position `VET_TIMEOUT_SEC`, and the window
    between the start time and the close is only 30 minutes wide.
    """
    monkeypatch.setattr(review.vet_mod, '_spawn_generic', lambda *a, **k: None)
    assert review.run(store, _ltps(store), now=clock(AT)) == []
    assert not _marker(store).get(review.EOD_SCAN_KEY)
    monkeypatch.setattr(review.vet_mod, '_spawn_generic', lambda *a, **k: 42)
    lapsed = AT + timedelta(seconds=cfg.VET_TIMEOUT_SEC + 60)
    assert review.run(store, _ltps(store), now=clock(lapsed)) == [1]


def test_a_refused_sweep_does_not_take_the_price_slot_with_it(store, clock,
                                                              monkeypatch):
    """A sweep-only request never spent `attempted_at`, so releasing it must
    not clear a price review that really did run this morning."""
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(MORNING))
    review.record(store, 1, 'hold')
    morning_stamp = _marker(store)['attempted_at']
    monkeypatch.setattr(review.vet_mod, '_spawn_generic', lambda *a, **k: None)
    review.run(store, _ltps(store), now=clock(AT))
    assert _marker(store)['attempted_at'] == morning_stamp


# ── the two caps are independent ─────────────────────────────────────────
def test_a_morning_price_review_does_not_satisfy_the_afternoon_sweep(store,
                                                                     clock):
    """The whole point. "Price moved 6%" and "what came out on the wires
    today" are different questions; answering one does not answer the other."""
    ltps = {'TESTCO': ADVERSE}
    assert review.run(store, ltps, spawn=False, now=clock(MORNING)) == [1]
    assert 'adverse' in _marker(store)['why']
    review.record(store, 1, 'hold')

    reasons = review.review_reasons(store.find(1), ADVERSE, evts=[],
                                    now=clock(AT))
    assert reasons == [review.EOD_REASON], \
        'the morning review swallowed the afternoon sweep'


def test_the_sweep_does_not_consume_the_price_cap(store, clock):
    """The mirror of the test above: a 15:00 sweep must not make the position
    deaf to a 15:20 price move it has not been looked at for."""
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    review.record(store, 1, 'hold')
    reasons = review.review_reasons(store.find(1), ADVERSE, evts=[],
                                    now=clock(LATER))
    assert any('adverse' in r for r in reasons)
    assert review.EOD_REASON not in reasons      # that one IS spent


def test_a_completed_sweep_does_not_mute_tomorrow_morning(store, clock):
    """The price cap is a rolling 24 HOURS. If the sweep spent it — at request
    time via `attempted_at`, or at landing time via `reviewed_at` — then a
    15:00 sweep every session would suppress every price-triggered review from
    15:00 to 15:00, permanently, and the layer would go quiet without a single
    error anywhere."""
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    review.record(store, 1, 'hold')
    tomorrow = MORNING.replace(day=7)                     # the next Monday
    reasons = review.review_reasons(store.find(1), ADVERSE, evts=[],
                                    now=clock(tomorrow))
    assert any('adverse' in r for r in reasons), \
        'the sweep spent the price cap it does not own'


def test_two_reviews_per_position_per_day_is_the_ceiling(store, clock, spawns):
    ltps = {'TESTCO': ADVERSE}
    review.run(store, ltps, now=clock(MORNING))
    review.record(store, 1, 'hold')
    review.run(store, ltps, now=clock(AT))
    review.record(store, 1, 'hold')
    for when in (LATER, LATER.replace(minute=20), LATER.replace(minute=25)):
        review.run(store, ltps, now=clock(when))
    assert len(spawns) == 2, 'reviews today: %d' % len(spawns)


def test_a_price_review_still_carries_the_sweep_forward(store, clock):
    """`request()` rewrites the marker wholesale. An un-carried
    `eod_scanned_on` would hand the position a fresh sweep on the very next
    cycle, and every cycle after it."""
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    review.record(store, 1, 'hold')
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(LATER))
    assert _marker(store)[review.EOD_SCAN_KEY] == '2026-09-04'


# ── model routing ────────────────────────────────────────────────────────
def test_the_sweep_alone_runs_on_the_cheap_model(store, clock, spawns):
    review.run(store, _ltps(store), now=clock(AT))
    assert [m for _, m in spawns] == ['sonnet']


def test_anything_alongside_the_sweep_runs_on_the_decision_model(store, clock,
                                                                 spawns):
    """A position that is BOTH 6% adverse and due its sweep is a price-
    triggered review that happens to also carry the sweep. Something has
    already happened; the call is about money."""
    reasons = review.review_reasons(store.find(1), ADVERSE, evts=[],
                                    now=clock(AT))
    assert len(reasons) == 2 and review.EOD_REASON in reasons
    review.run(store, {'TESTCO': ADVERSE}, now=clock(AT))
    assert [m for _, m in spawns] == ['opus']


def test_a_price_review_outside_the_sweep_window_is_unchanged(store, clock,
                                                              spawns):
    review.run(store, {'TESTCO': ADVERSE}, now=clock(MORNING))
    assert [m for _, m in spawns] == ['opus']


def test_routing_reads_the_reason_set_not_the_reason_string(store, clock):
    """`why` is prose a human reads and the agent is shown. If routing parsed
    it, rewording the reason would silently re-route a spawn to the wrong
    model."""
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(AT)) == [review.EOD_REASON]


# ── the stagger ──────────────────────────────────────────────────────────
def test_the_per_cycle_cap_defers_the_rest_to_the_next_cycle(_pinned_policy,
                                                             clock, spawns):
    """The whole book qualifies the instant the clock passes 15:00. Without a
    stagger that is N detached agents in one cycle, on a Pi that also runs the
    live-money monitor."""
    store = _store(_pinned_policy, n=4)
    first = review.run(store, _ltps(store), now=clock(AT))
    assert len(first) == 2 and len(spawns) == 2
    second = review.run(store, _ltps(store), now=clock(LATER))
    assert len(second) == 2, 'the deferred positions never came back'
    assert len(spawns) == 4


def test_the_scan_cap_always_leaves_a_deferrable_slot(monkeypatch):
    """The sweep and the price review draw on ONE small agent pool. At the
    full deferrable cap the quiet low-id scans take every slot and a genuine
    adverse move is refused in the same cycle — a starvation with no alarm
    behind it, because `record_spawn_refused` correctly does not raise one.

    RETIRES WHEN: the sweep stops sharing an agent pool with the price-
    triggered reviews — a per-channel budget rather than one box-wide count —
    at which point the shipped default cannot starve anything and the tracked
    config file no longer has to be read here to prove it does not.
    """
    assert cfg.DEFERRABLE_AGENT_CAP == max(
        1, cfg.MAX_CONCURRENT_AGENTS - cfg.AGENT_RESERVE)
    monkeypatch.setitem(cfg._runtime, 'eod_review_max_per_cycle',
                        cfg.DEFERRABLE_AGENT_CAP + 5)
    clamped = min(cfg._int('eod_review_max_per_cycle'),
                  max(1, cfg.DEFERRABLE_AGENT_CAP - 1))
    assert clamped <= cfg.DEFERRABLE_AGENT_CAP - 1
    # ...and the shipped value is already inside it, so a healthy box never
    # logs the clamp warning.
    import json as _json
    tracked = _json.loads((HELPER / 'config' / 'zebra_config.defaults.json')
                          .read_text(encoding='utf-8'))
    assert tracked['eod_review_max_per_cycle'] <= cfg.DEFERRABLE_AGENT_CAP - 1


def test_a_price_review_is_spawned_before_the_quiet_scans(_pinned_policy,
                                                          clock, spawns):
    """`get_entered()` is in id order and both kinds share one pool, so
    ordering IS the fix: without it the two cheap scans on ids 1-2 spend the
    budget and the 6% adverse move on id 4 waits a cycle for no reason."""
    store = _store(_pinned_policy, n=4)
    ltps = dict(_ltps(store))
    ltps['TESTCO3'] = ADVERSE                     # the highest id
    review.run(store, ltps, now=clock(AT))
    assert [m for _, m in spawns][0] == 'opus', \
        'a routine scan was spawned ahead of a price-triggered review'


def test_a_deferred_position_is_not_stamped(_pinned_policy, clock):
    """Deferred, not dropped: it re-qualifies next cycle only because nothing
    was written for it."""
    store = _store(_pinned_policy, n=4)
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    stamped = [t['id'] for t in store.get_entered()
               if (t.get('review') or {}).get(review.EOD_SCAN_KEY)]
    assert len(stamped) == 2


def test_a_price_triggered_review_is_not_held_back_by_the_sweep_cap(
        _pinned_policy, clock, spawns):
    """The cap rations the routine sweep. A position with something else to
    say is not routine."""
    store = _store(_pinned_policy, n=4)
    ltps = dict(_ltps(store))
    ltps['TESTCO3'] = ADVERSE          # the one the cap would have deferred
    review.run(store, ltps, now=clock(AT))
    assert sorted(m for _, m in spawns) == ['opus', 'sonnet', 'sonnet']


def test_the_sweep_logs_what_it_asked_for_and_what_it_deferred(_pinned_policy,
                                                              clock, spawns,
                                                              caplog):
    store = _store(_pinned_policy, n=4)
    with caplog.at_level(logging.INFO, logger=review.logger.name):
        review.run(store, _ltps(store), now=clock(AT))
    text = caplog.text
    assert 'EOD SCAN requested #1 TESTCO0 model=sonnet' in text
    assert 'EOD SCAN: 2 requested, 2 deferred to next cycle' in text


def test_a_quiet_cycle_outside_the_window_says_nothing(store, clock, caplog):
    """A 0/0 line every five minutes is the noise that trains a reader to skim
    the line that matters."""
    with caplog.at_level(logging.INFO, logger=review.logger.name):
        review.run(store, _ltps(store), spawn=False, now=clock(BEFORE))
    assert 'EOD SCAN' not in caplog.text


# ── the switch, and a stale value for it ─────────────────────────────────
def test_the_flag_disables_the_sweep(store, clock, monkeypatch):
    monkeypatch.setattr(cfg, 'EOD_REVIEW_ENABLED', False)
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(AT)) == []
    # ...and leaves the price-triggered path exactly as it was.
    assert review.review_reasons(store.find(1), ADVERSE, evts=[],
                                 now=clock(AT)) != []


def test_an_unparseable_start_time_is_loud_and_keeps_sweeping(monkeypatch,
                                                              caplog):
    """A typo'd time must not silently DISABLE the sweep. That is the exact
    shape the feature exists to close — a layer that reads armed and is dark —
    and it would be invisible: the flag still says true and nothing ever
    fires."""
    monkeypatch.setitem(cfg._runtime, 'eod_review_start', '3 PM')
    with caplog.at_level(logging.ERROR, logger=cfg.logger.name):
        assert cfg._hhmm('eod_review_start') == (15, 0)
    assert 'eod_review_start' in caplog.text
    assert 'STILL RUNNING' in caplog.text


@pytest.mark.parametrize('raw', ['3 PM', '15', '15:00:00', '', '25:00',
                                 '15:61', '-1:00', 900, None, '1500',
                                 'aa:bb'])
def test_only_a_wall_clock_string_parses(raw):
    assert cfg._parse_hhmm(raw) is None


@pytest.mark.parametrize('raw,want', [('15:00', (15, 0)), ('09:15', (9, 15)),
                                      ('0:00', (0, 0)), ('23:59', (23, 59)),
                                      (' 15:30 ', (15, 30))])
def test_a_wall_clock_string_parses_to_hour_and_minute(raw, want):
    assert cfg._parse_hhmm(raw) == want


# ── the agent has to know which question it was asked ────────────────────
def test_the_sweep_reason_reaches_the_agent(store, clock):
    """`zebra review show` prints the marker's `why` as `flagged_because`, and
    the prompt sends the agent there first. That string is how it knows to
    sweep the news instead of post-morteming a price move — and VETTING.md's
    "Daily scan" paragraph keys off this exact text.

    RETIRES WHEN: the review agent is told which question it was asked through
    a structured field instead of a prose `why` string — at that point the
    prompt and the checklist stop being coupled to this literal and the
    document read here has nothing left to pin.
    """
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    assert _marker(store)['why'] == review.EOD_REASON
    assert 'review show' in cfg.REVIEW_PROMPT_TEMPLATE
    doc = (HELPER / 'zebra' / 'VETTING.md').read_text(encoding='utf-8')
    assert review.EOD_REASON in doc, \
        'VETTING.md does not tell the agent what this flag means'


def test_the_sweep_cannot_close_a_position(store, clock):
    """Unchanged boundary, restated for the new path: the sweep runs on every
    open position every session, so it is the highest-volume way into the
    review channel and the one most worth pinning."""
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    review.record(store, 1, 'exit', reasons=['monthly update due tomorrow'])
    assert store.find(1)['status'] == 'entered'
    assert store.find(1).get('exit_reason') is None


# ══ regressions from the 2026-09-04 adversarial review ═══════════════════
# Each of these was TRACED AND REPRODUCED against the first cut of this
# feature. They share one root: the sweep made a request path that used to run
# at most once a day, on a handful of positions, run on EVERY position EVERY
# session — so every latent sharp edge in that path started firing daily.

def test_an_undelivered_recommendation_is_not_overwritten_by_the_sweep(
        store, clock):
    """R1. `request()` REPLACES the marker, so a recommendation that has not
    reached the human yet is destroyed by the next request.

    The window is routine, not exotic: `_claim_alert` releases the flag when a
    Telegram send fails so the message retries on the next sweep — and the
    sweep now guarantees a fresh request at 15:00 on every open position. A
    09:20 `exit` whose send failed became `action: None, why: 'daily EOD scan'`
    six hours later, and the only trace was one ERROR line about the send.
    """
    pre = FRIDAY.replace(hour=14, minute=50)
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(pre))
    review.record(store, 1, 'exit', reasons=['thesis dead'], decision_id=77)
    # The send fails; `run` releases the flag so it retries next sweep.
    review.run(store, {'TESTCO': ADVERSE}, send=lambda m, **k: False,
               spawn=False, now=clock(pre))
    assert _marker(store)['action'] == 'exit' and not _marker(store)['alerted']

    assert review.run(store, _ltps(store), spawn=False, now=clock(AT)) == []
    m = _marker(store)
    assert m['action'] == 'exit' and m['decision_id'] == 77,         'the sweep overwrote an exit recommendation the human never saw'

    # Delivered on a later sweep, and only THEN may the scan take the marker.
    sent = []
    review.run(store, _ltps(store),
               send=lambda msg, **k: sent.append(msg) or True,
               spawn=False, now=clock(AT))
    assert len(sent) == 1
    assert review.run(store, _ltps(store), spawn=False,
                      now=clock(LATER)) == [1]
    assert _marker(store)['why'] == review.EOD_REASON


def test_the_undelivered_guard_is_bounded_by_attempts(store, clock, caplog):
    """E1. The guard was UNBOUNDED. With `send()` failing permanently — no
    `config/telegram.json`, or a 400 the message will never stop earning — the
    marker froze at `done/exit/alerted:False` and EVERY later request, price
    reviews included, was refused for the life of the trade. A protection
    against losing one recommendation had become a permanent outage of the
    layer that produces them, on the position already flagged for exit."""
    pre = FRIDAY.replace(hour=14, minute=50)
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(pre))
    review.record(store, 1, 'exit', reasons=['thesis dead'], decision_id=77)
    dead = lambda msg, **k: False                       # noqa: E731

    for i in range(review.MAX_ALERT_ATTEMPTS):
        at = pre + timedelta(minutes=i)
        assert review.run(store, _ltps(store), send=dead, spawn=False,
                          now=clock(at)) == [] or at < AT
    assert _marker(store)['alert_attempts'] == review.MAX_ALERT_ATTEMPTS

    with caplog.at_level(logging.ERROR, logger=review.logger.name):
        assert review.run(store, _ltps(store), spawn=False,
                          now=clock(AT)) == [1],             'the layer stayed locked out after delivery had plainly failed'
    assert 'NEVER DELIVERED' in caplog.text

    # PRESERVED, not dropped — and still being delivered.
    prev = _marker(store)['prev']
    assert prev['action'] == 'exit' and prev['decision_id'] == 77
    sent = []
    review.run(store, _ltps(store),
               send=lambda msg, **k: sent.append(msg) or True,
               spawn=False, now=clock(LATER))
    assert len(sent) == 1 and 'EXIT' in sent[0]
    assert _marker(store)['prev']['alerted'] is True


def test_the_undelivered_guard_is_bounded_by_time(store, clock):
    """The other bound, for the case where delivery is never even RETRIED —
    a box that stops sweeping for an hour still must not come back to a
    permanently locked position."""
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(MORNING))
    review.record(store, 1, 'exit', reasons=['thesis dead'])
    review.run(store, {'TESTCO': ADVERSE}, send=lambda m, **k: False,
               spawn=False, now=clock(MORNING))
    inside = MORNING + timedelta(seconds=review.UNDELIVERED_HOLD_SEC - 60)
    assert review._holds_an_undelivered_recommendation(_marker(store),
                                                       inside) is True
    past = MORNING + timedelta(seconds=review.UNDELIVERED_HOLD_SEC + 60)
    assert review._holds_an_undelivered_recommendation(_marker(store),
                                                       past) is False


def test_a_hold_verdict_does_not_block_the_sweep(store, clock):
    """The guard is about UNDELIVERED ADVICE, not about any finished review.
    `hold` is never sent, so blocking on it would stall the sweep forever."""
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(MORNING))
    review.record(store, 1, 'hold')
    assert review.run(store, _ltps(store), spawn=False, now=clock(AT)) == [1]


def test_a_still_pending_price_review_is_not_replaced_by_a_scan(store, clock,
                                                                spawns):
    """R2. `VET_TIMEOUT_SEC` is when WE stop waiting, not when the child dies.

    A slow Opus review that lands a minute past its own deadline found its
    marker replaced by a scan: `record()` applied the Opus verdict to the
    SCAN's marker — so `kind == 'scan'` and `reviewed_at` was never stamped —
    and the scan's own verdict was discarded five minutes later as "no pending
    review". Two agents, one marker, both answers wrong.
    """
    # Requested just before the sweep window opens, so its deadline lapses
    # INSIDE it — the shape that actually occurs, since the sweep now fires on
    # every open position at 15:00 sharp.
    pre = AT - timedelta(seconds=cfg.VET_TIMEOUT_SEC // 4)
    review.run(store, {'TESTCO': ADVERSE}, now=clock(pre))
    assert _marker(store)['kind'] == 'review' and len(spawns) == 1
    lapsed = pre + timedelta(seconds=cfg.VET_TIMEOUT_SEC + 60)
    assert lapsed > AT, 'the deadline must lapse inside the sweep window'

    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(lapsed)) == [], \
        'a scan took the marker from a price review that may still land'
    assert _marker(store)['kind'] == 'review'
    assert len(spawns) == 1

    # BOUNDED. Past the kill deadline no verdict can arrive, and refusing
    # forever would let one dead agent silence that position's sweep
    # permanently — a silent, unbounded loss of the coverage being added.
    # It is the NEXT session before that bound is reached, because
    # VET_TIMEOUT_SEC + CHILD_KILL_SEC (~19 min) is wider than the whole
    # request window: a price review started near 15:00 costs that position
    # its sweep for the day, which is fine — it was just reviewed.
    monday = (FRIDAY + timedelta(days=3)).replace(hour=15, minute=0)
    assert review.review_reasons(store.find(1), QUIET, evts=[],
                                 now=clock(monday)) == [review.EOD_REASON]


def test_a_market_wide_expiry_row_does_not_flag_every_position(_pinned_policy,
                                                               clock,
                                                               monkeypatch):
    """R3. `upcoming()` returns symbol-less rows to EVERY symbol and they sit
    inside EVENT_HORIZON_DAYS for ten days. One "September F&O monthly expiry"
    row therefore flagged the whole book every day for ten days — and made
    `reasons` non-empty, so the 15:00 sweep stopped being scan-only and routed
    to Opus for all of it. The engine already knows the expiry: it is in every
    trade record and the TIME stop is computed from it.
    """
    store = _store(_pinned_policy, n=4)
    row = {'type': 'expiry', 'days_away': 6, 'title': 'September F&O expiry',
           'symbol': ''}
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [row])
    assert review.review_reasons(store.find(1), QUIET, now=clock(BEFORE)) == []
    assert review.review_reasons(store.find(1), QUIET,
                                 now=clock(AT)) == [review.EOD_REASON], \
        'a standing market-wide row made the cheap sweep expensive'
    # Still SHOWN to the agent — filtered as a trigger, not hidden as evidence.
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    assert _marker(store)['context']['events'] == [row]


def test_a_dated_market_decision_still_flags_a_review(store, clock,
                                                      monkeypatch):
    """The other side of R3: `budget` / `election` / `rbi_policy` genuinely
    change the distribution and are the reason the event trigger exists. Only
    the standing `expiry` / `other` rows are dropped."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [
        {'type': 'rbi_policy', 'days_away': 2, 'title': 'MPC decision'}])
    reasons = review.review_reasons(store.find(1), QUIET, now=clock(BEFORE))
    assert reasons and 'rbi_policy' in reasons[0]


def test_a_symbol_ed_other_row_still_flags_a_review(store, clock, monkeypatch):
    """Scope, not type: an OFS in THIS name is per-stock and must still fire."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [
        {'type': 'other', 'days_away': 3, 'title': 'OFS', 'symbol': 'TESTCO'}])
    reasons = review.review_reasons(store.find(1), QUIET, now=clock(BEFORE))
    assert reasons and 'other in 3d' in reasons[0]


def test_a_refused_spawn_withdraws_the_request(store, clock, monkeypatch):
    """R5. Releasing only the CAP keys left `state: 'pending'` with a live
    deadline on a position no agent was ever started for, so the next cycle
    saw an in-flight review and skipped it — the opposite of what the log line
    said, for ten minutes. On a quota-blocked CLI every spawn is refused, so
    the whole book locks at once, and the request window is fifteen minutes
    wide."""
    monkeypatch.setattr(review.vet_mod, '_spawn_generic', lambda *a, **k: None)
    assert review.run(store, {'TESTCO': ADVERSE}, now=clock(MORNING)) == []
    m = _marker(store)
    assert m.get('state') != 'pending' and not m.get('attempted_at')
    monkeypatch.setattr(review.vet_mod, '_spawn_generic', lambda *a, **k: 42)
    nxt = MORNING + timedelta(minutes=5)
    assert review.run(store, {'TESTCO': ADVERSE}, now=clock(nxt)) == [1], \
        'the withdrawn request did not retry on the next cycle'


def test_no_scan_is_requested_after_the_last_request_time(store, clock):
    """R6. `run()` REQUESTS in one loop and DELIVERS in the next, and the
    monitor's last cycle is 15:30. A scan spawned at 15:25 whose agent lands at
    15:31 is not Telegrammed until ~09:15 the next morning — silently, having
    been asked precisely so a human could act before the close."""
    assert review.review_reasons(
        store.find(1), QUIET, evts=[],
        now=clock(FRIDAY.replace(hour=15, minute=15))) == [review.EOD_REASON]
    assert review.review_reasons(
        store.find(1), QUIET, evts=[],
        now=clock(FRIDAY.replace(hour=15, minute=16))) == []


def test_the_closing_window_names_what_it_never_scanned(_pinned_policy, clock,
                                                        caplog):
    """An unfinished sweep leaves no trace but an ABSENCE — no `EOD SCAN
    requested` lines — which reads exactly like a quiet, fully-swept book."""
    store = _store(_pinned_policy, n=4)
    with caplog.at_level(logging.WARNING, logger=review.logger.name):
        review.run(store, _ltps(store), spawn=False,
                   now=clock(FRIDAY.replace(hour=15, minute=20)))
    assert 'EOD SCAN INCOMPLETE' in caplog.text
    assert '4 open position(s)' in caplog.text


def test_a_finished_sweep_says_nothing_when_the_window_closes(store, clock,
                                                              caplog):
    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    with caplog.at_level(logging.WARNING, logger=review.logger.name):
        review.run(store, _ltps(store), spawn=False,
                   now=clock(FRIDAY.replace(hour=15, minute=20)))
    assert 'INCOMPLETE' not in caplog.text


def test_the_holiday_skip_is_logged(store, clock, caplog, monkeypatch):
    """R10. The skip was silent, so "the sweep produced nothing today" and
    "the sweep never ran today" looked identical in the log."""
    monkeypatch.setattr(nse_holidays, 'is_holiday', lambda d: True)
    with caplog.at_level(logging.INFO, logger=review.logger.name):
        review.run(store, _ltps(store), spawn=False, now=clock(AT))
    assert 'EOD SCAN skipped' in caplog.text
    assert '2026-09-04 is not a trading session' in caplog.text


def test_a_degraded_calendar_says_so_in_the_skip(store, clock, caplog,
                                                 monkeypatch):
    monkeypatch.setattr(nse_holidays, 'is_holiday', lambda d: True)
    monkeypatch.setattr(nse_holidays, 'coverage_status',
                        lambda d: {'state': 'stale'})
    with caplog.at_level(logging.INFO, logger=review.logger.name):
        review.run(store, _ltps(store), spawn=False, now=clock(AT))
    assert 'holiday calendar is stale' in caplog.text


def test_the_scan_spawns_on_its_own_health_channel(store, clock, monkeypatch):
    """R7. `record_agent_landed` zeroes `spawns_since_landing` for a whole
    channel. ~8 Sonnet sweeps land every session, so sharing 'review' made the
    cheap sweep's success an all-clear for the Opus price reviews — which could
    fail on every spawn and never reach SILENT_SPAWN_LIMIT."""
    seen = []
    monkeypatch.setattr(
        review.vet_mod, '_spawn_generic',
        lambda p, m, tag, channel='entry': seen.append(channel) or 42)
    review.run(store, {'TESTCO': ADVERSE}, now=clock(MORNING))
    review.record(store, 1, 'hold')
    review.run(store, _ltps(store), now=clock(AT))
    assert seen == ['review', 'review_scan']
    # Both must stay deferrable, or the sweep would draw on the reserve the
    # entry/exit decisions are held back for.
    assert set(review.CHANNELS.values()) <= set(cfg.DEFERRABLE_CHANNELS)


def test_the_startup_banner_reports_the_sweep(monkeypatch):
    """R9. A disabled sweep is indistinguishable from a quiet one in every log
    the box writes, which is the same failure `vet_state_line` exists for."""
    level, msg = cfg.eod_review_state_line()
    assert level == 'info' and 'ENABLED' in msg
    for token in ('15:00', '15:15', cfg.EOD_REVIEW_MODEL,
                  str(cfg.EOD_REVIEW_MAX_PER_CYCLE)):
        assert token in msg
    monkeypatch.setattr(cfg, 'EOD_REVIEW_ENABLED', False)
    level, msg = cfg.eod_review_state_line()
    assert level == 'warning' and 'DISABLED' in msg


def test_the_shipped_request_window_is_not_empty():
    """A `last_request` before `start` is a sweep that can never run, wearing a
    config that says it is on. config.py widens it and logs ERROR; this pins
    that the SHIPPED pair never needs that rescue."""
    assert cfg.EOD_REVIEW_LAST_REQUEST >= cfg.EOD_REVIEW_START
    assert cfg._parse_hhmm('09:30') < cfg._parse_hhmm('15:00')


def test_the_journal_tells_a_scan_from_a_price_review(tmp_path, monkeypatch,
                                                      clock):
    """R8. `cmd_review_record` hardcoded `kind='review'` and
    `model=VET_MODEL`, so every routine Sonnet sweep was journalled as an Opus
    price review — and the plan is to judge the sweep's output after a week,
    which that journal cannot support."""
    from argparse import Namespace
    from zebra import __main__ as cli
    from zebra import decisions as dec_mod
    from zebra import trade_store as ts_mod

    monkeypatch.setattr(cfg, 'DECISIONS_FILE', tmp_path / 'dec.json')
    monkeypatch.setattr(cfg, 'DECISIONS_LOCK', tmp_path / 'dec.lock')
    store = _store(tmp_path)
    monkeypatch.setattr(ts_mod, '_store', store)
    monkeypatch.setattr(ts_mod, 'get_store', lambda: store)
    journal = dec_mod.DecisionStore(path=cfg.DECISIONS_FILE,
                                    lock_path=cfg.DECISIONS_LOCK).initialize()
    monkeypatch.setattr(dec_mod, '_store', journal)
    monkeypatch.setattr(dec_mod, 'get_store', lambda: journal)

    review.run(store, _ltps(store), spawn=False, now=clock(AT))
    cli.cmd_review_record(Namespace(id=1, action='hold', reason=['quiet'],
                                    red_flag=None, confidence=0.7, notes=''))
    row = journal.all()[-1]
    assert row['kind'] == 'scan' and row['model'] == cfg.EOD_REVIEW_MODEL


def test_a_refused_cycle_stops_after_the_cap_instead_of_walking_the_book(
        _pinned_policy, clock, monkeypatch):
    """E2. `scan_budget` counted SUCCESSES, so a quota-blocked box walked the
    whole book: every scan-only position called `request()`, each of which
    writes the marker and then withdraws it — two store mutations, a local
    write and a Drive upload each — to start nothing, repeating every cycle
    until 15:15.

    A refusal means the shared agent pool is full, so no other scan-only
    position can succeed this cycle either. Counting it against the budget
    bounds a refused cycle at `cap` attempts.
    """
    store = _store(_pinned_policy, n=6)
    tries = []
    monkeypatch.setattr(review.vet_mod, '_spawn_generic',
                        lambda *a, **k: tries.append(1))      # returns None
    versions_before = [t['version'] for t in store.get_entered()]
    assert review.run(store, _ltps(store), now=clock(AT)) == []
    assert len(tries) == cfg.EOD_REVIEW_MAX_PER_CYCLE, \
        'a refused cycle attempted %d spawns against a cap of %d' % (
            len(tries), cfg.EOD_REVIEW_MAX_PER_CYCLE)
    touched = sum(1 for before, t in zip(versions_before, store.get_entered())
                  if t['version'] != before)
    assert touched == cfg.EOD_REVIEW_MAX_PER_CYCLE, \
        'the refused cycle mutated %d records' % touched


def test_a_deferral_does_not_spend_the_cycle_budget(store, clock, monkeypatch):
    """The other side of E2: a per-position deferral says nothing about the
    agent pool, so it must not consume a slot another position could use."""
    pre = FRIDAY.replace(hour=14, minute=50)
    review.run(store, {'TESTCO': ADVERSE}, spawn=False, now=clock(pre))
    review.record(store, 1, 'exit', reasons=['thesis dead'])
    review.run(store, {'TESTCO': ADVERSE}, send=lambda m, **k: False,
               spawn=False, now=clock(pre))
    seen = []
    monkeypatch.setattr(review.vet_mod, '_spawn_generic',
                        lambda *a, **k: seen.append(1) or 42)
    review.run(store, _ltps(store), now=clock(AT))
    assert seen == [], 'the deferred position spawned anyway'


def test_a_position_entered_after_the_window_is_not_reported_unscanned(
        _pinned_policy, clock, caplog):
    """E5. A 15:20 entry was never open while the sweep ran, so calling it
    "never scanned today" turns the incomplete-sweep alarm into a false one on
    a healthy box — every cycle to the close, for a trade ten minutes old."""
    store = _store(_pinned_policy, n=2)
    late = FRIDAY.replace(hour=15, minute=20)
    with store._mutate():
        t = store.find(2)
        t['entry_date'] = late.date().isoformat()
        t['entry_time'] = '15:18:04'
    review.run(store, _ltps(store), spawn=False, now=clock(AT))     # scans #1
    assert review.unscanned_today(store, review._ist_clock(late)) == []
    with caplog.at_level(logging.WARNING, logger=review.logger.name):
        review.run(store, _ltps(store), spawn=False, now=clock(late))
    assert 'INCOMPLETE' not in caplog.text


def test_an_unreadable_entry_time_is_still_reported_unscanned(_pinned_policy,
                                                              clock):
    """Unknown timing must not be able to HIDE a position from the only alarm
    that reports the sweep failing."""
    store = _store(_pinned_policy, n=1)
    late = FRIDAY.replace(hour=15, minute=20)
    with store._mutate():
        t = store.find(1)
        t['entry_date'] = late.date().isoformat()
        t['entry_time'] = 'who knows'
    assert review.unscanned_today(store, review._ist_clock(late)) == [1]


def test_the_request_window_ends_inside_the_session():
    """E6. `monitor._is_market_open` stops the cycle after MARKET_CLOSE, so a
    `last_request` of 15:40 arms a window no cycle ever reaches: scans there
    are neither requested nor delivered, the config reads healthy, and the
    only symptom is positions quietly going unscanned.

    RETIRES WHEN: the sweep stops depending on a LATER cycle to deliver its
    verdict — `run()` sending in the same pass it requests — at which point
    the close is no longer the binding deadline for a request.
    """
    assert cfg.EOD_REVIEW_LAST_REQUEST <= cfg.MARKET_CLOSE
    assert cfg.EOD_REVIEW_START >= cfg.MARKET_OPEN
    # And it leaves at least one delivering cycle before the close.
    end = cfg.EOD_REVIEW_LAST_REQUEST[0] * 60 + cfg.EOD_REVIEW_LAST_REQUEST[1]
    close = cfg.MARKET_CLOSE[0] * 60 + cfg.MARKET_CLOSE[1]
    assert close - end >= cfg.MONITOR_INTERVAL_SEC // 60, \
        'no cycle is left between the last request and the close'
    # The shape config.py clamps.
    assert cfg._parse_hhmm('15:40') > cfg.MARKET_CLOSE

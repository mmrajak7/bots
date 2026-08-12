"""Event calendar, position review, and the auth watchdog.

These three share one property worth pinning hard: **none of them may ever be
load-bearing.** A missing calendar, a failed review, a broken credential file —
each must degrade to "the bot behaves exactly as it did before this existed".
A safety feature that can halt trading is a liability.

The review tests additionally pin the boundary that makes the feature safe at
all: a review can recommend, and can never close a position.

Run:  cd Helper && python -m pytest zebra/tests/test_side_channels.py -v
"""
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg          # noqa: E402
from zebra import events                 # noqa: E402
from zebra import health                 # noqa: E402
from zebra import review                 # noqa: E402
from zebra.trade_store import ZebraStore  # noqa: E402

TODAY = datetime(2026, 8, 11)
SIGNAL = {'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
          'st_value': 100.0, 'st_direction': 'UP',
          'signal_price': 96.0, 'signal_gap_pct': 4.0}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'EVENT_FILE', tmp_path / 'events.json')
    monkeypatch.setattr(cfg, 'EVENT_LOCK', tmp_path / 'events.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', True)
    return tmp_path


@pytest.fixture
def store(paths):
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal(dict(SIGNAL))
    s.mark_entered(1, {'long_strike': 96.0, 'short_strike': 100.0,
                       'long_symbol': 'X96CE', 'short_symbol': 'X100CE',
                       'debit': 10.0, 'lot_size': 100, 'lots': 1,
                       'expiry': (TODAY + timedelta(days=30)
                                  ).strftime('%Y-%m-%d')})
    return s


# ── event calendar ───────────────────────────────────────────────────────
def test_missing_calendar_reads_as_empty_not_an_error(paths):
    assert events.load() == {'refreshed_at': None, 'events': []}
    assert events.upcoming('TESTCO') == []


def test_corrupt_calendar_degrades_to_empty(paths):
    cfg.EVENT_FILE.write_text('{not json', encoding='utf-8')
    assert events.load()['events'] == []


def test_replace_validates_and_drops_bad_rows(paths):
    doc = events.replace([
        {'date': '2026-08-14', 'type': 'results', 'symbol': 'testco',
         'title': 'Q1 results'},
        {'date': 'not-a-date', 'type': 'results', 'symbol': 'X', 'title': 'x'},
        {'date': '2026-08-15', 'type': 'invented', 'symbol': 'X', 'title': 'x'},
        {'date': '2026-08-16', 'type': 'results', 'title': 'no symbol'},
    ])
    assert len(doc['events']) == 1
    assert doc['events'][0]['symbol'] == 'TESTCO'      # normalised


def test_replace_rejects_an_all_invalid_batch(paths):
    """Silently accepting it would empty the calendar while reporting success."""
    events.replace([{'date': '2026-08-14', 'type': 'budget', 'title': 'Budget'}])
    with pytest.raises(ValueError):
        events.replace([{'date': 'bad', 'type': 'nope', 'title': ''}])
    assert len(events.load()['events']) == 1           # previous survives


def test_upcoming_filters_by_symbol_but_keeps_market_events(paths):
    events.replace([
        {'date': '2026-08-14', 'type': 'results', 'symbol': 'TESTCO',
         'title': 'Q1'},
        {'date': '2026-08-15', 'type': 'results', 'symbol': 'OTHER',
         'title': 'Q1'},
        {'date': '2026-08-16', 'type': 'rbi_policy', 'title': 'MPC'},
    ])
    got = events.upcoming('TESTCO', within_days=10, today=TODAY.date())
    assert {e['type'] for e in got} == {'results', 'rbi_policy'}
    assert all(e.get('symbol') != 'OTHER' for e in got)


def test_past_events_are_not_upcoming(paths):
    events.replace([{'date': '2026-08-01', 'type': 'budget', 'title': 'past'}])
    assert events.upcoming(None, today=TODAY.date()) == []


def test_staleness_drives_the_refresh(paths):
    assert events.is_stale() is True                   # never refreshed
    events.replace([{'date': '2026-08-14', 'type': 'budget', 'title': 'B'}])
    assert events.is_stale() is False
    old = (datetime.now() - timedelta(seconds=cfg.EVENT_REFRESH_SEC + 60))
    assert events.is_stale({'refreshed_at': old.isoformat(), 'events': []})


# ── position review ──────────────────────────────────────────────────────
def test_quiet_position_is_not_reviewed(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    needed, _ = review.needs_review(store.find(1), 96.0, now=TODAY)
    assert needed is False


def test_an_upcoming_event_flags_a_review(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [
        {'type': 'results', 'days_away': 3, 'title': 'Q1 results'}])
    needed, why = review.needs_review(store.find(1), 96.0, now=TODAY)
    assert needed and 'results in 3d' in why


def test_adverse_move_flags_a_review(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    needed, why = review.needs_review(store.find(1), 90.0, now=TODAY)
    assert needed and 'adverse' in why


def test_favourable_move_does_not_flag(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    needed, _ = review.needs_review(store.find(1), 105.0, now=TODAY)
    assert needed is False


def test_review_is_capped_at_once_per_day(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [
        {'type': 'results', 'days_away': 3, 'title': 'Q1'}])
    review.request(store, 1, 'why', {}, spawn=False)
    review.record(store, 1, 'hold')
    needed, _ = review.needs_review(store.find(1), 96.0)
    assert needed is False


def test_a_review_can_never_close_a_position(store):
    """The hard boundary. Exiting stays with the deterministic triggers plus
    the exit gate — the path that has been reviewed and negative-controlled."""
    review.request(store, 1, 'why', {}, spawn=False)
    review.record(store, 1, 'exit', reasons=['thesis dead'])
    assert store.find(1)['status'] == 'entered'
    assert store.find(1).get('exit_reason') is None


def test_review_result_without_a_request_is_discarded(store):
    assert 'discarded' in review.record(store, 1, 'hold')


def test_only_non_hold_recommendations_alert(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    sent = []
    review.request(store, 1, 'why', {}, spawn=False)
    review.record(store, 1, 'hold')
    review.run(store, {'TESTCO': 96.0}, send=lambda m, **k: sent.append(m) or True,
               spawn=False)
    assert sent == []

    review.request(store, 1, 'why2', {}, spawn=False)
    review.record(store, 1, 'adjust', reasons=['roll the short leg'])
    review.run(store, {'TESTCO': 96.0}, send=lambda m, **k: sent.append(m) or True,
               spawn=False)
    assert len(sent) == 1 and 'NOTHING HAS BEEN CLOSED' in sent[0]


def test_review_alert_escapes_html(store):
    msg = review.format_alert(store.find(1), 'adjust',
                              ['spread > 5% & depth < 100'])
    assert '&gt;' in msg and '&lt;' in msg and '&amp;' in msg


def test_review_sweep_is_off_when_the_layer_is_off(store, monkeypatch):
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    assert review.run(store, {'TESTCO': 90.0}, spawn=False) == []


# ── auth watchdog ────────────────────────────────────────────────────────
def _creds(tmp_path, **oauth):
    p = tmp_path / 'creds.json'
    p.write_text(json.dumps({'claudeAiOauth': oauth}), encoding='utf-8')
    return [p]


def test_reads_the_session_expiry_not_the_access_token(paths):
    """A real credential file carries BOTH. `expiresAt` is the access token,
    hours away and auto-refreshed; warning on it would fire every single day
    and be wrong every single day."""
    access = (datetime.now() + timedelta(hours=6)).timestamp() * 1000
    session = (datetime.now() + timedelta(days=27)).timestamp() * 1000
    got = health.credential_expiry(_creds(paths, expiresAt=access,
                                          refreshTokenExpiresAt=session))
    assert got is not None and (got - datetime.now()).days == 26


def test_access_token_only_layout_reports_nothing(paths):
    """Silence beats a daily false alarm."""
    access = (datetime.now() + timedelta(hours=6)).timestamp() * 1000
    assert health.credential_expiry(_creds(paths, expiresAt=access)) is None


def test_missing_credential_file_is_not_an_error(paths):
    assert health.credential_expiry([paths / 'nope.json']) is None


def test_warns_inside_the_window_once_per_day(paths):
    soon = (datetime.now() + timedelta(days=2)).timestamp() * 1000
    sent = []
    send = lambda m, **k: sent.append(m) or True       # noqa: E731
    health.check(send=send, paths=_creds(paths, refreshTokenExpiresAt=soon))
    health.check(send=send, paths=_creds(paths, refreshTokenExpiresAt=soon))
    assert len(sent) == 1 and 'EXPIRING' in sent[0]


def test_does_not_warn_while_the_session_is_healthy(paths):
    far = (datetime.now() + timedelta(days=27)).timestamp() * 1000
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=_creds(paths, refreshTokenExpiresAt=far))
    assert sent == []


def test_an_unsent_warning_is_retried_not_marked_done(paths):
    soon = (datetime.now() + timedelta(days=2)).timestamp() * 1000
    creds = _creds(paths, refreshTokenExpiresAt=soon)
    health.check(send=lambda m, **k: False, paths=creds)
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True, paths=creds)
    assert len(sent) == 1


def test_repeated_spawn_failures_raise_the_alarm_without_a_credential_file(paths):
    for _ in range(3):
        health.record_spawn_result(False)
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=[paths / 'nope.json'])
    assert len(sent) == 1 and 'NOT STARTING' in sent[0]


def test_only_a_landed_agent_clears_the_alarm(paths):
    """A successful Popen proves the BINARY EXISTS, nothing more. An expired
    login spawns perfectly and exits instantly with an auth error — the exact
    failure this watchdog is for. Only a verb landing counts as proof of life."""
    for _ in range(3):
        health.record_spawn_result(False)
    health.record_spawn_result(True)                  # spawned fine...
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=[paths / 'nope.json'])
    assert len(sent) == 1, "a mere spawn silenced the alarm"

    health.record_agent_landed()
    assert health.status()['spawn_failures'] == 0
    assert health.status()['spawns_since_landing']['entry'] == 0


def test_spawning_without_ever_landing_raises_the_alarm(paths):
    """What an expired login actually looks like from the outside: processes
    start all day and not one of them ever reports back."""
    for _ in range(health.SILENT_SPAWN_LIMIT):
        health.record_spawn_result(True)
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=[paths / 'nope.json'])
    assert len(sent) == 1 and 'NOT REPORTING BACK' in sent[0]


def test_a_landed_agent_keeps_the_watchdog_quiet(paths):
    for _ in range(health.SILENT_SPAWN_LIMIT):
        health.record_spawn_result(True)
        health.record_agent_landed()
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=[paths / 'nope.json'])
    assert sent == []


def test_auth_watch_is_silent_when_the_layer_is_off(paths, monkeypatch):
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    soon = (datetime.now() + timedelta(days=1)).timestamp() * 1000
    sent = []
    health.check(send=lambda m, **k: sent.append(m) or True,
                 paths=_creds(paths, refreshTokenExpiresAt=soon))
    assert sent == []


# ── WIRING: drive the monitor, not the module ────────────────────────────
# The veto shadow shipped dead: it was wired into a branch the control flow
# could never reach, because an early `continue` for VETOED sat above it — the
# third "wired but never executes" bug in this fleet. Every test below drives
# check_watching so a future regression that unwires it FAILS here.
@pytest.fixture
def vetoed(paths, monkeypatch):
    """A signal that has been triggered and then vetoed, as the CLI leaves it."""
    from zebra import monitor
    from zebra import vet as vet_mod
    s = ZebraStore(config={})
    s._load_local()
    s.add_signal({'stock': 'VETOCO', 'timeframe': 'weekly', 'direction': 'CE',
                  'st_value': 100.0, 'st_direction': 'UP',
                  'signal_price': 90.0, 'signal_gap_pct': 10.0})
    s.mark_triggered(1, 96.5, 3.5, [])
    vet_mod.request_entry_vet(s, 1, {'stock': 'VETOCO'}, spawn=False)
    vet_mod.record_verdict(s, 1, vet_mod.VETOED, decision_id=1)
    monkeypatch.setattr(monitor, 'get_ltp', lambda kite, stocks: {'VETOCO': 96.5})
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, **k: True)
    return s, monitor


def test_a_veto_opens_its_shadow_through_the_real_monitor(vetoed):
    """THE regression test for the Critical. If open_shadow ever moves back
    below the early `continue`, this fails."""
    store, monitor = vetoed
    monitor.check_watching(store, kite=None, dry_run=True)
    shadow = store.find(1).get('veto_shadow')
    assert shadow, "veto shadow never opened in the live path"
    assert shadow['status'] == 'open'


def test_the_shadow_anchors_on_veto_time_spot_not_the_watch_price(vetoed):
    """signal_price (90.0) is where the signal joined the watchlist, possibly
    days earlier and 10% away. Anchoring there measures a move that started
    before the decision was made."""
    store, monitor = vetoed
    monitor.check_watching(store, kite=None, dry_run=True)
    s = store.find(1)['veto_shadow']
    assert s['entry_spot'] == 96.5
    assert s['adverse_spot'] == 93.0        # symmetric about 96.5 vs target 100


def test_repeated_cycles_do_not_reopen_the_shadow(vetoed):
    store, monitor = vetoed
    for _ in range(3):
        monitor.check_watching(store, kite=None, dry_run=True)
    assert store.find(1)['veto_shadow']['status'] == 'open'
    assert store.find(1)['status'] == 'triggered'      # still never entered


def test_a_vetoed_signal_still_never_enters(vetoed):
    store, monitor = vetoed
    monitor.check_watching(store, kite=None, dry_run=True)
    assert store.find(1)['status'] == 'triggered'
    assert store.find(1).get('entry_date') is None


# ── spawn discipline: a failure must not become a spawn cannon ───────────
def test_refresh_spawns_once_while_one_is_in_flight(paths, spawns):
    """Staleness clears only when an agent lands `events replace`, which takes
    minutes. Without an in-flight marker a stale calendar spawns a fresh Claude
    process EVERY cycle — ~75/day, forever, if the agent can never succeed."""
    for _ in range(4):
        if events.is_stale():
            events.refresh(['TESTCO'])
    assert len(spawns) == 1, "event refresh spawned %d agents" % len(spawns)


def test_refresh_can_spawn_again_once_the_marker_expires(paths, spawns,
                                                         monkeypatch):
    events.refresh(['TESTCO'])
    assert len(spawns) == 1
    monkeypatch.setattr(events, 'refresh_pending', lambda *a, **k: False)
    events.refresh(['TESTCO'])
    assert len(spawns) == 2


def test_a_landed_refresh_clears_the_in_flight_marker(paths, spawns):
    events.refresh(['TESTCO'])
    assert events.refresh_pending() is True
    events.replace([{'date': '2026-08-14', 'type': 'budget', 'title': 'B'}])
    assert events.refresh_pending() is False


def test_an_empty_calendar_install_is_refused(paths):
    """An empty install wipes the calendar AND refreshes the timestamp, so it
    reads healthy for 2h while every gate sees no events — indistinguishable
    from a failed research run."""
    events.replace([{'date': '2026-08-14', 'type': 'budget', 'title': 'B'}])
    with pytest.raises(ValueError, match='empty'):
        events.replace([])
    assert len(events.load()['events']) == 1
    events.replace([], allow_empty=True)              # explicit is fine
    assert events.load()['events'] == []


def test_a_dead_review_agent_does_not_respawn_all_day(store, monkeypatch,
                                                      spawns):
    """A review agent that never reports (expired auth, crash) leaves a pending
    marker that expires. If the daily cap keyed only on COMPLETION, the sweep
    would respawn it every 10 minutes — ~35/day/position, each with a trade
    store write and a Drive upload."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [
        {'type': 'results', 'days_away': 3, 'title': 'Q1'}])
    review.run(store, {'TESTCO': 96.0})
    assert len(spawns) == 1
    # The agent dies. Its deadline lapses; the flagging condition still stands.
    with store._mutate():
        store.find(1)['review']['deadline'] = (
            datetime.now() - timedelta(hours=1)).isoformat()
    for _ in range(3):
        review.run(store, {'TESTCO': 96.0})
    assert len(spawns) == 1, "dead review agent respawned %d times" % len(spawns)


def test_a_review_that_completes_still_caps_at_one_a_day(store, monkeypatch,
                                                         spawns):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [
        {'type': 'results', 'days_away': 3, 'title': 'Q1'}])
    review.run(store, {'TESTCO': 96.0})
    review.record(store, 1, 'hold')
    review.run(store, {'TESTCO': 96.0})
    assert len(spawns) == 1


def test_a_review_alert_is_sent_once_across_repeated_sweeps(store, monkeypatch):
    """The flag is claimed BEFORE sending: an overlapping cron and `zebra loop`
    both see it un-alerted otherwise (flock covers the store, not the cycle)."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    sent = []
    review.request(store, 1, 'why', {}, spawn=False)
    review.record(store, 1, 'adjust', reasons=['roll the short leg'])
    for _ in range(3):
        review.run(store, {'TESTCO': 96.0},
                   send=lambda m, **k: sent.append(m) or True, spawn=False)
    assert len(sent) == 1, "review alert sent %d times" % len(sent)


def test_a_failed_review_alert_is_retried_not_lost(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    review.request(store, 1, 'why', {}, spawn=False)
    review.record(store, 1, 'adjust', reasons=['roll the short leg'])
    review.run(store, {'TESTCO': 96.0}, send=lambda m, **k: False, spawn=False)
    sent = []
    review.run(store, {'TESTCO': 96.0},
               send=lambda m, **k: sent.append(m) or True, spawn=False)
    assert len(sent) == 1, "recommendation lost after a send failure"


# ── give-back watch (2026-08-12) ─────────────────────────────────────────
# The pre-filter fired only on ADVERSE conditions, so a position that ran most
# of the way to target and handed it all back never tripped it — the single
# most expensive pattern in the book (10 of 116 closed trades, -Rs 273,446,
# about twice what the whole book made). Retracing a big gain is not an adverse
# move from ENTRY; it can happen entirely in profit.
def _peaked(store, peak_spot):
    """Entry 96, target 100 (the ST line). Give the position a peak."""
    store.apply_mfe({1: {'mfe_spot': peak_spot}})
    return store.find(1)


def test_giving_back_a_big_gain_flags_a_review(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    t = _peaked(store, 99.5)                    # 87% of the way to target
    needed, why = review.needs_review(t, 96.8, now=TODAY)   # back to 20%
    assert needed and 'gave back' in why


def test_a_position_still_near_its_peak_is_not_flagged(store, monkeypatch):
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    t = _peaked(store, 99.5)
    needed, _ = review.needs_review(t, 99.2, now=TODAY)
    assert needed is False


def test_a_small_gain_given_back_is_not_flagged(store, monkeypatch):
    """Only moves that got MOST of the way to target count. A position that
    reached 30% and slipped is ordinary noise, not the give-back pattern."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    t = _peaked(store, 97.2)                    # 30% of the way
    needed, _ = review.needs_review(t, 96.5, now=TODAY)
    assert needed is False


def test_give_back_is_measured_from_the_peak_not_from_entry(store, monkeypatch):
    """The whole point: this can trigger while the position is still IN PROFIT,
    which is why no adverse-move check could ever have caught it."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    t = _peaked(store, 99.9)
    needed, why = review.needs_review(t, 97.5, now=TODAY)   # +1.6% vs entry
    assert needed, "a profitable position that gave back its gain was ignored"
    assert 'adverse' not in why


def test_no_peak_recorded_means_no_give_back_signal(store, monkeypatch):
    """Positions opened before MFE capture existed carry no peak. Inferring one
    from the current spot would manufacture a signal out of missing data."""
    monkeypatch.setattr(events, 'upcoming', lambda *a, **k: [])
    needed, _ = review.needs_review(store.find(1), 96.8, now=TODAY)
    assert needed is False


# ── strike-adjusting corporate actions (2026-08-12) ──────────────────────
# The calendar had been collecting these and nothing consumed them.
def test_a_bonus_today_is_an_adjustment(paths):
    events.replace([{'date': TODAY.strftime('%Y-%m-%d'), 'type': 'bonus',
                     'symbol': 'TESTCO', 'title': '1:1 bonus'}])
    assert events.adjustment_today('TESTCO', today=TODAY.date())


def test_an_ordinary_dividend_is_NOT_an_adjustment(paths):
    """The strikes are not adjusted, so the drop is real and a stop firing on
    it is a genuine stop. Suppressing it is how a capped loss becomes a
    maximum loss."""
    events.replace([{'date': TODAY.strftime('%Y-%m-%d'), 'type': 'ex_dividend',
                     'symbol': 'TESTCO', 'title': 'Rs 4 dividend'}])
    assert events.adjustment_today('TESTCO', today=TODAY.date()) is None


def test_an_adjustment_on_another_symbol_or_day_is_ignored(paths):
    events.replace([
        {'date': TODAY.strftime('%Y-%m-%d'), 'type': 'split',
         'symbol': 'OTHER', 'title': '1:5 split'},
        {'date': (TODAY + timedelta(days=1)).strftime('%Y-%m-%d'),
         'type': 'split', 'symbol': 'TESTCO', 'title': 'tomorrow'},
    ])
    assert events.adjustment_today('TESTCO', today=TODAY.date()) is None


def test_a_market_wide_event_never_counts_as_an_adjustment(paths):
    events.replace([{'date': TODAY.strftime('%Y-%m-%d'), 'type': 'budget',
                     'title': 'Union Budget'}])
    assert events.adjustment_today('TESTCO', today=TODAY.date()) is None


# ── exit alerts: the claim must come back if the send fails ──────────────
# set_alert_flag is one-time-EVER for the four exit kinds (TIME uses the daily
# variant and self-heals). A claim that is never released silences that exit
# for the life of the position, and in LIVE the alert IS the exit mechanism —
# there is no auto-close. Same discipline the entry path already had.
@pytest.mark.parametrize('kind', ['tp', 'spot_sl', 'debit_sl', 'trail'])
def test_a_failed_exit_alert_releases_its_claim(store, monkeypatch, kind):
    from zebra import monitor
    trade = store.find(1)
    assert store.set_alert_flag(1, kind) is True, "fixture could not claim"
    monkeypatch.setattr(monitor, '_alerts_enabled', lambda t: True)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, dry_run=False: False)

    monitor._send_exit_alert(store, trade, kind, 'msg')

    assert store.set_alert_flag(1, kind) is True, \
        f"{kind} stayed claimed after a failed send — that exit is now silent " \
        f"for the life of the trade"


@pytest.mark.parametrize('kind', ['tp', 'spot_sl', 'debit_sl', 'trail'])
def test_a_delivered_exit_alert_keeps_its_claim(store, monkeypatch, kind):
    """The release must be narrow. If it fired on success too, overlapping
    crons would each re-send the same exit."""
    from zebra import monitor
    trade = store.find(1)
    store.set_alert_flag(1, kind)
    monkeypatch.setattr(monitor, '_alerts_enabled', lambda t: True)
    monkeypatch.setattr(monitor, '_send_telegram', lambda m, dry_run=False: True)

    monitor._send_exit_alert(store, trade, kind, 'msg')

    assert store.set_alert_flag(1, kind) is False, "a delivered alert lost its claim"


def test_every_exit_branch_routes_through_the_release_helper():
    """A future exit kind that calls _send_telegram directly reintroduces the
    orphaned-claim bug silently. Pin the call sites."""
    src = (Path(__file__).resolve().parents[1] / 'monitor.py').read_text(encoding='utf-8')
    for fmt in ('_format_tp_alert', '_format_spot_sl_alert',
                '_format_debit_sl_alert', '_format_trail_alert'):
        assert f"_send_telegram({fmt}" not in src, \
            f"{fmt} is handed straight to _send_telegram — a failed send " \
            f"orphans the consume-once claim and silences that exit forever"
        assert f"_send_exit_alert(store, trade, " in src and fmt in src, \
            f"{fmt} no longer routed through _send_exit_alert"

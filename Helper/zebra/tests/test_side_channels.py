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


def test_a_successful_spawn_clears_the_streak(paths):
    for _ in range(3):
        health.record_spawn_result(False)
    health.record_spawn_result(True)
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

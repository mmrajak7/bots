"""The 2026-08-27 blind-monitoring incident: report the cause, do not guess it.

At 14:40:36 the Pi Telegrammed

    ZEBRA MONITORING BLIND
    No price for ANY of 9 open position(s).
    ... Most likely the Kite access token has expired ...

The token had been generated at 08:45:05 that morning by the scheduled task.
The real cause was on the line above in `logs/cron_zebra_20260827.log`:

    14:40:36 [ERROR] playbook.magnet.scanner: LTP fetch failed: Too many requests

a Kite RATE LIMIT. The alert would have sent the owner to regenerate a healthy
token while the actual fault continued.

Every test here FAILS against the pre-fix code:
  * `common.kite_errors` did not exist, so the classifier tests cannot run
  * the alert text was a hardcoded "Most likely ... expired" with no exception
    reaching it at all — `get_ltp` swallowed it
  * the dedup marker was keyed on the DATE only, so the first cause of the day
    silenced every other one
  * `run_cycle` ran the discretionary scanner BEFORE exit monitoring

Run:  cd Helper && python -m pytest zebra/tests/test_rate_limit_diagnosis.py -v
"""
import json
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import kite_errors                    # noqa: E402
from playbook.magnet import scanner as mscanner    # noqa: E402
from zebra import config as cfg                   # noqa: E402
from zebra import monitor                         # noqa: E402


class FakeKiteError(Exception):
    """Shaped like `kiteconnect.exceptions.KiteException`: `.code` is the real
    HTTP status, the class name is whatever the body's `error_type` said."""

    def __init__(self, message, code=500):
        super().__init__(message)
        self.code = code


class NetworkException(FakeKiteError):
    pass


class TokenException(FakeKiteError):
    pass


def _rate_limited():
    """Exactly what Kite returns for a 429, per its own raw body:
    {"status":"error","message":"Too many requests",
     "error_type":"NetworkException"} — so the CLASS is NetworkException and
    only `.code` distinguishes it from a genuine OMS outage."""
    return NetworkException('Too many requests', code=429)


def _token_dead():
    return TokenException('Incorrect `api_key` or `access_token`.', code=403)


# ── the classifier ───────────────────────────────────────────────────────

def test_a_429_is_rate_limiting_not_a_network_outage():
    """The trap: Kite dresses its rate limit as `NetworkException`. Branching
    on the class alone reads 429 as "the link is down"."""
    assert kite_errors.classify(_rate_limited()) == kite_errors.RATE_LIMIT
    assert kite_errors.is_rate_limit(_rate_limited())
    assert not kite_errors.is_auth_error(_rate_limited())


def test_a_503_network_exception_is_still_a_network_fault():
    assert kite_errors.classify(
        NetworkException('gateway timeout', code=503)) == kite_errors.NETWORK


def test_a_dead_token_is_auth_and_nothing_else():
    assert kite_errors.classify(_token_dead()) == kite_errors.AUTH
    assert not kite_errors.is_rate_limit(_token_dead())


def test_the_incident_message_classifies_even_stripped_of_its_class():
    """A wrapper that loses the class and the code must still not read as
    auth — this is the exact string from the 2026-08-27 log."""
    assert kite_errors.classify(
        RuntimeError('Too many requests')) == kite_errors.RATE_LIMIT


def test_no_exception_is_unknown_not_auth():
    """`get_ltp` returning an empty dict with nothing raised is a real state
    (every symbol unmapped). It must not be reported as a dead token."""
    assert kite_errors.classify(None) == kite_errors.UNKNOWN


# ── the token file is CHECKED, never assumed ─────────────────────────────

def _token_file(tmp_path, generated_at):
    p = tmp_path / 'kite_access_token.json'
    p.write_text(json.dumps({'access_token': 'x', 'api_key': 'y',
                             'generated_at': generated_at}))
    return p


def test_a_fresh_token_is_reported_as_fresh(tmp_path):
    from datetime import datetime
    now = datetime.now().replace(hour=8, minute=45, second=5, microsecond=0)
    st = kite_errors.token_file_status(_token_file(tmp_path, now.isoformat()))
    assert st['is_today'] is True
    assert 'TODAY' in st['summary']


def test_a_missing_token_file_says_so_rather_than_raising(tmp_path):
    st = kite_errors.token_file_status(tmp_path / 'nope.json')
    assert st['exists'] is False and 'MISSING' in st['summary']


def test_diagnose_contradicts_an_auth_guess_when_the_token_is_todays(tmp_path):
    """The 2026-08-27 shape in miniature: even a genuine auth error must not
    be reported as EXPIRY when the file was written this morning."""
    from datetime import datetime
    tok = _token_file(tmp_path, datetime.now().isoformat())
    d = kite_errors.diagnose(_token_dead(), tok)
    assert d['cause'] == kite_errors.AUTH
    assert 'generated TODAY' in d['advice']
    assert 'expiry is NOT the obvious explanation' in d['advice']


# ── the alert itself ─────────────────────────────────────────────────────

@pytest.fixture
def alerting(tmp_path, monkeypatch):
    from datetime import datetime
    sent = []
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'KITE_TOKEN_FILE',
                        _token_file(tmp_path, datetime.now().isoformat()))
    monkeypatch.setattr(monitor, '_send_telegram',
                        lambda m, **k: sent.append(m) or True)
    return sent


def test_a_rate_limit_is_never_reported_as_token_expiry(alerting):
    """THE regression. The pre-fix alert said "Most likely the Kite access
    token has expired" for this exact failure, with a token generated 6 hours
    earlier that morning."""
    monitor._alert_monitoring_blind(9, ['COALINDIA', 'COFORGE'],
                                    error=_rate_limited(), dry_run=True)
    assert alerting, 'monitoring went blind without telling anyone'
    msg = alerting[0]
    assert 'RATE LIMIT' in msg, 'the alert does not name rate limiting'
    assert 'expired' not in msg.lower(), \
        'the alert still claims the token expired'
    assert 'do NOT re-auth' in msg, \
        'the alert must stop the owner regenerating a healthy token'
    assert 'Too many requests' in msg, "Kite's own words are not quoted"
    assert 'TODAY' in msg, 'the token file was not actually checked'


def test_an_auth_failure_is_still_reported_loudly_and_names_the_file(alerting):
    """Accuracy must not cost the alert its teeth. A real dead token still
    says re-auth, and still points at the file."""
    monitor._alert_monitoring_blind(9, ['COALINDIA'], error=_token_dead(),
                                    dry_run=True)
    msg = alerting[0]
    assert 'AUTH' in msg
    assert 'kite_access_token' in msg
    assert 'Regenerate the access token' in msg


def test_a_network_failure_is_neither(alerting):
    monitor._alert_monitoring_blind(9, ['COALINDIA'],
                                    error=NetworkException('conn reset',
                                                           code=503),
                                    dry_run=True)
    msg = alerting[0]
    assert 'NETWORK' in msg
    assert 'expired' not in msg.lower()


def test_a_second_CAUSE_on_the_same_day_still_alerts(alerting):
    """The dedup was keyed on the DATE alone. A morning rate limit would then
    silence an afternoon token death — turning the once-a-day guard into
    exactly the silence it exists to prevent."""
    monitor._alert_monitoring_blind(9, ['A'], error=_rate_limited(),
                                    dry_run=True)
    monitor._alert_monitoring_blind(9, ['A'], error=_rate_limited(),
                                    dry_run=True)
    assert len(alerting) == 1, 'the same cause alerted twice in one day'
    monitor._alert_monitoring_blind(9, ['A'], error=_token_dead(),
                                    dry_run=True)
    assert len(alerting) == 2, 'a NEW cause was swallowed by the day marker'
    assert 'AUTH' in alerting[1]


# ── the cause survives the fetch ─────────────────────────────────────────

def test_get_ltp_records_why_it_failed(monkeypatch):
    """`get_ltp` logged the exception and dropped it, so the alert built on
    top of it had nothing to report and guessed."""
    class K:
        def ltp(self, instruments):
            raise _rate_limited()
    monkeypatch.setattr(mscanner, '_instrument_cache', {'ACME': 1})
    monkeypatch.setattr(mscanner, '_instrument_cache_loaded', True)
    mscanner.clear_ltp_error()
    out = mscanner.get_ltp(K(), ['ACME'])
    assert out == {}
    assert kite_errors.is_rate_limit(mscanner.last_ltp_error())


def test_a_stale_cause_is_never_reported_as_a_fresh_one(monkeypatch):
    class Good:
        def ltp(self, instruments):
            return {'NSE:ACME': {'last_price': 10.0}}
    monkeypatch.setattr(mscanner, '_instrument_cache', {'ACME': 1})
    monkeypatch.setattr(mscanner, '_instrument_cache_loaded', True)
    mscanner.clear_ltp_error()
    mscanner.get_ltp(Good(), ['ACME'])
    assert mscanner.last_ltp_error() is None


# ── the exit path waits out a rate limit, and only a rate limit ──────────

def test_the_spot_fetch_retries_a_rate_limit(monkeypatch):
    """A 429 clears in ~10s. The one fetch that stops exit monitoring is worth
    waiting for — this is the whole margin between a blind cycle and a seeing
    one."""
    slept = []
    calls = []
    monkeypatch.setattr(monitor.time, 'sleep', lambda s: slept.append(s))

    def flaky(kite, stocks):
        calls.append(1)
        if len(calls) == 1:
            mscanner._last_ltp_error = _rate_limited()
            return {}
        mscanner._last_ltp_error = None
        return {'ACME': 100.0}
    monkeypatch.setattr(monitor, 'get_ltp', flaky)
    ltps, err = monitor._spot_ltps(None, ['ACME'])
    assert ltps == {'ACME': 100.0}
    assert err is None
    assert slept and slept[0] >= 10.0, \
        'retried without waiting out Kite\'s sliding cooldown, which EXTENDS it'


def test_the_spot_fetch_does_not_retry_a_dead_token(monkeypatch):
    """Kite throttles sustained 403s into a 429 lockout, so retrying an auth
    failure both fails and disguises itself as the other thing."""
    calls = []
    monkeypatch.setattr(monitor.time, 'sleep',
                        lambda s: pytest.fail('slept on an auth failure'))

    def dead(kite, stocks):
        calls.append(1)
        mscanner._last_ltp_error = _token_dead()
        return {}
    monkeypatch.setattr(monitor, 'get_ltp', dead)
    ltps, err = monitor._spot_ltps(None, ['ACME'])
    assert ltps == {} and len(calls) == 1
    assert kite_errors.is_auth_error(err)


# ── budget: exits get first claim on the per-second quota ────────────────

def test_exit_monitoring_runs_before_the_discretionary_scanner():
    """On 2026-08-27 the scanner ran first, spent ~20s of quota on ~49
    candidates, and `check_entered` was refused three seconds after it
    finished. A missed scan costs nothing; a blind exit check has cost this
    book money twice. Pinned on the source: the ordering IS the control."""
    import inspect
    src = inspect.getsource(monitor.run_cycle)
    assert src.index('check_entered(store, kite') < src.index('validate_and_add('), \
        'the scanner reclaimed priority over exit monitoring'
    assert src.index('check_entered(store, kite') < src.index('check_watching('), \
        'entry checking reclaimed priority over exit monitoring'


def test_the_analyzer_costs_one_quote_not_nine():
    """`analyze()` used to quote the ATM leg plus up to 8 deep-ITM strikes,
    one `kite.quote` each against a 1 req/s cap, to price a back ratio nobody
    trades. Pinned on the source so the loop cannot creep back."""
    import inspect
    from zebra import strikes
    src = inspect.getsource(strikes.analyze)
    assert '_quote_option' in src
    assert src.count('_quote_option(') == 1, \
        'analyze() quotes more than the ATM leg again'
    assert 'k_l_candidates' not in src, 'the deep-ITM candidate loop is back'
    assert '2.0 * long' not in src, 'the back-ratio debit is being priced again'


# ── the historical cache is written, not only read ──────────────────────

def test_fetched_candles_are_cached_without_todays_partial_bar(tmp_path):
    """The cache was read and never written, so every stale symbol re-fetched
    its whole history every 5-minute cycle. Today's bar is excluded because
    this directory is shared with backtest code — a half-formed session must
    never land in it."""
    from datetime import datetime, timedelta
    today = datetime.now().date()
    bars = [{'date': (today - timedelta(days=i)).isoformat(),
             'open': 1, 'high': 2, 'low': 0, 'close': 1, 'volume': 10}
            for i in range(40, -1, -1)]
    f = tmp_path / 'ACME.json'
    mscanner._write_daily_cache(f, 'ACME', bars)
    assert f.exists(), 'nothing was cached; the next cycle re-fetches'
    got = json.loads(f.read_text())
    assert all(c['date'][:10] < today.isoformat() for c in got), \
        "today's incomplete bar was written into the shared backtest cache"
    assert len(got) == len(bars) - 1


def test_a_cache_write_failure_never_stops_a_scan(tmp_path):
    """An optimisation must not become a new way to fail."""
    mscanner._write_daily_cache(tmp_path / 'no' / 'such' / 'dir' / 'x.json',
                                'ACME', [])          # must not raise


def test_historical_fetches_are_paced_under_the_3_per_second_cap(monkeypatch):
    """48 of the 51 `Too many requests` errors on 2026-08-27 were historical
    fetches. The scanner issued them in a tight loop with no pacing whatever,
    against a published cap of 3 req/s.

    Pacing also makes the one-time cache warm-up safe: the first cycle of the
    day now pulls a full 6Y history per symbol, which unpaced would be the
    worst burst of the session."""
    waits = []
    clock = [0.0]
    monkeypatch.setattr(mscanner.time, 'monotonic', lambda: clock[0])

    def fake_sleep(s):
        waits.append(s)
        clock[0] += s
    monkeypatch.setattr(mscanner.time, 'sleep', fake_sleep)
    monkeypatch.setattr(mscanner, '_last_historical_at', 0.0)

    for _ in range(6):
        mscanner._historical_throttle()
    assert len(waits) >= 5, 'historical calls are issued with no pacing at all'
    assert all(w <= 1.0 / 3.0 + 1e-9 for w in waits)
    assert clock[0] >= 5 * (1.0 / 3.0) - 1e-9, \
        '6 historical calls completed faster than the 3 req/s cap allows'


def test_the_throttle_is_wired_into_every_historical_call():
    """`wire_into_live_path`: a throttle called from nowhere is decorative.
    Pinned by counting, because the two-chunk 6Y fetch is two calls and it is
    exactly the kind of place one gets missed."""
    import inspect
    src = inspect.getsource(mscanner)
    assert src.count('kite.historical_data(') == \
        src.count('_historical_throttle()') - 1, \
        'a kite.historical_data call is not preceded by the throttle'

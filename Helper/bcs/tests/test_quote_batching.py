"""F7 — the monitor was exhausting Kite's quote quota, and going blind for it.

`logs/spread_monitor_cron_20260828.log` carries **58** `Too many requests`
failures, still arriving at 10:02:

    [09:59:40]  BCS #457 JINDALSTEL: Spot=1188.50 | TP:1225.05(+36.5) |
                SL:1142.66(+45.8) [QUOTE-FAIL Too many requests]

Every one of those lines is a position that was UNWATCHED for that poll. The
cause is arithmetic, not a bug in the usual sense: 8 open positions x
(1 `/ltp` + 2 `/quote`) every 5 seconds is 4.8 req/s against a quote family
capped at **1 req/s**. Kite's `/quote` and `/ltp` each take up to 500
instruments, so the whole book fits in two calls.

The tests below pin three things, and the third is the one that matters most:

1. the budget — one `/quote` and one `/ltp` per poll, whatever the book size
2. the back-off — a 429 stops further calls instead of extending Kite's own
   sliding cooldown with them
3. **that nothing a valuation guard reads has changed.** This is the exit
   path. A cheaper quote that a guard sees differently is not a saving.

Run:  cd Helper && python -m pytest bcs/tests/test_quote_batching.py -v
"""
import inspect
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from bcs import spread_monitor as sm                              # noqa: E402
from common import kite_errors                                    # noqa: E402


def _book(bid, ask, ltp=None, prev=1.0):
    return {'depth': {'buy': [{'price': bid, 'quantity': 1400}] if bid else [],
                      'sell': [{'price': ask, 'quantity': 1400}] if ask else []},
            'last_price': ltp if ltp is not None else (bid + ask) / 2.0,
            'ohlc': {'close': prev},
            'last_trade_time': None}


class CountingBroker:
    """Counts CALLS, not instruments — the rate limiter counts calls too."""

    def __init__(self, books=None, spots=None, quote_raises=None,
                 ltp_raises=None):
        self.books = books or {}
        self.spots = spots or {}
        self.quote_calls = []      # one entry per HTTP call, holding its batch
        self.ltp_calls = []
        self.quote_raises = quote_raises
        self.ltp_raises = ltp_raises

    def quote(self, instruments):
        self.quote_calls.append(list(instruments))
        if self.quote_raises:
            raise self.quote_raises
        return {i: self.books[i] for i in instruments if i in self.books}

    def ltp(self, symbols):
        self.ltp_calls.append(list(symbols))
        if self.ltp_raises:
            raise self.ltp_raises
        return {s: {'last_price': self.spots[s]}
                for s in symbols if s in self.spots}


def _cohort(n):
    """n BCS records shaped like the ones open on the Pi this morning."""
    out = []
    for i in range(n):
        stock = 'STK%d' % i
        out.append({'id': 400 + i, 'stock': stock, 'exchange': 'NFO',
                    'spot_symbol': 'NSE:%s' % stock,
                    'long_symbol': '%s26SEP100CE' % stock,
                    'short_symbol': '%s26SEP110CE' % stock,
                    'net_debit': 3.0, 'quantity': 700, 'paper': True})
    return out


def _broker_for(trades):
    books, spots = {}, {}
    for t in trades:
        books['NFO:%s' % t['long_symbol']] = _book(8.00, 8.20)
        books['NFO:%s' % t['short_symbol']] = _book(4.00, 4.20)
        spots[t['spot_symbol']] = 105.0
    return CountingBroker(books=books, spots=spots)


def _scrub():
    # `getattr` so this file can be run against PRE-FIX code, where none of
    # these exist: the point of a test is to fail on its ASSERTION, not to
    # error in a hygiene fixture before it reaches one.
    getattr(sm, 'reset_quote_cache', lambda: None)()
    getattr(sm, 'reset_quote_cooldown', lambda: None)()


@pytest.fixture(autouse=True)
def _clean():
    _scrub()
    yield
    _scrub()


# ── 1. the budget ───────────────────────────────────────────────────────────

def test_a_whole_poll_costs_two_requests_however_big_the_book():
    """THE fix. Pre-fix this was 8 ltp + 16 quote = 24 calls per poll at
    4.8 req/s; the family cap is 1 req/s."""
    trades = _cohort(8)
    k = _broker_for(trades)
    # a DELTA: `quote_stats` accumulates for the life of the process, because
    # the poll's log line reports the session's running budget.
    singles_before = sm.quote_stats['singles']

    sm.prefetch_book(k, trades)
    for t in trades:                       # exactly what the poll loop does
        sm.get_spot(k, t['spot_symbol'])
        sm.get_spread_value(k, t, spot=105.0)

    assert len(k.quote_calls) == 1, (
        'leg quotes are not batched: %d /quote calls for 8 positions'
        % len(k.quote_calls))
    assert len(k.ltp_calls) == 1
    assert len(k.quote_calls[0]) == 16     # both legs of all eight, one call
    assert sm.quote_stats['singles'] == singles_before, (
        'something fetched off the batch')


def test_the_budget_does_not_grow_with_the_book():
    """The 2026-08-27 incident note says this scales with the open book, which
    is what made it arrive as a load curve rather than a bug. Sixteen
    positions must still cost two requests."""
    for n in (1, 4, 8, 16):
        _scrub()
        trades = _cohort(n)
        k = _broker_for(trades)
        sm.prefetch_book(k, trades)
        for t in trades:
            sm.get_spot(k, t['spot_symbol'])
            sm.get_spread_value(k, t, spot=105.0)
        assert (len(k.quote_calls), len(k.ltp_calls)) == (1, 1), n


def test_without_the_prefetch_it_is_the_pre_fix_shape():
    """The negative control, and the record of the before number. Remove
    `prefetch_book` from the poll loop and this is what comes back."""
    trades = _cohort(8)
    k = _broker_for(trades)
    for t in trades:
        sm.get_spot(k, t['spot_symbol'])
        sm.get_spread_value(k, t, spot=105.0)
    assert len(k.quote_calls) == 16
    assert len(k.ltp_calls) == 8           # 24 requests per poll, as measured


def test_the_same_instrument_is_never_fetched_twice_in_one_poll():
    """Two records on the same stock share a spot symbol, and the FH status
    read touches legs the trigger path already read."""
    trades = _cohort(1) + _cohort(1)
    trades[1]['id'] = 999
    k = _broker_for(trades)
    sm.prefetch_book(k, trades)
    assert len(k.quote_calls[0]) == 2, 'duplicate instruments were requested'
    assert len(k.ltp_calls[0]) == 1


def test_the_poll_loop_actually_calls_the_prefetch():
    """A batching helper nothing calls is not a fix. Pinned on the source of
    the cron loop, which is the code that runs on the Pi."""
    src = inspect.getsource(sm.monitor_all)
    assert 'prefetch_book(kite, all_trades)' in src
    assert src.index('prefetch_book(kite, all_trades)') < src.index(
        'for trade in all_trades:'), 'the prefetch runs after the loop it feeds'


# ── 2. what the guards see must not change ──────────────────────────────────

def test_a_batched_depth_is_identical_to_a_single_fetch():
    """The whole risk of this change in one assertion. Every valuation guard —
    `leg_quote_reliable`, the intrinsic floor, the depth checks in the order
    path — reads this dict."""
    t = _cohort(1)[0]
    k = _broker_for([t])

    single = sm.get_option_depth(k, 'NFO', t['long_symbol'])
    _scrub()
    sm.prefetch_book(k, [t])
    batched = sm.get_option_depth(k, 'NFO', t['long_symbol'])
    assert batched == single


def test_the_reliability_gate_reaches_the_same_verdict_on_a_junk_book():
    """The NHPC shape through the batched path: bid 0.28 / ask 1.40."""
    t = _cohort(1)[0]
    k = _broker_for([t])
    k.books['NFO:%s' % t['short_symbol']] = _book(0.28, 1.40, ltp=0.30)
    sm.prefetch_book(k, [t])
    r = sm.get_spread_value(k, t, spot=105.0)
    assert r['spread'] is None
    assert 'wide_book' in r['unreliable']


def test_the_reverify_quote_is_never_served_from_the_poll_cache():
    """`close_spread`'s re-verify is THE second look before real orders. A
    cached answer would re-check the trigger against the quote that caused
    it, which passes every time."""
    t = _cohort(1)[0]
    k = _broker_for([t])
    sm.prefetch_book(k, [t])
    before = len(k.quote_calls)
    sm.get_spread_value(k, t, spot=105.0)                  # cached
    assert len(k.quote_calls) == before
    sm.get_spread_value(k, t, spot=105.0, fresh=True)      # must hit Kite
    assert len(k.quote_calls) == before + 2


def test_the_order_path_asks_the_broker_on_every_depth_wait():
    """`close_leg`'s loop waits for the book to RE-FORM and then prices an
    order off it. Served from a cache it would neither change between waits
    nor reflect what the order meets. Source-pinned: driving the whole retry
    loop here would test the harness, not the guard."""
    src = inspect.getsource(sm.close_leg)
    assert 'get_option_depth(kite, exchange, symbol, fresh=True)' in src
    assert 'get_option_depth(kite, exchange, symbol)' not in src


def test_a_cached_quote_older_than_the_ttl_is_refetched(monkeypatch):
    """Backstop. The cache is dropped every poll, but a caller that forgot to
    prefetch — or a poll where `close_spread` spent 30 seconds mid-loop —
    must not hand a stale price to an exit guard."""
    t = _cohort(1)[0]
    k = _broker_for([t])
    now = [1000.0]
    monkeypatch.setattr(sm.time, 'time', lambda: now[0])
    sm.prefetch_book(k, [t])
    assert len(k.quote_calls) == 1
    now[0] += sm.QUOTE_CACHE_TTL_SEC + 0.1
    sm.get_option_depth(k, 'NFO', t['long_symbol'])
    assert len(k.quote_calls) == 2, 'a stale quote was served to a guard'


def test_the_ttl_is_shorter_than_the_poll_interval():
    """Stated as a rule, not left to a reader comparing two constants: a quote
    older than one poll must never reach a valuation guard."""
    assert sm.QUOTE_CACHE_TTL_SEC <= sm.POLL_INTERVAL_SEC


# ── 3. backing off, rather than making it worse ─────────────────────────────

class _429(Exception):
    code = 429


def test_a_rate_limited_batch_does_not_retry_per_instrument():
    """The pre-fix behaviour was 18 calls, all failing, all EXTENDING Kite's
    sliding 10s window. The failure must be replayed, not re-attempted."""
    trades = _cohort(8)
    k = _broker_for(trades)
    k.quote_raises = _429('Too many requests')
    sm.prefetch_book(k, trades)
    assert len(k.quote_calls) == 1
    for t in trades:
        with pytest.raises(Exception):
            sm.get_spread_value(k, t, spot=105.0)
    assert len(k.quote_calls) == 1, (
        'a failed batch fell back to per-instrument calls, which is exactly '
        'what extends the cooldown')


def test_a_429_arms_a_local_cooldown_that_refuses_further_calls():
    t = _cohort(1)[0]
    k = _broker_for([t])
    k.quote_raises = _429('Too many requests')
    sm.prefetch_book(k, [t])
    assert sm.quote_cooldown_remaining() > 0
    k.quote_raises = None                  # Kite would answer now; we do not ask
    with pytest.raises(sm.QuoteRateLimited):
        sm.get_option_depth(k, 'NFO', t['long_symbol'], fresh=True)
    assert len(k.quote_calls) == 1


def test_the_cooldown_reads_as_a_rate_limit_to_the_shared_classifier():
    """`common.kite_errors` is the ONE definition of why a Kite call failed,
    and the blind alert, `_is_auth_error` and the diagnosis text all route
    through it. A locally-generated backoff must look exactly like the
    server's own refusal, or the alert will tell the owner to re-auth — the
    2026-08-27 mistake that module exists to end."""
    exc = sm.QuoteRateLimited('Too many requests — local backoff, 9s left.')
    assert kite_errors.classify(exc) == kite_errors.RATE_LIMIT
    assert kite_errors.is_auth_error(exc) is False
    assert sm._is_auth_error(exc) is False


def test_the_cooldown_survives_a_cache_reset():
    """The cache is per poll; the rate limiter is Kite's. Clearing the
    cooldown every 5 seconds would defeat the whole back-off."""
    t = _cohort(1)[0]
    k = _broker_for([t])
    k.quote_raises = _429('Too many requests')
    sm.prefetch_book(k, [t])
    left = sm.quote_cooldown_remaining()
    assert left > 0
    sm.reset_quote_cache()
    assert sm.quote_cooldown_remaining() > 0


def test_a_missing_instrument_still_raises_rather_than_inventing_a_price():
    """Kite omits instruments it does not know. The old single-instrument
    `kite.quote([full])[full]` raised KeyError; the batched read must too,
    and must NOT quietly spend a request re-asking."""
    t = _cohort(1)[0]
    k = _broker_for([t])
    del k.books['NFO:%s' % t['short_symbol']]
    sm.prefetch_book(k, [t])
    with pytest.raises(KeyError):
        sm.get_option_depth(k, 'NFO', t['short_symbol'])
    assert len(k.quote_calls) == 1


def test_every_leg_of_a_four_legged_fh_record_is_prefetched():
    """`trade_instruments` reads `*_symbol` generically rather than naming
    BCS's pair — a hand-written list is the shape that misses the leg someone
    added later."""
    fh = {'id': 1, 'stock': 'TESTCO', 'exchange': 'NFO',
          'spot_symbol': 'NSE:TESTCO',
          'short_call_symbol': 'TESTCO26SEP3000CE',
          'short_put_symbol': 'TESTCO26SEP2600PE',
          'long_put_symbol': 'TESTCO26SEP2550PE',
          'long_call_symbol': 'TESTCO26SEP3200CE'}
    got = sorted(sm.trade_instruments(fh))
    assert got == sorted(['NFO:TESTCO26SEP3000CE', 'NFO:TESTCO26SEP2600PE',
                          'NFO:TESTCO26SEP2550PE', 'NFO:TESTCO26SEP3200CE'])
    assert 'NSE:TESTCO' not in got, 'the underlying is an /ltp instrument'

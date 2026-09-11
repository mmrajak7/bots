"""zebra priced its book one leg at a time against a 1 req/s quote cap.

2026-09-11: with 9 open positions and 10 structure shadows, a zebra cycle made
~40 single-instrument `kite.quote` calls in ~20 seconds on the API key it
shares with the BCS monitor. The monitor's own calls were refused at 09:35:32,
09:55:31 and 10:35:22, each within four seconds of zebra's shadow pass, and the
09:55 refusal blinded the monitor for 25 minutes (that lockout is a separate
defect, fixed in `bcs/spread_monitor.py`). The monitor had batched itself on
2026-08-28; zebra had not.

Pinned here:
1. the budget — a whole book costs one quote call, whatever its size
2. nothing a valuation guard reads has changed — a batched quote parses to the
   identical dict a single call produces
3. a refused batch is replayed, never retried one leg at a time
4. staleness — nothing older than the TTL is served, and a plain
   `_quote_option` never fills the cache
5. the wiring — both call sites actually prefetch, before they read

Run:  cd Helper && python -m pytest zebra/tests/test_quote_prefetch.py -v
"""
import inspect

import pytest

from zebra import monitor, strikes, structure_shadow


def _book(bid, ask, oi=50_000):
    return {'depth': {'buy': [{'price': bid, 'quantity': 700}],
                      'sell': [{'price': ask, 'quantity': 350}]},
            'last_price': round((bid + ask) / 2, 2), 'oi': oi,
            'last_trade_time': None}


class CountingKite:
    """Counts CALLS, not instruments — the rate limiter counts calls too."""

    def __init__(self, books=None, raises=None):
        self.books = books or {}
        self.raises = raises
        self.calls = []

    def quote(self, keys):
        self.calls.append(list(keys))
        if self.raises:
            raise self.raises
        return {k: self.books[k] for k in keys if k in self.books}


class _429(Exception):
    code = 429


def _pairs(n):
    return [('STK%d26SEP100CE' % i, 'STK%d26SEP110CE' % i) for i in range(n)]


def _symbols(pairs):
    return [s for p in pairs for s in p]


def _kite_for(pairs):
    books = {}
    for long_sym, short_sym in pairs:
        books['NFO:' + long_sym] = _book(8.0, 8.2)
        books['NFO:' + short_sym] = _book(4.0, 4.2)
    return CountingKite(books)


@pytest.fixture(autouse=True)
def _clean():
    strikes.reset_quote_cache()
    yield
    strikes.reset_quote_cache()


# ── 1. the budget ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('n', [1, 9, 40])
def test_a_whole_book_costs_one_quote_call_however_big(n):
    pairs = _pairs(n)
    k = _kite_for(pairs)
    strikes.prefetch_quotes(k, _symbols(pairs))
    for long_sym, short_sym in pairs:
        assert strikes._quote_option(k, long_sym)['mid'] == 8.1
        assert strikes._quote_option(k, short_sym)['mid'] == 4.1
    assert len(k.calls) == 1, (
        '%d positions cost %d quote calls' % (n, len(k.calls)))


def test_the_batch_splits_only_past_the_per_call_maximum():
    pairs = _pairs(strikes.QUOTE_BATCH_MAX)          # 2x the max instruments
    k = _kite_for(pairs)
    assert strikes.prefetch_quotes(k, _symbols(pairs)) == 2
    assert len(k.calls) == 2


def test_a_second_pass_pays_only_for_legs_it_adds():
    """The shadow pass shares legs with the open positions. Within the TTL it
    must not buy them twice."""
    pairs = _pairs(2)
    k = _kite_for(pairs)
    strikes.prefetch_quotes(k, list(pairs[0]))
    strikes.prefetch_quotes(k, _symbols(pairs))
    assert k.calls[1] == sorted('NFO:' + s for s in pairs[1])


# ── 2. nothing a guard reads has changed ────────────────────────────────────

def test_a_batched_quote_is_identical_to_a_single_call():
    """Every exit guard reads this dict. A cheaper quote that a guard sees
    differently is not a saving."""
    pairs = _pairs(3)
    single = {s: strikes._quote_option(_kite_for(pairs), s)
              for s in _symbols(pairs)}
    k = _kite_for(pairs)
    strikes.prefetch_quotes(k, list(single))
    assert {s: strikes._quote_option(k, s) for s in single} == single
    assert len(k.calls) == 1


# ── 3. a refused batch is replayed, not retried ─────────────────────────────

def test_a_refused_batch_is_replayed_not_retried_per_leg():
    pairs = _pairs(9)
    k = _kite_for(pairs)
    k.raises = _429('Too many requests')
    assert strikes.prefetch_quotes(k, _symbols(pairs)) == 0
    k.raises = None                  # Kite would answer now; we must not ask
    for s in _symbols(pairs):
        q = strikes._quote_option(k, s)
        assert q['reliable'] is False and q['mid'] == 0
        assert 'Too many requests' in q['unreliable_reason']
    assert len(k.calls) == 1, (
        'a refused batch fell back to one call per leg — the burst that '
        'extends the cooldown')


def test_a_refused_batch_defers_the_position_rather_than_valuing_it():
    """End to end through the exit path's own reader: a position priced off a
    refused batch must come back unvalued, exactly as a failed single call
    did, so `check_entered` defers it instead of booking a made-up price."""
    t = {'id': 1, 'long_symbol': 'AAA26SEP100CE',
         'short_symbol': 'AAA26SEP110CE', 'structure': 'bcs'}
    k = CountingKite(raises=_429('Too many requests'))
    strikes.prefetch_quotes(k, [t['long_symbol'], t['short_symbol']])
    q = monitor._structure_quote(k, t)
    assert q['mid'] is None and q['reliable'] is False
    assert len(k.calls) == 1


def test_an_instrument_kite_leaves_out_is_a_miss_not_an_extra_call():
    pairs = _pairs(2)
    k = _kite_for(pairs)
    del k.books['NFO:' + pairs[1][1]]
    strikes.prefetch_quotes(k, _symbols(pairs))
    q = strikes._quote_option(k, pairs[1][1])
    assert q.get('error') and q['reliable'] is False
    assert len(k.calls) == 1


def test_prefetch_never_raises():
    """It sits in front of exit monitoring. A broken broker object must cost
    the prefetch, never the phase."""
    assert strikes.prefetch_quotes(object(), ['X26SEP1CE']) == 0
    assert strikes.prefetch_quotes(None, ['X26SEP1CE']) == 0
    assert strikes.prefetch_quotes(CountingKite(), []) == 0
    assert strikes.prefetch_quotes(CountingKite(), [None, '']) == 0


# ── 4. staleness ────────────────────────────────────────────────────────────

def test_a_plain_quote_never_fills_the_cache():
    """Only an explicit prefetch may make a later read cheaper. A caller that
    re-quotes a book to watch it change must keep getting the broker's answer."""
    pairs = _pairs(1)
    sym = pairs[0][0]
    k = _kite_for(pairs)
    strikes._quote_option(k, sym)
    k.books['NFO:' + sym] = _book(9.0, 9.2)
    assert strikes._quote_option(k, sym)['mid'] == 9.1
    assert len(k.calls) == 2


def test_a_quote_older_than_the_ttl_is_asked_again(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(strikes, '_clock', lambda: now[0])
    pairs = _pairs(1)
    sym = pairs[0][0]
    k = _kite_for(pairs)
    strikes.prefetch_quotes(k, [sym])
    now[0] += strikes.QUOTE_CACHE_TTL_SEC + 0.1
    k.books['NFO:' + sym] = _book(9.0, 9.2)
    assert strikes._quote_option(k, sym)['mid'] == 9.1
    assert len(k.calls) == 2


def test_a_stale_miss_is_asked_again_not_replayed(monkeypatch):
    """A failure is a fact about the feed at that moment. Replaying it after
    the TTL would blind a position for no reason. (A network error, not a 429:
    a 429 also holds the cooldown, which has its own tests below.)"""
    now = [1000.0]
    monkeypatch.setattr(strikes, '_clock', lambda: now[0])
    pairs = _pairs(1)
    sym = pairs[0][0]
    k = _kite_for(pairs)
    k.raises = ConnectionError('Connection reset by peer')
    strikes.prefetch_quotes(k, [sym])
    k.raises = None
    now[0] += strikes.QUOTE_CACHE_TTL_SEC + 0.1
    assert strikes._quote_option(k, sym)['mid'] == 8.1
    assert len(k.calls) == 2


def test_a_failed_batch_is_not_asked_again_inside_its_ttl():
    """Reviewer M2. The exit loop re-batches before every position; if a fresh
    MISS did not count as answered, each position after a failed batch would
    re-ask Kite — one request per position, the burst this file removes.
    (A network error, so the 429 cooldown is not what holds it.)"""
    pairs = _pairs(3)
    k = _kite_for(pairs)
    k.raises = ConnectionError('Connection reset by peer')
    strikes.prefetch_quotes(k, _symbols(pairs))
    k.raises = None
    for _ in pairs:
        assert strikes.prefetch_quotes(k, _symbols(pairs)) == 0
    assert len(k.calls) == 1


def test_a_lapsed_batch_is_refreshed_in_one_request_not_one_per_leg(monkeypatch):
    """Reviewer M1. A position that outlasts the TTL (a Drive write, the M12
    in-cycle vet wait) must not send the rest of the book back to per-leg
    calls: the loop's re-batch pays one request for everything that lapsed."""
    now = [1000.0]
    monkeypatch.setattr(strikes, '_clock', lambda: now[0])
    pairs = _pairs(9)
    k = _kite_for(pairs)
    strikes.prefetch_quotes(k, _symbols(pairs))
    now[0] += 120.0
    strikes.prefetch_quotes(k, _symbols(pairs))
    for s in _symbols(pairs):
        strikes._quote_option(k, s)
    assert len(k.calls) == 2


def test_a_429_holds_every_quote_call_for_the_cooldown(monkeypatch):
    """Reviewer M2. Once the TTL lapses the replayed refusal is gone, and
    Kite's sliding window is still open. Nothing may ask until it has passed."""
    now = [1000.0]
    monkeypatch.setattr(strikes, '_clock', lambda: now[0])
    pairs = _pairs(2)
    syms = _symbols(pairs)
    k = _kite_for(pairs)
    k.raises = _429('Too many requests')
    strikes.prefetch_quotes(k, syms)
    k.raises = None
    now[0] += strikes.QUOTE_CACHE_TTL_SEC + 0.1       # misses stale, window open
    assert strikes.prefetch_quotes(k, syms) == 0
    q = strikes._quote_option(k, syms[0])
    assert q['reliable'] is False and 'cooldown' in q['unreliable_reason']
    assert len(k.calls) == 1
    now[0] = 1000.0 + strikes.QUOTE_COOLDOWN_SEC + 0.1
    assert strikes.prefetch_quotes(k, syms) == 1
    assert strikes._quote_option(k, syms[0])['mid'] == 8.1


def test_a_local_refusal_never_extends_the_cooldown(monkeypatch):
    """`bcs.spread_monitor`, 2026-09-11: its own refusal re-armed the backoff
    it was refused by, and one 429 blinded it for 25 minutes. The same shape
    must not be buildable here — including by replaying the ORIGINAL 429,
    which the shared classifier would read as a fresh one."""
    now = [1000.0]
    monkeypatch.setattr(strikes, '_clock', lambda: now[0])
    pairs = _pairs(1)
    syms = _symbols(pairs)
    k = _kite_for(pairs)
    k.raises = _429('Too many requests')
    strikes.prefetch_quotes(k, syms)
    k.raises = None
    armed = strikes._cooldown_until
    for _ in range(4):                                # 10s of refused reads
        now[0] += 2.5
        strikes.prefetch_quotes(k, syms)
        strikes._quote_option(k, syms[0])
    assert strikes._cooldown_until == armed
    now[0] = 1000.0 + strikes.QUOTE_COOLDOWN_SEC + 0.1
    assert strikes._quote_option(k, syms[0])['mid'] == 8.1


def test_the_local_refusal_does_not_read_as_kites_own():
    """The second lock on the rule above: were the type check ever lost, the
    shared classifier must still not find a rate limit in our own wording."""
    from common import kite_errors
    strikes._cooldown_until = strikes._clock() + 5.0
    err = strikes._cooldown_error()
    assert err is not None and not kite_errors.is_rate_limit(err)


def test_a_single_call_429_arms_the_cooldown_too():
    """The entry analyzer and `zebra quote` read without a prefetch. A 429 there
    is the same window, and the next read inside it must not extend it."""
    k = CountingKite(raises=_429('Too many requests'))
    strikes._quote_option(k, 'AAA26SEP100CE')
    k.raises = None
    k.books['NFO:BBB26SEP100CE'] = _book(8.0, 8.2)
    assert strikes._quote_option(k, 'BBB26SEP100CE')['reliable'] is False
    assert len(k.calls) == 1


def test_an_ordinary_failure_does_not_arm_the_cooldown():
    k = CountingKite(raises=ConnectionError('Connection reset by peer'))
    strikes._quote_option(k, 'AAA26SEP100CE')
    k.raises = None
    k.books['NFO:BBB26SEP100CE'] = _book(8.0, 8.2)
    assert strikes._quote_option(k, 'BBB26SEP100CE')['mid'] == 8.1


def test_the_cooldown_is_no_shorter_than_the_monitors():
    from bcs import spread_monitor
    assert strikes.QUOTE_COOLDOWN_SEC >= spread_monitor.QUOTE_COOLDOWN_SEC


def test_a_replayed_miss_does_not_grow_its_traceback():
    """Reviewer L1: one exception object, raised once per leg per cycle, gained
    a frame each time and was held for the life of `zebra loop`."""
    import traceback
    sym = _pairs(1)[0][0]
    k = CountingKite(raises=ConnectionError('Connection reset by peer'))
    strikes.prefetch_quotes(k, [sym])
    for _ in range(20):
        strikes._quote_option(k, sym)
    exc = strikes._quote_misses['NFO:' + sym][1]
    assert len(traceback.extract_tb(exc.__traceback__)) <= 2


class InputException(Exception):
    """Named like kiteconnect's, which is what `_rejected_input` reads."""
    code = 400


def test_a_batch_kite_rejects_for_one_bad_symbol_does_not_blind_the_book():
    """Pre-deploy review M1. If Kite refuses a whole /quote because ONE leg's
    symbol expired or was renamed by a corporate action, recording that against
    every leg would leave the entire book unpriced every cycle — where the old
    per-leg calls lost only that position. The good legs must still price, and
    the bad one must fail alone.

    The message says 'token' on purpose: the shared classifier reads that as
    AUTH, which is why the decision is made on class and status, not text."""
    bad = 'GONE26SEP100CE'
    pairs = _pairs(3)

    class RejectsBatchesHoldingBad(CountingKite):
        def quote(self, keys):
            self.calls.append(list(keys))
            if 'NFO:' + bad in keys:
                raise InputException('Invalid `instrument_token` in request')
            return {k: self.books[k] for k in keys if k in self.books}

    k = RejectsBatchesHoldingBad(_kite_for(pairs).books)
    strikes.prefetch_quotes(k, _symbols(pairs) + [bad])
    for long_sym, short_sym in pairs:
        assert strikes._quote_option(k, long_sym)['mid'] == 8.1
        assert strikes._quote_option(k, short_sym)['mid'] == 4.1
    assert strikes._quote_option(k, bad)['reliable'] is False
    assert strikes._cooldown_until == 0.0, 'an input error armed the 429 cooldown'


def test_a_rate_limit_is_never_mistaken_for_a_rejected_input():
    assert strikes._rejected_input(InputException('bad instrument')) is True
    assert strikes._rejected_input(_429('Too many requests')) is False
    assert strikes._rejected_input(ConnectionError('reset')) is False

    class _Bool(Exception):
        code = True                       # bool is an int; never a status
    assert strikes._rejected_input(_Bool()) is False


# ── the real exit phase, driven end to end (reviewer L3) ────────────────────

@pytest.fixture
def one_open_position(tmp_path, monkeypatch):
    """`test_bcs_only.wired`, entered: one real paper BCS in a real store.

    The expiry is pushed out of reach so no TIME, delivery or expiry path can
    close the position before the quote is read — a fixture dated 2026-09-30
    would turn these tests red three weeks from now."""
    from zebra import config as cfg
    from zebra.tests import test_bcs_only as bo
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)
    monkeypatch.setattr(cfg, 'PAPER_MODE', True)
    monkeypatch.setattr(cfg, 'ENTRY_STRUCTURE', 'bcs')
    monkeypatch.setattr(monitor, 'get_ltp',
                        lambda kite, stocks: {'TESTCO': bo.SPOT})
    monkeypatch.setattr(monitor.strikes_mod, 'analyze',
                        lambda *a, **k: dict(bo.ANALYSIS, expiry='2099-12-30'))
    monkeypatch.setattr(monitor.strikes_mod, 'analyze_bcs',
                        lambda *a, **k: dict(bo.BCS))
    store = ZebraStore(config={})
    store._load_local()
    store.add_signal(dict(bo.SIGNAL))
    monitor.check_watching(store, kite=None, dry_run=True)
    trade = store.find(1)
    assert trade['status'] == 'entered', trade.get('status')
    assert str(trade.get('expiry', '')).startswith('2099'), trade.get('expiry')
    strikes.reset_quote_cache()
    return store, trade


def _leg_calls(k, trade):
    legs = {'NFO:' + trade['long_symbol'], 'NFO:' + trade['short_symbol']}
    return [c for c in k.calls if legs & set(c)]


def test_the_exit_phase_prices_a_position_in_one_request(one_open_position):
    """The behavioural half of the wiring guard below: the real
    `check_entered` and `_structure_quote` against a broker that counts calls.
    Before the fix this position cost two single-leg requests."""
    store, trade = one_open_position
    k = CountingKite({'NFO:' + trade['long_symbol']: _book(11.9, 12.1),
                      'NFO:' + trade['short_symbol']: _book(1.9, 2.1)})
    monitor.check_entered(store, kite=k, dry_run=True)
    assert _leg_calls(k, trade) == [sorted(
        ['NFO:' + trade['long_symbol'], 'NFO:' + trade['short_symbol']])]


def test_a_refused_batch_defers_the_book_quietly(one_open_position, telegrams):
    """Reviewer L1. One 429 defers the whole book for a cycle. It must not
    retry per leg, must not raise out of the exit phase, must not close the
    position on a missing price, and must not alarm the owner — the blind
    alert is for consecutive cycles, not one."""
    store, trade = one_open_position
    k = CountingKite({'NFO:' + trade['long_symbol']: _book(11.9, 12.1),
                      'NFO:' + trade['short_symbol']: _book(1.9, 2.1)},
                     raises=_429('Too many requests'))
    monitor.check_entered(store, kite=k, dry_run=True)
    assert len(_leg_calls(k, trade)) == 1, 'a refused batch was retried'
    assert store.find(1)['status'] == 'entered'
    assert not [m for m in telegrams if 'BLIND' in m.upper()], telegrams


def test_the_ttl_is_no_looser_than_the_monitors():
    """The two engines value the same cohort. A looser bound here would let
    zebra's exit guard read a book the monitor's would already refuse."""
    from bcs import spread_monitor
    assert strikes.QUOTE_CACHE_TTL_SEC <= spread_monitor.QUOTE_CACHE_TTL_SEC


# ── 5. the wiring ───────────────────────────────────────────────────────────

def test_the_exit_phase_prefetches_before_it_values_a_position():
    """The budget tests above prove `prefetch_quotes` batches; only this proves
    the exit phase calls it. Without the call every behaviour test still
    passes and the cycle is back to two requests per position.

    RETIRES WHEN: `check_entered` no longer quotes legs itself — valuation
    moves behind one shared quote service that both engines call, or a
    behavioural test drives a full `check_entered` against a call-counting
    broker.
    """
    src = inspect.getsource(monitor.check_entered)
    assert 'prefetch_quotes(' in src, 'check_entered no longer batches its legs'
    # INSIDE the per-position loop (reviewer M1): above it, one slow position
    # sends every position after it back to per-leg calls.
    assert (src.index('for trade in entered:') < src.index('prefetch_quotes(')
            < src.index('_structure_quote(kite, trade')), (
        'the re-batch moved out of the per-position loop')


def test_the_shadow_pass_prefetches_before_its_loop():
    """Same gap as above, for the pass that sat inside all three 429s.

    RETIRES WHEN: `structure_shadow.poll` stops quoting per shadow (it reads
    the exit phase's quotes, or the shadow measurement is retired), or a
    behavioural test drives `poll` against a call-counting broker.
    """
    src = inspect.getsource(structure_shadow.poll)
    assert 'prefetch_quotes(' in src, 'the shadow pass no longer batches its legs'
    assert src.index('prefetch_quotes(') < src.index('_quote_option(kite')

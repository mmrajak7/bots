"""Test-wide safety RAILS for the LIVE-money package.

Six of them now, autouse, listed here because a rail nobody can find is one
the next file re-implements badly. Each exists because something already
escaped:

1. **No real Telegram** (`_no_telegram_http`). Reported by the owner
   2026-08-12: the zebra suite had been messaging his phone on every run (a
   VETOED alert for TESTCO, the fixture symbol). `bcs/` had no conftest at all,
   so its only protection was whatever each file happened to patch for itself
   — the arrangement that failed next door. Blocked at the NETWORK call rather
   than at the wrapper, so a future sender or a copy-pasted `requests.post`
   cannot slip past it, and it RAISES: a silent stub would let "we never send
   anything" pass as healthy.
2. **No production order journal** (`_journal_to_tmp`) — that file is the
   evidence of what this engine intended to trade; a test row in it is a lie
   about a real order.
3. **No production monitor log** (`_monitor_logs_to_tmp`) — the digest parses
   those, and the digest is what the arming decision is read from.
4. **The vetting switch is PINNED, not read** (`_pinned_vet_flag`). The
   2026-08-13 incident: `cfg.VET_ENABLED` is resolved at import from the
   machine's config, so the suite's answer depended on the box it ran on. It
   pins a DEFAULT rather than railing a MISTAKE, so unlike its neighbours it
   stays overridable per test.
5. **No writes under the real `logs/`** (`_no_production_writes`) — the
   backstop for a path nobody thought to redirect, stated as a location rather
   than as a list of files.
6. **A clean quote cache per test** (`_fresh_quote_cache`) — a cache that
   survives between tests makes the second one measure the first.

Two rails from `zebra/tests/conftest.py` are deliberately NOT here: the spawn
block and the production-path redirect for the vet's own files. The vet flag
above is pinned False, and the exit gate short-circuits on it before reaching
any spawn, so the route is currently unreachable — but it REOPENS the moment a
test here sets that flag True. Adding an unrequested autouse rail to the
live-money package wants its own review; until then this paragraph is the
warning. See N7 in the work order and
`[[feedback_tests_must_not_touch_production]]` — this class of escape has
happened four times.
"""
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))


class RealTelegramAttempted(BaseException):
    """Deliberately a BaseException, not an Exception.

    The production senders are wrapped in `except Exception` — correctly, since
    a Telegram failure must never make an agent think its verdict did not land.
    That same handler swallows a plain-Exception rail and logs it as a warning,
    so the suite passes, nobody sees it, and the messages keep arriving. A test
    rail that production error handling can catch is not a rail.
    """


@pytest.fixture(autouse=True)
def _no_telegram_http(monkeypatch):
    import requests
    real_post = requests.post

    def guarded(url, *a, **k):
        if 'api.telegram.org' in str(url):
            raise RealTelegramAttempted(
                'a test tried to send a REAL Telegram: %s' % str(url)[:60])
        return real_post(url, *a, **k)

    monkeypatch.setattr(requests, 'post', guarded)


# ── No test may write into the REAL logs/ directory ─────────────────────────
#
# Added 2026-08-24 after one of my own adversarial tests called
# `monkeypatch.undo()` mid-test. That undoes EVERY patch the fixture made,
# including the redirection of LOCAL_TRADES_FILE — so the next write went to
# the live book. It put a junk field on three real closed trades and injected a
# fake OPEN position into bear_put_trades.json. Local only (Drive was disabled
# and update_trade_fields never uploads), but an open record in a live book is
# exactly what the monitor acts on.
#
# The Telegram rail above existed because this had already happened twice with
# a different output channel. The lesson was recorded and the FILESYSTEM was
# left unrailed. Rail the channel, not the incident.
#
# Railed at the syscalls the stores actually use, not at the store API, so a
# future writer — a new sidecar, a log file, a lock — cannot slip past by not
# going through TradeStore.

REAL_LOGS = (HELPER / 'logs').resolve()


class ProductionWriteAttempted(BaseException):
    """BaseException on purpose.

    `_write_high_water` and `_flag_corruption` both swallow Exception by
    design — a trade must not fail to save because a sidecar could not be
    written. A plain-Exception rail would be caught by exactly those handlers,
    logged as a warning, and the suite would stay green while the writes kept
    landing. That is how the Telegram version of this survived two fixes.
    """


def _under_real_logs(path) -> bool:
    try:
        p = Path(path)
        p = p if p.is_absolute() else Path.cwd() / p
        return REAL_LOGS in p.resolve().parents
    except Exception:
        return False


def _forbid(path, how):
    if _under_real_logs(path):
        raise ProductionWriteAttempted(
            f'a test tried to {how} inside the real logs/ directory: {path}. '
            f'Redirect the store with the `book` fixture, and never call '
            f'monkeypatch.undo() in a test that relies on it.')


@pytest.fixture(autouse=True)
def _journal_to_tmp(tmp_path):
    """Point the order journal at a throwaway directory, for EVERY test.

    `place_limit_order` journals every order it intends, dry run and live
    alike, into `logs/order_intents_<date>.jsonl`. Without this rail any test
    that reaches the order path writes into the real logs/ -- which the
    production-write rail below correctly refuses, so the whole B10/B11 family
    went red the moment the journal was wired in.

    Autouse and at the SOURCE, not opt-in per test: a rail a test has to
    remember is a rail that catches nothing. Its own private MonkeyPatch, for
    the same reason `_no_production_writes` has one -- `monkeypatch.undo()`
    inside a test must not be able to switch it off.
    """
    from bcs import order_journal

    mp = pytest.MonkeyPatch()
    d = tmp_path / 'journal_logs'
    d.mkdir(exist_ok=True)
    mp.setattr(order_journal, 'LOG_DIR', d)
    yield d
    mp.undo()


@pytest.fixture(autouse=True)
def _monitor_logs_to_tmp(tmp_path):
    """Point `spread_monitor.LOG_DIR` at a throwaway directory, for EVERY test.

    Second instance of `_journal_to_tmp` above, and for the same reason: H4
    added a per-poll heartbeat file (`logs/exit_engine_heartbeat.json`) that
    `monitor_all` writes on every path including the empty-book one, so the
    replay family went red against the production-write rail the moment it was
    wired in. `replay.py` stubs `set_log_file`, which covered the session log
    but not a second artifact in the same directory.

    Redirecting the DIRECTORY rather than stubbing the writer, so the next
    file written beside it is covered without anyone remembering — the whole
    lesson of `feedback_the_copy_you_did_not_open`. It is strictly safer than
    the status quo: with this in place no test can write the real session log
    either.

    A test that wants to observe the heartbeat repoints `sm.LOG_DIR` itself; a
    test-body monkeypatch runs after fixtures and wins.
    """
    from bcs import spread_monitor

    mp = pytest.MonkeyPatch()
    d = tmp_path / 'monitor_logs'
    d.mkdir(exist_ok=True)
    mp.setattr(spread_monitor, 'LOG_DIR', d)
    yield d
    mp.undo()


@pytest.fixture(autouse=True)
def _pinned_vet_flag(monkeypatch):
    """This suite must not read the MACHINE's live vetting switch either.

    `zebra/tests/conftest.py` has had this rail since 2026-08-13, when five
    tests failed on the Pi and passed on the dev box on the same commit:
    `cfg.VET_ENABLED` is resolved from config at import, the Pi had vetting ON
    and the dev box had no vet key at all, so the gated exits under test simply
    never fired there. A suite whose result depends on the config of the box it
    runs on cannot certify a deploy — which is exactly what it is used for.

    `bcs/` had no equivalent, and it reaches the same flag: `bcs/exit_vet.py`
    calls `zebra.monitor._exit_cleared` -> `zebra.vet.exit_gate`, whose FIRST
    line is `if not cfg.VET_ENABLED: return 'proceed'`. With the flag on, that
    call instead writes vet markers into the store under test and can spawn a
    Claude agent. So every `_close_spread_inner` test on a `_store_type:
    'zebra'` trade has been taking whichever branch the box happened to be
    configured for.

    Latent until 2026-08-27, when H3 moved the switch into tracked config and
    flipped the CODE default from False to True so it would be auditable from
    git. The resolved value on a box with no overlay key went False -> True
    that day, silently, for these tests only. The suite is green either way
    today; a rail on one side of a copied pair and not the other is the shape
    that has produced six separate bugs in this repo, and the fix is the rail,
    not the audit that found it green this time.

    Pinned FALSE for the same reason zebra pins it there: what most tests here
    want to assert is what the DETERMINISTIC engine does. Deliberately on the
    shared `monkeypatch` — unlike `_journal_to_tmp` and `_monitor_logs_to_tmp`
    above, which own private MonkeyPatches so a test cannot switch them off.
    Those two rail against a mistake; this one pins a DEFAULT, and a test that
    genuinely wants to exercise a vet path must be able to say so. Fixtures
    and test-body monkeypatches both run after this one and win, which is how
    `zebra/tests/test_exit_vet.py` and its 14 siblings set it True.

    This does NOT change the resolved production value; the config is
    untouched. Reproduce either state with `ZEBRA_VET_ENABLED=1 pytest` (env
    wins over the file, see `zebra/config.py`).
    """
    from zebra import config as cfg
    monkeypatch.setattr(cfg, 'VET_ENABLED', False)


@pytest.fixture(autouse=True)
def _no_production_writes():
    """Deliberately does NOT take the shared `monkeypatch` fixture.

    The first version did, and its own proof-test failed: `monkeypatch.undo()`
    reverts every patch on that instance, so the rail was removed by exactly
    the call it exists to catch. A guard that a test can switch off by making
    the mistake is not a guard. It owns a private MonkeyPatch instead, which
    nothing in a test can reach.
    """
    import builtins
    import os
    import tempfile

    monkeypatch = pytest.MonkeyPatch()

    real_open, real_replace = builtins.open, os.replace
    real_rename, real_mkstemp = os.rename, tempfile.mkstemp
    real_osopen = os.open
    real_wtext, real_prename = Path.write_text, Path.rename

    def open_(file, mode='r', *a, **k):
        if any(c in mode for c in 'wxa+'):
            _forbid(file, 'open for writing')
        return real_open(file, mode, *a, **k)

    def osopen_(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
            _forbid(path, 'os.open for writing')
        return real_osopen(path, flags, *a, **k)

    def mkstemp_(*a, **k):
        _forbid(k.get('dir') or (a[2] if len(a) > 2 else '.'), 'mkstemp into')
        return real_mkstemp(*a, **k)

    monkeypatch.setattr(builtins, 'open', open_)
    monkeypatch.setattr(os, 'open', osopen_)
    monkeypatch.setattr(os, 'replace',
                        lambda s, d: (_forbid(d, 'os.replace onto'), real_replace(s, d))[1])
    monkeypatch.setattr(os, 'rename',
                        lambda s, d: (_forbid(d, 'os.rename onto'), real_rename(s, d))[1])
    monkeypatch.setattr(tempfile, 'mkstemp', mkstemp_)
    monkeypatch.setattr(Path, 'write_text',
                        lambda self, *a, **k: (_forbid(self, 'Path.write_text'),
                                               real_wtext(self, *a, **k))[1])
    monkeypatch.setattr(Path, 'rename',
                        lambda self, t: (_forbid(t, 'Path.rename onto'),
                                         real_prename(self, t))[1])
    try:
        yield
    finally:
        monkeypatch.undo()


# ── The three money books, as one list ──────────────────────────────────────
#
# `bcs`, `fallen_hero` and `bear_put` are near-verbatim copies of one another,
# so a fix applied to one and not the others is the DEFAULT failure mode here,
# not an unlikely one — B11 and B10 each shipped an untested `fallen_hero` twin
# because the BCS half was written first. Keeping the roster in conftest means
# every store-level test file iterates the same list, and a fourth book is
# covered by all of them the moment it is added here.

import importlib
import json
from types import SimpleNamespace

NO_DRIVE = {'google_drive': {'enabled': False}}

#: (module, class, file stem, a payload `add_trade` accepts)
BOOKS = [
    ('bcs.trade_store', 'TradeStore', 'bcs_trades', dict(
        stock='TESTCO', long_symbol='TESTCO26SEP1340CE',
        short_symbol='TESTCO26SEP1390CE', spot_symbol='NSE:TESTCO',
        exchange='NFO', quantity=700, lot_size=700,
        entry_long_price=21.20, entry_short_price=7.65, net_debit=13.55,
        spread_width=50, target_spot=1435.0, sl_spot=1319.0, sl_spread=6.78,
        expiry='2026-09-29')),
    ('fallen_hero.trade_store', 'FallenHeroStore', 'fallen_hero_trades', dict(
        stock='TESTCO', long_put_symbol='TESTCO26SEP2550PE',
        short_put_symbol='TESTCO26SEP2600PE',
        short_call_symbol='TESTCO26SEP3000CE', spot_symbol='NSE:TESTCO',
        exchange='NFO', quantity=400, lot_size=400,
        entry_long_put_price=122.85, entry_short_put_price=142.00,
        entry_short_call_price=78.60,
        long_put_strike=2550, short_put_strike=2600, short_call_strike=3000,
        put_spread_width=50, put_spread_credit=19.15, call_credit=78.60,
        total_credit=97.75, breakeven=3097.75, sl_spot=2850.0,
        entry_date='2026-08-24', entry_spot=2692.50, expiry='2026-09-29')),
    ('bear_put.trade_store', 'BearPutStore', 'bear_put_trades', dict(
        stock='TESTCO', long_symbol='TESTCO26SEP1400PE',
        short_symbol='TESTCO26SEP1340PE', spot_symbol='NSE:TESTCO',
        exchange='NFO', quantity=700, lot_size=700,
        entry_long_price=40.00, entry_short_price=26.45, net_debit=13.55,
        spread_width=60, target_spot=1330.0, sl_spot=1445.0, sl_spread=6.78,
        expiry='2026-09-29')),
]
BOOK_IDS = [b[0].split('.')[0] for b in BOOKS]


def seed_trades(ids):
    """Trades minimal enough for `_migrate_trades` yet distinct by id."""
    return [{'id': i, 'status': 'open', 'version': 1, 'quantity': 700,
             'lot_size': 700, 'lots': 1, 'stock': f'S{i}'} for i in ids]


@pytest.fixture(params=BOOKS, ids=BOOK_IDS)
def book(request, tmp_path, monkeypatch):
    """One money store, redirected entirely into tmp_path.

    Every path the store touches is a module global resolved at CALL time —
    data, lock, and (since B7) both sidecars, which hang off the data path. So
    this one redirect covers all four, and a fifth would fail loudly here
    rather than quietly writing into the real logs/ directory.
    """
    modname, clsname, stem, payload = request.param
    mod = importlib.import_module(modname)
    data = tmp_path / f'{stem}.json'
    lock = tmp_path / f'{stem}.lock'
    monkeypatch.setattr(mod, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(mod, 'LOCAL_TRADES_FILE', data)
    monkeypatch.setattr(mod, 'LOCK_FILE', lock)

    cls = getattr(mod, clsname)
    monkeypatch.setattr(cls, 'LOCK_TIMEOUT', 0.4, raising=False)

    def make(seed_ids=(1, 2), raw=None):
        if raw is not None:
            data.write_text(raw, encoding='utf-8')
        else:
            data.write_text(json.dumps(seed_trades(seed_ids)), encoding='utf-8')
        s = cls(config=dict(NO_DRIVE))
        s.initialize()
        return s

    return SimpleNamespace(
        mod=mod, cls=cls, make=make, data=data, lock=lock, stem=stem,
        payload=dict(payload), tmp=tmp_path,
        read=lambda: json.loads(data.read_text(encoding='utf-8')))


# ── The per-poll quote cache is MODULE state; no test may inherit it ────────
#
# Added 2026-08-28 with the F7 batching. `spread_monitor._quote_cache` /
# `_ltp_cache` live for a poll in production (`prefetch_book` drops them at the
# top of every one) but for the whole PROCESS in a test run — so without this
# rail, `test_a_one_sided_book_reports_zero_not_a_crash` read a book left
# behind by an earlier file and asserted against a price from another test's
# fixture. It failed loudly, which was luck: the same leak could just as easily
# have made a guard test PASS on a quote it never fetched.
#
# The rate-limit cooldown is reset too. It is wall-clock state that a test
# simulating a 429 would otherwise leave armed for every test that runs after
# it in the same process, silently turning their quote calls into refusals.
@pytest.fixture(autouse=True)
def _fresh_quote_cache():
    from bcs import spread_monitor as _sm
    _sm.reset_quote_cache()
    _sm.reset_quote_cooldown()
    yield
    _sm.reset_quote_cache()
    _sm.reset_quote_cooldown()

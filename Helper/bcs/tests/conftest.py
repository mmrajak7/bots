"""Test-wide safety rail for the LIVE-money package.

No test here may send a real Telegram. Reported by the owner 2026-08-12: the
zebra suite had been messaging his phone on every run (a VETOED alert for
TESTCO, the fixture symbol). `bcs/` had no conftest at all, so its only
protection was whatever each file happened to patch for itself — which is the
arrangement that failed next door.

Blocked at the NETWORK call rather than at the wrapper, so a future sender or a
copy-pasted requests.post cannot slip past it. It RAISES: a silent stub would
let "we never send anything" pass as healthy.
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

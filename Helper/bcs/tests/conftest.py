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

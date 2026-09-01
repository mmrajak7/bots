"""No test in this package may write inside the real `logs/` directory.

WHY THIS EXISTS, AND WHY IT IS DATED 2026-08-31
-----------------------------------------------
`bcs/tests/conftest.py` has carried this rail for a while, stated there as a
LOCATION rather than as a list of files -- "the backstop for a path nobody
thought to redirect". `common/tests/` had no conftest at all, and `common/`
is precisely where the SHARED store base lives: a test here instantiates
`TradeStore`, `BearPutStore` and `FallenHeroStore` directly, and every one of
them resolves its own real path unless something moves it.

It escaped exactly that way while `test_spread_store_hardenings.py` was being
written. One line --

    store.update_trade_fields(1, some_new_field_nobody_listed=7)

-- ran `_mutate`, which refreshes against the real book on disk and then
saves. The synthetic field landed in all three LIVE-MONEY books
(`logs/bcs_trades.json`, `logs/bear_put_trades.json`,
`logs/fallen_hero_trades.json`) and left a `merge_conflict` marker behind. The
trade data survived -- the merge kept the disk copy and no version was bumped,
so the damage was one stray key, removed by hand -- but it was a test writing
to the money books, which is the fifth instance of this class.
[[feedback_tests_must_not_touch_production]]

The per-file fixture that redirects paths is the fix; this is the backstop for
the next file that forgets one. Both, deliberately: the rail catches what the
author did not think of, and it must never be the only thing standing there.

WHAT IT DOES NOT COVER
----------------------
Telegram and CLI spawns. `common/` reaches neither today, and adding
unrequested autouse rails to a package that does not need them makes the real
ones harder to find. If a module here ever grows one, copy the rail from
`bcs/tests/conftest.py` rather than inventing a second version.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest common/tests -q
"""
import builtins
import os
import tempfile
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
REAL_LOGS = (HELPER / 'logs').resolve()


class ProductionWriteAttempted(BaseException):
    """BaseException on purpose, for the same reason as its twin in `bcs/`.

    `_write_high_water` and `_flag_corruption` both swallow `Exception` by
    design -- a trade must not fail to save because a sidecar could not be
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
            'a test tried to %s inside the real logs/ directory: %s. '
            'Redirect the store: monkeypatch LOG_DIR, LOCAL_TRADES_FILE and '
            'LOCK_FILE on the store module to a tmp_path.' % (how, path))


@pytest.fixture(autouse=True)
def _no_production_writes():
    """Owns a PRIVATE MonkeyPatch, deliberately.

    Taking the shared `monkeypatch` fixture would let `monkeypatch.undo()` --
    the very mistake this catches -- remove the rail. A guard a test can
    switch off by making the mistake is not a guard.
    """
    monkeypatch = pytest.MonkeyPatch()

    real_open, real_replace = builtins.open, os.replace
    real_rename, real_mkstemp = os.rename, tempfile.mkstemp
    real_osopen = os.open
    real_wtext, real_prename = Path.write_text, Path.rename
    real_wbytes = Path.write_bytes

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
                        lambda s, d: (_forbid(d, 'os.replace onto'),
                                      real_replace(s, d))[1])
    monkeypatch.setattr(os, 'rename',
                        lambda s, d: (_forbid(d, 'os.rename onto'),
                                      real_rename(s, d))[1])
    monkeypatch.setattr(tempfile, 'mkstemp', mkstemp_)
    monkeypatch.setattr(Path, 'write_text',
                        lambda self, *a, **k: (_forbid(self, 'Path.write_text'),
                                               real_wtext(self, *a, **k))[1])
    monkeypatch.setattr(Path, 'write_bytes',
                        lambda self, *a, **k: (_forbid(self, 'Path.write_bytes'),
                                               real_wbytes(self, *a, **k))[1])
    monkeypatch.setattr(Path, 'rename',
                        lambda self, t: (_forbid(t, 'Path.rename onto'),
                                         real_prename(self, t))[1])
    try:
        yield
    finally:
        monkeypatch.undo()

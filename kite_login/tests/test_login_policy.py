"""Only kite_login/core.py may log in to Kite (owner, 2026-11-27).

The endpoint names are the shape that matters: any code that logs in to Kite
must send one of them. Legacy holders are listed with what retires them. A NEW
file containing an endpoint fails this test; so does a listed file once it
stops holding one (the list may only shrink).
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# /api/login = step one of EVERY Kite login, API or web enctoken (the 2nd catches
# a web-only login that never calls /session/token).
ENDPOINTS = ('/api/login', '/api/twofa', '/session/token', 'generate_session(')
OWNER = 'kite_login/core.py'

#: path -> RETIRES WHEN
LEGACY = {
    'SNAIL/src/api/kite_auth.py': 'SNAIL folder deleted (cron + service removed 2026-11-27 cutover)',
    'FIFTY/src/utils/token_generator.py': 'FIFTY folder deleted (daemon disabled at cutover)',
    'FIFTY/kite_trade.py': 'FIFTY folder deleted',
    'Bouncer/scripts/kite_auth.py': 'Bouncer folder deleted (no cron since 2026)',
    'CROCODILE/generate_kite_token.py': 'CROCODILE moved onto the shared token',
    'CROCODILE/kite_trade.py': 'CROCODILE moved onto the shared token (web enctoken login)',
}
# ETF_PI/kite_trade.py retired 2026-11-27: tracker.py / extract_data.py read the
# shared token through ETF_PI/shared_kite.py. ETF_PI must never log in again.

SKIP_DIRS = {'.git', '__pycache__', 'venv', '.venv', 'node_modules', 'Archive'}


def _files(root: Path):
    """TRACKED .py/.sh files (same answer on laptop and Pi); a non-repo tmp dir is walked."""
    try:
        out = subprocess.run(['git', 'ls-files', '*.py', '*.sh'], cwd=str(root),
                             capture_output=True, text=True, check=True).stdout.split()
        if out:
            return [root / f for f in out]
    except (OSError, subprocess.CalledProcessError):
        pass
    return list(root.rglob('*.py')) + list(root.rglob('*.sh'))


def holders(root: Path):
    out = set()
    for p in _files(root):
        rel = p.relative_to(root).as_posix()
        if set(rel.split('/')) & SKIP_DIRS or rel.startswith('kite_login/tests/'):
            continue
        try:
            text = p.read_text(encoding='utf-8', errors='ignore')
        except OSError:
            continue
        if any(e in text for e in ENDPOINTS):
            out.add(rel)
    return out


def test_only_the_package_and_listed_legacy_files_log_in():
    found = holders(ROOT)
    assert OWNER in found, 'the package itself must hold the login'
    stray = sorted(found - {OWNER} - set(LEGACY))
    assert not stray, ('Kite login code outside kite_login/core.py: %s -- read the shared '
                       'token file instead (python -m kite_login makes it)' % stray)


def test_the_legacy_list_only_shrinks():
    gone = sorted(set(LEGACY) - holders(ROOT))
    assert not gone, '%s no longer log in -- remove them from LEGACY' % gone


def test_negative_control_a_planted_login_is_caught(tmp_path):
    (tmp_path / 'kite_login').mkdir()
    (tmp_path / 'kite_login' / 'core.py').write_text("U = BASE + '/api/twofa'\n")
    (tmp_path / 'somebot').mkdir()
    (tmp_path / 'somebot' / 'auth.py').write_text("kite.generate_session(rt, secret)\n")
    found = holders(tmp_path)
    assert {'somebot/auth.py', 'kite_login/core.py'} <= found

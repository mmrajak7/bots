"""Only kotak_neo/session.py may call NEO's login endpoints (owner, 2026-11-27).

Two bots that each log in can kill each other's session; that is the
in-fighting the shared package exists to end. The literal endpoint names are
the shape that matters: any code that logs in to NEO must send one of them.

Legacy holders are listed with what retires them. A NEW file containing an
endpoint fails this test; so does a listed file once its retirement has
happened and someone forgot to take it off the list (the list may only shrink).
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ENDPOINTS = ('tradeApiLogin', 'tradeApiValidate', 'totp_login', 'totp_validate',
             'neo_api_client', 'session_2fa')
OWNER = 'kotak_neo/session.py'

#: path -> RETIRES WHEN
LEGACY = {
    'Strategies/NIFTY_BTST/nifty_btst/neo_client.py':
        'K5: BTST reads the shared session; its login code is deleted',
    'Scalper/core/session_manager.py': 'Scalper retired or moved onto kotak_neo',
    'Scalper/test_api.py': 'Scalper retired or moved onto kotak_neo',
}

SKIP_DIRS = {'.git', '__pycache__', 'venv', '.venv', 'node_modules', 'Archive'}


def _files(root: Path):
    """TRACKED .py/.sh files: the same answer on the laptop and the Pi, where
    untracked research folders differ. A tree that is not a git repo (the
    negative control's tmp dir) is walked instead."""
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
        parts = set(rel.split('/'))
        if parts & SKIP_DIRS or rel.startswith('kotak_neo/tests/'):
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
    assert not stray, ('NEO login code outside kotak_neo/session.py: %s -- use '
                       'kotak_neo.session.refresh() instead' % stray)


def test_the_legacy_list_only_shrinks():
    found = holders(ROOT)
    gone = sorted(set(LEGACY) - found)
    assert not gone, ('%s no longer log in -- remove them from LEGACY' % gone)


def test_negative_control_a_planted_login_is_caught(tmp_path):
    (tmp_path / 'kotak_neo').mkdir()
    (tmp_path / 'kotak_neo' / 'session.py').write_text("P = '/login/1.0/tradeApiLogin'\n")
    (tmp_path / 'somebot').mkdir()
    (tmp_path / 'somebot' / 'client.py').write_text("URL = BASE + '/login/1.0/tradeApiValidate'\n")
    found = holders(tmp_path)
    assert 'somebot/client.py' in found and 'kotak_neo/session.py' in found

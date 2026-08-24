"""Concurrent-writer safety for the zebra trade store.

As of 2026-08-10 the store has two writer processes: the zebra cron and the
Claude vetting/review cron. The failure this guards against is SILENT — no
exception, no corrupt file, a trade simply ceases to exist:

    zebra reads  [A,B] ; claude reads [A,B]
    zebra writes [A,B,C] ; claude writes [A,B,D]   -> C is gone

These tests spawn REAL subprocesses (not threads) because the lock is a
cross-process OS primitive; threads in one interpreter would not exercise it.

Run:  cd Helper && python -m pytest zebra/tests/test_store_locking.py -v
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from common import filelock                      # noqa: E402


# ── the lock primitive itself ────────────────────────────────────────────
def test_lock_is_exclusive_across_processes(tmp_path):
    """A second process must NOT be able to take a held lock."""
    lock = tmp_path / 'x.lock'
    probe = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(HELPER)!r})
        from common import filelock
        try:
            with filelock.exclusive({str(lock)!r}, timeout=0.3):
                print("ACQUIRED")
        except filelock.LockTimeout:
            print("BLOCKED")
    """)
    with filelock.exclusive(lock):
        out = subprocess.run([sys.executable, '-c', probe],
                             capture_output=True, text=True, timeout=30)
    assert 'BLOCKED' in out.stdout, out.stdout + out.stderr


def test_lock_is_released_after_the_block(tmp_path):
    lock = tmp_path / 'x.lock'
    with filelock.exclusive(lock):
        pass
    with filelock.exclusive(lock, timeout=1):   # must not raise
        pass


def test_lock_released_when_holder_dies(tmp_path):
    """A crashed cron must not strand the lock — the OS releases on exit.
    This is why we use an advisory OS lock rather than a PID lockfile."""
    lock = tmp_path / 'x.lock'
    killer = textwrap.dedent(f"""
        import sys, os
        sys.path.insert(0, {str(HELPER)!r})
        from common import filelock
        with filelock.exclusive({str(lock)!r}):
            os._exit(1)          # die hard, holding the lock
    """)
    subprocess.run([sys.executable, '-c', killer], capture_output=True, timeout=30)
    with filelock.exclusive(lock, timeout=2):   # must be free again
        pass


def test_timeout_raises_rather_than_writing_anyway(tmp_path):
    """LockTimeout must propagate. Degrading to 'write without the lock'
    would reintroduce exactly the corruption this module prevents."""
    lock = tmp_path / 'x.lock'
    with filelock.exclusive(lock):
        with pytest.raises(filelock.LockTimeout):
            with filelock.exclusive(lock, timeout=0.2):
                pass


# ── the store under real concurrency ─────────────────────────────────────
WRITER = textwrap.dedent("""
    import sys, os
    sys.path.insert(0, {helper!r})
    os.environ['ZEBRA_TEST_LOG_DIR'] = {logdir!r}
    from zebra import config as cfg
    from pathlib import Path
    cfg.LOG_DIR = Path({logdir!r})
    cfg.LOCAL_FILE = Path({logdir!r}) / 'zebra_trades.json'
    cfg.LOCK_FILE = Path({logdir!r}) / 'zebra_trades.lock'
    from zebra.trade_store import ZebraStore
    s = ZebraStore(config={{}})
    s._load_local()
    for i in range({n}):
        s.add_signal({{
            'stock': 'W{tag}S%d' % i, 'timeframe': 'weekly', 'direction': 'CE',
            'st_value': 100.0, 'st_direction': 'UP',
            'signal_price': 96.0, 'signal_gap_pct': 4.0,
        }})
""")


def _spawn_writers(tmp_path, procs: int, per_proc: int):
    (tmp_path / 'zebra_trades.json').write_text('[]')
    running = [
        subprocess.Popen(
            [sys.executable, '-c',
             WRITER.format(helper=str(HELPER), logdir=str(tmp_path),
                           n=per_proc, tag=chr(ord('A') + p))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for p in range(procs)
    ]
    for p in running:
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
    return json.loads((tmp_path / 'zebra_trades.json').read_text())


def test_concurrent_writers_lose_nothing(tmp_path):
    """THE test. 4 processes x 10 signals = 40 trades must all survive.
    Without the lock this loses writes essentially every run."""
    procs, per = 4, 10
    trades = _spawn_writers(tmp_path, procs, per)
    assert len(trades) == procs * per, (
        f"LOST WRITES: expected {procs*per}, got {len(trades)}")


def test_concurrent_writers_never_duplicate_an_id(tmp_path):
    """_next_id() must run inside the lock, or two processes hand the same id
    to two different trades and one is later silently overwritten on merge."""
    trades = _spawn_writers(tmp_path, 4, 10)
    ids = [t['id'] for t in trades]
    assert len(ids) == len(set(ids)), f"DUPLICATE IDS: {sorted(ids)}"


def test_file_is_valid_json_after_concurrent_writes(tmp_path):
    """Atomic tmp+rename should already guarantee this, but a torn file would
    be catastrophic and silent, so assert it explicitly."""
    trades = _spawn_writers(tmp_path, 4, 5)
    assert isinstance(trades, list) and all('id' in t for t in trades)


# ── exception paths inside _mutate ───────────────────────────────────────
@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """In-process store pointed at a temp dir (cfg is read at call time)."""
    from zebra import config as cfg
    from zebra.trade_store import ZebraStore
    monkeypatch.setattr(cfg, 'LOG_DIR', tmp_path)
    monkeypatch.setattr(cfg, 'LOCAL_FILE', tmp_path / 'zebra_trades.json')
    monkeypatch.setattr(cfg, 'LOCK_FILE', tmp_path / 'zebra_trades.lock')
    return ZebraStore(config={})


SIGNAL = {
    'stock': 'TESTCO', 'timeframe': 'weekly', 'direction': 'CE',
    'st_value': 100.0, 'st_direction': 'UP',
    'signal_price': 96.0, 'signal_gap_pct': 4.0,
}


def test_dedup_error_releases_lock_and_leaves_disk_clean(local_store, tmp_path):
    """add_signal's dedup ValueError must not save, must not leave the lock
    held, and must leave the store usable for the next (valid) write."""
    from zebra import config as cfg
    local_store.add_signal(dict(SIGNAL))
    with pytest.raises(ValueError, match='already open'):
        local_store.add_signal(dict(SIGNAL))
    on_disk = json.loads(cfg.LOCAL_FILE.read_text())
    assert len(on_disk) == 1
    # Lock released + state sane: a different signal still goes through
    other = dict(SIGNAL, stock='OTHERCO')
    t = local_store.add_signal(other)
    assert t['id'] == 2
    assert len(json.loads(cfg.LOCAL_FILE.read_text())) == 2


def test_partial_mutation_is_rolled_back_on_exception(local_store, tmp_path):
    """_apply_entry sets status='entered' BEFORE casting the strike fields.
    If a later cast raises, the half-entered trade must not linger in memory
    (get_entered() would report a position that was never persisted) and the
    disk file must be untouched."""
    from zebra import config as cfg
    local_store.add_signal(dict(SIGNAL))
    bad_entry = {
        'long_strike': 'garbage',       # float() blows up AFTER status is set
        'short_strike': 110.0,
        'long_symbol': 'TESTCO26AUG90CE', 'short_symbol': 'TESTCO26AUG110CE',
        'debit': 5.0, 'lot_size': 100, 'lots': 1, 'expiry': '2026-09-24',
    }
    with pytest.raises(ValueError):
        local_store.mark_entered(1, bad_entry)
    # Memory rolled back — no phantom 'entered' position
    assert local_store.find(1)['status'] == 'watching'
    assert local_store.get_entered() == []
    # Disk never saw the partial mutation
    assert json.loads(cfg.LOCAL_FILE.read_text())[0]['status'] == 'watching'
    # Store still fully usable: the same entry with valid data succeeds
    good_entry = dict(bad_entry, long_strike=90.0)
    t = local_store.mark_entered(1, good_entry)
    assert t['status'] == 'entered' and t['long_strike'] == 90.0


# ── consume-once flags under real concurrency ────────────────────────────
FLAG_RACER = textwrap.dedent("""
    import sys, os
    sys.path.insert(0, {helper!r})
    from zebra import config as cfg
    from pathlib import Path
    cfg.LOG_DIR = Path({logdir!r})
    cfg.LOCAL_FILE = Path({logdir!r}) / 'zebra_trades.json'
    cfg.LOCK_FILE = Path({logdir!r}) / 'zebra_trades.lock'
    from zebra.trade_store import ZebraStore
    s = ZebraStore(config={{}})
    s._load_local()
    print(s.set_alert_flag(1, 'tp'))
""")


def test_alert_flag_fires_exactly_once_across_processes(tmp_path):
    """The ICICI-bug shape: two processes racing a consume-once flag must not
    both observe it unset. Exactly ONE racer may see True."""
    (tmp_path / 'zebra_trades.json').write_text(json.dumps([{
        'id': 1, 'version': 1, 'status': 'entered', 'stock': 'TESTCO',
    }]))
    procs = [
        subprocess.Popen(
            [sys.executable, '-c',
             FLAG_RACER.format(helper=str(HELPER), logdir=str(tmp_path))],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    outs = []
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        outs.append(out.strip())
    assert outs.count('True') == 1, f"flag consumed {outs.count('True')}x: {outs}"


# ── the Drive-sync path (the third writer) ───────────────────────────────
SYNCER = textwrap.dedent("""
    import sys, time, types
    sys.path.insert(0, {helper!r})
    from zebra import config as cfg
    from pathlib import Path
    cfg.LOG_DIR = Path({logdir!r})
    cfg.LOCAL_FILE = Path({logdir!r}) / 'zebra_trades.json'
    cfg.LOCK_FILE = Path({logdir!r}) / 'zebra_trades.lock'
    # Fake Drive layer: empty remote, no-op upload. The point is the local
    # merge+save inside _sync_from_drive, not the network.
    fake = types.ModuleType('bcs.drive_store')
    fake.download_json = lambda svc, fid: []
    fake.upload_json = lambda svc, folder, name, data, fid: fid
    bcs_pkg = types.ModuleType('bcs')
    bcs_pkg.drive_store = fake
    sys.modules['bcs'] = bcs_pkg
    sys.modules['bcs.drive_store'] = fake
    from zebra.trade_store import ZebraStore
    s = ZebraStore(config={{'google_drive': {{'enabled': False, 'folder_id': 'x'}}}})
    s._drive_enabled = True
    s._drive_file_id = 'fake'
    deadline = time.time() + 25
    while time.time() < deadline:
        s._sync_from_drive()
        if len(s._trades) >= {expected}:
            break
""")


def test_drive_sync_never_clobbers_concurrent_writes(tmp_path):
    """_sync_from_drive is a read-modify-write on the shared file and runs
    every 5 minutes in the monitor loop. Unlocked, its save clobbers any trade
    the other cron writes between its read and its os.replace. The syncer here
    hammers that path while a writer adds trades; every write must survive."""
    n = 20
    (tmp_path / 'zebra_trades.json').write_text('[]')
    syncer = subprocess.Popen(
        [sys.executable, '-c',
         SYNCER.format(helper=str(HELPER), logdir=str(tmp_path), expected=n)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    writer = subprocess.Popen(
        [sys.executable, '-c',
         WRITER.format(helper=str(HELPER), logdir=str(tmp_path), n=n, tag='A')],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for p in (writer, syncer):
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
    trades = json.loads((tmp_path / 'zebra_trades.json').read_text())
    ids = [t['id'] for t in trades]
    assert len(trades) == n, f"SYNC CLOBBERED WRITES: expected {n}, got {len(trades)}"
    assert len(ids) == len(set(ids)), f"DUPLICATE IDS: {sorted(ids)}"

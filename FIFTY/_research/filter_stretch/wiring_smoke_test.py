"""Wiring parity test: every orchestrator task reachable from the DEAD cron path
(orchestrator.run) must also be reachable from the LIVE daemon path
(main._run_scheduled_tasks).

WHY THIS EXISTS
---------------
Production runs `main.py --daemon`. orchestrator.run() is the cron-mode entry
point, and the daemon's process lock makes it unreachable - it is dead code that
still LOOKS authoritative when you read it. Two tasks were written into run()
and silently never executed in production:

  * _update_regime   - breadth/regime gate, captured 0 sessions in 5 days live
  * _monitor_sl_hits - intraday SL-hit detection, never ran

Both were found by reading logs, not by any test. This test closes that hole:
add a task to run() without wiring it into the daemon and it fails here.

Static AST comparison - imports nothing, touches no network, no DB.
Run: cd FIFTY && python _research/filter_stretch/wiring_smoke_test.py
"""
import ast
import os
import sys

FIFTY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FIFTY_DIR)
os.chdir(FIFTY_DIR)

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS' if cond else 'FAIL') + f' | {name} | {detail}')


def _find_func(tree, name, cls=None):
    """Locate a top-level function, or a method inside class `cls`."""
    if cls is not None:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == cls:
                tree = node
                break
        else:
            return None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _parse(relpath):
    with open(os.path.join(FIFTY_DIR, relpath), encoding='utf-8') as fh:
        return ast.parse(fh.read())


def cron_tasks():
    """Underscore-prefixed self.X() calls made directly by orchestrator.run()."""
    fn = _find_func(_parse('src/core/orchestrator.py'), 'run', cls='Orchestrator')
    if fn is None:
        return None
    found = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if (isinstance(f, ast.Attribute)
                and isinstance(f.value, ast.Name) and f.value.id == 'self'
                and f.attr.startswith('_')):
            found.add(f.attr)
    return found


def daemon_tasks():
    """Orchestrator attrs referenced by main._run_scheduled_tasks.

    Counts BOTH `_safe_run("x", orchestrator._foo)` (passed as a value, so it is
    an Attribute node, not a Call) and any direct `orchestrator._foo()` call.
    """
    fn = _find_func(_parse('main.py'), '_run_scheduled_tasks')
    if fn is None:
        return None
    found = set()
    for node in ast.walk(fn):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name) and node.value.id == 'orchestrator'
                and node.attr.startswith('_')):
            found.add(node.attr)
    return found


# Tasks the daemon deliberately handles differently. Each needs a reason -
# an empty allowlist entry is how the next silent gap gets rubber-stamped.
DAEMON_HANDLES_ELSEWHERE = {
    '_process_telegram_callbacks':
        'daemon runs Telegram long-polling on a background thread and drains '
        'the action queue every ~1s (run_daemon -> _drain_action_queue), which '
        'is strictly faster than the 5-min scheduled tick',
}

cron = cron_tasks()
daemon = daemon_tasks()

check('W1 orchestrator.run() parsed', cron is not None, f'{len(cron or [])} task calls')
check('W2 _run_scheduled_tasks parsed', daemon is not None, f'{len(daemon or [])} task refs')

if cron and daemon:
    missing = {t for t in cron - daemon if t not in DAEMON_HANDLES_ELSEWHERE}
    check(
        'W3 no cron task is unreachable in the daemon',
        not missing,
        'OK' if not missing
        else f'NOT WIRED INTO main._run_scheduled_tasks: {sorted(missing)}',
    )

    # The two known regressions, pinned by name so a refactor cannot quietly
    # drop them again.
    check('W4 _update_regime wired', '_update_regime' in daemon)
    check('W5 _monitor_sl_hits wired', '_monitor_sl_hits' in daemon)

    # Keep the allowlist honest: an entry that is no longer a real divergence
    # (or was never a cron task at all) is stale and should be deleted.
    stale = [t for t in DAEMON_HANDLES_ELSEWHERE if t in daemon or t not in cron]
    check('W6 no stale allowlist entries', not stale, f'stale: {stale}' if stale else 'OK')

print(f'\n{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    print('FAILED: ' + ', '.join(FAIL))
sys.exit(1 if FAIL else 0)

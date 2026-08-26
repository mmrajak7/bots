"""Pre-flight for the config split. `python -m common.config_preflight`.

Run this on a box the FIRST time it pulls the tracked `config/*.defaults.json`
files, BEFORE letting anything restart. It changes nothing.

The one way the split can bite
------------------------------
The tracked defaults were derived by stripping secrets out of the *Windows*
config files. Every box keeps its own untracked `config/<name>.json`, and the
overlay wins, so a key present in BOTH is safe — this box's value is preserved.

The risk is a key present in the defaults and **absent from this box's
overlay**. Before the pull it did not exist here at all and the code fell back
to whatever its in-module default was. After the pull it exists, with the
value the Windows box happened to have. That is a silent behaviour change on a
machine that runs live, and it is invisible in a diff because the file that
changed is one that never existed here.

Output is safe to paste. It only ever prints values that came out of a TRACKED
file, and a tracked file is secret-free by construction (enforced by
`common/tests/test_layered_config.py`). Overlay-only keys — where every secret
lives — are counted, never shown.
"""
from __future__ import annotations

import json
import sys

from common.layered_config import CONFIG_DIR, DEFAULTS_SUFFIX


def _flatten(d, prefix=''):
    out = {}
    for k, v in (d or {}).items():
        here = f'{prefix}{k}'
        if isinstance(v, dict):
            out.update(_flatten(v, here + '.'))
        else:
            out[here] = v
    return out


def main() -> int:
    files = sorted(CONFIG_DIR.glob('*' + DEFAULTS_SUFFIX))
    if not files:
        print('No *%s files in %s.' % (DEFAULTS_SUFFIX, CONFIG_DIR))
        print('The pull has not landed, or you are in the wrong directory.')
        print('Run this from Helper/ after `git pull`.')
        return 2

    print('CONFIG SPLIT PRE-FLIGHT   (%d tracked defaults file(s))' % len(files))
    print('=' * 78)
    print('Nothing is modified by this script.')
    print()

    total_new = 0
    total_shadowed = 0
    no_overlay = []

    for f in files:
        name = f.name[:-len(DEFAULTS_SUFFIX)]
        overlay_path = CONFIG_DIR / (name + '.json')
        defaults = _flatten(json.loads(f.read_text(encoding='utf-8')))

        if not overlay_path.exists():
            no_overlay.append(name)
            continue

        try:
            overlay = _flatten(json.loads(overlay_path.read_text(encoding='utf-8')))
        except Exception as e:
            print('!! %s.json is unreadable (%s)' % (name, e))
            print('   The loader will fall back to the tracked layer ALONE, so')
            print('   every secret in it is missing. Fix this before restarting.')
            print()
            continue

        new = {k: v for k, v in defaults.items() if k not in overlay}
        shadowed = [k for k, v in defaults.items()
                    if k in overlay and overlay[k] != v]
        overlay_only = [k for k in overlay if k not in defaults]

        total_new += len(new)
        total_shadowed += len(shadowed)

        # An overlay holding NOTHING the defaults also hold is a TRIMMED
        # overlay -- secrets only, which is the end state the split is
        # heading for. On such a box every key reads as "new" and none of it
        # is a change: those values are simply where they now live. Only a
        # FAT overlay (one that still duplicates tracked keys) can hide the
        # failure this script is looking for.
        trimmed = not shadowed and not (set(defaults) & set(overlay))
        if trimmed:
            print('%-26s overlay is TRIMMED to %d secret key(s) -- expected, '
                  'no change' % (name, len(overlay_only)))
            total_new -= len(new)
            continue

        flag = '  <-- REVIEW' if new else ''
        print('%-26s new:%-3d kept:%-3d overlay-only:%-3d%s'
              % (name, len(new), len(shadowed), len(overlay_only), flag))
        for k in sorted(new):
            print('      NEW   %s = %r' % (k, new[k]))
        for k in sorted(shadowed):
            print('      kept  %s   (this box keeps its own value)' % k)

    print()
    if no_overlay:
        print('No overlay on this box for: %s' % ', '.join(no_overlay))
        print('  Those subsystems will run on the tracked defaults alone. That')
        print('  is correct for a fresh checkout and WRONG for a box that is')
        print('  supposed to have credentials -- check before restarting.')
        print()

    print('-' * 78)
    if total_new == 0:
        print('SAFE: on every FAT overlay, the tracked defaults add no key this')
        print('box did not already have. Nothing changes. Restart when you like.')
    else:
        print('%d key(s) marked NEW above will start applying on this box with' % total_new)
        print('the value shown, which came from the Windows machine. Read them.')
        print('If any is wrong for this box, put the right value in the')
        print('untracked config/<name>.json -- the overlay wins -- and re-run.')
    if total_shadowed:
        print()
        print('%d key(s) are "kept": this box\'s overlay holds a different value' % total_shadowed)
        print('and that value WINS. Nothing changes for them now -- but a future')
        print('edit to the tracked file will not take effect here until the')
        print('overlay is trimmed to secrets. The loader warns about these at')
        print('runtime too.')
    return 1 if total_new else 0


if __name__ == '__main__':
    raise SystemExit(main())

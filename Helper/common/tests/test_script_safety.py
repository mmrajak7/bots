"""A shell script that can destroy something must say so, or guard it.

WHY THIS EXISTS
---------------
2026-08-30 cost a whole open book and a morning of recovery, and the causal
chain was two shell scripts that each asserted a safety property nothing
enforced:

* **`zebra/deploy_server.sh`** ran an unguarded `zebra reset --confirm` under a
  header calling it "one-time hygiene" — and, three lines up, "Idempotent:
  re-running is safe". Re-running it force-closed six open cohort positions at
  -100%. Nothing made the "one-time" true.
* **`restart_services.sh`** contained `rm -f "$HELPER_DIR/restart_services.sh"`
  with the comment "Remove this script if it blocks git pull". The file was
  TRACKED, so deleting it left a permanent `D` in the working tree — which is
  precisely what blocks `git pull --rebase`. Its own workaround caused the
  problem it claimed to solve, forever, and that is why the pull failed on the
  morning of the incident.

Both were reviewed by a human, both read as safe, and both said so in prose.
Prose is not a guard. [[incident_deploy_reset_2026_08_30]]

THE RULE
--------
A tracked `.sh` containing a destructive verb must EITHER

  * gate it behind an explicit opt-in flag (`if [ "$1" = "--reset" ]`), OR
  * carry a `# SAFE-TO-RERUN:` line saying, in one sentence, why running it
    twice cannot destroy anything.

The declaration is deliberately a sentence somebody has to write rather than a
marker they can paste: the value is entirely in having thought about what a
second run does. Same design as `RETIRES WHEN:` on the source guards, which
has already been paid down once.

WHAT THIS DOES NOT CLAIM
------------------------
It cannot prove a script is safe. It only refuses the case where nobody
considered the question — which is the one that has actually cost money here.

Run:  cd Helper && python -m pytest common/tests/test_script_safety.py -v
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]

#: Verbs that can lose data, change the machine, or move money. Deliberately
#: broad: a false positive costs one sentence of documentation, a false
#: negative cost a book.
DESTRUCTIVE = {
    'reset --confirm': r'reset\s+--confirm',
    'rm -rf': r'\brm\s+-[a-zA-Z]*r[a-zA-Z]*f?\b',
    'rm -f': r'\brm\s+-[a-zA-Z]*f\b',
    # `crontab -` (install from stdin) or `crontab "$FILE"`. NOT `crontab -l`,
    # and not the word inside a message — a detector that cries wolf is one the
    # next reader learns to skip, which is the failure mode this whole file
    # exists to prevent. Both false positives below were real: `warn "no
    # crontab for $(whoami)"` and `ok "crontab backed up to $BACKUP"`.
    'crontab write': r'(?:^|[;&|]|\bthen\b|\bdo\b|\$\()\s*crontab\s+(?:-\s|-$|["\']?[$/])',
    '--force': r'--force\b',
    'git reset --hard': r'git\s+reset\s+--hard',
    'truncate/overwrite': r'>\s*/(?:etc|var|home)\S+',
}

DECLARATION = '# SAFE-TO-RERUN:'

#: An opt-in gate. Any of these near a destructive verb means the operator has
#: to ask for it, which is the other acceptable answer.
GUARDED = re.compile(
    r'if\s+\[\s*"?\$\{?1[:\-]?[^\]]*\]\s*;\s*then|'
    r'\[\s*"\$\{1:-\}"\s*=|read\s+-p|--i-know|--confirm-destructive',
)


def tracked_scripts():
    """Every `.sh` git actually carries. Untracked ones are the operator's
    scratch space and not this suite's business."""
    out = subprocess.run(['git', 'ls-files', '*.sh'], cwd=str(HELPER),
                         capture_output=True, text=True)
    return sorted(HELPER / p for p in out.stdout.split() if p.strip())


def findings(path: Path):
    """(verb, line_no, line) for each destructive verb outside a comment."""
    hits = []
    for i, raw in enumerate(path.read_text(encoding='utf-8',
                                           errors='replace').splitlines(), 1):
        code = raw.split('#', 1)[0]
        if not code.strip():
            continue
        for name, pat in DESTRUCTIVE.items():
            if re.search(pat, code):
                hits.append((name, i, raw.strip()))
    return hits


SCRIPTS = tracked_scripts()


def test_there_are_scripts_to_check():
    """The negative control, and the one this file cannot do without: every
    assertion below passes trivially if the glob finds nothing."""
    assert SCRIPTS, 'no tracked .sh found — the scan is looking in the wrong place'


@pytest.mark.parametrize('script', SCRIPTS,
                         ids=[p.name for p in SCRIPTS] or ['none'])
def test_a_destructive_script_is_guarded_or_declares_itself(script):
    """THE RULE. Either the destructive step is opt-in, or the script states in
    one sentence why a second run cannot destroy anything.

    `zebra/deploy_server.sh` fails this against its pre-2026-08-30 source: the
    `zebra reset --confirm` sat at the top level with no flag and no
    declaration, under a header that claimed idempotence.

    RETIRES WHEN: destructive operations move behind a shared shell helper that
    prompts or requires a flag itself, so a script cannot call one unguarded.
    """
    hits = findings(script)
    if not hits:
        return
    text = script.read_text(encoding='utf-8', errors='replace')
    if DECLARATION in text or GUARDED.search(text):
        return
    listed = '\n'.join('    line %d: %s  (%s)' % (n, ln[:70], v)
                       for v, n, ln in hits[:5])
    pytest.fail(
        '%s can destroy something and neither guards it behind an opt-in flag '
        'nor carries a "%s" line:\n%s\n\n'
        'Add ONE of:\n'
        '  * a flag gate, e.g.  if [ "${1:-}" = "--reset" ]; then ... fi\n'
        '  * a line  %s <why running this twice cannot destroy anything>'
        % (script.relative_to(HELPER), DECLARATION, listed, DECLARATION))


def test_no_tracked_script_deletes_itself():
    """`restart_services.sh` did, to "unblock git pull" — and because the file
    was TRACKED, the deletion is exactly what blocks a rebase pull from then
    on. A permanently dirty working tree on the box, caused by the script's own
    workaround, and it is why the pull failed on the day of the incident.

    RETIRES WHEN: no deploy path needs to touch its own file — i.e. the pull is
    done by something that is not itself in the repo being pulled.
    """
    for script in SCRIPTS:
        text = script.read_text(encoding='utf-8', errors='replace')
        name = script.name
        for line in text.splitlines():
            code = line.split('#', 1)[0]
            if re.search(r'\brm\b', code) and name in code:
                pytest.fail(
                    '%s deletes itself (%s). It is tracked, so the deletion '
                    'leaves a permanent unstaged change that blocks every '
                    'later `git pull --rebase`.'
                    % (script.relative_to(HELPER), line.strip()))


def test_the_deploy_script_reset_is_opt_in():
    """The specific regression, pinned by name rather than only by the general
    rule — this one destroyed a real book and deserves to fail loudly and
    unambiguously if it comes back.

    RETIRES WHEN: `zebra reset` itself refuses a non-empty book, so no caller
    can be the dangerous one.
    """
    p = HELPER / 'zebra' / 'deploy_server.sh'
    if not p.exists():
        pytest.skip('deploy_server.sh is gone')
    text = p.read_text(encoding='utf-8', errors='replace')
    code = '\n'.join(l.split('#', 1)[0] for l in text.splitlines())
    assert 'reset --confirm' in code, 'the reset moved; re-point this test'
    assert '--reset' in code, 'the reset is no longer behind an opt-in flag'
    assert 'REFUSED' in code, (
        'the in-flight check is gone — the flag alone still lets an operator '
        'destroy a live book in one command')


def test_the_rule_is_written_down_where_a_script_author_will_see_it():
    """A convention that lives only in a test is one nobody follows until the
    test fails. It belongs in the tracked instructions too.

    RETIRES WHEN: CLAUDE.md is generated from the tests that enforce its rules,
    so the two cannot disagree.
    """
    claude_md = (HELPER / 'CLAUDE.md').read_text(encoding='utf-8',
                                                 errors='replace')
    assert DECLARATION in claude_md, (
        'CLAUDE.md does not document the SAFE-TO-RERUN rule, so a script '
        'author meets it for the first time as a test failure')

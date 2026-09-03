"""The spawned agent's tool grants: an ALLOWLIST of verbs, kept honest.

THE DEFECT (found 2026-08-31). `VET_ALLOWED_TOOLS` was one blanket prefix,
`Bash({python} -m zebra:*)`, with `VET_DENIED_TOOLS` as the only thing between
a spawned agent and the position verbs. Two ways through it:

  * DENY IS BY TEXT, AND TEXT IS QUOTABLE. The deny rules are globs over the
    literal command string, so `-m zebra "close" 455 --exit-debit 0 --reason
    tp` matches NONE of them while still matching the allow prefix. The agent
    is granted WebSearch/WebFetch by design and does live event research, so
    the input that would type that is not hypothetical.
  * THE PREFIX COVERED THE MODULE FORM. `-m zebra.restore_snapshot` begins
    with `-m zebra`; it REWRITES THE BOOK, and it appeared in no deny rule.

`zebra reset --confirm` is the verb that force-closed the whole cohort at
-100% on 2026-08-30. A deny list cannot be the boundary for something that
matters this much, so the ALLOW list is now the boundary and the deny list is
defence in depth.

WHY THIS FILE EXISTS AT ALL. Narrowing an allow list has its own failure mode,
and this exact grant has already been fixed three times for it: **an allow rule
that does not match is indistinguishable from a broken agent.** Claude Code
prints an approval request into a log nobody reads live and the agent exits 0
with the work undone — not a hang, not an error. So every verb the prompts and
VETTING.md instruct an agent to run must be provably covered HERE, in the
suite, rather than discovered on the Pi.

Run:  cd Helper && PURE_PYTHON=1 python -m pytest zebra/tests/test_vet_tool_grants.py -v
"""
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

HELPER = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(HELPER))

from zebra import config as cfg                                  # noqa: E402
from zebra import vet                                            # noqa: E402


def _bash_prefixes(channel='vet'):
    """The command prefixes this channel may run, interpreter stripped."""
    out = []
    for t in vet._allowed_tools(channel):
        m = re.match(r'^Bash\((.*):\*\)$', t)
        if m:
            cmd = m.group(1)
            i = cmd.find('-m zebra')
            if i >= 0:
                out.append(cmd[i:])
    return sorted(set(out))


def _covered(command: str, channel='vet') -> bool:
    """Would any granted prefix admit this command?"""
    return any(command.startswith(p) for p in _bash_prefixes(channel))


# ── the allowlist must admit everything the agent is TOLD to run ───────────

#: Every zebra command a prompt template or VETTING.md instructs an agent to
#: run. A verb missing from the grants makes that channel silently useless.
INSTRUCTED = [
    '-m zebra vet show 455',
    '-m zebra vet decide 455 --verdict allow --reason x',
    # `exit-decide`, NOT `exit`. This line read `vet exit ...` until
    # 2026-09-02 -- a command argparse rejects with "invalid choice" -- so the
    # test proved a fiction granted while every real exit vet was refused at
    # the permission layer and burned the full 900s hold. A hand-written
    # command string is only as good as the hand; see
    # `test_every_granted_verb_is_a_real_subcommand`.
    '-m zebra vet exit-decide 455 --kind debit_sl --verdict allow',
    '-m zebra quote 455',
    '-m zebra review show 455',
    '-m zebra review record 455 --action hold --reason x',
    '-m zebra postmortem show 455',
    '-m zebra postmortem record 455 --tag x --lesson y',
]


@pytest.mark.parametrize('command', INSTRUCTED)
def test_the_agent_can_run_every_verb_its_prompts_ask_for(command):
    """The silent-failure guard. An unmatched grant looks exactly like an
    agent that answered nothing."""
    assert _covered(command), (
        'the spawn prompt instructs this command but no allow rule matches '
        'it — the agent will print an approval request to a log nobody reads '
        'and exit 0 with the work undone: %r' % command)


def test_every_verb_in_the_prompt_templates_is_granted():
    """Derived from the TEMPLATES rather than from a hand list, so a new verb
    added to a prompt cannot quietly go ungranted.

    RETIRES WHEN: the grants are generated from the prompt templates, so the
    two cannot disagree.
    """
    blob = ' '.join([cfg.VET_PROMPT_TEMPLATE, cfg.EXIT_PROMPT_TEMPLATE,
                     cfg.REVIEW_PROMPT_TEMPLATE,
                     cfg.POSTMORTEM_PROMPT_TEMPLATE])
    # `[a-z-]`, WITH THE HYPHEN. The class was `[a-z]` until 2026-09-02, which
    # stops dead at a hyphen: `-m zebra vet exit-decide` yielded the verb
    # `vet exit`, the grant list happened to contain that exact fiction, and
    # this test passed for the one verb that was broken in production.
    verbs = set(re.findall(r'-m zebra ([a-z][a-z-]*(?: [a-z][a-z-]*)?)', blob))
    verbs.discard('events replace')          # events channel, denied on purpose
    missing = [v for v in sorted(verbs)
               if not _covered('-m zebra %s x' % v)]
    assert not missing, (
        'these verbs appear in a spawn prompt but are not granted: %s'
        % missing)


# ── and it must admit nothing else ─────────────────────────────────────────

@pytest.mark.parametrize('command', [
    '-m zebra close 455 --exit-debit 0 --reason tp',
    '-m zebra "close" 455 --exit-debit 0 --reason tp',   # the quoting evasion
    "-m zebra 'close' 455",
    '-m zebra reset --confirm',
    '-m zebra "reset" --confirm',
    '-m zebra enter 455 --pair 100/110 --debit 1 --lots 1',
    '-m zebra cancel 455 --reason x',
    '-m zebra trigger 455',
    '-m zebra run',
    '-m zebra loop',
    '-m zebra scan',
    '-m zebra report --telegram',
    '-m zebra.restore_snapshot logs/archive/x.json',
    '-m zebra.reset',
    '-m zebra postmortem run',
])
def test_the_position_and_spawning_verbs_are_NOT_granted(command):
    """THE DEFECT, one line per way through it. Every one of these matched the
    old `-m zebra:*` prefix; the quoted ones and the two module forms then
    matched no deny rule either."""
    assert not _covered(command), (
        'a spawned agent could run %r' % command)


def test_the_deny_list_still_names_the_position_verbs():
    """Defence in depth. Narrowing the allow must not have quietly become a
    reason to drop the second refusal."""
    joined = ' '.join(cfg.VET_DENIED_TOOLS)
    for verb in ('close', 'enter', 'cancel', 'reset', 'trigger',
                 'run', 'loop', 'scan', 'report'):
        assert 'zebra %s' % verb in joined, verb
    assert 'restore_snapshot' in joined, (
        'the module that rewrites the book is in no deny rule')
    assert '-m zebra.' in joined, 'the bare module form is not denied'


def test_no_grant_carries_an_unformatted_placeholder():
    """`{python}` reaching argv verbatim would match nothing the agent types
    — the same silent shape, arrived at from the other side."""
    for t in vet._allowed_tools('vet'):
        assert '{' not in t and '}' not in t, t


def test_the_events_channel_keeps_its_scoped_write_and_gains_nothing_else():
    base = set(vet._allowed_tools('vet'))
    events = set(vet._allowed_tools('events'))
    extra = events - base
    assert extra, 'the events channel lost its candidate-file grant'
    assert any(t.startswith('Edit(') for t in extra), (
        'the events channel lost its scoped candidate-file Edit')
    # It also gets its finishing VERB, and nothing wider: `events replace` is
    # denied to every other channel by `_denied_tools`, and the allowlist is
    # what grants it back here.
    for t in extra:
        assert t.startswith('Edit(') or 'events replace' in t, (
            'the events channel gained something unexpected: %s' % t)


def test_web_research_is_still_granted():
    """The negative control on the narrowing: the vetting checklist is real
    work (results dates, ex-div, overhangs) and needs the web."""
    tools = vet._allowed_tools('vet')
    for t in ('WebSearch', 'WebFetch', 'Read', 'Glob', 'Grep'):
        assert t in tools


# ── the grant must name a verb that EXISTS ────────────────────────────────

@pytest.mark.parametrize('verb', cfg._VET_VERBS)
def test_every_granted_verb_is_a_real_subcommand(verb):
    """Ask the REAL CLI whether each granted verb parses.

    THE DEFECT THIS EXISTS FOR (2026-09-02). `_VET_VERBS` carried `vet exit`
    for weeks. There is no such subcommand -- argparse answers "invalid
    choice: 'exit' (choose from 'show', 'exit-decide', 'decide', 'score')" --
    so the grant matched nothing an agent could ever type, and every exit vet
    silently burned its full 900s hold instead of recording a verdict.

    Two other checks in this file were meant to catch that and both missed it,
    because both compared one hand-written string against another: `INSTRUCTED`
    listed the same non-existent command, and the template-derived test used a
    regex that stopped at the hyphen. This test compares the grant against the
    ONLY authority that cannot be mis-spelled twice -- the parser itself.

    Subprocess rather than import because the parser is built inside `main()`
    and there is no `build_parser()` to call. ~0.8s per verb.

    RETIRES WHEN: parser construction moves out of `main()` into an importable
    `build_parser()`, at which point assert against its choices directly.
    """
    env = dict(os.environ, PURE_PYTHON='1')
    r = subprocess.run(
        [sys.executable, '-m', 'zebra'] + verb.split() + ['--help'],
        cwd=str(HELPER), env=env, capture_output=True, text=True, timeout=120)
    blob = (r.stdout or '') + (r.stderr or '')
    # FAIL-OPEN GUARD, added in review before deploy. Asserting only on
    # 'invalid choice' passes when the CLI cannot even IMPORT: rc=1 with "No
    # module named zebra" contains no such string, so all 8 cases go green
    # while the parser is dead. A bad overlay on the Pi, a failed config
    # assert (this same change added two) or a py3.8 slip all land there --
    # and "the check cannot tell a broken CLI from a working one" is the exact
    # defect class this whole test file exists for. `--help` exits 0; an
    # invalid choice exits 2; an import crash exits 1.
    assert r.returncode == 0, (
        'the CLI did not start, so this test proves nothing about the grant. '
        'rc=%s: %s' % (r.returncode, blob.strip()[-400:]))
    assert 'invalid choice' not in blob, (
        'VET_ALLOWED_TOOLS grants `%s`, but the CLI has no such subcommand, so '
        'the allow rule can never match. An agent told to run it prints an '
        'approval request into a log nobody reads and exits 0 with the work '
        'undone. argparse said: %s'
        % (verb, blob.strip().splitlines()[-1] if blob.strip() else '(no output)'))

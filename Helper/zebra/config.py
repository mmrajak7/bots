"""Zebra strategy configuration — paths, thresholds, Chartink scan clauses."""

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from common import layered_config

logger = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # zebra/
PROJECT_ROOT = SCRIPT_DIR.parent                    # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                     # BOTS/
LOG_DIR = PROJECT_ROOT / 'logs'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'zebra_config.json'
LOCAL_FILE = LOG_DIR / 'zebra_trades.json'
# Cross-process mutex for the trade store. Two writers exist as of 2026-08-10
# (zebra cron + the Claude vetting/review cron), and an unprotected
# read-modify-write loses trades SILENTLY. A sidecar token file, never read —
# see common/filelock.py for why an OS advisory lock beats a PID lockfile here.
LOCK_FILE = LOG_DIR / 'zebra_trades.lock'
# Claude vetting journal. SEPARATE lock from the trade store on purpose: sharing
# one would deadlock any caller that journalled while holding the trade lock
# (POSIX flock is per-fd, so a second acquire in the same process blocks).
# Callers must never nest the two.
DECISIONS_FILE = LOG_DIR / 'zebra_decisions.json'
DECISIONS_LOCK = LOG_DIR / 'zebra_decisions.lock'

# ── Claude vetting layer ──────────────────────────────────────────────────
# Master switch. ON by default since 2026-08-27, and the default now agrees
# with `config/zebra_config.defaults.json`, which is TRACKED.
#
# It shipped dark (default False, key absent from the tracked file), so the ON
# state existed only in the Pi's untracked overlay: the master switch for the
# whole layer could not be read, reviewed or flipped from git, and a routine
# overlay rebuild would have disarmed it silently. Silently is the operative
# word — with the flag False `vet.exit_gate` returns 'proceed' unconditionally
# and every entry alert goes out unvetted, which looks exactly like a healthy
# system.
#
# True is not an arming step and cannot cause an unvetted entry: the entry path
# fails CLOSED on this layer (no verdict, no entry), so a broken CLI holds
# signals rather than releasing them. Exits fail OPEN on the vet timeout, back
# onto the deterministic guards. What changes is only that the layer can no
# longer vanish without anyone noticing.
# (VET_ENABLED is exported further down — it reads zebra_config.json, which is
# not loaded yet at this point in the module.)
# CLI ONLY — never the Anthropic API/SDK. The Pi is authenticated once
# interactively and that is the sanctioned path for this fleet.
# Bare `claude` is only a starting point — vet.resolve_cli() turns it into an
# absolute path, because cron's PATH (/usr/bin:/bin) does not contain the CLI's
# install directory and every spawn would fail.
VET_CLI = os.environ.get('ZEBRA_VET_CLI', 'claude')
# Tool permissions for the spawned agents, passed as CLI FLAGS.
#
# Measured 2026-08-12, because the obvious approach silently does not work:
# a project `.claude/settings.json` **allow** rule is IGNORED by `claude -p`
# (Claude Code will not grant itself permissions from a file in the working
# directory — otherwise any cloned repo could). Its **deny** rules ARE honoured.
# So grants must ride on argv; the settings file is only a deny-side backstop.
#
# The failure mode without this is nasty precisely because it is quiet: the
# agent starts, decides it needs approval, writes "please approve this command"
# to a log nobody reads, and **exits 0 after ~12 seconds**. Not a hang, not an
# error — a clean exit with the work undone. Every signal then fails open and
# enters unvetted while the switch still reads ON.
#
# Deliberately coarse on the allow side and precise on the deny side. Matching
# one exact verb per rule looked tighter but breaks the moment the agent
# prefixes a `cd`, and a denied tool is indistinguishable from a broken layer.
# The invariant that actually matters — the model can never open or close a
# position — is carried by the deny list, whose patterns match anywhere in the
# command string (verified).
#
# BOTH interpreter spellings, 2026-08-14. The prompt interpolates `{python}` =
# `sys.executable`, an ABSOLUTE path, and the grant was built from the same
# value — so on paper they always matched. In practice the calendar agent typed
#     ../CROCODILE/venv/bin/python -m zebra events replace --file ...
# because every doc, runbook and memory note on this box spells the interpreter
# relatively, and a model reading the repo will reproduce what it sees. The
# absolute grant does not prefix-match the relative command, so the install was
# refused with "This command requires approval" — after the agent had already
# done the research and written a perfect candidate file.
#
# Fourth variant of one bug: a grant that names ONE spelling of a thing the
# agent can legitimately write two ways. (Candidate file: cwd-relative vs `//`
# absolute. Tool family: Write vs Edit. Now: the interpreter.) The rule that
# generalises — **grant every form the agent could plausibly type, because an
# unmatched grant is indistinguishable from a broken agent and costs a whole
# spawn to discover.** This is not a widening: same interpreter, same module,
# same `-m zebra` prefix, and the deny list still matches anywhere in the
# string, so no position verb becomes reachable.
#: The zebra verbs a spawned agent may run, and nothing else.
#
# THIS USED TO BE `-m zebra:*` — one prefix covering EVERY verb the package
# has, with the deny list as the only thing standing between an agent and the
# position verbs. Two problems with leaning on the deny list here, both found
# 2026-08-31:
#
#   * DENY IS BY TEXT AND TEXT IS QUOTABLE. `Bash(*zebra close*)` is a glob
#     over the literal command string, so `-m zebra "close" 455` matches none
#     of the deny rules while still matching the allow prefix. The agent does
#     live web research by design (WebSearch/WebFetch are granted), so the
#     input that would type that is not hypothetical.
#   * THE PREFIX ALSO COVERED THE MODULE FORM. `-m zebra.restore_snapshot`
#     starts with `-m zebra`, and that module REWRITES THE BOOK. It appears in
#     no deny rule at all.
#
# So the allow list is now the boundary, and it is an ALLOWLIST OF VERBS —
# the same shape, and for the same reason, as `log_cleanup`'s allowlist and
# `outcomes.STOP_KINDS`: "is this one of the things I do" is a closed
# question, "is this dangerous" needs every dangerous spelling that will ever
# exist. The deny list stays as defence in depth.
#
# EVERY VERB HERE IS EITHER READ-ONLY OR THE CHANNEL'S OWN FINISHING WRITE.
# `test_the_agent_can_run_every_verb_its_prompts_ask_for` pins this list
# against the prompts and VETTING.md, because an allow rule that does not
# match is INDISTINGUISHABLE from a broken agent — it prints an approval
# request into a log nobody reads live and exits 0 with the work undone. That
# silent shape has already cost this grant three separate fixes; a missing
# verb must fail in the suite, not on the Pi.
# A VERB HERE MUST BE THE REAL SUBCOMMAND, SPELLED EXACTLY.
#
# 2026-09-02, the fourth cut at this grant. This list said `vet exit`; the
# argparse subcommand is `exit-decide`, and `EXIT_PROMPT_TEMPLATE` instructs
# `vet exit-decide`. Claude Code's prefix match does not cross the token
# boundary, so the grant matched nothing the agent ever typed. All three of
# that day's exit vets reached a verdict of `allow` and NONE could record it:
# each held the full 900s `exit_vet_max_hold_sec` and fell through on EXIT VET
# TIMED OUT. On COALINDIA #440 that hold cost 0.40 points -- Rs 540 of a
# Rs 4,703 loss -- while the agent's own transcript said the write was refused.
#
# The suite did not catch it because BOTH its checks were blind in the same
# place: `INSTRUCTED` hand-wrote a command that does not exist, and the
# template-derived test matched verbs with `[a-z]+`, which stops at the hyphen
# and extracted `vet exit` from `vet exit-decide` -- proving the fiction
# granted. `test_every_granted_verb_is_a_real_subcommand` now parses each verb
# against the real CLI, which is the check that does not depend on anyone
# spelling the same thing twice.
_VET_VERBS = [
    'vet show', 'vet decide', 'vet exit-decide',   # the vetting channel itself
    'quote',                                   # the live re-quote (read-only)
    'review show', 'review record',            # position review
    'postmortem show', 'postmortem record',    # post-mortems
]
VET_ALLOWED_TOOLS = (
    ['WebSearch', 'WebFetch', 'Read', 'Glob', 'Grep']
    + ['Bash({python} -m zebra %s:*)' % v for v in _VET_VERBS]
    + ['Bash({python_rel} -m zebra %s:*)' % v for v in _VET_VERBS]
)
# The calendar agent builds a candidate JSON file before installing it — and
# that is ALL it may write. An unscoped `Write` made vet.py's stated invariant
# ("Claude NEVER writes the store directly ... the vetting layer physically
# cannot corrupt the trade record") false for this one channel: the agent could
# write zebra_trades.json, zebra_config.json (which carries vet_enabled),
# VETTING.md, or any .py in the package. The deny list has no Write rule to
# catch it, and the test asserting the OTHER channels lack Write did not bound
# this one. Scoped to the single file it legitimately produces.
EVENT_CANDIDATE_FILE = LOG_DIR / 'event_calendar.candidate.json'
# Granted in BOTH forms, because a permission pattern that does not match is
# indistinguishable from a broken agent. Claude Code matches file-tool patterns
# relative to the project directory; an ABSOLUTE path needs a `//` prefix. The
# first cut of this rule passed a bare absolute path, which matched nothing —
# so the calendar agent was denied its only Write, printed an approval request
# to a log nobody reads, and exited 0 with the work undone. That is not a hang
# and not an error: it is the exact silent shape the block comment above warns
# about, and the auth watchdog reported it as "spawned but never completed",
# whose suggested cause (expired login) sent the owner to re-login for nothing.
# Both forms name the same single file, so the scope is unchanged.
#
# 2026-08-14, THE ACTUAL FIX. The two rules below said `Write(...)`, and Claude
# Code rejects that form outright — it says so in the agent log, verbatim:
#
#   Permission allow rule (--allowed-tools): Write(logs/event_calendar.
#   candidate.json) is not matched by file permission checks — only Edit(path)
#   rules are. Use Edit(logs/event_calendar.candidate.json) instead (Edit rules
#   cover all file-editing tools).
#
# So a path-scoped grant must be written `Edit(path)` REGARDLESS of which
# file-editing tool the agent reaches for; `Edit` is the permission family, not
# the tool name. `Write(path)` matched nothing, the agent printed an approval
# request to a log nobody reads live, and exited 0 with the calendar unwritten.
#
# That is the third distinct cut at this one grant — first the tool had no
# scope, then the absolute form was missing its `//`, now the family was wrong.
# Every version "looked right" and every version failed the same silent way,
# because an unmatched grant and an auth failure are indistinguishable from
# outside. The lesson is in the log-reading, not the pattern: this was only ever
# diagnosable from `vet_cli_*.log`, which is why that file exists.
_EVENT_CANDIDATE_REL = EVENT_CANDIDATE_FILE.relative_to(PROJECT_ROOT).as_posix()
EVENT_EXTRA_TOOLS = [
    'Edit({})'.format(_EVENT_CANDIDATE_REL),                 # cwd-relative
    'Edit(//{})'.format(EVENT_CANDIDATE_FILE.as_posix().lstrip('/')),  # absolute
    # AND THE VERB THAT INSTALLS IT (added 2026-09-01).
    #
    # The DENY side of this was already correct: `_denied_tools` strips
    # `events replace` for the events channel alone, which is the only way to
    # say "everybody except the agent whose job this is" when deny beats
    # allow. What carried the ALLOW side was the blanket `-m zebra:*` prefix,
    # so narrowing that prefix to a verb allowlist on 2026-08-31 silently took
    # this verb away from the one channel entitled to it -- and the failure
    # would have been the quiet kind: the agent researches, writes its
    # candidate, and is refused at the last step, leaving `adjustment_today`
    # (the interlock that suspends automated exits on a bonus or split day)
    # reading a calendar nobody refreshed.
    #
    # Scoped to the events channel, like the Edit rules above. The separate
    # `--allow-empty` hazard is refused inside `cmd_events_replace` by
    # channel: an agent may REFRESH the calendar and may not EMPTY it.
    'Bash({python} -m zebra events replace:*)',
    'Bash({python_rel} -m zebra events replace:*)',
]
# The five position VERBS, plus the four verbs that CALL them. Denying only
# the explicit verbs left the invariant above false: `zebra run` was granted by
# the coarse allow rule and denied by nothing, and it runs the whole cycle —
# `run_once` -> `run_cycle` -> `_enter_as_bcs` (opens a paper position) and
# `check_entered` -> `_paper_auto_close` (closes one), then `_run_vet_side_
# channels` spawns FURTHER agents recursively. A model asked to judge one
# signal could open and close positions across the whole book and fork the
# fleet, while the preflight reported the deny list complete. `loop` is the
# same cycle held open all session; `scan` mutates the watchlist; `report`
# sends Telegram to the owner as if the engine had spoken.
#
# Two more, found 2026-08-13 by re-applying the same "deny the callers" test to
# the verbs added AFTER that rule was written:
#   `postmortem run` calls `spawn_batch` -> `_spawn_generic`. It is a SPAWNER,
#   the exact class the paragraph above exists to catch, and it was reachable
#   by every channel. Recursion is bounded by the budget cap and a daily
#   marker, but a preflight asserting "the deny list is complete" would have
#   passed again while the invariant was false.
#   `events replace` REPLACES the shared event calendar, and `--allow-empty`
#   can wipe it — the same calendar the corporate-action interlock reads. Any
#   channel could blank a safety input and re-stamp it "fresh". Denied for
#   everyone here and granted back to the events channel alone via
#   EVENT_EXTRA_TOOLS, which is how per-channel scoping already works.
#
# DEFENCE IN DEPTH ONLY, since 2026-08-31. These are globs over the literal
# command text, so quoting evades every one of them (`zebra "close"`), and a
# deny list can never be the boundary for that reason. `VET_ALLOWED_TOOLS`
# above is the boundary; this stays because two independent refusals are
# better than one, and because it documents the intent.
#
# `restore_snapshot` and the bare MODULE FORM are named explicitly: the old
# `-m zebra:*` allow covered `-m zebra.restore_snapshot`, which rewrites the
# trade book, and it appeared in no deny rule.
VET_DENIED_TOOLS = ['Bash(*zebra close*)', 'Bash(*zebra enter*)',
                    'Bash(*zebra cancel*)', 'Bash(*zebra reset*)',
                    'Bash(*zebra trigger*)',
                    'Bash(*zebra run*)', 'Bash(*zebra loop*)',
                    'Bash(*zebra scan*)', 'Bash(*zebra report*)',
                    'Bash(*postmortem run*)', 'Bash(*events replace*)',
                    'Bash(*restore_snapshot*)', 'Bash(*-m zebra.*)']

# VET_MODEL, VET_TIMEOUT_SEC and CHILD_KILL_SEC are exported further down —
# they read zebra_config.json, which is not loaded yet at this point.
# The fail-open deadline is generous because the CLI does live web research
# (results dates, ex-div, overhangs) and the structure is hedged and non-HFT,
# so a few minutes of entry drift costs far less than trading an unvetted
# signal. Past it an ENTRY is QUEUED for a fresh agent, not entered unvetted
# (inverted 2026-08-13); the wait is bounded and a give-up telegraphs, so the
# outage still cannot become a SILENT trading halt.
# {python} is filled with sys.executable at spawn time: the Pi runs the bot
# under a venv and has no guaranteed bare `python` on PATH, and the CLI verbs
# must import the exact zebra package the bot runs.
# The spawn prompt stays SHORT because it is argv. The real instructions live in
# zebra/VETTING.md, which the agent reads — editable without touching code, and
# reviewable as prose rather than as an escaped string literal.
VET_PROMPT_TEMPLATE = (
    "Read {vetting_doc} and follow it exactly to vet signal {trade_id}. "
    "Start with `{python} -m zebra vet show {trade_id}`. "
    "Use `{python}` for every zebra command. "
    "You MUST finish by calling `{python} -m zebra vet decide {trade_id} "
    "--verdict allow|veto ...` exactly once."
)
VETTING_DOC = SCRIPT_DIR / 'VETTING.md'
# How many times Claude may defer one exit before the human is asked. Two
# deferrals is ~10 min of re-checks with fresh quotes. Past that the exit does
# not fire on the deferral alone: every structure here is hedged with max loss
# = debit known at entry, so holding is bounded while exiting on a bad print is
# not (NHPC). The conservative direction is to hold and escalate.
#
# CORRECTED 2026-09-01: this used to say "we do NOT fall through to the
# deterministic trigger", full stop, which `exit_vet_max_hold_sec` has
# contradicted since 2026-08-29. The escalation buys the HUMAN TIME; it is not
# a veto over the guards. Past that budget (900s, per session) the exit
# proceeds on the deterministic guards alone and says so loudly -- because the
# guards had already cleared it before the agent was ever asked, and an
# unbounded hold inverts that (ASHOKLEY #390: -50% to -75% over three cycles
# on an agent that had died on quota).
EXIT_MAX_DEFERS = 2
EXIT_PROMPT_TEMPLATE = (
    "An EXIT trigger fired on open position {trade_id} ({exit_kind}) and the "
    "quote behind it looks questionable. Read {vetting_doc} (the EXIT section) "
    "and follow it exactly. Start with "
    "`{python} -m zebra vet show {trade_id} --exit {exit_kind}`. "
    "Use `{python}` for every zebra command. You MUST finish by calling "
    "`{python} -m zebra vet exit-decide {trade_id} --kind {exit_kind} "
    "--verdict allow|defer ...` exactly once."
)
KITE_TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
TELEGRAM_CONFIG = BOTS_ROOT / 'data' / 'telegram_config.json'
OPTIONS_CSV = PROJECT_ROOT / 'nse_stocks_options.csv'

# ── Chartink scan clauses ─────────────────────────────────────────────────
# Both sides: price within ±X% of ST line, ST direction determines play.
# Width 8.1% mirrors magnet (catches candidates before Chartink delivery delay
# pushes them past entry window). Our own Kite-LTP check enforces actual gate.

CHARTINK_URL = 'https://chartink.com/screener/process'

CHARTINK_MONTHLY = (
    '( {33489} ( '
    ' monthly close <=  monthly supertrend( 10 , 3 ) *  1.081'
    ' and  monthly close >=  monthly supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

CHARTINK_WEEKLY = (
    '( {33489} ( '
    ' weekly close <=  weekly supertrend( 10 , 3 ) *  1.081'
    ' and  weekly close >=  weekly supertrend( 10 , 3 ) *  0.919'
    ' ) )'
)

_ALL_SCANNERS = [
    {'name': 'monthly', 'clause': CHARTINK_MONTHLY, 'timeframe': 'monthly'},
    {'name': 'weekly',  'clause': CHARTINK_WEEKLY,  'timeframe': 'weekly'},
]

# ── Defaults ──────────────────────────────────────────────────────────────
_DEFAULTS = {
    'paper_mode': True,          # PAPER: auto-enter on trigger + auto-close on exit signal
    # Hand this book's EXITS to `bcs/spread_monitor.py`, the only code in the
    # fleet that can place a real order. Cohort records only — everything else
    # in this store is the dropped back ratio and no other engine can see it.
    #
    # Default FALSE, and it must stay false until the monitor is armed. The
    # two switches move TOGETHER: `--dry-run` off the monitor's crontab line
    # and this on. Turning this on alone leaves those positions with NO exit
    # engine at all — the monitor would watch and place nothing while zebra
    # stands aside — and nothing in either log would look wrong. Turning it on
    # LATE is the safe direction: two engines racing is visible, an
    # unmonitored position is not.
    'exits_managed_externally': False,
    'entry_structure': 'bcs',    # What a triggered signal actually opens.
                                 # 'bcs'   — ONE record, structure='bcs', no
                                 #           shadow. The pipeline as of
                                 #           2026-08-12: zebra is retired
                                 #           (28 matched pairs, 4.1% RoC vs
                                 #           18.4%, identical win counts, ~9x
                                 #           the capital, 3 legs, one of them
                                 #           deep-ITM and illiquid).
                                 # 'zebra' — the old path: a zebra entry plus
                                 #           an optional BCS shadow. Set this
                                 #           to roll the migration back; no
                                 #           data migration either way, since
                                 #           open positions are untouched.
    'bcs_paper_enabled': True,   # Shadow BCS (buy ATM, sell strike nearest ST target)
                                 # paper-traded alongside every zebra entry for A/B
                                 # comparison (July 2026 slippage analysis). Only
                                 # active when paper_mode is also true.
    'alert_structures': ['bcs'], # Which structures' Telegram alerts fire
                                 # (ENTER + TP/SL/TIME). 2026-07-17: BCS is the
                                 # voice, zebra trades silently in the background.
                                 # Both keep auto-trading + appear in EOD reports
                                 # regardless. Set ['zebra','bcs'] or ['zebra'] to
                                 # change who talks.
    'watch_gap_max': 0.05,       # WATCH band ceiling (signal added to watchlist)
    'trigger_gap_max': 0.04,     # TRIGGER zone: run Zebra analyzer + alert
    'stale_gap_min': 0.03,       # Floor: skip if gap < this at trigger (too late)
    'freshness_days': 5,         # Skip if price touched ST in last N days (bounce)
    # The REAL watch-band floor — see FRESH_ENTRY_GAP below. 0.04, not
    # stale_gap_min's 0.03, because that is the value magnet's check_freshness
    # has been silently enforcing all along.
    'fresh_entry_gap': 0.04,
    'min_dte': 15,
    'max_dte': 45,
    'min_leg_oi': 5000,
    # M3. How old `nse_stocks_options.csv` may be before ENTRIES are refused.
    #
    # That file is where LOT SIZES come from, and a lot size becomes an order
    # QUANTITY at the broker. It is refreshed by its own 09:00 Mon-Fri cron
    # (`flow/server_setup/README_SERVER.txt`) and nothing checked that the
    # cron had actually run - a job that dies quietly leaves every entry
    # sizing itself from whatever was true the last time it worked.
    #
    # 4 days, not 1: Friday's file is correct on Monday, and a holiday makes
    # that three calendar days. The number is not trying to catch a
    # lot-size circular the day it lands - it is trying to catch a refresh
    # that has STOPPED. EXITS are never gated on it; they read symbols off
    # the trade record and never touch this file.
    'options_csv_max_age_days': 4,
    # 0.01 -> 0.015 on 2026-08-29 (M1). NOT a threshold change: the tracked
    # defaults file is a real config LAYER and wins at runtime, so 0.015 is
    # what the book has always run on and 0.01 here was dead. Aligning the
    # dead fallback to the live value removes a source that would take effect
    # only if the config file ever went missing - i.e. exactly when nobody
    # would notice the gate had moved. Measurement-only for BCS: it governs
    # the retired back-ratio path.
    'max_leg_spread_pct': 0.015,  # bid-ask spread cap per leg (1% of mid)
    'bcs_max_entry_cost_pct': 15.0,
                                 # HARD gate: (ask(long) - bid(short)) minus
                                 # the same spread at mid, as a share of the
                                 # max gain at mid. What the book charges just
                                 # to open the position.
                                 # Replaces the raw per-leg rupee bid-ask cap
                                 # dropped 2026-08-10 — that one fired on 68%
                                 # of shadows with no signal (58.8% WR flagged
                                 # vs 62.5% clean) because rupees per leg say
                                 # nothing about whether the payoff survives.
                                 # This is denominated in the payoff, the same
                                 # logic bcs_max_debit_to_width_pct uses.
                                 # UNCALIBRATED — reasoned, not fitted. No
                                 # historical record persisted its entry books
                                 # so the rejection rate on the existing 42 is
                                 # unmeasurable. 15% ≈ legs ~15% wide, well
                                 # inside the 25%-of-mid that _leg_reliable
                                 # admits (a garbage-print detector, never a
                                 # tradeability gate). Review once ~30
                                 # fill-basis records exist.
    # ── the payoff gate, MEASURED BUT NOT ENFORCED (2026-09-03) ──────────
    # A candidate replacement for bcs_max_debit_to_width_pct, recorded on every
    # signal and blocking nothing, so ~30 signals of evidence accrue before
    # anyone argues about the threshold.
    #
    # WHY. The TP fires when spot reaches the ST target, and the short strike
    # sits AT that target, so every take-profit books with spot on the short
    # leg and 27-36 DTE left. Measured on 12 cohort TP exits, the spread is
    # then worth 42-66% of width, mean 55.2%. That makes the realised gain an
    # identity -- (V/w)/(d/w) - 1 -- with V/w near-constant, so d/w is the only
    # controllable input and the win is CAPPED AT ENTRY. At d/w 44 the ceiling
    # is about +28% against a -50% stop: a 0.56:1 payoff, knowable before the
    # order goes out. GMRAIRPORT entered at 36.7 and made +54.5%; MCX at 43.9
    # made +27.6%, on the same exit quality.
    #
    # The existing 45% cap is denominated in the EXPIRY payoff -- a payoff this
    # engine never collects, because it exits at the target. This one is
    # denominated in the exit that actually happens.
    'bcs_tp_value_frac_of_width': 0.55,
                                 # k: assumed spread value at the TP, as a
                                 # share of width. This is the MEAN of the
                                 # 42.0-66.3% observed on the 12 cohort TP
                                 # exits (mean 55.2, sd ~8).
                                 # AN EARLIER VERSION OF THIS COMMENT CALLED IT
                                 # "the conservative end ... NOT the mean" and
                                 # claimed it errs towards refusing a trade.
                                 # That was simply wrong -- 0.42 is the
                                 # conservative end -- and it inverted the
                                 # property: at the mean, half the observed
                                 # exits fall BELOW k, so the projection is
                                 # optimistic about half the time.
                                 # Kept at the mean deliberately. This number
                                 # classifies at ENTRY (is the payoff priced
                                 # out?), and with sd ~8 it is a weak per-trade
                                 # PREDICTOR either way; the honest use is the
                                 # rank on d/w, which any k in the range gives.
                                 # Note the boundary is k-sensitive: the 50%
                                 # floor implies d/w <= 36.7% at k=0.55, 28% at
                                 # 0.42 and 44% at 0.66 -- a span covering the
                                 # whole cohort range, which is the real reason
                                 # this must not be enforced yet.
                                 # Re-derive per ~10 new exit books; every BCS
                                 # record persists `exit_legs`, so unlike the
                                 # 45% cap this one CAN be re-fitted.
    'bcs_min_gain_at_tp_pct': 50.0,
                                 # would-block threshold: modelled gain at the
                                 # TP, (k*width)/debit_fill - 1. At k=0.55 this
                                 # is d/w <= 36.7% on the FILL basis. 50% pairs
                                 # the win ceiling to the -50% debit stop, i.e.
                                 # payoff >= 1:1 by construction.
                                 # NOT ENFORCED. Only 3 of 19 cohort entries
                                 # clear it, so switching it on today would cut
                                 # throughput by ~85% on the strength of an
                                 # identity plus 15 trades. The identity is
                                 # sound; what is unmeasured is whether the hit
                                 # rate is flat across d/w bands -- d/w is the
                                 # market's own probability quote, so buying
                                 # only cheap spreads is a bet against its
                                 # odds. That is exactly the ST-magnet claim,
                                 # and exactly what 15 closes cannot show.
                                 # REVISIT AT ~30 COHORT CLOSES: compare the
                                 # realised win rate of would-block vs
                                 # would-pass signals before enforcing.
    'bcs_max_debit_to_width_pct': 45.0,
                                 # HARD gate on the shadow BCS: reject when the
                                 # debit exceeds this share of the spread width.
                                 # From the 2026-08-10 study of 25 closed shadows:
                                 # d/w is the market's probability quote, so it
                                 # barely predicts WINNING (r=+0.20) but almost
                                 # perfectly predicts PAYOFF (r=-0.92 on winners).
                                 # Above ~45% there is no payoff left — that band
                                 # ran 40% WR / -16% ROC / PF 0.24. Gating at 45
                                 # kept 80% of trades and lifted ROC 16.6%->28.9%.
                                 # NOT the BCS playbook's 30-35%: that rule assumes
                                 # a freely-chosen width, but here the ST-magnet
                                 # distance pins width at ~3.8% of spot, and 35%
                                 # would keep only 24% of signals at 33% WR.
                                 # Jackknife puts the true optimum in 39-47, so
                                 # treat 45 as a region, not a magic number.
                                 # There is deliberately NO floor: cheap spreads
                                 # are the high-payoff tail (avg win +127%) and
                                 # capping that would violate the power-law rule.
    'tp_target': 'st_line',       # 'st_line' or 'short_strike'
    # ── COHORT BOUNDARY ────────────────────────────────────────────────────
    # The date the CURRENT engine started trading. Every entry from here on is
    # stamped with it, and reports split on that stamp.
    #
    # Why this exists rather than "just read the whole book": the 383 records
    # before this date were produced by an engine that no longer exists. They
    # were priced MID on both ends (so they model zero round-trip cost), they
    # ran without the Claude vetting layer, without the gain-anchored trail,
    # at min_dte 10, and without the guards added on 2026-08-13. Their
    # aggregate P&L answers a question nobody is asking any more.
    #
    # 2026-08-14 is day one of the two-month paper run whose whole purpose is
    # to answer ONE question: does this strategy clear its own costs? The
    # measured baseline says the median trade is +0.90% gross and -0.79% NET
    # of Zerodha fees, carried entirely by 3 winners out of 33 — see
    # Helper/ISSUES.md S1/S2. Mixing the old book into that measurement would
    # bury the answer.
    #
    # Stamped per trade, never recomputed: moving this date later must not
    # silently reclassify positions that are already open, for the same reason
    # `pricing_basis` is a property of the trade.
    'cohort_start': '2026-08-14',
    # Report and alert ONLY on trades this engine opened. Owner's call
    # 2026-08-13: "going forward i need to monitor and get alerts only for new
    # trades ... both live and consolidated eod report."
    #
    # This silences TELEGRAM only. The legacy positions still poll, still book
    # their exits, still appear in the store and in `zebra status` under the
    # whole-book block — they just stop talking. That distinction is the whole
    # design: an alert filter must never become an exit filter.
    'alerts_cohort_only': True,
    # Whether the EOD/weekly Telegram lists every open position with its
    # entry spot, live spot and own unrealized P&L.
    #
    # Was OFF while 25 LEGACY positions made that block most of the message.
    # `alerts_cohort_only` now caps the list at the current engine's book
    # (max_open_trades = 8), and on 2026-08-21 the owner asked for exactly
    # this detail — "Positions - Entry (spot price), LTP, our P&L for this
    # position ... so we know which positions are performing" — for BOTH the
    # daily and the weekly. The two reasons it was off are both gone, so it
    # is on. The COUNT and the aggregate go out either way.
    'eod_open_positions': True,
    'swing_tp_enabled': True,     # Shorten TP to a swing level standing between
                                  # spot and the ST magnet. LUPIN 2026-08: a PE
                                  # signal with a prior swing LOW well above the
                                  # ST line — price stalls at its own support far
                                  # more often than it runs the full distance to
                                  # a magnet. Only ever SHORTENS; a level beyond
                                  # ST is ignored. Off => TP stays the ST line.
    'swing_pivot_bars': 2,        # Candles either side that must be higher/lower
                                  # for a swing to count. BOTH sides required, so
                                  # the last N candles cannot form one — an
                                  # unconfirmed pivot at the right edge is the
                                  # move still happening, not a level.
    'swing_lookback_candles': 60, # How far back to look for pivots. ~14 months
                                  # weekly, 5 years monthly. Older levels are
                                  # archaeology, not support.
    'swing_min_gap_pct': 1.0,     # A swing nearer than this to spot is not a
                                  # target — booking there pays the round-trip
                                  # spread for nothing.
    'swing_min_retained_pct': 40.0,
                                  # The shortened TP must keep at least this
                                  # share of the original spot->ST run.
                                  # Measured on 75 signal-like symbols: 57% had
                                  # a swing in the way and the shortening ran
                                  # as high as 82% of the journey. Keeping a
                                  # fifth of the distance is not a shortened
                                  # target, it is a much worse trade — under
                                  # BCS the short strike is still chosen at the
                                  # ST line, so max gain would need a move the
                                  # TP is set never to wait for. Below the
                                  # floor the TP is left ALONE (conservative)
                                  # and the level is reported to the vetting
                                  # agent instead: support that close to spot
                                  # says the trade has little room.
    'attraction_enabled': True,   # Measure whether this symbol actually gets
                                  # pulled back to its ST line, and hand it to
                                  # the vetting agent. The magnet IS the trade;
                                  # some symbols trend away from ST for months
                                  # and nothing was measuring which kind a
                                  # symbol was.
    'attraction_horizon_bars': 8, # Candles allowed for the return. ~2 months
                                  # weekly, which is the DTE band these trades
                                  # actually live in.
    'attraction_gap_pct': 3.0,    # How far from ST a candle must close to open
                                  # an episode. Matches the trigger band, so the
                                  # statistic describes the setup being traded.
    'attraction_min_episodes': 4, # Below this the rate is reported WITH its
                                  # sample size and flagged thin. 2 of 3 is not
                                  # a 67% hit rate.
    'spot_sl_enabled': False,     # master switch for the adverse-spot SL (off: debit floor only)
    'spot_sl_pct': 0.03,          # adverse spot move from entry that triggers SL (only if enabled)
    'debit_sl_pct': 0.50,         # exit if option mid drops to this fraction of entry debit
    'time_sl_days_before_expiry': 6,
                                 # TRADING SESSIONS, not calendar days (the
                                 # count was calendar until 2026-08-12, so a
                                 # Friday read "3 days left" when one session
                                 # remained). Indian stock options are
                                 # PHYSICALLY settled and the exchange ramps a
                                 # delivery margin on ITM legs over the last
                                 # ~4 sessions.
                                 #
                                 # 4 -> 6 on 2026-08-29 (M10), from sourced
                                 # research rather than from the estimate this
                                 # comment used to carry. NSE Clearing levies
                                 # 10% at EOD of E-4, 25% at E-3, 45% at E-2,
                                 # 70% at E-1 (Risk Management FAQ Q24; F&O
                                 # circular NCL/CMPT/73997, 30 Apr 2026), and
                                 # THREE facts move the number:
                                 #
                                 #  1. the base is the long ITM leg at its
                                 #     STRIKE - full contract value, not the
                                 #     width and not the debit. For this book
                                 #     that is ~Rs 2.82L demanded at E-3
                                 #     against a Rs 2L account.
                                 #  2. the BROKER does not net the legs even
                                 #     though the exchange does, so budget the
                                 #     gross per-leg figure.
                                 #  3. a holiday moves each tranche EARLIER
                                 #     while `sessions_to_expiry` counts
                                 #     weekdays and moves the close LATER. The
                                 #     two errors compound in one direction,
                                 #     and 4 sat inside the ramp outright.
                                 #
                                 # 5 is only clear in a holiday-free month.
                                 # This is a FLOOR, never a ceiling: an ITM
                                 # long leg closes EARLIER (see M10), never
                                 # later, because the spread that most rewards
                                 # holding is the one with the most delivery
                                 # exposure.
                                 # Raise to 5 if your broker levies intraday.
    # 8 -> 4 on 2026-08-29 (M9; owner, 2026-08-27: "start at 4 slots, move to
    # 8 once it is going well"). LIVE-only by construction, so this does not
    # slow the paper book's evidence: see the note below.
    'max_open_trades': 4,        # LIVE guidance only. PAPER intentionally does
                                 # NOT cap entries — capturing every signal keeps
                                 # the validation P&L unbiased (a cap would skew
                                 # which trades the track record contains).
    # ── Portfolio capital (Phase 2, 2026-08-26) ─────────────────────────
    #
    # Owner: "We shall load 2L as initial and then reserve some per trade...
    # as the capital grows to say 4L then it can auto go for 2 lots and so
    # on... so capital based risk and so on + compounding", with Rs 25,000 per
    # trade and 1 lot for now.
    #
    # Those numbers are ONE scheme, so they are stored as ratios:
    #
    #     Rs 2,00,000 / 8 slots = Rs 25,000 each = 12.5% of capital
    #
    # Store the ratio and everything scales on one number. Store three rupee
    # figures and they drift apart the first time capital moves, silently: 8
    # slots at a stale Rs 25,000 against Rs 4,00,000 is a book running at half
    # size with nothing announcing it.
    # Place the ENTRY orders instead of printing a ticket (Phase 3).
    #
    # OFF, and it is the third switch that has to be thrown deliberately --
    # alongside `--dry-run` off the monitor crontab and
    # `exits_managed_externally`. Unlike those two, this one arms code that
    # OPENS positions, so `bcs.entry_executor.entries_allowed` fails CLOSED on
    # it: an unreadable config means no entries, never "default to yes".
    'auto_entry': False,
    'capital_rupees': 200000,
    # One lot per this much capital. 2L -> 1 lot, 4L -> 2, and so on, floored.
    'capital_per_lot': 200000,
    # 12.5% = Rs 25,000 at 2L, which is the owner's figure, and it stays 1/8th
    # of the book as capital grows instead of quietly becoming 1/16th.
    'max_trade_pct': 12.5,
    # 100%: 8 slots x 12.5% is the whole account by construction, so the count
    # cap and the per-trade cap are the limits that actually bind. Lower this
    # to hold cash back.
    'max_deployed_pct': 100,
    # Safety ceiling on the DERIVED lot count. A stray zero in capital_rupees
    # must not be able to order 50 lots.
    'max_lots_hard': 5,
    # Add realised net P&L to the base capital, which is what makes size grow
    # on its own. OFF until it is watched -- `describe()` reports what it would
    # be every cycle, the same alert-only-first discipline every other control
    # here shipped with.
    'compound': False,
    # Codifies observed behaviour rather than inventing a rule: 10 of 10 cohort
    # entries were distinct stocks, and the scanner already dedups on open
    # positions. This makes that a limit instead of a coincidence.
    'max_open_per_stock': 1,
    'max_watching_signals': 25,
    'watch_max_age_days': 45,  # A watching/triggered row whose symbol has
                               # stopped quoting can never drift- or
                               # stale-cancel (both need a gap, which needs
                               # a price), so it holds one of the 25 slots
                               # and its stock's dedup entry forever. Age is
                               # the only bound that survives a dead feed.
                               # 45d comfortably outlives a real approach:
                               # the whole watch band is a few % of spot.
    'scan_interval_sec': 300,    # 5 min between Chartink scans
    'monitor_interval_sec': 300, # 5 min between LTP/monitor checks
    'enabled_directions': ['CE', 'PE'],
    'enabled_timeframes': ['monthly', 'weekly'],
    'st_period': 10,
    'st_multiplier': 3,
    'vet_enabled': True,         # Claude vetting layer master switch. Lives HERE
                                 # rather than env-only so it cannot be ON in the
                                 # cron and OFF in a manual `python -m zebra run`
                                 # — a half-enabled fleet is worse than a dark
                                 # one: the unvetted process would fire, and burn
                                 # the consume-once flag on, the very exit the
                                 # vetted process is deliberately holding.
                                 # ZEBRA_VET_ENABLED still overrides, for a
                                 # one-off test without editing config.
                                 # True since 2026-08-27 and MIRRORED in the
                                 # tracked defaults file, so the two sources
                                 # agree and the switch is auditable from git.
                                 # See the block comment at the top of this
                                 # module for why ON is the safe default here.
    'veto_shadow_days': 30,      # Horizon for scoring a VETOED signal. A veto
                                 # blocks a trade that never happens, so without
                                 # a shadow the layer's most important decisions
                                 # are the only ones with no evidence. ~30d is
                                 # one option cycle: long enough for the thesis
                                 # to play out, short enough to score this month.
    'event_refresh_sec': 7200,   # Event calendar staleness bound (2h).
    'event_horizon_days': 10,    # How far ahead an event counts as relevant.
    'review_adverse_pct': 0.04,  # Position-review pre-filter: |move| from entry
                                 # spot that makes a position worth a fresh look.
    'eod_review_enabled': True,  # DAILY end-of-session news sweep over EVERY
                                 # open position, whatever price has done.
                                 #
                                 # Everything else in the review pre-filter is
                                 # REACTIVE TO PRICE: a 4% adverse move, a
                                 # give-back, an event the calendar already
                                 # carries, the last cheap window before the
                                 # time stop. News that has not moved the tape
                                 # yet trips none of them, and neither does an
                                 # event class the calendar has no type for.
                                 #
                                 # #472 ANGELONE lost 65.8% overnight on the
                                 # August monthly business update. The calendar
                                 # had no `monthly_update` class, so the events
                                 # agent never looked for one; price had done
                                 # nothing by the close, so no price trigger
                                 # fired; and the review agent — which asks
                                 # exactly the right question — was therefore
                                 # never asked it. The gap was the PRE-FILTER,
                                 # not the agent.
    'eod_review_start': '15:00',  # IST wall clock, "HH:MM". Late enough that
                                 # the session's news is out, early enough that
                                 # a verdict can still be acted on before the
                                 # 15:30 close — the alert asks a human to run
                                 # `zebra close`, so a 15:35 verdict is a
                                 # verdict about tomorrow's open. Owner
                                 # decision, 2026-09-04.
    'eod_review_last_request': '15:15',
                                 # IST wall clock. NO scan is REQUESTED after
                                 # this, because a verdict that lands after the
                                 # session is over cannot be delivered.
                                 #
                                 # The alert is only sent by a LATER sweep —
                                 # `run()` requests in one loop and delivers in
                                 # the next — and `monitor._is_market_open`
                                 # stops the cycle after 15:30. So a scan
                                 # spawned at 15:25 whose agent lands at 15:31
                                 # is not Telegrammed until ~09:15 the next
                                 # morning, silently, having been asked
                                 # precisely so a human could act before the
                                 # close. 15:15 leaves ~3 cycles for the agent
                                 # (measured round trip ~4m50s) and at least
                                 # one delivering cycle after it.
    'eod_review_model': 'sonnet',  # The routine daily sweep. Sonnet, like the
                                 # calendar and the post-mortems: reading the
                                 # day's news against a written checklist is
                                 # fact-collection, and running Opus over every
                                 # open position every session would spend the
                                 # weekly limit on "nothing changed". Opus
                                 # (VET_MODEL) stays on the price-triggered
                                 # reviews, where something has already
                                 # happened and the call is about money.
    'eod_review_max_per_cycle': 2,  # Scan spawns per 5-minute sweep. The whole
                                 # book qualifies the moment the clock passes
                                 # `eod_review_start`, so without a stagger a
                                 # full book starts N detached agents in one
                                 # cycle on a Pi that also runs the live-money
                                 # monitor.
                                 #
                                 # 2, not the deferrable cap of 3, and the
                                 # missing slot is the point: at the full cap
                                 # three quiet low-id scans take every
                                 # deferrable slot and a genuine 6% adverse
                                 # review on a higher id is REFUSED in the same
                                 # cycle — the cheap sweep crowding out the
                                 # expensive judgement it was never meant to
                                 # compete with, with no alarm anywhere because
                                 # the budget was working as designed. CLAMPED
                                 # at import to DEFERRABLE_AGENT_CAP - 1, so
                                 # raising this cannot reintroduce that.
                                 #
                                 # 8 positions at 2 a cycle is 4 cycles —
                                 # 15:00, 15:05, 15:10, 15:15 — which is
                                 # exactly the request window. Deferred
                                 # positions re-qualify next cycle because
                                 # nothing was stamped for them.
    'auth_warn_days': 3,         # Telegram this many days before the Claude CLI
                                 # credential expires (user re-logs in manually).
    'entry_vet_ttl_sec': 1800,   # How long an ALLOWED **ENTRY** verdict stays
                                 # valid. Owner decision, 2026-09-01: *"vet has
                                 # to be realtime"*.
                                 #
                                 # There was no bound at all, and it showed:
                                 # signal #472 ANGELONE triggered 08-31 14:05,
                                 # was vetted 14:07 (decision #109), sat at
                                 # `triggered` overnight, and ENTERED on
                                 # 09-01 at 13:50 -- on a verdict 23h45m old,
                                 # whose own red flag was about a book that no
                                 # longer existed ("only one lot sitting at the
                                 # short leg's best bid RIGHT NOW").
                                 #
                                 # The mechanical gates are re-run at entry, so
                                 # price drift was caught; what was stale is the
                                 # AGENT's judgement. VETTING.md already tells
                                 # the agent "your verdict covers this episode,
                                 # not this trade" -- for EXITS. The entry side
                                 # simply never enforced it.
                                 #
                                 # 30 min: the normal path is trigger -> answer
                                 # in 2-5 min -> enter on the next 5-minute
                                 # cycle, so ~10 min is typical and 30 leaves
                                 # generous room. It cannot survive a session
                                 # boundary, which is the case that matters.
                                 # Expiry RE-REQUESTS; it never vetoes.
    'exit_vet_ttl_sec': 900,     # How long a terminal EXIT verdict stays valid.
                                 # A verdict is a judgement about the book AT
                                 # THAT MOMENT, so it must not authorise an exit
                                 # days later on a book that has since rotted —
                                 # precisely the NHPC shape this layer exists to
                                 # catch. Must comfortably exceed one
                                 # monitor_interval_sec (the verdict has to
                                 # survive until the next cycle fires the exit)
                                 # and stay far below a session.
    'exit_hold_ttl_sec': 86400,  # How long an ESCALATED exit (deferred to the
                                 # cap, human asked) keeps holding without
                                 # re-vetting. Deliberately far longer than
                                 # exit_vet_ttl_sec: on a persistently
                                 # untradeable book the short TTL made the
                                 # episode restart — agent and all — every 15
                                 # minutes, ~30 Fable runs/day for one position
                                 # to reach the same escalation it already
                                 # reached. The human has been told and the
                                 # loss is capped, so holding quietly until
                                 # tomorrow is both cheaper and safer.
    'exit_vet_max_hold_sec': 900,
                                 # THE STOP IS BOUNDED, AND SO IS THE WAIT FOR
                                 # PERMISSION TO TAKE IT. Owner decision,
                                 # 2026-08-29. `needs_exit_vet` flags every
                                 # non-spot-corroborated exit, and the cohort's
                                 # only loss-side exits ARE value-based
                                 # (spot_sl_enabled is False) -- so EVERY stop
                                 # waits on an agent, and a single `defer`
                                 # means a later timeout no longer fails open:
                                 # it counts as another failure to verify and
                                 # lands on 'hold', waiting on a human while
                                 # the position loses. ASHOKLEY #390 went -50%
                                 # to -75% over three cycles on a dead agent.
                                 #
                                 # The vet is ADDITIVE, never load-bearing --
                                 # the deterministic guards cleared the exit
                                 # before it was ever asked. An unbounded hold
                                 # inverts that. 900s is the designed sequence
                                 # run to completion (request + up to
                                 # exit_max_defers re-checks at roughly one
                                 # monitor_interval each); past it, the exit
                                 # proceeds on the guards alone and says so
                                 # loudly. PER SESSION, not per episode: an
                                 # undated budget banks Friday's wait against
                                 # Monday's first poll, which is the residue
                                 # sweep's dating lesson exactly. 0 disables
                                 # the bound, restoring the old behaviour
                                 # without a code change.
    'exit_vet_incycle_wait_sec': 120,
                                 # M12. How long a CRON-PACED cycle may wait,
                                 # in-cycle, for a verdict it just requested.
                                 # Measured: the agent answers in ~1m50s of a
                                 # ~4m50s round trip, and the other ~3 min is
                                 # purely waiting for the next 5-minute cycle.
                                 # This spends the poll thread instead.
                                 #
                                 # NOT used by `bcs/spread_monitor.py`, which
                                 # polls every 5s: there the verdict is picked
                                 # up within 5 seconds anyway, and blocking
                                 # that loop would stop watching every OTHER
                                 # position for two minutes to save nothing.
                                 # The cap is per CYCLE, not per trade, so
                                 # several triggering positions cannot push a
                                 # 5-minute cron past its own interval (whose
                                 # `flock -n` then SKIPS the next run).
                                 # 0 disables it.
    'vet_timeout_sec': 600,      # Fail-open deadline for one agent run. NOT
                                 # shortened with child_kill_sec: VETTING.md
                                 # itself budgets the agent ~5 min and promises
                                 # 10, and the checklist is real work (several
                                 # CLI calls plus live event research). A
                                 # deadline under the work just converts every
                                 # vet into a timeout.
    'child_kill_sec': 555,       # Hard wall-clock bound on a spawned CLI. Past
                                 # this the process is killed: the deadline
                                 # fails the MARKER open, but a hung child
                                 # would otherwise live until reboot and this
                                 # Pi also runs live-money bots.
                                 # 900 -> 555 on 2026-08-13. It must be UNDER
                                 # vet_timeout_sec (600), not over: an agent
                                 # outliving its own marker can land a verdict
                                 # on the NEXT attempt's PENDING window, and
                                 # record_verdict cannot tell the two apart.
                                 # 600 - 45s covers `timeout`'s -k grace. The
                                 # clamp below enforces this regardless.
    'max_concurrent_agents': 5,  # LIVE agents box-wide, not starts-per-window.
                                 # Raised 3 -> 5 on 2026-08-13 at the owner's
                                 # call. Each is a node process of a few hundred
                                 # MB and this Pi also runs live-money bots —
                                 # MEASURE RSS before raising further.
    'entry_queue_drop_after_sec': 3600,
                                 # How long a refused/timed-out ENTRY may wait
                                 # for a verdict before it is dropped. Anchored
                                 # at the FIRST vet request and never moved, so
                                 # requeues cannot walk it forward. An hour of
                                 # failures means the layer is broken, and the
                                 # drop telegraphs — a fail-CLOSED entry path is
                                 # only safe while the halt cannot be silent.
    'entry_vet_max_attempts': 2,
                                 # Spawned agents that may time out on one
                                 # signal. One is an outage blip; two on a
                                 # freshly re-snapshotted book is a broken
                                 # layer — stop burning slots and drop it.
    'fee_rates': {
        # Published Zerodha equity-OPTION charges. ESTIMATES: the only
        # authority is a real contract note, and these have changed before
        # (the Apr 2026 round is in this repo's history). Stamped per trade
        # with a model version so a correction can be RECOMPUTED from the
        # stored leg prices instead of guessed at later.
        # VERIFY AGAINST A LIVE CONTRACT NOTE BEFORE ANY GO-LIVE DECISION.
        'brokerage_per_order': 20.0,   # flat, per executed order
        'stt_sell_pct': 0.10,          # SELL side only, on premium
        'exchange_pct': 0.03503,       # NSE options, both sides
        'sebi_pct': 0.0001,            # Rs 10 per crore
        'stamp_buy_pct': 0.003,        # BUY side only
        'gst_pct': 18.0,               # on brokerage + exchange + SEBI ONLY
    },
    'attraction_horizon_days': 60,
                                 # Trading days allowed for the DAY-clock
                                 # velocity measure — ~3 months, well past any
                                 # DTE this book trades, so the median is not
                                 # truncated by the window itself.
    'attraction_timeframes': ['weekly'],
                                 # Where the magnet statistic is measured at
                                 # all. Monthly is excluded deliberately: 6Y of
                                 # monthly candles is ~72 bars, an episode
                                 # consumes 9+, and it reached a usable sample
                                 # on 5.4% of symbols. Monthly signals are rare
                                 # besides (45 weekly vs 6 monthly on a typical
                                 # scan), so the section is reported as
                                 # NOT MEASURED rather than as missing data.
    'cli_block_scan_window_sec': 1800,
                                 # How far back to look for a Claude usage-limit
                                 # refusal in the per-spawn transcripts. Wide
                                 # enough to still see the block that killed a
                                 # spawn two cycles ago, short enough that
                                 # yesterday's is never resurrected.
    'cli_block_grace_sec': 600,  # Extra time a queued entry gets AFTER the
                                 # stated reset, so at least one cycle actually
                                 # spawns before the drop clock resumes. On
                                 # 2026-08-14 HAVELLS was dropped at 14:05
                                 # against a 14:10 reset — five minutes short of
                                 # the attempt it had been waiting an hour for.
    'agent_reserve': 2,          # Slots the DEFERRABLE_CHANNELS may never take,
                                 # so an entry/exit decision always has room.
                                 # Batch channels therefore cap at 5-2=3, which
                                 # still drains a 24-position review sweep while
                                 # leaving the trading decisions unblocked.
    'vet_model': 'opus',         # Entry/exit decisions. Fable until
                                 # 2026-08-13; the owner's call to move to
                                 # Opus. Fable draws down the weekly limit
                                 # materially faster, and the vetting job is
                                 # mostly research-and-check (events, liquidity,
                                 # attraction) against a written checklist —
                                 # which is the shape Opus is for. Reserve Fable
                                 # for open-ended design work.
    'event_model': 'sonnet',     # Routine calendar refresh.
    'postmortem_model': 'sonnet',  # Classifying settled trades
                                 # against a CLOSED tag list is
                                 # fact-collection, not a call
                                 # about money.
    'mfe_confirm_polls': 2,      # Max-favourable-excursion peak tracking. A
                                 # peak is a MAX, so one garbage-high print
                                 # poisons it PERMANENTLY — unlike a low print,
                                 # which the DEBIT-SL debounce already handles.
                                 # A proposed peak beyond the jump bound below
                                 # must therefore repeat on this many polls,
                                 # and the window MINIMUM is what gets stored.
                                 # Same shape as bcs.update_trail's gate.
    'mfe_jump_mult': 1.5,        # Structure mid: implausible to rise >50% above
                                 # the running peak inside one ~5-min poll.
    'mfe_spot_jump_pct': 0.15,   # Underlying: same idea in price terms. A real
                                 # 15% single-poll move confirms on the next
                                 # poll and is recorded ~5 min late; a bad tick
                                 # never repeats and is dropped.
    'trail_enabled': True,       # Gain-anchored trailing stop. BCS only — a
                                 # zebra back-ratio has no capped payoff, so
                                 # "fraction of max gain" is undefined there.
                                 # PAPER auto-closes; in LIVE mode the same
                                 # code alerts and closes nothing, because
                                 # _paper_auto_close no-ops. No new automated
                                 # close path in the real-order system.
    'trail_engage_frac': 0.25,   # Arm once the PEAK gain reaches this share of
                                 # max gain (width - debit). Deliberately NOT
                                 # the live monitor's 2x-debit rule: that lands
                                 # at 43% of max gain on a 30% d/w spread but
                                 # 82% on a 45% one, tightening exactly as the
                                 # payoff shrinks, and it would have engaged on
                                 # only 2 of 32 closed shadows.
                                 #
                                 # 0.50 -> 0.25 on 2026-09-03, REPLAYED not
                                 # reasoned. At 0.50 the trail had armed on
                                 # 0 of 19 cohort positions — decorative, and
                                 # unreachable BY CONSTRUCTION: the TP fires at
                                 # the short strike with 27-36 DTE left, where
                                 # the spread holds ~55% of width, so the peak
                                 # gain is (0.55w - d)/(w - d) ~ 25% at d/w 40.
                                 # Arming at 50% needs V/w ~70%, which this TP
                                 # rule never lets a position reach. Observed
                                 # cohort peak: median ~25%, max 40.2%.
                                 #
                                 # Replayed over all 15 cohort closes on their
                                 # real 5-minute value paths (9,449 POLL
                                 # observations):
                                 #     engage 0.50/0.30 -> Rs      0, arms never
                                 #     engage 0.25      -> Rs +1,080, clips none
                                 #     engage 0.20      -> Rs -4,400, clips 2
                                 #     engage 0.15      -> Rs -7,537, clips 4
                                 #
                                 # ⚠ THIS SITS ON A CLIFF, one notch above a
                                 # Rs -4,400 outcome, and its whole +Rs 1,080
                                 # comes from ONE position (COALINDIA #440,
                                 # which gapped 7.85 -> 3.15 overnight). It is
                                 # retain-INSENSITIVE for the same reason —
                                 # identical result at retain 0.40/0.50/0.60/
                                 # 0.70 — because the only position that arms
                                 # gaps straight through every level. So this
                                 # is really a gap-detector, not a trail.
                                 # `test_the_trail_engage_cliff` pins the
                                 # neighbourhood. DO NOT LOWER IT without
                                 # re-running the replay.
                                 # REVISIT AT ~30 COHORT CLOSES.
    'trail_retain_frac': 0.50,   # Keep this share of the peak gain. Must stay
                                 # below 1: a trail sitting AT the peak fires
                                 # on the first tick down.
}


def _load_runtime() -> dict:
    cfg = dict(_DEFAULTS)
    # TWO LAYERS: config/zebra_config.defaults.json (tracked, secret-free) under
    # config/zebra_config.json (untracked, secrets). common/layered_config.py.
    file_cfg = layered_config.load('zebra_config')
    if file_cfg:
        try:
            for key in _DEFAULTS:
                if key in file_cfg:
                    cfg[key] = file_cfg[key]
            unknown = [k for k in file_cfg if k not in _DEFAULTS
                       and k not in ('google_drive', 'telegram')]
            if unknown:
                logger.warning("zebra_config.json: unknown keys ignored: %s", unknown)
        except Exception as e:
            logger.warning("Failed to load %s, using defaults: %s", CONFIG_FILE, e)
    return cfg


#: The MERGED file config (tracked defaults under the untracked overlay), kept
#: so callers that need a non-`_DEFAULTS` block do not re-read the files - and,
#: more importantly, do not read ONE LAYER. `zebra/monitor._send_telegram`
#: opened `CONFIG_FILE` directly for `telegram.enabled`, which is the overlay
#: alone: an edit to the tracked defaults silently did nothing, and on a box
#: whose overlay has been trimmed to secrets (all of them, since 2026-08-26)
#: the key was not there at all. M8.
_file_cfg = layered_config.load('zebra_config', warn_on_shadow=False) or {}

_runtime = _load_runtime()

def telegram_enabled() -> bool:
    """Telegram's master switch, resolved through BOTH config layers.

    A FUNCTION, not an import-time constant. The code this replaced opened
    `CONFIG_FILE` on every send — which was wrong about the LAYER (overlay
    only) but right about the TIMING: `zebra loop` is long-lived, and an
    import-time constant means an operator muting alerts mid-session has to
    restart the process for it to take effect. Fixing the layer bug by
    freezing the value would have traded one defect for another.

    Only `false` mutes. `None`/absent means SEND: absence has always meant
    send, a config that stops existing must not silently mute a safety
    channel, and `bool(None)` would have made a null key do it.
    """
    try:
        cfg = layered_config.load('zebra_config', warn_on_shadow=False) or {}
    except Exception:
        return True             # unreadable config never mutes the alerts
    return (cfg.get('telegram') or {}).get('enabled') is not False


def _positive_finite(key: str, val, default: float) -> float:
    """Coerce a gate threshold to a positive finite number, else the default.

    A threshold that arrives from JSON as a string/null raises TypeError on
    every comparison — which would suppress EVERY shadow with a misleading
    'shadow build failed' reason. Worse, json.load happily parses the NaN
    literal, and NaN compares False against everything, which silently turns
    a hard gate into a no-op. Fail LOUD and fall back to the tested default
    instead; never let a config typo disarm a gate. (bool is excluded
    explicitly because it is an int subclass: true would gate at 1%.)
    """
    if isinstance(val, bool) or not isinstance(val, (int, float)) \
            or not math.isfinite(val) or val <= 0:
        logger.warning("zebra_config.json: %s=%r is not a positive finite "
                       "number — using default %g", key, val, default)
        return float(default)
    return float(val)


def _positive_int(key: str, val, default: int) -> int:
    """Same guard for the count-valued keys, preserving int type.

    These feed range()/timedelta()/sleep() and integer comparisons, so they
    must not silently become floats. A non-integral value (st_period=10.5) is
    rejected rather than rounded — quietly changing a lookback window is the
    kind of drift nobody notices until a backtest stops reconciling.
    """
    checked = _positive_finite(key, val, float(default))
    if checked != int(checked):
        logger.warning("zebra_config.json: %s=%r must be a whole number — "
                       "using default %d", key, val, default)
        return int(default)
    return int(checked)

# ── Exports ───────────────────────────────────────────────────────────────
def _strict_bool(key: str) -> bool:
    """A LIVE/PAPER switch may only be a real boolean.

    `PAPER_MODE = _runtime['paper_mode']` took the value raw, so every numeric
    threshold in this file was validated while the one key that decides whether
    real orders are placed was not. `"paper_mode": 0` — or `"false"`, or `""`,
    or a stray `null` from a half-written config — silently means LIVE, and the
    only visible difference is that the bot starts placing orders. JSON has a
    real boolean type; anything else here is a typo, and a typo must not be
    able to arm the money path. Unparseable falls back to the SAFE default.
    """
    val = _runtime.get(key, _DEFAULTS[key])
    if isinstance(val, bool):
        return val
    logger.warning("zebra_config.json: %s=%r is not true/false — using the "
                   "safe default %r. A LIVE/PAPER switch is never inferred.",
                   key, val, _DEFAULTS[key])
    return bool(_DEFAULTS[key])


PAPER_MODE = _strict_bool('paper_mode')
# `_strict_bool`, not a raw read, for the same reason PAPER_MODE is: this
# decides which process closes real positions, and `"exits_managed_
# externally": 0` must not be able to answer that question by accident.
EXITS_MANAGED_EXTERNALLY = _strict_bool('exits_managed_externally')
# DECOMMISSIONED 2026-08-27 (owner): the back-ratio structure — BUY 2x ITM +
# SELL 1x ATM — is retired. `entry_structure` is now SINGLE-VALUED: 'bcs' is
# the only accepted value and anything else, including the old 'zebra', is
# refused here rather than routed to code that no longer exists.
#
# The KEY is deliberately kept. Removing a config key is the owner's call, not
# a side effect of a code change, and an unknown-key preflight would flag its
# disappearance on the Pi. Keeping it also means the refusal is LOUD: an
# overlay still saying 'zebra' logs an error every start-up instead of silently
# behaving as 'bcs'.
_ENTRY_STRUCTURES = ('bcs',)
_raw_struct = str(_runtime['entry_structure']).strip().lower()
if _raw_struct == 'zebra':
    logger.error("entry_structure='zebra' is RETIRED — the back-ratio "
                 "structure was decommissioned on 2026-08-27 and its entry, "
                 "pricing and alert paths are gone. Using 'bcs'. Remove the "
                 "key from the overlay, or set it to 'bcs'.")
    _raw_struct = 'bcs'
elif _raw_struct not in _ENTRY_STRUCTURES:
    logger.warning("entry_structure=%r is not one of %s — falling back to %r",
                   _runtime['entry_structure'], _ENTRY_STRUCTURES,
                   _DEFAULTS['entry_structure'])
    _raw_struct = _DEFAULTS['entry_structure']
ENTRY_STRUCTURE = _raw_struct
BCS_PAPER_ENABLED = _runtime['bcs_paper_enabled']
_raw_alerts = _runtime['alert_structures']
if isinstance(_raw_alerts, str):     # common JSON typo: "bcs" not ["bcs"]
    _raw_alerts = [_raw_alerts]
ALERT_STRUCTURES = [s for s in _raw_alerts if s in ('zebra', 'bcs')]
if list(_raw_alerts) != ALERT_STRUCTURES:
    logger.warning("alert_structures: unknown entries ignored in %s",
                   _raw_alerts)
if not ALERT_STRUCTURES and PAPER_MODE:
    logger.warning("alert_structures is empty — NO per-trade Telegram alerts "
                   "will fire in paper mode (EOD reports unaffected)")
if 'bcs' in ALERT_STRUCTURES \
        and 'zebra' not in ALERT_STRUCTURES and not BCS_PAPER_ENABLED:
    logger.warning("alert_structures=['bcs'] but bcs_paper_enabled=false — "
                   "no BCS trades exist to alert on; paper mode will be "
                   "silent. Enable bcs_paper_enabled or add 'zebra'.")
# Every numeric threshold goes through a validator. `zebra_config.json` is
# hand-edited and Drive-synced, so a typo is a live risk, and an unvalidated
# threshold fails in the worst possible way: json.load accepts the bare NaN
# literal, NaN compares False against everything, and the gate silently becomes
# a no-op that still LOOKS configured. Fail loud, fall back to the tested
# default. (2026-08-10, found by review before it ever ran.)
def _num(key: str) -> float:
    return _positive_finite(key, _runtime[key], _DEFAULTS[key])


def _int(key: str) -> int:
    return _positive_int(key, _runtime[key], _DEFAULTS[key])


def _non_negative_int(key: str) -> int:
    """`_int`, except that 0 is a MEANING here rather than a typo.

    Two keys document 0 as their own disable switch -- `exit_vet_max_hold_sec`
    ("0 disables the bound, restoring the old behaviour without a code change")
    and `exit_vet_incycle_wait_sec` ("0 disables it"). Both CONSUMERS honour it:
    `vet._apply_hold_budget` returns early on `if not budget` and the M12 wait
    treats 0 as never-block.

    Only the loader could not deliver it. `_positive_finite` rejects `val <= 0`
    and substitutes the default, so an owner writing `"exit_vet_max_hold_sec":
    0` got 900 back plus one WARNING in a cron log nobody tails -- and believed
    every value stop now waits for a human, when in fact every one of them
    proceeds after 15 minutes. A documented switch that silently does the
    OPPOSITE of what it says is worse than no switch, and this one is on the
    only loss-side exits the cohort has.

    Everything else `_positive_int` refuses is still refused, for its reasons:
    bool (an int subclass), non-numeric, NaN/inf (NaN compares False against
    everything and would silently disarm the bound), negative, and fractional.
    """
    val = _runtime.get(key, _DEFAULTS[key])
    if isinstance(val, bool) or not isinstance(val, (int, float)) \
            or not math.isfinite(val) or val < 0 or val != int(val):
        logger.warning("zebra_config.json: %s=%r is not a whole number >= 0 "
                       "— using default %d", key, val, _DEFAULTS[key])
        return int(_DEFAULTS[key])
    return int(val)


def _parse_hhmm(raw):
    """`"HH:MM"` -> `(hour, minute)`, or None if it is not a wall-clock time."""
    if not isinstance(raw, str):
        return None
    parts = raw.strip().split(':')
    if len(parts) != 2 or not all(p.strip().isdigit() for p in parts):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return (hour, minute)


def _hhmm(key: str) -> tuple:
    """A wall-clock config value as an (hour, minute) tuple on the IST clock.

    LOUD, then the tested default — the same shape as `_num`/`_int`, and for a
    sharper version of the same reason. The alternative (return None and let
    the consumer skip) means a typo'd `"3 PM"` DISABLES the daily sweep, on a
    box where the switch still reads `eod_review_enabled: true` and nothing
    ever fires. That is the exact failure the sweep was built to close: a layer
    that looks armed and is dark. Falling back keeps it running at the known-
    good time and says so at ERROR, which is a state an operator can see.

    Turning the sweep OFF is `eod_review_enabled: false`, not a broken time.
    """
    raw = _runtime.get(key, _DEFAULTS[key])
    parsed = _parse_hhmm(raw)
    if parsed is None:
        logger.error("zebra_config.json: %s=%r is not an \"HH:MM\" IST time "
                     "— using default %r. The daily review sweep is STILL "
                     "RUNNING, at the default time; set eod_review_enabled "
                     "to false if you meant to stop it.",
                     key, raw, _DEFAULTS[key])
        parsed = _parse_hhmm(_DEFAULTS[key])
    return parsed


WATCH_GAP_MAX = _num('watch_gap_max')
TRIGGER_GAP_MAX = _num('trigger_gap_max')
STALE_GAP_MIN = _num('stale_gap_min')
FRESHNESS_DAYS = _int('freshness_days')
# The freshness floor zebra passes into magnet's `check_freshness`.
#
# DEFAULTS TO THE VALUE THAT WAS ALREADY IN FORCE (magnet's ENTRY_GAP = 0.04),
# so this changes nothing today — it only makes the number local, visible and
# owned. It was inherited silently: zebra advertises a [3%, 5%] band via
# `stale_gap_min`, and its own gate honours that, but `check_freshness` then
# rejected everything under magnet's 4% as "missed approach". The effective
# band was [4%, 5%], the 3-4% band was discarded, and the skip was counted as
# `not_fresh` — indistinguishable from ordinary freshness filtering.
#
# THE OWNER'S CALL, not a silent fix: setting this to STALE_GAP_MIN (0.03)
# restores the advertised band and will admit meaningfully more signals. That
# is a strategy change and wants measuring, so it is left at the status quo.
FRESH_ENTRY_GAP = _num('fresh_entry_gap')
MIN_DTE = _int('min_dte')
MAX_DTE = _int('max_dte')
MIN_LEG_OI = _int('min_leg_oi')
OPTIONS_CSV_MAX_AGE_DAYS = _int('options_csv_max_age_days')
MAX_LEG_SPREAD_PCT = _num('max_leg_spread_pct')
BCS_MAX_DEBIT_TO_WIDTH_PCT = _num('bcs_max_debit_to_width_pct')
BCS_MAX_ENTRY_COST_PCT = _num('bcs_max_entry_cost_pct')
assert 0 < BCS_MAX_ENTRY_COST_PCT < 100, \
    "BCS_MAX_ENTRY_COST_PCT is a percentage of max gain; 0 blocks every trade"
assert 0 < BCS_MAX_DEBIT_TO_WIDTH_PCT < 100, \
    "BCS_MAX_DEBIT_TO_WIDTH_PCT is a percentage of width"
# Measured, not enforced — see the defaults block for the full rationale and
# the REVISIT AT ~30 COHORT CLOSES note.
BCS_TP_VALUE_FRAC_OF_WIDTH = _num('bcs_tp_value_frac_of_width')
BCS_MIN_GAIN_AT_TP_PCT = _num('bcs_min_gain_at_tp_pct')
assert 0 < BCS_TP_VALUE_FRAC_OF_WIDTH < 1, \
    "BCS_TP_VALUE_FRAC_OF_WIDTH is a fraction of width, exclusive of 0 and 1"
assert BCS_MIN_GAIN_AT_TP_PCT > 0, \
    "BCS_MIN_GAIN_AT_TP_PCT is a percentage gain over the debit"
TP_TARGET = _runtime['tp_target']


def _cohort_start() -> str:
    """The cohort boundary, validated as a real ISO date.

    A typo here would silently mis-stamp every future entry and quietly corrupt
    the only measurement the paper run exists to produce, so it is parsed
    rather than trusted — same discipline as `_strict_bool` on `paper_mode`.
    Unparseable falls back to the default instead of stamping garbage.
    """
    val = _runtime.get('cohort_start', _DEFAULTS['cohort_start'])
    try:
        datetime.strptime(str(val), '%Y-%m-%d')
        return str(val)
    except (TypeError, ValueError):
        logger.warning("cohort_start %r is not a YYYY-MM-DD date — falling "
                       "back to %s", val, _DEFAULTS['cohort_start'])
        return _DEFAULTS['cohort_start']


COHORT_START = _cohort_start()
ALERTS_COHORT_ONLY = _strict_bool('alerts_cohort_only')
EOD_OPEN_POSITIONS = _strict_bool('eod_open_positions')
SWING_TP_ENABLED = bool(_runtime['swing_tp_enabled'])
SWING_PIVOT_BARS = _int('swing_pivot_bars')
SWING_LOOKBACK_CANDLES = _int('swing_lookback_candles')
SWING_MIN_GAP_PCT = _num('swing_min_gap_pct')
SWING_MIN_RETAINED_PCT = _num('swing_min_retained_pct')
assert 0 < SWING_MIN_RETAINED_PCT < 100, \
    "SWING_MIN_RETAINED_PCT is a share of the spot->ST run"
ATTRACTION_ENABLED = bool(_runtime['attraction_enabled'])
ATTRACTION_HORIZON_BARS = _int('attraction_horizon_bars')
ATTRACTION_GAP_PCT = _num('attraction_gap_pct')
ATTRACTION_MIN_EPISODES = _int('attraction_min_episodes')
FEE_RATES = dict(_runtime['fee_rates'])
for _k in ('brokerage_per_order', 'stt_sell_pct', 'exchange_pct', 'sebi_pct',
           'stamp_buy_pct', 'gst_pct'):
    assert _k in FEE_RATES, f"fee_rates is missing {_k}"
    assert float(FEE_RATES[_k]) >= 0, f"fee_rates.{_k} cannot be negative"
ATTRACTION_HORIZON_DAYS = _int('attraction_horizon_days')
ATTRACTION_TIMEFRAMES = tuple(_runtime['attraction_timeframes'])
assert ATTRACTION_TIMEFRAMES, \
    "measuring the magnet on no timeframe at all silently removes the section"
assert SWING_PIVOT_BARS >= 1, "a swing needs at least one candle either side"
assert SWING_LOOKBACK_CANDLES > SWING_PIVOT_BARS * 2, \
    "the lookback window cannot be shorter than one pivot window"
assert ATTRACTION_HORIZON_BARS >= 1, "the return horizon must be at least 1 candle"
# `_strict_bool`, not a raw read. This switch took the value verbatim until
# 2026-08-31, so `"spot_sl_enabled": "false"` -- or `0`, or `"no"` -- ARMED it:
# every non-empty string is truthy. It is the one switch in this file whose OFF
# state was decided by MEASUREMENT rather than by preference (147 records: a 3%
# spot stop cuts 40% of winners and gives up Rs 8.9L to catch losses the debit
# floor already caps), and it was the only money-deciding switch here still
# reading raw while paper_mode, auto_entry and exits_managed_externally were
# all validated. A typo must not be able to arm the money path.
SPOT_SL_ENABLED = _strict_bool('spot_sl_enabled')
SPOT_SL_PCT = _num('spot_sl_pct')
DEBIT_SL_PCT = _num('debit_sl_pct')
TIME_SL_DAYS = _int('time_sl_days_before_expiry')
MAX_OPEN_TRADES = _int('max_open_trades')
MAX_OPEN_PER_STOCK = _int('max_open_per_stock')
CAPITAL_RUPEES = _positive_finite('capital_rupees',
                                  _runtime.get('capital_rupees'),
                                  _DEFAULTS['capital_rupees'])
CAPITAL_PER_LOT = _positive_finite('capital_per_lot',
                                   _runtime.get('capital_per_lot'),
                                   _DEFAULTS['capital_per_lot'])
MAX_TRADE_PCT = _positive_finite('max_trade_pct',
                                 _runtime.get('max_trade_pct'),
                                 _DEFAULTS['max_trade_pct'])
MAX_DEPLOYED_PCT = _positive_finite('max_deployed_pct',
                                    _runtime.get('max_deployed_pct'),
                                    _DEFAULTS['max_deployed_pct'])
MAX_LOTS_HARD = _int('max_lots_hard')
# `_strict_bool`, not a raw read: this decides position SIZE from a P&L
# figure, and `"compound": 1` must not be able to arm that by accident.
COMPOUND = _strict_bool('compound')
# `_strict_bool` again: this one arms code that places OPENING orders.
AUTO_ENTRY = _strict_bool('auto_entry')


MAX_WATCHING_SIGNALS = _int('max_watching_signals')
WATCH_MAX_AGE_DAYS = _int('watch_max_age_days')
SCAN_INTERVAL_SEC = _int('scan_interval_sec')
MONITOR_INTERVAL_SEC = _int('monitor_interval_sec')
ENABLED_DIRECTIONS = _runtime['enabled_directions']
ENABLED_TIMEFRAMES = _runtime['enabled_timeframes']
ST_PERIOD = _int('st_period')
ST_MULTIPLIER = _num('st_multiplier')

# ── Claude vetting switches (need _runtime, hence exported here) ───────────
# Env wins when set, so a one-off `ZEBRA_VET_ENABLED=1 python -m zebra run`
# works without editing config; otherwise every process in the fleet reads the
# SAME file and cannot disagree about whether the layer is on.
_vet_env = os.environ.get('ZEBRA_VET_ENABLED', '').strip()
# `_strict_bool` on the config side for the same reason as the switches above:
# `bool()` reads `0`, `""` and a stray `null` as DISARM-THE-VETTING-LAYER, and
# that is the unsafe direction here -- entry vetting fails closed only while it
# is running at all, and with the layer off the exit gate returns `proceed`
# unconditionally. The env override keeps its own parsing: it is a deliberate
# one-off on a command line, not a file that a rebuild can half-write.
VET_ENABLED = (_vet_env.lower() in ('1', 'true', 'yes') if _vet_env
               else _strict_bool('vet_enabled'))


#: Statuses that mean rupees and legs are live for a cohort record. `closing`
#: and `partial_close` count: a position half-way out of the market is the
#: LAST one you want judged by a layer nobody noticed had gone.
OPEN_COHORT_STATUSES = ('entered', 'closing', 'partial_close')


def open_cohort_positions() -> Optional[int]:
    """Best-effort count of open cohort positions. None = could not tell.

    Reads the local trade file directly rather than going through
    `zebra.trade_store.get_store()`, for two reasons: that module imports this
    one (a circular import at module scope), and its `initialize()` reaches
    for Google Drive — neither belongs in a config import.

    Never raises. This exists to decorate a log line; a missing or malformed
    book must not stop the process that was about to say so.
    """
    try:
        with open(LOCAL_FILE, encoding='utf-8') as f:
            data = json.load(f)
        trades = data if isinstance(data, list) else data.get('trades', [])
        return sum(1 for t in trades
                   if isinstance(t, dict)
                   and t.get('status') in OPEN_COHORT_STATUSES
                   and t.get('cohort') == COHORT_START)
    except Exception:
        return None


def vet_state_line(enabled: bool, open_cohort: Optional[int]) -> tuple:
    """(log level, message) for where the vetting master switch resolved.

    A pure function so the WARNING case can be asserted without arranging a
    disarmed box. The level is the point of it: `vet_enabled` used to live
    only in the Pi's untracked overlay, so the layer could disappear in a
    routine overlay rebuild and nothing anywhere would say a word — while
    `exit_gate` returned 'proceed' unvetted on every exit.

    UNKNOWN open-position count warns like a non-zero one. "We could not look"
    must never render as "we looked and it is fine".
    """
    src = ('the ZEBRA_VET_ENABLED env var' if _vet_env
           else 'config (zebra_config.defaults.json <- zebra_config.json)')
    if enabled:
        return 'info', ('Claude vetting layer ENABLED (resolved from %s)' % src)
    tail = ('Entries are unvetted and vet.exit_gate() returns "proceed" for '
            'every exit. This is the DARK state, and it looks identical to a '
            'healthy one from the outside.')
    if open_cohort is None:
        return 'warning', (
            'Claude vetting layer DISABLED (resolved from %s) and the cohort '
            'book could not be read, so whether positions are open is UNKNOWN. '
            '%s' % (src, tail))
    if open_cohort > 0:
        return 'warning', (
            'Claude vetting layer DISABLED (resolved from %s) with %d open '
            'cohort position(s). %s' % (src, open_cohort, tail))
    return 'info', (
        'Claude vetting layer DISABLED (resolved from %s); no cohort '
        'positions are open. %s' % (src, tail))


def _log_vet_state() -> None:
    """Say where the switch resolved. Called at import, which is startup for
    every process in this fleet — cron run, manual CLI and monitor alike, so
    none of them can be dark without saying so in its own log.

    The cohort book is read only in the DISABLED branch, the one where the
    count changes the level, so the healthy path costs nothing.
    """
    level, msg = vet_state_line(
        VET_ENABLED, None if VET_ENABLED else open_cohort_positions())
    getattr(logger, level)('%s', msg)


# ── Startup banners: emitted once, into a log that EXISTS ─────────────────
# Both state lines (vetting, and the daily sweep further down) are emitted at
# import so that no process in the fleet can be dark without saying so. But
# `python -m zebra ...` imports this module through the package `__init__`
# BEFORE `main()` has called `setup_logging()`, and an INFO record with no
# handler is silently dropped (logging's last-resort handler only prints
# WARNING and above). Verified 2026-09-04 against the Pi's cron log: the
# vetting banner had NEVER appeared in it, and the sweep banner inherited the
# same hole on its first day. So: emit at import only when a sink already
# exists (the live monitor imports zebra lazily, after its own logging is up);
# otherwise stay PENDING and let `main()` call `emit_state_banners()` once the
# handler is there. Emitted at most once per process either way.
_BANNERS_PENDING = True


def _log_sink_exists() -> bool:
    return bool(logging.getLogger().handlers)


def emit_state_banners(force: bool = False) -> bool:
    """Log the vetting and daily-sweep state lines once. True if emitted now.

    Called at import (emits only if a log sink exists) and again from
    `zebra.__main__.main()` after `setup_logging()`. `force` re-emits even
    if already done — for a long-lived process that rotates its log.
    """
    global _BANNERS_PENDING
    if not force and not _BANNERS_PENDING:
        return False
    if not force and not _log_sink_exists():
        return False
    _log_vet_state()
    _eod_level, _eod_msg = eod_review_state_line()
    getattr(logger, _eod_level)('%s', _eod_msg)
    _BANNERS_PENDING = False
    return True

ENTRY_VET_TTL_SEC = _int('entry_vet_ttl_sec')
EXIT_VET_TTL_SEC = _int('exit_vet_ttl_sec')
EXIT_HOLD_TTL_SEC = _int('exit_hold_ttl_sec')
EXIT_VET_MAX_HOLD_SEC = _non_negative_int('exit_vet_max_hold_sec')
EXIT_VET_INCYCLE_WAIT_SEC = _non_negative_int('exit_vet_incycle_wait_sec')
VETO_SHADOW_DAYS = _int('veto_shadow_days')
EVENT_REFRESH_SEC = _int('event_refresh_sec')
EVENT_HORIZON_DAYS = _int('event_horizon_days')
REVIEW_ADVERSE_PCT = _num('review_adverse_pct')
# Env wins, same one-off-on-a-command-line shape as ZEBRA_VET_ENABLED. It is a
# KILL switch as much as a switch: the sweep spawns an agent per open position
# per session, so an operator watching the box burn its weekly limit needs a
# way to stop it without editing a Drive-synced config file.
_eod_env = os.environ.get('ZEBRA_EOD_REVIEW_ENABLED', '').strip()
EOD_REVIEW_ENABLED = (_eod_env.lower() in ('1', 'true', 'yes') if _eod_env
                      else bool(_runtime['eod_review_enabled']))
EOD_REVIEW_START = _hhmm('eod_review_start')
EOD_REVIEW_LAST_REQUEST = _hhmm('eod_review_last_request')
if EOD_REVIEW_LAST_REQUEST < EOD_REVIEW_START:
    # An empty window is a silently disabled sweep — the failure this feature
    # exists to close, wearing the config that says it is on. Widened to a
    # single instant rather than refused, so the sweep still runs and the log
    # says exactly what was wrong.
    logger.error('eod_review_last_request %02d:%02d is BEFORE '
                 'eod_review_start %02d:%02d — that is an empty window and no '
                 'scan could ever be requested. Using the start time; fix the '
                 'config, or set eod_review_enabled false if you meant to stop '
                 'the sweep.', EOD_REVIEW_LAST_REQUEST[0],
                 EOD_REVIEW_LAST_REQUEST[1], EOD_REVIEW_START[0],
                 EOD_REVIEW_START[1])
    EOD_REVIEW_LAST_REQUEST = EOD_REVIEW_START
AUTH_WARN_DAYS = _int('auth_warn_days')
MFE_CONFIRM_POLLS = _int('mfe_confirm_polls')
MFE_JUMP_MULT = _num('mfe_jump_mult')
MFE_SPOT_JUMP_PCT = _num('mfe_spot_jump_pct')
TRAIL_ENABLED = bool(_runtime['trail_enabled'])
TRAIL_ENGAGE_FRAC = _num('trail_engage_frac')
TRAIL_RETAIN_FRAC = _num('trail_retain_frac')
# Timeout and model live in the config FILE like every other threshold, with an
# env override for one-off runs. They were env-only-or-hardcoded, so editing
# zebra_config.json — the documented surface — logged "unknown keys ignored"
# and silently changed nothing.
VET_TIMEOUT_SEC = int(os.environ.get('ZEBRA_VET_TIMEOUT_SEC')
                      or _int('vet_timeout_sec'))
CHILD_KILL_SEC = int(os.environ.get('ZEBRA_CHILD_KILL_SEC')
                     or _int('child_kill_sec'))
# The child MUST be dead before its marker expires. It used to outlive it
# (720 kill vs 600 deadline, plus `timeout`'s own -k grace), and that gap is a
# correctness bug, not just waste: the expiry sweep requeues and re-promotes
# the signal in the same cycle, so the ORIGINAL agent — still alive — could
# land its verdict on the SECOND attempt's PENDING marker. `record_verdict`
# only checks "PENDING and not expired", so it cannot tell the two apart, and
# an ALLOW formed against a 10-minute-old book would be applied as though it
# judged the fresh one. That silently undoes the re-snapshot the queue exists
# to guarantee. Clamped rather than validated: an operator editing one number
# should not be able to reintroduce it. The 45s covers `timeout`'s -k grace.
_KILL_GRACE_SEC = 45
if CHILD_KILL_SEC > VET_TIMEOUT_SEC - _KILL_GRACE_SEC:
    _clamped = max(60, VET_TIMEOUT_SEC - _KILL_GRACE_SEC)
    if CHILD_KILL_SEC != _clamped:
        logger.warning(
            'child_kill_sec %d would outlive vet_timeout_sec %d — clamped to '
            '%d so a killed agent cannot answer for a later attempt',
            CHILD_KILL_SEC, VET_TIMEOUT_SEC, _clamped)
    CHILD_KILL_SEC = _clamped
# How many agents may be ALIVE at once, across every channel, on this box.
# The other caps are per-position-per-day, which bound how often one trade is
# looked at and say nothing about how many processes start together: one
# market-wide event row makes `needs_review` true for every open position in
# the same cycle, which was 24 detached node processes on a Pi that also runs
# the live-money monitor. Refusing a spawn is not refusing to trade — the
# caller's deadline lapses and the signal fails open as in any other outage.
MAX_CONCURRENT_AGENTS = int(os.environ.get('ZEBRA_MAX_CONCURRENT_AGENTS')
                            or _int('max_concurrent_agents'))

# Channels whose work can wait for the next window without a decision going
# unmade. `review` sweeps every open position and `events` runs on a timer, so
# they are the NUMEROUS ones — under a single shared cap they win by weight and
# starve the two channels that gate a trade. `entry`/`exit` are deliberately
# absent: a signal that enters unvetted cannot be re-vetted afterwards.
# `review_scan` is the DAILY sweep, spawned on the cheap model. It is a
# separate channel ONLY so the health watchdog counts it separately: ~8 Sonnet
# sweeps land every session, and `record_agent_landed` zeroes
# `spawns_since_landing` for its whole channel, so sharing 'review' meant the
# sweep's success permanently cleared the alarm for the Opus price reviews —
# a systematically failing price review could never reach SILENT_SPAWN_LIMIT.
# Same lesson as the per-channel split itself, one level down. Everything else
# about it is identical to `review`: same tool grants, same deny list, same
# deferrable pool and cap.
DEFERRABLE_CHANNELS = ('review', 'review_scan', 'events', 'postmortem')

# Slots the deferrable channels may never take, so a decision channel always
# has room. Must stay < MAX_CONCURRENT_AGENTS or the batch channels can never
# run at all (the cap floors at 1 regardless).
ENTRY_QUEUE_DROP_AFTER_SEC = _int('entry_queue_drop_after_sec')
ENTRY_VET_MAX_ATTEMPTS = _int('entry_vet_max_attempts')
CLI_BLOCK_SCAN_WINDOW_SEC = _int('cli_block_scan_window_sec')
CLI_BLOCK_GRACE_SEC = _int('cli_block_grace_sec')
AGENT_RESERVE = int(os.environ.get('ZEBRA_AGENT_RESERVE')
                    or _int('agent_reserve'))
# The env var bypasses `_positive_int`, so it can carry values the config file
# cannot. Two of them are actively harmful and both fail SILENTLY:
#   ZEBRA_AGENT_RESERVE=-3 -> deferrable cap max(1, 5-(-3)) = 8, i.e. BIGGER
#                             than the total cap: the reserve inverted, and
#                             batch channels get more room than decisions.
#   a non-numeric value    -> ValueError at import, killing the whole bot.
# Clamped into the only range that means anything: at least one reserved slot,
# and never so many that batch channels can never run.
if not 0 <= AGENT_RESERVE < MAX_CONCURRENT_AGENTS:
    _safe = min(max(AGENT_RESERVE, 0), max(MAX_CONCURRENT_AGENTS - 1, 0))
    logger.warning('agent_reserve %d is outside [0, %d) — clamped to %d',
                   AGENT_RESERVE, MAX_CONCURRENT_AGENTS, _safe)
    AGENT_RESERVE = _safe
VET_MODEL = os.environ.get('ZEBRA_VET_MODEL') or _runtime['vet_model']
# The routine daily position sweep — Sonnet, not VET_MODEL. Same env-override
# shape as every other model key so a one-off run can compare the two without
# editing config on the box.
EOD_REVIEW_MODEL = os.environ.get('ZEBRA_EOD_REVIEW_MODEL') \
    or _runtime['eod_review_model']

#: What ONE deferrable channel may hold at once. Derived, never written down
#: twice: `_spawn_budget_ok` computes exactly this and a second hardcoded copy
#: would drift the day AGENT_RESERVE moves.
DEFERRABLE_AGENT_CAP = max(1, MAX_CONCURRENT_AGENTS - AGENT_RESERVE)
EOD_REVIEW_MAX_PER_CYCLE = _int('eod_review_max_per_cycle')
# LEAVE ONE SLOT. `run()` now spawns price-triggered reviews first, which is
# the ordering half of the fix; this is the capacity half, and both are needed
# because the sweep and the price review share one pool. At the full deferrable
# cap a book of quiet positions takes every slot, and the adverse move that
# actually needed an agent is refused — with `record_spawn_refused` correctly
# NOT raising an alarm, because the budget was working as designed.
_eod_cap_max = max(1, DEFERRABLE_AGENT_CAP - 1)
if EOD_REVIEW_MAX_PER_CYCLE > _eod_cap_max:
    logger.warning('eod_review_max_per_cycle=%d would take every deferrable '
                   'agent slot (cap %d) and starve a price-triggered review '
                   'in the same cycle — clamped to %d.',
                   EOD_REVIEW_MAX_PER_CYCLE, DEFERRABLE_AGENT_CAP,
                   _eod_cap_max)
    EOD_REVIEW_MAX_PER_CYCLE = _eod_cap_max


def eod_review_state_line() -> tuple:
    """(log level, message) for where the daily sweep resolved.

    A pure function, and a LINE AT STARTUP, for the same reason
    `vet_state_line` is both: this switch decides whether every open position
    gets looked at once a day, and the disabled state is indistinguishable from
    a healthy quiet one in every log the box writes.
    """
    src = ('the ZEBRA_EOD_REVIEW_ENABLED env var' if _eod_env
           else 'config (zebra_config.defaults.json <- zebra_config.json)')
    if not EOD_REVIEW_ENABLED:
        return 'warning', (
            'Daily EOD position sweep DISABLED (resolved from %s). Open '
            'positions get NO standing news check; only price and calendar '
            'triggers can flag a review.' % src)
    return 'info', (
        'Daily EOD position sweep ENABLED (resolved from %s): %02d:%02d-'
        '%02d:%02d IST, model=%s, max %d scan spawn(s)/cycle (deferrable cap '
        '%d).' % (src, EOD_REVIEW_START[0], EOD_REVIEW_START[1],
                  EOD_REVIEW_LAST_REQUEST[0], EOD_REVIEW_LAST_REQUEST[1],
                  EOD_REVIEW_MODEL, EOD_REVIEW_MAX_PER_CYCLE,
                  DEFERRABLE_AGENT_CAP))

# The banner is LOGGED further down, after the market-hours block clamps the
# request window to the close — a startup line that reports a window the code
# has since narrowed is worse than no line at all.
EVENT_FILE = LOG_DIR / 'event_calendar.json'
EVENT_LOCK = LOG_DIR / 'event_calendar.lock'
# Sonnet, not Fable: this is routine fact-collection, not a judgement call.
EVENT_MODEL = os.environ.get('ZEBRA_EVENT_MODEL') or _runtime['event_model']
EVENT_PROMPT_TEMPLATE = (
    "Refresh the trading event calendar. Read {vetting_doc} (the EVENT CALENDAR "
    "section) and follow it exactly. Research upcoming India-market events and "
    "the per-stock events for these symbols: {symbols}. "
    "Write your findings to EXACTLY this path: {candidate} — it is the only "
    "path you are permitted to write. Then install it with "
    "`{python} -m zebra events replace --file {candidate}` exactly once."
)
# Sonnet, like the calendar: classifying settled trades against a CLOSED tag
# list is fact-collection, not a judgement about money.
POSTMORTEM_MODEL = os.environ.get('ZEBRA_POSTMORTEM_MODEL') \
    or _runtime['postmortem_model']
POSTMORTEM_PROMPT_TEMPLATE = (
    "Write post-mortems for the settled positions {ids}. Read {vetting_doc} "
    "(the POST-MORTEM section) and follow it exactly. For EACH id: run "
    "`{python} -m zebra postmortem show <id>`, then finish it with "
    "`{python} -m zebra postmortem record <id> --tag ... --lesson '...'` "
    "exactly once. Tags MUST come from the list the context prints; a tag "
    "outside it is rejected and the post-mortem is lost."
)
REVIEW_PROMPT_TEMPLATE = (
    "Review open position {trade_id} for event/macro risk the mechanical rules "
    "cannot see. Read {vetting_doc} (the POSITION REVIEW section) and follow it "
    "exactly. Start with `{python} -m zebra review show {trade_id}`. "
    "Use `{python}` for every zebra command. You MUST finish by calling "
    "`{python} -m zebra review record {trade_id} --action hold|adjust|exit "
    "--reason ...` exactly once."
)
if EXIT_VET_TTL_SEC <= MONITOR_INTERVAL_SEC:
    # A TTL shorter than one cycle expires every verdict before the cycle that
    # would act on it — the gate would re-request forever and no exit could
    # ever fire through it. Refuse to run in that shape.
    logger.warning("exit_vet_ttl_sec=%d must exceed monitor_interval_sec=%d — "
                   "using %d", EXIT_VET_TTL_SEC, MONITOR_INTERVAL_SEC,
                   MONITOR_INTERVAL_SEC * 3)
    EXIT_VET_TTL_SEC = MONITOR_INTERVAL_SEC * 3

SCANNERS = [s for s in _ALL_SCANNERS if s['timeframe'] in ENABLED_TIMEFRAMES]

# ── Helpers ───────────────────────────────────────────────────────────────
def is_trend_aligned(direction: str, st_direction: str) -> bool:
    """True if a Zebra signal is a WITH-TREND pullback (the validated premium
    setup): CE into a rising ST, PE into a falling ST. Counter-trend signals
    still trade (the capped-loss structure keeps them net-positive) — this flag
    is a conviction tag, not a hard filter. Single source of truth so the
    scanner tag and any consumer never diverge.
    """
    return (direction == 'CE' and st_direction == 'UP') or \
           (direction == 'PE' and st_direction == 'DOWN')


# ── Market hours ──────────────────────────────────────────────────────────
# Single source of truth for the exchange clock. Anything that reasons about
# "where are we in the session" must use this, never the host's local time —
# a UTC-clocked server would otherwise read 09:15 IST as 03:45 and mis-apply
# every session-relative rule.
IST = timezone(timedelta(hours=5, minutes=30))
MARKET_OPEN = (9, 15)
MARKET_CLOSE = (15, 30)

# The daily sweep's request window must END INSIDE THE SESSION. Checked here
# rather than beside the other EOD keys because this is where MARKET_CLOSE is
# defined, and a second copy of 15:30 is exactly the drift this block exists to
# prevent.
#
# `monitor._is_market_open` stops the cycle after MARKET_CLOSE, so a
# `eod_review_last_request` of "15:40" arms a window no cycle ever reaches:
# every scan after 15:30 is neither requested nor delivered, the config reads
# healthy, and the only symptom is positions quietly going unscanned. Same
# failure shape as the empty window above, one bound further out.
if EOD_REVIEW_LAST_REQUEST > MARKET_CLOSE:
    logger.error('eod_review_last_request %02d:%02d is after the %02d:%02d '
                 'close — the monitor runs no cycle there, so scans requested '
                 'in that window would never be requested OR delivered. '
                 'Clamped to the close; pick a time that leaves at least one '
                 'cycle for the agent to answer in.',
                 EOD_REVIEW_LAST_REQUEST[0], EOD_REVIEW_LAST_REQUEST[1],
                 MARKET_CLOSE[0], MARKET_CLOSE[1])
    EOD_REVIEW_LAST_REQUEST = MARKET_CLOSE
if EOD_REVIEW_START > EOD_REVIEW_LAST_REQUEST:
    # Only reachable when the clamp above moved the end back past a start that
    # was itself outside the session. Re-asserted rather than assumed: the
    # empty-window guard ran earlier, on the values BEFORE this clamp, and a
    # start after the close is the same silent no-op it exists to refuse.
    logger.error('eod_review_start %02d:%02d is outside the session — the '
                 'daily sweep would never fire. Clamped to %02d:%02d.',
                 EOD_REVIEW_START[0], EOD_REVIEW_START[1],
                 EOD_REVIEW_LAST_REQUEST[0], EOD_REVIEW_LAST_REQUEST[1])
    EOD_REVIEW_START = EOD_REVIEW_LAST_REQUEST

# Both banners go out together here, now that every value they report is
# final (the sweep window is clamped just above). See `emit_state_banners`.
emit_state_banners()

# ── Quote-reliability guards (2026-07-24 NHPC false DEBIT-SL incident) ──────
# The DEBIT-SL is a VALUE trigger (structure mid <= 50% of entry debit); a
# single garbage opening book once booked a phantom -50% exit. Zebra polls
# every MONITOR_INTERVAL_SEC (~5 min), so require N consecutive RELIABLE
# triggering reads before the alert fires. An unreliable read FREEZES the
# counter (a flickering book must not indefinitely block a genuine exit); a
# reliable non-trigger read resets it; a streak with a poll gap wider than
# CONFIRM_STALE_SEC restarts from zero. Spot-based TP/SPOT-SL stay single-poll
# (spot LTP is real trades). Layered ON TOP of the existing intrinsic-floor guard.
DEBIT_SL_CONFIRM_POLLS = 2       # reliable triggering polls before DEBIT SL alert
CONFIRM_STALE_SEC = 15 * 60      # confirm streak restarts if the poll gap exceeds this
DEBIT_BLIND_CYCLES = 3           # consecutive unusable-quote cycles (~15 min) => one blind alert

# ── The SECOND source (2026-08-12) ─────────────────────────────────────────
# Everything above is four CHECKS on ONE SOURCE. The reliability gate, the
# intrinsic floor, the debounce and the blind alert all read the same NFO
# option book, so a book that is wrong in a way they all accept produces a
# wrong exit with four green lights. Spot is the independent source: it is the
# most liquid instrument in the chain and it is quoted continuously.
#
# It is a VETO, never a trigger. Measured on 147 records with candle coverage,
# a 3% adverse SPOT TRIGGER cuts 31 of 78 eventual winners (Rs 8.9L given up)
# because the strategy enters on a pullback TOWARD the ST line — adverse
# movement is the thesis, not its failure. Winners' median MAE is 2.74%, so a
# 3% stop sits ON the median, and the book's biggest winner (IDFCFIRSTB
# +155.4%) has an MAE of 4.43% and dies at 3% or 4%. Reaping winners is the
# rule inverted. As a veto, spot costs nothing: it can only ever REFUSE an
# exit the option book asked for.
#
# Ported from bcs/spread_monitor.py:369 (`spot_corroborates`), which has run on
# the live money system since the NHPC post-mortem. One change: the reference
# is persisted to the store. The live monitor keeps it in memory, which a
# long-lived process can afford; zebra's cron process EXITS between cycles, so
# an in-memory reference would reset every 5 minutes and never veto anything.
SPOT_VETO_ENABLED = True         # veto a collapse the underlying cannot explain
SPREAD_COLLAPSE_PCT = 0.35       # value drop that demands an explanation
SPOT_MOVE_MIN_PCT = 0.004        # spot move that would count as one
CORROBORATION_STALE_SEC = 15 * 60  # reference older than this proves nothing

# Value triggers (DEBIT-SL, TRAIL) stay dark until this many seconds after the
# open. Both incidents that cost real money — ICICI 2026-02-18, NHPC
# 2026-07-24 — happened at the open, on the first prints of the day, and the
# hardened live monitor already refuses to act before 09:30
# (SPREAD_TRIGGER_OPEN_BUFFER_SEC). Zebra's first cycle is 09:15 with no
# buffer at all, so BOTH of its debounce polls land inside the window the live
# system will not trade in. Spot-driven TP and the TIME nag are unaffected:
# spot at the open is real trades, and the calendar does not care.
VALUE_TRIGGER_OPEN_BUFFER_SEC = 15 * 60

# ── Invariant checks ──────────────────────────────────────────────────────
assert WATCH_GAP_MAX > TRIGGER_GAP_MAX, (
    f"WATCH_GAP_MAX ({WATCH_GAP_MAX}) must be > TRIGGER_GAP_MAX ({TRIGGER_GAP_MAX})")
assert TRIGGER_GAP_MAX > STALE_GAP_MIN, (
    f"TRIGGER_GAP_MAX ({TRIGGER_GAP_MAX}) must be > STALE_GAP_MIN ({STALE_GAP_MIN})")
assert MIN_DTE < MAX_DTE, f"MIN_DTE must be < MAX_DTE"
assert MIN_DTE >= 1, f"MIN_DTE must be >= 1"
# A multiplier of 1.0 would send EVERY new peak through the confirmation
# window, so a peak reached in the final poll of a session (or the poll that
# exits the trade) would never be recorded at all. _num() already rejects
# non-positive values; this catches the merely useless ones.
assert MFE_JUMP_MULT > 1.0, f"MFE_JUMP_MULT ({MFE_JUMP_MULT}) must be > 1.0"
# A trail that keeps 100% of the peak sits ON the peak and fires on the first
# tick down — it would convert every winner into an immediate exit at the high,
# which is not a tighter stop but a different (and untested) strategy.
assert 0 < TRAIL_RETAIN_FRAC < 1, (
    f"TRAIL_RETAIN_FRAC ({TRAIL_RETAIN_FRAC}) must be strictly between 0 and 1")
assert 0 < TRAIL_ENGAGE_FRAC <= 1, (
    f"TRAIL_ENGAGE_FRAC ({TRAIL_ENGAGE_FRAC}) must be in (0, 1]")

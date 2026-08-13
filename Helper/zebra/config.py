"""Zebra strategy configuration — paths, thresholds, Chartink scan clauses."""

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
# see zebra/filelock.py for why an OS advisory lock beats a PID lockfile here.
LOCK_FILE = LOG_DIR / 'zebra_trades.lock'
# Claude vetting journal. SEPARATE lock from the trade store on purpose: sharing
# one would deadlock any caller that journalled while holding the trade lock
# (POSIX flock is per-fd, so a second acquire in the same process blocks).
# Callers must never nest the two.
DECISIONS_FILE = LOG_DIR / 'zebra_decisions.json'
DECISIONS_LOCK = LOG_DIR / 'zebra_decisions.lock'

# ── Claude vetting layer ──────────────────────────────────────────────────
# Master switch. OFF by default: the layer ships dark and is enabled explicitly
# once the CLI is authenticated on the Pi and the prompt has been exercised.
# With this False, zebra behaves exactly as it did before the layer existed.
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
VET_ALLOWED_TOOLS = ['WebSearch', 'WebFetch', 'Read', 'Glob', 'Grep',
                     'Bash({python} -m zebra:*)']
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
_EVENT_CANDIDATE_REL = EVENT_CANDIDATE_FILE.relative_to(PROJECT_ROOT).as_posix()
EVENT_EXTRA_TOOLS = [
    'Write({})'.format(_EVENT_CANDIDATE_REL),                 # cwd-relative
    'Write(//{})'.format(EVENT_CANDIDATE_FILE.as_posix().lstrip('/')),  # absolute
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
VET_DENIED_TOOLS = ['Bash(*zebra close*)', 'Bash(*zebra enter*)',
                    'Bash(*zebra cancel*)', 'Bash(*zebra reset*)',
                    'Bash(*zebra trigger*)',
                    'Bash(*zebra run*)', 'Bash(*zebra loop*)',
                    'Bash(*zebra scan*)', 'Bash(*zebra report*)',
                    'Bash(*postmortem run*)', 'Bash(*events replace*)']

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
# deferrals is ~10 min of re-checks with fresh quotes. Past that we do NOT fall
# through to the deterministic trigger: every structure here is hedged with max
# loss = debit known at entry, so HOLDING is bounded while exiting on a bad
# print is not (NHPC). The conservative direction is to hold and escalate.
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
    'max_leg_spread_pct': 0.01,  # bid-ask spread cap per leg (1% of mid)
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
    'time_sl_days_before_expiry': 4,
                                 # TRADING SESSIONS, not calendar days (the
                                 # count was calendar until 2026-08-12, so a
                                 # Friday read "3 days left" when one session
                                 # remained). Indian stock options are
                                 # PHYSICALLY settled and the exchange ramps a
                                 # delivery margin on ITM legs over the last
                                 # ~4 sessions; 3 sessions sat INSIDE that
                                 # ramp. Firing at the start of the E-4 session
                                 # means acting before that day's end-of-day
                                 # risk run, while keeping the time value that
                                 # exiting a session earlier would give up.
                                 # Raise to 5 if your broker levies intraday.
    'max_open_trades': 8,        # LIVE guidance only. PAPER intentionally does
                                 # NOT cap entries — capturing every signal keeps
                                 # the validation P&L unbiased (a cap would skew
                                 # which trades the track record contains).
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
    'vet_enabled': False,        # Claude vetting layer master switch. Lives HERE
                                 # rather than env-only so it cannot be ON in the
                                 # cron and OFF in a manual `python -m zebra run`
                                 # — a half-enabled fleet is worse than a dark
                                 # one: the unvetted process would fire, and burn
                                 # the consume-once flag on, the very exit the
                                 # vetted process is deliberately holding.
                                 # ZEBRA_VET_ENABLED still overrides, for a
                                 # one-off test without editing config.
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
    'auth_warn_days': 3,         # Telegram this many days before the Claude CLI
                                 # credential expires (user re-logs in manually).
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
    'trail_engage_frac': 0.50,   # Arm once the PEAK gain reaches this share of
                                 # max gain (width - debit). Deliberately NOT
                                 # the live monitor's 2x-debit rule: that lands
                                 # at 43% of max gain on a 30% d/w spread but
                                 # 82% on a 45% one, tightening exactly as the
                                 # payoff shrinks, and it would have engaged on
                                 # only 2 of 32 closed shadows.
    'trail_retain_frac': 0.50,   # Keep this share of the peak gain. Must stay
                                 # below 1: a trail sitting AT the peak fires
                                 # on the first tick down.
}


def _load_runtime() -> dict:
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                file_cfg = json.load(f)
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


_runtime = _load_runtime()


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
_raw_struct = str(_runtime['entry_structure']).strip().lower()
if _raw_struct not in ('bcs', 'zebra'):
    logger.warning("entry_structure=%r is not 'bcs' or 'zebra' — falling back "
                   "to %r", _runtime['entry_structure'],
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
MAX_LEG_SPREAD_PCT = _num('max_leg_spread_pct')
BCS_MAX_DEBIT_TO_WIDTH_PCT = _num('bcs_max_debit_to_width_pct')
BCS_MAX_ENTRY_COST_PCT = _num('bcs_max_entry_cost_pct')
assert 0 < BCS_MAX_ENTRY_COST_PCT < 100, \
    "BCS_MAX_ENTRY_COST_PCT is a percentage of max gain; 0 blocks every trade"
assert 0 < BCS_MAX_DEBIT_TO_WIDTH_PCT < 100, \
    "BCS_MAX_DEBIT_TO_WIDTH_PCT is a percentage of width"
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
assert SWING_PIVOT_BARS >= 1, "a swing needs at least one candle either side"
assert SWING_LOOKBACK_CANDLES > SWING_PIVOT_BARS * 2, \
    "the lookback window cannot be shorter than one pivot window"
assert ATTRACTION_HORIZON_BARS >= 1, "the return horizon must be at least 1 candle"
SPOT_SL_ENABLED = _runtime['spot_sl_enabled']
SPOT_SL_PCT = _num('spot_sl_pct')
DEBIT_SL_PCT = _num('debit_sl_pct')
TIME_SL_DAYS = _int('time_sl_days_before_expiry')
MAX_OPEN_TRADES = _int('max_open_trades')
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
VET_ENABLED = (_vet_env.lower() in ('1', 'true', 'yes') if _vet_env
               else bool(_runtime['vet_enabled']))
EXIT_VET_TTL_SEC = _int('exit_vet_ttl_sec')
EXIT_HOLD_TTL_SEC = _int('exit_hold_ttl_sec')
VETO_SHADOW_DAYS = _int('veto_shadow_days')
EVENT_REFRESH_SEC = _int('event_refresh_sec')
EVENT_HORIZON_DAYS = _int('event_horizon_days')
REVIEW_ADVERSE_PCT = _num('review_adverse_pct')
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
DEFERRABLE_CHANNELS = ('review', 'events', 'postmortem')

# Slots the deferrable channels may never take, so a decision channel always
# has room. Must stay < MAX_CONCURRENT_AGENTS or the batch channels can never
# run at all (the cap floors at 1 regardless).
ENTRY_QUEUE_DROP_AFTER_SEC = _int('entry_queue_drop_after_sec')
ENTRY_VET_MAX_ATTEMPTS = _int('entry_vet_max_attempts')
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

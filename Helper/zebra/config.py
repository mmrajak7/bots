"""Zebra strategy configuration — paths, thresholds, Chartink scan clauses."""

import json
import logging
import math
import os
from datetime import timedelta, timezone
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
# The calendar agent builds a candidate JSON file before installing it.
EVENT_EXTRA_TOOLS = ['Write']
VET_DENIED_TOOLS = ['Bash(*zebra close*)', 'Bash(*zebra enter*)',
                    'Bash(*zebra cancel*)', 'Bash(*zebra reset*)',
                    'Bash(*zebra trigger*)']

# VET_MODEL, VET_TIMEOUT_SEC and CHILD_KILL_SEC are exported further down —
# they read zebra_config.json, which is not loaded yet at this point.
# The fail-open deadline is generous because the CLI does live web research
# (results dates, ex-div, overhangs) and the structure is hedged and non-HFT,
# so a few minutes of entry drift costs far less than trading an unvetted
# signal. Past it the signal enters UNVETTED rather than stalling — a vetting
# outage must never become a silent trading halt.
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
    'min_dte': 15,
    'max_dte': 45,
    'min_leg_oi': 5000,
    'max_leg_spread_pct': 0.01,  # bid-ask spread cap per leg (1% of mid)
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
    'spot_sl_enabled': False,     # master switch for the adverse-spot SL (off: debit floor only)
    'spot_sl_pct': 0.03,          # adverse spot move from entry that triggers SL (only if enabled)
    'debit_sl_pct': 0.50,         # exit if option mid drops to this fraction of entry debit
    'time_sl_days_before_expiry': 3,
    'max_open_trades': 8,        # LIVE guidance only. PAPER intentionally does
                                 # NOT cap entries — capturing every signal keeps
                                 # the validation P&L unbiased (a cap would skew
                                 # which trades the track record contains).
    'max_watching_signals': 25,
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
    'vet_timeout_sec': 600,      # Fail-open deadline for one agent run.
    'child_kill_sec': 900,       # Hard wall-clock bound on a spawned CLI. Past
                                 # this the process is killed: the deadline
                                 # fails the MARKER open, but a hung child
                                 # would otherwise live until reboot and this
                                 # Pi also runs live-money bots.
    'vet_model': 'fable',        # Decisions.
    'event_model': 'sonnet',     # Routine calendar refresh.
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
PAPER_MODE = _runtime['paper_mode']
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
MIN_DTE = _int('min_dte')
MAX_DTE = _int('max_dte')
MIN_LEG_OI = _int('min_leg_oi')
MAX_LEG_SPREAD_PCT = _num('max_leg_spread_pct')
BCS_MAX_DEBIT_TO_WIDTH_PCT = _num('bcs_max_debit_to_width_pct')
TP_TARGET = _runtime['tp_target']
SPOT_SL_ENABLED = _runtime['spot_sl_enabled']
SPOT_SL_PCT = _num('spot_sl_pct')
DEBIT_SL_PCT = _num('debit_sl_pct')
TIME_SL_DAYS = _int('time_sl_days_before_expiry')
MAX_OPEN_TRADES = _int('max_open_trades')
MAX_WATCHING_SIGNALS = _int('max_watching_signals')
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
VET_MODEL = os.environ.get('ZEBRA_VET_MODEL') or _runtime['vet_model']
EVENT_FILE = LOG_DIR / 'event_calendar.json'
EVENT_LOCK = LOG_DIR / 'event_calendar.lock'
# Sonnet, not Fable: this is routine fact-collection, not a judgement call.
EVENT_MODEL = os.environ.get('ZEBRA_EVENT_MODEL') or _runtime['event_model']
EVENT_PROMPT_TEMPLATE = (
    "Refresh the trading event calendar. Read {vetting_doc} (the EVENT CALENDAR "
    "section) and follow it exactly. Research upcoming India-market events and "
    "the per-stock events for these symbols: {symbols}. "
    "Write your findings to a JSON file, then install it with "
    "`{python} -m zebra events replace --file <path>` exactly once."
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

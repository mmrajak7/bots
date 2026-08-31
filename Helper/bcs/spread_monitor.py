#!/usr/bin/env python3
"""
Unified Spread Monitor — BCS + Bear Put Spread + Fallen Hero

Monitors spot price and spread value for BCS and Fallen Hero trades.
Auto-closes on target hit or stop-loss trigger.

BCS Stop-Loss Layers (checked every poll cycle, in order):
  1. SL_SPOT    — spot <= sl_spot → thesis dead, close
  2. SL_SPREAD  — spread_value <= sl_spread → 50% loss guard, close
  3. SL_TRAIL   — engages at 2x entry debit, trails 60% of peak spread
  4. TP         — spot >= target → profit target, close

FH Stop-Loss (spot-only, reversed direction):
  1. SL_SPOT    — spot >= sl_spot → upside breakout, close
  2. EXPIRY     — force-close on expiry day

BCS Close sequence (margin rules - CRITICAL):
  1. BUY back short leg FIRST  (avoids naked short margin spike)
  2. SELL long leg AFTER short fills

FH Close sequence (naked risk first):
  1. BUY back short call (naked risk — most dangerous)
  2. SELL long call (if exists — hedge)
  3. BUY back short put
  4. SELL long put

Usage:
    python -m bcs.spread_monitor ICICIBANK                 # Monitor open BCS trade
    python -m bcs.spread_monitor ICICIBANK --target 1440   # Override target
    python -m bcs.spread_monitor ICICIBANK --sl-spot 1320  # Override SL
    python -m bcs.spread_monitor ICICIBANK --dry-run       # No real orders
    python -m bcs.spread_monitor --list                    # List all trades (BCS + FH)
    python -m bcs.spread_monitor --cron                    # Monitor ALL open (BCS + FH)
"""

import json
import logging
import os
import re
import sys
import time
import argparse
from datetime import datetime, date, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Kite's IP whitelist holds only the shared home IPv4; dual-stack machines
# otherwise connect over a rotating IPv6 and order placement is rejected
# with PermissionException (quotes still work, so the failure only surfaces
# at the exit/entry moment). Force all connections over IPv4.
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4_only_getaddrinfo

from kiteconnect import KiteConnect

from common import option_symbols as _sym
from common.option_symbols import check_leg_types
from common import layered_config
from common import kite_errors
from common import spread_valuation
from common import nse_holidays
from common import store_contract
from bcs import alert_policy
from bcs import exit_vet
from bcs import order_journal
from .trade_store import get_store
from fallen_hero import get_store as get_fh_store
from bear_put import get_store as get_bps_store
from zerodha.alert_checker import check_watchlist_alerts
from zerodha.watchlist import get_watchlist_store

logger = logging.getLogger(__name__)

# ── Execution Config ─────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 5
STATUS_PRINT_INTERVAL_SEC = 30
TICK_SIZE = 0.05
SLIPPAGE_TICKS_BASE = 2          # initial buffer: 2 ticks = Rs 0.10
SLIPPAGE_TICKS_INCREMENT = 2     # add 2 more ticks per retry
ORDER_WAIT_SEC = 30              # wait for fill before retry
MAX_RETRIES = 3
PRODUCT = "NRML"

# ── Trailing SL Config ───────────────────────────────────────────────────────
TRAIL_ENGAGE_MULTIPLIER = 2.0    # engage when spread >= 2x entry debit
TRAIL_PERCENT = 0.60             # trail at 60% of peak spread

# ── Market-Open Buffer ──────────────────────────────────────────────────────
# Don't act on SL/TP triggers for N seconds after market open.
# At 09:15, order books are thin/empty → spread values are garbage.
MARKET_OPEN_BUFFER_SEC = 180     # 3 minutes = wait until 09:18

# ── Quote-Reliability Guards (post 2026-07-24 NHPC false SL_SPREAD) ─────────
# Incident: 86CE opening book bid 0.28 / ask 1.40 (width 133% of mid, fair
# ~0.65) computed spread 0.42 <= SL 0.71 on a single poll at 09:18:24 and the
# close paid the garbage 1.40 ask. Rs 4,900 of the Rs 7,297 loss was phantom.
# A leg's quote is UNRELIABLE if (ask-bid) > max(ABS, PCT*mid), if the book is
# crossed, or if today's traded LTP contradicts the book. Unreliable leg =>
# spread value is None => no value-based trigger can fire or trail can update.
QUOTE_MAX_WIDTH_ABS = 0.30       # 6 ticks: cheap options near tick size stay reliable
QUOTE_MAX_WIDTH_PCT = 0.25       # normal formed books run 2-10% of mid; incident was 133%
# LTP on illiquid options is itself a lone print and cannot be fully trusted:
# it may only VETO a book (never validate one), and only when fresh. The width
# test above is the primary detector — it needs no trade history at all.
LTP_DIVERGENCE_MULT = 2.0        # ask > ltp*2 + ABS (or bid < ltp/2 - ABS) = unreliable
LTP_DIVERGENCE_ABS = 0.10        # exempts tick-sized options from the LTP check
LTP_FRESH_SEC = 30 * 60          # LTP may veto only if last trade < 30 min old

# Value-based triggers (SL_SPREAD / SL_TRAIL) must hold on N reliable polls
# before closing. SL_SPOT and expiry stay single-poll (spot LTP comes from
# real trades; delaying a thesis-dead exit costs more than any spread
# artifact can). Unreliable polls FREEZE the counter (a flickering book must
# not indefinitely block a genuine exit); a reliable non-trigger poll resets
# it; a streak older than CONFIRM_STALE_SEC restarts from zero.
SL_CONFIRM_POLLS = 3
CONFIRM_STALE_SEC = 180

# SL_SPOT was deliberately single-poll (spot LTP comes from real trades, and
# delaying a thesis-dead exit is itself a cost). Kept single-poll in spirit —
# two polls is 10 seconds — because the residual risk is not a FAKE spot but an
# outlier/stale OPENING print, and SL_SPOT is the one trigger that is exempt
# from every cooldown AND runs at URGENT urgency, whose final attempt pays
# through uncapped. 10s of confirmation cannot meaningfully delay a genuine
# gap-down while removing the last single-print path to a live order.
# Set to 1 to restore the old behaviour.
SL_SPOT_CONFIRM_POLLS = 2

# ── Spot corroboration (the NHPC signature) ───────────────────────────────
# NHPC's book passed nothing, but the shape it exposed generalises: a vertical
# spread's value is monotonic in spot, so a large collapse in the structure
# WITHOUT a corresponding move in the underlying is not a price — it is a
# broken book. Reliability/debounce/re-verify all interrogate the SAME order
# book, so identical stale reads can confirm each other; spot is an
# INDEPENDENT source and is the only cross-check available.
#
# Like the LTP test, this may only VETO a valuation, never validate one.
SPREAD_COLLAPSE_PCT = 0.35       # value drop vs the last reliable reading...
SPOT_MOVE_MIN_PCT = 0.004        # ...unexplained by a spot move this small
CORROBORATION_STALE_SEC = 900    # ignore a reference older than this (gaps)

# After a close ABORTs (unreliable book / re-verify artifact), don't re-attempt
# valuation/TP closes for this long — prevents an abort->re-trigger->abort loop
# from spamming orders/Telegram and starving the poll loop. SL_SPOT is exempt.
ABORT_COOLDOWN_SEC = 300

# Spread-based triggers wait longer after open than spot-based ones: the
# incident proved illiquid stock-option books are still unformed at 09:18.
SPREAD_TRIGGER_OPEN_BUFFER_SEC = 900   # SL_SPREAD/SL_TRAIL live from 09:30

# Trail-peak jump gate: a vertical spread can't rise >50% above its running
# peak within one poll without a huge spot move. Bigger jumps need
# SL_CONFIRM_POLLS consecutive reliable polls; the window MINIMUM is persisted.
# Blocks one garbage-high bid from poisoning trail state in the store/Drive.
TRAIL_PEAK_JUMP_MULT = 1.5

# Execution anchors: never validate an ask against itself. Anchor = today's
# LTP if the option traded today, else prev close. Caps are deliberately loose
# (an option CAN double intraday) — they are the last resort behind the
# reliability + debounce + re-verify layers.
BUY_CAP_ANCHOR_MULT = 2.5        # buy limit capped at anchor*2.5 (except final urgent attempt)
SELL_FLOOR_ANCHOR_DIV = 2.5      # sell limit floored at anchor/2.5 (except final urgent attempt)
URGENT_BOOK_WAITS = 2            # urgent closes wait at most 2x3s for a reliable book

# Blind-mode observability: if spread quotes stay unreliable/absent while the
# market is settled, the user MUST know SL_SPREAD/SL_TRAIL are suspended.
# The blind clock clears only after BLIND_CLEAR_OK_POLLS consecutive reliable
# polls — a single good quote in a flickering book must not silence the alert.
SPREAD_BLIND_ALERT_SEC = 15 * 60      # first Telegram after 15 min blind
SPREAD_BLIND_REPEAT_SEC = 60 * 60     # repeat at most hourly
BLIND_CLEAR_OK_POLLS = 3
# B12 — spot-blind escalates HARDER than spread-blind, deliberately. A
# spread-blind trade still has SL_SPOT armed; a SPOT-blind trade has NO live
# trigger at all — SL_SPOT, SL_SPREAD and TP are all dead for that record
# while the process keeps reporting itself healthy.
SPOT_BLIND_ALERT_SEC = 5 * 60         # first Telegram after 5 min, not 15
SPOT_BLIND_REPEAT_SEC = 10 * 60       # every 10 min, not hourly
SPOT_PROXIMITY_ALERT_PCT = 0.01       # escalate while blind AND spot within 1% of sl_spot
PROXIMITY_REPEAT_SEC = 10 * 60        # proximity alert every 10 min

# Re-verify: after debounce passes, fetch one fresh quote before placing any
# order — kills artifacts that healed between the confirming polls and order time.
REVERIFY_DELAY_SEC = 2

# ── Market Hours (IST) ──────────────────────────────────────────────────────
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
LAST_ORDER_TIME = dtime(15, 20)   # No NEW normal-close orders after this time
HARD_ORDER_CUTOFF_TIME = dtime(15, 25)  # urgent reduce-only exits allowed until here

# ── Error Budget ──────────────────────────────────────────────────────────
MAX_CONSECUTIVE_ERRORS = 20       # Exit after this many consecutive API errors

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/bcs/
PROJECT_ROOT = SCRIPT_DIR.parent                   # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                    # BOTS/
TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
LOG_DIR = PROJECT_ROOT / 'logs'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'bcs_config.json'
SWITCH_FILE = PROJECT_ROOT / 'config' / 'trading_switch.json'


def _switch_says(path: Path) -> bool:
    """Read one kill-switch file. True unless it says boolean `false`.

    **FAILS OPEN, deliberately.** A missing, unreadable or malformed file
    means ENABLED. That is the opposite of the usual instinct and it is
    correct here, because this path only ever CLOSES positions: disarming it
    does not prevent a bad trade, it abandons the stops on a live book.
    "Stop trading" is not "stop watching", and silent unmonitoring is the
    failure mode that has actually cost money (ICICI Feb-2026, NHPC Jul-2026).
    """
    try:
        with open(path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        return True
    except Exception as e:
        log(f"  WARNING: {path.name} unreadable ({e}) — trading stays "
            f"ENABLED; a config error must not disarm live stops")
        return True
    return _trading_flag(cfg, path.name)


def _trading_flag(cfg: dict, label: str) -> bool:
    """`trading.enabled` out of an already-loaded config. Absent means True.

    One definition, because the two arms of the switch now reach their config
    by different routes — one raw file, one layered — and a kill switch whose
    two halves disagree about what `enabled` means is worse than one half.
    """
    node = cfg.get('trading')
    if not isinstance(node, dict):
        return True
    val = node.get('enabled', True)
    if isinstance(val, bool):
        return val
    # Never infer a live/disarmed switch from a non-boolean. Same discipline
    # as zebra/config.py:_strict_bool — except the safe default there is
    # PAPER, and the safe default here is ARMED, for the reason above.
    log(f"  WARNING: trading.enabled={val!r} in {label} is not "
        f"true/false — trading stays ENABLED. A kill switch is never "
        f"inferred.")
    return True


def _bcs_config_says() -> bool:
    """The bcs_config arm of the switch, read through BOTH of its layers.

    `config/bcs_config.json` was stripped to secrets on 2026-08-26 when the
    config was split into a tracked defaults file and an untracked overlay
    (see common/layered_config.py). Reading it raw would therefore find no
    `trading` key at all and fail open — silently demoting a deliberately
    two-source switch to one source. The flag now lives in the TRACKED layer,
    which is the better place for it: it has history, and it reaches the Pi
    over git rather than over Drive.
    """
    cfg = layered_config.load('bcs_config',
                              warn=lambda m: log(f'  WARNING: {m} — '
                                                 f'trading stays ENABLED'))
    return _trading_flag(cfg, 'bcs_config')


def trading_enabled() -> bool:
    """The live-order kill switch. Read on EVERY poll, never cached.

    A switch consulted once at startup cannot stop a monitor that is already
    running — and the cron line is `flock -n` on a */5 schedule, so killing
    the process just restarts it within five minutes. Re-reading each cycle
    is what makes this an actual stop button rather than a restart-time
    preference.

    TWO files, ANDed: `config/trading_switch.json` (TRACKED in git) and
    `config/bcs_config.json` (untracked, Drive-carried). Either saying boolean
    `false` disarms.

    The tracked one exists because the flag deciding whether real orders fire
    had no version history: you could not see when it moved or why, a fresh
    deploy was armed by whatever untracked file happened to be on the box, and
    a change to the money switch never appeared in a diff. It could not simply
    BE `bcs_config.json`, because that file carries the Drive folder id and a
    credentials path — and its neighbours in `config/` carry live Telegram bot
    tokens — into a PUBLIC repo.

    ANDed rather than ranked on purpose. A precedence rule means a reader who
    sets the wrong file gets silently overruled, which is the one outcome a
    stop button may never have. Both are stop buttons; neither is an arm
    button.
    """
    return _switch_says(SWITCH_FILE) and _bcs_config_says()


# ── Module-level log state (set per-trade in monitor()) ──────────────────────
_log_file: Optional[Path] = None


def set_log_file(path: Path):
    """Set the active log file path."""
    global _log_file
    _log_file = path


def log(msg: str):
    """Print and append to log file with timestamp."""
    ts = now_ist().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    if _log_file:
        LOG_DIR.mkdir(exist_ok=True)
        with open(_log_file, 'a') as f:
            f.write(line + '\n')


# ── Kite Auth ────────────────────────────────────────────────────────────────

def load_kite() -> KiteConnect:
    """Authenticate and return Kite client."""
    if not TOKEN_FILE.exists():
        log(f"FATAL: Token file not found at {TOKEN_FILE}")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    generated = datetime.fromisoformat(token_data['generated_at'])
    if generated.date() != today_ist():
        # A log line is not an alert. This runs unattended under cron; a stale
        # token here becomes "every stop is dark today" and the only witness
        # was a line in a file nobody opens until something has gone wrong.
        msg = (f"Token is from {generated.date()}, not today — the monitor is "
               f"starting but every Kite call may fail. Open positions would "
               f"be UNMONITORED. Re-run SNAIL auth.")
        log(f"WARNING: {msg}")
        send_telegram(f"⚠️ BCS MONITOR: STALE KITE TOKEN\n{msg}", alert_class=alert_policy.SAFETY)

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# ── The clock ────────────────────────────────────────────────────────────────

#: This engine trades ONE exchange in ONE timezone. Every session gate here
#: (market open, the order cutoffs, the settle buffers) and every date stamp
#: (the token's freshness, the daily nag keys, the log file name) used to read
#: `datetime.now()` / `date.today()` — the BOX's local time. zebra next door
#: has been IST-aware since it was written, so the two engines managing the
#: same positions disagreed about what day it was on any box not set to IST.
#:
#: Latent, not harmless: the Pi is IST today, and the failure mode if it ever
#: is not would be a monitor that thinks the market is shut, or a token it
#: reads as stale, or a daily nag that fires twice — silently, at the open.
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """IST wall-clock, returned NAIVE.

    Naive on purpose. Every comparison in this module is naive-vs-naive
    (`datetime.combine(today, MARKET_OPEN)`, the `.time()` gates), and handing
    back an aware datetime would raise `TypeError: can't compare offset-naive
    and offset-aware` at a session gate — turning a timezone tidy-up into an
    outage on the money path. This changes WHICH wall clock is read, never the
    arithmetic that reads it.

    Goes through `datetime.now(...)` rather than `date.today()` so that
    `bcs/tests/replay.py`'s ReplayClock — which substitutes this module's
    `datetime` with a subclass whose `.now()` ignores its tz argument — keeps
    driving it. A helper the harness cannot reach would make every replay run
    on the wall clock.
    """
    return datetime.now(IST).replace(tzinfo=None)


def today_ist() -> date:
    """The IST trading date. See `now_ist`."""
    return now_ist().date()


def ist_epoch(stamp: str) -> float:
    """Absolute epoch seconds for one of THIS module's naive-IST ISO stamps.

    A REGRESSION FIXED, created by the clock sweep that preceded it. Every
    stamp this module writes (`frozen_at`, `exit_date`, ...) is now naive IST,
    and `datetime.fromisoformat(s).timestamp()` interprets a naive value in
    the BOX's zone — so `recovery_gate` compared an IST-derived epoch against
    `time.time()` and, on a UTC box, read a freeze as 5.5 HOURS IN THE FUTURE.
    `age` goes negative, and the bounded class's 300-second recovery grace
    becomes about five and a half hours: the M14 sweep dark for most of a
    session, on exactly the positions it exists to finish.

    Before the sweep both sides were box-local and agreed by accident. This
    makes them agree on purpose. Reading historical stamps is safe too — they
    were written box-local on a box that is IST.

    (The naked-short class has grace 0 and never reaches this branch, so the
    unbounded case was never affected.)
    """
    return datetime.fromisoformat(stamp).replace(tzinfo=IST).timestamp()


# ── Market & Price ───────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Check if within NSE trading hours."""
    now = now_ist().time()
    return MARKET_OPEN <= now < MARKET_CLOSE


# ── Batched quotes (F7) ─────────────────────────────────────────────────────
#
# THE PROBLEM, measured on the Pi 2026-08-28: this monitor issued ONE
# `/quote` per option leg and ONE `/ltp` per underlying, every poll, for every
# open position. At 8 positions that is 8 + 16 = 24 requests every 5 seconds
# = 4.8 req/s against a quote family capped at **1 req/s**. The log for that
# morning carries 58 `Too many requests` failures still arriving at 10:02, and
# each one is a position that went UNWATCHED for that poll:
#
#   [09:59:40]  BCS #457 JINDALSTEL: ... [QUOTE-FAIL Too many requests]
#
# Kite's `/quote` and `/ltp` both take up to 500 instruments in ONE call, so
# the entire book fits in two requests. The fix is a per-poll PREFETCH plus a
# read-through cache, giving **2 requests per poll regardless of book size**.
#
# WHAT THIS MUST NOT CHANGE. Every valuation guard — `leg_quote_reliable`, the
# intrinsic floor, `spot_corroborates`, the depth/quantity checks in the order
# path — reads the dict `get_option_depth` derives. Kite returns the SAME
# per-instrument payload whether one instrument was asked for or five hundred,
# so the derivation is byte-identical and the guards receive exactly what they
# received before. What changes is only how many HTTP calls produced it.
#
# The one place batching WOULD change a guard's input is anywhere the code
# deliberately wants a NEW read of a book it has already seen this poll: the
# re-verify after a valuation trigger, and the wait-for-depth loop that prices
# an order. Those pass `fresh=True` and bypass the cache entirely. A cached
# answer there would turn "wait for the book to re-form" into an infinite loop
# over a frozen snapshot — the opposite of the guard's purpose.
#
# BACKING OFF, not retrying. A 429 carries a ~10s sliding cooldown that
# FURTHER requests EXTEND, so the old behaviour (18 calls, all failing, all
# extending) was self-sustaining. After a rate-limited batch, calls are
# refused LOCALLY for QUOTE_COOLDOWN_SEC — the refusal is raised as an
# exception the shared `common.kite_errors` classifier already reads as
# RATE_LIMIT, so every caller's existing handling applies unchanged and no
# second classifier is born.

#: Kite documents 500 per call. 200 keeps the query string comfortably short
#: and still puts any realistic book in a single request.
QUOTE_BATCH_MAX = 200

#: Kite's 429 window is ~10s and sliding. Wait longer than the window, never
#: shorter, or the first call back is what extends it again.
QUOTE_COOLDOWN_SEC = 12.0

#: How old a cached quote may be before it is refetched rather than served.
#: The cache is ALREADY dropped at the top of every poll, so this is a
#: structural backstop, not the mechanism: it makes a stale price unable to
#: reach a valuation guard even from a caller that forgot to prefetch, or on
#: the tail of a poll where `close_spread` spent 30 seconds in the middle.
#: Set below the poll interval on purpose — an exit guard reading a quote
#: older than one poll is the failure this whole file is built around.
QUOTE_CACHE_TTL_SEC = 5.0


class QuoteRateLimited(Exception):
    """Refused locally because Kite rate-limited us moments ago.

    Carries `.code = 429` and Kite's own wording so
    `common.kite_errors.classify` returns RATE_LIMIT without needing to know
    this class exists. That matters: the blind-mode alert, `_is_auth_error`
    and the diagnosis text all route through that one classifier, and a
    locally-generated backoff must read to them exactly like the server's own
    refusal — otherwise a backoff would be reported as an auth problem, which
    is the mistake `common/kite_errors.py` was written to end.
    """
    code = 429


#: 'NFO:SYM' -> (fetched_at, the raw per-instrument dict Kite returned).
_quote_cache: dict = {}
#: 'NSE:SYM' -> (fetched_at, the raw per-instrument dict `/ltp` returned).
_ltp_cache: dict = {}
#: Instruments this poll's prefetch ASKED for. An instrument in here but not
#: in the cache was genuinely absent from Kite's answer — that is a KeyError,
#: exactly as the old single-instrument `q[full]` produced, and must NOT
#: silently fall back to a per-instrument call.
_quote_requested: set = set()
_ltp_requested: set = set()
#: The exception a failed prefetch died of, replayed to every consumer of the
#: instruments it covered. One failure, one cause, N honest per-trade reports.
_quote_batch_error: Optional[BaseException] = None
_ltp_batch_error: Optional[BaseException] = None
#: Wall-clock instant before which no quote-family call may be made.
_quote_cooldown_until: float = 0.0
#: Request accounting, for the log line and for tests that assert the budget.
quote_stats: dict = {'batches': 0, 'singles': 0, 'instruments': 0,
                     'cache_hits': 0}


def reset_quote_cache() -> None:
    """Drop the per-poll cache. Called at the top of every poll, and by tests.

    The cooldown deliberately SURVIVES this: it is a property of the broker's
    rate limiter, not of our polling cycle, and clearing it every 5 seconds
    would defeat the whole back-off.
    """
    global _quote_batch_error, _ltp_batch_error
    _quote_cache.clear()
    _ltp_cache.clear()
    _quote_requested.clear()
    _ltp_requested.clear()
    _quote_batch_error = None
    _ltp_batch_error = None


def reset_quote_cooldown() -> None:
    """Clear the local rate-limit backoff (process start, and tests)."""
    global _quote_cooldown_until
    _quote_cooldown_until = 0.0


def quote_cooldown_remaining(now: Optional[float] = None) -> float:
    """Seconds left on the local backoff; 0.0 when clear."""
    now = time.time() if now is None else now
    return max(0.0, _quote_cooldown_until - now)


def _guard_cooldown() -> None:
    """Raise rather than make a call that would extend Kite's 429 window."""
    left = quote_cooldown_remaining()
    if left > 0:
        raise QuoteRateLimited(
            f"Too many requests — local backoff, {left:.0f}s left. Kite's 429 "
            f"window is sliding and a call now would extend it.")


def _note_rate_limit(exc) -> None:
    """Start the local backoff if Kite said 429."""
    global _quote_cooldown_until
    if kite_errors.is_rate_limit(exc):
        _quote_cooldown_until = time.time() + QUOTE_COOLDOWN_SEC


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _cache_get(cache: dict, key: str, now: Optional[float] = None):
    """The cached payload, or None if absent OR older than the TTL."""
    hit = cache.get(key)
    if hit is None:
        return None
    ts, payload = hit
    now = time.time() if now is None else now
    if now - ts > QUOTE_CACHE_TTL_SEC:
        return None
    return payload


def prefetch_quotes(kite: KiteConnect, instruments) -> int:
    """Fill the per-poll quote cache in as few `/quote` calls as possible.

    Returns the number of HTTP requests made. Never raises: a failure is
    stored and replayed to whichever consumer asks for a covered instrument,
    so one broken batch produces one honest per-trade error rather than an
    exception escaping into the middle of the poll.
    """
    global _quote_batch_error
    wanted = sorted({i for i in instruments if i})
    wanted = [i for i in wanted if _cache_get(_quote_cache, i) is None]
    if not wanted:
        return 0
    _quote_requested.update(wanted)
    made = 0
    for chunk in _chunks(wanted, QUOTE_BATCH_MAX):
        try:
            _guard_cooldown()
            data = kite.quote(chunk) or {}
            made += 1
            quote_stats['batches'] += 1
            quote_stats['instruments'] += len(chunk)
            stamp = time.time()
            for k, v in data.items():
                _quote_cache[k] = (stamp, v)
        except Exception as e:
            _note_rate_limit(e)
            _quote_batch_error = e
            log(f"  QUOTE BATCH FAILED for {len(chunk)} instrument(s): {e}")
            break
    return made


def prefetch_ltps(kite: KiteConnect, symbols) -> int:
    """Fill the per-poll LTP cache. Same contract as `prefetch_quotes`."""
    global _ltp_batch_error
    wanted = sorted({s for s in symbols if s})
    wanted = [s for s in wanted if _cache_get(_ltp_cache, s) is None]
    if not wanted:
        return 0
    _ltp_requested.update(wanted)
    made = 0
    for chunk in _chunks(wanted, QUOTE_BATCH_MAX):
        try:
            _guard_cooldown()
            data = kite.ltp(chunk) or {}
            made += 1
            quote_stats['batches'] += 1
            quote_stats['instruments'] += len(chunk)
            stamp = time.time()
            for k, v in data.items():
                _ltp_cache[k] = (stamp, v)
        except Exception as e:
            _note_rate_limit(e)
            _ltp_batch_error = e
            log(f"  LTP BATCH FAILED for {len(chunk)} symbol(s): {e}")
            break
    return made


def trade_instruments(trade: dict) -> list:
    """Every option leg of a record, as `EXCHANGE:SYMBOL`.

    Reads `*_symbol` keys generically rather than naming BCS's pair, for the
    same reason `delivery_exposure` does: this file manages four books, one of
    which (FH) has three or four legs, and a hand-written list is the shape
    that silently misses the leg someone added later. `spot_symbol` is
    excluded — it is an equity and goes through `/ltp`, and it already carries
    its own exchange prefix.
    """
    exch = trade.get('exchange') or 'NFO'
    out = []
    for key, val in trade.items():
        if not key.endswith('_symbol') or key == 'spot_symbol':
            continue
        if isinstance(val, str) and val:
            out.append(f"{exch}:{val}")
    return out


def prefetch_book(kite: KiteConnect, trades) -> int:
    """Prefetch every leg and every underlying for a whole book.

    This is the entire F7 fix at the call site: two requests replace
    `2 x len(trades)` quote calls and `len(trades)` ltp calls.
    """
    reset_quote_cache()
    legs = []
    spots = []
    for t in trades or []:
        try:
            legs.extend(trade_instruments(t))
            if t.get('spot_symbol'):
                spots.append(t['spot_symbol'])
        except Exception:
            # A malformed record must not cost the whole book its prefetch;
            # `_malformed_reason` quarantines it a few lines later anyway.
            continue
    return prefetch_quotes(kite, legs) + prefetch_ltps(kite, spots)


def get_spot(kite: KiteConnect, spot_symbol: str, fresh: bool = False) -> float:
    """Fetch spot price for given symbol.

    Served from this poll's prefetch when there is one. `fresh=True` forces a
    new read for the rare caller that needs the book to have moved.
    """
    if not fresh:
        hit = _cache_get(_ltp_cache, spot_symbol)
        if hit is not None:
            quote_stats['cache_hits'] += 1
            return hit['last_price']
        if spot_symbol in _ltp_requested and spot_symbol not in _ltp_cache:
            # Asked for and not returned, or the batch died. Either way this
            # is the same failure the old per-symbol call produced, and it
            # must reach the caller's per-trade handler rather than being
            # papered over with an extra request. (An entry that merely aged
            # past the TTL is still in `_ltp_cache` and falls through to a
            # refetch below — expiry is not absence.)
            if _ltp_batch_error is not None:
                raise _ltp_batch_error
            raise KeyError(spot_symbol)
    _guard_cooldown()
    try:
        q = kite.ltp([spot_symbol])
        quote_stats['singles'] += 1
    except Exception as e:
        _note_rate_limit(e)
        raise
    _ltp_cache[spot_symbol] = (time.time(), q[spot_symbol])
    return q[spot_symbol]['last_price']


def get_option_depth(kite: KiteConnect, exchange: str, symbol: str,
                     fresh: bool = False) -> dict:
    """Fetch full depth for an option symbol.

    Served from this poll's prefetch when there is one — the derived dict is
    identical either way, because Kite's per-instrument payload does not
    depend on how many instruments were requested alongside it.

    `fresh=True` bypasses the cache. Use it wherever the code is waiting for
    the book to CHANGE (the order path's depth loop) or deliberately taking a
    second look (the post-trigger re-verify). A cached answer in either place
    would turn a guard into a tautology.
    """
    full = f"{exchange}:{symbol}"
    q = None
    if not fresh:
        q = _cache_get(_quote_cache, full)
        if q is not None:
            quote_stats['cache_hits'] += 1
        elif full in _quote_requested and full not in _quote_cache:
            # Requested and genuinely absent from Kite's answer — the same
            # KeyError the old `kite.quote([full])[full]` raised. An entry
            # that merely aged past the TTL is still present and refetches.
            if _quote_batch_error is not None:
                raise _quote_batch_error
            raise KeyError(full)
    if q is None:
        _guard_cooldown()
        try:
            q = kite.quote([full])[full]
            quote_stats['singles'] += 1
        except Exception as e:
            _note_rate_limit(e)
            raise
        _quote_cache[full] = (time.time(), q)
    depth = q.get('depth', {})
    best_bid = depth['buy'][0] if depth.get('buy') else {'price': 0, 'quantity': 0}
    best_ask = depth['sell'][0] if depth.get('sell') else {'price': 0, 'quantity': 0}

    # LTP trust tiers. On illiquid options the LTP is itself a lone print:
    # a stale (yesterday's) LTP must not judge a legitimately repriced book,
    # and even today's print goes stale within minutes. ltp_fresh gates the
    # divergence VETO; traded_today gates the (looser) execution anchor.
    traded_today = False
    ltp_fresh = False
    ltt = q.get('last_trade_time')
    try:
        if ltt is not None:
            if hasattr(ltt, 'date'):
                ltt_dt = ltt
            else:
                ltt_dt = datetime.strptime(str(ltt)[:19], '%Y-%m-%d %H:%M:%S')
            traded_today = (ltt_dt.date() == today_ist())
            if traded_today:
                ltp_fresh = (now_ist() - ltt_dt).total_seconds() <= LTP_FRESH_SEC
    except Exception:
        traded_today = False
        ltp_fresh = False

    return {
        'bid': best_bid.get('price', 0),
        'bid_qty': best_bid.get('quantity', 0),
        'ask': best_ask.get('price', 0),
        'ask_qty': best_ask.get('quantity', 0),
        'ltp': q.get('last_price', 0),
        'prev_close': (q.get('ohlc') or {}).get('close', 0),
        'traded_today': traded_today,
        'ltp_fresh': ltp_fresh,
    }


def leg_quote_reliable(depth: dict) -> tuple:
    """Judge whether a leg's top-of-book is trustworthy for VALUATION.

    Post 2026-07-24 NHPC incident: an unformed book (bid 0.28 / ask 1.40,
    width 133% of mid) produced a phantom SL_SPREAD trigger AND a garbage
    fill. Returns (reliable: bool, reason: str).

    Unreliable if:
      - either side missing/zero (no two-way market)
      - crossed book (bid > ask)
      - width > max(QUOTE_MAX_WIDTH_ABS, QUOTE_MAX_WIDTH_PCT * mid)
      - a FRESH traded LTP (< LTP_FRESH_SEC old) contradicts the book

    LTP is itself untrustworthy on illiquid strikes (a lone stale print), so
    it may only veto — a book is never validated BY its LTP, and a stale LTP
    cannot blind the monitor against a legitimately repriced book.
    """
    bid, ask = depth['bid'], depth['ask']
    if bid <= 0 or ask <= 0 or depth['bid_qty'] <= 0 or depth['ask_qty'] <= 0:
        return False, 'no_two_way_book'
    if bid > ask:
        return False, f'crossed_book bid {bid} > ask {ask}'
    width = ask - bid
    mid = (bid + ask) / 2.0
    if width > max(QUOTE_MAX_WIDTH_ABS, QUOTE_MAX_WIDTH_PCT * mid) + 1e-9:
        return False, f'wide_book width {width:.2f} vs mid {mid:.2f}'
    ltp = depth.get('ltp') or 0
    if ltp > 0 and depth.get('ltp_fresh'):
        if ask > ltp * LTP_DIVERGENCE_MULT + LTP_DIVERGENCE_ABS:
            return False, f'ask {ask} diverges from fresh ltp {ltp}'
        if bid < ltp / LTP_DIVERGENCE_MULT - LTP_DIVERGENCE_ABS:
            return False, f'bid {bid} diverges from fresh ltp {ltp}'
    return True, ''


#: Re-exported so existing call sites keep their names. The definitions live
#: in `common/` because the three stores need them too, and the last time a
#: piece of option arithmetic lived in only one place the bear-put book ran for
#: months with no intrinsic floor at all (B21).
strike_from_symbol = _sym.strike
option_type_from_symbol = _sym.option_type


def _spread_width(trade: dict):
    """The vertical's width in points, or None when the record cannot say.

    None rather than 0 on purpose: 0 would clamp every valuation to zero.
    """
    try:
        w = float(trade.get('spread_width') or trade.get('width') or 0)
    except (TypeError, ValueError):
        return None
    return w if w > 0 else None


def spread_intrinsic_floor(trade: dict, spot: float):
    """No-arbitrage floor for a vertical at `spot`. See `common.spread_valuation`.

    A thin delegate since 2026-08-30. This function and
    `zebra.monitor._intrinsic_floor` were one arithmetic implemented twice, and
    each engine held HALF of the fix:

    * this one derived CE/PE from the leg SYMBOLS and built the short-leg
      allowance from the entry price less its intrinsic at entry -- both
      corrections zebra never got;
    * zebra clamped the floor at zero and this one did not -- which changes no
      outcome on either path, because both clamp the VALUE to >= 0 first, so a
      floor at or below zero rejects nothing.
      `test_the_intrinsic_floor_is_inert_below_the_long_strike_by_construction`
      exists to stop a reader "fixing" the live-money path to match the paper
      one on that difference, and it did its job during this merge.

    Verticals only, so `long_multiplier` stays 1. The retired back-ratio's 2 is
    zebra's business and this engine has never held one.
    """
    return spread_valuation.intrinsic_floor(trade, spot)


def spot_corroborates(state: dict, spot: float, spread_val,
                      now: float = None) -> tuple:
    """Does the underlying explain this structure move? (ok, reason).

    Returns ok=True whenever it cannot prove otherwise — no reference yet, a
    stale reference, a rise rather than a collapse. It VETOES one specific
    shape: the structure falling off a cliff while spot barely moves, which is
    the NHPC signature and is not reachable by any real repricing of a vertical
    spread.

    `state` is per-run in-memory (like the confirm counters); the reference
    updates only on readings that were themselves reliable, so a garbage read
    can never become the baseline that a later real move is judged against.
    """
    now = time.time() if now is None else now
    ref_spot, ref_spread = state.get('spot'), state.get('spread')
    ref_t = state.get('t', 0.0)
    ok, reason = True, ''
    if (ref_spread is not None and ref_spot and spread_val is not None
            and now - ref_t <= CORROBORATION_STALE_SEC and ref_spread > 0):
        drop = (ref_spread - spread_val) / ref_spread
        spot_move = abs(spot - ref_spot) / ref_spot if ref_spot else 1.0
        if drop >= SPREAD_COLLAPSE_PCT and spot_move < SPOT_MOVE_MIN_PCT:
            ok = False
            reason = ('uncorroborated collapse: spread %.2f -> %.2f (-%.0f%%) '
                      'on a %.2f%% spot move' % (ref_spread, spread_val,
                                                 drop * 100, spot_move * 100))
    if ok and spread_val is not None:
        state.update({'spot': spot, 'spread': spread_val, 't': now})
    return ok, reason


def price_anchor(depth: dict) -> float:
    """External fair-price anchor for execution caps.

    max(today's LTP, prev close) — deliberately the LOOSER reference, because
    LTP on illiquid options can be a lone garbage/stale print and the anchor's
    only job is a last-resort bound on urgent fills; a too-tight anchor could
    block a real exit. 0 if no reference exists (brand-new strike).
    """
    ltp = depth['ltp'] if (depth.get('traded_today') and (depth.get('ltp') or 0) > 0) else 0
    return max(ltp, depth.get('prev_close') or 0)


def get_spread_value(kite: KiteConnect, trade: dict, spot: float = None,
                     fresh: bool = False) -> dict:
    """Fetch both legs and compute spread value (long bid - short ask).

    Returns dict with long depth, short depth, computed spread, and
    'unreliable' (reason string, or None if both books are trustworthy).
    Spread is None whenever EITHER leg's book fails leg_quote_reliable() —
    a garbage book must never produce a tradeable valuation (2026-07-24).

    When `spot` is supplied, a value below the no-arbitrage floor is also
    rejected. Note this catches a DIFFERENT failure from the width/LTP tests:
    those judge the book's SHAPE, this judges whether the number is possible at
    all. A well-formed book can still quote an impossible price.

    `fresh=True` forces a new read of both legs, bypassing the per-poll quote
    cache. The re-verify after a valuation trigger MUST pass it: re-checking a
    trigger against the very quote that caused it is not a check.
    """
    long_d = get_option_depth(kite, trade['exchange'], trade['long_symbol'],
                              fresh=fresh)
    short_d = get_option_depth(kite, trade['exchange'], trade['short_symbol'],
                               fresh=fresh)

    long_ok, long_why = leg_quote_reliable(long_d)
    short_ok, short_why = leg_quote_reliable(short_d)

    spread_val = None
    unreliable = None
    if not long_ok:
        unreliable = f'long {long_why}'
    elif not short_ok:
        unreliable = f'short {short_why}'
    else:
        spread_val = long_d['bid'] - short_d['ask']
        # ── MATHEMATICAL BOUNDS: clamp. Do not refuse. ──────────────────
        #
        # This used to read "negative spread = dislocation, treat as
        # unreliable" and return None. `zebra/monitor._structure_quote` was
        # fixed to clamp in August and this — the engine that will own LIVE
        # exits — was not. CLAUDE.md's valuation-bounds table has said clamp
        # since it was written. [[feedback_the_copy_you_did_not_open]], on the
        # LOSS path.
        #
        # Why clamping is right: expiry is always available and costs nothing,
        # so a debit structure is never worth less than zero, and long bid
        # 0.55 / short ask 0.60 is an ORDINARY book for a worthless spread —
        # "worth about nothing", not "unpriceable". Both legs have already
        # passed `leg_quote_reliable` to get here, so the shape is not junk.
        #
        # What refusing cost: after a gap straight through the debit stop the
        # book looks exactly like that, so SL_SPREAD could never CONFIRM, the
        # TIME close (normal urgency, never trades a garbage book) aborted on
        # it daily, and the position rode to EXPIRY_FORCE_CLOSE — the one
        # close allowed to pay through anything, at the worst prices of its
        # life. Booking the loss beats stranding the position.
        #
        # The genuinely impossible is still refused, one branch down: an
        # inverted ITM book clamps to 0 and is then caught by the intrinsic
        # floor, which is large and positive exactly there. The clamp only
        # survives where the floor is at or below zero, which is where the
        # spread really is worthless.
        if spread_val < 0:
            spread_val = 0.0
        width = _spread_width(trade)
        if width and spread_val > width:
            # A vertical's ceiling. The mirror of the clamp above, and the
            # same table: a value above width is arithmetic, not information.
            spread_val = float(width)
        if spot is not None and spot > 0:
            floor = spread_intrinsic_floor(trade, spot)
            if floor is not None and spread_val < floor:
                # Below what the structure could be unwound for. Impossible,
                # not unlucky — refuse the valuation entirely rather than
                # clamping it: in a LIVE order path a clamped number would
                # still be a number we might trade on.
                unreliable = (f'below_intrinsic {spread_val:.2f} < floor '
                              f'{floor:.2f} at spot {spot:.2f}')
                spread_val = None

    return {
        'long': long_d,
        'short': short_d,
        'spread': spread_val,
        'unreliable': unreliable,
    }


# ── Strategy Detection ───────────────────────────────────────────────────────

def vertical_direction(trade: dict):
    """'BCS' | 'BPS' | None, read from the LEG SYMBOLS.

    Every store but one holds a single direction, so `_store_type` has always
    been enough. The zebra store is not like that: it holds bull call spreads
    (`direction: 'CE'`, long the LOWER strike, TP on a RISE) and bear put
    spreads (`direction: 'PE'`, long the HIGHER strike, TP on a FALL) side by
    side in one book. KOTAKBANK 395/410 CE has tp_spot 407.8 above sl_spot
    383.63; TMPV 320/310 PE has tp_spot 309.29 BELOW sl_spot 330.78.

    So direction has to come from the contract, not the bookkeeping — the same
    conclusion B21 reached for the intrinsic floor and B23 for misfiled
    records. Both legs CE is a call vertical, both PE a put vertical; anything
    else (a missing symbol, an unreadable suffix, mixed legs) returns None and
    the caller must refuse to guess.
    """
    lo = _sym.option_type(trade.get('long_symbol'))
    sh = _sym.option_type(trade.get('short_symbol'))
    if lo is None or sh is None or lo != sh:
        return None
    return 'BCS' if lo == 'CE' else 'BPS'


def get_strategy(trade: dict) -> str:
    """Detect strategy from trade fields.

    FH has long_put_symbol + short_call_symbol.
    A zebra record is classified from its SYMBOLS — that store holds both
    directions, see `vertical_direction`. An unreadable pair falls back to
    'BCS' here, but `_malformed_reason` has already quarantined such a record,
    so the fallback is never what actually manages a position.
    BPS otherwise has _store_type='bps' (same field names as BCS).
    BCS is the default.
    """
    if 'long_put_symbol' in trade:
        return 'FH'
    if trade.get('_store_type') == 'zebra':
        return vertical_direction(trade) or 'BCS'
    if trade.get('_store_type') == 'bps':
        return 'BPS'
    return 'BCS'


def get_fh_position_value(kite: KiteConnect, trade: dict) -> dict:
    """Fetch all FH legs and compute net position value (cost to close).

    Returns dict with leg depths, close cost, and P&L per share.
    P&L = total_credit - close_cost (credit strategy).
    """
    exchange = trade['exchange']
    sc_d = get_option_depth(kite, exchange, trade['short_call_symbol'])
    sp_d = get_option_depth(kite, exchange, trade['short_put_symbol'])
    lp_d = get_option_depth(kite, exchange, trade['long_put_symbol'])
    lc_d = None
    if trade.get('long_call_symbol'):
        lc_d = get_option_depth(kite, exchange, trade['long_call_symbol'])

    # Cost to close = buy back shorts (ask) - sell longs (bid)
    close_cost = None
    if sc_d['ask'] > 0 and sp_d['ask'] > 0 and lp_d['bid'] > 0:
        cost = sc_d['ask'] + sp_d['ask'] - lp_d['bid']
        if lc_d and lc_d['bid'] > 0:
            cost -= lc_d['bid']
        close_cost = cost

    pnl_per_share = None
    if close_cost is not None:
        pnl_per_share = trade['total_credit'] - close_cost

    return {
        'short_call': sc_d,
        'short_put': sp_d,
        'long_put': lp_d,
        'long_call': lc_d,
        'close_cost': close_cost,
        'pnl_per_share': pnl_per_share,
    }


# ── Position & Alerting Helpers ──────────────────────────────────────────────

def get_net_position(kite: KiteConnect, symbol: str) -> int:
    """Get net quantity for a tradingsymbol from Kite positions."""
    for p in kite.positions()['net']:
        if p['tradingsymbol'] == symbol:
            return p['quantity']
    return 0


def is_market_settled() -> bool:
    """Check if we're past the initial market-open volatility window.

    At 09:15, order books are thin/empty and spread values are unreliable.
    Returns True once MARKET_OPEN_BUFFER_SEC has elapsed after open.
    """
    from datetime import timedelta
    settle_time = datetime.combine(today_ist(), MARKET_OPEN) + timedelta(seconds=MARKET_OPEN_BUFFER_SEC)
    return now_ist() >= settle_time


# ── Resumption after an interruption ───────────────────────────────────────
# Both buffers above are computed from a FIXED clock time, so they arm once at
# 09:18 / 09:30 and stay armed for the rest of the session. That assumes the
# session runs uninterrupted. It does not: an exchange halt, a broker outage, a
# network drop, or this process crashing and being restarted by the 5-minute
# cron all end with the monitor looking at a book that is exactly as unformed
# as it was at 09:15 — while `is_spread_settled()` cheerfully reports True.
#
# Only the SPREAD (value) buffer re-arms. Spot triggers run off real trades in
# the underlying and are the ones that catch a dead thesis, so delaying them
# would suppress the exit that should still work. This is the same split the
# rest of this file makes: value triggers are the ones a broken book can fake.
RESUME_SETTLE_SEC = 180

_last_ok_poll: Optional[float] = None
_resume_settle_at: Optional[float] = None


def reset_poll_state() -> None:
    """Clear resumption state (process start, and tests)."""
    global _last_ok_poll, _resume_settle_at
    _last_ok_poll = None
    _resume_settle_at = None


def note_poll(ok: bool, now: Optional[float] = None,
              gap_sec: int = RESUME_SETTLE_SEC) -> bool:
    """Record a poll outcome; re-arm the spread buffer after a long blackout.

    Returns True when this call armed a fresh buffer. `gap_sec` is both the
    blackout that counts as an interruption and the buffer served afterwards —
    one knob, because they answer the same question: how long is long enough to
    mean the book has to re-form.
    """
    global _last_ok_poll, _resume_settle_at
    now = time.time() if now is None else now
    if not ok:
        return False
    prev, _last_ok_poll = _last_ok_poll, now
    if prev is not None and (now - prev) > gap_sec:
        _resume_settle_at = now + gap_sec
        log(f"  Resumed after a {int(now - prev)}s blackout — spread-value "
            f"triggers re-armed for {gap_sec}s (book must re-form)")
        return True
    return False


def is_spread_settled(now: Optional[float] = None) -> bool:
    """Longer buffer for SPREAD-VALUE triggers (SL_SPREAD / SL_TRAIL / trail
    updates). The 2026-07-24 incident proved illiquid option books are still
    unformed at 09:18; they reliably form by 09:30. SL_SPOT and TP are
    spot-based and keep their existing (shorter/no) buffers.

    Also False during a post-interruption buffer — see note_poll.
    """
    from datetime import timedelta
    settle_time = datetime.combine(today_ist(), MARKET_OPEN) + timedelta(seconds=SPREAD_TRIGGER_OPEN_BUFFER_SEC)
    if now_ist() < settle_time:
        return False
    now = time.time() if now is None else now
    return _resume_settle_at is None or now >= _resume_settle_at


# ── Exit-engine heartbeat ───────────────────────────────────────────────────
# `exits_managed_externally` is a ONE-SIDED stand-down. It is read only by
# `zebra/monitor.py`; this file has never contained the string. So zebra hands
# the cohort's stops to this process on the strength of its own config alone,
# without ever checking that this process exists. Flag on + this monitor not
# running = NO exit engine at all, and nothing anywhere looks wrong: zebra logs
# "EXITS EXTERNAL ... measured, not acted on" and carries on, its alerts keep
# arriving, and the book is unmanaged. `feedback_never_asked_is_not_failed`:
# "we stood down" and "we stood down and nobody took over" need opposite
# responses and had one indistinguishable appearance.
#
# The kill switch reaches the same state by a different road. Tripping it does
# not stop this monitor, it forces DRY RUN for the session — so the process is
# alive, polling and alerting while unable to place a single closing order. If
# zebra has already stood down, nothing books. Alerts continuing is exactly
# what makes that look healthy.
#
# Hence two facts per beat, never one: that the engine polled, and whether it
# could actually book. RUNNING IS NOT ABLE-TO-CLOSE.
#
# This module only ever WRITES. `zebra/monitor.py` is the reader and resolves
# the same filename through its own LOG_DIR; a test pins the two paths equal,
# because a constant copied into two modules is the single most frequent bug
# shape in this codebase (`feedback_the_copy_you_did_not_open`).
HEARTBEAT_NAME = 'exit_engine_heartbeat.json'
HEARTBEAT_SCHEMA = 1


def heartbeat_path() -> Path:
    """Resolved per call, never cached at import — tests repoint LOG_DIR."""
    return LOG_DIR / HEARTBEAT_NAME


def write_heartbeat(state: str, dry_run: bool, *, open_trades: int = 0,
                    cohort_trades: int = 0, cohort_store: bool = False,
                    kill_switch: bool = False,
                    now: Optional[float] = None) -> bool:
    """Stamp one poll of the exit engine. Best-effort; never raises.

    `state` is what this process is doing — 'startup', 'waiting' (pre-open),
    'polling', 'idle' (started, nothing to watch) or 'closed' (session over).
    A beat is written even with an EMPTY book, deliberately: a monitor with
    nothing to do is not a dead one, and "quiet" must never read as "dead".

    Written atomically (tmp + replace) so a reader on its own 5-minute cron can
    never see a half-written file and conclude the peer is corrupt.

    Raising here would be the tail wagging the dog: a failure to write the
    health file must not be able to stop the monitoring the file describes.
    """
    now = time.time() if now is None else now
    payload = {
        'schema': HEARTBEAT_SCHEMA,
        'ts': now,
        'at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'state': state,
        # The whole reason this is not a touch-file. `dry_run` True means this
        # process watches and alerts but places NO order — safe on its own,
        # catastrophic in combination with a stood-down zebra.
        'dry_run': bool(dry_run),
        'kill_switch': bool(kill_switch),
        'open_trades': int(open_trades),
        'cohort_trades': int(cohort_trades),
        # False = the fourth book could not be opened, so cohort positions are
        # not being watched even though this process is alive and armed.
        'cohort_store': bool(cohort_store),
        'pid': os.getpid(),
    }
    try:
        LOG_DIR.mkdir(exist_ok=True)
        path = heartbeat_path()
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w') as f:
            json.dump(payload, f)
        tmp.replace(path)
        return True
    except Exception as e:
        log(f"  WARNING: heartbeat write failed ({e}) — zebra cannot tell "
            f"this engine is alive")
        return False


def _beat_all(state: str, dry_run: bool, trades: list, zebra_store,
              kill_switch: bool = False) -> bool:
    """`write_heartbeat` with the counts read off the book `monitor_all` holds.

    One derivation of "how many cohort positions is this engine watching",
    used by every call site, so a beat can never disagree with another beat
    about what the same loaded book contained.
    """
    return write_heartbeat(
        state, dry_run,
        open_trades=len(trades),
        # `_store_type` is stamped by `_load_all_trades`; 'zebra' IS the
        # cohort book, and it is the only one zebra stands down over.
        cohort_trades=sum(1 for t in trades
                          if t.get('_store_type') == 'zebra'),
        cohort_store=zebra_store is not None,
        kill_switch=kill_switch)


def is_expiry_day(trade: dict, today: Optional[date] = None) -> bool:
    """Check if today is the trade's expiry date.

    `today` is injectable so `time_stop_due` can answer for a given date
    without this branch quietly reading the wall clock while the other branch
    honours the argument — a function that disagrees with its own parameter is
    untestable off expiry day (`feedback_pin_the_wall_clock_in_tests`).
    Every existing caller passes nothing and gets the old behaviour.
    """
    try:
        expiry_str = trade.get('expiry', '')
        expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
        return (today or today_ist()) == expiry_date
    except (ValueError, TypeError):
        return False


EXPIRY_FORCE_CLOSE_TIME = dtime(15, 15)   # Force close by 15:15 on expiry day

# ── Physical-delivery margin proximity ─────────────────────────────────────
# Indian single-stock options are PHYSICALLY settled, and the exchange levies a
# delivery margin on ITM options that ramps up over the final trading sessions
# before expiry. Until now the only expiry handling in this file was a
# force-close ON EXPIRY DAY — i.e. after the entire ramp has been paid, and
# late enough that a broker short of margin may square the position off first,
# at a price nobody chose.
#
# Both legs of a BCS are calls on one underlying, so if both finish ITM the
# delivery obligations largely offset. That is an exchange/broker policy
# question, not one this code can answer, and brokers apply their own stricter
# rules — so this WARNS with the facts (sessions left, which legs are ITM) and
# closes nothing. Confirm the exact schedule and your broker's netting policy
# before relying on the numbers.
DELIVERY_MARGIN_SESSIONS = 4    # ramp is widely documented as starting at E-4
EXPIRY_WARN_SESSIONS = 5        # warn one session BEFORE the ramp starts


def expiry_warn_sessions(trade: dict) -> int:
    """When to start warning about THIS trade's expiry.

    A flat 5 was a silent no-op for the cohort the moment M10 moved its time
    stop to 6 sessions: the stop fires at 6, the warn only starts at 5, so the
    position is already closed before its own "expiry is close" nag can fire.
    A warning that can only arrive after the thing it warns about is not a
    warning, and nothing would have said so — it just stops appearing.

    So it is derived per-record: one session ahead of whichever close comes
    first for that trade. The three default-policy books keep 5 (their close
    is expiry day itself); a cohort record with `time_stop_sessions: 6` warns
    from 7.
    """
    try:
        n = int(trade.get('time_stop_sessions') or 0)
    except (TypeError, ValueError):
        n = 0
    return max(EXPIRY_WARN_SESSIONS, n + 1)


def sessions_to_expiry(trade: dict, today: Optional[date] = None) -> Optional[int]:
    """Trading sessions remaining, expiry inclusive. 0 = expiry day.

    Counts SESSIONS, not calendar days: over a weekend a calendar count reads
    "3 days left" when only one session remains, which is precisely when this
    warning matters most.

    **Holidays included since 2026-08-29 (M4/M10).** This used to skip
    weekends and nothing else, and the omission pointed the wrong way against
    real money: a holiday inside the window made this an OVER-estimate — more
    sessions reported than remain — so the close fired LATER, while NSE moves
    each delivery-margin tranche EARLIER around a holiday (it collects a
    holiday's margins on the preceding session). The two errors compounded in
    one direction, and that direction is "still holding when the ramp starts".

    `common/nse_holidays.py` is a STATIC list with a coverage window. Past it
    this degrades to weekday-only — the old behaviour — and says so in the
    log rather than continuing to look authoritative.
    """
    try:
        expiry = datetime.strptime(trade.get('expiry', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
    d = today or today_ist()
    if expiry < d:
        return 0
    return nse_holidays.sessions_between(d, expiry, warn=log)


def time_stop_due(trade: dict, today: Optional[date] = None,
                  spot: Optional[float] = None) -> bool:
    """Does this trade's TIME policy say close it today?

    Two policies, and which one applies is a property of the TRADE:

    * default (`None`) -- EXPIRY DAY itself. What the three original books have
      always done: warn from E-5, force-close on the last day.
    * `sessions_before_expiry` -- N trading SESSIONS before expiry,
      unconditional. zebra's rule, and what the BCS cohort was measured with.
      It fires EARLIER and is therefore the stricter of the two.

    The rule has to travel with the record for the same reason `spot_sl_enabled`
    does: `zebra/monitor.py` stands down for a cohort position once the order
    path owns it (`exits_managed_externally`), and if this engine then applied
    only its own expiry-day rule, the handover would quietly DELETE a stop that
    fires four sessions earlier. A migration must not weaken a rule by omission.

    A malformed session count falls back to the default rather than to "never":
    a time stop that cannot be computed must still fire on expiry day.
    """
    if trade.get('time_policy') != 'sessions_before_expiry':
        return is_expiry_day(trade, today)
    try:
        # M10. `delivery_stop_sessions` is the record's own count with the
        # moneyness FLOOR applied -- it can only ever raise it, and a larger
        # count fires the stop EARLIER. Passing `spot=None` (no live price
        # this cycle) leaves the stored count untouched rather than guessing.
        n = int(delivery_stop_sessions(trade, spot))
        if n <= 0:
            n = int(trade.get('time_stop_sessions'))
    except (TypeError, ValueError):
        log(f"  WARNING: #{trade.get('id')} {trade.get('stock')} has "
            f"time_policy=sessions_before_expiry but "
            f"time_stop_sessions={trade.get('time_stop_sessions')!r} — "
            f"falling back to the expiry-day rule")
        return is_expiry_day(trade, today)
    if n < 0:
        return is_expiry_day(trade, today)
    left = sessions_to_expiry(trade, today)
    # `left is None` means the expiry itself is unparseable, which
    # `is_expiry_day` also reports as False. Nothing to stand on either way.
    return left is not None and left <= n


# ── The time stop has THREE outcomes, not two (M11) ────────────────────────
#
# It used to have two: armed, or absent. `expiry_trades[key] = True` was set
# once and every later poll short-circuited on `if key in expiry_trades`, so a
# force-close that did NOT fill was indistinguishable from one that did. The
# dict is per-process, so the only retry was TOMORROW, from a fresh process.
#
# That is expensive because of what the delivery-margin ramp costs. The margin
# is levied on the long ITM leg at its STRIKE — full contract value, ~Rs 2.82L
# against a Rs 2L account — and ramps 10/25/45/70% at EOD of E-4/E-3/E-2/E-1.
# The close is scheduled six sessions out precisely to stay clear of it, so a
# day lost to a silent retry spends a sixth of that buffer and moves the
# position one tranche closer. For a bear put spread the tail has no ceiling:
# a long ITM put is a GIVE-delivery obligation against an empty demat, and
# short delivery goes to auction with a 20% floor and no cap.
#
# So: ARMED / CLOSED / FAILED, explicitly, and the third is PERSISTED on the
# record. The in-memory dict loses it on restart, which is exactly when it
# matters — a restarted monitor would otherwise re-arm and re-send the routine
# "will force-close by 15:15" line for a stop that has already failed twice.
#
# What did NOT change: arming is still once per session. That flag exists so
# the alert is not repeated every five minutes and that behaviour is correct.
TIME_STOP_MAX_ATTEMPTS = 3
#: Seconds to wait before attempt 2, then attempt 3. SHRINKING, deliberately:
#: the whole retry window is 15:15 -> HARD_ORDER_CUTOFF_TIME, ten minutes, and
#: each attempt burns its own MAX_RETRIES of order timeouts inside close_leg.
#: Urgency escalates as the cutoff approaches; it cannot escalate through the
#: order pricing, which is already at maximum for an URGENT_CLOSE_REASON.
TIME_STOP_RETRY_WAITS = (120, 60)


def time_stop_window_open(now_time: Optional[dtime] = None) -> bool:
    """Is the clock past the force-close time? ONE definition of that test.

    Trivial on its own; it exists so the three places that ask cannot drift
    apart, which is the single most frequent bug shape in this codebase.
    """
    now_time = now_ist().time() if now_time is None else now_time
    return now_time >= EXPIRY_FORCE_CLOSE_TIME


def new_time_stop_state(trade: dict, stamp: str) -> dict:
    """ARMED, unless the RECORD says a process already tried today and failed.

    A restart mid-session is the case this exists for: the cron relaunches this
    process every five minutes after a crash, and an in-memory-only flag comes
    back as "not yet armed", which then reads as a first arming and re-alerts
    accordingly. The persisted counter makes the resumed state honest.

    A stamp from a PREVIOUS day is ignored: attempts do not accumulate across
    sessions, and a stale counter would silently spend today's retries.
    """
    prior = trade.get('time_stop_attempt')
    if isinstance(prior, dict) and prior.get('date') == stamp:
        try:
            attempts = int(prior.get('attempts') or 0)
        except (TypeError, ValueError):
            attempts = 0
        state = prior.get('state')
        if state not in ('failed', 'escalated', 'closed'):
            state = 'failed'   # unrecognised -> the pessimistic reading
        return {'date': stamp, 'state': state, 'attempts': max(0, attempts),
                'next_attempt_after': 0.0}
    return {'date': stamp, 'state': 'armed', 'attempts': 0,
            'next_attempt_after': 0.0}


def time_stop_attempt_due(state: Optional[dict], now: Optional[float] = None,
                          now_time: Optional[dtime] = None) -> bool:
    """May a force-close be ATTEMPTED for this trade right now?

    Separate from `time_stop_due`, which answers "does the policy say close it
    TODAY" and remains the only place that decides that. This answers "has the
    close already been discharged", which is a different question and the one
    the old `key in expiry_trades` conflated with it.

      armed     -> yes, first attempt
      failed    -> yes, again, THIS session, after a shrinking backoff
      closed    -> never
      escalated -> never; a human has been called
    """
    if not state or state.get('state') in ('closed', 'escalated'):
        return False
    if state.get('attempts', 0) >= TIME_STOP_MAX_ATTEMPTS:
        return False
    now = time.time() if now is None else now
    if now < state.get('next_attempt_after', 0.0):
        return False
    return time_stop_window_open(now_time)


def _persist_time_stop(trade: dict, store, state: dict) -> None:
    """Write the attempt counter to the record. Never raises.

    Same doctrine as `expiry_warn_date`: losing the flag costs a duplicated
    alert, losing the CLOSE costs a delivery obligation, so a failed write can
    never block the close path.
    """
    payload = {'date': state['date'], 'state': state['state'],
               'attempts': state['attempts']}
    trade['time_stop_attempt'] = payload
    if store is None:
        return
    try:
        store.update_trade_fields(trade['id'], time_stop_attempt=payload)
    except Exception as e:
        log(f"  WARNING: could not persist time-stop attempt state for "
            f"#{trade.get('id')}: {e} — a restart will re-arm as if this "
            f"close had never been tried")


def record_time_stop_result(state: dict, success, trade: dict, store,
                            label: str, now: Optional[float] = None,
                            now_time: Optional[dtime] = None) -> str:
    """Fold one force-close outcome into the three-state machine.

    Returns the new state name. Telegram-escalates exactly ONCE, on the final
    failure — the point at which no further automatic attempt will be made
    today and the position is still open inside the delivery ramp.

    `'ABORT'` is not a failure to close: nothing was placed and the trade is
    still open, so it does not consume an attempt. It still backs off, because
    an abort that repeats every five seconds is a spin, not a retry.
    """
    now = time.time() if now is None else now
    now_time = now_ist().time() if now_time is None else now_time

    if success == 'ABORT':
        state['state'] = 'armed'
        state['next_attempt_after'] = now + TIME_STOP_RETRY_WAITS[0]
        log(f"  {label} #{trade.get('id')} {trade.get('stock')}: force close "
            f"ABORTED before any order. Time stop stays armed.")
        return 'armed'

    #: `'MANUAL'` — Fallen Hero, which the system does not trade. It consumes
    #: an attempt and walks the same retry-then-escalate ladder, because the
    #: ladder's real product is ESCALATING PRESSURE ON A HUMAN and that is
    #: exactly what is wanted here. Only the words differ, and they have to:
    #: "force close FAILED, none filled" would describe an order this system
    #: never places, and an alert that misdescribes itself is how a reader
    #: learns to skip it.
    manual = (success == 'MANUAL')

    if success is True:
        state['state'] = 'closed'
        state['next_attempt_after'] = 0.0
        # Deliberately NOT persisted: a closed record leaves get_open_trades,
        # so no restart can re-arm it, and writing to a just-closed record is
        # a store mutation this path has no reason to make.
        return 'closed'

    state['attempts'] = state.get('attempts', 0) + 1
    attempts = state['attempts']
    # No more attempts today when the retries are spent OR when the broker's
    # hard reduce-only cutoff has passed — past it `close_spread`'s late-day
    # guard refuses to place anything, so "retrying" would only delay the one
    # thing that still helps, which is telling the human.
    final = (attempts >= TIME_STOP_MAX_ATTEMPTS
             or now_time >= HARD_ORDER_CUTOFF_TIME)
    state['state'] = 'escalated' if final else 'failed'
    wait = TIME_STOP_RETRY_WAITS[min(attempts - 1,
                                     len(TIME_STOP_RETRY_WAITS) - 1)]
    state['next_attempt_after'] = 0.0 if final else now + wait
    _persist_time_stop(trade, store, state)

    who = f"{label} #{trade.get('id')} {trade.get('stock')}"
    if final and manual:
        log(f"  {who}: {attempts} manual-close alert(s) sent and the position "
            f"is STILL OPEN. No further reminder today.")
        send_telegram(
            f"🚨 {who}: STILL OPEN ON EXPIRY DAY\n"
            f"{attempts} reminder(s) sent this session. This is a Fallen Hero "
            f"— the system places NO orders for it, by design.\n"
            f"The position is OPEN inside the delivery-margin ramp and its "
            f"short call is the largest delivery exposure in the book. "
            f"CLOSE IT BY HAND IN KITE NOW.")
    elif final:
        log(f"  {who}: EXPIRY FORCE CLOSE FAILED after {attempts} attempt(s). "
            f"No further automatic attempt today. Manual intervention needed!")
        send_telegram(
            f"🚨 {who}: EXPIRY FORCE CLOSE FAILED\n"
            f"{attempts} attempt(s) this session, none filled. No further "
            f"automatic attempt today.\n"
            f"The position is OPEN inside the delivery-margin ramp — close it "
            f"by hand in Kite NOW.")
    elif manual:
        log(f"  {who}: manual-close reminder {attempts}/"
            f"{TIME_STOP_MAX_ATTEMPTS} sent. Next in {wait}s while the "
            f"position is still open.")
    else:
        # "while the position is still open" is not hedging. Every close
        # failure past the close-lock freezes the record at `partial_close`,
        # which leaves `get_open_trades` and therefore leaves this loop — by
        # design, because a frozen position must not be given more orders.
        # Those paths send their own CRITICAL/FLIPPED alert, so promising an
        # unconditional retry here would be the only untrue line in the thread.
        log(f"  {who}: force close attempt {attempts}/"
            f"{TIME_STOP_MAX_ATTEMPTS} did not fill. Retrying in {wait}s "
            f"(same session) while the position is still open.")
        send_telegram(
            f"⚠️ {who}: force-close attempt {attempts}/"
            f"{TIME_STOP_MAX_ATTEMPTS} did not fill. Retrying in ~{wait}s "
            f"while the position is still open.")
    return state['state']


def _arming_spot(kite, trade: dict) -> Optional[float]:
    """Spot for the M10 moneyness floor, or None. Never raises.

    A separate quote at the three arming sites rather than threading the poll
    loop's `spot` down to them: two of the three arm a trade that has just
    appeared mid-session, BEFORE the loop has fetched its price, and moving
    the arming below that fetch would reorder the one call site the time-stop
    policy is deliberately funnelled through.

    None is safe by construction. Without a price the floor does not apply and
    the record's stored session count stands -- and the floor can only ever
    fire a close EARLIER, so losing it costs the head start and never the
    close.
    """
    try:
        return get_spot(kite, trade['spot_symbol'])
    except Exception as e:
        log(f"  WARNING: no spot for the delivery moneyness floor on "
            f"#{trade.get('id')} {trade.get('stock')} ({e}) — the scheduled "
            f"close stands")
        return None


def arm_time_stop(trade: dict, key, expiry_trades: dict, label: str,
                  note: str = '', today: Optional[date] = None,
                  store=None, spot: Optional[float] = None) -> bool:
    """Arm the force-close flag when this trade's TIME policy says today.

    ONE call site for `time_stop_due`, deliberately. There were three -- the
    startup sweep, the BCS mid-session init and the FH mid-session check -- and
    a mutation reverting only the mid-session pair to `is_expiry_day` survived
    the entire suite. The policy was honoured on the path the tests drive and
    silently not on the other two, which is the same shape as the trail_state /
    expiry_trades key mismatch: a rule applied in one place and quietly not in
    its copy.

    The RETRY lives behind this same call site for the same reason: the value
    stored in `expiry_trades` is the attempt state, so there is nowhere else to
    decide whether a time stop is outstanding.

    Returns True when it armed this call (False if already armed, or not due).
    """
    if key in expiry_trades or not time_stop_due(trade, today, spot=spot):
        return False
    stamp = (today or today_ist()).isoformat()
    state = new_time_stop_state(trade, stamp)
    expiry_trades[key] = state
    tail = f" ({note})" if note else ''
    when = EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')

    if state['state'] != 'armed':
        # A process earlier TODAY already tried this close and it did not fill.
        # Re-arming must not read as a first arming: the routine line below
        # would announce a force-close that has already failed twice as though
        # nothing had happened, which is the exact silence M11 exists to end.
        more = (". No further automatic attempt — close it by hand."
                if state['state'] in ('escalated', 'closed')
                else f". Retrying this session "
                     f"(max {TIME_STOP_MAX_ATTEMPTS} attempts).")
        log(f"  *** {label} #{trade['id']} {trade['stock']}: TIME STOP RESUMED"
            f"{tail} — state={state['state']}, "
            f"{state['attempts']} failed attempt(s) today ***")
        send_telegram(
            f"{label} #{trade['id']} {trade['stock']}: TIME STOP RESUMED after "
            f"a monitor restart{tail}. {state['attempts']} force-close "
            f"attempt(s) already FAILED today{more}")
        return True

    early = (trade.get('time_policy') == 'sessions_before_expiry'
             and not is_expiry_day(trade, today))
    why = 'TIME STOP' if early else 'EXPIRY DAY'
    log(f"  *** {label} #{trade['id']} {trade['stock']}: {why}{tail}! "
        f"Force-close by {when} ***")
    send_telegram(f"{label} #{trade['id']} {trade['stock']}: {why}{tail}. "
                  f"Will force-close by {when}.")
    return True


def delivery_exposure(trade: dict, spot: float) -> dict:
    """Which legs are ITM — i.e. which carry a delivery obligation at expiry.

    Reads every `*_symbol` leg on the record rather than the BCS pair, so this
    is correct for the bear put spread and for Fallen Hero — whose SHORT CALL
    is the largest delivery exposure in the fleet. A BCS-shaped implementation
    would have found no legs on an FH record and reported "ITM legs: none",
    which is not a missing feature but an actively false all-clear on the one
    position where being wrong costs the most.

    `itm` is None (unknown), never an empty list, when no leg could be parsed.
    """
    legs = []
    for key, val in trade.items():
        if not key.endswith('_symbol') or key == 'spot_symbol':
            continue
        sym = str(val or '').upper()
        m = re.search(r'(\d+(?:\.\d+)?)(CE|PE)$', sym)
        if not m:
            continue
        strike, kind = float(m.group(1)), m.group(2)
        itm = spot > strike if kind == 'CE' else spot < strike
        legs.append({'leg': key[:-len('_symbol')], 'strike': strike,
                     'type': kind, 'itm': itm})
    return {'legs': legs,
            'itm': None if not legs else [l['leg'] for l in legs if l['itm']]}


# ── M10 remainders: moneyness, and the E-9 preflight ───────────────────────
#
# The 6-session close (`time_stop_sessions`) is sized to clear the delivery
# ramp, which starts at E-4. Two things were designed with it and are built
# below (2026-08-30); the reasoning is kept because both run backwards.
#
# **The session count is a FLOOR, never a ceiling.** The intuition runs
# backwards, which is why it is written down: a far-OTM spread is worth pennies
# at E-6, so holding it earns nothing and risks nothing; the DEEP-ITM one
# converging on max value is the one with MAXIMUM delivery exposure, because
# the margin base is the long ITM leg AT ITS STRIKE -- full contract value,
# ~Rs 2.82L against a Rs 2L account. So moneyness may pull the close EARLIER
# and may never push it later, and that is enforced with a `max()` rather than
# left to whoever edits the arithmetic next.

#: Extra trading sessions of head start when the LONG leg is ITM, i.e. when a
#: delivery obligation actually exists. One session, not more: the ramp's first
#: tranche lands at EOD of E-4 and the base close is already at E-6, so this
#: buys a whole session of slack without materially shortening the trade.
DELIVERY_MONEYNESS_BONUS_SESSIONS = 1

#: When the preflight starts asking. Three sessions ahead of the base close,
#: so a CLOSE_NOW verdict still has room to be acted on by a human before the
#: automated stop would have fired anyway.
DELIVERY_PREFLIGHT_SESSIONS = 9

#: The whole verdict space. TWO members, and deliberately no DEFER and no HOLD.
#:
#: The exit vet's entire safety argument is that HOLDING IS BOUNDED -- the
#: structure's max loss is the debit and it is already capped, so waiting for a
#: verdict costs at most some of a known number. Past the delivery deadline
#: that premise INVERTS: a long ITM put is a give-delivery obligation against
#: an empty demat, and short delivery goes to auction with a 20% floor and NO
#: CEILING. A gate whose safety argument has inverted must not have a state
#: that means "wait", so this one does not, and `EXPIRY_FORCE_CLOSE` stays out
#: of `bcs.exit_vet.VET_KIND`.
CLOSE_NOW = 'CLOSE_NOW'
CLOSE_ON_SCHEDULE = 'CLOSE_ON_SCHEDULE'
DELIVERY_VERDICTS = (CLOSE_NOW, CLOSE_ON_SCHEDULE)

#: Fraction of max spread value past which there is nothing left to wait for.
#: At or above it a long-ITM position is holding full delivery exposure to
#: collect the last few percent of a capped payoff.
DELIVERY_CAPTURED_PCT = 90.0


def _long_leg_itm(trade: dict, spot: float):
    """Is the LONG leg ITM? None when it cannot be read.

    The long leg, specifically, because that is what the margin is levied on:
    the base is the long ITM leg AT ITS STRIKE. A short ITM leg is the
    counterparty's delivery obligation, not ours, and the broker does not net
    the two -- so "some leg is ITM" is the wrong question and would fire this
    on a spread carrying no obligation at all.

    None, not False, when the symbols cannot be parsed. Unknown is not clear.
    """
    exp = delivery_exposure(trade, spot)
    if exp['itm'] is None:
        return None
    for leg in exp['legs']:
        if leg['leg'] in ('long', 'long_put', 'long_call'):
            return bool(leg['itm'])
    return None


def delivery_stop_sessions(trade: dict, spot: Optional[float] = None) -> int:
    """This trade's time-stop session count, moneyness included.

    `max()` of the record's own count and the moneyness-adjusted one, so the
    stored value is a FLOOR that this can only raise -- and a larger session
    count fires the stop EARLIER. Getting the direction wrong here would push
    an ITM position further INTO the ramp, which is the one outcome the whole
    6-session schedule exists to avoid, so the invariant is expressed as an
    operation rather than as care.

    Unknown moneyness (unreadable symbols, no spot) leaves the base count. It
    does not guess ITM: firing every close a session early on a book whose
    symbols simply did not parse is a real cost paid for no information.
    """
    try:
        base = int(trade.get('time_stop_sessions') or 0)
    except (TypeError, ValueError):
        base = 0
    if spot is None or base <= 0:
        return base
    return max(base, base + DELIVERY_MONEYNESS_BONUS_SESSIONS
               if _long_leg_itm(trade, spot) else base)


def delivery_preflight(trade: dict, spot: Optional[float],
                       spread_val: Optional[float] = None,
                       today: Optional[date] = None) -> Optional[dict]:
    """Close now, or on schedule? Asked from E-9. Returns None before that.

    `{'verdict', 'why', 'sessions'}`. MONOTONIC: once CLOSE_NOW, it stays
    CLOSE_NOW for the life of the position. The delivery deadline only ever
    gets nearer, so a verdict that could soften would be a verdict that lets a
    quiet afternoon undo a decision the calendar already made.

    Alert-only. It changes no stop and places no order; what it produces is a
    named verdict on the record and a message, three sessions ahead of a close
    that is going to happen anyway. That is the whole point of being early.

    Two reasons to say CLOSE_NOW, both deterministic and both about the same
    fact -- an obligation exists and waiting no longer buys anything:

    * a PE spread whose long put is ITM. That is a GIVE-delivery obligation
      against an empty demat: short delivery goes to auction at E+3 with a 20%
      floor and no ceiling, and Do-Not-Exercise was permanently withdrawn in
      Jan 2023, so there is no opt-out. This is the only exposure in the book
      that can exceed the account, and it does not wait for a value threshold.
    * any spread whose long leg is ITM and which has already captured
      DELIVERY_CAPTURED_PCT of its max value. Full contract-value margin base,
      and a capped payoff with almost nothing left in it.
    """
    left = sessions_to_expiry(trade, today)
    if left is None or left > DELIVERY_PREFLIGHT_SESSIONS or left < 0:
        return None
    prior = (trade.get('delivery_preflight') or {}).get('verdict')
    if prior == CLOSE_NOW:
        return {'verdict': CLOSE_NOW, 'sessions': left,
                'why': (trade.get('delivery_preflight') or {}).get('why', ''),
                'monotonic': True}
    itm = _long_leg_itm(trade, spot) if spot is not None else None
    if itm:
        if _is_put_spread(trade):
            return {'verdict': CLOSE_NOW, 'sessions': left, 'monotonic': False,
                    'why': 'the long PUT is ITM — a give-delivery obligation '
                           'against an empty demat, auctioned at E+3 with a '
                           '20% floor and no ceiling'}
        pct = _captured_pct(trade, spread_val)
        if pct is not None and pct >= DELIVERY_CAPTURED_PCT:
            return {'verdict': CLOSE_NOW, 'sessions': left, 'monotonic': False,
                    'why': 'the long leg is ITM and %.0f%% of max value is '
                           'already captured — full contract-value margin '
                           'base for the last %.0f%%' % (pct, 100 - pct)}
    return {'verdict': CLOSE_ON_SCHEDULE, 'sessions': left, 'monotonic': False,
            'why': ('the long leg is not ITM, so no delivery obligation exists'
                    if itm is False else
                    'moneyness could not be read — the scheduled close stands'
                    if itm is None else
                    'ITM but still worth holding to the scheduled close')}


def _is_put_spread(trade: dict) -> bool:
    """A structure whose LONG leg is a put. Reads the symbol, not a label.

    `direction` is zebra's word and `_strategy` is this file's; neither is on
    every record that reaches here, and the fact that matters is what the
    contract actually is.
    """
    sym = str(trade.get('long_put_symbol') or trade.get('long_symbol') or '')
    return sym.upper().endswith('PE')


def _captured_pct(trade: dict, spread_val: Optional[float]) -> Optional[float]:
    """How much of max value this spread has taken, or None if unknowable."""
    if isinstance(spread_val, dict):
        spread_val = spread_val.get('spread')
    try:
        width = float(trade.get('spread_width') or 0)
        debit = float(trade.get('net_debit') or 0)
        val = float(spread_val)
    except (TypeError, ValueError):
        return None
    if width <= debit:
        return None
    return (val - debit) / (width - debit) * 100


def record_delivery_preflight(store, trade: dict, verdict: dict,
                              label: str) -> bool:
    """Persist a preflight verdict and announce a CLOSE_NOW once. Never raises.

    Announced ONCE per position, not once per day: unlike the expiry nag this
    is not a recurring calendar fact but a one-way transition, and the daily
    nag already runs alongside it in the same window.
    """
    if not verdict or verdict.get('monotonic'):
        return False
    prior = (trade.get('delivery_preflight') or {}).get('verdict')
    if prior == verdict['verdict']:
        return False
    row = {'verdict': verdict['verdict'], 'why': verdict['why'],
           'sessions': verdict['sessions'], 'at': now_ist().isoformat()}
    try:
        store.update_trade_fields(trade['id'], delivery_preflight=row)
        trade['delivery_preflight'] = row
    except Exception as e:
        # The ALERT matters more than the record, so this never blocks it.
        log('  WARNING: could not persist the delivery preflight for #%s: %s'
            % (trade.get('id'), e))
    if verdict['verdict'] != CLOSE_NOW:
        log('  %s #%s %s: delivery preflight at E-%d — %s'
            % (label, trade['id'], trade.get('stock', '?'),
               verdict['sessions'], verdict['why']))
        return False
    log('  *** %s #%s %s: DELIVERY PREFLIGHT SAYS CLOSE NOW (E-%d) — %s ***'
        % (label, trade['id'], trade.get('stock', '?'), verdict['sessions'],
           verdict['why']))
    send_telegram(
        '\U000026A0 %s #%s %s: DELIVERY PREFLIGHT — CLOSE NOW\n'
        '%d trading session(s) to expiry (%s).\n%s\n'
        'This is ALERT-ONLY: no order was placed and the scheduled close still '
        'stands. Closing by hand now is the cheaper of the two.'
        % (label, trade['id'], trade.get('stock', '?'), verdict['sessions'],
           trade.get('expiry', '?'), verdict['why']),
        alert_class=alert_policy.SAFETY)
    return True


GAMMA_RULE_SESSIONS = 5          # playbook: "DTE < 5"
GAMMA_RULE_CAPTURE_PCT = 80.0    # "...and spread < 80% max -> close, gamma risk"


def gamma_note(trade: dict, spread_val: Optional[float],
               sessions: int) -> str:
    """The playbook's DTE<5 gamma rule, which nothing has ever enforced.

    Folded into the expiry warning rather than given its own alert: it fires in
    the same window, on the same position, and two notifications about one
    decision is the alert fatigue the gates exist to cure. Reported, never
    acted on — whether the remaining upside is worth the gamma is a judgement,
    and this file gains no new automated close.

    Returns '' when it does not apply or cannot be computed. A missing quote
    yields silence, not a warning built on a number nobody has.
    """
    # Defence in depth: both call sites once handed us get_spread_value's whole
    # dict. float() raised, the caller's except swallowed it, and the warning
    # was silently dead. Accept the dict rather than die on it, and say so.
    if isinstance(spread_val, dict):
        log("  WARNING: gamma_note got a quote dict, not a float — "
            "caller should pass ['spread']")
        spread_val = spread_val.get('spread')
    try:
        width = float(trade.get('spread_width') or 0)
        debit = float(trade.get('net_debit') or 0)
    except (TypeError, ValueError):
        return ''
    if spread_val is None or sessions > GAMMA_RULE_SESSIONS or width <= debit:
        return ''
    try:
        spread_val = float(spread_val)
    except (TypeError, ValueError):
        return ''
    pct = (spread_val - debit) / (width - debit) * 100
    if pct >= GAMMA_RULE_CAPTURE_PCT:
        return ''
    return (f"\nPlaybook: {sessions} session(s) left with only {pct:.0f}% of "
            f"max captured (<{GAMMA_RULE_CAPTURE_PCT:.0f}%) — gamma risk now "
            f"outweighs what is left to win.")


def maybe_warn_expiry_proximity(store, trade: dict, spot: float, label: str,
                                today: Optional[date] = None,
                                spread_val: Optional[float] = None) -> bool:
    """One Telegram per trade per day once expiry is close. Alert only.

    Returns True if a warning was sent this call.
    """
    sessions = sessions_to_expiry(trade, today)
    if sessions is None or sessions > expiry_warn_sessions(trade) or sessions <= 0:
        return False        # expiry day has its own force-close path
    stamp = (today or today_ist()).isoformat()
    if trade.get('expiry_warn_date') == stamp:
        return False        # already nagged today; survives a monitor restart

    # The playbook's DTE<5 gamma rule, which nothing has ever enforced: "DTE < 5
    # and spread < 80% of max -> close, gamma risk". Folded into this warning
    # rather than given its own alert — it fires in the same window, on the
    # same position, and two notifications about one decision is the alert
    # fatigue the gates exist to cure. Reported, never acted on: it is a
    # judgement about whether the remaining upside is worth the gamma, and this
    # file gains no new automated close.
    gamma = gamma_note(trade, spread_val, sessions)

    exp = delivery_exposure(trade, spot)
    itm = exp['itm']
    itm_txt = ('could not read the leg symbols — CHECK MANUALLY' if itm is None
               else (', '.join(itm) if itm else 'none'))
    ramp = 'ACTIVE' if sessions <= DELIVERY_MARGIN_SESSIONS else \
           f'starts in {sessions - DELIVERY_MARGIN_SESSIONS} session(s)'
    msg = (f"⏳ {label} #{trade['id']} {trade['stock']}: "
           f"{sessions} trading session(s) to expiry ({trade.get('expiry')}).\n"
           f"Delivery-margin ramp {ramp}. ITM legs: {itm_txt}.\n"
           f"Physical settlement — close before the margin builds, or confirm "
           f"your broker nets the legs.{gamma}")
    log(f"  *** {label} #{trade['id']} {trade['stock']}: {sessions} session(s) "
        f"to expiry, delivery ramp {ramp}, ITM: {itm_txt} ***")
    send_telegram(msg)
    try:
        store.update_trade_fields(trade['id'], expiry_warn_date=stamp)
        trade['expiry_warn_date'] = stamp
    except Exception as e:
        # A failed flag write means tomorrow's identical nag; losing the WARNING
        # would be the worse failure, so this never blocks the alert.
        log(f"  WARNING: could not persist expiry-warn flag for "
            f"#{trade['id']}: {e}")
    return True


_telegram_cfg: Optional[dict] = None
_telegram_cfg_loaded = False


def send_telegram(msg: str, alert_class: Optional[str] = None):
    """Send Telegram alert. Best-effort: never blocks or crashes trading.

    `alert_class` is one of `bcs.alert_policy`'s classes. Omitting it SENDS —
    the default is deliberate and is the whole reason the policy is a module
    rather than a scatter of `if dry_run` at the call sites: silence has to be
    something someone chose, and it has to be readable in one place.

    A suppressed alert is still LOGGED, in full, with the policy's reason. The
    phone is not the evidence channel; `logs/spread_monitor_cron_*.log` and the
    order journal are, and neither is affected by anything decided here.
    """
    global _telegram_cfg, _telegram_cfg_loaded

    ok, why = alert_policy.should_send(alert_class)
    if not ok:
        first = (msg or '').split('\n')[0]
        log(f"  [telegram suppressed: {why}] {first}")
        for extra in (msg or '').split('\n')[1:]:
            log(f"    | {extra}")
        return

    try:
        # Load config once and cache
        if not _telegram_cfg_loaded:
            config_path = BOTS_ROOT / 'data' / 'telegram_config.json'
            if config_path.exists():
                with open(config_path) as f:
                    _telegram_cfg = json.load(f)
            _telegram_cfg_loaded = True

        if not _telegram_cfg:
            return

        import requests
        requests.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg},
            timeout=10,
        )
    except ImportError:
        log(f"  Telegram alert skipped: 'requests' package not installed")
    except Exception as e:
        log(f"  Telegram alert failed: {e}")


# ── Order Execution ─────────────────────────────────────────────────────────

ORDER_TAG = "BCS_MON"   # Tag for all orders placed by this script

# The only statuses from which an order can NEVER fill again. Anything else —
# including a status this code has never seen — counts as live, so the
# duplicate-order guard fails toward NOT placing a second order on a leg.
_TERMINAL_ORDER_STATUS = frozenset({'COMPLETE', 'REJECTED', 'CANCELLED'})


def find_recoverable_fill(kite: KiteConnect, symbol: str,
                          txn_type: str) -> dict:
    """The fill price of the most recent COMPLETE **ORDER_TAG** order, with
    enough context for the caller to say what went wrong.

    Returns `{'price', 'untagged': int, 'error': str|None}` where `price` is
    None whenever no order THIS SCRIPT placed can be found. Three outcomes, and
    they need three different sentences from whoever reads the alert:

      price set              -- our order, our fill, safe to book
      price None, untagged>0 -- somebody else's fill for this symbol+side
      price None, untagged=0 -- nothing at all in today's order book

    ONLY OUR OWN ORDERS COUNT, and that is the change. This used to return
    `tagged_best or any_best`: with no tagged order it adopted the most recent
    matching order in the ACCOUNT and reported it as this trade's exit fill,
    with a "(WARNING: not tagged by BCS_MON)" note in a log nobody reads at
    14:35. Three ways that goes wrong, and the third is why it had to change:

      * a manual close of the same leg, at a price the operator chose, booked
        as though the monitor had transacted it;
      * `kite.orders()` is TODAY only, so an unrelated same-symbol order from
        this morning outranks the real close from yesterday;
      * a PAPER record adopted by the live bridge has no orders of its own by
        construction, so the fallback is the ONLY thing it could ever find --
        it manufactures an exit price for a position that never existed.

    `average_price > 0` is kept as the acceptance test. A COMPLETE order with a
    zero average price is a broker-side anomaly, not a free fill.
    """
    out = {'price': None, 'untagged': 0, 'error': None}
    try:
        best = None
        for o in kite.orders():
            if (o.get('tradingsymbol') == symbol
                    and o.get('transaction_type') == txn_type
                    and o.get('status') == 'COMPLETE'
                    and o.get('average_price', 0) > 0):
                if o.get('tag') != ORDER_TAG:
                    out['untagged'] += 1
                    continue
                ts = str(o.get('order_timestamp', ''))
                if best is None or ts > str(best.get('order_timestamp', '')):
                    best = o
        if best is not None:
            log(f"    Recovered fill: {symbol} {txn_type} @ "
                f"{best['average_price']} (order {best['order_id']}, "
                f"tag={ORDER_TAG})")
            out['price'] = best['average_price']
            return out
        if out['untagged']:
            log(f"    REFUSING to recover a fill for {symbol} {txn_type}: "
                f"{out['untagged']} COMPLETE order(s) match, none tagged "
                f"{ORDER_TAG}. Those are somebody else's fills — adopting one "
                f"would book a price this system never transacted.")
        else:
            log(f"    No {ORDER_TAG} fill found for {symbol} {txn_type} in "
                f"today's order book (kite.orders() covers today only).")
    except Exception as e:
        out['error'] = str(e)
        log(f"    Could not read the order book to recover a fill: {e}")
    return out


def _find_last_fill_price(kite: KiteConnect, symbol: str,
                          txn_type: str) -> Optional[float]:
    """`find_recoverable_fill`'s price, or None. **None is not zero.**

    The return type changed from `float` to `Optional[float]` on 2026-08-27.
    Returning 0.0 for "not found" is what let the already-flat close path book
    `exit_net = 0.0` — a full max loss computed from a price that was never
    observed, on a structure whose max loss is the debit. An unobserved price
    is UNKNOWN, and the project's own rule (CLAUDE.md, "Valuation bounds")
    is to CLAMP what is mathematically achievable and REFUSE what is only
    estimated. Nothing about an empty order book estimates zero.
    """
    return find_recoverable_fill(kite, symbol, txn_type)['price']


def _find_pending_orders(kite: KiteConnect, symbol: str, txn_type: str):
    """Find live orders for a symbol+side placed by this script.

    Used to detect orders that were placed but whose response was lost
    (network drop after place_order succeeded). Prevents duplicate orders.

    Returns a list, or **None meaning "could not tell"**. That distinction is
    the whole point: this guard exists for the network-flake window, and an
    `except` returning `[]` stood the guard down at exactly the moment it was
    needed — an unreadable order book was reported as "no live orders", and the
    caller placed on top of a possibly-live one. `None` makes the caller wait
    instead of guess.
    """
    pending = []
    try:
        for o in kite.orders():
            if (o.get('tradingsymbol') == symbol
                    and o.get('transaction_type') == txn_type
                    and o.get('tag') == ORDER_TAG
                    # TERMINAL-list, not a live-list. The old check named three
                    # statuses and missed every transient one Kite actually
                    # emits under load at 09:15 — 'PUT ORDER REQ RECEIVED',
                    # 'VALIDATION PENDING', 'OPEN PENDING', 'MODIFY PENDING',
                    # 'CANCEL PENDING'. An order sitting in any of those was
                    # invisible here, so the retry placed a SECOND order on the
                    # same leg and both could fill: the short leg bought twice
                    # and flipped long. That is the Feb-2026 ICICIBANK loss
                    # exactly, and enumerating the live states is what let it
                    # back in. Enumerate the DEAD states instead — a status
                    # this code has never heard of is now treated as live,
                    # which fails toward "do not place another order".
                    and str(o.get('status', '')).upper() not in _TERMINAL_ORDER_STATUS):
                pending.append(o)
    except Exception as e:
        log(f"    Could not check pending orders: {e} — treating as UNKNOWN, "
            f"refusing to place until the order book is readable")
        return None
    return pending


def round_to_tick(price: float) -> float:
    """Round price to nearest tick size."""
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)


#: The frozen-book LABEL for each `_store_type`. One table, because the two
#: vocabularies already exist and a third place inventing a third one is how
#: they drift. `_load_all_trades` stamps the keys; `frozen_books` uses the
#: values; `bcs/journal_report.py` reads the keys uppercased.
STORE_TYPE_LABEL = {'bcs': 'BCS', 'bps': 'BPS', 'fh': 'FH', 'zebra': 'COHORT'}
LABEL_STORE_TYPE = {v: k for k, v in STORE_TYPE_LABEL.items()}


def _order_ctx(trade: dict, reason: str, leg: str, strategy: str,
               book: Optional[str] = None) -> dict:
    """Why this order exists, for the journal.

    Deliberately only fields already on the record -- no re-derivation. A
    journal that computes anything can disagree with the system it is
    witnessing, and then it is evidence of nothing.

    **N5 - `book` is not `strategy`.** `strategy` is the DIRECTION this
    structure trades ('BCS' for a bull call spread, 'BPS' for a bear put), and
    the cohort store holds BOTH, so it can never name which of the four books
    a record came from. All four number their trades from 1, so `#1` on a
    journal line named four different positions and `journal_report` could
    only report the collision, never resolve it - in the one file that exists
    to say what this engine intended to trade.

    `book` is stamped even when it is None, and that is the point: a key
    missing from a jsonl line and a key holding null read the same to a human
    and differently to a reader. Null says WE LOOKED AND DID NOT KNOW, which
    is a fact about the code path, not about the trade.
    """
    return {
        'trade_id': trade.get('id'),
        'book': book or trade.get('_store_type'),
        'stock': trade.get('stock'),
        'strategy': strategy,
        'reason': reason,
        'leg': leg,
    }


def place_limit_order(kite: KiteConnect, exchange: str, symbol: str,
                      txn_type: str, qty: int, price: float,
                      dry_run: bool, context: dict = None) -> str:
    """Place a LIMIT NRML order. Returns order_id or 'DRY_RUN_xxx'.

    Every call is journalled to `logs/order_intents_<date>.jsonl`, dry run and
    live alike, INTENT first and RESULT after. That file is what makes
    "alert-only first" checkable: a dry-run session's intents can be diffed
    against what the paper system booked, and the two modes stay comparable
    because both write the same record. See bcs/order_journal.py — including
    why an intent with no result is the point rather than a gap.
    """
    price = round_to_tick(price)
    intent_id = order_journal.record_intent(
        symbol=symbol, txn_type=txn_type, qty=qty, price=price,
        exchange=exchange, dry_run=dry_run, context=context, log=log)

    if dry_run:
        fake_id = f"DRY_{now_ist().strftime('%H%M%S%f')}"
        log(f"    [DRY RUN] {txn_type} {symbol} x {qty} @ {price} -> {fake_id}")
        order_journal.record_result(intent_id, order_id=fake_id, dry_run=True,
                                    log=log)
        return fake_id

    try:
        order_id = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=txn_type,
            quantity=qty,
            product=PRODUCT,
            order_type=kite.ORDER_TYPE_LIMIT,
            price=price,
            tag=ORDER_TAG,
        )
    except Exception as e:
        # Stamp the failure and RE-RAISE. The caller's retry/abort logic is
        # unchanged -- this only ensures a rejected order leaves a record,
        # instead of the intent line dangling as if the process had died.
        order_journal.record_result(intent_id, error=f'{type(e).__name__}: {e}',
                                    log=log)
        raise
    log(f"    Order placed: {txn_type} {symbol} x {qty} @ {price} -> {order_id}")
    order_journal.record_result(intent_id, order_id=str(order_id), log=log)
    return str(order_id)


def booking_engine(trade: Optional[dict]) -> Optional[str]:
    """Which engine will actually BOOK this record's exit, or None for this one.

    A record that positively declares itself paper is booked by zebra's
    `_paper_auto_close`; this monitor only SHADOWS it during the dry-run
    evidence week. Naming the engine is the whole of the 2026-08-28 fix for
    "two messages, two numbers, one position": at 09:18 the monitor announced
    COFORGE #436's TP and at 09:25 zebra announced the same TP with the real
    +Rs 7,790, and neither message said which of the two was the booking. Both
    were correct; the owner still had to reconcile them by hand.

    Deliberately keyed on `_record_says_paper` rather than on `paper_mode`:
    the question is who owns THIS record, not what mode the process is in.
    """
    if trade and _record_says_paper(trade):
        return 'zebra (paper)'
    return None


def rehearsal_lines(trade: Optional[dict], dry_run: bool) -> list:
    """Say plainly that a dry-run alert is a rehearsal, and who books instead.

    Two situations wear the same words if nobody separates them, and they need
    opposite reactions from the reader:

    * **dry run over a PAPER record** — expected, routine, zebra owns the
      booking and carries the real P&L. Nothing is wrong.
    * **dry run over a REAL record** — also expected, but nobody books it: the
      position is still open at the broker and the only engine that could
      close it is disarmed. Worth knowing.

    Neither is the third thing, which stays alarming and lives in
    `_refuse_unpriced_close`: ARMED, and no fill could be found to price a
    close that really happened.

    Returns [] when not a dry run, so callers can splice unconditionally.
    """
    if not dry_run:
        return []
    engine = booking_engine(trade)
    if engine:
        return ["REHEARSAL — this is expected, not a malfunction. The monitor "
                "is in DRY RUN, so no order was placed and there is no fill "
                "to price an exit at. No P&L belongs in THIS message.",
                f"Booked by: {engine}. Its own alert for this position "
                f"carries the real exit price and P&L."]
    return ["⚠️ NO ORDER WAS PLACED. The monitor is in DRY RUN and this is a "
            "REAL record — the position is STILL OPEN at the broker and "
            "NOTHING ELSE BOOKS IT.",
            "ACTION NEEDED: close it by hand, or arm the monitor."]


def trigger_alert_text(label: str, stock: str, reason: str, spot,
                       dry_run: bool, trade: Optional[dict],
                       live_tail: str) -> str:
    """The Telegram sent the moment a trigger fires, BEFORE any order.

    ONE definition for the spread and FH paths, for the usual reason. In a dry
    run "Closing spread..." is a claim about the future that will not happen,
    and it is the FIRST of the two messages the owner has to reconcile against
    zebra's — so it names the shadow and the booking engine too.
    """
    lines = [f"{label} {reason} TRIGGERED", f"{stock} spot={spot}"]
    if dry_run:
        lines[0] = f"🧪 [DRY RUN] {lines[0]}"
        lines.extend(rehearsal_lines(trade, dry_run))
        lines.append("Walking the close so the intended orders are "
                     "journalled. Nothing will be placed.")
    else:
        lines.append(live_tail)
    return "\n".join(lines)


def close_alert_text(label: str, stock: str, reason: str, spot,
                     value_lines: list, total_pnl: float, dry_run: bool,
                     trade: Optional[dict] = None) -> str:
    """The Telegram body for a completed close. ONE definition, both books.

    **A dry run never carries a P&L figure.** `wait_for_fill`'s dry stub
    returns `average_price: 0.0` — correctly, since no order was placed and
    there is no fill to report — so every close computes `exit_net = 0` and
    lands on exactly minus-the-entry-debit. During the planned dry-run
    evidence week that is a stream of fabricated MAXIMUM LOSSES arriving next
    to zebra's real paper bookings of the same positions, including on the
    take-profit winners. A number that is wrong is worse than no number: the
    reader cannot tell it apart from the real ones, and the arming gate is
    read off exactly this evidence.

    The alert itself is NOT suppressed. "The monitor would have closed here,
    for this reason, at this spot" is the entire point of running the week —
    what is withheld is the one line that is not a measurement.

    Both close paths call this, because fixing the copy you happened to open
    is the most repeated bug in this codebase (`feedback_the_copy_you_did_not_open`).

    2026-08-28: withholding the number was right but not enough. On a phone,
    "P&L: not computed - no order was placed, so there is no fill to price the
    exit at" reads as a malfunction, and it fires on EVERY dry-run close
    because in dry run no order is EVER placed. The monitor's own log already
    said the reassuring thing ("armed, this close would be REFUSED … zebra
    still owns this trade's exits") and the alert did not. `trade` is passed so
    the alert can carry it too — see `rehearsal_lines`.
    """
    if dry_run:
        head = (f"🧪 [DRY RUN] {label} SHADOW CLOSE: {stock}"
                if booking_engine(trade)
                else f"🧪 [DRY RUN] {label} WOULD CLOSE: {stock}")
    else:
        head = f"{label} CLOSED: {stock}"
    lines = [head, f"Reason: {reason}"] + list(value_lines)
    if dry_run:
        lines.extend(rehearsal_lines(trade, dry_run))
    else:
        lines.append(f"P&L: Rs {total_pnl:+,.0f}")
    lines.append(f"Spot: {spot}")
    return "\n".join(lines)


def wait_for_fill(kite: KiteConnect, order_id: str, dry_run: bool) -> Optional[dict]:
    """Poll order status until COMPLETE, REJECTED, CANCELLED, or timeout.

    Returns the order dict with status info. For partial fills:
    - status will be 'COMPLETE' if fully filled
    - filled_quantity and average_price reflect what actually filled
    - Returns None on timeout (caller should cancel and check)
    """
    if dry_run:
        return {'status': 'COMPLETE', 'average_price': 0.0, 'order_id': order_id,
                'filled_quantity': 0}

    deadline = time.time() + ORDER_WAIT_SEC
    while time.time() < deadline:
        try:
            for o in kite.orders():
                if str(o['order_id']) == order_id:
                    if o['status'] == 'COMPLETE':
                        return o
                    if o['status'] == 'REJECTED':
                        log(f"    Order REJECTED: {o.get('status_message', '')}")
                        return o
                    if o['status'] == 'CANCELLED':
                        # Could be partially filled then cancelled
                        filled = o.get('filled_quantity', 0)
                        if filled > 0:
                            log(f"    Order CANCELLED with partial fill: {filled} qty @ {o.get('average_price', 0)}")
                        else:
                            log(f"    Order CANCELLED: {o.get('status_message', '')}")
                        return o
        except Exception as e:
            log(f"    Order status poll error: {e}")
        time.sleep(1)

    log(f"    Order {order_id} timed out after {ORDER_WAIT_SEC}s")
    return None


def cancel_order_safe(kite: KiteConnect, order_id: str, dry_run: bool):
    """Cancel an open order, swallowing errors."""
    if dry_run:
        return
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
        log(f"    Cancelled order {order_id}")
    except Exception as e:
        log(f"    Cancel failed for {order_id}: {e}")


def _order_final_state(kite: KiteConnect, order_id: str):
    """Read an order's state after a cancel, to catch a fill in the race.

    A cancel request and a fill can cross: the order completes at the exchange
    microseconds before the cancel lands, so treating "we cancelled it" as
    "it did not fill" would lose a real fill and leave the code closing a leg
    that is already closed. Returns the order dict, or None if it cannot be
    read — callers must treat None as "unknown", never as "did not fill".
    """
    try:
        time.sleep(1)
        for o in kite.orders():
            if str(o['order_id']) == str(order_id):
                return o
    except Exception as e:
        log(f"    Post-cancel state check failed for {order_id}: {e}")
    return None


def close_leg(kite: KiteConnect, exchange: str, symbol: str, txn_type: str,
              qty: int, is_buy: bool, dry_run: bool,
              urgent: bool = False, context: dict = None,
              attempts: int = None,
              allow_pay_through: bool = True) -> Optional[dict]:
    """
    Close one leg with retry + escalating slippage.

    is_buy=True  -> buying back short, price = ASK + slippage
    is_buy=False -> selling long,       price = BID - slippage

    urgent=False (SL_SPREAD/SL_TRAIL/TP): the depth wait also demands a
      RELIABLE book (leg_quote_reliable). If the book never becomes reliable
      the leg is NOT closed (returns None) — the caller aborts pre-fill and
      the trigger re-arms. Never lift a garbage ask for a valuation trigger.
    urgent=True (SL_SPOT/EXPIRY/second-leg escalation): waits at most
      URGENT_BOOK_WAITS cycles for reliability, then proceeds — attempts
      before the last are price-capped vs an external anchor (max of today's
      LTP / prev close), the FINAL attempt pays through uncapped with a loud
      Telegram. Must-exit beats overpay, but overpay is bounded first.

    Design principles (post 2026-02-18 + 2026-07-24 incidents):
      1. Track REMAINING qty — never retry with the original full qty
      2. Check for pending orders from this script before placing new ones
      3. Handle partial fills — reduce remaining, continue with rest
      4. Price guards anchored EXTERNALLY (LTP/prev_close), never to the
         quote being sanity-checked (the old 5x-ask check was self-referential
         and mathematically could not fire)

    M14 adds two knobs, both defaulting to today's behaviour so no existing
    caller changes:

    `attempts` — how many escalating orders this call may place (default
      MAX_RETRIES). The recovery sweep passes 1: the initial close already
      spent three escalating orders on this leg, and the design reads the
      owner's "try 3 times" as 3 CLOSE attempts, not 3x3 = 9 orders. Ceiling
      per leg per incident becomes 3 initial + 3 recovery = 6 placements, each
      position-verified and pending-order-checked first.

    `allow_pay_through` — whether the FINAL urgent attempt may lift an
      unreliable book uncapped (default True, which is what urgent has always
      meant). The sweep passes False for BOUNDED states: a naked long's
      downside is the premium already paid, and paying through a garbage book
      to exit it can cost more than holding it does. Uncapped pay-through is
      reserved for the naked-short class, where the exposure is unbounded and
      gap-shaped. With this False the anchor cap simply stays on for every
      attempt — the order rests as a working limit and often fills as the book
      forms, which is the same non-catastrophic outcome as not trying.
      5. Don't retry REJECTED orders (margin, price band — won't resolve)
      6. After cancel, always re-check for race-condition fills
      7. Re-check order-time cutoff EVERY attempt (a close that starts 15:19
         must not still be placing orders at 15:24 unless urgent)
    """
    NO_DEPTH_MAX_WAITS = 10     # Wait up to 10 × 3s = 30s for depth to appear
    NO_DEPTH_WAIT_SEC = 3

    remaining_qty = qty
    cumulative_fill_value = 0.0   # sum of (fill_price × filled_qty) across attempts
    cumulative_fill_qty = 0       # sum of filled qty across attempts

    # Clamped, not trusted: a caller passing 0 or a negative would place no
    # order and return None, which reads to `_close_spread_inner` as "the leg
    # was not tradeable" — a lie about the book. Capped at MAX_RETRIES so a
    # caller cannot widen the order ceiling this function exists to bound.
    max_attempts = MAX_RETRIES if attempts is None else max(
        1, min(int(attempts), MAX_RETRIES))

    for attempt in range(1, max_attempts + 1):
        # ── Order-time cutoff, re-checked EVERY attempt ───────────────
        cutoff = HARD_ORDER_CUTOFF_TIME if urgent else LAST_ORDER_TIME
        if now_ist().time() > cutoff:
            log(f"    ORDER CUTOFF: {now_ist().strftime('%H:%M:%S')} > {cutoff.strftime('%H:%M')} "
                f"({'urgent' if urgent else 'normal'}). No more orders for {symbol}.")
            send_telegram(f"Order cutoff reached closing {symbol} — "
                          f"{remaining_qty} qty NOT closed. Manual intervention needed!", alert_class=alert_policy.SAFETY)
            break
        # ── Safety: Re-check position to compute actual remaining ─────
        if not dry_run:
            current_qty = get_net_position(kite, symbol)
            if is_buy:
                # Buying back short: remaining = how much is still short
                actual_remaining = abs(min(current_qty, 0))
            else:
                # Selling long: remaining = how much is still long
                actual_remaining = max(current_qty, 0)

            if actual_remaining == 0:
                log(f"    Position {symbol} already flat (qty={current_qty}). Nothing to close.")
                # `average_price` may be None here, and callers must keep it
                # None rather than coercing it: it means "this leg went flat
                # and no order of ours priced it". Our OWN partial fills win
                # when there are any -- those are observed prices.
                fill_price = _find_last_fill_price(kite, symbol, txn_type)
                total_filled = cumulative_fill_qty if cumulative_fill_qty > 0 else qty
                if cumulative_fill_qty > 0:
                    fill_price = cumulative_fill_value / cumulative_fill_qty
                elif fill_price is None:
                    log(f"    WARNING: {symbol} is flat with no {ORDER_TAG} "
                        f"fill to price it. Reporting the leg as CLOSED with "
                        f"NO price — the caller must not invent one.")
                return {'status': 'COMPLETE', 'average_price': fill_price,
                        'order_id': 'position_verified', 'filled_quantity': total_filled}

            if actual_remaining < remaining_qty:
                log(f"    Position changed: {symbol} qty={current_qty}. Reducing order from {remaining_qty} to {actual_remaining}.")
                remaining_qty = actual_remaining

            # ── Idempotency: Check for pending orders from this script ──
            # Always check (not just retries) — a crashed previous run may
            # have left pending orders that recover_closing_trade didn't cancel.
            if not dry_run:
                pending = _find_pending_orders(kite, symbol, txn_type)
                if pending is None:
                    # Could not read the order book. Placing now risks doubling
                    # a live order on this leg, which is the ICICI failure.
                    # Skip to the next attempt instead — the retry loop already
                    # bounds how long this can go on, and the caller's ABORT /
                    # partial_close paths handle running out of attempts.
                    log(f"    Order book unreadable — skipping attempt {attempt} "
                        f"rather than risk a duplicate order on {symbol}.")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                if pending:
                    order_id = str(pending[0]['order_id'])
                    log(f"    Found pending order {order_id} from this script. Waiting for it...")
                    result = wait_for_fill(kite, order_id, dry_run=False)
                    if result and result['status'] == 'COMPLETE':
                        log(f"    Pending order FILLED at {result['average_price']}")
                        return result
                    # TIMEOUT MEANS THE ORDER IS STILL LIVE. `wait_for_fill`
                    # returns None on timeout WITHOUT cancelling — its own
                    # docstring says "caller should cancel and check" — and
                    # this caller did not, then fell through and placed a
                    # replacement. Two live orders on one leg, both fillable:
                    # the short leg bought twice, flipped long. The code
                    # already applies exactly this cancel-then-verify
                    # discipline to its OWN timed-out orders further down; the
                    # ADOPTED order was the one path that skipped it.
                    if result is None:
                        log(f"    Adopted order {order_id} timed out — cancelling "
                            f"before any replacement is placed.")
                        cancel_order_safe(kite, order_id, dry_run=False)
                        # Re-read it: a fill can land in the cancel race.
                        result = _order_final_state(kite, order_id)
                        if result and result.get('status') == 'COMPLETE':
                            log(f"    Adopted order filled during cancel at "
                                f"{result.get('average_price')}")
                            return result
                    filled = result.get('filled_quantity', 0) if result else 0
                    if filled > 0:
                        cumulative_fill_qty += filled
                        cumulative_fill_value += filled * result.get('average_price', 0)
                        remaining_qty = max(0, remaining_qty - filled)
                        log(f"    Partial fill: {filled} qty. Remaining: {remaining_qty}")
                        if remaining_qty == 0:
                            avg = cumulative_fill_value / cumulative_fill_qty if cumulative_fill_qty else 0
                            return {'status': 'COMPLETE', 'average_price': avg,
                                    'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

        # ── Wait for depth AND a reliable book ────────────────────────
        # Normal closes (valuation-triggered) NEVER trade an unreliable
        # book — they keep waiting and ultimately give up (caller re-arms).
        # Urgent closes wait at most URGENT_BOOK_WAITS cycles, then proceed
        # with anchor-capped pricing (must exit, but bounded overpay).
        depth = None
        book_reliable = False
        for wait_i in range(NO_DEPTH_MAX_WAITS):
            # fresh=True: this loop exists to WAIT for the book to re-form and
            # then price an order off it. A cached snapshot would neither
            # change between waits nor reflect what the order will meet.
            depth = get_option_depth(kite, exchange, symbol, fresh=True)
            has_side = (depth['ask'] > 0 and depth['ask_qty'] > 0) if is_buy \
                       else (depth['bid'] > 0 and depth['bid_qty'] > 0)
            book_reliable, why = leg_quote_reliable(depth)
            if has_side and book_reliable:
                break
            if has_side and urgent and wait_i >= URGENT_BOOK_WAITS:
                log(f"    URGENT: proceeding on unreliable book ({why}) after {wait_i} waits.")
                break
            side = "ask" if is_buy else "bid"
            reason = why if has_side else f"no {side} depth"
            log(f"    Book not tradeable for {symbol}: {reason} (wait {wait_i+1}/{NO_DEPTH_MAX_WAITS})...")
            time.sleep(NO_DEPTH_WAIT_SEC)
        else:
            log(f"    No tradeable book after {NO_DEPTH_MAX_WAITS * NO_DEPTH_WAIT_SEC}s "
                f"({'urgent' if urgent else 'normal'}). Skipping attempt {attempt}.")
            # Normal close with nothing filled: further attempts would just
            # repeat the same 30s wait — bail out now so the caller can abort
            # and re-arm instead of grinding ~90s of the poll loop.
            if not urgent and cumulative_fill_qty == 0:
                log(f"    Normal close: aborting leg early (nothing filled, book unreliable).")
                return None
            continue

        slippage = (SLIPPAGE_TICKS_BASE + SLIPPAGE_TICKS_INCREMENT * (attempt - 1)) * TICK_SIZE
        anchor = price_anchor(depth)
        final_urgent_attempt = (urgent and attempt == max_attempts
                                and allow_pay_through)

        if is_buy:
            price = depth['ask'] + slippage
            # ── Anchored buy cap: never validate the ask against itself ──
            # (2026-07-24: a lone 1.40 ask vs fair 0.65 passed the old
            # ask-relative check and was paid in full.) Applies ONLY to
            # UNRELIABLE books — a tight book's ask is market consensus and
            # capping it would strand an urgent gap exit off-market. A capped
            # limit below a garbage ask simply rests and works the order for
            # ORDER_WAIT_SEC — it often fills as the book forms.
            if anchor > 0 and not book_reliable and not final_urgent_attempt:
                cap = round_to_tick(anchor * BUY_CAP_ANCHOR_MULT + slippage)
                if price > cap:
                    log(f"    BUY CAP: ask+slip {round_to_tick(price)} > anchor {anchor} x {BUY_CAP_ANCHOR_MULT}. "
                        f"Working limit {cap} instead.")
                    price = cap
            elif final_urgent_attempt and not book_reliable and anchor > 0 and price > anchor * BUY_CAP_ANCHOR_MULT:
                log(f"    URGENT FINAL ATTEMPT: paying through ask {depth['ask']} (anchor {anchor}).")
                send_telegram(f"URGENT close paying through: BUY {symbol} at ask {depth['ask']} "
                              f"vs anchor {anchor} — exiting anyway.")
            log(f"  Attempt {attempt}/{max_attempts}: BUY {symbol} x {remaining_qty}")
            log(f"    Depth -> Ask: {depth['ask']} x {depth['ask_qty']} | Bid: {depth['bid']} x {depth['bid_qty']} "
                f"| LTP: {depth['ltp']} | PrevCl: {depth['prev_close']} | Reliable: {book_reliable}")
            log(f"    Limit price: {round_to_tick(price)}")
        else:
            price = depth['bid'] - slippage
            # ── Sell price floor: never sell for nothing ──
            if price < TICK_SIZE:
                log(f"    SELL PRICE FLOOR: {price:.2f} < {TICK_SIZE}. Setting to {TICK_SIZE}.")
                price = TICK_SIZE
            # ── Anchored sell floor: don't dump the leg into a garbage-low
            # lone bid (mirror of the buy-side incident mode). Unreliable
            # books only — a tight bid is market consensus. ──
            if anchor > 0 and not book_reliable and not final_urgent_attempt:
                floor_p = round_to_tick(max(TICK_SIZE, anchor / SELL_FLOOR_ANCHOR_DIV - slippage))
                if price < floor_p:
                    log(f"    SELL FLOOR: bid-slip {round_to_tick(price)} < anchor {anchor} / {SELL_FLOOR_ANCHOR_DIV}. "
                        f"Working limit {floor_p} instead.")
                    price = floor_p
            elif final_urgent_attempt and not book_reliable:
                log(f"    URGENT FINAL ATTEMPT: selling at bid {depth['bid']} (anchor {anchor}).")
            log(f"  Attempt {attempt}/{max_attempts}: SELL {symbol} x {remaining_qty}")
            log(f"    Depth -> Bid: {depth['bid']} x {depth['bid_qty']} | Ask: {depth['ask']} x {depth['ask_qty']} "
                f"| LTP: {depth['ltp']} | PrevCl: {depth['prev_close']} | Reliable: {book_reliable}")
            log(f"    Limit price: {round_to_tick(price)}")

        # The caller knows the trade and the trigger; this frame knows the
        # book that produced the price. Both belong on the record -- "would
        # have sold at 0.35" is only meaningful next to the bid it read.
        leg_ctx = dict(context or {})
        leg_ctx.update({
            'attempt': attempt,
            'urgent': urgent,
            'book_reliable': book_reliable,
            'bid': depth.get('bid'), 'bid_qty': depth.get('bid_qty'),
            'ask': depth.get('ask'), 'ask_qty': depth.get('ask_qty'),
            'ltp': depth.get('ltp'), 'prev_close': depth.get('prev_close'),
        })
        order_id = place_limit_order(kite, exchange, symbol, txn_type, remaining_qty, price, dry_run,
                                     context=leg_ctx)
        result = wait_for_fill(kite, order_id, dry_run)

        if result and result['status'] == 'COMPLETE':
            fill = result['average_price']
            filled_qty = result.get('filled_quantity', remaining_qty)
            cumulative_fill_qty += filled_qty
            cumulative_fill_value += filled_qty * fill
            log(f"    FILLED at {fill} | Qty: {filled_qty} | Order: {order_id}")
            avg = cumulative_fill_value / cumulative_fill_qty if cumulative_fill_qty else fill
            return {'status': 'COMPLETE', 'average_price': avg,
                    'order_id': order_id, 'filled_quantity': cumulative_fill_qty}

        # ── REJECTED: don't retry (margin, price band, frozen qty) ──
        # Return a typed status, NOT None: None means "nothing tradeable,
        # safe to abort and re-arm", but a rejection will repeat on every
        # re-attempt — the caller must LOCK the trade (partial_close), not
        # re-open it into an infinite order-placement loop.
        if result and result['status'] == 'REJECTED':
            msg = result.get('status_message', 'unknown')
            log(f"    ORDER REJECTED: {msg}. Will not retry (same error likely).")
            send_telegram(f"Order REJECTED: {txn_type} {symbol} x {remaining_qty} — {msg}", alert_class=alert_policy.SAFETY)
            if cumulative_fill_qty > 0:
                avg = cumulative_fill_value / cumulative_fill_qty
                log(f"    PARTIAL before rejection: {cumulative_fill_qty}/{qty} @ avg {avg:.2f}")
                return {'status': 'PARTIAL', 'average_price': avg,
                        'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}
            return {'status': 'REJECTED', 'average_price': 0.0,
                    'order_id': order_id, 'filled_quantity': 0}

        # ── Not filled / CANCELLED — check for partial fill then retry ──
        partial_qty = 0
        if result and result.get('filled_quantity', 0) > 0:
            # Partial fill on cancelled order
            partial_qty = result['filled_quantity']
            partial_price = result.get('average_price', 0)
            cumulative_fill_qty += partial_qty
            cumulative_fill_value += partial_qty * partial_price
            remaining_qty = max(0, remaining_qty - partial_qty)
            log(f"    Partial fill: {partial_qty} @ {partial_price}. Remaining: {remaining_qty}")
            if remaining_qty == 0:
                avg = cumulative_fill_value / cumulative_fill_qty
                return {'status': 'COMPLETE', 'average_price': avg,
                        'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

        # ── Timeout — cancel and check for race-condition fill ──
        if result is None:
            cancel_order_safe(kite, order_id, dry_run)
            if not dry_run:
                time.sleep(1)
                try:
                    for o in kite.orders():
                        if str(o['order_id']) == order_id:
                            if o['status'] == 'COMPLETE':
                                log(f"    Order {order_id} filled after cancel! Fill: {o['average_price']}")
                                fill = o['average_price']
                                filled_qty = o.get('filled_quantity', remaining_qty)
                                cumulative_fill_qty += filled_qty
                                cumulative_fill_value += filled_qty * fill
                                avg = cumulative_fill_value / cumulative_fill_qty
                                return {'status': 'COMPLETE', 'average_price': avg,
                                        'order_id': order_id, 'filled_quantity': cumulative_fill_qty}
                            # Check for partial fill on the cancelled order
                            filled_qty = o.get('filled_quantity', 0)
                            if filled_qty > 0:
                                cumulative_fill_qty += filled_qty
                                cumulative_fill_value += filled_qty * o.get('average_price', 0)
                                remaining_qty = max(0, remaining_qty - filled_qty)
                                log(f"    Post-cancel partial: {filled_qty} filled. Remaining: {remaining_qty}")
                                if remaining_qty == 0:
                                    avg = cumulative_fill_value / cumulative_fill_qty
                                    return {'status': 'COMPLETE', 'average_price': avg,
                                            'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}
                            break
                except Exception as e:
                    log(f"    Post-cancel check failed: {e}")

        if attempt < max_attempts:
            log(f"    Retrying with +{SLIPPAGE_TICKS_INCREMENT} ticks slippage... (remaining: {remaining_qty})")
            time.sleep(1)

    # All retries exhausted
    if cumulative_fill_qty > 0:
        avg = cumulative_fill_value / cumulative_fill_qty
        log(f"    PARTIAL CLOSE: {cumulative_fill_qty}/{qty} filled @ avg {avg:.2f}. {remaining_qty} remaining!")
        send_telegram(f"PARTIAL CLOSE: {symbol} {cumulative_fill_qty}/{qty} filled. {remaining_qty} remaining!", alert_class=alert_policy.SAFETY)
        return {'status': 'PARTIAL', 'average_price': avg,
                'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

    log(f"    FAILED to close {symbol} after {max_attempts} attempt(s)!")
    return None


# ── Refusing to price a close, instead of inventing one ─────────────────────

#: (strategy-label, trade-id) -> date string already escalated. In memory, like
#: `expiry_trades`: losing it on restart costs one repeated Telegram, which is
#: the correct direction for an escalation that is still unresolved.
_unpriced_close_alerted: dict = {}


# ── M14 · frozen-close recovery: the DECISION layer ─────────────────────────
#
# Classification and gating only. Nothing here places an order, reads a book or
# touches a store; it answers "what is this record, and may anything be done
# about it right now". The order-placing orchestrator is a separate, thinner
# piece that does what these functions say — which is the point of the split:
# the reasoning is pure and exhaustively testable, and the part that can lose
# money has almost no reasoning left in it.

#: A bounded position can wait: its max loss is already fixed, so the grace
#: window buys free optionality on a human taking a BETTER action than the
#: machine would. A naked short cannot: the exposure is unbounded and
#: gap-shaped, and there is no better human action to wait for — a human would
#: buy it back, which is exactly what the machine will do. Waiting buys nothing
#: and can cost a lakh. Telegram fires simultaneously either way, so the human
#: can still intervene between attempts.
RECOVERY_GRACE_SEC = 300
NAKED_SHORT_GRACE_SEC = 0

#: Per INCIDENT, not per day. A freeze at 15:10 with one attempt spent does NOT
#: get three fresh attempts tomorrow. Deliberately unlike the time stop, whose
#: attempts reset each session — a time stop is a daily deadline, a frozen
#: close is one incident.
RECOVERY_MAX_ATTEMPTS = 3
RECOVERY_WAIT_BOUNDED_SEC = 180
RECOVERY_WAIT_NAKED_SEC = 60

#: Classes, in the order the sweep cares about them.
RC_NAKED_SHORT = 'naked_short'   # unbounded. zero grace, urgent, pay through
RC_BOUNDED = 'bounded'           # spread intact or naked long. grace, capped
RC_FLAT = 'flat'                 # nothing live. book it, place nothing
RC_NO_ORDERS = 'no_orders'       # flipped / rejected / unknown. escalate only

#: Terminal for the incident: no further orders, ever, for this freeze. Not
#: re-armed by a restart, a new day, or a config reload.
RECOVERY_TERMINAL_STATES = ('escalated', 'resolved')


def _legs_of(trade):
    """(symbol, sign) for every leg a record declares, shorts as -1.

    One reader for both books so the classifier does not need to know which
    kind of trade it holds. FH is included even though nothing will order on
    it — classification is what produces the alert, and an FH freeze that
    cannot be described is exactly the invisible position M14 is about.
    """
    pairs = [('short_symbol', -1), ('long_symbol', +1),
             ('short_call_symbol', -1), ('long_call_symbol', +1),
             ('short_put_symbol', -1), ('long_put_symbol', +1)]
    return [(trade[k], sign) for k, sign in pairs if trade.get(k)]


def classify_frozen(trade, net_by_symbol):
    """What IS this frozen record, judged from LIVE broker positions.

    **Never from the freeze-time snapshot.** The human may have fixed it, and
    self-resolution on a now-flat book — booking with zero orders — is the
    outcome this whole mechanism most wants. A classifier reading its own
    freeze record would never see that.

    `net_by_symbol` comes from ONE `kite.positions()` per sweep pass, shared
    across every record. Per-leg calls here would multiply broker requests by
    the size of the frozen book, which is the rate-limit lesson the monitor
    already learned the expensive way.

    Returns `(class, detail)`. The classes are deliberately coarse — the sweep
    branches on RISK, not on the sixteen states of the design's table, because
    what an action must obey is the risk class and nothing else.
    """
    cf = trade.get('close_failure') or {}

    # A record frozen before this shipped has no `close_failure`. It is
    # alert-and-nag, never automated: retrofitting recovery onto a freeze
    # nobody diagnosed is how helpfulness loses money.
    if not cf:
        return RC_NO_ORDERS, 'legacy freeze, no close_failure record'

    cause = cf.get('cause')
    if cause == 'flipped':
        # The Feb-2026 amplifier. The book is not what the record says, so
        # every quantity an order would be sized from is untrusted.
        return RC_NO_ORDERS, 'FLIPPED — the book contradicts the record'
    if cause == 'rejected':
        # The broker's reason (margin, price band, frozen scrip) will repeat.
        return RC_NO_ORDERS, 'the broker REJECTED the close; it will reject again'
    if cause not in CLOSE_FAILURE_CAUSES:
        return RC_NO_ORDERS, 'unrecognised cause %r' % (cause,)

    state = cf.get('state')
    if state in RECOVERY_TERMINAL_STATES:
        return RC_NO_ORDERS, 'incident already %s' % state
    if state not in ('frozen', 'recovering'):
        # Polarity, in the direction that matters for PERMISSION TO ORDER: an
        # unknown must count as no. (For exit REASONS the opposite holds — an
        # unknown must not count as a stop. Same lesson, opposite direction,
        # and getting it backwards here places orders.)
        return RC_NO_ORDERS, 'unrecognised recovery state %r' % (state,)

    legs = _legs_of(trade)
    if not legs:
        return RC_NO_ORDERS, 'record declares no legs'

    live = {sym: net_by_symbol.get(sym, 0) for sym, _ in legs}
    if all(q == 0 for q in live.values()):
        return RC_FLAT, 'every leg is flat at the broker'

    # A leg live in the OPPOSITE direction to the one the record claims is the
    # flipped shape reached by a different door — the freeze cause said
    # something else, but the book says the record is wrong about its own
    # position. Trust the book and place nothing.
    for sym, sign in legs:
        q = live.get(sym, 0)
        if q and (q > 0) != (sign > 0):
            return RC_NO_ORDERS, ('%s is %+d, opposite the %s the record '
                                  'claims' % (sym, q,
                                              'long' if sign > 0 else 'short'))

    shorts_live = sum(1 for sym, sign in legs if sign < 0 and live.get(sym, 0))
    longs_live = sum(1 for sym, sign in legs if sign > 0 and live.get(sym, 0))
    if shorts_live and not longs_live:
        return RC_NAKED_SHORT, 'short leg live with no long against it'
    if shorts_live:
        # Short and long both live: the spread is intact, or over-hedged after
        # a partial buyback. Either way max loss is already fixed.
        return RC_BOUNDED, 'short and long both live — hedged'
    return RC_BOUNDED, 'long leg only — bounded by the premium already paid'


def recovery_grace_sec(rclass):
    return (NAKED_SHORT_GRACE_SEC if rclass == RC_NAKED_SHORT
            else RECOVERY_GRACE_SEC)


def recovery_wait_sec(rclass):
    return (RECOVERY_WAIT_NAKED_SEC if rclass == RC_NAKED_SHORT
            else RECOVERY_WAIT_BOUNDED_SEC)


def recovery_gate(trade, rclass, *, now=None, now_time=None,
                  market_settled=True):
    """May the sweep place an order for this record RIGHT NOW?

    Returns `(ok, why)`. `why` is always populated — a refusal nobody can read
    is a position nobody looks at, which is the failure one level up.

    Every gate here fails CLOSED. The order of the checks is not cosmetic: the
    ones that can never become true (class, terminal state, attempts spent) are
    asked before the ones that are merely not-yet-true (grace, backoff,
    cutoff), so an escalated incident never reports itself as "waiting".
    """
    cf = trade.get('close_failure') or {}
    now = time.time() if now is None else now
    now_time = now_ist().time() if now_time is None else now_time

    if rclass in (RC_NO_ORDERS, RC_FLAT):
        return False, 'class %s never orders' % rclass
    if cf.get('state') in RECOVERY_TERMINAL_STATES:
        return False, 'incident is %s' % cf.get('state')

    attempts = int(cf.get('attempts') or 0)
    if attempts >= RECOVERY_MAX_ATTEMPTS:
        return False, 'all %d attempts spent' % RECOVERY_MAX_ATTEMPTS

    # The kill switch forces dry-run for every order path; recovery is not an
    # exception to it. Checked here rather than at the order so a tripped
    # switch reads as a refusal with a reason, not as a silent no-op.
    if not trading_enabled():
        return False, 'kill switch: trading disabled'

    frozen_at = cf.get('frozen_at')
    grace = recovery_grace_sec(rclass)
    if grace and frozen_at:
        try:
            age = now - ist_epoch(frozen_at)
        except (TypeError, ValueError):
            # An unparseable stamp cannot prove the grace window has passed.
            return False, 'unreadable frozen_at %r' % (frozen_at,)
        if age < grace:
            return False, 'grace: %ds of %ds elapsed' % (age, grace)

    nxt = cf.get('next_attempt_after')
    if nxt and now < float(nxt):
        return False, 'backing off for %ds' % (float(nxt) - now)

    # Both real-money incidents were opening prints, so recovery orders stay
    # dark for the same first 15 minutes as the value triggers.
    if not market_settled:
        return False, 'market not settled — recovery orders are dark at the open'

    cutoff = (HARD_ORDER_CUTOFF_TIME if rclass == RC_NAKED_SHORT
              else LAST_ORDER_TIME)
    if now_time > cutoff:
        return False, 'past %s cutoff for class %s' % (
            cutoff.strftime('%H:%M'), rclass)

    return True, 'attempt %d of %d, class %s' % (
        attempts + 1, RECOVERY_MAX_ATTEMPTS, rclass)


def _refreeze_after_recovery(store, trade, label, cf):
    """Put a record the recovery close did NOT book back to `partial_close`.

    `_refuse_unpriced_close` deliberately leaves the record at `closing` — on
    the NORMAL path that is right, because `closing` sits inside
    `OPEN_STATUSES` and the crash-recovery sweep at monitor start will restore
    it to `open` for re-monitoring.

    On the RECOVERY path that same behaviour LOSES THE INCIDENT: the record
    came from `partial_close`, and `recover_closing_trade` would flip it to
    `open` at the next restart, dropping it out of `get_frozen_trades()` while
    keeping a stale `close_failure` nobody reads. The frozen position would go
    back to looking like a healthy open one — the exact invisibility M14 exists
    to end, manufactured by M14.

    So the sweep returns what it borrowed. `set_trade_status` (not
    `recover_closing_trade`, whose Telegram would announce a recovery that did
    not happen).
    """
    # Read the status back through `get_frozen_trades`'s sibling rather than a
    # method only some books have: every store exposes the frozen list, so
    # "not frozen and not booked" is decidable without a new store verb.
    try:
        still_closing = any(
            t.get('id') == trade['id'] and t.get('status') == 'closing'
            for t in store.get_closing_trades())
        if still_closing:
            store.set_trade_status(trade['id'], 'partial_close',
                                   close_failure=cf)
            log('  #%s %s: recovery could not book it; returned to '
                'partial_close so the sweep keeps seeing it.'
                % (trade.get('id'), label))
    except Exception as e:                     # pragma: no cover - store-level
        log('  Could not re-freeze #%s after recovery: %s'
            % (trade.get('id'), e))


def _recovery_event(name, **kv):
    """One machine-parseable line per recovery decision, for the digest.

    `EVENT <name> k=v ...` on its own line. The digest counts BY NAME, never by
    warning tally, so a recovery that ran and a recovery that was refused are
    different facts rather than two entries in a blob of WARNING text.
    """
    log('EVENT %s %s' % (name, ' '.join('%s=%s' % (k, v)
                                        for k, v in sorted(kv.items()))))


def _persist_recovery(store, trade, cf):
    """Write `close_failure` back. Never touches `status`.

    Status moves only through `begin_recovery` / `set_trade_status`, which is
    what keeps the concurrency lock meaningful; zebra's `update_trade_fields`
    refuses status writes outright and this respects that on every book.
    """
    trade['close_failure'] = cf
    try:
        store.update_trade_fields(trade['id'], close_failure=cf)
    except Exception as e:                     # pragma: no cover - store-level
        log('  Could not persist close_failure for #%s: %s' % (trade.get('id'), e))


def _escalate_recovery(store, trade, label, cf, why, nagged):
    """Terminal for the incident: alerting continues, ordering never resumes.

    No restart, no new day and no config reload re-arms it. Clearance is
    explicit and human. The alert fires ONCE at escalation and then at most
    once a day, keyed `(label, id, cause, date)` — a safety alert nobody can
    silence is right, but one that repeats every five seconds trains the reader
    to swipe it away, which is the same thing as silencing it.
    """
    first = cf.get('state') != 'escalated'
    if first:
        cf['state'] = 'escalated'
        cf['next_attempt_after'] = None
        _persist_recovery(store, trade, cf)
        _recovery_event('recovery_exhausted', store=label, id=trade['id'],
                        cause=cf.get('cause'))
    key = (label, trade['id'], cf.get('cause'), today_ist().isoformat())
    if key in nagged:
        return
    nagged.add(key)
    send_telegram(
        "\U0001F6A8 %s #%s %s: FROZEN CLOSE NEEDS A HUMAN\n"
        "%s\n"
        "Automatic recovery will place NO further orders for this incident.\n"
        "Close it by hand in Kite, then clear it with "
        "`--book-frozen %s:%s` or `--reopen-frozen %s:%s`."
        % (label, trade['id'], trade.get('stock', '?'), why,
           label.lower(), trade['id'], label.lower(), trade['id']),
        alert_class=alert_policy.SAFETY)


def recover_frozen_positions(kite, books, dry_run, *, nagged=None,
                             now=None, market_settled=None):
    """M14 - finish, or escalate, closes that froze half-done.

    **The gap this closes.** A `partial_close` record drops out of
    `get_open_trades()`. Nothing retried it, nothing re-monitored it, nothing
    re-alerted: one Telegram at freeze time was the entire lifecycle of a
    position that may be live at the broker with its stops dead. That is the
    unwatched-position failure that has cost this account real money twice.

    **The recovery routine IS `_close_spread_inner`.** It is already
    position-driven, reduce-only, skip-flat-legs, flip-guarded, already-flat
    booking and unpriced-refusing. A second "recovery close" would be the
    copy-nobody-opens shape that produced D2. This function decides WHETHER and
    with what urgency; the close itself is the same code that has been reviewed
    into shape all year.

    **Fallen Hero is ALERT-ONLY (owner, 2026-08-28).** FH is traded by hand and
    the monitor places no FH orders at all, so its frozen records are
    classified and nagged and never ordered on. That is a deliberate departure
    from the design's S13/S14, which were written before that decision.
    `books` carries the flag per book rather than inferring it from the record,
    so the rule is stated where it is decided.

    `books` is `[(label, store, orders_allowed)]`.

    Skipped on this path, deliberately: the exit vet (a safety action past its
    window), the re-verify (meaningless on a half-closed position and dangerous
    - it could "abort" a naked long back to open), and the trigger debounce.
    Kept: the paper guard, the flip guard, both cutoffs and the kill switch.
    """
    nagged = _RECOVERY_NAGGED if nagged is None else nagged
    if market_settled is None:
        market_settled = is_market_settled()

    frozen = []
    for label, store, orders_allowed in books:
        if store is None:
            continue
        try:
            for t in store.get_frozen_trades():
                frozen.append((label, store, orders_allowed, t))
        except Exception as e:                 # pragma: no cover - store-level
            log('  Could not read frozen trades from %s: %s' % (label, e))
    if not frozen:
        return 0

    # ONE positions read for the whole sweep, shared. Per-leg calls would
    # multiply broker requests by the size of the frozen book - the same F7
    # lesson the poll loop already learned expensively.
    try:
        net_by_symbol = {p['tradingsymbol']: p['quantity']
                         for p in kite.positions()['net']}
    except Exception as e:
        log('  Recovery sweep: positions() failed, nothing classified: %s' % e)
        _recovery_event('recovery_blind', reason=str(e)[:60].replace(' ', '_'))
        return 0

    acted = 0
    for label, store, orders_allowed, trade in frozen:
        try:
            acted += _recover_one(kite, label, store, orders_allowed, trade,
                                  net_by_symbol, dry_run, nagged,
                                  now, market_settled)
        except Exception as e:
            # Per-record isolation, the same lesson the poll loop learned: one
            # malformed frozen record must not stop the sweep for every other.
            log('  Recovery sweep: #%s in %s raised: %s'
                % (trade.get('id'), label, e))
    return acted


def _recover_one(kite, label, store, orders_allowed, trade, net_by_symbol,
                 dry_run, nagged, now, market_settled):
    """One frozen record. Returns 1 if an action was taken, else 0."""
    tid = trade.get('id')
    cf = dict(trade.get('close_failure') or {})

    # ── A PAPER RECORD NEVER REACHES THE ORDER PATH ────────────────────────
    #
    # The sweep is the SECOND route into `_close_spread_inner`; `close_spread`
    # was the first and only one, and its guard cannot cover a caller that does
    # not go through it. This is not that check repeated — it is the same check
    # at a different door, and the door is what makes it observable: remove it
    # and a paper cohort record gets real orders aimed at legs that do not
    # exist at the broker, which is precisely the mistake the arming order
    # exists to prevent.
    #
    # Placed before classification so a paper record is not even described in
    # terms of an action. It is not an error and earns no alert: paper freezes
    # are ordinary, and zebra owns them.
    if _record_says_paper(trade):
        _recovery_event('frozen_paper_skipped', store=label, id=tid)
        return 0

    rclass, why = classify_frozen(trade, net_by_symbol)
    _recovery_event('frozen_seen', store=label, id=tid, cls=rclass)

    # A record already booked or reopened by hand needs nothing.
    if rclass == RC_FLAT:
        if not orders_allowed:
            # FH: the human books it. Saying "resolved" here would claim a
            # booking this path never made.
            _escalate_recovery(store, trade, label, cf,
                               'every leg is flat at the broker - book the '
                               'exit by hand', nagged)
            return 0
        return _finish_flat(kite, label, store, trade, cf, dry_run, nagged)

    if rclass == RC_NO_ORDERS:
        _escalate_recovery(store, trade, label, cf, why, nagged)
        return 0

    if not orders_allowed:
        _escalate_recovery(
            store, trade, label, cf,
            '%s - %s. This book is traded by hand; no automatic order will be '
            'placed.' % (rclass, why), nagged)
        return 0

    ok, gate_why = recovery_gate(trade, rclass, now=now,
                                 market_settled=market_settled)
    if not ok:
        # A spent-attempt refusal is terminal; every other refusal is "not
        # yet" and must not escalate, or a grace window would end the incident.
        if 'attempts spent' in gate_why:
            _escalate_recovery(store, trade, label, cf,
                               '%d recovery attempts, none finished the close'
                               % RECOVERY_MAX_ATTEMPTS, nagged)
        else:
            _recovery_event('recovery_waiting', store=label, id=tid,
                            why=gate_why.split(':')[0].replace(' ', '_'))
        return 0

    # ── THE ATTEMPT ────────────────────────────────────────────────────────
    # Persisted BEFORE the order, deliberately stricter than the time stop
    # (which persists after failure): the risk being bought off is a restart
    # silently resetting the counter and re-firing. A crash mid-attempt costs
    # one attempt and never gains one - the order journal's
    # intent-before-broker-call doctrine, applied to retries.
    urgent = rclass == RC_NAKED_SHORT
    cf['attempts'] = int(cf.get('attempts') or 0) + 1
    cf['state'] = 'recovering'
    cf['next_attempt_after'] = (time.time() if now is None else now) \
        + recovery_wait_sec(rclass)
    _persist_recovery(store, trade, cf)
    _recovery_event('recovery_attempt', store=label, id=tid,
                    n='%d/%d' % (cf['attempts'], RECOVERY_MAX_ATTEMPTS),
                    cls=rclass)
    send_telegram(
        "%s #%s %s: recovering a frozen close (attempt %d/%d, %s).\n%s"
        % (label, tid, trade.get('stock', '?'), cf['attempts'],
           RECOVERY_MAX_ATTEMPTS, rclass, why),
        alert_class=alert_policy.SAFETY)

    if not store.begin_recovery(tid, cf.get('reason') or 'RECOVERY'):
        # Somebody else moved it. Not a failure of ours and not an attempt we
        # should have spent - but it IS spent, and that is the safe direction.
        log('  #%s %s: begin_recovery refused; another process has it.'
            % (tid, label))
        return 0

    result = _close_spread_inner(
        kite, store, trade, spot=None, reason=cf.get('reason') or 'RECOVERY',
        dry_run=dry_run, label=label, recovery=True,
        recovery_urgent=urgent,
        # N5. `_store_type` is stamped by `_load_all_trades`, and a frozen
        # record never went through it - the sweep reads the stores directly,
        # which is the whole point of it. Without this the ONLY orders with no
        # book on their journal line would be the RECOVERY orders: the ones
        # placed on a position nobody was watching, which is exactly when an
        # incident report needs to name it.
        #
        # Passed, never stamped onto the record. `get_frozen_trades` hands out
        # the store's LIVE dict on every book, so writing a monitor-internal
        # tag into it would persist `_store_type` into the trade file on the
        # next save — the pollution `_load_all_trades` takes shallow copies to
        # avoid. And it is passed rather than GUESSED from `label`: three of
        # the four labels collide with strategy names, so a fallback would
        # confidently file a cohort bull-call under 'bcs'.
        book=LABEL_STORE_TYPE.get(label))

    if result is True:
        cf['state'] = 'resolved'
        _persist_recovery(store, trade, cf)
        _recovery_event('recovery_resolved', store=label, id=tid, via='orders',
                        attempts=cf['attempts'])
        send_telegram("\u2705 %s #%s %s: frozen close FINISHED by recovery "
                      "(attempt %d)." % (label, tid, trade.get('stock', '?'),
                                         cf['attempts']),
                      alert_class=alert_policy.SAFETY)
        return 1

    # Anything else re-froze the record inside `_close_spread_inner`, which
    # rewrote `close_failure` with a FRESH state - so re-read and carry the
    # attempt count forward, or the next pass gets a free attempt.
    fresh = dict(trade.get('close_failure') or {})
    fresh['attempts'] = cf['attempts']
    fresh['next_attempt_after'] = cf['next_attempt_after']
    fresh['state'] = 'recovering'
    _persist_recovery(store, trade, fresh)
    # `_close_spread_inner` re-freezes most failures itself, but the unpriced
    # refusal parks the record at `closing` — see `_refreeze_after_recovery`.
    _refreeze_after_recovery(store, trade, label, fresh)
    if fresh.get('cause') == 'rejected':
        _escalate_recovery(store, trade, label, fresh,
                           'the broker REJECTED the recovery order; the same '
                           'reason will reject the next one', nagged)
    elif fresh['attempts'] >= RECOVERY_MAX_ATTEMPTS:
        _escalate_recovery(store, trade, label, fresh,
                           '%d recovery attempts, none finished the close'
                           % RECOVERY_MAX_ATTEMPTS, nagged)
    return 1


def _finish_flat(kite, label, store, trade, cf, dry_run, nagged):
    """Every leg flat: book it from OUR OWN tagged fills, or refuse.

    Zero orders by construction - `_close_spread_inner` skips flat legs - so
    this is the self-resolution the grace window exists to allow: a human fixed
    it at the broker and the sweep simply records what happened.

    It can still refuse: with no ORDER_TAG fill to price a leg, the inner close
    routes to `_refuse_unpriced_close` and books nothing rather than inventing
    a price. That is the right answer and it leaves the record frozen for the
    CLI to book with operator prices.
    """
    tid = trade['id']
    if not store.begin_recovery(tid, cf.get('reason') or 'RECOVERY'):
        return 0
    result = _close_spread_inner(
        kite, store, trade, spot=None, reason=cf.get('reason') or 'RECOVERY',
        dry_run=dry_run, label=label, recovery=True, recovery_urgent=False)
    if result is True:
        cf['state'] = 'resolved'
        _persist_recovery(store, trade, cf)
        _recovery_event('recovery_resolved', store=label, id=tid,
                        via='human_flat', attempts=cf.get('attempts', 0))
        send_telegram("\u2705 %s #%s %s: the book went flat and the exit is "
                      "booked. No recovery order was placed."
                      % (label, tid, trade.get('stock', '?')),
                      alert_class=alert_policy.SAFETY)
        return 1
    # ESCALATE, do not retry. The legs are flat, so no order exists that could
    # ever price this: the only thing that can resolve it is an operator typing
    # what they filled at (`--book-frozen`). Left merely re-frozen, the sweep
    # re-classified it as flat on the NEXT POLL and refused again — an
    # `unpriced_refusal` event and a SAFETY Telegram every five seconds for the
    # rest of the session, roughly four thousand of them. An alert that repeats
    # that way is not louder than one that fires once; it is quieter, because
    # the reader learns to swipe it away.
    _recovery_event('unpriced_refusal', store=label, id=tid)
    _refreeze_after_recovery(store, trade, label, cf)
    _escalate_recovery(store, trade, label, cf,
                       'every leg is flat but no fill this system placed can '
                       'price the exit — book it by hand with the real price',
                       nagged)
    return 0


#: Daily-nag dedup, keyed `(label, id, cause, date)`. Module-level because the
#: monitor is one long-lived process per session and the sweep runs every poll.
_RECOVERY_NAGGED = set()


def _record_says_paper(trade: dict) -> bool:
    """Did this record POSITIVELY declare that no broker ever saw it?

    Deliberately the OPPOSITE default to `zebra.trade_store.is_paper_record`,
    and the asymmetry is the point rather than an oversight:

      * There, absence means paper. That predicate answers "may the money
        engine own this?" for records in `zebra_trades.json`, where every
        writer stamps the flag; refusing means zebra keeps booking it, so the
        record still has an engine and the conservative answer is safe.
      * Here, absence means REAL. This runs on all four books, and the
        bcs / bear_put / fallen_hero stores hold nothing but real positions
        and have never carried a `paper` key. Defaulting those to paper would
        refuse to close live spreads on three books at once — a guard that
        abandons the stops on real money is not a safer guard.

    So each side defaults toward the engine that will actually act on the
    record. A future refactor that "unifies" them breaks one or the other;
    `test_the_two_paper_predicates_default_in_opposite_directions` fails if
    someone tries.
    """
    return trade.get('paper') is True


def _refuse_unpriced_close(trade, label, reason, spot, dry_run, legs):
    """A close that CANNOT be priced is not booked. Escalate and stop.

    `legs` is a list of `(side, symbol, txn_type, recovery)` where recovery is
    a `find_recoverable_fill` result. Returns False — "manual intervention
    alerted", which is exactly what this is.

    The record is left wherever the caller found it (`closing`, having taken
    the lock). That is deliberate: it stays inside `OPEN_STATUSES` so capital
    and scanner dedup keep counting it, `get_closing_trades` surfaces it, and
    the crash-recovery sweep un-freezes it at the next process start. Booking
    it would be the only way to lose it silently.
    """
    lines = []
    for side, symbol, txn, rec in legs:
        if rec['price'] is not None:
            lines.append(f"{side} {symbol}: {rec['price']}")
        elif rec.get('error'):
            lines.append(f"{side} {symbol}: order book unreadable "
                         f"({str(rec['error'])[:60]})")
        elif rec['untagged']:
            lines.append(f"{side} {symbol}: {rec['untagged']} COMPLETE "
                         f"{txn} order(s) found, NONE tagged {ORDER_TAG} — "
                         f"not ours, refused")
        else:
            lines.append(f"{side} {symbol}: no {ORDER_TAG} {txn} fill today")
    detail = '\n'.join(lines)

    log("")
    log("  *** CLOSE NOT PRICED — REFUSING TO BOOK ***")
    log(f"  {reason}: both legs are flat but no fill this system placed can "
        f"price the exit.")
    for ln in lines:
        log(f"    {ln}")
    log(f"  Trade #{trade.get('id')} left at 'closing'. It is NOT booked and "
        f"NOT lost — close it by hand with the real exit price.")

    key = (label, trade.get('id'))
    today = now_ist().strftime('%Y-%m-%d')
    if _unpriced_close_alerted.get(key) != today and not dry_run:
        _unpriced_close_alerted[key] = today
        send_telegram(
            f"⚠️ {label} {trade.get('stock')}: {reason} triggered, both legs "
            f"already flat, and the exit CANNOT BE PRICED.\n"
            f"{detail}\n"
            f"Nothing was booked — booking a guess here would record a "
            f"max loss that never happened.\n"
            # Said in as many words because the dry-run rehearsal alert now
            # ALSO reports "no fill". These are the two situations that wear
            # the same words and need opposite reactions: a rehearsal is
            # routine, this is not. The alert has to settle which it is before
            # the reader has to guess.
            f"THIS IS NOT A DRY-RUN REHEARSAL — the monitor is ARMED and a "
            f"real close could not be priced. ACTION NEEDED.\n"
            f"Trade #{trade.get('id')} is parked at 'closing'. Close it by "
            f"hand with the real exit price.", alert_class=alert_policy.SAFETY)
    return False


URGENT_CLOSE_REASONS = ('SL_SPOT', 'EXPIRY_FORCE_CLOSE')


def close_spread(kite: KiteConnect, trade: dict, spot: float,
                 reason: str, dry_run: bool, store=None,
                 strategy_label: str = 'BCS',
                 reverify_sl: Optional[float] = None,
                 quote: Optional[dict] = None):
    """
    Close the full spread. Short FIRST, then long (margin rules).
    Works for both BCS and BPS (same 2-leg structure).
    Updates the provided TradeStore on success (local + Drive).

    Returns:
      True     — fully closed
      False    — a leg failed after orders were placed (partial_close state,
                 manual intervention alerted)
      'ABORT'  — nothing was placed/filled and the trade is still OPEN:
                 re-verify found the trigger was a quote artifact, or a
                 normal-urgency close could not get a reliable book. The
                 caller must clear its closing state and re-arm the trigger.

    reverify_sl: for SL_SPREAD/SL_TRAIL — the trigger threshold. Before any
    order, one FRESH quote is taken after REVERIFY_DELAY_SEC; if the fresh
    spread is reliable and back ABOVE the threshold (or is unreliable), the
    close aborts. Kills single-poll artifacts that healed. (2026-07-24)

    quote: the `get_spread_value` dict the trigger fired on, passed through to
    the exit vet so Claude judges the book that caused this, not a fresh one.
    Superseded by the re-verify quote when there is one. Omitting it is safe
    but pessimistic: a missing book reads as unreliable, which is the one state
    that always earns a vet.

    Urgency: SL_SPOT / EXPIRY_FORCE_CLOSE closes are URGENT — they may pay
    through unreliable books (bounded by anchor caps until the final
    attempt). Valuation closes are NORMAL — they never trade a garbage book.

    Safety checks:
      - Acquires close-lock (status='closing') before any orders
      - Late-day guard: NORMAL closes refuse after LAST_ORDER_TIME; URGENT
        (reduce-only) closes get until HARD_ORDER_CUTOFF_TIME
      - Verifies actual position state before placing any orders
      - If short is already flat/long, skips to closing the long
      - On both-legs-flat: marks trade closed with recovered fill prices
      - On partial failure: marks trade 'partial_close' (not 'open')
      - Sends Telegram alerts on trigger, success, and failure
    """
    if store is None:
        store = get_store()  # Default to BCS store for backward compat
    label = strategy_label
    stock = trade['stock']
    urgent = reason in URGENT_CLOSE_REASONS

    log("")
    log("=" * 70)
    log(f"  {reason} TRIGGERED! {stock} spot = {spot}")
    log(f"  Initiating spread close sequence... ({'URGENT' if urgent else 'normal'})")
    log("=" * 70)

    #: Set when a PAPER record is walked in dry run purely to journal the
    #: orders it WOULD have placed. It marks "journalled, not owned", and every
    #: human-escalation path in this function checks it: a record this engine
    #: has refused must never produce an instruction to go and trade.
    paper_passthrough = False

    # ── A PAPER RECORD NEVER REACHES THE ORDER PATH ──────────────────────
    #
    # `ZebraStoreAdapter.get_open_trades` already withholds these, so this is
    # not that filter repeated: it is the WRITE boundary rather than the read
    # one, and it is here because the two fail differently. The adapter decides
    # what the monitor SEES — it cannot cover a trade loaded by the CLI, by a
    # recovery sweep, or by whatever the next caller does. This decides what
    # the monitor may PLACE, and every route to a real order goes through this
    # function.
    #
    # `'ABORT'`, not False: nothing was placed and the position (such as it is)
    # is untouched, which is precisely the contract the callers' ABORT branch
    # is written against. It also means a misconfiguration cannot freeze the
    # record — a paper trade must never end up at partial_close.
    if _record_says_paper(trade):
        if dry_run:
            # A DRY RUN CANNOT PLACE AN ORDER, so refusing here buys no safety
            # and costs the evidence week: `journal_report --compare` works by
            # letting the monitor walk the whole close and journal what it
            # WOULD have done, then setting that beside zebra's real paper
            # booking. Aborting here would journal nothing, and that
            # comparison is what gates arming anything. Say what the armed
            # engine would have done, then carry on measuring.
            log(f"  [DRY RUN] trade #{trade.get('id')} is a PAPER record — "
                f"armed, this close would be REFUSED and nothing placed. "
                f"Continuing so the intended orders are journalled.")
            # JOURNALLING IS NOT OWNERSHIP. Walking on is right -- the compare
            # study needs the intended orders -- but every human-escalation
            # path below is now reachable for a record this engine has just
            # said it does not own, and an escalation is an instruction to go
            # and trade. On 2026-08-31 the late-day guard did exactly that:
            # "TP TRIGGERED GMRAIRPORT ... Close manually in Kite!" for paper
            # #455, which zebra closed correctly 53 seconds later. Nothing was
            # placed, but a human who obeyed would have put real orders on a
            # position that exists at no broker.
            paper_passthrough = True
        else:
            log(f"  REFUSED: trade #{trade.get('id')} is a PAPER record. It "
                f"has no legs at any broker, so a close here would either "
                f"book a price nobody transacted or put real orders on "
                f"somebody else's position. zebra books this one; nothing "
                f"is placed.")
            key = ('PAPER', trade.get('id'))
            today = now_ist().strftime('%Y-%m-%d')
            if _unpriced_close_alerted.get(key) != today:
                _unpriced_close_alerted[key] = today
                send_telegram(
                    f"⚠️ {label} {stock}: {reason} reached the ORDER PATH for "
                    f"paper trade #{trade.get('id')}. No order placed. "
                    f"The exit bridge should never have been handed this "
                    f"record — check who called close_spread for it.", alert_class=alert_policy.SAFETY)
            return 'ABORT'

    # ── Late-day guard (urgent closes are reduce-only and get 5 more min) ──
    now_t = now_ist().time()
    cutoff = HARD_ORDER_CUTOFF_TIME if urgent else LAST_ORDER_TIME
    if now_t > cutoff:
        log(f"  LATE-DAY GUARD: {now_t.strftime('%H:%M')} > {cutoff.strftime('%H:%M')}.")
        if paper_passthrough:
            # Journalled, not owned. Telling a human to "close manually in
            # Kite" would send them to trade a position that exists at no
            # broker; the engine that owns this record books it on its own
            # cycle. True rather than False for the same reason the
            # already-closing branch returns True: not an error, and not ours.
            log(f"  PAPER #{trade.get('id')}: no escalation — this record has "
                f"no legs at any broker and its own engine books the exit. "
                f"The intended orders are journalled; nothing is owed to a "
                f"human here.")
            return True
        log(f"  Too close to market close. Not placing orders — manual intervention needed.")
        send_telegram(
            f"{label} {reason} TRIGGERED {stock} @ {spot}\n"
            f"BUT past {cutoff.strftime('%H:%M')} — NOT auto-closing.\n"
            f"Close manually in Kite!", alert_class=alert_policy.SAFETY)
        return False

    # ── Re-verify valuation triggers on a FRESH quote before any order ────
    if reverify_sl is not None:
        time.sleep(REVERIFY_DELAY_SEC)
        try:
            # Spot passed so the re-verify applies the arbitrage floor too —
            # otherwise the freshest, most decision-relevant quote in the whole
            # flow would be the one quote nobody floor-checks.
            # fresh=True: this is THE second look. Serving it from the poll
            # cache would re-verify the trigger against the quote that caused
            # the trigger, which passes every time.
            fresh = get_spread_value(kite, trade, spot=spot, fresh=True)
        except Exception as e:
            log(f"  RE-VERIFY: quote fetch failed ({e}). Aborting close — trigger will re-arm.")
            return 'ABORT'
        if fresh['spread'] is None:
            log(f"  RE-VERIFY ABORT: fresh quote unreliable ({fresh['unreliable']}). "
                f"Not closing on a garbage book — trigger re-arms.")
            send_telegram(f"{label} {stock}: {reason} suppressed — fresh quote unreliable "
                          f"({fresh['unreliable']}). Trade still open, "
                          f"monitoring continues.",
                          alert_class=alert_policy.close_alert_class(
                              dry_run, _record_says_paper(trade)))
            return 'ABORT'
        if fresh['spread'] > reverify_sl:
            log(f"  RE-VERIFY ABORT: fresh spread {fresh['spread']:.2f} > {reverify_sl:.2f} — "
                f"trigger was a quote artifact. "
                f"(L bid {fresh['long']['bid']}/ask {fresh['long']['ask']} | "
                f"S bid {fresh['short']['bid']}/ask {fresh['short']['ask']})")
            send_telegram(f"{label} {stock}: {reason} near-miss — fresh spread "
                          f"{fresh['spread']:.2f} > SL {reverify_sl:.2f}. "
                          f"NOT closed (quote artifact).",
                          alert_class=alert_policy.close_alert_class(
                              dry_run, _record_says_paper(trade)))
            return 'ABORT'
        log(f"  RE-VERIFY CONFIRMED: fresh spread {fresh['spread']:.2f} <= {reverify_sl:.2f}. Proceeding.")
        # The freshest book wins. The vet is being asked "could this price be
        # a lie", and the price we are about to trade on is this one.
        quote = fresh

    # ── Claude exit vet — AFTER the deterministic guards, BEFORE the lock ──
    #
    # After the guards so an exit they were going to abort never spends an
    # agent, and so the agent judges the re-verified book. Before the lock for
    # the same reason zebra calls it before `set_alert_flag`: that flag is
    # consume-once and burning it on an exit that does not execute strands the
    # exit permanently.
    #
    # 'ABORT', not False: nothing has been placed and the trade is still open,
    # which is exactly what the caller's ABORT branch is for. It re-arms the
    # trigger, so a `wait` costs one confirm cycle and a `hold` re-nags at most
    # once a day until the human answers.
    if not exit_vet.exit_cleared(store, trade, reason, quote, spot,
                                 dry_run=dry_run, log=log):
        return 'ABORT'

    # ── Acquire close-lock (prevents concurrent close from another machine) ──
    close_lock_acquired = False
    if not dry_run:
        if not store.begin_close(trade['id'], reason):
            log(f"  Trade #{trade['id']} is already closing/closed. Skipping.")
            return True  # Not an error — another process has it
        close_lock_acquired = True

    try:
        result = _close_spread_inner(kite, store, trade, spot, reason, dry_run,
                                     label, urgent=urgent)
        # THE ESCALATION BOUNDARY, and it belongs here rather than at each of
        # the eight `return False` paths inside the inner close. Every one of
        # those means "this close did not complete", which for a REAL position
        # is an emergency the callers rightly Telegram about -- each of the
        # four sites sends its own "close FAILED. Manual intervention needed!"
        # on a falsy result. For a record this engine has already refused,
        # that same signal is an instruction to trade a position that exists
        # at no broker. Converting once, here, covers the paths that exist and
        # the ones added later; `is False` so an 'ABORT' still aborts.
        if paper_passthrough and result is False:
            log(f"  PAPER #{trade.get('id')}: the rehearsal did not complete, "
                f"which is not an emergency — no legs at any broker, and the "
                f"owning engine books the exit. Journalled; no escalation.")
            return True
        return result
    except Exception as e:
        log(f"  EXCEPTION during close_spread: {e}")
        if paper_passthrough:
            # Still reported -- an exception here is a CODE defect and hiding
            # it would be worse. But it is not a position emergency: there are
            # no legs at any broker, `close_lock_acquired` is False under dry
            # run so nothing is frozen, and the owning engine books the exit
            # regardless. Say which of those two things it is.
            send_telegram(
                f"{label} {stock}: EXCEPTION while journalling the intended "
                f"{reason} close of PAPER trade #{trade.get('id')}: {e}\n"
                f"No position at risk — this record has no legs at any broker "
                f"and its own engine books the exit. This is a CODE defect to "
                f"investigate, not a trade to place.",
                alert_class=alert_policy.SAFETY)
        else:
            send_telegram(f"{label} {stock}: EXCEPTION during {reason} close! {e}\nManual intervention needed!", alert_class=alert_policy.SAFETY)
        if close_lock_acquired:
            try:
                store.set_trade_status(
                    trade['id'], 'partial_close',
                    close_error=str(e), close_reason=reason,
                    close_failure=_close_failure(
                        cause='exception', leg=None, reason=reason))
            except Exception as inner_e:
                log(f"  Could not set partial_close status: {inner_e}")
                store._sync_locked = False  # Last resort: clear lock directly
        # Same boundary as the success path: the defect has already been
        # reported above in its own words, and a falsy return here would add
        # the callers' "Manual intervention needed!" on top of it.
        return True if paper_passthrough else False


#: M14 - the causes a close can freeze for. `rejected` is the one that CHANGES
#: BEHAVIOUR: the recovery sweep escalates on it immediately and places zero
#: orders, because the reason a broker rejected (margin, price band, frozen
#: scrip) will repeat, and re-firing into it is the Feb-2026 "firing into a
#: broken state" failure. The rest are forensic - the sweep classifies from
#: LIVE broker positions, never from this snapshot, because the human may have
#: fixed it in the meantime.
CLOSE_FAILURE_CAUSES = ('unfilled', 'rejected', 'unreadable_book',
                        'exception', 'flipped')


def _close_failure_cause(result):
    """Read the freeze cause off a `close_leg` result.

    `None` means the leg was never tradeable - no book, no order, nothing
    placed - which is `unreadable_book` and NOT `unfilled`: one says we could
    not look, the other says we looked and nobody traded. They earn different
    recovery treatment, and collapsing them is the
    `feedback_never_asked_is_not_failed` mistake.
    """
    if result is None:
        return 'unreadable_book'
    status = str((result or {}).get('status') or '').upper()
    return 'rejected' if status == 'REJECTED' else 'unfilled'


def _close_failure(*, cause, leg, reason):
    """The frozen-close record M14's recovery sweep reads.

    Stamped in the SAME `set_trade_status` call that freezes the trade, never
    at Telegram-send time: a dropped send must not postpone recovery, and
    acting is the safe direction when nobody was told.

    `attempts` starts at 0 and is incremented BEFORE an order is placed, so a
    crash mid-attempt costs one attempt and never gains one - the order
    journal's intent-before-broker-call doctrine, applied to retries. `state`
    starts `frozen`; the sweep moves it to `recovering` / `escalated` /
    `resolved`, and an unrecognised value must resolve to `escalated`, the
    pessimistic side. (C0's polarity lesson applied in the direction that
    matters here: for exit REASONS an unknown must not count as a stop; for
    PERMISSION TO PLACE ORDERS an unknown must count as no.)

    A `partial_close` record with NO `close_failure` is a freeze from before
    this shipped and is treated as `escalated` - alert and nag, never order.
    Retrofitting automation onto a freeze nobody diagnosed is how helpfulness
    loses money.
    """
    assert cause in CLOSE_FAILURE_CAUSES, cause
    return {
        'frozen_at': now_ist().isoformat(),
        'cause': cause,
        'leg': leg,
        'reason': reason,
        'state': 'frozen',
        'attempts': 0,
        'next_attempt_after': None,
        'recovery_fills': {},
    }


def _blend_fill(first_price, first_qty, retry_price, retry_qty):
    """N13 — quantity-weighted price for a leg that filled in TWO tranches.

    A partial close gets one urgent retry on the residual. The retry's
    `filled_quantity` was read (to decide whether residue remains); its PRICE
    was thrown away, and the leg kept the FIRST tranche's `average_price`. That
    is not an approximation, it is the wrong number weighted at 100%:

        short 600 @ 5.00, retry 100 @ 50.00
          booked   short_fill  5.00   -> pnl/share +1.45, total +Rs 1,015
          true     short_fill 11.4286 -> pnl/share -4.98, total -Rs 4,980

    Rs 4,500 out, and it reports a LOSS AS A WIN. It bites only when a retry
    clears the residue — the path D2/D3 just made reachable, so it gets MORE
    likely, not less.

    **An unpriced tranche makes the whole leg unpriced.** If a tranche filled
    quantity but carries no usable price, there is no honest weighted average,
    so this returns None rather than silently weighting the half it can see.
    None is the established signal here: `_close_spread_inner` routes a None
    leg fill to `_refuse_unpriced_close`, which books nothing and escalates.
    Returning the known tranche's price instead would be the
    default-that-looks-like-a-value mistake this file keeps relearning — a
    price nobody transacted at, presented as one.
    """
    if not retry_qty or retry_qty <= 0:
        return first_price            # no second tranche; nothing to blend
    if retry_price is None or retry_price <= 0:
        return None                   # filled, but at a price we never saw
    if not first_qty or first_qty <= 0:
        return retry_price            # first tranche filled nothing
    if first_price is None or first_price <= 0:
        return None                   # ditto, other tranche
    total = first_qty + retry_qty
    return (first_price * first_qty + retry_price * retry_qty) / total


def _close_spread_inner(kite, store, trade, spot, reason, dry_run, label='BCS',
                        urgent=False, recovery=False, recovery_urgent=False,
                        book=None):
    """Inner close logic, separated so close_spread() can wrap with try/except.

    **M14 recovery reuses this function rather than copying it.** It is already
    position-driven, reduce-only, skip-flat-legs, flip-guarded, already-flat
    booking and unpriced-refusing; a second "recovery close" would be the
    copy-nobody-opens shape that produced D2. `recovery=True` changes exactly
    three things and nothing else:

      * ONE order per leg (`attempts=1`). The initial close already spent three
        escalating orders on this leg; "try 3 times" means 3 CLOSE attempts,
        not 3x3 = 9 orders. Ceiling per leg per incident: 3 + 3 = 6.
      * urgency comes from the RISK CLASS (`recovery_urgent`), not from the
        original trigger, which may have been a leisurely valuation stop on a
        position that is now a naked short.
      * uncapped pay-through only for that urgent class. Never pay through a
        garbage book to exit a bounded long — its downside is the premium
        already paid, and the pay-through can cost more than holding does.

    `spot` may be None on the recovery path: the sweep classifies from broker
    POSITIONS, not from price, and fetching a spot it does not use would be a
    request per frozen record for a number that decides nothing here.
    """
    stock = trade['stock']
    short_sym = trade['short_symbol']
    long_sym = trade['long_symbol']
    qty = trade['quantity']
    exchange = trade['exchange']

    # DISPLAY only. `spot` itself stays None on the record, which is the honest
    # value: the recovery sweep classifies from broker POSITIONS and never
    # reads a price, so there is no spot to record. Rendering a bare None in
    # the alert would read as a FAILED FETCH — the opposite of what happened.
    spot_disp = (spot if spot is not None
                 else 'n/a (recovery: classified from positions)')

    # SHADOW for a paper record (zebra sends the one that carries the P&L a
    # few minutes later); SAFETY for a real record the disarmed monitor cannot
    # close. Logged either way — see `send_telegram`.
    send_telegram(trigger_alert_text(label, stock, reason, spot_disp, dry_run,
                                     trade, 'Closing spread...'),
                  alert_class=alert_policy.close_alert_class(
                      dry_run, _record_says_paper(trade)))

    # ── Pre-flight: Check actual position state ──────────────────────────
    if not dry_run:
        short_qty = get_net_position(kite, short_sym)
        long_qty = get_net_position(kite, long_sym)
    else:
        short_qty = -qty  # Assume normal state for dry run
        long_qty = qty

    log(f"  Position check: {short_sym} qty={short_qty} | {long_sym} qty={long_qty}")

    # ── B11 · Guard: a FLIPPED leg is not a flat leg ─────────────────────
    #
    # The flat check below used to read `short_qty >= 0 and long_qty <= 0`.
    # A short leg sitting at **+2100** — four 700-lot BUYs against -700, the
    # literal Feb-2026 ICICIBANK shape — satisfies `>= 0`, so the trade was
    # booked CLOSED with a live naked long, and `reconcile_after_close` was
    # never reached on that branch. The position then sat unmonitored because
    # nothing looks at a closed trade.
    #
    # Place NO orders here. A flipped leg means the ORDER-PLACING code is what
    # needs auditing; putting more orders on top is precisely the amplification
    # that turned a stop into a four-fill loss. Freeze it, say so loudly, and
    # record the broker's own view.
    flipped = []
    if short_qty > 0:
        flipped.append(f"SHORT {short_sym} is LONG {short_qty:+d}")
    if long_qty < 0:
        flipped.append(f"LONG {long_sym} is SHORT {long_qty:+d}")
    if flipped:
        detail = '; '.join(flipped)
        log(f"\n  *** FLIPPED POSITION — NO ORDERS WILL BE PLACED ***")
        log(f"  {detail}")
        log(f"  short={short_qty:+d} long={long_qty:+d} (expected short<0, long>0)")
        if not dry_run:
            store.set_trade_status(
                trade['id'], 'partial_close',
                close_reason=f"FLIPPED_{reason}",
                flipped=detail,
                short_qty_seen=short_qty, long_qty_seen=long_qty,
                close_failure=_close_failure(
                    cause='flipped', leg=None, reason=reason))
            reconcile_after_close(kite, trade, label, store=store)
        send_telegram(
            f"🔴 {label} {stock}: FLIPPED POSITION\n"
            f"{reason} triggered, but the book is not what the trade says:\n"
            f"{detail}\n"
            f"No orders placed. The trade is frozen at "
            f"partial_close and will NOT be monitored further.\n"
            f"This is the Feb-2026 shape — audit the order history for this "
            f"leg before doing anything else.", alert_class=alert_policy.SAFETY)
        return False

    # ── Guard: Both legs already flat — mark closed with recovered fills ──
    # Strictly zero on both sides. Anything else is handled above.
    if short_qty == 0 and long_qty == 0:
        log(f"\n  Both legs already flat (short={short_qty}, long={long_qty}).")
        log(f"  Trade was closed by another process or manually. Recovering fill prices...")
        short_rec = find_recoverable_fill(kite, short_sym, "BUY")
        long_rec = find_recoverable_fill(kite, long_sym, "SELL")
        short_fill = short_rec['price']
        long_fill = long_rec['price']
        entry_net = trade['net_debit']
        if short_fill is None or long_fill is None:
            # N1. THIS USED TO BOOK `exit_net = 0.0`.
            #
            # Zero is not a missing price, it is the WORST price a vertical can
            # close at, so the fallback computed `pnl_per_share = 0.0 - debit`
            # and booked a silent MAX LOSS from a number nobody observed --
            # take-profits included. It survived the `exit_value` fix because
            # 0.0 is not None and the adapter's None-guard never saw it.
            #
            # Refuse, exactly as the paper engine refuses an unusable book:
            # "Paper never books a price it could not have transacted at", and
            # the money path is held to at least that standard. The position is
            # flat either way -- nothing here is at risk -- so the only thing
            # lost by waiting is a P&L figure, and the only thing lost by
            # guessing is the truth about it.
            #
            # The record stays at 'closing' ON PURPOSE. It remains in
            # OPEN_STATUSES (capital keeps counting it, the scanner keeps
            # blocking its stock), `get_closing_trades` surfaces it, and the
            # crash-recovery sweep un-freezes it at the next process start --
            # which is also what rate-limits this escalation to roughly once
            # per run rather than once per five-second poll.
            return _refuse_unpriced_close(
                trade, label, reason, spot, dry_run,
                [('short', short_sym, 'BUY', short_rec),
                 ('long', long_sym, 'SELL', long_rec)])
        exit_net = long_fill - short_fill
        exit_data = {
            'exit_date': now_ist().isoformat(),
            'exit_reason': f"ALREADY_FLAT_{reason}",
            'exit_spot': spot,
            'short_fill': short_fill,
            'long_fill': long_fill,
            'exit_spread': exit_net,
            # No `if exit_net else 0` any more. Both fills are OBSERVED by the
            # time this runs, so an exit_net of exactly 0.00 is a real price
            # (long 10.00 / short 10.00 nets zero) and reporting its P&L as
            # zero rather than -debit was a second, smaller lie riding on the
            # same falsy test that produced the first one.
            'pnl_per_share': exit_net - entry_net,
            'total_pnl': (exit_net - entry_net) * qty,
            'notes': (f'Both legs flat when close triggered. Fills recovered '
                      f'from {ORDER_TAG} order history.'),
        }
        if not dry_run:
            store.update_trade_exit(trade['id'], exit_data)
            log(f"  Trade #{trade['id']} marked closed (already flat)")
        send_telegram(f"{label} {stock}: {reason} triggered but both legs already flat. Marked closed.")
        return True

    short_fill = 0.0
    long_fill = 0.0
    short_closed_now = False
    long_closed_now = False

    # ── Step 1: Close SHORT leg (BUY back) — only if still short ─────────
    if short_qty < 0:
        close_qty = min(abs(short_qty), qty)
        log("")
        log(f"STEP 1: Close SHORT leg -> BUY {short_sym} x {close_qty}")
        log("-" * 55)
        short_result = close_leg(
            kite, exchange, short_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run,
            urgent=(recovery_urgent if recovery else urgent),
            context=_order_ctx(trade, reason, 'short', label, book=book),
            attempts=1 if recovery else None,
            allow_pay_through=(recovery_urgent if recovery else True)
        )

        if not short_result or short_result['status'] not in ('COMPLETE', 'PARTIAL'):
            # close_leg returns None only when ZERO qty filled — for a NORMAL
            # (valuation-triggered) close the position is untouched, so abort
            # back to 'open' and let the trigger re-arm instead of freezing
            # the trade in partial_close. (2026-07-24 guard design.)
            if not recovery and not urgent and short_result is None:
                log(f"\n  NORMAL close abort: short leg not tradeable/fillable, nothing filled.")
                log(f"  Trade stays OPEN — trigger re-arms on the next reliable poll.")
                send_telegram(f"{label} {stock}: {reason} close aborted — short-leg book not "
                              f"tradeable. Trade still open, monitoring continues.")
                if not dry_run:
                    store.recover_closing_trade(trade['id'])
                return 'ABORT'
            msg = f"CRITICAL: {stock} SHORT LEG CLOSE FAILED! Manual intervention needed."
            log("")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log("!!! CRITICAL: SHORT LEG CLOSE FAILED              !!!")
            log("!!! DO NOT close long leg manually - margin spike! !!!")
            log("!!! Intervene manually in Kite terminal            !!!")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            send_telegram(msg)
            if not dry_run:
                store.set_trade_status(
                    trade['id'], 'partial_close',
                    close_failed_leg='short', close_reason=reason,
                    close_failure=_close_failure(
                        cause=_close_failure_cause(short_result),
                        leg='short', reason=reason))
            return False

        short_fill = short_result.get('average_price', 0)
        short_closed_now = True
        if short_result['status'] == 'PARTIAL':
            remaining = close_qty - short_result.get('filled_quantity', 0)
            log(f"  WARNING: Short leg partially filled. {remaining} qty still short!")
            send_telegram(f"{label} {stock}: Short leg partial fill. {remaining} still short!", alert_class=alert_policy.SAFETY)

            # ── B10 · the long leg is sold ONLY when the short is flat ──────
            #
            # 'PARTIAL' passes the success check above, so the old code fell
            # straight through to Step 2 and sold the long in FULL — leaving a
            # NAKED SHORT residue — then `update_trade_exit` marked the trade
            # closed so nothing monitored it again. A reconciliation Telegram
            # fired once; if that send dropped, the naked short sat overnight.
            #
            # One urgent retry on the residual only. `close_leg` tracks the
            # remaining quantity itself and `_find_pending_orders` already
            # prevents doubling up, so this cannot become the Feb-2026
            # multiple-order shape.
            # On the RECOVERY path this retry is skipped: the one order per
            # leg per attempt is already spent, and firing a second here would
            # silently double the ceiling the sweep is counting against.
            if remaining > 0 and not recovery:
                log(f"  RETRY: closing the {remaining} qty residual (urgent)")
                retry = close_leg(kite, exchange, short_sym, "BUY", remaining,
                                  is_buy=True, dry_run=dry_run, urgent=True,
                                  context=_order_ctx(trade, reason, 'short-retry', label, book=book))
                got = retry.get('filled_quantity', 0) if retry else 0
                remaining -= got
                log(f"  RETRY filled {got}; {remaining} still short")
                # N13 — fold the retry's PRICE in, not just its quantity.
                short_fill = _blend_fill(
                    short_fill, short_result.get('filled_quantity', 0),
                    (retry or {}).get('average_price'), got)
                log(f"  Short fill after blend: {short_fill}")

            if remaining > 0:
                # Leaving 700 long against 200 short is an OVER-HEDGED debit
                # position with bounded risk. Selling the long would leave a
                # naked short. Prefer the bounded one and stop, every time.
                log("")
                log("  *** SHORT RESIDUE REMAINS — LONG LEG WILL NOT BE SOLD ***")
                log(f"  {remaining} qty still short on {short_sym}.")
                if not dry_run:
                    store.set_trade_status(
                        trade['id'], 'partial_close',
                        close_reason=f"PARTIAL_SHORT_{reason}",
                        residual_short_qty=remaining,
                        short_fill=short_fill,
                        close_failure=_close_failure(
                            cause='unfilled', leg='short', reason=reason))
                    reconcile_after_close(kite, trade, label, store=store)
                send_telegram(
                    f"🔴 {label} {stock}: PARTIAL SHORT CLOSE\n"
                    f"{remaining} qty still SHORT on {short_sym} after a retry.\n"
                    f"The long leg was NOT sold — selling it would leave you "
                    f"naked short. You are over-hedged, which is the bounded "
                    f"side.\n"
                    f"Trade frozen at partial_close and NOT monitored further. "
                    f"Close the residue by hand.", alert_class=alert_policy.SAFETY)
                return False
    else:
        log(f"\n  SHORT leg {short_sym} already flat/long (qty={short_qty}). Skipping BUY.")

    # ── Step 2: Close LONG leg (SELL) — only if still long ───────────────
    if long_qty > 0:
        close_qty = min(long_qty, qty)
        log("")
        log(f"STEP 2: Close LONG leg -> SELL {long_sym} x {close_qty}")
        log("-" * 55)
        # Escalation invariant: once the short leg has FILLED this close, the
        # long leg MUST exit too (a lingering naked long is unhedged risk +
        # the close can never abort half-done) — run it urgent regardless of
        # the original trigger's urgency.
        long_urgent = urgent or short_closed_now
        long_result = close_leg(
            kite, exchange, long_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run,
            urgent=long_urgent,
            context=_order_ctx(trade, reason, 'long', label, book=book),
            attempts=1 if recovery else None,
            allow_pay_through=(recovery_urgent if recovery else True)
        )

        if not long_result or long_result['status'] not in ('COMPLETE', 'PARTIAL'):
            # Same pre-fill abort as the short leg: if this close hasn't
            # touched the position at all (short was already flat, long fill
            # zero) a NORMAL close aborts back to open instead of freezing.
            # NOT on the recovery path. `recover_closing_trade` puts the
            # record back to `open`, which is right for a NORMAL close that
            # never touched the position (the trigger simply re-arms) and
            # WRONG for a recovery: this record was already frozen, so
            # "aborting" it to `open` drops it out of `get_frozen_trades()`,
            # discards the attempt count, and hands a half-closed position to
            # a per-trade loop that will price it as a whole spread. The
            # incident must stay an incident.
            if (not recovery and not long_urgent and long_result is None
                    and not short_closed_now):
                log(f"\n  NORMAL close abort: long leg not tradeable/fillable, nothing filled.")
                log(f"  Trade stays OPEN — trigger re-arms on the next reliable poll.")
                send_telegram(f"{label} {stock}: {reason} close aborted — long-leg book not "
                              f"tradeable. Trade still open, monitoring continues.")
                if not dry_run:
                    store.recover_closing_trade(trade['id'])
                return 'ABORT'
            actual_close_qty = min(long_qty, qty)
            msg = (f"WARNING: {stock} LONG LEG CLOSE FAILED!\n"
                   f"Short is closed. Naked long {long_sym} remains.\n"
                   f"Close manually: SELL {actual_close_qty}")
            log("")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log(f"!!! WARNING: LONG LEG CLOSE FAILED                !!!")
            log(f"!!! Short is closed - naked long remains           !!!")
            log(f"!!! Close {long_sym} manually (SELL {actual_close_qty}) !!!")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            send_telegram(msg)
            if not dry_run:
                store.set_trade_status(
                    trade['id'], 'partial_close',
                    close_failed_leg='long', close_reason=reason,
                    short_fill=short_fill,
                    close_failure=_close_failure(
                        cause=_close_failure_cause(long_result),
                        leg='long', reason=reason))
            return False

        long_fill = long_result.get('average_price', 0)
        long_closed_now = True

        # ── D3 · a PARTIAL long sale is not a closed long leg ───────────────
        #
        # 'PARTIAL' passes the check above, so this used to fall through to the
        # summary: `update_trade_exit` booked the trade CLOSED, `total_pnl` was
        # computed on the FULL quantity, and the unsold residue sat live at the
        # broker under a record no engine looks at again. Bounded risk — it is
        # a long option — but the same invisibility as D2 and a P&L that is
        # simply wrong. One urgent retry (the short is already flat, so the
        # escalation invariant applies), then FREEZE rather than book a lie.
        if long_result['status'] == 'PARTIAL':
            remaining = close_qty - long_result.get('filled_quantity', 0)
            if remaining > 0 and not recovery:
                log(f"  WARNING: Long leg partially filled. {remaining} qty still long!")
                log(f"  RETRY: closing the {remaining} qty residual (urgent)")
                retry = close_leg(kite, exchange, long_sym, "SELL", remaining,
                                  is_buy=False, dry_run=dry_run, urgent=True,
                                  context=_order_ctx(trade, reason, 'long-retry', label, book=book))
                got = retry.get('filled_quantity', 0) if retry else 0
                remaining -= got
                log(f"  RETRY filled {got}; {remaining} still long")
                # N13 — fold the retry's PRICE in, not just its quantity.
                long_fill = _blend_fill(
                    long_fill, long_result.get('filled_quantity', 0),
                    (retry or {}).get('average_price'), got)
                log(f"  Long fill after blend: {long_fill}")
            if remaining > 0:
                log("")
                log("  *** LONG RESIDUE REMAINS — NOT BOOKING ***")
                log(f"  {remaining} qty still long on {long_sym}.")
                if not dry_run:
                    store.set_trade_status(
                        trade['id'], 'partial_close',
                        close_failed_leg='long',
                        close_reason=f"PARTIAL_LONG_{reason}",
                        residual_long_qty=remaining,
                        short_fill=short_fill, long_fill=long_fill,
                        close_failure=_close_failure(
                            cause='unfilled', leg='long', reason=reason))
                    reconcile_after_close(kite, trade, label, store=store)
                send_telegram(
                    f"🔴 {label} {stock}: PARTIAL LONG CLOSE\n"
                    f"{remaining} of {close_qty} qty still LONG on {long_sym} "
                    f"after an urgent retry.\n"
                    f"The short leg is flat, so nothing is naked — but the "
                    f"residue is a live option and NOTHING was booked: the "
                    f"P&L would have been computed on the full quantity.\n"
                    f"Trade frozen at partial_close and NOT monitored further. "
                    f"Sell the residue by hand.", alert_class=alert_policy.SAFETY)
                return False
    else:
        log(f"\n  LONG leg {long_sym} already flat/short (qty={long_qty}). Skipping SELL.")

    # ── A leg that went flat without one of OUR fills has NO price ───────
    #
    # `close_leg` returns `average_price: None` when it finds the leg already
    # flat and no ORDER_TAG order priced it (its "position_verified" return).
    # Before 2026-08-27 that was 0.0 and it flowed straight into the arithmetic
    # below — the same N1 lie as the already-flat branch, reached by a
    # different door, which is exactly the shape that keeps recurring here.
    # Same answer: refuse, escalate, book nothing.
    unpriced = []
    if short_closed_now and short_fill is None:
        unpriced.append(('short', short_sym, 'BUY',
                         {'price': None, 'untagged': 0, 'error': None}))
    if long_closed_now and long_fill is None:
        unpriced.append(('long', long_sym, 'SELL',
                         {'price': None, 'untagged': 0, 'error': None}))
    if unpriced:
        return _refuse_unpriced_close(trade, label, reason, spot, dry_run,
                                      unpriced)

    # ── Summary ──────────────────────────────────────────────────────────
    entry_net = trade['net_debit']
    # N14 — a leg we did not close is counted at 0.00, so the figure is not
    # merely uncertain, it is WRONG IN A KNOWN DIRECTION. The log already said
    # so and the RECORD did not, which meant `pnl_net`, the digest and the
    # journal report all read an approximation as exact. `approx_legs` names
    # the unpriced side; `bound_bcs_exit` turns that into the marker.
    approx_legs = []
    if short_closed_now and long_closed_now:
        exit_net = long_fill - short_fill
    elif long_closed_now and not short_closed_now:
        exit_net = long_fill
        approx_legs.append('short_fill')
        log(f"  NOTE: Short was already flat. P&L is approximate (long fill only).")
    elif short_closed_now and not long_closed_now:
        exit_net = -short_fill
        approx_legs.append('long_fill')
        log(f"  NOTE: Long was already flat. P&L is approximate (short fill only).")
    else:
        # NEITHER leg was closed by us. There is no fill anywhere in this
        # figure — it is not an approximation of a close, it is a close that
        # did not happen — so both legs are unknown, not zero.
        exit_net = 0.0
        approx_legs.extend(('short_fill', 'long_fill'))
        log(f"  NOTE: Neither leg was closed by us. P&L is NOT computed from "
            f"any fill.")
    pnl_per_share = exit_net - entry_net
    total_pnl = pnl_per_share * qty

    log("")
    log("=" * 70)
    log(f"  SPREAD {'WOULD CLOSE [DRY RUN]' if dry_run else 'CLOSED SUCCESSFULLY'}")
    log("=" * 70)
    log(f"  Reason:                 {reason}")
    if dry_run:
        # Every one of these is 0.0 from the dry stub, and the arithmetic on
        # them is a fabricated max loss — see `close_alert_text`. The log is
        # the forensic record of the dry-run week, so it must not carry the
        # invented numbers either.
        log( "  Fills:                  none — no order was placed")
        log(f"  Entry spread cost:      {entry_net:.2f}/share")
        log( "  P&L:                    not computed (dry run has no fills)")
    else:
        log(f"  Short (BUY back) fill:  {short_fill}")
        log(f"  Long  (SELL)     fill:  {long_fill}")
        log(f"  Exit spread value:      {exit_net:.2f}/share")
        log(f"  Entry spread cost:      {entry_net:.2f}/share")
        log(f"  P&L per share:          {pnl_per_share:+.2f}")
        log(f"  Total P&L:              Rs {total_pnl:+,.0f}")
    log(f"  Spot at close:          {spot}")
    log("=" * 70)

    send_telegram(close_alert_text(
        label, stock, reason, spot,
        ([f"Entry: {entry_net:.2f} | Exit: n/a (no order placed)"] if dry_run
         else [f"Exit spread: {exit_net:.2f} | Entry: {entry_net:.2f}"]),
        total_pnl, dry_run, trade=trade),
        alert_class=alert_policy.close_alert_class(
            dry_run, _record_says_paper(trade)))

    # ── Update trade store ───────────────────────────────────────────────
    exit_data = {
        'exit_date': now_ist().isoformat(),
        'exit_reason': reason,
        'exit_spot': spot,
        'short_fill': short_fill,
        'long_fill': long_fill,
        'exit_spread': exit_net,
        'pnl_per_share': pnl_per_share,
        'total_pnl': total_pnl,
    }
    if approx_legs:
        # An unclosed leg is recorded as UNKNOWN, never as the 0.00 the
        # arithmetic above had to use. `bound_bcs_exit` reads these Nones at
        # the store's write boundary and sets `pnl_approximate` there, so a
        # hand-built exit gets the same treatment.
        for f in approx_legs:
            exit_data[f] = None
        exit_data['pnl_approximate'] = True
        exit_data['unpriced_legs'] = list(approx_legs)
        exit_data['notes'] = (
            'P&L is APPROXIMATE: ' + ', '.join(approx_legs)
            + ' — the leg was already flat on arrival and is counted at 0.00.')

    if not dry_run:
        store.update_trade_exit(trade['id'], exit_data)
        log(f"  Trade #{trade['id']} marked closed (local + Drive)")
        reconcile_after_close(kite, trade, label, store=store)
    else:
        log(f"  [DRY RUN] Would mark trade #{trade['id']} closed")

    return True


def reconcile_after_close(kite: KiteConnect, trade: dict, label: str = 'BCS',
                          *, store=None) -> bool:
    """After a close reports success, PROVE every leg is actually flat.

    This is the ICICI-class guard, and it is the one thing the vetting layer
    cannot supply: in Feb 2026 a monitor bug placed 4x BUY on the short leg and
    flipped the position long, turning a +190% spread into +Rs 2K. The bug was
    in the code that placed the orders - so the code that placed them was never
    going to catch it. This check reads the BROKER's view instead, and it is
    deliberately independent of every fill/quantity variable the close path
    computed.

    **S3.** A residue found here is persisted as `reconcile_residue` on the
    record and swept until the book goes flat. Until 2026-08-29 it was one
    Telegram and nothing else: the record is already booked terminal, so it is
    out of the open book, out of `get_frozen_trades()` and out of every sweep
    there is. An unwatched live leg is the failure that has cost this account
    real money twice. `store` is optional only because a caller with nothing
    to write to (a probe, a test) is a real case; when it is None the alert
    still fires and only the sweep is lost, which the log says out loud.

    Read-only against the broker and never raises: it cannot make an exit
    worse, only visible. Returns True when flat.
    """
    try:
        legs = _legs_of(trade)
        if not legs:
            # UNKNOWN IS NOT FLAT.
            #
            # Until 2026-08-29 this read `short_symbol` and `long_symbol` and
            # nothing else, so all four FH call sites - the ones guarding the
            # only NAKED SHORT this fleet ever holds - found no symbols,
            # collected no residues, and logged "both legs flat OK" for a book
            # they had not looked at. A guard that reports CLEAN on a position
            # it never read is worse than no guard, because it answers the
            # question. `_legs_of` is the fleet's one leg reader, and a record
            # it cannot describe fails here rather than passing.
            log("  *** RECONCILE: #%s declares NO LEGS - nothing was "
                "verified ***" % trade.get('id'))
            _recovery_event('reconcile_unknown_shape', store=label,
                            id=trade.get('id'))
            send_telegram(
                "\U0001F6A8 %s %s: CLOSE NOT VERIFIED\n"
                "The record declares no option legs, so the post-close check "
                "could not read the broker at all. This is NOT a clean close "
                "- check Kite by hand." % (label, trade.get('stock')),
                alert_class=alert_policy.SAFETY)
            return False

        residues, live = [], {}
        for sym, _sign in legs:
            qty = get_net_position(kite, sym)
            live[sym] = qty
            if qty != 0:
                residues.append('%s net %+d' % (sym, qty))
        if not residues:
            log("  RECONCILE: all %d leg(s) flat at the broker \u2713" % len(legs))
            return True
        detail = '; '.join(residues)
        log("  *** RECONCILE FAILED: %s ***" % detail)
        _recovery_event('reconcile_residue', store=label, id=trade.get('id'))
        _persist_residue(store, trade, label, detail, live)
        send_telegram(
            "\U0001F6A8 %s %s: POSITION NOT FLAT AFTER CLOSE\n"
            #: The record's own status, not the word "closed". This runs from
            #: the FREEZE paths too, where the record sits at `partial_close`;
            #: telling the owner it is "marked closed" would send them looking
            #: for the wrong record.
            "The record is at status=%s but the broker still shows: %s\n"
            "Check Kite NOW - this is the shape of the Feb 2026 bug that "
            "flipped a short leg long."
            % (label, trade.get('stock'), trade.get('status', '?'), detail),
            alert_class=alert_policy.SAFETY)
        return False
    except Exception as e:
        # Never let the audit break the close it is auditing. It does not go
        # quiet either: a reconcile that could not run is a close nobody
        # verified, and the digest counts it by name.
        log("  RECONCILE: could not verify positions (%s)" % e)
        _recovery_event('reconcile_blind', store=label, id=trade.get('id'),
                        reason=str(e)[:60].replace(' ', '_'))
        return False


# ── Residue: a leg is live and no record accounts for it ───────────────────
#
# TWO SPECIES, ONE MECHANISM. They arrive from opposite ends of a position's
# life and they need different sentences from whoever reads the Telegram, but
# everything between detection and resolution is identical: persist the
# incident on the record, re-read the broker every sweep, nag once a day,
# resolve on two DATED consecutive flat reads outside the opening window, and
# never, ever place an order.
#
# So the machinery is parameterised rather than copied
# (`feedback_copy_pasted_modules_fix_once`). The second species is the one
# that made this necessary:
#
#   POST-CLOSE (S3) — the record is booked terminal and a leg is still live.
#     `reconcile_after_close` finds it. This is the ICICI Feb-2026 shape.
#   ENTRY — an entry round bought its long and could not sell its short. The
#     executor REPORTS the orphan and deliberately never unwinds it (placing a
#     corrective order through the book that just failed is the amplification
#     that turned a Feb-2026 stop into a four-fill loss). Until now "reports"
#     meant one Telegram and nothing else: the orphan exists in NO store, so
#     the frozen sweep, this sweep, the startup verification and `--list` all
#     miss it — they read RECORDS. It is the entry-side twin of S3, and S3 was
#     judged worth building.


class ResidueKind:
    """One species of residue, as data the shared machinery reads."""

    def __init__(self, *, field, lister, headline, provenance, detect_event,
                 unresolved_event, resolved_event, blind_event, clear_hint):
        #: Record field the incident lives under. Also the nag key, so two
        #: species on one record cannot silence each other.
        self.field = field
        #: Store method listing records carrying an OPEN incident of this kind.
        self.lister = lister
        #: First line of the alert.
        self.headline = headline
        #: How the leg came to be there — one sentence, because the reader's
        #: next action differs completely between the two.
        self.provenance = provenance
        #: Event names are DECLARED, never derived from `field`. `zebra/
        #: engine_log.py` renders each by name and its list of known events is
        #: a literal; a name assembled here would render as an unknown event
        #: in the digest the arming decision is read from.
        self.detect_event = detect_event
        self.unresolved_event = unresolved_event
        self.resolved_event = resolved_event
        self.blind_event = blind_event
        self.clear_hint = clear_hint


POST_CLOSE_RESIDUE = ResidueKind(
    field='reconcile_residue',
    lister='get_residue_trades',
    headline='A LEG IS STILL LIVE ON A CLOSED TRADE',
    provenance='The record was booked closed on %(detected_at)s and the '
               'broker still shows that leg.',
    detect_event='reconcile_residue',
    unresolved_event='residue_unresolved',
    resolved_event='reconcile_residue_resolved',
    blind_event='residue_blind',
    clear_hint='NO order will be placed against a closed record - close it by '
               'hand in Kite, or clear the incident with '
               '`--clear-residue %(book)s:%(id)s` if you are holding it on '
               'purpose.',
)

ENTRY_RESIDUE = ResidueKind(
    field='entry_residue',
    lister='get_entry_residue_trades',
    headline='AN ENTRY LEFT A LEG NOTHING IS MANAGING',
    provenance='An entry round on %(detected_at)s filled this leg and could '
               'not complete its spread. It is NOT part of any recorded '
               'position, so no stop, no trail and no target applies to it.',
    detect_event='entry_residue',
    unresolved_event='entry_residue_unresolved',
    resolved_event='entry_residue_resolved',
    blind_event='entry_residue_blind',
    clear_hint='NO order will be placed against it - the entry path never '
               'unwinds (a corrective order through the book that just failed '
               'is how a Feb-2026 stop became a four-fill loss). Close or keep '
               'it by hand, or clear the incident with '
               '`--clear-residue %(book)s:%(id)s`.',
)

RESIDUE_KINDS = (POST_CLOSE_RESIDUE, ENTRY_RESIDUE)


def _residue_watch(trade, kind, res):
    """The symbols this incident is about, as [(symbol, sign)].

    Recorded on the incident at DETECTION (`watch`) rather than re-derived
    from the record each sweep. For an entry residue that is the only way to
    know: the record's declared legs are the spread that was INTENDED, and the
    orphan is precisely the part of it that did not happen.

    Legacy post-close incidents carry no `watch` and fall back to the record's
    legs, which is what they were swept on before.
    """
    watch = res.get('watch')
    if watch:
        return [(sym, 0) for sym in watch]
    if kind is POST_CLOSE_RESIDUE:
        return _legs_of(trade)
    return []


def _persist_residue(store, trade, label, detail, live,
                     kind=None):
    """Write the incident onto the record so a sweep can find it tomorrow.

    Idempotent on `detected_at`: a residue re-observed is the SAME incident,
    and resetting its clock would make an unresolved one look new every time
    it was seen. Only `last_seen` and the live quantities move.

    `watch` is written ONCE, at detection, and never moved afterwards: it is
    the set of symbols the incident is about. Re-deriving it each sweep from
    the record works for a post-close residue (the record declares the legs it
    just closed) and cannot work for an entry one (the record declares the
    spread that was INTENDED, and the orphan is the half that did not happen).
    """
    kind = kind or POST_CLOSE_RESIDUE
    res = dict(trade.get(kind.field) or {})
    now_iso = now_ist().isoformat()
    if res.get('state') != 'open':
        res = {'detected_at': now_iso, 'state': 'open', 'resolved_at': None,
               'watch': sorted(live)}
    res['label'] = label
    res['detail'] = detail
    res['legs'] = dict(live)
    res['last_seen'] = now_iso
    #: The sweep re-observes an open incident every five seconds. Writing an
    #: unchanged one back each time is a disk write per poll per incident for
    #: no new fact; `last_seen` alone is not worth it, and its absence costs
    #: nothing that `detected_at` does not already say. A CHANGE in the legs
    #: is a different matter and is always written.
    unchanged = (store is not None
                 and (trade.get(kind.field) or {}).get('legs') == res['legs']
                 and (trade.get(kind.field) or {}).get('state') == 'open')
    if unchanged:
        return
    #: No EVENT here, deliberately. This runs on EVERY sweep pass - once per
    #: five seconds while an incident is open - and an event emitted here
    #: would report one live leg as thousands, which is the "223 degraded
    #: events" shape that makes a reader stop reading. The event is emitted
    #: where a DECISION is made instead: once at detection, and once a day by
    #: the nag, so the digest counts incident-days.
    if store is None:
        # Nothing to sweep it with. Say so, rather than implying it is tracked.
        # The in-memory record is all there is, so it is written here.
        trade[kind.field] = res
        log("  RESIDUE: no store supplied - the %s incident was ALERTED but "
            "NOT recorded, so nothing will chase it." % kind.field)
        return
    try:
        store.update_trade_fields(trade['id'], **{kind.field: res})
    except Exception as e:                     # pragma: no cover - store-level
        log('  Could not persist %s for #%s: %s'
            % (kind.field, trade.get('id'), e))
        return
    # THE RECORD SAYS WHAT THE STORE SAYS. The BCS-family stores hand out the
    # live dict rather than a copy, so assigning before the write would leave
    # an in-memory record asserting something that was never saved - and the
    # next unrelated write would then persist it.
    trade[kind.field] = res


#: Consecutive flat broker reads required before a residue incident is
#: RESOLVED. Resolution is terminal and one-way, so a single bad read must not
#: reach it. Two, at the sweep's 5-second cadence, costs ten seconds.
RESIDUE_FLAT_CONFIRMATIONS = 2


def _persist_residue_state(store, trade, res, kind=None):
    """Write the incident dict back without touching its detail/legs.

    Separate from `_persist_residue` because that one is the DETECTION writer
    and re-stamps `last_seen`, `detail` and `legs` from a live observation.
    This only moves the confirmation counter.
    """
    kind = kind or POST_CLOSE_RESIDUE
    trade[kind.field] = res
    try:
        store.update_trade_fields(trade['id'], **{kind.field: res})
    except Exception as e:                     # pragma: no cover - store-level
        log('  Could not persist %s state for #%s: %s'
            % (kind.field, trade.get('id'), e))


def _resolve_residue(store, trade, label, nagged, kind=None):
    """The book went flat. Close the incident, and say so ONCE."""
    kind = kind or POST_CLOSE_RESIDUE
    res = dict(trade.get(kind.field) or {})
    res['state'] = 'resolved'
    res['resolved_at'] = now_ist().isoformat()
    try:
        store.update_trade_fields(trade['id'], **{kind.field: res})
    except Exception as e:                     # pragma: no cover - store-level
        # The incident is NOT resolved if the record does not say so, and the
        # write is the only thing that makes it say so. Assigning first would
        # mark it resolved IN MEMORY on the very stores that hand out the live
        # dict - ending the nag on a leg whose all-clear was never saved.
        log('  Could not clear %s for #%s: %s'
            % (kind.field, trade.get('id'), e))
        return
    trade[kind.field] = res
    _recovery_event(kind.resolved_event, store=label, id=trade.get('id'))
    log("  RESIDUE #%s (%s, %s) is FLAT at the broker - incident closed."
        % (trade.get('id'), label, kind.field))
    send_telegram(
        "✅ %s #%s %s: the leftover leg is now FLAT.\n"
        "The residue reported earlier is resolved; nothing further is needed."
        % (label, trade['id'], trade.get('stock', '?')),
        alert_class=alert_policy.SAFETY)
    nagged.discard((label, trade['id'], kind.field, today_ist().isoformat()))



def sweep_reconcile_residue(kite, books, *, nagged=None,
                            kind=POST_CLOSE_RESIDUE):
    """Chase every record carrying a live leg nothing is managing.

    ONE sweep for both species (see `ResidueKind`). `kind` selects which store
    method lists the records, which field the incident lives under, and what
    the alert says; everything else -- the shared positions() read, the two
    dated flat confirmations, the settled-book gate, the daily nag, the
    per-record isolation -- is identical and is therefore not written twice.

    S3 - a booked-closed record that still has a live leg.

    **Escalate-only. This function places no orders and never will.** The
    record is terminal: there is no close lock to take, no stop to re-arm, and
    the residue may well be a leg the owner is deliberately holding. What it
    owes the owner is that the fact does not go quiet, and that it STOPS when
    the book goes flat - self-resolution read from the broker rather than from
    our own record, exactly as the M14 sweep classifies.

    `books` is `[(label, store, orders_allowed)]`, the same shape
    `recover_frozen_positions` takes. `orders_allowed` is ignored here, and
    that is the point: no value of it authorises anything.

    Returns the number of records that still carry a residue.
    """
    nagged = _RECOVERY_NAGGED if nagged is None else nagged

    pending = []
    for label, store, _orders_allowed in books:
        if store is None:
            continue
        # `hasattr`, not `except AttributeError` around the call. The M14
        # twin of this method shipped with an AttributeError INSIDE it
        # (`MemoryStore.get_frozen_trades` read a field that does not exist on
        # that class); catching the exception here would have reported that
        # bug as "an old store without the method" and moved on.
        if not hasattr(store, kind.lister):
            log('  Residue sweep: %s cannot list residues (%s: no %s)'
                % (label, kind.field, kind.lister))
            continue
        try:
            for t in getattr(store, kind.lister)():
                pending.append((label, store, t))
        except Exception as e:                 # pragma: no cover - store-level
            log('  Could not read residues (%s) from %s: %s'
                % (kind.field, label, e))
    if not pending:
        return 0

    # ONE positions read for the whole sweep, shared - the same F7 rate-limit
    # lesson the poll loop and the recovery sweep both learned expensively.
    try:
        net_by_symbol = {p['tradingsymbol']: p['quantity']
                         for p in kite.positions()['net']}
    except Exception as e:
        log('  Residue sweep: positions() failed, nothing checked: %s' % e)
        # ONCE A DAY, not once per pass. This runs every five seconds while
        # any incident is open, and a needs-human digest event at 12/min is
        # the "one live leg reads as thousands of findings" shape this same
        # change forbade for the persist path — written correctly there and
        # copied wrong here from `recovery_blind`, which has the same defect.
        key = ('RESIDUE', 'sweep', kind.blind_event, today_ist().isoformat())
        if key not in nagged:
            nagged.add(key)
            _recovery_event(kind.blind_event,
                            reason=str(e)[:60].replace(' ', '_'))
        return len(pending)

    still = 0
    for label, store, trade in pending:
        try:
            res0 = dict(trade.get(kind.field) or {})
            legs = _residue_watch(trade, kind, res0)
            if not legs:
                # It got onto this list by naming symbols; an incident that no
                # longer does cannot be checked - and must not be called
                # resolved either. Through the DAILY nag, not straight to an
                # event: this branch runs every five seconds like the rest, and
                # a per-poll event would bury the incident it is reporting.
                still += 1
                _nag_residue(label, trade,
                             'neither the incident nor the record names any '
                             'option leg, so nothing could be checked',
                             nagged, kind=kind)
                continue
            live = {sym: net_by_symbol.get(sym, 0) for sym, _s in legs}
            if not any(live.values()):
                # ── TWO CONSECUTIVE FLAT READS, NOT ONE ─────────────────
                #
                # Resolution is TERMINAL and one-way: `get_residue_trades`
                # only returns `state == 'open'`, and the only writer of a new
                # incident is `reconcile_after_close`, which runs at close
                # time on a record that is already terminal. So a single
                # successful-but-wrong `positions()` — an empty list during
                # the early-session sync window, a degraded response, a row
                # simply missing — would resolve every open incident, send a
                # green "the leftover leg is now FLAT", and permanently disarm
                # the guard for the shape that has cost real money twice.
                #
                # A RAISING positions() was already handled (`residue_blind`);
                # a LYING one was not. Everything else on this path debounces
                # or defers on a first print — value triggers are dark for 15
                # minutes, refused quotes defer, and the detection half of
                # this very function says "unknown is not flat". Resolution
                # was the one transition with no second look.
                #
                # The counter lives on the RECORD, so a restart cannot bank a
                # half-confirmation, and any live reading resets it.
                # ...AND NOT IN THE OPENING WINDOW, AND NOT ACROSS SESSIONS.
                #
                # Two reads five seconds apart do not defeat a CORRELATED
                # burst, and the sync window at the open is exactly that: at
                # 09:15:00 and 09:15:05 both reads are the same lie. Value
                # triggers are already dark for 15 minutes for this reason;
                # resolving a terminal, one-way incident is at least as
                # consequential as firing a stop, so it waits too.
                #
                # And the counter is dated. Persisting it was right — a
                # restart must not lose a confirmation — but an UNDATED
                # counter banks Friday 15:29's read against Monday 09:15's,
                # which resolves on the first read of the session and is the
                # burst problem with a weekend in the middle.
                if not is_market_settled():
                    log('  RESIDUE #%s (%s): flat, but the book is not '
                        'settled yet — not resolving in the opening window.'
                        % (trade.get('id'), label))
                    still += 1
                    continue
                res = dict(res0)
                today = today_ist().isoformat()
                seen = (int(res.get('flat_reads') or 0) + 1
                        if res.get('flat_reads_date') == today else 1)
                res['flat_reads_date'] = today
                if seen < RESIDUE_FLAT_CONFIRMATIONS:
                    log('  RESIDUE #%s (%s): reads FLAT (%d/%d) — confirming '
                        'before resolving.'
                        % (trade.get('id'), label, seen,
                           RESIDUE_FLAT_CONFIRMATIONS))
                    res['flat_reads'] = seen
                    _persist_residue_state(store, trade, res, kind=kind)
                    still += 1
                    continue
                _resolve_residue(store, trade, label, nagged, kind=kind)
                continue
            still += 1
            detail = '; '.join('%s net %+d' % (s, q)
                               for s, q in live.items() if q)
            # A live reading RESETS the confirmation counter: two flat reads
            # separated by a live one are not two consecutive flat reads.
            res = dict(res0)
            if res.get('flat_reads'):
                res['flat_reads'] = 0
                _persist_residue_state(store, trade, res, kind=kind)
            _persist_residue(store, trade, label, detail, live, kind=kind)
            _nag_residue(label, trade, detail, nagged, kind=kind)
        except Exception as e:
            # Per-record isolation, the rule the recovery sweep already
            # follows: one malformed record must not stop the sweep for the
            # rest of the book.
            log('  Residue sweep: #%s in %s raised: %s'
                % (trade.get('id'), label, e))
            still += 1
    return still


def sweep_entry_residue(kite, books, *, nagged=None):
    """Chase every leg an ENTRY left behind. Escalate-only, like its twin.

    A thin call, deliberately: the sweep is `sweep_reconcile_residue` with a
    different `ResidueKind`, and writing it twice is how the two would drift
    on the detail that matters (the two dated flat confirmations, the
    settled-book gate, the one shared positions() read).

    The name exists so the CALL SITE reads as two distinct sweeps, because
    they are two distinct facts about the book and a reader scanning
    `monitor_all` should see both.
    """
    return sweep_reconcile_residue(kite, books, nagged=nagged,
                                   kind=ENTRY_RESIDUE)


def _nag_residue(label, trade, detail, nagged, kind=None):
    """At most one Telegram a day per incident, keyed like every other nag.

    Same reasoning as `_escalate_recovery`: a safety alert nobody can silence
    is right, and one that repeats every five seconds trains the reader to
    swipe it away, which is the same thing as silencing it.

    Keyed on `kind.field`, so two species of incident on one record cannot
    silence each other.
    """
    kind = kind or POST_CLOSE_RESIDUE
    key = (label, trade['id'], kind.field, today_ist().isoformat())
    if key in nagged:
        return
    nagged.add(key)
    #: A DIFFERENT name from the detection event, deliberately. Both firing on
    #: the same day would report ONE live leg as two findings, and the digest
    #: renders these as counts a human reads as incidents. The detection event
    #: means "a leg was found"; this one means "a day it was still there".
    _recovery_event(kind.unresolved_event, store=label, id=trade['id'])
    subs = {'detected_at': (trade.get(kind.field) or {}).get('detected_at',
                                                             '?'),
            'book': label.lower(), 'id': trade['id']}
    send_telegram(
        "\U0001F6A8 %s #%s %s: %s\n%s\n%s %s"
        % (label, trade['id'], trade.get('stock', '?'), kind.headline, detail,
           kind.provenance % subs, kind.clear_hint % subs),
        alert_class=alert_policy.SAFETY)



# ── FH Close ────────────────────────────────────────────────────────────────

#: (trade id, reason, date) already told to the owner. FH triggers evaluate on
#: every five-second poll and the record STAYS OPEN afterwards — a manual close
#: is not something the monitor can complete — so without this the SL breach
#: would Telegram roughly 700 times an hour. In memory, like
#: `_unpriced_close_alerted`: the process is long-lived within a session, and a
#: new session re-alerting a still-breached FH stop is correct, not noise.
_fh_manual_alerted: dict = {}

#: The order the OWNER should work in by hand, and the reason for it. Same
#: sequence `_close_fh_inner` used to place: the naked short first, then its
#: hedge, then the put spread's short before its long. Never sell a hedge while
#: the short it covers is live.
_FH_MANUAL_SEQUENCE = (
    ('short_call_symbol', 'BUY back', 'SHORT CALL', 'naked — do this FIRST'),
    ('long_call_symbol', 'SELL', 'LONG CALL', 'only after the short call is flat'),
    ('short_put_symbol', 'BUY back', 'SHORT PUT', 'before its long put'),
    ('long_put_symbol', 'SELL', 'LONG PUT', 'last — it is the hedge'),
)


def _fh_manual_close_required(trade, spot, reason, live, dry_run):
    """FH is traded BY HAND. Say so, with the symbols and quantities, and stop.

    Returns `'MANUAL'` — a third outcome beside True/False, deliberately, so
    neither call site can read this as a close that failed. Nothing was
    attempted; there is nothing to have failed.
    """
    stock = trade.get('stock')
    #: Quantities come from `live`, i.e. from the BROKER's net position, not
    #: from the record's `quantity`. A leg partially closed by hand already
    #: must not be re-quoted at its original size.
    lines = [f"{i}. {action} {label}  {sym}  x {abs(q)}   ({why})"
             for i, (sym, q, action, label, why) in enumerate(live, 1)]

    log("")
    log("=" * 70)
    log(f"  FH {reason} TRIGGERED — {stock} spot = {spot}")
    log(f"  FALLEN HERO IS TRADED BY HAND. NO ORDER WILL BE PLACED.")
    log("=" * 70)
    for ln in lines:
        log(f"    {ln}")
    log(f"  Record the exit afterwards with the real fill prices — see "
        f"`fallen_hero.trade_store.update_trade_exit`.")

    key = (trade.get('id'), reason, now_ist().strftime('%Y-%m-%d'))
    if _fh_manual_alerted.get(key) is None:
        _fh_manual_alerted[key] = True
        send_telegram(
            f"🔴 FH {stock}: {reason} TRIGGERED @ {spot}\n"
            f"Fallen Hero is traded BY HAND — the monitor places no FH "
            f"orders. Close it yourself, in this order:\n"
            + '\n'.join(lines) + "\n"
            f"Then record the exit with the real fill prices, or the book "
            f"will keep showing this position open."
            + ("\n[DRY RUN]" if dry_run else ""),
            alert_class=alert_policy.SAFETY)
    return 'MANUAL'


def close_fh_position(kite: KiteConnect, trade: dict, spot: float,
                      reason: str, dry_run: bool):
    """**FALLEN HERO IS TRADED BY HAND. THIS PLACES NO ORDERS. EVER.**

    Owner decision, 2026-08-28: *"FH is done manually — only monitoring we
    need"*. The system's job for a Fallen Hero is to WATCH and ALERT. Triggers
    still evaluate, the SL still fires, the delivery-margin nag still runs —
    and the action is a Telegram telling the owner what to do in Kite, with the
    symbols and the quantities.

    **The refusal is structural, not a switch.** FH orders were inert before
    today only because the crontab carries `--dry-run`, which is a property of
    a flag rather than of the design: the day the exit bridge arms for BCS,
    every FH order in `_close_fh_inner` would have come along for the ride, on
    the one structure in the fleet with a NAKED SHORT CALL. So the gate is
    here, above the close lock and above every order, and it is unconditional:

        any leg live at the broker  ->  alert the owner, place nothing
        every leg already flat      ->  nothing to trade; try to BOOK it

    The second branch is the only route left into `_close_fh_inner`, and it
    lands in that function's all-legs-flat recovery, which places no orders and
    books only from ORDER_TAG fills this system itself made. It cannot reach
    Steps 1-4. Those steps are RETAINED, not deleted — ~450 historical records
    and the D1/D2/D3 regression suite are built on that structure, and deleting
    a store method's last caller is how a store method quietly loses its
    contract — but they are unreachable from the monitor and must stay that
    way. See `_close_fh_inner`'s own banner.

    NO CLOSE LOCK is taken on the flat path either. `begin_close` exists to
    stop two processes placing the same orders; with no orders it buys nothing
    and costs something real — a record parked at `'closing'` when the booking
    is then refused for want of a priceable fill, which for a hand-closed
    position (the owner's orders are UNTAGGED, so `find_recoverable_fill`
    correctly refuses them) is the NORMAL outcome, not the exception.

    THE LATE-DAY GUARD IS GONE, on purpose. It existed to stop this function
    placing an order past `HARD_ORDER_CUTOFF_TIME`; with no order to stop it
    would only have SUPPRESSED the manual alert in the last minutes of the
    session, which is when the owner most needs to hear that a stop has been
    breached and there is no automation behind it.

    Returns `'MANUAL'` when the owner must act, True when the record was
    booked, False when it could not be.
    """
    fh_store = get_fh_store()
    stock = trade['stock']

    # ── Read the book. READ-ONLY: this is the whole of the money path now. ──
    #
    # The read is guarded because it is no longer inside the try/except that
    # used to wrap `_close_fh_inner`: a dead token or a rate-limited
    # `kite.positions()` would otherwise raise out of here into the poll loop.
    # An unreadable book means we do not know whether the legs are live, and
    # the honest response to "I cannot see it" is to tell the owner the trigger
    # fired — never to conclude it is flat, which is what an empty list would
    # mean one branch further down.
    try:
        live = []
        for field, action, label, why in _FH_MANUAL_SEQUENCE:
            sym = trade.get(field)
            if not sym:
                continue                  # 3-leg FH has no long call
            qty = get_net_position(kite, sym) if not dry_run else (
                -trade['quantity'] if 'short' in field else trade['quantity'])
            if qty != 0:
                live.append((sym, qty, action, label, why))
    except Exception as e:
        log(f"  FH #{trade.get('id')} {stock}: could not read positions ({e}). "
            f"Assuming the legs are LIVE and alerting.")
        live = [(trade[f], trade.get('quantity', 0), a, lb,
                 f"{w} — QUANTITY UNVERIFIED, the position book was unreadable")
                for f, a, lb, w in _FH_MANUAL_SEQUENCE if trade.get(f)]

    if live:
        return _fh_manual_close_required(trade, spot, reason, live, dry_run)

    # ── Every leg is flat. Nothing to trade — only something to record. ────
    log("")
    log("=" * 70)
    log(f"  FH {reason} TRIGGERED! {stock} spot = {spot}")
    log(f"  Every leg is already FLAT at the broker — no order is owed. "
        f"Attempting to BOOK the exit.")
    log("=" * 70)
    try:
        return _close_fh_inner(kite, fh_store, trade, spot, reason, dry_run)
    except Exception as e:
        log(f"  EXCEPTION during close_fh_position: {e}")
        send_telegram(f"FH {stock}: EXCEPTION while booking the {reason} "
                      f"close! {e}\nNo order was placed (FH is manual). "
                      f"Record the exit by hand.",
                      alert_class=alert_policy.SAFETY)
        return False


#: fills-key -> the field an FH exit record stores that leg's price in. A
#: frozen record uses the SAME names as a booked one, so whatever reads a close
#: (a human, or M14's recovery sweep) does not need a second vocabulary for the
#: half-finished case.
_FH_FILL_FIELD = {'short_call': 'short_call_fill', 'long_call': 'long_call_fill',
                  'short_put': 'short_put_fill', 'long_put': 'long_put_fill'}


def _fh_skipped_leg(kite, fills, unpriced_skips, *, leg_key, leg_label,
                    symbol, txn, qty_seen, dry_run):
    """D4 — a leg ALREADY FLAT on arrival is not a leg that closed at 0.00.

    The close sequence skips a leg the broker says is already flat. `fills` is
    pre-seeded with `0.0`, so before this existed the skip left the seed
    standing and the summary arithmetic read it as an OBSERVED price:

        close_cost = SC + SP - LC - LP

    A skipped SHORT leg therefore contributed a buyback cost of zero, which is
    the CHEAPEST buyback that exists — so the error runs in the FLATTERING
    direction, silently, with no note on the record and `update_trade_exit`
    called as though everything had been transacted. Measured on the reported
    case: `short_put_fill 0.0`, `close_cost 7.00`, `pnl_per_share 90.75`
    against a truthful 15.00 / 82.75 — Rs 3,200 of P&L that did not exist.
    Same `0.0`-seed ambiguity as D1: skipped, failed and genuinely-zero all
    read alike. D1/D2 answered it with `closed_now`; this is the third face of
    it and reuses the same idea rather than inventing a fourth.

    Two outcomes, and only two:

      * **the price is KNOWABLE** — one of OUR ORDER_TAG orders closed that leg
        earlier this session, so `find_recoverable_fill` can supply the real
        fill. Nothing is approximate then; it is an observed price that simply
        arrived through the order book instead of through this sequence.
      * **the price is NOT knowable** — book APPROXIMATE, with a marker.
        Deliberately NOT `_refuse_unpriced_close`: that guard exists for the
        case where a price is unknown AND something needs a human. Here the
        position genuinely IS flat, nothing is at risk, and refusing would
        strand ordinary closes on a non-dangerous state. The cost of booking
        is a P&L figure known to be optimistic and SAID to be; the cost of
        refusing is a live-looking record for a position that no longer
        exists. See `feedback_guards_need_the_inverse_review`.

    An ABSENT leg (a 3-leg FH with no long call) never reaches here — the
    caller guards on the symbol. An absent hedge is worth exactly zero and must
    not share a literal with a leg that exists and could not be priced.
    """
    log(f"\n  {leg_label.upper()} {symbol} already flat (qty={qty_seen}). Skipping.")
    if dry_run:
        # No order was placed and none will be booked; reading the live order
        # book to price a rehearsal would be a real API call for a fiction.
        return
    rec = find_recoverable_fill(kite, symbol, txn)
    if rec['price'] is not None:
        fills[leg_key] = rec['price']
        log(f"    Priced from our own {txn} fill: {rec['price']} — this leg is "
            f"EXACT, not approximate.")
        return
    unpriced_skips.append((leg_key, leg_label, symbol, txn, rec))
    log(f"    No {ORDER_TAG} {txn} fill prices it. Its close cost is UNKNOWN "
        f"and is counted as 0.00 — the P&L below is APPROXIMATE and, for a "
        f"short leg, optimistic.")


def _fh_clamp_pnl(pnl_per_share, total_credit, trade, where):
    """CLAMP `pnl_per_share` to `total_credit` — a Fallen Hero cannot beat it.

    `close_cost = SC + SP - LC - LP`, and both long strikes sit further OTM
    than their shorts, so `SC >= LC` and `SP >= LP` for the same expiry:
    close_cost is non-negative and `credit - close_cost` can never exceed the
    credit. That makes `total_credit` a MATHEMATICALLY ACHIEVABLE bound (every
    leg expiring worthless reaches it), which per CLAUDE.md's "Valuation
    bounds" is a thing to CLAMP, not an estimate to refuse.

    It is applied on every booking path regardless of cause, not only where D4
    can reach: the reported number was 102.75 against a credit of 97.75, and a
    figure that is arithmetically impossible must not be reachable by any
    future route either.

    Returns `(pnl, clamped_from)` — `clamped_from` is None when the clamp did
    not bind. It is LOUD when it does. A clamp that fires silently converts a
    visible bug into a plausible number, which is the failure it exists to
    prevent, not a fix for it.
    """
    if pnl_per_share <= total_credit:
        return pnl_per_share, None
    log("")
    log("  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    log(f"  !!! IMPOSSIBLE P&L CLAMPED ({where})")
    log(f"  !!! computed {pnl_per_share:+.2f}/share on a structure whose "
        f"maximum is its credit, {total_credit:.2f}")
    log(f"  !!! booking {total_credit:.2f}. Something priced a leg wrongly — "
        f"read the fills above.")
    log("  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    logger.error(
        'FH #%s %s: pnl_per_share %.2f exceeds total_credit %.2f at %s — '
        'clamped. This is arithmetically impossible for a Fallen Hero; a leg '
        'was mispriced.', trade.get('id'), trade.get('stock'),
        pnl_per_share, total_credit, where)
    return total_credit, pnl_per_share


def _freeze_fh_leg_failure(fh_store, trade, reason, dry_run, leg_key,
                           leg_label, symbol, close_qty, closed_now,
                           remaining, residual=None, hedge_label=None,
                           kite=None, cause='unfilled'):
    """D1/D2/D3 — a leg that did NOT fully close FREEZES the record. It never books.

    Both FH long legs (Step 2's long call, Step 4's long put) used to log a
    warning, leave `fills[<leg>]` at its 0.0 seed and FALL THROUGH into the
    booking arithmetic. Two lies in one write:

      * a live long option sits at the broker under a record marked CLOSED, so
        nothing monitors it, nothing re-alerts and its stops are dead — the
        unwatched-position shape that has cost this account real money twice;
      * `close_cost = SC + SP - LC - LP` is then computed with that leg at
        0.00, which is not a missing price but the WORST price a long option
        can fetch. The booked P&L is wrong on a leg that was never sold.

    The `fh_unpriced` guard downstream catches `None` — `close_leg`'s "flat and
    nothing of ours priced it" — and structurally cannot catch this, because
    `0.0 is not None`. Same family as N1 and the `exit_value` key mismatch,
    both already fixed on the BCS path: **an unobserved price is UNKNOWN, never
    zero** ([[feedback_copy_pasted_modules_fix_once]] — this was the copy
    nobody opened).

    Mirrors `_close_spread_inner`'s `close_failed_leg='long'` freeze and its
    `partial_close` semantics exactly, including the uncomfortable part: a
    `partial_close` record drops out of `get_open_trades()` and is therefore
    NOT re-monitored. That is the honest state — a position needing a human —
    and it is what M14's recovery sweep is designed to pick up. Booking it
    closed does not make it monitored; it makes it invisible.

    Every fill this close DID observe is written onto the record. The BCS twin
    carries `short_fill` through for the same reason: after the freeze, the
    record is the only place those prices exist.

    **`residual` (D2/D3) — the PARTIAL twin of the same defect.** A leg can
    also fail by half-succeeding: `close_leg` returns `'PARTIAL'`, which passes
    every caller's success check, so the sequence walked on with part of the
    leg still live. Pass the unfilled quantity as `residual` and the record
    freezes exactly as above, plus `residual_<leg>_qty` so the next reader (a
    human, or M14's sweep) knows how much is out there. `closed_now[leg_key]`
    is then the average of the part that DID fill — a real observation of a
    partial, which is precisely why the residual quantity must sit beside it.

    `hedge_label` names the leg deliberately NOT being closed, so the alert
    cannot be read as "tidy the rest up by hand". `kite` is needed only on the
    residual path, where orders DID go out and a broker-side audit is worth
    running.
    """
    partial = residual is not None
    headline = (f"PARTIAL {leg_label.upper()} CLOSE" if partial
                else f"{leg_label.upper()} CLOSE FAILED")
    #: Buying back a short is not "selling" it, and this alert is read under
    #: stress by someone about to place an order by hand.
    action = 'buy back' if leg_key.startswith('short') else 'sell'
    live_qty = residual if partial else close_qty

    log("")
    log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    log(f"!!! {headline} — NOT BOOKING     !!!")
    log(f"!!! {symbol} x {live_qty} is STILL LIVE at the broker")
    if hedge_label:
        log(f"!!! the {hedge_label} will NOT be sold — it is the hedge")
    log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
    for k, v in closed_now.items():
        log(f"    observed this close: {k} = {v}")
    if remaining:
        log(f"    still open: {'; '.join(remaining)}")

    if not dry_run:
        extra = {_FH_FILL_FIELD[k]: v for k, v in closed_now.items()}
        if partial:
            extra[f'residual_{leg_key}_qty'] = residual
        fh_store.set_trade_status(
            trade['id'], 'partial_close',
            close_failed_leg=leg_key,
            close_reason=(f"PARTIAL_{leg_key.upper()}_{reason}" if partial
                          else reason),
            close_failure=_close_failure(cause=cause, leg=leg_key,
                                         reason=reason),
            **extra)
        if partial and kite is not None:
            reconcile_after_close(kite, trade, 'FH', store=fh_store)

    send_telegram(
        f"🔴 FH {trade.get('stock')}: {headline}\n"
        + (f"{residual} of {close_qty} qty on {symbol} is STILL LIVE at the "
           f"broker after an urgent retry.\n" if partial else
           f"{reason} triggered but {symbol} x {close_qty} did NOT {action} — "
           f"it is still LIVE at the broker.\n")
        + (f"The {hedge_label} was NOT sold — it is the hedge on that naked "
           f"short. You are over-hedged, which is the bounded side.\n"
           if hedge_label else "")
        + (f"Still open: {'; '.join(remaining)}\n" if remaining else "")
        + (f"NOTHING was booked — the P&L would have been computed on the "
           f"FULL quantity while {residual} is still live.\n" if partial else
           f"NOTHING was booked. Pricing that leg at 0.00 would record a P&L "
           f"for a sale that never happened.\n")
        + f"Trade #{trade.get('id')} is frozen at partial_close and NOT "
          f"monitored further. Close it by hand.",
        alert_class=alert_policy.SAFETY)
    return False


def _fh_residue(kite, fh_store, trade, exchange, reason, dry_run, result,
                close_qty, *, leg_key, leg_label, symbol, txn, closed_now,
                remaining_legs=(), hedge_label=None, fills=None):
    """D2/D3 — a PARTIALLY closed FH leg gets ONE urgent retry, then FREEZES.

    `'PARTIAL'` satisfies `if result['status'] not in ('COMPLETE', 'PARTIAL')`,
    so before this existed the sequence walked straight on to the next leg with
    part of this one still live at the broker.

    On a SHORT leg that is the worst state this system can reach. Step 3
    filling 300 of 400 short puts let Step 4 sell the FULL long put, leaving
    **100 naked short puts** live under a record marked `closed` (D2) — not in
    `get_open_trades()`, not in `get_frozen_trades()`, no stop, no monitor, and
    nothing that will ever look at it again. On a LONG leg the risk is bounded
    (it is a long option), but the residue is just as invisible and the booked
    P&L is computed on the FULL quantity (D3).

    **THE INVARIANT: a close sequence never leaves the book LESS hedged than it
    found it.** While short residue exists the hedge is NOT sold. Over-hedged
    is bounded, naked short is not, and we take the bounded side every time —
    the same sentence the BCS twin already states in `_close_spread_inner`.

    One retry, on the residual quantity only. `close_leg` re-reads the position
    itself and `_find_pending_orders` blocks doubling up, so this cannot become
    the Feb-2026 multiple-order shape.

    Returns True when the leg is flat and the sequence may continue; False when
    residue remains and the record has been frozen.
    """
    if result['status'] != 'PARTIAL':
        return True
    remaining = close_qty - result.get('filled_quantity', 0)
    if remaining <= 0:
        return True

    log(f"  WARNING: {leg_label} partially filled. {remaining} qty still open!")
    log(f"  RETRY: closing the {remaining} qty {leg_label} residual (urgent)")
    retry = close_leg(kite, exchange, symbol, txn, remaining,
                      is_buy=(txn == 'BUY'), dry_run=dry_run, urgent=True,
                      context=_order_ctx(trade, reason,
                                         f"{leg_key.replace('_', '-')}-retry",
                                         'FH'))
    got = retry.get('filled_quantity', 0) if retry else 0
    remaining -= got
    log(f"  RETRY filled {got}; {remaining} still open on {symbol}")
    # N13 — the retry's PRICE, not just its quantity. The caller owns `fills`
    # and `closed_now`, so both are corrected here rather than at four call
    # sites: one definition, for the same reason this function exists at all.
    if fills is not None and leg_key in fills:
        fills[leg_key] = _blend_fill(fills[leg_key],
                                     result.get('filled_quantity', 0),
                                     (retry or {}).get('average_price'), got)
        if leg_key in closed_now:
            closed_now[leg_key] = fills[leg_key]
        log(f"  {leg_label} fill after blend: {fills[leg_key]}")
    if remaining <= 0:
        return True

    return _freeze_fh_leg_failure(
        fh_store, trade, reason, dry_run, leg_key, leg_label, symbol,
        close_qty, closed_now, list(remaining_legs), residual=remaining,
        hedge_label=hedge_label, kite=kite)


def _fh_put_legs_open(sp_sym, sp_qty, lp_sym, lp_qty):
    """The put legs still live, described for a freeze alert.

    Shared by every FH freeze that lands BEFORE the put spread is closed, so
    the reader is always told what they have been handed. The one genuinely
    uncomfortable combination — a short put whose long put was already flat on
    arrival — is named out loud rather than described as an ordinary spread.
    """
    out = []
    if sp_qty < 0:
        out.append(f"SHORT PUT {sp_sym} {sp_qty:+d}"
                   + ("" if lp_qty > 0
                      else " — UNHEDGED, the long put was already flat"))
    if lp_qty > 0:
        out.append(f"LONG PUT {lp_sym} {lp_qty:+d}")
    return out


def _close_fh_inner(kite, fh_store, trade, spot, reason, dry_run):
    """Inner FH close logic, separated so close_fh_position() can wrap with try/except.

    ⚠ **STEPS 1-4 BELOW PLACE ORDERS AND ARE NO LONGER REACHABLE FROM THE
    MONITOR.** Fallen Hero is traded BY HAND (owner, 2026-08-28). Its only
    caller, `close_fh_position`, refuses any close with a leg live at the
    broker before it ever gets here, so the only branch this function can now
    reach in production is the all-legs-flat recovery below — which places
    nothing and books only from ORDER_TAG fills this system itself made.

    They are RETAINED rather than deleted, deliberately: ~450 historical
    records were closed by this exact sequence, and the D1/D2/D3 regression
    suite (`test_d1_fh_long_leg_freeze.py`, `test_d2_partial_close_residue.py`,
    `test_b11_fh_and_legstate.py`) drives this function directly to pin the
    freeze-don't-book invariants. Deleting them would delete the only executable
    statement of what an FH close is supposed to do.

    **If you are wiring a new caller into this function, do not.** Route it
    through `close_fh_position` and it will be refused, which is the answer.
    `test_the_monitor_places_no_fh_order` fails if a live-leg FH trigger ever
    reaches a `place_order`.
    """
    stock = trade['stock']
    qty = trade['quantity']
    exchange = trade['exchange']

    sc_sym = trade['short_call_symbol']
    lc_sym = trade.get('long_call_symbol')  # Optional 4th leg
    sp_sym = trade['short_put_symbol']
    lp_sym = trade['long_put_symbol']

    send_telegram(trigger_alert_text('FH', stock, reason, spot, dry_run,
                                     trade, 'Closing position...'),
                  alert_class=alert_policy.close_alert_class(
                      dry_run, _record_says_paper(trade)))

    # ── Pre-flight: Check actual position state ──────────────────────────
    if not dry_run:
        sc_qty = get_net_position(kite, sc_sym)
        lc_qty = get_net_position(kite, lc_sym) if lc_sym else 0
        sp_qty = get_net_position(kite, sp_sym)
        lp_qty = get_net_position(kite, lp_sym)
    else:
        sc_qty = -qty
        lc_qty = qty if lc_sym else 0
        sp_qty = -qty
        lp_qty = qty

    log(f"  Positions: SC({sc_sym})={sc_qty} | LC({lc_sym or 'N/A'})={lc_qty} | SP({sp_sym})={sp_qty} | LP({lp_sym})={lp_qty}")

    # ── B11 · Guard: a FLIPPED leg is not a flat leg (FH twin) ───────────
    #
    # Same defect as `_close_spread_inner`, on all FOUR legs: `sc_qty >= 0`
    # is satisfied by a short call that has flipped LONG. FH is the more
    # dangerous case — the short call is the naked leg, so a flip there is an
    # uncovered position the monitor would mark closed and stop watching.
    fh_flipped = []
    if sc_qty > 0:
        fh_flipped.append(f"SHORT CALL {sc_sym} is LONG {sc_qty:+d}")
    if sp_qty > 0:
        fh_flipped.append(f"SHORT PUT {sp_sym} is LONG {sp_qty:+d}")
    if lp_qty < 0:
        fh_flipped.append(f"LONG PUT {lp_sym} is SHORT {lp_qty:+d}")
    if lc_sym and lc_qty < 0:
        fh_flipped.append(f"LONG CALL {lc_sym} is SHORT {lc_qty:+d}")
    if fh_flipped:
        detail = '; '.join(fh_flipped)
        log(f"\n  *** FLIPPED POSITION — NO ORDERS WILL BE PLACED ***")
        log(f"  {detail}")
        if not dry_run:
            fh_store.set_trade_status(
                trade['id'], 'partial_close',
                close_reason=f"FLIPPED_{reason}", flipped=detail,
                sc_qty_seen=sc_qty, lc_qty_seen=lc_qty,
                sp_qty_seen=sp_qty, lp_qty_seen=lp_qty,
                close_failure=_close_failure(
                    cause='flipped', leg=None, reason=reason))
            reconcile_after_close(kite, trade, 'FH', store=fh_store)
        send_telegram(
            f"🔴 FH {stock}: FLIPPED POSITION\n"
            f"{reason} triggered, but the book is not what the trade says:\n"
            f"{detail}\n"
            f"No orders placed. The trade is frozen at partial_close and "
            f"will NOT be monitored further.\n"
            f"An FH short leg is NAKED — check Kite now.", alert_class=alert_policy.SAFETY)
        return False

    # ── Guard: All legs already flat ─────────────────────────────────────
    # Strictly zero on every leg. Anything else is handled above.
    all_flat = (sc_qty == 0 and sp_qty == 0 and lp_qty == 0
                and (not lc_sym or lc_qty == 0))
    if all_flat:
        log(f"\n  All legs already flat. Recovering fill prices...")
        # The FH twin of N1, fixed in the same change as its BCS original.
        # [[feedback_copy_pasted_modules_fix_once]]: the last two times a rule
        # was fixed on one of these two paths and not the other, the untested
        # twin shipped broken. Four legs instead of two, and the same rule —
        # every leg priced by one of OUR fills, or nothing is booked.
        fh_recs = [('short call', sc_sym, 'BUY',
                    find_recoverable_fill(kite, sc_sym, "BUY")),
                   ('short put', sp_sym, 'BUY',
                    find_recoverable_fill(kite, sp_sym, "BUY")),
                   ('long put', lp_sym, 'SELL',
                    find_recoverable_fill(kite, lp_sym, "SELL"))]
        if lc_sym:
            fh_recs.append(('long call', lc_sym, 'SELL',
                            find_recoverable_fill(kite, lc_sym, "SELL")))
        if any(r['price'] is None for _, _, _, r in fh_recs):
            return _refuse_unpriced_close(trade, 'FH', reason, spot,
                                          dry_run, fh_recs)
        prices = {side: r['price'] for side, _, _, r in fh_recs}
        sc_fill = prices['short call']
        sp_fill = prices['short put']
        lp_fill = prices['long put']
        # An ABSENT hedge leg is worth zero; a leg that exists and cannot be
        # priced was refused above. The two must not share a literal.
        lc_fill = prices['long call'] if lc_sym else 0.0
        close_cost = sc_fill + sp_fill - lc_fill - lp_fill
        total_credit = trade['total_credit']
        pnl_per_share = total_credit - close_cost
        # Every leg here is priced by one of OUR fills or the close was
        # refused above, so the clamp cannot bind on any input this branch
        # accepts. It is applied anyway: `_fh_clamp_pnl`'s whole point is that
        # an impossible number must be unreachable by ANY route, including one
        # that does not exist yet. It is loud, so a bind is a bug report.
        pnl_per_share, clamped_from = _fh_clamp_pnl(
            pnl_per_share, total_credit, trade, 'already-flat recovery')
        total_pnl = pnl_per_share * qty
        exit_data = {
            'exit_date': now_ist().isoformat(),
            'exit_reason': f"ALREADY_FLAT_{reason}",
            'exit_spot': spot,
            'short_call_fill': sc_fill, 'long_call_fill': lc_fill,
            'short_put_fill': sp_fill, 'long_put_fill': lp_fill,
            'close_cost': close_cost,
            'pnl_per_share': pnl_per_share, 'total_pnl': total_pnl,
            'notes': 'All legs flat when close triggered. Fills recovered from order history.',
        }
        if clamped_from is not None:
            exit_data['pnl_clamped_from'] = clamped_from
            exit_data['pnl_approximate'] = True
        if not dry_run:
            fh_store.update_trade_exit(trade['id'], exit_data)
        send_telegram(f"FH {stock}: {reason} but all legs already flat. Marked closed.")
        return True

    fills = {'short_call': 0.0, 'long_call': 0.0, 'short_put': 0.0, 'long_put': 0.0}

    #: What THIS close actually traded, and at what price. `fills` cannot say:
    #: it is pre-seeded with 0.0, so a leg that was skipped (already flat), a
    #: leg that failed, and a leg that genuinely filled at zero all read the
    #: same. D1 turned on exactly that ambiguity, so the freeze paths below
    #: read this instead of guessing from `fills`.
    closed_now = {}

    #: D4 — legs ALREADY FLAT on arrival that no ORDER_TAG fill of ours could
    #: price. Their close cost is genuinely unknown; the arithmetic below
    #: counts it as 0.00 and the exit is stamped APPROXIMATE so no reader can
    #: present it as exact. `[(leg_key, leg_label, symbol, txn, recovery)]`.
    unpriced_skips: list = []

    # ── Step 1: BUY back short call (naked risk — MOST DANGEROUS) ────────
    if sc_qty < 0:
        close_qty = min(abs(sc_qty), qty)
        log(f"\nSTEP 1: BUY back SHORT CALL {sc_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, sc_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run,
                           urgent=True,
                           context=_order_ctx(trade, reason, 'short-call', 'FH'))
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log("!!! CRITICAL: SHORT CALL CLOSE FAILED — naked risk remains !!!")
            send_telegram(f"FH {stock}: SHORT CALL CLOSE FAILED! Naked risk! Manual intervention needed!", alert_class=alert_policy.SAFETY)
            if not dry_run:
                fh_store.set_trade_status(
                    trade['id'], 'partial_close',
                    close_failed_leg='short_call', close_reason=reason,
                    close_failure=_close_failure(
                        cause=_close_failure_cause(result),
                        leg='short_call', reason=reason))
            return False
        fills['short_call'] = result.get('average_price', 0)
        closed_now['short_call'] = fills['short_call']

        # ── B10 (FH twin) · the long call is the short call's HEDGE ────────
        #
        # 'PARTIAL' passes the check above. Falling through to Step 2 sells the
        # long call in full while part of the short call is still open — i.e.
        # it REMOVES THE HEDGE FROM A NAKED SHORT. That is the worst version of
        # the B10 shape anywhere in this file.
        #
        # This used to be an inline copy of the block Step 3 did NOT have (D2).
        # One definition now serves all four legs — the copy nobody opened is
        # how the short PUT went uncovered for as long as it did.
        if not _fh_residue(kite, fh_store, trade, exchange, reason, dry_run,
                           result, close_qty, leg_key='short_call',
                           leg_label='short call', symbol=sc_sym, txn='BUY',
                           closed_now=closed_now, fills=fills,
                           hedge_label='long call',
                           remaining_legs=_fh_put_legs_open(
                               sp_sym, sp_qty, lp_sym, lp_qty)):
            return False
    else:
        _fh_skipped_leg(kite, fills, unpriced_skips, leg_key='short_call',
                        leg_label='short call', symbol=sc_sym, txn='BUY',
                        qty_seen=sc_qty, dry_run=dry_run)

    # ── Step 2: SELL long call (if exists — hedge, no longer needed) ─────
    if lc_sym and lc_qty > 0:
        close_qty = min(lc_qty, qty)
        log(f"\nSTEP 2: SELL LONG CALL {lc_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, lc_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run,
                           urgent=True,
                           context=_order_ctx(trade, reason, 'long-call', 'FH'))
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            # D1. This used to warn and FALL THROUGH, booking the trade closed
            # with `fills['long_call']` still at 0.0. See
            # `_freeze_fh_leg_failure`. Freezing here leaves the short put open
            # against the long put — the ORIGINAL bull put spread, bounded by
            # its width — plus an unsold long call, which is bounded by its own
            # premium. No new risk is created by stopping, and continuing would
            # mean placing further orders into a book that has just refused
            # one. The one shape worth knowing about is named in the alert:
            # `_fh_put_legs_open` reports the short put if it is still open,
            # and says UNHEDGED when its long put was already flat.
            return _freeze_fh_leg_failure(
                fh_store, trade, reason, dry_run, 'long_call', 'long call',
                lc_sym, close_qty, closed_now,
                _fh_put_legs_open(sp_sym, sp_qty, lp_sym, lp_qty),
                cause=_close_failure_cause(result))
        fills['long_call'] = result.get('average_price', 0)
        closed_now['long_call'] = fills['long_call']
        # D3 — a PARTIAL sale leaves long-call residue live under what would
        # otherwise become a CLOSED record, with the P&L computed on the full
        # quantity. Bounded risk, but the same invisibility as D2. Stopping is
        # safe: the put spread is left intact and bounded by its width.
        if not _fh_residue(kite, fh_store, trade, exchange, reason, dry_run,
                           result, close_qty, leg_key='long_call',
                           leg_label='long call', symbol=lc_sym, txn='SELL',
                           closed_now=closed_now, fills=fills,
                           remaining_legs=_fh_put_legs_open(
                               sp_sym, sp_qty, lp_sym, lp_qty)):
            return False
    elif lc_sym:
        # An ABSENT long call is worth exactly zero and is NOT unpriced; only a
        # leg that EXISTS and was flat on arrival can be unknown. The two must
        # never share a literal — the all-flat branch above says the same.
        _fh_skipped_leg(kite, fills, unpriced_skips, leg_key='long_call',
                        leg_label='long call', symbol=lc_sym, txn='SELL',
                        qty_seen=lc_qty, dry_run=dry_run)

    # ── Step 3: BUY back short put ───────────────────────────────────────
    if sp_qty < 0:
        close_qty = min(abs(sp_qty), qty)
        log(f"\nSTEP 3: BUY back SHORT PUT {sp_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, sp_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run,
                           urgent=True,
                           context=_order_ctx(trade, reason, 'short-put', 'FH'))
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log("!!! WARNING: SHORT PUT CLOSE FAILED !!!")
            send_telegram(f"FH {stock}: SHORT PUT CLOSE FAILED! Manual intervention needed!", alert_class=alert_policy.SAFETY)
            if not dry_run:
                fh_store.set_trade_status(
                    trade['id'], 'partial_close',
                    close_failed_leg='short_put', close_reason=reason,
                    close_failure=_close_failure(
                        cause=_close_failure_cause(result),
                        leg='short_put', reason=reason))
            return False
        fills['short_put'] = result.get('average_price', 0)
        closed_now['short_put'] = fills['short_put']

        # ── D2 · THE LONG PUT IS THE SHORT PUT'S HEDGE ─────────────────────
        #
        # This is the defect that made a naked short under a CLOSED record
        # reachable. 'PARTIAL' passed the check above with nothing behind it —
        # Step 1 had the B10 retry-then-freeze and Step 3 had none — so a short
        # put filling 300 of 400 fell through and Step 4 sold the FULL long
        # put: 100 NAKED SHORT PUTS live, `status: 'closed'`,
        # `update_trade_exit` called, and no engine that will ever look at it
        # again. Same guard, same helper, same invariant as Step 1.
        if not _fh_residue(kite, fh_store, trade, exchange, reason, dry_run,
                           result, close_qty, leg_key='short_put',
                           leg_label='short put', symbol=sp_sym, txn='BUY',
                           closed_now=closed_now, fills=fills,
                           hedge_label='long put'):
            # No `remaining_legs` here, deliberately: `sp_qty` is the
            # PRE-close reading, so listing it would tell the reader 400 are
            # still short one line after saying 100 are. The residual line and
            # the hedge line together say everything true about this state.
            return False
    else:
        _fh_skipped_leg(kite, fills, unpriced_skips, leg_key='short_put',
                        leg_label='short put', symbol=sp_sym, txn='BUY',
                        qty_seen=sp_qty, dry_run=dry_run)

    # ── Step 4: SELL long put ────────────────────────────────────────────
    if lp_qty > 0:
        close_qty = min(lp_qty, qty)
        log(f"\nSTEP 4: SELL LONG PUT {lp_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, lp_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run,
                           urgent=True,
                           context=_order_ctx(trade, reason, 'long-put', 'FH'))
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            # D1, the leg the defect was reported against. Every short leg is
            # flat by the time we get here, so nothing is naked — but the long
            # put is LIVE, and booking it at 0.00 records a loss on a sale that
            # never happened. See `_freeze_fh_leg_failure`.
            return _freeze_fh_leg_failure(
                fh_store, trade, reason, dry_run, 'long_put', 'long put',
                lp_sym, close_qty, closed_now, [],
                cause=_close_failure_cause(result))
        fills['long_put'] = result.get('average_price', 0)
        closed_now['long_put'] = fills['long_put']
        # D3, on the last leg: a PARTIAL sale leaves long-put residue live
        # under a record that would otherwise book CLOSED, with the P&L
        # computed on the full quantity. Every short leg is flat by now, so
        # nothing is naked — but nothing would watch the residue either.
        if not _fh_residue(kite, fh_store, trade, exchange, reason, dry_run,
                           result, close_qty, leg_key='long_put',
                           leg_label='long put', symbol=lp_sym, txn='SELL',
                           closed_now=closed_now, fills=fills):
            return False
    else:
        _fh_skipped_leg(kite, fills, unpriced_skips, leg_key='long_put',
                        leg_label='long put', symbol=lp_sym, txn='SELL',
                        qty_seen=lp_qty, dry_run=dry_run)

    # The FH twin of the unpriced-leg guard on the BCS path. `close_leg` can
    # report a leg CLOSED with `average_price: None` when it found the leg
    # already flat and nothing of ours priced it; here that would not even
    # produce a wrong number, it would raise inside the arithmetic below and
    # land in close_spread's blanket handler as "EXCEPTION during close" — a
    # true statement about the wrong thing.
    fh_unpriced = [(side, sym, txn,
                    {'price': None, 'untagged': 0, 'error': None})
                   for side, sym, txn, key in (
                       ('short call', sc_sym, 'BUY', 'short_call'),
                       ('long call', lc_sym, 'SELL', 'long_call'),
                       ('short put', sp_sym, 'BUY', 'short_put'),
                       ('long put', lp_sym, 'SELL', 'long_put'))
                   if fills[key] is None]
    if fh_unpriced:
        return _refuse_unpriced_close(trade, 'FH', reason, spot, dry_run,
                                      fh_unpriced)

    # ── Summary ──────────────────────────────────────────────────────────
    total_credit = trade['total_credit']
    close_cost = fills['short_call'] + fills['short_put'] - fills['long_call'] - fills['long_put']
    pnl_per_share = total_credit - close_cost

    # D4 — an unpriced skipped leg contributes 0.00 above. That is not a
    # price, it is a hole, and it runs one way per side: a SHORT leg counted
    # at zero is the cheapest buyback there is, so the P&L is optimistic. Say
    # it, on the record and in the alert, and never let a reader downstream
    # meet the number without the caveat.
    approx_notes = []
    if unpriced_skips:
        log("")
        log("  *** P&L IS APPROXIMATE — a leg could not be priced ***")
        for leg_key, leg_label, symbol, txn, rec in unpriced_skips:
            side = 'optimistic' if leg_key.startswith('short') else 'conservative'
            why = ('the order book was unreadable' if rec.get('error')
                   else f"{rec['untagged']} untagged {txn} order(s), none ours"
                   if rec.get('untagged') else f"no {ORDER_TAG} {txn} fill today")
            log(f"    {leg_label} {symbol}: already flat, {why}. "
                f"Counted as 0.00 — {side}.")
            approx_notes.append(f"{leg_label} ({why})")

    pnl_per_share, clamped_from = _fh_clamp_pnl(
        pnl_per_share, total_credit, trade, 'close summary')
    total_pnl = pnl_per_share * qty

    log("")
    log("=" * 70)
    log(f"  FH POSITION {'WOULD CLOSE [DRY RUN]' if dry_run else 'CLOSED'}")
    log("=" * 70)
    log(f"  Reason:              {reason}")
    if dry_run:
        # Same dry stub, same fabricated arithmetic — and here it reads as the
        # FULL credit banked rather than a max loss, i.e. wrong in the
        # flattering direction. See `close_alert_text`.
        log( "  Fills:               none — no order was placed")
        log(f"  Entry credit/share:  {total_credit:.2f}")
        log( "  P&L:                 not computed (dry run has no fills)")
    else:
        unknown = {k for k, *_ in unpriced_skips}

        def _f(key):
            return ('UNKNOWN (counted 0.00)' if key in unknown
                    else f"{fills[key]:.2f}")
        log(f"  Short Call fill:     {_f('short_call')} (BUY back)")
        log(f"  Long Call fill:      {_f('long_call')} (SELL)")
        log(f"  Short Put fill:      {_f('short_put')} (BUY back)")
        log(f"  Long Put fill:       {_f('long_put')} (SELL)")
        log(f"  Close cost/share:    {close_cost:.2f}"
            + ('  [APPROXIMATE]' if unpriced_skips else ''))
        log(f"  Entry credit/share:  {total_credit:.2f}")
        log(f"  P&L per share:       {pnl_per_share:+.2f}"
            + ('  [APPROXIMATE]' if unpriced_skips or clamped_from else ''))
        log(f"  Total P&L:           Rs {total_pnl:+,.0f}")
    log(f"  Spot at close:       {spot}")
    log("=" * 70)

    _fh_lines = ([f"Credit: {total_credit:.2f} | Close cost: n/a (no order placed)"]
                 if dry_run
                 else [f"Credit: {total_credit:.2f} | Close cost: {close_cost:.2f}"])
    if unpriced_skips and not dry_run:
        _fh_lines.append(
            "P&L is APPROXIMATE — " + ', '.join(l for _, l, *_ in unpriced_skips)
            + " was already flat and could not be priced (counted at 0.00).")
    if clamped_from is not None:
        _fh_lines.append(
            f"P&L was CLAMPED from {clamped_from:+.2f} to the structure's "
            f"maximum {total_credit:.2f}/share — a leg was mispriced.")
    send_telegram(close_alert_text(
        'FH', stock, reason, spot, _fh_lines,
        total_pnl, dry_run, trade=trade),
        alert_class=alert_policy.close_alert_class(
            dry_run, _record_says_paper(trade)))

    # An unpriced leg is recorded as UNKNOWN, never as 0.00. The arithmetic
    # above had to put a number somewhere; the RECORD must not, or the next
    # reader meets a price nobody observed and has no way to tell.
    recorded = {field: fills[key] for key, field in _FH_FILL_FIELD.items()}
    for leg_key, *_ in unpriced_skips:
        recorded[_FH_FILL_FIELD[leg_key]] = None

    exit_data = {
        'exit_date': now_ist().isoformat(),
        'exit_reason': reason,
        'exit_spot': spot,
        **recorded,
        'close_cost': close_cost,
        'pnl_per_share': pnl_per_share,
        'total_pnl': total_pnl,
    }
    if unpriced_skips:
        exit_data['pnl_approximate'] = True
        exit_data['unpriced_legs'] = [k for k, *_ in unpriced_skips]
        exit_data['notes'] = (
            'P&L is APPROXIMATE: ' + '; '.join(approx_notes)
            + '. Each was already flat on arrival and is counted at 0.00.')
    if clamped_from is not None:
        exit_data['pnl_clamped_from'] = clamped_from
        exit_data['pnl_approximate'] = True

    if not dry_run:
        fh_store.update_trade_exit(trade['id'], exit_data)
        log(f"  Trade #{trade['id']} marked closed (local + Drive)")
    else:
        log(f"  [DRY RUN] Would mark trade #{trade['id']} closed")

    return True


# ── Position Verification ────────────────────────────────────────────────────

def verify_positions(kite: KiteConnect, trade: dict, fatal: bool = True):
    """Confirm both legs exist in Kite before starting monitor.

    Args:
        fatal: If True (default, single-trade mode), sys.exit on missing.
               If False (cron mode), return None and let caller handle.

    A paper record is exempt: it has no legs at any broker by definition, so
    `fatal=True` here would `sys.exit(1)` on a perfectly healthy cohort trade.
    Same predicate and same reasoning as the cron startup sweep — this is the
    copy of that check, and fixing only the one you opened is the most
    repeated defect in this repo.
    """
    if _record_says_paper(trade):
        log(f"  #{trade.get('id')} {trade.get('stock')} — PAPER record, no "
            f"broker legs expected (position check does not apply)")
        return
    positions = kite.positions()['net']

    long_pos = None
    short_pos = None
    for p in positions:
        if p['tradingsymbol'] == trade['long_symbol'] and p['quantity'] > 0:
            long_pos = p
        elif p['tradingsymbol'] == trade['short_symbol'] and p['quantity'] < 0:
            short_pos = p

    if not long_pos:
        msg = f"Long position {trade['long_symbol']} not found in Kite!"
        if fatal:
            log(f"FATAL: {msg}")
            sys.exit(1)
        else:
            log(f"  WARNING: {msg}")
    if not short_pos:
        msg = f"Short position {trade['short_symbol']} not found in Kite!"
        if fatal:
            log(f"FATAL: {msg}")
            sys.exit(1)
        else:
            log(f"  WARNING: {msg}")

    if long_pos and short_pos:
        log(f"  Long:  {trade['long_symbol']}  qty={long_pos['quantity']}  avg={long_pos['average_price']}")
        log(f"  Short: {trade['short_symbol']}  qty={short_pos['quantity']}  avg={short_pos['average_price']}")

    return long_pos, short_pos


# ── Trigger-Confirmation / Trail / Blind-Mode Helpers (2026-07-24) ──────────

def _gain_anchored_levels(trade: dict, peak: float):
    """zebra's trail arithmetic, or None if it cannot be computed.

    Delegates to `zebra.mfe.trail_levels` rather than restating the formula.
    That function is the single definition of this rule — it carries the
    reasoning about arming off the PEAK rather than the live gain, and the
    warning that `level` bounds the trigger and not the fill. A second copy
    here would be one more thing to keep in step, which is exactly how the
    intrinsic floor ended up call-only (B21).

    Imported lazily: `zebra/strikes.py` already imports from this module, so a
    module-level import in the other direction would close the cycle.
    """
    try:
        from zebra import mfe as _mfe
    except Exception:
        return None
    width = trade.get('spread_width', trade.get('width'))
    debit = trade.get('net_debit', trade.get('debit'))
    if width is None or debit is None:
        return None
    lv = _mfe.trail_levels({'width': float(width), 'debit': float(debit),
                            'mfe_mid': float(peak)})
    if lv is None:
        return None
    lv['engage_level'] = round(float(debit) + lv['engage_gain'], 2)
    return lv


def _tp_latch_mod():
    """`zebra.trade_store`'s latch vocabulary, or None if it cannot be reached.

    Lazily imported for the reason `_gain_anchored_levels` documents:
    `zebra/strikes.py` imports from this module, and a module-level import in
    the other direction is one edge away from closing that cycle.

    None on failure, and every caller degrades to the LIVE comparison alone —
    i.e. exactly the behaviour this engine had before the latch existed. A
    broken import must not be able to disarm a take-profit.
    """
    try:
        from zebra import trade_store as _ts
        return _ts
    except Exception as e:                      # pragma: no cover - import guard
        log(f"  WARNING: TP latch unavailable ({e}) — take-profits fall back "
            f"to the live spot comparison only")
        return None


def tp_armed(trade: dict, hit_now: bool, spot: float, store=None,
             dry_run: bool = False, label: str = 'BCS') -> bool:
    """May the take-profit fire — now, or on a touch seen in an earlier poll?

    THE DECISION IS NOT MADE HERE. It is `zebra.trade_store.tp_latch`, which
    `zebra/monitor.py` calls too, because a latch honoured by one engine and
    not the other is this codebase's most repeated defect and both engines can
    hold the same cohort record. This function is the WIRING: it supplies this
    engine's own live comparison and owns the persistence decision below.

    WHO MAY WRITE THE LATCH. Only the engine that would actually place the
    order for this record:

    * `dry_run` writes nothing. Dry run means "monitor everything, change
      nothing", and while the monitor is in dry run zebra is still the engine
      that books these exits — and it latches them itself. A dry run mutating
      the live cohort record would be this engine arming a trigger it is not
      allowed to pull.
    * a PAPER record writes nothing, for the same reason `close_spread`
      refuses one outright: no broker ever saw it, zebra owns it, and zebra
      does the arming.

    Both still HONOUR an existing latch, which is the half that matters for
    the dry-run evidence week: what gets journalled must be what an armed
    engine would have done.
    """
    ts = _tp_latch_mod()
    if ts is None:
        return bool(hit_now)
    # `now_ist()` from THIS module, not the store's own default: the
    # timestamp and the touch->fill measurement below must be read off one
    # clock, and this is the one the engine (and the replay harness) runs on.
    latch = ts.tp_latch(trade, hit_now, spot, now=now_ist())
    tid, stock = trade.get('id'), trade.get('stock')
    if latch['patch']:
        if dry_run or _record_says_paper(trade):
            log(f"  {label} #{tid} {stock}: TP touched at {spot} — latch NOT "
                f"written ({'dry run' if dry_run else 'paper record'}); the "
                f"engine that owns this record's exits arms it.")
        elif store is None:
            log(f"  {label} #{tid} {stock}: TP touched at {spot} but no store "
                f"to persist the latch — this trigger can still evaporate.")
        else:
            try:
                store.update_trade_fields(tid, **latch['patch'])
                trade.update(latch['patch'])
                log(f"  {label} #{tid} {stock}: TP LATCHED at spot {spot}. "
                    f"This exit is armed permanently now, wherever spot goes.")
            except Exception as e:
                # The close still goes ahead this poll (hit_now is true), so
                # nothing is lost yet. What is lost is the arming if it does
                # not fill — a silent reversion to pre-latch behaviour.
                log(f"  {label} #{tid} {stock}: TP LATCH WRITE FAILED ({e}) — "
                    f"the touch is not persisted and this trigger can still "
                    f"evaporate.")
    # No per-poll line for "still armed": this loop polls every 5 seconds and
    # an armed exit can sit through a 300s abort cooldown, so saying it here
    # would print sixty identical lines and train the reader to skim. The
    # ACTION is logged by the caller, and `_tp_latch_tag` puts the state on the
    # 30-second status line so an armed position is never indistinguishable
    # from a quiet one.
    return latch['armed']


def _tp_latch_tag(trade: dict) -> str:
    """' [TP-LATCHED]' for a position whose take-profit is already armed.

    Empty for FH and for anything untouched — nothing outside the two vertical
    books can carry the field.
    """
    ts = _tp_latch_mod()
    return ' [TP-LATCHED]' if ts is not None and ts.tp_latched(trade) else ''


def stamp_tp_touch_to_fill(trade: dict, store, exit_spot: float,
                           rising: bool, dry_run: bool = False,
                           label: str = 'BCS') -> None:
    """Record what the touch-to-fill lag cost, on the trade it cost it on.

    Called AFTER a take-profit has actually booked, and wrapped whole: an exit
    that happened must never be endangered by an accounting stamp. The booking
    price is the one the close transacted at — this measures the SPOT
    give-back between the touch and the fill, it does not adjust anything
    (`feedback_trigger_is_not_the_fill`).
    """
    if dry_run or store is None or _record_says_paper(trade):
        return
    ts = _tp_latch_mod()
    if ts is None:
        return
    try:
        gap = ts.tp_touch_to_fill(trade, exit_spot, rising=rising,
                                  now=now_ist())
        if gap:
            store.update_trade_fields(trade.get('id'), **gap)
            log(f"  {label} #{trade.get('id')} {trade.get('stock')}: TP "
                f"touch->fill {gap.get('tp_touch_to_exit_sec')}s, spot moved "
                f"{gap.get('tp_touch_spot_move')} "
                f"({gap.get('tp_touch_spot_move_pct')}%) from the touch.")
    except Exception as e:
        log(f"  WARNING: TP touch->fill stamp failed for #{trade.get('id')}: "
            f"{e} — the exit IS booked; only the measurement is missing.")


def trail_engage_level(trade: dict) -> float:
    """Spread value at which the trail arms, per THIS trade's policy.

    Two rules, and they are not the same rule with different numbers:

    * `debit_anchored` (the three original books) arms at 2x the entry debit.
      What that means varies wildly with the spread — 43% of max gain at 30%
      debit/width, 82% at 45%.
    * `gain_anchored` (the BCS cohort) arms when the peak has taken half of
      MAX gain, so the trigger means the same thing across spreads. The two
      cross at d/w = 1/3, and 34 of 42 records sit above it, so this one arms
      earlier on roughly 80% of that book.

    Falls back to the debit anchor whenever the gain anchor cannot be computed
    (missing width, or a debit at or above width). Falling back to the EXISTING
    behaviour is the conservative direction: it can only delay the trail, never
    arm one early.
    """
    if trade.get('trail_policy') == 'gain_anchored':
        lv = _gain_anchored_levels(trade, float(trade.get('net_debit') or 0.0))
        if lv is not None:
            return lv['engage_level']
    return float(trade['net_debit']) * TRAIL_ENGAGE_MULTIPLIER


def trail_level_for(trade: dict, peak: float) -> float:
    """Where the trail sits, given an accepted peak, per this trade's policy.

    `debit_anchored` gives back 40% of the PEAK VALUE; `gain_anchored` gives
    back half the peak GAIN, which is strictly above the entry debit while the
    position is up. Neither is a promise about the fill — the trigger is
    `value <= level` and the booking price is the value, so a gap through the
    level books wherever it landed.
    """
    if trade.get('trail_policy') == 'gain_anchored':
        lv = _gain_anchored_levels(trade, peak)
        if lv is not None:
            return lv['level']
    return peak * TRAIL_PERCENT


def restored_peak(trade: dict) -> float:
    """The highest peak ANY engine has recorded for this position.

    Two engines write a peak for a cohort record and they write it under
    different names. This one persists `trail_peak`; `zebra/monitor.py`
    persists `mfe_mid`, and it keeps persisting it while it is stood down --
    measurement is not an exit decision, so the peak accrues whoever is
    holding the trigger.

    The arming plan explicitly contemplates ROLLBACK (`exits_managed_externally`
    back to false, `--dry-run` back on, then forward again later). Across that
    cycle the two names diverge: everything zebra saw while it held the exits
    lives only in `mfe_mid`, so on the way back this engine would resume from
    its own stale `trail_peak` -- a LOWER peak, which pushes the trail level
    down and gives back more than the rule says. A supported toggle with an
    unsupported state transfer.

    MAX, not "prefer zebra's". Neither is authoritative: this engine polls
    every 5 seconds and zebra every 5 minutes, so this one sees more; zebra's
    jump gate needs `MFE_CONFIRM_POLLS` agreeing reads, so it accepts less. A
    peak is a maximum, and the union of two partial observations of a maximum
    is their maximum.

    The three BCS-family books carry no `mfe_mid`, so this is `trail_peak`
    unchanged for them.
    """
    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0
    return max(_f(trade.get('trail_peak')), _f(trade.get('mfe_mid')))


def new_trail_state(trade: dict) -> dict:
    """Trail state dict, restored from persisted trade fields.

    The peak is reconciled across both engines' names (`restored_peak`), and
    the ARMED flag and level are re-derived from it rather than read back.
    Restoring a higher peak without them would be worse than not restoring it
    at all: the trail would sit at a level computed for the old peak, or -- in
    the give-back case the trail exists for -- stay disarmed at
    `trail_active: False` with a peak that has long since passed the engage
    level, because `update_trail` only arms on a LIVE reading above it.
    """
    peak = restored_peak(trade)
    engage = trail_engage_level(trade)
    active = bool(trade.get('trail_active', False))
    trail = float(trade.get('trail_sl') or 0.0)
    if not active and peak >= engage and peak > 0:
        active = True
    if active:
        # Never DOWN. The stored level was computed from a peak this one is
        # at least equal to, and a trail that loosens on a restart is a stop
        # moving away from the position while it is open.
        trail = max(trail, trail_level_for(trade, peak))
    return {
        'peak': peak,
        'trail': trail,
        'active': active,
        'engage_level': engage,
        # The trail RULE rides on the state dict, not just its engage level.
        # `update_trail` computes where the trail sits every time the peak
        # moves, and it is called from two places that have the state but not
        # the trade. Carrying the three fields the rule needs beats threading
        # the trade through both call sites, and keeps the state dict the one
        # thing that describes this position's trail.
        'policy': trade.get('trail_policy'),
        'net_debit': float(trade['net_debit']),
        'spread_width': trade.get('spread_width', trade.get('width')),
        'cand_count': 0,     # jump-gate candidate confirmation counter
        'cand_min': 0.0,     # minimum spread seen across the candidate window
    }


def update_trail(ts: dict, spread_val: float) -> bool:
    """Apply trail engage/peak logic with the jump-plausibility gate.

    Mutates ts. Returns True when peak/trail changed and should be persisted.

    A proposed peak more than TRAIL_PEAK_JUMP_MULT above the baseline
    (max(current peak, engage level)) is implausible for a vertical spread
    within one poll — it must be seen on SL_CONFIRM_POLLS consecutive
    reliable polls, and the window MINIMUM is what gets persisted. One
    garbage-high bid can no longer poison trail state in the store/Drive
    and fire a delayed false SL_TRAIL. (2026-07-24 guard design.)
    """
    if not ts['active'] and spread_val < ts['engage_level']:
        ts['cand_count'] = 0
        return False
    if ts['active'] and spread_val <= ts['peak']:
        ts['cand_count'] = 0
        return False

    baseline = max(ts['peak'], ts['engage_level'])
    if spread_val <= baseline * TRAIL_PEAK_JUMP_MULT:
        accepted = spread_val
        ts['cand_count'] = 0
    else:
        # Stale candidate window restarts (same rule as bump_confirm):
        # confirming polls must be reasonably contiguous.
        now = time.time()
        if now - ts.get('cand_t', 0.0) > CONFIRM_STALE_SEC:
            ts['cand_count'] = 0
        ts['cand_t'] = now
        if ts['cand_count'] == 0:
            ts['cand_min'] = spread_val
        ts['cand_count'] += 1
        ts['cand_min'] = min(ts['cand_min'], spread_val)
        if ts['cand_count'] < SL_CONFIRM_POLLS:
            return False
        accepted = ts['cand_min']
        ts['cand_count'] = 0

    if accepted <= ts['peak']:
        return False
    ts['active'] = True
    ts['peak'] = accepted
    ts['trail'] = trail_level_for(
        {'trail_policy': ts.get('policy'), 'net_debit': ts['net_debit'],
         'spread_width': ts.get('spread_width')}, accepted)
    return True


def new_blind_state() -> dict:
    # 'alerted' is what makes recovery announceable. 'since' is set on the
    # FIRST failed poll, long before the escalation clock says anything, so
    # it cannot stand in for "the owner was told" — see track_spot_blindness.
    return {'since': None, 'reason': '', 'last_alert': 0.0, 'last_prox': 0.0,
            'ok_streak': 0, 'alerted': False}


def _is_auth_error(exc) -> bool:
    """Is this exception the Kite token dying?

    ONE definition, called from both the per-trade handler and the outer loop
    handler. The predicate used to exist only inline in the outer handler, so
    the per-trade path could not recognise the very failure that makes the
    outer handler's alert necessary — and any future edit to one copy would
    have silently diverged from the other.

    2026-08-27: it now delegates to `common.kite_errors`, which is the same
    definition `zebra.monitor` uses. Two systems watching the same broker for
    the same failure had two independent substring rules, which is the
    "copy you did not open" shape this repo keeps paying for. The delegate
    also classifies on the exception CLASS and HTTP status rather than only on
    the message, so a 429 `NetworkException('Too many requests')` is a rate
    limit here too — the local rule happened to get that one right by luck
    (the word "token" is absent), not by design.
    """
    return kite_errors.is_auth_error(exc)


def track_spot_blindness(bs: dict, spot_ok: bool, reason: str, label: str):
    """Tell the user when a single record's SPOT fetch has been failing.

    Structurally a twin of `track_spread_blindness`, with two differences that
    are the whole point:

    * It escalates on a 5-minute clock repeating every 10, not 15/60. A
      spread-blind trade still has SL_SPOT armed. A spot-blind trade has
      **nothing** armed — SL_SPOT, SL_SPREAD and TP all read spot, so that
      record is completely unmanaged while the monitor logs happily.
    * It cannot report proximity to the stop, because it does not know where
      spot is. That absence IS the severity, and the message says so.

    The original code swallowed this per-trade (`log(); continue`) forever: a
    renamed or garbled `spot_symbol` meant one position was silently abandoned
    for the whole session with the process reporting healthy.
    """
    now = time.time()
    if spot_ok:
        bs['ok_streak'] += 1
        if bs['ok_streak'] >= BLIND_CLEAR_OK_POLLS:
            # Announce recovery only to someone who was told about the
            # outage. `since` is set on the FIRST failed poll, but the SPOT
            # UNAVAILABLE alert needs SPOT_BLIND_ALERT_SEC of unbroken
            # blindness — so gating the all-clear on `since` announced the end
            # of an alarm that never rang. At a 5s poll one Kite read timeout
            # (they land roughly hourly on api.kite.trade, and 503s arrive in
            # bursts) produced a RECOVERED message ~15s later, per open
            # position, all session. An alert nobody can act on is what trains
            # the owner to swipe past the alert that matters.
            if bs['alerted']:
                send_telegram(f"{label}: spot quotes RECOVERED — triggers "
                              f"re-armed.")
            bs['since'] = None
            bs['alerted'] = False
            # The repeat clock belongs to the spell that just ended. Left
            # standing it would swallow the FIRST alert of the next outage.
            bs['last_alert'] = 0.0
        return
    bs['ok_streak'] = 0
    bs['reason'] = reason or 'no_data'
    if bs['since'] is None:
        bs['since'] = now
        return
    dur = now - bs['since']
    if dur < SPOT_BLIND_ALERT_SEC:
        return
    if now - bs['last_alert'] >= SPOT_BLIND_REPEAT_SEC:
        send_telegram(f"{label}: SPOT UNAVAILABLE for {int(dur / 60)} min "
                      f"({bs['reason']}). This position has NO live triggers — "
                      f"SL_SPOT, SL_SPREAD and TP are all dark. Check the "
                      f"spot_symbol and consider managing it by hand.", alert_class=alert_policy.SAFETY)
        bs['last_alert'] = now
        bs['alerted'] = True


def _malformed_reason(trade: dict):
    """Name the field that makes this record unmonitorable, or None.

    Checked BEFORE the monitor touches a record, because the alternative —
    letting a KeyError escape the per-trade body — takes the whole book down
    with it. Only fields the loop dereferences unconditionally are listed; an
    optional field missing is a different problem and must not quarantine a
    live position.
    """
    common = ('id', 'stock', '_strategy', 'sl_spot', 'spot_symbol', 'quantity')
    missing = [f for f in common if trade.get(f) is None]
    if trade.get('_strategy') in ('BCS', 'BPS'):
        missing += [f for f in ('target_spot', 'sl_spread', 'net_debit',
                                'long_symbol', 'short_symbol')
                    if trade.get(f) is None]
    if missing:
        return 'missing ' + ', '.join(missing)

    # A record filed in the wrong book is unmonitorable for a different
    # reason: every field is present, and the DIRECTION of the stops is wrong.
    # `_store_type` chooses whether SL_SPOT means "spot fell" or "spot rose",
    # so a bear put spread in the BCS book fires both SL_SPOT and TP on the
    # first poll of a perfectly healthy position.
    #
    # Routed through the existing malformed path on purpose: the required
    # behaviour is identical — skip the record, alert once, keep the rest of
    # the book monitored — and a second parallel quarantine path would be one
    # more thing to keep in step.
    wrong = trade_is_misfiled(trade)
    if wrong:
        return ('filed in the wrong book: ' + '; '.join(wrong)
                + '. Its stops would run in the WRONG DIRECTION, so it is not '
                  'monitored. Move it to the store for its own structure.')
    return None


def _alert_malformed_record(trade: dict, reason: str, alerted: set) -> None:
    """Skip a broken record loudly, once — never silently, never repeatedly.

    A live position the monitor cannot read is the worst state in the system:
    it has real risk and no stop. The owner has to be told, but at a 5s poll
    an unconditional Telegram would be thousands of messages a day, so it is
    once per (strategy, id) per process.
    """
    key = (trade.get('_strategy'), trade.get('id'))
    log(f"  MALFORMED RECORD {key}: {reason} — NOT MONITORED, skipping.")
    if key in alerted:
        return
    alerted.add(key)
    send_telegram(
        f"CRITICAL: {key[0]} #{key[1]} {trade.get('stock', '?')} record is "
        f"malformed ({reason}). It is NOT being monitored — no SL, no TP. "
        f"Fix the record or close the position manually.")


def bump_confirm(confirm: dict, key: str) -> int:
    """Increment a trigger-confirmation counter, restarting a stale streak.

    A streak whose last hit is older than CONFIRM_STALE_SEC restarts from
    zero — hits must be reasonably contiguous, but unreliable polls in
    between (which simply don't call this) may not indefinitely block a
    genuine exit in a flickering book.
    """
    now = time.time()
    if now - confirm.get(key + '_t', 0.0) > CONFIRM_STALE_SEC:
        confirm[key] = 0
    confirm[key] += 1
    confirm[key + '_t'] = now
    return confirm[key]


def track_spread_blindness(bs: dict, spread_ok: bool, reason: str,
                           spot: float, sl_spot: float, adverse_below: bool,
                           label: str):
    """Tell the user when spread valuation has been blind long enough to
    matter. Pure observability — no trading action; SL_SPOT stays armed.

    The blind clock clears only after BLIND_CLEAR_OK_POLLS consecutive ok
    polls — one good quote inside a flickering book must not silence the
    alert. adverse_below: True when the SL direction is DOWN (BCS:
    spot <= sl_spot), False when UP (BPS).
    """
    now = time.time()
    if spread_ok:
        bs['ok_streak'] += 1
        if bs['ok_streak'] >= BLIND_CLEAR_OK_POLLS:
            bs['since'] = None
        return
    bs['ok_streak'] = 0
    bs['reason'] = reason or 'no_data'
    if bs['since'] is None:
        bs['since'] = now
        return
    dur = now - bs['since']
    if dur < SPREAD_BLIND_ALERT_SEC:
        return
    if now - bs['last_alert'] >= SPREAD_BLIND_REPEAT_SEC:
        send_telegram(f"{label}: spread quotes unreliable for {int(dur / 60)} min "
                      f"({bs['reason']}). SL_SPREAD/SL_TRAIL suspended. "
                      f"SL_SPOT still armed at {sl_spot}.", alert_class=alert_policy.SAFETY)
        bs['last_alert'] = now
    near = (spot <= sl_spot * (1 + SPOT_PROXIMITY_ALERT_PCT)) if adverse_below \
        else (spot >= sl_spot * (1 - SPOT_PROXIMITY_ALERT_PCT))
    if near and now - bs['last_prox'] >= PROXIMITY_REPEAT_SEC:
        send_telegram(f"{label}: BLIND NEAR SL! spot {spot} vs SL {sl_spot}, quotes "
                      f"unreliable ({bs['reason']}). Spread SLs cannot fire — "
                      f"consider manual action.", alert_class=alert_policy.SAFETY)
        bs['last_prox'] = now


# ── Main Monitor Loop ────────────────────────────────────────────────────────

def monitor(kite: KiteConnect, trade: dict, target: float,
            sl_spot: float, sl_spread: float, dry_run: bool,
            cli_target: Optional[float] = None,
            cli_sl_spot: Optional[float] = None,
            cli_sl_spread: Optional[float] = None):
    """
    Main monitoring loop with TP + 3-layer SL.

    Check order each cycle:
      1. SL_SPOT    — spot <= sl_spot
      2. SL_SPREAD  — spread_value <= sl_spread
      3. SL_TRAIL   — auto-engages at 2x debit, trails 60% of peak
      4. TP         — spot >= target

    cli_target/cli_sl_spot/cli_sl_spread: If set, these CLI overrides take
    precedence over trade store values. If None, values refresh from store
    on each Drive sync.
    """
    store = get_store()
    stock = trade['stock']
    set_log_file(LOG_DIR / f"spread_monitor_{stock}_{today_ist().strftime('%Y%m%d')}.log")

    entry_net = trade['net_debit']
    trail_engage_level = entry_net * TRAIL_ENGAGE_MULTIPLIER

    # Restore trailing SL state from trade store (survives process restarts)
    ts = new_trail_state(trade)
    if ts['active']:
        log(f"  Restored trail state: peak={ts['peak']:.2f}, trail={ts['trail']:.2f}")

    # Trigger-confirmation + blind-mode state (in-memory; reset on restart —
    # a restart can only DELAY a value trigger, never accelerate one)
    confirm = {'sl_spread': 0, 'sl_trail': 0, 'sl_spot': 0}
    abort_until = 0.0    # no valuation/TP close attempts before this time
    blind = new_blind_state()
    # Spot-corroboration reference. In-memory and reset on restart for the
    # same reason as `confirm`: a restart may only DELAY a trigger (the first
    # poll after it has no reference and therefore cannot veto), never fire one.
    corrob = {}

    log("")
    log("=" * 70)
    log(f"  {stock} SPREAD MONITOR")
    log("=" * 70)
    log(f"  Position: Bull Call Spread")
    log(f"  Long:     {trade['long_symbol']} x {trade['quantity']}")
    log(f"  Short:    {trade['short_symbol']} x {trade['quantity']}")
    log(f"  Entry:    Long @ {trade['entry_long_price']} | Short @ {trade['entry_short_price']} | Net: {entry_net:.2f}")
    log(f"  Qty:      {trade['quantity']} ({trade.get('lots', '?')} lots x {trade.get('lot_size', '?')})")
    log(f"  Target:   {stock} >= {target}")
    log(f"  SL Spot:  {stock} <= {sl_spot}")
    log(f"  SL Spread:<= {sl_spread:.2f}")
    log(f"  SL Trail: Engages at spread >= {trail_engage_level:.2f} (2x debit), trails 60% of peak")
    log(f"  Mode:     {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    log(f"  Poll:     Every {POLL_INTERVAL_SEC}s | Status every {STATUS_PRINT_INTERVAL_SEC}s")
    log("=" * 70)

    log("\nVerifying positions in Kite...")
    verify_positions(kite, trade)

    spot = get_spot(kite, trade['spot_symbol'])
    log(f"\nCurrent spot: {spot:.2f} | Gap to target: {target - spot:+.2f} | Buffer above SL: {spot - sl_spot:+.2f}")

    # Immediate checks — only when market has settled (not during open buffer)
    if is_market_settled():
        if spot <= sl_spot:
            log(f"Spot already at/below SL!")
            if close_spread(kite, trade, spot, "SL_SPOT", dry_run) != 'ABORT':
                return

        elif tp_armed(trade, spot >= target, spot, store=store,
                      dry_run=dry_run, label='BCS'):
            log("Spot already at/above target!" if spot >= target else
                "TP was touched in an earlier session and is still armed!")
            res = close_spread(kite, trade, spot, "TP", dry_run)
            if res != 'ABORT':
                if res is True:
                    stamp_tp_touch_to_fill(trade, store, spot, rising=True,
                                           dry_run=dry_run, label='BCS')
                return
    else:
        log(f"\n  Skipping immediate checks — market-open buffer active")

    # ── Expiry day warning ────────────────────────────────────────────────
    expiry_today = is_expiry_day(trade)
    if expiry_today:
        log(f"\n  *** EXPIRY DAY! Will force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
        send_telegram(f"BCS {stock}: EXPIRY DAY. Monitor will force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")
    else:
        try:
            # ['spread'], not the whole dict — get_spread_value returns
            # {'long','short','spread','unreliable'} and gamma_note does
            # float(spread_val). Passing the dict raised TypeError before the
            # message was ever built, and the caller's except swallowed it, so
            # the warning was dead every day it had something to say.
            maybe_warn_expiry_proximity(store, trade, spot, 'BCS',
                                        spread_val=get_spread_value(
                                            kite, trade, spot=spot).get('spread'))
        except Exception as e:
            log(f"  WARNING: expiry-proximity check failed: {e}")

    log(f"\nMonitoring started. Waiting for TP={target} | SL={sl_spot}...\n")
    if not is_market_settled():
        log(f"  Market-open buffer active: SL/TP checks delayed until {MARKET_OPEN_BUFFER_SEC}s after open")

    last_status_time = 0
    consecutive_errors = 0

    while True:
        try:
            if not is_market_open():
                now_t = now_ist().time()
                if now_t > MARKET_CLOSE:
                    log("Market closed for the day. Exiting monitor.")
                    if ts['active']:
                        log(f"  Trailing SL state: peak={ts['peak']:.2f}, trail={ts['trail']:.2f}")
                    return
                # Before market open - wait
                log(f"Market not open yet ({now_t.strftime('%H:%M')}). Waiting...")
                time.sleep(30)
                continue

            # Periodic Drive sync (picks up manual trade edits)
            store.maybe_sync()

            # Re-read trade from store (picks up SL/TP edits from other machines)
            fresh = store.find_open_trade(stock, trade['id'])
            if fresh is None:
                log("Trade no longer open (closed/edited from another machine?). Exiting.")
                return
            trade = fresh
            # Refresh SL/TP from store unless CLI overrode them
            if cli_target is None:
                target = trade['target_spot']
            if cli_sl_spot is None:
                sl_spot = trade['sl_spot']
            if cli_sl_spread is None:
                sl_spread = trade['sl_spread']

            # F7, single-trade mode: 3 requests per poll become 2. A smaller
            # win than the cron loop's, but the same call is doing it, so
            # leaving this path unbatched would just be a second definition of
            # how a poll reads prices.
            prefetch_book(kite, [trade])
            spot = get_spot(kite, trade['spot_symbol'])
            now = time.time()
            consecutive_errors = 0  # Reset on successful API call
            # A long gap since the last GOOD poll means a halt, an outage or a
            # crash-restart: the book has to re-form before value triggers
            # arm again.
            note_poll(True, now)

            # Fetch spread for SL checks and status
            spread_data = None
            spread_val = None
            spread_fail = None
            try:
                spread_data = get_spread_value(kite, trade, spot=spot)
                spread_val = spread_data['spread']
                # Independent cross-check. Every guard above reads the same
                # order book, so identical stale prints confirm one another;
                # spot is the only source that cannot be wrong in the same way.
                if spread_val is not None:
                    ok, why = spot_corroborates(corrob, spot, spread_val)
                    if not ok:
                        log(f"  QUOTE GUARD: {why} — valuation rejected")
                        spread_data['unreliable'] = why
                        spread_val = None
            except Exception as e:
                spread_fail = str(e)  # spot-based checks still work

            settled = is_market_settled()
            spread_settled = is_spread_settled()

            # ── Blind-mode tracking: user must know when spread SLs are
            # suspended (unreliable books, quote failures) ────────────────
            blind_reason = spread_fail or (spread_data['unreliable'] if spread_data else 'no_data')
            track_spread_blindness(
                blind, spread_ok=(spread_val is not None or not spread_settled),
                reason=blind_reason, spot=spot, sl_spot=sl_spot,
                adverse_below=True, label=f"BCS {stock}")

            # ── Update trailing SL state (jump-gated, reliable quotes only,
            # spread-settled market only) ─────────────────────────────────
            if spread_settled and spread_val is not None:
                was_active = ts['active']
                if update_trail(ts, spread_val):
                    if not was_active:
                        log(f"  ** TRAILING SL ENGAGED ** spread={spread_val:.2f} >= {trail_engage_level:.2f}")
                        log(f"     Peak: {ts['peak']:.2f} | Trail level: {ts['trail']:.2f}")
                    else:
                        log(f"  ** TRAIL UPDATED ** Peak: {ts['peak']:.2f} | Trail: {ts['trail']:.2f}")
                    store.update_trade_fields(trade['id'], trail_active=True,
                                              trail_peak=ts['peak'], trail_sl=ts['trail'])
                elif ts['cand_count'] > 0:
                    log(f"  Trail peak candidate {spread_val:.2f} held "
                        f"(confirm {ts['cand_count']}/{SL_CONFIRM_POLLS})")

            # ── Periodic status line ─────────────────────────────────────
            if now - last_status_time >= STATUS_PRINT_INTERVAL_SEC:
                settle_tag = "" if settled else " [COOLDOWN]"
                if settled and not spread_settled:
                    settle_tag = " [SPREAD-COOLDOWN]"
                expiry_tag = " [EXPIRY]" if expiry_today else ""
                suspect_tag = ""
                if spread_data and spread_data.get('unreliable'):
                    suspect_tag = f" [SUSPECT {spread_data['unreliable']}]"
                elif spread_fail:
                    suspect_tag = f" [QUOTE-FAIL {spread_fail[:40]}]"
                try:
                    if spread_data and spread_val is not None:
                        unrealized = (spread_val - entry_net) * trade['quantity']
                        trail_str = f" | Trail: {ts['trail']:.2f}" if ts['active'] else ""
                        log(
                            f"Spot: {spot:>8.2f} | "
                            f"TP: {target} (gap: {target - spot:>+.2f}) | "
                            f"SL: {sl_spot} (buf: {spot - sl_spot:>+.2f}) | "
                            f"Spread: {spread_val:>6.2f} "
                            f"(L:{spread_data['long']['bid']} S:{spread_data['short']['ask']}) | "
                            f"P&L: Rs {unrealized:>+,.0f}{trail_str}{settle_tag}{expiry_tag}"
                        )
                    else:
                        log(f"Spot: {spot:>8.2f} | TP: {target} (gap: {target - spot:>+.2f}) | SL: {sl_spot} (buf: {spot - sl_spot:>+.2f}){settle_tag}{expiry_tag}{suspect_tag}")
                except Exception:
                    log(f"Spot: {spot:>8.2f} | TP: {target} | SL: {sl_spot}{settle_tag}{expiry_tag}")
                last_status_time = now

            # ── EXPIRY DAY: Force close by EXPIRY_FORCE_CLOSE_TIME ──────
            # Single-trade mode: no retry state here on purpose. It closes and
            # RETURNS, so there is no later poll to retry on, and adding a
            # second place that owns time-stop state is the shape M11 is
            # fixing. Only the clock test is shared.
            if expiry_today and time_stop_window_open():
                log(f"\n  *** EXPIRY FORCE CLOSE: {now_ist().strftime('%H:%M')} >= {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
                success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run)
                if success:
                    log("\nMonitor complete. Position closed (expiry day force close).")
                else:
                    log("\nExpiry force close FAILED. CHECK POSITION MANUALLY!")
                return

            # ── CHECK 1: SL_SPOT — always active, even during cooldown ────
            # Still exempt from every cooldown (a thesis-dead exit must not
            # wait for a book to form), but no longer single-poll: this is the
            # one trigger that runs at URGENT urgency, whose final attempt pays
            # through uncapped, and both real-money losses happened at the open.
            # SL_SPOT_CONFIRM_POLLS=2 is ~10 seconds.
            if spot <= sl_spot:
                n = bump_confirm(confirm, 'sl_spot')
                if n < SL_SPOT_CONFIRM_POLLS:
                    log(f"  SL_SPOT condition {spot:.2f} <= {sl_spot} "
                        f"(confirm {n}/{SL_SPOT_CONFIRM_POLLS})")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                log(f"\n  *** SL_SPOT HIT: {spot:.2f} <= {sl_spot} "
                    f"(confirmed {n}/{SL_SPOT_CONFIRM_POLLS}) ***")
                success = close_spread(kite, trade, spot, "SL_SPOT", dry_run)
                if success == 'ABORT':
                    log("  SL_SPOT close aborted — still monitoring.")
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                if success:
                    log("\nMonitor complete. Position closed on SL_SPOT.")
                else:
                    log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                return
            else:
                # Spot recovered above the stop: the streak is broken. Same
                # discipline as sl_spread/sl_trail — only contiguous hits count.
                confirm['sl_spot'] = 0

            # ── Cooldown gate: TP needs settled market ───────────────────
            if not settled:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # ── Abort cooldown: after an aborted close, hold off further
            # valuation/TP attempts (SL_SPOT above stays exempt) ──────────
            in_abort_cooldown = time.time() < abort_until

            # ── Spread-trigger gate: SL_SPREAD/SL_TRAIL wait for the longer
            # spread buffer AND need SL_CONFIRM_POLLS reliable polls in
            # trigger state (2026-07-24: single garbage poll fired a false
            # SL_SPREAD and cost Rs 7,297). Unreliable polls freeze the
            # counters; bump_confirm restarts stale streaks. ──────────────
            if spread_settled and spread_val is not None and not in_abort_cooldown:

                # ── CHECK 2: SL_SPREAD (debounced + re-verified) ─────────
                if spread_val <= sl_spread:
                    n = bump_confirm(confirm, 'sl_spread')
                    if n < SL_CONFIRM_POLLS:
                        log(f"  SL_SPREAD condition {spread_val:.2f} <= {sl_spread:.2f} "
                            f"(confirm {n}/{SL_CONFIRM_POLLS})")
                    else:
                        log(f"\n  *** SL_SPREAD HIT: {spread_val:.2f} <= {sl_spread:.2f} "
                            f"(confirmed {n}x) ***")
                        success = close_spread(kite, trade, spot, "SL_SPREAD", dry_run,
                                               reverify_sl=sl_spread)
                        if success == 'ABORT':
                            confirm['sl_spread'] = 0
                            abort_until = time.time() + ABORT_COOLDOWN_SEC
                            time.sleep(POLL_INTERVAL_SEC)
                            continue
                        if success:
                            log("\nMonitor complete. Position closed on SL_SPREAD.")
                        else:
                            log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                        return
                else:
                    confirm['sl_spread'] = 0

                # ── CHECK 3: SL_TRAIL (debounced + re-verified) ──────────
                if ts['active'] and spread_val <= ts['trail']:
                    n = bump_confirm(confirm, 'sl_trail')
                    if n < SL_CONFIRM_POLLS:
                        log(f"  SL_TRAIL condition {spread_val:.2f} <= {ts['trail']:.2f} "
                            f"(confirm {n}/{SL_CONFIRM_POLLS})")
                    else:
                        log(f"\n  *** SL_TRAIL HIT: {spread_val:.2f} <= {ts['trail']:.2f} "
                            f"(peak was {ts['peak']:.2f}, confirmed {n}x) ***")
                        success = close_spread(kite, trade, spot, "SL_TRAIL", dry_run,
                                               reverify_sl=ts['trail'])
                        if success == 'ABORT':
                            confirm['sl_trail'] = 0
                            abort_until = time.time() + ABORT_COOLDOWN_SEC
                            time.sleep(POLL_INTERVAL_SEC)
                            continue
                        if success:
                            log("\nMonitor complete. Position closed on SL_TRAIL.")
                        else:
                            log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                        return
                else:
                    confirm['sl_trail'] = 0

            # ── CHECK 4: TP ──────────────────────────────────────────────
            # Same latch as the cron loop, from the same shared decision. This
            # mode is the one nobody runs, which is exactly why it gets the
            # rule too: a trigger honoured in one copy and not the other is
            # this codebase's most repeated defect.
            tp_go = tp_armed(trade, spot >= target, spot, store=store,
                             dry_run=dry_run, label='BCS')
            if tp_go and not in_abort_cooldown:
                success = close_spread(kite, trade, spot, "TP", dry_run)
                if success is True:
                    stamp_tp_touch_to_fill(trade, store, spot, rising=True,
                                           dry_run=dry_run, label='BCS')
                if success == 'ABORT':
                    log(f"  TP close aborted — retrying after {ABORT_COOLDOWN_SEC}s cooldown "
                        f"(spot trigger persists).")
                    abort_until = time.time() + ABORT_COOLDOWN_SEC
                    time.sleep(POLL_INTERVAL_SEC)
                    continue
                if success:
                    log("\nMonitor complete. Position closed on TP.")
                else:
                    log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                return

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            log("\nMonitor stopped by user (Ctrl+C)")
            log("Position is still OPEN.")
            if ts['active']:
                log(f"  Trailing SL state: peak={ts['peak']:.2f}, trail={ts['trail']:.2f}")
            return
        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            # Check for token expiry (fatal — can't recover without new token)
            # ONE definition of "the token died", shared with the per-trade
            # handler — see _is_auth_error.
            if _is_auth_error(e):
                log("FATAL: Kite token appears expired. Cannot continue.")
                send_telegram(f"BCS MONITOR FATAL: Kite token expired! {stock} is UNMONITORED.", alert_class=alert_policy.SAFETY)
                return
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting.")
                send_telegram(f"BCS MONITOR FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. {stock} is UNMONITORED!", alert_class=alert_policy.SAFETY)
                return
            if consecutive_errors == 10:
                send_telegram(f"BCS MONITOR WARNING: 10 consecutive errors for {stock}. Last: {e}", alert_class=alert_policy.SAFETY)
            time.sleep(10)


# ── Cron Mode (All Open Trades — BCS + FH) ──────────────────────────────────

def list_cohort_trades() -> int:
    """Print the cohort positions the ORDER PATH manages. Returns the count.

    Reads through the same adapter `monitor_all` uses, deliberately. A listing
    built from its own query would be a second definition of "open", and the
    first definition being wrong -- `Trades: 0 open` with six live -- is what
    started this whole exercise.
    """
    print("\n=== BCS Cohort (zebra store) ===")
    store = _open_zebra_store()
    if store is None:
        print("  cohort store unavailable - the monitor would not see these "
              "positions either")
        return 0
    try:
        trades = store.get_open_trades()
    except Exception as e:
        print(f"  could not read the cohort store: {e}")
        return 0
    if not trades:
        print("  no open cohort positions")
        return 0
    print("\n %-4s %-12s %-5s %-13s %-12s %8s %9s %9s %10s" % (
        'ID', 'Stock', 'Dir', 'Strikes', 'Expiry', 'Debit', 'Lots',
        'Target', 'SL Spread'))
    print("-" * 96)
    for t in sorted(trades, key=lambda x: x.get('id') or 0):
        # `vertical_direction`, NOT `get_strategy`. The adapter does not stamp
        # `_store_type` -- `_load_all_trades` does -- so `get_strategy` falls
        # through to its 'BCS' default here and labelled TMPV, COALINDIA and
        # CROMPTON as bull call spreads when all three are PE bear puts. The
        # symbols are the authoritative source, which is the entire point of
        # the bridge; a listing that reports direction from anything else is
        # the bug the bridge exists to prevent, wearing a different hat.
        print(" %-4s %-12s %-5s %-13s %-12s %8s %9s %9s %10s" % (
            t.get('id'), t.get('stock'), vertical_direction(t) or '?',
            '%g/%g' % (t.get('long_strike') or 0, t.get('short_strike') or 0),
            t.get('expiry'), t.get('net_debit'),
            '%sx%s' % (t.get('lots') or 1, t.get('lot_size') or '?'),
            t.get('target_spot'), t.get('sl_spread')))
    print("\nOpen: %d" % len(trades))
    return len(trades)


def _open_zebra_store():
    """The BCS cohort's store, adapted — or None, with a loud line, if not.

    Returns None rather than raising. The cohort is a FOURTH book: if it cannot
    be opened, the right outcome is that the other three stay monitored and
    somebody is told, not that a whole session of exit monitoring dies over a
    store that may hold nothing today.

    But it is never silent. A cohort position with no monitor is precisely the
    failure that has cost real money here — twice — so an unavailable book
    reaches Telegram, not just the log.
    """
    try:
        from bcs.zebra_adapter import get_adapter, cohort_label
        store = get_adapter()
        n = len(store.get_open_trades())
        log(f"  Cohort store ({cohort_label()}): {n} open position(s).")
        return store
    except Exception as e:
        log(f"  WARNING: cohort store unavailable ({e}). Its positions are "
            f"NOT being monitored this session.")
        # NOT html.escape'd, deliberately. `send_telegram` in this module
        # posts with no parse_mode, so Telegram treats the text as plain and
        # escaping would put a literal `&lt;` in the alert. The escaping rule
        # in feedback_telegram_html_escape is about zebra's sender, which does
        # set parse_mode. Two senders, two rules; check which one you are in.
        send_telegram(f"Cohort store unavailable ({str(e)[:120]}) "
                      f"- those positions are NOT monitored. The other books "
                      f"continue.", alert_class=alert_policy.SAFETY)
        return None


def trade_key(trade: dict):
    """The per-position key for every in-loop state dict.

    `(store, strategy, id)`. All three parts are load-bearing:

    * `id` alone collides — each book numbers its own trades from 1.
    * `strategy` alone does not separate them either, now that the zebra store
      produces BOTH BCS and BPS records: two of its rows can share a strategy
      while a bcs-store row shares an id.
    * `store` alone would merge a zebra BCS with a zebra BPS of the same id.

    One function because the key was previously built in two places — the
    startup block wrote `('BCS', id)` while the loop wrote `close_key` — and
    the moment those two disagreed, `trail_state[close_key]` raised inside the
    cron loop on every cycle. It surfaced as "too many values to unpack",
    twenty consecutive errors, and a FATAL "Unmonitored" Telegram; the trail
    state was silently never restored in the meantime.
    """
    return (trade.get('_store_type'), trade.get('_strategy'), trade.get('id'))


def _load_all_trades(bcs_store, fh_store, bps_store=None,
                     zebra_store=None) -> list:
    """Load and tag open trades from all stores.

    Returns shallow copies with _strategy/_store_type tags to avoid
    polluting the store's in-memory trade dicts (which get persisted).
    """
    all_trades = []
    # `strat` is None for zebra: that book holds bull call spreads AND bear
    # put spreads together, so the direction cannot come from which store a
    # record arrived in. It is read from the record's own legs below.
    for store, strat, st in ((bcs_store, 'BCS', 'bcs'),
                             (bps_store, 'BPS', 'bps'),
                             (fh_store, 'FH', 'fh'),
                             (zebra_store, None, 'zebra')):
        if store is None:
            continue
        for t in store.get_open_trades():
            tagged = dict(t)
            tagged['_store_type'] = st
            tagged['_strategy'] = strat if strat else get_strategy(tagged)
            # DIRECTION COMES FROM THE STORE, so a misfiled record has its
            # stops inverted. `_misfiled` carries the contradiction forward
            # rather than resolving it here — see `trade_is_misfiled`.
            tagged['_misfiled'] = _leg_problems(tagged, st)
            all_trades.append(tagged)
    return all_trades


#: Per-book leg expectations, indexed by the same tag `_load_all_trades`
#: stamps. Kept here rather than imported from the three stores so that adding
#: a book without a row is a KeyError at load, not a silently unchecked record.
LEG_TYPES_BY_STORE = {
    'bcs': {'long_symbol': 'CE', 'short_symbol': 'CE'},
    'bps': {'long_symbol': 'PE', 'short_symbol': 'PE'},
    # 'zebra' is deliberately ABSENT. That store holds both directions, so
    # there is no single pair of leg types it must satisfy — see
    # `_leg_problems`, which checks it a different way. A KeyError here for any
    # future book without a row is the point.
    'fh': {'long_put_symbol': 'PE', 'short_put_symbol': 'PE',
           'short_call_symbol': 'CE', 'long_call_symbol': 'CE'},
}


def _leg_problems(trade: dict, store_type: str):
    """Leg problems for a record from `store_type`, or an empty list.

    Two different checks, because the books are two different shapes.

    A single-direction book (bcs / bps / fh) has one right answer, so the legs
    are compared against a fixed table.

    The zebra book holds bull call spreads and bear put spreads together, so
    there is no fixed answer — what must hold instead is that the record agrees
    with ITSELF. The legs must form a readable vertical (both CE or both PE),
    and that must match the `direction` field the scanner wrote. When the
    contract and the bookkeeping disagree, neither is trusted: the record is
    quarantined rather than resolved in favour of one, exactly as B23 does,
    because sl_spot / tp_spot / debit_sl_value were all computed for whichever
    one the writer believed.
    """
    if store_type != 'zebra':
        return check_leg_types(trade, LEG_TYPES_BY_STORE[store_type])

    lo = _sym.option_type(trade.get('long_symbol'))
    sh = _sym.option_type(trade.get('short_symbol'))
    if lo is None or sh is None:
        return [f"long_symbol={trade.get('long_symbol')!r} / "
                f"short_symbol={trade.get('short_symbol')!r}: one or both are "
                f"not option symbols, so the direction cannot be read"]
    if lo != sh:
        return [f"legs are mixed ({lo} long, {sh} short) — not a vertical, so "
                f"SL_SPOT and TP have no defined direction"]
    want = trade.get('direction')
    if want is not None and str(want).upper() != lo:
        return [f"symbols say {lo} but direction={want!r}. The stops were "
                f"computed for one of these and the payoff follows the other; "
                f"which is wrong cannot be inferred here"]
    return []


def trade_is_misfiled(trade: dict):
    """Problems with this record's legs versus its book, or an empty list.

    The stores refuse a mismatched record at `add_trade` since 2026-08-25, but
    that cannot help anything already open, and the consequence is not subtle:
    SL_SPOT and TP direction are chosen by `_store_type`, so a bear put spread
    sitting in the BCS book reads its upside stop as a downside one. On the
    FIRST poll of a perfectly healthy position both `spot <= sl_spot` and
    `spot >= target` are true, and the monitor closes it at whatever the book
    offers.

    Deliberately NOT auto-corrected from the symbols. Every level on the
    record — sl_spot, target, sl_spread — was written for the structure whoever
    saved it believed they had, so flipping the comparison would run real
    money against numbers that may mean nothing. The monitor withholds the
    ORDER and keeps the WARNING, the same division as the kill switch.
    """
    return trade.get('_misfiled') or []


def _get_store_for(trade, bcs_store, fh_store, bps_store=None,
                   zebra_store=None):
    """Return the correct store for a trade based on its _store_type tag.

    Routed by _store_type and NOT by _strategy, which is the distinction the
    zebra book forces: one of its records can be tagged BPS while still
    belonging to the zebra store. Getting this backwards would write a cohort
    exit into the bear_put book.
    """
    st = trade.get('_store_type')
    if st == 'fh':
        return fh_store
    if st == 'bps' and bps_store:
        return bps_store
    if st == 'zebra' and zebra_store:
        return zebra_store
    return bcs_store


def _leg_state(positions, symbol, want_long: bool) -> str:
    """OK / MISSING / FLIPPED — never conflate the last two.

    The startup check tested `quantity > 0` (or `< 0`) and reported anything
    else as "MISSING". A leg that has FLIPPED — the Feb-2026 shape — is not
    missing: it is a live position facing the wrong way, and it needs the
    opposite response. Saying "MISSING" sends the reader looking for an
    unfilled order instead of at a naked leg.
    """
    for p in positions:
        if p['tradingsymbol'] == symbol:
            q = p['quantity']
            if q == 0:
                return 'MISSING'
            if (q > 0) == want_long:
                return 'OK'
            return 'FLIPPED %+d' % q
    return 'MISSING'


def position_check_line(trade: dict, strat: str, positions) -> tuple:
    """`(is_warning, message)` for one record's broker-leg verification.

    Extracted from the cron startup sweep 2026-08-28, for two reasons. The
    small one: three near-identical branches, and this repo's most repeated
    defect is a rule fixed in one of them. The load-bearing one: it makes the
    PAPER exemption below a single testable decision rather than a `continue`
    buried in a 4,000-line function.

    **A paper record has no broker legs and never had any.** On 2026-08-28 the
    startup sweep logged

        WARNING: BCS #436 COFORGE — positions MISSING! (long=MISSING, short=MISSING)

    for all eight cohort records. Every one was accurate and every one was
    noise: the whole book is `paper: True`, so absent legs are the DEFINITION
    of the record and not a discrepancy. A warning that fires on the healthy
    case trains the reader to skip it, which is precisely how the OI flag on
    COCHINSHIP got waved through.

    It reports as a plain line rather than "verified", because claiming a
    verification that never ran is the opposite lie.

    The predicate is `_record_says_paper` — which DEFAULTS TO REAL — and not
    `paper_mode`, because the question is who owns THIS record, not what mode
    the process is in. The bcs / bear_put / fallen_hero books have never
    carried a `paper` key, so they keep the loud warning they need, and a real
    record with missing legs stays a WARNING in every mode.
    """
    tid, stock = trade.get('id'), trade.get('stock')
    if _record_says_paper(trade):
        return False, (f"{strat} #{tid} {stock} — PAPER record, no broker legs "
                       f"expected (position check does not apply)")
    if strat in ('BCS', 'BPS'):
        long_st = _leg_state(positions, trade['long_symbol'], want_long=True)
        short_st = _leg_state(positions, trade['short_symbol'], want_long=False)
        if long_st != 'OK' or short_st != 'OK':
            bad = 'FLIPPED' if 'FLIPPED' in (long_st + short_st) else 'MISSING'
            return True, (f"{strat} #{tid} {stock} — positions {bad}! "
                          f"(long={long_st}, short={short_st})")
        return False, f"{strat} #{tid} {stock} — positions verified"
    # FH: 3-4 legs
    def _held(sym, want_long):
        return any(p['tradingsymbol'] == sym
                   and (p['quantity'] > 0 if want_long else p['quantity'] < 0)
                   for p in positions)
    missing = []
    if not _held(trade['short_call_symbol'], False):
        missing.append('SC')
    if not _held(trade['short_put_symbol'], False):
        missing.append('SP')
    if not _held(trade['long_put_symbol'], True):
        missing.append('LP')
    if trade.get('long_call_symbol') and not _held(trade['long_call_symbol'],
                                                   True):
        missing.append('LC')
    if missing:
        return True, f"FH #{tid} {stock} — legs missing: {', '.join(missing)}"
    return False, f"FH  #{tid} {stock} — all legs verified"


def alert_store_corruption(books) -> bool:
    """Turn any quarantine marker into a Telegram. True if any book is flagged.

    B7. `_read_local` backs up a corrupt file and returns `[]`, which the
    monitor could not tell from "the book is empty because everything closed".
    So the loop logged "All trades closed... exiting" and stopped watching
    every open position, and the only witness was one CRITICAL line in a log
    nobody tails. The total failure was exactly the case that could not report
    itself.

    `books` is [(label, store), ...]. Alerting is per book and re-armed hourly
    by `corruption_due_for_alert`, because the cron relaunches every 5 minutes
    and this path EXITS the monitor.

    THE RETURN VALUE MEANS "this book may have gone empty underneath us", and
    only a QUARANTINE means that. A merge conflict still alerts -- two writers
    disagreeing is worth knowing -- but returns False, because the callers use
    a True to refuse to exit on an empty book and to report that the emptiness
    was caused by a quarantine. Saying that about an intact book would put a
    false explanation on an ordinary quiet morning.
    """
    flagged = False
    for label, store in books:
        try:
            if not store.read_corruption_marker():
                continue
            marker = store.corruption_due_for_alert()
            kind = (store.read_corruption_marker().get('kind')
                    or store_contract.MARKER_QUARANTINE)
            quarantined = kind != store_contract.MARKER_MERGE_CONFLICT
            flagged = flagged or quarantined
            if not marker:
                continue            # already shouted within the hour
            if quarantined:
                log(f"  *** {label} STORE QUARANTINED: {marker.get('error')} "
                    f"— backup {marker.get('backup')} ***")
                send_telegram(
                    f"CRITICAL: the {label} trade store was QUARANTINED at "
                    f"{marker.get('at')}.\n"
                    f"Reason: {marker.get('error')}\n"
                    f"Backup: {marker.get('backup')}\n"
                    f"The book now reads EMPTY, so any open {label} position "
                    f"is UNMONITORED. Check the backup and restore before the "
                    f"next session. New trades will NOT reuse the old ids.")
            else:
                log(f"  *** {label} STORE MERGE CONFLICT: "
                    f"{marker.get('error')} ***")
                send_telegram(
                    f"WARNING: the {label} trade store hit a merge conflict "
                    f"at {marker.get('at')}.\n"
                    f"Detail: {marker.get('error')}\n"
                    f"The book is INTACT and still monitored — nothing was "
                    f"quarantined and no backup is needed. The losing side's "
                    f"edit was dropped; re-check those records if it mattered.")
            store.note_corruption_alerted()
        except Exception as e:
            # Never let the alerting path be the thing that stops the monitor.
            log(f"  WARNING: corruption check failed for {label}: {e}")
    return flagged


#: The books `--book-frozen` / `--reopen-frozen` can address, by CLI name.
#: Keyed by the word the operator types, so an unknown one is refused with the
#: list rather than silently resolving to the first store.
FROZEN_BOOKS = ('bcs', 'bps', 'fh', 'cohort')


def _frozen_book(name):
    """`(label, store)` for a CLI book name, or `(None, None)`."""
    name = (name or '').strip().lower()
    if name == 'bcs':
        return 'BCS', get_store()
    if name == 'bps':
        return 'BPS', get_bps_store()
    if name == 'fh':
        return 'FH', get_fh_store()
    if name == 'cohort':
        return 'COHORT', _open_zebra_store()
    return None, None


def _parse_frozen_ref(ref):
    """`STORE:ID` -> `(label, store, id)`. Prints and returns None on nonsense.

    Deliberately strict. These two verbs are the ONLY way an escalated incident
    is ever cleared, and they write to the money book; guessing which store the
    operator meant is not a convenience worth having.
    """
    if ':' not in str(ref or ''):
        print('Expected STORE:ID, e.g. bcs:7. Books: %s'
              % ', '.join(FROZEN_BOOKS))
        return None
    book, _, raw = str(ref).partition(':')
    label, store = _frozen_book(book)
    if store is None:
        print('Unknown book %r. Books: %s' % (book, ', '.join(FROZEN_BOOKS)))
        return None
    try:
        tid = int(raw)
    except ValueError:
        print('Trade id must be a number, got %r' % raw)
        return None
    return label, store, tid


def _show_broker_reality(kite, trade, label):
    """Print the LIVE book for a record. Returns `{symbol: qty}`.

    Printed before either verb acts, always. The operator is about to assert
    something about reality ("it is flat", "it is intact") and the whole point
    of the frozen state is that the record cannot be trusted to say which.
    """
    live = {}
    for sym, _sign in _legs_of(trade):
        try:
            live[sym] = get_net_position(kite, sym)
        except Exception as e:
            print('  %s: could not read position (%s)' % (sym, e))
            live[sym] = None
    print('  Broker says, right now:')
    for sym, q in live.items():
        print('    %-28s %s' % (sym, 'UNREADABLE' if q is None else '%+d' % q))
    return live


def book_frozen(kite, ref, short_price=None, long_price=None):
    """Clear a frozen incident by BOOKING it — for a book that is already flat.

    The missing tool S5 always needed: an unpriced refusal leaves a record
    frozen precisely because this system could not price it, and no automated
    path ever will. The operator supplies what they actually filled at.

    REFUSES if the broker still shows a live leg. Booking a position that is
    not flat would write an exit for quantity still at risk — the same lie
    D2/D3 were about, typed in by hand instead of computed.
    """
    parsed = _parse_frozen_ref(ref)
    if not parsed:
        return 1
    label, store, tid = parsed
    trade = next((t for t in store.get_frozen_trades() if t['id'] == tid), None)
    if trade is None:
        print('%s #%s is not frozen (nothing at partial_close with that id).'
              % (label, tid))
        return 1

    print('\n%s #%s %s — frozen at %s'
          % (label, tid, trade.get('stock', '?'),
             (trade.get('close_failure') or {}).get('frozen_at', '?')))
    live = _show_broker_reality(kite, trade, label)
    if any(q is None for q in live.values()):
        print('\nREFUSING: a leg could not be read. Try again when the broker '
              'answers — booking against an unknown book is guessing.')
        return 1
    if any(q for q in live.values()):
        print('\nREFUSING: a leg is still LIVE at the broker. Close it first, '
              'or use --reopen-frozen to put the trade back under the monitor.')
        return 1

    fills = {}
    if short_price is not None:
        fills['short_fill'] = float(short_price)
    if long_price is not None:
        fills['long_fill'] = float(long_price)
    if not fills:
        print('\nNo prices given. Re-run with --short-price/--long-price for '
              'what you actually filled at; this command will not invent one.')
        return 1

    exit_net = fills.get('long_fill', 0.0) - fills.get('short_fill', 0.0)
    entry = float(trade.get('net_debit') or trade.get('debit') or 0.0)
    exit_data = dict(fills)
    exit_data.update({
        'exit_date': now_ist().isoformat(),
        'exit_reason': (trade.get('close_failure') or {}).get('reason')
                       or 'MANUAL',
        'exit_spot': None,
        'exit_spread': exit_net,
        'pnl_per_share': exit_net - entry,
        'total_pnl': (exit_net - entry) * float(trade.get('quantity') or 0),
    })
    # A leg the operator did NOT price is UNKNOWN, never zero — the store's
    # `bound_bcs_exit` reads these Nones and marks the whole figure
    # approximate, which is exactly right for a hand-booked exit.
    for key in ('short_fill', 'long_fill'):
        exit_data.setdefault(key, None)

    # Through `begin_recovery`, not straight into `update_trade_exit`.
    # `partial_close` REFUSES a booking by contract — legs are live at the
    # broker and nothing may write an exit over them — and the one door out of
    # that state is the recovery lock. A hand-booking is still a booking, so it
    # walks the same state machine as the sweep's; skipping the lock raised
    # `ValueError: #7 status=partial_close, can't exit` on every real store.
    if not store.begin_recovery(tid, exit_data['exit_reason']):
        print('\nCould not take the lock on %s #%s — another process may be '
              'working on it. Nothing was booked.' % (label, tid))
        return 1
    store.update_trade_exit(tid, exit_data)
    cf = dict(trade.get('close_failure') or {})
    cf['state'] = 'resolved'
    try:
        store.update_trade_fields(tid, close_failure=cf)
    except Exception:
        pass
    # `format()`, not `%` — the thousands comma (`+,.0f`) is only valid in the
    # newer mini-language, and `%` raises `unsupported format character ','`
    # on the LAST line of a hand-booking, after the store has already been
    # written. The operator would see a traceback over a completed booking.
    pnl = '{:+,.0f}'.format(exit_data['total_pnl'])
    print('\n{} #{} booked: exit {:.2f} vs entry {:.2f}, P&L Rs {}'
          .format(label, tid, exit_net, entry, pnl))
    send_telegram('{} #{} {}: frozen close BOOKED BY HAND. Exit {:.2f}, '
                  'P&L Rs {}.'.format(label, tid, trade.get('stock', '?'),
                                      exit_net, pnl),
                  alert_class=alert_policy.SAFETY)
    return 0


def reopen_frozen(kite, ref):
    """Clear a frozen incident by RE-MONITORING it — for an intact spread.

    S7: the close failed before anything filled, so the position is exactly
    what the record says and its only real problem is that nothing is watching
    it. Putting it back under the monitor restores its stops.

    REFUSES if the broker shows a leg flat or inverted. A half-closed or
    flipped position must NOT go back into the open book: the monitor would
    then compute a spread value for legs that are not both there.
    """
    parsed = _parse_frozen_ref(ref)
    if not parsed:
        return 1
    label, store, tid = parsed
    trade = next((t for t in store.get_frozen_trades() if t['id'] == tid), None)
    if trade is None:
        print('%s #%s is not frozen (nothing at partial_close with that id).'
              % (label, tid))
        return 1

    print('\n%s #%s %s' % (label, tid, trade.get('stock', '?')))
    live = _show_broker_reality(kite, trade, label)
    expected = dict(_legs_of(trade))
    bad = [sym for sym, q in live.items()
           if q is None or q == 0 or (q > 0) != (expected[sym] > 0)]
    if bad:
        print('\nREFUSING: %s not live in the direction the record claims. '
              'This spread is not intact, so re-monitoring it would price a '
              'position that is not there. Use --book-frozen once it is flat.'
              % ', '.join(bad))
        return 1

    open_status = 'entered' if label == 'COHORT' else 'open'
    store.set_trade_status(tid, open_status, close_failure=None)
    print('\n%s #%s returned to %r and is monitored again.'
          % (label, tid, open_status))
    send_telegram('%s #%s %s: frozen close cleared by hand — the spread is '
                  'intact and back under the monitor.'
                  % (label, tid, trade.get('stock', '?')),
                  alert_class=alert_policy.SAFETY)
    return 0


def clear_residue(kite, ref):
    """Stop nagging about a residue the owner owns. Either species.

    The sweep resolves itself the moment the broker goes flat, so the ONLY
    reason to reach for this is the case the machine cannot infer: the leg is
    still live and that is deliberate - it was rolled, adopted into another
    position, or is being held. Refusing to clear it would leave a daily
    safety alert firing forever about a decision already made, and an alert
    nobody can end is one the reader learns to swipe away.

    So it clears while live - and RECORDS that it did. `cleared_while_live`
    keeps the leg and its quantity on the record, because the difference
    between "the residue went away" and "somebody silenced it" is exactly the
    fact a later reader needs and the one a bare `state: cleared` would lose.

    ONE ref clears whichever species is open on that record, and BOTH if both
    are: `BOOK:ID` is how the owner identifies the thing that is nagging them,
    and making them learn which of two incident names it was would be an
    interface built around this file's internals.
    """
    parsed = _parse_frozen_ref(ref)
    if not parsed:
        return 1
    label, store, tid = parsed
    cleared = 0
    missing = []
    for kind in RESIDUE_KINDS:
        try:
            pending = getattr(store, kind.lister)()
        except AttributeError:
            print('%s cannot list residues (%s: no %s).'
                  % (label, kind.field, kind.lister))
            continue
        trade = next((t for t in pending if t['id'] == tid), None)
        if trade is None:
            missing.append(kind.field)
            continue
        if _clear_one_residue(kite, store, label, tid, trade, kind):
            cleared += 1
    if not cleared:
        print('%s #%s has no unresolved residue (%s).'
              % (label, tid, ', '.join(missing) or 'none listable'))
        return 1
    return 0


def _clear_one_residue(kite, store, label, tid, trade, kind) -> bool:
    res = dict(trade.get(kind.field) or {})
    print('\n%s #%s %s - %s first seen %s'
          % (label, tid, trade.get('stock', '?'), kind.field,
             res.get('detected_at', '?')))
    live = _show_broker_reality(kite, trade, label)
    still = {sym: q for sym, q in live.items() if q}
    unread = [sym for sym, q in live.items() if q is None]

    res['state'] = 'cleared'
    res['cleared_at'] = now_ist().isoformat()
    res['cleared_while_live'] = ({'legs': still} if still else None)
    if unread:
        # Not the same as flat, and not the same as live. Say which it was.
        res['cleared_unreadable'] = unread
    try:
        store.update_trade_fields(tid, **{kind.field: res})
    except Exception as e:
        print('Could not write the record: %s' % e)
        return False
    _RECOVERY_NAGGED.discard((label, tid, kind.field, today_ist().isoformat()))

    if still:
        detail = '; '.join('%s net %+d' % (s_, q) for s_, q in still.items())
        print('\nCLEARED WHILE LIVE: %s' % detail)
        print('The nag stops. The leg does not - nothing here closed it, and '
              'nothing will monitor it.')
        send_telegram(
            '%s #%s %s: %s CLEARED BY HAND while still live (%s). No further '
            'alerts for this incident.'
            % (label, tid, trade.get('stock', '?'), kind.field, detail),
            alert_class=alert_policy.SAFETY)
    else:
        print('\nCleared. The broker shows nothing live on this record.')
    return True


#: Heading and closing note per species, for `list_residue_trades`.
RESIDUE_LISTING = {
    'reconcile_residue': (
        '=== POST-CLOSE RESIDUE (booked CLOSED, a leg is still live) ===',
        'No order is ever placed against these - the record is closed.'),
    'entry_residue': (
        '=== ENTRY RESIDUE (an entry left a leg no record describes) ===',
        'No order is ever placed against these - the entry path never '
        'unwinds.'),
}


def list_residue_trades():
    """Every unresolved residue, of either species, in every book.

    Printed by `--list-frozen` alongside the frozen book rather than behind a
    switch of its own. They are the same fact from the reader's side - a live
    leg with nothing watching it - and splitting them across two commands is
    how one of them ends up being the one nobody runs. Same argument, one
    level down, for listing both SPECIES here instead of adding a command.
    """
    for kind in RESIDUE_KINDS:
        _list_residues_of(kind)


def _list_residues_of(kind):
    rows = []
    for name in FROZEN_BOOKS:
        label, store = _frozen_book(name)
        if store is None:
            continue
        try:
            for t in getattr(store, kind.lister)():
                rows.append((label, t))
        except AttributeError:
            print('  %s: cannot list residues (%s: no %s)'
                  % (label, kind.field, kind.lister))
        except Exception as e:
            print('  %s: could not read residues (%s: %s)'
                  % (label, kind.field, e))
    if not rows:
        return
    heading, note = RESIDUE_LISTING[kind.field]
    print('\n' + heading)
    print('%-8s %-5s %-12s %-19s %s'
          % ('book', 'id', 'stock', 'detected', 'legs'))
    for label, t in rows:
        res = t.get(kind.field) or {}
        print('%-8s %-5s %-12s %-19s %s'
              % (label, t.get('id'), t.get('stock', '?'),
                 str(res.get('detected_at', '-'))[:19],
                 res.get('detail', '-')))
    print('\n' + note)
    print('Close the leg in Kite and it resolves itself on the next sweep,')
    print('or  --clear-residue BOOK:ID   if you are holding it on purpose.')


def list_frozen_trades():
    """Every frozen record in every book, with what it is owed.

    `get_frozen_trades` existed on one adapter and nothing called it. A method
    nobody invokes is indistinguishable from one that does not exist, which is
    how eight live positions once answered `Open: 0`.
    """
    rows = []
    for name in FROZEN_BOOKS:
        label, store = _frozen_book(name)
        if store is None:
            continue
        try:
            for t in store.get_frozen_trades():
                rows.append((label, t))
        except Exception as e:
            print('  %s: could not read frozen trades (%s)' % (label, e))
    if not rows:
        print('No frozen trades. (Nothing stuck at partial_close.)')
        list_residue_trades()
        return
    print('\n=== FROZEN (partial_close — live legs, NOT monitored) ===')
    print('%-8s %-5s %-12s %-12s %-11s %-9s %s'
          % ('book', 'id', 'stock', 'cause', 'state', 'attempts', 'frozen_at'))
    for label, t in rows:
        cf = t.get('close_failure') or {}
        print('%-8s %-5s %-12s %-12s %-11s %-9s %s'
              % (label, t.get('id'), t.get('stock', '?'),
                 cf.get('cause', '—'), cf.get('state', 'legacy'),
                 '%s/%s' % (cf.get('attempts', 0), RECOVERY_MAX_ATTEMPTS),
                 cf.get('frozen_at', '—')))
    print('\nClear with:  --book-frozen BOOK:ID --short-price X --long-price Y'
          '   (flat book)')
    print('             --reopen-frozen BOOK:ID'
          '                              (spread intact)')
    list_residue_trades()


def _count_residues(books, kind) -> int:
    """How many records carry an OPEN incident of this kind, across the fleet.

    Swallows per-book failures on purpose: this is a startup banner, and a
    book that cannot answer must not stop the monitor from watching the other
    three. The SWEEP is where an unanswerable book is reported, once per poll
    and by name.
    """
    n = 0
    for _label, store, _ in books:
        if store is None:
            continue
        try:
            n += len(getattr(store, kind.lister)())
        except Exception:
            pass
    return n


def _arming_preflight(dry_run: bool, all_trades) -> dict:
    """Announce the arming verdict for this session. Never raises.

    This engine knows its own `dry_run` exactly -- it is the switch that lives
    on a crontab line and in no config file -- so unlike zebra's copy there is
    no unknown here. It reads the other three switches from zebra's config,
    which is where they live.

    Startup-only, not per poll: this is a long-lived process and the switches
    it reads cannot change under it. The one that CAN change mid-session is
    the kill switch, which forces this engine into dry run for the rest of the
    session -- and that transition is announced by `announce_kill_switch`,
    which now says what it does to the arming state rather than leaving the
    reader to consult a table.
    """
    from common import arming
    try:
        from zebra import config as zcfg
        paper_mode = bool(zcfg.PAPER_MODE)
        exits_external = bool(zcfg.EXITS_MANAGED_EXTERNALLY)
        auto_entry = bool(zcfg.AUTO_ENTRY)
    except Exception as e:
        # Unreadable switches are not a reason to stop monitoring three books
        # that have nothing to do with the cohort. Said out loud, and skipped.
        log(f"  ARMING: could not read the zebra switches ({e}) — the arming "
            f"verdict is UNAVAILABLE this session.")
        return {'legal': True, 'faults': [], 'latent': [], 'warnings': [],
                'engines': {}, 'summary': 'unavailable'}
    population = set()
    for t in all_trades or ():
        if t.get('_store_type') != 'zebra':
            continue
        # Read from the RAW field, not through `_record_says_paper`. That
        # predicate resolves an absent flag to "live" on purpose, which is
        # correct for deciding whether THIS engine may trade the record and
        # wrong for deciding what the record IS -- zebra's twin resolves the
        # same absence the opposite way. Classifying it here would have both
        # preflights report OK on a record they disagree about.
        flag = t.get('paper')
        if not isinstance(flag, bool):
            population.add(arming.UNSTAMPED_RECORD)
            continue
        population.add(arming.PAPER_RECORD if _record_says_paper(t)
                       else arming.LIVE_RECORD)
    state = arming.check(
        paper_mode=paper_mode, exits_external=exits_external,
        auto_entry=auto_entry, dry_run=bool(dry_run),
        population=population, engine='bcs/spread_monitor.py',
        log=lambda line: log('  ' + line),
        telegram=lambda m: send_telegram(m, alert_class=alert_policy.SAFETY))
    return state


def monitor_all(kite: KiteConnect, dry_run: bool):
    """
    Monitor ALL open trades (BCS + BPS + Fallen Hero) in a single loop.
    Designed to be run as a scheduled task during market hours.

    BCS: 4-layer SL (SL_SPOT <=, SL_SPREAD, SL_TRAIL, TP >=)
    BPS: 4-layer SL (SL_SPOT >=, SL_SPREAD, SL_TRAIL, TP <=) — REVERSED direction
    FH:  spot-only SL (SL_SPOT >=) + expiry force-close
    """
    bcs_store = get_store()
    fh_store = get_fh_store()
    bps_store = get_bps_store()
    wl_store = get_watchlist_store()
    zebra_store = _open_zebra_store()
    set_log_file(LOG_DIR / f"spread_monitor_cron_{today_ist().strftime('%Y%m%d')}.log")

    # ── Recover trades stuck in 'closing' from a previous crash ─────────
    for label, store in [('BCS', bcs_store), ('BPS', bps_store), ('FH', fh_store),
                         ('COHORT', zebra_store)]:
        if store is None:
            continue
        for t in store.get_closing_trades():
            log(f"  RECOVERY: {label} #{t['id']} {t['stock']} stuck in 'closing'. Resetting to 'open'.")
            send_telegram(f"{label} #{t['id']} {t['stock']}: Recovered from 'closing' (previous crash). Re-monitoring.")
            store.recover_closing_trade(t['id'])

    all_trades = _load_all_trades(bcs_store, fh_store, bps_store, zebra_store)
    wl_active = wl_store.get_active()

    # B7: an empty book may mean "everything closed" or "the file was corrupt
    # and got quarantined". Those need opposite responses, and the second one
    # is the one that leaves live positions unwatched.
    corrupt = alert_store_corruption(
        [('BCS', bcs_store), ('BPS', bps_store), ('FH', fh_store)])

    # WHICH ENGINE MAY BOOK, stated before this one starts polling. The
    # combinations that leave a cohort record with no exit engine, or with
    # two, used to live only in a table in CLAUDE.md -- and the table went
    # stale, could not be consulted by either process, and described states
    # whose defining property is that every log looks healthy. See
    # `common/arming.py`. Announced from BOTH engines deliberately: these are
    # exactly the states where the other one may be absent.
    _arming_preflight(dry_run, all_trades)

    # First beat of the session, BEFORE the empty-book return below. A monitor
    # that started and found nothing to watch is alive; without this beat that
    # case is indistinguishable from one that never started, and zebra would
    # shout about a healthy morning. See `write_heartbeat`.
    _beat_all(('idle' if not all_trades and not wl_active else 'startup'),
              dry_run, all_trades, zebra_store)

    # FROZEN records are not in `all_trades` — dropping out of the open book is
    # the whole M14 defect — so they have to be counted separately HERE, or a
    # book whose only record is frozen returns "Nothing to monitor" and the
    # recovery sweep never runs. That is M14's own failure mode reproduced one
    # level up: a position live at the broker with dead stops, and a log line
    # saying there is nothing to watch.
    #: Labels come from `STORE_TYPE_LABEL` rather than being typed again here,
    #: so the sweep's `label` and the journal's `book` cannot drift apart.
    #: FH carries orders_allowed=False: it is traded by hand (owner,
    #: 2026-08-28) and this monitor places no FH order anywhere.
    frozen_books = [(STORE_TYPE_LABEL['bcs'], bcs_store, True),
                    (STORE_TYPE_LABEL['bps'], bps_store, True),
                    (STORE_TYPE_LABEL['zebra'], zebra_store, True),
                    (STORE_TYPE_LABEL['fh'], fh_store, False)]
    frozen_n = 0
    for _label, _store, _ in frozen_books:
        if _store is None:
            continue
        try:
            frozen_n += len(_store.get_frozen_trades())
        except Exception:
            pass

    if not all_trades and not wl_active and not frozen_n:
        if corrupt:
            log("Book is empty because a store was QUARANTINED — NOT exiting "
                "quietly. Alert sent; fix the store before the next session.")
            return
        log("No open trades and no active watchlist alerts. Nothing to monitor.")
        return
    if frozen_n:
        log(f"  {frozen_n} FROZEN record(s) at partial_close — legs may be "
            f"live at the broker. The recovery sweep runs every poll.")

    # S3. Counted and announced on the same footing as the frozen book: both
    # are live legs that nothing else in this process is watching, and the
    # only difference between them is which door they came out of.
    residue_n = _count_residues(frozen_books, POST_CLOSE_RESIDUE)
    if residue_n:
        log(f"  {residue_n} CLOSED record(s) with an unresolved post-close "
            f"RESIDUE — a leg is still live at the broker. Escalate-only: no "
            f"order will be placed against a closed record.")
    entry_residue_n = _count_residues(frozen_books, ENTRY_RESIDUE)
    if entry_residue_n:
        log(f"  {entry_residue_n} record(s) with an unresolved ENTRY RESIDUE "
            f"— an entry left a leg that is not part of any recorded "
            f"position, so no stop applies to it. Escalate-only: the entry "
            f"path never unwinds.")

    bcs_count = sum(1 for t in all_trades if t['_strategy'] == 'BCS')
    bps_count = sum(1 for t in all_trades if t['_strategy'] == 'BPS')
    fh_count = sum(1 for t in all_trades if t['_strategy'] == 'FH')
    wl_count = len(wl_active)

    log("")
    log("=" * 70)
    log("  SPREAD MONITOR — ALL OPEN TRADES (BCS + BPS + FH) + WATCHLIST")
    log("=" * 70)
    log(f"  Mode:   {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    log(f"  Trades: {len(all_trades)} open (BCS: {bcs_count}, BPS: {bps_count}, FH: {fh_count})")
    log(f"  Watchlist: {wl_count} active alert(s)")
    log(f"  Drive:  BCS={'on' if bcs_store._drive_enabled else 'off'} | BPS={'on' if bps_store._drive_enabled else 'off'} | FH={'on' if fh_store._drive_enabled else 'off'} | WL={'on' if wl_store._drive_enabled else 'off'}")
    log(f"  Poll:   Every {POLL_INTERVAL_SEC}s | Status every {STATUS_PRINT_INTERVAL_SEC}s")
    log("=" * 70)

    # Per-trade trailing SL state (BCS/BPS): {(strategy, trade_id): trail dict}
    trail_state = {}
    # Per-trade trigger confirmation counters + blind-mode state (in-memory;
    # reset on restart — a restart can only DELAY a value trigger)
    confirm_state = {}   # {(strategy, id): {'sl_spread': n, 'sl_trail': n}}
    blind_state = {}     # {(strategy, id): blind dict}
    # B8 — the independent source. Every other loss-side guard (reliability
    # gate, intrinsic floor, debounce, blind alert) reads the SAME order book,
    # so three identical stale prints confirm one another and trade. Spot is
    # the one source that cannot be wrong in the same way. Count SOURCES, not
    # checks. In-memory is correct here (unlike zebra, which persists it):
    # this is a long-lived 5s poll, not a cron process that exits each cycle.
    corrob_state = {}    # {(strategy, id): {'spot','spread','t'}}
    spot_blind_state = {}  # B12: {(strategy, id): blind dict} — spot, not spread
    abort_until = {}     # {(strategy, id): time.time() before which no valuation/TP closes}
    kill_switch_announced = False   # Telegram the disarm ONCE, not every poll
    for t in all_trades:
        strat = t['_strategy']
        lots_str = f"{t.get('lots', '?')}x{t.get('lot_size', '?')}"

        if strat == 'BCS':
            trail_state[trade_key(t)] = new_trail_state(t)
            _ts = trail_state[trade_key(t)]
            if _ts['active']:
                # The RECONCILED values, not the record's own. For a cohort
                # record the peak is the max of this engine's `trail_peak` and
                # zebra's `mfe_mid` (`restored_peak`), so printing the stored
                # field would report a lower peak than the one the trail is
                # actually running on -- and the line exists to be checked
                # against the record by hand.
                log(f"  BCS #{t['id']} {t['stock']}: Restored trail: "
                    f"peak={_ts['peak']:.2f}, trail={_ts['trail']:.2f}"
                    f"{' (peak from the cohort book)' if _ts['peak'] > float(t.get('trail_peak') or 0) else ''}")
            log(f"  BCS #{t['id']} {t['stock']} {t['long_symbol']}/{t['short_symbol']} "
                f"| Lots: {lots_str} "
                f"| TP: {t['target_spot']} | SL: {t['sl_spot']} | SL Spread: {t['sl_spread']:.2f}")
        elif strat == 'BPS':
            trail_state[trade_key(t)] = new_trail_state(t)
            _ts = trail_state[trade_key(t)]
            if _ts['active']:
                # The RECONCILED values, not the record's own. For a cohort
                # record the peak is the max of this engine's `trail_peak` and
                # zebra's `mfe_mid` (`restored_peak`), so printing the stored
                # field would report a lower peak than the one the trail is
                # actually running on -- and the line exists to be checked
                # against the record by hand.
                log(f"  BPS #{t['id']} {t['stock']}: Restored trail: "
                    f"peak={_ts['peak']:.2f}, trail={_ts['trail']:.2f}"
                    f"{' (peak from the cohort book)' if _ts['peak'] > float(t.get('trail_peak') or 0) else ''}")
            log(f"  BPS #{t['id']} {t['stock']} {t['long_symbol']}/{t['short_symbol']} "
                f"| Lots: {lots_str} "
                f"| TP: spot<={t['target_spot']} | SL: spot>={t['sl_spot']} | SL Spread: {t['sl_spread']:.2f}")
        else:
            # FH: credit strategy, spot-only SL (reversed direction)
            log(f"  FH  #{t['id']} {t['stock']} SC:{t['short_call_symbol']} SP:{t['short_put_symbol']} "
                f"LP:{t['long_put_symbol']}"
                f"{' LC:' + t['long_call_symbol'] if t.get('long_call_symbol') else ''} "
                f"| Lots: {lots_str} "
                f"| SL: spot>={t['sl_spot']} | BE: {t['breakeven']} | Credit: {t['total_credit']:.2f}")

    log("")
    log("Verifying all positions in Kite...")
    # Unwrapped, this raise exits main() -> cron restarts in 5 min -> dies
    # again -> all day, with no alert. The startup path had no handler at all,
    # so a token that was already dead at 09:15 produced a silent restart loop
    # rather than the "UNMONITORED" Telegram the in-loop path would have sent.
    try:
        positions = kite.positions()['net']
    except Exception as e:
        stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in all_trades)
        if _is_auth_error(e):
            log(f"FATAL: Kite auth failed at startup: {e}")
            send_telegram(f"🔴 BCS MONITOR CANNOT START\nKite auth failed "
                          f"at startup ({e}).\nUNMONITORED: {stocks}\n"
                          f"Cron will keep retrying every 5 min and keep "
                          f"failing until the token is refreshed.", alert_class=alert_policy.SAFETY)
            return
        # Not auth: the verification is a nicety, the monitoring is not. Carry
        # on watching rather than abandoning live positions over a hiccup.
        log(f"WARNING: startup position verification failed ({e}) — "
            f"continuing to monitor")
        send_telegram(f"⚠️ BCS MONITOR: startup position check failed ({e}). "
                      f"Monitoring continues for: {stocks}")
        positions = []
    for t in all_trades:
        strat = t['_strategy']
        warn, msg = position_check_line(t, strat, positions)
        log(("  WARNING: " if warn else "  ") + msg)

    # ── Expiry day warnings ──────────────────────────────────────────────
    # Values are the three-state attempt dicts from `new_time_stop_state`, not
    # bare True. Membership still means "armed today"; the value carries
    # whether the close has been discharged, which is what M11 restored.
    expiry_trades = {}  # keyed by trade_key(), like every other state dict
    # Same F7 batching as the poll loop. This runs at 09:00 alongside the
    # positions fetch and the store syncs, so an unbatched burst here is the
    # one most likely to open the session already inside a 429 cooldown.
    prefetch_book(kite, all_trades)
    for t in all_trades:
        _store = _get_store_for(t, bcs_store, fh_store, bps_store, zebra_store)
        if arm_time_stop(t, trade_key(t), expiry_trades, t['_strategy'],
                         store=_store, spot=_arming_spot(kite, t)):
            continue
        # Not expiry day, but close enough that physical-delivery margin is
        # about to build. Every strategy here is physically settled, so all of
        # them get the warning. Alert only — the force-close above remains the
        # ONLY automated close this file does on expiry proximity.
        try:
            _spot = get_spot(kite, t['spot_symbol'])
            _sv = None
            if t.get('_strategy') in ('BCS', 'BPS'):
                _sv = get_spread_value(kite, t, spot=_spot).get('spread')
            maybe_warn_expiry_proximity(
                _store, t, _spot,
                t.get('_strategy', '?'), spread_val=_sv)
            # M10. Asked from E-9, three sessions ahead of the scheduled
            # close, so a CLOSE_NOW verdict still has room to be acted on by a
            # human. Alert-only and monotonic; it changes no stop.
            record_delivery_preflight(
                _store, t, delivery_preflight(t, _spot, _sv),
                t.get('_strategy', '?'))
        except Exception as e:
            log(f"  WARNING: expiry-proximity check failed for "
                f"#{t['id']} {t.get('stock')}: {e}")

    log(f"\nCron monitoring started at {now_ist().strftime('%H:%M:%S')}...")
    if not is_market_settled():
        log(f"  Market-open buffer active: SL/TP checks delayed until {MARKET_OPEN_BUFFER_SEC}s after open")
    log("")

    last_status_time = 0
    closing_in_progress = {}  # {(strategy, trade_id): reason}
    malformed_alerted = set()  # (strategy, id) already shouted about
    consecutive_errors = 0

    while True:
        try:
            if not is_market_open():
                now_t = now_ist().time()
                if now_t > MARKET_CLOSE:
                    log("Market closed for the day. Cron monitor exiting.")
                    for (store_key, strat_key, tid_key), ts in trail_state.items():
                        if ts['active']:
                            log(f"  {strat_key} #{tid_key} ({store_key}) trail "
                                f"state: peak={ts['peak']:.2f}, "
                                f"trail={ts['trail']:.2f}")
                    # A finished session and a crashed one leave the same
                    # silence behind. Stamp which this was.
                    _beat_all('closed', dry_run, all_trades, zebra_store,
                              kill_switch=kill_switch_announced)
                    return
                log(f"Market not open yet ({now_t.strftime('%H:%M')}). Waiting...")
                # The cron line starts this process at 09:00; without a beat
                # here the only stamp for the whole pre-open wait is the
                # startup one, which ages past the staleness window on a slow
                # morning.
                _beat_all('waiting', dry_run, all_trades, zebra_store,
                          kill_switch=kill_switch_announced)
                time.sleep(30)
                continue

            # Periodic Drive sync (picks up trades added from other machines)
            bcs_store.maybe_sync()
            bps_store.maybe_sync()
            fh_store.maybe_sync()
            if zebra_store is not None:
                # Same reason as the other three: the cohort book is written
                # by the zebra cron on this same machine, so a monitor holding
                # a stale copy would manage a position that has already moved.
                zebra_store.maybe_sync()
            wl_store.maybe_sync()
            all_trades = _load_all_trades(bcs_store, fh_store, bps_store, zebra_store)
            wl_active = wl_store.get_active()

            # Recomputed every poll for the same reason as the startup guard:
            # a frozen record is NOT in `all_trades`, so without this the loop
            # exits the moment the last open trade closes — abandoning any
            # position that froze earlier in the session, mid-recovery,
            # with its stops dead. The startup guard alone was not enough; a
            # book can also go frozen-only DURING a session, which is in fact
            # the likelier way to get there.
            frozen_n = 0
            for _label, _store, _ in frozen_books:
                if _store is None:
                    continue
                try:
                    frozen_n += len(_store.get_frozen_trades())
                except Exception:
                    pass

            if not all_trades and not wl_active and not frozen_n:
                # A book that went empty MID-SESSION is far more suspicious
                # than one that started empty: the trades were there at 09:15.
                if alert_store_corruption([('BCS', bcs_store),
                                           ('BPS', bps_store),
                                           ('FH', fh_store)]):
                    log("Book emptied MID-SESSION by a store QUARANTINE. "
                        "Alert sent. Exiting — there is nothing left to read.")
                    _beat_all('idle', dry_run, all_trades, zebra_store,
                              kill_switch=kill_switch_announced)
                    return
                log("All trades closed and no active watchlist alerts. Cron monitor exiting.")
                _beat_all('idle', dry_run, all_trades, zebra_store,
                          kill_switch=kill_switch_announced)
                return

            now = time.time()
            # BEFORE reading is_spread_settled(), so a re-arm takes effect on
            # this very iteration instead of one poll late — the poll right
            # after a blackout is the one most likely to see a torn book.
            note_poll(True, now)
            settled = is_market_settled()
            spread_settled = is_spread_settled(now)
            # NOTE: `consecutive_errors` is NOT reset here. It used to be, and
            # that made MAX_CONSECUTIVE_ERRORS unreachable — the counter read
            # "we reached the top of the loop", not "an iteration succeeded",
            # so any per-iteration raise went 0->1 forever and neither the
            # 10-error warning nor the fatal Telegram could ever fire. It is
            # now reset at the BOTTOM, after the whole iteration completes.

            # ── Live-order kill switch ─────────────────────────────────────
            # Checked every poll so it can stop a monitor already running.
            # It forces DRY RUN rather than returning: the positions still
            # need watching, and the alerts still need sending — what is
            # withheld is the order. One-way for the session; re-arming means
            # a restart, so a fat-fingered config cannot silently re-arm the
            # money path mid-session.
            if not dry_run and not trading_enabled():
                dry_run = True
                which = ', '.join(
                    name for name, ok in (
                        (SWITCH_FILE.name, _switch_says(SWITCH_FILE)),
                        ('bcs_config (tracked defaults + overlay)',
                         _bcs_config_says()))
                    if not ok) or '(re-read says armed)'
                log(f"KILL SWITCH: trading.enabled=false in {which} — "
                    "forcing DRY RUN for the rest of this session. Positions "
                    "are still monitored and alerted; no orders will be placed.")
                if not kill_switch_announced:
                    kill_switch_announced = True
                    send_telegram(
                        "⛔ BCS MONITOR DISARMED\n"
                        f"trading.enabled=false in {which}.\n"
                        "Positions are still watched and alerted, but "
                        "NO EXIT ORDERS WILL BE PLACED - you must close "
                        "by hand.\nRe-arm: set it back to true and "
                        "restart the monitor.", alert_class=alert_policy.SAFETY)
                    # THE STATE THIS TRANSITION JUST CREATED, computed
                    # rather than left to a table. Tripping the kill switch to
                    # be SAFE is how the no-engine state is most often
                    # reached: this process does not stop, it stops BOOKING,
                    # and if zebra has already stood down then nothing books
                    # at all while both logs keep looking healthy. Re-run
                    # because the verdict is a function of `dry_run`, which
                    # the line above just changed.
                    _arming_preflight(dry_run, all_trades)

            # AFTER the kill-switch block, never before: a beat claiming this
            # engine can book, written on the very poll the switch forced it
            # to DRY RUN, is worse than no beat at all. That combination
            # (zebra stood down + this engine unable to place an order) is the
            # exact silent state the heartbeat exists to expose.
            _beat_all('polling', dry_run, all_trades, zebra_store,
                      kill_switch=kill_switch_announced)
            print_status = (now - last_status_time >= STATUS_PRINT_INTERVAL_SEC)

            # ── F7: one batched read for the WHOLE book, once per poll ─────
            # Before this line the loop below issued 1 `/ltp` + 2 `/quote` per
            # BCS/BPS record (3-4 quotes for an FH one) EVERY five seconds. At
            # 8 positions that is 24 requests/poll against a 1 req/s family,
            # and the 2026-08-28 log has 58 `Too many requests` failures to
            # show for it — each one a position unwatched for that poll.
            # `/quote` and `/ltp` take 500 instruments apiece, so the whole
            # book costs 2 requests no matter how big it gets. Everything
            # below reads through the cache this fills; nothing below changed.
            _pf = prefetch_book(kite, all_trades)
            if print_status:
                log(f"  [quotes] {_pf} batched request(s) for "
                    f"{len(all_trades)} position(s) "
                    f"({quote_stats['cache_hits']} served from cache "
                    f"since start, {quote_stats['singles']} unbatched)")

            for trade in all_trades:
                # ── Per-record isolation ───────────────────────────────────
                # One malformed record used to halt exit checks for the ENTIRE
                # live book, silently and forever: a missing key raised out of
                # this loop body, the outer handler slept 10s and retried, and
                # `consecutive_errors = 0` above runs BEFORE the loop on every
                # pass — so the counter went 0->1 forever and neither the
                # warning nor the MAX_CONSECUTIVE_ERRORS fatal Telegram could
                # ever be reached. Every trade sorted after the bad one got
                # zero exit checks, witnessed only by a log line every 10s.
                # zebra fixed this class months ago (one try per record); the
                # live-money file never got the fix.
                bad = _malformed_reason(trade)
                if bad:
                    _alert_malformed_record(trade, bad, malformed_alerted)
                    continue

                tid = trade['id']
                stock = trade['stock']
                strat = trade['_strategy']
                sl_spot_val = trade['sl_spot']
                trade_store = _get_store_for(trade, bcs_store, fh_store, bps_store, zebra_store)

                # Skip trades where close is already in progress.
                # Keyed by (store, strategy, id): each book numbers its trades
                # independently, so id alone collides across books. `strat` is
                # in the key too but cannot carry it alone — the zebra store
                # produces both BCS and BPS records, so two of its rows can
                # share a strategy while a bcs-store row shares an id.
                close_key = trade_key(trade)
                # Outside the BCS/BPS branch below on purpose: FH reads spot
                # too, and FH's only stop IS a spot stop, so a spot-blind FH
                # record is the most completely unmanaged case of all.
                if close_key not in spot_blind_state:
                    spot_blind_state[close_key] = new_blind_state()
                if close_key in closing_in_progress:
                    if print_status:
                        log(f"  {strat} #{tid} {stock}: CLOSE IN PROGRESS ({closing_in_progress[close_key]}). Skipping.")
                    continue

                # ── BCS/BPS-specific fields ────────────────────────────────
                if strat in ('BCS', 'BPS'):
                    target = trade['target_spot']
                    sl_spread_val = trade['sl_spread']
                    entry_net = trade['net_debit']

                    # Initialize state for new trades added mid-session
                    if close_key not in trail_state:
                        trail_state[close_key] = new_trail_state(trade)
                        arm_time_stop(trade, close_key, expiry_trades,
                                      strat, note='added mid-session',
                                      store=trade_store,
                                      spot=_arming_spot(kite, trade))
                    if close_key not in confirm_state:
                        confirm_state[close_key] = {'sl_spread': 0, 'sl_trail': 0}
                    if close_key not in blind_state:
                        blind_state[close_key] = new_blind_state()
                    if close_key not in corrob_state:
                        corrob_state[close_key] = {}
                else:
                    # FH: check for new expiry-day trades added mid-session
                    arm_time_stop(trade, close_key, expiry_trades, 'FH',
                                  note='added mid-session', store=trade_store,
                                  spot=_arming_spot(kite, trade))

                # B9 + B12 — two failure classes, opposite responses.
                #
                # This handler used to swallow BOTH with `log(); continue`.
                # That made the outer handler's token check UNREACHABLE from
                # the first Kite call of every poll: the token could die at
                # 11:00 with open positions and produce nothing but log lines
                # until 15:30 — every stop dark, no Telegram, and on expiry day
                # the 15:15 force-close never firing, i.e. a physical delivery
                # obligation. Meanwhile a merely renamed spot_symbol was
                # equally silent.
                try:
                    spot = get_spot(kite, trade['spot_symbol'])
                except Exception as e:
                    if _is_auth_error(e):
                        # GLOBAL failure. Re-raise so the outer handler — which
                        # already builds the "UNMONITORED: {stocks}" alert and
                        # returns — actually sees it. Reuse that escalation
                        # rather than building a second one here.
                        raise
                    # PER-TRADE failure. Every other record is fine, so isolate
                    # this one and escalate on its own clock.
                    log(f"  {strat} #{tid} {stock}: spot fetch failed: {e}")
                    track_spot_blindness(
                        spot_blind_state[close_key], spot_ok=False,
                        reason=str(e)[:80], label=f"{strat} #{tid} {stock}")
                    continue
                track_spot_blindness(
                    spot_blind_state[close_key], spot_ok=True, reason='',
                    label=f"{strat} #{tid} {stock}")

                # ── BCS/BPS: Fetch spread + update trailing SL ────────────
                spread_val = None
                spread_data = None
                spread_fail = None
                fh_val = None
                if strat in ('BCS', 'BPS'):
                    try:
                        # `spot=` is what arms the no-arbitrage floor inside
                        # get_spread_value. Every other call site passes it;
                        # this one — the loop that actually runs on the Pi —
                        # did not, so the floor was inert in production while
                        # its unit tests passed. Guard class 2 (possibility)
                        # from the incident review: a tidy, two-sided, tight
                        # book can still quote an impossible price.
                        spread_data = get_spread_value(kite, trade, spot=spot)
                        spread_val = spread_data['spread']
                        # B8 — the independent cross-check, ported from the
                        # `spot_corroborates(corrob, ...)` call inside
                        # `monitor()` (named rather than by line number, which
                        # drifts). This is the direct descendant of the
                        # NHPC loss and the ONLY guard here reading a source
                        # other than the order book. Its absence from the cron
                        # path meant the 3-poll debounce and the re-verify both
                        # interrogated the same book, so a tidy-but-wrong quote
                        # confirmed itself three times and traded.
                        #
                        # Placement is load-bearing: inside the try and BEFORE
                        # blind_reason is computed below, so a veto counts as
                        # BLIND and the existing track_spread_blindness
                        # Telegrams it. Moving it later silences the alert.
                        #
                        # VETO-ONLY. Setting spread_val=None suppresses
                        # SL_SPREAD, SL_TRAIL and the trail-peak update (a
                        # garbage high must not poison the persisted peak). It
                        # does not touch SL_SPOT or TP, which are spot-driven.
                        # It can therefore only ever PREVENT an exit, never
                        # cause one.
                        if spread_val is not None:
                            ok, why = spot_corroborates(
                                corrob_state[close_key], spot, spread_val)
                            if not ok:
                                log(f"  {strat} #{tid} {stock} QUOTE GUARD: "
                                    f"{why} — valuation rejected")
                                spread_data['unreliable'] = why
                                spread_val = None
                    except Exception as e:
                        spread_fail = str(e)

                    # Blind-mode tracking: alert when spread SLs are suspended
                    blind_reason = spread_fail or (spread_data['unreliable'] if spread_data else 'no_data')
                    track_spread_blindness(
                        blind_state[close_key],
                        spread_ok=(spread_val is not None or not spread_settled),
                        reason=blind_reason, spot=spot, sl_spot=sl_spot_val,
                        adverse_below=(strat == 'BCS'),
                        label=f"{strat} #{tid} {stock}")

                    # Trail update: jump-gated, reliable quotes only, longer
                    # spread buffer (2026-07-24 guard design)
                    ts = trail_state[close_key]
                    if spread_settled and spread_val is not None:
                        was_active = ts['active']
                        if update_trail(ts, spread_val):
                            verb = "TRAIL UPDATED" if was_active else "TRAIL ENGAGED"
                            log(f"  {strat} #{tid} {stock} ** {verb} ** peak={ts['peak']:.2f} | trail={ts['trail']:.2f}")
                            trade_store.update_trade_fields(tid, trail_active=True,
                                                            trail_peak=ts['peak'], trail_sl=ts['trail'])
                        elif ts['cand_count'] > 0:
                            log(f"  {strat} #{tid} {stock}: trail peak candidate {spread_val:.2f} "
                                f"held (confirm {ts['cand_count']}/{SL_CONFIRM_POLLS})")
                else:
                    # FH: Fetch position value for status display only
                    try:
                        fh_val = get_fh_position_value(kite, trade)
                    except Exception as e:
                        if print_status:
                            log(f"  FH #{tid} {stock}: value fetch failed: {e}")

                # ── Status line ───────────────────────────────────────────
                if print_status:
                    settle_tag = "" if settled else " [COOLDOWN]"
                    if settled and not spread_settled and strat in ('BCS', 'BPS'):
                        settle_tag = " [SPREAD-COOLDOWN]"
                    if strat in ('BCS', 'BPS'):
                        if spread_data and spread_data.get('unreliable'):
                            settle_tag += f" [SUSPECT {spread_data['unreliable']}]"
                        elif spread_fail:
                            settle_tag += f" [QUOTE-FAIL {spread_fail[:40]}]"
                    expiry_tag = (" [EXPIRY]" if close_key in expiry_trades
                                  else "") + _tp_latch_tag(trade)
                    try:
                        if strat == 'BCS':
                            if spread_data and spread_val is not None:
                                unrealized = (spread_val - entry_net) * trade['quantity']
                                ts = trail_state[close_key]
                                trail_str = f" | Trail: {ts['trail']:.2f}" if ts['active'] else ""
                                log(
                                    f"  BCS #{tid} {stock}: Spot={spot:.2f} | "
                                    f"TP:{target}({target - spot:>+.1f}) | "
                                    f"SL:{sl_spot_val}({spot - sl_spot_val:>+.1f}) | "
                                    f"Spread:{spread_val:.2f} | "
                                    f"P&L: Rs {unrealized:>+,.0f}{trail_str}{settle_tag}{expiry_tag}"
                                )
                            else:
                                log(f"  BCS #{tid} {stock}: Spot={spot:.2f} | TP:{target}({target - spot:>+.1f}) | SL:{sl_spot_val}({spot - sl_spot_val:>+.1f}){settle_tag}{expiry_tag}")
                        elif strat == 'BPS':
                            # BPS status: REVERSED — gap to target = spot - target (positive = above target = not yet profitable)
                            # Buffer above SL = sl_spot - spot (positive = below SL = safe)
                            gap_to_target = spot - target  # positive when above target
                            buf_above_sl = sl_spot_val - spot  # positive when below SL = safe
                            if spread_data and spread_val is not None:
                                unrealized = (spread_val - entry_net) * trade['quantity']
                                ts = trail_state[close_key]
                                trail_str = f" | Trail: {ts['trail']:.2f}" if ts['active'] else ""
                                log(
                                    f"  BPS #{tid} {stock}: Spot={spot:.2f} | "
                                    f"TP:{target}(gap:{gap_to_target:>+.1f}) | "
                                    f"SL:{sl_spot_val}(buf:{buf_above_sl:>+.1f}) | "
                                    f"Spread:{spread_val:.2f} | "
                                    f"P&L: Rs {unrealized:>+,.0f}{trail_str}{settle_tag}{expiry_tag}"
                                )
                            else:
                                log(f"  BPS #{tid} {stock}: Spot={spot:.2f} | TP:{target}(gap:{gap_to_target:>+.1f}) | SL:{sl_spot_val}(buf:{buf_above_sl:>+.1f}){settle_tag}{expiry_tag}")
                        else:
                            # FH status: spot vs SL (upside), breakeven, P&L
                            be = trade['breakeven']
                            buf_to_sl = sl_spot_val - spot  # positive = safe buffer
                            if fh_val and fh_val['pnl_per_share'] is not None:
                                unrealized = fh_val['pnl_per_share'] * trade['quantity']
                                log(
                                    f"  FH  #{tid} {stock}: Spot={spot:.2f} | "
                                    f"SL:{sl_spot_val}(buf:{buf_to_sl:>+.1f}) | "
                                    f"BE:{be} | "
                                    f"P&L: Rs {unrealized:>+,.0f}{settle_tag}{expiry_tag}"
                                )
                            else:
                                log(f"  FH  #{tid} {stock}: Spot={spot:.2f} | SL:{sl_spot_val}(buf:{buf_to_sl:>+.1f}) | BE:{be}{settle_tag}{expiry_tag}")
                    except Exception:
                        log(f"  {strat} #{tid} {stock}: Spot={spot:.2f}{settle_tag}")

                # ── TIME STOP: force close from EXPIRY_FORCE_CLOSE_TIME ──
                # The outer test suppresses every other trigger for an armed
                # trade past the force-close clock, exactly as before. The
                # inner one decides whether an ATTEMPT is owed — the two were
                # the same question until M11, which is how a close that never
                # filled read as a close that did.
                if close_key in expiry_trades and time_stop_window_open():
                    ts_state = expiry_trades[close_key]
                    if (close_key not in closing_in_progress
                            and time_stop_attempt_due(ts_state)):
                        attempt = ts_state.get('attempts', 0) + 1
                        log(f"\n  {strat} #{tid} {stock} *** EXPIRY FORCE CLOSE "
                            f"(attempt {attempt}/{TIME_STOP_MAX_ATTEMPTS}): "
                            f"{now_ist().strftime('%H:%M')} >= {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
                        closing_in_progress[close_key] = "EXPIRY_FORCE_CLOSE"
                        if strat == 'BCS':
                            success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run,
                                                   store=trade_store, strategy_label='BCS')
                        elif strat == 'BPS':
                            success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run,
                                                   store=trade_store, strategy_label='BPS')
                        else:
                            success = close_fh_position(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run)
                        outcome = record_time_stop_result(
                            ts_state, success, trade, trade_store, strat)
                        if outcome == 'closed':
                            trail_state.pop(close_key, None)
                        else:
                            # RELEASE the in-progress lock so the retry can run
                            # on a later poll THIS session. Safe by structure,
                            # not by inspection: every close failure past the
                            # close-lock freezes the record at `partial_close`,
                            # which drops out of `get_open_trades`, so the next
                            # `_load_all_trades` cannot hand this loop a frozen
                            # position. Only a close that touched nothing —
                            # the late-day guard, an abort — comes back.
                            closing_in_progress.pop(close_key, None)
                    continue

                # ── SL/TP checks ─────────────────────────────────────────
                closed = False

                if strat in ('BCS', 'BPS'):
                    # ── SL_SPOT: direction-aware, exempt from every cooldown
                    # (a thesis-dead exit must not wait for a book to form),
                    # but NOT single-poll. SL_SPOT_CONFIRM_POLLS=2 (~10s) was
                    # added after the two real-money losses because this is the
                    # trigger that runs at URGENT urgency, whose final attempt
                    # pays through uncapped — and both losses were opening
                    # prints. The debounce was wired into `monitor()`, the
                    # single-trade mode nobody runs, while `monitor_all()` —
                    # the --cron entrypoint on the Pi — kept firing on ONE
                    # print. The constant and its unit test both passed the
                    # whole time. BCS risk is DOWN (spot <= sl), BPS risk is UP.
                    #
                    # ...unless this record's owner switched the spot stop
                    # OFF. `spot_sl_enabled` is a property of the TRADE, not
                    # of this engine, because a position can be managed here
                    # while having been measured elsewhere. The BCS cohort
                    # runs with it False: over 147 records a 3% spot stop cut
                    # 31 of 78 winners for a Rs 8.9L giveaway, while the
                    # losers it "caught" only halved their depth. In that book
                    # spot is a VETO on an exit the option book asked for,
                    # never a reason to ask for one.
                    #
                    # Absent means True, so the three original books are
                    # untouched. Only the REPORTED number survives the switch:
                    # the blind alert below still says where spot sits against
                    # sl_spot, which is the whole point of keeping it stored.
                    sl_spot_armed = trade.get('spot_sl_enabled', True) is not False
                    sl_spot_hit = sl_spot_armed and (
                        (spot <= sl_spot_val) if strat == 'BCS'
                        else (spot >= sl_spot_val))
                    if sl_spot_hit:
                        # confirm_state[...] directly: the `confirm` local is
                        # bound below, after the TP cooldown gate.
                        n_sl = bump_confirm(confirm_state[close_key], 'sl_spot')
                        if n_sl < SL_SPOT_CONFIRM_POLLS:
                            op = '<=' if strat == 'BCS' else '>='
                            log(f"  {strat} #{tid} {stock}: SL_SPOT condition "
                                f"{spot:.2f} {op} {sl_spot_val} "
                                f"(confirm {n_sl}/{SL_SPOT_CONFIRM_POLLS})")
                            sl_spot_hit = False
                    if sl_spot_hit:
                        op = '<=' if strat == 'BCS' else '>='
                        log(f"\n  {strat} #{tid} {stock} *** SL_SPOT HIT: {spot:.2f} {op} {sl_spot_val} "
                            f"(confirmed x{SL_SPOT_CONFIRM_POLLS}) ***")
                        closing_in_progress[close_key] = "SL_SPOT"
                        success = close_spread(kite, trade, spot, "SL_SPOT", dry_run,
                                               store=trade_store, strategy_label=strat,
                                               quote=spread_data)
                        if success == 'ABORT':
                            closing_in_progress.pop(close_key, None)
                        else:
                            closed = True
                            if not success:
                                log(f"  {strat} #{tid} {stock}: Close failed. Trade locked — manual intervention needed.")
                                send_telegram(f"{strat} #{tid} {stock}: SL_SPOT close FAILED. Manual intervention needed!", alert_class=alert_policy.SAFETY)

                    # Cooldown gate: TP needs settled market
                    if not closed and not settled:
                        continue

                    confirm = confirm_state[close_key]
                    ts = trail_state[close_key]
                    # Abort cooldown: after an aborted close, hold off further
                    # valuation/TP attempts for this trade (SL_SPOT exempt)
                    in_abort_cooldown = time.time() < abort_until.get(close_key, 0)

                    # CHECK 2: SL_SPREAD — spread-settled market + debounce +
                    # re-verify (2026-07-24: one garbage poll = Rs 7,297 lost).
                    # Counter semantics: reliable trigger poll increments
                    # (stale-aware), reliable non-trigger poll resets,
                    # UNRELIABLE poll freezes — a flickering book must not
                    # indefinitely block a genuine exit.
                    if not closed and not in_abort_cooldown and spread_settled and spread_val is not None and spread_val <= sl_spread_val:
                        n = bump_confirm(confirm, 'sl_spread')
                        if n < SL_CONFIRM_POLLS:
                            log(f"  {strat} #{tid} {stock}: SL_SPREAD condition {spread_val:.2f} <= "
                                f"{sl_spread_val:.2f} (confirm {n}/{SL_CONFIRM_POLLS})")
                        else:
                            log(f"\n  {strat} #{tid} {stock} *** SL_SPREAD HIT: {spread_val:.2f} <= "
                                f"{sl_spread_val:.2f} (confirmed {n}x) ***")
                            closing_in_progress[close_key] = "SL_SPREAD"
                            success = close_spread(kite, trade, spot, "SL_SPREAD", dry_run,
                                                   store=trade_store, strategy_label=strat,
                                                   reverify_sl=sl_spread_val,
                                                   quote=spread_data)
                            if success == 'ABORT':
                                closing_in_progress.pop(close_key, None)
                                confirm['sl_spread'] = 0
                                abort_until[close_key] = time.time() + ABORT_COOLDOWN_SEC
                            else:
                                closed = True
                                if not success:
                                    log(f"  {strat} #{tid} {stock}: Close failed — manual intervention needed.")
                                    send_telegram(f"{strat} #{tid} {stock}: SL_SPREAD close FAILED. Manual intervention needed!", alert_class=alert_policy.SAFETY)
                    elif not closed and spread_val is not None and spread_val > sl_spread_val:
                        confirm['sl_spread'] = 0

                    # CHECK 3: SL_TRAIL — same guards as SL_SPREAD
                    if not closed and not in_abort_cooldown and spread_settled and ts['active'] and spread_val is not None and spread_val <= ts['trail']:
                        n = bump_confirm(confirm, 'sl_trail')
                        if n < SL_CONFIRM_POLLS:
                            log(f"  {strat} #{tid} {stock}: SL_TRAIL condition {spread_val:.2f} <= "
                                f"{ts['trail']:.2f} (confirm {n}/{SL_CONFIRM_POLLS})")
                        else:
                            log(f"\n  {strat} #{tid} {stock} *** SL_TRAIL HIT: {spread_val:.2f} <= "
                                f"{ts['trail']:.2f} (confirmed {n}x) ***")
                            closing_in_progress[close_key] = "SL_TRAIL"
                            success = close_spread(kite, trade, spot, "SL_TRAIL", dry_run,
                                                   store=trade_store, strategy_label=strat,
                                                   reverify_sl=ts['trail'],
                                                   quote=spread_data)
                            if success == 'ABORT':
                                closing_in_progress.pop(close_key, None)
                                confirm['sl_trail'] = 0
                                abort_until[close_key] = time.time() + ABORT_COOLDOWN_SEC
                            else:
                                closed = True
                                if not success:
                                    log(f"  {strat} #{tid} {stock}: Close failed — manual intervention needed.")
                                    send_telegram(f"{strat} #{tid} {stock}: SL_TRAIL close FAILED. Manual intervention needed!", alert_class=alert_policy.SAFETY)
                    elif not closed and spread_val is not None and (not ts['active'] or spread_val > ts['trail']):
                        confirm['sl_trail'] = 0

                    # CHECK 4: TP — spot-based (BCS: spot >= target rising;
                    # BPS: spot <= target dropping). ABORT re-fires after the
                    # cooldown since the spot condition persists.
                    tp_hit = (spot >= target) if strat == 'BCS' else (spot <= target)
                    # THE LATCH. The first touch arms this exit permanently —
                    # written to the RECORD before the vet and before any
                    # order, so it survives a defer, a restart and a spot that
                    # has since backed off (COFORGE #436, 2026-08-27). Called
                    # unconditionally, not inside the `if`, so the touch is
                    # captured even on a poll the abort cooldown is holding:
                    # the cooldown delays the close, it does not un-see the
                    # trigger.
                    tp_go = (not closed) and tp_armed(
                        trade, tp_hit, spot, store=trade_store,
                        dry_run=dry_run, label=strat)
                    if tp_go and not in_abort_cooldown:
                        op = '>=' if strat == 'BCS' else '<='
                        if tp_hit:
                            log(f"\n  {strat} #{tid} {stock} *** TP HIT: {spot:.2f} {op} {target} ***")
                        else:
                            log(f"\n  {strat} #{tid} {stock} *** TP LATCHED: touched "
                                f"{target} earlier; spot now {spot:.2f} ***")
                        closing_in_progress[close_key] = "TP"
                        success = close_spread(kite, trade, spot, "TP", dry_run,
                                               store=trade_store, strategy_label=strat,
                                               quote=spread_data)
                        if success == 'ABORT':
                            closing_in_progress.pop(close_key, None)
                            abort_until[close_key] = time.time() + ABORT_COOLDOWN_SEC
                            log(f"  {strat} #{tid} {stock}: TP close aborted — cooldown {ABORT_COOLDOWN_SEC}s.")
                        else:
                            closed = True
                            if success:
                                stamp_tp_touch_to_fill(
                                    trade, trade_store, spot,
                                    rising=(strat == 'BCS'), dry_run=dry_run,
                                    label=strat)
                            if not success:
                                log(f"  {strat} #{tid} {stock}: Close failed — manual intervention needed.")
                                send_telegram(f"{strat} #{tid} {stock}: TP close FAILED. Manual intervention needed!", alert_class=alert_policy.SAFETY)

                else:
                    # ── FH SL_SPOT: spot >= sl_spot (upside/bullish risk) ──
                    if spot >= sl_spot_val:
                        log(f"\n  FH #{tid} {stock} *** SL_SPOT HIT: {spot:.2f} >= {sl_spot_val} ***")
                        # NO `closing_in_progress` MARKER FOR FH, and this is
                        # load-bearing. The marker stops a second close being
                        # started while orders are in flight; FH places none,
                        # so it protects nothing — and it would never be
                        # released, because neither FH outcome moves the record
                        # out of `get_open_trades()` any more (the close lock
                        # is gone with the orders). A held marker means the
                        # position is never polled again: the M16 shape, made
                        # permanent. A hand-traded position must stay WATCHED.
                        success = close_fh_position(kite, trade, spot, "SL_SPOT", dry_run)
                        if success is True:
                            closed = True
                        else:
                            # 'MANUAL' — legs live, the owner must trade it —
                            # or False — legs flat but the exit cannot be
                            # priced. Neither is fixed by retrying and both
                            # alert with their OWN once-per-day dedup
                            # (`_fh_manual_alerted`, `_unpriced_close_alerted`).
                            # A Telegram here would fire on every 5s poll.
                            log(f"  FH #{tid} {stock}: SL_SPOT needs the owner "
                                f"({'no order placed — FH is manual' if success == 'MANUAL' else 'flat but unpriceable'}). "
                                f"Still monitoring.")

                if closed:
                    trail_state.pop(close_key, None)
                    confirm_state.pop(close_key, None)
                    blind_state.pop(close_key, None)
                    corrob_state.pop(close_key, None)
                    spot_blind_state.pop(close_key, None)
                    abort_until.pop(close_key, None)

            # ── M14 · frozen-close recovery ──────────────────────────
            #
            # AFTER the per-trade loop, deliberately. The loop is what watches
            # live positions and its stops must not be delayed by a sweep over
            # records that are, by definition, already stuck. Frozen records
            # are not in `all_trades` at all — dropping out of the open book is
            # the whole defect — so this reads them from the stores directly.
            #
            # FH is ALERT-ONLY (owner, 2026-08-28): it is traded by hand and
            # this monitor places no FH order anywhere, so its frozen records
            # are classified and nagged, never ordered on. The flag rides on
            # the book rather than being inferred from the record, so the rule
            # sits where it was decided.
            try:
                recover_frozen_positions(kite, frozen_books, dry_run)
            except Exception as e:
                # Never let the sweep take down the poll loop: the loop is what
                # protects the LIVE book, and a frozen record is already stuck.
                log(f"  Recovery sweep raised, continuing: {e}")

            # ── S3 · post-close residue ──────────────────────────────
            #
            # A record BOOKED CLOSED whose reconcile found a live leg is not
            # frozen, so the sweep above cannot see it — it is out of the open
            # book, out of `get_frozen_trades()`, out of everything. One
            # Telegram at close time was its entire lifecycle. This chases it
            # until the broker says flat, and places no order ever: the record
            # is terminal, and the leg may be one the owner is holding on
            # purpose.
            #
            # It costs a `positions()` call ONLY while an incident is open,
            # which is the normal state of never.
            try:
                sweep_reconcile_residue(kite, frozen_books)
                # The entry-side twin. Separate call, one shared machine —
                # see `ResidueKind`. A leg an entry left behind is not a leg
                # a close left behind, and telling the owner the wrong story
                # about which sends them to the wrong screen.
                sweep_entry_residue(kite, frozen_books)
            except Exception as e:
                log(f"  Residue sweep raised, continuing: {e}")

            # ── Watchlist price alerts (after trade checks) ──────────
            if wl_active:
                check_watchlist_alerts(kite, log_fn=log, telegram_fn=send_telegram,
                                       store=wl_store)

            if print_status:
                last_status_time = now

            # The iteration actually completed — only now is the error budget
            # genuinely clean. See the note where this used to live.
            consecutive_errors = 0

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            log("\nCron monitor stopped by user (Ctrl+C)")
            remaining = _load_all_trades(bcs_store, fh_store, bps_store, zebra_store)
            log(f"Remaining open trades: {len(remaining)}")
            return
        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR in cron loop ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            # ONE definition of "the token died", shared with the per-trade
            # handler — see _is_auth_error.
            if _is_auth_error(e):
                log("FATAL: Kite token appears expired. Cannot continue.")
                remaining = _load_all_trades(bcs_store, fh_store, bps_store, zebra_store)
                stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in remaining)
                send_telegram(f"SPREAD MONITOR FATAL: Kite token expired! Unmonitored: {stocks}", alert_class=alert_policy.SAFETY)
                return
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting.")
                remaining = _load_all_trades(bcs_store, fh_store, bps_store, zebra_store)
                stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in remaining)
                send_telegram(f"SPREAD MONITOR FATAL: {MAX_CONSECUTIVE_ERRORS} errors. Unmonitored: {stocks}", alert_class=alert_policy.SAFETY)
                return
            if consecutive_errors == 10:
                send_telegram(f"SPREAD MONITOR WARNING: 10 consecutive errors. Last: {e}", alert_class=alert_policy.SAFETY)
            time.sleep(10)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Unified Spread Monitor — BCS + Bear Put Spread + Fallen Hero with strategy-aware SL'
    )
    parser.add_argument('stock', nargs='?', default=None,
                        help='Stock name (e.g., ICICIBANK). Required unless --list.')
    parser.add_argument('--list', action='store_true',
                        help='List all trades')
    parser.add_argument('--cron', action='store_true',
                        help='Monitor ALL open trades (cron/scheduler mode)')
    parser.add_argument('--trade-id', type=int, default=None,
                        help='Trade ID to monitor (if multiple open trades for same stock)')
    parser.add_argument('--target', type=float, default=None,
                        help='Override target spot price (default: from trade store)')
    parser.add_argument('--sl-spot', type=float, default=None,
                        help='Override spot-based stop loss (default: from trade store)')
    parser.add_argument('--sl-spread', type=float, default=None,
                        help='Override spread-based stop loss (default: from trade store)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Test mode - no real orders placed')
    parser.add_argument('--watchlist', action='store_true',
                        help='List all watchlist items')
    parser.add_argument('--cancel-alert', type=int, default=None,
                        metavar='ID', help='Cancel a watchlist alert by ID')
    # ── M14 clearance verbs ──────────────────────────────────────────────
    parser.add_argument('--frozen', action='store_true',
                        help='List trades stuck at partial_close')
    parser.add_argument('--book-frozen', default=None, metavar='BOOK:ID',
                        help='Book a frozen trade whose legs are all flat')
    parser.add_argument('--clear-residue', default=None, metavar='BOOK:ID',
                        help='S3: stop nagging about a post-close residue you '
                             'are holding on purpose. Places no orders; '
                             'records that it was cleared while live.')
    parser.add_argument('--reopen-frozen', default=None, metavar='BOOK:ID',
                        help='Return an INTACT frozen spread to monitoring')
    parser.add_argument('--short-price', type=float, default=None,
                        help='Fill price for the short leg (--book-frozen)')
    parser.add_argument('--long-price', type=float, default=None,
                        help='Fill price for the long leg (--book-frozen)')
    args = parser.parse_args()

    # Set up logging for bcs package
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(name)s %(levelname)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # Initialize trade stores (downloads from Drive on startup)
    bcs_store = get_store()

    # ── --watchlist mode ─────────────────────────────────────────────────
    if args.watchlist:
        wl_store = get_watchlist_store()
        wl_store.list_items()
        return

    # ── --cancel-alert mode ──────────────────────────────────────────────
    if args.cancel_alert is not None:
        wl_store = get_watchlist_store()
        wl_store.update_status(args.cancel_alert, 'cancelled')
        print(f"Watchlist item #{args.cancel_alert} cancelled.")
        return

    # ── M14 clearance verbs ──────────────────────────────────────────────
    #
    # Before `--list`, because they are the ones that WRITE. Both print the
    # live broker book first and refuse if reality contradicts the verb.
    if args.frozen:
        list_frozen_trades()
        return
    if args.clear_residue:
        kite = load_kite()
        sys.exit(clear_residue(kite, args.clear_residue))

    if args.book_frozen or args.reopen_frozen:
        kite = load_kite()
        if args.book_frozen:
            sys.exit(book_frozen(kite, args.book_frozen,
                                 args.short_price, args.long_price))
        sys.exit(reopen_frozen(kite, args.reopen_frozen))

    # ── --list mode ──────────────────────────────────────────────────────
    if args.list:
        print("\n=== BCS Trades ===")
        bcs_store.list_trades()
        bps_store_inst = get_bps_store()
        print("\n=== Bear Put Spread Trades ===")
        bps_store_inst.list_trades()
        fh_store_inst = get_fh_store()
        print("\n=== Fallen Hero Trades ===")
        fh_store_inst.list_trades()
        # The COHORT. It was missing here for exactly the reason it was
        # missing from `monitor_all` until the bridge landed: this command
        # hardcodes its stores. Fixing the monitor loop and leaving the
        # LISTING blind meant the owner's "what do I have open" answered
        # `Open: 0` with eight positions live -- the same misreport the bridge
        # exists to end, one command over.
        list_cohort_trades()
        return

    # ── --cron mode ──────────────────────────────────────────────────────
    if args.cron:
        # Log file is set inside monitor_all()
        kite = load_kite()
        log("Kite authenticated.")
        monitor_all(kite, args.dry_run)
        return

    # ── Stock is required for monitoring ─────────────────────────────────
    if not args.stock:
        parser.error("stock name is required (or use --list)")

    stock = args.stock.upper()

    # ── Look up trade (search BCS first, then BPS, then FH) ─────────────
    trade = bcs_store.find_open_trade(stock, args.trade_id)
    trade_strategy = 'BCS'
    if not trade:
        bps_store_inst = get_bps_store()
        trade = bps_store_inst.find_open_trade(stock, args.trade_id)
        trade_strategy = 'BPS'
    if not trade:
        fh_store_inst = get_fh_store()
        trade = fh_store_inst.find_open_trade(stock, args.trade_id)
        trade_strategy = 'FH'
    if not trade:
        if args.trade_id:
            print(f"ERROR: No open trade found for {stock} with ID {args.trade_id}")
        else:
            print(f"ERROR: No open trade found for {stock}")
        print("Use --list to see all trades.")
        sys.exit(1)

    # ── Connect and run ──────────────────────────────────────────────────
    set_log_file(LOG_DIR / f"spread_monitor_{stock}_{today_ist().strftime('%Y%m%d')}.log")
    log(f"Loading Kite token from {TOKEN_FILE}...")
    kite = load_kite()
    log("Kite authenticated.")

    if trade_strategy == 'BCS':
        # ── BCS: Resolve params and run BCS monitor ──────────────────────
        target = args.target if args.target is not None else trade['target_spot']
        sl_spot = args.sl_spot if args.sl_spot is not None else trade['sl_spot']
        sl_spread = args.sl_spread if args.sl_spread is not None else trade['sl_spread']
        log(f"Loaded BCS trade #{trade['id']}: {stock} {trade['long_symbol']}/{trade['short_symbol']}")

        monitor(kite, trade, target, sl_spot, sl_spread, args.dry_run,
                cli_target=args.target, cli_sl_spot=args.sl_spot,
                cli_sl_spread=args.sl_spread)
    elif trade_strategy == 'BPS':
        # ── BPS: Delegate to cron monitor (reversed SL/TP direction) ─────
        log(f"Loaded BPS trade #{trade['id']}: {stock} {trade['long_symbol']}/{trade['short_symbol']}")
        log(f"  BPS single-trade mode delegates to cron monitor (reversed SL/TP).")
        monitor_all(kite, args.dry_run)
    else:
        # ── FH: Use cron-style monitoring for single FH trade ────────────
        log(f"Loaded FH trade #{trade['id']}: {stock} SC:{trade['short_call_symbol']} "
            f"SP:{trade['short_put_symbol']} LP:{trade['long_put_symbol']}")
        log(f"  FH single-trade mode delegates to cron monitor.")
        monitor_all(kite, args.dry_run)


if __name__ == '__main__':
    main()

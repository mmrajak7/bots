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
import re
import sys
import time
import argparse
from datetime import datetime, date, time as dtime, timedelta
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
    ts = datetime.now().strftime('%H:%M:%S')
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
    if generated.date() != date.today():
        # A log line is not an alert. This runs unattended under cron; a stale
        # token here becomes "every stop is dark today" and the only witness
        # was a line in a file nobody opens until something has gone wrong.
        msg = (f"Token is from {generated.date()}, not today — the monitor is "
               f"starting but every Kite call may fail. Open positions would "
               f"be UNMONITORED. Re-run SNAIL auth.")
        log(f"WARNING: {msg}")
        send_telegram(f"⚠️ BCS MONITOR: STALE KITE TOKEN\n{msg}")

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# ── Market & Price ───────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Check if within NSE trading hours."""
    now = datetime.now().time()
    return MARKET_OPEN <= now < MARKET_CLOSE


def get_spot(kite: KiteConnect, spot_symbol: str) -> float:
    """Fetch spot price for given symbol."""
    q = kite.ltp([spot_symbol])
    return q[spot_symbol]['last_price']


def get_option_depth(kite: KiteConnect, exchange: str, symbol: str) -> dict:
    """Fetch full depth for an option symbol."""
    full = f"{exchange}:{symbol}"
    q = kite.quote([full])[full]
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
            traded_today = (ltt_dt.date() == date.today())
            if traded_today:
                ltp_fresh = (datetime.now() - ltt_dt).total_seconds() <= LTP_FRESH_SEC
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


def spread_intrinsic_floor(trade: dict, spot: float):
    """No-arbitrage floor for a vertical debit spread at `spot`, or None.

    Ported from the zebra monitor's ABB #242 guard: in July 2026 a junk quote
    on an illiquid ITM leg booked a -50% stop at a value of 335 when pure
    intrinsic at the recorded spot was 1,020. A structure cannot be worth less
    than it could be unwound for; a quote below this is proof of a broken book,
    not an unlucky price.

    Deliberately generous — it subtracts 1.5x the short leg's entry-time
    extrinsic, so it only ever fires on the impossible, never the merely
    unfavourable. A too-tight floor would block a real stop, which is the
    error that costs money.

    **CALLS AND PUTS, since 2026-08-25.** It used to compute
    `max(spot - k_l, 0) - max(spot - k_s, 0)` unconditionally, which is call
    arithmetic. A bear put spread holds the HIGHER strike long, so that
    expression returns 0 or a negative number for every spot — and since a
    negative value is already refused upstream as `negative_spread`, the
    comparison `value < floor` could never be true. The guard was not merely
    approximate for BPS; it was INERT, and inert precisely where it matters:
    at spot 1250 a 1400/1340 put spread is worth its full 60, the old floor
    said -9.67, and a garbage quote of 1.00 passed straight through. That is
    the ABB #242 scenario itself, unguarded, on one of the three live books.
    `bcs/spread_monitor.py` monitors all three; the guard was written for the
    first and silently skipped the second ([[feedback_copy_pasted_modules_fix_once]]).

    The allowance was wrong for puts in the same way: a short put's intrinsic
    at entry is `max(k_s - entry_spot, 0)`, not `max(entry_spot - k_s, 0)`.

    Direction comes from the SYMBOL, not from `_store_type` — see
    `option_type_from_symbol`. If the two legs disagree, or either is
    unreadable, the floor does not apply.
    """
    try:
        k_l = strike_from_symbol(trade.get('long_symbol'))
        k_s = strike_from_symbol(trade.get('short_symbol'))
        if k_l is None or k_s is None:
            return None
        opt_l = option_type_from_symbol(trade.get('long_symbol'))
        opt_s = option_type_from_symbol(trade.get('short_symbol'))
        if opt_l is None or opt_l != opt_s:
            # A vertical has both legs in the same instrument. Anything else
            # is not a shape this floor knows how to price.
            return None

        if opt_l == 'CE':
            intrinsic = max(spot - k_l, 0.0) - max(spot - k_s, 0.0)
            short_intrinsic_at_entry = lambda es: max(es - k_s, 0.0)
        else:
            intrinsic = max(k_l - spot, 0.0) - max(k_s - spot, 0.0)
            short_intrinsic_at_entry = lambda es: max(k_s - es, 0.0)

        # Short-leg extrinsic AT ENTRY. The short leg is sold OTM/NTM, so its
        # entire premium is extrinsic unless spot was already past the strike.
        short_px = trade.get('entry_short_price')
        if short_px is None:
            # No basis for an allowance at all. Disable rather than invent
            # one: a floor built on a guessed allowance is not a no-arbitrage
            # bound, and getting it too TIGHT blinds the monitor completely.
            return None
        entry_spot = trade.get('entry_spot')
        if entry_spot is not None:
            allowance = float(short_px) - short_intrinsic_at_entry(
                float(entry_spot))
        else:
            # B17: the old fallback was `0.3 * net_debit`, which on the real
            # ICICI record is 4.07 against a true 7.65 — a TIGHTER floor than
            # the truth, so the healthy 09:16 book (38.95) fell below it and
            # the monitor refused every valuation for the rest of the session.
            # SL_SPREAD, SL_TRAIL and the trail all went dark, which is the
            # exact opposite of what this guard is for.
            #
            # The whole premium is a strict UPPER bound on the extrinsic, so
            # using it makes the floor generous when we are ignorant. Being
            # ignorant should widen the benefit of the doubt, never narrow it.
            allowance = float(short_px)
        allowance = max(allowance, 0.0)
        return round(intrinsic - 1.5 * allowance, 2)
    except Exception:
        return None


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


def get_spread_value(kite: KiteConnect, trade: dict, spot: float = None) -> dict:
    """Fetch both legs and compute spread value (long bid - short ask).

    Returns dict with long depth, short depth, computed spread, and
    'unreliable' (reason string, or None if both books are trustworthy).
    Spread is None whenever EITHER leg's book fails leg_quote_reliable() —
    a garbage book must never produce a tradeable valuation (2026-07-24).

    When `spot` is supplied, a value below the no-arbitrage floor is also
    rejected. Note this catches a DIFFERENT failure from the width/LTP tests:
    those judge the book's SHAPE, this judges whether the number is possible at
    all. A well-formed book can still quote an impossible price.
    """
    long_d = get_option_depth(kite, trade['exchange'], trade['long_symbol'])
    short_d = get_option_depth(kite, trade['exchange'], trade['short_symbol'])

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
        # Negative spread = bid-ask inversion or market dislocation.
        # Not a real loss signal, treat as unreliable data.
        if spread_val < 0:
            spread_val = None
            unreliable = f"negative_spread {long_d['bid']} - {short_d['ask']}"
        elif spot is not None and spot > 0:
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

def get_strategy(trade: dict) -> str:
    """Detect strategy from trade fields.

    FH has long_put_symbol + short_call_symbol.
    BPS has _store_type='bps' (same field names as BCS).
    BCS is the default.
    """
    if trade.get('_store_type') == 'bps':
        return 'BPS'
    return 'FH' if 'long_put_symbol' in trade else 'BCS'


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
    settle_time = datetime.combine(date.today(), MARKET_OPEN) + timedelta(seconds=MARKET_OPEN_BUFFER_SEC)
    return datetime.now() >= settle_time


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
    settle_time = datetime.combine(date.today(), MARKET_OPEN) + timedelta(seconds=SPREAD_TRIGGER_OPEN_BUFFER_SEC)
    if datetime.now() < settle_time:
        return False
    now = time.time() if now is None else now
    return _resume_settle_at is None or now >= _resume_settle_at


def is_expiry_day(trade: dict) -> bool:
    """Check if today is the trade's expiry date."""
    try:
        expiry_str = trade.get('expiry', '')
        expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
        return date.today() == expiry_date
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


def sessions_to_expiry(trade: dict, today: Optional[date] = None) -> Optional[int]:
    """Trading sessions remaining, expiry inclusive. 0 = expiry day.

    Counts WEEKDAYS, not calendar days: over a weekend a calendar count reads
    "3 days left" when only one session remains, which is precisely when this
    warning matters most.

    No holiday calendar exists in this repo, so a holiday inside the window
    makes this an OVER-estimate — it will say more sessions remain than really
    do. That is why the warning threshold sits one session before the ramp
    starts: it absorbs a single holiday. It is not a substitute for a real
    calendar, and a long holiday stretch will still surprise it.
    """
    try:
        expiry = datetime.strptime(trade.get('expiry', ''), '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None
    d = today or date.today()
    if expiry < d:
        return 0
    sessions = 0
    cur = d
    while cur < expiry:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            sessions += 1
    return sessions


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
    if sessions is None or sessions > EXPIRY_WARN_SESSIONS or sessions <= 0:
        return False        # expiry day has its own force-close path
    stamp = (today or date.today()).isoformat()
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


def send_telegram(msg: str):
    """Send Telegram alert. Best-effort: never blocks or crashes trading."""
    global _telegram_cfg, _telegram_cfg_loaded

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


def _find_last_fill_price(kite: KiteConnect, symbol: str, txn_type: str) -> float:
    """Find the fill price of the most recent COMPLETE order for a symbol+side.

    Used to recover the actual fill when close_leg detects the position changed
    before it could observe the fill directly. Returns 0 if not found.

    Prefers orders tagged with ORDER_TAG (placed by this script). Falls back
    to any matching order if no tagged order found.
    """
    try:
        tagged_best = None
        any_best = None
        for o in kite.orders():
            if (o.get('tradingsymbol') == symbol
                    and o.get('transaction_type') == txn_type
                    and o.get('status') == 'COMPLETE'
                    and o.get('average_price', 0) > 0):
                ts = str(o.get('order_timestamp', ''))
                if o.get('tag') == ORDER_TAG:
                    if tagged_best is None or ts > str(tagged_best.get('order_timestamp', '')):
                        tagged_best = o
                if any_best is None or ts > str(any_best.get('order_timestamp', '')):
                    any_best = o

        best = tagged_best or any_best
        if best:
            tag_note = "" if best.get('tag') == ORDER_TAG else " (WARNING: not tagged by BCS_MON)"
            log(f"    Recovered fill: {symbol} {txn_type} @ {best['average_price']} (order {best['order_id']}){tag_note}")
            return best['average_price']
    except Exception as e:
        log(f"    Could not recover fill price: {e}")
    return 0.0


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


def place_limit_order(kite: KiteConnect, exchange: str, symbol: str,
                      txn_type: str, qty: int, price: float,
                      dry_run: bool) -> str:
    """Place a LIMIT NRML order. Returns order_id or 'DRY_RUN_xxx'."""
    price = round_to_tick(price)
    if dry_run:
        fake_id = f"DRY_{datetime.now().strftime('%H%M%S%f')}"
        log(f"    [DRY RUN] {txn_type} {symbol} x {qty} @ {price} -> {fake_id}")
        return fake_id

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
    log(f"    Order placed: {txn_type} {symbol} x {qty} @ {price} -> {order_id}")
    return str(order_id)


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
              urgent: bool = False) -> Optional[dict]:
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

    for attempt in range(1, MAX_RETRIES + 1):
        # ── Order-time cutoff, re-checked EVERY attempt ───────────────
        cutoff = HARD_ORDER_CUTOFF_TIME if urgent else LAST_ORDER_TIME
        if datetime.now().time() > cutoff:
            log(f"    ORDER CUTOFF: {datetime.now().strftime('%H:%M:%S')} > {cutoff.strftime('%H:%M')} "
                f"({'urgent' if urgent else 'normal'}). No more orders for {symbol}.")
            send_telegram(f"Order cutoff reached closing {symbol} — "
                          f"{remaining_qty} qty NOT closed. Manual intervention needed!")
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
                fill_price = _find_last_fill_price(kite, symbol, txn_type)
                total_filled = cumulative_fill_qty if cumulative_fill_qty > 0 else qty
                if cumulative_fill_qty > 0:
                    fill_price = cumulative_fill_value / cumulative_fill_qty
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
            depth = get_option_depth(kite, exchange, symbol)
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
        final_urgent_attempt = urgent and attempt == MAX_RETRIES

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
            log(f"  Attempt {attempt}/{MAX_RETRIES}: BUY {symbol} x {remaining_qty}")
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
            log(f"  Attempt {attempt}/{MAX_RETRIES}: SELL {symbol} x {remaining_qty}")
            log(f"    Depth -> Bid: {depth['bid']} x {depth['bid_qty']} | Ask: {depth['ask']} x {depth['ask_qty']} "
                f"| LTP: {depth['ltp']} | PrevCl: {depth['prev_close']} | Reliable: {book_reliable}")
            log(f"    Limit price: {round_to_tick(price)}")

        order_id = place_limit_order(kite, exchange, symbol, txn_type, remaining_qty, price, dry_run)
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
            send_telegram(f"Order REJECTED: {txn_type} {symbol} x {remaining_qty} — {msg}")
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

        if attempt < MAX_RETRIES:
            log(f"    Retrying with +{SLIPPAGE_TICKS_INCREMENT} ticks slippage... (remaining: {remaining_qty})")
            time.sleep(1)

    # All retries exhausted
    if cumulative_fill_qty > 0:
        avg = cumulative_fill_value / cumulative_fill_qty
        log(f"    PARTIAL CLOSE: {cumulative_fill_qty}/{qty} filled @ avg {avg:.2f}. {remaining_qty} remaining!")
        send_telegram(f"PARTIAL CLOSE: {symbol} {cumulative_fill_qty}/{qty} filled. {remaining_qty} remaining!")
        return {'status': 'PARTIAL', 'average_price': avg,
                'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

    log(f"    FAILED to close {symbol} after {MAX_RETRIES} attempts!")
    return None


URGENT_CLOSE_REASONS = ('SL_SPOT', 'EXPIRY_FORCE_CLOSE')


def close_spread(kite: KiteConnect, trade: dict, spot: float,
                 reason: str, dry_run: bool, store=None,
                 strategy_label: str = 'BCS',
                 reverify_sl: Optional[float] = None):
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

    # ── Late-day guard (urgent closes are reduce-only and get 5 more min) ──
    now_t = datetime.now().time()
    cutoff = HARD_ORDER_CUTOFF_TIME if urgent else LAST_ORDER_TIME
    if now_t > cutoff:
        log(f"  LATE-DAY GUARD: {now_t.strftime('%H:%M')} > {cutoff.strftime('%H:%M')}.")
        log(f"  Too close to market close. Not placing orders — manual intervention needed.")
        send_telegram(
            f"{label} {reason} TRIGGERED {stock} @ {spot}\n"
            f"BUT past {cutoff.strftime('%H:%M')} — NOT auto-closing.\n"
            f"Close manually in Kite!"
        )
        return False

    # ── Re-verify valuation triggers on a FRESH quote before any order ────
    if reverify_sl is not None:
        time.sleep(REVERIFY_DELAY_SEC)
        try:
            # Spot passed so the re-verify applies the arbitrage floor too —
            # otherwise the freshest, most decision-relevant quote in the whole
            # flow would be the one quote nobody floor-checks.
            fresh = get_spread_value(kite, trade, spot=spot)
        except Exception as e:
            log(f"  RE-VERIFY: quote fetch failed ({e}). Aborting close — trigger will re-arm.")
            return 'ABORT'
        if fresh['spread'] is None:
            log(f"  RE-VERIFY ABORT: fresh quote unreliable ({fresh['unreliable']}). "
                f"Not closing on a garbage book — trigger re-arms.")
            send_telegram(f"{label} {stock}: {reason} suppressed — fresh quote unreliable "
                          f"({fresh['unreliable']}). Trade still open, monitoring continues.")
            return 'ABORT'
        if fresh['spread'] > reverify_sl:
            log(f"  RE-VERIFY ABORT: fresh spread {fresh['spread']:.2f} > {reverify_sl:.2f} — "
                f"trigger was a quote artifact. "
                f"(L bid {fresh['long']['bid']}/ask {fresh['long']['ask']} | "
                f"S bid {fresh['short']['bid']}/ask {fresh['short']['ask']})")
            send_telegram(f"{label} {stock}: {reason} near-miss — fresh spread "
                          f"{fresh['spread']:.2f} > SL {reverify_sl:.2f}. NOT closed (quote artifact).")
            return 'ABORT'
        log(f"  RE-VERIFY CONFIRMED: fresh spread {fresh['spread']:.2f} <= {reverify_sl:.2f}. Proceeding.")

    # ── Acquire close-lock (prevents concurrent close from another machine) ──
    close_lock_acquired = False
    if not dry_run:
        if not store.begin_close(trade['id'], reason):
            log(f"  Trade #{trade['id']} is already closing/closed. Skipping.")
            return True  # Not an error — another process has it
        close_lock_acquired = True

    try:
        return _close_spread_inner(kite, store, trade, spot, reason, dry_run, label,
                                   urgent=urgent)
    except Exception as e:
        log(f"  EXCEPTION during close_spread: {e}")
        send_telegram(f"{label} {stock}: EXCEPTION during {reason} close! {e}\nManual intervention needed!")
        if close_lock_acquired:
            try:
                store.set_trade_status(trade['id'], 'partial_close',
                                       close_error=str(e), close_reason=reason)
            except Exception as inner_e:
                log(f"  Could not set partial_close status: {inner_e}")
                store._sync_locked = False  # Last resort: clear lock directly
        return False


def _close_spread_inner(kite, store, trade, spot, reason, dry_run, label='BCS',
                        urgent=False):
    """Inner close logic, separated so close_spread() can wrap with try/except."""
    stock = trade['stock']
    short_sym = trade['short_symbol']
    long_sym = trade['long_symbol']
    qty = trade['quantity']
    exchange = trade['exchange']

    send_telegram(f"{label} {reason} TRIGGERED\n{stock} spot={spot}\nClosing spread...")

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
                short_qty_seen=short_qty, long_qty_seen=long_qty)
            reconcile_after_close(kite, trade, label)
        send_telegram(
            f"🔴 {label} {stock}: FLIPPED POSITION\n"
            f"{reason} triggered, but the book is not what the trade says:\n"
            f"{detail}\n"
            f"No orders placed. The trade is frozen at "
            f"partial_close and will NOT be monitored further.\n"
            f"This is the Feb-2026 shape — audit the order history for this "
            f"leg before doing anything else.")
        return False

    # ── Guard: Both legs already flat — mark closed with recovered fills ──
    # Strictly zero on both sides. Anything else is handled above.
    if short_qty == 0 and long_qty == 0:
        log(f"\n  Both legs already flat (short={short_qty}, long={long_qty}).")
        log(f"  Trade was closed by another process or manually. Recovering fill prices...")
        short_fill = _find_last_fill_price(kite, short_sym, "BUY")
        long_fill = _find_last_fill_price(kite, long_sym, "SELL")
        entry_net = trade['net_debit']
        if short_fill > 0 and long_fill > 0:
            exit_net = long_fill - short_fill
        else:
            exit_net = 0.0
        exit_data = {
            'exit_date': datetime.now().isoformat(),
            'exit_reason': f"ALREADY_FLAT_{reason}",
            'exit_spot': spot,
            'short_fill': short_fill,
            'long_fill': long_fill,
            'exit_spread': exit_net,
            'pnl_per_share': exit_net - entry_net if exit_net else 0,
            'total_pnl': (exit_net - entry_net) * qty if exit_net else 0,
            'notes': 'Both legs flat when close triggered. Fills recovered from order history.',
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
            urgent=urgent
        )

        if not short_result or short_result['status'] not in ('COMPLETE', 'PARTIAL'):
            # close_leg returns None only when ZERO qty filled — for a NORMAL
            # (valuation-triggered) close the position is untouched, so abort
            # back to 'open' and let the trigger re-arm instead of freezing
            # the trade in partial_close. (2026-07-24 guard design.)
            if not urgent and short_result is None:
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
                store.set_trade_status(trade['id'], 'partial_close',
                                       close_failed_leg='short', close_reason=reason)
            return False

        short_fill = short_result.get('average_price', 0)
        short_closed_now = True
        if short_result['status'] == 'PARTIAL':
            remaining = close_qty - short_result.get('filled_quantity', 0)
            log(f"  WARNING: Short leg partially filled. {remaining} qty still short!")
            send_telegram(f"{label} {stock}: Short leg partial fill. {remaining} still short!")

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
            if remaining > 0:
                log(f"  RETRY: closing the {remaining} qty residual (urgent)")
                retry = close_leg(kite, exchange, short_sym, "BUY", remaining,
                                  is_buy=True, dry_run=dry_run, urgent=True)
                got = retry.get('filled_quantity', 0) if retry else 0
                remaining -= got
                log(f"  RETRY filled {got}; {remaining} still short")

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
                        short_fill=short_fill)
                    reconcile_after_close(kite, trade, label)
                send_telegram(
                    f"🔴 {label} {stock}: PARTIAL SHORT CLOSE\n"
                    f"{remaining} qty still SHORT on {short_sym} after a retry.\n"
                    f"The long leg was NOT sold — selling it would leave you "
                    f"naked short. You are over-hedged, which is the bounded "
                    f"side.\n"
                    f"Trade frozen at partial_close and NOT monitored further. "
                    f"Close the residue by hand.")
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
            urgent=long_urgent
        )

        if not long_result or long_result['status'] not in ('COMPLETE', 'PARTIAL'):
            # Same pre-fill abort as the short leg: if this close hasn't
            # touched the position at all (short was already flat, long fill
            # zero) a NORMAL close aborts back to open instead of freezing.
            if not long_urgent and long_result is None and not short_closed_now:
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
                store.set_trade_status(trade['id'], 'partial_close',
                                       close_failed_leg='long', close_reason=reason,
                                       short_fill=short_fill)
            return False

        long_fill = long_result.get('average_price', 0)
        long_closed_now = True
    else:
        log(f"\n  LONG leg {long_sym} already flat/short (qty={long_qty}). Skipping SELL.")

    # ── Summary ──────────────────────────────────────────────────────────
    entry_net = trade['net_debit']
    if short_closed_now and long_closed_now:
        exit_net = long_fill - short_fill
    elif long_closed_now and not short_closed_now:
        exit_net = long_fill
        log(f"  NOTE: Short was already flat. P&L is approximate (long fill only).")
    elif short_closed_now and not long_closed_now:
        exit_net = -short_fill
        log(f"  NOTE: Long was already flat. P&L is approximate (short fill only).")
    else:
        exit_net = 0.0
    pnl_per_share = exit_net - entry_net
    total_pnl = pnl_per_share * qty

    log("")
    log("=" * 70)
    log("  SPREAD CLOSED SUCCESSFULLY")
    log("=" * 70)
    log(f"  Reason:                 {reason}")
    log(f"  Short (BUY back) fill:  {short_fill}")
    log(f"  Long  (SELL)     fill:  {long_fill}")
    log(f"  Exit spread value:      {exit_net:.2f}/share")
    log(f"  Entry spread cost:      {entry_net:.2f}/share")
    log(f"  P&L per share:          {pnl_per_share:+.2f}")
    log(f"  Total P&L:              Rs {total_pnl:+,.0f}")
    log(f"  Spot at close:          {spot}")
    log("=" * 70)

    send_telegram(
        f"{label} CLOSED: {stock}\n"
        f"Reason: {reason}\n"
        f"Exit spread: {exit_net:.2f} | Entry: {entry_net:.2f}\n"
        f"P&L: Rs {total_pnl:+,.0f}\n"
        f"Spot: {spot}"
    )

    # ── Update trade store ───────────────────────────────────────────────
    exit_data = {
        'exit_date': datetime.now().isoformat(),
        'exit_reason': reason,
        'exit_spot': spot,
        'short_fill': short_fill,
        'long_fill': long_fill,
        'exit_spread': exit_net,
        'pnl_per_share': pnl_per_share,
        'total_pnl': total_pnl,
    }

    if not dry_run:
        store.update_trade_exit(trade['id'], exit_data)
        log(f"  Trade #{trade['id']} marked closed (local + Drive)")
        reconcile_after_close(kite, trade, label)
    else:
        log(f"  [DRY RUN] Would mark trade #{trade['id']} closed")

    return True


def reconcile_after_close(kite: KiteConnect, trade: dict, label: str = 'BCS'
                          ) -> bool:
    """After a close reports success, PROVE both legs are actually flat.

    This is the ICICI-class guard, and it is the one thing the vetting layer
    cannot supply: in Feb 2026 a monitor bug placed 4x BUY on the short leg and
    flipped the position long, turning a +190% spread into +Rs 2K. The bug was
    in the code that placed the orders — so the code that placed them was never
    going to catch it. This check reads the BROKER's view instead, and it is
    deliberately independent of every fill/quantity variable the close path
    computed.

    Read-only and never raises: it cannot make an exit worse, only visible.
    Returns True when flat.
    """
    try:
        residues = []
        for leg in ('short_symbol', 'long_symbol'):
            sym = trade.get(leg)
            if not sym:
                continue
            qty = get_net_position(kite, sym)
            if qty != 0:
                residues.append(f'{sym} net {qty:+d}')
        if not residues:
            log("  RECONCILE: both legs flat at the broker ✓")
            return True
        detail = '; '.join(residues)
        log(f"  *** RECONCILE FAILED: {detail} ***")
        send_telegram(
            f"🚨 {label} {trade.get('stock')}: POSITION NOT FLAT AFTER CLOSE\n"
            f"The trade is marked closed but the broker still shows: {detail}\n"
            f"Check Kite NOW — this is the shape of the Feb 2026 bug that "
            f"flipped a short leg long."
        )
        return False
    except Exception as e:
        # Never let the audit break the close it is auditing.
        log(f"  RECONCILE: could not verify positions ({e})")
        return False


# ── FH Close ────────────────────────────────────────────────────────────────

def close_fh_position(kite: KiteConnect, trade: dict, spot: float,
                      reason: str, dry_run: bool) -> bool:
    """
    Close a Fallen Hero position. Naked risk (short call) first.
    Updates FH TradeStore on success (local + Drive).
    Returns True if fully closed, False if any leg failed.

    Close order (naked risk first):
      1. BUY back short call (naked — most dangerous)
      2. SELL long call (if exists — hedge, no longer needed)
      3. BUY back short put
      4. SELL long put
    """
    fh_store = get_fh_store()
    stock = trade['stock']

    log("")
    log("=" * 70)
    log(f"  FH {reason} TRIGGERED! {stock} spot = {spot}")
    log(f"  Initiating FH close sequence...")
    log("=" * 70)

    # ── Late-day guard (FH closes are always urgent: SL_SPOT/expiry only) ──
    now_t = datetime.now().time()
    if now_t > HARD_ORDER_CUTOFF_TIME:
        log(f"  LATE-DAY GUARD: {now_t.strftime('%H:%M')} > {HARD_ORDER_CUTOFF_TIME.strftime('%H:%M')}.")
        log(f"  Too close to market close. Not placing orders — manual intervention needed.")
        send_telegram(
            f"FH {reason} TRIGGERED {stock} @ {spot}\n"
            f"BUT past {HARD_ORDER_CUTOFF_TIME.strftime('%H:%M')} — NOT auto-closing.\n"
            f"Close manually in Kite!"
        )
        return False

    # ── Acquire close-lock ────────────────────────────────────────────────
    close_lock_acquired = False
    if not dry_run:
        if not fh_store.begin_close(trade['id'], reason):
            log(f"  Trade #{trade['id']} is already closing/closed. Skipping.")
            return True  # Not an error — another process has it
        close_lock_acquired = True

    try:
        return _close_fh_inner(kite, fh_store, trade, spot, reason, dry_run)
    except Exception as e:
        log(f"  EXCEPTION during close_fh_position: {e}")
        send_telegram(f"FH {stock}: EXCEPTION during {reason} close! {e}\nManual intervention needed!")
        if close_lock_acquired:
            try:
                fh_store.set_trade_status(trade['id'], 'partial_close',
                                          close_error=str(e), close_reason=reason)
            except Exception as inner_e:
                log(f"  Could not set partial_close status: {inner_e}")
                fh_store._sync_locked = False
        return False


def _close_fh_inner(kite, fh_store, trade, spot, reason, dry_run):
    """Inner FH close logic, separated so close_fh_position() can wrap with try/except."""
    stock = trade['stock']
    qty = trade['quantity']
    exchange = trade['exchange']

    sc_sym = trade['short_call_symbol']
    lc_sym = trade.get('long_call_symbol')  # Optional 4th leg
    sp_sym = trade['short_put_symbol']
    lp_sym = trade['long_put_symbol']

    send_telegram(f"FH {reason} TRIGGERED\n{stock} spot={spot}\nClosing position...")

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
                sp_qty_seen=sp_qty, lp_qty_seen=lp_qty)
            reconcile_after_close(kite, trade, 'FH')
        send_telegram(
            f"🔴 FH {stock}: FLIPPED POSITION\n"
            f"{reason} triggered, but the book is not what the trade says:\n"
            f"{detail}\n"
            f"No orders placed. The trade is frozen at partial_close and "
            f"will NOT be monitored further.\n"
            f"An FH short leg is NAKED — check Kite now.")
        return False

    # ── Guard: All legs already flat ─────────────────────────────────────
    # Strictly zero on every leg. Anything else is handled above.
    all_flat = (sc_qty == 0 and sp_qty == 0 and lp_qty == 0
                and (not lc_sym or lc_qty == 0))
    if all_flat:
        log(f"\n  All legs already flat. Recovering fill prices...")
        sc_fill = _find_last_fill_price(kite, sc_sym, "BUY")
        lc_fill = _find_last_fill_price(kite, lc_sym, "SELL") if lc_sym else 0.0
        sp_fill = _find_last_fill_price(kite, sp_sym, "BUY")
        lp_fill = _find_last_fill_price(kite, lp_sym, "SELL")
        close_cost = sc_fill + sp_fill - lc_fill - lp_fill
        total_credit = trade['total_credit']
        pnl_per_share = total_credit - close_cost
        total_pnl = pnl_per_share * qty
        exit_data = {
            'exit_date': datetime.now().isoformat(),
            'exit_reason': f"ALREADY_FLAT_{reason}",
            'exit_spot': spot,
            'short_call_fill': sc_fill, 'long_call_fill': lc_fill,
            'short_put_fill': sp_fill, 'long_put_fill': lp_fill,
            'close_cost': close_cost,
            'pnl_per_share': pnl_per_share, 'total_pnl': total_pnl,
            'notes': 'All legs flat when close triggered. Fills recovered from order history.',
        }
        if not dry_run:
            fh_store.update_trade_exit(trade['id'], exit_data)
        send_telegram(f"FH {stock}: {reason} but all legs already flat. Marked closed.")
        return True

    fills = {'short_call': 0.0, 'long_call': 0.0, 'short_put': 0.0, 'long_put': 0.0}

    # ── Step 1: BUY back short call (naked risk — MOST DANGEROUS) ────────
    if sc_qty < 0:
        close_qty = min(abs(sc_qty), qty)
        log(f"\nSTEP 1: BUY back SHORT CALL {sc_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, sc_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run,
                           urgent=True)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log("!!! CRITICAL: SHORT CALL CLOSE FAILED — naked risk remains !!!")
            send_telegram(f"FH {stock}: SHORT CALL CLOSE FAILED! Naked risk! Manual intervention needed!")
            if not dry_run:
                fh_store.set_trade_status(trade['id'], 'partial_close',
                                          close_failed_leg='short_call', close_reason=reason)
            return False
        fills['short_call'] = result.get('average_price', 0)

        # ── B10 (FH twin) · the long call is the short call's HEDGE ────────
        #
        # 'PARTIAL' passes the check above. Falling through to Step 2 sells the
        # long call in full while part of the short call is still open — i.e.
        # it REMOVES THE HEDGE FROM A NAKED SHORT. That is the worst version of
        # the B10 shape anywhere in this file.
        sc_remaining = close_qty - result.get('filled_quantity', 0)
        if result['status'] == 'PARTIAL' and sc_remaining > 0:
            log(f"  RETRY: closing the {sc_remaining} qty short-call residual")
            retry = close_leg(kite, exchange, sc_sym, "BUY", sc_remaining,
                              is_buy=True, dry_run=dry_run, urgent=True)
            got = retry.get('filled_quantity', 0) if retry else 0
            sc_remaining -= got
        if result['status'] == 'PARTIAL' and sc_remaining > 0:
            log("")
            log("  *** SHORT CALL RESIDUE — HEDGE WILL NOT BE SOLD ***")
            if not dry_run:
                fh_store.set_trade_status(
                    trade['id'], 'partial_close',
                    close_reason=f"PARTIAL_SHORT_CALL_{reason}",
                    residual_short_call_qty=sc_remaining)
                reconcile_after_close(kite, trade, 'FH')
            send_telegram(
                f"🔴 FH {stock}: PARTIAL SHORT CALL CLOSE\n"
                f"{sc_remaining} qty still SHORT on {sc_sym} after a retry.\n"
                f"The long call was NOT sold — it is the hedge on that naked "
                f"short.\n"
                f"Trade frozen at partial_close and NOT monitored further. "
                f"Close the residue by hand.")
            return False
    else:
        log(f"\n  SHORT CALL {sc_sym} already flat (qty={sc_qty}). Skipping.")

    # ── Step 2: SELL long call (if exists — hedge, no longer needed) ─────
    if lc_sym and lc_qty > 0:
        close_qty = min(lc_qty, qty)
        log(f"\nSTEP 2: SELL LONG CALL {lc_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, lc_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run,
                           urgent=True)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log(f"  WARNING: Long call close failed. Naked long remains — not critical.")
            send_telegram(f"FH {stock}: Long call sell failed. Manual sell {lc_sym}.")
        else:
            fills['long_call'] = result.get('average_price', 0)
    else:
        if lc_sym:
            log(f"\n  LONG CALL {lc_sym} already flat (qty={lc_qty}). Skipping.")

    # ── Step 3: BUY back short put ───────────────────────────────────────
    if sp_qty < 0:
        close_qty = min(abs(sp_qty), qty)
        log(f"\nSTEP 3: BUY back SHORT PUT {sp_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, sp_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run,
                           urgent=True)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log("!!! WARNING: SHORT PUT CLOSE FAILED !!!")
            send_telegram(f"FH {stock}: SHORT PUT CLOSE FAILED! Manual intervention needed!")
            if not dry_run:
                fh_store.set_trade_status(trade['id'], 'partial_close',
                                          close_failed_leg='short_put', close_reason=reason)
            return False
        fills['short_put'] = result.get('average_price', 0)
    else:
        log(f"\n  SHORT PUT {sp_sym} already flat (qty={sp_qty}). Skipping.")

    # ── Step 4: SELL long put ────────────────────────────────────────────
    if lp_qty > 0:
        close_qty = min(lp_qty, qty)
        log(f"\nSTEP 4: SELL LONG PUT {lp_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, lp_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run,
                           urgent=True)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log(f"  WARNING: Long put sell failed. Manual sell {lp_sym}.")
            send_telegram(f"FH {stock}: Long put sell failed. Manual sell {lp_sym}.")
        else:
            fills['long_put'] = result.get('average_price', 0)
    else:
        log(f"\n  LONG PUT {lp_sym} already flat (qty={lp_qty}). Skipping.")

    # ── Summary ──────────────────────────────────────────────────────────
    total_credit = trade['total_credit']
    close_cost = fills['short_call'] + fills['short_put'] - fills['long_call'] - fills['long_put']
    pnl_per_share = total_credit - close_cost
    total_pnl = pnl_per_share * qty

    log("")
    log("=" * 70)
    log("  FH POSITION CLOSED")
    log("=" * 70)
    log(f"  Reason:              {reason}")
    log(f"  Short Call fill:     {fills['short_call']:.2f} (BUY back)")
    log(f"  Long Call fill:      {fills['long_call']:.2f} (SELL)")
    log(f"  Short Put fill:      {fills['short_put']:.2f} (BUY back)")
    log(f"  Long Put fill:       {fills['long_put']:.2f} (SELL)")
    log(f"  Close cost/share:    {close_cost:.2f}")
    log(f"  Entry credit/share:  {total_credit:.2f}")
    log(f"  P&L per share:       {pnl_per_share:+.2f}")
    log(f"  Total P&L:           Rs {total_pnl:+,.0f}")
    log(f"  Spot at close:       {spot}")
    log("=" * 70)

    send_telegram(
        f"FH CLOSED: {stock}\n"
        f"Reason: {reason}\n"
        f"Credit: {total_credit:.2f} | Close cost: {close_cost:.2f}\n"
        f"P&L: Rs {total_pnl:+,.0f}\n"
        f"Spot: {spot}"
    )

    exit_data = {
        'exit_date': datetime.now().isoformat(),
        'exit_reason': reason,
        'exit_spot': spot,
        'short_call_fill': fills['short_call'],
        'long_call_fill': fills['long_call'],
        'short_put_fill': fills['short_put'],
        'long_put_fill': fills['long_put'],
        'close_cost': close_cost,
        'pnl_per_share': pnl_per_share,
        'total_pnl': total_pnl,
    }

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
    """
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

def new_trail_state(trade: dict) -> dict:
    """Trail state dict, restored from persisted trade fields."""
    return {
        'peak': trade.get('trail_peak', 0.0),
        'trail': trade.get('trail_sl', 0.0),
        'active': trade.get('trail_active', False),
        'engage_level': trade['net_debit'] * TRAIL_ENGAGE_MULTIPLIER,
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
    ts['trail'] = accepted * TRAIL_PERCENT
    return True


def new_blind_state() -> dict:
    return {'since': None, 'reason': '', 'last_alert': 0.0, 'last_prox': 0.0,
            'ok_streak': 0}


def _is_auth_error(exc) -> bool:
    """Is this exception the Kite token dying?

    ONE definition, called from both the per-trade handler and the outer loop
    handler. The predicate used to exist only inline in the outer handler, so
    the per-trade path could not recognise the very failure that makes the
    outer handler's alert necessary — and any future edit to one copy would
    have silently diverged from the other.
    """
    s = str(exc).lower()
    return ('token' in s or 'invalidtoken' in s or 'sessionexpired' in s
            or 'api_key' in s)


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
            if bs['since'] is not None:
                send_telegram(f"{label}: spot quotes RECOVERED — triggers "
                              f"re-armed.")
            bs['since'] = None
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
                      f"spot_symbol and consider managing it by hand.")
        bs['last_alert'] = now


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
                      f"SL_SPOT still armed at {sl_spot}.")
        bs['last_alert'] = now
    near = (spot <= sl_spot * (1 + SPOT_PROXIMITY_ALERT_PCT)) if adverse_below \
        else (spot >= sl_spot * (1 - SPOT_PROXIMITY_ALERT_PCT))
    if near and now - bs['last_prox'] >= PROXIMITY_REPEAT_SEC:
        send_telegram(f"{label}: BLIND NEAR SL! spot {spot} vs SL {sl_spot}, quotes "
                      f"unreliable ({bs['reason']}). Spread SLs cannot fire — "
                      f"consider manual action.")
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
    set_log_file(LOG_DIR / f"spread_monitor_{stock}_{date.today().strftime('%Y%m%d')}.log")

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

        elif spot >= target:
            log(f"Spot already at/above target!")
            if close_spread(kite, trade, spot, "TP", dry_run) != 'ABORT':
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
                now_t = datetime.now().time()
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
            if expiry_today and datetime.now().time() >= EXPIRY_FORCE_CLOSE_TIME:
                log(f"\n  *** EXPIRY FORCE CLOSE: {datetime.now().strftime('%H:%M')} >= {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
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
            if spot >= target and not in_abort_cooldown:
                success = close_spread(kite, trade, spot, "TP", dry_run)
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
                send_telegram(f"BCS MONITOR FATAL: Kite token expired! {stock} is UNMONITORED.")
                return
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting.")
                send_telegram(f"BCS MONITOR FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. {stock} is UNMONITORED!")
                return
            if consecutive_errors == 10:
                send_telegram(f"BCS MONITOR WARNING: 10 consecutive errors for {stock}. Last: {e}")
            time.sleep(10)


# ── Cron Mode (All Open Trades — BCS + FH) ──────────────────────────────────

def _load_all_trades(bcs_store, fh_store, bps_store=None) -> list:
    """Load and tag open trades from all stores.

    Returns shallow copies with _strategy/_store_type tags to avoid
    polluting the store's in-memory trade dicts (which get persisted).
    """
    all_trades = []
    for store, strat, st in ((bcs_store, 'BCS', 'bcs'),
                             (bps_store, 'BPS', 'bps'),
                             (fh_store, 'FH', 'fh')):
        if store is None:
            continue
        for t in store.get_open_trades():
            tagged = dict(t)
            tagged['_strategy'] = strat
            tagged['_store_type'] = st
            # DIRECTION COMES FROM THE STORE, so a misfiled record has its
            # stops inverted. `_misfiled` carries the contradiction forward
            # rather than resolving it here — see `trade_is_misfiled`.
            tagged['_misfiled'] = check_leg_types(t, LEG_TYPES_BY_STORE[st])
            all_trades.append(tagged)
    return all_trades


#: Per-book leg expectations, indexed by the same tag `_load_all_trades`
#: stamps. Kept here rather than imported from the three stores so that adding
#: a book without a row is a KeyError at load, not a silently unchecked record.
LEG_TYPES_BY_STORE = {
    'bcs': {'long_symbol': 'CE', 'short_symbol': 'CE'},
    'bps': {'long_symbol': 'PE', 'short_symbol': 'PE'},
    'fh': {'long_put_symbol': 'PE', 'short_put_symbol': 'PE',
           'short_call_symbol': 'CE', 'long_call_symbol': 'CE'},
}


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


def _get_store_for(trade, bcs_store, fh_store, bps_store=None):
    """Return the correct store for a trade based on its _store_type tag."""
    st = trade.get('_store_type')
    if st == 'fh':
        return fh_store
    if st == 'bps' and bps_store:
        return bps_store
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
    """
    flagged = False
    for label, store in books:
        try:
            if not store.read_corruption_marker():
                continue
            flagged = True
            marker = store.corruption_due_for_alert()
            if not marker:
                continue            # already shouted within the hour
            log(f"  *** {label} STORE QUARANTINED: {marker.get('error')} "
                f"— backup {marker.get('backup')} ***")
            send_telegram(
                f"CRITICAL: the {label} trade store was QUARANTINED at "
                f"{marker.get('at')}.\n"
                f"Reason: {marker.get('error')}\n"
                f"Backup: {marker.get('backup')}\n"
                f"The book now reads EMPTY, so any open {label} position is "
                f"UNMONITORED. Check the backup and restore before the next "
                f"session. New trades will NOT reuse the old ids.")
            store.note_corruption_alerted()
        except Exception as e:
            # Never let the alerting path be the thing that stops the monitor.
            log(f"  WARNING: corruption check failed for {label}: {e}")
    return flagged


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
    set_log_file(LOG_DIR / f"spread_monitor_cron_{date.today().strftime('%Y%m%d')}.log")

    # ── Recover trades stuck in 'closing' from a previous crash ─────────
    for label, store in [('BCS', bcs_store), ('BPS', bps_store), ('FH', fh_store)]:
        for t in store.get_closing_trades():
            log(f"  RECOVERY: {label} #{t['id']} {t['stock']} stuck in 'closing'. Resetting to 'open'.")
            send_telegram(f"{label} #{t['id']} {t['stock']}: Recovered from 'closing' (previous crash). Re-monitoring.")
            store.recover_closing_trade(t['id'])

    all_trades = _load_all_trades(bcs_store, fh_store, bps_store)
    wl_active = wl_store.get_active()

    # B7: an empty book may mean "everything closed" or "the file was corrupt
    # and got quarantined". Those need opposite responses, and the second one
    # is the one that leaves live positions unwatched.
    corrupt = alert_store_corruption(
        [('BCS', bcs_store), ('BPS', bps_store), ('FH', fh_store)])

    if not all_trades and not wl_active:
        if corrupt:
            log("Book is empty because a store was QUARANTINED — NOT exiting "
                "quietly. Alert sent; fix the store before the next session.")
            return
        log("No open trades and no active watchlist alerts. Nothing to monitor.")
        return

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
            trail_state[('BCS', t['id'])] = new_trail_state(t)
            if trail_state[('BCS', t['id'])]['active']:
                log(f"  BCS #{t['id']} {t['stock']}: Restored trail: peak={t.get('trail_peak', 0):.2f}, trail={t.get('trail_sl', 0):.2f}")
            log(f"  BCS #{t['id']} {t['stock']} {t['long_symbol']}/{t['short_symbol']} "
                f"| Lots: {lots_str} "
                f"| TP: {t['target_spot']} | SL: {t['sl_spot']} | SL Spread: {t['sl_spread']:.2f}")
        elif strat == 'BPS':
            trail_state[('BPS', t['id'])] = new_trail_state(t)
            if trail_state[('BPS', t['id'])]['active']:
                log(f"  BPS #{t['id']} {t['stock']}: Restored trail: peak={t.get('trail_peak', 0):.2f}, trail={t.get('trail_sl', 0):.2f}")
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
                          f"failing until the token is refreshed.")
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
        if strat == 'BCS':
            long_st = _leg_state(positions, t['long_symbol'], want_long=True)
            short_st = _leg_state(positions, t['short_symbol'], want_long=False)
            if long_st != 'OK' or short_st != 'OK':
                bad = 'FLIPPED' if 'FLIPPED' in (long_st + short_st) else 'MISSING'
                log(f"  WARNING: BCS #{t['id']} {t['stock']} — positions {bad}! "
                    f"(long={long_st}, short={short_st})")
            else:
                log(f"  BCS #{t['id']} {t['stock']} — positions verified")
        elif strat == 'BPS':
            long_st = _leg_state(positions, t['long_symbol'], want_long=True)
            short_st = _leg_state(positions, t['short_symbol'], want_long=False)
            if long_st != 'OK' or short_st != 'OK':
                bad = 'FLIPPED' if 'FLIPPED' in (long_st + short_st) else 'MISSING'
                log(f"  WARNING: BPS #{t['id']} {t['stock']} — positions {bad}! "
                    f"(long={long_st}, short={short_st})")
            else:
                log(f"  BPS #{t['id']} {t['stock']} — positions verified")
        else:
            # FH: verify 3-4 legs
            sc_ok = any(p['tradingsymbol'] == t['short_call_symbol'] and p['quantity'] < 0 for p in positions)
            sp_ok = any(p['tradingsymbol'] == t['short_put_symbol'] and p['quantity'] < 0 for p in positions)
            lp_ok = any(p['tradingsymbol'] == t['long_put_symbol'] and p['quantity'] > 0 for p in positions)
            lc_ok = True
            if t.get('long_call_symbol'):
                lc_ok = any(p['tradingsymbol'] == t['long_call_symbol'] and p['quantity'] > 0 for p in positions)
            missing = []
            if not sc_ok: missing.append('SC')
            if not sp_ok: missing.append('SP')
            if not lp_ok: missing.append('LP')
            if not lc_ok: missing.append('LC')
            if missing:
                log(f"  WARNING: FH #{t['id']} {t['stock']} — legs missing: {', '.join(missing)}")
            else:
                log(f"  FH  #{t['id']} {t['stock']} — all legs verified")

    # ── Expiry day warnings ──────────────────────────────────────────────
    expiry_trades = {}  # {(strategy, trade_id): True}
    for t in all_trades:
        if is_expiry_day(t):
            strat = t['_strategy']
            expiry_trades[(strat, t['id'])] = True
            log(f"  *** {strat} #{t['id']} {t['stock']}: EXPIRY DAY! Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
            send_telegram(f"{strat} #{t['id']} {t['stock']}: EXPIRY DAY. Will force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")
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
                _get_store_for(t, bcs_store, fh_store, bps_store), t, _spot,
                t.get('_strategy', '?'), spread_val=_sv)
        except Exception as e:
            log(f"  WARNING: expiry-proximity check failed for "
                f"#{t['id']} {t.get('stock')}: {e}")

    log(f"\nCron monitoring started at {datetime.now().strftime('%H:%M:%S')}...")
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
                now_t = datetime.now().time()
                if now_t > MARKET_CLOSE:
                    log("Market closed for the day. Cron monitor exiting.")
                    for (strat_key, tid_key), ts in trail_state.items():
                        if ts['active']:
                            log(f"  {strat_key} #{tid_key} trail state: peak={ts['peak']:.2f}, trail={ts['trail']:.2f}")
                    return
                log(f"Market not open yet ({now_t.strftime('%H:%M')}). Waiting...")
                time.sleep(30)
                continue

            # Periodic Drive sync (picks up trades added from other machines)
            bcs_store.maybe_sync()
            bps_store.maybe_sync()
            fh_store.maybe_sync()
            wl_store.maybe_sync()
            all_trades = _load_all_trades(bcs_store, fh_store, bps_store)
            wl_active = wl_store.get_active()

            if not all_trades and not wl_active:
                # A book that went empty MID-SESSION is far more suspicious
                # than one that started empty: the trades were there at 09:15.
                if alert_store_corruption([('BCS', bcs_store),
                                           ('BPS', bps_store),
                                           ('FH', fh_store)]):
                    log("Book emptied MID-SESSION by a store QUARANTINE. "
                        "Alert sent. Exiting — there is nothing left to read.")
                    return
                log("All trades closed and no active watchlist alerts. Cron monitor exiting.")
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
                        "restart the monitor.")
            print_status = (now - last_status_time >= STATUS_PRINT_INTERVAL_SEC)

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
                trade_store = _get_store_for(trade, bcs_store, fh_store, bps_store)

                # Skip trades where close is already in progress
                # Use (strategy, id) as key since BCS and FH IDs are independent
                close_key = (strat, tid)
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
                        if is_expiry_day(trade):
                            expiry_trades[close_key] = True
                            log(f"  {strat} #{tid} {stock}: EXPIRY DAY (added mid-session)")
                            send_telegram(f"{strat} #{tid} {stock}: EXPIRY DAY (added mid-session). Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")
                    if close_key not in confirm_state:
                        confirm_state[close_key] = {'sl_spread': 0, 'sl_trail': 0}
                    if close_key not in blind_state:
                        blind_state[close_key] = new_blind_state()
                    if close_key not in corrob_state:
                        corrob_state[close_key] = {}
                else:
                    # FH: check for new expiry-day trades added mid-session
                    if close_key not in expiry_trades and is_expiry_day(trade):
                        expiry_trades[close_key] = True
                        log(f"  FH #{tid} {stock}: EXPIRY DAY (added mid-session)")
                        send_telegram(f"FH #{tid} {stock}: EXPIRY DAY (added mid-session). Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")

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
                        # B8 — the independent cross-check, ported from
                        # monitor():2282. This is the direct descendant of the
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
                    expiry_tag = " [EXPIRY]" if close_key in expiry_trades else ""
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

                # ── EXPIRY DAY: Force close by EXPIRY_FORCE_CLOSE_TIME ──
                if close_key in expiry_trades and datetime.now().time() >= EXPIRY_FORCE_CLOSE_TIME:
                    if close_key not in closing_in_progress:
                        log(f"\n  {strat} #{tid} {stock} *** EXPIRY FORCE CLOSE: {datetime.now().strftime('%H:%M')} >= {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
                        closing_in_progress[close_key] = "EXPIRY_FORCE_CLOSE"
                        if strat == 'BCS':
                            success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run,
                                                   store=trade_store, strategy_label='BCS')
                        elif strat == 'BPS':
                            success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run,
                                                   store=trade_store, strategy_label='BPS')
                        else:
                            success = close_fh_position(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run)
                        if not success:
                            log(f"  {strat} #{tid} {stock}: Expiry force close FAILED. Manual intervention needed!")
                            send_telegram(f"{strat} #{tid} {stock}: EXPIRY FORCE CLOSE FAILED! Manual intervention needed!")
                        trail_state.pop(close_key, None)
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
                    sl_spot_hit = (spot <= sl_spot_val) if strat == 'BCS' else (spot >= sl_spot_val)
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
                                               store=trade_store, strategy_label=strat)
                        if success == 'ABORT':
                            closing_in_progress.pop(close_key, None)
                        else:
                            closed = True
                            if not success:
                                log(f"  {strat} #{tid} {stock}: Close failed. Trade locked — manual intervention needed.")
                                send_telegram(f"{strat} #{tid} {stock}: SL_SPOT close FAILED. Manual intervention needed!")

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
                                                   reverify_sl=sl_spread_val)
                            if success == 'ABORT':
                                closing_in_progress.pop(close_key, None)
                                confirm['sl_spread'] = 0
                                abort_until[close_key] = time.time() + ABORT_COOLDOWN_SEC
                            else:
                                closed = True
                                if not success:
                                    log(f"  {strat} #{tid} {stock}: Close failed — manual intervention needed.")
                                    send_telegram(f"{strat} #{tid} {stock}: SL_SPREAD close FAILED. Manual intervention needed!")
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
                                                   reverify_sl=ts['trail'])
                            if success == 'ABORT':
                                closing_in_progress.pop(close_key, None)
                                confirm['sl_trail'] = 0
                                abort_until[close_key] = time.time() + ABORT_COOLDOWN_SEC
                            else:
                                closed = True
                                if not success:
                                    log(f"  {strat} #{tid} {stock}: Close failed — manual intervention needed.")
                                    send_telegram(f"{strat} #{tid} {stock}: SL_TRAIL close FAILED. Manual intervention needed!")
                    elif not closed and spread_val is not None and (not ts['active'] or spread_val > ts['trail']):
                        confirm['sl_trail'] = 0

                    # CHECK 4: TP — spot-based (BCS: spot >= target rising;
                    # BPS: spot <= target dropping). ABORT re-fires after the
                    # cooldown since the spot condition persists.
                    tp_hit = (spot >= target) if strat == 'BCS' else (spot <= target)
                    if not closed and tp_hit and not in_abort_cooldown:
                        op = '>=' if strat == 'BCS' else '<='
                        log(f"\n  {strat} #{tid} {stock} *** TP HIT: {spot:.2f} {op} {target} ***")
                        closing_in_progress[close_key] = "TP"
                        success = close_spread(kite, trade, spot, "TP", dry_run,
                                               store=trade_store, strategy_label=strat)
                        if success == 'ABORT':
                            closing_in_progress.pop(close_key, None)
                            abort_until[close_key] = time.time() + ABORT_COOLDOWN_SEC
                            log(f"  {strat} #{tid} {stock}: TP close aborted — cooldown {ABORT_COOLDOWN_SEC}s.")
                        else:
                            closed = True
                            if not success:
                                log(f"  {strat} #{tid} {stock}: Close failed — manual intervention needed.")
                                send_telegram(f"{strat} #{tid} {stock}: TP close FAILED. Manual intervention needed!")

                else:
                    # ── FH SL_SPOT: spot >= sl_spot (upside/bullish risk) ──
                    if spot >= sl_spot_val:
                        log(f"\n  FH #{tid} {stock} *** SL_SPOT HIT: {spot:.2f} >= {sl_spot_val} ***")
                        closing_in_progress[close_key] = "SL_SPOT"
                        success = close_fh_position(kite, trade, spot, "SL_SPOT", dry_run)
                        closed = True
                        if not success:
                            log(f"  FH #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"FH #{tid} {stock}: SL_SPOT close FAILED. Manual intervention needed!")

                if closed:
                    trail_state.pop(close_key, None)
                    confirm_state.pop(close_key, None)
                    blind_state.pop(close_key, None)
                    corrob_state.pop(close_key, None)
                    spot_blind_state.pop(close_key, None)
                    abort_until.pop(close_key, None)

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
            remaining = _load_all_trades(bcs_store, fh_store, bps_store)
            log(f"Remaining open trades: {len(remaining)}")
            return
        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR in cron loop ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            # ONE definition of "the token died", shared with the per-trade
            # handler — see _is_auth_error.
            if _is_auth_error(e):
                log("FATAL: Kite token appears expired. Cannot continue.")
                remaining = _load_all_trades(bcs_store, fh_store, bps_store)
                stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in remaining)
                send_telegram(f"SPREAD MONITOR FATAL: Kite token expired! Unmonitored: {stocks}")
                return
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting.")
                remaining = _load_all_trades(bcs_store, fh_store, bps_store)
                stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in remaining)
                send_telegram(f"SPREAD MONITOR FATAL: {MAX_CONSECUTIVE_ERRORS} errors. Unmonitored: {stocks}")
                return
            if consecutive_errors == 10:
                send_telegram(f"SPREAD MONITOR WARNING: 10 consecutive errors. Last: {e}")
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
    set_log_file(LOG_DIR / f"spread_monitor_{stock}_{date.today().strftime('%Y%m%d')}.log")
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

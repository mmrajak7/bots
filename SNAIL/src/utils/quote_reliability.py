"""
SNAIL Quote-Reliability Guards

Protects value-based decisions from garbage option quotes (unformed / one-sided /
crossed / abnormally wide books).

@file        quote_reliability.py
@description Reliability gating for option top-of-book valuation + execution
@references  Ported from Helper/bcs/spread_monitor.py
             (leg_quote_reliable / price_anchor / bump_confirm, QUOTE_* consts)

WHY THIS EXISTS
---------------
2026-07-24 incident: a sibling bot valued a position off an unformed opening
book (bid 0.28 / ask 1.40 -> width 133% of mid, fair ~0.65), fired a false
stop-loss on a SINGLE poll, and auto-closed a live position at the garbage
1.40 ask. Rs 7,297 lost.

SNAIL is a SEPARATE deployment root from Helper on the Pi, so these helpers are
COPIED here (not imported from Helper/bcs). Keep in sync with the reference if
the detection logic changes.

PRINCIPLES
----------
1. An option top-of-book is two resting orders, not a price. A leg is
   UNRELIABLE for valuation if one side is missing, the book is crossed, or the
   width > max(QUOTE_MAX_WIDTH_ABS, QUOTE_MAX_WIDTH_PCT * mid).
2. LTP is itself a lone print on illiquid strikes: it may only VETO a book
   (never validate one), and only if it traded within LTP_FRESH_SEC.
3. Value-based AUTO-EXIT triggers need N consecutive RELIABLE polls before they
   fire. An unreliable poll FREEZES the counter (does not reset it) so a
   flickering book cannot indefinitely block a genuine exit; a reliable
   non-trigger poll resets it; a stale streak restarts from zero.
4. Must-exit paths (spot SL / VIX / expiry) are NEVER blocked by these guards --
   they do not depend on the option book at all and stay armed every poll.
5. Never validate a price against itself. Execution caps use an EXTERNAL anchor
   (fresh LTP / previous close), never a multiple of the suspect ask/bid.
"""

import json
import time
from datetime import datetime, date
from pathlib import Path
from typing import Any, Optional, Tuple

from loguru import logger


# =============================================================================
# RELIABILITY THRESHOLDS
# =============================================================================

# A cheap option near tick size stays reliable even with a few-tick width.
QUOTE_MAX_WIDTH_ABS = 0.30       # 6 ticks
# Formed NIFTY books run a few % of mid; the incident book was 133%.
QUOTE_MAX_WIDTH_PCT = 0.25

# LTP divergence VETO (only applied when the LTP is fresh).
LTP_DIVERGENCE_MULT = 2.0        # ask > ltp*2 + ABS  (or bid < ltp/2 - ABS) = unreliable
LTP_DIVERGENCE_ABS = 0.10        # exempts tick-sized options from the LTP check
LTP_FRESH_SEC = 30 * 60          # LTP may veto only if last trade < 30 min old

# =============================================================================
# CONSECUTIVE-POLL CONFIRMATION (value-based auto-exit triggers)
# =============================================================================

# SNAIL's monitor runs as cron every ~10 min (see DEPLOYMENT_GUIDE cron entries),
# so a genuine trigger persists across polls while a one-poll artifact does not.
# 2 = one confirming re-poll (~10 min later). Overridable via
# trading_config['exit']['reliability_confirm_polls'].
DEFAULT_CONFIRM_POLLS = 2
# A streak whose last hit is older than this restarts from zero. Sized so two
# back-to-back 10-min polls count, but a missed cron / next session restarts.
CONFIRM_STALE_SEC = 25 * 60

# =============================================================================
# EXECUTION ANCHORS (never validate a price against itself)
# =============================================================================

# Deliberately loose -- an option CAN double intraday; the anchor's only job is
# a last-resort bound on a must-exit fill through an unreliable book. The
# MARKET-order fallback remains the uncapped final attempt.
BUY_CAP_ANCHOR_MULT = 2.5        # buy limit capped at anchor * 2.5
SELL_FLOOR_ANCHOR_DIV = 2.5      # sell limit floored at anchor / 2.5


# =============================================================================
# BOOK RELIABILITY
# =============================================================================

def book_reliable(
    bid: float,
    ask: float,
    bid_qty: Optional[int] = None,
    ask_qty: Optional[int] = None,
    ltp: float = 0.0,
    ltp_fresh: bool = False,
) -> Tuple[bool, str]:
    """Judge whether a leg's top-of-book is trustworthy for VALUATION.

    Mirrors Helper/bcs/spread_monitor.leg_quote_reliable(). Returns
    (reliable, reason). qty args are optional: pass None when depth quantities
    are unavailable (the width test needs no quantities).

    Unreliable if:
      - either side missing / non-positive (no two-way market)
      - a supplied quantity is non-positive (no two-way market)
      - crossed book (bid > ask)
      - width > max(QUOTE_MAX_WIDTH_ABS, QUOTE_MAX_WIDTH_PCT * mid)
      - a FRESH traded LTP contradicts the book

    LTP may only veto -- a book is never validated BY its LTP, and a stale LTP
    cannot blind the monitor against a legitimately repriced book.
    """
    if bid <= 0 or ask <= 0:
        return False, 'no_two_way_book'
    if bid_qty is not None and bid_qty <= 0:
        return False, 'no_bid_depth'
    if ask_qty is not None and ask_qty <= 0:
        return False, 'no_ask_depth'
    if bid > ask:
        return False, 'crossed_book bid {0} > ask {1}'.format(bid, ask)
    width = ask - bid
    mid = (bid + ask) / 2.0
    if width > max(QUOTE_MAX_WIDTH_ABS, QUOTE_MAX_WIDTH_PCT * mid) + 1e-9:
        return False, 'wide_book width {0:.2f} vs mid {1:.2f}'.format(width, mid)
    if ltp and ltp > 0 and ltp_fresh:
        if ask > ltp * LTP_DIVERGENCE_MULT + LTP_DIVERGENCE_ABS:
            return False, 'ask {0} diverges from fresh ltp {1}'.format(ask, ltp)
        if bid < ltp / LTP_DIVERGENCE_MULT - LTP_DIVERGENCE_ABS:
            return False, 'bid {0} diverges from fresh ltp {1}'.format(bid, ltp)
    return True, ''


def quote_reliable(quote: Any) -> Tuple[bool, str]:
    """book_reliable() adapter for a SNAIL Quote (or any bid/ask/ltp object).

    Reads optional depth quantities and LTP-freshness if present on the object;
    tolerates the leaner QuoteWrapper used by the expiry sub-bot.
    """
    bid = getattr(quote, 'bid', 0) or 0
    ask = getattr(quote, 'ask', 0) or 0
    bid_qty = getattr(quote, 'bid_qty', None)
    ask_qty = getattr(quote, 'ask_qty', None)
    ltp = getattr(quote, 'ltp', 0) or 0
    return book_reliable(bid, ask, bid_qty, ask_qty, ltp, ltp_is_fresh(quote))


def ltp_is_fresh(quote: Any) -> bool:
    """True if the quote's last trade happened within LTP_FRESH_SEC today."""
    ltt = getattr(quote, 'last_trade_time', None)
    if not ltt:
        return False
    try:
        if hasattr(ltt, 'date'):
            ltt_dt = ltt
        else:
            ltt_dt = datetime.strptime(str(ltt)[:19], '%Y-%m-%d %H:%M:%S')
        if ltt_dt.date() != date.today():
            return False
        return (datetime.now() - ltt_dt).total_seconds() <= LTP_FRESH_SEC
    except (ValueError, TypeError):
        return False


def traded_today(quote: Any) -> bool:
    """True if the quote's last trade timestamp is today (for the anchor)."""
    ltt = getattr(quote, 'last_trade_time', None)
    if not ltt:
        return False
    try:
        if hasattr(ltt, 'date'):
            ltt_dt = ltt
        else:
            ltt_dt = datetime.strptime(str(ltt)[:19], '%Y-%m-%d %H:%M:%S')
        return ltt_dt.date() == date.today()
    except (ValueError, TypeError):
        return False


def price_anchor(quote: Any) -> float:
    """External fair-price anchor for execution caps.

    max(today's LTP if traded today, previous close). Deliberately the LOOSER
    reference: LTP on an illiquid strike can be a lone stale/garbage print, and
    a too-tight anchor could block a genuine must-exit fill. 0 if no reference.
    """
    ltp = getattr(quote, 'ltp', 0) or 0
    prev_close = getattr(quote, 'prev_close', 0) or 0
    ltp = ltp if (traded_today(quote) and ltp > 0) else 0
    return max(ltp, prev_close)


def cap_buy_price(price: float, anchor: float) -> float:
    """Cap a BUY limit at anchor*BUY_CAP_ANCHOR_MULT (no-op if anchor unknown)."""
    if anchor and anchor > 0:
        return min(price, anchor * BUY_CAP_ANCHOR_MULT)
    return price


def floor_sell_price(price: float, anchor: float) -> float:
    """Floor a SELL limit at anchor/SELL_FLOOR_ANCHOR_DIV (no-op if anchor unknown)."""
    if anchor and anchor > 0:
        return max(price, anchor / SELL_FLOOR_ANCHOR_DIV)
    return price


# =============================================================================
# CONSECUTIVE-POLL CONFIRMATION STORE (cron-safe, file-backed)
# =============================================================================
# The monitor runs as a fresh process every ~10 min under cron, so the
# confirmation counters must survive across processes. A tiny JSON sidecar next
# to snail.db is enough: monitor and exit are serialised by the shared file
# lock and cron polls are minutes apart, so there is no concurrent writer.

_CONFIRM_FILE = Path(__file__).resolve().parents[2] / 'data' / 'reliability_confirm.json'


def _load_confirm() -> dict:
    try:
        if _CONFIRM_FILE.exists():
            with open(_CONFIRM_FILE, 'r') as fh:
                data = json.load(fh)
                return data if isinstance(data, dict) else {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Confirm store read failed ({0}); starting empty".format(exc))
    return {}


def _save_confirm(data: dict) -> None:
    try:
        _CONFIRM_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIRM_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as fh:
            json.dump(data, fh)
        tmp.replace(_CONFIRM_FILE)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Confirm store write failed (non-fatal): {0}".format(exc))


def confirm_bump(key: str) -> int:
    """Increment a trigger-confirmation counter, restarting a stale streak.

    Call ONLY on a reliable poll where the trigger condition is met. Returns the
    new consecutive count.
    """
    data = _load_confirm()
    now = time.time()
    entry = data.get(key)
    if not isinstance(entry, dict) or (now - entry.get('t', 0.0)) > CONFIRM_STALE_SEC:
        entry = {'n': 0, 't': now}
    entry['n'] = int(entry.get('n', 0)) + 1
    entry['t'] = now
    data[key] = entry
    _save_confirm(data)
    return entry['n']


def confirm_reset(key: str) -> None:
    """Reset a trigger-confirmation counter (reliable poll, condition not met)."""
    data = _load_confirm()
    if key in data:
        del data[key]
        _save_confirm(data)


def confirm_reset_position(position_id: int) -> None:
    """Drop all confirmation counters for a position (on exit / close)."""
    data = _load_confirm()
    prefix = '{0}:'.format(position_id)
    changed = False
    for key in list(data.keys()):
        if key.startswith(prefix):
            del data[key]
            changed = True
    if changed:
        _save_confirm(data)

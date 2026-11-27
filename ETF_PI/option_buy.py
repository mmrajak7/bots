"""
NIFTY Option Magnet — SuperTrend on ATM CE/PE option premiums.

Magnet concept on options:
  1. Get NIFTY spot → ATM strike → resolve monthly CE & PE
  2. Compute ST(10,3) on CE and PE option candles (5 TFs: 15M-4H)
  3. Only ST DOWN = premium is BELOW ST (ST is resistance above)
  4. WATCHING: premium within 10% below ST | ENTRY: 5-7% below ST
  5. TP: premium rises to touch its ST from below → PROFIT ✓
  6. Multi-layer exits: cost SL (at 1.5%), trailing, premium SL (-35%), EOD

Why this works:
  ST DOWN on option = premium in downtrend, ST is ceiling above
  Premium starts rising toward ceiling = potential reversal
  Buy cheap (below ST), sell when premium touches ST resistance
  Always profitable at TP because premium RISES from entry to ST level
"""
import pandas as pd
import numpy as np
import datetime
import pytz
import indicators
import os
import sys
import json
import time
import platform
import copy
import math
import common
from kiteconnect import KiteConnect
import logging
import yaml
import atexit
import trade_observer
import turbulence
import underlyings
import market_calendar
import event_calendar

# ---------------------------------------------------------------------------
# Underlying selection (spec SPEC_multi_instrument_2026-10-06 §11 step 2)
# ---------------------------------------------------------------------------
# Decided at IMPORT, before logging is set up: the log file, lock, state, ledger and observer
# paths all depend on it, and a process that logged even one line into NIFTY's live log before
# knowing it was CRUDEOILM would pollute the file every review reads (review-1 C1).
# Order: env OB_UNDERLYING (tests, harness) -> --underlying (cron) -> NIFTY (today's cron line,
# which passes no argument, keeps running NIFTY unchanged).

def _underlying_name():
    name = os.environ.get('OB_UNDERLYING')
    if not name:
        import argparse
        p = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
        p.add_argument('--underlying')
        name = p.parse_known_args()[0].underlying
    return name or 'NIFTY'

U = underlyings.get(_underlying_name())

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    # Single logger name 'option_buy' for every underlying (Tests/_muzzle.py detaches exactly
    # this logger); only the FileHandler path varies. NIFTY's file_suffix is '' -> same name.
    logger = logging.getLogger('option_buy')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    log_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d')
    fh = logging.FileHandler(os.path.join(log_dir, f'option_buy{U.file_suffix}_{today}.log'), encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S'))
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging()
IST = pytz.timezone('Asia/Kolkata')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
TOKEN_PATH = (r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
              if platform.system() == 'Windows'
              else '/home/trustit/Desktop/BOTS/data/kite_access_token.json')

# 30M returned 2026-09-01 as a COMPUTE-ONLY timeframe. It was removed 2026-05-12 for being a
# bad SIGNAL TF (1W/8L over 4 weeks) and that verdict is unchanged — it is NOT in
# SIGNAL_TIMEFRAMES. It is back solely as 15M's T2 anchor, because 60M in that role is
# unreachable: measured over the 33 post-05-12 15M trades that carried a 60M T2, the anchor
# sat a median +46.3% above T1 (max +93.4%), and of the 17 that reached T1, ZERO ever reached
# T2. Contrast 10M -> 15M: median +8.9%, 8 of 20 reached. 60M stays computed because it is now
# 30M's own next-TF and the T2_ALT counterfactual anchor.
ALL_TIMEFRAMES = ['10minute', '15minute', '30minute', '60minute']
# TFs that GENERATE entries. 30M and 60M are computed (ALL_TIMEFRAMES) and kept ONLY as T2
# anchors, but no longer trade their own entries — 60M 2026-05-29: -73.5/lot all-time, PF 0.46,
# negative in BOTH pre/post-05-12 regimes (10t); 30M 2026-05-12: 1W/8L over 4 weeks. Splitting
# compute-TFs from signal-TFs preserves 15M's runner upside (15M holds past T1 toward the next
# TF's ST) while removing their structural drag.
SIGNAL_TIMEFRAMES = ['10minute', '15minute']
TF_DISPLAY = {'10minute': '10M', '15minute': '15M', '30minute': '30M', '60minute': '1H'}
TF_NEXT = {'10minute': '15minute', '15minute': '30minute', '30minute': '60minute'}  # 60min = last, no T2

# The T2 anchor each signal TF USED to use, recorded on the position but NEVER acted on.
# Purpose: price the 2026-09-01 15M anchor switch (60M -> 30M) on real data instead of the
# fixed-multiple proxy it was chosen with. A fixed-multiple sweep over the 17 post-05-12 15M
# T1-hit trades scored T2=T1x1.15 at +2,513 and T1x1.20 at +1,277 but T1x1.30 at +0 — a
# non-monotonic curve on n=17, i.e. noise from 2-4 trades, NOT an established edge. Booking at
# T1 with no T2 at all was clearly worse (-1,818), which is why the anchor was moved closer
# rather than removed. Compare t2_value against t2_alt_value once ~20 trades have accumulated.
T2_ALT = {'15minute': '60minute'}

# ATM reference quote key (index spot for NIFTY/BANKNIFTY). None for MCX, where the reference is
# the futures contract the option expiry maps to — resolved per level update (step 4).
SPOT_KEY = U.ref_key
# NIFTY strikes come from its fixed 50-point grid (the formula the live bot always used). Every
# other underlying picks from the strikes Kite actually lists for the chosen expiry: MCX grids
# differ per contract and change over a contract's life, so a constant would pick dead strikes.
STRIKE_INTERVAL = U.strike_step

# Signal zones (premium below ST, ST DOWN only):
WATCH_GAP_PCT = 10.0       # WATCHING alert: premium within 10% below ST
ENTRY_GAP_PCT = 7.0        # ENTRY trigger: premium within 7% below ST
MIN_REWARD_PCT = 5.0       # minimum upside filter: skip entry if gap < 5%
COST_SL_GAP_PCT = 1.5      # cost SL: premium within 1.5% of ST

# Absolute floor on the T1 target distance, in rupees (2026-08-17).
# MIN_REWARD_PCT is a PERCENTAGE floor with no absolute companion, so as premiums fall the
# same 5-7% band shrinks toward the tick grid. Every threshold in this system is a percentage
# but microstructure noise is in TICKS: at 150 one tick is 0.03% of premium, at 36 it is
# 0.14% — the same band is a quarter as wide in ticks, so bid/ask bounce crosses it that
# much more easily. Measured on 150 trades (04-13->08-17): entries under 80 resolve in a
# median 8.0 min with 78% done inside 10 minutes, vs ~14 min / ~39% for everything above.
# 3.00 (60 ticks) is deliberately set to bind ONLY where there is no track record. It blocks
# exactly 1 of the 150 trades — 2026-08-17 PE 24250, T1 distance 2.12 = 42 ticks, -234,
# below the 1st percentile (p5 = 5.15, median = 9.78). Tighter floors cost real money:
# 5.00 blocks 7 trades worth +1,170, and a minimum-PREMIUM floor at 80 blocks 9 worth
# +2,223 because 50-80 is the second-best premium bucket (PF 2.73, n=8).
# Chosen over a minimum-premium filter on purpose: this gates ROOM TO WORK directly rather
# than a proxy for it, so it stays correct as the premium regime drifts (median entry
# premium has fallen 192 -> 123 since April) and it also catches an expensive option with
# a freakishly tight band, which a premium floor never would.
# HONEST LIMIT: this is a microstructure argument plus a guard sized to cost nothing
# historically — NOT a backtested edge (n=1 below the line). Revisit once trades hit it;
# grep the log for "MIN T1 DIST" to count them.
MIN_T1_DISTANCE = U.min_t1_distance   # NIFTY 3.00; 0 (off) for new underlyings — recorded as t1_dist_ticks

# MAGNET TOUCH gate (2026-09-04). See tasks/SPEC_magnet_touched_2026-09-04.md.
#
# THE DEFECT: every entry threshold is a snapshot of the CURRENT gap, so the entry side has
# no memory of whether the magnet it is aiming at has already been visited today. On
# 2026-09-04 the 15M ST on NIFTY2690824000CE (99.35) was printed through at 10:01 — the
# 1-minute candle high was 99.80 — and the sibling 23950 CE booked T2 on the SAME 15M
# ceiling in that same minute. The scan at 10:01 saw LTP 99.05, a 0.3% gap, and skipped it
# as "insufficient reward"; nothing recorded that the level had been reached. Seven minutes
# later the premium had been rejected to 94.00, the gap read -5.4%, and the bot entered a
# setup whose target had already been consumed. It exited ADV_SL at 86.40, -494. The
# premium entered the entry zone from ABOVE (a pullback off a rejection), not from below
# (an approach toward an untouched magnet), and the % gap cannot tell those apart.
#
# WHAT WAS PRICED (do not re-derive; see memory note project-post-touch-entry-2026-09-04):
#   Ledger, 169 trades — an earlier SAME-SIDE trade today already reached the ST of the
#   candidate's TF (as its T1 or T2): 3 later entries, all losers, -1,063 (+ today's -494
#   = 4/4). Control: entries <=15 min after a same-side WIN whose magnet was NOT the
#   candidate's: n=17, PF 4.06. So this is NOT a cooldown — a plain "silence after a win"
#   would delete the best subset in the ledger. It is specific to the LEVEL being spent.
#   Local logs 08-19->09-04, 17 trades — the candidate strike's own signal-TF gap printed
#   <=1.5% within the previous 15-30 min: 3 blocked, all losers, -965; PF 0.53 -> 0.66.
#
# HONEST LIMIT: n=4. Directional, not conclusive. Shipped SKIP-ONLY, exactly like
# MIN_T1_DISTANCE — no entry_ dedup consumed, no sticky sweep — so a later ST refresh can
# restore the setup rather than blackballing it for the day. Grep the log for
# "MAGNET TOUCH" to count how often it binds.
TOUCH_GAP_PCT = 1.5        # a high within this % of the ST counts as the level having printed
TOUCH_LOOKBACK_MIN = 30    # LEDGER source: how long after its exit a trade still spends the magnet
TOUCH_CANDLE_LOOKBACK_MIN = 15   # CANDLE source: how far back this strike's own highs are read
# The two sources get DIFFERENT windows because the live evidence points opposite ways
# (measured 2026-09-04, the day this gate shipped — see project-post-touch-entry-2026-09-04):
#   LEDGER, on 169 ledger trades: a 30-min window blocks 3 later entries (all losers, -1,063);
#     tightening to 15 min blocks only 2 (-666). Wider is strictly better here, so it stays 30.
#   CANDLES, replayed against the observer record (only 6 of 17 trades testable — Kite serves no
#     history for expired contracts): at 30 min it BLOCKS A WINNER, NIFTY_PE_10minute_20260901
#     (+344), whose touch came 16 min before entry and is NOT the opening-auction spike. Today's
#     loser was touched 7 min before entry, so 15 min separates them: blocked set becomes the
#     loser alone and the kept set goes PF 1.45 -> 1.83.
# HONEST LIMIT: that separation rests on ONE observation and is therefore fitted to the trade
# that motivated it. The independent argument is that this system's horizons are short (median
# TP resolves ~5 min, TIME_SL caps are 30-45 min), so a magnet spent 16 minutes and a round-trip
# ago is weak evidence that it is still spent. Revisit once the gate has fired live a few times.
# TOUCH_GAP_PCT deliberately equals COST_SL_GAP_PCT (1.5): the exit ladder already treats
# "within 1.5% of ST" as REACHED (it arms the cost floor there). The entry side must use the
# same definition of reached — but it stays a separate constant so the two can be tuned
# independently, since one governs an exit and the other an entry.

# Premium-based exits
PREMIUM_SL_PCT = 35         # hard stop: premium drops 35% from entry
TRAIL_PCT = 50              # trail at 50% of peak gain
TRAIL_MIN_GAIN_PCT = 8      # trail starts after 8% gain (lowered 2026-05-12 from 15)

# T1 profit lock: after t1_hit, when peak breaks T1 + buffer, raise SL from cost (entry+0.10) to T1
T1_BUFFER_PCT = 2.0

# Fraction of the entry->T1 gain protected the INSTANT T1 prints (2026-09-01).
#
# THE HOLE THIS CLOSES: before this, a position that hit T1 had exactly two guards, and
# NEITHER was anchored to T1 — the T1 LOCK (peak >= T1 x 1.02) and the TRAIL (peak gain >= 8%).
# A trade whose peak landed between T1 and min(1.02xT1, 1.08xentry) cleared both bars and fell
# through both guards, leaving the floor at entry+0.10 — i.e. keeping ZERO of a 5-8% gain.
# With entry gaps of 5-7% that hole is 1.0-2.1% of premium wide and sits directly on top of
# the modal outcome, because T1 IS the SuperTrend ceiling the premium just touched:
# touch-and-reverse is what this strategy predicts, not an edge case.
#   2026-08-31 CE 24050: peak 128.10 = 1.007 x T1 and +7.47% -> missed the lock by 1.69 and
#                        the trail by 0.53% -> gave back all of +7.5%, booked -0.05.
#   2026-08-26 PE 24250: peak 76.20 = 1.008 x T1 but +8.32% -> landed on the far side of the
#                        trail threshold by 0.32% and was saved. Identical shape, opposite
#                        outcome, decided by a coin flip. That arbitrariness is the defect.
# Measured over the 37 post-T1-LOCK-era T1-hit trades, 3 fell in the hole; a floor at k=0.50
# recovers +690 (k=1.00: +2,194) before slippage.
#
# WHY IT CANNOT HURT A RUNNER: this floor is always BELOW T1, and the existing T1 LOCK already
# raises the floor TO T1 for anything that exceeds T1 by 2%. So this can only bind on trades
# the current ladder leaves completely unprotected, and on those it can only raise the exit.
# The single residual risk is a whipsaw: retrace >50% of the entry->T1 move, then rally to T2.
# 50 is deliberately below the 100 the raw numbers prefer, to buy margin against that.
#
# HONEST LIMIT: only 2 T1-hit trades in the whole ledger have per-minute trajectories (DEBUG
# logging began 2026-08), so the whipsaw rate is UNMEASURED. It is now measurable — the exit
# reports as T1_GIVEBACK, so grep the ledger for that type and check how many later peaked
# above T1 x 1.02. 1-minute polling also caps the realised recovery well below the modelled
# one: on 08-31 the tape went 128.10 -> 121.55 in a single poll, so the recoverable amount
# there was +153 of the +523 T1 gain, not the full figure.
T1_GIVEBACK_KEEP_PCT = 50.0

# Adverse-move SL: exit positions that never moved up
# Catches "never went up" bleeders before TIME_SL drains them at the cap
ADV_SL_PEAK_PCT = 3.0       # if peak gain < this AND
ADV_SL_DRIFT_PCT = 8.0      # current drift <= -this → ADV_SL exit

# ST computation
MIN_ST_CANDLES = 50
HISTORY_DAYS = 60           # Kite max for intraday intervals
LEVEL_UPDATE_SETTLE_S = 15  # level update waits until this second of the minute
MIN_DTE = 1                 # minimum 1 DTE (use weekly expiries)

# Sticky watch: keep monitoring watched strikes even after ATM shift
STICKY_WATCH_MAX_GAP = 13.0  # drop watched strike if gap exceeds this %

# Hand the flight recorder the LIVE exit constants. Without this it falls back to its
# own defaults, and any future change here (TRAIL_PCT 50->40, PREMIUM_SL_PCT 35->30)
# would leave it reporting overshoots against levels the bot never used — a second copy
# of the rules drifting from the first, which is the failure it exists to catch.
trade_observer.configure(
    TRAIL_PCT=TRAIL_PCT, TRAIL_MIN_GAIN_PCT=TRAIL_MIN_GAIN_PCT,
    PREMIUM_SL_PCT=PREMIUM_SL_PCT, ADV_SL_PEAK_PCT=ADV_SL_PEAK_PCT,
    ADV_SL_DRIFT_PCT=ADV_SL_DRIFT_PCT,
    COST_SL_FLOOR_OFFSET=(U.cost_floor_rs if U.cost_floor_rs is not None
                          else U.cost_floor_ticks * U.tick_fallback),
    T1_GIVEBACK_KEEP_PCT=T1_GIVEBACK_KEEP_PCT)
trade_observer.set_stream(U.file_suffix)   # one observer file per underlying; NIFTY unchanged

# Signal & trade timing: no signals or Neo trades before this time
# First 15 min (9:15-9:30) used for ST level computation only
SIGNAL_START_TIME = U.signal_start   # NIFTY 09:30; re-set per session in main() (holiday half-days)

# No-entry windows (2026-06-27): blocked intraday hours where new position OPENS are
# suppressed. Both windows lose in ~every sub-period of the 04-13→06-25 ledger (106 trades):
#   10:30-11:30 — PF 0.21, holds 7 of 18 lifetime ADV_SL knives; post-open momentum exhausts
#   14:15-14:45 — PF 0.31, net-negative in ALL 3 sub-periods; pre-close chop
# Blocking both: net +4,049→+13,416, PF 1.09→1.40, WR 47%→51%. WATCHING alerts and watch-gate
# recording still run; only the actual _open_pos is gated.
# Behavior B (2026-07-17): a setup blocked in a window also consumes the daily entry dedup,
# so it stays blocked for the DAY. Forward test 06-29→07-16 (PF 1.85 overall) showed all 4
# post-window re-entries were instant ADV_SL knives (−₹4,004); without them PF 5.83.
NO_ENTRY_WINDOWS = list(U.no_entry_windows)   # NIFTY: 10:30-11:30, 14:15-14:45; none for new underlyings

def _in_no_entry_window(t=None):
    """True if t (IST time, default = now) falls inside a blocked no-entry window."""
    if t is None:
        t = datetime.datetime.now(IST).time()
    return any(lo <= t < hi for lo, hi in NO_ENTRY_WINDOWS)

# End-of-day handling. EOD_CLOSE_TIME is the hard square-off: every open position is
# force-closed at this time (see _close_eod), so a position opened close to it has no
# room to reach T1/T2 and exits on whatever the premium happens to be.
EOD_CLOSE_TIME = U.eod_close(datetime.datetime.now(IST).date())   # NIFTY 15:25; MCX close-10 (US DST)

# Minimum runway a new position must have before the EOD force-close (2026-08-09).
# Full-history review (145 trades, 04-13→08-07): entries with <45 min of runway went
# 0-for-6 for −₹3,611 in the P3+P4 sample; by bucket, 0-20 min left = 4 trades / −₹2,168
# and 20-45 min left = 2 trades / −₹1,443. Entries at 15:15-15:19 had 6-10 minutes to
# work before a forced exit — structurally broken, not variance.
# 25 min (⇒ last entry 15:00) is the threshold that improves BOTH samples:
#   P3+P4   PF 1.94 → 2.65  (net +10,793 → +13,864)
#   all-time PF 1.26 → 1.32  (net +14,843 → +17,586, drops 9 trades worth −₹2,743)
# Tighter cutoffs (14:45) score better on the recent sample alone but worse all-time,
# so 25 is chosen as the setting that survives out-of-sample rather than the local max.
MIN_MINUTES_TO_EOD = 25

# Lot size used only when the instruments lookup fails (see _get_instrument_info).
LOT_FALLBACK = U.lot_fallback

# NFO option tick. Used for reporting distances in ticks (the unit microstructure noise
# actually moves in) — the per-contract tick_size from instruments is what orders use.
TICK_SIZE = U.tick_fallback

# Backstop age for a position that _close_stale cannot price (see _close_stale). Max weekly
# DTE observed is 8, so anything held this long is certainly past its expiry and worth 0.
MAX_STALE_DAYS = 10

# ---------------------------------------------------------------------------
# Entry-Confirmation Gate — SHADOW MODE (2026-08-10)
# ---------------------------------------------------------------------------
# Problem: 28 of 146 trades never ticked up at all after entry (peak < 1%), costing
# −₹31,466. ADV_SL catches them at a median of 6 min, but only AFTER paying. This is
# an ENTRY problem, not an exit problem.
#
# Design (frozen 2026-08-10): arm when a setup enters the 5-7% zone, track the running
# LOW of the premium while armed, and only enter once the premium turns — LTP rises
# X% off that low — while the setup is still in the zone. Waiting for a candle close
# was rejected: TPs hit at a median of 5 min, so a 10-15 min candle wait would miss
# most winners. Confirmation therefore runs on the existing 1-minute LTP scan.
#
# WHY SHADOW FIRST: this cannot be backtested. Kite purges expired-option intraday
# history, and the ledger records peak_premium but no post-signal MINIMUM — so the
# higher-low rule has no historical analogue at all. Rather than invent X and N, this
# observes and logs what EVERY candidate (X, N) pair would have done while changing
# no behaviour whatsoever. Tune on real forward data, then enable.
#
# SHADOW MODE MUST NOT ALTER CONTROL FLOW. _confirm_observe() only reads, writes its
# own state bucket, and logs. It never returns a decision and is never branched on.
ENTRY_CONFIRM_SHADOW = True          # True = observe/log only. Flip only after tuning.
ENTRY_CONFIRM_X_CANDIDATES = [0.3, 0.5, 1.0]   # % bounce off the running low
ENTRY_CONFIRM_N_CANDIDATES = [10, 15, 20]      # minutes of turn->entry lag to bucket by
# (ENTRY_CONFIRM_MAX_WAIT removed 2026-08-19 — it capped an arm's life at 20min, which was
#  right only while arms were born at entry-eligibility. Arms now start in the watch zone
#  and legitimately live for hours; staleness, below, is what expires them.)
# An arm is observed only while the setup sits inside the signal band. A gap in
# observations longer than this means it LEFT the band and came back, so the running low
# belongs to a different move and must not anchor a bounce. See _confirm_observe.
ENTRY_CONFIRM_STALE_GAP = 15                   # minutes of absence that invalidate an arm
_ARMS_KEY = 'confirm_arms'

def _arm_key(opt_type, tf, strike, today):
    """Arm identity = the INSTRUMENT observed, deliberately NOT the code path observing it.

    `site` was dropped from the key 2026-08-19. Main and sticky both watch the same strike
    — sticky picks it up as ATM drifts away — so keying by site split one continuous price
    path into two stubs. An entry firing just after that handoff would measure its bounce
    against the stub's low instead of the real move's low, and a knife could score as a
    clean turn. `site` survives as a field: stamped at arm time, overwritten at entry with
    the path that actually fired.
    """
    return f"arm_{opt_type}_{tf}_{strike}_{today}"

def _confirm_archive_path():
    return os.path.join(CURRENT_DIR, 'logs', f'confirm_shadow{U.file_suffix}.jsonl')

# Hard cap on retained arms. Only reached if archiving has been failing for days; without
# it a permanently unwritable logs/ would grow st_opt_state.json without bound.
_ARMS_MAX_RETAINED = 500

def _shadow_safe(fn):
    """Shadow instrumentation must NEVER affect trading.

    Without this, a malformed arm record (hand-edited state, a partial write, an older
    on-disk format) would raise inside check_and_enter and stop real entries — the exact
    opposite of what a shadow experiment is for. A broken experiment is acceptable; a
    broken entry path is not. Also gates on ENTRY_CONFIRM_SHADOW so the flag is honoured
    in one place.
    """
    def wrapper(*args, **kwargs):
        if not ENTRY_CONFIRM_SHADOW:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.error(f"CONFIRM shadow error in {fn.__name__} (ignored, "
                      f"trading unaffected): {e}")
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

def _prune_arms(state, today):
    """Archive, then drop, arms from previous days.

    ARCHIVING IS THE POINT. st_opt_state.json is pruned daily, so without this the
    shadow experiment would silently lose its data every night and never accumulate
    the weeks of forward observations it exists to collect. Arms are appended to
    logs/confirm_shadow.jsonl (append-only, one JSON object per line) before removal.
    """
    arms = state.get(_ARMS_KEY, {})
    stale = [k for k, a in arms.items() if a.get('day') != today]
    if not stale:
        return
    try:
        os.makedirs(os.path.dirname(_confirm_archive_path()), exist_ok=True)
        with open(_confirm_archive_path(), 'a', encoding='utf-8') as fh:
            for k in stale:
                rec = dict(arms[k])
                rec['arm_key'] = k
                fh.write(json.dumps(rec) + '\n')
    except Exception as e:
        # Archive failed. Do NOT drop the arms — keep them so the next run can retry,
        # otherwise a transient disk/permission blip silently eats a day of experiment
        # data. Bounded by _ARMS_MAX_RETAINED so a permanent failure cannot grow the
        # state file without limit.
        log.error(f"CONFIRM: archive FAILED, retaining {len(stale)} arm(s) for retry: {e}")
        if len(arms) > _ARMS_MAX_RETAINED:
            drop = sorted(stale, key=lambda k: arms[k].get('armed_at', ''))
            drop = drop[:len(arms) - _ARMS_MAX_RETAINED]
            for k in drop:
                arms.pop(k, None)
            log.error(f"CONFIRM: over {_ARMS_MAX_RETAINED} retained arms — "
                      f"discarded {len(drop)} oldest UNARCHIVED arm(s)")
        return
    for k in stale:
        arms.pop(k, None)
    log.info(f"CONFIRM: archived + pruned {len(stale)} arm(s) from previous days")

@_shadow_safe
def _confirm_observe(state, site, opt_type, tf, strike, ltp, gap, now):
    """SHADOW: track the running low of an in-zone setup and record, for each candidate
    X, how many minutes it took for the premium to turn X% off that low.

    ARMED IN THE WATCH ZONE (revised 2026-08-19). Originally this was called only from
    inside the entry-zone branch — but the real entry fires on that SAME scan pass, so
    every armed-and-entered setup was stamped obs=1, fired={}, entry_after_min=0.0 and the
    experiment could not answer its own question (12 records to 08-17, 5 matched entries,
    all empty). The call now sits ABOVE the zone split at both sites, so an arm is created
    as soon as the setup enters the signal band (gap <= WATCH_GAP_PCT on the main path,
    <= STICKY_WATCH_MAX_GAP on the sticky path) and has a real price path behind it by the
    time entry becomes possible.

    That reframes the question from "should we DELAY entry waiting for a bounce?" to the
    sharper "at the instant we entered, had the premium already turned up off its low, or
    were we still catching a falling knife?" — which gap alone cannot answer: with ST DOWN
    the line descends, so gap can narrow into the entry zone while the premium itself is
    still falling. That is exactly the ADV_SL failure shape.

    Records only. Returns nothing. Never branch on this.
    """
    # E4: never poison the running low with a bad quote. float() first so a non-numeric
    # ltp raises here (caught by _shadow_safe) rather than corrupting an arm downstream.
    if ltp is None or float(ltp) <= 0:
        return
    today = now.strftime('%Y%m%d')
    arms = state.setdefault(_ARMS_KEY, {})
    key = _arm_key(opt_type, tf, strike, today)

    def _rearm(why=''):
        # E1: low == ltp on this scan, so no candidate can confirm here by construction —
        # confirmation always needs at least one later observation.
        # Creates IN PLACE on this scan rather than popping and waiting for the next one:
        # the real entry fires later in this same pass, and an absent arm there means the
        # trade is never stamped and drops out of the experiment silently.
        arms[key] = {
            'day': today, 'site': site, 'opt_type': opt_type, 'tf': tf,
            'strike': strike, 'armed_at': now.isoformat(), 'arm_ltp': round(ltp, 2),
            'arm_gap': round(gap, 2), 'last_seen': now.isoformat(),
            'low': round(ltp, 2), 'obs': 1, 'fired': {}, 'entered_at': None,
        }
        # An arm born already inside the entry zone gets its zone stamp here; one born in
        # the watch zone gets it later, on the scan its gap first narrows to ENTRY_GAP_PCT.
        if gap <= ENTRY_GAP_PCT:
            arms[key]['entry_zone_at'] = now.isoformat()
            arms[key]['entry_zone_low'] = round(ltp, 2)
        log.info(f"CONFIRM ARM: {site} {opt_type} {TF_DISPLAY.get(tf, tf)} {strike} "
                 f"ltp={ltp:.2f} gap={gap:.1f}% "
                 f"zone={'ENTRY' if gap <= ENTRY_GAP_PCT else 'WATCH'}{why}")

    a = arms.get(key)
    if a is None:
        _rearm()
        return

    try:
        armed_at = datetime.datetime.fromisoformat(a['armed_at'])
    except Exception:
        _rearm(' — replaced corrupt record')
        return

    elapsed = (now - armed_at).total_seconds() / 60.0
    if elapsed < 0:
        # Clock skew / an armed_at in the future. Recording a negative delay would
        # silently corrupt the N-attribution, so drop the record and re-arm cleanly.
        log.warning(f"CONFIRM: negative elapsed ({elapsed:.1f}min) for {key} — re-arming")
        _rearm(' — clock skew')
        return

    try:
        last_seen = datetime.datetime.fromisoformat(a.get('last_seen') or a['armed_at'])
    except Exception:
        last_seen = armed_at
    hole = (now - last_seen).total_seconds() / 60.0

    # Staleness, not age, expires an arm (revised 2026-08-19). The old rule froze the
    # record once elapsed passed a fixed 20min cap, which was right only while an arm was
    # born at the moment of entry-eligibility. Arms now start in the WATCH zone and a
    # setup may legitimately marinate for hours; a fixed cap would stop tracking the low
    # exactly on the scans that matter — the ones just before entry. What genuinely
    # invalidates an arm is the setup LEAVING the observed band and coming back: the low
    # then belongs to a different move. Detect that as a hole in observations, and re-arm.
    #
    # NEVER for an arm that already entered. An entered arm IS the experiment's result —
    # it carries entered_at, entry_bounce_pct and fired_at_entry, and nothing archives it
    # until the next morning, so dropping it here deletes a matched trade outright. And
    # entered arms leave the band routinely: winners rally until gap < MIN_REWARD_PCT,
    # knives fall until gap > WATCH_GAP_PCT, and either can drift back hours later. The
    # entry_* fields are already frozen, so the only casualty is post-entry path
    # continuity — flagged for the analysis, not fatal.
    if hole > ENTRY_CONFIRM_STALE_GAP:
        if not a.get('entered_at'):
            _rearm(f" — re-armed, out of band {hole:.0f}min, stale low {a.get('low')}")
            return
        a['post_entry_hole'] = True
        log.info(f"CONFIRM: {site} {opt_type} {TF_DISPLAY.get(tf, tf)} {strike} back in "
                 f"band after {hole:.0f}min — arm already ENTERED, record kept")

    # Both call sites can observe the same strike on the same scan (sticky picks up the
    # ATM strike main is already watching) and they now share one arm. Count that once;
    # the low/fired updates below are idempotent for the same quote.
    if hole * 60.0 >= 2.0:
        a['obs'] = a.get('obs', 0) + 1
    a['last_seen'] = now.isoformat()

    # First scan on which this setup became enterable. Separates "path while merely
    # watched" from "path once enterable" in the analysis.
    if gap <= ENTRY_GAP_PCT and not a.get('entry_zone_at'):
        a['entry_zone_at'] = now.isoformat()
        a['entry_zone_low'] = a.get('low')
    # .get with fallbacks: an arm persisted by an older/partial write may be missing
    # these. Repair in place rather than raising — the record stays usable.
    low = a.get('low')
    if not low or low <= 0:
        low = a['low'] = round(ltp, 2)
    if 'fired' not in a:
        a['fired'] = {}
    if ltp < low:
        # A NEW LOW RESETS THE TURN. Without this, fired[X] would keep recording a turn off
        # a low the premium has since undercut — so an arm could read "confirmed" while the
        # premium was making fresh lows and falling into the entry, which is precisely the
        # knife the gate is meant to catch. It would also contradict entry_bounce_pct,
        # which is always measured off the FINAL low. Reset so the two agree: fired[X]
        # present at entry means the premium turned X% off the low that was still standing.
        a['low'] = round(ltp, 2)
        if a.get('fired'):
            a['low_resets'] = a.get('low_resets', 0) + 1
            a['fired'] = {}
        return                          # a new low is not a turn — nothing can confirm

    for x in ENTRY_CONFIRM_X_CANDIDATES:
        xk = f"{x}"
        if xk in a['fired']:
            continue
        if ltp >= low * (1 + x / 100.0):
            a['fired'][xk] = {'min': round(elapsed, 1), 'ltp': round(ltp, 2),
                              'low': a['low'], 'gap': round(gap, 2)}
            ns = [n for n in ENTRY_CONFIRM_N_CANDIDATES if elapsed <= n]
            log.info(f"CONFIRM FIRE[X={x}%]: {site} {opt_type} {TF_DISPLAY.get(tf, tf)} "
                     f"{strike} low={a['low']:.2f} ltp={ltp:.2f} gap={gap:.1f}% "
                     f"after {elapsed:.1f}min — would pass N={ns if ns else 'NONE'}")

@_shadow_safe
def _confirm_mark_entry(state, site, opt_type, tf, strike, now, ltp):
    """SHADOW: stamp the arm at the moment the REAL entry fires, so the analysis can
    compare 'gate would have entered at t+X' against 'we actually entered at t+0'."""
    today = now.strftime('%Y%m%d')
    a = state.get(_ARMS_KEY, {}).get(_arm_key(opt_type, tf, strike, today))
    if a is None:
        return
    # SNAPSHOT FIRST, then mark entered. If this raised after entered_at was set,
    # _shadow_safe would swallow it and leave an entered arm with no snapshot — which the
    # analyser's back-compat path reads as "fall back to live fired", i.e. exactly the
    # post-entry-contaminated data the snapshot exists to keep out.
    a['fired_at_entry'] = {k: dict(v) for k, v in (a.get('fired') or {}).items()}
    a['site'] = site               # which path actually fired, vs which one armed it
    a['entered_at'] = now.isoformat()
    a['entry_ltp'] = round(ltp, 2)
    # THE headline metric: how far above its running low was the premium when we bought?
    # Near 0 = bought while still falling (the ADV_SL shape). Positive = bought into a turn.
    low = a.get('low')
    if low and low > 0:
        a['entry_bounce_pct'] = round((round(ltp, 2) - low) / low * 100.0, 2)
        a['entry_low'] = low
    # (fired_at_entry was frozen above, before entered_at, and it is load-bearing:
    #  _confirm_observe sits above the zone split, so it keeps running on later scans while
    #  the position is open and goes on adding to a['fired'] — turns that happened AFTER we
    #  were already in the trade. Scoring the gate on those would flatter it with hindsight.
    #  a['fired'] is deliberately left live for a future "should we have waited?" question.)
    try:
        a['entry_after_min'] = round(
            (now - datetime.datetime.fromisoformat(a['armed_at'])).total_seconds() / 60.0, 1)
    except Exception:
        pass
    fired = sorted(a.get('fired_at_entry', {}).keys())
    log.info(f"CONFIRM ENTRY: {site} {opt_type} {TF_DISPLAY.get(tf, tf)} {strike} "
             f"actual entry at +{a.get('entry_after_min', 0)}min after arm "
             f"(obs={a.get('obs', 0)}, low={a.get('entry_low', '?')}, "
             f"bounce={a.get('entry_bounce_pct', '?')}%), "
             f"candidates already confirmed: {fired if fired else 'NONE'}")

# ---------------------------------------------------------------------------
# Pre-entry Momentum — SHADOW MODE (2026-09-25)
# ---------------------------------------------------------------------------
# Question (user, 2026-09-25): does the SPEED / ACCELERATION with which the premium
# arrives in the entry zone predict whether it reaches the magnet (signal-TF ST)?
# First pass on the 08-04→09-25 LTP SCAN logs (85 zone arrivals, 28 matched trades):
# arrivals rising >=1%/min over 5min reached ST ~11% of the time vs 50% at 0.7-1.2%/min,
# and trades entered at v5 >= 1%/min were n=10, -Rs2,023 — i.e. a fast spike into the
# zone looks like exhaustion, not momentum. n is small and thresholds were chosen after
# looking, so this is recorded for an out-of-sample test, NOT acted on.
#
# The bot is a one-shot cron process, so there is no in-memory price history. Every LTP
# the scan already fetches (ATM CE/PE + sticky strikes) is written to a rolling per-symbol
# minute buffer in st_opt_state.json; features are computed from it and stamped on the
# confirm arm at three moments: arm (setup first watched), zone (first enterable scan),
# entry (real entry). Arms archive to logs/confirm_shadow.jsonl with everything else.
#
# SHADOW MODE MUST NOT ALTER CONTROL FLOW — same contract as ENTRY_CONFIRM_SHADOW.
MOMENTUM_SHADOW = True               # True = record/log only. Never gates an entry.
MOMENTUM_WINDOWS = [3, 5, 10, 15]    # minutes; speed = % change per minute over window
MOMENTUM_HISTORY_MIN = 20            # buffer depth; must exceed max(MOMENTUM_WINDOWS)
MOMENTUM_LOOKUP_SLACK = 1            # cron can skip/double a minute; accept t-w +/- this
# An arm/zone event older than this when first seen by _momentum_stamp is NOT stamped: the
# features would describe a later moment than the event (e.g. arms created before this code
# was deployed mid-session). A missing mom_arm/mom_zone is honest; a mis-timed one is not.
MOMENTUM_STAMP_MAX_AGE_MIN = 2
_LTP_HIST_KEY = 'ltp_history'

def _momentum_safe(fn):
    """Mirror of _shadow_safe for the momentum shadow: gated on its own flag, and any
    exception is logged and swallowed so a broken buffer can never stop a real entry."""
    def wrapper(*args, **kwargs):
        if not MOMENTUM_SHADOW:
            return None
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            log.error(f"MOMENTUM shadow error in {fn.__name__} (ignored, "
                      f"trading unaffected): {e}")
            return None
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

@_momentum_safe
def _log_sticky_ltp(opt_type, strike, ltp, levels):
    """Per-minute price line for a sticky strike, in the LTP SCAN format so the same
    parser reads both. Wrapped: it sits inside the sticky entry loop and must not raise."""
    parts = [f"{TF_DISPLAY.get(t, t)}:{(v - ltp) / v * 100:.1f}%↓ST"
             for t, v in levels.items()
             if t in ALL_TIMEFRAMES and v > 0 and v >= ltp]
    if parts:
        log.info(f"  STICKY {opt_type} {strike} LTP={ltp:.2f} | {' | '.join(parts)}")

def _minute_of_day(now):
    return now.hour * 60 + now.minute

@_momentum_safe
def _momentum_record(state, symbol, ltp, now):
    """Append this scan's LTP for `symbol` to the rolling minute buffer and trim it.

    Keyed by trading SYMBOL, not strike: the symbol carries the expiry, so a sticky strike
    from another weekly can never splice into the ATM strike's path. One value per minute
    (last write wins) — main and sticky can quote the same symbol on the same scan.
    """
    if not symbol or ltp is None or float(ltp) <= 0:
        return
    today = now.strftime('%Y%m%d')
    t = _minute_of_day(now)
    hist = state.get(_LTP_HIST_KEY)
    if not isinstance(hist, dict):
        # Self-heal. Raising here would be swallowed by _momentum_safe, but the corrupt
        # value would persist and fail again on every scan for the rest of the day.
        hist = state[_LTP_HIST_KEY] = {}
    rec = hist.get(symbol)
    if (not isinstance(rec, dict) or rec.get('day') != today
            or not isinstance(rec.get('p'), dict)):
        rec = hist[symbol] = {'day': today, 'p': {}}
    rec['p'][str(t)] = round(float(ltp), 2)
    cutoff = t - MOMENTUM_HISTORY_MIN
    # Drop old minutes, other days, and symbols not quoted within the buffer depth (a strike
    # that left ATM and sticky watch would otherwise sit in the state file all day).
    # Unparseable records count as stale, so one bad entry cannot wedge the pruning.
    for s in list(hist):
        r = hist[s]
        try:
            if r.get('day') != today:
                raise ValueError('other day')
            r['p'] = {k: float(v) for k, v in r['p'].items() if int(k) >= cutoff}
            if not r['p']:
                raise ValueError('quiet')
        except Exception:
            hist.pop(s, None)

def _momentum_price_at(p, t):
    """(LTP, minute) at minute t, or the nearest within MOMENTUM_LOOKUP_SLACK (exact wins).
    The minute comes back so speeds divide by the REAL elapsed time, not the nominal window."""
    for d in [0] + [s for k in range(1, MOMENTUM_LOOKUP_SLACK + 1) for s in (-k, k)]:
        v = p.get(str(t + d))
        if v:
            return v, t + d
    return None, None

def _momentum_speed(p_new, m_new, p_old, m_old):
    """% change per minute between two quotes; None if either is missing or dt <= 0."""
    if not p_new or not p_old or m_new is None or m_old is None or m_new <= m_old:
        return None
    return (p_new / p_old - 1) * 100 / (m_new - m_old)

def _momentum_features(state, symbol, ltp, now, gap=None):
    """Speed/acceleration of `symbol` ending at `now`. Pure read. None where history is short.

    v{w}  : % premium change per minute over the last w minutes (+ = rising toward ST)
    acc   : v5 minus the speed over the 5 minutes before that (+ = speeding up)
    hi15 / lo15 : % of ltp vs the 15-min high / low (spike-and-fade vs steady climb)
    at / ltp / gap : when and where it was measured, so each record stands alone (a zone
      stamp can land below MIN_REWARD_PCT when premium jumps straight past 5%).
    """
    hist = state.get(_LTP_HIST_KEY)
    rec = (hist.get(symbol) if isinstance(hist, dict) else None) or {}
    p = rec.get('p', {}) if rec.get('day') == now.strftime('%Y%m%d') else {}
    t = _minute_of_day(now)
    ltp = float(ltp)
    f = {'at': now.isoformat(), 'ltp': round(ltp, 2),
         'gap': round(float(gap), 2) if gap is not None else None, 'n': len(p)}
    for w in MOMENTUM_WINDOWS:
        b, bm = _momentum_price_at(p, t - w)
        v = _momentum_speed(ltp, t, b, bm)
        f[f"v{w}"] = round(v, 3) if v is not None else None
    b5, m5 = _momentum_price_at(p, t - 5)
    b10, m10 = _momentum_price_at(p, t - 10)
    prior = _momentum_speed(b5, m5, b10, m10)
    f['acc'] = (round(f['v5'] - prior, 3)
                if (f['v5'] is not None and prior is not None) else None)
    window = [v for k, v in p.items() if t - 15 <= int(k) <= t] + [ltp]
    f['hi15'] = round((ltp / max(window) - 1) * 100, 2)
    f['lo15'] = round((ltp / min(window) - 1) * 100, 2)
    return f

def _fmt_momentum(f):
    def s(v):
        return 'n/a' if v is None else f"{v:+.2f}"
    return (" ".join(f"v{w}={s(f.get(f'v{w}'))}" for w in MOMENTUM_WINDOWS)
            + f" acc={s(f.get('acc'))} %/min | hi15={f.get('hi15'):+.1f}% "
              f"lo15={f.get('lo15'):+.1f}% (hist {f.get('n')}m)")

def _momentum_fresh(iso, now):
    """True if the event stamped at `iso` happened within MOMENTUM_STAMP_MAX_AGE_MIN."""
    try:
        age = (now - datetime.datetime.fromisoformat(iso)).total_seconds() / 60.0
    except Exception:
        return False
    return 0 <= age <= MOMENTUM_STAMP_MAX_AGE_MIN

@_momentum_safe
def _momentum_stamp(state, stage, site, opt_type, tf, strike, symbol, ltp, gap, now):
    """SHADOW: stamp momentum features on the confirm arm at `stage`.

    stage 'observe' — call right after _confirm_observe. Stamps mom_arm once per arm (a
      re-arm replaces the dict, so the new arm is stamped afresh) and mom_zone on the first
      scan the arm carries entry_zone_at. Covers untraded setups too, which is the point:
      blocked arrivals are the out-of-sample control group.
    stage 'entry'   — call right after a real entry. Always logs; stamps mom_entry if the
      arm exists (it may not, if ENTRY_CONFIRM_SHADOW is off).
    Records only. Returns nothing. Never branch on this.
    """
    a = state.get(_ARMS_KEY, {}).get(_arm_key(opt_type, tf, strike, now.strftime('%Y%m%d')))
    lbl = f"{site} {opt_type} {TF_DISPLAY.get(tf, tf)} {strike}"
    if stage == 'entry':
        f = _momentum_features(state, symbol, ltp, now, gap)
        if a is not None:
            a['mom_entry'] = f
        log.info(f"MOMENTUM ENTRY: {lbl} ltp={float(ltp):.2f} gap={float(gap):.1f}% | "
                 f"{_fmt_momentum(f)} — shadow")
        return
    if a is None:
        return
    # Each event is stamped ONCE and only while fresh (MOMENTUM_STAMP_MAX_AGE_MIN). A stale
    # event is stamped None — the key's presence, not its truthiness, marks it as decided.
    if 'mom_arm' not in a:
        fresh = _momentum_fresh(a.get('armed_at'), now)
        a['mom_arm'] = _momentum_features(state, symbol, ltp, now, gap) if fresh else None
        if fresh:
            log.debug(f"MOMENTUM ARM: {lbl} | {_fmt_momentum(a['mom_arm'])}")
    if a.get('entry_zone_at') and 'mom_zone' not in a:
        fresh = _momentum_fresh(a.get('entry_zone_at'), now)
        a['mom_zone'] = _momentum_features(state, symbol, ltp, now, gap) if fresh else None
        if fresh:
            log.info(f"MOMENTUM ZONE: {lbl} ltp={float(ltp):.2f} gap={float(gap):.1f}% | "
                     f"{_fmt_momentum(a['mom_zone'])} — shadow")

def _minutes_to_eod(t=None):
    """Minutes remaining from t (IST time, default = now) to the EOD force-close."""
    if t is None:
        t = datetime.datetime.now(IST).time()
    return ((EOD_CLOSE_TIME.hour * 60 + EOD_CLOSE_TIME.minute)
            - (t.hour * 60 + t.minute))

def _too_close_to_eod(t=None):
    """True if a new position opened at t would have less than MIN_MINUTES_TO_EOD
    of runway before the EOD force-close. Gates OPENS only — existing positions are
    still monitored and exited normally, including by _close_eod."""
    return _minutes_to_eod(t) < MIN_MINUTES_TO_EOD

# Session halves (2026-10-27, user): MCX energy enters only from U.entry_from; metals get one slot
# per side x TF in EACH half ('per_half'). SESSION_SPLIT is also the ledger's session_half tag.
def _session_half(now=None):
    now = now or datetime.datetime.now(IST)
    return '17+' if now.time() >= underlyings.SESSION_SPLIT else 'pre17'

def _half_suffix(now=None):
    """per_half: evening entries get their own ledger key and entry dedup ('_eve'). '' in every
    other mode, so NIFTY/BANKNIFTY keys and state keys are unchanged."""
    if U.slot_mode != 'per_half':
        return ''
    return '_eve' if _session_half(now) == '17+' else ''

def _slot_left_later_today(now=None):
    """True if using this side x TF slot now still leaves another one later today. Decides whether
    a consumed setup's watched strike is kept (it can fire again) or swept (per-day behaviour)."""
    if U.slot_mode == 'unlimited':
        return True
    if U.slot_mode == 'per_half':
        return _session_half(now) == 'pre17'
    return False

def _before_entry_from(t=None):
    """True while new entries are not yet allowed (U.entry_from). Skip-only: no slot consumed."""
    if U.entry_from is None:
        return False
    t = t or datetime.datetime.now(IST).time()
    return t < U.entry_from

def _log_entry_from_skip(state, site, opt_type, tf, strike, gap, now_iso):
    """INFO once per side x TF per day, then DEBUG — a morning of in-zone scans would otherwise
    write one INFO line per minute per strike."""
    line = (f"ENTRY FROM: {site}{opt_type} {TF_DISPLAY.get(tf, tf)} {strike} gap={gap:.1f}% — "
            f"no entries before {U.entry_from:%H:%M}, skipped (slot kept)")
    dk = f"efrom_{opt_type}_{tf}"
    if _dedup(dk, state):
        state[dk] = {'t': now_iso}
        log.info(line)
    else:
        log.debug(line)

# ---------------------------------------------------------------------------
# Neo Bridge & Time SL Config
# ---------------------------------------------------------------------------

_NEO_BRIDGE = None
TRADE_ENABLED = False
TF_TIME_SL = {}  # populated from config_trade.yaml

def _init_bridge():
    """Initialize Neo bridge from config_trade.yaml if available."""
    global _NEO_BRIDGE, TRADE_ENABLED, TF_TIME_SL
    config_path = os.path.join(CURRENT_DIR, 'config_trade.yaml')
    if not os.path.exists(config_path):
        log.info("No config_trade.yaml — trade execution disabled")
        return

    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        TRADE_ENABLED = cfg.get('trade_enabled', False)
        TF_TIME_SL = cfg.get('time_sl', {})
        # Paper-only underlyings can never reach Neo, whatever config_trade.yaml says: the
        # file is shared by every process, so switching NIFTY live must not arm the others.
        if TRADE_ENABLED and not U.orders_allowed:
            log.warning(f"ORDER PATH DISABLED for {U.name} (paper only) — trade_enabled ignored")
            TRADE_ENABLED = False
        if TRADE_ENABLED:
            from neo import NeoTrader
            _NEO_BRIDGE = NeoTrader(config_path)
            log.info(f"Neo trader active: log_only={_NEO_BRIDGE.log_only}")
        else:
            # Still load TIME_SL even without trade execution
            log.info(f"Trade disabled. TIME_SL loaded: {TF_TIME_SL}")
    except Exception as e:
        log.error(f"Bridge init failed: {e}", exc_info=True)
        TRADE_ENABLED = False

# ---------------------------------------------------------------------------
# Kite Client & Instruments
# ---------------------------------------------------------------------------

_KITE = None
_INSTRUMENTS = None

def get_kite():
    global _KITE
    if _KITE is None:
        try:
            with open(TOKEN_PATH) as f:
                td = json.load(f)
            _KITE = KiteConnect(api_key=td['api_key'])
            _KITE.set_access_token(td['access_token'])
            log.info(f"Kite initialized ({td['api_key'][:4]}****)")
        except FileNotFoundError:
            log.critical(f"Token file not found: {TOKEN_PATH}"); sys.exit(1)
        except (json.JSONDecodeError, KeyError) as e:
            log.critical(f"Token file corrupt or missing keys: {e}"); sys.exit(1)
        except Exception as e:
            log.critical(f"Kite init failed: {e}"); sys.exit(1)
    return _KITE

def get_instruments():
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        _INSTRUMENTS = pd.read_csv(os.path.join(CURRENT_DIR, 'op_instruments.csv'))
        _INSTRUMENTS['expiry'] = pd.to_datetime(_INSTRUMENTS['expiry'])
    return _INSTRUMENTS


# ---------------------------------------------------------------------------
# Kite rate limiting + retry (spec §13, 2026-10-06)
# ---------------------------------------------------------------------------
# WHY: on 2026-08-27/28 a SINGLE bot process logged 31 "Too many requests" failures (turbulence
# quotes, LTP scans, historical fetches and one position quote = an open trade unmonitored that
# minute). They cluster at :05-:09 / :21-:25, i.e. when the other cron bots on the SAME API key
# fire at second 0, and the bot itself spaced quote-family calls only 0.35 s apart against
# Kite's ~1 req/s quote limit. With one process per underlying this would multiply, so every
# Kite call goes through _kite_call():
#   1. a cross-process limiter (file lock): quote family >= QUOTE_MIN_GAP_S apart, historical
#      >= HIST_MIN_GAP_S apart, across ALL bot processes on this machine (it cannot see other
#      bots; cron start offsets keep us out of their second-0 burst);
#   2. up to KITE_RETRIES retries with 1 s / 2 s backoff on rate-limit / network errors only;
#   3. counters, logged once per run, with a daily total and a Telegram above the threshold.
# The limiter only ever sleeps: it never changes WHICH calls are made, so the NIFTY replay proof
# (sleeps are no-ops there) is unaffected. If the lock itself fails the call proceeds unthrottled
# — a broken lock file must never stop trading.
import tempfile

QUOTE_MIN_GAP_S = 1.05
HIST_MIN_GAP_S = 0.34
KITE_RETRIES = 2
KITE_RETRY_BACKOFF_S = (1.0, 2.0)
KITE_FAIL_ALERT_PER_DAY = 10
_RATE_FILE = os.path.join(tempfile.gettempdir(), 'ob_kite_rate.json')
_KITE_STATS = {'calls': 0, 'retries': 0, 'failures': 0, 'wait_s': 0.0}
RATE_MAX_WAIT_S = 15   # deepest queue honoured; beyond this something else is wrong
_LAST_QUOTES = {}   # key -> full kite.quote() record from the most recent scan (bid/ask/ltt)

_RATE_WARNED = []

def _rate_file(family):
    """One lock file per call family, so a historical burst never queues quote calls."""
    root, ext = os.path.splitext(_RATE_FILE)
    return f"{root}_{family}{ext}"

def _rate_wait(family):
    """Wait for this call's slot; never sleeps while holding the lock (review-3 R3-1).

    Under the lock the caller only RESERVES its slot: slot = max(now, last + gap), and the
    file's `last` becomes that slot, so the next process reserves the slot after it. The lock is
    released immediately and the sleep happens outside it — six processes no longer serialise
    behind one sleeper. Fail-open on any lock/file problem (warned once per run, R3-11).
    """
    gap = QUOTE_MIN_GAP_S if family == 'quote' else HIST_MIN_GAP_S
    slot = None
    try:
        fh = open(_rate_file(family), 'a+')
    except Exception as e:
        if not _RATE_WARNED:
            _RATE_WARNED.append(1)
            log.warning(f"Kite rate limiter unavailable ({e}) — calls go out unthrottled this run")
        return
    try:
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except ImportError:
            pass   # Windows (dev/tests): single process, no cross-process lock needed
        fh.seek(0)
        try:
            last = float(json.loads(fh.read() or '{}').get('last', 0))
        except Exception:
            last = 0.0
        now = time.time()
        if last > now + 60:         # a stamp in the FUTURE is corrupt (clock stepped back)
            last = now
        # A deep queue is legitimate (processes bunched after a slow minute): keep the spacing
        # (review-4 R4-4). Only a pathological depth is capped, so a run can never stall.
        slot = min(max(now, last + gap), now + RATE_MAX_WAIT_S)
        fh.seek(0); fh.truncate(); fh.write(json.dumps({'last': slot})); fh.flush()
    except Exception as e:
        if not _RATE_WARNED:
            _RATE_WARNED.append(1)
            log.warning(f"Kite rate limiter skipped ({e}) — calls go out unthrottled this run")
    finally:
        try:
            fh.close()      # releases the flock BEFORE any sleeping
        except Exception:
            pass
    if slot is not None:
        wait = slot - time.time()
        if wait > 0:
            _KITE_STATS['wait_s'] += wait
            time.sleep(wait)

def _is_retryable(e):
    msg = str(e).lower()
    import re
    return (type(e).__name__ in ('NetworkException', 'ConnectionError', 'Timeout', 'ReadTimeout')
            or 'too many requests' in msg or re.search(r'(^|\D)429(\D|$)', msg) is not None
            or 'gateway timeout' in msg)

def _kite_call(family, fn):
    """Run one Kite call through the limiter with bounded retry. Raises the last error."""
    for attempt in range(KITE_RETRIES + 1):
        _rate_wait(family)
        _KITE_STATS['calls'] += 1
        try:
            return fn()
        except Exception as e:
            if attempt < KITE_RETRIES and _is_retryable(e):
                _KITE_STATS['retries'] += 1
                log.warning(f"Kite {family} retry {attempt + 1}/{KITE_RETRIES}: {e}")
                time.sleep(KITE_RETRY_BACKOFF_S[attempt])
                continue
            _KITE_STATS['failures'] += 1
            raise

_KITE_STATS_CLOSED = []

def _kite_stats_close():
    """End-of-run: one log line + daily failure total in state; Telegram once above threshold.

    Called before COMPLETE and registered with atexit (review-3 R3-15) so a run that dies in
    the scan still counts its failures; the flag makes the second call a no-op.
    """
    if _KITE_STATS_CLOSED:
        return
    _KITE_STATS_CLOSED.append(1)
    if not (_KITE_STATS['calls'] or _KITE_STATS['failures']):
        return
    # Day-1 cost check (review-4 R4-4/R4-16): logged when anything happened worth seeing.
    if _KITE_STATS['retries'] or _KITE_STATS['failures'] or _KITE_STATS['wait_s'] >= 3:
        log.info(f"Kite calls: {_KITE_STATS['calls']} retries: {_KITE_STATS['retries']} "
                 f"failures: {_KITE_STATS['failures']} wait: {_KITE_STATS['wait_s']:.1f}s")
    if not _KITE_STATS['failures']:
        return
    try:
        state = _load(_state_path())
        k = f"kite_failures_{datetime.datetime.now(IST).strftime('%Y%m%d')}"
        prev = int(state.get(k, 0) or 0)
        state[k] = prev + _KITE_STATS['failures']
        if prev < KITE_FAIL_ALERT_PER_DAY <= state[k]:
            common.sendMsgTelegram(f"<b>\u26a0\ufe0f KITE</b> {U.name}: {state[k]} failed calls today "
                                   f"(rate limit / network) — check other bots on the API key", 'I')
        _save(state, _state_path())
    except Exception as e:
        log.warning(f"Kite failure counter not saved: {e}")

# ---------------------------------------------------------------------------
# API Helpers
# ---------------------------------------------------------------------------

def _hist(token, start, end, interval):
    kite = get_kite(); time.sleep(0.35)
    try:
        data = _kite_call('hist', lambda: kite.historical_data(int(token), start, end, interval, continuous=False))
        if not data: return pd.DataFrame()
        return pd.DataFrame(data).rename(columns={
            'date':'Date','open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})
    except Exception as e:
        log.error(f"Hist failed ({token},{interval}): {e}"); return pd.DataFrame()

def _ltp(keys):
    kite = get_kite(); time.sleep(0.35)
    try:
        # quote() instead of ltp(): same last_price, plus bid/ask/last_trade_time/depth that the
        # liquidity gate reads from _LAST_QUOTES without a second call (review-1 H5).
        r = _kite_call('quote', lambda: kite.quote(keys))
        _LAST_QUOTES.update(r or {})
        return {k: v['last_price'] for k, v in r.items() if v.get('last_price', 0) > 0}
    except Exception as e:
        log.error(f"LTP failed: {e}"); return {}

INDIA_VIX_KEY = 'NSE:INDIA VIX'   # instrument token 264969, NSE INDICES

def _scan_turbulence(spot):
    """B6: feed the abrupt-move tripwire, once per scan.

    One `kite.quote` call covering NIFTY and India VIX. `quote` carries an `ohlc` block
    whose `close` is the PREVIOUS session's close — which is what makes the overnight
    gap and the VIX jump computable without a second historical fetch.

    Never raises and never gates: `turbulence.BLOCK_ENTRIES` is False, so this records
    and alerts only. A quote failure means the tripwire simply sees nothing this scan,
    which `evaluate` reports as signals_ok=0 rather than as calm.
    """
    try:
        kite = get_kite()
        if not kite:
            return
        time.sleep(0.35)
        q = _kite_call('quote', lambda: kite.quote([SPOT_KEY, INDIA_VIX_KEY]))
        n = q.get(SPOT_KEY, {}) or {}
        v = q.get(INDIA_VIX_KEY, {}) or {}
        n_ohlc = n.get('ohlc', {}) or {}
        v_ohlc = v.get('ohlc', {}) or {}
        verdict = turbulence.evaluate(
            vix=v.get('last_price'), vix_prev_close=v_ohlc.get('close'),
            spot=spot or n.get('last_price'), prev_close=n_ohlc.get('close'),
            day_open=n_ohlc.get('open'), day_high=n_ohlc.get('high'),
            day_low=n_ohlc.get('low'))
        turbulence.observe(verdict,
                           notify=lambda m: common.sendMsgTelegram(m, 'I'))
    except Exception as e:
        log.warning(f"Turbulence scan failed (ignored, trading unaffected): {e}")

def _quote(key):
    kite = get_kite(); time.sleep(0.35)
    try:
        q = _kite_call('quote', lambda: kite.quote([key]))
        _LAST_QUOTES.update(q or {})
        if key not in q: return {'bid':0,'ask':0,'ltp':0}
        d = q[key]; depth = d.get('depth',{})
        bid = depth.get('buy',[{}])[0].get('price',0) if depth.get('buy') else 0
        ask = depth.get('sell',[{}])[0].get('price',0) if depth.get('sell') else 0
        # DAY HIGH (2026-08-21). The quote already carries an ohlc block; it was being
        # thrown away. It is the only way a once-a-minute scan can learn that the
        # premium traded somewhere it never sampled.
        # WHY THIS MATTERS: on 2026-08-21 PE 24200 was polled at 62.65 and 63.75 while
        # the 09:34 minute bar actually printed a HIGH of 65.00 — above its T1 of 64.51.
        # The target was touched and the bot never saw it, so a +Rs267 TP was booked as
        # a -Rs23 cost-SL scratch. `peak_premium` is built from polls and therefore
        # UNDERSTATES the true high; every analysis resting on it inherits that bias.
        # Recorded only — no exit logic reads this. See trade_observer.on_tick.
        ohlc = d.get('ohlc', {}) or {}
        return {'bid':bid, 'ask':ask, 'ltp':d.get('last_price',0),
                'day_high':ohlc.get('high',0) or 0,
                'day_low':ohlc.get('low',0) or 0}
    except Exception as e:
        log.error(f"Quote failed {key}: {e}")
        return {'bid':0,'ask':0,'ltp':0,'day_high':0,'day_low':0}

# ---------------------------------------------------------------------------
# Option Resolution
# ---------------------------------------------------------------------------

def get_atm_strike(spot):
    return round(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL

def get_otm_strike(spot, opt_type, expiry=None):
    """ATM or 1 OTM strike — never ITM.
    CE: round up (strike >= spot), PE: round down (strike <= spot).
    Fixed grid when STRIKE_INTERVAL is set (NIFTY); otherwise the nearest strike LISTED for
    `expiry` on the right side of the reference price (None if the chain has none).
    """
    if STRIKE_INTERVAL:
        if opt_type == 'CE':
            return int(math.ceil(spot / STRIKE_INTERVAL)) * STRIKE_INTERVAL
        return int(math.floor(spot / STRIKE_INTERVAL)) * STRIKE_INTERVAL
    strikes = _listed_strikes(expiry)
    side = [s for s in strikes if s >= spot] if opt_type == 'CE' else [s for s in strikes if s <= spot]
    if not side:
        return None
    s = side[0] if opt_type == 'CE' else side[-1]
    return int(s) if float(s).is_integer() else float(s)

def _option_rows(expiry=None):
    """This underlying's CE/PE rows (optionally for one expiry) from the cached instruments."""
    df = get_instruments()
    rows = df[(df['name'] == U.name) & (df['exchange'] == U.exchange) &
              (df['instrument_type'].isin(['CE', 'PE']))]
    if expiry is not None:
        rows = rows[rows['expiry'].dt.date == expiry]
    return rows

def _listed_strikes(expiry):
    if expiry is None:
        return []
    return sorted(float(s) for s in _option_rows(expiry)['strike'].unique())

def _ref_key(expiry):
    """Quote key of the price the ATM strike is chosen from.

    Index underlyings: the index spot (unchanged NIFTY behaviour). MCX: the futures contract
    the option expiry settles into = the first FUT expiring on/after the option expiry
    (GOLDM Oct options -> GOLDM NOV fut). Re-derived on every call, never cached in state, so
    the mapping follows the contract roll (review-1 M16). None if no such contract is listed.
    """
    if U.ref_key:
        return U.ref_key
    df = get_instruments()
    futs = df[(df['name'] == U.name) & (df['exchange'] == U.exchange) & (df['instrument_type'] == 'FUT')]
    if expiry is not None:
        futs = futs[futs['expiry'].dt.date >= expiry]
    if futs.empty:
        log.error(f"No futures contract for {U.name} expiring on/after {expiry} — cannot pick ATM")
        return None
    return f"{U.exchange}:{futs.sort_values('expiry').iloc[0]['tradingsymbol']}"

def _get_instrument_info(opt_sym):
    """Look up lot_size and tick_size from instruments for a trading symbol."""
    df = get_instruments()
    m = df[df['tradingsymbol'] == opt_sym]
    # lot_size is the P&L multiplier (pnl = (exit-entry) x lot_size). Kite lists every MCX
    # contract with lot_size=1 (quantity in lots), so MCX underlyings carry their units per lot
    # in underlyings.py; NFO keeps the instruments value (NIFTY 65) exactly as before.
    if not m.empty:
        return {
            'lot_size': int(U.multiplier or m['lot_size'].iloc[0]),
            'tick_size': float(m['tick_size'].iloc[0]),
        }
    # Fallback: any instrument of this underlying (lot_size is same across strikes)
    nifty = df[df['name'] == U.name]
    if not nifty.empty:
        return {
            'lot_size': int(U.multiplier or nifty['lot_size'].iloc[0]),
            'tick_size': float(nifty['tick_size'].iloc[0]),
        }
    # Last-resort default. NIFTY's lot is 65 (verified against op_instruments.csv,
    # 1792 contracts) — this was 75, which would have oversized every paper trade by
    # 15% whenever the instrument lookup failed. Kept as a fallback (paper sizing
    # must not crash) but logged at ERROR: reaching this means instruments.csv is
    # stale or the download failed, which needs fixing before live trading.
    log.error(f"Instrument info NOT FOUND for {opt_sym} — falling back to "
              f"lot_size={LOT_FALLBACK}; check instruments.csv freshness")
    return {'lot_size': LOT_FALLBACK, 'tick_size': U.tick_fallback}

def _token_for_symbol(opt_sym):
    """instrument_token for a tradingsymbol, or None if it is not in the instrument list.

    Same lookup _get_instrument_info and _resolve_opt already do, over the SAME cached
    frame (get_instruments memoises _INSTRUMENTS), so this adds no second CSV read.
    Added for the MAGNET TOUCH candle check, which is handed a symbol but needs a token.
    Never raises: the caller is in the entry hot path and treats None as "no evidence".
    """
    try:
        df = get_instruments()
        m = df[df['tradingsymbol'] == opt_sym]
        if m.empty:
            return None
        return int(m['instrument_token'].iloc[0])
    except Exception as e:
        log.warning(f"MAGNET TOUCH: token lookup failed for {opt_sym}: {e}")
        return None

def get_weekly_expiry():
    """Pick nearest NIFTY expiry with DTE >= MIN_DTE (weekly or monthly)."""
    df = get_instruments()
    today = datetime.datetime.now(IST).date()
    opts = df[(df['name']==U.name)&(df['exchange']==U.exchange)&(df['instrument_type'].isin(['CE','PE']))]
    if opts.empty: return None
    expiries = sorted(opts['expiry'].dt.date.unique())
    for e in expiries:
        dte = (e - today).days
        if dte >= MIN_DTE:
            return e
    return None

def _resolve_opt(strike, expiry, opt_type):
    """(instrument_token, tradingsymbol) for one contract, or (None, None).

    Looked up by (name, exchange, expiry, strike, type) — never by formatting a symbol string:
    symbol formats differ per exchange and per weekly/monthly expiry (review-2 H8). For NIFTY
    the result is the same contract the old weekly/monthly string formats found.
    """
    if strike is None or expiry is None:
        return None, None
    rows = _option_rows(expiry)
    m = rows[(rows['instrument_type'] == opt_type) & ((rows['strike'] - float(strike)).abs() < 1e-6)]
    if m.empty:
        return None, None
    return int(m['instrument_token'].iloc[0]), m['tradingsymbol'].iloc[0]

# ---------------------------------------------------------------------------
# SuperTrend on Option Candles
# ---------------------------------------------------------------------------

def _candle_meta(df):
    """Recorded only (review-1 H9, spec A1): how much of TODAY the ST was computed on.

    Not a gate: Kite fills dead MCX minutes with zero-volume candles, so candle presence proves
    nothing — the entry-time last_trade_time freshness gate is the enforcement. These fields let
    day-1 logs show whether the forming-candle phase assumption holds per underlying.
    """
    try:
        d = pd.to_datetime(df['Date'])
        today = datetime.datetime.now(IST).date()
        mask = d.dt.date == today
        return {'candles_today': int(mask.sum()),
                'vol_candles_today': int((df.loc[mask, 'Volume'] > 0).sum()) if 'Volume' in df else None,
                'last_candle': str(d.iloc[-1])}
    except Exception:
        return {}

def _st(df):
    if df.empty or len(df) < MIN_ST_CANDLES: return None
    r = indicators.SuperTrend(df.copy(), 10, 3)
    if 'ST' not in r.columns or r['ST'].iloc[-1] == 0: return None
    v = float(r['ST'].iloc[-1]); c = float(r['Close'].iloc[-1])
    out = {'direction': 'UP' if c >= v else 'DOWN', 'value': v, 'close': c}
    out.update(_candle_meta(df))
    return out

def compute_option_st(token):
    """Compute ST on option candles for active TFs (10M, 15M, 1H — see ALL_TIMEFRAMES)."""
    now = datetime.datetime.now(IST)
    s = (now - datetime.timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')
    e = now.strftime('%Y-%m-%d')
    results = {}
    insuf = {'direction':'INSUFFICIENT','value':0,'close':0}

    for tf in ALL_TIMEFRAMES:
        df = _hist(token, s, e, tf)
        results[tf] = _st(df) or insuf.copy()

    return results

# ---------------------------------------------------------------------------
# JSON Helpers
# ---------------------------------------------------------------------------

class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, np.ndarray): return o.tolist()
        return super().default(o)

def _save(data, path):
    tmp = path+'.tmp'
    with open(tmp,'w') as f: json.dump(data,f,indent=2,cls=_Enc)
    os.replace(tmp, path)

def _load(path):
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except Exception: return {}

def _levels_path():
    return os.path.join(CURRENT_DIR, f"st_opt_levels{U.file_suffix}_{datetime.datetime.now(IST).strftime('%Y-%m-%d')}.json")
def _state_path(): return os.path.join(CURRENT_DIR, f'st_opt_state{U.file_suffix}.json')
def _pos_path(): return os.path.join(CURRENT_DIR, f'st_opt_positions{U.file_suffix}.json')

# ---------------------------------------------------------------------------
# Sticky Watch Helpers
# ---------------------------------------------------------------------------

def _has_signal_level(info):
    """True if a watched-strike record still holds a level on a TF that can trade.

    A record left holding only anchor TFs (60M) can never produce an entry —
    _check_watched_strikes skips non-SIGNAL_TIMEFRAMES — so it is dead weight that
    would still cost an LTP fetch every scan until the next day's cleanup.
    """
    return any(t in SIGNAL_TIMEFRAMES for t in (info or {}).get('levels', {}))

def _record_watched_strike(state, opt_type, strike, symbol, expiry_iso, tf, st_val,
                           all_levels=None):
    """Add a watched strike to persistent state for cross-ATM monitoring.

    all_levels: the full per-TF level dict for this option. Supplying it also records
    the T2 ANCHOR level (the next higher TF, when that TF is not itself a signal TF).

    WHY (bug fix 2026-08-17): this function is only ever called from inside
    `for tf in SIGNAL_TIMEFRAMES`, so 60M was never written into 'levels'. The T2
    lookup in _check_watched_strikes (`s_next in levels`) therefore could not succeed
    for a 15M sticky entry, and every one of them silently degraded to T1-only —
    exactly the runner upside SIGNAL_TIMEFRAMES was split out to preserve.

    Only NON-signal anchors are recorded here. Writing 10M's anchor (15M) would inject
    a 15M level that never passed its own watch gate, and the sticky loop would then
    treat it as an independent entry candidate — a new entry, not a restored target.
    """
    ws = state.setdefault('watched_strikes', {})
    key = f"{strike}_{opt_type}"
    if key not in ws:
        ws[key] = {
            'strike': strike, 'opt_type': opt_type,
            'symbol': symbol, 'expiry': expiry_iso,
            'levels': {}, 'added': datetime.datetime.now(IST).isoformat()
        }
    ws[key]['levels'][tf] = st_val

    # Record the live T2 anchor AND the pre-2026-09-01 anchor (T2_ALT). The second is a
    # counterfactual the sticky entry path stamps on the position and never trades; without
    # it here, sticky 15M entries would carry no alt anchor and would be silently missing
    # from the 60M -> 30M comparison — the same class of gap as the 2026-08-17 bug this
    # function's docstring describes.
    for anchor in (TF_NEXT.get(tf), T2_ALT.get(tf)):
        if not anchor or anchor in SIGNAL_TIMEFRAMES or all_levels is None:
            continue
        d = all_levels.get(anchor, {})
        # Same qualification as the main path (check_and_enter): the anchor must be
        # DOWN with an ST strictly above this TF's, else it is not a reachable target.
        if d.get('direction') == 'DOWN' and d.get('value', 0) > st_val:
            ws[key]['levels'][anchor] = d['value']
        else:
            # No longer qualifies — drop any anchor recorded on an earlier scan so a
            # stale level cannot be resurrected as T2 later in the day.
            ws[key]['levels'].pop(anchor, None)

def _remove_watched_strike(state, opt_type, strike, tf=None):
    """Remove a watched strike (or specific TF) after entry or invalidation."""
    ws = state.get('watched_strikes', {})
    key = f"{strike}_{opt_type}"
    if key not in ws:
        return
    if tf:
        ws[key].get('levels', {}).pop(tf, None)
        # Drop the record once no tradeable TF remains. Checking for an empty dict is
        # not enough now that anchor levels are stored: removing the 15M level after an
        # entry would otherwise leave a 60M-only orphan that can never fire again.
        if not _has_signal_level(ws[key]):
            del ws[key]
    else:
        del ws[key]

def _sticky_t2(levels, tf, st_val):
    """(t2, t2_tf, t2_alt, t2_alt_tf) for a sticky entry from the watched-strike levels.

    Nearest qualifying anchor, falling back to T2_ALT — see check_and_enter. The alt anchor
    is a counterfactual only — recorded, never acted on. See T2_ALT.
    """
    t2, t2_tf = 0, ''
    for cand in (TF_NEXT.get(tf), T2_ALT.get(tf)):
        if cand and cand in levels and levels[cand] > st_val:
            t2, t2_tf = levels[cand], cand
            break
    alt, alt_tf = 0, ''
    a = T2_ALT.get(tf)
    if a and a in levels and levels[a] > st_val:
        alt, alt_tf = levels[a], a
    return t2, t2_tf, alt, alt_tf

def _sticky_fresh_levels(symbol, tfs):
    """ST for a watched strike on just `tfs`, read now (same _hist/_st as the level update),
    or None. Only the TFs the recheck needs (signal TF + its anchors), not all four: this can
    run every minute while a sticky setup sits in zone behind MAGNET TOUCH. A failed read
    comes back INSUFFICIENT (_hist swallows Kite errors) — the caller treats that as no data.
    Wrapped: it sits in the entry hot path."""
    tok = _token_for_symbol(symbol)
    if not tok:
        return None
    try:
        now = datetime.datetime.now(IST)
        s = (now - datetime.timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')
        e = now.strftime('%Y-%m-%d')
        insuf = {'direction': 'INSUFFICIENT', 'value': 0, 'close': 0}
        return {t: _st(_hist(tok, s, e, t)) or dict(insuf) for t in tfs}
    except Exception as e:
        log.warning(f"STICKY RECHECK: level fetch failed for {symbol}: {e}")
        return None

def _sticky_recheck(symbol, opt_type, tf, strike, ltp, levels):
    """Re-validate a sticky entry against the CURRENT ST before it opens.

    WHY (2026-10-30 week replay): a watched strike's levels are written when the strike was
    ATM and never refreshed afterwards, so a sticky entry could fire hours later on a level
    whose trend had already flipped — CRUDEOILM PE 9650 10-28 17:56 entered on ST 511.22 DOWN
    while the 10M ST was 461.83 UP; GOLDM PE 143000 10-29 20:52 used the 20:16 level, UP
    since 20:26. The main path never has this problem: its levels refresh every 10 minutes.

    Called only once every other gate has passed, so the extra Kite reads (one per TF) are
    spent only on a would-be entry. Refreshes `levels` in place (this TF + its anchors) so
    the next minute evaluates the current level instead of re-checking the stale one.

    Returns (action, st_val, gap, t2_levels):
      'enter' — st_val/gap are current; t2_levels holds only anchors that qualify NOW
                (feed it to _sticky_t2)
      'skip'  — keep watching (out of zone on the current level, or no data)
      'drop'  — sweep this TF (trend flipped, or premium already above the current ST)
    No fresh data (Kite error, too few candles) -> 'skip' (fail CLOSED for this minute: the
    stale level is exactly what this guards) — never 'drop', a hiccup must not cost the day.
    Only a real UP reading drops. A forming-candle flip can make that UP transient; the drop
    stands for the day, the same outcome as an ATM strike whose level update reads UP.
    """
    tag = f"{opt_type} {strike} {TF_DISPLAY.get(tf, tf)}"
    old = levels.get(tf, 0)
    anchors = [a for a in (TF_NEXT.get(tf), T2_ALT.get(tf)) if a]
    fresh = _sticky_fresh_levels(symbol, [tf] + anchors)
    d = (fresh or {}).get(tf) or {}
    if d.get('direction') not in ('UP', 'DOWN') or d.get('value', 0) <= 0:
        log.warning(f"STICKY RECHECK: {tag} — no current level, entry skipped this minute")
        return 'skip', 0.0, 0.0, {}
    if d['direction'] != 'DOWN':
        log.info(f"STICKY DROP: {tag} — recorded ST {old:.2f} is stale, "
                 f"{TF_DISPLAY.get(tf, tf)} now {d.get('direction', '?')} ST {d.get('value', 0):.2f}")
        return 'drop', 0.0, 0.0, {}
    st_val = float(d['value'])
    levels[tf] = st_val
    t2_levels = {tf: st_val}
    for anchor in anchors:
        ad = fresh.get(anchor) or {}
        if ad.get('direction') not in ('UP', 'DOWN'):
            continue   # anchor unreadable: leave the watched record as it is, no T2 from it
        anchor_ok = ad.get('direction') == 'DOWN' and ad.get('value', 0) > st_val
        if anchor in SIGNAL_TIMEFRAMES:
            # A signal-TF anchor is also its own entry candidate: refresh its value but never
            # remove it here — its own pass of the sticky loop decides its fate. It serves as
            # T2 only if it was watched (as before) AND qualifies now.
            if anchor in levels and ad.get('direction') == 'DOWN' and ad.get('value', 0) > 0:
                levels[anchor] = float(ad['value'])
            if anchor in levels and anchor_ok:
                t2_levels[anchor] = float(ad['value'])
        elif anchor_ok:
            levels[anchor] = t2_levels[anchor] = float(ad['value'])
        else:
            levels.pop(anchor, None)   # same qualification as _record_watched_strike
    gap = ((st_val - ltp) / st_val) * 100
    if gap < 0:
        log.info(f"STICKY DROP: {tag} — prem {ltp:.2f} above current ST {st_val:.2f} "
                 f"(recorded {old:.2f})")
        return 'drop', 0.0, 0.0, {}
    if gap < MIN_REWARD_PCT or gap > ENTRY_GAP_PCT or (st_val - ltp) < MIN_T1_DISTANCE:
        log.info(f"STICKY RECHECK: {tag} — current ST {st_val:.2f} (recorded {old:.2f}) "
                 f"puts gap at {gap:.1f}%, outside entry zone — keep watching")
        return 'skip', 0.0, 0.0, {}
    return 'enter', st_val, gap, t2_levels

def _clean_watched_strikes():
    """Remove watched strikes from previous days (called at session start).

    Also prunes previous days' shadow confirmation arms — they share this state file
    and would otherwise accumulate without bound.
    """
    state = _load(_state_path())
    ws = state.get('watched_strikes', {})
    now = datetime.datetime.now(IST)
    today = now.date()

    before_arms = len(state.get(_ARMS_KEY, {}))
    _prune_arms(state, now.strftime('%Y%m%d'))
    arms_pruned = before_arms - len(state.get(_ARMS_KEY, {}))

    to_remove = [k for k, v in ws.items() if _watch_is_stale(v, today)] if ws else []
    for key in to_remove:
        ws.pop(key, None)

    # Expired dedup keys. seen_/watch_/entry_ keys are date-stamped and read only via
    # _dedup, which already treats anything older than today as absent — but nothing
    # ever deleted them, so the file grew by one key per distinct strike forever (it
    # still carried seen_*_30minute_* keys from the TF retired in May 2026). _dedup()
    # returning True for a key that IS present means exactly "stale or unparseable",
    # which is the removal condition.
    # 'touch' covers BOTH magnet-touch keys — touch_<...> (the alert) and
    # touch_warn_<...> (the fail-open warning) — since the family is the first
    # underscore-delimited token.
    dedup_stale = [k for k in list(state)
                   if k.split('_', 1)[0] in ('seen', 'watch', 'entry', 't1dist', 'touch', 'liq', 'evt', 'evtfc')
                   and _dedup(k, state)]
    # kite_failures_<YYYYMMDD> hold plain counts (not dedup dicts): drop previous days.
    dedup_stale += [k for k in list(state) if k.startswith('kite_failures_')
                    and k[len('kite_failures_'):] < now.strftime('%Y%m%d')]
    for k in dedup_stale:
        state.pop(k, None)

    if to_remove or arms_pruned or dedup_stale:
        _save(state, _state_path())
        if to_remove:
            log.info(f"Cleaned {len(to_remove)} old watched strikes")
        if arms_pruned:
            log.info(f"Cleaned {arms_pruned} old confirmation arms")
        if dedup_stale:
            log.info(f"Cleaned {len(dedup_stale)} expired dedup keys")

def _watch_is_stale(info, today):
    try:
        added = datetime.datetime.fromisoformat(info.get('added', ''))
        if added.tzinfo is None:
            added = IST.localize(added)
        return added.date() < today
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Alert Formatting
# ---------------------------------------------------------------------------

def _fmt_table(ce_levels, pe_levels, ce_ltp, pe_ltp):
    html = "<pre>"
    html += "     CE                    PE\n"
    html += " TF  Dir    ST    LTP Gap  Dir    ST    LTP Gap\n"
    html += "─────────────────────────────────────────────────\n"
    for tf in ALL_TIMEFRAMES:
        lbl = TF_DISPLAY[tf]
        parts = []
        for levels, ltp in [(ce_levels, ce_ltp), (pe_levels, pe_ltp)]:
            d = levels.get(tf, {})
            if d.get('direction') == 'INSUFFICIENT' or d.get('value',0) <= 0:
                parts.append("  --    N/A       ")
            else:
                sv = d['value']; cl = ltp if ltp > 0 else d['close']
                g = ((cl - sv)/sv)*100 if sv > 0 else 0
                icon = "\U0001f7e2" if d['direction']=='UP' else "\U0001f534"
                parts.append(f"{icon} {sv:>6.1f} {cl:>6.1f}{g:>+5.1f}%")
        html += f"{lbl:>3} {parts[0]} {parts[1]}\n"
    html += "─────────────────────────────────────────────────\n</pre>"
    return html

def _fmt_entry(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp, st_val, gap, spot, t2_value=0, t2_tf=''):
    lbl = TF_DISPLAY[tf]; exp_str = expiry.strftime('%d-%b')
    sl = opt_ltp * (1 - PREMIUM_SL_PCT/100)
    html = f"<b>\U0001f7e2 ENTRY | {U.name} {opt_type} {strike} {lbl}</b>\n"
    html += f"Spot: {spot:,.2f} | Expiry: {exp_str} ({dte}d)\n"
    html += f"Option: <code>{opt_sym}</code>\n\n"
    html += f"Entry: <b>{opt_ltp:.2f}</b> | ST: {st_val:.2f} | Gap: {gap:+.1f}%\n"
    html += f"T1: premium touches ST ({st_val:.2f})\n"
    if t2_value > 0:
        html += f"T2: {TF_DISPLAY.get(t2_tf, t2_tf)} ST ({t2_value:.2f})\n"
    html += f"Premium SL: {sl:.2f} (-{PREMIUM_SL_PCT}%)\n"
    html += f"Cost SL: activates at gap &lt;= {COST_SL_GAP_PCT}%"
    return html

def _fmt_watching(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp, st_val, gap, spot):
    lbl = TF_DISPLAY[tf]; exp_str = expiry.strftime('%d-%b')
    html = f"<b>\U0001f7e1 WATCHING | {U.name} {opt_type} {strike} {lbl}</b>\n"
    html += f"Spot: {spot:,.2f} | {opt_type}: {opt_ltp:.2f} | ST: {st_val:.2f} | Gap: {gap:+.1f}%\n"
    html += f"<code>{opt_sym}</code> ({exp_str}, {dte}d)\n"
    html += f"Entry zone: {MIN_REWARD_PCT}% &lt;= gap &lt;= {ENTRY_GAP_PCT}%"
    return html

def _fmt_exit(pos, exit_prem, exit_type):
    entry = pos.get('entry_premium',0); pnl = exit_prem - entry
    pnl_pct = (pnl/entry*100) if entry > 0 else 0
    # TIME_SL and ADV_SL were added as exit layers (2026-04 / 2026-05-12) but never
    # added here, so every one of those alerts rendered as "? TIME_SL" on Telegram.
    # T1_LOCK and T1_GIVEBACK (2026-09-01) are profit-protection exits, not stop-outs, and
    # must not read as "COST SL" \u2014 13 of the 15 post-05-12 T1-hit COST_SL alerts were in fact
    # the T1 lock booking a profit (+5,908 between them) and were misread as failures.
    icons = {'TP':'\U0001f3af','COST_SL':'\U0001f4b0','TRAIL':'\U0001f4c9',
             'PREM_SL':'\U0001f6d1','EOD':'\U0001f556','STALE':'\u26a0\ufe0f',
             'TIME_SL':'\u23f1\ufe0f','ADV_SL':'\U0001f53b',
             'T1_LOCK':'\U0001f512','T1_GIVEBACK':'\U0001f6e1\ufe0f',
             'EVENT_FLAT':'\U0001f4c5','STALE_LKG':'\u26a0\ufe0f'}
    labels = {'TP':'TARGET','COST_SL':'COST SL','TRAIL':'TRAILING SL',
              'PREM_SL':'PREMIUM SL','EOD':'END OF DAY','STALE':'STALE',
              'TIME_SL':'TIME SL','ADV_SL':'ADVERSE MOVE SL',
              'T1_LOCK':'T1 LOCKED','T1_GIVEBACK':'T1 GIVE-BACK SL',
              'EVENT_FLAT':'EVENT FLATTEN','STALE_LKG':'STALE (LAST GOOD PRICE)'}
    dur = ''
    try:
        mins = max(0,int((datetime.datetime.now(IST)-datetime.datetime.fromisoformat(pos['entry_time'])).total_seconds()/60))
        dur = f" | {mins//60}h{mins%60}m" if mins >= 60 else f" | {mins}m"
    except Exception: pass
    lbl = labels.get(exit_type, exit_type)
    if exit_type == 'TP' and pos.get('t1_hit'):
        lbl = 'T2 TARGET'
    tag = f"{U.name} " if U.file_suffix else ''   # instrument tag; NIFTY text unchanged
    html = f"<b>{icons.get(exit_type,'?')} {lbl} | "
    html += f"{tag}{pos.get('opt_type','?')} {pos.get('strike','?')}{dur}</b>\n"
    html += f"<code>{pos.get('option_symbol','?')}</code>\n"
    html += f"Entry: {entry:.2f} → Exit: {exit_prem:.2f}\n"
    html += f"P&amp;L: {pnl:+.2f} ({pnl_pct:+.0f}%)"
    return html

# ---------------------------------------------------------------------------
# Position Management (Magnet-style)
# ---------------------------------------------------------------------------

# SHADOW (2026-11-05, user idea): "10M takes its target first, then the 15M run-up follows."
# Ledger since Jul: 15M after a 10M target that day n11 +1,380 vs otherwise n25 -10,386 (n small,
# same-side vs any-side not settled). Every entry records whether a 10M position of this
# underlying reached T1 earlier TODAY, so the 15M review at n45 can split by it.
# Record-only: nothing reads these fields; a failure records None.
#   T1 time = `t1_time` (open or closed; stamped by the ladder since 2026-10-27), else the
#   exit_time of a row closed with a target exit. The fallback covers ledger rows from before
#   10-27 (so history can be tagged with this same function) and a TP filled by the exchange
#   order, which _close_pos books as 'TP' without passing the ladder.
#   "Earlier" = as the bot knew it at the entry decision: run_ltp_scan() scans for entries
#   BEFORE monitor_positions(), so a 10M T1 in the same minute is seen one scan later.
SEQ10_TF = '10minute'
SEQ10_TARGET_EXITS = ('TP', 'T1_LOCK', 'T1_GIVEBACK')

def _seq10_tag(positions, opt_type, now):
    """{'seq10_any', 'seq10_same', 'seq10_first_t1'} from today's 10M ledger rows; never raises."""
    try:
        today = now.strftime('%Y%m%d')
        hits = []
        for p in positions.values():
            if not (isinstance(p, dict) and p.get('signal_tf') == SEQ10_TF and p.get('entry_date') == today):
                continue
            t = p.get('t1_time') or (p.get('exit_time') if p.get('status') == 'closed'
                                     and p.get('exit_type') in SEQ10_TARGET_EXITS else None)
            if t and datetime.datetime.fromisoformat(t) <= now:
                hits.append((t, p.get('opt_type')))
        hits.sort()
        return {'seq10_any': bool(hits),
                'seq10_same': any(o == opt_type for _, o in hits),
                'seq10_first_t1': hits[0][0] if hits else None}
    except Exception as e:
        log.warning(f"SEQ10 shadow tag failed (recorded as None): {e}")
        return {'seq10_any': None, 'seq10_same': None, 'seq10_first_t1': None}

def _open_pos(opt_type, tf, strike, expiry, opt_sym, opt_ltp, st_val, gap, spot, t2_value=0, t2_tf='',
              t2_alt_value=0, t2_alt_tf=''):
    positions = _load(_pos_path())
    today = datetime.datetime.now(IST).strftime('%Y%m%d')
    key = f"{U.name}_{opt_type}_{tf}_{today}{_half_suffix()}"
    slot_n = 1
    if U.slot_mode == 'unlimited':
        # One key per entry (spec §12): the per-day key IS the per-day dedup, so unlimited
        # re-entry needs a slot suffix. slot_n counts this side x TF's entries today.
        slot_n = 1 + sum(1 for p in positions.values()
                         if isinstance(p, dict) and p.get('entry_date') == today
                         and p.get('opt_type') == opt_type and p.get('signal_tf') == tf)
        key = f"{key}_{slot_n}"
    if key in positions: return None  # used today (open or closed)
    inst = _get_instrument_info(opt_sym)
    # Friction + analysis fields (review-1 H7, review-2 B12). Kite serves no history for expired
    # options, so bid/ask at entry can never be backfilled: record it now, from the quote the
    # scan already fetched (no extra API call).
    bid, ask = _best_bid_ask(_LAST_QUOTES.get(f"{U.exchange}:{opt_sym}") or {})
    now_dt = datetime.datetime.now(IST)
    positions[key] = {
        'status':'open', 'opt_type':opt_type, 'signal_tf':tf,
        'strike':strike, 'expiry':expiry.isoformat(),
        'option_symbol':opt_sym, 'exchange':U.exchange,
        'entry_premium':opt_ltp, 'entry_spot':spot,
        'st_value':st_val, 'entry_gap':gap,
        't2_value': t2_value, 't2_tf': t2_tf, 't1_hit': False,
        # Counterfactual only — the anchor this TF would have used before 2026-09-01.
        # Never read by the exit ladder; exists so the 60M -> 30M switch can be priced
        # against what it replaced rather than against a fixed-multiple proxy. See T2_ALT.
        't2_alt_value': t2_alt_value, 't2_alt_tf': t2_alt_tf,
        'entry_time':datetime.datetime.now(IST).isoformat(), 'entry_date':today,
        'peak_premium':opt_ltp, 'cost_sl_active':False, 'cost_sl_level':0,
        'lot_size':inst['lot_size'], 'tick_size':inst['tick_size'],
        'underlying': U.name, 'slot_n': slot_n,
        'session_half': _session_half(now_dt),
        'dte': (expiry - now_dt.date()).days,
        'entry_bid': bid, 'entry_ask': ask,
        'spread_pct': round((ask - bid) / opt_ltp * 100, 3) if bid > 0 and ask > 0 and opt_ltp > 0 else None,
        't1_dist_ticks': round((st_val - opt_ltp) / (inst['tick_size'] or U.tick_fallback), 1),
    }
    positions[key].update(_seq10_tag(positions, opt_type, now_dt))   # SHADOW, record-only
    _save(positions, _pos_path())
    log.info(f"Opened: {key} | {opt_sym} @ {opt_ltp:.2f} ST={st_val:.2f} gap={gap:+.1f}%")
    _sq = positions[key]
    log.info(f"SEQ10: {key} | 10M target earlier today: any={_sq['seq10_any']} same-side={_sq['seq10_same']}"
             f"{' first ' + _sq['seq10_first_t1'][11:16] if _sq['seq10_first_t1'] else ''} — shadow")

    # OBSERVER (flight recorder) — bare statement, never raises, never gates. Records the
    # PLAN this trade is being held against, with the config in force stamped per-trade so
    # a later replay cannot apply today's constants to an older trade.
    trade_observer.on_open(key, positions[key], thresholds={
        'PREMIUM_SL_PCT': PREMIUM_SL_PCT, 'TRAIL_PCT': TRAIL_PCT,
        'TRAIL_MIN_GAIN_PCT': TRAIL_MIN_GAIN_PCT, 'T1_BUFFER_PCT': T1_BUFFER_PCT,
        'ADV_SL_PEAK_PCT': ADV_SL_PEAK_PCT, 'ADV_SL_DRIFT_PCT': ADV_SL_DRIFT_PCT,
        'COST_SL_GAP_PCT': COST_SL_GAP_PCT, 'MIN_T1_DISTANCE': MIN_T1_DISTANCE,
        'ENTRY_GAP_PCT': ENTRY_GAP_PCT, 'MIN_REWARD_PCT': MIN_REWARD_PCT,
        'T1_GIVEBACK_KEEP_PCT': T1_GIVEBACK_KEEP_PCT,
        'TIME_SL_CAP': (TF_TIME_SL or {}).get(tf, (TF_TIME_SL or {}).get('default', 120)),
    })

    # Neo bridge: place entry + TP orders
    if TRADE_ENABLED and _NEO_BRIDGE:
        try:
            _NEO_BRIDGE.on_entry(key, positions[key])
        except Exception as e:
            log.error(f"Bridge on_entry failed: {e}")

    return key

def _close_pos(key, pos, exit_prem, exit_type):
    pos['status'] = 'closed'; pos['exit_type'] = exit_type
    pos['exit_premium'] = exit_prem; pos['exit_time'] = datetime.datetime.now(IST).isoformat()
    # Exit friction from the quote the caller just fetched (_quote/_ltp fill _LAST_QUOTES).
    bid, ask = _best_bid_ask(_LAST_QUOTES.get(f"{pos.get('exchange', U.exchange)}:{pos.get('option_symbol', '')}") or {})
    pos['exit_bid'], pos['exit_ask'] = bid, ask
    _entry = pos.get('entry_premium') or 0
    pos['pnl_pct'] = round((exit_prem - _entry) / _entry * 100, 3) if _entry > 0 else None
    msg = _fmt_exit(pos, exit_prem, exit_type)
    try: common.sendMsgTelegram(msg, 'I')
    except Exception as e: log.error(f"Exit alert failed {key}: {e}")
    pnl = exit_prem - pos['entry_premium']
    log.info(f"{exit_type}: {key} | {pos['entry_premium']:.2f}→{exit_prem:.2f} P&L={pnl:+.2f}")

    # Exit forensics — single line per exit so a monthly review can grep "FORENSICS"
    # and re-tune TIME_SL caps, ADV_SL thresholds, T1 lock buffer from data.
    try:
        entry_prem = pos.get('entry_premium', 0) or 0
        peak = pos.get('peak_premium', entry_prem)
        t1 = pos.get('st_value', 0) or 0
        peak_gain_pct = ((peak - entry_prem) / entry_prem * 100) if entry_prem > 0 else 0
        drift_pct = ((exit_prem - entry_prem) / entry_prem * 100) if entry_prem > 0 else 0
        peak_over_t1 = (peak / t1) if t1 > 0 else 0
        entry_dt = datetime.datetime.fromisoformat(pos.get('entry_time', ''))
        if entry_dt.tzinfo is None:
            entry_dt = IST.localize(entry_dt)
        hold_mins = (datetime.datetime.now(IST) - entry_dt).total_seconds() / 60
        log.info(
            f"FORENSICS {exit_type} | {key} tf={pos.get('signal_tf','?')} "
            f"hold={hold_mins:.0f}m peak={peak:.2f} peak_gain={peak_gain_pct:+.1f}% "
            f"drift={drift_pct:+.1f}% T1={t1:.2f} peak/T1={peak_over_t1:.3f} "
            f"t1_hit={pos.get('t1_hit', False)}"
        )
    except Exception as e:
        log.warning(f"FORENSICS calc failed for {key}: {e}")

    # OBSERVER (flight recorder) — bare statement, never raises, never gates. Measures the
    # fill against the level the firing rule NAMED. This is the check that surfaces the
    # Rs6,103 the ledger leaked between one poll and the next; FORENSICS above records
    # peak/drift/hold but never compares the exit price to its own trigger.
    trade_observer.on_exit(key, pos, exit_prem, exit_type)

    # Neo trader: cancel TP + place exit order (pass exit_prem for C1 fix)
    if TRADE_ENABLED and _NEO_BRIDGE:
        try:
            _NEO_BRIDGE.on_exit(key, exit_type, exit_prem)
        except Exception as e:
            log.error(f"Neo on_exit failed: {e}")

    # SHADOW (non-NIFTY only): what longer / no TIME_SL caps and holding through an event would
    # have done. Never raises, never gates.
    _sp_on_real_close(key, pos, exit_type)

#: Floor identities, weakest to strongest. The stop floor under an open position can be armed
#: by three different rules, and before 2026-09-01 all three booked the exit as 'COST_SL'.
#: That conflated a profit-take at T1 with a scratch at entry: of the 15 post-05-12 T1-hit
#: exits tagged COST_SL, 13 were actually the T1 LOCK firing at T1 and were worth +5,908
#: between them, while only 2 were genuine cost-SL scratches (-19). Every review since May has
#: read those 13 as failures, and so did the Telegram alert. The floor now carries its own
#: reason and the exit reports it.
SL_COST = 'COST_SL'            # floor = entry + 0.10 (break-even protection)
SL_T1_GIVEBACK = 'T1_GIVEBACK'  # floor = entry + T1_GIVEBACK_KEEP_PCT% of the entry->T1 gain
SL_T1_LOCK = 'T1_LOCK'         # floor = T1 itself (profit-take, not a stop-out)

def _arm_floor(pos, level, reason):
    """Raise the stop floor under an open position, recording WHICH rule set it.

    Ratchets UP only. A later rule may raise the floor (cost -> give-back -> T1 lock) but no
    rule may ever lower it, so ordering between the callers cannot silently widen a stop that
    is already tight. Returns True if the floor moved, so callers can drive `modified`.
    """
    if level <= 0:
        return False
    if pos.get('cost_sl_active') and level <= pos.get('cost_sl_level', 0):
        return False
    pos['cost_sl_level'] = level
    pos['cost_sl_active'] = True
    pos['sl_reason'] = reason
    return True

def _cost_floor_offset(pos):
    """Break-even floor offset above entry: NIFTY +0.10 Rs (unchanged); others 2 ticks (review-1 M1).

    +0.10 Rs is 2 NIFTY ticks; on GOLDM (tick 0.5) it would be sub-tick, on NATGASMINI 0.67% of the
    premium. Ticks are the unit microstructure noise moves in, so non-NIFTY uses ticks.
    """
    if U.cost_floor_rs is not None:
        return U.cost_floor_rs
    return U.cost_floor_ticks * (pos.get('tick_size') or U.tick_fallback)

def _tg(msg, kind):
    """Ladder Telegram with the per-underlying volume policy (spec §12, user 2026-10-06).

    Non-NIFTY paper instruments send entries and exits only — T1 HIT / COST SL updates would be
    60-150 messages a day across six processes. NIFTY ('full') is unchanged. Never raises.
    """
    if U.telegram_mode == 'entry_exit' and kind in ('t1', 'cost'):
        return
    try:
        common.sendMsgTelegram(msg, 'I')
    except Exception:
        pass

class _Silent:
    """Logger stand-in for shadow positions: the ladder's per-tick lines are for real trades."""
    def info(self, *a, **k): pass
    warning = debug = error = info

_SILENT = _Silent()

def _ladder(key, pos, curr, now, cap='config', flatten=False, say=None, tg=None):
    """One tick of the exit ladder for one position. Mutates pos; returns (exit_type, modified).

    The ONLY copy of the exit rules (2026-10-06): real positions (monitor_positions) and shadow
    positions (_sp_monitor) both run it, so a shadow can never drift from the rules the bot
    trades. Side effects are injected: `say` (logger) and `tg` (telegram(msg, kind)) — real trades
    pass the live ones, shadows pass silent ones, so log/Telegram order for real trades is exactly
    what it was inline.
      cap      'config' = TIME_SL from config_trade.yaml exactly as before; int = minutes;
               None = no time cap (shadow "hold to EOD")
      flatten  False, or a reason string: EVENT_FLAT when no other rule fires this tick
    Peak and the observer tick are handled by the caller BEFORE this runs (observer must see
    the state the ladder saw).
    """
    say = say or _SILENT
    tg = tg or (lambda msg, kind: None)
    modified = False
    entry_prem = pos.get('entry_premium', 0)
    opt_type = pos.get('opt_type', '')
    signal_tf = pos.get('signal_tf', '')

    # Use the ST value from ENTRY time (not current ATM which may have shifted)
    st_val = pos.get('st_value', 0)

    exit_type = None
    t2_val = pos.get('t2_value', 0)
    t1_hit = pos.get('t1_hit', False)
    floor_off = _cost_floor_offset(pos)

    # T1 profit lock: after T1 hit, once peak breaks T1+buffer, raise SL from cost to T1.
    # Locks in T1 gain on retracement while still riding for T2.
    if t1_hit and st_val > 0:
        peak = pos.get('peak_premium', entry_prem)
        if peak >= st_val * (1 + T1_BUFFER_PCT / 100):
            prev = pos.get('cost_sl_level', 0)
            if _arm_floor(pos, st_val, SL_T1_LOCK):
                modified = True
                say.info(f"{key}: T1 LOCK — peak {peak:.2f} >= T1+{T1_BUFFER_PCT}%, SL raised {prev:.2f} → T1 {st_val:.2f}")

    # Layer 1: TP — T1 (signal TF ST) and optional T2 (next higher TF ST)
    # The t1_hit guard is deliberately dropped here (2026-09-01). It used to mean that a
    # scan finding the premium ALREADY past T2 — because a single 1-minute poll spanned
    # the whole T1->T2 move — only marked t1_hit and then waited a further minute before
    # booking, holding through the top of a spike, which is exactly where a reversal is
    # most likely. Rare while T2 sat at the 60M ST (median +46% beyond T1, never once
    # reached in 17 trades); realistic now that 15M anchors on 30M, so it is closed here
    # rather than left as a hazard this change introduces. `curr >= t2_val > 0` implies
    # the premium crossed T1 on the way, since entry gating guarantees T2 > T1 > entry.
    if t2_val > 0 and curr >= t2_val:
        # T2 hit — full exit
        exit_type = 'TP'
        pos['t1_hit'] = True      # for _fmt_exit's 'T2 TARGET' label and the ledger
        pos.setdefault('t1_time', now.isoformat())   # minutes-to-T1 (winners-come-fast study)
        modified = True
        say.info(f"{key}: T2 hit — premium {curr:.2f} >= T2 {t2_val:.2f}"
                 f"{'' if t1_hit else ' (same scan as T1 — single-poll gap)'}")
    elif st_val > 0 and curr >= st_val and not t1_hit:
        pos.setdefault('t1_time', now.isoformat())
        if t2_val > 0:
            # T1 hit with T2 pending — move SL to cost, hold for T2
            pos['t1_hit'] = True
            # The floor goes to entry + T1_GIVEBACK_KEEP_PCT% of the entry->T1 move, NOT
            # to entry+0.10. See T1_GIVEBACK_KEEP_PCT: the old break-even floor meant a
            # position that touched its target and stalled kept NONE of the gain unless it
            # separately cleared T1+2% (lock) or +8% (trail), and a peak landing between
            # those two bars gave everything back. max() against the cost floor keeps this
            # safe if T1 <= entry — entry gating makes that impossible, but nothing here
            # asserts it, and a negative keep-fraction would be a silent stop widening.
            floor = max(entry_prem + floor_off,
                        entry_prem + (st_val - entry_prem) * (T1_GIVEBACK_KEEP_PCT / 100))
            _arm_floor(pos, floor, SL_T1_GIVEBACK)
            modified = True
            t2_tf = pos.get('t2_tf', '')
            say.info(f"{key}: T1 hit {curr:.2f} >= ST {st_val:.2f}, floor -> {pos['cost_sl_level']:.2f} "
                     f"(keeps {T1_GIVEBACK_KEEP_PCT:.0f}% of T1 gain), trailing for T2 {t2_val:.2f}")
            t1_msg = f"<b>\U0001f3af T1 HIT | {opt_type} {pos.get('strike','?')} {TF_DISPLAY.get(signal_tf,'')}</b>\n"
            t1_msg += f"Prem: {curr:.2f} touched ST {st_val:.2f}\n"
            t1_msg += (f"SL \u2192 {pos['cost_sl_level']:.2f} "
                       f"(keeps {T1_GIVEBACK_KEEP_PCT:.0f}% of T1 gain)\n")
            t1_msg += f"Trailing for T2: {t2_val:.2f} ({TF_DISPLAY.get(t2_tf,'')} ST)"
            tg(t1_msg, 't1')
        else:
            # T1 only — normal TP exit
            exit_type = 'TP'
            say.info(f"{key}: premium {curr:.2f} >= ST {st_val:.2f} \u2192 TP")

    # Layer 1.5: Time-based SL (from backtest — closes if held too long)
    if not exit_type and cap == 'config' and TF_TIME_SL and signal_tf:
        try:
            entry_dt = datetime.datetime.fromisoformat(pos.get('entry_time', ''))
            if entry_dt.tzinfo is None:
                entry_dt = IST.localize(entry_dt)
            elapsed_mins = (now - entry_dt).total_seconds() / 60
            max_hold = TF_TIME_SL.get(signal_tf, TF_TIME_SL.get('default', 120))
            if elapsed_mins > max_hold:
                exit_type = 'TIME_SL'
                say.info(f"{key}: TIME_SL — held {elapsed_mins:.0f}min > {max_hold}min limit")
        except Exception as e:
            # Safer to close than hold forever with broken timestamp
            exit_type = 'TIME_SL'
            say.warning(f"{key}: TIME_SL parse error ({e}) — closing as safety fallback")
    elif not exit_type and isinstance(cap, (int, float)) and not isinstance(cap, bool):
        try:
            entry_dt = datetime.datetime.fromisoformat(pos.get('entry_time', ''))
            if entry_dt.tzinfo is None:
                entry_dt = IST.localize(entry_dt)
            if (now - entry_dt).total_seconds() / 60 > cap:
                exit_type = 'TIME_SL'
        except Exception:
            exit_type = 'TIME_SL'

    # Layer 1.6: Adverse-move SL — peak never broke threshold AND drift below threshold
    # Catches "never went up" bleeders before the TIME_SL cap drains them further.
    if not exit_type and entry_prem > 0:
        peak = pos.get('peak_premium', entry_prem)
        peak_gain_pct = ((peak - entry_prem) / entry_prem) * 100
        drift_pct = ((curr - entry_prem) / entry_prem) * 100
        if peak_gain_pct < ADV_SL_PEAK_PCT and drift_pct <= -ADV_SL_DRIFT_PCT:
            exit_type = 'ADV_SL'
            say.info(f"{key}: ADV_SL — peak +{peak_gain_pct:.1f}% < {ADV_SL_PEAK_PCT}%, drift {drift_pct:+.1f}% <= -{ADV_SL_DRIFT_PCT}%")

    # Layer 2: Cost SL activation (premium within COST_SL_GAP_PCT of ST)
    if not exit_type and st_val > 0 and not pos.get('cost_sl_active'):
        gap_to_st = ((st_val - curr) / st_val) * 100 if st_val > 0 else 99
        if gap_to_st <= COST_SL_GAP_PCT:
            _arm_floor(pos, entry_prem + floor_off, SL_COST)
            modified = True
            say.info(f"{key}: Cost SL active — gap to ST={gap_to_st:.1f}% floor={pos['cost_sl_level']:.2f}")
            try:
                msg = f"<b>\U0001f4b0 COST SL | {opt_type} {pos.get('strike','?')}</b>\n"
                msg += f"Premium {curr:.2f} within {gap_to_st:.1f}% of ST | Floor: {pos['cost_sl_level']:.2f}"
                tg(msg, 'cost')
            except Exception: pass

    # Layer 3: Cost SL hit
    # Layer 3: floor hit. The exit reports WHICH floor fired (see SL_* above) so a
    # profit-take at T1 is no longer indistinguishable from a scratch at break-even.
    # Positions opened before 2026-09-01 carry no 'sl_reason', so default to COST_SL —
    # that is what they were, and it keeps the ledger's history readable unchanged.
    if not exit_type and pos.get('cost_sl_active') and curr <= pos.get('cost_sl_level',0):
        exit_type = pos.get('sl_reason', SL_COST)

    # Layer 4: Trailing SL — fires after peak gain >= TRAIL_MIN_GAIN_PCT (8%)
    peak = pos.get('peak_premium', entry_prem)
    gain_pct = ((peak - entry_prem)/entry_prem*100) if entry_prem > 0 else 0
    if not exit_type and gain_pct >= TRAIL_MIN_GAIN_PCT:
        trail = entry_prem + (peak - entry_prem) * (TRAIL_PCT/100)
        if pos.get('cost_sl_active') and pos.get('cost_sl_level',0) > trail:
            trail = pos['cost_sl_level']
        if curr <= trail:
            exit_type = 'TRAIL'
            say.info(f"{key}: Trail hit — {curr:.2f} <= {trail:.2f} (peak={peak:.2f})")

    # Layer 5: Premium SL
    prem_sl = entry_prem * (1 - PREMIUM_SL_PCT/100)
    if not exit_type and curr <= prem_sl:
        exit_type = 'PREM_SL'

    # Layer 6: scheduled-event flatten (spec §3/§12): a STATE (T-5 .. T+30), so a skipped cron
    # minute cannot let a position hold through the release. Never fires for NIFTY.
    if not exit_type and flatten:
        exit_type = 'EVENT_FLAT'
        say.info(f"{key}: EVENT_FLAT — {flatten}")

    return exit_type, modified

def monitor_positions():
    """Magnet-style multi-layer exits. TP = option premium touches its ST from below."""
    positions = _load(_pos_path())
    modified = False
    flatten = _event_flatten_reason(datetime.datetime.now(IST))
    for key, pos in positions.items():
        if pos.get('status') != 'open': continue
        entry_prem = pos.get('entry_premium',0)
        opt_sym = pos.get('option_symbol','')
        signal_tf = pos.get('signal_tf','')
        if not opt_sym or entry_prem <= 0: continue

        # Current option LTP
        depth = _quote(f"{U.exchange}:{opt_sym}")
        curr = depth['ltp']
        if curr <= 0: curr = depth.get('bid',0)
        if curr <= 0: continue

        # Update peak
        if curr > pos.get('peak_premium',0):
            pos['peak_premium'] = curr; modified = True

        # OBSERVER (flight recorder) — bare statement, never raises, never gates. Runs
        # BEFORE the exit ladder so the tick that triggers an exit is recorded with the
        # state the ladder actually saw, not the state it left behind.
        trade_observer.on_tick(key, pos, curr, time_sl_cap=(
            (TF_TIME_SL or {}).get(signal_tf, (TF_TIME_SL or {}).get('default', 120))
            if TF_TIME_SL and signal_tf else None),
            day_high=depth.get('day_high', 0))

        exit_type, mod = _ladder(key, pos, curr, datetime.datetime.now(IST), cap='config',
                                 flatten=flatten, say=log, tg=_tg)
        modified = modified or mod

        if exit_type:
            _close_pos(key, pos, curr, exit_type); modified = True
        else:
            # Last known good premium (review-1 M13): what _close_stale books if this position
            # is ever stranded past EOD, instead of the next session's opening price.
            pos['last_ltp'] = curr
            pos['last_ltp_time'] = datetime.datetime.now(IST).isoformat()
            modified = True
            pnl_pct = ((curr-entry_prem)/entry_prem*100) if entry_prem > 0 else 0
            cost = " [COST SL]" if pos.get('cost_sl_active') else ""
            t2_info = f" [T2={pos.get('t2_value',0):.0f}]" if pos.get('t1_hit') else ""
            log.debug(f"{key}: {curr:.2f} ({pnl_pct:+.0f}%){cost}{t2_info}")

    if modified: _save(positions, _pos_path())
    _sp_monitor()

def _close_stale():
    positions = _load(_pos_path())
    today = datetime.datetime.now(IST).strftime('%Y%m%d')
    candidates = [k for k, p in positions.items() if p.get('status') == 'open' and p.get('entry_date', '') < today]
    stale = []
    for k in candidates:
        pos = positions[k]
        # Last-known-good premium first (review-1 M13): the price the position was last monitored
        # at, i.e. at or just before the EOD close that failed. The next session's opening quote
        # (below) carries an overnight gap that never belonged to this trade — large for crude/NG.
        lkg = pos.get('last_ltp') or 0
        try:
            lkg_expired = datetime.date.fromisoformat(pos.get('expiry', '')) < datetime.datetime.now(IST).date()
        except Exception:
            lkg_expired = False
        if lkg > 0 and not lkg_expired:   # past expiry the settlement logic below applies (R3-14)
            log.warning(f"Stale: {k} — booking last-known-good premium {lkg:.2f} "
                        f"from {pos.get('last_ltp_time', '?')} (STALE_LKG)")
            _close_pos(k, pos, lkg, 'STALE_LKG')
            stale.append(k)
            continue
        # Fetch last available premium before closing
        prem = 0
        try:
            depth = _quote(f"{U.exchange}:{pos.get('option_symbol', '')}")
            prem = max(depth['ltp'], depth.get('bid', 0), 0)
        except Exception:
            pass
        if prem <= 0:
            # A zero here means one of two very different things, and recording the
            # wrong one corrupts the ledger the reviews are tuned on:
            #   expiry passed  -> the contract really did settle worthless; 0 is the
            #                     true exit and must be booked.
            #   still tradeable -> the quote call failed. Booking 0 would fabricate a
            #                     -100% loss, so leave the position open and retry on
            #                     the next run instead.
            expired = False
            expiry_ok = True
            try:
                expired = datetime.date.fromisoformat(pos.get('expiry', '')) < datetime.datetime.now(IST).date()
            except Exception:
                expired = False       # unparseable expiry — assume live, do not book 0
                expiry_ok = False
            # Backstop: with a missing or corrupt expiry the check above can never turn true,
            # which would leave the position open forever, re-quoted every scan. Max weekly
            # DTE is 8, so a position held MAX_STALE_DAYS is certainly past expiry and 0 is
            # then the correct exit, not a fabricated one. Only when the expiry is UNPARSEABLE:
            # a monthly contract (BANKNIFTY/MCX) is legitimately alive 10+ days after entry, so
            # a valid future expiry must never be overridden by age (spec F16).
            if not expired and not expiry_ok:
                try:
                    age = (datetime.datetime.now(IST).date()
                           - datetime.datetime.strptime(pos.get('entry_date', ''), '%Y%m%d').date()).days
                except Exception:
                    age = 0
                if age >= MAX_STALE_DAYS:
                    log.error(f"Stale: {k} — unpriceable and {age} days old "
                              f"(expiry={pos.get('expiry','?')!r}); force-closing at 0")
                    expired = True
            if not expired:
                log.error(f"Stale: {k} — no quote for {pos.get('option_symbol','?')} and "
                          f"contract not expired; leaving OPEN for retry rather than "
                          f"booking a false 0 exit")
                continue
            log.warning(f"Stale: {k} — contract expired, booking 0 exit")
        # Route through _close_pos so an exchange position is actually squared off:
        # this used to close the record inline, which left the Neo bridge holding a
        # live position and a resting TP order (neo/orders.py:on_exit already lists
        # 'STALE' as an expected exit_type), and skipped the FORENSICS line every
        # review greps for.
        _close_pos(k, pos, prem, 'STALE')
        stale.append(k)
    if stale:
        _save(positions, _pos_path())
        try:
            common.sendMsgTelegram(f"<b>\u26a0\ufe0f STALE</b> {(U.name + ' ') if U.file_suffix else ''}"
                                   f"{len(stale)} position(s) closed from previous day", 'I')
        except Exception:
            pass

def _close_eod():
    positions = _load(_pos_path())
    modified = False
    for key, pos in positions.items():
        if pos.get('status') != 'open': continue
        depth = _quote(f"{U.exchange}:{pos['option_symbol']}")
        prem = max(depth['ltp'], depth.get('bid',0), 0)
        if prem <= 0:
            # Quote failed. The contract is live (MIN_DTE >= 1 at entry), so 0 is never
            # the real price — booking it would write a fabricated -100% into the
            # ledger. main() re-runs every minute until 15:30, so retry; anything still
            # open after that is picked up by _close_stale on the next session.
            log.error(f"EOD: no quote for {pos['option_symbol']} ({key}) — "
                      f"leaving OPEN for retry rather than booking a false 0 exit")
            continue
        _close_pos(key, pos, prem, 'EOD'); modified = True
    if modified: _save(positions, _pos_path())
    _sp_close_all('EOD')


# ---------------------------------------------------------------------------
# Per-underlying session, gates, slots and shadow positions (spec §11-§13, 2026-10-06)
# ---------------------------------------------------------------------------

def _session_today(day):
    """(open, close) for this underlying today from market_holidays.yaml; None = closed.

    A broken/missing calendar falls back to the normal session (logged): the liquidity
    freshness gate is the backstop, and an unreadable YAML must not stop a normal trading day.
    """
    try:
        return market_calendar.session(U, day, market_calendar.load(CURRENT_DIR))
    except Exception as e:
        log.error(f"Holiday calendar unusable ({e}) — assuming a normal {U.name} session")
        return (U.session_open, U.session_close(day))

def _apply_session(sess, day):
    """This run's signal start / EOD from today's session (half days move both)."""
    global SIGNAL_START_TIME, EOD_CLOSE_TIME
    open_t, close_t = sess
    SIGNAL_START_TIME = (datetime.datetime.combine(day, open_t) + datetime.timedelta(minutes=15)).time()
    EOD_CLOSE_TIME = (datetime.datetime.combine(day, close_t)
                      - datetime.timedelta(minutes=U.eod_offset_min)).time()

_EVENT_WINDOWS = []

def _load_event_windows(now):
    """Today's event windows for this underlying, once per run. Unreadable calendar -> FAIL-CLOSED."""
    global _EVENT_WINDOWS
    _EVENT_WINDOWS = []
    if not U.events:
        return
    try:
        _EVENT_WINDOWS = event_calendar.windows_for(U.name, now.date(), CURRENT_DIR)
    except Exception as e:
        _EVENT_WINDOWS = event_calendar.fail_closed_windows(U.name, now.date())
        log.error(f"EVENT CALENDAR unusable ({e}) — FAIL-CLOSED: {len(_EVENT_WINDOWS)} window(s)")
    if _EVENT_WINDOWS:
        log.info("EVENT WINDOWS: " + "; ".join(
            f"{w.get('kind')} {w.get('event') or ''} "
            f"{w['T'].strftime('%H:%M') if w.get('T') else '19:00->close'}"
            f"{' ' + ','.join(w.get('exception_ids') or []) if w.get('exception_ids') else ''}"
            for w in _EVENT_WINDOWS))
    # A fail_closed window blocks the whole evening (no verified calendar for an event day, an
    # UNVERIFIED scrape, or an unreadable config). Correct, but invisible until 19:00 unless
    # said up front: WARNING once per day (review-3 R3-13).
    if any(w.get('kind') == 'fail_closed' for w in _EVENT_WINDOWS):
        try:
            state = _load(_state_path())
            if _dedup(f"evtfc_{U.name}", state):
                state[f"evtfc_{U.name}"] = {'t': now.isoformat()}
                _save(state, _state_path())
                log.warning(f"EVENT FAIL-CLOSED today ({U.name}): no entries 19:00->close, flatten from 18:55 — "
                            "run `event_calendar.py build` / check events_override.yaml")
        except Exception:
            pass

def _event_flatten_reason(now):
    """False, or the reason open positions must be flattened now (a state predicate)."""
    if not _EVENT_WINDOWS:
        return False
    flat, reason, ids = event_calendar.must_flatten(_EVENT_WINDOWS, now.replace(tzinfo=None))
    return f"{reason}{' ' + ','.join(ids) if ids else ''}" if flat else False

def _best_bid_ask(q):
    depth = (q or {}).get('depth') or {}
    try:
        bid = (depth.get('buy') or [{}])[0].get('price', 0) or 0
        ask = (depth.get('sell') or [{}])[0].get('price', 0) or 0
    except Exception:
        bid = ask = 0
    return float(bid), float(ask)

def _liquidity_problem(sym, now):
    """None if the option is tradeable now, else the reason (spec §4b, §9 (c)).

    Freshness (every underlying incl. NIFTY): the option's own last_trade_time within
    U.fresh_min minutes — the direct test that its candles are fed by trades (Kite emits
    zero-volume MCX minute candles, so candle presence proves nothing). Spread and two-sided
    quote: non-NIFTY only (U.max_spread_pct is None for NIFTY = metrics only).
    A missing quote/timestamp blocks a strict (non-NIFTY) underlying and passes NIFTY.
    """
    strict = U.max_spread_pct is not None
    q = _LAST_QUOTES.get(f"{U.exchange}:{sym}")
    if not q:
        return 'no quote' if strict else None
    ltt = q.get('last_trade_time')
    if hasattr(ltt, 'year'):
        if getattr(ltt, 'tzinfo', None) is not None:
            ltt = ltt.astimezone(IST).replace(tzinfo=None)
        age = (now.replace(tzinfo=None) - ltt).total_seconds() / 60
        if age > U.fresh_min:
            return f"stale: last trade {age:.0f} min ago (> {U.fresh_min})"
    elif strict:
        return 'no last_trade_time'
    if strict:
        bid, ask = _best_bid_ask(q)
        ltp = q.get('last_price') or 0
        if bid <= 0 or ask <= 0:
            return 'one-sided quote'
        spread = (ask - bid) / ltp * 100 if ltp > 0 else 999
        if spread > U.max_spread_pct:
            return f"spread {spread:.2f}% > {U.max_spread_pct}%"
    return None

def _probe_leg(role, key):
    q = _LAST_QUOTES.get(key) or {}
    bid, ask = _best_bid_ask(q)
    ltp = q.get('last_price') or 0
    depth = q.get('depth') or {}
    return {'role': role, 'key': key, 'ltp': ltp, 'bid': bid, 'ask': ask,
            'spread_pct': round((ask - bid) / ltp * 100, 3) if bid > 0 and ask > 0 and ltp > 0 else None,
            'buy': [[d.get('price'), d.get('quantity')] for d in (depth.get('buy') or [])[:5]],
            'sell': [[d.get('price'), d.get('quantity')] for d in (depth.get('sell') or [])[:5]],
            'volume': q.get('volume'), 'oi': q.get('oi'), 'ltt': str(q.get('last_trade_time'))}

def _liquidity_probe(ce_sym, pe_sym, ce_strike, pe_strike, expiry, spot):
    """One JSON line per minute: signal-chain ATM pair + the full-size companion chain at the
    same strikes, with top-5 depth (spec §1/§4, review-3 R3-4). This is the dataset the mini-vs-
    full sizing decision is made from; Kite has no history for expired options, so it can only
    be collected live. Never raises, never gates. One extra quote call for the companion.
    """
    try:
        legs = [_probe_leg('ce', f"{U.exchange}:{ce_sym}"), _probe_leg('pe', f"{U.exchange}:{pe_sym}")]
        if U.quote_companion:
            df = get_instruments()
            comp = df[(df['name'] == U.quote_companion) & (df['exchange'] == U.exchange) &
                      (df['instrument_type'].isin(['CE', 'PE'])) & (df['expiry'].dt.date >= expiry)]
            keys = {}
            if not comp.empty:
                exp_c = comp['expiry'].min()
                comp = comp[comp['expiry'] == exp_c]
                for role, strike, typ in (('comp_ce', ce_strike, 'CE'), ('comp_pe', pe_strike, 'PE')):
                    m = comp[(comp['instrument_type'] == typ) & ((comp['strike'] - float(strike)).abs() < 1e-6)]
                    if not m.empty:
                        keys[role] = f"{U.exchange}:{m['tradingsymbol'].iloc[0]}"
            if keys:
                _ltp(list(keys.values()))
            legs += [_probe_leg(role, k) for role, k in keys.items()]
        rec = {'t': datetime.datetime.now(IST).isoformat(), 'underlying': U.name, 'ref': spot, 'legs': legs}
        path = os.path.join(CURRENT_DIR, 'logs',
                            f"liquidity_probe{U.file_suffix}_{datetime.datetime.now(IST).strftime('%Y-%m-%d')}.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, default=str) + '\n')
    except Exception as e:
        log.debug(f"liquidity probe skipped: {e}")

def _gate_line(state, dk, line, now_iso):
    """INFO once per setup per day, then DEBUG (same pattern as MIN T1 DIST)."""
    if _dedup(dk, state):
        state[dk] = {'t': now_iso}
        log.info(line)
    else:
        log.debug(line)

def _entry_gate_block(site, opt_type, tf, strike, sym, ltp, st_val, gap, t2_value, t2_tf, state, now_iso):
    """Event + liquidity gates for one would-be entry. True = blocked (shadow-recorded).

    Ordered after NO_ENTRY / EOD runway / turbulence and before the dedup + MAGNET TOUCH, and
    consumes no dedup (review-2 B10): a window or a thin minute says "not now", not "dead today".
    """
    try:
        return _entry_gate_check(site, opt_type, tf, strike, sym, ltp, st_val, gap, t2_value, t2_tf,
                                 state, now_iso)
    except Exception as e:
        # check_and_enter runs BEFORE monitor_positions: an escaping error would skip position
        # monitoring for the minute (review-3 R3-5). Strict underlyings fail CLOSED (no entry);
        # NIFTY fails open (its pre-build behaviour had no such gate).
        strict = U.max_spread_pct is not None
        log.error(f"ENTRY GATE error ({e}) — {'entry blocked' if strict else 'gate skipped'}")
        return strict

def _entry_gate_check(site, opt_type, tf, strike, sym, ltp, st_val, gap, t2_value, t2_tf, state, now_iso):
    now = datetime.datetime.now(IST)
    if _EVENT_WINDOWS:
        cap = (TF_TIME_SL or {}).get(tf, (TF_TIME_SL or {}).get('default', 45))
        blocked, reason, ids = event_calendar.entry_blocked(_EVENT_WINDOWS, now.replace(tzinfo=None), cap)
        if blocked:
            _gate_line(state, f"evt_{opt_type}_{tf}_{strike}",
                       f"EVENT_BLOCK: {site}{opt_type} {TF_DISPLAY.get(tf, tf)} {strike} gap={gap:.1f}% — "
                       f"{reason}{' [' + ','.join(ids) + ']' if ids else ''}", now_iso)
            _sp_spawn_blocked('event_block', opt_type, tf, strike, sym, ltp, st_val, t2_value, t2_tf, reason)
            return True
    why = _liquidity_problem(sym, now)
    if why:
        _gate_line(state, f"liq_{opt_type}_{tf}_{strike}",
                   f"LIQ_BLOCK: {site}{opt_type} {TF_DISPLAY.get(tf, tf)} {strike} gap={gap:.1f}% — {why}", now_iso)
        _sp_spawn_blocked('liq_block', opt_type, tf, strike, sym, ltp, st_val, t2_value, t2_tf, why)
        return True
    return False

def _side_conflict(positions, opt_type, tf, today_str):
    """Unlimited-slot exclusion as scans (review-2 B4): same side open on any TF, or the
    opposite side open on this TF. Mirrors the per-day key checks without relying on keys."""
    opp = 'PE' if opt_type == 'CE' else 'CE'
    for p in positions.values():
        if not isinstance(p, dict) or p.get('status') != 'open' or p.get('entry_date') != today_str:
            continue
        if p.get('opt_type') == opt_type:
            return f"same type open ({p.get('signal_tf')})"
        if p.get('opt_type') == opp and p.get('signal_tf') == tf:
            return f"opposite {opp} open"
    return None

# --- Shadow positions (non-NIFTY only; spec Q-B2, §3 EVENT_FLAT hold, §4b LIQ_BLOCK) ----------
# A shadow is a paper-of-paper position that runs the SAME _ladder as real trades but is never
# traded, never messaged and never written to the real ledger (review-1 H6). Kinds:
#   cap          a real trade continued under a longer / no TIME_SL cap. Spawned only when the
#                real exit was TIME_SL or EVENT_FLAT — for any other exit every cap >= the real
#                cap ends identically, so those caps are archived immediately as same_as_real.
#   event_hold   a real trade flattened by EVENT_FLAT, held through the release under its real cap.
#   event_block  / liq_block   the entry a gate refused, run as if taken (real cap; liq_block
#                still honours event flattening). At most one open per (kind, side).
# Open shadows live in shadow_positions<suffix>.json; closed ones are appended to
# logs/shadow_positions<suffix>.jsonl. All are force-closed at EOD, so the open set is bounded
# by one day. One batched quote per process-minute prices every open shadow (review-2 B5).
SHADOW_CAPS = underlyings.SHADOW_TIME_SL_CAPS

def _sp_path():
    return os.path.join(CURRENT_DIR, f'shadow_positions{U.file_suffix}.json')

def _sp_archive_path():
    return os.path.join(CURRENT_DIR, 'logs', f'shadow_positions{U.file_suffix}.jsonl')

def _sp_safe(fn):
    """Shadow code must NEVER affect trading: swallow and log any error."""
    def wrapper(*a, **k):
        if not U.shadow_caps:
            return None
        try:
            return fn(*a, **k)
        except Exception as e:
            log.warning(f"SHADOW {fn.__name__} failed (ignored, trading unaffected): {e}")
            return None
    wrapper.__name__ = fn.__name__
    return wrapper

def _sp_archive(recs):
    if not recs:
        return
    os.makedirs(os.path.dirname(_sp_archive_path()), exist_ok=True)
    with open(_sp_archive_path(), 'a', encoding='utf-8') as f:
        for r in recs:
            f.write(json.dumps(r, cls=_Enc) + '\n')

_SP_FIELDS = ('entry_bid', 'entry_ask', 'spread_pct', 'block_reason',
              'opt_type', 'signal_tf', 'strike', 'expiry', 'option_symbol', 'exchange', 'entry_premium',
              'st_value', 't2_value', 't2_tf', 't1_hit', 't1_time', 'entry_time', 'entry_date',
              'peak_premium', 'cost_sl_active', 'cost_sl_level', 'sl_reason', 'lot_size', 'tick_size',
              'slot_n', 'session_half')

def _sp_from(pos, kind, cap, real_key):
    sp = {k: pos.get(k) for k in _SP_FIELDS if k in pos}
    sp.update(kind=kind, cap_min=cap, real_key=real_key, underlying=U.name, status='open',
              shadow_start=datetime.datetime.now(IST).isoformat())
    return sp

@_sp_safe
def _sp_on_real_close(key, pos, exit_type):
    caps = [c for c in SHADOW_CAPS]
    if exit_type not in ('TIME_SL', 'EVENT_FLAT'):
        # Every cap >= the real cap would have exited identically: archive, no live shadow.
        recs = []
        for c in caps:
            r = _sp_from(pos, 'cap', c, key)
            r.update(status='closed', exit_type=pos.get('exit_type'), exit_premium=pos.get('exit_premium'),
                     exit_time=pos.get('exit_time'), same_as_real=True)
            recs.append(r)
        _sp_archive(recs)
        return
    store = _load(_sp_path())
    # After EVENT_FLAT a longer-cap continuation is "longer cap AND held through the release":
    # its own kind, so cap analysis is not contaminated on event days (review-3 R3-7).
    cap_kind = 'cap_after_event' if exit_type == 'EVENT_FLAT' else 'cap'
    for c in caps:
        store[f"{key}|{cap_kind}{c}"] = _sp_from(pos, cap_kind, c, key)
    if exit_type == 'EVENT_FLAT':
        real_cap = (TF_TIME_SL or {}).get(pos.get('signal_tf'), (TF_TIME_SL or {}).get('default', 45))
        store[f"{key}|event_hold"] = _sp_from(pos, 'event_hold', real_cap, key)
    _save(store, _sp_path())

@_sp_safe
def _sp_spawn_blocked(kind, opt_type, tf, strike, sym, ltp, st_val, t2_value, t2_tf, reason=None):
    store = _load(_sp_path())
    if any(v.get('kind') == kind and v.get('opt_type') == opt_type for v in store.values()):
        return
    now = datetime.datetime.now(IST)
    inst = _get_instrument_info(sym)
    pos = {'opt_type': opt_type, 'signal_tf': tf, 'strike': strike, 'option_symbol': sym,
           'exchange': U.exchange, 'entry_premium': ltp, 'st_value': st_val, 't2_value': t2_value,
           't2_tf': t2_tf, 't1_hit': False, 'entry_time': now.isoformat(),
           'entry_date': now.strftime('%Y%m%d'), 'peak_premium': ltp, 'cost_sl_active': False,
           'cost_sl_level': 0, 'lot_size': inst['lot_size'], 'tick_size': inst['tick_size'],
           'session_half': _session_half(now)}
    bid, ask = _best_bid_ask(_LAST_QUOTES.get(f"{U.exchange}:{sym}") or {})
    pos.update(entry_bid=bid, entry_ask=ask, block_reason=reason,
               spread_pct=round((ask - bid) / ltp * 100, 3) if bid > 0 and ask > 0 and ltp > 0 else None)
    cap = (TF_TIME_SL or {}).get(tf, (TF_TIME_SL or {}).get('default', 45))
    store[f"{kind}|{opt_type}|{tf}|{strike}|{now.strftime('%Y%m%d%H%M')}"] = _sp_from(pos, kind, cap, None)
    _save(store, _sp_path())

def _sp_close(sp, price, exit_type):
    sp.update(status='closed', exit_premium=price, exit_type=exit_type,
              exit_time=datetime.datetime.now(IST).isoformat())
    e = sp.get('entry_premium') or 0
    sp['pnl_pct'] = round((price - e) / e * 100, 3) if e > 0 else None
    return sp

@_sp_safe
def _sp_monitor():
    store = _load(_sp_path())
    if not store:
        return
    # Review-4 R4-5: shadows left open by a missed EOD close out at the last price they were
    # seen at (never the next session's price), so a stale shadow neither corrupts the decision
    # data nor blocks new liq/event shadows through the kind x side dedup.
    today = datetime.datetime.now(IST).strftime('%Y%m%d')
    old = [k for k, v in store.items() if str(v.get('shadow_start', ''))[:10].replace('-', '') < today]
    if old:
        stale = [_sp_close(store.pop(k), store_v.get('last_ltp') or store_v.get('entry_premium', 0), 'STALE')
                 for k, store_v in [(k, store[k]) for k in old]]
        _sp_archive(stale)
        _save(store, _sp_path())
        log.warning(f"SHADOW: closed {len(stale)} stale shadow(s) from an earlier day")
        if not store:
            return
    syms = sorted({f"{v.get('exchange', U.exchange)}:{v['option_symbol']}" for v in store.values()})
    ltps = _ltp(syms)
    now = datetime.datetime.now(IST)
    flatten = _event_flatten_reason(now)
    closed = []
    for k, sp in list(store.items()):
        curr = ltps.get(f"{sp.get('exchange', U.exchange)}:{sp['option_symbol']}", 0)
        if curr <= 0:
            continue
        sp['last_ltp'] = curr
        if curr > sp.get('peak_premium', 0):
            sp['peak_premium'] = curr
        flat = flatten if sp.get('kind') == 'liq_block' else False
        exit_type, _ = _ladder(k, sp, curr, now, cap=sp.get('cap_min'), flatten=flat)
        if exit_type:
            closed.append(_sp_close(sp, curr, exit_type))
            del store[k]
    _sp_archive(closed)
    _save(store, _sp_path())
    if closed:
        log.info(f"SHADOW: closed {len(closed)} — " + ', '.join(
            f"{c['kind']}/{c.get('cap_min')} {c['option_symbol']} {c['exit_type']} {c.get('pnl_pct')}%" for c in closed))

@_sp_safe
def _sp_close_all(reason):
    store = _load(_sp_path())
    if not store:
        return
    syms = sorted({f"{v.get('exchange', U.exchange)}:{v['option_symbol']}" for v in store.values()})
    ltps = _ltp(syms)
    closed = []
    for k, sp in list(store.items()):
        # Quote, else the last price this shadow was monitored at, else its entry (just spawned).
        # Never the peak: that would bias the decision dataset upward (review-3 R3-3).
        px = (ltps.get(f"{sp.get('exchange', U.exchange)}:{sp['option_symbol']}", 0)
              or sp.get('last_ltp') or sp.get('entry_premium', 0))
        closed.append(_sp_close(sp, px, reason if ltps.get(
            f"{sp.get('exchange', U.exchange)}:{sp['option_symbol']}", 0) else reason + '_LASTSEEN'))
    _sp_archive(closed)
    _save({}, _sp_path())
    log.info(f"SHADOW: {reason} closed {len(closed)} open shadow(s)")

# ---------------------------------------------------------------------------
# Signal Detection
# ---------------------------------------------------------------------------

def _dedup(state_key, state):
    s = state.get(state_key, {})
    # A non-dict value (hand-edited state, a partial write, an older on-disk format) would
    # raise AttributeError on .get below. This runs in the entry hot path, so an unguarded
    # raise here aborts check_and_enter and stops ALL entries for the scan. Treat a corrupt
    # record as absent — the caller then rewrites it in the correct shape.
    if not isinstance(s, dict):
        log.warning(f"_dedup: corrupt state value for {state_key!r} "
                    f"({type(s).__name__}) — treating as absent")
        return True
    last = s.get('t')
    if not last: return True
    try:
        return datetime.datetime.fromisoformat(last).date() < datetime.datetime.now(IST).date()
    except Exception: return True

# ---------------------------------------------------------------------------
# MAGNET TOUCH gate — has the level this entry aims at already been spent today?
# See the TOUCH_GAP_PCT / TOUCH_LOOKBACK_MIN comment block for the defect and the pricing.
# The two sources use different lookbacks on purpose — see that block.
# ---------------------------------------------------------------------------

def _to_ist(dt):
    """Return dt as an IST-aware datetime; naive input is assumed to already be IST."""
    if dt is None:
        return None
    return IST.localize(dt) if dt.tzinfo is None else dt.astimezone(IST)

def _magnet_touched_by_ledger(opt_type, tf, positions, now, detail=None):
    """Evidence source (a): an earlier SAME-SIDE trade today already reached this TF's ST.

    Returns a short human reason (the `<source>` fragment of the log line) or None.
    Costs nothing — it reads the position ledger already in memory, makes no API call, and
    works ACROSS strikes, which is what catches the 2026-09-04 case where the 23950 CE
    consumed the same 15M ceiling the 24000 CE was about to aim at.

    `detail`, when a dict is passed, is filled with the fields the Telegram alert needs
    (key/opt_type/strike/tf/at/high). It is an optional out-parameter so the spec's
    `-> str | None` contract is unchanged for callers that do not want them.

    NEVER raises: this runs in the entry hot path (see _dedup) where an unguarded raise
    would abort the whole scan. A missing or corrupt exit_time makes that ONE position not
    count as evidence and logs a WARNING; it does not fail the check.
    """
    now = _to_ist(now)
    today = now.strftime('%Y%m%d')
    for key, pos in (positions or {}).items():
        if not isinstance(pos, dict):
            continue
        if pos.get('entry_date') != today or pos.get('opt_type') != opt_type:
            continue

        st_val = pos.get('st_value') or 0
        t2_val = pos.get('t2_value') or 0
        peak = pos.get('peak_premium') or 0
        # Reached as this trade's T1 (its own signal TF)...
        reached = bool(pos.get('signal_tf') == tf
                       and (pos.get('t1_hit') or (st_val > 0 and peak >= st_val)))
        ref_level = st_val
        # ...or as its T2 anchor, which is how one TF's ceiling gets spent by a trade
        # signalled on a lower TF.
        if not reached and pos.get('t2_tf') == tf and t2_val > 0 and peak >= t2_val:
            reached = True
            ref_level = t2_val
        if not reached:
            continue

        # Recency: a closed trade spends the magnet at its exit, an open one is spending it
        # right now.
        ref = now
        if pos.get('status') == 'closed':
            raw = pos.get('exit_time') or ''
            try:
                ref = _to_ist(datetime.datetime.fromisoformat(raw))
            except Exception:
                log.warning(f"MAGNET TOUCH: unparseable exit_time {raw!r} on {key} — "
                            f"not counted as evidence")
                continue
        mins_ago = (now - ref).total_seconds() / 60.0
        if mins_ago > TOUCH_LOOKBACK_MIN:
            continue

        if isinstance(detail, dict):
            detail.update({'key': key, 'opt_type': pos.get('opt_type', opt_type),
                           'strike': pos.get('strike', 0),
                           'tf': pos.get('signal_tf', ''),
                           'at': ref, 'mins_ago': max(mins_ago, 0.0),
                           'high': peak, 'level': ref_level})
        return f"ledger: {key} reached {tf} at {ref:%H:%M}"
    return None

def _magnet_touched_by_candles(token, st_val, now):
    """Evidence source (b): this strike's own 1-minute highs over TOUCH_CANDLE_LOOKBACK_MIN.

    Returns (touched, high, mins_ago) — or None meaning UNKNOWN (no history available),
    which the caller treats as fail-open. Fail-open is a decision: the ledger source still
    runs, and the base system traded for five months without this gate at all, so a Kite
    hiccup must not silently stop every entry of the day.

    `mins_ago` is measured from the row holding the MAX high, not from the last row — the
    age of the touch is what decides whether the magnet is still spent.
    """
    now = _to_ist(now)
    if not token or not st_val or st_val <= 0:
        return None
    # Naive IST, matching how the level update builds its _hist window.
    start = (now - datetime.timedelta(minutes=TOUCH_CANDLE_LOOKBACK_MIN)).replace(tzinfo=None)
    end = now.replace(tzinfo=None)
    df = _hist(token, start, end, 'minute')
    if df is None or getattr(df, 'empty', True) or 'High' not in df.columns:
        return None
    try:
        highs = pd.to_numeric(df['High'], errors='coerce').dropna()
        if highs.empty:
            return None
        idx = highs.idxmax()
        high = float(highs.loc[idx])
        mins_ago = 0.0
        if 'Date' in df.columns:
            ts = _to_ist(pd.Timestamp(df.loc[idx, 'Date']).to_pydatetime())
            mins_ago = max((now - ts).total_seconds() / 60.0, 0.0)
    except Exception as e:
        log.warning(f"MAGNET TOUCH: candle parse failed for token {token}: {e}")
        return None
    return (high >= st_val * (1 - TOUCH_GAP_PCT / 100), high, mins_ago)

def _magnet_touch_skip(opt_type, tf, strike, opt_sym, st_val, ltp, gap, positions, state,
                       now, sticky=False):
    """LAST gate before _open_pos: True => skip this entry, the magnet is already spent.

    Ledger first (free, no API), candles only when the ledger is clean — so the extra
    historical fetch is paid for only on a scan the bot would otherwise have traded.

    SKIP-ONLY. The caller `continue`s: no entry_ dedup consumed, no seen_ change, no sticky
    sweep. The only state written is the log/alert dedup key, so a later ST update can
    still let the setup fire. Never raises.
    """
    source, high, mins_ago, detail = None, 0.0, 0.0, {}
    try:
        source = _magnet_touched_by_ledger(opt_type, tf, positions, now, detail=detail)
    except Exception as e:
        log.warning(f"MAGNET TOUCH: ledger check failed for {opt_type} {tf} {strike}: {e}")
        source = None
    if source:
        high = detail.get('high', 0.0) or 0.0
        mins_ago = detail.get('mins_ago', 0.0) or 0.0
    else:
        try:
            res = _magnet_touched_by_candles(_token_for_symbol(opt_sym), st_val, now)
        except Exception as e:
            log.warning(f"MAGNET TOUCH: candle check failed for {opt_sym}: {e}")
            res = None
        if res is None:
            # Unknown, not clean — say so once per strike/tf/day and let the entry through.
            wk = f"touch_warn_{opt_type}_{tf}_{strike}"
            line = (f"MAGNET TOUCH: candle check unavailable for {opt_sym}, gate fail-open")
            if _dedup(wk, state):
                state[wk] = {'t': _to_ist(now).isoformat()}
                log.warning(line)
            else:
                log.debug(line)
            return False
        touched, high, mins_ago = res
        if not touched:
            return False
        source = 'candles'

    # INFO + Telegram once per strike/tf/day, then DEBUG. A blocked setup can sit in the
    # zone for hours and would otherwise emit one alert per minute per strike.
    tf_lbl = TF_DISPLAY.get(tf, tf)
    prefix = 'STICKY ' if sticky else ''
    # The evidence differs by source: for candles `high` is THIS strike's own print; for
    # the ledger it is the spending trade's peak on ITS strike, so name it as such rather
    # than let a 23950 peak read as a 24000 high.
    evidence = (f"high {high:.2f}, candles" if source == 'candles'
                else f"peak {high:.2f} on that trade, {source}")
    line = (f"MAGNET TOUCHED: {prefix}{opt_type} {tf_lbl} {strike} — T1 {st_val:.2f} "
            f"printed {mins_ago:.0f}m ago ({evidence}), "
            f"prem {ltp:.2f} gap {gap:.1f}%, entry skipped")
    dk = f"touch_{opt_type}_{tf}_{strike}"
    if _dedup(dk, state):
        state[dk] = {'t': _to_ist(now).isoformat()}
        log.info(line)
        if source == 'candles':
            second = f"T1 {st_val:.2f} printed {mins_ago:.0f}m ago (high {high:.2f})"
        else:
            second = (f"T1 {st_val:.2f} reached by {detail.get('opt_type', opt_type)} "
                      f"{detail.get('strike', '?')} "
                      f"{TF_DISPLAY.get(detail.get('tf', ''), detail.get('tf', '?'))} "
                      f"at {detail['at']:%H:%M}" if detail.get('at')
                      else f"T1 {st_val:.2f} reached earlier today")
        msg = (f"<b>⛔ MAGNET SPENT | {opt_type} {strike} {tf_lbl}</b>\n"
               f"{second}\n"
               f"Prem: {ltp:.2f} | Gap: {-gap:.1f}% — entry skipped")
        # Non-NIFTY paper instruments send entries and exits only (spec §12, review-3 R3-9);
        # under unlimited re-entry this alert would fire for every refused setup.
        if U.telegram_mode != 'entry_exit':
            try:
                common.sendMsgTelegram(msg, 'I')
            except Exception as e:
                log.error(f"Magnet touch alert fail: {e}")
    else:
        log.debug(line)
    return True

def check_and_enter(ce_levels, pe_levels, ce_info, pe_info, spot, ce_strike, pe_strike, expiry, dte, state):
    """Scan CE and PE for magnet signals: ST DOWN + premium below ST.

    Two zones:
      WATCHING: premium within WATCH_GAP_PCT (10%) below ST
      ENTRY:    MIN_REWARD_PCT (5%) <= gap <= ENTRY_GAP_PCT (7%)
    Signals with gap < MIN_REWARD_PCT are skipped (insufficient R:R).
    Mutual exclusion: only one of CE or PE per TF (no accidental straddle).
    CE and PE use separate OTM-biased strikes (never ITM).
    """
    now_iso = datetime.datetime.now(IST).isoformat()
    positions = _load(_pos_path())
    today = datetime.datetime.now(IST).strftime('%Y%m%d')

    for opt_type, levels, info in [('CE', ce_levels, ce_info), ('PE', pe_levels, pe_info)]:
        strike = ce_strike if opt_type == 'CE' else pe_strike
        if not info or info.get('ltp', 0) <= 0:
            continue
        opt_ltp = info['ltp']
        opt_sym = info['symbol']

        # Opposite type for mutual exclusion check
        opp = 'PE' if opt_type == 'CE' else 'CE'

        # SIGNAL_TIMEFRAMES only: 60M is computed for T2 but does not generate entries (see constant def)
        for tf in SIGNAL_TIMEFRAMES:
            d = levels.get(tf, {})
            if d.get('direction') != 'DOWN' or d.get('value', 0) <= 0:
                continue

            st_val = d['value']
            gap = ((st_val - opt_ltp) / st_val) * 100  # positive = below ST
            if gap < 0 or gap > WATCH_GAP_PCT:
                continue  # outside signal range

            sk = f"{opt_type}_{tf}"

            # SHADOW ONLY — records the running low / turn timing. Sits ABOVE the zone
            # split AND above the reward/T1 skips (moved here 2026-08-19) so the arm is
            # created while the setup is still merely WATCHED and keeps one continuous
            # price path. Inside the entry branch it armed and was consumed on the same
            # scan pass, so every matched entry logged obs=1 / fired={} / +0.0min. Above
            # MIN_REWARD_PCT because a premium that rallies to within 5% of ST has not
            # left the setup — it is the strongest turn there is — and going unobserved
            # there for >ENTRY_CONFIRM_STALE_GAP would destroy the arm's history.
            # Returns nothing; never branch on it.
            _confirm_observe(state, 'main', opt_type, tf, strike,
                             opt_ltp, gap, datetime.datetime.now(IST))
            _momentum_stamp(state, 'observe', 'main', opt_type, tf, strike, opt_sym,
                            opt_ltp, gap, datetime.datetime.now(IST))

            if gap < MIN_REWARD_PCT:
                # --- TOO CLOSE TO ST (insufficient reward) ---
                log.debug(f"Skip {opt_type} {tf}: gap {gap:.1f}% < min reward {MIN_REWARD_PCT}%")
                continue

            # Percentage reward can be adequate while the absolute band is inside the noise
            # floor (see MIN_T1_DISTANCE). Skip only — no dedup consumed, no state written:
            # like MIN_REWARD_PCT this says "not a good enough setup right now", not
            # "this setup is dead for the day". A later ST update can restore the room.
            t1_dist = st_val - opt_ltp
            if t1_dist < MIN_T1_DISTANCE:
                # INFO once per setup per day — the hit-rate is exactly what the next review
                # needs — then DEBUG. A blocked setup can sit in the zone for hours and would
                # otherwise emit one INFO line per minute per strike.
                dk = f"t1dist_{opt_type}_{tf}_{strike}"
                line = (f"MIN T1 DIST: {opt_type} {TF_DISPLAY[tf]} {strike} — gap {gap:.1f}% "
                        f"OK but T1 only {t1_dist:.2f} "
                        f"({t1_dist / TICK_SIZE:.0f} ticks) < {MIN_T1_DISTANCE:.2f}, skipped")
                if _dedup(dk, state):
                    state[dk] = {'t': now_iso}
                    log.info(line)
                else:
                    log.debug(line)
                continue

            if gap <= ENTRY_GAP_PCT:
                # --- ENTRY ZONE (5-7% below ST) ---
                # Watch gate: new strikes must be observed once today before entry
                seen_key = f"seen_{opt_type}_{tf}_{strike}"
                if _dedup(seen_key, state):
                    state[seen_key] = {'t': now_iso}
                    # WATCHING Telegram alerts suppressed (2026-06-29): bookkeeping
                    # below still runs so the entry watch-gate functions normally.
                    log.info(f"WATCH GATE: {opt_type} {TF_DISPLAY[tf]} {strike} "
                             f"gap={gap:.1f}% — new strike, deferring entry")
                    _record_watched_strike(state, opt_type, strike, opt_sym,
                                           expiry.isoformat(), tf, st_val,
                                           all_levels=levels)
                    continue

                # Unlimited slots (MCX, spec §12): re-enter any minute the setup is in zone once
                # no same-side position is open; the per-day keys below never match a slotted key.
                # per_half evening keys carry '_eve', so they need the same scan.
                if U.slot_mode in ('unlimited', 'per_half'):
                    why = _side_conflict(positions, opt_type, tf, today)
                    if why:
                        log.debug(f"Skip {opt_type} {tf}: {why}")
                        continue

                # Mutual exclusion: skip if opposite side already open for this TF
                opp_key = f"{U.name}_{opp}_{tf}_{today}"
                if opp_key in positions and positions[opp_key].get('status') == 'open':
                    log.debug(f"Skip {opt_type} {tf}: opposite {opp} already open")
                    continue

                # Cross-TF exclusion: skip if same opt_type already open on ANY TF
                # Prevents two BUY orders for the same exchange symbol
                same_type_open = False
                for tf_chk in ALL_TIMEFRAMES:
                    chk_key = f"{U.name}_{opt_type}_{tf_chk}_{today}"
                    if chk_key in positions and positions[chk_key].get('status') == 'open':
                        same_type_open = True
                        log.debug(f"Skip {opt_type} {tf}: same type already open on {tf_chk}")
                        break
                if same_type_open:
                    continue

                # Determine T2: the NEAREST higher TF whose ST qualifies (DOWN, above this
                # TF's ST). The fallback to T2_ALT matters: timeframes disagree — measured
                # 2026-09-01 on the live tape, the 30M ST can be UP while 15M is DOWN, and it
                # can print BELOW the 15M ST. Without the fallback those cases would silently
                # degrade a 15M entry to T1-only, and booking at T1 with no T2 is the one
                # option the data rules out (-1,818 over 17 trades). This keeps the invariant
                # the anchor change was meant to have: T2 can only get CLOSER, never vanish.
                t2_value, t2_tf = 0, ''
                for cand in (TF_NEXT.get(tf), T2_ALT.get(tf)):
                    if not cand:
                        continue
                    cd = levels.get(cand, {})
                    if cd.get('direction') == 'DOWN' and cd.get('value', 0) > st_val:
                        t2_value, t2_tf = cd['value'], cand
                        break
                # The anchor this TF used before 2026-09-01, recorded and NEVER acted on, so
                # the 60M -> 30M switch can be priced on real levels later. See T2_ALT.
                t2_alt_value, t2_alt_tf = 0, ''
                alt_tf = T2_ALT.get(tf)
                if alt_tf:
                    alt_d = levels.get(alt_tf, {})
                    if alt_d.get('direction') == 'DOWN' and alt_d.get('value', 0) > st_val:
                        t2_alt_value, t2_alt_tf = alt_d['value'], alt_tf

                # No-entry window gate (behavior B, 2026-07-17): suppress the OPEN and
                # consume the daily entry dedup so the setup cannot re-enter after the
                # window. Forward test 06-29→07-16: all 4 post-window re-entries were
                # immediate ADV_SL exits (−₹4,004) — a setup formed in a blocked hour
                # is stale for the day, not just the hour.
                if _in_no_entry_window():
                    ek = f"entry_{sk}"
                    if _dedup(ek, state):
                        state[ek] = {'t': now_iso}
                        log.info(f"NO-ENTRY WINDOW: {opt_type} {TF_DISPLAY[tf]} {strike} "
                                 f"gap={gap:.1f}% — entry suppressed, blocked for the day")
                    continue

                # EOD runway gate (2026-08-09): too little time left for the setup to
                # reach T1/T2 before the force-close. No dedup consumed — the day is
                # ending anyway, and the dedup key is per-day.
                if _too_close_to_eod():
                    log.info(f"EOD RUNWAY: {opt_type} {TF_DISPLAY[tf]} {strike} "
                             f"gap={gap:.1f}% — only {_minutes_to_eod()}min to "
                             f"{EOD_CLOSE_TIME:%H:%M} (need {MIN_MINUTES_TO_EOD}), entry suppressed")
                    continue

                # Entry-from gate (2026-10-27, MCX energy evening-only): skip-only like the EOD
                # runway — no dedup consumed, so the setup can still enter once the time arrives.
                if _before_entry_from():
                    _log_entry_from_skip(state, '', opt_type, tf, strike, gap, now_iso)
                    continue

                # B6 turbulence gate. Returns False unconditionally while
                # turbulence.BLOCK_ENTRIES is off, which is its shipped state — the
                # thresholds are unpriced. No dedup consumed: unlike the no-entry
                # window this is a market-condition gate, not a judgement that the
                # setup is stale, so it must not blackball the setup for the day.
                if turbulence.should_block():
                    log.info(f"TURBULENCE GATE: {opt_type} {TF_DISPLAY[tf]} {strike} "
                             f"gap={gap:.1f}% — entry suppressed")
                    continue

                # Scheduled-event and liquidity gates (spec §3/§4b): market-condition gates like
                # turbulence — no dedup consumed. Both are no-ops for NIFTY on a normal session.
                if _entry_gate_block('', opt_type, tf, strike, opt_sym, opt_ltp, st_val, gap,
                                     t2_value, t2_tf, state, now_iso):
                    continue

                ek = f"entry_{sk}{_half_suffix()}"
                if U.slot_mode == 'unlimited' or _dedup(ek, state):
                    # MAGNET TOUCH — the LAST gate, deliberately inside the dedup branch
                    # and above _open_pos: the candle fetch is paid for only on a scan the
                    # bot would otherwise have traded, and `continue` here leaves entry_
                    # UNCONSUMED so a later ST refresh can still restore the setup.
                    if _magnet_touch_skip(opt_type, tf, strike, opt_sym, st_val, opt_ltp,
                                          gap, positions, state,
                                          datetime.datetime.now(IST)):
                        continue
                    k = _open_pos(opt_type, tf, strike, expiry, opt_sym, opt_ltp,
                                  st_val, -gap, spot, t2_value=t2_value, t2_tf=t2_tf,
                                  t2_alt_value=t2_alt_value, t2_alt_tf=t2_alt_tf)
                    if k:
                        _confirm_mark_entry(state, 'main', opt_type, tf, strike,
                                            datetime.datetime.now(IST), opt_ltp)
                        _momentum_stamp(state, 'entry', 'main', opt_type, tf, strike,
                                        opt_sym, opt_ltp, gap, datetime.datetime.now(IST))
                        # Refresh positions dict so mutual exclusion sees this new entry
                        positions = _load(_pos_path())
                        msg = _fmt_entry(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp,
                                         st_val, -gap, spot, t2_value=t2_value, t2_tf=t2_tf)
                        try:
                            common.sendMsgTelegram(msg, 'I')
                        except Exception as e:
                            log.error(f"Entry alert fail: {e}")
                        # Clean from sticky watch if present — only when no slot is left today:
                        # under unlimited re-entry, or a per_half morning entry, the record must
                        # survive an ATM shift (review-4 R4-6).
                        if not _slot_left_later_today():
                            _remove_watched_strike(state, opt_type, strike, tf)
                    if U.slot_mode != 'unlimited':
                        state[ek] = {'t': now_iso}

            else:
                # --- WATCHING ZONE (7-10% below ST) ---
                # Mark as seen so entry gate passes if gap narrows later
                state[f"seen_{opt_type}_{tf}_{strike}"] = {'t': now_iso}
                wk = f"watch_{sk}"
                if _dedup(wk, state):
                    # WATCHING Telegram alerts suppressed (2026-06-29) — log only.
                    state[wk] = {'t': now_iso}
                    log.info(f"WATCHING: {opt_type} {TF_DISPLAY[tf]} gap={gap:.1f}% below ST")

                # Sticky watch: record regardless of alert dedup so it persists
                _record_watched_strike(state, opt_type, strike, opt_sym,
                                       expiry.isoformat(), tf, st_val,
                                       all_levels=levels)

# ---------------------------------------------------------------------------
# Dual-Mode Dispatch
# ---------------------------------------------------------------------------

def should_update():
    p = _levels_path()
    if not os.path.exists(p): return True
    now = datetime.datetime.now(IST)
    if now.minute % 10 == U.update_phase: return True  # 6 min into each 10M candle (NIFTY :x1, MCX :x6)
    try:
        ts = _load(p).get('timestamp')
        if ts:
            st = datetime.datetime.fromisoformat(ts)
            if st.tzinfo is None: st = IST.localize(st)
            if (now - st).total_seconds()/60 > 11: return True  # 10M + 1min margin
    except Exception: return True
    return False

def _pre_shift_check():
    """Final entry check on current strike before ATM shifts.

    If NIFTY moved and ATM is about to change, the old strike may now be
    in ENTRY zone (premium rose toward ST). Check it before the level
    update overwrites the saved data.
    """
    saved = _load(_levels_path())
    if not saved or 'CE_levels' not in saved:
        return

    old_ce_strike = saved.get('ce_strike', saved.get('strike', 0))
    old_pe_strike = saved.get('pe_strike', saved.get('strike', 0))
    ce_sym = saved.get('ce_symbol', '')
    pe_sym = saved.get('pe_symbol', '')
    expiry_str = saved.get('expiry', '')
    if not ce_sym or not pe_sym or not expiry_str:
        return
    try:
        saved_expiry = datetime.date.fromisoformat(expiry_str)
    except Exception:
        return
    ref = _ref_key(saved_expiry)
    if not ref:
        return

    # Quick spot check — is ATM actually shifting?
    spot_data = _ltp([ref])
    spot = spot_data.get(ref, 0)
    if spot <= 0:
        return
    new_ce_strike = get_otm_strike(spot, 'CE', saved_expiry)
    new_pe_strike = get_otm_strike(spot, 'PE', saved_expiry)
    if new_ce_strike == old_ce_strike and new_pe_strike == old_pe_strike:
        return  # ATM not shifting, normal scan handles it

    log.info(f"PRE-SHIFT: CE {old_ce_strike}->{new_ce_strike} PE {old_pe_strike}->{new_pe_strike}, final check")

    # Fetch fresh LTPs for old strike options
    keys = []
    if ce_sym: keys.append(f"{U.exchange}:{ce_sym}")
    if pe_sym: keys.append(f"{U.exchange}:{pe_sym}")
    if not keys:
        return
    ltps = _ltp(keys)
    ce_ltp = ltps.get(f"{U.exchange}:{ce_sym}", 0)
    pe_ltp = ltps.get(f"{U.exchange}:{pe_sym}", 0)

    ce_levels = copy.deepcopy(saved.get('CE_levels', {}))
    pe_levels = copy.deepcopy(saved.get('PE_levels', {}))

    # Patch close with fresh LTP
    for tf in ALL_TIMEFRAMES:
        if ce_ltp > 0 and ce_levels.get(tf, {}).get('value', 0) > 0:
            ce_levels[tf]['close'] = ce_ltp
        if pe_ltp > 0 and pe_levels.get(tf, {}).get('value', 0) > 0:
            pe_levels[tf]['close'] = pe_ltp

    try:
        expiry = datetime.date.fromisoformat(expiry_str)
    except Exception:
        return
    dte = (expiry - datetime.datetime.now(IST).date()).days

    ce_info = {'symbol': ce_sym, 'ltp': ce_ltp} if ce_ltp > 0 else None
    pe_info = {'symbol': pe_sym, 'ltp': pe_ltp} if pe_ltp > 0 else None

    now_time = datetime.datetime.now(IST).time()
    if SIGNAL_START_TIME <= now_time < EOD_CLOSE_TIME:
        state = _load(_state_path())
        check_and_enter(ce_levels, pe_levels, ce_info, pe_info,
                        spot, old_ce_strike, old_pe_strike, expiry, dte, state)
        _save(state, _state_path())

def _level_update_wait_s(now):
    """Seconds to wait so the level update starts at second LEVEL_UPDATE_SETTLE_S, never later."""
    return max(0, LEVEL_UPDATE_SETTLE_S - now.second)

def run_level_update(kite, df_instruments):
    # Settle until second LEVEL_UPDATE_SETTLE_S of the minute, not a flat 15 s. The bots
    # start at staggered cron offsets (NIFTY :00, BANKNIFTY :12 … SILVERM :48 — Kite rate
    # limit); a flat sleep pushed SILVERM's update to :x7:06, a minute late every time,
    # reading 7 minutes of the forming candle and sometimes another ATM strike (10-30 week
    # replay). NIFTY starts at :00 and still waits ~15 s.
    wait = _level_update_wait_s(datetime.datetime.now(IST))
    if wait:
        time.sleep(wait)
    log.info("--- LEVEL UPDATE ---")

    # Pre-check: enter old strike if it's now in ENTRY zone before ATM shifts
    _pre_shift_check()

    # Expiry first (a CSV lookup, no Kite call): MCX needs it to know which futures contract
    # is the reference. The NIFTY call sequence and log order are unchanged.
    expiry = get_weekly_expiry()
    ref = _ref_key(expiry) if expiry else U.ref_key
    if not ref: log.error("No reference contract"); return
    spot_data = _ltp([ref])
    spot = spot_data.get(ref, 0)
    if spot <= 0: log.error("No spot"); return

    ce_strike = get_otm_strike(spot, 'CE', expiry)
    pe_strike = get_otm_strike(spot, 'PE', expiry)
    if not expiry: log.error("No expiry"); return
    dte = (expiry - datetime.datetime.now(IST).date()).days
    log.info(f"Spot: {spot:.2f} | CE: {ce_strike} PE: {pe_strike} | Expiry: {expiry} ({dte}d)")

    ce_tok, ce_sym = _resolve_opt(ce_strike, expiry, 'CE')
    pe_tok, pe_sym = _resolve_opt(pe_strike, expiry, 'PE')
    if not ce_tok or not pe_tok: log.error(f"Resolve failed CE={ce_sym} PE={pe_sym}"); return

    ce_levels = compute_option_st(ce_tok)
    pe_levels = compute_option_st(pe_tok)

    data = {
        'timestamp': datetime.datetime.now(IST).isoformat(),
        'spot': spot, 'ce_strike': ce_strike, 'pe_strike': pe_strike,
        'expiry': expiry.isoformat(), 'dte': dte,
        'ce_symbol': ce_sym, 'pe_symbol': pe_sym,
        'ce_token': ce_tok, 'pe_token': pe_tok,
        'CE_levels': ce_levels, 'PE_levels': pe_levels,
    }
    _save(data, _levels_path())

    for ot, levels, stk in [('CE',ce_levels,ce_strike),('PE',pe_levels,pe_strike)]:
        print(f"\n  {ot} {stk}:")
        for tf in ALL_TIMEFRAMES:
            d = levels.get(tf,{})
            if d.get('direction')=='INSUFFICIENT': print(f"    {TF_DISPLAY[tf]:>3}  --  N/A"); continue
            g = ((d['close']-d['value'])/d['value']*100) if d['value']>0 else 0
            marker = " <<<" if d['direction']=='DOWN' and -WATCH_GAP_PCT <= g < 0 else ""
            print(f"    {TF_DISPLAY[tf]:>3}  {d['direction']:>4}  ST={d['value']:.1f}  LTP={d['close']:.1f}  gap={g:+.1f}%{marker}")
    log.info("Levels saved")

def _check_watched_strikes(spot, current_ce_strike, current_pe_strike, expiry, dte, state):
    """Check watched strikes that differ from current ATM for entry signals.

    After an ATM shift, previously watched strikes are still tracked here.
    Batch-fetches LTPs, enters if gap narrows to ENTRY zone, cleans up stale.
    """
    ws = state.get('watched_strikes', {})
    if not ws:
        return

    today = datetime.datetime.now(IST).date()
    today_str = today.strftime('%Y%m%d')
    positions = _load(_pos_path())
    now_iso = datetime.datetime.now(IST).isoformat()

    # Collect symbols for watched strikes that differ from current ATM
    active = {}
    for key, info in list(ws.items()):
        s = info.get('strike', 0)
        sym = info.get('symbol', '')
        # _has_signal_level, not just a non-empty dict: a record holding only a 60M
        # anchor can never enter (60M is skipped below) and would otherwise be
        # LTP-fetched every scan for the rest of the day.
        if not sym or not _has_signal_level(info):
            ws.pop(key, None); continue
        # Skip if this strike is the current OTM strike for its type (normal scan handles it)
        current_for_type = current_ce_strike if info.get('opt_type') == 'CE' else current_pe_strike
        if s == current_for_type:
            continue
        # Check expiry
        try:
            exp = datetime.date.fromisoformat(info.get('expiry', ''))
            if exp < today:
                ws.pop(key, None); continue
        except Exception:
            ws.pop(key, None); continue
        active[key] = info

    if not active:
        return

    # Batch LTP fetch for all watched symbols
    sym_keys = {k: f"{U.exchange}:{v['symbol']}" for k, v in active.items()}
    unique_keys = list(set(sym_keys.values()))
    all_ltps = _ltp(unique_keys) if unique_keys else {}

    for key, info in active.items():
        strike = info['strike']
        opt_type = info['opt_type']
        symbol = info['symbol']
        levels = info.get('levels', {})
        ltp = all_ltps.get(f"{U.exchange}:{symbol}", 0)
        if ltp <= 0:
            continue  # can't check, keep watching
        # Momentum buffer + a per-minute price line in the SAME format as LTP SCAN.
        # LTP SCAN only prints the ATM pair, so before 2026-09-25 a sticky strike's path
        # was invisible and 16 of 44 trades (08-04→09-25) had no pre-entry history.
        _momentum_record(state, symbol, ltp, datetime.datetime.now(IST))
        _log_sticky_ltp(opt_type, strike, ltp, levels)

        try:
            exp = datetime.date.fromisoformat(info['expiry'])
        except Exception:
            continue
        exp_dte = (exp - today).days

        tfs_done = []
        for tf, st_val in list(levels.items()):
            # Drop levels for TFs no longer active (e.g., legacy 30M entries
            # in watched_strikes from before 2026-05-12 30M removal)
            if tf not in ALL_TIMEFRAMES:
                tfs_done.append(tf); continue
            if st_val <= 0:
                tfs_done.append(tf); continue

            # 60M is a T2 anchor only — skip entry but KEEP it in the watched levels
            # (do NOT sweep) so a sticky 15M entry can still resolve T2 = 60M ST below.
            if tf not in SIGNAL_TIMEFRAMES:
                continue

            gap = ((st_val - ltp) / st_val) * 100  # positive = below ST

            # Premium above ST — signal invalid for this TF
            if gap < 0:
                tfs_done.append(tf)
                log.info(f"STICKY DROP: {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} "
                         f"— prem {ltp:.2f} above ST {st_val:.2f}")
                continue

            # Drifted too far away
            if gap > STICKY_WATCH_MAX_GAP:
                tfs_done.append(tf)
                log.info(f"STICKY DROP: {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} "
                         f"— gap {gap:.1f}% > {STICKY_WATCH_MAX_GAP}%")
                continue

            # SHADOW ONLY — see check_and_enter for why this sits above every skip.
            # Shares one arm with the main path (see _arm_key), so a strike handed over as
            # ATM drifts keeps a single continuous low. The sticky band runs wider, to
            # STICKY_WATCH_MAX_GAP.
            _confirm_observe(state, 'sticky', opt_type, tf, strike,
                             ltp, gap, datetime.datetime.now(IST))
            _momentum_stamp(state, 'observe', 'sticky', opt_type, tf, strike, symbol,
                            ltp, gap, datetime.datetime.now(IST))

            # ENTRY zone (with min reward filter)
            if gap < MIN_REWARD_PCT:
                log.debug(f"STICKY skip {opt_type} {strike} {tf}: gap {gap:.1f}% < min reward")
                continue

            # Absolute room-to-work floor — see MIN_T1_DISTANCE. Skip, do not sweep: the
            # recorded ST can be refreshed by a later level update, restoring the band.
            t1_dist = st_val - ltp
            if t1_dist < MIN_T1_DISTANCE:
                # INFO once per setup per day, then DEBUG — see check_and_enter.
                dk = f"t1dist_{opt_type}_{tf}_{strike}"
                line = (f"MIN T1 DIST: STICKY {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} — "
                        f"gap {gap:.1f}% OK but T1 only {t1_dist:.2f} "
                        f"({t1_dist / TICK_SIZE:.0f} ticks) < {MIN_T1_DISTANCE:.2f}, skipped")
                if _dedup(dk, state):
                    state[dk] = {'t': now_iso}
                    log.info(line)
                else:
                    log.debug(line)
                continue

            if gap <= ENTRY_GAP_PCT:
                if U.slot_mode in ('unlimited', 'per_half'):
                    why = _side_conflict(positions, opt_type, tf, today_str)
                    if why:
                        log.debug(f"STICKY skip {opt_type} {strike} {tf}: {why}")
                        continue
                opp = 'PE' if opt_type == 'CE' else 'CE'
                opp_key = f"{U.name}_{opp}_{tf}_{today_str}"
                if opp_key in positions and positions[opp_key].get('status') == 'open':
                    log.debug(f"STICKY skip {opt_type} {strike} {tf}: opposite open")
                    continue

                # Cross-TF exclusion: same opt_type already open on any TF
                same_open = any(
                    positions.get(f"{U.name}_{opt_type}_{t}_{today_str}", {}).get('status') == 'open'
                    for t in ALL_TIMEFRAMES)
                if same_open:
                    log.debug(f"STICKY skip {opt_type} {strike} {tf}: same type open another TF")
                    continue

                # Determine T2 from watched levels (next higher TF if present).
                # `levels` here is the watched-strike record, which only ever holds anchors
                # that already passed the DOWN/above-ST test in _record_watched_strike.
                s_t2, s_t2_tf, s_t2_alt, s_t2_alt_tf = _sticky_t2(levels, tf, st_val)

                # No-entry window gate (behavior B, 2026-07-17): consume the daily entry
                # dedup so this opt/TF cannot re-enter after the window today. Level is
                # NOT added to tfs_done — the watched strike survives and may enter on a
                # future day (dedup is per-day).
                if _in_no_entry_window():
                    ek = f"entry_{opt_type}_{tf}"
                    if _dedup(ek, state):
                        state[ek] = {'t': now_iso}
                        log.info(f"NO-ENTRY WINDOW: STICKY {opt_type} {strike} "
                                 f"{TF_DISPLAY.get(tf,tf)} gap={gap:.1f}% — entry suppressed, blocked for the day")
                    continue

                # EOD runway gate (2026-08-09) — see check_and_enter for rationale.
                if _too_close_to_eod():
                    log.info(f"EOD RUNWAY: STICKY {opt_type} {strike} "
                             f"{TF_DISPLAY.get(tf,tf)} gap={gap:.1f}% — only {_minutes_to_eod()}min to "
                             f"{EOD_CLOSE_TIME:%H:%M} (need {MIN_MINUTES_TO_EOD}), entry suppressed")
                    continue

                # Entry-from gate — see check_and_enter. Skip-only: the watched level is NOT
                # swept, so it can still fire once entries open.
                if _before_entry_from():
                    _log_entry_from_skip(state, 'STICKY ', opt_type, tf, strike, gap, now_iso)
                    continue

                # B6 turbulence gate — see check_and_enter. Off by default; no dedup
                # consumed, and the level is NOT added to tfs_done, so the watched
                # strike survives a turbulent day rather than being swept by it.
                if turbulence.should_block():
                    log.info(f"TURBULENCE GATE: STICKY {opt_type} {strike} "
                             f"{TF_DISPLAY.get(tf,tf)} gap={gap:.1f}% — entry suppressed")
                    continue

                # Event / liquidity gates — see check_and_enter. Skip only: the watched level
                # is NOT swept, so it can still fire once the window passes.
                if _entry_gate_block('STICKY ', opt_type, tf, strike, symbol, ltp, st_val, gap,
                                     s_t2, s_t2_tf, state, now_iso):
                    continue

                ek = f"entry_{opt_type}_{tf}{_half_suffix()}"
                if U.slot_mode == 'unlimited' or _dedup(ek, state):
                    # Recorded level may be hours old — re-validate against the current ST
                    # (see _sticky_recheck). Nothing is consumed on skip/drop.
                    action, f_st, f_gap, t2_now = _sticky_recheck(symbol, opt_type, tf,
                                                                  strike, ltp, levels)
                    if action == 'drop':
                        tfs_done.append(tf)
                        continue
                    if action == 'skip':
                        continue
                    st_val, gap = f_st, f_gap
                    s_t2, s_t2_tf, s_t2_alt, s_t2_alt_tf = _sticky_t2(t2_now, tf, st_val)
                    # MAGNET TOUCH — see check_and_enter. `continue` skips the
                    # tfs_done.append below as well, so the watched strike is NOT swept:
                    # skip-only, the level can be refreshed and fire later.
                    if _magnet_touch_skip(opt_type, tf, strike, symbol, st_val, ltp,
                                          gap, positions, state,
                                          datetime.datetime.now(IST), sticky=True):
                        continue
                    k = _open_pos(opt_type, tf, strike, exp, symbol, ltp,
                                  st_val, -gap, spot, t2_value=s_t2, t2_tf=s_t2_tf,
                                  t2_alt_value=s_t2_alt, t2_alt_tf=s_t2_alt_tf)
                    if k:
                        _confirm_mark_entry(state, 'sticky', opt_type, tf, strike,
                                            datetime.datetime.now(IST), ltp)
                        _momentum_stamp(state, 'entry', 'sticky', opt_type, tf, strike,
                                        symbol, ltp, gap, datetime.datetime.now(IST))
                        positions = _load(_pos_path())
                        msg = _fmt_entry(opt_type, strike, exp, exp_dte, tf,
                                         symbol, ltp, st_val, -gap, spot,
                                         t2_value=s_t2, t2_tf=s_t2_tf)
                        msg += "\n<i>(watched strike — ATM shifted)</i>"
                        try:
                            common.sendMsgTelegram(msg, 'I')
                        except Exception as e:
                            log.error(f"Sticky entry alert fail: {e}")
                        log.info(f"STICKY ENTRY: {opt_type} {strike} "
                                 f"{TF_DISPLAY.get(tf,tf)} gap={gap:.1f}%")
                    if U.slot_mode != 'unlimited':
                        state[ek] = {'t': now_iso}
                else:
                    # Dedup already consumed today (an earlier entry, or a no-entry
                    # window block). The key is per-day, so this TF cannot fire again
                    # today — sweep it instead of re-quoting the symbol every minute.
                    log.debug(f"STICKY skip {opt_type} {strike} {tf}: entry dedup "
                              f"already consumed today")
                # per_day: the TF cannot fire again today, so sweep it. unlimited: keep it — the
                # frozen re-entry rule applies to watched strikes too (review-3 R3-6). per_half:
                # keep it through the morning (the evening slot can still use it), sweep after.
                if not _slot_left_later_today():
                    tfs_done.append(tf)
            else:
                log.debug(f"STICKY: {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} "
                          f"gap={gap:.1f}% — watching")

        for tf in tfs_done:
            levels.pop(tf, None)
        # Drop once no tradeable TF is left, rather than only when the dict empties —
        # sweeping the last signal TF strands the 60M anchor behind it.
        if not _has_signal_level(info):
            ws.pop(key, None)

def run_ltp_scan():
    log.info("--- LTP SCAN ---")
    saved = _load(_levels_path())
    if not saved or 'CE_levels' not in saved: log.warning("No levels"); return

    ce_levels = copy.deepcopy(saved.get('CE_levels',{}))
    pe_levels = copy.deepcopy(saved.get('PE_levels',{}))
    ce_strike = saved.get('ce_strike', saved.get('strike', 0))
    pe_strike = saved.get('pe_strike', saved.get('strike', 0))
    ce_sym = saved.get('ce_symbol',''); pe_sym = saved.get('pe_symbol','')
    expiry_str = saved.get('expiry','')
    if not ce_sym or not pe_sym: return
    try: expiry = datetime.date.fromisoformat(expiry_str)
    except Exception: return
    dte = (expiry - datetime.datetime.now(IST).date()).days

    # Batch fetch LTPs
    ref = _ref_key(expiry)
    if not ref: return
    all_ltps = _ltp([ref, f"{U.exchange}:{ce_sym}", f"{U.exchange}:{pe_sym}"])
    spot = all_ltps.get(ref, 0)
    ce_ltp = all_ltps.get(f"{U.exchange}:{ce_sym}", 0)
    pe_ltp = all_ltps.get(f"{U.exchange}:{pe_sym}", 0)

    # Update close with live LTP
    for tf in ALL_TIMEFRAMES:
        if ce_ltp > 0 and ce_levels.get(tf,{}).get('value',0) > 0: ce_levels[tf]['close'] = ce_ltp
        if pe_ltp > 0 and pe_levels.get(tf,{}).get('value',0) > 0: pe_levels[tf]['close'] = pe_ltp

    # Log
    for ot, levels, ltp, stk in [('CE',ce_levels,ce_ltp,ce_strike),('PE',pe_levels,pe_ltp,pe_strike)]:
        parts = []
        for tf in ALL_TIMEFRAMES:
            d = levels.get(tf,{})
            if d.get('direction')=='DOWN' and d.get('value',0)>0 and ltp>0:
                g = ((d['value']-ltp)/d['value'])*100
                parts.append(f"{TF_DISPLAY[tf]}:{g:.1f}%↓ST{'*' if g <= WATCH_GAP_PCT else ''}")
        if parts: log.info(f"  {ot} {stk} LTP={ltp:.2f} | {' | '.join(parts)}")

    ce_info = {'symbol':ce_sym,'ltp':ce_ltp} if ce_ltp > 0 else None
    pe_info = {'symbol':pe_sym,'ltp':pe_ltp} if pe_ltp > 0 else None

    # B6 tripwire — evaluated every scan, gates nothing while BLOCK_ENTRIES is False.
    # Placed before the signal window so a turbulent morning is recorded even during
    # the 09:15-09:30 levels-only period, which is exactly when an overnight event shows.
    # NIFTY only: the tripwire reads NIFTY + India VIX, and every other process calling it
    # would spend a quote/min and race on one shared state file (review-1 M10).
    if U.turbulence:
        _scan_turbulence(spot)
    if U.file_suffix:   # every underlying except NIFTY (unchanged)
        _liquidity_probe(ce_sym, pe_sym, ce_strike, pe_strike, expiry, spot)

    # Signals: only between SIGNAL_START_TIME (9:30) and EOD window (15:25)
    now_time = datetime.datetime.now(IST).time()
    # State is loaded here (not inside the signal branch) so the momentum buffer also
    # fills during the 09:15-09:30 levels-only window — a 09:30 entry then has history.
    state = _load(_state_path())
    _momentum_record(state, ce_sym, ce_ltp, datetime.datetime.now(IST))
    _momentum_record(state, pe_sym, pe_ltp, datetime.datetime.now(IST))
    if SIGNAL_START_TIME <= now_time < EOD_CLOSE_TIME:
        check_and_enter(ce_levels, pe_levels, ce_info, pe_info, spot, ce_strike, pe_strike, expiry, dte, state)
        _check_watched_strikes(spot, ce_strike, pe_strike, expiry, dte, state)
        _save(state, _state_path())
    elif now_time < SIGNAL_START_TIME:
        log.info(f"Pre-signal window ({now_time.strftime('%H:%M')}) — levels only, no signals")
        if MOMENTUM_SHADOW:
            try:
                _save(state, _state_path())
            except Exception as e:
                log.error(f"MOMENTUM: pre-signal state save failed (ignored): {e}")

    # Monitor positions
    try: monitor_positions()
    except Exception as e: log.error(f"Monitor failed: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Lock & Main
# ---------------------------------------------------------------------------

_LOCK = os.path.join(CURRENT_DIR, f'option_buy{U.file_suffix}.lock')

def _pid_alive(pid):
    if platform.system()=='Windows':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000,False,pid)
        if h: ctypes.windll.kernel32.CloseHandle(h); return True
        return False
    try: os.kill(pid,0); return True
    except OSError: return False

def acquire_lock():
    if os.path.exists(_LOCK):
        try:
            with open(_LOCK) as f: pid = int(f.read().strip())
            if _pid_alive(pid): return False
        except Exception: pass
    with open(_LOCK,'w') as f: f.write(str(os.getpid()))
    return True

def release_lock():
    try:
        if os.path.exists(_LOCK):
            with open(_LOCK) as f: p = int(f.read().strip())
            if p == os.getpid(): os.remove(_LOCK)
    except Exception: pass

def main():
    # An underlying runs only once every build step it depends on has landed (underlyings.py
    # `enabled`). Checked before the lock so a disabled process touches nothing but its own log.
    if not U.enabled:
        log.warning(f"{U.name} is not enabled in this build — exiting"); sys.exit(0)
    if not acquire_lock(): log.warning("Locked"); sys.exit(0)
    atexit.register(release_lock)
    atexit.register(_kite_stats_close)
    log.info("=" * 60); log.info(f"{U.name} OPTION MAGNET"); log.info("=" * 60)

    now = datetime.datetime.now(IST)
    if now.weekday() >= 5: log.info("Weekend"); sys.exit()
    # Holiday-aware session (review-1 H4, proven 2026-10-02: 375 scans on a frozen NSE tape).
    sess = _session_today(now.date())
    if sess is None:
        log.info(f"Market closed today ({market_calendar.exchange_for(U)} holiday)"); sys.exit()
    if now.time() < sess[0] or now.time() >= sess[1]:
        log.info(f"Off hours ({now.strftime('%H:%M')})"); sys.exit()
    _apply_session(sess, now.date())
    _load_event_windows(now)

    # Initialize Neo bridge + time SL config
    _init_bridge()

    try: _close_stale()
    except Exception as e: log.error(f"Stale: {e}")

    try: _clean_watched_strikes()
    except Exception as e: log.error(f"Clean watches: {e}")

    if now.time() >= EOD_CLOSE_TIME:
        log.info("EOD -- closing all")
        try: _close_eod()
        except Exception as e: log.error(f"EOD: {e}")

    df_instruments = get_instruments()
    kite = get_kite()

    if should_update(): run_level_update(kite, df_instruments)
    run_ltp_scan()

    # Sync TP fills from Neo exchange (if bridge active)
    # CRITICAL: must update st_opt_positions.json to prevent double-sell
    if TRADE_ENABLED and _NEO_BRIDGE:
        try:
            fills = _NEO_BRIDGE.sync_tp_fills()
            if fills:
                positions = _load(_pos_path())
                for key, fill_price in fills:
                    log.info(f"TP filled on Neo: {key} @ {fill_price}")
                    if key in positions and positions[key].get('status') == 'open':
                        _close_pos(key, positions[key], fill_price, 'TP')
                _save(positions, _pos_path())

            # Also sync entry rejections — mark them closed in our state
            rejections = _NEO_BRIDGE.get_rejected_entries()
            if rejections:
                positions = _load(_pos_path())
                for key in rejections:
                    if key in positions and positions[key].get('status') == 'open':
                        positions[key]['status'] = 'closed'
                        positions[key]['exit_type'] = 'ENTRY_REJECTED'
                        log.warning(f"Entry rejected on exchange: {key}")
                _save(positions, _pos_path())
        except Exception as e:
            log.error(f"TP sync failed: {e}")

    _kite_stats_close()
    log.info("=" * 60); log.info("COMPLETE"); log.info("=" * 60)

if __name__ == "__main__":
    main()

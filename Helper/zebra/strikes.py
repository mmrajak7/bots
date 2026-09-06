"""Zebra strike analyzer — expiry, ATM strike, ATM book, and the BCS pair.

The name is historical. This module served the back-ratio ("zebra") structure
— BUY 2x deep-ITM + SELL 1x ATM — which was **DECOMMISSIONED on 2026-08-27**.
Two functions live here now:

  `analyze()`      resolves the expiry, the ATM strike and the ATM book. It no
                   longer prices the back ratio; `best` is always None and
                   `back_ratio` reads `'retired'`. See its docstring for why
                   (rate limit: it cost up to 8 deep-ITM quotes per signal).
  `analyze_bcs()`  builds the pair that actually trades — BUY 1x ATM +
                   SELL 1x the strike nearest the ST target — with its own
                   hard gates. `cfg.ENTRY_STRUCTURE` is `'bcs'` and config
                   accepts no other value.

Rules for the live BCS path are documented on `analyze_bcs` itself, not here,
so this text cannot drift from them. Shared inputs:

  - K_S / the BCS long leg is the closest strike to spot (ATM).
  - OI >= cfg.MIN_LEG_OI per leg (a HARD gate in `analyze_bcs`).
  - DTE cfg.MIN_DTE - cfg.MAX_DTE, first expiry at or beyond the floor.

~450 historical records were opened as back ratios. They remain fully
readable: each stores its own strikes, symbols and prices, and no read,
report or close path calls `analyze()`.
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import config as cfg

logger = logging.getLogger(__name__)

# The back-ratio (BUY 2x ITM + SELL 1x ATM) structure was decommissioned by
# the owner on 2026-08-27. `analyze()` stamps this on every result so a caller
# reading `best is None` can tell "retired by decision" from "nothing viable
# was found", which are opposite facts. Historical records that USED the
# structure stay readable — they carry their own strikes and prices.
BACK_RATIO_RETIRED = 'retired'

# ── Quote-reliability guard (2026-07-24 NHPC false-SL incident) ────────────
# Reuse the BCS monitor's leg reliability rule so both systems judge a garbage
# opening book identically. The import pulls in the bcs package (IPv4 socket
# monkeypatch is idempotent; nothing else runs at its module top-level), and
# nothing in bcs imports zebra, so there is no circular-import risk. A defensive
# fallback keeps zebra usable even if the import ever breaks — the fallback
# replicates the width / crossed / one-sided core with the same thresholds.
try:
    from bcs.spread_monitor import (
        leg_quote_reliable as _bcs_leg_reliable,
        LTP_FRESH_SEC as _LTP_FRESH_SEC,
    )
except Exception as _e:  # pragma: no cover - import safety fallback
    logger.warning("Could not import bcs reliability helpers (%s); using local "
                   "fallback", _e)
    _bcs_leg_reliable = None
    _LTP_FRESH_SEC = 30 * 60


def _now_ist():
    """IST wall-clock, naive — the same clock `bcs.spread_monitor.now_ist`
    reads, so the two engines agree about what time it is at the exchange.

    Every date/DTE decision in this module is an EXCHANGE fact (is this print
    from today, how many days to expiry), so none of them may be answered from
    the box's timezone.
    """
    return datetime.now(cfg.IST).replace(tzinfo=None)


def _ltp_fresh(ltt) -> bool:
    """True if the option printed a trade within _LTP_FRESH_SEC (today only).

    Mirrors bcs.get_option_depth: a stale LTP must never veto a legitimately
    repriced book, so freshness gates the LTP-divergence check inside
    leg_quote_reliable.
    """
    if ltt is None:
        return False
    try:
        ltt_dt = ltt if hasattr(ltt, 'date') else \
            datetime.strptime(str(ltt)[:19], '%Y-%m-%d %H:%M:%S')
        # IST both sides. `ltt` is the EXCHANGE's last-trade time; measuring
        # its age against the box clock made every print read fresh (or every
        # print read stale) by the offset. This is the twin of
        # `bcs.spread_monitor._ltp_fresh` — same name, same docstring, fixed
        # there first and not here. [[feedback_the_copy_you_did_not_open]]
        now = _now_ist()
        if ltt_dt.date() != now.date():
            return False
        return (now - ltt_dt).total_seconds() <= _LTP_FRESH_SEC
    except Exception:
        return False


def _leg_reliable(q: dict) -> tuple:
    """Judge a leg's top-of-book for VALUATION. Returns (reliable, reason).

    Delegates to bcs.leg_quote_reliable (same width/crossed/one-sided/LTP-veto
    rule as the BCS monitor). Fallback covers the width/crossed/one-sided core.
    """
    if _bcs_leg_reliable is not None:
        return _bcs_leg_reliable(q)
    bid, ask = q.get('bid', 0), q.get('ask', 0)
    if bid <= 0 or ask <= 0 or q.get('bid_qty', 0) <= 0 or q.get('ask_qty', 0) <= 0:
        return False, 'no_two_way_book'
    if bid > ask:
        return False, f'crossed_book bid {bid} > ask {ask}'
    width = ask - bid
    mid = (bid + ask) / 2.0
    if width > max(0.30, 0.25 * mid) + 1e-9:
        return False, f'wide_book width {width:.2f} vs mid {mid:.2f}'
    return True, ''


# ── Options CSV loader ────────────────────────────────────────────────────

_OPTIONS_CACHE: dict = {}      # {stock: {expiry: {strike: {CE: {...}, PE: {...}}}}}
_OPTIONS_CACHE_LOADED = False
#: (mtime_ns, size) of the CSV this cache was built from, so a
#: long-lived process can notice the 09:00 refresh. See
#: `_load_options_csv`.
_OPTIONS_CACHE_KEY = None


def options_csv_age_days(now=None):
    """How old `nse_stocks_options.csv` is, in days. None when it is missing.

    Read from the file's mtime rather than from anything inside it: the file
    has no header row saying when it was built, and the only thing that
    rewrites it is the 09:00 refresh job.
    """
    try:
        st = cfg.OPTIONS_CSV.stat()
    except OSError:
        return None
    now = now or _now_ist()
    return (now - datetime.fromtimestamp(st.st_mtime)).total_seconds() / 86400.0


def options_csv_stale(now=None):
    """`(stale, why)` for the ENTRY gate. Missing counts as STALE.

    **This file is where LOT SIZES come from, and a lot size becomes an order
    QUANTITY at the broker.** It is rebuilt by its own 09:00 Mon-Fri cron and
    nothing checked that the cron had run: a job that dies quietly leaves
    every entry sizing itself from whatever was true the last time it worked,
    and the failure is invisible because a stale chain still parses, still
    has strikes, and still returns a number.

    Missing fails CLOSED for the same reason an unknown OI does: the answer
    "I could not look" must not be spendable as "it is fine".
    ([[feedback_never_asked_is_not_failed]] - the two states differ, and
    `why` says which.)

    EXITS ARE NEVER GATED ON THIS and must never be. A close reads its
    symbols and quantity off the trade record; refusing to exit because a
    scanner input went stale would abandon a live position over a cron job.
    """
    age = options_csv_age_days(now=now)
    if age is None:
        return True, 'options CSV is MISSING at %s' % cfg.OPTIONS_CSV
    if age > cfg.OPTIONS_CSV_MAX_AGE_DAYS:
        return True, ('options CSV is %.1f days old (max %d) - the 09:00 '
                      'refresh has not run; lot sizes may be wrong'
                      % (age, cfg.OPTIONS_CSV_MAX_AGE_DAYS))
    return False, ''


def _load_options_csv() -> None:
    """Load nse_stocks_options.csv into the in-memory chain map."""
    global _OPTIONS_CACHE, _OPTIONS_CACHE_LOADED, _OPTIONS_CACHE_KEY

    # RELOAD WHEN THE FILE CHANGES (fixed 2026-08-31).
    #
    # The latch was `if _OPTIONS_CACHE_LOADED: return`, loaded once per
    # PROCESS and never invalidated, while `options_csv_stale()` -- Gate 0,
    # the hard gate that certifies this very file -- re-stats it on every
    # call. In any long-lived process the two diverge: `zebra loop` started
    # before the 09:00 refresh keeps yesterday's chain, and after the refresh
    # lands the gate reads a fresh mtime and PASSES while symbols, lot sizes
    # and expiries still flow from yesterday's rows. Gate 0's own docstring
    # calls this the file "where LOT SIZES come from, and a lot size becomes
    # an order QUANTITY at the broker".
    #
    # Benign under the one-shot `zebra run` cron (a fresh process each tick),
    # which is why it has never bitten -- and exactly the kind of latent fault
    # that surfaces the day somebody runs `zebra loop` instead.
    try:
        st = cfg.OPTIONS_CSV.stat()
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    if _OPTIONS_CACHE_LOADED and key == _OPTIONS_CACHE_KEY:
        return
    if _OPTIONS_CACHE_LOADED and key is not None:
        logger.info("options CSV changed on disk — reloading the chain cache")
    _OPTIONS_CACHE_KEY = key

    if not cfg.OPTIONS_CSV.exists():
        logger.error("Options CSV not found: %s. Run kite_nse_options.py first.",
                     cfg.OPTIONS_CSV)
        _OPTIONS_CACHE_LOADED = True
        return

    chain: dict = defaultdict(lambda: defaultdict(dict))
    rows = 0
    with open(cfg.OPTIONS_CSV, newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get('option_tradingsymbol'):
                continue
            stock = row['stock_symbol']
            try:
                strike = float(row['option_strike'])
                lot_size = int(row['option_lot_size'])
                token = int(row['option_instrument_token'])
            except (ValueError, KeyError):
                continue
            expiry = row['option_expiry']
            opt_type = row['option_type']  # 'CE' or 'PE'
            chain[stock][expiry].setdefault(strike, {})[opt_type] = {
                'tradingsymbol': row['option_tradingsymbol'],
                'lot_size': lot_size,
                'instrument_token': token,
            }
            rows += 1
    _OPTIONS_CACHE = chain
    _OPTIONS_CACHE_LOADED = True
    # The AGE, on every load, whether or not it passes. A gate that only
    # speaks when it fires leaves "the file is fresh" and "nobody ever looked"
    # printing the same thing, which is how this input went six weeks stale
    # without a line in any log.
    age = options_csv_age_days()
    logger.info("Options CSV loaded: %d rows, %d stocks, %s",
                rows, len(_OPTIONS_CACHE),
                'age unknown' if age is None else 'age %.1f days' % age)


def _pick_expiry(stock: str, today: Optional[datetime] = None) -> Optional[str]:
    """Pick first expiry with DTE >= MIN_DTE (and <= MAX_DTE).

    Returns the expiry string (YYYY-MM-DD) or None.
    """
    _load_options_csv()
    today = today or _now_ist()
    expiries = sorted(_OPTIONS_CACHE.get(stock, {}).keys())
    for exp in expiries:
        try:
            dte = (datetime.strptime(exp, '%Y-%m-%d').date() - today.date()).days
        except ValueError:
            continue
        if cfg.MIN_DTE <= dte <= cfg.MAX_DTE:
            return exp
    return None


def _list_strikes(stock: str, expiry: str, opt_type: str) -> list:
    """Sorted list of strikes that have the requested option type."""
    chain = _OPTIONS_CACHE.get(stock, {}).get(expiry, {})
    return sorted([k for k, v in chain.items() if opt_type in v])


def _quote_option(kite, tradingsymbol: str) -> dict:
    """Get bid/ask/OI for an NFO option.

    Returns {bid, ask, mid, oi, last, bid_qty, ask_qty, ltp, ltp_fresh,
    reliable, unreliable_reason, error?}. `reliable` is False when the
    top-of-book is unusable for valuation (one-sided, crossed, or wider than
    max(0.30, 25% of mid)) — a garbage opening book must never feed a DEBIT-SL
    trigger or an ENTER click-copy price (2026-07-24 NHPC incident).
    """
    key = f"NFO:{tradingsymbol}"
    try:
        q = kite.quote([key])[key]
        depth = q.get('depth', {})
        buy = depth.get('buy', [])
        sell = depth.get('sell', [])
        bid = buy[0]['price'] if buy else 0.0
        ask = sell[0]['price'] if sell else 0.0
        bid_qty = buy[0].get('quantity', 0) if buy else 0
        ask_qty = sell[0].get('quantity', 0) if sell else 0
        oi = q.get('oi', 0)
        last = q.get('last_price', 0)
        mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else last
        out = {
            'bid': bid, 'ask': ask, 'mid': mid, 'oi': oi, 'last': last,
            'bid_qty': bid_qty, 'ask_qty': ask_qty,
            'ltp': last, 'ltp_fresh': _ltp_fresh(q.get('last_trade_time')),
        }
        ok, why = _leg_reliable(out)
        out['reliable'] = ok
        out['unreliable_reason'] = why
        return out
    except Exception as e:
        return {'bid': 0, 'ask': 0, 'mid': 0, 'oi': 0, 'last': 0,
                'bid_qty': 0, 'ask_qty': 0, 'ltp': 0, 'ltp_fresh': False,
                'reliable': False, 'unreliable_reason': f'quote_error: {e}',
                'error': str(e)}


# ── Zebra metrics ─────────────────────────────────────────────────────────

def _intrinsic(opt_type: str, spot: float, strike: float) -> float:
    """Black-Scholes-free intrinsic value."""
    if opt_type == 'CE':
        return max(0.0, spot - strike)
    return max(0.0, strike - spot)


def _extrinsic(opt_type: str, spot: float, strike: float, mid_price: float) -> float:
    """Time value = mid - intrinsic. Clamped to >=0 (avoid neg from spread noise)."""
    intr = _intrinsic(opt_type, spot, strike)
    return max(0.0, mid_price - intr)


def analyze(kite, stock: str, direction: str, spot: float,
            *, max_candidates: int = 3,
            target_capital: float = 90000) -> dict:
    """Resolve the expiry, the ATM strike and the ATM book for a signal.

    **The back-ratio pricing this function used to do is DECOMMISSIONED**
    (owner, 2026-08-27). It quoted up to 8 deep-ITM strikes on every triggered
    signal to rank `2*long_mid - short_mid` pairs for a structure that has not
    been traded since 2026-08-12 and has no open position left. Under
    `ENTRY_STRUCTURE == 'bcs'` — now the only value config accepts — the only
    keys any caller reads are `atm_quote`, `atm_strike`, `expiry`, `dte` and
    `lot_size`, all of which cost ONE quote.

    That waste was not theoretical. Kite's quote family allows 1 request per
    second; on 2026-08-27 the per-cycle budget was exceeded all day and at
    14:40 the spot fetch for all 9 open positions was refused with
    `Too many requests`, leaving TP, the spot veto and the expiry nag dark.
    Nine deep-ITM quotes per triggered signal, for a `best` nothing trades,
    were part of what spent it.

    `best` is therefore always None and `candidates` always empty; the
    `back_ratio` key says `'retired'` so a reader sees a decision rather than
    an unexplained absence. ~450 historical records carry the old structure and
    remain fully readable — they store their own strikes and prices, and
    nothing on the read, reporting or close path calls this function.

    Returns:
      {
        'stock': ..., 'direction': 'CE'|'PE', 'spot': ..., 'expiry': ...,
        'dte': ..., 'lot_size': ...,
        'atm_strike': ..., 'k_s_used': ...,
        'atm_quote': {bid, ask, mid, oi, bid_qty, ask_qty},
        'best': None,             # RETIRED — the back ratio is not priced
        'back_ratio': 'retired',
        'candidates': [],         # RETIRED — always empty
      }
    """
    if direction not in ('CE', 'PE'):
        return {'error': f"direction must be CE or PE, got {direction}"}

    _load_options_csv()
    expiry = _pick_expiry(stock)
    if not expiry:
        return {'error': f"No expiry found with {cfg.MIN_DTE}-{cfg.MAX_DTE} DTE for {stock}"}

    dte = (datetime.strptime(expiry, '%Y-%m-%d').date() - _now_ist().date()).days
    chain_at_expiry = _OPTIONS_CACHE.get(stock, {}).get(expiry, {})
    if not chain_at_expiry:
        return {'error': f"No chain in CSV for {stock} {expiry}"}

    strikes = _list_strikes(stock, expiry, direction)
    if len(strikes) < 4:
        return {'error': f"Insufficient strikes for {stock} {expiry} ({direction}): {len(strikes)}"}

    # K_S = ATM (closest strike to spot)
    k_s = min(strikes, key=lambda k: abs(k - spot))

    # ── RETIRED 2026-08-27: the deep-ITM back-ratio candidate loop ──────
    # What stood here quoted up to 8 K_L strikes one at a time (each
    # `_quote_option` is its own kite.quote call, and the quote family is
    # capped at 1 req/s), priced `2*long_mid - short_mid`, ranked the pairs
    # and returned a `best`. Nothing has traded that structure since
    # 2026-08-12 and no back-ratio position is open. It is deleted rather
    # than gated because a dropped thing left wired in stays authoritative —
    # #449 refused a signal on this `best`'s Rs 121,700 debit while the trade
    # actually on the table cost Rs 6,361.
    #
    # Everything below this point is what the BCS path needs, and it is one
    # quote: the ATM book.
    k_s_meta = chain_at_expiry[k_s][direction]
    k_s_q = _quote_option(kite, k_s_meta['tradingsymbol'])
    if k_s_q.get('error') or k_s_q['mid'] <= 0:
        return {'error': f"Bad quote for ATM leg {k_s_meta['tradingsymbol']}: "
                         f"{k_s_q.get('error')}"}
    # A garbage ATM book means there is no tradeable spread at all — the BCS
    # buys this exact strike. Skip (no alert) and let the next 5-min cycle
    # retry once the book forms, rather than alert click-copy prices off an
    # unformed book.
    if not k_s_q.get('reliable', True):
        return {'error': f"Unreliable ATM book {k_s_meta['tradingsymbol']}: "
                         f"{k_s_q.get('unreliable_reason')}"}

    return {
        'stock': stock,
        'direction': direction,
        'spot': spot,
        'expiry': expiry,
        'dte': dte,
        'lot_size': k_s_meta['lot_size'],
        'atm_strike': k_s,
        'k_s_used': k_s,
        # The ATM book, at the top level. A BCS buys this exact strike.
        'atm_quote': _atm_quote(k_s_q),
        # RETIRED. Kept as explicit None + a labelled marker rather than
        # dropped, so a caller that still reads `best` gets "there is no such
        # thing any more" instead of a KeyError that reads like a bug.
        'best': None,
        'back_ratio': BACK_RATIO_RETIRED,
        'candidates': [],
        'all_evaluated': 0,
    }


def _atm_quote(q: dict) -> dict:
    """The ATM leg's book, in the shape analyze_bcs expects.

    `bid_qty`/`ask_qty` are carried DELIBERATELY and must stay carried. This
    projection is the only seam between `analyze()` and `analyze_bcs()`, and
    until 2026-08-30 it dropped them: `analyze_bcs` read
    `atm_quote.get('ask_qty')` and got None on EVERY production entry, so
    `zebra/monitor.py`'s `if long_ask_qty is not None` never built a depth
    dict, `capital.plan` never received one, and the liquidity bound — plus
    the documented `liquidity_unknown -> 1 lot` fallback — was dead code. All
    13 cohort records carry `long_ask_qty_entry: None` because of this line.

    Every test hand-built an `atm_quote` WITH the size keys, so the suite
    could not see it. `test_the_atm_projection_carries_size` pins the seam by
    feeding `_quote_option`'s real output through this projection.
    """
    return {k: q.get(k)
            for k in ('bid', 'ask', 'mid', 'oi', 'bid_qty', 'ask_qty')}


def analyze_bcs(kite, stock: str, direction: str, spot: float,
                target_spot: float, expiry: str,
                atm_strike: float, atm_quote: dict,
                lot_size: int) -> dict:
    """Build the BCS pair for a triggered zebra signal.

    Dual role, by `cfg.ENTRY_STRUCTURE`: when it is `'bcs'` (the default, and
    the pipeline since 2026-08-12) the caller (`_enter_as_bcs` in
    `zebra/monitor.py`) opens THIS as the first-class, only position for the
    signal -- not a shadow. When it is `'zebra'` the back-ratio pair from
    `analyze()` is what opens, and this is called separately to record a
    passive measurement companion (a "shadow") that never places an order.
    Same function either way; only what the caller does with the result
    differs.

    BUY 1× ATM (the same strike/quote as the zebra short leg — passed in so
    both structures share one snapshot) + SELL 1× the strike nearest the ST
    target, forced at least one strike beyond ATM in the trade direction.

    `atm_quote` needs bid/ask/mid/oi keys, and SHOULD carry bid_qty/ask_qty
    (the zebra analyzer's short-leg quote, as projected by `_atm_quote`).
    Without the size keys the sizing plan silently loses its depth bound. Returns {'error': ...} when no viable target leg exists — the
    caller skips the shadow, never blocks the zebra flow.

    Two HARD gates (2026-08-10, from the 25-trade shadow study). Both return
    an error rather than a warning, so an unclean signal is SUPPRESSED
    instead of being alerted with a ⚠ nobody reads:
      1. OI >= cfg.MIN_LEG_OI on BOTH legs  — the COCHINSHIP/NHPC failure
      2. debit <= cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT of width  — payoff floor
    Together they rejected 32% of the closed sample, and that rejected third
    ran 37.5% WR / -22.9% ROC / PF 0.27 — it lost money outright.
    """
    # ── HARD GATE 0: is the chain we are about to size from CURRENT? ─────
    # First, before a single quote is spent, because a stale file makes every
    # answer below untrustworthy rather than merely unattractive. An `error`
    # like the gates beneath it, so the signal is SUPPRESSED rather than
    # alerted with a warning nobody reads - but unlike them this is an
    # OPERATIONAL fault, not an unclean signal, so `zebra/monitor.py` also
    # alerts on it once a day. Suppressing entries silently until somebody
    # notices would stall the cohort's evidence, which is the one thing the
    # arming gate is waiting on.
    stale, why = options_csv_stale()
    if stale:
        return {'error': why}

    _load_options_csv()
    strikes = _list_strikes(stock, expiry, direction)
    if direction == 'CE':
        beyond = sorted(k for k in strikes if k > atm_strike)
    else:
        beyond = sorted((k for k in strikes if k < atm_strike), reverse=True)
    if not beyond:
        return {'error': f"no strike beyond ATM {atm_strike} for BCS target"}

    # Nearest strike to the ST target, but at least one step beyond ATM
    # (beyond[0]) so the spread always has width.
    k_tgt = min(beyond, key=lambda k: abs(k - target_spot))

    tgt_meta = _OPTIONS_CACHE.get(stock, {}).get(expiry, {}) \
        .get(k_tgt, {}).get(direction)
    if not tgt_meta:
        return {'error': f"no chain entry for target strike {k_tgt}"}
    tgt_q = _quote_option(kite, tgt_meta['tradingsymbol'])
    if tgt_q.get('error') or tgt_q['mid'] <= 0:
        return {'error': f"bad quote for target leg "
                         f"{tgt_meta['tradingsymbol']}: {tgt_q.get('error')}"}
    if not tgt_q.get('reliable', True):
        return {'error': f"unreliable target-leg book "
                         f"{tgt_meta['tradingsymbol']}: {tgt_q.get('unreliable_reason')}"}

    # ── HARD GATE 1: liquidity on BOTH legs ──────────────────────────────
    # Was a soft warning until 2026-08-10. It is now a block because the
    # damage is mechanical, not statistical: on an illiquid book the debit
    # stop does not fill where it is set. Across the closed shadows, OI-
    # flagged trades overshot the -50% trigger by -22.0 pts (realised
    # -72.0%) against -2.7 pts on clean books. COCHINSHIP collapsed
    # 2.18 -> 0.18 in one session while spot moved the RIGHT way; NHPC cost
    # real money the same way. A missed signal is cheap, this tail is not.
    for leg, oi in (('ATM', atm_quote.get('oi')), ('target', tgt_q.get('oi'))):
        # Kite can ship "oi": null and a caller-built atm_quote may omit the
        # key entirely. Unknown liquidity must fail CLOSED (treat as 0) —
        # never raise, which would misreport the block as 'shadow build
        # failed' instead of the gate reason.
        oi = oi or 0
        if oi < cfg.MIN_LEG_OI:
            return {'error': f"{leg} leg OI {oi:,} < {cfg.MIN_LEG_OI:,} "
                             f"— illiquid book, signal suppressed"}

    # ── FILL basis, not mid (2026-08-12) ─────────────────────────────────
    # You BUY the long at the ASK and SELL the short at the BID. Local BCS
    # rule, CLAUDE.md: "For BUYING use ASK. For SELLING use BID (never LTP)."
    # The zebra ticket always obeyed this; the BCS one quoted mid on both legs
    # — and BCS is now the only structure that trades and the only Telegram
    # voice, so the ticket quoted a debit nobody could fill at. The number
    # anchors everything downstream: max_gain, the debit-SL, both trail levels.
    # On the widest book `_leg_reliable` admits (25% of mid per leg) a spread
    # reading 32% d/w fills at 48% — past its own cap, a quarter of the max
    # gain gone before the position exists.
    long_ask = atm_quote.get('ask') or 0
    short_bid = tgt_q.get('bid') or 0
    if long_ask <= 0 or short_bid <= 0:
        return {'error': f"no two-way book to price the fill "
                         f"(long ask {long_ask}, short bid {short_bid})"}
    debit = round(long_ask - short_bid, 2)
    # The same spread at fair value. Kept, and gated on, because the d/w cap
    # below was CALIBRATED on this basis and cannot be re-derived: entry books
    # were never persisted, so all 42 historical records carry mid-basis d/w
    # only.
    #
    # This used to end "moving the cap onto the fill basis would silently
    # change selectivity by an unmeasurable amount". It was MEASURED on
    # 2026-09-03: across all 19 cohort entries it changes exactly ONE
    # (SAGILITY #469, mid 44.5% -> fill 46.5%). The fill basis is now ALSO
    # capped, at HARD GATE 4 below -- the mid test is kept for the
    # calibration reason above, and both use the same threshold.
    debit_mid = round(atm_quote['mid'] - tgt_q['mid'], 2)
    if debit <= 0:
        return {'error': f"non-positive BCS debit {debit} "
                         f"(long ask {long_ask} vs short bid {short_bid})"}
    # The mid basis needs the same check, and for a sharper reason than
    # symmetry: BOTH gates below divide by a quantity derived from debit_mid,
    # so a broken mid book does not merely mis-measure, it makes the gates
    # MORE PERMISSIVE. At debit_mid = -0.2 the entry-cost denominator
    # (width - debit_mid) inflates and an identical true cost reads 14.3%
    # PASS where a healthy book reads 16.7% BLOCK; the d/w gate passes
    # trivially at -5%. A gate that relaxes as its input degrades is worse
    # than no gate, so an unusable mid fails CLOSED like every other one.
    if debit_mid <= 0:
        return {'error': f"non-positive BCS mid debit {debit_mid} "
                         f"(long mid {atm_quote['mid']} vs short mid "
                         f"{tgt_q['mid']}) — both entry gates are denominated "
                         f"in it, so a broken mid book would relax them"}

    width = abs(k_tgt - atm_strike)
    long_ext = _extrinsic(direction, spot, atm_strike, atm_quote['mid'])
    short_ext = _extrinsic(direction, spot, k_tgt, tgt_q['mid'])
    tgt_spread_pct = ((tgt_q['ask'] - tgt_q['bid']) / tgt_q['mid']) \
        if tgt_q['mid'] > 0 else 1.0
    debit_to_width_pct = round(debit / width * 100, 1) if width else 0
    debit_to_width_pct_mid = round(debit_mid / width * 100, 1) if width else 0
    # What the book charges just to open, denominated in the payoff it eats.
    entry_cost = round(debit - debit_mid, 2)
    max_gain_mid = width - debit_mid
    entry_cost_pct = round(entry_cost / max_gain_mid * 100, 1) \
        if max_gain_mid > 0 else 999.0

    # ── HARD GATE 2: debit as a share of width ───────────────────────────
    # See cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT for the full rationale. Short
    # version: width is pinned at ~3.8% of spot by the ST-magnet distance,
    # so d/w is the market's own probability quote. Past ~45% the payoff is
    # priced out — that band ran PF 0.24 while still winning 50% of the
    # time, i.e. it is a payoff problem, not a hit-rate problem.
    # Evaluated on the MID basis — the one it was fitted on. See debit_mid.
    # The FILL basis is ALSO capped, but further down, after the entry-cost
    # gate: see "HARD GATE 4" for why the order matters.
    if debit_to_width_pct_mid > cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT:
        return {'error': f"debit {debit_mid:g} (mid) is "
                         f"{debit_to_width_pct_mid:.1f}% of width {width:g} "
                         f"(cap {cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT:g}%) "
                         f"— no payoff left, signal suppressed"}

    # ── HARD GATE 3: what the book charges to get in ─────────────────────
    # The principled replacement for the raw bid-ask cap dropped on
    # 2026-08-10. That rule was denominated in RUPEES per leg, fired on 68% of
    # shadows and carried no signal (58.8% WR flagged vs 62.5% clean) — its
    # only effect was to train the reader to ignore the ⚠. This one is
    # denominated in the PAYOFF, which is the same logic gate 2 already uses:
    # a spread whose entry cost eats a fifth of the max gain has the payoff
    # priced out just as surely as a rich debit does, and until the fill-basis
    # pricing above there was nothing measuring it at all — `_leg_reliable`
    # (≤25% of mid per leg) is a garbage-print detector, not a tradeability
    # gate.
    #
    # UNCALIBRATED. The threshold is reasoned, not fitted: no historical
    # record persisted its entry books, so the rejection rate on the existing
    # 42 is unmeasurable. Entry books are now stored on every BCS record
    # (`pricing_basis: 'fill'`) precisely so this can be fitted later. Review
    # once ~30 fill-basis records exist.
    if entry_cost_pct > cfg.BCS_MAX_ENTRY_COST_PCT:
        return {'error': f"entry cost {entry_cost:g}/sh is {entry_cost_pct:.1f}% "
                         f"of the {max_gain_mid:g} max gain "
                         f"(cap {cfg.BCS_MAX_ENTRY_COST_PCT:g}%) — the book "
                         f"takes too much of the payoff, signal suppressed"}

    # ── HARD GATE 4: the same cap, on the price actually paid ────────────
    # Added 2026-09-03. Gate 2 alone LEAKS: `debit_mid` is not a price anyone
    # transacts at, and the gap between the bases is widest exactly where width
    # is smallest — on a 2-point spread one tick is 2.5% of width. SAGILITY
    # #469 passed gate 2 at mid 44.5% and FILLED at 46.5%, over the cap and
    # live in the book. Across all 19 cohort entries this is the ONLY one it
    # stops, so it closes a real hole without re-cutting the strategy.
    #
    # NOT a re-calibration: the SAME threshold, so the gate still means "45% of
    # width". What changes is that it can no longer be passed on a price the
    # trade will not get — which is only what the PRICING box already says: "a
    # spread that reads 30% at mid and 38% at the touch is a 38% spread."
    #
    # ORDER MATTERS, AND THIS ONE GOES LAST OF THE THREE. A wide book inflates
    # the fill debit, so on a badly-quoted spread this gate fires for a reason
    # that is really the BOOK's, and would mask the entry-cost gate that is
    # denominated for exactly that. `test_a_book_that_eats_the_payoff_is_blocked`
    # pins the distinction: a 25%-wide book must report "entry cost", because
    # that is the complaint that goes away on a better book, whereas a rich
    # mid debit is the one that survives it. Placed here, this gate only ever
    # fires on a spread whose book is already acceptable — i.e. on a genuine
    # over-cap fill, which is the SAGILITY case.
    if debit_to_width_pct > cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT:
        return {'error': f"debit {debit:g} (fill) is "
                         f"{debit_to_width_pct:.1f}% of width {width:g} "
                         f"(cap {cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT:g}%) "
                         f"— no payoff left at the touch, signal suppressed"}

    # ── MEASURED, NOT ENFORCED: the payoff this exit actually pays ───────
    # Every gate above is denominated in the EXPIRY payoff. This engine never
    # collects that: the TP fires when spot reaches the target, the short strike
    # sits AT the target, so exits book with 27-36 DTE left and the spread worth
    # 42-66% of width (mean 55.2% over 12 cohort TPs). The realised gain is
    # therefore an identity in d/w, and the win is capped BEFORE the order goes
    # out. Recorded here so ~30 signals of evidence accrue; it blocks nothing.
    # See cfg.BCS_MIN_GAIN_AT_TP_PCT for why enforcing it today would be
    # premature (3 of 19 cohort entries clear it).
    #
    # PENETRATION-AWARE since 2026-09-06. `k * width` is a pure function of
    # d/w and blind to WHERE the target sits in the spread, so it was
    # systematically optimistic on exactly the trades the gate exists to
    # catch. Model:
    #
    #     V/w = d/w + pen * (k - d/w),   pen = (target - K_long)/width, [0,1]
    #
    # i.e. at pen=0 the spread is worth what you paid (spot has gone nowhere)
    # and at pen=1 it is worth the calibrated k. That makes the projected gain
    # exactly `pen * (k/(d/w) - 1)` — the OLD formula scaled by penetration,
    # and identical to it at pen=1, which is where k was fitted.
    #
    # Checked against the 12 cohort TPs (with the three post-close bookings
    # re-marked at their last executable poll): mean abs error 0.068 of width
    # against 0.082 for the flat model, bias -0.002 against +0.020. The
    # overall gain is modest and the improvement is CONCENTRATED where the
    # gate needs it — LICHSGFIN #439 (pen 0.59) err +0.032 vs flat +0.092,
    # WAAREEENER #449 (pen 0.39) +0.002 vs flat +0.115. Those two are the
    # smallest wins in the book.
    #
    # `_rebuild_against_resolved_tp` now pins the short strike to the resolved
    # target, so pen should sit near 1 on new entries and this correction
    # should rarely bite. It is kept because "should" is not "does": the
    # nearest strike still lands either side of the target, and a projection
    # that silently assumes its own fix worked is how the last one went wrong.
    pen = None
    try:
        if target_spot is not None and width:
            _sign = 1.0 if direction == 'CE' else -1.0
            pen = _sign * (float(target_spot) - float(atm_strike)) / float(width)
            pen = max(0.0, min(1.0, pen))
    except (TypeError, ValueError, ZeroDivisionError):
        pen = None
    # Unknown penetration falls back to the flat model rather than to 0. A 0
    # would read as "this projects a -100% gain" and would_block every signal
    # whose target could not be parsed — the loudest possible answer to the
    # least informative input.
    _dw = debit / width
    proj_value_at_tp = round(
        width * (_dw + (1.0 if pen is None else pen)
                 * (cfg.BCS_TP_VALUE_FRAC_OF_WIDTH - _dw)), 2)
    # No `else 999.0` sentinel. `debit <= 0` already returned an error ~100
    # lines up, so that branch was unreachable -- and had code motion ever
    # revived it, 999.0 reads as a +999% projected gain and sets
    # would_block=False: the most permissive answer on the most broken
    # input. That is the documented "a default that looks like a value"
    # shape. If the guard above ever moves, this should raise, not invent.
    proj_gain_at_tp_pct = round((proj_value_at_tp / debit - 1) * 100, 1)
    would_block_on_gain_at_tp = proj_gain_at_tp_pct < cfg.BCS_MIN_GAIN_AT_TP_PCT
    if would_block_on_gain_at_tp:
        logger.info(
            "GAIN-AT-TP would-block (MEASURED ONLY, not enforced) %s %s "
            "%g/%g: debit %g (fill, d/w %.1f%%) projects %.1f%% at the TP "
            "against a %.0f%% floor — value at TP assumed %g (k=%.2f x width "
            "%g). Signal NOT suppressed.",
            stock, direction, atm_strike, k_tgt, debit, debit_to_width_pct,
            proj_gain_at_tp_pct, cfg.BCS_MIN_GAIN_AT_TP_PCT,
            proj_value_at_tp, cfg.BCS_TP_VALUE_FRAC_OF_WIDTH, width)

    # `target_spread>2%` was dropped here on 2026-08-10. It fired on 17 of 25
    # closed shadows (68%) and carried no signal whatsoever — 58.8% WR flagged
    # vs 62.5% clean. Its only real effect was to train the reader to ignore
    # the ⚠ marker, which is how the OI flag on COCHINSHIP got waved through.
    # The measured value still ships below as `short_spread_pct` for the record.
    # The list stays (empty) so the stored schema is stable and there is a home
    # for genuinely informational, non-blocking flags later.
    warnings: list = []

    return {
        'long_strike': atm_strike,
        'short_strike': k_tgt,
        'long_symbol': _OPTIONS_CACHE[stock][expiry][atm_strike][direction]['tradingsymbol'],
        'short_symbol': tgt_meta['tradingsymbol'],
        'long_mid': atm_quote['mid'], 'long_bid': atm_quote['bid'],
        'long_ask': atm_quote['ask'], 'long_oi': atm_quote['oi'],
        'short_mid': tgt_q['mid'], 'short_bid': tgt_q['bid'],
        'short_ask': tgt_q['ask'], 'short_oi': tgt_q['oi'],
        # SIZE at the touch, not just the price. `_quote_option` has always
        # returned these and this dict has always dropped them, so every
        # consumer downstream -- the sizing plan, the vetting agent, the
        # ticket -- could see WHAT the book was quoting but not HOW MUCH of
        # it. OI is a different measurement: it says a strike is traded, not
        # that there are contracts on the touch right now.
        #
        # Entry buys the long (needs size on the ASK) and sells the short
        # (needs size on the BID), so those are the two sides carried.
        'long_ask_qty': atm_quote.get('ask_qty'),
        'short_bid_qty': tgt_q.get('bid_qty'),
        'short_spread_pct': round(tgt_spread_pct * 100, 2),
        'long_extrinsic': round(long_ext, 2),
        'short_extrinsic': round(short_ext, 2),
        'debit': debit,                      # FILL basis: ask(long) - bid(short)
        'debit_mid': debit_mid,              # fair value, the gate's basis
        'entry_cost': entry_cost,            # what the book takes to open
        'entry_cost_pct': entry_cost_pct,    # ...as a share of max gain at mid
        'pricing_basis': 'fill',
        'width': width,
        'max_profit_per_share': round(width - debit, 2),
        'debit_to_width_pct': debit_to_width_pct,          # matches `debit`
        'debit_to_width_pct_mid': debit_to_width_pct_mid,  # matches `debit_mid`
        # MEASURED, NOT ENFORCED. Stamped on the record so the would-block
        # population can be scored against the would-pass one at ~30 closes.
        # `k` travels with the numbers because it is provisional: re-deriving
        # it later must not silently re-interpret rows written under the old
        # value. See cfg.BCS_MIN_GAIN_AT_TP_PCT.
        'proj_value_at_tp': proj_value_at_tp,
        'proj_gain_at_tp_pct': proj_gain_at_tp_pct,
        # BOTH inputs travel, not just k. The flag is a comparison of two
        # numbers; stamping only one of them still lets a later threshold move
        # re-label history, which is the exact failure the k stamp exists to
        # prevent.
        'tp_value_frac_of_width_k': cfg.BCS_TP_VALUE_FRAC_OF_WIDTH,
        'min_gain_at_tp_pct_at_entry': cfg.BCS_MIN_GAIN_AT_TP_PCT,
        # THE THIRD input, added with the penetration model. Without it a
        # stored `proj_gain_at_tp_pct` cannot be re-derived at all: k and the
        # floor travel, but the term that scales them did not, so every row
        # written under the flat model reads as pen=1 whether it was or not.
        # None means the target could not be parsed and the flat model was
        # used — recorded as None rather than 1.0 so the two are not confused.
        'tp_penetration': None if pen is None else round(pen, 3),
        'would_block_on_gain_at_tp': would_block_on_gain_at_tp,
        'lot_size': lot_size,
        'warnings': warnings,
    }

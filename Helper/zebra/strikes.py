"""Zebra strike analyzer — pulls live option chain, returns 2-3 valid (K_L, K_S) pairs.

For CE-Zebra (bullish): K_L deep ITM (below spot), K_S near ATM at spot.
For PE-Zebra (bearish): K_L deep ITM (above spot), K_S near ATM at spot.

Rules from PLAYBOOK.md:
  - K_S is closest strike to spot (ATM).
  - K_L 5-10% ITM for high-IV F&O names.
  - NetExt = 2 × long_extrinsic - short_extrinsic. Target ≤ 0 (theta-neutral or positive).
  - BE within ±0.5% of spot (≤1% acceptable).
  - OI ≥ 5,000 per leg.
  - Bid-ask spread < 1% of leg price.
  - DTE 15-45.
  - Capital ₹75k-₹1L.

Returns a sorted list of candidate dicts ordered by NetExt (lowest first).
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
        if ltt_dt.date() != datetime.now().date():
            return False
        return (datetime.now() - ltt_dt).total_seconds() <= _LTP_FRESH_SEC
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


def _load_options_csv() -> None:
    """Load nse_stocks_options.csv into the in-memory chain map."""
    global _OPTIONS_CACHE, _OPTIONS_CACHE_LOADED
    if _OPTIONS_CACHE_LOADED:
        return

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
    logger.info("Options CSV loaded: %d rows, %d stocks", rows, len(_OPTIONS_CACHE))


def _pick_expiry(stock: str, today: Optional[datetime] = None) -> Optional[str]:
    """Pick first expiry with DTE >= MIN_DTE (and <= MAX_DTE).

    Returns the expiry string (YYYY-MM-DD) or None.
    """
    _load_options_csv()
    today = today or datetime.now()
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


def _breakeven(opt_type: str, k_l: float, k_s: float, debit: float) -> dict:
    """Compute BE per Zebra math. Returns {'be': float, 'regime': str, 'be_pct_from_spot': float|None}."""
    width = k_s - k_l if opt_type == 'CE' else k_l - k_s
    if opt_type == 'CE':
        if debit <= 2 * width:
            be = k_l + debit / 2.0
            regime = 'good'
        else:
            be = 2 * k_l - k_s + debit
            regime = 'sub_optimal'
    else:  # PE-mirror: K_L > K_S (long deeper ITM = higher strike)
        if debit <= 2 * width:
            be = k_l - debit / 2.0
            regime = 'good'
        else:
            be = 2 * k_l - k_s - debit
            regime = 'sub_optimal'
    return {'be': round(be, 2), 'regime': regime, 'width': abs(width)}


def analyze(kite, stock: str, direction: str, spot: float,
            *, max_candidates: int = 3,
            target_capital: float = 90000) -> dict:
    """Pull option chain, evaluate Zebra pairs, return ranked candidates.

    Returns:
      {
        'stock': ..., 'direction': 'CE'|'PE', 'spot': ..., 'expiry': ...,
        'dte': ..., 'lot_size': ...,
        'atm_strike': ..., 'k_s_used': ...,
        'candidates': [
          {
            'k_l': float, 'k_s': float, 'width': float,
            'long_symbol': str, 'short_symbol': str,
            'long_mid': float, 'long_ask': float, 'long_bid': float,
            'short_mid': float, 'short_bid': float, 'short_ask': float,
            'long_oi': int, 'short_oi': int,
            'long_spread_pct': float, 'short_spread_pct': float,
            'long_intrinsic': float, 'long_extrinsic': float,
            'short_intrinsic': float, 'short_extrinsic': float,
            'debit': float,                  # 2*long_ask - 1*short_bid
            'net_ext': float,                # 2*long_ext - short_ext
            'be': float, 'be_pct_from_spot': float,
            'regime': 'good'|'sub_optimal',
            'capital_per_lot': float,
            'lots': int,                     # how many fit in target_capital
            'liquidity_ok': bool,
            'gate_fails': [str],
          }, ...
        ],
        'note': str  (any high-level diagnostic)
      }
    """
    if direction not in ('CE', 'PE'):
        return {'error': f"direction must be CE or PE, got {direction}"}

    _load_options_csv()
    expiry = _pick_expiry(stock)
    if not expiry:
        return {'error': f"No expiry found with {cfg.MIN_DTE}-{cfg.MAX_DTE} DTE for {stock}"}

    dte = (datetime.strptime(expiry, '%Y-%m-%d').date() - datetime.now().date()).days
    chain_at_expiry = _OPTIONS_CACHE.get(stock, {}).get(expiry, {})
    if not chain_at_expiry:
        return {'error': f"No chain in CSV for {stock} {expiry}"}

    strikes = _list_strikes(stock, expiry, direction)
    if len(strikes) < 4:
        return {'error': f"Insufficient strikes for {stock} {expiry} ({direction}): {len(strikes)}"}

    # K_S = ATM (closest strike to spot)
    k_s = min(strikes, key=lambda k: abs(k - spot))

    # For CE-Zebra: K_L < K_S (deeper ITM = lower strike)
    # For PE-Zebra: K_L > K_S (deeper ITM = higher strike)
    if direction == 'CE':
        k_l_candidates = sorted([k for k in strikes if k < k_s], reverse=True)
    else:
        k_l_candidates = sorted([k for k in strikes if k > k_s])

    if len(k_l_candidates) < 1:
        return {'error': f"No ITM K_L candidates for {stock} {direction} (K_S={k_s})"}

    # Get K_S quote once
    k_s_meta = chain_at_expiry[k_s][direction]
    k_s_q = _quote_option(kite, k_s_meta['tradingsymbol'])
    if k_s_q.get('error') or k_s_q['mid'] <= 0:
        return {'error': f"Bad quote for short leg {k_s_meta['tradingsymbol']}: {k_s_q.get('error')}"}
    # ATM book is shared by every candidate — if it's garbage there is no
    # tradeable pair. Skip (no alert) and let the next 5-min cycle retry once
    # the book forms, rather than alert click-copy prices off an unformed book.
    if not k_s_q.get('reliable', True):
        return {'error': f"Unreliable short-leg book {k_s_meta['tradingsymbol']}: "
                         f"{k_s_q.get('unreliable_reason')}"}

    short_int = _intrinsic(direction, spot, k_s)
    short_ext = _extrinsic(direction, spot, k_s, k_s_q['mid'])
    short_spread_pct = ((k_s_q['ask'] - k_s_q['bid']) / k_s_q['mid']) if k_s_q['mid'] > 0 else 1.0

    lot_size = k_s_meta['lot_size']

    candidates = []
    # Try up to 8 K_L candidates (deeper ITM = more depth options)
    for k_l in k_l_candidates[:8]:
        leg_meta = chain_at_expiry.get(k_l, {}).get(direction)
        if not leg_meta:
            continue
        long_q = _quote_option(kite, leg_meta['tradingsymbol'])
        if long_q.get('error') or long_q['mid'] <= 0:
            continue
        # Never price a candidate off a garbage ITM book (the false-SL leg).
        if not long_q.get('reliable', True):
            logger.info("Skip K_L=%s for %s: unreliable book (%s)",
                        k_l, stock, long_q.get('unreliable_reason'))
            continue

        long_int = _intrinsic(direction, spot, k_l)
        long_ext = _extrinsic(direction, spot, k_l, long_q['mid'])
        long_spread_pct = ((long_q['ask'] - long_q['bid']) / long_q['mid']) if long_q['mid'] > 0 else 1.0

        # Debit = 2 × long_ask - 1 × short_bid (worst-case entry pricing)
        # But for analysis display, use mids — fill prices come at place time.
        debit = round(2.0 * long_q['mid'] - 1.0 * k_s_q['mid'], 2)
        if debit <= 0:
            # Theta-positive credit Zebra is rare; skip if negative debit (suggests data noise)
            continue

        net_ext = round(2.0 * long_ext - short_ext, 2)
        be_info = _breakeven(direction, k_l, k_s, debit)
        be = be_info['be']
        be_pct_from_spot = round((be - spot) / spot * 100, 2)
        capital_per_lot = round(debit * lot_size, 2)
        # Suggest 1 lot by default. User can scale via --lots N at zebra enter.
        lots = 1

        # Gates
        gate_fails = []
        if long_q['oi'] < cfg.MIN_LEG_OI:
            gate_fails.append(f'long_OI<{cfg.MIN_LEG_OI}')
        if k_s_q['oi'] < cfg.MIN_LEG_OI:
            gate_fails.append(f'short_OI<{cfg.MIN_LEG_OI}')
        if long_spread_pct > cfg.MAX_LEG_SPREAD_PCT:
            gate_fails.append(f'long_spread>{cfg.MAX_LEG_SPREAD_PCT*100:.0f}%')
        if short_spread_pct > cfg.MAX_LEG_SPREAD_PCT:
            gate_fails.append(f'short_spread>{cfg.MAX_LEG_SPREAD_PCT*100:.0f}%')
        if be_info['regime'] == 'sub_optimal':
            gate_fails.append('BE>K_S')

        liquidity_ok = not any(g.endswith('_OI<5000') or g.startswith('long_spread')
                               or g.startswith('short_spread') for g in gate_fails)

        candidates.append({
            'k_l': k_l, 'k_s': k_s, 'width': abs(k_s - k_l),
            'long_symbol': leg_meta['tradingsymbol'],
            'short_symbol': k_s_meta['tradingsymbol'],
            'long_mid': long_q['mid'], 'long_bid': long_q['bid'], 'long_ask': long_q['ask'],
            'short_mid': k_s_q['mid'], 'short_bid': k_s_q['bid'], 'short_ask': k_s_q['ask'],
            'long_oi': long_q['oi'], 'short_oi': k_s_q['oi'],
            'long_spread_pct': round(long_spread_pct * 100, 2),
            'short_spread_pct': round(short_spread_pct * 100, 2),
            'long_intrinsic': round(long_int, 2),
            'long_extrinsic': round(long_ext, 2),
            'short_intrinsic': round(short_int, 2),
            'short_extrinsic': round(short_ext, 2),
            'debit': debit,
            'net_ext': net_ext,
            'be': be,
            'be_pct_from_spot': be_pct_from_spot,
            'regime': be_info['regime'],
            'capital_per_lot': capital_per_lot,
            'lots': lots,
            'lot_size': lot_size,
            'liquidity_ok': liquidity_ok,
            'gate_fails': gate_fails,
        })

    if not candidates:
        return {
            'error': f"No tradeable K_L found for {stock} {direction} {expiry}",
            'stock': stock, 'direction': direction, 'spot': spot,
            'expiry': expiry, 'dte': dte, 'lot_size': lot_size,
            'k_s_used': k_s, 'atm_strike': k_s, 'atm_quote': _atm_quote(k_s_q),
            'candidates': [],
        }

    best = _pick_best(candidates, spot)

    # Full ranked list (best first) — kept for inspection / debugging via
    # the analyze CLI, but the Telegram ENTER alert uses only `best`.
    ranked = sorted(candidates, key=lambda c: (
        len(c['gate_fails']),
        c['net_ext'],
        abs(c['be'] - spot),
    ))

    return {
        'stock': stock,
        'direction': direction,
        'spot': spot,
        'expiry': expiry,
        'dte': dte,
        'lot_size': lot_size,
        'atm_strike': k_s,
        'k_s_used': k_s,
        # The ATM book, at the top level rather than only inside `best`. A BCS
        # buys this exact strike, so tucking the quote inside the zebra's
        # recommended PAIR meant the zebra's own gates (net-extrinsic, deep-ITM
        # liquidity) could veto a perfectly tradeable spread that shares none
        # of those constraints.
        'atm_quote': _atm_quote(k_s_q),
        'best': best,                         # the recommended pair (or None)
        'candidates': ranked[:max_candidates], # for `zebra analyze` inspection
        'all_evaluated': len(candidates),
    }


def _atm_quote(q: dict) -> dict:
    """The ATM leg's book, in the shape analyze_bcs expects."""
    return {k: q.get(k) for k in ('bid', 'ask', 'mid', 'oi')}


def analyze_bcs(kite, stock: str, direction: str, spot: float,
                target_spot: float, expiry: str,
                atm_strike: float, atm_quote: dict,
                lot_size: int) -> dict:
    """Build the shadow BCS pair for a triggered zebra signal.

    BUY 1× ATM (the same strike/quote as the zebra short leg — passed in so
    both structures share one snapshot) + SELL 1× the strike nearest the ST
    target, forced at least one strike beyond ATM in the trade direction.

    `atm_quote` needs bid/ask/mid/oi keys (the zebra analyzer's short-leg
    quote). Returns {'error': ...} when no viable target leg exists — the
    caller skips the shadow, never blocks the zebra flow.

    Two HARD gates (2026-08-10, from the 25-trade shadow study). Both return
    an error rather than a warning, so an unclean signal is SUPPRESSED
    instead of being alerted with a ⚠ nobody reads:
      1. OI >= cfg.MIN_LEG_OI on BOTH legs  — the COCHINSHIP/NHPC failure
      2. debit <= cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT of width  — payoff floor
    Together they rejected 32% of the closed sample, and that rejected third
    ran 37.5% WR / -22.9% ROC / PF 0.27 — it lost money outright.
    """
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
    # only. Moving the cap onto the fill basis would silently change
    # selectivity by an unmeasurable amount.
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
        'lot_size': lot_size,
        'warnings': warnings,
    }


def _pick_best(candidates: list, spot: float) -> Optional[dict]:
    """Pick the single best Zebra pair.

    Order of preference:
      1. ALL gates pass (OI ≥ 5k both legs, spread ≤ 1% both legs, BE inside body).
      2. NetExt ≤ 0 (theta-neutral or positive).
      3. BE within ±0.5% of spot (closest to ideal).
      4. Among the above, prefer LOWEST capital_per_lot (most capital-efficient).

    Each gate is relaxed in turn if no candidate passes; we never return None
    when at least one viable structure exists — fallback is the deepest one
    with smallest gate-fail count.

    The picker balances the user's three asks: least slippage (gate 1 covers
    bid-ask), better OI (gate 1), capital efficient (tiebreaker).
    """
    if not candidates:
        return None

    # Tier 1: clean (no gate fails) + theta-OK + BE near spot
    tier1 = [c for c in candidates
             if not c['gate_fails']
             and c['net_ext'] <= 0
             and abs(c['be_pct_from_spot']) <= 0.5]
    if tier1:
        return min(tier1, key=lambda c: c['capital_per_lot'])

    # Tier 2: clean + theta-OK (BE constraint relaxed)
    tier2 = [c for c in candidates
             if not c['gate_fails']
             and c['net_ext'] <= 0]
    if tier2:
        return min(tier2, key=lambda c: c['capital_per_lot'])

    # Tier 3: clean (theta constraint relaxed)
    tier3 = [c for c in candidates if not c['gate_fails']]
    if tier3:
        return min(tier3, key=lambda c: (c['net_ext'], c['capital_per_lot']))

    # Tier 4: ranked best-effort (gate fails allowed)
    ranked = sorted(candidates, key=lambda c: (
        len(c['gate_fails']),
        c['net_ext'],
        c['capital_per_lot'],
    ))
    return ranked[0]

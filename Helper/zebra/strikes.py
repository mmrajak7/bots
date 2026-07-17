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
    """Get bid/ask/OI for an NFO option. Returns {bid, ask, mid, oi, error}."""
    key = f"NFO:{tradingsymbol}"
    try:
        q = kite.quote([key])[key]
        depth = q.get('depth', {})
        buy = depth.get('buy', [])
        sell = depth.get('sell', [])
        bid = buy[0]['price'] if buy else 0.0
        ask = sell[0]['price'] if sell else 0.0
        oi = q.get('oi', 0)
        mid = round((bid + ask) / 2, 2) if (bid > 0 and ask > 0) else q.get('last_price', 0)
        return {'bid': bid, 'ask': ask, 'mid': mid, 'oi': oi,
                'last': q.get('last_price', 0)}
    except Exception as e:
        return {'bid': 0, 'ask': 0, 'mid': 0, 'oi': 0, 'error': str(e)}


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
            'k_s_used': k_s, 'candidates': [],
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
        'best': best,                         # the recommended pair (or None)
        'candidates': ranked[:max_candidates], # for `zebra analyze` inspection
        'all_evaluated': len(candidates),
    }


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

    debit = round(atm_quote['mid'] - tgt_q['mid'], 2)
    if debit <= 0:
        return {'error': f"non-positive BCS debit {debit} "
                         f"(ATM {atm_quote['mid']} vs target {tgt_q['mid']})"}

    width = abs(k_tgt - atm_strike)
    long_ext = _extrinsic(direction, spot, atm_strike, atm_quote['mid'])
    short_ext = _extrinsic(direction, spot, k_tgt, tgt_q['mid'])
    tgt_spread_pct = ((tgt_q['ask'] - tgt_q['bid']) / tgt_q['mid']) \
        if tgt_q['mid'] > 0 else 1.0

    warnings = []
    if tgt_q['oi'] < cfg.MIN_LEG_OI:
        warnings.append(f'target_OI<{cfg.MIN_LEG_OI}')
    if tgt_spread_pct > cfg.MAX_LEG_SPREAD_PCT:
        warnings.append(f'target_spread>{cfg.MAX_LEG_SPREAD_PCT*100:.0f}%')

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
        'debit': debit,
        'width': width,
        'max_profit_per_share': round(width - debit, 2),
        'debit_to_width_pct': round(debit / width * 100, 1) if width else 0,
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

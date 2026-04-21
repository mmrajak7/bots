"""Magnet monitor — price watching, entry/exit lifecycle, Telegram alerts.

Responsibilities:
1. Poll LTP for 'watching' signals — enter when gap shrinks to ≤2%
2. Poll LTP for 'entered' trades — exit on TP (spot crosses ST) / SL / time
3. Send Telegram alerts on state changes
4. Orchestrate the full run loop (scan every 5 min + monitor every 30s)
"""

import json
import logging
import math
import time
from datetime import datetime, timedelta

import pytz
import requests

from . import config as cfg
from .scanner import _get_kite, get_ltp, validate_and_add_signals
from .trade_store import get_store

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# Peak premiums + entry retries now persisted in trade dict (survive restarts).
# See trade_store.update_peak_premium() and update_entry_retries().
_expiry_warned = set()  # {trade_id} — dedup expiry-day midday alerts (ok to reset)


# ── NFO Instrument Cache ──────────────────────────────────────────────────
_nfo_instruments = None


def _get_nfo_instruments(kite):
    """Load and cache NFO instrument list (once per session)."""
    global _nfo_instruments
    if _nfo_instruments is None:
        logger.info("Loading NFO instrument list (one-time)...")
        _nfo_instruments = kite.instruments('NFO')
        _build_tick_cache()
        logger.info("Cached %d NFO instruments, %d tick sizes",
                     len(_nfo_instruments), len(_tick_cache))
    return _nfo_instruments


# ── Telegram ──────────────────────────────────────────────────────────────

_telegram_cfg = None
_telegram_cfg_loaded = False


def send_telegram(msg: str):
    """Send Telegram alert with HTML parse mode. Best-effort: never blocks or crashes."""
    global _telegram_cfg, _telegram_cfg_loaded

    try:
        if not _telegram_cfg_loaded:
            if cfg.TELEGRAM_CONFIG.exists():
                with open(cfg.TELEGRAM_CONFIG) as f:
                    _telegram_cfg = json.load(f)
            _telegram_cfg_loaded = True

        if not _telegram_cfg:
            return

        requests.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        logger.debug("Telegram send failed: %s", e)


def _tf_tag(timeframe: str) -> str:
    """Short timeframe tag: [M] [W] [D]."""
    return {'monthly': '[M]', 'weekly': '[W]', 'daily': '[D]'}.get(timeframe, '[?]')


def _dir_icon(direction: str) -> str:
    """Red circle for PE (put), green for CE (call)."""
    return '\U0001f534' if direction == 'PE' else '\U0001f7e2'  # red/green circle


# ── Market Hours ──────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """Check if current time is within market hours (9:15-15:30 IST, Mon-Fri)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    market_open = now.replace(
        hour=cfg.MARKET_OPEN[0], minute=cfg.MARKET_OPEN[1], second=0, microsecond=0
    )
    market_close = now.replace(
        hour=cfg.MARKET_CLOSE[0], minute=cfg.MARKET_CLOSE[1], second=0, microsecond=0
    )
    return market_open <= now <= market_close


# ── Bid-Ask Pricing ──────────────────────────────────────────────────────

def get_option_quote(kite, symbol: str) -> dict:
    """Get full quote for an option: bid, ask, OI, volume, depth qty."""
    try:
        data = kite.quote([f"NFO:{symbol}"])
        q = list(data.values())[0]
        depth = q.get('depth', {})
        buyers = depth.get('buy', [])
        sellers = depth.get('sell', [])
        bid = buyers[0]['price'] if buyers and buyers[0].get('price', 0) > 0 else 0
        ask = sellers[0]['price'] if sellers and sellers[0].get('price', 0) > 0 else 0
        bid_qty = buyers[0].get('quantity', 0) if buyers else 0
        ask_qty = sellers[0].get('quantity', 0) if sellers else 0
        ltp = q.get('last_price', 0)
        spread = round(ask - bid, 2) if bid > 0 and ask > 0 else 0
        oi = q.get('oi', 0)
        volume = q.get('volume', 0)
        return {
            'bid': bid, 'ask': ask, 'ltp': ltp, 'spread': spread,
            'bid_qty': bid_qty, 'ask_qty': ask_qty,
            'oi': oi, 'volume': volume,
        }
    except Exception:
        return {'bid': 0, 'ask': 0, 'ltp': 0, 'spread': 0,
                'bid_qty': 0, 'ask_qty': 0, 'oi': 0, 'volume': 0}


def check_synthetic_viable(kite, option_symbol: str, direction: str,
                           long_ask: float = 0,
                           strike: float = 0, lot_size: int = 0,
                           spot: float = 0) -> dict:
    """Check if a synthetic future at the same strike is viable, and compute
    execution-ready numbers so the trader can optionally run synth alongside
    the naked option.

    CE magnet (expecting rally):  synthetic LONG  = buy CE + sell PE
    PE magnet (expecting decline): synthetic SHORT = buy PE + sell CE

    Liquidity filter (opposite leg): OI >= 50K, spread < 2% of ask.
    """
    try:
        if direction == 'CE' and option_symbol.endswith('CE'):
            opp_symbol = option_symbol[:-2] + 'PE'
            opp_type = 'PE'
            synth_type = 'LONG'
        elif direction == 'PE' and option_symbol.endswith('PE'):
            opp_symbol = option_symbol[:-2] + 'CE'
            opp_type = 'CE'
            synth_type = 'SHORT'
        else:
            return {'viable': False, 'reason': 'symbol suffix mismatch'}

        # Single batched quote for both legs (round-trip cost view)
        both = kite.quote([f"NFO:{option_symbol}", f"NFO:{opp_symbol}"])
        long_q = both.get(f"NFO:{option_symbol}", {}) or {}
        opp_q = both.get(f"NFO:{opp_symbol}", {}) or {}

        def _bid_ask(q):
            depth = q.get('depth', {}) or {}
            buy = depth.get('buy') or [{}]
            sell = depth.get('sell') or [{}]
            b = buy[0].get('price', 0) if buy else 0
            a = sell[0].get('price', 0) if sell else 0
            return b, a

        long_bid, _ = _bid_ask(long_q)
        bid, ask = _bid_ask(opp_q)
        oi = opp_q.get('oi', 0)

        if ask <= 0 or bid <= 0:
            return {'viable': False, 'reason': f'{opp_type} no bid/ask',
                    'opp_symbol': opp_symbol, 'opp_oi': oi}
        opp_spread_pct = (ask - bid) / ask if ask > 0 else 1.0
        viable = oi >= 50_000 and opp_spread_pct < 0.02

        # Long-leg spread (round-trip view — select_option confirmed liquid
        # at entry, but worth showing alongside the short leg).
        long_spread_pct = ((long_ask - long_bid) / long_ask
                           if long_ask and long_bid else 0)

        # Execution-ready calcs. Net "debit" = cash out if positive, credit if negative.
        # Synth: pay long-leg ask, receive opposite-leg bid.
        net_debit = round(long_ask - bid, 2) if long_ask else None
        notional = round(strike * lot_size, 0) if (strike and lot_size) else None
        # SPAN + exposure for stock F&O: typically 18-24% (large-cap), up to
        # 40%+ for volatile mid-caps. Show as range, not point estimate, to
        # avoid giving the trader false precision.
        margin_low = round(notional * 0.18, 0) if notional else None
        margin_high = round(notional * 0.40, 0) if notional else None
        cash_per_lot = (round(net_debit * lot_size, 0)
                        if (net_debit is not None and lot_size) else None)

        return {
            'viable': viable,
            'synth_type': synth_type,          # 'LONG' or 'SHORT'
            'opp_symbol': opp_symbol,
            'opp_type': opp_type,
            'opp_bid': bid,
            'opp_ask': ask,
            'opp_oi': oi,
            'opp_spread_pct': round(opp_spread_pct * 100, 2),
            'long_spread_pct': round(long_spread_pct * 100, 2),
            'net_debit': net_debit,
            'notional': notional,
            'margin_low': margin_low,
            'margin_high': margin_high,
            'cash_per_lot': cash_per_lot,
            'reason': ('OK' if viable
                       else f'OI={oi:,} sprd={opp_spread_pct*100:.1f}%'),
        }
    except Exception as e:
        return {'viable': False, 'reason': f'error: {e}'}


def check_option_liquidity(quote: dict, premium: float) -> tuple:
    """Check if option is liquid enough for entry.

    Returns (is_liquid, reason).
    Rules:
    - Must have both BID and ASK (no one-sided market)
    - Spread < Rs 2.00 or < 10% of premium (whichever is larger)
    - OI > 500 contracts (some open interest exists)
    - ASK qty > 0 (sellers available)
    """
    bid = quote.get('bid', 0)
    ask = quote.get('ask', 0)
    spread = quote.get('spread', 0)
    oi = quote.get('oi', 0)
    ask_qty = quote.get('ask_qty', 0)

    if bid <= 0 or ask <= 0:
        return False, "no bid or ask"

    if ask_qty <= 0:
        return False, "no sellers at ask"

    # Spread check: absolute Rs 2.00 cap OR 10% of premium
    max_spread = max(2.00, premium * 0.10) if premium > 0 else 2.00
    if spread > max_spread:
        return False, f"spread Rs {spread:.2f} > limit Rs {max_spread:.2f}"

    if oi < 500:
        return False, f"OI {oi} < 500 (illiquid contract)"

    return True, f"liquid (spread={spread:.2f}, OI={oi}, ask_qty={ask_qty})"


_tick_cache: dict = {}  # {symbol: tick_size} — populated from NFO instruments


def _get_tick_size(symbol: str) -> float:
    """Get tick size from cache. O(1) lookup. Default 0.05 for NSE options."""
    return _tick_cache.get(symbol, 0.05)


def _build_tick_cache():
    """Populate tick cache from NFO instruments (called once after load)."""
    if _nfo_instruments:
        for inst in _nfo_instruments:
            sym = inst.get('tradingsymbol', '')
            if sym:
                _tick_cache[sym] = inst.get('tick_size', 0.05)


def _round_up_tick(price: float, tick: float) -> float:
    """Round price UP to nearest tick (for buying)."""
    return round(math.ceil(price / tick) * tick, 2)


def _round_down_tick(price: float, tick: float) -> float:
    """Round price DOWN to nearest tick (for selling)."""
    return round(math.floor(price / tick) * tick, 2)


def get_buy_price(kite, symbol: str) -> float:
    """ASK + 1 tick — what we actually pay to buy.

    Uses tick size from instruments (typically Rs 0.05 for options).
    Returns 0 if no ASK available (do NOT fallback to stale LTP).
    """
    q = get_option_quote(kite, symbol)
    ask = q['ask']
    if ask <= 0:
        return 0  # no sellers — caller must handle (skip entry/exit)
    tick = _get_tick_size(symbol)
    return _round_up_tick(ask + tick, tick)


def get_sell_price(kite, symbol: str) -> float:
    """BID - 1 tick — what we actually receive selling.

    Uses tick size from instruments (typically Rs 0.05 for options).
    Returns 0 if no BID available (do NOT fallback to stale LTP).
    """
    q = get_option_quote(kite, symbol)
    bid = q['bid']
    if bid <= 0:
        return 0  # no buyers — caller must handle (skip exit, retry)
    tick = _get_tick_size(symbol)
    return _round_down_tick(bid - tick, tick)


# ── Spot OHLC ────────────────────────────────────────────────────────────

def get_spot_with_recent_range(kite, symbols, instrument_tokens=None):
    """Get LTP + recent HIGH/LOW for spot symbols.

    Uses LTP for current price, then fetches last 5 minute-candles
    to find recent high/low (catches wicks between 30s polls).
    Falls back to LTP-only if minute data unavailable.

    Returns {symbol: {ltp, high, low}}.
    """
    if not symbols:
        return {}

    # Step 1: Get LTP for all
    instruments = [f"NSE:{s}" for s in symbols]
    result = {}
    try:
        ltp_data = kite.ltp(instruments)
        for key, val in ltp_data.items():
            sym = key.replace('NSE:', '')
            ltp = val['last_price']
            result[sym] = {'ltp': ltp, 'high': ltp, 'low': ltp}
    except Exception as e:
        logger.error("LTP fetch failed: %s", e)
        return {}

    # Step 2: Get last 5 minute candles for recent high/low
    from .scanner import get_instrument_token
    now = datetime.now(IST)
    from_time = now - timedelta(minutes=5)

    for sym in symbols:
        try:
            token = get_instrument_token(kite, sym)
            candles = kite.historical_data(
                token, from_time.strftime('%Y-%m-%d %H:%M:%S'),
                now.strftime('%Y-%m-%d %H:%M:%S'), 'minute'
            )
            if candles:
                recent_high = max(c['high'] for c in candles)
                recent_low = min(c['low'] for c in candles)
                result[sym]['high'] = recent_high
                result[sym]['low'] = recent_low
        except Exception:
            pass  # keep LTP as fallback

    return result


# ── Option Selection ──────────────────────────────────────────────────────

def select_option(kite, stock: str, direction: str, target: float) -> dict:
    """Select option strike NEAREST TO TARGET (not ATM).

    target = ST line (where price is heading). Picks strike closest to target so
    the option becomes ATM when the move completes — better leverage than ATM-at-entry.
    Example: spot=1350, target=1380 -> picks 1380 CE (slightly OTM at entry).

    Returns: {strike, symbol, lot_size, premium} or empty dict on failure.

    Reads from nse_stocks_options.csv (CLAUDE.md rule: NEVER construct symbols).
    Falls back to Kite instruments if CSV not available.
    """
    import csv
    option_csv = cfg.PROJECT_ROOT / 'nse_stocks_options.csv'

    option_type = direction  # CE or PE

    if option_csv.exists():
        try:
            with open(option_csv, newline='') as f:
                reader = csv.DictReader(f)
                candidates = []
                for row in reader:
                    if (row.get('stock_symbol', '') == stock
                            and row.get('option_type', '') == option_type):
                        strike = float(row.get('option_strike', 0))
                        candidates.append({
                            'strike': strike,
                            'symbol': row.get('option_tradingsymbol', ''),
                            'lot_size': int(row.get('option_lot_size', 0)),
                            'expiry': row.get('option_expiry', ''),
                        })

                if candidates:
                    # Filter to nearest expiry with >= MIN_DTE days
                    now_dt = datetime.now()
                    today = now_dt.strftime('%Y-%m-%d')
                    min_dte = (now_dt + timedelta(days=cfg.MIN_DTE)).strftime('%Y-%m-%d')
                    valid_expiry = [c for c in candidates if c['expiry'] >= min_dte]
                    if not valid_expiry:
                        valid_expiry = [c for c in candidates if c['expiry'] >= today]
                    if not valid_expiry:
                        logger.warning("No valid expiry for %s %s — CSV may be stale (run kite_nse_options.py)",
                                       stock, option_type)
                        return {}

                    # Group by nearest expiry
                    valid_expiry.sort(key=lambda x: x['expiry'])
                    target_exp = valid_expiry[0]['expiry']
                    same_exp = [c for c in valid_expiry if c['expiry'] == target_exp]

                    # Nearest strike to TARGET (ST line) within same expiry
                    # Strike becomes ATM at target — better leverage than ATM-at-entry
                    same_exp.sort(key=lambda x: abs(x['strike'] - target))

                    for candidate in same_exp[:5]:  # check up to 5 nearest strikes
                        quote = get_option_quote(kite, candidate['symbol'])
                        premium = quote['ask']
                        if premium <= 0:
                            continue

                        # Apply tick-size rounding
                        tick = _get_tick_size(candidate['symbol'])
                        premium = _round_up_tick(premium + tick, tick)

                        # Liquidity check
                        is_liquid, liq_reason = check_option_liquidity(quote, premium)
                        if not is_liquid:
                            logger.info("SKIP %s %s: %s", stock, candidate['symbol'], liq_reason)
                            continue

                        logger.info("SELECT %s: %s (spread=%.2f, OI=%d)",
                                     stock, candidate['symbol'], quote['spread'], quote['oi'])
                        return {
                            'strike': candidate['strike'],
                            'symbol': candidate['symbol'],
                            'lot_size': candidate['lot_size'],
                            'premium': premium,
                            'expiry': candidate['expiry'],
                        }

                    logger.warning("No liquid option for %s %s in %s expiry",
                                   stock, option_type, target_exp)
                    return {}
        except Exception as e:
            logger.warning("CSV option lookup failed: %s", e)

    # Fallback: search Kite instruments (cached)
    try:
        instruments = _get_nfo_instruments(kite)
        now = datetime.now()
        candidates = []
        for inst in instruments:
            if (inst['name'] == stock
                    and inst['instrument_type'] == option_type
                    and inst['expiry'] >= now.date()):
                candidates.append(inst)

        if not candidates:
            return {}

        # Filter to nearest expiry with >=7 DTE
        min_dte = now.date() + timedelta(days=cfg.MIN_DTE)
        near_expiry = [c for c in candidates if c['expiry'] >= min_dte]
        if not near_expiry:
            near_expiry = candidates

        # Nearest expiry
        near_expiry.sort(key=lambda x: x['expiry'])
        target_expiry = near_expiry[0]['expiry']
        same_expiry = [c for c in near_expiry if c['expiry'] == target_expiry]

        # Nearest strike to TARGET (ST line) — try nearest strikes, skip illiquid
        same_expiry.sort(key=lambda x: abs(x['strike'] - target))

        for candidate in same_expiry[:5]:
            sym = candidate['tradingsymbol']
            quote = get_option_quote(kite, sym)
            premium = quote['ask']
            if premium <= 0:
                continue

            tick = _get_tick_size(sym)
            premium = _round_up_tick(premium + tick, tick)

            is_liquid, liq_reason = check_option_liquidity(quote, premium)
            if not is_liquid:
                logger.info("SKIP %s %s: %s", stock, sym, liq_reason)
                continue

            logger.info("SELECT %s: %s (spread=%.2f, OI=%d)",
                         stock, sym, quote['spread'], quote['oi'])
            return {
                'strike': candidate['strike'],
                'symbol': sym,
                'lot_size': candidate['lot_size'],
                'premium': premium,
                'expiry': str(candidate['expiry']),
            }

        logger.warning("No liquid option for %s %s via NFO instruments", stock, option_type)
        return {}

    except Exception as e:
        logger.error("Instrument search failed for %s: %s", stock, e)
        return {}


# ── Monitor: Watching Signals ─────────────────────────────────────────────

def check_watching_signals(store, kite):
    """Check watching signals: if gap shrinks to ≤2%, enter paper trade."""
    watching = store.get_watching()
    if not watching:
        return

    # Capacity check: don't exceed max open trades
    entered_count = len(store.get_entered())
    if entered_count >= cfg.MAX_OPEN_TRADES:
        logger.info("At capacity (%d/%d entered), skipping watching checks",
                     entered_count, cfg.MAX_OPEN_TRADES)
        return

    stocks = list({t['stock'] for t in watching})
    ltps = get_ltp(kite, stocks)

    for trade in watching:
        stock = trade['stock']
        price = ltps.get(stock)
        if not price:
            continue

        st_val = trade['st_value']
        gap = abs(price - st_val) / st_val

        # Too far — still approaching
        if gap > cfg.ENTRY_GAP:
            continue

        # Config-skew sanity floor: cancel entries below 2.5% regardless of
        # runtime config. Backstop against stale magnet_config.json (AUBANK
        # entered at 1.9% because server had entry_gap_min=0.005). This floor
        # is source-code-level — loosen only by editing, not JSON.
        _MIN_ENTRY_GAP_SANITY = 0.025
        if gap < _MIN_ENTRY_GAP_SANITY:
            logger.warning(
                "CANCEL #%d %s: gap %.2f%% below sanity floor %.1f%% "
                "(runtime ENTRY_GAP_MIN=%.1f%%). Check config drift.",
                trade['id'], stock, gap * 100,
                _MIN_ENTRY_GAP_SANITY * 100, cfg.ENTRY_GAP_MIN * 100,
            )
            store.cancel_signal(
                trade['id'],
                f"gap {gap:.2%} below sanity floor {_MIN_ENTRY_GAP_SANITY:.1%}"
            )
            continue

        # Too close — already past entry (shouldn't happen if scanner was right)
        if gap < cfg.ENTRY_GAP_MIN:
            logger.info("Signal #%d %s: gap %.1f%% too narrow, cancelling",
                        trade['id'], stock, gap * 100)
            store.cancel_signal(trade['id'], f"gap narrowed to {gap:.1%}, past entry zone")
            continue

        # === ENTRY ZONE: gap is between ENTRY_GAP_MIN and ENTRY_GAP ===
        logger.info("ENTRY TRIGGER: %s gap=%.1f%% (≤%.1f%%)",
                     stock, gap * 100, cfg.ENTRY_GAP * 100)

        # Select option at TARGET strike (ST line), not ATM
        # Strike becomes ATM at target — better leverage than ATM-at-entry
        target = trade.get('target_spot') or trade['st_value']
        option = select_option(kite, stock, trade['direction'], target)

        if not option or not option.get('symbol'):
            retries = (trade.get('entry_retries') or 0) + 1
            store.update_entry_retries(trade['id'], retries)
            if retries >= cfg.ENTRY_MAX_RETRIES:
                logger.warning("No liquid option for %s after %d attempts, cancelling signal",
                               stock, retries)
                store.cancel_signal(trade['id'], f"no liquid option after {retries} attempts")
            else:
                logger.warning("No option found for %s %s, attempt %d/%d",
                               stock, trade['direction'], retries, cfg.ENTRY_MAX_RETRIES)
            continue

        lot_size = option['lot_size']
        premium = option.get('premium', 0)

        if premium <= 0:
            retries = (trade.get('entry_retries') or 0) + 1
            store.update_entry_retries(trade['id'], retries)
            if retries >= cfg.ENTRY_MAX_RETRIES:
                logger.warning("SKIP ENTRY %s: premium Rs 0 after %d attempts, cancelling",
                               stock, retries)
                store.cancel_signal(trade['id'], f"premium Rs 0 after {retries} attempts")
            else:
                logger.warning("SKIP ENTRY %s: premium is Rs 0 for %s, attempt %d/%d",
                               stock, option.get('symbol', '?'), retries, cfg.ENTRY_MAX_RETRIES)
            continue

        # Validate option expiry is not in the past
        opt_expiry = option.get('expiry', '')
        if opt_expiry:
            try:
                exp_str = str(opt_expiry)[:10]
                from datetime import datetime as _dt
                exp_date = _dt.strptime(exp_str, '%Y-%m-%d')
                today = _dt.now()
                dte = (exp_date - today).days
                if dte < 0:
                    logger.warning("SKIP ENTRY %s: option %s expired (%s, %d days ago)",
                                   stock, option.get('symbol', '?'), exp_str, abs(dte))
                    store.cancel_signal(trade['id'], f"option expired: {exp_str}")
                    continue
                if dte < cfg.MIN_DTE:
                    logger.warning("SKIP ENTRY %s: option %s DTE=%d < min %d",
                                   stock, option.get('symbol', '?'), dte, cfg.MIN_DTE)
                    continue
            except (ValueError, TypeError):
                pass  # can't parse — proceed with entry

        # Fixed lots per trade
        qty = lot_size * cfg.LOTS_PER_TRADE

        # Max capital per trade check (prevents outsized exposure on expensive options)
        capital_at_risk = premium * qty
        max_capital = cfg.MAX_CAPITAL_PER_TRADE
        if max_capital > 0 and capital_at_risk > max_capital:
            logger.warning("SKIP ENTRY %s: capital Rs %,.0f > max Rs %,.0f (premium %.2f x %d qty)",
                           stock, capital_at_risk, max_capital, premium, qty)
            store.cancel_signal(trade['id'],
                                f"capital Rs {capital_at_risk:,.0f} > max Rs {max_capital:,.0f}")
            continue

        # Compute SL spot: price at 5% gap from ST on the same side
        side = trade['side']
        if side == 'above':
            # Price is above ST, we expect decline. SL = ST * 1.05 (price goes further up)
            sl_spot = round(st_val * (1 + cfg.SL_GAP), 2)
            # Tight SL: 2.5% ABOVE entry (adverse = further up for PE)
            tight_sl_spot = round(price * (1 + cfg.TIGHT_SL_PCT), 2)
        else:
            # Price is below ST, we expect rally. SL = ST * 0.95 (price goes further down)
            sl_spot = round(st_val * (1 - cfg.SL_GAP), 2)
            # Tight SL: 2.5% BELOW entry (adverse = further down for CE)
            tight_sl_spot = round(price * (1 - cfg.TIGHT_SL_PCT), 2)

        # Synthetic-viability check: can we build CE + -PE at same strike?
        # Informational only — doesn't gate entry. Surfaces in Telegram + trade store.
        synth = check_synthetic_viable(
            kite, option['symbol'], trade['direction'],
            long_ask=premium, strike=option['strike'],
            lot_size=lot_size, spot=price,
        )
        if synth.get('viable'):
            logger.info("SYNTH-OK %s: %s OI=%d spread=%.1f%% net_debit=%.2f est_margin=%.0f",
                        stock, synth.get('opp_symbol'),
                        synth.get('opp_oi', 0), synth.get('opp_spread_pct', 0),
                        synth.get('net_debit') or 0, synth.get('est_margin') or 0)

        # === ENTRY PATH: direct or via confidence tracker ===
        if cfg.USE_CONFIDENCE_TRACKER:
            # Delegate to confidence tracker — it will wait for pullback,
            # score the signal, and alert when entry timing is right.
            _delegate_to_confidence_tracker(
                store, kite, trade, stock, price, st_val, gap,
                option, qty, sl_spot, tight_sl_spot, synth)
        else:
            # Direct entry (original behavior)
            store.enter_trade(trade['id'], {
                'entry_spot': price,
                'option_strike': option['strike'],
                'option_symbol': option['symbol'],
                'option_premium': premium,
                'option_expiry': option.get('expiry', ''),
                'lot_size': lot_size,
                'quantity': qty,
                'sl_spot': sl_spot,
                'tight_sl_spot': tight_sl_spot,
                'synthetic_viable': synth.get('viable', False),
                'synthetic_info': synth,
            })
            if trade.get('entry_retries', 0) > 0:
                store.update_entry_retries(trade['id'], 0)

            icon = _dir_icon(trade['direction'])
            tf = _tf_tag(trade['timeframe'])
            synth_block = ""
            if synth.get('viable'):
                opp = synth.get('opp_symbol', '?')
                opp_bid = synth.get('opp_bid', 0)
                opp_ask = synth.get('opp_ask', 0)
                opp_oi = synth.get('opp_oi', 0)
                opp_sprd = synth.get('opp_spread_pct', 0)
                long_sprd = synth.get('long_spread_pct', 0)
                net_debit = synth.get('net_debit')
                notional = synth.get('notional')
                margin_low = synth.get('margin_low')
                margin_high = synth.get('margin_high')
                cash_per_lot = synth.get('cash_per_lot')
                synth_type = synth.get('synth_type', 'LONG')

                synth_block = f"\n\n⚡ <b>SYNTHETIC {synth_type}</b> (you decide)\n"
                synth_block += (f"Buy {trade['direction']} @ {premium:.2f} + "
                                f"Sell {synth.get('opp_type', '?')} @ {opp_bid:.2f} (bid)\n")
                synth_block += (f"<code>{opp}</code> bid/ask {opp_bid:.2f}/{opp_ask:.2f} "
                                f"(sprd {opp_sprd:.1f}%) | OI {opp_oi:,}")
                if long_sprd > 0:
                    synth_block += f"\nLong-leg sprd {long_sprd:.1f}%"
                if net_debit is not None and cash_per_lot is not None:
                    credit_tag = "debit" if net_debit >= 0 else "credit"
                    synth_block += (f"\nNet {credit_tag} = Rs {abs(net_debit):.2f}/sh "
                                    f"(Rs {abs(cash_per_lot):,.0f} "
                                    f"{'out' if net_debit>=0 else 'in'} per lot)")
                if notional and margin_low and margin_high:
                    synth_block += (f"\nNotional Rs {notional/100000:.2f}L | "
                                    f"SPAN typical Rs {margin_low/100000:.2f}-"
                                    f"{margin_high/100000:.2f}L (18-40%)")

            msg = (
                f"{icon} <b>ENTRY</b> {tf} <code>{stock}</code>\n"
                f"Spot {price:,.1f} | ST {st_val:,.1f} | Gap {gap:.1%}\n"
                f"<code>{option['symbol']}</code> @ {premium:.2f}\n"
                f"Qty {qty} | SL {sl_spot:,.1f} | Tight {tight_sl_spot:,.1f}"
                f"{synth_block}"
            )
            send_telegram(msg)
            logger.info(msg.replace('\n', ' | '))


def _delegate_to_confidence_tracker(store, kite, trade, stock, price, st_val,
                                     gap, option, qty, sl_spot,
                                     tight_sl_spot=None, synth=None):
    """Hand off entry to confidence tracker for pullback-timed entry.

    Instead of entering immediately, creates a confidence tracker signal.
    The confidence monitor will score the signal, wait for pullback entry
    timing, and alert the user when conditions are right.
    """
    from .confidence_tracker import (
        get_tracker, format_watch_alert, _send_telegram as ct_send,
    )
    from .confidence import (
        _get_instrument_token, _fetch_daily, _fetch_hourly, _fetch_15min,
        _compute_all_st, score_signal,
    )

    premium = option.get('premium', 0)
    direction = trade['direction']
    timeframe = trade['timeframe']

    # Mark the magnet signal as entered (so magnet doesn't re-process it)
    # We use a special status to indicate it's delegated
    synth = synth or {}
    store.enter_trade(trade['id'], {
        'entry_spot': price,
        'option_strike': option['strike'],
        'option_symbol': option['symbol'],
        'option_premium': premium,
        'option_expiry': option.get('expiry', ''),
        'lot_size': option['lot_size'],
        'quantity': qty,
        'sl_spot': sl_spot,
        'tight_sl_spot': tight_sl_spot,
        'synthetic_viable': synth.get('viable', False),
        'synthetic_info': synth,
        'delegated_to_confidence': True,
    })
    if trade.get('entry_retries', 0) > 0:
        store.update_entry_retries(trade['id'], 0)

    # Run confidence scorer
    try:
        tok = _get_instrument_token(kite, stock)
        daily = _fetch_daily(kite, tok)
        hourly = _fetch_hourly(kite, tok)
        m15 = _fetch_15min(kite, tok)
        st_data = _compute_all_st(daily)
        result = score_signal(stock, price, st_data, daily, hourly, m15,
                              target_tf=timeframe, kite=kite)
    except Exception as e:
        logger.warning("Confidence score failed for %s: %s", stock, e)
        result = None

    score = result.get('total_score', 0) if result and 'error' not in result else 0
    grade = result.get('grade', '') if result and 'error' not in result else ''
    sq = result.get('sq_total', 0) if result and 'error' not in result else 0
    et = result.get('et_total', 0) if result and 'error' not in result else 0

    # Add to confidence tracker (revert magnet trade if this fails)
    try:
        tracker = get_tracker()
        need_up = direction == 'CE'
        target_label = 'resistance' if need_up else 'support'

        sig = tracker.add(
            symbol=stock,
            timeframe=timeframe,
            target_st=st_val,
            direction=direction,
            option_symbol=option['symbol'],
            option_price=premium,
            quantity=qty,
            sl_spot=sl_spot,
            signal_price=price,
            score=score,
            grade=grade,
            sq=sq,
            et=et,
            target_label=target_label,
            option_expiry=option.get('expiry', ''),
            tight_sl_spot=tight_sl_spot,
            synthetic_info=synth,
        )
    except Exception as e:
        # Tracker add failed — revert magnet trade to watching so it can retry
        logger.error("Confidence tracker add failed for %s: %s, reverting magnet trade", stock, e)
        try:
            store.revert_to_watching(trade['id'], f'tracker add failed: {e}')
        except Exception as re:
            logger.error("Revert also failed for #%d %s: %s", trade['id'], stock, re)
        return

    # Send WATCHING alert with 15M ST info on option
    icon = _dir_icon(direction)
    tf = _tf_tag(timeframe)

    # Compute 15M ST on option chart
    st15m = None
    try:
        from .confidence_tracker import _fetch_option_15m, compute_option_15m_st
        opt_candles = _fetch_option_15m(kite, option['symbol'])
        st15m = compute_option_15m_st(opt_candles)
    except Exception as e:
        logger.warning("15M ST computation failed for %s: %s", option['symbol'], e)

    if result and 'error' not in result:
        msg = format_watch_alert(result, option={
            'symbol': option['symbol'],
            'price': premium,
            'qty': qty,
        }, st15m=st15m)
        ct_send(msg)  # honors telegram_watching.enabled kill-switch
    else:
        # Scoring failed — log only, no Telegram (watching-channel silenced)
        logger.info(
            "WATCHING fallback %s %s %s spot=%.1f ST=%.1f gap=%.2f%% opt=%s @ %.2f",
            timeframe, stock, direction, price, st_val, gap * 100,
            option['symbol'], premium,
        )

    logger.info("Delegated %s %s to confidence tracker (signal #%d, score %d)",
                stock, direction, sig['id'], score)


def _sync_ct_exits_to_magnet(store, ct_tracker):
    """Close magnet trades whose confidence tracker signals have exited.

    When CT marks a signal as exited/cancelled, the corresponding magnet trade
    (status='entered', delegated_to_confidence=True) should also be closed.
    Prevents orphaned 'entered' trades from accumulating and hitting MAX_OPEN_TRADES.
    """
    # Get all 'entered' magnet trades that were delegated
    delegated = [t for t in store.get_entered()
                 if t.get('delegated_to_confidence')]
    if not delegated:
        return

    # Get all exited/cancelled CT signals
    ct_done = ct_tracker.list_all()
    ct_done = [s for s in ct_done if s['status'] in ('exited', 'cancelled')]

    # Match by stock + option_symbol
    ct_done_keys = {(s['symbol'], s.get('option_symbol', '')) for s in ct_done}

    for trade in delegated:
        key = (trade['stock'], trade.get('option_symbol', ''))
        if key in ct_done_keys:
            # Find matching CT signal for exit details
            ct_sig = next((s for s in ct_done
                           if s['symbol'] == trade['stock']
                           and s.get('option_symbol', '') == trade.get('option_symbol', '')),
                          None)
            exit_spot = ct_sig.get('exit_spot', 0) if ct_sig else 0
            exit_opt = ct_sig.get('exit_option_price', 0) if ct_sig else 0
            reason = ct_sig.get('exit_reason', 'ct_closed') if ct_sig else 'ct_closed'
            try:
                store.exit_trade(trade['id'], exit_spot, exit_opt,
                                 f'ct:{reason}')
                logger.info("Synced CT exit to magnet #%d %s: %s",
                            trade['id'], trade['stock'], reason)
            except Exception as e:
                logger.warning("Failed to sync CT exit for #%d %s: %s",
                               trade['id'], trade['stock'], e)


# ── Monitor: Open Trades ─────────────────────────────────────────────────

def check_open_trades(store, kite):
    """Check entered trades for TP/SL/time/trail exit conditions.

    TP logic: checks intraday HIGH/LOW (not just LTP) to catch wicks.
    Trail: once in profit, trails at 50% of peak premium gain.
    """
    entered = store.get_entered()
    if not entered:
        return

    # Get LTP + last 5 min high/low for wick detection
    stocks = list({t['stock'] for t in entered})
    spot_data = get_spot_with_recent_range(kite, stocks)

    for trade in entered:
      try:
        # Skip delegated trades — confidence tracker manages their exit timing.
        # These are marked 'entered' for capacity counting but user hasn't actually entered.
        if trade.get('delegated_to_confidence'):
            continue

        stock = trade['stock']
        sd = spot_data.get(stock)
        if not sd:
            continue

        price = sd['ltp']
        day_high = sd['high']
        day_low = sd['low']
        st_val = trade['st_value']
        side = trade['side']
        sl_spot = trade.get('sl_spot', 0)
        trade_id = trade['id']

        # === AUTO-CLOSE: option already expired ===
        option_expiry = trade.get('option_expiry', '')
        today_str = datetime.now(IST).strftime('%Y-%m-%d')
        if option_expiry and option_expiry < today_str:
            entry_prem = trade.get('option_premium', 0) or 0
            qty = trade.get('quantity', 0) or 0
            store.exit_trade(trade_id, price, 0, 'option_expired')
            tf = _tf_tag(trade.get('timeframe', ''))
            pnl = -entry_prem * qty if entry_prem > 0 else 0
            send_telegram(
                f"\u23f0 <b>EXPIRED</b> {tf} <code>{stock}</code>\n"
                f"Option {trade.get('option_symbol', '?')} expired {option_expiry}\n"
                f"<b>Rs {pnl:+,.0f}</b> (total loss)"
            )
            logger.info("EXPIRED: %s option %s expired %s, P&L Rs %+.0f",
                        stock, trade.get('option_symbol', '?'), option_expiry, pnl)
            continue

        # Get current option premiums
        option_symbol = trade.get('option_symbol')
        long_exit = get_sell_price(kite, option_symbol) if option_symbol else 0
        hedge_exit = None
        if trade.get('hedged') and trade.get('hedge_symbol'):
            hedge_exit = get_buy_price(kite, trade['hedge_symbol'])

        # Guard: skip exit checks if option has no bid (illiquid)
        # Prevents false exits at premium=0 which would book max loss
        if long_exit <= 0 and option_symbol:
            logger.debug("SKIP %s: no bid for %s, retry next cycle", stock, option_symbol)
            continue

        # === EXPIRY DAY ALERT: warn at midday to close (liquidity dries up) ===
        # option_expiry and today_str already computed above (auto-close block)
        now_dt = datetime.now(IST)
        if (option_expiry and option_expiry == today_str
                and trade_id not in _expiry_warned
                and (now_dt.hour, now_dt.minute) >= (12, 30)):
            entry_prem_exp = trade.get('option_premium', 0) or 0
            tf = _tf_tag(trade.get('timeframe', ''))
            pnl_exp = 0
            if long_exit > 0 and entry_prem_exp > 0:
                qty = trade.get('quantity', 0)
                pnl_exp = (long_exit - entry_prem_exp) * qty
            pnl_icon = '\u2705' if pnl_exp >= 0 else '\u274c'
            msg = (
                f"\u26a0\ufe0f <b>EXPIRY TODAY</b> {tf} <code>{stock}</code>\n"
                f"<code>{option_symbol}</code> expires today!\n"
                f"Entry {entry_prem_exp:.2f} | Bid {long_exit:.2f} | "
                f"<b>Rs {pnl_exp:+,.0f}</b>\n"
                f"Expiring worthless if not closed. Consider rolling to next expiry."
            )
            send_telegram(msg)
            logger.info("EXPIRY WARN: %s", msg.replace('\n', ' | '))
            _expiry_warned.add(trade_id)

        # Gap from ST
        gap = abs(price - st_val) / st_val

        # === TRACK PEAK PREMIUM (for trailing SL — long leg only) ===
        entry_prem = trade.get('option_premium', 0) or 0
        if long_exit > 0 and entry_prem > 0:
            old_peak = trade.get('peak_premium') or entry_prem
            new_peak = max(old_peak, long_exit)
            if new_peak > old_peak:
                store.update_peak_premium(trade_id, new_peak)

        # === ACTIVATE COST SL: gap shrinks to 1% (stock halfway to target) ===
        if not trade.get('cost_sl_active') and gap <= cfg.COST_SL_GAP:
            store.activate_cost_sl(trade_id)
            cost_lvl = trade.get('cost_sl_level', 0)
            msg = (
                f"\U0001f6e1 COST SL <code>{stock}</code> | gap {gap:.1%}\n"
                f"Floor {cost_lvl:.2f} (entry {entry_prem:.2f}) ZERO RISK"
            )
            send_telegram(msg)
            logger.info("COST SL: %s", msg.replace('\n', ' | '))

        # === HEDGE: gap widened back to 3% (reversal) — add short beyond ST ===
        # Skip if cost SL already active (trade was near ST, cost SL handles exit)
        if (not trade.get('hedged') and not trade.get('cost_sl_active')
                and gap >= cfg.HEDGE_GAP and gap < cfg.SL_GAP):
            hedge_result = _find_hedge_strike(kite, trade, price)
            if hedge_result:
                store.add_hedge(trade_id, hedge_result)
                nd = trade.get('hedge_net_debit', entry_prem)
                msg = (
                    f"\U0001f6e1 HEDGE <code>{stock}</code> | gap {gap:.1%}\n"
                    f"Short <code>{hedge_result['hedge_symbol']}</code> "
                    f"@ {hedge_result['hedge_premium']:.2f}\n"
                    f"Net debit {nd:.2f} | Max loss capped"
                )
                send_telegram(msg)
                logger.info("HEDGE: %s", msg.replace('\n', ' | '))
                hedge_exit = get_buy_price(kite, hedge_result['hedge_symbol'])

        # === CHECK TP: spot crossed ST line (using 5-min HIGH/LOW) ===
        tp_hit = False
        if side == 'above':
            # Bought PUT, expect decline. Touch = low ≤ ST
            tp_hit = day_low <= st_val
        else:
            # Bought CALL, expect rally. Touch = high ≥ ST
            tp_hit = day_high >= st_val

        if tp_hit:
            store.exit_trade(trade_id, price, long_exit, 'tp', hedge_exit)
            # peak_premium persisted in trade dict — kept as historical record
            pnl = trade.get('pnl', 0)
            wick_note = ""
            if side == 'above' and price > st_val:
                wick_note = f"\nWick: 5m low {day_low:.2f} touched ST, current {price:.2f}"
            elif side == 'below' and price < st_val:
                wick_note = f"\nWick: 5m high {day_high:.2f} touched ST, current {price:.2f}"
            h = " H" if trade.get('hedged') else ""
            pnl_icon = '\u2705' if pnl >= 0 else '\u274c'
            tf = _tf_tag(trade.get('timeframe', ''))
            msg = (
                f"{pnl_icon} <b>TP</b> {tf} <code>{stock}</code>{h}\n"
                f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                f"{trade.get('days_held', 0)}d"
            )
            send_telegram(msg)
            logger.info("TP: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK TRAILING PREMIUM SL: 50% of peak gain ===
        # Trail only activates after minimum gain threshold (avoids bid-ask noise exits)
        # CRITICAL: trail must NEVER exit at a loss — minimum profit floor enforced
        peak = trade.get('peak_premium') or entry_prem
        min_gain = entry_prem * cfg.TRAIL_MIN_GAIN_PCT  # e.g., 15% of entry
        if peak >= entry_prem + min_gain and long_exit > 0:
            gain = peak - entry_prem
            trail_level = entry_prem + gain * cfg.TRAIL_PCT
            # Trail must be above cost SL if active
            cost_sl = trade.get('cost_sl_level', 0) or 0
            # Minimum profit floor: trail never below entry + 0.50 (prevents loss on trail exit)
            min_profit_floor = entry_prem + max(0.50, _get_tick_size(trade.get('option_symbol', '')) * 2)
            effective_trail = max(trail_level, cost_sl, min_profit_floor)
            if long_exit <= effective_trail:
                store.exit_trade(trade_id, price, long_exit, 'tp_trail', hedge_exit)
                # peak_premium persisted in trade dict — kept as historical record
                pnl = trade.get('pnl', 0)
                pnl_icon = '\u2705' if pnl >= 0 else '\u274c'
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"{pnl_icon} <b>TRAIL</b> {tf} <code>{stock}</code>\n"
                    f"Peak {peak:.2f} | Trail {trail_level:.2f}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                    f"{trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("TRAIL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK COST SL: premium drops back to cost+0.10 ===
        if trade.get('cost_sl_active') and long_exit > 0:
            cost_sl = trade.get('cost_sl_level', 0) or 0
            if long_exit <= cost_sl:
                store.exit_trade(trade_id, price, long_exit, 'sl_cost', hedge_exit)
                # peak_premium persisted in trade dict — kept as historical record
                pnl = trade.get('pnl', 0)
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"\U0001f7f0 <b>COST SL</b> {tf} <code>{stock}</code>\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ~BE {trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("COST SL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK PREMIUM SL: 25% for daily (intraday), 40% for W/M ===
        # Daily: tighter SL because no day-2 recovery. Backtest: +80L at 25% vs +63L at none.
        if not trade.get('cost_sl_active') and not trade.get('hedged') and long_exit > 0:
            sl_pct = (cfg.DAILY_PREMIUM_SL_PCT if trade.get('timeframe') == 'daily'
                      else cfg.PREMIUM_SL_PCT)
            premium_sl = entry_prem * (1 - sl_pct)
            if long_exit <= premium_sl:
                actual_loss_pct = (entry_prem - long_exit) / entry_prem
                is_gap = actual_loss_pct > (sl_pct + 0.15)
                exit_reason = 'sl_premium_gap' if is_gap else 'sl_premium'
                store.exit_trade(trade_id, price, long_exit, exit_reason, hedge_exit)
                # peak_premium persisted in trade dict — kept as historical record
                pnl = trade.get('pnl', 0)
                tf = _tf_tag(trade.get('timeframe', ''))
                gap_tag = " GAP!" if is_gap else ""
                msg = (
                    f"\u274c <b>PREM SL</b> {tf} <code>{stock}</code>{gap_tag}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} ({actual_loss_pct:.0%})\n"
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                    f"{trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("PREM SL: %s", msg.replace('\n', ' | '))
                continue

        # === TIGHT SPOT SL: entry-anchored adverse move (fires FIRST) ===
        # Tighter than sl_spot (which is 7% from ST). Required for sizing up —
        # synthetic futures have symmetric 1:1 P&L; can't tolerate 10% drift.
        tight_sl_spot = trade.get('tight_sl_spot')
        if tight_sl_spot:
            tight_hit = False
            if side == 'above' and price >= tight_sl_spot:
                tight_hit = True
            elif side == 'below' and price <= tight_sl_spot:
                tight_hit = True
            if tight_hit:
                entry_spot_ref = trade.get('entry_spot') or price
                adverse_pct = abs(price - entry_spot_ref) / entry_spot_ref * 100
                store.exit_trade(trade_id, price, long_exit, 'sl_spot_tight', hedge_exit)
                pnl = trade.get('pnl', 0)
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"❌ <b>TIGHT SL</b> {tf} <code>{stock}</code>\n"
                    f"Spot {price:,.1f} vs entry {entry_spot_ref:,.1f} "
                    f"({adverse_pct:.1f}% adverse)\n"
                    f"{entry_prem:.2f}→{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                    f"{trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("TIGHT SL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK SPOT SL: gap widened to 5% (hard backstop) ===
        sl_hit = False
        if side == 'above' and sl_spot:
            sl_hit = price >= sl_spot
        elif side == 'below' and sl_spot:
            sl_hit = price <= sl_spot

        if sl_hit:
            sl_gap_pct = abs(price - sl_spot) / sl_spot * 100
            exit_reason = 'sl_spot_gap' if sl_gap_pct > 1.0 else 'sl_spot'
            store.exit_trade(trade_id, price, long_exit, exit_reason, hedge_exit)
            # peak_premium persisted in trade dict — kept as historical record
            pnl = trade.get('pnl', 0)
            gap_warn = (f"\nGAP: spot {price:.2f} is {sl_gap_pct:.1f}% past "
                        f"SL {sl_spot:.2f}") if sl_gap_pct > 1.0 else ""
            tf = _tf_tag(trade.get('timeframe', ''))
            gap_tag = " GAP!" if sl_gap_pct > 1.0 else ""
            msg = (
                f"\u274c <b>SPOT SL</b> {tf} <code>{stock}</code>{gap_tag}\n"
                f"Spot {price:,.1f} hit SL {sl_spot:,.1f}\n"
                f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                f"{trade.get('days_held', 0)}d"
            )
            send_telegram(msg)
            logger.info("SL: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK EOD EXIT: daily trades are intraday-only ===
        # Backtest: day1 = +63L (77% win), day2+ = -37L (39% win). Theta kills overnight.
        # Exit same-day trades at 15:15. Exit prior-day daily trades immediately.
        if trade.get('timeframe') == 'daily':
            now_eod = datetime.now(IST)
            entry_date_str = trade.get('entry_date', '')
            today_eod = now_eod.strftime('%Y-%m-%d')
            should_eod = False

            if entry_date_str and entry_date_str < today_eod:
                # Entered yesterday or earlier — exit immediately (stale daily trade)
                should_eod = True
            elif (entry_date_str == today_eod
                    and (now_eod.hour, now_eod.minute) >= (cfg.DAILY_EOD_EXIT_HOUR, cfg.DAILY_EOD_EXIT_MIN)):
                # Same day, past EOD exit time
                should_eod = True

            if should_eod:
                reason = 'eod_daily' if entry_date_str == today_eod else 'eod_daily_stale'
                store.exit_trade(trade_id, price, long_exit, reason, hedge_exit)
                # peak_premium persisted in trade dict — kept as historical record
                pnl = trade.get('pnl', 0)
                pnl_icon = '\u2705' if pnl >= 0 else '\u274c'
                stale = " (stale)" if entry_date_str != today_eod else ""
                msg = (
                    f"{pnl_icon} <b>EOD EXIT</b> [D] <code>{stock}</code>{stale}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%)"
                )
                send_telegram(msg)
                logger.info("EOD: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK TIME SL: 5 trading days (weekly/monthly only now) ===
        entry_date = trade.get('entry_date')
        if entry_date:
            entry_dt = datetime.strptime(entry_date, '%Y-%m-%d')
            now = datetime.now()
            # Count trading days (weekdays excluding known Indian market holidays)
            _market_holidays_2026 = {
                '2026-01-26', '2026-03-14', '2026-03-17', '2026-03-31',
                '2026-04-01', '2026-04-14', '2026-04-18', '2026-05-01',
                '2026-06-26', '2026-07-17', '2026-08-15', '2026-08-28',
                '2026-10-02', '2026-10-20', '2026-10-21', '2026-10-23',
                '2026-11-04', '2026-11-05', '2026-12-25',
            }
            biz_days = 0
            d = entry_dt + timedelta(days=1)
            while d <= now:
                if d.weekday() < 5 and d.strftime('%Y-%m-%d') not in _market_holidays_2026:
                    biz_days += 1
                d += timedelta(days=1)
            if biz_days >= cfg.SL_TIME_DAYS:
                store.exit_trade(trade_id, price, long_exit, 'sl_time', hedge_exit)
                # peak_premium persisted in trade dict — kept as historical record
                pnl = trade.get('pnl', 0)
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"\u23f0 <b>TIME SL</b> {tf} <code>{stock}</code> ({biz_days}d)\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%)"
                )
                send_telegram(msg)
                logger.info("TIME SL: %s", msg.replace('\n', ' | '))
                continue

      except Exception as e:
          logger.error("Error monitoring trade #%d %s: %s",
                       trade.get('id', 0), trade.get('stock', '?'), e,
                       exc_info=True)


# ── Hedge: Short Leg Beyond ST ─────────────────────────────────────────────

def _find_hedge_strike(kite, trade: dict, current_spot: float) -> dict:
    """Find nearest strike BEYOND ST to sell as hedge.

    For PE (price above ST): sell PUT with strike BELOW ST
    For CE (price below ST): sell CALL with strike ABOVE ST

    This ensures short leg is OTM at target → no TV problem.
    Returns hedge_data dict or None.
    """
    import csv

    stock = trade['stock']
    direction = trade['direction']  # CE or PE
    long_strike = trade['option_strike']
    long_expiry = trade.get('option_expiry', '')
    st_val = trade['st_value']
    entry_prem = trade.get('option_premium', 0) or 0
    option_csv = cfg.PROJECT_ROOT / 'nse_stocks_options.csv'

    candidates = []

    # Try CSV — filter to same expiry
    if option_csv.exists():
        try:
            with open(option_csv, newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get('stock_symbol', '') == stock
                            and row.get('option_type', '') == direction):
                        expiry = row.get('option_expiry', '')
                        if long_expiry and expiry != long_expiry:
                            continue
                        strike = float(row.get('option_strike', 0))
                        candidates.append({
                            'strike': strike,
                            'symbol': row.get('option_tradingsymbol', ''),
                        })
        except Exception:
            pass

    # Fallback: NFO instruments
    if not candidates:
        try:
            instruments = _get_nfo_instruments(kite)
            for inst in instruments:
                if (inst['name'] == stock
                        and inst['instrument_type'] == direction):
                    inst_expiry = str(inst.get('expiry', ''))
                    if long_expiry and inst_expiry != long_expiry:
                        continue
                    candidates.append({
                        'strike': inst['strike'],
                        'symbol': inst['tradingsymbol'],
                    })
        except Exception:
            pass

    if not candidates:
        return None

    # KEY RULE: short strike must be BEYOND ST
    if direction == 'PE':
        # Long PUT, target is decline to ST. Short PUT below ST.
        beyond = [c for c in candidates if c['strike'] < st_val]
        if not beyond:
            return None
        # Nearest strike below ST (maximize credit, minimize spread width)
        beyond.sort(key=lambda x: x['strike'], reverse=True)
        best = beyond[0]
        spread_width = long_strike - best['strike']
    else:
        # Long CALL, target is rally to ST. Short CALL above ST.
        beyond = [c for c in candidates if c['strike'] > st_val]
        if not beyond:
            return None
        # Nearest strike above ST
        beyond.sort(key=lambda x: x['strike'])
        best = beyond[0]
        spread_width = best['strike'] - long_strike

    if spread_width <= 0:
        return None

    # Get credit (BID - slippage)
    credit = get_sell_price(kite, best['symbol'])
    if credit <= 0:
        logger.warning("No bid for hedge %s, skipping", best['symbol'])
        return None

    # Check debit ratio
    net_debit = entry_prem - credit
    if net_debit <= 0:
        logger.warning("Hedge net_debit=%.2f (credit %.2f > entry %.2f) — net credit spread "
                        "creates unlimited risk on short side, skipping", net_debit, credit, entry_prem)
        return None
    debit_ratio = net_debit / spread_width if spread_width > 0 else 1.0
    if debit_ratio > cfg.HEDGE_MAX_DEBIT_RATIO:
        logger.info("Hedge debit ratio %.1f%% > max %.0f%%, skipping (fallback to premium SL)",
                     debit_ratio * 100, cfg.HEDGE_MAX_DEBIT_RATIO * 100)
        return None

    logger.info("Hedge: sell %s @%.2f, width=%.0f, net_debit=%.2f, ratio=%.1f%%",
                best['symbol'], credit, spread_width, net_debit, debit_ratio * 100)

    return {
        'hedge_strike': best['strike'],
        'hedge_symbol': best['symbol'],
        'hedge_premium': credit,
        'hedge_spread_width': spread_width,
    }


# ── Stale Signal Cleanup ──────────────────────────────────────────────────

def cleanup_stale_signals(store):
    """Cancel stale watching signals at end of their timeframe lifecycle.

    Daily:   cancel next day (ST recomputes daily, yesterday's signal is stale).
    Weekly:  cancel when current ISO week differs from signal week (Monday = fresh).
    Monthly: cancel when current month differs from signal month (1st = fresh).
    """
    now = datetime.now()
    today_str = now.strftime('%Y-%m-%d')
    current_week = now.isocalendar()[:2]       # (year, week_number)
    current_month = (now.year, now.month)

    for trade in store.get_watching():
        sig_date = trade.get('signal_date', '')
        if not sig_date:
            continue

        timeframe = trade.get('timeframe', '')
        sig_dt = datetime.strptime(sig_date, '%Y-%m-%d')

        stale = False
        reason = ''

        if timeframe == 'daily':
            # Daily: stale if signal is from a previous day
            if sig_date < today_str:
                stale = True
                reason = "daily signal stale (new day, ST recomputed)"

        elif timeframe == 'weekly':
            # Weekly: stale if signal is from a previous ISO week
            sig_week = sig_dt.isocalendar()[:2]
            if sig_week != current_week:
                stale = True
                reason = f"weekly signal stale (week {sig_week[1]} -> {current_week[1]})"

        elif timeframe == 'monthly':
            # Monthly: stale if signal is from a previous month
            sig_month = (sig_dt.year, sig_dt.month)
            if sig_month != current_month:
                stale = True
                reason = f"monthly signal stale ({sig_dt.strftime('%b')} -> {now.strftime('%b')})"

        if stale:
            store.cancel_signal(trade['id'], reason)
            logger.info("Cleaned stale %s signal #%d %s (from %s): %s",
                        timeframe, trade['id'], trade['stock'], sig_date, reason)


# ── Main Run Loop ────────────────────────────────────────────────────────

def _setup_file_logging():
    """Add file handler for cron mode logging."""
    log_file = cfg.LOG_DIR / f"magnet_{datetime.now().strftime('%Y%m%d')}.log"
    cfg.LOG_DIR.mkdir(exist_ok=True)
    fh = logging.FileHandler(str(log_file))
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    ))
    logging.getLogger().addHandler(fh)
    return log_file


# ── EOD Summary ──────────────────────────────────────────────────────────────


def _fmt_val(val: float) -> str:
    """Format Rs value compactly: 2,450 / 31.8K / 10.0L."""
    av = abs(val)
    s = '' if val >= 0 else '-'
    if av >= 1000000:
        return f"{s}{av / 100000:.1f}L"
    if av >= 10000:
        return f"{s}{av / 1000:.1f}K"
    return f"{val:,.0f}"


def _short_reason(reason: str) -> str:
    """Abbreviate exit reason for table display."""
    return {
        'tp': 'TP', 'tp_trail': 'TRL', 'sl_cost': 'CSL',
        'sl_premium': 'PSL', 'sl_premium_gap': 'PSL',
        'sl_spot': 'SSL', 'sl_spot_gap': 'SSL', 'sl_spot_tight': 'TSL-S',
        'sl_time': 'TSL',
        'eod_daily': 'EOD', 'eod_stale': 'EOD',
        'option_expired': 'EXP', 'manual': 'MAN',
        'weekly signal stale': 'STL', 'monthly signal stale': 'STL',
        'ct_exit': 'CTX', 'ct_cancel': 'CTC',
    }.get(reason, reason[:3].upper() if reason else '?')


def build_eod_html(store) -> str:
    """Build rich HTML EOD summary table for Telegram.

    Sections:
    1. Exits today — full table with buy/sell/P&L/%/reason
    2. Entries today (still open) — option symbol + capital deployed
    3. Open positions — invested vs peak, days held
    4. Footer — day P&L, ROI%, open/watch counts
    """
    today = datetime.now().strftime('%Y-%m-%d')
    today_disp = datetime.now().strftime('%d-%b')

    all_trades = store.load_trades()
    exited_today = sorted(
        [t for t in all_trades
         if t.get('exit_date') == today and t.get('status') == 'exited'],
        key=lambda t: t.get('exit_time', ''),
    )
    # Entries today that are still open at EOD
    entered_today = sorted(
        [t for t in all_trades
         if t.get('entry_date') == today and t.get('status') == 'entered'],
        key=lambda t: t.get('entry_time', ''),
    )
    open_trades = store.get_entered()
    watching = store.get_watching()
    cancelled_today = [t for t in all_trades
                       if t.get('status') == 'cancelled'
                       and t.get('exit_date', t.get('signal_date', '')) == today]

    parts = [f"\U0001f4ca <b>MAGNET EOD</b> | {today_disp}\n"]

    # ── Exits table ──
    day_buy = day_pnl = 0
    if exited_today:
        parts.append(f"<b>\U0001f4e4 Exits ({len(exited_today)})</b>")
        rows = [
            f"{'Exit':<5} {'Symbol':<10} {'Buy':>6} {'Sell':>6} {'P&L':>7} {'%':>5} {'':>3}",
            "\u2500" * 48,
        ]
        for t in exited_today:
            stk = (t.get('stock') or '?')[:7]
            d = (t.get('direction') or '?')[0]
            tf = {'monthly': 'M', 'weekly': 'W', 'daily': 'D'}.get(
                t.get('timeframe'), '?')
            sym = f"{stk:<7} {d}{tf}"

            etime = (t.get('exit_time') or '??:??')[:5]
            ep = t.get('option_premium', 0) or 0
            qty = t.get('quantity', 0) or 0
            pnl = t.get('pnl', 0) or 0
            pct = t.get('pnl_pct', 0) or 0

            bv = ep * qty
            sv = bv + pnl  # effective proceeds = cost + P&L
            rsn = _short_reason(t.get('exit_reason', ''))
            h = '*' if t.get('hedged') else ''

            day_buy += bv
            day_pnl += pnl

            rows.append(
                f"{etime:<5} {sym:<10} {_fmt_val(bv):>6} {_fmt_val(sv):>6} "
                f"{pnl:>+7,.0f} {pct:>+5.1f} {rsn}{h}"
            )

        rows.append("\u2500" * 48)
        rows.append(
            f"{'':5} {'TOTAL':<10} {_fmt_val(day_buy):>6} {_fmt_val(day_buy + day_pnl):>6} "
            f"{day_pnl:>+7,.0f}"
        )
        parts.append("<pre>" + "\n".join(rows) + "</pre>")
    else:
        parts.append("No exits today.")

    # ── Entries today (still open at EOD) ──
    # Only show if there are new entries NOT already covered in exits
    if entered_today:
        parts.append(f"\n<b>\U0001f4e5 Entries ({len(entered_today)})</b>")
        rows = [
            f"{'Time':<5} {'Stock':<8} {'Option':<20} {'Capital':>7}",
            "\u2500" * 44,
        ]
        for t in entered_today:
            stk = (t.get('stock') or '?')[:8]
            etime = (t.get('entry_time') or '??:??')[:5]
            opt = (t.get('option_symbol') or '?')[:20]
            ep = t.get('option_premium', 0) or 0
            qty = t.get('quantity', 0) or 0
            cap = ep * qty
            rows.append(f"{etime:<5} {stk:<8} {opt:<20} {_fmt_val(cap):>7}")
        parts.append("<pre>" + "\n".join(rows) + "</pre>")

    # ── Open positions (exclude today's entries to avoid duplication) ──
    entered_ids = {t.get('id') for t in entered_today}
    carry_trades = [t for t in open_trades if t.get('id') not in entered_ids]
    if carry_trades:
        total_inv = sum(
            (t.get('option_premium', 0) or 0) * (t.get('quantity', 0) or 0)
            for t in carry_trades)
        parts.append(
            f"\n<b>\U0001f4c2 Carry ({len(carry_trades)})"
            f" | \u20b9{_fmt_val(total_inv)}</b>"
        )
        rows = [
            f"{'Symbol':<10} {'Invested':>8} {'Peak':>8} {'Days':>4}",
            "\u2500" * 34,
        ]
        for t in carry_trades:
            stk = (t.get('stock') or '?')[:7]
            d = (t.get('direction') or '?')[0]
            tf = {'monthly': 'M', 'weekly': 'W', 'daily': 'D'}.get(
                t.get('timeframe'), '?')
            sym = f"{stk:<7} {d}{tf}"

            ep = t.get('option_premium', 0) or 0
            qty = t.get('quantity', 0) or 0
            inv = ep * qty
            pk = (t.get('peak_premium', 0) or 0) * qty

            days = ''
            if t.get('entry_date'):
                try:
                    ed = datetime.strptime(t['entry_date'], '%Y-%m-%d')
                    days = f"{(datetime.now() - ed).days}d"
                except Exception:
                    days = '?'

            rows.append(
                f"{sym:<10} {_fmt_val(inv):>8} {_fmt_val(pk):>8} {days:>4}"
            )
        parts.append("<pre>" + "\n".join(rows) + "</pre>")

    # ── Footer summary ──
    pnl_icon = '\u2705' if day_pnl >= 0 else '\u274c'
    footer = [f"{pnl_icon} <b>\u20b9{day_pnl:+,.0f}</b>"]
    if day_buy:
        footer.append(f"on \u20b9{_fmt_val(day_buy)} ({day_pnl / day_buy * 100:+.1f}%)")
    footer.append(f"Open {len(open_trades)}")
    if watching:
        footer.append(f"Watch {len(watching)}")
    if cancelled_today:
        footer.append(f"Cancel {len(cancelled_today)}")

    parts.append("\n" + " | ".join(footer))

    return "\n".join(parts)


def run(dry_run: bool = False):
    """Main loop: scan every 5 min + monitor every 30s.

    Runs during market hours only. Exits cleanly outside hours.
    """
    log_file = _setup_file_logging()
    logger.info("Log file: %s", log_file)

    store = get_store()
    kite = _get_kite()

    # Confidence tracker setup (if enabled)
    ct_tracker = None
    if cfg.USE_CONFIDENCE_TRACKER:
        from .confidence_tracker import get_tracker, monitor_once as ct_monitor_once
        ct_tracker = get_tracker()
        logger.info("Confidence tracker ENABLED (poll every %ds)", cfg.CONFIDENCE_POLL_SEC)

    # Startup summary
    watching = len(store.get_watching())
    entered = len(store.get_entered())
    ct_watching = len(ct_tracker.get_watching()) if ct_tracker else 0
    ct_entered = len(ct_tracker.get_entered()) if ct_tracker else 0
    logger.info("Magnet monitor started: %d watching, %d entered, "
                "ct_watching=%d, ct_entered=%d, dry_run=%s",
                watching, entered, ct_watching, ct_entered, dry_run)

    # Config banner — makes stale deployment obvious
    logger.info(
        "Active thresholds: CHARTINK<=%.1f%% | WATCH<=%.1f%% | ENTRY %.1f-%.1f%% | "
        "COST_SL=%.1f%% | HEDGE=%.1f%% | TIGHT_SL=%.1f%% | SL=%.1f%%",
        cfg.CHARTINK_GAP_MAX * 100, cfg.SIGNAL_GAP_MAX * 100,
        cfg.ENTRY_GAP_MIN * 100, cfg.ENTRY_GAP * 100,
        cfg.COST_SL_GAP * 100, cfg.HEDGE_GAP * 100,
        cfg.TIGHT_SL_PCT * 100, cfg.SL_GAP * 100,
    )

    # Config-drift warning: flag runtime thresholds that are LOOSER than
    # compile-time defaults. Common cause: server running old magnet_config.json
    # after a tightening commit (root cause of AUBANK 1.9% entry bug).
    # Special case for tight_sl_pct: smaller = tighter, so "looser" means > default.
    drifted = []
    for key in ('entry_gap_min', 'entry_gap', 'signal_gap_max', 'chartink_gap_max'):
        default = cfg._DEFAULTS[key]
        runtime = cfg._runtime.get(key, default)
        if runtime < default:
            drifted.append(f"{key}={runtime*100:.1f}% (default {default*100:.1f}%)")
    # tight_sl_pct: direction inverted — larger runtime = looser SL
    tight_default = cfg._DEFAULTS.get('tight_sl_pct', 0.025)
    tight_runtime = cfg._runtime.get('tight_sl_pct', tight_default)
    if tight_runtime > tight_default:
        drifted.append(
            f"tight_sl_pct={tight_runtime*100:.1f}% (default {tight_default*100:.1f}%)"
        )
    if drifted:
        logger.warning(
            "CONFIG DRIFT — runtime thresholds LOOSER than defaults: %s. "
            "Server may be running stale magnet_config.json. Re-deploy to match code.",
            ", ".join(drifted)
        )

    if not is_market_hours():
        logger.info("Market closed. Exiting.")
        print("Market is closed. Run during 9:15-15:30 IST, Mon-Fri.")
        return

    last_scan_time = 0
    last_ct_time = 0  # last confidence tracker poll
    cycle = 0
    kite_error_count = 0

    try:
        while is_market_hours():
            cycle += 1
            now = time.time()

            # Scan Chartink every SCAN_INTERVAL_SEC
            if now - last_scan_time >= cfg.SCAN_INTERVAL_SEC:
                logger.info("--- Scan cycle %d ---", cycle)
                try:
                    added = validate_and_add_signals(store, kite, dry_run=dry_run)
                    kite_error_count = 0  # reset on success
                except Exception as e:
                    logger.error("Scan error: %s", e)
                    kite_error_count += 1
                    # Reconnect Kite if repeated errors (token may have expired)
                    if kite_error_count >= 3:
                        logger.warning("3+ consecutive errors, reconnecting Kite...")
                        try:
                            kite = _get_kite()
                            kite_error_count = 0
                            logger.info("Kite reconnected")
                        except Exception as re:
                            logger.error("Kite reconnect failed: %s", re)
                            send_telegram(f"MAGNET ERROR: Kite reconnect failed: {re}")

                last_scan_time = now

                # Cleanup stale signals during scan cycle
                cleanup_stale_signals(store)

                # Periodic Drive sync
                store.maybe_sync()

            # Monitor prices every MONITOR_INTERVAL_SEC (independent error boundaries)
            if not dry_run:
                try:
                    check_watching_signals(store, kite)
                except Exception as e:
                    logger.error("Watching monitor error: %s", e, exc_info=True)

                # 5-layer exit monitoring — ALWAYS runs for non-delegated trades.
                # When confidence tracker is enabled, it handles delegated trades'
                # entry timing, but the mechanical SL layers still protect all entered trades.
                try:
                    check_open_trades(store, kite)
                except Exception as e:
                    logger.error("Trade monitor error: %s", e, exc_info=True)

            # Confidence tracker: poll for entry timing + exhaustion
            if ct_tracker and not dry_run and (now - last_ct_time) >= cfg.CONFIDENCE_POLL_SEC:
                ct_w = len(ct_tracker.get_watching())
                ct_e = len(ct_tracker.get_entered())
                logger.info("CT poll: %d watching, %d entered", ct_w, ct_e)
                try:
                    ct_monitor_once(kite, ct_tracker, dry_run=False)
                except Exception as e:
                    logger.error("Confidence tracker error: %s", e, exc_info=True)

                # Sync: close magnet trades whose CT signals have been exited/cancelled
                try:
                    _sync_ct_exits_to_magnet(store, ct_tracker)
                except Exception as e:
                    logger.error("CT->magnet sync error: %s", e, exc_info=True)
                last_ct_time = now

            time.sleep(cfg.MONITOR_INTERVAL_SEC)

    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")
    finally:
        # Flush store to ensure last state is persisted
        try:
            store.flush()
        except Exception:
            pass

    # End of day summary — rich HTML table
    summary = build_eod_html(store)
    logger.info("EOD summary sent")
    send_telegram(summary)


def run_scan_once(dry_run: bool = False):
    """Single scan cycle — for testing."""
    store = get_store()
    kite = _get_kite()
    added = validate_and_add_signals(store, kite, dry_run=dry_run)
    print(f"\nScan complete: {len(added)} signals added")
    for sig in added:
        if isinstance(sig, dict):
            stock = sig.get('stock', '?')
            gap = sig.get('signal_gap_pct', sig.get('gap', 0))
            if isinstance(gap, float) and gap < 1:
                gap *= 100
            print(f"  {stock}: gap={gap:.1f}%")
    return added

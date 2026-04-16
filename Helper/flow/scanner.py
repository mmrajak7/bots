"""Thread 2 core: 15M ST computation, LTP proximity check, option lookup.

Responsibilities:
- Fetch 15M candle data from Kite and compute ST(10,3)
- Check if LTP is within +/-0.5% of 15M ST (entry alert zone)
- Look up ATM option from CSV, check bid-ask liquidity
- Build Telegram alert message
"""

import csv
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from . import config as cfg

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Kite Singleton ────────────────────────────────────────────────────────

_kite = None
_instrument_cache: Dict[str, int] = {}  # symbol -> instrument_token
_instruments_loaded = False


def _get_kite():
    """Initialize KiteConnect from shared token file. Re-reads on token expiry."""
    global _kite
    if _kite:
        return _kite
    from kiteconnect import KiteConnect

    if not cfg.KITE_TOKEN_FILE.exists():
        raise FileNotFoundError(f"Kite token not found: {cfg.KITE_TOKEN_FILE}")
    with open(cfg.KITE_TOKEN_FILE) as f:
        token_data = json.load(f)
    _kite = KiteConnect(api_key=token_data['api_key'])
    _kite.set_access_token(token_data['access_token'])
    return _kite


def reset_kite():
    """Force re-init on next call (e.g., after token expiry)."""
    global _kite, _instruments_loaded
    _kite = None
    _instruments_loaded = False
    _instrument_cache.clear()


def _load_instruments(kite) -> None:
    """Load full NSE instrument list once, cache all tokens."""
    global _instruments_loaded
    if _instruments_loaded:
        return
    try:
        instruments = kite.instruments('NSE')
        for inst in instruments:
            _instrument_cache[inst['tradingsymbol']] = inst['instrument_token']
        _instruments_loaded = True
        logger.info("Loaded %d NSE instrument tokens", len(_instrument_cache))
    except Exception as e:
        logger.error("Failed to load NSE instruments: %s", e)


def _get_instrument_token(kite, symbol: str) -> Optional[int]:
    """Get NSE instrument token from cache. Loads full list on first call."""
    _load_instruments(kite)
    return _instrument_cache.get(symbol)


# ── 15M ST Computation ────────────────────────────────────────────────────

def fetch_15m_candles(kite, symbol: str, days: int = 5) -> List[dict]:
    """Fetch 15-minute OHLC candles from Kite for the last N days."""
    token = _get_instrument_token(kite, symbol)
    if not token:
        logger.warning("No instrument token for %s, skipping", symbol)
        return []

    to_date = datetime.now(IST)
    from_date = to_date - timedelta(days=days)

    try:
        candles = kite.historical_data(
            token,
            from_date.strftime('%Y-%m-%d'),
            to_date.strftime('%Y-%m-%d %H:%M:%S'),
            '15minute',
        )
        return candles
    except Exception as e:
        logger.error("Failed to fetch 15M data for %s: %s", symbol, e)
        return []


def compute_supertrend(candles: List[dict], period: int = 10,
                       multiplier: float = 3) -> List[dict]:
    """Compute SuperTrend on candle data. Returns list with st_value + direction."""
    n = len(candles)
    if n < period + 1:
        return []

    # True Range
    tr = []
    for i in range(n):
        h, l = candles[i]['high'], candles[i]['low']
        if i == 0:
            tr.append(h - l)
        else:
            pc = candles[i - 1]['close']
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))

    # ATR
    atr = [0.0] * n
    atr[period - 1] = sum(tr[:period]) / period
    for i in range(period, n):
        atr[i] = (atr[i - 1] * (period - 1) + tr[i]) / period

    # SuperTrend
    upper = [0.0] * n
    lower = [0.0] * n
    st = [0.0] * n
    direction = [1] * n  # 1=UP, -1=DOWN

    for i in range(period - 1, n):
        hl2 = (candles[i]['high'] + candles[i]['low']) / 2
        upper[i] = hl2 + multiplier * atr[i]
        lower[i] = hl2 - multiplier * atr[i]

        if i == period - 1:
            st[i] = lower[i]
            direction[i] = 1
            continue

        if lower[i] > lower[i - 1] or candles[i - 1]['close'] < lower[i - 1]:
            pass
        else:
            lower[i] = lower[i - 1]

        if upper[i] < upper[i - 1] or candles[i - 1]['close'] > upper[i - 1]:
            pass
        else:
            upper[i] = upper[i - 1]

        if direction[i - 1] == 1:
            if candles[i]['close'] < lower[i]:
                direction[i] = -1
                st[i] = upper[i]
            else:
                direction[i] = 1
                st[i] = lower[i]
        else:
            if candles[i]['close'] > upper[i]:
                direction[i] = 1
                st[i] = lower[i]
            else:
                direction[i] = -1
                st[i] = upper[i]

    results = []
    for i in range(period - 1, n):
        results.append({
            'date': candles[i]['date'],
            'close': candles[i]['close'],
            'high': candles[i]['high'],
            'low': candles[i]['low'],
            'st_value': round(st[i], 2),
            'direction': 'UP' if direction[i] == 1 else 'DOWN',
        })
    return results


def compute_15m_st(kite, symbol: str) -> Optional[dict]:
    """Fetch 15M candles and compute current ST. Returns latest ST info.

    Returns dict: {st_value, direction, candle_close, candle_date} or None.
    """
    candles = fetch_15m_candles(kite, symbol, days=cfg.CANDLE_DATA_DAYS)
    if not candles:
        return None

    st_data = compute_supertrend(candles, cfg.ST_PERIOD, cfg.ST_MULTIPLIER)
    if not st_data:
        return None

    last = st_data[-1]
    return {
        'st_value': last['st_value'],
        'direction': last['direction'],
        'candle_close': last['close'],
        'candle_date': last['date'],
    }


# ── LTP Batch Fetch ──────────────────────────────────────────────────────

def get_ltp_batch(kite, symbols: List[str]) -> Dict[str, float]:
    """Fetch LTP for multiple NSE symbols in one call."""
    if not symbols:
        return {}
    nse_symbols = [f"NSE:{s}" for s in symbols]
    try:
        data = kite.ltp(nse_symbols)
        result = {}
        for s in symbols:
            key = f"NSE:{s}"
            if key in data:
                result[s] = data[key]['last_price']
        return result
    except Exception as e:
        logger.error("LTP batch fetch failed: %s", e)
        return {}


# ── Option Lookup ─────────────────────────────────────────────────────────

def find_atm_option(stock: str, direction: str,
                    spot: float) -> Optional[dict]:
    """Find ATM option from nse_stocks_options.csv.

    direction: 'CE' or 'PE'
    Returns: {symbol, strike, expiry, lot_size} or None.
    """
    if not cfg.OPTIONS_CSV.exists():
        logger.warning("Options CSV not found: %s — run kite_nse_options.py",
                        cfg.OPTIONS_CSV)
        return None

    try:
        with open(cfg.OPTIONS_CSV, newline='') as f:
            reader = csv.DictReader(f)
            candidates = []
            for row in reader:
                if (row.get('stock_symbol', '') == stock
                        and row.get('option_type', '') == direction):
                    strike = float(row.get('option_strike', 0))
                    expiry = row.get('option_expiry', '')
                    candidates.append({
                        'strike': strike,
                        'symbol': row.get('option_tradingsymbol', ''),
                        'lot_size': int(row.get('option_lot_size', 0)),
                        'expiry': expiry,
                    })

        if not candidates:
            logger.warning("No options found for %s %s in CSV", stock, direction)
            return None

        # Filter by min DTE
        now_dt = datetime.now()
        min_expiry = (now_dt + timedelta(days=cfg.MIN_DTE)).strftime('%Y-%m-%d')
        valid = [c for c in candidates if c['expiry'] >= min_expiry]
        if not valid:
            # Fallback: any future expiry
            today = now_dt.strftime('%Y-%m-%d')
            valid = [c for c in candidates if c['expiry'] >= today]
        if not valid:
            logger.warning("No valid expiry for %s %s (min DTE=%d)",
                           stock, direction, cfg.MIN_DTE)
            return None

        # Pick nearest valid expiry
        nearest_expiry = min(c['expiry'] for c in valid)
        same_expiry = [c for c in valid if c['expiry'] == nearest_expiry]

        # Find ATM strike (nearest to spot)
        atm = min(same_expiry, key=lambda c: abs(c['strike'] - spot))
        return atm

    except Exception as e:
        logger.error("Option lookup failed for %s: %s", stock, e)
        return None


def get_option_quote(kite, option_symbol: str) -> Optional[dict]:
    """Get bid/ask/LTP for an option. Returns {bid, ask, ltp, spread_pct}."""
    try:
        key = f"NFO:{option_symbol}"
        data = kite.quote([key])
        if key not in data:
            return None

        q = data[key]
        depth = q.get('depth', {})
        buy_depth = depth.get('buy', [])
        sell_depth = depth.get('sell', [])

        bid = buy_depth[0]['price'] if buy_depth and buy_depth[0]['price'] > 0 else 0
        ask = sell_depth[0]['price'] if sell_depth and sell_depth[0]['price'] > 0 else 0

        mid = (bid + ask) / 2 if bid > 0 and ask > 0 else 0
        spread_pct = ((ask - bid) / mid) if mid > 0 else 999

        return {
            'bid': bid,
            'ask': ask,
            'ltp': q.get('last_price', 0),
            'mid': round(mid, 2),
            'spread_pct': round(spread_pct, 4),
            'oi': q.get('oi', 0),
            'volume': q.get('volume', 0),
        }
    except Exception as e:
        logger.error("Quote fetch failed for %s: %s", option_symbol, e)
        return None


# ── Alert Message Builder ─────────────────────────────────────────────────

def build_entry_alert(stock: str, side: str, gap_pct: float,
                      spot: float, st_value: float,
                      option: dict, quote: dict,
                      lots: int, cost: float,
                      skip_reason: str = '') -> str:
    """Build Telegram alert message in frozen format."""
    icon = '\U0001f7e2' if side == 'UP' else '\U0001f534'  # green/red circle
    direction = 'CE' if side == 'UP' else 'PE'

    spread_str = f"{quote['spread_pct']*100:.1f}%"

    msg = (
        f"{icon} FLOW ENTRY \u2014 <code>{stock}</code> ({side}) {gap_pct*100:.2f}%\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Spot: \u20b9{spot:,.2f} | Level: \u20b9{st_value:,.2f}\n"
        f"\n"
        f"<code>{option['symbol']}</code>\n"
        f"Ask: \u20b9{quote['ask']:.2f} | Bid: \u20b9{quote['bid']:.2f} | "
        f"Spread: {spread_str}\n"
        f"Lots: {lots} ({lots * option['lot_size']} qty) | "
        f"Cost: \u20b9{cost:,.0f}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
    )

    if skip_reason:
        msg += f"\n\u26a0\ufe0f SKIP: {skip_reason}"

    return msg


def build_exit_alert(trade: dict, reason: str, current_spot: float,
                     current_premium: float = None) -> str:
    """Build Telegram exit/SL/TP alert message."""
    icon = '\U0001f6d1' if 'SL' in reason else '\U0001f3af'  # stop sign / target
    pnl = trade.get('pnl', 0) or 0
    pnl_pct = trade.get('pnl_pct', 0) or 0
    pnl_icon = '\u2705' if pnl >= 0 else '\u274c'

    msg = (
        f"{icon} FLOW EXIT \u2014 <code>{trade['stock']}</code> "
        f"({trade['side']})\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"Reason: {reason}\n"
        f"Spot: \u20b9{current_spot:,.2f}\n"
    )
    if current_premium is not None:
        msg += f"Premium: \u20b9{current_premium:.2f}\n"
    msg += (
        f"{pnl_icon} P&L: \u20b9{pnl:,.0f} ({pnl_pct:+.1f}%)\n"
        f"Days held: {trade.get('days_held', 0)}\n"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        f"\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
    )
    return msg


def build_pre_close_alert(trades: list, ltp_map: Dict[str, float]) -> str:
    """Build 2:45 PM pre-close alert for all open trades."""
    msg = (
        "\U0001f553 FLOW PRE-CLOSE (2:45 PM)\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
    )
    for t in trades:
        spot = ltp_map.get(t['stock'], 0)
        entry = t.get('entry_spot', 0) or 0
        if entry > 0 and spot > 0:
            if t['side'] == 'UP':
                spot_pnl_pct = (spot - entry) / entry * 100
            else:
                spot_pnl_pct = (entry - spot) / entry * 100
        else:
            spot_pnl_pct = 0

        msg += (
            f"  <code>{t['stock']}</code> ({t['side']}) "
            f"Entry: \u20b9{entry:,.2f} \u2192 Now: \u20b9{spot:,.2f} "
            f"({spot_pnl_pct:+.1f}%)\n"
        )

    msg += (
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        "Reply to hold overnight or exit manually."
    )
    return msg


def build_skip_alert(stock: str, side: str, gap_pct: float,
                     spot: float, st_value: float,
                     option: dict, reason: str) -> str:
    """Build alert for skipped entries (capital insufficient, liquidity, etc)."""
    icon = '\u26a0\ufe0f'
    msg = (
        f"{icon} FLOW SKIP \u2014 <code>{stock}</code> ({side}) "
        f"{gap_pct*100:.2f}%\n"
        f"Spot: \u20b9{spot:,.2f} | Level: \u20b9{st_value:,.2f}\n"
        f"<code>{option['symbol']}</code>\n"
        f"Reason: {reason}\n"
    )
    return msg

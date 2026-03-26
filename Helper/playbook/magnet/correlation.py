"""Market correlation analysis for magnet signals.

Computes 3-month Pearson correlation between signal stock and:
1. NIFTY 50 (market benchmark)
2. Other currently watching/entered stocks (portfolio overlap)

Used to decide: "respect the market" vs "trust the stock setup."

Usage:
    from playbook.magnet.correlation import compute_correlations, format_correlation_block

    corr = compute_correlations('SUNPHARMA', kite, get_token_fn,
                                other_symbols=['CIPLA', 'TATAPOWER'],
                                daily_data=daily_candles)
    block = format_correlation_block(corr, market_score=3)
    # -> Telegram HTML string
"""

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# NIFTY 50 instrument token (NSE index, constant)
_NIFTY_TOKEN = 256265

# Number of trading days for correlation window (~3 months)
CORR_DAYS = 60

# Minimum common dates needed for a meaningful correlation
_MIN_COMMON = 20

# Per-session cache: {key: {'date': 'YYYY-MM-DD', 'pairs': [(date_str, close)]}}
# key = symbol or 'NIFTY50'
_closes_cache: dict = {}


# ---------------------------------------------------------------------------
#  Core math (pure Python, no numpy)
# ---------------------------------------------------------------------------

def _daily_returns(closes: list) -> list:
    """Compute daily simple returns from a list of closing prices.

    Always produces len(closes)-1 entries to preserve alignment.
    Zero-close (impossible on NSE) is treated as zero return.
    """
    returns = []
    for i in range(1, len(closes)):
        prev = closes[i - 1]
        if prev == 0:
            returns.append(0.0)
        else:
            returns.append((closes[i] - prev) / prev)
    return returns


def _pearson(x: list, y: list):
    """Pearson correlation coefficient. Returns None if insufficient data.

    Uses the LAST n entries (most recent) when lists differ in length.
    Returns None if either series has near-zero variance (constant prices).
    """
    n = min(len(x), len(y))
    if n < _MIN_COMMON:
        return None
    x, y = x[-n:], y[-n:]

    mean_x = sum(x) / n
    mean_y = sum(y) / n

    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    var_x = sum((xi - mean_x) ** 2 for xi in x)
    var_y = sum((yi - mean_y) ** 2 for yi in y)

    # Epsilon check: variance < 1e-12 means series is effectively constant
    # (e.g., stock price unchanged for 60 days — no meaningful correlation)
    if var_x < 1e-12 or var_y < 1e-12:
        return None

    den = math.sqrt(var_x * var_y)
    return round(num / den, 4)


# ---------------------------------------------------------------------------
#  Data extraction & alignment
# ---------------------------------------------------------------------------

def _extract_date_closes(candles: list, max_days: int = 120) -> list:
    """Extract (date_str, close) pairs from daily candle data.

    Handles both datetime objects and ISO strings from Kite API.
    Returns at most max_days most recent pairs.
    """
    pairs = []
    for c in candles[-max_days:]:
        dt = c['date']
        if isinstance(dt, str):
            date_str = dt[:10]
        elif hasattr(dt, 'strftime'):
            date_str = dt.strftime('%Y-%m-%d')
        else:
            continue
        pairs.append((date_str, c['close']))
    return pairs


def _aligned_correlation(pairs_a: list, pairs_b: list):
    """Compute Pearson correlation of daily returns aligned by common dates.

    Args:
        pairs_a: [(date_str, close), ...] for stock A
        pairs_b: [(date_str, close), ...] for stock B

    Returns: float correlation or None
    """
    dict_a = {d: c for d, c in pairs_a}
    dict_b = {d: c for d, c in pairs_b}
    common = sorted(set(dict_a) & set(dict_b))

    # Use last CORR_DAYS+1 common dates (+1 because returns need N+1 prices)
    common = common[-(CORR_DAYS + 1):]

    if len(common) < _MIN_COMMON + 1:
        return None

    closes_a = [dict_a[d] for d in common]
    closes_b = [dict_b[d] for d in common]

    returns_a = _daily_returns(closes_a)
    returns_b = _daily_returns(closes_b)

    return _pearson(returns_a, returns_b)


# ---------------------------------------------------------------------------
#  Data fetching (with daily cache)
# ---------------------------------------------------------------------------

def _fetch_date_closes(kite, token: int, days: int = 100) -> list:
    """Fetch daily OHLC from Kite, return [(date_str, close), ...]."""
    now = datetime.now()
    start = now - timedelta(days=days)
    try:
        data = kite.historical_data(
            token, start.strftime('%Y-%m-%d'),
            now.strftime('%Y-%m-%d'), 'day')
        return _extract_date_closes(data, max_days=days)
    except Exception as e:
        logger.warning("Correlation: fetch failed for token %s: %s", token, e)
        return []


def _get_cached_pairs(kite, symbol: str, token: int) -> list:
    """Get (date, close) pairs with daily cache. 1 API call per symbol per day."""
    today = datetime.now().strftime('%Y-%m-%d')
    cached = _closes_cache.get(symbol)
    if cached and cached.get('date') == today:
        return cached['pairs']

    pairs = _fetch_date_closes(kite, token, days=100)
    if pairs:
        _closes_cache[symbol] = {'date': today, 'pairs': pairs}
    return pairs


# ---------------------------------------------------------------------------
#  Labels & icons
# ---------------------------------------------------------------------------

def _corr_label(r: float) -> str:
    """Human-readable correlation label."""
    ar = abs(r)
    if ar >= 0.70:
        return 'HIGH'
    elif ar >= 0.40:
        return 'MOD'
    else:
        return 'LOW'


def _corr_icon(r: float) -> str:
    """Telegram icon for NIFTY correlation level.

    Sign matters: positive HIGH = stock follows market (⚠️ if market weak).
    Negative HIGH = stock moves against market (contrarian, less common).
    """
    ar = abs(r)
    if ar < 0.40:
        return '\u2705'         # ✅ independent
    elif r < 0:
        return '\U0001f504'     # 🔄 inverse
    elif ar >= 0.70:
        return '\u26a0\ufe0f'   # ⚠️ highly linked
    else:
        return '~'              # moderate


# ---------------------------------------------------------------------------
#  Main API
# ---------------------------------------------------------------------------

def compute_correlations(symbol: str, kite, get_token_fn,
                         other_symbols: list = None,
                         daily_data: list = None) -> dict:
    """Compute 3-month correlations for a signal stock.

    Args:
        symbol:        Stock symbol (e.g., 'SUNPHARMA')
        kite:          KiteConnect instance
        get_token_fn:  fn(kite, symbol) -> instrument_token
        other_symbols: Other watching/entered symbols to check overlap
        daily_data:    Pre-fetched daily candles for signal stock (avoids re-fetch)

    Returns:
        {
            'nifty': {'corr': 0.72, 'label': 'HIGH', 'icon': '⚠️'} or None,
            'stocks': {'CIPLA': {'corr': 0.81, ...}, ...},
            'nifty_corr': 0.72 or None,
        }
    """
    result = {
        'nifty': None,
        'stocks': {},
        'nifty_corr': None,
    }

    # 1. Signal stock's (date, close) pairs
    if daily_data:
        stock_pairs = _extract_date_closes(daily_data, max_days=CORR_DAYS + 10)
    else:
        try:
            tok = get_token_fn(kite, symbol)
            stock_pairs = _get_cached_pairs(kite, symbol, tok)
        except Exception as e:
            logger.warning("Correlation: can't get data for %s: %s", symbol, e)
            return result

    if len(stock_pairs) < _MIN_COMMON + 1:
        logger.warning("Correlation: insufficient data for %s (%d days)",
                        symbol, len(stock_pairs))
        return result

    # 2. NIFTY 50 correlation
    try:
        nifty_pairs = _get_cached_pairs(kite, 'NIFTY50', _NIFTY_TOKEN)
        if nifty_pairs and len(nifty_pairs) >= _MIN_COMMON + 1:
            n_corr = _aligned_correlation(stock_pairs, nifty_pairs)
            if n_corr is not None:
                result['nifty'] = {
                    'corr': round(n_corr, 2),
                    'label': _corr_label(n_corr),
                    'icon': _corr_icon(n_corr),
                }
                result['nifty_corr'] = round(n_corr, 2)
    except Exception as e:
        logger.warning("Correlation: NIFTY fetch failed: %s", e)

    # 3. Other watched/entered stocks (cap at 5 to limit API calls)
    if other_symbols:
        for other_sym in other_symbols[:5]:
            if other_sym == symbol:
                continue
            try:
                other_tok = get_token_fn(kite, other_sym)
                other_pairs = _get_cached_pairs(kite, other_sym, other_tok)
                if other_pairs and len(other_pairs) >= _MIN_COMMON + 1:
                    r = _aligned_correlation(stock_pairs, other_pairs)
                    if r is not None:
                        result['stocks'][other_sym] = {
                            'corr': round(r, 2),
                            'label': _corr_label(r),
                            'icon': _corr_icon(r),
                        }
            except Exception as e:
                logger.debug("Correlation: skip %s: %s", other_sym, e)

    return result


def get_sector_breadth(symbol: str) -> dict:
    """Look up a stock's sector and latest sector breadth.

    Reads from breadth_readings.json (written by st_watch breadth tracker).
    Zero API calls — pure local file read.

    Returns: {'sector': 'Metal', 'breadth_pct': 18.0, 'above': 4, 'total': 32,
              'sector_key': 'nifty_metal'} or None.
    """
    try:
        from playbook.backtest_cache._sector_mapping import get_sector
        from playbook.st_watch.breadth_tracker import get_latest, SECTOR_LABELS

        sector_key = get_sector(symbol)
        if sector_key == 'other':
            return None

        latest = get_latest()
        if not latest:
            return None

        for s in latest.get('sectors', []):
            if s['sector'] == sector_key:
                return {
                    'sector': SECTOR_LABELS.get(sector_key, sector_key),
                    'sector_key': sector_key,
                    'breadth_pct': s['breadth_pct'],
                    'above': s['above'],
                    'total': s['total'],
                }
        return None
    except Exception as e:
        logger.debug("Sector breadth lookup failed for %s: %s", symbol, e)
        return None


def format_correlation_block(corr_data: dict, market_score: int = None,
                             sector_info: dict = None) -> str:
    """Format correlation data as Telegram HTML block.

    Args:
        corr_data:    Output of compute_correlations()
        market_score: Market outlook score (0-7) from confidence scorer.
                      Used to combine correlation + current market state in verdict.
        sector_info:  Output of get_sector_breadth() — stock's sector + breadth.

    Returns: Multi-line HTML string, or '' if no data.
    """
    if not corr_data or not corr_data.get('nifty'):
        # Still show sector info even without correlation data
        if sector_info:
            sb = sector_info
            sec_icon = '\u26a0\ufe0f' if sb['breadth_pct'] < 30 else (
                '\u2705' if sb['breadth_pct'] >= 50 else '~')
            return (f"\U0001f4ca {sb['sector']}: {sb['breadth_pct']:.0f}% breadth "
                    f"({sb['above']}/{sb['total']}) {sec_icon}")
        return ''

    n = corr_data['nifty']
    nc = n['corr']

    lines = ['\U0001f4ca <b>Correlation</b> (3M):']   # 📊

    # NIFTY line
    lines.append(f"  vs NIFTY: {nc:+.2f} {n['label']} {n['icon']}")

    # Other stocks (top 3 by absolute correlation, descending)
    stocks = corr_data.get('stocks', {})
    if stocks:
        sorted_stocks = sorted(stocks.items(),
                               key=lambda x: abs(x[1]['corr']), reverse=True)
        for sym, data in sorted_stocks[:3]:
            tag = ' (same trade!)' if abs(data['corr']) >= 0.75 else ''
            lines.append(f"  vs {sym}: {data['corr']:+.2f} {data['label']}{tag}")

    # Sector breadth line
    if sector_info:
        sb = sector_info
        if sb['breadth_pct'] < 30:
            sec_tag = 'weak sector \u26a0\ufe0f'
        elif sb['breadth_pct'] >= 60:
            sec_tag = 'strong sector \u2705'
        elif sb['breadth_pct'] >= 40:
            sec_tag = 'neutral'
        else:
            sec_tag = 'soft sector'
        lines.append(f"  {sb['sector']}: {sb['breadth_pct']:.0f}% breadth "
                     f"({sb['above']}/{sb['total']}) {sec_tag}")

    # Verdict — combine correlation level with market state
    verdict = _build_verdict(nc, market_score)
    if verdict:
        lines.append(f"  \u2192 {verdict}")   # →

    return '\n'.join(lines)


def _build_verdict(nifty_corr: float, market_score: int = None) -> str:
    """Actionable verdict combining NIFTY correlation + market health.

    Sign-aware: positive corr = moves WITH market, negative = AGAINST.
    market_score: 0-7 from confidence scorer's market outlook dimension.
        7 = bullish, 5 = mixed, 3 = weak, 1 = bearish.
    """
    if nifty_corr is None:
        return ''

    ac = abs(nifty_corr)
    is_positive = nifty_corr >= 0

    # Determine market state from score
    if market_score is not None:
        mkt_strong = market_score >= 5
        mkt_weak = market_score <= 2
    else:
        mkt_strong = None
        mkt_weak = None

    # LOW correlation — independent regardless of sign
    if ac < 0.40:
        return 'Independent mover \u2705 trust stock setup'

    # NEGATIVE correlation — stock moves AGAINST market (rare for F&O stocks)
    if not is_positive:
        if ac >= 0.70:
            if mkt_weak:
                return 'Inverse to market + NIFTY weak \u2705 contrarian edge'
            elif mkt_strong:
                return 'Inverse to market + NIFTY strong \u26a0\ufe0f headwind'
            else:
                return 'Inverse to market \u2014 contrarian stock'
        else:
            return 'Weakly inverse \u2014 mild contrarian, weigh setup'

    # POSITIVE correlation — stock moves WITH market
    if ac >= 0.70:
        if mkt_weak:
            return 'Market-linked + NIFTY weak \u26a0\ufe0f setup riskier'
        elif mkt_strong:
            return 'Market-linked + NIFTY strong \u2705 tailwind'
        else:
            return 'Market-linked \u2014 respect NIFTY direction'
    else:
        if mkt_weak:
            return 'Moderate link + weak market \u2014 weigh carefully'
        elif mkt_strong:
            return 'Moderate link + strong market \u2014 setup OK'
        else:
            return 'Moderate link \u2014 weigh market + stock setup'

"""Market correlation + sector context for magnet signals.

Computes 2-month Pearson correlation between signal stock and NIFTY 50.
Looks up sector breadth (with intraday velocity) from st_watch data.

Used to decide: "respect the market" vs "trust the stock setup."

Usage:
    from playbook.magnet.correlation import (
        compute_nifty_correlation, get_sector_breadth, format_context_line,
    )

    corr = compute_nifty_correlation('SUNPHARMA', kite, get_token_fn,
                                      daily_data=daily_candles)
    sector = get_sector_breadth('SUNPHARMA')
    line = format_context_line(corr, market_score=3, sector_info=sector)
    # -> "NIFTY +0.58 MOD | Pharma 55% ↑+8 (21/38)"
"""

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# NIFTY 50 instrument token (NSE index, constant)
_NIFTY_TOKEN = 256265

# Number of trading days for correlation window (~2 months)
CORR_DAYS = 42

# Minimum common dates needed for a meaningful correlation
_MIN_COMMON = 15

# Per-session cache: {key: {'date': 'YYYY-MM-DD', 'pairs': [(date_str, close)]}}
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

    if var_x < 1e-12 or var_y < 1e-12:
        return None

    den = math.sqrt(var_x * var_y)
    return round(num / den, 4)


# ---------------------------------------------------------------------------
#  Data extraction & alignment
# ---------------------------------------------------------------------------

def _extract_date_closes(candles: list, max_days: int = 80) -> list:
    """Extract (date_str, close) pairs from daily candle data.

    Handles both datetime objects and ISO strings from Kite API.
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
    """Compute Pearson correlation of daily returns aligned by common dates."""
    dict_a = {d: c for d, c in pairs_a}
    dict_b = {d: c for d, c in pairs_b}
    common = sorted(set(dict_a) & set(dict_b))

    common = common[-(CORR_DAYS + 1):]

    if len(common) < _MIN_COMMON + 1:
        return None

    closes_a = [dict_a[d] for d in common]
    closes_b = [dict_b[d] for d in common]

    return _pearson(_daily_returns(closes_a), _daily_returns(closes_b))


# ---------------------------------------------------------------------------
#  Data fetching (with daily cache)
# ---------------------------------------------------------------------------

def _fetch_date_closes(kite, token: int, days: int = 75) -> list:
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

    pairs = _fetch_date_closes(kite, token)
    if pairs:
        _closes_cache[symbol] = {'date': today, 'pairs': pairs}
    return pairs


# ---------------------------------------------------------------------------
#  Labels
# ---------------------------------------------------------------------------

def _corr_label(r: float) -> str:
    ar = abs(r)
    if ar >= 0.70:
        return 'HIGH'
    elif ar >= 0.40:
        return 'MOD'
    else:
        return 'LOW'


# ---------------------------------------------------------------------------
#  Main API
# ---------------------------------------------------------------------------

def compute_nifty_correlation(symbol: str, kite, get_token_fn,
                              daily_data: list = None) -> dict:
    """Compute 2-month correlation between signal stock and NIFTY 50.

    Args:
        symbol:        Stock symbol (e.g., 'SUNPHARMA')
        kite:          KiteConnect instance
        get_token_fn:  fn(kite, symbol) -> instrument_token
        daily_data:    Pre-fetched daily candles for signal stock (avoids re-fetch)

    Returns: {'corr': 0.58, 'label': 'MOD'} or None.
    """
    # Signal stock's (date, close) pairs
    if daily_data:
        stock_pairs = _extract_date_closes(daily_data, max_days=CORR_DAYS + 10)
    else:
        try:
            tok = get_token_fn(kite, symbol)
            stock_pairs = _get_cached_pairs(kite, symbol, tok)
        except Exception as e:
            logger.warning("Correlation: can't get data for %s: %s", symbol, e)
            return None

    if len(stock_pairs) < _MIN_COMMON + 1:
        return None

    # NIFTY 50 correlation
    try:
        nifty_pairs = _get_cached_pairs(kite, 'NIFTY50', _NIFTY_TOKEN)
        if not nifty_pairs or len(nifty_pairs) < _MIN_COMMON + 1:
            return None
        n_corr = _aligned_correlation(stock_pairs, nifty_pairs)
        if n_corr is not None:
            return {'corr': round(n_corr, 2), 'label': _corr_label(n_corr)}
    except Exception as e:
        logger.warning("Correlation: NIFTY fetch failed: %s", e)

    return None


def get_sector_breadth(symbol: str) -> dict:
    """Look up a stock's sector, latest breadth, and 5-day velocity.

    Reads from breadth_readings.json (written by st_watch breadth tracker).
    Zero API calls — pure local file read.

    Returns: {'sector': 'Metal', 'breadth_pct': 18.0, 'above': 4, 'total': 32,
              'vel_5d': +12.0} or None.
    vel_5d = 5-day sector breadth change (momentum direction). None if insufficient data.
    """
    try:
        from playbook.backtest_cache._sector_mapping import get_sector
        from playbook.st_watch.breadth_tracker import (
            get_latest, SECTOR_LABELS, _load_readings,
        )

        sector_key = get_sector(symbol)
        if sector_key == 'other':
            return None

        latest = get_latest()
        if not latest:
            return None

        # Find sector in latest reading
        sector_data = None
        for s in latest.get('sectors', []):
            if s['sector'] == sector_key:
                sector_data = s
                break
        if not sector_data:
            return None

        label = SECTOR_LABELS.get(sector_key, sector_key)

        # Compute 5-day velocity: last reading from ~5 days ago vs latest
        vel_5d = None
        try:
            readings = _load_readings()
            if readings:
                latest_date = latest.get('date', '')
                # Find last reading from 4-6 days ago (allow weekends)
                cutoff = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
                older = [r for r in readings
                         if cutoff <= r.get('date', '') < latest_date
                         and r.get('sectors')]
                if older:
                    # Use last reading of the oldest qualifying day
                    ref = older[0]
                    for rs in ref.get('sectors', []):
                        if rs['sector'] == sector_key:
                            vel_5d = round(sector_data['breadth_pct'] - rs['breadth_pct'], 1)
                            break
        except Exception:
            pass  # velocity is optional

        return {
            'sector': label,
            'sector_key': sector_key,
            'breadth_pct': sector_data['breadth_pct'],
            'above': sector_data['above'],
            'total': sector_data['total'],
            'vel_5d': vel_5d,
        }
    except Exception as e:
        logger.debug("Sector breadth lookup failed for %s: %s", symbol, e)
        return None


def format_context_line(nifty_corr: dict, market_score: int = None,
                        sector_info: dict = None) -> str:
    """Format compact context line for Telegram alert.

    Output: "NIFTY +0.58 MOD | Pharma 55% ↑+8 (21/38)"
    Plus verdict line: "→ Setup OK — moderate link, market supports"

    Returns multi-line string, or '' if no data.
    """
    parts = []

    # NIFTY correlation
    if nifty_corr:
        nc = nifty_corr['corr']
        parts.append(f"NIFTY {nc:+.2f} {nifty_corr['label']}")

    # Sector breadth + 5-day velocity
    if sector_info:
        sb = sector_info
        vel_str = ''
        if sb.get('vel_5d') is not None:
            arrow = '\u2191' if sb['vel_5d'] >= 0 else '\u2193'  # ↑ or ↓
            vel_str = f" {arrow}{sb['vel_5d']:+.0f}/5d"
        parts.append(f"{sb['sector']} {sb['breadth_pct']:.0f}%{vel_str} "
                     f"({sb['above']}/{sb['total']})")

    if not parts:
        return ''

    lines = [' | '.join(parts)]

    # Verdict
    if nifty_corr:
        verdict = _build_verdict(nifty_corr['corr'], market_score, sector_info)
        if verdict:
            lines.append(f"\u2192 {verdict}")   # →

    return '\n'.join(lines)


def _build_verdict(nifty_corr: float, market_score: int = None,
                   sector_info: dict = None) -> str:
    """Actionable verdict combining NIFTY correlation + market + sector.

    Sign-aware: positive corr = moves WITH market, negative = AGAINST.
    market_score: 0-7 from confidence scorer's market outlook dimension.
    sector_info: from get_sector_breadth() — adds sector strength to verdict.
    """
    if nifty_corr is None:
        return ''

    ac = abs(nifty_corr)
    is_positive = nifty_corr >= 0

    # Determine market state
    if market_score is not None:
        mkt_strong = market_score >= 5
        mkt_weak = market_score <= 2
    else:
        mkt_strong = None
        mkt_weak = None

    # Sector suffix
    sec_suffix = ''
    if sector_info:
        bp = sector_info['breadth_pct']
        if bp < 30:
            sec_suffix = ', weak sector'
        elif bp >= 60:
            sec_suffix = ', strong sector'

    # LOW correlation — independent
    if ac < 0.40:
        return f'Independent \u2705 trust setup{sec_suffix}'

    # NEGATIVE — contrarian
    if not is_positive:
        if ac >= 0.70:
            if mkt_weak:
                return f'Inverse + NIFTY weak \u2705 contrarian edge{sec_suffix}'
            elif mkt_strong:
                return f'Inverse + NIFTY strong \u26a0\ufe0f headwind{sec_suffix}'
            return f'Inverse \u2014 contrarian{sec_suffix}'
        return f'Weakly inverse{sec_suffix}'

    # POSITIVE HIGH — market-linked
    if ac >= 0.70:
        if mkt_weak:
            return f'Market-linked + weak \u26a0\ufe0f riskier{sec_suffix}'
        elif mkt_strong:
            return f'Market-linked + strong \u2705 tailwind{sec_suffix}'
        return f'Market-linked \u2014 respect NIFTY{sec_suffix}'

    # POSITIVE MODERATE
    if mkt_weak:
        return f'Moderate link + weak mkt \u2014 caution{sec_suffix}'
    elif mkt_strong:
        return f'Moderate link \u2705 setup OK{sec_suffix}'
    return f'Moderate link{sec_suffix}'

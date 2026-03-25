"""Magnet Signal Confidence Scorer v2.

Two-part score:
  Signal Quality (0-60): Is this a good signal to trade?
  Entry Timing   (0-40): Should we enter NOW or wait?

Usage:
    cd Helper
    python -m playbook.magnet confidence SUNPHARMA
    python -m playbook.magnet confidence SUNPHARMA SUZLON --target weekly
    python -m playbook.magnet confidence --from-magnet
    python -m playbook.magnet confidence SUNPHARMA --json

Signal Quality (60 pts):
     1. Higher TF Support  (13) - Monthly trend supports the required move
     2. TF Alignment       (10) - Daily + Hourly ST direction alignment
     3. Gap to Target      (10) - Proximity to target ST line
     4. ATR Reachability    (7) - Gap relative to daily volatility
     5. Gap Velocity        (8) - Steady gap narrowing over 5 days
     6. RSI Context         (5) - Mean reversion fuel
     7. Market Outlook      (7) - NIFTY health: RSI, 50DMA, daily ST

Entry Timing (40 pts):
     7. Pullback Quality    (8) - Healthy pullback from recent highs/lows
     8. Intraday Pattern    (8) - 1hr/15min pullback in aligned trend = entry
     9. Volume Dynamics     (8) - Low vol pullback + high vol bounce
    10. Support Pattern     (8) - Double/triple bottom (CE) or top (PE)
    11. Reversal Candle     (8) - Hammer, engulfing at key levels

Grades:  80+ = HIGH | 65-79 = MODERATE-HIGH | 50-64 = MODERATE
         35-49 = LOW | <35 = SKIP
"""

import json
import sys
import argparse
from datetime import datetime, timedelta
from pathlib import Path

# Paths
_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent
_HELPER = _PLAYBOOK.parent
_BOTS = _HELPER.parent
_KITE_TOKEN = _BOTS / 'data' / 'kite_access_token.json'

sys.path.insert(0, str(_HELPER))
from playbook.compute_st import (
    aggregate_to_monthly, aggregate_to_weekly, compute_supertrend,
)


# --- Kite helpers -----------------------------------------------------------

_kite = None
_token_cache = {}


def _get_kite():
    global _kite
    if _kite:
        return _kite
    from kiteconnect import KiteConnect

    if not _KITE_TOKEN.exists():
        raise FileNotFoundError(f"Token not found: {_KITE_TOKEN}")
    with open(_KITE_TOKEN) as f:
        tok = json.load(f)
    _kite = KiteConnect(api_key=tok['api_key'])
    _kite.set_access_token(tok['access_token'])
    return _kite


def _get_instrument_token(kite, symbol):
    if not _token_cache:
        for inst in kite.instruments('NSE'):
            if inst['segment'] == 'NSE' and inst['instrument_type'] == 'EQ':
                _token_cache[inst['tradingsymbol']] = inst['instrument_token']
    tok = _token_cache.get(symbol)
    if not tok:
        raise ValueError(f"NSE symbol not found: {symbol}")
    return tok


def _fetch_daily(kite, token, years=6):
    """Fetch daily OHLC in 2 chunks (avoids Kite 2000-day limit)."""
    now = datetime.now()
    start = now - timedelta(days=365 * years)
    if years > 5:
        mid = now - timedelta(days=365 * 3)
        c1 = kite.historical_data(
            token, start.strftime('%Y-%m-%d'), mid.strftime('%Y-%m-%d'), 'day')
        c2 = kite.historical_data(
            token, (mid + timedelta(days=1)).strftime('%Y-%m-%d'),
            now.strftime('%Y-%m-%d'), 'day')
        return c1 + c2
    return kite.historical_data(
        token, start.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d'), 'day')


def _fetch_hourly(kite, token, days=30):
    """Fetch 60-minute candles (for 1hr ST computation)."""
    now = datetime.now()
    start = now - timedelta(days=days)
    return kite.historical_data(
        token, start.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d'),
        '60minute')


def _fetch_15min(kite, token, days=5):
    """Fetch 15-minute candles (for entry timing)."""
    now = datetime.now()
    start = now - timedelta(days=days + 2)  # pad for weekends
    return kite.historical_data(
        token, start.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d'),
        '15minute')


# --- Technical computations -------------------------------------------------

def _compute_all_st(daily):
    """Monthly, Weekly, Daily ST(10,3) from daily candles."""
    monthly = aggregate_to_monthly(daily, exclude_current=True)
    weekly = aggregate_to_weekly(daily, exclude_current=True)
    st_m = compute_supertrend(monthly, 10, 3)
    st_w = compute_supertrend(weekly, 10, 3)
    st_d = compute_supertrend(daily, 10, 3)
    return {
        'monthly': st_m[-1] if st_m else None,
        'weekly': st_w[-1] if st_w else None,
        'daily': st_d[-1] if st_d else None,
    }


def _compute_hourly_st(hourly):
    """Hourly ST(10,3) from 60-minute candles."""
    if not hourly or len(hourly) < 15:
        return None
    st = compute_supertrend(hourly, 10, 3)
    return st[-1] if st else None


def _atr(daily, period=14):
    trs = []
    for i in range(1, len(daily)):
        h, l, pc = daily[i]['high'], daily[i]['low'], daily[i - 1]['close']
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-period:]) / period if len(trs) >= period else 0


def _rsi(daily, period=14):
    if len(daily) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(len(daily) - period, len(daily)):
        chg = daily[i]['close'] - daily[i - 1]['close']
        gains.append(max(0, chg))
        losses.append(max(0, -chg))
    ag = sum(gains) / period
    al = sum(losses) / period
    return 100 - (100 / (1 + ag / al)) if al > 0 else 100


_nifty_cache = None
_nifty_cache_ts = 0  # timestamp of last successful cache
_NIFTY_CACHE_TTL = 1800  # 30 minutes


def _compute_market_outlook(kite):
    """Compute market health from NIFTY 50: RSI(14), 50DMA, daily ST.

    Returns dict with: nifty_rsi, above_50dma, nifty_st_dir, score (0-7), detail.
    Cache expires every 30 minutes so intraday changes are reflected.
    """
    import time as _time
    global _nifty_cache, _nifty_cache_ts
    if _nifty_cache is not None and (_time.time() - _nifty_cache_ts) < _NIFTY_CACHE_TTL:
        return _nifty_cache

    try:
        # NIFTY 50 instrument token = 256265
        now = datetime.now()
        nifty_daily = kite.historical_data(
            256265,
            (now - timedelta(days=120)).strftime('%Y-%m-%d'),
            now.strftime('%Y-%m-%d'), 'day')

        if len(nifty_daily) < 55:
            _nifty_cache = {'score': 4, 'detail': 'Insufficient NIFTY data',
                            'nifty_rsi': 50, 'above_50dma': None,
                            'nifty_st_dir': None}
            return _nifty_cache

        nifty_close = nifty_daily[-1]['close']

        # RSI(14)
        nifty_rsi = _rsi(nifty_daily)

        # 50 DMA
        closes_50 = [c['close'] for c in nifty_daily[-50:]]
        dma50 = sum(closes_50) / len(closes_50)
        above_50dma = nifty_close > dma50

        # Daily ST direction
        nifty_st = compute_supertrend(nifty_daily, 10, 3)
        nifty_st_dir = nifty_st[-1]['direction'] if nifty_st else None

        # Count bullish signals
        signals_on = 0
        if nifty_rsi > 50:
            signals_on += 1
        if above_50dma:
            signals_on += 1
        if nifty_st_dir == 'UP':
            signals_on += 1

        if signals_on == 3:
            sc = 7
            desc = (f"NIFTY healthy: RSI {nifty_rsi:.0f}, "
                    f"above 50DMA, ST UP")
        elif signals_on == 2:
            sc = 5
            desc = (f"NIFTY mixed ({signals_on}/3): RSI {nifty_rsi:.0f}, "
                    f"{'above' if above_50dma else 'below'} 50DMA, "
                    f"ST {nifty_st_dir or '?'}")
        elif signals_on == 1:
            sc = 3
            desc = (f"NIFTY weak ({signals_on}/3): RSI {nifty_rsi:.0f}, "
                    f"{'above' if above_50dma else 'below'} 50DMA, "
                    f"ST {nifty_st_dir or '?'}")
        else:
            sc = 1
            desc = (f"NIFTY bearish (0/3): RSI {nifty_rsi:.0f}, "
                    f"below 50DMA, ST DOWN")

        _nifty_cache_ts = _time.time()
        _nifty_cache = {
            'score': sc, 'detail': desc,
            'nifty_rsi': nifty_rsi, 'above_50dma': above_50dma,
            'nifty_st_dir': nifty_st_dir,
        }
        return _nifty_cache
    except Exception as e:
        # Do NOT cache errors -- next symbol should retry NIFTY fetch
        return {'score': 4, 'detail': f'NIFTY data error: {e}',
                'nifty_rsi': 50, 'above_50dma': None,
                'nifty_st_dir': None}


def _auto_target(st_data, ltp):
    """Find which ST line is closest to price (within 5%)."""
    best_tf, best_st, best_gap = None, None, 999
    for tf in ['weekly', 'monthly']:
        st = st_data.get(tf)
        if st:
            gap = abs(ltp - st['supertrend']) / st['supertrend'] * 100
            if gap < best_gap and gap <= 5:
                best_tf, best_st, best_gap = tf, st, gap
    return best_tf, best_st


# --- Pattern detectors -------------------------------------------------------

def _detect_support_pattern(daily, need_up, n=20):
    """Detect double/triple bottom (CE) or top (PE) in last n candles.

    Returns: (tests, pattern_name, level)
    """
    candles = daily[-n:]
    if not candles:
        return 0, 'none', 0

    if need_up:
        vals = [c['low'] for c in candles]
        extreme = min(vals)
        tol = extreme * 0.012  # 1.2% band
        test_idx = [i for i, v in enumerate(vals) if v <= extreme + tol]
    else:
        vals = [c['high'] for c in candles]
        extreme = max(vals)
        tol = extreme * 0.012
        test_idx = [i for i, v in enumerate(vals) if v >= extreme - tol]

    # Cluster into distinct tests (at least 2 candles apart)
    distinct = []
    for idx in test_idx:
        if not distinct or idx - distinct[-1] >= 2:
            distinct.append(idx)

    count = len(distinct)
    if count >= 3:
        name = 'triple'
    elif count >= 2:
        name = 'double'
    elif count >= 1:
        name = 'single'
    else:
        name = 'none'

    return count, name, extreme


def _detect_reversal_candle(candles, need_up):
    """Detect reversal candle patterns in last 2-3 candles.

    Returns: (pattern_name, strength 0-8)
    """
    if len(candles) < 2:
        return 'none', 0

    c = candles[-1]
    p = candles[-2]

    body = abs(c['close'] - c['open'])
    upper_wick = c['high'] - max(c['close'], c['open'])
    lower_wick = min(c['close'], c['open']) - c['low']
    full_range = c['high'] - c['low']
    is_green = c['close'] > c['open']

    p_body = abs(p['close'] - p['open'])
    p_green = p['close'] > p['open']

    if full_range == 0:
        return 'none', 0

    if need_up:
        # Bullish engulfing: prev red, current green engulfs entire prev body
        if (not p_green and is_green and p_body > 0
                and c['close'] >= p['open'] and c['open'] <= p['close']):
            return 'bullish_engulfing', 8

        # Hammer: small body at top, long lower wick >= 2x body
        if (is_green and body > 0 and lower_wick >= 2 * body
                and upper_wick < body * 0.5):
            return 'hammer', 7

        # Strong green close (close in top 30% of range)
        if is_green and (c['close'] - c['low']) / full_range > 0.7:
            return 'strong_close', 5

        # Green candle after red (simple bounce)
        if is_green and not p_green:
            return 'bounce', 3

        # Doji at support (indecision, could reverse)
        if body / full_range < 0.15:
            return 'doji', 2
    else:
        # Bearish engulfing
        if (p_green and not is_green and p_body > 0
                and c['close'] <= p['open'] and c['open'] >= p['close']):
            return 'bearish_engulfing', 8

        # Shooting star: small body at bottom, long upper wick
        if (not is_green and body > 0 and upper_wick >= 2 * body
                and lower_wick < body * 0.5):
            return 'shooting_star', 7

        # Strong red close (close in bottom 30% of range)
        if not is_green and (c['high'] - c['close']) / full_range > 0.7:
            return 'strong_close', 5

        # Red candle after green
        if not is_green and p_green:
            return 'reversal', 3

        if body / full_range < 0.15:
            return 'doji', 2

    return 'none', 0


def _analyze_volume(daily, need_up):
    """Analyze volume: pullback vs bounce volume, today's volume.

    Returns: (score 0-8, detail_str)
    """
    r10 = daily[-10:]
    r20 = daily[-20:] if len(daily) >= 20 else daily[-len(daily):]
    vol_20d = sum(c.get('volume', 0) for c in r20) / len(r20)
    if vol_20d == 0:
        return 4, "No volume data"

    # Split recent candles into pullback (against direction) and with-direction
    pb_vols = []
    dir_vols = []
    for c in r10:
        vol = c.get('volume', 0)
        if need_up:
            if c['close'] < c['open']:  # red = pullback day
                pb_vols.append(vol)
            else:
                dir_vols.append(vol)
        else:
            if c['close'] > c['open']:  # green = pullback day for PE
                pb_vols.append(vol)
            else:
                dir_vols.append(vol)

    avg_pb = sum(pb_vols) / len(pb_vols) if pb_vols else vol_20d
    avg_dir = sum(dir_vols) / len(dir_vols) if dir_vols else vol_20d

    pb_ratio = avg_pb / vol_20d
    dir_ratio = avg_dir / vol_20d

    today = daily[-1]
    today_vol = today.get('volume', 0)
    today_with_dir = (today['close'] > today['open']) if need_up else (today['close'] < today['open'])
    today_spike = today_vol > vol_20d * 1.2 and today_with_dir

    # Score: low vol pullback + high vol in-direction = best
    if pb_ratio < 0.85 and (dir_ratio > 1.1 or today_spike):
        sc = 8
        desc = "Low vol pullback + vol spike on move = accumulation"
    elif pb_ratio < 0.85:
        sc = 6
        desc = f"Low vol pullback ({pb_ratio:.2f}x avg) = sellers tiring"
    elif today_spike:
        sc = 6
        desc = "Today's volume spike in direction = conviction"
    elif pb_ratio < 1.0:
        sc = 4
        desc = f"Avg volume ({pb_ratio:.2f}x) = neutral"
    elif pb_ratio < 1.2:
        sc = 3
        desc = "Slightly elevated pullback volume"
    else:
        sc = 1
        desc = f"High vol pullback ({pb_ratio:.2f}x) = distribution"

    return sc, desc


def _analyze_15min_pullback(m15, need_up):
    """Deep 15-minute pullback analysis.

    Checks:
      1. Pullback depth (how far from recent intraday high/low)
      2. Pullback duration (how many candles in the dip)
      3. Volume on pullback vs on-direction candles
      4. Reversal candle on 15min (hammer, engulfing)
      5. Recovery status (bouncing from dip)

    Returns dict with: depth_pct, duration, vol_declining, reversal,
                        recovering, quality_score (0-8), detail.
    """
    result = {
        'depth_pct': 0, 'duration': 0, 'vol_declining': False,
        'reversal': 'none', 'recovering': False,
        'quality_score': 0, 'detail': 'Insufficient 15min data',
    }
    if not m15 or len(m15) < 8:
        return result

    # Use last ~3 hours (12 candles) for pullback context
    window = m15[-12:] if len(m15) >= 12 else m15[-8:]
    cur = m15[-1]
    cur_close = cur['close']

    # --- 1. Pullback depth ---
    if need_up:
        window_hi = max(c['high'] for c in window)
        window_lo = min(c['low'] for c in window)
        depth_pct = (window_hi - cur_close) / window_hi * 100 if window_hi > 0 else 0
    else:
        window_hi = max(c['high'] for c in window)
        window_lo = min(c['low'] for c in window)
        depth_pct = (cur_close - window_lo) / window_lo * 100 if window_lo > 0 else 0
    result['depth_pct'] = round(depth_pct, 2)

    # --- 2. Pullback duration (count against-direction candles in window) ---
    # Counts total red candles (CE) or green candles (PE) in the window,
    # not just consecutive — captures the full pullback leg even after recovery
    duration = 0
    for c in window:
        if need_up and c['close'] < c['open']:
            duration += 1
        elif not need_up and c['close'] > c['open']:
            duration += 1
    result['duration'] = duration

    # --- 3. Volume on pullback vs direction candles (last 12 candles) ---
    pb_vols = []
    dir_vols = []
    for c in window:
        vol = c.get('volume', 0)
        if need_up:
            if c['close'] < c['open']:
                pb_vols.append(vol)
            else:
                dir_vols.append(vol)
        else:
            if c['close'] > c['open']:
                pb_vols.append(vol)
            else:
                dir_vols.append(vol)
    avg_pb_vol = sum(pb_vols) / len(pb_vols) if pb_vols else 1
    avg_dir_vol = sum(dir_vols) / len(dir_vols) if dir_vols else 1
    vol_declining = avg_pb_vol < avg_dir_vol * 0.85 if avg_dir_vol > 0 else False
    result['vol_declining'] = vol_declining

    # --- 4. 15min reversal candle (last 2-3 candles) ---
    rc_name, rc_str = _detect_reversal_candle(m15[-3:], need_up)
    result['reversal'] = rc_name

    # --- 5. Recovery status ---
    # Recovering = last 2 candles moving in our direction after the dip
    if len(m15) >= 4:
        if need_up:
            result['recovering'] = (
                cur['close'] > m15[-2]['close']
                and cur['close'] > m15[-3]['close']
            )
        else:
            result['recovering'] = (
                cur['close'] < m15[-2]['close']
                and cur['close'] < m15[-3]['close']
            )

    # --- Composite quality score (0-8) ---
    score = 0
    details = []

    # Depth: sweet spot is 0.3-1.5% (healthy dip, not crash)
    if 0.3 <= depth_pct <= 1.5:
        score += 2
        details.append(f"{depth_pct:.1f}% dip")
    elif depth_pct > 1.5:
        score += 1
        details.append(f"deep {depth_pct:.1f}% dip")

    # Duration: 2-6 candles (30min-1.5hr) is ideal
    if 2 <= duration <= 6:
        score += 1
        details.append(f"{duration} candles")
    elif duration > 6:
        details.append(f"extended ({duration} candles)")

    # Volume declining on pullback
    if vol_declining:
        score += 2
        details.append("low vol")

    # 15min reversal candle
    if rc_str >= 5:
        score += 2
        details.append(rc_name.replace('_', ' '))
    elif rc_str >= 3:
        score += 1
        details.append(rc_name.replace('_', ' '))

    # Recovering from dip
    if result['recovering']:
        score += 1
        details.append("bouncing")

    score = min(score, 8)
    result['quality_score'] = score

    if not details:
        result['detail'] = "No 15min pullback detected"
    else:
        action = "entry" if score >= 6 else "developing" if score >= 3 else "weak"
        result['detail'] = f"15min: {', '.join(details)} = {action}"

    return result


def _analyze_intraday(hourly, m15, need_up, daily_dir):
    """Analyze 1hr ST + 15min pullback quality for entry timing.

    Key idea: D/W green + 1hr pulling back + 15min bouncing = PRIME entry.
    Now includes deep 15min analysis (depth, volume, reversal candle, duration).

    Returns: (score 0-8, detail_str, hourly_st_dir)
    """
    h_dir = None

    # Compute 1hr ST
    if hourly and len(hourly) >= 15:
        h_st = _compute_hourly_st(hourly)
        if h_st:
            h_dir = h_st['direction']

    # Deep 15min pullback analysis
    m15_analysis = _analyze_15min_pullback(m15, need_up)
    m15_quality = m15_analysis['quality_score']
    m15_recovering = m15_analysis['recovering']
    m15_has_pullback = m15_analysis['depth_pct'] >= 0.3
    m15_detail = m15_analysis['detail']

    # Score based on TF alignment + 15min quality
    if need_up:
        daily_aligned = daily_dir == 'UP'
        if daily_aligned and h_dir == 'DOWN' and m15_quality >= 5:
            sc = 8
            d = f"D UP + 1hr pullback + {m15_detail}"
        elif daily_aligned and h_dir == 'DOWN' and m15_has_pullback:
            sc = 6
            d = f"D UP + 1hr pullback + {m15_detail}"
        elif daily_aligned and h_dir == 'DOWN':
            sc = 5
            d = "D UP + 1hr pulling back, wait for 15min setup"
        elif daily_aligned and h_dir == 'UP' and m15_quality >= 4:
            sc = 6
            d = f"All UP + {m15_detail}"
        elif daily_aligned and h_dir == 'UP':
            sc = 4
            d = "D+1hr UP, running = no dip for entry"
        elif h_dir == 'DOWN':
            sc = 3
            d = f"Daily {daily_dir}, 1hr DOWN = deeper pullback, risky"
        else:
            sc = 2
            d = f"D {daily_dir}, 1hr {h_dir or '?'} = unclear"
    else:
        daily_aligned = daily_dir == 'DOWN'
        if daily_aligned and h_dir == 'UP' and m15_quality >= 5:
            sc = 8
            d = f"D DOWN + 1hr bounce + {m15_detail}"
        elif daily_aligned and h_dir == 'UP' and m15_has_pullback:
            sc = 6
            d = f"D DOWN + 1hr bounce + {m15_detail}"
        elif daily_aligned and h_dir == 'UP':
            sc = 5
            d = "D DOWN + 1hr bouncing, wait for 15min setup"
        elif daily_aligned and h_dir == 'DOWN' and m15_quality >= 4:
            sc = 6
            d = f"All DOWN + {m15_detail}"
        elif daily_aligned and h_dir == 'DOWN':
            sc = 4
            d = "D+1hr DOWN, running = no bounce for entry"
        elif h_dir == 'UP':
            sc = 3
            d = f"Daily {daily_dir}, 1hr UP = bounce, risky"
        else:
            sc = 2
            d = f"D {daily_dir}, 1hr {h_dir or '?'} = unclear"

    return sc, d, h_dir


# --- Scoring engine ----------------------------------------------------------

def score_signal(symbol, ltp, st_data, daily, hourly=None, m15=None,
                 target_tf=None, kite=None):
    """Score a magnet signal's confidence (0-100).

    Two-part:
      Signal Quality (0-60): Multi-TF alignment, gap, velocity, RSI.
      Entry Timing   (0-40): Pullback, intraday, volume, support, candle.
    """
    # Determine target
    if target_tf:
        tgt = st_data.get(target_tf)
        if not tgt:
            return {'symbol': symbol, 'error': f'No {target_tf} ST data'}
    else:
        target_tf, tgt = _auto_target(st_data, ltp)
        if not tgt:
            return {'symbol': symbol, 'error': 'No signal (price >5% from all ST)'}

    st_val = tgt['supertrend']
    st_dir = tgt['direction']
    gap_pct = (ltp - st_val) / st_val * 100
    need_up = ltp < st_val
    opt = 'CE' if need_up else 'PE'

    atr14 = _atr(daily)
    rsi14 = _rsi(daily)
    gap_pts = abs(ltp - st_val)
    atr_ratio = gap_pts / atr14 if atr14 > 0 else 99

    # Recent windows
    r5 = daily[-5:]
    r20 = daily[-20:] if len(daily) >= 20 else daily[-len(daily):]
    hi20 = max(c['high'] for c in r20)
    lo20 = min(c['low'] for c in r20)

    today_chg = 0
    if len(daily) >= 2:
        today_chg = ((daily[-1]['close'] - daily[-2]['close'])
                     / daily[-2]['close'] * 100)

    # Gap velocity (5d)
    gaps_5d = [(c['close'] - st_val) / st_val * 100 for c in r5]
    gap_vel = gaps_5d[-1] - gaps_5d[0] if len(gaps_5d) >= 2 else 0

    # Daily ST direction
    dd = st_data.get('daily')
    daily_dir = dd['direction'] if dd else 'UP'

    # Pullback depth from 20d extreme
    if need_up:
        pb_pct = (hi20 - ltp) / hi20 * 100
    else:
        pb_pct = (ltp - lo20) / lo20 * 100

    # ==========================================================================
    #  SIGNAL QUALITY (0-60)
    # ==========================================================================
    sq = {}

    # SQ1. Higher TF Support (0-13)
    m = st_data.get('monthly')
    if target_tf == 'weekly' and m:
        m_dir = m['direction']
        aligned = ((need_up and m_dir == 'UP')
                   or (not need_up and m_dir == 'DOWN'))
        s1 = 13 if aligned else 4
        d1 = f"Monthly {m_dir} -- {'supports' if aligned else 'against'} {opt}"
    elif target_tf == 'monthly':
        s1, d1 = 10, "Target is Monthly -- no higher TF"
    else:
        s1, d1 = 7, "No monthly data"
    sq['higher_tf'] = (s1, 13, d1)

    # SQ2. TF Alignment: Daily + Hourly (0-10)
    h_st = _compute_hourly_st(hourly) if hourly and len(hourly) >= 15 else None
    h_dir = h_st['direction'] if h_st else None

    d_aligned = ((need_up and daily_dir == 'UP')
                 or (not need_up and daily_dir == 'DOWN'))
    h_aligned = (h_dir is not None and
                 ((need_up and h_dir == 'UP')
                  or (not need_up and h_dir == 'DOWN')))

    if d_aligned and h_aligned:
        s2 = 10
        d2 = f"Daily {daily_dir} + 1hr {h_dir} -- full alignment"
    elif d_aligned and h_dir is not None:
        s2 = 7
        d2 = (f"Daily {daily_dir} (aligned) + 1hr {h_dir} "
              f"(pullback -- good for entry timing)")
    elif d_aligned:
        s2 = 6
        d2 = f"Daily {daily_dir} aligned, no 1hr data"
    elif h_aligned:
        s2 = 3
        d2 = f"Daily {daily_dir} against, 1hr {h_dir} aligned"
    else:
        s2 = 1
        d2 = f"Daily {daily_dir} + 1hr {h_dir or '?'} -- weak alignment"
    sq['tf_align'] = (s2, 10, d2)

    # SQ3. Gap to Target (0-10)
    ag = abs(gap_pct)
    if ag <= 1:     s3 = 10
    elif ag <= 1.5: s3 = 9
    elif ag <= 2:   s3 = 8
    elif ag <= 2.5: s3 = 6
    elif ag <= 3:   s3 = 4
    else:           s3 = 2
    sq['gap'] = (s3, 10,
                 f"{gap_pct:+.1f}% ({gap_pts:.0f} pts) to "
                 f"{target_tf.title()} {'resistance' if need_up else 'support'} "
                 f"at {st_val:,.0f}")

    # SQ4. ATR Reachability (0-7)
    if atr_ratio <= 0.5:   s4 = 7
    elif atr_ratio <= 1.0: s4 = 6
    elif atr_ratio <= 1.5: s4 = 5
    elif atr_ratio <= 2.0: s4 = 3
    elif atr_ratio <= 3.0: s4 = 2
    else:                  s4 = 1
    if atr_ratio <= 1:     reach = "<1 day"
    elif atr_ratio <= 3:   reach = f"~{atr_ratio:.0f} days"
    else:                  reach = f"{atr_ratio:.0f}+ days"
    sq['atr_reach'] = (s4, 7, f"Gap = {atr_ratio:.1f}x ATR({atr14:.0f}) -- {reach}")

    # SQ5. Gap Velocity (0-8)
    closing = (need_up and gap_vel > 0) or (not need_up and gap_vel < 0)
    av = abs(gap_vel)
    if closing:
        if av > 2:     s5, gd = 8, 'rapidly closing'
        elif av > 1:   s5, gd = 6, 'steadily closing'
        elif av > 0.3: s5, gd = 5, 'closing'
        else:          s5, gd = 3, 'slowly closing'
    else:
        if av < 0.3:   s5, gd = 3, 'flat'
        elif av < 1:   s5, gd = 2, 'drifting away'
        else:          s5, gd = 1, 'widening fast'
    sq['velocity'] = (s5, 8, f"5d gap change {gap_vel:+.1f}% -- {gd}")

    # SQ6. RSI Context (0-5)
    if need_up:
        if rsi14 < 30:   s6, rd = 5, 'deeply oversold'
        elif rsi14 < 40: s6, rd = 4, 'oversold'
        elif rsi14 < 50: s6, rd = 3, 'neutral-low'
        elif rsi14 < 60: s6, rd = 2, 'neutral'
        else:            s6, rd = 1, 'overbought'
    else:
        if rsi14 > 70:   s6, rd = 5, 'deeply overbought'
        elif rsi14 > 60: s6, rd = 4, 'overbought'
        elif rsi14 > 50: s6, rd = 3, 'neutral-high'
        elif rsi14 > 40: s6, rd = 2, 'neutral'
        else:            s6, rd = 1, 'oversold'
    sq['rsi'] = (s6, 5, f"RSI {rsi14:.0f} -- {rd}")

    # SQ7. Market Outlook (0-7) -- NIFTY health
    mkt = _compute_market_outlook(kite)
    sq['market'] = (mkt['score'], 7, mkt['detail'])

    sq_total = sum(v[0] for v in sq.values())

    # ==========================================================================
    #  ENTRY TIMING (0-40)
    # ==========================================================================
    et = {}

    # ET8. Intraday Pattern (0-8) — compute FIRST so we can use 15min data in ET7
    s8, d8, _ = _analyze_intraday(hourly, m15, need_up, daily_dir)
    et['intraday'] = (s8, 8, d8)

    # Deep 15min analysis for pullback quality boost
    m15_pb = _analyze_15min_pullback(m15, need_up)

    # ET7. Pullback Quality (0-8) — daily + 15min combined
    if need_up:
        if 1 <= pb_pct <= 5 and today_chg > 0.5:
            s7 = 8
            d7 = f"-{pb_pct:.1f}% pullback + green {today_chg:+.1f}% = entry"
        elif 1 <= pb_pct <= 5:
            s7 = 6
            d7 = f"-{pb_pct:.1f}% pullback zone, awaiting bounce"
        elif pb_pct < 1 and m15_pb['quality_score'] >= 4:
            # Daily near highs but 15min shows intraday pullback = entry window
            s7 = 7
            d7 = f"Intraday dip ({m15_pb['depth_pct']:.1f}%) in daily uptrend"
        elif pb_pct < 1:
            s7 = 3
            d7 = f"Near highs ({pb_pct:.1f}% off), no dip for entry"
        elif pb_pct > 5 and today_chg > 0:
            s7 = 5
            d7 = f"Deep -{pb_pct:.1f}% pullback, early recovery"
        else:
            s7 = 2
            d7 = f"Deep -{pb_pct:.1f}% pullback, still falling"
    else:
        if 1 <= pb_pct <= 5 and today_chg < -0.5:
            s7 = 8
            d7 = f"+{pb_pct:.1f}% bounce + red {today_chg:+.1f}% = entry"
        elif 1 <= pb_pct <= 5:
            s7 = 6
            d7 = f"+{pb_pct:.1f}% bounce zone, awaiting fade"
        elif pb_pct < 1 and m15_pb['quality_score'] >= 4:
            s7 = 7
            d7 = f"Intraday bounce ({m15_pb['depth_pct']:.1f}%) in daily downtrend"
        elif pb_pct < 1:
            s7 = 3
            d7 = f"Near lows ({pb_pct:.1f}%), no bounce for entry"
        elif pb_pct > 5 and today_chg < 0:
            s7 = 5
            d7 = f"Extended +{pb_pct:.1f}% bounce, fading"
        else:
            s7 = 2
            d7 = f"Strong +{pb_pct:.1f}% bounce, overbought"
    et['pullback'] = (s7, 8, d7)

    # ET9. Volume Dynamics (0-8)
    s9, d9 = _analyze_volume(daily, need_up)
    et['volume'] = (s9, 8, d9)

    # ET10. Support Pattern (0-8)
    tests, pat_name, pat_level = _detect_support_pattern(daily, need_up, n=20)
    pat_label = 'bottom' if need_up else 'top'
    if tests >= 3:
        s10 = 8
        d10 = f"Triple {pat_label} at {pat_level:,.0f} ({tests}x tests)"
    elif tests >= 2:
        s10 = 6
        d10 = f"Double {pat_label} at {pat_level:,.0f}"
    elif tests >= 1:
        s10 = 3
        d10 = f"Single test of {pat_level:,.0f}"
    else:
        s10 = 1
        d10 = "No clear support pattern"
    et['support'] = (s10, 8, d10)

    # ET11. Reversal Candle (0-8)
    # Check on daily candles
    rc_name, rc_str = _detect_reversal_candle(daily[-3:], need_up)
    # Also check hourly (last 3 candles) for fresher signal
    if hourly and len(hourly) >= 3:
        hrc_name, hrc_str = _detect_reversal_candle(hourly[-3:], need_up)
        if hrc_str > rc_str:
            rc_name = f"{hrc_name} (1hr)"
            rc_str = hrc_str

    s11 = rc_str
    if rc_name == 'none':
        d11 = "No reversal candle detected"
        s11 = 1
    else:
        candle_label = rc_name.replace('_', ' ').title()
        d11 = f"{candle_label} -- strength {rc_str}/8"
    et['reversal'] = (s11, 8, d11)

    et_total = sum(v[0] for v in et.values())

    # ==========================================================================
    #  AGGREGATE
    # ==========================================================================
    total = sq_total + et_total

    if total >= 80:   grade = 'HIGH'
    elif total >= 65: grade = 'MODERATE-HIGH'
    elif total >= 50: grade = 'MODERATE'
    elif total >= 35: grade = 'LOW'
    else:             grade = 'SKIP'

    # Thesis
    all_dims = {**sq, **et}
    sorted_d = sorted(all_dims.items(),
                      key=lambda x: x[1][0] / x[1][1], reverse=True)
    strengths = [d[1][2] for d in sorted_d if d[1][0] / d[1][1] >= 0.7][:4]
    risks = [d[1][2] for d in sorted_d if d[1][0] / d[1][1] < 0.45][:3]

    # Timing
    if et_total >= 32:
        timing = "NOW -- strong entry setup across all timing signals"
    elif et_total >= 24:
        timing = "SOON -- most timing signals aligned, minor gap"
    elif s7 >= 6 and s8 >= 5:
        timing = "SOON -- pullback active, wait for confirmation"
    elif sq_total >= 45:
        timing = "WAIT -- great signal, entry timing not ideal yet"
    else:
        timing = "WATCH -- developing"

    return {
        'symbol': symbol,
        'ltp': ltp,
        'target_tf': target_tf,
        'target_st': st_val,
        'target_dir': st_dir,
        'gap_pct': gap_pct,
        'option_type': opt,
        'need_up': need_up,
        'atr14': atr14,
        'rsi14': rsi14,
        'hourly_dir': h_dir,
        'sq_total': sq_total,
        'et_total': et_total,
        'total_score': total,
        'grade': grade,
        'sq_dims': sq,
        'et_dims': et,
        'strengths': strengths,
        'risks': risks,
        'timing': timing,
        'vol_ratio': 0,
        'gap_velocity': gap_vel,
        'pullback_pct': pb_pct,
        'today_chg': today_chg,
    }


# --- Output formatting -------------------------------------------------------

def format_stars(score):
    """Score to 5-star visual (ASCII safe)."""
    n = (5 if score >= 80 else 4 if score >= 65 else
         3 if score >= 50 else 2 if score >= 35 else 1)
    return '*' * n + '.' * (5 - n)


_SQ_NAMES = {
    'higher_tf': 'Higher TF',
    'tf_align':  'TF Alignment',
    'gap':       'Gap to Target',
    'atr_reach': 'ATR Reach',
    'velocity':  'Gap Velocity',
    'rsi':       'RSI Context',
    'market':    'Market Outlook',
}

_ET_NAMES = {
    'pullback': 'Pullback Quality',
    'intraday': 'Intraday Pattern',
    'volume':   'Volume Dynamics',
    'support':  'Support Pattern',
    'reversal': 'Reversal Candle',
}


def print_detail(result):
    """Detailed confidence report for a single symbol."""
    if 'error' in result:
        print(f"\n  {result['symbol']}: {result['error']}")
        return

    s = result
    w = 76
    stars = format_stars(s['total_score'])
    print(f"\n{'=' * w}")
    print(f"  {s['symbol']} -- {stars} ({s['total_score']}/100)")
    print(f"{'=' * w}")
    print(f"  LTP: {s['ltp']:,.2f}  |  Target: {s['target_tf'].title()} ST "
          f"{s['target_st']:,.0f} ({s['target_dir']})")
    print(f"  Gap: {s['gap_pct']:+.1f}%  |  Dir: {s['option_type']}  |  "
          f"ATR: {s['atr14']:.0f}  |  1hr: {s.get('hourly_dir', '?')}")
    print(f"{'=' * w}")

    # Signal Quality section
    print(f"\n  === SIGNAL QUALITY ({s['sq_total']}/60) ===")
    print(f"  {'#':<4} {'Dimension':<18} {'Score':>5}  {'':>8} Detail")
    print(f"  {'-' * (w - 4)}")
    for i, (key, (sc, mx, detail)) in enumerate(s['sq_dims'].items(), 1):
        name = _SQ_NAMES.get(key, key)
        fill = min(int(sc / mx * 8), 8)
        bar = '#' * fill + '.' * (8 - fill)
        print(f"  {i:<4} {name:<18} {sc:>2}/{mx:<2}  {bar}  {detail}")

    # Entry Timing section
    print(f"\n  === ENTRY TIMING ({s['et_total']}/40) ===")
    print(f"  {'#':<4} {'Dimension':<18} {'Score':>5}  {'':>8} Detail")
    print(f"  {'-' * (w - 4)}")
    for i, (key, (sc, mx, detail)) in enumerate(s['et_dims'].items(), 7):
        name = _ET_NAMES.get(key, key)
        fill = min(int(sc / mx * 8), 8)
        bar = '#' * fill + '.' * (8 - fill)
        print(f"  {i:<4} {name:<18} {sc:>2}/{mx:<2}  {bar}  {detail}")

    # Total
    print(f"\n  {'-' * (w - 4)}")
    grade_icon = {
        'HIGH': '[+]', 'MODERATE-HIGH': '[~]', 'MODERATE': '[.]',
        'LOW': '[-]', 'SKIP': '[X]',
    }
    gi = grade_icon.get(s['grade'], '[?]')
    print(f"  {'':4} {'TOTAL':<18} {s['total_score']:>2}/100       "
          f"{gi} {s['grade']}  "
          f"(Signal: {s['sq_total']}/60, Entry: {s['et_total']}/40)")
    print(f"{'=' * w}")

    move = 'rallies' if s['need_up'] else 'drops'
    print(f"\n  THESIS : Buy {s['option_type']} -- price {move} to "
          f"{s['target_tf'].title()} ST {s['target_st']:,.0f}")
    if s['strengths']:
        print(f"  WHY    : {s['strengths'][0]}")
        for x in s['strengths'][1:3]:
            print(f"           {x}")
    if s['risks']:
        print(f"  RISK   : {s['risks'][0]}")
        for x in s['risks'][1:2]:
            print(f"           {x}")
    print(f"  TIMING : {s['timing']}")
    print()


def print_summary(results):
    """Multi-symbol summary table."""
    valid = [r for r in results if 'error' not in r]
    errors = [r for r in results if 'error' in r]

    if valid:
        print(f"\n  {'Symbol':<12} {'LTP':>8} {'Target':>14} {'Gap':>6} "
              f"{'Dir':>3} {'Sig':>4} {'Ent':>4} {'Tot':>4}  Grade")
        print(f"  {'-' * 76}")
        for r in sorted(valid, key=lambda x: x['total_score'], reverse=True):
            tf = 'W' if r['target_tf'] == 'weekly' else 'M'
            print(f"  {r['symbol']:<12} {r['ltp']:>8,.0f}  "
                  f"{tf} ST {r['target_st']:>8,.0f} {r['gap_pct']:>+5.1f}% "
                  f"{r['option_type']:>3} {r['sq_total']:>3} "
                  f"{r['et_total']:>4} {r['total_score']:>4}  "
                  f"{r['grade']}")
        print()

    for r in errors:
        print(f"  {r['symbol']}: {r['error']}")
    if errors:
        print()


# --- CLI ---------------------------------------------------------------------

def cli_main(args=None):
    """Entry point for CLI."""
    parser = argparse.ArgumentParser(
        description='Magnet Confidence Scorer v2 -- signal quality + entry timing')
    parser.add_argument('symbols', nargs='*', help='Stock symbols to score')
    parser.add_argument('--target', choices=['weekly', 'monthly'], default=None,
                        help='Target ST timeframe (default: auto-detect)')
    parser.add_argument('--from-magnet', action='store_true',
                        help='Score all "watching" signals from magnet store')
    parser.add_argument('--json', action='store_true', help='JSON output')
    parser.add_argument('--summary', action='store_true',
                        help='Summary table only')

    parsed = parser.parse_args(args)

    symbols = list(parsed.symbols)
    tf_overrides = {}

    if parsed.from_magnet:
        from .trade_store import get_store
        store = get_store()
        for t in store.load_trades():
            if t.get('status') == 'watching':
                sym = t['stock']
                if sym not in symbols:
                    symbols.append(sym)
                tf_overrides[sym] = t.get('timeframe', parsed.target)

    if not symbols:
        parser.error("Provide symbols or use --from-magnet")

    kite = _get_kite()
    n = len(symbols)
    print(f"  Scoring {n} symbol(s) (daily + hourly + 15min)...\n")

    results = []
    for idx, sym in enumerate(symbols, 1):
        try:
            tok = _get_instrument_token(kite, sym)
            print(f"  [{idx}/{n}] {sym}: fetching data...", end='', flush=True)

            daily = _fetch_daily(kite, tok)
            hourly = _fetch_hourly(kite, tok)
            m15 = _fetch_15min(kite, tok)

            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data[f'NSE:{sym}']['last_price']

            st_data = _compute_all_st(daily)
            tf = tf_overrides.get(sym, parsed.target)
            result = score_signal(sym, ltp, st_data, daily, hourly, m15,
                                  target_tf=tf, kite=kite)
            results.append(result)
            sc = result.get('total_score', '?')
            gr = result.get('grade', '')
            print(f" {sc}/100 {gr}")
        except Exception as e:
            results.append({'symbol': sym, 'error': str(e)})
            print(f" ERROR: {e}")

    # Output
    if parsed.json:
        out = []
        for r in results:
            r = dict(r)
            for k in ('sq_dims', 'et_dims'):
                if k in r:
                    r[k] = {dk: {'score': v[0], 'max': v[1], 'detail': v[2]}
                            for dk, v in r[k].items()}
            out.append(r)
        print(json.dumps(out, indent=2, default=str))
    elif parsed.summary or len(results) > 3:
        print_summary(results)
    else:
        for r in results:
            print_detail(r)
        if len(results) > 1:
            print_summary(results)


if __name__ == '__main__':
    cli_main()

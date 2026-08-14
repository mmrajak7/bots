"""What this symbol's own chart says — two questions off one candle series.

1. **Is there a level in the way?** (`swing_tp`)
   The magnet thesis targets the ST line, so TP has always been the ST line.
   But price does not travel to a magnet through empty space: on the way there
   it meets its own prior swing points. LUPIN 2026-08 is the case that prompted
   this — a PE signal (price above a rising ST, expected to fall to it) with a
   prior swing LOW sitting well above the ST line. Price is far likelier to
   stall at that support than to run the whole distance, so booking at the
   swing beats holding out for a target the chart argues against.

2. **Does this symbol get pulled to ST at all?** (`attraction`)
   The magnet IS the trade. Some symbols oscillate around their ST line and
   some trend away from it for months without touching. Nothing measured which
   kind a symbol was, so every signal was vetted as though the pull were a
   given. This gives the vetting agent that symbol's own history.

Both read the daily candles `playbook.magnet.scanner` already cached while
computing ST, so the normal path costs ZERO extra API calls — the same trick
`check_freshness` and `compute_daily_velocity` use.
"""

from __future__ import annotations

import logging
import statistics
from datetime import datetime
from typing import List, Optional

from . import config as cfg

logger = logging.getLogger(__name__)

# {(stock, timeframe, date): result} — ST is stable within its own candle, so
# one computation per symbol per day is plenty. Keyed by date so a long-lived
# process picks up new candles instead of serving last week's answer forever.
_attraction_cache: dict = {}


# ── candles ───────────────────────────────────────────────────────────────

def _timeframe_candles(kite, stock: str, timeframe: str) -> List[dict]:
    """Completed candles on the signal's own timeframe.

    Reuses the daily cache `compute_st_for_stock` fills. If it is empty we call
    that function purely for its side effect — it is the one place that knows
    the 6Y/3Y fetch shape and the 2000-day chunking rule.
    """
    try:
        from playbook.magnet import scanner as mscan
    except Exception as e:                      # pragma: no cover - import guard
        logger.debug("magnet scanner unavailable: %s", e)
        return []

    daily = mscan._raw_daily_cache.get(stock)
    if not daily:
        try:
            mscan.compute_st_for_stock(kite, stock, timeframe)
            daily = mscan._raw_daily_cache.get(stock)
        except Exception as e:
            logger.debug("candle fetch failed for %s: %s", stock, e)
            return []
    if not daily:
        return []

    try:
        if timeframe == 'monthly':
            return mscan._aggregate_to_monthly(daily)
        if timeframe == 'weekly':
            return mscan._aggregate_to_weekly(daily)
        today = datetime.now().strftime('%Y-%m-%d')
        return [c for c in daily if str(c['date'])[:10] < today]
    except Exception as e:
        logger.debug("aggregation failed for %s %s: %s", stock, timeframe, e)
        return []


# ── 1. swing levels ───────────────────────────────────────────────────────

def _pivots(candles: List[dict], n: int, kind: str) -> List[dict]:
    """CONFIRMED swing points: n candles either side, both sides present.

    The last n candles can never produce one, and that is the point — an
    unconfirmed pivot at the right edge is not a level, it is the move still
    happening. Treating it as support is how you place a target inside the
    candle that is currently breaking it.

    Strictly beyond its IMMEDIATE neighbours, and at least equal to the rest of
    the window. The strictness matters: with a plain `== min(window)` a flat
    run of equal lows registers every single bar as support, so a quiet
    sideways stretch manufactures a level at whatever price it drifted to. A
    genuine double bottom — two equal lows separated by higher bars — still
    qualifies, which is the case worth keeping.
    """
    key = 'low' if kind == 'low' else 'high'
    lower = kind == 'low'
    out = []
    for i in range(n, len(candles) - n):
        v = candles[i][key]
        window = candles[i - n:i + n + 1]
        rest = [c[key] for j, c in enumerate(window) if j != n]
        neighbours = (candles[i - 1][key], candles[i + 1][key])
        ok = (all(v < x for x in neighbours) and all(v <= x for x in rest)) \
            if lower else \
            (all(v > x for x in neighbours) and all(v >= x for x in rest))
        if ok:
            out.append({'value': float(v), 'date': str(candles[i]['date'])[:10],
                        'bars_ago': len(candles) - 1 - i})
    return out


def swing_tp(kite, stock: str, timeframe: str, direction: str,
             spot: float, st_value: float) -> Optional[dict]:
    """The first swing level between spot and the ST magnet, or None.

    PE — price sits ABOVE ST and is expected to fall to it, so a swing LOW in
    between is support the fall is likely to stall at.
    CE — the mirror: price below ST, and a swing HIGH is resistance.

    Only ever SHORTENS. A level beyond the ST line would move the target
    further away, which is a different (and unasked-for) trade, so the search
    window is strictly between spot and ST.

    None means "keep the ST line": no level, none worth trading to, or no
    candles. Never raises — a missing chart must not block an entry.
    """
    if not cfg.SWING_TP_ENABLED:
        return None
    try:
        spot, st_value = float(spot), float(st_value)
    except (TypeError, ValueError):
        return None
    if direction not in ('CE', 'PE') or spot <= 0 or st_value <= 0:
        return None

    candles = _timeframe_candles(kite, stock, timeframe)
    if len(candles) < cfg.SWING_PIVOT_BARS * 2 + 1:
        return None
    candles = candles[-cfg.SWING_LOOKBACK_CANDLES:]

    kind = 'low' if direction == 'PE' else 'high'
    lo, hi = min(spot, st_value), max(spot, st_value)
    # Strictly inside: a "level" equal to spot or to the ST line is not a
    # shortening, it is a rounding artefact.
    inside = [p for p in _pivots(candles, cfg.SWING_PIVOT_BARS, kind)
              if lo < p['value'] < hi]
    if not inside:
        return None

    # Nearest to spot = the first one price meets on the way to the magnet.
    best = (max(inside, key=lambda p: p['value']) if direction == 'PE'
            else min(inside, key=lambda p: p['value']))

    gap_pct = abs(spot - best['value']) / spot * 100
    full = abs(st_value - spot)
    retained_pct = abs(best['value'] - spot) / full * 100 if full else 0.0

    if gap_pct < cfg.SWING_MIN_GAP_PCT:
        # Support two candles below the entry print is not a target, it is
        # noise, and booking there pays the round-trip spread for nothing.
        logger.debug("%s: swing %s %.2f is only %.2f%% from spot — keeping ST",
                     stock, kind, best['value'], gap_pct)
        return None

    if retained_pct < cfg.SWING_MIN_RETAINED_PCT:
        # Measured across 75 signal-like symbols: 57% had a swing in the way,
        # and the shortening ran as high as 82% of the journey. A TP that
        # keeps a fifth of the distance is not a shortened target, it is a
        # different and much worse trade — especially under BCS, where the
        # short strike is still chosen at the ST line, so max gain would need a
        # move the TP is set to never wait for.
        #
        # Keeping the ST line here is the CONSERVATIVE branch (unchanged
        # behaviour), not an endorsement: support this close to spot says the
        # trade has little room, which is a fact the vetting agent should see.
        # `swing_tp` still reports it via _vet_context; only the TP is left
        # alone.
        logger.info("%s: swing %s %.2f keeps only %.0f%% of the run to ST "
                    "%.2f — TP unchanged, flagged for vetting",
                    stock, kind, best['value'], retained_pct, st_value)
        return {'tp_spot': None, 'applied': False,
                'reason': f"retains only {retained_pct:.0f}% of the run to ST "
                          f"(floor {cfg.SWING_MIN_RETAINED_PCT:g}%)",
                'level': round(best['value'], 2),
                'kind': f"swing_{kind}", 'date': best['date'],
                'bars_ago': best['bars_ago'], 'timeframe': timeframe,
                'st_value': round(st_value, 2)}

    return {
        'tp_spot': round(best['value'], 2),
        'applied': True,
        'kind': f"swing_{kind}",
        'date': best['date'],
        'bars_ago': best['bars_ago'],
        'timeframe': timeframe,
        'gap_from_spot_pct': round(gap_pct, 2),
        'st_value': round(st_value, 2),
        # How much of the original journey we give up to book earlier, and how
        # much is left to win.
        'shortened_by_pct': round(100 - retained_pct, 1),
        'retained_pct': round(retained_pct, 1),
    }


# ── 2. does this symbol get pulled to ST? ─────────────────────────────────

def _touches(candle: dict, st: float, from_above: bool) -> bool:
    """Did this candle reach the ST line? Uses the WICK, not the close —
    the magnet thesis is about price reaching the level, and TP is a spot
    trigger checked intraday, not a close."""
    return candle['low'] <= st if from_above else candle['high'] >= st


def attraction(kite, stock: str, timeframe: str,
               direction: Optional[str] = None) -> Optional[dict]:
    """How often this symbol actually gets pulled back to its own ST line.

    An EPISODE begins on the first completed candle that TRADED inside the
    entry band — the band is checked against the candle's high/low range, not
    its close, because the signal that opens a real trade is an intraday LTP
    reading. It ends when price touches ST or the horizon expires. Consecutive
    qualifying candles belong to ONE episode — otherwise a symbol that sat 5%
    away for ten weeks would report ten independent outcomes off a single move,
    and the rate would describe how long it lingered rather than whether it
    came back.

    Returns None when there is not enough history to say anything. A thin
    sample is reported WITH its size and a 'thin' verdict rather than as a
    confident percentage — 2 for 3 is not a 67% hit rate.
    """
    if not cfg.ATTRACTION_ENABLED:
        return None
    # MEASURED ON WEEKLY ONLY, and this is a decision rather than a failure —
    # so it does NOT return None. `null` means "we tried and could not say",
    # which VETTING.md tells the agent to treat as a missing section worth
    # noting; reporting a deliberate omission that way trains it to flag a
    # non-problem on every monthly signal. Same distinction as
    # `feedback_never_asked_is_not_failed`: declined-to-start and
    # started-and-failed need different words.
    #
    # Six years of monthly candles is ~72 bars and an episode consumes at least
    # nine, so the statistic cannot get a usable sample there — measured, it
    # reached `usable` on 5.4% of symbols. Monthly signals are also rare
    # (today's scan: 45 weekly vs 6 monthly).
    if timeframe not in cfg.ATTRACTION_TIMEFRAMES:
        return {'timeframe': timeframe, 'measured': False,
                'why': 'not measured on %s — only weekly has enough candles '
                       'to build a sample worth reading' % timeframe}
    key = (stock, timeframe, datetime.now().strftime('%Y-%m-%d'))
    if key in _attraction_cache:
        return _attraction_cache[key]

    result = None
    try:
        result = _attraction(kite, stock, timeframe, direction)
    except Exception as e:
        # Never block an entry on a statistics failure.
        logger.warning("attraction failed for %s %s: %s", stock, timeframe, e)
    _attraction_cache[key] = result
    return result


def _attraction(kite, stock, timeframe, direction):
    candles = _timeframe_candles(kite, stock, timeframe)
    if len(candles) < cfg.ST_PERIOD + cfg.ATTRACTION_HORIZON_BARS + 5:
        return None

    from playbook.compute_st import compute_supertrend
    series = compute_supertrend(candles, cfg.ST_PERIOD, cfg.ST_MULTIPLIER)
    if not series:
        return None

    horizon = cfg.ATTRACTION_HORIZON_BARS
    thresh = cfg.ATTRACTION_GAP_PCT
    # ×100: WATCH_GAP_MAX is a FRACTION (0.05 = 5%) while gap_pct below is a
    # percent. Comparing them raw makes the band `3.0 <= gap <= 0.05`, which no
    # candle can satisfy — the whole statistic returns None for every symbol,
    # forever, with nothing in the logs to say so. Pinned by
    # test_the_gap_band_is_in_the_same_units.
    ceiling = cfg.WATCH_GAP_MAX * 100.0
    episodes = []
    i = 0
    while i < len(series) - 1:
        bar = series[i]
        st = bar['supertrend']
        if not st:
            i += 1
            continue

        from_above = bar['close'] > st
        # BAND, not a floor. An unbounded threshold counts a symbol sitting 20%
        # from its ST as an episode, and of course that does not come back
        # inside two months — but the scanner would never have signalled it
        # either (watch_gap_max caps the setup). Measured: dropping the ceiling
        # is what made the median symbol look non-magnetic. The statistic has
        # to describe the setup that actually gets traded.
        #
        # And "the setup that actually gets traded" is an INTRADAY LTP reading,
        # sampled every few minutes — never a close. So the band is tested
        # against the candle's RANGE, as an interval intersection, not against
        # its close as a point. The close-based test asked a question the
        # strategy never asks, and the answer was mostly silence.
        #
        # Measured on LIVE Kite data over the 210 F&O names (NOT the disk
        # cache, which is 22 days stale and carries two partial-day bars per
        # symbol, and NOT the 827-symbol cached NSE set, which is not what this
        # bot trades):
        #
        #     close in band : 6.7% null, 52.4% thin -> 41.0% usable
        #     candle range  : 0.0% null,  8.6% thin -> 91.4% usable
        #
        # COALINDIA 2026-08-14 is the case that surfaced it: a signal triggering
        # at a 3.97% intraday gap was vetted with no magnet history at all,
        # because not one of its weekly closes had ever landed in the band.
        # It now reads 7 episodes / 28.6% — "often does NOT return to ST".
        if from_above:
            near, far = (bar['low'] - st) / st * 100, (bar['high'] - st) / st * 100
        else:
            near, far = (st - bar['high']) / st * 100, (st - bar['low']) / st * 100
        if far < thresh or near > ceiling:
            i += 1
            continue

        # Ordering WITHIN a candle is unknowable, so a candle that also reached
        # ST cannot say whether the entry came before the touch or after it.
        # Scoring it either invents a hit or invents a miss, so it is skipped.
        #
        # Measured on LIVE data across the 210 F&O names: 16.1% of episodes,
        # and dropping them moves the population median NOT AT ALL (60.0% both
        # ways) — it costs 10 of 202 symbols their `usable` verdict and buys
        # accuracy per symbol rather than a different headline. Kept for the
        # unknowability, not for the number; do not re-justify it as a rate
        # effect, because it is not one.
        if bar['low'] <= st <= bar['high']:
            i += 1
            continue

        touched_in = None
        # ST moves with every candle, so each forward bar is tested against
        # ITS OWN ST value, not the one at episode start. A rising ST can come
        # up to meet a falling price; that is still the magnet working.
        for j in range(i + 1, min(i + 1 + horizon, len(series))):
            if _touches(series[j], series[j]['supertrend'], from_above):
                touched_in = j - i
                break
        episodes.append({'from_above': from_above, 'bars': touched_in,
                         'date': str(bar['date'])[:10]})
        # Advance past this episode — to the touch if there was one, else past
        # the horizon. This is what keeps one long move from counting N times.
        i += (touched_in or horizon) + 1

    if not episodes:
        return None

    def _summary(rows):
        if not rows:
            return {'episodes': 0, 'touched': 0, 'touch_rate_pct': None,
                    'median_bars_to_touch': None}
        hits = [r['bars'] for r in rows if r['bars'] is not None]
        return {
            'episodes': len(rows),
            'touched': len(hits),
            'touch_rate_pct': round(len(hits) / len(rows) * 100, 1),
            'median_bars_to_touch': (round(statistics.median(hits), 1)
                                     if hits else None),
        }

    overall = _summary(episodes)
    same = None
    if direction in ('CE', 'PE'):
        # PE = price ABOVE ST falling to it. CE = below, rising to it.
        want_above = direction == 'PE'
        same = _summary([e for e in episodes if e['from_above'] == want_above])

    thin = overall['episodes'] < cfg.ATTRACTION_MIN_EPISODES
    rate = overall['touch_rate_pct']
    return {
        'timeframe': timeframe,
        'measured': True,
        'horizon_bars': horizon,
        'gap_threshold_pct': thresh,
        'gap_band_pct': [thresh, ceiling],
        # Stamped so a rate can never be compared across the two definitions by
        # accident: every number recorded before 2026-08-14 is 'close'-based and
        # runs ~11 points higher (median 71.4% vs 60.0% across the F&O names),
        # because that test only ever caught candles that STOPPED in the band.
        'band_basis': 'candle_range',
        'overall': overall,
        'same_direction': same,
        'sample': 'thin' if thin else 'usable',
        'verdict': ('too few episodes to lean on' if thin else
                    'reliably magnetic' if rate >= 70 else
                    'usually magnetic' if rate >= 50 else
                    'often does NOT return to ST'),
        'note': ('An episode is one move away from ST, not one candle. It '
                 'opens when the candle TRADED through the entry band, which '
                 'is what the intraday trigger sees; candles that also reached '
                 'ST are skipped as ambiguous. Touch is judged on the wick '
                 'against the ST value of THAT candle, matching how the TP '
                 'trigger actually fires.'),
    }

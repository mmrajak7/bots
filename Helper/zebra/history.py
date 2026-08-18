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


# ── 2a. how FAST, in the unit the option actually lives in ────────────────

def _days_to_touch(daily: List[dict], series: List[dict]) -> Optional[float]:
    """Median TRADING DAYS from entering the band to reaching the ST line.

    `attraction` counts weekly bars, which is the wrong unit for a bounded
    instrument: an option dies on a DTE clock, in days. Worse, weekly bars are
    too coarse to separate the cases that matter — 18 sessions and 39 sessions
    both land in the same 4-8 bar bucket, and those are opposite trades.

    Modelled on the REAL entry: a daily bar whose gap to the ST line IN FORCE
    enters the band. In force means the last COMPLETED weekly bar's ST, which
    is what the scanner reads intraday — using the current week's own ST is
    look-ahead, because it is not knowable until Friday.

    Measured across 155 closed weekly trades, this splits the group the touch
    rate already calls 'reliably magnetic' almost in half:

        rate >=70% and fast (<=7d) : 62% wins, median +26.5%
        rate >=70% and slow (> 7d) : 53% wins, median  +2.9%

    A +2.9% GROSS median does not survive this book's fee drag, so the two
    halves are not "good and less good" — they are a trade and a non-trade,
    and until now they carried the same label.
    """
    marks = [(str(b['date'])[:10], b['supertrend'])
             for b in series if b.get('supertrend')]
    if len(marks) < 2 or not daily:
        return None
    thresh, ceiling = cfg.ATTRACTION_GAP_PCT, cfg.WATCH_GAP_MAX * 100.0
    horizon = cfg.ATTRACTION_HORIZON_DAYS

    def st_in_force(day: str, hint: int = 0) -> tuple:
        """ST of the last weekly bar that CLOSED before `day`, plus a cursor so
        the caller can resume instead of rescanning from the top."""
        i, st = hint, None
        while i < len(marks) and marks[i][0] < day:
            st = marks[i][1]
            i += 1
        return st, max(0, i - 1)

    hits, i, n, cur = [], 0, len(daily), 0
    while i < n:
        day = str(daily[i]['date'])[:10]
        st, cur = st_in_force(day, cur)
        if not st:
            i += 1
            continue
        close = daily[i]['close']
        gap = abs(close - st) / st * 100
        if not (thresh <= gap <= ceiling):
            i += 1
            continue
        from_above = close > st
        touched = None
        inner = cur
        for j in range(i + 1, min(i + 1 + horizon, n)):
            dayj = str(daily[j]['date'])[:10]
            stj, inner = st_in_force(dayj, inner)
            if not stj:
                continue
            if _touches(daily[j], stj, from_above):
                touched = j - i
                break
        if touched is not None:
            hits.append(touched)
        # Advance past this episode, exactly as the weekly pass does, so one
        # long move away cannot report N independent outcomes.
        i += (touched or horizon) + 1
    return round(statistics.median(hits), 1) if hits else None


def _daily_candles(kite, stock: str, timeframe: str) -> List[dict]:
    """The COMPLETED daily bars behind the timeframe series — free.

    `_timeframe_candles` has already filled `_raw_daily_cache` by the time this
    is called, so there is no second fetch and no extra API call; this is the
    same trick the aggregation itself uses.
    """
    try:
        from playbook.magnet import scanner as mscan
        daily = mscan._raw_daily_cache.get(stock) or []
    except Exception as e:                      # pragma: no cover - import guard
        logger.debug("daily cache unavailable for %s: %s", stock, e)
        return []
    today = datetime.now().strftime('%Y-%m-%d')
    return [c for c in daily if str(c['date'])[:10] < today]


# ── 2. does this symbol get pulled to ST? ─────────────────────────────────

def _touches(candle: dict, st: float, from_above: bool) -> bool:
    """Did this candle reach the ST line? Uses the WICK, not the close —
    the magnet thesis is about price reaching the level, and TP is a spot
    trigger checked intraday, not a close."""
    return candle['low'] <= st if from_above else candle['high'] >= st


def attraction(kite, stock: str, timeframe: str,
               direction: Optional[str] = None,
               dte: Optional[int] = None) -> Optional[dict]:
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
    # NOTE: no early `return _attraction_cache[key]` here. The cache holds the
    # symbol's history; `sessions_of_room` belongs to THIS signal's expiry and
    # is layered on below. Returning the cached dict directly would give the
    # first caller of the day its room figure and silently deny it to every
    # other signal on the same stock.
    if key in _attraction_cache:
        result = _attraction_cache[key]
    else:
        result = None
        try:
            result = _attraction(kite, stock, timeframe, direction)
        except Exception as e:
            # Never block an entry on a statistics failure.
            logger.warning("attraction failed for %s %s: %s",
                           stock, timeframe, e)
        _attraction_cache[key] = result
    return _with_room(result, dte)


def _with_room(result: Optional[dict], dte: Optional[int]) -> Optional[dict]:
    """Add "does this option have time for the move this symbol needs?".

    Returned on a COPY. The cache is keyed by symbol and day, but DTE belongs
    to the SIGNAL, so writing it into the cached dict would stamp one signal's
    expiry onto every later reader of the same symbol.

    Measured over 155 closed weekly trades: where the option expires before the
    stock typically arrives, 37 trades ran 41% wins and a -21.1% median; where
    it does not, ~56% and positive. Computed here rather than left to the agent
    because it is arithmetic across two units (calendar DTE vs trading
    sessions) and that is exactly the sort of step a model quietly gets wrong.
    """
    if not result or not result.get('measured'):
        return result
    need = result.get('median_days_to_touch')
    if need is None or not dte:
        return result
    out = dict(result)
    sessions = dte * 5.0 / 7.0
    out['sessions_to_expiry'] = round(sessions, 1)
    out['sessions_of_room'] = round(sessions - need, 1)
    return out


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
        # `room` is how many forward bars this episode was ever given. An
        # episode opened last week has had one of its eight, and the loop above
        # cannot tell "did not come back" from "has not come back yet" - both
        # leave touched_in None. Carried here so the summary can.
        episodes.append({'from_above': from_above, 'bars': touched_in,
                         'date': str(bar['date'])[:10],
                         'room': len(series) - 1 - i})
        # Advance past this episode — to the touch if there was one, else past
        # the horizon. This is what keeps one long move from counting N times.
        i += (touched_in or horizon) + 1

    if not episodes:
        return None

    # CENSORED EPISODES ARE NOT MISSES. An episode resolves when it touches or
    # when its horizon runs out; one opened inside the last `horizon` bars has
    # done neither, and counting it as "did not return" states an outcome that
    # has not happened yet.
    #
    # It is not a rounding error, because of WHICH episode it always is. The
    # unfinished one is by construction the most recent - the departure a live
    # signal is trading right now. HAVELLS #405 on 2026-08-17 was vetoed at
    # 28.6% (2 of 7); the seventh episode opened 2026-08-03 and was the very
    # move being entered, one week into eight, scored as a failure to justify
    # not taking it. Read correctly it is 33.3% of 6.
    #
    # Measured live over the 210 F&O names: 37 (18%) carry one, the population
    # median moves 60.0% -> 62.5%, and among the affected symbols the median
    # rate moves 50.0% -> 60.0%. The bias only ever runs one way - down - so
    # nothing here can make a symbol look more magnetic than it was.
    #
    # Dropped from the RATE, never from the record: `in_progress` below says a
    # departure is open, because "no completed episodes" and "one running" are
    # different facts and the agent should not have to infer either.
    def _done(e):
        return e['bars'] is not None or e['room'] >= horizon
    # Partitioned by the predicate, not by `not in resolved`: two episodes can
    # carry identical values and dict equality would then drop the wrong one.
    resolved = [e for e in episodes if _done(e)]
    running = [e for e in episodes if not _done(e)]

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

    days = _days_to_touch(_daily_candles(kite, stock, timeframe), series)
    overall = _summary(resolved)
    same = None
    if direction in ('CE', 'PE'):
        # PE = price ABOVE ST falling to it. CE = below, rising to it.
        want_above = direction == 'PE'
        same = _summary([e for e in resolved if e['from_above'] == want_above])

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
        # The same journey in the unit the OPTION lives in. `median_bars_to_
        # touch` above is weekly bars and cannot separate 18 sessions from 39;
        # those are opposite trades on a 30-DTE structure.
        'median_days_to_touch': days,
        'day_horizon': cfg.ATTRACTION_HORIZON_DAYS,
        'same_direction': same,
        # A departure that is open right now, excluded from the rate above
        # because it has not finished. Reported so the omission is visible.
        'in_progress': ({'episodes': len(running),
                         'latest': running[-1]['date'],
                         'bars_elapsed': running[-1]['room'],
                         'of_horizon': horizon} if running else None),
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
                 'trigger actually fires. An episode too recent to have had '
                 'its full horizon is NOT counted as a miss - see '
                 'in_progress.'),
    }

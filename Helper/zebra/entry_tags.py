"""SHADOW TAGS: four facts about an entry, stamped for the 100/150-trade review.

Measures, never acts. Nothing here gates, sizes or vetoes a trade, and no rule
anywhere reads these fields to decide anything. They exist because the Fable
enhancement review (2026-11-19, `_research/fable_enhance_2026_11_19/REPORT.md`)
found two candidate skips worth MEASURING forward rather than adopting
mid-test (docs/NAKED_100_TRADE_TEST.md rule 3 forbids changing entry, exit or
vetting before the verdict):

  E2  cost of distance  `gap_over_premium` = gap to the line (% of the line)
      / premium (% of spot). Low = the option is expensive for how far the
      stock has to travel. The live third at or below 0.87 ran -7.5%.
  E3  ran at the line   `tail5_pct` = % the stock moved TOWARD the line over
      the 5 sessions before entry, ending at the entry spot. >= 4.6 was the
      one replay survivor of the 10-27 vet-edge study.

plus `atr_pct` (daily ATR(14) % of spot) and `iv_over_rv20` (entry IV over
20-session realised vol, the VRP proxy), so the review can separate a
cost-of-distance effect from a plain volatility one.

ONE definition, mirroring Fable's `lib.features` exactly: every input is a
COMPLETED daily candle from BEFORE the entry session, except the entry spot
itself. So a tag stamped live at entry and one derived later from the cache
are the same number -- which is what lets older shadows be backfilled.

Stale candles are refused, never used. The newest candle before the entry day
must be the session IMMEDIATELY before it -- proven either by the entry day's
own candle being in the cache (the series is then continuous by construction)
or by the NSE holiday calendar counting no session in between. Anything else
is `why: stale_candles` with every candle-based tag None. The scanner accepts a
cache up to 5 days old, so this is not hypothetical: on a series missing the
last three sessions, "5 sessions before entry" would silently be 8.
"""
import logging
import math
from datetime import date
from typing import Optional

from common.nse_holidays import sessions_between

from . import replay

logger = logging.getLogger(__name__)

#: Schema of the tag block. Bump when a definition changes, so the review
#: never pools two meanings under one field name.
TAGS_VERSION = 1
ATR_N, RV_N, TAIL_N = 14, 20, 5


def _continuous(daily: list, i: int, last: str, day: str) -> bool:
    """Is `last` the session immediately before `day`?"""
    if i < len(daily) and daily[i]['date'] == day:
        return True                      # the entry day itself is in the series
    try:
        return sessions_between(date.fromisoformat(last), date.fromisoformat(day)) == 1
    except Exception:
        return False


def _atr(dl: list, i: int, n: int = ATR_N) -> Optional[float]:
    if i <= n:
        return None
    tr = []
    for k in range(i - n, i):
        c, p = dl[k], dl[k - 1]['close']
        tr.append(max(c['high'] - c['low'], abs(c['high'] - p), abs(c['low'] - p)))
    return sum(tr) / n


def _rv(dl: list, i: int, n: int = RV_N) -> Optional[float]:
    """Annualised close-to-close vol over the n sessions before index i."""
    if i <= n:
        return None
    r = [math.log(dl[k]['close'] / dl[k - 1]['close']) for k in range(i - n, i)]
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


def compute(direction: str, entry_spot, target, entry_day, premium_pct_spot=None,
            iv_long_pct=None, daily: Optional[list] = None) -> dict:
    """The tag block. Never raises: a field it cannot compute is None and
    `why` says which input was missing."""
    out = {'v': TAGS_VERSION, 'gap_pct': None, 'gap_over_premium': None,
           'tail5_pct': None, 'atr_pct': None, 'rv20_pct': None,
           'iv_over_rv20': None, 'candle_through': None, 'why': None}
    try:
        spot, tgt = float(entry_spot or 0), float(target or 0)
        if spot <= 0 or tgt <= 0 or direction not in ('CE', 'PE'):
            out['why'] = 'no_spot_or_target'
            return out
        out['gap_pct'] = round(100.0 * abs(spot - tgt) / tgt, 3)
        prem = float(premium_pct_spot or 0)
        if prem > 0:
            out['gap_over_premium'] = round(out['gap_pct'] / prem, 3)
        day = str(entry_day)[:10]
        try:
            date.fromisoformat(day)
        except ValueError:
            out['why'] = 'bad_entry_day'
            return out
        if not daily:
            out['why'] = 'no_candles'
            return out
        # Index of the entry session: candles before it are the only inputs,
        # whether or not the entry day itself is already in the cache.
        i = next((k for k, c in enumerate(daily) if c['date'] >= day), len(daily))
        if i == 0:
            out['why'] = 'no_candles_before_entry'
            return out
        last = daily[i - 1]['date']
        out['candle_through'] = last
        if not _continuous(daily, i, last, day):
            out['why'] = 'stale_candles'
            return out
        sign = 1.0 if direction == 'CE' else -1.0
        if i >= TAIL_N:
            out['tail5_pct'] = round(sign * (spot / daily[i - TAIL_N]['close'] - 1.0) * 100.0, 3)
        a = _atr(daily, i)
        if a:
            out['atr_pct'] = round(100.0 * a / spot, 3)
        rv = _rv(daily, i)
        if rv:
            out['rv20_pct'] = round(100.0 * rv, 2)
            iv = float(iv_long_pct or 0)
            if iv > 0:
                out['iv_over_rv20'] = round(iv / out['rv20_pct'], 3)
    except Exception as e:                       # a tag must never stop a shadow
        out['why'] = 'error: %s' % e
    return out


def for_shadow(sh: dict) -> dict:
    """Tags for a shadow row (structure shadow or veto_naked): everything it
    needs is already on the row and its `context`."""
    ctx = sh.get('context') or {}
    try:
        daily = replay.load_daily(sh.get('stock') or '')
    except Exception as e:
        logger.warning('entry_tags: candles for %s unreadable: %s', sh.get('stock'), e)
        daily = None
    return compute(sh.get('direction'), sh.get('entry_spot'), sh.get('target_spot'),
                   sh.get('entry_date') or str(sh.get('since') or '')[:10],
                   ctx.get('premium_pct_spot'), ctx.get('iv_long'), daily)


#: Outcomes worth retrying: the cache can catch up with them (a later scanner
#: fetch or `golive --refresh`). Any other outcome is a property of the row.
RETRY_WHY = ('stale_candles', 'no_candles', 'no_candles_before_entry')


def stamp_missing(shadows: dict, today: Optional[str] = None) -> int:
    """Stamp `context.tags` on every row that lacks it. Returns how many.

    Safe for any row, old or new: every input predates the entry session, so
    a late stamp equals the one that would have been taken at entry. A row
    whose tags came back for a cache reason (`RETRY_WHY`) is retried at most
    ONCE A DAY -- open or closed, since a refreshed cache fills either -- so a
    cycle costs a file read only for rows still waiting, and only once daily."""
    today = today or date.today().isoformat()
    n = 0
    for sh in (shadows or {}).values():
        ctx = sh.setdefault('context', {})
        old = ctx.get('tags')
        if isinstance(old, dict) and old.get('v') == TAGS_VERSION:
            if old.get('why') not in RETRY_WHY or old.get('tried') == today:
                continue
        tags = for_shadow(sh)
        tags['tried'] = today
        ctx['tags'] = tags
        n += 1
    return n

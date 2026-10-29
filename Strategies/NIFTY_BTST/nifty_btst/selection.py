"""Which expiry, which strike. Pure functions over the instrument list.

Rule (Volrix, as published): at 09:20 mark the nearest weekly call and put
whose premium is closest to Rs 100 — **the next weekly on expiry day.**
"""
from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional

from .pricing import Book


class SelectionError(RuntimeError):
    pass


def expiries(options: Iterable[dict]) -> List[date]:
    return sorted({date.fromisoformat(o['expiry']) for o in options})


def pick_expiry(all_expiries: List[date], today: date) -> date:
    live = [e for e in sorted(all_expiries) if e >= today]
    if not live:
        raise SelectionError('no live %s expiry on or after %s' % ('option', today))
    if live[0] == today:
        if len(live) < 2:
            raise SelectionError('expiry day %s and no next expiry listed' % today)
        return live[1]
    return live[0]


def is_expiry_day(all_expiries: List[date], today: date) -> bool:
    return today in set(all_expiries)


def candidates(options: Iterable[dict], expiry: date, spot: float,
               band_points: float) -> List[dict]:
    exp = expiry.isoformat()
    return [o for o in options
            if o['expiry'] == exp and o['instrument_type'] in ('CE', 'PE')
            and abs(float(o['strike']) - spot) <= band_points]


def pick_nearest_premium(cands: List[dict], books: Dict[str, Book],
                         side: str, target: float,
                         spot: float) -> Optional[dict]:
    """Option of `side` whose LTP is closest to `target`.

    Ties go to the strike nearer spot, then the lower strike, so the pick is
    deterministic. An option with no trade yet (LTP 0) is not a candidate.
    Returns None when nothing on that side has a price.
    """
    best, best_key = None, None
    for o in cands:
        if o['instrument_type'] != side:
            continue
        b = books.get(o['tradingsymbol'])
        if b is None or b.ltp <= 0:
            continue
        key = (abs(b.ltp - target), abs(float(o['strike']) - spot),
               float(o['strike']))
        if best_key is None or key < best_key:
            best, best_key = o, key
    return best


def edge_of_band(pick: dict, cands: List[dict]) -> bool:
    """True when the pick is the outermost strike quoted on its side.

    The real nearest-to-target option may then lie outside the band — the
    caller logs it so a too-narrow band is visible instead of silent.
    """
    same = sorted(float(o['strike']) for o in cands
                  if o['instrument_type'] == pick['instrument_type'])
    return bool(same) and float(pick['strike']) in (same[0], same[-1])

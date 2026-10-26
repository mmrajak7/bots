"""What a NIFTY move of +/- N% would do to the open naked book, in rupees.

Report only -- nothing reads this to decide anything. It exists because
docs/NAKED_100_TRADE_TEST.md section 11 accepted a full-reversal exposure of
~48% of capital KNOWINGLY, and asked for the figure that judges that choice to
be recorded every night instead of guessed: "record nightly what NIFTY +3%
would do to the open book, in rupees" (design finding F25, rollout step 2 in
docs/NAKED_LIVE_DESIGN.md). The book is 55 of 58 PE at the time of writing, so
a rally is the move that hurts; the opposite move is shown beside it so a
CE-heavy book later reads just as plainly.

The book is the `naked_long` arm of the structure shadow -- the 100-trade
test's own positions. It is UNCAPPED (paper takes every signal), so it
overstates what the live book can hold (max 8 open, <= Rs 25K each). When the
live naked path exists its records join here; until then this is the closest
measured book there is.

The model, stated so nobody mistakes it for a price:
  stock move   = beta x NIFTY move, beta from daily closes in the shared
                 candle cache (`book_shock_beta_sessions`); 1.0 when there are
                 fewer than `book_shock_beta_min` common returns, and SAID so.
                 Correlation is not 1 -- a real rally moves some names against
                 it -- so this is the systematic part only.
  option value = Black-Scholes at the shocked spot, same IV, same expiry. The
                 IV is backed out of the arm's own session-close mark (a BID,
                 so it reads slightly low) at the spot recorded with it; the
                 entry IV only when the mark cannot carry one.
  The close mark is taken at or after 15:00, and from 15:15 spot is the
  frozen cash price beside a still-trading option (closing auction), so the
  backed-out IV carries that small mismatch.
  No IV crush on a rally, no gap beyond the move: this is a move, not the
  worst case. The accepted worst case (a -85% gap on everything) stays in the
  plan; this number is what a plain 3% day would actually do.

Never raises out of `analyse` -- the digest calls it, and a broken estimate
must not cost the day's digest.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Callable, Dict, List, Optional

from . import config as cfg
from . import ivcalc

logger = logging.getLogger(__name__)

#: The shadow arm whose positions make up the book. Same arm the 100-trade
#: test scores (`structure_shadow.MARKS_ARM`); imported lazily below so this
#: module stays importable without the shadow's broker dependencies.
BOOK_ARM = 'naked_long'
NIFTY_CANDLES = '_nifty50_daily'


def beta(stock_closes: List[dict], nifty_closes: List[dict],
         sessions: int, min_n: int) -> Optional[float]:
    """Slope of the stock's daily return on NIFTY's, over the last `sessions`
    common dates, or None when fewer than `min_n` returns line up.

    Dates are matched, never positions: a missing candle on one side would
    otherwise pair Monday's stock return with Tuesday's NIFTY return.
    """
    n_by_d = {str(c['date'])[:10]: float(c['close']) for c in nifty_closes
              if c.get('close')}
    pairs = [(str(c['date'])[:10], float(c['close'])) for c in stock_closes
             if c.get('close') and str(c['date'])[:10] in n_by_d]
    pairs = pairs[-(sessions + 1):]
    xs, ys = [], []
    for (d0, s0), (d1, s1) in zip(pairs, pairs[1:]):
        n0, n1 = n_by_d[d0], n_by_d[d1]
        if s0 > 0 and n0 > 0:
            ys.append(s1 / s0 - 1.0)
            xs.append(n1 / n0 - 1.0)
    if len(xs) < min_n:
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var


def _open_on(sh: dict, a: dict, day: str) -> bool:
    """Was this arm holding a position at the end of `day`?"""
    if str(sh.get('entry_date', ''))[:10] > day:
        return False
    if a.get('status') == 'open':
        return True
    at = str((a.get('exit') or {}).get('at') or '')[:10]
    return bool(at) and at > day


def _mark_on(a: dict, day: str) -> Optional[tuple]:
    """(date, mark) of the latest session-close mark on or before `day`."""
    marks = {d: m for d, m in (a.get('marks') or {}).items() if d <= day}
    if not marks:
        return None
    d = max(marks)
    return d, marks[d]


def position_shock(sh: dict, a: dict, day: str, pct: float,
                   b: Optional[float], close_on: Callable[[str, str], Optional[float]]
                   ) -> dict:
    """One position, valued now and at NIFTY +pct and -pct. Never raises.

    `close_on(stock, date)` is the candle-cache fallback for a mark recorded
    before marks carried spot. Value and spot must describe the SAME moment,
    so a mark with no spot anywhere is UNPRICED, never paired with entry spot.
    """
    row = {'stock': sh.get('stock'), 'direction': sh.get('direction'),
           'beta': None if b is None else round(b, 2), 'beta_assumed': b is None,
           'basis': None, 'value': None, 'up': None, 'down': None,
           'held': None, 'why': None}
    try:
        kind, qty = sh.get('direction'), int(sh.get('quantity') or 0)
        strike = float(sh.get('long_strike') or 0)
        expiry = date.fromisoformat(str(sh.get('expiry'))[:10])
        if kind not in ('CE', 'PE') or qty <= 0 or strike <= 0:
            row['why'] = 'record lacks direction/quantity/strike'
            return row
        got = _mark_on(a, day)
        if got:
            mday, m = got
            value, spot = m.get('value'), m.get('spot')
            if spot is None:
                spot = close_on(sh.get('stock'), mday)
            row['basis'] = 'close %s' % mday if mday == day else 'STALE close %s' % mday
        else:
            # Entered today and not yet marked at a close: the entry book is
            # the only price there is, and its spot is from the same moment.
            mday, value, spot = (str(sh.get('entry_date'))[:10],
                                 a.get('entry_value'), sh.get('entry_spot'))
            row['basis'] = 'entry %s' % mday
        if not value or not spot or float(value) <= 0 or float(spot) <= 0:
            row['why'] = 'no price and spot from the same moment'
            return row
        value, spot = float(value), float(spot)
        t = max((expiry - date.fromisoformat(mday)).days, 0.5) / 365.0
        rate = cfg.IV_RISK_FREE_RATE
        iv = ivcalc.implied_vol(kind, value, spot, strike, t, rate)
        if iv is None:
            ent = (sh.get('context') or {}).get('iv_long')
            iv = None if not ent else float(ent) / 100.0
            row['iv_basis'] = 'entry'
        else:
            row['iv_basis'] = 'mark'
        if iv is None:
            row['why'] = 'no IV from the mark or the entry'
            return row
        move = (1.0 if b is None else b) * pct / 100.0
        up = ivcalc.bs_price(kind, spot * (1 + move), strike, t, rate, iv)
        dn = ivcalc.bs_price(kind, spot * (1 - move), strike, t, rate, iv)
        row.update(value=round(value, 2), iv=round(100 * iv, 1),
                   held=round(value * qty, 0),
                   up=round((up - value) * qty, 0),
                   down=round((dn - value) * qty, 0))
    except Exception as e:
        row['why'] = 'error: %s' % e
    return row


def _id_key(sid) -> tuple:
    s = str(sid)
    return (0, int(s), '') if s.isdigit() else (1, 0, s)


def open_positions(shadows: Dict[str, dict], day: str) -> List[tuple]:
    """(id, shadow, arm) for every BOOK_ARM position open at the end of
    `day`, in id order."""
    out = []
    for sid in sorted(shadows, key=_id_key):
        sh = shadows[sid]
        a = (sh.get('arms') or {}).get(BOOK_ARM)
        if a and _open_on(sh, a, day):
            out.append((sid, sh, a))
    return out


def shock(shadows: Dict[str, dict], day: str, pct: float,
          betas: Dict[str, Optional[float]],
          close_on: Callable[[str, str], Optional[float]]) -> dict:
    """The whole book. Pure: every input is passed in."""
    rows = []
    for sid, sh, a in open_positions(shadows, day):
        r = position_shock(sh, a, day, pct, betas.get(sh.get('stock')), close_on)
        r['id'] = sid
        rows.append(r)
    priced = [r for r in rows if r['why'] is None]
    return {
        'day': day, 'pct': pct, 'book': 'shadow:%s (paper, uncapped)' % BOOK_ARM,
        'open': len(rows), 'priced': len(priced),
        'held': round(sum(r['held'] for r in priced), 0),
        'up': round(sum(r['up'] for r in priced), 0),
        'down': round(sum(r['down'] for r in priced), 0),
        'sides': {s: sum(1 for r in rows if r['direction'] == s) for s in ('CE', 'PE')},
        'beta_assumed': sum(1 for r in priced if r['beta_assumed']),
        'rows': rows,
    }


def analyse(day: str, state_file=None, load=None) -> dict:
    """Read the shadow book and the candle cache, then `shock`. Never raises."""
    try:
        if state_file is None:
            # Through cfg.LOG_DIR at CALL time, never the shadow module's
            # import-time path: a test that repoints LOG_DIR must not end up
            # reading the production book. The NAME stays the shadow's own.
            from .structure_shadow import STATE_FILE
            state_file = cfg.LOG_DIR / STATE_FILE.name
        if load is None:
            from .replay import load_daily as load
        try:
            with open(state_file, encoding='utf-8') as f:
                shadows = (json.load(f) or {}).get('shadows') or {}
        except FileNotFoundError:
            shadows = {}
        nifty = load(NIFTY_CANDLES) or []
        cache: Dict[str, list] = {}

        def candles(stock):
            if stock not in cache:
                cache[stock] = load(stock) or []
            return cache[stock]

        def close_on(stock, d):
            for c in candles(stock):
                if c['date'] == d:
                    return c['close']
            return None

        # Only the OPEN names: the shadow book only grows, and each beta is a
        # candle file read. A minimum above the window could never be met, so
        # it is held to the window rather than silently assuming 1 forever.
        sessions = cfg.BOOK_SHOCK_BETA_SESSIONS
        min_n = min(cfg.BOOK_SHOCK_BETA_MIN, sessions)
        betas = {}
        for _sid, sh, _a in open_positions(shadows, day):
            s = sh.get('stock')
            if s and s not in betas:
                betas[s] = (beta(candles(s), nifty, sessions, min_n)
                            if nifty else None)
        return shock(shadows, day, cfg.BOOK_SHOCK_PCT, betas, close_on)
    except Exception as e:
        logger.warning('book shock failed: %s', e)
        return {'day': day, 'error': str(e), 'rows': [], 'open': 0, 'priced': 0}


def _rs(x: float) -> str:
    return format(x, '+,.0f')


def render(d: dict) -> List[str]:
    """Markdown lines for the digest."""
    if d.get('error'):
        return ['## NIFTY shock on the open book',
                '- **could not be computed** (%s) — the figure is MISSING '
                'today, not zero' % d['error']]
    pct = d.get('pct')
    L = ['## NIFTY ±%g%% on the open book' % pct]
    if not d.get('open'):
        L.append('- no open %s position' % BOOK_ARM)
        return L
    sides = d.get('sides') or {}
    if not d.get('priced'):
        L.append('**not computable today** — none of the %d open position(s) '
                 'has a price and spot from the same moment (rows below). The '
                 'figure is MISSING, not zero.' % d['open'])
    elif d['priced'] < d['open']:
        L.append('⚠ **PARTIAL: %d of %d open position(s) unpriced** — the '
                 'totals cover the priced ones only.'
                 % (d['open'] - d['priced'], d['open']))
    if d.get('priced'):
        L.append('**NIFTY +%g%% → Rs %s · NIFTY −%g%% → Rs %s** on Rs %s held'
                 % (pct, _rs(d['up']), pct, _rs(d['down']),
                    format(int(d['held']), ',')))
    L.append('%d open (%d PE / %d CE), %d priced  ·  book: %s'
             % (d['open'], sides.get('PE', 0), sides.get('CE', 0),
                d['priced'], d['book']))
    if d.get('beta_assumed'):
        L.append('- β = 1 ASSUMED on %d position(s): too few cached candles '
                 'to measure it' % d['beta_assumed'])
    L.append('')
    L.append('| # | stock | side | β | basis | value | held | +%g%% | −%g%% |'
             % (pct, pct))
    L.append('|---|---|---|---|---|---|---|---|---|')
    for r in d['rows']:
        if r['why']:
            L.append('| %s | %s | %s | | | **unpriced:** %s | | | |'
                     % (r['id'], r['stock'], r['direction'], r['why']))
            continue
        L.append('| %s | %s | %s | %s%s | %s | %.2f | %s | %s | %s |'
                 % (r['id'], r['stock'], r['direction'],
                    '1.00' if r['beta'] is None else '%.2f' % r['beta'],
                    '*' if r['beta_assumed'] else '', r['basis'], r['value'],
                    format(int(r['held']), ','), _rs(r['up']), _rs(r['down'])))
    L.append('')
    L.append('_Model: stock = β × NIFTY, Black-Scholes at the same IV '
             '(from the close mark). A plain move, not the worst case — no IV '
             'change, no gap beyond it._')
    return L

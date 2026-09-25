"""Replay of the live NAKED-LONG rules on daily candles.

The yardstick of the 100-trade test (`docs/NAKED_100_TRADE_TEST.md`, summary
in CLAUDE.md). It answers two questions the live book cannot answer about
itself:

  SELECTION  Did the trades live actually took beat EVERY signal the rules
             produced over the same dates? Live filters (intraday trigger,
             entry gates, vetting, drift cancel); the replay takes them all.
  VETTING    What would the signals the agent VETOED have done, priced the
             same way as the ones it allowed?

What it is. The live signal (ST(10,3) on COMPLETED weekly/monthly candles,
watch band, freshness, trigger band, drift cancel) rebuilt on daily candles,
and a naked ATM option priced by Black-Scholes off the stock's own realised
volatility: target at the ST line, the live debit stop, TIME a fixed number
of sessions before expiry, one position per stock.

How far to trust it -- measured 2026-09-25, not assumed. On the 14 closed
naked shadows it matched the live result's SIGN 14/14 and the exit type 13/14;
on the 9 fully watched ones it averaged +23.3% against the shadow's real
+23.4%. It reproduces the live `st_value` to within 0.1%. On single trades the
SIZE can differ by 20-50 points (GMRAIRPORT +107% live, +79% here): it is a
yardstick for averages over many trades, never a price for one.

What it cannot see: the 5-minute path (a day that touches both the stop and
the target is booked as the stop), IV moving during a trade, vetting, the
entry gates. Those are exactly the things the SELECTION comparison measures,
so the omission is the point, not a flaw.

Reads daily candles from the shared cache (`playbook/backtest_cache`, the one
the scanner already reads and writes). `refresh()` tops it up from Kite; it
is the ONLY network call here, and nothing in this module writes the trade
book, places an order or sends an alert.
"""
from __future__ import annotations

import calendar
import csv
import json
import logging
import math
import time
from datetime import date, datetime, timedelta
from typing import Optional

from . import config as cfg
from . import ivcalc

logger = logging.getLogger(__name__)

#: Round-trip cost, % of the premium paid. The replay prices at the model's
#: fair value (a mid), so the cost has to carry BOTH the fees and the bid-ask:
#: measured 2026-09-25 as ~0.5% fees on the naked shadow's exits plus a
#: ~0.7% half-spread on each side of the cohort's entry books.
COST_PCT = 2.0
#: Implied vol as a multiple of 20-session realised vol, with a floor. Not
#: fitted -- the value the 14/14 calibration was run with.
IV_MULT, IV_FLOOR, RV_DAYS = 1.1, 0.18, 20
#: A watch the replay has not triggered within this many sessions is dropped.
#: A REPLAY assumption -- live keeps a watch until drift/stale/trigger -- kept
#: because it is what the calibration ran with.
WATCH_LIFE = 20
#: Live cancels a watch that drifts past watch_gap_max x this (monitor.py,
#: `gap > cfg.WATCH_GAP_MAX * 1.2`). Pinned by a test against that source.
DRIFT_MULT = 1.2
#: A close-to-close move larger than this is a split or bonus, not a price.
CORP_ACTION_MOVE = 0.25
#: NSE moved monthly stock-option expiry from the last Thursday to the last
#: Tuesday of the month from September 2025.
TUESDAY_EXPIRY_FROM = date(2025, 9, 1)
#: How much history `refresh()` re-fetches, in calendar days.
REFRESH_DAYS = 120


def _cache_dir():
    # The scanner's own cache directory -- one definition, not a copy.
    from playbook.magnet import config as mcfg
    return mcfg.BACKTEST_CACHE


def _touch_threshold() -> float:
    from playbook.magnet import config as mcfg
    return float(mcfg.TOUCHED_THRESHOLD)


# -- candles ---------------------------------------------------------------

def universe() -> list:
    """Every stock with listed options, from the options CSV the engine already
    trusts for symbols and lot sizes. [(symbol, instrument_token)]."""
    out = {}
    with open(cfg.OPTIONS_CSV, newline='') as f:
        for r in csv.DictReader(f):
            if r.get('has_options') == 'Yes' and r.get('stock_instrument_token'):
                out[r['stock_symbol']] = int(r['stock_instrument_token'])
    return sorted(out.items())


def load_daily(symbol: str) -> Optional[list]:
    """Daily candles for one stock, dates normalised to YYYY-MM-DD, or None."""
    p = _cache_dir() / ('%s.json' % symbol)
    if not p.exists():
        return None
    try:
        raw = json.load(open(p))
    except (OSError, ValueError) as e:
        logger.warning('REPLAY cache unreadable for %s: %s', symbol, e)
        return None
    return [{'date': str(c['date'])[:10], 'open': float(c['open']), 'high': float(c['high']),
             'low': float(c['low']), 'close': float(c['close'])} for c in raw]


def cache_last_date(symbols) -> Optional[str]:
    """The most common last candle date across `symbols` -- what the replay can
    see up to. A median, so one stale file cannot hide the rest."""
    ends = sorted(d[-1]['date'] for d in (load_daily(s) for s in symbols) if d)
    return ends[len(ends) // 2] if ends else None


def refresh(kite, symbols=None, days: int = REFRESH_DAYS, pause: float = 0.35) -> dict:
    """Top up the candle cache from Kite. Fetched candles win on overlap; older
    history is never dropped; today's incomplete bar is never written (the
    scanner's own writer enforces that). Returns {'updated', 'failed'}.

    `pause` keeps under Kite's 3 req/s historical limit -- the limit whose
    burn on 2026-08-27 is why the writer exists."""
    from playbook.magnet.scanner import _write_daily_cache
    symbols = symbols if symbols is not None else universe()
    start = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
    end = datetime.now().strftime('%Y-%m-%d')
    updated, failed = 0, []
    for sym, token in symbols:
        try:
            fresh = kite.historical_data(token, start, end, 'day')
        except Exception as e:                     # one bad symbol must not stop the rest
            failed.append(sym)
            logger.warning('REPLAY refresh %s failed: %s', sym, e)
            time.sleep(pause)
            continue
        merged = {c['date']: c for c in (load_daily(sym) or [])}
        for c in fresh:
            d = c['date'].isoformat() if hasattr(c['date'], 'isoformat') else str(c['date'])
            merged[d[:10]] = {'date': d[:10], 'open': c['open'], 'high': c['high'],
                              'low': c['low'], 'close': c['close'], 'volume': c.get('volume', 0)}
        _write_daily_cache(_cache_dir() / ('%s.json' % sym), sym,
                           [merged[k] for k in sorted(merged)])
        updated += 1
        time.sleep(pause)
    logger.info('REPLAY refresh: %d updated, %d failed %s', updated, len(failed), failed[:10])
    return {'updated': updated, 'failed': failed}


# -- the signal --------------------------------------------------------------

def _period_key(d: str, timeframe: str):
    dt = date.fromisoformat(d)
    return (dt.year, dt.month) if timeframe == 'monthly' else dt.isocalendar()[:2]


def st_as_of(daily: list, timeframe: str) -> list:
    """Per day: the ST value of the last COMPLETED period before it, or None.

    Live excludes the forming candle (`playbook/magnet/scanner.py`), so the
    replay must too. Periods are built with the same keys as
    `playbook.compute_st` (calendar month / ISO week)."""
    from playbook.compute_st import compute_supertrend
    keys, periods = [], []
    for c in daily:
        k = _period_key(c['date'], timeframe)
        if keys and keys[-1] == k:
            p = periods[-1]
            p['high'] = max(p['high'], c['high'])
            p['low'] = min(p['low'], c['low'])
            p['close'] = c['close']
        else:
            keys.append(k)
            periods.append({'date': c['date'], 'open': c['open'], 'high': c['high'],
                            'low': c['low'], 'close': c['close']})
    st = compute_supertrend(periods, cfg.ST_PERIOD, cfg.ST_MULTIPLIER)
    offset = len(periods) - len(st)
    by_key = {keys[offset + j]: row['supertrend'] for j, row in enumerate(st)}
    out, prev_key, last_done = [], None, None
    for c in daily:
        k = _period_key(c['date'], timeframe)
        if prev_key is not None and k != prev_key:
            last_done = by_key.get(prev_key)       # the period that just COMPLETED
        prev_key = k
        out.append(last_done)
    return out


def signals(symbol: str, daily: list, since: str, until: str) -> list:
    """Every trigger the live rules would have produced in [since, until].

    Watch: a close whose gap to ST sits in [fresh_entry_gap, watch_gap_max]
    with no touch of the line (within the magnet touch threshold) in the last
    freshness_days sessions. Trigger: a later session trades to the
    trigger_gap_max level -- filled there, or at the open if it opened inside
    the band; skipped as stale if it opened past stale_gap_min or through the
    line. Drift past watch_gap_max x DRIFT_MULT cancels the watch, as live."""
    touch = _touch_threshold()
    closes = [c['close'] for c in daily]
    out = []
    for tf in cfg.ENABLED_TIMEFRAMES:
        sts = st_as_of(daily, tf)
        watch = None
        for i in range(1, len(daily)):
            d = daily[i]['date']
            if d < since or d > until:
                continue
            st = sts[i]
            if not st:
                watch = None
                continue
            if abs(closes[i] / closes[i - 1] - 1) > CORP_ACTION_MOVE:
                watch = None
                continue
            if watch and (abs(watch['st'] - st) > 1e-9 or i - watch['i'] > WATCH_LIFE):
                watch = None
            if watch and abs(closes[i] - st) / st > cfg.WATCH_GAP_MAX * DRIFT_MULT:
                watch = None
            if watch:
                call = watch['dir'] == 'CE'
                lvl = st * (1 - cfg.TRIGGER_GAP_MAX) if call else st * (1 + cfg.TRIGGER_GAP_MAX)
                op = daily[i]['open']
                og = abs(op - st) / st
                if (daily[i]['high'] >= lvl) if call else (daily[i]['low'] <= lvl):
                    through = (call and op > st) or (not call and op < st)
                    if not (through or og < cfg.STALE_GAP_MIN):
                        out.append({'sym': symbol, 'tf': tf, 'dir': watch['dir'], 'i': i,
                                    'date': d, 'entry': op if og <= cfg.TRIGGER_GAP_MAX else lvl,
                                    'target': st})
                    watch = None
                    continue
            g = abs(closes[i] - st) / st
            if (watch is None and cfg.FRESH_ENTRY_GAP <= g <= cfg.WATCH_GAP_MAX
                    and closes[i] != st and cfg_direction_enabled(closes[i], st)):
                fresh = all(min(abs(daily[k][x] - st) / st for x in ('close', 'high', 'low')) >= touch
                            for k in range(max(0, i - cfg.FRESHNESS_DAYS + 1), i + 1))
                if fresh:
                    watch = {'i': i, 'st': st, 'dir': 'CE' if closes[i] < st else 'PE'}
    return out


def cfg_direction_enabled(price: float, st: float) -> bool:
    return ('CE' if price < st else 'PE') in cfg.ENABLED_DIRECTIONS


# -- expiry and sessions --------------------------------------------------------

def _is_session(d: date) -> bool:
    from common import nse_holidays
    return nse_holidays.is_session(d)


def monthly_expiry(year: int, month: int) -> date:
    """Last Thursday (before Sep 2025) or last Tuesday of the month, moved back
    to a session if it lands on a declared holiday."""
    wd = 1 if date(year, month, 1) >= TUESDAY_EXPIRY_FROM else 3
    d = date(year, month, calendar.monthrange(year, month)[1])
    while d.weekday() != wd:
        d -= timedelta(days=1)
    while not _is_session(d):
        d -= timedelta(days=1)
    return d


def expiry_for(d: date) -> Optional[date]:
    """First monthly expiry with min_dte <= DTE <= max_dte, as live picks."""
    y, m = d.year, d.month
    for _ in range(3):
        e = monthly_expiry(y, m)
        if cfg.MIN_DTE <= (e - d).days <= cfg.MAX_DTE:
            return e
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return None


def time_exit_date(expiry: date) -> date:
    """The session `time_sl_days_before_expiry` sessions before expiry."""
    d, n = expiry, 0
    while n < cfg.TIME_SL_DAYS:
        d -= timedelta(days=1)
        if _is_session(d):
            n += 1
    return d


# -- the trade ------------------------------------------------------------------

def realised_vol(daily: list, i: int) -> Optional[float]:
    """Annualised vol of the RV_DAYS log returns ending the session before i."""
    if i <= RV_DAYS:
        return None
    r = [math.log(daily[k]['close'] / daily[k - 1]['close']) for k in range(i - RV_DAYS, i)]
    mu = sum(r) / len(r)
    return math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(252)


def simulate(daily: list, i: int, direction: str, entry: float, target: float,
             expiry: Optional[date] = None) -> Optional[dict]:
    """One naked ATM trade from session i at spot `entry`.

    Per later session, in this order: the OPEN below the stop books at the open
    (a gap is not a stop at its level); the adverse extreme below the stop
    books AT the stop; the target touched books the option at spot = target.
    Same session both -> the stop (the pessimistic reading of a daily bar). The
    entry session can only reach the target. TIME books the close.
    Returns None when it cannot be priced; reason 'open' while unresolved."""
    call = direction == 'CE'
    kind = 'CE' if call else 'PE'
    d0 = date.fromisoformat(daily[i]['date'])
    expiry = expiry or expiry_for(d0)
    rv = realised_vol(daily, i)
    if expiry is None or rv is None or entry <= 0:
        return None
    vol = max(IV_MULT * rv, IV_FLOOR)

    def value(spot, d):
        return ivcalc.bs_price(kind, spot, entry, max((expiry - d).days, 0) / 365.0,
                               cfg.IV_RISK_FREE_RATE, vol)
    v0 = value(entry, d0)
    if v0 <= 0:
        return None
    stop_v = v0 * (1 - cfg.DEBIT_SL_PCT)
    t_exit = time_exit_date(expiry)
    reason, xv, xdate = 'open', None, None
    for k in range(i, len(daily)):
        c = daily[k]
        dk = date.fromisoformat(c['date'])
        fav, adv = (c['high'], c['low']) if call else (c['low'], c['high'])
        tp_hit = fav >= target if call else fav <= target
        if k > i:
            if abs(c['close'] / daily[k - 1]['close'] - 1) > CORP_ACTION_MOVE:
                reason, xv, xdate = 'corp_action', value(daily[k - 1]['close'], dk), c['date']
                break
            vo = value(c['open'], dk)
            if vo <= stop_v:
                reason, xv, xdate = 'stop_gap', vo, c['date']
                break
            if value(adv, dk) <= stop_v:
                reason, xv, xdate = 'stop', stop_v, c['date']
                break
        if tp_hit:
            reason, xv, xdate = 'tp', value(target, dk), c['date']
            break
        if dk >= t_exit:
            reason, xv, xdate = 'time', value(c['close'], dk), c['date']
            break
    out = {'entry_date': daily[i]['date'], 'exit_date': xdate, 'reason': reason,
           'expiry': expiry.isoformat(), 'premium_pct_spot': 100.0 * v0 / entry}
    if xv is not None:
        gross = 100.0 * (xv - v0) / v0
        out.update(gross_pct=gross, net_pct=gross - COST_PCT)
    return out


def replay_window(since: str, until: str, symbols=None) -> list:
    """Every signal in [since, until], one open position per stock, as live."""
    symbols = [s for s, _ in (symbols if symbols is not None else universe())]
    sigs = []
    for s in symbols:
        daily = load_daily(s)
        if daily and len(daily) > 60:
            sigs += [(sg, daily) for sg in signals(s, daily, since, until)]
    sigs.sort(key=lambda x: (x[0]['date'], x[0]['sym']))
    busy_until, trades = {}, []
    for sg, daily in sigs:
        if busy_until.get(sg['sym'], '') >= sg['date']:
            continue
        r = simulate(daily, sg['i'], sg['dir'], sg['entry'], sg['target'])
        if r is None:
            continue
        busy_until[sg['sym']] = r['exit_date'] or '9999-12-31'
        trades.append(dict(r, sym=sg['sym'], dir=sg['dir'], tf=sg['tf']))
    return trades


#: Sessions over which the APPROACH into a trigger is measured.
APPROACH_SESSIONS = 3


def approach_move(daily: list, day: str, entry: float, direction: str,
                  sessions: int = APPROACH_SESSIONS) -> Optional[float]:
    """% the stock moved TOWARD its line over the `sessions` sessions before
    the entry session, ending at the entry price: known at entry, nothing
    after it. Positive = travelling toward the target.

    The one precursor that survived the wrong-way control (2026-09-25, 3,446
    replayed triggers): the fastest fifth reached the line within 3 sessions
    40% of the time against 22% for the rest, and ran +5.1% vs -2.9% a trade
    as a naked ATM option. ONE definition, used by the research and the pack.
    """
    idx = next((k for k, c in enumerate(daily) if c['date'] == day), None)
    if idx is None or idx < sessions or entry <= 0:
        return None
    base = daily[idx - sessions]['close']
    sign = 1.0 if direction == 'CE' else -1.0
    return sign * (entry / base - 1.0) * 100.0


def replay_record(trade: dict) -> Optional[dict]:
    """A live record (entered, vetoed or cancelled) replayed as a naked trade
    from its TRIGGER: same stock, direction, ST target and spot live saw."""
    day = str(trade.get('triggered_at') or trade.get('entry_date') or '')[:10]
    spot = trade.get('trigger_spot') or trade.get('entry_spot')
    target = trade.get('st_value')
    daily = load_daily(trade.get('stock') or '')
    if not (day and spot and target and daily):
        return None
    idx = next((k for k, c in enumerate(daily) if c['date'] == day), None)
    if idx is None:
        return None
    exp = trade.get('expiry')
    return simulate(daily, idx, trade['direction'], float(spot), float(target),
                    date.fromisoformat(str(exp)[:10]) if exp else None)

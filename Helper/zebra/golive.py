"""The go-live decision pack: everything the owner must choose, from the shadows.

`python -m zebra golive`. READ-ONLY on every book -- reads the shadow book
and the trade book, places nothing, alerts nothing. Its ONE write is a cache
of its own: the every-signal replay since 2020 (`replay_history`), which takes
minutes on the Pi, saved under the log dir keyed by the candle-cache end date
so a second run the same day is fast. A failed cache read or write is logged
and the pack runs on regardless.

Owner, 2026-09-24, on what must be settled before going live: *"strategy -
naked or spread, capital needed, max positions, any change to SL, max capital
utilised by a trade (sometimes too high IV in a stock - should we avoid)"*,
measured over the next 30-50 trades. Each section answers one of those:

    1 STRUCTURE    naked vs spread vs spread_wide, on return on PEAK capital,
                   net of every charge and under a one-tick slippage stress
    2 CAPITAL      peak capital tied up at once, max positions open at once,
                   the owner's 1.5x-of-peak planning figure, VIX alongside
    3 POSITIONS    what a cap of N open positions would have done
    4 STOP         every stop level on the ladder, scored after the fact
    5 PER-TRADE    what a ceiling on one trade's capital would have done
    6 BANDS        outcome by implied vol, VIX, premium % of spot, capital
    7 SELECTION    the trades live took vs the REPLAY of every signal, same dates
    8 VETTING      allowed vs vetoed signals, both replayed the same way
    9 TIME EXIT    the test's second hypothesis: exit at the close of session N
                   unless the target was hit, priced on the arm's real marks
   10 APPROACH     descriptive only: trades whose price RAN at the line before
                   entry vs the rest (the hypothesis was withdrawn -- see below)

Above all of them sits THE 100-TRADE TEST (owner, 2026-09-25): real money is
decided on 100 fully watched, closed naked_long shadows against criteria
fixed IN ADVANCE (`docs/NAKED_100_TRADE_TEST.md`, summary in CLAUDE.md).
Until then nothing here chooses a capital, a position limit or a loss cap --
the earlier quick conclusions of that kind were withdrawn as built on too
little data. Sections 2-6 are measurements for AFTER the test, not inputs to
it.

Why PEAK concurrent capital, not the sum of premiums: the owner's correction,
2026-09-24 -- capital is what is tied up AT ONCE. Summing every premium
charged the same rupee four times over and understated return ~4x.

What it will not do: flatter itself. Partial shadows (opened after their
parent entered) are excluded; arms with unresolved positions are marked
CENSORED; an uncosted row is counted as uncosted, never as free; any group
under THIN_N is marked thin; and the one-tick stress is shown beside every
net, because impact beyond the touch is unmeasured until real orders go in.

WHOLE BOOK: reads the whole trade book only as a lookup by shadow id; the
population is the shadow book, which `open_shadows` scopes to the cohort.

RETIRES WHEN: go-live is decided and the chosen structure becomes the live
engine -- then this is rebuilt around the new control.
"""
from __future__ import annotations

import json
import logging
import math
import os
import statistics
from datetime import date, datetime, timedelta
from typing import Optional

from . import config as cfg
from . import fees as fees_mod
from . import replay
from . import structure_shadow as ss
from . import veto_naked

logger = logging.getLogger(__name__)

#: One tick, the smallest price step on NSE stock options. The stress line
#: prices every order one tick worse than the touch it was measured at.
TICK = 0.05
#: Below this many rows a group is printed but marked thin.
THIN_N = 10
#: The 100-trade test, fixed IN ADVANCE on 2026-09-25 so the result cannot
#: move the bar (amended 2026-10-26 -- gap in SE, regime conditions -- and
#: 2026-11-26 -- the bad-weeks stage; see `verdict`). Population: the first
#: TEST_N fully watched naked_long shadows by entry. PASS needs mean net >=
#: PASS_PCT, the gap over the replay >= GAP_SE_MULT SE, the mean still positive
#: without the TOP_K best, and both regime conditions. FAIL below FAIL_PCT.
#: Otherwise extend once to EXTEND_N (bar EXTEND_PASS_PCT).
#: Why 12: two standard errors at n=100 when one trade's net swings ~59%
#: (the replay's measured spread) -- 2 x 59 / sqrt(100).
TEST_N, PASS_PCT, FAIL_PCT = 100, 12.0, 5.0
EXTEND_N, EXTEND_PASS_PCT, TOP_K = 150, 10.0, 3
#: Vetting review trigger, also fixed in advance: with at least this many
#: vetoes replayed, vetoed signals averaging this many points ABOVE allowed
#: ones means the agent is turning away better trades than it lets in.
VET_REVIEW_MIN_N, VET_REVIEW_GAP = 50, 10.0
#: Where the test's evidence starts: vetting armed and the cohort opened.
TEST_SINCE = '2026-08-14'
#: The test's SECOND hypothesis, fixed IN ADVANCE on 2026-09-25: exit at the
#: close of session TIME_EXIT_N after entry unless the target was hit. N=3 is
#: what a walk-forward over (3,4,5,6,7,8,10) chose on 2020-2022 alone, on the
#: live-faithful replay (review 2026-09-25): +1.7 pts a trade over 2020-2026,
#: paired SE 0.37; 2023-2026 went -5.0% -> -3.0%. 1 and 2 are SHOWN, not
#: tested -- picking the best of several on the same data is fitting.
#: SUPPORTED at TEST_N rows when the paired gain is positive and >= 2 paired
#: standard errors. It cannot rescue a failed main test.
TIME_EXIT_N = 3
TIME_EXIT_SHOWN = (1, 2)
#: A session-close mark must be taken at or after this time, so a day whose
#: last good poll was mid-morning is UNPRICED rather than scored as a close.
#: The shadow's own constant -- one definition for the writer and the reader.
MARK_FROM = ss.MARKS_FROM
#: The approach split, DESCRIPTIVE ONLY. Registered on 2026-09-25 as a third
#: hypothesis ("fast approachers do better") on the first replay -- which
#: missed most same-day triggers. On the live-faithful replay it REVERSED:
#: the fastest fifth ran -7.1% a trade against -3.1% for the rest (diff -4.1,
#: SE 1.7), and hit the wrong-way level MORE often (81% vs 70%) -- speed was
#: volatility, not direction. Withdrawn before any test trade existed; kept
#: as a column because the owner asked to watch it. 7.6 = the top-fifth cut
#: of 2020-2022 on the corrected replay.
FAST_APPROACH_PCT = 7.6
#: What an exit that never found a price counts as in the test: the whole
#: premium. Unpriceable books correlate with bad outcomes, so dropping them
#: would flatter the mean; this is the conservative booking, fixed in advance.
UNPRICED_NET_PCT = -100.0
#: "Beats the replay" (amendment 2026-10-26): live mean - replay mean over the
#: same entry dates >= GAP_SE_MULT x SE of the gap, SE_gap = sqrt(SE_live^2 +
#: SE_replay^2), each SE = sample sd / sqrt(n). Exactly 2 SE is MET.
GAP_SE_MULT = 2.0
#: Regime condition 1 ("not one market", 2026-10-26): among the CLOSED test
#: trades, at least this many entered in a week NIFTY rose (the week's last
#: close over the previous week's last close -- Friday over Friday, the last
#: session when Friday is a holiday) OR at least this many CE.
REGIME_MIN_N = 25
#: Regime condition 2 ("not a lucky window"): the every-signal replay mean over
#: the test's entry days must sit AT OR BELOW this percentile of every rolling
#: window of the same number of replay entry days, REGIME_HISTORY_FROM to the
#: test start. Percentile by linear interpolation (numpy's default).
REGIME_PCTL = 75.0
REGIME_HISTORY_FROM = '2020-01-01'
#: The NIFTY 50 daily series in the shared candle cache -- the same file
#: `book_shock` reads. `golive --refresh` does NOT top it up.
NIFTY_CANDLES = '_nifty50_daily'
#: The bad-weeks stage (registered 2026-11-26, after INCONCLUSIVE at 150).
#: A cell is ISO entry week x side; it is replay-negative when the every-signal
#: replay of that cell has a mean net < 0 over at least BADWEEK_CELL_MIN closed
#: replay trades (fewer = unclassified, counted nowhere). Evaluated once, when
#: at least BADWEEK_MIN_CLOSED population trades in replay-negative cells have
#: closed.
BADWEEK_MIN_CLOSED, BADWEEK_CELL_MIN = 40, 3
#: The replay-history cache (see the module docstring). Bump the version when
#: the slim row shape changes, so an old file is ignored rather than misread.
REPLAY_CACHE_NAME, REPLAY_CACHE_VERSION = 'golive_replay_history.json', 1
DEFAULT_CAPS = (6, 8, 10, 12)
DEFAULT_CUTS = (15000, 20000, 25000, 30000, 40000)
PLAN_MULT = 1.5          # owner, 2026-09-24: "plan 1.5x peak capital needed"
ARM_KEYS = ('naked_long', 'naked_hold', 'spread_hold', 'spread_wide', 'naked_run1')


def _dt(s: Optional[str]) -> Optional[datetime]:
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M'):
        try:
            return datetime.strptime(str(s), fmt)
        except (TypeError, ValueError):
            continue
    return None


def _trade_dt(day, tm) -> Optional[datetime]:
    return _dt('%s %s' % (str(day)[:10], str(tm or '09:15:00')[:8]))


# -- rows ----------------------------------------------------------------------

def arm_rows(state: dict, by_id: dict, key: str, now: datetime) -> list:
    """One row per shadow for this arm: capital, interval, and -- if resolved
    and priced -- gross, fees, stress and net."""
    arm = ss.ARMS[key]
    out = []
    for tid, sh in state['shadows'].items():
        a = sh['arms'].get(key)
        if not a:
            continue
        q = int(sh.get('quantity') or 0)
        start = _dt(a.get('since') or sh.get('since'))
        ev = a.get('entry_value') or 0
        r = {'id': tid, 'stock': sh.get('stock'), 'dir': sh.get('direction'),
             'start': start, 'capital': ev * q, 'partial': bool(sh.get('opened_late')),
             'resolved': False, 'priced': False, 'end': now,
             'stress': arm['fills'] * TICK * q, 'ctx': _context(sh, by_id.get(tid))}
        ex = a.get('exit')
        if a.get('status') == 'exited' and ex:
            r['resolved'] = True
            r['end'] = _dt(ex.get('at')) or now
            r['reason'] = ex.get('reason')
            if ex.get('pnl_pct') is not None:
                r['priced'] = True
                r['pct'] = ex['pnl_pct']
                r['gross'] = (ex['value'] - ev) * q
                r['fees'] = ss.arm_fees(arm, sh, a)
                r['net'] = None if r['fees'] is None else r['gross'] - r['fees']
        out.append(r)
    return out


def run_vs_twin(run: list, twin: list) -> dict:
    """`naked_run1` against `naked_long` on the ids where BOTH are priced.
    Paired on % of premium, because the two differ only after the target:
    every id that never reached it carries an identical result on both sides."""
    t = {r['id']: r for r in twin if r.get('priced')}
    g = [(r['pct'] - t[r['id']]['pct'], r) for r in run if r.get('priced') and r['id'] in t]
    d = [x for x, _ in g]
    se = (statistics.stdev(d) / len(d) ** 0.5) if len(d) > 1 else None
    return {'n': len(d), 'ran': sum(1 for _, r in g if str(r.get('reason')).startswith('run_')),
            'gain': statistics.mean(d) if d else 0.0, 'se': se,
            'better': sum(1 for x in d if x > 0.5), 'worse': sum(1 for x in d if x < -0.5)}


def real_rows(state: dict, by_id: dict, now: datetime) -> list:
    """The REAL spread on the shadowed ids -- the control every arm is against."""
    out = []
    for tid, sh in state['shadows'].items():
        t = by_id.get(tid)
        if not t:
            continue
        q = int(t.get('quantity') or 0)
        r = {'id': tid, 'stock': t.get('stock'), 'dir': t.get('direction'),
             'start': _trade_dt(t.get('entry_date'), t.get('entry_time')),
             'capital': (t.get('debit') or 0) * q,
             'partial': bool(sh.get('opened_late')), 'resolved': False,
             'priced': False, 'end': now, 'stress': 4 * TICK * q,
             'ctx': _context(sh, t)}
        if t.get('status') == 'exited' and t.get('pnl') is not None:
            r.update(resolved=True, priced=True,
                     end=_trade_dt(t.get('exit_date'), t.get('exit_time')) or now,
                     pct=t.get('pnl_pct'), gross=t['pnl'],
                     net=t.get('pnl_net'), reason=t.get('exit_reason'))
            r['fees'] = None if r['net'] is None else r['gross'] - r['net']
        out.append(r)
    return out


def _context(sh: dict, trade: Optional[dict]) -> dict:
    """The entry context: stamped live, or derived for shadows that predate it
    (everything but VIX, which only a live read can supply)."""
    if sh.get('context'):
        return sh['context']
    return ss.entry_context(trade, None, 'predates_capture') if trade else {}


# -- arithmetic ----------------------------------------------------------------

def summary(rows: list) -> dict:
    """Win/loss shape of the resolved, PRICED rows. Uncosted rows are counted
    and left out of net -- never added in at a fee of zero."""
    pr = [r for r in rows if r['priced']]
    costed = [r for r in pr if r.get('net') is not None]
    w = [r['pct'] for r in pr if r['pct'] > 0]
    lo = [r['pct'] for r in pr if r['pct'] <= 0]
    aw = sum(w) / len(w) if w else None
    al = sum(lo) / len(lo) if lo else None
    return {
        'n': len(pr), 'open': sum(1 for r in rows if not r['resolved']),
        'unpriced': sum(1 for r in rows if r['resolved'] and not r['priced']),
        'uncosted': len(pr) - len(costed),
        'wins': len(w), 'win_pct': 100.0 * len(w) / len(pr) if pr else None,
        'avg_pct': sum(r['pct'] for r in pr) / len(pr) if pr else None,
        'avg_win': aw, 'avg_loss': al,
        'payoff': (aw / -al) if (aw is not None and al) else None,
        'be_wr': (100.0 * -al / (aw - al)) if (aw is not None and al is not None and aw - al) else None,
        'net': sum(r['net'] for r in costed),
        'fees': sum(r['fees'] for r in costed),
        'stress': sum(r['stress'] for r in costed),
    }


def peak_concurrent(rows: list) -> dict:
    """Peak capital tied up at one time, max positions open at once, and the
    time-weighted average -- over every row, OPEN ones included (they tie up
    capital now). Exits are processed before entries at the same instant."""
    iv = [(r['start'], r['end'], r['capital'], r) for r in rows
          if r['start'] and r['end'] and r['capital'] > 0]
    if not iv:
        return {'peak': 0.0, 'at': None, 'max_open': 0, 'avg': 0.0, 'open_at_peak': []}
    ev = sorted([(a, 1, c, r) for a, b, c, r in iv] + [(b, -1, c, r) for a, b, c, r in iv],
                key=lambda e: (e[0], e[1]))
    cap, live, best, at, most, at_peak = 0.0, [], 0.0, None, 0, []
    for when, d, c, r in ev:
        cap += d * c
        if d > 0:
            live.append(r)
        else:
            live = [x for x in live if x is not r]
        most = max(most, len(live))
        if cap > best + 1e-9:
            best, at, at_peak = cap, when, list(live)
    span = (max(b for _, b, _, _ in iv) - min(a for a, _, _, _ in iv)).total_seconds()
    avg = (sum(c * (b - a).total_seconds() for a, b, c, _ in iv) / span) if span > 0 else best
    return {'peak': best, 'at': at, 'max_open': most, 'avg': avg, 'open_at_peak': at_peak}


def cap_simulation(rows: list, cap: int) -> dict:
    """Take signals in entry order; skip one when `cap` positions are already
    open. What the book would have been under a live position limit.

    Every row OCCUPIES a slot; only non-partial rows are SCORED -- a partial
    shadow's outcome may have missed its own stop, but its capital was real.
    """
    taken = []
    for r in sorted((x for x in rows if x['start']), key=lambda x: x['start']):
        if sum(1 for t in taken if t['end'] > r['start']) < cap:
            taken.append(r)
    s = summary([t for t in taken if not t.get('partial')])
    s['taken'], s['skipped'] = len(taken), len([x for x in rows if x['start']]) - len(taken)
    s['peak'] = peak_concurrent(taken)['peak']
    return s


def ladder_outcomes(state: dict, now: datetime) -> dict:
    """Every stop level, scored on the no-stop arm's own path.

    Level L: if the arm breached L before it resolved, the trade exits at the
    breach print; otherwise at the arm's own TP/TIME exit. Only shadows whose
    arm carries a ladder, is resolved and priced, and is not partial.
    """
    levels = [ss.ladder_key(f) for f in cfg.SHADOW_STOP_LADDER]
    res = {k: {'n': 0, 'stopped': 0, 'gaps': 0, 'net': 0.0, 'pcts': [], 'uncosted': 0}
           for k in levels + ['none']}
    # Not scored, and SAID: an open arm has no outcome yet, and an unpriced
    # exit has no price -- dropping either silently would read as complete.
    res['_skipped'] = {'open': 0, 'unpriced': 0, 'partial': 0}
    for sh in state['shadows'].values():
        a = sh['arms'].get(ss.LADDER_ARM) or {}
        ex = a.get('exit') or {}
        if 'ladder' not in a:
            continue
        if sh.get('opened_late'):
            res['_skipped']['partial'] += 1
            continue
        if a.get('status') != 'exited':
            res['_skipped']['open'] += 1
            continue
        if ex.get('value') is None:
            res['_skipped']['unpriced'] += 1
            continue
        q = int(sh.get('quantity') or 0)
        ev = a['entry_value']
        for k in levels + ['none']:
            hit = a['ladder'].get(k) if k != 'none' else None
            val = hit['value'] if hit else ex['value']
            f = fees_mod.estimate([
                {'leg': 'long', 'side': 'BUY', 'price': float(ev), 'qty': q, 'when': 'entry'},
                {'leg': 'long', 'side': 'SELL', 'price': float(val), 'qty': q, 'when': 'exit'}])
            g = res[k]
            g['n'] += 1
            g['stopped'] += 1 if hit else 0
            g['gaps'] += 1 if (hit and hit.get('gap')) else 0
            g['pcts'].append(100.0 * (val - ev) / ev)
            g['net'] += (val - ev) * q - float(f.get('total') or 0.0)
    return res


def bands(rows: list, field: str) -> list:
    """Terciles of a context field over resolved, priced rows that have it."""
    have = sorted((r for r in rows if r['priced'] and r['ctx'].get(field) is not None),
                  key=lambda r: r['ctx'][field])
    if len(have) < 3:
        return []
    k = len(have)
    cuts = [have[: k // 3], have[k // 3: 2 * k // 3], have[2 * k // 3:]]
    return [(g[0]['ctx'][field], g[-1]['ctx'][field], summary(g)) for g in cuts if g]


# -- the 100-trade test ----------------------------------------------------------

def population(rows: list, n: int) -> list:
    """The FIXED test population: the first `n` fully watched naked_long shadows
    by ENTRY time, open or closed. Fixing it by entry, not by exit, is what
    stops the first closes (fast winners) from standing in for the whole
    sample, and a late winner from flipping a verdict already reached.

    Each row gets `net_pct` (None while open) and `booked`:
      net       closed, priced and costed: net rupees / capital
      uncosted  priced, fees unknown: gross less replay.COST_PCT
      unpriced  closed without a price: UNPRICED_NET_PCT
      open      still running"""
    first = sorted((r for r in rows if not r.get('partial') and r.get('start')),
                   key=lambda r: r['start'])[:n]
    return [_booked(r) for r in first]


def _booked(r: dict) -> dict:
    """The row with `net_pct` and `booked`, scored the way the test scores it."""
    if not r['resolved']:
        return dict(r, net_pct=None, booked='open')
    if not r['priced']:
        return dict(r, net_pct=UNPRICED_NET_PCT, booked='unpriced')
    if r.get('net') is None or not r['capital']:
        return dict(r, net_pct=r['pct'] - replay.COST_PCT, booked='uncosted')
    return dict(r, net_pct=100.0 * r['net'] / r['capital'], booked='net')


def test_rows(rows: list, n: int = TEST_N) -> list:
    """The CLOSED rows of the fixed population -- what can be scored today."""
    return [r for r in population(rows, n) if r['net_pct'] is not None]


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def week_of(d) -> str:
    """ISO entry week, 'YYYY-WW': the cluster outcomes move together in."""
    y, w, _ = (d if isinstance(d, date) else date.fromisoformat(str(d)[:10])).isocalendar()
    return '%d-%02d' % (y, w)


def clustered(vals: list, clusters: list) -> dict:
    """Mean and a cluster-robust SE (clusters = entry weeks).

    Why (Fable review 2026-11-19): trades entered the same week share one
    market move -- entry week x side explained 38% of outcome variance -- so
    the naive SE treats ~10 weekly draws as 80-odd independent ones and
    roughly halves the true uncertainty. With few clusters this SE is itself
    noisy and probably LOW; read it against t(g-1), not 1.96. Reported beside
    the naive SE, never instead of it: the pre-registered gate is unchanged."""
    n = len(vals)
    if n == 0:
        return {'mean': None, 'se': None, 'n': 0, 'g': 0}
    m = sum(vals) / n
    acc = {}
    for v, c in zip(vals, clusters):
        acc[c] = acc.get(c, 0.0) + (v - m)
    g = len(acc)
    se = math.sqrt(sum(x * x for x in acc.values()) / (n * n) * g / (g - 1)) if g > 1 else None
    return {'mean': m, 'se': se, 'n': n, 'g': g}


def within_week_side(tests: list, replayed: list, key: str = 'net_pct') -> dict:
    """Each test trade's `key` minus the mean replay result of EVERY signal
    entered the same ISO week on the same side, clustered by week.

    The selection question with the market taken out: CE trades in a rally
    week are compared with every CE signal of that week, so a gap here is
    picking, not direction. Trades whose week x side has no closed replay
    signal are dropped and counted."""
    cell = {}
    for t in replayed:
        if t.get('net_pct') is None or not t.get('entry_date'):
            continue
        cell.setdefault((week_of(t['entry_date']), t.get('dir')), []).append(t['net_pct'])
    res, wk, dropped = [], [], 0
    for r in tests:
        v = r.get(key)
        k = (week_of(r['start'].date()), r.get('dir'))
        if v is None or k not in cell:
            dropped += 1
            continue
        res.append(v - sum(cell[k]) / len(cell[k]))
        wk.append(k[0])
    return dict(clustered(res, wk), dropped=dropped)


def _stats(nets: list) -> dict:
    sd = _sd(nets)
    return {'n': len(nets), 'mean': _mean(nets), 'se': (sd / math.sqrt(len(nets))) if sd else None,
            'win': (100.0 * sum(x > 0 for x in nets) / len(nets)) if nets else None,
            'ex_top': _mean(sorted(nets)[:-TOP_K]) if len(nets) > TOP_K else None}


def _se(xs: list) -> Optional[float]:
    """Sample sd / sqrt(n); None below two values."""
    sd = _sd(xs)
    return None if sd is None else sd / math.sqrt(len(xs))


# -- the registered gate (amendments 2026-10-26, bad-weeks stage 2026-11-26) ---

def gap_test(live_mean, live_se, replay_mean, replay_se) -> dict:
    """"Beats the replay": gap = live - replay >= GAP_SE_MULT x sqrt(SE_live^2 +
    SE_replay^2). `met` is None -- UNKNOWN, never a pass or a fail -- when any
    input is missing: the old `mean > replay` rule is not a fallback."""
    out = {'gap': None, 'se': None, 'need': None, 'met': None}
    if live_mean is None or replay_mean is None:
        return out
    out['gap'] = live_mean - replay_mean
    if live_se is None or replay_se is None:
        return out
    out['se'] = math.sqrt(live_se ** 2 + replay_se ** 2)
    out['need'] = GAP_SE_MULT * out['se']
    # A hair of float tolerance so "exactly 2 SE" reads MET, as registered.
    out['met'] = out['gap'] >= out['need'] - 1e-9
    return out


def nifty_weeks(daily: Optional[list]) -> dict:
    """{'YYYY-WW': (last session date, close)} plus '_end' = the series' last
    date. The last session of each ISO week -- Friday, or the day before a
    Friday holiday."""
    out = {}
    for c in daily or []:
        try:
            d = date.fromisoformat(str(c['date'])[:10])
            k = week_of(d)
            if k not in out or out[k][0] < d:
                out[k] = (d, float(c['close']))
        except (KeyError, TypeError, ValueError):
            continue
    if out:
        out['_end'] = max(v[0] for v in out.values())
    return out


def nifty_week_up(weeks: dict, day) -> Optional[bool]:
    """Did NIFTY rise over the ISO week containing `day`? None = UNKNOWN: the
    series has no close for this week or the one before, or it ends before
    this week's Friday (a mid-week candle must not stand in for the close)."""
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    monday = d - timedelta(days=d.weekday())
    end = weeks.get('_end')
    this, prev = weeks.get(week_of(monday)), weeks.get(week_of(monday - timedelta(days=7)))
    if end is None or this is None or prev is None or end < monday + timedelta(days=4):
        return None
    return this[1] > prev[1]


def regime_one(tests: list, weeks: dict) -> dict:
    """Condition 1 over CLOSED test rows: >= REGIME_MIN_N entered in a NIFTY-up
    week OR >= REGIME_MIN_N CE. `met` None = undecidable today (CE short and
    the unknown NIFTY weeks could still carry the up count over the line)."""
    out = {'n': len(tests), 'ce': 0, 'pe': 0, 'up': 0, 'down': 0, 'unknown': 0,
           'nifty_end': weeks.get('_end')}
    for r in tests:
        out['ce' if r.get('dir') == 'CE' else 'pe'] += 1
        up = nifty_week_up(weeks, r['start'].date())
        out['unknown' if up is None else ('up' if up else 'down')] += 1
    if out['ce'] >= REGIME_MIN_N or out['up'] >= REGIME_MIN_N:
        out['met'] = True
    elif out['up'] + out['unknown'] < REGIME_MIN_N:
        out['met'] = False
    else:
        out['met'] = None
    return out


def quantile(xs: list, q: float) -> Optional[float]:
    """Linear-interpolation quantile (numpy's default), q in [0, 1]."""
    if not xs:
        return None
    v = sorted(xs)
    pos = q * (len(v) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def regime_two(hist_rows: list, since: str, until: str) -> dict:
    """Condition 2: is the replay over the test's entry days an ordinary window?

    `hist_rows` = CLOSED every-signal replay trades since REGIME_HISTORY_FROM
    ({'entry_date', 'net_pct'}), from ONE run (`replay_history`), so the
    current window and the history are scored identically. Entry days = the
    distinct replay entry dates; L = how many fall in [since, until]; the
    history is every run of L consecutive entry days strictly before `since`,
    each scored as the mean net of the trades entered in it. MET when the
    current window's mean is at or below the REGIME_PCTL-th percentile of
    those (linear interpolation). `met` None when either side is empty.
    `rank` = % of history windows at or below the current one."""
    by_day = {}
    for t in hist_rows:
        d = str(t.get('entry_date') or '')[:10]
        if t.get('net_pct') is None or not d or d > until:
            continue
        s_n = by_day.setdefault(d, [0.0, 0])
        s_n[0] += float(t['net_pct'])
        s_n[1] += 1
    days = sorted(by_day)
    cur = [d for d in days if since <= d <= until]
    prior = [d for d in days if d < since]
    out = {'days': len(cur), 'windows': 0, 'current': None, 'p75': None, 'rank': None,
           'trades': sum(by_day[d][1] for d in cur), 'met': None}
    if not cur:
        return out
    out['current'] = sum(by_day[d][0] for d in cur) / out['trades']
    L = len(cur)
    if len(prior) < L:
        return out
    # Rolling sums over prefix arrays: O(days), not O(days x L).
    cs, cn = [0.0], [0]
    for d in prior:
        cs.append(cs[-1] + by_day[d][0])
        cn.append(cn[-1] + by_day[d][1])
    means = [(cs[j + L] - cs[j]) / (cn[j + L] - cn[j]) for j in range(len(prior) - L + 1)]
    out['windows'] = len(means)
    out['p75'] = quantile(means, REGIME_PCTL / 100.0)
    out['rank'] = 100.0 * sum(m <= out['current'] for m in means) / len(means)
    out['met'] = out['current'] <= out['p75'] + 1e-9
    return out


def cell_of(day, side) -> tuple:
    """A bad-weeks cell: (ISO entry week, side)."""
    return (week_of(day), side)


def classify_cells(hist_rows: list, open_rows=()) -> dict:
    """{cell: {'n', 'mean', 'neg', 'open'}} for every cell with at least
    BADWEEK_CELL_MIN closed replay trades -- the only cells that are classified.
    neg = mean < 0 (a mean of exactly 0 is NOT negative). `open` counts replay
    trades of that cell still running: its class may still move. The key
    '_open' maps EVERY cell (classified or not) to its open replay count."""
    acc = {}
    for t in hist_rows:
        if t.get('net_pct') is None or not t.get('entry_date'):
            continue
        acc.setdefault(cell_of(t['entry_date'], t.get('dir')), []).append(float(t['net_pct']))
    opn = {}
    for t in open_rows:
        if t.get('entry_date'):
            k = cell_of(t['entry_date'], t.get('dir'))
            opn[k] = opn.get(k, 0) + 1
    out = {}
    for k, v in acc.items():
        if len(v) >= BADWEEK_CELL_MIN:
            m = sum(v) / len(v)
            out[k] = {'n': len(v), 'mean': m, 'neg': m < 0, 'open': opn.get(k, 0)}
    out['_open'] = opn
    return out


def historical_neg_share(hist_rows: list, cells: dict, before: str) -> Optional[float]:
    """Share of CLASSIFIED replay trades entered before `before` (the test
    start) that sit in replay-negative cells. None when there are none."""
    neg = tot = 0
    for t in hist_rows:
        d = str(t.get('entry_date') or '')[:10]
        if t.get('net_pct') is None or not d or d >= before:
            continue
        c = cells.get(cell_of(d, t.get('dir')))
        if c is None:
            continue
        tot += 1
        neg += 1 if c['neg'] else 0
    return (neg / tot) if tot else None


def bad_weeks(pop: list, hist: Optional[dict], min_n: int = EXTEND_N) -> dict:
    """The bad-weeks stage over the CONTINUING population (`population` with no
    cap: every fully watched naked_long shadow by entry, the first 150 included).

    Its population is the PREFIX by entry that ends at the BADWEEK_MIN_CLOSED-th
    trade entered in a replay-negative cell (never shorter than the first
    `min_n`): the §3 rule -- fixed by entry, so fast closes cannot stand in
    for slow ones -- applied to this stage's own count. Evaluated once every
    row of that prefix has closed and none of its cells has a replay trade
    still open. PASS needs (1) >= BADWEEK_MIN_CLOSED closed in replay-negative
    cells, (2) selection gap in those cells >= 0 (each live trade minus its
    cell's replay mean, averaged over live trades = cells weighted by live
    trades), (3) mix-reweighted mean > 0: live mean in replay-positive and in
    replay-negative cells, weighted by their historical shares of classified
    replay trades REGIME_HISTORY_FROM -> test start. Unclassified cells count
    nowhere. Anything else FAILS; no replay history is BLOCKED."""
    out = {'state': None, 'why': '', 'n_neg': 0, 'n_neg_closed': 0, 'prefix': None,
           'gap': None, 'mean_neg': None, 'mean_pos': None, 'share_neg': None,
           'mix': None, 'unclassified': 0}
    if not hist or hist.get('rows') is None:
        out.update(state='BLOCKED', why='no every-signal replay history (needs the candle cache)')
        return out
    cells = classify_cells(hist['rows'], hist.get('open') or ())

    def cell(r):
        return cells.get(cell_of(r['start'].date(), r.get('dir')))
    since = min(r['start'] for r in pop).strftime('%Y-%m-%d') if pop else None
    out['share_neg'] = historical_neg_share(hist['rows'], cells, since) if since else None
    k_end, seen = None, 0
    for k, r in enumerate(pop):
        c = cell(r)
        if c is not None and c['neg']:
            seen += 1
            if seen == BADWEEK_MIN_CLOSED:
                k_end = k
                break
    pre = pop[:max(min_n, k_end + 1)] if k_end is not None else pop
    closed = [r for r in pre if r['net_pct'] is not None]
    neg = [r for r in closed if cell(r) is not None and cell(r)['neg']]
    pos = [r for r in closed if cell(r) is not None and not cell(r)['neg']]
    out.update(prefix=len(pre), n_neg=sum(1 for r in pre if cell(r) is not None and cell(r)['neg']),
               n_neg_closed=len(neg), unclassified=sum(1 for r in closed if cell(r) is None),
               mean_neg=_mean([r['net_pct'] for r in neg]), mean_pos=_mean([r['net_pct'] for r in pos]),
               gap=_mean([r['net_pct'] - cell(r)['mean'] for r in neg]))
    if out['mean_neg'] is not None and out['mean_pos'] is not None and out['share_neg'] is not None:
        out['mix'] = out['share_neg'] * out['mean_neg'] + (1 - out['share_neg']) * out['mean_pos']
    if k_end is None:
        out.update(state='IN PROGRESS', why='%d of %d entered in replay-negative cells (%d closed)'
                   % (out['n_neg'], BADWEEK_MIN_CLOSED, out['n_neg_closed']))
        return out
    still = len(pre) - len(closed)
    if still:
        out.update(state='WAITING', why='the first %d by entry hold the %d; %d still open'
                   % (len(pre), BADWEEK_MIN_CLOSED, still))
        return out
    pending = sum(cells['_open'].get(cell_of(r['start'].date(), r.get('dir')), 0) for r in pre)
    if pending:
        out.update(state='WAITING', why='%d replay trades in the cells of the first %d still open'
                   % (pending, len(pre)))
        return out
    # The cache must reach the FRIDAY of the last prefix week: a cell read on
    # Monday-Tuesday signals alone can flip once the week's later signals
    # land, and move which trade is the 40th -- "evaluated once" would not hold.
    last = max(r['start'] for r in pre).date()
    friday = (last + timedelta(days=4 - last.weekday())).isoformat()
    if hist.get('cache_end') and hist['cache_end'] < friday:
        out.update(state='BLOCKED', why='candle cache ends %s, before the end of the last prefix week '
                   '(%s) -- run --refresh' % (hist['cache_end'], friday))
        return out
    if out['share_neg'] is None:
        out.update(state='BLOCKED', why='no classified replay history before the test start')
        return out
    fails = []
    if out['n_neg_closed'] < BADWEEK_MIN_CLOSED:
        fails.append('only %d closed in replay-negative cells' % out['n_neg_closed'])
    if out['gap'] is None or out['gap'] < 0:
        fails.append('selection gap in bad cells %s < 0' % _f(out['gap'], '%+.1f'))
    if out['mix'] is None:
        fails.append('mix-reweighted mean undefined (no live trade in a replay-%s cell)'
                     % ('positive' if out['mean_pos'] is None else 'negative'))
    elif out['mix'] <= 0:
        fails.append('mix-reweighted mean %+.1f%% <= 0' % out['mix'])
    out['state'] = 'FAIL' if fails else 'PASS'
    out['why'] = ('; '.join(fails) if fails else
                  '%d closed in replay-negative cells, gap %+.1f >= 0, mix-reweighted %+.1f%% > 0'
                  % (out['n_neg_closed'], out['gap'], out['mix']))
    return out


def _stage(pop: list, need: int, sel: Optional[dict]) -> dict:
    """Where one stage (100, or the extension to 150) stands. EVAL only when
    all `need` have entered AND closed AND the replay of their window is
    complete -- anything less is a state, never a verdict."""
    still = sum(1 for r in pop if r['net_pct'] is None)
    if len(pop) < need:
        return {'state': 'IN PROGRESS', 'why': '%d of %d entered' % (len(pop), need)}
    if still:
        return {'state': 'WAITING', 'why': 'all %d entered, %d still open' % (need, still)}
    if not sel or sel.get('replay') is None:
        return {'state': 'BLOCKED', 'why': (sel or {}).get('blocked') or 'no replay for the window'}
    if sel.get('open'):
        return {'state': 'WAITING', 'why': '%d replay trades in the window still open -- refresh later' % sel['open']}
    st = _stats([r['net_pct'] for r in pop])
    gap = gap_test(st['mean'], st['se'], sel['replay'], sel.get('replay_se'))
    st.update(state='EVAL', replay=sel['replay'], gap=gap, beats=gap['met'])
    return st


def _regime(regime_fn, tests: list) -> dict:
    """Both regime conditions for a stage, or UNKNOWN when they cannot be read.
    Never raises: an unreadable regime is BLOCKED by the caller, never FAIL."""
    try:
        r = regime_fn(tests) if regime_fn else None
    except Exception as e:                       # the gate must not crash the pack
        logger.warning('golive regime conditions failed: %s', e)
        r = None
    r = r or {}
    return {'one': r.get('one') or {'met': None}, 'two': r.get('two') or {'met': None}}


def verdict(rows: list, sel_fn, regime_fn=None, bad_fn=None) -> dict:
    """The pre-registered verdict over the naked_long rows (§4).

    `sel_fn(tests)` -> the selection dict for a set of closed test rows (the
    replay of the same window, with `replay_se`), or None when it cannot run.
    `regime_fn(tests)` -> {'one': regime_one(...), 'two': regime_two(...)}.
    `bad_fn(pop)` -> bad_weeks(pop, ...) over the uncapped population.

    100:  FAIL if mean < FAIL_PCT. PASS needs mean >= PASS_PCT, gap >= 2 SE,
          positive without the top TOP_K, and both regime conditions.
          Anything else extends ONCE to EXTEND_N.
    150:  FAIL if mean < EXTEND_PASS_PCT, the gap is unmet or the top TOP_K
          carry it. Regime unmet -> INCONCLUSIVE -> the bad-weeks stage
          (PASS or FAIL, no further extension). All met -> PASS.
    An UNKNOWN condition that would decide the outcome (no SE, no NIFTY
    series, no replay history) is BLOCKED -- never FAIL, never INCONCLUSIVE."""
    p100 = population(rows, TEST_N)
    s100 = _stage(p100, TEST_N, sel_fn([r for r in p100 if r['net_pct'] is not None])
                  if len(p100) == TEST_N and all(r['net_pct'] is not None for r in p100) else None)
    if s100['state'] != 'EVAL':
        return dict(s100, stage=TEST_N)
    s100['regime'] = rg = _regime(regime_fn, p100)
    s100['stage'] = TEST_N
    if s100['mean'] < FAIL_PCT:
        return dict(s100, state='FAIL', why='at %d: mean %+.1f%% < %.0f%%' % (TEST_N, s100['mean'], FAIL_PCT))
    returns = s100['mean'] >= PASS_PCT and (s100['ex_top'] or 0) > 0
    conds = (('gap >= 2 SE', s100['beats']), ('regime 1', rg['one']['met']),
             ('regime 2', rg['two']['met']))
    if returns and all(v is True for _, v in conds):
        return dict(s100, state='PASS', why='at %d: mean %+.1f%% >= %.0f%%, gap %+.1f >= %.1f (2 SE), '
                    'holds without the top %d, both regime conditions met'
                    % (TEST_N, s100['mean'], PASS_PCT, s100['gap']['gap'], s100['gap']['need'], TOP_K))
    if returns and not any(v is False for _, v in conds):
        return dict(s100, state='BLOCKED', why='at %d: returns clear the bar but %s cannot be read -- '
                    'PASS or extend depends on it'
                    % (TEST_N, ', '.join(k for k, v in conds if v is None)))
    unmet100 = [k for k, ok in (('mean >= %.0f%%' % PASS_PCT, s100['mean'] >= PASS_PCT),
                                ('ex-top-%d > 0' % TOP_K, (s100['ex_top'] or 0) > 0)) if not ok]
    unmet100 += [('%s unknown' % k) if v is None else k for k, v in conds if v is not True]
    p150 = population(rows, EXTEND_N)
    s150 = _stage(p150, EXTEND_N, sel_fn([r for r in p150 if r['net_pct'] is not None])
                  if len(p150) == EXTEND_N and all(r['net_pct'] is not None for r in p150) else None)
    if s150['state'] != 'EVAL':
        return dict(s150, stage=EXTEND_N, at100=s100, state='EXTENDED -> ' + s150['state'],
                    why='at %d: %+.1f%%, unmet: %s -- extending to %d: %s'
                    % (TEST_N, s100['mean'], ', '.join(unmet100), EXTEND_N, s150['why']))
    s150['regime'] = rg = _regime(regime_fn, p150)
    s150.update(stage=EXTEND_N, at100=s100)
    fails = []
    if s150['mean'] < EXTEND_PASS_PCT:
        fails.append('mean %+.1f%% < %.0f%%' % (s150['mean'], EXTEND_PASS_PCT))
    if s150['beats'] is False:
        fails.append('gap %+.1f < %.1f (2 SE)' % (s150['gap']['gap'], s150['gap']['need']))
    if (s150['ex_top'] or 0) <= 0:
        fails.append('carried by the top %d (%s without them)' % (TOP_K, _f(s150['ex_top'], '%+.1f%%')))
    if fails:
        return dict(s150, state='FAIL', why='at %d: %s' % (EXTEND_N, '; '.join(fails)))
    if s150['beats'] is None:
        return dict(s150, state='BLOCKED', why='at %d: returns clear the bar but the gap SE cannot be '
                    'read' % EXTEND_N)
    r1, r2 = rg['one']['met'], rg['two']['met']
    if r1 is True and r2 is True:
        return dict(s150, state='PASS', why='at %d: mean %+.1f%% >= %.0f%%, gap %+.1f >= %.1f, holds '
                    'without the top %d, both regime conditions met'
                    % (EXTEND_N, s150['mean'], EXTEND_PASS_PCT, s150['gap']['gap'],
                       s150['gap']['need'], TOP_K))
    if r1 is not False and r2 is not False:
        return dict(s150, state='BLOCKED', why='at %d: returns clear the bar but %s cannot be read'
                    % (EXTEND_N, ', '.join(k for k, v in (('regime 1', r1), ('regime 2', r2))
                                           if v is None)))
    unmet = ', '.join(k for k, v in (('regime 1', r1), ('regime 2', r2)) if v is False)
    incon = 'at %d: returns met, %s unmet -> INCONCLUSIVE' % (EXTEND_N, unmet)
    try:
        bw = bad_fn(population(rows, len(rows))) if bad_fn else None
    except Exception as e:
        logger.warning('golive bad-weeks stage failed: %s', e)
        bw = None
    bw = bw or {'state': 'BLOCKED', 'why': 'bad-weeks stage could not be computed'}
    if bw['state'] in ('PASS', 'FAIL'):
        return dict(s150, state=bw['state'], stage='bad_weeks', bad_weeks=bw,
                    why='%s; bad-weeks stage: %s' % (incon, bw['why']))
    return dict(s150, state='INCONCLUSIVE -> BAD WEEKS ' + bw['state'], stage='bad_weeks',
                bad_weeks=bw, why='%s; bad-weeks stage: %s' % (incon, bw['why']))


# -- the replay history: one every-signal run since 2020, cached ------------------

def _slim(t: dict) -> dict:
    return {'entry_date': t.get('entry_date'), 'dir': t.get('dir'), 'net_pct': t.get('net_pct')}


def replay_history(cache_end: Optional[str], n_symbols: int, path=None, run=None) -> Optional[dict]:
    """The every-signal replay from REGIME_HISTORY_FROM to `cache_end`, slim:
    {'rows': closed [{'entry_date','dir','net_pct'}], 'open': [...], 'cached': bool}.

    One run serves regime 2 (current window and history alike) and the
    bad-weeks cells. Running to the cache end and filtering by entry date
    gives the same trades as a run to any earlier `until` (one position per
    stock depends only on earlier signals). Cached under the log dir, keyed
    by (cache_end, n_symbols, version): a second pack the same day reads the
    file. A bad cache file is ignored and rebuilt; a failed write is logged.
    The replay itself may raise -- the caller reports it as unavailable."""
    if not cache_end:
        return None
    path = path or (cfg.LOG_DIR / REPLAY_CACHE_NAME)
    key = {'version': REPLAY_CACHE_VERSION, 'from': REGIME_HISTORY_FROM,
           'cache_end': cache_end, 'n_symbols': int(n_symbols)}
    try:
        with open(str(path), encoding='utf-8') as fh:
            got = json.load(fh)
        if isinstance(got, dict) and got.get('key') == key and isinstance(got.get('rows'), list):
            return {'rows': got['rows'], 'open': got.get('open') or [], 'cached': True,
                    'cache_end': cache_end}
    except (OSError, ValueError):
        pass
    trades = (run or replay.replay_window)(REGIME_HISTORY_FROM, cache_end)
    rows = [_slim(t) for t in trades if t.get('net_pct') is not None]
    opn = [_slim(t) for t in trades if t.get('net_pct') is None]
    if rows:
        tmp = '%s.tmp' % path
        try:
            with open(tmp, 'w', encoding='utf-8') as fh:
                json.dump({'key': key, 'rows': rows, 'open': opn}, fh)
            os.replace(tmp, str(path))
        except OSError as e:
            logger.warning('golive: replay history cache not written (%s) -- next run recomputes', e)
    return {'rows': rows, 'open': opn, 'cached': False, 'cache_end': cache_end}


def selection(tests: list, replayed: Optional[list] = None,
              cache_end: Optional[str] = None, self_nets: Optional[dict] = None) -> dict:
    """The trades live took against the replay of EVERY signal entered over the
    same dates. BLOCKED (replay None) when the candle cache does not reach the
    window's last entry -- a stale cache would silently turn late replay
    trades into 'open' and drop them. `replayed` is injectable for tests."""
    out = {'since': None, 'until': None, 'live': None, 'live_win': None, 'replay': None,
           'replay_win': None, 'open': 0, 'n': 0, 'blocked': None, 'live_se': None,
           'replay_se': None}
    if not tests:
        return out
    out['since'] = min(r['start'] for r in tests).strftime('%Y-%m-%d')
    out['until'] = max(r['start'] for r in tests).strftime('%Y-%m-%d')
    out['live'] = _mean([r['net_pct'] for r in tests])
    out['live_win'] = 100.0 * sum(r['net_pct'] > 0 for r in tests) / len(tests)
    if replayed is None:
        if not cache_end or cache_end < out['until']:
            out['blocked'] = 'candle cache ends %s, before the window (%s) -- run --refresh' % (
                cache_end, out['until'])
            return out
        replayed = replay.replay_window(out['since'], out['until'])
    closed = [t['net_pct'] for t in replayed if t.get('net_pct') is not None]
    out['live_se'] = _se([r['net_pct'] for r in tests])
    out.update(replay=_mean(closed), replay_se=_se(closed), n=len(closed),
               open=len(replayed) - len(closed),
               replay_win=(100.0 * sum(x > 0 for x in closed) / len(closed)) if closed else None)
    # Descriptive (Fable review 2026-11-19): the gap with week x side removed,
    # in total (live price) and as SELECTION only (the live trade's own replay
    # against its cell, so pricing is held on one yardstick). Never gated.
    out['within'] = within_week_side(tests, replayed)
    if self_nets is not None:
        rows = [dict(r, self_net=self_nets.get(r['id'])) for r in tests]
        out['within_sel'] = within_week_side(rows, replayed, key='self_net')
    return out


def vetting(trades: list, replay_fn=None) -> dict:
    """Allowed vs vetoed, each replayed as a naked trade from its trigger.

    `trades` must already be `decided()` -- vetoes never enter, so they never
    carry the cohort stamp `scored()` looks for. The live `veto_shadow` label
    (hit/miss/flat against spot barriers) is reported beside the replay, as a
    second, independent reading of the same vetoes.

    ONE SETUP, ONE ROW. A vetoed signal can re-trigger and be vetoed again on
    the same ST line -- ADANIGREEN was vetoed six times against 1,247.43 in
    September -- and counting each repeat as a new trade multiplies one
    outcome. Measured 2026-09-25: with repeats, vetoed signals read +11.9%;
    one row per setup, about -2%. A setup is (stock, direction, ST line); the
    first record of it that the replay can price stands for it, and the
    repeats are counted, never scored."""
    replay_fn = replay_fn or replay.replay_record
    groups, seen = {}, set()
    for t in sorted(trades, key=lambda x: str(x.get('triggered_at') or '')):
        v = t.get('vet')
        if not isinstance(v, dict) or str(t.get('triggered_at') or '')[:10] < TEST_SINCE:
            continue
        state = v.get('state') or 'unknown'
        g = groups.setdefault(state, {'n': 0, 'nets': [], 'open': 0, 'unpriced': 0,
                                      'repeats': 0, 'labels': {}})
        key = (state, t.get('stock'), t.get('direction'), round(float(t.get('st_value') or 0), 2))
        if key in seen:
            g['repeats'] += 1
            continue
        r = replay_fn(t)
        if r is None:
            g['unpriced'] += 1          # a later repeat may still price this setup
            continue
        seen.add(key)
        g['n'] += 1
        lab = (t.get('veto_shadow') or {}).get('label') if isinstance(t.get('veto_shadow'), dict) else None
        if lab:
            g['labels'][lab] = g['labels'].get(lab, 0) + 1
        if r.get('net_pct') is None:
            g['open'] += 1
        else:
            g['nets'].append(r['net_pct'])
    a = _mean(groups.get('allowed', {}).get('nets', []))
    vet_nets = groups.get('vetoed', {}).get('nets', [])
    v = _mean(vet_nets)
    review = (len(vet_nets) >= VET_REVIEW_MIN_N and a is not None and v is not None
              and v - a >= VET_REVIEW_GAP)
    return {'groups': groups, 'allowed_mean': a, 'vetoed_mean': v, 'review': review}


def _session_after(day, n: int):
    """The n-th trading session after `day` (calendar-aware)."""
    from datetime import timedelta
    from common import nse_holidays
    d, k = day, 0
    while k < n:
        d += timedelta(days=1)
        if nse_holidays.is_session(d):
            k += 1
    return d


def _paths_marks(paths_dir=None) -> dict:
    """{trade id: {date: long-leg bid at the last reliable poll of that day}}
    from the persisted value paths -- the fallback for shadows opened before
    the arm recorded its own `marks` (2026-09-25). Same basis: the long leg at
    its BID, only on a poll the engine judged reliable."""
    import glob
    import json as _json
    d = paths_dir or (cfg.LOG_DIR / 'eod')
    out = {}
    for f in sorted(glob.glob(str(d / 'paths_*.json'))):
        try:
            with open(f, encoding='utf-8') as fh:
                day = _json.load(fh)
        except (OSError, ValueError):
            continue
        for tid, t in (day.get('trades') or {}).items():
            for o in t.get('obs') or []:
                if (o.get('q') == 'ok' and (o.get('long_bid') or 0) > 0 and o.get('ts')
                        and o['ts'][11:16] >= MARK_FROM):
                    out.setdefault(str(tid), {})[o['ts'][:10]] = (o['ts'], float(o['long_bid']))
    return {tid: {day: v[1] for day, v in days.items()} for tid, days in out.items()}


def time_exit_outcome(row: dict, sh: dict, n: int, fallback: Optional[dict] = None) -> dict:
    """The test row re-scored under "exit at the close of session n unless the
    target was hit". Its own exit stands if it came on or before that session
    (TP or stop earlier, or the same trade either way). Otherwise it books the
    session-n close mark, net of the fees that sale would have cost.
    Returns {'net_pct', 'cut'} or {'unpriced': True} -- never a guess."""
    a = sh['arms'][ss.MARKS_ARM]
    start = row['start'].date()
    cutoff = _session_after(start, n)
    # When the exit FIRED, not when it was booked: a TP latched on the cutoff
    # session and booked at the next priced poll still happened in time.
    fired = _dt((a.get('exit') or {}).get('triggered_at')) or row['end']
    if fired.date() <= cutoff:
        return {'net_pct': row['net_pct'], 'cut': False}
    day = cutoff.isoformat()
    m = (a.get('marks') or {}).get(day)
    if m and str(m.get('at', ''))[11:16] < MARK_FROM:
        m = None                        # a morning price is not a close
    val = m['value'] if m else (fallback or {}).get(day)
    if val is None:
        return {'unpriced': True}
    q = int(sh.get('quantity') or 0)
    ev = float(a['entry_value'])
    f = fees_mod.estimate([
        {'leg': 'long', 'side': 'BUY', 'price': ev, 'qty': q, 'when': 'entry'},
        {'leg': 'long', 'side': 'SELL', 'price': float(val), 'qty': q, 'when': 'exit'}])
    net = (float(val) - ev) * q - float(f.get('total') or 0.0)
    return {'net_pct': 100.0 * net / (ev * q), 'cut': True, 'value': val}


def time_exit(tests: list, state: dict, n: int, paths_marks: Optional[dict] = None) -> dict:
    """Paired comparison on the test rows: the time-exit variant vs the rule
    actually run, over the rows the variant can price. Unpriced rows are
    counted, not dropped."""
    pm = paths_marks if paths_marks is not None else _paths_marks()
    pairs, unpriced, cut, cut_later_tp = [], 0, 0, 0
    for r in tests:
        sh = state['shadows'].get(str(r['id']))
        if not sh:
            unpriced += 1
            continue
        o = time_exit_outcome(r, sh, n, pm.get(str(r['id'])))
        if o.get('unpriced'):
            unpriced += 1
            continue
        pairs.append((r['net_pct'], o['net_pct']))
        if o['cut']:
            cut += 1
            cut_later_tp += 1 if r.get('reason') == 'tp' else 0
    diffs = [b - a for a, b in pairs]
    sd = _sd(diffs)
    out = {'n': len(pairs), 'unpriced': unpriced, 'cut': cut, 'cut_later_tp': cut_later_tp,
           'base': _mean([a for a, _ in pairs]), 'variant': _mean([b for _, b in pairs]),
           'gain': _mean(diffs), 'se': (sd / math.sqrt(len(diffs))) if sd is not None else None}
    if len(pairs) < TEST_N:
        out['state'] = 'IN PROGRESS'
    elif out['gain'] > 0 and out['se'] is not None and out['gain'] >= 2 * out['se']:
        out['state'] = 'SUPPORTED'
    else:
        out['state'] = 'NOT SUPPORTED'
    return out


def approach(tests: list, state: dict, load=None) -> dict:
    """DESCRIPTIVE ONLY: the test trades split by their approach into entry
    (see FAST_APPROACH_PCT for why this is no longer a hypothesis). Rows whose
    candles are missing are counted as unknown, never guessed into a group."""
    load = load or replay.load_daily
    fast, rest, unknown, cache = [], [], 0, {}
    for r in tests:
        sh = state['shadows'].get(str(r['id'])) or {}
        stock, spot = sh.get('stock') or r.get('stock'), sh.get('entry_spot')
        if stock not in cache:
            try:
                cache[stock] = load(stock) if stock else None
            except Exception:                  # a bad cache file must not stop the pack
                cache[stock] = None
        mv = (replay.approach_move(cache[stock], r['start'].strftime('%Y-%m-%d'),
                                   float(spot), sh.get('direction') or r.get('dir'))
              if cache[stock] and spot else None)
        if mv is None:
            unknown += 1
        else:
            (fast if mv > FAST_APPROACH_PCT else rest).append(r['net_pct'])
    return {'fast': _stats(fast), 'rest': _stats(rest), 'unknown': unknown}


# -- report --------------------------------------------------------------------

def veto_naked_read(allowed_rows: list, now: datetime, trades: list = (),
                    state: Optional[dict] = None) -> dict:
    """Vetoed setups as real naked options, against the allowed `naked_long`
    rows that entered over the same dates. Read-only.

    Same dates because a comparison across different markets is the exact flaw
    this book exists to remove from section 8. Naked trades track the market and
    its direction, so the `matched` pair keeps only the weeks and sides BOTH
    groups traded: that is the row to read. Every row is scored as the test
    scores it (`_booked`), so a dead book cannot flatter either side. PARTIAL
    rows are left out of both. `flipped` counts vetoed setups that also ENTERED
    on the same line, before or after: real veto outcomes, so they stay in, but
    the reader should know the two groups overlap by that much."""
    st = state if state is not None else veto_naked.load_state()
    rows = [r for r in arm_rows(st, {}, veto_naked.ARM, now) if r['start']]
    out = {'rows': len(rows), 'partial': sum(r['partial'] for r in rows),
           'repeats': sum(len(sh.get('repeat_ids') or []) for sh in st['shadows'].values()),
           'unopened': {}, 'vetoed': None, 'allowed': None, 'flipped': 0, 'since': None}
    for u in st.get('unopened', {}).values():
        why = str(u.get('reason'))
        out['unopened'][why] = out['unopened'].get(why, 0) + 1
    if not rows:
        return out
    out['since'], out['until'] = min(r['start'] for r in rows), max(r['start'] for r in rows)
    entered_keys = {veto_naked.setup_key(t) for t in trades
                    if isinstance(t.get('vet'), dict) and t['vet'].get('state') == 'allowed'
                    and t.get('status') in ('entered', 'exited')}
    out['flipped'] = sum(k in entered_keys for k in st.get('setups', {}))

    def side(xs):
        booked = [_booked(r) for r in xs if not r['partial']]
        nets = [r['net_pct'] for r in booked if r['net_pct'] is not None]
        return {'n': len(nets), 'open': len(booked) - len(nets),
                'unpriced': sum(r['booked'] == 'unpriced' for r in booked),
                'uncosted': sum(r['booked'] == 'uncosted' for r in booked),
                'win': 100.0 * sum(x > 0 for x in nets) / len(nets) if nets else None,
                'mean': _mean(nets), 'se': _sd(nets) / len(nets) ** 0.5 if len(nets) > 1 else None}

    def cell(r):
        return (tuple(r['start'].isocalendar()[:2]), r.get('dir'))
    allowed = [r for r in allowed_rows if r['start'] and out['since'] <= r['start'] <= out['until']]
    both = {cell(r) for r in rows} & {cell(r) for r in allowed}
    out['vetoed'], out['allowed'] = side(rows), side(allowed)
    out['vetoed_matched'] = side([r for r in rows if cell(r) in both])
    out['allowed_matched'] = side([r for r in allowed if cell(r) in both])
    return out


def _veto_naked_lines(state: dict, by_id: dict, now: datetime) -> list:
    vn = veto_naked_read(arm_rows(state, by_id, 'naked_long', now), now, list(by_id.values()))
    if not vn['rows']:
        lines = ['  Nothing yet: rows open only on vetoes decided after this shipped (never backfilled).']
    else:
        lines = ['  %s to %s; %d partial left out; %d re-vetoes not scored; %d vetoed setups also entered'
                 % (vn['since'].strftime('%Y-%m-%d'), vn['until'].strftime('%Y-%m-%d'), vn['partial'],
                    vn['repeats'], vn['flipped']),
                 '  %-22s %4s %5s %5s %6s %9s %6s' % ('group', 'n', 'open', 'unpr', 'win%', 'mean net', 'SE')]
        for label, key in (('vetoed', 'vetoed'), ('allowed', 'allowed'),
                           ('vetoed, same wk+side', 'vetoed_matched'),
                           ('allowed, same wk+side', 'allowed_matched')):
            g = vn[key]
            lines.append('  %-22s %4d %5d %5d %6s %9s %6s%s' % (
                label, g['n'], g['open'], g['unpriced'], _f(g['win'], '%.0f%%'),
                _f(g['mean'], '%+.1f%%'), _f(g['se'], '%.1f'), _thin(g['n'])))
        lines.append('  Read the same wk+side rows: the market and its direction drive most of the rest.')
        lines.append('  !! CENSORED while any row is open: closed rows are the fast ones, on both sides.')
    if vn['unopened']:
        lines.append('  never opened: %s' % ', '.join('%s %d' % kv for kv in sorted(vn['unopened'].items())))
    return lines


def by_side_and_week(tests: list, weeks: dict) -> list:
    """Regime item 3 -- REPORTED, never gated: (label, n, mean, win%) by side
    and by NIFTY week up / down / unknown."""
    groups = {}
    for r in tests:
        up = nifty_week_up(weeks, r['start'].date())
        for g in (r.get('dir') or '?', 'NIFTY ' + ('?' if up is None else ('up' if up else 'down'))):
            groups.setdefault(g, []).append(r['net_pct'])
    order = ['CE', 'PE', 'NIFTY up', 'NIFTY down', 'NIFTY ?']
    out = []
    for g in order + sorted(k for k in groups if k not in order):
        if g in groups:
            xs = groups[g]
            out.append((g, len(xs), _mean(xs), 100.0 * sum(x > 0 for x in xs) / len(xs)))
    return out


def _met(v) -> str:
    return 'MET' if v is True else ('NOT MET' if v is False else 'UNKNOWN')


def _f(x, fmt='%.0f', dash='-'):
    return dash if x is None else fmt % x


def _thin(n):
    return ' (thin)' if n < THIN_N else ''


def report(store, caps=DEFAULT_CAPS, cuts=DEFAULT_CUTS, now=None) -> str:
    now = now or datetime.now(cfg.IST).replace(tzinfo=None)
    state = ss._load()
    # WHOLE BOOK: a lookup by shadow id only. The population is the shadow
    # book, which `open_shadows` already scopes to the cohort.
    by_id = {str(t.get('id')): t for t in store.load_trades()}
    L = []
    p = L.append
    # Two populations, deliberately. OUTCOMES (returns, win rates, the stop)
    # use only fully watched shadows -- a partial one may have missed its own
    # stop. CAPITAL uses every position: a partial shadow's parent tied up
    # real money whether or not the shadow saw its first minutes, and leaving
    # it out understates the capital and the position count the plan needs.
    every = {k: arm_rows(state, by_id, k, now) for k in ARM_KEYS}
    every['REAL spread'] = real_rows(state, by_id, now)
    rows = {k: [r for r in v if not r['partial']] for k, v in every.items()}
    # The replay reads the candle cache and the options CSV; neither may take
    # the whole pack down with it (review 2026-09-25) -- sections 1-6 need neither.
    replay_err, cache_end, n_symbols = None, None, 0
    try:
        uni = [s for s, _ in replay.universe()]
        n_symbols = len(uni)
        cache_end = replay.cache_last_date(uni)
    except Exception as e:
        replay_err = '%s: %s' % (type(e).__name__, e)
    sel_memo, hist_memo = {}, {}

    def sel_fn(t):
        # Memoised on the trade ids: the 'now' line and the stage of the same
        # rows would otherwise run the window replay twice.
        key = tuple(str(r['id']) for r in t)
        if key not in sel_memo:
            sel_memo[key] = _sel_uncached(t)
        return sel_memo[key]

    def _sel_uncached(t):
        if replay_err:
            return None
        try:
            # Each test trade replayed on its own, so selection can be read
            # on one yardstick (within_sel). Only when the replay can run.
            own = None
            if cache_end and t and cache_end >= max(r['start'] for r in t).strftime('%Y-%m-%d'):
                own = {}
                for r in t:
                    rec = by_id.get(r['id'])
                    rp = replay.replay_record(rec) if rec else None
                    own[r['id']] = rp.get('net_pct') if rp else None
            return selection(t, cache_end=cache_end, self_nets=own)
        except Exception as e:
            return {'replay': None, 'blocked': 'replay failed: %s' % e}

    def history():
        # ONE every-signal run since 2020 for the whole pack (cached on disk by
        # candle-cache end). Its failure is reported, never raised.
        if 'h' not in hist_memo:
            hist_memo['h'], hist_memo['err'] = None, None
            if replay_err or not cache_end:
                hist_memo['err'] = replay_err or 'no candle cache'
            else:
                try:
                    hist_memo['h'] = replay_history(cache_end, n_symbols)
                except Exception as e:
                    hist_memo['err'] = '%s: %s' % (type(e).__name__, e)
                    logger.warning('golive: replay history failed: %s', hist_memo['err'])
        return hist_memo['h']
    try:
        weeks = nifty_weeks(replay.load_daily(NIFTY_CANDLES))
    except Exception as e:                     # a bad NIFTY file is UNKNOWN, not a crash
        logger.warning('golive: NIFTY series unreadable: %s', e)
        weeks = {}

    def regime_fn(t):
        closed = [r for r in t if r['net_pct'] is not None]
        out = {'one': regime_one(closed, weeks), 'two': None}
        if not closed:
            return out
        since = min(r['start'] for r in closed).strftime('%Y-%m-%d')
        until = max(r['start'] for r in closed).strftime('%Y-%m-%d')
        if not cache_end or cache_end < until:
            # Same rule as `selection`: a cache short of the window silently
            # drops its last days from the current mean. UNKNOWN, not a reading.
            out['two'] = {'met': None, 'current': None, 'blocked': 'candle cache ends %s, before '
                          'the window (%s) -- run --refresh' % (cache_end, until)}
            return out
        h = history()
        if h is not None:
            out['two'] = regime_two(h['rows'], since, until)
        return out

    def bad_fn(all_pop):
        return bad_weeks(all_pop, history())

    pop = population(every['naked_long'], TEST_N)
    tests = [r for r in pop if r['net_pct'] is not None]
    st_now = _stats([r['net_pct'] for r in tests])
    sel = sel_fn(tests) or {'replay': None, 'live': None, 'n': 0, 'open': 0, 'since': None,
                            'until': None, 'live_win': None, 'replay_win': None,
                            'blocked': replay_err}
    vd = verdict(every['naked_long'], sel_fn, regime_fn, bad_fn)
    gap_now = gap_test(st_now['mean'], st_now['se'], sel.get('replay'), sel.get('replay_se'))
    rg_now = _regime(regime_fn, tests)
    try:
        bw_now = bad_weeks(population(every['naked_long'], len(every['naked_long'])), history())
    except Exception as e:
        bw_now = {'state': 'BLOCKED', 'why': 'bad-weeks read failed: %s' % e}
    booked = {k: sum(1 for r in pop if r['booked'] == k) for k in ('net', 'uncosted', 'unpriced', 'open')}

    p('GO-LIVE DECISION PACK -- measures only, from the structure shadow')
    p('  FORWARD shadows only (live-priced from 2026-09-09). %d shadows.' % len(state['shadows']))
    p('\nTHE 100-TRADE TEST -- the first %d fully watched naked_long shadows BY ENTRY' % TEST_N)
    p('  (criteria fixed in advance: docs/NAKED_100_TRADE_TEST.md, summary in CLAUDE.md)')
    p('  entered %d / %d   closed %d   open %d   | booked: %d net, %d uncosted (gross - %.0f%%), '
      '%d unpriced (at %.0f%%)' % (len(pop), TEST_N, len(tests), booked['open'], booked['net'],
                                   booked['uncosted'], replay.COST_PCT, booked['unpriced'], UNPRICED_NET_PCT))
    p('  closed so far: mean net %s   SE %s   win %s   without top %d: %s' % (
        _f(st_now['mean'], '%+.1f%%'), _f(st_now['se'], '%.1f'), _f(st_now['win'], '%.0f%%'),
        TOP_K, _f(st_now['ex_top'], '%+.1f%%')))
    cl_now = clustered([r['net_pct'] for r in tests], [week_of(r['start'].date()) for r in tests])
    p('  week-clustered SE %s over %d entry weeks (descriptive; trades in one week share a market '
      'move -- read against t(%d), the gate itself is unchanged)'
      % (_f(cl_now['se'], '%.1f'), cl_now['g'], max(cl_now['g'] - 1, 1)))
    p('  vs replay of every signal, same dates: live %s vs replay %s (%d replayed, %d open)%s' % (
        _f(sel['live'], '%+.1f%%'), _f(sel['replay'], '%+.1f%%'), sel['n'], sel['open'],
        ('  !! ' + sel['blocked']) if sel.get('blocked') else ''))
    p('  GAP (gated): live %s - replay %s = %s; SE_gap %s = sqrt(%s^2 + %s^2); need >= %s (%.0f SE) -> %s'
      % (_f(st_now['mean'], '%+.1f'), _f(sel.get('replay'), '%+.1f'), _f(gap_now['gap'], '%+.1f'),
         _f(gap_now['se'], '%.1f'), _f(st_now['se'], '%.1f'), _f(sel.get('replay_se'), '%.1f'),
         _f(gap_now['need'], '%.1f'), GAP_SE_MULT, _met(gap_now['met'])))
    r1 = rg_now['one']
    p('  REGIME 1 (not one market, gated): of %d closed, CE %d / PE %d; entered in a NIFTY-up week %d, '
      'down %d, unknown %d -> %s (needs >= %d CE or >= %d up-week)'
      % (r1.get('n', 0), r1.get('ce', 0), r1.get('pe', 0), r1.get('up', 0), r1.get('down', 0),
         r1.get('unknown', 0), _met(r1['met']), REGIME_MIN_N, REGIME_MIN_N))
    p('    NIFTY series (%s) ends %s -- `--refresh` does NOT top it up; a week it does not reach '
      'is UNKNOWN' % (NIFTY_CANDLES, r1.get('nifty_end') or 'MISSING'))
    p('    by side / NIFTY week (reported, not gated): %s' % ('; '.join(
        '%s n %d %+.1f%% win %.0f%%' % g for g in by_side_and_week(tests, weeks)) or '-'))
    r2 = rg_now['two']
    if r2.get('current') is None:
        p('  REGIME 2 (not a lucky window, gated): UNKNOWN -- %s'
          % (r2.get('blocked') or hist_memo.get('err') or 'no replay history for the window'))
    else:
        p('  REGIME 2 (not a lucky window, gated): replay %+.1f%% over %d entry days (%d trades) sits at '
          'the %s pct of %d same-length windows since %s; p%.0f %s -> %s (needs <= p%.0f)'
          % (r2['current'], r2['days'], r2['trades'], _f(r2['rank'], '%.0f'), r2['windows'],
             REGIME_HISTORY_FROM[:4], REGIME_PCTL, _f(r2['p75'], '%+.1f'), _met(r2['met']), REGIME_PCTL))
    p('  BAD WEEKS (only after INCONCLUSIVE at %d): %d entered / %d closed in replay-negative cells '
      '(evaluated at %d closed); gap there %s, live bad %s / good %s, hist bad share %s, '
      'mix-reweighted %s; %d closed unclassified -- %s'
      % (EXTEND_N, bw_now.get('n_neg', 0), bw_now.get('n_neg_closed', 0), BADWEEK_MIN_CLOSED,
         _f(bw_now.get('gap'), '%+.1f'), _f(bw_now.get('mean_neg'), '%+.1f%%'),
         _f(bw_now.get('mean_pos'), '%+.1f%%'),
         _f(None if bw_now.get('share_neg') is None else 100 * bw_now['share_neg'], '%.0f%%'),
         _f(bw_now.get('mix'), '%+.1f%%'), bw_now.get('unclassified', 0),
         'descriptive until then' if vd.get('stage') != 'bad_weeks' else bw_now['state']))
    p('  VERDICT: %s -- %s' % (vd['state'], vd['why']))
    p('  PASS at %d: mean >= %.0f%% AND gap >= %.0f SE AND positive without the top %d AND both regime'
      % (TEST_N, PASS_PCT, GAP_SE_MULT, TOP_K))
    p('  conditions. FAIL below %.0f%%. Otherwise extend once to %d: FAIL below %.0f%%, gap unmet or'
      % (FAIL_PCT, EXTEND_N, EXTEND_PASS_PCT))
    p('  carried by the top %d; regime unmet -> INCONCLUSIVE -> the bad-weeks stage (PASS/FAIL, no'
      % TOP_K)
    p('  more extension). An unreadable condition is BLOCKED. No early stop; the verdict waits until')
    p('  every one has closed and the replay of their window is complete.')
    p('  candle cache ends %s -- `python -m zebra golive --refresh` fetches from Kite first.'
      % (cache_end or 'MISSING'))
    if replay_err:
        p('  !! replay UNAVAILABLE (%s) -- sections 7-10 cannot be computed' % replay_err)
    sc = ss.scorecard()
    cens = [k for k in ARM_KEYS if ss.censored(sc, k)]
    if cens:
        p('  !! CENSORED (unresolved positions -- win rates not final): %s' % ', '.join(cens))

    p('\n1 STRUCTURE -- return on PEAK capital, net of all charges; stress = +1 tick every order')
    p('  %-12s %4s %4s %6s %7s %7s %6s %5s %9s %8s %9s %9s %7s %7s' % (
        'arm', 'n', 'open', 'win%', 'avgWin', 'avgLoss', 'payoff', 'BEWR', 'net Rs', 'fees',
        'stress', 'peak cap', 'RoC', 'RoC/str'))
    for k in ('naked_long', 'REAL spread', 'spread_wide', 'naked_hold', 'spread_hold', 'naked_run1'):
        s, pk = summary(rows[k]), peak_concurrent(rows[k])['peak']
        p('  %-12s %4d %4d %6s %7s %7s %6s %5s %9.0f %8.0f %9.0f %9.0f %7s %7s%s' % (
            k, s['n'], s['open'], _f(s['win_pct'], '%.0f%%'), _f(s['avg_win'], '%+.1f'),
            _f(s['avg_loss'], '%+.1f'), _f(s['payoff'], '%.2f'), _f(s['be_wr'], '%.0f%%'),
            s['net'], s['fees'], s['stress'], pk,
            _f(100 * s['net'] / pk if pk else None, '%+.1f%%'),
            _f(100 * (s['net'] - s['stress']) / pk if pk else None, '%+.1f%%'),
            _thin(s['n']) + (' uncosted %d' % s['uncosted'] if s['uncosted'] else '')))
    p('  spread_wide exists only on shadows opened from 2026-09-24; compare it on its own ids.')
    run = run_vs_twin(rows['naked_run1'], rows['naked_long'])
    p('  naked_run1 (hold past the target, floor %.1f%%, %d session) vs naked_long, same ids: %s'
      % (cfg.SHADOW_RUN_FLOOR_PCT, cfg.SHADOW_RUN_HOLD_SESSIONS,
         'none resolved yet' if not run['n'] else
         'n %d, %d reached the target, gain %+.1f pts (SE %s), better %d / worse %d%s'
         % (run['n'], run['ran'], run['gain'], _f(run['se'], '%.1f'), run['better'],
            run['worse'], _thin(run['n']))))

    p('\n2 CAPITAL -- what is tied up AT ONCE (every position: open and partial included)')
    for k in ('naked_long', 'REAL spread'):
        c = peak_concurrent(every[k])
        vix = [r['ctx'].get('vix') for r in c['open_at_peak'] if r['ctx'].get('vix')]
        p('  %-12s peak Rs %s at %s, max %d open at once, time-weighted avg Rs %s'
          % (k, '{:,.0f}'.format(c['peak']), c['at'].strftime('%Y-%m-%d %H:%M') if c['at'] else '-',
             c['max_open'], '{:,.0f}'.format(c['avg'])))
        p('  %-12s plan at %.1fx peak = Rs %s; VIX of positions open at the peak: %s'
          % ('', PLAN_MULT, '{:,.0f}'.format(PLAN_MULT * c['peak']),
             ('%.1f-%.1f' % (min(vix), max(vix))) if vix else 'not captured (pre-09-24)'))

    p('\n3 POSITIONS -- naked_long under a cap on open positions (signals taken in entry order;')
    p('  every position occupies a slot, outcomes counted on fully watched ones only)')
    p('  %-6s %6s %7s %4s %6s %9s %9s %7s' % ('cap', 'taken', 'skipped', 'n', 'win%', 'net Rs', 'peak cap', 'RoC'))
    for cap in list(caps) + [10 ** 6]:
        s = cap_simulation(every['naked_long'], cap)
        p('  %-6s %6d %7d %4d %6s %9.0f %9.0f %7s%s' % (
            'none' if cap >= 10 ** 6 else cap, s['taken'], s['skipped'], s['n'],
            _f(s['win_pct'], '%.0f%%'), s['net'], s['peak'],
            _f(100 * s['net'] / s['peak'] if s['peak'] else None, '%+.1f%%'), _thin(s['n'])))

    p('\n4 STOP -- every level scored on the no-stop arm\'s path (only shadows with a ladder)')
    lad = ladder_outcomes(state, now)
    skipped = lad.pop('_skipped')
    if not any(g['n'] for g in lad.values()):
        p('  no resolved shadow carries a ladder yet -- it starts with shadows opened 2026-09-24.')
    else:
        p('  %-6s %4s %8s %5s %7s %9s' % ('stop', 'n', 'stopped', 'gaps', 'avg%', 'net Rs'))
        for k, g in lad.items():
            if g['n']:
                p('  %-6s %4d %8d %5d %+7.1f %9.0f%s' % (
                    ('-%s%%' % k[1:]) if k != 'none' else 'none', g['n'], g['stopped'],
                    g['gaps'], sum(g['pcts']) / g['n'], g['net'], _thin(g['n'])))
        p('  gaps = breached overnight; a real stop fills at the open, not at its level.')
    if any(skipped.values()):
        p('  not scored: %d still open, %d unpriced exit, %d partial'
          % (skipped['open'], skipped['unpriced'], skipped['partial']))

    p('\n5 PER-TRADE CAPITAL -- naked_long if trades above a ceiling were skipped')
    p('  %-9s %4s %6s %9s %9s %7s' % ('ceiling', 'n', 'win%', 'net Rs', 'peak cap', 'RoC'))
    for cut in list(cuts) + [None]:
        kept = [r for r in rows['naked_long'] if cut is None or r['capital'] <= cut]
        s, pk = summary(kept), peak_concurrent(kept)['peak']
        p('  %-9s %4d %6s %9.0f %9.0f %7s%s' % (
            'none' if cut is None else '{:,}'.format(cut), s['n'], _f(s['win_pct'], '%.0f%%'),
            s['net'], pk, _f(100 * s['net'] / pk if pk else None, '%+.1f%%'), _thin(s['n'])))

    p('\n6 BANDS -- naked_long outcome by entry context (terciles)')
    for field, label in (('iv_long', 'implied vol %'), ('vix', 'India VIX'),
                         ('premium_pct_spot', 'premium % spot'), ('capital_naked', 'capital Rs')):
        b = bands(rows['naked_long'], field)
        if not b:
            p('  %-16s not enough data yet' % label)
            continue
        for lo, hi, s in b:
            p('  %-16s %9s-%-9s n=%-3d win %4s  avg %+6.1f%%  net Rs %8.0f%s' % (
                label, _f(lo, '%.1f' if field != 'capital_naked' else '%.0f'),
                _f(hi, '%.1f' if field != 'capital_naked' else '%.0f'), s['n'],
                _f(s['win_pct'], '%.0f%%'), s['avg_pct'], s['net'], _thin(s['n'])))
            label = ''
    p('\n7 SELECTION -- the trades live took vs the replay of EVERY signal, same dates')
    if not tests:
        p('  no closed trade in the test population yet.')
    else:
        p('  window %s .. %s (entry dates of the closed test trades)' % (sel['since'], sel['until']))
        p('  live    n=%-4d win %4s  mean net %s' % (len(tests), _f(sel['live_win'], '%.0f%%'),
                                                   _f(sel['live'], '%+.1f%%')))
        p('  replay  n=%-4d win %4s  mean net %s  (%d still open in the replay)' % (
            sel['n'], _f(sel['replay_win'], '%.0f%%'), _f(sel['replay'], '%+.1f%%'), sel['open']))
        if sel['live'] is not None and sel['replay'] is not None:
            p("  difference %+.1f points -- positive means live's filters picked better trades"
              % (sel['live'] - sel['replay']))
        for lab, k in (('within week x side, live price', 'within'),
                       ('within week x side, SELECTION only (own replay)', 'within_sel')):
            w = sel.get(k)
            if w and w.get('n'):
                p('  %-48s %s  SE %s (week-clustered, %d weeks, n %d, %d without a cell)'
                  % (lab, _f(w['mean'], '%+.1f pts'), _f(w['se'], '%.1f'), w['g'], w['n'], w['dropped']))
        if sel.get('blocked'):
            p('  !! %s' % sel['blocked'])
    p('  The replay alone over 2020-2026: -3.9% a trade (n=6,187, one position per stock);')
    p('  this gap is the thing under test.')

    p('\n8 VETTING -- allowed vs vetoed, each replayed as a naked trade from its trigger')
    # `decided`: vetoes never enter, so they never carry the cohort stamp.
    from .trade_store import decided
    try:
        vt = None if replay_err else vetting(decided(store.load_trades()))
    except Exception as e:
        vt, replay_err_v = None, str(e)
    if vt is None:
        p('  UNAVAILABLE -- the replay cannot run here (%s)' % (replay_err or 'error'))
        vt = {'groups': {}, 'allowed_mean': None, 'vetoed_mean': None, 'review': False}
    p('  %-11s %6s %8s %5s %8s %7s %5s %9s  %s' % ('verdict', 'setups', 'replayed', 'open', 'unpriced',
                                                   'repeats', 'win%', 'mean net', 'veto_shadow (spot barriers)'))
    for state_, g in sorted(vt['groups'].items(), key=lambda kv: -kv[1]['n']):
        nets = g['nets']
        p('  %-11s %6d %8d %5d %8d %7d %5s %9s  %s' % (
            state_, g['n'], len(nets), g['open'], g['unpriced'], g['repeats'],
            _f(100.0 * sum(x > 0 for x in nets) / len(nets) if nets else None, '%.0f%%'),
            _f(_mean(nets), '%+.1f%%'),
            ', '.join('%s %d' % kv for kv in sorted(g['labels'].items())) or '-'))
    if vt['allowed_mean'] is not None and vt['vetoed_mean'] is not None:
        p('  allowed minus vetoed: %+.1f points (positive = the vetoes avoided worse trades)'
          % (vt['allowed_mean'] - vt['vetoed_mean']))
    p('  One row per setup (stock, direction, ST line); re-vetoes of it are counted as repeats.')
    p('  Descriptive only: proving a 5-point vetting edge needs ~800 signals per group.')
    p('  REVIEW TRIGGER (fixed in advance): >= %d vetoes replayed and vetoed averaging >= %.0f'
      % (VET_REVIEW_MIN_N, VET_REVIEW_GAP))
    p('  points ABOVE allowed.  Now: %s' % ('!! TRIPPED -- review the vetting' if vt['review'] else 'not tripped'))

    p('\n8b VETO_NAKED -- every veto as a real naked option, same exits as naked_long, own book')
    # This report returns its text at the end, so a crash here would also lose
    # the 100-trade verdict printed above it.
    try:
        for line in _veto_naked_lines(state, by_id, now):
            p(line)
    except Exception as e:
        p('  UNAVAILABLE -- %r' % e)

    p('\n9 TIME EXIT -- second hypothesis: exit at the close of session N unless the target')
    p('  was hit, on the SAME fixed test trades, priced on the arm\'s real session-close marks')
    p('  (at or after %s; value paths for shadows older than the marks)' % MARK_FROM)
    p('  %-9s %4s %8s %5s %11s %9s %9s %14s  %s' % ('N', 'n', 'unpriced', 'cut', 'cut then TP',
                                                  'as run', 'variant', 'gain +- SE', 'verdict'))
    try:
        pm = _paths_marks()
    except Exception:
        pm = {}
    for n in (TIME_EXIT_N,) + TIME_EXIT_SHOWN:
        te = time_exit(tests, state, n, pm)
        p('  %-9s %4d %8d %5d %11d %9s %9s %14s  %s' % (
            ('%d TESTED' % n) if n == TIME_EXIT_N else ('%d shown' % n), te['n'], te['unpriced'],
            te['cut'], te['cut_later_tp'], _f(te['base'], '%+.1f%%'), _f(te['variant'], '%+.1f%%'),
            ('%+.1f +- %.1f' % (te['gain'], te['se'])) if te['se'] is not None else _f(te['gain'], '%+.1f'),
            te['state'] if n == TIME_EXIT_N else '(not tested)'))
    p('  SUPPORTED at %d trades if the gain is positive and >= 2 SE. "cut then TP" = trades the'
      % TEST_N)
    p('  time exit closed that went on to hit the target -- the late winners it costs.')
    p('  Replay 2020-2026 said +1.7 pts a trade at N=3 (paired SE 0.37); this is the real-price check.')

    p('\n10 APPROACH -- DESCRIPTIVE ONLY: moved > %.1f%% toward the line in the 3 sessions before'
      % FAST_APPROACH_PCT)
    p('  entry (`replay.approach_move`) vs the rest, on the same test trades')
    try:
        ap = approach(tests, state)
        p('  %-8s %4s %6s %9s' % ('group', 'n', 'win%', 'mean net'))
        for g in ('fast', 'rest'):
            p('  %-8s %4d %6s %9s' % (g, ap[g]['n'], _f(ap[g]['win'], '%.0f%%'), _f(ap[g]['mean'], '%+.1f%%')))
        if ap['unknown']:
            p('  %d trade(s) without candles for the approach -- run with --refresh' % ap['unknown'])
    except Exception as e:
        p('  UNAVAILABLE (%s)' % e)
    p('  Withdrawn as a hypothesis on 2026-09-25: on the live-faithful replay the fastest fifth ran')
    p('  -7.1% a trade vs -3.1% for the rest -- speed tracked volatility, not direction.')

    p('\n  Every number here is PAPER: fills at the touch, fees modelled. Impact beyond the')
    p('  touch is unmeasured until real orders go in -- read the stress column, not just net.')
    return '\n'.join(L)

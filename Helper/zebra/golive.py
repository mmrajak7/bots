"""The go-live decision pack: everything the owner must choose, from the shadows.

`python -m zebra golive`. READ-ONLY -- reads the shadow book and the trade
book, writes nothing, places nothing, alerts nothing.

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

from datetime import datetime
from typing import Optional

from . import config as cfg
from . import fees as fees_mod
from . import structure_shadow as ss

#: One tick, the smallest price step on NSE stock options. The stress line
#: prices every order one tick worse than the touch it was measured at.
TICK = 0.05
#: Below this many rows a group is printed but marked thin.
THIN_N = 10
#: The owner's decision point: revisit at 25-30 resolved, measure to 50.
DECIDE_AT, MEASURE_TO = 30, 50
DEFAULT_CAPS = (6, 8, 10, 12)
DEFAULT_CUTS = (15000, 20000, 25000, 30000, 40000)
PLAN_MULT = 1.5          # owner, 2026-09-24: "plan 1.5x peak capital needed"
ARM_KEYS = ('naked_long', 'naked_hold', 'spread_hold', 'spread_wide')


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


# -- report --------------------------------------------------------------------

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
    nl = summary(rows['naked_long'])['n']

    p('GO-LIVE DECISION PACK -- measures only, from the structure shadow')
    p('  FORWARD shadows only (live-priced from 2026-09-09). The earlier cohort was')
    p('  replayed once from stored value paths on 2026-09-24 and is not re-run here.')
    p('  %d shadows, partial ones excluded. naked_long resolved: %d  (decide at %d, measure to %d)'
      % (len(state['shadows']), nl, DECIDE_AT, MEASURE_TO))
    if nl < DECIDE_AT:
        p('  !! NOT YET DECIDABLE -- %d more resolved naked_long rows before the revisit.' % (DECIDE_AT - nl))
    sc = ss.scorecard()
    cens = [k for k in ARM_KEYS if ss.censored(sc, k)]
    if cens:
        p('  !! CENSORED (unresolved positions -- win rates not final): %s' % ', '.join(cens))

    p('\n1 STRUCTURE -- return on PEAK capital, net of all charges; stress = +1 tick every order')
    p('  %-12s %4s %4s %6s %7s %7s %6s %5s %9s %8s %9s %9s %7s %7s' % (
        'arm', 'n', 'open', 'win%', 'avgWin', 'avgLoss', 'payoff', 'BEWR', 'net Rs', 'fees',
        'stress', 'peak cap', 'RoC', 'RoC/str'))
    for k in ('naked_long', 'REAL spread', 'spread_wide', 'naked_hold', 'spread_hold'):
        s, pk = summary(rows[k]), peak_concurrent(rows[k])['peak']
        p('  %-12s %4d %4d %6s %7s %7s %6s %5s %9.0f %8.0f %9.0f %9.0f %7s %7s%s' % (
            k, s['n'], s['open'], _f(s['win_pct'], '%.0f%%'), _f(s['avg_win'], '%+.1f'),
            _f(s['avg_loss'], '%+.1f'), _f(s['payoff'], '%.2f'), _f(s['be_wr'], '%.0f%%'),
            s['net'], s['fees'], s['stress'], pk,
            _f(100 * s['net'] / pk if pk else None, '%+.1f%%'),
            _f(100 * (s['net'] - s['stress']) / pk if pk else None, '%+.1f%%'),
            _thin(s['n']) + (' uncosted %d' % s['uncosted'] if s['uncosted'] else '')))
    p('  spread_wide exists only on shadows opened from 2026-09-24; compare it on its own ids.')

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
    p('\n  Every number here is PAPER: fills at the touch, fees modelled. Impact beyond the')
    p('  touch is unmeasured until real orders go in -- read the stress column, not just net.')
    return '\n'.join(L)

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
    7 SELECTION    the trades live took vs the REPLAY of every signal, same dates
    8 VETTING      allowed vs vetoed signals, both replayed the same way
    9 TIME EXIT    the test's second hypothesis: exit at the close of session N
                   unless the target was hit, priced on the arm's real marks
   10 APPROACH     the third: trades whose price RAN at the line before entry
                   (fast approach) vs the rest, on the same test trades

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

import math
from datetime import datetime
from typing import Optional

from . import config as cfg
from . import fees as fees_mod
from . import replay
from . import structure_shadow as ss

#: One tick, the smallest price step on NSE stock options. The stress line
#: prices every order one tick worse than the touch it was measured at.
TICK = 0.05
#: Below this many rows a group is printed but marked thin.
THIN_N = 10
#: The 100-trade test, fixed IN ADVANCE on 2026-09-25 so the result cannot
#: move the bar. Population: fully watched (not partial), closed, priced and
#: costed naked_long shadows. PASS needs all three of: mean net >= PASS_PCT,
#: mean net above the replay of every signal over the same dates, and the
#: mean still positive with the TOP_K best trades removed. FAIL below
#: FAIL_PCT. In between: extend once to EXTEND_N, where PASS needs
#: EXTEND_PASS_PCT with the same two conditions, and anything else FAILS.
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
#: the value a walk-forward chose on 2020-2022 alone (2023-2026 then went
#: -2.9% -> -0.1% a trade in the replay); 1 and 2 did better on the full
#: sample and are SHOWN, not tested -- picking the best of three on the same
#: data is how a fitted number passes for a finding. SUPPORTED at TEST_N rows
#: when the paired gain is positive and at least 2 paired standard errors.
#: It cannot rescue a failed main test: that would be a new hypothesis and a
#: new test.
TIME_EXIT_N = 3
TIME_EXIT_SHOWN = (1, 2)
#: The test's THIRD hypothesis, fixed IN ADVANCE on 2026-09-25: a trade is
#: FAST when the stock moved more than FAST_APPROACH_PCT toward its line over
#: the 3 sessions before entry (`replay.approach_move`). 4.2 is the top-fifth
#: cut of 2020-2022 ALONE -- 2023-2026 then ran fast +5.0% vs rest -2.9% a
#: trade in the replay -- not the full-sample 3.7%. Only ~1 in 5 trades is
#: fast, so 100 test trades hold ~20: too few to prove it live. The evidence
#: is the replay; the live test only has to NOT CONTRADICT it (fast mean >=
#: rest mean at the verdict). A fast-only filter becomes a go-live candidate
#: only if the main test PASSES; after a FAIL it is a new hypothesis and a
#: new test.
FAST_APPROACH_PCT = 4.2
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


# -- the 100-trade test ----------------------------------------------------------

def test_rows(rows: list) -> list:
    """The test population: fully watched, closed, priced AND costed rows, each
    with `net_pct` = net rupees / capital. An uncosted row is left out rather
    than counted at zero fees, which would flatter the mean."""
    out = []
    for r in rows:
        if r.get('partial') or not r['priced'] or r.get('net') is None or not r['capital']:
            continue
        out.append(dict(r, net_pct=100.0 * r['net'] / r['capital']))
    return sorted(out, key=lambda r: r['start'])


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def _sd(xs):
    if len(xs) < 2:
        return None
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def verdict(nets: list, replay_mean: Optional[float]) -> dict:
    """The pre-registered verdict. Before TEST_N it is IN PROGRESS whatever the
    numbers say -- stopping early on a good run is how a lucky patch gets
    mistaken for an edge."""
    n = len(nets)
    mean = _mean(nets)
    sd = _sd(nets)
    ex_top = _mean(sorted(nets)[:-TOP_K]) if n > TOP_K else None
    beats = (mean is not None and replay_mean is not None and mean > replay_mean)
    out = {'n': n, 'mean': mean, 'sd': sd, 'se': (sd / math.sqrt(n)) if sd else None,
           'ex_top': ex_top, 'beats_replay': beats if replay_mean is not None else None}
    if n < TEST_N:
        out['state'], out['why'] = 'IN PROGRESS', '%d of %d trades' % (n, TEST_N)
        return out
    bar = PASS_PCT if n < EXTEND_N else EXTEND_PASS_PCT
    passed = mean >= bar and beats and (ex_top or 0) > 0
    if passed:
        out['state'], out['why'] = 'PASS', 'mean %+.1f%% >= %.0f%%, beats replay, holds without top %d' % (mean, bar, TOP_K)
    elif n < EXTEND_N and mean >= FAIL_PCT:
        out['state'], out['why'] = 'INCONCLUSIVE', 'between %.0f%% and the bar -- extend once to %d' % (FAIL_PCT, EXTEND_N)
    else:
        out['state'], out['why'] = 'FAIL', 'mean %+.1f%% (bar %.0f%%), beats replay: %s, without top %d: %s' % (
            mean, bar, beats, TOP_K, _f(ex_top, '%+.1f%%'))
    return out


def selection(tests: list, replayed: Optional[list] = None) -> dict:
    """The trades live took against the replay of EVERY signal entered over the
    same dates. `replayed` is injectable for tests; by default the replay runs
    over the candle cache."""
    if not tests:
        return {'since': None, 'until': None, 'live': None, 'replay': None, 'open': 0, 'n': 0}
    since = tests[0]['start'].strftime('%Y-%m-%d')
    until = max(r['start'] for r in tests).strftime('%Y-%m-%d')
    if replayed is None:
        replayed = replay.replay_window(since, until)
    closed = [t['net_pct'] for t in replayed if t.get('net_pct') is not None]
    return {'since': since, 'until': until, 'live': _mean([r['net_pct'] for r in tests]),
            'live_win': 100.0 * sum(r['net_pct'] > 0 for r in tests) / len(tests),
            'replay': _mean(closed), 'n': len(closed),
            'replay_win': (100.0 * sum(x > 0 for x in closed) / len(closed)) if closed else None,
            'open': len(replayed) - len(closed)}


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
                if o.get('q') == 'ok' and (o.get('long_bid') or 0) > 0 and o.get('ts'):
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
    if row['end'].date() <= cutoff:
        return {'net_pct': row['net_pct'], 'cut': False}
    day = cutoff.isoformat()
    m = (a.get('marks') or {}).get(day)
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
    """The test trades split by their approach into entry. Rows whose candles
    are missing are counted as unknown, never guessed into a group."""
    load = load or replay.load_daily
    groups = {'fast': [], 'rest': [], 'unknown': 0}
    cache = {}
    for r in tests:
        sh = state['shadows'].get(str(r['id'])) or {}
        stock, spot = sh.get('stock') or r.get('stock'), sh.get('entry_spot')
        if stock not in cache:
            cache[stock] = load(stock) if stock else None
        mv = (replay.approach_move(cache[stock], r['start'].strftime('%Y-%m-%d'),
                                   float(spot), sh.get('direction') or r.get('dir'))
              if cache[stock] and spot else None)
        if mv is None:
            groups['unknown'] += 1
        else:
            groups['fast' if mv > FAST_APPROACH_PCT else 'rest'].append(r['net_pct'])
    f, rest = groups['fast'], groups['rest']
    out = {'fast_n': len(f), 'rest_n': len(rest), 'unknown': groups['unknown'],
           'fast_mean': _mean(f), 'rest_mean': _mean(rest),
           'fast_win': (100.0 * sum(x > 0 for x in f) / len(f)) if f else None,
           'rest_win': (100.0 * sum(x > 0 for x in rest) / len(rest)) if rest else None}
    n = len(f) + len(rest)
    if n < TEST_N:
        out['state'] = 'IN PROGRESS'
    elif out['fast_mean'] is not None and out['rest_mean'] is not None \
            and out['fast_mean'] >= out['rest_mean']:
        out['state'] = 'CONSISTENT with the replay'
    else:
        out['state'] = 'CONTRADICTS the replay'
    return out


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
    tests = test_rows(rows['naked_long'])
    sel = selection(tests)
    vd = verdict([r['net_pct'] for r in tests], sel['replay'])
    cache_end = replay.cache_last_date([s for s, _ in replay.universe()])

    p('GO-LIVE DECISION PACK -- measures only, from the structure shadow')
    p('  FORWARD shadows only (live-priced from 2026-09-09). %d shadows.' % len(state['shadows']))
    p('\nTHE 100-TRADE TEST -- naked_long, fully watched, closed, net of fees')
    p('  (criteria fixed in advance: docs/NAKED_100_TRADE_TEST.md, summary in CLAUDE.md)')
    p('  progress %d / %d   mean net %s   SE %s   win %s   without top %d: %s' % (
        vd['n'], TEST_N, _f(vd['mean'], '%+.1f%%'), _f(vd['se'], '%.1f'),
        _f(sel.get('live_win'), '%.0f%%'), TOP_K, _f(vd['ex_top'], '%+.1f%%')))
    p('  vs replay of every signal, same dates: live %s vs replay %s (%d replayed)' % (
        _f(sel['live'], '%+.1f%%'), _f(sel['replay'], '%+.1f%%'), sel['n']))
    p('  VERDICT: %s -- %s' % (vd['state'], vd['why']))
    p('  PASS at %d: mean >= %.0f%% AND beats the replay AND positive without the top %d.'
      % (TEST_N, PASS_PCT, TOP_K))
    p('  FAIL below %.0f%%. In between: extend once to %d (bar %.0f%%). No early stop.'
      % (FAIL_PCT, EXTEND_N, EXTEND_PASS_PCT))
    p('  candle cache ends %s -- `python -m zebra golive --refresh` fetches from Kite first.'
      % (cache_end or 'MISSING'))
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
    p('\n7 SELECTION -- the trades live took vs the replay of EVERY signal, same dates')
    if not tests:
        p('  no fully watched, closed naked_long shadow yet.')
    else:
        p('  window %s .. %s (entry dates of the test trades)' % (sel['since'], sel['until']))
        p('  live    n=%-4d win %4s  mean net %s' % (vd['n'], _f(sel['live_win'], '%.0f%%'),
                                                   _f(sel['live'], '%+.1f%%')))
        p('  replay  n=%-4d win %4s  mean net %s  (%d still open in the replay)' % (
            sel['n'], _f(sel['replay_win'], '%.0f%%'), _f(sel['replay'], '%+.1f%%'), sel['open']))
        if sel['live'] is not None and sel['replay'] is not None:
            p("  difference %+.1f points -- positive means live's filters picked better trades"
              % (sel['live'] - sel['replay']))
        if cache_end and cache_end < sel['until']:
            p('  !! the candle cache ends %s, before the window does -- run with --refresh' % cache_end)
    p('  The replay has no edge on its own over 2020-2026 (+0.8% before costs, n=2,541);')
    p('  this gap is the thing under test.')

    p('\n8 VETTING -- allowed vs vetoed, each replayed as a naked trade from its trigger')
    # `decided`: vetoes never enter, so they never carry the cohort stamp.
    from .trade_store import decided
    vt = vetting(decided(store.load_trades()))
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

    p('\n9 TIME EXIT -- second hypothesis: exit at the close of session N unless the target')
    p('  was hit, on the SAME test trades, priced on the arm\'s real session-close marks')
    p('  %-9s %4s %8s %5s %11s %9s %9s %14s  %s' % ('N', 'n', 'unpriced', 'cut', 'cut then TP',
                                                  'as run', 'variant', 'gain +- SE', 'verdict'))
    pm = _paths_marks()
    for n in (TIME_EXIT_N,) + TIME_EXIT_SHOWN:
        te = time_exit(tests, state, n, pm)
        p('  %-9s %4d %8d %5d %11d %9s %9s %14s  %s' % (
            ('%d TESTED' % n) if n == TIME_EXIT_N else ('%d shown' % n), te['n'], te['unpriced'],
            te['cut'], te['cut_later_tp'], _f(te['base'], '%+.1f%%'), _f(te['variant'], '%+.1f%%'),
            ('%+.1f +- %.1f' % (te['gain'], te['se'])) if te['se'] else _f(te['gain'], '%+.1f'),
            te['state'] if n == TIME_EXIT_N else '(not tested)'))
    p('  SUPPORTED at %d trades if the gain is positive and >= 2 SE. "cut then TP" = trades the'
      % TEST_N)
    p('  time exit closed that went on to hit the target -- the late winners it costs.')
    p('  Replay 2020-2026 said +2.7 pts a trade at N=3 (SE 0.7); this is the real-price check.')

    p('\n10 APPROACH -- third hypothesis: FAST = the stock moved > %.1f%% toward its line in the'
      % FAST_APPROACH_PCT)
    p('  3 sessions before entry (`replay.approach_move`), on the same test trades')
    ap = approach(tests, state)
    p('  %-8s %4s %6s %9s' % ('group', 'n', 'win%', 'mean net'))
    p('  %-8s %4d %6s %9s' % ('fast', ap['fast_n'], _f(ap['fast_win'], '%.0f%%'), _f(ap['fast_mean'], '%+.1f%%')))
    p('  %-8s %4d %6s %9s' % ('rest', ap['rest_n'], _f(ap['rest_win'], '%.0f%%'), _f(ap['rest_mean'], '%+.1f%%')))
    if ap['unknown']:
        p('  %d trade(s) without candles for the approach -- run with --refresh' % ap['unknown'])
    p('  Replay 2020-2026: fast +5.1% vs rest -2.9% a trade (fast reached the line in 3 sessions')
    p('  40% of the time vs 22%). Live holds only ~1 in 5 fast trades, so it cannot prove this;')
    p('  at 100 it must NOT CONTRADICT it (fast >= rest).  Now: %s' % ap['state'])

    p('\n  Every number here is PAPER: fills at the touch, fees modelled. Impact beyond the')
    p('  touch is unmeasured until real orders go in -- read the stress column, not just net.')
    return '\n'.join(L)

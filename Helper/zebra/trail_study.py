"""Would the gain-anchored trail have armed, and would arming have paid?

`zebra_trail_never_arms` recorded the problem and then stopped, correctly: the
trail armed on 2 of 17 real winners, lowering the engage fraction caps upside,
and the power-law rule forbids capping upside. The note says the decision waits
on `mfe_*` data. That data now exists — 28 closed records carry a measured
`mfe_mid`, and 19 of them were ahead at some point.

What this does
--------------
For every closed BCS with a peak, replay the trail arithmetic that
`mfe.trail_levels` already implements — arm when the PEAK gain reaches
TRAIL_ENGAGE_FRAC of max gain, then exit at `debit + TRAIL_GIVEBACK_FRAC x peak
gain` — and compare what that would have booked with what the trade actually
booked. Sweep the engage fraction so the question is answered as a curve rather
than at the one point currently configured.

Three caveats, all load-bearing
-------------------------------
1. **Zebra is excluded.** A back ratio has no capped payoff, so "fraction of
   max gain" is undefined; `trail_levels` returns None and so does this.
2. **The trail level is not the fill, and it is the FINAL level.** Two
   separate optimisms, both upward. The monitor fires on `mid <= level` and
   books at `mid`, so a gap straight through books lower than the level. And
   the level ratchets up with the peak, so an exit that really happened partway
   through the run would have fired against a LOWER level than the one derived
   from the final peak. Modelling the exit at the final level is therefore an
   UPPER BOUND on what trailing earns — the same shape of bound as the debit-SL
   study's held-to-expiry figure. Never compare an upper bound with a realised
   number and call the difference profit.
3. **The peak is only as good as the polling.** `mfe_mid` is sampled once per
   monitor cycle, so a spike between polls is invisible. That biases peaks DOWN
   and therefore biases arming DOWN — the true arm rate is at least what this
   reports, never less.

Usage
-----
    python -m zebra.trail_study
    python -m zebra.trail_study --sweep 0.2,0.3,0.4,0.5
    python -m zebra.trail_study --json out.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HELPER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HELPER))

TRADES = HELPER / 'logs' / 'zebra_trades.json'


def outcome_if_trailed(trade: dict, engage_frac: float, giveback_frac: float):
    """What the trail would have booked, or None if it does not apply.

    Returns (armed, fired, pnl_if_trailed, pnl_actual), pnl in rupees.

    ARMED and FIRED are different questions, and reporting only the first
    overstates the trail's reach: at a 20% engage fraction 7 of 13 records arm
    and exactly 1 is ever touched. Arming is a property of the peak; firing
    needs the retracement as well.
    """
    width, debit = trade.get('width'), trade.get('debit')
    peak, qty = trade.get('mfe_mid'), trade.get('quantity')
    if None in (width, debit, peak, qty):
        return None
    width, debit, peak, qty = float(width), float(debit), float(peak), int(qty)
    max_gain = width - debit
    if max_gain <= 0:
        return None

    peak_gain = peak - debit
    armed = peak_gain >= max_gain * engage_frac
    actual = trade.get('pnl')
    if actual is None:
        return None
    actual = float(actual)
    if not armed:
        return False, False, actual, actual

    level = debit + giveback_frac * peak_gain
    trailed = (level - debit) * qty

    # DOES IT EVEN FIRE? The peak is by definition the maximum over the whole
    # life, so the final value is at or after it. The trail therefore fires if
    # and only if the trade ENDED at or below the level — that is the proof it
    # crossed. A trade that peaked and went straight out at TP never retraced,
    # so the trail never triggered and the real outcome stands.
    #
    # The first version of this had the comparison the other way round and
    # reported that trailing cost money at every engage fraction, "hurting"
    # exactly the two trades that ran to TP. It was clean, decisive, and
    # backwards.
    if actual <= trailed:
        return True, True, trailed, actual
    return True, False, actual, actual


def rows_for(book, engage, giveback):
    out = []
    for t in book:
        if t.get('status') != 'exited' or t.get('structure') != 'bcs':
            continue
        r = outcome_if_trailed(t, engage, giveback)
        if r is None:
            continue
        armed, fired, trailed, actual = r
        out.append({'id': t.get('id'), 'stock': t.get('stock'),
                    'reason': t.get('exit_reason'), 'armed': armed,
                    'fired': fired,
                    'actual': round(actual, 0), 'trailed': round(trailed, 0),
                    'delta': round(trailed - actual, 0),
                    'peak': t.get('mfe_mid'), 'debit': t.get('debit'),
                    'width': t.get('width')})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--sweep', default='0.2,0.3,0.4,0.5,0.6',
                    help='engage fractions to try (default 0.2..0.6)')
    ap.add_argument('--retain', type=float, default=None,
                    help='override TRAIL_RETAIN_FRAC')
    ap.add_argument('--json', dest='out', default=None)
    a = ap.parse_args(argv)

    from zebra import config as cfg
    giveback = a.retain if a.retain is not None else cfg.TRAIL_RETAIN_FRAC
    live_engage = cfg.TRAIL_ENGAGE_FRAC

    book = json.loads(TRADES.read_text(encoding='utf-8'))
    measured = rows_for(book, live_engage, giveback)
    if not measured:
        print('No closed BCS record carries a measured mfe_mid yet.')
        print('MFE capture is per-poll on OPEN positions, so the sample grows '
              'only as trades entered after it shipped close.')
        return 0

    print(f'closed BCS with a measured peak: {len(measured)}   '
          f'live config: engage {live_engage:.0%} of max gain, '
          f'give back to {giveback:.0%} of peak gain')
    print()
    print(f"{'id':>5} {'stock':<12} {'reason':<18} {'armed':<6} {'fired':<6} "
          f"{'actual':>10} {'trailed':>10} {'delta':>10}")
    print('-' * 85)
    for r in sorted(measured, key=lambda r: r['delta']):
        print(f"{r['id']:>5} {r['stock']:<12} {str(r['reason'])[:18]:<18} "
              f"{'YES' if r['armed'] else 'no':<6} "
              f"{'YES' if r['fired'] else 'no':<6} {r['actual']:>10,.0f} "
              f"{r['trailed']:>10,.0f} {r['delta']:>10,.0f}")

    print()
    # ARMED is not FIRED. A sweep reporting only the first reads as though
    # lowering the threshold engages the trail across half the book, when what
    # it does is arm trades the market never came back to test.
    print(f"{'engage':>8} {'armed':>7} {'fired':>7} {'helped':>7} {'hurt':>6} "
          f"{'net effect':>14}")
    print('-' * 58)
    sweep = []
    for frac in [float(x) for x in a.sweep.split(',')]:
        rs = rows_for(book, frac, giveback)
        armed = [r for r in rs if r['armed']]
        fired = [r for r in rs if r['fired']]
        helped = sum(1 for r in fired if r['delta'] > 0)
        hurt = sum(1 for r in fired if r['delta'] < 0)
        net = sum(r['delta'] for r in rs)
        mark = '  <- live' if abs(frac - live_engage) < 1e-9 else ''
        print(f"{frac:>8.0%} {len(armed):>7} {len(fired):>7} {helped:>7} "
              f"{hurt:>6} Rs {net:>+11,.0f}{mark}")
        sweep.append({'engage': frac, 'armed': len(armed),
                      'fired': len(fired), 'helped': helped,
                      'hurt': hurt, 'net': round(net, 0)})

    # WHERE DOES THE NUMBER COME FROM? A sweep total is a sum, and a sum over
    # 13 records can be one record. Printing the concentration is the
    # difference between "trailing earns Rs 7,302" and "one thin spread does".
    print()
    for frac in [float(x) for x in a.sweep.split(',')]:
        rs = [r for r in rows_for(book, frac, giveback) if r['delta'] != 0]
        if not rs:
            continue
        net = sum(r['delta'] for r in rs)
        top = max(rs, key=lambda r: abs(r['delta']))
        share = abs(top['delta']) / abs(net) * 100 if net else 100.0
        print(f"  at {frac:.0%}: {len(rs)} record(s) move the total; the "
              f"largest is #{top['id']} {top['stock']} at "
              f"Rs {top['delta']:+,.0f} = {share:.0f}% of it")
        if share >= 80.0:
            print(f"    CONCENTRATED IN ONE RECORD — that is an anecdote, "
                  f"not a result. debit {top['debit']}, width {top['width']}, "
                  f"peak {top['peak']}.")

    print()
    print('UPPER BOUND on what trailing earns, twice over: the exit is '
          'modelled AT the level while the monitor books at `mid`, and the '
          'level is derived from the FINAL peak while a real exit partway '
          'through would have fired against a lower one.')
    print('The peak is polled, not continuous, so arming is UNDER-counted.')
    print('A negative net is a reason not to lower the engage fraction. A '
          'positive one is NOT on its own a reason to lower it — cutting a '
          'run short is what the power-law rule forbids. Check the '
          'concentration line above before reading a total as a result.')

    if a.out:
        Path(a.out).write_text(
            json.dumps({'rows': measured, 'sweep': sweep}, indent=1),
            encoding='utf-8')
        print(f'\nwrote {a.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())

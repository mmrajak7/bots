#!/usr/bin/env python3
"""Analyse the entry-confirmation SHADOW experiment.

Reads the arms recorded by option_buy.py's _confirm_observe() and reports, for every
candidate (X, N) pair, what the gate WOULD have done had it been live — how many real
entries it would have skipped, what those trades actually made or lost, and how long it
would have delayed the entries it did allow.

Data sources
  logs/confirm_shadow.jsonl  archived arms from previous days (append-only)
  st_opt_state.json          today's live arms (not yet archived)
  st_opt_positions.json      the real trades, for outcome attribution

Usage
  python analyze_confirm_shadow.py                # everything collected so far
  python analyze_confirm_shadow.py 20260811       # only from this date onward

Design context: memory/project_entry_confirmation_design.md
"""
import json
import os
import sys
from collections import defaultdict

DIR = os.path.dirname(os.path.realpath(__file__))
ARCHIVE = os.path.join(DIR, 'logs', 'confirm_shadow.jsonl')
STATE = os.path.join(DIR, 'st_opt_state.json')
POSITIONS = os.path.join(DIR, 'st_opt_positions.json')

X_CANDIDATES = [0.3, 0.5, 1.0]
N_CANDIDATES = [10, 15, 20]


def load_arms(since=None):
    """Archived arms + today's live arms, de-duplicated by arm_key."""
    arms = {}
    if os.path.exists(ARCHIVE):
        with open(ARCHIVE, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                arms[r.get('arm_key', repr(r))] = r
    if os.path.exists(STATE):
        try:
            state = json.load(open(STATE, encoding='utf-8'))
            for k, r in (state.get('confirm_arms') or {}).items():
                arms[k] = dict(r, arm_key=k)
        except (json.JSONDecodeError, OSError):
            pass
    out = list(arms.values())
    if since:
        out = [a for a in out if a.get('day', '') >= since]
    return out


def load_positions():
    """Map (opt_type, tf, entry_date) -> trade outcome."""
    if not os.path.exists(POSITIONS):
        return {}
    try:
        pos = json.load(open(POSITIONS, encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return {}
    out = {}
    for v in pos.values():
        ep, xp, ls = v.get('entry_premium', 0), v.get('exit_premium', 0), v.get('lot_size', 0)
        if not ep:
            continue
        peak = v.get('peak_premium', ep)
        out[(v.get('opt_type'), v.get('signal_tf'), v.get('entry_date'))] = {
            'pl': (xp - ep) * ls,
            'exit': v.get('exit_type'),
            'peak_pct': (peak - ep) / ep * 100,
            'ep': ep,
        }
    return out


def confirmed(arm, x, n):
    """Would the gate have fired for this arm under (X, N)?"""
    f = (arm.get('fired') or {}).get(str(x))
    return bool(f) and f.get('min', 1e9) <= n


def main():
    since = sys.argv[1] if len(sys.argv) > 1 else None
    arms = load_arms(since)
    trades = load_positions()

    if not arms:
        print("No shadow arms recorded yet.")
        print(f"  looked in: {ARCHIVE}")
        print(f"             {STATE} (key 'confirm_arms')")
        print("\nThe gate arms only when a setup is inside the 5-7%% entry zone, so it")
        print("takes live in-zone signals to produce data. Check the log for 'CONFIRM ARM:'.")
        return

    days = sorted({a.get('day', '?') for a in arms})
    entered = [a for a in arms if a.get('entered_at')]
    print("=" * 84)
    print("ENTRY-CONFIRMATION SHADOW EXPERIMENT")
    print("=" * 84)
    print(f"  arms observed     : {len(arms)}")
    print(f"  of which entered  : {len(entered)}  (a real position was opened)")
    print(f"  trading days      : {len(days)}  ({days[0]} -> {days[-1]})")
    matched = [a for a in entered
               if (a.get('opt_type'), a.get('tf'), a.get('day')) in trades]
    print(f"  matched to trades : {len(matched)}")

    if len(matched) < 10:
        print("\n  ** SAMPLE TOO SMALL — treat everything below as directional only. **")
        print("     Target ~20 matched trades before drawing conclusions.")

    print("\n--- 1. WOULD-BE SKIPS: real entries the gate would NOT have taken ---")
    print(f"    {'X':>5} {'N':>4} {'skipped':>8} {'their P/L':>12} {'kept':>6} {'kept P/L':>11} {'verdict':>10}")
    best = None
    for x in X_CANDIDATES:
        for n in N_CANDIDATES:
            skip = [a for a in matched if not confirmed(a, x, n)]
            keep = [a for a in matched if confirmed(a, x, n)]
            spl = sum(trades[(a['opt_type'], a['tf'], a['day'])]['pl'] for a in skip)
            kpl = sum(trades[(a['opt_type'], a['tf'], a['day'])]['pl'] for a in keep)
            verdict = 'GOOD' if spl < 0 else ('BAD' if spl > 0 else '-')
            print(f"    {x:>4.1f}% {n:>4} {len(skip):>8} {spl:>12,.0f} "
                  f"{len(keep):>6} {kpl:>11,.0f} {verdict:>10}")
            if best is None or kpl > best[2]:
                best = (x, n, kpl)
    if best and matched:
        base = sum(trades[(a['opt_type'], a['tf'], a['day'])]['pl'] for a in matched)
        print(f"\n    baseline (no gate)      : Rs{base:,.0f} over {len(matched)} trades")
        print(f"    best candidate X={best[0]}%, N={best[1]}min : Rs{best[2]:,.0f}")
        print("    NOTE: 'best' is selected on this same data — expect shrinkage live.")

    print("\n--- 2. IS IT SKIPPING THE KNIVES? (peak < 1% = never ticked up) ---")
    for x in X_CANDIDATES:
        for n in N_CANDIDATES:
            skip = [a for a in matched if not confirmed(a, x, n)]
            if not skip:
                continue
            kn = sum(1 for a in skip
                     if trades[(a['opt_type'], a['tf'], a['day'])]['peak_pct'] < 1.0)
            wins = sum(1 for a in skip
                       if trades[(a['opt_type'], a['tf'], a['day'])]['pl'] > 0)
            print(f"    X={x}% N={n:>2}: of {len(skip):>2} skipped -> {kn} knives, "
                  f"{wins} were WINNERS {'<-- costly' if wins else ''}")

    print("\n--- 3. DELAY COST on the entries it would still have taken ---")
    for x in X_CANDIDATES:
        rows = [(a.get('fired') or {}).get(str(x)) for a in matched]
        rows = [r for r in rows if r]
        if not rows:
            print(f"    X={x}%: never confirmed on any matched arm")
            continue
        mins = sorted(r['min'] for r in rows)
        med = mins[len(mins) // 2]
        print(f"    X={x}%: confirmed on {len(rows)}/{len(matched)} — "
              f"delay median {med:.1f}min, max {mins[-1]:.1f}min")

    print("\n--- 4. ARMS THAT NEVER BECAME TRADES (gate had nothing to skip) ---")
    never = [a for a in arms if not a.get('entered_at')]
    print(f"    {len(never)} arms never produced an entry (blocked by exclusion, dedup,")
    print("    a gate, or the setup simply left the zone). Listed for completeness only —")
    print("    the gate cannot help or hurt on these.")

    print("\n--- 5. PER-DAY ARM COUNTS (sanity: is the experiment actually collecting?) ---")
    byday = defaultdict(int)
    for a in arms:
        byday[a.get('day', '?')] += 1
    for d in sorted(byday):
        print(f"    {d}: {byday[d]:>3} arms")

    print("\n" + "=" * 84)
    print("Reminder: shadow mode changes NO behaviour. To enable the gate, resolve X and N")
    print("from the table above, then implement the live branch (see design memo).")


if __name__ == '__main__':
    main()

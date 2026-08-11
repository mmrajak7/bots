"""CLI: python -m zebra [command]

Commands:
  scan        One-shot Chartink scan + watchlist add (no analyzer, no alert)
  run         One cycle: scan + check watching + check entered (cron target)
  loop        Long-running market-hours loop
  list        List trades (optionally filter by status)
  analyze     Run strike analyzer on a stock without scanning (for manual use)
  trigger     Manually trigger analyzer + ENTER alert on a watching signal
  enter       Mark a triggered signal as entered (after manual order placement)
  close       Mark an entered trade as exited
  cancel      Cancel a watching/triggered signal
  status      Dashboard summary
"""

import argparse
import logging
import sys


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def cmd_scan(args):
    from .scanner import validate_and_add
    from .monitor import _get_kite
    from .trade_store import get_store
    kite = _get_kite()
    store = get_store()
    added = validate_and_add(store, kite=kite, dry_run=args.dry_run)
    print(f"\nScanner: {len(added)} signal(s) {'previewed' if args.dry_run else 'added'}")


def cmd_run(args):
    from .monitor import run_once
    run_once(dry_run=args.dry_run)


def cmd_loop(args):
    from .monitor import run_loop
    run_loop(dry_run=args.dry_run)


def cmd_list(args):
    from .trade_store import get_store
    store = get_store()
    store.list_trades(status_filter=args.status)


def cmd_vet_show(args):
    """Dump the vetting context as JSON — the Claude CLI's only input.

    Deliberately reads the context CAPTURED AT TRIGGER rather than re-quoting:
    the vetter must judge the same book the bot saw, or its verdict describes a
    trade that no longer exists. Live re-quoting belongs in the checklist the
    agent runs, not in the handoff.
    """
    import json as _json
    from . import config as cfg
    from .trade_store import get_store
    from . import vet as vet_mod
    t = get_store().find(args.id)
    if not t:
        print(_json.dumps({'error': f'trade #{args.id} not found'}))
        return 1
    if getattr(args, 'exit', None):
        # Exit context: what the trigger saw, plus the entry reference points a
        # verdict needs (intrinsic floor, entry debit, SL level).
        m = ((t.get('exit_vet') or {}).get(args.exit) or {})
        print(_json.dumps({
            'trade_id': t['id'],
            'stock': t.get('stock'),
            'direction': t.get('direction'),
            'exit_kind': args.exit,
            'vet_state': m.get('state'),
            'defers_so_far': m.get('defers', 0),
            'max_defers': cfg.EXIT_MAX_DEFERS,
            'deadline': m.get('deadline'),
            'expired': vet_mod._exit_expired(m) if m else True,
            'entry_debit': t.get('debit'),
            'debit_sl_value': t.get('debit_sl_value'),
            'entry_spot': t.get('entry_spot'),
            'tp_spot': t.get('tp_spot'),
            'expiry': t.get('expiry'),
            'long_symbol': t.get('long_symbol'),
            'short_symbol': t.get('short_symbol'),
            'quantity': t.get('quantity'),
            'context': m.get('context', {}),
            'checklist': str(cfg.VETTING_DOC) + ' — EXIT section',
        }, indent=2, default=str))
        return 0
    v = t.get('vet') or {}
    print(_json.dumps({
        'trade_id': t['id'],
        'stock': t.get('stock'),
        'direction': t.get('direction'),
        'timeframe': t.get('timeframe'),
        'st_value': t.get('st_value'),
        'st_direction': t.get('st_direction'),
        'signal_gap_pct': t.get('signal_gap_pct'),
        'trend_aligned': cfg.is_trend_aligned(t.get('direction'),
                                              t.get('st_direction')),
        'vet_state': v.get('state'),
        'deadline': v.get('deadline'),
        'expired': vet_mod.is_expired(t),
        'context': v.get('context', {}),
        'checklist': 'Helper/CLAUDE.md — BCS pre-entry checklist (A-E)',
    }, indent=2, default=str))
    return 0


def cmd_vet_decide(args):
    """Record Claude's verdict: journal it, then land it on the signal.

    Order matters. The journal is written FIRST so a crash between the two
    leaves an auditable decision with no action taken — the safe direction.
    The reverse would act on a verdict with no record of why.
    """
    from . import config as cfg
    from .trade_store import get_store
    from .decisions import get_store as get_decisions
    from . import vet as vet_mod

    store = get_store()
    t = store.find(args.id)
    if not t:
        print(f"trade #{args.id} not found")
        return 1

    verdict = vet_mod.ALLOWED if args.verdict == 'allow' else vet_mod.VETOED
    # One decision, both A/B arms — keeps the structure comparison clean.
    trade_ids = [t['id']] + [s['id'] for s in store.load_trades()
                             if s.get('shadow_of') == t['id']]
    d = get_decisions().record(
        kind='entry',
        verdict='allow' if verdict == vet_mod.ALLOWED else 'veto',
        trade_ids=trade_ids,
        stock=t.get('stock'), direction=t.get('direction'),
        reasons=args.reason or [], red_flags=args.red_flag or [],
        confidence=args.confidence, model=cfg.VET_MODEL,
        notes=args.notes or '',
    )
    outcome = vet_mod.record_verdict(store, args.id, verdict,
                                     decision_id=d['id'])
    print(f"decision #{d['id']} recorded; verdict {outcome}")

    # A VETO is the end of the story for this signal — nothing else will be
    # sent, so silence would be indistinguishable from "nothing fired". An
    # ALLOW deliberately stays quiet here: it rides on the ENTER alert that is
    # already going out (one signal, one alert).
    if verdict == vet_mod.VETOED and outcome == 'applied':
        from .monitor import _send_telegram, format_vetoed_alert
        try:
            _send_telegram(format_vetoed_alert(t, args.reason or [],
                                               args.red_flag or []))
        except Exception as e:
            # The decision is already recorded and applied; a Telegram failure
            # must not make the agent think its verdict did not land.
            logging.getLogger(__name__).warning(
                "veto alert failed for #%d: %s", args.id, e)
    # A discarded verdict is NOT an error the agent should retry — the signal
    # already settled. Exit 0 so a retry loop does not hammer a closed case.
    return 0


def cmd_vet_exit_decide(args):
    """Record an exit verdict: allow the exit, or defer for a re-check.

    No hard veto exists here by design — see vet.record_exit_verdict. Deferring
    is the same protective power in a shape that cannot silently disarm a stop.
    """
    from . import config as cfg
    from .trade_store import get_store
    from .decisions import get_store as get_decisions
    from . import vet as vet_mod

    store = get_store()
    t = store.find(args.id)
    if not t:
        print(f"trade #{args.id} not found")
        return 1

    d = get_decisions().record(
        kind='exit',
        verdict='allow' if args.verdict == 'allow' else 'defer',
        trade_ids=[t['id']],
        stock=t.get('stock'), direction=t.get('direction'),
        reasons=args.reason or [], red_flags=args.red_flag or [],
        confidence=args.confidence, model=cfg.VET_MODEL,
        notes=args.notes or '', evidence={'exit_kind': args.kind},
    )
    outcome = vet_mod.record_exit_verdict(store, args.id, args.kind,
                                          args.verdict, decision_id=d['id'])
    print(f"decision #{d['id']} recorded; exit verdict {outcome}")
    return 0


def cmd_vet_score(args):
    """Is the vetting layer earning its keep?

    The report that decides whether this layer ever gets live authority. Two
    scores, never blended: realised P&L (which can only judge ALLOWS, because a
    vetoed structure was never priced) and the signal-quality label (which
    judges both arms on the same hit/miss basis). Blending a spot proxy into a
    rupee figure would be the easiest possible way to lie to ourselves.
    """
    from .decisions import get_store as get_decisions
    from .trade_store import get_store
    from . import outcomes as outcomes_mod

    store = get_store()
    decisions = get_decisions()
    if not getattr(args, 'no_join', False):
        joined = outcomes_mod.join(store, decisions)
        if joined:
            print(f"joined {joined} new outcome(s)\n")
    decisions.refresh()

    s = decisions.score('entry')
    q = s.get('signal_quality') or {}
    print("ENTRY VETTING")
    print("-" * 62)
    print(f"decisions scored on realised P&L : {s['scored']}")
    for verdict in ('veto', 'allow'):
        b = s.get(verdict) or {}
        if not b.get('n'):
            print(f"  {verdict:<6} n=0")
            continue
        money = b.get('pnl_avoided', b.get('pnl_captured'))
        print(f"  {verdict:<6} n={b['n']:<3} correct={b['correct']:<3} "
              f"precision={b['precision']:<6} Rs{money:,.0f}")
    print(f"\nsignal quality (hit/miss, comparable across both) : "
          f"{q.get('labelled', 0)} labelled")
    for verdict in ('veto', 'allow'):
        b = q.get(verdict) or {}
        prec = b.get('precision')
        print(f"  {verdict:<6} n={b.get('n', 0):<3} decisive={b.get('decisive', 0):<3} "
              f"flat={b.get('flat', 0):<3} correct={b.get('correct', 0):<3} "
              f"precision={prec if prec is not None else 'n/a'}")
    pending = [d for d in decisions.pending_outcome() if d.get('kind') == 'entry']
    if pending:
        print(f"\n{len(pending)} decision(s) still awaiting an outcome "
              f"(open positions / unresolved veto shadows)")

    if getattr(args, 'list', False):
        print("\nRECENT DECISIONS")
        print("-" * 62)
        for d in decisions.all()[-args.list:]:
            o = d.get('outcome') or {}
            print(f"#{d['id']:<4} {d['created_at'][:16]} {d['kind']:<6} "
                  f"{d['verdict']:<12} {(d.get('signal_ref') or {}).get('stock', ''):<12} "
                  f"{o.get('label') or ('pending' if not o else ''):<8} "
                  f"{('Rs%.0f' % o['pnl']) if o.get('pnl') is not None else ''}")
    return 0


def cmd_events_show(args):
    """Print the event calendar (optionally filtered to one symbol)."""
    import json as _json
    from . import events as events_mod
    data = events_mod.load()
    if args.symbol:
        rows = events_mod.upcoming(args.symbol, within_days=args.days, data=data)
    else:
        rows = events_mod.upcoming(None, within_days=args.days, data=data)
    print(_json.dumps({'refreshed_at': data.get('refreshed_at'),
                       'stale': events_mod.is_stale(data),
                       'events': rows}, indent=2, default=str))
    return 0


def cmd_events_replace(args):
    """Install a validated event calendar from a JSON file.

    The agent never writes the calendar directly: it hands a candidate here and
    this verb owns the schema, so a malformed refresh is rejected as a batch
    rather than silently emptying the file.
    """
    import json as _json
    from . import events as events_mod
    try:
        with open(args.file) as f:
            payload = _json.load(f)
    except Exception as e:
        print(f"cannot read {args.file}: {e}")
        return 1
    rows = payload.get('events') if isinstance(payload, dict) else payload
    try:
        doc = events_mod.replace(rows)
    except ValueError as e:
        print(f"rejected: {e}")
        return 1
    print(f"event calendar replaced: {len(doc['events'])} event(s)")
    return 0


def cmd_review_show(args):
    """Dump a position-review context as JSON — the review agent's input."""
    import json as _json
    from . import config as cfg
    from . import events as events_mod
    from .trade_store import get_store
    t = get_store().find(args.id)
    if not t:
        print(_json.dumps({'error': f'trade #{args.id} not found'}))
        return 1
    m = t.get('review') if isinstance(t.get('review'), dict) else {}
    print(_json.dumps({
        'trade_id': t['id'],
        'stock': t.get('stock'),
        'direction': t.get('direction'),
        'status': t.get('status'),
        'entry_spot': t.get('entry_spot'),
        'entry_date': t.get('entry_date'),
        'debit': t.get('debit'),
        'max_loss': t.get('capital'),
        'tp_spot': t.get('tp_spot'),
        'expiry': t.get('expiry'),
        'long_symbol': t.get('long_symbol'),
        'short_symbol': t.get('short_symbol'),
        'quantity': t.get('quantity'),
        'flagged_because': m.get('why'),
        'context': m.get('context', {}),
        'events': events_mod.upcoming(t.get('stock')),
        'checklist': str(cfg.VETTING_DOC) + ' — POSITION REVIEW section',
    }, indent=2, default=str))
    return 0


def cmd_review_record(args):
    """Record a review recommendation. Cannot close anything — by design."""
    from . import config as cfg
    from .trade_store import get_store
    from .decisions import get_store as get_decisions
    from . import review as review_mod

    store = get_store()
    t = store.find(args.id)
    if not t:
        print(f"trade #{args.id} not found")
        return 1
    d = get_decisions().record(
        kind='review', verdict=args.action, trade_ids=[t['id']],
        stock=t.get('stock'), direction=t.get('direction'),
        reasons=args.reason or [], red_flags=args.red_flag or [],
        confidence=args.confidence, model=cfg.VET_MODEL,
        notes=args.notes or '',
    )
    outcome = review_mod.record(store, args.id, args.action,
                               reasons=args.reason or [], decision_id=d['id'])
    print(f"decision #{d['id']} recorded; review {outcome}")
    return 0


def cmd_analyze(args):
    """Run the strike analyzer on a stock outside the scanner. Manual review."""
    from .scanner import _get_kite, get_ltp, compute_st_for_stock
    from . import strikes as strikes_mod
    from . import config as cfg

    kite = _get_kite()
    stock = args.symbol.upper()
    ltps = get_ltp(kite, [stock])
    spot = ltps.get(stock, 0)
    if spot <= 0:
        print(f"No LTP for {stock}")
        sys.exit(1)

    timeframe = args.timeframe
    direction = args.direction.upper() if args.direction else None
    if direction is None:
        st = compute_st_for_stock(kite, stock, timeframe)
        if not st:
            print(f"ST compute failed for {stock} {timeframe}")
            sys.exit(1)
        # Magnet routing — same as the scanner's _direction_for: direction is
        # set by which side of the ST line price sits on, and spot exactly on
        # the line is SKIP (no edge). Trend alignment is reported as conviction
        # (not a hard skip), matching how the bot actually trades.
        if spot < st['st']:
            direction = 'CE'
        elif spot > st['st']:
            direction = 'PE'
        else:
            print(f"  spot {spot:.2f} is exactly on ST {st['st']:.2f} — no "
                  f"direction. Specify --direction to override.")
            sys.exit(1)
        aligned = cfg.is_trend_aligned(direction, st['direction'])
        tag = 'ALIGNED (with-trend, premium)' if aligned \
            else 'COUNTER-TREND (lower conviction)'
        print(f"  spot {spot:.2f} vs ST {st['st']:.2f} ({st['direction']}) "
              f"-> {direction}  [{tag}]")

    print(f"\nAnalyzing {stock} ({direction}) at spot {spot:.2f}...")
    result = strikes_mod.analyze(kite, stock, direction, spot,
                                 max_candidates=args.max_candidates)
    if result.get('error'):
        print(f"  ERROR: {result['error']}")
        sys.exit(1)

    print(f"  expiry {result['expiry']} ({result['dte']} DTE), lot={result['lot_size']}")
    print(f"  K_S (ATM): {result['k_s_used']}")
    print(f"  evaluated {result['all_evaluated']} K_L candidates\n")

    best = result.get('best')

    def _print_row(c, label):
        warn = ' [' + ','.join(c['gate_fails']) + ']' if c['gate_fails'] else ''
        print(f"  {label}  K_L={c['k_l']}, K_S={c['k_s']}, width={c['width']:.0f}{warn}")
        print(f"        long  {c['long_symbol']}  mid={c['long_mid']:.2f} "
              f"(b={c['long_bid']:.2f}/a={c['long_ask']:.2f}, "
              f"sprd={c['long_spread_pct']:.1f}%) OI={c['long_oi']:,}")
        print(f"        short {c['short_symbol']}  mid={c['short_mid']:.2f} "
              f"(b={c['short_bid']:.2f}/a={c['short_ask']:.2f}, "
              f"sprd={c['short_spread_pct']:.1f}%) OI={c['short_oi']:,}")
        print(f"        debit={c['debit']:.2f}  BE={c['be']:.2f} ({c['be_pct_from_spot']:+.2f}%)  "
              f"NetExt={c['net_ext']:+.2f}  regime={c['regime']}")
        print(f"        cap/lot=Rs {c['capital_per_lot']:,.0f}\n")

    if best:
        print("  >>> BEST PICK <<<")
        _print_row(best, '       ')
        print(f"  Place this trade then run:")
        print(f"    python -m zebra enter ID --pair {int(best['k_l'])}/{int(best['k_s'])} "
              f"--debit X --lots 1 --expiry {result['expiry']}\n")

    best_key = (best['k_l'], best['k_s']) if best else (None, None)
    alternatives = [c for c in result['candidates']
                    if (c['k_l'], c['k_s']) != best_key]
    if alternatives:
        print("  Alternatives (top by theta-positivity):")
        for i, c in enumerate(alternatives, 1):
            _print_row(c, f'  [{i}]')


def cmd_trigger(args):
    """Force-run analyzer + alert on a specific watching signal."""
    from .monitor import _send_telegram, _format_enter_alert
    from .scanner import _get_kite, get_ltp
    from . import strikes as strikes_mod
    from .trade_store import get_store

    store = get_store()
    trade = store.find(args.id)
    if not trade:
        print(f"Trade #{args.id} not found")
        sys.exit(1)
    if trade['status'] not in ('watching', 'triggered'):
        print(f"#{args.id} status={trade['status']}, can't re-trigger")
        sys.exit(1)

    kite = _get_kite()
    spot = get_ltp(kite, [trade['stock']]).get(trade['stock'], 0)
    if spot <= 0:
        print(f"No LTP for {trade['stock']}")
        sys.exit(1)

    analysis = strikes_mod.analyze(kite, trade['stock'], trade['direction'],
                                   spot, max_candidates=args.max_candidates)
    if analysis.get('error'):
        print(f"Analyzer error: {analysis['error']}")
        sys.exit(1)

    gap = abs(spot - trade['st_value']) / trade['st_value'] * 100
    analysis['current_gap_pct'] = gap

    if trade['status'] == 'watching':
        store.mark_triggered(trade['id'], spot, gap, analysis['candidates'])

    msg = _format_enter_alert(trade, analysis)
    if args.dry_run:
        print(msg)
    else:
        ok = _send_telegram(msg, dry_run=False)
        print(f"Alert {'sent' if ok else 'FAILED'}")


def cmd_enter(args):
    from .trade_store import get_store
    store = get_store()
    try:
        k_l_str, k_s_str = args.pair.split('/')
        long_strike = float(k_l_str)
        short_strike = float(k_s_str)
    except Exception:
        print(f"--pair must be K_L/K_S e.g. 700/750, got {args.pair}")
        sys.exit(1)

    # Look up the actual symbols + lot_size from the triggered alert if present,
    # else from the options CSV directly.
    trade = store.find(args.id)
    if not trade:
        print(f"#{args.id} not found")
        sys.exit(1)

    long_symbol = args.long_symbol
    short_symbol = args.short_symbol
    lot_size = args.lot_size

    if (not long_symbol or not short_symbol or not lot_size) and \
       trade.get('alert_strikes'):
        for c in trade['alert_strikes']:
            if abs(c['k_l'] - long_strike) < 1e-6 and abs(c['k_s'] - short_strike) < 1e-6:
                long_symbol = long_symbol or c['long_symbol']
                short_symbol = short_symbol or c['short_symbol']
                lot_size = lot_size or c['lot_size']
                break

    if not (long_symbol and short_symbol and lot_size):
        print("Could not resolve symbols/lot_size. Pass --long-symbol, --short-symbol, --lot-size.")
        sys.exit(1)

    entry_data = {
        'long_strike': long_strike,
        'short_strike': short_strike,
        'long_symbol': long_symbol,
        'short_symbol': short_symbol,
        'debit': args.debit,
        'lot_size': int(lot_size),
        'lots': args.lots,
        'expiry': args.expiry,
    }
    if args.entry_spot is not None:
        entry_data['entry_spot'] = args.entry_spot
    if args.spot_sl_pct is not None:
        entry_data['spot_sl_pct'] = args.spot_sl_pct

    try:
        t = store.mark_entered(args.id, entry_data)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    from . import config as cfg
    print(f"Entered #{t['id']} {t['stock']} {int(t['long_strike'])}/{int(t['short_strike'])} "
          f"debit={t['debit']:.2f} qty={t['quantity']} cap=Rs{t['capital']:,.0f}")
    sl_txt = f"SPOT SL at {t['sl_spot']:.2f}, " if cfg.SPOT_SL_ENABLED else "SPOT SL off, "
    print(f"  TP at {t['tp_spot']:.2f}, {sl_txt}"
          f"DEBIT SL at {t['debit_sl_value']:.2f}")


def cmd_close(args):
    from .trade_store import get_store
    store = get_store()
    trade = store.find(args.id)
    if not trade:
        print(f"#{args.id} not found")
        sys.exit(1)
    if trade['status'] == 'entered':
        exit_debit = args.exit_debit
        exit_spot = args.exit_spot

        # If user didn't pass --exit-debit, try to fetch live structure mid
        # from Kite. Falls back to None (max loss) if Kite is unreachable.
        if exit_debit is None:
            try:
                from .scanner import _get_kite, get_ltp
                from .monitor import _quote_zebra_value
                kite = _get_kite()
                exit_debit = _quote_zebra_value(kite, trade)
                if exit_debit is not None:
                    print(f"  Fetched live structure mid: {exit_debit:.2f}")
                if exit_spot is None:
                    exit_spot = get_ltp(kite, [trade['stock']]).get(trade['stock'])
            except Exception as e:
                print(f"  (could not fetch live quote: {e})")

        if exit_spot is None:
            exit_spot = trade.get('entry_spot', 0)
        if exit_debit is None:
            print("  WARNING: no --exit-debit and Kite quote failed; "
                  "P&L will be booked as MAX LOSS. Re-run with --exit-debit "
                  "to record the actual fill.")

        try:
            t = store.mark_exited(args.id, exit_spot, exit_debit,
                                  args.reason or 'manual')
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        print(f"Closed #{t['id']} P&L=Rs{t['pnl']:,.0f} ({t['pnl_pct']:.1f}%)")
    elif trade['status'] in ('watching', 'triggered'):
        store.cancel(args.id, args.reason or 'manual cancel')
        print(f"Cancelled #{args.id}")
    else:
        print(f"#{args.id} status={trade['status']}, nothing to do")


def cmd_cancel(args):
    from .trade_store import get_store
    store = get_store()
    try:
        store.cancel(args.id, args.reason or 'manual cancel')
        print(f"Cancelled #{args.id}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def cmd_status(args):
    from .trade_store import get_store
    store = get_store()
    trades = store.load_trades()
    by_status = {}
    for t in trades:
        by_status.setdefault(t.get('status', '?'), []).append(t)

    print("\n=== ZEBRA DASHBOARD ===")
    for st in ('watching', 'triggered', 'entered', 'exited', 'cancelled'):
        lst = by_status.get(st, [])
        print(f"  {st:<10} {len(lst)}")

    entered = by_status.get('entered', [])
    if entered:
        from . import config as cfg
        print(f"\n  --- Open Trades ---")
        for t in entered:
            sl_txt = f"SL={t['sl_spot']:.2f} " if cfg.SPOT_SL_ENABLED else ""
            st_tag = ' [BCS]' if t.get('structure') == 'bcs' else ''
            print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<3} "
                  f"{int(t['long_strike'])}/{int(t['short_strike'])} "
                  f"debit={t['debit']:.2f} TP={t['tp_spot']:.2f} "
                  f"{sl_txt}exp={t['expiry']}{st_tag}")

    watching = by_status.get('watching', [])
    if watching:
        print(f"\n  --- Watching ---")
        for t in watching:
            last = t.get('last_gap_pct', t['signal_gap_pct'])
            print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<3} "
                  f"{t['timeframe']:<8} signal_gap={t['signal_gap_pct']:.2f}% "
                  f"last={last:.2f}%")

    exited = by_status.get('exited', [])
    if exited:
        print(f"\n  --- Performance ---")
        for label, group in (
                ('Zebra', [t for t in exited if t.get('structure') != 'bcs']),
                ('BCS  ', [t for t in exited if t.get('structure') == 'bcs'])):
            if not group:
                continue
            wins = sum(1 for t in group if t.get('pnl', 0) > 0)
            total_pnl = sum(t.get('pnl', 0) or 0 for t in group)
            win_rate = wins / len(group) * 100
            print(f"  {label}  P&L: Rs {total_pnl:,.0f}  "
                  f"WR {win_rate:.0f}% ({wins}W / {len(group)-wins}L)")
    print()


def cmd_reset(args):
    """Wipe in-flight signals (watching/triggered/entered) — for one-time cleanup
    before going to paper mode, or to reset after a regime change. Already-exited
    or cancelled trades are kept for history.
    """
    import json
    import shutil
    from datetime import datetime
    from pathlib import Path
    from .trade_store import get_store
    from . import config as cfg

    store = get_store()
    all_trades = store.load_trades()
    open_states = ('watching', 'triggered', 'entered')
    in_flight = [t for t in all_trades if t.get('status') in open_states]

    if not in_flight:
        print("No in-flight signals to reset (0 watching/triggered/entered).")
        return

    print(f"In-flight signals to be cancelled:")
    for t in in_flight:
        print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<3} "
              f"{t['status']:<10} {t['timeframe']:<8}")

    if not args.confirm:
        print(f"\n{len(in_flight)} signal(s) would be cancelled. "
              f"Re-run with --confirm to apply.")
        return

    # Archive the current file before mutating
    cfg.LOG_DIR.mkdir(exist_ok=True)
    archive_dir = cfg.LOG_DIR / 'archive' / datetime.now().strftime('%Y-%m-%d')
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_path = archive_dir / f'zebra_trades_pre_reset_{stamp}.json'
    if cfg.LOCAL_FILE.exists():
        shutil.copy(cfg.LOCAL_FILE, archive_path)
        print(f"\nArchived current store -> {archive_path}")

    # Cancel each in-flight via the store API (so version + Drive sync are correct)
    cancelled = 0
    for t in in_flight:
        try:
            if t['status'] == 'entered':
                # Force-close at debit (max loss assumption — paper trade, not real money)
                store.mark_exited(t['id'], t.get('entry_spot', 0), None,
                                  'reset_force_close')
            else:
                store.cancel(t['id'], 'reset')
            cancelled += 1
        except ValueError as e:
            print(f"  WARN: #{t['id']} skip — {e}")

    print(f"Reset complete: {cancelled} signal(s) closed/cancelled. "
          f"Local + Drive synced.")


def cmd_report(args):
    from .report import run as run_report
    run_report(
        report_type=args.report_type,
        send_telegram=args.telegram,
        no_kite=args.no_kite,
    )


def main():
    p = argparse.ArgumentParser(prog='python -m zebra',
                                description='Zebra — synthetic long/short option strategy')
    p.add_argument('-v', '--verbose', action='store_true', help='Debug logging')
    sub = p.add_subparsers(dest='command')

    p_scan = sub.add_parser('scan', help='Chartink scan + watchlist add')
    p_scan.add_argument('--dry-run', action='store_true')
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser('run', help='One full cycle (cron target)')
    p_run.add_argument('--dry-run', action='store_true')
    p_run.set_defaults(func=cmd_run)

    p_loop = sub.add_parser('loop', help='Long-running market-hours loop')
    p_loop.add_argument('--dry-run', action='store_true')
    p_loop.set_defaults(func=cmd_loop)

    p_list = sub.add_parser('list', help='List trades')
    p_list.add_argument('--status', choices=['watching', 'triggered',
                                              'entered', 'exited', 'cancelled'])
    p_list.set_defaults(func=cmd_list)

    # ── Claude vetting layer (called BY the spawned CLI, not by the user) ──
    p_vet = sub.add_parser('vet', help='Claude vetting layer')
    vet_sub = p_vet.add_subparsers(dest='vet_command')

    p_vshow = vet_sub.add_parser('show', help='Dump vetting context as JSON')
    p_vshow.add_argument('id', type=int)
    p_vshow.add_argument('--exit', choices=['tp', 'spot_sl', 'debit_sl'],
                         default=None, help='Show EXIT context for this trigger')
    p_vshow.set_defaults(func=cmd_vet_show)

    p_vexit = vet_sub.add_parser('exit-decide', help='Record an exit verdict')
    p_vexit.add_argument('id', type=int)
    p_vexit.add_argument('--kind', required=True,
                         choices=['tp', 'spot_sl', 'debit_sl'])
    p_vexit.add_argument('--verdict', required=True, choices=['allow', 'defer'])
    p_vexit.add_argument('--reason', action='append')
    p_vexit.add_argument('--red-flag', action='append')
    p_vexit.add_argument('--confidence', type=float, default=None)
    p_vexit.add_argument('--notes', default='')
    p_vexit.set_defaults(func=cmd_vet_exit_decide)

    p_vdec = vet_sub.add_parser('decide', help='Record a verdict')
    p_vdec.add_argument('id', type=int)
    p_vdec.add_argument('--verdict', required=True, choices=['allow', 'veto'])
    p_vdec.add_argument('--reason', action='append',
                        help='Repeatable: one checklist finding per flag')
    p_vdec.add_argument('--red-flag', action='append',
                        help='Repeatable: a concrete risk found')
    p_vdec.add_argument('--confidence', type=float, default=None)
    p_vdec.add_argument('--notes', default='')
    p_vdec.set_defaults(func=cmd_vet_decide)

    p_vscore = vet_sub.add_parser('score', help='Was the vetting layer right?')
    p_vscore.add_argument('--list', type=int, nargs='?', const=20, default=0,
                          metavar='N', help='Also print the last N decisions')
    p_vscore.add_argument('--no-join', action='store_true',
                          help='Report only; do not join new outcomes first')
    p_vscore.set_defaults(func=cmd_vet_score)

    # ── Event calendar (refreshed BY a Sonnet agent, read by the gates) ────
    p_ev = sub.add_parser('events', help='Event calendar')
    ev_sub = p_ev.add_subparsers(dest='events_command')

    p_evshow = ev_sub.add_parser('show', help='Print upcoming events')
    p_evshow.add_argument('--symbol', default=None)
    p_evshow.add_argument('--days', type=int, default=None)
    p_evshow.set_defaults(func=cmd_events_show)

    p_evrep = ev_sub.add_parser('replace', help='Install a validated calendar')
    p_evrep.add_argument('--file', required=True,
                         help='JSON: {"events":[...]} or a bare [...] list')
    p_evrep.set_defaults(func=cmd_events_replace)

    # ── Position review (called BY the spawned CLI) ────────────────────────
    p_rev = sub.add_parser('review', help='Periodic position review')
    rev_sub = p_rev.add_subparsers(dest='review_command')

    p_rvshow = rev_sub.add_parser('show', help='Dump review context as JSON')
    p_rvshow.add_argument('id', type=int)
    p_rvshow.set_defaults(func=cmd_review_show)

    p_rvrec = rev_sub.add_parser('record', help='Record a recommendation')
    p_rvrec.add_argument('id', type=int)
    p_rvrec.add_argument('--action', required=True,
                         choices=['hold', 'adjust', 'exit'])
    p_rvrec.add_argument('--reason', action='append')
    p_rvrec.add_argument('--red-flag', action='append')
    p_rvrec.add_argument('--confidence', type=float, default=None)
    p_rvrec.add_argument('--notes', default='')
    p_rvrec.set_defaults(func=cmd_review_record)

    p_anly = sub.add_parser('analyze', help='Strike analyzer for a stock (no save)')
    p_anly.add_argument('symbol')
    p_anly.add_argument('--timeframe', choices=['monthly', 'weekly'],
                        default='monthly')
    p_anly.add_argument('--direction', choices=['CE', 'PE'], default=None)
    p_anly.add_argument('--max-candidates', type=int, default=3)
    p_anly.set_defaults(func=cmd_analyze)

    p_trg = sub.add_parser('trigger', help='Force analyzer + alert on watching ID')
    p_trg.add_argument('id', type=int)
    p_trg.add_argument('--max-candidates', type=int, default=3)
    p_trg.add_argument('--dry-run', action='store_true')
    p_trg.set_defaults(func=cmd_trigger)

    p_ent = sub.add_parser('enter', help='Mark triggered signal as entered')
    p_ent.add_argument('id', type=int)
    p_ent.add_argument('--pair', required=True, help='K_L/K_S e.g. 700/750')
    p_ent.add_argument('--debit', type=float, required=True)
    p_ent.add_argument('--lots', type=int, required=True)
    p_ent.add_argument('--expiry', required=True, help='YYYY-MM-DD')
    p_ent.add_argument('--long-symbol', default=None)
    p_ent.add_argument('--short-symbol', default=None)
    p_ent.add_argument('--lot-size', type=int, default=None)
    p_ent.add_argument('--entry-spot', type=float, default=None)
    p_ent.add_argument('--spot-sl-pct', type=float, default=None,
                       help='Override default spot SL percentage (e.g. 0.03)')
    p_ent.set_defaults(func=cmd_enter)

    p_cls = sub.add_parser('close', help='Close entered trade (or cancel watching)')
    p_cls.add_argument('id', type=int)
    p_cls.add_argument('--exit-debit', type=float, default=None,
                       help='Closing net debit per share (struct value)')
    p_cls.add_argument('--exit-spot', type=float, default=None)
    p_cls.add_argument('--reason', default=None)
    p_cls.set_defaults(func=cmd_close)

    p_cnc = sub.add_parser('cancel', help='Cancel watching/triggered signal')
    p_cnc.add_argument('id', type=int)
    p_cnc.add_argument('--reason', default=None)
    p_cnc.set_defaults(func=cmd_cancel)

    p_sts = sub.add_parser('status', help='Dashboard')
    p_sts.set_defaults(func=cmd_status)

    p_rst = sub.add_parser('reset',
                           help='Cancel all in-flight signals (watching/triggered/entered)')
    p_rst.add_argument('--confirm', action='store_true',
                       help='Actually apply; without this, prints what would happen')
    p_rst.set_defaults(func=cmd_reset)

    p_rep = sub.add_parser('report',
                           help='EOD daily or Friday weekly performance report')
    p_rep.add_argument('--type', dest='report_type',
                       choices=['daily', 'weekly', 'auto'], default='auto',
                       help='auto = weekly on Fri, daily on Mon-Thu (default)')
    p_rep.add_argument('--telegram', action='store_true',
                       help='Also send to Telegram')
    p_rep.add_argument('--no-kite', action='store_true',
                       help='Skip live mid fetch for open positions (faster, no unrealized P&L)')
    p_rep.set_defaults(func=cmd_report)

    args = p.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        p.print_help()
        sys.exit(1)
    # A parent command used without its subcommand (e.g. `zebra vet`) parses
    # fine but carries no func — print that group's help instead of an
    # AttributeError traceback.
    if not hasattr(args, 'func'):
        p.parse_args([args.command, '--help'])
        sys.exit(1)
    sys.exit(args.func(args) or 0)


if __name__ == '__main__':
    main()

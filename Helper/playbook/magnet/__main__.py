"""CLI entry point: python -m playbook.magnet [command]"""

import argparse
import logging
import sys

from . import get_store


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )


def cmd_scan(args):
    """One-shot Chartink scan — validates signals, adds to watchlist."""
    from .monitor import run_scan_once
    run_scan_once(dry_run=args.dry_run)


def cmd_run(args):
    """Full run loop: scan every 5 min + monitor every 30s."""
    from .monitor import run
    run(dry_run=args.dry_run)


def cmd_list(args):
    """List all trades."""
    store = get_store()
    store.list_trades(status_filter=args.status)


def cmd_status(args):
    """Dashboard: open positions + P&L summary."""
    store = get_store()
    trades = store.load_trades()

    watching = [t for t in trades if t['status'] == 'watching']
    entered = [t for t in trades if t['status'] == 'entered']
    exited = [t for t in trades if t['status'] == 'exited']
    cancelled = [t for t in trades if t['status'] == 'cancelled']

    print("\n=== MAGNET DASHBOARD ===\n")
    print(f"  Watching:   {len(watching)}")
    print(f"  Entered:    {len(entered)}")
    print(f"  Exited:     {len(exited)}")
    print(f"  Cancelled:  {len(cancelled)}")

    if exited:
        total_pnl = sum(t.get('pnl', 0) or 0 for t in exited)
        wins = sum(1 for t in exited if (t.get('pnl', 0) or 0) > 0)
        losses = sum(1 for t in exited if (t.get('pnl', 0) or 0) <= 0)
        win_rate = (wins / len(exited) * 100) if exited else 0
        avg_days = sum(t.get('days_held', 0) for t in exited) / len(exited)

        print(f"\n  --- Performance ---")
        print(f"  Total P&L:  Rs {total_pnl:,.0f}")
        print(f"  Win rate:   {win_rate:.0f}% ({wins}W / {losses}L)")
        print(f"  Avg hold:   {avg_days:.1f} days")

        # By exit reason
        reasons = {}
        for t in exited:
            r = t.get('exit_reason', '?')
            if r not in reasons:
                reasons[r] = {'count': 0, 'pnl': 0}
            reasons[r]['count'] += 1
            reasons[r]['pnl'] += t.get('pnl', 0) or 0

        print(f"\n  --- By Exit Reason ---")
        for reason, stats in reasons.items():
            print(f"  {reason:<12} {stats['count']:>3} trades  Rs {stats['pnl']:>10,.0f}")

    if watching:
        print(f"\n  --- Watching ---")
        for t in watching:
            print(f"  #{t['id']} {t['stock']:<12} {t['timeframe']:<8} "
                  f"gap={t['signal_gap_pct']:.1f}% -> {t['direction']}")

    if entered:
        print(f"\n  --- Open Trades ---")
        for t in entered:
            flags = ""
            if t.get('hedged'):
                flags += f" [HEDGED: {t.get('hedge_symbol', '?')}]"
            if t.get('cost_sl_active'):
                flags += f" [COST SL: {t.get('cost_sl_level', 0):.2f}]"
            print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<4} "
                  f"entry={t.get('entry_spot', 0):.2f} "
                  f"target={t['target_spot']:.2f} "
                  f"sl={t.get('sl_spot', 0):.2f} "
                  f"option={t.get('option_symbol', '?')}{flags}")

    print()


def cmd_confidence(args):
    """Score magnet signal confidence (multi-TF analysis)."""
    from .confidence import cli_main
    # Forward remaining args to confidence CLI
    fwd = list(args.conf_symbols)
    if args.target:
        fwd.extend(['--target', args.target])
    if args.from_magnet:
        fwd.append('--from-magnet')
    if args.conf_json:
        fwd.append('--json')
    if args.conf_summary:
        fwd.append('--summary')
    cli_main(fwd)


def cmd_ctrack(args):
    """Confidence tracker sub-commands."""
    from .confidence_tracker import (
        get_tracker, monitor_once, monitor_loop,
        format_watch_alert, print_tracker_list,
    )

    action = args.ctrack_action

    if action == 'list':
        tracker = get_tracker()
        signals = tracker.list_all(
            status_filter=getattr(args, 'ctrack_status', None))
        print_tracker_list(signals)
        return

    if action == 'add':
        # Run confidence scorer on the symbol
        from .confidence import (
            _get_kite, _get_instrument_token, _fetch_daily,
            _fetch_hourly, _fetch_15min, _compute_all_st, score_signal,
        )
        try:
            kite = _get_kite()
        except Exception as e:
            print(f"Kite auth failed: {e}")
            return

        sym = args.ctrack_symbol
        try:
            tok = _get_instrument_token(kite, sym)
            print(f"  Fetching data for {sym}...")
            daily = _fetch_daily(kite, tok)
            hourly = _fetch_hourly(kite, tok)
            m15 = _fetch_15min(kite, tok)
            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data[f'NSE:{sym}']['last_price']
            st_data = _compute_all_st(daily)
            result = score_signal(sym, ltp, st_data, daily, hourly, m15,
                                  target_tf=args.ctrack_tf, kite=kite)
        except Exception as e:
            print(f"  Score error for {sym}: {e}")
            return

        if 'error' in result:
            print(f"  Signal error: {result['error']}")
            return

        # Determine direction from --dir flag or result
        direction = args.ctrack_dir or result['option_type']

        # Use --target-st override or from scorer
        target_st = args.ctrack_target_st or result['target_st']

        # need_up from direction
        need_up = (direction == 'CE')
        target_label = 'resistance' if need_up else 'support'

        # Spot price: from Kite or --spot arg
        signal_price = args.ctrack_spot if args.ctrack_spot else ltp

        tracker = get_tracker()
        sig = tracker.add(
            symbol=sym,
            timeframe=args.ctrack_tf or result['target_tf'],
            target_st=target_st,
            direction=direction,
            option_symbol=args.ctrack_option,
            option_price=args.ctrack_price,
            quantity=args.ctrack_qty,
            sl_spot=args.ctrack_sl,
            signal_price=signal_price,
            score=result['total_score'],
            grade=result['grade'],
            sq=result['sq_total'],
            et=result['et_total'],
            target_label=target_label,
        )

        # Print score detail
        from .confidence import print_detail
        print_detail(result)

        # Compute market correlation (3M: stock vs NIFTY + other watched stocks)
        corr_block = ''
        try:
            from .correlation import compute_correlations, format_correlation_block

            # Gather other symbols from tracker + magnet store
            other_syms = set()
            for s_sig in tracker.get_watching() + tracker.get_entered():
                other_syms.add(s_sig['symbol'])
            try:
                from . import get_store as _get_magnet_store
                for t in _get_magnet_store().load_trades():
                    if t.get('status') in ('watching', 'entered'):
                        other_syms.add(t['stock'])
            except Exception:
                pass  # magnet store unavailable, proceed with tracker only
            other_syms.discard(sym)

            corr_data = compute_correlations(
                symbol=sym,
                kite=kite,
                get_token_fn=_get_instrument_token,
                other_symbols=list(other_syms),
                daily_data=daily,
            )
            market_sc = result.get('sq_dims', {}).get('market', (4, 7, ''))[0]
            corr_block = format_correlation_block(corr_data, market_score=market_sc)

            # Print correlation to console
            if corr_data.get('nifty'):
                nc = corr_data['nifty']
                print(f"\n  Correlation (3M): vs NIFTY {nc['corr']:+.2f} ({nc['label']})")
                for cs, cd in sorted(corr_data.get('stocks', {}).items(),
                                     key=lambda x: abs(x[1]['corr']), reverse=True):
                    tag = ' <-- same trade!' if abs(cd['corr']) >= 0.75 else ''
                    print(f"    vs {cs}: {cd['corr']:+.2f} ({cd['label']}){tag}")
                from .correlation import _build_verdict
                verdict = _build_verdict(nc['corr'], market_sc)
                if verdict:
                    # Strip unicode for console safety
                    safe_verdict = verdict.encode('ascii', errors='replace').decode('ascii')
                    print(f"  Verdict: {safe_verdict}")
        except Exception as e:
            print(f"  Correlation computation skipped: {e}")

        # Send WATCHING Telegram alert
        opt_info = {
            'symbol': args.ctrack_option,
            'price': args.ctrack_price,
            'qty': args.ctrack_qty,
        }
        msg = format_watch_alert(result, option=opt_info, corr_block=corr_block)
        dry = getattr(args, 'dry_run', False)
        from .confidence_tracker import _send_telegram
        _send_telegram(msg, dry_run=dry)

        print(f"\n  Signal #{sig['id']} added: {sym} {direction} | "
              f"Score {sig['score']}/100 {sig['grade']}")
        print(f"  Target: {target_label} at {target_st:,.0f} | "
              f"Option: {args.ctrack_option} @ {args.ctrack_price:.2f}")
        return

    if action == 'check':
        from .confidence import _get_kite
        try:
            kite = _get_kite()
        except Exception as e:
            print(f"Kite auth failed: {e}")
            return
        tracker = get_tracker()
        dry = getattr(args, 'dry_run', False)
        monitor_once(kite, tracker, dry_run=dry)
        return

    if action == 'monitor':
        from .confidence import _get_kite
        try:
            kite = _get_kite()
        except Exception as e:
            print(f"Kite auth failed: {e}")
            return
        tracker = get_tracker()
        dry = getattr(args, 'dry_run', False)
        monitor_loop(kite, tracker, dry_run=dry)
        return

    if action == 'enter':
        tracker = get_tracker()
        sig_id = args.ctrack_id
        entry_price = args.ctrack_entry_price
        opt_price = args.ctrack_opt_price or 0.0
        tracker.mark_entered(sig_id, entry_price, opt_price)
        print(f"  Signal #{sig_id} marked as entered: "
              f"spot={entry_price:,.2f} option={opt_price:.2f}")
        return

    if action == 'close':
        tracker = get_tracker()
        sig_id = args.ctrack_id
        tracker.mark_exited(sig_id, args.ctrack_reason or 'manual close')
        print(f"  Signal #{sig_id} closed: {args.ctrack_reason or 'manual close'}")
        return

    if action == 'cancel':
        tracker = get_tracker()
        sig_id = args.ctrack_id
        tracker.cancel(sig_id, args.ctrack_reason or 'cancelled')
        print(f"  Signal #{sig_id} cancelled: {args.ctrack_reason or 'cancelled'}")
        return

    print(f"Unknown ctrack action: {action}")


def cmd_close(args):
    """Manually close a trade."""
    store = get_store()
    trade_id = args.id

    trade = None
    for t in store.load_trades():
        if t['id'] == trade_id:
            trade = t
            break

    if not trade:
        print(f"Trade #{trade_id} not found")
        return

    if trade['status'] == 'watching':
        store.cancel_signal(trade_id, args.reason or 'manual cancel')
        print(f"Signal #{trade_id} cancelled")
    elif trade['status'] == 'entered':
        # Get current spot for paper exit
        try:
            from .scanner import _get_kite, get_ltp
            from .monitor import get_sell_price, get_buy_price
            kite = _get_kite()
            ltps = get_ltp(kite, [trade['stock']])
            spot = ltps.get(trade['stock'], 0)

            # Long leg: sell at BID - slippage
            premium = get_sell_price(kite, trade['option_symbol']) if trade.get('option_symbol') else 0
            # Short leg (if hedged): buy back at ASK + slippage
            hedge_prem = None
            if trade.get('hedged') and trade.get('hedge_symbol'):
                hedge_prem = get_buy_price(kite, trade['hedge_symbol'])
        except Exception:
            spot = trade.get('entry_spot', 0)
            premium = 0
            hedge_prem = None

        store.exit_trade(trade_id, spot, premium,
                         args.reason or 'manual close', hedge_prem)
        # Re-read to get computed P&L
        for t in store.load_trades():
            if t['id'] == trade_id:
                trade = t
                break
        print(f"Trade #{trade_id} closed: P&L Rs {trade.get('pnl', 0):,.0f}")
    else:
        print(f"Trade #{trade_id} is already {trade['status']}")


def main():
    parser = argparse.ArgumentParser(
        prog='python -m playbook.magnet',
        description='Magnet — ST-magnet naked option strategy'
    )
    parser.add_argument('-v', '--verbose', action='store_true',
                        help='Debug logging')
    sub = parser.add_subparsers(dest='command')

    # scan
    p_scan = sub.add_parser('scan', help='One-shot Chartink scan')
    p_scan.add_argument('--dry-run', action='store_true',
                        help='Show signals without adding to store')
    p_scan.set_defaults(func=cmd_scan)

    # run
    p_run = sub.add_parser('run', help='Full monitor loop')
    p_run.add_argument('--dry-run', action='store_true',
                       help='Scan only, no entries')
    p_run.set_defaults(func=cmd_run)

    # list
    p_list = sub.add_parser('list', help='List trades')
    p_list.add_argument('--status', choices=['watching', 'entered', 'exited', 'cancelled'],
                        help='Filter by status')
    p_list.set_defaults(func=cmd_list)

    # status
    p_status = sub.add_parser('status', help='Dashboard')
    p_status.set_defaults(func=cmd_status)

    # confidence
    p_conf = sub.add_parser('confidence', help='Score signal confidence')
    p_conf.add_argument('conf_symbols', nargs='*', metavar='SYMBOL',
                        help='Stock symbols to score')
    p_conf.add_argument('--target', choices=['weekly', 'monthly'], default=None,
                        help='Target ST timeframe')
    p_conf.add_argument('--from-magnet', action='store_true',
                        help='Score watching signals from store')
    p_conf.add_argument('--conf-json', action='store_true',
                        help='JSON output')
    p_conf.add_argument('--conf-summary', action='store_true',
                        help='Summary table only')
    p_conf.set_defaults(func=cmd_confidence)

    # close
    p_close = sub.add_parser('close', help='Manual close/cancel')
    p_close.add_argument('id', type=int, help='Trade ID')
    p_close.add_argument('--reason', default=None, help='Exit reason')
    p_close.set_defaults(func=cmd_close)

    # ctrack -- confidence tracker
    p_ct = sub.add_parser('ctrack', help='Confidence tracker (entry/exit alerts)')
    ct_sub = p_ct.add_subparsers(dest='ctrack_action')

    # ctrack add
    ct_add = ct_sub.add_parser('add', help='Add signal to track')
    ct_add.add_argument('ctrack_symbol', metavar='SYMBOL', help='Stock symbol')
    ct_add.add_argument('--tf', dest='ctrack_tf',
                        choices=['daily', 'weekly', 'monthly'], default=None,
                        help='Target ST timeframe')
    ct_add.add_argument('--target-st', dest='ctrack_target_st',
                        type=float, default=None,
                        help='Target ST level (override auto-detect)')
    ct_add.add_argument('--dir', dest='ctrack_dir',
                        choices=['CE', 'PE'], default=None,
                        help='Option direction CE or PE')
    ct_add.add_argument('--option', dest='ctrack_option', required=True,
                        help='Option symbol e.g. NAUKRI26MAR1020CE')
    ct_add.add_argument('--price', dest='ctrack_price',
                        type=float, required=True,
                        help='Current option price')
    ct_add.add_argument('--qty', dest='ctrack_qty',
                        type=int, required=True,
                        help='Quantity (shares)')
    ct_add.add_argument('--sl', dest='ctrack_sl',
                        type=float, required=True,
                        help='Stop-loss spot level')
    ct_add.add_argument('--spot', dest='ctrack_spot',
                        type=float, default=None,
                        help='Spot price at signal (default: current LTP)')
    ct_add.add_argument('--dry-run', action='store_true',
                        help='Do not send Telegram alert')
    ct_add.set_defaults(func=cmd_ctrack)

    # ctrack list
    ct_list = ct_sub.add_parser('list', help='List tracked signals')
    ct_list.add_argument('--status',
                         dest='ctrack_status',
                         choices=['watching', 'ready', 'entered', 'exited', 'cancelled'],
                         default=None, help='Filter by status')
    ct_list.set_defaults(func=cmd_ctrack)

    # ctrack check
    ct_check = ct_sub.add_parser('check', help='One-shot check all signals')
    ct_check.add_argument('--dry-run', action='store_true',
                          help='No Telegram alerts')
    ct_check.set_defaults(func=cmd_ctrack)

    # ctrack monitor
    ct_mon = ct_sub.add_parser('monitor', help='Long-running monitor loop')
    ct_mon.add_argument('--dry-run', action='store_true',
                        help='No Telegram alerts')
    ct_mon.set_defaults(func=cmd_ctrack)

    # ctrack enter
    ct_enter = ct_sub.add_parser('enter', help='Mark signal as entered')
    ct_enter.add_argument('ctrack_id', metavar='ID', type=int, help='Signal ID')
    ct_enter.add_argument('--price', dest='ctrack_entry_price',
                          type=float, required=True,
                          help='Entry spot price')
    ct_enter.add_argument('--opt-price', dest='ctrack_opt_price',
                          type=float, default=None,
                          help='Entry option price')
    ct_enter.set_defaults(func=cmd_ctrack)

    # ctrack close
    ct_close = ct_sub.add_parser('close', help='Mark signal as exited')
    ct_close.add_argument('ctrack_id', metavar='ID', type=int, help='Signal ID')
    ct_close.add_argument('--reason', dest='ctrack_reason',
                          default=None, help='Exit reason')
    ct_close.set_defaults(func=cmd_ctrack)

    # ctrack cancel
    ct_cancel = ct_sub.add_parser('cancel', help='Cancel a watching signal')
    ct_cancel.add_argument('ctrack_id', metavar='ID', type=int, help='Signal ID')
    ct_cancel.add_argument('--reason', dest='ctrack_reason',
                           default=None, help='Cancel reason')
    ct_cancel.set_defaults(func=cmd_ctrack)

    p_ct.set_defaults(func=cmd_ctrack)

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        sys.exit(1)

    # For ctrack with no sub-action, show ctrack help
    if args.command == 'ctrack' and not args.ctrack_action:
        p_ct.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()

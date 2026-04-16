"""Main run loop: Chartink watchlist + 15M ST + LTP proximity + trade monitoring.

Architecture:
  - Every 5 min: Chartink scan (Thread 1 logic — watchlist refresh)
  - Every 15M boundary (+1 min): Fetch 15M candles, compute ST, check SL
  - Every 1 min: LTP check vs ST (entry alerts) + TP check (open trades)
  - At 14:45: Pre-close alert for open trades
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

from . import config as cfg
from .watchlist import scan_both, diff_watchlist
from .scanner import (
    _get_kite, reset_kite, compute_15m_st, get_ltp_batch, find_atm_option,
    get_option_quote, build_entry_alert, build_exit_alert,
    build_pre_close_alert, build_skip_alert,
)
from .trade_store import get_store

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

# ── Telegram ──────────────────────────────────────────────────────────────

_telegram_cfg = None
_telegram_cfg_loaded = False


def send_telegram(msg: str):
    """Send Telegram alert. Best-effort: never blocks or crashes."""
    global _telegram_cfg, _telegram_cfg_loaded
    import requests

    try:
        if not _telegram_cfg_loaded:
            if cfg.TELEGRAM_CONFIG.exists():
                with open(cfg.TELEGRAM_CONFIG) as f:
                    _telegram_cfg = json.load(f)
            _telegram_cfg_loaded = True

        if not _telegram_cfg:
            return

        requests.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        logger.debug("Telegram send failed: %s", e)


# ── Market Hours ──────────────────────────────────────────────────────────

def is_market_hours(now: datetime = None) -> bool:
    """Check if within scan window: 9:30-15:00, Mon-Fri."""
    if now is None:
        now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    start = cfg.MARKET_START[0] * 100 + cfg.MARKET_START[1]
    end = cfg.MARKET_END[0] * 100 + cfg.MARKET_END[1]
    return start <= t <= end


def is_15m_check_time(now: datetime) -> bool:
    """True if 1 minute past a 15M candle close (xx:01, xx:16, xx:31, xx:46)."""
    return now.minute % 15 == 1


# ── Core Logic ────────────────────────────────────────────────────────────

def update_all_15m_st(kite, stocks: Set[str],
                      st_cache: Dict[str, dict]) -> None:
    """Fetch 15M candles and update ST cache for all stocks.

    Rate-limited: ~3 req/sec for Kite historical API.
    """
    for symbol in sorted(stocks):
        try:
            st_info = compute_15m_st(kite, symbol)
            if st_info:
                st_cache[symbol] = st_info
                logger.debug("ST %s: %.2f (%s) close=%.2f",
                             symbol, st_info['st_value'],
                             st_info['direction'], st_info['candle_close'])
            time.sleep(0.35)  # rate limit: ~3/sec
        except Exception as e:
            logger.error("ST computation failed for %s: %s", symbol, e)


def check_proximity_alerts(kite, store, watchlist: Dict[str, Set[str]],
                           st_cache: Dict[str, dict],
                           alerted_today: Set[str],
                           ltp_map: Dict[str, float] = None,
                           dry_run: bool = False) -> Dict[str, float]:
    """Check LTP vs 15M ST for all watchlist stocks. Alert if within alert zone.

    Returns the ltp_map used (fetched if not provided), so caller can reuse it.
    """
    all_stocks = watchlist.get('UP', set()) | watchlist.get('DOWN', set())
    # Only check stocks that have ST data
    stocks_with_st = [s for s in all_stocks if s in st_cache]
    if not stocks_with_st:
        return ltp_map or {}

    if ltp_map is None:
        ltp_map = get_ltp_batch(kite, stocks_with_st)
    if not ltp_map:
        return {}

    for side in ('UP', 'DOWN'):
        direction = 'CE' if side == 'UP' else 'PE'
        for stock in watchlist.get(side, set()):
            if stock in alerted_today:
                continue
            if stock not in st_cache or stock not in ltp_map:
                continue

            st_info = st_cache[stock]
            ltp = ltp_map[stock]
            st_val = st_info['st_value']

            if st_val <= 0:
                continue

            gap = (ltp - st_val) / st_val
            abs_gap = abs(gap)

            # Check: LTP within alert zone of ST
            if abs_gap > cfg.ALERT_ZONE_PCT:
                continue

            # Direction consistency: for UP side, we want ST direction UP
            # and price pulling back TO the ST (support test)
            if side == 'UP' and st_info['direction'] != 'UP':
                continue
            if side == 'DOWN' and st_info['direction'] != 'DOWN':
                continue

            logger.info("PROXIMITY: %s (%s) LTP=%.2f ST=%.2f gap=%.2f%%",
                        stock, side, ltp, st_val, gap * 100)

            # Look up ATM option
            option = find_atm_option(stock, direction, ltp)
            if not option:
                logger.warning("No option found for %s %s, skipping", stock, direction)
                continue

            # Get option quote (bid/ask)
            quote = get_option_quote(kite, option['symbol'])
            if not quote:
                logger.warning("No quote for %s, skipping", option['symbol'])
                continue

            # Calculate lots and cost (always at least 1 lot — user decides capital)
            ask_price = quote['ask']
            lot_size = option['lot_size']
            cost_per_lot = ask_price * lot_size
            lots = max(1, int(cfg.CAPITAL_PER_TRADE / cost_per_lot)) if cost_per_lot > 0 else 1
            total_cost = lots * cost_per_lot

            # Only skip on wide spread — capital is user's decision
            if quote['spread_pct'] > cfg.MAX_SPREAD_PCT:
                logger.info("SKIP: %s — Spread %.1f%% > %.0f%% limit",
                            stock, quote['spread_pct'] * 100,
                            cfg.MAX_SPREAD_PCT * 100)
                alerted_today.add(stock)
                continue

            # Build and send alert
            msg = build_entry_alert(
                stock, side, abs_gap, ltp, st_val,
                option, quote, lots, total_cost,
            )

            if not dry_run:
                send_telegram(msg)

            # Record in store
            store.add_alert({
                'stock': stock,
                'side': side,
                'direction': direction,
                'spot': ltp,
                'st_value': st_val,
                'gap_pct': abs_gap,
                'option_symbol': option['symbol'],
                'option_strike': option['strike'],
                'option_expiry': option['expiry'],
                'option_ask': quote['ask'],
                'option_bid': quote['bid'],
                'spread_pct': quote['spread_pct'],
                'lot_size': lot_size,
                'lots': lots,
                'cost': total_cost,
            })

            alerted_today.add(stock)
            logger.info("ALERT SENT: %s (%s) gap=%.2f%% option=%s",
                        stock, side, abs_gap * 100, option['symbol'])

    return ltp_map


def check_sl_at_candle_close(kite, store, st_cache: Dict[str, dict],
                             dry_run: bool = False) -> None:
    """Check SL for entered trades: did 15M candle close below ST?

    Called at each 15M boundary. ST was just recomputed from latest candles.
    If direction flipped (UP->DOWN for longs, DOWN->UP for shorts), SL hit.
    """
    entered = store.get_entered()
    if not entered:
        return

    for trade in entered:
        stock = trade['stock']
        if stock not in st_cache:
            continue

        st_info = st_cache[stock]
        current_dir = st_info['direction']
        trade_side = trade['side']

        # SL: for UP trade, ST must remain UP. If flipped DOWN -> SL.
        # For DOWN trade, ST must remain DOWN. If flipped UP -> SL.
        sl_triggered = False
        if trade_side == 'UP' and current_dir == 'DOWN':
            sl_triggered = True
        elif trade_side == 'DOWN' and current_dir == 'UP':
            sl_triggered = True

        # Update monitoring data
        store.update_monitoring(trade['id'], st_info['st_value'])

        if sl_triggered:
            logger.info("SL HIT: %s (#%d) -- 15M ST flipped to %s",
                        stock, trade['id'], current_dir)

            # Fetch actual option bid for accurate P&L
            exit_premium = None
            opt_sym = trade.get('option_symbol')
            if opt_sym:
                quote = get_option_quote(kite, opt_sym)
                if quote and quote['bid'] > 0:
                    exit_premium = quote['bid']

            msg = build_exit_alert(
                trade, f"15M SL -- ST flipped {current_dir}",
                st_info['candle_close'],
                current_premium=exit_premium,
            )
            if not dry_run:
                send_telegram(msg)

            store.exit_trade(
                trade['id'],
                exit_spot=st_info['candle_close'],
                exit_premium=exit_premium or 0,
                reason=f"15M_SL_FLIP_{current_dir}",
            )


def check_tp(kite, store, ltp_map: Dict[str, float],
             dry_run: bool = False) -> None:
    """Check take profit (5% spot), DTE guard, and overnight gap."""
    entered = store.get_entered()
    if not entered:
        return

    now = datetime.now(IST)

    for trade in entered:
        stock = trade['stock']
        entry_spot = trade.get('entry_spot')
        if not entry_spot or stock not in ltp_map:
            continue

        ltp = ltp_map[stock]
        side = trade['side']

        # Update peak/extreme spot (UP=highest, DOWN=lowest)
        current_peak = trade.get('peak_spot')
        if side == 'UP':
            if current_peak is None or ltp > (current_peak or 0):
                store.update_monitoring(trade['id'],
                                        trade.get('current_st', 0),
                                        peak_spot=ltp)
        else:  # DOWN: track lowest spot (best for PE)
            if current_peak is None or ltp < current_peak:
                store.update_monitoring(trade['id'],
                                        trade.get('current_st', 0),
                                        peak_spot=ltp)

        # ── TP check: 5% move in trade direction ─────────────────
        if side == 'UP':
            target = entry_spot * (1 + cfg.TP_PCT)
            tp_hit = ltp >= target
        else:
            target = entry_spot * (1 - cfg.TP_PCT)
            tp_hit = ltp <= target

        if tp_hit:
            logger.info("TP HIT: %s (#%d) LTP=%.2f target=%.2f",
                        stock, trade['id'], ltp, target)

            exit_premium = None
            opt_sym = trade.get('option_symbol')
            if opt_sym:
                quote = get_option_quote(kite, opt_sym)
                if quote and quote['bid'] > 0:
                    exit_premium = quote['bid']

            msg = build_exit_alert(trade, "TP -- 5% target hit", ltp,
                                   current_premium=exit_premium)
            if not dry_run:
                send_telegram(msg)

            store.exit_trade(
                trade['id'],
                exit_spot=ltp,
                exit_premium=exit_premium or 0,
                reason="TP_5PCT",
            )
            continue

        # ── DTE guard: warn if option expiry < 5 days away ───────
        expiry_str = trade.get('option_expiry')
        if expiry_str:
            try:
                expiry_dt = datetime.strptime(expiry_str, '%Y-%m-%d')
                dte = (expiry_dt - now.replace(tzinfo=None)).days
                if dte <= 5 and not trade.get('_dte_warned'):
                    msg = (f"\u26a0\ufe0f DTE WARNING -- <code>{stock}</code> "
                           f"({side})\nOption {trade.get('option_symbol')} "
                           f"expires in {dte} days.\n"
                           f"Theta decay accelerating. Consider exiting.")
                    if not dry_run:
                        send_telegram(msg)
                    trade['_dte_warned'] = True
                    logger.info("DTE WARNING: %s #%d — %d days to expiry",
                                stock, trade['id'], dte)
            except ValueError:
                pass

        # ── Overnight gap check (first check of the day, 9:30-9:35) ──
        if (now.hour == 9 and 30 <= now.minute <= 35
                and not trade.get('_gap_checked_today')):
            gap_pct = (ltp - entry_spot) / entry_spot
            if side == 'DOWN':
                gap_pct = -gap_pct  # for DOWN, adverse gap = spot going UP

            if gap_pct < -0.02:  # 2% adverse gap
                msg = (f"\u26a0\ufe0f GAP ALERT -- <code>{stock}</code> "
                       f"({side})\n"
                       f"Entry: \u20b9{entry_spot:,.2f} | "
                       f"Open: \u20b9{ltp:,.2f} ({gap_pct*100:+.1f}%)\n"
                       f"Adverse gap > 2%. Review position.")
                if not dry_run:
                    send_telegram(msg)
                logger.warning("GAP ALERT: %s #%d gap=%.1f%%",
                               stock, trade['id'], gap_pct * 100)
            trade['_gap_checked_today'] = now.strftime('%Y-%m-%d')


def do_pre_close_alert(kite, store, dry_run: bool = False) -> None:
    """Send pre-close alert at 14:45 for all entered trades."""
    entered = store.get_entered()
    if not entered:
        return

    stocks = [t['stock'] for t in entered]
    ltp_map = get_ltp_batch(kite, stocks)

    msg = build_pre_close_alert(entered, ltp_map)
    if not dry_run:
        send_telegram(msg)
    logger.info("Pre-close alert sent for %d trades", len(entered))


# ── Main Run Loop ─────────────────────────────────────────────────────────

def _safe_api_call(fn, *args, **kwargs):
    """Wrap Kite API calls — reset kite on token expiry."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        err_str = str(e).lower()
        if 'token' in err_str or 'session' in err_str or '403' in err_str:
            logger.warning("Kite token may be expired, resetting: %s", e)
            reset_kite()
        raise


def run(dry_run: bool = False):
    """Main event loop: combines watchlist refresh, ST updates, and monitoring."""
    logger.info("Flow monitor starting (dry_run=%s)", dry_run)

    kite = _get_kite()
    store = get_store()

    watchlist: Dict[str, Set[str]] = {'UP': set(), 'DOWN': set()}
    st_cache: Dict[str, dict] = {}
    alerted_today: Set[str] = set()

    last_chartink_scan = 0.0
    last_ltp_check = 0.0
    last_15m_update = 0.0
    last_pre_close_ts = 0.0
    last_date = ''

    # Pre-populate alerted_today from store (crash recovery)
    alerted_today.update(store.get_today_stocks())

    while True:
        now = datetime.now(IST)
        ts = time.time()

        # Reset daily state at start of new day
        today_str = now.strftime('%Y-%m-%d')
        if last_date and last_date != today_str:
            alerted_today.clear()
            last_pre_close_ts = 0.0
            alerted_today.update(store.get_today_stocks())
            # Re-init kite in case token was refreshed overnight
            try:
                kite = _get_kite()
            except Exception as e:
                logger.error("Kite re-init failed: %s", e)
        last_date = today_str

        if not is_market_hours(now):
            if now.hour >= 15:
                logger.info("Market closed, exiting.")
                break
            logger.debug("Outside market hours, waiting...")
            time.sleep(30)
            continue

        # Refresh kite reference (may have been reset by _safe_api_call)
        try:
            kite = _get_kite()
        except Exception:
            time.sleep(30)
            continue

        # ── Thread 1: Chartink watchlist refresh (every 5 min) ────────
        if ts - last_chartink_scan >= cfg.CHARTINK_INTERVAL_SEC:
            new_watchlist = scan_both()

            if watchlist['UP'] or watchlist['DOWN']:
                changes = diff_watchlist(watchlist, new_watchlist)
                for side in ('UP', 'DOWN'):
                    added = changes[side]['added']
                    removed = changes[side]['removed']
                    if added:
                        logger.info("Watchlist %s added: %s", side,
                                    ', '.join(sorted(added)))
                    if removed:
                        logger.info("Watchlist %s removed: %s", side,
                                    ', '.join(sorted(removed)))

            watchlist = new_watchlist
            last_chartink_scan = ts

            total = len(watchlist['UP']) + len(watchlist['DOWN'])
            logger.info("Watchlist: UP=%d DOWN=%d total=%d",
                        len(watchlist['UP']), len(watchlist['DOWN']), total)

        # ── Thread 2a: 15M ST update (at 15M candle boundaries) ──────
        if is_15m_check_time(now) and (ts - last_15m_update >= 840):
            all_stocks = watchlist['UP'] | watchlist['DOWN']
            for t in store.get_entered():
                all_stocks.add(t['stock'])

            if all_stocks:
                logger.info("Updating 15M ST for %d stocks...", len(all_stocks))
                update_all_15m_st(kite, all_stocks, st_cache)
                check_sl_at_candle_close(kite, store, st_cache, dry_run)

            last_15m_update = ts

        # ── Thread 2b: LTP proximity + TP check (every 1 min) ────────
        if ts - last_ltp_check >= cfg.LTP_CHECK_INTERVAL_SEC:
            # Fetch LTP for ALL stocks in one batch (watchlist + open trades)
            all_ltp_stocks = set()
            for side in ('UP', 'DOWN'):
                for s in watchlist.get(side, set()):
                    if s in st_cache:
                        all_ltp_stocks.add(s)
            for t in store.get_entered():
                all_ltp_stocks.add(t['stock'])

            ltp_map = get_ltp_batch(kite, list(all_ltp_stocks)) if all_ltp_stocks else {}

            # Entry proximity alerts (reuse ltp_map)
            ltp_map = check_proximity_alerts(kite, store, watchlist, st_cache,
                                              alerted_today, ltp_map, dry_run)

            # TP + DTE + gap check for entered trades (reuse ltp_map)
            check_tp(kite, store, ltp_map, dry_run)

            last_ltp_check = ts

        # ── Pre-close alert at 14:45 (range check to avoid missing) ──
        if (now.hour == cfg.PRE_CLOSE_HOUR
                and cfg.PRE_CLOSE_MINUTE <= now.minute <= cfg.PRE_CLOSE_MINUTE + 2
                and ts - last_pre_close_ts > 3600):
            do_pre_close_alert(kite, store, dry_run)
            last_pre_close_ts = ts

        # ── Drive sync ───────────────────────────────────────────────
        store.maybe_sync()

        time.sleep(5)


def run_scan_once(dry_run: bool = False):
    """One-shot: scan Chartink + compute ST + check proximity. No monitoring loop."""
    kite = _get_kite()
    store = get_store()

    logger.info("One-shot scan starting...")

    # Chartink scan
    watchlist = scan_both()
    logger.info("Watchlist: UP=%d DOWN=%d",
                len(watchlist['UP']), len(watchlist['DOWN']))

    # Compute 15M ST for all stocks
    all_stocks = watchlist['UP'] | watchlist['DOWN']
    st_cache: Dict[str, dict] = {}
    if all_stocks:
        logger.info("Computing 15M ST for %d stocks...", len(all_stocks))
        update_all_15m_st(kite, all_stocks, st_cache)

    # Check proximity (ltp_map=None -> will fetch internally)
    alerted_today = store.get_today_stocks()
    check_proximity_alerts(kite, store, watchlist, st_cache,
                           alerted_today, None, dry_run)

    # Print summary
    print(f"\n=== FLOW SCAN RESULTS ===")
    print(f"  UP stocks:   {len(watchlist['UP'])}")
    print(f"  DOWN stocks: {len(watchlist['DOWN'])}")
    print(f"  ST computed: {len(st_cache)}")

    # Show stocks near ST (single batch LTP call)
    all_stocks_list = sorted(watchlist.get('UP', set()) | watchlist.get('DOWN', set()))
    stocks_with_st = [s for s in all_stocks_list if s in st_cache]
    ltp_map = get_ltp_batch(kite, stocks_with_st) if stocks_with_st else {}

    near_st = []
    for side in ('UP', 'DOWN'):
        for stock in sorted(watchlist.get(side, set())):
            if stock in st_cache and stock in ltp_map:
                st_info = st_cache[stock]
                ltp = ltp_map[stock]
                if ltp > 0 and st_info['st_value'] > 0:
                    gap = abs(ltp - st_info['st_value']) / st_info['st_value']
                    near_st.append((stock, side, ltp, st_info['st_value'],
                                    gap, st_info['direction']))

    near_st.sort(key=lambda x: x[4])  # sort by gap ascending

    print(f"\n{'Stock':<12} {'Side':<5} {'LTP':>10} {'15M ST':>10} "
          f"{'Gap%':>7} {'ST Dir':<6}")
    print("-" * 55)
    for stock, side, ltp, st_val, gap, st_dir in near_st[:20]:
        marker = " ***" if gap <= cfg.ALERT_ZONE_PCT else ""
        print(f"{stock:<12} {side:<5} {ltp:>10,.2f} {st_val:>10,.2f} "
              f"{gap*100:>6.2f}% {st_dir:<6}{marker}")
    print()

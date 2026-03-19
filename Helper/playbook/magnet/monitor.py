"""Magnet monitor — price watching, entry/exit lifecycle, Telegram alerts.

Responsibilities:
1. Poll LTP for 'watching' signals — enter when gap shrinks to ≤2%
2. Poll LTP for 'entered' trades — exit on TP (spot crosses ST) / SL / time
3. Send Telegram alerts on state changes
4. Orchestrate the full run loop (scan every 5 min + monitor every 30s)
"""

import json
import logging
import sys
import time
from datetime import datetime, timedelta

import pytz
import requests

from . import config as cfg
from .scanner import _get_kite, get_ltp, validate_and_add_signals
from .trade_store import get_store

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

# ── NFO Instrument Cache ──────────────────────────────────────────────────
_nfo_instruments = None


def _get_nfo_instruments(kite):
    """Load and cache NFO instrument list (once per session)."""
    global _nfo_instruments
    if _nfo_instruments is None:
        logger.info("Loading NFO instrument list (one-time)...")
        _nfo_instruments = kite.instruments('NFO')
        logger.info("Cached %d NFO instruments", len(_nfo_instruments))
    return _nfo_instruments


# ── Telegram ──────────────────────────────────────────────────────────────

_telegram_cfg = None
_telegram_cfg_loaded = False


def send_telegram(msg: str):
    """Send Telegram alert. Best-effort: never blocks or crashes."""
    global _telegram_cfg, _telegram_cfg_loaded

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
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg},
            timeout=10,
        )
    except Exception:
        pass  # best-effort


# ── Market Hours ──────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    """Check if current time is within market hours (9:15-15:30 IST, Mon-Fri)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday/Sunday
        return False
    market_open = now.replace(
        hour=cfg.MARKET_OPEN[0], minute=cfg.MARKET_OPEN[1], second=0, microsecond=0
    )
    market_close = now.replace(
        hour=cfg.MARKET_CLOSE[0], minute=cfg.MARKET_CLOSE[1], second=0, microsecond=0
    )
    return market_open <= now <= market_close


# ── Option Selection ──────────────────────────────────────────────────────

def select_option(kite, stock: str, direction: str, spot: float) -> dict:
    """Select ATM option for the trade.

    Returns: {strike, symbol, lot_size, premium} or empty dict on failure.

    Reads from nse_stocks_options.csv (CLAUDE.md rule: NEVER construct symbols).
    Falls back to Kite instruments if CSV not available.
    """
    import csv
    option_csv = cfg.PROJECT_ROOT / 'nse_stocks_options.csv'

    option_type = direction  # CE or PE

    if option_csv.exists():
        try:
            with open(option_csv, newline='') as f:
                reader = csv.DictReader(f)
                candidates = []
                for row in reader:
                    if (row.get('stock_symbol', '') == stock
                            and row.get('option_type', '') == option_type):
                        strike = float(row.get('option_strike', 0))
                        candidates.append({
                            'strike': strike,
                            'symbol': row.get('option_tradingsymbol', ''),
                            'lot_size': int(row.get('option_lot_size', 0)),
                            'expiry': row.get('option_expiry', ''),
                        })

                if candidates:
                    # ATM: nearest strike to spot
                    candidates.sort(key=lambda x: abs(x['strike'] - spot))
                    best = candidates[0]

                    # Get premium via LTP
                    try:
                        ltp_data = kite.ltp([f"NFO:{best['symbol']}"])
                        premium = list(ltp_data.values())[0]['last_price']
                    except Exception:
                        premium = 0

                    return {
                        'strike': best['strike'],
                        'symbol': best['symbol'],
                        'lot_size': best['lot_size'],
                        'premium': premium,
                        'expiry': best['expiry'],
                    }
        except Exception as e:
            logger.warning("CSV option lookup failed: %s", e)

    # Fallback: search Kite instruments (cached)
    try:
        instruments = _get_nfo_instruments(kite)
        now = datetime.now()
        candidates = []
        for inst in instruments:
            if (inst['name'] == stock
                    and inst['instrument_type'] == option_type
                    and inst['expiry'] >= now.date()):
                candidates.append(inst)

        if not candidates:
            return {}

        # Filter to nearest expiry with >=7 DTE
        min_dte = now.date() + timedelta(days=7)
        near_expiry = [c for c in candidates if c['expiry'] >= min_dte]
        if not near_expiry:
            near_expiry = candidates

        # Nearest expiry
        near_expiry.sort(key=lambda x: x['expiry'])
        target_expiry = near_expiry[0]['expiry']
        same_expiry = [c for c in near_expiry if c['expiry'] == target_expiry]

        # ATM strike
        same_expiry.sort(key=lambda x: abs(x['strike'] - spot))
        best = same_expiry[0]

        # Get premium
        try:
            ltp_data = kite.ltp([f"NFO:{best['tradingsymbol']}"])
            premium = list(ltp_data.values())[0]['last_price']
        except Exception:
            premium = 0

        return {
            'strike': best['strike'],
            'symbol': best['tradingsymbol'],
            'lot_size': best['lot_size'],
            'premium': premium,
            'expiry': str(best['expiry']),
        }

    except Exception as e:
        logger.error("Instrument search failed for %s: %s", stock, e)
        return {}


# ── Monitor: Watching Signals ─────────────────────────────────────────────

def check_watching_signals(store, kite):
    """Check watching signals: if gap shrinks to ≤2%, enter paper trade."""
    watching = store.get_watching()
    if not watching:
        return

    stocks = list({t['stock'] for t in watching})
    ltps = get_ltp(kite, stocks)

    for trade in watching:
        stock = trade['stock']
        price = ltps.get(stock)
        if not price:
            continue

        st_val = trade['st_value']
        gap = abs(price - st_val) / st_val

        # Too far — still approaching
        if gap > cfg.ENTRY_GAP:
            continue

        # Too close — already past entry (shouldn't happen if scanner was right)
        if gap < cfg.ENTRY_GAP_MIN:
            logger.info("Signal #%d %s: gap %.1f%% too narrow, cancelling",
                        trade['id'], stock, gap * 100)
            store.cancel_signal(trade['id'], f"gap narrowed to {gap:.1%}, past entry zone")
            send_telegram(
                f"MAGNET CANCEL: {stock}\n"
                f"Gap narrowed to {gap:.1%}, missed entry window"
            )
            continue

        # === ENTRY ZONE: gap is between 0.5% and 2% ===
        logger.info("ENTRY TRIGGER: %s gap=%.1f%% (≤2%%)", stock, gap * 100)

        # Select option
        option = select_option(kite, stock, trade['direction'], price)

        if not option or not option.get('symbol'):
            logger.warning("No option found for %s %s, skipping entry",
                           stock, trade['direction'])
            continue

        lot_size = option['lot_size']
        premium = option.get('premium', 0)

        # Position sizing: POSITION_SIZE / (premium * lot_size), minimum 1 lot
        if premium > 0 and lot_size > 0:
            qty = max(lot_size, int(cfg.POSITION_SIZE / (premium * lot_size)) * lot_size)
        else:
            qty = lot_size

        # Compute SL spot: price at 5% gap from ST on the same side
        side = trade['side']
        if side == 'above':
            # Price is above ST, we expect decline. SL = ST * 1.05 (price goes further up)
            sl_spot = round(st_val * (1 + cfg.SL_GAP), 2)
        else:
            # Price is below ST, we expect rally. SL = ST * 0.95 (price goes further down)
            sl_spot = round(st_val * (1 - cfg.SL_GAP), 2)

        # Enter paper trade
        store.enter_trade(trade['id'], {
            'entry_spot': price,
            'option_strike': option['strike'],
            'option_symbol': option['symbol'],
            'option_premium': premium,
            'lot_size': lot_size,
            'quantity': qty,
            'sl_spot': sl_spot,
        })

        msg = (
            f"MAGNET ENTRY (PAPER): {stock}\n"
            f"Direction: {trade['direction']} ({side} ST)\n"
            f"Spot: Rs {price:,.2f} | ST: Rs {st_val:,.2f} | Gap: {gap:.1%}\n"
            f"Option: {option['symbol']} @ Rs {premium:.2f}\n"
            f"Qty: {qty} (lot={lot_size})\n"
            f"Target: Rs {st_val:,.2f} (ST line)\n"
            f"SL Spot: Rs {sl_spot:,.2f} (5% gap)\n"
            f"Timeframe: {trade['timeframe']}"
        )
        send_telegram(msg)
        logger.info(msg.replace('\n', ' | '))


# ── Monitor: Open Trades ─────────────────────────────────────────────────

def check_open_trades(store, kite):
    """Check entered trades for TP/SL/time exit conditions."""
    entered = store.get_entered()
    if not entered:
        return

    stocks = list({t['stock'] for t in entered})
    ltps = get_ltp(kite, stocks)

    for trade in entered:
        stock = trade['stock']
        price = ltps.get(stock)
        if not price:
            continue

        st_val = trade['st_value']
        side = trade['side']
        sl_spot = trade.get('sl_spot', 0)

        # Get current option premium for P&L tracking
        option_symbol = trade.get('option_symbol')
        current_premium = 0
        if option_symbol:
            try:
                ltp_data = kite.ltp([f"NFO:{option_symbol}"])
                current_premium = list(ltp_data.values())[0]['last_price']
            except Exception:
                pass

        # === CHECK TP: spot crossed ST line ===
        tp_hit = False
        if side == 'above':
            # Price above ST, bought PUT, target = price drops to ST
            tp_hit = price <= st_val
        else:
            # Price below ST, bought CALL, target = price rises to ST
            tp_hit = price >= st_val

        if tp_hit:
            store.exit_trade(trade['id'], price, current_premium, 'tp')
            pnl = trade.get('pnl', 0)
            msg = (
                f"MAGNET TP HIT (PAPER): {stock}\n"
                f"Spot: Rs {price:,.2f} crossed ST: Rs {st_val:,.2f}\n"
                f"Premium: {trade.get('option_premium', 0):.2f} -> {current_premium:.2f}\n"
                f"P&L: Rs {pnl:,.0f} ({trade.get('pnl_pct', 0):.1f}%)\n"
                f"Days held: {trade.get('days_held', 0)}"
            )
            send_telegram(msg)
            logger.info("TP: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK SL: spot gap widened to 5% ===
        sl_hit = False
        if side == 'above' and sl_spot:
            sl_hit = price >= sl_spot  # price moved further UP (wrong direction)
        elif side == 'below' and sl_spot:
            sl_hit = price <= sl_spot  # price moved further DOWN (wrong direction)

        if sl_hit:
            store.exit_trade(trade['id'], price, current_premium, 'sl_spot')
            pnl = trade.get('pnl', 0)
            msg = (
                f"MAGNET SL HIT (PAPER): {stock}\n"
                f"Spot: Rs {price:,.2f} hit SL: Rs {sl_spot:,.2f}\n"
                f"Thesis failed — gap widened past 5%\n"
                f"Premium: {trade.get('option_premium', 0):.2f} -> {current_premium:.2f}\n"
                f"P&L: Rs {pnl:,.0f} ({trade.get('pnl_pct', 0):.1f}%)\n"
                f"Days held: {trade.get('days_held', 0)}"
            )
            send_telegram(msg)
            logger.info("SL: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK TIME SL: 5 trading days ===
        entry_date = trade.get('entry_date')
        if entry_date:
            entry_dt = datetime.strptime(entry_date, '%Y-%m-%d')
            now = datetime.now()
            # Count business days accurately (exclude Sat/Sun)
            biz_days = 0
            d = entry_dt + timedelta(days=1)
            while d <= now:
                if d.weekday() < 5:  # Mon-Fri
                    biz_days += 1
                d += timedelta(days=1)
            if biz_days >= cfg.SL_TIME_DAYS:
                store.exit_trade(trade['id'], price, current_premium, 'sl_time')
                pnl = trade.get('pnl', 0)
                msg = (
                    f"MAGNET TIME SL (PAPER): {stock}\n"
                    f"Held {biz_days} trading days without touching ST\n"
                    f"Spot: Rs {price:,.2f} | ST: Rs {st_val:,.2f}\n"
                    f"Premium: {trade.get('option_premium', 0):.2f} -> {current_premium:.2f}\n"
                    f"P&L: Rs {pnl:,.0f} ({trade.get('pnl_pct', 0):.1f}%)"
                )
                send_telegram(msg)
                logger.info("TIME SL: %s", msg.replace('\n', ' | '))
                continue


# ── Stale Signal Cleanup ──────────────────────────────────────────────────

def cleanup_stale_signals(store):
    """Cancel watching signals older than 3 days (never reached 2% entry)."""
    for trade in store.get_watching():
        sig_date = trade.get('signal_date', '')
        if not sig_date:
            continue
        sig_dt = datetime.strptime(sig_date, '%Y-%m-%d')
        days_old = (datetime.now() - sig_dt).days
        if days_old > 3:
            store.cancel_signal(trade['id'], f"stale after {days_old} days")
            logger.info("Cleaned stale signal #%d %s (%d days old)",
                        trade['id'], trade['stock'], days_old)


# ── Main Run Loop ────────────────────────────────────────────────────────

def _setup_file_logging():
    """Add file handler for cron mode logging."""
    log_file = cfg.LOG_DIR / f"magnet_{datetime.now().strftime('%Y%m%d')}.log"
    cfg.LOG_DIR.mkdir(exist_ok=True)
    fh = logging.FileHandler(str(log_file))
    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        '%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%H:%M:%S'
    ))
    logging.getLogger().addHandler(fh)
    return log_file


def run(dry_run: bool = False):
    """Main loop: scan every 5 min + monitor every 30s.

    Runs during market hours only. Exits cleanly outside hours.
    """
    log_file = _setup_file_logging()
    logger.info("Log file: %s", log_file)

    store = get_store()
    kite = _get_kite()

    # Startup summary
    watching = len(store.get_watching())
    entered = len(store.get_entered())
    logger.info("Magnet monitor started: %d watching, %d entered, dry_run=%s",
                watching, entered, dry_run)
    send_telegram(
        f"Magnet monitor started\n"
        f"Watching: {watching} | Entered: {entered}\n"
        f"Mode: {'DRY RUN' if dry_run else 'PAPER'}"
    )

    if not is_market_hours():
        logger.info("Market closed. Exiting.")
        print("Market is closed. Run during 9:15-15:30 IST, Mon-Fri.")
        return

    last_scan_time = 0
    cycle = 0
    kite_error_count = 0

    try:
        while is_market_hours():
            cycle += 1
            now = time.time()

            # Scan Chartink every SCAN_INTERVAL_SEC
            if now - last_scan_time >= cfg.SCAN_INTERVAL_SEC:
                logger.info("--- Scan cycle %d ---", cycle)
                try:
                    added = validate_and_add_signals(store, kite, dry_run=dry_run)
                    if added:
                        for sig in added:
                            if not sig.get('dry_run'):
                                send_telegram(
                                    f"MAGNET SIGNAL: {sig['stock']}\n"
                                    f"Timeframe: {sig['timeframe']}\n"
                                    f"Gap: {sig['signal_gap_pct']:.1f}%\n"
                                    f"ST: Rs {sig['st_value']:,.2f} ({sig['st_direction']})\n"
                                    f"Direction: {sig['direction']}\n"
                                    f"Watching for 2% entry..."
                                )
                    kite_error_count = 0  # reset on success
                except Exception as e:
                    logger.error("Scan error: %s", e)
                    kite_error_count += 1
                    # Reconnect Kite if repeated errors (token may have expired)
                    if kite_error_count >= 3:
                        logger.warning("3+ consecutive errors, reconnecting Kite...")
                        try:
                            kite = _get_kite()
                            kite_error_count = 0
                            logger.info("Kite reconnected")
                        except Exception as re:
                            logger.error("Kite reconnect failed: %s", re)
                            send_telegram(f"MAGNET ERROR: Kite reconnect failed: {re}")

                last_scan_time = now

                # Cleanup stale signals during scan cycle
                cleanup_stale_signals(store)

                # Periodic Drive sync
                store.maybe_sync()

            # Monitor prices every MONITOR_INTERVAL_SEC
            try:
                if not dry_run:
                    check_watching_signals(store, kite)
                    check_open_trades(store, kite)
            except Exception as e:
                logger.error("Monitor error: %s", e)

            time.sleep(cfg.MONITOR_INTERVAL_SEC)

    except KeyboardInterrupt:
        logger.info("Monitor stopped by user")

    # End of day summary
    watching = len(store.get_watching())
    entered = len(store.get_entered())
    exited_today = [t for t in store.load_trades()
                    if t.get('exit_date') == datetime.now().strftime('%Y-%m-%d')]
    total_pnl = sum(t.get('pnl', 0) or 0 for t in exited_today)

    summary = (
        f"Magnet EOD Summary\n"
        f"Watching: {watching} | Open: {entered}\n"
        f"Exited today: {len(exited_today)} | P&L: Rs {total_pnl:,.0f}"
    )
    logger.info(summary.replace('\n', ' | '))
    send_telegram(summary)


def run_scan_once(dry_run: bool = False):
    """Single scan cycle — for testing."""
    store = get_store()
    kite = _get_kite()
    added = validate_and_add_signals(store, kite, dry_run=dry_run)
    print(f"\nScan complete: {len(added)} signals added")
    for sig in added:
        if isinstance(sig, dict):
            stock = sig.get('stock', '?')
            gap = sig.get('signal_gap_pct', sig.get('gap', 0))
            if isinstance(gap, float) and gap < 1:
                gap *= 100
            print(f"  {stock}: gap={gap:.1f}%")
    return added

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

# ── Peak Premium Tracker (in-memory, resets on restart) ───────────────────
_peak_premiums = {}  # {trade_id: peak_premium_value}


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


# ── Bid-Ask Pricing ──────────────────────────────────────────────────────

def get_option_quote(kite, symbol: str) -> dict:
    """Get full bid/ask/LTP for an option. Returns {bid, ask, ltp, spread}."""
    try:
        data = kite.quote([f"NFO:{symbol}"])
        q = list(data.values())[0]
        depth = q.get('depth', {})
        buyers = depth.get('buy', [])
        sellers = depth.get('sell', [])
        bid = buyers[0]['price'] if buyers and buyers[0].get('price', 0) > 0 else 0
        ask = sellers[0]['price'] if sellers and sellers[0].get('price', 0) > 0 else 0
        ltp = q.get('last_price', 0)
        spread = round(ask - bid, 2) if bid > 0 and ask > 0 else 0
        return {'bid': bid, 'ask': ask, 'ltp': ltp, 'spread': spread}
    except Exception:
        return {'bid': 0, 'ask': 0, 'ltp': 0, 'spread': 0}


def get_buy_price(kite, symbol: str) -> float:
    """ASK + slippage — what we actually pay to buy."""
    q = get_option_quote(kite, symbol)
    ask = q['ask'] or q['ltp']
    if ask <= 0:
        return 0
    return round(ask * (1 + cfg.SLIPPAGE_PCT), 2)


def get_sell_price(kite, symbol: str) -> float:
    """BID - slippage — what we actually receive selling."""
    q = get_option_quote(kite, symbol)
    bid = q['bid'] or q['ltp']
    if bid <= 0:
        return 0
    return round(bid * (1 - cfg.SLIPPAGE_PCT), 2)


# ── Spot OHLC ────────────────────────────────────────────────────────────

def get_spot_ohlc(kite, symbols):
    """Get LTP + intraday HIGH/LOW for spot symbols.

    Returns {symbol: {ltp, high, low}}. Uses kite.ohlc() for day's range.
    """
    if not symbols:
        return {}
    instruments = [f"NSE:{s}" for s in symbols]
    try:
        data = kite.ohlc(instruments)
        result = {}
        for key, val in data.items():
            sym = key.replace('NSE:', '')
            ohlc = val.get('ohlc', {})
            result[sym] = {
                'ltp': val.get('last_price', 0),
                'high': ohlc.get('high', 0),
                'low': ohlc.get('low', 0),
            }
        return result
    except Exception as e:
        logger.error("OHLC fetch failed: %s", e)
        return {}


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
                    # Filter to nearest expiry with >= 7 DTE
                    today = datetime.now().strftime('%Y-%m-%d')
                    min_dte = (datetime.now() + timedelta(days=cfg.MIN_DTE)).strftime('%Y-%m-%d')
                    valid_expiry = [c for c in candidates if c['expiry'] >= min_dte]
                    if not valid_expiry:
                        valid_expiry = [c for c in candidates if c['expiry'] >= today]
                    if not valid_expiry:
                        valid_expiry = candidates

                    # Group by nearest expiry
                    valid_expiry.sort(key=lambda x: x['expiry'])
                    target_exp = valid_expiry[0]['expiry']
                    same_exp = [c for c in valid_expiry if c['expiry'] == target_exp]

                    # ATM: nearest strike to spot within same expiry
                    same_exp.sort(key=lambda x: abs(x['strike'] - spot))
                    best = same_exp[0]

                    # Get premium via ASK + slippage (what we actually pay)
                    premium = get_buy_price(kite, best['symbol'])

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
        min_dte = now.date() + timedelta(days=cfg.MIN_DTE)
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

        # Get premium via ASK + slippage (what we actually pay)
        premium = get_buy_price(kite, best['tradingsymbol'])

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

        # Fixed lots per trade
        qty = lot_size * cfg.LOTS_PER_TRADE

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
            'option_expiry': option.get('expiry', ''),
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
    """Check entered trades for TP/SL/time/trail exit conditions.

    TP logic: checks intraday HIGH/LOW (not just LTP) to catch wicks.
    Trail: once in profit, trails at 50% of peak premium gain.
    """
    entered = store.get_entered()
    if not entered:
        return

    # Use OHLC for spot — gives intraday high/low for wick detection
    stocks = list({t['stock'] for t in entered})
    spot_data = get_spot_ohlc(kite, stocks)

    for trade in entered:
        stock = trade['stock']
        sd = spot_data.get(stock)
        if not sd:
            continue

        price = sd['ltp']
        day_high = sd['high']
        day_low = sd['low']
        st_val = trade['st_value']
        side = trade['side']
        sl_spot = trade.get('sl_spot', 0)
        trade_id = trade['id']

        # Get current premiums (with slippage) for P&L tracking
        option_symbol = trade.get('option_symbol')
        long_exit = get_sell_price(kite, option_symbol) if option_symbol else 0
        adj_exit = None
        if trade.get('adjusted') and trade.get('adj_symbol'):
            adj_exit = get_buy_price(kite, trade['adj_symbol'])

        # Gap from ST (for adjustment trigger)
        gap = abs(price - st_val) / st_val

        # === TRACK PEAK PREMIUM (for trailing SL) ===
        entry_prem = trade.get('option_premium', 0) or 0
        if long_exit > 0 and entry_prem > 0:
            old_peak = _peak_premiums.get(trade_id, entry_prem)
            new_peak = max(old_peak, long_exit)
            _peak_premiums[trade_id] = new_peak

        # === CHECK ADJUSTMENT: gap reached 3.5%, not yet adjusted ===
        if (not trade.get('adjusted') and gap >= cfg.ADJ_GAP
                and gap < cfg.SL_GAP):
            adj_result = _try_adjustment(kite, trade, price, gap)
            if adj_result:
                store.adjust_trade(trade['id'], adj_result)
                msg = (
                    f"MAGNET ADJUST (PAPER): {stock}\n"
                    f"Gap widened to {gap:.1%} — adding short leg\n"
                    f"Sell {adj_result['adj_symbol']} @ Rs {adj_result['adj_premium']:.2f}\n"
                    f"Spread: {trade['option_strike']} / {adj_result['adj_strike']}\n"
                    f"Max loss: Rs {trade.get('adj_max_loss', entry_prem):.2f}/sh "
                    f"(was Rs {entry_prem:.2f})\n"
                    f"Saved: {((entry_prem - trade.get('adj_max_loss', entry_prem)) / entry_prem):.0%} of premium"
                )
                send_telegram(msg)
                logger.info("ADJUST: %s", msg.replace('\n', ' | '))
                adj_exit = get_buy_price(kite, adj_result['adj_symbol'])

        # === CHECK TP: spot crossed ST line (using intraday HIGH/LOW) ===
        tp_hit = False
        if side == 'above':
            # Bought PUT, expect decline. Touch = low ≤ ST
            tp_hit = day_low <= st_val
        else:
            # Bought CALL, expect rally. Touch = high ≥ ST
            tp_hit = day_high >= st_val

        if tp_hit:
            store.exit_trade(trade_id, price, long_exit, 'tp', adj_exit)
            _peak_premiums.pop(trade_id, None)
            pnl = trade.get('pnl', 0)
            adj_str = f" [SPREAD: bought back short @{adj_exit:.2f}]" if adj_exit else ""
            wick_note = ""
            if side == 'above' and price > st_val:
                wick_note = f"\nWick detected: day low {day_low:.2f} touched ST, current {price:.2f}"
            elif side == 'below' and price < st_val:
                wick_note = f"\nWick detected: day high {day_high:.2f} touched ST, current {price:.2f}"
            msg = (
                f"MAGNET TP HIT (PAPER): {stock}\n"
                f"Spot: Rs {price:,.2f} | ST: Rs {st_val:,.2f}\n"
                f"Long exit: {entry_prem:.2f} -> {long_exit:.2f}{adj_str}\n"
                f"P&L: Rs {pnl:,.0f} ({trade.get('pnl_pct', 0):.1f}%)\n"
                f"Days held: {trade.get('days_held', 0)}{wick_note}"
            )
            send_telegram(msg)
            logger.info("TP: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK TRAILING PREMIUM SL: 50% of peak gain ===
        peak = _peak_premiums.get(trade_id, entry_prem)
        if peak > entry_prem:  # only trail when in profit
            gain = peak - entry_prem
            trail_level = entry_prem + gain * cfg.TRAIL_PCT
            if long_exit <= trail_level and long_exit > 0:
                store.exit_trade(trade_id, price, long_exit, 'tp_trail', adj_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                msg = (
                    f"MAGNET TRAIL TP (PAPER): {stock}\n"
                    f"Premium dropped below 50% trail\n"
                    f"Peak: Rs {peak:.2f} | Trail: Rs {trail_level:.2f} | "
                    f"Current: Rs {long_exit:.2f}\n"
                    f"Entry: Rs {entry_prem:.2f} -> Exit: Rs {long_exit:.2f}\n"
                    f"P&L: Rs {pnl:,.0f} ({trade.get('pnl_pct', 0):.1f}%)\n"
                    f"Days held: {trade.get('days_held', 0)}"
                )
                send_telegram(msg)
                logger.info("TRAIL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK SL: spot gap widened to 5% ===
        sl_hit = False
        if side == 'above' and sl_spot:
            sl_hit = price >= sl_spot
        elif side == 'below' and sl_spot:
            sl_hit = price <= sl_spot

        if sl_hit:
            store.exit_trade(trade_id, price, long_exit, 'sl_spot', adj_exit)
            _peak_premiums.pop(trade_id, None)
            pnl = trade.get('pnl', 0)
            adj_str = " [SPREAD]" if trade.get('adjusted') else ""
            msg = (
                f"MAGNET SL HIT (PAPER): {stock}{adj_str}\n"
                f"Spot: Rs {price:,.2f} hit SL: Rs {sl_spot:,.2f}\n"
                f"Long exit: {long_exit:.2f}" +
                (f" | Short buyback: {adj_exit:.2f}" if adj_exit else "") + "\n"
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
            biz_days = 0
            d = entry_dt + timedelta(days=1)
            while d <= now:
                if d.weekday() < 5:
                    biz_days += 1
                d += timedelta(days=1)
            if biz_days >= cfg.SL_TIME_DAYS:
                store.exit_trade(trade_id, price, long_exit, 'sl_time', adj_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                msg = (
                    f"MAGNET TIME SL (PAPER): {stock}\n"
                    f"Held {biz_days} trading days without touching ST\n"
                    f"Spot: Rs {price:,.2f} | ST: Rs {st_val:,.2f}\n"
                    f"Long exit: {long_exit:.2f}" +
                    (f" | Short buyback: {adj_exit:.2f}" if adj_exit else "") + "\n"
                    f"P&L: Rs {pnl:,.0f} ({trade.get('pnl_pct', 0):.1f}%)"
                )
                send_telegram(msg)
                logger.info("TIME SL: %s", msg.replace('\n', ' | '))
                continue


# ── Adjustment: Sell OTM to Create Spread ─────────────────────────────────

def _try_adjustment(kite, trade: dict, current_spot: float,
                    current_gap: float) -> dict:
    """Find nearest OTM option to sell (1 strike away from long leg).

    For PE (bought PUT at strike X): sell PUT at strike X - interval
    For CE (bought CALL at strike X): sell CALL at strike X + interval

    Returns adj_data dict or None if no suitable option found.
    """
    import csv

    stock = trade['stock']
    direction = trade['direction']  # CE or PE
    long_strike = trade['option_strike']
    long_expiry = trade.get('option_expiry', '')  # MUST match
    option_csv = cfg.PROJECT_ROOT / 'nse_stocks_options.csv'

    candidates = []

    # Try CSV first — filter to SAME EXPIRY as long leg
    if option_csv.exists():
        try:
            with open(option_csv, newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get('stock_symbol', '') == stock
                            and row.get('option_type', '') == direction):
                        expiry = row.get('option_expiry', '')
                        # Must match long leg expiry
                        if long_expiry and expiry != long_expiry:
                            continue
                        strike = float(row.get('option_strike', 0))
                        candidates.append({
                            'strike': strike,
                            'symbol': row.get('option_tradingsymbol', ''),
                            'lot_size': int(row.get('option_lot_size', 0)),
                        })
        except Exception:
            pass

    # Fallback: NFO instruments — filter to SAME EXPIRY
    if not candidates:
        try:
            instruments = _get_nfo_instruments(kite)
            for inst in instruments:
                if (inst['name'] == stock
                        and inst['instrument_type'] == direction):
                    # Match expiry
                    inst_expiry = str(inst.get('expiry', ''))
                    if long_expiry and inst_expiry != long_expiry:
                        continue
                    candidates.append({
                        'strike': inst['strike'],
                        'symbol': inst['tradingsymbol'],
                        'lot_size': inst['lot_size'],
                    })
        except Exception:
            pass

    if not candidates:
        logger.warning("No OTM strikes found for %s %s adjustment", stock, direction)
        return None

    # Find 1 strike OTM from our long strike
    if direction == 'PE':
        # Long PUT at X, sell PUT at X - interval (lower strike = more OTM)
        otm = [c for c in candidates if c['strike'] < long_strike]
        if not otm:
            return None
        otm.sort(key=lambda x: x['strike'], reverse=True)  # nearest lower
        best = otm[0]
        spread_width = long_strike - best['strike']
    else:
        # Long CALL at X, sell CALL at X + interval (higher strike = more OTM)
        otm = [c for c in candidates if c['strike'] > long_strike]
        if not otm:
            return None
        otm.sort(key=lambda x: x['strike'])  # nearest higher
        best = otm[0]
        spread_width = best['strike'] - long_strike

    if spread_width <= 0:
        return None

    # Get BID - slippage for selling
    credit = get_sell_price(kite, best['symbol'])
    if credit <= 0:
        logger.warning("No bid for %s, skipping adjustment", best['symbol'])
        return None

    logger.info("Adjustment candidate: sell %s @%.2f (bid-slippage), spread_width=%.0f",
                best['symbol'], credit, spread_width)

    return {
        'adj_strike': best['strike'],
        'adj_symbol': best['symbol'],
        'adj_premium': credit,
        'adj_gap_pct': round(current_gap * 100, 2),
        'adj_spread_width': spread_width,
    }


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
    finally:
        # Flush store to ensure last state is persisted
        try:
            store.flush()
        except Exception:
            pass

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

"""Magnet monitor — price watching, entry/exit lifecycle, Telegram alerts.

Responsibilities:
1. Poll LTP for 'watching' signals — enter when gap shrinks to ≤2%
2. Poll LTP for 'entered' trades — exit on TP (spot crosses ST) / SL / time
3. Send Telegram alerts on state changes
4. Orchestrate the full run loop (scan every 5 min + monitor every 30s)
"""

import json
import logging
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
    """Send Telegram alert with HTML parse mode. Best-effort: never blocks or crashes."""
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
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg,
                  'parse_mode': 'HTML'},
            timeout=10,
        )
    except Exception as e:
        logger.debug("Telegram send failed: %s", e)


def _tf_tag(timeframe: str) -> str:
    """Short timeframe tag: [M] [W] [D]."""
    return {'monthly': '[M]', 'weekly': '[W]', 'daily': '[D]'}.get(timeframe, '[?]')


def _dir_icon(direction: str) -> str:
    """Red circle for PE (put), green for CE (call)."""
    return '\U0001f534' if direction == 'PE' else '\U0001f7e2'  # red/green circle


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

def get_spot_with_recent_range(kite, symbols, instrument_tokens=None):
    """Get LTP + recent HIGH/LOW for spot symbols.

    Uses LTP for current price, then fetches last 5 minute-candles
    to find recent high/low (catches wicks between 30s polls).
    Falls back to LTP-only if minute data unavailable.

    Returns {symbol: {ltp, high, low}}.
    """
    if not symbols:
        return {}

    # Step 1: Get LTP for all
    instruments = [f"NSE:{s}" for s in symbols]
    result = {}
    try:
        ltp_data = kite.ltp(instruments)
        for key, val in ltp_data.items():
            sym = key.replace('NSE:', '')
            ltp = val['last_price']
            result[sym] = {'ltp': ltp, 'high': ltp, 'low': ltp}
    except Exception as e:
        logger.error("LTP fetch failed: %s", e)
        return {}

    # Step 2: Get last 5 minute candles for recent high/low
    from .scanner import get_instrument_token
    now = datetime.now(IST)
    from_time = now - timedelta(minutes=5)

    for sym in symbols:
        try:
            token = get_instrument_token(kite, sym)
            candles = kite.historical_data(
                token, from_time.strftime('%Y-%m-%d %H:%M:%S'),
                now.strftime('%Y-%m-%d %H:%M:%S'), 'minute'
            )
            if candles:
                recent_high = max(c['high'] for c in candles)
                recent_low = min(c['low'] for c in candles)
                result[sym]['high'] = recent_high
                result[sym]['low'] = recent_low
        except Exception:
            pass  # keep LTP as fallback

    return result


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
                    # Filter to nearest expiry with >= MIN_DTE days
                    now_dt = datetime.now()
                    today = now_dt.strftime('%Y-%m-%d')
                    min_dte = (now_dt + timedelta(days=cfg.MIN_DTE)).strftime('%Y-%m-%d')
                    valid_expiry = [c for c in candidates if c['expiry'] >= min_dte]
                    if not valid_expiry:
                        valid_expiry = [c for c in candidates if c['expiry'] >= today]
                    if not valid_expiry:
                        logger.warning("No valid expiry for %s %s — CSV may be stale (run kite_nse_options.py)",
                                       stock, option_type)
                        return {}

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

    # Capacity check: don't exceed max open trades
    entered_count = len(store.get_entered())
    if entered_count >= cfg.MAX_OPEN_TRADES:
        logger.info("At capacity (%d/%d entered), skipping watching checks",
                     entered_count, cfg.MAX_OPEN_TRADES)
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
            tf = _tf_tag(trade.get('timeframe', ''))
            send_telegram(
                f"\u274c <b>CANCEL</b> {tf} {stock} | gap {gap:.1%}, missed window"
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

        if premium <= 0:
            logger.warning("SKIP ENTRY %s: premium is Rs 0 for %s — option expired or illiquid",
                           stock, option.get('symbol', '?'))
            continue

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

        icon = _dir_icon(trade['direction'])
        tf = _tf_tag(trade['timeframe'])
        msg = (
            f"{icon} <b>ENTRY</b> {tf} {stock}\n"
            f"Spot {price:,.1f} | ST {st_val:,.1f} | Gap {gap:.1%}\n"
            f"<code>{option['symbol']}</code> @ {premium:.2f}\n"
            f"Qty {qty} | SL {sl_spot:,.1f}"
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

    # Get LTP + last 5 min high/low for wick detection
    stocks = list({t['stock'] for t in entered})
    spot_data = get_spot_with_recent_range(kite, stocks)

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

        # Get current option premiums
        option_symbol = trade.get('option_symbol')
        long_exit = get_sell_price(kite, option_symbol) if option_symbol else 0
        hedge_exit = None
        if trade.get('hedged') and trade.get('hedge_symbol'):
            hedge_exit = get_buy_price(kite, trade['hedge_symbol'])

        # Gap from ST
        gap = abs(price - st_val) / st_val

        # === TRACK PEAK PREMIUM (for trailing SL — long leg only) ===
        entry_prem = trade.get('option_premium', 0) or 0
        if long_exit > 0 and entry_prem > 0:
            old_peak = _peak_premiums.get(trade_id, entry_prem)
            new_peak = max(old_peak, long_exit)
            _peak_premiums[trade_id] = new_peak

        # === ACTIVATE COST SL: gap shrinks to 1% (stock halfway to target) ===
        if not trade.get('cost_sl_active') and gap <= cfg.COST_SL_GAP:
            store.activate_cost_sl(trade_id)
            cost_lvl = trade.get('cost_sl_level', 0)
            msg = (
                f"\U0001f6e1 COST SL {stock} | gap {gap:.1%}\n"
                f"Floor {cost_lvl:.2f} (entry {entry_prem:.2f}) ZERO RISK"
            )
            send_telegram(msg)
            logger.info("COST SL: %s", msg.replace('\n', ' | '))

        # === HEDGE: gap widened back to 3% (reversal) — add short beyond ST ===
        # Skip if cost SL already active (trade was near ST, cost SL handles exit)
        if (not trade.get('hedged') and not trade.get('cost_sl_active')
                and gap >= cfg.HEDGE_GAP and gap < cfg.SL_GAP):
            hedge_result = _find_hedge_strike(kite, trade, price)
            if hedge_result:
                store.add_hedge(trade_id, hedge_result)
                nd = trade.get('hedge_net_debit', entry_prem)
                msg = (
                    f"\U0001f6e1 HEDGE {stock} | gap {gap:.1%}\n"
                    f"Short <code>{hedge_result['hedge_symbol']}</code> "
                    f"@ {hedge_result['hedge_premium']:.2f}\n"
                    f"Net debit {nd:.2f} | Max loss capped"
                )
                send_telegram(msg)
                logger.info("HEDGE: %s", msg.replace('\n', ' | '))
                hedge_exit = get_buy_price(kite, hedge_result['hedge_symbol'])

        # === CHECK TP: spot crossed ST line (using 5-min HIGH/LOW) ===
        tp_hit = False
        if side == 'above':
            # Bought PUT, expect decline. Touch = low ≤ ST
            tp_hit = day_low <= st_val
        else:
            # Bought CALL, expect rally. Touch = high ≥ ST
            tp_hit = day_high >= st_val

        if tp_hit:
            store.exit_trade(trade_id, price, long_exit, 'tp', hedge_exit)
            _peak_premiums.pop(trade_id, None)
            pnl = trade.get('pnl', 0)
            wick_note = ""
            if side == 'above' and price > st_val:
                wick_note = f"\nWick: 5m low {day_low:.2f} touched ST, current {price:.2f}"
            elif side == 'below' and price < st_val:
                wick_note = f"\nWick: 5m high {day_high:.2f} touched ST, current {price:.2f}"
            h = " H" if trade.get('hedged') else ""
            pnl_icon = '\u2705' if pnl >= 0 else '\u274c'
            tf = _tf_tag(trade.get('timeframe', ''))
            msg = (
                f"{pnl_icon} <b>TP</b> {tf} {stock}{h}\n"
                f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                f"{trade.get('days_held', 0)}d"
            )
            send_telegram(msg)
            logger.info("TP: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK TRAILING PREMIUM SL: 50% of peak gain ===
        peak = _peak_premiums.get(trade_id, entry_prem)
        if peak > entry_prem and long_exit > 0:
            gain = peak - entry_prem
            trail_level = entry_prem + gain * cfg.TRAIL_PCT
            # Trail must be above cost SL if active
            cost_sl = trade.get('cost_sl_level', 0) or 0
            effective_trail = max(trail_level, cost_sl)
            if long_exit <= effective_trail:
                store.exit_trade(trade_id, price, long_exit, 'tp_trail', hedge_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                pnl_icon = '\u2705' if pnl >= 0 else '\u274c'
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"{pnl_icon} <b>TRAIL</b> {tf} {stock}\n"
                    f"Peak {peak:.2f} | Trail {trail_level:.2f}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                    f"{trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("TRAIL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK COST SL: premium drops back to cost+0.10 ===
        if trade.get('cost_sl_active') and long_exit > 0:
            cost_sl = trade.get('cost_sl_level', 0) or 0
            if long_exit <= cost_sl:
                store.exit_trade(trade_id, price, long_exit, 'sl_cost', hedge_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"\U0001f7f0 <b>COST SL</b> {tf} {stock}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ~BE {trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("COST SL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK PREMIUM SL: 25% for daily (intraday), 40% for W/M ===
        # Daily: tighter SL because no day-2 recovery. Backtest: +80L at 25% vs +63L at none.
        if not trade.get('cost_sl_active') and not trade.get('hedged') and long_exit > 0:
            sl_pct = (cfg.DAILY_PREMIUM_SL_PCT if trade.get('timeframe') == 'daily'
                      else cfg.PREMIUM_SL_PCT)
            premium_sl = entry_prem * (1 - sl_pct)
            if long_exit <= premium_sl:
                actual_loss_pct = (entry_prem - long_exit) / entry_prem
                is_gap = actual_loss_pct > (sl_pct + 0.15)
                exit_reason = 'sl_premium_gap' if is_gap else 'sl_premium'
                store.exit_trade(trade_id, price, long_exit, exit_reason, hedge_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                gap_warn = (f"\nGAP: premium {long_exit:.2f} is {actual_loss_pct:.0%} "
                            f"below entry (SL was {cfg.PREMIUM_SL_PCT:.0%})") if is_gap else ""
                tf = _tf_tag(trade.get('timeframe', ''))
                gap_tag = " GAP!" if is_gap else ""
                msg = (
                    f"\u274c <b>PREM SL</b> {tf} {stock}{gap_tag}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} ({actual_loss_pct:.0%})\n"
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                    f"{trade.get('days_held', 0)}d"
                )
                send_telegram(msg)
                logger.info("PREM SL: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK SPOT SL: gap widened to 5% (hard backstop) ===
        sl_hit = False
        if side == 'above' and sl_spot:
            sl_hit = price >= sl_spot
        elif side == 'below' and sl_spot:
            sl_hit = price <= sl_spot

        if sl_hit:
            sl_gap_pct = abs(price - sl_spot) / sl_spot * 100
            exit_reason = 'sl_spot_gap' if sl_gap_pct > 1.0 else 'sl_spot'
            store.exit_trade(trade_id, price, long_exit, exit_reason, hedge_exit)
            _peak_premiums.pop(trade_id, None)
            pnl = trade.get('pnl', 0)
            gap_warn = (f"\nGAP: spot {price:.2f} is {sl_gap_pct:.1f}% past "
                        f"SL {sl_spot:.2f}") if sl_gap_pct > 1.0 else ""
            tf = _tf_tag(trade.get('timeframe', ''))
            gap_tag = " GAP!" if sl_gap_pct > 1.0 else ""
            msg = (
                f"\u274c <b>SPOT SL</b> {tf} {stock}{gap_tag}\n"
                f"Spot {price:,.1f} hit SL {sl_spot:,.1f}\n"
                f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%) "
                f"{trade.get('days_held', 0)}d"
            )
            send_telegram(msg)
            logger.info("SL: %s", msg.replace('\n', ' | '))
            continue

        # === CHECK EOD EXIT: daily trades are intraday-only ===
        # Backtest: day1 = +63L (77% win), day2+ = -37L (39% win). Theta kills overnight.
        # Exit same-day trades at 15:15. Exit prior-day daily trades immediately.
        if trade.get('timeframe') == 'daily':
            now_dt = datetime.now()
            entry_date_str = trade.get('entry_date', '')
            today_str = now_dt.strftime('%Y-%m-%d')
            should_eod = False

            if entry_date_str and entry_date_str < today_str:
                # Entered yesterday or earlier — exit immediately (stale daily trade)
                should_eod = True
            elif (entry_date_str == today_str
                    and (now_dt.hour, now_dt.minute) >= (cfg.DAILY_EOD_EXIT_HOUR, cfg.DAILY_EOD_EXIT_MIN)):
                # Same day, past EOD exit time
                should_eod = True

            if should_eod:
                reason = 'eod_daily' if entry_date_str == today_str else 'eod_daily_stale'
                store.exit_trade(trade_id, price, long_exit, reason, hedge_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                pnl_icon = '\u2705' if pnl >= 0 else '\u274c'
                stale = " (stale)" if entry_date_str != today_str else ""
                msg = (
                    f"{pnl_icon} <b>EOD EXIT</b> [D] {stock}{stale}\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%)"
                )
                send_telegram(msg)
                logger.info("EOD: %s", msg.replace('\n', ' | '))
                continue

        # === CHECK TIME SL: 5 trading days (weekly/monthly only now) ===
        entry_date = trade.get('entry_date')
        if entry_date:
            entry_dt = datetime.strptime(entry_date, '%Y-%m-%d')
            now = datetime.now()
            # Count trading days (weekdays excluding known Indian market holidays)
            _market_holidays_2026 = {
                '2026-01-26', '2026-03-14', '2026-03-17', '2026-03-31',
                '2026-04-01', '2026-04-14', '2026-04-18', '2026-05-01',
                '2026-06-26', '2026-07-17', '2026-08-15', '2026-08-28',
                '2026-10-02', '2026-10-20', '2026-10-21', '2026-10-23',
                '2026-11-04', '2026-11-05', '2026-12-25',
            }
            biz_days = 0
            d = entry_dt + timedelta(days=1)
            while d <= now:
                if d.weekday() < 5 and d.strftime('%Y-%m-%d') not in _market_holidays_2026:
                    biz_days += 1
                d += timedelta(days=1)
            if biz_days >= cfg.SL_TIME_DAYS:
                store.exit_trade(trade_id, price, long_exit, 'sl_time', hedge_exit)
                _peak_premiums.pop(trade_id, None)
                pnl = trade.get('pnl', 0)
                tf = _tf_tag(trade.get('timeframe', ''))
                msg = (
                    f"\u23f0 <b>TIME SL</b> {tf} {stock} ({biz_days}d)\n"
                    f"{entry_prem:.2f}\u2192{long_exit:.2f} | "
                    f"<b>Rs {pnl:+,.0f}</b> ({trade.get('pnl_pct', 0):+.1f}%)"
                )
                send_telegram(msg)
                logger.info("TIME SL: %s", msg.replace('\n', ' | '))
                continue


# ── Hedge: Short Leg Beyond ST ─────────────────────────────────────────────

def _find_hedge_strike(kite, trade: dict, current_spot: float) -> dict:
    """Find nearest strike BEYOND ST to sell as hedge.

    For PE (price above ST): sell PUT with strike BELOW ST
    For CE (price below ST): sell CALL with strike ABOVE ST

    This ensures short leg is OTM at target → no TV problem.
    Returns hedge_data dict or None.
    """
    import csv

    stock = trade['stock']
    direction = trade['direction']  # CE or PE
    long_strike = trade['option_strike']
    long_expiry = trade.get('option_expiry', '')
    st_val = trade['st_value']
    entry_prem = trade.get('option_premium', 0) or 0
    option_csv = cfg.PROJECT_ROOT / 'nse_stocks_options.csv'

    candidates = []

    # Try CSV — filter to same expiry
    if option_csv.exists():
        try:
            with open(option_csv, newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (row.get('stock_symbol', '') == stock
                            and row.get('option_type', '') == direction):
                        expiry = row.get('option_expiry', '')
                        if long_expiry and expiry != long_expiry:
                            continue
                        strike = float(row.get('option_strike', 0))
                        candidates.append({
                            'strike': strike,
                            'symbol': row.get('option_tradingsymbol', ''),
                        })
        except Exception:
            pass

    # Fallback: NFO instruments
    if not candidates:
        try:
            instruments = _get_nfo_instruments(kite)
            for inst in instruments:
                if (inst['name'] == stock
                        and inst['instrument_type'] == direction):
                    inst_expiry = str(inst.get('expiry', ''))
                    if long_expiry and inst_expiry != long_expiry:
                        continue
                    candidates.append({
                        'strike': inst['strike'],
                        'symbol': inst['tradingsymbol'],
                    })
        except Exception:
            pass

    if not candidates:
        return None

    # KEY RULE: short strike must be BEYOND ST
    if direction == 'PE':
        # Long PUT, target is decline to ST. Short PUT below ST.
        beyond = [c for c in candidates if c['strike'] < st_val]
        if not beyond:
            return None
        # Nearest strike below ST (maximize credit, minimize spread width)
        beyond.sort(key=lambda x: x['strike'], reverse=True)
        best = beyond[0]
        spread_width = long_strike - best['strike']
    else:
        # Long CALL, target is rally to ST. Short CALL above ST.
        beyond = [c for c in candidates if c['strike'] > st_val]
        if not beyond:
            return None
        # Nearest strike above ST
        beyond.sort(key=lambda x: x['strike'])
        best = beyond[0]
        spread_width = best['strike'] - long_strike

    if spread_width <= 0:
        return None

    # Get credit (BID - slippage)
    credit = get_sell_price(kite, best['symbol'])
    if credit <= 0:
        logger.warning("No bid for hedge %s, skipping", best['symbol'])
        return None

    # Check debit ratio
    net_debit = entry_prem - credit
    debit_ratio = net_debit / spread_width if spread_width > 0 else 1.0
    if debit_ratio > cfg.HEDGE_MAX_DEBIT_RATIO:
        logger.info("Hedge debit ratio %.1f%% > max %.0f%%, skipping (fallback to premium SL)",
                     debit_ratio * 100, cfg.HEDGE_MAX_DEBIT_RATIO * 100)
        return None

    logger.info("Hedge: sell %s @%.2f, width=%.0f, net_debit=%.2f, ratio=%.1f%%",
                best['symbol'], credit, spread_width, net_debit, debit_ratio * 100)

    return {
        'hedge_strike': best['strike'],
        'hedge_symbol': best['symbol'],
        'hedge_premium': credit,
        'hedge_spread_width': spread_width,
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
                                icon = _dir_icon(sig['direction'])
                                tf = _tf_tag(sig['timeframe'])
                                send_telegram(
                                    f"{icon} <b>SIGNAL</b> {tf} {sig['stock']}\n"
                                    f"ST {sig['st_value']:,.1f} ({sig['st_direction']}) "
                                    f"| Gap {sig['signal_gap_pct']:.1f}%\n"
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

    pnl_icon = '\u2705' if total_pnl >= 0 else '\u274c'
    summary = (
        f"\U0001f4ca <b>Magnet EOD</b>\n"
        f"Watch {watching} | Open {entered} | "
        f"Exit {len(exited_today)} | {pnl_icon} <b>Rs {total_pnl:+,.0f}</b>"
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

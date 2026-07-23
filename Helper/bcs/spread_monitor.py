#!/usr/bin/env python3
"""
Unified Spread Monitor — BCS + Bear Put Spread + Fallen Hero

Monitors spot price and spread value for BCS and Fallen Hero trades.
Auto-closes on target hit or stop-loss trigger.

BCS Stop-Loss Layers (checked every poll cycle, in order):
  1. SL_SPOT    — spot <= sl_spot → thesis dead, close
  2. SL_SPREAD  — spread_value <= sl_spread → 50% loss guard, close
  3. SL_TRAIL   — engages at 2x entry debit, trails 60% of peak spread
  4. TP         — spot >= target → profit target, close

FH Stop-Loss (spot-only, reversed direction):
  1. SL_SPOT    — spot >= sl_spot → upside breakout, close
  2. EXPIRY     — force-close on expiry day

BCS Close sequence (margin rules - CRITICAL):
  1. BUY back short leg FIRST  (avoids naked short margin spike)
  2. SELL long leg AFTER short fills

FH Close sequence (naked risk first):
  1. BUY back short call (naked risk — most dangerous)
  2. SELL long call (if exists — hedge)
  3. BUY back short put
  4. SELL long put

Usage:
    python -m bcs.spread_monitor ICICIBANK                 # Monitor open BCS trade
    python -m bcs.spread_monitor ICICIBANK --target 1440   # Override target
    python -m bcs.spread_monitor ICICIBANK --sl-spot 1320  # Override SL
    python -m bcs.spread_monitor ICICIBANK --dry-run       # No real orders
    python -m bcs.spread_monitor --list                    # List all trades (BCS + FH)
    python -m bcs.spread_monitor --cron                    # Monitor ALL open (BCS + FH)
"""

import json
import logging
import sys
import time
import argparse
from datetime import datetime, date, time as dtime
from pathlib import Path
from typing import Optional

# Kite's IP whitelist holds only the shared home IPv4; dual-stack machines
# otherwise connect over a rotating IPv6 and order placement is rejected
# with PermissionException (quotes still work, so the failure only surfaces
# at the exit/entry moment). Force all connections over IPv4.
import socket as _socket

_orig_getaddrinfo = _socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, _socket.AF_INET, type, proto, flags)


_socket.getaddrinfo = _ipv4_only_getaddrinfo

from kiteconnect import KiteConnect

from .trade_store import get_store
from fallen_hero import get_store as get_fh_store
from bear_put import get_store as get_bps_store
from zerodha.alert_checker import check_watchlist_alerts
from zerodha.watchlist import get_watchlist_store

logger = logging.getLogger(__name__)

# ── Execution Config ─────────────────────────────────────────────────────────
POLL_INTERVAL_SEC = 5
STATUS_PRINT_INTERVAL_SEC = 30
TICK_SIZE = 0.05
SLIPPAGE_TICKS_BASE = 2          # initial buffer: 2 ticks = Rs 0.10
SLIPPAGE_TICKS_INCREMENT = 2     # add 2 more ticks per retry
ORDER_WAIT_SEC = 30              # wait for fill before retry
MAX_RETRIES = 3
PRODUCT = "NRML"

# ── Trailing SL Config ───────────────────────────────────────────────────────
TRAIL_ENGAGE_MULTIPLIER = 2.0    # engage when spread >= 2x entry debit
TRAIL_PERCENT = 0.60             # trail at 60% of peak spread

# ── Market-Open Buffer ──────────────────────────────────────────────────────
# Don't act on SL/TP triggers for N seconds after market open.
# At 09:15, order books are thin/empty → spread values are garbage.
MARKET_OPEN_BUFFER_SEC = 180     # 3 minutes = wait until 09:18

# ── Market Hours (IST) ──────────────────────────────────────────────────────
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
LAST_ORDER_TIME = dtime(15, 20)   # Don't place new orders after this time

# ── Error Budget ──────────────────────────────────────────────────────────
MAX_CONSECUTIVE_ERRORS = 20       # Exit after this many consecutive API errors

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # Helper/bcs/
PROJECT_ROOT = SCRIPT_DIR.parent                   # Helper/
BOTS_ROOT = PROJECT_ROOT.parent                    # BOTS/
TOKEN_FILE = BOTS_ROOT / 'data' / 'kite_access_token.json'
LOG_DIR = PROJECT_ROOT / 'logs'

# ── Module-level log state (set per-trade in monitor()) ──────────────────────
_log_file: Optional[Path] = None


def set_log_file(path: Path):
    """Set the active log file path."""
    global _log_file
    _log_file = path


def log(msg: str):
    """Print and append to log file with timestamp."""
    ts = datetime.now().strftime('%H:%M:%S')
    line = f"[{ts}] {msg}"
    print(line)
    if _log_file:
        LOG_DIR.mkdir(exist_ok=True)
        with open(_log_file, 'a') as f:
            f.write(line + '\n')


# ── Kite Auth ────────────────────────────────────────────────────────────────

def load_kite() -> KiteConnect:
    """Authenticate and return Kite client."""
    if not TOKEN_FILE.exists():
        log(f"FATAL: Token file not found at {TOKEN_FILE}")
        sys.exit(1)

    with open(TOKEN_FILE) as f:
        token_data = json.load(f)

    generated = datetime.fromisoformat(token_data['generated_at'])
    if generated.date() != date.today():
        log(f"WARNING: Token is from {generated.date()}, may be expired!")

    kite = KiteConnect(api_key=token_data['api_key'])
    kite.set_access_token(token_data['access_token'])
    return kite


# ── Market & Price ───────────────────────────────────────────────────────────

def is_market_open() -> bool:
    """Check if within NSE trading hours."""
    now = datetime.now().time()
    return MARKET_OPEN <= now < MARKET_CLOSE


def get_spot(kite: KiteConnect, spot_symbol: str) -> float:
    """Fetch spot price for given symbol."""
    q = kite.ltp([spot_symbol])
    return q[spot_symbol]['last_price']


def get_option_depth(kite: KiteConnect, exchange: str, symbol: str) -> dict:
    """Fetch full depth for an option symbol."""
    full = f"{exchange}:{symbol}"
    q = kite.quote([full])[full]
    depth = q.get('depth', {})
    best_bid = depth['buy'][0] if depth.get('buy') else {'price': 0, 'quantity': 0}
    best_ask = depth['sell'][0] if depth.get('sell') else {'price': 0, 'quantity': 0}
    return {
        'bid': best_bid['price'],
        'bid_qty': best_bid['quantity'],
        'ask': best_ask['price'],
        'ask_qty': best_ask['quantity'],
        'ltp': q['last_price'],
    }


def get_spread_value(kite: KiteConnect, trade: dict) -> dict:
    """Fetch both legs and compute spread value (long bid - short ask).

    Returns dict with long depth, short depth, and computed spread.
    Spread is None if depth is invalid (e.g. no bids/asks at market open).
    """
    long_d = get_option_depth(kite, trade['exchange'], trade['long_symbol'])
    short_d = get_option_depth(kite, trade['exchange'], trade['short_symbol'])

    # Spread is only meaningful when both sides have real depth.
    # At market open, bid/ask can be 0 → spread = 0 - ask = negative garbage.
    if long_d['bid'] > 0 and long_d['bid_qty'] > 0 and short_d['ask'] > 0 and short_d['ask_qty'] > 0:
        spread_val = long_d['bid'] - short_d['ask']
        # Negative spread = bid-ask inversion or market dislocation.
        # Not a real loss signal, treat as unreliable data.
        if spread_val < 0:
            spread_val = None
    else:
        spread_val = None

    return {
        'long': long_d,
        'short': short_d,
        'spread': spread_val,
    }


# ── Strategy Detection ───────────────────────────────────────────────────────

def get_strategy(trade: dict) -> str:
    """Detect strategy from trade fields.

    FH has long_put_symbol + short_call_symbol.
    BPS has _store_type='bps' (same field names as BCS).
    BCS is the default.
    """
    if trade.get('_store_type') == 'bps':
        return 'BPS'
    return 'FH' if 'long_put_symbol' in trade else 'BCS'


def get_fh_position_value(kite: KiteConnect, trade: dict) -> dict:
    """Fetch all FH legs and compute net position value (cost to close).

    Returns dict with leg depths, close cost, and P&L per share.
    P&L = total_credit - close_cost (credit strategy).
    """
    exchange = trade['exchange']
    sc_d = get_option_depth(kite, exchange, trade['short_call_symbol'])
    sp_d = get_option_depth(kite, exchange, trade['short_put_symbol'])
    lp_d = get_option_depth(kite, exchange, trade['long_put_symbol'])
    lc_d = None
    if trade.get('long_call_symbol'):
        lc_d = get_option_depth(kite, exchange, trade['long_call_symbol'])

    # Cost to close = buy back shorts (ask) - sell longs (bid)
    close_cost = None
    if sc_d['ask'] > 0 and sp_d['ask'] > 0 and lp_d['bid'] > 0:
        cost = sc_d['ask'] + sp_d['ask'] - lp_d['bid']
        if lc_d and lc_d['bid'] > 0:
            cost -= lc_d['bid']
        close_cost = cost

    pnl_per_share = None
    if close_cost is not None:
        pnl_per_share = trade['total_credit'] - close_cost

    return {
        'short_call': sc_d,
        'short_put': sp_d,
        'long_put': lp_d,
        'long_call': lc_d,
        'close_cost': close_cost,
        'pnl_per_share': pnl_per_share,
    }


# ── Position & Alerting Helpers ──────────────────────────────────────────────

def get_net_position(kite: KiteConnect, symbol: str) -> int:
    """Get net quantity for a tradingsymbol from Kite positions."""
    for p in kite.positions()['net']:
        if p['tradingsymbol'] == symbol:
            return p['quantity']
    return 0


def is_market_settled() -> bool:
    """Check if we're past the initial market-open volatility window.

    At 09:15, order books are thin/empty and spread values are unreliable.
    Returns True once MARKET_OPEN_BUFFER_SEC has elapsed after open.
    """
    from datetime import timedelta
    settle_time = datetime.combine(date.today(), MARKET_OPEN) + timedelta(seconds=MARKET_OPEN_BUFFER_SEC)
    return datetime.now() >= settle_time


def is_expiry_day(trade: dict) -> bool:
    """Check if today is the trade's expiry date."""
    try:
        expiry_str = trade.get('expiry', '')
        expiry_date = datetime.strptime(expiry_str, '%Y-%m-%d').date()
        return date.today() == expiry_date
    except (ValueError, TypeError):
        return False


EXPIRY_FORCE_CLOSE_TIME = dtime(15, 15)   # Force close by 15:15 on expiry day


_telegram_cfg: Optional[dict] = None
_telegram_cfg_loaded = False


def send_telegram(msg: str):
    """Send Telegram alert. Best-effort: never blocks or crashes trading."""
    global _telegram_cfg, _telegram_cfg_loaded

    try:
        # Load config once and cache
        if not _telegram_cfg_loaded:
            config_path = BOTS_ROOT / 'data' / 'telegram_config.json'
            if config_path.exists():
                with open(config_path) as f:
                    _telegram_cfg = json.load(f)
            _telegram_cfg_loaded = True

        if not _telegram_cfg:
            return

        import requests
        requests.post(
            f"https://api.telegram.org/bot{_telegram_cfg['bot_token']}/sendMessage",
            json={'chat_id': _telegram_cfg['chat_id'], 'text': msg},
            timeout=10,
        )
    except ImportError:
        log(f"  Telegram alert skipped: 'requests' package not installed")
    except Exception as e:
        log(f"  Telegram alert failed: {e}")


# ── Order Execution ─────────────────────────────────────────────────────────

ORDER_TAG = "BCS_MON"   # Tag for all orders placed by this script


def _find_last_fill_price(kite: KiteConnect, symbol: str, txn_type: str) -> float:
    """Find the fill price of the most recent COMPLETE order for a symbol+side.

    Used to recover the actual fill when close_leg detects the position changed
    before it could observe the fill directly. Returns 0 if not found.

    Prefers orders tagged with ORDER_TAG (placed by this script). Falls back
    to any matching order if no tagged order found.
    """
    try:
        tagged_best = None
        any_best = None
        for o in kite.orders():
            if (o.get('tradingsymbol') == symbol
                    and o.get('transaction_type') == txn_type
                    and o.get('status') == 'COMPLETE'
                    and o.get('average_price', 0) > 0):
                ts = str(o.get('order_timestamp', ''))
                if o.get('tag') == ORDER_TAG:
                    if tagged_best is None or ts > str(tagged_best.get('order_timestamp', '')):
                        tagged_best = o
                if any_best is None or ts > str(any_best.get('order_timestamp', '')):
                    any_best = o

        best = tagged_best or any_best
        if best:
            tag_note = "" if best.get('tag') == ORDER_TAG else " (WARNING: not tagged by BCS_MON)"
            log(f"    Recovered fill: {symbol} {txn_type} @ {best['average_price']} (order {best['order_id']}){tag_note}")
            return best['average_price']
    except Exception as e:
        log(f"    Could not recover fill price: {e}")
    return 0.0


def _find_pending_orders(kite: KiteConnect, symbol: str, txn_type: str) -> list:
    """Find OPEN/PENDING orders for a symbol+side placed by this script.

    Used to detect orders that were placed but whose response was lost
    (network drop after place_order succeeded). Prevents duplicate orders.
    """
    pending = []
    try:
        for o in kite.orders():
            if (o.get('tradingsymbol') == symbol
                    and o.get('transaction_type') == txn_type
                    and o.get('tag') == ORDER_TAG
                    and o.get('status') in ('OPEN', 'PENDING', 'TRIGGER PENDING')):
                pending.append(o)
    except Exception as e:
        log(f"    Could not check pending orders: {e}")
    return pending


def round_to_tick(price: float) -> float:
    """Round price to nearest tick size."""
    return round(round(price / TICK_SIZE) * TICK_SIZE, 2)


def place_limit_order(kite: KiteConnect, exchange: str, symbol: str,
                      txn_type: str, qty: int, price: float,
                      dry_run: bool) -> str:
    """Place a LIMIT NRML order. Returns order_id or 'DRY_RUN_xxx'."""
    price = round_to_tick(price)
    if dry_run:
        fake_id = f"DRY_{datetime.now().strftime('%H%M%S%f')}"
        log(f"    [DRY RUN] {txn_type} {symbol} x {qty} @ {price} -> {fake_id}")
        return fake_id

    order_id = kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange=exchange,
        tradingsymbol=symbol,
        transaction_type=txn_type,
        quantity=qty,
        product=PRODUCT,
        order_type=kite.ORDER_TYPE_LIMIT,
        price=price,
        tag=ORDER_TAG,
    )
    log(f"    Order placed: {txn_type} {symbol} x {qty} @ {price} -> {order_id}")
    return str(order_id)


def wait_for_fill(kite: KiteConnect, order_id: str, dry_run: bool) -> Optional[dict]:
    """Poll order status until COMPLETE, REJECTED, CANCELLED, or timeout.

    Returns the order dict with status info. For partial fills:
    - status will be 'COMPLETE' if fully filled
    - filled_quantity and average_price reflect what actually filled
    - Returns None on timeout (caller should cancel and check)
    """
    if dry_run:
        return {'status': 'COMPLETE', 'average_price': 0.0, 'order_id': order_id,
                'filled_quantity': 0}

    deadline = time.time() + ORDER_WAIT_SEC
    while time.time() < deadline:
        try:
            for o in kite.orders():
                if str(o['order_id']) == order_id:
                    if o['status'] == 'COMPLETE':
                        return o
                    if o['status'] == 'REJECTED':
                        log(f"    Order REJECTED: {o.get('status_message', '')}")
                        return o
                    if o['status'] == 'CANCELLED':
                        # Could be partially filled then cancelled
                        filled = o.get('filled_quantity', 0)
                        if filled > 0:
                            log(f"    Order CANCELLED with partial fill: {filled} qty @ {o.get('average_price', 0)}")
                        else:
                            log(f"    Order CANCELLED: {o.get('status_message', '')}")
                        return o
        except Exception as e:
            log(f"    Order status poll error: {e}")
        time.sleep(1)

    log(f"    Order {order_id} timed out after {ORDER_WAIT_SEC}s")
    return None


def cancel_order_safe(kite: KiteConnect, order_id: str, dry_run: bool):
    """Cancel an open order, swallowing errors."""
    if dry_run:
        return
    try:
        kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
        log(f"    Cancelled order {order_id}")
    except Exception as e:
        log(f"    Cancel failed for {order_id}: {e}")


def close_leg(kite: KiteConnect, exchange: str, symbol: str, txn_type: str,
              qty: int, is_buy: bool, dry_run: bool) -> Optional[dict]:
    """
    Close one leg with retry + escalating slippage.

    is_buy=True  -> buying back short, price = ASK + slippage
    is_buy=False -> selling long,       price = BID - slippage

    Design principles (post 2026-02-18 incident):
      1. Track REMAINING qty — never retry with the original full qty
      2. Check for pending orders from this script before placing new ones
      3. Handle partial fills — reduce remaining, continue with rest
      4. Price guards — sell never below TICK_SIZE, buy never above sanity limit
      5. Don't retry REJECTED orders (margin, price band — won't resolve)
      6. After cancel, always re-check for race-condition fills
    """
    NO_DEPTH_MAX_WAITS = 10     # Wait up to 10 × 3s = 30s for depth to appear
    NO_DEPTH_WAIT_SEC = 3
    BUY_PRICE_SANITY_MULT = 5.0  # Reject buy price > 5x spread_width (illiquid)

    remaining_qty = qty
    cumulative_fill_value = 0.0   # sum of (fill_price × filled_qty) across attempts
    cumulative_fill_qty = 0       # sum of filled qty across attempts

    for attempt in range(1, MAX_RETRIES + 1):
        # ── Safety: Re-check position to compute actual remaining ─────
        if not dry_run:
            current_qty = get_net_position(kite, symbol)
            if is_buy:
                # Buying back short: remaining = how much is still short
                actual_remaining = abs(min(current_qty, 0))
            else:
                # Selling long: remaining = how much is still long
                actual_remaining = max(current_qty, 0)

            if actual_remaining == 0:
                log(f"    Position {symbol} already flat (qty={current_qty}). Nothing to close.")
                fill_price = _find_last_fill_price(kite, symbol, txn_type)
                total_filled = cumulative_fill_qty if cumulative_fill_qty > 0 else qty
                if cumulative_fill_qty > 0:
                    fill_price = cumulative_fill_value / cumulative_fill_qty
                return {'status': 'COMPLETE', 'average_price': fill_price,
                        'order_id': 'position_verified', 'filled_quantity': total_filled}

            if actual_remaining < remaining_qty:
                log(f"    Position changed: {symbol} qty={current_qty}. Reducing order from {remaining_qty} to {actual_remaining}.")
                remaining_qty = actual_remaining

            # ── Idempotency: Check for pending orders from this script ──
            # Always check (not just retries) — a crashed previous run may
            # have left pending orders that recover_closing_trade didn't cancel.
            if not dry_run:
                pending = _find_pending_orders(kite, symbol, txn_type)
                if pending:
                    order_id = str(pending[0]['order_id'])
                    log(f"    Found pending order {order_id} from this script. Waiting for it...")
                    result = wait_for_fill(kite, order_id, dry_run=False)
                    if result and result['status'] == 'COMPLETE':
                        log(f"    Pending order FILLED at {result['average_price']}")
                        return result
                    # If pending order timed out/cancelled, continue to retry logic
                    filled = result.get('filled_quantity', 0) if result else 0
                    if filled > 0:
                        cumulative_fill_qty += filled
                        cumulative_fill_value += filled * result.get('average_price', 0)
                        remaining_qty = max(0, remaining_qty - filled)
                        log(f"    Partial fill: {filled} qty. Remaining: {remaining_qty}")
                        if remaining_qty == 0:
                            avg = cumulative_fill_value / cumulative_fill_qty if cumulative_fill_qty else 0
                            return {'status': 'COMPLETE', 'average_price': avg,
                                    'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

        # ── Wait for depth (don't burn order-retry slots on empty books) ──
        depth = None
        for wait_i in range(NO_DEPTH_MAX_WAITS):
            depth = get_option_depth(kite, exchange, symbol)
            if is_buy and depth['ask'] > 0 and depth['ask_qty'] > 0:
                break
            if not is_buy and depth['bid'] > 0 and depth['bid_qty'] > 0:
                break
            side = "ask" if is_buy else "bid"
            log(f"    No {side} depth for {symbol} (wait {wait_i+1}/{NO_DEPTH_MAX_WAITS})...")
            time.sleep(NO_DEPTH_WAIT_SEC)
        else:
            side = "ask" if is_buy else "bid"
            log(f"    No {side} depth after {NO_DEPTH_MAX_WAITS * NO_DEPTH_WAIT_SEC}s. Skipping attempt {attempt}.")
            continue

        slippage = (SLIPPAGE_TICKS_BASE + SLIPPAGE_TICKS_INCREMENT * (attempt - 1)) * TICK_SIZE

        if is_buy:
            price = depth['ask'] + slippage
            # ── Buy price sanity: don't overpay into illiquid books ──
            if price > depth['ask'] * BUY_PRICE_SANITY_MULT and depth['ask'] > 1.0:
                log(f"    BUY PRICE SANITY: {round_to_tick(price)} > {BUY_PRICE_SANITY_MULT}x ask ({depth['ask']}). Capping.")
                price = depth['ask'] * 2  # Cap at 2x ask as reasonable ceiling
            log(f"  Attempt {attempt}/{MAX_RETRIES}: BUY {symbol} x {remaining_qty}")
            log(f"    Depth -> Ask: {depth['ask']} x {depth['ask_qty']} | Bid: {depth['bid']} x {depth['bid_qty']}")
            log(f"    Limit price: {depth['ask']} + {slippage:.2f} slippage = {round_to_tick(price)}")
        else:
            price = depth['bid'] - slippage
            # ── Sell price floor: never sell for nothing ──
            if price < TICK_SIZE:
                log(f"    SELL PRICE FLOOR: {price:.2f} < {TICK_SIZE}. Setting to {TICK_SIZE}.")
                price = TICK_SIZE
            # ── Sell price sanity: don't sell at < 50% of bid ──
            if price < depth['bid'] * 0.5 and depth['bid'] > TICK_SIZE:
                log(f"    SELL PRICE SANITY: {round_to_tick(price)} < 50% of bid ({depth['bid']}). Using bid directly.")
                price = depth['bid']
            log(f"  Attempt {attempt}/{MAX_RETRIES}: SELL {symbol} x {remaining_qty}")
            log(f"    Depth -> Bid: {depth['bid']} x {depth['bid_qty']} | Ask: {depth['ask']} x {depth['ask_qty']}")
            log(f"    Limit price: {depth['bid']} - {slippage:.2f} slippage = {round_to_tick(price)}")

        order_id = place_limit_order(kite, exchange, symbol, txn_type, remaining_qty, price, dry_run)
        result = wait_for_fill(kite, order_id, dry_run)

        if result and result['status'] == 'COMPLETE':
            fill = result['average_price']
            filled_qty = result.get('filled_quantity', remaining_qty)
            cumulative_fill_qty += filled_qty
            cumulative_fill_value += filled_qty * fill
            log(f"    FILLED at {fill} | Qty: {filled_qty} | Order: {order_id}")
            avg = cumulative_fill_value / cumulative_fill_qty if cumulative_fill_qty else fill
            return {'status': 'COMPLETE', 'average_price': avg,
                    'order_id': order_id, 'filled_quantity': cumulative_fill_qty}

        # ── REJECTED: don't retry (margin, price band, frozen qty) ──
        if result and result['status'] == 'REJECTED':
            msg = result.get('status_message', 'unknown')
            log(f"    ORDER REJECTED: {msg}. Will not retry (same error likely).")
            send_telegram(f"Order REJECTED: {txn_type} {symbol} x {remaining_qty} — {msg}")
            return None

        # ── Not filled / CANCELLED — check for partial fill then retry ──
        partial_qty = 0
        if result and result.get('filled_quantity', 0) > 0:
            # Partial fill on cancelled order
            partial_qty = result['filled_quantity']
            partial_price = result.get('average_price', 0)
            cumulative_fill_qty += partial_qty
            cumulative_fill_value += partial_qty * partial_price
            remaining_qty = max(0, remaining_qty - partial_qty)
            log(f"    Partial fill: {partial_qty} @ {partial_price}. Remaining: {remaining_qty}")
            if remaining_qty == 0:
                avg = cumulative_fill_value / cumulative_fill_qty
                return {'status': 'COMPLETE', 'average_price': avg,
                        'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

        # ── Timeout — cancel and check for race-condition fill ──
        if result is None:
            cancel_order_safe(kite, order_id, dry_run)
            if not dry_run:
                time.sleep(1)
                try:
                    for o in kite.orders():
                        if str(o['order_id']) == order_id:
                            if o['status'] == 'COMPLETE':
                                log(f"    Order {order_id} filled after cancel! Fill: {o['average_price']}")
                                fill = o['average_price']
                                filled_qty = o.get('filled_quantity', remaining_qty)
                                cumulative_fill_qty += filled_qty
                                cumulative_fill_value += filled_qty * fill
                                avg = cumulative_fill_value / cumulative_fill_qty
                                return {'status': 'COMPLETE', 'average_price': avg,
                                        'order_id': order_id, 'filled_quantity': cumulative_fill_qty}
                            # Check for partial fill on the cancelled order
                            filled_qty = o.get('filled_quantity', 0)
                            if filled_qty > 0:
                                cumulative_fill_qty += filled_qty
                                cumulative_fill_value += filled_qty * o.get('average_price', 0)
                                remaining_qty = max(0, remaining_qty - filled_qty)
                                log(f"    Post-cancel partial: {filled_qty} filled. Remaining: {remaining_qty}")
                                if remaining_qty == 0:
                                    avg = cumulative_fill_value / cumulative_fill_qty
                                    return {'status': 'COMPLETE', 'average_price': avg,
                                            'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}
                            break
                except Exception as e:
                    log(f"    Post-cancel check failed: {e}")

        if attempt < MAX_RETRIES:
            log(f"    Retrying with +{SLIPPAGE_TICKS_INCREMENT} ticks slippage... (remaining: {remaining_qty})")
            time.sleep(1)

    # All retries exhausted
    if cumulative_fill_qty > 0:
        avg = cumulative_fill_value / cumulative_fill_qty
        log(f"    PARTIAL CLOSE: {cumulative_fill_qty}/{qty} filled @ avg {avg:.2f}. {remaining_qty} remaining!")
        send_telegram(f"PARTIAL CLOSE: {symbol} {cumulative_fill_qty}/{qty} filled. {remaining_qty} remaining!")
        return {'status': 'PARTIAL', 'average_price': avg,
                'order_id': 'cumulative', 'filled_quantity': cumulative_fill_qty}

    log(f"    FAILED to close {symbol} after {MAX_RETRIES} attempts!")
    return None


def close_spread(kite: KiteConnect, trade: dict, spot: float,
                 reason: str, dry_run: bool, store=None,
                 strategy_label: str = 'BCS') -> bool:
    """
    Close the full spread. Short FIRST, then long (margin rules).
    Works for both BCS and BPS (same 2-leg structure).
    Updates the provided TradeStore on success (local + Drive).
    Returns True if fully closed, False if any leg failed.

    Safety checks:
      - Acquires close-lock (status='closing') before any orders
      - Late-day guard: refuses to place orders after LAST_ORDER_TIME
      - Verifies actual position state before placing any orders
      - If short is already flat/long, skips to closing the long
      - On both-legs-flat: marks trade closed with recovered fill prices
      - On partial failure: marks trade 'partial_close' (not 'open')
      - Sends Telegram alerts on trigger, success, and failure
    """
    if store is None:
        store = get_store()  # Default to BCS store for backward compat
    label = strategy_label
    stock = trade['stock']

    log("")
    log("=" * 70)
    log(f"  {reason} TRIGGERED! {stock} spot = {spot}")
    log(f"  Initiating spread close sequence...")
    log("=" * 70)

    # ── Late-day guard ────────────────────────────────────────────────────
    now_t = datetime.now().time()
    if now_t > LAST_ORDER_TIME:
        log(f"  LATE-DAY GUARD: {now_t.strftime('%H:%M')} > {LAST_ORDER_TIME.strftime('%H:%M')}.")
        log(f"  Too close to market close. Not placing orders — manual intervention needed.")
        send_telegram(
            f"{label} {reason} TRIGGERED {stock} @ {spot}\n"
            f"BUT past {LAST_ORDER_TIME.strftime('%H:%M')} — NOT auto-closing.\n"
            f"Close manually in Kite!"
        )
        return False

    # ── Acquire close-lock (prevents concurrent close from another machine) ──
    close_lock_acquired = False
    if not dry_run:
        if not store.begin_close(trade['id'], reason):
            log(f"  Trade #{trade['id']} is already closing/closed. Skipping.")
            return True  # Not an error — another process has it
        close_lock_acquired = True

    try:
        return _close_spread_inner(kite, store, trade, spot, reason, dry_run, label)
    except Exception as e:
        log(f"  EXCEPTION during close_spread: {e}")
        send_telegram(f"{label} {stock}: EXCEPTION during {reason} close! {e}\nManual intervention needed!")
        if close_lock_acquired:
            try:
                store.set_trade_status(trade['id'], 'partial_close',
                                       close_error=str(e), close_reason=reason)
            except Exception as inner_e:
                log(f"  Could not set partial_close status: {inner_e}")
                store._sync_locked = False  # Last resort: clear lock directly
        return False


def _close_spread_inner(kite, store, trade, spot, reason, dry_run, label='BCS'):
    """Inner close logic, separated so close_spread() can wrap with try/except."""
    stock = trade['stock']
    short_sym = trade['short_symbol']
    long_sym = trade['long_symbol']
    qty = trade['quantity']
    exchange = trade['exchange']

    send_telegram(f"{label} {reason} TRIGGERED\n{stock} spot={spot}\nClosing spread...")

    # ── Pre-flight: Check actual position state ──────────────────────────
    if not dry_run:
        short_qty = get_net_position(kite, short_sym)
        long_qty = get_net_position(kite, long_sym)
    else:
        short_qty = -qty  # Assume normal state for dry run
        long_qty = qty

    log(f"  Position check: {short_sym} qty={short_qty} | {long_sym} qty={long_qty}")

    # ── Guard: Both legs already flat — mark closed with recovered fills ──
    if short_qty >= 0 and long_qty <= 0:
        log(f"\n  Both legs already flat (short={short_qty}, long={long_qty}).")
        log(f"  Trade was closed by another process or manually. Recovering fill prices...")
        short_fill = _find_last_fill_price(kite, short_sym, "BUY")
        long_fill = _find_last_fill_price(kite, long_sym, "SELL")
        entry_net = trade['net_debit']
        if short_fill > 0 and long_fill > 0:
            exit_net = long_fill - short_fill
        else:
            exit_net = 0.0
        exit_data = {
            'exit_date': datetime.now().isoformat(),
            'exit_reason': f"ALREADY_FLAT_{reason}",
            'exit_spot': spot,
            'short_fill': short_fill,
            'long_fill': long_fill,
            'exit_spread': exit_net,
            'pnl_per_share': exit_net - entry_net if exit_net else 0,
            'total_pnl': (exit_net - entry_net) * qty if exit_net else 0,
            'notes': 'Both legs flat when close triggered. Fills recovered from order history.',
        }
        if not dry_run:
            store.update_trade_exit(trade['id'], exit_data)
            log(f"  Trade #{trade['id']} marked closed (already flat)")
        send_telegram(f"{label} {stock}: {reason} triggered but both legs already flat. Marked closed.")
        return True

    short_fill = 0.0
    long_fill = 0.0
    short_closed_now = False
    long_closed_now = False

    # ── Step 1: Close SHORT leg (BUY back) — only if still short ─────────
    if short_qty < 0:
        close_qty = min(abs(short_qty), qty)
        log("")
        log(f"STEP 1: Close SHORT leg -> BUY {short_sym} x {close_qty}")
        log("-" * 55)
        short_result = close_leg(
            kite, exchange, short_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run
        )

        if not short_result or short_result['status'] not in ('COMPLETE', 'PARTIAL'):
            msg = f"CRITICAL: {stock} SHORT LEG CLOSE FAILED! Manual intervention needed."
            log("")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log("!!! CRITICAL: SHORT LEG CLOSE FAILED              !!!")
            log("!!! DO NOT close long leg manually - margin spike! !!!")
            log("!!! Intervene manually in Kite terminal            !!!")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            send_telegram(msg)
            if not dry_run:
                store.set_trade_status(trade['id'], 'partial_close',
                                       close_failed_leg='short', close_reason=reason)
            return False

        short_fill = short_result.get('average_price', 0)
        short_closed_now = True
        if short_result['status'] == 'PARTIAL':
            remaining = close_qty - short_result.get('filled_quantity', 0)
            log(f"  WARNING: Short leg partially filled. {remaining} qty still short!")
            send_telegram(f"{label} {stock}: Short leg partial fill. {remaining} still short!")
    else:
        log(f"\n  SHORT leg {short_sym} already flat/long (qty={short_qty}). Skipping BUY.")

    # ── Step 2: Close LONG leg (SELL) — only if still long ───────────────
    if long_qty > 0:
        close_qty = min(long_qty, qty)
        log("")
        log(f"STEP 2: Close LONG leg -> SELL {long_sym} x {close_qty}")
        log("-" * 55)
        long_result = close_leg(
            kite, exchange, long_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run
        )

        if not long_result or long_result['status'] not in ('COMPLETE', 'PARTIAL'):
            actual_close_qty = min(long_qty, qty)
            msg = (f"WARNING: {stock} LONG LEG CLOSE FAILED!\n"
                   f"Short is closed. Naked long {long_sym} remains.\n"
                   f"Close manually: SELL {actual_close_qty}")
            log("")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            log(f"!!! WARNING: LONG LEG CLOSE FAILED                !!!")
            log(f"!!! Short is closed - naked long remains           !!!")
            log(f"!!! Close {long_sym} manually (SELL {actual_close_qty}) !!!")
            log("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            send_telegram(msg)
            if not dry_run:
                store.set_trade_status(trade['id'], 'partial_close',
                                       close_failed_leg='long', close_reason=reason,
                                       short_fill=short_fill)
            return False

        long_fill = long_result.get('average_price', 0)
        long_closed_now = True
    else:
        log(f"\n  LONG leg {long_sym} already flat/short (qty={long_qty}). Skipping SELL.")

    # ── Summary ──────────────────────────────────────────────────────────
    entry_net = trade['net_debit']
    if short_closed_now and long_closed_now:
        exit_net = long_fill - short_fill
    elif long_closed_now and not short_closed_now:
        exit_net = long_fill
        log(f"  NOTE: Short was already flat. P&L is approximate (long fill only).")
    elif short_closed_now and not long_closed_now:
        exit_net = -short_fill
        log(f"  NOTE: Long was already flat. P&L is approximate (short fill only).")
    else:
        exit_net = 0.0
    pnl_per_share = exit_net - entry_net
    total_pnl = pnl_per_share * qty

    log("")
    log("=" * 70)
    log("  SPREAD CLOSED SUCCESSFULLY")
    log("=" * 70)
    log(f"  Reason:                 {reason}")
    log(f"  Short (BUY back) fill:  {short_fill}")
    log(f"  Long  (SELL)     fill:  {long_fill}")
    log(f"  Exit spread value:      {exit_net:.2f}/share")
    log(f"  Entry spread cost:      {entry_net:.2f}/share")
    log(f"  P&L per share:          {pnl_per_share:+.2f}")
    log(f"  Total P&L:              Rs {total_pnl:+,.0f}")
    log(f"  Spot at close:          {spot}")
    log("=" * 70)

    send_telegram(
        f"{label} CLOSED: {stock}\n"
        f"Reason: {reason}\n"
        f"Exit spread: {exit_net:.2f} | Entry: {entry_net:.2f}\n"
        f"P&L: Rs {total_pnl:+,.0f}\n"
        f"Spot: {spot}"
    )

    # ── Update trade store ───────────────────────────────────────────────
    exit_data = {
        'exit_date': datetime.now().isoformat(),
        'exit_reason': reason,
        'exit_spot': spot,
        'short_fill': short_fill,
        'long_fill': long_fill,
        'exit_spread': exit_net,
        'pnl_per_share': pnl_per_share,
        'total_pnl': total_pnl,
    }

    if not dry_run:
        store.update_trade_exit(trade['id'], exit_data)
        log(f"  Trade #{trade['id']} marked closed (local + Drive)")
    else:
        log(f"  [DRY RUN] Would mark trade #{trade['id']} closed")

    return True


# ── FH Close ────────────────────────────────────────────────────────────────

def close_fh_position(kite: KiteConnect, trade: dict, spot: float,
                      reason: str, dry_run: bool) -> bool:
    """
    Close a Fallen Hero position. Naked risk (short call) first.
    Updates FH TradeStore on success (local + Drive).
    Returns True if fully closed, False if any leg failed.

    Close order (naked risk first):
      1. BUY back short call (naked — most dangerous)
      2. SELL long call (if exists — hedge, no longer needed)
      3. BUY back short put
      4. SELL long put
    """
    fh_store = get_fh_store()
    stock = trade['stock']

    log("")
    log("=" * 70)
    log(f"  FH {reason} TRIGGERED! {stock} spot = {spot}")
    log(f"  Initiating FH close sequence...")
    log("=" * 70)

    # ── Late-day guard ────────────────────────────────────────────────────
    now_t = datetime.now().time()
    if now_t > LAST_ORDER_TIME:
        log(f"  LATE-DAY GUARD: {now_t.strftime('%H:%M')} > {LAST_ORDER_TIME.strftime('%H:%M')}.")
        log(f"  Too close to market close. Not placing orders — manual intervention needed.")
        send_telegram(
            f"FH {reason} TRIGGERED {stock} @ {spot}\n"
            f"BUT past {LAST_ORDER_TIME.strftime('%H:%M')} — NOT auto-closing.\n"
            f"Close manually in Kite!"
        )
        return False

    # ── Acquire close-lock ────────────────────────────────────────────────
    close_lock_acquired = False
    if not dry_run:
        if not fh_store.begin_close(trade['id'], reason):
            log(f"  Trade #{trade['id']} is already closing/closed. Skipping.")
            return True  # Not an error — another process has it
        close_lock_acquired = True

    try:
        return _close_fh_inner(kite, fh_store, trade, spot, reason, dry_run)
    except Exception as e:
        log(f"  EXCEPTION during close_fh_position: {e}")
        send_telegram(f"FH {stock}: EXCEPTION during {reason} close! {e}\nManual intervention needed!")
        if close_lock_acquired:
            try:
                fh_store.set_trade_status(trade['id'], 'partial_close',
                                          close_error=str(e), close_reason=reason)
            except Exception as inner_e:
                log(f"  Could not set partial_close status: {inner_e}")
                fh_store._sync_locked = False
        return False


def _close_fh_inner(kite, fh_store, trade, spot, reason, dry_run):
    """Inner FH close logic, separated so close_fh_position() can wrap with try/except."""
    stock = trade['stock']
    qty = trade['quantity']
    exchange = trade['exchange']

    sc_sym = trade['short_call_symbol']
    lc_sym = trade.get('long_call_symbol')  # Optional 4th leg
    sp_sym = trade['short_put_symbol']
    lp_sym = trade['long_put_symbol']

    send_telegram(f"FH {reason} TRIGGERED\n{stock} spot={spot}\nClosing position...")

    # ── Pre-flight: Check actual position state ──────────────────────────
    if not dry_run:
        sc_qty = get_net_position(kite, sc_sym)
        lc_qty = get_net_position(kite, lc_sym) if lc_sym else 0
        sp_qty = get_net_position(kite, sp_sym)
        lp_qty = get_net_position(kite, lp_sym)
    else:
        sc_qty = -qty
        lc_qty = qty if lc_sym else 0
        sp_qty = -qty
        lp_qty = qty

    log(f"  Positions: SC({sc_sym})={sc_qty} | LC({lc_sym or 'N/A'})={lc_qty} | SP({sp_sym})={sp_qty} | LP({lp_sym})={lp_qty}")

    # ── Guard: All legs already flat ─────────────────────────────────────
    all_flat = (sc_qty >= 0 and sp_qty >= 0 and lp_qty <= 0
                and (not lc_sym or lc_qty <= 0))
    if all_flat:
        log(f"\n  All legs already flat. Recovering fill prices...")
        sc_fill = _find_last_fill_price(kite, sc_sym, "BUY")
        lc_fill = _find_last_fill_price(kite, lc_sym, "SELL") if lc_sym else 0.0
        sp_fill = _find_last_fill_price(kite, sp_sym, "BUY")
        lp_fill = _find_last_fill_price(kite, lp_sym, "SELL")
        close_cost = sc_fill + sp_fill - lc_fill - lp_fill
        total_credit = trade['total_credit']
        pnl_per_share = total_credit - close_cost
        total_pnl = pnl_per_share * qty
        exit_data = {
            'exit_date': datetime.now().isoformat(),
            'exit_reason': f"ALREADY_FLAT_{reason}",
            'exit_spot': spot,
            'short_call_fill': sc_fill, 'long_call_fill': lc_fill,
            'short_put_fill': sp_fill, 'long_put_fill': lp_fill,
            'close_cost': close_cost,
            'pnl_per_share': pnl_per_share, 'total_pnl': total_pnl,
            'notes': 'All legs flat when close triggered. Fills recovered from order history.',
        }
        if not dry_run:
            fh_store.update_trade_exit(trade['id'], exit_data)
        send_telegram(f"FH {stock}: {reason} but all legs already flat. Marked closed.")
        return True

    fills = {'short_call': 0.0, 'long_call': 0.0, 'short_put': 0.0, 'long_put': 0.0}

    # ── Step 1: BUY back short call (naked risk — MOST DANGEROUS) ────────
    if sc_qty < 0:
        close_qty = min(abs(sc_qty), qty)
        log(f"\nSTEP 1: BUY back SHORT CALL {sc_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, sc_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log("!!! CRITICAL: SHORT CALL CLOSE FAILED — naked risk remains !!!")
            send_telegram(f"FH {stock}: SHORT CALL CLOSE FAILED! Naked risk! Manual intervention needed!")
            if not dry_run:
                fh_store.set_trade_status(trade['id'], 'partial_close',
                                          close_failed_leg='short_call', close_reason=reason)
            return False
        fills['short_call'] = result.get('average_price', 0)
    else:
        log(f"\n  SHORT CALL {sc_sym} already flat (qty={sc_qty}). Skipping.")

    # ── Step 2: SELL long call (if exists — hedge, no longer needed) ─────
    if lc_sym and lc_qty > 0:
        close_qty = min(lc_qty, qty)
        log(f"\nSTEP 2: SELL LONG CALL {lc_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, lc_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log(f"  WARNING: Long call close failed. Naked long remains — not critical.")
            send_telegram(f"FH {stock}: Long call sell failed. Manual sell {lc_sym}.")
        else:
            fills['long_call'] = result.get('average_price', 0)
    else:
        if lc_sym:
            log(f"\n  LONG CALL {lc_sym} already flat (qty={lc_qty}). Skipping.")

    # ── Step 3: BUY back short put ───────────────────────────────────────
    if sp_qty < 0:
        close_qty = min(abs(sp_qty), qty)
        log(f"\nSTEP 3: BUY back SHORT PUT {sp_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, sp_sym, "BUY", close_qty, is_buy=True, dry_run=dry_run)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log("!!! WARNING: SHORT PUT CLOSE FAILED !!!")
            send_telegram(f"FH {stock}: SHORT PUT CLOSE FAILED! Manual intervention needed!")
            if not dry_run:
                fh_store.set_trade_status(trade['id'], 'partial_close',
                                          close_failed_leg='short_put', close_reason=reason)
            return False
        fills['short_put'] = result.get('average_price', 0)
    else:
        log(f"\n  SHORT PUT {sp_sym} already flat (qty={sp_qty}). Skipping.")

    # ── Step 4: SELL long put ────────────────────────────────────────────
    if lp_qty > 0:
        close_qty = min(lp_qty, qty)
        log(f"\nSTEP 4: SELL LONG PUT {lp_sym} x {close_qty}")
        log("-" * 55)
        result = close_leg(kite, exchange, lp_sym, "SELL", close_qty, is_buy=False, dry_run=dry_run)
        if not result or result['status'] not in ('COMPLETE', 'PARTIAL'):
            log(f"  WARNING: Long put sell failed. Manual sell {lp_sym}.")
            send_telegram(f"FH {stock}: Long put sell failed. Manual sell {lp_sym}.")
        else:
            fills['long_put'] = result.get('average_price', 0)
    else:
        log(f"\n  LONG PUT {lp_sym} already flat (qty={lp_qty}). Skipping.")

    # ── Summary ──────────────────────────────────────────────────────────
    total_credit = trade['total_credit']
    close_cost = fills['short_call'] + fills['short_put'] - fills['long_call'] - fills['long_put']
    pnl_per_share = total_credit - close_cost
    total_pnl = pnl_per_share * qty

    log("")
    log("=" * 70)
    log("  FH POSITION CLOSED")
    log("=" * 70)
    log(f"  Reason:              {reason}")
    log(f"  Short Call fill:     {fills['short_call']:.2f} (BUY back)")
    log(f"  Long Call fill:      {fills['long_call']:.2f} (SELL)")
    log(f"  Short Put fill:      {fills['short_put']:.2f} (BUY back)")
    log(f"  Long Put fill:       {fills['long_put']:.2f} (SELL)")
    log(f"  Close cost/share:    {close_cost:.2f}")
    log(f"  Entry credit/share:  {total_credit:.2f}")
    log(f"  P&L per share:       {pnl_per_share:+.2f}")
    log(f"  Total P&L:           Rs {total_pnl:+,.0f}")
    log(f"  Spot at close:       {spot}")
    log("=" * 70)

    send_telegram(
        f"FH CLOSED: {stock}\n"
        f"Reason: {reason}\n"
        f"Credit: {total_credit:.2f} | Close cost: {close_cost:.2f}\n"
        f"P&L: Rs {total_pnl:+,.0f}\n"
        f"Spot: {spot}"
    )

    exit_data = {
        'exit_date': datetime.now().isoformat(),
        'exit_reason': reason,
        'exit_spot': spot,
        'short_call_fill': fills['short_call'],
        'long_call_fill': fills['long_call'],
        'short_put_fill': fills['short_put'],
        'long_put_fill': fills['long_put'],
        'close_cost': close_cost,
        'pnl_per_share': pnl_per_share,
        'total_pnl': total_pnl,
    }

    if not dry_run:
        fh_store.update_trade_exit(trade['id'], exit_data)
        log(f"  Trade #{trade['id']} marked closed (local + Drive)")
    else:
        log(f"  [DRY RUN] Would mark trade #{trade['id']} closed")

    return True


# ── Position Verification ────────────────────────────────────────────────────

def verify_positions(kite: KiteConnect, trade: dict, fatal: bool = True):
    """Confirm both legs exist in Kite before starting monitor.

    Args:
        fatal: If True (default, single-trade mode), sys.exit on missing.
               If False (cron mode), return None and let caller handle.
    """
    positions = kite.positions()['net']

    long_pos = None
    short_pos = None
    for p in positions:
        if p['tradingsymbol'] == trade['long_symbol'] and p['quantity'] > 0:
            long_pos = p
        elif p['tradingsymbol'] == trade['short_symbol'] and p['quantity'] < 0:
            short_pos = p

    if not long_pos:
        msg = f"Long position {trade['long_symbol']} not found in Kite!"
        if fatal:
            log(f"FATAL: {msg}")
            sys.exit(1)
        else:
            log(f"  WARNING: {msg}")
    if not short_pos:
        msg = f"Short position {trade['short_symbol']} not found in Kite!"
        if fatal:
            log(f"FATAL: {msg}")
            sys.exit(1)
        else:
            log(f"  WARNING: {msg}")

    if long_pos and short_pos:
        log(f"  Long:  {trade['long_symbol']}  qty={long_pos['quantity']}  avg={long_pos['average_price']}")
        log(f"  Short: {trade['short_symbol']}  qty={short_pos['quantity']}  avg={short_pos['average_price']}")

    return long_pos, short_pos


# ── Main Monitor Loop ────────────────────────────────────────────────────────

def monitor(kite: KiteConnect, trade: dict, target: float,
            sl_spot: float, sl_spread: float, dry_run: bool,
            cli_target: Optional[float] = None,
            cli_sl_spot: Optional[float] = None,
            cli_sl_spread: Optional[float] = None):
    """
    Main monitoring loop with TP + 3-layer SL.

    Check order each cycle:
      1. SL_SPOT    — spot <= sl_spot
      2. SL_SPREAD  — spread_value <= sl_spread
      3. SL_TRAIL   — auto-engages at 2x debit, trails 60% of peak
      4. TP         — spot >= target

    cli_target/cli_sl_spot/cli_sl_spread: If set, these CLI overrides take
    precedence over trade store values. If None, values refresh from store
    on each Drive sync.
    """
    store = get_store()
    stock = trade['stock']
    set_log_file(LOG_DIR / f"spread_monitor_{stock}_{date.today().strftime('%Y%m%d')}.log")

    entry_net = trade['net_debit']
    trail_engage_level = entry_net * TRAIL_ENGAGE_MULTIPLIER

    # Restore trailing SL state from trade store (survives process restarts)
    peak_spread = trade.get('trail_peak', 0.0)
    trailing_sl = trade.get('trail_sl', 0.0)
    trail_active = trade.get('trail_active', False)
    if trail_active:
        log(f"  Restored trail state: peak={peak_spread:.2f}, trail={trailing_sl:.2f}")

    log("")
    log("=" * 70)
    log(f"  {stock} SPREAD MONITOR")
    log("=" * 70)
    log(f"  Position: Bull Call Spread")
    log(f"  Long:     {trade['long_symbol']} x {trade['quantity']}")
    log(f"  Short:    {trade['short_symbol']} x {trade['quantity']}")
    log(f"  Entry:    Long @ {trade['entry_long_price']} | Short @ {trade['entry_short_price']} | Net: {entry_net:.2f}")
    log(f"  Qty:      {trade['quantity']} ({trade.get('lots', '?')} lots x {trade.get('lot_size', '?')})")
    log(f"  Target:   {stock} >= {target}")
    log(f"  SL Spot:  {stock} <= {sl_spot}")
    log(f"  SL Spread:<= {sl_spread:.2f}")
    log(f"  SL Trail: Engages at spread >= {trail_engage_level:.2f} (2x debit), trails 60% of peak")
    log(f"  Mode:     {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    log(f"  Poll:     Every {POLL_INTERVAL_SEC}s | Status every {STATUS_PRINT_INTERVAL_SEC}s")
    log("=" * 70)

    log("\nVerifying positions in Kite...")
    verify_positions(kite, trade)

    spot = get_spot(kite, trade['spot_symbol'])
    log(f"\nCurrent spot: {spot:.2f} | Gap to target: {target - spot:+.2f} | Buffer above SL: {spot - sl_spot:+.2f}")

    # Immediate checks — only when market has settled (not during open buffer)
    if is_market_settled():
        if spot <= sl_spot:
            log(f"Spot already at/below SL!")
            close_spread(kite, trade, spot, "SL_SPOT", dry_run)
            return

        if spot >= target:
            log(f"Spot already at/above target!")
            close_spread(kite, trade, spot, "TP", dry_run)
            return
    else:
        log(f"\n  Skipping immediate checks — market-open buffer active")

    # ── Expiry day warning ────────────────────────────────────────────────
    expiry_today = is_expiry_day(trade)
    if expiry_today:
        log(f"\n  *** EXPIRY DAY! Will force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
        send_telegram(f"BCS {stock}: EXPIRY DAY. Monitor will force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")

    log(f"\nMonitoring started. Waiting for TP={target} | SL={sl_spot}...\n")
    if not is_market_settled():
        log(f"  Market-open buffer active: SL/TP checks delayed until {MARKET_OPEN_BUFFER_SEC}s after open")

    last_status_time = 0
    consecutive_errors = 0

    while True:
        try:
            if not is_market_open():
                now_t = datetime.now().time()
                if now_t > MARKET_CLOSE:
                    log("Market closed for the day. Exiting monitor.")
                    if trail_active:
                        log(f"  Trailing SL state: peak={peak_spread:.2f}, trail={trailing_sl:.2f}")
                    return
                # Before market open - wait
                log(f"Market not open yet ({now_t.strftime('%H:%M')}). Waiting...")
                time.sleep(30)
                continue

            # Periodic Drive sync (picks up manual trade edits)
            store.maybe_sync()

            # Re-read trade from store (picks up SL/TP edits from other machines)
            fresh = store.find_open_trade(stock, trade['id'])
            if fresh is None:
                log("Trade no longer open (closed/edited from another machine?). Exiting.")
                return
            trade = fresh
            # Refresh SL/TP from store unless CLI overrode them
            if cli_target is None:
                target = trade['target_spot']
            if cli_sl_spot is None:
                sl_spot = trade['sl_spot']
            if cli_sl_spread is None:
                sl_spread = trade['sl_spread']

            spot = get_spot(kite, trade['spot_symbol'])
            now = time.time()
            consecutive_errors = 0  # Reset on successful API call

            # Fetch spread for SL checks and status
            spread_data = None
            spread_val = None
            try:
                spread_data = get_spread_value(kite, trade)
                spread_val = spread_data['spread']
            except Exception:
                pass  # spread fetch can fail; spot-based checks still work

            # ── Update trailing SL state ─────────────────────────────────
            # Only when market is settled. During cooldown, spread values can
            # be aberrant (wide bid-ask) → fake peak → immediate SL_TRAIL
            # after cooldown ends.
            settled = is_market_settled()
            if settled and spread_val is not None:
                if not trail_active and spread_val >= trail_engage_level:
                    trail_active = True
                    peak_spread = spread_val
                    trailing_sl = peak_spread * TRAIL_PERCENT
                    log(f"  ** TRAILING SL ENGAGED ** spread={spread_val:.2f} >= {trail_engage_level:.2f}")
                    log(f"     Peak: {peak_spread:.2f} | Trail level: {trailing_sl:.2f}")
                    store.update_trade_fields(trade['id'],
                                              trail_active=True, trail_peak=peak_spread, trail_sl=trailing_sl)

                if trail_active and spread_val > peak_spread:
                    peak_spread = spread_val
                    trailing_sl = peak_spread * TRAIL_PERCENT
                    log(f"  ** TRAIL UPDATED ** Peak: {peak_spread:.2f} | Trail: {trailing_sl:.2f}")
                    store.update_trade_fields(trade['id'],
                                              trail_peak=peak_spread, trail_sl=trailing_sl)

            # ── Periodic status line ─────────────────────────────────────
            if now - last_status_time >= STATUS_PRINT_INTERVAL_SEC:
                settle_tag = "" if settled else " [COOLDOWN]"
                expiry_tag = " [EXPIRY]" if expiry_today else ""
                try:
                    if spread_data and spread_val is not None:
                        unrealized = (spread_val - entry_net) * trade['quantity']
                        trail_str = f" | Trail: {trailing_sl:.2f}" if trail_active else ""
                        log(
                            f"Spot: {spot:>8.2f} | "
                            f"TP: {target} (gap: {target - spot:>+.2f}) | "
                            f"SL: {sl_spot} (buf: {spot - sl_spot:>+.2f}) | "
                            f"Spread: {spread_val:>6.2f} "
                            f"(L:{spread_data['long']['bid']} S:{spread_data['short']['ask']}) | "
                            f"P&L: Rs {unrealized:>+,.0f}{trail_str}{settle_tag}{expiry_tag}"
                        )
                    else:
                        log(f"Spot: {spot:>8.2f} | TP: {target} (gap: {target - spot:>+.2f}) | SL: {sl_spot} (buf: {spot - sl_spot:>+.2f}){settle_tag}{expiry_tag}")
                except Exception:
                    log(f"Spot: {spot:>8.2f} | TP: {target} | SL: {sl_spot}{settle_tag}{expiry_tag}")
                last_status_time = now

            # ── EXPIRY DAY: Force close by EXPIRY_FORCE_CLOSE_TIME ──────
            if expiry_today and datetime.now().time() >= EXPIRY_FORCE_CLOSE_TIME:
                log(f"\n  *** EXPIRY FORCE CLOSE: {datetime.now().strftime('%H:%M')} >= {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
                success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run)
                if success:
                    log("\nMonitor complete. Position closed (expiry day force close).")
                else:
                    log("\nExpiry force close FAILED. CHECK POSITION MANUALLY!")
                return

            # ── CHECK 1: SL_SPOT — always active, even during cooldown ────
            if spot <= sl_spot:
                log(f"\n  *** SL_SPOT HIT: {spot:.2f} <= {sl_spot} ***")
                success = close_spread(kite, trade, spot, "SL_SPOT", dry_run)
                if success:
                    log("\nMonitor complete. Position closed on SL_SPOT.")
                else:
                    log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                return

            # ── Cooldown gate: SL_SPREAD, SL_TRAIL, TP need settled market ──
            if not settled:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            # ── CHECK 2: SL_SPREAD ───────────────────────────────────────
            if spread_val is not None and spread_val <= sl_spread:
                log(f"\n  *** SL_SPREAD HIT: {spread_val:.2f} <= {sl_spread:.2f} ***")
                success = close_spread(kite, trade, spot, "SL_SPREAD", dry_run)
                if success:
                    log("\nMonitor complete. Position closed on SL_SPREAD.")
                else:
                    log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                return

            # ── CHECK 3: SL_TRAIL ────────────────────────────────────────
            if trail_active and spread_val is not None and spread_val <= trailing_sl:
                log(f"\n  *** SL_TRAIL HIT: {spread_val:.2f} <= {trailing_sl:.2f} (peak was {peak_spread:.2f}) ***")
                success = close_spread(kite, trade, spot, "SL_TRAIL", dry_run)
                if success:
                    log("\nMonitor complete. Position closed on SL_TRAIL.")
                else:
                    log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                return

            # ── CHECK 4: TP ──────────────────────────────────────────────
            if spot >= target:
                success = close_spread(kite, trade, spot, "TP", dry_run)
                if success:
                    log("\nMonitor complete. Position closed on TP.")
                else:
                    log("\nMonitor stopped. CHECK POSITION MANUALLY!")
                return

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            log("\nMonitor stopped by user (Ctrl+C)")
            log("Position is still OPEN.")
            if trail_active:
                log(f"  Trailing SL state: peak={peak_spread:.2f}, trail={trailing_sl:.2f}")
            return
        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            # Check for token expiry (fatal — can't recover without new token)
            err_str = str(e).lower()
            if 'token' in err_str or 'invalidtoken' in err_str or 'sessionexpired' in err_str:
                log("FATAL: Kite token appears expired. Cannot continue.")
                send_telegram(f"BCS MONITOR FATAL: Kite token expired! {stock} is UNMONITORED.")
                return
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting.")
                send_telegram(f"BCS MONITOR FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. {stock} is UNMONITORED!")
                return
            if consecutive_errors == 10:
                send_telegram(f"BCS MONITOR WARNING: 10 consecutive errors for {stock}. Last: {e}")
            time.sleep(10)


# ── Cron Mode (All Open Trades — BCS + FH) ──────────────────────────────────

def _load_all_trades(bcs_store, fh_store, bps_store=None) -> list:
    """Load and tag open trades from all stores.

    Returns shallow copies with _strategy/_store_type tags to avoid
    polluting the store's in-memory trade dicts (which get persisted).
    """
    all_trades = []
    for t in bcs_store.get_open_trades():
        tagged = dict(t)
        tagged['_strategy'] = 'BCS'
        tagged['_store_type'] = 'bcs'
        all_trades.append(tagged)
    if bps_store:
        for t in bps_store.get_open_trades():
            tagged = dict(t)
            tagged['_strategy'] = 'BPS'
            tagged['_store_type'] = 'bps'
            all_trades.append(tagged)
    for t in fh_store.get_open_trades():
        tagged = dict(t)
        tagged['_strategy'] = 'FH'
        tagged['_store_type'] = 'fh'
        all_trades.append(tagged)
    return all_trades


def _get_store_for(trade, bcs_store, fh_store, bps_store=None):
    """Return the correct store for a trade based on its _store_type tag."""
    st = trade.get('_store_type')
    if st == 'fh':
        return fh_store
    if st == 'bps' and bps_store:
        return bps_store
    return bcs_store


def monitor_all(kite: KiteConnect, dry_run: bool):
    """
    Monitor ALL open trades (BCS + BPS + Fallen Hero) in a single loop.
    Designed to be run as a scheduled task during market hours.

    BCS: 4-layer SL (SL_SPOT <=, SL_SPREAD, SL_TRAIL, TP >=)
    BPS: 4-layer SL (SL_SPOT >=, SL_SPREAD, SL_TRAIL, TP <=) — REVERSED direction
    FH:  spot-only SL (SL_SPOT >=) + expiry force-close
    """
    bcs_store = get_store()
    fh_store = get_fh_store()
    bps_store = get_bps_store()
    wl_store = get_watchlist_store()
    set_log_file(LOG_DIR / f"spread_monitor_cron_{date.today().strftime('%Y%m%d')}.log")

    # ── Recover trades stuck in 'closing' from a previous crash ─────────
    for label, store in [('BCS', bcs_store), ('BPS', bps_store), ('FH', fh_store)]:
        for t in store.get_closing_trades():
            log(f"  RECOVERY: {label} #{t['id']} {t['stock']} stuck in 'closing'. Resetting to 'open'.")
            send_telegram(f"{label} #{t['id']} {t['stock']}: Recovered from 'closing' (previous crash). Re-monitoring.")
            store.recover_closing_trade(t['id'])

    all_trades = _load_all_trades(bcs_store, fh_store, bps_store)
    wl_active = wl_store.get_active()

    if not all_trades and not wl_active:
        log("No open trades and no active watchlist alerts. Nothing to monitor.")
        return

    bcs_count = sum(1 for t in all_trades if t['_strategy'] == 'BCS')
    bps_count = sum(1 for t in all_trades if t['_strategy'] == 'BPS')
    fh_count = sum(1 for t in all_trades if t['_strategy'] == 'FH')
    wl_count = len(wl_active)

    log("")
    log("=" * 70)
    log("  SPREAD MONITOR — ALL OPEN TRADES (BCS + BPS + FH) + WATCHLIST")
    log("=" * 70)
    log(f"  Mode:   {'DRY RUN' if dry_run else 'LIVE EXECUTION'}")
    log(f"  Trades: {len(all_trades)} open (BCS: {bcs_count}, BPS: {bps_count}, FH: {fh_count})")
    log(f"  Watchlist: {wl_count} active alert(s)")
    log(f"  Drive:  BCS={'on' if bcs_store._drive_enabled else 'off'} | BPS={'on' if bps_store._drive_enabled else 'off'} | FH={'on' if fh_store._drive_enabled else 'off'} | WL={'on' if wl_store._drive_enabled else 'off'}")
    log(f"  Poll:   Every {POLL_INTERVAL_SEC}s | Status every {STATUS_PRINT_INTERVAL_SEC}s")
    log("=" * 70)

    # Per-trade trailing SL state (BCS only): {(strategy, trade_id): {peak, trail, active}}
    trail_state = {}
    for t in all_trades:
        strat = t['_strategy']
        lots_str = f"{t.get('lots', '?')}x{t.get('lot_size', '?')}"

        if strat == 'BCS':
            entry_net = t['net_debit']
            trail_state[('BCS', t['id'])] = {
                'peak': t.get('trail_peak', 0.0),
                'trail': t.get('trail_sl', 0.0),
                'active': t.get('trail_active', False),
                'engage_level': entry_net * TRAIL_ENGAGE_MULTIPLIER,
            }
            if trail_state[('BCS', t['id'])]['active']:
                log(f"  BCS #{t['id']} {t['stock']}: Restored trail: peak={t.get('trail_peak', 0):.2f}, trail={t.get('trail_sl', 0):.2f}")
            log(f"  BCS #{t['id']} {t['stock']} {t['long_symbol']}/{t['short_symbol']} "
                f"| Lots: {lots_str} "
                f"| TP: {t['target_spot']} | SL: {t['sl_spot']} | SL Spread: {t['sl_spread']:.2f}")
        elif strat == 'BPS':
            entry_net = t['net_debit']
            trail_state[('BPS', t['id'])] = {
                'peak': t.get('trail_peak', 0.0),
                'trail': t.get('trail_sl', 0.0),
                'active': t.get('trail_active', False),
                'engage_level': entry_net * TRAIL_ENGAGE_MULTIPLIER,
            }
            if trail_state[('BPS', t['id'])]['active']:
                log(f"  BPS #{t['id']} {t['stock']}: Restored trail: peak={t.get('trail_peak', 0):.2f}, trail={t.get('trail_sl', 0):.2f}")
            log(f"  BPS #{t['id']} {t['stock']} {t['long_symbol']}/{t['short_symbol']} "
                f"| Lots: {lots_str} "
                f"| TP: spot<={t['target_spot']} | SL: spot>={t['sl_spot']} | SL Spread: {t['sl_spread']:.2f}")
        else:
            # FH: credit strategy, spot-only SL (reversed direction)
            log(f"  FH  #{t['id']} {t['stock']} SC:{t['short_call_symbol']} SP:{t['short_put_symbol']} "
                f"LP:{t['long_put_symbol']}"
                f"{' LC:' + t['long_call_symbol'] if t.get('long_call_symbol') else ''} "
                f"| Lots: {lots_str} "
                f"| SL: spot>={t['sl_spot']} | BE: {t['breakeven']} | Credit: {t['total_credit']:.2f}")

    log("")
    log("Verifying all positions in Kite...")
    positions = kite.positions()['net']
    for t in all_trades:
        strat = t['_strategy']
        if strat == 'BCS':
            long_found = any(p['tradingsymbol'] == t['long_symbol'] and p['quantity'] > 0 for p in positions)
            short_found = any(p['tradingsymbol'] == t['short_symbol'] and p['quantity'] < 0 for p in positions)
            if not long_found or not short_found:
                log(f"  WARNING: BCS #{t['id']} {t['stock']} — positions missing! "
                    f"(long={'OK' if long_found else 'MISSING'}, short={'OK' if short_found else 'MISSING'})")
            else:
                log(f"  BCS #{t['id']} {t['stock']} — positions verified")
        elif strat == 'BPS':
            long_found = any(p['tradingsymbol'] == t['long_symbol'] and p['quantity'] > 0 for p in positions)
            short_found = any(p['tradingsymbol'] == t['short_symbol'] and p['quantity'] < 0 for p in positions)
            if not long_found or not short_found:
                log(f"  WARNING: BPS #{t['id']} {t['stock']} — positions missing! "
                    f"(long={'OK' if long_found else 'MISSING'}, short={'OK' if short_found else 'MISSING'})")
            else:
                log(f"  BPS #{t['id']} {t['stock']} — positions verified")
        else:
            # FH: verify 3-4 legs
            sc_ok = any(p['tradingsymbol'] == t['short_call_symbol'] and p['quantity'] < 0 for p in positions)
            sp_ok = any(p['tradingsymbol'] == t['short_put_symbol'] and p['quantity'] < 0 for p in positions)
            lp_ok = any(p['tradingsymbol'] == t['long_put_symbol'] and p['quantity'] > 0 for p in positions)
            lc_ok = True
            if t.get('long_call_symbol'):
                lc_ok = any(p['tradingsymbol'] == t['long_call_symbol'] and p['quantity'] > 0 for p in positions)
            missing = []
            if not sc_ok: missing.append('SC')
            if not sp_ok: missing.append('SP')
            if not lp_ok: missing.append('LP')
            if not lc_ok: missing.append('LC')
            if missing:
                log(f"  WARNING: FH #{t['id']} {t['stock']} — legs missing: {', '.join(missing)}")
            else:
                log(f"  FH  #{t['id']} {t['stock']} — all legs verified")

    # ── Expiry day warnings ──────────────────────────────────────────────
    expiry_trades = {}  # {(strategy, trade_id): True}
    for t in all_trades:
        if is_expiry_day(t):
            strat = t['_strategy']
            expiry_trades[(strat, t['id'])] = True
            log(f"  *** {strat} #{t['id']} {t['stock']}: EXPIRY DAY! Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
            send_telegram(f"{strat} #{t['id']} {t['stock']}: EXPIRY DAY. Will force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")

    log(f"\nCron monitoring started at {datetime.now().strftime('%H:%M:%S')}...")
    if not is_market_settled():
        log(f"  Market-open buffer active: SL/TP checks delayed until {MARKET_OPEN_BUFFER_SEC}s after open")
    log("")

    last_status_time = 0
    closing_in_progress = {}  # {(strategy, trade_id): reason}
    consecutive_errors = 0

    while True:
        try:
            if not is_market_open():
                now_t = datetime.now().time()
                if now_t > MARKET_CLOSE:
                    log("Market closed for the day. Cron monitor exiting.")
                    for (strat_key, tid_key), ts in trail_state.items():
                        if ts['active']:
                            log(f"  {strat_key} #{tid_key} trail state: peak={ts['peak']:.2f}, trail={ts['trail']:.2f}")
                    return
                log(f"Market not open yet ({now_t.strftime('%H:%M')}). Waiting...")
                time.sleep(30)
                continue

            # Periodic Drive sync (picks up trades added from other machines)
            bcs_store.maybe_sync()
            bps_store.maybe_sync()
            fh_store.maybe_sync()
            wl_store.maybe_sync()
            all_trades = _load_all_trades(bcs_store, fh_store, bps_store)
            wl_active = wl_store.get_active()

            if not all_trades and not wl_active:
                log("All trades closed and no active watchlist alerts. Cron monitor exiting.")
                return

            settled = is_market_settled()
            consecutive_errors = 0  # Reset on successful iteration
            now = time.time()
            print_status = (now - last_status_time >= STATUS_PRINT_INTERVAL_SEC)

            for trade in all_trades:
                tid = trade['id']
                stock = trade['stock']
                strat = trade['_strategy']
                sl_spot_val = trade['sl_spot']
                trade_store = _get_store_for(trade, bcs_store, fh_store, bps_store)

                # Skip trades where close is already in progress
                # Use (strategy, id) as key since BCS and FH IDs are independent
                close_key = (strat, tid)
                if close_key in closing_in_progress:
                    if print_status:
                        log(f"  {strat} #{tid} {stock}: CLOSE IN PROGRESS ({closing_in_progress[close_key]}). Skipping.")
                    continue

                # ── BCS/BPS-specific fields ────────────────────────────────
                if strat == 'BCS':
                    target = trade['target_spot']
                    sl_spread_val = trade['sl_spread']
                    entry_net = trade['net_debit']

                    # Initialize trail state for new BCS trades added mid-session
                    if close_key not in trail_state:
                        trail_state[close_key] = {
                            'peak': 0.0, 'trail': 0.0, 'active': False,
                            'engage_level': entry_net * TRAIL_ENGAGE_MULTIPLIER,
                        }
                        if is_expiry_day(trade):
                            expiry_trades[close_key] = True
                            log(f"  BCS #{tid} {stock}: EXPIRY DAY (added mid-session)")
                            send_telegram(f"BCS #{tid} {stock}: EXPIRY DAY (added mid-session). Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")
                elif strat == 'BPS':
                    target = trade['target_spot']
                    sl_spread_val = trade['sl_spread']
                    entry_net = trade['net_debit']

                    # Initialize trail state for new BPS trades added mid-session
                    if close_key not in trail_state:
                        trail_state[close_key] = {
                            'peak': 0.0, 'trail': 0.0, 'active': False,
                            'engage_level': entry_net * TRAIL_ENGAGE_MULTIPLIER,
                        }
                        if is_expiry_day(trade):
                            expiry_trades[close_key] = True
                            log(f"  BPS #{tid} {stock}: EXPIRY DAY (added mid-session)")
                            send_telegram(f"BPS #{tid} {stock}: EXPIRY DAY (added mid-session). Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")
                else:
                    # FH: check for new expiry-day trades added mid-session
                    if close_key not in expiry_trades and is_expiry_day(trade):
                        expiry_trades[close_key] = True
                        log(f"  FH #{tid} {stock}: EXPIRY DAY (added mid-session)")
                        send_telegram(f"FH #{tid} {stock}: EXPIRY DAY (added mid-session). Force-close by {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')}.")

                try:
                    spot = get_spot(kite, trade['spot_symbol'])
                except Exception as e:
                    log(f"  {strat} #{tid} {stock}: spot fetch failed: {e}")
                    continue

                # ── BCS/BPS: Fetch spread + update trailing SL ────────────
                spread_val = None
                spread_data = None
                fh_val = None
                if strat == 'BCS':
                    try:
                        spread_data = get_spread_value(kite, trade)
                        spread_val = spread_data['spread']
                    except Exception:
                        pass

                    ts = trail_state[close_key]
                    if settled and spread_val is not None:
                        if not ts['active'] and spread_val >= ts['engage_level']:
                            ts['active'] = True
                            ts['peak'] = spread_val
                            ts['trail'] = ts['peak'] * TRAIL_PERCENT
                            log(f"  BCS #{tid} {stock} ** TRAIL ENGAGED ** spread={spread_val:.2f} | trail={ts['trail']:.2f}")
                            trade_store.update_trade_fields(tid,
                                                            trail_active=True, trail_peak=ts['peak'], trail_sl=ts['trail'])

                        if ts['active'] and spread_val > ts['peak']:
                            ts['peak'] = spread_val
                            ts['trail'] = ts['peak'] * TRAIL_PERCENT
                            log(f"  BCS #{tid} {stock} ** TRAIL UPDATED ** peak={ts['peak']:.2f} | trail={ts['trail']:.2f}")
                            trade_store.update_trade_fields(tid,
                                                            trail_peak=ts['peak'], trail_sl=ts['trail'])
                elif strat == 'BPS':
                    try:
                        spread_data = get_spread_value(kite, trade)
                        spread_val = spread_data['spread']
                    except Exception:
                        pass

                    ts = trail_state[close_key]
                    if settled and spread_val is not None:
                        if not ts['active'] and spread_val >= ts['engage_level']:
                            ts['active'] = True
                            ts['peak'] = spread_val
                            ts['trail'] = ts['peak'] * TRAIL_PERCENT
                            log(f"  BPS #{tid} {stock} ** TRAIL ENGAGED ** spread={spread_val:.2f} | trail={ts['trail']:.2f}")
                            trade_store.update_trade_fields(tid,
                                                            trail_active=True, trail_peak=ts['peak'], trail_sl=ts['trail'])

                        if ts['active'] and spread_val > ts['peak']:
                            ts['peak'] = spread_val
                            ts['trail'] = ts['peak'] * TRAIL_PERCENT
                            log(f"  BPS #{tid} {stock} ** TRAIL UPDATED ** peak={ts['peak']:.2f} | trail={ts['trail']:.2f}")
                            trade_store.update_trade_fields(tid,
                                                            trail_peak=ts['peak'], trail_sl=ts['trail'])
                else:
                    # FH: Fetch position value for status display only
                    try:
                        fh_val = get_fh_position_value(kite, trade)
                    except Exception:
                        pass

                # ── Status line ───────────────────────────────────────────
                if print_status:
                    settle_tag = "" if settled else " [COOLDOWN]"
                    expiry_tag = " [EXPIRY]" if close_key in expiry_trades else ""
                    try:
                        if strat == 'BCS':
                            if spread_data and spread_val is not None:
                                unrealized = (spread_val - entry_net) * trade['quantity']
                                ts = trail_state[close_key]
                                trail_str = f" | Trail: {ts['trail']:.2f}" if ts['active'] else ""
                                log(
                                    f"  BCS #{tid} {stock}: Spot={spot:.2f} | "
                                    f"TP:{target}({target - spot:>+.1f}) | "
                                    f"SL:{sl_spot_val}({spot - sl_spot_val:>+.1f}) | "
                                    f"Spread:{spread_val:.2f} | "
                                    f"P&L: Rs {unrealized:>+,.0f}{trail_str}{settle_tag}{expiry_tag}"
                                )
                            else:
                                log(f"  BCS #{tid} {stock}: Spot={spot:.2f} | TP:{target}({target - spot:>+.1f}) | SL:{sl_spot_val}({spot - sl_spot_val:>+.1f}){settle_tag}{expiry_tag}")
                        elif strat == 'BPS':
                            # BPS status: REVERSED — gap to target = spot - target (positive = above target = not yet profitable)
                            # Buffer above SL = sl_spot - spot (positive = below SL = safe)
                            gap_to_target = spot - target  # positive when above target
                            buf_above_sl = sl_spot_val - spot  # positive when below SL = safe
                            if spread_data and spread_val is not None:
                                unrealized = (spread_val - entry_net) * trade['quantity']
                                ts = trail_state[close_key]
                                trail_str = f" | Trail: {ts['trail']:.2f}" if ts['active'] else ""
                                log(
                                    f"  BPS #{tid} {stock}: Spot={spot:.2f} | "
                                    f"TP:{target}(gap:{gap_to_target:>+.1f}) | "
                                    f"SL:{sl_spot_val}(buf:{buf_above_sl:>+.1f}) | "
                                    f"Spread:{spread_val:.2f} | "
                                    f"P&L: Rs {unrealized:>+,.0f}{trail_str}{settle_tag}{expiry_tag}"
                                )
                            else:
                                log(f"  BPS #{tid} {stock}: Spot={spot:.2f} | TP:{target}(gap:{gap_to_target:>+.1f}) | SL:{sl_spot_val}(buf:{buf_above_sl:>+.1f}){settle_tag}{expiry_tag}")
                        else:
                            # FH status: spot vs SL (upside), breakeven, P&L
                            be = trade['breakeven']
                            buf_to_sl = sl_spot_val - spot  # positive = safe buffer
                            if fh_val and fh_val['pnl_per_share'] is not None:
                                unrealized = fh_val['pnl_per_share'] * trade['quantity']
                                log(
                                    f"  FH  #{tid} {stock}: Spot={spot:.2f} | "
                                    f"SL:{sl_spot_val}(buf:{buf_to_sl:>+.1f}) | "
                                    f"BE:{be} | "
                                    f"P&L: Rs {unrealized:>+,.0f}{settle_tag}{expiry_tag}"
                                )
                            else:
                                log(f"  FH  #{tid} {stock}: Spot={spot:.2f} | SL:{sl_spot_val}(buf:{buf_to_sl:>+.1f}) | BE:{be}{settle_tag}{expiry_tag}")
                    except Exception:
                        log(f"  {strat} #{tid} {stock}: Spot={spot:.2f}{settle_tag}")

                # ── EXPIRY DAY: Force close by EXPIRY_FORCE_CLOSE_TIME ──
                if close_key in expiry_trades and datetime.now().time() >= EXPIRY_FORCE_CLOSE_TIME:
                    if close_key not in closing_in_progress:
                        log(f"\n  {strat} #{tid} {stock} *** EXPIRY FORCE CLOSE: {datetime.now().strftime('%H:%M')} >= {EXPIRY_FORCE_CLOSE_TIME.strftime('%H:%M')} ***")
                        closing_in_progress[close_key] = "EXPIRY_FORCE_CLOSE"
                        if strat == 'BCS':
                            success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run,
                                                   store=trade_store, strategy_label='BCS')
                        elif strat == 'BPS':
                            success = close_spread(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run,
                                                   store=trade_store, strategy_label='BPS')
                        else:
                            success = close_fh_position(kite, trade, spot, "EXPIRY_FORCE_CLOSE", dry_run)
                        if not success:
                            log(f"  {strat} #{tid} {stock}: Expiry force close FAILED. Manual intervention needed!")
                            send_telegram(f"{strat} #{tid} {stock}: EXPIRY FORCE CLOSE FAILED! Manual intervention needed!")
                        trail_state.pop(close_key, None)
                    continue

                # ── SL/TP checks ─────────────────────────────────────────
                closed = False

                if strat == 'BCS':
                    # ── BCS SL_SPOT: spot <= sl_spot (bearish risk) ──────
                    if spot <= sl_spot_val:
                        log(f"\n  BCS #{tid} {stock} *** SL_SPOT HIT: {spot:.2f} <= {sl_spot_val} ***")
                        closing_in_progress[close_key] = "SL_SPOT"
                        success = close_spread(kite, trade, spot, "SL_SPOT", dry_run,
                                               store=trade_store, strategy_label='BCS')
                        closed = True
                        if not success:
                            log(f"  BCS #{tid} {stock}: Close failed. Trade locked — manual intervention needed.")
                            send_telegram(f"BCS #{tid} {stock}: SL_SPOT close FAILED. Manual intervention needed!")

                    # Cooldown gate: SL_SPREAD, SL_TRAIL, TP need settled market
                    if not closed and not settled:
                        continue

                    # CHECK 2: SL_SPREAD
                    if not closed and spread_val is not None and spread_val <= sl_spread_val:
                        log(f"\n  BCS #{tid} {stock} *** SL_SPREAD HIT: {spread_val:.2f} <= {sl_spread_val:.2f} ***")
                        closing_in_progress[close_key] = "SL_SPREAD"
                        success = close_spread(kite, trade, spot, "SL_SPREAD", dry_run,
                                               store=trade_store, strategy_label='BCS')
                        closed = True
                        if not success:
                            log(f"  BCS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BCS #{tid} {stock}: SL_SPREAD close FAILED. Manual intervention needed!")

                    # CHECK 3: SL_TRAIL
                    ts = trail_state[close_key]
                    if not closed and ts['active'] and spread_val is not None and spread_val <= ts['trail']:
                        log(f"\n  BCS #{tid} {stock} *** SL_TRAIL HIT: {spread_val:.2f} <= {ts['trail']:.2f} ***")
                        closing_in_progress[close_key] = "SL_TRAIL"
                        success = close_spread(kite, trade, spot, "SL_TRAIL", dry_run,
                                               store=trade_store, strategy_label='BCS')
                        closed = True
                        if not success:
                            log(f"  BCS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BCS #{tid} {stock}: SL_TRAIL close FAILED. Manual intervention needed!")

                    # CHECK 4: TP
                    if not closed and spot >= target:
                        log(f"\n  BCS #{tid} {stock} *** TP HIT: {spot:.2f} >= {target} ***")
                        closing_in_progress[close_key] = "TP"
                        success = close_spread(kite, trade, spot, "TP", dry_run,
                                               store=trade_store, strategy_label='BCS')
                        closed = True
                        if not success:
                            log(f"  BCS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BCS #{tid} {stock}: TP close FAILED. Manual intervention needed!")

                elif strat == 'BPS':
                    # ── BPS SL_SPOT: spot >= sl_spot (bullish risk — stock rising is BAD) ──
                    if spot >= sl_spot_val:
                        log(f"\n  BPS #{tid} {stock} *** SL_SPOT HIT: {spot:.2f} >= {sl_spot_val} ***")
                        closing_in_progress[close_key] = "SL_SPOT"
                        success = close_spread(kite, trade, spot, "SL_SPOT", dry_run,
                                               store=trade_store, strategy_label='BPS')
                        closed = True
                        if not success:
                            log(f"  BPS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BPS #{tid} {stock}: SL_SPOT close FAILED. Manual intervention needed!")

                    # Cooldown gate
                    if not closed and not settled:
                        continue

                    # CHECK 2: SL_SPREAD (spread shrinking = bad, same as BCS)
                    if not closed and spread_val is not None and spread_val <= sl_spread_val:
                        log(f"\n  BPS #{tid} {stock} *** SL_SPREAD HIT: {spread_val:.2f} <= {sl_spread_val:.2f} ***")
                        closing_in_progress[close_key] = "SL_SPREAD"
                        success = close_spread(kite, trade, spot, "SL_SPREAD", dry_run,
                                               store=trade_store, strategy_label='BPS')
                        closed = True
                        if not success:
                            log(f"  BPS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BPS #{tid} {stock}: SL_SPREAD close FAILED. Manual intervention needed!")

                    # CHECK 3: SL_TRAIL (spread shrinking = bad, same as BCS)
                    ts = trail_state[close_key]
                    if not closed and ts['active'] and spread_val is not None and spread_val <= ts['trail']:
                        log(f"\n  BPS #{tid} {stock} *** SL_TRAIL HIT: {spread_val:.2f} <= {ts['trail']:.2f} ***")
                        closing_in_progress[close_key] = "SL_TRAIL"
                        success = close_spread(kite, trade, spot, "SL_TRAIL", dry_run,
                                               store=trade_store, strategy_label='BPS')
                        closed = True
                        if not success:
                            log(f"  BPS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BPS #{tid} {stock}: SL_TRAIL close FAILED. Manual intervention needed!")

                    # CHECK 4: TP — spot <= target (stock DROPPING to target is good for BPS)
                    if not closed and spot <= target:
                        log(f"\n  BPS #{tid} {stock} *** TP HIT: {spot:.2f} <= {target} ***")
                        closing_in_progress[close_key] = "TP"
                        success = close_spread(kite, trade, spot, "TP", dry_run,
                                               store=trade_store, strategy_label='BPS')
                        closed = True
                        if not success:
                            log(f"  BPS #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"BPS #{tid} {stock}: TP close FAILED. Manual intervention needed!")

                else:
                    # ── FH SL_SPOT: spot >= sl_spot (upside/bullish risk) ──
                    if spot >= sl_spot_val:
                        log(f"\n  FH #{tid} {stock} *** SL_SPOT HIT: {spot:.2f} >= {sl_spot_val} ***")
                        closing_in_progress[close_key] = "SL_SPOT"
                        success = close_fh_position(kite, trade, spot, "SL_SPOT", dry_run)
                        closed = True
                        if not success:
                            log(f"  FH #{tid} {stock}: Close failed — manual intervention needed.")
                            send_telegram(f"FH #{tid} {stock}: SL_SPOT close FAILED. Manual intervention needed!")

                if closed:
                    trail_state.pop(close_key, None)

            # ── Watchlist price alerts (after trade checks) ──────────
            if wl_active:
                check_watchlist_alerts(kite, log_fn=log, telegram_fn=send_telegram,
                                       store=wl_store)

            if print_status:
                last_status_time = now

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            log("\nCron monitor stopped by user (Ctrl+C)")
            remaining = _load_all_trades(bcs_store, fh_store, bps_store)
            log(f"Remaining open trades: {len(remaining)}")
            return
        except Exception as e:
            consecutive_errors += 1
            log(f"ERROR in cron loop ({consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}): {e}")
            err_str = str(e).lower()
            if 'token' in err_str or 'invalidtoken' in err_str or 'sessionexpired' in err_str:
                log("FATAL: Kite token appears expired. Cannot continue.")
                remaining = _load_all_trades(bcs_store, fh_store, bps_store)
                stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in remaining)
                send_telegram(f"SPREAD MONITOR FATAL: Kite token expired! Unmonitored: {stocks}")
                return
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                log(f"FATAL: {MAX_CONSECUTIVE_ERRORS} consecutive errors. Exiting.")
                remaining = _load_all_trades(bcs_store, fh_store, bps_store)
                stocks = ', '.join(f"{t['_strategy']}:{t['stock']}" for t in remaining)
                send_telegram(f"SPREAD MONITOR FATAL: {MAX_CONSECUTIVE_ERRORS} errors. Unmonitored: {stocks}")
                return
            if consecutive_errors == 10:
                send_telegram(f"SPREAD MONITOR WARNING: 10 consecutive errors. Last: {e}")
            time.sleep(10)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Unified Spread Monitor — BCS + Bear Put Spread + Fallen Hero with strategy-aware SL'
    )
    parser.add_argument('stock', nargs='?', default=None,
                        help='Stock name (e.g., ICICIBANK). Required unless --list.')
    parser.add_argument('--list', action='store_true',
                        help='List all trades')
    parser.add_argument('--cron', action='store_true',
                        help='Monitor ALL open trades (cron/scheduler mode)')
    parser.add_argument('--trade-id', type=int, default=None,
                        help='Trade ID to monitor (if multiple open trades for same stock)')
    parser.add_argument('--target', type=float, default=None,
                        help='Override target spot price (default: from trade store)')
    parser.add_argument('--sl-spot', type=float, default=None,
                        help='Override spot-based stop loss (default: from trade store)')
    parser.add_argument('--sl-spread', type=float, default=None,
                        help='Override spread-based stop loss (default: from trade store)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Test mode - no real orders placed')
    parser.add_argument('--watchlist', action='store_true',
                        help='List all watchlist items')
    parser.add_argument('--cancel-alert', type=int, default=None,
                        metavar='ID', help='Cancel a watchlist alert by ID')
    args = parser.parse_args()

    # Set up logging for bcs package
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] %(name)s %(levelname)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    # Initialize trade stores (downloads from Drive on startup)
    bcs_store = get_store()

    # ── --watchlist mode ─────────────────────────────────────────────────
    if args.watchlist:
        wl_store = get_watchlist_store()
        wl_store.list_items()
        return

    # ── --cancel-alert mode ──────────────────────────────────────────────
    if args.cancel_alert is not None:
        wl_store = get_watchlist_store()
        wl_store.update_status(args.cancel_alert, 'cancelled')
        print(f"Watchlist item #{args.cancel_alert} cancelled.")
        return

    # ── --list mode ──────────────────────────────────────────────────────
    if args.list:
        print("\n=== BCS Trades ===")
        bcs_store.list_trades()
        bps_store_inst = get_bps_store()
        print("\n=== Bear Put Spread Trades ===")
        bps_store_inst.list_trades()
        fh_store_inst = get_fh_store()
        print("\n=== Fallen Hero Trades ===")
        fh_store_inst.list_trades()
        return

    # ── --cron mode ──────────────────────────────────────────────────────
    if args.cron:
        # Log file is set inside monitor_all()
        kite = load_kite()
        log("Kite authenticated.")
        monitor_all(kite, args.dry_run)
        return

    # ── Stock is required for monitoring ─────────────────────────────────
    if not args.stock:
        parser.error("stock name is required (or use --list)")

    stock = args.stock.upper()

    # ── Look up trade (search BCS first, then BPS, then FH) ─────────────
    trade = bcs_store.find_open_trade(stock, args.trade_id)
    trade_strategy = 'BCS'
    if not trade:
        bps_store_inst = get_bps_store()
        trade = bps_store_inst.find_open_trade(stock, args.trade_id)
        trade_strategy = 'BPS'
    if not trade:
        fh_store_inst = get_fh_store()
        trade = fh_store_inst.find_open_trade(stock, args.trade_id)
        trade_strategy = 'FH'
    if not trade:
        if args.trade_id:
            print(f"ERROR: No open trade found for {stock} with ID {args.trade_id}")
        else:
            print(f"ERROR: No open trade found for {stock}")
        print("Use --list to see all trades.")
        sys.exit(1)

    # ── Connect and run ──────────────────────────────────────────────────
    set_log_file(LOG_DIR / f"spread_monitor_{stock}_{date.today().strftime('%Y%m%d')}.log")
    log(f"Loading Kite token from {TOKEN_FILE}...")
    kite = load_kite()
    log("Kite authenticated.")

    if trade_strategy == 'BCS':
        # ── BCS: Resolve params and run BCS monitor ──────────────────────
        target = args.target if args.target is not None else trade['target_spot']
        sl_spot = args.sl_spot if args.sl_spot is not None else trade['sl_spot']
        sl_spread = args.sl_spread if args.sl_spread is not None else trade['sl_spread']
        log(f"Loaded BCS trade #{trade['id']}: {stock} {trade['long_symbol']}/{trade['short_symbol']}")

        monitor(kite, trade, target, sl_spot, sl_spread, args.dry_run,
                cli_target=args.target, cli_sl_spot=args.sl_spot,
                cli_sl_spread=args.sl_spread)
    elif trade_strategy == 'BPS':
        # ── BPS: Delegate to cron monitor (reversed SL/TP direction) ─────
        log(f"Loaded BPS trade #{trade['id']}: {stock} {trade['long_symbol']}/{trade['short_symbol']}")
        log(f"  BPS single-trade mode delegates to cron monitor (reversed SL/TP).")
        monitor_all(kite, args.dry_run)
    else:
        # ── FH: Use cron-style monitoring for single FH trade ────────────
        log(f"Loaded FH trade #{trade['id']}: {stock} SC:{trade['short_call_symbol']} "
            f"SP:{trade['short_put_symbol']} LP:{trade['long_put_symbol']}")
        log(f"  FH single-trade mode delegates to cron monitor.")
        monitor_all(kite, args.dry_run)


if __name__ == '__main__':
    main()

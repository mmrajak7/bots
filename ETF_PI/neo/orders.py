"""
neo.orders — NeoTrader: order execution engine with safety pipeline.

Public interface for option_buy.py. Drop-in replacement for NeoBridge.
Fixes: C1 (exit price), C3 (fill before TP), C4 (TP race), H2 (stale cancel),
       H3 (single order_report), H4 (circuit breaker), H5 (buffer), M1 (partial fills).
"""

import os
import time
import logging
import datetime
import pytz
from typing import List, Tuple, Optional

from neo.config import NeoConfig
from neo.session import SessionManager
from neo.state import StateManager, NeoPosition
from neo.tracker import OrderTracker, OrderType
from neo.symbols import SymbolMapper
from neo.audit import AuditLog
from neo.tick import round_to_tick_str
from neo import fields

log = logging.getLogger('neo.orders')
IST = pytz.timezone('Asia/Kolkata')


class NeoTrader:
    """
    Bridges option_buy.py signals to Kotak Neo limit orders.

    Usage:
        from neo import NeoTrader
        trader = NeoTrader('config_trade.yaml')
        trader.on_entry(key, pos_dict)
        trader.on_exit(key, exit_type, current_premium)
        fills = trader.sync_tp_fills()
    """

    def __init__(self, config_path=None):
        self.cfg = NeoConfig(config_path)
        data_dir = self.cfg.data_dir

        self._state = StateManager(data_dir)
        self._tracker = OrderTracker(data_dir)
        self._audit = AuditLog(self.cfg.log_dir)
        self._session = None
        self._client = None
        self._mapper = None
        self._last_order_ts = 0.0  # rate limiter

        # Connect FIRST so clean_stale can actually cancel exchange orders
        if self.cfg.enabled and not self.cfg.log_only:
            self._connect()

        # Clean stale state — cancel exchange orders (H2 fix, needs client)
        self._state.clean_stale(cancel_fn=self._cancel_safe)

        log.info(f"NeoTrader: enabled={self.cfg.enabled}, "
                 f"log_only={self.cfg.log_only}, lots={self.cfg.num_lots}")

    # ==================================================================
    # Properties (for option_buy.py compatibility)
    # ==================================================================

    @property
    def enabled(self):
        return self.cfg.enabled

    @property
    def log_only(self):
        return self.cfg.log_only

    def get_open_count(self):
        return self._state.get_open_count()

    # ==================================================================
    # PUBLIC: on_entry
    # ==================================================================

    def on_entry(self, key, pos_dict):
        """Place entry LIMIT BUY. TP is NOT placed until fill confirmed (C3 fix).

        Args:
            key: Position key (e.g., 'NIFTY_CE_15minute_20260406')
            pos_dict: Dict from _open_pos() with entry_premium, st_value, etc.
        """
        if not self.cfg.enabled:
            return

        # Defense-in-depth: no trades before 9:30 AM IST
        now_time = datetime.datetime.now(IST).time()
        if now_time < datetime.time(9, 30):
            log.warning(f"on_entry blocked: before 9:30 AM ({now_time.strftime('%H:%M')})")
            self._audit.log('ENTRY_BLOCKED', {'key': key, 'reason': 'before 9:30 AM'})
            return

        opt_sym = pos_dict.get('option_symbol', '')
        entry_prem = pos_dict.get('entry_premium', 0)
        st_val = pos_dict.get('st_value', 0)
        signal_tf = pos_dict.get('signal_tf', '')

        if not opt_sym or entry_prem <= 0 or st_val <= 0:
            log.warning(f"on_entry: invalid data for {key}")
            return

        # Safety pipeline
        ok, reason = self._check_safety(key)
        if not ok:
            log.warning(f"on_entry blocked: {key} — {reason}")
            self._audit.log('ENTRY_BLOCKED', {'key': key, 'reason': reason})
            return

        # Cross-TF safety net: prevent two positions for the same exchange symbol
        ok, reason = self._check_symbol_conflict(opt_sym)
        if not ok:
            log.warning(f"on_entry blocked: {key} — {reason}")
            self._audit.log('ENTRY_BLOCKED', {'key': key, 'reason': reason})
            return

        # Lot size & tick size from instruments (passed by option_buy.py)
        lot_size = int(pos_dict.get('lot_size', 0))
        tick_size = float(pos_dict.get('tick_size', 0))
        if lot_size <= 0:
            lot_size = self._mapper.get_lot_size() if self._mapper else 75
            log.warning(f"lot_size missing in pos_dict, using fallback: {lot_size}")
        if tick_size <= 0:
            tick_size = 0.05
            log.warning("tick_size missing in pos_dict, using fallback: 0.05")
        qty = lot_size * self.cfg.num_lots

        # Prices (use tick_size from instruments for rounding)
        entry_price = float(round_to_tick_str(
            entry_prem + self.cfg.entry_tick_buffer, tick_size, 'up'))
        tp_price = float(round_to_tick_str(st_val, tick_size, 'down'))
        max_hold = self.cfg.time_sl.get(signal_tf,
                                         self.cfg.time_sl.get('default', 120))

        pos = NeoPosition(
            key=key, symbol=opt_sym, qty=qty,
            entry_price=entry_price, tp_price=tp_price,
            signal_tf=signal_tf, max_hold_mins=int(max_hold),
            entry_time=datetime.datetime.now(IST).isoformat(),
            status='pending_entry', entry_filled=False,
            lot_size=lot_size, tick_size=tick_size,
        )

        self._audit.log('ENTRY_SIGNAL', {
            'key': key, 'symbol': opt_sym, 'qty': qty,
            'entry_price': entry_price, 'tp_price': tp_price,
            'time_sl': max_hold,
        })

        if self.cfg.log_only:
            log.info(f"[LOG-ONLY] ENTRY: {opt_sym} qty={qty} @ {entry_price} "
                     f"TP={tp_price} TIME_SL={max_hold}min")
            pos.entry_order_id = 'LOG_ONLY'
            pos.tp_order_id = 'LOG_ONLY'
            pos.entry_filled = True
            pos.status = 'open'
            self._state.positions[key] = pos
            self._state.trade_count += 1
            self._state.save()
            return

        # Place LIMIT BUY
        oid = self._place_buy(opt_sym, qty, entry_price, tick_size)
        if oid:
            pos.entry_order_id = oid
            tracked = self._tracker.get_order_by_id(oid)
            pos.entry_tag = tracked.tag if tracked else None
            log.info(f"ENTRY placed: {opt_sym} qty={qty} @ {entry_price} oid={oid}")
        else:
            pos.status = 'failed'
            log.error(f"ENTRY failed: {opt_sym}")

        self._state.positions[key] = pos
        self._state.trade_count += 1
        self._state.save()
        self._tracker.save()

    # ==================================================================
    # PUBLIC: on_exit
    # ==================================================================

    def on_exit(self, key, exit_type, current_premium=0.0):
        """Handle position exit. Receives current_premium from option_buy (C1 fix).

        Args:
            key: Position key.
            exit_type: 'TP', 'TIME_SL', 'PREM_SL', 'COST_SL', 'EOD', 'STALE'.
            current_premium: Current option LTP from Kite _quote() (fixes C1).
        """
        if not self.cfg.enabled:
            return

        pos = self._state.positions.get(key)
        if not pos or pos.status not in ('open', 'pending_entry'):
            return

        self._audit.log(f'EXIT_{exit_type}', {
            'key': key, 'symbol': pos.symbol, 'qty': pos.qty,
            'current_premium': current_premium,
        })

        if self.cfg.log_only:
            log.info(f"[LOG-ONLY] EXIT ({exit_type}): {pos.symbol} qty={pos.qty}")
            pos.status = 'closed'
            pos.exit_type = exit_type
            pos.exit_time = datetime.datetime.now(IST).isoformat()
            self._state.save()
            return

        tp_oid = pos.tp_order_id

        if exit_type == 'TP':
            # TP was filled on exchange. Mark closed, no exit order needed.
            pos.status = 'closed'
            pos.exit_type = 'TP'
            pos.exit_time = datetime.datetime.now(IST).isoformat()
            self._state.save()
            return

        # Non-TP exit: cancel TP first, then place exit sell
        # C4 fix: check if TP filled AFTER cancel
        if tp_oid and tp_oid not in ('LOG_ONLY', None):
            self._cancel_order(tp_oid)
            # Re-check TP status (C4 race fix)
            tp_status = self._check_single_order(tp_oid)
            if tp_status == 'complete':
                log.info(f"TP RACE: {key} — TP filled during cancel. Marking TP_EXCHANGE.")
                pos.status = 'closed'
                pos.exit_type = 'TP_EXCHANGE'
                pos.exit_time = datetime.datetime.now(IST).isoformat()
                self._audit.log('TP_RACE_WIN', {'key': key})
                self._state.save()
                return

        # Cancel unfilled entry if pending_entry (Fix F2: verify cancel)
        if pos.status == 'pending_entry' and pos.entry_order_id:
            self._cancel_order(pos.entry_order_id)
            # Verify the entry didn't fill BEFORE the cancel took effect
            entry_status = self._check_single_order(pos.entry_order_id)
            if entry_status == 'complete':
                # Entry filled despite cancel — need to exit this position
                log.warning(f"Entry filled during cancel for {key} — placing exit sell")
                pos.entry_filled = True
                pos.status = 'open'
                # Fall through to exit sell below
            else:
                pos.status = 'closed'
                pos.exit_type = exit_type
                pos.exit_time = datetime.datetime.now(IST).isoformat()
                self._state.save()
                return

        # Place exit LIMIT SELL (C1 fix: uses current_premium, not _get_bid)
        exit_price = max(current_premium - self.cfg.exit_tick_buffer, 0.05)
        exit_oid = self._place_sell(pos.symbol, pos.qty, exit_price, OrderType.EXIT, pos.tick_size)
        pos.exit_order_id = exit_oid
        if exit_oid:
            log.info(f"EXIT ({exit_type}): {pos.symbol} SELL @ {exit_price} oid={exit_oid}")
            pos.status = 'pending_exit'
            pos.exit_type = exit_type
        else:
            log.error(f"EXIT order failed: {pos.symbol} — will retry next tick")
            pos.status = 'open'  # Keep open so next tick retries
            pos.exit_type = exit_type  # Remember intent

        self._state.save()
        self._tracker.save()

    # ==================================================================
    # PUBLIC: sync_tp_fills
    # ==================================================================

    def sync_tp_fills(self):
        """Check entry fills and TP fills. Single order_report() call (H3 fix).

        Returns:
            List of (key, fill_price) for TP-filled positions.
        """
        if self.cfg.log_only or not self._client:
            return []

        # Refresh session if needed
        if self._session:
            self._session.refresh_if_needed()

        # Single order_report fetch (H3 fix)
        all_orders = self._fetch_all_orders()
        if all_orders is None:
            return []

        filled_tps = []
        modified = False

        for key, pos in list(self._state.positions.items()):
            if pos.status == 'pending_entry' and not pos.entry_filled:
                # C3 fix: check if entry LIMIT BUY filled
                if pos.entry_order_id and pos.entry_order_id != 'LOG_ONLY':
                    status, fill_price, fill_qty = self._order_info(
                        pos.entry_order_id, all_orders)

                    if status == 'complete' and fill_qty >= pos.qty:
                        pos.entry_filled = True
                        pos.status = 'open'
                        modified = True
                        log.info(f"Entry filled: {key} @ {fill_price} qty={fill_qty}")

                        # NOW place TP LIMIT SELL (C3 fix: only after fill confirmed)
                        if self.cfg.tp_order_enabled and pos.tp_price > pos.entry_price:
                            tp_oid = self._place_sell(
                                pos.symbol, pos.qty, pos.tp_price,
                                OrderType.TARGET, pos.tick_size)
                            pos.tp_order_id = tp_oid
                            if tp_oid:
                                log.info(f"TP placed: {pos.symbol} SELL @ {pos.tp_price}")

                    elif status == 'complete' and 0 < fill_qty < pos.qty:
                        # M1: partial fill — adjust qty to filled amount
                        pos.qty = fill_qty
                        pos.entry_filled = True
                        pos.status = 'open'
                        modified = True
                        log.warning(f"Entry PARTIAL fill: {key} filled={fill_qty}")

                        if self.cfg.tp_order_enabled and pos.tp_price > pos.entry_price:
                            tp_oid = self._place_sell(
                                pos.symbol, fill_qty, pos.tp_price,
                                OrderType.TARGET, pos.tick_size)
                            pos.tp_order_id = tp_oid

                    elif status == 'rejected':
                        pos.status = 'failed'
                        pos.exit_type = 'ENTRY_REJECTED'
                        modified = True
                        log.error(f"Entry REJECTED: {key}")

            elif pos.status == 'open':
                # Check if TP LIMIT SELL filled on exchange
                if pos.tp_order_id and pos.tp_order_id != 'LOG_ONLY':
                    status, fill_price, fill_qty = self._order_info(
                        pos.tp_order_id, all_orders)
                    if status == 'complete':
                        filled_tps.append((key, fill_price))
                        pos.status = 'closed'
                        pos.exit_type = 'TP_EXCHANGE'
                        pos.exit_time = datetime.datetime.now(IST).isoformat()
                        modified = True
                        log.info(f"TP filled on exchange: {key} @ {fill_price}")

            elif pos.status == 'pending_exit':
                # Fix 2: Monitor exit orders until filled
                if pos.exit_order_id and pos.exit_order_id != 'LOG_ONLY':
                    status, fill_price, fill_qty = self._order_info(
                        pos.exit_order_id, all_orders)
                    if status == 'complete':
                        pos.status = 'closed'
                        pos.exit_time = datetime.datetime.now(IST).isoformat()
                        modified = True
                        log.info(f"Exit filled: {key} @ {fill_price}")
                    elif status == 'rejected':
                        # Exit rejected — revert to open for retry
                        pos.status = 'open'
                        pos.exit_order_id = None
                        modified = True
                        log.warning(f"Exit REJECTED: {key} — will retry")

        # Update circuit breaker (H4 fix)
        self._update_circuit_breaker()

        if modified or filled_tps:
            self._state.save()
            self._tracker.save()

        return filled_tps

    # ==================================================================
    # PUBLIC: check_time_sl
    # ==================================================================

    def check_time_sl(self):
        """Return position keys that exceeded their time SL."""
        to_close = []
        now = datetime.datetime.now(IST)

        for key, pos in self._state.positions.items():
            if pos.status not in ('open', 'pending_entry'):
                continue
            if not pos.entry_time:
                continue
            try:
                entry_dt = datetime.datetime.fromisoformat(pos.entry_time)
                if entry_dt.tzinfo is None:
                    entry_dt = IST.localize(entry_dt)
                elapsed = (now - entry_dt).total_seconds() / 60
                if elapsed > pos.max_hold_mins:
                    to_close.append(key)
                    log.info(f"TIME_SL: {key} held {elapsed:.0f}min > {pos.max_hold_mins}min")
            except Exception as e:
                # Don't silently skip — safer to flag for close than hold forever
                log.warning(f"TIME_SL parse error for {key}: {e} — flagging for close")
                to_close.append(key)

        return to_close

    # ==================================================================
    # PUBLIC: get_rejected_entries (Fix A3: propagate rejections)
    # ==================================================================

    def get_rejected_entries(self):
        """Return keys of positions whose entry was rejected on exchange.

        Called by option_buy.py to sync its st_opt_positions.json.
        """
        return [k for k, p in self._state.positions.items()
                if p.status == 'failed' and p.exit_type == 'ENTRY_REJECTED']

    # ==================================================================
    # Connection
    # ==================================================================

    def _connect(self):
        """Initialize Neo session and symbol mapper."""
        try:
            self._session = SessionManager(self.cfg, self.cfg.data_dir)
            ok, msg = self._session.auto_login()
            if not ok:
                log.error(f"Neo login failed: {msg}")
                return
            self._client = self._session.get_client()
            log.info(f"Neo connected: {msg}")

            self._mapper = SymbolMapper(self._client, self.cfg.data_dir)
        except Exception as e:
            log.error(f"Neo connect failed: {e}", exc_info=True)

    # ==================================================================
    # Safety Pipeline (H4, H5)
    # ==================================================================

    def _check_safety(self, key):
        """Pre-order safety checks. Returns (allowed, reason)."""
        # H4: Circuit breaker
        if self._state.circuit_breaker_active:
            return False, f"Circuit breaker: {self._state.circuit_breaker_reason}"

        # Position limit
        open_count = self._state.get_open_count()
        if open_count >= self.cfg.max_concurrent_positions:
            return False, f"Position limit: {open_count}/{self.cfg.max_concurrent_positions}"

        # Fat finger: max lots per order
        if self.cfg.num_lots > self.cfg.max_lots_per_order:
            return False, (f"Fat finger: num_lots={self.cfg.num_lots} > "
                           f"max={self.cfg.max_lots_per_order}")

        # Duplicate check (same key already exists and not closed/failed)
        existing = self._state.positions.get(key)
        if existing and existing.status in ('pending_entry', 'open', 'pending_exit'):
            return False, f"Duplicate: {key} already active"

        return True, ''

    def _check_symbol_conflict(self, symbol):
        """Prevent two positions for the same exchange symbol (cross-TF safety net)."""
        for k, p in self._state.positions.items():
            if p.status in ('pending_entry', 'open', 'pending_exit') and p.symbol == symbol:
                return False, f"Symbol conflict: {symbol} already active in {k}"
        return True, ''

    def _update_circuit_breaker(self):
        """Fetch Neo positions, check daily P&L against limit (H4 fix)."""
        if not self._client or self.cfg.log_only:
            return
        try:
            resp = self._client.positions()
            positions_list = []
            if isinstance(resp, dict):
                positions_list = resp.get('data', [])
            elif isinstance(resp, list):
                positions_list = resp

            total_pnl = sum(fields.get_raw_pnl(p) for p in positions_list
                            if isinstance(p, dict))
            self._state.daily_pnl = total_pnl

            if total_pnl < -self.cfg.max_loss_per_day:
                self._state.circuit_breaker_active = True
                self._state.circuit_breaker_reason = (
                    f"Daily loss {total_pnl:.0f} > limit {self.cfg.max_loss_per_day:.0f}")
                log.warning(f"CIRCUIT BREAKER: {self._state.circuit_breaker_reason}")
        except Exception as e:
            log.warning(f"Circuit breaker check failed: {e}")

    # ==================================================================
    # Order Placement
    # ==================================================================

    def _ensure_session(self):
        """Refresh session before order placement if needed."""
        if self._session:
            self._session.refresh_if_needed()

    def _place_buy(self, symbol, qty, price, tick_size=0.05):
        """Place LIMIT BUY. Returns order_id or None."""
        if not self._client:
            return None
        self._ensure_session()

        neo_params = self._map(symbol)
        if not neo_params:
            return None

        self._rate_limit()
        tag = self._tracker.generate_tag(OrderType.ENTRY)
        self._tracker.register_order(tag, symbol, 'B', qty, 'L', OrderType.ENTRY)
        price_str = round_to_tick_str(price, tick_size, 'up')

        try:
            # LIMIT orders only — limit price IS the market protection (SEBI Apr 2026)
            resp = self._client.place_order(
                exchange_segment=neo_params['exchange_segment'],
                product=self.cfg.product,
                price=price_str,
                order_type='L',
                quantity=str(qty),
                validity='DAY',
                trading_symbol=neo_params['trading_symbol'],
                transaction_type='B',
                amo='NO',
                disclosed_quantity='0',
                market_protection=self.cfg.market_protection,
                pf='N',
                trigger_price='0',
                tag=tag,
            )

            oid = fields.get_order_id(resp)
            if oid:
                self._tracker.confirm_order(tag, oid, resp)
                self._audit.log('BUY_PLACED', {
                    'symbol': symbol, 'qty': qty, 'price': price_str,
                    'oid': oid, 'tag': tag,
                })
                return oid

            # Order might have been placed despite garbled response
            log.warning(f"BUY response unclear: {resp}")
            verified = self._tracker.verify_order_by_tag(self._client, tag)
            if verified:
                return verified

            self._tracker.mark_failed(tag, f"No order_id in response: {resp}")
            return None

        except Exception as e:
            log.error(f"BUY failed: {e}", exc_info=True)
            self._tracker.mark_failed(tag, str(e))
            return None

    def _place_sell(self, symbol, qty, price, order_type_tag=OrderType.TARGET, tick_size=0.05):
        """Place LIMIT SELL. Returns order_id or None."""
        if not self._client:
            return None
        self._ensure_session()

        neo_params = self._map(symbol)
        if not neo_params:
            return None

        self._rate_limit()
        tag = self._tracker.generate_tag(order_type_tag)
        self._tracker.register_order(tag, symbol, 'S', qty, 'L', order_type_tag)
        price_str = round_to_tick_str(price, tick_size, 'down')

        try:
            # LIMIT orders only — limit price IS the market protection (SEBI Apr 2026)
            resp = self._client.place_order(
                exchange_segment=neo_params['exchange_segment'],
                product=self.cfg.product,
                price=price_str,
                order_type='L',
                quantity=str(qty),
                validity='DAY',
                trading_symbol=neo_params['trading_symbol'],
                transaction_type='S',
                amo='NO',
                disclosed_quantity='0',
                market_protection=self.cfg.market_protection,
                pf='N',
                trigger_price='0',
                tag=tag,
            )

            oid = fields.get_order_id(resp)
            if oid:
                self._tracker.confirm_order(tag, oid, resp)
                tag_label = 'TP' if order_type_tag == OrderType.TARGET else 'EXIT'
                self._audit.log(f'{tag_label}_PLACED', {
                    'symbol': symbol, 'qty': qty, 'price': price_str,
                    'oid': oid, 'tag': tag,
                })
                return oid

            log.warning(f"SELL response unclear: {resp}")
            verified = self._tracker.verify_order_by_tag(self._client, tag)
            if verified:
                return verified

            self._tracker.mark_failed(tag, f"No order_id in response: {resp}")
            return None

        except Exception as e:
            log.error(f"SELL failed: {e}", exc_info=True)
            self._tracker.mark_failed(tag, str(e))
            return None

    def _cancel_order(self, order_id):
        """Cancel an order. Returns True if cancel call succeeded."""
        if not self._client or not order_id:
            return False
        try:
            self._client.cancel_order(order_id=str(order_id), amo='NO')
            log.info(f"Cancel sent: {order_id}")
            return True
        except Exception as e:
            log.warning(f"Cancel failed {order_id}: {e}")
            return False

    def _cancel_safe(self, order_id):
        """Best-effort cancel for stale cleanup. Swallows exceptions."""
        try:
            self._cancel_order(order_id)
        except Exception:
            pass

    # ==================================================================
    # Order Status
    # ==================================================================

    def _fetch_all_orders(self):
        """Single order_report() call. Returns list of order dicts (H3 fix)."""
        try:
            resp = self._client.order_report()
            if not resp:
                return []
            if isinstance(resp, dict):
                return resp.get('data', [])
            if isinstance(resp, list):
                return resp
            return []
        except Exception as e:
            log.warning(f"order_report failed: {e}")
            return None

    def _order_info(self, order_id, all_orders):
        """Get (status, fill_price, fill_qty) for an order from cached list."""
        for o in (all_orders or []):
            oid = fields.get_order_id(o)
            if oid and str(oid) == str(order_id):
                return (
                    fields.get_order_status(o),
                    fields.get_avg_fill_price(o),
                    fields.get_filled_qty(o),
                )
        return ('unknown', 0.0, 0)

    def _check_single_order(self, order_id):
        """Check status of a single order (for C4 race fix)."""
        orders = self._fetch_all_orders()
        if orders is None:
            return 'unknown'
        status, _, _ = self._order_info(order_id, orders)
        return status

    # ==================================================================
    # Helpers
    # ==================================================================

    def _map(self, kite_symbol):
        """Map Kite symbol to Neo params. Returns dict or None."""
        if not self._mapper:
            log.error(f"Symbol mapper not initialized")
            return None
        result = self._mapper.map_symbol(kite_symbol)
        if not result:
            log.error(f"Symbol mapping failed: {kite_symbol}")
        return result

    def _rate_limit(self):
        """Enforce 10 OPS rate limit (SEBI mandate)."""
        now = time.time()
        elapsed = now - self._last_order_ts
        if elapsed < 0.1:  # 10 per second = 0.1s gap
            time.sleep(0.1 - elapsed)
        self._last_order_ts = time.time()

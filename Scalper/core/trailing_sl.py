"""
NEO Trade Terminal - Trailing Stop Loss Manager

Provides multiple ways to trail SL:
1. Manual one-click trail (button click moves SL to cost/LTP-X)
2. Auto-trail based on LTP movement
3. Quick keyboard shortcuts for trail increments

Keyed by entry_order_id to support multiple brackets on same symbol.
"""

import math
import threading
import time
from typing import Dict, Optional, Callable, Any, List
from dataclasses import dataclass, field
from enum import Enum
import logging

from core.charge_calculator import get_breakeven_points
from core.tick_utils import calculate_sl_limit_price, round_trigger_price, get_tick_size, round_to_tick
from core.order_tracker import OrderType, get_order_tracker

logger = logging.getLogger(__name__)


class TrailMode(Enum):
    """Trailing stop loss modes."""
    MANUAL = "manual"           # User clicks to trail
    AUTO_POINTS = "auto_points" # Trail by fixed points when profit increases
    AUTO_PERCENT = "auto_pct"   # Trail by percentage of profit
    LOCK_COST = "lock_cost"     # Move SL to cost when X profit reached


@dataclass
class TrailingPosition:
    """Position being trailed."""
    entry_order_id: str  # CRITICAL: Unique key for this trailing position
    symbol: str
    exchange_segment: str
    entry_price: float
    quantity: int
    side: str                    # 'LONG' or 'SHORT'
    current_sl: float
    sl_order_id: str
    instrument_token: str = ""   # Required for LTP fetch in auto-trail
    target_order_id: Optional[str] = None
    product: str = "MIS"         # S23-C1: Store product type for SL recreation (MIS/NRML)

    # Trailing config
    trail_mode: TrailMode = TrailMode.MANUAL
    trail_points: float = 0      # For AUTO_POINTS: trail by X points
    trail_percent: float = 0     # For AUTO_PERCENT: trail by X% of profit
    lock_cost_trigger: float = 0 # For LOCK_COST: lock when profit > X points
    cost_locked: bool = False

    # Tracking
    highest_profit: float = 0    # Track peak profit (for LONG)
    lowest_profit: float = 0     # Track lowest profit (for SHORT)
    last_ltp: float = 0
    trail_history: List[Dict[str, Any]] = field(default_factory=list)


class TrailingSLManager:
    """
    Manages trailing stop losses for positions.

    CRITICAL: Keyed by entry_order_id (not symbol) to support multiple
    bracket orders on the same symbol at different levels.
    """

    def __init__(self, neo_client, order_manager=None,
                 sound_mgr=None, telegram_mgr=None, config: Dict[str, Any] = None,
                 position_tracker=None):
        self.client = neo_client
        self.order_mgr = order_manager
        self.sound = sound_mgr
        self.telegram = telegram_mgr
        self.config = config or {}
        self.pos_tracker = position_tracker  # For checking exit locks
        self.oco_monitor = None  # Set after construction (circular dependency)
        self.order_tracker = get_order_tracker()  # S35: Order tracking

        self.positions: Dict[str, TrailingPosition] = {}  # keyed by entry_order_id
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._check_interval = 0.5  # Check every 500ms for fast trailing

        # CRITICAL: Health monitoring for daemon thread
        self._last_heartbeat: float = 0.0
        self._heartbeat_timeout: float = 10.0  # Alert if no heartbeat for 10 seconds
        self._consecutive_errors: int = 0
        self._max_consecutive_errors: int = 5

        # Config
        trail_config = self.config.get('trailing_sl', {})
        self.min_sl_distance = trail_config.get('min_sl_distance', 5)
        self.trail_buttons = trail_config.get('trail_buttons', [10, 25, 50])
        self.ltp_buffer = trail_config.get('ltp_buffer', 20)

        # Callbacks for GUI updates
        self.on_sl_updated: Optional[Callable] = None
        self.on_cost_locked: Optional[Callable] = None

    def add_position(self, entry_order_id: str, symbol: str, exchange_segment: str,
                    entry_price: float, quantity: int, side: str,
                    sl_price: float, sl_order_id: str,
                    instrument_token: str = "",
                    target_order_id: str = None,
                    product: str = "MIS",
                    trail_mode: TrailMode = TrailMode.MANUAL,
                    trail_points: float = 0,
                    trail_percent: float = 0,
                    lock_cost_trigger: float = 0) -> bool:
        """
        Register position for trailing.

        Args:
            entry_order_id: Entry order ID (UNIQUE key for this position)
            symbol: Trading symbol
            exchange_segment: Exchange segment
            entry_price: Entry price
            quantity: Position quantity
            side: 'LONG' or 'SHORT'
            sl_price: Current SL price
            sl_order_id: SL order ID
            instrument_token: Instrument token for LTP fetch (required for auto-trail)
            target_order_id: Target order ID (optional)
            product: Product type MIS or NRML (for SL recreation)
            trail_mode: Trailing mode
            trail_points: Points to trail by (for AUTO_POINTS)
            trail_percent: Percent to trail by (for AUTO_PERCENT)
            lock_cost_trigger: Profit trigger for cost lock (for LOCK_COST)

        Returns:
            True if added successfully
        """
        with self._lock:
            pos = TrailingPosition(
                entry_order_id=entry_order_id,
                symbol=symbol,
                exchange_segment=exchange_segment,
                entry_price=entry_price,
                quantity=quantity,
                side=side.upper(),
                current_sl=sl_price,
                sl_order_id=sl_order_id,
                instrument_token=instrument_token,
                target_order_id=target_order_id,
                product=product,
                trail_mode=trail_mode,
                trail_points=trail_points,
                trail_percent=trail_percent,
                lock_cost_trigger=lock_cost_trigger,
                last_ltp=entry_price
            )
            self.positions[entry_order_id] = pos
            logger.info(f"[TRAIL] Added {symbol} (entry={entry_order_id}) mode={trail_mode.value}")
            return True

    def remove_position(self, entry_order_id: str):
        """Remove position from trailing."""
        with self._lock:
            if entry_order_id in self.positions:
                pos = self.positions[entry_order_id]
                del self.positions[entry_order_id]
                logger.info(f"[TRAIL] Removed {pos.symbol} (entry={entry_order_id})")

    def remove_positions_by_symbol(self, symbol: str):
        """Remove all trailing positions for a symbol."""
        with self._lock:
            entries_to_remove = [
                entry_id for entry_id, pos in self.positions.items()
                if pos.symbol == symbol
            ]
            for entry_id in entries_to_remove:
                del self.positions[entry_id]
            if entries_to_remove:
                logger.info(f"[TRAIL] Removed {len(entries_to_remove)} positions for {symbol}")

    # ==================== MANUAL TRAIL METHODS ====================

    def trail_to_cost(self, entry_order_id: str, current_ltp: float = None) -> Dict[str, Any]:
        """
        One-click: Move SL to breakeven or entry price.

        S34 Logic:
        - Calculate BE = entry + charges (LONG) or entry - charges (SHORT)
        - If LTP allows BE (LTP > BE for LONG): set SL = BE
        - If LTP doesn't allow BE but allows entry: set SL = entry
        - Works for both new SL placement and modifying existing SL

        Args:
            entry_order_id: Entry order ID
            current_ltp: Current LTP (optional - if not provided, uses cached last_ltp)

        Returns:
            Result dict with success status
        """
        # S36: Calculate new_sl inside lock, then call _update_sl outside lock
        # This prevents blocking other threads during API calls
        new_sl = None
        reason = None
        symbol = None
        entry_price = None
        calculated_be = None
        ltp = None

        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            # Use provided LTP or fall back to cached
            ltp = current_ltp if current_ltp and current_ltp > 0 else pos.last_ltp
            if ltp <= 0:
                return {'success': False, 'error': f'No valid LTP available (cached: {pos.last_ltp})'}

            # Update cached LTP if fresh one provided
            if current_ltp and current_ltp > 0:
                pos.last_ltp = current_ltp

            # Copy needed values for calculation
            symbol = pos.symbol
            entry_price = pos.entry_price

            # Calculate TRUE breakeven including round-trip transaction costs
            be_points = get_breakeven_points(pos.entry_price, pos.quantity)

            if pos.side == 'LONG':
                # LONG: BE = entry + charges
                calculated_be = pos.entry_price + be_points
            else:
                # SHORT: BE = entry - charges
                calculated_be = pos.entry_price - be_points

            # S38: Round to actual tick size (was hardcoded 0.1, should use exchange tick size)
            tick = get_tick_size(pos.exchange_segment)
            calculated_be = round_to_tick(calculated_be, tick, direction='nearest')
            entry_rounded = round_to_tick(pos.entry_price, tick, direction='nearest')

            # S34: Determine best SL price based on LTP
            if pos.side == 'LONG':
                # LONG: SL must be BELOW LTP
                if ltp > calculated_be:
                    # Best case: can lock BE
                    new_sl = calculated_be
                    reason = f"BE({be_points:.1f}pts)"
                elif ltp > entry_rounded:
                    # Can't lock BE, but can lock entry
                    new_sl = entry_rounded
                    reason = "ENTRY(BE not reachable)"
                else:
                    # LTP <= entry - position in loss, can't set protective SL
                    return {
                        'success': False,
                        'error': f'LTP ({ltp:.2f}) below entry ({entry_price:.2f}). Cannot set BE/entry SL.'
                    }
            else:  # SHORT
                # SHORT: SL must be ABOVE LTP
                if ltp < calculated_be:
                    # Best case: can lock BE
                    new_sl = calculated_be
                    reason = f"BE({be_points:.1f}pts)"
                elif ltp < entry_rounded:
                    # Can't lock BE, but can lock entry
                    new_sl = entry_rounded
                    reason = "ENTRY(BE not reachable)"
                else:
                    # LTP >= entry - position in loss
                    return {
                        'success': False,
                        'error': f'LTP ({ltp:.2f}) above entry ({entry_price:.2f}). Cannot set BE/entry SL.'
                    }

        logger.info(f"[BE] {symbol}: Entry={entry_price:.2f}, BE={calculated_be:.2f}, LTP={ltp:.2f} -> SL={new_sl:.2f} ({reason})")

        # API call outside lock - _update_sl is now thread-safe
        return self._update_sl(entry_order_id, new_sl, reason, force=True)

    def trail_by_points(self, entry_order_id: str, points: float) -> Dict[str, Any]:
        """
        One-click: Move SL up/down by X points from current SL.
        Positive = tighter SL (favorable), Negative = wider SL.

        For LONG: trail_by_points(+10) moves SL from 100 to 110
        For SHORT: trail_by_points(+10) moves SL from 100 to 90

        Args:
            entry_order_id: Entry order ID
            points: Points to trail by

        Returns:
            Result dict
        """
        # S36: Calculate new_sl inside lock, then call _update_sl outside lock
        # This prevents blocking other threads during API calls
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            if pos.side == 'LONG':
                new_sl = pos.current_sl + points
            else:
                new_sl = pos.current_sl - points

        # API call outside lock - _update_sl is now thread-safe
        return self._update_sl(entry_order_id, new_sl, f"TRAIL_+{points}pts", force=True)

    def trail_to_price(self, entry_order_id: str, new_sl: float) -> Dict[str, Any]:
        """
        Set SL to specific price.

        Args:
            entry_order_id: Entry order ID
            new_sl: New SL price

        Returns:
            Result dict
        """
        # S36: Verify position exists inside lock, then call _update_sl outside lock
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

        # API call outside lock - _update_sl is now thread-safe
        return self._update_sl(entry_order_id, new_sl, f"TRAIL_TO_{new_sl}", force=True)

    def trail_to_ltp_minus(self, entry_order_id: str, buffer_points: float,
                          current_ltp: float) -> Dict[str, Any]:
        """
        One-click: Move SL to (LTP - buffer).
        Useful for quick trailing in momentum.

        For LONG: SL = LTP - buffer
        For SHORT: SL = LTP + buffer

        Args:
            entry_order_id: Entry order ID
            buffer_points: Buffer from LTP
            current_ltp: Current LTP

        Returns:
            Result dict
        """
        # S36: Calculate new_sl inside lock, then call _update_sl outside lock
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            if pos.side == 'LONG':
                new_sl = current_ltp - buffer_points
                # Don't trail backwards
                if new_sl <= pos.current_sl:
                    return {'success': False, 'error': 'New SL not favorable'}
            else:
                new_sl = current_ltp + buffer_points
                if new_sl >= pos.current_sl:
                    return {'success': False, 'error': 'New SL not favorable'}

        # API call outside lock - _update_sl is now thread-safe
        return self._update_sl(entry_order_id, new_sl, f"TRAIL_LTP-{buffer_points}", force=True)

    # ==================== AUTO TRAIL METHODS ====================

    def start_auto_trail(self):
        """Start auto-trailing thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._auto_trail_loop, daemon=True)
        self._thread.start()
        logger.info("[TRAIL] Auto-trail started")

    def stop_auto_trail(self):
        """Stop auto-trailing."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        logger.info("[TRAIL] Auto-trail stopped")

    def _auto_trail_loop(self):
        """Background loop for auto-trailing with health tracking."""
        while self._running:
            try:
                # Update heartbeat
                self._last_heartbeat = time.time()
                self._process_auto_trails()
                self._consecutive_errors = 0
            except Exception as e:
                logger.error(f"[TRAIL] Error: {e}")
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._max_consecutive_errors:
                    logger.error(f"[TRAIL] CRITICAL: {self._consecutive_errors} consecutive errors!")
                    if self.telegram:
                        self.telegram.send(f"🚨 Trail Manager: {self._consecutive_errors} errors! Auto-trailing may fail!")
                    self._consecutive_errors = 0
            time.sleep(self._check_interval)

    def is_healthy(self) -> bool:
        """Check if trail manager thread is healthy."""
        if not self._running:
            return True
        if self._last_heartbeat == 0:
            return True
        return (time.time() - self._last_heartbeat) < self._heartbeat_timeout

    def _process_auto_trails(self):
        """Process all positions for auto-trailing.

        CRITICAL: API calls (modify_order, order_report) are made OUTSIDE the lock
        to prevent blocking other operations for seconds at a time.
        """
        # Phase 1: Collect position data inside lock
        positions_data = []
        with self._lock:
            for entry_id, pos in self.positions.items():
                if pos.trail_mode != TrailMode.MANUAL:
                    if pos.instrument_token:
                        # Collect immutable snapshot of position data
                        positions_data.append({
                            'entry_order_id': entry_id,
                            'instrument_token': pos.instrument_token,
                            'exchange_segment': pos.exchange_segment,
                            'symbol': pos.symbol,
                            'side': pos.side,
                            'entry_price': pos.entry_price,
                            'current_sl': pos.current_sl,
                            'sl_order_id': pos.sl_order_id,
                            'trail_mode': pos.trail_mode,
                            'trail_points': pos.trail_points,
                            'trail_percent': pos.trail_percent,
                            'lock_cost_trigger': pos.lock_cost_trigger,
                            'cost_locked': pos.cost_locked,
                            'highest_profit': pos.highest_profit,
                            'last_ltp': pos.last_ltp
                        })
                    else:
                        logger.warning(f"[TRAIL] Skipping {pos.symbol} - no instrument_token for LTP fetch")

        if not positions_data:
            return

        # Phase 2: Fetch LTPs (API call - OUTSIDE lock)
        tokens = [
            {"instrument_token": p['instrument_token'], "exchange_segment": p['exchange_segment']}
            for p in positions_data
        ]

        try:
            quotes = self.client.quotes(instrument_tokens=tokens, quote_type="ltp")
            # S33: NEO API returns list directly, not {'data': [...]}
            # Handle both formats for safety
            if isinstance(quotes, list):
                ltp_data = quotes
            elif isinstance(quotes, dict):
                ltp_data = quotes.get('data', [])
            else:
                ltp_data = []
            # S27: Handle None in ltp_data
            if not isinstance(ltp_data, list):
                ltp_data = []
            ltp_map = {}
            for q in ltp_data:
                # S27: Use explicit None checks - token=0 and ltp=0 are technically valid
                token = q.get('instrument_token')
                if token is None:
                    token = q.get('token')
                ltp = q.get('ltp')
                if ltp is None:
                    ltp = q.get('last_price')
                if token is not None and ltp is not None:
                    try:
                        ltp_map[str(token)] = float(ltp)
                    except (ValueError, TypeError):
                        logger.warning(f"[TRAIL] Invalid LTP value for token {token}: {ltp}")
        except Exception as e:
            logger.warning(f"Failed to fetch LTPs: {e}")
            return

        # Phase 3: Calculate trail actions (no lock needed - working on snapshots)
        trail_actions = []  # List of (entry_order_id, new_sl, reason, is_cost_lock)

        for pdata in positions_data:
            ltp = ltp_map.get(str(pdata['instrument_token']), pdata['last_ltp'])

            # S38: Skip positions with invalid LTP - prevents false trail triggers
            if ltp is None or ltp <= 0:
                continue

            # Calculate current profit in points
            if pdata['side'] == 'LONG':
                profit_points = ltp - pdata['entry_price']
            else:
                profit_points = pdata['entry_price'] - ltp

            # Determine trail action based on mode
            new_sl = None
            reason = None
            is_cost_lock = False

            if pdata['trail_mode'] == TrailMode.LOCK_COST:
                if not pdata['cost_locked'] and profit_points >= pdata['lock_cost_trigger']:
                    new_sl = pdata['entry_price']
                    reason = "AUTO_LOCK_COST"
                    is_cost_lock = True

            elif pdata['trail_mode'] == TrailMode.AUTO_POINTS:
                if profit_points > 0 and pdata['trail_points'] > 0:
                    trail_increments = int(profit_points / pdata['trail_points'])
                    if trail_increments > 0:
                        if pdata['side'] == 'LONG':
                            candidate_sl = pdata['entry_price'] + ((trail_increments - 1) * pdata['trail_points'])
                            if candidate_sl > pdata['current_sl']:
                                new_sl = candidate_sl
                                reason = f"AUTO_TRAIL_{trail_increments}x"
                        else:
                            candidate_sl = pdata['entry_price'] - ((trail_increments - 1) * pdata['trail_points'])
                            if candidate_sl < pdata['current_sl']:
                                new_sl = candidate_sl
                                reason = f"AUTO_TRAIL_{trail_increments}x"

            elif pdata['trail_mode'] == TrailMode.AUTO_PERCENT:
                if profit_points > 0 and pdata['trail_percent'] > 0:
                    lock_amount = profit_points * (1 - pdata['trail_percent'] / 100)
                    if pdata['side'] == 'LONG':
                        candidate_sl = pdata['entry_price'] + lock_amount
                        if candidate_sl > pdata['current_sl']:
                            new_sl = candidate_sl
                            reason = f"AUTO_TRAIL_{pdata['trail_percent']}%"
                    else:
                        candidate_sl = pdata['entry_price'] - lock_amount
                        if candidate_sl < pdata['current_sl']:
                            new_sl = candidate_sl
                            reason = f"AUTO_TRAIL_{pdata['trail_percent']}%"

            # Store LTP update and optional trail action
            trail_actions.append({
                'entry_order_id': pdata['entry_order_id'],
                'ltp': ltp,
                'profit_points': profit_points,
                'new_sl': new_sl,
                'reason': reason,
                'is_cost_lock': is_cost_lock
            })

        # Phase 4: Update LTPs and execute trail actions
        for action in trail_actions:
            entry_id = action['entry_order_id']

            # Update LTP and profit tracking (quick lock)
            with self._lock:
                pos = self.positions.get(entry_id)
                if pos:
                    pos.last_ltp = action['ltp']
                    if action['profit_points'] > pos.highest_profit:
                        pos.highest_profit = action['profit_points']

            # Execute trail if needed (API calls - OUTSIDE lock)
            if action['new_sl'] is not None:
                # S36: Check position exists before calling _update_sl
                with self._lock:
                    if entry_id not in self.positions:
                        continue  # Position removed while we were processing

                # S36: Call _update_sl with entry_id - it's now thread-safe
                result = self._update_sl(entry_id, action['new_sl'], action['reason'])

                # If cost lock succeeded, update flag
                if result.get('success') and action['is_cost_lock']:
                    with self._lock:
                        pos = self.positions.get(entry_id)
                        if pos:
                            pos.cost_locked = True
                            # Copy values for callback outside lock
                            symbol = pos.symbol
                            entry_price = pos.entry_price

                    # Callback outside lock
                    if self.on_cost_locked:
                        self.on_cost_locked(symbol, entry_id, entry_price)

    # S36: Removed dead code methods (_process_lock_cost, _process_auto_points, _process_auto_percent)
    # These were never called - all logic is now inline in _process_auto_trails

    # ==================== CORE SL UPDATE ====================

    def _update_sl(self, entry_order_id: str, new_sl: float,
                   reason: str, force: bool = False) -> Dict[str, Any]:
        """Actually modify the SL order.

        S36: Refactored to be thread-safe - acquires lock internally, reads position
        data, releases lock for API calls, re-acquires lock for state updates.

        Args:
            entry_order_id: Entry order ID (position key)
            new_sl: New SL price
            reason: Reason for update (for logging)
            force: If True, bypass minimum distance check (for explicit user actions like BE)
        """
        try:
            # Phase 1: Read position data inside lock (fast)
            symbol = None
            sl_order_id = None
            old_sl = None
            last_ltp = None
            side = None
            quantity = None
            exchange_segment = None
            product = None

            with self._lock:
                pos = self.positions.get(entry_order_id)
                if not pos:
                    return {'success': False, 'error': 'Position not found'}

                # Copy all needed values for API calls
                symbol = pos.symbol
                sl_order_id = pos.sl_order_id
                old_sl = pos.current_sl
                last_ltp = pos.last_ltp
                side = pos.side
                quantity = pos.quantity
                exchange_segment = pos.exchange_segment
                product = pos.product

            # CRITICAL: Check if position is locked for exit (OCO/Exit processing)
            # If locked, Trail Manager should NOT modify - position is being closed
            if self.pos_tracker and self.pos_tracker.is_locked_for_exit(entry_order_id):
                logger.warning(f"[TRAIL] {symbol} (entry={entry_order_id}) is locked for exit - skipping modification")
                return {'success': False, 'error': 'Position locked for exit'}

            # S38: Round to tick size using integer arithmetic (was hardcoded 0.1)
            tick = get_tick_size(exchange_segment)
            new_sl = round_to_tick(new_sl, tick, direction='up')

            # CRITICAL FIX (E1): Skip unnecessary API call if new SL equals current SL
            # This prevents redundant modify calls from rapid button clicks or identical trails
            if abs(new_sl - old_sl) < tick:  # Within tick tolerance
                logger.debug(f"[TRAIL] {symbol}: Skipping modify - SL unchanged ({old_sl:.2f})")
                return {'success': True, 'new_sl': old_sl, 'order_id': sl_order_id, 'skipped': True}

            # Validate minimum distance (skip for explicit user actions)
            if not force:
                if side == 'LONG':
                    distance = last_ltp - new_sl
                else:
                    distance = new_sl - last_ltp

                if distance < self.min_sl_distance:
                    return {'success': False, 'error': f'SL too close to LTP (min: {self.min_sl_distance})'}

            # S34: If no SL order exists, PLACE new one instead of modify
            if not sl_order_id:
                logger.info(f"[TRAIL] {symbol}: No SL order - placing new SL @ {new_sl:.2f}")
                return self._place_new_sl_order(entry_order_id, new_sl, reason)

            # Phase 2: API calls outside lock (slow)
            # Fetch SL order to get order_type, price, quantity (required by NEO API)
            orders = self.client.order_report()
            order_list = orders.get('data', []) if orders else []
            sl_order = None
            for order in order_list:
                if order.get('nOrdNo') == sl_order_id:
                    sl_order = order
                    break

            if not sl_order:
                # S34: Order ID exists but order not found - place new SL
                logger.warning(f"[TRAIL] SL order {sl_order_id} not found in order book - placing new SL")
                return self._place_new_sl_order(entry_order_id, new_sl, reason)

            sl_order_type = sl_order.get('prcTp', sl_order.get('orderType', 'SL'))
            sl_order_price = sl_order.get('prc', sl_order.get('price', '0'))
            sl_order_qty = sl_order.get('qty', sl_order.get('quantity', str(quantity)))

            # Modify the SL order
            result = self.client.modify_order(
                order_id=sl_order_id,
                order_type=sl_order_type,
                price=str(sl_order_price),
                quantity=str(sl_order_qty),
                trigger_price=round_trigger_price(new_sl, exchange_segment),
                validity="DAY"
            )

            # Check if modification response indicates failure
            if result and result.get('stat', '').lower() in ['not_ok', 'error', 'rejected']:
                error_msg = result.get('errMsg') or result.get('message') or 'Modify failed'
                logger.error(f"[TRAIL] Modify failed: {error_msg}")
                return {'success': False, 'error': error_msg}

            # CRITICAL: Verify the modification with broker
            # The API may return OK but the order may not have been modified
            verified = self._verify_sl_modification(sl_order_id, new_sl)

            if not verified:
                logger.error(f"[TRAIL] CRITICAL: Modify returned OK but verification failed! "
                           f"Expected SL={new_sl}, broker may have different value")
                # DON'T update local state - keep old_sl as current
                return {
                    'success': False,
                    'error': 'Modify verification failed - broker SL may differ from expected',
                    'expected_sl': new_sl,
                    'actual_sl': 'unknown'
                }

            # Phase 3: Update state inside lock (fast)
            with self._lock:
                pos = self.positions.get(entry_order_id)
                if pos:  # Position may have been removed during API calls
                    pos.current_sl = new_sl
                    pos.trail_history.append({
                        'time': time.time(),
                        'old_sl': old_sl,
                        'new_sl': new_sl,
                        'reason': reason
                    })
                    # Limit trail history to prevent unbounded growth (keep last 100)
                    if len(pos.trail_history) > 100:
                        pos.trail_history = pos.trail_history[-100:]

            logger.info(f"[TRAIL] {symbol} (entry={entry_order_id}): SL {old_sl} -> {new_sl} ({reason}) [VERIFIED]")

            # S38: Propagate SL update to pos_tracker and oco_monitor
            # Without this, those modules have stale SL prices causing incorrect behavior
            if self.pos_tracker:
                self.pos_tracker.update_sl_order(entry_order_id, sl_order_id, new_sl)
            if self.oco_monitor:
                self.oco_monitor.update_sl_order(entry_order_id, sl_order_id, new_sl)

            # Notify (outside lock)
            if self.sound:
                self.sound.play('order_placed')

            if self.on_sl_updated:
                self.on_sl_updated(symbol, entry_order_id, old_sl, new_sl, reason)

            return {'success': True, 'new_sl': new_sl, 'order_id': sl_order_id}

        except Exception as e:
            logger.error(f"[TRAIL] Modify failed: {e}")

            # CRITICAL: Check if SL order still exists - if not, position is UNPROTECTED
            if sl_order_id:
                sl_status = self._check_sl_order_exists(sl_order_id)

                if sl_status == 'not_found' or sl_status in ['cancelled', 'rejected']:
                    logger.error(f"[TRAIL] CRITICAL: SL order {sl_order_id} is {sl_status}! "
                                f"Position {symbol} is UNPROTECTED!")

                    # Attempt to recreate SL at the new trigger price
                    recreate_result = self._recreate_sl_order(entry_order_id, new_sl, reason)
                    if recreate_result['success']:
                        return recreate_result

                    # Recreation failed - alert user
                    if self.telegram:
                        self.telegram.send(
                            f"🚨 CRITICAL: SL LOST for {symbol}!\n"
                            f"Original SL order {sl_order_id} is {sl_status}\n"
                            f"Recreation FAILED: {recreate_result.get('error', 'unknown')}\n"
                            f"Position is UNPROTECTED! Manual intervention required!"
                        )
                    if self.sound:
                        self.sound.play('error')

                    return {
                        'success': False,
                        'error': f'SL order lost ({sl_status}) and recreation failed',
                        'unprotected': True
                    }

            return {'success': False, 'error': str(e)}

    def _place_new_sl_order(self, entry_order_id: str, trigger_price: float,
                            reason: str) -> Dict[str, Any]:
        """
        S34: Place a NEW SL order when none exists.

        S36: Refactored to take entry_order_id for thread safety.
        Used by BE button when position has no SL yet.
        Delegates to _recreate_sl_order which handles the actual placement.
        """
        # Get symbol for logging
        with self._lock:
            pos = self.positions.get(entry_order_id)
            symbol = pos.symbol if pos else 'unknown'

        logger.info(f"[TRAIL] Placing NEW SL for {symbol} @ {trigger_price:.2f} ({reason})")
        result = self._recreate_sl_order(entry_order_id, trigger_price, reason)

        if result['success']:
            # Update trail history (inside lock)
            with self._lock:
                pos = self.positions.get(entry_order_id)
                if pos:
                    pos.trail_history.append({
                        'time': time.time(),
                        'old_sl': 0,
                        'new_sl': trigger_price,
                        'reason': f"NEW_SL:{reason}"
                    })

        return result

    def _check_sl_order_exists(self, order_id: str) -> str:
        """
        Check if an SL order exists and its status.

        Args:
            order_id: SL order ID to check

        Returns:
            Order status string: 'pending', 'complete', 'cancelled', 'rejected', 'not_found'
        """
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            for o in orders:
                if o.get('nOrdNo') == order_id:
                    status = o.get('ordSt', '').lower()
                    if status in ['open', 'pending', 'trigger pending', 'after market order req received']:
                        return 'pending'
                    elif status in ['complete', 'traded', 'filled']:
                        return 'complete'
                    elif status in ['cancelled', 'canceled']:
                        return 'cancelled'
                    elif status in ['rejected']:
                        return 'rejected'
                    return status

            return 'not_found'
        except Exception as e:
            logger.error(f"[TRAIL] Error checking SL order {order_id}: {e}")
            return 'unknown'

    def _recreate_sl_order(self, entry_order_id: str, trigger_price: float,
                          reason: str, max_retries: int = 3) -> Dict[str, Any]:
        """
        Recreate SL order when original is lost/cancelled.

        CRITICAL: This is a recovery mechanism when SL order disappears unexpectedly.

        S36: Refactored to take entry_order_id for thread safety. Reads position data
        inside lock, makes API calls outside lock.

        Args:
            entry_order_id: Entry order ID (position key)
            trigger_price: New SL trigger price
            reason: Reason for recreation (for logging)
            max_retries: Number of retry attempts

        Returns:
            Result dict with success status and new order ID
        """
        # Phase 1: Read position data inside lock
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            # Copy all needed values
            symbol = pos.symbol
            side = pos.side
            exchange_segment = pos.exchange_segment
            product = pos.product
            quantity = pos.quantity
            old_sl_id = pos.sl_order_id

        logger.warning(f"[TRAIL] Attempting to recreate SL for {symbol} @ {trigger_price}")

        # Determine transaction type for SL (opposite of position)
        sl_txn_type = 'S' if side == 'LONG' else 'B'

        # CRITICAL: ALL OPTIONS (nse_fo, bse_fo) do NOT allow SL-M orders - must use SL-L
        # S29: Kotak NEO rejects SL-M for ALL options, not just BSE
        # S31: Fixed floating-point precision bug using tick_utils
        sl_order_type, sl_limit_price = calculate_sl_limit_price(
            trigger_price=trigger_price,
            transaction_type=sl_txn_type,
            exchange_segment=exchange_segment,
            buffer_percent=0.5
        )
        if sl_order_type == "SL":
            logger.info(f"[TRAIL] Options detected - using SL-L: trigger={trigger_price}, limit={sl_limit_price}")

        sl_tag = None  # Track for cleanup on failure

        # Phase 2: API calls outside lock
        for attempt in range(max_retries):
            try:
                # S35: Generate unique tag for SL order using tracker
                sl_tag = self.order_tracker.generate_tag(OrderType.SL_RECREATE, symbol)
                self.order_tracker.register_order(
                    tag=sl_tag,
                    symbol=symbol,
                    transaction_type=sl_txn_type,
                    quantity=quantity,
                    api_order_type=sl_order_type,
                    price_type=OrderType.SL_RECREATE
                )

                sl_response = self.client.place_order(
                    exchange_segment=exchange_segment,
                    product=product,  # S23-C1: Use stored product (MIS/NRML)
                    price=sl_limit_price,
                    order_type=sl_order_type,
                    quantity=str(quantity),
                    validity="DAY",
                    trading_symbol=symbol,
                    transaction_type=sl_txn_type,
                    amo="NO",
                    trigger_price=round_trigger_price(trigger_price, exchange_segment),
                    tag=sl_tag  # S35: Use tracker-generated tag
                )

                new_sl_id = sl_response.get('nOrdNo') if sl_response else None

                if new_sl_id:
                    # S35: Confirm order with tracker
                    self.order_tracker.confirm_order(sl_tag, new_sl_id, sl_response)

                    # Phase 3: Update state inside lock
                    with self._lock:
                        pos = self.positions.get(entry_order_id)
                        if pos:  # Position may have been removed
                            pos.sl_order_id = new_sl_id
                            pos.current_sl = trigger_price

                    logger.info(f"[TRAIL] SL RECREATED for {symbol}: "
                               f"{old_sl_id} -> {new_sl_id} @ {trigger_price} ({reason})")

                    # Update position tracker if available
                    if self.pos_tracker:
                        self.pos_tracker.update_sl_order(entry_order_id, new_sl_id, trigger_price)

                    if self.sound:
                        self.sound.play('order_placed')

                    if self.telegram:
                        self.telegram.send(
                            f"✅ SL RECOVERED for {symbol}\n"
                            f"New SL @ {trigger_price}"
                        )

                    if self.on_sl_updated:
                        self.on_sl_updated(symbol, entry_order_id, 0, trigger_price, 'SL_RECOVERY')

                    return {'success': True, 'new_sl': trigger_price, 'order_id': new_sl_id}

                # S35: Mark order as failed with tracker
                self.order_tracker.mark_failed(sl_tag, "No order ID returned")
                logger.warning(f"[TRAIL] SL recreation attempt {attempt + 1}/{max_retries} "
                              f"returned no order ID")

            except Exception as e:
                # S35: Mark order as failed with tracker
                if sl_tag:
                    self.order_tracker.mark_failed(sl_tag, str(e))
                logger.error(f"[TRAIL] SL recreation attempt {attempt + 1}/{max_retries} failed: {e}")

            # Wait before retry (exponential backoff)
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))

        logger.error(f"[TRAIL] CRITICAL: Failed to recreate SL for {symbol} "
                    f"after {max_retries} attempts!")
        return {'success': False, 'error': f'SL recreation failed after {max_retries} attempts'}

    def _verify_sl_modification(self, order_id: str, expected_trigger: float,
                                 tolerance: float = 0.15) -> bool:
        """
        Verify that SL order modification actually took effect at broker.

        CRITICAL: Prevents mismatch between code state and broker reality.

        Args:
            order_id: SL order ID
            expected_trigger: Expected trigger price after modification
            tolerance: Price tolerance for comparison (default 0.15 for tick rounding)

        Returns:
            True if broker order has the expected trigger price
        """
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            for o in orders:
                if o.get('nOrdNo') == order_id:
                    # S27: Safe conversion with explicit None check
                    trgPrc = o.get('trgPrc')
                    try:
                        actual_trigger = float(trgPrc) if trgPrc is not None else 0.0
                    except (ValueError, TypeError):
                        actual_trigger = 0.0
                    status = o.get('ordSt', '').lower()

                    # If order is no longer pending, can't verify modification
                    if status in ['complete', 'traded', 'filled', 'cancelled', 'rejected']:
                        logger.warning(f"[TRAIL] SL order {order_id} is {status}, cannot verify")
                        # Return True - order executed, modification is moot
                        return True

                    # Check if trigger price matches expected (within tolerance)
                    if abs(actual_trigger - expected_trigger) <= tolerance:
                        logger.debug(f"[TRAIL] Verified: order {order_id} trigger={actual_trigger}")
                        return True
                    else:
                        logger.error(f"[TRAIL] MISMATCH: order {order_id} "
                                   f"expected={expected_trigger}, actual={actual_trigger}")
                        return False

            logger.warning(f"[TRAIL] Order {order_id} not found in order report")
            return False

        except Exception as e:
            logger.error(f"[TRAIL] Verification error: {e}")
            # On verification error, be conservative - assume it failed
            return False

    def get_position_info(self, entry_order_id: str) -> Optional[Dict[str, Any]]:
        """Get trailing position info for GUI."""
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if not pos:
                return None

            return {
                'entry_order_id': pos.entry_order_id,
                'symbol': pos.symbol,
                'entry': pos.entry_price,
                'current_sl': pos.current_sl,
                'side': pos.side,
                'mode': pos.trail_mode.value,
                'cost_locked': pos.cost_locked,
                'highest_profit': pos.highest_profit,
                'trail_count': len(pos.trail_history),
                'last_ltp': pos.last_ltp
            }

    def get_positions_by_symbol(self, symbol: str) -> List[Dict[str, Any]]:
        """Get all trailing positions for a symbol.

        NOTE: Must NOT call get_position_info() here to avoid deadlock
        (both methods acquire self._lock, and threading.Lock is not reentrant).
        """
        with self._lock:
            result = []
            for entry_id, pos in self.positions.items():
                if pos.symbol == symbol:
                    result.append({
                        'entry_order_id': pos.entry_order_id,
                        'symbol': pos.symbol,
                        'entry': pos.entry_price,
                        'current_sl': pos.current_sl,
                        'side': pos.side,
                        'mode': pos.trail_mode.value,
                        'cost_locked': pos.cost_locked,
                        'highest_profit': pos.highest_profit,
                        'trail_count': len(pos.trail_history),
                        'last_ltp': pos.last_ltp
                    })
            return result

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Get all trailing positions.

        NOTE: Must NOT call get_position_info() here to avoid deadlock
        (both methods acquire self._lock, and threading.Lock is not reentrant).
        """
        with self._lock:
            result = []
            for entry_id, pos in self.positions.items():
                result.append({
                    'entry_order_id': pos.entry_order_id,
                    'symbol': pos.symbol,
                    'entry': pos.entry_price,
                    'current_sl': pos.current_sl,
                    'side': pos.side,
                    'mode': pos.trail_mode.value,
                    'cost_locked': pos.cost_locked,
                    'highest_profit': pos.highest_profit,
                    'trail_count': len(pos.trail_history),
                    'last_ltp': pos.last_ltp
                })
            return result

    def update_sl_order_id(self, entry_order_id: str, new_sl_order_id: str):
        """Update SL order ID (after modification)."""
        with self._lock:
            if entry_order_id in self.positions:
                self.positions[entry_order_id].sl_order_id = new_sl_order_id

    def has_position(self, entry_order_id: str) -> bool:
        """Check if entry_order_id is being trailed."""
        with self._lock:
            return entry_order_id in self.positions

    def has_position_for_symbol(self, symbol: str) -> bool:
        """Check if any position for symbol is being trailed."""
        with self._lock:
            return any(p.symbol == symbol for p in self.positions.values())

    # ==================== Symbol-based methods (for GUI compatibility) ====================

    def trail_to_cost_by_symbol(self, symbol: str, current_ltp: float = None) -> Dict[str, Any]:
        """Trail all positions for a symbol to cost. Returns first result."""
        with self._lock:
            entry_ids = [eid for eid, p in self.positions.items() if p.symbol == symbol]

        if not entry_ids:
            return {'success': False, 'error': 'No positions found for symbol'}

        # Trail all positions for this symbol
        results = []
        for entry_id in entry_ids:
            result = self.trail_to_cost(entry_id, current_ltp)
            results.append(result)

        # Return first successful result or first failure
        for r in results:
            if r['success']:
                return r
        return results[0] if results else {'success': False, 'error': 'No positions'}

    def trail_by_points_by_symbol(self, symbol: str, points: float) -> Dict[str, Any]:
        """Trail all positions for a symbol by points. Returns first result."""
        with self._lock:
            entry_ids = [eid for eid, p in self.positions.items() if p.symbol == symbol]

        if not entry_ids:
            return {'success': False, 'error': 'No positions found for symbol'}

        # Trail all positions for this symbol
        results = []
        for entry_id in entry_ids:
            result = self.trail_by_points(entry_id, points)
            results.append(result)

        # Return first successful result or first failure
        for r in results:
            if r['success']:
                return r
        return results[0] if results else {'success': False, 'error': 'No positions'}

    def get_first_entry_id_for_symbol(self, symbol: str) -> Optional[str]:
        """Get first entry_order_id for a symbol."""
        with self._lock:
            for entry_id, p in self.positions.items():
                if p.symbol == symbol:
                    return entry_id
            return None

    def update_quantity_by_symbol(self, symbol: str, new_quantity: int):
        """
        Thread-safe update of quantity for all positions of a symbol.

        Used after partial exits when SL adjustment succeeds.
        """
        with self._lock:
            updated = 0
            for entry_id, pos in self.positions.items():
                if pos.symbol == symbol:
                    pos.quantity = new_quantity
                    updated += 1
            if updated:
                logger.info(f"[TRAIL] Updated quantity to {new_quantity} for {updated} positions of {symbol}")

    # ==================== S17-M2: Thread-safe accessor methods ====================

    def update_current_sl(self, entry_order_id: str, sl_price: Optional[float]):
        """
        Thread-safe update of current SL price.

        Args:
            entry_order_id: Position entry order ID
            sl_price: New SL price (or None to clear)
        """
        with self._lock:
            if entry_order_id in self.positions:
                self.positions[entry_order_id].current_sl = sl_price

    def update_last_ltp(self, entry_order_id: str, ltp: float):
        """
        Thread-safe update of last LTP for a position.

        Args:
            entry_order_id: Position entry order ID
            ltp: Last traded price
        """
        with self._lock:
            if entry_order_id in self.positions:
                self.positions[entry_order_id].last_ltp = ltp

    def clear_sl_tracking(self, entry_order_id: str):
        """
        Thread-safe clearing of SL order tracking.

        Clears sl_order_id and current_sl. Used when SL order is cancelled
        without replacement.

        Args:
            entry_order_id: Position entry order ID
        """
        with self._lock:
            if entry_order_id in self.positions:
                self.positions[entry_order_id].sl_order_id = None
                self.positions[entry_order_id].current_sl = None

    def clear_all(self):
        """
        Thread-safe clearing of all trailing positions.

        Used when cancelling all orders - must be called instead of
        directly accessing positions.clear() to prevent RuntimeError
        if auto-trail thread is iterating.
        """
        with self._lock:
            count = len(self.positions)
            self.positions.clear()
            if count > 0:
                logger.info(f"[TRAIL] Cleared all {count} trailing positions")

    def get_position_for_ltp_fetch(self, entry_order_id: str) -> Optional[tuple]:
        """
        Thread-safe getter for LTP fetch parameters.

        Returns immutable tuple of (instrument_token, exchange_segment) to avoid
        race conditions when GUI code needs to fetch fresh LTP.

        Args:
            entry_order_id: Position entry order ID

        Returns:
            Tuple of (instrument_token, exchange_segment) or None if not found
        """
        with self._lock:
            pos = self.positions.get(entry_order_id)
            if pos:
                return (pos.instrument_token, pos.exchange_segment)
            return None

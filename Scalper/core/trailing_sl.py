"""
NEO Trade Terminal - Trailing Stop Loss Manager

Provides multiple ways to trail SL:
1. Manual one-click trail (button click moves SL to cost/LTP-X)
2. Auto-trail based on LTP movement
3. Quick keyboard shortcuts for trail increments
"""

import threading
import time
from typing import Dict, Optional, Callable, Any, List
from dataclasses import dataclass, field
from enum import Enum
import logging

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
    symbol: str
    exchange_segment: str
    entry_price: float
    quantity: int
    side: str                    # 'LONG' or 'SHORT'
    current_sl: float
    sl_order_id: str
    instrument_token: str = ""   # Required for LTP fetch in auto-trail
    target_order_id: Optional[str] = None

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
    """Manages trailing stop losses for positions."""

    def __init__(self, neo_client, order_manager=None,
                 sound_mgr=None, telegram_mgr=None, config: Dict[str, Any] = None):
        self.client = neo_client
        self.order_mgr = order_manager
        self.sound = sound_mgr
        self.telegram = telegram_mgr
        self.config = config or {}

        self.positions: Dict[str, TrailingPosition] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._check_interval = 0.5  # Check every 500ms for fast trailing

        # Config
        trail_config = self.config.get('trailing_sl', {})
        self.min_sl_distance = trail_config.get('min_sl_distance', 5)
        self.trail_buttons = trail_config.get('trail_buttons', [10, 25, 50])
        self.ltp_buffer = trail_config.get('ltp_buffer', 20)

        # Callbacks for GUI updates
        self.on_sl_updated: Optional[Callable] = None
        self.on_cost_locked: Optional[Callable] = None

    def add_position(self, symbol: str, exchange_segment: str,
                    entry_price: float, quantity: int, side: str,
                    sl_price: float, sl_order_id: str,
                    instrument_token: str = "",
                    target_order_id: str = None,
                    trail_mode: TrailMode = TrailMode.MANUAL,
                    trail_points: float = 0,
                    trail_percent: float = 0,
                    lock_cost_trigger: float = 0) -> bool:
        """
        Register position for trailing.

        Args:
            symbol: Trading symbol
            exchange_segment: Exchange segment
            entry_price: Entry price
            quantity: Position quantity
            side: 'LONG' or 'SHORT'
            sl_price: Current SL price
            sl_order_id: SL order ID
            instrument_token: Instrument token for LTP fetch (required for auto-trail)
            target_order_id: Target order ID (optional)
            trail_mode: Trailing mode
            trail_points: Points to trail by (for AUTO_POINTS)
            trail_percent: Percent to trail by (for AUTO_PERCENT)
            lock_cost_trigger: Profit trigger for cost lock (for LOCK_COST)

        Returns:
            True if added successfully
        """
        with self._lock:
            pos = TrailingPosition(
                symbol=symbol,
                exchange_segment=exchange_segment,
                entry_price=entry_price,
                quantity=quantity,
                side=side.upper(),
                current_sl=sl_price,
                sl_order_id=sl_order_id,
                instrument_token=instrument_token,
                target_order_id=target_order_id,
                trail_mode=trail_mode,
                trail_points=trail_points,
                trail_percent=trail_percent,
                lock_cost_trigger=lock_cost_trigger,
                last_ltp=entry_price
            )
            self.positions[symbol] = pos
            logger.info(f"[TRAIL] Added {symbol} mode={trail_mode.value}")
            return True

    def remove_position(self, symbol: str):
        """Remove position from trailing."""
        with self._lock:
            if symbol in self.positions:
                del self.positions[symbol]
                logger.info(f"[TRAIL] Removed {symbol}")

    # ==================== MANUAL TRAIL METHODS ====================

    def trail_to_cost(self, symbol: str) -> Dict[str, Any]:
        """
        One-click: Move SL to entry price (cost).
        Use when position is in profit and want to lock breakeven.

        Args:
            symbol: Symbol to trail

        Returns:
            Result dict with success status
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            new_sl = pos.entry_price

            # CRITICAL: Validate that trailing to cost is favorable (position is in profit)
            # Otherwise the SL would trigger immediately or be worse than current SL
            if pos.side == 'LONG':
                # For LONG: Entry must be below LTP to be a valid SL
                if pos.last_ltp <= pos.entry_price:
                    return {
                        'success': False,
                        'error': f'Position not in profit (LTP {pos.last_ltp:.2f} <= Entry {pos.entry_price:.2f}). Cannot trail to cost.'
                    }
                # Also verify it's better than current SL
                if new_sl <= pos.current_sl:
                    return {
                        'success': False,
                        'error': f'Cost ({new_sl:.2f}) is not better than current SL ({pos.current_sl:.2f})'
                    }
            else:  # SHORT
                # For SHORT: Entry must be above LTP to be a valid SL
                if pos.last_ltp >= pos.entry_price:
                    return {
                        'success': False,
                        'error': f'Position not in profit (LTP {pos.last_ltp:.2f} >= Entry {pos.entry_price:.2f}). Cannot trail to cost.'
                    }
                # Also verify it's better than current SL
                if new_sl >= pos.current_sl:
                    return {
                        'success': False,
                        'error': f'Cost ({new_sl:.2f}) is not better than current SL ({pos.current_sl:.2f})'
                    }

            return self._update_sl(pos, new_sl, "TRAIL_TO_COST")

    def trail_by_points(self, symbol: str, points: float) -> Dict[str, Any]:
        """
        One-click: Move SL up/down by X points from current SL.
        Positive = tighter SL (favorable), Negative = wider SL.

        For LONG: trail_by_points(+10) moves SL from 100 to 110
        For SHORT: trail_by_points(+10) moves SL from 100 to 90

        Args:
            symbol: Symbol to trail
            points: Points to trail by

        Returns:
            Result dict
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            if pos.side == 'LONG':
                new_sl = pos.current_sl + points
            else:
                new_sl = pos.current_sl - points

            return self._update_sl(pos, new_sl, f"TRAIL_+{points}pts")

    def trail_to_price(self, symbol: str, new_sl: float) -> Dict[str, Any]:
        """
        Set SL to specific price.

        Args:
            symbol: Symbol to trail
            new_sl: New SL price

        Returns:
            Result dict
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return {'success': False, 'error': 'Position not found'}

            return self._update_sl(pos, new_sl, f"TRAIL_TO_{new_sl}")

    def trail_to_ltp_minus(self, symbol: str, buffer_points: float,
                          current_ltp: float) -> Dict[str, Any]:
        """
        One-click: Move SL to (LTP - buffer).
        Useful for quick trailing in momentum.

        For LONG: SL = LTP - buffer
        For SHORT: SL = LTP + buffer

        Args:
            symbol: Symbol to trail
            buffer_points: Buffer from LTP
            current_ltp: Current LTP

        Returns:
            Result dict
        """
        with self._lock:
            pos = self.positions.get(symbol)
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

            return self._update_sl(pos, new_sl, f"TRAIL_LTP-{buffer_points}")

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
        """Background loop for auto-trailing."""
        while self._running:
            try:
                self._process_auto_trails()
            except Exception as e:
                logger.error(f"[TRAIL] Error: {e}")
            time.sleep(self._check_interval)

    def _process_auto_trails(self):
        """Process all positions for auto-trailing."""
        # Get positions to check
        positions_to_check = []
        with self._lock:
            for pos in self.positions.values():
                if pos.trail_mode != TrailMode.MANUAL:
                    # Only include if we have valid instrument_token for LTP fetch
                    if pos.instrument_token:
                        positions_to_check.append(pos)
                    else:
                        logger.warning(f"[TRAIL] Skipping {pos.symbol} - no instrument_token for LTP fetch")

        if not positions_to_check:
            return

        # Fetch LTPs using proper instrument_token (not symbol string)
        tokens = [
            {"instrument_token": pos.instrument_token, "exchange_segment": pos.exchange_segment}
            for pos in positions_to_check
        ]

        try:
            quotes = self.client.quotes(instrument_tokens=tokens, quote_type="ltp")
            ltp_data = quotes.get('data', []) if quotes else []
            ltp_map = {}
            for q in ltp_data:
                token = q.get('instrument_token') or q.get('token')
                ltp = q.get('ltp') or q.get('last_price')
                if token and ltp:
                    ltp_map[str(token)] = float(ltp)
        except Exception as e:
            logger.warning(f"Failed to fetch LTPs: {e}")
            return

        # Process each position
        with self._lock:
            for pos in positions_to_check:
                # Look up LTP by instrument_token (converted to string for consistent key matching)
                ltp = ltp_map.get(str(pos.instrument_token), pos.last_ltp)
                pos.last_ltp = ltp

                # Calculate current profit in points
                if pos.side == 'LONG':
                    profit_points = ltp - pos.entry_price
                else:
                    profit_points = pos.entry_price - ltp

                # Track peak profit
                if profit_points > pos.highest_profit:
                    pos.highest_profit = profit_points

                # Process based on mode
                if pos.trail_mode == TrailMode.LOCK_COST:
                    self._process_lock_cost(pos, profit_points)

                elif pos.trail_mode == TrailMode.AUTO_POINTS:
                    self._process_auto_points(pos, profit_points, ltp)

                elif pos.trail_mode == TrailMode.AUTO_PERCENT:
                    self._process_auto_percent(pos, profit_points, ltp)

    def _process_lock_cost(self, pos: TrailingPosition, profit_points: float):
        """Lock SL to cost when trigger profit reached."""
        if pos.cost_locked:
            return

        if profit_points >= pos.lock_cost_trigger:
            result = self._update_sl(pos, pos.entry_price, "AUTO_LOCK_COST")
            if result['success']:
                pos.cost_locked = True
                if self.on_cost_locked:
                    self.on_cost_locked(pos.symbol, pos.entry_price)

    def _process_auto_points(self, pos: TrailingPosition,
                            profit_points: float, ltp: float):
        """
        Auto trail by fixed points.
        When profit increases by trail_points, move SL up by same amount.
        """
        if profit_points <= 0 or pos.trail_points <= 0:
            return

        # Calculate how many trail increments we've achieved
        trail_increments = int(profit_points / pos.trail_points)

        if trail_increments > 0:
            if pos.side == 'LONG':
                new_sl = pos.entry_price + ((trail_increments - 1) * pos.trail_points)
            else:
                new_sl = pos.entry_price - ((trail_increments - 1) * pos.trail_points)

            # Only trail if new SL is better
            if pos.side == 'LONG' and new_sl > pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{trail_increments}x")
            elif pos.side == 'SHORT' and new_sl < pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{trail_increments}x")

    def _process_auto_percent(self, pos: TrailingPosition,
                             profit_points: float, ltp: float):
        """
        Auto trail by percentage of profit.
        SL = Entry + (profit * (1 - trail_percent))
        """
        if profit_points <= 0 or pos.trail_percent <= 0:
            return

        # Calculate trailing amount (lock X% of profit)
        lock_amount = profit_points * (1 - pos.trail_percent / 100)

        if pos.side == 'LONG':
            new_sl = pos.entry_price + lock_amount
            if new_sl > pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{pos.trail_percent}%")
        else:
            new_sl = pos.entry_price - lock_amount
            if new_sl < pos.current_sl:
                self._update_sl(pos, new_sl, f"AUTO_TRAIL_{pos.trail_percent}%")

    # ==================== CORE SL UPDATE ====================

    def _update_sl(self, pos: TrailingPosition, new_sl: float,
                   reason: str) -> Dict[str, Any]:
        """Actually modify the SL order."""
        try:
            # Round to tick size (0.05 for options)
            new_sl = round(new_sl / 0.05) * 0.05

            # Validate minimum distance
            if pos.side == 'LONG':
                distance = pos.last_ltp - new_sl
            else:
                distance = new_sl - pos.last_ltp

            if distance < self.min_sl_distance:
                return {'success': False, 'error': f'SL too close to LTP (min: {self.min_sl_distance})'}

            # Modify the SL order
            result = self.client.modify_order(
                order_id=pos.sl_order_id,
                trigger_price=str(new_sl),
                validity="DAY"
            )

            old_sl = pos.current_sl
            pos.current_sl = new_sl
            pos.trail_history.append({
                'time': time.time(),
                'old_sl': old_sl,
                'new_sl': new_sl,
                'reason': reason
            })

            logger.info(f"[TRAIL] {pos.symbol}: SL {old_sl} -> {new_sl} ({reason})")

            # Notify
            if self.sound:
                self.sound.play('order_placed')

            if self.on_sl_updated:
                self.on_sl_updated(pos.symbol, old_sl, new_sl, reason)

            return {'success': True, 'new_sl': new_sl, 'order_id': pos.sl_order_id}

        except Exception as e:
            logger.error(f"[TRAIL] Failed: {e}")
            return {'success': False, 'error': str(e)}

    def get_position_info(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get trailing position info for GUI."""
        with self._lock:
            pos = self.positions.get(symbol)
            if not pos:
                return None

            return {
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

    def get_all_positions(self) -> List[Dict[str, Any]]:
        """Get all trailing positions."""
        with self._lock:
            return [self.get_position_info(sym) for sym in self.positions.keys()]

    def update_sl_order_id(self, symbol: str, new_sl_order_id: str):
        """Update SL order ID (after modification)."""
        with self._lock:
            if symbol in self.positions:
                self.positions[symbol].sl_order_id = new_sl_order_id

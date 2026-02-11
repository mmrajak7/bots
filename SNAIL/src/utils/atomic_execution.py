"""
SNAIL Atomic Iron Fly Execution

Provides transactionally-safe execution of Iron Fly orders with rollback capability.

Key Safety Guarantee: At ANY failure point during execution, the resulting position
has DEFINED RISK (never unlimited risk from naked shorts).

Execution Sequence (Interleaved for Safety):
1. BUY Wing CE    → If fails: No position, abort ✓
2. SELL Straddle CE → If fails: Just long wing (limited loss) ✓
3. BUY Wing PE    → If fails: Bear call spread (defined risk) ✓
4. SELL Straddle PE → If fails: Bear call + long put (defined risk) ✓

@file        atomic_execution.py
@description Transactionally-safe Iron Fly order execution
@author      SNAIL Development Team
@created     2025-12-12
@version     1.0.0
@references  ISSUES.md A-C002
"""

import threading
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

from src.api.kite_client import SNAILKiteClient, Quote
from src.utils.order_helpers import round_to_tick, TICK_SIZE, ORDER_POLL_INTERVAL


# =============================================================================
# CONSTANTS
# =============================================================================

# Slippage settings for atomic execution (more aggressive for guaranteed fills)
ATOMIC_SLIPPAGE_TICKS = 3  # Slightly more aggressive for safety

# Timeout for individual leg execution
LEG_TIMEOUT_SECONDS = 30

# Timeout for rollback operations
ROLLBACK_TIMEOUT_SECONDS = 60

# Required legs for a complete Iron Fly
REQUIRED_LEGS = {'wing_ce', 'straddle_ce', 'wing_pe', 'straddle_pe'}

# Default lot sizes by instrument (can be overridden by config)
from src.utils.calculations import NIFTY_LOT_SIZE
DEFAULT_LOT_SIZES = {
    'NIFTY': NIFTY_LOT_SIZE,
    'BANKNIFTY': 15,
    'FINNIFTY': 40,
}
DEFAULT_LOT_SIZE = NIFTY_LOT_SIZE  # Fallback if instrument not found

# Module-level lock for concurrent execution protection (thread-safe initialization)
_EXECUTION_LOCK = threading.Lock()


# =============================================================================
# ENUMS
# =============================================================================

class LegStatus(Enum):
    """Status of an individual leg execution."""
    PENDING = "pending"
    PLACED = "placed"
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLED_BACK = "rolled_back"


class PositionState(Enum):
    """
    Possible position states after partial execution.

    All states except NAKED_* have DEFINED RISK.
    """
    NO_POSITION = "no_position"
    LONG_CALL_ONLY = "long_call_only"                           # Safe: max loss = premium
    LONG_PUT_ONLY = "long_put_only"                             # Safe: max loss = premium
    BEAR_CALL_SPREAD = "bear_call_spread"                       # Safe: defined risk
    BULL_PUT_SPREAD = "bull_put_spread"                         # Safe: defined risk
    BEAR_CALL_SPREAD_PLUS_LONG_PUT = "bear_call_spread_plus_long_put"  # Safe
    COMPLETE_IRON_FLY = "complete_iron_fly"                     # Safe: defined risk
    NAKED_SHORT_CALL = "naked_short_call"                       # DANGER! Unlimited risk
    NAKED_SHORT_PUT = "naked_short_put"                         # DANGER! Unlimited risk
    NAKED_STRADDLE = "naked_straddle"                           # EXTREME DANGER!
    UNKNOWN = "unknown"


# Dangerous states that require immediate rollback
DANGEROUS_STATES = {
    PositionState.NAKED_SHORT_CALL,
    PositionState.NAKED_SHORT_PUT,
    PositionState.NAKED_STRADDLE
}


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class ExecutedLeg:
    """
    Result of a single leg execution.

    Attributes:
        leg_type: Type of leg (wing_ce, straddle_ce, wing_pe, straddle_pe)
        symbol: Trading symbol
        txn_type: Transaction type (BUY/SELL)
        status: Current status
        order_id: Kite order ID (if placed)
        fill_price: Average fill price (if filled)
        quantity: Order quantity
        error: Error message (if failed)
    """
    leg_type: str
    symbol: str
    txn_type: str
    status: LegStatus
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    quantity: Optional[int] = None
    error: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class RollbackLegResult:
    """Result of rolling back a single leg."""
    leg_type: str
    success: bool
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    error: Optional[str] = None


@dataclass
class RollbackResult:
    """Result of rollback operation."""
    success: bool
    legs: List[RollbackLegResult] = field(default_factory=list)
    total_slippage: float = 0.0


@dataclass
class AtomicExecutionResult:
    """
    Result of atomic Iron Fly execution.

    Attributes:
        success: True if all 4 legs executed successfully
        legs: List of executed legs
        position_state: Current position state
        failed_leg: Leg that caused failure (if any)
        error: Error message
        rollback_performed: Whether rollback was executed
        rollback_result: Rollback details (if performed)
        net_credit: Net credit received (if successful)
        total_slippage: Total slippage across all legs
    """
    success: bool
    legs: List[ExecutedLeg]
    position_state: PositionState
    failed_leg: Optional[str] = None
    error: Optional[str] = None
    rollback_performed: bool = False
    rollback_result: Optional[RollbackResult] = None
    net_credit: Optional[float] = None
    total_slippage: float = 0.0


@dataclass
class PreValidationResult:
    """Result of pre-flight validation."""
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# ATOMIC IRON FLY EXECUTOR
# =============================================================================

class AtomicIronFlyExecutor:
    """
    Execute Iron Fly with transactional safety guarantees.

    Key Safety Features:
    1. Interleaved execution order (wings before straddles)
    2. At any failure point, position has defined risk
    3. Automatic rollback if dangerous state detected
    4. Pre-validation of margin and liquidity
    5. Comprehensive logging for audit trail

    Usage:
        executor = AtomicIronFlyExecutor(kite_client, config)
        result = executor.execute(symbols, quantity, quotes)

        if result.success:
            # Full iron fly executed
        elif result.position_state != PositionState.NO_POSITION:
            # Partial position - handle accordingly
    """

    # Execution order - CRITICAL: wings before their corresponding straddle
    # This ensures we NEVER have unlimited risk at any failure point
    SAFE_EXECUTION_ORDER: List[Tuple[str, str]] = [
        ('wing_ce', 'BUY'),       # Step 1: Protection first
        ('straddle_ce', 'SELL'),  # Step 2: Now protected on CE side
        ('wing_pe', 'BUY'),       # Step 3: Protection first
        ('straddle_pe', 'SELL'),  # Step 4: Now protected on PE side
    ]

    def __init__(
        self,
        kite: SNAILKiteClient,
        config: Optional[Dict[str, Any]] = None,
        telegram_alerts: Optional[Any] = None,
        lot_size: Optional[int] = None,
        instrument: str = 'NIFTY'
    ):
        """
        Initialize atomic executor.

        Args:
            kite: Kite client instance
            config: Configuration dictionary
            telegram_alerts: Telegram alerts instance for critical notifications
            lot_size: Lot size for the instrument (overrides default)
            instrument: Instrument name for default lot size lookup
        """
        self.kite = kite
        self.config = config or {}
        self.telegram = telegram_alerts
        self.executed_legs: List[ExecutedLeg] = []
        self.instrument = instrument

        # Determine lot size: explicit > config > default lookup > fallback
        if lot_size is not None:
            self.lot_size = lot_size
        else:
            config_lot_size = self.config.get('trading', {}).get('lot_size')
            if config_lot_size:
                self.lot_size = int(config_lot_size)
            else:
                self.lot_size = DEFAULT_LOT_SIZES.get(instrument, DEFAULT_LOT_SIZE)

        # Get slippage from config or use default
        self.slippage_ticks = self.config.get('trading', {}).get(
            'entry', {}
        ).get('slippage_ticks', ATOMIC_SLIPPAGE_TICKS)

    def execute(
        self,
        symbols: Dict[str, str],
        quantity: int,
        quotes: Dict[str, Quote]
    ) -> AtomicExecutionResult:
        """
        Execute Iron Fly with atomic safety guarantees.

        Args:
            symbols: Dict mapping leg_type to trading symbol
            quantity: Order quantity (total contracts, e.g., 65 for 1 NIFTY lot)
            quotes: Dict mapping leg_type to Quote

        Returns:
            AtomicExecutionResult with execution details
        """
        # Use module-level lock to prevent concurrent executions (AE-010 fix)
        acquired = _EXECUTION_LOCK.acquire(blocking=False)
        if not acquired:
            logger.error("Another atomic execution is already in progress")
            return AtomicExecutionResult(
                success=False,
                error="Concurrent execution blocked - another atomic execution in progress",
                legs=[],
                position_state=PositionState.NO_POSITION
            )

        try:
            return self._execute_internal(symbols, quantity, quotes)
        finally:
            _EXECUTION_LOCK.release()

    def _execute_internal(
        self,
        symbols: Dict[str, str],
        quantity: int,
        quotes: Dict[str, Quote]
    ) -> AtomicExecutionResult:
        """Internal execution logic (called under lock)."""
        self.executed_legs = []

        # Basic quantity validation
        if quantity <= 0:
            logger.error(f"Invalid quantity: {quantity}")
            return AtomicExecutionResult(
                success=False,
                error=f"Invalid quantity: {quantity} (must be > 0)",
                legs=[],
                position_state=PositionState.NO_POSITION
            )

        logger.info("=" * 60)
        logger.info("ATOMIC IRON FLY EXECUTION - STARTING")
        logger.info(f"Quantity: {quantity}, Slippage: {self.slippage_ticks} ticks, Lot size: {self.lot_size}")
        logger.info("=" * 60)

        # Pre-validation
        validation = self._validate_preconditions(symbols, quantity, quotes)
        if not validation.valid:
            logger.error(f"Pre-validation failed: {validation.errors}")
            return AtomicExecutionResult(
                success=False,
                error=f"Pre-validation failed: {'; '.join(validation.errors)}",
                legs=[],
                position_state=PositionState.NO_POSITION
            )

        if validation.warnings:
            for warning in validation.warnings:
                logger.warning(f"Pre-validation warning: {warning}")

        # Execute in safe order
        for leg_type, txn_type in self.SAFE_EXECUTION_ORDER:
            symbol = symbols[leg_type]
            quote = quotes[leg_type]

            logger.info(f"Step {len(self.executed_legs) + 1}/4: {txn_type} {leg_type}")

            result = self._execute_leg(
                leg_type=leg_type,
                symbol=symbol,
                txn_type=txn_type,
                quantity=quantity,
                quote=quote
            )

            self.executed_legs.append(result)

            if result.status == LegStatus.FAILED:
                return self._handle_failure(leg_type, result)

        # All legs executed successfully
        logger.info("=" * 60)
        logger.info("ATOMIC IRON FLY EXECUTION - SUCCESS")
        logger.info("=" * 60)

        # Calculate net credit
        straddle_credit = sum(
            leg.fill_price for leg in self.executed_legs
            if leg.leg_type.startswith('straddle') and leg.fill_price
        )
        wing_debit = sum(
            leg.fill_price for leg in self.executed_legs
            if leg.leg_type.startswith('wing') and leg.fill_price
        )
        net_credit = straddle_credit - wing_debit

        return AtomicExecutionResult(
            success=True,
            legs=self.executed_legs,
            position_state=PositionState.COMPLETE_IRON_FLY,
            net_credit=net_credit
        )

    def _execute_leg(
        self,
        leg_type: str,
        symbol: str,
        txn_type: str,
        quantity: int,
        quote: Quote
    ) -> ExecutedLeg:
        """
        Execute single leg with retry logic.

        Args:
            leg_type: Type of leg
            symbol: Trading symbol
            txn_type: BUY or SELL
            quantity: Order quantity
            quote: Current quote

        Returns:
            ExecutedLeg with execution result
        """
        # PRE-FLIGHT: Check for ghost orders on this symbol from earlier failed attempts
        # This prevents double orders when place_order() succeeds on exchange but
        # throws network error on client side, then we retry
        ghost = self._check_ghost_orders(symbol, txn_type, quantity)
        if ghost:
            logger.warning(
                f"  ⚠ GHOST ORDER detected for {leg_type}: order_id={ghost['order_id']}, "
                f"status={ghost['status']}, filled_qty={ghost.get('filled_quantity', 0)}"
            )
            filled_qty = ghost.get('filled_quantity', 0) or 0
            if ghost['status'] == 'COMPLETE' or filled_qty >= quantity:
                # Ghost order already filled - use it instead of placing new order
                avg_price = ghost.get('average_price', 0)
                logger.info(f"  ✓ Using ghost order fill @ {avg_price:.2f}")
                return ExecutedLeg(
                    leg_type=leg_type,
                    symbol=symbol,
                    txn_type=txn_type,
                    status=LegStatus.FILLED,
                    order_id=ghost['order_id'],
                    fill_price=avg_price,
                    quantity=filled_qty or quantity
                )
            elif ghost['status'] in ('OPEN', 'PENDING', 'TRIGGER PENDING'):
                # Ghost order still open - wait for it instead of placing new one
                logger.info(f"  Waiting for ghost order {ghost['order_id']} to fill...")
                fill_result = self._wait_for_fill(ghost['order_id'], LEG_TIMEOUT_SECONDS)
                if fill_result['filled']:
                    avg_price = fill_result['avg_price']
                    actual_qty = fill_result.get('filled_quantity', quantity)
                    logger.info(f"  ✓ Ghost order filled @ {avg_price:.2f}")
                    return ExecutedLeg(
                        leg_type=leg_type,
                        symbol=symbol,
                        txn_type=txn_type,
                        status=LegStatus.FILLED,
                        order_id=ghost['order_id'],
                        fill_price=avg_price,
                        quantity=actual_qty
                    )
                # Ghost order didn't fill - cancel it and place fresh
                self._cancel_order_safe(ghost['order_id'])
                logger.info(f"  Ghost order cancelled, placing fresh order")

        # Calculate limit price based on transaction type
        if txn_type == 'BUY':
            # For buys (wings), use ask + slippage (aggressive to ensure fill)
            base_price = quote.ask
            price = round_to_tick(base_price + (self.slippage_ticks * TICK_SIZE))
        else:
            # For sells (straddle), use bid - slippage (aggressive to ensure fill)
            base_price = quote.bid
            price = round_to_tick(base_price - (self.slippage_ticks * TICK_SIZE))

        logger.info(f"  {leg_type}: {symbol} {txn_type} {quantity} @ {price:.2f}")

        try:
            # Place order
            order_id = self.kite.place_order(
                tradingsymbol=symbol,
                transaction_type=txn_type,
                quantity=quantity,
                price=price,
                order_type='LIMIT'
            )

            logger.info(f"  Order placed: {order_id}")

            # Wait for fill
            fill_result = self._wait_for_fill(order_id, LEG_TIMEOUT_SECONDS)

            if fill_result['filled']:
                avg_price = fill_result['avg_price']
                actual_qty = fill_result.get('filled_quantity', quantity)
                is_partial = fill_result.get('partial', False)
                slippage = abs(avg_price - base_price)

                if is_partial:
                    logger.warning(
                        f"  ⚠ PARTIAL FILL @ {avg_price:.2f} "
                        f"({actual_qty}/{quantity} qty, slippage: {slippage:.2f})"
                    )
                    return ExecutedLeg(
                        leg_type=leg_type,
                        symbol=symbol,
                        txn_type=txn_type,
                        status=LegStatus.PARTIALLY_FILLED,
                        order_id=order_id,
                        fill_price=avg_price,
                        quantity=actual_qty
                    )

                logger.info(f"  ✓ FILLED @ {avg_price:.2f} (slippage: {slippage:.2f})")

                return ExecutedLeg(
                    leg_type=leg_type,
                    symbol=symbol,
                    txn_type=txn_type,
                    status=LegStatus.FILLED,
                    order_id=order_id,
                    fill_price=avg_price,
                    quantity=actual_qty
                )
            else:
                # Order not filled - attempt cancel and verify final status
                # AE-004 Fix: Re-check order status after cancel to detect race condition
                self._cancel_order_safe(order_id)

                # Wait briefly and re-check - order may have filled during cancel
                time.sleep(0.5)
                final_status = self._get_final_order_status(order_id)

                if final_status.get('filled'):
                    avg_price = final_status.get('avg_price', 0)
                    actual_qty = final_status.get('filled_quantity', quantity)
                    is_partial = final_status.get('partial', False)

                    if is_partial:
                        logger.warning(
                            f"  ⚠ Partial fill during cancel: {actual_qty}/{quantity} @ {avg_price:.2f}"
                        )
                        return ExecutedLeg(
                            leg_type=leg_type,
                            symbol=symbol,
                            txn_type=txn_type,
                            status=LegStatus.PARTIALLY_FILLED,
                            order_id=order_id,
                            fill_price=avg_price,
                            quantity=actual_qty
                        )

                    logger.info(f"  ✓ Order filled during cancel (race condition): {avg_price:.2f}")
                    return ExecutedLeg(
                        leg_type=leg_type,
                        symbol=symbol,
                        txn_type=txn_type,
                        status=LegStatus.FILLED,
                        order_id=order_id,
                        fill_price=avg_price,
                        quantity=actual_qty
                    )

                error = fill_result.get('error', 'Order not filled within timeout')
                logger.warning(f"  ✗ FAILED: {error}")

                return ExecutedLeg(
                    leg_type=leg_type,
                    symbol=symbol,
                    txn_type=txn_type,
                    status=LegStatus.FAILED,
                    order_id=order_id,
                    error=error
                )

        except Exception as e:
            logger.error(f"  ✗ EXCEPTION placing order: {e}")
            # CRITICAL: If place_order threw, the order MAY have been placed on exchange
            # Check today's orders for this symbol to detect ghost orders
            logger.info(f"  Checking for ghost order after exception...")
            ghost = self._check_ghost_orders(symbol, txn_type, quantity)
            if ghost and (ghost['status'] == 'COMPLETE' or (ghost.get('filled_quantity', 0) or 0) > 0):
                filled_qty = ghost.get('filled_quantity', 0) or 0
                avg_price = ghost.get('average_price', 0)
                logger.warning(
                    f"  ⚠ GHOST ORDER found after exception! order_id={ghost['order_id']}, "
                    f"filled_qty={filled_qty}, avg_price={avg_price}"
                )
                status = LegStatus.FILLED if filled_qty >= quantity else LegStatus.PARTIALLY_FILLED
                return ExecutedLeg(
                    leg_type=leg_type,
                    symbol=symbol,
                    txn_type=txn_type,
                    status=status,
                    order_id=ghost['order_id'],
                    fill_price=avg_price,
                    quantity=filled_qty
                )

            return ExecutedLeg(
                leg_type=leg_type,
                symbol=symbol,
                txn_type=txn_type,
                status=LegStatus.FAILED,
                error=str(e)
            )

    def _wait_for_fill(
        self,
        order_id: str,
        timeout_seconds: int
    ) -> Dict[str, Any]:
        """
        Wait for order to fill with timeout.

        Args:
            order_id: Kite order ID
            timeout_seconds: Maximum wait time

        Returns:
            Dict with 'filled', 'avg_price', 'status', 'error', 'filled_quantity'
        """
        start_time = time.time()

        while time.time() - start_time < timeout_seconds:
            try:
                status = self.kite.get_order_status(order_id)

                order_status = status.get('status', '')
                filled_qty = status.get('filled_quantity', 0) or 0

                if order_status == 'COMPLETE':
                    return {
                        'filled': True,
                        'avg_price': status.get('average_price', 0),
                        'status': order_status,
                        'filled_quantity': filled_qty
                    }
                elif order_status in ('REJECTED', 'CANCELLED'):
                    # CRITICAL: Check for partial fills on cancelled orders
                    # An order can be cancelled AFTER some quantity was filled
                    if filled_qty > 0:
                        logger.warning(
                            f"Order {order_id} {order_status} but has {filled_qty} filled qty! "
                            f"Treating as partial fill."
                        )
                        return {
                            'filled': True,
                            'partial': True,
                            'avg_price': status.get('average_price', 0),
                            'status': order_status,
                            'filled_quantity': filled_qty,
                            'error': f'Partial fill: {filled_qty} filled before {order_status}'
                        }
                    return {
                        'filled': False,
                        'status': order_status,
                        'error': status.get('status_message', order_status),
                        'filled_quantity': 0
                    }

                time.sleep(ORDER_POLL_INTERVAL)

            except Exception as e:
                logger.warning(f"Error checking order status: {e}")
                time.sleep(ORDER_POLL_INTERVAL)

        return {
            'filled': False,
            'status': 'TIMEOUT',
            'error': f'Order not filled within {timeout_seconds}s',
            'filled_quantity': 0
        }

    def _cancel_order_safe(self, order_id: str) -> bool:
        """Safely cancel an order, ignoring errors."""
        try:
            self.kite.cancel_order(order_id)
            logger.info(f"  Order {order_id} cancelled")
            return True
        except Exception as e:
            logger.warning(f"  Could not cancel order {order_id}: {e}")
            return False

    def _check_ghost_orders(
        self,
        symbol: str,
        txn_type: str,
        quantity: int
    ) -> Optional[Dict[str, Any]]:
        """
        Check today's orders for ghost orders on the same symbol.

        A ghost order is one that was placed on the exchange but the client
        didn't receive the order_id (network timeout during place_order).
        Without this check, a retry would create a DUPLICATE order.

        Args:
            symbol: Trading symbol to check
            txn_type: BUY or SELL
            quantity: Expected quantity

        Returns:
            Order dict if ghost found, None otherwise
        """
        try:
            all_orders = self.kite.orders()

            # Look for recent orders on same symbol with same direction and quantity
            for order in all_orders:
                if (order.get('tradingsymbol') == symbol
                        and order.get('transaction_type') == txn_type
                        and order.get('quantity') == quantity
                        and order.get('status') in ('OPEN', 'COMPLETE', 'PENDING',
                                                     'TRIGGER PENDING')):
                    # Check if this order was placed recently (within last 5 minutes)
                    order_time = order.get('order_timestamp')
                    if order_time:
                        try:
                            if isinstance(order_time, str):
                                from datetime import datetime as dt
                                order_dt = dt.fromisoformat(order_time)
                            else:
                                order_dt = order_time
                            age_seconds = (datetime.now() - order_dt).total_seconds()
                            if age_seconds <= 300:  # 5 minutes
                                # Check this order_id is NOT one we already know about
                                known_ids = {leg.order_id for leg in self.executed_legs if leg.order_id}
                                if order.get('order_id') not in known_ids:
                                    logger.warning(
                                        f"Ghost order found: {order.get('order_id')} "
                                        f"({symbol} {txn_type} {quantity}) "
                                        f"placed {age_seconds:.0f}s ago"
                                    )
                                    return order
                        except (ValueError, TypeError):
                            pass  # Can't parse timestamp, skip

            return None

        except Exception as e:
            logger.warning(f"Ghost order check failed (non-blocking): {e}")
            return None

    def _get_final_order_status(self, order_id: str) -> Dict[str, Any]:
        """
        Get final order status after cancel attempt.

        AE-004 Fix: Checks if order filled during the cancel race condition.
        Also detects partial fills on cancelled orders.

        Args:
            order_id: Kite order ID

        Returns:
            Dict with 'filled', 'avg_price', 'status', 'filled_quantity', 'partial'
        """
        try:
            status = self.kite.get_order_status(order_id)
            order_status = status.get('status', '')
            filled_qty = status.get('filled_quantity', 0) or 0

            if order_status == 'COMPLETE':
                return {
                    'filled': True,
                    'avg_price': status.get('average_price', 0),
                    'status': order_status,
                    'filled_quantity': filled_qty
                }

            # CRITICAL: Check for partial fills on cancelled orders
            if filled_qty > 0:
                logger.warning(
                    f"Order {order_id} status={order_status} but filled_quantity={filled_qty}! "
                    f"Partial fill detected after cancel."
                )
                return {
                    'filled': True,
                    'partial': True,
                    'avg_price': status.get('average_price', 0),
                    'status': order_status,
                    'filled_quantity': filled_qty
                }

            return {
                'filled': False,
                'status': order_status,
                'filled_quantity': 0
            }
        except Exception as e:
            logger.warning(f"Error getting final order status: {e}")
            return {'filled': False, 'status': 'UNKNOWN', 'error': str(e), 'filled_quantity': 0}

    def _handle_failure(
        self,
        failed_leg: str,
        result: ExecutedLeg
    ) -> AtomicExecutionResult:
        """
        Handle leg failure - determine position state and take action.

        With our execution order, failure at any point leaves us with
        DEFINED risk (never unlimited).
        """
        position_state = self._determine_position_state()

        logger.warning("=" * 60)
        logger.warning("ATOMIC EXECUTION INCOMPLETE")
        logger.warning(f"Failed at: {failed_leg}")
        logger.warning(f"Position state: {position_state.name}")
        logger.warning(f"Error: {result.error}")
        logger.warning("=" * 60)

        # Check if we need emergency rollback
        needs_rollback = position_state in DANGEROUS_STATES

        rollback_result = None
        if needs_rollback:
            logger.critical("🚨 DANGEROUS STATE DETECTED - EXECUTING ROLLBACK")
            rollback_result = self._execute_rollback()

            if rollback_result.success:
                position_state = PositionState.NO_POSITION
                logger.info("Rollback successful - position cleared")
            else:
                logger.critical("🚨 ROLLBACK FAILED - MANUAL INTERVENTION REQUIRED")
                self._send_critical_alert(
                    "ROLLBACK FAILED",
                    f"Failed leg: {failed_leg}\nPosition state: {position_state.name}"
                )
        else:
            # Position has defined risk - log and notify
            self._log_partial_position(position_state)

        return AtomicExecutionResult(
            success=False,
            legs=self.executed_legs,
            position_state=position_state,
            failed_leg=failed_leg,
            error=result.error,
            rollback_performed=needs_rollback,
            rollback_result=rollback_result
        )

    def _determine_position_state(self) -> PositionState:
        """
        Analyze current position state based on executed legs.

        Returns:
            PositionState enum value
        """
        # Include both FILLED and PARTIALLY_FILLED legs - any fill means
        # we have a position on the exchange that must be accounted for
        filled_legs = {
            leg.leg_type for leg in self.executed_legs
            if leg.status in (LegStatus.FILLED, LegStatus.PARTIALLY_FILLED)
        }

        if not filled_legs:
            return PositionState.NO_POSITION

        # Check for dangerous states (should NEVER happen with our order)
        has_straddle_ce = 'straddle_ce' in filled_legs
        has_straddle_pe = 'straddle_pe' in filled_legs
        has_wing_ce = 'wing_ce' in filled_legs
        has_wing_pe = 'wing_pe' in filled_legs

        # Naked short detection
        if has_straddle_ce and not has_wing_ce:
            return PositionState.NAKED_SHORT_CALL
        if has_straddle_pe and not has_wing_pe:
            return PositionState.NAKED_SHORT_PUT
        if has_straddle_ce and has_straddle_pe and not (has_wing_ce and has_wing_pe):
            return PositionState.NAKED_STRADDLE

        # Safe partial states (expected with our execution order)
        if filled_legs == {'wing_ce'}:
            return PositionState.LONG_CALL_ONLY

        if filled_legs == {'wing_ce', 'straddle_ce'}:
            return PositionState.BEAR_CALL_SPREAD

        if filled_legs == {'wing_ce', 'straddle_ce', 'wing_pe'}:
            return PositionState.BEAR_CALL_SPREAD_PLUS_LONG_PUT

        if filled_legs == REQUIRED_LEGS:
            return PositionState.COMPLETE_IRON_FLY

        return PositionState.UNKNOWN

    def _execute_rollback(self) -> RollbackResult:
        """
        Emergency rollback of dangerous positions.

        Uses MARKET orders for guaranteed fill.
        Only rolls back SELL legs that don't have protective wings.
        """
        logger.critical("=" * 60)
        logger.critical("🚨 EMERGENCY ROLLBACK IN PROGRESS")
        logger.critical("=" * 60)

        rollback_results: List[RollbackLegResult] = []
        total_slippage = 0.0

        for leg in self.executed_legs:
            if leg.status != LegStatus.FILLED:
                continue

            # Only rollback SELL legs (the dangerous ones)
            if leg.txn_type != 'SELL':
                continue

            # Check if this SELL has corresponding protective wing
            wing_type = 'wing_ce' if leg.leg_type == 'straddle_ce' else 'wing_pe'
            wing_filled = any(
                exec_leg.leg_type == wing_type and exec_leg.status == LegStatus.FILLED
                for exec_leg in self.executed_legs
            )

            if wing_filled:
                logger.info(f"  {leg.leg_type} is protected by {wing_type} - no rollback needed")
                continue

            # UNPROTECTED SHORT - ROLLBACK IMMEDIATELY
            logger.critical(f"  Rolling back unprotected {leg.leg_type}: {leg.symbol}")

            # Validate quantity before rollback
            if leg.quantity is None or leg.quantity <= 0:
                logger.error(f"  Cannot rollback {leg.leg_type}: invalid quantity {leg.quantity}")
                continue

            try:
                order_id = self.kite.place_order(
                    tradingsymbol=leg.symbol,
                    transaction_type='BUY',  # Opposite of SELL
                    quantity=leg.quantity,
                    order_type='MARKET'  # Guaranteed fill
                )

                # Wait for fill with longer timeout for MARKET
                fill = self._wait_for_fill(order_id, ROLLBACK_TIMEOUT_SECONDS)

                if fill['filled']:
                    slippage_pts = abs(fill['avg_price'] - leg.fill_price) if leg.fill_price else 0
                    slippage_cost = slippage_pts * (leg.quantity or 0)  # Convert to ₹
                    total_slippage += slippage_cost

                    logger.info(f"  ✓ Rollback filled @ {fill['avg_price']:.2f} (slippage: {slippage_pts:.2f} pts = ₹{slippage_cost:.2f})")

                    rollback_results.append(RollbackLegResult(
                        leg_type=leg.leg_type,
                        success=True,
                        order_id=order_id,
                        fill_price=fill['avg_price']
                    ))

                    # Update original leg status
                    leg.status = LegStatus.ROLLED_BACK
                else:
                    logger.critical(f"  ✗ Rollback FAILED: {fill.get('error')}")
                    rollback_results.append(RollbackLegResult(
                        leg_type=leg.leg_type,
                        success=False,
                        error=fill.get('error')
                    ))

            except Exception as e:
                logger.critical(f"  ✗ Rollback EXCEPTION: {e}")
                rollback_results.append(RollbackLegResult(
                    leg_type=leg.leg_type,
                    success=False,
                    error=str(e)
                ))

        all_success = all(r.success for r in rollback_results)

        logger.critical("=" * 60)
        logger.critical(f"ROLLBACK {'SUCCESSFUL' if all_success else 'FAILED'}")
        # AE-011 Fix: total_slippage is now already in ₹ (calculated per leg × quantity)
        logger.critical(f"Total slippage cost: ₹{total_slippage:.2f}")
        logger.critical("=" * 60)

        return RollbackResult(
            success=all_success,
            legs=rollback_results,
            total_slippage=total_slippage
        )

    def _validate_preconditions(
        self,
        symbols: Dict[str, str],
        quantity: int,
        quotes: Dict[str, Quote]
    ) -> PreValidationResult:
        """
        Pre-flight validation before attempting entry.

        Checks:
        1. All 4 required symbols present
        2. Sufficient margin (with buffer)
        3. Reasonable bid-ask spreads
        4. Adequate liquidity
        """
        errors: List[str] = []
        warnings: List[str] = []

        # 1. Verify all 4 symbols provided
        missing = REQUIRED_LEGS - set(symbols.keys())
        if missing:
            errors.append(f"Missing legs: {missing}")

        missing_quotes = REQUIRED_LEGS - set(quotes.keys())
        if missing_quotes:
            errors.append(f"Missing quotes: {missing_quotes}")

        if errors:
            return PreValidationResult(valid=False, errors=errors)

        # 2. Check margin for Iron Fly (defined risk position)
        # Iron Fly margin is much lower than naked straddle since risk is capped
        # Typical broker margin for 1 lot NIFTY Iron Fly: ~₹80,000-₹1,00,000
        try:
            margins = self.kite.get_available_margin()
            available = margins if isinstance(margins, (int, float)) else 0

            # Use config's min_available as the expected margin per lot
            # quantity is total contracts, so num_lots = quantity / lot_size
            num_lots = quantity // self.lot_size if self.lot_size > 0 else 1
            min_available = self.config.get('trading', {}).get('capital', {}).get('min_available', 100000)

            # Required margin = min_available per lot × number of lots × 1.2 buffer
            required_margin = min_available * num_lots * 1.2

            if available < required_margin:
                errors.append(
                    f"Insufficient margin: ₹{available:,.0f} < ₹{required_margin:,.0f} "
                    f"(estimated Iron Fly margin for {num_lots} lot(s) with 20% buffer)"
                )
            elif available < min_available * num_lots * 1.5:
                warnings.append(
                    f"Margin buffer low: ₹{available:,.0f} "
                    f"(recommend ₹{min_available * num_lots * 1.5:,.0f} for comfortable buffer)"
                )
        except Exception as e:
            warnings.append(f"Could not verify margin: {e}")

        # 3. Check bid-ask spreads
        for leg_type, quote in quotes.items():
            if quote.ask <= 0 or quote.bid <= 0:
                errors.append(f"{leg_type}: Invalid quote (bid={quote.bid}, ask={quote.ask})")
                continue

            spread = quote.ask - quote.bid
            spread_pct = (spread / quote.ltp) * 100 if quote.ltp > 0 else 100

            # Tighter threshold for straddle legs (more premium at stake)
            if 'straddle' in leg_type:
                if spread_pct > 1.5:
                    errors.append(f"{leg_type}: Spread too wide ({spread_pct:.2f}% > 1.5%)")
                elif spread_pct > 0.75:
                    warnings.append(f"{leg_type}: Elevated spread ({spread_pct:.2f}%)")
            else:  # wings
                if spread_pct > 3.0:
                    errors.append(f"{leg_type}: Spread too wide ({spread_pct:.2f}% > 3.0%)")
                elif spread_pct > 1.5:
                    warnings.append(f"{leg_type}: Elevated spread ({spread_pct:.2f}%)")

        # 4. Check liquidity
        # quantity is already total contracts (e.g., 65 for 1 lot NIFTY)
        for leg_type, quote in quotes.items():
            if hasattr(quote, 'bid_qty') and hasattr(quote, 'ask_qty'):
                if quote.bid_qty < quantity or quote.ask_qty < quantity:
                    warnings.append(
                        f"{leg_type}: Low liquidity "
                        f"(need {quantity}, have bid={quote.bid_qty}, ask={quote.ask_qty})"
                    )

        return PreValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )

    def _log_partial_position(self, state: PositionState) -> None:
        """Log details of partial position for tracking."""
        logger.warning("-" * 40)
        logger.warning("PARTIAL POSITION DETAILS:")
        logger.warning(f"State: {state.name}")

        for leg in self.executed_legs:
            status_icon = "✓" if leg.status == LegStatus.FILLED else "✗"
            logger.warning(
                f"  {status_icon} {leg.leg_type}: {leg.symbol} "
                f"{leg.txn_type} @ {leg.fill_price or 'N/A'} "
                f"({leg.status.value})"
            )

        # Risk assessment
        if state == PositionState.LONG_CALL_ONLY:
            logger.warning("Risk: Max loss = premium paid for wing CE")
        elif state == PositionState.BEAR_CALL_SPREAD:
            logger.warning("Risk: Max loss = strike difference - credit received (CE side)")
        elif state == PositionState.BEAR_CALL_SPREAD_PLUS_LONG_PUT:
            logger.warning("Risk: Defined on CE side, long put on PE side")

        logger.warning("-" * 40)

    def _send_critical_alert(self, title: str, details: str) -> None:
        """Send critical alert via Telegram."""
        if self.telegram:
            try:
                message = f"🚨 *CRITICAL: {title}*\n\n{details}\n\n⚠️ Manual intervention may be required!"
                self.telegram.send_message(message)
            except Exception as e:
                logger.error(f"Failed to send critical alert: {e}")

        # Always log critically
        logger.critical(f"CRITICAL ALERT: {title} - {details}")


# =============================================================================
# CONVENIENCE FUNCTIONS
# =============================================================================

def execute_atomic_iron_fly_entry(
    kite: SNAILKiteClient,
    symbols: Dict[str, str],
    quotes: Dict[str, Quote],
    quantity: int,
    config: Optional[Dict[str, Any]] = None,
    telegram_alerts: Optional[Any] = None,
    lot_size: Optional[int] = None,
    instrument: str = 'NIFTY'
) -> AtomicExecutionResult:
    """
    Convenience function for atomic Iron Fly entry.

    Args:
        kite: Kite client
        symbols: Dict of leg symbols
        quotes: Dict of leg quotes
        quantity: Order quantity
        config: Optional config
        telegram_alerts: Optional Telegram alerts
        lot_size: Lot size for the instrument (optional, defaults based on instrument)
        instrument: Instrument name (NIFTY, BANKNIFTY, FINNIFTY)

    Returns:
        AtomicExecutionResult
    """
    executor = AtomicIronFlyExecutor(
        kite=kite,
        config=config,
        telegram_alerts=telegram_alerts,
        lot_size=lot_size,
        instrument=instrument
    )
    return executor.execute(symbols, quantity, quotes)


def get_position_risk_description(state: PositionState) -> str:
    """
    Get human-readable risk description for position state.

    Args:
        state: Position state

    Returns:
        Risk description string
    """
    descriptions = {
        PositionState.NO_POSITION: "No position - no risk",
        PositionState.LONG_CALL_ONLY: "Long OTM call only - max loss is premium paid",
        PositionState.LONG_PUT_ONLY: "Long OTM put only - max loss is premium paid",
        PositionState.BEAR_CALL_SPREAD: "Bear call spread - risk capped at strike difference minus credit",
        PositionState.BULL_PUT_SPREAD: "Bull put spread - risk capped at strike difference minus credit",
        PositionState.BEAR_CALL_SPREAD_PLUS_LONG_PUT: "Bear call spread + long put - defined risk, incomplete iron fly",
        PositionState.COMPLETE_IRON_FLY: "Complete iron fly - defined risk, full position",
        PositionState.NAKED_SHORT_CALL: "⚠️ NAKED SHORT CALL - UNLIMITED UPSIDE RISK!",
        PositionState.NAKED_SHORT_PUT: "⚠️ NAKED SHORT PUT - UNLIMITED DOWNSIDE RISK!",
        PositionState.NAKED_STRADDLE: "🚨 NAKED STRADDLE - UNLIMITED RISK BOTH DIRECTIONS!",
        PositionState.UNKNOWN: "Unknown position state - manual verification required",
    }
    return descriptions.get(state, "Unknown state")

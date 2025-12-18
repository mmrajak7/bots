"""
SNAIL Scaled Order Execution

Handles large order execution by splitting into batches that respect NSE freeze limits.
Maintains position integrity through rebalancing when fills are unequal.

@file        scaled_execution.py
@description Scaled order execution for Iron Fly positions
@author      SNAIL Development Team
@created     2025-12-11
@version     1.0.0
@references  docs/SCALED_ORDER_EXECUTION_DESIGN.md
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger

from src.api.kite_client import SNAILKiteClient, Quote
from src.utils.order_helpers import round_to_tick, TICK_SIZE


# =============================================================================
# CONSTANTS
# =============================================================================

# Default freeze limit if not in config (NIFTY)
DEFAULT_FREEZE_LIMIT: int = 1800

# Default execution parameters
DEFAULT_INTER_BATCH_DELAY_MS: int = 3000
DEFAULT_ORDER_TIMEOUT_SECONDS: int = 30
DEFAULT_CANCEL_WAIT_SECONDS: int = 1
DEFAULT_MAX_RETRIES: int = 2

# Default slippage parameters
DEFAULT_BASE_TICKS: int = 3
DEFAULT_DEPTH_BUFFER_TICKS: int = 2
DEFAULT_MAX_TICKS: int = 15
DEFAULT_WING_RETRY_INCREMENT: int = 2

# Default rebalance parameters
DEFAULT_MAX_REBALANCE_ATTEMPTS: int = 2


# =============================================================================
# ENUMS
# =============================================================================

class BatchStatus(Enum):
    """Status of a batch execution."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    REBALANCED = "REBALANCED"
    FAILED = "FAILED"


class LegStatus(Enum):
    """Status of a single leg order."""
    PENDING = "PENDING"
    PLACED = "PLACED"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class DepthLevel:
    """Single level of market depth."""
    price: float
    quantity: int
    orders: int = 0


@dataclass
class MarketDepth:
    """
    Full market depth for an instrument.

    Attributes:
        tradingsymbol: Trading symbol without exchange prefix
        bid_levels: Buy side depth (for sell orders)
        ask_levels: Sell side depth (for buy orders)
        ltp: Last traded price
        timestamp: When depth was fetched
    """
    tradingsymbol: str
    bid_levels: List[DepthLevel]
    ask_levels: List[DepthLevel]
    ltp: float
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def total_bid_qty(self) -> int:
        """Total quantity on bid side."""
        return sum(level.quantity for level in self.bid_levels)

    @property
    def total_ask_qty(self) -> int:
        """Total quantity on ask side."""
        return sum(level.quantity for level in self.ask_levels)

    @property
    def best_bid(self) -> float:
        """Best bid price."""
        return self.bid_levels[0].price if self.bid_levels else 0.0

    @property
    def best_ask(self) -> float:
        """Best ask price."""
        return self.ask_levels[0].price if self.ask_levels else 0.0


@dataclass
class LegOrder:
    """
    Represents a single leg order within a batch.

    Attributes:
        leg_type: One of straddle_ce, straddle_pe, wing_ce, wing_pe
        tradingsymbol: Trading symbol without exchange prefix
        transaction_type: BUY or SELL
        quantity: Order quantity in contracts
        order_type: MARKET or LIMIT
        price: Limit price (None for MARKET orders)
        status: Current order status
        order_id: Kite order ID once placed
        fill_price: Actual fill price
        filled_qty: Quantity filled
        error_message: Error if order failed
        placed_at: Timestamp when order was placed
        filled_at: Timestamp when order was filled
    """
    leg_type: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_type: str
    price: Optional[float] = None
    status: LegStatus = LegStatus.PENDING
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_qty: int = 0
    error_message: str = ""
    placed_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None


@dataclass
class Batch:
    """
    Represents a batch of 4 leg orders.

    A batch contains one order for each Iron Fly leg, all with the same quantity.
    Position integrity requires all 4 legs to have equal filled quantities.

    Attributes:
        batch_number: Sequential batch number (1-indexed)
        quantity_per_leg: Target quantity for each leg
        legs: Dict mapping leg_type to LegOrder
        status: Current batch status
        started_at: When batch execution started
        completed_at: When batch execution completed
        rebalance_orders: Orders placed to restore balance
    """
    batch_number: int
    quantity_per_leg: int
    legs: Dict[str, LegOrder]
    status: BatchStatus = BatchStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    rebalance_orders: List[LegOrder] = field(default_factory=list)

    @property
    def is_balanced(self) -> bool:
        """Check if all legs have equal filled quantity."""
        filled_qtys = [leg.filled_qty for leg in self.legs.values()]
        if not filled_qtys:
            return False
        return len(set(filled_qtys)) == 1 and filled_qtys[0] > 0

    @property
    def filled_quantity(self) -> int:
        """Minimum filled quantity across all legs (balanced amount)."""
        filled_qtys = [leg.filled_qty for leg in self.legs.values()]
        return min(filled_qtys) if filled_qtys else 0

    def get_imbalance(self) -> Dict[str, int]:
        """
        Get imbalance per leg.

        Returns:
            Dict mapping leg_type to excess quantity (positive = excess to trim)
        """
        min_filled = self.filled_quantity
        return {
            leg_type: leg.filled_qty - min_filled
            for leg_type, leg in self.legs.items()
        }


@dataclass
class ExecutionPlan:
    """
    Complete execution plan for scaled order.

    Attributes:
        total_quantity: Total contracts per leg
        freeze_limit: NSE freeze limit used
        num_batches: Number of batches planned
        batches: List of Batch objects
        symbols: Mapping of leg_type to tradingsymbol
        created_at: When plan was created
    """
    total_quantity: int
    freeze_limit: int
    num_batches: int
    batches: List[Batch]
    symbols: Dict[str, str]
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ExecutionResult:
    """
    Result of scaled order execution.

    Attributes:
        success: True if any quantity was successfully filled
        total_filled_qty: Balanced quantity achieved per leg
        target_qty: Original target quantity per leg
        batches_completed: Number of batches fully completed
        batches_total: Total batches in plan
        total_slippage: Total slippage in points
        execution_time_seconds: Total execution time
        leg_fills: Per-leg fill prices (weighted average)
        errors: List of error messages
        warnings: List of warning messages
    """
    success: bool
    total_filled_qty: int
    target_qty: int
    batches_completed: int
    batches_total: int
    total_slippage: float
    execution_time_seconds: float
    leg_fills: Dict[str, float]
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


# =============================================================================
# DEPTH ANALYZER
# =============================================================================

class DepthAnalyzer:
    """
    Analyzes market depth for pricing decisions.

    Fetches 5-level market depth and calculates appropriate slippage
    based on available liquidity. Falls back to simple quotes if depth
    is unavailable.
    """

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]) -> None:
        """
        Initialize depth analyzer.

        Args:
            kite: Kite client instance
            config: Configuration dictionary
        """
        self.kite = kite
        slippage_config = config.get('scaling', {}).get('slippage', {})
        self.base_ticks: int = slippage_config.get('base_ticks', DEFAULT_BASE_TICKS)
        self.depth_buffer_ticks: int = slippage_config.get(
            'depth_buffer_ticks', DEFAULT_DEPTH_BUFFER_TICKS
        )
        self.max_ticks: int = slippage_config.get('max_ticks', DEFAULT_MAX_TICKS)
        self.tick_size: float = TICK_SIZE

    def fetch_depth(self, instruments: List[str]) -> Dict[str, MarketDepth]:
        """
        Fetch full market depth for instruments.

        Args:
            instruments: List of "NFO:SYMBOL" strings

        Returns:
            Dict mapping instrument to MarketDepth
        """
        try:
            # Use kite property which ensures authentication
            raw_quotes = self.kite.kite.quote(instruments)
        except Exception as e:
            logger.error(f"Failed to fetch depth: {e}")
            return {}

        result: Dict[str, MarketDepth] = {}

        for inst, data in raw_quotes.items():
            depth = data.get('depth', {})

            bid_levels = [
                DepthLevel(
                    price=level.get('price', 0),
                    quantity=level.get('quantity', 0),
                    orders=level.get('orders', 0)
                )
                for level in depth.get('buy', [])
                if level.get('price', 0) > 0
            ]

            ask_levels = [
                DepthLevel(
                    price=level.get('price', 0),
                    quantity=level.get('quantity', 0),
                    orders=level.get('orders', 0)
                )
                for level in depth.get('sell', [])
                if level.get('price', 0) > 0
            ]

            # Extract symbol from "NFO:SYMBOL" format
            symbol = inst.split(':')[1] if ':' in inst else inst

            result[inst] = MarketDepth(
                tradingsymbol=symbol,
                bid_levels=bid_levels,
                ask_levels=ask_levels,
                ltp=data.get('last_price', 0)
            )

        return result

    def fetch_quotes(self, instruments: List[str]) -> Dict[str, Quote]:
        """
        Fetch simple quotes (fallback when depth not available).

        Args:
            instruments: List of "NFO:SYMBOL" strings

        Returns:
            Dict mapping instrument to Quote
        """
        return self.kite.quote(instruments)

    def calculate_slippage_ticks(
        self,
        depth: Optional[MarketDepth],
        quantity: int,
        side: str,
        batch_number: int = 1
    ) -> int:
        """
        Calculate appropriate slippage ticks based on depth.

        Args:
            depth: Market depth data (can be None)
            quantity: Order quantity
            side: BUY or SELL
            batch_number: Current batch number (for progressive slippage)

        Returns:
            Number of slippage ticks to apply
        """
        ticks = self.base_ticks

        # Add depth buffer if depth is thin or unavailable
        if depth is None:
            ticks += self.depth_buffer_ticks
        else:
            levels = depth.ask_levels if side == "BUY" else depth.bid_levels
            total_depth = sum(level.quantity for level in levels)

            if total_depth < quantity * 0.5:
                ticks += self.depth_buffer_ticks
                logger.debug(
                    f"Thin depth ({total_depth} < {quantity * 0.5}), "
                    f"adding {self.depth_buffer_ticks} buffer ticks"
                )

        # Progressive slippage for later batches
        ticks += (batch_number - 1)

        # Cap at maximum
        return min(ticks, self.max_ticks)

    def calculate_price_with_slippage(
        self,
        base_price: float,
        transaction_type: str,
        slippage_ticks: int
    ) -> float:
        """
        Calculate order price with slippage applied.

        Args:
            base_price: Base price (bid for SELL, ask for BUY)
            transaction_type: BUY or SELL
            slippage_ticks: Number of ticks to add as slippage

        Returns:
            Price rounded to tick
        """
        adjustment = slippage_ticks * self.tick_size

        if transaction_type == "BUY":
            # For buys, we go higher than ask
            price = base_price + adjustment
        else:
            # For sells, we go lower than bid
            price = base_price - adjustment

        return round_to_tick(max(price, self.tick_size))


# =============================================================================
# BATCH PLANNER
# =============================================================================

class BatchPlanner:
    """
    Plans batch execution for scaled orders.

    Splits total quantity into batches respecting NSE freeze limits.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        """
        Initialize batch planner.

        Args:
            config: Configuration dictionary
        """
        self.freeze_limits: Dict[str, int] = config.get('scaling', {}).get(
            'freeze_limits', {}
        )
        self.default_freeze_limit: int = DEFAULT_FREEZE_LIMIT

    def get_freeze_limit(self, instrument: str = "NIFTY") -> int:
        """
        Get freeze limit for instrument.

        Args:
            instrument: Index name (NIFTY, BANKNIFTY, etc.)

        Returns:
            Freeze limit in contracts
        """
        return self.freeze_limits.get(instrument, self.default_freeze_limit)

    def create_plan(
        self,
        total_quantity: int,
        symbols: Dict[str, str],
        instrument: str = "NIFTY",
        order_types: Optional[Dict[str, str]] = None
    ) -> ExecutionPlan:
        """
        Create execution plan with batches.

        Args:
            total_quantity: Total contracts per leg
            symbols: Mapping of leg_type to tradingsymbol
            instrument: Index name for freeze limit lookup
            order_types: Optional override for order types per leg category

        Returns:
            ExecutionPlan with batches
        """
        freeze_limit = self.get_freeze_limit(instrument)
        num_batches = math.ceil(total_quantity / freeze_limit)

        # Default order types
        if order_types is None:
            order_types = {
                'atm_straddle': 'MARKET',
                'otm_wings': 'LIMIT'
            }

        batches: List[Batch] = []
        remaining = total_quantity

        for i in range(num_batches):
            batch_qty = min(freeze_limit, remaining)

            legs: Dict[str, LegOrder] = {}
            for leg_type, symbol in symbols.items():
                # Determine order type and transaction type based on leg
                if leg_type.startswith('straddle'):
                    order_type = order_types.get('atm_straddle', 'MARKET')
                    transaction_type = "SELL"
                else:  # wing
                    order_type = order_types.get('otm_wings', 'LIMIT')
                    transaction_type = "BUY"

                legs[leg_type] = LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=batch_qty,
                    order_type=order_type,
                    price=None  # Set by DepthAnalyzer before execution
                )

            batches.append(Batch(
                batch_number=i + 1,
                quantity_per_leg=batch_qty,
                legs=legs
            ))

            remaining -= batch_qty

        logger.info(
            f"Created execution plan: {num_batches} batches for {total_quantity} contracts "
            f"(freeze_limit={freeze_limit})"
        )

        return ExecutionPlan(
            total_quantity=total_quantity,
            freeze_limit=freeze_limit,
            num_batches=num_batches,
            batches=batches,
            symbols=symbols
        )


# =============================================================================
# BATCH EXECUTOR
# =============================================================================

class BatchExecutor:
    """
    Executes a batch of 4 leg orders.

    Places all orders, waits for fills, and handles timeouts by
    converting LIMIT orders to MARKET.
    """

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]) -> None:
        """
        Initialize batch executor.

        Args:
            kite: Kite client instance
            config: Configuration dictionary
        """
        self.kite = kite
        exec_config = config.get('scaling', {}).get('execution', {})
        self.order_timeout: int = exec_config.get(
            'order_timeout_seconds', DEFAULT_ORDER_TIMEOUT_SECONDS
        )
        self.cancel_wait: int = exec_config.get(
            'cancel_wait_seconds', DEFAULT_CANCEL_WAIT_SECONDS
        )
        self.max_retries: int = exec_config.get(
            'max_retries_per_order', DEFAULT_MAX_RETRIES
        )

        slippage_config = config.get('scaling', {}).get('slippage', {})
        self.wing_retry_increment: int = slippage_config.get(
            'wing_retry_increment', DEFAULT_WING_RETRY_INCREMENT
        )
        self.tick_size: float = TICK_SIZE

    def execute_batch(self, batch: Batch) -> Batch:
        """
        Execute all legs in a batch.

        Strategy:
        1. Place all 4 orders
        2. Wait for fills with timeout
        3. Convert timed-out LIMIT orders to MARKET

        Args:
            batch: Batch to execute

        Returns:
            Updated batch with execution results
        """
        batch.status = BatchStatus.IN_PROGRESS
        batch.started_at = datetime.now()

        logger.info(
            f"Executing batch {batch.batch_number}: "
            f"{batch.quantity_per_leg} contracts per leg"
        )

        # Step 1: Place all orders
        self._place_all_orders(batch)

        # Step 2: Wait for fills
        self._wait_for_fills(batch)

        # Step 3: Determine final status
        batch.completed_at = datetime.now()

        all_filled = all(
            leg.status == LegStatus.FILLED
            for leg in batch.legs.values()
        )

        if all_filled and batch.is_balanced:
            batch.status = BatchStatus.COMPLETED
            logger.info(
                f"Batch {batch.batch_number} completed: "
                f"{batch.filled_quantity} per leg"
            )
        elif batch.filled_quantity > 0:
            batch.status = BatchStatus.PARTIAL
            logger.warning(
                f"Batch {batch.batch_number} partial: "
                f"imbalance={batch.get_imbalance()}"
            )
        else:
            batch.status = BatchStatus.FAILED
            logger.error(f"Batch {batch.batch_number} failed: no fills")

        return batch

    def _place_all_orders(self, batch: Batch) -> None:
        """Place all 4 leg orders."""
        for leg_type, leg in batch.legs.items():
            try:
                order_id = self.kite.place_order(
                    tradingsymbol=leg.tradingsymbol,
                    transaction_type=leg.transaction_type,
                    quantity=leg.quantity,
                    price=leg.price,
                    order_type=leg.order_type
                )
                leg.order_id = order_id
                leg.status = LegStatus.PLACED
                leg.placed_at = datetime.now()
                logger.debug(f"Placed {leg_type}: {order_id}")

            except Exception as e:
                leg.status = LegStatus.REJECTED
                leg.error_message = str(e)
                logger.error(f"Failed to place {leg_type}: {e}")

    def _wait_for_fills(self, batch: Batch) -> None:
        """Wait for all orders to fill or timeout."""
        start_time = time.time()
        pending_legs = [
            leg for leg in batch.legs.values()
            if leg.status == LegStatus.PLACED
        ]

        while pending_legs and (time.time() - start_time) < self.order_timeout:
            for leg in pending_legs[:]:  # Copy list for safe modification
                if leg.order_id is None:
                    pending_legs.remove(leg)
                    continue

                try:
                    status = self.kite.get_order_status(leg.order_id)
                    order_status = status.get('status', '')

                    if order_status == 'COMPLETE':
                        leg.status = LegStatus.FILLED
                        leg.fill_price = status.get('average_price', leg.price or 0)
                        leg.filled_qty = status.get('filled_quantity', leg.quantity)
                        leg.filled_at = datetime.now()
                        pending_legs.remove(leg)
                        logger.debug(
                            f"{leg.leg_type} filled: {leg.fill_price} "
                            f"({leg.filled_qty} qty)"
                        )

                    elif order_status in ('REJECTED', 'CANCELLED'):
                        leg.status = LegStatus.REJECTED
                        leg.error_message = status.get('status_message', order_status)
                        pending_legs.remove(leg)
                        logger.warning(
                            f"{leg.leg_type} {order_status}: {leg.error_message}"
                        )

                except Exception as e:
                    logger.warning(f"Error checking {leg.leg_type}: {e}")

            if pending_legs:
                time.sleep(0.5)

        # Handle timeout - convert LIMIT orders to MARKET
        for leg in pending_legs:
            if leg.order_type == "LIMIT":
                logger.warning(
                    f"{leg.leg_type} timeout after {self.order_timeout}s, "
                    f"converting to MARKET"
                )
                self._convert_to_market(leg)

    def _convert_to_market(self, leg: LegOrder) -> None:
        """
        Convert unfilled LIMIT order to MARKET.

        Steps:
        1. Cancel existing order
        2. Wait for cancel confirmation
        3. Place MARKET order for unfilled quantity
        """
        if leg.order_id is None:
            return

        try:
            # Step 1: Cancel existing order
            self.kite.cancel_order(leg.order_id)
            logger.debug(f"Cancelled {leg.leg_type} order {leg.order_id}")

            # Step 2: Wait for cancel to settle
            time.sleep(self.cancel_wait)

            # Check current filled quantity after cancel
            # ISSUE-001 FIX: Initialize status_data outside try block
            status_data: Dict[str, Any] = {}
            try:
                status_data = self.kite.get_order_status(leg.order_id)
                leg.filled_qty = status_data.get('filled_quantity', 0)
            except Exception:
                pass  # Keep existing filled_qty

            # Calculate unfilled quantity
            unfilled = leg.quantity - leg.filled_qty
            if unfilled <= 0:
                # Order was filled during cancel
                leg.status = LegStatus.FILLED
                # ISSUE-001 FIX: Use status_data with safe fallback
                leg.fill_price = status_data.get('average_price', leg.price or 0)
                leg.filled_at = datetime.now()
                return

            # Step 3: Place MARKET order for unfilled portion
            order_id = self.kite.place_order(
                tradingsymbol=leg.tradingsymbol,
                transaction_type=leg.transaction_type,
                quantity=unfilled,
                price=None,
                order_type="MARKET"
            )

            leg.order_id = order_id
            leg.order_type = "MARKET"

            # ISSUE-006 FIX: Poll for MARKET fill with timeout instead of single check
            market_timeout = 10  # seconds
            market_start = time.time()
            market_status: Dict[str, Any] = {}

            while (time.time() - market_start) < market_timeout:
                time.sleep(1)
                try:
                    market_status = self.kite.get_order_status(order_id)
                    order_state = market_status.get('status', '')

                    if order_state == 'COMPLETE':
                        break
                    elif order_state in ('REJECTED', 'CANCELLED'):
                        break
                except Exception as e:
                    logger.warning(f"Error polling MARKET order: {e}")

            if market_status.get('status') == 'COMPLETE':
                # Calculate weighted average fill price
                market_fill_price = market_status.get('average_price', 0)
                market_filled_qty = market_status.get('filled_quantity', unfilled)

                if leg.filled_qty > 0 and leg.fill_price:
                    # Weighted average of limit fill and market fill
                    total_qty = leg.filled_qty + market_filled_qty
                    leg.fill_price = (
                        (leg.fill_price * leg.filled_qty) +
                        (market_fill_price * market_filled_qty)
                    ) / total_qty
                else:
                    leg.fill_price = market_fill_price

                leg.filled_qty = leg.quantity
                leg.status = LegStatus.FILLED
                leg.filled_at = datetime.now()
                logger.info(
                    f"{leg.leg_type} MARKET filled: {leg.fill_price}"
                )
            else:
                leg.status = LegStatus.REJECTED
                leg.error_message = f"MARKET order failed: {market_status.get('status_message', 'Timeout or rejection')}"
                logger.error(leg.error_message)

        except Exception as e:
            leg.status = LegStatus.REJECTED
            leg.error_message = f"Market conversion failed: {e}"
            logger.error(f"Market conversion failed for {leg.leg_type}: {e}")


# =============================================================================
# POSITION REBALANCER
# =============================================================================

class PositionRebalancer:
    """
    Restores position balance by trimming excess legs.

    When fills are unequal, this component places reverse orders to
    trim excess quantities and restore position integrity.
    """

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]) -> None:
        """
        Initialize position rebalancer.

        Args:
            kite: Kite client instance
            config: Configuration dictionary
        """
        self.kite = kite
        rebalance_config = config.get('scaling', {}).get('rebalance', {})
        self.max_attempts: int = rebalance_config.get(
            'max_attempts', DEFAULT_MAX_REBALANCE_ATTEMPTS
        )

    def rebalance_batch(self, batch: Batch) -> bool:
        """
        Rebalance a batch by trimming excess quantities.

        Args:
            batch: Batch with imbalanced fills

        Returns:
            True if successfully rebalanced
        """
        if batch.is_balanced:
            return True

        imbalance = batch.get_imbalance()
        logger.info(
            f"Rebalancing batch {batch.batch_number}: "
            f"imbalance={imbalance}"
        )

        # Find legs with excess (positive imbalance)
        success = True
        for leg_type, excess in imbalance.items():
            if excess <= 0:
                continue

            leg = batch.legs[leg_type]

            # Create reverse order to trim excess
            reverse_txn = "BUY" if leg.transaction_type == "SELL" else "SELL"

            trimmed = False
            for attempt in range(self.max_attempts):
                try:
                    order_id = self.kite.place_order(
                        tradingsymbol=leg.tradingsymbol,
                        transaction_type=reverse_txn,
                        quantity=excess,
                        price=None,
                        order_type="MARKET"
                    )

                    # Wait for fill
                    time.sleep(2)
                    rebalance_status = self.kite.get_order_status(order_id)

                    if rebalance_status.get('status') == 'COMPLETE':
                        # ISSUE-007 FIX: Use actual filled quantity, not expected excess
                        actual_trimmed = rebalance_status.get('filled_quantity', excess)
                        leg.filled_qty -= actual_trimmed

                        # Record rebalance order
                        rebalance_order = LegOrder(
                            leg_type=f"rebalance_{leg_type}",
                            tradingsymbol=leg.tradingsymbol,
                            transaction_type=reverse_txn,
                            quantity=excess,
                            order_type="MARKET",
                            price=None,
                            status=LegStatus.FILLED,
                            order_id=order_id,
                            fill_price=rebalance_status.get('average_price', 0),
                            filled_qty=actual_trimmed,
                            filled_at=datetime.now()
                        )
                        batch.rebalance_orders.append(rebalance_order)

                        logger.info(
                            f"Trimmed {actual_trimmed} from {leg_type} "
                            f"@ {rebalance_order.fill_price}"
                        )
                        trimmed = True
                        break

                    else:
                        logger.warning(
                            f"Rebalance order {order_id} not complete: "
                            f"{rebalance_status.get('status')}"
                        )

                except Exception as e:
                    logger.error(
                        f"Rebalance attempt {attempt + 1} failed for "
                        f"{leg_type}: {e}"
                    )

            if not trimmed:
                success = False

        # Verify balance restored
        if batch.is_balanced:
            batch.status = BatchStatus.REBALANCED
            logger.info(f"Batch {batch.batch_number} rebalanced successfully")
            return True

        logger.error(
            f"Batch {batch.batch_number} rebalance failed: "
            f"remaining imbalance={batch.get_imbalance()}"
        )
        return success


# =============================================================================
# SCALED ORDER EXECUTOR (MAIN ORCHESTRATOR)
# =============================================================================

class ScaledOrderExecutor:
    """
    Main orchestrator for scaled order execution.

    Coordinates BatchPlanner, DepthAnalyzer, BatchExecutor, and
    PositionRebalancer to execute large Iron Fly orders in batches.

    Key invariant: Position is ALWAYS balanced after each batch.
    """

    def __init__(
        self,
        kite: SNAILKiteClient,
        config: Dict[str, Any],
        paper_trading: bool = False
    ) -> None:
        """
        Initialize scaled order executor.

        Args:
            kite: Kite client instance
            config: Configuration dictionary
            paper_trading: Whether in paper trading mode
        """
        self.kite = kite
        self.config = config
        self.paper_trading = paper_trading

        self.planner = BatchPlanner(config)
        self.depth_analyzer = DepthAnalyzer(kite, config)
        self.batch_executor = BatchExecutor(kite, config)
        self.rebalancer = PositionRebalancer(kite, config)

        exec_config = config.get('scaling', {}).get('execution', {})
        self.inter_batch_delay: float = exec_config.get(
            'inter_batch_delay_ms', DEFAULT_INTER_BATCH_DELAY_MS
        ) / 1000

        # Get order types from config
        order_types_config = config.get('scaling', {}).get('order_types', {})
        self.order_types: Dict[str, str] = {
            'atm_straddle': order_types_config.get('atm_straddle', 'MARKET'),
            'otm_wings': order_types_config.get('otm_wings', 'LIMIT')
        }

    def execute_entry(
        self,
        symbols: Dict[str, str],
        total_quantity: int,
        quotes: Dict[str, Quote],
        instrument: str = "NIFTY"
    ) -> ExecutionResult:
        """
        Execute scaled Iron Fly entry.

        Args:
            symbols: Dict mapping leg_type to tradingsymbol
            total_quantity: Total contracts per leg
            quotes: Current quotes for all legs (for fallback pricing)
            instrument: Index name for freeze limit

        Returns:
            ExecutionResult with complete execution details

        Raises:
            ValueError: If symbols dict doesn't have exactly 4 required legs
        """
        start_time = time.time()
        errors: List[str] = []
        warnings: List[str] = []

        # ISSUE-005 FIX: Validate symbols dict has all required legs
        required_legs = {'straddle_ce', 'straddle_pe', 'wing_ce', 'wing_pe'}
        if set(symbols.keys()) != required_legs:
            missing = required_legs - set(symbols.keys())
            extra = set(symbols.keys()) - required_legs
            error_msg = f"Invalid symbols dict. Missing: {missing}, Extra: {extra}"
            logger.error(error_msg)
            return ExecutionResult(
                success=False,
                total_filled_qty=0,
                target_qty=total_quantity,
                batches_completed=0,
                batches_total=0,
                total_slippage=0.0,
                execution_time_seconds=0.0,
                leg_fills={},
                errors=[error_msg],
                warnings=[]
            )

        logger.info(
            f"Starting scaled entry: {total_quantity} contracts, "
            f"instrument={instrument}, paper_trading={self.paper_trading}"
        )

        # Step 1: Create execution plan
        plan = self.planner.create_plan(
            total_quantity=total_quantity,
            symbols=symbols,
            instrument=instrument,
            order_types=self.order_types
        )

        # Step 2: Execute batches
        completed_batches = 0
        total_filled = 0
        leg_fill_totals: Dict[str, Tuple[float, int]] = {
            leg: (0.0, 0) for leg in symbols
        }  # (total_value, total_qty)

        for batch_idx, batch in enumerate(plan.batches):
            try:
                # Set prices for this batch
                self._set_batch_prices(batch, symbols, quotes)

                # Execute batch
                batch = self.batch_executor.execute_batch(batch)

                # Handle batch 1 failure - abort entire execution
                if batch_idx == 0 and batch.status == BatchStatus.FAILED:
                    errors.append("Batch 1 failed - aborting execution (no position)")
                    logger.error("Batch 1 failed - aborting scaled execution")
                    break

                # Handle partial fills with rebalancing
                if batch.status == BatchStatus.PARTIAL:
                    warnings.append(
                        f"Batch {batch.batch_number} had partial fills"
                    )

                    if self.rebalancer.rebalance_batch(batch):
                        logger.info(
                            f"Batch {batch.batch_number} rebalanced successfully"
                        )
                    else:
                        errors.append(
                            f"Batch {batch.batch_number} rebalance failed"
                        )
                        # Stop execution - position integrity at risk
                        logger.error(
                            "Rebalance failed - stopping to preserve position integrity"
                        )
                        break

                # Aggregate results if batch succeeded
                if batch.status in (BatchStatus.COMPLETED, BatchStatus.REBALANCED):
                    completed_batches += 1
                    batch_filled = batch.filled_quantity
                    total_filled += batch_filled

                    # Accumulate leg fill statistics
                    for leg_type, leg in batch.legs.items():
                        if leg.status == LegStatus.FILLED and leg.fill_price:
                            prev_value, prev_qty = leg_fill_totals[leg_type]
                            leg_fill_totals[leg_type] = (
                                prev_value + (leg.fill_price * leg.filled_qty),
                                prev_qty + leg.filled_qty
                            )

                # Inter-batch delay (except for last batch)
                if batch != plan.batches[-1] and batch.status != BatchStatus.FAILED:
                    logger.debug(f"Inter-batch delay: {self.inter_batch_delay}s")
                    time.sleep(self.inter_batch_delay)

            except Exception as e:
                errors.append(f"Batch {batch.batch_number} failed: {e}")
                logger.error(f"Batch {batch.batch_number} error: {e}")

                # Stop on first batch failure, continue otherwise if we have position
                if batch_idx == 0:
                    break

        # Calculate weighted average fill prices and total slippage
        leg_fills: Dict[str, float] = {}
        total_slippage = 0.0

        for leg_type, (total_value, total_qty) in leg_fill_totals.items():
            if total_qty > 0:
                avg_fill = total_value / total_qty
                leg_fills[leg_type] = avg_fill

                # Calculate slippage vs quote
                if leg_type in quotes:
                    quote = quotes[leg_type]
                    if leg_type.startswith('straddle'):
                        # SELL - slippage is bid minus fill (positive = bad)
                        expected = quote.bid
                        slippage = (expected - avg_fill) * total_qty
                    else:
                        # BUY - slippage is fill minus ask (positive = bad)
                        expected = quote.ask
                        slippage = (avg_fill - expected) * total_qty

                    total_slippage += max(0, slippage)  # Only count unfavorable

        execution_time = time.time() - start_time

        result = ExecutionResult(
            success=total_filled > 0,
            total_filled_qty=total_filled,
            target_qty=total_quantity,
            batches_completed=completed_batches,
            batches_total=plan.num_batches,
            total_slippage=total_slippage,
            execution_time_seconds=execution_time,
            leg_fills=leg_fills,
            errors=errors,
            warnings=warnings
        )

        logger.info(
            f"Scaled entry complete: {total_filled}/{total_quantity} filled "
            f"in {execution_time:.1f}s, slippage={total_slippage:.2f}"
        )

        return result

    def _set_batch_prices(
        self,
        batch: Batch,
        symbols: Dict[str, str],
        quotes: Dict[str, Quote]
    ) -> None:
        """
        Set prices for all legs in a batch.

        In paper trading mode, uses fixed slippage.
        In live mode, analyzes depth for optimal slippage.

        Args:
            batch: Batch to update
            symbols: Mapping of leg_type to tradingsymbol
            quotes: Current quotes for fallback pricing
        """
        if self.paper_trading:
            # Paper trading: use fixed slippage from quotes
            self._set_prices_from_quotes(batch, quotes)
            return

        # Live trading: try depth analysis with quote fallback
        instruments = [f"NFO:{s}" for s in symbols.values()]
        depth_data = self.depth_analyzer.fetch_depth(instruments)

        for leg_type, leg in batch.legs.items():
            # MARKET orders don't need price
            if leg.order_type == "MARKET":
                continue

            # Try to find depth for this leg
            depth: Optional[MarketDepth] = None
            for key, d in depth_data.items():
                if leg.tradingsymbol in key or d.tradingsymbol == leg.tradingsymbol:
                    depth = d
                    break

            # Calculate slippage ticks
            slippage_ticks = self.depth_analyzer.calculate_slippage_ticks(
                depth=depth,
                quantity=leg.quantity,
                side=leg.transaction_type,
                batch_number=batch.batch_number
            )

            # Get base price from depth or quotes
            if depth:
                base_price = (
                    depth.best_ask if leg.transaction_type == "BUY"
                    else depth.best_bid
                )
            else:
                # Fallback to quotes
                if leg_type in quotes:
                    quote = quotes[leg_type]
                    base_price = (
                        quote.ask if leg.transaction_type == "BUY"
                        else quote.bid
                    )
                else:
                    logger.warning(
                        f"No price data for {leg_type}, using LTP"
                    )
                    base_price = depth.ltp if depth else 0

            if base_price <= 0:
                logger.error(f"Invalid base price for {leg_type}: {base_price}")
                continue

            # Calculate price with slippage
            leg.price = self.depth_analyzer.calculate_price_with_slippage(
                base_price=base_price,
                transaction_type=leg.transaction_type,
                slippage_ticks=slippage_ticks
            )

            logger.debug(
                f"{leg_type}: base={base_price:.2f}, "
                f"slippage={slippage_ticks} ticks, "
                f"price={leg.price:.2f}"
            )

    def _set_prices_from_quotes(
        self,
        batch: Batch,
        quotes: Dict[str, Quote]
    ) -> None:
        """
        Set prices from quotes with fixed slippage (paper trading mode).

        Args:
            batch: Batch to update
            quotes: Current quotes
        """
        for leg_type, leg in batch.legs.items():
            if leg.order_type == "MARKET":
                continue

            if leg_type not in quotes:
                logger.warning(f"No quote for {leg_type} in paper mode")
                continue

            quote = quotes[leg_type]

            # Use base slippage ticks
            slippage_ticks = self.depth_analyzer.base_ticks + (batch.batch_number - 1)

            if leg.transaction_type == "BUY":
                base_price = quote.ask
            else:
                base_price = quote.bid

            leg.price = self.depth_analyzer.calculate_price_with_slippage(
                base_price=base_price,
                transaction_type=leg.transaction_type,
                slippage_ticks=slippage_ticks
            )

    # =========================================================================
    # EXIT EXECUTION
    # =========================================================================

    def execute_exit(
        self,
        symbols: Dict[str, str],
        total_quantity: int,
        quotes: Dict[str, Quote],
        instrument: str = "NIFTY",
        urgent: bool = False
    ) -> ExecutionResult:
        """
        Execute scaled Iron Fly exit.

        CRITICAL: Exit sequence closes SHORT positions (straddle) FIRST
        to release margin before closing LONG positions (wings). This prevents
        naked short straddle exposure which would cause massive margin spike.

        Exit sequence per batch:
        1. BUY Straddle CE (close short) - Release margin FIRST
        2. BUY Straddle PE (close short) - Release margin
        3. SELL Wing CE (close long) - Then close protection
        4. SELL Wing PE (close long) - Finally close protection

        Args:
            symbols: Dict mapping leg_type to tradingsymbol
            total_quantity: Total contracts per leg to exit
            quotes: Current quotes for all legs
            instrument: Index name for freeze limit
            urgent: If True, use MARKET orders for all legs (emergency exit)

        Returns:
            ExecutionResult with complete execution details
        """
        start_time = time.time()
        errors: List[str] = []
        warnings: List[str] = []

        # ISSUE-005 FIX: Validate symbols dict has all required legs
        required_legs = {'straddle_ce', 'straddle_pe', 'wing_ce', 'wing_pe'}
        if set(symbols.keys()) != required_legs:
            missing = required_legs - set(symbols.keys())
            extra = set(symbols.keys()) - required_legs
            error_msg = f"Invalid symbols dict for exit. Missing: {missing}, Extra: {extra}"
            logger.error(error_msg)
            return ExecutionResult(
                success=False,
                total_filled_qty=0,
                target_qty=total_quantity,
                batches_completed=0,
                batches_total=0,
                total_slippage=0.0,
                execution_time_seconds=0.0,
                leg_fills={},
                errors=[error_msg],
                warnings=[]
            )

        logger.info(
            f"Starting scaled exit: {total_quantity} contracts, "
            f"instrument={instrument}, urgent={urgent}, paper_trading={self.paper_trading}"
        )

        # Step 1: Create exit execution plan
        # For exit, order types are different:
        # - Straddle (close SHORT): BUY - want guaranteed fill, use MARKET or aggressive LIMIT
        # - Wings (close LONG): SELL - less urgent, can use LIMIT
        exit_order_types = {
            'atm_straddle': 'MARKET' if urgent else self.order_types.get('atm_straddle', 'MARKET'),
            'otm_wings': 'MARKET' if urgent else self.order_types.get('otm_wings', 'LIMIT')
        }

        plan = self._create_exit_plan(
            total_quantity=total_quantity,
            symbols=symbols,
            instrument=instrument,
            order_types=exit_order_types
        )

        # Step 2: Execute batches
        completed_batches = 0
        total_closed = 0
        leg_fill_totals: Dict[str, Tuple[float, int]] = {
            leg: (0.0, 0) for leg in symbols
        }

        for batch_idx, batch in enumerate(plan.batches):
            try:
                # Set exit prices for this batch
                self._set_exit_batch_prices(batch, symbols, quotes, urgent)

                # Execute batch
                batch = self.batch_executor.execute_batch(batch)

                # For exit, batch 1 failure is CRITICAL - we have open position!
                if batch_idx == 0 and batch.status == BatchStatus.FAILED:
                    errors.append("EXIT BATCH 1 FAILED - CRITICAL! Position still open!")
                    logger.critical("EXIT Batch 1 failed - position remains open!")
                    # Don't break - try remaining batches if partial fill
                    if batch.filled_quantity == 0:
                        break

                # Handle partial fills with rebalancing
                if batch.status == BatchStatus.PARTIAL:
                    warnings.append(
                        f"Exit batch {batch.batch_number} had partial fills"
                    )

                    if self.rebalancer.rebalance_batch(batch):
                        logger.info(
                            f"Exit batch {batch.batch_number} rebalanced successfully"
                        )
                    else:
                        errors.append(
                            f"Exit batch {batch.batch_number} rebalance failed - CRITICAL"
                        )
                        # For exit, we MUST continue trying - can't leave position open
                        logger.error(
                            "Exit rebalance failed - continuing to close remaining position"
                        )

                # Aggregate results if batch succeeded
                if batch.status in (BatchStatus.COMPLETED, BatchStatus.REBALANCED):
                    completed_batches += 1
                    batch_closed = batch.filled_quantity
                    total_closed += batch_closed

                    # Accumulate leg fill statistics
                    for leg_type, leg in batch.legs.items():
                        if leg.status == LegStatus.FILLED and leg.fill_price:
                            prev_value, prev_qty = leg_fill_totals[leg_type]
                            leg_fill_totals[leg_type] = (
                                prev_value + (leg.fill_price * leg.filled_qty),
                                prev_qty + leg.filled_qty
                            )
                elif batch.filled_quantity > 0:
                    # Partial success - still count what we closed
                    total_closed += batch.filled_quantity
                    for leg_type, leg in batch.legs.items():
                        if leg.filled_qty > 0 and leg.fill_price:
                            prev_value, prev_qty = leg_fill_totals[leg_type]
                            leg_fill_totals[leg_type] = (
                                prev_value + (leg.fill_price * leg.filled_qty),
                                prev_qty + leg.filled_qty
                            )

                # Shorter delay for exit (urgency)
                exit_delay = self.inter_batch_delay / 2 if not urgent else 0.5
                if batch != plan.batches[-1]:
                    logger.debug(f"Exit inter-batch delay: {exit_delay}s")
                    time.sleep(exit_delay)

            except Exception as e:
                errors.append(f"Exit batch {batch.batch_number} failed: {e}")
                logger.error(f"Exit batch {batch.batch_number} error: {e}")
                # For exit, NEVER stop - try to close as much as possible

        # Calculate weighted average fill prices and total slippage
        leg_fills: Dict[str, float] = {}
        total_slippage = 0.0

        for leg_type, (total_value, total_qty) in leg_fill_totals.items():
            if total_qty > 0:
                avg_fill = total_value / total_qty
                leg_fills[leg_type] = avg_fill

                # Calculate exit slippage vs quote
                if leg_type in quotes:
                    quote = quotes[leg_type]
                    if leg_type.startswith('straddle'):
                        # BUY to close - slippage is fill minus ask (positive = bad)
                        expected = quote.ask
                        slippage = (avg_fill - expected) * total_qty
                    else:
                        # SELL to close - slippage is bid minus fill (positive = bad)
                        expected = quote.bid
                        slippage = (expected - avg_fill) * total_qty

                    total_slippage += max(0, slippage)

        execution_time = time.time() - start_time

        # Check if full exit achieved
        remaining = total_quantity - total_closed
        if remaining > 0:
            errors.append(f"INCOMPLETE EXIT: {remaining} contracts still open!")
            logger.critical(f"Exit incomplete - {remaining} contracts remain open!")

        result = ExecutionResult(
            success=total_closed > 0,
            total_filled_qty=total_closed,
            target_qty=total_quantity,
            batches_completed=completed_batches,
            batches_total=plan.num_batches,
            total_slippage=total_slippage,
            execution_time_seconds=execution_time,
            leg_fills=leg_fills,
            errors=errors,
            warnings=warnings
        )

        logger.info(
            f"Scaled exit complete: {total_closed}/{total_quantity} closed "
            f"in {execution_time:.1f}s, slippage={total_slippage:.2f}"
        )

        return result

    def _create_exit_plan(
        self,
        total_quantity: int,
        symbols: Dict[str, str],
        instrument: str,
        order_types: Dict[str, str]
    ) -> ExecutionPlan:
        """
        Create execution plan for exit with correct transaction types.

        Exit transaction types are REVERSED from entry:
        - Straddle: SELL at entry -> BUY at exit (close short)
        - Wings: BUY at entry -> SELL at exit (close long)

        Args:
            total_quantity: Total contracts per leg
            symbols: Mapping of leg_type to tradingsymbol
            instrument: Index name for freeze limit
            order_types: Order types for each leg category

        Returns:
            ExecutionPlan with exit batches
        """
        freeze_limit = self.planner.get_freeze_limit(instrument)
        num_batches = math.ceil(total_quantity / freeze_limit)

        batches: List[Batch] = []
        remaining = total_quantity

        for i in range(num_batches):
            batch_qty = min(freeze_limit, remaining)

            legs: Dict[str, LegOrder] = {}
            for leg_type, symbol in symbols.items():
                # EXIT: Transaction types are REVERSED from entry
                if leg_type.startswith('straddle'):
                    # Close SHORT position -> BUY
                    order_type = order_types.get('atm_straddle', 'MARKET')
                    transaction_type = "BUY"
                else:  # wing
                    # Close LONG position -> SELL
                    order_type = order_types.get('otm_wings', 'LIMIT')
                    transaction_type = "SELL"

                legs[leg_type] = LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=batch_qty,
                    order_type=order_type,
                    price=None
                )

            batches.append(Batch(
                batch_number=i + 1,
                quantity_per_leg=batch_qty,
                legs=legs
            ))

            remaining -= batch_qty

        logger.info(
            f"Created EXIT plan: {num_batches} batches for {total_quantity} contracts "
            f"(freeze_limit={freeze_limit})"
        )

        return ExecutionPlan(
            total_quantity=total_quantity,
            freeze_limit=freeze_limit,
            num_batches=num_batches,
            batches=batches,
            symbols=symbols
        )

    def _set_exit_batch_prices(
        self,
        batch: Batch,
        symbols: Dict[str, str],
        quotes: Dict[str, Quote],
        urgent: bool = False
    ) -> None:
        """
        Set prices for exit batch legs.

        Exit pricing is REVERSED from entry:
        - Straddle (BUY to close): Use ASK + slippage (willing to pay more)
        - Wings (SELL to close): Use BID - slippage (willing to accept less)

        Args:
            batch: Batch to update
            symbols: Mapping of leg_type to tradingsymbol
            quotes: Current quotes
            urgent: If True, use more aggressive slippage
        """
        # Use more aggressive slippage for exit
        exit_slippage_multiplier = 1.5 if not urgent else 2.0

        if self.paper_trading:
            # Paper trading: use fixed slippage
            for leg_type, leg in batch.legs.items():
                if leg.order_type == "MARKET":
                    continue

                if leg_type not in quotes:
                    continue

                quote = quotes[leg_type]
                base_ticks = int(self.depth_analyzer.base_ticks * exit_slippage_multiplier)
                slippage_ticks = base_ticks + (batch.batch_number - 1)

                if leg.transaction_type == "BUY":
                    # Closing short - use ask + slippage
                    base_price = quote.ask
                else:
                    # Closing long - use bid - slippage
                    base_price = quote.bid

                leg.price = self.depth_analyzer.calculate_price_with_slippage(
                    base_price=base_price,
                    transaction_type=leg.transaction_type,
                    slippage_ticks=slippage_ticks
                )
            return

        # Live trading: depth analysis
        instruments = [f"NFO:{s}" for s in symbols.values()]
        depth_data = self.depth_analyzer.fetch_depth(instruments)

        for leg_type, leg in batch.legs.items():
            if leg.order_type == "MARKET":
                continue

            # Find depth
            depth: Optional[MarketDepth] = None
            for key, d in depth_data.items():
                if leg.tradingsymbol in key or d.tradingsymbol == leg.tradingsymbol:
                    depth = d
                    break

            # Calculate slippage with exit multiplier
            base_slippage = self.depth_analyzer.calculate_slippage_ticks(
                depth=depth,
                quantity=leg.quantity,
                side=leg.transaction_type,
                batch_number=batch.batch_number
            )
            slippage_ticks = int(base_slippage * exit_slippage_multiplier)

            # Get base price
            if depth:
                base_price = (
                    depth.best_ask if leg.transaction_type == "BUY"
                    else depth.best_bid
                )
            elif leg_type in quotes:
                quote = quotes[leg_type]
                base_price = (
                    quote.ask if leg.transaction_type == "BUY"
                    else quote.bid
                )
            else:
                logger.warning(f"No price data for exit {leg_type}")
                continue

            if base_price <= 0:
                logger.error(f"Invalid exit base price for {leg_type}")
                continue

            leg.price = self.depth_analyzer.calculate_price_with_slippage(
                base_price=base_price,
                transaction_type=leg.transaction_type,
                slippage_ticks=slippage_ticks
            )

            logger.debug(
                f"EXIT {leg_type}: base={base_price:.2f}, "
                f"slippage={slippage_ticks} ticks, "
                f"price={leg.price:.2f}"
            )


# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def should_use_scaled_execution(quantity: int, config: Dict[str, Any]) -> bool:
    """
    Determine if scaled execution is needed based on quantity.

    Args:
        quantity: Order quantity per leg
        config: Configuration dictionary

    Returns:
        True if quantity exceeds freeze limit
    """
    # ISSUE-009 FIX: Explicit type casting to avoid mypy Any return
    scaling_config = config.get('scaling', {})
    freeze_limits = scaling_config.get('freeze_limits', {}) if isinstance(scaling_config, dict) else {}
    freeze_limit: int = int(freeze_limits.get('NIFTY', DEFAULT_FREEZE_LIMIT)) if isinstance(freeze_limits, dict) else DEFAULT_FREEZE_LIMIT

    return bool(quantity > freeze_limit)


def get_scaling_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract scaling configuration with defaults.

    Args:
        config: Full configuration dictionary

    Returns:
        Scaling configuration with defaults applied
    """
    scaling = config.get('scaling', {})

    return {
        'freeze_limits': scaling.get('freeze_limits', {
            'NIFTY': 1800,
            'BANKNIFTY': 600,
            'FINNIFTY': 1200,
            'MIDCPNIFTY': 2800,
            'NIFTYNXT50': 600
        }),
        'execution': {
            'inter_batch_delay_ms': scaling.get('execution', {}).get(
                'inter_batch_delay_ms', DEFAULT_INTER_BATCH_DELAY_MS
            ),
            'order_timeout_seconds': scaling.get('execution', {}).get(
                'order_timeout_seconds', DEFAULT_ORDER_TIMEOUT_SECONDS
            ),
            'cancel_wait_seconds': scaling.get('execution', {}).get(
                'cancel_wait_seconds', DEFAULT_CANCEL_WAIT_SECONDS
            ),
            'max_retries_per_order': scaling.get('execution', {}).get(
                'max_retries_per_order', DEFAULT_MAX_RETRIES
            )
        },
        'order_types': {
            'atm_straddle': scaling.get('order_types', {}).get(
                'atm_straddle', 'MARKET'
            ),
            'otm_wings': scaling.get('order_types', {}).get(
                'otm_wings', 'LIMIT'
            )
        },
        'slippage': {
            'base_ticks': scaling.get('slippage', {}).get(
                'base_ticks', DEFAULT_BASE_TICKS
            ),
            'depth_buffer_ticks': scaling.get('slippage', {}).get(
                'depth_buffer_ticks', DEFAULT_DEPTH_BUFFER_TICKS
            ),
            'max_ticks': scaling.get('slippage', {}).get(
                'max_ticks', DEFAULT_MAX_TICKS
            ),
            'wing_retry_increment': scaling.get('slippage', {}).get(
                'wing_retry_increment', DEFAULT_WING_RETRY_INCREMENT
            )
        },
        'rebalance': {
            'max_attempts': scaling.get('rebalance', {}).get(
                'max_attempts', DEFAULT_MAX_REBALANCE_ATTEMPTS
            ),
            'strategy': scaling.get('rebalance', {}).get(
                'strategy', 'trim_to_balance'
            )
        }
    }


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':

    print("\n" + "=" * 60)
    print("SNAIL Scaled Execution Module Test")
    print("=" * 60)

    # Test BatchPlanner
    print("\n[1] Testing BatchPlanner...")
    test_config = {
        'scaling': {
            'freeze_limits': {'NIFTY': 1800}
        }
    }
    planner = BatchPlanner(test_config)

    # Test 100 lots = 7500 contracts
    test_symbols = {
        'straddle_ce': 'NIFTY24D1924500CE',
        'straddle_pe': 'NIFTY24D1924500PE',
        'wing_ce': 'NIFTY24D1924800CE',
        'wing_pe': 'NIFTY24D1924200PE'
    }

    plan = planner.create_plan(
        total_quantity=7500,
        symbols=test_symbols,
        instrument='NIFTY'
    )

    print(f"    Total quantity: {plan.total_quantity}")
    print(f"    Freeze limit: {plan.freeze_limit}")
    print(f"    Num batches: {plan.num_batches}")
    for batch in plan.batches:
        print(f"    Batch {batch.batch_number}: {batch.quantity_per_leg} per leg")

    # Test helper functions
    print("\n[2] Testing helper functions...")
    print(f"    should_use_scaled_execution(75, config): "
          f"{should_use_scaled_execution(75, test_config)}")
    print(f"    should_use_scaled_execution(7500, config): "
          f"{should_use_scaled_execution(7500, test_config)}")

    # Test Batch balance checking
    print("\n[3] Testing Batch balance logic...")
    test_batch = plan.batches[0]

    # Simulate unequal fills
    test_batch.legs['straddle_ce'].filled_qty = 1800
    test_batch.legs['straddle_pe'].filled_qty = 1800
    test_batch.legs['wing_ce'].filled_qty = 1800
    test_batch.legs['wing_pe'].filled_qty = 1500  # Partial fill

    print(f"    is_balanced: {test_batch.is_balanced}")
    print(f"    filled_quantity: {test_batch.filled_quantity}")
    print(f"    imbalance: {test_batch.get_imbalance()}")

    print("\n" + "=" * 60)
    print("Scaled execution module test complete!")
    print("=" * 60 + "\n")

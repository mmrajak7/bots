"""
SNAIL Order Helpers

Order execution utilities including validation, slippage handling, and batch operations.

@file        order_helpers.py
@description Order execution and validation utilities
@author      SNAIL Development Team
@created     2025-12-04
@version     1.0.0
@references  TECHNICAL_DESIGN_REFERENCE.md Section 4
"""

import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any, TypedDict
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

from src.api.kite_client import SNAILKiteClient, Quote, OrderExecutionError
from src.utils.calculations import NIFTY_LOT_SIZE
from src.utils.quote_reliability import (
    quote_reliable,
    price_anchor,
    cap_buy_price,
    floor_sell_price,
)


class SlippageTierConfig(TypedDict):
    """Type definition for slippage tier configuration."""
    ticks: List[int]
    interval_seconds: int
    final_market: bool


# =============================================================================
# CONSTANTS
# =============================================================================

# Order execution settings
MAX_ORDER_RETRIES = 3
RETRY_DELAY_SECONDS = 2
ORDER_POLL_INTERVAL = 0.5
ORDER_TIMEOUT_SECONDS = 30

# Tick sizes for NIFTY options
TICK_SIZE = 0.05

# Slippage settings (in ticks)
DEFAULT_SLIPPAGE_TICKS = 2  # 0.10 for options

# Tiered Slippage Configuration (TDD Section 1.3)
# Progressive slippage: Exact → +2pts → +3pts → MARKET (10s intervals)
SLIPPAGE_TIERS: Dict[str, SlippageTierConfig] = {
    "entry": {
        "ticks": [0, 2],           # Exact, then +2 ticks
        "interval_seconds": 10,
        "final_market": False      # Entry doesn't go to MARKET
    },
    "exit_normal": {
        "ticks": [0, 2, 3],        # Exact, +2, +3 ticks
        "interval_seconds": 10,
        "final_market": False
    },
    "exit_urgent": {
        "ticks": [0, 3, 5],        # Exact, +3, +5 ticks
        "interval_seconds": 8,
        "final_market": True       # Goes to MARKET if still unfilled
    },
    "exit_critical": {
        "ticks": [5],              # Start at +5 ticks
        "interval_seconds": 5,
        "final_market": True       # Goes to MARKET quickly
    }
}

# Bid-Ask spread thresholds (TDD Section 5.2)
# ATM options (straddle) have tighter spreads due to higher liquidity
# Wing options (OTM) have wider spreads due to lower liquidity
SPREAD_THRESHOLDS = {
    "atm": {"warn": 2.0, "block": 5.0},    # ATM: warn >2pts, block >5pts
    "wing": {"warn": 5.0, "block": 10.0},  # Wings: warn >5pts, block >10pts
}


# =============================================================================
# ENUMS
# =============================================================================

class OrderStatus(Enum):
    """Order status enumeration."""
    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TRIGGER_PENDING = "TRIGGER PENDING"


class TransactionType(Enum):
    """Transaction type enumeration."""
    BUY = "BUY"
    SELL = "SELL"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class OrderParams:
    """
    Parameters for order placement.

    Attributes:
        tradingsymbol: Symbol without exchange prefix
        transaction_type: BUY or SELL
        quantity: Order quantity
        price: Limit price (None for market)
        order_type: LIMIT or MARKET
        tag: Optional order tag for identification
    """
    tradingsymbol: str
    transaction_type: str
    quantity: int
    price: Optional[float] = None
    order_type: str = "LIMIT"
    tag: Optional[str] = None


@dataclass
class ExecutedOrder:
    """
    Result of an executed order.

    Attributes:
        order_id: Kite order ID
        tradingsymbol: Executed symbol
        transaction_type: BUY or SELL
        quantity: Filled quantity
        order_price: Original order price
        fill_price: Actual fill price
        slippage: Slippage in points
        status: Final order status
        timestamp: Execution timestamp
    """
    order_id: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    order_price: float
    fill_price: float
    slippage: float
    status: str
    timestamp: datetime = field(default_factory=datetime.now)


@dataclass
class IronFlyOrders:
    """
    Iron Fly position order details.

    Attributes:
        straddle_ce: Straddle CE order
        straddle_pe: Straddle PE order
        wing_ce: Wing CE order
        wing_pe: Wing PE order
    """
    straddle_ce: ExecutedOrder
    straddle_pe: ExecutedOrder
    wing_ce: ExecutedOrder
    wing_pe: ExecutedOrder

    @property
    def total_premium_collected(self) -> float:
        """Total premium collected from short legs."""
        return self.straddle_ce.fill_price + self.straddle_pe.fill_price

    @property
    def total_premium_paid(self) -> float:
        """Total premium paid for long legs."""
        return self.wing_ce.fill_price + self.wing_pe.fill_price

    @property
    def net_credit(self) -> float:
        """Net credit received."""
        return self.total_premium_collected - self.total_premium_paid

    @property
    def total_slippage(self) -> float:
        """Total slippage across all legs."""
        return (
            self.straddle_ce.slippage +
            self.straddle_pe.slippage +
            self.wing_ce.slippage +
            self.wing_pe.slippage
        )


@dataclass
class SpreadValidationResult:
    """
    Result of bid-ask spread validation.

    TDD Section 5.2: Bid-Ask Spread Validation

    Attributes:
        valid: True if spreads are within acceptable limits
        warnings: List of warning messages (elevated but acceptable)
        blocks: List of blocking issues (spreads too wide)
        total_spread_cost: Hidden cost due to spreads in INR
        spreads: Individual spread values by leg
    """
    valid: bool
    warnings: List[str] = field(default_factory=list)
    blocks: List[str] = field(default_factory=list)
    total_spread_cost: float = 0.0
    spreads: Dict[str, float] = field(default_factory=dict)


@dataclass
class ExitExecutionResult:
    """
    Result of Iron Fly exit execution with detailed leg status.

    Tracks which legs succeeded and which failed for proper
    error handling and partial exit recovery.

    Attributes:
        success: True if ALL legs closed successfully
        partial: True if SOME legs closed (requires manual intervention)
        orders: IronFlyOrders if complete, None if partial/failed
        legs_closed: List of leg names that were closed successfully
        legs_failed: Dict of leg names to error messages
        total_debit: Total debit paid to close (if any legs closed)
        total_credit: Total credit received from wings (if any)
        error: Overall error message if failed
    """
    success: bool
    partial: bool = False
    orders: Optional['IronFlyOrders'] = None
    legs_closed: List[str] = field(default_factory=list)
    legs_failed: Dict[str, str] = field(default_factory=dict)
    total_debit: float = 0.0
    total_credit: float = 0.0
    error: str = ""

    @property
    def net_exit_cost(self) -> float:
        """Net cost of exit (debit - credit from wings)."""
        return self.total_debit - self.total_credit

    def get_status_summary(self) -> str:
        """Get human-readable status summary."""
        if self.success:
            return f"EXIT COMPLETE: Net cost ₹{self.net_exit_cost:.2f}"
        elif self.partial:
            closed = ", ".join(self.legs_closed) or "None"
            failed = ", ".join(self.legs_failed.keys()) or "None"
            return f"PARTIAL EXIT: Closed [{closed}], Failed [{failed}]"
        else:
            return f"EXIT FAILED: {self.error}"


# =============================================================================
# PRICE UTILITIES
# =============================================================================

def round_to_tick(price: float, tick_size: float = TICK_SIZE) -> float:
    """
    Round price to nearest tick size.

    Args:
        price: Price to round
        tick_size: Tick size (default 0.05)

    Returns:
        Price rounded to tick size

    Example:
        >>> round_to_tick(123.47)
        123.45
        >>> round_to_tick(123.48)
        123.50
    """
    return round(price / tick_size) * tick_size


def apply_slippage(
    price: float,
    transaction_type: str,
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS
) -> float:
    """
    Apply slippage to price for order execution.

    For SELL orders (we want to sell), we reduce price slightly.
    For BUY orders (we want to buy), we increase price slightly.

    Args:
        price: Base price
        transaction_type: BUY or SELL
        slippage_ticks: Number of ticks to adjust

    Returns:
        Adjusted price rounded to tick

    Example:
        >>> apply_slippage(100.0, "SELL", 2)  # Sell slightly lower
        99.90
        >>> apply_slippage(100.0, "BUY", 2)   # Buy slightly higher
        100.10
    """
    adjustment = slippage_ticks * TICK_SIZE

    if transaction_type == "SELL":
        # Willing to sell slightly lower
        adjusted = price - adjustment
    else:
        # Willing to buy slightly higher
        adjusted = price + adjustment

    return round_to_tick(max(adjusted, TICK_SIZE))


def calculate_slippage(order_price: float, fill_price: float, transaction_type: str) -> float:
    """
    Calculate slippage from execution.

    Positive slippage = unfavorable (cost us money).
    Negative slippage = favorable (saved money).

    Args:
        order_price: Original order price
        fill_price: Actual fill price
        transaction_type: BUY or SELL

    Returns:
        Slippage in points (positive = unfavorable)
    """
    if transaction_type == "SELL":
        # For sells, lower fill = unfavorable
        return order_price - fill_price
    else:
        # For buys, higher fill = unfavorable
        return fill_price - order_price


# =============================================================================
# ORDER VALIDATION
# =============================================================================

def validate_order_params(params: OrderParams) -> Tuple[bool, List[str]]:
    """
    Validate order parameters before placement.

    Args:
        params: Order parameters to validate

    Returns:
        (is_valid, list of error messages)
    """
    errors = []

    # Validate tradingsymbol
    if not params.tradingsymbol:
        errors.append("tradingsymbol is required")
    elif not params.tradingsymbol.startswith("NIFTY"):
        errors.append(f"Invalid symbol format: {params.tradingsymbol}")

    # Validate transaction type
    if params.transaction_type not in ("BUY", "SELL"):
        errors.append(f"Invalid transaction_type: {params.transaction_type}")

    # Validate quantity
    if params.quantity <= 0:
        errors.append(f"Invalid quantity: {params.quantity}")
    elif params.quantity % NIFTY_LOT_SIZE != 0:
        errors.append(f"Quantity must be multiple of lot size ({NIFTY_LOT_SIZE}): {params.quantity}")

    # Validate price for LIMIT orders
    if params.order_type == "LIMIT":
        if params.price is None:
            errors.append("Price required for LIMIT orders")
        elif params.price <= 0:
            errors.append(f"Invalid price: {params.price}")

    return len(errors) == 0, errors


def validate_iron_fly_quotes(quotes: Dict[str, Quote]) -> Tuple[bool, List[str]]:
    """
    Validate quotes for Iron Fly entry.

    Checks:
    - All quotes present
    - Non-zero bids and asks
    - Reasonable bid-ask spreads

    Args:
        quotes: Dictionary of quotes by leg name

    Returns:
        (is_valid, list of warnings/errors)
    """
    required_legs = ['straddle_ce', 'straddle_pe', 'wing_ce', 'wing_pe']
    issues = []

    for leg in required_legs:
        if leg not in quotes:
            issues.append(f"Missing quote for {leg}")
            continue

        quote = quotes[leg]

        # Check for valid bid
        if quote.bid <= 0:
            issues.append(f"{leg}: Invalid bid price (0)")

        # Check for valid ask
        if quote.ask <= 0:
            issues.append(f"{leg}: Invalid ask price (0)")

        # Check bid-ask spread (warn if > 5%)
        if quote.bid > 0 and quote.ask > 0:
            spread_pct = (quote.ask - quote.bid) / quote.bid * 100
            if spread_pct > 5:
                issues.append(f"{leg}: Wide bid-ask spread ({spread_pct:.1f}%)")

    return len([i for i in issues if "Missing" in i or "Invalid" in i]) == 0, issues


def validate_bid_ask_spreads(
    quotes: Dict[str, Quote],
    quantity: int
) -> SpreadValidationResult:
    """
    Validate bid-ask spreads for all 4 Iron Fly legs before entry.

    TDD Section 5.2: Bid-Ask Spread Validation

    Wide spreads indicate illiquidity and cause hidden costs. This function
    validates spreads against TDD-specified thresholds:

    ATM Options (straddle):
    - Spread ≤ 2 pts: OK
    - Spread 2-5 pts: Warning (proceed with caution)
    - Spread > 5 pts: BLOCK entry

    Wing Options (OTM):
    - Spread ≤ 5 pts: OK
    - Spread 5-10 pts: Warning (proceed with caution)
    - Spread > 10 pts: BLOCK entry

    Args:
        quotes: Dict with Quote objects for each leg (straddle_ce, straddle_pe, wing_ce, wing_pe)
        quantity: Position quantity (for cost calculation)

    Returns:
        SpreadValidationResult with validation status and details

    Example:
        >>> result = validate_bid_ask_spreads(quotes, 65)
        >>> if not result.valid:
        ...     for block in result.blocks:
        ...         print(f"BLOCKED: {block}")
    """
    warnings = []
    blocks = []
    total_spread_cost = 0.0
    spreads = {}

    # Define leg types
    atm_legs = ['straddle_ce', 'straddle_pe']
    wing_legs = ['wing_ce', 'wing_pe']

    for leg in atm_legs + wing_legs:
        if leg not in quotes:
            blocks.append(f"{leg}: Missing quote data")
            continue

        quote = quotes[leg]

        # Skip if invalid quote
        if quote.bid <= 0 or quote.ask <= 0:
            blocks.append(f"{leg}: Invalid bid ({quote.bid}) or ask ({quote.ask})")
            continue

        # Calculate spread in points
        spread = quote.ask - quote.bid
        spreads[leg] = spread

        # Add to total spread cost (spread × quantity for each leg)
        total_spread_cost += spread * quantity

        # Determine threshold type
        if leg in atm_legs:
            thresholds = SPREAD_THRESHOLDS["atm"]
            leg_label = "ATM " + ("CE" if "ce" in leg else "PE")
        else:
            thresholds = SPREAD_THRESHOLDS["wing"]
            leg_label = "Wing " + ("CE" if "ce" in leg else "PE")

        # Check against thresholds
        if spread > thresholds["block"]:
            blocks.append(
                f"{leg_label} spread too wide: {spread:.2f} pts (max: {thresholds['block']:.0f} pts)"
            )
        elif spread > thresholds["warn"]:
            warnings.append(
                f"{leg_label} spread elevated: {spread:.2f} pts (threshold: {thresholds['warn']:.0f} pts)"
            )

    # Valid only if no blocking issues
    is_valid = len(blocks) == 0

    return SpreadValidationResult(
        valid=is_valid,
        warnings=warnings,
        blocks=blocks,
        total_spread_cost=total_spread_cost,
        spreads=spreads
    )


# =============================================================================
# ORDER EXECUTION
# =============================================================================

def place_order_with_retry(
    kite: SNAILKiteClient,
    params: OrderParams,
    max_retries: int = MAX_ORDER_RETRIES
) -> str:
    """
    Place order with retry logic.

    CRITICAL: Before retrying, checks today's orders for ghost orders
    (orders that were placed on exchange but client got network error).
    This prevents duplicate orders on the same symbol.

    Args:
        kite: Kite client instance
        params: Order parameters
        max_retries: Maximum retry attempts

    Returns:
        Order ID

    Raises:
        OrderExecutionError: If all retries fail
    """
    last_error = None

    for attempt in range(max_retries):
        try:
            # Before retry (attempt > 0), check for ghost orders from previous attempt
            if attempt > 0:
                ghost_id = _check_for_ghost_order(
                    kite, params.tradingsymbol, params.transaction_type, params.quantity
                )
                if ghost_id:
                    logger.warning(
                        f"Ghost order found on retry: {ghost_id} for {params.tradingsymbol}. "
                        f"Using existing order instead of placing duplicate."
                    )
                    return ghost_id

            order_id = kite.place_order(
                tradingsymbol=params.tradingsymbol,
                transaction_type=params.transaction_type,
                quantity=params.quantity,
                price=params.price,
                order_type=params.order_type
            )

            logger.info(f"Order placed: {order_id} for {params.tradingsymbol}")
            return order_id

        except Exception as e:
            last_error = e
            logger.warning(f"Order attempt {attempt + 1}/{max_retries} failed: {e}")

            if attempt < max_retries - 1:
                time.sleep(RETRY_DELAY_SECONDS)

    raise OrderExecutionError(f"Failed after {max_retries} attempts: {last_error}")


def _check_for_ghost_order(
    kite: SNAILKiteClient,
    symbol: str,
    txn_type: str,
    quantity: int
) -> Optional[str]:
    """
    Check today's orders for a ghost order matching the given parameters.

    A ghost order is one placed on exchange but client received network error.

    Returns:
        order_id if ghost found, None otherwise
    """
    try:
        from datetime import datetime as dt
        all_orders = kite.orders()
        now = dt.now()

        for order in all_orders:
            if (order.get('tradingsymbol') == symbol
                    and order.get('transaction_type') == txn_type
                    and order.get('quantity') == quantity
                    and order.get('status') in ('OPEN', 'COMPLETE', 'PENDING')):
                order_time = order.get('order_timestamp')
                if order_time:
                    try:
                        if isinstance(order_time, str):
                            order_dt = dt.fromisoformat(order_time)
                        else:
                            order_dt = order_time
                        age_seconds = (now - order_dt).total_seconds()
                        if age_seconds <= 120:  # 2 minutes
                            return order.get('order_id')
                    except (ValueError, TypeError):
                        pass
        return None
    except Exception as e:
        logger.warning(f"Ghost order check failed: {e}")
        return None


def wait_for_order_completion(
    kite: SNAILKiteClient,
    order_id: str,
    timeout: int = ORDER_TIMEOUT_SECONDS
) -> Dict[str, Any]:
    """
    Wait for order to complete or timeout.

    Args:
        kite: Kite client instance
        order_id: Order ID to monitor
        timeout: Timeout in seconds

    Returns:
        Final order details

    Raises:
        OrderExecutionError: If order rejected or timeout
    """
    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            order = kite.get_order_status(order_id)
            status = order.get('status', '')

            if status == 'COMPLETE':
                logger.info(f"Order {order_id} completed")
                return order

            elif status in ('REJECTED', 'CANCELLED'):
                reason = order.get('status_message', 'Unknown')
                raise OrderExecutionError(f"Order {status}: {reason}")

            # Still pending, wait
            time.sleep(ORDER_POLL_INTERVAL)

        except OrderExecutionError:
            raise
        except Exception as e:
            logger.warning(f"Error checking order status: {e}")
            time.sleep(ORDER_POLL_INTERVAL)

    # CRITICAL FIX: Cancel orphan order on timeout to prevent unexpected fills
    # Without this, timed-out orders can fill later causing position mismatches
    logger.warning(f"Order {order_id} timeout after {timeout}s - attempting to cancel")
    try:
        kite.cancel_order(order_id)
        logger.info(f"Order {order_id} cancelled after timeout")
    except Exception as cancel_err:
        logger.error(f"Failed to cancel timed-out order {order_id}: {cancel_err}")
        # Continue to raise timeout error - order may still be open!

    raise OrderExecutionError(f"Order {order_id} timeout after {timeout}s (cancel attempted)")


def execute_order(
    kite: SNAILKiteClient,
    params: OrderParams,
    wait_for_fill: bool = True
) -> ExecutedOrder:
    """
    Execute order with validation and optional fill waiting.

    Args:
        kite: Kite client instance
        params: Order parameters
        wait_for_fill: Whether to wait for fill

    Returns:
        ExecutedOrder with fill details

    Raises:
        OrderExecutionError: If execution fails
    """
    # Validate parameters
    is_valid, errors = validate_order_params(params)
    if not is_valid:
        raise OrderExecutionError(f"Invalid order parameters: {errors}")

    # Place order
    order_id = place_order_with_retry(kite, params)

    if wait_for_fill:
        # Wait for completion
        order_details = wait_for_order_completion(kite, order_id)
        fill_price = order_details.get('average_price', params.price or 0)
        filled_qty = order_details.get('filled_quantity', params.quantity)
        status = order_details.get('status', 'COMPLETE')
    else:
        fill_price = params.price or 0
        filled_qty = params.quantity
        status = 'OPEN'

    # Calculate slippage
    slippage = calculate_slippage(
        params.price or fill_price,
        fill_price,
        params.transaction_type
    )

    return ExecutedOrder(
        order_id=order_id,
        tradingsymbol=params.tradingsymbol,
        transaction_type=params.transaction_type,
        quantity=filled_qty,
        order_price=params.price or fill_price,
        fill_price=fill_price,
        slippage=slippage,
        status=status
    )


def execute_order_with_tiered_slippage(
    kite: SNAILKiteClient,
    tradingsymbol: str,
    transaction_type: str,
    quantity: int,
    base_price: float,
    tier_type: str = "exit_normal",
    tag: str = ""
) -> ExecutedOrder:
    """
    Execute order with progressive tiered slippage.

    TDD Section 1.3: Tiered Slippage Handling

    Progressively widens price until filled:
    - Entry: Exact → +2 ticks (10s intervals)
    - Exit Normal: Exact → +2 → +3 ticks (10s intervals)
    - Exit Urgent: Exact → +3 → +5 ticks → MARKET (8s intervals)
    - Exit Critical: +5 ticks → MARKET (5s intervals)

    Args:
        kite: Kite client instance
        tradingsymbol: Symbol to trade
        transaction_type: "BUY" or "SELL"
        quantity: Order quantity
        base_price: Starting price (bid for SELL, ask for BUY)
        tier_type: One of "entry", "exit_normal", "exit_urgent", "exit_critical"
        tag: Order tag for tracking

    Returns:
        ExecutedOrder with fill details

    Raises:
        OrderExecutionError: If all tiers exhausted without fill
    """
    tier_config = SLIPPAGE_TIERS.get(tier_type, SLIPPAGE_TIERS["exit_normal"])
    tick_tiers = tier_config["ticks"]
    interval = tier_config["interval_seconds"]
    use_market_final = tier_config["final_market"]

    last_order_id = None
    last_error = None

    logger.info(f"Tiered execution for {tradingsymbol}: type={tier_type}, "
                f"tiers={tick_tiers}, market_final={use_market_final}")

    # Try each slippage tier
    for tier_idx, slippage_ticks in enumerate(tick_tiers):
        try:
            # Calculate price with slippage
            order_price = apply_slippage(base_price, transaction_type, slippage_ticks)

            logger.info(f"Tier {tier_idx + 1}/{len(tick_tiers)}: "
                       f"{transaction_type} @ {order_price} (+{slippage_ticks} ticks)")

            # Cancel previous order if exists
            if last_order_id:
                try:
                    kite.cancel_order(last_order_id)
                    logger.debug(f"Cancelled previous order {last_order_id}")
                except Exception as e:
                    logger.warning(f"Failed to cancel order {last_order_id}: {e}")

            # Place new order
            order_id = kite.place_order(
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=quantity,
                price=order_price,
                order_type="LIMIT"
            )
            last_order_id = order_id

            # Wait for fill with timeout
            start_time = time.time()
            while time.time() - start_time < interval:
                order_status = kite.get_order_status(order_id)
                status = order_status.get('status', '')

                if status == 'COMPLETE':
                    fill_price = order_status.get('average_price', order_price)
                    logger.info(f"Order filled at tier {tier_idx + 1}: {fill_price}")

                    return ExecutedOrder(
                        order_id=order_id,
                        tradingsymbol=tradingsymbol,
                        transaction_type=transaction_type,
                        quantity=order_status.get('filled_quantity', quantity),
                        order_price=order_price,
                        fill_price=fill_price,
                        slippage=calculate_slippage(base_price, fill_price, transaction_type),
                        status='COMPLETE'
                    )

                elif status in ('REJECTED', 'CANCELLED'):
                    reason = order_status.get('status_message', 'Unknown')
                    logger.warning(f"Order {status} at tier {tier_idx + 1}: {reason}")
                    last_error = f"Order {status}: {reason}"
                    break

                time.sleep(ORDER_POLL_INTERVAL)

            # Not filled in this tier, continue to next

        except Exception as e:
            logger.warning(f"Tier {tier_idx + 1} error: {e}")
            last_error = str(e)

    # All tiers exhausted - try MARKET if configured
    if use_market_final:
        logger.warning(f"All tiers exhausted for {tradingsymbol}, placing MARKET order")

        try:
            # Cancel any pending order
            if last_order_id:
                try:
                    kite.cancel_order(last_order_id)
                    logger.info(f"Cancelled pending order {last_order_id} before MARKET order")
                except Exception as cancel_err:
                    # Log but continue - the pending order may have already been rejected/cancelled
                    logger.warning(f"Failed to cancel pending order {last_order_id}: {cancel_err}")

            # Place MARKET order
            order_id = kite.place_order(
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=quantity,
                price=None,
                order_type="MARKET"
            )

            # Wait for MARKET fill (should be quick)
            order_status = wait_for_order_completion(kite, order_id, timeout=15)
            fill_price = order_status.get('average_price', base_price)

            logger.info(f"MARKET order filled: {fill_price}")

            return ExecutedOrder(
                order_id=order_id,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=order_status.get('filled_quantity', quantity),
                order_price=0,  # MARKET has no price
                fill_price=fill_price,
                slippage=calculate_slippage(base_price, fill_price, transaction_type),
                status='COMPLETE'
            )

        except Exception as e:
            logger.error(f"MARKET order failed: {e}")
            raise OrderExecutionError(f"All tiers + MARKET failed for {tradingsymbol}: {e}")

    # No MARKET fallback configured
    raise OrderExecutionError(
        f"All {len(tick_tiers)} slippage tiers exhausted for {tradingsymbol}. "
        f"Last error: {last_error}"
    )


# =============================================================================
# IRON FLY ORDER EXECUTION
# =============================================================================

def execute_iron_fly_entry(
    kite: SNAILKiteClient,
    symbols: Dict[str, str],
    quotes: Dict[str, Quote],
    quantity: int,
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS,
    use_tiered_slippage: bool = False
) -> IronFlyOrders:
    """
    Execute Iron Fly entry orders.

    Order sequence:
    1. SELL Straddle CE (ATM CE)
    2. SELL Straddle PE (ATM PE)
    3. BUY Wing CE (OTM CE protection)
    4. BUY Wing PE (OTM PE protection)

    Uses bid prices for sells, ask prices for buys.

    Args:
        kite: Kite client instance
        symbols: Dict of symbols by leg name
        quotes: Dict of quotes by leg name
        quantity: Order quantity (should be lot size multiple)
        slippage_ticks: Slippage buffer in ticks
        use_tiered_slippage: Use progressive tiered slippage (TDD Section 1.3)

    Returns:
        IronFlyOrders with all executed legs

    Raises:
        OrderExecutionError: If any leg fails
    """
    logger.info(f"Executing Iron Fly entry: {quantity} qty, tiered={use_tiered_slippage}")

    # Validate quotes
    is_valid, issues = validate_iron_fly_quotes(quotes)
    if not is_valid:
        raise OrderExecutionError(f"Invalid quotes: {issues}")

    if issues:
        for issue in issues:
            logger.warning(issue)

    executed = {}

    try:
        if use_tiered_slippage:
            # Use progressive tiered slippage (TDD Section 1.3)
            # Entry: Exact → +2 ticks (10s intervals)

            # Order 1: SELL Straddle CE
            logger.info("Placing straddle CE sell order (tiered)...")
            executed['straddle_ce'] = execute_order_with_tiered_slippage(
                kite=kite,
                tradingsymbol=symbols['straddle_ce'],
                transaction_type="SELL",
                quantity=quantity,
                base_price=quotes['straddle_ce'].bid,
                tier_type="entry",
                tag="SNAIL_STRADDLE_CE"
            )

            # Order 2: SELL Straddle PE
            logger.info("Placing straddle PE sell order (tiered)...")
            executed['straddle_pe'] = execute_order_with_tiered_slippage(
                kite=kite,
                tradingsymbol=symbols['straddle_pe'],
                transaction_type="SELL",
                quantity=quantity,
                base_price=quotes['straddle_pe'].bid,
                tier_type="entry",
                tag="SNAIL_STRADDLE_PE"
            )

            # Order 3: BUY Wing CE
            logger.info("Placing wing CE buy order (tiered)...")
            executed['wing_ce'] = execute_order_with_tiered_slippage(
                kite=kite,
                tradingsymbol=symbols['wing_ce'],
                transaction_type="BUY",
                quantity=quantity,
                base_price=quotes['wing_ce'].ask,
                tier_type="entry",
                tag="SNAIL_WING_CE"
            )

            # Order 4: BUY Wing PE
            logger.info("Placing wing PE buy order (tiered)...")
            executed['wing_pe'] = execute_order_with_tiered_slippage(
                kite=kite,
                tradingsymbol=symbols['wing_pe'],
                transaction_type="BUY",
                quantity=quantity,
                base_price=quotes['wing_pe'].ask,
                tier_type="entry",
                tag="SNAIL_WING_PE"
            )
        else:
            # Standard fixed slippage approach

            # Order 1: SELL Straddle CE
            logger.info("Placing straddle CE sell order...")
            ce_price = apply_slippage(quotes['straddle_ce'].bid, "SELL", slippage_ticks)
            executed['straddle_ce'] = execute_order(
                kite,
                OrderParams(
                    tradingsymbol=symbols['straddle_ce'],
                    transaction_type="SELL",
                    quantity=quantity,
                    price=ce_price,
                    tag="SNAIL_STRADDLE_CE"
                )
            )

            # Order 2: SELL Straddle PE
            logger.info("Placing straddle PE sell order...")
            pe_price = apply_slippage(quotes['straddle_pe'].bid, "SELL", slippage_ticks)
            executed['straddle_pe'] = execute_order(
                kite,
                OrderParams(
                    tradingsymbol=symbols['straddle_pe'],
                    transaction_type="SELL",
                    quantity=quantity,
                    price=pe_price,
                    tag="SNAIL_STRADDLE_PE"
                )
            )

            # Order 3: BUY Wing CE
            logger.info("Placing wing CE buy order...")
            wing_ce_price = apply_slippage(quotes['wing_ce'].ask, "BUY", slippage_ticks)
            executed['wing_ce'] = execute_order(
                kite,
                OrderParams(
                    tradingsymbol=symbols['wing_ce'],
                    transaction_type="BUY",
                    quantity=quantity,
                    price=wing_ce_price,
                    tag="SNAIL_WING_CE"
                )
            )

            # Order 4: BUY Wing PE
            logger.info("Placing wing PE buy order...")
            wing_pe_price = apply_slippage(quotes['wing_pe'].ask, "BUY", slippage_ticks)
            executed['wing_pe'] = execute_order(
                kite,
                OrderParams(
                    tradingsymbol=symbols['wing_pe'],
                    transaction_type="BUY",
                    quantity=quantity,
                    price=wing_pe_price,
                    tag="SNAIL_WING_PE"
                )
            )

        result = IronFlyOrders(**executed)

        logger.info(f"Iron Fly entry complete. Net credit: {result.net_credit:.2f}, "
                   f"Total slippage: {result.total_slippage:.2f}")

        return result

    except Exception as e:
        # Log what was executed before failure
        if executed:
            logger.error(f"Partial execution - completed legs: {list(executed.keys())}")
        raise OrderExecutionError(f"Iron Fly entry failed: {e}")


def execute_iron_fly_exit(
    kite: SNAILKiteClient,
    symbols: Dict[str, str],
    quotes: Dict[str, Quote],
    quantity: int,
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS
) -> IronFlyOrders:
    """
    Execute Iron Fly exit orders (legacy interface - wraps safe version).

    CRITICAL: Exit sequence must close SHORT positions (straddle) FIRST
    to release margin before closing long wings. Closing wings first would
    leave naked short straddle causing massive margin spike.

    Exit sequence:
    1. BUY Straddle CE (close short) - Release margin first
    2. BUY Straddle PE (close short) - Release margin
    3. SELL Wing CE (close long) - Then close protection
    4. SELL Wing PE (close long) - Finally close protection

    Args:
        kite: Kite client instance
        symbols: Dict of symbols by leg name
        quotes: Dict of quotes by leg name
        quantity: Order quantity
        slippage_ticks: Slippage buffer

    Returns:
        IronFlyOrders with exit details

    Raises:
        OrderExecutionError: If exit fails (includes partial exit info)
    """
    result = execute_iron_fly_exit_safe(kite, symbols, quotes, quantity, slippage_ticks)

    if result.success:
        return result.orders

    # Partial or full failure
    error_msg = result.get_status_summary()
    if result.legs_failed:
        error_msg += f" Errors: {result.legs_failed}"
    raise OrderExecutionError(error_msg)


def execute_iron_fly_exit_safe(
    kite: SNAILKiteClient,
    symbols: Dict[str, str],
    quotes: Dict[str, Quote],
    quantity: int,
    slippage_ticks: int = DEFAULT_SLIPPAGE_TICKS,
    max_retries: int = 3,
    use_market_fallback: bool = True
) -> ExitExecutionResult:
    """
    Execute Iron Fly exit orders with robust partial failure handling and retry logic.

    Unlike execute_iron_fly_exit, this function:
    - Attempts ALL legs even if some fail
    - RETRIES failed legs with increasing slippage
    - Uses MARKET orders as final fallback (configurable)
    - Returns detailed status of each leg
    - Never raises exceptions for order failures
    - Enables proper recovery from partial exits

    CRITICAL: Exit sequence must close SHORT positions (straddle) FIRST.

    Args:
        kite: Kite client instance
        symbols: Dict of symbols by leg name
        quotes: Dict of quotes by leg name
        quantity: Order quantity
        slippage_ticks: Initial slippage buffer
        max_retries: Maximum retry attempts per leg (default 3)
        use_market_fallback: Use MARKET orders if all retries fail (default True)

    Returns:
        ExitExecutionResult with detailed leg status
    """
    logger.info(f"Executing Iron Fly exit (safe mode with retry): {quantity} qty")

    executed: Dict[str, ExecutedOrder] = {}
    legs_closed: List[str] = []
    legs_failed: Dict[str, str] = {}
    total_debit = 0.0
    total_credit = 0.0

    # Define exit order - STRADDLE FIRST (release margin), THEN WINGS
    exit_sequence = [
        ('straddle_ce', 'BUY', 'ask', 'SNAIL_EXIT_STRADDLE_CE', 'debit'),
        ('straddle_pe', 'BUY', 'ask', 'SNAIL_EXIT_STRADDLE_PE', 'debit'),
        ('wing_ce', 'SELL', 'bid', 'SNAIL_EXIT_WING_CE', 'credit'),
        ('wing_pe', 'SELL', 'bid', 'SNAIL_EXIT_WING_PE', 'credit'),
    ]

    # Slippage progression: initial, +3, +5, then MARKET
    slippage_progression = [slippage_ticks, slippage_ticks + 3, slippage_ticks + 5]

    for leg_name, txn_type, price_field, tag, cost_type in exit_sequence:
        leg_success = False
        last_error = ""

        # Get base price
        if price_field == 'ask':
            base_price = quotes[leg_name].ask
        else:
            base_price = quotes[leg_name].bid

        # Garbage-book guard (2026-07-24): this is a MUST-EXIT, so we never
        # BLOCK — but if the leg's top-of-book is unreliable we BOUND the LIMIT
        # price against an EXTERNAL anchor (fresh LTP / prev close) so we cannot
        # lift a phantom ask / hit a phantom bid. The MARKET fallback below
        # remains the uncapped last resort.
        leg_ok, leg_why = quote_reliable(quotes[leg_name])
        leg_anchor = 0.0 if leg_ok else price_anchor(quotes[leg_name])
        if not leg_ok:
            logger.warning(
                "  {0}: UNRELIABLE book ({1}) - bounding exit LIMIT vs anchor "
                "{2:.2f} (MARKET fallback stays uncapped)".format(leg_name, leg_why, leg_anchor)
            )

        # Try with increasing slippage
        for retry_idx, current_slippage in enumerate(slippage_progression[:max_retries]):
            try:
                logger.info(f"Closing {leg_name} ({txn_type}), attempt {retry_idx + 1}, slippage={current_slippage}...")

                price = apply_slippage(base_price, txn_type, current_slippage)
                if not leg_ok and leg_anchor > 0:
                    # Anchor never validates a price against itself; it caps buys
                    # and floors sells at a loose multiple of the external anchor.
                    capped = cap_buy_price(price, leg_anchor) if txn_type == 'BUY' else floor_sell_price(price, leg_anchor)
                    price = round_to_tick(max(capped, TICK_SIZE))

                order = execute_order(
                    kite,
                    OrderParams(
                        tradingsymbol=symbols[leg_name],
                        transaction_type=txn_type,
                        quantity=quantity,
                        price=price,
                        tag=tag
                    )
                )

                executed[leg_name] = order
                legs_closed.append(leg_name)
                leg_success = True

                # Track costs
                if cost_type == 'debit':
                    total_debit += order.fill_price
                else:
                    total_credit += order.fill_price

                logger.info(f"  {leg_name}: Closed @ {order.fill_price:.2f} (attempt {retry_idx + 1})")
                break  # Success, move to next leg

            except Exception as e:
                last_error = str(e)
                logger.warning(f"  {leg_name}: Attempt {retry_idx + 1} failed - {last_error}")
                time.sleep(1)  # Brief pause before retry

        # If all LIMIT attempts failed, try MARKET order as fallback
        if not leg_success and use_market_fallback:
            try:
                logger.warning(f"  {leg_name}: All LIMIT attempts failed, trying MARKET order...")

                order_id = kite.place_order(
                    tradingsymbol=symbols[leg_name],
                    transaction_type=txn_type,
                    quantity=quantity,
                    price=None,
                    order_type="MARKET"
                )

                # Wait for MARKET order completion
                order_details = wait_for_order_completion(kite, order_id, timeout=15)
                fill_price = order_details.get('average_price', base_price)

                market_order = ExecutedOrder(
                    order_id=order_id,
                    tradingsymbol=symbols[leg_name],
                    transaction_type=txn_type,
                    quantity=quantity,
                    order_price=0,  # MARKET
                    fill_price=fill_price,
                    slippage=calculate_slippage(base_price, fill_price, txn_type),
                    status='COMPLETE'
                )

                executed[leg_name] = market_order
                legs_closed.append(leg_name)
                leg_success = True

                if cost_type == 'debit':
                    total_debit += fill_price
                else:
                    total_credit += fill_price

                logger.info(f"  {leg_name}: MARKET order filled @ {fill_price:.2f}")

            except Exception as e:
                last_error = f"MARKET fallback failed: {str(e)}"
                logger.error(f"  {leg_name}: {last_error}")

        # Record failure if all attempts failed
        if not leg_success:
            legs_failed[leg_name] = last_error
            logger.error(f"  {leg_name}: ALL ATTEMPTS FAILED - {last_error}")

    # Determine overall status
    all_closed = len(legs_closed) == 4
    some_closed = len(legs_closed) > 0

    if all_closed:
        # Complete success
        orders = IronFlyOrders(**executed)
        logger.info(f"Iron Fly exit complete. Debit: {total_debit:.2f}, Credit: {total_credit:.2f}")
        return ExitExecutionResult(
            success=True,
            partial=False,
            orders=orders,
            legs_closed=legs_closed,
            total_debit=total_debit,
            total_credit=total_credit
        )
    elif some_closed:
        # Partial exit - CRITICAL situation requiring manual intervention
        logger.critical(f"PARTIAL EXIT! Closed: {legs_closed}, Failed: {list(legs_failed.keys())}")
        return ExitExecutionResult(
            success=False,
            partial=True,
            orders=None,
            legs_closed=legs_closed,
            legs_failed=legs_failed,
            total_debit=total_debit,
            total_credit=total_credit,
            error="Partial exit - manual intervention required"
        )
    else:
        # Complete failure
        logger.error(f"Exit FAILED - no legs closed. Errors: {legs_failed}")
        return ExitExecutionResult(
            success=False,
            partial=False,
            legs_failed=legs_failed,
            error="Exit failed - all legs failed"
        )


# =============================================================================
# POSITION VERIFICATION
# =============================================================================

def verify_position(
    kite: SNAILKiteClient,
    tradingsymbol: str,
    expected_qty: int,
    expected_side: str
) -> bool:
    """
    Verify that a position exists with expected quantity.

    Args:
        kite: Kite client instance
        tradingsymbol: Symbol to verify
        expected_qty: Expected quantity (negative for short)
        expected_side: "LONG" or "SHORT"

    Returns:
        True if position matches expectation
    """
    try:
        positions = kite.positions()
        net_positions = positions.get('net', [])

        for pos in net_positions:
            if pos.get('tradingsymbol') == tradingsymbol:
                qty = pos.get('quantity', 0)

                if expected_side == "SHORT" and qty < 0 and abs(qty) == expected_qty:
                    return True
                elif expected_side == "LONG" and qty > 0 and qty == expected_qty:
                    return True

        return False

    except Exception as e:
        logger.error(f"Position verification failed: {e}")
        return False


def verify_iron_fly_positions(
    kite: SNAILKiteClient,
    symbols: Dict[str, str],
    quantity: int
) -> Tuple[bool, List[str]]:
    """
    Verify all Iron Fly positions are in place.

    Args:
        kite: Kite client instance
        symbols: Dict of symbols by leg name
        quantity: Expected quantity per leg

    Returns:
        (all_verified, list of issues)
    """
    issues = []

    # Verify short legs (straddle)
    if not verify_position(kite, symbols['straddle_ce'], quantity, "SHORT"):
        issues.append(f"Straddle CE position mismatch: {symbols['straddle_ce']}")

    if not verify_position(kite, symbols['straddle_pe'], quantity, "SHORT"):
        issues.append(f"Straddle PE position mismatch: {symbols['straddle_pe']}")

    # Verify long legs (wings)
    if not verify_position(kite, symbols['wing_ce'], quantity, "LONG"):
        issues.append(f"Wing CE position mismatch: {symbols['wing_ce']}")

    if not verify_position(kite, symbols['wing_pe'], quantity, "LONG"):
        issues.append(f"Wing PE position mismatch: {symbols['wing_pe']}")

    return len(issues) == 0, issues


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == '__main__':

    print("\n" + "=" * 60)
    print("SNAIL Order Helpers Test")
    print("=" * 60)

    # Test price utilities
    print("\n[1] Price rounding tests:")
    test_prices = [123.47, 123.48, 123.475, 100.02, 99.99]
    for price in test_prices:
        rounded = round_to_tick(price)
        print(f"    {price} -> {rounded}")

    # Test slippage application
    print("\n[2] Slippage application tests:")
    base_price = 100.0
    print(f"    Base price: {base_price}")
    print(f"    SELL with 2 ticks: {apply_slippage(base_price, 'SELL', 2)}")
    print(f"    BUY with 2 ticks: {apply_slippage(base_price, 'BUY', 2)}")

    # Test slippage calculation
    print("\n[3] Slippage calculation tests:")
    print(f"    SELL @ 100, filled @ 99.90: {calculate_slippage(100, 99.90, 'SELL')} (unfavorable)")
    print(f"    SELL @ 100, filled @ 100.10: {calculate_slippage(100, 100.10, 'SELL')} (favorable)")
    print(f"    BUY @ 100, filled @ 100.10: {calculate_slippage(100, 100.10, 'BUY')} (unfavorable)")
    print(f"    BUY @ 100, filled @ 99.90: {calculate_slippage(100, 99.90, 'BUY')} (favorable)")

    # Test order validation
    print("\n[4] Order validation tests:")
    valid_params = OrderParams(
        tradingsymbol="NIFTY2511424000CE",
        transaction_type="SELL",
        quantity=NIFTY_LOT_SIZE,
        price=150.0
    )
    is_valid, errors = validate_order_params(valid_params)
    print(f"    Valid order: {is_valid}, errors: {errors}")

    invalid_params = OrderParams(
        tradingsymbol="NIFTY2511424000CE",
        transaction_type="SELL",
        quantity=50,  # Not multiple of NIFTY_LOT_SIZE
        price=150.0
    )
    is_valid, errors = validate_order_params(invalid_params)
    print(f"    Invalid quantity: {is_valid}, errors: {errors}")

    print("\n" + "=" * 60)
    print("Order helpers test complete!")
    print("=" * 60 + "\n")

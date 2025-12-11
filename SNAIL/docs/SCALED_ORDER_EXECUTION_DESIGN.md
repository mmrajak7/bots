# Scaled Order Execution Technical Design

## Document Info
- **Version**: 1.0.0
- **Created**: 2025-12-11
- **Author**: SNAIL Development Team
- **Status**: DRAFT - Pending Review

---

## 1. Overview

### 1.1 Problem Statement

The current SNAIL system executes Iron Fly orders one leg at a time for 1 lot (75 contracts). This approach has critical limitations when scaling to larger positions (up to 100 lots = 7,500 contracts per leg):

1. **Freeze Limit Constraints**: NSE limits single orders to 1,800 contracts for NIFTY
2. **Position Integrity Risk**: Sequential leg execution at scale can leave unbalanced positions
3. **Inadequate Slippage**: Fixed 2-tick slippage doesn't account for market depth depletion
4. **No Depth Analysis**: System uses only top-of-book, ignoring market depth

### 1.2 Design Decisions (Frozen from Brainstorm)

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Partial success | Accept | 72-lot balanced Iron Fly is valid |
| Rebalance failure | Trim other legs | Position integrity > target size |
| Inter-batch timing | 3-5 seconds | Balance speed and safety |
| Low depth scenario | Proceed + extra slippage | Hidden liquidity exists |
| ATM order type | MARKET | Guaranteed fills for shorts |
| OTM order type | LIMIT with retry→MARKET | Price control on wings |
| Freeze limits | Config file | Manual update on NSE changes |

### 1.3 Scope

**In Scope:**
- Batch execution engine for scaled orders
- Depth-aware slippage calculation
- Position integrity maintenance (rebalancing)
- Config-driven freeze limits
- Entry flow integration

**Out of Scope:**
- Exit flow (separate enhancement)
- Real-time freeze limit fetching from NSE
- TWAP/VWAP execution algorithms

---

## 2. Architecture

### 2.1 Component Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                        EntryManager                              │
│  (Existing - orchestrates entry flow)                           │
└───────────────────────────┬─────────────────────────────────────┘
                            │ calls
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ScaledOrderExecutor                          │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────────┐ │
│  │ BatchPlanner│  │ BatchExecutor│  │ PositionRebalancer      │ │
│  │             │  │              │  │                         │ │
│  │ - Split qty │  │ - Execute    │  │ - Detect imbalance      │ │
│  │   into      │  │   4 legs     │  │ - Trim to restore       │ │
│  │   batches   │  │   parallel   │  │   balance               │ │
│  │ - Respect   │  │ - Wait for   │  │                         │ │
│  │   freeze    │  │   fills      │  │                         │ │
│  │   limits    │  │ - Track      │  │                         │ │
│  │             │  │   state      │  │                         │ │
│  └─────────────┘  └──────────────┘  └─────────────────────────┘ │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    DepthAnalyzer                             ││
│  │  - Fetch 5-level depth                                       ││
│  │  - Calculate expected fill price                             ││
│  │  - Determine slippage buffer                                 ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      SNAILKiteClient                             │
│  (Existing - API wrapper)                                        │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Data Flow

```
1. EntryManager calls ScaledOrderExecutor.execute_entry()
2. BatchPlanner splits total quantity into batches
3. For each batch:
   a. DepthAnalyzer fetches depth, calculates prices
   b. BatchExecutor places 4 orders (parallel)
   c. BatchExecutor waits for fills
   d. If imbalance: PositionRebalancer restores balance
   e. Inter-batch delay (3-5 sec)
4. Return aggregated results
```

---

## 3. Data Models

### 3.1 Configuration Schema

```yaml
# config/config.yaml additions
scaling:
  # NSE Freeze Limits - UPDATE WHEN NSE ANNOUNCES CHANGES
  # Last updated: 2025-12-11
  freeze_limits:
    NIFTY: 1800       # 24 lots max per order
    BANKNIFTY: 600    # 40 lots max per order
    FINNIFTY: 1200    # 48 lots max per order
    MIDCPNIFTY: 2800  # 56 lots max per order
    NIFTYNXT50: 600   # 10 lots max per order

  # Execution parameters
  execution:
    inter_batch_delay_ms: 3000    # 3 seconds between batches
    order_timeout_seconds: 30     # Max wait per order
    max_retries_per_order: 2      # Retry failed orders

  # Order types by leg
  order_types:
    atm_straddle: "MARKET"        # Guaranteed fill for shorts
    otm_wings: "LIMIT"            # Price control for longs

  # Slippage configuration
  slippage:
    base_ticks: 3                 # Starting slippage for wings
    depth_buffer_ticks: 2         # Extra when depth < 50%
    max_ticks: 15                 # Cap slippage
    wing_retry_increment: 2       # Add per retry

  # Rebalancing
  rebalance:
    max_attempts: 2               # Max rebalance tries
    strategy: "trim_to_balance"   # trim_to_balance | abort
```

### 3.2 Core Data Classes

```python
# src/utils/scaled_execution.py

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any


class BatchStatus(Enum):
    """Status of a batch execution."""
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"           # Some legs filled
    REBALANCED = "REBALANCED"     # Imbalance corrected
    FAILED = "FAILED"


class LegStatus(Enum):
    """Status of a single leg order."""
    PENDING = "PENDING"
    PLACED = "PLACED"
    FILLED = "FILLED"
    PARTIAL_FILL = "PARTIAL_FILL"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class DepthLevel:
    """Single level of market depth."""
    price: float
    quantity: int
    orders: int = 0


@dataclass
class MarketDepth:
    """Full market depth for an instrument."""
    tradingsymbol: str
    bid_levels: List[DepthLevel]    # Buy side (for sells)
    ask_levels: List[DepthLevel]    # Sell side (for buys)
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
    """Represents a single leg order within a batch."""
    leg_type: str                   # straddle_ce, straddle_pe, wing_ce, wing_pe
    tradingsymbol: str
    transaction_type: str           # BUY or SELL
    quantity: int
    order_type: str                 # MARKET or LIMIT
    price: Optional[float]          # None for MARKET
    status: LegStatus = LegStatus.PENDING
    order_id: Optional[str] = None
    fill_price: Optional[float] = None
    filled_qty: int = 0
    error_message: str = ""
    placed_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None


@dataclass
class Batch:
    """Represents a batch of 4 leg orders."""
    batch_number: int
    quantity_per_leg: int
    legs: Dict[str, LegOrder]       # leg_type -> LegOrder
    status: BatchStatus = BatchStatus.PENDING
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    rebalance_orders: List[LegOrder] = field(default_factory=list)

    @property
    def is_balanced(self) -> bool:
        """Check if all legs have equal filled quantity."""
        filled_qtys = [leg.filled_qty for leg in self.legs.values()]
        return len(set(filled_qtys)) == 1 and filled_qtys[0] > 0

    @property
    def filled_quantity(self) -> int:
        """Minimum filled quantity across all legs (balanced amount)."""
        return min(leg.filled_qty for leg in self.legs.values())

    def get_imbalance(self) -> Dict[str, int]:
        """Get imbalance per leg (positive = excess, negative = deficit)."""
        min_filled = self.filled_quantity
        return {
            leg_type: leg.filled_qty - min_filled
            for leg_type, leg in self.legs.items()
        }


@dataclass
class ExecutionPlan:
    """Complete execution plan for scaled order."""
    total_quantity: int
    freeze_limit: int
    num_batches: int
    batches: List[Batch]
    symbols: Dict[str, str]         # leg_type -> tradingsymbol
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class ExecutionResult:
    """Result of scaled order execution."""
    success: bool
    total_filled_qty: int           # Balanced quantity achieved
    target_qty: int                 # Original target
    batches_completed: int
    batches_total: int
    total_slippage: float
    execution_time_seconds: float
    leg_summaries: Dict[str, Dict[str, Any]]  # Per-leg statistics
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
```

---

## 4. Component Specifications

### 4.1 BatchPlanner

**Responsibility**: Split total quantity into batches respecting freeze limits.

```python
class BatchPlanner:
    """Plans batch execution for scaled orders."""

    def __init__(self, config: Dict[str, Any]):
        self.freeze_limits = config.get('scaling', {}).get('freeze_limits', {})
        self.default_freeze_limit = 1800  # NIFTY default

    def get_freeze_limit(self, instrument: str = "NIFTY") -> int:
        """Get freeze limit for instrument."""
        return self.freeze_limits.get(instrument, self.default_freeze_limit)

    def create_plan(
        self,
        total_quantity: int,
        symbols: Dict[str, str],
        instrument: str = "NIFTY"
    ) -> ExecutionPlan:
        """
        Create execution plan with batches.

        Args:
            total_quantity: Total contracts per leg
            symbols: Mapping of leg_type to tradingsymbol
            instrument: Index name for freeze limit lookup

        Returns:
            ExecutionPlan with batches
        """
        freeze_limit = self.get_freeze_limit(instrument)

        # Calculate number of batches
        num_batches = math.ceil(total_quantity / freeze_limit)

        batches = []
        remaining = total_quantity

        for i in range(num_batches):
            # Batch size is min of freeze_limit and remaining
            batch_qty = min(freeze_limit, remaining)

            # Create leg orders for this batch
            legs = {}
            for leg_type, symbol in symbols.items():
                # Determine order type and transaction type
                if leg_type.startswith('straddle'):
                    order_type = "MARKET"
                    transaction_type = "SELL"
                else:  # wing
                    order_type = "LIMIT"
                    transaction_type = "BUY"

                legs[leg_type] = LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type=transaction_type,
                    quantity=batch_qty,
                    order_type=order_type,
                    price=None  # Set by DepthAnalyzer
                )

            batches.append(Batch(
                batch_number=i + 1,
                quantity_per_leg=batch_qty,
                legs=legs
            ))

            remaining -= batch_qty

        return ExecutionPlan(
            total_quantity=total_quantity,
            freeze_limit=freeze_limit,
            num_batches=num_batches,
            batches=batches,
            symbols=symbols
        )
```

### 4.2 DepthAnalyzer

**Responsibility**: Analyze market depth and calculate optimal prices.

```python
class DepthAnalyzer:
    """Analyzes market depth for pricing decisions."""

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]):
        self.kite = kite
        slippage_config = config.get('scaling', {}).get('slippage', {})
        self.base_ticks = slippage_config.get('base_ticks', 3)
        self.depth_buffer_ticks = slippage_config.get('depth_buffer_ticks', 2)
        self.max_ticks = slippage_config.get('max_ticks', 15)
        self.tick_size = 0.05

    def fetch_depth(self, instruments: List[str]) -> Dict[str, MarketDepth]:
        """
        Fetch full market depth for instruments.

        Args:
            instruments: List of "NFO:SYMBOL" strings

        Returns:
            Dict mapping instrument to MarketDepth
        """
        raw_quotes = self.kite.kite.quote(instruments)

        result = {}
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

            result[inst] = MarketDepth(
                tradingsymbol=inst.split(':')[1] if ':' in inst else inst,
                bid_levels=bid_levels,
                ask_levels=ask_levels,
                ltp=data.get('last_price', 0)
            )

        return result

    def calculate_expected_fill_price(
        self,
        depth: MarketDepth,
        quantity: int,
        side: str  # "BUY" or "SELL"
    ) -> Tuple[float, int]:
        """
        Calculate expected fill price across depth levels.

        Args:
            depth: Market depth data
            quantity: Order quantity
            side: BUY or SELL

        Returns:
            (weighted_avg_price, unfillable_quantity)
        """
        levels = depth.ask_levels if side == "BUY" else depth.bid_levels

        if not levels:
            return 0.0, quantity

        remaining = quantity
        total_value = 0.0

        for level in levels:
            fill_at_level = min(remaining, level.quantity)
            total_value += fill_at_level * level.price
            remaining -= fill_at_level

            if remaining <= 0:
                break

        filled = quantity - remaining
        if filled == 0:
            return 0.0, quantity

        return total_value / filled, remaining

    def calculate_slippage_ticks(
        self,
        depth: MarketDepth,
        quantity: int,
        side: str,
        batch_number: int = 1
    ) -> int:
        """
        Calculate appropriate slippage ticks based on depth.

        Args:
            depth: Market depth data
            quantity: Order quantity
            side: BUY or SELL
            batch_number: Current batch (for progressive slippage)

        Returns:
            Number of slippage ticks to apply
        """
        levels = depth.ask_levels if side == "BUY" else depth.bid_levels
        total_depth = sum(l.quantity for l in levels)

        # Start with base slippage
        ticks = self.base_ticks

        # Add buffer if depth is thin
        if total_depth < quantity * 0.5:
            ticks += self.depth_buffer_ticks

        # Progressive slippage for later batches
        ticks += (batch_number - 1)

        # Cap at maximum
        return min(ticks, self.max_ticks)

    def set_leg_prices(
        self,
        batch: Batch,
        depth_data: Dict[str, MarketDepth]
    ) -> None:
        """
        Set prices for all legs in batch based on depth analysis.

        Args:
            batch: Batch to update
            depth_data: Depth data keyed by tradingsymbol
        """
        for leg_type, leg in batch.legs.items():
            # MARKET orders don't need price
            if leg.order_type == "MARKET":
                continue

            # Find depth for this leg
            depth = None
            for key, d in depth_data.items():
                if leg.tradingsymbol in key or d.tradingsymbol == leg.tradingsymbol:
                    depth = d
                    break

            if not depth:
                # Fallback: use high slippage
                logger.warning(f"No depth data for {leg.tradingsymbol}, using max slippage")
                # Will be set by BatchExecutor using quote
                continue

            # Calculate slippage
            ticks = self.calculate_slippage_ticks(
                depth,
                leg.quantity,
                leg.transaction_type,
                batch.batch_number
            )

            # Calculate price with slippage
            if leg.transaction_type == "BUY":
                base_price = depth.best_ask
                leg.price = round_to_tick(base_price + ticks * self.tick_size)
            else:
                base_price = depth.best_bid
                leg.price = round_to_tick(base_price - ticks * self.tick_size)
```

### 4.3 BatchExecutor

**Responsibility**: Execute a single batch, managing order lifecycle.

```python
class BatchExecutor:
    """Executes a batch of 4 leg orders."""

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]):
        self.kite = kite
        exec_config = config.get('scaling', {}).get('execution', {})
        self.order_timeout = exec_config.get('order_timeout_seconds', 30)
        self.max_retries = exec_config.get('max_retries_per_order', 2)
        slippage_config = config.get('scaling', {}).get('slippage', {})
        self.wing_retry_increment = slippage_config.get('wing_retry_increment', 2)
        self.tick_size = 0.05

    def execute_batch(self, batch: Batch) -> Batch:
        """
        Execute all legs in a batch.

        Strategy:
        1. Place all 4 orders in parallel
        2. Wait for fills with timeout
        3. Handle partial fills with retry/rebalance

        Args:
            batch: Batch to execute

        Returns:
            Updated batch with execution results
        """
        batch.status = BatchStatus.IN_PROGRESS
        batch.started_at = datetime.now()

        logger.info(f"Executing batch {batch.batch_number}: {batch.quantity_per_leg} contracts per leg")

        # Step 1: Place all orders
        self._place_all_orders(batch)

        # Step 2: Wait for fills
        self._wait_for_fills(batch)

        # Step 3: Check balance and determine status
        batch.completed_at = datetime.now()

        if batch.is_balanced:
            batch.status = BatchStatus.COMPLETED
            logger.info(f"Batch {batch.batch_number} completed: {batch.filled_quantity} per leg")
        else:
            batch.status = BatchStatus.PARTIAL
            logger.warning(f"Batch {batch.batch_number} partial: imbalance={batch.get_imbalance()}")

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
        pending_legs = [leg for leg in batch.legs.values()
                       if leg.status == LegStatus.PLACED]

        while pending_legs and (time.time() - start_time) < self.order_timeout:
            for leg in pending_legs[:]:  # Copy to allow modification
                try:
                    status = self.kite.get_order_status(leg.order_id)
                    order_status = status.get('status', '')

                    if order_status == 'COMPLETE':
                        leg.status = LegStatus.FILLED
                        leg.fill_price = status.get('average_price', leg.price)
                        leg.filled_qty = status.get('filled_quantity', leg.quantity)
                        leg.filled_at = datetime.now()
                        pending_legs.remove(leg)
                        logger.debug(f"{leg.leg_type} filled: {leg.fill_price}")

                    elif order_status in ('REJECTED', 'CANCELLED'):
                        leg.status = LegStatus.REJECTED
                        leg.error_message = status.get('status_message', order_status)
                        pending_legs.remove(leg)
                        logger.warning(f"{leg.leg_type} {order_status}: {leg.error_message}")

                except Exception as e:
                    logger.warning(f"Error checking {leg.leg_type}: {e}")

            if pending_legs:
                time.sleep(0.5)

        # Handle timeout - retry LIMIT orders as MARKET
        for leg in pending_legs:
            if leg.order_type == "LIMIT":
                logger.warning(f"{leg.leg_type} timeout, converting to MARKET")
                self._convert_to_market(leg)

    def _convert_to_market(self, leg: LegOrder) -> None:
        """Convert unfilled LIMIT order to MARKET."""
        try:
            # Cancel existing order
            self.kite.cancel_order(leg.order_id)

            # Place MARKET order
            order_id = self.kite.place_order(
                tradingsymbol=leg.tradingsymbol,
                transaction_type=leg.transaction_type,
                quantity=leg.quantity - leg.filled_qty,  # Only unfilled portion
                price=None,
                order_type="MARKET"
            )

            leg.order_id = order_id
            leg.order_type = "MARKET"

            # Wait briefly for MARKET fill
            time.sleep(2)
            status = self.kite.get_order_status(order_id)

            if status.get('status') == 'COMPLETE':
                leg.status = LegStatus.FILLED
                leg.fill_price = status.get('average_price', 0)
                leg.filled_qty = leg.quantity
                leg.filled_at = datetime.now()
            else:
                leg.status = LegStatus.REJECTED
                leg.error_message = f"MARKET order failed: {status.get('status_message', 'Unknown')}"

        except Exception as e:
            leg.status = LegStatus.REJECTED
            leg.error_message = f"Market conversion failed: {e}"
            logger.error(f"Market conversion failed for {leg.leg_type}: {e}")
```

### 4.4 PositionRebalancer

**Responsibility**: Restore balance when batches have unequal fills.

```python
class PositionRebalancer:
    """Restores position balance by trimming excess legs."""

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]):
        self.kite = kite
        rebalance_config = config.get('scaling', {}).get('rebalance', {})
        self.max_attempts = rebalance_config.get('max_attempts', 2)
        self.strategy = rebalance_config.get('strategy', 'trim_to_balance')

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
        logger.info(f"Rebalancing batch {batch.batch_number}: {imbalance}")

        # Find legs with excess (positive imbalance)
        for leg_type, excess in imbalance.items():
            if excess <= 0:
                continue

            leg = batch.legs[leg_type]

            # Create reverse order to trim excess
            reverse_txn = "BUY" if leg.transaction_type == "SELL" else "SELL"

            for attempt in range(self.max_attempts):
                try:
                    # Place trimming order at MARKET
                    order_id = self.kite.place_order(
                        tradingsymbol=leg.tradingsymbol,
                        transaction_type=reverse_txn,
                        quantity=excess,
                        price=None,
                        order_type="MARKET"
                    )

                    # Wait for fill
                    time.sleep(2)
                    status = self.kite.get_order_status(order_id)

                    if status.get('status') == 'COMPLETE':
                        leg.filled_qty -= excess

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
                            fill_price=status.get('average_price', 0),
                            filled_qty=excess
                        )
                        batch.rebalance_orders.append(rebalance_order)

                        logger.info(f"Trimmed {excess} from {leg_type}")
                        break

                except Exception as e:
                    logger.error(f"Rebalance attempt {attempt + 1} failed for {leg_type}: {e}")
                    if attempt == self.max_attempts - 1:
                        return False

        # Verify balance restored
        if batch.is_balanced:
            batch.status = BatchStatus.REBALANCED
            return True

        return False
```

### 4.5 ScaledOrderExecutor (Main Orchestrator)

**Responsibility**: Orchestrate the complete scaled execution flow.

```python
class ScaledOrderExecutor:
    """
    Main orchestrator for scaled order execution.

    Coordinates BatchPlanner, DepthAnalyzer, BatchExecutor, and
    PositionRebalancer to execute large Iron Fly orders in batches.
    """

    def __init__(self, kite: SNAILKiteClient, config: Dict[str, Any]):
        self.kite = kite
        self.config = config

        self.planner = BatchPlanner(config)
        self.depth_analyzer = DepthAnalyzer(kite, config)
        self.batch_executor = BatchExecutor(kite, config)
        self.rebalancer = PositionRebalancer(kite, config)

        exec_config = config.get('scaling', {}).get('execution', {})
        self.inter_batch_delay = exec_config.get('inter_batch_delay_ms', 3000) / 1000

    def execute_entry(
        self,
        symbols: Dict[str, str],
        total_quantity: int,
        instrument: str = "NIFTY"
    ) -> ExecutionResult:
        """
        Execute scaled Iron Fly entry.

        Args:
            symbols: Dict mapping leg_type to tradingsymbol
            total_quantity: Total contracts per leg
            instrument: Index name for freeze limit

        Returns:
            ExecutionResult with complete execution details
        """
        start_time = time.time()
        errors = []
        warnings = []

        logger.info(f"Starting scaled entry: {total_quantity} contracts, "
                   f"instrument={instrument}")

        # Step 1: Create execution plan
        plan = self.planner.create_plan(total_quantity, symbols, instrument)
        logger.info(f"Execution plan: {plan.num_batches} batches, "
                   f"freeze_limit={plan.freeze_limit}")

        # Step 2: Execute batches
        completed_batches = 0
        total_filled = 0
        leg_stats = {leg: {'filled': 0, 'slippage': 0.0} for leg in symbols}

        for batch in plan.batches:
            try:
                # Fetch depth and set prices
                instruments = [f"NFO:{s}" for s in symbols.values()]
                depth_data = self.depth_analyzer.fetch_depth(instruments)
                self.depth_analyzer.set_leg_prices(batch, depth_data)

                # Execute batch
                batch = self.batch_executor.execute_batch(batch)

                # Handle partial fills
                if batch.status == BatchStatus.PARTIAL:
                    warnings.append(f"Batch {batch.batch_number} had partial fills")

                    # Attempt rebalance
                    if self.rebalancer.rebalance_batch(batch):
                        logger.info(f"Batch {batch.batch_number} rebalanced successfully")
                    else:
                        errors.append(f"Batch {batch.batch_number} rebalance failed")
                        # Continue with next batch - position still balanced up to this point

                # Aggregate results if batch is balanced or rebalanced
                if batch.status in (BatchStatus.COMPLETED, BatchStatus.REBALANCED):
                    completed_batches += 1
                    batch_filled = batch.filled_quantity
                    total_filled += batch_filled

                    # Update leg statistics
                    for leg_type, leg in batch.legs.items():
                        if leg.status == LegStatus.FILLED:
                            leg_stats[leg_type]['filled'] += leg.filled_qty
                            if leg.price and leg.fill_price:
                                leg_stats[leg_type]['slippage'] += abs(
                                    leg.fill_price - leg.price
                                ) * leg.filled_qty

                # Inter-batch delay (except for last batch)
                if batch != plan.batches[-1]:
                    logger.debug(f"Inter-batch delay: {self.inter_batch_delay}s")
                    time.sleep(self.inter_batch_delay)

            except Exception as e:
                errors.append(f"Batch {batch.batch_number} failed: {e}")
                logger.error(f"Batch {batch.batch_number} error: {e}")
                # Stop execution on critical failure
                break

        # Calculate total slippage
        total_slippage = sum(s['slippage'] for s in leg_stats.values())

        execution_time = time.time() - start_time

        result = ExecutionResult(
            success=total_filled > 0,
            total_filled_qty=total_filled,
            target_qty=total_quantity,
            batches_completed=completed_batches,
            batches_total=plan.num_batches,
            total_slippage=total_slippage,
            execution_time_seconds=execution_time,
            leg_summaries=leg_stats,
            errors=errors,
            warnings=warnings
        )

        logger.info(f"Scaled entry complete: {total_filled}/{total_quantity} filled "
                   f"in {execution_time:.1f}s, slippage={total_slippage:.2f}")

        return result
```

---

## 5. Integration Points

### 5.1 EntryManager Integration

The existing `EntryManager.execute_entry()` will be modified to use `ScaledOrderExecutor` when quantity exceeds a threshold:

```python
# In entry_manager.py

def execute_entry(self, conditions: EntryConditions, ...) -> EntryResult:
    # ... existing validation ...

    quantity = self._get_quantity(conditions.expiry)

    # Use scaled executor for large orders
    freeze_limit = self.config.get('scaling', {}).get('freeze_limits', {}).get('NIFTY', 1800)

    if quantity > freeze_limit:
        logger.info(f"Using scaled execution for {quantity} contracts")
        scaled_executor = ScaledOrderExecutor(self.kite, self.config)

        result = scaled_executor.execute_entry(
            symbols=symbols,
            total_quantity=quantity,
            instrument="NIFTY"
        )

        # Map ScaledOrderExecutor result to existing flow
        if not result.success:
            return EntryResult(success=False, error="; ".join(result.errors))

        # Update quantity to actual filled amount
        actual_quantity = result.total_filled_qty
        # ... continue with existing recording logic ...
    else:
        # Use existing single-batch execution
        orders = execute_iron_fly_entry(...)
```

### 5.2 Configuration Integration

New config section will be added to `config/config.yaml`:

```yaml
# Add under trading section
scaling:
  freeze_limits:
    NIFTY: 1800
    BANKNIFTY: 600
    FINNIFTY: 1200
    MIDCPNIFTY: 2800
  execution:
    inter_batch_delay_ms: 3000
    order_timeout_seconds: 30
    max_retries_per_order: 2
  order_types:
    atm_straddle: "MARKET"
    otm_wings: "LIMIT"
  slippage:
    base_ticks: 3
    depth_buffer_ticks: 2
    max_ticks: 15
    wing_retry_increment: 2
  rebalance:
    max_attempts: 2
    strategy: "trim_to_balance"
```

---

## 6. Error Handling

### 6.1 Error Categories

| Category | Handling | Recovery |
|----------|----------|----------|
| Order Rejected | Log, retry once | If retry fails, mark batch partial |
| Order Timeout | Convert LIMIT→MARKET | If MARKET fails, mark batch partial |
| API Error | Log, retry with backoff | Max 3 retries then abort |
| Imbalanced Batch | Trigger rebalancer | Trim excess to restore balance |
| Rebalance Failed | Log critical alert | Stop execution, alert user |
| Network Error | Retry with exponential backoff | Max 3 retries then abort |

### 6.2 Critical Alerts

When rebalancing fails, system MUST:
1. Stop further batch execution
2. Log position state (what's filled, what's imbalanced)
3. Send CRITICAL Telegram alert with:
   - Current position state
   - Imbalance details
   - Required manual action

---

## 7. Testing Strategy

### 7.1 Unit Tests

- `test_batch_planner.py`: Batch splitting logic
- `test_depth_analyzer.py`: Price calculation, slippage
- `test_batch_executor.py`: Order placement, fill waiting
- `test_position_rebalancer.py`: Trim logic
- `test_scaled_executor.py`: Full flow orchestration

### 7.2 Integration Tests

- Test with paper trading mode
- Simulate partial fills
- Simulate order rejections
- Verify rebalancing works

### 7.3 Test Scenarios

| Scenario | Expected Outcome |
|----------|------------------|
| 100 lots (7500 qty) | 5 batches, all complete |
| Batch 3 partial fill | Rebalance trims excess |
| Wing LIMIT timeout | Convert to MARKET |
| API failure mid-batch | Retry, then abort if persistent |
| All batches fail | Return partial success if any filled |

---

## 8. Monitoring & Logging

### 8.1 Log Events

```
[INFO] Starting scaled entry: 7500 contracts, instrument=NIFTY
[INFO] Execution plan: 5 batches, freeze_limit=1800
[INFO] Batch 1 started: 1800 contracts per leg
[DEBUG] Placed straddle_ce: PAPER_20251211...
[DEBUG] straddle_ce filled: 245.50
[INFO] Batch 1 completed: 1800 per leg
[DEBUG] Inter-batch delay: 3.0s
[WARNING] Batch 3 partial: imbalance={'wing_pe': -200}
[INFO] Rebalancing batch 3: {'wing_pe': -200, ...}
[INFO] Trimmed 200 from straddle_ce
[INFO] Batch 3 rebalanced successfully
[INFO] Scaled entry complete: 7200/7500 filled in 45.3s, slippage=125.50
```

### 8.2 Metrics to Track

- Batches completed vs total
- Fill rate per leg
- Total slippage vs expected
- Execution time
- Rebalance frequency

---

## 9. Future Enhancements

1. **Exit Flow Integration**: Apply same batch logic to exits
2. **Dynamic Freeze Limit Fetching**: Query NSE/broker API
3. **TWAP/VWAP Execution**: Spread execution over time
4. **Liquidity Scoring**: Skip batches during low liquidity
5. **Adaptive Slippage**: ML-based slippage prediction

---

## 10. Appendix

### 10.1 Freeze Limit History

| Date | Index | Old Limit | New Limit |
|------|-------|-----------|-----------|
| May 2025 | BANKNIFTY | 900 | 600 |
| Dec 2025 | FINNIFTY | 1800 | 1200 |

### 10.2 References

- [Zerodha Freeze Limits Bulletin](https://zerodha.com/marketintel/bulletin/412395/quantity-freeze-limits-for-indices-from-may-02-2025)
- [Kite Connect API Documentation](https://kite.trade/docs/connect/v3/orders/)
- SNAIL Technical Design Reference v1.0

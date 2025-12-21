"""
Order Executor for Expiry Trading System

Handles batch order execution respecting exchange freeze limits.

@file        order_executor.py
@description Batch order execution with freeze limit handling
"""

import time
from datetime import datetime
from typing import Dict, List, Any, Tuple
from dataclasses import dataclass, field
from enum import Enum
from loguru import logger

from src.strategy import IronCondorSetup
from src.state_manager import LegPosition


class OrderSide(str, Enum):
    """Order transaction type."""
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """Order status."""
    PENDING = "PENDING"
    COMPLETE = "COMPLETE"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass
class OrderRequest:
    """Single order request."""
    symbol: str
    instrument_token: int
    strike: float
    option_type: str
    transaction_type: OrderSide
    quantity: int
    price: float
    order_type: str = "LIMIT"


@dataclass
class OrderResult:
    """Order execution result."""
    order_id: str
    symbol: str
    transaction_type: str
    quantity: int
    price: float
    status: OrderStatus
    fill_price: float = 0.0
    message: str = ""


@dataclass
class BatchResult:
    """Result of batch order execution."""
    success: bool
    orders: List[OrderResult] = field(default_factory=list)
    total_quantity: int = 0
    error_message: str = ""


class OrderExecutor:
    """
    Execute orders in batches respecting freeze limits.

    For large positions (50+ lots), splits orders into batches
    within exchange limits.
    """

    EXCHANGE = "NFO"
    PRODUCT = "NRML"

    def __init__(self, kite_client: Any, config: Dict[str, Any]):
        """
        Initialize order executor.

        Args:
            kite_client: Kite API client
            config: Configuration dictionary
        """
        self.kite = kite_client
        self.config = config
        self.mode = config.get('mode', 'paper')

        # Freeze limits from config
        self.freeze_limits = config.get('freeze_limits', {'NIFTY': 1800})

        # Execution params
        exec_config = config.get('order_execution', {})
        self.inter_batch_delay = exec_config.get('inter_batch_delay_secs', 3)
        self.order_timeout = exec_config.get('order_timeout_secs', 30)
        self.max_retries = exec_config.get('max_retries', 2)

        # Slippage
        strategy_config = config.get('strategy', {})
        self.slippage_pct = strategy_config.get('slippage_buffer_pct', 0.5)

    def get_freeze_limit(self, instrument: str) -> int:
        """Get freeze limit for instrument."""
        return self.freeze_limits.get(instrument, 1800)

    def calculate_batches(
        self,
        total_qty: int,
        freeze_limit: int
    ) -> List[int]:
        """
        Split total quantity into batches.

        Args:
            total_qty: Total quantity to execute
            freeze_limit: Max quantity per order

        Returns:
            List of quantities per batch
        """
        batches = []
        remaining = total_qty

        while remaining > 0:
            batch_qty = min(remaining, freeze_limit)
            batches.append(batch_qty)
            remaining -= batch_qty

        logger.info(f"Split {total_qty} into {len(batches)} batches: {batches}")
        return batches

    def apply_slippage(
        self,
        price: float,
        side: OrderSide
    ) -> float:
        """
        Apply slippage buffer to price.

        For BUY: increase price (willing to pay more)
        For SELL: decrease price (willing to accept less)

        Args:
            price: Original price
            side: Order side

        Returns:
            Price with slippage applied
        """
        slippage = price * (self.slippage_pct / 100)

        if side == OrderSide.BUY:
            return round(price + slippage, 2)
        else:
            return round(price - slippage, 2)

    def place_single_order(
        self,
        request: OrderRequest
    ) -> OrderResult:
        """
        Place a single order.

        Args:
            request: Order request details

        Returns:
            Order result
        """
        try:
            if self.mode == 'paper':
                # Paper trading - simulate order
                order_id = f"PAPER_{datetime.now().strftime('%H%M%S%f')}"
                logger.info(
                    f"[PAPER] Order: {request.transaction_type.value} "
                    f"{request.quantity} {request.symbol} @ {request.price:.2f}"
                )
                return OrderResult(
                    order_id=order_id,
                    symbol=request.symbol,
                    transaction_type=request.transaction_type.value,
                    quantity=request.quantity,
                    price=request.price,
                    status=OrderStatus.COMPLETE,
                    fill_price=request.price
                )

            # Live trading
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.EXCHANGE,
                tradingsymbol=request.symbol,
                transaction_type=request.transaction_type.value,
                quantity=request.quantity,
                price=request.price,
                order_type=request.order_type,
                product=self.PRODUCT
            )

            logger.info(
                f"Order placed: {order_id} - {request.transaction_type.value} "
                f"{request.quantity} {request.symbol} @ {request.price:.2f}"
            )

            # Wait for fill and get status
            fill_price = self._wait_for_fill(order_id)

            return OrderResult(
                order_id=order_id,
                symbol=request.symbol,
                transaction_type=request.transaction_type.value,
                quantity=request.quantity,
                price=request.price,
                status=OrderStatus.COMPLETE,
                fill_price=fill_price
            )

        except Exception as e:
            logger.error(f"Order failed: {e}")
            return OrderResult(
                order_id="",
                symbol=request.symbol,
                transaction_type=request.transaction_type.value,
                quantity=request.quantity,
                price=request.price,
                status=OrderStatus.REJECTED,
                message=str(e)
            )

    def _wait_for_fill(self, order_id: str) -> float:
        """
        Wait for order to fill and return fill price.

        Args:
            order_id: Order ID to wait for

        Returns:
            Average fill price
        """
        start_time = time.time()

        while time.time() - start_time < self.order_timeout:
            try:
                # Use order_history to get order status (Kite Connect API)
                history = self.kite.order_history(order_id)
                if not history:
                    logger.warning(f"No order history for {order_id}")
                    time.sleep(1)
                    continue

                # Latest status is the last item in history
                status = history[-1]
                order_status = status.get('status', '')

                if order_status == 'COMPLETE':
                    return float(status.get('average_price', 0))
                elif order_status in ['REJECTED', 'CANCELLED']:
                    raise Exception(
                        f"Order {order_status}: {status.get('status_message', 'Unknown error')}"
                    )
                # OPEN, PENDING, TRIGGER_PENDING - keep waiting
            except Exception as e:
                if 'Order not found' in str(e):
                    # Order may not be in system yet, retry
                    logger.debug(f"Order {order_id} not found yet, retrying...")
                else:
                    logger.warning(f"Error checking order status: {e}")

            time.sleep(1)

        raise Exception(f"Order timeout after {self.order_timeout}s")

    def execute_iron_condor_entry(
        self,
        setup: IronCondorSetup,
        num_lots: int
    ) -> Tuple[bool, List[LegPosition]]:
        """
        Execute iron condor entry with batch handling.

        Args:
            setup: Iron condor setup with strikes and premiums
            num_lots: Number of lots to trade

        Returns:
            Tuple of (success, list of leg positions)
        """
        total_qty = num_lots * setup.lot_size
        freeze_limit = self.get_freeze_limit(self.config.get('instrument', 'NIFTY'))

        # Calculate batches
        batches = self.calculate_batches(total_qty, freeze_limit)

        # Prepare order requests for all 4 legs
        def create_leg_orders(batch_qty: int) -> List[OrderRequest]:
            return [
                # Short CE (SELL at bid - slippage)
                OrderRequest(
                    symbol=setup.short_ce.symbol,
                    instrument_token=setup.short_ce.instrument_token,
                    strike=setup.short_ce.strike,
                    option_type='CE',
                    transaction_type=OrderSide.SELL,
                    quantity=batch_qty,
                    price=self.apply_slippage(setup.short_ce_premium, OrderSide.SELL)
                ),
                # Short PE (SELL at bid - slippage)
                OrderRequest(
                    symbol=setup.short_pe.symbol,
                    instrument_token=setup.short_pe.instrument_token,
                    strike=setup.short_pe.strike,
                    option_type='PE',
                    transaction_type=OrderSide.SELL,
                    quantity=batch_qty,
                    price=self.apply_slippage(setup.short_pe_premium, OrderSide.SELL)
                ),
                # Long CE (BUY at ask + slippage)
                OrderRequest(
                    symbol=setup.long_ce.symbol,
                    instrument_token=setup.long_ce.instrument_token,
                    strike=setup.long_ce.strike,
                    option_type='CE',
                    transaction_type=OrderSide.BUY,
                    quantity=batch_qty,
                    price=self.apply_slippage(setup.long_ce_premium, OrderSide.BUY)
                ),
                # Long PE (BUY at ask + slippage)
                OrderRequest(
                    symbol=setup.long_pe.symbol,
                    instrument_token=setup.long_pe.instrument_token,
                    strike=setup.long_pe.strike,
                    option_type='PE',
                    transaction_type=OrderSide.BUY,
                    quantity=batch_qty,
                    price=self.apply_slippage(setup.long_pe_premium, OrderSide.BUY)
                )
            ]

        all_results: Dict[str, List[OrderResult]] = {
            setup.short_ce.symbol: [],
            setup.short_pe.symbol: [],
            setup.long_ce.symbol: [],
            setup.long_pe.symbol: []
        }

        # Track filled orders for potential rollback
        filled_orders: List[OrderResult] = []

        # Execute batches
        for batch_idx, batch_qty in enumerate(batches):
            logger.info(f"Executing batch {batch_idx + 1}/{len(batches)}: {batch_qty} qty")

            orders = create_leg_orders(batch_qty)

            for order in orders:
                result = self.place_single_order(order)
                all_results[order.symbol].append(result)

                if result.status == OrderStatus.COMPLETE:
                    filled_orders.append(result)
                else:
                    logger.error(f"Order failed: {result.message}")

                    # CRITICAL: On failure, attempt to unwind filled orders
                    if filled_orders and self.mode != 'paper':
                        logger.error(
                            f"PARTIAL FILL DETECTED: {len(filled_orders)} legs filled, "
                            f"attempting rollback"
                        )
                        self._rollback_partial_fill(filled_orders)

                    # Return immediately with failure
                    legs = self._build_leg_positions(setup, all_results, total_qty)
                    return False, legs

            # Delay between batches
            if batch_idx < len(batches) - 1:
                logger.info(f"Waiting {self.inter_batch_delay}s before next batch...")
                time.sleep(self.inter_batch_delay)

        # Build leg positions from results
        legs = self._build_leg_positions(setup, all_results, total_qty)

        # Check if all legs filled
        success = all(
            sum(r.quantity for r in results if r.status == OrderStatus.COMPLETE) == total_qty
            for results in all_results.values()
        )

        return success, legs

    def _build_leg_positions(
        self,
        setup: IronCondorSetup,
        results: Dict[str, List[OrderResult]],
        total_qty: int
    ) -> List[LegPosition]:
        """Build leg positions from order results."""
        legs = []

        # Short CE
        short_ce_results = results[setup.short_ce.symbol]
        avg_price = self._calculate_avg_price(short_ce_results)
        legs.append(LegPosition(
            symbol=setup.short_ce.symbol,
            instrument_token=setup.short_ce.instrument_token,
            strike=setup.short_ce.strike,
            option_type='CE',
            transaction_type='SELL',
            quantity=total_qty,
            entry_price=avg_price,
            order_id=','.join(r.order_id for r in short_ce_results)
        ))

        # Short PE
        short_pe_results = results[setup.short_pe.symbol]
        avg_price = self._calculate_avg_price(short_pe_results)
        legs.append(LegPosition(
            symbol=setup.short_pe.symbol,
            instrument_token=setup.short_pe.instrument_token,
            strike=setup.short_pe.strike,
            option_type='PE',
            transaction_type='SELL',
            quantity=total_qty,
            entry_price=avg_price,
            order_id=','.join(r.order_id for r in short_pe_results)
        ))

        # Long CE
        long_ce_results = results[setup.long_ce.symbol]
        avg_price = self._calculate_avg_price(long_ce_results)
        legs.append(LegPosition(
            symbol=setup.long_ce.symbol,
            instrument_token=setup.long_ce.instrument_token,
            strike=setup.long_ce.strike,
            option_type='CE',
            transaction_type='BUY',
            quantity=total_qty,
            entry_price=avg_price,
            order_id=','.join(r.order_id for r in long_ce_results)
        ))

        # Long PE
        long_pe_results = results[setup.long_pe.symbol]
        avg_price = self._calculate_avg_price(long_pe_results)
        legs.append(LegPosition(
            symbol=setup.long_pe.symbol,
            instrument_token=setup.long_pe.instrument_token,
            strike=setup.long_pe.strike,
            option_type='PE',
            transaction_type='BUY',
            quantity=total_qty,
            entry_price=avg_price,
            order_id=','.join(r.order_id for r in long_pe_results)
        ))

        return legs

    def _calculate_avg_price(self, results: List[OrderResult]) -> float:
        """Calculate average fill price from results."""
        total_qty = 0
        total_value = 0.0

        for r in results:
            if r.status == OrderStatus.COMPLETE:
                price = r.fill_price if r.fill_price > 0 else r.price
                total_qty += r.quantity
                total_value += price * r.quantity

        if total_qty == 0:
            return 0.0

        return total_value / total_qty

    def _rollback_partial_fill(self, filled_orders: List[OrderResult]) -> None:
        """
        Attempt to unwind partially filled orders.

        When some legs of an iron condor fill but others fail,
        we must close the filled positions to avoid naked exposure.

        Args:
            filled_orders: List of successfully filled order results
        """
        logger.error("=" * 60)
        logger.error("EMERGENCY ROLLBACK: Unwinding partial fill")
        logger.error("=" * 60)

        for order in filled_orders:
            try:
                # Reverse the transaction type
                if order.transaction_type == 'SELL':
                    reverse_side = 'BUY'
                    # For closing a short, pay more to ensure fill
                    exit_price = order.fill_price * 1.02  # 2% above fill
                else:
                    reverse_side = 'SELL'
                    # For closing a long, accept less
                    exit_price = order.fill_price * 0.98  # 2% below fill

                logger.error(
                    f"Rollback: {reverse_side} {order.quantity} {order.symbol} "
                    f"@ {exit_price:.2f} (original: {order.fill_price:.2f})"
                )

                rollback_order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.EXCHANGE,
                    tradingsymbol=order.symbol,
                    transaction_type=reverse_side,
                    quantity=order.quantity,
                    price=round(exit_price, 2),
                    order_type="LIMIT",
                    product=self.PRODUCT
                )

                logger.error(f"Rollback order placed: {rollback_order_id}")

                # Wait briefly for fill
                try:
                    self._wait_for_fill(rollback_order_id)
                    logger.error(f"Rollback order {rollback_order_id} filled")
                except Exception as fill_error:
                    logger.error(f"Rollback order may not have filled: {fill_error}")

            except Exception as e:
                logger.error(f"CRITICAL: Rollback failed for {order.symbol}: {e}")
                logger.error("MANUAL INTERVENTION REQUIRED!")

        logger.error("=" * 60)
        logger.error("ROLLBACK COMPLETE - Check positions manually")
        logger.error("=" * 60)

    def execute_exit(
        self,
        legs: List[LegPosition],
        quotes: Dict[str, Any]
    ) -> Tuple[bool, float]:
        """
        Execute exit for all legs.

        For SELL positions: BUY back at ask
        For BUY positions: SELL at bid

        Args:
            legs: List of leg positions to exit
            quotes: Current quotes for pricing

        Returns:
            Tuple of (success, total_exit_value)
        """
        total_exit_value = 0.0
        all_success = True

        for leg in legs:
            quote_key = f"NFO:{leg.symbol}"
            quote = quotes.get(quote_key)

            if not quote:
                logger.error(f"No quote for {leg.symbol}")
                all_success = False
                continue

            # Determine exit price and side
            if leg.transaction_type == 'SELL':
                # Close short by buying back at ask
                exit_side = OrderSide.BUY
                exit_price = self.apply_slippage(quote.ask, OrderSide.BUY)
            else:
                # Close long by selling at bid
                exit_side = OrderSide.SELL
                exit_price = self.apply_slippage(quote.bid, OrderSide.SELL)

            # Execute in batches if needed
            freeze_limit = self.get_freeze_limit(self.config.get('instrument', 'NIFTY'))
            batches = self.calculate_batches(leg.quantity, freeze_limit)

            for batch_idx, batch_qty in enumerate(batches):
                request = OrderRequest(
                    symbol=leg.symbol,
                    instrument_token=leg.instrument_token,
                    strike=leg.strike,
                    option_type=leg.option_type,
                    transaction_type=exit_side,
                    quantity=batch_qty,
                    price=exit_price
                )

                result = self.place_single_order(request)

                if result.status == OrderStatus.COMPLETE:
                    fill_price = result.fill_price if result.fill_price > 0 else exit_price
                    leg.exit_price = fill_price
                    leg.exit_order_id = result.order_id
                    total_exit_value += fill_price * batch_qty
                else:
                    logger.error(f"Exit failed for {leg.symbol}: {result.message}")
                    all_success = False

                if batch_idx < len(batches) - 1:
                    time.sleep(self.inter_batch_delay)

        return all_success, total_exit_value

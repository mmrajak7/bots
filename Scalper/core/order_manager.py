"""
NEO Trade Terminal - Order Manager

Handles all order operations with safety checks.
Implements bracket orders, multi-leg orders, and position management.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import threading
import time
import logging
import math

from core.tick_utils import calculate_sl_limit_price, round_to_tick_str, round_trigger_price, format_price
from core.order_tracker import OrderTracker, OrderType, get_order_tracker, TrackedOrder

logger = logging.getLogger(__name__)


@dataclass
class OrderParams:
    """Standard order parameters."""
    symbol: str                    # NEO trading symbol
    exchange_segment: str          # nse_fo, nse_cm, bse_fo, etc.
    instrument_token: str
    transaction_type: str          # B or S
    quantity: int                  # Total quantity (not lots)
    product: str                   # MIS, NRML
    order_type: str               # L, MKT, SL, SL-M
    price: Optional[float] = None  # For limit orders
    trigger_price: Optional[float] = None  # For SL orders
    disclosed_qty: int = 0
    validity: str = "DAY"
    tag: Optional[str] = None


@dataclass
class BracketOrderParams:
    """Bracket order with SL and Target."""
    entry: OrderParams
    stop_loss_points: float        # Points from entry
    target_points: float           # Points from entry
    trailing_sl: bool = False
    trailing_sl_points: float = 0


@dataclass
class OrderResult:
    """Result of an order operation."""
    success: bool
    order_id: Optional[str] = None
    message: str = ""
    response: Optional[Dict[str, Any]] = None


class OrderManager:
    """Manages order execution with safety checks."""

    def __init__(self, neo_client, config: Dict[str, Any], symbol_mapper=None,
                 realized_pnl_tracker=None, telegram=None):
        self.client = neo_client
        self.config = config
        self.mapper = symbol_mapper
        self.pnl_tracker = realized_pnl_tracker  # For accurate realized P&L
        self.telegram = telegram  # For critical SL failure notifications
        self.recent_orders: List[Tuple[float, OrderParams]] = []
        self._lock = threading.Lock()

        # S35: Order tracker for unique tag-based order identification
        self.order_tracker = get_order_tracker()

        # Risk management settings
        risk_config = config.get('risk_management', {})
        self.max_loss_per_day = risk_config.get('max_loss_per_day', 10000)
        self.max_open_positions = risk_config.get('max_open_positions', 5)
        self.duplicate_window = risk_config.get('duplicate_order_window_sec', 5)

        # Trading defaults
        trading_defaults = config.get('trading_defaults', {})
        self.default_product = trading_defaults.get('product', 'MIS')
        self.default_order_type = trading_defaults.get('order_type', 'L')
        self.default_validity = trading_defaults.get('validity', 'DAY')

        # Track daily P&L
        self._daily_pnl = 0.0
        self._trading_halted = False
        self._last_reset_date = datetime.now().date()  # Track last reset for new day detection
        self._circuit_breaker_file = "data/circuit_breaker.json"

        # CRITICAL: Load circuit breaker state from disk (survives restarts)
        self._load_circuit_breaker_state()

        # Market hours configuration
        market_config = config.get('market_hours', {})
        self.market_open = market_config.get('open', '09:15')
        self.market_close = market_config.get('close', '15:30')
        self.mis_squareoff_warning = market_config.get('mis_warning', '15:15')
        self.mis_squareoff_cutoff = market_config.get('mis_cutoff', '15:20')

    def _check_market_hours(self, params: OrderParams) -> Dict[str, Any]:
        """
        Check if current time is within market hours.

        Returns:
            Dict with:
            - 'allowed': True if order can proceed
            - 'warning': Optional warning message
            - 'error': Error message if blocked
        """
        now = datetime.now()
        current_time = now.strftime('%H:%M')

        # Parse market hours
        market_open = self.market_open
        market_close = self.market_close

        # Check if before market open
        if current_time < market_open:
            return {
                'allowed': False,
                'error': f'Market not open yet. Opens at {market_open}. Current: {current_time}'
            }

        # Check if after market close
        if current_time >= market_close:
            return {
                'allowed': False,
                'error': f'Market closed at {market_close}. Current: {current_time}'
            }

        # MIS-specific checks (only for NEW orders, not exits)
        is_exit = params.tag == 'EXIT'
        if params.product == 'MIS' and not is_exit:
            # Hard cutoff - block new MIS orders after cutoff time
            # EXIT orders are always allowed (must be able to square off)
            if current_time >= self.mis_squareoff_cutoff:
                return {
                    'allowed': False,
                    'error': f'MIS orders blocked after {self.mis_squareoff_cutoff} (broker squareoff imminent)'
                }

            # Warning zone - allow but warn
            if current_time >= self.mis_squareoff_warning:
                return {
                    'allowed': True,
                    'warning': f'WARNING: MIS squareoff at 15:25. Consider exiting positions.'
                }

        return {'allowed': True}

    def _verify_order_in_book(self, symbol: str, transaction_type: str,
                               quantity: int, order_type: str = 'MKT',
                               max_age_seconds: int = 10) -> Optional[str]:
        """
        S35: Verify if an order exists in order book (for 'error from core' recovery).

        When NEO API returns error but order may have been placed, check order book
        for recent orders matching the criteria.

        CRITICAL FIX (S35): Now requires order_type match to prevent confusing
        SL orders (SL/SL-L) with regular orders (MKT/L).

        Args:
            symbol: Trading symbol
            transaction_type: 'B' or 'S'
            quantity: Order quantity
            order_type: Expected order type ('MKT', 'L', 'SL', 'SL-M')
            max_age_seconds: How recent the order must be (default 10s)

        Returns:
            Order ID if found, None otherwise
        """
        import time
        from datetime import datetime

        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            if not orders:
                return None

            current_time = time.time()

            for order in orders:
                # Match symbol
                order_symbol = order.get('trdSym', order.get('tradingSymbol', ''))
                if order_symbol != symbol:
                    continue

                # Match transaction type
                order_txn = order.get('trnsTp', order.get('transactionType', ''))
                if order_txn != transaction_type:
                    continue

                # Match quantity
                try:
                    order_qty = int(float(order.get('qty', 0) or 0))
                except (ValueError, TypeError):
                    order_qty = 0
                if order_qty != quantity:
                    continue

                # S35 CRITICAL: Match order type to prevent confusing SL with EXIT orders
                # NEO API uses 'prcTp' for order type (L, MKT, SL, SL-M)
                api_order_type = order.get('prcTp', order.get('orderType', '')).upper()
                expected_type = order_type.upper()

                # Normalize order types for comparison
                # MKT orders should only match MKT, not SL/SL-M/SL-L
                if expected_type == 'MKT':
                    if api_order_type not in ['MKT', 'MARKET']:
                        continue
                elif expected_type in ['L', 'LIMIT']:
                    if api_order_type not in ['L', 'LIMIT']:
                        continue
                elif expected_type in ['SL', 'SL-M', 'SL-L']:
                    if api_order_type not in ['SL', 'SL-M', 'SL-L']:
                        continue

                # Check if order is recent (within max_age_seconds)
                order_time_str = order.get('ordDtTm', order.get('orderDateTime', ''))
                if order_time_str:
                    try:
                        # Parse order time (format: "23-Jan-2026 15:02:45")
                        order_dt = datetime.strptime(order_time_str, "%d-%b-%Y %H:%M:%S")
                        order_timestamp = order_dt.timestamp()
                        age_seconds = current_time - order_timestamp

                        if age_seconds > max_age_seconds:
                            continue
                    except (ValueError, TypeError):
                        # If can't parse time, check status instead
                        pass

                # Check status - only accept completed/open orders
                # S35: Also reject 'trigger pending' for MKT orders (those are SL orders)
                status = str(order.get('ordSt', order.get('status', ''))).lower()
                if status in ['rejected', 'cancelled', 'canceled']:
                    continue
                if expected_type == 'MKT' and status == 'trigger pending':
                    continue  # MKT orders can't be "trigger pending" - that's an SL order

                # Found matching order!
                order_id = order.get('nOrdNo', order.get('orderId', order.get('order_id', '')))
                if order_id:
                    logger.info(f"[ORDER VERIFY] Found order in book: {symbol} {transaction_type} "
                               f"qty={quantity} type={api_order_type} -> ID: {order_id} (status: {status})")
                    return str(order_id)

            return None

        except Exception as e:
            logger.warning(f"[ORDER VERIFY] Failed to check order book: {e}")
            return None

    def place_order(self, params: OrderParams) -> OrderResult:
        """
        Place single order with safety checks.

        Args:
            params: OrderParams with order details

        Returns:
            OrderResult with success status and order ID
        """
        # Safety Check 1: Trading halt check
        if self._trading_halted:
            return OrderResult(
                success=False,
                message="Trading halted - Daily loss limit reached"
            )

        # Safety Check 2: Market hours check
        market_check = self._check_market_hours(params)
        if not market_check.get('allowed'):
            return OrderResult(
                success=False,
                message=market_check.get('error', 'Order blocked - outside market hours')
            )
        if market_check.get('warning'):
            logger.warning(f"[MARKET HOURS] {market_check['warning']}")

        # Safety Check 3: Duplicate prevention
        if self._is_duplicate_order(params):
            return OrderResult(
                success=False,
                message="Duplicate order detected within time window"
            )

        # Safety Check 4: Position limits
        # Check for ALL new positions - both LONG (BUY) and SHORT (SELL) entries
        # Note: Exits don't increase position count, but fresh SELL creates SHORT position
        if not self._check_position_limits_for_entry(params):
            return OrderResult(
                success=False,
                message="Position limit exceeded"
            )

        # Safety Check 5: Daily loss limit
        if not self._check_daily_loss_limit():
            return OrderResult(
                success=False,
                message="Daily loss limit reached - trading halted"
            )

        # Safety Check 6: Margin check (configurable)
        margin_check = self._check_margin_for_order(params)
        if margin_check.get('block'):
            return OrderResult(
                success=False,
                message=margin_check.get('message', 'Insufficient margin')
            )
        elif margin_check.get('warning'):
            logger.warning(f"[MARGIN WARNING] {margin_check.get('message')}")

        # Place the order
        try:
            # CRITICAL: Properly format price and trigger_price based on order type
            # - MKT: price="0", trigger="0"
            # - L: price=tick-aligned, trigger="0"
            # - SL/SL-M: Handled separately (see _place_sl_order)
            if params.order_type == "MKT":
                formatted_price = "0"
                formatted_trigger = "0"
            elif params.order_type == "L":
                formatted_price = format_price(params.price, params.exchange_segment) if params.price else "0"
                formatted_trigger = "0"
            else:
                # SL orders - format both price and trigger
                formatted_price = format_price(params.price, params.exchange_segment) if params.price else "0"
                formatted_trigger = round_trigger_price(params.trigger_price, params.exchange_segment) if params.trigger_price else "0"

            # S35: Generate unique tag based on order type for tracking
            # Determine order classification from params.tag hint
            price_type = OrderType.ENTRY  # Default
            if params.tag:
                tag_upper = params.tag.upper()
                if 'EXIT' in tag_upper or 'EXT' in tag_upper:
                    price_type = OrderType.EXIT
                elif 'SL' in tag_upper or 'STOP' in tag_upper:
                    price_type = OrderType.SL
                elif 'TGT' in tag_upper or 'TARGET' in tag_upper:
                    price_type = OrderType.TARGET

            # Generate unique tracking tag
            tracking_tag = self.order_tracker.generate_tag(price_type, params.symbol)

            # Register order BEFORE placing (so we can find it later)
            self.order_tracker.register_order(
                tag=tracking_tag,
                symbol=params.symbol,
                transaction_type=params.transaction_type,
                quantity=params.quantity,
                api_order_type=params.order_type,
                price_type=price_type
            )

            logger.info(f"[ORDER] Placing {params.order_type}: {params.symbol} {params.transaction_type} "
                       f"qty={params.quantity} price={formatted_price} trigger={formatted_trigger} tag={tracking_tag}")

            response = self.client.place_order(
                exchange_segment=params.exchange_segment,
                product=params.product,
                price=formatted_price,
                order_type=params.order_type,
                quantity=str(params.quantity),
                validity=params.validity,
                trading_symbol=params.symbol,
                transaction_type=params.transaction_type,
                amo="NO",
                disclosed_quantity=str(params.disclosed_qty),
                trigger_price=formatted_trigger,
                tag=tracking_tag  # S35: Use our unique tracking tag
            )

            # Record for duplicate prevention
            self._record_order(params)

            # Extract order ID and check for rejection
            # S29/S34: More robust order ID extraction - NEO API may return ID in different fields
            order_id = None
            if isinstance(response, dict):
                # Try multiple possible field names (flat and nested)
                order_id = (response.get('nOrdNo') or
                           response.get('orderId') or
                           response.get('order_id') or
                           response.get('orderNo') or
                           response.get('order_no'))

                # Check nested 'data' or 'result' objects
                if not order_id:
                    nested = response.get('data') or response.get('result')
                    if isinstance(nested, dict):
                        order_id = (nested.get('nOrdNo') or
                                   nested.get('orderId') or
                                   nested.get('order_id') or
                                   nested.get('orderNo') or
                                   nested.get('order_no'))
                    # S34: Also check if nested is a list (API sometimes returns list)
                    elif isinstance(nested, list) and len(nested) > 0:
                        first = nested[0]
                        if isinstance(first, dict):
                            order_id = (first.get('nOrdNo') or
                                       first.get('orderId') or
                                       first.get('order_id'))

                # S34: PRIORITIZE order_id - if we have one, treat as success even with error messages
                # NEO API sometimes returns error message but still processes the order
                if order_id and str(order_id).strip():
                    # S35: Confirm order in tracker
                    self.order_tracker.confirm_order(tracking_tag, str(order_id), response)

                    # Check if there's also an error message (API quirk)
                    error_msg = (response.get('errMsg') or response.get('error') or
                                response.get('emsg') or response.get('Error'))
                    if error_msg:
                        logger.warning(f"Order {order_id} placed with API warning: {error_msg}")
                    logger.info(f"Order placed: {params.symbol} {params.transaction_type} "
                               f"{params.quantity} @ {formatted_price} -> ID: {order_id}")
                    return OrderResult(
                        success=True,
                        order_id=order_id,
                        message=f"Order placed successfully",
                        response=response
                    )

                # No order ID - check if order was rejected by broker
                stat = str(response.get('stat', '')).lower()
                if stat in ['rejected', 'not_ok', 'error', 'failed']:
                    rej_reason = response.get('rejRsn') or response.get('rejectionReason') or \
                                 response.get('errMsg') or response.get('message') or 'Unknown reason'
                    logger.warning(f"Order REJECTED by broker: {params.symbol} - {rej_reason}")
                    logger.warning(f"Full rejection response: {response}")
                    return OrderResult(
                        success=False,
                        message=f"REJECTED: {rej_reason}",
                        response=response
                    )

            # CRITICAL: No order ID found - order may or may not have been placed
            # S34: Log full response for debugging and give user actionable message
            logger.error(f"Order placed but NO order ID returned for {params.symbol}!")
            logger.error(f"Full response: {response}")
            logger.error(f"Response type: {type(response)}, keys: {response.keys() if isinstance(response, dict) else 'N/A'}")

            # Check for hidden error messages in response
            error_msg = None
            if isinstance(response, dict):
                error_msg = (response.get('errMsg') or response.get('error') or
                            response.get('message') or response.get('emsg') or
                            response.get('Error') or response.get('Emsg'))
                # Also check stat field for non-OK status
                stat = str(response.get('stat', '')).lower()
                if stat and stat not in ['ok', 'success', '']:
                    error_msg = error_msg or f"Status: {stat}"

            # S34: "error from core" without order ID - order MAY have been placed!
            # Double-check order book before declaring failure
            if error_msg:
                if 'error from core' in str(error_msg).lower():
                    logger.warning(f"NEO API returned 'error from core' - verifying by tag: {tracking_tag}")

                    # S35 CRITICAL: Use TAG-BASED verification first (most reliable)
                    import time
                    time.sleep(0.5)  # Brief wait for order to appear in book

                    # Method 1: Search by our unique tag (preferred)
                    verified_order_id = self.order_tracker.verify_order_by_tag(
                        self.client, tracking_tag, max_age_seconds=15
                    )

                    if verified_order_id:
                        logger.info(f"[ORDER VERIFY] Order CONFIRMED by tag {tracking_tag}: {verified_order_id}")
                        return OrderResult(
                            success=True,
                            order_id=verified_order_id,
                            message=f"Order placed (verified by tag after API error)",
                            response=response
                        )

                    # Method 2: Fallback to criteria-based search (less reliable)
                    # This is kept as backup but with stricter matching
                    logger.warning(f"[ORDER VERIFY] Tag not found, trying criteria match...")
                    verified_order_id = self._verify_order_in_book(
                        symbol=params.symbol,
                        transaction_type=params.transaction_type,
                        quantity=params.quantity,
                        order_type=params.order_type,
                        max_age_seconds=15
                    )

                    if verified_order_id:
                        logger.info(f"[ORDER VERIFY] Order CONFIRMED by criteria: {verified_order_id}")
                        self.order_tracker.confirm_order(tracking_tag, verified_order_id, response)
                        return OrderResult(
                            success=True,
                            order_id=verified_order_id,
                            message=f"Order placed (verified by criteria after API error)",
                            response=response
                        )
                    else:
                        logger.warning(f"[ORDER VERIFY] Order NOT found in book - API error is real")
                        self.order_tracker.mark_failed(tracking_tag, "Not found in book")
                        return OrderResult(
                            success=False,
                            message=f"API error (order not in book): {error_msg}",
                            response=response
                        )
                # Mark as failed for other errors
                self.order_tracker.mark_failed(tracking_tag, str(error_msg))
                return OrderResult(
                    success=False,
                    message=f"Order failed: {error_msg}",
                    response=response
                )

            return OrderResult(
                success=False,
                message="Order may have been placed but no ID returned - check order book",
                response=response
            )

        except Exception as e:
            error_msg = str(e)
            # Try to extract rejection reason from exception message
            if 'rejRsn' in error_msg or 'rejected' in error_msg.lower():
                logger.warning(f"Order REJECTED: {params.symbol} - {error_msg}")
            else:
                logger.error(f"Order placement failed: {error_msg}", exc_info=True)
            return OrderResult(
                success=False,
                message=f"Order failed: {error_msg}"
            )

    def place_bracket_order(self, params: BracketOrderParams) -> OrderResult:
        """
        Place bracket order (entry + SL + target as OCO).
        NEO supports BO for F&O with specific parameters.

        Args:
            params: BracketOrderParams with entry and SL/Target

        Returns:
            OrderResult
        """
        entry = params.entry

        # Calculate SL and Target prices
        if entry.transaction_type == 'B':
            sl_trigger = entry.price - params.stop_loss_points
            target_price = entry.price + params.target_points
        else:
            sl_trigger = entry.price + params.stop_loss_points
            target_price = entry.price - params.target_points

        try:
            # CRITICAL: Format price to tick size
            formatted_price = format_price(entry.price, entry.exchange_segment)
            formatted_trigger = round_trigger_price(sl_trigger, entry.exchange_segment)

            logger.info(f"[BRACKET] Placing: {entry.symbol} {entry.transaction_type} "
                       f"qty={entry.quantity} price={formatted_price} sl_trigger={formatted_trigger}")

            response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product="BO",
                price=formatted_price,
                order_type="L",
                quantity=str(entry.quantity),
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=entry.transaction_type,
                amo="NO",
                trigger_price=formatted_trigger,
                square_off_type="Absolute",
                stop_loss_type="Absolute",
                stop_loss_value=str(params.stop_loss_points),
                square_off_value=str(params.target_points),
                trailing_stop_loss="Y" if params.trailing_sl else "N",
                trailing_sl_value=str(params.trailing_sl_points) if params.trailing_sl else "0"
            )

            self._record_order(entry)

            order_id = None
            if isinstance(response, dict):
                order_id = response.get('nOrdNo') or response.get('orderId')

            return OrderResult(
                success=True,
                order_id=order_id,
                message="Bracket order placed",
                response=response
            )

        except Exception as e:
            logger.warning(f"Bracket order failed: {e}, falling back to manual SL/Target")
            return OrderResult(
                success=False,
                message=f"Bracket order failed: {str(e)}. Use manual SL/Target."
            )

    def place_multi_leg_order(self, legs: List[OrderParams]) -> List[OrderResult]:
        """
        Place multiple legs simultaneously (for spreads).
        Uses threading for parallel execution.

        Args:
            legs: List of OrderParams for each leg

        Returns:
            List of OrderResults
        """
        results: List[Tuple[int, OrderResult]] = []
        threads = []
        results_lock = threading.Lock()

        def place_leg(leg: OrderParams, index: int):
            try:
                result = self.place_order(leg)
                with results_lock:
                    results.append((index, result))
            except Exception as e:
                with results_lock:
                    results.append((index, OrderResult(
                        success=False,
                        message=str(e)
                    )))

        # Start all orders in parallel
        for i, leg in enumerate(legs):
            t = threading.Thread(target=place_leg, args=(leg, i))
            threads.append(t)
            t.start()

        # Wait for all to complete
        for t in threads:
            t.join()

        # Sort results by index to maintain order
        results.sort(key=lambda x: x[0])
        return [r[1] for r in results]

    def modify_order(self, order_id: str, new_price: float = None,
                     new_quantity: int = None, new_trigger: float = None) -> OrderResult:
        """
        Modify existing order.

        Args:
            order_id: Order ID to modify
            new_price: New price (optional)
            new_quantity: New quantity (optional)
            new_trigger: New trigger price for SL orders (optional)

        Returns:
            OrderResult
        """
        try:
            # First fetch the order to get current details (NEO API requires order_type)
            orders = self.get_orders()
            current_order = None
            for order in orders:
                if order.get('nOrdNo') == order_id:
                    current_order = order
                    break

            if not current_order:
                return OrderResult(
                    success=False,
                    message=f"Order not found: {order_id}"
                )

            # Get current order details
            current_price = current_order.get('prc', current_order.get('price', '0'))
            current_qty = current_order.get('qty', current_order.get('quantity', '0'))
            current_trigger = current_order.get('trgPrc', current_order.get('triggerPrice', '0'))
            order_type = current_order.get('prcTp', current_order.get('orderType', 'L'))
            exchange_segment = current_order.get('exSeg', current_order.get('exchange_segment', 'nse_fo'))

            # Use new values or fall back to current
            # S32: Round new prices to tick size for exchange
            if new_price is not None:
                price = format_price(new_price, exchange_segment)
            else:
                price = str(current_price)

            quantity = str(new_quantity) if new_quantity is not None else str(current_qty)

            if new_trigger is not None:
                trigger_price = round_trigger_price(new_trigger, exchange_segment)
            else:
                trigger_price = str(current_trigger)

            response = self.client.modify_order(
                order_id=order_id,
                price=price,
                order_type=order_type,
                quantity=quantity,
                trigger_price=trigger_price,
                validity="DAY"
            )

            logger.info(f"Order modified: {order_id}")
            return OrderResult(
                success=True,
                order_id=order_id,
                message="Order modified",
                response=response
            )

        except Exception as e:
            logger.error(f"Order modification failed: {e}")
            return OrderResult(
                success=False,
                message=f"Modification failed: {str(e)}"
            )

    def cancel_order(self, order_id: str) -> OrderResult:
        """
        Cancel order by ID.

        Args:
            order_id: Order ID to cancel

        Returns:
            OrderResult
        """
        try:
            response = self.client.cancel_order(order_id=order_id)
            logger.info(f"Order cancelled: {order_id}")
            return OrderResult(
                success=True,
                order_id=order_id,
                message="Order cancelled",
                response=response
            )

        except Exception as e:
            logger.error(f"Order cancellation failed: {e}")
            return OrderResult(
                success=False,
                message=f"Cancellation failed: {str(e)}"
            )

    def exit_position(self, position: Dict[str, Any], exit_qty_percent: int = 100) -> OrderResult:
        """
        Exit a position by placing opposite order.

        Args:
            position: Position dict from positions()
            exit_qty_percent: 25, 50, 75, or 100

        Returns:
            OrderResult
        """
        # NEO API uses flBuyQty and flSellQty - calculate net position
        # S35: Use int(float()) to handle string values like "150.5"
        buy_qty = position.get('flBuyQty', position.get('buyQty', 0))
        sell_qty = position.get('flSellQty', position.get('sellQty', 0))
        try:
            qty = int(float(buy_qty or 0)) - int(float(sell_qty or 0))
        except (ValueError, TypeError):
            try:
                qty = int(float(position.get('qty', 0) or 0))
            except (ValueError, TypeError):
                qty = 0

        if qty == 0:
            return OrderResult(success=False, message="No position to exit")

        exit_qty = int(abs(qty) * exit_qty_percent / 100)

        # S27: Validate exit_qty is positive
        if exit_qty <= 0:
            return OrderResult(
                success=False,
                message=f"Exit quantity too small: {abs(qty)} qty * {exit_qty_percent}% = {exit_qty} (need >0)"
            )

        # Opposite transaction
        txn_type = 'S' if qty > 0 else 'B'

        # NEO API uses 'trdSym' as primary key for trading symbol
        symbol = position.get('trdSym', position.get('tradingSymbol', position.get('symbol', '')))
        if not symbol:
            return OrderResult(success=False, message="REJECTED: please provide valid symbol")

        params = OrderParams(
            symbol=symbol,
            exchange_segment=position.get('exchange_segment', position.get('exchangeSegment', position.get('exSeg', 'nse_fo'))),
            instrument_token=str(position.get('instrument_token', position.get('token', position.get('tok', '')))),
            transaction_type=txn_type,
            quantity=exit_qty,
            product=position.get('product', position.get('prd', self.default_product)),
            order_type='MKT',  # Market order for quick exit
            tag='EXIT'
        )

        return self.place_order(params)

    def exit_all_positions(self) -> List[Dict[str, Any]]:
        """
        Square off all open positions.

        Returns:
            List of results for each position
        """
        try:
            positions_response = self.client.positions()
            positions = positions_response.get('data', []) if positions_response else []
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return [{'status': 'error', 'error': str(e)}]

        results = []

        for pos in positions:
            # S35: Safe int conversion - handle "150.0" strings and None
            try:
                qty = int(float(pos.get('qty', 0) or 0))
            except (ValueError, TypeError):
                qty = 0
            if qty != 0:
                symbol = pos.get('trdSym', pos.get('tradingSymbol', pos.get('symbol', '')))
                try:
                    result = self.exit_position(pos)
                    results.append({
                        'symbol': symbol,
                        'status': 'success' if result.success else 'failed',
                        'order_id': result.order_id,
                        'message': result.message
                    })
                except Exception as e:
                    results.append({
                        'symbol': symbol,
                        'status': 'failed',
                        'error': str(e)
                    })

        return results

    def validate_sl_price(self, sl_price: float, entry_price: float, ltp: float,
                          is_long: bool, min_distance: float = 5.0) -> Dict[str, Any]:
        """
        Validate SL price for correctness.

        Args:
            sl_price: Proposed SL trigger price
            entry_price: Position entry price
            ltp: Current Last Traded Price
            is_long: True if LONG position, False if SHORT
            min_distance: Minimum distance from LTP (default 5 points)

        Returns:
            Dict with 'valid' bool and 'error' message if invalid
        """
        # Check 1: SL must be on correct side of LTP (not entry!)
        # For LONG: SL triggers SELL when price DROPS to SL level → SL must be BELOW LTP
        # For SHORT: SL triggers BUY when price RISES to SL level → SL must be ABOVE LTP
        # NOTE: SL can be above/below entry for profitable positions (BE scenario)
        if is_long:
            if sl_price >= ltp:
                return {
                    'valid': False,
                    'reason': 'WRONG_DIRECTION',
                    'error': f'WRONG DIRECTION: LONG SL ({sl_price:.2f}) must be BELOW LTP ({ltp:.2f})'
                }
        else:
            if sl_price <= ltp:
                return {
                    'valid': False,
                    'reason': 'WRONG_DIRECTION',
                    'error': f'WRONG DIRECTION: SHORT SL ({sl_price:.2f}) must be ABOVE LTP ({ltp:.2f})'
                }

        # Check 2: SL must not be triggered immediately
        # Use percentage-based buffer for equity, fixed points for options
        # Options: use fixed buffer (ltp_buffer config, default 10 points)
        # Equity: use 0.3% of LTP as minimum safe buffer
        fixed_buffer = self.config.get('trailing_sl', {}).get('ltp_buffer', 10)

        # For equity (prices typically > 50), use percentage-based minimum
        # For options (can be any price), use fixed minimum
        if ltp > 50:
            # Equity: 0.3% of LTP as safety buffer (e.g., ₹1350 * 0.003 = 4.05 pts)
            safety_buffer = max(min_distance, ltp * 0.003)
        else:
            # Options: use fixed buffer
            safety_buffer = fixed_buffer

        distance = abs(ltp - sl_price)

        # Only warn if SL would be triggered immediately (within safety buffer)
        if is_long and sl_price >= (ltp - safety_buffer):
            return {
                'valid': False,
                'reason': 'IMMEDIATE_TRIGGER',
                'error': f'IMMEDIATE TRIGGER: LONG SL ({sl_price:.2f}) too close to LTP ({ltp:.2f}). Min safe distance: {safety_buffer:.2f} pts'
            }
        if not is_long and sl_price <= (ltp + safety_buffer):
            return {
                'valid': False,
                'reason': 'IMMEDIATE_TRIGGER',
                'error': f'IMMEDIATE TRIGGER: SHORT SL ({sl_price:.2f}) too close to LTP ({ltp:.2f}). Min safe distance: {safety_buffer:.2f} pts'
            }

        return {'valid': True, 'reason': None, 'error': None}

    def validate_target_price(self, target_price: float, entry_price: float,
                              is_long: bool) -> Dict[str, Any]:
        """
        Validate Target price for profitability.

        Args:
            target_price: Proposed target price
            entry_price: Position entry price
            is_long: True if LONG position, False if SHORT

        Returns:
            Dict with 'valid' bool and 'error' message if invalid
        """
        if is_long:
            if target_price <= entry_price:
                return {'valid': False, 'error': f'LONG Target ({target_price}) must be ABOVE entry ({entry_price})'}
        else:
            if target_price >= entry_price:
                return {'valid': False, 'error': f'SHORT Target ({target_price}) must be BELOW entry ({entry_price})'}

        return {'valid': True, 'error': None}

    def set_sl_target(self, position: Dict[str, Any], sl_price: float,
                      target_price: float = None, validate: bool = True) -> Dict[str, Any]:
        """
        Set SL and Target for existing position.
        Places two separate orders (no native OCO in NEO).

        Args:
            position: Position dict
            sl_price: Stop loss trigger price
            target_price: Target price (optional)
            validate: Whether to validate prices (default True)

        Returns:
            Dict with SL and Target order IDs
        """
        qty = abs(int(float(position.get('qty', 0) or 0)))
        if qty == 0:
            return {'success': False, 'error': 'No position quantity'}

        pos_qty = int(float(position.get('qty', 0) or 0))
        is_long = pos_qty > 0
        exit_type = 'S' if is_long else 'B'

        entry_price = float(position.get('averagePrice', position.get('avgPrc', 0)) or 0)
        ltp = float(position.get('ltp', position.get('lastPrice', entry_price)) or entry_price)

        result = {'sl_order_id': None, 'target_order_id': None}

        # Validate SL price
        if validate and entry_price > 0:
            min_distance = self.config.get('trailing_sl', {}).get('min_sl_distance', 5)
            sl_validation = self.validate_sl_price(sl_price, entry_price, ltp, is_long, min_distance)
            if not sl_validation['valid']:
                result['sl_error'] = sl_validation['error']
                logger.warning(f"SL validation failed: {sl_validation['error']}")
                return result

        # Validate Target price
        if validate and target_price and entry_price > 0:
            target_validation = self.validate_target_price(target_price, entry_price, is_long)
            if not target_validation['valid']:
                result['target_error'] = target_validation['error']
                logger.warning(f"Target validation failed: {target_validation['error']}")
                # Continue with SL placement even if target is invalid

        # NEO API uses 'trdSym' for trading symbol
        trading_symbol = position.get('trdSym', position.get('tradingSymbol', position.get('symbol', '')))

        # Place SL order with retry logic
        sl_result = self._place_sl_with_retry(
            exchange_segment=position.get('exchange_segment', position.get('exSeg', 'nse_fo')),
            product=position.get('product', position.get('prd', self.default_product)),
            quantity=qty,
            trading_symbol=trading_symbol,
            transaction_type=exit_type,
            trigger_price=sl_price,
            max_retries=3
        )
        result['sl_order_id'] = sl_result.get('order_id')
        if sl_result.get('error'):
            result['sl_error'] = sl_result['error']
            result['sl_critical'] = sl_result.get('critical', False)

        # Place Target order if provided and valid
        if target_price and 'target_error' not in result:
            try:
                exchange_seg = position.get('exchange_segment', position.get('exSeg', 'nse_fo'))
                # CRITICAL: Format target price to tick size
                formatted_target_price = format_price(target_price, exchange_seg)

                # S35: Generate unique tag for target order using tracker
                target_tag = self.order_tracker.generate_tag(OrderType.TARGET, trading_symbol)
                self.order_tracker.register_order(
                    tag=target_tag,
                    symbol=trading_symbol,
                    transaction_type=exit_type,
                    quantity=qty,
                    api_order_type="L",
                    price_type=OrderType.TARGET
                )

                logger.info(f"[TARGET] Placing: {trading_symbol} {exit_type} qty={qty} price={formatted_target_price}")

                target_response = self.client.place_order(
                    exchange_segment=exchange_seg,
                    product=position.get('product', position.get('prd', self.default_product)),
                    price=formatted_target_price,
                    order_type="L",
                    quantity=str(qty),
                    validity="DAY",
                    trading_symbol=trading_symbol,  # Use already extracted symbol
                    transaction_type=exit_type,
                    amo="NO",
                    tag=target_tag  # S35: Use tracker-generated tag
                )
                result['target_order_id'] = target_response.get('nOrdNo') if target_response else None
                if result['target_order_id']:
                    self.order_tracker.confirm_order(target_tag, result['target_order_id'], target_response)
                else:
                    self.order_tracker.mark_failed(target_tag, "No order ID returned")
                logger.info(f"[TARGET] Order placed: {result['target_order_id']} @ {formatted_target_price}")
            except Exception as e:
                logger.error(f"Target order failed: {e}")
                result['target_error'] = str(e)

        result['success'] = bool(result['sl_order_id'])
        return result

    def cancel_pending_orders(self) -> List[Dict[str, Any]]:
        """Cancel all pending orders."""
        results = []

        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []
        except Exception as e:
            return [{'status': 'error', 'error': str(e)}]

        for order in orders:
            status = order.get('ordSt', '').lower()
            if status in ['pending', 'open', 'trigger pending', 'after market order req received']:
                order_id = order.get('nOrdNo')
                if order_id:
                    result = self.cancel_order(order_id)
                    results.append({
                        'order_id': order_id,
                        'status': 'cancelled' if result.success else 'failed',
                        'message': result.message
                    })

        return results

    def get_positions(self) -> List[Dict[str, Any]]:
        """Get all positions (open and closed) with P&L."""
        try:
            response = self.client.positions()
            positions = response.get('data', []) if response else []
            return positions  # Return all positions, UI will filter
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    def get_orders(self) -> List[Dict[str, Any]]:
        """Get today's orders."""
        try:
            response = self.client.order_report()
            return response.get('data', []) if response else []
        except Exception as e:
            logger.error(f"Failed to get orders: {e}")
            return []

    def get_margin_required(self, params: OrderParams) -> Optional[float]:
        """Calculate margin required for an order."""
        try:
            # S27: Use explicit None check - price of 0 is valid for market orders
            response = self.client.margin_required(
                exchange_segment=params.exchange_segment,
                price=str(params.price) if params.price is not None else "0",
                order_type=params.order_type,
                product=params.product,
                quantity=str(params.quantity),
                instrument_token=params.instrument_token,
                transaction_type=params.transaction_type
            )

            if response and 'data' in response:
                return float(response['data'].get('totalMargin', 0))
            return None

        except Exception as e:
            logger.warning(f"Margin calculation failed: {e}")
            return None

    def get_available_margin(self) -> Optional[float]:
        """Get available margin from broker."""
        try:
            response = self.client.limits()
            if response and 'data' in response:
                data = response['data']
                # S26: Use explicit None checks - margin of 0 is valid (though rare)
                # NEO API returns limits in different formats - handle common fields
                available_cash = data.get('availableCash')
                net = data.get('net')
                cash_nested = data.get('available', {}).get('cash')

                if available_cash is not None:
                    return float(available_cash)
                elif net is not None:
                    return float(net)
                elif cash_nested is not None:
                    return float(cash_nested)
                return 0.0
            return None
        except Exception as e:
            logger.warning(f"Failed to get available margin: {e}")
            return None

    def _check_margin_for_order(self, params: OrderParams) -> Dict[str, Any]:
        """
        Check if sufficient margin is available for order.

        Returns:
            Dict with keys:
            - 'block': True if order should be blocked
            - 'warning': True if warning should be logged
            - 'message': Description of the issue
        """
        # Check if margin checking is enabled
        margin_config = self.config.get('risk_management', {})
        margin_check_enabled = margin_config.get('margin_check_enabled', True)
        margin_check_mode = margin_config.get('margin_check_mode', 'warn')  # 'warn' or 'block'

        if not margin_check_enabled:
            return {'block': False, 'warning': False}

        try:
            # Get required margin for this order
            required_margin = self.get_margin_required(params)
            if required_margin is None:
                # CRITICAL: Can't calculate margin - safer to block in 'block' mode
                if margin_check_mode == 'block':
                    return {
                        'block': True,
                        'warning': False,
                        'message': 'Could not calculate required margin - order blocked for safety'
                    }
                return {
                    'block': False,
                    'warning': True,
                    'message': 'Could not calculate required margin - proceeding with caution'
                }

            # Get available margin
            available_margin = self.get_available_margin()
            if available_margin is None:
                # CRITICAL: Can't check margin - safer to block in 'block' mode
                if margin_check_mode == 'block':
                    return {
                        'block': True,
                        'warning': False,
                        'message': 'Could not fetch available margin - order blocked for safety'
                    }
                return {
                    'block': False,
                    'warning': True,
                    'message': 'Could not fetch available margin - proceeding with caution'
                }

            # Check if sufficient
            if required_margin > available_margin:
                msg = f'Insufficient margin: Required ₹{required_margin:,.0f}, Available ₹{available_margin:,.0f}'
                if margin_check_mode == 'block':
                    return {'block': True, 'warning': False, 'message': msg}
                else:
                    return {'block': False, 'warning': True, 'message': msg}

            # Sufficient margin
            return {'block': False, 'warning': False}

        except Exception as e:
            logger.warning(f"Margin check error: {e}")
            return {
                'block': False,
                'warning': True,
                'message': f'Margin check error: {e}'
            }

    # Safety check methods

    def _is_duplicate_order(self, params: OrderParams) -> bool:
        """
        Check if same order was placed within time window.

        Allows legitimate scaling by:
        1. Checking if the order has 'SCALE' tag (bypass duplicate check)
        2. Only blocking if price is also the same (accidental double-click)
        3. Configurable via 'allow_scaling' setting
        """
        # Check if scaling is explicitly allowed
        allow_scaling = self.config.get('risk_management', {}).get('allow_scaling', True)
        if allow_scaling and hasattr(params, 'tag') and params.tag and 'SCALE' in str(params.tag).upper():
            return False  # Explicit scaling - allow

        now = time.time()

        with self._lock:
            for order_time, order_params in self.recent_orders:
                if now - order_time < self.duplicate_window:
                    # Check symbol, direction, AND quantity match
                    if (order_params.symbol == params.symbol and
                        order_params.transaction_type == params.transaction_type and
                        order_params.quantity == params.quantity):
                        # Also check price if available - different prices = intentional scaling
                        # S27: Use explicit None checks - price of 0 is valid
                        if allow_scaling:
                            old_price = getattr(order_params, 'price', None)
                            new_price = getattr(params, 'price', None)
                            if old_price is not None and new_price is not None:
                                try:
                                    if abs(float(old_price) - float(new_price)) > 0.5:
                                        # Different prices = likely intentional, allow
                                        return False
                                except (ValueError, TypeError):
                                    pass  # Can't compare, treat as duplicate
                        return True
            return False

    def _record_order(self, params: OrderParams):
        """Record order for duplicate prevention."""
        with self._lock:
            self.recent_orders.append((time.time(), params))
            # Cleanup old entries
            cutoff = time.time() - 60  # Keep last 60 seconds
            self.recent_orders = [(t, p) for t, p in self.recent_orders if t > cutoff]

    def _check_position_limits(self) -> bool:
        """Check if new order exceeds position limits."""
        try:
            positions = self.get_positions()
            open_count = len([p for p in positions if int(float(p.get('qty', 0) or 0)) != 0])
            return open_count < self.max_open_positions
        except Exception as e:
            # CRITICAL: Block trading if we can't verify position limits
            logger.error(f"Position limit check failed - BLOCKING: {e}")
            return False

    def _check_position_limits_for_entry(self, params: OrderParams) -> bool:
        """
        Check if order would exceed position limits.
        Intelligently detects if order is new position entry or exit.

        Logic:
        - If symbol has existing position and order is opposite direction: it's an exit (allow)
        - If symbol has existing position and order is same direction: it's scaling (check limit)
        - If symbol has no position: it's a new entry (check limit)
        """
        try:
            positions = self.get_positions()
            open_positions = [p for p in positions if int(float(p.get('qty', 0) or 0)) != 0]
            open_count = len(open_positions)

            # Find if we have existing position in this symbol
            existing_pos = None
            for p in open_positions:
                sym = p.get('tradingSymbol', p.get('symbol', ''))
                if sym == params.symbol:
                    existing_pos = p
                    break

            if existing_pos:
                existing_qty = int(float(existing_pos.get('qty', 0) or 0))
                is_existing_long = existing_qty > 0

                # Check if this order is an exit (opposite direction)
                if params.transaction_type == 'B' and not is_existing_long:
                    # BUY to cover SHORT = exit, always allow
                    return True
                elif params.transaction_type == 'S' and is_existing_long:
                    # SELL to close LONG = exit, always allow
                    return True
                # Same direction = scaling into position, check limit
                # (we're not adding a new position, so allow)
                return True
            else:
                # No existing position - this is a NEW position entry
                # Check if we have room for one more
                return open_count < self.max_open_positions

        except Exception as e:
            # CRITICAL: Block trading if we can't verify position limits
            logger.error(f"Position limit check failed - BLOCKING: {e}")
            return False

    def _check_daily_loss_limit(self) -> bool:
        """
        Check if daily loss limit is breached.
        Tracks BOTH open position P&L AND realized P&L from closed trades.
        """
        try:
            # Part 1: Get P&L from open positions
            positions = self.get_positions()
            open_pnl = 0.0
            for pos in positions:
                # S28: Fix P&L cascade - explicit None checks to handle break-even (0) correctly
                pnl_value = pos.get('pnl')
                if pnl_value is None:
                    pnl_value = pos.get('dayPnl')
                if pnl_value is None:
                    pnl_value = pos.get('unrealizedPnl')
                try:
                    pnl = float(pnl_value) if pnl_value is not None else 0.0
                except (ValueError, TypeError):
                    pnl = 0.0
                open_pnl += pnl

            # Part 2: Get realized P&L from today's closed trades
            realized_pnl = self._get_realized_pnl_today()

            # Total daily P&L = open + realized
            total_pnl = open_pnl + realized_pnl
            self._daily_pnl = total_pnl

            logger.debug(f"Daily P&L check: Open={open_pnl:.2f}, Realized={realized_pnl:.2f}, Total={total_pnl:.2f}")

            if total_pnl < -self.max_loss_per_day:
                self._set_trading_halted(True, f"Daily loss limit: {total_pnl:.2f} < -{self.max_loss_per_day}")
                logger.warning(f"DAILY LOSS LIMIT REACHED: {total_pnl:.2f} (limit: -{self.max_loss_per_day})")
                return False

            return True

        except Exception as e:
            # CRITICAL: Block trading if we can't verify loss limit
            logger.error(f"Loss limit check failed - BLOCKING: {e}")
            self._set_trading_halted(True, f"Loss limit check failed: {e}")
            return False

    def _set_trading_halted(self, halted: bool, reason: str = ""):
        """Set trading halted state and persist to disk."""
        self._trading_halted = halted
        self._save_circuit_breaker_state(halted, reason)

    def _save_circuit_breaker_state(self, halted: bool, reason: str):
        """Save circuit breaker state to disk for persistence across restarts."""
        try:
            import json
            import os
            from datetime import datetime

            os.makedirs(os.path.dirname(self._circuit_breaker_file), exist_ok=True)

            state = {
                'halted': halted,
                'reason': reason,
                'timestamp': datetime.now().isoformat(),
                'date': datetime.now().strftime('%Y-%m-%d')
            }

            with open(self._circuit_breaker_file, 'w') as f:
                json.dump(state, f, indent=2)

            logger.info(f"[CIRCUIT] State saved: halted={halted}, reason={reason}")

        except Exception as e:
            logger.error(f"Failed to save circuit breaker state: {e}")

    def _load_circuit_breaker_state(self):
        """Load circuit breaker state from disk. Only applies if same day."""
        try:
            import json
            import os
            from datetime import datetime

            if not os.path.exists(self._circuit_breaker_file):
                return

            with open(self._circuit_breaker_file, 'r') as f:
                state = json.load(f)

            saved_date = state.get('date', '')
            today = datetime.now().strftime('%Y-%m-%d')

            # Only restore if same day - new day = fresh start
            if saved_date == today and state.get('halted', False):
                self._trading_halted = True
                logger.warning(f"[CIRCUIT] Restored halted state from disk: {state.get('reason', 'Unknown')}")
            elif saved_date != today:
                # Clear old state file on new day
                os.remove(self._circuit_breaker_file)
                logger.info("[CIRCUIT] New trading day - cleared old circuit breaker state")

        except Exception as e:
            logger.warning(f"Failed to load circuit breaker state: {e}")

    def _get_realized_pnl_today(self) -> float:
        """
        Get realized P&L from today's completed trades.

        Uses RealizedPnLTracker if available (accurate).
        Falls back to broker API (less reliable).
        """
        # PRIMARY: Use our tracker (accurate, tracks entry prices)
        if self.pnl_tracker:
            return self.pnl_tracker.get_realized_pnl_today()

        # FALLBACK: Try to get from broker positions API (less accurate)
        # NEO positions() sometimes includes realized P&L in dayPnl field
        try:
            positions_response = self.client.positions()
            positions = positions_response.get('data', []) if positions_response else []

            realized_pnl = 0.0
            for pos in positions:
                # Look for closed positions (qty = 0 but had trades)
                qty = int(float(pos.get('qty', 0) or 0))
                if qty == 0:
                    # Closed position - dayPnl is realized
                    day_pnl = float(pos.get('dayPnl', 0) or
                                   pos.get('pnl', 0) or
                                   pos.get('realizedPnl', 0) or 0)
                    realized_pnl += day_pnl

            return realized_pnl

        except Exception as e:
            logger.warning(f"Could not get realized P&L: {e}")
            return 0.0

    def set_pnl_tracker(self, tracker):
        """Set the realized P&L tracker (can be set after initialization)."""
        self.pnl_tracker = tracker
        logger.info("[ORDER] Realized P&L tracker attached")

    def reset_daily_limits(self):
        """Reset daily limits (call at start of day)."""
        self._daily_pnl = 0.0
        self._trading_halted = False
        self._last_reset_date = datetime.now().date()
        with self._lock:
            self.recent_orders.clear()
        logger.info("Daily limits reset")

    def check_and_reset_if_new_day(self):
        """Auto-reset daily limits if it's a new trading day."""
        today = datetime.now().date()
        if today > self._last_reset_date:
            logger.info(f"New trading day detected: {today}")
            self.reset_daily_limits()

    def is_trading_halted(self) -> bool:
        """Check if trading is halted."""
        return self._trading_halted

    def get_daily_pnl(self) -> float:
        """Get current daily P&L."""
        return self._daily_pnl

    def force_halt_trading(self, reason: str = "Manual halt"):
        """Force halt trading immediately."""
        self._trading_halted = True
        logger.warning(f"Trading HALTED: {reason}")

    def resume_trading(self):
        """Resume trading after manual halt (use with caution)."""
        # S23-H1: Refresh P&L before checking (prevents stale value after app restart)
        self._check_daily_loss_limit()
        if self._daily_pnl < -self.max_loss_per_day:
            logger.warning(f"Cannot resume - daily loss limit still breached (P&L: {self._daily_pnl:.2f})")
            return False
        self._trading_halted = False
        logger.info("Trading resumed")
        return True

    # ==================== FIX #3, #4, #5: PARTIAL FILL & SL RETRY ====================

    def _place_sl_with_retry(self, exchange_segment: str, product: str,
                              quantity: int, trading_symbol: str,
                              transaction_type: str, trigger_price: float,
                              max_retries: int = 3) -> Dict[str, Any]:
        """
        Place SL order with retry logic and exponential backoff.

        CRITICAL: If SL fails after all retries, position is UNPROTECTED.
        Returns critical=True flag for GUI to alert user.

        Args:
            exchange_segment: Exchange segment
            product: Product type (MIS/NRML)
            quantity: Order quantity
            trading_symbol: Trading symbol
            transaction_type: B or S
            trigger_price: SL trigger price
            max_retries: Maximum retry attempts

        Returns:
            Dict with 'order_id', 'error', 'critical' flags
        """
        last_error = None

        # CRITICAL: ALL OPTIONS (nse_fo, bse_fo) do NOT allow SL-M orders - must use SL-L
        # S29: Kotak NEO rejects SL-M for ALL options, not just BSE
        # S31: Fixed floating-point precision bug using tick_utils
        sl_order_type, sl_limit_price = calculate_sl_limit_price(
            trigger_price=trigger_price,
            transaction_type=transaction_type,
            exchange_segment=exchange_segment,
            buffer_percent=0.5
        )
        if sl_order_type == "SL":
            logger.info(f"[SL] Options detected - using SL-L: trigger={trigger_price}, limit={sl_limit_price}")

        for attempt in range(max_retries):
            try:
                # S35: Generate unique tag for SL order using tracker
                sl_tag = self.order_tracker.generate_tag(OrderType.SL, trading_symbol)
                self.order_tracker.register_order(
                    tag=sl_tag,
                    symbol=trading_symbol,
                    transaction_type=transaction_type,
                    quantity=quantity,
                    api_order_type=sl_order_type,
                    price_type=OrderType.SL
                )

                sl_response = self.client.place_order(
                    exchange_segment=exchange_segment,
                    product=product,
                    price=sl_limit_price,
                    order_type=sl_order_type,
                    quantity=str(quantity),
                    validity="DAY",
                    trading_symbol=trading_symbol,
                    transaction_type=transaction_type,
                    amo="NO",
                    trigger_price=round_trigger_price(trigger_price, exchange_segment),
                    tag=sl_tag  # S35: Use tracker-generated tag
                )

                order_id = sl_response.get('nOrdNo') if sl_response else None
                if order_id:
                    # S35: Confirm order with tracker
                    self.order_tracker.confirm_order(sl_tag, order_id, sl_response)
                    logger.info(f"SL order placed: {order_id} @ {trigger_price} (attempt {attempt + 1})")
                    return {'order_id': order_id, 'error': None, 'critical': False, 'tag': sl_tag}
                else:
                    last_error = "No order ID returned"
                    self.order_tracker.mark_failed(sl_tag, last_error)
                    logger.warning(f"SL attempt {attempt + 1}: {last_error}")

            except Exception as e:
                last_error = str(e)
                self.order_tracker.mark_failed(sl_tag, str(e))
                logger.warning(f"SL attempt {attempt + 1} failed: {e}")

            # Exponential backoff: 0.5s, 1s, 2s
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * 0.5
                time.sleep(wait_time)

        # All retries failed - CRITICAL: position is unprotected
        logger.error(f"CRITICAL: SL placement failed after {max_retries} attempts for {trading_symbol}!")
        return {
            'order_id': None,
            'error': f'SL FAILED after {max_retries} attempts: {last_error}',
            'critical': True,
            'tag': sl_tag  # Return tag for debugging/tracking
        }

    def handle_partial_fill(self, entry_order_id: str, sl_order_id: str,
                            sl_price: float, position_tracker=None) -> Dict[str, Any]:
        """
        Handle partial fill of entry order by adjusting SL quantity.

        When entry order is partially filled:
        1. Check filled quantity vs SL order quantity
        2. If mismatch, modify SL order to match filled quantity
        3. If modification fails, cancel and recreate SL

        Args:
            entry_order_id: Entry order ID to check
            sl_order_id: Existing SL order ID
            sl_price: Current SL price (for recreation if needed)
            position_tracker: Optional PositionTracker instance

        Returns:
            Dict with 'success', 'action', 'new_sl_order_id', 'filled_qty'
        """
        result = {
            'success': False,
            'action': 'none',
            'new_sl_order_id': sl_order_id,
            'filled_qty': 0,
            'pending_qty': 0
        }

        try:
            # Get entry order status
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            entry_order = None
            sl_order = None

            for o in orders:
                if o.get('nOrdNo') == entry_order_id:
                    entry_order = o
                if o.get('nOrdNo') == sl_order_id:
                    sl_order = o

            if not entry_order:
                result['error'] = 'Entry order not found'
                return result

            # Get quantities
            filled_qty = int(entry_order.get('fldQty', 0) or 0)
            total_qty = int(entry_order.get('qty', 0) or 0)
            pending_qty = total_qty - filled_qty

            result['filled_qty'] = filled_qty
            result['pending_qty'] = pending_qty

            # Check if partial fill (some filled, some pending)
            entry_status = entry_order.get('ordSt', '').lower()
            if entry_status not in ['partial', 'partially filled'] and filled_qty == total_qty:
                result['action'] = 'complete'
                result['success'] = True
                return result

            if filled_qty == 0:
                result['action'] = 'no_fill'
                return result

            # We have a partial fill - check SL quantity
            if not sl_order:
                # No SL order exists - create one for filled qty
                logger.warning(f"No SL order found for partial fill, creating new SL")
                result['action'] = 'no_sl_found'
                return result

            sl_qty = int(sl_order.get('qty', 0) or 0)
            sl_status = sl_order.get('ordSt', '').lower()

            # If SL qty matches filled qty, we're good
            if sl_qty == filled_qty:
                result['action'] = 'already_synced'
                result['success'] = True
                return result

            # SL qty needs adjustment
            logger.info(f"Partial fill detected: filled={filled_qty}, SL qty={sl_qty}, adjusting...")

            # Only modify if SL is still pending
            if sl_status not in ['pending', 'open', 'trigger pending', 'after market order req received']:
                result['action'] = 'sl_not_pending'
                result['error'] = f'SL order status is {sl_status}, cannot modify'
                return result

            # Try to modify SL quantity
            try:
                # Get SL order details for order_type (required by NEO API)
                sl_order_type = sl_order.get('prcTp', sl_order.get('orderType', 'SL'))
                # CRITICAL FIX (S25): Use different name to avoid shadowing sl_price parameter
                sl_order_limit_price = sl_order.get('prc', sl_order.get('price', '0'))
                sl_trigger = sl_order.get('trgPrc', sl_order.get('triggerPrice', '0'))

                self.client.modify_order(
                    order_id=sl_order_id,
                    order_type=sl_order_type,
                    price=str(sl_order_limit_price),
                    quantity=str(filled_qty),
                    trigger_price=str(sl_trigger),
                    validity="DAY"
                )
                logger.info(f"SL qty modified: {sl_qty} -> {filled_qty}")
                result['action'] = 'modified'
                result['success'] = True
                return result

            except Exception as mod_e:
                logger.warning(f"SL modify failed: {mod_e}, attempting cancel + recreate")

                # Fallback: Cancel and recreate
                try:
                    self.client.cancel_order(order_id=sl_order_id)
                    logger.info(f"Cancelled old SL: {sl_order_id}")

                    # Get position details for recreation
                    symbol = sl_order.get('trdSym', entry_order.get('trdSym', ''))
                    exchange_seg = sl_order.get('exSeg', entry_order.get('exSeg', 'nse_fo'))
                    product = sl_order.get('prod', entry_order.get('prod', 'MIS'))
                    txn_type = sl_order.get('trnsTp', 'S')

                    # Recreate SL with correct quantity
                    new_sl = self._place_sl_with_retry(
                        exchange_segment=exchange_seg,
                        product=product,
                        quantity=filled_qty,
                        trading_symbol=symbol,
                        transaction_type=txn_type,
                        trigger_price=sl_price,
                        max_retries=2
                    )

                    if new_sl.get('order_id'):
                        result['action'] = 'recreated'
                        result['new_sl_order_id'] = new_sl['order_id']
                        result['success'] = True

                        # Update position tracker if provided (use symbol-based method)
                        if position_tracker:
                            position_tracker.update_sl_order_by_symbol(symbol, new_sl_order_id=new_sl['order_id'], new_sl_price=sl_price)

                    else:
                        result['action'] = 'recreate_failed'
                        result['error'] = new_sl.get('error')
                        result['critical'] = True

                except Exception as cancel_e:
                    result['action'] = 'cancel_failed'
                    result['error'] = f'Modify and cancel both failed: {cancel_e}'
                    result['critical'] = True

                return result

        except Exception as e:
            result['error'] = str(e)
            logger.error(f"Partial fill handling error: {e}")
            return result

    def adjust_sl_after_partial_exit(self, symbol: str, remaining_qty: int,
                                      sl_order_id: str, sl_price: float,
                                      exchange_segment: str = 'nse_fo',
                                      product: str = 'MIS',
                                      transaction_type: str = 'S',
                                      position_tracker=None,
                                      oco_monitor=None) -> Dict[str, Any]:
        """
        Adjust SL order quantity after partial position exit.

        When user exits 50% of position, the SL order quantity must be
        reduced to match remaining position quantity.

        Args:
            symbol: Trading symbol
            remaining_qty: Remaining position quantity after partial exit
            sl_order_id: Current SL order ID
            sl_price: Current SL price
            exchange_segment: Exchange segment
            product: Product type
            transaction_type: Transaction type for SL (opposite of position)
            position_tracker: Optional PositionTracker instance
            oco_monitor: Optional OCOMonitor instance

        Returns:
            Dict with 'success', 'action', 'new_sl_order_id'
        """
        result = {
            'success': False,
            'action': 'none',
            'new_sl_order_id': sl_order_id
        }

        if remaining_qty <= 0:
            # Position fully closed - cancel SL
            try:
                self.client.cancel_order(order_id=sl_order_id)
                logger.info(f"Position closed, cancelled SL: {sl_order_id}")
                result['action'] = 'cancelled'
                result['success'] = True
                result['new_sl_order_id'] = None

                # Update trackers (use symbol-based methods)
                if position_tracker:
                    position_tracker.cancel_sl_order_by_symbol(symbol)
                if oco_monitor:
                    oco_monitor.remove_pairs_by_symbol(symbol)

            except Exception as e:
                result['error'] = f'Failed to cancel SL: {e}'
                logger.error(result['error'])

            return result

        # Try to modify SL quantity
        try:
            # Fetch SL order to get order_type (required by NEO API)
            orders = self.get_orders()
            sl_order = None
            for order in orders:
                if order.get('nOrdNo') == sl_order_id:
                    sl_order = order
                    break

            if not sl_order:
                logger.warning(f"SL order {sl_order_id} not found in order book, cannot modify")
                raise Exception(f"SL order {sl_order_id} not found")

            sl_order_type = sl_order.get('prcTp', sl_order.get('orderType', 'SL'))
            sl_order_price = sl_order.get('prc', sl_order.get('price', '0'))
            sl_trigger_price = sl_order.get('trgPrc', sl_order.get('triggerPrice', str(sl_price)))

            self.client.modify_order(
                order_id=sl_order_id,
                order_type=sl_order_type,
                price=str(sl_order_price),
                quantity=str(remaining_qty),
                trigger_price=str(sl_trigger_price),
                validity="DAY"
            )
            logger.info(f"SL qty adjusted for {symbol}: -> {remaining_qty}")
            result['action'] = 'modified'
            result['success'] = True
            return result

        except Exception as mod_e:
            logger.warning(f"SL modify failed for {symbol}: {mod_e}, attempting cancel + recreate")

            # CRITICAL: Track state for recovery
            cancel_succeeded = False
            cancelled_sl_id = sl_order_id

            # Fallback: Cancel and recreate
            try:
                self.client.cancel_order(order_id=sl_order_id)
                cancel_succeeded = True
                logger.info(f"Cancelled old SL for qty adjust: {sl_order_id}")

                # Recreate SL with correct quantity - MORE retries for critical operation
                new_sl = self._place_sl_with_retry(
                    exchange_segment=exchange_segment,
                    product=product,
                    quantity=remaining_qty,
                    trading_symbol=symbol,
                    transaction_type=transaction_type,
                    trigger_price=sl_price,
                    max_retries=3  # Increased retries for critical operation
                )

                if new_sl.get('order_id'):
                    result['action'] = 'recreated'
                    result['new_sl_order_id'] = new_sl['order_id']
                    result['success'] = True

                    # Update trackers (use symbol-based methods)
                    if position_tracker:
                        position_tracker.update_sl_order_by_symbol(symbol, new_sl_order_id=new_sl['order_id'], new_sl_price=sl_price)
                    if oco_monitor:
                        oco_monitor.update_sl_order_by_symbol(symbol, new_sl['order_id'], sl_price)

                    logger.info(f"SL recreated for {symbol}: {new_sl['order_id']} qty={remaining_qty}")

                else:
                    # CRITICAL: Cancel succeeded but recreate failed - NAKED POSITION!
                    result['action'] = 'recreate_failed'
                    result['error'] = new_sl.get('error')
                    result['critical'] = True
                    result['cancelled_sl_id'] = cancelled_sl_id  # Track for recovery
                    result['remaining_qty'] = remaining_qty
                    result['sl_price'] = sl_price
                    logger.error(f"CRITICAL: Failed to recreate SL for {symbol}! "
                               f"Position has {remaining_qty} qty with NO SL protection!")

                    # CRITICAL: Notify immediately
                    # S17-L2: telegram always initialized in __init__
                    if self.telegram:
                        self.telegram.send(
                            f"🚨 CRITICAL: SL FAILED for {symbol}!\n"
                            f"Old SL cancelled but new SL failed!\n"
                            f"Position qty: {remaining_qty}\n"
                            f"Expected SL: {sl_price}\n"
                            f"MANUAL INTERVENTION REQUIRED!"
                        )

            except Exception as cancel_e:
                if cancel_succeeded:
                    # Cancel worked but something else failed
                    result['action'] = 'recreate_failed'
                    result['critical'] = True
                    result['cancelled_sl_id'] = cancelled_sl_id
                    result['remaining_qty'] = remaining_qty
                    result['sl_price'] = sl_price
                else:
                    # Cancel failed - SL might still be active (less critical)
                    result['action'] = 'cancel_failed'
                    result['critical'] = False  # SL might still be protecting

                result['error'] = f'SL adjustment exception: {cancel_e}'
                logger.error(f"CRITICAL: SL adjustment failed for {symbol}: {cancel_e}")

            return result

    def get_order_fill_status(self, order_id: str) -> Dict[str, Any]:
        """
        Get fill status for an order.

        Returns:
            Dict with 'status', 'filled_qty', 'pending_qty', 'total_qty', 'avg_price'
        """
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []

            for o in orders:
                if o.get('nOrdNo') == order_id:
                    filled_qty = int(o.get('fldQty', 0) or 0)
                    total_qty = int(o.get('qty', 0) or 0)
                    status = o.get('ordSt', '').lower()

                    return {
                        'found': True,
                        'status': status,
                        'filled_qty': filled_qty,
                        'pending_qty': total_qty - filled_qty,
                        'total_qty': total_qty,
                        'avg_price': float(o.get('avgPrc', 0) or 0),
                        'is_complete': status in ['complete', 'traded', 'filled'],
                        'is_partial': status in ['partial', 'partially filled'] or (filled_qty > 0 and filled_qty < total_qty)
                    }

            return {'found': False, 'error': 'Order not found'}

        except Exception as e:
            return {'found': False, 'error': str(e)}

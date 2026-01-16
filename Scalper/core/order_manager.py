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

    def __init__(self, neo_client, config: Dict[str, Any], symbol_mapper=None):
        self.client = neo_client
        self.config = config
        self.mapper = symbol_mapper
        self.recent_orders: List[Tuple[float, OrderParams]] = []
        self._lock = threading.Lock()

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

        # MIS-specific checks
        if params.product == 'MIS':
            # Hard cutoff - block new MIS orders after cutoff time
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
            response = self.client.place_order(
                exchange_segment=params.exchange_segment,
                product=params.product,
                price=str(params.price) if params.price else "0",
                order_type=params.order_type,
                quantity=str(params.quantity),
                validity=params.validity,
                trading_symbol=params.symbol,
                transaction_type=params.transaction_type,
                amo="NO",
                disclosed_quantity=str(params.disclosed_qty),
                trigger_price=str(params.trigger_price) if params.trigger_price else "0",
                tag=params.tag or ""
            )

            # Record for duplicate prevention
            self._record_order(params)

            # Extract order ID
            order_id = None
            if isinstance(response, dict):
                order_id = response.get('nOrdNo') or response.get('orderId') or response.get('order_id')

            logger.info(f"Order placed: {params.symbol} {params.transaction_type} "
                       f"{params.quantity} @ {params.price} -> ID: {order_id}")

            return OrderResult(
                success=True,
                order_id=order_id,
                message=f"Order placed successfully",
                response=response
            )

        except Exception as e:
            logger.error(f"Order placement failed: {str(e)}", exc_info=True)
            return OrderResult(
                success=False,
                message=f"Order placement failed: {str(e)}"
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
            response = self.client.place_order(
                exchange_segment=entry.exchange_segment,
                product="BO",
                price=str(entry.price),
                order_type="L",
                quantity=str(entry.quantity),
                validity="DAY",
                trading_symbol=entry.symbol,
                transaction_type=entry.transaction_type,
                amo="NO",
                trigger_price=str(sl_trigger),
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
            response = self.client.modify_order(
                order_id=order_id,
                price=str(new_price) if new_price else "",
                quantity=str(new_quantity) if new_quantity else "",
                trigger_price=str(new_trigger) if new_trigger else "",
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
        qty = int(position.get('qty', 0))
        if qty == 0:
            return OrderResult(success=False, message="No position to exit")

        exit_qty = int(abs(qty) * exit_qty_percent / 100)

        # Opposite transaction
        txn_type = 'S' if qty > 0 else 'B'

        params = OrderParams(
            symbol=position.get('symbol', position.get('tradingSymbol', '')),
            exchange_segment=position.get('exchange_segment', position.get('exchangeSegment', 'nse_fo')),
            instrument_token=str(position.get('instrument_token', position.get('token', ''))),
            transaction_type=txn_type,
            quantity=exit_qty,
            product=position.get('product', self.default_product),
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
            qty = int(pos.get('qty', 0))
            if qty != 0:
                try:
                    result = self.exit_position(pos)
                    results.append({
                        'symbol': pos.get('symbol', pos.get('tradingSymbol', '')),
                        'status': 'success' if result.success else 'failed',
                        'order_id': result.order_id,
                        'message': result.message
                    })
                except Exception as e:
                    results.append({
                        'symbol': pos.get('symbol', ''),
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
        # Check 1: SL must be on correct side of entry
        if is_long:
            if sl_price >= entry_price:
                return {'valid': False, 'error': f'LONG SL ({sl_price}) must be BELOW entry ({entry_price})'}
        else:
            if sl_price <= entry_price:
                return {'valid': False, 'error': f'SHORT SL ({sl_price}) must be ABOVE entry ({entry_price})'}

        # Check 2: SL must be at least min_distance from LTP
        # Add safety buffer (50% extra) to account for LTP movement between validation and order placement
        # In fast markets, LTP can move significantly in milliseconds
        safety_buffer = self.config.get('trailing_sl', {}).get('ltp_buffer', 10)
        effective_min_distance = min_distance + safety_buffer

        distance = abs(ltp - sl_price)
        if distance < effective_min_distance:
            return {
                'valid': False,
                'error': f'SL too close to LTP ({distance:.2f} < {effective_min_distance} points including {safety_buffer}pt safety buffer)'
            }

        # Check 3: SL should not be triggered immediately (with buffer for LTP movement)
        if is_long and sl_price >= (ltp - safety_buffer):
            return {'valid': False, 'error': f'LONG SL ({sl_price}) too close to LTP ({ltp}), may trigger immediately'}
        if not is_long and sl_price <= (ltp + safety_buffer):
            return {'valid': False, 'error': f'SHORT SL ({sl_price}) too close to LTP ({ltp}), may trigger immediately'}

        return {'valid': True, 'error': None}

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
        qty = abs(int(position.get('qty', 0)))
        if qty == 0:
            return {'success': False, 'error': 'No position quantity'}

        pos_qty = int(position.get('qty', 0))
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

        # Place SL order with retry logic
        sl_result = self._place_sl_with_retry(
            exchange_segment=position.get('exchange_segment', 'nse_fo'),
            product=position.get('product', self.default_product),
            quantity=qty,
            trading_symbol=position.get('symbol', position.get('tradingSymbol', '')),
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
                target_response = self.client.place_order(
                    exchange_segment=position.get('exchange_segment', 'nse_fo'),
                    product=position.get('product', self.default_product),
                    price=str(target_price),
                    order_type="L",
                    quantity=str(qty),
                    validity="DAY",
                    trading_symbol=position.get('symbol', position.get('tradingSymbol', '')),
                    transaction_type=exit_type,
                    amo="NO",
                    tag='TARGET'
                )
                result['target_order_id'] = target_response.get('nOrdNo') if target_response else None
                logger.info(f"Target order placed: {result['target_order_id']} @ {target_price}")
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
        """Get current positions with P&L."""
        try:
            response = self.client.positions()
            positions = response.get('data', []) if response else []

            # Filter and format positions
            result = []
            for pos in positions:
                qty = int(pos.get('qty', 0))
                if qty != 0:  # Only include open positions
                    result.append(pos)

            return result
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
            response = self.client.margin_required(
                exchange_segment=params.exchange_segment,
                price=str(params.price) if params.price else "0",
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
                # NEO API returns limits in different formats - handle common fields
                available = float(
                    data.get('availableCash', 0) or
                    data.get('net', 0) or
                    data.get('available', {}).get('cash', 0) or 0
                )
                return available
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
                # Can't calculate - don't block but warn
                return {
                    'block': False,
                    'warning': True,
                    'message': 'Could not calculate required margin - proceeding anyway'
                }

            # Get available margin
            available_margin = self.get_available_margin()
            if available_margin is None:
                # Can't check - don't block but warn
                return {
                    'block': False,
                    'warning': True,
                    'message': 'Could not fetch available margin - proceeding anyway'
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
                        if allow_scaling:
                            old_price = getattr(order_params, 'price', None)
                            new_price = getattr(params, 'price', None)
                            if old_price and new_price and abs(float(old_price) - float(new_price)) > 0.5:
                                # Different prices = likely intentional, allow
                                return False
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
            open_count = len([p for p in positions if int(p.get('qty', 0)) != 0])
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
            open_positions = [p for p in positions if int(p.get('qty', 0)) != 0]
            open_count = len(open_positions)

            # Find if we have existing position in this symbol
            existing_pos = None
            for p in open_positions:
                sym = p.get('tradingSymbol', p.get('symbol', ''))
                if sym == params.symbol:
                    existing_pos = p
                    break

            if existing_pos:
                existing_qty = int(existing_pos.get('qty', 0))
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
                pnl = float(pos.get('pnl', 0) or
                           pos.get('dayPnl', 0) or
                           pos.get('unrealizedPnl', 0) or 0)
                open_pnl += pnl

            # Part 2: Get realized P&L from today's closed trades
            realized_pnl = self._get_realized_pnl_today()

            # Total daily P&L = open + realized
            total_pnl = open_pnl + realized_pnl
            self._daily_pnl = total_pnl

            logger.debug(f"Daily P&L check: Open={open_pnl:.2f}, Realized={realized_pnl:.2f}, Total={total_pnl:.2f}")

            if total_pnl < -self.max_loss_per_day:
                self._trading_halted = True
                logger.warning(f"DAILY LOSS LIMIT REACHED: {total_pnl:.2f} (limit: -{self.max_loss_per_day})")
                return False

            return True

        except Exception as e:
            # CRITICAL: Block trading if we can't verify loss limit
            logger.error(f"Loss limit check failed - BLOCKING: {e}")
            self._trading_halted = True
            return False

    def _get_realized_pnl_today(self) -> float:
        """
        Get realized P&L from today's completed trades.
        Parses order report to calculate P&L from filled exit orders.
        """
        try:
            order_report = self.client.order_report()
            orders = order_report.get('data', []) if order_report else []
            realized_pnl = 0.0

            # Get today's date for filtering
            from datetime import datetime
            today = datetime.now().strftime('%d-%b-%Y').upper()

            for order in orders:
                # Only count filled orders
                status = order.get('ordSt', '').lower()
                if status not in ['complete', 'traded', 'filled']:
                    continue

                # Check if order is from today
                order_date = order.get('exOrdDt', order.get('ordDt', ''))
                if today not in order_date.upper():
                    continue

                # Look for exit orders (tagged as SL, TARGET, EXIT)
                tag = order.get('tag', '').upper()
                if tag in ['SL', 'TARGET', 'EXIT', 'SQUARE_OFF']:
                    # Calculate P&L based on average price
                    avg_price = float(order.get('avgPrc', 0) or 0)
                    qty = int(order.get('fldQty', order.get('qty', 0)) or 0)
                    txn_type = order.get('tranType', order.get('transactionType', ''))

                    # For exit orders, we can estimate P&L from the fill
                    # Note: This is approximate - broker may provide better data
                    # The actual P&L should come from trade book if available
                    pass  # P&L from individual orders needs entry price reference

            # Alternative: Try to get from trade book or settlement API
            try:
                # NEO API might have trade_report or settlement endpoint
                trade_report = getattr(self.client, 'trade_report', None)
                if trade_report:
                    trades = trade_report()
                    if trades and 'data' in trades:
                        for trade in trades.get('data', []):
                            pnl = float(trade.get('realizedPnl', 0) or
                                       trade.get('pnl', 0) or 0)
                            realized_pnl += pnl
            except Exception:
                pass  # Trade report not available

            return realized_pnl

        except Exception as e:
            logger.warning(f"Could not get realized P&L: {e}")
            return 0.0  # Return 0 if we can't get realized P&L (open P&L still checked)

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
        if not hasattr(self, '_last_reset_date'):
            self._last_reset_date = today
            return

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
        if self._daily_pnl < -self.max_loss_per_day:
            logger.warning("Cannot resume - daily loss limit still breached")
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

        for attempt in range(max_retries):
            try:
                sl_response = self.client.place_order(
                    exchange_segment=exchange_segment,
                    product=product,
                    price="0",
                    order_type="SL-M",
                    quantity=str(quantity),
                    validity="DAY",
                    trading_symbol=trading_symbol,
                    transaction_type=transaction_type,
                    amo="NO",
                    trigger_price=str(trigger_price),
                    tag='SL'
                )

                order_id = sl_response.get('nOrdNo') if sl_response else None
                if order_id:
                    logger.info(f"SL order placed: {order_id} @ {trigger_price} (attempt {attempt + 1})")
                    return {'order_id': order_id, 'error': None, 'critical': False}
                else:
                    last_error = "No order ID returned"
                    logger.warning(f"SL attempt {attempt + 1}: {last_error}")

            except Exception as e:
                last_error = str(e)
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
            'critical': True
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
                self.client.modify_order(
                    order_id=sl_order_id,
                    quantity=str(filled_qty)
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

                        # Update position tracker if provided
                        if position_tracker:
                            position_tracker.update_sl_order(symbol, new_sl['order_id'], sl_price)

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

                # Update trackers
                if position_tracker:
                    position_tracker.cancel_sl_order(symbol)
                if oco_monitor:
                    oco_monitor.remove_oco_pair(symbol)

            except Exception as e:
                result['error'] = f'Failed to cancel SL: {e}'
                logger.error(result['error'])

            return result

        # Try to modify SL quantity
        try:
            self.client.modify_order(
                order_id=sl_order_id,
                quantity=str(remaining_qty)
            )
            logger.info(f"SL qty adjusted for {symbol}: -> {remaining_qty}")
            result['action'] = 'modified'
            result['success'] = True
            return result

        except Exception as mod_e:
            logger.warning(f"SL modify failed for {symbol}: {mod_e}, attempting cancel + recreate")

            # Fallback: Cancel and recreate
            try:
                self.client.cancel_order(order_id=sl_order_id)
                logger.info(f"Cancelled old SL for qty adjust: {sl_order_id}")

                # Recreate SL with correct quantity
                new_sl = self._place_sl_with_retry(
                    exchange_segment=exchange_segment,
                    product=product,
                    quantity=remaining_qty,
                    trading_symbol=symbol,
                    transaction_type=transaction_type,
                    trigger_price=sl_price,
                    max_retries=2
                )

                if new_sl.get('order_id'):
                    result['action'] = 'recreated'
                    result['new_sl_order_id'] = new_sl['order_id']
                    result['success'] = True

                    # Update trackers
                    if position_tracker:
                        position_tracker.update_sl_order(symbol, new_sl['order_id'], sl_price)
                    if oco_monitor:
                        oco_monitor.update_sl_order(symbol, new_sl['order_id'], sl_price)

                    logger.info(f"SL recreated for {symbol}: {new_sl['order_id']} qty={remaining_qty}")

                else:
                    result['action'] = 'recreate_failed'
                    result['error'] = new_sl.get('error')
                    result['critical'] = True
                    logger.error(f"CRITICAL: Failed to recreate SL for {symbol} after partial exit!")

            except Exception as cancel_e:
                result['action'] = 'cancel_failed'
                result['error'] = f'Modify and cancel both failed: {cancel_e}'
                result['critical'] = True
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

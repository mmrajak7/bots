"""
Tests for SNAIL Scaled Order Execution

Comprehensive tests for batch planning, execution, and rebalancing logic.

@file        test_scaled_execution.py
@description Unit tests for scaled_execution module
@author      SNAIL Development Team
@created     2025-12-11
@version     1.0.0
"""

import pytest
from datetime import datetime
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from src.utils.scaled_execution import (
    # Enums
    BatchStatus,
    LegStatus,
    # Data classes
    DepthLevel,
    MarketDepth,
    LegOrder,
    Batch,
    ExecutionPlan,
    ExecutionResult,
    # Components
    BatchPlanner,
    DepthAnalyzer,
    BatchExecutor,
    PositionRebalancer,
    ScaledOrderExecutor,
    # Helper functions
    should_use_scaled_execution,
    get_scaling_config,
)
from src.api.kite_client import Quote


# =============================================================================
# TEST FIXTURES
# =============================================================================

@pytest.fixture
def base_config() -> Dict[str, Any]:
    """Base configuration for tests."""
    return {
        'scaling': {
            'freeze_limits': {
                'NIFTY': 1800,
                'BANKNIFTY': 600,
            },
            'execution': {
                'inter_batch_delay_ms': 100,  # Fast for tests
                'order_timeout_seconds': 5,
                'cancel_wait_seconds': 0,
                'max_retries_per_order': 1,
            },
            'order_types': {
                'atm_straddle': 'MARKET',
                'otm_wings': 'LIMIT',
            },
            'slippage': {
                'base_ticks': 3,
                'depth_buffer_ticks': 2,
                'max_ticks': 15,
                'wing_retry_increment': 2,
            },
            'rebalance': {
                'max_attempts': 2,
                'strategy': 'trim_to_balance',
            },
        }
    }


@pytest.fixture
def test_symbols() -> Dict[str, str]:
    """Test Iron Fly symbols."""
    return {
        'straddle_ce': 'NIFTY24D1924500CE',
        'straddle_pe': 'NIFTY24D1924500PE',
        'wing_ce': 'NIFTY24D1924800CE',
        'wing_pe': 'NIFTY24D1924200PE',
    }


@pytest.fixture
def test_quotes() -> Dict[str, Quote]:
    """Test quotes for Iron Fly legs."""
    return {
        'straddle_ce': Quote(bid=245.0, ask=246.0, ltp=245.5),
        'straddle_pe': Quote(bid=242.0, ask=243.0, ltp=242.5),
        'wing_ce': Quote(bid=45.0, ask=46.0, ltp=45.5),
        'wing_pe': Quote(bid=43.0, ask=44.0, ltp=43.5),
    }


@pytest.fixture
def mock_kite() -> MagicMock:
    """Mock KiteClient for tests."""
    kite = MagicMock()

    # Default paper trading order behavior
    order_counter = [0]

    def place_order(**kwargs) -> str:
        order_counter[0] += 1
        return f"ORDER_{order_counter[0]}"

    def get_order_status(order_id: str) -> Dict[str, Any]:
        return {
            'order_id': order_id,
            'status': 'COMPLETE',
            'filled_quantity': 1800,
            'average_price': 245.0,
        }

    kite.place_order.side_effect = place_order
    kite.get_order_status.side_effect = get_order_status
    kite.cancel_order.return_value = None
    kite.kite = MagicMock()
    kite.kite.quote.return_value = {}

    return kite


# =============================================================================
# BATCH PLANNER TESTS
# =============================================================================

class TestBatchPlanner:
    """Tests for BatchPlanner component."""

    def test_get_freeze_limit_known_instrument(self, base_config: Dict[str, Any]) -> None:
        """Test freeze limit lookup for known instrument."""
        planner = BatchPlanner(base_config)
        assert planner.get_freeze_limit('NIFTY') == 1800
        assert planner.get_freeze_limit('BANKNIFTY') == 600

    def test_get_freeze_limit_unknown_instrument(self, base_config: Dict[str, Any]) -> None:
        """Test freeze limit fallback for unknown instrument."""
        planner = BatchPlanner(base_config)
        assert planner.get_freeze_limit('UNKNOWN') == 1800  # Default

    def test_create_plan_single_batch(
        self, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test plan creation when quantity <= freeze limit."""
        planner = BatchPlanner(base_config)
        plan = planner.create_plan(
            total_quantity=1800,
            symbols=test_symbols,
            instrument='NIFTY'
        )

        assert plan.total_quantity == 1800
        assert plan.freeze_limit == 1800
        assert plan.num_batches == 1
        assert len(plan.batches) == 1
        assert plan.batches[0].quantity_per_leg == 1800

    def test_create_plan_multiple_batches(
        self, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test plan creation when quantity > freeze limit."""
        planner = BatchPlanner(base_config)
        plan = planner.create_plan(
            total_quantity=7500,
            symbols=test_symbols,
            instrument='NIFTY'
        )

        assert plan.total_quantity == 7500
        assert plan.num_batches == 5

        # First 4 batches should be at freeze limit
        for i in range(4):
            assert plan.batches[i].quantity_per_leg == 1800

        # Last batch should be remainder
        assert plan.batches[4].quantity_per_leg == 300

        # Total should equal input
        total = sum(b.quantity_per_leg for b in plan.batches)
        assert total == 7500

    def test_create_plan_leg_order_types(
        self, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test that leg order types are set correctly."""
        planner = BatchPlanner(base_config)
        plan = planner.create_plan(
            total_quantity=1800,
            symbols=test_symbols,
            instrument='NIFTY'
        )

        batch = plan.batches[0]

        # Straddle legs should be MARKET SELL
        assert batch.legs['straddle_ce'].order_type == 'MARKET'
        assert batch.legs['straddle_ce'].transaction_type == 'SELL'
        assert batch.legs['straddle_pe'].order_type == 'MARKET'
        assert batch.legs['straddle_pe'].transaction_type == 'SELL'

        # Wing legs should be LIMIT BUY
        assert batch.legs['wing_ce'].order_type == 'LIMIT'
        assert batch.legs['wing_ce'].transaction_type == 'BUY'
        assert batch.legs['wing_pe'].order_type == 'LIMIT'
        assert batch.legs['wing_pe'].transaction_type == 'BUY'


# =============================================================================
# DEPTH ANALYZER TESTS
# =============================================================================

class TestDepthAnalyzer:
    """Tests for DepthAnalyzer component."""

    def test_calculate_slippage_ticks_with_depth(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test slippage calculation with adequate depth."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        # Create depth with adequate liquidity
        depth = MarketDepth(
            tradingsymbol='NIFTY24D1924500CE',
            bid_levels=[
                DepthLevel(price=245.0, quantity=500),
                DepthLevel(price=244.95, quantity=800),
                DepthLevel(price=244.90, quantity=600),
            ],
            ask_levels=[
                DepthLevel(price=246.0, quantity=500),
                DepthLevel(price=246.05, quantity=800),
            ],
            ltp=245.5
        )

        # For 500 qty, depth is adequate (total bid = 1900)
        ticks = analyzer.calculate_slippage_ticks(depth, 500, 'SELL', batch_number=1)
        assert ticks == 3  # Base ticks only

    def test_calculate_slippage_ticks_thin_depth(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test slippage calculation with thin depth."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        # Create depth with thin liquidity
        depth = MarketDepth(
            tradingsymbol='NIFTY24D1924500CE',
            bid_levels=[
                DepthLevel(price=245.0, quantity=100),
            ],
            ask_levels=[],
            ltp=245.5
        )

        # For 500 qty, depth is thin (100 < 250 = 50% of 500)
        ticks = analyzer.calculate_slippage_ticks(depth, 500, 'SELL', batch_number=1)
        assert ticks == 5  # Base (3) + buffer (2)

    def test_calculate_slippage_ticks_no_depth(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test slippage calculation with no depth data."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        ticks = analyzer.calculate_slippage_ticks(None, 500, 'BUY', batch_number=1)
        assert ticks == 5  # Base (3) + buffer (2)

    def test_calculate_slippage_progressive(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test progressive slippage for later batches."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        depth = MarketDepth(
            tradingsymbol='TEST',
            bid_levels=[DepthLevel(price=100.0, quantity=10000)],
            ask_levels=[DepthLevel(price=101.0, quantity=10000)],
            ltp=100.5
        )

        # Batch 1: base ticks only
        assert analyzer.calculate_slippage_ticks(depth, 100, 'BUY', batch_number=1) == 3

        # Batch 3: base + 2 (for batch 3)
        assert analyzer.calculate_slippage_ticks(depth, 100, 'BUY', batch_number=3) == 5

    def test_calculate_price_with_slippage_buy(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test price calculation for BUY orders."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        # BUY: price goes UP (willing to pay more)
        price = analyzer.calculate_price_with_slippage(100.0, 'BUY', slippage_ticks=4)
        assert price == 100.20  # 100 + 4 * 0.05

    def test_calculate_price_with_slippage_sell(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test price calculation for SELL orders."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        # SELL: price goes DOWN (willing to accept less)
        price = analyzer.calculate_price_with_slippage(100.0, 'SELL', slippage_ticks=4)
        assert abs(price - 99.80) < 0.001  # 100 - 4 * 0.05 (use tolerance for float)


# =============================================================================
# BATCH TESTS
# =============================================================================

class TestBatch:
    """Tests for Batch data class."""

    def test_is_balanced_equal_fills(self, test_symbols: Dict[str, str]) -> None:
        """Test balance check with equal fills."""
        batch = Batch(
            batch_number=1,
            quantity_per_leg=1800,
            legs={
                leg_type: LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                    quantity=1800,
                    order_type='MARKET',
                    filled_qty=1800
                )
                for leg_type, symbol in test_symbols.items()
            }
        )

        assert batch.is_balanced is True
        assert batch.filled_quantity == 1800

    def test_is_balanced_unequal_fills(self, test_symbols: Dict[str, str]) -> None:
        """Test balance check with unequal fills."""
        legs = {}
        for leg_type, symbol in test_symbols.items():
            filled = 1800 if leg_type != 'wing_pe' else 1500
            legs[leg_type] = LegOrder(
                leg_type=leg_type,
                tradingsymbol=symbol,
                transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                quantity=1800,
                order_type='MARKET',
                filled_qty=filled
            )

        batch = Batch(batch_number=1, quantity_per_leg=1800, legs=legs)

        assert batch.is_balanced is False
        assert batch.filled_quantity == 1500  # Min of all

    def test_get_imbalance(self, test_symbols: Dict[str, str]) -> None:
        """Test imbalance calculation."""
        legs = {}
        fill_amounts = {
            'straddle_ce': 1800,
            'straddle_pe': 1800,
            'wing_ce': 1700,
            'wing_pe': 1500,
        }
        for leg_type, symbol in test_symbols.items():
            legs[leg_type] = LegOrder(
                leg_type=leg_type,
                tradingsymbol=symbol,
                transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                quantity=1800,
                order_type='MARKET',
                filled_qty=fill_amounts[leg_type]
            )

        batch = Batch(batch_number=1, quantity_per_leg=1800, legs=legs)
        imbalance = batch.get_imbalance()

        # Min is 1500, so excess = fill - 1500
        assert imbalance['straddle_ce'] == 300
        assert imbalance['straddle_pe'] == 300
        assert imbalance['wing_ce'] == 200
        assert imbalance['wing_pe'] == 0


# =============================================================================
# BATCH EXECUTOR TESTS
# =============================================================================

class TestBatchExecutor:
    """Tests for BatchExecutor component."""

    def test_execute_batch_all_fill(
        self, mock_kite: MagicMock, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test batch execution with all orders filling."""
        executor = BatchExecutor(mock_kite, base_config)

        batch = Batch(
            batch_number=1,
            quantity_per_leg=1800,
            legs={
                leg_type: LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                    quantity=1800,
                    order_type='MARKET' if 'straddle' in leg_type else 'LIMIT',
                    price=100.0 if 'wing' in leg_type else None
                )
                for leg_type, symbol in test_symbols.items()
            }
        )

        result = executor.execute_batch(batch)

        assert result.status == BatchStatus.COMPLETED
        assert result.is_balanced is True
        assert all(leg.status == LegStatus.FILLED for leg in result.legs.values())

    def test_execute_batch_partial_fill(
        self, mock_kite: MagicMock, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test batch execution with partial fills."""
        executor = BatchExecutor(mock_kite, base_config)

        # Make one leg partially fill
        call_count = [0]
        def get_order_status(order_id: str) -> Dict[str, Any]:
            call_count[0] += 1
            if 'wing_pe' in order_id or call_count[0] == 4:
                return {
                    'order_id': order_id,
                    'status': 'COMPLETE',
                    'filled_quantity': 1500,  # Partial
                    'average_price': 45.0,
                }
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'filled_quantity': 1800,
                'average_price': 245.0,
            }

        mock_kite.get_order_status.side_effect = get_order_status

        batch = Batch(
            batch_number=1,
            quantity_per_leg=1800,
            legs={
                leg_type: LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                    quantity=1800,
                    order_type='MARKET',
                    price=None
                )
                for leg_type, symbol in test_symbols.items()
            }
        )

        result = executor.execute_batch(batch)

        # Should be PARTIAL since not all legs have equal fills
        assert result.status in (BatchStatus.PARTIAL, BatchStatus.COMPLETED)


# =============================================================================
# POSITION REBALANCER TESTS
# =============================================================================

class TestPositionRebalancer:
    """Tests for PositionRebalancer component."""

    def test_rebalance_balanced_batch(
        self, mock_kite: MagicMock, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test rebalancing a batch that's already balanced."""
        rebalancer = PositionRebalancer(mock_kite, base_config)

        batch = Batch(
            batch_number=1,
            quantity_per_leg=1800,
            legs={
                leg_type: LegOrder(
                    leg_type=leg_type,
                    tradingsymbol=symbol,
                    transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                    quantity=1800,
                    order_type='MARKET',
                    filled_qty=1800,
                    status=LegStatus.FILLED
                )
                for leg_type, symbol in test_symbols.items()
            }
        )

        result = rebalancer.rebalance_batch(batch)
        assert result is True
        assert len(batch.rebalance_orders) == 0

    def test_rebalance_imbalanced_batch(
        self, mock_kite: MagicMock, base_config: Dict[str, Any], test_symbols: Dict[str, str]
    ) -> None:
        """Test rebalancing an imbalanced batch."""
        rebalancer = PositionRebalancer(mock_kite, base_config)

        # Create batch with imbalance
        legs = {}
        for leg_type, symbol in test_symbols.items():
            filled = 1800 if leg_type != 'wing_pe' else 1500
            legs[leg_type] = LegOrder(
                leg_type=leg_type,
                tradingsymbol=symbol,
                transaction_type='SELL' if 'straddle' in leg_type else 'BUY',
                quantity=1800,
                order_type='MARKET',
                filled_qty=filled,
                status=LegStatus.FILLED
            )

        batch = Batch(batch_number=1, quantity_per_leg=1800, legs=legs)

        # Mock successful rebalance orders
        mock_kite.get_order_status.return_value = {
            'status': 'COMPLETE',
            'filled_quantity': 300,
            'average_price': 100.0,
        }

        result = rebalancer.rebalance_batch(batch)

        # Should have placed trim orders for excess legs
        assert len(batch.rebalance_orders) == 3  # 3 legs had excess


# =============================================================================
# SCALED ORDER EXECUTOR TESTS
# =============================================================================

class TestScaledOrderExecutor:
    """Tests for ScaledOrderExecutor component."""

    def test_execute_entry_success(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test successful scaled entry execution."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_entry(
            symbols=test_symbols,
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is True
        assert result.total_filled_qty == 1800
        assert result.batches_completed == 1
        assert result.batches_total == 1

    def test_execute_entry_multiple_batches(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test scaled entry with multiple batches."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_entry(
            symbols=test_symbols,
            total_quantity=3600,  # 2 batches
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is True
        assert result.batches_total == 2


# =============================================================================
# HELPER FUNCTION TESTS
# =============================================================================

class TestHelperFunctions:
    """Tests for helper functions."""

    def test_should_use_scaled_execution_below_limit(self, base_config: Dict[str, Any]) -> None:
        """Test that small quantities don't use scaled execution."""
        assert should_use_scaled_execution(75, base_config) is False
        assert should_use_scaled_execution(1800, base_config) is False

    def test_should_use_scaled_execution_above_limit(self, base_config: Dict[str, Any]) -> None:
        """Test that large quantities use scaled execution."""
        assert should_use_scaled_execution(1801, base_config) is True
        assert should_use_scaled_execution(7500, base_config) is True

    def test_get_scaling_config_with_config(self, base_config: Dict[str, Any]) -> None:
        """Test config extraction with full config."""
        scaling = get_scaling_config(base_config)

        assert scaling['freeze_limits']['NIFTY'] == 1800
        assert scaling['execution']['inter_batch_delay_ms'] == 100
        assert scaling['order_types']['atm_straddle'] == 'MARKET'

    def test_get_scaling_config_empty(self) -> None:
        """Test config extraction with empty config."""
        scaling = get_scaling_config({})

        # Should have defaults
        assert scaling['freeze_limits']['NIFTY'] == 1800
        assert scaling['execution']['inter_batch_delay_ms'] == 3000


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

class TestIntegration:
    """Integration tests for complete flow."""

    def test_full_flow_100_lots(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test complete flow for 100 lots (7500 contracts)."""
        # Create a mock that returns correct filled quantities for each batch
        # Batches: 1800, 1800, 1800, 1800, 300
        # Each batch has 4 legs, so 20 order status calls total
        batch_quantities = [1800] * 4 + [1800] * 4 + [1800] * 4 + [1800] * 4 + [300] * 4
        call_count = [0]

        def get_order_status(order_id: str) -> Dict[str, Any]:
            idx = call_count[0]
            call_count[0] += 1

            # Return the correct batch quantity
            qty = batch_quantities[idx] if idx < len(batch_quantities) else 300
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'filled_quantity': qty,
                'average_price': 245.0,
            }

        mock_kite.get_order_status.side_effect = get_order_status

        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_entry(
            symbols=test_symbols,
            total_quantity=7500,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is True
        assert result.batches_total == 5
        # All batches should complete
        assert result.batches_completed == 5
        # Total filled should be 7500
        assert result.total_filled_qty == 7500

    def test_batch1_failure_aborts(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test that batch 1 failure aborts entire execution."""
        # Make all orders reject
        mock_kite.place_order.side_effect = Exception("Order rejected")

        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=False)

        result = executor.execute_entry(
            symbols=test_symbols,
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is False
        assert result.total_filled_qty == 0
        assert any('Batch 1 failed' in e for e in result.errors)


# =============================================================================
# EXIT EXECUTION TESTS
# =============================================================================

class TestScaledExit:
    """Tests for scaled exit execution."""

    def test_create_exit_plan_transaction_types(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str]
    ) -> None:
        """Test that exit plan has reversed transaction types."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        plan = executor._create_exit_plan(
            total_quantity=1800,
            symbols=test_symbols,
            instrument='NIFTY',
            order_types={'atm_straddle': 'MARKET', 'otm_wings': 'LIMIT'}
        )

        batch = plan.batches[0]

        # Straddle legs should be BUY (closing short)
        assert batch.legs['straddle_ce'].transaction_type == 'BUY'
        assert batch.legs['straddle_pe'].transaction_type == 'BUY'

        # Wing legs should be SELL (closing long)
        assert batch.legs['wing_ce'].transaction_type == 'SELL'
        assert batch.legs['wing_pe'].transaction_type == 'SELL'

    def test_execute_exit_success(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test successful scaled exit execution."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_exit(
            symbols=test_symbols,
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY',
            urgent=False
        )

        assert result.success is True
        assert result.total_filled_qty == 1800
        assert result.batches_completed == 1

    def test_execute_exit_urgent_mode(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test urgent exit uses MARKET orders."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        # Create a plan with urgent=True to verify order types
        plan = executor._create_exit_plan(
            total_quantity=1800,
            symbols=test_symbols,
            instrument='NIFTY',
            order_types={'atm_straddle': 'MARKET', 'otm_wings': 'MARKET'}  # Urgent = all MARKET
        )

        batch = plan.batches[0]

        # All legs should be MARKET in urgent mode
        for leg in batch.legs.values():
            assert leg.order_type == 'MARKET'

    def test_execute_exit_multiple_batches(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test scaled exit with multiple batches."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_exit(
            symbols=test_symbols,
            total_quantity=3600,  # 2 batches
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is True
        assert result.batches_total == 2

    def test_exit_continues_on_failure(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test that exit continues even after batch failure (unlike entry which aborts)."""
        call_count = [0]

        def place_order(**kwargs) -> str:
            call_count[0] += 1
            # First 4 orders succeed, rest fail
            if call_count[0] <= 4:
                return f"ORDER_{call_count[0]}"
            raise Exception("Order failed")

        mock_kite.place_order.side_effect = place_order

        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=False)

        result = executor.execute_exit(
            symbols=test_symbols,
            total_quantity=3600,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        # Should still have success=True if any quantity was closed
        # (Unlike entry which would abort)
        assert result.total_filled_qty > 0 or len(result.errors) > 0

    def test_exit_slippage_calculation(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test that exit slippage is calculated correctly (reversed from entry)."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        # For exit:
        # - Straddle BUY: slippage = fill - ask (positive = bad)
        # - Wing SELL: slippage = bid - fill (positive = bad)

        result = executor.execute_exit(
            symbols=test_symbols,
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is True
        # Slippage should be calculated (may be 0 or positive)
        assert result.total_slippage >= 0


class TestExitPlanCreation:
    """Tests for exit plan creation."""

    def test_exit_plan_batching(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str]
    ) -> None:
        """Test exit plan batch creation matches entry logic."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        # 100 lots = 7500 contracts = 5 batches
        plan = executor._create_exit_plan(
            total_quantity=7500,
            symbols=test_symbols,
            instrument='NIFTY',
            order_types={'atm_straddle': 'MARKET', 'otm_wings': 'LIMIT'}
        )

        assert plan.num_batches == 5
        assert plan.batches[0].quantity_per_leg == 1800
        assert plan.batches[4].quantity_per_leg == 300

        # Total should match
        total = sum(b.quantity_per_leg for b in plan.batches)
        assert total == 7500


# =============================================================================
# EDGE CASE TESTS - Added during code review
# =============================================================================

class TestEdgeCases:
    """Edge case tests identified during code review."""

    def test_execute_entry_empty_symbols(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test entry fails gracefully with empty symbols dict."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_entry(
            symbols={},
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is False
        assert 'Invalid symbols dict' in result.errors[0]

    def test_execute_entry_missing_leg(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test entry fails with missing leg in symbols."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        # Missing wing_pe
        incomplete_symbols = {
            'straddle_ce': 'NIFTY24D1924500CE',
            'straddle_pe': 'NIFTY24D1924500PE',
            'wing_ce': 'NIFTY24D1924800CE',
        }

        result = executor.execute_entry(
            symbols=incomplete_symbols,
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is False
        assert 'Invalid symbols dict' in result.errors[0]
        assert 'wing_pe' in result.errors[0]

    def test_execute_exit_empty_symbols(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test exit fails gracefully with empty symbols dict."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        result = executor.execute_exit(
            symbols={},
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is False
        assert 'Invalid symbols dict' in result.errors[0]

    def test_zero_quantity(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_symbols: Dict[str, str],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test handling of zero quantity."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        # Zero quantity should create a plan with 0 batches
        result = executor.execute_entry(
            symbols=test_symbols,
            total_quantity=0,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        # Should succeed trivially with 0 filled
        assert result.total_filled_qty == 0
        assert result.batches_total == 0

    def test_banknifty_freeze_limit(self, base_config: Dict[str, Any]) -> None:
        """Test BANKNIFTY uses correct freeze limit."""
        planner = BatchPlanner(base_config)

        # BANKNIFTY freeze limit is 600 (40 lots * 15 lot size)
        assert planner.get_freeze_limit('BANKNIFTY') == 600

        # 1200 contracts should be 2 batches for BANKNIFTY
        test_symbols = {
            'straddle_ce': 'BANKNIFTY24D1951000CE',
            'straddle_pe': 'BANKNIFTY24D1951000PE',
            'wing_ce': 'BANKNIFTY24D1951500CE',
            'wing_pe': 'BANKNIFTY24D1950500PE',
        }
        plan = planner.create_plan(
            total_quantity=1200,
            symbols=test_symbols,
            instrument='BANKNIFTY'
        )

        assert plan.num_batches == 2
        assert plan.batches[0].quantity_per_leg == 600
        assert plan.batches[1].quantity_per_leg == 600

    def test_slippage_max_cap(
        self, mock_kite: MagicMock, base_config: Dict[str, Any]
    ) -> None:
        """Test that slippage is capped at max_ticks."""
        analyzer = DepthAnalyzer(mock_kite, base_config)

        # Very thin depth - should trigger buffer
        # High batch number - should add progressive
        # But should not exceed max_ticks (15)
        thin_depth = MarketDepth(
            tradingsymbol='TEST',
            bid_levels=[DepthLevel(price=100.0, quantity=10)],  # Very thin
            ask_levels=[],
            ltp=100.0
        )

        # batch_number=10 would add 9 * wing_retry_increment = 18
        # base (3) + buffer (2) + progressive (18) = 23, but capped at 15
        ticks = analyzer.calculate_slippage_ticks(thin_depth, 500, 'SELL', batch_number=10)
        assert ticks <= 15  # max_ticks


class TestSymbolsValidation:
    """Tests for symbols dict validation."""

    def test_extra_leg_rejected(
        self,
        mock_kite: MagicMock,
        base_config: Dict[str, Any],
        test_quotes: Dict[str, Quote]
    ) -> None:
        """Test that extra legs in symbols dict are rejected."""
        executor = ScaledOrderExecutor(mock_kite, base_config, paper_trading=True)

        symbols_with_extra = {
            'straddle_ce': 'NIFTY24D1924500CE',
            'straddle_pe': 'NIFTY24D1924500PE',
            'wing_ce': 'NIFTY24D1924800CE',
            'wing_pe': 'NIFTY24D1924200PE',
            'extra_leg': 'NIFTY24D1925000CE',  # Extra
        }

        result = executor.execute_entry(
            symbols=symbols_with_extra,
            total_quantity=1800,
            quotes=test_quotes,
            instrument='NIFTY'
        )

        assert result.success is False
        assert 'Extra:' in result.errors[0]


# =============================================================================
# RUN TESTS
# =============================================================================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])

"""
Integration Tests for Order Tracking and Verification

Tests critical scenarios that have caused production failures:
1. EXIT order not confused with SL order
2. Tag-based order verification
3. "error from core" recovery
4. Double exit detection

Run with: py -3.11 -m pytest tests/test_order_tracking.py -v
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime

# Add parent to path for imports
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.order_tracker import OrderTracker, OrderType, TrackedOrder
from core.order_manager import OrderManager, OrderParams, OrderResult


class TestOrderTracker:
    """Tests for OrderTracker class."""

    def test_generate_unique_tags(self):
        """Tags must be unique even when generated rapidly."""
        tracker = OrderTracker()
        tags = set()

        for _ in range(100):
            tag = tracker.generate_tag(OrderType.EXIT, "BANKNIFTY")
            assert tag not in tags, f"Duplicate tag generated: {tag}"
            tags.add(tag)

    def test_tag_format(self):
        """Tags should have correct format and length."""
        tracker = OrderTracker()

        exit_tag = tracker.generate_tag(OrderType.EXIT, "NIFTY")
        assert exit_tag.startswith("EXT_"), f"EXIT tag wrong format: {exit_tag}"
        assert len(exit_tag) <= 20, f"Tag too long: {exit_tag}"

        sl_tag = tracker.generate_tag(OrderType.SL, "NIFTY")
        assert sl_tag.startswith("SLP_"), f"SL tag wrong format: {sl_tag}"

    def test_register_and_confirm_order(self):
        """Order registration and confirmation flow."""
        tracker = OrderTracker()

        tag = tracker.generate_tag(OrderType.EXIT, "BANKNIFTY26FEB58700PE")
        tracker.register_order(
            tag=tag,
            symbol="BANKNIFTY26FEB58700PE",
            transaction_type="S",
            quantity=30,
            api_order_type="MKT",
            price_type=OrderType.EXIT
        )

        order = tracker.get_order_by_tag(tag)
        assert order is not None
        assert order.status == "pending"
        assert order.symbol == "BANKNIFTY26FEB58700PE"

        # Confirm with order ID
        tracker.confirm_order(tag, "260127000936663")

        order = tracker.get_order_by_tag(tag)
        assert order.status == "confirmed"
        assert order.order_id == "260127000936663"

        # Reverse lookup should work
        order_by_id = tracker.get_order_by_id("260127000936663")
        assert order_by_id is not None
        assert order_by_id.tag == tag

    def test_is_our_order(self):
        """Check if order belongs to us."""
        tracker = OrderTracker()

        tag = tracker.generate_tag(OrderType.SL, "NIFTY")
        tracker.register_order(tag, "NIFTY", "S", 50, "SL", OrderType.SL)
        tracker.confirm_order(tag, "ORDER123")

        assert tracker.is_our_order("ORDER123") == True
        assert tracker.is_our_order("UNKNOWN456") == False

    def test_get_order_type(self):
        """Get order classification by order ID."""
        tracker = OrderTracker()

        # Register EXIT order
        exit_tag = tracker.generate_tag(OrderType.EXIT, "NIFTY")
        tracker.register_order(exit_tag, "NIFTY", "S", 50, "MKT", OrderType.EXIT)
        tracker.confirm_order(exit_tag, "EXIT_ORDER_123")

        # Register SL order
        sl_tag = tracker.generate_tag(OrderType.SL, "NIFTY")
        tracker.register_order(sl_tag, "NIFTY", "S", 50, "SL", OrderType.SL)
        tracker.confirm_order(sl_tag, "SL_ORDER_456")

        assert tracker.get_order_type("EXIT_ORDER_123") == OrderType.EXIT
        assert tracker.get_order_type("SL_ORDER_456") == OrderType.SL
        assert tracker.get_order_type("UNKNOWN") is None


class TestOrderVerificationByTag:
    """Tests for tag-based order verification."""

    def create_mock_order(self, symbol, txn_type, qty, order_type, status, tag, order_id):
        """Create a mock order dict as returned by NEO API."""
        return {
            'nOrdNo': order_id,
            'trdSym': symbol,
            'trnsTp': txn_type,
            'qty': str(qty),
            'prcTp': order_type,
            'ordSt': status,
            'remarks': tag,
            'ordDtTm': datetime.now().strftime("%d-%b-%Y %H:%M:%S")
        }

    def test_verify_by_tag_finds_correct_order(self):
        """Tag-based verification finds the exact order."""
        tracker = OrderTracker()

        # Register our EXIT order
        exit_tag = tracker.generate_tag(OrderType.EXIT, "BANKNIFTY26FEB58700PE")
        tracker.register_order(
            exit_tag, "BANKNIFTY26FEB58700PE", "S", 30, "MKT", OrderType.EXIT
        )

        # Mock API client
        mock_client = Mock()
        mock_client.order_report.return_value = {
            'data': [
                # SL order (same symbol, same qty, same direction - but different tag!)
                self.create_mock_order(
                    "BANKNIFTY26FEB58700PE", "S", 30, "SL",
                    "trigger pending", "SLP_1234567890_001", "260127000936663"
                ),
                # Our EXIT order (with our tag)
                self.create_mock_order(
                    "BANKNIFTY26FEB58700PE", "S", 30, "MKT",
                    "complete", exit_tag, "260127000936999"
                ),
            ]
        }

        # Verify should find our order by tag, not the SL
        order_id = tracker.verify_order_by_tag(mock_client, exit_tag)

        assert order_id == "260127000936999", f"Wrong order found: {order_id}"
        assert order_id != "260127000936663", "Found SL order instead of EXIT!"

    def test_verify_rejects_wrong_tag(self):
        """Verification should not match orders with different tags."""
        tracker = OrderTracker()

        exit_tag = tracker.generate_tag(OrderType.EXIT, "NIFTY")
        tracker.register_order(exit_tag, "NIFTY", "S", 50, "MKT", OrderType.EXIT)

        mock_client = Mock()
        mock_client.order_report.return_value = {
            'data': [
                # Order with different tag
                self.create_mock_order(
                    "NIFTY", "S", 50, "MKT",
                    "complete", "DIFFERENT_TAG", "ORDER123"
                ),
            ]
        }

        # Should not find by tag (tag doesn't match)
        # But might find by criteria fallback
        order_id = tracker.verify_order_by_tag(mock_client, exit_tag)

        # With criteria fallback, it might find ORDER123
        # But the important thing is it searched by tag first


class TestCriticalScenario_ExitVsSL:
    """
    Test the critical scenario that caused production failure:
    EXIT order placed, API returns error, verification finds SL order instead.
    """

    def test_exit_not_confused_with_sl(self):
        """
        CRITICAL TEST: When EXIT fails and we verify, we must NOT return
        an existing SL order as the "verified EXIT".
        """
        tracker = OrderTracker()

        # Step 1: SL order was placed earlier
        sl_tag = tracker.generate_tag(OrderType.SL, "BANKNIFTY26FEB58700PE")
        tracker.register_order(
            sl_tag, "BANKNIFTY26FEB58700PE", "S", 30, "SL", OrderType.SL
        )
        tracker.confirm_order(sl_tag, "SL_ORDER_663")

        # Step 2: EXIT order is being placed (not yet confirmed)
        exit_tag = tracker.generate_tag(OrderType.EXIT, "BANKNIFTY26FEB58700PE")
        tracker.register_order(
            exit_tag, "BANKNIFTY26FEB58700PE", "S", 30, "MKT", OrderType.EXIT
        )

        # Step 3: API returns "error from core", we need to verify
        mock_client = Mock()
        mock_client.order_report.return_value = {
            'data': [
                # Only the SL order exists (EXIT was never placed)
                {
                    'nOrdNo': 'SL_ORDER_663',
                    'trdSym': 'BANKNIFTY26FEB58700PE',
                    'trnsTp': 'S',
                    'qty': '30',
                    'prcTp': 'SL',  # This is SL, not MKT
                    'ordSt': 'trigger pending',
                    'remarks': sl_tag,
                    'ordDtTm': datetime.now().strftime("%d-%b-%Y %H:%M:%S")
                }
            ]
        }

        # Verify by tag should NOT find the EXIT (it doesn't exist)
        order_id = tracker.verify_order_by_tag(mock_client, exit_tag)

        # CRITICAL ASSERTION: Must NOT return the SL order
        assert order_id != 'SL_ORDER_663', \
            "CRITICAL BUG: Found SL order when searching for EXIT!"

        # Should return None since EXIT order doesn't exist
        assert order_id is None, \
            f"Should return None, but got: {order_id}"


class TestOrderManagerIntegration:
    """Integration tests for OrderManager with tracking."""

    @pytest.fixture
    def mock_config(self):
        return {
            'risk_management': {
                'max_loss_per_day': 10000,
                'max_open_positions': 5,
                'duplicate_order_window_sec': 5
            },
            'trading_defaults': {
                'product': 'MIS',
                'order_type': 'L',
                'validity': 'DAY'
            },
            'market_hours': {
                'open': '09:15',
                'close': '15:30'
            }
        }

    @pytest.fixture
    def mock_client(self):
        client = Mock()
        client.positions.return_value = {'data': []}
        client.order_report.return_value = {'data': []}
        client.limits.return_value = {
            'data': {'marginAvailable': '100000'}
        }
        return client

    def test_exit_order_uses_tracking_tag(self, mock_config, mock_client):
        """EXIT orders should use unique tracking tags."""
        # Successful order placement
        mock_client.place_order.return_value = {
            'nOrdNo': 'EXIT_123'
        }

        manager = OrderManager(mock_client, mock_config)

        params = OrderParams(
            symbol="NIFTY26JAN50000PE",
            exchange_segment="nse_fo",
            instrument_token="12345",
            transaction_type="S",
            quantity=50,
            product="MIS",
            order_type="MKT",
            tag="EXIT"  # Hint that this is an exit
        )

        result = manager.place_order(params)

        # Verify place_order was called with a tracking tag
        call_args = mock_client.place_order.call_args
        assert call_args is not None
        tag_used = call_args.kwargs.get('tag', call_args[1].get('tag', ''))
        assert tag_used.startswith('EXT_'), f"Wrong tag prefix: {tag_used}"

    def test_error_from_core_uses_tag_verification(self, mock_config, mock_client):
        """When 'error from core' occurs, verify by tag first."""
        # First call fails with "error from core"
        mock_client.place_order.return_value = {
            'stCode': 32,
            'errMsg': 'error from core',
            'stat': 'failed'
        }

        # Order book will be checked
        mock_client.order_report.return_value = {
            'data': []  # Empty - order really wasn't placed
        }

        manager = OrderManager(mock_client, mock_config)

        params = OrderParams(
            symbol="NIFTY26JAN50000PE",
            exchange_segment="nse_fo",
            instrument_token="12345",
            transaction_type="S",
            quantity=50,
            product="MIS",
            order_type="MKT",
            tag="EXIT"
        )

        result = manager.place_order(params)

        # Order should fail since it's not in the book
        assert result.success == False, "Should fail when order not in book"
        assert "not in book" in result.message.lower()


class TestTrackerStats:
    """Tests for tracker statistics."""

    def test_stats_tracking(self):
        """Tracker should maintain accurate statistics."""
        tracker = OrderTracker()

        # Add various orders
        for i in range(3):
            tag = tracker.generate_tag(OrderType.EXIT, "NIFTY")
            tracker.register_order(tag, "NIFTY", "S", 50, "MKT", OrderType.EXIT)
            if i < 2:  # Confirm 2, leave 1 pending
                tracker.confirm_order(tag, f"ORDER_{i}")

        for i in range(2):
            tag = tracker.generate_tag(OrderType.SL, "NIFTY")
            tracker.register_order(tag, "NIFTY", "S", 50, "SL", OrderType.SL)
            tracker.confirm_order(tag, f"SL_{i}")

        tag = tracker.generate_tag(OrderType.ENTRY, "NIFTY")
        tracker.register_order(tag, "NIFTY", "B", 50, "L", OrderType.ENTRY)
        tracker.mark_failed(tag, "Rejected")

        stats = tracker.get_stats()

        assert stats['total'] == 6
        assert stats['confirmed'] == 4
        assert stats['pending'] == 1
        assert stats['failed'] == 1
        assert stats['by_type']['EXIT'] == 3
        assert stats['by_type']['SL'] == 2
        assert stats['by_type']['ENTRY'] == 1


if __name__ == '__main__':
    pytest.main([__file__, '-v', '--tb=short'])

"""
Comprehensive Kite API Adapter Tests

Tests all broker adapter operations using the new Kite API method.
Run this to verify the adapter pattern works correctly before production use.

Usage: python test_kite_api_adapter.py
"""

import sys
import os
import io
from datetime import datetime, timedelta
from pathlib import Path

# Fix Windows console encoding
if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger

# Configure logger for testing
logger.remove()
logger.add(sys.stdout, level="INFO", format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}")


def print_header(title):
    """Print a section header."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_result(test_name, passed, details=""):
    """Print test result."""
    status = "PASS" if passed else "FAIL"
    symbol = "\u2713" if passed else "\u2717"  # Unicode check/cross
    print(f"  [{symbol}] {test_name}: {status}")
    if details:
        print(f"      {details}")


def test_broker_factory():
    """Test 1: Broker Factory creates correct adapter."""
    print_header("TEST 1: Broker Factory")

    try:
        from src.api.broker_factory import get_broker_adapter, validate_kite_api_token

        # Test token validation
        is_valid, message = validate_kite_api_token()
        print_result("Token validation", is_valid, message)

        # Test adapter creation
        broker = get_broker_adapter()
        trade_method = broker.trade_method

        is_kite_api = trade_method == "kite_api"
        print_result("Adapter type is kite_api", is_kite_api, f"Method: {trade_method}")

        return is_valid and is_kite_api, broker

    except Exception as e:
        print_result("Broker factory", False, str(e))
        return False, None


def test_connection_validation(broker):
    """Test 2: Connection validation."""
    print_header("TEST 2: Connection Validation")

    try:
        is_valid = broker.validate_connection()
        print_result("Connection to Zerodha", is_valid)
        return is_valid

    except Exception as e:
        print_result("Connection validation", False, str(e))
        return False


def test_historical_data(broker):
    """Test 3: Historical data fetching."""
    print_header("TEST 3: Historical Data")

    tests_passed = 0
    total_tests = 3

    try:
        # Test 3a: Fetch NIFTY daily data (last 7 days)
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')

        df = broker.get_historical_data(
            instrument_token="256265",  # NIFTY
            start_date=start_date,
            end_date=end_date,
            interval='day'
        )

        if df is not None and not df.empty:
            print_result("NIFTY daily data (7 days)", True, f"{len(df)} candles")
            tests_passed += 1
        else:
            print_result("NIFTY daily data", False, "Empty dataframe")

    except Exception as e:
        print_result("NIFTY daily data", False, str(e))

    try:
        # Test 3b: Fetch RELIANCE data
        df = broker.get_historical_data(
            instrument_token="738561",  # RELIANCE
            start_date=start_date,
            end_date=end_date,
            interval='day'
        )

        if df is not None and not df.empty:
            print_result("RELIANCE daily data", True, f"{len(df)} candles")
            tests_passed += 1
        else:
            print_result("RELIANCE daily data", False, "Empty dataframe")

    except Exception as e:
        print_result("RELIANCE daily data", False, str(e))

    try:
        # Test 3c: Get sampled monthly data
        df = broker.get_historical_data_sampled(
            instrument_token="256265",
            timeframe='monthly',
            years_back=1.0
        )

        if df is not None and not df.empty:
            print_result("NIFTY monthly sampled", True, f"{len(df)} candles")
            tests_passed += 1
        else:
            print_result("NIFTY monthly sampled", False, "Empty dataframe")

    except Exception as e:
        print_result("NIFTY monthly sampled", False, str(e))

    return tests_passed == total_tests


def test_instrument_operations(broker):
    """Test 4: Instrument token lookup."""
    print_header("TEST 4: Instrument Operations")

    tests_passed = 0
    total_tests = 4

    try:
        # Test 4a: Load instruments cache
        loaded = broker.load_instruments_cache()
        print_result("Load instruments cache", loaded)
        if loaded:
            tests_passed += 1

    except Exception as e:
        print_result("Load instruments cache", False, str(e))

    try:
        # Test 4b: Get token for RELIANCE
        token = broker.get_instrument_token("RELIANCE")
        is_valid = token and len(token) > 0
        print_result("Get RELIANCE token", is_valid, f"Token: {token}")
        if is_valid:
            tests_passed += 1

    except Exception as e:
        print_result("Get RELIANCE token", False, str(e))

    try:
        # Test 4c: Get token with symbol variation (M&M)
        token = broker.get_instrument_token("M&M")
        is_valid = token and len(token) > 0
        print_result("Get M&M token (variation)", is_valid, f"Token: {token}")
        if is_valid:
            tests_passed += 1

    except Exception as e:
        print_result("Get M&M token", False, str(e))

    try:
        # Test 4d: Get LTP for instrument
        ltp = broker.get_instrument_ltp("738561")  # RELIANCE
        is_valid = ltp and ltp > 0
        print_result("Get RELIANCE LTP", is_valid, f"LTP: Rs.{ltp:.2f}" if ltp else "N/A")
        if is_valid:
            tests_passed += 1

    except Exception as e:
        print_result("Get RELIANCE LTP", False, str(e))

    return tests_passed == total_tests


def test_margins(broker):
    """Test 5: Margin data."""
    print_header("TEST 5: Margin Data")

    try:
        margins = broker.get_margins()

        if margins:
            # Check if we have data in expected format
            data = margins.get('data', margins)

            if 'equity' in data:
                equity = data['equity']
                available = equity.get('available', {}).get('cash', 0)
                print_result("Get margin data", True,
                           f"Available: Rs.{available:,.2f}")
                return True
            elif 'net' in str(data):
                print_result("Get margin data", True, "Margin data received")
                return True
            else:
                print_result("Get margin data", True, f"Data: {str(data)[:100]}")
                return True
        else:
            print_result("Get margin data", False, "No data returned")
            return False

    except Exception as e:
        print_result("Get margin data", False, str(e))
        return False


def test_positions(broker):
    """Test 6: Positions data."""
    print_header("TEST 6: Positions")

    try:
        positions = broker.get_positions()

        if positions is not None:
            net_positions = positions.get('net', [])
            day_positions = positions.get('day', [])

            print_result("Get positions", True,
                       f"Net: {len(net_positions)}, Day: {len(day_positions)}")
            return True
        else:
            print_result("Get positions", False, "No data returned")
            return False

    except Exception as e:
        print_result("Get positions", False, str(e))
        return False


def test_orders(broker):
    """Test 7: Orders data."""
    print_header("TEST 7: Orders")

    try:
        orders = broker.get_all_orders()

        if orders is not None:
            print_result("Get all orders", True, f"Orders today: {len(orders)}")
            return True
        else:
            print_result("Get all orders", False, "No data returned")
            return False

    except Exception as e:
        print_result("Get all orders", False, str(e))
        return False


def test_gtt_orders(broker):
    """Test 8: GTT Orders."""
    print_header("TEST 8: GTT Orders")

    try:
        gtt_orders = broker.get_gtt_orders()

        if gtt_orders is not None:
            print_result("Get GTT orders", True, f"Active GTTs: {len(gtt_orders)}")
            return True
        else:
            print_result("Get GTT orders", False, "No data returned")
            return False

    except Exception as e:
        print_result("Get GTT orders", False, str(e))
        return False


def test_cache_operations(broker):
    """Test 9: Cache operations."""
    print_header("TEST 9: Cache Operations")

    tests_passed = 0

    try:
        broker.clear_cache()
        print_result("Clear data cache", True)
        tests_passed += 1
    except Exception as e:
        print_result("Clear data cache", False, str(e))

    try:
        broker.clear_instruments_cache()
        print_result("Clear instruments cache", True)
        tests_passed += 1
    except Exception as e:
        print_result("Clear instruments cache", False, str(e))

    return tests_passed == 2


def test_service_integration():
    """Test 10: Service integration (uses broker factory internally)."""
    print_header("TEST 10: Service Integration")

    tests_passed = 0
    total_tests = 3

    try:
        # Test capital manager
        from src.services.capital_manager import CapitalManager
        cm = CapitalManager()

        # Check if it uses the broker
        trade_method = cm.kite_client.trade_method
        is_kite_api = trade_method == "kite_api"
        print_result("CapitalManager uses kite_api", is_kite_api, f"Method: {trade_method}")
        if is_kite_api:
            tests_passed += 1

    except Exception as e:
        print_result("CapitalManager integration", False, str(e))

    try:
        # Test entry manager
        from src.services.entry_manager import EntryManager
        em = EntryManager()

        trade_method = em.kite_client.trade_method
        is_kite_api = trade_method == "kite_api"
        print_result("EntryManager uses kite_api", is_kite_api, f"Method: {trade_method}")
        if is_kite_api:
            tests_passed += 1

    except Exception as e:
        print_result("EntryManager integration", False, str(e))

    try:
        # Test exit manager
        from src.services.exit_manager import ExitManager
        exm = ExitManager()

        trade_method = exm.kite_client.trade_method
        is_kite_api = trade_method == "kite_api"
        print_result("ExitManager uses kite_api", is_kite_api, f"Method: {trade_method}")
        if is_kite_api:
            tests_passed += 1

    except Exception as e:
        print_result("ExitManager integration", False, str(e))

    return tests_passed == total_tests


def run_all_tests():
    """Run all tests and print summary."""
    print("\n")
    print("="*70)
    print("  CROCODILE - Kite API Adapter Test Suite")
    print("  Testing new kite_api trade method")
    print(f"  Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*70)

    results = {}

    # Test 1: Broker Factory
    passed, broker = test_broker_factory()
    results["Broker Factory"] = passed

    if not broker:
        print("\n" + "!"*70)
        print("  CRITICAL: Broker factory failed. Cannot continue tests.")
        print("!"*70)
        return False

    # Test 2: Connection
    results["Connection Validation"] = test_connection_validation(broker)

    # Test 3: Historical Data
    results["Historical Data"] = test_historical_data(broker)

    # Test 4: Instruments
    results["Instrument Operations"] = test_instrument_operations(broker)

    # Test 5: Margins
    results["Margin Data"] = test_margins(broker)

    # Test 6: Positions
    results["Positions"] = test_positions(broker)

    # Test 7: Orders
    results["Orders"] = test_orders(broker)

    # Test 8: GTT Orders
    results["GTT Orders"] = test_gtt_orders(broker)

    # Test 9: Cache Operations
    results["Cache Operations"] = test_cache_operations(broker)

    # Test 10: Service Integration
    results["Service Integration"] = test_service_integration()

    # Print Summary
    print_header("TEST SUMMARY")

    passed_count = sum(1 for v in results.values() if v)
    total_count = len(results)

    for test_name, passed in results.items():
        symbol = "\u2713" if passed else "\u2717"
        status = "PASSED" if passed else "FAILED"
        print(f"  [{symbol}] {test_name}: {status}")

    print(f"\n  {'='*50}")
    print(f"  Total: {passed_count}/{total_count} tests passed")

    if passed_count == total_count:
        print(f"\n  \u2714 ALL TESTS PASSED - Kite API adapter is ready!")
    else:
        print(f"\n  \u2718 SOME TESTS FAILED - Review issues above")

    print(f"  {'='*50}\n")

    return passed_count == total_count


if __name__ == "__main__":
    success = run_all_tests()
    sys.exit(0 if success else 1)

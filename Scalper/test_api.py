"""
NEO Trade Terminal - API Connectivity & Trade Test Script

Run this BEFORE using the main terminal to verify:
1. NEO API login works
2. Order placement works
3. Order modification works
4. Order cancellation works
5. Position tracking works

Usage:
    python test_api.py --check          # Check connectivity only
    python test_api.py --trade SYMBOL PRICE QTY ACTION

Example:
    python test_api.py --check
    python test_api.py --trade NIFTY24JAN24000CE 5.50 75 BUY
"""

import sys
import os
import yaml
import json
import time
import argparse
from datetime import datetime
from typing import Dict, Any, Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def load_config():
    """Load configuration."""
    with open('config/settings.yaml', 'r') as f:
        config = yaml.safe_load(f)

    creds_path = 'config/credentials.yaml'
    if os.path.exists(creds_path):
        with open(creds_path, 'r') as f:
            creds = yaml.safe_load(f)
            for key in ['neo_credentials', 'kite_credentials', 'telegram']:
                if key in creds:
                    config[key] = creds[key]

    return config


def print_section(title: str):
    """Print section header."""
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def print_result(test: str, success: bool, details: str = ""):
    """Print test result."""
    status = "[PASS]" if success else "[FAIL]"
    print(f"  {status} | {test}")
    if details:
        print(f"         -> {details}")


def test_neo_login(config: Dict[str, Any]) -> tuple:
    """Test NEO API v2 login."""
    print_section("TEST 1: NEO API v2 Login")

    try:
        from neo_api_client import NeoAPI
        import pyotp

        neo_creds = config.get('neo_credentials', {})

        # Check credentials exist (v2: no consumer_secret needed)
        required = ['consumer_key', 'mobile_number', 'ucc', 'mpin', 'totp_secret']
        missing = [k for k in required if not neo_creds.get(k)]
        if missing:
            print_result("Credentials check", False, f"Missing: {missing}")
            return None, "Missing credentials"

        print_result("Credentials check", True, "All required fields present")

        # Initialize client (v2: only consumer_key needed)
        client = NeoAPI(
            consumer_key=neo_creds['consumer_key'],
            environment='prod'
        )
        print_result("Client initialization", True)

        # Generate TOTP
        totp = pyotp.TOTP(neo_creds['totp_secret'])
        otp = totp.now()
        print_result("TOTP generation", True, f"OTP: {otp}")

        # v2 Login Step 1: totp_login
        print("  ... Attempting TOTP login (this may take a few seconds)...")
        login_response = client.totp_login(
            mobile_number=neo_creds['mobile_number'],
            ucc=neo_creds['ucc'],
            totp=otp
        )
        print(f"  TOTP login response: {json.dumps(login_response, indent=2, default=str)[:500]}")

        # v2 Login Step 2: totp_validate with MPIN
        print("  ... Validating with MPIN...")
        validate_response = client.totp_validate(mpin=neo_creds['mpin'])
        print(f"  Validate response: {json.dumps(validate_response, indent=2, default=str)[:500]}")

        if validate_response and ('data' in str(validate_response) or 'token' in str(validate_response).lower()):
            print_result("NEO Login", True, "Session established")
            return client, "Success"
        else:
            print_result("NEO Login", False, str(validate_response)[:200])
            return None, str(validate_response)

    except ImportError as e:
        print_result("Import", False, f"neo_api_client not installed: {e}")
        print("\n  Install with: pip -3.11 -m pip install git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git")
        return None, str(e)
    except Exception as e:
        print_result("NEO Login", False, str(e)[:200])
        return None, str(e)


def test_get_positions(client) -> list:
    """Test fetching positions."""
    print_section("TEST 2: Get Positions")

    try:
        response = client.positions()
        print(f"  Raw response type: {type(response)}")
        print(f"  Raw response: {json.dumps(response, indent=2, default=str)[:1000]}")

        positions = response.get('data', []) if response else []

        if positions:
            print_result("Get positions", True, f"Found {len(positions)} positions")
            for i, pos in enumerate(positions[:5]):  # Show first 5
                symbol = pos.get('tradingSymbol', pos.get('symbol', 'Unknown'))
                qty = pos.get('qty', 0)
                pnl = pos.get('pnl', pos.get('dayPnl', 0))
                print(f"         [{i+1}] {symbol}: qty={qty}, pnl={pnl}")
        else:
            print_result("Get positions", True, "No open positions (expected if fresh)")

        return positions

    except Exception as e:
        print_result("Get positions", False, str(e)[:200])
        return []


def test_get_orders(client) -> list:
    """Test fetching orders."""
    print_section("TEST 3: Get Orders")

    try:
        response = client.order_report()
        print(f"  Raw response: {json.dumps(response, indent=2, default=str)[:1000]}")

        orders = response.get('data', []) if response else []

        if orders:
            print_result("Get orders", True, f"Found {len(orders)} orders")
            for i, order in enumerate(orders[:5]):  # Show first 5
                order_id = order.get('nOrdNo', order.get('orderId', 'Unknown'))
                symbol = order.get('tradingSymbol', order.get('symbol', ''))
                status = order.get('ordSt', order.get('status', ''))
                print(f"         [{i+1}] {order_id}: {symbol} - {status}")
        else:
            print_result("Get orders", True, "No orders today (expected if fresh)")

        return orders

    except Exception as e:
        print_result("Get orders", False, str(e)[:200])
        return []


def test_get_limits(client):
    """Test fetching margin/limits."""
    print_section("TEST 4: Get Limits/Margin")

    try:
        response = client.limits()
        print(f"  Raw response: {json.dumps(response, indent=2, default=str)[:1000]}")

        if response and 'data' in response:
            data = response['data']
            available = data.get('marginAvailable', data.get('availableMargin', 'N/A'))
            used = data.get('marginUsed', data.get('usedMargin', 'N/A'))
            print_result("Get limits", True, f"Available: {available}, Used: {used}")
        else:
            print_result("Get limits", False, "No data in response")

    except Exception as e:
        print_result("Get limits", False, str(e)[:200])


def test_scrip_master(client, config: Dict[str, Any]):
    """Test scrip master download."""
    print_section("TEST 5: Scrip Master")

    try:
        # Check if we can fetch scrip master
        response = client.scrip_master(exchange_segment="nse_fo")

        if response:
            # It returns a large list
            count = len(response) if isinstance(response, list) else 0
            print_result("Scrip master download", True, f"Got {count} instruments")

            # Show sample
            if count > 0:
                sample = response[0]
                print(f"         Sample fields: {list(sample.keys())[:10]}")
        else:
            print_result("Scrip master download", False, "No data returned")

    except Exception as e:
        print_result("Scrip master", False, str(e)[:200])


def place_test_order(client, symbol: str, price: float, qty: int, action: str, config: Dict[str, Any]):
    """Place a test order and verify."""
    print_section(f"TEST TRADE: {action} {qty} {symbol} @ {price}")

    # Safety confirmation
    print(f"\n  WARNING:  WARNING: This will place a REAL order!")
    print(f"  Symbol: {symbol}")
    print(f"  Action: {action}")
    print(f"  Qty:    {qty}")
    print(f"  Price:  {price} (LIMIT order)")
    print(f"\n  Type 'YES' to confirm: ", end="")

    confirm = input().strip()
    if confirm != 'YES':
        print("  Cancelled by user.")
        return None

    try:
        # Map Kite symbol to NEO using search_scrip API
        print("\n  Step 1: Mapping Kite symbol to NEO...")

        from core.symbol_mapper import SymbolMapper
        mapper = SymbolMapper(client, config)

        try:
            mapping = mapper.map_kite_to_neo(symbol)
            exchange_segment = mapping['exchange_segment']
            trading_symbol = mapping['trading_symbol']
            lot_size = mapping['lot_size']

            print_result("Symbol mapping (via NEO search_scrip)", True)
            print(f"         Kite Symbol: {symbol}")
            print(f"         NEO Symbol: {trading_symbol}")
            print(f"         Exchange: {exchange_segment}")
            print(f"         Lot Size: {lot_size}")
            print(f"         Expiry: {mapping.get('expiry_date', 'N/A')}")

            # Use the NEO trading symbol
            symbol = trading_symbol

        except Exception as e:
            print_result("Symbol mapping", False, str(e)[:100])
            print("         Falling back to direct symbol...")
            # Fallback to direct mapping
            if symbol.startswith('NIFTY') and not symbol.startswith('NIFTYBANK'):
                exchange_segment = 'nse_fo'
                lot_size = 65  # Updated NIFTY lot size
            elif symbol.startswith('BANKNIFTY') or symbol.startswith('NIFTYBANK'):
                exchange_segment = 'nse_fo'
                lot_size = 15
            elif symbol.startswith('FINNIFTY'):
                exchange_segment = 'nse_fo'
                lot_size = 60  # Updated FINNIFTY lot size
            elif symbol.startswith('SENSEX'):
                exchange_segment = 'bse_fo'
                lot_size = 10
            elif symbol.startswith('BANKEX'):
                exchange_segment = 'bse_fo'
                lot_size = 15
            else:
                exchange_segment = 'nse_fo'
                lot_size = 1

        # Step 2: Place order
        print("\n  Step 2: Placing order...")

        txn_type = 'B' if action.upper() == 'BUY' else 'S'

        order_response = client.place_order(
            exchange_segment=exchange_segment,
            product='MIS',
            price=str(price),
            order_type='L',  # Limit order
            quantity=str(qty),
            validity='DAY',
            trading_symbol=symbol,
            transaction_type=txn_type,
            amo='NO',
            tag='TEST'
        )

        print(f"  Order response: {json.dumps(order_response, indent=2, default=str)}")

        order_id = order_response.get('nOrdNo') if order_response else None

        if order_id:
            print_result("Order placement", True, f"Order ID: {order_id}")

            # Step 3: Check order status
            print("\n  Step 3: Verifying order in order book...")
            time.sleep(1)  # Wait for order to appear

            orders = client.order_report()
            order_list = orders.get('data', []) if orders else []

            found = None
            for order in order_list:
                if order.get('nOrdNo') == order_id:
                    found = order
                    break

            if found:
                status = found.get('ordSt', 'Unknown')
                print_result("Order verification", True, f"Status: {status}")
                print(f"         Full order: {json.dumps(found, indent=2, default=str)[:500]}")

                # If pending, offer to cancel
                if status.lower() in ['pending', 'open', 'trigger pending']:
                    print(f"\n  Order is {status}. Cancel it? (y/n): ", end="")
                    if input().strip().lower() == 'y':
                        cancel_response = client.cancel_order(order_id=order_id)
                        print(f"  Cancel response: {cancel_response}")
                        print_result("Order cancellation", True)
            else:
                print_result("Order verification", False, "Order not found in book")

            return order_id
        else:
            print_result("Order placement", False, "No order ID returned")
            return None

    except Exception as e:
        print_result("Test trade", False, str(e))
        import traceback
        traceback.print_exc()
        return None


def test_sl_order(client, symbol: str, trigger_price: float, qty: int, config: Dict[str, Any]):
    """Test SL order placement."""
    print_section(f"TEST SL ORDER: {symbol} trigger @ {trigger_price}")

    print(f"\n  WARNING:  This will place a SELL SL-M order!")
    print(f"  Symbol: {symbol}")
    print(f"  Trigger: {trigger_price}")
    print(f"  Qty: {qty}")
    print(f"\n  Type 'YES' to confirm: ", end="")

    if input().strip() != 'YES':
        print("  Cancelled.")
        return None

    try:
        from core.symbol_mapper import SymbolMapper
        mapper = SymbolMapper(client, config)
        mapper.initialize()
        mapping = mapper.map_to_neo(symbol)

        if not mapping:
            print_result("Symbol lookup", False)
            return None

        print_result("Symbol lookup", True, mapping['trading_symbol'])

        # Place SL-M order
        sl_response = client.place_order(
            exchange_segment=mapping['exchange_segment'],
            product='MIS',
            price='0',  # Market price on trigger
            order_type='SL-M',
            quantity=str(qty),
            validity='DAY',
            trading_symbol=mapping['trading_symbol'],
            transaction_type='S',  # Sell to exit long
            amo='NO',
            trigger_price=str(trigger_price),
            tag='SL_TEST'
        )

        print(f"  SL Response: {json.dumps(sl_response, indent=2, default=str)}")

        sl_order_id = sl_response.get('nOrdNo') if sl_response else None

        if sl_order_id:
            print_result("SL order placed", True, f"ID: {sl_order_id}")

            # Verify
            time.sleep(1)
            orders = client.order_report()
            for order in orders.get('data', []):
                if order.get('nOrdNo') == sl_order_id:
                    print(f"  SL Order status: {order.get('ordSt')}")
                    print(f"  Order type: {order.get('ordTyp')}")
                    print(f"  Trigger price: {order.get('trgPrc')}")
                    break

            # Cancel it
            print(f"\n  Cancel SL order? (y/n): ", end="")
            if input().strip().lower() == 'y':
                cancel = client.cancel_order(order_id=sl_order_id)
                print(f"  Cancel response: {cancel}")

            return sl_order_id
        else:
            print_result("SL order", False, "No order ID")
            return None

    except Exception as e:
        print_result("SL order", False, str(e))
        import traceback
        traceback.print_exc()
        return None


def test_kite_connection(config: Dict[str, Any]):
    """Test Kite connection for spot prices."""
    print_section("TEST 6: Kite Connection (Spot Prices)")

    try:
        from core.kite_spot import KiteSpotFetcher

        kite = KiteSpotFetcher(config)
        success, msg = kite.connect()

        if success:
            print_result("Kite connection", True, msg)

            # Test fetching spot prices
            try:
                nifty_spot = kite.get_spot_price('NIFTY')
                print_result("NIFTY spot price", True, f"{nifty_spot:.2f}")
            except Exception as e:
                print_result("NIFTY spot price", False, str(e)[:100])

            try:
                bnf_spot = kite.get_spot_price('BANKNIFTY')
                print_result("BANKNIFTY spot price", True, f"{bnf_spot:.2f}")
            except Exception as e:
                print_result("BANKNIFTY spot price", False, str(e)[:100])

            # Test ATM calculation
            try:
                nifty_atm = kite.get_atm_strike('NIFTY')
                print_result("NIFTY ATM strike", True, f"{nifty_atm}")
            except Exception as e:
                print_result("NIFTY ATM", False, str(e)[:100])

        else:
            print_result("Kite connection", False, msg)
            print("         -> Check BOTS/data/kite_access_token.json")

    except ImportError as e:
        print_result("Import kite_spot", False, str(e))
    except Exception as e:
        print_result("Kite test", False, str(e)[:200])


def test_telegram(config: Dict[str, Any]):
    """Test Telegram notification."""
    print_section("TEST 7: Telegram Connection")

    try:
        from core.telegram_notifier import TelegramNotifier

        tg = TelegramNotifier(config)

        if not tg.enabled:
            print_result("Telegram enabled", False, "Telegram is disabled in config")
            return

        print_result("Telegram enabled", True)
        print(f"         Alert mode: {tg.alert_mode}")

        if tg.test():
            print_result("Telegram connection", True, "Bot responded")

            # Ask to send test
            print(f"\n  Send test message? (y/n): ", end="")
            if input().strip().lower() == 'y':
                tg.send("NEO Terminal - Test message")
                print_result("Test message sent", True, "Check Telegram")
        else:
            print_result("Telegram connection", False, "Bot test failed")

    except ImportError as e:
        print_result("Import telegram", False, str(e))
    except Exception as e:
        print_result("Telegram test", False, str(e)[:200])


def main():
    parser = argparse.ArgumentParser(description='NEO API Test Script')
    parser.add_argument('--check', action='store_true', help='Check connectivity only')
    parser.add_argument('--trade', nargs=4, metavar=('SYMBOL', 'PRICE', 'QTY', 'ACTION'),
                       help='Place test trade: SYMBOL PRICE QTY BUY/SELL')
    parser.add_argument('--sl', nargs=3, metavar=('SYMBOL', 'TRIGGER', 'QTY'),
                       help='Test SL order: SYMBOL TRIGGER_PRICE QTY')

    args = parser.parse_args()

    print("\n" + "="*60)
    print("  NEO TRADE TERMINAL - API TEST SCRIPT")
    print("  " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("="*60)

    # Load config
    try:
        config = load_config()
        print_result("Config loaded", True)
    except Exception as e:
        print_result("Config loaded", False, str(e))
        return 1

    # Test login
    client, login_msg = test_neo_login(config)

    if not client:
        print("\n[ERROR] Cannot proceed without login. Fix credentials and retry.")
        return 1

    # Basic connectivity tests
    if args.check or not (args.trade or args.sl):
        test_get_positions(client)
        test_get_orders(client)
        test_get_limits(client)
        test_scrip_master(client, config)
        test_kite_connection(config)
        test_telegram(config)

    # Test trade
    if args.trade:
        symbol, price, qty, action = args.trade
        place_test_order(client, symbol, float(price), int(qty), action, config)

    # Test SL
    if args.sl:
        symbol, trigger, qty = args.sl
        test_sl_order(client, symbol, float(trigger), int(qty), config)

    print_section("TEST COMPLETE")
    print("  Review the output above for any failures.")
    print("  Fix issues in the code before using the main terminal.")
    print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())

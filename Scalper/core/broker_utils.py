"""
NEO Trade Terminal - Broker Response Utilities

Centralized extraction of position/order fields from NEO API responses.
All modules MUST use these helpers instead of inline extraction to prevent
field-name mismatches across the codebase.

NEO API quirks:
- Primary symbol field is 'trdSym' (not 'tradingSymbol')
- Quantity is split: flBuyQty / flSellQty (not a single 'qty')
- Average price may need to be calculated from buyAmt / flBuyQty
- Exchange segment field is 'exSeg'
- Product field is 'prod'
"""

from typing import Any, Dict


def get_symbol(pos: Dict[str, Any]) -> str:
    """Extract trading symbol from NEO API position/order response."""
    for field in ['trdSym', 'tradingSymbol', 'symbol', 'tsym', 'scrip', 'scripName']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return ''


def get_net_qty(pos: Dict[str, Any]) -> int:
    """Extract NET position quantity from NEO API response.

    NEO API returns flBuyQty and flSellQty separately.
    Net = flBuyQty - flSellQty (positive = long, negative = short, 0 = closed).
    """
    # Primary: Calculate from buy/sell quantities
    buy_qty = pos.get('flBuyQty', pos.get('buyQty', 0))
    sell_qty = pos.get('flSellQty', pos.get('sellQty', 0))
    primary_parsed = False
    try:
        parsed_buy = int(buy_qty or 0)
        parsed_sell = int(sell_qty or 0)
        net = parsed_buy - parsed_sell
        primary_parsed = (parsed_buy > 0 or parsed_sell > 0)
        if net != 0:
            return net
        # If net==0 AND we successfully parsed both sides, position is closed - return 0
        if primary_parsed:
            return 0
    except (ValueError, TypeError):
        pass

    # Fallback: direct qty field
    qty = pos.get('qty')
    if qty is not None and str(qty).strip():
        try:
            return int(qty)
        except (ValueError, TypeError):
            # Handle "150.0" strings
            try:
                return int(float(qty))
            except (ValueError, TypeError):
                pass

    # Fallback: netQty
    net_qty = pos.get('netQty')
    if net_qty is not None and str(net_qty).strip():
        try:
            return int(net_qty)
        except (ValueError, TypeError):
            try:
                return int(float(net_qty))
            except (ValueError, TypeError):
                pass

    return 0


def get_avg_price(pos: Dict[str, Any]) -> float:
    """Extract/calculate average price from NEO API position response.

    NEO API may not provide avgPrice directly — calculate from buyAmt/flBuyQty.
    """
    for field in ['averagePrice', 'avgPrc', 'avgPrice', 'buyAvgPrc', 'netAvgPrc']:
        val = pos.get(field)
        if val is not None:
            try:
                price = float(val)
                if price > 0:
                    return price
            except (ValueError, TypeError):
                pass

    # Calculate from amounts / quantities based on position direction
    # Determine direction: if net qty > 0, LONG (use buy side); if < 0, SHORT (use sell side)
    try:
        buy_q = int(pos.get('flBuyQty', 0) or 0)
        sell_q = int(pos.get('flSellQty', 0) or 0)
        net_q = buy_q - sell_q

        if net_q > 0:
            # LONG position - use buy side avg
            buy_amt = float(pos.get('buyAmt', 0) or 0)
            if buy_q > 0 and buy_amt > 0:
                return round(buy_amt / buy_q, 2)
        elif net_q < 0:
            # SHORT position - use sell side avg
            sell_amt = float(pos.get('sellAmt', 0) or 0)
            if sell_q > 0 and sell_amt > 0:
                return round(sell_amt / sell_q, 2)
    except (ValueError, TypeError):
        pass

    # Last resort fallback: try buy side anyway
    try:
        buy_amt = float(pos.get('buyAmt', 0) or 0)
        buy_qty = int(pos.get('flBuyQty', 0) or 0)
        if buy_qty > 0 and buy_amt > 0:
            return round(buy_amt / buy_qty, 2)
    except (ValueError, TypeError):
        pass

    return 0.0


def get_ltp(pos: Dict[str, Any]) -> float:
    """Extract LTP from NEO API position response.

    Note: NEO positions API rarely returns LTP — it comes from websocket/quotes.
    """
    for field in ['ltp', 'lastPrice', 'lp', 'lastTradedPrice', 'ltP']:
        val = pos.get(field)
        if val is not None:
            try:
                price = float(val)
                if price > 0:
                    return price
            except (ValueError, TypeError):
                pass
    return 0.0


def get_pnl(pos: Dict[str, Any]) -> float:
    """Extract/calculate P&L from NEO API position response.

    For closed positions, P&L = sellAmt - buyAmt.
    For open positions, need LTP to calculate MTM.
    """
    for field in ['pnl', 'dayPnl', 'mtm', 'realizedPnl', 'urmtom', 'unrealizedPnl', 'netPnl']:
        val = pos.get(field)
        if val is not None:
            try:
                pnl = float(val)
                # Return non-zero immediately; for zero, continue to check other fields
                # in case this field is uninitialized while another has the real value
                if pnl != 0:
                    return pnl
            except (ValueError, TypeError):
                pass

    # Calculate realized P&L from sellAmt - buyAmt
    try:
        buy_amt = float(pos.get('buyAmt', 0) or 0)
        sell_amt = float(pos.get('sellAmt', 0) or 0)
        if sell_amt > 0:
            return round(sell_amt - buy_amt, 2)
    except (ValueError, TypeError):
        pass

    return 0.0


def get_raw_pnl(pos: Dict[str, Any]) -> float:
    """Extract P&L preserving zero values (for circuit breaker / summing).

    Unlike get_pnl(), this returns the first non-None value even if it's 0.0.
    Use this when summing P&L across all positions (break-even = 0 is a valid value).
    """
    for field in ['pnl', 'dayPnl', 'unrealizedPnl', 'mtm', 'realizedPnl', 'urmtom', 'netPnl']:
        val = pos.get(field)
        if val is not None:
            try:
                return float(val)
            except (ValueError, TypeError):
                pass
    return 0.0


def get_product(pos: Dict[str, Any], default: str = 'MIS') -> str:
    """Extract product type from NEO API position response."""
    for field in ['prod', 'product', 'prd', 'productType']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip().upper()
    return default


def get_exchange(pos: Dict[str, Any], default: str = 'nse_fo') -> str:
    """Extract exchange segment from NEO API position response."""
    for field in ['exSeg', 'exchange_segment', 'exchangeSegment', 'exchange', 'exch']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return default


def get_token(pos: Dict[str, Any]) -> str:
    """Extract instrument token from NEO API position response."""
    for field in ['tok', 'token', 'instrument_token', 'instrumentToken', 'pSymbol']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return ''

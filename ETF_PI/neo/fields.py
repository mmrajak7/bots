"""
neo.fields — Neo API response field extraction.

All modules MUST use these helpers instead of inline field access.
Neo API returns different field names across endpoints and versions.

Adapted from Scalper core/broker_utils.py.
"""


def get_order_id(resp):
    """Extract order ID from place_order or order_report response."""
    if not resp:
        return None
    # Try nested result first
    if isinstance(resp, dict):
        for wrapper in ('data', 'result'):
            inner = resp.get(wrapper)
            if isinstance(inner, dict):
                for f in ('nOrdNo', 'orderId', 'order_id', 'orderNo'):
                    v = inner.get(f)
                    if v:
                        return str(v)
    for f in ('nOrdNo', 'orderId', 'order_id', 'orderNo'):
        v = resp.get(f)
        if v:
            return str(v)
    return None


def get_order_status(order):
    """Normalize order status to: 'open', 'complete', 'rejected', 'cancelled', 'trigger_pending'.

    Handles both v1 and v2 field names and value formats.
    """
    if not order:
        return 'unknown'
    raw = ''
    for f in ('ordSt', 'status', 'orderStatus', 'ordStatus'):
        v = order.get(f)
        if v:
            raw = str(v).lower().strip()
            break
    if not raw:
        return 'unknown'
    if raw in ('complete', 'traded', 'filled'):
        return 'complete'
    if raw in ('rejected',):
        return 'rejected'
    if raw in ('cancelled', 'canceled'):
        return 'cancelled'
    if raw in ('trigger pending', 'trigger_pending'):
        return 'trigger_pending'
    if raw in ('open', 'pending', 'after market order req received',
               'put order req received', 'validation pending',
               'open pending', 'modify pending'):
        return 'open'
    return raw


def get_order_tag(order):
    """Extract tag/remarks from order."""
    if not order:
        return ''
    for f in ('remarks', 'tag', 'Remarks', 'algoBkRmrks'):
        v = order.get(f)
        if v and str(v).strip():
            return str(v).strip()
    return ''


def get_avg_fill_price(order):
    """Extract average fill price from order."""
    if not order:
        return 0.0
    for f in ('avgPrc', 'avgPrice', 'averagePrice', 'average_price'):
        v = order.get(f)
        if v is not None:
            try:
                p = float(v)
                if p > 0:
                    return p
            except (ValueError, TypeError):
                pass
    return 0.0


def get_filled_qty(order):
    """Extract filled quantity from order."""
    if not order:
        return 0
    for f in ('fldQty', 'filledQty', 'tradedQty', 'filled_quantity'):
        v = order.get(f)
        if v is not None:
            try:
                q = int(float(v))
                if q > 0:
                    return q
            except (ValueError, TypeError):
                pass
    return 0


def get_pending_qty(order):
    """Extract pending (unfilled) quantity from order."""
    if not order:
        return 0
    for f in ('unfilledSize', 'pendingQty', 'remaining_quantity'):
        v = order.get(f)
        if v is not None:
            try:
                return int(float(v))
            except (ValueError, TypeError):
                pass
    # Fallback: total - filled
    total = 0
    for f in ('qty', 'quantity', 'orderQty'):
        v = order.get(f)
        if v is not None:
            try:
                total = int(float(v))
                break
            except (ValueError, TypeError):
                pass
    filled = get_filled_qty(order)
    return max(total - filled, 0) if total > 0 else 0


def get_symbol(pos):
    """Extract trading symbol from position/order response."""
    for f in ('trdSym', 'tradingSymbol', 'symbol', 'tsym', 'scrip', 'scripName'):
        v = pos.get(f)
        if v and str(v).strip():
            return str(v).strip()
    return ''


def get_net_qty(pos):
    """Extract net position quantity. Positive=long, negative=short, 0=closed."""
    buy_qty = pos.get('flBuyQty', pos.get('buyQty', 0))
    sell_qty = pos.get('flSellQty', pos.get('sellQty', 0))
    try:
        b = int(buy_qty or 0)
        s = int(sell_qty or 0)
        if b > 0 or s > 0:
            return b - s
    except (ValueError, TypeError):
        pass
    for f in ('qty', 'netQty', 'quantity'):
        v = pos.get(f)
        if v is not None:
            try:
                return int(float(v))
            except (ValueError, TypeError):
                pass
    return 0


def get_pnl(pos):
    """Extract P&L from position response."""
    for f in ('pnl', 'dayPnl', 'mtm', 'realizedPnl', 'urmtom', 'unrealizedPnl', 'netPnl'):
        v = pos.get(f)
        if v is not None:
            try:
                pnl = float(v)
                if pnl != 0:
                    return pnl
            except (ValueError, TypeError):
                pass
    # Calculate from sell - buy amounts
    try:
        buy_amt = float(pos.get('buyAmt', 0) or 0)
        sell_amt = float(pos.get('sellAmt', 0) or 0)
        if sell_amt > 0:
            return round(sell_amt - buy_amt, 2)
    except (ValueError, TypeError):
        pass
    return 0.0


def get_raw_pnl(pos):
    """Extract P&L preserving zero (for summing across positions)."""
    for f in ('pnl', 'dayPnl', 'unrealizedPnl', 'mtm', 'realizedPnl', 'urmtom', 'netPnl'):
        v = pos.get(f)
        if v is not None:
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return 0.0


def get_exchange(pos, default='nse_fo'):
    """Extract exchange segment from response."""
    for f in ('exSeg', 'exchange_segment', 'exchangeSegment', 'exchange', 'exch'):
        v = pos.get(f)
        if v and str(v).strip():
            return str(v).strip()
    return default

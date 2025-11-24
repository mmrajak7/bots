"""Kite API Constants - Order Statuses and Types"""


class KiteOrderStatus:
    """
    Kite Connect API Order Status Constants

    Based on official Kite Connect v3 documentation:
    https://kite.trade/docs/connect/v3/orders/
    """

    # Terminal statuses (order lifecycle complete)
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    # Active statuses (order still in progress)
    OPEN = "OPEN"
    TRIGGER_PENDING = "TRIGGER PENDING"
    OPEN_PENDING = "OPEN PENDING"
    VALIDATION_PENDING = "VALIDATION PENDING"
    PUT_ORDER_REQ_RECEIVED = "PUT ORDER REQ RECEIVED"
    MODIFY_PENDING = "MODIFY PENDING"

    # Status groups
    FINAL_STATUSES = [COMPLETE, CANCELLED, REJECTED]
    ACTIVE_STATUSES = [
        OPEN,
        TRIGGER_PENDING,
        OPEN_PENDING,
        VALIDATION_PENDING,
        PUT_ORDER_REQ_RECEIVED,
        MODIFY_PENDING
    ]

    @classmethod
    def is_final(cls, status: str) -> bool:
        """Check if order status is final (terminal state)"""
        return status.upper() in cls.FINAL_STATUSES

    @classmethod
    def is_active(cls, status: str) -> bool:
        """Check if order is still active (not final)"""
        return status.upper() in cls.ACTIVE_STATUSES


class KiteProductType:
    """
    Kite Connect API Product Types

    Product types determine the type of position created
    """
    CNC = "CNC"      # Cash and Carry (Delivery)
    MIS = "MIS"      # Margin Intraday Square-off
    NRML = "NRML"    # Normal (for F&O)
    MTF = "MTF"      # Margin Trading Facility


class KiteTransactionType:
    """
    Kite Connect API Transaction Types

    Direction of the trade
    """
    BUY = "BUY"
    SELL = "SELL"


class KiteOrderType:
    """
    Kite Connect API Order Types

    Different types of orders supported by Kite
    """
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"          # Stoploss
    SL_M = "SL-M"      # Stoploss Market


class KiteValidity:
    """
    Kite Connect API Order Validity Types

    How long the order remains active
    """
    DAY = "DAY"    # Valid for the day
    IOC = "IOC"    # Immediate or Cancel
    TTL = "TTL"    # Time to Live (with validity_ttl parameter)


class KiteExchange:
    """
    Kite Connect API Supported Exchanges
    """
    NSE = "NSE"    # National Stock Exchange
    BSE = "BSE"    # Bombay Stock Exchange
    NFO = "NFO"    # NSE Futures and Options
    CDS = "CDS"    # Currency Derivatives Segment
    BCD = "BCD"    # BSE Currency Derivatives
    MCX = "MCX"    # Multi Commodity Exchange

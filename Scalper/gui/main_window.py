"""
NEO Trade Terminal - Main GUI Window

PyQt6-based trading interface with:
- Quick entry panel with symbol mapping
- Positions table with trailing SL controls
- Order book display
- Basket order builder
- Keyboard shortcuts for fast execution
"""

import logging
import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QGroupBox, QTextEdit, QRadioButton, QButtonGroup,
    QFrame, QSplitter, QHeaderView, QSpinBox, QMessageBox
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QThread
from PyQt6.QtGui import QFont, QColor, QKeySequence, QShortcut, QBrush

from core.order_manager import OrderParams, OrderManager
from core.trailing_sl import TrailingSLManager, TrailMode
from core.position_tracker import PositionTracker
from core.charge_calculator import get_breakeven_points, calculate_charges


class SortableTableWidgetItem(QTableWidgetItem):
    """Custom QTableWidgetItem that sorts by UserRole data when available.

    This enables storing a sortable value (e.g., full timestamp) separately
    from the display text (e.g., HH:MM).
    """
    def __lt__(self, other):
        # Get UserRole data for sorting, fall back to display text
        self_data = self.data(Qt.ItemDataRole.UserRole)
        other_data = other.data(Qt.ItemDataRole.UserRole) if hasattr(other, 'data') else None

        if self_data is not None and other_data is not None:
            return str(self_data) < str(other_data)
        return super().__lt__(other)


def _get_position_qty(pos: Dict[str, Any]) -> int:
    """Extract NET position quantity from NEO API response.

    NEO API returns flBuyQty and flSellQty separately.
    Net position = flBuyQty - flSellQty (positive = long, negative = short, 0 = closed)
    """
    # Calculate from buy/sell quantities (NEO API primary method)
    buy_qty = pos.get('flBuyQty', pos.get('buyQty', 0))
    sell_qty = pos.get('flSellQty', pos.get('sellQty', 0))
    try:
        net = int(buy_qty or 0) - int(sell_qty or 0)
        if net != 0:
            return net
    except (ValueError, TypeError):
        pass

    # Fallback: Try direct qty field
    qty = pos.get('qty')
    if qty is not None and str(qty).strip():
        try:
            return int(qty)
        except (ValueError, TypeError):
            pass

    # Fallback: Try netQty
    net_qty = pos.get('netQty')
    if net_qty is not None and str(net_qty).strip():
        try:
            return int(net_qty)
        except (ValueError, TypeError):
            pass

    return 0


def _get_position_symbol(pos: Dict[str, Any]) -> str:
    """Extract trading symbol from NEO API position response."""
    # NEO API uses 'trdSym' as primary field
    for field in ['trdSym', 'tradingSymbol', 'symbol', 'tsym', 'scrip', 'scripName']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return ''


def _get_position_avg_price(pos: Dict[str, Any]) -> float:
    """Extract/calculate average price from NEO API position response.

    NEO API doesn't provide avgPrice directly - calculate from buyAmt/flBuyQty.
    """
    # Try direct fields first
    for field in ['averagePrice', 'avgPrc', 'avgPrice', 'buyAvgPrc', 'netAvgPrc']:
        val = pos.get(field)
        if val is not None:
            try:
                price = float(val)
                if price > 0:
                    return price
            except (ValueError, TypeError):
                pass

    # NEO API: Calculate from buyAmt / flBuyQty
    try:
        buy_amt = float(pos.get('buyAmt', 0) or 0)
        buy_qty = int(pos.get('flBuyQty', 0) or 0)
        if buy_qty > 0 and buy_amt > 0:
            return round(buy_amt / buy_qty, 2)
    except (ValueError, TypeError):
        pass

    return 0.0


def _get_position_ltp(pos: Dict[str, Any]) -> float:
    """Extract LTP from NEO API position response.

    Note: NEO positions API doesn't return LTP - it comes from websocket/quotes.
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


def _get_position_pnl(pos: Dict[str, Any]) -> float:
    """Extract/calculate P&L from NEO API position response.

    NEO API: For closed positions, P&L = sellAmt - buyAmt
    For open positions, need LTP to calculate MTM.
    """
    # Try direct P&L fields first
    for field in ['pnl', 'dayPnl', 'mtm', 'realizedPnl', 'urmtom', 'unrealizedPnl', 'netPnl']:
        val = pos.get(field)
        if val is not None:
            try:
                pnl = float(val)
                if pnl != 0:
                    return pnl
            except (ValueError, TypeError):
                pass

    # NEO API: Calculate realized P&L from sellAmt - buyAmt
    try:
        buy_amt = float(pos.get('buyAmt', 0) or 0)
        sell_amt = float(pos.get('sellAmt', 0) or 0)
        if sell_amt > 0:  # Has some sells
            return round(sell_amt - buy_amt, 2)
    except (ValueError, TypeError):
        pass

    return 0.0


def _get_position_product(pos: Dict[str, Any]) -> str:
    """Extract product type from NEO API position response."""
    # NEO API uses 'prod'
    for field in ['prod', 'product', 'prd', 'productType']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip().upper()
    return 'MIS'


def _get_position_exchange(pos: Dict[str, Any]) -> str:
    """Extract exchange segment from NEO API position response."""
    # NEO API uses 'exSeg'
    for field in ['exSeg', 'exchange_segment', 'exchangeSegment', 'exchange', 'exch']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return 'nse_fo'


def _get_position_token(pos: Dict[str, Any]) -> str:
    """Extract instrument token from NEO API position response."""
    for field in ['tok', 'token', 'instrument_token', 'instrumentToken']:
        val = pos.get(field)
        if val and str(val).strip():
            return str(val).strip()
    return ''


class MainWindow(QMainWindow):
    """Main trading terminal window."""

    # Signals for thread-safe GUI updates
    position_updated = pyqtSignal(list)
    order_updated = pyqtSignal(list)
    log_message = pyqtSignal(str)
    margin_updated = pyqtSignal(float)
    pnl_updated = pyqtSignal(float)
    ws_order_update = pyqtSignal(dict)  # WebSocket order update signal

    def __init__(self, session_mgr, order_mgr: OrderManager, symbol_mapper,
                 kite_spot, config: Dict[str, Any],
                 sound_mgr=None, telegram_mgr=None, trade_logger=None,
                 trail_mgr: TrailingSLManager = None, oco_monitor=None,
                 ws_handler=None, partial_fill_monitor=None, pnl_tracker=None,
                 cancel_mgr=None, rejection_learner=None):
        super().__init__()

        # Core components
        self.session = session_mgr
        self.orders = order_mgr
        self.mapper = symbol_mapper
        self.kite = kite_spot
        self.config = config
        self.sound = sound_mgr
        self.telegram = telegram_mgr
        self.logger = trade_logger
        self.trail_mgr = trail_mgr
        self.oco_monitor = oco_monitor
        self.ws_handler = ws_handler
        self.partial_fill_monitor = partial_fill_monitor
        self.pnl_tracker = pnl_tracker
        self.cancel_mgr = cancel_mgr
        self.rejection_learner = rejection_learner

        # State
        self.basket_legs: List[Dict[str, Any]] = []
        self.current_mapping: Optional[Dict[str, Any]] = None
        self.sl_inputs: Dict[str, QLineEdit] = {}
        self._positions_cache: List[Dict[str, Any]] = []
        self._filtered_positions_cache: List[Dict[str, Any]] = []

        # Position tracker for SL/Target order management (with cancel manager)
        self.pos_tracker = PositionTracker(
            session_mgr.get_client() if session_mgr else None,
            order_mgr,
            cancel_mgr
        )
        # Wire up OCO monitor reference for automatic cleanup on position removal
        if oco_monitor:
            self.pos_tracker.oco_monitor = oco_monitor

        # Setup UI
        self.init_ui()
        self.setup_shortcuts()
        self.setup_timers()
        self.connect_signals()

        # Initial data load
        QTimer.singleShot(500, self.refresh_all)

        # Session recovery - sync existing broker positions
        QTimer.singleShot(1500, self._recover_existing_positions)

    def init_ui(self):
        """Initialize the user interface - 3 column layout."""
        self.setWindowTitle("Kayal v1.0")
        self.setMinimumSize(1500, 900)
        self.setStyleSheet(self._get_dark_theme())

        # Central widget
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(4)
        layout.setContentsMargins(6, 6, 6, 6)

        # Status bar at top (includes Total P&L, EXIT ALL / CANCEL ALL)
        layout.addWidget(self._create_status_bar())

        # Quick entry panel (3 rows with F1-F4)
        layout.addWidget(self._create_quick_entry_panel())

        # LAYOUT: Horizontal split - Left (Positions + Basket/Orders) | Right (Logs full height)
        main_hsplit = QSplitter(Qt.Orientation.Horizontal)

        # LEFT SIDE: Positions on top, Basket/Orders below
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)

        # Positions panel (top)
        left_layout.addWidget(self._create_positions_panel())

        # Basket | Orders (bottom, horizontal split)
        bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        bottom_splitter.addWidget(self._create_basket_panel())
        bottom_splitter.addWidget(self._create_order_book_panel())
        bottom_splitter.setSizes([380, 550])  # Basket | Orders
        left_layout.addWidget(bottom_splitter, 1)

        main_hsplit.addWidget(left_widget)

        # RIGHT SIDE: Logs (full height, compact width)
        main_hsplit.addWidget(self._create_log_panel())

        # Set widths: Left 78% | Logs 22%
        main_hsplit.setSizes([1050, 300])

        layout.addWidget(main_hsplit, 1)

    def _create_status_bar(self) -> QFrame:
        """Create top status bar with margin, P&L, BUY/SELL buttons, and actions."""
        frame = QFrame()
        frame.setFrameStyle(QFrame.Shape.StyledPanel)
        frame.setMaximumHeight(50)
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(8, 4, 8, 4)

        # Connection status
        self.conn_status = QLabel("🟢 LIVE")
        self.conn_status.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        layout.addWidget(self.conn_status)

        layout.addWidget(self._create_separator())

        # Available Margin
        lbl = QLabel("Margin:")
        lbl.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(lbl)
        self.margin_label = QLabel("₹0")
        self.margin_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.margin_label.setStyleSheet("color: #00ff88;")
        self.margin_label.setToolTip("Available margin for trading")
        layout.addWidget(self.margin_label)

        layout.addWidget(self._create_separator())

        # Total P&L (Open + Closed)
        lbl2 = QLabel("Total P&L:")
        lbl2.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(lbl2)
        self.pnl_label = QLabel("₹0")
        self.pnl_label.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.pnl_label.setToolTip("Total P&L = Open positions + Closed trades")
        layout.addWidget(self.pnl_label)

        layout.addWidget(self._create_separator())

        # Time
        self.time_label = QLabel("00:00:00")
        self.time_label.setFont(QFont("Consolas", 9))
        self.time_label.setStyleSheet("color: #888888;")
        layout.addWidget(self.time_label)

        # Trading halt indicator
        self.halt_label = QLabel("")
        self.halt_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        layout.addWidget(self.halt_label)

        layout.addStretch()

        # BIG BUY/SELL/BASKET buttons in header
        layout.addWidget(self._create_separator())

        self.header_buy_btn = QPushButton("BUY")
        self.header_buy_btn.setFixedSize(70, 36)
        self.header_buy_btn.setStyleSheet("""
            QPushButton {
                background-color: #00aa44; color: white;
                font-weight: bold; font-size: 14px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #00cc55; }
        """)
        self.header_buy_btn.setToolTip("Place BUY order (Ctrl+B)")
        self.header_buy_btn.clicked.connect(lambda: self._place_quick_order('B'))
        layout.addWidget(self.header_buy_btn)

        self.header_sell_btn = QPushButton("SELL")
        self.header_sell_btn.setFixedSize(70, 36)
        self.header_sell_btn.setStyleSheet("""
            QPushButton {
                background-color: #cc2222; color: white;
                font-weight: bold; font-size: 14px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #ee3333; }
        """)
        self.header_sell_btn.setToolTip("Place SELL order (Ctrl+S)")
        self.header_sell_btn.clicked.connect(lambda: self._place_quick_order('S'))
        layout.addWidget(self.header_sell_btn)

        self.header_basket_btn = QPushButton("+BASKET")
        self.header_basket_btn.setFixedSize(80, 36)
        self.header_basket_btn.setStyleSheet("""
            QPushButton {
                background-color: #5566cc; color: white;
                font-weight: bold; font-size: 12px; border-radius: 4px;
            }
            QPushButton:hover { background-color: #6677dd; }
        """)
        self.header_basket_btn.setToolTip("Add to basket")
        self.header_basket_btn.clicked.connect(self._add_to_basket)
        layout.addWidget(self.header_basket_btn)

        layout.addWidget(self._create_separator())

        # Expiry type selector
        lbl3 = QLabel("Expiry:")
        lbl3.setStyleSheet("color: #aaaaaa;")
        layout.addWidget(lbl3)
        self.expiry_combo = QComboBox()
        self.expiry_combo.addItems(["Monthly", "Weekly"])
        self.expiry_combo.setCurrentText("Monthly")
        self.expiry_combo.setToolTip("Monthly: All indices\nWeekly: NIFTY & SENSEX only")
        self.expiry_combo.setStyleSheet("""
            QComboBox {
                background-color: #333355;
                color: #00ff88;
                font-weight: bold;
                padding: 2px 6px;
                min-width: 75px;
            }
        """)
        layout.addWidget(self.expiry_combo)

        layout.addWidget(self._create_separator())

        # Exit All button
        self.exit_all_btn = QPushButton("EXIT ALL")
        self.exit_all_btn.setFixedSize(80, 30)
        self.exit_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #aa0000; color: white;
                font-weight: bold; font-size: 10px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #cc0000; }
        """)
        self.exit_all_btn.setToolTip("Exit all positions (F9)")
        self.exit_all_btn.clicked.connect(self._exit_all_positions)
        layout.addWidget(self.exit_all_btn)

        # Cancel All button
        self.cancel_all_btn = QPushButton("CANCEL ALL")
        self.cancel_all_btn.setFixedSize(90, 30)
        self.cancel_all_btn.setStyleSheet("""
            QPushButton {
                background-color: #885500; color: white;
                font-weight: bold; font-size: 10px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #aa6600; }
        """)
        self.cancel_all_btn.setToolTip("Cancel all pending orders (F10)")
        self.cancel_all_btn.clicked.connect(self._cancel_all_orders)
        layout.addWidget(self.cancel_all_btn)

        return frame

    def _create_quick_entry_panel(self) -> QGroupBox:
        """Create compact order entry panel with high contrast colors."""
        group = QGroupBox("QUICK ENTRY")
        layout = QVBoxLayout(group)
        layout.setSpacing(5)
        layout.setContentsMargins(8, 6, 8, 6)

        # Row 1: Symbol MAP | Lots ▲▼ Qty | LTP Bid Ask
        row1 = QHBoxLayout()
        row1.setSpacing(5)

        # Symbol input with ATM autocomplete dropdown
        lbl_sym = QLabel("Symbol:")
        lbl_sym.setStyleSheet("color: #cccccc;")
        row1.addWidget(lbl_sym)
        self.symbol_input = QComboBox()
        self.symbol_input.setEditable(True)
        self.symbol_input.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.symbol_input.lineEdit().setPlaceholderText("Type NIFTY, BANK...")
        self.symbol_input.setMinimumWidth(180)
        self.symbol_input.setMaxVisibleItems(12)
        # Connect text changed for ATM suggestions
        self.symbol_input.lineEdit().textChanged.connect(self._on_symbol_text_changed)
        self.symbol_input.lineEdit().returnPressed.connect(self._on_symbol_entered)
        # When user selects from dropdown
        self.symbol_input.activated.connect(self._on_symbol_selected)
        row1.addWidget(self.symbol_input)
        # Debounce timer for ATM lookup
        self._symbol_debounce_timer = QTimer()
        self._symbol_debounce_timer.setSingleShot(True)
        self._symbol_debounce_timer.timeout.connect(self._fetch_atm_suggestions)

        self.search_btn = QPushButton("MAP")
        self.search_btn.setFixedSize(50, 26)
        self.search_btn.setStyleSheet("""
            QPushButton {
                background-color: #4488aa; color: white;
                font-weight: bold; font-size: 11px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #55aacc; }
        """)
        self.search_btn.clicked.connect(self._on_symbol_entered)
        row1.addWidget(self.search_btn)

        row1.addWidget(self._create_separator())

        # Lots with HIGH CONTRAST up/down arrows
        lbl_lots = QLabel("Lots:")
        lbl_lots.setStyleSheet("color: #cccccc;")
        row1.addWidget(lbl_lots)

        self.lot_down_btn = QPushButton("▼")
        self.lot_down_btn.setFixedSize(24, 24)
        self.lot_down_btn.setStyleSheet("""
            QPushButton {
                background-color: #cc6600; color: white;
                font-weight: bold; font-size: 12px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #ee8800; }
        """)
        self.lot_down_btn.clicked.connect(lambda: self._adjust_lots(-1))
        row1.addWidget(self.lot_down_btn)

        self.lots_spin = QSpinBox()
        self.lots_spin.setRange(1, 100)
        self.lots_spin.setValue(self.config.get('trading_defaults', {}).get('default_lots', 1))
        self.lots_spin.valueChanged.connect(self._update_quantity)
        self.lots_spin.setFixedWidth(40)
        self.lots_spin.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        self.lots_spin.setStyleSheet("font-weight: bold; font-size: 11px;")
        row1.addWidget(self.lots_spin)

        self.lot_up_btn = QPushButton("▲")
        self.lot_up_btn.setFixedSize(24, 24)
        self.lot_up_btn.setStyleSheet("""
            QPushButton {
                background-color: #cc6600; color: white;
                font-weight: bold; font-size: 12px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #ee8800; }
        """)
        self.lot_up_btn.clicked.connect(lambda: self._adjust_lots(1))
        row1.addWidget(self.lot_up_btn)

        lbl_qty = QLabel("Qty:")
        lbl_qty.setStyleSheet("color: #cccccc;")
        row1.addWidget(lbl_qty)
        self.qty_label = QLabel("0")
        self.qty_label.setFixedWidth(45)
        self.qty_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.qty_label.setStyleSheet("color: #ffff00;")  # Yellow for visibility
        row1.addWidget(self.qty_label)

        row1.addWidget(self._create_separator())

        # LTP display
        lbl_ltp = QLabel("LTP:")
        lbl_ltp.setStyleSheet("color: #cccccc;")
        row1.addWidget(lbl_ltp)
        self.ltp_label = QLabel("--")
        self.ltp_label.setFixedWidth(60)
        self.ltp_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self.ltp_label.setStyleSheet("color: #00ccff;")
        row1.addWidget(self.ltp_label)

        # Bid/Ask display
        lbl_bid = QLabel("Bid:")
        lbl_bid.setStyleSheet("color: #888888;")
        row1.addWidget(lbl_bid)
        self.bid_label = QLabel("--")
        self.bid_label.setFixedWidth(50)
        self.bid_label.setStyleSheet("color: #00ff88; font-weight: bold;")
        row1.addWidget(self.bid_label)

        lbl_ask = QLabel("Ask:")
        lbl_ask.setStyleSheet("color: #888888;")
        row1.addWidget(lbl_ask)
        self.ask_label = QLabel("--")
        self.ask_label.setFixedWidth(50)
        self.ask_label.setStyleSheet("color: #ff6666; font-weight: bold;")
        row1.addWidget(self.ask_label)

        row1.addStretch()
        layout.addLayout(row1)

        # Row 2: Action B/S | Prod | Type | Price | SL (%) | Tgt (%) | Bracket | Margin
        row2 = QHBoxLayout()
        row2.setSpacing(5)

        lbl_action = QLabel("Action:")
        lbl_action.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_action)

        self.action_group = QButtonGroup()
        self.buy_radio = QRadioButton("BUY")
        self.buy_radio.setChecked(True)
        self.buy_radio.setStyleSheet("""
            QRadioButton { color: #00ff88; font-weight: bold; font-size: 11px; }
            QRadioButton::indicator { width: 14px; height: 14px; }
            QRadioButton::indicator:checked { background-color: #00ff88; border: 2px solid #00ff88; border-radius: 7px; }
            QRadioButton::indicator:unchecked { background-color: #333; border: 2px solid #666; border-radius: 7px; }
        """)
        self.sell_radio = QRadioButton("SELL")
        self.sell_radio.setStyleSheet("""
            QRadioButton { color: #ff5555; font-weight: bold; font-size: 11px; }
            QRadioButton::indicator { width: 14px; height: 14px; }
            QRadioButton::indicator:checked { background-color: #ff5555; border: 2px solid #ff5555; border-radius: 7px; }
            QRadioButton::indicator:unchecked { background-color: #333; border: 2px solid #666; border-radius: 7px; }
        """)
        self.action_group.addButton(self.buy_radio)
        self.action_group.addButton(self.sell_radio)
        row2.addWidget(self.buy_radio)
        row2.addWidget(self.sell_radio)

        row2.addWidget(self._create_separator())

        lbl_prod = QLabel("Prod:")
        lbl_prod.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_prod)
        self.product_combo = QComboBox()
        self.product_combo.addItems(["MIS", "NRML"])
        self.product_combo.setFixedWidth(55)
        row2.addWidget(self.product_combo)

        lbl_type = QLabel("Type:")
        lbl_type.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_type)
        self.order_type_combo = QComboBox()
        self.order_type_combo.addItems(["LIMIT", "MARKET", "SL", "SL-M"])
        self.order_type_combo.setFixedWidth(65)
        self.order_type_combo.currentTextChanged.connect(self._on_order_type_changed)
        row2.addWidget(self.order_type_combo)

        lbl_price = QLabel("Price:")
        lbl_price.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_price)
        self.price_input = QLineEdit()
        self.price_input.setPlaceholderText("0.00")
        self.price_input.setFixedWidth(65)
        self.price_input.textChanged.connect(self._update_sl_target_pct)
        self.price_input.returnPressed.connect(self._recalculate_sl_target_from_price)
        row2.addWidget(self.price_input)

        # SL with percentage
        lbl_sl = QLabel("SL:")
        lbl_sl.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_sl)
        self.entry_sl_input = QLineEdit()
        self.entry_sl_input.setPlaceholderText("--")
        self.entry_sl_input.setFixedWidth(55)
        self.entry_sl_input.textChanged.connect(self._update_sl_target_pct)
        row2.addWidget(self.entry_sl_input)
        self.sl_pct_label = QLabel("")
        self.sl_pct_label.setFixedWidth(40)
        self.sl_pct_label.setStyleSheet("color: #ff8888; font-size: 9px;")
        row2.addWidget(self.sl_pct_label)

        # Target with percentage
        lbl_tgt = QLabel("Tgt:")
        lbl_tgt.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_tgt)
        self.target_input = QLineEdit()
        self.target_input.setPlaceholderText("--")
        self.target_input.setFixedWidth(55)
        self.target_input.textChanged.connect(self._update_sl_target_pct)
        row2.addWidget(self.target_input)
        self.tgt_pct_label = QLabel("")
        self.tgt_pct_label.setFixedWidth(40)
        self.tgt_pct_label.setStyleSheet("color: #88ff88; font-size: 9px;")
        row2.addWidget(self.tgt_pct_label)

        row2.addWidget(self._create_separator())

        # Bracket checkbox - HIGH CONTRAST
        self.bracket_check = QCheckBox("Bracket")
        self.bracket_check.setStyleSheet("""
            QCheckBox { color: #ffcc00; font-weight: bold; font-size: 10px; }
            QCheckBox::indicator { width: 14px; height: 14px; }
            QCheckBox::indicator:checked { background-color: #ffcc00; border: 2px solid #ffcc00; }
            QCheckBox::indicator:unchecked { background-color: #333; border: 2px solid #888; }
        """)
        row2.addWidget(self.bracket_check)

        row2.addWidget(self._create_separator())

        # F1-F4 Preset buttons with short descriptions - same row as Bracket
        self.f1_btn = QPushButton("F1:NC")
        self.f1_btn.setFixedSize(45, 22)
        self.f1_btn.setStyleSheet("""
            QPushButton {
                background-color: #336699; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #4477aa; }
        """)
        self.f1_btn.setToolTip("F1: NIFTY CE ATM")
        self.f1_btn.clicked.connect(lambda: self._load_preset('NIFTY', 'CE', 0))
        row2.addWidget(self.f1_btn)

        self.f2_btn = QPushButton("F2:NP")
        self.f2_btn.setFixedSize(45, 22)
        self.f2_btn.setStyleSheet("""
            QPushButton {
                background-color: #336699; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #4477aa; }
        """)
        self.f2_btn.setToolTip("F2: NIFTY PE ATM")
        self.f2_btn.clicked.connect(lambda: self._load_preset('NIFTY', 'PE', 0))
        row2.addWidget(self.f2_btn)

        self.f3_btn = QPushButton("F3:BC")
        self.f3_btn.setFixedSize(45, 22)
        self.f3_btn.setStyleSheet("""
            QPushButton {
                background-color: #336699; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #4477aa; }
        """)
        self.f3_btn.setToolTip("F3: BANKNIFTY CE ATM")
        self.f3_btn.clicked.connect(lambda: self._load_preset('BANKNIFTY', 'CE', 0))
        row2.addWidget(self.f3_btn)

        self.f4_btn = QPushButton("F4:BP")
        self.f4_btn.setFixedSize(45, 22)
        self.f4_btn.setStyleSheet("""
            QPushButton {
                background-color: #336699; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #4477aa; }
        """)
        self.f4_btn.setToolTip("F4: BANKNIFTY PE ATM")
        self.f4_btn.clicked.connect(lambda: self._load_preset('BANKNIFTY', 'PE', 0))
        row2.addWidget(self.f4_btn)

        # SENSEX preset buttons (BSE)
        self.f7_btn = QPushButton("F7:SC")
        self.f7_btn.setFixedSize(45, 22)
        self.f7_btn.setStyleSheet("""
            QPushButton {
                background-color: #996633; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #aa7744; }
        """)
        self.f7_btn.setToolTip("F7: SENSEX CE ATM (BSE)")
        self.f7_btn.clicked.connect(lambda: self._load_preset('SENSEX', 'CE', 0))
        row2.addWidget(self.f7_btn)

        self.f8_btn = QPushButton("F8:SP")
        self.f8_btn.setFixedSize(45, 22)
        self.f8_btn.setStyleSheet("""
            QPushButton {
                background-color: #996633; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #aa7744; }
        """)
        self.f8_btn.setToolTip("F8: SENSEX PE ATM (BSE)")
        self.f8_btn.clicked.connect(lambda: self._load_preset('SENSEX', 'PE', 0))
        row2.addWidget(self.f8_btn)

        row2.addStretch()

        # Breakeven points label (charges / qty)
        lbl_be = QLabel("BE:")
        lbl_be.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_be)
        self.be_label = QLabel("--")
        self.be_label.setStyleSheet("color: #ff8888; font-weight: bold;")
        self.be_label.setToolTip("Breakeven points to cover round-trip charges")
        self.be_label.setFixedWidth(40)
        row2.addWidget(self.be_label)

        row2.addWidget(self._create_separator())

        lbl_mgn = QLabel("Margin:")
        lbl_mgn.setStyleSheet("color: #cccccc;")
        row2.addWidget(lbl_mgn)
        self.margin_preview = QLabel("₹0")
        self.margin_preview.setStyleSheet("color: #ffaa00; font-weight: bold;")
        self.margin_preview.setToolTip("Required margin for current order")
        row2.addWidget(self.margin_preview)

        layout.addLayout(row2)

        return group

    def _create_positions_panel(self) -> QGroupBox:
        """Create positions table with filter dropdown."""
        group = QGroupBox()
        group.setTitle("")  # No title, we'll use custom header
        layout = QVBoxLayout(group)
        layout.setSpacing(3)
        layout.setContentsMargins(5, 5, 5, 5)

        # Header row with dropdown filter
        header_row = QHBoxLayout()
        header_row.setSpacing(5)
        lbl_pos = QLabel("POSITIONS")
        lbl_pos.setStyleSheet("color: #ffffff; font-weight: bold; font-size: 11px;")
        header_row.addWidget(lbl_pos)

        self.pos_filter_combo = QComboBox()
        self.pos_filter_combo.addItems(["Open", "Closed", "All"])
        self.pos_filter_combo.setCurrentText("Open")
        self.pos_filter_combo.setFixedWidth(70)
        self.pos_filter_combo.setStyleSheet("""
            QComboBox {
                background-color: #333355;
                color: #00ff88;
                font-weight: bold;
                font-size: 10px;
                padding: 2px 4px;
            }
        """)
        self.pos_filter_combo.currentTextChanged.connect(self._on_pos_filter_changed)
        header_row.addWidget(self.pos_filter_combo)
        header_row.addStretch()
        layout.addLayout(header_row)

        # Positions table - Symbol STRETCHES, others fixed
        self.positions_table = QTableWidget()
        self.positions_table.setColumnCount(9)
        self.positions_table.setHorizontalHeaderLabels([
            "Symbol", "Qty", "Avg", "LTP", "P&L", "%", "SL", "Trail", "Exit"
        ])
        header = self.positions_table.horizontalHeader()
        header.setStretchLastSection(False)
        # Symbol STRETCHES to absorb extra space - solves the empty space problem
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # All other columns: FIXED with proper widths
        for i in range(1, 9):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.positions_table.setColumnWidth(1, 45)   # Qty
        self.positions_table.setColumnWidth(2, 65)   # Avg
        self.positions_table.setColumnWidth(3, 65)   # LTP
        self.positions_table.setColumnWidth(4, 75)   # P&L
        self.positions_table.setColumnWidth(5, 65)   # % (wider for +10.5%)
        self.positions_table.setColumnWidth(6, 65)   # SL input
        self.positions_table.setColumnWidth(7, 155)  # Trail: BE + +10 + +25 (bigger buttons)
        self.positions_table.setColumnWidth(8, 115)  # Exit: 50% + EXIT (bigger buttons)
        self.positions_table.setAlternatingRowColors(True)
        self.positions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        # Set row height for button widgets (28px buttons + padding)
        self.positions_table.verticalHeader().setDefaultSectionSize(34)
        # Connect cell click for Symbol copy-to-clipboard
        self.positions_table.cellClicked.connect(self._on_position_cell_clicked)
        layout.addWidget(self.positions_table)

        # Bottom row - P&L based on filter
        bottom = QHBoxLayout()
        self.pos_pnl_label = QLabel("Open P&L:")
        self.pos_pnl_label.setStyleSheet("color: #aaaaaa;")
        bottom.addWidget(self.pos_pnl_label)
        self.total_pnl = QLabel("₹0")
        self.total_pnl.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        self.total_pnl.setToolTip("P&L based on filter selection")
        bottom.addWidget(self.total_pnl)
        bottom.addStretch()

        layout.addLayout(bottom)

        return group

    def _create_basket_panel(self) -> QGroupBox:
        """Create basket order builder - shows 4 legs clearly."""
        group = QGroupBox("BASKET ORDER")
        group.setMinimumHeight(200)  # Taller for 4 visible rows
        group.setMaximumHeight(220)
        layout = QVBoxLayout(group)
        layout.setSpacing(3)
        layout.setContentsMargins(5, 5, 5, 5)

        # Basket legs table - 4 rows visible clearly
        self.basket_table = QTableWidget()
        self.basket_table.setColumnCount(5)
        self.basket_table.setHorizontalHeaderLabels(["#", "Symbol", "B/S", "Qty", "Price"])
        self.basket_table.setRowCount(4)  # Always show 4 rows
        self.basket_table.verticalHeader().setVisible(False)
        self.basket_table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.basket_table.setFixedHeight(155)  # Taller for 4 rows + header
        # Optimize column widths - Symbol is fixed, not stretch
        bheader = self.basket_table.horizontalHeader()
        bheader.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)  # #
        bheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)  # Symbol - fixed width
        bheader.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)  # B/S
        bheader.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # Qty
        bheader.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)  # Price stretches
        self.basket_table.setColumnWidth(0, 22)  # #
        self.basket_table.setColumnWidth(1, 180)  # Symbol - fixed optimal width
        self.basket_table.setColumnWidth(2, 35)  # B/S
        self.basket_table.setColumnWidth(3, 45)  # Qty
        layout.addWidget(self.basket_table)

        # Summary row
        summary = QHBoxLayout()
        summary.addWidget(QLabel("Premium:"))
        self.net_premium = QLabel("₹0")
        self.net_premium.setStyleSheet("font-weight: bold;")
        summary.addWidget(self.net_premium)

        summary.addWidget(QLabel("Legs:"))
        self.leg_count = QLabel("0")
        summary.addWidget(self.leg_count)

        summary.addStretch()

        self.clear_basket_btn = QPushButton("CLR")
        self.clear_basket_btn.setFixedWidth(40)
        self.clear_basket_btn.setStyleSheet("font-size: 9px;")
        self.clear_basket_btn.clicked.connect(self._clear_basket)
        summary.addWidget(self.clear_basket_btn)

        self.execute_basket_btn = QPushButton("EXECUTE")
        self.execute_basket_btn.setFixedWidth(70)
        self.execute_basket_btn.setStyleSheet("background-color: #006644; color: white; font-weight: bold; font-size: 9px;")
        self.execute_basket_btn.clicked.connect(self._execute_basket)
        summary.addWidget(self.execute_basket_btn)

        layout.addLayout(summary)

        return group

    def _create_order_book_panel(self) -> QGroupBox:
        """Create order book display with cancel button and editable price."""
        group = QGroupBox("ORDER BOOK")
        layout = QVBoxLayout(group)
        layout.setSpacing(2)
        layout.setContentsMargins(5, 5, 5, 5)

        self.orders_table = QTableWidget()
        self.orders_table.setColumnCount(8)
        self.orders_table.setHorizontalHeaderLabels(["Symbol", "B/S", "Qty", "LTP", "Price", "Status", "Time", "X"])
        self.orders_table.verticalHeader().setVisible(False)  # Hide row numbers
        # Optimized columns: Symbol stretches, others fixed
        oheader = self.orders_table.horizontalHeader()
        oheader.setStretchLastSection(False)
        # Symbol STRETCHES to absorb extra space
        oheader.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        # All other columns: FIXED with proper widths
        for i in range(1, 8):
            oheader.setSectionResizeMode(i, QHeaderView.ResizeMode.Fixed)
        self.orders_table.setColumnWidth(1, 35)   # B/S
        self.orders_table.setColumnWidth(2, 45)   # Qty
        self.orders_table.setColumnWidth(3, 60)   # LTP
        self.orders_table.setColumnWidth(4, 65)   # Price (editable)
        self.orders_table.setColumnWidth(5, 75)   # Status
        self.orders_table.setColumnWidth(6, 60)   # Time (HH:MM)
        self.orders_table.setColumnWidth(7, 30)   # X button
        self.orders_table.setAlternatingRowColors(True)

        # Enable sorting - users can click headers to sort
        self.orders_table.setSortingEnabled(True)
        # Default sort: Time column (6) descending (newest first)
        self.orders_table.sortByColumn(6, Qt.SortOrder.DescendingOrder)
        # Track current sort state for persistence across refreshes
        self._orders_sort_column = 6
        self._orders_sort_order = Qt.SortOrder.DescendingOrder
        # Connect header click to track user's sort preference
        oheader.sectionClicked.connect(self._on_orders_header_clicked)

        # Connect cell changed for price editing
        self.orders_table.cellChanged.connect(self._on_order_price_edited)
        # Track editing state to avoid refresh during edit
        self._editing_order_price = False

        # Connect cell click for Symbol copy-to-clipboard
        self.orders_table.cellClicked.connect(self._on_order_cell_clicked)

        layout.addWidget(self.orders_table, 1)

        return group

    def _on_orders_header_clicked(self, logical_index: int):
        """Track user's sort preference when they click a header."""
        self._orders_sort_column = logical_index
        self._orders_sort_order = self.orders_table.horizontalHeader().sortIndicatorOrder()

    def _on_order_cell_clicked(self, row: int, column: int):
        """Handle cell click - copy Symbol to clipboard when Symbol column clicked."""
        if column != 0:  # Only Symbol column
            return

        symbol_item = self.orders_table.item(row, 0)
        if not symbol_item:
            return

        symbol_text = symbol_item.text()
        if symbol_text:
            QApplication.clipboard().setText(symbol_text)
            self.log_message.emit(f"[INFO] Copied: {symbol_text}")

    def _on_order_price_edited(self, row: int, column: int):
        """Handle price cell editing - modify order when Enter pressed."""
        # Only process price column (column 4)
        if column != 4:
            return

        try:
            # Get order data from Symbol column (stored in UserRole)
            symbol_item = self.orders_table.item(row, 0)
            price_item = self.orders_table.item(row, 4)
            if not symbol_item or not price_item:
                return

            order_data = symbol_item.data(Qt.ItemDataRole.UserRole)
            if not order_data or not isinstance(order_data, dict):
                return

            new_price_str = price_item.text().strip()
            if not new_price_str or new_price_str == "--":
                return

            try:
                new_price = float(new_price_str)
            except ValueError:
                self.log_message.emit(f"[ERROR] Invalid price: {new_price_str}")
                return

            if new_price <= 0:
                self.log_message.emit(f"[ERROR] Price must be > 0")
                return

            order_id = order_data.get('order_id', '')
            order_type = str(order_data.get('order_type', '')).upper()
            trigger_price = order_data.get('trigger_price', '')
            old_price = order_data.get('price', '')

            # Determine if this is an SL order (uses trigger price)
            is_sl_order = 'SL' in order_type or (trigger_price and float(trigger_price) > 0)

            # Get old price for comparison
            old_price_val = float(trigger_price) if is_sl_order and trigger_price else float(old_price or 0)

            # Skip if price unchanged
            if abs(new_price - old_price_val) < 0.01:
                return

            # Modify order
            if is_sl_order:
                result = self.orders.modify_order(order_id, new_trigger=new_price)
                price_label = "trigger"
            else:
                result = self.orders.modify_order(order_id, new_price=new_price)
                price_label = "price"

            if result.success:
                self.log_message.emit(f"[MODIFY] Order {order_id[-6:]}: {price_label} {old_price_val:.2f} -> {new_price:.2f}")
                if self.sound:
                    self.sound.play('order_placed')

                # Update tracker if this is a tracked SL order
                for symbol, pos in self.pos_tracker.positions.items():
                    if pos.sl_order_id == order_id:
                        pos.sl_price = new_price
                        if self.trail_mgr and symbol in self.trail_mgr.positions:
                            self.trail_mgr.positions[symbol].current_sl = new_price

                self._force_next_orders_refresh()
                QTimer.singleShot(500, self._refresh_orders)
            else:
                self.log_message.emit(f"[ERROR] Modify failed: {result.message}")
                if self.sound:
                    self.sound.play('error')
                # Revert display to old price
                self.orders_table.blockSignals(True)
                price_item.setText(str(old_price_val))
                self.orders_table.blockSignals(False)

        except Exception as e:
            self.log_message.emit(f"[ERROR] Modify order failed: {e}")

    def _cancel_single_order(self, order_id: str, source: str = "button"):
        """Cancel a single order by ID."""
        import traceback
        logger.info(f"Cancel triggered for {order_id}, source={source}, stack:\n{''.join(traceback.format_stack()[-5:])}")
        try:
            self.log_message.emit(f"[CANCEL] Cancelling {order_id[-6:]} (source: {source})")
            result = self.orders.cancel_order(order_id)
            if result.success:
                self.log_message.emit(f"[CANCEL] Order {order_id[-6:]} cancelled OK")
                if self.sound:
                    self.sound.play('order_placed')

                # Clear tracker references if this was SL/Target order
                for symbol, pos in list(self.pos_tracker.positions.items()):
                    if pos.sl_order_id == order_id:
                        pos.sl_order_id = None
                        pos.sl_price = None
                    if pos.target_order_id == order_id:
                        pos.target_order_id = None
                        pos.target_price = None

                # Remove from OCO monitor if present
                if self.oco_monitor:
                    self.oco_monitor.remove_order(order_id)

                self._force_next_orders_refresh()
                QTimer.singleShot(500, self._refresh_orders)
            else:
                self.log_message.emit(f"[ERROR] Cancel failed: {result.message}")
                if self.sound:
                    self.sound.play('error')

        except Exception as e:
            self.log_message.emit(f"[ERROR] Cancel order failed: {e}")

    def _create_log_panel(self) -> QGroupBox:
        """Create log panel for 3-column layout - fills vertical space."""
        group = QGroupBox("LOG")
        layout = QVBoxLayout(group)
        layout.setSpacing(3)
        layout.setContentsMargins(5, 5, 5, 5)

        # Log text area - fills space
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Consolas", 9))
        self.log_text.setStyleSheet("QTextEdit { line-height: 1.2; }")
        layout.addWidget(self.log_text, 1)

        # Clear button at bottom - high contrast
        self.clear_log_btn = QPushButton("CLEAR LOG")
        self.clear_log_btn.setFixedHeight(24)
        self.clear_log_btn.setToolTip("Clear log display")
        self.clear_log_btn.setStyleSheet("""
            QPushButton {
                background-color: #555555; color: white;
                font-weight: bold; font-size: 9px; border-radius: 3px;
            }
            QPushButton:hover { background-color: #777777; }
        """)
        self.clear_log_btn.clicked.connect(lambda: self.log_text.clear())
        layout.addWidget(self.clear_log_btn)

        return group

    def _create_separator(self) -> QFrame:
        """Create a vertical separator line."""
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Sunken)
        sep.setMaximumWidth(2)
        return sep

    def setup_shortcuts(self):
        """Setup keyboard shortcuts."""
        # Buy/Sell shortcuts
        QShortcut(QKeySequence("Ctrl+B"), self, lambda: self._place_quick_order('B'))
        QShortcut(QKeySequence("Ctrl+S"), self, lambda: self._place_quick_order('S'))

        # Function keys
        QShortcut(QKeySequence("F1"), self, lambda: self._load_preset('NIFTY', 'CE', 0))
        QShortcut(QKeySequence("F2"), self, lambda: self._load_preset('NIFTY', 'PE', 0))
        QShortcut(QKeySequence("F3"), self, lambda: self._load_preset('BANKNIFTY', 'CE', 0))
        QShortcut(QKeySequence("F4"), self, lambda: self._load_preset('BANKNIFTY', 'PE', 0))
        QShortcut(QKeySequence("F5"), self, lambda: self._adjust_lots(1))
        QShortcut(QKeySequence("F6"), self, lambda: self._adjust_lots(-1))
        QShortcut(QKeySequence("F7"), self, lambda: self._load_preset('SENSEX', 'CE', 0))
        QShortcut(QKeySequence("F8"), self, lambda: self._load_preset('SENSEX', 'PE', 0))
        QShortcut(QKeySequence("F9"), self, self._exit_all_positions)
        QShortcut(QKeySequence("F10"), self, self._cancel_all_orders)

        # Quick focus
        QShortcut(QKeySequence("Ctrl+L"), self, lambda: self.symbol_input.lineEdit().setFocus())

        # Trailing shortcuts
        QShortcut(QKeySequence("T"), self, self._trail_selected_to_cost)
        QShortcut(QKeySequence("Shift+T"), self, lambda: self._trail_selected_plus(10))

        # Escape to clear
        QShortcut(QKeySequence("Escape"), self, self._clear_inputs)

    def setup_timers(self):
        """Setup refresh timers."""
        # Position refresh
        refresh_interval = self.config.get('position_refresh', {}).get('interval_sec', 2) * 1000
        self.position_timer = QTimer()
        self.position_timer.timeout.connect(self._refresh_positions)
        self.position_timer.start(refresh_interval)

        # Time update every second
        self.time_timer = QTimer()
        self.time_timer.timeout.connect(self._update_time)
        self.time_timer.start(1000)

        # Margin refresh every 30 seconds
        self.margin_timer = QTimer()
        self.margin_timer.timeout.connect(self._refresh_margin)
        self.margin_timer.start(30000)

        # Orders refresh every 5 seconds
        self.orders_timer = QTimer()
        self.orders_timer.timeout.connect(self._refresh_orders)
        self.orders_timer.start(5000)

        # Quote (LTP/Bid/Ask) refresh every 2 seconds
        self.quote_timer = QTimer()
        self.quote_timer.timeout.connect(self._refresh_quote)
        self.quote_timer.start(2000)

    def connect_signals(self):
        """Connect internal signals."""
        self.position_updated.connect(self._update_positions_table)
        self.order_updated.connect(self._update_orders_table)
        self.log_message.connect(self._append_log)
        self.margin_updated.connect(self._update_margin_display)
        self.pnl_updated.connect(self._update_pnl_display)
        self.ws_order_update.connect(self._handle_ws_order_update)

        # Setup WebSocket callbacks for real-time order updates
        if self.ws_handler:
            self.ws_handler.register_callback('order_update', self._on_ws_order_update)
            self.ws_handler.register_callback('open', lambda msg: self.log_message.emit("[WS] Connected"))
            self.ws_handler.register_callback('close', lambda msg: self.log_message.emit("[WS] Disconnected"))

    def _on_ws_order_update(self, data: dict):
        """WebSocket callback - runs in WebSocket thread, emit signal for thread-safety."""
        self.ws_order_update.emit(data)

    def _handle_ws_order_update(self, data: dict):
        """Handle WebSocket order update in main thread."""
        try:
            order_id = data.get('nOrdNo', data.get('orderId', ''))
            status = data.get('ordSt', data.get('status', '')).lower()
            symbol = data.get('trdSym', data.get('tradingSymbol', data.get('symbol', '')))

            # Log the update
            status_display = status.upper()
            if status in ['complete', 'completed', 'traded', 'filled', 'executed']:
                self.log_message.emit(f"[WS] ✓ Order {order_id[-6:]} FILLED: {symbol}")
                if self.sound:
                    self.sound.play('order_filled')

                # Check if this is an SL order - show popup alert
                is_sl_or_target = False
                for pos_symbol, pos in self.pos_tracker.positions.items():
                    if pos.sl_order_id == order_id:
                        # SL HIT! Show popup alert
                        self._show_sl_breach_alert(pos_symbol, pos.sl_price)
                        is_sl_or_target = True
                        break
                    if pos.target_order_id == order_id:
                        # Target hit - just log
                        self.log_message.emit(f"[ALERT] TARGET HIT: {pos_symbol}")
                        is_sl_or_target = True
                        break

                # If this is an ENTRY order (not SL/Target), check for protection after delay
                if not is_sl_or_target and symbol:
                    # Wait 8 seconds for SL/Target placement to complete, then check
                    QTimer.singleShot(8000, lambda s=symbol: self._check_position_protection(s))

                # Immediately refresh positions and orders
                self._force_next_orders_refresh()
                QTimer.singleShot(100, self._refresh_positions)
                QTimer.singleShot(200, self._refresh_orders)

            elif status in ['rejected']:
                reason = data.get('rejRsn') or data.get('rejectionReason') or \
                         data.get('errMsg') or data.get('text') or 'Unknown reason'
                self.log_message.emit(f"[WS] ✗ REJECTED {symbol}: {reason}")
                if self.sound:
                    self.sound.play('order_rejected')
                # Send Telegram alert for rejected orders
                if self.telegram:
                    self.telegram.send(f"❌ ORDER REJECTED: {symbol}\n{reason}")
                self._force_next_orders_refresh()
                QTimer.singleShot(200, self._refresh_orders)

            elif status in ['cancelled']:
                self.log_message.emit(f"[WS] Order {order_id[-6:]} CANCELLED: {symbol}")
                self._force_next_orders_refresh()
                QTimer.singleShot(200, self._refresh_orders)

            elif status in ['open', 'pending', 'trigger pending']:
                self.log_message.emit(f"[WS] Order {order_id[-6:]} PENDING: {symbol}")
                # Also refresh orders table for pending status changes
                self._force_next_orders_refresh()
                QTimer.singleShot(200, self._refresh_orders)

            else:
                # Any other status change - refresh to keep table current
                self.log_message.emit(f"[WS] Order {order_id[-6:]} {status_display}: {symbol}")
                self._force_next_orders_refresh()
                QTimer.singleShot(200, self._refresh_orders)

        except Exception as e:
            self.log_message.emit(f"[WS] Error processing update: {e}")

    def _show_sl_breach_alert(self, symbol: str, sl_price: float):
        """Show popup alert when SL is hit."""
        self.log_message.emit(f"[ALERT] SL HIT: {symbol} @ {sl_price:.2f}")

        # Play alert sound
        if self.sound:
            self.sound.play('sl_hit')  # Will fall back to error sound if not defined

        # Send Telegram alert
        if self.telegram:
            self.telegram.send(f"🛑 SL TRIGGERED: {symbol} @ {sl_price:.2f}")

        # Show popup message box
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Icon.Warning)
        msg.setWindowTitle("SL TRIGGERED")
        msg.setText(f"STOP LOSS HIT!\n\n{symbol}\n@ {sl_price:.2f}")
        msg.setStyleSheet("""
            QMessageBox {
                background-color: #2a0000;
            }
            QMessageBox QLabel {
                color: #ff4444;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton {
                background-color: #aa0000;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #cc0000;
            }
        """)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)

        # Auto-close after 5 seconds
        QTimer.singleShot(5000, msg.close)
        msg.show()  # Non-blocking show

    def _check_position_protection(self, symbol: str):
        """Check if position has SL/Target protection. Alert via Telegram if not."""
        try:
            # First check if position still exists and is open
            pos_exists = False
            for pos in self._positions_cache:
                if _get_position_symbol(pos) == symbol:
                    qty = _get_position_qty(pos)
                    if qty != 0:  # Position is open
                        pos_exists = True
                    break

            if not pos_exists:
                # Position was closed or doesn't exist - no alert needed
                return

            # Check position tracker for SL/Target
            tracked = self.pos_tracker.get_position(symbol)

            has_sl = tracked and tracked.sl_order_id
            has_target = tracked and tracked.target_order_id

            if not has_sl and not has_target:
                # NO PROTECTION! Send alert
                alert_msg = f"⚠️ UNPROTECTED POSITION: {symbol}\nNo SL or Target order in place!"
                self.log_message.emit(f"[ALERT] {alert_msg}")

                # Send Telegram alert
                if self.telegram:
                    self.telegram.send(alert_msg)

                # Play warning sound
                if self.sound:
                    self.sound.play('error')

        except Exception as e:
            self.log_message.emit(f"[ALERT] Error checking protection for {symbol}: {e}")

    # ==================== Event Handlers ====================

    def _on_symbol_entered(self):
        """Handle symbol input - map to NEO."""
        symbol = self.symbol_input.currentText().strip().upper()
        if not symbol:
            return

        # Reset all price labels while loading
        self.ltp_label.setText("...")
        self.ltp_label.setStyleSheet("color: #888888;")
        self.bid_label.setText("--")
        self.ask_label.setText("--")
        self.be_label.setText("--")

        try:
            # Use cached lookup (fast) - this is instant
            mapping = self.mapper.get_neo_params(symbol)

            if mapping is None:
                # Clear current mapping and quantity on failure
                self.current_mapping = None
                self.qty_label.setText("0")
                self.ltp_label.setText("--")
                self.ltp_label.setStyleSheet("color: #ff4444;")

                # Symbol not in cache - check if it's weekly format
                weekly_pattern = r'^([A-Z]+)(\d{5,7})(\d+)(CE|PE)$'
                weekly_match = re.match(weekly_pattern, symbol)

                if weekly_match:
                    underlying = weekly_match.group(1)
                    # NIFTY and SENSEX support weekly - try live search
                    weekly_supported = ['NIFTY', 'SENSEX']
                    # BANKNIFTY, FINNIFTY, MIDCPNIFTY, BANKEX - monthly only
                    monthly_only = ['BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'BANKEX']

                    if underlying in monthly_only:
                        self.log_message.emit(f"[ERROR] {underlying} weekly not supported - use Monthly expiry")
                        if self.sound:
                            self.sound.play('error')
                        return
                    elif underlying in weekly_supported:
                        # Try live search for weekly symbol
                        self.log_message.emit(f"[INFO] Searching for weekly symbol: {symbol}")
                        live_mapping = self.mapper.search_and_map(symbol)
                        if live_mapping:
                            mapping = live_mapping
                        else:
                            self.log_message.emit(f"[ERROR] Weekly symbol not found: {symbol}")
                            if self.sound:
                                self.sound.play('error')
                            return
                    else:
                        self.log_message.emit(f"[ERROR] Symbol not mapped: {symbol}")
                        if self.sound:
                            self.sound.play('error')
                        return
                else:
                    self.log_message.emit(f"[ERROR] Symbol not mapped: {symbol}")
                    if self.sound:
                        self.sound.play('error')
                    return

            self.current_mapping = mapping

            # Update lot size and quantity
            lot_size = mapping['lot_size']
            self.qty_label.setText(str(lot_size * self.lots_spin.value()))

            # Fetch and display LTP, Bid, Ask
            ltp = None
            bid = None
            ask = None
            if self.kite and self.kite.is_connected():
                try:
                    quote = self.kite.get_quote(symbol)
                    if quote:
                        ltp = quote.get('last_price')
                        depth = quote.get('depth', {})
                        buy_depth = depth.get('buy', [])
                        sell_depth = depth.get('sell', [])
                        if buy_depth:
                            bid = buy_depth[0].get('price')
                        if sell_depth:
                            ask = sell_depth[0].get('price')

                        if ltp:
                            self.ltp_label.setText(f"₹{ltp:.2f}")
                            self.ltp_label.setStyleSheet("color: #00ff88; font-weight: bold;")
                            # Auto-fill LTP to price field
                            self.price_input.setText(f"{ltp:.2f}")
                            # Auto-fill default SL (10%) and Target (25%) for options
                            sl_default = round(ltp * 0.90, 2)  # 10% below
                            tgt_default = round(ltp * 1.25, 2)  # 25% above
                            self.entry_sl_input.setText(f"{sl_default:.2f}")
                            self.target_input.setText(f"{tgt_default:.2f}")
                            self._update_sl_target_pct()
                            # Auto-check Bracket for quick trading
                            self.bracket_check.setChecked(True)
                            # Calculate and display breakeven points
                            qty = lot_size * self.lots_spin.value()
                            be_points = get_breakeven_points(ltp, qty)
                            self.be_label.setText(f"{be_points:.1f}")
                        if bid:
                            self.bid_label.setText(f"₹{bid:.2f}")
                        if ask:
                            self.ask_label.setText(f"₹{ask:.2f}")
                    else:
                        self.ltp_label.setText("--")
                        self.bid_label.setText("--")
                        self.ask_label.setText("--")
                except Exception:
                    self.ltp_label.setText("--")
                    self.bid_label.setText("--")
                    self.ask_label.setText("--")

            ltp_str = f", LTP: ₹{ltp:.2f}" if ltp else ""
            self.log_message.emit(f"[MAP] {symbol} -> Lot: {lot_size}{ltp_str}")

            # Apply learned rules from rejection learner
            if self.rejection_learner:
                current_product = self.product_combo.currentText()
                recommendation = self.rejection_learner.get_recommended_product(symbol, current_product)
                if recommendation:
                    new_product = recommendation['product']
                    reason = recommendation['reason']
                    # Auto-switch product
                    idx = self.product_combo.findText(new_product)
                    if idx >= 0:
                        self.product_combo.setCurrentIndex(idx)
                        self.log_message.emit(f"[LEARNER] Auto-switched to {new_product}: {reason}")
                        if self.sound:
                            self.sound.play('order_placed')  # Gentle notification

        except Exception as e:
            self.current_mapping = None
            self.qty_label.setText("0")
            self.ltp_label.setText("--")
            self.ltp_label.setStyleSheet("color: #ff4444;")
            self.log_message.emit(f"[ERROR] Symbol mapping failed: {str(e)}")
            if self.sound:
                self.sound.play('error')

    def _on_symbol_text_changed(self, text: str):
        """Handle text changes in symbol input - trigger ATM suggestions."""
        text = text.strip().upper()

        # Index name triggers for ATM suggestions
        index_triggers = {
            'NIFTY': 'NIFTY',
            'NIF': 'NIFTY',
            'BANK': 'BANKNIFTY',
            'BANKNIFTY': 'BANKNIFTY',
            'BNF': 'BANKNIFTY',
            'FIN': 'FINNIFTY',
            'FINNIFTY': 'FINNIFTY',
            'MID': 'MIDCPNIFTY',
            'MIDCP': 'MIDCPNIFTY',
            'SEN': 'SENSEX',
            'SENSEX': 'SENSEX',
        }

        # Check if text matches an index trigger
        self._pending_index_lookup = None
        for trigger, index_name in index_triggers.items():
            if text == trigger or text.startswith(trigger):
                # Don't trigger if user has already typed a full symbol
                if len(text) > len(trigger) + 2 and any(c.isdigit() for c in text):
                    return
                self._pending_index_lookup = index_name
                # Debounce - wait 300ms before fetching
                self._symbol_debounce_timer.start(300)
                return

    def _on_symbol_selected(self, index: int):
        """Handle selection from dropdown."""
        # Get selected symbol and trigger mapping
        selected = self.symbol_input.currentText()
        if selected:
            self._on_symbol_entered()

    def _fetch_atm_suggestions(self):
        """Fetch ATM strikes for the pending index and populate dropdown."""
        if not hasattr(self, '_pending_index_lookup') or not self._pending_index_lookup:
            return

        index_name = self._pending_index_lookup
        self._pending_index_lookup = None

        if not self.kite or not self.kite.is_connected():
            self.log_message.emit("[INFO] Kite not connected - cannot fetch ATM strikes")
            return

        try:
            # Get ATM strike
            spot = self.kite.get_spot_price(index_name)
            if not spot:
                return

            atm_strike = self.kite.get_atm_strike(index_name)
            gap = self.kite.strike_gaps.get(index_name, 50)

            # Get expiry type from combo
            use_monthly = self.expiry_combo.currentText() == "Monthly"

            # Generate 5 strikes each side of ATM for CE and PE
            suggestions = []

            # CE options: ITM2, ITM1, ATM, OTM1, OTM2 (ascending strikes)
            for offset in range(-2, 3):
                try:
                    symbol = self.kite.get_option_symbol(index_name, 'CE', offset, use_monthly=use_monthly)
                    strike = atm_strike + (offset * gap)
                    label = f"{symbol} (CE {'ATM' if offset == 0 else 'ITM' + str(abs(offset)) if offset < 0 else 'OTM' + str(offset)})"
                    suggestions.append((symbol, label, strike))
                except Exception:
                    pass

            # PE options: OTM2, OTM1, ATM, ITM1, ITM2 (descending strikes)
            for offset in range(2, -3, -1):
                try:
                    symbol = self.kite.get_option_symbol(index_name, 'PE', offset, use_monthly=use_monthly)
                    strike = atm_strike + (offset * gap)
                    label = f"{symbol} (PE {'ATM' if offset == 0 else 'OTM' + str(abs(offset)) if offset > 0 else 'ITM' + str(abs(offset))})"
                    suggestions.append((symbol, label, strike))
                except Exception:
                    pass

            # Populate dropdown
            current_text = self.symbol_input.currentText()
            self.symbol_input.blockSignals(True)
            self.symbol_input.clear()

            # Add suggestions (symbol only, not the label - label is for display info)
            for symbol, label, strike in suggestions:
                self.symbol_input.addItem(symbol)

            # Restore current text
            self.symbol_input.setEditText(current_text)
            self.symbol_input.blockSignals(False)

            # Show dropdown
            self.symbol_input.showPopup()

            self.log_message.emit(f"[ATM] {index_name} Spot: {spot:.0f}, ATM: {atm_strike} - {len(suggestions)} options loaded")

        except Exception as e:
            self.log_message.emit(f"[ERROR] ATM fetch failed: {e}")

    def _on_order_type_changed(self, order_type: str):
        """Enable/disable price field based on order type."""
        self.price_input.setEnabled(order_type in ["LIMIT", "SL"])

    def _update_quantity(self):
        """Update quantity when lots change."""
        if self.current_mapping:
            lot_size = self.current_mapping['lot_size']
            qty = lot_size * self.lots_spin.value()
            self.qty_label.setText(str(qty))
            # Recalculate breakeven with new quantity
            self._update_breakeven(qty)

    def _update_breakeven(self, qty: int = None):
        """Update breakeven points based on price and quantity."""
        try:
            price_text = self.price_input.text().strip()
            if not price_text:
                self.be_label.setText("--")
                self.be_label.setToolTip("Breakeven points to cover round-trip charges")
                return

            price = float(price_text)
            if price <= 0:
                self.be_label.setText("--")
                self.be_label.setToolTip("Breakeven points to cover round-trip charges")
                return

            if qty is None:
                if self.current_mapping:
                    lot_size = self.current_mapping['lot_size']
                    qty = lot_size * self.lots_spin.value()
                else:
                    self.be_label.setText("--")
                    self.be_label.setToolTip("Breakeven points to cover round-trip charges")
                    return

            # Get full charge breakdown for tooltip
            charges = calculate_charges(price, qty)
            self.be_label.setText(f"{charges.breakeven_points:.1f}")

            # Detailed tooltip with charge breakdown
            tooltip = (
                f"Round-trip Charges (₹{charges.total:.2f}):\n"
                f"  Exchange Txn: ₹{charges.exchange_txn:.2f}\n"
                f"  STT (sell): ₹{charges.stt:.2f}\n"
                f"  Stamp (buy): ₹{charges.stamp_duty:.2f}\n"
                f"  SEBI: ₹{charges.sebi:.2f}\n"
                f"  GST: ₹{charges.gst:.2f}\n"
                f"────────────────\n"
                f"Exit at ₹{price + charges.breakeven_points:.2f} to breakeven"
            )
            self.be_label.setToolTip(tooltip)
        except (ValueError, TypeError):
            self.be_label.setText("--")
            self.be_label.setToolTip("Breakeven points to cover round-trip charges")

    def _update_sl_target_pct(self):
        """Update SL/Target percentage display based on price."""
        try:
            price_text = self.price_input.text().strip()
            if not price_text:
                self.sl_pct_label.setText("")
                self.tgt_pct_label.setText("")
                return

            price = float(price_text)
            if price <= 0:
                self.sl_pct_label.setText("")
                self.tgt_pct_label.setText("")
                return

            # Calculate SL percentage
            sl_text = self.entry_sl_input.text().strip()
            if sl_text:
                sl = float(sl_text)
                sl_pct = abs(price - sl) / price * 100
                self.sl_pct_label.setText(f"-{sl_pct:.1f}%")
            else:
                self.sl_pct_label.setText("")

            # Calculate Target percentage
            tgt_text = self.target_input.text().strip()
            if tgt_text:
                tgt = float(tgt_text)
                tgt_pct = abs(tgt - price) / price * 100
                self.tgt_pct_label.setText(f"+{tgt_pct:.1f}%")
            else:
                self.tgt_pct_label.setText("")

            # Update breakeven when price changes
            self._update_breakeven()

        except (ValueError, ZeroDivisionError):
            self.sl_pct_label.setText("")
            self.tgt_pct_label.setText("")
            self.be_label.setText("--")

    def _recalculate_sl_target_from_price(self):
        """Recalculate SL and Target when user changes price and presses Enter."""
        try:
            price_text = self.price_input.text().strip()
            if not price_text:
                return

            price = float(price_text)
            if price <= 0:
                return

            # Recalculate SL (10% below) and Target (25% above)
            sl_default = round(price * 0.90, 2)
            tgt_default = round(price * 1.25, 2)

            self.entry_sl_input.setText(f"{sl_default:.2f}")
            self.target_input.setText(f"{tgt_default:.2f}")

            # Update percentage display
            self._update_sl_target_pct()

            self.log_message.emit(f"[PRICE] Recalculated SL: {sl_default:.2f}, Target: {tgt_default:.2f}")

        except ValueError:
            pass

    def _place_quick_order(self, action: str):
        """Place order from quick entry panel."""
        if not self.current_mapping:
            self.log_message.emit("[ERROR] No symbol selected. Enter symbol first.")
            return

        try:
            price_text = self.price_input.text().strip()
            order_type = self.order_type_combo.currentText()

            # Validate price for LIMIT orders
            if order_type == "LIMIT" and not price_text:
                self.price_input.setStyleSheet("background-color: #660000; border: 2px solid red;")
                self.log_message.emit("[ERROR] Price required for LIMIT order")
                if self.sound:
                    self.sound.play('error')
                return
            else:
                self.price_input.setStyleSheet("")  # Reset style

            price = float(price_text) if price_text else 0

            # Validate price against LTP for LIMIT orders (trader sanity check)
            if order_type == "LIMIT" and price > 0:
                ltp_text = self.ltp_label.text().replace('₹', '').strip()
                if ltp_text and ltp_text != '--':
                    try:
                        ltp = float(ltp_text)
                        if ltp > 0:
                            # Check if price is within 50% of LTP (reasonable for options)
                            deviation = abs(price - ltp) / ltp * 100
                            if deviation > 50:
                                self.price_input.setStyleSheet("background-color: #663300; border: 2px solid orange;")
                                self.log_message.emit(
                                    f"[WARN] Price {price:.2f} is {deviation:.0f}% away from LTP {ltp:.2f}. "
                                    f"Press again to confirm or adjust price."
                                )
                                if self.sound:
                                    self.sound.play('error')
                                # Use flag to allow second press to proceed
                                if not hasattr(self, '_price_warning_acknowledged'):
                                    self._price_warning_acknowledged = False
                                if not self._price_warning_acknowledged:
                                    self._price_warning_acknowledged = True
                                    return
                                self._price_warning_acknowledged = False
                            else:
                                self.price_input.setStyleSheet("")
                                if hasattr(self, '_price_warning_acknowledged'):
                                    self._price_warning_acknowledged = False
                    except ValueError:
                        pass

            # Map order type to NEO format
            type_map = {'LIMIT': 'L', 'MARKET': 'MKT', 'SL': 'SL', 'SL-M': 'SL-M'}

            params = OrderParams(
                symbol=self.current_mapping['trading_symbol'],
                exchange_segment=self.current_mapping['exchange_segment'],
                instrument_token=self.current_mapping['instrument_token'],
                transaction_type=action,
                quantity=int(self.qty_label.text()),
                product=self.product_combo.currentText(),
                order_type=type_map.get(order_type, 'L'),
                price=price if order_type == 'LIMIT' else None,
            )

            result = self.orders.place_order(params)

            if result.success:
                action_str = "BUY" if action == 'B' else "SELL"
                self.log_message.emit(
                    f"[ORDER] {action_str} {params.symbol} {params.quantity} @ {price} -> ID: {result.order_id}"
                )
                if self.sound:
                    self.sound.play('order_placed')

                # Log to trade logger
                if self.logger:
                    self.logger.log_order_placed(
                        symbol=params.symbol,
                        action=action,
                        quantity=params.quantity,
                        price=price,
                        order_type=params.order_type,
                        product=params.product,
                        order_id=result.order_id or ''
                    )

                # Get SL/Target prices before clearing
                sl_text = self.entry_sl_input.text().strip()
                target_text = self.target_input.text().strip()
                sl_price_pending = float(sl_text) if sl_text else None
                target_price_pending = float(target_text) if target_text else None

                # Clear SL/Target fields after order placed
                self.entry_sl_input.clear()
                self.target_input.clear()
                self.sl_pct_label.clear()
                self.tgt_pct_label.clear()

                # Register entry order with PartialFillMonitor
                # Monitor will place SL/Target automatically when entry fills (no timeout!)
                if self.partial_fill_monitor and result.order_id:
                    side = 'LONG' if action == 'B' else 'SHORT'
                    self.partial_fill_monitor.register_entry(
                        symbol=params.symbol,
                        entry_order_id=result.order_id,
                        expected_qty=params.quantity,
                        side=side,
                        exchange_segment=params.exchange_segment,
                        product=params.product,
                        sl_price_pending=sl_price_pending,
                        target_price_pending=target_price_pending,
                        instrument_token=params.instrument_token or ""
                    )
                    if sl_price_pending or target_price_pending:
                        self.log_message.emit(f"[INFO] Entry registered - SL/Target will be placed on fill")

            else:
                self.log_message.emit(f"[ERROR] {result.message}")
                if self.sound:
                    self.sound.play('order_rejected')

            # Force refresh after any order action
            self._force_next_orders_refresh()
            QTimer.singleShot(500, self._refresh_positions)
            QTimer.singleShot(600, self._refresh_orders)

        except Exception as e:
            self.log_message.emit(f"[ERROR] Order failed: {str(e)}")
            if self.sound:
                self.sound.play('error')

    def _place_sl_target_after_fill(self, entry_order_id: str, symbol: str,
                                      exchange_segment: str, quantity: int,
                                      entry_action: str, sl_text: str,
                                      target_text: str, product: str,
                                      instrument_token: str = "",
                                      retry_count: int = 0):
        """
        Place SL/Target orders after entry order is filled.
        Called via QTimer after entry order placement.
        """
        max_retries = 8  # Check up to 8 times (12 seconds total)

        try:
            # Check if order is filled
            orders = self.orders.get_orders()
            entry_order = None
            for order in orders:
                if order.get('nOrdNo') == entry_order_id:
                    entry_order = order
                    break

            if not entry_order:
                self.log_message.emit(f"[SL/TGT] Entry order {entry_order_id} not found")
                return

            status = entry_order.get('ordSt', '').lower()

            # Get fill quantities for partial fill handling
            filled_qty = int(entry_order.get('fldQty', 0) or 0)
            total_qty = int(entry_order.get('qty', quantity) or quantity)
            is_partial = status in ['partial', 'partially filled'] or (filled_qty > 0 and filled_qty < total_qty)

            if status in ['complete', 'completed', 'traded', 'filled', 'executed'] or (is_partial and filled_qty > 0):
                # Order filled (fully or partially) - place SL/Target for FILLED quantity only
                if is_partial:
                    self.log_message.emit(f"[SL/TGT] Partial fill detected: {filled_qty}/{total_qty} filled")
                    # Use filled quantity for SL/Target, not original quantity
                    quantity = filled_qty
                else:
                    self.log_message.emit(f"[SL/TGT] Entry filled, placing protection orders...")

                # Determine exit direction (opposite of entry)
                exit_type = 'S' if entry_action == 'B' else 'B'
                is_long = entry_action == 'B'

                sl_order_id = None
                target_order_id = None
                entry_price = float(entry_order.get('avgPrc', 0) or 0)

                # Record entry with P&L tracker for accurate realized P&L calculation
                if self.pnl_tracker and entry_price > 0:
                    self.pnl_tracker.record_entry(
                        symbol=symbol,
                        side='LONG' if is_long else 'SHORT',
                        price=entry_price,
                        qty=filled_qty,
                        order_id=entry_order_id
                    )

                # Place SL order with retry logic
                if sl_text:
                    try:
                        sl_price = float(sl_text)
                        # Use order_manager's retry method for SL placement
                        sl_result = self.orders._place_sl_with_retry(
                            exchange_segment=exchange_segment,
                            product=product,
                            quantity=quantity,
                            trading_symbol=symbol,
                            transaction_type=exit_type,
                            trigger_price=sl_price,
                            max_retries=3
                        )
                        sl_order_id = sl_result.get('order_id')
                        if sl_order_id:
                            self.log_message.emit(f"[SL/TGT] SL placed @ {sl_price} -> {sl_order_id}")

                            # Update partial fill monitor with SL order details
                            if self.partial_fill_monitor:
                                self.partial_fill_monitor.update_sl_order(
                                    entry_order_id=entry_order_id,
                                    sl_order_id=sl_order_id,
                                    sl_price=sl_price,
                                    sl_qty=quantity
                                )

                        elif sl_result.get('critical'):
                            # CRITICAL: SL placement failed after all retries
                            self.log_message.emit(f"[SL/TGT] CRITICAL: SL FAILED - {sl_result.get('error')}")
                            self.log_message.emit(f"[SL/TGT] POSITION {symbol} IS UNPROTECTED!")
                            if self.sound:
                                self.sound.play('error')
                            if self.telegram:
                                self.telegram.send(f"CRITICAL: SL placement failed for {symbol}! Position unprotected!")
                    except Exception as e:
                        self.log_message.emit(f"[SL/TGT] SL placement failed: {e}")

                # Place Target order
                if target_text:
                    try:
                        target_price = float(target_text)
                        target_response = self.session.get_client().place_order(
                            exchange_segment=exchange_segment,
                            product=product,
                            price=str(target_price),
                            order_type="L",
                            quantity=str(quantity),
                            validity="DAY",
                            trading_symbol=symbol,
                            transaction_type=exit_type,
                            amo="NO",
                            tag='TARGET'
                        )
                        target_order_id = target_response.get('nOrdNo') if target_response else None
                        if target_order_id:
                            self.log_message.emit(f"[SL/TGT] Target placed @ {target_price} -> {target_order_id}")
                    except Exception as e:
                        self.log_message.emit(f"[SL/TGT] Target placement failed: {e}")

                # Track position
                if sl_order_id or target_order_id:
                    tracked = self.pos_tracker.get_position(symbol)
                    if not tracked:
                        self.pos_tracker.add_position(
                            symbol=symbol,
                            exchange_segment=exchange_segment,
                            quantity=quantity,
                            side='LONG' if is_long else 'SHORT',
                            entry_price=entry_price
                        )

                    if sl_order_id:
                        sl_price = float(sl_text)
                        self.pos_tracker.set_sl_order(symbol, sl_order_id, sl_price)

                        # Also add to trail manager
                        if self.trail_mgr:
                            self.trail_mgr.add_position(
                                symbol=symbol,
                                exchange_segment=exchange_segment,
                                entry_price=entry_price,
                                quantity=quantity,
                                side='LONG' if is_long else 'SHORT',
                                sl_price=sl_price,
                                sl_order_id=sl_order_id,
                                instrument_token=instrument_token
                            )

                    if target_order_id:
                        target_price = float(target_text)
                        self.pos_tracker.set_target_order(symbol, target_order_id, target_price)

                    # Register OCO pair if both SL and Target are set
                    if sl_order_id and target_order_id and self.oco_monitor:
                        self.oco_monitor.add_oco_pair(
                            position_symbol=symbol,
                            sl_order_id=sl_order_id,
                            target_order_id=target_order_id,
                            sl_trigger=float(sl_text),
                            target_price=float(target_text),
                            quantity=quantity,
                            side='LONG' if is_long else 'SHORT',
                            entry_price=entry_price
                        )
                        self.log_message.emit(f"[OCO] Registered SL/Target pair for {symbol}")

                if self.sound:
                    self.sound.play('order_placed')

            elif status in ['rejected', 'cancelled', 'canceled']:
                self.log_message.emit(f"[SL/TGT] Entry order {status} - skipping protection orders")

            else:
                # Order still pending - retry
                if retry_count < max_retries:
                    if retry_count == 0:
                        self.log_message.emit(f"[SL/TGT] Waiting for fill... (status: {status})")
                    QTimer.singleShot(1500, lambda: self._place_sl_target_after_fill(
                        entry_order_id, symbol, exchange_segment, quantity,
                        entry_action, sl_text, target_text, product, instrument_token, retry_count + 1
                    ))
                else:
                    self.log_message.emit(f"[SL/TGT] Entry order status='{status}' after {max_retries} checks - set SL manually")

        except Exception as e:
            self.log_message.emit(f"[SL/TGT] Error: {e}")

    def _add_to_basket(self):
        """Add current order to basket."""
        if not self.current_mapping:
            self.log_message.emit("[ERROR] No symbol selected")
            return

        # Check basket limit (max 4 legs)
        if len(self.basket_legs) >= 4:
            self.log_message.emit("[ERROR] Basket full (max 4 legs)")
            return

        action = 'B' if self.buy_radio.isChecked() else 'S'
        price_text = self.price_input.text().strip()

        # Validate price for LIMIT orders
        order_type = self.order_type_combo.currentText()
        if order_type == "LIMIT" and not price_text:
            self.price_input.setStyleSheet("background-color: #660000; border: 2px solid red;")
            self.log_message.emit("[ERROR] Price required for LIMIT order")
            if self.sound:
                self.sound.play('error')
            return
        else:
            self.price_input.setStyleSheet("")  # Reset style

        price = float(price_text) if price_text else 0
        qty = int(self.qty_label.text())

        leg = {
            'symbol': self.current_mapping['trading_symbol'],
            'mapping': self.current_mapping,
            'action': action,
            'qty': qty,
            'price': price
        }

        self.basket_legs.append(leg)
        self._update_basket_table()
        action_str = 'BUY' if action == 'B' else 'SELL'
        self.log_message.emit(f"[BASKET] Added: {leg['symbol']} {action_str} {qty} @ ₹{price}")

    def _update_basket_table(self):
        """Update basket table display - always 4 rows."""
        self.basket_table.setRowCount(4)  # Always 4 rows
        net_premium = 0

        # Clear all rows first
        for i in range(4):
            for j in range(5):
                self.basket_table.setItem(i, j, QTableWidgetItem(""))

        # Fill with basket legs
        for i, leg in enumerate(self.basket_legs):
            self.basket_table.setItem(i, 0, QTableWidgetItem(str(i + 1)))
            self.basket_table.setItem(i, 1, QTableWidgetItem(leg['symbol']))

            # B/S with color
            action_item = QTableWidgetItem('B' if leg['action'] == 'B' else 'S')
            if leg['action'] == 'B':
                action_item.setForeground(QBrush(QColor("#00ff88")))  # Green
            else:
                action_item.setForeground(QBrush(QColor("#ff4444")))  # Red
            self.basket_table.setItem(i, 2, action_item)

            self.basket_table.setItem(i, 3, QTableWidgetItem(str(leg['qty'])))
            self.basket_table.setItem(i, 4, QTableWidgetItem(f"{leg['price']:.2f}"))  # No INR symbol

            # Calculate net premium
            if leg['action'] == 'B':
                net_premium -= leg['price'] * leg['qty']
            else:
                net_premium += leg['price'] * leg['qty']

        self.net_premium.setText(f"₹{net_premium:,.0f}")
        self.leg_count.setText(str(len(self.basket_legs)))

    def _clear_basket(self):
        """Clear basket."""
        self.basket_legs.clear()
        self._update_basket_table()

    def _execute_basket(self):
        """Execute all basket legs."""
        if not self.basket_legs:
            return

        leg_params = []
        for leg in self.basket_legs:
            params = OrderParams(
                symbol=leg['mapping']['trading_symbol'],
                exchange_segment=leg['mapping']['exchange_segment'],
                instrument_token=leg['mapping']['instrument_token'],
                transaction_type=leg['action'],
                quantity=leg['qty'],
                product=self.product_combo.currentText(),
                order_type='L' if leg['price'] > 0 else 'MKT',
                price=leg['price'] if leg['price'] > 0 else None,
            )
            leg_params.append(params)

        results = self.orders.place_multi_leg_order(leg_params)

        for i, result in enumerate(results):
            if result.success:
                self.log_message.emit(f"[BASKET] Leg {i+1}: Order placed -> ID: {result.order_id}")
            else:
                self.log_message.emit(f"[BASKET] Leg {i+1}: Failed - {result.message}")

        self._clear_basket()
        QTimer.singleShot(500, self._refresh_positions)

    def _load_preset(self, underlying: str, opt_type: str, strike_offset: int):
        """Load preset ATM option based on Monthly/Weekly setting."""
        if not self.kite or not self.kite.is_connected():
            self.log_message.emit("[ERROR] Kite not connected for spot price")
            return

        try:
            # Get expiry type from combo box
            use_monthly = self.expiry_combo.currentText() == "Monthly"

            # Get symbol with appropriate expiry
            symbol = self.kite.get_option_symbol(
                underlying, opt_type, strike_offset,
                use_monthly=use_monthly
            )

            # Clear price fields when loading new preset (keep lots)
            self.price_input.clear()
            self.price_input.setStyleSheet("")  # Reset any red highlight
            self.entry_sl_input.clear()
            self.target_input.clear()

            # Set symbol and trigger mapping
            self.symbol_input.setCurrentText(symbol)
            self._on_symbol_entered()

            expiry_type = "Monthly" if use_monthly else "Weekly"
            self.log_message.emit(f"[PRESET] {underlying} {opt_type} ATM ({expiry_type}): {symbol}")

        except Exception as e:
            self.log_message.emit(f"[ERROR] Preset load failed: {str(e)}")

    def _adjust_lots(self, delta: int):
        """Adjust lot count."""
        new_value = max(1, self.lots_spin.value() + delta)
        self.lots_spin.setValue(new_value)

    def _clear_inputs(self):
        """Clear all input fields and display labels."""
        self.symbol_input.clear()
        self.price_input.clear()
        self.price_input.setStyleSheet("")  # Reset any red highlight
        self.entry_sl_input.clear()
        self.target_input.clear()
        self.current_mapping = None
        self.qty_label.setText("0")
        # Clear price display labels
        self.ltp_label.setText("--")
        self.ltp_label.setStyleSheet("color: #00ccff;")
        self.bid_label.setText("--")
        self.ask_label.setText("--")
        # Clear SL/Target percentage labels
        self.sl_pct_label.setText("")
        self.tgt_pct_label.setText("")

    # ==================== Position Management ====================

    def _refresh_positions(self):
        """Refresh positions from broker and sync with tracker."""
        try:
            positions = self.orders.get_positions()

            # Debug logging removed - was for initial field discovery only

            self._positions_cache = positions
            self.position_updated.emit(positions)

            # Sync position tracker with broker - cleanup closed positions
            # Find symbols that are no longer in broker positions
            broker_symbols = set()
            for pos in positions:
                symbol = _get_position_symbol(pos)
                qty = _get_position_qty(pos)
                if qty != 0:  # Only active positions
                    broker_symbols.add(symbol)

            # Find tracked positions that are no longer at broker
            closed_symbols = []
            for symbol in list(self.pos_tracker.positions.keys()):
                if symbol not in broker_symbols:
                    closed_symbols.append(symbol)

            # Cleanup closed positions - cancel any orphan orders
            for symbol in closed_symbols:
                self.log_message.emit(f"[SYNC] Position closed: {symbol} - cleaning up orders")
                cancelled = self.pos_tracker.cancel_all_orders_for_position(symbol)
                if cancelled.get('sl_cancelled'):
                    self.log_message.emit(f"[SYNC] Cancelled orphan SL: {cancelled['sl_cancelled']}")
                if cancelled.get('target_cancelled'):
                    self.log_message.emit(f"[SYNC] Cancelled orphan Target: {cancelled['target_cancelled']}")

                # Remove from trail manager
                if self.trail_mgr and symbol in self.trail_mgr.positions:
                    del self.trail_mgr.positions[symbol]

                # Remove from OCO monitor
                if self.oco_monitor:
                    self.oco_monitor.remove_oco_pair(symbol)

                # Remove from tracker
                if symbol in self.pos_tracker.positions:
                    del self.pos_tracker.positions[symbol]

            # Calculate total P&L
            total_pnl = 0
            for pos in positions:
                pnl = _get_position_pnl(pos)
                total_pnl += pnl

            self.pnl_updated.emit(total_pnl)

        except Exception as e:
            self.log_message.emit(f"[ERROR] Position refresh failed: {str(e)}")

    def _on_position_cell_clicked(self, row: int, column: int):
        """Handle cell click in positions table - copy Symbol to clipboard."""
        if column != 0:  # Only Symbol column
            return

        symbol_item = self.positions_table.item(row, 0)
        if not symbol_item:
            return

        symbol_text = symbol_item.text()
        if symbol_text:
            QApplication.clipboard().setText(symbol_text)
            self.log_message.emit(f"[INFO] Copied: {symbol_text}")

    def _on_pos_filter_changed(self, filter_text: str):
        """Handle position filter dropdown change."""
        # Update P&L label based on filter
        label_map = {
            "Open": "Open P&L:",
            "Closed": "Closed P&L:",
            "All": "Total P&L:"
        }
        self.pos_pnl_label.setText(label_map.get(filter_text, "P&L:"))

        # Refresh positions table with current filter
        if hasattr(self, '_positions_cache') and self._positions_cache:
            self._update_positions_table(self._positions_cache)

    def _update_positions_table(self, positions: List[Dict[str, Any]]):
        """Update positions table with data based on filter."""
        # Get current filter
        filter_type = self.pos_filter_combo.currentText() if hasattr(self, 'pos_filter_combo') else "Open"

        # Filter positions based on selection
        if filter_type == "Open":
            filtered_positions = [p for p in positions if _get_position_qty(p) != 0]
        elif filter_type == "Closed":
            filtered_positions = [p for p in positions if _get_position_qty(p) == 0]
        else:  # All
            filtered_positions = positions

        self.positions_table.setRowCount(len(filtered_positions))
        self._filtered_positions_cache = filtered_positions  # Store for cell widget access
        total_pnl = 0

        for row, pos in enumerate(filtered_positions):
            symbol = _get_position_symbol(pos)
            qty = _get_position_qty(pos)
            avg = _get_position_avg_price(pos)
            ltp = _get_position_ltp(pos)
            pnl = _get_position_pnl(pos)

            # P&L % calculation - handle closed positions correctly
            # For open positions with LTP: unrealized % = (LTP - avg) / avg * 100
            # For closed positions (qty=0 or LTP=0): realized % = P&L / cost * 100
            if qty != 0 and ltp > 0 and avg > 0:
                # Open position with live LTP - use unrealized calculation
                pnl_pct = ((ltp - avg) / avg * 100)
            elif avg > 0 and pnl != 0:
                # Closed position or no LTP - use realized P&L percentage
                # Cost basis = avg * total_buy_qty (but we use buyAmt directly)
                buy_amt = float(pos.get('buyAmt', 0) or 0)
                if buy_amt > 0:
                    pnl_pct = (pnl / buy_amt * 100)
                else:
                    pnl_pct = 0
            else:
                pnl_pct = 0

            # Symbol (non-editable)
            symbol_item = QTableWidgetItem(symbol)
            symbol_item.setFlags(symbol_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.positions_table.setItem(row, 0, symbol_item)

            # Qty (color coded, non-editable)
            qty_item = QTableWidgetItem(str(qty))
            qty_item.setForeground(QBrush(QColor("#00ff88" if qty > 0 else "#ff4444")))
            qty_item.setFlags(qty_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.positions_table.setItem(row, 1, qty_item)

            # Avg (non-editable)
            avg_item = QTableWidgetItem(f"{avg:.2f}")
            avg_item.setFlags(avg_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.positions_table.setItem(row, 2, avg_item)

            # LTP (non-editable)
            ltp_item = QTableWidgetItem(f"{ltp:.2f}")
            ltp_item.setFlags(ltp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.positions_table.setItem(row, 3, ltp_item)

            # P&L (color coded, non-editable)
            pnl_item = QTableWidgetItem(f"₹{pnl:,.0f}")
            pnl_item.setForeground(QBrush(QColor("#00ff88" if pnl >= 0 else "#ff4444")))
            pnl_item.setFlags(pnl_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.positions_table.setItem(row, 4, pnl_item)

            # P&L % (non-editable)
            pct_item = QTableWidgetItem(f"{pnl_pct:+.1f}%")
            pct_item.setForeground(QBrush(QColor("#00ff88" if pnl_pct >= 0 else "#ff4444")))
            pct_item.setFlags(pct_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.positions_table.setItem(row, 5, pct_item)

            # SL Input
            sl_widget = self._create_sl_widget(symbol, pos)
            self.positions_table.setCellWidget(row, 6, sl_widget)

            # Trail buttons
            trail_widget = self._create_trail_widget(symbol)
            self.positions_table.setCellWidget(row, 7, trail_widget)

            # Exit buttons
            exit_widget = self._create_exit_widget(pos)
            self.positions_table.setCellWidget(row, 8, exit_widget)

            total_pnl += pnl

        # Update total
        self.total_pnl.setText(f"₹{total_pnl:,.0f}")
        self.total_pnl.setStyleSheet(f"color: {'#00ff88' if total_pnl >= 0 else '#ff4444'};")

    def _create_sl_widget(self, symbol: str, pos: Dict[str, Any]) -> QWidget:
        """Create compact SL input widget - just input field, Enter to set."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        sl_input = QLineEdit()
        sl_input.setPlaceholderText("SL")
        sl_input.setFixedWidth(60)
        sl_input.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Check if we have a tracked SL for this position
        tracked = self.pos_tracker.get_position(symbol)
        if tracked and tracked.sl_price:
            sl_input.setText(f"{tracked.sl_price:.2f}")
            sl_input.setStyleSheet("""
                QLineEdit {
                    background-color: #0d3320;
                    border: 2px solid #00cc55;
                    border-radius: 4px;
                    color: #00ff66;
                    font-weight: bold;
                    font-size: 11px;
                    padding: 3px;
                }
                QLineEdit:focus {
                    border: 2px solid #00ff88;
                    background-color: #0a4428;
                }
            """)
        else:
            sl_input.setStyleSheet("""
                QLineEdit {
                    background-color: #2a2a35;
                    border: 2px solid #555566;
                    border-radius: 4px;
                    color: #aaaaaa;
                    font-size: 11px;
                    padding: 3px;
                }
                QLineEdit:focus {
                    border: 2px solid #ffaa00;
                    color: #ffcc00;
                    background-color: #332a1a;
                }
            """)

        sl_input.returnPressed.connect(lambda s=symbol, inp=sl_input: self._update_sl_from_input(s, inp.text()))
        layout.addWidget(sl_input)

        self.sl_inputs[symbol] = sl_input
        return widget

    def _adjust_sl_value(self, sl_input: QLineEdit, delta: float):
        """Adjust SL value in input field by delta points."""
        try:
            current_text = sl_input.text().strip()
            if current_text:
                current_val = float(current_text)
                new_val = max(0.05, current_val + delta)  # Minimum 0.05
                sl_input.setText(f"{new_val:.2f}")
            else:
                # If empty, don't do anything
                pass
        except ValueError:
            pass  # Invalid input, ignore

    def _create_trail_widget(self, symbol: str) -> QWidget:
        """Create trail buttons widget - bigger buttons with clear text."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # Breakeven button - cyan/teal
        be_btn = QPushButton("BE")
        be_btn.setToolTip("Trail to TRUE Breakeven (Entry + Charges)")
        be_btn.setFixedSize(44, 28)
        be_btn.setStyleSheet("""
            QPushButton {
                background-color: #006688;
                color: #ffffff;
                font-weight: bold;
                font-size: 12px;
                border: 1px solid #0088aa;
                border-radius: 3px;
                margin: 0px;
                padding-top: 0px;
                padding-bottom: 4px;
            }
            QPushButton:hover {
                background-color: #0099bb;
            }
            QPushButton:pressed {
                background-color: #004466;
            }
        """)
        be_btn.clicked.connect(lambda _, s=symbol: self._trail_to_cost(s))
        layout.addWidget(be_btn)

        # +10 button - green
        t10_btn = QPushButton("+10")
        t10_btn.setToolTip("Trail SL +10 points")
        t10_btn.setFixedSize(46, 28)
        t10_btn.setStyleSheet("""
            QPushButton {
                background-color: #227744;
                color: #ffffff;
                font-weight: bold;
                font-size: 12px;
                border: 1px solid #33aa55;
                border-radius: 3px;
                margin: 0px;
                padding-top: 0px;
                padding-bottom: 4px;
            }
            QPushButton:hover {
                background-color: #33bb66;
            }
            QPushButton:pressed {
                background-color: #115533;
            }
        """)
        t10_btn.clicked.connect(lambda _, s=symbol: self._trail_by_points(s, 10))
        layout.addWidget(t10_btn)

        # +25 button - bright green
        t25_btn = QPushButton("+25")
        t25_btn.setToolTip("Trail SL +25 points")
        t25_btn.setFixedSize(46, 28)
        t25_btn.setStyleSheet("""
            QPushButton {
                background-color: #33aa55;
                color: #ffffff;
                font-weight: bold;
                font-size: 12px;
                border: 1px solid #44cc66;
                border-radius: 3px;
                margin: 0px;
                padding-top: 0px;
                padding-bottom: 4px;
            }
            QPushButton:hover {
                background-color: #44dd77;
            }
            QPushButton:pressed {
                background-color: #228844;
            }
        """)
        t25_btn.clicked.connect(lambda _, s=symbol: self._trail_by_points(s, 25))
        layout.addWidget(t25_btn)

        return widget

    def _create_exit_widget(self, pos: Dict[str, Any]) -> QWidget:
        """Create exit buttons widget - bigger buttons with clear text."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # 50% exit - amber/orange
        exit_50 = QPushButton("50%")
        exit_50.setToolTip("Exit 50% of position")
        exit_50.setFixedSize(48, 28)
        exit_50.setStyleSheet("""
            QPushButton {
                background-color: #aa7700;
                color: #ffffff;
                font-weight: bold;
                font-size: 12px;
                border: 1px solid #cc9900;
                border-radius: 3px;
                margin: 0px;
                padding-top: 0px;
                padding-bottom: 4px;
            }
            QPushButton:hover {
                background-color: #cc9900;
            }
            QPushButton:pressed {
                background-color: #885500;
            }
        """)
        exit_50.clicked.connect(lambda _, p=pos: self._exit_position(p, 50))
        layout.addWidget(exit_50)

        # Full EXIT - bright red, prominent
        exit_full = QPushButton("EXIT")
        exit_full.setToolTip("Exit 100% - Close position")
        exit_full.setFixedSize(52, 28)
        exit_full.setStyleSheet("""
            QPushButton {
                background-color: #cc2222;
                color: #ffffff;
                font-weight: bold;
                font-size: 12px;
                border: 1px solid #ee3333;
                border-radius: 3px;
                margin: 0px;
                padding-top: 0px;
                padding-bottom: 4px;
            }
            QPushButton:hover {
                background-color: #ee3333;
            }
            QPushButton:pressed {
                background-color: #991111;
            }
        """)
        exit_full.clicked.connect(lambda _, p=pos: self._exit_position(p, 100))
        layout.addWidget(exit_full)

        return widget

    def _update_sl_from_input(self, symbol: str, price_str: str):
        """Update SL to manually entered price - place or modify SL order."""
        try:
            new_sl = float(price_str.strip())
            if new_sl <= 0:
                self.log_message.emit(f"[SL] Invalid price: {price_str}")
                return

            # Find position in cache
            pos = None
            for p in self._positions_cache:
                if _get_position_symbol(p) == symbol:
                    pos = p
                    break

            if not pos:
                self.log_message.emit(f"[SL] Position not found: {symbol}")
                return

            qty = abs(_get_position_qty(pos))
            if qty == 0:
                self.log_message.emit(f"[SL] No quantity to protect")
                return

            # Determine exit direction
            is_long = _get_position_qty(pos) > 0
            exit_type = 'S' if is_long else 'B'

            # Get prices for validation
            entry_price = _get_position_avg_price(pos)
            ltp = _get_position_ltp(pos) or entry_price

            # Validate SL price
            min_distance = self.config.get('trailing_sl', {}).get('min_sl_distance', 5)
            validation = self.orders.validate_sl_price(new_sl, entry_price, ltp, is_long, min_distance)
            if not validation['valid']:
                self.log_message.emit(f"[SL] INVALID: {validation['error']}")
                if self.sound:
                    self.sound.play('error')
                return

            # Check if we already have SL order - modify it
            tracked = self.pos_tracker.get_position(symbol)
            if tracked and tracked.sl_order_id:
                # Modify existing SL
                try:
                    result = self.session.get_client().modify_order(
                        order_id=tracked.sl_order_id,
                        trigger_price=str(new_sl)
                    )

                    # Check if modification succeeded
                    if result and result.get('stat', '').lower() in ['not_ok', 'error', 'rejected']:
                        error_msg = result.get('errMsg') or result.get('message') or 'Modify failed'
                        self.log_message.emit(f"[SL] Modify failed: {error_msg}")
                        if self.sound:
                            self.sound.play('error')
                        return

                    self.pos_tracker.update_sl_order(symbol, new_sl_price=new_sl)
                    self.log_message.emit(f"[SL] {symbol}: Modified SL -> {new_sl:.2f}")
                    if self.sound:
                        self.sound.play('order_placed')

                    # Update trail manager if registered
                    if self.trail_mgr and symbol in self.trail_mgr.positions:
                        self.trail_mgr.positions[symbol].current_sl = new_sl

                    # Refresh orders table to show updated price
                    self._force_next_orders_refresh()
                    QTimer.singleShot(500, self._refresh_orders)

                except Exception as e:
                    self.log_message.emit(f"[SL] Modify failed: {e}")
                    if self.sound:
                        self.sound.play('error')
            else:
                # Place new SL order
                try:
                    exchange_seg = _get_position_exchange(pos)
                    product = _get_position_product(pos)
                    sl_response = self.session.get_client().place_order(
                        exchange_segment=exchange_seg,
                        product=product,
                        price="0",
                        order_type="SL-M",
                        quantity=str(qty),
                        validity="DAY",
                        trading_symbol=symbol,
                        transaction_type=exit_type,
                        amo="NO",
                        trigger_price=str(new_sl),
                        tag='SL'
                    )

                    sl_order_id = sl_response.get('nOrdNo') if sl_response else None
                    if sl_order_id:
                        # Track the position if not already tracked
                        if not tracked:
                            avg_price = _get_position_avg_price(pos)
                            self.pos_tracker.add_position(
                                symbol=symbol,
                                exchange_segment=exchange_seg,
                                quantity=qty,
                                side='LONG' if is_long else 'SHORT',
                                entry_price=avg_price
                            )

                        # Record SL order
                        self.pos_tracker.set_sl_order(symbol, sl_order_id, new_sl)

                        # Register with trail manager
                        if self.trail_mgr:
                            avg_price = _get_position_avg_price(pos)
                            inst_token = str(pos.get('instrument_token', pos.get('token', '')))
                            self.trail_mgr.add_position(
                                symbol=symbol,
                                exchange_segment=exchange_seg,
                                entry_price=avg_price,
                                quantity=qty,
                                side='LONG' if is_long else 'SHORT',
                                sl_price=new_sl,
                                sl_order_id=sl_order_id,
                                instrument_token=inst_token
                            )

                        self.log_message.emit(f"[SL] {symbol}: Placed SL @ {new_sl:.2f} -> {sl_order_id}")
                        if self.sound:
                            self.sound.play('order_placed')

                        # Refresh orders table to show new SL order
                        self._force_next_orders_refresh()
                        QTimer.singleShot(500, self._refresh_orders)
                    else:
                        self.log_message.emit(f"[SL] {symbol}: Order placed but no ID returned")

                except Exception as e:
                    self.log_message.emit(f"[SL] Place failed: {e}")
                    if self.sound:
                        self.sound.play('error')

        except ValueError:
            self.log_message.emit(f"[SL] Invalid price format: {price_str}")

    def _trail_to_cost(self, symbol: str):
        """Trail SL to TRUE breakeven (entry + charges)."""
        if self.trail_mgr:
            result = self.trail_mgr.trail_to_cost(symbol)
            if result['success']:
                new_sl = result['new_sl']
                self.log_message.emit(f"[TRAIL] {symbol}: SL → TRUE BE ({new_sl:.2f})")
                # Sync new SL with position tracker
                self.pos_tracker.update_sl_order(symbol, new_sl_price=new_sl)
                if self.sound:
                    self.sound.play('order_placed')
            else:
                self.log_message.emit(f"[TRAIL] {symbol}: Failed - {result.get('error', 'Unknown')}")

    def _trail_by_points(self, symbol: str, points: float):
        """Trail SL by points."""
        if self.trail_mgr:
            result = self.trail_mgr.trail_by_points(symbol, points)
            if result['success']:
                new_sl = result['new_sl']
                self.log_message.emit(f"[TRAIL] {symbol}: SL -> {new_sl:.2f} (+{points}pts)")
                # Sync new SL with position tracker
                self.pos_tracker.update_sl_order(symbol, new_sl_price=new_sl)
                if self.sound:
                    self.sound.play('order_placed')
            else:
                self.log_message.emit(f"[TRAIL] {symbol}: Failed - {result.get('error', 'Unknown')}")

    def _trail_selected_to_cost(self):
        """Trail selected position to cost."""
        row = self.positions_table.currentRow()
        if row >= 0 and row < len(self._positions_cache):
            symbol = _get_position_symbol(self._filtered_positions_cache[row])
            self._trail_to_cost(symbol)

    def _trail_selected_plus(self, points: float):
        """Trail selected position by points."""
        row = self.positions_table.currentRow()
        if row >= 0 and row < len(self._positions_cache):
            symbol = _get_position_symbol(self._filtered_positions_cache[row])
            self._trail_by_points(symbol, points)

    def _exit_position(self, pos: Dict[str, Any], percent: int):
        """Exit position by percentage - EXIT FIRST, then cancel SL/Target.

        CRITICAL: Place exit order FIRST, only cancel SL/Target AFTER exit succeeds.
        This prevents leaving positions unprotected if exit order fails.
        """
        symbol = _get_position_symbol(pos)
        qty = abs(_get_position_qty(pos))

        # Calculate exit quantity
        exit_qty = int(qty * percent / 100)
        remaining_qty = qty - exit_qty
        is_full_exit = percent == 100 or remaining_qty == 0

        # For FULL exit, cancel pending ENTRY orders first (these don't have protection yet)
        if is_full_exit:
            if self.partial_fill_monitor:
                cancelled_entry = self.partial_fill_monitor.cancel_pending_entry(symbol)
                if cancelled_entry:
                    self.log_message.emit(f"[EXIT] Cancelled pending entry: {cancelled_entry[-6:]}")

        # CRITICAL: Place exit order FIRST
        result = self.orders.exit_position(pos, percent)
        if result.success:
            self.log_message.emit(f"[EXIT] {symbol} {percent}% ({exit_qty} qty) -> ID: {result.order_id}")
            if self.sound:
                self.sound.play('order_placed')

            # ONLY NOW (after exit succeeds) cancel SL/Target for FULL exits
            if is_full_exit:
                # Cancel SL/Target orders
                cancelled = self.pos_tracker.cancel_all_orders_for_position(symbol)
                if cancelled.get('sl_cancelled'):
                    self.log_message.emit(f"[EXIT] Cancelled SL order: {cancelled['sl_cancelled']}")
                if cancelled.get('target_cancelled'):
                    self.log_message.emit(f"[EXIT] Cancelled Target order: {cancelled['target_cancelled']}")

                # Remove from trail manager
                if self.trail_mgr and symbol in self.trail_mgr.positions:
                    del self.trail_mgr.positions[symbol]
                    self.log_message.emit(f"[EXIT] Removed {symbol} from trail manager")

                # Remove from OCO monitor
                if self.oco_monitor:
                    self.oco_monitor.remove_oco_pair(symbol)

            # Schedule P&L recording after exit fill is confirmed
            if self.pnl_tracker and result.order_id:
                QTimer.singleShot(1000, lambda: self._record_exit_pnl(
                    symbol=symbol,
                    exit_order_id=result.order_id,
                    exit_qty=exit_qty,
                    exit_type='EXIT'
                ))

            # For partial exit, modify SL order quantity using order_manager with retry logic
            if not is_full_exit and remaining_qty > 0:
                tracked = self.pos_tracker.get_position(symbol)
                if tracked and tracked.sl_order_id:
                    # Update tracker quantity
                    self.pos_tracker.reduce_position_qty(symbol, exit_qty)

                    # Determine exit type for SL
                    is_long = _get_position_qty(pos) > 0
                    exit_type = 'S' if is_long else 'B'

                    # Use order_manager's method with retry logic
                    adjust_result = self.orders.adjust_sl_after_partial_exit(
                        symbol=symbol,
                        remaining_qty=remaining_qty,
                        sl_order_id=tracked.sl_order_id,
                        sl_price=tracked.sl_price,
                        exchange_segment=_get_position_exchange(pos),
                        product=_get_position_product(pos),
                        transaction_type=exit_type,
                        position_tracker=self.pos_tracker,
                        oco_monitor=self.oco_monitor
                    )

                    if adjust_result.get('success'):
                        action = adjust_result.get('action', 'modified')
                        new_sl_id = adjust_result.get('new_sl_order_id')
                        self.log_message.emit(f"[EXIT] SL qty adjusted to {remaining_qty} ({action}) -> {new_sl_id}")

                        # Update trail manager with new SL order ID if recreated
                        if action == 'recreated' and self.trail_mgr and symbol in self.trail_mgr.positions:
                            self.trail_mgr.positions[symbol].sl_order_id = new_sl_id
                    else:
                        error_msg = adjust_result.get('error', 'Unknown error')
                        is_critical = adjust_result.get('critical', False)
                        if is_critical:
                            self.log_message.emit(f"[EXIT] CRITICAL: SL adjustment FAILED - {error_msg}")
                            self.log_message.emit(f"[EXIT] POSITION {symbol} MAY BE UNPROTECTED!")
                            if self.sound:
                                self.sound.play('error')
                            if self.telegram:
                                self.telegram.send(f"CRITICAL: SL failed for {symbol} after partial exit!")
                        else:
                            self.log_message.emit(f"[EXIT] SL adjustment failed - {error_msg}")
            else:
                # Full exit - close position tracking
                self.pos_tracker.close_position(symbol)
        else:
            self.log_message.emit(f"[EXIT] Failed - {result.message}")

        QTimer.singleShot(500, self._refresh_positions)

    def _recreate_sl_after_partial(self, symbol: str, sl_price: float, qty: int, pos: Dict[str, Any]):
        """Recreate SL order after partial exit when modify failed."""
        try:
            is_long = _get_position_qty(pos) > 0
            exit_type = 'S' if is_long else 'B'

            sl_response = self.session.get_client().place_order(
                exchange_segment=_get_position_exchange(pos),
                product=_get_position_product(pos),
                price="0",
                order_type="SL-M",
                quantity=str(qty),
                validity="DAY",
                trading_symbol=symbol,
                transaction_type=exit_type,
                amo="NO",
                trigger_price=str(sl_price),
                tag='SL'
            )

            sl_order_id = sl_response.get('nOrdNo') if sl_response else None
            if sl_order_id:
                self.pos_tracker.set_sl_order(symbol, sl_order_id, sl_price)
                self.log_message.emit(f"[EXIT] Recreated SL @ {sl_price} -> {sl_order_id}")

                # Update trail manager
                if self.trail_mgr and symbol in self.trail_mgr.positions:
                    self.trail_mgr.positions[symbol].sl_order_id = sl_order_id

                # Update OCO monitor with new SL order ID
                if self.oco_monitor and self.oco_monitor.is_monitoring(symbol):
                    self.oco_monitor.update_sl_order(symbol, sl_order_id, sl_price)
                    self.log_message.emit(f"[EXIT] Updated OCO monitor with new SL")
            else:
                self.log_message.emit(f"[EXIT] SL recreation returned no order ID")

        except Exception as e:
            self.log_message.emit(f"[EXIT] SL recreation failed: {e}")

    def _record_exit_pnl(self, symbol: str, exit_order_id: str, exit_qty: int,
                          exit_type: str = 'EXIT', retry_count: int = 0):
        """
        Record exit P&L after order fills.
        Called via QTimer after exit order placement.
        """
        max_retries = 3

        try:
            orders = self.orders.get_orders()
            exit_order = None
            for order in orders:
                if order.get('nOrdNo') == exit_order_id:
                    exit_order = order
                    break

            if not exit_order:
                if retry_count < max_retries:
                    QTimer.singleShot(1000, lambda: self._record_exit_pnl(
                        symbol, exit_order_id, exit_qty, exit_type, retry_count + 1
                    ))
                return

            status = exit_order.get('ordSt', '').lower()

            if status in ['complete', 'traded', 'filled']:
                # Get fill price
                exit_price = float(exit_order.get('avgPrc', 0) or 0)
                filled_qty = int(exit_order.get('fldQty', 0) or exit_qty)

                if exit_price > 0 and self.pnl_tracker:
                    result = self.pnl_tracker.record_exit(
                        symbol=symbol,
                        exit_price=exit_price,
                        exit_qty=filled_qty,
                        order_id=exit_order_id,
                        exit_type=exit_type
                    )

                    pnl = result.get('pnl', 0)
                    pnl_pct = result.get('pnl_percent', 0)
                    self.log_message.emit(
                        f"[PNL] {symbol} exit recorded: ₹{pnl:+,.2f} ({pnl_pct:+.2f}%)"
                    )

            elif status in ['pending', 'open', 'trigger pending']:
                # Still pending - retry
                if retry_count < max_retries:
                    QTimer.singleShot(1000, lambda: self._record_exit_pnl(
                        symbol, exit_order_id, exit_qty, exit_type, retry_count + 1
                    ))

        except Exception as e:
            self.log_message.emit(f"[PNL] Error recording exit: {e}")

    def _exit_all_positions(self):
        """Exit all positions safely - exits FIRST, then clears trackers only for successful exits.

        CRITICAL: Never clear trackers before confirming exits succeeded.
        Failed exits keep their SL/Target protection intact.
        """
        reply = QMessageBox.question(
            self, "Confirm Exit All",
            "Are you sure you want to EXIT ALL positions?\n(SL/Target orders will be cancelled for successful exits only)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self.log_message.emit("[EXIT ALL] Starting safe exit sequence...")

            # Step 1: Cancel pending ENTRY orders first (these don't have positions yet)
            if self.partial_fill_monitor:
                pending_entries = self.partial_fill_monitor.get_all_pending_entries()
                for entry in pending_entries:
                    cancelled = self.partial_fill_monitor.cancel_pending_entry(entry.symbol)
                    if cancelled:
                        self.log_message.emit(f"[EXIT ALL] Cancelled pending entry: {entry.symbol}")

            # Step 2: Get current positions BEFORE any exit attempts
            tracked_symbols = list(self.pos_tracker.positions.keys())
            self.log_message.emit(f"[EXIT ALL] Attempting to exit {len(tracked_symbols)} tracked positions...")

            # Step 3: EXIT POSITIONS FIRST - track successes and failures
            results = self.orders.exit_all_positions()

            successful_exits = set()
            failed_exits = set()

            for r in results:
                symbol = r.get('symbol', '')
                status = r.get('status', 'unknown').lower()

                if status in ['success', 'ok', 'complete', 'submitted']:
                    successful_exits.add(symbol)
                    self.log_message.emit(f"[EXIT ALL] ✓ {symbol}: EXIT OK")
                else:
                    failed_exits.add(symbol)
                    self.log_message.emit(f"[EXIT ALL] ✗ {symbol}: EXIT FAILED - {status}")

            # Step 4: ONLY clear trackers for SUCCESSFUL exits
            cancelled_count = 0
            for symbol in successful_exits:
                # Cancel SL/Target orders for this position
                if symbol in self.pos_tracker.positions:
                    cancelled = self.pos_tracker.cancel_all_orders_for_position(symbol)
                    if cancelled.get('sl_cancelled') or cancelled.get('target_cancelled'):
                        cancelled_count += 1

                # Clear from trail manager
                if self.trail_mgr and symbol in self.trail_mgr.positions:
                    del self.trail_mgr.positions[symbol]

                # Clear from OCO monitor
                if self.oco_monitor:
                    self.oco_monitor.remove_pair_by_symbol(symbol)

                # Clear from position tracker
                if symbol in self.pos_tracker.positions:
                    del self.pos_tracker.positions[symbol]

            self.log_message.emit(f"[EXIT ALL] Cancelled SL/Target for {cancelled_count} exited positions")

            # Step 5: ALERT if any exits failed - these positions still have protection!
            if failed_exits:
                failed_list = ', '.join(list(failed_exits)[:5])  # Show first 5
                self.log_message.emit(f"[EXIT ALL] ⚠️ FAILED EXITS: {failed_list}")
                self.log_message.emit(f"[EXIT ALL] ⚠️ {len(failed_exits)} positions STILL OPEN with SL/Target protection")

                # Send Telegram alert for failed exits
                if self.telegram:
                    self.telegram.send(
                        f"⚠️ EXIT ALL: {len(failed_exits)} positions FAILED to exit!\n"
                        f"Positions: {failed_list}\n"
                        f"SL/Target orders kept intact."
                    )

                # Show warning dialog
                QMessageBox.warning(
                    self, "Exit All - Partial Failure",
                    f"{len(failed_exits)} positions FAILED to exit.\n\n"
                    f"These positions still have SL/Target protection.\n"
                    f"Please check and exit manually if needed."
                )
            else:
                self.log_message.emit(f"[EXIT ALL] ✓ All {len(successful_exits)} positions exited successfully")

            if self.sound:
                self.sound.play('position_exit')

            self._force_next_orders_refresh()
            QTimer.singleShot(500, self._refresh_positions)
            QTimer.singleShot(600, self._refresh_orders)

    # ==================== Order Management ====================

    def _force_next_orders_refresh(self):
        """Force the next orders refresh to happen (bypass optimization).

        Call this after placing/modifying/cancelling any order.
        """
        self._force_orders_refresh = True

    def _should_refresh_orders(self) -> bool:
        """Check if order refresh is needed based on current state.

        Returns True if:
        - Force flag is set (after order action)
        - Pending orders exist (from last check)
        - Open positions exist (SL/Target may be pending)
        - OCO monitor has active pairs
        - Trailing SL has active positions
        - PartialFillMonitor has pending entries
        - Safety: every 6th call (30 seconds) regardless
        """
        # Check force flag first (set after order actions)
        if getattr(self, '_force_orders_refresh', False):
            self._force_orders_refresh = False
            return True

        # Safety counter - force refresh every 30 seconds
        if not hasattr(self, '_orders_refresh_skip_count'):
            self._orders_refresh_skip_count = 0

        self._orders_refresh_skip_count += 1
        if self._orders_refresh_skip_count >= 6:  # 6 × 5s = 30s
            self._orders_refresh_skip_count = 0
            return True

        # Check for pending orders from last refresh
        if hasattr(self, '_has_pending_orders') and self._has_pending_orders:
            return True

        # Check for open positions
        if self.pos_tracker and self.pos_tracker.positions:
            return True

        # Check OCO monitor
        if self.oco_monitor and hasattr(self.oco_monitor, 'pairs') and self.oco_monitor.pairs:
            return True

        # Check trailing SL manager
        if self.trail_mgr and hasattr(self.trail_mgr, 'auto_trail_positions') and self.trail_mgr.auto_trail_positions:
            return True

        # Check partial fill monitor for pending entries
        if self.partial_fill_monitor:
            pending = self.partial_fill_monitor.get_all_pending_entries()
            if pending:
                return True

        return False

    def _refresh_orders(self):
        """Refresh orders from broker (conditional - skips if nothing to track)."""
        # Optimization: skip refresh if nothing to track
        if not self._should_refresh_orders():
            return

        try:
            orders = self.orders.get_orders()

            # Track if any pending orders exist for next cycle optimization
            self._has_pending_orders = any(
                order.get('ordSt', order.get('status', '')).lower()
                in ['pending', 'open', 'trigger pending', 'after market order req received']
                for order in orders
            )

            self.order_updated.emit(orders)
        except Exception as e:
            self.log_message.emit(f"[ERROR] Order refresh failed: {str(e)}")

    def _shorten_order_id(self, order_id: str) -> str:
        """Shorten order ID by removing date prefix and leading zeros.

        Example: 260116000681488 -> 681488
        Format: YYMMDD + order_number_with_leading_zeros
        """
        order_id = str(order_id)
        if len(order_id) > 6:
            # Remove first 6 digits (date: YYMMDD)
            order_num = order_id[6:]
            # Remove leading zeros
            order_num = order_num.lstrip('0') or '0'
            return order_num
        return order_id

    def _update_orders_table(self, orders: List[Dict[str, Any]]):
        """Update orders table: Symbol, B/S, Qty, LTP, Price, Status, Time, X.

        Preserves user's sort selection across refreshes.
        """
        # Check for SL fills (backup detection if WebSocket misses it)
        if not hasattr(self, '_alerted_sl_orders'):
            self._alerted_sl_orders = set()

        for order in orders:
            order_id = order.get('nOrdNo', order.get('orderId', ''))
            status = order.get('ordSt', order.get('status', '')).lower()

            # Check if this is a filled SL order we haven't alerted yet
            if status in ['complete', 'completed', 'traded', 'filled', 'executed']:
                if order_id not in self._alerted_sl_orders:
                    for pos_symbol, pos in self.pos_tracker.positions.items():
                        if pos.sl_order_id == order_id:
                            self._alerted_sl_orders.add(order_id)
                            self._show_sl_breach_alert(pos_symbol, pos.sl_price or 0)
                            break

        # Show only recent orders (last 20)
        recent_orders = orders[-20:] if len(orders) > 20 else orders

        # Block signals during update to avoid triggering cellChanged
        self.orders_table.blockSignals(True)

        # Disable sorting during update for performance and to prevent mid-update re-sorts
        self.orders_table.setSortingEnabled(False)
        self.orders_table.setRowCount(len(recent_orders))

        for row, order in enumerate(recent_orders):
            order_id = order.get('nOrdNo', order.get('orderId', ''))
            # NEO API uses 'trdSym' for trading symbol
            symbol = order.get('trdSym', order.get('tradingSymbol', order.get('symbol', '')))
            trans_type = order.get('tranType', order.get('trnsTp', order.get('transactionType', '')))
            action = 'B' if trans_type == 'B' else 'S'
            qty = order.get('qty', order.get('quantity', ''))
            price = order.get('prc', order.get('price', order.get('avgPrc', '')))
            trigger_price = order.get('trgPrc', order.get('triggerPrice', ''))
            order_type = order.get('ordTyp', order.get('prcTyp', order.get('orderType', '')))
            ltp = order.get('ltp', order.get('lastPrice', order.get('lp', '')))
            status = order.get('ordSt', order.get('status', ''))
            is_pending = status.lower() in ['pending', 'open', 'trigger pending', 'after market order req received']
            # NEO API uses 'ordDtTm' or 'exOrdTm' for order time
            raw_time = order.get('ordDtTm', order.get('exOrdTm', order.get('orderTime', '')))
            time_display = str(raw_time) if raw_time else ''
            time_sort_key = str(raw_time) if raw_time else ''  # Full timestamp for sorting

            # Extract display time (HH:MM) while keeping full timestamp for sorting
            if time_display:
                if ' ' in time_display:
                    time_display = time_display.split(' ')[-1]  # Get time part after date
                if len(time_display) >= 5:
                    time_display = time_display[:5]  # Show HH:MM only

            # Column 0: Symbol - store order data in UserRole for modify/cancel
            symbol_item = QTableWidgetItem(symbol)
            symbol_item.setToolTip(f"Order ID: {order_id}")
            symbol_item.setData(Qt.ItemDataRole.UserRole, {
                'order_id': order_id,
                'order_type': order_type,
                'trigger_price': trigger_price,
                'price': price
            })
            symbol_item.setFlags(symbol_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 0, symbol_item)

            # Column 1: B/S with color
            action_item = QTableWidgetItem(action)
            if action == 'B':
                action_item.setForeground(QBrush(QColor("#00ff88")))
            else:
                action_item.setForeground(QBrush(QColor("#ff5555")))
            action_item.setFlags(action_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 1, action_item)

            # Column 2: Qty
            qty_item = QTableWidgetItem(str(qty))
            qty_item.setFlags(qty_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 2, qty_item)

            # Column 3: LTP
            ltp_item = QTableWidgetItem(str(ltp) if ltp else "--")
            ltp_item.setForeground(QBrush(QColor("#00ccff")))
            ltp_item.setFlags(ltp_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 3, ltp_item)

            # Column 4: Price - EDITABLE for pending orders
            display_price = trigger_price if trigger_price and float(trigger_price) > 0 else price
            price_item = QTableWidgetItem(str(display_price) if display_price else "--")
            if is_pending:
                # Make editable - yellow background to indicate editable
                price_item.setFlags(price_item.flags() | Qt.ItemFlag.ItemIsEditable)
                price_item.setBackground(QBrush(QColor("#333300")))
                price_item.setToolTip("Double-click to edit price")
            else:
                price_item.setFlags(price_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 4, price_item)

            # Column 5: Status with color and rejection reason
            rej_reason = order.get('rejRsn') or order.get('rejectionReason') or \
                         order.get('text') or order.get('remarks') or ''
            status_display = status
            status_tooltip = f"Status: {status}"

            if status.lower() in ['rejected', 'cancelled'] and rej_reason:
                # Show shortened reason in status column
                status_display = f"REJ"
                status_tooltip = f"REJECTED: {rej_reason}"
                # Log rejection reason once (track by order_id)
                if not hasattr(self, '_logged_rejections'):
                    self._logged_rejections = set()
                if order_id and order_id not in self._logged_rejections:
                    self._logged_rejections.add(order_id)
                    self.log_message.emit(f"[ORDER] {symbol} REJECTED: {rej_reason}")
                    # Debug: log full order data for unhelpful rejection reasons
                    if rej_reason.strip() in ['-', '', 'Unknown']:
                        logger.warning(f"Unhelpful rejection reason. Full order: {order}")

                    # Learn from this rejection
                    if self.rejection_learner and status.lower() == 'rejected':
                        order_details = {
                            'product': order.get('prod', order.get('product', '')),
                            'order_type': order_type,
                            'price': price
                        }
                        learned = self.rejection_learner.learn_from_rejection(
                            symbol, rej_reason, order_details
                        )
                        if learned:
                            self.log_message.emit(
                                f"[LEARNER] Learned: {learned.get('index') or symbol} -> "
                                f"Use {learned['product']} ({learned['reason']})"
                            )

            status_item = QTableWidgetItem(status_display)
            status_item.setToolTip(status_tooltip)
            if status.lower() in ['complete', 'traded', 'filled']:
                status_item.setForeground(QBrush(QColor("#00ff88")))
            elif status.lower() in ['rejected', 'cancelled']:
                status_item.setForeground(QBrush(QColor("#ff4444")))
            else:
                status_item.setForeground(QBrush(QColor("#ffaa00")))
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 5, status_item)

            # Column 6: Time - display HH:MM but sort by full timestamp
            time_item = SortableTableWidgetItem(time_display)
            time_item.setData(Qt.ItemDataRole.UserRole, time_sort_key)
            time_item.setFlags(time_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.orders_table.setItem(row, 6, time_item)

            # Column 7: Cancel button (X) for pending orders
            if is_pending:
                cancel_btn = QPushButton("X")
                cancel_btn.setFixedSize(24, 20)
                cancel_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #dd2222; color: white;
                        font-weight: bold; font-size: 11px; border-radius: 3px;
                        border: 1px solid #ff4444;
                        margin: 0px;
                padding-top: 0px;
                padding-bottom: 4px;
                    }
                    QPushButton:hover { background-color: #ff3333; border: 1px solid #ff6666; }
                """)
                cancel_btn.setToolTip("Cancel order")
                cancel_btn.clicked.connect(lambda checked, oid=order_id: self._cancel_single_order(oid))
                self.orders_table.setCellWidget(row, 7, cancel_btn)
            else:
                # No cancel button for completed/cancelled orders
                empty_item = QTableWidgetItem("")
                empty_item.setFlags(empty_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.orders_table.setItem(row, 7, empty_item)

        # Unblock signals
        self.orders_table.blockSignals(False)

        # Re-enable sorting and restore user's sort preference
        self.orders_table.setSortingEnabled(True)
        self.orders_table.sortByColumn(self._orders_sort_column, self._orders_sort_order)

    def _cancel_pending_orders(self):
        """Cancel all pending orders and clear tracker references."""
        # First cancel pending ENTRY orders (not yet filled)
        if self.partial_fill_monitor:
            pending_entries = self.partial_fill_monitor.get_all_pending_entries()
            for entry in pending_entries:
                cancelled = self.partial_fill_monitor.cancel_pending_entry(entry.symbol)
                if cancelled:
                    self.log_message.emit(f"[CANCEL] Pending entry {entry.symbol}: cancelled")

        # Cancel all other pending orders
        results = self.orders.cancel_pending_orders()
        cancelled_ids = set()
        for r in results:
            self.log_message.emit(f"[CANCEL] {r.get('order_id', '')}: {r.get('status', '')}")
            if r.get('status') == 'cancelled':
                cancelled_ids.add(r.get('order_id'))

        # Clear tracker references for cancelled orders
        for symbol, pos in list(self.pos_tracker.positions.items()):
            if pos.sl_order_id in cancelled_ids:
                pos.sl_order_id = None
                pos.sl_price = None
            if pos.target_order_id in cancelled_ids:
                pos.target_order_id = None
                pos.target_price = None

        # Clear trail manager (SL orders cancelled)
        if self.trail_mgr:
            self.trail_mgr.positions.clear()

        # Clear OCO monitor
        if self.oco_monitor:
            self.oco_monitor.clear_all()

        self._force_next_orders_refresh()
        QTimer.singleShot(500, self._refresh_orders)

    def _cancel_all_orders(self):
        """Cancel all pending orders with confirmation."""
        reply = QMessageBox.question(
            self, "Confirm Cancel All",
            "Are you sure you want to CANCEL ALL pending orders?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            self._cancel_pending_orders()

    # ==================== Updates ====================

    def _update_time(self):
        """Update time display and check market hours."""
        now = datetime.now()
        self.time_label.setText(now.strftime('%H:%M:%S'))

        # Check for new trading day - auto reset limits
        self.orders.check_and_reset_if_new_day()

        # Check if trading halted
        if self.orders.is_trading_halted():
            self.halt_label.setText("TRADING HALTED")
            self.halt_label.setStyleSheet("color: #ff4444; font-weight: bold;")
        else:
            # Check market hours and EOD warning
            market_warning = self._check_market_hours(now)
            if market_warning:
                self.halt_label.setText(market_warning)
                self.halt_label.setStyleSheet("color: #ffaa00; font-weight: bold;")
            else:
                self.halt_label.setText("")

    def _check_market_hours(self, now: datetime) -> str:
        """
        Check market hours and return warning message if applicable.
        Market hours: 9:15 AM - 3:30 PM
        """
        current_time = now.time()
        market_open = datetime.strptime("09:15", "%H:%M").time()
        market_close = datetime.strptime("15:30", "%H:%M").time()
        warning_time = datetime.strptime("15:15", "%H:%M").time()  # 15 min warning
        freeze_time = datetime.strptime("15:20", "%H:%M").time()   # 10 min before close

        # Pre-market
        if current_time < market_open:
            return "PRE-MARKET"

        # Post-market
        if current_time >= market_close:
            return "MARKET CLOSED"

        # Final 10 minutes - FREEZE new orders
        if current_time >= freeze_time:
            if not hasattr(self, '_eod_freeze_shown'):
                self._eod_freeze_shown = True
                self.log_message.emit("[WARNING] Market closing in 10 min - New orders FROZEN")
                # Consider auto-exit here if configured
                if self.config.get('risk_management', {}).get('auto_exit_before_close', False):
                    self._trigger_eod_exit()
            return f"EOD FREEZE - {self._time_to_close(now)}"

        # Warning zone (15-10 min before close)
        if current_time >= warning_time:
            if not hasattr(self, '_eod_warning_shown'):
                self._eod_warning_shown = True
                self.log_message.emit("[WARNING] Market closing soon - Square off MIS positions!")
                if self.sound:
                    self.sound.play('alert')
            return f"CLOSE IN {self._time_to_close(now)}"

        # Reset flags for next day
        if hasattr(self, '_eod_warning_shown'):
            del self._eod_warning_shown
        if hasattr(self, '_eod_freeze_shown'):
            del self._eod_freeze_shown

        return ""

    def _time_to_close(self, now: datetime) -> str:
        """Calculate time remaining to market close."""
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
        delta = market_close - now
        minutes = int(delta.total_seconds() // 60)
        return f"{minutes}m"

    def _trigger_eod_exit(self):
        """Trigger end-of-day position exit (if configured)."""
        self.log_message.emit("[EOD] Auto-exit triggered - exiting all MIS positions")
        # Only exit MIS positions (not NRML)
        for pos in self._positions_cache:
            product = _get_position_product(pos)
            qty = _get_position_qty(pos)
            if product == 'MIS' and qty != 0:
                self._exit_position(pos, 100)

    def _refresh_margin(self):
        """Refresh margin display."""
        if self.session and self.session.is_connected():
            limits = self.session.get_limits()
            if limits:
                # NEO API returns flat dict with Net (available), MarginUsed, RmsPayInAmt
                available = float(limits.get('Net', 0) or 0)
                margin_used = float(limits.get('MarginUsed', 0) or 0)
                self.margin_updated.emit(available)

    def _refresh_quote(self):
        """Refresh LTP/Bid/Ask for current symbol every 2 seconds."""
        if not self.current_mapping:
            return

        symbol = self.symbol_input.currentText().strip().upper()
        if not symbol:
            return

        if self.kite and self.kite.is_connected():
            try:
                quote = self.kite.get_quote(symbol)
                if quote:
                    ltp = quote.get('last_price')
                    depth = quote.get('depth', {})
                    buy_depth = depth.get('buy', [])
                    sell_depth = depth.get('sell', [])

                    if ltp:
                        self.ltp_label.setText(f"₹{ltp:.2f}")
                        self.ltp_label.setStyleSheet("color: #00ff88; font-weight: bold;")

                    if buy_depth and buy_depth[0].get('price'):
                        bid = buy_depth[0].get('price')
                        self.bid_label.setText(f"₹{bid:.2f}")

                    if sell_depth and sell_depth[0].get('price'):
                        ask = sell_depth[0].get('price')
                        self.ask_label.setText(f"₹{ask:.2f}")
            except Exception:
                pass  # Silent fail for quote refresh

    def _update_margin_display(self, margin: float):
        """Update margin label."""
        self.margin_label.setText(f"₹{margin:,.0f}")

    def _update_pnl_display(self, pnl: float):
        """Update P&L label."""
        self.pnl_label.setText(f"₹{pnl:,.0f}")
        self.pnl_label.setStyleSheet(f"color: {'#00ff88' if pnl >= 0 else '#ff4444'}; font-weight: bold;")

    def _append_log(self, message: str):
        """Append message to log."""
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.append(f"{timestamp} {message}")

        # Auto-scroll to bottom
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def refresh_all(self):
        """Refresh all data."""
        self._refresh_positions()
        self._refresh_orders()
        self._refresh_margin()

    def _recover_existing_positions(self):
        """
        Recover existing positions from broker on startup.
        Syncs positions with tracker and checks for existing SL/Target orders.
        CRITICAL: Prevents exposure without SL protection after app restart.
        """
        self.log_message.emit("[RECOVERY] Checking for existing positions...")

        try:
            # Get current positions from broker
            positions = self.orders.get_positions()
            active_positions = [p for p in positions if _get_position_qty(p) != 0]

            if not active_positions:
                self.log_message.emit("[RECOVERY] No existing positions found")
                return

            self.log_message.emit(f"[RECOVERY] Found {len(active_positions)} existing positions")

            # Get all pending orders to find existing SL/Target
            pending_orders = self._get_pending_orders_by_symbol()

            for pos in active_positions:
                symbol = _get_position_symbol(pos)
                qty = _get_position_qty(pos)
                avg_price = _get_position_avg_price(pos)
                exchange_segment = _get_position_exchange(pos)
                is_long = qty > 0

                self.log_message.emit(f"[RECOVERY] Position: {symbol} {'LONG' if is_long else 'SHORT'} {abs(qty)} @ {avg_price:.2f}")

                # Add to position tracker
                self.pos_tracker.add_position(
                    symbol=symbol,
                    exchange_segment=exchange_segment,
                    quantity=abs(qty),
                    side='LONG' if is_long else 'SHORT',
                    entry_price=avg_price
                )

                # Check for existing SL/Target orders
                symbol_orders = pending_orders.get(symbol, [])
                sl_order = None
                target_order = None

                for order in symbol_orders:
                    order_type = order.get('ordTyp', order.get('orderType', '')).upper()
                    tag = order.get('tag', '').upper()

                    if 'SL' in order_type or tag == 'SL':
                        sl_order = order
                    elif tag == 'TARGET' or (order_type == 'L' and tag != 'ENTRY'):
                        target_order = order

                # Register existing SL order
                if sl_order:
                    sl_order_id = sl_order.get('nOrdNo', sl_order.get('orderId', ''))
                    sl_price = float(sl_order.get('trgPrc', sl_order.get('triggerPrice', 0)) or 0)
                    self.pos_tracker.set_sl_order(symbol, sl_order_id, sl_price)
                    self.log_message.emit(f"[RECOVERY] Found existing SL: {symbol} @ {sl_price}")

                    # Register with trail manager
                    if self.trail_mgr:
                        inst_token = str(pos.get('instrument_token', pos.get('token', '')))
                        self.trail_mgr.add_position(
                            symbol=symbol,
                            exchange_segment=exchange_segment,
                            entry_price=avg_price,
                            quantity=abs(qty),
                            side='LONG' if is_long else 'SHORT',
                            sl_price=sl_price,
                            sl_order_id=sl_order_id,
                            instrument_token=inst_token
                        )
                else:
                    # WARNING: Position has no SL protection!
                    self.log_message.emit(f"[WARNING] {symbol} has NO SL ORDER - SET SL IMMEDIATELY!")
                    if self.sound:
                        self.sound.play('alert')

                # Register existing Target order
                if target_order:
                    target_order_id = target_order.get('nOrdNo', target_order.get('orderId', ''))
                    target_price = float(target_order.get('prc', target_order.get('price', 0)) or 0)
                    self.pos_tracker.set_target_order(symbol, target_order_id, target_price)
                    self.log_message.emit(f"[RECOVERY] Found existing Target: {symbol} @ {target_price}")

                # Register OCO pair if both exist
                if sl_order and target_order and self.oco_monitor:
                    sl_order_id = sl_order.get('nOrdNo', sl_order.get('orderId', ''))
                    target_order_id = target_order.get('nOrdNo', target_order.get('orderId', ''))
                    sl_price = float(sl_order.get('trgPrc', sl_order.get('triggerPrice', 0)) or 0)
                    target_price = float(target_order.get('prc', target_order.get('price', 0)) or 0)

                    self.oco_monitor.add_oco_pair(
                        position_symbol=symbol,
                        sl_order_id=sl_order_id,
                        target_order_id=target_order_id,
                        sl_trigger=sl_price,
                        target_price=target_price,
                        quantity=abs(qty),
                        side='LONG' if is_long else 'SHORT',
                        entry_price=avg_price
                    )
                    self.log_message.emit(f"[RECOVERY] Registered OCO pair for {symbol}")

            self.log_message.emit("[RECOVERY] Position recovery complete")

        except Exception as e:
            self.log_message.emit(f"[ERROR] Position recovery failed: {e}")

    def _get_pending_orders_by_symbol(self) -> Dict[str, List[Dict[str, Any]]]:
        """Get pending orders grouped by symbol."""
        orders_by_symbol: Dict[str, List[Dict[str, Any]]] = {}

        try:
            orders = self.orders.get_orders()
            for order in orders:
                status = order.get('ordSt', '').lower()
                if status in ['pending', 'open', 'trigger pending', 'after market order req received']:
                    symbol = order.get('tradingSymbol', order.get('symbol', ''))
                    if symbol not in orders_by_symbol:
                        orders_by_symbol[symbol] = []
                    orders_by_symbol[symbol].append(order)
        except Exception:
            pass

        return orders_by_symbol

    # ==================== Theme ====================

    def _get_dark_theme(self) -> str:
        """Get dark theme stylesheet."""
        return """
            QMainWindow, QWidget {
                background-color: #1a1a2e;
                color: #e0e0e0;
                font-family: "Segoe UI", sans-serif;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #333355;
                border-radius: 5px;
                margin-top: 10px;
                padding-top: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                padding: 0 5px;
                color: #8888aa;
            }
            QLineEdit, QComboBox, QSpinBox {
                background-color: #252540;
                border: 1px solid #444466;
                border-radius: 3px;
                padding: 5px;
                color: #e0e0e0;
            }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus {
                border: 1px solid #6666aa;
            }
            QPushButton {
                background-color: #333355;
                border: 1px solid #444466;
                border-radius: 4px;
                padding: 5px 10px;
                color: #e0e0e0;
            }
            QPushButton:hover {
                background-color: #444477;
            }
            QPushButton:pressed {
                background-color: #222244;
            }
            QTableWidget {
                background-color: #1a1a2e;
                alternate-background-color: #222240;
                border: 1px solid #333355;
                gridline-color: #333355;
            }
            QTableWidget::item {
                padding: 5px;
            }
            QTableWidget::item:selected {
                background-color: #444477;
            }
            QHeaderView::section {
                background-color: #252540;
                border: 1px solid #333355;
                padding: 5px;
                font-weight: bold;
            }
            QTextEdit {
                background-color: #0d0d1a;
                border: 1px solid #333355;
                color: #aaaacc;
            }
            QRadioButton {
                spacing: 5px;
            }
            QRadioButton::indicator {
                width: 13px;
                height: 13px;
            }
            QCheckBox {
                spacing: 5px;
            }
            QCheckBox::indicator {
                width: 13px;
                height: 13px;
            }
            QScrollBar:vertical {
                background: #1a1a2e;
                width: 10px;
            }
            QScrollBar::handle:vertical {
                background: #444466;
                border-radius: 5px;
            }
            QFrame[frameShape="5"] {
                color: #444466;
            }
        """

    def closeEvent(self, event):
        """Handle window close."""
        # Stop timers
        self.position_timer.stop()
        self.time_timer.stop()
        self.margin_timer.stop()
        self.orders_timer.stop()

        # Stop trail manager
        if self.trail_mgr:
            self.trail_mgr.stop_auto_trail()

        # Stop OCO monitor
        if self.oco_monitor:
            self.oco_monitor.stop()

        event.accept()

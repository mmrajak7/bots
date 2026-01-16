"""
NEO Trade Terminal - Main GUI Window

PyQt6-based trading interface with:
- Quick entry panel with symbol mapping
- Positions table with trailing SL controls
- Order book display
- Basket order builder
- Keyboard shortcuts for fast execution
"""

import re
import sys
from datetime import datetime
from typing import Dict, List, Optional, Any

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
                 ws_handler=None):
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

        # State
        self.basket_legs: List[Dict[str, Any]] = []
        self.current_mapping: Optional[Dict[str, Any]] = None
        self.sl_inputs: Dict[str, QLineEdit] = {}
        self._positions_cache: List[Dict[str, Any]] = []
        self._filtered_positions_cache: List[Dict[str, Any]] = []

        # Position tracker for SL/Target order management
        self.pos_tracker = PositionTracker(
            session_mgr.get_client() if session_mgr else None,
            order_mgr
        )

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

        # Main content: 3 columns - Positions | Basket+OrderBook | Logs
        main_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Column 1: Positions
        main_splitter.addWidget(self._create_positions_panel())

        # Column 2: Basket + Order Book (stacked vertically)
        middle_widget = QWidget()
        middle_layout = QVBoxLayout(middle_widget)
        middle_layout.setSpacing(4)
        middle_layout.setContentsMargins(0, 0, 0, 0)
        middle_layout.addWidget(self._create_basket_panel())
        middle_layout.addWidget(self._create_order_book_panel(), 1)
        main_splitter.addWidget(middle_widget)

        # Column 3: Logs
        main_splitter.addWidget(self._create_log_panel())

        # Set column sizes: Positions 45% | Basket+Orders 35% | Logs 20%
        main_splitter.setSizes([650, 500, 300])

        layout.addWidget(main_splitter, 1)

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

        # Positions table
        self.positions_table = QTableWidget()
        self.positions_table.setColumnCount(9)
        self.positions_table.setHorizontalHeaderLabels([
            "Symbol", "Qty", "Avg", "LTP", "P&L", "%", "SL", "Trail", "Exit"
        ])
        # Set optimal fixed column widths
        header = self.positions_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # Symbol stretches
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)  # Qty
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)  # Avg
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # LTP
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)  # P&L
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)  # %
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)  # SL
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Fixed)  # Trail
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Fixed)  # Exit
        self.positions_table.setColumnWidth(1, 45)   # Qty
        self.positions_table.setColumnWidth(2, 55)   # Avg
        self.positions_table.setColumnWidth(3, 55)   # LTP
        self.positions_table.setColumnWidth(4, 65)   # P&L
        self.positions_table.setColumnWidth(5, 40)   # %
        self.positions_table.setColumnWidth(6, 55)   # SL
        self.positions_table.setColumnWidth(7, 45)   # Trail
        self.positions_table.setColumnWidth(8, 40)   # Exit
        self.positions_table.setAlternatingRowColors(True)
        self.positions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
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
        """Create order book display with proper columns."""
        group = QGroupBox("ORDER BOOK")
        layout = QVBoxLayout(group)
        layout.setSpacing(2)
        layout.setContentsMargins(5, 5, 5, 5)

        self.orders_table = QTableWidget()
        self.orders_table.setColumnCount(7)
        self.orders_table.setHorizontalHeaderLabels(["ID", "Symbol", "B/S", "Qty", "Price", "Status", "Time"])
        self.orders_table.verticalHeader().setVisible(False)  # Hide row numbers
        # Optimize column widths for proper display
        oheader = self.orders_table.horizontalHeader()
        oheader.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)  # ID
        oheader.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)  # Symbol - stretch to fill
        oheader.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)  # B/S
        oheader.setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)  # Qty
        oheader.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)  # Price
        oheader.setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)  # Status
        oheader.setSectionResizeMode(6, QHeaderView.ResizeMode.Fixed)  # Time
        self.orders_table.setColumnWidth(0, 65)   # ID
        self.orders_table.setColumnWidth(2, 35)   # B/S
        self.orders_table.setColumnWidth(3, 45)   # Qty
        self.orders_table.setColumnWidth(4, 55)   # Price
        self.orders_table.setColumnWidth(5, 65)   # Status
        self.orders_table.setColumnWidth(6, 50)   # Time
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

        layout.addWidget(self.orders_table, 1)

        return group

    def _on_orders_header_clicked(self, logical_index: int):
        """Track user's sort preference when they click a header."""
        self._orders_sort_column = logical_index
        self._orders_sort_order = self.orders_table.horizontalHeader().sortIndicatorOrder()

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
            if status in ['complete', 'traded', 'filled']:
                self.log_message.emit(f"[WS] ✓ Order {order_id[-6:]} FILLED: {symbol}")
                if self.sound:
                    self.sound.play('order_filled')
                # Immediately refresh positions and orders
                QTimer.singleShot(100, self._refresh_positions)
                QTimer.singleShot(200, self._refresh_orders)

            elif status in ['rejected']:
                reason = data.get('rejRsn', data.get('rejectionReason', 'Unknown'))
                self.log_message.emit(f"[WS] ✗ Order {order_id[-6:]} REJECTED: {reason}")
                if self.sound:
                    self.sound.play('order_rejected')
                QTimer.singleShot(200, self._refresh_orders)

            elif status in ['cancelled']:
                self.log_message.emit(f"[WS] Order {order_id[-6:]} CANCELLED: {symbol}")
                QTimer.singleShot(200, self._refresh_orders)

            elif status in ['open', 'pending', 'trigger pending']:
                self.log_message.emit(f"[WS] Order {order_id[-6:]} PENDING: {symbol}")
                # Also refresh orders table for pending status changes
                QTimer.singleShot(200, self._refresh_orders)

            else:
                # Any other status change - refresh to keep table current
                self.log_message.emit(f"[WS] Order {order_id[-6:]} {status_display}: {symbol}")
                QTimer.singleShot(200, self._refresh_orders)

        except Exception as e:
            self.log_message.emit(f"[WS] Error processing update: {e}")

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

                # Check if SL/Target provided - schedule placement after fill
                sl_text = self.entry_sl_input.text().strip()
                target_text = self.target_input.text().strip()

                # Clear SL/Target fields after order placed
                self.entry_sl_input.clear()
                self.target_input.clear()
                self.sl_pct_label.clear()
                self.tgt_pct_label.clear()
                if sl_text or target_text:
                    self.log_message.emit("[INFO] Scheduling SL/Target after entry fill...")
                    # Schedule a check for order fill and SL/Target placement
                    QTimer.singleShot(1500, lambda: self._place_sl_target_after_fill(
                        result.order_id,
                        params.symbol,
                        params.exchange_segment,
                        params.quantity,
                        action,
                        sl_text,
                        target_text,
                        params.product,
                        params.instrument_token
                    ))

            else:
                self.log_message.emit(f"[ERROR] {result.message}")
                if self.sound:
                    self.sound.play('order_rejected')

            QTimer.singleShot(500, self._refresh_positions)

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
        max_retries = 5  # Check up to 5 times (7.5 seconds total)

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

            if status in ['complete', 'traded', 'filled'] or (is_partial and filled_qty > 0):
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
                    QTimer.singleShot(1500, lambda: self._place_sl_target_after_fill(
                        entry_order_id, symbol, exchange_segment, quantity,
                        entry_action, sl_text, target_text, product, instrument_token, retry_count + 1
                    ))
                else:
                    self.log_message.emit(f"[SL/TGT] Entry order still pending after {max_retries} checks - set SL manually")

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
            self._positions_cache = positions
            self.position_updated.emit(positions)

            # Sync position tracker with broker - cleanup closed positions
            # Find symbols that are no longer in broker positions
            broker_symbols = set()
            for pos in positions:
                symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))
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
                pnl = float(pos.get('pnl', 0) or pos.get('dayPnl', 0) or 0)
                total_pnl += pnl

            self.pnl_updated.emit(total_pnl)

        except Exception as e:
            self.log_message.emit(f"[ERROR] Position refresh failed: {str(e)}")

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
            filtered_positions = [p for p in positions if int(p.get('qty', 0)) != 0]
        elif filter_type == "Closed":
            filtered_positions = [p for p in positions if int(p.get('qty', 0)) == 0]
        else:  # All
            filtered_positions = positions

        self.positions_table.setRowCount(len(filtered_positions))
        self._filtered_positions_cache = filtered_positions  # Store for cell widget access
        total_pnl = 0

        for row, pos in enumerate(filtered_positions):
            symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
            qty = int(pos.get('qty', 0))
            avg = float(pos.get('averagePrice', pos.get('avgPrc', 0)) or 0)
            ltp = float(pos.get('ltp', pos.get('lastPrice', 0)) or 0)
            pnl = float(pos.get('pnl', pos.get('dayPnl', 0)) or 0)
            pnl_pct = ((ltp - avg) / avg * 100) if avg > 0 else 0

            # Symbol
            self.positions_table.setItem(row, 0, QTableWidgetItem(symbol))

            # Qty (color coded)
            qty_item = QTableWidgetItem(str(qty))
            qty_item.setForeground(QBrush(QColor("#00ff88" if qty > 0 else "#ff4444")))
            self.positions_table.setItem(row, 1, qty_item)

            # Avg
            self.positions_table.setItem(row, 2, QTableWidgetItem(f"{avg:.2f}"))

            # LTP
            self.positions_table.setItem(row, 3, QTableWidgetItem(f"{ltp:.2f}"))

            # P&L (color coded)
            pnl_item = QTableWidgetItem(f"₹{pnl:,.0f}")
            pnl_item.setForeground(QBrush(QColor("#00ff88" if pnl >= 0 else "#ff4444")))
            self.positions_table.setItem(row, 4, pnl_item)

            # P&L %
            pct_item = QTableWidgetItem(f"{pnl_pct:+.1f}%")
            pct_item.setForeground(QBrush(QColor("#00ff88" if pnl_pct >= 0 else "#ff4444")))
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
        """Create SL input widget for position row with +5/-5 adjustment buttons."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        # -5 button (decrease SL)
        minus_btn = QPushButton("-5")
        minus_btn.setFixedWidth(24)
        minus_btn.setStyleSheet("""
            QPushButton {
                background-color: #553333; color: #ff8888;
                font-weight: bold; font-size: 9px; border-radius: 2px;
            }
            QPushButton:hover { background-color: #664444; }
        """)
        minus_btn.setToolTip("Decrease SL by 5 points")
        layout.addWidget(minus_btn)

        sl_input = QLineEdit()
        sl_input.setPlaceholderText("SL")
        sl_input.setFixedWidth(55)

        # Check if we have a tracked SL for this position
        tracked = self.pos_tracker.get_position(symbol)
        if tracked and tracked.sl_price:
            sl_input.setText(f"{tracked.sl_price:.2f}")
            sl_input.setStyleSheet("""
                QLineEdit {
                    background-color: #1a3a2e;
                    border: 1px solid #00aa66;
                    border-radius: 3px;
                    color: #00ff88;
                    font-weight: bold;
                }
            """)
        else:
            sl_input.setStyleSheet("""
                QLineEdit {
                    background-color: #1a1a2e;
                    border: 1px solid #444466;
                    border-radius: 3px;
                    color: #ffaa00;
                    font-weight: bold;
                }
            """)

        sl_input.returnPressed.connect(lambda s=symbol, inp=sl_input: self._update_sl_from_input(s, inp.text()))
        layout.addWidget(sl_input)

        self.sl_inputs[symbol] = sl_input

        # +5 button (increase SL)
        plus_btn = QPushButton("+5")
        plus_btn.setFixedWidth(24)
        plus_btn.setStyleSheet("""
            QPushButton {
                background-color: #335533; color: #88ff88;
                font-weight: bold; font-size: 9px; border-radius: 2px;
            }
            QPushButton:hover { background-color: #446644; }
        """)
        plus_btn.setToolTip("Increase SL by 5 points")
        layout.addWidget(plus_btn)

        # Connect +/- buttons to adjust SL value
        minus_btn.clicked.connect(lambda _, inp=sl_input: self._adjust_sl_value(inp, -5))
        plus_btn.clicked.connect(lambda _, inp=sl_input: self._adjust_sl_value(inp, 5))

        set_btn = QPushButton("SET")
        set_btn.setFixedWidth(32)
        set_btn.setStyleSheet("""
            QPushButton {
                background-color: #663300; color: white;
                font-weight: bold; font-size: 9px; border-radius: 2px;
            }
            QPushButton:hover { background-color: #884400; }
        """)
        set_btn.clicked.connect(lambda _, s=symbol, inp=sl_input: self._update_sl_from_input(s, inp.text()))
        layout.addWidget(set_btn)

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
        """Create trail buttons widget."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        be_btn = QPushButton("BE")
        be_btn.setToolTip("Trail to Breakeven")
        be_btn.setMaximumWidth(30)
        be_btn.setStyleSheet("background-color: #004466;")
        be_btn.clicked.connect(lambda _, s=symbol: self._trail_to_cost(s))
        layout.addWidget(be_btn)

        t10_btn = QPushButton("+10")
        t10_btn.setMaximumWidth(30)
        t10_btn.setStyleSheet("background-color: #005544;")
        t10_btn.clicked.connect(lambda _, s=symbol: self._trail_by_points(s, 10))
        layout.addWidget(t10_btn)

        t25_btn = QPushButton("+25")
        t25_btn.setMaximumWidth(30)
        t25_btn.setStyleSheet("background-color: #006633;")
        t25_btn.clicked.connect(lambda _, s=symbol: self._trail_by_points(s, 25))
        layout.addWidget(t25_btn)

        return widget

    def _create_exit_widget(self, pos: Dict[str, Any]) -> QWidget:
        """Create exit buttons widget."""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        exit_50 = QPushButton("50%")
        exit_50.setMaximumWidth(35)
        exit_50.clicked.connect(lambda _, p=pos: self._exit_position(p, 50))
        layout.addWidget(exit_50)

        exit_full = QPushButton("EXIT")
        exit_full.setStyleSheet("background-color: #880000; color: white;")
        exit_full.setMaximumWidth(40)
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
                if p.get('tradingSymbol', p.get('symbol', '')) == symbol:
                    pos = p
                    break

            if not pos:
                self.log_message.emit(f"[SL] Position not found: {symbol}")
                return

            qty = abs(int(pos.get('qty', 0)))
            if qty == 0:
                self.log_message.emit(f"[SL] No quantity to protect")
                return

            # Determine exit direction
            is_long = int(pos.get('qty', 0)) > 0
            exit_type = 'S' if is_long else 'B'

            # Get prices for validation
            entry_price = float(pos.get('averagePrice', pos.get('avgPrc', 0)) or 0)
            ltp = float(pos.get('ltp', pos.get('lastPrice', entry_price)) or entry_price)

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
                    self.session.get_client().modify_order(
                        order_id=tracked.sl_order_id,
                        trigger_price=str(new_sl)
                    )
                    self.pos_tracker.update_sl_order(symbol, new_sl_price=new_sl)
                    self.log_message.emit(f"[SL] {symbol}: Modified SL -> {new_sl:.2f}")
                    if self.sound:
                        self.sound.play('order_placed')

                    # Update trail manager if registered
                    if self.trail_mgr and symbol in self.trail_mgr.positions:
                        self.trail_mgr.positions[symbol].current_sl = new_sl

                except Exception as e:
                    self.log_message.emit(f"[SL] Modify failed: {e}")
            else:
                # Place new SL order
                try:
                    sl_response = self.session.get_client().place_order(
                        exchange_segment=pos.get('exchange_segment', pos.get('exchangeSegment', 'nse_fo')),
                        product=pos.get('product', 'MIS'),
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
                            avg_price = float(pos.get('averagePrice', pos.get('avgPrc', 0)) or 0)
                            self.pos_tracker.add_position(
                                symbol=symbol,
                                exchange_segment=pos.get('exchange_segment', pos.get('exchangeSegment', 'nse_fo')),
                                quantity=qty,
                                side='LONG' if is_long else 'SHORT',
                                entry_price=avg_price
                            )

                        # Record SL order
                        self.pos_tracker.set_sl_order(symbol, sl_order_id, new_sl)

                        # Register with trail manager
                        if self.trail_mgr:
                            avg_price = float(pos.get('averagePrice', pos.get('avgPrc', 0)) or 0)
                            inst_token = str(pos.get('instrument_token', pos.get('token', '')))
                            self.trail_mgr.add_position(
                                symbol=symbol,
                                exchange_segment=pos.get('exchange_segment', pos.get('exchangeSegment', 'nse_fo')),
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
                    else:
                        self.log_message.emit(f"[SL] {symbol}: Order placed but no ID returned")

                except Exception as e:
                    self.log_message.emit(f"[SL] Place failed: {e}")

        except ValueError:
            self.log_message.emit(f"[SL] Invalid price format: {price_str}")

    def _trail_to_cost(self, symbol: str):
        """Trail SL to breakeven."""
        if self.trail_mgr:
            result = self.trail_mgr.trail_to_cost(symbol)
            if result['success']:
                new_sl = result['new_sl']
                self.log_message.emit(f"[TRAIL] {symbol}: SL moved to COST ({new_sl:.2f})")
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
            symbol = self._filtered_positions_cache[row].get('tradingSymbol', self._filtered_positions_cache[row].get('symbol', ''))
            self._trail_to_cost(symbol)

    def _trail_selected_plus(self, points: float):
        """Trail selected position by points."""
        row = self.positions_table.currentRow()
        if row >= 0 and row < len(self._positions_cache):
            symbol = self._filtered_positions_cache[row].get('tradingSymbol', self._filtered_positions_cache[row].get('symbol', ''))
            self._trail_by_points(symbol, points)

    def _exit_position(self, pos: Dict[str, Any], percent: int):
        """Exit position by percentage - cancel SL/Target first to prevent ghost orders."""
        symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
        qty = abs(int(pos.get('qty', 0)))

        # Calculate exit quantity
        exit_qty = int(qty * percent / 100)
        remaining_qty = qty - exit_qty

        # For FULL exit (100%), cancel ALL pending orders first
        if percent == 100 or remaining_qty == 0:
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

        # Place exit order
        result = self.orders.exit_position(pos, percent)
        if result.success:
            self.log_message.emit(f"[EXIT] {symbol} {percent}% ({exit_qty} qty) -> ID: {result.order_id}")
            if self.sound:
                self.sound.play('order_placed')

            # For partial exit, modify SL order quantity using order_manager with retry logic
            if percent < 100 and remaining_qty > 0:
                tracked = self.pos_tracker.get_position(symbol)
                if tracked and tracked.sl_order_id:
                    # Update tracker quantity
                    self.pos_tracker.reduce_position_qty(symbol, exit_qty)

                    # Determine exit type for SL
                    is_long = int(pos.get('qty', 0)) > 0
                    exit_type = 'S' if is_long else 'B'

                    # Use order_manager's method with retry logic
                    adjust_result = self.orders.adjust_sl_after_partial_exit(
                        symbol=symbol,
                        remaining_qty=remaining_qty,
                        sl_order_id=tracked.sl_order_id,
                        sl_price=tracked.sl_price,
                        exchange_segment=pos.get('exchange_segment', pos.get('exchangeSegment', 'nse_fo')),
                        product=pos.get('product', 'MIS'),
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
            is_long = int(pos.get('qty', 0)) > 0
            exit_type = 'S' if is_long else 'B'

            sl_response = self.session.get_client().place_order(
                exchange_segment=pos.get('exchange_segment', pos.get('exchangeSegment', 'nse_fo')),
                product=pos.get('product', 'MIS'),
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

    def _exit_all_positions(self):
        """Exit all positions - cancel ALL pending SL/Target orders first."""
        reply = QMessageBox.question(
            self, "Confirm Exit All",
            "Are you sure you want to EXIT ALL positions?\n(This will cancel all SL/Target orders)",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply == QMessageBox.StandardButton.Yes:
            # CRITICAL: Cancel all SL/Target orders BEFORE exiting positions
            self.log_message.emit("[EXIT ALL] Cancelling all pending SL/Target orders...")

            # Cancel all tracked orders
            cancelled_count = 0
            for symbol in list(self.pos_tracker.positions.keys()):
                cancelled = self.pos_tracker.cancel_all_orders_for_position(symbol)
                if cancelled.get('sl_cancelled') or cancelled.get('target_cancelled'):
                    cancelled_count += 1
                    self.log_message.emit(f"[EXIT ALL] Cancelled orders for {symbol}")

            self.log_message.emit(f"[EXIT ALL] Cancelled SL/Target for {cancelled_count} positions")

            # Clear trail manager
            if self.trail_mgr:
                self.trail_mgr.positions.clear()
                self.log_message.emit("[EXIT ALL] Cleared trail manager")

            # Clear OCO monitor
            if self.oco_monitor:
                self.oco_monitor.clear_all()
                self.log_message.emit("[EXIT ALL] Cleared OCO monitor")

            # Clear position tracker
            self.pos_tracker.positions.clear()

            # Now exit all positions
            results = self.orders.exit_all_positions()
            for r in results:
                self.log_message.emit(f"[EXIT ALL] {r.get('symbol', '')}: {r.get('status', 'unknown')}")

            if self.sound:
                self.sound.play('position_exit')

            QTimer.singleShot(500, self._refresh_positions)

    # ==================== Order Management ====================

    def _refresh_orders(self):
        """Refresh orders from broker."""
        try:
            orders = self.orders.get_orders()
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
        """Update orders table with 7 columns: ID, Symbol, B/S, Qty, Price, Status, Time.

        Preserves user's sort selection across refreshes.
        """
        # Show only recent orders (last 20)
        recent_orders = orders[-20:] if len(orders) > 20 else orders

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
            status = order.get('ordSt', order.get('status', ''))
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

            # Shorten order ID for display, store full ID in tooltip and UserRole
            short_id = self._shorten_order_id(order_id)
            id_item = QTableWidgetItem(short_id)
            id_item.setToolTip(f"Full ID: {order_id}")  # Tooltip shows full ID on hover
            id_item.setData(Qt.ItemDataRole.UserRole, order_id)  # Store full ID for reference
            self.orders_table.setItem(row, 0, id_item)
            self.orders_table.setItem(row, 1, QTableWidgetItem(symbol))

            # B/S with color
            action_item = QTableWidgetItem(action)
            if action == 'B':
                action_item.setForeground(QBrush(QColor("#00ff88")))
            else:
                action_item.setForeground(QBrush(QColor("#ff5555")))
            self.orders_table.setItem(row, 2, action_item)

            self.orders_table.setItem(row, 3, QTableWidgetItem(str(qty)))
            self.orders_table.setItem(row, 4, QTableWidgetItem(str(price) if price else "--"))

            # Status with color
            status_item = QTableWidgetItem(status)
            if status.lower() in ['complete', 'traded', 'filled']:
                status_item.setForeground(QBrush(QColor("#00ff88")))
            elif status.lower() in ['rejected', 'cancelled']:
                status_item.setForeground(QBrush(QColor("#ff4444")))
            else:
                status_item.setForeground(QBrush(QColor("#ffaa00")))
            self.orders_table.setItem(row, 5, status_item)

            # Time column: display HH:MM but sort by full timestamp
            time_item = SortableTableWidgetItem(time_display)
            time_item.setData(Qt.ItemDataRole.UserRole, time_sort_key)
            self.orders_table.setItem(row, 6, time_item)

        # Re-enable sorting and restore user's sort preference
        self.orders_table.setSortingEnabled(True)
        self.orders_table.sortByColumn(self._orders_sort_column, self._orders_sort_order)

    def _cancel_pending_orders(self):
        """Cancel all pending orders and clear tracker references."""
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
            product = pos.get('product', '').upper()
            qty = int(pos.get('qty', 0))
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
            active_positions = [p for p in positions if int(p.get('qty', 0)) != 0]

            if not active_positions:
                self.log_message.emit("[RECOVERY] No existing positions found")
                return

            self.log_message.emit(f"[RECOVERY] Found {len(active_positions)} existing positions")

            # Get all pending orders to find existing SL/Target
            pending_orders = self._get_pending_orders_by_symbol()

            for pos in active_positions:
                symbol = pos.get('tradingSymbol', pos.get('symbol', ''))
                qty = int(pos.get('qty', 0))
                avg_price = float(pos.get('averagePrice', pos.get('avgPrc', 0)) or 0)
                exchange_segment = pos.get('exchange_segment', pos.get('exchangeSegment', 'nse_fo'))
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

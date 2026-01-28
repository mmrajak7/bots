"""
NEO Trade Terminal - Symbol Mapper

Maps Kite/Zerodha symbol format to NEO instrument tokens.
Downloads both Kite and NEO instruments at login, builds mapping cache.
"""

import pandas as pd
import os
import json
import threading
from datetime import datetime, date, timedelta
import re
import logging
from typing import Dict, List, Optional, Any, Tuple

logger = logging.getLogger(__name__)

# Default Kite instruments paths (configurable via settings)
# These are fallbacks if not specified in config
DEFAULT_KITE_INSTRUMENTS_PATH = "../data/index_options.csv"
DEFAULT_KITE_STOCK_INSTRUMENTS_PATH = "../data/stock_instruments.csv"


class SymbolMapper:
    """Maps Kite symbols to NEO trading parameters."""

    def __init__(self, neo_client, config: Dict[str, Any] = None):
        self.client = neo_client
        self.config = config or {}
        self.cache_dir = self.config.get('paths', {}).get('scrip_master', 'data/scrip_master')
        self.mapping_cache_file = os.path.join(self.cache_dir, 'mapping_cache.json')
        self.nse_fo_df: Optional[pd.DataFrame] = None
        self.nse_cm_df: Optional[pd.DataFrame] = None
        self.bse_fo_df: Optional[pd.DataFrame] = None
        self.loaded_date: Optional[str] = None

        # Kite instruments DataFrame
        self.kite_instruments: Optional[pd.DataFrame] = None

        # Pre-computed mapping: kite_tradingsymbol -> NEO params
        self.kite_to_neo_map: Dict[str, Dict[str, Any]] = {}
        self.mapping_ready = False

        # Thread safety for map access (build_mapping_cache and search_and_map can run concurrently)
        self._map_lock = threading.Lock()

        # Default lot sizes (fallback)
        self.lot_sizes = self.config.get('lot_sizes', {
            'NIFTY': 65,      # Updated
            'BANKNIFTY': 30,  # Updated
            'FINNIFTY': 60,   # Updated
            'SENSEX': 10,
            'BANKEX': 15,
            'MIDCPNIFTY': 120,  # Updated
        })

        # Index info from config
        self.indices = self.config.get('indices', {
            'NIFTY': {'strike_gap': 50, 'exchange': 'NFO'},
            'BANKNIFTY': {'strike_gap': 100, 'exchange': 'NFO'},
            'FINNIFTY': {'strike_gap': 50, 'exchange': 'NFO'},
            'SENSEX': {'strike_gap': 100, 'exchange': 'BFO'},
            'BANKEX': {'strike_gap': 100, 'exchange': 'BFO'},
        })

    def build_mapping_cache(self) -> Tuple[bool, str]:
        """
        Build Kite to NEO mapping cache at login time.
        Should be called once at startup after NEO login.

        If today's cache exists, loads from cache (fast).
        Otherwise downloads fresh and saves to cache.

        Returns:
            Tuple of (success: bool, message: str)
        """
        try:
            # Check if today's cache exists
            if self._load_mapping_cache():
                map_count = len(self.kite_to_neo_map)
                self.mapping_ready = True
                return True, f"Mapping loaded from cache: {map_count} symbols (fast)"

            # Step 1: Load Kite instruments
            kite_count = self._load_kite_instruments()
            if kite_count == 0:
                return False, "No Kite instruments loaded"

            # Step 2: Download NEO instruments for active underlyings
            neo_count = self._download_neo_instruments()

            # Step 3: Build mapping
            map_count = self._build_mapping()

            # Step 4: Save mapping cache for today
            self._save_mapping_cache()

            self.mapping_ready = True
            return True, f"Mapping ready: {kite_count} Kite, {neo_count} NEO, {map_count} mapped"

        except Exception as e:
            logger.error(f"Failed to build mapping cache: {e}", exc_info=True)
            return False, f"Mapping failed: {str(e)}"

    def _load_mapping_cache(self) -> bool:
        """
        Load mapping from today's cache file if it exists.

        Returns:
            True if cache loaded successfully, False otherwise
        """
        try:
            if not os.path.exists(self.mapping_cache_file):
                logger.info("No mapping cache file found, will download fresh")
                return False

            with open(self.mapping_cache_file, 'r') as f:
                cache_data = json.load(f)

            # Check if cache is from today
            cache_date = cache_data.get('date')
            today = date.today().isoformat()

            if cache_date != today:
                logger.info(f"Mapping cache is from {cache_date}, need fresh for {today}")
                return False

            # Load the mapping
            self.kite_to_neo_map = cache_data.get('mapping', {})

            if not self.kite_to_neo_map:
                logger.info("Mapping cache is empty, will download fresh")
                return False

            logger.info(f"Loaded {len(self.kite_to_neo_map)} mappings from today's cache (FAST)")
            return True

        except Exception as e:
            logger.warning(f"Failed to load mapping cache: {e}")
            return False

    def _save_mapping_cache(self) -> bool:
        """
        Save mapping to cache file with today's date.

        Returns:
            True if saved successfully, False otherwise
        """
        try:
            os.makedirs(self.cache_dir, exist_ok=True)

            cache_data = {
                'date': date.today().isoformat(),
                'mapping': self.kite_to_neo_map
            }

            with open(self.mapping_cache_file, 'w') as f:
                json.dump(cache_data, f)

            logger.info(f"Saved {len(self.kite_to_neo_map)} mappings to cache")
            return True

        except Exception as e:
            logger.warning(f"Failed to save mapping cache: {e}")
            return False

    def _load_kite_instruments(self) -> int:
        """Load Kite instruments from shared BOTS data folder."""
        try:
            dfs = []

            # Get paths from config or use defaults
            paths_config = self.config.get('paths', {})
            kite_index_path = paths_config.get('kite_instruments', DEFAULT_KITE_INSTRUMENTS_PATH)
            kite_stock_path = paths_config.get('kite_stock_instruments', DEFAULT_KITE_STOCK_INSTRUMENTS_PATH)

            # Load index options
            if os.path.exists(kite_index_path):
                df = pd.read_csv(kite_index_path, dtype=str)
                dfs.append(df)
                logger.info(f"Loaded {len(df)} index options from Kite")
            else:
                logger.debug(f"Kite index instruments file not found: {kite_index_path}")

            # Load stock instruments (F&O)
            if os.path.exists(kite_stock_path):
                df = pd.read_csv(kite_stock_path, dtype=str)
                # Filter for F&O only
                if 'segment' in df.columns:
                    df = df[df['segment'].isin(['NFO-OPT', 'NFO-FUT', 'BFO-OPT', 'BFO-FUT'])]
                dfs.append(df)
                logger.info(f"Loaded {len(df)} stock F&O from Kite")
            else:
                logger.debug(f"Kite stock instruments file not found: {kite_stock_path}")

            if dfs:
                self.kite_instruments = pd.concat(dfs, ignore_index=True)
                return len(self.kite_instruments)
            else:
                logger.warning("No Kite instrument files found")
                return 0

        except Exception as e:
            logger.error(f"Failed to load Kite instruments: {e}")
            return 0

    def _download_neo_instruments(self) -> int:
        """Download NEO instruments for active underlyings."""
        try:
            # Get unique underlyings from Kite instruments
            if self.kite_instruments is None:
                return 0

            # Common underlyings for F&O
            underlyings = ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX']

            all_neo_instruments = []

            for underlying in underlyings:
                try:
                    exchange = 'bse_fo' if underlying in ['SENSEX', 'BANKEX'] else 'nse_fo'
                    result = self.client.search_scrip(
                        exchange_segment=exchange,
                        symbol=underlying
                    )
                    if isinstance(result, list):
                        for item in result:
                            item['_underlying'] = underlying
                            item['_exchange'] = exchange
                        all_neo_instruments.extend(result)
                        logger.info(f"Downloaded {len(result)} NEO instruments for {underlying}")
                except Exception as e:
                    logger.warning(f"Failed to download NEO instruments for {underlying}: {e}")

            if all_neo_instruments:
                self.nse_fo_df = pd.DataFrame(all_neo_instruments)
                return len(all_neo_instruments)

            return 0

        except Exception as e:
            logger.error(f"Failed to download NEO instruments: {e}")
            return 0

    def _build_mapping(self) -> int:
        """Build Kite to NEO symbol mapping."""
        if self.kite_instruments is None or self.nse_fo_df is None:
            return 0

        mapped = 0

        # Find trading symbol column in Kite data
        kite_symbol_col = None
        for col in ['option_symbol', 'tradingsymbol', 'trading_symbol', 'TradingSymbol', 'symbol']:
            if col in self.kite_instruments.columns:
                kite_symbol_col = col
                break

        if not kite_symbol_col:
            logger.warning("No trading symbol column found in Kite instruments")
            return 0

        # Build NEO symbol lookup dict
        neo_lookup = {}
        for _, row in self.nse_fo_df.iterrows():
            neo_symbol = str(row.get('pTrdSymbol', '')).upper()
            if neo_symbol:
                neo_lookup[neo_symbol] = row.to_dict()

        # Map Kite symbols to NEO
        for _, kite_row in self.kite_instruments.iterrows():
            kite_symbol = str(kite_row.get(kite_symbol_col, '')).upper()
            if not kite_symbol:
                continue

            # Direct match
            if kite_symbol in neo_lookup:
                neo_row = neo_lookup[kite_symbol]
                self.kite_to_neo_map[kite_symbol] = {
                    'trading_symbol': neo_row.get('pTrdSymbol', kite_symbol),
                    'exchange_segment': neo_row.get('_exchange', 'nse_fo'),
                    'lot_size': int(neo_row.get('lLotSize', 1)),
                    'instrument_token': str(neo_row.get('pSymbol', '')),
                    'tick_size': float(neo_row.get('dTickSize', 5)) / 100,
                    'kite_token': kite_row.get('instrument_token', ''),
                }
                mapped += 1

        logger.info(f"Built mapping for {mapped} symbols")
        return mapped

    def get_neo_params(self, kite_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Get NEO trading parameters for a Kite symbol.
        Uses pre-computed mapping for instant lookup.
        Does NOT fallback to live search to avoid UI blocking.

        Args:
            kite_symbol: Kite trading symbol

        Returns:
            Dict with NEO parameters or None if not found
        """
        kite_symbol = kite_symbol.upper()

        # Check pre-computed mapping only (instant) - thread-safe read
        with self._map_lock:
            if kite_symbol in self.kite_to_neo_map:
                return self.kite_to_neo_map[kite_symbol]

        # No live search fallback - return None immediately
        # Use search_and_map for weekly symbols
        return None

    def search_and_map(self, kite_symbol: str) -> Optional[Dict[str, Any]]:
        """
        Live search for a symbol not in cache (e.g., weekly options).
        Slower than cached lookup but necessary for weekly symbols.

        Args:
            kite_symbol: Kite trading symbol (e.g., NIFTY2612225700CE)

        Returns:
            Dict with NEO parameters or None if not found
        """
        if not self.client:
            return None

        kite_symbol = kite_symbol.upper()
        try:
            parsed = self.parse_kite_symbol(kite_symbol)
        except ValueError:
            return None

        if not parsed:
            logger.warning(f"Could not parse symbol: {kite_symbol}")
            return None

        underlying = parsed.get('underlying', '')
        strike = parsed.get('strike', '')
        opt_type = parsed.get('option_type', '')

        # Determine exchange
        exchange = 'bse_fo' if underlying in ['SENSEX', 'BANKEX'] else 'nse_fo'

        try:
            # Search NEO for this symbol
            results = self.client.search_scrip(
                exchange_segment=exchange,
                symbol=underlying
            )

            if not results:
                logger.warning(f"No NEO results for {underlying}")
                return None

            # Get expiry from parsed symbol for matching
            expiry = parsed.get('expiry', '')

            # First try: exact symbol match (fastest, most reliable)
            for item in results:
                neo_symbol = item.get('pTrdSymbol', '').upper()
                if neo_symbol == kite_symbol:
                    lot_size = int(item.get('lLotSize', 1) or 1)
                    lot_sizes = self.config.get('lot_sizes', {})
                    if underlying in lot_sizes:
                        lot_size = lot_sizes[underlying]

                    mapping = {
                        'kite_symbol': kite_symbol,
                        'trading_symbol': item.get('pTrdSymbol', kite_symbol),
                        'instrument_token': str(item.get('pSymbol', '')),
                        'exchange_segment': exchange,
                        'lot_size': lot_size,
                        'underlying': underlying,
                        'strike': strike,
                        'option_type': opt_type
                    }
                    # Thread-safe write to map
                    with self._map_lock:
                        self.kite_to_neo_map[kite_symbol] = mapping
                    logger.info(f"Live mapped (exact): {kite_symbol} -> {neo_symbol}")
                    return mapping

            # Second try: match by strike + option type + expiry
            for item in results:
                neo_symbol = item.get('pTrdSymbol', item.get('tradingSymbol', ''))
                # NEO API has typo: 'dStrikePrice;' (with semicolon)
                neo_strike_raw = item.get('dStrikePrice;', item.get('dStrikePrice', item.get('strikePrice', 0)))
                # NEO stores strikes scaled by 100 (13400 -> 1340000.0)
                try:
                    neo_strike_scaled = float(neo_strike_raw)
                    neo_strike = str(int(neo_strike_scaled / 100))
                except (ValueError, TypeError):
                    neo_strike = str(neo_strike_raw).replace('.0', '')
                # NEO uses pOptionType, not cOptionType
                neo_opt = item.get('pOptionType', item.get('cOptionType', item.get('optionType', '')))

                # Match strike and option type
                if neo_strike == strike and neo_opt == opt_type:
                    # Get lot size
                    lot_size = int(item.get('lLotSize', item.get('lotSize', 1)) or 1)

                    # Get lot size from config if available
                    lot_sizes = self.config.get('lot_sizes', {})
                    if underlying in lot_sizes:
                        lot_size = lot_sizes[underlying]

                    mapping = {
                        'kite_symbol': kite_symbol,
                        'trading_symbol': neo_symbol,
                        # NEO uses pSymbol for instrument token
                        'instrument_token': str(item.get('pSymbol', item.get('pInstToken', item.get('instrumentToken', '')))),
                        'exchange_segment': exchange,
                        'lot_size': lot_size,
                        'underlying': underlying,
                        'strike': strike,
                        'option_type': opt_type
                    }

                    # Add to cache for future lookups - thread-safe write
                    with self._map_lock:
                        self.kite_to_neo_map[kite_symbol] = mapping
                    logger.info(f"Live mapped: {kite_symbol} -> {neo_symbol}")
                    return mapping

            logger.warning(f"No matching NEO instrument for {kite_symbol}")
            return None

        except Exception as e:
            logger.error(f"Live search failed for {kite_symbol}: {e}")
            return None

    def initialize(self) -> Tuple[bool, str]:
        """
        Download and load scrip master files.
        Should be called once at startup before market hours.

        Returns:
            Tuple of (success: bool, message: str)
        """
        os.makedirs(self.cache_dir, exist_ok=True)
        today = date.today().isoformat()

        # Check if today's cache exists
        nse_fo_file = os.path.join(self.cache_dir, f"nse_fo_{today}.csv")

        if os.path.exists(nse_fo_file):
            self._load_from_cache(today)
            count = len(self.nse_fo_df) if self.nse_fo_df is not None else 0
            return True, f"Loaded scrip master from cache ({today}) - {count} instruments"

        # Download fresh
        try:
            return self._download_scrip_masters(today)
        except Exception as e:
            logger.error(f"Failed to download scrip master: {e}", exc_info=True)
            # Try to load most recent cache
            return self._load_latest_cache()

    def _download_scrip_masters(self, today: str) -> Tuple[bool, str]:
        """Download scrip masters from NEO API."""
        try:
            # Download NSE F&O scrip master
            logger.info("Downloading NSE F&O scrip master...")
            nse_fo_data = self.client.scrip_master(exchange_segment="nse_fo")

            if nse_fo_data and 'data' in nse_fo_data and isinstance(nse_fo_data['data'], list):
                self.nse_fo_df = pd.DataFrame(nse_fo_data['data'])
            elif isinstance(nse_fo_data, list):
                self.nse_fo_df = pd.DataFrame(nse_fo_data)
            elif isinstance(nse_fo_data, dict) and nse_fo_data.get('stat', '').lower() in ['not_ok', 'error']:
                # API returned error response
                error_msg = nse_fo_data.get('error', nse_fo_data.get('message', 'Unknown error'))
                raise Exception(f"NSE F&O scrip master API error: {error_msg}")
            else:
                raise Exception(f"Unexpected scrip master response format: {type(nse_fo_data)}")

            nse_fo_file = os.path.join(self.cache_dir, f"nse_fo_{today}.csv")
            self.nse_fo_df.to_csv(nse_fo_file, index=False)
            logger.info(f"Saved {len(self.nse_fo_df)} NSE F&O instruments")

            # Download NSE Cash scrip master
            try:
                logger.info("Downloading NSE Cash scrip master...")
                nse_cm_data = self.client.scrip_master(exchange_segment="nse_cm")
                if nse_cm_data and 'data' in nse_cm_data and isinstance(nse_cm_data['data'], list):
                    self.nse_cm_df = pd.DataFrame(nse_cm_data['data'])
                    nse_cm_file = os.path.join(self.cache_dir, f"nse_cm_{today}.csv")
                    self.nse_cm_df.to_csv(nse_cm_file, index=False)
                elif isinstance(nse_cm_data, list):
                    self.nse_cm_df = pd.DataFrame(nse_cm_data)
                    nse_cm_file = os.path.join(self.cache_dir, f"nse_cm_{today}.csv")
                    self.nse_cm_df.to_csv(nse_cm_file, index=False)
                elif isinstance(nse_cm_data, dict) and nse_cm_data.get('stat', '').lower() in ['not_ok', 'error']:
                    error_msg = nse_cm_data.get('error', nse_cm_data.get('message', 'Unknown error'))
                    logger.warning(f"NSE Cash scrip master API error: {error_msg}")
                else:
                    logger.warning(f"Unexpected NSE Cash scrip master format: {type(nse_cm_data)}")
            except Exception as e:
                logger.warning(f"NSE Cash download failed: {e}")

            # Download BSE F&O scrip master (for SENSEX/BANKEX)
            try:
                logger.info("Downloading BSE F&O scrip master...")
                bse_fo_data = self.client.scrip_master(exchange_segment="bse_fo")
                if bse_fo_data and 'data' in bse_fo_data and isinstance(bse_fo_data['data'], list):
                    self.bse_fo_df = pd.DataFrame(bse_fo_data['data'])
                    bse_fo_file = os.path.join(self.cache_dir, f"bse_fo_{today}.csv")
                    self.bse_fo_df.to_csv(bse_fo_file, index=False)
                elif isinstance(bse_fo_data, list):
                    self.bse_fo_df = pd.DataFrame(bse_fo_data)
                    bse_fo_file = os.path.join(self.cache_dir, f"bse_fo_{today}.csv")
                    self.bse_fo_df.to_csv(bse_fo_file, index=False)
                elif isinstance(bse_fo_data, dict) and bse_fo_data.get('stat', '').lower() in ['not_ok', 'error']:
                    error_msg = bse_fo_data.get('error', bse_fo_data.get('message', 'Unknown error'))
                    logger.warning(f"BSE F&O scrip master API error: {error_msg}")
                else:
                    logger.warning(f"Unexpected BSE F&O scrip master format: {type(bse_fo_data)}")
            except Exception as e:
                logger.warning(f"BSE F&O download failed: {e}")

            self.loaded_date = today
            count = len(self.nse_fo_df) if self.nse_fo_df is not None else 0
            return True, f"Downloaded fresh scrip master ({today}) - {count} instruments"

        except Exception as e:
            return False, f"Failed to download scrip master: {str(e)}"

    def _load_from_cache(self, date_str: str):
        """Load from cached CSV files."""
        nse_fo_file = os.path.join(self.cache_dir, f"nse_fo_{date_str}.csv")
        if os.path.exists(nse_fo_file):
            self.nse_fo_df = pd.read_csv(nse_fo_file, dtype=str)
            logger.info(f"Loaded {len(self.nse_fo_df)} NSE F&O instruments from cache")

        nse_cm_file = os.path.join(self.cache_dir, f"nse_cm_{date_str}.csv")
        if os.path.exists(nse_cm_file):
            self.nse_cm_df = pd.read_csv(nse_cm_file, dtype=str)

        bse_fo_file = os.path.join(self.cache_dir, f"bse_fo_{date_str}.csv")
        if os.path.exists(bse_fo_file):
            self.bse_fo_df = pd.read_csv(bse_fo_file, dtype=str)

        self.loaded_date = date_str

    def _load_latest_cache(self) -> Tuple[bool, str]:
        """Load most recent cache file."""
        try:
            files = [f for f in os.listdir(self.cache_dir) if f.startswith('nse_fo_')]
            if not files:
                return False, "No cached scrip master found"

            # Get most recent
            files.sort(reverse=True)
            latest = files[0]
            date_str = latest.replace('nse_fo_', '').replace('.csv', '')

            self._load_from_cache(date_str)
            count = len(self.nse_fo_df) if self.nse_fo_df is not None else 0
            return True, f"Loaded scrip master from cache ({date_str}) - {count} instruments"
        except Exception as e:
            return False, f"Failed to load cache: {str(e)}"

    def parse_kite_symbol(self, kite_symbol: str) -> Dict[str, Any]:
        """
        Parse Kite symbol format into components.

        Examples:
            NIFTY25JAN24000CE -> {underlying: NIFTY, expiry: 25JAN, strike: 24000, opt_type: CE}
            BANKNIFTY25JAN52000PE -> {underlying: BANKNIFTY, expiry: 25JAN, strike: 52000, opt_type: PE}
            NIFTY25115024000CE -> {underlying: NIFTY, expiry: 251150, strike: 24000, opt_type: CE} (weekly)
            NIFTY25JANFUT -> {underlying: NIFTY, expiry: 25JAN, instrument: FUT}

        Returns:
            Parsed symbol dict
        """
        kite_symbol = kite_symbol.upper().strip()

        # Pattern for monthly options: SYMBOL + YYMM + STRIKE + CE/PE
        # e.g., NIFTY25JAN24000CE, BANKNIFTY25FEB52000PE
        monthly_option_pattern = r'^([A-Z]+)(\d{2}[A-Z]{3})(\d+)(CE|PE)$'

        # Pattern for weekly options: SYMBOL + YYMMDD + STRIKE + CE/PE
        # e.g., NIFTY2511624000CE (16th Jan 2025)
        weekly_option_pattern = r'^([A-Z]+)(\d{5,7})(\d+)(CE|PE)$'

        # Pattern for futures: SYMBOL + YYMM + FUT
        future_pattern = r'^([A-Z]+)(\d{2}[A-Z]{3})(FUT)$'

        # Try monthly options pattern
        match = re.match(monthly_option_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'strike': match.group(3),
                'option_type': match.group(4),
                'instrument_type': 'OPTION',
                'expiry_type': 'MONTHLY'
            }

        # Try weekly options pattern
        match = re.match(weekly_option_pattern, kite_symbol)
        if match:
            # Parse YYMMDD format
            expiry_str = match.group(2)
            return {
                'underlying': match.group(1),
                'expiry': expiry_str,
                'strike': match.group(3),
                'option_type': match.group(4),
                'instrument_type': 'OPTION',
                'expiry_type': 'WEEKLY'
            }

        # Try futures pattern
        match = re.match(future_pattern, kite_symbol)
        if match:
            return {
                'underlying': match.group(1),
                'expiry': match.group(2),
                'option_type': None,
                'strike': None,
                'instrument_type': 'FUTURE'
            }

        raise ValueError(f"Unable to parse symbol: {kite_symbol}")

    def map_to_neo(self, kite_symbol: str) -> Dict[str, Any]:
        """
        Map Kite symbol to NEO trading parameters.

        Args:
            kite_symbol: Kite format symbol like NIFTY25JAN24000CE

        Returns:
            Dict ready for place_order():
            {
                'exchange_segment': 'nse_fo',
                'trading_symbol': 'NIFTY25JAN24000CE',
                'instrument_token': '12345',
                'lot_size': 75,
                'tick_size': 0.05
            }
        """
        parsed = self.parse_kite_symbol(kite_symbol)
        underlying = parsed['underlying']

        # Determine exchange (NSE F&O or BSE F&O)
        index_info = self.indices.get(underlying, {})
        exchange = index_info.get('exchange', 'NFO')

        if exchange == 'BFO':
            df = self.bse_fo_df
            exchange_segment = 'bse_fo'
        else:
            df = self.nse_fo_df
            exchange_segment = 'nse_fo'

        if df is None or df.empty:
            raise ValueError("Scrip master not loaded. Call initialize() first.")

        # Build search criteria
        # NEO column names may vary - common ones: pSymbol, pTradingSymbol, pOptionType, pStrikePrice
        possible_symbol_cols = ['pSymbol', 'pSymbolName', 'symbol', 'Symbol']
        possible_trading_cols = ['pTradingSymbol', 'tradingSymbol', 'TradingSymbol', 'trading_symbol']
        possible_option_cols = ['pOptionType', 'optionType', 'OptionType', 'option_type']
        possible_strike_cols = ['pStrikePrice', 'strikePrice', 'StrikePrice', 'strike_price']
        possible_token_cols = ['pScripCode', 'scripCode', 'ScripCode', 'instrument_token', 'token']
        possible_lot_cols = ['pLotSize', 'lotSize', 'LotSize', 'lot_size']
        possible_tick_cols = ['pTickSize', 'tickSize', 'TickSize', 'tick_size']

        def get_col(df, possible_names):
            for col in possible_names:
                if col in df.columns:
                    return col
            return None

        symbol_col = get_col(df, possible_symbol_cols)
        trading_col = get_col(df, possible_trading_cols)
        option_col = get_col(df, possible_option_cols)
        strike_col = get_col(df, possible_strike_cols)
        token_col = get_col(df, possible_token_cols)
        lot_col = get_col(df, possible_lot_cols)
        tick_col = get_col(df, possible_tick_cols)

        # Search by trading symbol first (exact match)
        if trading_col:
            mask = df[trading_col].str.upper() == kite_symbol.upper()
            matches = df[mask]

            if not matches.empty:
                row = matches.iloc[0]
                return self._build_result(
                    row, exchange_segment, parsed,
                    trading_col, token_col, lot_col, tick_col
                )

        # Search by components
        if symbol_col and option_col and strike_col:
            mask = df[symbol_col].str.upper() == underlying

            if parsed['instrument_type'] == 'OPTION':
                mask &= df[option_col].str.upper() == parsed['option_type']
                # Strike comparison - handle float/string conversion
                strike_val = parsed['strike']
                mask &= df[strike_col].astype(str).str.replace('.0', '', regex=False) == strike_val

            matches = df[mask]

            if not matches.empty:
                # If multiple matches, try to filter by expiry
                if len(matches) > 1:
                    # Try expiry column filtering
                    expiry_cols = ['pExpiryDate', 'expiryDate', 'ExpiryDate', 'expiry']
                    expiry_col = get_col(df, expiry_cols)
                    if expiry_col:
                        expiry_pattern = parsed['expiry']
                        expiry_mask = matches[expiry_col].str.contains(expiry_pattern, case=False, na=False)
                        if expiry_mask.any():
                            matches = matches[expiry_mask]

                row = matches.iloc[0]
                return self._build_result(
                    row, exchange_segment, parsed,
                    trading_col, token_col, lot_col, tick_col
                )

        raise ValueError(f"Symbol not found in NEO scrip master: {kite_symbol}")

    def _build_result(self, row: pd.Series, exchange_segment: str, parsed: Dict,
                      trading_col: str, token_col: str, lot_col: str, tick_col: str) -> Dict[str, Any]:
        """Build result dict from matched row."""
        trading_symbol = row[trading_col] if trading_col and trading_col in row else parsed.get('underlying', '')
        instrument_token = str(row[token_col]) if token_col and token_col in row else ''

        lot_size = self._get_lot_size(row, lot_col, parsed['underlying'])
        # S22-M1: Guard tick_size conversion against NaN/invalid values
        tick_size = 0.05  # default
        if tick_col and tick_col in row:
            try:
                tick_val = row[tick_col]
                if pd.notna(tick_val):
                    tick_size = float(tick_val)
            except (ValueError, TypeError):
                pass  # Keep default 0.05

        return {
            'exchange_segment': exchange_segment,
            'trading_symbol': trading_symbol,
            'instrument_token': instrument_token,
            'lot_size': lot_size,
            'tick_size': tick_size,
            'underlying': parsed['underlying'],
            'strike': parsed.get('strike'),
            'option_type': parsed.get('option_type'),
            'expiry': parsed.get('expiry'),
            'instrument_type': parsed.get('instrument_type', 'OPTION')
        }

    def _get_lot_size(self, row: pd.Series, lot_col: str, underlying: str) -> int:
        """Get lot size from row or fallback to defaults."""
        if lot_col and lot_col in row:
            try:
                return int(float(row[lot_col]))
            except (ValueError, TypeError):
                pass
        return self.lot_sizes.get(underlying, 1)

    def get_default_lot_size(self, underlying: str) -> int:
        """Get default lot size for underlying."""
        return self.lot_sizes.get(underlying, 1)

    def search_symbol(self, query: str, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Search for symbols matching query.
        Useful for autocomplete in GUI.

        Args:
            query: Search query
            limit: Max results to return

        Returns:
            List of matching symbols with basic info
        """
        if self.nse_fo_df is None:
            return []

        query = query.upper()

        # Find trading symbol column
        possible_cols = ['pTradingSymbol', 'tradingSymbol', 'TradingSymbol', 'trading_symbol']
        trading_col = None
        for col in possible_cols:
            if col in self.nse_fo_df.columns:
                trading_col = col
                break

        if not trading_col:
            return []

        mask = self.nse_fo_df[trading_col].str.contains(query, na=False, case=False)
        matches = self.nse_fo_df[mask].head(limit)

        # Build results
        results = []
        token_cols = ['pScripCode', 'scripCode', 'instrument_token']
        lot_cols = ['pLotSize', 'lotSize', 'lot_size']

        token_col = None
        for col in token_cols:
            if col in matches.columns:
                token_col = col
                break

        lot_col = None
        for col in lot_cols:
            if col in matches.columns:
                lot_col = col
                break

        for _, row in matches.iterrows():
            # S22-M2: Guard lot_size conversion against invalid values
            lot_size = 0
            if lot_col and pd.notna(row[lot_col]):
                try:
                    lot_size = int(float(row[lot_col]))
                except (ValueError, TypeError):
                    pass  # Keep default 0
            result = {
                'trading_symbol': row[trading_col],
                'token': row[token_col] if token_col else '',
                'lot_size': lot_size
            }
            results.append(result)

        return results

    def get_instrument_token(self, trading_symbol: str) -> Optional[str]:
        """Get instrument token for a trading symbol."""
        try:
            result = self.map_to_neo(trading_symbol)
            return result.get('instrument_token')
        except ValueError:
            return None

    def map_kite_to_neo(self, kite_symbol: str) -> Dict[str, Any]:
        """
        Map Kite symbol to NEO using search_scrip API.
        This is the preferred method - no scrip master download needed.

        Args:
            kite_symbol: Kite format symbol like NIFTY26JAN25800CE

        Returns:
            Dict with NEO trading parameters:
            {
                'trading_symbol': 'NIFTY26JAN25800CE',
                'exchange_segment': 'nse_fo',
                'lot_size': 65,
                'instrument_token': '12345',
                'tick_size': 0.05,
                'expiry_date': '27 Jan, 2026'
            }
        """
        # Step 1: Parse Kite symbol
        parsed = self.parse_kite_symbol(kite_symbol)
        underlying = parsed['underlying']
        strike = parsed.get('strike')
        option_type = parsed.get('option_type')

        # Determine exchange segment
        if underlying in ['SENSEX', 'BANKEX']:
            exchange_segment = 'bse_fo'
        else:
            exchange_segment = 'nse_fo'

        # Step 2: Search in NEO using search_scrip
        try:
            search_params = {
                'exchange_segment': exchange_segment,
                'symbol': underlying,
            }

            if option_type:
                search_params['option_type'] = option_type
            if strike:
                search_params['strike_price'] = str(strike)

            result = self.client.search_scrip(**search_params)

            if not result or isinstance(result, dict) and 'message' in result:
                raise ValueError(f"Symbol not found in NEO: {kite_symbol}")

            # Step 3: Find exact match from results
            if isinstance(result, list):
                # Match by trading symbol
                for item in result:
                    neo_symbol = item.get('pTrdSymbol', '')
                    if neo_symbol.upper() == kite_symbol.upper():
                        return self._build_neo_result(item, exchange_segment)

                # If no exact match, find by expiry pattern
                expiry_pattern = parsed.get('expiry', '')
                for item in result:
                    neo_symbol = item.get('pTrdSymbol', '')
                    if expiry_pattern.upper() in neo_symbol.upper():
                        if strike and str(strike) in neo_symbol:
                            if option_type and option_type in neo_symbol:
                                return self._build_neo_result(item, exchange_segment)

                # Return first match if available
                if result:
                    return self._build_neo_result(result[0], exchange_segment)

            raise ValueError(f"No matching symbol found for: {kite_symbol}")

        except Exception as e:
            logger.error(f"Failed to map {kite_symbol} to NEO: {e}")
            raise ValueError(f"Failed to map symbol: {str(e)}")

    def _build_neo_result(self, item: Dict, exchange_segment: str) -> Dict[str, Any]:
        """Build result dict from NEO search_scrip response."""
        # S27: Safe conversion for lot_size and tick_size
        try:
            lot_size = int(item.get('lLotSize', 1) or 1)
        except (ValueError, TypeError):
            lot_size = 1
        try:
            tick_size = float(item.get('dTickSize', 5) or 5) / 100
        except (ValueError, TypeError):
            tick_size = 0.05

        return {
            'trading_symbol': item.get('pTrdSymbol', ''),
            'exchange_segment': exchange_segment,
            'lot_size': lot_size,
            'instrument_token': str(item.get('pSymbol', '')),
            'tick_size': tick_size,  # Convert paise to rupees
            'expiry_date': item.get('pScripRefKey', ''),
            'symbol_name': item.get('pSymbolName', ''),
            'instrument_type': item.get('pInstType', ''),
            'option_type': item.get('pOptionType', ''),
        }

    def get_lot_size_from_neo(self, kite_symbol: str) -> int:
        """
        Get lot size for a Kite symbol by querying NEO.

        Args:
            kite_symbol: Kite format symbol

        Returns:
            Lot size from NEO
        """
        try:
            result = self.map_kite_to_neo(kite_symbol)
            return result.get('lot_size', 1)
        except Exception as e:
            logger.warning(f"Failed to get lot size for {kite_symbol}: {e}")
            # Fallback to defaults
            parsed = self.parse_kite_symbol(kite_symbol)
            return self.lot_sizes.get(parsed['underlying'], 1)

    def get_available_expiries(self, underlying: str, use_monthly: bool = True) -> List[date]:
        """
        Get available expiry dates from scrip master for an underlying.
        Uses NEO scrip master as primary source, Kite instruments as fallback.

        Args:
            underlying: Index name (NIFTY, SENSEX, BANKNIFTY, etc.)
            use_monthly: True for monthly expiries, False for weekly

        Returns:
            List of expiry dates sorted ascending (nearest first)
        """
        underlying = underlying.upper()
        expiries = set()
        today = date.today()

        # Try NEO scrip master first (primary source)
        neo_expiries = self._get_expiries_from_neo(underlying, today)
        expiries.update(neo_expiries)

        # Fallback to Kite instruments if no expiries found
        if not expiries and self.kite_instruments is not None:
            kite_expiries = self._get_expiries_from_kite(underlying, today)
            expiries.update(kite_expiries)

        if not expiries:
            logger.warning(f"No expiries found for {underlying} in any data source")
            return []

        # Sort ascending (nearest first)
        sorted_expiries = sorted(expiries)

        # Filter for monthly vs weekly
        if use_monthly:
            # Monthly expiries are in the last week of month
            # Check if expiry is within last 7 days of its month
            monthly = []
            for exp in sorted_expiries:
                # Get last day of the month
                if exp.month == 12:
                    next_month_first = date(exp.year + 1, 1, 1)
                else:
                    next_month_first = date(exp.year, exp.month + 1, 1)
                last_day_of_month = (next_month_first - timedelta(days=1)).day

                # Monthly expiry is in last 7 days of month (inclusive)
                if exp.day >= last_day_of_month - 6:
                    monthly.append(exp)
            return monthly if monthly else sorted_expiries[:1]
        else:
            # Weekly: return all expiries, nearest first
            return sorted_expiries

    def _get_expiries_from_neo(self, underlying: str, today: date) -> set:
        """Extract expiry dates from NEO scrip master."""
        expiries = set()

        # Determine which dataframe to use
        if underlying in ['SENSEX', 'BANKEX']:
            df = self.bse_fo_df
        else:
            df = self.nse_fo_df

        if df is None or df.empty:
            return expiries

        try:
            # Find the expiry date column
            expiry_col = None
            for col in ['pExpiryDate', 'expiryDate', 'ExpiryDate', 'expiry']:
                if col in df.columns:
                    expiry_col = col
                    break

            if not expiry_col:
                return expiries

            # Find trading symbol column
            symbol_col = None
            for col in ['pTrdSymbol', 'tradingSymbol', 'TradingSymbol']:
                if col in df.columns:
                    symbol_col = col
                    break

            if not symbol_col:
                return expiries

            # Filter for underlying options (CE/PE)
            mask = df[symbol_col].str.upper().str.startswith(underlying)
            mask &= df[symbol_col].str.upper().str.contains('CE|PE')
            filtered = df[mask]

            if filtered.empty:
                return expiries

            # Parse expiry dates
            for expiry_str in filtered[expiry_col].unique():
                exp_date = self._parse_expiry_date(expiry_str)
                if exp_date and exp_date >= today:
                    expiries.add(exp_date)

        except Exception as e:
            logger.debug(f"Error getting NEO expiries for {underlying}: {e}")

        return expiries

    def _get_expiries_from_kite(self, underlying: str, today: date) -> set:
        """Extract expiry dates from Kite instruments (fallback)."""
        expiries = set()

        if self.kite_instruments is None or self.kite_instruments.empty:
            return expiries

        try:
            df = self.kite_instruments

            # Find expiry column
            expiry_col = None
            for col in ['expiry', 'expiry_date', 'ExpiryDate']:
                if col in df.columns:
                    expiry_col = col
                    break

            if not expiry_col:
                return expiries

            # Find symbol/name column
            symbol_col = None
            for col in ['name', 'tradingsymbol', 'trading_symbol', 'option_symbol']:
                if col in df.columns:
                    symbol_col = col
                    break

            if not symbol_col:
                return expiries

            # Filter for underlying
            mask = df[symbol_col].str.upper().str.startswith(underlying)
            filtered = df[mask]

            if filtered.empty:
                return expiries

            # Parse expiry dates
            for expiry_str in filtered[expiry_col].unique():
                exp_date = self._parse_expiry_date(expiry_str)
                if exp_date and exp_date >= today:
                    expiries.add(exp_date)

        except Exception as e:
            logger.debug(f"Error getting Kite expiries for {underlying}: {e}")

        return expiries

    def _parse_expiry_date(self, expiry_str) -> Optional[date]:
        """Parse expiry date string to date object."""
        if pd.isna(expiry_str):
            return None

        expiry_str = str(expiry_str).strip()

        # Try multiple date formats
        for fmt in ['%d-%b-%Y', '%d-%m-%Y', '%Y-%m-%d', '%d %b %Y', '%d/%m/%Y',
                    '%Y-%m-%dT%H:%M:%S', '%d-%b-%y', '%d %b, %Y']:
            try:
                return datetime.strptime(expiry_str, fmt).date()
            except ValueError:
                continue

        return None

    def get_nearest_expiry(self, underlying: str, use_monthly: bool = True) -> Optional[date]:
        """
        Get the nearest available expiry date for an underlying.

        Args:
            underlying: Index name
            use_monthly: True for monthly, False for weekly (nearest)

        Returns:
            Nearest expiry date or None
        """
        expiries = self.get_available_expiries(underlying, use_monthly)
        return expiries[0] if expiries else None

    def search_equity(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        Search for an equity (cash market) symbol using NEO search_scrip API.

        Args:
            symbol: Equity symbol like 'ICICIBANK', 'RELIANCE', etc.

        Returns:
            Dict with NEO trading parameters or None if not found
        """
        if not self.client:
            return None

        # S31: Strip -EQ suffix if present (NEO uses plain symbol for search)
        symbol = symbol.upper().strip()
        if symbol.endswith('-EQ'):
            symbol = symbol[:-3]

        try:
            # Search in NSE Cash Market segment
            result = self.client.search_scrip(
                exchange_segment='nse_cm',
                symbol=symbol
            )

            if not result:
                logger.warning(f"No equity results for {symbol}")
                return None

            # Find exact match from results
            if isinstance(result, list):
                for item in result:
                    neo_symbol = item.get('pTrdSymbol', item.get('pSymbolName', '')).upper()
                    # Exact match on symbol name
                    if neo_symbol == symbol or neo_symbol.startswith(symbol + '-'):
                        # Get lot size (typically 1 for equity)
                        try:
                            lot_size = int(item.get('lLotSize', 1) or 1)
                        except (ValueError, TypeError):
                            lot_size = 1

                        # Get tick size
                        try:
                            tick_size_raw = float(item.get('dTickSize', 5) or 5)
                            tick_size = tick_size_raw / 100  # Convert paise to rupees
                        except (ValueError, TypeError):
                            tick_size = 0.05

                        mapping = {
                            'kite_symbol': symbol,
                            'trading_symbol': item.get('pTrdSymbol', symbol),
                            'instrument_token': str(item.get('pSymbol', '')),
                            'exchange_segment': 'nse_cm',  # NSE Cash Market
                            'lot_size': lot_size,
                            'tick_size': tick_size,
                            'instrument_type': 'EQ',
                            'symbol_name': item.get('pSymbolName', symbol),
                            'series': item.get('pSeries', 'EQ'),
                        }

                        # Add to cache for future lookups
                        with self._map_lock:
                            self.kite_to_neo_map[symbol] = mapping

                        logger.info(f"Equity mapped: {symbol} -> {mapping['trading_symbol']}")
                        return mapping

                # If no exact match, try first result
                if result:
                    item = result[0]
                    try:
                        lot_size = int(item.get('lLotSize', 1) or 1)
                    except (ValueError, TypeError):
                        lot_size = 1

                    try:
                        tick_size_raw = float(item.get('dTickSize', 5) or 5)
                        tick_size = tick_size_raw / 100
                    except (ValueError, TypeError):
                        tick_size = 0.05

                    mapping = {
                        'kite_symbol': symbol,
                        'trading_symbol': item.get('pTrdSymbol', symbol),
                        'instrument_token': str(item.get('pSymbol', '')),
                        'exchange_segment': 'nse_cm',
                        'lot_size': lot_size,
                        'tick_size': tick_size,
                        'instrument_type': 'EQ',
                        'symbol_name': item.get('pSymbolName', symbol),
                        'series': item.get('pSeries', 'EQ'),
                    }

                    with self._map_lock:
                        self.kite_to_neo_map[symbol] = mapping

                    logger.info(f"Equity mapped (first match): {symbol} -> {mapping['trading_symbol']}")
                    return mapping

            logger.warning(f"No matching equity symbol found for: {symbol}")
            return None

        except Exception as e:
            logger.error(f"Equity search failed for {symbol}: {e}")
            return None

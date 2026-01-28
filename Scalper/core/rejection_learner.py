"""
NEO Trade Terminal - Rejection Pattern Learner

Learns from order rejections and auto-applies fixes for future orders.
Stores learned patterns persistently in JSON file.
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class RejectionLearner:
    """
    Learns from order rejections and suggests/applies fixes.

    Patterns detected:
    - MIS blocked for monthly expiry > 1 week out -> Use NRML
    - Other patterns can be added as discovered
    """

    # Rule TTL: rules expire after this many days (broker policies change)
    RULE_TTL_DAYS = 30

    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(exist_ok=True)
        self.rules_file = self.data_dir / "rejection_rules.json"
        self.rules: Dict[str, Any] = self._load_rules()
        # Clean up expired rules on init
        self._cleanup_expired_rules()

        # Built-in pattern matchers (regex pattern -> rule to apply)
        self.pattern_matchers = [
            {
                'pattern': r'MIS.*MONTHLY\s*EXPIRY|MONTHLY\s*EXPIRY.*MIS',
                'rule_type': 'product_switch',
                'from': 'MIS',
                'to': 'NRML',
                'description': 'MIS blocked for monthly expiry > 1 week out'
            },
            {
                'pattern': r'BO\s*ORDER.*MONTHLY\s*EXPIRY|MONTHLY\s*EXPIRY.*BO',
                'rule_type': 'product_switch',
                'from': 'MIS',
                'to': 'NRML',
                'description': 'BO blocked for monthly expiry > 1 week out'
            },
            {
                'pattern': r'RMS.*Blocked.*MIS',
                'rule_type': 'product_switch',
                'from': 'MIS',
                'to': 'NRML',
                'description': 'RMS blocked MIS order'
            }
        ]

    def _load_rules(self) -> Dict[str, Any]:
        """Load learned rules from file."""
        if self.rules_file.exists():
            try:
                with open(self.rules_file, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.warning(f"[LEARNER] Failed to load rules: {e}")
        return {
            'symbol_rules': {},  # symbol -> {product: 'NRML', reason: '...'}
            'index_rules': {},   # index_name -> {product: 'NRML', reason: '...'}
            'rejection_history': []  # List of recent rejections for analysis
        }

    def _save_rules(self):
        """Save learned rules to file."""
        try:
            with open(self.rules_file, 'w') as f:
                json.dump(self.rules, f, indent=2, default=str)
            logger.info(f"[LEARNER] Rules saved to {self.rules_file}")
        except Exception as e:
            logger.error(f"[LEARNER] Failed to save rules: {e}")

    def _is_rule_expired(self, rule: Dict) -> bool:
        """Check if a rule has expired based on TTL."""
        learned_at = rule.get('learned_at')
        if not learned_at:
            return True  # No timestamp = treat as expired

        try:
            learned_date = datetime.fromisoformat(learned_at)
            age_days = (datetime.now() - learned_date).days
            return age_days > self.RULE_TTL_DAYS
        except (ValueError, TypeError):
            return True

    def _cleanup_expired_rules(self):
        """Remove expired rules (older than RULE_TTL_DAYS)."""
        cleaned = False

        # Clean symbol rules
        expired_symbols = [
            symbol for symbol, rule in self.rules.get('symbol_rules', {}).items()
            if self._is_rule_expired(rule)
        ]
        for symbol in expired_symbols:
            del self.rules['symbol_rules'][symbol]
            logger.info(f"[LEARNER] Expired rule removed for symbol: {symbol}")
            cleaned = True

        # Clean index rules
        expired_indices = [
            index for index, rule in self.rules.get('index_rules', {}).items()
            if self._is_rule_expired(rule)
        ]
        for index in expired_indices:
            del self.rules['index_rules'][index]
            logger.info(f"[LEARNER] Expired rule removed for index: {index}")
            cleaned = True

        if cleaned:
            self._save_rules()

    def learn_from_rejection(self, symbol: str, rejection_reason: str,
                             order_details: Optional[Dict] = None) -> Optional[Dict]:
        """
        Learn from a rejection and create a rule if pattern matches.

        Args:
            symbol: Trading symbol that was rejected
            rejection_reason: The rejection reason from broker
            order_details: Optional dict with product, order_type, etc.

        Returns:
            Dict with learned rule if pattern matched, None otherwise
        """
        if not rejection_reason:
            return None

        rejection_upper = rejection_reason.upper()
        learned_rule = None

        # Check against known patterns
        for matcher in self.pattern_matchers:
            if re.search(matcher['pattern'], rejection_upper, re.IGNORECASE):
                # Pattern matched - create rule
                rule = {
                    'product': matcher['to'],
                    'reason': matcher['description'],
                    'original_rejection': rejection_reason[:200],  # Truncate
                    'learned_at': datetime.now().isoformat(),
                    'from_product': matcher.get('from', 'MIS')
                }

                # Extract index name from symbol (BANKNIFTY, NIFTY, FINNIFTY, etc.)
                index_name = self._extract_index_name(symbol)

                if index_name:
                    # Apply rule to entire index
                    self.rules['index_rules'][index_name] = rule
                    logger.warning(f"[LEARNER] Learned: {index_name} -> Use {rule['product']} "
                                  f"(Reason: {rule['reason']})")
                    learned_rule = {'index': index_name, **rule}
                else:
                    # Apply to specific symbol
                    self.rules['symbol_rules'][symbol] = rule
                    logger.warning(f"[LEARNER] Learned: {symbol} -> Use {rule['product']}")
                    learned_rule = {'symbol': symbol, **rule}

                # Save rules
                self._save_rules()
                break

        # Store in rejection history (keep last 50)
        self.rules['rejection_history'].append({
            'symbol': symbol,
            'reason': rejection_reason[:500],
            'timestamp': datetime.now().isoformat(),
            'order_details': order_details
        })
        self.rules['rejection_history'] = self.rules['rejection_history'][-50:]
        self._save_rules()

        return learned_rule

    def _extract_index_name(self, symbol: str) -> Optional[str]:
        """Extract index name from trading symbol."""
        symbol_upper = symbol.upper()

        # Known index prefixes
        indices = ['BANKNIFTY', 'NIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX', 'BANKEX']

        for index in indices:
            if symbol_upper.startswith(index):
                return index

        return None

    def get_recommended_product(self, symbol: str, current_product: str = 'MIS') -> Dict[str, Any]:
        """
        Get recommended product type for a symbol based on learned rules.

        Args:
            symbol: Trading symbol
            current_product: Currently selected product

        Returns:
            Dict with 'product' and 'reason' if rule applies, empty dict otherwise
        """
        # Check symbol-specific rules first
        if symbol in self.rules.get('symbol_rules', {}):
            rule = self.rules['symbol_rules'][symbol]
            if current_product == rule.get('from_product', 'MIS'):
                return {
                    'product': rule['product'],
                    'reason': rule['reason'],
                    'source': 'symbol_rule'
                }

        # Check index-level rules
        index_name = self._extract_index_name(symbol)
        if index_name and index_name in self.rules.get('index_rules', {}):
            rule = self.rules['index_rules'][index_name]
            if current_product == rule.get('from_product', 'MIS'):
                return {
                    'product': rule['product'],
                    'reason': rule['reason'],
                    'source': 'index_rule'
                }

        return {}

    def should_switch_product(self, symbol: str, current_product: str = 'MIS') -> bool:
        """Check if product should be switched based on learned rules."""
        recommendation = self.get_recommended_product(symbol, current_product)
        return bool(recommendation)

    def clear_rule(self, symbol_or_index: str):
        """Clear a learned rule for symbol or index."""
        if symbol_or_index in self.rules.get('symbol_rules', {}):
            del self.rules['symbol_rules'][symbol_or_index]
            self._save_rules()
            logger.info(f"[LEARNER] Cleared rule for symbol: {symbol_or_index}")
        elif symbol_or_index in self.rules.get('index_rules', {}):
            del self.rules['index_rules'][symbol_or_index]
            self._save_rules()
            logger.info(f"[LEARNER] Cleared rule for index: {symbol_or_index}")

    def get_all_rules(self) -> Dict[str, Any]:
        """Get all learned rules."""
        return {
            'symbol_rules': self.rules.get('symbol_rules', {}),
            'index_rules': self.rules.get('index_rules', {})
        }

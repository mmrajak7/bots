"""
Investment Decision Store — CRUD with Google Drive sync.

Tracks all stock analysis decisions: BUY accepted, BUY rejected,
WAIT, EXIT, SELL — with full thesis, scores, and outcome tracking.

Follows the proven bcs/trade_store.py pattern:
  - In-memory cache for reads (zero network)
  - Local file written FIRST on every write
  - Drive sync: download on startup, upload on writes
  - Version-based merge for multi-machine sync
"""

import json
import logging
import os
import platform
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent.resolve()       # playbook/portfolio_tracker/
PLAYBOOK_DIR = SCRIPT_DIR.parent                   # playbook/
PROJECT_ROOT = PLAYBOOK_DIR.parent                 # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_FILE = LOG_DIR / 'portfolio_decisions.json'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'portfolio_config.json'

# Add Helper to path for bcs.drive_store import
import sys
sys.path.insert(0, str(PROJECT_ROOT))

from bcs import drive_store

# ── Required fields ─────────────────────────────────────────────────────────
REQUIRED_FIELDS = [
    'symbol', 'decision_date', 'verdict', 'composite_score', 'thesis',
]

VALID_VERDICTS = {'BUY', 'WAIT', 'EXIT', 'SELL'}
VALID_STATUSES = {'accepted', 'rejected', 'watching', 'exited'}


def _load_config() -> dict:
    """Load portfolio config."""
    if not CONFIG_FILE.exists():
        logger.warning("Config not found at %s, using defaults", CONFIG_FILE)
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials(config: dict) -> Optional[Path]:
    """Resolve Drive credentials path from config + env var."""
    env_path = os.environ.get('PORTFOLIO_GOOGLE_CREDS') or os.environ.get('BCS_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)

    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')

    return Path(path_str) if path_str else None


class DecisionStore:
    """Investment decision CRUD with local file + Google Drive sync.

    Usage:
        store = DecisionStore()
        store.initialize()
        store.add_decision({symbol: 'RELIANCE', verdict: 'BUY', ...})
        store.list_decisions()
    """

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._decisions: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )

    def initialize(self):
        """Startup: auth Drive, download, fall back to local."""
        drive_cfg = self._config.get('google_drive', {})

        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)

        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        self._migrate_decisions()

        active = sum(1 for d in self._decisions
                     if d.get('status') in ('accepted', 'watching'))
        logger.info(
            "DecisionStore: %d decisions (%d active), drive=%s",
            len(self._decisions), active,
            'enabled' if self._drive_enabled else 'disabled'
        )

    def _init_drive(self, drive_cfg: dict):
        """Authenticate with Google Drive."""
        creds_path = _resolve_credentials(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found, running local-only")
            return

        try:
            self._drive_service = drive_store.get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('decisions_file', 'portfolio_decisions.json')
            self._drive_file_id = drive_store.find_file(
                self._drive_service, folder_id, file_name
            )
            self._drive_enabled = True
            logger.info(
                "Drive connected. File: %s",
                self._drive_file_id or '(will create on first write)'
            )
        except Exception as e:
            logger.warning("Drive init failed: %s. Local-only mode.", e)

    def _sync_from_drive(self):
        """Download from Drive and merge with local."""
        try:
            if self._drive_file_id:
                drive_data = drive_store.download_json(
                    self._drive_service, self._drive_file_id
                )
                base = self._decisions if self._decisions else self._read_local()
                merged = self._merge(base, drive_data)
                self._decisions = merged
                self._save_local()
                self._last_sync_time = time.time()
                # Re-upload if merge changed anything
                drive_versions = {d['id']: d.get('version', 0) for d in drive_data}
                merged_versions = {d['id']: d.get('version', 0) for d in merged}
                if drive_versions != merged_versions:
                    self._upload_to_drive()
            else:
                self._load_local()
        except Exception as e:
            logger.warning("Drive download failed: %s. Using local.", e)
            self._load_local()

    def _upload_to_drive(self):
        """Upload to Drive. Logs error on failure."""
        if not self._drive_enabled:
            return
        try:
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('decisions_file', 'portfolio_decisions.json')
            self._drive_file_id = drive_store.upload_json(
                self._drive_service, folder_id, file_name,
                self._decisions, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local is safe.", e)

    def _read_local(self) -> list:
        """Read from local file safely."""
        if not LOCAL_FILE.exists():
            return []
        try:
            with open(LOCAL_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError("Expected list")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            backup = LOCAL_FILE.with_suffix(f'.corrupt.{int(time.time())}.json')
            try:
                LOCAL_FILE.rename(backup)
            except OSError:
                pass
            logger.critical("Decision file CORRUPT (%s). Backed up to %s.", e, backup)
            return []

    def _load_local(self):
        """Load from local file into cache."""
        self._decisions = self._read_local()

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        """Merge two lists. Higher version wins per ID."""
        by_id = {}
        for d in base:
            by_id[d['id']] = d
        for d in incoming:
            did = d['id']
            if did not in by_id:
                by_id[did] = d
            elif d.get('version', 0) > by_id[did].get('version', 0):
                by_id[did] = d
        return sorted(by_id.values(), key=lambda d: d['id'])

    def _save_local(self):
        """Atomic write to local file."""
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='portfolio_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._decisions, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(LOCAL_FILE))
            tmp_path = None
        except Exception:
            if fd is not None:
                os.close(fd)
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise

    def _migrate_decisions(self):
        """Backfill version and account_id fields on legacy entries."""
        changed = False
        for d in self._decisions:
            if 'version' not in d:
                d['version'] = 1
                changed = True
            if 'account_id' not in d:
                d['account_id'] = None
                d['account_name'] = None
                changed = True
        if changed:
            self._save_local()
            self._upload_to_drive()

    # ── Read Operations ───────────────────────────────────────────────────

    def load_decisions(self) -> list:
        """All decisions from cache."""
        return self._decisions

    def get_by_symbol(self, symbol: str) -> list:
        """All decisions for a stock."""
        s = symbol.upper()
        return [d for d in self._decisions if d['symbol'].upper() == s]

    def get_by_status(self, status: str) -> list:
        """All decisions with given status."""
        return [d for d in self._decisions if d.get('status') == status]

    def get_active(self) -> list:
        """All accepted + watching decisions."""
        return [d for d in self._decisions
                if d.get('status') in ('accepted', 'watching')]

    def get_by_verdict(self, verdict: str) -> list:
        """All decisions with given verdict."""
        return [d for d in self._decisions if d.get('verdict') == verdict.upper()]

    def get_by_account(self, account_id: str) -> list:
        """All decisions for a specific account."""
        return [d for d in self._decisions if d.get('account_id') == account_id]

    def get_active_by_account(self, account_id: str) -> list:
        """Active decisions (accepted/watching) for a specific account."""
        return [d for d in self._decisions
                if d.get('account_id') == account_id
                and d.get('status') in ('accepted', 'watching')]

    def find_decision(self, decision_id: int) -> Optional[dict]:
        """Find decision by ID."""
        for d in self._decisions:
            if d['id'] == decision_id:
                return d
        return None

    def next_id(self) -> int:
        """Next available ID."""
        if not self._decisions:
            return 1
        return max(d['id'] for d in self._decisions) + 1

    # ── Write Operations ──────────────────────────────────────────────────

    def add_decision(self, data: dict) -> dict:
        """Add a new investment decision. Returns the decision dict.

        Required: symbol, decision_date, verdict, composite_score, thesis
        Optional: scores, entry_price, target_price, stop_loss, red_flags,
                  bull_case, bear_case, news_at_time, etc.
        """
        # Validate required fields
        missing = [f for f in REQUIRED_FIELDS if f not in data]
        if missing:
            raise ValueError("Missing required fields: %s" % missing)

        # Validate verdict
        verdict = data['verdict'].upper()
        if verdict not in VALID_VERDICTS:
            raise ValueError("Invalid verdict '%s'. Must be: %s" % (verdict, VALID_VERDICTS))
        data['verdict'] = verdict

        # Validate composite score
        score = data['composite_score']
        if not isinstance(score, (int, float)) or score < 0 or score > 100:
            raise ValueError("composite_score must be 0-100, got %s" % score)

        # Set defaults
        data['id'] = self.next_id()
        data['version'] = 1
        data['symbol'] = data['symbol'].upper().strip()
        data.setdefault('account_id', None)  # e.g. "YL6478" or "QSK814"
        data.setdefault('account_name', None)  # e.g. "Investment Portfolio"
        data.setdefault('status', 'watching')
        data.setdefault('scores', {})
        data.setdefault('entry_price', None)
        data.setdefault('target_price', None)
        data.setdefault('stop_loss', None)
        data.setdefault('red_flags', [])
        data.setdefault('bull_case', '')
        data.setdefault('bear_case', '')
        data.setdefault('news_at_time', [])
        # Accepted fields
        data.setdefault('quantity', None)
        data.setdefault('avg_buy_price', None)
        data.setdefault('buy_date', None)
        # Exit fields
        data.setdefault('exit_date', None)
        data.setdefault('exit_price', None)
        data.setdefault('realized_pnl', None)
        data.setdefault('exit_reason', None)
        # Performance tracking
        data.setdefault('current_price', None)
        data.setdefault('unrealized_pnl', None)
        data.setdefault('pnl_pct', None)
        data.setdefault('last_checked', None)
        # Review
        data.setdefault('review_notes', [])
        data.setdefault('last_reviewed', None)

        self._decisions.append(data)
        self._save_local()
        self._upload_to_drive()

        logger.info("Decision #%d: %s %s (score: %.1f)",
                     data['id'], data['verdict'], data['symbol'],
                     data['composite_score'])
        return data

    def accept_decision(self, decision_id: int,
                        quantity: int = None,
                        avg_buy_price: float = None,
                        buy_date: str = None,
                        **extra) -> Optional[dict]:
        """Mark a decision as accepted (actually bought)."""
        d = self.find_decision(decision_id)
        if not d:
            logger.error("Decision #%d not found", decision_id)
            return None

        d['status'] = 'accepted'
        d['version'] = d.get('version', 0) + 1
        if quantity is not None:
            d['quantity'] = quantity
        if avg_buy_price is not None:
            d['avg_buy_price'] = avg_buy_price
        d['buy_date'] = buy_date or datetime.now().strftime('%Y-%m-%d')
        for k, v in extra.items():
            d[k] = v

        self._save_local()
        self._upload_to_drive()
        logger.info("Decision #%d accepted: %s qty=%s at Rs %.2f",
                     decision_id, d['symbol'], quantity, avg_buy_price or 0)
        return d

    def reject_decision(self, decision_id: int, reason: str = '') -> Optional[dict]:
        """Mark a decision as rejected (passed on)."""
        d = self.find_decision(decision_id)
        if not d:
            logger.error("Decision #%d not found", decision_id)
            return None

        d['status'] = 'rejected'
        d['version'] = d.get('version', 0) + 1
        if reason:
            d.setdefault('review_notes', []).append({
                'date': datetime.now().strftime('%Y-%m-%d'),
                'note': 'Rejected: ' + reason
            })

        self._save_local()
        self._upload_to_drive()
        logger.info("Decision #%d rejected: %s", decision_id, d['symbol'])
        return d

    def exit_decision(self, decision_id: int,
                      exit_price: float = None,
                      exit_reason: str = '',
                      **extra) -> Optional[dict]:
        """Mark a decision as exited (sold)."""
        d = self.find_decision(decision_id)
        if not d:
            logger.error("Decision #%d not found", decision_id)
            return None

        d['status'] = 'exited'
        d['version'] = d.get('version', 0) + 1
        d['exit_date'] = datetime.now().strftime('%Y-%m-%d')
        if exit_price is not None:
            d['exit_price'] = exit_price
            # Compute realized P&L if we have buy price
            if d.get('avg_buy_price') and d.get('quantity'):
                d['realized_pnl'] = round(
                    (exit_price - d['avg_buy_price']) * d['quantity'], 2
                )
        d['exit_reason'] = exit_reason
        for k, v in extra.items():
            d[k] = v

        self._save_local()
        self._upload_to_drive()
        logger.info("Decision #%d exited: %s at Rs %.2f (%s)",
                     decision_id, d['symbol'], exit_price or 0, exit_reason)
        return d

    def add_review(self, decision_id: int, note: str,
                   new_verdict: str = None,
                   current_price: float = None) -> Optional[dict]:
        """Add a review note to a decision."""
        d = self.find_decision(decision_id)
        if not d:
            logger.error("Decision #%d not found", decision_id)
            return None

        d['version'] = d.get('version', 0) + 1
        d['last_reviewed'] = datetime.now().strftime('%Y-%m-%d')
        d.setdefault('review_notes', []).append({
            'date': datetime.now().strftime('%Y-%m-%d'),
            'note': note,
            'verdict_change': new_verdict
        })

        if new_verdict:
            d['review_verdict_change'] = new_verdict

        if current_price is not None:
            d['current_price'] = current_price
            d['last_checked'] = datetime.now().isoformat()
            if d.get('avg_buy_price') and d.get('quantity'):
                d['unrealized_pnl'] = round(
                    (current_price - d['avg_buy_price']) * d['quantity'], 2
                )
                d['pnl_pct'] = round(
                    (current_price - d['avg_buy_price']) / d['avg_buy_price'] * 100, 2
                )

        self._save_local()
        self._upload_to_drive()
        return d

    def update_prices(self, price_map: dict):
        """Bulk update current prices. price_map = {symbol: price}.

        Used after fetching LTP from Kite MCP for all active holdings.
        """
        changed = False
        now = datetime.now().isoformat()
        for d in self._decisions:
            if d.get('status') != 'accepted':
                continue
            sym = d['symbol'].upper()
            if sym in price_map:
                price = price_map[sym]
                d['current_price'] = price
                d['last_checked'] = now
                if d.get('avg_buy_price') and d.get('quantity'):
                    d['unrealized_pnl'] = round(
                        (price - d['avg_buy_price']) * d['quantity'], 2
                    )
                    d['pnl_pct'] = round(
                        (price - d['avg_buy_price']) / d['avg_buy_price'] * 100, 2
                    )
                changed = True

        if changed:
            self._version += 1
            self._save_local()
            self._upload_to_drive()

    # ── Analytics ─────────────────────────────────────────────────────────

    def get_scorecard(self) -> dict:
        """Compute decision-making scorecard."""
        accepted = self.get_by_status('accepted')
        rejected = self.get_by_status('rejected')
        exited = self.get_by_status('exited')
        watching = self.get_by_status('watching')

        # Win rate from exited decisions
        wins = [d for d in exited if (d.get('realized_pnl') or 0) > 0]
        losses = [d for d in exited if (d.get('realized_pnl') or 0) < 0]

        total_realized = sum(d.get('realized_pnl', 0) for d in exited)
        total_unrealized = sum(d.get('unrealized_pnl', 0) or 0 for d in accepted)

        return {
            'total_decisions': len(self._decisions),
            'accepted': len(accepted),
            'rejected': len(rejected),
            'exited': len(exited),
            'watching': len(watching),
            'wins': len(wins),
            'losses': len(losses),
            'win_rate': round(len(wins) / max(len(exited), 1) * 100, 1),
            'total_realized_pnl': round(total_realized, 2),
            'total_unrealized_pnl': round(total_unrealized, 2),
            'avg_realized_pnl': round(total_realized / max(len(exited), 1), 2),
        }

    # ── Sync ──────────────────────────────────────────────────────────────

    def maybe_sync(self, force: bool = False):
        """Re-sync from Drive if stale."""
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Display ───────────────────────────────────────────────────────────

    def list_decisions(self, status_filter: str = None, account_id: str = None, limit: int = 50):
        """Print formatted table of decisions."""
        decisions = self._decisions
        if status_filter:
            decisions = [d for d in decisions if d.get('status') == status_filter]
        if account_id:
            decisions = [d for d in decisions if d.get('account_id') == account_id]

        if not decisions:
            print("No decisions found.")
            return

        decisions = decisions[-limit:]  # Most recent

        if account_id:
            # Resolve account name from config
            cfg = self._config.get('accounts', {})
            acct_name = cfg.get(account_id, {}).get('name', account_id)
            print(f"\n  Account: {acct_name} ({account_id})")

        print(f"\n{'ID':>3}  {'Symbol':<12} {'Verdict':<6} {'Status':<10} "
              f"{'Score':>6} {'Entry':>8} {'Current':>8} {'P&L%':>7} "
              f"{'Date':<12} {'Acct':<8}")
        print("-" * 103)

        for d in decisions:
            pnl = d.get('pnl_pct')
            pnl_str = f"{pnl:+.1f}%" if pnl is not None else "N/A"
            entry = d.get('avg_buy_price') or d.get('entry_price')
            entry_str = f"{entry:.1f}" if entry else "N/A"
            curr = d.get('current_price')
            curr_str = f"{curr:.1f}" if curr else "N/A"

            acct = d.get('account_id', '-') or '-'
            print(f"{d['id']:>3}  {d['symbol']:<12} {d['verdict']:<6} "
                  f"{d.get('status', '?'):<10} "
                  f"{d['composite_score']:>6.1f} {entry_str:>8} "
                  f"{curr_str:>8} {pnl_str:>7} "
                  f"{d['decision_date']:<12} {acct:<8}")

        # Summary
        scorecard = self.get_scorecard()
        print(f"\nTotal: {scorecard['total_decisions']} | "
              f"Accepted: {scorecard['accepted']} | "
              f"Rejected: {scorecard['rejected']} | "
              f"Exited: {scorecard['exited']} | "
              f"Watching: {scorecard['watching']}")
        if scorecard['exited'] > 0:
            print(f"Win Rate: {scorecard['win_rate']}% | "
                  f"Realized P&L: Rs {scorecard['total_realized_pnl']:+,.0f}")
        if scorecard['accepted'] > 0:
            print(f"Unrealized P&L: Rs {scorecard['total_unrealized_pnl']:+,.0f}")


# ── Singleton ────────────────────────────────────────────────────────────────

_store: Optional[DecisionStore] = None


def get_decision_store() -> DecisionStore:
    """Get or create the singleton DecisionStore."""
    global _store
    if _store is None:
        _store = DecisionStore()
        _store.initialize()
    return _store

"""
Investment Journal — append-only log of all investment conversations.

Tracks analysis sessions, trade decisions, research notes,
and periodic reviews with Drive sync.
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

SCRIPT_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # Helper/
LOG_DIR = PROJECT_ROOT / 'logs'
LOCAL_FILE = LOG_DIR / 'investment_journal.json'
CONFIG_FILE = PROJECT_ROOT / 'config' / 'portfolio_config.json'

import sys
sys.path.insert(0, str(PROJECT_ROOT))
from bcs import drive_store


def _load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    with open(CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials(config: dict) -> Optional[Path]:
    env_path = os.environ.get('PORTFOLIO_GOOGLE_CREDS') or os.environ.get('BCS_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)
    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')
    return Path(path_str) if path_str else None


class InvestmentJournal:
    """Append-only investment journal with Drive sync."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._entries: list = []
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
            creds_path = _resolve_credentials(self._config)
            if creds_path and creds_path.exists():
                try:
                    self._drive_service = drive_store.get_drive_service(creds_path)
                    folder_id = drive_cfg['folder_id']
                    file_name = drive_cfg.get('journal_file', 'investment_journal.json')
                    self._drive_file_id = drive_store.find_file(
                        self._drive_service, folder_id, file_name
                    )
                    self._drive_enabled = True
                except Exception as e:
                    logger.warning("Journal Drive init failed: %s", e)

        if self._drive_enabled and self._drive_file_id:
            try:
                drive_data = drive_store.download_json(
                    self._drive_service, self._drive_file_id
                )
                local_data = self._read_local()
                self._entries = self._merge(local_data, drive_data)
                self._save_local()
                self._last_sync_time = time.time()
            except Exception as e:
                logger.warning("Journal Drive download failed: %s", e)
                self._entries = self._read_local()
        else:
            self._entries = self._read_local()

        logger.info("Journal: %d entries, drive=%s",
                     len(self._entries),
                     'enabled' if self._drive_enabled else 'disabled')

    def _read_local(self) -> list:
        if not LOCAL_FILE.exists():
            return []
        try:
            with open(LOCAL_FILE) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            return []

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        by_id = {}
        for e in base:
            by_id[e['id']] = e
        for e in incoming:
            eid = e['id']
            if eid not in by_id:
                by_id[eid] = e
            elif e.get('version', 0) > by_id[eid].get('version', 0):
                by_id[eid] = e
        return sorted(by_id.values(), key=lambda e: e['id'])

    def _save_local(self):
        LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(LOG_DIR), suffix='.tmp', prefix='journal_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._entries, f, indent=2, default=str)
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

    def _upload_to_drive(self):
        if not self._drive_enabled:
            return
        try:
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('journal_file', 'investment_journal.json')
            self._drive_file_id = drive_store.upload_json(
                self._drive_service, folder_id, file_name,
                self._entries, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Journal Drive upload failed: %s", e)

    def next_id(self) -> int:
        if not self._entries:
            return 1
        return max(e['id'] for e in self._entries) + 1

    # ── CRUD ──────────────────────────────────────────────────────────────

    def add_entry(self, data: dict) -> dict:
        """Add a journal entry.

        Required: type, summary
        Optional: symbols, decision_ids, details, tags
        """
        if 'summary' not in data:
            raise ValueError("Journal entry must have 'summary'")

        data['id'] = self.next_id()
        data['version'] = 1
        data.setdefault('date', datetime.now().strftime('%Y-%m-%d'))
        data.setdefault('type', 'analysis')  # analysis, trade, review, research, decision
        data.setdefault('account_id', None)  # e.g. "QSK814" or "YL6478"
        data.setdefault('symbols', [])
        data.setdefault('decision_ids', [])
        data.setdefault('details', '')
        data.setdefault('tags', [])
        data.setdefault('outcome_date', None)
        data.setdefault('outcome', None)

        self._entries.append(data)
        self._save_local()
        self._upload_to_drive()
        logger.info("Journal #%d: %s — %s", data['id'], data['type'],
                     data['summary'][:50])
        return data

    def add_outcome(self, entry_id: int, outcome: str) -> Optional[dict]:
        """Record what actually happened for a journal entry."""
        for e in self._entries:
            if e['id'] == entry_id:
                e['outcome'] = outcome
                e['outcome_date'] = datetime.now().strftime('%Y-%m-%d')
                e['version'] = e.get('version', 0) + 1
                self._save_local()
                self._upload_to_drive()
                return e
        return None

    def link_decision(self, entry_id: int, decision_id: int):
        """Link a journal entry to a decision tracker entry."""
        for e in self._entries:
            if e['id'] == entry_id:
                if decision_id not in e.get('decision_ids', []):
                    e.setdefault('decision_ids', []).append(decision_id)
                    e['version'] = e.get('version', 0) + 1
                    self._save_local()
                    self._upload_to_drive()
                return

    # ── Queries ───────────────────────────────────────────────────────────

    def search(self, symbol: str = None, tag: str = None,
               entry_type: str = None, account_id: str = None,
               date_from: str = None, date_to: str = None) -> list:
        """Search journal entries."""
        results = self._entries
        if account_id:
            results = [e for e in results if e.get('account_id') == account_id]
        if symbol:
            s = symbol.upper()
            results = [e for e in results if s in [x.upper() for x in e.get('symbols', [])]]
        if tag:
            results = [e for e in results if tag in e.get('tags', [])]
        if entry_type:
            results = [e for e in results if e.get('type') == entry_type]
        if date_from:
            results = [e for e in results if e.get('date', '') >= date_from]
        if date_to:
            results = [e for e in results if e.get('date', '') <= date_to]
        return results

    def list_entries(self, limit: int = 20):
        """Print recent journal entries."""
        entries = self._entries[-limit:]
        if not entries:
            print("No journal entries.")
            return

        print(f"\n{'ID':>3}  {'Date':<12} {'Type':<10} {'Symbols':<20} {'Summary'}")
        print("-" * 90)
        for e in entries:
            symbols = ', '.join(e.get('symbols', [])[:3])
            summary = e.get('summary', '')[:40]
            print(f"{e['id']:>3}  {e.get('date', '?'):<12} "
                  f"{e.get('type', '?'):<10} {symbols:<20} {summary}")

    def maybe_sync(self, force: bool = False):
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            if self._drive_file_id:
                try:
                    drive_data = drive_store.download_json(
                        self._drive_service, self._drive_file_id
                    )
                    self._entries = self._merge(self._entries, drive_data)
                    self._save_local()
                    self._last_sync_time = time.time()
                except Exception as e:
                    logger.warning("Journal sync failed: %s", e)


# ── Singleton ────────────────────────────────────────────────────────────────

_journal: Optional[InvestmentJournal] = None


def get_journal() -> InvestmentJournal:
    global _journal
    if _journal is None:
        _journal = InvestmentJournal()
        _journal.initialize()
    return _journal

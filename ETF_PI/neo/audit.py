"""
neo.audit — Trade audit log (JSON lines).

Appends one JSON object per line to logs/neo_trades_{date}.jsonl.
"""

import os
import json
import datetime
import pytz

IST = pytz.timezone('Asia/Kolkata')


class AuditLog:
    """Append-only JSON-lines trade audit log."""

    def __init__(self, log_dir):
        self._log_dir = log_dir
        os.makedirs(self._log_dir, exist_ok=True)

    def log(self, action, data=None):
        """Append an action record.

        Args:
            action: String label (e.g., 'ENTRY_SIGNAL', 'EXIT_TP', 'CANCEL').
            data: Dict of extra fields.
        """
        today = datetime.datetime.now(IST).strftime('%Y-%m-%d')
        path = os.path.join(self._log_dir, f'neo_trades_{today}.jsonl')
        entry = {
            'ts': datetime.datetime.now(IST).isoformat(),
            'action': action,
            **(data or {}),
        }
        with open(path, 'a') as f:
            f.write(json.dumps(entry) + '\n')

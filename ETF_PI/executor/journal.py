"""Append-only, fsync'd journal = the live book's source of truth (spec §3, R5, F23).

Every line: {"ts", "ev", "key"?, "pos"?, ...}. A line that carries `pos` holds the
FULL position dict after the change, so a rebuild is "last pos per key" -- no event
replay logic that could drift from the engine.

Rebuild rules (Fable F23):
  * an unparsable FINAL line is a torn write (power cut mid-append): ignored, logged;
  * an unparsable line in the MIDDLE is corruption: rebuild continues from the good
    lines (exits must go on) but `corrupt` is set -> the engine allows NO new entries
    and alerts until the owner repairs the file.
"""
from __future__ import annotations

import io
import json
import os
from typing import Any, Dict, List, Optional, Tuple


class Journal:
    def __init__(self, path: str):
        self.path = path
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)

    def append(self, row: Dict[str, Any]) -> None:
        """One line, flushed and fsync'd before returning (the tag of an order is
        on disk BEFORE the order is sent)."""
        line = json.dumps(row, sort_keys=True, default=str, separators=(',', ':'))
        if '\n' in line:
            raise ValueError('journal row would span lines')
        with io.open(self.path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(line + '\n')
            f.flush()
            os.fsync(f.fileno())

    def load(self) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], Optional[str]]:
        """(positions by key, all good rows, corruption note or None)."""
        positions: Dict[str, Dict[str, Any]] = {}
        rows: List[Dict[str, Any]] = []
        note = None
        if not os.path.exists(self.path):
            return positions, rows, None
        with io.open(self.path, encoding='utf-8', newline='\n') as f:
            lines = f.read().split('\n')
        if lines and lines[-1] == '':
            lines.pop()
        for i, ln in enumerate(lines):
            try:
                row = json.loads(ln)
                if not isinstance(row, dict):
                    raise ValueError('not an object')
            except ValueError as e:
                if i == len(lines) - 1:
                    note_tail = 'torn final line %d ignored (%s)' % (i + 1, e)
                    rows.append({'ev': '_TORN', 'line': i + 1, 'note': note_tail})
                    continue
                note = 'corrupt journal line %d of %d (%s)' % (i + 1, len(lines), e)
                continue
            rows.append(row)
            if isinstance(row.get('pos'), dict) and row.get('key'):
                positions[str(row['key'])] = row['pos']
        return positions, rows, note

    def keys_seen(self, rows: List[Dict[str, Any]]) -> set:
        """Intent keys already processed (I2)."""
        return {str(r['key']) for r in rows if r.get('key')}

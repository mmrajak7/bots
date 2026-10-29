"""The paper book: one JSON file, written atomically, never silently reset.

Layout of `state/book.json`:
    {
      "version": 1,
      "next_id": 7,
      "days": {"2026-10-29": {...marks, flags...}},
      "positions": [{...}, ...]          # open AND closed, never deleted
    }

Plus `state/events_YYYYMMDD.jsonl`: one line per MARK / ENTRY / EXIT / DEFER /
MISSED / ERROR event and a sampled POLL line — the evidence of what the
engine saw, because an option book cannot be reconstructed after the fact.

A book that fails to parse is NOT replaced with an empty one: that would
orphan every open position. `load` raises `CorruptBook`, the caller alerts
and stops, and the file stays where it is for a human.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class CorruptBook(RuntimeError):
    pass


def _empty() -> Dict[str, Any]:
    return {'version': 1, 'next_id': 1, 'days': {}, 'positions': []}


class Store:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / 'book.json'
        self.data = self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return _empty()
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            raise CorruptBook('%s does not parse (%s). It has NOT been '
                              'replaced; fix or restore it by hand.'
                              % (self.path, exc))
        for k in ('next_id', 'days', 'positions'):
            if k not in data:
                raise CorruptBook('%s has no %r key' % (self.path, k))
        return data

    def save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix='.book.',
                                   suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=1, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- days --------------------------------------------------------
    def day(self, d: date) -> Dict[str, Any]:
        key = d.isoformat()
        if key not in self.data['days']:
            self.data['days'][key] = {'marks': {}, 'fired': {},
                                      'missed': {}, 'flags': []}
        return self.data['days'][key]

    # ---- positions ---------------------------------------------------
    def open_positions(self) -> List[Dict[str, Any]]:
        return [p for p in self.data['positions'] if p['status'] == 'open']

    def closed_positions(self) -> List[Dict[str, Any]]:
        return [p for p in self.data['positions'] if p['status'] == 'closed']

    def add_position(self, pos: Dict[str, Any]) -> Dict[str, Any]:
        pos = dict(pos)
        pos['id'] = self.data['next_id']
        self.data['next_id'] += 1
        self.data['positions'].append(pos)
        return pos

    # ---- evidence ----------------------------------------------------
    def event(self, when: datetime, kind: str, **fields: Any) -> None:
        line = {'t': when.isoformat(timespec='seconds'), 'kind': kind}
        line.update(fields)
        path = self.dir / ('events_%s.jsonl' % when.strftime('%Y%m%d'))
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(line, sort_keys=True, default=str) + '\n')

    def heartbeat(self, when: datetime, **fields: Any) -> None:
        hb = {'t': when.isoformat(timespec='seconds')}
        hb.update(fields)
        path = self.dir / 'heartbeat.json'
        tmp = self.dir / '.heartbeat.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(hb, f, default=str)
        os.replace(tmp, path)

    def read_heartbeat(self) -> Optional[Dict[str, Any]]:
        path = self.dir / 'heartbeat.json'
        if not path.exists():
            return None
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

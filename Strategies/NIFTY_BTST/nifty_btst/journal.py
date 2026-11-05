"""Intent journal: the order is written down BEFORE it is sent, fsync'd.

NEO's tag (`ig` -> `GuiOrdId`) is a correlation id, not an idempotency key:
the broker will happily accept the same tag twice. So the client must be the
one that refuses to resend, and it can only do that if the intent survived the
crash. One JSONL file, append-only, one line per event:

  INTENT      tag, kind (ENTRY|EXIT), symbol, side, qty, price    - before the send
  PLACED      tag, order_no                                        - clear answer
  NOT_PLACED  tag, why                                             - clear refusal, or
                                                                     searched twice and absent
  BOUND       tag, order_no, how (tag|criteria)                    - found in the book later

An INTENT with no PLACED / NOT_PLACED / BOUND after it is UNRESOLVED: a send
whose fate is unknown. On start the engine resolves every one of those against
the broker's book before it sends anything. The file is also the per-day order
counter behind the ceilings, which is why it is read, not trusted from memory.

Every line is written with flush + fsync: the Pi loses power.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


def make_tag(day: date, pos_id: int, kind: str, attempt: int) -> str:
    """`<yyyymmdd>-<id>-<ENTRY|EXIT>-<attempt>`: unique per send, short, and
    readable in the broker's order book."""
    if kind not in ('ENTRY', 'EXIT'):
        raise ValueError('kind must be ENTRY or EXIT')
    return '%s-%d-%s-%d' % (day.strftime('%Y%m%d'), pos_id, kind, attempt)


def parse_tag(tag: str) -> Optional[Dict[str, Any]]:
    parts = str(tag).split('-')
    if len(parts) != 4 or parts[2] not in ('ENTRY', 'EXIT'):
        return None
    try:
        return {'day': datetime.strptime(parts[0], '%Y%m%d').date(),
                'id': int(parts[1]), 'kind': parts[2], 'attempt': int(parts[3])}
    except ValueError:
        return None


class Journal:
    RESOLVING = ('PLACED', 'NOT_PLACED', 'BOUND')

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / 'intents.jsonl'
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, line: Dict[str, Any]) -> None:
        with open(self.path, 'a+', encoding='utf-8') as f:
            # A torn last line (power cut mid-write) has no newline; without
            # this the next record would be glued to it and lost (NEO-28).
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(f.tell() - 1)
                if f.read(1) != '\n':
                    f.write('\n')
            f.write(json.dumps(line, sort_keys=True, default=str) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def intent(self, when: datetime, tag: str, kind: str, pos_id: int, symbol: str,
               side: str, qty: int, price: str, **extra: Any) -> None:
        line = {'t': when.isoformat(timespec='seconds'), 'ev': 'INTENT', 'tag': tag,
                'kind': kind, 'id': pos_id, 'symbol': symbol, 'side': side,
                'qty': qty, 'price': price}
        line.update(extra)
        self._write(line)

    def placed(self, when: datetime, tag: str, order_no: str) -> None:
        self._write({'t': when.isoformat(timespec='seconds'), 'ev': 'PLACED',
                     'tag': tag, 'order_no': str(order_no)})

    def not_placed(self, when: datetime, tag: str, why: str) -> None:
        self._write({'t': when.isoformat(timespec='seconds'), 'ev': 'NOT_PLACED',
                     'tag': tag, 'why': why})

    def bound(self, when: datetime, tag: str, order_no: str, how: str) -> None:
        self._write({'t': when.isoformat(timespec='seconds'), 'ev': 'BOUND',
                     'tag': tag, 'order_no': str(order_no), 'how': how})

    def lines(self) -> List[Dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding='utf-8') as f:
            for n, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    # A torn last line after a power cut is expected; a torn
                    # line in the middle is not, but neither may hide an
                    # unresolved intent above it.
                    out.append({'ev': 'TORN', 'line_no': n})
        return out

    def unresolved(self) -> List[Dict[str, Any]]:
        """INTENT lines with no resolving line after them, oldest first."""
        resolved = set()
        intents: Dict[str, Dict[str, Any]] = {}
        for line in self.lines():
            ev, tag = line.get('ev'), line.get('tag')
            if ev == 'INTENT' and tag:
                intents[tag] = line
            elif ev in self.RESOLVING and tag:
                resolved.add(tag)
        return [v for k, v in intents.items() if k not in resolved]

    def sends_today(self, day: date, kind: Optional[str] = None,
                    include_probe: bool = False) -> int:
        """Orders SENT today (intents, resolved or not): the ceiling counter.
        Probe sends (`probe: true`) are excluded unless asked for, so a Stage-3
        probe never eats the strategy's budget (NEO-21)."""
        prefix = day.strftime('%Y%m%d') + '-'
        n = 0
        for line in self.lines():
            if line.get('ev') != 'INTENT' or not str(line.get('tag', '')).startswith(prefix):
                continue
            if line.get('probe') and not include_probe:
                continue
            if kind is None or line.get('kind') == kind:
                n += 1
        return n

    def owner(self, when: datetime, pos_id: int, action: str, **extra: Any) -> None:
        """An owner command (confirm held / book external): journalled with
        the same durability as a send, because it changes what may be sold."""
        line = {'t': when.isoformat(timespec='seconds'), 'ev': 'OWNER', 'id': pos_id,
                'action': action}
        line.update(extra)
        self._write(line)

    def probe_held(self, when: datetime, symbol: str, qty: int) -> None:
        """The order probe's `hold` step filled: the lot it carries overnight."""
        self._write({'t': when.isoformat(timespec='seconds'), 'ev': 'PROBE_HELD',
                     'symbol': symbol, 'qty': qty})

    def probe_sold(self, when: datetime, symbol: str, qty: int) -> None:
        """A probe SELL FILLED. A placement alone is not a sale (R3-27)."""
        self._write({'t': when.isoformat(timespec='seconds'), 'ev': 'PROBE_SOLD',
                     'symbol': symbol, 'qty': qty})

    def probe_open_symbols(self) -> List[str]:
        """Symbols the probe journalled as held and has not since journalled a
        FILLED sale for."""
        held: Dict[str, str] = {}
        sold: Dict[str, str] = {}
        for ln in self.lines():
            if ln.get('ev') == 'PROBE_HELD' and ln.get('symbol'):
                held[str(ln['symbol'])] = str(ln.get('t', ''))
            elif ln.get('ev') == 'PROBE_SOLD' and ln.get('symbol'):
                sold[str(ln['symbol'])] = str(ln.get('t', ''))
        return [s for s, t in held.items() if sold.get(s, '') < t]

    def probe_hold_open(self, symbol: str) -> bool:
        return symbol in self.probe_open_symbols()

    def not_placed_tags(self) -> List[str]:
        """Tags once concluded NOT placed. If one of these ever appears in the
        broker's book, the conclusion was wrong (a late arrival) and the kill
        must trip (NEO-01)."""
        return [str(ln['tag']) for ln in self.lines() if ln.get('ev') == 'NOT_PLACED' and ln.get('tag')]

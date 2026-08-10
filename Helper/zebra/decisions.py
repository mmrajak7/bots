"""Decision journal for the Claude vetting layer.

Every entry/exit judgement Claude makes is recorded here with the evidence it
saw, then joined to what actually happened. That join is the whole point: the
journal exists so future decisions can be informed by scored past ones, and so
the veto layer can be measured rather than trusted.

Two stores, deliberately
------------------------
- `zebra_trades.json`  — OPERATIONAL truth. The verdict that the monitor acts
  on lives on the trade record itself (`trade['vet']`), so the process reading
  it needs exactly one file and one lock. No cross-file atomicity problem.
- `zebra_decisions.json` (this module) — AUDIT history. Append-mostly, never
  read for control flow, carries the full reasoning and evidence payload that
  would bloat the trade store.

They use SEPARATE lock files on purpose. Sharing one lock would deadlock the
moment a caller held the trade lock and tried to journal inside it (POSIX flock
is per-fd, so a second acquire in the same process blocks until timeout).
Callers must therefore never nest the two — write the decision, then update the
trade, each under its own lock.

Scoring
-------
`score()` answers the question that decides whether this layer ever earns live
authority: **of the entries Claude vetoed, how many actually went on to lose?**
A veto with no outcome is unfalsifiable, which is why vetoed signals are still
tracked to their conclusion in paper (see tasks/todo.md).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from typing import Optional

from . import config as cfg
from .filelock import exclusive

logger = logging.getLogger(__name__)

# Valid verdicts. `unavailable` is not a judgement — it records that Claude
# could not be reached, so those rows are EXCLUDED from scoring rather than
# counted as allows (which would silently flatter the layer's precision).
VERDICTS = ('allow', 'veto', 'exit', 'defer', 'unavailable')
KINDS = ('entry', 'exit')


class DecisionStore:
    """Append-mostly journal of Claude's vetting decisions."""

    def __init__(self, path=None, lock_path=None):
        self._path = path or cfg.DECISIONS_FILE
        self._lock = lock_path or cfg.DECISIONS_LOCK
        self._rows: list = []

    def initialize(self):
        with exclusive(self._lock):
            self._rows = self._read()
        logger.info("DecisionStore: %d decisions", len(self._rows))
        return self

    # ── reads (in-memory; refresh() for a fresh snapshot) ─────────────────
    def all(self) -> list:
        return self._rows

    def refresh(self) -> list:
        with exclusive(self._lock):
            self._rows = self._read()
        return self._rows

    def find(self, decision_id: int) -> Optional[dict]:
        return next((d for d in self._rows if d.get('id') == decision_id), None)

    def for_trade(self, trade_id: int, kind: Optional[str] = None) -> list:
        out = [d for d in self._rows
               if trade_id in (d.get('signal_ref') or {}).get('trade_ids', [])]
        return [d for d in out if d.get('kind') == kind] if kind else out

    def pending_outcome(self) -> list:
        """Decisions still waiting to be scored."""
        return [d for d in self._rows
                if d.get('outcome') is None and d.get('verdict') != 'unavailable']

    # ── writes ────────────────────────────────────────────────────────────
    @contextmanager
    def _mutate(self):
        """Same discipline as ZebraStore: lock, re-read disk, mutate, save.

        Rolls back in-memory state on a caller exception so a half-written
        decision never lingers and get-by-id never returns a phantom row.
        """
        with exclusive(self._lock):
            self._rows = self._merge(self._read(), self._rows)
            snapshot = [dict(r) for r in self._rows]
            try:
                yield
            except BaseException:
                self._rows = snapshot
                raise
            self._save()

    def record(self, kind: str, verdict: str, trade_ids: list,
               stock: str, direction: str, reasons=None, red_flags=None,
               evidence=None, confidence=None, model=None,
               tokens=None, latency_ms=None, notes: str = '') -> dict:
        """Append one decision. Returns the stored row (with its id)."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
        if confidence is not None and not 0.0 <= float(confidence) <= 1.0:
            raise ValueError(f"confidence must be 0..1, got {confidence!r}")

        with self._mutate():
            row = {
                'id': self._next_id(),
                'version': 1,
                'created_at': datetime.now().isoformat(),
                'kind': kind,
                'verdict': verdict,
                'confidence': confidence,
                'signal_ref': {
                    # Both A/B arms share one verdict, so one decision points at
                    # both trade ids — that is what keeps the zebra-vs-BCS
                    # structure comparison unconfounded by the vetting layer.
                    'trade_ids': list(trade_ids),
                    'stock': stock,
                    'direction': direction,
                },
                'reasons': list(reasons or []),
                'red_flags': list(red_flags or []),
                'evidence': dict(evidence or {}),
                'model': model,
                'agent_tokens': tokens,
                'latency_ms': latency_ms,
                'notes': notes,
                'acted': False,
                'acted_at': None,
                'outcome': None,
                'outcome_at': None,
            }
            self._rows.append(row)
        logger.info("DECISION #%d %s %s %s (%s) flags=%s",
                    row['id'], kind, verdict, stock, direction,
                    row['red_flags'] or 'none')
        return row

    def mark_acted(self, decision_id: int) -> dict:
        """Record that the bot actually applied this verdict."""
        with self._mutate():
            d = self._must_find(decision_id)
            d['acted'] = True
            d['acted_at'] = datetime.now().isoformat()
            d['version'] = d.get('version', 0) + 1
            return d

    def set_outcome(self, decision_id: int, outcome: dict) -> dict:
        """Join the result back. `outcome` is free-form but should carry at
        least pnl / pnl_pct / exit_reason so score() can judge the call."""
        with self._mutate():
            d = self._must_find(decision_id)
            d['outcome'] = dict(outcome)
            d['outcome_at'] = datetime.now().isoformat()
            d['version'] = d.get('version', 0) + 1
            return d

    # ── scoring — the reason the journal exists ──────────────────────────
    def score(self, kind: str = 'entry') -> dict:
        """Was the vetting layer right?

        A veto is CORRECT when the trade it blocked went on to lose, and WRONG
        when it would have won. An allow is the mirror. `unavailable` rows are
        excluded — they are outages, not judgements, and counting them as
        allows would flatter the layer.
        """
        rows = [d for d in self._rows
                if d.get('kind') == kind
                and d.get('verdict') in ('allow', 'veto')
                and isinstance(d.get('outcome'), dict)
                and d['outcome'].get('pnl') is not None]
        out = {'scored': len(rows), 'veto': {}, 'allow': {}}
        for verdict in ('veto', 'allow'):
            sub = [d for d in rows if d['verdict'] == verdict]
            if not sub:
                out[verdict] = {'n': 0}
                continue
            losers = [d for d in sub if float(d['outcome']['pnl']) <= 0]
            # For a veto, blocking a loser is a hit. For an allow, a winner is.
            hits = len(losers) if verdict == 'veto' else len(sub) - len(losers)
            out[verdict] = {
                'n': len(sub),
                'correct': hits,
                'precision': round(hits / len(sub), 3),
                'pnl_avoided' if verdict == 'veto' else 'pnl_captured':
                    round(sum(-float(d['outcome']['pnl']) if verdict == 'veto'
                              else float(d['outcome']['pnl']) for d in sub), 2),
            }
        return out

    # ── internals ─────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        return max((d.get('id', 0) for d in self._rows), default=0) + 1

    def _must_find(self, decision_id: int) -> dict:
        d = self.find(decision_id)
        if not d:
            raise ValueError(f"decision #{decision_id} not found")
        return d

    @staticmethod
    def _merge(base: list, incoming: list) -> list:
        by_id = {d['id']: d for d in base}
        for d in incoming:
            if d['id'] not in by_id or \
                    d.get('version', 0) > by_id[d['id']].get('version', 0):
                by_id[d['id']] = d
        return sorted(by_id.values(), key=lambda d: d['id'])

    def _read(self) -> list:
        if not self._path.exists():
            return []
        try:
            with open(self._path) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return data
        except (json.JSONDecodeError, ValueError) as e:
            backup = self._path.with_suffix(f'.corrupt.{int(time.time())}.json')
            try:
                self._path.rename(backup)
            except OSError:
                pass
            logger.critical("Decisions file CORRUPT (%s). Backed up to %s.",
                            e, backup)
            return []

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent),
                                   suffix='.tmp', prefix='zdec_')
        try:
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._rows, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):
                try:
                    os.replace(tmp, str(self._path))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
            tmp = None
        finally:
            if fd is not None:
                os.close(fd)
            if tmp and os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass


_store: Optional[DecisionStore] = None


def get_store() -> DecisionStore:
    global _store
    if _store is None:
        _store = DecisionStore().initialize()
    return _store

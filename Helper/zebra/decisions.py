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
from common.filelock import exclusive

logger = logging.getLogger(__name__)

# Valid verdicts. `unavailable` is not a judgement — it records that Claude
# could not be reached, so those rows are EXCLUDED from scoring rather than
# counted as allows (which would silently flatter the layer's precision).
VERDICTS = ('allow', 'veto', 'exit', 'defer', 'unavailable',
            # Position-review recommendations. `exit` is shared with the exit
            # kind, and means the same thing in both: get out. It is still only
            # ever a RECOMMENDATION here — review.py cannot close a position.
            'hold', 'adjust')
# `scan` is the DAILY end-of-session news sweep — same verdict vocabulary as
# `review` and the same inability to close anything, but a different question,
# a different (cheaper) model, and ~8 rows a session against a handful. Rolled
# into `review` it would swamp the price-triggered rows it must be judged
# separately from, and the model column would say Opus on every one of them.
# Additive: nothing selects `review` rows, so no existing reader changes.
KINDS = ('entry', 'exit', 'review', 'scan')


def _load_config() -> dict:
    """Same zebra_config.json the trade store reads, for the Drive block."""
    try:
        with open(cfg.CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


class DecisionStore:
    """Append-mostly journal of Claude's vetting decisions."""

    def __init__(self, path=None, lock_path=None, config=None,
                 drive: bool = True):
        self._path = path or cfg.DECISIONS_FILE
        self._lock = lock_path or cfg.DECISIONS_LOCK
        self._rows: list = []
        # Drive is OFF for any store pointed at a non-default path — that is
        # what every test constructs, and a test must never touch the real
        # Drive file. Production goes through get_store(), which passes none of
        # these and gets the default path plus sync.
        self._drive_wanted = drive and path is None
        self._config = config or _load_config()
        self._drive_service = None
        self._drive_file_id = None
        self._drive_enabled = False
        self._last_sync_time = 0.0

    def initialize(self):
        drive_cfg = self._config.get('google_drive', {})
        if self._drive_wanted and drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)
        if self._drive_enabled:
            self._sync_from_drive()
        else:
            with exclusive(self._lock):
                self._rows = self._read()
        logger.info("DecisionStore: %d decisions, drive=%s", len(self._rows),
                    'enabled' if self._drive_enabled else 'disabled')
        return self

    # ── reads (in-memory; refresh() for a fresh snapshot) ─────────────────
    def all(self) -> list:
        return self._rows

    def refresh(self, force_drive: bool = False) -> list:
        """Re-read the journal. Pulls from Drive on the sync interval.

        The journal is written by TWO processes on the Pi (the monitor and each
        spawned CLI agent) and read from a third machine, so a local-only read
        can be stale in exactly the window where the join runs.
        """
        interval = self._config.get('google_drive', {}).get(
            'sync_interval_sec', 300)
        if self._drive_enabled and (
                force_drive or time.time() - self._last_sync_time >= interval):
            self._sync_from_drive()
            return self._rows
        with exclusive(self._lock):
            self._rows = self._read()
        return self._rows

    def find(self, decision_id: int) -> Optional[dict]:
        return next((d for d in self._rows if d.get('id') == decision_id), None)

    def for_trade(self, trade_id: int, kind: Optional[str] = None) -> list:
        out = [d for d in self._rows
               if trade_id in (d.get('signal_ref') or {}).get('trade_ids', [])]
        return [d for d in out if d.get('kind') == kind] if kind else out

    def pending_outcome(self, kind: Optional[str] = None) -> list:
        """Decisions still waiting to be scored.

        `kind` matters for cost: only ENTRY decisions are ever joined to an
        outcome, so an unfiltered call hands the joiner every exit and review
        row ever written, forever, on a five-minute cycle. Rows missing an `id`
        are dropped here rather than downstream, so one hand-mangled row cannot
        stall the whole channel.

        **Only ACTED decisions are joinable.** A journalled verdict that the
        trade store discarded (arrived after the deadline, duplicate from a
        retried CLI, signal already settled) never influenced anything, so
        joining it would attribute a trade's P&L to a judgement that did not
        cause it. Worse, a discarded VETO has no shadow and no position, so it
        can never settle — leaving it here means re-scanning it every five
        minutes for the life of the journal.
        """
        return [d for d in self._rows
                if d.get('id') is not None
                and d.get('acted') is True
                and d.get('outcome') is None
                and d.get('verdict') != 'unavailable'
                and (kind is None or d.get('kind') == kind)]

    # ── writes ────────────────────────────────────────────────────────────
    @contextmanager
    def _mutate(self):
        """Same discipline as ZebraStore: lock, re-read disk, mutate, save.

        Rolls back in-memory state on a caller exception so a half-written
        decision never lingers and get-by-id never returns a phantom row.

        The Drive upload happens AFTER the lock is released — it is a network
        call, and holding a cross-process lock across it would stall the other
        process's whole cycle behind a Drive timeout.
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
        self._upload_to_drive()

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

        # Drive-first before allocating an id. Ids come from max(id)+1, so two
        # machines writing against stale copies would both mint the same id and
        # the union-merge — which keys on id — would silently drop one agent's
        # reasoning. A decision is written a handful of times a day; one
        # download to make the id safe is free at that rate.
        if self._drive_enabled:
            self._sync_from_drive()

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
        """Record that the bot actually applied this verdict.

        The gate on every score. A decision is only evidence about the layer if
        the layer's verdict is what happened — see `pending_outcome`.
        """
        with self._mutate():
            d = self._must_find(decision_id)
            d['acted'] = True
            d['acted_at'] = datetime.now().isoformat()
            d['version'] = d.get('version', 0) + 1
            return d

    def mark_discarded(self, decision_id: int, reason: str) -> dict:
        """Record that the trade store REFUSED this verdict, and why.

        The row stays in the journal — an agent's reasoning is worth keeping
        even when it arrived too late to matter, and deleting it would hide how
        often the deadline is missed. It is simply never scored.
        """
        with self._mutate():
            d = self._must_find(decision_id)
            d['acted'] = False
            d['discarded_reason'] = str(reason)
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
        allows would flatter the layer. So are un-ACTED rows: a verdict the
        store discarded did not cause the outcome, and crediting the layer for
        a trade that entered unvetted is the same flattery by another route.
        """
        rows = [d for d in self._rows
                if d.get('kind') == kind
                and d.get('acted') is True
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
        out['signal_quality'] = self.score_signal_quality(kind)
        return out

    def score_signal_quality(self, kind: str = 'entry') -> dict:
        """The comparable view: did price go where the strategy said?

        The money score above can only judge ALLOWS — a vetoed structure was
        never priced, so it has no P&L and is invisible there. That leaves the
        layer's most important decisions unscored. This scores both arms on the
        one basis they share: a `hit`/`miss`/`flat` label (see outcomes.py).

        Explicitly NOT a P&L claim. A veto that blocked a `miss` was right about
        DIRECTION; how much money it saved is unknown and deliberately not
        guessed at. `flat` rows are excluded from precision — an unresolved
        signal is not evidence either way — but reported, because a layer that
        mostly blocks signals that go nowhere is telling you something too.
        """
        rows = [d for d in self._rows
                if d.get('kind') == kind
                and d.get('acted') is True
                and d.get('verdict') in ('allow', 'veto')
                and isinstance(d.get('outcome'), dict)
                and d['outcome'].get('label') in ('hit', 'miss', 'flat')]
        out = {'labelled': len(rows)}
        for verdict in ('veto', 'allow'):
            sub = [d for d in rows if d['verdict'] == verdict]
            decisive = [d for d in sub if d['outcome']['label'] != 'flat']
            # A veto is right when the signal it blocked went on to MISS; an
            # allow is right when the signal it let through HIT.
            want = 'miss' if verdict == 'veto' else 'hit'
            hits = [d for d in decisive if d['outcome']['label'] == want]
            out[verdict] = {
                'n': len(sub),
                'decisive': len(decisive),
                'flat': len(sub) - len(decisive),
                'correct': len(hits),
                'precision': (round(len(hits) / len(decisive), 3)
                              if decisive else None),
            }
        return out

    # ── Drive ─────────────────────────────────────────────────────────────
    # The journal is the layer's ONLY evidence base — the record that decides
    # whether Claude vetting ever earns live authority. It lived on the Pi's SD
    # card alone, which is the one component in this fleet with a documented
    # habit of dying, and it was invisible from the machine the user actually
    # reads reports on. Same local-first, Drive-secondary pattern as every
    # other store here: Drive never gates a write, and a Drive outage degrades
    # to exactly the local-only behaviour this had before.
    def _drive_name(self) -> str:
        return self._config.get('google_drive', {}).get(
            'decisions_file_name', 'zebra_decisions.json')

    def _init_drive(self, drive_cfg: dict):
        from .trade_store import _resolve_credentials_path
        creds = _resolve_credentials_path(self._config)
        if not creds or not creds.exists():
            logger.warning("Drive credentials not found at %s — decision "
                           "journal is local-only", creds)
            return
        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds)
            self._drive_file_id = find_file(self._drive_service,
                                            drive_cfg['folder_id'],
                                            self._drive_name())
            self._drive_enabled = True
        except Exception as e:
            logger.warning("Decision-journal Drive init failed: %s. "
                           "Local-only.", e)

    def _resolve_remote_id(self):
        """Look the remote file up again if we do not have its id yet.

        Not a formality: the journal does not exist on Drive until the first
        write, so every process that starts before that holds file_id=None. If
        that were sticky, such a process would keep taking the 'nothing on
        Drive' branch forever — never seeing another machine's rows, and
        re-minting ids that already exist. Caught by a negative control.
        """
        if self._drive_file_id:
            return self._drive_file_id
        try:
            from bcs.drive_store import find_file
            self._drive_file_id = find_file(
                self._drive_service,
                self._config['google_drive']['folder_id'], self._drive_name())
        except Exception as e:
            logger.debug('decision journal lookup failed: %s', e)
        return self._drive_file_id

    def _sync_from_drive(self):
        """Pull, union-merge, persist. Network call outside the lock."""
        try:
            from bcs.drive_store import download_json
            if not self._resolve_remote_id():
                logger.info("No decision journal on Drive yet — will create on "
                            "the first write")
                with exclusive(self._lock):
                    self._rows = self._read()
                return
            remote = download_json(self._drive_service, self._drive_file_id)
            # The merge+save is a read-modify-write on the shared file, so it
            # holds the lock; the download above deliberately does not.
            with exclusive(self._lock):
                base = self._merge(self._read(), self._rows)
                self._rows = self._merge(base, remote or [])
                self._save()
            self._last_sync_time = time.time()
        except Exception as e:
            logger.warning("Decision-journal Drive sync failed: %s. Using "
                           "local.", e)
            try:
                with exclusive(self._lock):
                    self._rows = self._read()
            except Exception:                    # pragma: no cover - paranoia
                pass

    def _upload_to_drive(self):
        if not self._drive_enabled:
            return
        try:
            from bcs.drive_store import upload_json
            self._drive_file_id = upload_json(
                self._drive_service,
                self._config['google_drive']['folder_id'],
                self._drive_name(), self._rows, self._drive_file_id)
            self._last_sync_time = time.time()
        except Exception as e:
            # Local is already written and is the operational truth; Drive is
            # durability. Never raise — an agent must not think its verdict
            # failed to land because a network call did.
            logger.error("Decision-journal Drive upload failed: %s. Local is "
                         "safe.", e)

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
        """Higher version wins, per id.

        ID-LESS ROWS ARE SKIPPED, NOT FATAL (fixed 2026-09-01). This indexed
        `d['id']` directly, so ONE row without an id -- a hand edit during an
        incident, and this repo's history has several -- made every subsequent
        `_mutate` raise KeyError. Every verdict from then on would land
        unaudited through `_journal`'s CRITICAL fallback and scoring would
        freeze, until somebody found and repaired the file. `pending_outcome`
        already tolerates id-less rows; the merge did not, which is the same
        asymmetry `partition_readable` exists to remove on the trade stores.

        Kept rather than dropped: an id-less row cannot be merged (there is
        nothing to merge it ON), so it is passed through and reported. Losing
        an audit row silently is the outcome to avoid.
        """
        by_id, orphans = {}, []
        for d in list(base) + list(incoming):
            try:
                did = d['id']
            except (TypeError, KeyError, IndexError):
                orphans.append(d)
                continue
            prev = by_id.get(did)
            if prev is None or d.get('version', 0) > prev.get('version', 0):
                by_id[did] = d
        if orphans:
            logger.error('decision journal: %d row(s) have no id and cannot '
                         'be merged; keeping them as-is. Repair the file.',
                         len(orphans))
        return sorted(by_id.values(), key=lambda d: d['id']) + orphans

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

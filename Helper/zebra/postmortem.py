"""Post-mortems and precedents — the feedback loop from exits back to entries.

Owner's ask: *"after exit — post mortem should be done for future learnings —
for VET — feedback loop."*

Every settled position gets one tagged post-mortem. Tags aggregate into
PRECEDENTS, and precedents are injected into the entry-vetting context so the
agent judging a new signal can see what happened the last times this pattern
appeared. Without that, the vetting layer starts every decision from zero and
the book's own history is the one input it never gets.

Two design choices carry most of the weight:

**Fixed taxonomy.** Tags come from a closed list. An open vocabulary reads
better in a single post-mortem and is useless in aggregate — "illiquid book",
"bad book", "couldn't exit" and "wide spreads" would be four precedents with
support 1 instead of one with support 4, which is exactly the number the
injection filter reads.

**The self-training trap.** A VETOED signal never traded, so its outcome is a
spot triple-barrier proxy, not P&L. If a veto-leaning precedent could be built
from proxies alone, the layer would confirm its own vetoes, veto more, and
quietly shrink the book — with the evidence base looking healthier every round.
So every post-mortem records its BASIS, and a precedent may only argue FOR a
veto once enough of its support is realised. A precedent arguing to trade needs
no such guard: it cannot shrink the book.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from . import config as cfg

logger = logging.getLogger(__name__)

# ── taxonomy ───────────────────────────────────────────────────────────────
# Closed by construction. `record` rejects anything else rather than storing a
# tag no downstream reader will ever match on.
TAGS = {
    'event_inside_window':
        'results / ex-div / corporate action landed inside the expiry window',
    'illiquid_book':
        'could not exit at a fair price — the book, not the thesis, cost money',
    'thesis_invalidated':
        'the ST-magnet thesis broke: the level gave way or the trend turned',
    'gap_against':
        'an overnight gap did the damage; intraday management was irrelevant',
    'slow_no_move':
        'the move never came and time ran out — right idea, wrong clock',
    'gave_back':
        'reached most of the way to target, then handed it back',
    'debit_too_rich':
        'paid too much of the width; the payoff was never there to win',
    'sector_move':
        'moved with its sector, not on anything specific to the name',
    'worked_as_designed':
        'the signal did what it was supposed to do',
}

BASIS_REALISED = 'realised'   # the position traded; P&L is real money
BASIS_PROXY = 'proxy'         # vetoed; a spot triple-barrier, never priced

# Injection thresholds. K is small on purpose: the point is to hand the agent
# the few most relevant precedents, not a research corpus it will skim.
INJECT_K = 5
MIN_SUPPORT = 2
MIN_REALISED_SUPPORT = 2

# How many settled decisions one agent run handles. The book has a ~200-trade
# backlog predating this feature, and handing all of it to a single agent is a
# long session with many chances to die halfway — losing the whole run's work,
# since a post-mortem only lands when `record` is called. Small batches clear
# the backlog over a few weeks and lose at most one batch to a crash. Newest
# first, so a fresh exit is never stuck behind the archaeology.
BATCH_SIZE = 8


def _now() -> datetime:
    return datetime.now()


# ── writing ────────────────────────────────────────────────────────────────
def pending(store, limit: int = 20) -> list:
    """Settled decisions with no post-mortem yet, newest first.

    Covers BOTH arms deliberately. Post-morteming only the trades that happened
    would build an evidence base made entirely of decisions to trade, and the
    vetoes — the layer's most consequential calls — would be the ones with no
    lessons attached.
    """
    out = []
    for t in store.load_trades():
        if isinstance(t.get('postmortem'), dict):
            continue
        if t.get('status') == 'exited' and t.get('pnl') is not None:
            out.append({'trade_id': t['id'], 'basis': BASIS_REALISED,
                        'settled': t.get('exit_date') or ''})
            continue
        shadow = t.get('veto_shadow')
        if isinstance(shadow, dict) and shadow.get('status') == 'resolved':
            out.append({'trade_id': t['id'], 'basis': BASIS_PROXY,
                        'settled': str(shadow.get('resolved_at') or '')})
    out.sort(key=lambda r: r['settled'], reverse=True)
    return out[:limit]


def context(store, trade_id: int) -> dict:
    """The evidence bundle for one post-mortem — what happened, and what was
    known at the time."""
    t = store.find(trade_id)
    if not t:
        return {'error': f'trade #{trade_id} not found'}
    shadow = t.get('veto_shadow') if isinstance(t.get('veto_shadow'), dict) else {}
    realised = t.get('status') == 'exited' and t.get('pnl') is not None
    debit = t.get('debit')
    peak_mid = t.get('mfe_mid')
    return {
        'trade_id': t['id'],
        'basis': BASIS_REALISED if realised else BASIS_PROXY,
        'stock': t.get('stock'),
        'direction': t.get('direction'),
        'timeframe': t.get('timeframe'),
        'structure': t.get('structure', 'zebra'),
        'entry_date': t.get('entry_date'),
        'entry_spot': t.get('entry_spot'),
        'st_value': t.get('st_value'),
        'st_direction': t.get('st_direction'),
        'signal_gap_pct': t.get('signal_gap_pct'),
        'debit': debit,
        'width': t.get('width'),
        'debit_to_width_pct': t.get('debit_to_width_pct'),
        'expiry': t.get('expiry'),
        'dte_at_entry': t.get('dte_at_entry'),
        # Outcome — whichever arm applies.
        'exit_date': t.get('exit_date'),
        'exit_reason': t.get('exit_reason'),
        'pnl': t.get('pnl'),
        'pnl_pct': t.get('pnl_pct'),
        'veto_outcome': shadow.get('label'),
        # How far it got. The give-back story is unreadable without these, and
        # `gave_back` is one of the tags the agent is asked to consider.
        'peak_spot': t.get('mfe_spot'),
        'peak_mid': peak_mid,
        'peak_gain_pct': (round((float(peak_mid) / float(debit) - 1) * 100, 1)
                          if peak_mid is not None and debit else None),
        'peak_at': t.get('mfe_mid_at'),
        # What the vetting layer said at the time, so a wrong verdict can be
        # recognised as one.
        'vet_state': (t.get('vet') or {}).get('state'),
        'entry_warnings': t.get('entry_warnings'),
        'tags_allowed': TAGS,
    }


def record(store, trade_id: int, tags: list, lesson: str) -> dict:
    """Attach a post-mortem. Idempotent per trade; tags are validated."""
    tags = [str(x).strip().lower() for x in (tags or []) if str(x).strip()]
    bad = [x for x in tags if x not in TAGS]
    if bad:
        raise ValueError(f"unknown tag(s) {bad}; allowed: {sorted(TAGS)}")
    if not tags:
        raise ValueError('at least one tag is required')
    lesson = str(lesson or '').strip()
    if not lesson:
        raise ValueError('a one-line lesson is required')

    with store._mutate():
        t = store._must_find(trade_id)
        if isinstance(t.get('postmortem'), dict):
            raise ValueError(f"#{trade_id} already has a post-mortem")
        realised = t.get('status') == 'exited' and t.get('pnl') is not None
        t['postmortem'] = {
            'tags': tags,
            'lesson': lesson[:400],
            'basis': BASIS_REALISED if realised else BASIS_PROXY,
            'recorded_at': _now().isoformat(timespec='seconds'),
        }
        t['version'] = t.get('version', 0) + 1
    logger.info("POST-MORTEM #%d %s: %s", trade_id, t.get('stock'),
                ','.join(tags))
    return t['postmortem']


# ── reading ────────────────────────────────────────────────────────────────
def _verdict(t: dict) -> Optional[bool]:
    """Did this decision work out? None when it cannot be told."""
    pm = t.get('postmortem') or {}
    if pm.get('basis') == BASIS_REALISED:
        pnl = t.get('pnl')
        return None if pnl is None else float(pnl) > 0
    label = ((t.get('veto_shadow') or {}).get('label'))
    if label is None:
        return None
    # A vetoed signal that went on to HIT its target means the veto cost money;
    # the tag on it is evidence AGAINST vetoing that pattern again.
    return label == 'hit'


def precedents(store, stock: Optional[str] = None) -> list:
    """Aggregate post-mortems into per-tag precedents.

    `support` counts every post-mortem carrying the tag; `realised_support`
    counts only those whose basis is real P&L. The difference is the whole
    point — see the module docstring.
    """
    by_tag: dict = {}
    for t in store.load_trades():
        pm = t.get('postmortem')
        if not isinstance(pm, dict):
            continue
        if stock and t.get('stock') != stock:
            continue
        good = _verdict(t)
        if good is None:
            continue
        for tag in pm.get('tags') or []:
            r = by_tag.setdefault(tag, {
                'tag': tag, 'description': TAGS.get(tag, ''),
                'support': 0, 'realised_support': 0,
                'worked': 0, 'failed': 0, 'realised_pnl': 0.0,
                'examples': [],
            })
            r['support'] += 1
            if pm.get('basis') == BASIS_REALISED:
                r['realised_support'] += 1
                r['realised_pnl'] += float(t.get('pnl') or 0)
            r['worked' if good else 'failed'] += 1
            if len(r['examples']) < 3:
                r['examples'].append({
                    'id': t['id'], 'stock': t.get('stock'),
                    'basis': pm.get('basis'), 'pnl': t.get('pnl'),
                    'lesson': pm.get('lesson'),
                })
    for r in by_tag.values():
        n = r['support']
        r['win_rate'] = round(r['worked'] / n * 100, 1) if n else None
        r['leans'] = 'veto' if r['failed'] > r['worked'] else 'allow'
        r['realised_pnl'] = round(r['realised_pnl'], 2)
    return sorted(by_tag.values(), key=lambda r: (-r['support'], r['tag']))


def for_signal(store, stock: Optional[str], k: int = INJECT_K) -> dict:
    """Precedents worth showing the agent vetting a NEW signal.

    Returns {'shown': [...], 'withheld': [...]}. Withheld entries are reported
    rather than dropped: a precedent suppressed by the realised-support guard
    is itself information ("we think this pattern is bad but have only ever
    vetoed it"), and silently dropping it would make the guard invisible.
    """
    shown, withheld = [], []
    for r in precedents(store):
        if r['support'] < MIN_SUPPORT:
            continue
        # THE GUARD. A precedent that argues for a veto must be backed by
        # decisions that actually traded, or the layer trains on its own
        # vetoes: veto -> proxy outcome -> precedent -> more vetoes, with the
        # evidence base looking stronger every round while the book shrinks.
        if r['leans'] == 'veto' and r['realised_support'] < MIN_REALISED_SUPPORT:
            withheld.append(dict(r, withheld_because=(
                'leans veto with only %d realised trade(s) — needs %d'
                % (r['realised_support'], MIN_REALISED_SUPPORT))))
            continue
        shown.append(r)
    shown.sort(key=lambda r: (-r['support'], r['tag']))
    # Same-stock precedents first: the most specific evidence available.
    if stock:
        same = {r['tag'] for r in precedents(store, stock=stock)}
        shown.sort(key=lambda r: (r['tag'] not in same, -r['support']))
    return {'shown': shown[:k], 'withheld': withheld,
            'min_support': MIN_SUPPORT,
            'min_realised_support': MIN_REALISED_SUPPORT}


# ── the EOD batch ──────────────────────────────────────────────────────────
def due(store, now: Optional[datetime] = None) -> bool:
    """Should the batch run? Only when something settled and not already today.

    The cap is per DAY and lives on the calendar file's sibling marker rather
    than in memory, because the cron starts a fresh process every five minutes
    — an in-memory 'already ran' would be forgotten instantly and the batch
    would spawn an agent every cycle.
    """
    if not pending(store):
        return False
    now = now or _now()
    last = _last_run(store)
    return last is None or (now - last) >= timedelta(hours=20)


def _marker_path():
    return cfg.LOG_DIR / 'zebra_postmortem_run.txt'


def _last_run(store=None) -> Optional[datetime]:
    try:
        raw = _marker_path().read_text(encoding='utf-8').strip()
        return datetime.fromisoformat(raw)
    except Exception:
        return None


def spawn_batch(store, spawn: bool = True) -> bool:
    """One Sonnet run covering everything that settled. Returns True if spawned.

    Sonnet, not Fable: this is fact-collection and classification against a
    closed tag list, not a judgement call about money. The batch is marked as
    run BEFORE the spawn — a crashed agent must cost one day of missed
    post-mortems, not an agent every five minutes for the rest of the session,
    which is the shape the review channel already had to be fixed for.
    """
    rows = pending(store, limit=BATCH_SIZE)
    if not rows:
        return False
    from .vet import _spawn_generic
    ids = ', '.join(str(r['trade_id']) for r in rows)
    prompt = cfg.POSTMORTEM_PROMPT_TEMPLATE.format(
        vetting_doc=cfg.VETTING_DOC, python=_interpreter(), ids=ids)
    if not spawn:
        # A dry run must not consume the day's slot. Marking here would mean
        # one `zebra run --dry-run` silently cancels that day's real batch.
        return False
    mark_run()
    started = _spawn_generic(prompt, cfg.POSTMORTEM_MODEL, 'postmortem',
                             channel='postmortem') is not None
    if not started:
        # Same reasoning as the dry-run guard above: the day's slot must be
        # spent on a batch that actually RAN. A refused spawn (the budget is
        # saturated — postmortem is a deferrable channel and goes last) would
        # otherwise cost the whole day's post-mortems, and the next day's
        # sweep hits the same contention at the same point in the cycle.
        clear_run()
        logger.info('POSTMORTEM batch not started (no agent slot) — the '
                    "day's slot was released, retried next cycle")
    return started


def _interpreter() -> str:
    import sys
    from .vet import resolve_cli          # noqa: F401  (same resolution rules)
    return sys.executable


def clear_run() -> None:
    """Undo `mark_run` when the batch never started.

    Symmetric with the dry-run guard: the day's slot belongs to a batch that
    actually ran. Failing to clear costs one day of post-mortems, so it is
    logged and swallowed rather than raised into the cycle.
    """
    try:
        p = _marker_path()
        if p.exists():
            p.unlink()
    except OSError as e:                         # pragma: no cover - paranoia
        logger.warning("post-mortem run marker not cleared: %s", e)


def mark_run(now: Optional[datetime] = None) -> None:
    now = now or _now()
    try:
        cfg.LOG_DIR.mkdir(exist_ok=True)
        _marker_path().write_text(now.isoformat(timespec='seconds'),
                                  encoding='utf-8')
    except OSError as e:
        # Losing the marker costs a duplicate batch tomorrow, not a wrong
        # decision. It must never stop the batch from running.
        logger.warning("post-mortem run marker not written: %s", e)

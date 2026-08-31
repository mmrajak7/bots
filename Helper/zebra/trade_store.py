"""Zebra trade store — CRUD + Drive sync.

Lifecycle: watching → triggered → entered → exited.
A signal can also be 'cancelled' from watching/triggered.

Mirrors pyramid/bcs store pattern: local-first JSON, Drive-secondary,
atomic writes, version-based merge, singleton access.
"""

import copy
import json
import logging
import os
import platform
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from . import capital
from . import config as cfg
from common import store_contract
from common.filelock import LockTimeout, exclusive

logger = logging.getLogger(__name__)

# Non-mfe_* fields the batched per-poll write is allowed to carry. Kept as an
# explicit allowlist rather than loosening the prefix check: that check exists
# because callers pass whole-state patches, so one typo'd key would silently
# overwrite `status` or `debit` on a live position. The spot-corroboration
# reference rides this write for the same reason the peaks do — it updates once
# per open position per poll, and a write per trade per cycle is precisely what
# the batching removed.
# `exit_depth` joined them on 2026-08-30: the same cadence (once per open
# position per poll) and the same reason for riding this write rather than
# taking one of its own.
_BATCHED_POLL_FIELDS = frozenset({'corrob_spot', 'corrob_value', 'corrob_t',
                                  'exit_depth'})

#: Statuses that mean this thesis is ALREADY LIVE somewhere and a second
#: signal on the same (stock, timeframe, direction) must be refused.
#:
#: The set is deliberately wider than "entered". Dedup is not asking "is money
#: deployed", it is asking "could a new entry end up beside an existing
#: obligation at the broker" — and the two transient close states both answer
#: yes:
#:
#: * 'closing'       — the close lock is taken, orders are out, the legs are
#:                     still there until they fill.
#: * 'partial_close' — the close FAILED part-way and the record is frozen for a
#:                     human. Legs are live and nothing is monitoring them.
#:                     Omitting it let the scanner re-signal and re-enter the
#:                     same stock while the stranded position sat at the broker.
#:
#: One tuple, imported by the dedup check, so a future state cannot be added to
#: the machine and silently missed here (`feedback_the_copy_you_did_not_open`).
OPEN_STATUSES = ('watching', 'triggered', 'entered', 'closing', 'partial_close')


def in_cohort(trade: dict, cohort: Optional[str] = None) -> bool:
    """Was this position opened by the CURRENT engine?

    ONE definition, imported by every reader, because the alternative is each
    report inventing its own date test and them quietly disagreeing about what
    the scoreboard means.

    A record with no `cohort` stamp is LEGACY by construction — the 383 records
    that predate the stamp were priced mid-mid, ran unvetted, and were made by
    an engine that no longer exists. Absence is the answer, not a reason to
    guess from `entry_date`.
    """
    stamped = trade.get('cohort')
    if not stamped:
        return False
    return stamped == (cohort or cfg.COHORT_START)


#: Statuses a signal passes through BEFORE it becomes a position. The cohort
#: stamp is applied at ENTRY (`mark_entered_bcs`), deliberately -- moving
#: `cohort_start` must not reclassify positions already open and already being
#: measured -- so a signal cannot be in-cohort yet and must not be judged as
#: out-of-cohort either. It is simply not a result.
PRE_POSITION_STATUSES = ('watching', 'triggered')


def scored(trades, cohort=None) -> list:
    """The ONLY population a measurement, report, scorecard or gate may use.

    OWNER DECISION, 2026-08-31: *"the old records are not to be used any more
    ... all code rules shud stick to cohort bcs only ... forget old trades and
    results."*

    The engine changed substantially in early August: the back ratio was
    dropped for a Bull Call Spread, pricing moved from mid-mid to fill on
    2026-08-12, entry vetting was armed, and the cohort opened 2026-08-14. The
    448 records before that describe a DIFFERENT strategy. Mixing them into a
    number about this one does not make the sample bigger, it makes the number
    meaningless -- and every one of them is mid-priced, so it also makes it
    optimistic in a known direction.

    UNCONDITIONAL, and not behind `alerts_cohort_only`. That switch governs
    how CHATTY an alert is; this governs what is true. A measurement that a
    config file can widen back over a retired engine is not a rule, it is a
    default.

    The records are NOT deleted, hidden from the store, or archived (owner
    decision, same date): `zebra status` still shows the whole book, the store
    still merges and ids against all of it, and the forensic history stays
    readable. They simply stop being EVIDENCE.

    Signals that have not entered yet are excluded too, and that is not a
    judgement about them -- they carry no stamp because they carry no result.
    Use `in_flight` for the population a liveness check needs.
    """
    return [t for t in trades if in_cohort(t, cohort)]


def in_flight(trades) -> list:
    """Signals and positions the CURRENT engine still has work to do on.

    The complement of `scored` for the other question: not "what did we
    achieve" but "what is live". An unstamped `watching`/`triggered` signal
    belongs here -- it is a candidate of this engine that has not entered --
    while an unstamped `entered` record would be a legacy position and does
    not.
    """
    return [t for t in trades
            if (t.get('status') in PRE_POSITION_STATUSES
                or (t.get('status') == 'entered' and in_cohort(t)))]


def from_current_engine(trade: dict, cohort: Optional[str] = None) -> bool:
    """Did THIS engine make this decision, whether or not it became a trade?

    Wider than `in_cohort`, and the difference is entirely about records that
    never entered. The cohort stamp is applied at ENTRY, so a signal this
    engine VETOED, drift-cancelled or stale-cancelled can never carry one --
    52 records are in exactly that state. `scored` therefore cannot see them,
    and it should not: they are not results.

    They ARE decisions, and the post-mortem layer exists to learn from the
    vetoes specifically. `zebra/postmortem.py` says why in its own docstring:
    covering only the trades that HAPPENED "would build an evidence base made
    entirely of decisions to trade, and the vetoes -- the layer's most
    consequential calls -- would be the ones with no lessons attached."

    WHY A DATE HERE, when `in_cohort` explicitly refuses to guess from one.
    That refusal is about POSITIONS, where a stamp exists and its absence is
    itself the answer ("made by an engine that no longer exists"). For a
    record that never entered, no stamp could ever have been written, so
    absence carries no information at all and the signal date is the only
    thing that distinguishes a veto from last week from one in May. The rule
    stays: never re-derive a stamped record's era from a date.
    """
    if trade.get('cohort'):
        return in_cohort(trade, cohort)
    if trade.get('status') not in ('watching', 'triggered', 'cancelled'):
        # An `entered`/`exited` record with no stamp is a legacy POSITION.
        # Absence is the answer there, exactly as `in_cohort` says.
        return False
    start = cohort or cfg.COHORT_START
    return bool(start) and (trade.get('signal_date') or '')[:10] >= start


def decided(trades, cohort=None) -> list:
    """Every decision this engine made -- taken, vetoed or abandoned.

    The population for the vetting scorecard and the post-mortem layer. Use
    `scored` for anything that produces a P&L number; a veto has no P&L and
    including it in one would be its own kind of nonsense.
    """
    return [t for t in trades if from_current_engine(t, cohort)]


def is_paper_record(trade: dict) -> bool:
    """Did this record's legs NEVER reach a broker?

    ONE definition, for the same reason `in_cohort` is one: the adapter that
    decides whether the money path may see a record and the monitor that
    decides whether it may still book one in paper have to agree, or the
    position falls between them and NO engine holds it. That is the silent
    state this whole interlock exists to prevent.

    **Absence means PAPER, and that is a statement about THIS store.** Every
    writer here stamps the key — `zebra/scanner.py` on the signal,
    `_bcs_entry_fields` on the position — so within `zebra_trades.json` a
    missing flag can only be a record written by something that did not know
    about the flag, which is not evidence that a broker ever saw it. The
    conservative reading is the one that keeps a booking engine attached: a
    paper record that zebra keeps is watched; a live record the bridge drops
    is not.

    The BCS-family stores (bcs / bear_put / fallen_hero) hold real positions
    and no `paper` key at all, so they must NEVER be asked this question.
    `bcs/spread_monitor._record_says_paper` is the predicate for that side and
    reads the flag POSITIVELY on purpose — see its docstring for why the two
    defaults are deliberately opposite.
    """
    return bool(trade.get('paper', True))


# ── The take-profit latch ────────────────────────────────────────────────────
#
# Owner decision, 2026-08-28: *"touch that doesn't persist — does not matter ->
# exit -> if seeing touch once, proven is ok."*
#
# THE PROBLEM IT SOLVES. Exits are decided on a 5-minute cron tick and a vetted
# exit adds ~90 seconds on top (measured 83-106s), so the round trip from
# "trigger observed" to "close executed" is about one whole cycle. A take-profit
# therefore only converted if the touch SURVIVED that window. On 2026-08-27
# COFORGE #436 traded THROUGH its TP (`mfe_spot` 1934.2 against tp 1931.91 at
# 09:25), the exit vet re-quoted and allowed at 09:27, and by the next
# actionable poll spot had backed off — nothing was booked. The cause is
# structural, not a bug: the trigger was re-evaluated from scratch on a later
# price than the one that fired it.
#
# THE RULE. The FIRST observed touch arms the exit FOR THE REST OF THAT TRADING
# DAY. From then on the exit proceeds regardless of where spot has moved — until
# the session it was armed in ends, at which point it simply stops counting.
#
# THE BOUND, owner 2026-08-28: *"TP latch should be for same day"*. As first
# built the latch never expired, so a touch followed by a permanently unusable
# book would exit on the first usable one, days later if need be — a Monday
# touch booking on Thursday's first print. The trigger is a statement about
# where the market was, and it stops being one overnight.
#
# HOW THE EXPIRY WORKS, and why it is not a sweep. It is evaluated on READ:
# `tp_latched` compares the stamp's trading day with today's and answers False
# when they differ. Nothing has to RUN to expire a latch, so a missed cron
# cycle, a weekend, a dead process or a box that was switched off cannot leave
# one armed — which a nightly sweep, or an in-memory timer, all could. Nothing
# is ever deleted either: the stale stamp stays on the record as evidence and
# the READ is what changes.
#
# THE DAY IS THE IST CALENDAR DATE. The session is 09:15-15:30 IST and cannot
# straddle midnight, so the IST date IS the trading day. It is emphatically not
# the host's naive date (`bcs/spread_monitor.py` still gates its session on
# `datetime.now()` — open item M7 — and a UTC-clocked box reads 09:15 IST as
# 03:45) and not the UTC date (01:30 IST is the previous UTC day, mid-session
# for nobody but a rule that would expire a latch two hours after arming it).
# Naive datetimes handed in by callers are normalised through `_ist`.
#
# AT THE BOUNDARY. A touch at 15:29 that has not booked by the close expires
# unbooked. That is the rule doing its job, not a failure — but it is also the
# only way this rule can cost anything, so it is RECORDED: `tp_latch` returns an
# `expired_patch` appending the lapsed touch to `TP_LATCH_EXPIRED`, and the
# engine that owns the record writes it. Count those against the exits the
# unbounded version would have booked days late, and the question of whether
# same-day is the right rule stops being an argument.
#
# WHY IT LIVES IN THE STORE MODULE. Two engines hold this trigger — `zebra/
# monitor.py` books it in paper, `bcs/spread_monitor.py` books it with real
# orders once armed — and a latch honoured by one and not the other is this
# codebase's single most repeated defect (`feedback_the_copy_you_did_not_open`,
# six instances on 2026-08-26 alone). So the decision is ONE function that both
# import, sitting beside `in_cohort` and `is_paper_record`, which are here for
# exactly the same reason.
#
# WHAT LATCHES AND WHAT DOES NOT. **The TRIGGER latches; the PRICE does not.**
# Nothing here records a price to book at, and no caller may treat the touch
# spot as one — booking is done at the book observed when the close actually
# executes, and a latched exit still faces every valuation guard it always did
# (the reliability gate, the intrinsic floor, refuse-the-estimate, the
# debounce). Latching decides WHETHER to exit, never at what price
# (`feedback_trigger_is_not_the_fill`). Expect a booked P&L below the one the
# touch implied; that is the accepted cost of the decision.
#
# SCOPE IS TAKE-PROFIT ONLY, deliberately:
#   * SPOT SL is a VETO in this system and never a trigger (measured: a 3% spot
#     stop cut 31 of 78 winners for Rs 8.9L). Latching it would promote it to a
#     trigger, and a latched one at that — the exact inversion `CLAUDE.md`'s
#     "Spot-based stops — VETO, never TRIGGER" section forbids.
#   * DEBIT SL and TRAIL are VALUE triggers whose whole defence is the
#     N-consecutive-reliable-polls debounce that exists because ONE garbage
#     print cost Rs 7,297 (NHPC, 2026-07-24). A latch is a debounce of one, in
#     the loss direction, on the source that has twice been wrong.
#   * TIME/EXPIRY is calendar-driven and re-arms daily by design; it has nothing
#     that can evaporate.
# The asymmetry is the point: a missed TP costs an opportunity, and a latched
# stop on a bad print costs capital.

#: When the first touch of the CURRENT trading day was seen. Written as an
#: IST-AWARE ISO timestamp (`...+05:30`): the day boundary is the whole rule
#: now, and a naive stamp is a boundary nobody can settle after the fact.
#: Records written before 2026-08-28 carry naive local time and are read
#: through `_ist`, which resolves them on the host clock they were written on.
TP_TOUCHED_AT = 'tp_touched_at'
#: The spot that did it. FORENSIC AND MEASUREMENT ONLY — never a booking price.
TP_TOUCH_SPOT = 'tp_touch_spot'
#: Append-only list of touches that reached the end of their session without
#: booking: `{'touched_at', 'touch_spot', 'noticed_at'}`. The price of the
#: same-day bound, on the records that paid it.
TP_LATCH_EXPIRED = 'tp_latch_expired'


def _ist(dt: Optional[datetime] = None) -> datetime:
    """`dt` as an IST-aware instant; now, in IST, when `dt` is None.

    A NAIVE input is system-local, because that is what `datetime.now()`
    returns and both engines hand one over (`bcs/spread_monitor.py` passes its
    own `datetime.now()` deliberately, so the touch and the latency stamp read
    off one clock). `astimezone` is the documented conversion for it and is the
    identity on the IST-clocked Pi.
    """
    if dt is None:
        return datetime.now(cfg.IST)
    try:
        return dt.astimezone(cfg.IST)
    except (OSError, OverflowError, ValueError):    # pragma: no cover
        # A host with no resolvable local zone, or a datetime near the range
        # ends. Reading it as IST is the assumption the rest of this system
        # already makes; raising inside an exit path is not an option.
        return dt.replace(tzinfo=cfg.IST)


def tp_trading_day(dt: Optional[datetime] = None):
    """The trading day `dt` falls in — i.e. its IST calendar date.

    The session is 09:15-15:30 IST, so it cannot straddle midnight and the date
    is the whole answer. One function, because a second spelling of "which day
    is this" is how the two engines would drift apart.
    """
    return _ist(dt).date()


def _touch_day(trade: dict):
    """The trading day this record's latch was armed on, or None if it has no
    stamp or the stamp cannot be read."""
    stamp = trade.get(TP_TOUCHED_AT)
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        return tp_trading_day(datetime.fromisoformat(stamp))
    except (TypeError, ValueError):
        return None


def tp_latched(trade: dict, now: Optional[datetime] = None) -> bool:
    """Is this record's take-profit armed by a touch seen EARLIER TODAY?

    Absence means NOT YET TOUCHED. All 462 existing records predate the field
    and must read as unlatched, which is what `.get()` on a missing key gives —
    stated here because it is a property, not an accident.

    A stamp from any other trading day means NOT ARMED (owner, 2026-08-28:
    *"TP latch should be for same day"*). "Any other" and not "any earlier": a
    stamp in the future is a clock nobody can trust, and an unbounded arming
    off one is the thing this bound exists to prevent. An unreadable stamp is
    treated the same way — it cannot be shown to be today's, and falling back
    to the live spot comparison costs an opportunity, while honouring a stamp
    of unknown age is the pre-bound behaviour the owner has just ruled out.
    """
    day = _touch_day(trade)
    return day is not None and day == tp_trading_day(now)


def _expiry_evidence(trade: dict, now: datetime) -> dict:
    """The patch that records a touch which lapsed unbooked, or `{}`.

    Idempotent by the stamp itself: `tp_latch` is called every poll of every
    open position, and an evidence log that grows every five minutes is not
    evidence. Nothing is cleared — the lapsed stamp stays where it is, and the
    list is what a later reader counts.
    """
    stamp = trade.get(TP_TOUCHED_AT)
    if not stamp:
        return {}
    prior = trade.get(TP_LATCH_EXPIRED) or []
    if not isinstance(prior, list):             # pragma: no cover - corrupt
        prior = []
    if any(isinstance(e, dict) and e.get('touched_at') == stamp
           for e in prior):
        return {}
    return {TP_LATCH_EXPIRED: list(prior) + [{
        'touched_at': stamp,
        'touch_spot': trade.get(TP_TOUCH_SPOT),
        'noticed_at': _ist(now).isoformat(timespec='seconds'),
    }]}


def tp_latch(trade: dict, hit_now: bool, spot,
             now: Optional[datetime] = None) -> dict:
    """Should the TP exit proceed, and what must be persisted to keep it armed?

    `hit_now` is the engine's own live comparison — zebra reads
    `direction == 'CE'`, the monitor reads `_strategy == 'BCS'`, and those two
    vocabularies are left where they are rather than unified here. What is
    shared is the DECISION, which is the part that must not diverge.

    Returns
      armed         — the exit may proceed this cycle (touched now, or earlier
                      TODAY)
      latched       — the record was ALREADY armed when this cycle started
      new_touch     — this cycle is the first touch OF THIS TRADING DAY, so
                      `patch` must be persisted
      patch         — the fields to write, or `{}`. The caller writes them
                      BEFORE the vet and before any order, because a verdict in
                      flight is exactly when the trigger used to evaporate.
      expired       — a stamp is present but belongs to another trading day, so
                      it no longer arms anything
      expired_patch — the evidence write for that lapse, or `{}`. SEPARATE from
                      `patch` on purpose: `patch` means "a touch just happened"
                      and its callers log and alert accordingly, so folding
                      bookkeeping into it would have an engine announce a touch
                      that is not happening. A caller that ignores this key is
                      correct, merely silent.

    `now` is the caller's clock and may be naive (the order path passes its own
    `datetime.now()` so the touch and the latency stamp share one clock); it is
    normalised to IST here, and the stamp is WRITTEN with its offset.
    """
    ist_now = _ist(now)
    latched = tp_latched(trade, now=ist_now)
    new_touch = bool(hit_now) and not latched
    expired = bool(trade.get(TP_TOUCHED_AT)) and not latched
    evidence = _expiry_evidence(trade, ist_now) if expired else {}
    patch: dict = {}
    if new_touch:
        patch[TP_TOUCHED_AT] = ist_now.isoformat()
        try:
            patch[TP_TOUCH_SPOT] = float(spot)
        except (TypeError, ValueError):
            # A latch with no spot is still a latch. The touch is the fact that
            # matters; the number is measurement, and losing the measurement
            # must never cost the arming.
            patch[TP_TOUCH_SPOT] = None
        # A re-arm OVERWRITES the stamp, so yesterday's lapse has to be
        # preserved in the same write or it is gone. Folded into `patch` rather
        # than left in `expired_patch` because here a caller that only writes
        # `patch` is not being misled — a touch really is happening this cycle.
        patch.update(evidence)
        evidence = {}
    return {'armed': bool(hit_now) or latched, 'latched': latched,
            'new_touch': new_touch, 'patch': patch, 'expired': expired,
            'expired_patch': evidence}


def tp_touch_to_fill(trade: dict, exit_spot, rising: Optional[bool] = None,
                     now: Optional[datetime] = None) -> dict:
    """What the latency between the touch and the booking actually cost.

    Stamped by whichever engine books the exit, AFTER it is booked. This is the
    number that says whether M12 (consuming the vet verdict inside the same
    cycle instead of on the next tick) is worth building: if the give-back is
    routinely near zero, the ~5-minute lag is a tidiness problem; if it is not,
    it is a P&L problem with a price on it.

    Returns `{}` for an unlatched record — a TP that fired and booked inside one
    observation has no gap to report, and inventing a zero would make the
    distribution unreadable. An EXPIRED latch reads as unlatched here too: a
    close on the morning after a lapsed touch would otherwise report a
    seventeen-hour give-back into a distribution that is meant to price a
    five-minute one.

    `rising` says which way this position's TP points (True for CE / BCS). When
    omitted it is inferred from the touch itself: the touch spot sat at or above
    the level for a rising target. Purely for the `gave_back` label; the signed
    move is reported either way.
    """
    if not tp_latched(trade, now=now):
        return {}
    out: dict = {}
    touched = trade.get(TP_TOUCHED_AT)
    try:
        # BOTH sides through `_ist`. The two engines timestamp on different
        # clocks — zebra reasons in IST-aware datetimes, the order path passes
        # naive local — and subtracting one from the other raises. Normalising
        # (rather than the old blanket tzinfo-strip) also stops a UTC-clocked
        # host reporting a 5.5-hour lag on a five-minute one.
        t0 = _ist(datetime.fromisoformat(str(touched)))
        out['tp_touch_to_exit_sec'] = round(
            (_ist(now) - t0).total_seconds(), 1)
    except (TypeError, ValueError):
        pass
    try:
        touch_spot = float(trade.get(TP_TOUCH_SPOT))
        exit_spot = float(exit_spot)
    except (TypeError, ValueError):
        return out
    move = round(exit_spot - touch_spot, 4)
    out['tp_touch_spot_move'] = move
    if touch_spot:
        out['tp_touch_spot_move_pct'] = round(move / touch_spot * 100, 4)
    if rising is None:
        try:
            rising = touch_spot >= float(trade.get('tp_spot'))
        except (TypeError, ValueError):
            rising = None
    if rising is not None:
        # Did spot retreat back through the touch while the exit was in flight?
        # That is the COFORGE shape, and the thing this latch exists to stop
        # from cancelling an exit.
        out['tp_touch_gave_back'] = bool(move < 0) if rising else bool(move > 0)
    return out


def cohort_split(trades: list, cohort: Optional[str] = None) -> tuple:
    """(current-engine trades, legacy trades). Order matters — current first,
    because it is the one anybody is actually asking about."""
    want = cohort or cfg.COHORT_START
    current = [t for t in trades if in_cohort(t, want)]
    legacy = [t for t in trades if not in_cohort(t, want)]
    return current, legacy


def _load_config() -> dict:
    if not cfg.CONFIG_FILE.exists():
        logger.warning("Config %s not found, using defaults", cfg.CONFIG_FILE)
        return {}
    with open(cfg.CONFIG_FILE) as f:
        return json.load(f)


def _resolve_credentials_path(config: dict) -> Optional[Path]:
    env_path = os.environ.get('ZEBRA_GOOGLE_CREDS')
    if env_path:
        return Path(env_path)
    drive_cfg = config.get('google_drive', {})
    if platform.system() == 'Windows':
        path_str = drive_cfg.get('credentials_path_windows')
    else:
        path_str = drive_cfg.get('credentials_path_linux')
    return Path(path_str) if path_str else None


def _lots_from(bcs: dict) -> int:
    """Lots for this entry, from the record rather than hardcoded.

    Both entry builders said `lots = 1`. That was correct while every entry
    was one lot and silently wrong the moment sizing arrived: a 3-lot fill
    would be booked as ONE, so `quantity`, `capital` and every P&L derived
    from them would understate the position by two thirds -- while the record
    looked perfectly healthy and every stop level was computed off it.

    ONE definition for both call sites, because that is exactly how the bug
    was able to exist in two places at once
    (`feedback_copy_pasted_modules_fix_once`).

    Defaults to 1, so a caller that does not size still gets the old
    behaviour, and floors at 1 -- a zero-lot entry is not a position.
    """
    try:
        return max(1, int(bcs.get('lots') or 1))
    except (TypeError, ValueError):
        return 1


class ZebraStore:
    """Zebra trades with local JSON + Drive sync."""

    def __init__(self, config: Optional[dict] = None):
        self._config = config or _load_config()
        self._trades: list = []
        self._drive_service = None
        self._drive_file_id: Optional[str] = None
        self._drive_enabled = False
        self._last_sync_time = 0.0
        self._sync_interval = (
            self._config.get('google_drive', {}).get('sync_interval_sec', 300)
        )

    def initialize(self):
        drive_cfg = self._config.get('google_drive', {})
        if drive_cfg.get('enabled', False):
            self._init_drive(drive_cfg)
        if self._drive_enabled:
            self._sync_from_drive()
        else:
            self._load_local()

        watching = sum(1 for t in self._trades if t.get('status') == 'watching')
        triggered = sum(1 for t in self._trades if t.get('status') == 'triggered')
        entered = sum(1 for t in self._trades if t.get('status') == 'entered')
        logger.info(
            "ZebraStore: %d trades (%d watching, %d triggered, %d entered), drive=%s",
            len(self._trades), watching, triggered, entered,
            'enabled' if self._drive_enabled else 'disabled'
        )

    # ── Reads ─────────────────────────────────────────────────────────────
    def load_trades(self) -> list:
        return self._trades

    def get_by_status(self, status: str) -> list:
        return [t for t in self._trades if t.get('status') == status]

    def get_watching(self) -> list:
        return self.get_by_status('watching')

    def get_triggered(self) -> list:
        return self.get_by_status('triggered')

    def get_entered(self) -> list:
        return self.get_by_status('entered')

    def find(self, trade_id: int) -> Optional[dict]:
        for t in self._trades:
            if t.get('id') == trade_id:
                return t
        return None

    def reload(self) -> None:
        """Re-read the local file into this process's cache.

        Public because a reader in one process sometimes has to see a write
        made by ANOTHER one without going through `_mutate`: the exit vet's
        in-cycle wait (M12) polls for a verdict that the Claude CLI writes from
        a separate process, and every read below this line answers from
        `self._trades`, which that write cannot touch.

        Takes the same lock `_load_local` does, so it is never safe to call
        from inside `_mutate` -- flock on a second fd in one process
        deadlocks. Callers wait BEFORE they mutate, which is the only ordering
        that makes sense anyway.
        """
        self._load_local()

    # ── Cross-process mutation guard ──────────────────────────────────────
    @contextmanager
    def _mutate(self, drive: bool = True, persist: bool = True):
        """Lock → refresh from disk → caller mutates → save → unlock → Drive.

        Every write path MUST go through this. As of 2026-08-10 there are two
        writer processes (zebra cron and the Claude vetting/review cron), and
        an unprotected read-modify-write silently loses trades: both read the
        same state, both write, and the second erases the first's change with
        no error and no corrupt file.

        The refresh matters as much as the lock. `self._trades` is a cache that
        goes stale the moment the OTHER process writes, so mutating it and
        saving would push stale data back over fresh. Inside the lock we
        re-read the disk and version-merge, making disk the truth before the
        caller touches anything. That is also why callers must call `find()`
        INSIDE the block — a dict fetched before the refresh is a detached
        object whose mutations would be silently dropped.

        Ordering is deliberate: the Drive upload is a NETWORK call and happens
        after the lock is released. Holding a mutex across an HTTP round-trip
        would stall the other cron's entire cycle (see feedback: drive-first —
        sync down before, upload after, never inside).
        """
        with exclusive(cfg.LOCK_FILE):
            # same_replica: disk vs THIS process's cache. A version tie here is
            # the sibling writer this docstring already describes, not a split
            # brain -- absorbing its write is what the refresh is for.
            self._trades = self._merge_announced(
                self._read_local(), self._trades, same_replica=True)
            # Rollback point. If the caller raises mid-mutation (e.g.
            # _apply_entry sets status='entered' and then a float() cast on a
            # later field blows up), the half-mutated trade must not linger in
            # self._trades — get_entered() would report a position that was
            # never persisted. Snapshot post-refresh state and restore it on
            # any exception; disk is untouched either way (save is skipped).
            snapshot = copy.deepcopy(self._trades)
            try:
                yield
            except BaseException:
                self._trades = snapshot
                raise
            # A mutation that mutated nothing must not rewrite the store. The
            # guarded advisory writers (reset_confirm, clear_blind) run for
            # every open position on every poll and their guard skips only the
            # FIELD assignment, not this save — so a quiet cycle with 24
            # positions was rewriting ~1 MB forty-eight times and taking the
            # cross-process lock for each, on a Pi that also runs the
            # live-money monitor. The snapshot already exists for rollback;
            # comparing against it is far cheaper than serialising to disk.
            changed = self._trades != snapshot
            if persist and changed:
                self._save_local()
        if persist and changed and drive:
            self._upload_to_drive()

    # ── Writes ────────────────────────────────────────────────────────────
    def add_signal(self, data: dict) -> dict:
        """Add a fresh signal at WATCH band entry (gap <= watch_gap_max).

        Required: stock, timeframe, direction, st_value, st_direction,
                  signal_price, signal_gap_pct.
        """
        required = ['stock', 'timeframe', 'direction', 'st_value',
                    'st_direction', 'signal_price', 'signal_gap_pct']
        missing = [f for f in required if f not in data]
        if missing:
            raise ValueError(f"Missing required fields: {missing}")

        stock = data['stock']
        timeframe = data['timeframe']
        direction = data['direction']

        # Dedup + id allocation must both happen INSIDE the lock: they are a
        # read-check-write, and two processes racing here would either double-add
        # the same signal or hand out the same id to two different trades.
        #
        # The dedup predicate is `shadow_of is None`, NOT `structure != 'bcs'`.
        # It used to be the latter, which was right only while every BCS was a
        # second record shadowing a zebra: a shadow is an observation, so it
        # must not block a new signal. Once a BCS became the position itself
        # (2026-08-12) that same test would have excluded every real position
        # from dedup and let duplicates open on one thesis. "Is this a shadow
        # of something else" is the property that actually matters, and it does
        # not change meaning when the structure does.
        with self._mutate():
            for t in self._trades:
                if (t.get('stock') == stock
                        and t.get('timeframe') == timeframe
                        and t.get('direction') == direction
                        and t.get('shadow_of') is None
                        and t.get('status') in OPEN_STATUSES):
                    raise ValueError(
                        f"{stock} {timeframe} {direction} already open as #{t['id']}"
                    )

            now = datetime.now()
            trade = {
                'id': self._next_id(),
                'version': 1,
                'status': 'watching',
                'stock': stock,
                'timeframe': timeframe,
                'direction': direction,            # CE or PE
                'st_value': data['st_value'],
                'st_direction': data['st_direction'],
                # trend_aligned is NOT stored — it is derived on demand from
                # direction + st_direction via cfg.is_trend_aligned (single source
                # of truth), so it can never drift from or be dropped by the schema.
                'signal_price': data['signal_price'],
                'signal_gap_pct': data['signal_gap_pct'],
                'signal_date': now.strftime('%Y-%m-%d'),
                'signal_time': now.strftime('%H:%M:%S'),
                'paper': data.get('paper', True),
                'notes': data.get('notes', ''),
            }
            self._trades.append(trade)
        logger.info(
            "WATCHING #%d %s %s %s spot=%.2f ST=%.2f gap=%.2f%%",
            trade['id'], stock, timeframe, direction,
            trade['signal_price'], trade['st_value'], trade['signal_gap_pct']
        )
        return trade

    def mark_triggered(self, trade_id: int, trigger_spot: float,
                       trigger_gap_pct: float,
                       alert_strikes: list) -> dict:
        """Promote watching → triggered. alert_strikes = candidate pairs from analyzer."""
        with self._mutate():
            # find() inside the lock: a dict fetched before the refresh is a
            # detached object and its mutations would be silently discarded.
            t = self._must_find(trade_id)
            if t['status'] != 'watching':
                raise ValueError(f"#{trade_id} status={t['status']}, can't trigger")
            now = datetime.now()
            t['status'] = 'triggered'
            t['triggered_at'] = now.isoformat()
            t['trigger_spot'] = trigger_spot
            t['trigger_gap_pct'] = trigger_gap_pct
            t['alert_strikes'] = alert_strikes
            t['version'] = t.get('version', 0) + 1
        logger.info("TRIGGERED #%d %s gap=%.2f%% (%d candidate pairs)",
                    trade_id, t['stock'], trigger_gap_pct, len(alert_strikes))
        return t

    def mark_entered(self, trade_id: int, entry_data: dict) -> dict:
        """Promote triggered → entered. User has placed the trade.

        Required in entry_data: long_strike, short_strike, long_symbol,
        short_symbol, debit, lot_size, lots, expiry.
        Computed: quantity, capital, tp_spot, sl_spot, debit_sl_value, dte.
        """
        required = ['long_strike', 'short_strike', 'long_symbol',
                    'short_symbol', 'debit', 'lot_size', 'lots', 'expiry']
        missing = [f for f in required if f not in entry_data]
        if missing:
            raise ValueError(f"mark_entered missing: {missing}")

        with self._mutate():
            t = self._must_find(trade_id)
            if t['status'] not in ('watching', 'triggered'):
                raise ValueError(f"#{trade_id} status={t['status']}, can't enter")
            # `already_filled=True`, ALWAYS. This is the hand-entry path:
            # `--debit` is what was actually PAID, so by definition the trade
            # exists at the broker before this method is called. Refusing here
            # does not undo it — it only loses the RECORD, and a live position
            # with no record has no stops, no monitor and no exit engine.
            # `mark_entered_bcs` has threaded this flag since it was written;
            # the manual path, which is always-filled, did not. Latent while
            # paper mode exempts the check, and it arms the moment
            # `paper_mode` goes false — which is where `zebra enter` becomes
            # the money path.
            self._refuse_if_over_budget(trade_id, {
                'stock': t.get('stock'), 'debit': entry_data.get('debit'),
                'lot_size': entry_data.get('lot_size'),
                'lots': entry_data.get('lots') or 1},
                already_filled=True)
            self._apply_entry(t, entry_data)
        logger.info(
            "ENTERED #%d %s %s/%s debit=%.2f qty=%d cap=Rs%.0f TP=%.2f SL=%.2f",
            trade_id, t['stock'], int(t['long_strike']), int(t['short_strike']),
            t['debit'], t['quantity'], t['capital'], t['tp_spot'], t['sl_spot']
        )
        return t

    def _apply_entry(self, t: dict, entry_data: dict) -> None:
        """Field-level entry mutation. Split out of mark_entered so the whole
        computation runs INSIDE the lock without a 60-line critical section
        being visually lost in the middle of the method.

        **`paper` — WHO OWNS THIS POSITION (added 2026-08-29).**

        `add_signal` stamps every signal `paper: True`, and until now nothing
        on THIS path ever changed it: only `mark_entered_bcs` set it, and only
        from `_auto_enter_bcs`. So a trade entered by hand — which is the
        FIRST live-money action in the arming order, back when "the alert IS
        the order ticket" — kept `paper: True` while its legs sat at the
        broker.

        Every consequence of that runs the wrong way and none of it is loud:
        `is_paper_record` says paper, so `_exits_external` keeps the position
        with THIS engine and `_paper_auto_close` books its exit at mid;
        `close_spread` correctly refuses it, so the armed monitor will not
        touch it; and the startup broker-leg check SKIPS it ("PAPER record, no
        broker legs expected"), so the one sweep that could notice the
        contradiction is disabled by the same flag that causes it. On the
        first trigger the record leaves `entered` on a paper booking and the
        REAL legs stay open with no engine and no record in any open book.

        C5 fixed exactly this on the automated door and not on this one —
        [[feedback_the_copy_you_did_not_open]], on the paper/real boundary
        itself. The caller must now decide, and `cmd_enter` decides it from
        the BROKER, which it was already reading and printing.
        """
        if 'paper' in entry_data:
            t['paper'] = bool(entry_data['paper'])
        lot_size = int(entry_data['lot_size'])
        lots = int(entry_data['lots'])
        quantity = lot_size * lots
        debit = float(entry_data['debit'])
        capital = round(debit * quantity, 2)
        entry_spot = float(entry_data.get('entry_spot', t.get('trigger_spot',
                                          t.get('signal_price'))))

        direction = t['direction']
        # Spot SL: adverse direction from entry_spot
        spot_sl_pct = float(entry_data.get('spot_sl_pct', cfg.SPOT_SL_PCT))
        if direction == 'CE':
            sl_spot = round(entry_spot * (1 - spot_sl_pct), 2)
        else:
            sl_spot = round(entry_spot * (1 + spot_sl_pct), 2)

        # TP: a swing level in the way (if the caller found one), else the ST
        # line, else the short strike. The override is computed in the monitor
        # because it needs candles and a kite handle; it arrives already
        # validated as lying strictly between spot and ST, so it can only ever
        # SHORTEN the target. See zebra/history.py:swing_tp.
        tp_target = cfg.TP_TARGET
        base = (float(entry_data['short_strike']) if tp_target == 'short_strike'
                else float(t['st_value']))
        if entry_data.get('tp_spot') is not None:
            # The LIVE ticket can quote a swing-shortened target, but this path
            # had no way to receive it — so the monitor watched the ST line
            # while the owner had been told a nearer level. The TP then fired
            # late, or never, in exactly the case the feature exists for
            # (price stalling at its own support short of the magnet).
            tp_spot = float(entry_data['tp_spot'])
            t['tp_source'] = 'manual'
        else:
            tp_spot = self._resolve_tp(
                t, entry_data.get('swing_tp'), base,
                'short_strike' if tp_target == 'short_strike' else 'st_line')

        debit_sl_value = round(debit * cfg.DEBIT_SL_PCT, 2)

        # DTE
        try:
            exp_date = datetime.strptime(entry_data['expiry'], '%Y-%m-%d')
            dte = (exp_date.date() - datetime.now().date()).days
        except Exception:
            dte = None

        now = datetime.now()
        t['status'] = 'entered'
        t['entry_date'] = now.strftime('%Y-%m-%d')
        t['entry_time'] = now.strftime('%H:%M:%S')
        t['entry_spot'] = entry_spot
        t['long_strike'] = float(entry_data['long_strike'])
        t['short_strike'] = float(entry_data['short_strike'])
        t['long_symbol'] = entry_data['long_symbol']
        t['short_symbol'] = entry_data['short_symbol']
        t['debit'] = debit
        t['lot_size'] = lot_size
        t['lots'] = lots
        t['quantity'] = quantity
        t['capital'] = capital
        t['expiry'] = entry_data['expiry']
        t['dte_at_entry'] = dte
        t['tp_spot'] = tp_spot
        t['sl_spot'] = sl_spot
        t['spot_sl_pct'] = spot_sl_pct
        t['debit_sl_value'] = debit_sl_value
        t['debit_sl_pct'] = cfg.DEBIT_SL_PCT
        # WHICH STRUCTURE, and therefore which valuation formula, for the rest
        # of the position's life. Absent, `_long_multiplier` falls back to 2
        # (zebra) and a hand-entered BCS is marked at `2*long - short`: the
        # debit SL never fires because the value reads roughly double, the
        # trail never arms because it needs a width, and the P&L is twice the
        # truth. `width` is derived here rather than passed so the two can
        # never disagree.
        # Only 'bcs' is stamped. A zebra stays unmarked, which is the existing
        # convention every reader already keys on (`structure != 'bcs'`), and
        # leaving it alone keeps this fix to the one case that was broken.
        structure = entry_data.get('structure') or 'zebra'
        if structure == 'bcs':
            t['structure'] = 'bcs'
            t['width'] = round(abs(float(entry_data['short_strike'])
                                   - float(entry_data['long_strike'])), 2)
        # Entry-time extrinsic of the short leg — feeds the intrinsic-floor
        # quote-sanity guard in the monitor (bad-quote false-SL protection).
        # Absent, the floor falls back to a flat 30%-of-debit guess, so the
        # NHPC-class garbage-quote protection runs degraded for the life of the
        # position. This is the LIVE path, where that guard matters most.
        if 'short_extrinsic_entry' in entry_data:
            t['short_extrinsic_entry'] = float(entry_data['short_extrinsic_entry'])
        # THE ENTRY BOOK, PER LEG. Zebra rows carried only the net `debit`,
        # which is not enough to cost the trade: STT is levied per leg, on that
        # leg's premium, so 179 of the 215 closed records simply cannot be
        # charged correctly and never will be. The BCS path has stored this
        # since fill pricing; the zebra path never did, and the gap only
        # surfaced when the two-month cohort needed a NET number.
        #
        # Same lesson as `exit_legs`: an option book cannot be reconstructed
        # after the fact. Write it down at the moment or the post-mortem is
        # guesswork forever.
        for _k in ('long_bid_entry', 'long_ask_entry', 'long_mid_entry',
                   'short_bid_entry', 'short_ask_entry', 'short_mid_entry'):
            if entry_data.get(_k) is not None:
                t[_k] = float(entry_data[_k])
        # WHICH PRICE CONVENTION this position is valued on, for the rest of
        # its life. `_structure_quote` reads it and falls through to MID when
        # absent — so a hand-entered LIVE trade, whose `debit` is what the
        # owner actually PAID (ask - bid), was then marked mid-mid every poll.
        # The debit SL is `mid <= 0.5 * debit`: measuring a fill-basis debit
        # against a mid-basis value fires the stop LATE by roughly half the
        # round-trip spread, and books exits at a price nobody could transact
        # at. The automated path has stamped this since fill pricing shipped;
        # this one never did. Stamped at entry and never changed afterwards —
        # flipping it under an open position would move its stop and trail
        # levels beneath it.
        basis = entry_data.get('pricing_basis')
        if basis:
            t['pricing_basis'] = basis
        self._stamp_cohort(t)
        t['version'] = t.get('version', 0) + 1

    def add_bcs_shadow(self, zebra_trade: dict, bcs: dict) -> dict:
        """Create a paper BCS trade shadowing a just-entered zebra trade.

        Born directly as 'entered' (it has no watching/triggered life of its
        own — the zebra signal already did that). `bcs` is the dict returned
        by strikes.analyze_bcs plus 'expiry' and 'entry_spot' set by caller.
        Tagged structure='bcs' + shadow_of=<zebra id> so reports can pair the
        A/B legs; excluded from all scanner dedup.
        """
        lot_size = int(bcs['lot_size'])
        lots = _lots_from(bcs)
        quantity = lot_size * lots
        debit = float(bcs['debit'])
        entry_spot = float(bcs['entry_spot'])
        direction = zebra_trade['direction']

        if direction == 'CE':
            sl_spot = round(entry_spot * (1 - cfg.SPOT_SL_PCT), 2)
        else:
            sl_spot = round(entry_spot * (1 + cfg.SPOT_SL_PCT), 2)

        try:
            exp_date = datetime.strptime(bcs['expiry'], '%Y-%m-%d')
            dte = (exp_date.date() - datetime.now().date()).days
        except Exception:
            dte = None

        now = datetime.now()
        # _next_id() + append must be inside the lock, or two processes racing
        # a shadow creation would hand the same id to two different trades.
        with self._mutate():
            trade = self._build_bcs_shadow(zebra_trade, bcs, now, lot_size, lots,
                                           quantity, debit, entry_spot,
                                           direction, sl_spot, dte)
            self._trades.append(trade)
        logger.info(
            "BCS SHADOW #%d (of #%d) %s %s %g/%g debit=%.2f qty=%d d/w=%s%%",
            trade['id'], zebra_trade['id'], trade['stock'], direction,
            trade['long_strike'], trade['short_strike'], debit, quantity,
            trade.get('debit_to_width_pct')
        )
        return trade

    @staticmethod
    def _bcs_entry_fields(bcs, now, lot_size, lots, quantity, debit,
                          entry_spot, sl_spot, dte, tp_spot) -> dict:
        """Every field that makes a record a BCS position.

        Shared by the shadow builder and by mark_entered_bcs so `structure`
        (and `width`, which the trail and the intrinsic floor both read) is
        stamped in ONE place. It used to live only in the shadow builder: a
        BCS promoted through any other path would have carried no `structure`
        key, been treated as a 2-long zebra by `_long_multiplier`, and had
        every structure value — quote, floor, P&L — doubled.
        """
        return {
            'status': 'entered',
            'structure': 'bcs',
            # ── The ONLY place a position stops being paper ──────────────
            #
            # `zebra/scanner.py` stamps `paper: True` on every signal and,
            # until 2026-08-27, nothing ever flipped it: the flag was
            # vestigial, and the money path could not tell a paper record
            # from a real one. That matters because the owner's decision is
            # that paper keeps running through go-live — so open paper
            # positions WILL exist when the switches flip, and the live exit
            # bridge would otherwise adopt them and place real closing orders
            # against records that have no legs at any broker.
            #
            # `placed_at_broker` is set by `zebra/monitor._auto_enter_bcs`
            # ONLY after `entry_executor.open_spread` reports filled lots on
            # a non-dry run. It is deliberately NOT `already_filled` (which
            # means "the budget gate may no longer refuse this") — one key,
            # one meaning, because these two will diverge the first time
            # anything else needs to bypass the budget.
            'paper': not bool(bcs.get('placed_at_broker')),
            'entry_date': now.strftime('%Y-%m-%d'),
            'entry_time': now.strftime('%H:%M:%S'),
            'entry_spot': entry_spot,
            'long_strike': float(bcs['long_strike']),
            'short_strike': float(bcs['short_strike']),
            'long_symbol': bcs['long_symbol'],
            'short_symbol': bcs['short_symbol'],
            'debit': debit,
            'lot_size': lot_size,
            'lots': lots,
            'quantity': quantity,
            'capital': round(debit * quantity, 2),
            'expiry': bcs['expiry'],
            'dte_at_entry': dte,
            'tp_spot': tp_spot,
            'sl_spot': sl_spot,
            'spot_sl_pct': cfg.SPOT_SL_PCT,
            'debit_sl_value': round(debit * cfg.DEBIT_SL_PCT, 2),
            'debit_sl_pct': cfg.DEBIT_SL_PCT,
            'width': float(bcs['width']),
            'debit_to_width_pct': bcs.get('debit_to_width_pct'),
            # ── the entry books, persisted (2026-08-12) ──────────────────
            # Records used to keep only the derived `debit`, so the gap
            # between quoted and fillable could never be measured after the
            # fact — 42 BCS records, zero with a book on them. That made the
            # entry-cost gate impossible to calibrate and left the
            # `illiquid_book` post-mortem tag with no entry-side evidence.
            'pricing_basis': bcs.get('pricing_basis', 'mid'),
            'debit_mid': bcs.get('debit_mid'),
            'entry_cost': bcs.get('entry_cost'),
            'entry_cost_pct': bcs.get('entry_cost_pct'),
            'debit_to_width_pct_mid': bcs.get('debit_to_width_pct_mid'),
            'long_ask_entry': bcs.get('long_ask'),
            'long_bid_entry': bcs.get('long_bid'),
            'long_mid_entry': bcs.get('long_mid'),
            'short_ask_entry': bcs.get('short_ask'),
            'short_bid_entry': bcs.get('short_bid'),
            'short_mid_entry': bcs.get('short_mid'),
            'long_oi_entry': bcs.get('long_oi'),
            'short_oi_entry': bcs.get('short_oi'),
            # ── DEPTH AT THE TOUCH, persisted (2026-08-30) ───────────────
            # The same argument as the entry books above, one field over.
            # `capital.liquidity_lots` sizes the position from exactly these
            # two numbers -- they are the only thing standing between "3 lots"
            # and an order bigger than the book -- and NOTHING kept them. 13
            # cohort records, zero with depth on them, so "how many lots would
            # the touch have absorbed" cannot be answered for a single
            # historical entry, and the lot ladder cannot be calibrated from
            # anything but argument.
            #
            # OI is not a substitute and must not be used as one: it counts
            # open contracts, not resting size at the touch. The cohort's
            # thinner-leg OI runs 8,550 to 2.1M, which says nothing about
            # whether one lot fills without walking the book.
            'long_ask_qty_entry': bcs.get('long_ask_qty'),
            'short_bid_qty_entry': bcs.get('short_bid_qty'),
            # ── WHICH LIMIT DECIDED THE SIZE ─────────────────────────────
            # `capital.plan` returns every limit's own answer and says so in
            # its docstring: "a size is a decision, and '3 lots' with no
            # record of what the other four limits said cannot be audited
            # after the fact". It was computed on every triggered signal and
            # spent on a log line. Now it rides on the record, so the question
            # "what actually binds as capital grows" is answered from the book
            # instead of re-derived.
            'entry_plan': bcs.get('entry_plan'),
            'short_extrinsic_entry': float(bcs.get('short_extrinsic', 0)
                                           or bcs.get('short_extrinsic_entry', 0)),
            'entry_warnings': bcs.get('warnings', []),
        }

    def _build_bcs_shadow(self, zebra_trade, bcs, now, lot_size, lots, quantity,
                          debit, entry_spot, direction, sl_spot, dte) -> dict:
        """Assemble the shadow record. Called INSIDE the store lock so that
        _next_id() sees every trade another process may have just added."""
        trade = {
            'id': self._next_id(),
            'version': 1,
            'shadow_of': zebra_trade['id'],
            'stock': zebra_trade['stock'],
            'timeframe': zebra_trade['timeframe'],
            'direction': direction,
            'st_value': zebra_trade['st_value'],
            'st_direction': zebra_trade['st_direction'],
            'signal_price': zebra_trade['signal_price'],
            'signal_gap_pct': zebra_trade['signal_gap_pct'],
            'signal_date': zebra_trade.get('signal_date'),
            'signal_time': zebra_trade.get('signal_time'),
            # `paper` is NOT set here. `_bcs_entry_fields` below owns it for
            # every record that becomes a position, so the two cannot drift;
            # a shadow is paper by construction anyway (nothing places it).
            'notes': f"BCS shadow of zebra #{zebra_trade['id']}",
        }
        trade.update(self._bcs_entry_fields(
            bcs, now, lot_size, lots, quantity, debit, entry_spot, sl_spot, dte,
            float(zebra_trade.get('tp_spot', zebra_trade['st_value']))))
        return trade

    @staticmethod
    def _resolve_tp(t: dict, swing, base: float, base_name: str) -> float:
        """TP: a swing level standing in the way, else the usual target.

        Shared by the zebra and BCS entry paths. They derived their TP
        independently before this, which is exactly how the BCS path — the one
        that actually trades — ends up quietly missing a feature.

        `swing` arrives already validated as lying strictly between spot and
        the ST line (zebra/history.py:swing_tp), so this can only ever SHORTEN
        the target. The original is kept on the record: a trade whose TP was
        moved has to be reviewable against the target it would otherwise have
        had, or the shortening can never be scored.
        """
        if isinstance(swing, dict) and swing.get('tp_spot'):
            t['tp_source'] = swing.get('kind', 'swing')
            t['tp_swing'] = swing
            t['tp_st_line'] = float(t['st_value'])
            return float(swing['tp_spot'])
        t['tp_source'] = base_name
        return float(base)

    def _stamp_cohort(self, t: dict) -> None:
        """Mark which engine opened this position. Stamped once, at entry.

        Derived from the config at ENTRY time and then frozen, rather than
        recomputed from `entry_date` on every read. Two reasons, both learned
        the hard way on `pricing_basis`:

        - Moving `cohort_start` later must not silently reclassify positions
          that are already open and already being measured.
        - `entry_date` is a display string; a cohort test that re-parses it on
          every read is one date-format change away from silently returning
          False for the whole book.
        """
        t['cohort'] = cfg.COHORT_START

    def _refuse_if_over_budget(self, trade_id: int,
                               candidate: dict = None,
                               already_filled: bool = False) -> None:
        """Every portfolio-level limit, in one place. MUST hold the lock.

        Was `_refuse_if_book_full`, and it checked one thing: a COUNT.
        `MAX_OPEN_TRADES` had been defined in config, asserted by a test and
        read by NOTHING -- the only portfolio control in the system was
        decorative, and the paper book duly reached 17 simultaneous positions
        against a stated cap of 8. Fixing that left the other half untouched:
        eight positions is not a budget, and measured on the cohort the book
        held Rs 81,291 across 7 positions with no rupee limit anywhere.
        `zebra.capital` now answers all of it.

        `candidate` prices the position being opened. Omitted (the legacy
        call), only the count and per-stock limits can bind -- the rupee ones
        need to know what is being bought.

        LIVE refuses. PAPER evaluates and LOGS what it WOULD have refused,
        which is the point:

        * capping paper entries would bias which trades the validation record
          contains, and an unentered paper signal costs nothing -- the reason
          the old exemption existed, and still right;
        * but an exemption that skips the check entirely means the capital
          layer has never run when it first becomes load-bearing. That is the
          exit bridge's mistake in a different file. Shadow-logging gives an
          unbiased record AND evidence, and the WOULD REFUSE lines are how the
          rupee numbers get chosen from data instead of guessed.

        Raises ValueError, which every caller already treats as "did not
        enter" (the monitor logs and leaves the row triggered; the CLI prints
        and exits non-zero). Refusing to open is always safe.
        """
        cand = dict(candidate or {})
        cand.setdefault('stock', (self.find(trade_id) or {}).get('stock'))
        if candidate is None:
            # Nothing to price. Ask only the limits that do not need a number,
            # rather than fail the unpriceable-candidate rule and refuse every
            # legacy entry.
            cand.setdefault('debit', 0.0)
            cand.setdefault('quantity', 1)
        ok, why = capital.check(self._trades, cand)
        if ok:
            return
        if already_filled:
            # THE MONEY IS ALREADY SPENT. This gate exists to prevent OPENING
            # a position, never to prevent recording one that is already at
            # the broker -- and refusing here does not undo the trade, it only
            # loses the record, leaving a live position nothing is watching.
            # That is the precise failure this whole design is built to avoid,
            # produced by a risk control.
            #
            # It is reachable in ordinary operation: `plan()` sizes against the
            # QUOTED debit and the record carries the PAID one, which is higher
            # by construction because entry crosses the touch.
            logger.error(
                'CAPITAL BREACHED #%s %s: %s -- recording it ANYWAY because '
                'the orders have already filled. Reduce or close manually. %s',
                trade_id, cand.get('stock'), why,
                capital.describe(self._trades))
            return
        if cfg.PAPER_MODE:
            logger.warning(
                'CAPITAL WOULD REFUSE #%s %s: %s -- entered anyway (paper '
                'keeps the record unbiased). %s',
                trade_id, cand.get('stock'), why, capital.describe(self._trades))
            return
        raise ValueError(f"#{trade_id} refused: {why}. Close something or "
                         f"raise the limit deliberately.")

    def mark_entered_bcs(self, trade_id: int, bcs: dict) -> dict:
        """Promote a signal straight into a BCS position — ONE record.

        The BCS-only pipeline (2026-08-12). Where `add_bcs_shadow` creates a
        second record shadowing a zebra, this turns the signal itself into the
        position: no `shadow_of`, so it dedups like any other open trade, and
        the scanner will not hand out a duplicate on the same thesis.
        """
        lot_size = int(bcs['lot_size'])
        lots = _lots_from(bcs)
        quantity = lot_size * lots
        debit = float(bcs['debit'])
        entry_spot = float(bcs['entry_spot'])

        with self._mutate():
            t = self._must_find(trade_id)
            if t['status'] not in ('watching', 'triggered'):
                raise ValueError(f"#{trade_id} status={t['status']}, can't enter")
            self._refuse_if_over_budget(trade_id, {
                'stock': t.get('stock'), 'debit': debit,
                'lot_size': lot_size, 'lots': lots},
                already_filled=bool(bcs.get('already_filled')))
            direction = t['direction']
            sl_spot = round(entry_spot * (1 - cfg.SPOT_SL_PCT), 2) \
                if direction == 'CE' else \
                round(entry_spot * (1 + cfg.SPOT_SL_PCT), 2)
            try:
                exp_date = datetime.strptime(bcs['expiry'], '%Y-%m-%d')
                dte = (exp_date.date() - datetime.now().date()).days
            except Exception:
                dte = None
            t.update(self._bcs_entry_fields(
                bcs, datetime.now(), lot_size, lots, quantity, debit,
                entry_spot, sl_spot, dte,
                self._resolve_tp(t, bcs.get('swing_tp'),
                                 float(t.get('tp_spot', t['st_value'])),
                                 'st_line')))
            self._stamp_cohort(t)
            t['version'] = t.get('version', 0) + 1
        logger.info(
            "ENTERED BCS #%d %s %g/%g debit=%.2f qty=%d cap=Rs%.0f d/w=%s%% "
            "TP=%.2f SL=%.2f",
            trade_id, t['stock'], t['long_strike'], t['short_strike'],
            debit, quantity, t['capital'], t.get('debit_to_width_pct'),
            t['tp_spot'], t['sl_spot'])
        return t

    def mark_exited(self, trade_id: int, exit_spot: float,
                    exit_debit: Optional[float],
                    reason: str, exit_legs: Optional[dict] = None,
                    approximate: bool = False) -> dict:
        """Close an entered trade. exit_debit = closing net debit per share
        (positive if still costs money to close, negative if closes for credit)."""
        with self._mutate():
            t = self._must_find(trade_id)
            # Status check inside the lock is what makes the exit idempotent
            # across processes: if the other cron already closed this trade,
            # we see 'exited' here and refuse, instead of double-booking a
            # close on stale in-memory state.
            #
            # 'closing' IS accepted, and that is the whole point of the state.
            # `begin_close` persists it BEFORE any order leaves for the broker,
            # so the order path's own book-the-exit call arrives here with
            # status='closing' every single time. Requiring 'entered' made
            # `mark_exited` raise on 100% of bridged closes: the caller
            # (`bcs/spread_monitor.py`) caught it, froze the record at
            # `partial_close` and Telegrammed "manual intervention needed" —
            # after the legs were already flat at the broker. The paper path
            # (`zebra/monitor.py`) books straight from 'entered' and never saw
            # it, which is why this survived a review.
            #
            # 'exited' stays refused. That is the idempotence guarantee and it
            # is unaffected: a second booking of the same close is still a
            # double-book, whichever state it came from.
            # The table, not a literal. `common/store_contract.py` holds the
            # rules and this store supplies its own vocabulary for them --
            # the four books use different words for the same four states,
            # so comparing raw status strings would compare the wrong things.
            if not store_contract.allows(store_contract.UPDATE_TRADE_EXIT,
                                         t['status'], store_contract.ZEBRA_STATUSES):
                raise ValueError(store_contract.refusal(
                    store_contract.UPDATE_TRADE_EXIT, t['status'], trade_id,
                    store_contract.ZEBRA_STATUSES))
            self._apply_exit(t, exit_spot, exit_debit, reason, exit_legs,
                             approximate=approximate)
        return t

    @staticmethod
    def _bound_exit_value(t: dict, exit_debit: Optional[float]) -> Optional[float]:
        """Hold the booked value inside what the structure can be worth.

        The monitor already rejects impossible quotes, so in the automated path
        this should never bind. It exists because this is the LAST place every
        exit passes through, including `zebra close` where a human types the
        number, and because the invariant is worth enforcing where the record
        is written rather than trusting six callers to have checked.

        Bounds: a debit structure is never worth less than zero (you can always
        let it expire), and a vertical is never worth more than its width.
        PIIND #50 booked exit_debit -30.04 on a debit of 242.11 — -112.4% on a
        structure whose arithmetic floor is -100% — because nothing enforced
        the lower bound anywhere.
        """
        if exit_debit is None:
            return None
        try:
            v = float(exit_debit)
        except (TypeError, ValueError):
            return exit_debit
        lo = 0.0
        hi = None
        if t.get('structure') == 'bcs':
            try:
                w = float(t.get('width') or 0)
            except (TypeError, ValueError):
                w = 0.0
            if w > 0:
                hi = w
        bounded = v
        if bounded < lo:
            bounded = lo
        if hi is not None and bounded > hi:
            bounded = hi
        if bounded != v:
            logger.error(
                "EXIT VALUE OUT OF RANGE #%s %s: %.2f -> %.2f "
                "(structure=%s width=%s) — booking the bound, not the quote",
                t.get('id'), t.get('stock'), v, bounded,
                t.get('structure') or 'zebra', t.get('width'))
        return bounded

    def _apply_exit(self, t: dict, exit_spot: float,
                    exit_debit: Optional[float], reason: str,
                    exit_legs: Optional[dict] = None,
                    approximate: bool = False) -> None:
        """Field-level exit mutation — runs inside the store lock.

        `approximate` is set when the producer knows the figure is not exact —
        a bridged BCS close that found one leg already flat counts it at 0.00,
        so its P&L is wrong in a KNOWN direction. It rides in here rather than
        being written by a follow-up call so the marker and the number it
        qualifies land in the SAME locked write; a second write could fail and
        leave an approximation on the record reading as exact (N14).
        """
        debit = float(t['debit'])
        qty = int(t['quantity'])
        exit_debit = self._bound_exit_value(t, exit_debit)
        if exit_debit is not None:
            # P&L per share = current value of structure - entry debit
            # If user closed at exit_debit (i.e., paid that much to unwind),
            # then they recovered (debit - exit_debit). But Zebra is a debit
            # trade: the structure has POSITIVE value when in profit, negative
            # of debit when worst case. exit_debit here = current mark of the
            # structure (long_value*2 - short_value), so P&L = exit_debit - debit.
            # We document this convention in monitor.
            pnl_per_share = float(exit_debit) - debit
        else:
            # Worst case: structure went to -debit (max loss)
            pnl_per_share = -debit
        pnl = round(pnl_per_share * qty, 2)
        pnl_pct = round((pnl_per_share / debit) * 100, 2) if debit > 0 else 0

        now = datetime.now()
        t['status'] = 'exited'
        t['exit_date'] = now.strftime('%Y-%m-%d')
        t['exit_time'] = now.strftime('%H:%M:%S')
        t['exit_spot'] = exit_spot
        t['exit_debit'] = exit_debit
        t['pnl'] = pnl
        t['pnl_pct'] = pnl_pct
        # Absent means EXACT; the key is never written False. ~450 records
        # predate the marker and defaulting those to approximate would put a
        # caveat on every line, which is how a caveat stops being read.
        if approximate:
            t['exit_approximate'] = True
        # THE BOOK WE EXITED ON. Entry books have been persisted since fill
        # pricing landed; exits kept only the two scalars, so the one direction
        # that has twice cost real money (ICICI Feb, NHPC Jul) was also the one
        # direction with no evidence. An option book cannot be reconstructed
        # after the fact -- if it is not written down at the moment of the
        # exit, the post-mortem is guesswork forever.
        #
        # WRITTEN BEFORE THE FEE STAMP, deliberately. `round_trip_for_trade`
        # reads `t['exit_legs'][side]['price']` and falls back to scaling the
        # ENTRY legs by the structure's decay when it cannot find one. This
        # assignment used to sit below the stamp, so the costing never saw the
        # book even when the book held real fills -- every bridged exit would
        # have been costed by the estimate meant for the 208 historical records
        # that have no leg prices at all. The paper run exists to answer
        # whether this strategy clears its own costs; estimating a cost that is
        # known is the one avoidable way to get that answer wrong.
        if exit_legs:
            t['exit_legs'] = exit_legs
        # NET OF CHARGES, beside the gross figure and never replacing it.
        # The two-month paper run exists to answer one question — does this
        # strategy clear its own costs — and the measured baseline says the
        # median trade is +0.90% gross but -0.79% NET. A cohort scored on
        # `pnl_pct` alone looks fine while losing money.
        #
        # `pnl` keeps its meaning so every earlier record stays comparable;
        # mixing the two definitions is the mistake `band_basis` exists to
        # prevent on the magnet stat. Best-effort: a costing failure must
        # never be able to stop a position being closed.
        try:
            from . import fees as fees_mod
            cost = fees_mod.round_trip_for_trade(t, exit_debit)
            t['fees'] = cost
            t['pnl_net'] = round(pnl - float(cost.get('total') or 0), 2)
            t['pnl_net_pct'] = (round((t['pnl_net'] / (debit * qty)) * 100, 2)
                                if debit > 0 and qty > 0 else 0)
        except Exception as e:
            logger.warning("fee stamp failed for #%d: %s — gross P&L stands",
                           t.get('id'), e)
        t['exit_reason'] = reason
        t['version'] = t.get('version', 0) + 1
        logger.info(
            "EXITED #%d %s reason=%s spot=%.2f P&L=Rs%.0f (%.1f%%)",
            t['id'], t['stock'], reason, exit_spot, pnl, pnl_pct
        )

    # ── The close lock ───────────────────────────────────────────────────
    #
    # Added 2026-08-26 so `bcs/spread_monitor.py` can manage cohort exits with
    # a real order. `mark_exited` is already idempotent inside `_mutate`, which
    # stops a position being BOOKED twice — but booking is not the dangerous
    # half. Two processes can both read `entered`, both place a closing order,
    # and only then does one of them lose the status check. The Feb-2026
    # incident was four unwanted BUYs on one short leg; this is the same shape.
    #
    # So the lock is taken and PERSISTED before any order goes out, and the
    # status it writes — 'closing' — is a new state for this schema. Reads that
    # ask for 'entered' will stop seeing a trade the moment a close begins,
    # which is the intended behaviour everywhere: the paper monitor must not
    # re-alert on a position the order path is already unwinding.

    def begin_close(self, trade_id: int, reason: str) -> bool:
        """Take the close lock. True if acquired, False if not ours to take.

        False is the normal, expected answer when another process got there
        first. It is not an error and must not be logged as one.
        """
        with self._mutate():
            t = self._must_find(trade_id)
            if not store_contract.allows(store_contract.BEGIN_CLOSE,
                                         t['status'], store_contract.ZEBRA_STATUSES):
                logger.info("#%d status=%s — close lock not taken (%s)",
                            trade_id, t['status'], reason)
                return False
            t['status'] = 'closing'
            t['close_reason'] = reason
            t['close_started'] = datetime.now().isoformat()
            t['version'] = t.get('version', 0) + 1
            logger.info("#%d close lock acquired: %s", trade_id, reason)
            return True

    def recover_closing_trade(self, trade_id: int) -> bool:
        """Put a stranded 'closing' record back to 'entered'.

        A process that dies between taking the lock and booking the exit
        leaves the trade locked forever: no engine will touch it again, and it
        silently stops being managed — the failure mode that has actually cost
        money here, twice. Recovery is deliberately a separate, explicit call
        rather than a timeout, because "the close may have partly filled" is
        not something a clock can decide.
        """
        with self._mutate():
            t = self._must_find(trade_id)
            if not store_contract.allows(store_contract.RECOVER_CLOSING,
                                         t['status'], store_contract.ZEBRA_STATUSES):
                return False
            t['status'] = 'entered'
            t.pop('close_reason', None)
            t.pop('close_started', None)
            t['version'] = t.get('version', 0) + 1
            logger.warning("#%d recovered from 'closing' back to 'entered' — "
                           "verify at the broker whether legs were closed",
                           trade_id)
            return True

    def update_trade_fields(self, trade_id: int, **fields) -> dict:
        """Patch arbitrary fields on a record. Used for trail state.

        Refuses to write `status` — that has dedicated transitions which do
        the version bump and the logging, and letting it through here would
        make the state machine editable from anywhere.
        """
        if 'status' in fields:
            raise ValueError("use the status transitions, not "
                             "update_trade_fields, to change status")
        with self._mutate():
            t = self._must_find(trade_id)
            t.update(fields)
            t['version'] = t.get('version', 0) + 1
        return t

    def begin_recovery(self, trade_id: int, reason: str) -> bool:
        """M14 - take the close-lock on a FROZEN record so recovery can finish it.

        The zebra twin of the BCS-family method, and the ONE door out of
        `partial_close`. Deliberately NOT folded into `begin_close`: that
        method's status check is the cross-process concurrency lock, and
        widening it to accept `partial_close` would weaken it for every
        ordinary close in order to serve a rare one.

        `entered` belongs to `begin_close`; `closing` means an attempt is
        already in flight and a second would be the Feb-2026 2x-order shape;
        `exited` is terminal. Returns False rather than raising - "somebody
        else got there first" is an ordinary answer on a shared store.
        """
        with self._mutate():
            t = self._must_find(trade_id)
            # The exact inverse of `begin_close`, stated as data.
            if not store_contract.allows(store_contract.BEGIN_RECOVERY,
                                         t['status'], store_contract.ZEBRA_STATUSES):
                logger.warning("#%d status=%s, cannot begin recovery",
                               trade_id, t['status'])
                return False
            t['status'] = 'closing'
            t['close_reason'] = reason
            t['close_started'] = datetime.now().isoformat()
            t['version'] = t.get('version', 0) + 1
            self._sync_locked = True
            logger.info("#%d recovery-lock acquired: %s", trade_id, reason)
            return True

    def get_frozen_trades(self) -> list:
        """Records stuck at `partial_close` - live legs, nothing monitoring them.

        `get_entered` skips them, `get_closing_trades` skips them, so before
        M14 nothing in this store could name them at all. An unmonitored live
        position nobody is told about is the failure that has cost real money
        here twice.

        Read-only, and NOT cohort-filtered - the adapter narrows to the cohort
        (`bcs/zebra_adapter.py`). A store method that silently dropped
        non-cohort frozen records would hide the older generation's freezes
        from every reader that goes direct.
        """
        # WHOLE BOOK: a frozen record has legs LIVE at the broker. Which
        # engine opened it is irrelevant to whether it needs recovering, and
        # scoping this would strand exactly the records that most need
        # finding. A safety sweep, not a measurement.
        return [t for t in self.load_trades()
                if t.get('status') == 'partial_close']

    def get_residue_trades(self) -> list:
        """S3 - records BOOKED CLOSED that still show a live leg at the broker.

        `reconcile_after_close` reads the broker's own view after a close
        reports success. When it finds a leg that is not flat, the record is
        already `exited`: it is out of the open book, out of
        `get_frozen_trades()`, and out of every sweep there is. Before this
        method the entire lifecycle of that fact was ONE Telegram — the same
        invisible-position shape M14 exists to end, one door over.

        Deliberately NOT the frozen list. A frozen record has a watcher (the
        recovery sweep) and a nag of its own; a second one for the same
        position would be noise. This names only the records nothing else can
        see, which is why the status filter is part of the query rather than
        left to the caller.

        Read-only, and terminal: no caller may place an order on the strength
        of this list. The record is closed — there is no close lock to take,
        no stop to re-arm, and the residue may be a leg the owner is holding
        on purpose. Escalate, never act.

        WHOLE BOOK: not cohort-filtered, for the same reason
        `get_frozen_trades` is not — the adapter narrows, and a store method
        that hid the older generation's residues from every direct reader
        would be answering a different question than the one it is named for.
        A residue is an unaccounted leg at a broker; which engine opened it
        does not change that.
        """
        return [t for t in self.load_trades()
                if t.get('status') == 'exited'
                and (t.get('reconcile_residue') or {}).get('state') == 'open']

    def get_entry_residue_trades(self) -> list:
        """Records carrying an ENTRY residue — a leg an entry left behind.

        The entry-side twin of `get_residue_trades`, and it exists for the
        same reason: `bcs/entry_executor.py` never unwinds an orphan leg (a
        corrective order through the book that just failed to fill is how a
        Feb-2026 stop became a four-fill loss), so it REPORTS one and stops.
        Until this method existed "reports" meant one Telegram: the orphan was
        in no store, so the frozen sweep, the residue sweep, the startup
        verification and `--list` all missed it, because every one of them
        reads RECORDS.

        NO status filter, and that is the difference from the post-close twin.
        An entry residue can sit on a record in any state: `entered` when the
        round completed some spreads and orphaned a leg, or the pre-entry
        state when nothing filled at all and the record never became a
        position. The incident, not the status, is the query.

        Read-only and escalate-only. No caller may place an order on the
        strength of this list.

        WHOLE BOOK: as with the two sweeps above. An orphan leg is an orphan
        leg whichever engine placed it.
        """
        return [t for t in self.load_trades()
                if (t.get('entry_residue') or {}).get('state') == 'open']

    def set_trade_status(self, trade_id: int, status: str, **extra) -> dict:
        with self._mutate():
            t = self._must_find(trade_id)
            t['status'] = status
            t.update(extra)
            t['version'] = t.get('version', 0) + 1
        return t

    def cancel(self, trade_id: int, reason: str) -> dict:
        """Cancel a watching/triggered signal."""
        with self._mutate():
            t = self._must_find(trade_id)
            if t['status'] not in ('watching', 'triggered'):
                raise ValueError(f"#{trade_id} status={t['status']}, can't cancel")
            t['status'] = 'cancelled'
            t['cancelled_at'] = datetime.now().isoformat()
            t['cancel_reason'] = reason
            t['version'] = t.get('version', 0) + 1
        logger.info("CANCELLED #%d %s: %s", trade_id, t['stock'], reason)
        return t

    def update_gap(self, trade_id: int, current_gap_pct: float) -> dict:
        """Cheap update of last seen gap on a watching signal.

        In-memory only — no save, no Drive, no version bump, so it needs no
        lock. Purely advisory display state; if the other process overwrites
        it nothing is lost that matters.
        """
        t = self._must_find(trade_id)
        t['last_gap_pct'] = current_gap_pct
        t['last_gap_at'] = datetime.now().isoformat()
        return t

    def set_alert_flag(self, trade_id: int, kind: str,
                       persist: bool = True) -> bool:
        """Idempotent: set <kind>_alerted_at on the trade if not already set.

        Returns True if the flag was newly set (caller should fire the alert),
        False if it was already set (alert already fired in a previous cycle).
        This is the persistent replacement for in-memory dedup that survives
        cron restarts.

        THE read-check-write that most needs the lock. Two processes racing it
        can both observe the flag unset and both return True — meaning two
        exits, two alerts, or in a live context two orders. That is exactly the
        shape of the Feb ICICI bug that put 4x BUY orders on the short leg.
        Test-and-set is only atomic while the lock is held.
        """
        newly_set = False
        with self._mutate(drive=persist, persist=persist):
            t = self.find(trade_id)
            if t:
                key = f"{kind}_alerted_at"
                if not t.get(key):
                    t[key] = datetime.now().isoformat()
                    t['version'] = t.get('version', 0) + 1
                    newly_set = True
        return newly_set

    def set_alert_flag_daily(self, trade_id: int, kind: str,
                             persist: bool = True) -> bool:
        """Like set_alert_flag, but fires at most once per calendar day.

        Used for recurring reminders (e.g. expiry T-3..T-1 daily nag) so the
        user keeps getting nudged each day until they close the position.
        Returns True if the flag was set/refreshed today (fire the alert);
        False if it already fired today.
        """
        newly_set = False
        with self._mutate(drive=persist, persist=persist):
            t = self.find(trade_id)
            if t:
                key = f"{kind}_alerted_at"
                today_str = datetime.now().strftime('%Y-%m-%d')
                if not str(t.get(key, '')).startswith(today_str):
                    t[key] = datetime.now().isoformat()
                    t['version'] = t.get('version', 0) + 1
                    newly_set = True
        return newly_set

    def clear_alert_flag(self, trade_id: int, kind: str,
                         persist: bool = True) -> None:
        """Un-set an alert flag so the next cycle may fire it again.

        Exists for one narrow case: an alert whose flag was claimed but whose
        SEND then failed. Claiming before sending is deliberate (two processes
        must not both send), but without this the failed message is simply lost
        — which for the exit escalation means the human is never asked about a
        position the bot has decided to hold indefinitely.
        """
        with self._mutate(drive=persist, persist=persist):
            t = self.find(trade_id)
            if t and t.get(f"{kind}_alerted_at"):
                t[f"{kind}_alerted_at"] = None
                t['version'] = t.get('version', 0) + 1

    # ── Quote-reliability confirm / blind counters ─────────────────────────
    # Advisory per-trade state for the DEBIT-SL value trigger. Persisted to the
    # LOCAL file only (no Drive upload, no version bump) so it survives cron
    # restarts on the server without churning Drive every 5-min poll. On a
    # version-tie merge the local copy wins, so these fields are never lost.
    def bump_confirm(self, trade_id: int, kind: str, persist: bool = True) -> int:
        """Increment a trigger-confirmation counter, restarting a stale streak.

        A streak whose last hit is older than cfg.CONFIRM_STALE_SEC restarts
        from zero — confirming polls must be reasonably contiguous, but
        unreliable polls in between (which simply don't call this) may not
        indefinitely block a genuine exit. Mirrors bcs.bump_confirm.
        """
        count = 0
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t:
                key = f"{kind}_confirm"
                tkey = f"{kind}_confirm_t"
                now = time.time()
                if now - float(t.get(tkey, 0.0)) > cfg.CONFIRM_STALE_SEC:
                    t[key] = 0
                t[key] = int(t.get(key, 0)) + 1
                t[tkey] = now
                count = t[key]
        return count

    def reset_confirm(self, trade_id: int, kind: str, persist: bool = True) -> None:
        """Clear a confirmation counter (a reliable non-trigger poll)."""
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t and t.get(f"{kind}_confirm"):
                t[f"{kind}_confirm"] = 0
                t[f"{kind}_confirm_t"] = time.time()

    def bump_blind(self, trade_id: int, persist: bool = True) -> int:
        """Increment the consecutive unusable-quote cycle counter."""
        count = 0
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t:
                t['debit_blind_cycles'] = int(t.get('debit_blind_cycles', 0)) + 1
                count = t['debit_blind_cycles']
        return count

    def clear_blind(self, trade_id: int, persist: bool = True) -> None:
        """Reset blindness state on the first usable quote (re-arms the alert)."""
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t and (t.get('debit_blind_cycles') or t.get('debit_blind_alerted')):
                t['debit_blind_cycles'] = 0
                t['debit_blind_alerted'] = False

    def corroboration_ref(self, trade_id: int) -> dict:
        """The last spot/value pair a RELIABLE poll observed for this trade.

        Persisted (local only, like the confirm counters) because zebra's cron
        process exits between cycles: the live monitor keeps this reference in
        memory only, which it can afford as a long-lived process, but here an
        in-memory reference would reset every 5 minutes and the veto would
        never have anything to compare against.
        """
        t = self.find(trade_id) or {}
        return {'spot': t.get('corrob_spot'), 'value': t.get('corrob_value'),
                't': float(t.get('corrob_t') or 0.0)}

    # The reference is ADVANCED through the batched `apply_mfe` write, not by a
    # setter here: it updates once per open position per poll, and a write per
    # trade per cycle is exactly what the batching exists to prevent. Only
    # RELIABLE readings may advance it — a garbage read that became the
    # baseline would make the next genuine move look uncorroborated, i.e. veto
    # the exit that should happen.

    def mark_blind_alerted(self, trade_id: int, persist: bool = True) -> bool:
        """Set the blind-alert flag once per blind spell. True if newly set."""
        newly_set = False
        with self._mutate(drive=False, persist=persist):
            t = self.find(trade_id)
            if t and not t.get('debit_blind_alerted'):
                t['debit_blind_alerted'] = True
                newly_set = True
        return newly_set

    # ── Max favourable excursion ───────────────────────────────────────────
    def apply_mfe(self, patches: dict, persist: bool = True) -> None:
        """Persist peak-excursion state for many trades in ONE write.

        `patches` is {trade_id: {field: value}}. Batched deliberately: this
        fires for every open position on every poll, and a write per trade
        would rewrite the whole ~1 MB store that many times per cycle while
        holding the cross-process lock each time.

        LOCAL only (drive=False, no version bump), like the confirm/blind
        counters: pushing a peak to Drive every 5 minutes would churn the
        network for data nobody reads until the trade closes. The values still
        REACH Drive, because `mark_exited` does a full drive=True write and its
        in-lock refresh picks them up — so the record lands exactly when it
        becomes worth keeping.

        Callers pass whole-state patches, so the key check is not decoration:
        one typo'd key in a dict-update would silently overwrite `status` or
        `debit` on a live position.
        """
        bad = sorted({k for f in patches.values() for k in f
                      if not str(k).startswith('mfe_')
                      and k not in _BATCHED_POLL_FIELDS})
        if bad:
            raise ValueError(
                f"apply_mfe only writes mfe_* fields or {sorted(_BATCHED_POLL_FIELDS)}, "
                f"got: {bad}")
        if not patches:
            return
        with self._mutate(drive=False, persist=persist):
            for trade_id, fields in patches.items():
                t = self.find(trade_id)
                if t:
                    t.update(fields)

    # ── Listing ───────────────────────────────────────────────────────────
    def list_trades(self, status_filter: Optional[str] = None):
        trades = self._trades
        if status_filter:
            trades = [t for t in trades if t.get('status') == status_filter]
        if not trades:
            print("No trades found.")
            return
        print(f"\n{'ID':>3} {'Status':<10} {'Stock':<12} {'Dir':<4} "
              f"{'TF':<8} {'Strikes':<14} {'Debit':>7} {'TP':>9} {'SL':>9}  Notes")
        print("-" * 110)
        for t in trades:
            strikes = '-'
            if t.get('long_strike') and t.get('short_strike'):
                strikes = f"{int(t['long_strike'])}/{int(t['short_strike'])}"
            debit = f"{t.get('debit', 0):.2f}" if t.get('debit') else '-'
            tp = f"{t.get('tp_spot', 0):.2f}" if t.get('tp_spot') else '-'
            sl = f"{t.get('sl_spot', 0):.2f}" if t.get('sl_spot') else '-'
            notes = (t.get('notes') or t.get('exit_reason') or '')[:30]
            if t.get('structure') == 'bcs':
                notes = f"[BCS] {notes}"[:36]
            print(f"{t['id']:>3} {t.get('status', '?'):<10} {t.get('stock', '?'):<12} "
                  f"{t.get('direction', '?'):<4} {t.get('timeframe', '?'):<8} "
                  f"{strikes:<14} {debit:>7} {tp:>9} {sl:>9}  {notes}")
        print()

    # ── Sync ──────────────────────────────────────────────────────────────
    def maybe_sync(self, force: bool = False):
        if not self._drive_enabled:
            return
        elapsed = time.time() - self._last_sync_time
        if force or elapsed >= self._sync_interval:
            self._sync_from_drive()

    # ── Private ───────────────────────────────────────────────────────────
    def _next_id(self) -> int:
        """Allocate an id that has never been used, even after a quarantine.

        `max(id) + 1` over the live list is an allocator only while the list is
        COMPLETE. It is not, in the one case that matters: `_read_local`
        quarantines a corrupt file and returns `[]`, and if Drive is also down
        `initialize()` takes the local branch, so the store legitimately starts
        empty — and this handed out 1, 2, 3 again.

        That is silently destructive rather than merely confusing. `_merge` is
        keyed on `id` with the higher `version` winning, so once Drive returns,
        a recycled id whose version has outrun the original REPLACES a genuine
        trade. The original is then gone from disk (quarantined) and from Drive
        (overwritten) — the only surviving copy is the `.corrupt.*.json` backup
        nobody knows to look in.

        A monotonic high-water mark on disk makes the collision impossible. It
        is advisory: if it is missing or unreadable we fall back to the live max
        and are no worse off than before.
        """
        live = max((t.get('id', 0) for t in self._trades), default=0)
        nid = max(live, self._read_high_water()) + 1
        self._write_high_water(nid)
        return nid

    @staticmethod
    def _high_water_path():
        # Keyed to the STORE FILE, not to LOG_DIR: the sequence belongs to one
        # book. A directory-wide sidecar would make two store files share an id
        # space, so opening a second store would silently skip ids in the first.
        return cfg.LOCAL_FILE.with_suffix('.nextid.json')

    def _read_high_water(self) -> int:
        try:
            return int(json.loads(
                self._high_water_path().read_text()).get('max_id_ever') or 0)
        except Exception:
            return 0

    def _write_high_water(self, value: int) -> None:
        """Best-effort and never fatal — a trade must not fail to save because
        a bookkeeping sidecar could not be written."""
        try:
            if value <= self._read_high_water():
                return
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._high_water_path().write_text(
                json.dumps({'max_id_ever': int(value)}))
        except Exception as e:
            logger.warning("Could not persist the id high-water mark: %s", e)

    def _must_find(self, trade_id: int) -> dict:
        t = self.find(trade_id)
        if not t:
            raise ValueError(f"Trade #{trade_id} not found")
        return t

    def _init_drive(self, drive_cfg: dict):
        creds_path = _resolve_credentials_path(self._config)
        if not creds_path or not creds_path.exists():
            logger.warning("Drive credentials not found at %s, local-only", creds_path)
            return
        try:
            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(creds_path)
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'zebra_trades.json')
            self._drive_file_id = find_file(self._drive_service, folder_id, file_name)
            self._drive_enabled = True
            logger.info("Drive enabled, file_id=%s", self._drive_file_id)
        except Exception as e:
            logger.warning("Drive init failed: %s. Local-only.", e)

    def _sync_from_drive(self):
        try:
            from bcs.drive_store import download_json
            if self._drive_file_id:
                # Network call OUTSIDE the lock (same rule as _mutate's upload).
                drive_data = download_json(self._drive_service, self._drive_file_id)
                # The merge+save is a read-modify-write on the shared file and
                # MUST hold the store lock: unlocked, a trade the other cron
                # writes between our read and our _save_local is clobbered —
                # the exact silent lost-write this lock exists to prevent.
                # Base is disk-merged-with-memory (disk is truth), never bare
                # self._trades, which goes stale the moment the other process
                # writes.
                with exclusive(cfg.LOCK_FILE):
                    # First merge is disk vs our own cache (same replica, ties
                    # are routine); only the second compares two REPLICAS and
                    # can legitimately report a split brain.
                    base = self._merge_announced(
                        self._read_local(), self._trades, same_replica=True)
                    merged = self._merge_announced(base, drive_data)
                    self._trades = merged
                    self._save_local()
                self._last_sync_time = time.time()
                drive_vers = {t['id']: t.get('version', 0) for t in drive_data}
                merged_vers = {t['id']: t.get('version', 0) for t in merged}
                if drive_vers != merged_vers:
                    logger.info("Merge diverged from Drive, re-uploading")
                    self._upload_to_drive()
            else:
                logger.info("No zebra file on Drive yet, loading local")
                self._load_local()
        except LockTimeout:
            # Lock busy for 30s+ — do NOT fall through to _load_local, which
            # would immediately re-attempt the same busy lock for another 30s.
            # In-memory state is untouched and still sane; next sync retries.
            logger.warning("Drive sync skipped: store lock busy")
        except Exception as e:
            # The `with exclusive` above has already released on unwind, so the
            # _load_local fallback (which takes the lock itself) cannot deadlock.
            logger.warning("Drive sync failed: %s. Using local.", e)
            self._load_local()

    def _upload_to_drive(self):
        if not self._drive_enabled:
            return
        try:
            from bcs.drive_store import upload_json
            drive_cfg = self._config.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('file_name', 'zebra_trades.json')
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                self._trades, self._drive_file_id
            )
            self._last_sync_time = time.time()
        except Exception as e:
            logger.error("Drive upload failed: %s. Local is safe.", e)

    def _read_local(self) -> list:
        if not cfg.LOCAL_FILE.exists():
            return []
        try:
            with open(cfg.LOCAL_FILE) as f:
                data = json.load(f)
            if not isinstance(data, list):
                raise ValueError(f"Expected list, got {type(data).__name__}")
            return self._quarantine_unreadable(data)
        except (json.JSONDecodeError, ValueError) as e:
            backup = cfg.LOCAL_FILE.with_suffix(
                f'.corrupt.{int(time.time())}.json'
            )
            try:
                cfg.LOCAL_FILE.rename(backup)
            except OSError:
                pass
            logger.critical("File CORRUPT (%s). Backed up to %s.", e, backup)
            self._flag_corruption(str(e), backup)
            return []

    def _quarantine_unreadable(self, data: list) -> list:
        """Drop records the merge cannot survive, preserving and alerting.

        The zebra twin of `common.spread_store._quarantine_unreadable`, and it
        matters more here: this is the book holding the BCS cohort, and the
        write path a bad record bricks includes `mark_exited`. See
        `common.store_contract.partition_readable`.
        """
        good, bad = store_contract.partition_readable(
            data, log=lambda f, *a: logger.warning(f, *a))
        if not bad:
            return good
        path = cfg.LOCAL_FILE.with_suffix(
            f'.unreadable.{int(time.time())}.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(bad, f, indent=2, default=str)
        except OSError:
            path = None
        logger.critical(
            "%d unreadable record(s) held out of the book: %s. Preserved at "
            "%s.", len(bad), '; '.join(b['why'] for b in bad), path)
        self._flag_corruption(
            '%d unreadable record(s): %s'
            % (len(bad), '; '.join(b['why'] for b in bad)), path)
        return good

    def _flag_corruption(self, err: str, backup,
                         kind: str = store_contract.MARKER_QUARANTINE) -> None:
        """Leave a marker the MONITOR turns into a Telegram.

        `kind` separates the two conditions that write this marker. A
        QUARANTINE means the file failed to parse, the book went empty and open
        positions stopped being monitored. A MERGE_CONFLICT means two writers
        touched one record and the book is entirely intact. They were sharing
        one alert until 2026-08-31, so a routine conflict shouted the
        quarantine text -- "restarted EMPTY", "exit monitoring is off" -- at a
        book with seven healthy positions. Defaults to QUARANTINE so existing
        callers and previously-written markers keep their original meaning.

        Quarantine is the single highest-consequence event in this system and it
        was log-only. The store goes empty; `check_entered` hits
        `if not entered: return` and exits before `_alert_monitoring_blind` can
        ever be reached — that alert requires a NON-empty book, so the total
        failure is precisely the case it cannot report. Every open position
        stops being monitored and the only witness is one CRITICAL line in a log
        nobody is tailing. "Blind means Telegram" is the fleet rule.

        A marker file rather than a direct send: this module deliberately has no
        Telegram dependency, and the cron process exits between cycles so an
        in-memory flag would not survive to the alerting layer.
        """
        try:
            cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
            (cfg.LOG_DIR / 'zebra_store_corrupt.json').write_text(json.dumps({
                'at': datetime.now().isoformat(timespec='seconds'),
                'kind': kind,
                'error': err,
                'backup': str(backup),
            }))
        except Exception as e:
            logger.error("Could not write the corruption marker: %s", e)

    def _load_local(self):
        # Under the lock: an unlocked startup read can land mid-save from the
        # other cron. On Linux that silently returns a stale snapshot; on
        # Windows the open handle makes the writer's os.replace fail outright.
        # Only safe here because _load_local is never called from inside
        # _mutate — flock on a second fd in the same process would deadlock.
        with exclusive(cfg.LOCK_FILE):
            self._trades = self._read_local()
        if self._trades:
            logger.info("Loaded %d trades from local", len(self._trades))
        else:
            logger.info("No local zebra file, starting empty")

    @staticmethod
    def _merge_with_notes(base: list, incoming: list, same_replica: bool = False):
        """Union by id, returning `(merged, notes)`. Pure -- logs nothing.

        `same_replica=True` marks the refresh shape -- this replica's DISK
        against this process's own CACHE -- where a version tie means a sibling
        process wrote while we held a cache, not that two books have diverged.
        See `store_contract.resolve_merge`. Resolution is identical either way.

        A booked exit is never walked back by a version. Version alone decided
        this until 2026-08-31, and a version is a per-replica counter rather
        than a conflict detector: local `exited` at version 7 against Drive
        `entered` at version 8 erased the exit and REOPENED the trade. See
        `common.store_contract.resolve_merge`.

        Split from `_merge` rather than folded into it because the resolution
        is worth testing without a store, a logger or a marker file -- and
        because `_merge`'s two-argument unbound form is a spec several tests
        already drive directly.
        """
        by_id = {t['id']: t for t in base}
        notes = []
        for t in incoming:
            tid = t['id']
            if tid not in by_id:
                by_id[tid] = t
                continue
            # The unversioned set is `apply_mfe`'s OWN allowlist, not a second
            # copy of it: those are exactly the fields it may write local-only
            # without bumping the counter, so they are exactly the fields that
            # may legitimately differ at an equal version. One source, so a
            # field added to the batched write cannot start a false alarm.
            winner, note = store_contract.resolve_merge(
                by_id[tid], t, store_contract.ZEBRA_STATUSES,
                same_replica=same_replica,
                unversioned_fields=_BATCHED_POLL_FIELDS,
                unversioned_prefixes=('mfe_',))
            by_id[tid] = winner
            if note:
                notes.append(note)
        return sorted(by_id.values(), key=lambda t: t['id']), notes

    @staticmethod
    def _merge(base: list, incoming: list, same_replica: bool = False) -> list:
        """The merged book. Unchanged signature; see `_merge_with_notes`."""
        merged, _notes = ZebraStore._merge_with_notes(
            base, incoming, same_replica=same_replica)
        return merged

    def _merge_announced(self, base: list, incoming: list,
                         same_replica: bool = False) -> list:
        """`_merge`, with the conflicts said out loud.

        The states this reports are ones no operator can infer from the book
        itself -- the replicas simply differ -- so a log line alone is not
        enough and the corruption marker is raised too.

        MERGE_CONFLICT, never QUARANTINE: nothing here failed to parse and
        nothing was held out of the book, so an alert claiming the store went
        empty would be false in every clause. `same_replica=True` callers pass
        the disk-vs-cache refresh, whose ties are routine and say nothing.
        """
        merged, notes = self._merge_with_notes(
            base, incoming, same_replica=same_replica)
        for note in notes:
            logger.critical('MERGE: %s', note)
        if notes:
            self._flag_corruption(
                '%d merge conflict(s): %s' % (len(notes), ' | '.join(notes)),
                None, kind=store_contract.MARKER_MERGE_CONFLICT)
        return merged

    def _save_local(self):
        cfg.LOG_DIR.mkdir(exist_ok=True)
        fd = None
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(cfg.LOG_DIR), suffix='.tmp', prefix='zebra_'
            )
            with os.fdopen(fd, 'w') as f:
                fd = None
                json.dump(self._trades, f, indent=2, default=str)
                f.flush()
                os.fsync(f.fileno())
            # POSIX rename is indifferent to open handles, but on Windows a
            # reader holding the destination open makes this fail outright.
            # Callers are all under the store lock, so a collision here means a
            # stray unlocked reader — retry briefly rather than lose the write.
            for attempt in range(5):
                try:
                    os.replace(tmp_path, str(cfg.LOCAL_FILE))
                    break
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.05)
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


# ── Singleton ────────────────────────────────────────────────────────────
_store: Optional[ZebraStore] = None


def get_store() -> ZebraStore:
    global _store
    if _store is None:
        _store = ZebraStore()
        _store.initialize()
    return _store

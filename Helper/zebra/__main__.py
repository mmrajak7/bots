"""CLI: python -m zebra [command]

Commands:
  scan        One-shot Chartink scan + watchlist add (no analyzer, no alert)
  run         One cycle: scan + check watching + check entered (cron target)
  loop        Long-running market-hours loop
  list        List trades (optionally filter by status)
  analyze     Run strike analyzer on a stock without scanning (for manual use)
  trigger     Manually trigger analyzer + ENTER alert on a watching signal
  enter       Mark a triggered signal as entered (after manual order placement)
  close       Mark an entered trade as exited
  cancel      Cancel a watching/triggered signal
  status      Dashboard summary
"""

import argparse
import logging
import os
import sys


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        # No datefmt: the default includes the DATE. cron appends every run to
        # one file, which already spans ~94 trading days, and with a
        # time-of-day-only stamp locating "what happened on Aug 7" meant
        # counting `Market closed` banners backwards from the end of a
        # 54,000-line file. Paper mode is a forensic exercise; the log is the
        # only evidence, and evidence needs a date.
    )


def cmd_scan(args):
    from .scanner import validate_and_add
    from .monitor import _get_kite
    from .trade_store import get_store
    kite = _get_kite()
    store = get_store()
    added = validate_and_add(store, kite=kite, dry_run=args.dry_run)
    print(f"\nScanner: {len(added)} signal(s) {'previewed' if args.dry_run else 'added'}")


def cmd_run(args):
    from .monitor import run_once
    run_once(dry_run=args.dry_run)


def cmd_loop(args):
    from .monitor import run_loop
    run_loop(dry_run=args.dry_run)


def cmd_list(args):
    from .trade_store import get_store
    store = get_store()
    store.list_trades(status_filter=args.status)


def cmd_vet_show(args):
    """Dump the vetting context as JSON — the Claude CLI's only input.

    Deliberately reads the context CAPTURED AT TRIGGER rather than re-quoting:
    the vetter must judge the same book the bot saw, or its verdict describes a
    trade that no longer exists. Live re-quoting belongs in the checklist the
    agent runs, not in the handoff.
    """
    import json as _json
    from . import config as cfg
    from .trade_store import get_store
    from . import vet as vet_mod
    from . import mfe as _mfe_mod
    t = get_store().find(args.id)
    if not t:
        print(_json.dumps({'error': f'trade #{args.id} not found'}))
        return 1
    if getattr(args, 'exit', None):
        # Exit context: what the trigger saw, plus the entry reference points a
        # verdict needs (intrinsic floor, entry debit, SL level).
        m = ((t.get('exit_vet') or {}).get(args.exit) or {})
        stop, why = _stop_reason(t, m.get('state'),
                                 vet_mod._exit_expired(m) if m else True,
                                 exit_kind=args.exit)
        print(_json.dumps({
            'stop': stop,
            'stop_reason': why,
            'trade_id': t['id'],
            'stock': t.get('stock'),
            'direction': t.get('direction'),
            'exit_kind': args.exit,
            'vet_state': m.get('state'),
            'defers_so_far': m.get('defers', 0),
            'max_defers': cfg.EXIT_MAX_DEFERS,
            'deadline': m.get('deadline'),
            'expired': vet_mod._exit_expired(m) if m else True,
            'entry_debit': t.get('debit'),
            'debit_sl_value': t.get('debit_sl_value'),
            # The peak this position reached and the trail derived from it.
            # A TRAIL trigger is unjudgeable without them — the agent would be
            # asked whether a level is right while being shown neither the
            # level nor the peak it came from, which is the same defect the
            # ENTRY context already had to fix.
            'peak_mid': t.get('mfe_mid'),
            'peak_mid_at': t.get('mfe_mid_at'),
            'peak_spot': t.get('mfe_spot'),
            'trail': _mfe_mod.trail_levels(t),
            'entry_spot': t.get('entry_spot'),
            'tp_spot': t.get('tp_spot'),
            'expiry': t.get('expiry'),
            'long_symbol': t.get('long_symbol'),
            'short_symbol': t.get('short_symbol'),
            'quantity': t.get('quantity'),
            'context': m.get('context', {}),
            'checklist': str(cfg.VETTING_DOC) + ' — EXIT section',
        }, indent=2, default=str))
        return 0
    from . import events as events_mod
    from . import postmortem as pm_mod
    v = t.get('vet') or {}
    stop, why = _stop_reason(t, v.get('state'), vet_mod.is_expired(t))
    print(_json.dumps({
        'stop': stop,
        'stop_reason': why,
        'trade_id': t['id'],
        'stock': t.get('stock'),
        'direction': t.get('direction'),
        'timeframe': t.get('timeframe'),
        'st_value': t.get('st_value'),
        'st_direction': t.get('st_direction'),
        'signal_gap_pct': t.get('signal_gap_pct'),
        'trend_aligned': cfg.is_trend_aligned(t.get('direction'),
                                              t.get('st_direction')),
        'vet_state': v.get('state'),
        'deadline': v.get('deadline'),
        'expired': vet_mod.is_expired(t),
        # The shared calendar the Sonnet agent maintains. The entry checklist
        # calls event risk "the big one", and this path used to omit it
        # entirely — so every entry agent re-researched results and ex-div
        # dates from scratch, which is the exact cost the calendar exists to
        # remove, and could miss an event already known to the fleet.
        'known_events': events_mod.upcoming(t.get('stock')),
        # What happened the last times this pattern appeared. Without it the
        # layer starts every decision from zero and the book's own history is
        # the one input it never gets. `withheld` is included rather than
        # dropped: a precedent suppressed by the realised-support guard is
        # itself information ("we believe this pattern is bad but have only
        # ever vetoed it"), and a guard nobody can see is one nobody trusts.
        'precedents': _precedents_for(t),
        'context': v.get('context', {}),
        'checklist': 'Helper/CLAUDE.md — BCS pre-entry checklist (A-E)',
    }, indent=2, default=str))
    return 0


def _precedents_for(trade: dict) -> dict:
    """Precedent block for the entry context — never load-bearing.

    A broken or empty precedent store must leave vetting exactly as it was
    before this existed, so every failure here degrades to "no precedents"
    rather than to a missing verdict.
    """
    from . import postmortem as pm_mod
    from .trade_store import get_store
    try:
        return pm_mod.for_signal(get_store(), trade.get('stock'))
    except Exception as e:
        logging.getLogger(__name__).warning("precedent lookup failed: %s", e)
        return {'shown': [], 'withheld': [], 'error': str(e)}


def _stop_reason(trade: dict, state, expired: bool, exit_kind=None) -> tuple:
    """Should the agent stop without deciding? (stop, reason).

    VETTING.md used to tell the agent to stop when `expired: true`, describing
    it as "already failed open and entered unvetted". Both halves were wrong:
    `expired` goes true while the signal is still PENDING and nothing has
    entered, and once it really HAS failed open the state is `unavailable` and
    `expired` reads FALSE — so the documented stop condition never fired for
    the case it named. An agent following the doc therefore burned a full
    research run and a `decide` call on every already-settled signal.

    This computes the question the agent actually needs answered, so the doc
    can point at one boolean instead of describing an inference.
    """
    from . import vet as vet_mod
    if state is None:
        return True, 'no vet was requested for this signal — nothing to decide'
    if exit_kind is None and state in vet_mod.TERMINAL:
        return True, ('already settled as %s — a verdict now would be '
                      'discarded' % state)
    if exit_kind is not None and state in (vet_mod.ALLOWED, vet_mod.UNAVAILABLE):
        return True, 'this exit episode already settled as %s' % state
    if exit_kind is not None and trade.get('status') != 'entered':
        return True, ('position is %s, not open — a verdict cannot apply'
                      % trade.get('status'))
    if exit_kind is None and state == vet_mod.QUEUED:
        # A queued signal has NO agent attached — this one was requeued while
        # still running. Its verdict would be discarded (`record_verdict`
        # accepts only PENDING), so telling it to proceed burns a full research
        # run for nothing and holds an agent slot the retry needs.
        return True, ('this signal was requeued for a fresh agent — your '
                      'verdict would be discarded, so stop here')
    if expired:
        return True, ('the deadline passed; this signal is requeued for a '
                      'fresh agent and a verdict now is void')
    return False, ''


def _settle_decision(decision_id: int, outcome: str) -> None:
    """Stamp a journal row with whether its verdict was actually applied.

    `outcome` is whatever the store returned: 'applied', or a
    'discarded (...)' string. Best-effort — the verdict has already landed on
    the trade, and failing to annotate the journal must never make an agent
    believe its decision did not take effect. A miss here costs one unscored
    row, which is the safe direction: unscored beats wrongly scored.
    """
    from .decisions import get_store as get_decisions
    try:
        if outcome == 'applied':
            get_decisions().mark_acted(decision_id)
        else:
            get_decisions().mark_discarded(decision_id, outcome)
    except Exception as e:
        logging.getLogger(__name__).warning(
            "could not settle decision #%s (%s): %s", decision_id, outcome, e)


def _journal(**kw) -> dict:
    """Write the audit row, but never let it decide whether the verdict lands.

    The journal is deliberately written before the verdict (see below), and it
    was the ONE call in this flow allowed to raise. Everything else treats the
    journal as non-load-bearing — `_settle_decision` is best-effort, the Drive
    upload swallows, precedents degrade to empty — but `record()` raises on a
    `confidence` outside 0..1, `_mutate` raises LockTimeout, and `_save`
    re-raises PermissionError. `main()` has no handler, so any of those killed
    the process BEFORE `record_verdict` ran.

    The failure that matters: an agent researches for four minutes, finds an
    earnings print inside the expiry window, and calls `--verdict veto
    --confidence 85` instead of 0.85. Traceback, exit 1, marker still PENDING —
    six minutes later `expire_stale` fails it open and the signal ENTERS, with
    the alert telling the owner Claude did not answer in time. The veto is gone
    and the record says outage. A contended decisions lock does the same thing
    with no operator error at all.

    An audit store must never be able to disarm a veto. On an INFRASTRUCTURE
    failure we return a null id, log CRITICAL, and let the verdict proceed
    unaudited: an unaudited action beats an unexecuted decision that reports
    itself as an outage.

    VALIDATION errors are deliberately NOT swallowed. A `confidence` outside
    0..1 is the agent's own mistake, it is fully retryable, and nothing has
    landed yet — so raising leaves the marker pending and the signal genuinely
    re-vettable, which is the better outcome than recording a malformed verdict
    unaudited. The two failure classes look identical from the call site and are
    opposite in what they demand; only the infra one is a fail-open.
    """
    from .decisions import get_store as get_decisions
    try:
        return get_decisions().record(**kw)
    except ValueError:
        raise                      # the agent's error: let it retry cleanly
    except Exception as e:
        logging.getLogger(__name__).critical(
            "JOURNAL FAILED for a %s verdict (%s) — the verdict will still be "
            "applied, UNAUDITED: %s", kw.get('kind'), kw.get('verdict'), e,
            exc_info=True)
        return {'id': None}


def cmd_vet_decide(args):
    """Record Claude's verdict: journal it, then land it on the signal.

    Order matters. The journal is written FIRST so a crash between the two
    leaves an auditable decision with no action taken — the safe direction.
    The reverse would act on a verdict with no record of why.
    """
    from . import config as cfg
    from .trade_store import get_store
    from .decisions import get_store as get_decisions
    from . import vet as vet_mod

    store = get_store()
    t = store.find(args.id)
    if not t:
        print(f"trade #{args.id} not found")
        return 1

    verdict = vet_mod.ALLOWED if args.verdict == 'allow' else vet_mod.VETOED
    # One decision, both A/B arms — keeps the structure comparison clean.
    # WHOLE BOOK: addressed by explicit id, and by the shadow arms of that
    # id. The scope is the argument, not the era.
    trade_ids = [t['id']] + [s['id'] for s in store.load_trades()
                             if s.get('shadow_of') == t['id']]
    d = _journal(
        kind='entry',
        verdict='allow' if verdict == vet_mod.ALLOWED else 'veto',
        trade_ids=trade_ids,
        stock=t.get('stock'), direction=t.get('direction'),
        reasons=args.reason or [], red_flags=args.red_flag or [],
        confidence=args.confidence, model=cfg.VET_MODEL,
        notes=args.notes or '',
    )
    outcome = vet_mod.record_verdict(store, args.id, verdict,
                                     decision_id=d['id'])
    # Close the loop on the journal row: did this verdict actually govern the
    # signal, or did the store refuse it (late, duplicate, already settled)?
    # Without this the row looks identical to one that WAS applied, and the
    # scoring pipeline credits the layer for a trade it never authorised.
    _settle_decision(d['id'], outcome)
    from .health import record_agent_landed
    record_agent_landed('entry')   # proof of life for THIS channel
    print(f"decision #{d['id']} recorded; verdict {outcome}")

    # A VETO is the end of the story for this signal — nothing else will be
    # sent, so silence would be indistinguishable from "nothing fired". An
    # ALLOW deliberately stays quiet here: it rides on the ENTER alert that is
    # already going out (one signal, one alert).
    if verdict == vet_mod.VETOED and outcome == 'applied':
        from .monitor import _send_telegram, format_vetoed_alert
        try:
            _send_telegram(format_vetoed_alert(t, args.reason or [],
                                               args.red_flag or []))
        except Exception as e:
            # The decision is already recorded and applied; a Telegram failure
            # must not make the agent think its verdict did not land.
            logging.getLogger(__name__).warning(
                "veto alert failed for #%d: %s", args.id, e)
    # A discarded verdict is NOT an error the agent should retry — the signal
    # already settled. Exit 0 so a retry loop does not hammer a closed case.
    return 0


def cmd_vet_exit_decide(args):
    """Record an exit verdict: allow the exit, or defer for a re-check.

    No hard veto exists here by design — see vet.record_exit_verdict. Deferring
    is the same protective power in a shape that cannot silently disarm a stop.
    """
    from . import config as cfg
    from .trade_store import get_store
    from .decisions import get_store as get_decisions
    from . import vet as vet_mod

    store = get_store()
    t = store.find(args.id)
    if not t:
        print(f"trade #{args.id} not found")
        return 1

    d = _journal(
        kind='exit',
        verdict='allow' if args.verdict == 'allow' else 'defer',
        trade_ids=[t['id']],
        stock=t.get('stock'), direction=t.get('direction'),
        reasons=args.reason or [], red_flags=args.red_flag or [],
        confidence=args.confidence, model=cfg.VET_MODEL,
        notes=args.notes or '', evidence={'exit_kind': args.kind},
    )
    outcome = vet_mod.record_exit_verdict(store, args.id, args.kind,
                                          args.verdict, decision_id=d['id'])
    # The entry channel has settled its row since the refusal-journalling fix;
    # this one computed `outcome` and threw it away, so a REFUSED verdict —
    # trade no longer entered, CAS lost, marker not pending — left a row
    # byte-identical to an applied one. That is not hypothetical: all 36 rows in
    # zebra_decisions.stray-backup.json are refusals and none is marked as one,
    # so the file reads as "the exit vetter deferred 36 exits" when it deferred
    # nothing. An unmarked refusal silently becomes evidence.
    _settle_decision(d['id'], outcome)
    if outcome != 'applied':
        logging.getLogger(__name__).warning(
            "exit verdict for #%s %s was NOT applied: %s",
            args.id, args.kind, outcome)
    from .health import record_agent_landed
    # Stays unconditional: this measures "did the agent come back", and an
    # agent that reached this verb came back whether or not the store took its
    # verdict. The refusal is now visible in the journal instead.
    record_agent_landed('exit')   # proof of life for THIS channel
    print(f"decision #{d['id']} recorded; exit verdict {outcome}")
    return 0


def cmd_vet_score(args):
    """Is the vetting layer earning its keep?

    The report that decides whether this layer ever gets live authority. Two
    scores, never blended: realised P&L (which can only judge ALLOWS, because a
    vetoed structure was never priced) and the signal-quality label (which
    judges both arms on the same hit/miss basis). Blending a spot proxy into a
    rupee figure would be the easiest possible way to lie to ourselves.
    """
    from .decisions import get_store as get_decisions
    from .trade_store import get_store
    from . import outcomes as outcomes_mod

    store = get_store()
    decisions = get_decisions()
    if not getattr(args, 'no_join', False):
        joined = outcomes_mod.join(store, decisions)
        if joined:
            print(f"joined {joined} new outcome(s)\n")
    decisions.refresh()

    s = decisions.score('entry')
    q = s.get('signal_quality') or {}
    print("ENTRY VETTING")
    print("-" * 62)
    print(f"decisions scored on realised P&L : {s['scored']}")
    for verdict in ('veto', 'allow'):
        b = s.get(verdict) or {}
        if not b.get('n'):
            print(f"  {verdict:<6} n=0")
            continue
        money = b.get('pnl_avoided', b.get('pnl_captured'))
        print(f"  {verdict:<6} n={b['n']:<3} correct={b['correct']:<3} "
              f"precision={b['precision']:<6} Rs{money:,.0f}")
    print(f"\nsignal quality (hit/miss, comparable across both) : "
          f"{q.get('labelled', 0)} labelled")
    for verdict in ('veto', 'allow'):
        b = q.get(verdict) or {}
        prec = b.get('precision')
        print(f"  {verdict:<6} n={b.get('n', 0):<3} decisive={b.get('decisive', 0):<3} "
              f"flat={b.get('flat', 0):<3} correct={b.get('correct', 0):<3} "
              f"precision={prec if prec is not None else 'n/a'}")
    pending = [d for d in decisions.pending_outcome() if d.get('kind') == 'entry']
    if pending:
        print(f"\n{len(pending)} decision(s) still awaiting an outcome "
              f"(open positions / unresolved veto shadows)")

    if getattr(args, 'list', False):
        print("\nRECENT DECISIONS")
        print("-" * 62)
        for d in decisions.all()[-args.list:]:
            o = d.get('outcome') or {}
            print(f"#{d['id']:<4} {d['created_at'][:16]} {d['kind']:<6} "
                  f"{d['verdict']:<12} {(d.get('signal_ref') or {}).get('stock', ''):<12} "
                  f"{o.get('label') or ('pending' if not o else ''):<8} "
                  f"{('Rs%.0f' % o['pnl']) if o.get('pnl') is not None else ''}")
    return 0


def cmd_events_show(args):
    """Print the event calendar (optionally filtered to one symbol)."""
    import json as _json
    from . import events as events_mod
    data = events_mod.load()
    if args.symbol:
        rows = events_mod.upcoming(args.symbol, within_days=args.days, data=data)
    else:
        rows = events_mod.upcoming(None, within_days=args.days, data=data)
    print(_json.dumps({'refreshed_at': data.get('refreshed_at'),
                       'stale': events_mod.is_stale(data),
                       'events': rows}, indent=2, default=str))
    return 0


def cmd_events_replace(args):
    """Install a validated event calendar from a JSON file.

    The agent never writes the calendar directly: it hands a candidate here and
    this verb owns the schema, so a malformed refresh is rejected as a batch
    rather than silently emptying the file.
    """
    import json as _json
    from . import events as events_mod
    try:
        with open(args.file) as f:
            payload = _json.load(f)
    except Exception as e:
        print(f"cannot read {args.file}: {e}")
        return 1
    rows = payload.get('events') if isinstance(payload, dict) else payload
    # AN AGENT MAY REFRESH THE CALENDAR; IT MAY NOT EMPTY IT.
    #
    # `--allow-empty` refreshes `refreshed_at` while installing zero events,
    # so the calendar reads HEALTHY to `is_stale()` while `adjustment_today`
    # -- the corporate-action interlock that suspends automated exits on a
    # bonus/split day -- sees nothing. That is the "a default that looks like
    # a value" shape aimed at a safety input, and it is the whole reason this
    # verb was globally denied. Denying it also made the calendar unwritable
    # by its own channel, so the interlock read a file nobody maintained.
    #
    # The verb is now granted to the events channel and the DANGEROUS HALF is
    # refused here instead: a genuinely empty window is a human's call.
    if getattr(args, 'allow_empty', False) and os.environ.get(
            'ZEBRA_AGENT_CHANNEL'):
        print('refused: --allow-empty is not available to a spawned agent. '
              'An empty calendar silences the corporate-action interlock '
              'while reading as freshly refreshed. If the window genuinely '
              'has no events, install it by hand.')
        return 1
    try:
        # `replace()` refuses an empty install by default and tells the caller
        # to pass allow_empty — which the CLI did not expose, so the escape
        # hatch the error message advertised was unreachable by the only caller
        # that ever sees it.
        doc = events_mod.replace(rows, allow_empty=getattr(args, 'allow_empty',
                                                           False))
    except ValueError as e:
        print(f"rejected: {e}")
        return 1
    from .health import record_agent_landed
    record_agent_landed('events')   # proof of life for THIS channel
    print(f"event calendar replaced: {len(doc['events'])} event(s)")
    return 0


def cmd_review_show(args):
    """Dump a position-review context as JSON — the review agent's input."""
    import json as _json
    from . import config as cfg
    from . import events as events_mod
    from .trade_store import get_store
    t = get_store().find(args.id)
    if not t:
        print(_json.dumps({'error': f'trade #{args.id} not found'}))
        return 1
    m = t.get('review') if isinstance(t.get('review'), dict) else {}
    print(_json.dumps({
        'trade_id': t['id'],
        'stock': t.get('stock'),
        'direction': t.get('direction'),
        'status': t.get('status'),
        'entry_spot': t.get('entry_spot'),
        'entry_date': t.get('entry_date'),
        'debit': t.get('debit'),
        'max_loss': t.get('capital'),
        'tp_spot': t.get('tp_spot'),
        'expiry': t.get('expiry'),
        'long_symbol': t.get('long_symbol'),
        'short_symbol': t.get('short_symbol'),
        'quantity': t.get('quantity'),
        'flagged_because': m.get('why'),
        'context': m.get('context', {}),
        'events': events_mod.upcoming(t.get('stock')),
        'checklist': str(cfg.VETTING_DOC) + ' — POSITION REVIEW section',
    }, indent=2, default=str))
    return 0


def cmd_review_record(args):
    """Record a review recommendation. Cannot close anything — by design."""
    from . import config as cfg
    from .trade_store import get_store
    from .decisions import get_store as get_decisions
    from . import review as review_mod

    store = get_store()
    t = store.find(args.id)
    if not t:
        print(f"trade #{args.id} not found")
        return 1
    # WHICH question was asked, and therefore which model answered. Both were
    # hardcoded, so the journal recorded every routine Sonnet news sweep as an
    # Opus price review — and the owner's plan is to judge the sweep's output
    # after a week, which is impossible from a journal that cannot tell the two
    # apart. The marker is the source of truth: `review.request` stamps `kind`
    # when it spawns.
    _m = t.get('review') if isinstance(t.get('review'), dict) else {}
    _scan = _m.get('kind') == 'scan'
    d = _journal(
        kind='scan' if _scan else 'review', verdict=args.action,
        trade_ids=[t['id']],
        stock=t.get('stock'), direction=t.get('direction'),
        reasons=args.reason or [], red_flags=args.red_flag or [],
        confidence=args.confidence,
        model=cfg.EOD_REVIEW_MODEL if _scan else cfg.VET_MODEL,
        notes=args.notes or '',
    )
    outcome = review_mod.record(store, args.id, args.action,
                               reasons=args.reason or [], decision_id=d['id'])
    # Third channel, same contract as entry and exit: an unmarked refusal is
    # indistinguishable from an applied verdict and corrupts anything scored
    # off it.
    _settle_decision(d['id'], outcome)
    if outcome != 'applied':
        logging.getLogger(__name__).warning(
            "review verdict for #%s was NOT applied: %s", args.id, outcome)
    from .health import record_agent_landed
    # Proof of life for THIS channel — and the sweep has its own, so a Sonnet
    # scan landing can never clear the alarm for the Opus price reviews.
    record_agent_landed(review_mod.CHANNELS['scan' if _scan else 'review'])
    print(f"decision #{d['id']} recorded; review {outcome}")
    return 0


def cmd_quote(args):
    """Re-quote a signal or an open position LIVE. Read-only, JSON to stdout.

    Why this exists. `vet show` deliberately serves the book CAPTURED AT
    TRIGGER, so a verdict judges what the bot acted on. The checklist then
    tells the agent to "re-quote live at decision time (never trust alert
    pricing)" — and until now nothing let it. Measured on 2026-08-14: eleven
    separate agents reported they could not obtain a live book (Kite MCP and
    ad-hoc Python are both unpermitted, correctly), and every one of them
    docked its own confidence for it. The layer was being asked to run a step
    its permission set forbade.

    It matters most on the EXIT side. Both incidents that cost real money
    (ICICI Feb, NHPC Jul) were one garbage top-of-book acted on at the open,
    and the fix was debounce plus re-verify — a second observation, separated
    in time. That is exactly what an agent re-quoting here performs, from a
    different process, before it endorses a close.

    Two shapes, because the two states hold different evidence:

    - ENTERED — re-run the monitor's own `_structure_quote`, so the agent sees
      the same arithmetic the engine uses: the trade's stamped `pricing_basis`,
      the mathematical bounds, the intrinsic-floor REJECTION, and per-leg
      reliability. A second implementation here would eventually disagree with
      the one that trades, and the disagreement would surface as a verdict.
    - TRIGGERED/WATCHING — rebuild the live BCS pair through the same builder
      the cycle uses. The entry context carries only the ATM leg (the short is
      picked inside `analyze_bcs`, after the snapshot is taken), so this is the
      only way an entry agent can see the short leg's book at all.

    Read-only by construction: it opens no lock, writes no store, and places
    nothing. Reachable under the existing coarse `-m zebra` grant; the deny
    list still blocks every position verb.
    """
    import json as _json
    from datetime import datetime as _dt
    from .trade_store import get_store
    from . import config as cfg

    t = get_store().find(args.id)
    if not t:
        print(_json.dumps({'error': f'trade #{args.id} not found'}))
        return 1

    out = {'trade_id': t['id'], 'stock': t.get('stock'),
           'status': t.get('status'), 'direction': t.get('direction'),
           'quoted_at': _dt.now().isoformat(timespec='seconds')}
    try:
        from .scanner import _get_kite, get_ltp
        kite = _get_kite()
        spot = (get_ltp(kite, [t['stock']]) or {}).get(t['stock']) or None
    except Exception as e:
        # A dead token is the likeliest cause and it must read as a refusal to
        # answer, not as a quote of zero. An agent that cannot tell those apart
        # will endorse an exit on a book it never saw.
        print(_json.dumps({**out, 'error': f'no live quote: {e}',
                           'advice': 'treat as NO independent confirmation — '
                                     'do not upgrade confidence on this'}))
        return 1
    out['spot'] = spot
    if not spot:
        # "We could not ASK" and "we asked and the book was empty" need
        # OPPOSITE responses, and they arrive here looking identical: `get_ltp`
        # swallows an auth failure and returns {}, so a dead token reaches this
        # point as a missing spot rather than as an exception. Caught by running
        # the verb for real against an expired token, where it cheerfully
        # reported "no transactable price" — a statement about the market, made
        # on evidence that was really a statement about our login.
        #
        # The underlying is the most liquid instrument in the chain and is
        # quoted continuously, so no spot means the pipe is broken, full stop.
        # Refuse loudly instead of describing a book nobody managed to read.
        print(_json.dumps({**out,
                           'error': 'no spot for %s — the underlying is quoted '
                                    'continuously, so this is an API or token '
                                    'failure, NOT a thin book' % t.get('stock'),
                           'advice': 'treat as NO independent confirmation — '
                                     'do not upgrade confidence on this, and '
                                     'do not read it as a low valuation'}))
        return 1

    if t.get('status') == 'entered':
        from .monitor import _structure_quote
        q = _structure_quote(kite, t, spot)
        entry_debit = t.get('debit')
        out.update({
            'kind': 'position',
            'pricing_basis': t.get('pricing_basis') or 'mid',
            'value': q.get('mid'),
            'reliable': q.get('reliable'),
            'unreliable_reason': q.get('reason'),
            'rejected': q.get('rejected'),
            'legs': q.get('legs'),
            'entry_debit': entry_debit,
            'entry_spot': t.get('entry_spot'),
            'debit_sl_value': t.get('debit_sl_value'),
            'tp_spot': t.get('tp_spot'),
            'sl_spot': t.get('sl_spot'),
            'expiry': t.get('expiry'),
        })
        if q.get('mid') is not None and entry_debit:
            out['pnl_pct'] = round((q['mid'] - entry_debit) / entry_debit * 100, 1)
        # State the meaning of a refusal rather than leaving a null to be read
        # as "fine". `mid: None` is the honest answer to an unusable book, and
        # an agent skimming JSON will otherwise treat the absence as absence of
        # a problem.
        if q.get('mid') is None:
            out['advice'] = ('no transactable price right now (%s) — this is a '
                             'REFUSAL, not a low value; the engine defers to '
                             'the next poll on exactly this read'
                             % (q.get('reason') or 'unusable book'))
        print(_json.dumps(out, default=str))
        return 0

    if t.get('status') not in ('triggered', 'watching'):
        print(_json.dumps({**out,
                           'error': 'nothing to quote: only triggered, '
                                    'watching or entered signals have legs'}))
        return 1

    # Pre-entry: rebuild what would actually be opened right now.
    from . import strikes as strikes_mod
    try:
        analysis = strikes_mod.analyze(kite, t['stock'], t['direction'], spot)
        if analysis.get('error'):
            print(_json.dumps({**out, 'error': analysis['error']}))
            return 1
        bcs = strikes_mod.analyze_bcs(
            kite, t['stock'], t['direction'], spot,
            target_spot=t.get('st_value'), expiry=analysis['expiry'],
            atm_strike=analysis['atm_strike'],
            atm_quote=analysis['atm_quote'],
            lot_size=analysis['lot_size'])
    except Exception as e:
        print(_json.dumps({**out, 'error': f'rebuild failed: {e}'}))
        return 1
    out.update({'kind': 'signal', 'expiry': analysis.get('expiry'),
                'dte': analysis.get('dte'), 'st_value': t.get('st_value')})
    if bcs.get('error'):
        # A gate failing NOW is a real answer, not an error to route around:
        # the structure the agent is vetting would be suppressed at this book.
        out['buildable'] = False
        out['reason'] = bcs['error']
    else:
        out['buildable'] = True
        out['bcs'] = bcs
    print(_json.dumps(out, default=str))
    return 0


def cmd_analyze(args):
    """Run the strike analyzer on a stock outside the scanner. Manual review."""
    from .scanner import _get_kite, get_ltp, compute_st_for_stock
    from . import strikes as strikes_mod
    from . import config as cfg

    kite = _get_kite()
    stock = args.symbol.upper()
    ltps = get_ltp(kite, [stock])
    spot = ltps.get(stock, 0)
    if spot <= 0:
        print(f"No LTP for {stock}")
        sys.exit(1)

    timeframe = args.timeframe
    direction = args.direction.upper() if args.direction else None
    st = None       # set only when we route the direction ourselves
    if direction is None:
        st = compute_st_for_stock(kite, stock, timeframe)
        if not st:
            print(f"ST compute failed for {stock} {timeframe}")
            sys.exit(1)
        # Magnet routing — same as the scanner's _direction_for: direction is
        # set by which side of the ST line price sits on, and spot exactly on
        # the line is SKIP (no edge). Trend alignment is reported as conviction
        # (not a hard skip), matching how the bot actually trades.
        if spot < st['st']:
            direction = 'CE'
        elif spot > st['st']:
            direction = 'PE'
        else:
            print(f"  spot {spot:.2f} is exactly on ST {st['st']:.2f} — no "
                  f"direction. Specify --direction to override.")
            sys.exit(1)
        aligned = cfg.is_trend_aligned(direction, st['direction'])
        tag = 'ALIGNED (with-trend, premium)' if aligned \
            else 'COUNTER-TREND (lower conviction)'
        print(f"  spot {spot:.2f} vs ST {st['st']:.2f} ({st['direction']}) "
              f"-> {direction}  [{tag}]")

    print(f"\nAnalyzing {stock} ({direction}) at spot {spot:.2f}...")
    result = strikes_mod.analyze(kite, stock, direction, spot,
                                 max_candidates=args.max_candidates)
    if result.get('error'):
        print(f"  ERROR: {result['error']}")
        sys.exit(1)

    print(f"  expiry {result['expiry']} ({result['dte']} DTE), lot={result['lot_size']}")
    atm = result.get('atm_quote') or {}
    print(f"  ATM strike {result['atm_strike']}  "
          f"bid={atm.get('bid')} ask={atm.get('ask')} mid={atm.get('mid')} "
          f"OI={atm.get('oi')}")
    if result.get('back_ratio'):
        print("  back ratio: RETIRED 2026-08-27 — not priced, not tradeable")
    print("")

    # The BCS is the structure that trades, so it is the structure this verb
    # reports. It used to print a ranked list of back-ratio (K_L, K_S) pairs,
    # which is empty by design now — a verb whose entire output describes a
    # retired structure is a verb that lies.
    #
    # The short leg is picked nearest the ST target, exactly as the live path
    # does; with no ST value to hand (`--direction` given explicitly) the ATM
    # strike is the honest fallback and `analyze_bcs` forces at least one
    # strike beyond it, so the spread always has width.
    target = st['st'] if st else result['atm_strike']
    bcs = strikes_mod.analyze_bcs(
        kite, stock, direction, spot, target_spot=target,
        expiry=result['expiry'], atm_strike=result['atm_strike'],
        atm_quote=atm, lot_size=result['lot_size'])
    if bcs.get('error'):
        print(f"  BCS: SUPPRESSED — {bcs['error']}")
        return
    print("  >>> BCS (the structure that trades) <<<")
    print(f"    long  {bcs['long_symbol']}  "
          f"b={bcs['long_bid']:g}/a={bcs['long_ask']:g} OI={bcs['long_oi']:,}")
    print(f"    short {bcs['short_symbol']}  "
          f"b={bcs['short_bid']:g}/a={bcs['short_ask']:g} OI={bcs['short_oi']:,}")
    print(f"    width={bcs['width']:g}  debit={bcs['debit']:g} (fill) / "
          f"{bcs['debit_mid']:g} (mid)  d/w={bcs['debit_to_width_pct_mid']:.1f}%")
    print(f"    max profit/share={bcs['max_profit_per_share']:g}  "
          f"entry cost={bcs['entry_cost']:g} ({bcs['entry_cost_pct']:.1f}% of max gain)")


def cmd_trigger(args):
    """Force-run analyzer + alert on a specific watching signal."""
    from .monitor import (_send_telegram, _format_bcs_enter_alert,
                          _build_bcs)
    from .scanner import _get_kite, get_ltp
    from . import strikes as strikes_mod
    from .trade_store import get_store

    store = get_store()
    trade = store.find(args.id)
    if not trade:
        print(f"Trade #{args.id} not found")
        sys.exit(1)
    if trade['status'] not in ('watching', 'triggered'):
        print(f"#{args.id} status={trade['status']}, can't re-trigger")
        sys.exit(1)

    kite = _get_kite()
    spot = get_ltp(kite, [trade['stock']]).get(trade['stock'], 0)
    if spot <= 0:
        print(f"No LTP for {trade['stock']}")
        sys.exit(1)

    analysis = strikes_mod.analyze(kite, trade['stock'], trade['direction'],
                                   spot, max_candidates=args.max_candidates)
    if analysis.get('error'):
        print(f"Analyzer error: {analysis['error']}")
        sys.exit(1)

    gap = abs(spot - trade['st_value']) / trade['st_value'] * 100
    analysis['current_gap_pct'] = gap

    if trade['status'] == 'watching':
        store.mark_triggered(trade['id'], spot, gap, analysis['candidates'])

    # The BCS, not the retired back ratio. `_format_enter_alert` rendered
    # the latter and now raises; this verb is a manual re-run of the live
    # alert, so it must build the same ticket the live path builds.
    bcs = _build_bcs(kite, trade, analysis, spot)
    if bcs is None:
        print('BCS suppressed by a gate (see the log) — no ticket')
        sys.exit(1)
    msg = _format_bcs_enter_alert(trade, analysis, bcs)
    if args.dry_run:
        print(msg)
    else:
        ok = _send_telegram(msg, dry_run=False)
        print(f"Alert {'sent' if ok else 'FAILED'}")


def cmd_enter(args):
    from .trade_store import get_store
    store = get_store()
    try:
        k_l_str, k_s_str = args.pair.split('/')
        long_strike = float(k_l_str)
        short_strike = float(k_s_str)
    except Exception:
        print(f"--pair must be K_L/K_S e.g. 700/750, got {args.pair}")
        sys.exit(1)

    # Look up the actual symbols + lot_size from the triggered alert if present,
    # else from the options CSV directly.
    trade = store.find(args.id)
    if not trade:
        print(f"#{args.id} not found")
        sys.exit(1)

    long_symbol = args.long_symbol
    short_symbol = args.short_symbol
    lot_size = args.lot_size

    if (not long_symbol or not short_symbol or not lot_size) and \
       trade.get('alert_strikes'):
        for c in trade['alert_strikes']:
            if abs(c['k_l'] - long_strike) < 1e-6 and abs(c['k_s'] - short_strike) < 1e-6:
                long_symbol = long_symbol or c['long_symbol']
                short_symbol = short_symbol or c['short_symbol']
                lot_size = lot_size or c['lot_size']
                break

    if not (long_symbol and short_symbol and lot_size):
        print("Could not resolve symbols/lot_size. Pass --long-symbol, --short-symbol, --lot-size.")
        sys.exit(1)

    from . import config as cfg
    # Which structure was actually placed decides how the position is VALUED
    # for the rest of its life (`_long_multiplier`: a zebra is 2*long - short,
    # a BCS is long - short). Left unset, every hand-entered BCS was valued as
    # a zebra — debit SL effectively disarmed, trail dead for want of a width,
    # P&L doubled. The automated path has stamped this since the BCS entry
    # fields landed; this CLI never did.
    structure = args.structure or cfg.ENTRY_STRUCTURE

    entry_data = {
        'long_strike': long_strike,
        'short_strike': short_strike,
        'long_symbol': long_symbol,
        'short_symbol': short_symbol,
        'debit': args.debit,
        'lot_size': int(lot_size),
        'lots': args.lots,
        'expiry': args.expiry,
        'structure': structure,
    }
    if args.entry_spot is not None:
        entry_data['entry_spot'] = args.entry_spot
    if args.spot_sl_pct is not None:
        entry_data['spot_sl_pct'] = args.spot_sl_pct
    # A hand-entered trade is a REAL fill: --debit is what was actually paid,
    # i.e. ASK(long) - BID(short). Default the basis to 'fill' so the monitor
    # values it the way it will actually be closed. Left unset it fell through
    # to mid-mid, which reads optimistic at both ends and fires the debit SL
    # late. Overridable for the rare case of recording a mid-basis figure.
    entry_data['pricing_basis'] = args.pricing_basis
    if args.short_extrinsic is not None:
        entry_data['short_extrinsic_entry'] = args.short_extrinsic
    else:
        # Recover it from the alert the ticket came from, so the intrinsic-
        # floor guard starts armed with a real number rather than the
        # 30%-of-debit fallback.
        for c in trade.get('alert_strikes') or []:
            if abs(c.get('k_l', -1) - long_strike) < 1e-6 \
                    and abs(c.get('k_s', -1) - short_strike) < 1e-6 \
                    and c.get('short_extrinsic') is not None:
                entry_data['short_extrinsic_entry'] = c['short_extrinsic']
                print(f"  short extrinsic {c['short_extrinsic']} recovered "
                      f"from the alert (intrinsic-floor guard armed)")
                break
        else:
            print("  WARNING: no short-leg extrinsic available — the "
                  "intrinsic-floor quote guard will fall back to 30% of the "
                  "debit. Pass --short-extrinsic to arm it properly.")
    if args.tp is not None:
        entry_data['tp_spot'] = args.tp

    # ── WHO OWNS THIS POSITION? Ask the broker, before writing the record ──
    #
    # This one bit decides which engine manages the trade for the rest of its
    # life, and it used to be left at the scanner's `paper: True` by default.
    # A hand-entered LIVE trade then belonged to the paper engine: it books
    # exits at mid, the armed monitor declines it, and the startup broker
    # check skips it — so the real legs strand on the first trigger with every
    # safety line reading green.
    #
    # The broker's own view is the evidence, and this command was ALREADY
    # fetching it a few lines below to print "VERIFIED". Reading it before the
    # write instead of after turns an observation into a decision.
    positions, pos_err = _broker_positions()
    # `entry_data`, not `args`: the symbols may have been RESOLVED from the
    # alert a few lines up, and `args.long_symbol` is None in that ordinary
    # case. Asking the broker about None would refuse every entry that did
    # not spell its symbols out by hand.
    paper, why = _entry_ownership(args, entry_data, positions, pos_err)
    if paper is None:
        # UNKNOWN OWNERSHIP IS NOT A DEFAULT. Guessing 'real' would hand the
        # live order path a record with no legs; guessing 'paper' is the bug
        # above. Refuse, and say which flag settles it.
        print(f"REFUSING to record #{args.id}: {why}")
        print("  The `paper` flag decides which engine manages this trade for "
              "the rest of its life. Pass --live (you placed it at the "
              "broker) or --paper (you did not) to settle it by hand.")
        sys.exit(1)
    entry_data['paper'] = paper
    print(f"  Recorded as {'PAPER' if paper else 'LIVE'} — {why}")

    try:
        t = store.mark_entered(args.id, entry_data)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)
    print(f"Entered #{t['id']} {t['stock']} [{t.get('structure', 'zebra').upper()}] "
          f"{int(t['long_strike'])}/{int(t['short_strike'])} "
          f"debit={t['debit']:.2f} qty={t['quantity']} cap=Rs{t['capital']:,.0f}")
    sl_txt = f"SPOT SL at {t['sl_spot']:.2f}, " if cfg.SPOT_SL_ENABLED else "SPOT SL off, "
    print(f"  TP at {t['tp_spot']:.2f}, {sl_txt}"
          f"DEBIT SL at {t['debit_sl_value']:.2f}")

    # ── Does the broker agree? (owner: "ensure entries are correct after
    # entry"). Every stop level printed above is computed from the RECORD, so
    # a record that does not match the position is a set of stops pointing at
    # something that is not there. Read-only, never places an order, and never
    # fatal to the entry -- the trade IS recorded either way; what this decides
    # is whether the owner is told to go and look.
    from . import capital
    # The SAME read that decided ownership above — not a second call. Two
    # reads could disagree, and then the record's owner and the line printed
    # under it would be answering different questions.
    if pos_err:
        print(f"  (could not read broker positions: {pos_err})")
    if paper:
        # A paper record has no broker legs BY DEFINITION, so `verify_entry`
        # would print "*** ENTRY NOT VERIFIED *** broker shows NO position"
        # directly under "Recorded as PAPER". A guaranteed contradiction on
        # every paper entry is #449 in miniature, and it trains the reader to
        # scroll past NOT VERIFIED — which is the line that matters on the
        # entries that ARE real.
        print("  (paper — no broker legs expected, nothing to verify)")
        return
    v = capital.verify_entry(positions, t)
    if v['ok']:
        print("  VERIFIED: the broker shows both legs at the recorded size")
    else:
        print("  *** ENTRY NOT VERIFIED ***")
        for prob in v['problems']:
            print(f"    - {prob}")
        print("    The stops above are computed from the RECORD. Fix the "
              "record or the position before relying on them.")


def _broker_positions():
    """`(net_positions, error)`. Never raises — a broker outage must not stop
    a trade being RECORDED, only stop it being recorded as the wrong kind."""
    try:
        from .scanner import _get_kite
        return (_get_kite().positions() or {}).get('net'), None
    except Exception as e:
        return None, str(e)


def _entry_ownership(args, entry_data, positions, pos_err):
    """`(paper, why)` for a hand-entered trade. `paper is None` means REFUSE.

    Three sources, in this order:

    1. an explicit `--live` / `--paper` — the operator is the authority on
       what they did, and there are real cases the broker cannot settle (a
       trade placed in another account, a paper entry recorded while real
       positions in the same symbol are open);
    2. the BROKER, when it can be read — this is the ordinary path and needs
       no flag;
    3. nothing. Then REFUSE, because unknown is not a default here: guessing
       'real' hands the live order path a record with no legs at the broker,
       and guessing 'paper' is the defect this function exists to close.

    The broker is consulted for LEG PRESENCE only, not size — `verify_entry`
    reports the size mismatch separately, and a half-filled real position is
    still real. Answering "paper" because the quantity was short would be the
    original bug wearing a stricter face.
    """
    if getattr(args, 'live', False) and getattr(args, 'paper', False):
        return None, '--live and --paper are contradictory'
    if getattr(args, 'live', False):
        return False, 'you said --live'
    if getattr(args, 'paper', False):
        return True, 'you said --paper'
    if pos_err or positions is None:
        return None, 'the broker could not be read (%s)' % (pos_err or 'no data')

    want = {entry_data.get('long_symbol'),
            entry_data.get('short_symbol')} - {None}
    if not want:
        return None, 'the leg symbols are not known, so the broker cannot be asked'
    live_syms = {p.get('tradingsymbol') for p in positions
                 if p.get('quantity')}
    hit = want & live_syms
    if len(hit) == len(want):
        return False, 'the broker shows both legs live'
    if not hit and not positions:
        # AN EMPTY BOOK IS NOT EVIDENCE OF ANYTHING.
        #
        # `positions()` returning no rows at all is what the early-session
        # sync window looks like, and what positions-lag looks like in the
        # minutes after a fill — which is exactly when `zebra enter` is run.
        # Reading it as "neither leg is live" classifies a real, just-placed
        # trade as PAPER: the stranding bug, reached through its own fix.
        #
        # A book with OTHER rows in it and not ours IS evidence: the broker
        # answered about a populated account and our legs were not in it.
        # That distinction is the whole difference between "no data" and
        # "no position", and this one bit decides which engine owns a live
        # position for the rest of its life.
        return None, ('the broker returned an EMPTY position book — that is '
                      'what a sync lag looks like, not proof of no position')
    if not hit:
        return True, 'the broker shows other positions but neither of these legs'
    return None, ('the broker shows only %s of the two legs — half a spread '
                  'is not an ownership answer' % ', '.join(sorted(hit)))


def cmd_close(args):
    from .trade_store import get_store
    store = get_store()
    trade = store.find(args.id)
    if not trade:
        print(f"#{args.id} not found")
        sys.exit(1)
    if trade['status'] == 'entered':
        exit_debit = args.exit_debit
        exit_spot = args.exit_spot

        # If user didn't pass --exit-debit, try to fetch live structure mid
        # from Kite. Falls back to None (max loss) if Kite is unreachable.
        if exit_debit is None:
            try:
                from .scanner import _get_kite, get_ltp
                from .monitor import _quote_zebra_value
                kite = _get_kite()
                exit_debit = _quote_zebra_value(kite, trade)
                if exit_debit is not None:
                    print(f"  Fetched live structure mid: {exit_debit:.2f}")
                if exit_spot is None:
                    exit_spot = get_ltp(kite, [trade['stock']]).get(trade['stock'])
            except Exception as e:
                print(f"  (could not fetch live quote: {e})")

        if exit_spot is None:
            exit_spot = trade.get('entry_spot', 0)
        if exit_debit is None:
            print("  WARNING: no --exit-debit and Kite quote failed; "
                  "P&L will be booked as MAX LOSS. Re-run with --exit-debit "
                  "to record the actual fill.")

        try:
            t = store.mark_exited(args.id, exit_spot, exit_debit,
                                  args.reason or 'manual')
        except ValueError as e:
            print(f"Error: {e}")
            sys.exit(1)
        print(f"Closed #{t['id']} P&L=Rs{t['pnl']:,.0f} ({t['pnl_pct']:.1f}%)")
    elif trade['status'] in ('watching', 'triggered'):
        store.cancel(args.id, args.reason or 'manual cancel')
        print(f"Cancelled #{args.id}")
    else:
        print(f"#{args.id} status={trade['status']}, nothing to do")


def cmd_cancel(args):
    from .trade_store import get_store
    store = get_store()
    try:
        store.cancel(args.id, args.reason or 'manual cancel')
        print(f"Cancelled #{args.id}")
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)


def _print_cohort_scoreboard(exited, entered):
    """The only scoreboard that answers the question the paper run is asking.

    Kept ABOVE the whole-book performance block on purpose. The legacy numbers
    are larger, older and more flattering, and reading them as "how the strategy
    is doing" is exactly the mistake this split exists to prevent: those trades
    were priced mid-mid (so they model zero round-trip cost), ran unvetted, and
    came out of an engine that no longer exists.

    Gross is shown next to an estimated NET, because the measured baseline is
    that the median trade is +0.90% gross and -0.79% after Zerodha's fees on
    four option legs. A scoreboard that only ever prints gross would show this
    strategy as profitable in exactly the regime where it is not.
    """
    from . import config as cfg
    from .trade_store import cohort_split

    done, _ = cohort_split(exited)
    live, _ = cohort_split(entered)
    print(f"\n  === CURRENT ENGINE (cohort {cfg.COHORT_START}) ===")
    if not done and not live:
        print(f"  No trades yet. Everything in the book above predates "
              f"{cfg.COHORT_START} and is a different engine.")
        return
    print(f"  open {len(live)}   closed {len(done)}")
    if not done:
        print("  Nothing closed yet — no P&L to report.")
        return
    wins = sum(1 for t in done if (t.get('pnl') or 0) > 0)
    gross = sum(float(t.get('pnl') or 0) for t in done)
    costed = [_trade_cost(t) for t in done]
    fees = sum(c for c, _ in costed)
    bases = {}
    for _, b in costed:
        bases[b] = bases.get(b, 0) + 1
    floors = bases.get('brokerage_only', 0)
    pcts = sorted(float(t.get('pnl_pct') or 0) for t in done)
    med = pcts[len(pcts) // 2]
    print(f"  WR {wins}/{len(done)} ({wins / len(done) * 100:.0f}%)   "
          f"median trade {med:+.2f}% gross")
    print(f"  GROSS Rs {gross:,.0f}   fees Rs {fees:,.0f}"
          f"{' (a FLOOR)' if floors else ''}   NET Rs {gross - fees:,.0f}")
    print("  fee basis: "
          + ', '.join(f'{n} {b}' for b, n in sorted(bases.items())))
    if floors:
        # Those rows are missing STT, exchange, SEBI and stamp entirely.
        print(f"    {floors} record(s) have no per-leg prices, so their cost "
              f"is brokerage only — the NET above is BETTER than reality")
    n_fill = sum(1 for t in done if t.get('pricing_basis') == 'fill')
    print(f"  fill-basis records: {n_fill}/{len(done)}"
          f"{'' if n_fill >= 30 else '  (need ~30 to judge the cost question)'}")


def _trade_cost(trade: dict):
    """Round-trip cost for one closed trade: (rupees, basis).

    Prefers the cost STAMPED at close (`pnl - pnl_net`, computed then from the
    real exit book) and re-estimates only where that is absent — the records
    predating the stamp, and anything closed before `fees.py` existed.

    The scorecard used to hardcode its own copy of the rates. Every literal
    matched `cfg.FEE_RATES` exactly, which is what made it dangerous rather
    than merely redundant: nothing was wrong on the day it was written, and
    nothing would look wrong on the day it went stale. The go-live plan
    reconciles those rates against the first real contract note, after which
    the digest would move and this number would not — and this is the number
    the scale-up decision reads.

    `basis` matters as much as the rupees. `zebra.fees` charges a brokerage
    FLOOR for records with no per-leg prices, and that floor is missing STT
    entirely, so those rows UNDERSTATE cost. Counting them separately is the
    difference between "our fees are X" and "our fees are at least X on a
    third of the book".
    """
    pnl, net = trade.get('pnl'), trade.get('pnl_net')
    if pnl is not None and net is not None:
        try:
            return max(0.0, float(pnl) - float(net)), 'stamped'
        except (TypeError, ValueError):
            pass
    try:
        from .fees import round_trip_for_trade
        est = round_trip_for_trade(trade, trade.get('exit_debit'))
        total = float(est.get('total') or 0.0)
        if total != total:                      # NaN
            return 0.0, 'uncostable'
        return total, str(est.get('basis') or 'modelled')
    except Exception:
        # Never let a costing failure take down the scorecard.
        return 0.0, 'uncostable'


def cmd_spotstop(args):
    """What an adverse-spot stop WOULD have done. It is armed by nothing.

    `spot_sl_enabled` is False and this command does not change that. Its job
    is to answer one question the cohort cannot yet answer: does the rule fire
    correctly often enough to pay for itself?

    THE DENOMINATOR IS FIRINGS, NOT TRADES. Five of five is not evidence: the
    95% lower bound on 5/5 is 54.9%, below the ~70% bar. Ten straight correct
    firings with none false is the fastest a green light can arrive.

    THREE COLUMNS EXIST TO STOP THE COUNT FLATTERING ITSELF:

    * **gaps** — a breach first seen inside the open buffer is one the stop did
      not catch at its level; it booked wherever the gap landed. Two of the
      cohort's five were gaps.
    * **1-print** — every value trigger in this engine is debounced, so a
      breach that did not survive to the next poll is a firing no armed rule
      would have taken.
    * **saved / cost** — measured from the stored breach value against the
      trade's real exit, NOT from the replay's constants. A firing on a loser
      is only a GOOD firing if stopping there would have booked better than
      what actually happened, and outcome-labelling alone cannot see that.

    `missed` is the other half nobody would otherwise print: losers the rule
    never fired on at all. Without it "5 of 5 caught" has no place to be
    contradicted.
    """
    import statistics as st

    from zebra import spot_shadow as ss
    from .trade_store import get_store, in_cohort

    store = get_store()
    trades = [t for t in store.load_trades() if in_cohort(t)]
    if not getattr(args, 'all', False):
        trades = [t for t in trades if t.get('status') == 'exited']
    shadowed = [t for t in trades if t.get(ss.FIELD)]
    if not shadowed:
        print()
        print('No shadowed positions yet. The shadow starts accruing on the '
              'next poll after deploy; nothing before it can be back-filled, '
              'because it needs the 5-minute spot path.')
        print()
        return

    print()
    print('ADVERSE-SPOT STOP — SHADOW ONLY. spot_sl_enabled is False.')
    print()
    hdr = (f"{'id':>4} {'stock':<12}{'d':<3}{'net%':>7}{'res':<5}{'MAE%':>7}"
           + ''.join(f"{('b%g' % t):>22}" for t in ss.THRESHOLDS))
    print(hdr)
    print('-' * len(hdr))

    stat = {t: {'true': 0, 'false': 0, 'gap': 0, 'oneprint': 0,
                'saved': [], 'cost': []} for t in ss.THRESHOLDS}
    missed = {t: 0 for t in ss.THRESHOLDS}
    partial = 0
    for t in sorted(shadowed, key=lambda x: x['id']):
        sh = t[ss.FIELD]
        net = t.get('pnl_net')
        win = None if (net is None or t.get('status') != 'exited') else net > 0
        # A record the shadow started watching AFTER its own entry has an
        # unobserved head: its MAE is a lower bound and a breach may already
        # have happened before anyone was looking.
        head = (sh.get('since') or '')[:10] > (t.get('entry_date') or '')
        partial += bool(head)
        row = (f"{t['id']:>4} {t.get('stock', ''):<12}"
               f"{t.get('direction', ''):<3}"
               f"{(t.get('pnl_net_pct') or 0):>7.1f}"
               f"{('WIN' if win else 'LOSS' if win is not None else 'OPEN'):<5}"
               f"{sh.get('mae_pct', 0):>7.2f}")
        for thr in ss.THRESHOLDS:
            b = sh.get(ss.breach_key(thr))
            if not b:
                row += f"{('-' + (' PARTIAL' if head else '')):>22}"
                if win is False:
                    missed[thr] += 1
                continue
            tags = []
            if b.get('gap') is not False:
                tags.append('GAP' if b.get('gap') else 'GAP?')
            if not b.get('confirmed_at'):
                tags.append('1-print')
            if b.get('q') != 'ok':
                tags.append(str(b.get('q'))[:9])
            row += f"{('%.2f%% %s' % (b['adverse_pct'], ','.join(tags) or 'ok')):>22}"
            if win is None:
                continue
            s = stat[thr]
            s['gap'] += b.get('gap') is not False
            s['oneprint'] += not b.get('confirmed_at')
            s['false' if win else 'true'] += 1
            # What the stop would have BOOKED against what actually happened.
            # Only where the breach carries a price the engine would have
            # acted on — an unusable one is exactly the case a real stop could
            # not have filled at either.
            if b.get('q') == 'ok' and b.get('value') is not None \
                    and t.get('exit_debit') is not None:
                delta = (float(b['value']) - float(t['exit_debit'])) \
                    * int(t.get('quantity') or 0)
                (s['saved'] if delta > 0 else s['cost']).append(delta)
        print(row)

    print()
    print(f"{'thr':>5}{'fire':>6}{'true':>6}{'FALSE':>7}{'acc':>6}"
          f"{'ex-gap acc':>12}{'gaps':>6}{'1-print':>9}{'missed':>8}"
          f"{'net Rs':>12}")
    for thr in ss.THRESHOLDS:
        s = stat[thr]
        n = s['true'] + s['false']
        acc = '-' if not n else f"{s['true'] / n * 100:.0f}%"
        clean = n - s['gap']
        exgap = '-' if clean <= 0 else f"{(s['true'] - s['gap']) / clean * 100:.0f}%"
        net = sum(s['saved']) + sum(s['cost'])
        print(f"{thr:>5.1f}{n:>6}{s['true']:>6}{s['false']:>7}{acc:>6}"
              f"{exgap:>12}{s['gap']:>6}{s['oneprint']:>9}{missed[thr]:>8}"
              f"{net:>12,.0f}")
    print()
    if partial:
        print(f'{partial} row(s) marked PARTIAL: the shadow started after the '
              f'entry, so their MAE is a LOWER BOUND and an earlier breach may '
              f'have gone unseen.')
    print('A firing must be right ~70% of the time to break even at 1.5% and')
    print('~78% at 2.0% (from the replay: one true stop saves ~25.6 points of')
    print('debit, one false one costs ~60.5). Judge the accuracy column')
    print('against THAT — and prefer the ex-gap column, because a GAP is a')
    print('firing the rule did not earn. `net Rs` is measured from the stored')
    print('breach prices against each trade\'s real exit, so it can disagree')
    print('with the accuracy columns; when it does, believe it.')
    print()


def cmd_depth(args):
    """Report the depth this book has actually observed at the touch.

    The evidence half of the lot-scaling question (owner, 2026-08-30: *"let us
    collect oi and depth in coming weeks and decide"*). Entry-side depth is
    stamped on each record at entry; the exit side is sampled every
    `depth.SAMPLE_SEC` while a position is open, because closing at size is
    the constraint that has actually cost money here.

    Open positions only by default. A closed record's histogram is still
    valid evidence, but the question being asked is "how big can the NEXT
    entry be", and the open book is the one whose books are current.
    """
    from zebra import depth as depth_mod
    from .trade_store import get_store, in_cohort

    store = get_store()
    trades = [t for t in store.load_trades() if in_cohort(t)]
    if not getattr(args, 'all', False):
        trades = [t for t in trades if t.get('status') == 'entered']

    print()
    print(depth_mod.report(trades))
    print()

    # -- the entry side, straight off the records --------------------------
    rows = []
    for t in trades:
        lot = t.get('lot_size')
        aq, bq = t.get('long_ask_qty_entry'), t.get('short_bid_qty_entry')
        if not lot or aq is None or bq is None:
            continue
        rows.append((t.get('stock'), int(min(int(aq), int(bq)) // int(lot)),
                     t.get('long_oi_entry'), t.get('short_oi_entry')))
    print('ENTRY DEPTH — lots the touch could absorb when the position opened')
    print('-' * 66)
    if not rows:
        print('  none recorded yet. Depth has been persisted at entry since')
        print('  2026-08-30; records opened before that carry OI but no size,')
        print('  and OI is NOT a substitute — it counts open contracts, not')
        print('  resting size at the touch.')
    else:
        print('%-14s %-8s %-12s %s' % ('stock', 'lots', 'long OI', 'short OI'))
        for stock, lots, loi, soi in sorted(rows, key=lambda r: r[1]):
            print('%-14s %-8d %-12s %s' % (stock, lots, loi, soi))
        worst = min(r[1] for r in rows)
        print()
        print('  thinnest entry seen: %d lot(s). The ladder should not be'
              % worst)
        print('  raised past what the THINNEST book carries, because the')
        print('  scanner picks the stock, not the size.')
    print()
    print('Decide from BOTH tables. Entry depth bounds how big a position can')
    print('be opened; exit depth bounds whether it can be closed again, and')
    print('only the second one is unbounded when it goes wrong.')
    return 0


def cmd_status(args):
    # WHOLE BOOK: the dashboard is the one place that deliberately shows
    # everything, including the retired engine, so the operator can see what
    # is on disk. It reports counts; it does not compute a P&L or a gate.
    from .trade_store import get_store
    store = get_store()
    trades = store.load_trades()
    by_status = {}
    for t in trades:
        by_status.setdefault(t.get('status', '?'), []).append(t)

    print("\n=== ZEBRA DASHBOARD ===")
    for st in ('watching', 'triggered', 'entered', 'exited', 'cancelled'):
        lst = by_status.get(st, [])
        print(f"  {st:<10} {len(lst)}")

    entered = by_status.get('entered', [])
    if entered:
        from . import config as cfg
        print(f"\n  --- Open Trades ---")
        for t in entered:
            sl_txt = f"SL={t['sl_spot']:.2f} " if cfg.SPOT_SL_ENABLED else ""
            st_tag = ' [BCS]' if t.get('structure') == 'bcs' else ''
            print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<3} "
                  f"{int(t['long_strike'])}/{int(t['short_strike'])} "
                  f"debit={t['debit']:.2f} TP={t['tp_spot']:.2f} "
                  f"{sl_txt}exp={t['expiry']}{st_tag}")

    # ── FROZEN (M14) ─────────────────────────────────────────────────────
    #
    # Above `Watching`, deliberately: a frozen record is a position that may be
    # LIVE at the broker with nothing monitoring it, while a watchlist entry is
    # a trade that does not exist yet. Ordering a dashboard by risk is the job.
    #
    # These are counted in NO other line here — `partial_close` is not one of
    # the statuses looped over above — so before this section the answer to
    # "what do I have open" simply omitted them, which is the same misreport
    # that once answered `Open: 0` with eight positions live.
    frozen = by_status.get('partial_close', [])
    if frozen:
        print(f"\n  --- FROZEN: live legs, NOT monitored ({len(frozen)}) ---")
        for t in frozen:
            cf = t.get('close_failure') or {}
            print(f"  #{t['id']} {t['stock']:<12} "
                  f"{t.get('direction', '?'):<3} "
                  f"cause={cf.get('cause', 'legacy')} "
                  f"state={cf.get('state', 'legacy')} "
                  f"attempts={cf.get('attempts', 0)} "
                  f"since={cf.get('frozen_at', '?')}")
        print("  Clear with: python -m bcs.spread_monitor "
              "--book-frozen cohort:ID  |  --reopen-frozen cohort:ID")

    # ── POST-CLOSE RESIDUE (S3) ──────────────────────────────────────────
    #
    # Beside FROZEN and for the identical reason. These records ARE counted
    # elsewhere on this dashboard - under `exited`, as finished trades - which
    # makes them worse to omit, not better: the book says the position is over
    # and the broker says a leg is still there. Escalate-only; nothing in the
    # fleet will ever place an order against a closed record.
    residue = [t for t in trades
               if (t.get('reconcile_residue') or {}).get('state') == 'open']
    if residue:
        print(f"\n  --- POST-CLOSE RESIDUE: booked EXITED, a leg is still "
              f"LIVE ({len(residue)}) ---")
        for t in residue:
            r = t.get('reconcile_residue') or {}
            print(f"  #{t['id']} {t['stock']:<12} "
                  f"{r.get('detail', '?')}  since={r.get('detected_at', '?')}")
        print("  Close the leg in Kite and it resolves itself, or: "
              "python -m bcs.spread_monitor --clear-residue cohort:ID")

    watching = by_status.get('watching', [])
    if watching:
        print(f"\n  --- Watching ---")
        for t in watching:
            last = t.get('last_gap_pct', t['signal_gap_pct'])
            print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<3} "
                  f"{t['timeframe']:<8} signal_gap={t['signal_gap_pct']:.2f}% "
                  f"last={last:.2f}%")

    exited = by_status.get('exited', [])
    if exited:
        _print_cohort_scoreboard(exited, entered)
        print(f"\n  --- Performance (WHOLE BOOK, all engines) ---")
        for label, group in (
                ('Zebra', [t for t in exited if t.get('structure') != 'bcs']),
                ('BCS  ', [t for t in exited if t.get('structure') == 'bcs'])):
            if not group:
                continue
            wins = sum(1 for t in group if t.get('pnl', 0) > 0)
            total_pnl = sum(t.get('pnl', 0) or 0 for t in group)
            win_rate = wins / len(group) * 100
            print(f"  {label}  P&L: Rs {total_pnl:,.0f}  "
                  f"WR {win_rate:.0f}% ({wins}W / {len(group)-wins}L)")
    _print_vet_status(trades)
    print()


def _print_vet_status(trades: list) -> None:
    """The vetting layer, in the ONE dashboard — not a second status command.

    Without this an operator cannot tell "dark" from "on and working" from "on
    and silently dead" without grepping logs, and the two failure modes this
    layer is most likely to hit on the Pi (CLI not found, agents never
    reporting back) are exactly the invisible kind.
    """
    from . import config as cfg
    from . import events as events_mod
    from . import health as health_mod
    from . import vet as vet_mod

    if not cfg.VET_ENABLED:
        print("\n  --- Claude vetting: OFF (dark) ---")
        print(f"  enable in {cfg.CONFIG_FILE.name} -> \"vet_enabled\": true "
              f"(NOT via env: one process ON and another OFF is worse than "
              f"dark)")
        return

    h = health_mod.status()
    cli = vet_mod.resolve_cli() or 'NOT FOUND'
    print("\n  --- Claude vetting: ON ---")
    print(f"  cli       {cli}")
    days = h.get('days_left')
    print(f"  login     expires in {days}d" if days is not None
          else "  login     expiry unknown (behavioural probes only)")
    silent = h.get('silent_channels') or []
    fails = h.get('spawn_failures') or 0
    verdict = ('HEALTHY' if not silent and not fails
               else 'DEGRADED — agents are not completing')
    print(f"  agents    {verdict}"
          + (f"  silent={silent}" if silent else "")
          + (f"  spawn_failures={fails}" if fails else ""))

    pend = [t for t in trades if vet_mod.is_pending(t)]
    if pend:
        print(f"  pending   {len(pend)} signal(s) awaiting a verdict: "
              f"{[t['id'] for t in pend]}")
    # The QUEUE, on the one dashboard. Entries can legally wait an hour here
    # and are NOT entering while they do, so a queue nobody can see is a
    # trading pause nobody can see — the whole reason fail-closed needed to be
    # observable. Shows how close each one is to being dropped.
    queued = [t for t in trades if vet_mod.is_queued(t)]
    if queued:
        def _left(t):
            drop = vet_mod._parse((t.get('vet') or {}).get('drop_after'))
            if drop is None:
                return '?'
            mins = int((drop - vet_mod._now()).total_seconds() // 60)
            return f'{mins}m' if mins > 0 else 'due'
        print(f"  QUEUED    {len(queued)} entry(s) waiting for an agent slot — "
              "NOT entering: "
              + ', '.join('#%s %s (%s left, %d attempt(s))'
                          % (t['id'], t.get('stock'),
                             _left(t), (t.get('vet') or {}).get('attempts') or 0)
                          for t in queued))
    starved = [t for t in trades
               if vet_mod.vet_state(t) == vet_mod.STARVED
               and t.get('status') in ('watching', 'triggered')]
    if starved:
        print(f"  STARVED   {len(starved)} entry(s) dropped, awaiting reap: "
              f"{[t['id'] for t in starved]}")
    held = [(t['id'], k, vet_mod.exit_defers(t, k))
            for t in trades if isinstance(t.get('exit_vet'), dict)
            for k in t['exit_vet']
            if vet_mod.exit_state(t, k) == vet_mod.DEFER]
    if held:
        print(f"  exits     HELD, awaiting you: "
              + ', '.join('#%s %s (%d defers)' % x for x in held))
    stale = events_mod.is_stale()
    n_events = len(events_mod.load().get('events') or [])
    print(f"  calendar  {n_events} event(s)"
          + ("  STALE — refresh in flight or failing" if stale else "  fresh"))


def cmd_reset(args):
    """Wipe in-flight signals (watching/triggered/entered) — for one-time cleanup
    before going to paper mode, or to reset after a regime change. Already-exited
    or cancelled trades are kept for history.
    """
    import json
    import shutil
    from datetime import datetime
    from pathlib import Path
    from .trade_store import get_store
    from . import config as cfg

    store = get_store()
    # WHOLE BOOK: reset cancels everything IN FLIGHT. A legacy signal left
    # `triggered` is exactly what this verb is for, and scoping it out would
    # leave the records it exists to clear.
    all_trades = store.load_trades()
    open_states = ('watching', 'triggered', 'entered')
    in_flight = [t for t in all_trades if t.get('status') in open_states]

    if not in_flight:
        print("No in-flight signals to reset (0 watching/triggered/entered).")
        return

    print(f"In-flight signals to be cancelled:")
    for t in in_flight:
        print(f"  #{t['id']} {t['stock']:<12} {t['direction']:<3} "
              f"{t['status']:<10} {t['timeframe']:<8}")

    if not args.confirm:
        print(f"\n{len(in_flight)} signal(s) would be cancelled. "
              f"Re-run with --confirm to apply.")
        return

    # Archive the current file before mutating
    cfg.LOG_DIR.mkdir(exist_ok=True)
    archive_dir = cfg.LOG_DIR / 'archive' / datetime.now().strftime('%Y-%m-%d')
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    archive_path = archive_dir / f'zebra_trades_pre_reset_{stamp}.json'
    if cfg.LOCAL_FILE.exists():
        shutil.copy(cfg.LOCAL_FILE, archive_path)
        print(f"\nArchived current store -> {archive_path}")

    # Cancel each in-flight via the store API (so version + Drive sync are correct)
    cancelled = 0
    for t in in_flight:
        try:
            if t['status'] == 'entered':
                # Force-close at debit (max loss assumption — paper trade, not real money)
                store.mark_exited(t['id'], t.get('entry_spot', 0), None,
                                  'reset_force_close')
            else:
                store.cancel(t['id'], 'reset')
            cancelled += 1
        except ValueError as e:
            print(f"  WARN: #{t['id']} skip — {e}")

    print(f"Reset complete: {cancelled} signal(s) closed/cancelled. "
          f"Local + Drive synced.")


def cmd_report(args):
    from .report import run as run_report
    run_report(
        report_type=args.report_type,
        send_telegram=args.telegram,
        no_kite=args.no_kite,
    )


def cmd_paths(args):
    """Capture / inspect the persisted value paths.

    The POLL observations are the only record of what the engine saw, and they
    live in logs the cleaner deletes at 90 days. `zebra digest` captures them
    daily; this verb exists to backfill, to force a re-extract, and above all
    to ANSWER WHETHER IT IS WORKING -- `at_risk` names sessions whose numbers
    still exist only in a file scheduled for deletion.
    """
    from . import value_paths as vp_mod
    if args.backfill or args.date:
        if args.date:
            p = vp_mod.write_day(args.date, force=args.force)
            print('wrote %s' % p if p else
                  'nothing written for %s (already captured, or no log)'
                  % args.date)
        else:
            r = vp_mod.capture(force=args.force)
            print('captured %d session(s): %s'
                  % (len(r['written']), ', '.join(r['written']) or '-'))
            print('already had %d' % len(r['already_had']))
    cov = vp_mod.coverage()
    print('')
    print('captured sessions : %d (%d observations)'
          % (len(cov['captured']), cov['observations']))
    print('session logs found: %d' % len(cov['sessions_on_disk']))
    if cov['at_risk']:
        print('AT RISK (only in a log the cleaner will delete): %s'
              % ', '.join(cov['at_risk']))
    else:
        print('AT RISK: none - every session on disk is captured')
    return 0


def cmd_digest(args):
    """One day reduced to something readable — the paper-run record.

    Distinct from `report`, which sends a P&L summary to a phone. This is the
    diagnostic digest we read together: what ran, what refused, what earns a
    look. Deterministic and offline, so it can never compete with the trading
    vets for the agent budget.
    """
    # CAPTURE THE VALUE PATHS FIRST, and never let a failure here cost the
    # digest. This rides on the digest cron rather than getting its own line
    # for one reason: the digest cron ALREADY went uninstalled for 18 days
    # without anyone noticing, and adding a second independent line doubles
    # the number of things that can be quietly absent. One line, two jobs.
    #
    # It runs over every session log on disk, not just today's, so a missed
    # day is picked up later — the log survives 90 days, the capture only has
    # to happen once inside that window.
    try:
        from . import value_paths as vp_mod
        vp_mod.capture()
    except Exception as e:                       # a digest must still be written
        logging.getLogger(__name__).warning(
            'value-path capture failed (digest continues): %s', e)

    from . import digest as digest_mod
    path = digest_mod.write(args.date)
    text = path.read_text(encoding='utf-8')
    # The FILE stays UTF-8; only the console gets downgraded. A Windows cp1252
    # terminal raises on the arrows and flags, and a digest that crashes on
    # display is a digest nobody runs twice.
    enc = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(enc, 'replace').decode(enc, 'replace'))
    print(f"\n[written to {path}]")
    return 0


def cmd_pm_show(args):
    """Evidence bundle for one post-mortem — the agent's only input."""
    import json as _json
    from . import postmortem as pm_mod
    from .trade_store import get_store
    print(_json.dumps(pm_mod.context(get_store(), args.id), indent=2,
                      default=str))
    return 0


def cmd_pm_record(args):
    from . import postmortem as pm_mod
    from .health import record_agent_landed
    from .trade_store import get_store
    record_agent_landed('postmortem')   # proof of life for THIS channel
    try:
        pm = pm_mod.record(get_store(), args.id, args.tag, args.lesson)
    except ValueError as e:
        print(f"REJECTED: {e}")
        return 1
    print(f"post-mortem #{args.id} recorded ({pm['basis']}): "
          f"{', '.join(pm['tags'])}")
    return 0


def cmd_pm_pending(args):
    from . import postmortem as pm_mod
    from .trade_store import get_store
    rows = pm_mod.pending(get_store())
    if not rows:
        print("Nothing settled is waiting for a post-mortem.")
        return 0
    print(f"{len(rows)} awaiting a post-mortem:")
    for r in rows:
        print(f"  #{r['trade_id']:>4}  {r['basis']:<8}  settled {r['settled']}")
    return 0


def cmd_pm_precedents(args):
    from . import postmortem as pm_mod
    from .trade_store import get_store
    store = get_store()
    view = pm_mod.for_signal(store, args.stock)
    allr = pm_mod.precedents(store)
    if not allr:
        print("\nNo post-mortems recorded yet — precedents fill in as "
              "positions settle and the EOD batch classifies them.\n")
        return 0
    print(f"\nPRECEDENTS  (shown to the vetting agent: {len(view['shown'])}, "
          f"withheld: {len(view['withheld'])})")
    print(f"  {'TAG':<24} {'SUP':>4} {'REAL':>5} {'WIN%':>6} {'LEANS':>6}  "
          f"{'REALISED P&L':>13}")
    for r in allr:
        print(f"  {r['tag']:<24} {r['support']:>4} {r['realised_support']:>5} "
              f"{(r['win_rate'] if r['win_rate'] is not None else 0):>6.1f} "
              f"{r['leans']:>6}  {r['realised_pnl']:>13,.0f}")
    for w in view['withheld']:
        # Printed loudly: a suppressed precedent is itself information, and a
        # guard nobody can see is a guard nobody will trust.
        print(f"\n  WITHHELD  {w['tag']}: {w['withheld_because']}")
    print()
    return 0


def cmd_pm_run(args):
    """The EOD batch. Spawns at most one agent a day, only if something settled."""
    from . import postmortem as pm_mod
    from .trade_store import get_store
    store = get_store()
    if not pm_mod.due(store) and not args.force:
        print("Not due: nothing settled since the last run (or already ran "
              "today). --force overrides.")
        return 0
    spawned = pm_mod.spawn_batch(store, spawn=not args.dry_run)
    print("post-mortem batch spawned" if spawned else
          "post-mortem batch NOT spawned (dry-run or CLI unavailable)")
    return 0


def cmd_giveback(args):
    """Peak-vs-exit table for closed trades that carry a measured peak."""
    from . import mfe as mfe_mod
    from .trade_store import get_store, scored
    store = get_store()
    trades = scored(store.load_trades())
    g = mfe_mod.giveback(trades, min_progress=args.min_progress)

    print(f"\nGIVE-BACK  ({g['measured']} measured, "
          f"{g['unmeasured']} closed before MFE capture existed)")
    if not g['measured']:
        # Say this plainly rather than printing an empty table that reads as
        # "no leak found". Capture only starts on the next monitor poll of an
        # OPEN trade, so this is the expected state right after deployment.
        print("  Nothing measured yet — peaks are recorded from the first "
              "monitor poll after deployment, so this fills in as open "
              "trades close.\n")
        return

    print(f"  was ahead at some point : {g['was_ahead']}")
    print(f"  total handed back       : Rs {g['given_back']:,.0f}")
    if g['kept_pct_median'] is not None:
        print(f"  median % of peak kept   : {g['kept_pct_median']:.1f}%")
    print(f"  reached >={args.min_progress:.0%} to target then LOST: "
          f"{g['reached_then_lost']} "
          f"(Rs {g['reached_then_lost_pnl']:,.0f})")

    print(f"\n  {'#':>4} {'STOCK':<12} {'STRUCT':<6} {'PEAK':>10} {'P&L':>10} "
          f"{'BACK':>10} {'KEPT':>6} {'TP%':>6}  EXIT")
    for r in g['rows']:
        kept = f"{r['kept_pct']:.0f}%" if r['kept_pct'] is not None else '-'
        back = f"{r['given_back']:,.0f}" if r['given_back'] is not None else '-'
        prog = f"{r['tp_progress'] * 100:.0f}%" if r.get('tp_progress') is not None else '-'
        print(f"  {r['id']:>4} {r['stock']:<12} {r['structure']:<6} "
              f"{r['peak_gain']:>10,.0f} {r['pnl']:>10,.0f} {back:>10} "
              f"{kept:>6} {prog:>6}  {r['reason'] or ''}")
    print()


def main():
    from . import vet as vet_mod          # exit-kind choices, single source
    p = argparse.ArgumentParser(prog='python -m zebra',
                                description='Zebra — synthetic long/short option strategy')
    p.add_argument('-v', '--verbose', action='store_true', help='Debug logging')
    sub = p.add_subparsers(dest='command')

    p_scan = sub.add_parser('scan', help='Chartink scan + watchlist add')
    p_scan.add_argument('--dry-run', action='store_true')
    p_scan.set_defaults(func=cmd_scan)

    p_run = sub.add_parser('run', help='One full cycle (cron target)')
    p_run.add_argument('--dry-run', action='store_true')
    p_run.set_defaults(func=cmd_run)

    p_loop = sub.add_parser('loop', help='Long-running market-hours loop')
    p_loop.add_argument('--dry-run', action='store_true')
    p_loop.set_defaults(func=cmd_loop)

    p_list = sub.add_parser('list', help='List trades')
    p_list.add_argument('--status', choices=['watching', 'triggered',
                                              'entered', 'exited', 'cancelled'])
    p_list.set_defaults(func=cmd_list)

    # ── Claude vetting layer (called BY the spawned CLI, not by the user) ──
    p_vet = sub.add_parser('vet', help='Claude vetting layer')
    vet_sub = p_vet.add_subparsers(dest='vet_command')

    p_vshow = vet_sub.add_parser('show', help='Dump vetting context as JSON')
    p_vshow.add_argument('id', type=int)
    p_vshow.add_argument('--exit', choices=list(vet_mod.EXIT_KINDS),
                         default=None, help='Show EXIT context for this trigger')
    p_vshow.set_defaults(func=cmd_vet_show)

    p_vexit = vet_sub.add_parser('exit-decide', help='Record an exit verdict')
    p_vexit.add_argument('id', type=int)
    p_vexit.add_argument('--kind', required=True,
                         choices=list(vet_mod.EXIT_KINDS))
    p_vexit.add_argument('--verdict', required=True, choices=['allow', 'defer'])
    p_vexit.add_argument('--reason', action='append')
    p_vexit.add_argument('--red-flag', action='append')
    p_vexit.add_argument('--confidence', type=float, default=None)
    p_vexit.add_argument('--notes', default='')
    p_vexit.set_defaults(func=cmd_vet_exit_decide)

    p_vdec = vet_sub.add_parser('decide', help='Record a verdict')
    p_vdec.add_argument('id', type=int)
    p_vdec.add_argument('--verdict', required=True, choices=['allow', 'veto'])
    p_vdec.add_argument('--reason', action='append',
                        help='Repeatable: one checklist finding per flag')
    p_vdec.add_argument('--red-flag', action='append',
                        help='Repeatable: a concrete risk found')
    p_vdec.add_argument('--confidence', type=float, default=None)
    p_vdec.add_argument('--notes', default='')
    p_vdec.set_defaults(func=cmd_vet_decide)

    p_vscore = vet_sub.add_parser('score', help='Was the vetting layer right?')
    p_vscore.add_argument('--list', type=int, nargs='?', const=20, default=0,
                          metavar='N', help='Also print the last N decisions')
    p_vscore.add_argument('--no-join', action='store_true',
                          help='Report only; do not join new outcomes first')
    p_vscore.set_defaults(func=cmd_vet_score)

    # ── Event calendar (refreshed BY a Sonnet agent, read by the gates) ────
    p_ev = sub.add_parser('events', help='Event calendar')
    ev_sub = p_ev.add_subparsers(dest='events_command')

    p_evshow = ev_sub.add_parser('show', help='Print upcoming events')
    p_evshow.add_argument('--symbol', default=None)
    p_evshow.add_argument('--days', type=int, default=None)
    p_evshow.set_defaults(func=cmd_events_show)

    p_evrep = ev_sub.add_parser('replace', help='Install a validated calendar')
    p_evrep.add_argument('--file', required=True,
                         help='JSON: {"events":[...]} or a bare [...] list')
    p_evrep.add_argument('--allow-empty', action='store_true',
                         help='Install a calendar with NO events. Only when '
                              'the window genuinely has none — an empty '
                              'install also refreshes the timestamp, so it '
                              'reads healthy while every gate sees nothing.')
    p_evrep.set_defaults(func=cmd_events_replace)

    # ── Position review (called BY the spawned CLI) ────────────────────────
    p_rev = sub.add_parser('review', help='Periodic position review')
    rev_sub = p_rev.add_subparsers(dest='review_command')

    p_rvshow = rev_sub.add_parser('show', help='Dump review context as JSON')
    p_rvshow.add_argument('id', type=int)
    p_rvshow.set_defaults(func=cmd_review_show)

    p_rvrec = rev_sub.add_parser('record', help='Record a recommendation')
    p_rvrec.add_argument('id', type=int)
    p_rvrec.add_argument('--action', required=True,
                         choices=['hold', 'adjust', 'exit'])
    p_rvrec.add_argument('--reason', action='append')
    p_rvrec.add_argument('--red-flag', action='append')
    p_rvrec.add_argument('--confidence', type=float, default=None)
    p_rvrec.add_argument('--notes', default='')
    p_rvrec.set_defaults(func=cmd_review_record)

    p_qt = sub.add_parser('quote', help='Live re-quote of a signal/position '
                                        '(read-only, JSON)')
    p_qt.add_argument('id', type=int)
    p_qt.set_defaults(func=cmd_quote)

    p_anly = sub.add_parser('analyze', help='Strike analyzer for a stock (no save)')
    p_anly.add_argument('symbol')
    p_anly.add_argument('--timeframe', choices=['monthly', 'weekly'],
                        default='monthly')
    p_anly.add_argument('--direction', choices=['CE', 'PE'], default=None)
    p_anly.add_argument('--max-candidates', type=int, default=3)
    p_anly.set_defaults(func=cmd_analyze)

    p_trg = sub.add_parser('trigger', help='Force analyzer + alert on watching ID')
    p_trg.add_argument('id', type=int)
    p_trg.add_argument('--max-candidates', type=int, default=3)
    p_trg.add_argument('--dry-run', action='store_true')
    p_trg.set_defaults(func=cmd_trigger)

    p_ent = sub.add_parser('enter', help='Mark triggered signal as entered')
    p_ent.add_argument('id', type=int)
    p_ent.add_argument('--pair', required=True, help='K_L/K_S e.g. 700/750')
    p_ent.add_argument('--debit', type=float, required=True)
    p_ent.add_argument('--lots', type=int, required=True)
    p_ent.add_argument('--expiry', required=True, help='YYYY-MM-DD')
    p_ent.add_argument('--long-symbol', default=None)
    p_ent.add_argument('--short-symbol', default=None)
    p_ent.add_argument('--lot-size', type=int, default=None)
    p_ent.add_argument('--entry-spot', type=float, default=None)
    p_ent.add_argument('--live', action='store_true',
                       help='This was PLACED AT THE BROKER. Sets paper=False, '
                            'so the live monitor owns its exits. Normally '
                            'unnecessary — the broker is read and decides.')
    p_ent.add_argument('--paper', action='store_true',
                       help='This was NOT placed. Sets paper=True. Normally '
                            'unnecessary; needed when real legs in the same '
                            'symbols exist for another reason.')
    p_ent.add_argument('--pricing-basis', choices=['fill', 'mid'],
                       default='fill',
                       help="How --debit was priced. 'fill' (default) = what "
                            "you actually paid, ASK(long)-BID(short); the "
                            "monitor then values the position at BID-ASK, "
                            "which is what closing it would pay.")
    p_ent.add_argument('--short-extrinsic', type=float, default=None,
                       help='Short-leg time value at entry. Arms the '
                            'intrinsic-floor guard against garbage quotes; '
                            'recovered from the alert when omitted.')
    p_ent.add_argument('--tp', type=float, default=None,
                       help='Target spot, if the ticket quoted a shortened '
                            '(swing) target rather than the ST line.')
    p_ent.add_argument('--spot-sl-pct', type=float, default=None,
                       help='Override default spot SL percentage (e.g. 0.03)')
    p_ent.add_argument('--structure', choices=['bcs', 'zebra'], default=None,
                       help='What was actually placed. Defaults to the '
                            'configured entry_structure. This is NOT cosmetic: '
                            'it selects the valuation formula, so recording a '
                            'BCS as a zebra disarms its debit SL and doubles '
                            'its P&L.')
    p_ent.set_defaults(func=cmd_enter)

    p_cls = sub.add_parser('close', help='Close entered trade (or cancel watching)')
    p_cls.add_argument('id', type=int)
    p_cls.add_argument('--exit-debit', type=float, default=None,
                       help='Closing net debit per share (struct value)')
    p_cls.add_argument('--exit-spot', type=float, default=None)
    p_cls.add_argument('--reason', default=None)
    p_cls.set_defaults(func=cmd_close)

    p_cnc = sub.add_parser('cancel', help='Cancel watching/triggered signal')
    p_cnc.add_argument('id', type=int)
    p_cnc.add_argument('--reason', default=None)
    p_cnc.set_defaults(func=cmd_cancel)

    p_dep = sub.add_parser(
        'depth',
        help='Depth actually seen at the touch — the lot-scaling evidence')
    p_dep.add_argument('--all', action='store_true',
                       help='include CLOSED positions (default: open only)')
    p_dep.set_defaults(func=cmd_depth)

    p_ssh = sub.add_parser(
        'spotstop',
        help='Adverse-spot stop, SHADOWED — the firing count, not a switch')
    p_ssh.add_argument('--all', action='store_true',
                       help='include OPEN positions (default: closed only, '
                            'because an open one has no outcome to score)')
    p_ssh.set_defaults(func=cmd_spotstop)

    p_sts = sub.add_parser('status', help='Dashboard')
    p_sts.set_defaults(func=cmd_status)

    p_rst = sub.add_parser('reset',
                           help='Cancel all in-flight signals (watching/triggered/entered)')
    p_rst.add_argument('--confirm', action='store_true',
                       help='Actually apply; without this, prints what would happen')
    p_rst.set_defaults(func=cmd_reset)

    p_vp = sub.add_parser(
        'paths',
        help='capture/inspect the persisted POLL value paths (evidence that '
             'outlives the logs)')
    p_vp.add_argument('--backfill', action='store_true',
                      help='capture every session log on disk')
    p_vp.add_argument('--date', default=None, help='capture one YYYY-MM-DD')
    p_vp.add_argument('--force', action='store_true',
                      help='re-extract a day already captured')
    p_vp.set_defaults(func=cmd_paths)

    p_dig = sub.add_parser(
        'digest',
        help='diagnostic digest of one day (paper-run record; NOT the P&L '
             'report — that is `report`)')
    p_dig.add_argument('--date', default=None,
                       help='YYYY-MM-DD, default today')
    p_dig.set_defaults(func=cmd_digest)

    p_rep = sub.add_parser('report',
                           help='EOD daily or Friday weekly performance report')
    p_rep.add_argument('--type', dest='report_type',
                       choices=['daily', 'weekly', 'auto'], default='auto',
                       help='auto = weekly on Fri, daily on Mon-Thu (default)')
    p_rep.add_argument('--telegram', action='store_true',
                       help='Also send to Telegram')
    p_rep.add_argument('--no-kite', action='store_true',
                       help='Skip live mid fetch for open positions (faster, no unrealized P&L)')
    p_rep.set_defaults(func=cmd_report)

    p_pm = sub.add_parser('postmortem', help='Post-mortems and precedents')
    pm_sub = p_pm.add_subparsers(dest='pm_command')
    p_pmshow = pm_sub.add_parser('show', help='Dump post-mortem context as JSON')
    p_pmshow.add_argument('id', type=int)
    p_pmshow.set_defaults(func=cmd_pm_show)
    p_pmrec = pm_sub.add_parser('record', help='Attach a post-mortem')
    p_pmrec.add_argument('id', type=int)
    p_pmrec.add_argument('--tag', action='append', required=True,
                         help='Repeatable; must come from the fixed taxonomy')
    p_pmrec.add_argument('--lesson', required=True)
    p_pmrec.set_defaults(func=cmd_pm_record)
    p_pmpend = pm_sub.add_parser('pending', help='What is awaiting a post-mortem')
    p_pmpend.set_defaults(func=cmd_pm_pending)
    p_pmprec = pm_sub.add_parser('precedents', help='Aggregated tag evidence')
    p_pmprec.add_argument('--stock', default=None)
    p_pmprec.set_defaults(func=cmd_pm_precedents)
    p_pmrun = pm_sub.add_parser('run', help='EOD batch (max 1/day)')
    p_pmrun.add_argument('--force', action='store_true')
    p_pmrun.add_argument('--dry-run', action='store_true')
    p_pmrun.set_defaults(func=cmd_pm_run)

    p_mfe = sub.add_parser('giveback',
                           help='Peak-vs-exit: what winners handed back')
    p_mfe.add_argument('--min-progress', type=float, default=0.7,
                       help='Fraction of the way to target that counts as '
                            '"was winning" (default 0.7)')
    p_mfe.set_defaults(func=cmd_giveback)

    args = p.parse_args()
    setup_logging(args.verbose)
    # The package import above ran before this handler existed, so the
    # startup banners (vetting state, daily-sweep window) were held back.
    # Emit them now, into the log that will actually be read.
    from . import config as _cfg
    _cfg.emit_state_banners()

    if not args.command:
        p.print_help()
        sys.exit(1)
    # A parent command used without its subcommand (e.g. `zebra vet`) parses
    # fine but carries no func — print that group's help instead of an
    # AttributeError traceback.
    if not hasattr(args, 'func'):
        p.parse_args([args.command, '--help'])
        sys.exit(1)
    sys.exit(args.func(args) or 0)


if __name__ == '__main__':
    main()

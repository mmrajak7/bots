"""Zebra monitor — main loop.

Three jobs per cycle:
  1. Scan Chartink + add new WATCHING signals
  2. Check WATCHING signals: if gap <= TRIGGER_GAP_MAX, run strike analyzer
     and send ENTER alert with 2-3 candidate pairs (status → triggered).
     If gap >= WATCH_GAP_MAX*1.2 (drifted away), cancel.
  3. Check ENTERED trades: TP / SPOT SL / DEBIT SL / TIME alerts.

Entry and exit are each gated by their own switch, off by default, falling
back to a manual step when off or refused -- never alert-only unconditionally:
  - Entry: `_auto_enter_bcs` (armed by `cfg.AUTO_ENTRY`), else the alert is
    the order ticket and the user runs `zebra enter ID ...`.
  - Exit: in PAPER mode `_paper_auto_close` always books the trigger; in LIVE
    mode `_exits_external` (armed by `cfg.EXITS_MANAGED_EXTERNALLY` AND
    cohort membership) hands the close to `bcs/spread_monitor.py`'s real
    order path, else it is an alert and the user runs `zebra close ID ...`.
"""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from common import arming as arming_mod
from common import spread_valuation
from common import kite_errors
from common import store_contract

from . import capital
from . import config as cfg
from . import depth as depth_mod
from . import events as events_mod
from . import health as health_mod
from . import history
from . import mfe as mfe_mod
from . import outcomes as outcomes_mod
from . import postmortem as postmortem_mod
from . import review as review_mod
from common import nse_holidays
from . import strikes as strikes_mod
from . import vet as vet_mod
from .scanner import _get_kite, get_ltp, compute_st_for_stock, validate_and_add
from playbook.magnet import scanner as scanner_mod
from .trade_store import (ZebraStore, get_store, in_cohort,
                          is_paper_record, tp_latch, tp_latched,
                          tp_touch_to_fill)

logger = logging.getLogger(__name__)

IST = cfg.IST          # single definition lives in config; alias kept for callers

# ── Telegram ──────────────────────────────────────────────────────────────

_tg_cfg = None
_tg_loaded = False


def _send_telegram(msg: str, dry_run: bool = False) -> bool:
    """Send Telegram alert. Best-effort: never blocks or crashes."""
    if dry_run:
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[DRY] Telegram:\n{safe}\n")
        return True

    # M8. Through the MERGED config, not by opening the overlay file.
    # This used to `json.load(cfg.CONFIG_FILE)`, which is the untracked
    # secrets overlay ALONE — so `telegram.enabled` in the tracked defaults
    # had no effect, and on a box whose overlay has been trimmed to secrets
    # (all of them since 2026-08-26) the key was simply absent. A switch that
    # only works from the layer you are not supposed to edit is not a switch.
    if not cfg.telegram_enabled():
        logger.debug("Telegram disabled by config (telegram.enabled=false)")
        return True

    global _tg_cfg, _tg_loaded
    try:
        if not _tg_loaded:
            if cfg.TELEGRAM_CONFIG.exists():
                with open(cfg.TELEGRAM_CONFIG) as f:
                    _tg_cfg = json.load(f)
            _tg_loaded = True
        if not _tg_cfg:
            logger.warning("Telegram config missing at %s", cfg.TELEGRAM_CONFIG)
            return False
        resp = requests.post(
            f"https://api.telegram.org/bot{_tg_cfg['bot_token']}/sendMessage",
            json={'chat_id': _tg_cfg['chat_id'], 'text': msg, 'parse_mode': 'HTML'},
            timeout=10,
        )
        if resp.status_code != 200:
            # A non-200 used to return False and log NOTHING, so a vanished
            # alert left no trace whatsoever. That is exactly how a single
            # unescaped '<' in an interpolated reason 400s the whole message
            # and the alert simply never arrives. The body carries Telegram's
            # own description of what it objected to, so it goes in the log —
            # and the message with it, because "which alert died" is the first
            # question anyone will ask.
            logger.error("Telegram REJECTED (HTTP %s): %s | message was: %r",
                         resp.status_code, resp.text[:300], msg[:400])
            return False
        return True
    except Exception as e:
        # WARNING, not DEBUG. This is the notification channel for a trading
        # system; when it breaks, the log is the only place that can say so.
        logger.warning("Telegram send failed: %s | message was: %r",
                       e, msg[:200], exc_info=True)
        return False


# ── Market hours ─────────────────────────────────────────────────────────

def _is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    open_h, open_m = cfg.MARKET_OPEN
    close_h, close_m = cfg.MARKET_CLOSE
    after_open = (h, m) >= (open_h, open_m)
    before_close = (h, m) <= (close_h, close_m)
    return after_open and before_close


# ── Alert formatters ──────────────────────────────────────────────────────

def _struct_label(trade: dict) -> str:
    """Alert title tag by structure."""
    return 'BCS' if trade.get('structure') == 'bcs' else 'ZEBRA'


def _alerts_enabled(trade: dict) -> bool:
    """Telegram gate: does this trade's structure get to talk?

    2026-07-17: BCS is the alerting structure; zebra runs silently in the
    background (still auto-trades + shows in EOD reports). Config-driven via
    alert_structures — auto-close/dedup logic is NEVER gated by this, only
    the Telegram sends.

    LIVE-MODE OVERRIDE: when paper_mode is off, alerts are the ONLY exit
    mechanism (no auto-close), so every structure always talks regardless
    of alert_structures. Silencing is a paper-mode luxury.

    COHORT GATE (`alerts_cohort_only`, 2026-08-13): trades this engine did not
    open stop Telegramming. Three properties worth stating, because getting any
    of them wrong turns a notification filter into a safety defect:

    1. It gates the SEND only. `_paper_auto_close` runs whether or not the
       alert went out, so a silenced legacy position still books its exits,
       still records P&L, and still shows in `zebra status`. This function has
       always been send-only and must stay that way.
    2. It is deliberately INSIDE the paper-mode branch, below the live
       override. A pre-cohort record cannot be proven to carry no real money
       from the record alone, and in LIVE the alert IS the exit instruction —
       so live keeps talking about everything. In practice this costs nothing:
       every position opened from the cohort date onward is stamped, so by the
       time live is on there is nothing legacy left to be noisy about.
    3. Consume-once claims are unaffected. A silenced exit still claims and
       still books; it does not sit there re-firing.
    """
    if not cfg.PAPER_MODE:
        return True
    if cfg.ALERTS_COHORT_ONLY and not in_cohort(trade):
        return False
    struct = 'bcs' if trade.get('structure') == 'bcs' else 'zebra'
    return struct in cfg.ALERT_STRUCTURES


def _format_enter_alert(trade: dict, analysis: dict,
                        bcs: Optional[dict] = None) -> str:
    """RETIRED 2026-08-27 — the back-ratio order ticket.

    This rendered the BUY 2x ITM / SELL 1x ATM back ratio from
    `analysis['best']`. The owner decommissioned that structure; `analyze()`
    no longer produces a `best`, so every call would have fallen into the old
    "ZEBRA NO-PAIR" arm and Telegrammed a failure notice about a perfectly
    healthy signal — a dead branch still talking to the operator, which is
    precisely the shape this whole change exists to remove. (It had already
    done that once: the `not bcs` fallback of a dead BCS branch here
    Telegrammed "NO SPREAD" about signals that had a good spread.)

    It raises rather than returning a caption, and it is not deleted, so that
    a caller reintroduced by a merge fails loudly instead of sending a wrong
    ticket. The live ticket is `_format_bcs_enter_alert`.

    Historical records opened as back ratios are unaffected: they are read
    from the store, not re-rendered through here.
    """
    raise RuntimeError(
        "_format_enter_alert is retired: it renders the back-ratio structure, "
        "decommissioned 2026-08-27. Use _format_bcs_enter_alert.")


#: Exits `bcs/spread_monitor.py` takes over when it manages a position.
#:
#: `expiry` is deliberately ABSENT. That is not an exit rule -- it is
#: `_settle_if_expired`, the terminal net that books a record whose expiry has
#: PASSED and whose book has died. Nothing else can ever price that position,
#: and leaving it `entered` forever bans its stock from the scanner. It stays
#: here whatever else moves.
#:
#: Re-audited 2026-08-27 (N2) on the suspicion that the omission was a hole in
#: the interlock on the one date where being wrong is uncapped. It is not, and
#: here is the whole argument so nobody has to re-derive it a third time:
#: `_settle_if_expired` is the ONLY caller that passes `'expiry'`, and it
#: refuses unless `today > exp` -- strictly PAST expiry, when the contracts
#: have already auto-exercised and there is no option left for the order path
#: to close. Declining there protects nothing (nothing is tradeable) and costs
#: everything (the record sits at `entered` for good, and its stock is banned
#: from the scanner by dedup). The omission is only safe while that caller
#: stays the only one, so `test_only_the_terminal_settle_may_close_on_expiry`
#: reads this module's own source and fails if a second `'expiry'` close
#: appears -- particularly one on expiry DAY, where the legs are still live
#: and the argument above does not hold.
EXTERNALLY_MANAGED_EXITS = frozenset({'tp', 'trail', 'spot_sl', 'debit_sl',
                                      'time'})


def _exits_external(trade: dict) -> bool:
    """True when another engine owns this position's exits.

    Three conditions, all required. `EXITS_MANAGED_EXTERNALLY` is the
    operator's switch, thrown in the same step `--dry-run` comes off the
    monitor's crontab line; `in_cohort` is the scope, because the monitor only
    ever loads cohort records (`bcs/zebra_adapter.py`) and the other 450 rows
    in this store have no other engine watching them at all.

    `not is_paper_record` is the third, added 2026-08-27, and it MIRRORS the
    filter in `ZebraStoreAdapter.get_open_trades`: the bridge refuses to hand a
    paper record to the order path, so for a paper record there is no peer to
    stand down FOR. These two predicates must agree exactly — if the adapter
    drops a record and this function still claims someone else owns it, the
    position has no exit engine at all and nothing says so. That is why both
    call one imported definition rather than each testing the flag.

    Getting the AND wrong in the permissive direction is loud -- two engines
    closing one position shows up immediately. Getting it wrong the other way
    is silent: a position with NO exit engine looks exactly like a quiet one.
    So the cohort test is here rather than left to the caller.
    """
    return (cfg.EXITS_MANAGED_EXTERNALLY and in_cohort(trade)
            and not is_paper_record(trade))


# ── Is the engine we stood down for actually there? ─────────────────────────
#
# `_exits_external` above is a ONE-SIDED stand-down: it reads this process's
# own config and nothing else. Handing the stops to a peer without ever
# checking the peer exists produces the system's most dangerous silent state —
# flag on, `bcs/spread_monitor.py` not running, NO exit engine at all — and
# nothing looks wrong. This process logs "EXITS EXTERNAL ... measured, not
# acted on" every cycle and carries on; its other alerts keep arriving.
#
# The kill switch gets there by a different road: tripping it does not stop
# the monitor, it forces the monitor to DRY RUN for the session. Alive,
# polling, alerting — and unable to place a single closing order. Which is
# why a heartbeat that only said "I am running" would certify exactly the
# state it exists to catch. `bcs/spread_monitor.write_heartbeat` records
# whether the engine can BOOK, not merely whether it breathes.
#
# Four bad states, each needing a different sentence from whoever reads the
# Telegram at 11:00 (`feedback_never_asked_is_not_failed` — "never started"
# and "started and died" are not one condition):
HEARTBEAT_NAME = 'exit_engine_heartbeat.json'      # written by the peer
ALERT_STATE_NAME = 'exit_engine_alert_state.json'  # this file's own dedup
HEARTBEAT_STALE_SEC = 15 * 60      # ~3 missed cron restarts of a 5-min line
HEARTBEAT_REPEAT_SEC = 30 * 60     # re-arm, so a standing fault is not forgotten


def _heartbeat_path():
    """Resolved per call. `cfg.LOG_DIR` is repointed by tests, and the writer
    resolves the SAME filename through its own LOG_DIR — pinned equal by a
    test, because one constant living in two modules is how a fix lands in the
    copy nobody opened."""
    return cfg.LOG_DIR / HEARTBEAT_NAME


def read_exit_engine_heartbeat(now: Optional[float] = None) -> dict:
    """Classify the peer exit engine. Never raises.

    Returns `{'state', 'detail', 'age', 'beat'}` where state is one of:

      ok            polled recently AND armed to place orders
      missing       no file at all — the engine has never run here
      stale         a file, but old: it ran, then stopped
      dry_run       polling, but every close is a no-op
      no_cohort_book  polling and armed, but the cohort store would not open
      unreadable    a file that is not a heartbeat

    `missing` and `stale` are deliberately separate. They look identical from
    here — no fresh beat — and they need opposite responses: one is "start
    it", the other is "find out what killed it, and it was watching positions
    when it died".
    """
    now = time.time() if now is None else now
    path = _heartbeat_path()
    try:
        with open(path) as f:
            beat = json.load(f)
        if not isinstance(beat, dict):
            raise ValueError(f'not an object: {type(beat).__name__}')
        ts = float(beat['ts'])
    except FileNotFoundError:
        return {'state': 'missing', 'detail': f'no {path.name} exists',
                'age': None, 'beat': None}
    except Exception as e:
        # A heartbeat this process cannot parse is NOT evidence of health.
        return {'state': 'unreadable', 'age': None, 'beat': None,
                'detail': f'{path.name} is unreadable ({type(e).__name__}: '
                          f'{str(e)[:80]})'}
    age = now - ts
    when = beat.get('at') or '?'
    if age > HEARTBEAT_STALE_SEC:
        return {'state': 'stale', 'age': age, 'beat': beat,
                'detail': f'last beat {when} ({int(age / 60)} min ago, '
                          f'state={beat.get("state")})'}
    if beat.get('dry_run'):
        why = ('the kill switch tripped this session'
               if beat.get('kill_switch') else '--dry-run on its crontab line')
        return {'state': 'dry_run', 'age': age, 'beat': beat,
                'detail': f'running ({when}) but in DRY RUN — {why}'}
    if not beat.get('cohort_store', True):
        return {'state': 'no_cohort_book', 'age': age, 'beat': beat,
                'detail': f'running ({when}) but it could not open the cohort '
                          f'store, so it loaded none of these positions'}
    return {'state': 'ok', 'age': age, 'beat': beat,
            'detail': f'polling ({when}), armed'}


def _read_alert_state() -> dict:
    """Last thing this alert said, and when. PERSISTED, not in-memory: the
    zebra cron process exits between cycles, so an in-process flag would
    re-alert every five minutes — the same reason the spot-corroboration
    reference is persisted."""
    try:
        with open(cfg.LOG_DIR / ALERT_STATE_NAME) as f:
            prev = json.load(f)
        return prev if isinstance(prev, dict) else {}
    except Exception:
        return {}


def _write_alert_state(state: str, now: float) -> None:
    try:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = cfg.LOG_DIR / ALERT_STATE_NAME
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w') as f:
            json.dump({'state': state, 'alerted_at': now}, f)
        tmp.replace(path)
    except Exception as e:
        # Losing the dedup makes this NOISY, never silent. Right direction.
        logger.warning("could not persist exit-engine alert state: %s", e)


#: Why THIS engine is not the one booking. Both are true statements about a
#: cohort record the monitor alone can close; they differ in WHICH fact put it
#: in sole charge, and the operator needs to know which -- one is a switch
#: they set, the other is arithmetic they cannot change.
WHY_STOOD_DOWN = ('This engine has STOOD DOWN from %d cohort position(s) '
                  '(exits_managed_externally=true)')
WHY_LIVE_RECORD = ('This engine CANNOT book %d LIVE cohort position(s) - it '
                   'books at the structure mid, which is not a price a record '
                   'with real legs could have transacted at')


def _format_exit_engine_alert(hb: dict, n_positions: int, cohort_seen,
                              why: str = WHY_STOOD_DOWN) -> str:
    fix = {
        'missing': 'START IT: the cron line for `bcs.spread_monitor --cron` '
                   'is not running, or it never reached its first poll.',
        'stale': 'FIND OUT WHAT KILLED IT. It was watching these positions '
                 'and stopped; the cron line should have restarted it '
                 'within 5 minutes and did not.',
        'dry_run': 'It watches and alerts but places NO closing order. Either '
                   're-arm it, or set exits_managed_externally=false so this '
                   'engine books the exits again. The two switches move '
                   'TOGETHER.',
        'no_cohort_book': 'Its adapter onto the cohort book failed. Check the '
                          'cohort store, then restart it.',
        'unreadable': 'Treat as DOWN until proven otherwise.',
        'not_watching': 'It is alive and armed but reports 0 cohort positions '
                        'while this engine has %d stood down. One of the two '
                        'is reading the book wrong — the same shape as '
                        '"--list answered Open: 0 with eight positions live".'
                        % n_positions,
    }.get(hb['state'], 'Investigate.')
    return (
        f"\U0001F534 NO EXIT ENGINE\n"
        f"{why % n_positions}, but bcs/spread_monitor.py "
        f"{html.escape(str(hb['detail']))}.\n"
        f"Peer reports {cohort_seen} cohort position(s) loaded.\n"
        f"Nothing is holding their stops right now.\n"
        f"{html.escape(fix)}")


def alert_if_exit_engine_down(n_positions: int, dry_run: bool = False,
                              now: Optional[float] = None,
                              market_open: Optional[bool] = None,
                              why: str = WHY_STOOD_DOWN) -> Optional[str]:
    """Telegram when the engine we stood down for cannot close. Never raises.

    Returns the state alerted on, or None. Called ONCE per cycle from the
    EXITS-EXTERNAL branch — the branch is the trigger because the alert is
    only ever true when this engine has actually handed the stops away.

    Noise discipline: alert on a TRANSITION into a bad state, then re-arm
    every `HEARTBEAT_REPEAT_SEC`. Not every poll — an alert that repeats every
    five minutes is one the reader learns to ignore, which is precisely how
    the OI flag on COCHINSHIP got waved through.

    `market_open` is injectable and not read from the wall clock by default in
    tests: outside market hours the peer exits on purpose and its heartbeat
    goes stale by design, so alerting then would cry wolf every evening
    (`feedback_pin_the_wall_clock_in_tests` — this must pass at 02:00 Sunday).
    """
    now = time.time() if now is None else now
    if market_open is None:
        market_open = _is_market_open()
    hb = read_exit_engine_heartbeat(now=now)
    state = hb['state']
    beat = hb.get('beat') or {}
    cohort_seen = beat.get('cohort_trades', '?')
    if state == 'ok' and isinstance(cohort_seen, int) and cohort_seen == 0 \
            and n_positions > 0:
        # Alive, armed, and watching NONE of them. `--list answered Open: 0
        # with eight positions live` was this exact disagreement, and a
        # heartbeat that only reported liveness would have called it healthy.
        state = 'not_watching'
        hb = dict(hb, state=state,
                  detail=f'{hb["detail"]} but reports 0 cohort positions')
    prev = _read_alert_state()
    if state == 'ok':
        if prev.get('state') not in (None, 'ok'):
            _write_alert_state('ok', now)
            if market_open:
                _send_telegram(
                    f"✅ EXIT ENGINE BACK\nbcs/spread_monitor.py is "
                    f"{html.escape(str(hb['detail']))}. "
                    f"{n_positions} cohort position(s) are covered again.",
                    dry_run=dry_run)
                return 'recovered'
        return None
    logger.error("EXIT ENGINE %s: %s (%d cohort position(s) stood down here)",
                 state.upper(), hb['detail'], n_positions)
    if not market_open:
        # Logged above regardless — the log is the forensic record. Only the
        # Telegram is held, and only outside the session.
        return None
    same = prev.get('state') == state
    try:
        since = now - float(prev.get('alerted_at', 0))
    except (TypeError, ValueError):
        since = HEARTBEAT_REPEAT_SEC + 1
    if same and since < HEARTBEAT_REPEAT_SEC:
        return None
    _send_telegram(_format_exit_engine_alert(hb, n_positions, cohort_seen,
                                             why=why),
                   dry_run=dry_run)
    _write_alert_state(state, now)
    return state


def _exit_cleared(store, trade: dict, kind: str, quote: dict, spot: float,
                  dry_run: bool = False,
                  incycle_wait: Optional[int] = None) -> bool:
    """True if this exit may fire now. False = wait or hold.

    Called BEFORE `set_alert_flag` on every price-driven exit, because that
    flag is consume-once and burning it on an exit that does not execute
    strands the exit permanently.

    TIME exits are deliberately NOT gated: they are calendar-driven rather than
    quote-driven, and their flag re-arms daily, so a bad mid there costs one
    day of paper accounting instead of a stranded position.

    `incycle_wait` is M12 and defaults to "read the config", which is right for
    THIS engine: it looks at the marker once every five minutes, so a verdict
    that lands two minutes from now costs three minutes of a fired stop.
    `bcs/exit_vet.py` passes 0 -- see its docstring for why the same
    optimisation is a pessimisation on a five-second poll.
    """
    gate = vet_mod.exit_gate(store, trade, kind, quote, spot,
                             incycle_wait=incycle_wait)
    if gate == 'proceed':
        return True
    if gate == 'wait':
        logger.info("EXIT %s #%d held pending vet", kind, trade['id'])
        return False
    # 'hold' — deferred to the cap. This is the one case that needs a human:
    # the structure's loss is capped and known, but firing on a book we could
    # not verify is the unbounded-damage direction.
    flag = f'exit_escalate_{kind}'
    if store.set_alert_flag_daily(trade['id'], flag):
        # Re-read: _mutate has detached the caller's dict from the store, so the
        # defer count on `trade` can lag the write that just pushed us to hold.
        fresh = store.find(trade['id']) or trade
        if _send_telegram(_format_exit_escalation(fresh, kind, quote, spot),
                          dry_run=dry_run):
            logger.warning("EXIT %s #%d ESCALATED to user after %d defers",
                           kind, trade['id'], vet_mod.exit_defers(fresh, kind))
        else:
            # Give the day's flag back. This is the ONE message with a human in
            # the loop, on a position we are holding precisely because nothing
            # automated can verify it — losing it silently for a day is worse
            # than a duplicate nag.
            store.clear_alert_flag(trade['id'], flag)
            logger.error("EXIT %s #%d escalation FAILED to send — flag "
                         "released, retrying next cycle", kind, trade['id'])
    return False


def _format_exit_escalation(trade: dict, kind: str, quote: dict,
                            spot: float) -> str:
    """Ask the human. Nothing else will act on this signal until they do."""
    mid = quote.get('mid')
    return (
        f"🟡 <b>EXIT NEEDS YOU</b>  <code>{trade.get('stock')}</code> "
        f"({trade.get('direction')})\n"
        f"{html.escape(kind.upper())} triggered but Claude could not verify the "
        f"quote after {vet_mod.exit_defers(trade, kind)} re-checks.\n"
        f"spot {spot:,.2f} | structure mid "
        f"{('%.2f' % mid) if mid is not None else 'NA'} | entry debit "
        f"{trade.get('debit')}\n"
        f"reason: {html.escape(str(quote.get('reason') or 'unreliable book'))}\n"
        # SAY THAT THE HOLD IS BOUNDED. This read "HOLDING" with no
        # qualifier, which an owner reasonably takes as "the engine is
        # waiting for me" -- and since 2026-08-29 it waits only
        # `exit_vet_max_hold_sec`, then exits on the guards. Someone who
        # started closing by hand on the strength of that word was racing the
        # engine's own budget without knowing it.
        f"<i>HOLDING for now — max loss is the debit and already capped. "
        f"The engine proceeds on its deterministic guards in up to "
        f"{max(0, int(getattr(cfg, 'EXIT_VET_MAX_HOLD_SEC', 0)) // 60)} min "
        f"unless you close first. Close manually with "
        f"<code>zebra close {trade.get('id')}</code> if you disagree.</i>"
    )


def _required_capital(kite, bcs: dict, quantity: int) -> tuple:
    """What the exchange will actually block for this spread, and how we know.

    Prefers `basket_order_margins`: a BCS is HEDGED, and the exchange prices the
    pair as one position, so a leg-by-leg estimate is meaningfully wrong. Falls
    back to the net debit — the cash cost, and the true floor for a debit spread
    — when the API is unavailable.

    Returns (rupees, basis) where basis is 'exchange' or 'debit'.
    """
    basket = [
        {'exchange': 'NFO', 'tradingsymbol': bcs['long_symbol'],
         'transaction_type': 'BUY', 'variety': 'regular', 'product': 'NRML',
         'order_type': 'LIMIT', 'quantity': quantity,
         'price': bcs.get('long_ask') or 0},
        {'exchange': 'NFO', 'tradingsymbol': bcs['short_symbol'],
         'transaction_type': 'SELL', 'variety': 'regular', 'product': 'NRML',
         'order_type': 'LIMIT', 'quantity': quantity,
         'price': bcs.get('short_bid') or 0},
    ]
    try:
        om = kite.basket_order_margins(basket)
        final = (om or {}).get('final') or {}
        total = final.get('total')
        # `is not None`, not truthiness: a legitimate 0.0 (fully hedged, no
        # margin blocked) would otherwise be read as "API unavailable" and
        # silently relabelled `net debit`, so the ticket would quote a
        # different basis than it claims.
        if total is not None:
            return float(total), 'exchange'
        logger.warning('basket_order_margins returned no final.total (%r) — '
                       'falling back to the net debit', om)
    except Exception as e:
        logger.warning('basket_order_margins failed (%s) — falling back to the '
                       'net debit', e)
    return float(bcs['debit']) * quantity, 'debit'


def _funds_line(kite, bcs: dict, quantity: int) -> str:
    """Funds check for the ENTER ticket. LIVE MODE ONLY.

    In paper mode there is no account to check and no order to fund, so this
    costs nothing and says nothing — not even an API call.

    In live mode the ENTER alert IS the order ticket the owner acts on, so a
    ticket the account cannot fund is worse than no ticket: it invites a
    rejected order at the one moment attention is scarce.

    Deliberate asymmetry on failure. A definite shortfall SHOUTS; an inability
    to check merely warns. Blocking a real signal because a margin endpoint
    hiccuped would cost an opportunity to protect against a maybe, and in live
    mode a human places the order anyway — Kite will refuse it if the money is
    genuinely not there.
    """
    if cfg.PAPER_MODE:
        return ""
    # Distinct causes, distinct messages — "no broker session" was printed for
    # a missing spread too, which points the reader at Kite when the problem is
    # the structure. A zero quantity would make every figure below meaningless
    # (need = debit * 0 = 0, reported as "Funds OK — need Rs 0"), so it is
    # refused rather than answered.
    if not kite:
        return "\n\n⚠ <i>Funds not checked — no broker session.</i>"
    if not bcs:
        return "\n\n⚠ <i>Funds not checked — no spread to price.</i>"
    if not quantity or quantity <= 0:
        return ("\n\n⚠ <i>Funds not checked — lot size unknown, so the "
                "requirement cannot be computed.</i>")
    try:
        need, basis = _required_capital(kite, bcs, quantity)
        avail = float(kite.margins('equity')['available']['live_balance'])
    except Exception as e:
        logger.error('FUNDS CHECK FAILED: %s', e)
        return ("\n\n⚠ <i>Could not verify funds "
                f"({html.escape(str(e)[:60])}) — check before placing.</i>")

    tag = 'exchange margin' if basis == 'exchange' else 'net debit'
    if avail >= need:
        logger.info('FUNDS OK: need %.0f (%s), available %.0f', need, basis, avail)
        return (f"\n\n💰 <i>Funds OK — need Rs {need:,.0f} ({tag}), "
                f"have Rs {avail:,.0f}.</i>")

    short = need - avail
    logger.warning('INSUFFICIENT FUNDS: need %.0f (%s), available %.0f, '
                   'short %.0f', need, basis, avail, short)
    return (f"\n\n🛑 <b>INSUFFICIENT FUNDS — short Rs {short:,.0f}</b>\n"
            f"<i>Need Rs {need:,.0f} ({tag}), have Rs {avail:,.0f}. "
            f"Do not place this without adding funds.</i>")


def _vet_line(trade: dict) -> str:
    """One line describing the vetting verdict, appended to an ENTER alert.

    Allows deliberately do NOT get their own Telegram message — one signal, one
    alert. The verdict rides on the ticket that was already going to be sent.
    An UNVETTED entry says so explicitly: silence there would read as "Claude
    approved this", which is exactly the wrong impression.
    """
    state = vet_mod.vet_state(trade)
    if not state:
        return ""
    v = trade.get('vet') or {}
    if state == vet_mod.UNAVAILABLE:
        # WHY it failed open, not just THAT it did. "did not answer in time"
        # was printed for every cause, including the one where no agent was
        # ever started — the owner reasonably read it as "Claude looked and was
        # slow" when the truth was "Claude was never asked". They call for
        # different fixes (raise the budget vs investigate the CLI), so an
        # alert that cannot tell them apart sends the wrong one.
        why = str((trade.get('vet') or {}).get('failed_open_because') or '')
        if 'budget' in why.lower():
            return ("\n\n⚠ <i>Entered UNVETTED — no agent slot free, so Claude "
                    "was never asked.</i>")
        if why:
            return ("\n\n⚠ <i>Entered UNVETTED — vetting could not run "
                    f"({html.escape(why)}).</i>")
        return "\n\n⚠ <i>Entered UNVETTED — Claude did not answer in time.</i>"
    if state == vet_mod.ALLOWED:
        rid = v.get('decision_id')
        return f"\n\n✅ <i>Vetted by Claude (decision #{rid}).</i>" if rid \
            else "\n\n✅ <i>Vetted by Claude.</i>"
    return ""


def format_vetoed_alert(trade: dict, reasons: list, red_flags: list) -> str:
    """A veto DOES get its own message — nothing else will be sent for this
    signal, and silence would be indistinguishable from 'nothing fired'."""
    lines = [f"🛑 <b>VETOED</b>  <code>{trade.get('stock')}</code> "
             f"({trade.get('direction')})"]
    for r in (red_flags or [])[:4]:
        lines.append(f"⚠ {html.escape(str(r))}")
    for r in (reasons or [])[:4]:
        lines.append(f"• {html.escape(str(r))}")
    lines.append("<i>No entry taken.</i>")
    return "\n".join(lines)


def _entry_candidate(trade: dict, analysis: dict,
                     bcs: Optional[dict] = None) -> tuple:
    """(candidate, depth) for `zebra.capital` — the structure ACTUALLY opened.

    WAAREEENER #449, 2026-08-27. The capital layer priced
    `analysis['best']`: the ZEBRA BACK-RATIO pair, a structure retired on
    2026-08-12 that nothing has opened since. One Telegram consequently
    quoted the same position twice, at two prices that were both computed
    correctly from two different trades —

        ticket line  "Capital (1 lot) = 6,361"   = BCS debit 36.35 x 175
        sizing line  "one position at Rs 121700" = (2 x long_mid - short_mid)
                                                   on the 3000/2600 PE back
                                                   ratio, 695.43 x 175

    — refused the signal on the per-trade cap using the second, and spent a
    Claude vet arguing with it (decision #92 spotted the discrepancy itself).

    So the candidate is sourced from the BCS dict whenever the BCS pipeline is
    what runs. The back-ratio `best` it used to fall back on is RETIRED
    (2026-08-27) and no longer exists. With no BCS to price, `debit` is
    None and
    `capital.check` fails closed on an unknown candidate — the honest answer,
    and never a number belonging to some other spread.
    """
    # Always the BCS. The `analysis['best']` fallback that used to sit here
    # priced the RETIRED back ratio (decommissioned 2026-08-27) and is gone:
    # `analyze()` no longer produces a `best` at all.
    src = bcs or {}
    candidate = {
        'stock': trade.get('stock'),
        'debit': src.get('debit'),
        'lot_size': src.get('lot_size') or analysis.get('lot_size'),
    }
    # `{}`, NOT None. `capital.plan` distinguishes them deliberately: None
    # means "this caller never had depth to give" and carries NO liquidity
    # bound, while an empty mapping means "I looked and the book said
    # nothing" and caps the size at one lot. This call site ALWAYS looks, so
    # a missing `long_ask_qty` is the second case -- and Kite ships that key
    # absent, which is the state that held on all 13 cohort records until the
    # `_atm_quote` fix. Passing None sized those entries with no liquidity
    # check at all; masked only because `lots_for_capital(2L)` is 1.
    depth = {}
    if src.get('long_ask_qty') is not None:
        depth = {'long': {'ask_qty': src.get('long_ask_qty')},
                 'short': {'bid_qty': src.get('short_bid_qty')}}
    return candidate, depth


def _capital_context(store, trade: dict, analysis: dict,
                     bcs: Optional[dict] = None) -> dict:
    """What the book can afford, and what it already holds.

    Both halves matter and they answer different questions. `deployed` /
    `open_positions` is the PORTFOLIO question -- is this one more position
    than the book should carry. `plan` is the POSITION question -- how many
    lots this signal may take, and which limit decided it.

    `plan.bounds` carries every limit's own answer, not just the winner, so the
    agent can tell a size bound by LIQUIDITY (a thin book, which is a reason to
    be suspicious of this signal) from one bound by BUDGET (a full book, which
    says nothing about this signal's quality).

    `bcs` is the pair the ticket describes and the store records. It is not
    optional in spirit — see `_entry_candidate` for what pricing the wrong
    structure cost on 2026-08-27 — and `priced` is echoed back so a stored
    plan can be checked afterwards against the position it was meant to size.
    """
    candidate, depth = _entry_candidate(trade, analysis, bcs)
    # WHOLE BOOK: capital counts what is HOLDING, and a rupee committed by
    # the retired engine would still be committed. No legacy record is open
    # today, so this is identical either way — but a budget that could be
    # widened by re-scoping is not a budget.
    book = store.load_trades()
    lim = capital.limits(book)
    held, n_open, unpriced = capital.deployed(book)
    return {
        'capital_rupees': lim.capital,
        'capital_basis': lim.basis,
        'deployed_rupees': round(held, 2),
        'headroom_rupees': (round(lim.max_deployed - held, 2)
                            if lim.max_deployed is not None else None),
        'open_positions': n_open,
        'open_slots': (lim.max_open - n_open) if lim.max_open else None,
        'unpriced_positions': unpriced,
        'limits': capital.describe(book),
        # WHICH structure the plan below priced. Without it a wrong number is
        # indistinguishable from a right one at a glance, which is exactly how
        # #449 shipped.
        'priced': {'structure': cfg.ENTRY_STRUCTURE,
                   'debit': candidate.get('debit'),
                   'lot_size': candidate.get('lot_size')},
        'plan': capital.plan(book, candidate, depth, lim),
        'note': 'entry is SLICED into one-lot orders (no per-order brokerage '
                'on Neo) and each fill is verified before the next goes out',
    }


def _vet_context(store, trade: dict, analysis: dict, gap_pct: float,
                 kite=None, bcs: Optional[dict] = None) -> dict:
    """The evidence bundle handed to the vetting agent.

    Snapshotted here rather than re-quoted by the agent so its verdict judges
    exactly the book the bot acted on. Live re-quoting is a step INSIDE the
    agent's checklist, not part of the handoff.
    """
    # Both of these read candles. They are wrapped because a statistics or
    # chart failure must never stop a signal being vetted — a missing section
    # is a gap in the evidence, an exception here is a halted pipeline.
    try:
        attraction = history.attraction(kite, trade['stock'],
                                        trade.get('timeframe'),
                                        trade.get('direction'),
                                        dte=analysis.get('dte'))
    except Exception as e:
        logger.warning("attraction lookup failed for %s: %s", trade['stock'], e)
        attraction = None
    try:
        swing = history.swing_tp(kite, trade['stock'], trade.get('timeframe'),
                                 trade.get('direction'), analysis.get('spot'),
                                 trade.get('st_value'))
    except Exception as e:
        logger.warning("swing lookup failed for %s: %s", trade['stock'], e)
        swing = None
    # Under the BCS-only pipeline the zebra pair is never traded, so a context
    # describing only that pair would ask the agent to vet a position that
    # will not exist — and `gates_all_passed` would read TRUE off an empty
    # `best`, i.e. an all-clear derived from having nothing to check.
    # ── Capital, in the handoff (owner, 2026-08-26: "vet should carefully
    # handle capital allocation to trade as well ... keep an eye on overall
    # capital + per-trade capital").
    #
    # The agent was being asked to approve an ENTRY while shown nothing about
    # what the book could afford or what it already held. Same defect the
    # liquidity fields were fixed for one paragraph down: judge what you
    # cannot see. Wrapped, because a capital lookup failing must degrade the
    # evidence, never halt the pipeline -- and `None` reads as "not supplied",
    # which the agent's checklist can act on, where a zero would read as "no
    # money at risk".
    cap_ctx = None
    try:
        cap_ctx = _capital_context(store, trade, analysis, bcs)
    except Exception as e:
        logger.warning("capital context failed for %s: %s", trade['stock'], e)

    atm = analysis.get('atm_quote') or {}
    bcs_ctx = {
        'atm_strike': analysis.get('atm_strike'),
        'atm_bid': atm.get('bid'), 'atm_ask': atm.get('ask'),
        'atm_mid': atm.get('mid'), 'atm_oi': atm.get('oi'),
        'target_spot': trade.get('st_value'),
        'max_debit_to_width_pct': cfg.BCS_MAX_DEBIT_TO_WIDTH_PCT,
        'min_leg_oi': cfg.MIN_LEG_OI,
        # The short leg is picked, quoted and gated INSIDE analyze_bcs,
        # after this snapshot is taken. Say so rather than leave the agent
        # to assume the omission means "nothing there".
        'note': 'short leg is chosen and gated at entry; if this signal '
                'entered, both OI and debit/width gates passed',
    }
    if bcs:
        # The pair itself, once it has been built — the same dict the
        # ticket renders and `mark_entered_bcs` stores. The agent used to
        # be handed the ATM book and told the rest was decided later, so
        # it re-quoted the spread itself to argue with a capital figure
        # that belonged to a different structure (#449).
        bcs_ctx.update({
            k: bcs.get(k) for k in
            ('long_strike', 'short_strike', 'long_symbol', 'short_symbol',
             'width', 'debit', 'debit_mid', 'debit_to_width_pct',
             'debit_to_width_pct_mid', 'entry_cost', 'entry_cost_pct',
             'max_profit_per_share', 'long_bid', 'long_ask', 'short_bid',
             'short_ask', 'long_oi', 'short_oi', 'short_spread_pct',
             'long_ask_qty', 'short_bid_qty', 'lot_size',
             'pricing_basis', 'warnings')})
        bcs_ctx['note'] = ('this is the pair that would open, priced on '
                           'the fill basis: ask(long) - bid(short)')
    return {
        'structure': cfg.ENTRY_STRUCTURE,
        'bcs': bcs_ctx,
        'capital': cap_ctx,
        'stock': trade['stock'],
        'direction': trade['direction'],
        'timeframe': trade.get('timeframe'),
        'spot': analysis.get('spot'),
        'st_value': trade.get('st_value'),
        'st_direction': trade.get('st_direction'),
        'gap_pct': round(gap_pct, 2),
        'expiry': analysis.get('expiry'),
        'dte': analysis.get('dte'),
        'lot_size': analysis.get('lot_size'),
        # Liquidity evidence travels WITH the handoff. VETTING.md tells the
        # agent to judge "depth at touch and the spread as a % of mid" — the
        # analyzer measures exactly that, and this bundle used to drop it, so
        # the agent was asked to judge the one thing it could not see. That is
        # the failure mode (a book you cannot exit) the same doc calls the one
        # that has actually cost this book money.
        #
        # The RETIRED back-ratio block that used to sit here is GONE
        # (decommissioned 2026-08-27). It shipped `liquidity_ok`, `gate_fails`
        # and per-leg spreads belonging to a pair NOBODY OPENS, and decision
        # #92 (WAAREEENER #449) read them as facts about the trade it was
        # vetting and had to work out unaided that they were not. A caption
        # saying "ignore this" is not as good as not sending it. Judge `bcs`.
        #
        # `gates_all_passed` went with it: it was `not best['gate_fails']`,
        # which computed to True off an empty `best` — an all-clear derived
        # from having nothing to check.
        # ── does this symbol actually GET pulled to its ST line? ─────────
        # The magnet IS the thesis, and every signal was vetted as though the
        # pull were a property of the setup rather than of the symbol. Some
        # symbols oscillate around ST; some trend away from it for months and
        # never come back inside an option's life. This is that symbol's own
        # record on its own timeframe. `sample: 'thin'` means the rate is real
        # but built on too few episodes to lean on — say so, do not round it
        # into a confident number.
        'st_attraction': attraction,
        # A swing level standing between spot and the magnet. When present the
        # TP is booked HERE instead of at the ST line, so the agent is judging
        # the target the trade will actually use.
        'swing_tp': swing,
    }


def _band_cancel(store: ZebraStore, trade: dict, reason: str,
                 dry_run: bool = False) -> None:
    """Retire a signal that has left its entry band — safely in LIVE.

    In PAPER an unentered signal is worth nothing, so drift/crossed/stale just
    cancel the row and the slot is reclaimed.

    LIVE is different in one decisive way: the ENTER alert is an ORDER TICKET,
    and once it has been delivered the owner may ALREADY hold the real
    position. The band checks run on `triggered` rows too, so the plain cancel
    fired on signals that had a ticket out — and `mark_entered` refuses a
    cancelled row, so `zebra enter` could no longer record the fill. The result
    was a real, funded position with NO record: no TP alert, no debit-SL alert,
    no expiry nag, in the mode where those alerts are the only exit mechanism.

    Worse, the likeliest trigger is `crossed` — gap going negative means price
    reached the magnet, i.e. the trade WON. The failure preferentially struck
    winners.

    So in LIVE, a ticketed signal is never silently cancelled. It is announced
    once and left `triggered` for the human to resolve with `zebra enter` (I
    took it) or `zebra cancel` (I did not). `_expire_if_ancient` still bounds
    the row, so nothing becomes immortal.
    """
    ticketed = bool((trade.get('vet_enter_alerted_at')))
    if not cfg.PAPER_MODE and ticketed:
        if store.set_alert_flag_daily(trade['id'], 'band_exit'):
            msg = (f"⚠ <b>SIGNAL LEFT THE BAND</b>  "
                   f"<code>{html.escape(str(trade.get('stock')))}</code> "
                   f"({html.escape(str(trade.get('direction')))})\n"
                   f"{html.escape(reason)}\n"
                   f"<i>An order ticket was already sent for this signal. If "
                   f"you took the trade, record it now with "
                   f"<code>zebra enter {trade['id']} ...</code> — it is NOT "
                   f"being monitored until you do. If you did not, close it "
                   f"out with <code>zebra cancel {trade['id']}</code>.</i>")
            if not _send_telegram(msg, dry_run=dry_run):
                store.clear_alert_flag(trade['id'], 'band_exit')
                logger.error("BAND EXIT alert FAILED for #%d %s — flag "
                             "released, retrying next cycle",
                             trade['id'], trade.get('stock'))
        logger.warning("BAND EXIT #%d %s: %s — ticket already sent, NOT "
                       "cancelling; awaiting `zebra enter` or `zebra cancel`",
                       trade['id'], trade.get('stock'), reason)
        return
    try:
        store.cancel(trade['id'], reason)
    except ValueError:
        pass


def _claim_exit_alert(store: ZebraStore, trade: dict, kind: str) -> bool:
    """Claim the consume-once exit alert. Daily in LIVE, once-ever in PAPER.

    In PAPER the position is booked in the SAME cycle the alert fires, so one
    claim per position is exactly right — a second alert would describe a trade
    that is already closed.

    LIVE changed what the alert IS without changing how long the claim lives.
    There `_paper_auto_close` returns at its first line, so nothing ever books
    the exit and the position stays `entered`; the Telegram is not a
    notification about an exit, it is the ONLY instruction that an exit should
    happen. A one-time-EVER claim therefore means a breached stop is announced
    once and then never mentioned again while the position stays open and keeps
    losing — the owner has to be looking at the phone in the minute it fires or
    the capped loss quietly becomes the maximum loss. That is the same shape as
    both real-money incidents: protection that looks armed and is not.

    So in LIVE the claim re-arms daily, reusing the machinery the expiry nag
    has always used. Daily, not per-cycle: re-alerting every 5 minutes is the
    alert fatigue the gates exist to cure (owner's call, 2026-08-10), and a
    stop that keeps asking once a day until the position is actually closed is
    the behaviour a human can act on.

    THE RECORD DECIDES, NOT THE MODE (fixed 2026-08-31). This keyed on
    `cfg.PAPER_MODE`, but the two arguments above are both about what happens
    to THIS record: whether `_paper_auto_close` will book it in the same cycle,
    which it decides per record via `is_paper_record`. The arming order's very
    first live-money action is a hand-placed live trade filed with `zebra
    enter` while the store is still `paper_mode: true` — and that record got a
    once-EVER claim for a close zebra declines forever. Its stop was announced
    exactly once and then never again, which is precisely the shape the LIVE
    branch was written to prevent. Same `or`-versus-record defect that
    `_paper_auto_close` was fixed for on 2026-08-29, one layer up.
    """
    if is_paper_record(trade):
        return store.set_alert_flag(trade['id'], kind)
    return store.set_alert_flag_daily(trade['id'], kind)


#: The price-driven exits whose claim is consume-once. TIME is deliberately
#: absent: its flag is daily and its close is retried every cycle.
_PRICE_EXIT_KINDS = ('tp', 'trail', 'spot_sl', 'debit_sl')


def _release_stranded_claims(store: ZebraStore, trade: dict) -> None:
    """Give back a consume-once exit claim that no process is still holding.

    THE GAP (found 2026-08-31). `_claim_exit_alert` persists the claim, and the
    booking happens AFTER it — with a Telegram POST of up to 10 seconds in
    between. Every IN-PROCESS failure between the two already releases the flag
    (`_paper_auto_close`'s defer paths, `_send_exit_alert`'s failed send). What
    nothing covered is the process simply CEASING between them: a SIGKILL, an
    OOM, power loss, or the 15:30 window closing mid-cycle. zebra's cron
    process is one-shot and exits between cycles, so there is no in-memory
    state to notice — the claim is durable and the release was not.

    The consequence is worst exactly where it matters most. For `debit_sl` the
    position's only loss-side stop is silently disarmed FOR THE LIFE OF THE
    TRADE: every later cycle's `set_alert_flag` returns False and
    short-circuits the branch, and the position rides to max loss or expiry
    with nothing logged. That is "protection that looks armed and is not",
    which is the shape of both real-money incidents.

    The inference is sound rather than heuristic: a claim can only have been
    made by a cycle that was about to book, a booking leaves the record
    `exited`, and every in-process failure releases. So `entered` + a claim
    older than a couple of cycles is, by construction, a claim whose holder is
    gone. Two intervals of slack so a cycle that is merely SLOW is never robbed
    of its claim mid-flight.

    PAPER RECORDS ONLY. A live record's claim is daily, not once-ever, so it
    re-arms tomorrow on its own — releasing it here would convert a deliberate
    once-a-day nag into one every ten minutes, which is the alert fatigue the
    daily throttle exists to cure.

    Never raises: this is housekeeping, and it must not be able to stop the
    exit checks that follow it.
    """
    if not is_paper_record(trade):
        return
    slack = max(2 * cfg.MONITOR_INTERVAL_SEC, 600)
    now = datetime.now()
    for kind in _PRICE_EXIT_KINDS:
        stamp = trade.get('%s_alerted_at' % kind)
        if not stamp:
            continue
        try:
            age = (now - datetime.fromisoformat(str(stamp))).total_seconds()
        except (TypeError, ValueError):
            continue                     # unparseable: leave it alone, say so
        if age < slack:
            continue
        try:
            store.clear_alert_flag(trade['id'], kind)
            logger.warning(
                "STRANDED CLAIM #%d %s: the %s exit was claimed %.0fs ago and "
                "the position is still `entered` — the cycle that claimed it "
                "died before booking. Releasing so the trigger can fire again.",
                trade['id'], trade.get('stock'), kind.upper(), age)
        except Exception as e:
            logger.error("could not release the stranded %s claim on #%d: %s",
                         kind, trade['id'], e)


def _send_exit_alert(store: ZebraStore, trade: dict, kind: str, msg: str,
                     dry_run: bool = False) -> None:
    """Send an exit alert, giving the consume-once claim back if the send fails.

    The caller has ALREADY claimed `kind` via set_alert_flag, which is
    one-time-EVER (not daily like TIME). So an unreleased claim silences that
    exit for the life of the position: every later cycle's set_alert_flag
    returns False and short-circuits the whole branch.

    In LIVE the alert IS the exit mechanism — there is no auto-close — so one
    transient Telegram failure would strand a position with its stop already
    "fired" and nobody told. Same discipline as _send_enter_alert, the exit
    escalation and the review alert; the exit branches were the one family that
    never got it.
    """
    if not _alerts_enabled(trade):
        return
    if _send_telegram(msg, dry_run=dry_run):
        return
    logger.error("%s alert FAILED for #%d %s — releasing the claim to retry "
                 "next cycle", kind.upper(), trade['id'], trade.get('stock'))
    store.clear_alert_flag(trade['id'], kind)


def _send_enter_alert(store: ZebraStore, trade: dict, msg: str, stock: str,
                      dry_run: bool = False) -> None:
    """Send the ENTER alert exactly once, whatever structure produced it.

    LIVE + vet: the alert is the user's ORDER TICKET. It fires on the verdict
    tick rather than the trigger tick, and the signal stays 'triggered'
    afterwards (the user enters manually), so status alone cannot dedupe it.
    Atomic test-and-set: overlapping crons cannot send the ticket twice, and
    the flag is consumed only here, on the path that actually reaches the
    alert — never before a step that can fail.

    Shared rather than reimplemented per structure. The BCS-only path first
    sent its own alert directly, which skipped this claim entirely: in LIVE
    mode that is an order ticket re-sent every five minutes, forever, which is
    how duplicate manual entries happen.
    """
    # The claim is a property of LIVE, not of vetting. With vetting off the
    # dedup used to rest entirely on a `continue` in the caller, so the ticket
    # was neither claimed nor releasable — and a single failed Telegram lost
    # the only order instruction that signal would ever produce, with the
    # caller's `continue` then blocking every retry. Claim in LIVE regardless;
    # PAPER still never claims (the position is already open and the alert is
    # a notification, not a ticket).
    if not cfg.PAPER_MODE \
            and not store.set_alert_flag(trade['id'], 'vet_enter'):
        logger.info("Deferred ENTER alert already sent for #%d %s",
                    trade['id'], stock)
        return
    if _send_telegram(msg, dry_run=dry_run):
        logger.info("ENTER alert sent for #%d %s", trade['id'], stock)
        return
    logger.warning("ENTER alert FAILED for #%d %s", trade['id'], stock)
    # LIVE: that ticket was the ONLY notification this allowed signal will ever
    # produce, and the fast-path skips the trade once the flag is set — so a
    # single Telegram hiccup would silently lose a vetted entry until it
    # drift-cancelled. Give the claim back and retry next cycle, the same
    # discipline as the exit escalation and the review alert. (PAPER never
    # claims the flag; the position is already open and the alert is a
    # notification, not a ticket.)
    if not cfg.PAPER_MODE:
        store.clear_alert_flag(trade['id'], 'vet_enter')
        logger.error("Deferred ENTER ticket for #%d %s released — "
                     "retrying next cycle", trade['id'], stock)


def _format_capital_refused_alert(trade: dict, bcs: dict, plan: dict) -> str:
    """What a signal the book cannot fund is allowed to look like.

    NOT an order ticket. No click-copy symbols, no BUY/SELL lines, no debit to
    fill against, no vetting tick — everything on #449's message that told the
    reader to place a trade the same message then forbade.

    It still names the pair, because "WAAREEENER PE was refused" with no
    strikes is not something anyone can check afterwards, and it still says
    which limit bound, because a full book and a too-rich position call for
    completely different responses.
    """
    bcs = bcs or {}
    plan = plan or {}
    per_lot = 0.0
    try:
        per_lot = float(bcs.get('debit') or 0) * float(bcs.get('lot_size') or 0)
    except (TypeError, ValueError):
        pass
    pair = ''
    try:
        pair = (f"{float(bcs['long_strike']):g}/{float(bcs['short_strike']):g} "
                f"debit {float(bcs['debit']):g} x {int(bcs['lot_size'])} "
                f"= Rs {per_lot:,.0f}/lot\n")
    except (KeyError, TypeError, ValueError):
        # A malformed pair must not cost the operator the refusal itself.
        pair = ''
    paper = ('\n<i>Paper recorded it anyway — the validation book keeps every '
             'signal, funded or not.</i>' if cfg.PAPER_MODE else '')
    return (
        f"🚫 <b>NO ENTRY — capital</b>  "
        f"{html.escape(str(trade.get('stock')))} "
        f"({html.escape(str(trade.get('direction')))})\n"
        f"{pair}"
        f"{html.escape(str(plan.get('reason') or 'refused by the capital gate'))}"
        f"{paper}")


def _send_capital_refused_alert(store: ZebraStore, trade: dict, bcs: dict,
                                plan: dict, stock: str,
                                dry_run: bool = False) -> None:
    """Tell the operator once, without eating the order ticket's claim.

    Its own flag, deliberately. `vet_enter` is the LIVE ticket's consume-once
    claim, and spending it here would mean that when a slot frees an hour
    later the real ticket is silently suppressed as "already sent" — turning a
    temporary refusal into a permanently lost entry.

    Once ever in PAPER (the position is booked in this same cycle and will
    never be refused again); once a DAY in LIVE, where the signal keeps
    re-evaluating and a book that is full at 10:00 may not be at 14:00.
    """
    try:
        if not _refused_alert_claimed(store, trade['id']):
            return
        msg = _format_capital_refused_alert(trade, bcs, plan)
    except Exception as e:
        # Never raise into the cycle: this runs inside the per-trade loop and
        # an exception here would cost every signal after it its own checks.
        logger.error("CAPITAL REFUSED alert could not be built for #%s %s: %s",
                     trade.get('id'), stock, e)
        return
    if _send_telegram(msg, dry_run=dry_run):
        logger.info("CAPITAL REFUSED alert sent for #%d %s", trade['id'], stock)
        return
    logger.warning("CAPITAL REFUSED alert FAILED for #%d %s — claim released",
                   trade['id'], stock)
    store.clear_alert_flag(trade['id'], 'capital_refused')


def _refused_alert_claimed(store: ZebraStore, trade_id: int) -> bool:
    """Consume-once in PAPER, once-a-day in LIVE. Same rule as the exit nag."""
    if cfg.PAPER_MODE:
        return store.set_alert_flag(trade_id, 'capital_refused')
    return store.set_alert_flag_daily(trade_id, 'capital_refused')


def _as_float(v):
    """A number, or None. The debit cap must never be built from a string."""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _entry_already_in_flight(store: ZebraStore, trade: dict):
    """Why this signal must NOT be sent to the order path, or None.

    THE HOLE THIS CLOSES (found 2026-08-31). Every failure branch in
    `_auto_enter_bcs` records an entry residue, and NOTHING read one back
    before placing again. So a failure after the fills -- `mark_entered_bcs`
    raising, or the debit coming back unpriceable -- left the signal at
    `triggered` with its vet verdict still ALLOWED, and the next five-minute
    cycle placed ANOTHER FULL SPREAD. One per cycle until the order cutoff or
    the kill switch, none of them visible to `capital.check`, because none of
    them was ever recorded as a position.

    The same class covers a hard crash between the broker fill and the store
    write: the order journal holds an intent with no result, and until now
    nothing on the zebra entry path consulted it.

    Two independent sources are checked, deliberately -- the failure being
    guarded against is a STORE that would not write, so a guard reading only
    the store would be blind in exactly the case it exists for:

      * the record's own `entry_residue`, when the store did accept it;
      * the order journal, which is written to disk BEFORE the broker call and
        is therefore the only witness that survives the store failing.

    Fails CLOSED: if neither source can be read, the entry is refused. An
    entry not placed costs one signal; a duplicate costs a naked position.
    """
    try:
        from bcs.spread_monitor import ENTRY_RESIDUE
        residue = (trade.get(ENTRY_RESIDUE.field) or {})
        if residue.get('state') == 'open':
            return ('an entry residue is still OPEN on this signal (%s) -- '
                    'legs from an earlier attempt may be at the broker'
                    % str(residue.get('why'))[:120])
    except Exception as e:
        return 'the entry-residue field could not be read (%s)' % e

    try:
        from bcs import order_journal
        unresolved = order_journal.unresolved_for_trade(trade['id'])
        if unresolved:
            return ('%d order intent(s) for this signal have no recorded '
                    'result -- an order may be live at the broker (first: %s)'
                    % (len(unresolved), unresolved[0].get('symbol')))
    except Exception as e:
        return 'the order journal could not be read (%s)' % e
    return None


def _auto_enter_bcs(store: ZebraStore, kite, trade: dict, bcs: dict,
                    dry_run: bool = False):
    """LIVE auto-entry. Returns the fresh record, or None to fall back to the
    ticket.

    Order of operations, and every step is a refusal point:

    1. **Is auto-entry armed at all?** Off by default; fails closed. Off means
       the owner gets the ticket exactly as before.
    2. **What does capital allow?** `capital.plan` against the live book and
       the touch depth. Zero lots is a refusal, not a smaller trade.
    3. **Place it**, long-first, one lot per order, stopping on any failure.
    4. **Record what FILLED**, never what was asked for, at the debit PAID --
       every stop and the trail derive from it.
    5. **Verify against the broker**, because the code that placed the orders
       is exactly the code that cannot be trusted to say what it placed.

    Never raises. A failure here must cost the entry and nothing else: the
    caller still sends the ticket, so the worst case is the behaviour LIVE had
    before auto-entry existed.
    """
    from bcs import entry_executor as ee

    allowed, why = _entries_allowed_or_log(trade)
    if not allowed:
        return None

    # BEFORE the book, before capital, before any quote: has this signal
    # already been to the order path? Placed first in the sequence because
    # every step below costs a Kite call, and because the answer does not
    # depend on any of them.
    in_flight = _entry_already_in_flight(store, trade)
    if in_flight:
        logger.error(
            "AUTO-ENTRY #%d %s REFUSED: %s. NOT placing again -- resolve the "
            "outstanding legs first.", trade['id'], trade['stock'], in_flight)
        if store.set_alert_flag_daily(trade['id'], 'entry_in_flight'):
            _send_telegram(
                "\U0001F6A8 BCS %s: auto-entry REFUSED because %s.\n"
                "No new order was placed. Check Kite, resolve the residue, "
                "then re-arm this signal by hand."
                % (html.escape(str(trade['stock'])), html.escape(in_flight)),
                dry_run=dry_run)
        return None

    # WHOLE BOOK: capital counts what is HOLDING, and a rupee committed by
    # the retired engine would still be committed. No legacy record is open
    # today, so this is identical either way — but a budget that could be
    # widened by re-scoping is not a budget.
    book = store.load_trades()
    lim = capital.limits(book)
    candidate = {'stock': trade.get('stock'), 'debit': bcs.get('debit'),
                 'lot_size': bcs.get('lot_size')}
    # `{}`, NOT None. `capital.plan` distinguishes them deliberately: None
    # means "this caller never had depth to give" and carries NO liquidity
    # bound, while an empty mapping means "I looked and the book said
    # nothing" and caps the size at one lot. This call site ALWAYS looks, so
    # a missing `long_ask_qty` is the second case -- and Kite ships that key
    # absent, which is the state that held on all 13 cohort records until the
    # `_atm_quote` fix. Passing None sized those entries with no liquidity
    # check at all; masked only because `lots_for_capital(2L)` is 1.
    depth = {}
    if bcs.get('long_ask_qty') is not None:
        depth = {'long': {'ask_qty': bcs.get('long_ask_qty')},
                 'short': {'bid_qty': bcs.get('short_bid_qty')}}
    plan = capital.plan(book, candidate, depth, lim)
    if not plan['lots']:
        logger.warning("AUTO-ENTRY REFUSED #%d %s: %s", trade['id'],
                       trade['stock'], plan['reason'])
        return None
    logger.info("AUTO-ENTRY #%d %s: %s | %s", trade['id'], trade['stock'],
                plan['reason'], capital.describe(book))

    try:
        out = ee.open_spread(
            kite, stock=trade['stock'], long_symbol=bcs['long_symbol'],
            short_symbol=bcs['short_symbol'], exchange='NFO',
            lot_size=int(bcs['lot_size']), lots=plan['lots'],
            gated_debit=_as_float(bcs.get('debit')),
            dry_run=dry_run, trade_id=trade['id'],
            log=lambda m: logger.info('%s', m),
            telegram=lambda m: _send_telegram(m, dry_run=dry_run))
    except Exception as e:
        logger.error("AUTO-ENTRY #%d %s: executor raised (%s) -- falling back "
                     "to the ticket", trade['id'], trade['stock'], e)
        # THE ONE BRANCH WITH NO REPORT. `open_spread` documents that it
        # returns what actually filled whatever happens, so reaching here
        # means the failure was OUTSIDE its own guard -- and `out` is gone,
        # taking any orphan or partial with it. We cannot say what is at the
        # broker; the SWEEP can, so record the intended legs and let it ask.
        # If nothing filled they both read flat and the incident resolves
        # itself in two confirmations. The alternative is the pre-2026-08-29
        # behaviour on the least understood path of the four.
        _record_entry_residue(
            store, trade, {},
            'the order path RAISED (%s) after orders may have gone out, so '
            'what filled is unknown -- these are the legs it was placing'
            % str(e)[:80],
            extra={bcs.get('long_symbol'): 0, bcs.get('short_symbol'): 0},
            dry_run=dry_run)
        return None

    if not out['lots_filled']:
        # Nothing established -- but "no COMPLETE spread" is not "nothing
        # held". A round that bought its long and could not sell its short
        # leaves a real position, and a partial fill leaves odd-sized shares;
        # both report here with `lots_filled == 0`. Record the incident BEFORE
        # returning, or the only trace of that leg is a Telegram.
        _record_entry_residue(
            store, trade, out,
            'the entry established no complete spread, so nothing was '
            'recorded as a position', dry_run=dry_run)
        # The ticket still goes out: the signal is unchanged and the owner may
        # want it by hand.
        logger.warning("AUTO-ENTRY #%d %s: nothing filled -- ticket stands",
                       trade['id'], trade['stock'])
        return None

    paid = ee.entry_debit(out)
    if paid is None:
        # Filled but unpriceable, which must NOT be recorded: every stop is
        # derived from the debit, so a record without one is a position with
        # no levels at all. Loud, and left to a human.
        logger.error("AUTO-ENTRY #%d %s: %d lot(s) FILLED but the debit could "
                     "not be computed -- NOT recorded, the position is live "
                     "and unmanaged", trade['id'], trade['stock'],
                     out['lots_filled'])
        _send_telegram(
            "\U0001F6A8 BCS %s: %d lot(s) FILLED but the entry debit could "
            "not be computed, so nothing was recorded. The position is LIVE "
            "and UNMANAGED -- record it by hand now."
            % (html.escape(str(trade['stock'])), out['lots_filled']),
            dry_run=dry_run)
        # EVERY leg is unaccounted for here, not just an orphan: complete
        # spreads filled and no record was written, so the whole position is
        # invisible to every sweep that reads records.
        _record_entry_residue(
            store, trade, out,
            '%d complete spread(s) FILLED but the entry debit could not be '
            'computed, so no record was written' % out['lots_filled'],
            extra=_filled_legs(bcs, out), dry_run=dry_run)
        return None

    filled = dict(bcs)
    filled['debit'] = paid
    filled['lots'] = out['lots_filled']
    # The orders are DONE. The budget gate inside the store must not be able
    # to refuse the record now -- refusing would not undo the trade, only lose
    # it, and an unrecorded live position is the worst state available here. It
    # is reachable in ordinary operation: `plan` sized against the QUOTED
    # debit, this carries the PAID one, which is higher by construction.
    filled['already_filled'] = True
    # THE ONE PLACE A RECORD BECOMES REAL. Orders went out and lots came back
    # filled, so this position exists at the broker and the live exit path is
    # the engine that owns it. `not dry_run` is load-bearing: the dry stub in
    # `wait_for_fill` reports COMPLETE at 0.0, so a dry run reaches this line
    # with `lots_filled` set and nothing whatsoever placed — stamping that
    # record live would hand the money path a phantom position, which is the
    # exact failure this flag exists to prevent, only inverted.
    filled['placed_at_broker'] = not dry_run
    try:
        fresh = store.mark_entered_bcs(trade['id'], filled)
    except Exception as e:
        logger.error("AUTO-ENTRY #%d %s: %d lot(s) FILLED but the record "
                     "FAILED (%s) -- the position is live and unmanaged",
                     trade['id'], trade['stock'], out['lots_filled'], e)
        _send_telegram(
            "\U0001F6A8 BCS %s: %d lot(s) FILLED but the trade store refused "
            "the record (%s). LIVE and UNMANAGED -- record it by hand now."
            % (html.escape(str(trade['stock'])), out['lots_filled'],
               html.escape(str(e)[:80])),
            dry_run=dry_run)
        _record_entry_residue(
            store, trade, out,
            '%d complete spread(s) FILLED but the trade store refused the '
            'record (%s)' % (out['lots_filled'], str(e)[:80]),
            extra=_filled_legs(bcs, out), dry_run=dry_run)
        return None

    # RECORDED, and still carrying something the record does not describe.
    # `lots_filled` spreads are a valid position with stops; the orphan leg is
    # not part of them and no stop applies to it.
    _record_entry_residue(
        store, fresh, out,
        'the recorded position is %d complete spread(s); this leg is not part '
        'of it and no stop applies to it' % out['lots_filled'],
        dry_run=dry_run)
    _verify_entry(kite, fresh, out, dry_run=dry_run)
    return fresh


#: M2. Wall-clock an ENTRY phase may spend in one cycle, in seconds.
#:
#: The arithmetic it bounds: one leg can spend `ENTRY_MAX_ATTEMPTS` (2) x
#: (`ORDER_WAIT_SEC` 30 + a 5s re-quote sleep) = 70s, there are two legs per
#: round and one round per lot -- so ~140s for a single one-lot spread, and
#: `check_watching` can enter several signals in a cycle. Four of them is
#: ~9 minutes against a 5-minute cron whose `flock -n` SKIPS the next run.
#: Exit monitoring would then not run for ten minutes because an ENTRY was
#: slow, which inverts the ordering `run_cycle` was deliberately given.
#:
#: 180s leaves a one-lot entry its full two attempts on both legs and refuses
#: to START a second one that could push the cycle past the cron interval.
ENTRY_PHASE_BUDGET_SEC = 180

#: Set at the top of each entry phase; None outside one. Module-level rather
#: than threaded through, because the check belongs at the ONE place a new
#: entry begins and the callers between here and there carry no cycle state.
_entry_deadline = None


def entry_budget_open(now=None) -> bool:
    """May a NEW entry start? True when no budget is armed.

    Checked only BEFORE an entry begins, never during one. A budget that could
    interrupt a running entry would abandon it between the long leg and the
    short -- an ORPHAN LONG, which is a real position nobody asked for. The
    safe granularity is whole entries: a signal not started stays 'triggered'
    and the next cycle picks it up, and a missed entry costs nothing
    (`feedback_no_rush_to_enter`).
    """
    if _entry_deadline is None:
        return True
    return (time.time() if now is None else now) < _entry_deadline


def start_entry_phase(now=None) -> None:
    """Arm the budget for this cycle."""
    global _entry_deadline
    _entry_deadline = (time.time() if now is None else now) \
        + ENTRY_PHASE_BUDGET_SEC


def end_entry_phase() -> None:
    """Disarm. An armed budget leaking into the next phase would refuse
    entries for reasons that have nothing to do with this cycle."""
    global _entry_deadline
    _entry_deadline = None


def _entries_allowed_or_log(trade: dict) -> tuple:
    """The auto-entry gate, with one log line either way.

    Separate so the refusal is OBSERVABLE. A gate that returns False in
    silence is indistinguishable from a signal that never arrived, and this
    book has been bitten by exactly that ambiguity before.
    """
    from bcs import entry_executor as ee
    # M2. The cycle's wall clock, BEFORE the arming switch, because a blown
    # budget is a fact about this cycle rather than about the configuration --
    # and it must be logged even on a box where auto-entry is off, or the
    # budget would first be observed on the day it starts mattering.
    if not entry_budget_open():
        logger.warning(
            "ENTRY BUDGET SPENT (%ds) -- not starting #%d %s this cycle. It "
            "stays 'triggered' and the next cycle picks it up; exit "
            "monitoring is not delayed further.",
            ENTRY_PHASE_BUDGET_SEC, trade['id'], trade['stock'])
        return False, 'entry phase budget spent'
    allowed, why = ee.entries_allowed(log=lambda m: logger.warning('%s', m))
    if not allowed:
        logger.info("AUTO-ENTRY off for #%d %s (%s) -- sending the ticket",
                    trade['id'], trade['stock'], why)
    return allowed, why


def _filled_legs(bcs: dict, out: dict) -> dict:
    """Both legs of every spread that DID fill, by symbol.

    Used only where complete spreads filled and no record was written. In that
    state the orphan report is not enough: the spreads themselves are the
    unaccounted position, and naming only an orphan would understate what is
    at the broker.
    """
    n = int(out.get('lots_filled') or 0)
    if n <= 0:
        return {}
    try:
        qty = n * int(bcs['lot_size'])
    except (KeyError, TypeError, ValueError):
        qty = 0
    return {bcs.get('long_symbol'): qty, bcs.get('short_symbol'): -qty}


def _entry_residue_legs(out: dict, extra: Optional[dict] = None) -> dict:
    """Symbols an entry left at the broker that no record accounts for.

    FIVE sources, all of them things `open_spread` reports and then declines
    to act on:

      orphan        a round bought its long and could not sell its short;
      partials      a leg filled odd-sized -- those shares are held;
      unknown_orders an order that could NOT be confirmed dead: it may be
                    working at the broker right now;
      raised_legs   the legs in flight when the order path threw;
      extra         the caller's own case, for when COMPLETE spreads filled
                    and the RECORD could not be written (an uncomputable
                    debit, a store that refused). Both legs are then
                    unaccounted for.

    THE LAST TWO WERE MISSING (found 2026-08-31), and they are the two that
    reopened the amplification `_entry_already_in_flight` exists to stop.
    Both leave a leg possibly live at the broker while producing only PROSE in
    `out['problems']` -- so `legs` came back empty, `_record_entry_residue`
    returned False without writing anything, and the next cycle found no
    residue and no unresolved journal intent. It therefore sent the SAME
    signal back to the order path: another long, every cycle, none of them in
    any store, invisible to `capital.check` and to every sweep.

    `unknown_orders` is the sharper of the two. An unknown SHORT that fills
    later, beside a new round's short, is two lots short against one long --
    the net naked short the long-first sequencing exists to make impossible,
    achieved across cycles instead of within a run.

    Quantities are what the executor reported, i.e. what we believe we hold;
    ZERO means "we do not know, ask the broker". The sweep reads the BROKER
    for the live figure, so a zero is a symbol to chase rather than a claim.
    """
    legs = {}
    orphan = out.get('orphan') or {}
    if orphan.get('symbol'):
        legs[orphan['symbol']] = int(orphan.get('qty') or 0)
    for pt in out.get('partials') or ():
        if pt.get('symbol'):
            legs[pt['symbol']] = legs.get(pt['symbol'], 0) + int(pt.get('qty') or 0)
    for uo in out.get('unknown_orders') or ():
        # `max`, not `+`: an unknown order carries no proven quantity, and
        # adding a zero to a known orphan size must not overwrite it.
        if uo.get('symbol'):
            legs[uo['symbol']] = max(legs.get(uo['symbol'], 0), 0)
    for sym, qty in (out.get('raised_legs') or {}).items():
        if sym:
            legs[sym] = max(legs.get(sym, 0), int(qty or 0))
    for sym, qty in (extra or {}).items():
        if sym:
            legs[sym] = legs.get(sym, 0) + int(qty or 0)
    return legs


def _record_entry_residue(store, trade: dict, out: dict, why: str,
                          extra: Optional[dict] = None,
                          dry_run: bool = False) -> bool:
    """Persist an entry orphan as an incident a sweep can chase. Never raises.

    `bcs/entry_executor.py` never unwinds an orphan leg and never will --
    placing a corrective order through the book that just failed to fill is
    the amplification that turned a Feb-2026 stop into a four-fill loss. So
    the orphan is REPORTED and left alone, which was right and incomplete:
    reporting meant one Telegram, and the leg then existed in NO store. The
    frozen sweep, the residue sweep, the startup verification and `--list`
    all read RECORDS, so every one of them missed it. That is the entry-side
    twin of S3, and S3 was judged worth building.

    The incident goes on the SIGNAL/POSITION record this entry was for, which
    exists in every case -- including the one where nothing filled and the
    record never became a position. `bcs/spread_monitor.sweep_entry_residue`
    chases it from there and resolves it when the broker goes flat.

    Never raises: this runs immediately after real orders, and an accounting
    failure must not become a second failure on top of a live position.
    """
    legs = _entry_residue_legs(out, extra)
    if not legs:
        return False
    detail = '; '.join('%s x%s' % (sym, qty) for sym, qty in sorted(legs.items()))
    logger.error('ENTRY RESIDUE #%d %s: %s (%s)', trade['id'], trade['stock'],
                 detail, why)
    if dry_run:
        # A dry run places nothing, so there is nothing at the broker to
        # chase. Recording one would manufacture an incident and then nag
        # about it daily until someone cleared a leg that never existed.
        logger.info('[DRY RUN] entry residue for #%d not recorded',
                    trade['id'])
        return False
    try:
        from bcs.spread_monitor import _persist_residue, ENTRY_RESIDUE
        _persist_residue(store, trade, 'COHORT', detail + ' — ' + why,
                         legs, kind=ENTRY_RESIDUE)
        # VERIFIED, not assumed. `_persist_residue` swallows a store-level
        # failure and logs it, which is right for a sweep running every five
        # seconds and wrong here: this is the ONE moment the incident gets
        # written, and returning True on a write that did not land would make
        # the alert below unreachable -- a guard nobody can observe failing.
        # The record only carries the incident when the store took it.
        if (trade.get(ENTRY_RESIDUE.field) or {}).get('state') != 'open':
            raise RuntimeError('the store did not accept the incident')
        return True
    except Exception as e:
        logger.error(
            'ENTRY RESIDUE #%d could NOT be recorded (%s) — the leg is at the '
            'broker and nothing will chase it. This alert is all there is.',
            trade['id'], e)
        _send_telegram(
            '🚨 BCS %s: an entry left %s at the broker and the '
            'incident could NOT be recorded (%s). Nothing will chase it — '
            'check Kite by hand.'
            % (html.escape(str(trade.get('stock'))), html.escape(detail),
               html.escape(str(e)[:80])),
            dry_run=dry_run)
        return False


def _verify_entry(kite, trade: dict, out: dict, dry_run: bool = False) -> None:
    """Ask the BROKER whether the position matches the record just written.

    The exit path has had this since Feb-2026 (`reconcile_after_close`); entry
    had nothing. Every stop level is computed from the RECORD, so a record
    that does not match the position is a set of stops pointing at something
    that is not there.

    Read-only, never raises, never places an order.
    """
    if dry_run:
        logger.info("[DRY RUN] entry verification skipped for #%d",
                    trade['id'])
        return
    try:
        positions = (kite.positions() or {}).get('net')
    except Exception as e:
        logger.warning("entry verification could not read positions: %s", e)
        positions = None
    v = capital.verify_entry(positions, trade)
    if v['ok']:
        logger.info("ENTRY VERIFIED #%d %s: the broker shows both legs at the "
                    "recorded size", trade['id'], trade['stock'])
        return
    detail = '; '.join(v['problems'])
    logger.error("ENTRY NOT VERIFIED #%d %s: %s", trade['id'],
                 trade['stock'], detail)
    _send_telegram(
        "\u26A0 BCS %s #%d: entry recorded but NOT verified against the "
        "broker.\n%s\nEvery stop is computed from the record. Check Kite now."
        % (html.escape(str(trade['stock'])), trade['id'],
           html.escape(detail)),
        dry_run=dry_run)
    if out.get('orphan'):
        logger.error("ENTRY #%d also left an ORPHAN long: %s",
                     trade['id'], out['orphan'])


def _build_bcs(kite, trade: dict, analysis: dict,
               price: float) -> Optional[dict]:
    """Price the vertical this signal would open, or None if a gate killed it.

    Split out of `_enter_as_bcs` so the pair exists BEFORE the vet is
    requested. Two things depended on that ordering and both were wrong:

    - the capital gate priced `analysis['best']`, the retired back ratio, and
      refused #449 on a figure 19x the real one (`_entry_candidate`);
    - a Claude vet was spent on every triggered signal, INCLUDING the ones
      `analyze_bcs`'s own hard gates were about to suppress a moment later.

    Pure pricing: no store write, no Telegram, no swing lookup (that costs a
    candle fetch and only moves the TP, never the cost). Never raises — one
    bad chain must not stop the other positions being monitored.
    """
    atm_strike = analysis.get('atm_strike')
    atm_quote = analysis.get('atm_quote')
    if not atm_strike or not atm_quote or atm_quote.get('mid') in (None, 0):
        _log_bcs_suppressed(trade, 'no usable ATM book from the analyzer')
        return None

    try:
        bcs = strikes_mod.analyze_bcs(
            kite, trade['stock'], trade['direction'], price,
            target_spot=trade['st_value'],
            expiry=analysis['expiry'],
            atm_strike=atm_strike,
            atm_quote=atm_quote,
            lot_size=analysis['lot_size'],
        )
    except Exception as e:
        logger.error("BCS build failed for #%d %s: %s",
                     trade['id'], trade['stock'], e)
        _log_bcs_suppressed(trade, f"build failed: {e}")
        return None
    if bcs.get('error'):
        _log_bcs_suppressed(trade, bcs['error'])
        return None

    bcs['expiry'] = analysis['expiry']
    bcs['entry_spot'] = price
    return bcs


def _enter_as_bcs(store: ZebraStore, kite, trade: dict, analysis: dict,
                  price: float, bcs: Optional[dict] = None,
                  dry_run: bool = False):
    """Open a first-class BCS from a triggered signal.

    Returns:
      None            — skipped; the signal stays 'triggered' and the
                        drift/stale checks clean it up, as the zebra path does
      (bcs, trade)    — PAPER: entered, `trade` is the fresh record
      (bcs, None)     — LIVE: nothing entered, the alert IS the order ticket

    The three-way return is deliberate. An earlier version returned None for
    both "skipped" and "live", and the caller's `continue` then suppressed
    every entry alert in LIVE mode — where the alert is the only way a trade
    ever gets placed. Auto-entry is a paper-mode behaviour; alerting is not.

    `bcs` is the pair `_build_bcs` already priced for the capital gate and the
    vet handoff; it is re-built here only for callers that did not.

    Never raises into the cycle: one bad chain must not stop the other
    positions being monitored.
    """
    if bcs is None:
        bcs = _build_bcs(kite, trade, analysis, price)
        if bcs is None:
            return None

    # A swing level standing between spot and the magnet shortens the TP. Done
    # HERE, before either branch, so the LIVE order ticket quotes the same
    # target the paper position books against — the ticket is the only exit
    # instruction the owner gets in LIVE.
    try:
        bcs['swing_tp'] = history.swing_tp(
            kite, trade['stock'], trade.get('timeframe'), trade['direction'],
            price, float(trade['st_value']))
    except Exception as e:      # a missing chart must never block an entry
        logger.warning("swing TP lookup failed for #%d %s: %s",
                       trade['id'], trade['stock'], e)
        bcs['swing_tp'] = None
    bcs['swing_tp'] = _swing_clears_breakeven(trade, bcs, bcs.get('swing_tp'))
    s = bcs.get('swing_tp') or {}
    if s.get('applied'):
        logger.info("TP SHORTENED #%d %s: %s %.2f (%s, %d bars ago) instead of "
                    "ST %.2f — %.0f%% less distance, %.0f%% left to win",
                    trade['id'], trade['stock'], s['kind'], s['tp_spot'],
                    s['timeframe'], s['bars_ago'], s['st_value'],
                    s['shortened_by_pct'], s['retained_pct'])
    elif s:
        # Found but NOT applied — the level is too close to spot to be worth
        # trading to. TP stays the ST line; the agent still gets told.
        logger.info("TP UNCHANGED #%d %s: %s %.2f in the way but %s",
                    trade['id'], trade['stock'], s['kind'], s['level'],
                    s['reason'])
    if not cfg.PAPER_MODE:
        # ── LIVE. Alert-only unless auto-entry is armed ──────────────────
        #
        # `entries_allowed` fails CLOSED, which is the opposite of the exit
        # kill switch and correct for the opposite reason: there, a config
        # error must not abandon the stops on a live book; here, it must not
        # start opening positions. So the fall-through is the ticket, which is
        # exactly what LIVE did before this branch existed.
        fresh = _auto_enter_bcs(store, kite, trade, bcs, dry_run=dry_run)
        return bcs, fresh      # fresh is None when the ticket is the answer

    try:
        fresh = store.mark_entered_bcs(trade['id'], bcs)
    except Exception as e:      # broad: bad data OR persist/IO failure
        logger.error("BCS auto-enter failed for #%d %s: %s — left triggered "
                     "(will drift-cancel), no alert sent",
                     trade['id'], trade['stock'], e)
        return None
    return bcs, fresh


def _log_bcs_suppressed(trade: dict, reason: Optional[str]) -> None:
    """Record a gated shadow BCS — LOG ONLY, deliberately never Telegram.

    User's call (2026-08-10): a suppression is a non-event, and pushing one
    notification per rejected signal would be exactly the alert fatigue the
    gates exist to cure. The whole point is fewer, better tickets.

    It is still recorded at WARNING with a fixed 'BCS SUPPRESSED' prefix so a
    gate that starts rejecting everything is one grep away:
        grep 'BCS SUPPRESSED' logs/cron_zebra.log
    No html-escaping here — a log line is not parsed as markup.
    """
    # The tail of this line used to read "zebra entered silently for the A/B
    # record". That was true only while the back ratio was the primary
    # structure and the BCS was its shadow. Under the BCS-only pipeline —
    # and after the back ratio was decommissioned on 2026-08-27 — a suppressed
    # BCS means NOTHING entered at all, which is the opposite fact and the one
    # a reader needs.
    logger.warning("BCS SUPPRESSED #%d %s (%s): %s — NO position opened "
                   "(the BCS is the only structure; nothing enters behind it)",
                   trade['id'], trade['stock'], trade['direction'],
                   reason or "no viable BCS pair")


def _swing_clears_breakeven(trade: dict, bcs: dict, swing):
    """Refuse a shortened target the spread cannot make money at.

    `history.swing_tp` reasons purely about the CHART: it sees spot, the ST
    line and the pivots between them, and it knows nothing about the strikes or
    the debit. Its 40%-retained floor therefore bounds the SPOT distance kept,
    not the PAYOFF — and the short strike is still pinned at the ST line, so a
    level that keeps 40% of the journey can still sit below the spread's
    breakeven. Measured over the 42 records: the override applies to 11, the
    median collectable gain at the swing target is ~24% of max, and TWO land
    BELOW breakeven at intrinsic (MPHASIS -10.8%, BANDHANBNK -43.4% of max
    gain). Firing "target reached" into a booked loss is not a target.

    Breakeven for a vertical is `long_strike ± debit`, in the direction the
    trade needs spot to travel. Below it the override is dropped and the ST
    line stays the target; the level is still REPORTED, because "there is
    resistance in the way and it is not worth trading to" is exactly the
    context the vetting agent should have.
    """
    if not isinstance(swing, dict) or not swing.get('applied'):
        return swing
    try:
        k_l = float(bcs['long_strike'])
        debit = float(bcs['debit'])
        tp = float(swing['tp_spot'])
    except (KeyError, TypeError, ValueError):
        return swing
    be = k_l + debit if trade['direction'] == 'CE' else k_l - debit
    clears = tp >= be if trade['direction'] == 'CE' else tp <= be
    if clears:
        return swing
    logger.warning(
        "TP OVERRIDE DROPPED #%d %s: swing %s %.2f is short of breakeven "
        "%.2f (long %.2f %s debit %.2f) — reaching it would book a LOSS, "
        "keeping the ST line %.2f",
        trade['id'], trade['stock'], swing.get('kind'), tp, be, k_l,
        '+' if trade['direction'] == 'CE' else '-', debit, swing.get('st_value', 0))
    dropped = dict(swing)
    dropped.update({'applied': False, 'tp_spot': None,
                    'reason': f'below breakeven {be:.2f}',
                    'level': tp, 'breakeven': round(be, 2)})
    return dropped


def _swing_tp_line(swing) -> str:
    """One line explaining a moved target, or nothing at all.

    A TP that silently differs from the ST line the signal was built on reads
    as a bug to whoever is holding the position. Say which level it is and how
    old, so the owner can look at the same candle the bot did.
    """
    if not isinstance(swing, dict) or not swing.get('tp_spot'):
        return ''
    kind = 'swing low' if swing['kind'].endswith('low') else 'swing high'
    return (f"🎯 TP shortened to {swing['tp_spot']:g} — {kind} from "
            f"{html.escape(str(swing['date']))} ({swing['bars_ago']} "
            f"{html.escape(str(swing['timeframe']))} bars ago) stands between "
            f"spot and ST {swing['st_value']:g}\n")


def _shadow_size_line(reason: str) -> str:
    """What the ticket says for a PAPER entry a live book would have refused.

    Not `_size_line`: that one answers "how many lots shall I place", and for
    this signal the honest answer is "none, but one was opened anyway because
    paper deliberately does not cap". Rendering its DO-NOT-ENTER text over an
    entry that already happened is #449's contradiction from the other side.

    Says PAPER first, so the reader never mistakes it for an order.
    """
    return ("\n\n📝 <i>PAPER entry — a live book would have REFUSED "
            "this: %s. Recorded anyway so the validation set is not biased "
            "by which trades the cap happened to allow.</i>"
            % html.escape(str(reason)))


def _size_line(plan: Optional[dict]) -> str:
    """How many lots, what bounded it, and how to place them.

    The ticket used to quote "Capital (1 lot)" and stop there, which answers
    the cost of a size nobody had decided. This states the DECISION and its
    binding limit, because those are different facts to act on: bound by
    LIQUIDITY is a reason to look harder at the signal, bound by BUDGET is a
    reason to look at the book.

    Takes the plan the caller already made rather than making a second one.
    It used to re-run `capital.plan` here, and in PAPER that is AFTER the
    position has been booked -- so the ticket sized the signal against a book
    that already contained it, and `max_open_per_stock: 1` made every paper
    ticket refuse itself. Two calls to `capital.check` per signal, on
    different inputs, is also how #449 came to state two prices for one trade.

    Never raises. A sizing failure must not cost the owner the ticket -- the
    symbols and the debit are still the order.
    """
    if not plan:
        return "\n\n⚠ <i>Size not computed — enter 1 lot.</i>"
    pl = plan
    if not pl['lots']:
        # Defence in depth: `check_watching` sends the refusal notice instead
        # of a ticket, so a refused plan should never reach a ticket at all.
        return ("\n\n🚫 <i>Capital says DO NOT ENTER: "
                f"{html.escape(pl['reason'])}</i>")
    n, per = pl['lots'], pl['slice_lots']
    orders = '' if n == per else (f" as {n} x {per}-lot order(s) — no "
                                  f"per-order brokerage, and each fill is "
                                  f"confirmed before the next")
    return (f"\n\n📦 <i>Size <b>{n} lot(s)</b> = Rs {pl['capital']:,.0f} "
            f"(bound by {html.escape(pl['bound'])}). Place{orders}.</i>")


def _format_bcs_enter_alert(trade: dict, analysis: dict, bcs: dict) -> str:
    """BCS-led ENTER alert (zebra silenced): the shadow spread is the story."""
    stock = trade['stock']
    direction = trade['direction']
    spot = analysis['spot']
    st_val = trade['st_value']
    gap = analysis.get('current_gap_pct', abs(spot - st_val) / st_val * 100)
    pull_dir = 'up to' if direction == 'CE' else 'down to'
    conviction = ' ⭐ALIGNED' if cfg.is_trend_aligned(
        direction, trade.get('st_direction')) else ''
    warn = ' ⚠ ' + html.escape(','.join(bcs['warnings'])) \
        if bcs.get('warnings') else ''
    k_atm = f"{bcs['long_strike']:g}"
    k_tgt = f"{bcs['short_strike']:g}"
    capital = bcs['debit'] * bcs['lot_size']
    max_p = bcs['max_profit_per_share']
    rr = (max_p / bcs['debit']) if bcs['debit'] > 0 else 0
    return (
        f"📐 <b>ENTER BCS</b>  <code>{html.escape(str(stock))}</code>  "
        f"({html.escape(str(direction))}){conviction}\n"
        f"Level {st_val:,.2f} | spot {spot:,.2f} | gap {gap:.2f}% "
        f"({pull_dir} Level)\n"
        f"expiry {analysis['expiry']} ({analysis['dte']} DTE) | "
        f"lot {bcs['lot_size']} | Capital (1 lot) = {capital:,.0f}{warn}\n"
        f"\n"
        f"Strikes <b>{k_atm} / {k_tgt}</b>   debit {bcs['debit']:g} "
        f"({bcs['debit_to_width_pct']:.0f}% of width) | "
        f"maxP {max_p:g} | R:R 1:{rr:.1f}\n"
        f"fair {bcs.get('debit_mid', bcs['debit']):g} — the book takes "
        f"{bcs.get('entry_cost', 0):g}/sh to open "
        f"({bcs.get('entry_cost_pct', 0):.0f}% of max gain)\n"
        f"{_swing_tp_line(bcs.get('swing_tp'))}"
        f"\n"
        # ASK to buy, BID to sell — this is an order ticket, and the prices on
        # it have to be ones the owner can actually transact at.
        f"🟢 BUY 1× <code>{html.escape(str(bcs['long_symbol']))}</code>  "
        f"{bcs['long_ask']:g}\n"
        f"🔴 SELL 1× <code>{html.escape(str(bcs['short_symbol']))}</code>  "
        f"{bcs['short_bid']:g}"
    )


def _paper_close_line(trade: dict, mid: Optional[float]) -> str:
    """Inline P&L estimate for paper-mode auto-close alerts."""
    if not cfg.PAPER_MODE:
        return ""
    if mid is None:
        return "\n[PAPER auto-close pending — no quote, will retry]"
    debit = trade.get('debit', 0)
    qty = trade.get('quantity', 0)
    pnl_per_share = mid - debit
    pnl = pnl_per_share * qty
    pct = (pnl_per_share / debit * 100) if debit > 0 else 0
    return (f"\n[PAPER auto-closed] exit_mid {mid:.2f}  "
            f"P&L Rs {pnl:+,.0f} ({pct:+.1f}%)")


def _format_tp_alert(trade: dict, spot: float, mid: Optional[float] = None,
                     on_latch: bool = False) -> str:
    """The TP ticket.

    `on_latch` says this exit is firing on a touch seen in an EARLIER cycle,
    which the caller knows and this function cannot re-derive without copying
    the direction rule. It matters because the old first line — "spot X hit TP
    Y" — is simply false when spot has since retreated, and the reader acts on
    this message. The touch is what fired; say so, and say where spot is now.
    """
    paper = _paper_close_line(trade, mid)
    if on_latch:
        touch = trade.get('tp_touch_spot')
        seen = f"{touch:,.2f}" if isinstance(touch, (int, float)) else 'NA'
        line = (f"TP {trade['tp_spot']:,.2f} was TOUCHED at {seen}"
                f" ({html.escape(str(trade.get('tp_touched_at') or 'earlier'))})\n"
                f"spot is now {spot:,.2f} — exiting on the latched touch")
    else:
        line = f"spot {spot:,.2f} hit TP {trade['tp_spot']:,.2f}"
    return (
        f"\U0001F3AF <b>{_struct_label(trade)} TP</b>  "
        f"{html.escape(str(trade['stock']))} ({trade['direction']})\n"
        f"{line}\n"
        f"Long: <code>{html.escape(str(trade['long_symbol']))}</code>\n"
        f"Short: <code>{html.escape(str(trade['short_symbol']))}</code>{paper}"
    )


def _format_spot_sl_alert(trade: dict, spot: float, mid: Optional[float] = None) -> str:
    paper = _paper_close_line(trade, mid)
    return (
        f"\U0001F6D1 <b>{_struct_label(trade)} SPOT SL</b>  "
        f"{html.escape(str(trade['stock']))} ({trade['direction']})\n"
        f"spot {spot:,.2f} hit SL {trade['sl_spot']:,.2f}\n"
        f"Adverse move from entry {trade['entry_spot']:,.2f}{paper}"
    )


def _format_debit_sl_alert(trade: dict, mid: float) -> str:
    paper = _paper_close_line(trade, mid)
    pct_lost = (1 - mid / trade['debit']) * 100 if trade.get('debit') else 0
    return (
        f"\U0001F4C9 <b>{_struct_label(trade)} DEBIT SL</b>  "
        f"{html.escape(str(trade['stock']))} ({trade['direction']})\n"
        f"Mid {mid:.2f} ≤ debit-SL {trade['debit_sl_value']:.2f} "
        f"(entry debit {trade['debit']:.2f})\n"
        f"Lost ~{pct_lost:.0f}% of debit.{paper}"
    )


def _format_trail_alert(trade: dict, mid: float, tl: dict) -> str:
    """Trail-stop exit. The LEVEL sits above the entry debit by construction;
    the FILL does not.

    The docstring used to claim "Always a PROFIT", which mfe.py explicitly
    refutes two files away: the trigger is `mid <= level` and the booking price
    is `mid`, so a gap straight through the level books wherever it landed —
    possibly below the debit. `feedback_trigger_is_not_the_fill` is the lesson,
    and the alert is the layer the human actually reads, so a breached trail
    that lost money must not render as "Locking in ~Rs -12,000".
    """
    paper = _paper_close_line(trade, mid)
    qty = int(trade.get('quantity') or 0)
    kept = (mid - trade['debit']) * qty
    if kept >= 0:
        outcome = (f"Locking in ~Rs {kept:,.0f} of a peak Rs "
                   f"{tl['peak_gain'] * qty:,.0f}.")
    else:
        outcome = (f"⚠ BREACHED ON A GAP — booked BELOW entry: ~Rs "
                   f"{kept:,.0f} against a peak Rs "
                   f"{tl['peak_gain'] * qty:,.0f}. The trail triggered, it did "
                   f"not fill where it triggered.")
    return (
        f"\U0001F512 <b>{_struct_label(trade)} TRAIL</b>  "
        f"<code>{trade['stock']}</code> ({trade['direction']})\n"
        f"Mid {mid:.2f} ≤ trail {tl['level']:.2f} "
        f"(peak {trade['debit'] + tl['peak_gain']:.2f}, "
        f"{tl['peak_pct_of_max']:.0f}% of max)\n"
        f"{outcome}{paper}"
    )


def _format_corp_action_alert(trade: dict, evt: dict) -> str:
    """Tell the human the bot has stood down on this position for the day.

    Deliberately explicit that NOTHING will fire: this is the one alert where
    silence afterwards means the guards are off, not that all is well.
    """
    return (
        f"⚠ <b>CORPORATE ACTION</b>  <code>{trade['stock']}</code> "
        f"({trade['direction']})\n"
        f"{html.escape(str(evt.get('type', '?')).upper())} ex-date today — "
        f"{html.escape(str(evt.get('title', ''))[:80])}\n"
        f"Strikes and lot size are adjusted, so every stored level "
        f"(entry {trade.get('entry_spot')}, TP {trade.get('tp_spot')}, "
        f"SL {trade.get('sl_spot')}, debit {trade.get('debit')}) refers to a "
        f"share that no longer exists.\n"
        f"<b>All automated exits are SUSPENDED for this position today.</b> "
        f"Close or re-enter it manually."
    )


def _format_blind_alert(trade: dict, reason: Optional[str],
                        spot: Optional[float] = None) -> str:
    """One-shot warning that DEBIT-SL valuation has been blind (unreliable /
    missing book) long enough to matter. Spot TP/SL stay armed — this is pure
    observability, not a trading action."""
    mins = cfg.DEBIT_BLIND_CYCLES * cfg.MONITOR_INTERVAL_SEC // 60
    # Where the UNDERLYING is, while the book cannot be seen. This is the one
    # moment `sl_spot` earns its keep: the spot stop is deliberately not a
    # trigger (it cuts 40% of winners at 3%), but when the option book has gone
    # dark, how far spot has travelled against the position is the only
    # independent read available to the human deciding whether to look.
    spot_line = ''
    if spot and trade.get('sl_spot') and trade.get('entry_spot'):
        try:
            entry = float(trade['entry_spot'])
            adverse = ((entry - spot) / entry if trade['direction'] == 'CE'
                       else (spot - entry) / entry) * 100
            spot_line = (f"Spot {spot:.2f} vs entry {entry:.2f} "
                         f"({adverse:+.1f}% adverse), 3% mark "
                         f"{float(trade['sl_spot']):.2f}.\n")
        except (TypeError, ValueError, ZeroDivisionError):
            spot_line = ''
    return (
        f"⚠ <b>{_struct_label(trade)} DEBIT-BLIND</b>  "
        f"<code>{trade['stock']}</code> ({trade['direction']})\n"
        f"Debit-SL valuation blind ~{mins} min: option book unreliable "
        f"({reason or 'no quote'}).\n"
        f"{spot_line}"
        f"Spot TP/SL still armed. Check the book manually before acting."
    )


def _format_no_spot_alert(trade: dict) -> str:
    """The underlying itself stopped quoting on an OPEN position.

    Deliberately not `_format_blind_alert`: that one ends "Spot TP/SL still
    armed", which is the opposite of true here. When spot is the missing input,
    nothing is armed — TP, the expiry nag's pricing and the spot veto all run
    off it. This is the most complete blindness the engine has, and it used to
    be the only one that said nothing at all.
    """
    return (
        f"⚠ <b>{_struct_label(trade)} NO SPOT</b>  "
        f"<code>{trade['stock']}</code> ({trade['direction']})\n"
        f"The UNDERLYING is not quoting — suspended, renamed or delisted.\n"
        f"Every exit trigger on this position is blind: TP, debit-SL, trail "
        f"and the spot veto all read spot.\n"
        f"Expiry {trade.get('expiry', '?')}. Check the symbol manually — an "
        f"open position on a dead symbol will not close itself."
    )


def _format_time_alert(trade: dict, days_left: int,
                       mid: Optional[float] = None) -> str:
    paper = _paper_close_line(trade, mid)
    n_long = 1 if trade.get('structure') == 'bcs' else 2
    return (
        f"⏰ <b>EXIT REMINDER</b> [{_struct_label(trade)}]  "
        f"<code>{trade['stock']}</code>  ({trade['direction']})\n"
        f"T-{days_left} session(s) | expiry {trade['expiry']}\n"
        f"Close before physical-settlement margin spike ({n_long}× long ITM).\n"
        f"\n"
        f"🔴 BUY back 1× <code>{trade['short_symbol']}</code>\n"
        f"🟢 SELL {n_long}× <code>{trade['long_symbol']}</code>{paper}"
    )


# ── Watching → triggered ─────────────────────────────────────────────────

def _drain_queued_out_of_band(store: ZebraStore, trade: dict, kite,
                              price: float, gap_pct: float) -> None:
    """Retry a QUEUED vet whose gap has drifted out of the trigger band.

    The signal is still inside the watch band — drift and stale cancels ran
    before this — so it remains a live candidate; it simply cannot enter at
    this price. Vetting it now means the verdict is ready if price comes back,
    instead of the queue silently freezing until the drop clock kills it.

    Never raises. This runs inside the per-trade loop, so an exception here
    would cost every signal after this one its cycle, including its own
    drift-cancel check — the failure mode `promote_queued`'s caller below was
    already wrapped against.
    """
    try:
        analysis = strikes_mod.analyze(kite, trade['stock'],
                                       trade['direction'], price)
        if analysis.get('error') or not analysis.get('atm_strike'):
            # No usable book to hand an agent. Staying queued is correct: the
            # drop clock still bounds the wait and the book is routinely fine
            # a few cycles later.
            logger.debug("Queued #%d %s: no usable book out of band (%s)",
                         trade['id'], trade['stock'],
                         analysis.get('error') or 'no ATM strike')
            return
        # Price the pair here too. Without it the handoff carries a capital
        # plan built from an unpriceable candidate, and before 2026-08-27 it
        # carried one built from the retired back ratio — either way a number
        # about a position nobody would open.
        bcs = _build_bcs(kite, trade, analysis, price)
        vet_mod.promote_queued(
            store, trade['id'],
            context=_vet_context(store, trade, analysis, gap_pct, kite, bcs))
    except Exception as e:
        logger.error("Out-of-band queue drain failed for #%d: %s — stays "
                     "queued, retried next cycle", trade['id'], e)


def check_watching(store: ZebraStore, kite, dry_run: bool = False) -> None:
    """For each watching/triggered signal, recompute gap; trigger if in zone,
    cancel if drifted. Triggered signals are also re-checked so user doesn't
    act on stale alerts after the spot has moved out of zone."""
    watching = store.get_watching() + store.get_triggered()
    if not watching:
        return

    stocks = list({t['stock'] for t in watching})
    ltps = get_ltp(kite, stocks)

    for trade in watching:
        # ── Per-signal fault isolation ───────────────────────────────
        #
        # `check_entered` got exactly this guard, with the note that the
        # earlier fix 'guarded one CALL, this guards the CLASS'. This loop
        # never got the copy, and it has the same three raising shapes: it
        # indexes directly (`trade['st_value']`, `trade['direction']`),
        # divides by `st_value` (a 0 or None from a half-merge or a hand
        # edit is a ZeroDivisionError/TypeError), and calls a store that
        # can raise `LockTimeout` or `ValueError` from `_must_find` when a
        # sibling process has removed the row.
        #
        # Any of those propagated to run_cycle's PHASE-level catch, so
        # every signal sorted after the bad one got no band check, no
        # drift cancel and no entry -- and for a persistently bad row,
        # never again. One malformed record silently stopped the scanner.
        try:
            stock = trade['stock']
            price = ltps.get(stock, 0)
            if price <= 0:
                # No LTP, so the gap is never updated, so the drift and stale
                # cancels below can never fire — a suspended, renamed or delisted
                # symbol becomes an IMMORTAL row, holding a slot against
                # MAX_WATCHING_SIGNALS (25) and blocking its own stock through
                # dedup forever. Age is the only bound that still works when the
                # price feed does not.
                if _expire_if_ancient(store, trade):
                    continue
                continue
            st_val = trade['st_value']
            gap = (price - st_val) / st_val if trade['direction'] == 'PE' \
                else (st_val - price) / st_val
            gap_pct = round(gap * 100, 2)
            store.update_gap(trade['id'], gap_pct)

            # Drift cancel: if gap blew past watch band by 20%
            if gap > cfg.WATCH_GAP_MAX * 1.2:
                _band_cancel(store, trade,
                             f'drift: gap {gap_pct:.2f}% > watch+20%',
                             dry_run=dry_run)
                continue

            # Stale (too close): gap fell below stale_min → past entry zone
            if gap < 0:
                # Crossed the line. For CE-Zebra (price < ST, gap = (ST-price)/ST),
                # negative gap means price overshot above ST. Likely already moving
                # toward target. Cancel as we missed the entry window.
                _band_cancel(store, trade,
                             f'crossed: gap {gap_pct:.2f}% (past ST)',
                             dry_run=dry_run)
                continue

            if gap > cfg.TRIGGER_GAP_MAX:
                # Not yet in trigger zone; just keep watching.
                #
                # EXCEPT for a signal already waiting on a vet. The queue is
                # drained by the gate ~140 lines below, which this `continue` puts
                # out of reach, so a queued signal that drifted back into the watch
                # band could never be retried — `attempts` froze and the drop clock
                # ran out underneath it. HAVELLS #404 on 2026-08-14 is the case:
                # triggered at 3.92%, drifted to 4.46%, and was dropped 60 minutes
                # later having had exactly one attempt across nine cycles, with the
                # alert blaming an agent slot that was free the whole time.
                #
                # Vetting it here changes nothing about what may ENTER: entry lives
                # past this `continue` and is still gated on the trigger band, so an
                # ALLOWED verdict simply waits for price to come back. Costs one
                # analyzer re-quote per queued signal per cycle, and only while one
                # is queued — which is rare.
                if cfg.VET_ENABLED and vet_mod.is_queued(store.find(trade['id'])
                                                         or trade):
                    _drain_queued_out_of_band(store, trade, kite, price, gap_pct)
                continue
            if gap < cfg.STALE_GAP_MIN:
                # In stale zone: too late
                _band_cancel(
                    store, trade,
                    f'stale: gap {gap_pct:.2f}% < {cfg.STALE_GAP_MIN*100:.1f}%',
                    dry_run=dry_run)
                continue

            # Already triggered + still in zone.
            # Vet OFF: the alert (and paper entry) happened in the trigger cycle —
            #   nothing to do. (Saves Kite quote calls in the analyzer.)
            # Vet ON: 'triggered' is the WAITING state. The entry was deliberately
            #   deferred past the verdict and it happens HERE, on the first cycle
            #   whose verdict permits it. Without this fall-through an allowed
            #   signal would wait forever: this early `continue` runs long before
            #   the gate below, so the gate alone can never re-admit it.
            if trade['status'] == 'triggered':
                if not cfg.VET_ENABLED:
                    # In PAPER a completed entry moves the row to `entered`, so a
                    # row still sitting in `triggered` means the entry did NOT
                    # complete — a suppressing gate, one bad quote, an IO blip.
                    # The old `continue` (justified as "saves Kite quote calls")
                    # therefore parked it forever: still inside its trigger band,
                    # never retried, and only ever released by a drift/stale
                    # cancel. Retry while it is still in the zone; the band is a
                    # window, and a book that was unquotable at 10:05 is routinely
                    # fine at 10:10.
                    #
                    # LIVE stops here ONLY once the ticket has actually gone out.
                    # Re-alerting every 5 minutes would be noise rather than a
                    # retry — but a ticket whose Telegram FAILED released its
                    # claim, and an unconditional `continue` then parked the
                    # signal forever with the human never told. Gate on the claim,
                    # not on the mode.
                    if not cfg.PAPER_MODE:
                        if (store.find(trade['id']) or {}).get('vet_enter_alerted_at'):
                            continue
                        logger.warning(
                            "TICKET RETRY #%d %s: live entry ticket was never "
                            "delivered (claim not held) — re-running the analyzer",
                            trade['id'], stock)
                    logger.info(
                        "RETRY #%d %s: still triggered and in the zone "
                        "(gap %.2f%%) — entry did not complete, re-running the "
                        "analyzer", trade['id'], stock, gap_pct)
                state = vet_mod.vet_state(store.find(trade['id']) or trade)
                if state == vet_mod.VETOED:
                    # THE ONLY place a veto is ever observed. The verdict lands
                    # between cycles (the CLI is a separate process), so every
                    # post-veto cycle stops right here — anything downstream of
                    # this `continue` is unreachable for a vetoed signal. Opening
                    # the shadow anywhere below would look wired and never run,
                    # which is exactly the mistake the comment above describes and
                    # exactly how the veto scoring was dead on arrival.
                    try:
                        outcomes_mod.open_shadow(store, trade['id'],
                                                 entry_spot=price)
                    except Exception as e:
                        logger.error("Veto shadow failed for #%d: %s",
                                     trade['id'], e)
                    continue
                if state == vet_mod.PENDING:
                    continue    # still deciding; expire_stale bounds it
                # QUEUED falls THROUGH deliberately: the analyzer below re-quotes
                # the book, and the vetting gate then promotes the signal with that
                # fresh context. A queued entry therefore never hands an agent a
                # stale snapshot, and it costs no extra Kite call — this path runs
                # the analyzer anyway. The band is re-checked above every cycle, so
                # a queued signal whose price left the zone is drift-cancelled by
                # the machinery that already exists.
                # ALLOWED → enter now (re-running the analyzer for a fresh book;
                # entry drift ≤ one tick is the accepted cost of vetting).
                # None → the vet request never landed (crash after
                # mark_triggered): fall through so the gate below re-requests it —
                # recoverable instead of parked until drift-cancel.
                #
                # M6: UNAVAILABLE is NOT an entry state — see the gate below.
                if state == vet_mod.ALLOWED \
                        and not cfg.PAPER_MODE \
                        and (store.find(trade['id']) or {}).get('vet_enter_alerted_at'):
                    continue    # LIVE: the order-ticket alert already went out

            # In trigger zone — run analyzer + alert
            try:
                analysis = strikes_mod.analyze(kite, stock, trade['direction'],
                                               price)
            except Exception as e:
                logger.error("Strike analysis failed for %s: %s", stock, e)
                continue

            if analysis.get('error'):
                logger.warning("Strike analysis %s skipped: %s", stock, analysis['error'])
                continue
            # Require what the structure we ACTUALLY trade needs: the ATM book.
            # This used to have a second arm gating on a tradeable back-ratio
            # `best`, which let a retired structure's constraints (net-extrinsic,
            # deep-ITM liquidity) veto a spread that shares none of them. The back
            # ratio was decommissioned on 2026-08-27 and `analyze()` no longer
            # produces a `best` at all.
            atm_q = analysis.get('atm_quote') or {}
            if not analysis.get('atm_strike') or not atm_q.get('mid'):
                logger.info("No usable ATM book for %s, leaving in watching",
                            stock)
                continue

            analysis['current_gap_pct'] = gap_pct
            # `alert_strikes` recorded the back-ratio shortlist. There is no such
            # shortlist any more, so the record carries an empty list rather than
            # something that reads like a set of options the operator could pick
            # from. Historical records keep whatever they were written with.
            alert_strikes: list = []
            if trade['status'] == 'watching':
                try:
                    store.mark_triggered(trade['id'], price, gap_pct, alert_strikes)
                except ValueError as e:
                    logger.warning("mark_triggered failed for #%d: %s", trade['id'], e)
                    continue

            # ── Price the structure that would actually open, BEFORE the vet ──
            # Everything below reasons about a position: the capital gate sizes
            # it, the vet judges it, the ticket quotes it. Building it here means
            # all three see ONE pair. Before 2026-08-27 it was built AFTER the
            # vet, so the capital layer had nothing to price but the retired back
            # ratio in `analysis['best']` (#449), and `analyze_bcs`'s own hard
            # gates ran only after a Claude call had already been spent.
            bcs = _build_bcs(kite, trade, analysis, price)
            if bcs is None:
                continue          # gated and logged; nothing to vet or alert

            # ── Capital, BEFORE the vet ──────────────────────────────────────
            # A deterministic check costing one store read decides the same thing
            # a Claude spawn costs a slot and ~4 minutes to decide. #449 was
            # refused for having no free slot AFTER the vet had already run on it,
            # and the operator was then shown a full order ticket — click-copy
            # symbols, "Vetted by Claude" — with "DO NOT ENTER" underneath it.
            #
            # This is a SHADOW in paper and stays one: `_refuse_if_over_budget`
            # still records the position so the validation book keeps every
            # signal. What changes is that the vet is not spent and the operator
            # is not handed an order ticket for a trade the book cannot take.
            cap_plan = None
            try:
                cap_plan = _capital_context(store, trade, analysis, bcs)['plan']
            except Exception as e:
                # A capital lookup that fails must not decide anything. Fall
                # through exactly as before: the vet runs, the ticket renders, and
                # `_refuse_if_over_budget` is still the backstop at entry.
                logger.warning("capital pre-gate failed for #%d %s: %s — "
                               "continuing unsized", trade['id'], stock, e)
            capital_refused = bool(cap_plan) and not cap_plan['lots']
            # THE SIZING DECISION RIDES ON THE RECORD, not just on a log line.
            # `bcs` is the same dict `_bcs_entry_fields` reads at entry, so
            # stamping it here needs no plumbing and covers the paper shadow too
            # -- which is the only book there is to calibrate the lot ladder from.
            if cap_plan:
                bcs['entry_plan'] = {'lots': cap_plan['lots'],
                                     'bound': cap_plan['bound'],
                                     'bounds': cap_plan['bounds'],
                                     'capital': cap_plan['capital']}
            # ── PAPER SHADOW-LOGS WHAT IT WOULD HAVE REFUSED ────────────────
            #
            # `ZebraStore._refuse_if_over_budget` states the rule: "LIVE refuses.
            # PAPER evaluates and LOGS what it WOULD have refused" — capping paper
            # entries would bias which trades the validation record contains, and
            # an unentered paper signal costs nothing. The STORE implements that.
            # This pre-gate, the other place the cap is enforced, did not.
            #
            # What it cost, immediately: `max_open_trades` went 8 -> 4 on
            # 2026-08-29 (M9, a LIVE decision) while the cohort held 6 open paper
            # positions, so from the next session EVERY new signal was refused
            # here — which skips the vet (below) and swaps the order ticket for a
            # capital notice — while the store's paper exemption let the record
            # enter anyway. A paper trade entered with NO VERDICT on it, and the
            # vetting pipeline THE GOAL exists to validate going dark, from a
            # config change that was supposed to be live-only.
            # [[feedback_the_copy_you_did_not_open]], on the paper/live boundary.
            #
            # The log line stays either way: shadow evidence is how the rupee
            # numbers get chosen from data instead of guessed.
            capital_shadow = ''
            if capital_refused and cfg.PAPER_MODE:
                # THREE consequences were bundled behind one flag, and only two of
                # them are right in paper:
                #   * do not ENTER      — paper enters anyway (the store exempts
                #                         it), so this one never applied here;
                #   * spend no VET      — WRONG in paper: the record enters, so an
                #                         unvetted entry is a hole in the very
                #                         evidence the paper run exists to produce;
                #   * send no TICKET    — RIGHT in both: an order ticket that says
                #                         DO NOT ENTER underneath is #449.
                # So the refusal stops binding, and the reason is carried forward
                # to keep the ALERT honest instead of silently ticketing.
                capital_shadow = cap_plan['reason']
                logger.warning(
                    "CAPITAL WOULD REFUSE #%d %s: %s — PAPER enters anyway and is "
                    "vetted normally, so the validation record stays unbiased. "
                    "%s", trade['id'], stock, capital_shadow,
                    # WHOLE BOOK: see `_capital_context`.
                    capital.describe(store.load_trades()))
                capital_refused = False
            elif capital_refused:
                logger.warning(
                    "CAPITAL REFUSES #%d %s: %s — no vet spent, no order ticket. "
                    "%s", trade['id'], stock, cap_plan['reason'],
                    # WHOLE BOOK: see `_capital_context`.
                    capital.describe(store.load_trades()))

            # ── Claude vetting gate ──────────────────────────────────────────
            # A triggered signal waits for a verdict before it enters. The vet is
            # requested once; the spawned CLI is never waited on, so this cycle
            # returns immediately and the entry happens on a later tick.
            #
            # INVERTED 2026-08-13. This used to read "every branch that is not an
            # explicit ALLOW must still let the trade through eventually", and that
            # is now false and dangerous as an instruction: entries QUEUE when no
            # verdict is available, and only ALLOWED / UNAVAILABLE may proceed (see
            # the explicit allowlist below). A missed entry costs nothing; an
            # unqualified one costs capital. The halt is kept non-silent by
            # `drop_after` plus the ENTRY DROPPED Telegram, not by entering.
            #
            # A signal the book cannot fund is not a judgement call, so it does not
            # reach an agent — but ONLY when nothing is in flight for it yet. A
            # verdict already being decided is still honoured: entering behind a
            # PENDING vet would let a VETO land on a position that is already open.
            # Nothing is dropped either way — the row stays where it is and every
            # later cycle re-asks, so the moment a slot frees the vet is requested
            # normally.
            vet_skipped = (cfg.VET_ENABLED and capital_refused
                           and vet_mod.vet_state(store.find(trade['id'])) is None)
            if vet_skipped:
                logger.info("VET NOT REQUESTED #%d %s — capital refuses the "
                            "signal; no agent slot spent", trade['id'], stock)
            if cfg.VET_ENABLED and not vet_skipped:
                state = vet_mod.vet_state(store.find(trade['id']))
                if state == vet_mod.QUEUED:
                    # Retry with the book we just re-quoted. promote_queued is a
                    # CAS, so overlapping drainers cannot double-spawn; a loser
                    # simply waits for the next cycle.
                    #
                    # Wrapped: this is the only vet call on this path that was
                    # bare, and `_mutate` can raise LockTimeout. Unwrapped it
                    # propagated out of check_watching and cost EVERY signal after
                    # this one its cycle — including their drift-cancel checks.
                    try:
                        vet_mod.promote_queued(
                            store, trade['id'],
                            context=_vet_context(store, trade, analysis,
                                                 gap_pct, kite, bcs))
                    except Exception as e:
                        logger.error("Queue drain failed for #%d: %s — stays "
                                     "queued, retried next cycle", trade['id'], e)
                    continue
                if state is None:
                    try:
                        vet_mod.request_entry_vet(
                            store, trade['id'],
                            context=_vet_context(store, trade, analysis,
                                                 gap_pct, kite, bcs))
                    except ValueError as e:
                        # The locked re-check saw a state this cache missed —
                        # already requested or already SETTLED (possibly a veto).
                        # Never enter on a guess; the next cycle reads the real
                        # state (request_entry_vet's refresh updated the cache).
                        logger.warning("VET request refused for #%d: %s — "
                                       "re-reading next cycle", trade['id'], e)
                        continue
                    except Exception as e:
                        # Infra failure (lock timeout, IO) while REQUESTING. This
                        # handler predates the fail-closed inversion and used to
                        # enter unvetted here — which re-opened the hole the queue
                        # exists to close, and did it on the likeliest refusal
                        # path: `request_entry_vet` calls `queue_entry_vet` when a
                        # spawn is refused, so a LockTimeout in THAT write landed
                        # right here and turned a refused slot into a live
                        # position.
                        #
                        # A missed entry costs nothing; an unqualified one costs
                        # capital. So park it and try again next cycle. The queue's
                        # own drop_after still bounds the wait, and the sweep still
                        # announces a give-up, so this cannot become a silent halt.
                        logger.error("VET request failed for #%d: %s — QUEUED, not "
                                     "entered", trade['id'], e)
                        try:
                            vet_mod.queue_entry_vet(store, trade['id'],
                                                    'vet request failed: %s' % e)
                        except Exception as e2:
                            # Even the parking write failed. Leave it `triggered`
                            # with no marker: the next cycle re-reads state None
                            # and requests afresh. Entering is never the fallback.
                            logger.warning(
                                "could not queue #%d after a failed request: %s — "
                                "left triggered, retried next cycle",
                                trade['id'], e2)
                        continue
                    else:
                        continue          # wait for the verdict
                elif state == vet_mod.PENDING:
                    continue              # still deciding; expire_stale bounds it
                elif state == vet_mod.VETOED:
                    # DEFENCE IN DEPTH, and believed UNREACHABLE today: nothing
                    # between the fast-path read above and this one refreshes the
                    # store cache, and a verdict that settles mid-request comes
                    # back as request_entry_vet's ValueError instead. Kept because
                    # it is the safe duplicate of the fast-path (same action, same
                    # shadow) and any future cache refresh in between would make it
                    # live. Stated as a belief, not a fact: an over-confident
                    # reachability comment is what made the veto shadow dead code
                    # in the first place.
                    logger.info("VETOED #%d %s — no entry", trade['id'], stock)
                    try:
                        outcomes_mod.open_shadow(store, trade['id'],
                                                 entry_spot=price)
                    except Exception as e:
                        logger.error("Veto shadow failed for #%d: %s",
                                     trade['id'], e)
                    continue
                elif state != vet_mod.ALLOWED:
                    # EXPLICIT allowlist, not a bare fall-through. Entering was the
                    # DEFAULT for any state without an `elif` above, so adding a
                    # state to the machine silently meant "enter unvetted" — which
                    # is exactly what STARVED must never do.
                    #
                    # M6 (2026-08-29): UNAVAILABLE CAME OFF THIS ALLOWLIST.
                    #
                    # It means "the vet request could not even be made" — a lock
                    # timeout, an IO error, no agent slot. Letting it enter made an
                    # entry that was never reviewed indistinguishable from one that
                    # was reviewed and cleared, and that contradicts the standing
                    # rule this whole layer exists to serve: ENTRIES FAIL CLOSED.
                    # A missed entry costs nothing; an unqualified one costs
                    # capital (`feedback_no_rush_to_enter`).
                    #
                    # **EXITS ARE THE OPPOSITE and must stay that way.**
                    # `vet.exit_gate` returns 'proceed' on UNAVAILABLE, on purpose:
                    # there the bounded outcome is ACTING, and an exit deadline
                    # that depends on an LLM being reachable is how a stop stops
                    # working. Same word, opposite safe direction, because the two
                    # sides have opposite asymmetries. Do not "unify" them.
                    #
                    # This branch was unreachable before today — `mark_unavailable`
                    # (`zebra/vet.py`) has never had a caller, which is why the
                    # contradiction survived. It is now safe to wire in.
                    logger.info("NO ENTRY #%d %s — vet state %r is not an entry "
                                "state", trade['id'], stock, state)
                    continue
                # Only ALLOWED falls through and enters below.

            # PAPER mode: auto-record the entry FIRST, then alert — so the ENTER
            # alert only goes out for a position that actually opened. If the fill
            # is rejected we leave the signal in 'triggered' (it self-heals via the
            # drift/stale-cancel checks next cycle); we deliberately do NOT cancel
            # here, because a 'cancelled' record isn't deduped by the scanner and
            # would be re-added + re-alerted every scan (alert churn).
            # ── The only entry path: one BCS record, no shadow ──────────────
            # The `if cfg.ENTRY_STRUCTURE == 'bcs':` that used to wrap this, and
            # the ~120-line back-ratio entry branch that followed it, were removed
            # on 2026-08-27 when the owner decommissioned the back ratio. That
            # branch is the one logged as F2: it ignored `capital_refused` and
            # would still have rendered a full order ticket for a signal the book
            # had already refused.
            #
            # The strike analyzer still runs because it owns expiry selection, lot
            # size and the ATM book — but it no longer prices anything else.
            built = _enter_as_bcs(store, kite, trade, analysis, price,
                                  bcs=bcs, dry_run=dry_run)
            if built is None:
                continue
            bcs, fresh = built
            # `fresh` is None in LIVE mode when nothing auto-entered -- the
            # default, since `auto_entry` is off; `_auto_enter_bcs` inside
            # `_enter_as_bcs` can also fill it when auto-entry is armed and
            # the fill succeeds. Either way the alert still goes out (as the
            # order ticket, or as a record of what was just filled), and
            # _alerts_enabled always returns True when paper mode is off for
            # exactly that reason.
            target = fresh or trade
            if not _alerts_enabled(target):
                logger.info("ENTER alert suppressed for #%d %s "
                            "(alert_structures=%s)", trade['id'], stock,
                            cfg.ALERT_STRUCTURES)
                continue
            if capital_refused:
                # ONE signal, ONE alert -- and this one is not an order.
                # Rendering the ticket and appending "DO NOT ENTER" is what
                # #449 did: click-copy symbols, a debit, a lot size and a
                # Claude tick, contradicted by its own last line. Whatever a
                # reader acts on there, the message was wrong.
                _send_capital_refused_alert(store, target, bcs, cap_plan,
                                            stock, dry_run=dry_run)
                continue
            msg = _format_bcs_enter_alert(target, analysis, bcs)
            if cfg.VET_ENABLED:
                msg += _vet_line(store.find(trade['id']) or target)
            # Funds LAST, closest to the click-copy symbols. Quantity from the
            # BCS's own lot_size: the ticket and the margin must price the same
            # order. (This used to sit BELOW the retired branch's `continue`, so
            # under the BCS pipeline it ran exactly never — the wire-into-the-live-
            # path shape, pinned by a test.)
            # A refused plan must never render as a size. In PAPER the position
            # HAS been opened at one lot while the plan says zero, so
            # `_size_line`'s "DO NOT ENTER" would contradict the entry that just
            # happened — #449's incoherence, arrived at from the other side.
            msg += (_shadow_size_line(capital_shadow) if capital_shadow
                    else _size_line(cap_plan))
            msg += _funds_line(kite, bcs, bcs.get('lot_size') or 0)
            _send_enter_alert(store, trade, msg, stock, dry_run=dry_run)
        except Exception as e:
            logger.error(
                "WATCHING #%s %s raised (%s) — skipping this signal; the "
                "rest of the watchlist is still checked this cycle.",
                trade.get('id'), trade.get('stock'), e, exc_info=True)
            continue


# ── Entered → TP/SL/Time ─────────────────────────────────────────────────

def _long_multiplier(trade: dict) -> int:
    """2 long legs for zebra, 1 for a BCS shadow."""
    return 1 if trade.get('structure') == 'bcs' else 2


def _intrinsic_floor(trade: dict, spot: float) -> Optional[float]:
    """No-arbitrage floor for the structure value. See `common.spread_valuation`.

    A thin delegate since 2026-08-30. This and `bcs.spread_intrinsic_floor`
    were one arithmetic implemented twice, and the money path's version was
    the better one: it derives CE/PE from the leg SYMBOLS rather than from a
    `direction` label (B21 -- call arithmetic makes the floor inert for a bear
    put spread, which holds the higher strike long), and it builds the short
    allowance from the entry price less its intrinsic at entry.

    The allowance ladder no longer ends in `0.3 * debit`. B17 measured that
    fallback as TIGHTER than the truth on the real ICICI record -- 4.07 against
    7.65 -- so a healthy book fell below the floor and every valuation was
    refused for the rest of the session, taking SL_SPREAD, SL_TRAIL and the
    trail dark with it. With no basis for an allowance there is now NO FLOOR,
    which is a known gap rather than a guard that refuses healthy books.

    `_long_multiplier` is still consulted here: the back ratio's two long legs
    are this book's history and the shared module must not have to know about
    a structure that no longer trades.
    """
    return spread_valuation.intrinsic_floor(
        trade, spot, long_multiplier=_long_multiplier(trade))


def _structure_quote(kite, trade: dict, spot: Optional[float] = None) -> dict:
    """Fetch both legs and compute the structure value + per-leg reliability.

    Returns {'mid': float|None, 'reliable': bool, 'reason': str|None}.

    `mid` is the per-share structure value (mult*long_mid - short_mid,
    mult = 2 zebra / 1 BCS), clamped to the intrinsic floor when `spot` is
    given — the ABB #242 no-arbitrage guard against a junk ITM quote booking a
    phantom -50% exit. `mid` is None only when a leg has no usable quote at all.

    `reliable` is False when EITHER leg's top-of-book fails the width / crossed
    / one-sided test (2026-07-24 NHPC incident) — the caller must FREEZE any
    value-based DEBIT-SL confirmation on an unreliable read, never fire on it.
    Reliability is orthogonal to the floor clamp: the floor bounds the mid,
    reliability governs whether the mid may drive an exit decision this cycle.
    """
    try:
        long_q = strikes_mod._quote_option(kite, trade['long_symbol'])
        short_q = strikes_mod._quote_option(kite, trade['short_symbol'])
    except Exception as e:
        logger.debug("Quote fail for #%d: %s", trade['id'], e)
        return {'mid': None, 'reliable': False, 'reason': 'quote_error'}

    if long_q['mid'] <= 0 or short_q['mid'] <= 0:
        return {'mid': None, 'reliable': False, 'reason': 'no_quote'}

    reason = None
    if not long_q.get('reliable', True):
        reason = f"long {long_q.get('unreliable_reason')}"
    elif not short_q.get('reliable', True):
        reason = f"short {short_q.get('unreliable_reason')}"
    reliable = reason is None

    # Value it on the basis the position was ENTERED on, never on whatever the
    # current default is. Basis is a property of the TRADE, stamped at entry:
    # flipping a live position from mid to fill mid-flight would move its
    # debit-SL and trail levels under it, and would make its round-trip P&L a
    # comparison between two different price conventions.
    #
    # 'fill' = what closing would actually pay — sell the long at the BID, buy
    # the short back at the ASK. Entry pays the spread and exit pays it again;
    # a mid-mid book records neither, which is why the paper P&L read
    # optimistic at BOTH ends and modelled zero round-trip cost.
    if trade.get('pricing_basis') == 'fill':
        long_px, short_px = long_q.get('bid') or 0, short_q.get('ask') or 0
        if long_px <= 0 or short_px <= 0:
            # One-sided book: there is no price this position could be closed
            # at, so there is no honest value to report. Same answer as a dead
            # quote — the caller freezes its confirm counters rather than
            # acting on a number it cannot transact at.
            return {'mid': None, 'reliable': False, 'reason': 'no_two_way_book'}
    else:
        long_px, short_px = long_q['mid'], short_q['mid']
    mid = round(_long_multiplier(trade) * long_px - short_px, 2)
    legs = {'long': _leg_book(trade.get('long_symbol'), long_q),
            'short': _leg_book(trade.get('short_symbol'), short_q)}

    # ── Bounds vs heuristics: clamp the first, reject the second ─────────
    #
    # (1) MATHEMATICAL BOUNDS — clamp. A debit structure is never worth less
    # than zero, because letting it expire is always available and costs
    # nothing; so a book quoting -0.05 (long bid 0.55, short ask 0.60 — an
    # ordinary shape once a spread is worthless) means "worth about nothing",
    # not "unpriceable". Zero is achievable, so booking it is honest, and
    # booking the loss beats stranding the position until expiry. A vertical
    # is likewise never worth more than its width. PIIND #50 booked
    # exit_debit -30.04 against a debit of 242.11 — -112.4% on a -100%-capped
    # structure — because neither bound existed anywhere.
    bounded = mid
    if bounded < 0:
        bounded = 0.0
    if trade.get('structure') == 'bcs':
        try:
            w = float(trade.get('width') or 0)
        except (TypeError, ValueError):
            w = 0.0
        if w > 0 and bounded > w:
            bounded = w
    if bounded != mid:
        logger.warning(
            "VALUE BOUND #%d %s: %.2f -> %.2f (structure cannot be worth "
            "that) — long %s/%s short %s/%s",
            trade['id'], trade['stock'], mid, bounded,
            long_q.get('bid'), long_q.get('ask'),
            short_q.get('bid'), short_q.get('ask'))
        mid = round(bounded, 2)

    # (2) FAIR-VALUE HEURISTIC — reject, never clamp. The intrinsic floor is
    # an ESTIMATE of what the structure ought to be worth, not a price anyone
    # offered, so pulling a quote up to it invents a fill exactly the way the
    # garbage book did. The old code clamped, and on the fill basis that was
    # lifting honest valuations UP (1.8 booked as 2.5), re-introducing the
    # optimism fill pricing was shipped to remove. A rejected quote defers the
    # trade to the next poll — the same answer every other unusable book gets.
    if spot is not None and spot > 0:
        floor = _intrinsic_floor(trade, spot)
        if floor is not None and mid < floor:
            bad = (f'value {mid:.2f} below intrinsic floor {floor:.2f} '
                   f'at spot {spot:.2f}')
            logger.warning(
                "QUOTE REJECT #%d %s: %s — long %s/%s short %s/%s (an "
                "estimate is not a price; deferring, nothing booked)",
                trade['id'], trade['stock'], bad,
                long_q.get('bid'), long_q.get('ask'),
                short_q.get('bid'), short_q.get('ask'))
            return {'mid': None, 'reliable': False,
                    'reason': 'below_intrinsic_floor',
                    'legs': legs, 'floored': False, 'rejected': bad}

    return {'mid': mid, 'reliable': reliable, 'reason': reason,
            # PER-LEG BOOK. VETTING.md tells the exit agent to judge "depth at
            # touch and the spread as a % of mid", and this dict used to carry
            # only mid/reliable/reason — so the agent was asked to judge the one
            # thing it could not see, exactly the defect the ENTRY context had
            # already been fixed for.
            'legs': legs,
            # Kept for schema stability with every consumer of this dict.
            # Always False now: a quote that would once have been CLAMPED to
            # the intrinsic floor is rejected outright above, because booking a
            # clamped number invents a fill just as surely as booking the
            # garbage did.
            'floored': False}


def _leg_book(symbol, q: dict) -> dict:
    """One leg's top-of-book, in the shape the vetting agent is asked to judge."""
    bid, ask, mid_ = q.get('bid'), q.get('ask'), q.get('mid')
    spread_pct = None
    if bid is not None and ask is not None and mid_:
        spread_pct = round((ask - bid) / mid_ * 100, 1)
    return {'symbol': symbol, 'bid': bid, 'ask': ask, 'mid': mid_,
            'oi': q.get('oi'), 'last': q.get('last'),
            'spread_pct': spread_pct,
            # SIZE AT THE TOUCH, carried since 2026-08-30. `_quote_option` has
            # returned these all along and this function dropped them, so the
            # forensic POLL line, the persisted `exit_legs` and the vetting
            # agent's context all described a book without saying how much of
            # it there was. Depth is the only limit on position size that is a
            # fact about the market rather than a number we chose, and it is
            # the one the book never kept.
            'bid_qty': q.get('bid_qty'), 'ask_qty': q.get('ask_qty'),
            'reliable': q.get('reliable', True),
            'unreliable_reason': q.get('unreliable_reason')}


def _structure_value(kite, trade: dict, spot: Optional[float] = None
                     ) -> Optional[float]:
    """Structure value per share (floor-clamped). Thin wrapper over
    _structure_quote for callers that only need the scalar mid (report.py,
    manual close). Returns None if any leg has no usable quote."""
    return _structure_quote(kite, trade, spot)['mid']


# Backward-compat alias (report.py / manual close path import this name).
def _quote_zebra_value(kite, trade: dict) -> Optional[float]:
    return _structure_value(kite, trade)


def _sessions_left(today, expiry) -> int:
    """Trading sessions from `today` (exclusive) to `expiry` (inclusive).

    **Holidays included since 2026-08-29 (M4/M10)**, through the SAME module
    the order engine uses (`common/nse_holidays.py`). Two counters that
    disagreed about how many sessions remain would put the two engines
    managing one position on different close dates — the copy-nobody-opens
    shape, applied to a date.

    It used to be weekdays-only, and that error was not neutral: a holiday
    inside the window made it an OVER-estimate, so the close fired LATER,
    while NSE moves each delivery-margin tranche EARLIER around a holiday.
    Both errors pointed at "still holding when the ramp starts".

    Returns 0 on or after expiry day.
    """
    if expiry <= today:
        return 0
    return nse_holidays.sessions_between(
        today, expiry, warn=lambda m: logger.warning('%s', m))


def _flush_mfe(store: ZebraStore, pending: dict) -> None:
    """Write the cycle's accumulated peak state and clear the accumulator.

    Called before any exit books and once at the end of the cycle. It must run
    BEFORE mark_exited: `_merge` gives disk the tie on equal versions, so a
    patch still sitting in this dict would be discarded by mark_exited's own
    in-lock refresh — and mark_exited is the write that carries these fields to
    Drive. Losing them there loses exactly the trades the give-back question is
    about.
    """
    if not pending:
        return
    try:
        store.apply_mfe(dict(pending))
    except Exception as e:
        # Measurement must never be able to block an exit.
        logger.warning("MFE flush failed (%d trades): %s", len(pending), e)
    pending.clear()


def _paper_auto_close(store: ZebraStore, trade: dict, mid: Optional[float],
                       reason: str, spot: Optional[float] = None,
                       pending_mfe: Optional[dict] = None,
                       reliable: bool = True,
                       release_flag: bool = True,
                       legs: Optional[dict] = None) -> Optional[dict]:
    """Auto-close a paper trade at current structure mid. Returns the updated
    trade dict (with pnl/pnl_pct) or None if close failed.

    `spot` is the live underlying LTP at exit — recorded for post-trade
    spot-movement analysis. P&L itself is driven by `mid`, not spot.
    """
    if reason in EXTERNALLY_MANAGED_EXITS and _exits_external(trade):
        # THE BACKSTOP, and the reason it lives here rather than only at the
        # call sites: every close in this module funnels through this function,
        # so a future exit branch cannot forget it. The cascade in
        # `check_entered` stands down earlier to save the wasted vet and alert;
        # this catches anything that gets past it.
        #
        # Booking a paper exit on a position another engine holds for real is
        # the worst outcome available here -- the record leaves `get_entered()`,
        # so the monitor's `get_open_trades()` stops returning it, and a LIVE
        # position goes unwatched with nothing anywhere reporting a problem.
        #
        # REASON-SCOPED, and it stays that way. `expiry` is absent from the set
        # by design -- see the constant's docstring and
        # `test_the_terminal_expiry_settle_is_never_declined`. It was audited
        # again on 2026-08-27 (N2) on the theory that the omission was a hole,
        # and it is not one: `_settle_if_expired` is the ONLY caller that
        # passes 'expiry', and it fires strictly PAST expiry (`today > exp`),
        # at which point the options no longer exist and there is nothing for
        # the order path to close. Declining there would strand the record at
        # `entered` forever and ban its stock from the scanner, in exchange for
        # protecting a position that cannot be traded either way.
        # `test_only_the_terminal_settle_may_close_on_expiry` pins that single
        # call site, because the omission is only safe while it stays single.
        logger.warning(
            "PAPER close DECLINED #%d %s reason=%s: exits are managed by "
            "spread_monitor for this trade - booking it here would hide a "
            "live position from the engine that owns it",
            trade['id'], trade['stock'], reason)
        return None
    if reason in EXTERNALLY_MANAGED_EXITS and not is_paper_record(trade):
        # THE RECORD DECIDES, AND ONLY THE RECORD.
        #
        # `if not (cfg.PAPER_MODE or is_paper_record(trade))` stood here until
        # 2026-08-29, and that `or` was the last reachable TWO-ENGINE state in
        # the system. A record with `paper: False` in a store running
        # `paper_mode: true` -- exactly what `zebra enter` files for a
        # HAND-PLACED LIVE TRADE, the first live-money action in the arming
        # order -- was bookable BOTH here at the structure mid AND by the order
        # path at the broker. The CLAUDE.md arming table blamed that state on
        # the mode switch. The mode switch was never the cause.
        # `common/arming.py` now derives that whole table from one invariant,
        # and this predicate is half of it.
        #
        # Booking at MID is licensed by one fact and one only: no broker was
        # ever involved, so there is no fill to contradict. That is a property
        # of the POSITION, never of a config file, and a mid close of a record
        # with real legs is a fiction whatever mode the process is in.
        #
        # Absence still means paper (`is_paper_record`), so no legacy record
        # loses its booking engine here.
        #
        # SECOND, not first, and reason-scoped like the backstop above. Both
        # orderings decline the same set of closes; this one keeps BOTH guards
        # observable. `_exits_external` implies `not is_paper_record`, so with
        # this test first the backstop could never fire, and a guard nobody can
        # watch fail is one nobody can know works
        # (`feedback_a_second_guard_you_cannot_observe_is_decorative`). The
        # backstop's message names the engine that owns the trade, which is
        # what the reader needs; this one fires for the cases it does not
        # cover -- a live record outside the cohort, or inside it with the
        # switch off.
        #
        # The reason scope carries the `expiry` exemption across for the same
        # reason the backstop has it, argued in full at EXTERNALLY_MANAGED_EXITS:
        # `_settle_if_expired` fires strictly PAST expiry, when the contracts
        # have auto-exercised and there is nothing left for any order path to
        # close. Declining there protects nothing and strands the record at
        # `entered` for good, which also bans its stock from the scanner.
        #
        # A PAPER RECORD KEEPS ITS BOOKING ENGINE AFTER THE SWITCH FLIPS.
        #
        # This line once read `if not cfg.PAPER_MODE`, which disabled paper
        # booking for the WHOLE store the moment the mode changed. The owner's
        # decision of 2026-08-27 is that paper keeps running through go-live
        # and every paper position resolves NATURALLY -- "not the 8 cohort
        # positions, not any other" -- so a global gate would strand every
        # open paper record on the day of the flip: no auto-close here, and
        # (by the adapter's filter) no live engine either. Two engines, zero
        # coverage, silently.
        #
        # Gating on the RECORD rather than the mode is the smaller of the two
        # available fixes and the one that stays true afterwards: what makes a
        # close bookable at mid is that no broker was ever involved, which is
        # a property of the position, not of a config file. The alternative --
        # routing paper records back to zebra from inside the adapter -- puts
        # a paper-booking branch inside the only module in the fleet that can
        # place a real order, which is precisely where this codebase has twice
        # lost money.
        return None
    if trade.get('status') != 'entered':
        return None  # already closed by an earlier trigger this cycle
    if mid is None or not reliable:
        # Never book a price we could not have transacted at. `mid is None` is
        # the obvious case; `not reliable` is the one that was missing — the
        # reliability freeze covered DEBIT-SL and TRAIL only, so TP, SPOT-SL
        # and TIME still booked whatever a crossed or one-sided book said, and
        # in PAPER the booked number IS the result.
        #
        # The alert has already claimed its consume-once flag by now (claiming
        # before sending is deliberate — two processes must not both send), so
        # release it: otherwise the exit is announced, never booked, and never
        # allowed to fire again, leaving the position open with its own trigger
        # permanently disarmed.
        #
        # TIME opts out (`release_flag=False`). Its alert is a NAG about the
        # calendar, not a claim about a price, so it stands whatever the book
        # is doing — and it already re-arms daily. Releasing it would re-nag
        # every 5 minutes.
        if release_flag:
            # Guarded like the failure path below, and for the same reason. A
            # bare call here could raise LockTimeout out of the exit branch with
            # the consume-once flag ALREADY claimed: the exit is announced on
            # Telegram, never booked, and that exit kind is disarmed for the
            # position permanently — a capped loss quietly becoming a maximum
            # one. The careful handling existed one path over and not on this
            # one.
            try:
                store.clear_alert_flag(trade['id'], reason)
            except Exception as e2:
                logger.error("Could not release the %s flag on #%d after a "
                             "deferred close: %s — that exit is disarmed until "
                             "the flag is cleared", reason, trade['id'], e2)
        logger.warning(
            "PAPER close DEFERRED #%d %s reason=%s: %s — flag released, will "
            "re-fire when the book is usable",
            trade['id'], trade['stock'], reason,
            'no quote' if mid is None else 'book not reliable')
        return None
    if pending_mfe is not None:
        _flush_mfe(store, pending_mfe)
    try:
        updated = store.mark_exited(
            trade['id'],
            spot if spot is not None else trade.get('entry_spot', 0),
            mid,
            f'paper:{reason}',
            exit_legs=legs,
        )
        # The BOOK on the line, not just the derived value. An option book is
        # unreconstructable after the fact, so a post-mortem holding only
        # "exit_debit 11.29" can never answer the question that matters --
        # was that a price, or was it garbage?
        lg = legs or {}
        lo, sh = lg.get('long') or {}, lg.get('short') or {}
        logger.info(
            "PAPER auto-closed #%d %s reason=%s mid=%s P&L=Rs%.0f (%.1f%%) "
            "spot=%s | long %s %s/%s oi=%s | short %s %s/%s oi=%s",
            trade['id'], trade['stock'], reason,
            f'{mid:.2f}' if mid is not None else 'NA',
            updated.get('pnl', 0), updated.get('pnl_pct', 0),
            f'{spot:.2f}' if spot is not None else 'NA',
            lo.get('symbol'), lo.get('bid'), lo.get('ask'), lo.get('oi'),
            sh.get('symbol'), sh.get('bid'), sh.get('ask'), sh.get('oi'))
        # WHAT THE LATENCY COST, measured on the trade it cost it on.
        #
        # Booked at `mid` — the book observed HERE, never the touch price
        # (`feedback_trigger_is_not_the_fill`). This records the give-back so
        # the price of the ~5-minute lag is a number rather than an argument;
        # it is the evidence M12 is gated on. Measurement only: it runs AFTER
        # the exit is booked and is wrapped whole, because an accounting stamp
        # that can raise is a new way to lose an exit that already happened.
        if reason == 'tp':
            try:
                gap = tp_touch_to_fill(trade, spot,
                                       rising=trade.get('direction') == 'CE')
                if gap:
                    store.update_trade_fields(trade['id'], **gap)
                    updated.update(gap)
                    logger.info(
                        "TP touch->fill #%d %s: %ss later, spot moved %s "
                        "(%s%%) from the touch",
                        trade['id'], trade['stock'],
                        gap.get('tp_touch_to_exit_sec'),
                        gap.get('tp_touch_spot_move'),
                        gap.get('tp_touch_spot_move_pct'))
            except Exception as e:
                logger.warning(
                    "TP touch->fill measurement failed for #%d: %s — the exit "
                    "IS booked; only the latency stamp is missing",
                    trade['id'], e)
        # Mutate the in-loop dict so subsequent checks in this cycle skip it
        trade['status'] = 'exited'
        return updated
    except Exception as e:
        # Broad on purpose. Only ValueError was caught, so a LockTimeout or an
        # OSError from the store propagated out of the exit branch with the
        # consume-once flag ALREADY claimed — announcing an exit on Telegram,
        # never booking it, and permanently disarming that exit kind for the
        # position. Releasing the flag is what makes the failure retryable
        # instead of terminal.
        if release_flag:
            try:
                store.clear_alert_flag(trade['id'], reason)
            except Exception as e2:
                logger.error("Could not release the %s flag on #%d after a "
                             "failed close: %s", reason, trade['id'], e2)
        logger.error("PAPER auto-close FAILED for #%d %s reason=%s: %s "
                     "(flag released, will retry)",
                     trade['id'], trade['stock'], reason, e, exc_info=True)
        return None


def _value_triggers_live(now: Optional[datetime] = None) -> bool:
    """Are the BOOK-driven triggers (DEBIT-SL, TRAIL) allowed to fire yet?

    False for the first VALUE_TRIGGER_OPEN_BUFFER_SEC of the session. Both
    incidents that cost real money happened at the open on the first prints of
    the day, and the live monitor has refused to act before 09:30 ever since.
    Spot-driven TP and the TIME nag are deliberately NOT gated: spot at the
    open is real trades, and the calendar does not care what the book is doing.
    """
    now = now or datetime.now(IST)
    open_h, open_m = cfg.MARKET_OPEN
    open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    return (now - open_dt).total_seconds() >= cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC


def _spot_corroborates(store: ZebraStore, trade: dict, spot: float,
                       value: Optional[float], reliable: bool,
                       now: Optional[float] = None) -> tuple:
    """Does the underlying explain this collapse in structure value?

    Returns (ok, reason). ok=True whenever it cannot prove otherwise — no
    reference yet, a stale one, or a RISE rather than a collapse. It vetoes one
    specific shape: value falling off a cliff while spot barely moves. That is
    the NHPC signature, and no real repricing of a vertical spread can produce
    it — the structure's value is a function of the underlying.

    VETO-ONLY. It can refuse an exit the book asked for; it can never ask for
    one. That polarity is the whole reason a second source is safe to add: the
    measured cost of a spot TRIGGER is 31 of 78 winners, the cost of a spot
    VETO is nothing.

    The reference only advances on RELIABLE readings, so a garbage read can
    never become the baseline that a later genuine move is judged against.

    Returns (ok, reason, patch). The advanced reference comes back as a PATCH
    rather than being written here: this runs once per open position per poll,
    and writing it inline would put a store write per trade per cycle back
    into a loop that was deliberately reduced to one batched write.
    """
    if not cfg.SPOT_VETO_ENABLED or value is None or not spot or spot <= 0:
        return True, '', None
    now = time.time() if now is None else now
    ref = store.corroboration_ref(trade['id'])
    ok, reason = True, ''
    if (ref['value'] is not None and ref['spot']
            and now - ref['t'] <= cfg.CORROBORATION_STALE_SEC
            and ref['value'] > 0):
        drop = (ref['value'] - value) / ref['value']
        spot_move = abs(spot - ref['spot']) / ref['spot']
        if drop >= cfg.SPREAD_COLLAPSE_PCT and spot_move < cfg.SPOT_MOVE_MIN_PCT:
            ok = False
            reason = (f"uncorroborated collapse: value {ref['value']:.2f} -> "
                      f"{value:.2f} (-{drop*100:.0f}%) on a "
                      f"{spot_move*100:.2f}% spot move")
    # Advance the reference only when it would actually differ: a reading
    # identical to the stored one carries no new information, and rewriting it
    # would put a store write back into every cycle — the batched-write
    # optimisation exists because 24 open positions were rewriting ~1 MB
    # forty-eight times a cycle on a Pi that also runs the live-money monitor.
    # It is refreshed before it can go stale, so the baseline is never older
    # than half the staleness window. In a moving market spot changes most
    # polls, so this WILL usually write — one small batched write per cycle is
    # the accepted price of having a second source at all.
    patch = None
    if ok and reliable:
        moved = (ref['spot'] != spot or ref['value'] != value)
        ageing = (now - ref['t']) >= cfg.CORROBORATION_STALE_SEC / 2
        if ref['value'] is None or moved or ageing:
            patch = {'corrob_spot': float(spot), 'corrob_value': float(value),
                     'corrob_t': now}
    return ok, reason, patch


def _expire_if_ancient(store: ZebraStore, trade: dict) -> bool:
    """Cancel a watching/triggered signal that has outlived its usefulness.

    Every other exit from the watchlist is driven by the GAP, which needs a
    price. A symbol that stops quoting — suspended, renamed, delisted — never
    updates its gap, so it can never drift-cancel or stale-cancel, and it sits
    in the watchlist permanently: one of 25 slots, and a dedup entry that bans
    its own stock from ever signalling again.
    """
    raw = trade.get('signal_date') or trade.get('entry_date')
    if not raw:
        return False
    try:
        age = (datetime.now(IST).date()
               - datetime.strptime(str(raw)[:10], '%Y-%m-%d').date()).days
    except Exception:
        return False
    if age < cfg.WATCH_MAX_AGE_DAYS:
        return False
    try:
        store.cancel(trade['id'],
                     f'expired: {age}d in {trade.get("status")} with no '
                     f'tradeable price')
        logger.warning(
            "EXPIRED #%d %s: %d days in '%s' and still unpriceable — "
            "releasing the watchlist slot and the dedup hold",
            trade['id'], trade['stock'], age, trade.get('status'))
        return True
    except ValueError:
        return False


# Kite's quote family (/quote, /quote/ltp, /quote/ohlc) is capped at 1 req/s
# and a 429 opens a 10-second sliding cooldown that every further request
# EXTENDS. Exactly ONE call in this file is allowed to wait that cooldown out:
# the spot fetch for open positions, on which TP, the spot veto and the expiry
# nag all depend. Everything discretionary fails fast, so it can never queue
# ahead of this one.
_LTP_RETRIES_EXIT_PATH = 2
_RATE_LIMIT_COOLDOWN_SEC = 10.0


def _spot_ltps(kite, stocks: list):
    """Spot for every open position, plus WHY the fetch failed.

    Returns ``(prices, error)``. Two things this does that a bare `get_ltp`
    cannot, both of them lessons from 2026-08-27:

    1. **It carries the cause.** `get_ltp` returns a bare dict — it is the seam
       the test suite substitutes and it must stay that shape — so the reason
       is read from `scanner.last_ltp_error()`, cleared immediately before the
       call so a stale cause can never be reported as a fresh one. A
       substituted `get_ltp` simply leaves it None, and the blind alert then
       says "no exception was captured" rather than inventing one.
    2. **It waits out a rate limit, and only a rate limit.** A 429 clears in
       ~10 seconds; a dead token does not, and Kite throttles sustained 403s
       into a 429 lockout, so retrying an auth failure would both fail and
       disguise itself as the other thing.
    """
    scanner_mod.clear_ltp_error()
    ltps = get_ltp(kite, stocks)
    err = scanner_mod.last_ltp_error()
    priced = any((ltps.get(s) or 0) > 0 for s in stocks)
    attempt = 0
    while (not priced and kite_errors.is_rate_limit(err)
           and attempt < _LTP_RETRIES_EXIT_PATH):
        attempt += 1
        logger.warning(
            "SPOT fetch for %d open position(s) was RATE-LIMITED (429) — "
            "waiting %.0fs for Kite's sliding cooldown, retry %d/%d. Exit "
            "monitoring is blind until this succeeds.",
            len(stocks), _RATE_LIMIT_COOLDOWN_SEC, attempt,
            _LTP_RETRIES_EXIT_PATH)
        time.sleep(_RATE_LIMIT_COOLDOWN_SEC)
        scanner_mod.clear_ltp_error()
        ltps = get_ltp(kite, stocks)
        err = scanner_mod.last_ltp_error()
        priced = any((ltps.get(s) or 0) > 0 for s in stocks)
    return ltps, err


def _alert_monitoring_blind(n_open: int, stocks: list,
                            error: Optional[BaseException] = None,
                            dry_run: bool = False) -> None:
    """One Telegram per CAUSE per day when NO open position can be priced.

    Deliberately not a per-trade flag: the condition is about the data feed,
    not about any one position, and 24 identical messages would train the
    reader to ignore the one that matters. The marker is a file rather than a
    module global because the cron process exits between cycles, so an
    in-memory guard would re-alert every five minutes.

    **It reports what happened; it does not guess.** Until 2026-08-27 this
    said "Most likely the Kite access token has expired" unconditionally. On
    that day it fired at 14:40 with the token generated at 08:45:05 the same
    morning and the real cause — `Too many requests`, a rate limit — logged on
    the line immediately above. The alert would have sent the owner to
    regenerate a healthy token while the actual fault continued. So:

    * the exception is CARRIED here (`get_ltp_ex`) rather than swallowed at
      the fetch, and classified by `common.kite_errors`
    * expiry is asserted only when the token file on disk supports it — its
      `generated_at` is READ, never assumed
    * the dedup marker is keyed on (date, CAUSE), so a rate limit in the
      morning does not silence a genuine token death in the afternoon. That is
      the one way "once a day" could have turned this alert into the thing it
      is meant to prevent.
    """
    diag = kite_errors.diagnose(error, cfg.KITE_TOKEN_FILE)
    cause = diag['cause']
    today = datetime.now(IST).strftime('%Y-%m-%d')
    marker = cfg.LOG_DIR / 'zebra_blind_alert.json'
    try:
        prev = json.loads(marker.read_text()) if marker.exists() else {}
    except Exception:
        prev = {}
    seen = (prev.get('date'), prev.get('cause'))

    logger.error(
        "MONITORING BLIND: no LTP for ANY of %d open position(s) (%s) — "
        "TP, spot SL and the expiry nag are all dark this cycle. "
        "CAUSE=%s: %s | %s", n_open, ', '.join(sorted(stocks)[:12]),
        cause.upper(), diag['error'],
        (diag['token'] or {}).get('summary', 'token not checked'))
    if seen == (today, cause):
        return
    tok = diag['token'] or {}
    # MARKER AFTER THE SEND, not before. Written first, a failed send burned
    # the whole day's alert for this cause -- and this alert says every exit
    # trigger on every open position is dark. Same discipline as the exit
    # claims: mark it done only once it IS done.
    if not _send_telegram(
        f"🚨 <b>ZEBRA MONITORING BLIND</b>\n"
        f"No price for ANY of {n_open} open position(s).\n"
        f"TP, spot SL and the expiry nag are all dark.\n\n"
        f"<b>{html.escape(diag['headline'])}</b>\n"
        f"Kite said: <code>{html.escape(diag['error'][:200])}</code>\n"
        # Name the file even when it is healthy. The alert this replaced
        # pointed at it while ASSERTING expiry; this one points at it while
        # REPORTING what it says, so the reader can check rather than trust.
        f"<code>data/kite_access_token.json</code>: "
        f"{html.escape(str(tok.get('summary', 'not checked')))}\n\n"
        f"{html.escape(diag['advice'])}",
            dry_run=dry_run):
        logger.warning("BLIND alert failed to send — not marking it seen, so "
                       "the next cycle retries it.")
        return
    try:
        marker.write_text(json.dumps({'date': today, 'cause': cause}))
    except Exception as e:
        logger.warning("Could not write the blind-alert marker: %s", e)


def _store_corruption_message(info: dict) -> str:
    """The Telegram body for one marker, matched to what actually happened.

    Two conditions write this marker and they need opposite responses, so they
    get opposite words. A QUARANTINE is the catastrophe the original text
    described. A MERGE_CONFLICT is two writers touching one record: the book
    parsed, nothing was held out of it, no position stopped being monitored,
    and there is no backup to restore because nothing was quarantined --
    `backup: None` on such a marker is not a missing backup, it is the tell.

    Emitting the quarantine text for a conflict is not a cosmetic bug. It is
    the failure this repo has already paid for once: a CRITICAL that is false
    in every clause trains the reader to swipe past the marker, and the next
    one to be swiped past is the real one. A marker with no kind predates the
    split and was, in fact, a quarantine.
    """
    stamp = html.escape(str(info.get('at') or ''))
    err = html.escape(str(info.get('error'))[:400])
    kind = info.get('kind') or store_contract.MARKER_QUARANTINE

    if kind == store_contract.MARKER_MERGE_CONFLICT:
        # Stated as what IS, not as a denial of the quarantine text. A reader
        # scanning an alert during an incident takes the nouns out of it, and
        # "nothing was quarantined" leaves the word "quarantined" on the page.
        return (
            f"⚠ <b>ZEBRA STORE MERGE CONFLICT</b>\n"
            f"Two writers changed the same record(s) at {stamp}.\n"
            f"Detail: <code>{err}</code>\n"
            f"The book is INTACT, readable, and every open position is still "
            f"being watched. This is a write race between two processes, not a "
            f"damaged file, and no restore is needed. The losing side's edit "
            f"was dropped — re-check those records if that write mattered.")
    return (
        f"🛑 <b>ZEBRA STORE CORRUPT</b>\n"
        f"The trade file failed to parse and was quarantined at {stamp}.\n"
        f"Error: <code>{err}</code>\n"
        f"Backup: <code>{html.escape(str(info.get('backup')))}</code>\n"
        f"The store may have restarted EMPTY — if so, exit monitoring is "
        f"off on every open position and ids can be reissued. Restore from "
        f"the backup or Drive before the next session.")


def _alert_store_corruption(dry_run: bool = False) -> bool:
    """Telegram once per quarantine event, then disarm on the marker itself.

    The store writes the marker (it has no Telegram dependency by design); this
    reads it. Keyed on the marker's own timestamp rather than on today's date,
    because two corruptions in one day are two events and the second matters as
    much as the first.

    The alert names the backup file: after a quarantine the `.corrupt.*.json`
    is the ONLY surviving copy of anything the Drive merge has not seen, and it
    is deleted by nothing. Telling the human where it is, at the moment it is
    created, is the difference between a recoverable incident and a lost book.
    """
    marker = cfg.LOG_DIR / 'zebra_store_corrupt.json'
    seen = cfg.LOG_DIR / 'zebra_store_corrupt.alerted'
    try:
        if not marker.exists():
            return False
        info = json.loads(marker.read_text())
        stamp = str(info.get('at') or '')
        if seen.exists() and seen.read_text().strip() == stamp:
            return False
        # THE ALL-CLEAR IS GATED ON THE ALARM HAVING BEEN SENT.
        #
        # `seen.write_text(stamp)` ran unconditionally, discarding
        # `_send_telegram`'s return value -- so a network blip or an HTML-400
        # disarmed this event PERMANENTLY (the dedup is keyed on the marker's
        # own timestamp, once per event EVER, deliberately). The message it
        # loses is the highest-consequence one in the file: a quarantine means
        # the book went empty and every open position stopped being monitored.
        #
        # Every claim-then-act path in this module already has this discipline
        # -- `_send_exit_alert` releases its claim on a failed send, and so do
        # the enter alert, the escalation and the review alert. The ALERT
        # family was the one that never got it.
        kind = info.get('kind') or store_contract.MARKER_QUARANTINE
        if not _send_telegram(_store_corruption_message(info), dry_run=dry_run):
            logger.critical(
                "STORE %s alert FAILED to send (at %s, backup %s) — NOT "
                "marking it seen, so the next cycle retries. This is the "
                "alert that says the book went empty.",
                kind.upper(), stamp, info.get('backup'))
            return False
        seen.write_text(stamp)
        logger.critical("STORE %s alerted (at %s, backup %s)",
                        kind.upper(), stamp, info.get('backup'))
        return True
    except Exception as e:
        logger.error("Could not process the store-corruption marker: %s", e)
        return False


#: This process's own dedup for the arming verdict. Persisted for the same
#: reason the exit-engine alert's is: the zebra cron process exits between
#: cycles, so an in-memory flag would re-alert every five minutes and train the
#: reader to swipe past the one message that says nothing is holding the stops.
ARMING_ALERT_STATE_NAME = 'arming_alert_state.json'
ARMING_REPEAT_SEC = 60 * 60     # a standing fault is re-stated hourly


def _monitor_dry_run() -> Optional[bool]:
    """Is the peer engine armed to place orders? None when unknowable.

    Read from the heartbeat the peer writes, which records whether it can BOOK
    rather than merely whether it breathes -- the kill switch forces it into
    dry run for the session without stopping it, so "alive" is not the
    question. A missing, stale or unparseable beat is None, NOT False: an
    unknown that answers "armed" would let the arming verdict certify the one
    state it exists to catch. `alert_if_exit_engine_down` owns the ABSENCE of
    the peer; this owns the switch combination.
    """
    hb = read_exit_engine_heartbeat()
    if hb['state'] == 'dry_run':
        return True
    if hb['state'] == 'ok':
        return False
    # `no_cohort_book` used to answer False here, i.e. "armed" -- and it is
    # armed, for the three books it CAN read. It is not armed for this one:
    # the beat says its adapter onto the cohort store failed, so it cannot see
    # a cohort record at all, let alone book one. Reporting it as the live
    # records' engine on the strength of a beat that says it cannot reach them
    # is the same error as reading a missing beat as healthy. UNKNOWN is the
    # honest answer, and `classify` now turns unknown-plus-live-records into a
    # fault rather than into silence.
    return None


def _cohort_population(trades) -> set:
    """Which classes of cohort record are OPEN right now.

    The arming verdict turns on this: today's deployed state cannot book a
    LIVE cohort record and there are none, so reporting it as ILLEGAL every
    five minutes would be noise. `common/arming.py` renders the same finding
    as LATENT instead, which is the honest description -- one `zebra enter` on
    a hand-placed trade away from being real.
    """
    pop = set()
    for t in trades or ():
        if t.get('status') != 'entered' or not in_cohort(t):
            continue
        # The UNSTAMPED class is read from the RAW field, deliberately not
        # through `is_paper_record` -- that helper answers "may zebra book
        # this at mid", and its whole job is to resolve an absent flag to
        # True. Asking it here would launder away the very ambiguity being
        # detected, and the record would be counted as ordinary paper while
        # `bcs/spread_monitor` counted the same record as live.
        flag = t.get('paper')
        if not isinstance(flag, bool):
            pop.add(arming_mod.UNSTAMPED_RECORD)
            continue
        pop.add(arming_mod.PAPER_RECORD if is_paper_record(t)
                else arming_mod.LIVE_RECORD)
    return pop


#: Dedup for the calendar-lapse alert, alongside the arming one and for the
#: same reason: this process exits between cycles.
CALENDAR_ALERT_STATE_NAME = 'calendar_alert_state.json'


def _alert_calendar_coverage(dry_run: bool = False) -> Optional[str]:
    """Say so BEFORE the NSE holiday list runs out. Never raises.

    `sessions_between` already warns when it is asked to count past coverage.
    That warning is passive twice over: it only fires once a position with an
    expiry beyond the window already exists, and it lands in a cron log on the
    day it starts mattering. Refreshing the list is not a code change --
    somebody has to find next year's NSE circular, and NSE publishes it in
    December -- so the notice has to arrive with time to act on it.

    Stale data here means a position held INTO the delivery ramp: past
    coverage the count degrades to weekdays-only, which OVER-estimates the
    sessions remaining and fires every delivery close LATER.

    Once a week while expiring, once a day once expired.
    """
    # IST, like every date in this module (M7). A naive `date.today()` would
    # move the lapse warning by a day on a UTC box.
    st = nse_holidays.coverage_status(datetime.now(IST).date())
    if st['state'] == 'ok':
        # INFO, not debug. A healthy calendar that logs NOTHING is
        # indistinguishable from a check that is not wired in, which is the
        # distinction this whole file keeps insisting on -- and the calendar
        # is being watched in the Pi logs precisely to confirm it works.
        # One line per cron invocation, the same cadence as the capital line.
        logger.info('NSE holiday calendar OK: %s', st['detail'])
        return None
    logger.warning('NSE HOLIDAY CALENDAR: %s', st['detail'])
    # Daily for anything that means the calendar is NOT WORKING right now;
    # weekly for `expiring`, which is a diary note about December. A missing
    # or stale file is the same fault class as an expired one -- session
    # counts have silently degraded to weekdays-only -- so it gets the same
    # cadence.
    every = (24 * 3600
             if st['state'] in ('expired', 'missing', 'unreadable', 'stale')
             else 7 * 24 * 3600)
    now = time.time()
    try:
        with open(cfg.LOG_DIR / CALENDAR_ALERT_STATE_NAME) as f:
            prev = json.load(f)
        prev = prev if isinstance(prev, dict) else {}
    except Exception:
        prev = {}
    if prev.get('state') == st['state'] and             now - float(prev.get('alerted_at') or 0) < every:
        return None
    _send_telegram(
        html.escape('\U0001F4C5 NSE HOLIDAY CALENDAR %s\n%s'
                    % (st['state'].upper(), st['detail'])),
        dry_run=dry_run)
    try:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = cfg.LOG_DIR / CALENDAR_ALERT_STATE_NAME
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w') as f:
            json.dump({'state': st['state'], 'alerted_at': now}, f)
        tmp.replace(path)
    except Exception as e:
        logger.warning('could not persist the calendar alert state: %s', e)
    return st['state']


def _arming_preflight(store, dry_run: bool = False) -> dict:
    """State the switch combination, every cycle. Never raises.

    Beside the store-corruption and options-CSV checks, and for the same
    reason: all three are conditions the trading code reads as "nothing to
    do". This one is the worst of the three that way -- an illegal arming
    state produces no error anywhere, and its logs look healthy from both
    engines at once, which is why it was a table in a document instead of a
    check in a process.
    """
    try:
        # WHOLE BOOK: `_cohort_population` does the scoping itself, and it
        # must see unstamped records to classify them as UNSTAMPED.
        trades = store.load_trades()
    except Exception as e:
        logger.warning("arming preflight could not read the book: %s", e)
        trades = None
    state = arming_mod.check(
        paper_mode=cfg.PAPER_MODE,
        exits_external=cfg.EXITS_MANAGED_EXTERNALLY,
        auto_entry=cfg.AUTO_ENTRY,
        dry_run=_monitor_dry_run(),
        population=_cohort_population(trades),
        engine='zebra/monitor.py',
        log=lambda line: logger.info('%s', line))
    if not state['legal']:
        _alert_arming(state, dry_run=dry_run)
    return state


def _alert_arming(state: dict, dry_run: bool = False) -> None:
    """Telegram an illegal arming state on TRANSITION, then hourly.

    Same noise discipline as `alert_if_exit_engine_down`, and keyed on the
    fault SHAPE rather than on a bare boolean: moving from "no engine" to "two
    engines" is a different fault and must not be silenced by the dedup for
    the one before it.
    """
    key = '|'.join(sorted('%s:%s' % (f['state'], f['record_class'])
                          for f in state['faults']))
    now = time.time()
    try:
        with open(cfg.LOG_DIR / ARMING_ALERT_STATE_NAME) as f:
            prev = json.load(f)
        prev = prev if isinstance(prev, dict) else {}
    except Exception:
        prev = {}
    if prev.get('key') == key and             now - float(prev.get('alerted_at') or 0) < ARMING_REPEAT_SEC:
        return
    msg = arming_mod.telegram_text(state, 'zebra/monitor.py')
    if msg:
        _send_telegram(html.escape(msg), dry_run=dry_run)
    try:
        cfg.LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = cfg.LOG_DIR / ARMING_ALERT_STATE_NAME
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'w') as f:
            json.dump({'key': key, 'alerted_at': now}, f)
        tmp.replace(path)
    except Exception as e:
        # Losing the dedup makes this NOISY, never silent. Right direction.
        logger.warning("could not persist the arming alert state: %s", e)


def _alert_options_csv_stale(dry_run: bool = False) -> bool:
    """M3 - say it OUT LOUD when the options chain stops being refreshed.

    `analyze_bcs` already refuses to build on a stale chain, and that refusal
    is silent by design: it looks like every other hard gate, one
    `bcs_suppressed` line per signal. That is right for an unclean SIGNAL and
    wrong here. A dead refresh job suppresses EVERY signal, every cycle,
    indefinitely - and the cohort's missing evidence is the one thing standing
    between this system and being armed. Stalling it quietly is the expensive
    outcome, so the fault gets a voice separate from the trades it blocks.

    Once a day, on a MARKER FILE rather than in memory: this process is a
    five-minute cron that exits between cycles, so an in-process set would
    re-alert 78 times a session. (The BCS monitor's nags can use a set because
    that one is a single long-lived process; the difference is the process
    model, not the policy.)
    """
    stale, why = strikes_mod.options_csv_stale()
    seen = cfg.LOG_DIR / 'zebra_options_csv_stale.alerted'
    #: IST-aware, like every other date in this module. A naive `date.today()`
    #: would key the dedup to the box's timezone (M7).
    today = datetime.now(IST).date().isoformat()
    if not stale:
        # Clear the marker so a REPEAT of the fault next week alerts again
        # rather than being deduped against a stamp from the last outage.
        try:
            if seen.exists():
                seen.unlink()
        except OSError:
            pass
        return False
    logger.error("ENTRIES BLOCKED: %s", why)
    try:
        if seen.exists() and seen.read_text().strip() == today:
            return False
    except OSError:
        pass
    _send_telegram(
        "\u26a0\ufe0f <b>OPTIONS CHAIN IS STALE - ENTRIES BLOCKED</b>\n"
        "%s\n"
        "Lot sizes come from that file and a lot size becomes an order "
        "quantity, so no new entry will be built until it is refreshed. "
        "Open positions are UNAFFECTED - exits never read it.\n"
        "Fix: run <code>helper/kite_nse_options.py</code> (its 09:00 Mon-Fri "
        "cron has not produced a file)." % html.escape(why),
        dry_run=dry_run)
    try:
        seen.write_text(today)
    except OSError as e:                        # pragma: no cover - fs-level
        logger.error("Could not stamp the stale-CSV marker: %s", e)
    return True


def _settlement_value(trade: dict, spot: float) -> Optional[float]:
    """What the structure is worth at expiry, from spot alone.

    At expiry extrinsic is zero by definition, so no option book is needed —
    which is the point: this is the one valuation that still works when the
    book has gone dark. Bounded the same way every booked value is.
    """
    try:
        k_l = float(trade['long_strike'])
        k_s = float(trade['short_strike'])
        mult = _long_multiplier(trade)
        if trade['direction'] == 'CE':
            v = mult * max(spot - k_l, 0.0) - max(spot - k_s, 0.0)
        else:
            v = mult * max(k_l - spot, 0.0) - max(k_s - spot, 0.0)
        v = max(0.0, v)
        if trade.get('structure') == 'bcs':
            w = float(trade.get('width') or 0)
            if w > 0:
                v = min(v, w)
        return round(v, 2)
    except Exception as e:
        logger.warning("Settlement value failed for #%s: %s",
                       trade.get('id'), e)
        return None


def _settle_if_expired(store: ZebraStore, trade: dict, spot: float, today,
                       pending_mfe: Optional[dict] = None,
                       dry_run: bool = False) -> bool:
    """Terminal safety net: close a position whose expiry has PASSED.

    Every other exit needs a quote, so a trade whose book dies never reaches
    one — it rides past expiry and stays `entered` forever. That is not just an
    accounting leak: scanner dedup keys on open positions, so one orphan bans
    its stock from the pipeline permanently. Expiry is the one moment we can
    price without a book, so it is the one place the net can hang.

    Strictly PAST expiry (`today > exp`) — expiry day itself still trades.
    """
    try:
        exp = datetime.strptime(trade['expiry'], '%Y-%m-%d').date()
    except Exception:
        return False
    if today <= exp:
        return False
    val = _settlement_value(trade, spot)
    if val is None:
        return False
    logger.warning(
        "EXPIRY SETTLE #%d %s: expiry %s has passed and there is no usable "
        "book — settling at intrinsic %.2f from spot %.2f",
        trade['id'], trade['stock'], trade['expiry'], val, spot)
    # ONE name for the flag, the alert and the close reason. They disagreed:
    # the flag was claimed as `expiry_settled` and the close ran as `expiry`, so
    # on a failed close `_paper_auto_close` released `expiry_alerted_at` — a key
    # nothing had ever set — and logged "flag released, will retry" about a flag
    # it never touched. Benign only by accident, since this close is retried
    # every cycle regardless of the flag; a broken invariant waiting for the
    # next person who relies on the release actually releasing something.
    if store.set_alert_flag(trade['id'], 'expiry') \
            and _alerts_enabled(trade):
        _send_exit_alert(
            store, trade, 'expiry',
            f"⏹️ <b>{html.escape(str(trade['stock']))} EXPIRED</b>\n"
            f"#{trade['id']} {_struct_label(trade)} — no usable option book, "
            f"settled at intrinsic <b>{val:.2f}</b>/sh (spot {spot:.2f}).\n"
            f"Entry debit was {float(trade.get('debit', 0)):.2f}/sh.",
            dry_run=dry_run)
    _paper_auto_close(store, trade, val, 'expiry', spot,
                      pending_mfe=pending_mfe)
    return True


def _track_debit_blindness(store: ZebraStore, trade: dict, usable: bool,
                           reason: Optional[str], dry_run: bool = False,
                           spot: Optional[float] = None) -> None:
    """Fire ONE rate-limited Telegram when the DEBIT-SL valuation has been blind
    (unreliable / missing book) for DEBIT_BLIND_CYCLES consecutive cycles while
    a trade is entered. Re-arms once a usable quote returns. SL_SPOT/TP are
    unaffected — they run off real spot trades."""
    tid = trade['id']
    if usable:
        store.clear_blind(tid)
        return
    n = store.bump_blind(tid)
    if n >= cfg.DEBIT_BLIND_CYCLES and store.mark_blind_alerted(tid):
        if _alerts_enabled(trade):
            _send_telegram(_format_blind_alert(trade, reason, spot),
                           dry_run=dry_run)
        logger.warning("DEBIT-BLIND alert #%d %s: %d cycles unreliable (%s)",
                       tid, trade['stock'], n, reason)


def _time_nag(store: ZebraStore, trade: dict, today, mid, spot,
              pending_mfe, dry_run: bool = False, reliable: bool = True,
              legs=None) -> None:
    """The expiry reminder, and in PAPER the close that goes with it.

    Shared by the normal cascade and the dark-book deferral, because the nag is
    a CALENDAR fact: Indian stock options are physically settled and the
    exchange ramps a delivery margin over the final sessions, so a position
    whose option book has gone unquotable in its last week needs the warning
    MORE than one that is quoting fine, not less. It previously sat below the
    no-quote `continue` and was silenced by exactly that case.

    SESSIONS, not calendar days: "3 days left" on a Friday is one session, the
    moment the old calendar count was most wrong and the margin most urgent. No
    holiday calendar exists, so this over-counts across one; TIME_SL_DAYS is
    set with that slack in mind.
    """
    tid = trade['id']
    try:
        exp = datetime.strptime(trade['expiry'], '%Y-%m-%d').date()
        days_left = _sessions_left(today, exp)
    except Exception:
        days_left = 999
    if days_left > cfg.TIME_SL_DAYS:
        return

    # THE NAG is a calendar fact and fires once per DAY, ungated: a position
    # whose book has gone dark in its final week needs the delivery-margin
    # warning more than one quoting fine, not less.
    if store.set_alert_flag_daily(tid, 'time'):
        if _alerts_enabled(trade):
            _send_telegram(_format_time_alert(trade, days_left, mid),
                           dry_run=dry_run)
        logger.info("TIME alert #%d %s days_left=%d mid=%s", tid,
                    trade['stock'], days_left,
                    f'{mid:.2f}' if mid is not None else 'NA')

    # THE CLOSE is a claim about a PRICE, and was the one exit still booking at
    # the opening auction. The daily flag is claimed by the 09:15 cycle and the
    # close ran straight off it, so all 34 `paper:time` exits in the book priced
    # between 09:15:35 and 09:18:05 — not one later in the day, while every
    # other exit reason spreads across the session. That is the exact window
    # VALUE_TRIGGER_OPEN_BUFFER_SEC exists to sit out, and both incidents that
    # cost real money were opening prints. The nag above still goes out on time;
    # only the booking waits.
    if not _value_triggers_live():
        logger.info(
            "TIME HOLD #%d %s: nagged, close held until %ds after the open "
            "(days_left=%d)", tid, trade['stock'],
            cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC, days_left)
        return
    # Retried EVERY cycle past the buffer, not once per day. The close is no
    # longer gated on the daily flag, so a defer on a momentarily unusable book
    # costs one poll rather than stranding the position un-booked until
    # tomorrow — the old coupling gave TIME exactly one attempt per session.
    _paper_auto_close(store, trade, mid, 'time', spot, pending_mfe=pending_mfe,
                      reliable=reliable, release_flag=False, legs=legs)


def check_entered(store: ZebraStore, kite, dry_run: bool = False) -> None:
    """Monitor entered trades for TP/SL/time exits.

    PAPER mode (default): auto-close at structure mid after each exit alert.
    LIVE mode: alert only UNLESS `_exits_external(trade)` is true (armed by
    `cfg.EXITS_MANAGED_EXTERNALLY` AND cohort membership), in which case
    `bcs/spread_monitor.py` places the real closing orders through the exit
    bridge instead; otherwise the user runs `zebra close` manually.
    Dedup via persistent <kind>_alerted_at flags on each trade (survives
    cron restarts).
    """
    entered = store.get_entered()
    if not entered:
        return

    stocks = list({t['stock'] for t in entered})
    # `get_ltp` guards its kite.ltp() call but NOT the instrument-cache load
    # underneath it, so a dead access token raises straight out of here. That
    # killed the whole function, run_cycle logged one line, and exit monitoring
    # on every open position stopped — with no Telegram, and with the blind
    # counter untouched because it lives inside the per-trade loop below that
    # never ran. health.py watches the Claude CLI credential and nothing has
    # ever watched Kite's.
    # This is the ONLY fetch in the file whose failure stops exit monitoring
    # outright, so it is the one that must be able to say WHY it failed and
    # the one worth waiting out a rate limit for. `_spot_ltps` does both.
    ltp_error: Optional[BaseException] = None
    try:
        ltps, ltp_error = _spot_ltps(kite, stocks)
    except Exception as e:
        logger.error("LTP fetch RAISED for %d open position(s): %s",
                     len(entered), e, exc_info=True)
        ltps, ltp_error = {}, e
    if not any((ltps.get(s) or 0) > 0 for s in stocks):
        # Not one price for any open position. Blind on the SPOT source, which
        # is the one TP and the expiry nag run off, so this is a full stop of
        # exit monitoring rather than a degraded mode. Blind means Telegram.
        _alert_monitoring_blind(len(entered), stocks, error=ltp_error,
                                dry_run=dry_run)
        return
    today = datetime.now(IST).date()
    # One store write for the whole cycle's peak tracking — see _flush_mfe.
    pending_mfe: dict = {}
    # Once-per-cycle latch for the peer-engine heartbeat check below.
    stood_down = False
    #: Same once-per-cycle discipline as `stood_down`: a fault common
    #: to the whole book must not send one Telegram per row.
    live_unmanaged_checked = False

    for trade in entered:
        # An earlier exit-check this cycle may have already auto-closed.
        if trade.get('status') != 'entered':
            continue

        # ── Per-position fault isolation ─────────────────────────────
        # One bad record used to abort the whole phase. The body indexes
        # directly (`trade['tp_spot']`, `trade['debit_sl_value']`) and calls a
        # store that can raise LockTimeout, so a single hand-edited or
        # half-merged row raised out of the loop and EVERY position sorted
        # after it got no exit check at all — silently, since run_cycle catches
        # at the phase level. The cycle's accumulated peak and corroboration
        # patches went with it, because `_flush_mfe` below never ran. Same shape
        # as the dead-token incident this file already documents: that fix
        # guarded one CALL, this guards the CLASS.
        try:
            stock = trade['stock']
            tid = trade['id']
            _release_stranded_claims(store, trade)
            spot = ltps.get(stock, 0)
            if spot <= 0:
                # A suspended, renamed or delisted underlying — `get_ltp`
                # returns the 0.0 sentinel for a symbol missing from the
                # instrument cache. This was a bare `continue`: no POLL line, no
                # counter, no alert. The position produced ZERO log output while
                # staying `entered` forever, and the scanner's dedup went on
                # banning its stock from ever signalling again.
                # `_alert_monitoring_blind` cannot cover it — that fires only
                # when EVERY position is unpriceable, so one dead symbol among
                # 24 healthy ones is invisible by construction. In paper mode
                # the log is the entire forensic record, and this position was
                # not in it.
                logger.warning(
                    "NO SPOT #%d %s: underlying did not quote this cycle — "
                    "every exit trigger on this position is blind", tid, stock)
                # Expiry is the one valuation needing neither a book nor a live
                # spot. `_expire_if_ancient` already rescues WATCHING rows from
                # exactly this immortality; entered rows had no equivalent, so
                # fall back to the last spot ever recorded and let the position
                # terminate rather than outlive its own contract.
                last = (trade.get('corrob_spot') or trade.get('entry_spot') or 0)
                try:
                    if float(last) > 0:
                        _settle_if_expired(store, trade, float(last), today,
                                           pending_mfe, dry_run)
                except (TypeError, ValueError):
                    pass
                if store.set_alert_flag_daily(tid, 'no_spot') \
                        and _alerts_enabled(trade):
                    _send_telegram(_format_no_spot_alert(trade), dry_run=dry_run)
                continue

            direction = trade['direction']
            tp_spot = trade['tp_spot']
            sl_spot = trade['sl_spot']

            # One structure quote per trade per cycle. Every paper exit books at
            # this mid, so if the quote is momentarily unavailable we DEFER the
            # whole trade to the next poll — rather than burning a one-shot dedup
            # flag on a close we can't execute (which would strand the exit
            # forever) or booking a fabricated max-loss. LIVE mode still alerts
            # (mid rendered as NA) since there it's an alert, not an auto-close.
            # Passing spot arms the intrinsic-floor clamp (false-debit-SL guard).
            # ── Strike-adjusting corporate action ───────────────────────────
            # Before the quote, before MFE, before every exit. On a bonus/split/
            # rights ex-date the exchange re-prices the underlying and adjusts the
            # strikes with it: a 1:1 bonus halves the quoted spot, so yesterday's
            # sl_spot is breached instantly by an event in which nothing went
            # wrong, and the per-share debit refers to a lot size that changed. The
            # recorded PEAK would be corrupted too, which is why this sits above
            # the capture and not merely above the triggers. Nothing automated can
            # repair those levels, so the position is suspended for the day and the
            # human is told. (Ordinary ex-dividends are NOT included — see
            # events.ADJUSTMENT_TYPES.)
            adj = None
            try:
                adj = events_mod.adjustment_today(stock)
            except Exception as e:
                logger.debug("Adjustment lookup failed for %s: %s", stock, e)
            if adj:
                if store.set_alert_flag_daily(tid, 'corp_action') \
                        and _alerts_enabled(trade):
                    _send_telegram(_format_corp_action_alert(trade, adj),
                                   dry_run=dry_run)
                logger.warning("CORP ACTION #%d %s: %s today — automated exits "
                               "SUSPENDED (stored spot levels are stale)",
                               tid, stock, adj.get('type'))
                continue

            sq = _structure_quote(kite, trade, spot)
            mid = sq['mid']
            # DEBIT-SL valuation is usable only with a mid AND a reliable book —
            # a wide/crossed/one-sided book (2026-07-24) freezes the value trigger.
            debit_usable = mid is not None and sq['reliable']
            _track_debit_blindness(store, trade, debit_usable, sq['reason'],
                                   dry_run=dry_run, spot=spot)

            # ── The second source, and the open ──────────────────────────────
            # Everything above this line is checks on ONE source: the option book.
            # These two are the other kind. Both gate ONLY the value triggers
            # (DEBIT-SL, TRAIL) by folding into `debit_usable`; spot-driven TP and
            # the TIME nag are untouched, because spot at the open is real trades
            # and the calendar does not care about the book.
            if debit_usable:
                corrob_ok, corrob_why, corrob_patch = _spot_corroborates(
                    store, trade, spot, mid, sq['reliable'])
                if corrob_patch:
                    pending_mfe.setdefault(tid, {}).update(corrob_patch)
                if not corrob_ok:
                    logger.warning(
                        "SPOT VETO #%d %s: %s — value triggers held this cycle",
                        tid, stock, corrob_why)
                    debit_usable = False
            # ONE line per open position per cycle, unconditionally. Without it a
            # cycle carrying 33 positions logged nothing at all between the store
            # banner and the scanner summary — `check_entered` spoke only when a
            # trigger fired — so "what did the engine SEE at 14:35" and "why did TP
            # not fire" were both unanswerable. In paper mode the log is the entire
            # forensic record, and a record of exits only is a record of the
            # cycles that were already interesting.
            logger.info(
                "POLL #%d %s %s spot=%.2f tp=%.2f%s sl=%.2f | value=%s "
                "(%s) debit_sl=%.2f | long %s/%s short %s/%s",
                tid, stock, direction, spot, tp_spot,
                # The state the cycle STARTS in. An armed TP that is waiting on
                # a vet or an unusable book looks identical to a quiet position
                # otherwise, and this log is the whole forensic record in paper.
                ' [TP-LATCHED]' if tp_latched(trade) else '', sl_spot,
                f'{mid:.2f}' if mid is not None else 'NA',
                'ok' if debit_usable else (sq.get('rejected') or sq['reason']
                                           or 'unusable'),
                trade.get('debit_sl_value', 0),
                ((sq.get('legs') or {}).get('long') or {}).get('bid'),
                ((sq.get('legs') or {}).get('long') or {}).get('ask'),
                ((sq.get('legs') or {}).get('short') or {}).get('bid'),
                ((sq.get('legs') or {}).get('short') or {}).get('ask'))

            if debit_usable and not _value_triggers_live():
                logger.info(
                    "OPEN BUFFER #%d %s: value triggers dark until %ds after the "
                    "open (spot TP and the expiry nag still live)",
                    tid, stock, cfg.VALUE_TRIGGER_OPEN_BUFFER_SEC)
                debit_usable = False

            # BEFORE the exit branches and BEFORE the no-quote skip below: the poll
            # that exits a trade is usually the poll that set its peak, and the
            # underlying is still worth recording on a cycle when the option book
            # is dark. Never gates or blocks anything — pure measurement.
            # DEPTH, on the same footing as the peak and for the same
            # reason: measurement, before any exit branch, folded into the one
            # batched store write this cycle already makes. Never gates
            # anything. An exit that cannot fill is the unbounded failure on
            # this book, and until now nothing recorded whether the touch
            # could have carried the position out.
            dpatch = depth_mod.observe(trade, sq.get('legs'))
            if dpatch:
                pending_mfe.setdefault(tid, {}).update(dpatch)
                trade.update(dpatch)

            patch = mfe_mod.compute(trade, spot, mid, sq['reliable'])
            if patch:
                # MERGE, never assign. This dict may already hold the spot
                # corroboration reference for the same trade, and `pending_mfe[tid]
                # = patch` silently dropped it on every cycle where a peak
                # advanced — leaving the veto with a baseline that only updated on
                # quiet cycles, which is precisely backwards.
                pending_mfe.setdefault(tid, {}).update(patch)

            # ── THE TP LATCH ────────────────────────────────────────────────
            # The FIRST observed touch arms this exit for the REST OF THAT
            # TRADING DAY; from here on it proceeds wherever spot has gone
            # (owner, 2026-08-28, bounded to the session the same day: "TP
            # latch should be for same day"). The expiry is evaluated on read
            # inside `tp_latched`, so nothing here has to run to end it. The
            # trigger is a fact about SPOT, so it is captured here — above the
            # dark-book defer and above the stand-down — and NOT in the TP
            # branch below, which several guards `continue` past. A touch the
            # engine watched and did not record is the whole defect: COFORGE
            # #436 traded through its TP at 09:25, the vet allowed at 09:27,
            # and by the next actionable poll spot had backed off with nothing
            # booked.
            #
            # Written to the RECORD because this process exits between cycles —
            # the same reason the corroboration reference and the time-stop
            # state are persisted — and BEFORE the vet, because a verdict in
            # flight is exactly when the trigger used to evaporate.
            #
            # Deliberately BELOW the corporate-action guard. On a bonus/split
            # ex-date the exchange re-prices the underlying while `tp_spot`
            # still refers to yesterday's scale, so a halved spot would "touch"
            # a PE target for a reason that has nothing to do with the market.
            # A permanent arming off a stale level is the one version of this
            # rule that could lose money, and that guard `continue`s above.
            #
            # The PRICE does not latch, only the trigger: nothing here is a
            # booking, the exit below still faces every valuation guard it
            # always did, and the close books at the book observed when it runs
            # (`feedback_trigger_is_not_the_fill`).
            tp_hit = (direction == 'CE' and spot >= tp_spot) or \
                     (direction == 'PE' and spot <= tp_spot)
            latch = tp_latch(trade, tp_hit, spot)
            if latch['patch'] and _exits_external(trade):
                # The peer engine owns this position's exits AND watches the
                # same spot every 5 seconds, so it arms its own trigger. Both
                # engines READ the latch; only the one holding the trigger
                # writes it. Said out loud because a stood-down engine that
                # silently declines to record something looks identical to one
                # that never saw it.
                logger.info(
                    "TP touched #%d %s at %.2f but exits are EXTERNAL — "
                    "spread_monitor arms this one", tid, stock, spot)
            elif latch['patch']:
                try:
                    store.update_trade_fields(tid, **latch['patch'])
                    trade.update(latch['patch'])
                    logger.info(
                        "TP LATCHED #%d %s spot=%.2f tp=%.2f — this exit is "
                        "armed for the rest of today's session, wherever spot "
                        "goes next", tid, stock, spot, tp_spot)
                except Exception as e:
                    # The exit can still fire THIS cycle (tp_hit is true), so
                    # nothing is lost yet; what is lost is the arming if the
                    # close does not book. Loud, because that is a silent
                    # reversion to the behaviour this replaces.
                    logger.error(
                        "TP LATCH WRITE FAILED #%d %s: %s — the touch is not "
                        "persisted, so this trigger can still evaporate",
                        tid, stock, e, exc_info=True)
            elif latch['latched'] and not tp_hit:
                logger.info(
                    "TP LATCHED-ARMED #%d %s spot=%.2f has retreated from tp=%.2f "
                    "(touched %s at %s) — exiting anyway",
                    tid, stock, spot, tp_spot, trade.get('tp_touch_spot'),
                    trade.get('tp_touched_at'))

            # A touch that reached the end of its session unbooked. The owner
            # bounded the latch to the day it was armed on (2026-08-28), so
            # this is the rule working — and it is the ONLY way the bound can
            # cost anything, which makes it the number that says whether the
            # bound is right. Recorded on the position that paid it and said
            # out loud once, never per poll.
            #
            # `expired_patch` is empty unless there is something new to write,
            # so this cannot fire twice for the same lapse. It is deliberately
            # NOT folded into the branch above: nothing is being armed here.
            # The write is what makes it once: the evidence is keyed on the
            # lapsed stamp, so the next poll finds it already recorded and says
            # nothing. Where this engine is stood down it cannot write, so it
            # must not shout either — an unwritable notice on every poll of
            # every position for the rest of the position's life is how a
            # reader learns to skim the log this system is being judged on.
            if latch['expired_patch']:
                if _exits_external(trade):
                    logger.debug(
                        "TP latch on #%d %s expired unbooked; exits are "
                        "EXTERNAL so spread_monitor owns the record",
                        tid, stock)
                else:
                    try:
                        store.update_trade_fields(tid, **latch['expired_patch'])
                        trade.update(latch['expired_patch'])
                        logger.info(
                            "TP LATCH EXPIRED #%d %s: touched %s at spot %s "
                            "and never booked — a latch arms for its own "
                            "session only, so this exit is back to the live "
                            "comparison (spot=%.2f tp=%.2f)",
                            tid, stock, trade.get('tp_touched_at'),
                            trade.get('tp_touch_spot'), spot, tp_spot)
                    except Exception as e:
                        logger.warning(
                            "TP LATCH EXPIRED #%d %s but the evidence was not "
                            "recorded: %s — the latch IS expired either way",
                            tid, stock, e)

            # THE RECORD DECIDES, NOT THE MODE (fixed 2026-08-31).
            #
            # `_paper_auto_close` was moved off `cfg.PAPER_MODE` on 2026-08-29
            # so a paper record keeps its booking engine after the mode flips
            # -- and this branch, which is part of that same engine, was left
            # keyed on the mode. So at arming step 6 (`paper_mode: false`)
            # every still-open PAPER record silently loses its terminal
            # expiry net: with the option book dead (delisting, symbol death,
            # a chain that stops quoting) and spot still alive, neither call
            # site of `_settle_if_expired` is reachable. The record then stays
            # `entered` past expiry forever, holding a `max_open` slot and
            # deployed rupees, banning its stock through scanner dedup, and
            # nagging daily.
            if is_paper_record(trade) and mid is None:
                # Terminal net first: without it a position whose book has gone
                # dark never reaches ANY exit and stays `entered` past expiry
                # forever, which also bans its stock from the scanner for good.
                # Expiry is the one valuation that needs no book.
                if not _settle_if_expired(store, trade, spot, today,
                                          pending_mfe, dry_run):
                    # The expiry NAG is a calendar fact and must survive a dark
                    # book. It used to sit below this `continue`, so a position
                    # whose book went unquotable in its final week got no warning
                    # at all — and the delivery-margin ramp is exactly what the nag
                    # exists to stay ahead of. Alert only: with no price there is
                    # nothing to book, and the daily flag re-nags tomorrow.
                    _time_nag(store, trade, today, None, spot, None, dry_run)
                    logger.info(
                        "DEFER #%d %s: no usable book (%s) — spot=%.2f, nothing "
                        "booked this cycle", tid, stock,
                        sq.get('rejected') or sq['reason'] or 'no_quote', spot)
                continue

            # Stand down: another engine owns this position's exits.
            #
            # Placed AFTER the POLL line and the MFE/corroboration writes and
            # BEFORE the cascade, deliberately. Measurement is not an exit
            # decision: the peak, the spot-corroboration reference and the
            # forensic POLL line are this book's research record and must keep
            # accruing whoever is holding the trigger. What stops is deciding.
            #
            # Skipping the whole cascade rather than only the closes also stops
            # the duplicate work that would otherwise be invisible: two engines
            # raising vet requests against ONE shared marker per (trade, kind),
            # double-incrementing its defer count and escalating to the human
            # twice as fast, plus two Telegrams per trigger.
            #
            # `_settle_if_expired` above is untouched -- see
            # EXTERNALLY_MANAGED_EXITS.
            if _exits_external(trade):
                logger.info(
                    "EXITS EXTERNAL #%d %s: spread_monitor owns this "
                    "position's exits - measured, not acted on", tid, stock)
                # Standing down is only safe if somebody stood UP. Checked
                # here rather than at the top of the cycle because this branch
                # is the only place the hand-off actually happens, and once
                # per cycle rather than once per trade because a fault common
                # to the whole book must not produce one Telegram per row.
                if not stood_down:
                    stood_down = True
                    try:
                        alert_if_exit_engine_down(
                            sum(1 for t in entered if _exits_external(t)),
                            dry_run=dry_run)
                    except Exception as e:
                        logger.warning(
                            "exit-engine heartbeat check failed: %s", e,
                            exc_info=True)
                continue

            # NOT stood down, and this record has REAL LEGS. zebra will
            # decline to book it below (it can only book at mid), so the
            # monitor is its only possible engine -- and until 2026-08-31
            # nothing checked that the monitor was there, because the
            # heartbeat alert fired ONLY from the stand-down branch above.
            #
            # That left the arming order's own first live step unguarded: a
            # hand-placed live trade filed while `exits_managed_externally` is
            # still false, against a monitor that is dead. The record is
            # declined here, silently, every cycle.
            if not is_paper_record(trade):
                if not live_unmanaged_checked:
                    live_unmanaged_checked = True
                    try:
                        alert_if_exit_engine_down(
                            sum(1 for t in entered
                                if not is_paper_record(t)),
                            dry_run=dry_run, why=WHY_LIVE_RECORD)
                    except Exception as e:
                        logger.warning(
                            "exit-engine heartbeat check failed: %s", e,
                            exc_info=True)

            # ── TP ──────────────────────────────────────────────────────────
            # `latch` was decided ABOVE, before the dark-book defer and the
            # stand-down, because the touch is a fact about spot and several
            # guards `continue` past this point. `armed` is the trigger:
            # touched on this poll, or touched in some earlier one and never
            # un-touched. `tp_hit` is only used to word the alert honestly.
            # A blocked trigger skips ONLY ITS OWN branch. It must not `continue`:
            # that would also skip the DEBIT-SL and TIME checks below, so a TP held
            # on an untradeable book would suppress the T-3 expiry nag entirely and
            # ride the position into settlement week unnoticed.
            if latch['armed'] and _exit_cleared(store, trade, 'tp', sq, spot,
                                                dry_run=dry_run) \
                    and _claim_exit_alert(store, trade, 'tp'):
                _send_exit_alert(store, trade, 'tp',
                                 _format_tp_alert(trade, spot, mid,
                                                  on_latch=not tp_hit),
                                 dry_run=dry_run)
                logger.info("TP alert #%d %s spot=%.2f tp=%.2f", tid, stock, spot, tp_spot)
                _paper_auto_close(store, trade, mid, 'tp', spot,
                                  pending_mfe=pending_mfe, reliable=sq['reliable'],
                                  legs=sq.get('legs'))
                if trade.get('status') == 'exited':
                    continue

            # ── TRAIL ───────────────────────────────────────────────────────
            # Profit-protection, so it sits with TP rather than with the stops: the
            # LEVEL is above the entry debit by construction. The FILL is not — the
            # trigger is `mid <= level` and the booking price is `mid`, so a gap
            # through the level books wherever it landed. That is intended (a
            # breached trail means get out), but it means a `trail` exit can be a
            # loss, and it can land below the DEBIT-SL level too — TRAIL is checked
            # first, so it wins the tag. outcomes.label_for_reason takes the
            # realised P&L for exactly this reason; do not score `trail` as a HIT
            # off the reason string alone.
            tl = mfe_mod.trail_levels(trade) if cfg.TRAIL_ENABLED else None
            if tl and tl['armed'] and store.set_alert_flag(tid, 'trail_armed'):
                logger.info("TRAIL armed #%d %s peak=%.1f%% of max gain, level=%.2f",
                            tid, stock, tl['peak_pct_of_max'], tl['level'])
            if not debit_usable or not tl or not tl['armed']:
                # Unusable quote FREEZES the counter rather than resetting it —
                # same rule as the DEBIT-SL, so a flickering book cannot
                # indefinitely block a genuine exit.
                pass
            elif mid <= tl['level']:
                n = store.bump_confirm(tid, 'trail')
                if n >= cfg.DEBIT_SL_CONFIRM_POLLS:
                    if _exit_cleared(store, trade, 'trail', sq, spot,
                                     dry_run=dry_run) \
                            and _claim_exit_alert(store, trade, 'trail'):
                        _send_exit_alert(store, trade, 'trail',
                                         _format_trail_alert(trade, mid, tl),
                                         dry_run=dry_run)
                        logger.info("TRAIL alert #%d %s mid=%.2f<=level=%.2f "
                                    "peak_gain=%.2f (confirmed x%d)",
                                    tid, stock, mid, tl['level'], tl['peak_gain'], n)
                        _paper_auto_close(store, trade, mid, 'trail', spot,
                                          pending_mfe=pending_mfe,
                                          legs=sq.get('legs'))
                        if trade.get('status') == 'exited':
                            continue
                else:
                    logger.info("TRAIL pending #%d %s mid=%.2f<=level=%.2f "
                                "confirm %d/%d", tid, stock, mid, tl['level'],
                                n, cfg.DEBIT_SL_CONFIRM_POLLS)
            else:
                store.reset_confirm(tid, 'trail')

            # ── SPOT SL ─────────────────────────────────────────────────────
            # Disabled by default: the debit floor already caps max loss, and the
            # 3% adverse spot SL was force-exiting capped-risk trades near the
            # local bottom (biggest realized-loss bucket in paper). Flip
            # spot_sl_enabled=True in zebra_config.json to restore.
            sl_hit = cfg.SPOT_SL_ENABLED and (
                     (direction == 'CE' and spot <= sl_spot) or
                     (direction == 'PE' and spot >= sl_spot))
            if sl_hit and _exit_cleared(store, trade, 'spot_sl', sq, spot,
                                        dry_run=dry_run) \
                    and _claim_exit_alert(store, trade, 'spot_sl'):
                _send_exit_alert(store, trade, 'spot_sl',
                                 _format_spot_sl_alert(trade, spot, mid), dry_run=dry_run)
                logger.info("SPOT SL alert #%d %s spot=%.2f sl=%.2f", tid, stock, spot, sl_spot)
                _paper_auto_close(store, trade, mid, 'spot_sl', spot,
                                  pending_mfe=pending_mfe, reliable=sq['reliable'],
                                  legs=sq.get('legs'))
                if trade.get('status') == 'exited':
                    continue

            # ── DEBIT SL ────────────────────────────────────────────────────
            # Value trigger: needs DEBIT_SL_CONFIRM_POLLS consecutive RELIABLE
            # triggering reads. An unreliable / no-quote read FREEZES the counter
            # (never resets it — a flickering book must not block a genuine exit);
            # a reliable non-trigger read resets it. This is the direct fix for the
            # 2026-07-24 single-poll phantom SL.
            if not debit_usable:
                pass  # freeze confirm counter; blindness handled above
            elif mid <= trade['debit_sl_value']:
                n = store.bump_confirm(tid, 'debit_sl')
                if n >= cfg.DEBIT_SL_CONFIRM_POLLS:
                    # THE NHPC case. The debounce above proves the reading is
                    # repeatable; it cannot prove the book was real. Vet before
                    # claiming the consume-once flag.
                    if _exit_cleared(store, trade, 'debit_sl', sq, spot,
                                     dry_run=dry_run) \
                            and _claim_exit_alert(store, trade, 'debit_sl'):
                        _send_exit_alert(store, trade, 'debit_sl',
                                         _format_debit_sl_alert(trade, mid),
                                         dry_run=dry_run)
                        logger.info("DEBIT SL alert #%d %s mid=%.2f sl=%.2f (confirmed x%d)",
                                    tid, stock, mid, trade['debit_sl_value'], n)
                        _paper_auto_close(store, trade, mid, 'debit_sl', spot,
                                          pending_mfe=pending_mfe,
                                          legs=sq.get('legs'))
                        if trade.get('status') == 'exited':
                            continue
                else:
                    logger.info("DEBIT SL pending #%d %s mid=%.2f<=sl=%.2f confirm %d/%d",
                                tid, stock, mid, trade['debit_sl_value'],
                                n, cfg.DEBIT_SL_CONFIRM_POLLS)
            else:
                store.reset_confirm(tid, 'debit_sl')

            # ── TIME SL ─────────────────────────────────────────────────────
            # SESSIONS, not calendar days. Indian stock options are physically
            # settled and the exchange ramps a delivery margin over the final
            # trading sessions, so "3 days left" on a Friday — one session — is the
            # exact moment the old calendar count was most wrong and the margin
            # most urgent. No holiday calendar exists here, so this over-counts
            # across a holiday; TIME_SL_DAYS is set with that slack in mind.
            _time_nag(store, trade, today, mid, spot, pending_mfe, dry_run,
                      reliable=sq['reliable'], legs=sq.get('legs'))
        except Exception as e:
            # Never fatal to the phase. A position that cannot be evaluated is
            # a position that keeps its stop where it is until the next poll.
            logger.error(
                "POSITION CHECK FAILED #%s %s: %s — this position is skipped "
                "this cycle; the rest of the book continues",
                trade.get('id'), trade.get('stock'), e, exc_info=True)
            continue

    # Anything not already flushed by an exit. On a normal cycle this is the
    # ONLY store write the peak tracking does, however many positions moved.
    _flush_mfe(store, pending_mfe)


def _reap_starved_vets(store: ZebraStore, dry_run: bool = False) -> list:
    """Cancel signals the vetting queue gave up on, and SAY SO.

    This is the guardrail that makes a fail-CLOSED entry path safe. Parking
    entries instead of taking them unvetted is only defensible while the halt
    announces itself: a broken CLI must not be able to stop trading quietly
    behind a switch that still reads ON. Every drop is one Telegram naming the
    signal and the cause, so an outage is visible within the hour rather than
    at the end of a flat week.

    Cancelling (not leaving it triggered) is deliberate: a cancelled record is
    NOT deduped by the scanner, so if the setup still holds tomorrow it is
    re-added, re-triggered and vetted afresh. Nothing is lost but this attempt.
    """
    reaped = []
    # WHOLE BOOK: reaping a starved vet marker is housekeeping on a marker.
    # A legacy record holding one would keep it forever if scoped out.
    for t in list(store.load_trades()):
        if t.get('status') not in ('watching', 'triggered'):
            continue
        if vet_mod.vet_state(t) != vet_mod.STARVED:
            continue
        v = t.get('vet') or {}
        why = v.get('failed_open_because') or 'no verdict'
        # "It ran and stayed silent" and "it never started" need OPPOSITE
        # fixes, so the alert must not say the first when it means the second.
        # On 2026-08-14 HAVELLS read "Claude never returned a verdict — no
        # agent slot", and BOTH halves were wrong: the agent had been started
        # and had refused on a usage limit, and the slot budget was near empty.
        try:
            ran = int(v.get('attempts') or 0)
        except (TypeError, ValueError):
            ran = 0
        lead = ("Claude never returned a verdict" if ran else
                "Claude was never able to start on this")
        msg = (
            f"🛑 <b>ENTRY DROPPED</b>  <code>{html.escape(str(t.get('stock')))}</code> "
            f"({html.escape(str(t.get('direction')))})\n"
            f"{lead} — {html.escape(str(why))}.\n"
            f"<i>Not entered. No rush: the setup re-qualifies if it still "
            f"holds tomorrow.</i>")
        # ANNOUNCE FIRST, then cancel. The whole justification for parking
        # entries instead of taking them is "the halt cannot be silent" — and
        # cancelling first made silence permanent: the status filter above
        # excludes cancelled rows, so a Telegram lost to a network blip or an
        # HTML 400 could never be retried. Sending first means a failed send
        # leaves the row STARVED-but-uncancelled, and the next cycle tries
        # again. A duplicate message is a far cheaper failure than a silent
        # trading halt.
        if not _send_telegram(msg, dry_run=dry_run):
            logger.error('ENTRY DROPPED #%d %s — alert FAILED to send, left '
                         'uncancelled for retry next cycle',
                         t['id'], t.get('stock'))
            continue
        try:
            store.cancel(t['id'], f'vet starved: {why}')
        except Exception as e:
            logger.error('Could not cancel starved #%d: %s', t['id'], e)
            continue
        reaped.append(t['id'])
        logger.warning('ENTRY DROPPED #%d %s — %s', t['id'], t.get('stock'), why)
    return reaped


# ── Cycle ─────────────────────────────────────────────────────────────────

def run_cycle(store: ZebraStore, kite, dry_run: bool = False,
              do_scan: bool = True) -> None:
    """One full cycle: scan + check watching + check entered."""
    # Before anything else: did the store quarantine a corrupt file? That path
    # empties the book, and an empty book makes `check_entered` return at its
    # first line — so the ONE event that stops all exit monitoring is the one
    # event `_alert_monitoring_blind` structurally cannot report.
    _alert_store_corruption(dry_run=dry_run)
    # M3. Beside the corruption check and for the same reason: both are inputs
    # that fail in a way the trading code reads as "nothing to do". Never
    # allowed to raise into the cycle — an input-freshness check that can stop
    # exit monitoring would be a worse bug than the one it reports.
    try:
        _alert_options_csv_stale(dry_run=dry_run)
    except Exception as e:
        logger.error("Options-CSV freshness check failed: %s", e)
    # Which engine may BOOK, stated out loud before anything trades. The
    # illegal combinations used to live only in a CLAUDE.md table -- see
    # `common/arming.py` for why a document could not do this job.
    try:
        _arming_preflight(store, dry_run=dry_run)
    except Exception as e:
        logger.error("Arming preflight failed: %s", e, exc_info=True)
    # The NSE holiday list is DATA and it runs out. Beside the other two
    # input-freshness checks, because it fails the same way they do: the
    # session count keeps answering, and the answer quietly moves every
    # delivery close later, into the margin ramp.
    try:
        _alert_calendar_coverage(dry_run=dry_run)
    except Exception as e:
        logger.error("Calendar coverage check failed: %s", e)
    # Say which portfolio limits are ARMED, every cycle. An unset rupee cap
    # behaves exactly like a working one right up to the moment it should have
    # refused something, and this system has already shipped two controls that
    # were wired in, looked deployed and could never fire.
    # WHOLE BOOK: see `_capital_context` — deployed capital is deployed.
    logger.info(capital.describe(store.load_trades()))
    # FIRST, before anything can read a vet state: fail open any vet that blew
    # its deadline. This is the guard that stops a Claude outage (crash, hung
    # CLI, expired auth, Pi reboot) from quietly parking signals in `pending`
    # forever — i.e. from becoming a silent trading halt. It must run on the
    # entrypoint that ACTUALLY executes, which is this one; check_watching
    # relies on it having already run.
    if cfg.VET_ENABLED:
        try:
            expired = vet_mod.expire_stale(store)
            if expired:
                logger.warning("VET sweep: %d entry signal(s) requeued or "
                               "dropped: %s", len(expired), expired)
        except Exception as e:
            logger.error("Vet expiry sweep failed: %s", e, exc_info=True)
        try:
            _reap_starved_vets(store, dry_run=dry_run)
        except Exception as e:
            logger.error("Starved-vet sweep failed: %s", e, exc_info=True)
    # ── EXITS FIRST. This ordering is a rate-limit control, not a tidy-up ──
    # Kite allows ONE quote-family request per second and answers the rest with
    # a 429 that opens a 10-second sliding cooldown. On 2026-08-27 the scanner
    # ran first, spent ~20 seconds burning historical and LTP calls on ~49
    # discretionary candidates, and `check_entered` then asked for the spot of
    # all 9 OPEN POSITIONS and was refused — MONITORING BLIND at 14:40:36,
    # three seconds after the scan finished. Exit monitoring is the only phase
    # here that can lose money by not running; scanning a candidate five
    # minutes later costs nothing (`feedback_no_rush_to_enter`). So the phase
    # that manages open risk now takes the budget first and the discretionary
    # phases take what is left.
    # M12. Arm the in-line vet wait for THIS cycle only. A verdict requested
    # here otherwise sits on disk until the next 5-minute tick -- ~3 minutes of
    # a measured ~4m50s round trip spent doing nothing while a stop is fired
    # and unfilled. The budget is per CYCLE, not per trade, so several
    # triggering positions cannot push this cron past its own interval; the
    # `finally` is what keeps it from leaking into the next one.
    vet_mod.start_incycle_budget()
    try:
        check_entered(store, kite, dry_run=dry_run)
    except Exception as e:
        logger.error("Entered cycle failed: %s", e, exc_info=True)
    finally:
        vet_mod.end_incycle_budget()
    if do_scan:
        try:
            validate_and_add(store, kite=kite, dry_run=dry_run)
        except Exception as e:
            logger.error("Scanner cycle failed: %s", e, exc_info=True)
    # M2. `check_watching` is where entries are placed, and a slow multi-lot
    # entry used to be able to run the cycle past the 5-minute cron interval --
    # whose `flock -n` then SKIPS the next run, so the phase that manages open
    # risk does not execute for ten minutes because an entry was slow. The
    # EXITS-FIRST ordering above fixed the within-cycle half of that; this
    # bounds the across-cycle half.
    start_entry_phase()
    try:
        check_watching(store, kite, dry_run=dry_run)
    except Exception as e:
        logger.error("Watching cycle failed: %s", e, exc_info=True)
    finally:
        end_entry_phase()
    # Everything below is OBSERVATION, never trading. It runs last and each
    # piece is independently caught, so a failure in the learning/monitoring
    # half can never stop the half that trades.
    if cfg.VET_ENABLED:
        _run_vet_side_channels(store, kite, dry_run=dry_run)
    else:
        # The corporate-action guard is a SAFETY interlock, not a vetting
        # opinion: on a bonus/split ex-date the exchange re-prices the
        # underlying and adjusts the strikes, so yesterday's sl_spot is
        # breached by an event in which nothing went wrong. But the calendar it
        # reads has exactly ONE writer, and that writer lived inside the vet
        # side-channels — which ship disabled. So the guard was wired into
        # check_entered, looked deployed, and could never fire, because the
        # file it consults was never written. Safety interlocks do not get to
        # depend on an optional subsystem being switched on.
        try:
            _refresh_events_if_stale(store)
        except Exception as e:
            logger.error("Event calendar refresh failed: %s", e, exc_info=True)


def _run_vet_side_channels(store, kite, dry_run: bool = False) -> None:
    """Scoring, position review, event calendar and auth watch.

    Split out so the trading path above reads as one page, and so tests can
    drive these without a full cycle. None of it can open or close a position.
    """
    # LTPs for every symbol we are still tracking: open positions AND vetoed
    # signals whose shadow is still running. One batched call, as elsewhere.
    try:
        symbols = {t['stock'] for t in store.get_entered()}
        # WHOLE BOOK: a symbol set for quoting. Over-fetching a quote is
        # free; missing one blinds a position.
        symbols |= {t['stock'] for t in store.load_trades()
                    if isinstance(t.get('veto_shadow'), dict)
                    and t['veto_shadow'].get('status') == 'open'}
        ltps = get_ltp(kite, sorted(symbols)) if symbols else {}
    except Exception as e:
        logger.error("Vet side-channel LTP fetch failed: %s", e)
        ltps = {}

    for label, fn in (
        ('veto shadows', lambda: outcomes_mod.track_shadows(store, ltps)),
        ('outcome join', lambda: outcomes_mod.join(store)),
        ('position review', lambda: review_mod.run(
            store, ltps, send=_send_telegram, dry_run=dry_run)),
        ('event calendar', lambda: _refresh_events_if_stale(store)),
        # Runs AFTER the outcome join: a veto shadow that resolved this very
        # cycle should be post-mortemed today, not tomorrow. `due` caps it at
        # one spawn a day and returns False when nothing has settled, so on a
        # quiet day this costs a list comprehension.
        ('post-mortem batch', lambda: _run_postmortem_batch(store, dry_run)),
        ('auth watch', lambda: health_mod.check(send=_send_telegram,
                                                dry_run=dry_run)),
    ):
        try:
            fn()
        except Exception as e:
            logger.error("Vet side-channel '%s' failed: %s", label, e,
                         exc_info=True)


def _run_postmortem_batch(store, dry_run: bool = False) -> bool:
    """Spawn the EOD post-mortem agent if anything settled and it is due."""
    if not postmortem_mod.due(store):
        return False
    n = len(postmortem_mod.pending(store))
    logger.info("POST-MORTEM batch due: %d settled decision(s)", n)
    return postmortem_mod.spawn_batch(store, spawn=not dry_run)


def _refresh_events_if_stale(store) -> bool:
    """Kick the calendar agent when the file has aged out."""
    if not events_mod.is_stale():
        return False
    symbols = sorted({t['stock'] for t in store.get_entered()}
                     # WHOLE BOOK: symbol set, as above.
                     | {t['stock'] for t in store.load_trades()
                        if t.get('status') == 'triggered'})
    logger.info("Event calendar stale — refreshing (%d symbols)", len(symbols))
    return events_mod.refresh(symbols)


def run_once(dry_run: bool = False) -> None:
    """Single cycle target (cron-friendly). Exits if market closed."""
    if not _is_market_open():
        logger.info("Market closed, skipping cycle")
        return
    # CYCLE BOUNDARIES. cron appends every run to one file and the process
    # exits between them, so without a marker there is nothing separating one
    # cycle's lines from the next — and a cycle that died halfway is
    # indistinguishable from one that had nothing to say. The duration is the
    # cheap early warning for the 5-minute cron overlapping itself.
    t0 = time.time()
    logger.info("=== CYCLE START %s ===",
                datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S'))
    ok = False
    try:
        kite = _get_kite()
        store = get_store()
        run_cycle(store, kite, dry_run=dry_run, do_scan=True)
        ok = True
    finally:
        logger.info("=== CYCLE %s in %.1fs ===",
                    'END' if ok else 'ABORTED', time.time() - t0)


def run_loop(dry_run: bool = False) -> None:
    """Long-running loop. Sleeps until market open, polls every monitor_interval_sec."""
    kite = _get_kite()
    store = get_store()
    last_scan = 0.0
    logger.info("Zebra monitor loop starting")
    while True:
        if not _is_market_open():
            now = datetime.now(IST)
            # Past market close — exit cleanly
            if now.hour > cfg.MARKET_CLOSE[0] or \
               (now.hour == cfg.MARKET_CLOSE[0] and now.minute >= cfg.MARKET_CLOSE[1]):
                logger.info("Market closed for the day, exiting loop")
                return
            time.sleep(30)
            continue
        now_ts = time.time()
        do_scan = (now_ts - last_scan) >= cfg.SCAN_INTERVAL_SEC
        run_cycle(store, kite, dry_run=dry_run, do_scan=do_scan)
        if do_scan:
            last_scan = now_ts
        try:
            store.maybe_sync()
        except Exception:
            pass
        time.sleep(cfg.MONITOR_INTERVAL_SEC)

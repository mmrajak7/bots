"""Zebra monitor — main loop.

Three jobs per cycle:
  1. Scan Chartink + add new WATCHING signals
  2. Check WATCHING signals: if gap <= TRIGGER_GAP_MAX, run strike analyzer
     and send ENTER alert with 2-3 candidate pairs (status → triggered).
     If gap >= WATCH_GAP_MAX*1.2 (drifted away), cancel.
  3. Check ENTERED trades: TP / SPOT SL / DEBIT SL / TIME alerts.

User executes manually and uses `zebra enter ID ...` / `zebra close ID ...`.
"""

from __future__ import annotations

import html
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from . import config as cfg
from . import events as events_mod
from . import health as health_mod
from . import history
from . import mfe as mfe_mod
from . import outcomes as outcomes_mod
from . import postmortem as postmortem_mod
from . import review as review_mod
from . import strikes as strikes_mod
from . import vet as vet_mod
from .scanner import _get_kite, get_ltp, compute_st_for_stock, validate_and_add
from .trade_store import ZebraStore, get_store

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

    # Honor zebra_config.json telegram.enabled flag
    try:
        with open(cfg.CONFIG_FILE) as f:
            zcfg = json.load(f)
        if zcfg.get('telegram', {}).get('enabled') is False:
            logger.debug("Telegram disabled in zebra_config.json")
            return True
    except Exception:
        pass

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
        return resp.status_code == 200
    except Exception as e:
        logger.debug("Telegram send failed: %s", e)
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
    """
    if not cfg.PAPER_MODE:
        return True
    struct = 'bcs' if trade.get('structure') == 'bcs' else 'zebra'
    return struct in cfg.ALERT_STRUCTURES


def _format_enter_alert(trade: dict, analysis: dict,
                        bcs: Optional[dict] = None) -> str:
    """Build the ENTER alert. Single recommended Zebra pair (click-copy ready).

    Picker chose this pair as the best balance of: passes liquidity gates,
    NetExt ≤ 0 (theta-OK), BE near spot, lowest capital_per_lot.
    """
    stock = trade['stock']
    direction = trade['direction']
    spot = analysis['spot']
    st_val = trade['st_value']
    # Magnet gap: how far the price is from the ST attractor.
    gap = analysis.get('current_gap_pct', abs(spot - st_val) / st_val * 100)
    expiry = analysis['expiry']
    dte = analysis['dte']
    lot_size = analysis['lot_size']
    pull_dir = 'up to' if direction == 'CE' else 'down to'

    best = analysis.get('best')
    if not best:
        # No tradeable pair (all candidates failed gates). Surface this clearly.
        return (
            f"⚠ <b>ZEBRA NO-PAIR</b>  <code>{stock}</code> ({direction})\n"
            f"spot {spot:,.2f} | Level {st_val:,.2f} | gap {gap:.2f}%\n"
            f"Strike analyzer found no viable (K_L,K_S) at expiry {expiry} "
            f"— OI/spread/regime gates all failed."
        )

    k_l = int(best['k_l']) if best['k_l'].is_integer() else best['k_l']
    k_s = int(best['k_s']) if best['k_s'].is_integer() else best['k_s']
    # escape: gate tags contain '<' (long_OI<5000) — a bare '<' makes
    # Telegram's HTML parser reject the whole message.
    warn = ' ⚠ ' + html.escape(','.join(best['gate_fails'])) \
        if best['gate_fails'] else ''

    conviction = ' ⭐ALIGNED' if cfg.is_trend_aligned(
        trade.get('direction'), trade.get('st_direction')) else ''

    msg = (
        f"\U0001F993 <b>ENTER</b>  <code>{stock}</code>  ({direction}){conviction}\n"
        f"Level {st_val:,.2f} | spot {spot:,.2f} | gap {gap:.2f}% "
        f"({pull_dir} Level)\n"
        f"expiry {expiry} ({dte} DTE) | lot {lot_size} | "
        f"Capital (1 lot) = {best['capital_per_lot']:,.0f}{warn}\n"
        f"\n"
        f"Strikes <b>{k_l} / {k_s}</b>   debit {best['debit']:g} | "
        f"BE {best['be']:,.2f} ({best['be_pct_from_spot']:+.2f}%)\n"
        f"\n"
        f"🟢 BUY 2× <code>{best['long_symbol']}</code>  {best['long_ask']:g}\n"
        f"🔴 SELL 1× <code>{best['short_symbol']}</code>  {best['short_bid']:g}"
    )
    if bcs:
        warn = ' ⚠ ' + html.escape(','.join(bcs['warnings'])) \
            if bcs.get('warnings') else ''
        msg += (
            f"\n\n📐 <b>BCS shadow</b> (paper A/B): "
            f"{bcs['long_strike']:g}/{bcs['short_strike']:g}  "
            f"debit {bcs['debit']:g} ({bcs['debit_to_width_pct']:.0f}% of width) | "
            f"maxP {bcs['max_profit_per_share']:g}{warn}\n"
            f"🟢 BUY 1× <code>{bcs['long_symbol']}</code>  {bcs['long_ask']:g}\n"
            f"🔴 SELL 1× <code>{bcs['short_symbol']}</code>  {bcs['short_bid']:g}"
        )
    return msg


def _exit_cleared(store, trade: dict, kind: str, quote: dict, spot: float,
                  dry_run: bool = False) -> bool:
    """True if this exit may fire now. False = wait or hold.

    Called BEFORE `set_alert_flag` on every price-driven exit, because that
    flag is consume-once and burning it on an exit that does not execute
    strands the exit permanently.

    TIME exits are deliberately NOT gated: they are calendar-driven rather than
    quote-driven, and their flag re-arms daily, so a bad mid there costs one
    day of paper accounting instead of a stranded position.
    """
    gate = vet_mod.exit_gate(store, trade, kind, quote, spot)
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
        f"<i>HOLDING — max loss is the debit and already capped. "
        f"Close manually with <code>zebra close {trade.get('id')}</code> "
        f"if you disagree.</i>"
    )


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
    lines.append("<i>No entry — neither zebra nor the BCS shadow.</i>")
    return "\n".join(lines)


def _vet_context(trade: dict, analysis: dict, gap_pct: float,
                 kite=None) -> dict:
    """The evidence bundle handed to the vetting agent.

    Snapshotted here rather than re-quoted by the agent so its verdict judges
    exactly the book the bot acted on. Live re-quoting is a step INSIDE the
    agent's checklist, not part of the handoff.
    """
    best = analysis.get('best') or {}
    # Both of these read candles. They are wrapped because a statistics or
    # chart failure must never stop a signal being vetted — a missing section
    # is a gap in the evidence, an exception here is a halted pipeline.
    try:
        attraction = history.attraction(kite, trade['stock'],
                                        trade.get('timeframe'),
                                        trade.get('direction'))
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
    bcs_ctx = None
    if cfg.ENTRY_STRUCTURE == 'bcs':
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
    return {
        'structure': cfg.ENTRY_STRUCTURE,
        'bcs': bcs_ctx,
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
        'zebra': {k: best.get(k) for k in
                  ('k_l', 'k_s', 'debit', 'be', 'be_pct_from_spot',
                   'long_symbol', 'short_symbol', 'long_oi', 'short_oi',
                   'long_bid', 'long_ask', 'short_bid', 'short_ask',
                   'long_spread_pct', 'short_spread_pct',
                   'long_extrinsic', 'short_extrinsic', 'net_ext',
                   'liquidity_ok', 'capital_per_lot', 'gate_fails')},
        # Explicit, because the agent's instructions used to say these gates
        # "passed by construction". Under 'zebra' that is true for most picks
        # but `_pick_best` has a last-resort tier that returns a candidate WITH
        # failed gates. Under 'bcs' the zebra pair is not traded at all, so
        # None ("not applicable") is the honest value — an empty `best` would
        # otherwise compute to True and hand the agent an all-clear about a
        # structure nobody is opening.
        'gates_all_passed': (None if cfg.ENTRY_STRUCTURE == 'bcs'
                             else not (best.get('gate_fails') or [])),
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
    if cfg.VET_ENABLED and not cfg.PAPER_MODE \
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
    if cfg.VET_ENABLED and not cfg.PAPER_MODE:
        store.clear_alert_flag(trade['id'], 'vet_enter')
        logger.error("Deferred ENTER ticket for #%d %s released — "
                     "retrying next cycle", trade['id'], stock)


def _enter_as_bcs(store: ZebraStore, kite, trade: dict, analysis: dict,
                  price: float, dry_run: bool = False):
    """Build and open a first-class BCS from a triggered signal.

    Returns:
      None            — skipped; the signal stays 'triggered' and the
                        drift/stale checks clean it up, as the zebra path does
      (bcs, trade)    — PAPER: entered, `trade` is the fresh record
      (bcs, None)     — LIVE: nothing entered, the alert IS the order ticket

    The three-way return is deliberate. An earlier version returned None for
    both "skipped" and "live", and the caller's `continue` then suppressed
    every entry alert in LIVE mode — where the alert is the only way a trade
    ever gets placed. Auto-entry is a paper-mode behaviour; alerting is not.

    Never raises into the cycle: one bad chain must not stop the other
    positions being monitored.
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
        return bcs, None       # alert-only; the alert is the order ticket

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
    logger.warning("BCS SUPPRESSED #%d %s (%s): %s — no trade; zebra entered "
                   "silently for the A/B record", trade['id'], trade['stock'],
                   trade['direction'], reason or "no viable BCS pair")


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
        f"📐 <b>ENTER BCS</b>  <code>{stock}</code>  ({direction}){conviction}\n"
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
        f"🟢 BUY 1× <code>{bcs['long_symbol']}</code>  {bcs['long_ask']:g}\n"
        f"🔴 SELL 1× <code>{bcs['short_symbol']}</code>  {bcs['short_bid']:g}"
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


def _format_tp_alert(trade: dict, spot: float, mid: Optional[float] = None) -> str:
    paper = _paper_close_line(trade, mid)
    return (
        f"\U0001F3AF <b>{_struct_label(trade)} TP</b>  {trade['stock']} ({trade['direction']})\n"
        f"spot {spot:,.2f} hit TP {trade['tp_spot']:,.2f}\n"
        f"Long: <code>{trade['long_symbol']}</code>\n"
        f"Short: <code>{trade['short_symbol']}</code>{paper}"
    )


def _format_spot_sl_alert(trade: dict, spot: float, mid: Optional[float] = None) -> str:
    paper = _paper_close_line(trade, mid)
    return (
        f"\U0001F6D1 <b>{_struct_label(trade)} SPOT SL</b>  {trade['stock']} ({trade['direction']})\n"
        f"spot {spot:,.2f} hit SL {trade['sl_spot']:,.2f}\n"
        f"Adverse move from entry {trade['entry_spot']:,.2f}{paper}"
    )


def _format_debit_sl_alert(trade: dict, mid: float) -> str:
    paper = _paper_close_line(trade, mid)
    pct_lost = (1 - mid / trade['debit']) * 100 if trade.get('debit') else 0
    return (
        f"\U0001F4C9 <b>{_struct_label(trade)} DEBIT SL</b>  {trade['stock']} ({trade['direction']})\n"
        f"Mid {mid:.2f} ≤ debit-SL {trade['debit_sl_value']:.2f} "
        f"(entry debit {trade['debit']:.2f})\n"
        f"Lost ~{pct_lost:.0f}% of debit.{paper}"
    )


def _format_trail_alert(trade: dict, mid: float, tl: dict) -> str:
    """Trail-stop exit. Always a PROFIT — the level sits above the entry debit
    by construction — so the alert leads with what was kept, not with a stop."""
    paper = _paper_close_line(trade, mid)
    kept = (mid - trade['debit']) * int(trade.get('quantity') or 0)
    return (
        f"\U0001F512 <b>{_struct_label(trade)} TRAIL</b>  "
        f"<code>{trade['stock']}</code> ({trade['direction']})\n"
        f"Mid {mid:.2f} ≤ trail {tl['level']:.2f} "
        f"(peak {trade['debit'] + tl['peak_gain']:.2f}, "
        f"{tl['peak_pct_of_max']:.0f}% of max)\n"
        f"Locking in ~Rs {kept:,.0f} of a peak Rs "
        f"{tl['peak_gain'] * int(trade.get('quantity') or 0):,.0f}.{paper}"
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


def _format_blind_alert(trade: dict, reason: Optional[str]) -> str:
    """One-shot warning that DEBIT-SL valuation has been blind (unreliable /
    missing book) long enough to matter. Spot TP/SL stay armed — this is pure
    observability, not a trading action."""
    mins = cfg.DEBIT_BLIND_CYCLES * cfg.MONITOR_INTERVAL_SEC // 60
    return (
        f"⚠ <b>{_struct_label(trade)} DEBIT-BLIND</b>  "
        f"<code>{trade['stock']}</code> ({trade['direction']})\n"
        f"Debit-SL valuation blind ~{mins} min: option book unreliable "
        f"({reason or 'no quote'}).\n"
        f"Spot TP/SL still armed. Check the book manually before acting."
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
        stock = trade['stock']
        price = ltps.get(stock, 0)
        if price <= 0:
            continue
        st_val = trade['st_value']
        gap = (price - st_val) / st_val if trade['direction'] == 'PE' \
            else (st_val - price) / st_val
        gap_pct = round(gap * 100, 2)
        store.update_gap(trade['id'], gap_pct)

        # Drift cancel: if gap blew past watch band by 20%
        if gap > cfg.WATCH_GAP_MAX * 1.2:
            try:
                store.cancel(trade['id'],
                             f'drift: gap {gap_pct:.2f}% > watch+20%')
            except ValueError:
                pass
            continue

        # Stale (too close): gap fell below stale_min → past entry zone
        if gap < 0:
            # Crossed the line. For CE-Zebra (price < ST, gap = (ST-price)/ST),
            # negative gap means price overshot above ST. Likely already moving
            # toward target. Cancel as we missed the entry window.
            try:
                store.cancel(trade['id'],
                             f'crossed: gap {gap_pct:.2f}% (past ST)')
            except ValueError:
                pass
            continue

        if gap > cfg.TRIGGER_GAP_MAX:
            # Not yet in trigger zone; just keep watching
            continue
        if gap < cfg.STALE_GAP_MIN:
            # In stale zone: too late
            try:
                store.cancel(trade['id'],
                             f'stale: gap {gap_pct:.2f}% < {cfg.STALE_GAP_MIN*100:.1f}%')
            except ValueError:
                pass
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
                continue
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
            # ALLOWED / UNAVAILABLE → enter now (re-running the analyzer for a
            # fresh book; entry drift ≤ one tick is the accepted cost of
            # vetting). None → the vet request never landed (crash after
            # mark_triggered): fall through so the gate below re-requests it —
            # recoverable instead of parked until drift-cancel.
            if state in (vet_mod.ALLOWED, vet_mod.UNAVAILABLE) \
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
        # Require what the structure we ACTUALLY trade needs. Under BCS-only
        # the zebra pair is never opened, so gating the whole signal on a
        # tradeable zebra `best` would let the zebra's constraints
        # (net-extrinsic, deep-ITM liquidity) veto a spread that shares none of
        # them — a retired structure still deciding which trades happen.
        if cfg.ENTRY_STRUCTURE == 'bcs':
            atm_q = analysis.get('atm_quote') or {}
            if not analysis.get('atm_strike') or not atm_q.get('mid'):
                logger.info("No usable ATM book for %s, leaving in watching",
                            stock)
                continue
        elif not analysis.get('best'):
            logger.info("No tradeable best pick for %s, leaving in watching", stock)
            continue

        analysis['current_gap_pct'] = gap_pct
        # Store just the best pick + ranked list for traceability. Under
        # BCS-only these are zebra pairs nobody will trade, so the record
        # carries none rather than a list that reads like a shortlist.
        best = analysis.get('best')
        alert_strikes = [] if cfg.ENTRY_STRUCTURE == 'bcs' or not best else \
            [best] + [c for c in analysis.get('candidates', [])
                      if (c['k_l'], c['k_s']) != (best['k_l'], best['k_s'])]
        if trade['status'] == 'watching':
            try:
                store.mark_triggered(trade['id'], price, gap_pct, alert_strikes)
            except ValueError as e:
                logger.warning("mark_triggered failed for #%d: %s", trade['id'], e)
                continue

        # ── Claude vetting gate ──────────────────────────────────────────
        # A triggered signal waits for a verdict before it enters. The vet is
        # requested once; the spawned CLI is never waited on, so this cycle
        # returns immediately and the entry happens on a later tick.
        #
        # EVERY branch that is not an explicit ALLOW must still let the trade
        # through eventually — `unavailable` (the fail-open timeout) reads as
        # "enter unvetted", because a vetting outage must never become a
        # silent trading halt. Only an explicit VETO stops the entry.
        if cfg.VET_ENABLED:
            state = vet_mod.vet_state(store.find(trade['id']))
            if state is None:
                try:
                    vet_mod.request_entry_vet(
                        store, trade['id'],
                        context=_vet_context(trade, analysis, gap_pct, kite))
                except ValueError as e:
                    # The locked re-check saw a state this cache missed —
                    # already requested or already SETTLED (possibly a veto).
                    # Never enter on a guess; the next cycle reads the real
                    # state (request_entry_vet's refresh updated the cache).
                    logger.warning("VET request refused for #%d: %s — "
                                   "re-reading next cycle", trade['id'], e)
                    continue
                except Exception as e:
                    # Infra failure (lock timeout, IO). Requesting the vet must
                    # never block trading: fail open and enter unvetted THIS
                    # cycle, exactly as the bot behaved before this layer.
                    logger.error("VET request failed for #%d: %s — proceeding "
                                 "unvetted", trade['id'], e)
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
            # ALLOWED or UNAVAILABLE fall through and enter below.

        # PAPER mode: auto-record the entry FIRST, then alert — so the ENTER
        # alert only goes out for a position that actually opened. If the fill
        # is rejected we leave the signal in 'triggered' (it self-heals via the
        # drift/stale-cancel checks next cycle); we deliberately do NOT cancel
        # here, because a 'cancelled' record isn't deduped by the scanner and
        # would be re-added + re-alerted every scan (alert churn).
        bcs = None
        bcs_skip_reason = None      # why a gate suppressed the shadow, if it did

        # ── BCS-only pipeline (2026-08-12) ──────────────────────────────
        # One record, no zebra leg, no shadow. The strike analyzer still runs
        # because it owns expiry selection, lot size and the ATM book — but a
        # BCS is built from `atm_quote` at the TOP level of that result, not
        # from the zebra's recommended pair, so the zebra's own gates
        # (net-extrinsic, deep-ITM liquidity) can no longer veto a spread that
        # shares none of those constraints.
        if cfg.ENTRY_STRUCTURE == 'bcs':
            built = _enter_as_bcs(store, kite, trade, analysis, price,
                                  dry_run=dry_run)
            if built is None:
                continue
            bcs, fresh = built
            # `fresh` is None in LIVE mode (nothing entered). The alert still
            # goes out — it is the order ticket, and _alerts_enabled always
            # returns True when paper mode is off for exactly that reason.
            target = fresh or trade
            if _alerts_enabled(target):
                msg = _format_bcs_enter_alert(target, analysis, bcs)
                if cfg.VET_ENABLED:
                    msg += _vet_line(store.find(trade['id']) or target)
                _send_enter_alert(store, trade, msg, stock, dry_run=dry_run)
            else:
                logger.info("ENTER alert suppressed for #%d %s "
                            "(alert_structures=%s)", trade['id'], stock,
                            cfg.ALERT_STRUCTURES)
            continue

        if cfg.PAPER_MODE:
            best = analysis.get('best')
            if not best:
                continue
            try:
                store.mark_entered(trade['id'], {
                    'long_strike': best['k_l'],
                    'short_strike': best['k_s'],
                    'long_symbol': best['long_symbol'],
                    'short_symbol': best['short_symbol'],
                    'debit': best['debit'],
                    'lot_size': best['lot_size'],
                    'lots': 1,
                    'expiry': analysis['expiry'],
                    'entry_spot': price,
                    # Feeds the intrinsic-floor quote-sanity guard
                    'short_extrinsic_entry': best['short_extrinsic'],
                })
                logger.info("PAPER auto-entered #%d %s %d/%d debit=%.2f",
                            trade['id'], stock,
                            int(best['k_l']), int(best['k_s']), best['debit'])
            except Exception as e:  # broad: bad data OR persist/IO failure
                logger.error("PAPER auto-enter failed for #%d %s: %s — left "
                             "triggered (will drift-cancel), no alert sent",
                             trade['id'], stock, e)
                continue

            # Shadow BCS (paper A/B): buy the same ATM strike zebra shorts,
            # sell the strike nearest the ST target. Best-effort — a failure
            # here never blocks the zebra flow or its alert.
            if cfg.BCS_PAPER_ENABLED:
                try:
                    bcs = strikes_mod.analyze_bcs(
                        kite, stock, trade['direction'], price,
                        target_spot=trade['st_value'],
                        expiry=analysis['expiry'],
                        atm_strike=best['k_s'],
                        atm_quote={'mid': best['short_mid'],
                                   'bid': best['short_bid'],
                                   'ask': best['short_ask'],
                                   'oi': best['short_oi']},
                        lot_size=best['lot_size'],
                    )
                    if bcs.get('error'):
                        logger.warning("BCS shadow skipped for #%d %s: %s",
                                       trade['id'], stock, bcs['error'])
                        bcs_skip_reason = bcs['error']
                        bcs = None
                    else:
                        bcs['expiry'] = analysis['expiry']
                        bcs['entry_spot'] = price
                        store.add_bcs_shadow(store.find(trade['id']), bcs)
                except Exception as e:
                    logger.error("BCS shadow failed for #%d %s: %s",
                                 trade['id'], stock, e)
                    # Attribute honestly: this is a crash (or a persist
                    # failure AFTER a clean analysis), not a gate rejection —
                    # the alert must not imply the pair was unviable.
                    bcs_skip_reason = f"shadow build failed: {e}"
                    bcs = None

        # Who talks on Telegram (both structures auto-trade regardless):
        #   zebra in alert_structures  -> classic zebra alert (+BCS block if on)
        #   bcs only                   -> BCS-led alert, ONLY when there is a
        #                                 tradeable pair. A gated shadow is a
        #                                 non-event: it goes to the log, never
        #                                 to Telegram (user's call 2026-08-10 —
        #                                 one notification per rejected signal
        #                                 is the alert fatigue the gates exist
        #                                 to cure).
        # LIVE-mode override: the zebra ENTER alert is the user's order
        # ticket — with no paper auto-entry it must never be suppressed.
        send_zebra = 'zebra' in cfg.ALERT_STRUCTURES or not cfg.PAPER_MODE
        send_bcs = 'bcs' in cfg.ALERT_STRUCTURES
        if bcs is None and bcs_skip_reason:
            _log_bcs_suppressed(trade, bcs_skip_reason)
        if send_zebra:
            msg = _format_enter_alert(trade, analysis,
                                      bcs=bcs if send_bcs else None)
        elif send_bcs and bcs:
            msg = _format_bcs_enter_alert(trade, analysis, bcs)
        else:
            msg = None
        # One signal, one alert: an ALLOW rides on the ticket already being
        # sent rather than firing a second notification. Read from the FRESH
        # record — `trade` predates the verdict that let us reach this line.
        if msg is not None and cfg.VET_ENABLED:
            msg += _vet_line(store.find(trade['id']) or trade)
        if msg is None:
            # Two distinct reasons land here; say which, or a gated signal
            # looks like a config problem when reading the log later.
            if bcs is None and bcs_skip_reason:
                logger.info("No ENTER alert for #%d %s — shadow gated "
                            "(logged above), zebra silenced",
                            trade['id'], stock)
            else:
                logger.info("ENTER alert suppressed for #%d %s "
                            "(alert_structures=%s)", trade['id'], stock,
                            cfg.ALERT_STRUCTURES)
            continue
        _send_enter_alert(store, trade, msg, stock, dry_run=dry_run)


# ── Entered → TP/SL/Time ─────────────────────────────────────────────────

def _long_multiplier(trade: dict) -> int:
    """2 long legs for zebra, 1 for a BCS shadow."""
    return 1 if trade.get('structure') == 'bcs' else 2


def _intrinsic_floor(trade: dict, spot: float) -> Optional[float]:
    """Arbitrage-floor for the structure value at the given spot, minus a
    generous extrinsic allowance for the short leg.

    A quoted structure mid below this is a bad quote (stale/one-sided book on
    the illiquid ITM leg), not a real price — July 2026: ABB #242 booked a
    -50% debit-SL exit at mid 335 when pure intrinsic at the recorded spot
    was 1,020. Returns None if the floor can't be computed.
    """
    try:
        k_l = float(trade['long_strike'])
        k_s = float(trade['short_strike'])
        mult = _long_multiplier(trade)
        if trade['direction'] == 'CE':
            intr = mult * max(spot - k_l, 0.0) - max(spot - k_s, 0.0)
        else:
            intr = mult * max(k_l - spot, 0.0) - max(k_s - spot, 0.0)

        # Short-leg extrinsic allowance: the structure can legitimately trade
        # below pure intrinsic by up to the short leg's time value. Use the
        # entry-time value (extrinsic peaks ATM ≈ entry) with 1.5× headroom
        # for IV spikes; fall back to the triggered alert pair, then to 30%
        # of the entry debit for pre-guard trades.
        allowance = trade.get('short_extrinsic_entry')
        if allowance is None:
            for p in trade.get('alert_strikes') or []:
                if (abs(float(p.get('k_l', -1)) - k_l) < 1e-6
                        and abs(float(p.get('k_s', -1)) - k_s) < 1e-6):
                    allowance = p.get('short_extrinsic')
                    break
        if allowance is None:
            allowance = 0.3 * trade.get('debit', 0)
        return round(intr - 1.5 * float(allowance), 2)
    except Exception:
        return None


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
    floored = False
    if spot is not None and spot > 0:
        floor = _intrinsic_floor(trade, spot)
        if floor is not None and mid < floor:
            logger.warning(
                "QUOTE GUARD #%d %s: structure mid %.2f < intrinsic floor "
                "%.2f at spot %.2f — clamping to floor (bad ITM quote)",
                trade['id'], trade['stock'], mid, floor, spot)
            mid = floor
            floored = True
    return {'mid': mid, 'reliable': reliable, 'reason': reason,
            # PER-LEG BOOK. VETTING.md tells the exit agent to judge "depth at
            # touch and the spread as a % of mid", and this dict used to carry
            # only mid/reliable/reason — so the agent was asked to judge the one
            # thing it could not see, exactly the defect the ENTRY context had
            # already been fixed for. `floored` matters too: a clamped mid is a
            # number the market never quoted, and a verdict about "is this price
            # real" must know it is looking at a floor, not a bid.
            'legs': {
                'long': _leg_book(trade.get('long_symbol'), long_q),
                'short': _leg_book(trade.get('short_symbol'), short_q),
            },
            'floored': floored}


def _leg_book(symbol, q: dict) -> dict:
    """One leg's top-of-book, in the shape the vetting agent is asked to judge."""
    bid, ask, mid_ = q.get('bid'), q.get('ask'), q.get('mid')
    spread_pct = None
    if bid is not None and ask is not None and mid_:
        spread_pct = round((ask - bid) / mid_ * 100, 1)
    return {'symbol': symbol, 'bid': bid, 'ask': ask, 'mid': mid_,
            'oi': q.get('oi'), 'last': q.get('last'),
            'spread_pct': spread_pct,
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

    Weekdays only. Returns 0 on or after expiry day. Holidays are not known to
    this repo, so a holiday inside the window makes this an OVER-estimate —
    it reports more sessions than really remain, never fewer.
    """
    if expiry <= today:
        return 0
    sessions, cur = 0, today
    while cur < expiry:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            sessions += 1
    return sessions


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
                       pending_mfe: Optional[dict] = None) -> Optional[dict]:
    """Auto-close a paper trade at current structure mid. Returns the updated
    trade dict (with pnl/pnl_pct) or None if close failed.

    `spot` is the live underlying LTP at exit — recorded for post-trade
    spot-movement analysis. P&L itself is driven by `mid`, not spot.
    """
    if not cfg.PAPER_MODE:
        return None
    if trade.get('status') != 'entered':
        return None  # already closed by an earlier trigger this cycle
    if mid is None:
        # No quote — never fabricate a price (booking -debit max-loss on a
        # transient outage corrupts the paper P&L). Defer; the caller retries
        # next poll. Callers already skip paper trades with no mid.
        return None
    if pending_mfe is not None:
        _flush_mfe(store, pending_mfe)
    try:
        updated = store.mark_exited(
            trade['id'],
            spot if spot is not None else trade.get('entry_spot', 0),
            mid,
            f'paper:{reason}'
        )
        logger.info("PAPER auto-closed #%d %s reason=%s mid=%s P&L=Rs%.0f (%.1f%%)",
                    trade['id'], trade['stock'], reason,
                    f'{mid:.2f}' if mid is not None else 'NA',
                    updated.get('pnl', 0), updated.get('pnl_pct', 0))
        # Mutate the in-loop dict so subsequent checks in this cycle skip it
        trade['status'] = 'exited'
        return updated
    except ValueError as e:
        logger.error("PAPER auto-close failed for #%d: %s", trade['id'], e)
        return None


def _track_debit_blindness(store: ZebraStore, trade: dict, usable: bool,
                           reason: Optional[str], dry_run: bool = False) -> None:
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
            _send_telegram(_format_blind_alert(trade, reason), dry_run=dry_run)
        logger.warning("DEBIT-BLIND alert #%d %s: %d cycles unreliable (%s)",
                       tid, trade['stock'], n, reason)


def check_entered(store: ZebraStore, kite, dry_run: bool = False) -> None:
    """Monitor entered trades for TP/SL/time exits.

    PAPER mode (default): auto-close at structure mid after each exit alert.
    LIVE mode: alert only, user runs `zebra close` manually.
    Dedup via persistent <kind>_alerted_at flags on each trade (survives
    cron restarts).
    """
    entered = store.get_entered()
    if not entered:
        return

    stocks = list({t['stock'] for t in entered})
    ltps = get_ltp(kite, stocks)
    today = datetime.now(IST).date()
    # One store write for the whole cycle's peak tracking — see _flush_mfe.
    pending_mfe: dict = {}

    for trade in entered:
        # An earlier exit-check this cycle may have already auto-closed.
        if trade.get('status') != 'entered':
            continue

        stock = trade['stock']
        spot = ltps.get(stock, 0)
        if spot <= 0:
            continue

        tid = trade['id']
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
                               dry_run=dry_run)

        # BEFORE the exit branches and BEFORE the no-quote skip below: the poll
        # that exits a trade is usually the poll that set its peak, and the
        # underlying is still worth recording on a cycle when the option book
        # is dark. Never gates or blocks anything — pure measurement.
        patch = mfe_mod.compute(trade, spot, mid, sq['reliable'])
        if patch:
            pending_mfe[tid] = patch

        if cfg.PAPER_MODE and mid is None:
            continue

        # ── TP ──────────────────────────────────────────────────────────
        tp_hit = (direction == 'CE' and spot >= tp_spot) or \
                 (direction == 'PE' and spot <= tp_spot)
        # A blocked trigger skips ONLY ITS OWN branch. It must not `continue`:
        # that would also skip the DEBIT-SL and TIME checks below, so a TP held
        # on an untradeable book would suppress the T-3 expiry nag entirely and
        # ride the position into settlement week unnoticed.
        if tp_hit and _exit_cleared(store, trade, 'tp', sq, spot,
                                    dry_run=dry_run) \
                and store.set_alert_flag(tid, 'tp'):
            _send_exit_alert(store, trade, 'tp',
                             _format_tp_alert(trade, spot, mid), dry_run=dry_run)
            logger.info("TP alert #%d %s spot=%.2f tp=%.2f", tid, stock, spot, tp_spot)
            _paper_auto_close(store, trade, mid, 'tp', spot,
                              pending_mfe=pending_mfe)
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
                        and store.set_alert_flag(tid, 'trail'):
                    _send_exit_alert(store, trade, 'trail',
                                     _format_trail_alert(trade, mid, tl),
                                     dry_run=dry_run)
                    logger.info("TRAIL alert #%d %s mid=%.2f<=level=%.2f "
                                "peak_gain=%.2f (confirmed x%d)",
                                tid, stock, mid, tl['level'], tl['peak_gain'], n)
                    _paper_auto_close(store, trade, mid, 'trail', spot,
                                      pending_mfe=pending_mfe)
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
                and store.set_alert_flag(tid, 'spot_sl'):
            _send_exit_alert(store, trade, 'spot_sl',
                             _format_spot_sl_alert(trade, spot, mid), dry_run=dry_run)
            logger.info("SPOT SL alert #%d %s spot=%.2f sl=%.2f", tid, stock, spot, sl_spot)
            _paper_auto_close(store, trade, mid, 'spot_sl', spot,
                              pending_mfe=pending_mfe)
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
                        and store.set_alert_flag(tid, 'debit_sl'):
                    _send_exit_alert(store, trade, 'debit_sl',
                                     _format_debit_sl_alert(trade, mid),
                                     dry_run=dry_run)
                    logger.info("DEBIT SL alert #%d %s mid=%.2f sl=%.2f (confirmed x%d)",
                                tid, stock, mid, trade['debit_sl_value'], n)
                    _paper_auto_close(store, trade, mid, 'debit_sl', spot,
                                      pending_mfe=pending_mfe)
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
        try:
            exp = datetime.strptime(trade['expiry'], '%Y-%m-%d').date()
            days_left = _sessions_left(today, exp)
        except Exception:
            days_left = 999
        # Daily reminder during the last TIME_SL_DAYS — fires once per day so
        # the user keeps getting nudged until they exit. Paper mode auto-closes
        # on the first fire (subsequent days no-op since status='exited').
        if days_left <= cfg.TIME_SL_DAYS and store.set_alert_flag_daily(tid, 'time'):
            if _alerts_enabled(trade):
                _send_telegram(_format_time_alert(trade, days_left, mid), dry_run=dry_run)
            logger.info("TIME alert #%d %s days_left=%d", tid, stock, days_left)
            _paper_auto_close(store, trade, mid, 'time', spot,
                              pending_mfe=pending_mfe)

    # Anything not already flushed by an exit. On a normal cycle this is the
    # ONLY store write the peak tracking does, however many positions moved.
    _flush_mfe(store, pending_mfe)


# ── Cycle ─────────────────────────────────────────────────────────────────

def run_cycle(store: ZebraStore, kite, dry_run: bool = False,
              do_scan: bool = True) -> None:
    """One full cycle: scan + check watching + check entered."""
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
                logger.warning("VET fail-open: %d signal(s) timed out and will "
                               "enter UNVETTED: %s", len(expired), expired)
        except Exception as e:
            logger.error("Vet expiry sweep failed: %s", e, exc_info=True)
    if do_scan:
        try:
            validate_and_add(store, kite=kite, dry_run=dry_run)
        except Exception as e:
            logger.error("Scanner cycle failed: %s", e, exc_info=True)
    try:
        check_watching(store, kite, dry_run=dry_run)
    except Exception as e:
        logger.error("Watching cycle failed: %s", e, exc_info=True)
    try:
        check_entered(store, kite, dry_run=dry_run)
    except Exception as e:
        logger.error("Entered cycle failed: %s", e, exc_info=True)
    # Everything below is OBSERVATION, never trading. It runs last and each
    # piece is independently caught, so a failure in the learning/monitoring
    # half can never stop the half that trades.
    if cfg.VET_ENABLED:
        _run_vet_side_channels(store, kite, dry_run=dry_run)


def _run_vet_side_channels(store, kite, dry_run: bool = False) -> None:
    """Scoring, position review, event calendar and auth watch.

    Split out so the trading path above reads as one page, and so tests can
    drive these without a full cycle. None of it can open or close a position.
    """
    # LTPs for every symbol we are still tracking: open positions AND vetoed
    # signals whose shadow is still running. One batched call, as elsewhere.
    try:
        symbols = {t['stock'] for t in store.get_entered()}
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
                     | {t['stock'] for t in store.load_trades()
                        if t.get('status') == 'triggered'})
    logger.info("Event calendar stale — refreshing (%d symbols)", len(symbols))
    return events_mod.refresh(symbols)


def run_once(dry_run: bool = False) -> None:
    """Single cycle target (cron-friendly). Exits if market closed."""
    if not _is_market_open():
        logger.info("Market closed, skipping cycle")
        return
    kite = _get_kite()
    store = get_store()
    run_cycle(store, kite, dry_run=dry_run, do_scan=True)


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

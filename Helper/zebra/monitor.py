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
    """
    if not cfg.PAPER_MODE:
        return True
    struct = 'bcs' if trade.get('structure') == 'bcs' else 'zebra'
    return struct in cfg.ALERT_STRUCTURES


def _format_enter_alert(trade: dict, analysis: dict,
                        bcs: Optional[dict] = None) -> str:
    """Build the ENTER alert — the click-copy ORDER TICKET.

    Under `ENTRY_STRUCTURE == 'bcs'` (the pipeline since 2026-08-12) the ticket
    is the SPREAD, because that is the only structure the engine opens. The
    zebra back-ratio branch below is kept for the 'zebra' setting and for the
    15 legacy positions still running that structure — it is not dead code.

    In the zebra branch the picker chose its pair as the best balance of:
    passes liquidity gates, NetExt ≤ 0 (theta-OK), BE near spot, lowest
    capital_per_lot.
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

    head = (
        f"\U0001F993 <b>ENTER</b>  <code>{stock}</code>  ({direction}){conviction}\n"
        f"Level {st_val:,.2f} | spot {spot:,.2f} | gap {gap:.2f}% "
        f"({pull_dir} Level)\n"
    )

    # ── BCS-only pipeline: the SPREAD is the order ticket ───────────────────
    # Zebra was retired 2026-08-12 and its pair is never opened, so leading
    # with "BUY 2x / SELL 1x" and captioning the spread as a "shadow" told the
    # reader to place the one order the engine does not take. The alert IS the
    # order ticket (see the _alerts_enabled comment at the call site) — it must
    # name the structure that actually gets entered, and nothing else.
    if cfg.ENTRY_STRUCTURE == 'bcs':
        if not bcs:
            return (
                f"⚠ <b>NO SPREAD</b>  <code>{stock}</code> ({direction})\n"
                f"spot {spot:,.2f} | Level {st_val:,.2f} | gap {gap:.2f}%\n"
                f"No tradeable BCS pair at expiry {expiry}."
            )
        bwarn = ' ⚠ ' + html.escape(','.join(bcs['warnings'])) \
            if bcs.get('warnings') else ''
        return head + (
            f"expiry {expiry} ({dte} DTE) | lot {lot_size}{bwarn}\n"
            f"\n"
            f"Strikes <b>{bcs['long_strike']:g} / {bcs['short_strike']:g}</b>   "
            f"debit {bcs['debit']:g} "
            f"({bcs['debit_to_width_pct']:.0f}% of width)\n"
            f"Max profit {bcs['max_profit_per_share']:g}/share\n"
            f"\n"
            f"🟢 BUY 1× <code>{bcs['long_symbol']}</code>  {bcs['long_ask']:g}\n"
            f"🔴 SELL 1× <code>{bcs['short_symbol']}</code>  {bcs['short_bid']:g}"
        )

    msg = head + (
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
        if total:
            return float(total), 'exchange'
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
    if not kite or not bcs:
        return "\n\n⚠ <i>Funds not checked — no broker session.</i>"
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
                # LIVE still stops here: there the alert IS the order ticket,
                # the human acts on it, and re-alerting every 5 minutes would
                # be noise rather than a retry.
                if not cfg.PAPER_MODE:
                    continue
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
            if state == vet_mod.QUEUED:
                # Retry with the book we just re-quoted. promote_queued is a
                # CAS, so overlapping drainers cannot double-spawn; a loser
                # simply waits for the next cycle.
                vet_mod.promote_queued(
                    store, trade['id'],
                    context=_vet_context(trade, analysis, gap_pct, kite))
                continue
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
                    # ...but SAY SO on the record. This was the only fail-open
                    # that wrote nothing at all: the exception fired before or
                    # inside `_mutate`, so no `vet` key exists, and a trade
                    # entered this way is byte-identical to one entered with
                    # vetting switched off. `_vet_line` then returns "" for a
                    # missing state, so the ENTER alert carries nothing about
                    # vetting — and its own docstring says why that is wrong:
                    # silence reads as "Claude approved this". The UNAVAILABLE
                    # path is honest about the same outage; this one was not.
                    # Best-effort by design — we are already in an IO failure.
                    try:
                        vet_mod.mark_unavailable(store, trade['id'], str(e))
                    except Exception as e2:
                        logger.warning(
                            "could not stamp the failed-open marker on #%d: "
                            "%s", trade['id'], e2)
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
            elif state not in (vet_mod.ALLOWED, vet_mod.UNAVAILABLE):
                # EXPLICIT allowlist, not a bare fall-through. Entering was the
                # DEFAULT for any state without an `elif` above, so adding a
                # state to the machine silently meant "enter unvetted" — which
                # is exactly what STARVED must never do.
                logger.info("NO ENTRY #%d %s — vet state %r is not an entry "
                            "state", trade['id'], stock, state)
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
        # Funds last, so the money line sits closest to the click-copy symbols.
        # No-ops entirely in paper mode.
        if msg is not None:
            msg += _funds_line(kite, bcs,
                               (analysis.get('lot_size') or 0) * 1)
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
        # Never below zero. `intr - 1.5*allowance` goes negative the moment the
        # spread is OTM (intr = 0), which made this guard inert in exactly the
        # region it exists for: on a losing position it returned -0.83 against
        # a stop of 0.705, so no collapsed book could ever fall below it. Zero
        # is a real floor — a debit structure is never worth less, because you
        # can always let it expire.
        return max(0.0, round(intr - 1.5 * float(allowance), 2))
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
                       pending_mfe: Optional[dict] = None,
                       reliable: bool = True,
                       release_flag: bool = True,
                       legs: Optional[dict] = None) -> Optional[dict]:
    """Auto-close a paper trade at current structure mid. Returns the updated
    trade dict (with pnl/pnl_pct) or None if close failed.

    `spot` is the live underlying LTP at exit — recorded for post-trade
    spot-movement analysis. P&L itself is driven by `mid`, not spot.
    """
    if not cfg.PAPER_MODE:
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


def _alert_monitoring_blind(n_open: int, stocks: list,
                            dry_run: bool = False) -> None:
    """One Telegram a day when NO open position can be priced at all.

    Deliberately not a per-trade flag: the condition is about the data feed,
    not about any one position, and 24 identical messages would train the
    reader to ignore the one that matters. The marker is a file rather than a
    module global because the cron process exits between cycles, so an
    in-memory guard would re-alert every five minutes.
    """
    today = datetime.now(IST).strftime('%Y-%m-%d')
    marker = cfg.LOG_DIR / 'zebra_blind_alert.json'
    try:
        seen = json.loads(marker.read_text()).get('date') if marker.exists() else None
    except Exception:
        seen = None
    logger.error(
        "MONITORING BLIND: no LTP for ANY of %d open position(s) (%s) — "
        "TP, spot SL and the expiry nag are all dark this cycle. Check the "
        "Kite access token.", n_open, ', '.join(sorted(stocks)[:12]))
    if seen == today:
        return
    try:
        marker.write_text(json.dumps({'date': today}))
    except Exception as e:
        logger.warning("Could not write the blind-alert marker: %s", e)
    _send_telegram(
        f"🚨 <b>ZEBRA MONITORING BLIND</b>\n"
        f"No price for ANY of {n_open} open position(s).\n"
        f"TP, spot SL and the expiry nag are all dark. Most likely the Kite "
        f"access token has expired — check "
        f"<code>data/kite_access_token.json</code>.",
        dry_run=dry_run)


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
        _send_telegram(
            f"🛑 <b>ZEBRA STORE CORRUPT</b>\n"
            f"The trade file failed to parse and was quarantined at "
            f"{html.escape(stamp)}.\n"
            f"Error: <code>{html.escape(str(info.get('error'))[:200])}</code>\n"
            f"Backup: <code>{html.escape(str(info.get('backup')))}</code>\n"
            f"The store may have restarted EMPTY — if so, exit monitoring is "
            f"off on every open position and ids can be reissued. Restore from "
            f"the backup or Drive before the next session.",
            dry_run=dry_run)
        seen.write_text(stamp)
        logger.critical("STORE CORRUPTION alerted (quarantined at %s, "
                        "backup %s)", stamp, info.get('backup'))
        return True
    except Exception as e:
        logger.error("Could not process the store-corruption marker: %s", e)
        return False


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
    LIVE mode: alert only, user runs `zebra close` manually.
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
    try:
        ltps = get_ltp(kite, stocks)
    except Exception as e:
        logger.error("LTP fetch RAISED for %d open position(s): %s",
                     len(entered), e, exc_info=True)
        ltps = {}
    if not any((ltps.get(s) or 0) > 0 for s in stocks):
        # Not one price for any open position. Blind on the SPOT source, which
        # is the one TP and the expiry nag run off, so this is a full stop of
        # exit monitoring rather than a degraded mode. Blind means Telegram.
        _alert_monitoring_blind(len(entered), stocks, dry_run=dry_run)
        return
    today = datetime.now(IST).date()
    # One store write for the whole cycle's peak tracking — see _flush_mfe.
    pending_mfe: dict = {}

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
                "POLL #%d %s %s spot=%.2f tp=%.2f sl=%.2f | value=%s "
                "(%s) debit_sl=%.2f | long %s/%s short %s/%s",
                tid, stock, direction, spot, tp_spot, sl_spot,
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
            patch = mfe_mod.compute(trade, spot, mid, sq['reliable'])
            if patch:
                # MERGE, never assign. This dict may already hold the spot
                # corroboration reference for the same trade, and `pending_mfe[tid]
                # = patch` silently dropped it on every cycle where a peak
                # advanced — leaving the veto with a baseline that only updated on
                # quiet cycles, which is precisely backwards.
                pending_mfe.setdefault(tid, {}).update(patch)

            if cfg.PAPER_MODE and mid is None:
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
                            and store.set_alert_flag(tid, 'trail'):
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
                    and store.set_alert_flag(tid, 'spot_sl'):
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
                            and store.set_alert_flag(tid, 'debit_sl'):
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
    for t in list(store.load_trades()):
        if t.get('status') not in ('watching', 'triggered'):
            continue
        if vet_mod.vet_state(t) != vet_mod.STARVED:
            continue
        why = (t.get('vet') or {}).get('failed_open_because') or 'no verdict'
        try:
            store.cancel(t['id'], f'vet starved: {why}')
        except Exception as e:
            logger.error('Could not cancel starved #%d: %s', t['id'], e)
            continue
        reaped.append(t['id'])
        _send_telegram(
            f"🛑 <b>ENTRY DROPPED</b>  <code>{html.escape(str(t.get('stock')))}</code> "
            f"({html.escape(str(t.get('direction')))})\n"
            f"Claude never returned a verdict — {html.escape(str(why))}.\n"
            f"<i>Not entered. No rush: the setup re-qualifies if it still "
            f"holds tomorrow.</i>", dry_run=dry_run)
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

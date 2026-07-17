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

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

from . import config as cfg
from . import strikes as strikes_mod
from .scanner import _get_kite, get_ltp, compute_st_for_stock, validate_and_add
from .trade_store import ZebraStore, get_store

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

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
    """
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
    warn = ' ⚠ ' + ','.join(best['gate_fails']) if best['gate_fails'] else ''

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
        warn = ' ⚠ ' + ','.join(bcs['warnings']) if bcs.get('warnings') else ''
        msg += (
            f"\n\n📐 <b>BCS shadow</b> (paper A/B): "
            f"{bcs['long_strike']:g}/{bcs['short_strike']:g}  "
            f"debit {bcs['debit']:g} ({bcs['debit_to_width_pct']:.0f}% of width) | "
            f"maxP {bcs['max_profit_per_share']:g}{warn}\n"
            f"🟢 BUY 1× <code>{bcs['long_symbol']}</code>  {bcs['long_mid']:g}\n"
            f"🔴 SELL 1× <code>{bcs['short_symbol']}</code>  {bcs['short_mid']:g}"
        )
    return msg


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
    warn = ' ⚠ ' + ','.join(bcs['warnings']) if bcs.get('warnings') else ''
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
        f"\n"
        f"🟢 BUY 1× <code>{bcs['long_symbol']}</code>  {bcs['long_mid']:g}\n"
        f"🔴 SELL 1× <code>{bcs['short_symbol']}</code>  {bcs['short_mid']:g}"
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


def _format_time_alert(trade: dict, days_left: int,
                       mid: Optional[float] = None) -> str:
    paper = _paper_close_line(trade, mid)
    n_long = 1 if trade.get('structure') == 'bcs' else 2
    return (
        f"⏰ <b>EXIT REMINDER</b> [{_struct_label(trade)}]  "
        f"<code>{trade['stock']}</code>  ({trade['direction']})\n"
        f"T-{days_left} | expiry {trade['expiry']}\n"
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

        # Already triggered + still in zone → already alerted, no action.
        # (Saves Kite quote calls in the analyzer.)
        if trade['status'] == 'triggered':
            continue

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
        if not analysis.get('best'):
            logger.info("No tradeable best pick for %s, leaving in watching", stock)
            continue

        analysis['current_gap_pct'] = gap_pct
        # Store just the best pick + ranked list for traceability.
        alert_strikes = [analysis['best']] + [
            c for c in analysis.get('candidates', [])
            if (c['k_l'], c['k_s']) != (analysis['best']['k_l'], analysis['best']['k_s'])
        ]
        try:
            store.mark_triggered(trade['id'], price, gap_pct, alert_strikes)
        except ValueError as e:
            logger.warning("mark_triggered failed for #%d: %s", trade['id'], e)
            continue

        # PAPER mode: auto-record the entry FIRST, then alert — so the ENTER
        # alert only goes out for a position that actually opened. If the fill
        # is rejected we leave the signal in 'triggered' (it self-heals via the
        # drift/stale-cancel checks next cycle); we deliberately do NOT cancel
        # here, because a 'cancelled' record isn't deduped by the scanner and
        # would be re-added + re-alerted every scan (alert churn).
        bcs = None
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
                        bcs = None
                    else:
                        bcs['expiry'] = analysis['expiry']
                        bcs['entry_spot'] = price
                        store.add_bcs_shadow(store.find(trade['id']), bcs)
                except Exception as e:
                    logger.error("BCS shadow failed for #%d %s: %s",
                                 trade['id'], stock, e)
                    bcs = None

        # Who talks on Telegram (both structures auto-trade regardless):
        #   zebra in alert_structures  -> classic zebra alert (+BCS block if on)
        #   bcs only                   -> BCS-led alert; if the shadow failed,
        #                                 a short notice so a fired signal is
        #                                 never a silent miss.
        send_zebra = 'zebra' in cfg.ALERT_STRUCTURES
        send_bcs = 'bcs' in cfg.ALERT_STRUCTURES
        if send_zebra:
            msg = _format_enter_alert(trade, analysis,
                                      bcs=bcs if send_bcs else None)
        elif send_bcs and bcs:
            msg = _format_bcs_enter_alert(trade, analysis, bcs)
        elif send_bcs and cfg.PAPER_MODE and cfg.BCS_PAPER_ENABLED:
            msg = (f"⚠ <b>BCS SKIP</b>  <code>{stock}</code> "
                   f"({trade['direction']}) — signal fired but no viable "
                   f"BCS pair (see logs). Zebra #{trade['id']} entered "
                   f"silently.")
        else:
            msg = None
        if msg is None:
            logger.info("ENTER alert suppressed for #%d %s "
                        "(alert_structures=%s)", trade['id'], stock,
                        cfg.ALERT_STRUCTURES)
            continue
        sent = _send_telegram(msg, dry_run=dry_run)
        if sent:
            logger.info("ENTER alert sent for #%d %s", trade['id'], stock)
        else:
            logger.warning("ENTER alert FAILED for #%d %s", trade['id'], stock)


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
                if (p.get('k_l') == trade['long_strike']
                        and p.get('k_s') == trade['short_strike']):
                    allowance = p.get('short_extrinsic')
                    break
        if allowance is None:
            allowance = 0.3 * trade.get('debit', 0)
        return round(intr - 1.5 * float(allowance), 2)
    except Exception:
        return None


def _structure_value(kite, trade: dict, spot: Optional[float] = None
                     ) -> Optional[float]:
    """Current structure value per share: mult*long_mid - short_mid
    (mult = 2 zebra / 1 BCS). Returns None if any leg has a bad quote.

    When `spot` is given, the value is clamped to the intrinsic floor: a mid
    below the floor means the quote violates no-arbitrage (junk book on the
    ITM leg) and the floor is the conservative real closeable value. This is
    the false-debit-SL guard — without it a garbage quote can book a -50%
    exit on a winning trade (ABB #242, July 2026).
    """
    try:
        long_q = strikes_mod._quote_option(kite, trade['long_symbol'])
        short_q = strikes_mod._quote_option(kite, trade['short_symbol'])
        if long_q['mid'] <= 0 or short_q['mid'] <= 0:
            return None
        mid = round(_long_multiplier(trade) * long_q['mid'] - short_q['mid'], 2)
        if spot is not None and spot > 0:
            floor = _intrinsic_floor(trade, spot)
            if floor is not None and mid < floor:
                logger.warning(
                    "QUOTE GUARD #%d %s: structure mid %.2f < intrinsic floor "
                    "%.2f at spot %.2f — clamping to floor (bad ITM quote)",
                    trade['id'], trade['stock'], mid, floor, spot)
                return floor
        return mid
    except Exception as e:
        logger.debug("Quote fail for #%d: %s", trade['id'], e)
        return None


# Backward-compat alias (report.py / manual close path import this name).
def _quote_zebra_value(kite, trade: dict) -> Optional[float]:
    return _structure_value(kite, trade)


def _paper_auto_close(store: ZebraStore, trade: dict, mid: Optional[float],
                       reason: str, spot: Optional[float] = None) -> Optional[dict]:
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
        mid = _structure_value(kite, trade, spot)
        if cfg.PAPER_MODE and mid is None:
            continue

        # ── TP ──────────────────────────────────────────────────────────
        tp_hit = (direction == 'CE' and spot >= tp_spot) or \
                 (direction == 'PE' and spot <= tp_spot)
        if tp_hit and store.set_alert_flag(tid, 'tp'):
            if _alerts_enabled(trade):
                _send_telegram(_format_tp_alert(trade, spot, mid), dry_run=dry_run)
            logger.info("TP alert #%d %s spot=%.2f tp=%.2f", tid, stock, spot, tp_spot)
            _paper_auto_close(store, trade, mid, 'tp', spot)
            if trade.get('status') == 'exited':
                continue

        # ── SPOT SL ─────────────────────────────────────────────────────
        # Disabled by default: the debit floor already caps max loss, and the
        # 3% adverse spot SL was force-exiting capped-risk trades near the
        # local bottom (biggest realized-loss bucket in paper). Flip
        # spot_sl_enabled=True in zebra_config.json to restore.
        sl_hit = cfg.SPOT_SL_ENABLED and (
                 (direction == 'CE' and spot <= sl_spot) or
                 (direction == 'PE' and spot >= sl_spot))
        if sl_hit and store.set_alert_flag(tid, 'spot_sl'):
            if _alerts_enabled(trade):
                _send_telegram(_format_spot_sl_alert(trade, spot, mid), dry_run=dry_run)
            logger.info("SPOT SL alert #%d %s spot=%.2f sl=%.2f", tid, stock, spot, sl_spot)
            _paper_auto_close(store, trade, mid, 'spot_sl', spot)
            if trade.get('status') == 'exited':
                continue

        # ── DEBIT SL ────────────────────────────────────────────────────
        if mid is not None and mid <= trade['debit_sl_value']:
            if store.set_alert_flag(tid, 'debit_sl'):
                if _alerts_enabled(trade):
                    _send_telegram(_format_debit_sl_alert(trade, mid), dry_run=dry_run)
                logger.info("DEBIT SL alert #%d %s mid=%.2f sl=%.2f",
                            tid, stock, mid, trade['debit_sl_value'])
                _paper_auto_close(store, trade, mid, 'debit_sl', spot)
                if trade.get('status') == 'exited':
                    continue

        # ── TIME SL ─────────────────────────────────────────────────────
        try:
            exp = datetime.strptime(trade['expiry'], '%Y-%m-%d').date()
            days_left = (exp - today).days
        except Exception:
            days_left = 999
        # Daily reminder during the last TIME_SL_DAYS — fires once per day so
        # the user keeps getting nudged until they exit. Paper mode auto-closes
        # on the first fire (subsequent days no-op since status='exited').
        if days_left <= cfg.TIME_SL_DAYS and store.set_alert_flag_daily(tid, 'time'):
            if _alerts_enabled(trade):
                _send_telegram(_format_time_alert(trade, days_left, mid), dry_run=dry_run)
            logger.info("TIME alert #%d %s days_left=%d", tid, stock, days_left)
            _paper_auto_close(store, trade, mid, 'time', spot)


# ── Cycle ─────────────────────────────────────────────────────────────────

def run_cycle(store: ZebraStore, kite, dry_run: bool = False,
              do_scan: bool = True) -> None:
    """One full cycle: scan + check watching + check entered."""
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

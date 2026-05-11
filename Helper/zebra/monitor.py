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

def _format_enter_alert(trade: dict, analysis: dict) -> str:
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
            f"⚠ <b>ZEBRA NO-PAIR</b>  {stock} ({direction})\n"
            f"spot {spot:,.2f} | ST {st_val:,.2f} | gap {gap:.2f}%\n"
            f"Strike analyzer found no viable (K_L,K_S) at expiry {expiry} "
            f"— OI/spread/regime gates all failed."
        )

    k_l = int(best['k_l']) if best['k_l'].is_integer() else best['k_l']
    k_s = int(best['k_s']) if best['k_s'].is_integer() else best['k_s']
    warn = ' ⚠ ' + ','.join(best['gate_fails']) if best['gate_fails'] else ''
    net_ext_sign = '+' if best['net_ext'] > 0 else ''

    msg = (
        f"\U0001F993 <b>ZEBRA ENTER</b>  {stock} ({direction})\n"
        f"spot {spot:,.2f} | ST {st_val:,.2f} | gap {gap:.2f}% "
        f"({pull_dir} ST)\n"
        f"expiry {expiry} ({dte} DTE) | lot {lot_size}{warn}\n"
        f"\n"
        f"<b>{k_l}/{k_s}</b>  debit {best['debit']:.2f}  "
        f"BE {best['be']:.2f} ({best['be_pct_from_spot']:+.2f}%)  "
        f"NetExt {net_ext_sign}{best['net_ext']:.2f}\n"
        f"OI {best['long_oi']:,}/{best['short_oi']:,}  "
        f"sprd {best['long_spread_pct']:.1f}%/{best['short_spread_pct']:.1f}%\n"
        f"\n"
        f"  BUY 2x <code>{best['long_symbol']}</code>\n"
        f"  SELL 1x <code>{best['short_symbol']}</code>\n"
        f"\n"
        f"1 lot = Rs {best['capital_per_lot']:,.0f}\n"
        f"\nAfter placing:\n"
        f"<code>python -m zebra enter {trade['id']} --pair {k_l}/{k_s} "
        f"--debit X --lots 1 --expiry {expiry}</code>"
    )
    return msg


def _format_tp_alert(trade: dict, spot: float) -> str:
    return (
        f"\U0001F3AF <b>ZEBRA TP</b>  {trade['stock']} ({trade['direction']})\n"
        f"spot {spot:,.2f} hit TP {trade['tp_spot']:,.2f}\n"
        f"Long: <code>{trade['long_symbol']}</code>\n"
        f"Short: <code>{trade['short_symbol']}</code>\n"
        f"Consider closing. Use <code>zebra close {trade['id']} "
        f"--exit-debit X --reason tp</code>"
    )


def _format_spot_sl_alert(trade: dict, spot: float) -> str:
    return (
        f"\U0001F6D1 <b>ZEBRA SPOT SL</b>  {trade['stock']} ({trade['direction']})\n"
        f"spot {spot:,.2f} hit SL {trade['sl_spot']:,.2f}\n"
        f"Adverse move from entry {trade['entry_spot']:,.2f}\n"
        f"Close to save remaining premium: <code>zebra close {trade['id']} "
        f"--exit-debit X --reason spot_sl</code>"
    )


def _format_debit_sl_alert(trade: dict, mid: float) -> str:
    return (
        f"\U0001F4C9 <b>ZEBRA DEBIT SL</b>  {trade['stock']} ({trade['direction']})\n"
        f"Mid {mid:.2f} ≤ debit-SL {trade['debit_sl_value']:.2f} "
        f"(entry debit {trade['debit']:.2f})\n"
        f"Lost ~{(1 - mid/trade['debit'])*100:.0f}% of debit. Exit.\n"
        f"<code>zebra close {trade['id']} --exit-debit {mid:.2f} --reason debit_sl</code>"
    )


def _format_time_alert(trade: dict, days_left: int) -> str:
    return (
        f"⏰ <b>ZEBRA T-{days_left}</b>  {trade['stock']} ({trade['direction']})\n"
        f"Expiry {trade['expiry']}, {days_left} days left\n"
        f"Pin risk on short {trade['short_symbol']}. Close now.\n"
        f"<code>zebra close {trade['id']} --exit-debit X --reason time</code>"
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

        msg = _format_enter_alert(trade, analysis)
        sent = _send_telegram(msg, dry_run=dry_run)
        if sent:
            logger.info("ENTER alert sent for #%d %s", trade['id'], stock)
        else:
            logger.warning("ENTER alert FAILED for #%d %s", trade['id'], stock)


# ── Entered → TP/SL/Time ─────────────────────────────────────────────────

def _quote_zebra_value(kite, trade: dict) -> Optional[float]:
    """Compute current Zebra structure value per share (2*long_mid - 1*short_mid).
    Returns None if any leg has bad quote.
    """
    try:
        long_q = strikes_mod._quote_option(kite, trade['long_symbol'])
        short_q = strikes_mod._quote_option(kite, trade['short_symbol'])
        if long_q['mid'] <= 0 or short_q['mid'] <= 0:
            return None
        return round(2 * long_q['mid'] - 1 * short_q['mid'], 2)
    except Exception as e:
        logger.debug("Quote fail for #%d: %s", trade['id'], e)
        return None


def check_entered(store: ZebraStore, kite, dry_run: bool = False) -> None:
    """Monitor entered trades for TP/SL/time exits.

    NOTE: We don't auto-close. We alert and let the user run `zebra close`.
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
        stock = trade['stock']
        spot = ltps.get(stock, 0)
        if spot <= 0:
            continue

        tid = trade['id']
        direction = trade['direction']
        tp_spot = trade['tp_spot']
        sl_spot = trade['sl_spot']

        # ── TP ──────────────────────────────────────────────────────────
        tp_hit = (direction == 'CE' and spot >= tp_spot) or \
                 (direction == 'PE' and spot <= tp_spot)
        if tp_hit and store.set_alert_flag(tid, 'tp'):
            _send_telegram(_format_tp_alert(trade, spot), dry_run=dry_run)
            logger.info("TP alert #%d %s spot=%.2f tp=%.2f", tid, stock, spot, tp_spot)

        # ── SPOT SL ─────────────────────────────────────────────────────
        sl_hit = (direction == 'CE' and spot <= sl_spot) or \
                 (direction == 'PE' and spot >= sl_spot)
        if sl_hit and store.set_alert_flag(tid, 'spot_sl'):
            _send_telegram(_format_spot_sl_alert(trade, spot), dry_run=dry_run)
            logger.info("SPOT SL alert #%d %s spot=%.2f sl=%.2f", tid, stock, spot, sl_spot)

        # ── DEBIT SL ────────────────────────────────────────────────────
        mid = _quote_zebra_value(kite, trade)
        if mid is not None and mid <= trade['debit_sl_value']:
            if store.set_alert_flag(tid, 'debit_sl'):
                _send_telegram(_format_debit_sl_alert(trade, mid), dry_run=dry_run)
                logger.info("DEBIT SL alert #%d %s mid=%.2f sl=%.2f",
                            tid, stock, mid, trade['debit_sl_value'])

        # ── TIME SL ─────────────────────────────────────────────────────
        try:
            exp = datetime.strptime(trade['expiry'], '%Y-%m-%d').date()
            days_left = (exp - today).days
        except Exception:
            days_left = 999
        if days_left <= cfg.TIME_SL_DAYS and store.set_alert_flag(tid, 'time'):
            _send_telegram(_format_time_alert(trade, days_left), dry_run=dry_run)
            logger.info("TIME alert #%d %s days_left=%d", tid, stock, days_left)


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

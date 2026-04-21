"""Magnet Dashboard - live view of all active signals across scanner,
confidence tracker, and spot-15M tracker, with NIFTY regime context.

Invocation:
    python -m playbook.magnet dashboard
    python -m playbook.magnet md              # alias
    python -m playbook.magnet md --watch 30    # refresh every 30s

Renders four blocks:
  1. REGIME       - NIFTY spot + day%, RSI(14), 50DMA gap, latest breadth
  2. SCANNER      - magnet_trades.json watching + entered
  3. CONFIDENCE   - confidence_tracker.json watching + ready + entered
  4. SPOT 15M     - spot_tracker.json active entry + exit signals

Live prices fetched in a single batched Kite LTP call. NIFTY regime uses
one historical_data call (60 daily candles) for RSI + 50DMA.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from . import config as cfg
from .scanner import get_ltp

log = logging.getLogger(__name__)

_NIFTY_TOKEN = 256265
_NIFTY_QUOTE_KEY = 'NSE:NIFTY 50'

_BREADTH_PATH = cfg.LOG_DIR / 'breadth_readings.json'
_CONF_PATH = cfg.LOG_DIR / 'confidence_tracker.json'
_SPOT_PATH = cfg.LOG_DIR / 'spot_tracker.json'
_MAGNET_PATH = cfg.LOCAL_FILE


# ---------------------------------------------------------------------------
#  Loaders
# ---------------------------------------------------------------------------

def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        log.warning("Failed reading %s: %s", path, e)
        return default


def _load_magnet_trades() -> list:
    data = _load_json(_MAGNET_PATH, [])
    return data if isinstance(data, list) else []


def _load_confidence_signals() -> list:
    data = _load_json(_CONF_PATH, {})
    return data.get('signals', []) if isinstance(data, dict) else []


def _load_spot_signals() -> list:
    data = _load_json(_SPOT_PATH, {})
    return data.get('signals', []) if isinstance(data, dict) else []


def _latest_breadth() -> Optional[dict]:
    data = _load_json(_BREADTH_PATH, [])
    if not isinstance(data, list) or not data:
        return None
    return data[-1]


# ---------------------------------------------------------------------------
#  Regime
# ---------------------------------------------------------------------------

def _compute_rsi(closes: list, period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        g = max(diff, 0)
        l = max(-diff, 0)
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100 - (100 / (1 + rs))


def fetch_regime(kite) -> dict:
    """Returns dict with NIFTY spot, day%, RSI(14), 50DMA gap, breadth."""
    out = {
        'nifty_ltp': None, 'nifty_change_pct': None,
        'rsi': None, 'close_50dma_pct': None,
        'rsi_on': None, 'dma_on': None, 'breadth_on': None,
        'breadth': None, 'breadth_age': None,
    }

    # LTP + day change
    try:
        q = kite.quote([_NIFTY_QUOTE_KEY])
        data = q[_NIFTY_QUOTE_KEY]
        out['nifty_ltp'] = data['last_price']
        prev_close = data.get('ohlc', {}).get('close') or data.get('last_price')
        if prev_close and data['last_price']:
            out['nifty_change_pct'] = (data['last_price'] - prev_close) / prev_close * 100
    except Exception as e:
        log.warning("NIFTY quote failed: %s", e)

    # Historical for RSI + 50DMA
    try:
        to_dt = datetime.now()
        from_dt = to_dt - timedelta(days=100)
        candles = kite.historical_data(
            _NIFTY_TOKEN,
            from_dt.strftime('%Y-%m-%d'),
            to_dt.strftime('%Y-%m-%d'),
            'day',
        )
        closes = [c['close'] for c in candles]
        if out['nifty_ltp']:
            # Use live LTP as most recent close for up-to-date regime signals
            closes_with_live = closes[:-1] + [out['nifty_ltp']] if closes else [out['nifty_ltp']]
        else:
            closes_with_live = closes

        if len(closes_with_live) >= 15:
            out['rsi'] = _compute_rsi(closes_with_live, 14)
            out['rsi_on'] = out['rsi'] > 50 if out['rsi'] is not None else None
        if len(closes_with_live) >= 50:
            dma50 = sum(closes_with_live[-50:]) / 50
            last = closes_with_live[-1]
            out['close_50dma_pct'] = (last - dma50) / dma50 * 100
            out['dma_on'] = last > dma50
    except Exception as e:
        log.warning("NIFTY historical fetch failed: %s", e)

    # Breadth
    br = _latest_breadth()
    if br:
        out['breadth'] = br.get('breadth_pct')
        out['breadth_on'] = out['breadth'] > 40 if out['breadth'] is not None else None
        ts = br.get('timestamp') or f"{br.get('date', '')}T{br.get('time', '')}"
        out['breadth_age'] = ts

    return out


def _regime_verdict(r: dict) -> str:
    signals_on = sum(1 for x in (r['rsi_on'], r['dma_on'], r['breadth_on']) if x)
    known = sum(1 for x in (r['rsi_on'], r['dma_on'], r['breadth_on']) if x is not None)
    if known < 3:
        return f"PARTIAL ({signals_on}/{known} ON, need all 3)"
    if signals_on == 0:
        return "PAUSED (all 3 OFF - entry blocked)"
    if signals_on >= 2:
        return f"{signals_on}/3 ON - check 7-day sustained before entry"
    return f"{signals_on}/3 ON - insufficient"


# ---------------------------------------------------------------------------
#  Helpers
# ---------------------------------------------------------------------------

def _fmt_num(x, dp=2, width=8):
    if x is None:
        return ' ' * width
    try:
        return f"{float(x):>{width}.{dp}f}"
    except Exception:
        return ' ' * width


def _fmt_pct(x, width=6, signed=True):
    if x is None:
        return ' ' * width
    try:
        v = float(x)
        sign = '+' if v >= 0 and signed else ''
        return f"{sign}{v:.1f}%".rjust(width)
    except Exception:
        return ' ' * width


def _time_ago(iso_str: Optional[str]) -> str:
    if not iso_str:
        return '-'
    try:
        # Accept 'YYYY-MM-DDTHH:MM:SS' or with tz
        ts = iso_str.replace('Z', '+00:00')
        if '+' not in ts and 'T' in ts:
            # Naive; assume local
            dt = datetime.fromisoformat(ts)
        else:
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo:
                dt = dt.astimezone().replace(tzinfo=None)
        delta = datetime.now() - dt
        secs = int(delta.total_seconds())
        if secs < 60:
            return f"{secs}s"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            return f"{secs // 3600}h"
        return f"{secs // 86400}d"
    except Exception:
        return iso_str[:16] if iso_str else '-'


def _short(s: Optional[str], n: int) -> str:
    if not s:
        return ''
    s = str(s)
    return s if len(s) <= n else s[:n - 2] + '..'


def _clear_screen():
    if os.name == 'nt':
        os.system('cls')
    else:
        os.system('clear')


# ---------------------------------------------------------------------------
#  Renderers
# ---------------------------------------------------------------------------

def render_header():
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print()
    print("=" * 158)
    title = "MAGNET DASHBOARD"
    pad = 158 - len(title) - len(now) - 4
    print(f"  {title}" + " " * pad + f"{now}  ")
    print("=" * 158)


def render_regime(r: dict):
    nifty = _fmt_num(r.get('nifty_ltp'), 1, 1).strip() or '?'
    chg = r.get('nifty_change_pct')
    chg_str = f"{chg:+.2f}%" if chg is not None else '?'
    rsi = r.get('rsi')
    rsi_str = f"{rsi:.1f}" if rsi is not None else '?'
    rsi_flag = '[ON]' if r.get('rsi_on') else ('[OFF]' if r.get('rsi_on') is False else '[?]')
    dma = r.get('close_50dma_pct')
    dma_str = f"{dma:+.1f}%" if dma is not None else '?'
    dma_flag = '[ON]' if r.get('dma_on') else ('[OFF]' if r.get('dma_on') is False else '[?]')
    br = r.get('breadth')
    br_str = f"{br:.1f}%" if br is not None else '?'
    br_flag = '[ON]' if r.get('breadth_on') else ('[OFF]' if r.get('breadth_on') is False else '[?]')
    verdict = _regime_verdict(r)
    print(f"\n  REGIME  {verdict}")
    print(f"  NIFTY {nifty} ({chg_str})  |  RSI {rsi_str} {rsi_flag}  |  "
          f"50DMA {dma_str} {dma_flag}  |  Breadth {br_str} {br_flag}")


def _pnl_pct(entry, now) -> Optional[float]:
    if entry and now and entry > 0:
        return (now - entry) / entry * 100
    return None


def render_scanner(trades: list, spot_ltps: dict, opt_ltps: dict):
    watching = [t for t in trades if t.get('status') == 'watching']
    entered = [t for t in trades if t.get('status') == 'entered']

    print()
    print("+" + "-" * 156 + "+")
    print(f"|  SCANNER  (magnet_trades.json)  ---  watching={len(watching)}   entered={len(entered)}"
          + " " * (156 - 55 - len(str(len(watching))) - len(str(len(entered)))) + "|")
    print("+" + "-" * 156 + "+")

    if not watching and not entered:
        print("  (none)")
        return

    if watching:
        print("\n  [WATCHING]  (pending entry)")
        print(f"  {'ID':>3}  {'STOCK':<12} {'DIR':<3} {'TF':<7} "
              f"{'WHEN':>5}   {'SIG SPOT':>9} {'NOW':>9}  {'TARGET':>9} "
              f"{'GAP%':>7}")
        for t in watching:
            sym = t.get('stock', '?')
            now_px = spot_ltps.get(sym)
            tgt = t.get('st_value') or t.get('target_spot')
            gap_now = _pnl_pct(tgt, now_px)
            when = _time_ago(f"{t.get('signal_date', '')}T{t.get('signal_time', '')}")
            print(f"  {t.get('id', 0):>3}  {_short(sym, 12):<12} "
                  f"{t.get('direction', '?'):<3} "
                  f"{t.get('timeframe', '?'):<7} "
                  f"{when:>5}   "
                  f"{_fmt_num(t.get('signal_price'), 1, 9)} "
                  f"{_fmt_num(now_px, 1, 9)}  "
                  f"{_fmt_num(tgt, 1, 9)} "
                  f"{_fmt_pct(gap_now, 7)}")

    if entered:
        print("\n  [ENTERED]  (open positions)")
        print(f"  {'ID':>3}  {'STOCK':<11} {'DIR':<3} {'WHEN':>5}  "
              f"{'SIG>ENTRY>NOW (spot)':<30}  {'TARGET':>8} {'SL':>8}  "
              f"{'OPTION':<22} {'ENT>NOW (opt)':<15}  {'P&L':>7}  {'FLAGS':<14} HELD")
        for t in entered:
            sym = t.get('stock', '?')
            now_px = spot_ltps.get(sym)
            sig_px = t.get('signal_price')
            ent_px = t.get('entry_spot')
            opt = t.get('option_symbol', '') or ''
            opt_now = opt_ltps.get(opt)
            opt_ent = t.get('option_premium')
            pnl = _pnl_pct(opt_ent, opt_now)

            spot_chain = (f"{sig_px:>8.1f} > {ent_px:>8.1f} > {_fmt_num(now_px, 1, 8).strip() or '   ?   '}"
                          if sig_px and ent_px else
                          f"{_fmt_num(sig_px, 1, 8)} > {_fmt_num(ent_px, 1, 8)} > {_fmt_num(now_px, 1, 8)}")
            spot_chain = spot_chain.ljust(30)

            opt_chain = f"{_fmt_num(opt_ent, 2, 6).strip()}>{_fmt_num(opt_now, 2, 6).strip()}"
            opt_chain = opt_chain.ljust(15)

            flags = []
            if t.get('cost_sl_active'):
                flags.append(f"cost@{t.get('cost_sl_level', 0):.1f}")
            if t.get('hedged'):
                flags.append('HEDGED')
            flag_str = ' '.join(flags) if flags else '-'
            when = _time_ago(f"{t.get('signal_date', '')}T{t.get('signal_time', '')}")
            held = f"{t.get('days_held', 0)}d"

            print(f"  {t.get('id', 0):>3}  {_short(sym, 11):<11} "
                  f"{t.get('direction', '?'):<3} "
                  f"{when:>5}  "
                  f"{spot_chain}  "
                  f"{_fmt_num(t.get('target_spot'), 1, 8)} "
                  f"{_fmt_num(t.get('sl_spot'), 1, 8)}  "
                  f"{_short(opt, 22):<22} "
                  f"{opt_chain}  "
                  f"{_fmt_pct(pnl, 7)}  "
                  f"{_short(flag_str, 14):<14} {held}")


def render_confidence(signals: list, spot_ltps: dict, opt_ltps: dict):
    watching = [s for s in signals if s.get('status') == 'watching']
    ready = [s for s in signals if s.get('status') == 'ready']
    entered = [s for s in signals if s.get('status') == 'entered']

    total = len(watching) + len(ready) + len(entered)
    print()
    print("+" + "-" * 156 + "+")
    print(f"|  CONFIDENCE TRACKER  (confidence_tracker.json)  ---  "
          f"watching={len(watching)}   ready={len(ready)}   entered={len(entered)}"
          + " " * 80 + "|"[:1])
    print("+" + "-" * 156 + "+")

    if total == 0:
        print("  (none)")
        return

    def _row(s, state_tag):
        sym = s.get('symbol', '?')
        now_px = spot_ltps.get(sym)
        sig_px = s.get('signal_price')
        ent_px = s.get('entry_price')  # spot at entry (None if still watching)
        opt = s.get('option_symbol') or ''
        opt_now = opt_ltps.get(opt)
        opt_ent = s.get('entry_option_price') or s.get('option_price')
        opt_pnl_pct = _pnl_pct(opt_ent, opt_now)
        when = _time_ago(s.get('signal_at'))
        score = f"{s.get('score', 0):>2}/{_short(s.get('grade', ''), 3):<3}"

        # Spot chain: sig > [entry if entered] > now
        if ent_px:
            spot_chain = f"{sig_px or 0:>7.1f} > {ent_px:>7.1f} > {_fmt_num(now_px, 1, 7).strip()}"
        else:
            spot_chain = f"{_fmt_num(sig_px, 1, 7).strip()} > {_fmt_num(now_px, 1, 7).strip()}"
        spot_chain = spot_chain.ljust(26)

        opt_chain = f"{_fmt_num(opt_ent, 2, 6).strip()}>{_fmt_num(opt_now, 2, 6).strip()}".ljust(14)

        print(f"  {s.get('id', 0):>3}  {_short(sym, 11):<11} "
              f"{state_tag:<7} {s.get('direction', '?'):<3} "
              f"{s.get('timeframe', '?')[0].upper():<1} "
              f"{score:<7} {when:>5}  "
              f"{spot_chain}  "
              f"{_fmt_num(s.get('target_st'), 1, 8)} "
              f"{_fmt_num(s.get('sl_spot'), 1, 8)}  "
              f"{_short(opt, 22):<22} "
              f"{opt_chain}  "
              f"{_fmt_pct(opt_pnl_pct, 7)}")

    print(f"  {'ID':>3}  {'STOCK':<11} {'STATE':<7} {'DIR':<3} {'T':<1} "
          f"{'SCORE':<7} {'WHEN':>5}  {'SIG>[ENT]>NOW (spot)':<26}  "
          f"{'TARGET':>8} {'SL':>8}  {'OPTION':<22} {'ENT>NOW (opt)':<14}  {'P&L':>7}")
    for s in watching:
        _row(s, 'WATCH')
    for s in ready:
        _row(s, 'READY')
    for s in entered:
        _row(s, 'ENTERED')


def render_spot_tracker(signals: list, spot_ltps: dict):
    active = [s for s in signals if s.get('status') in _SPOT_ACTIVE_STATUSES]

    print()
    print("+" + "-" * 156 + "+")
    print(f"|  SPOT 15M TRACKER  (spot_tracker.json)  ---  active={len(active)}"
          + " " * (156 - 59 - len(str(len(active)))) + "|")
    print("+" + "-" * 156 + "+")

    if not active:
        print("  (none active)")
        return

    print(f"  {'ID':>3}  {'STOCK':<12} {'DIR':<3} {'TF':<7} "
          f"{'STATUS':<14} {'15M':<5} {'NEED':<5}  "
          f"{'SIG SPOT':>9} {'SPOT NOW':>9}  {'15M ST':>9} {'TGT ST':>9}  {'GAP%':>6}  SINCE")

    for s in active:
        sym = s.get('symbol', '?')
        live_px = spot_ltps.get(sym)
        sig_px = s.get('signal_price')
        tgt = s.get('st_value')
        gap_now = _pnl_pct(tgt, live_px)

        status = s.get('status', '?')
        if status == 'watching':
            need = s.get('need_pullback_dir', '?')
        elif status == 'exit_tracking':
            need = f"!{s.get('need_pullback_dir', '?')}"
        else:
            need = s.get('need_reentry_dir', '?')
        since = _time_ago(s.get('picked_up_at'))

        print(f"  {s.get('id', 0):>3}  {_short(sym, 12):<12} "
              f"{s.get('direction', '?'):<3} "
              f"{s.get('timeframe', '?'):<7} "
              f"{status:<14} "
              f"{s.get('last_spot_15m_dir', '?'):<5} "
              f"{need:<5}  "
              f"{_fmt_num(sig_px, 1, 9)} "
              f"{_fmt_num(live_px, 1, 9)}  "
              f"{_fmt_num(s.get('last_spot_15m_st'), 1, 9)} "
              f"{_fmt_num(tgt, 1, 9)}  "
              f"{_fmt_pct(gap_now, 6)}  {since}")


# ---------------------------------------------------------------------------
#  Orchestration
# ---------------------------------------------------------------------------

_SPOT_ACTIVE_STATUSES = ('watching', 'pullback', 'exit_tracking',
                        'alerted', 'exit_alert')


def _collect_symbols(scanner_trades, conf_signals, spot_signals):
    stocks = set()
    options = set()
    for t in scanner_trades:
        if t.get('status') in ('watching', 'entered'):
            if t.get('stock'):
                stocks.add(t['stock'])
            if t.get('status') == 'entered' and t.get('option_symbol'):
                options.add(t['option_symbol'])
            if t.get('hedged') and t.get('hedge_symbol'):
                options.add(t['hedge_symbol'])
    for s in conf_signals:
        if s.get('status') in ('watching', 'ready', 'entered'):
            if s.get('symbol'):
                stocks.add(s['symbol'])
            if s.get('option_symbol'):
                options.add(s['option_symbol'])
    for s in spot_signals:
        if s.get('status') in _SPOT_ACTIVE_STATUSES:
            if s.get('symbol'):
                stocks.add(s['symbol'])
            if s.get('option_symbol'):
                options.add(s['option_symbol'])
    return sorted(stocks), sorted(options)


def _fetch_option_ltps(kite, options: list) -> dict:
    """Options live on NFO, not NSE. Call kite.ltp directly with NFO: prefix."""
    if not options:
        return {}
    result = {}
    instruments = [f"NFO:{o}" for o in options]
    try:
        data = kite.ltp(instruments)
        for key, val in data.items():
            sym = key.replace('NFO:', '')
            result[sym] = val.get('last_price')
    except Exception as e:
        log.warning("Option LTP fetch failed: %s", e)
    return result


def _date_only(iso_str: Optional[str]) -> Optional[str]:
    if not iso_str:
        return None
    return str(iso_str)[:10]


def _days_since(date_str: Optional[str]) -> Optional[int]:
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str[:10])
        return (datetime.now().date() - dt.date()).days
    except Exception:
        return None


def _classify_opportunity(rec: dict) -> str:
    """Verdict tag: TP HIT / SL HIT / EXPIRED / CANCELLED / LIVE / PAST TGT /
    CHASED / STALE / STOPPED / NO DATA."""
    # Closed trades first
    if rec.get('row_status') == 'closed':
        reason = (rec.get('exit_reason') or '').lower()
        if rec.get('exit_type') == 'cancelled':
            return 'CANCELLED'
        if any(x in reason for x in ('tp', 'target')):
            return 'TP HIT'
        if any(x in reason for x in ('sl_spot', 'sl_cost', 'sl_time', 'sl ', 'stop')):
            return 'SL HIT'
        if 'expir' in reason:
            return 'EXPIRED'
        if 'stale' in reason:
            return 'STALE'
        return 'EXITED'

    now = rec.get('spot_now')
    tgt = rec.get('target')
    sl = rec.get('sl')
    direction = rec.get('direction')
    opt_move = rec.get('option_move_pct')
    spot_move = rec.get('spot_move_pct') or 0

    if not now or not tgt:
        return 'NO DATA'

    if direction == 'CE':
        if now >= tgt:
            return 'PAST TGT'
        if sl and now <= sl:
            return 'STOPPED'
    elif direction == 'PE':
        if now <= tgt:
            return 'PAST TGT'
        if sl and now >= sl:
            return 'STOPPED'

    if opt_move is not None and opt_move > 100:
        return 'CHASED'

    days = rec.get('days_since_signal')
    if days is not None and days > 10 and abs(spot_move) < 2:
        return 'STALE'

    return 'LIVE'


_EXIT_REASON_MAP = [
    ('tp_spot_touched_st', 'Target hit (spot touched ST)'),
    ('tp_spot',            'Target hit'),
    ('tp',                 'Target hit'),
    ('sl_cost',            'Cost SL hit (lost entry cushion)'),
    ('sl_spot',            'Spot SL hit'),
    ('sl_premium',         'Premium SL hit (40% loss)'),
    ('sl_time',            'Time SL (5d elapsed)'),
    ('option_expired',     'Option expired'),
    ('15m_st_break',       '15M ST broke (trend flip)'),
    ('15M trend DOWN',     '15M trend flipped DOWN'),
    ('gap narrowed',       'Gap narrowed - past entry zone'),
    ('weekly signal stale','Weekly signal aged out'),
    ('cleanup: pre-v2',    'Cleanup (pre-v2 signal)'),
    ('cleanup',            'Cleanup'),
    ('stale',              'Stale signal'),
    ('manual close',       'Closed manually'),
    ('manual cancel',      'Cancelled manually'),
]


def _friendly_exit_reason(raw: str) -> str:
    if not raw:
        return ''
    low = raw.lower()
    # Strip "ct:" prefix that the CT stores
    if low.startswith('ct:'):
        low = low[3:].lstrip()
        raw = raw[3:].lstrip()
    for frag, label in _EXIT_REASON_MAP:
        if frag.lower() in low:
            return label
    # Fallback: trim to 40 chars
    return raw[:40] + ('...' if len(raw) > 40 else '')


def _closed_record(t: dict, source: str) -> dict:
    """Build a closed-trade record from scanner or confidence tracker data.

    source: 'scanner' or 'ct'
    """
    is_cancelled = t.get('status') == 'cancelled'
    if source == 'scanner':
        sig_date = t.get('signal_date')
        entry_date = t.get('entry_date')
        exit_date = t.get('exit_date')
        sig_spot = t.get('signal_price')
        entry_spot = t.get('entry_spot')
        exit_spot = t.get('exit_spot')
        option_sym = t.get('option_symbol')
        opt_entry = t.get('option_premium')
        opt_exit = t.get('exit_premium')
        target = t.get('target_spot') or t.get('st_value')
        sl = t.get('sl_spot')
        trackers = [f"Scanner:{'CANCEL' if is_cancelled else 'EXIT'} #{t.get('id')}"]
    else:  # ct
        sig_date = _date_only(t.get('signal_at'))
        entry_date = _date_only(t.get('entry_at'))
        exit_date = _date_only(t.get('exit_at'))
        sig_spot = t.get('signal_price')
        entry_spot = t.get('entry_price')
        exit_spot = t.get('exit_price')
        option_sym = t.get('option_symbol')
        opt_entry = t.get('entry_option_price') or t.get('option_price')
        opt_exit = t.get('exit_option_price')
        target = t.get('target_st')
        sl = t.get('sl_spot')
        trackers = [f"CT:{'CANCEL' if is_cancelled else 'EXIT'} #{t.get('id')}"]
        if t.get('score'):
            trackers[0] += f" {t['score']}/{(t.get('grade', '') or '')[:3]}"

    rec = {
        'symbol': t.get('stock') or t.get('symbol'),
        'row_status': 'closed',
        'exit_type': 'cancelled' if is_cancelled else 'exited',
        'exit_date': exit_date,
        'exit_spot': exit_spot,
        'exit_option': opt_exit,
        'exit_reason_raw': t.get('exit_reason') or t.get('cancel_reason') or '',
        'exit_reason': _friendly_exit_reason(t.get('exit_reason') or t.get('cancel_reason') or ''),
        'pnl_pct_stored': t.get('pnl_pct'),
        'pnl_abs': t.get('pnl'),
        'direction': t.get('direction'),
        'timeframe': t.get('timeframe'),
        'signal_date': sig_date,
        'signal_spot': sig_spot,
        'entry_date': entry_date,
        'entry_spot': entry_spot,
        'option_symbol': option_sym,
        'option_entry': opt_entry,
        'target': target,
        'sl': sl,
        'trackers': trackers,
        'status_hint': 'CLOSED',
    }
    # Treat 0.0 as "data missing" so the row doesn't look like a crash-to-zero
    if exit_spot == 0:
        exit_spot = None
    if opt_exit == 0:
        opt_exit = None
    rec['spot_now'] = exit_spot  # "now" for closed = exit price
    rec['option_now'] = opt_exit
    rec['spot_move_pct'] = _pnl_pct(sig_spot, exit_spot)
    rec['option_move_pct'] = _pnl_pct(opt_entry, opt_exit)
    rec['days_since_signal'] = _days_since(sig_date)
    rec['verdict'] = _classify_opportunity(rec)
    return rec


_ALLOWED_TIMEFRAMES = {'weekly', 'monthly'}


def _is_allowed_tf(tf: Optional[str]) -> bool:
    return (tf or '').lower() in _ALLOWED_TIMEFRAMES


def merge_by_stock(scanner_trades: list, conf_signals: list,
                   spot_signals: list, spot_ltps: dict,
                   opt_ltps: dict) -> list:
    """Merge signals from all three trackers into one record per stock.

    Earliest signal date wins for 'when'. Option data comes from whichever
    tracker has it (scanner.entered > confidence > spot). Target/SL similar.
    Only weekly + monthly signals are included; daily is skipped.
    """
    by_stock: dict = {}

    def _rec(sym):
        return by_stock.setdefault(sym, {
            'symbol': sym, 'trackers': [], 'direction': None,
            'timeframe': None, 'signal_date': None, 'signal_spot': None,
            'entry_date': None, 'entry_spot': None,
            'option_symbol': None, 'option_entry': None,
            'target': None, 'sl': None, 'status_hint': None,
        })

    # Scanner (authoritative for entered positions)
    for t in scanner_trades:
        if t.get('status') not in ('watching', 'entered'):
            continue
        if not _is_allowed_tf(t.get('timeframe')):
            continue
        sym = t.get('stock')
        if not sym:
            continue
        rec = _rec(sym)
        rec['trackers'].append(f"Scanner:{t['status'].upper()} #{t.get('id')}")
        rec['direction'] = rec['direction'] or t.get('direction')
        rec['timeframe'] = rec['timeframe'] or t.get('timeframe')
        sd = t.get('signal_date')
        if sd and (not rec['signal_date'] or sd < rec['signal_date']):
            rec['signal_date'] = sd
            rec['signal_spot'] = t.get('signal_price')
        if t['status'] == 'entered':
            rec['entry_date'] = rec['entry_date'] or t.get('entry_date')
            rec['entry_spot'] = rec['entry_spot'] or t.get('entry_spot')
            rec['option_symbol'] = rec['option_symbol'] or t.get('option_symbol')
            rec['option_entry'] = rec['option_entry'] or t.get('option_premium')
            rec['target'] = rec['target'] or t.get('target_spot')
            rec['sl'] = rec['sl'] or t.get('sl_spot')
            rec['status_hint'] = 'ENTERED'
        else:
            rec['target'] = rec['target'] or t.get('st_value')
            rec['status_hint'] = rec['status_hint'] or 'WATCH'

    # Confidence tracker
    for s in conf_signals:
        if s.get('status') not in ('watching', 'ready', 'entered'):
            continue
        if not _is_allowed_tf(s.get('timeframe')):
            continue
        sym = s.get('symbol')
        if not sym:
            continue
        rec = _rec(sym)
        tag = f"CT:{s['status'].upper()}"
        if s.get('score'):
            tag += f" {s['score']}/{s.get('grade', '')[:3]}"
        rec['trackers'].append(tag)
        rec['direction'] = rec['direction'] or s.get('direction')
        rec['timeframe'] = rec['timeframe'] or s.get('timeframe')
        sig_date = _date_only(s.get('signal_at'))
        if sig_date and (not rec['signal_date'] or sig_date < rec['signal_date']):
            rec['signal_date'] = sig_date
            rec['signal_spot'] = s.get('signal_price')
        if not rec['option_symbol'] and s.get('option_symbol'):
            rec['option_symbol'] = s['option_symbol']
            rec['option_entry'] = s.get('entry_option_price') or s.get('option_price')
        if s.get('status') == 'entered':
            rec['entry_date'] = rec['entry_date'] or _date_only(s.get('entry_at'))
            rec['entry_spot'] = rec['entry_spot'] or s.get('entry_price')
            rec['status_hint'] = 'ENTERED'
        rec['target'] = rec['target'] or s.get('target_st')
        rec['sl'] = rec['sl'] or s.get('sl_spot')

    # Spot 15M tracker
    for s in spot_signals:
        if s.get('status') not in _SPOT_ACTIVE_STATUSES:
            continue
        if not _is_allowed_tf(s.get('timeframe')):
            continue
        sym = s.get('symbol')
        if not sym:
            continue
        rec = _rec(sym)
        rec['trackers'].append(f"Spot15M:{s['status']}")
        rec['direction'] = rec['direction'] or s.get('direction')
        rec['timeframe'] = rec['timeframe'] or s.get('timeframe')
        sig_date = _date_only(s.get('picked_up_at'))
        if sig_date and (not rec['signal_date'] or sig_date < rec['signal_date']):
            rec['signal_date'] = sig_date
            rec['signal_spot'] = s.get('signal_price')
        rec['target'] = rec['target'] or s.get('st_value')

    # Live prices + computed fields (open rows)
    for rec in by_stock.values():
        rec['row_status'] = 'open'
        rec['spot_now'] = spot_ltps.get(rec['symbol'])
        rec['option_now'] = opt_ltps.get(rec['option_symbol']) if rec['option_symbol'] else None
        rec['spot_move_pct'] = _pnl_pct(rec.get('signal_spot'), rec.get('spot_now'))
        rec['option_move_pct'] = _pnl_pct(rec.get('option_entry'), rec.get('option_now'))
        rec['days_since_signal'] = _days_since(rec.get('signal_date'))
        rec['verdict'] = _classify_opportunity(rec)

    open_records = list(by_stock.values())

    # Closed rows: one per closed trade (not merged)
    closed_records = []
    for t in scanner_trades:
        if t.get('status') in ('exited', 'cancelled') and _is_allowed_tf(t.get('timeframe')):
            closed_records.append(_closed_record(t, 'scanner'))
    for s in conf_signals:
        if s.get('status') in ('exited', 'cancelled') and _is_allowed_tf(s.get('timeframe')):
            closed_records.append(_closed_record(s, 'ct'))

    # Sort open: ENTERED first, then by days desc (oldest within group first)
    open_records.sort(key=lambda r: (
        0 if r.get('status_hint') == 'ENTERED' else 1,
        -(r.get('days_since_signal') or 0),
    ))
    # Sort closed: newest exit first
    closed_records.sort(key=lambda r: (r.get('exit_date') or ''), reverse=True)

    return open_records + closed_records


def _pnl_pct(entry, now):
    if entry and now and entry > 0:
        return (now - entry) / entry * 100
    return None


def render_stocks_text(records: list, filter_status: str = 'open'):
    """One unified table. filter_status: 'open' | 'closed' | 'all'."""
    # Overall totals (pre-filter)
    all_open = [r for r in records if r.get('row_status') == 'open']
    all_closed = [r for r in records if r.get('row_status') == 'closed']
    entered_all = sum(1 for r in all_open if r.get('status_hint') == 'ENTERED')
    tp_hits = sum(1 for r in all_closed if r.get('verdict') == 'TP HIT')
    sl_hits = sum(1 for r in all_closed if r.get('verdict') == 'SL HIT')

    # Filter rows to show
    if filter_status == 'open':
        rows = all_open
    elif filter_status == 'closed':
        rows = all_closed
    else:
        rows = all_open + all_closed

    print(f"\n  SIGNALS [{filter_status.upper()}]  "
          f"open={len(all_open)} (entered={entered_all})  "
          f"closed={len(all_closed)} (TP={tp_hits} SL={sl_hits})  "
          f"showing {len(rows)}")
    if not rows:
        print("  (no rows)")
        return

    # Group header (visual cue for the two groups)
    print(f"  {'':<11} {'':<3} {'':<7} {'':<7} {'':<11} {'':>4}  "
          f"{'<--------------- SPOT --------------->':<48}   "
          f"{'<--------- OPTION --------->':<34}")
    print(f"  {'STOCK':<11} {'DIR':<3} {'TF':<7} {'ST':<7} {'SIG DATE':<11} "
          f"{'DAYS':>4}  {'SIG':>9} {'NOW':>9} {'TARGET':>9} {'SL':>9} {'MOV%':>7}  "
          f"{'SIG':>8} {'NOW':>9} {'MOV%':>8}  "
          f"{'VERDICT':<10} NOTES")

    for r in rows:
        sym = r['symbol']
        dir_ = r.get('direction') or '-'
        tf = r.get('timeframe') or '-'
        state = r.get('status_hint') or ''
        sig_date = r.get('signal_date') or '-'
        days = r.get('days_since_signal')
        days_str = f"{days}d" if days is not None else '-'
        verdict = r.get('verdict', '-')

        notes = ' | '.join(r.get('trackers', []))
        if r.get('row_status') == 'closed':
            exit_date = r.get('exit_date') or ''
            reason = r.get('exit_reason') or ''
            notes = f"exit {exit_date} ({reason}) | " + notes

        print(f"  {sym:<11} {dir_:<3} {tf:<7} {state:<7} {sig_date:<11} "
              f"{days_str:>4}  "
              # SPOT group: sig | now | target | sl | Δ%
              f"{_fmt_num(r.get('signal_spot'), 1, 9)} "
              f"{_fmt_num(r.get('spot_now'), 1, 9)} "
              f"{_fmt_num(r.get('target'), 1, 9)} "
              f"{_fmt_num(r.get('sl'), 1, 9)} "
              f"{_fmt_pct(r.get('spot_move_pct'), 7)}  "
              # OPTION group: sig | now | Δ%
              f"{_fmt_num(r.get('option_entry'), 2, 8)} "
              f"{_fmt_num(r.get('option_now'), 2, 9)} "
              f"{_fmt_pct(r.get('option_move_pct'), 8)}  "
              f"{verdict:<10} {_short(notes, 60)}")


def render_once(kite, write_html: bool = True, open_browser: bool = True,
                filter_status: str = 'open') -> Optional[str]:
    scanner_trades = _load_magnet_trades()
    conf_signals = _load_confidence_signals()
    spot_signals = _load_spot_signals()

    stocks, options = _collect_symbols(scanner_trades, conf_signals, spot_signals)

    spot_ltps = {}
    if stocks:
        try:
            spot_ltps = get_ltp(kite, stocks)
        except Exception as e:
            log.warning("Spot LTP fetch failed: %s", e)

    opt_ltps = _fetch_option_ltps(kite, options)
    regime = fetch_regime(kite)
    records = merge_by_stock(scanner_trades, conf_signals, spot_signals,
                             spot_ltps, opt_ltps)

    render_header()
    render_regime(regime)
    render_stocks_text(records, filter_status=filter_status)
    print()

    html_path = None
    if write_html:
        try:
            from . import dashboard_html as _dh
            html_path = _dh.write_html(regime, records)
            print(f"  HTML dashboard: {html_path}")
            if open_browser:
                import webbrowser
                webbrowser.open(f"file:///{html_path.replace(chr(92), '/')}")
        except Exception as e:
            log.warning("HTML write/open failed: %s", e)
    return html_path


def run(watch_sec: Optional[int] = None,
        write_html: bool = True,
        open_browser: bool = True,
        filter_status: str = 'open'):
    from .scanner import _get_kite
    try:
        kite = _get_kite()
    except Exception as e:
        print(f"Kite auth failed: {e}")
        sys.exit(1)

    if not watch_sec:
        render_once(kite, write_html=write_html, open_browser=open_browser,
                    filter_status=filter_status)
        return

    first = True
    try:
        while True:
            _clear_screen()
            render_once(kite, write_html=write_html,
                        open_browser=(open_browser and first),
                        filter_status=filter_status)
            first = False
            print(f"  Refreshing every {watch_sec}s - Ctrl+C to stop.")
            time.sleep(watch_sec)
    except KeyboardInterrupt:
        print("\n  Stopped.")

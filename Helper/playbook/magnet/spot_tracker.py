"""Spot 15M ST Tracker -- parallel entry timing using spot pullback->flip.

Supplementary signal (not a hard gate). Backtest showed the flip filter helps
avoid big losers during directional market events, but can delay fast winners.
Use alongside existing magnet alerts -- user decides which to follow.

Flow:
  1. Reads WATCHING signals from magnet_trades.json (read-only).
  2. Records spot 15M ST(10,3) direction at signal time.
  3. Waits for spot 15M to flip AGAINST our direction (pullback).
  4. When spot 15M flips BACK to our direction (fresh momentum) -> ALERT.
  5. User decides manually whether to enter.

Storage: logs/spot_tracker.json (independent, never touches magnet_trades.json).
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from . import config as cfg

log = logging.getLogger(__name__)

# Paths
_HELPER = cfg.PROJECT_ROOT
_BOTS = cfg.BOTS_ROOT
_TRACKER_PATH = cfg.LOG_DIR / 'spot_tracker.json'
_MAGNET_PATH = cfg.LOCAL_FILE   # magnet_trades.json (read-only!)
_CONFIG_PATH = cfg.CONFIG_FILE

# Constants
_ST_PERIOD = 10
_ST_MULTIPLIER = 3
_MIN_15M_CANDLES = 15
_15M_FETCH_DAYS = 12           # enough for ST warmup
_MIN_DTE_DAYS = 3              # cancel if option DTE < this
_POLL_INTERVAL_SEC = 300       # 5 minutes between polls
_MAX_ACTIVE_SIGNALS = 20       # cap to avoid API rate limit issues
_STALE_CHECK_MINUTES = 30      # skip transitions if last check was > N min ago
_MAX_WATCHING_DAYS = {         # auto-cancel if no pullback within N days
    'daily': 3,
    'weekly': 10,
    'monthly': 20,
}
# Default DTE by timeframe (when option_expiry is null)
_DEFAULT_MAX_DAYS = {
    'daily': 5,
    'weekly': 12,
    'monthly': 30,
}


# ---------------------------------------------------------------------------
#  Telegram (reuse confidence_tracker's helper to avoid duplication)
# ---------------------------------------------------------------------------

def _send_telegram(msg: str, dry_run: bool = False) -> bool:
    """Send Telegram to watching channel. Delegates to CT's implementation."""
    if dry_run:
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[DRY RUN] [SPOT] Telegram:\n{safe}\n")
        return True
    try:
        from .confidence_tracker import _send_telegram as _ct_send
        return _ct_send(msg, dry_run=False, channel='watching')
    except ImportError:
        log.warning("Could not import CT telegram helper, using fallback")
        return _send_telegram_fallback(msg)


def _send_telegram_fallback(msg: str) -> bool:
    """Standalone fallback if CT import fails."""
    try:
        with open(_CONFIG_PATH) as f:
            mcfg = json.load(f)
        tg = mcfg.get('telegram_watching', {})
        bot_token = tg.get('bot_token')
        chat_id = tg.get('chat_id')
        if not bot_token or not chat_id:
            return False
        import requests
        resp = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={'chat_id': chat_id, 'text': msg, 'parse_mode': 'HTML'},
            timeout=10)
        return resp.status_code == 200
    except Exception as e:
        log.warning("Telegram fallback failed: %s", e)
        return False


# ---------------------------------------------------------------------------
#  SpotTracker state management
# ---------------------------------------------------------------------------

class SpotTracker:
    """Manages spot_tracker.json state file."""

    def __init__(self, path: Path = _TRACKER_PATH):
        self._path = path
        self._data = self._load()

    def _load(self) -> dict:
        if self._path.exists():
            try:
                with open(self._path) as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                log.warning("Corrupt state file, resetting: %s", e)
        return {'next_id': 1, 'signals': []}

    def _save(self):
        tmp = self._path.with_suffix('.json.tmp')
        with open(tmp, 'w') as f:
            json.dump(self._data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(self._path))

    def _find(self, sig_id: int) -> Optional[dict]:
        for s in self._data['signals']:
            if s['id'] == sig_id:
                return s
        return None

    def is_tracking(self, magnet_trade_id: int) -> bool:
        """Check if a magnet trade is already being tracked."""
        return any(s['magnet_trade_id'] == magnet_trade_id
                   and s['status'] in ('watching', 'pullback')
                   for s in self._data['signals'])

    def active_count(self) -> int:
        return len(self.get_active())

    def add(self, magnet_trade: dict, spot_15m_dir: str,
            spot_15m_st: float, spot_ltp: float) -> dict:
        """Add a new signal from a magnet WATCHING trade."""
        direction = magnet_trade['direction']  # CE or PE
        # CE = buy call = want spot UP. Pullback = spot goes DOWN first.
        # PE = buy put = want spot DOWN. Pullback = spot goes UP first.
        if direction == 'CE':
            need_pullback_dir = 'DOWN'  # spot falls = pullback
            need_reentry_dir = 'UP'     # spot rises = fresh entry
        else:
            need_pullback_dir = 'UP'    # spot bounces = pullback
            need_reentry_dir = 'DOWN'   # spot falls = fresh entry

        sig = {
            'id': self._data['next_id'],
            'magnet_trade_id': magnet_trade['id'],
            'symbol': magnet_trade['stock'],
            'timeframe': magnet_trade['timeframe'],
            'direction': direction,
            'st_value': magnet_trade['st_value'],
            'signal_price': magnet_trade.get('signal_price', 0),
            'signal_gap_pct': magnet_trade.get('signal_gap_pct', 0),
            'option_expiry': magnet_trade.get('option_expiry', ''),
            'initial_spot_15m_dir': spot_15m_dir,
            'need_pullback_dir': need_pullback_dir,
            'need_reentry_dir': need_reentry_dir,
            'pullback_at': None,
            'pullback_st': None,
            'pullback_spot': None,
            'reentry_at': None,
            'reentry_st': None,
            'reentry_spot': None,
            'status': 'watching',
            'picked_up_at': datetime.now().isoformat(),
            'alerted_at': None,
            'cancel_reason': None,
            'last_spot_15m_st': spot_15m_st,
            'last_spot_15m_dir': spot_15m_dir,
            'last_spot_ltp': spot_ltp,
            'last_checked_at': datetime.now().isoformat(),
        }

        # If spot is ALREADY in pullback direction, skip to pullback state
        if spot_15m_dir == need_pullback_dir:
            sig['status'] = 'pullback'
            sig['pullback_at'] = datetime.now().isoformat()
            sig['pullback_st'] = spot_15m_st
            sig['pullback_spot'] = spot_ltp
            log.info("ST #%d %s: already in pullback (%s), skipping to pullback state",
                     sig['id'], sig['symbol'], spot_15m_dir)

        self._data['next_id'] += 1
        self._data['signals'].append(sig)
        self._save()
        return sig

    def update_15m(self, sig_id: int, st_val: float, st_dir: str,
                   ltp: float):
        """Update the latest 15M ST data for a signal (no save, call save_batch)."""
        sig = self._find(sig_id)
        if sig:
            sig['last_spot_15m_st'] = st_val
            sig['last_spot_15m_dir'] = st_dir
            sig['last_spot_ltp'] = ltp
            sig['last_checked_at'] = datetime.now().isoformat()

    def mark_pullback(self, sig_id: int, st_val: float, ltp: float):
        """Transition: watching -> pullback."""
        sig = self._find(sig_id)
        if sig and sig['status'] == 'watching':
            sig['status'] = 'pullback'
            sig['pullback_at'] = datetime.now().isoformat()
            sig['pullback_st'] = st_val
            sig['pullback_spot'] = ltp
            self._save()

    def mark_ready(self, sig_id: int, st_val: float, ltp: float):
        """Transition: pullback -> alerted."""
        sig = self._find(sig_id)
        if sig and sig['status'] == 'pullback':
            sig['status'] = 'alerted'
            sig['reentry_at'] = datetime.now().isoformat()
            sig['reentry_st'] = st_val
            sig['reentry_spot'] = ltp
            sig['alerted_at'] = datetime.now().isoformat()
            self._save()

    def cancel(self, sig_id: int, reason: str):
        sig = self._find(sig_id)
        if sig and sig['status'] in ('watching', 'pullback'):
            sig['status'] = 'cancelled'
            sig['cancel_reason'] = reason
            self._save()

    def get_active(self) -> list:
        return [s for s in self._data['signals']
                if s['status'] in ('watching', 'pullback')]

    def list_all(self, status_filter=None) -> list:
        if status_filter:
            return [s for s in self._data['signals']
                    if s['status'] == status_filter]
        return list(self._data['signals'])

    def save_batch(self):
        """Explicit batch save after multiple update_15m calls."""
        self._save()


# ---------------------------------------------------------------------------
#  Read magnet_trades.json (READ-ONLY, single read per cycle)
# ---------------------------------------------------------------------------

_magnet_snapshot = None  # cached per poll cycle


def _load_magnet_snapshot() -> list:
    """Read magnet_trades.json once per poll cycle. Never writes."""
    global _magnet_snapshot
    if _magnet_snapshot is not None:
        return _magnet_snapshot
    if not _MAGNET_PATH.exists():
        _magnet_snapshot = []
        return []
    try:
        with open(_MAGNET_PATH) as f:
            _magnet_snapshot = json.load(f)
        return _magnet_snapshot
    except (json.JSONDecodeError, IOError) as e:
        log.warning("Could not read magnet_trades.json: %s", e)
        _magnet_snapshot = []
        return []


def _clear_magnet_snapshot():
    """Invalidate per-cycle cache. Call at start of each poll."""
    global _magnet_snapshot
    _magnet_snapshot = None


def _read_magnet_watching() -> list:
    """Filter WATCHING signals from the snapshot."""
    return [t for t in _load_magnet_snapshot() if t.get('status') == 'watching']


def _check_magnet_status(magnet_trade_id: int) -> str:
    """Check status from the same snapshot (no extra file read)."""
    for t in _load_magnet_snapshot():
        if t.get('id') == magnet_trade_id:
            return t.get('status', 'unknown')
    return 'not_found'


# ---------------------------------------------------------------------------
#  Spot 15M data + ST computation
# ---------------------------------------------------------------------------

_spot_token_cache = {}  # {symbol: instrument_token}
_15m_cache = {}         # {symbol: (st_summary, boundary_str)}


def _get_spot_token(kite, symbol: str) -> int:
    """Get NSE instrument token for a spot symbol. Cached per session."""
    if symbol in _spot_token_cache:
        return _spot_token_cache[symbol]
    ltp_data = kite.ltp([f'NSE:{symbol}'])
    tok = ltp_data[f'NSE:{symbol}']['instrument_token']
    _spot_token_cache[symbol] = tok
    return tok


def _current_15m_boundary_str() -> str:
    """Current 15-minute boundary as string 'HH:MM' for cache key.
    If within 5 seconds of a boundary, use the PREVIOUS boundary
    (candle may not be finalized yet)."""
    now = datetime.now()
    minute = now.minute - (now.minute % 15)
    if now.second < 5 and minute == now.minute:
        # We're in the first 5 seconds, use previous boundary
        prev = now.replace(minute=minute, second=0) - timedelta(minutes=15)
        return prev.strftime('%H:%M')
    return f"{now.hour:02d}:{minute:02d}"


def _evict_stale_cache(active_symbols: set):
    """Remove cache entries for symbols no longer tracked."""
    stale = [sym for sym in _15m_cache if sym not in active_symbols]
    for sym in stale:
        del _15m_cache[sym]
    stale_tok = [sym for sym in _spot_token_cache if sym not in active_symbols]
    for sym in stale_tok:
        del _spot_token_cache[sym]


def fetch_spot_15m_st(kite, symbol: str) -> Optional[dict]:
    """Fetch spot 15M candles and compute ST(10,3).

    Returns {st_value, direction, close, atr} or None.
    Uses per-symbol cache invalidated at each 15M boundary.
    Only uses the SECOND-TO-LAST ST point (last completed candle).
    """
    boundary = _current_15m_boundary_str()
    cached = _15m_cache.get(symbol)
    if cached:
        cached_result, cached_boundary = cached
        if cached_boundary == boundary:
            return cached_result

    # Fetch fresh data
    try:
        tok = _get_spot_token(kite, symbol)
        now = datetime.now()
        start = now - timedelta(days=_15M_FETCH_DAYS + 3)
        candles = kite.historical_data(
            tok, start.strftime('%Y-%m-%d'),
            now.strftime('%Y-%m-%d'), '15minute')
    except Exception as e:
        log.warning("15M fetch failed for %s: %s", symbol, e)
        return None

    if not candles or len(candles) < _MIN_15M_CANDLES:
        _15m_cache[symbol] = (None, boundary)
        return None

    from playbook.compute_st import compute_supertrend
    st_data = compute_supertrend(candles, _ST_PERIOD, _ST_MULTIPLIER)

    if not st_data or len(st_data) < 2:
        _15m_cache[symbol] = (None, boundary)
        return None

    # Use second-to-last: last COMPLETED candle (avoid partial candle flicker)
    completed = st_data[-2]
    result = {
        'st_value': completed['supertrend'],
        'direction': completed['direction'],
        'close': completed['close'],
        'atr': completed['atr'],
    }
    _15m_cache[symbol] = (result, boundary)
    return result


# ---------------------------------------------------------------------------
#  Core logic: pickup + poll
# ---------------------------------------------------------------------------

def pickup_new_signals(kite, tracker: SpotTracker, dry_run: bool = False):
    """Scan magnet_trades.json for new WATCHING signals and start tracking."""
    watching = _read_magnet_watching()
    if not watching:
        return

    # Capacity check
    if tracker.active_count() >= _MAX_ACTIVE_SIGNALS:
        log.info("At capacity (%d signals), skipping pickup", _MAX_ACTIVE_SIGNALS)
        return

    picked = 0
    for mt in watching:
        if tracker.active_count() >= _MAX_ACTIVE_SIGNALS:
            break

        if tracker.is_tracking(mt['id']):
            continue

        # Dedup by symbol (don't track same stock twice)
        active_syms = {s['symbol'] for s in tracker.get_active()}
        if mt['stock'] in active_syms:
            log.info("Already tracking %s, skipping magnet #%d", mt['stock'], mt['id'])
            continue

        # Fetch spot 15M ST
        st_info = fetch_spot_15m_st(kite, mt['stock'])
        if not st_info:
            log.info("No 15M data for %s, skipping", mt['stock'])
            continue

        # Get LTP
        try:
            ltp_data = kite.ltp([f"NSE:{mt['stock']}"])
            ltp = ltp_data[f"NSE:{mt['stock']}"]['last_price']
        except Exception:
            ltp = st_info['close']

        sig = tracker.add(mt, st_info['direction'], st_info['st_value'], ltp)
        picked += 1

        state_label = sig['status'].upper()
        print(f"  [strack] Picked up #{sig['id']} {sig['symbol']} "
              f"{sig['direction']} [{sig['timeframe']}] "
              f"15M={st_info['direction']} -> {state_label}")

        if sig['status'] == 'pullback':
            _send_telegram(
                f"<b>PULLBACK</b> <code>{sig['symbol']}</code> "
                f"{sig['direction']} [{sig['timeframe'][:1].upper()}]\n"
                f"Spot 15M already {st_info['direction']} (against {sig['direction']})\n"
                f"Watching for flip to {sig['need_reentry_dir']}...",
                dry_run=dry_run)

    if picked:
        print(f"  [strack] Picked up {picked} new signal(s)")


def poll_once(kite, tracker: SpotTracker, dry_run: bool = False):
    """Single monitoring pass: check all active signals for state transitions."""
    # Invalidate per-cycle magnet snapshot
    _clear_magnet_snapshot()

    active = tracker.get_active()
    if not active:
        return

    # Evict stale cache entries
    active_syms = {s['symbol'] for s in active}
    _evict_stale_cache(active_syms)

    now = datetime.now()
    dirty = False

    for sig in active:
        sym = sig['symbol']

        # 1. Check if magnet trade is still watching/entered
        mt_status = _check_magnet_status(sig['magnet_trade_id'])
        if mt_status not in ('watching', 'entered', 'unknown'):
            tracker.cancel(sig['id'], f'magnet trade {mt_status}')
            print(f"  [strack] #{sig['id']} {sym}: cancelled (magnet {mt_status})")
            continue

        # 2. DTE / max age check
        opt_expiry = sig.get('option_expiry', '')
        if opt_expiry:
            try:
                exp_date = datetime.strptime(str(opt_expiry)[:10], '%Y-%m-%d')
                dte = (exp_date - now).days
                if dte < _MIN_DTE_DAYS:
                    tracker.cancel(sig['id'], f'DTE too low ({dte}d)')
                    print(f"  [strack] #{sig['id']} {sym}: cancelled (DTE {dte}d)")
                    continue
            except (ValueError, TypeError):
                pass
        else:
            # No option_expiry: use default max days as backstop
            max_age = _DEFAULT_MAX_DAYS.get(sig['timeframe'], 12)
            picked_at = sig.get('picked_up_at', '')
            if picked_at:
                try:
                    picked_dt = datetime.fromisoformat(picked_at)
                    days_age = (now - picked_dt).total_seconds() / 86400
                    if days_age > max_age:
                        tracker.cancel(sig['id'],
                                       f'no expiry + age {days_age:.0f}d > {max_age}d')
                        print(f"  [strack] #{sig['id']} {sym}: cancelled (age {days_age:.0f}d)")
                        continue
                except Exception:
                    pass

        # 3. Stale check (separate from DTE -- catches stale signals WITH expiry too)
        #    For pullback state: measure from pullback_at (not picked_up_at) so
        #    signals that progressed aren't killed prematurely.
        if sig['status'] == 'pullback' and sig.get('pullback_at'):
            stale_ref = sig['pullback_at']
            # Pullback gets extra time (already past phase 1)
            max_days = _MAX_WATCHING_DAYS.get(sig['timeframe'], 10)
        else:
            stale_ref = sig.get('picked_up_at', '')
            max_days = _MAX_WATCHING_DAYS.get(sig['timeframe'], 10)

        if stale_ref:
            try:
                ref_dt = datetime.fromisoformat(stale_ref)
                days_waiting = (now - ref_dt).total_seconds() / 86400
                if days_waiting > max_days:
                    phase = sig['status']
                    tracker.cancel(sig['id'],
                                   f'stale {phase} ({days_waiting:.0f}d, max {max_days}d)')
                    print(f"  [strack] #{sig['id']} {sym}: cancelled (stale {phase})")
                    continue
            except Exception:
                pass

        # 4. Fetch spot 15M ST (uses completed candle only)
        st_info = fetch_spot_15m_st(kite, sym)
        if not st_info:
            print(f"  [strack] #{sig['id']} {sym}: no 15M data, skipping")
            continue

        try:
            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data[f'NSE:{sym}']['last_price']
        except Exception:
            ltp = st_info['close']

        cur_dir = st_info['direction']
        prev_dir = sig['last_spot_15m_dir']

        # FIX C2: Freshness guard -- skip transitions if last check was too long ago
        # (prevents false flips from overnight gaps or after market close)
        last_checked = sig.get('last_checked_at', '')
        is_fresh = True
        if last_checked:
            try:
                last_dt = datetime.fromisoformat(last_checked)
                minutes_since = (now - last_dt).total_seconds() / 60
                if minutes_since > _STALE_CHECK_MINUTES:
                    is_fresh = False
                    log.info("ST #%d %s: stale prev_dir (%d min), resync only",
                             sig['id'], sym, int(minutes_since))
            except Exception:
                is_fresh = False

        # Update latest data (always, even on stale resync)
        tracker.update_15m(sig['id'], st_info['st_value'], cur_dir, ltp)
        dirty = True

        # 5. State machine transitions (ONLY if prev_dir is fresh)
        if not is_fresh:
            gap = (ltp - sig['st_value']) / sig['st_value'] * 100
            print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                  f"15M={cur_dir:5s} (resync, skip transition) gap={gap:+.1f}%")
            continue

        if sig['status'] == 'watching':
            # Waiting for pullback: spot flips AGAINST our direction
            if cur_dir == sig['need_pullback_dir'] and prev_dir != cur_dir:
                tracker.mark_pullback(sig['id'], st_info['st_value'], ltp)
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']}: "
                      f"PULLBACK detected (15M -> {cur_dir})")

                _send_telegram(
                    f"<b>PULLBACK</b> <code>{sym}</code> "
                    f"{sig['direction']} [{sig['timeframe'][:1].upper()}]\n"
                    f"Spot 15M flipped {cur_dir} (against {sig['direction']})\n"
                    f"Watching for flip to {sig['need_reentry_dir']}...\n"
                    f"Spot {ltp:,.2f} | 15M ST {st_info['st_value']:,.2f}",
                    dry_run=dry_run)
            else:
                st_v = sig['st_value'] or 1  # guard div/0
                gap = (ltp - st_v) / st_v * 100
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                      f"15M={cur_dir:5s} need={sig['need_pullback_dir']} "
                      f"gap={gap:+.1f}% (watching)")

        elif sig['status'] == 'pullback':
            # Waiting for re-entry: spot flips BACK to our direction
            if cur_dir == sig['need_reentry_dir'] and prev_dir != cur_dir:
                tracker.mark_ready(sig['id'], st_info['st_value'], ltp)

                st_v = sig['st_value'] or 1
                gap = (ltp - st_v) / st_v * 100
                pullback_at = sig.get('pullback_at', '')
                try:
                    pb_dt = datetime.fromisoformat(pullback_at)
                    wait_hrs = (now - pb_dt).total_seconds() / 3600
                    wait_str = f"{wait_hrs:.0f}h"
                except Exception:
                    wait_str = "?"

                print(f"  [strack] #{sig['id']} {sym} {sig['direction']}: "
                      f"FLIP! 15M -> {cur_dir} | ENTER {sig['direction']} NOW")

                _send_telegram(
                    f"<b>SPOT FLIP</b> <code>{sym}</code> "
                    f"{sig['direction']} [{sig['timeframe'][:1].upper()}]\n"
                    f"Spot 15M ST flipped {cur_dir} -- "
                    f"enter {sig['direction']} now\n"
                    f"Spot {ltp:,.2f} | Higher-TF ST "
                    f"{sig['st_value']:,.2f} | Gap {gap:+.1f}%\n"
                    f"Pullback: {wait_str} ago | Fresh momentum confirmed",
                    dry_run=dry_run)
            else:
                st_v = sig['st_value'] or 1
                gap = (ltp - st_v) / st_v * 100
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                      f"15M={cur_dir:5s} need={sig['need_reentry_dir']} "
                      f"gap={gap:+.1f}% (pullback, waiting for flip)")

    if dirty:
        tracker.save_batch()


# ---------------------------------------------------------------------------
#  Market hours
# ---------------------------------------------------------------------------

def _is_market_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 100 + now.minute
    return 915 <= t <= 1530


def _seconds_to_next_15m() -> int:
    """Seconds until next 15M boundary + 5s buffer. Safe at all hours."""
    now = datetime.now()
    # Use timedelta to safely compute next boundary (no hour overflow)
    minutes_past = now.minute % 15
    delta = timedelta(minutes=(15 - minutes_past), seconds=(5 - now.second))
    if delta.total_seconds() <= 0:
        delta += timedelta(minutes=15)
    return max(int(delta.total_seconds()), 5)


# ---------------------------------------------------------------------------
#  CLI entry points
# ---------------------------------------------------------------------------

def cmd_scan(kite, tracker: SpotTracker, dry_run: bool = False):
    """One-shot: pick up new signals + poll once."""
    print(f"\n  [strack] {datetime.now().strftime('%H:%M:%S')} Scan + poll")
    pickup_new_signals(kite, tracker, dry_run=dry_run)
    poll_once(kite, tracker, dry_run=dry_run)


def cmd_status(tracker: SpotTracker):
    """Print status table of all tracked signals."""
    signals = tracker.list_all()
    if not signals:
        print("\n  [strack] No signals tracked yet.")
        return

    watching = [s for s in signals if s['status'] == 'watching']
    pullback = [s for s in signals if s['status'] == 'pullback']
    alerted = [s for s in signals if s['status'] == 'alerted']
    cancelled = [s for s in signals if s['status'] == 'cancelled']

    print(f"\n  === SPOT TRACKER ===")
    print(f"  Watching:   {len(watching)}  (waiting for pullback)")
    print(f"  Pullback:   {len(pullback)}  (waiting for re-flip)")
    print(f"  Alerted:    {len(alerted)}  (ENTER signal sent)")
    print(f"  Cancelled:  {len(cancelled)}")

    for label, group in [('WATCHING', watching), ('PULLBACK', pullback),
                         ('ALERTED', alerted)]:
        if not group:
            continue
        print(f"\n  --- {label} ---")
        for s in group:
            gap = 0
            st_v = s.get('st_value') or 0
            if s.get('last_spot_ltp') and st_v > 0:
                gap = (s['last_spot_ltp'] - st_v) / st_v * 100
            last_dir = s.get('last_spot_15m_dir', '?')
            need = (s['need_pullback_dir'] if s['status'] == 'watching'
                    else s['need_reentry_dir'])
            checked = s.get('last_checked_at', '')[:16]
            print(f"  #{s['id']:3d} {s['symbol']:15s} {s['direction']:<3} "
                  f"[{s['timeframe'][:1].upper()}] "
                  f"15M={last_dir:5s} need={need:5s} "
                  f"gap={gap:+.1f}% @ {checked}")

    if cancelled:
        print(f"\n  --- CANCELLED (last 5) ---")
        for s in cancelled[-5:]:
            print(f"  #{s['id']:3d} {s['symbol']:15s} "
                  f"{s.get('cancel_reason', '?')}")
    print()


def cmd_monitor(kite, tracker: SpotTracker, dry_run: bool = False):
    """Long-running monitor loop aligned to 15M candle boundaries."""
    print(f"\n  [strack] Monitor loop started (Ctrl+C to stop)")
    print(f"  [strack] Polling every 15M boundary + {_POLL_INTERVAL_SEC}s fallback\n")

    try:
        while True:
            if _is_market_hours():
                pickup_new_signals(kite, tracker, dry_run=dry_run)
                poll_once(kite, tracker, dry_run=dry_run)

                # Sleep until next 15M boundary
                sleep_sec = min(_seconds_to_next_15m(), _POLL_INTERVAL_SEC)
                print(f"  [strack] next poll in {sleep_sec}s "
                      f"({datetime.now().strftime('%H:%M:%S')})")
                time.sleep(sleep_sec)
            else:
                print(f"  [strack] Market closed. Waiting... "
                      f"({datetime.now().strftime('%H:%M:%S')})")
                time.sleep(60)
    except KeyboardInterrupt:
        print("\n  [strack] Monitor stopped.")


def cmd_run(kite, tracker: SpotTracker, dry_run: bool = False):
    """Single scan+poll with market hours check (cron target)."""
    if not _is_market_hours():
        print(f"  [strack] Outside market hours, skipping.")
        return
    cmd_scan(kite, tracker, dry_run=dry_run)


def get_tracker() -> SpotTracker:
    return SpotTracker()


def get_kite():
    from .scanner import _get_kite
    return _get_kite()

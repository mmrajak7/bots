"""Spot 15M ST Tracker -- parallel entry timing + exit heads-up.

Two modes:
  ENTRY TIMING (WATCHING signals):
    watching -> pullback (15M flips against) -> alerted (15M flips back = ENTER)

  EXIT HEADS-UP (ENTERED trades):
    exit_tracking -> exit_alert (15M flips against = informational heads-up)
    Terminal after one alert. NOT an exit command -- the magnet monitor's
    5-layer exit system handles actual exits. This is context only.

Reads magnet_trades.json (read-only). Own state in logs/spot_tracker.json.
Supplementary signal -- user decides manually whether to follow.
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
_MAX_WATCHING_DAYS = {         # auto-cancel if no flip within N days
    'daily': 3,
    'weekly': 10,
    'monthly': 20,
}
_DEFAULT_MAX_DAYS = {          # fallback when option_expiry is null
    'daily': 5,
    'weekly': 12,
    'monthly': 30,
}

# All "active" states (signals that need polling)
_ENTRY_ACTIVE = ('watching', 'pullback')
_EXIT_ACTIVE = ('exit_tracking',)          # exit_alert is terminal (heads-up only)
_ALL_ACTIVE = _ENTRY_ACTIVE + _EXIT_ACTIVE


# ---------------------------------------------------------------------------
#  Telegram
# ---------------------------------------------------------------------------

def _send_telegram(msg: str, dry_run: bool = False) -> bool:
    """Send Telegram to watching channel."""
    if dry_run:
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[DRY RUN] [SPOT] Telegram:\n{safe}\n")
        return True
    try:
        from .confidence_tracker import _send_telegram as _ct_send
        return _ct_send(msg, dry_run=False, channel='watching')
    except Exception:
        return _send_telegram_fallback(msg)


def _send_telegram_fallback(msg: str) -> bool:
    try:
        with open(_CONFIG_PATH) as f:
            mcfg = json.load(f)
        tg = mcfg.get('telegram_watching', {})
        bot_token, chat_id = tg.get('bot_token'), tg.get('chat_id')
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

    def _new_id(self) -> int:
        sid = self._data['next_id']
        self._data['next_id'] = sid + 1
        return sid

    # -- Direction helpers (shared by entry + exit) --

    @staticmethod
    def _dirs(direction: str):
        """Return (against_dir, with_dir) for a trade direction."""
        if direction == 'CE':
            return 'DOWN', 'UP'     # CE wants UP; against=DOWN
        return 'UP', 'DOWN'         # PE wants DOWN; against=UP

    # -- Query --

    def is_tracking(self, magnet_trade_id: int) -> bool:
        if magnet_trade_id == 0:
            return False  # Chartink-direct signals dedup by symbol, not ID
        return any(s['magnet_trade_id'] == magnet_trade_id
                   and s['status'] in _ENTRY_ACTIVE
                   for s in self._data['signals'])

    def is_tracking_exit(self, magnet_trade_id: int) -> bool:
        """Check if already tracking exit (active OR already alerted)."""
        if magnet_trade_id == 0:
            return False  # Chartink-direct signals dedup by symbol, not ID
        return any(s['magnet_trade_id'] == magnet_trade_id
                   and s['status'] in ('exit_tracking', 'exit_alert')
                   for s in self._data['signals'])

    def active_count(self) -> int:
        return len(self.get_active())

    def get_active(self) -> list:
        return [s for s in self._data['signals'] if s['status'] in _ALL_ACTIVE]

    def list_all(self, status_filter=None) -> list:
        if status_filter:
            return [s for s in self._data['signals']
                    if s['status'] == status_filter]
        return list(self._data['signals'])

    def save_batch(self):
        self._save()

    # -- Entry-timing signals (WATCHING magnet trades) --

    def add(self, magnet_trade: dict, spot_15m_dir: str,
            spot_15m_st: float, spot_ltp: float) -> dict:
        against_dir, with_dir = self._dirs(magnet_trade['direction'])

        sig = self._make_base_signal(magnet_trade, spot_15m_dir,
                                     spot_15m_st, spot_ltp)
        sig['tracking_type'] = 'entry'
        sig['need_pullback_dir'] = against_dir
        sig['need_reentry_dir'] = with_dir
        sig['status'] = 'watching'

        # Already in pullback direction? skip to pullback
        if spot_15m_dir == against_dir:
            sig['status'] = 'pullback'
            sig['pullback_at'] = datetime.now().isoformat()
            sig['pullback_st'] = spot_15m_st
            sig['pullback_spot'] = spot_ltp
            log.info("ST #%d %s: already in pullback (%s)",
                     sig['id'], sig['symbol'], spot_15m_dir)

        self._data['signals'].append(sig)
        self._save()
        return sig

    def mark_pullback(self, sig_id: int, st_val: float, ltp: float):
        sig = self._find(sig_id)
        if sig and sig['status'] == 'watching':
            sig['status'] = 'pullback'
            sig['pullback_at'] = datetime.now().isoformat()
            sig['pullback_st'] = st_val
            sig['pullback_spot'] = ltp
            self._save()

    def mark_alerted(self, sig_id: int, st_val: float, ltp: float):
        """Transition pullback -> alerted (terminal, ENTER signal)."""
        sig = self._find(sig_id)
        if sig and sig['status'] == 'pullback':
            sig['status'] = 'alerted'
            sig['reentry_at'] = datetime.now().isoformat()
            sig['reentry_st'] = st_val
            sig['reentry_spot'] = ltp
            sig['alerted_at'] = datetime.now().isoformat()
            self._save()

    # -- Exit-tracking signals (ENTERED magnet trades) --

    def add_exit_tracking(self, magnet_trade: dict, spot_15m_dir: str,
                          spot_15m_st: float, spot_ltp: float) -> dict:
        against_dir, with_dir = self._dirs(magnet_trade['direction'])

        sig = self._make_base_signal(magnet_trade, spot_15m_dir,
                                     spot_15m_st, spot_ltp)
        sig['tracking_type'] = 'exit'
        sig['need_pullback_dir'] = against_dir   # reused: flip against = EXIT
        sig['need_reentry_dir'] = with_dir       # reused: flip back = RE-ENTER
        sig['status'] = 'exit_tracking'
        sig['exit_alert_at'] = None
        sig['exit_alert_spot'] = None

        # Capture entry details from magnet trade
        sig['entry_spot'] = magnet_trade.get('entry_spot')
        sig['option_symbol'] = magnet_trade.get('option_symbol', '')
        sig['option_premium'] = magnet_trade.get('option_premium')

        self._data['signals'].append(sig)
        self._save()
        return sig

    def mark_exit_alert(self, sig_id: int, st_val: float, ltp: float):
        """exit_tracking -> exit_alert (terminal, heads-up only)."""
        sig = self._find(sig_id)
        if sig and sig['status'] == 'exit_tracking':
            sig['status'] = 'exit_alert'
            sig['exit_alert_at'] = datetime.now().isoformat()
            sig['exit_alert_spot'] = ltp
            sig['alerted_at'] = datetime.now().isoformat()
            self._save()

    # -- Shared --

    def cancel(self, sig_id: int, reason: str):
        sig = self._find(sig_id)
        if sig and sig['status'] in _ALL_ACTIVE:
            sig['status'] = 'cancelled'
            sig['cancel_reason'] = reason
            self._save()

    def update_15m(self, sig_id: int, st_val: float, st_dir: str, ltp: float):
        sig = self._find(sig_id)
        if sig:
            sig['last_spot_15m_st'] = st_val
            sig['last_spot_15m_dir'] = st_dir
            sig['last_spot_ltp'] = ltp
            sig['last_checked_at'] = datetime.now().isoformat()

    def _make_base_signal(self, magnet_trade: dict, spot_15m_dir: str,
                          spot_15m_st: float, spot_ltp: float) -> dict:
        return {
            'id': self._new_id(),
            'magnet_trade_id': magnet_trade['id'],
            'symbol': magnet_trade['stock'],
            'timeframe': magnet_trade['timeframe'],
            'direction': magnet_trade['direction'],
            'st_value': magnet_trade['st_value'],
            'signal_price': magnet_trade.get('signal_price', 0),
            'signal_gap_pct': magnet_trade.get('signal_gap_pct', 0),
            'option_expiry': magnet_trade.get('option_expiry', ''),
            'initial_spot_15m_dir': spot_15m_dir,
            'need_pullback_dir': None,   # set by caller
            'need_reentry_dir': None,    # set by caller
            'pullback_at': None,
            'pullback_st': None,
            'pullback_spot': None,
            'reentry_at': None,
            'reentry_st': None,
            'reentry_spot': None,
            'status': None,              # set by caller
            'tracking_type': None,       # set by caller
            'picked_up_at': datetime.now().isoformat(),
            'alerted_at': None,
            'cancel_reason': None,
            'last_spot_15m_st': spot_15m_st,
            'last_spot_15m_dir': spot_15m_dir,
            'last_spot_ltp': spot_ltp,
            'last_checked_at': datetime.now().isoformat(),
        }


# ---------------------------------------------------------------------------
#  Read magnet_trades.json (READ-ONLY, single read per cycle)
# ---------------------------------------------------------------------------

_magnet_snapshot = None


def _load_magnet_snapshot() -> list:
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
    global _magnet_snapshot
    _magnet_snapshot = None


def _read_magnet_by_status(status: str) -> list:
    return [t for t in _load_magnet_snapshot() if t.get('status') == status]


def _get_magnet_trade(magnet_trade_id: int) -> Optional[dict]:
    for t in _load_magnet_snapshot():
        if t.get('id') == magnet_trade_id:
            return t
    return None


# ---------------------------------------------------------------------------
#  Spot 15M data + ST computation
# ---------------------------------------------------------------------------

_spot_token_cache = {}
_15m_cache = {}


def _get_spot_token(kite, symbol: str) -> int:
    if symbol in _spot_token_cache:
        return _spot_token_cache[symbol]
    ltp_data = kite.ltp([f'NSE:{symbol}'])
    tok = ltp_data[f'NSE:{symbol}']['instrument_token']
    _spot_token_cache[symbol] = tok
    return tok


def _current_15m_boundary_str() -> str:
    now = datetime.now()
    minute = now.minute - (now.minute % 15)
    if now.second < 5 and minute == now.minute:
        prev = now.replace(minute=minute, second=0) - timedelta(minutes=15)
        return prev.strftime('%H:%M')
    return f"{now.hour:02d}:{minute:02d}"


def _evict_stale_cache(active_symbols: set):
    stale = [sym for sym in _15m_cache if sym not in active_symbols]
    for sym in stale:
        del _15m_cache[sym]
    stale_tok = [sym for sym in _spot_token_cache if sym not in active_symbols]
    for sym in stale_tok:
        del _spot_token_cache[sym]


def fetch_spot_15m_st(kite, symbol: str) -> Optional[dict]:
    """Fetch spot 15M candles, compute ST(10,3), return last completed candle."""
    boundary = _current_15m_boundary_str()
    cached = _15m_cache.get(symbol)
    if cached:
        cached_result, cached_boundary = cached
        if cached_boundary == boundary:
            return cached_result

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
#  Helpers
# ---------------------------------------------------------------------------

def _gap_pct(ltp: float, st_val: float) -> float:
    if st_val and st_val > 0:
        return (ltp - st_val) / st_val * 100
    return 0.0


def _is_fresh(sig: dict, now: datetime) -> bool:
    """Check if prev_dir is fresh enough for state transitions."""
    last_checked = sig.get('last_checked_at', '')
    if not last_checked:
        return False
    try:
        last_dt = datetime.fromisoformat(last_checked)
        return (now - last_dt).total_seconds() / 60 <= _STALE_CHECK_MINUTES
    except Exception:
        return False


def _check_stale(sig: dict, now: datetime) -> Optional[str]:
    """Return cancel reason if signal is stale, else None."""
    # For pullback/reentry_watch: measure from phase start, not pickup
    if sig['status'] in ('pullback', 'reentry_watch') and sig.get('pullback_at'):
        stale_ref = sig['pullback_at']
    else:
        stale_ref = sig.get('picked_up_at', '')

    if not stale_ref:
        return None
    try:
        ref_dt = datetime.fromisoformat(stale_ref)
        days = (now - ref_dt).total_seconds() / 86400
        max_days = _MAX_WATCHING_DAYS.get(sig['timeframe'], 10)
        if days > max_days:
            return f'stale {sig["status"]} ({days:.0f}d, max {max_days}d)'
    except Exception:
        pass
    return None


def _check_age_limit(sig: dict, now: datetime) -> Optional[str]:
    """Return cancel reason if signal exceeded age limit (no expiry), else None."""
    if sig.get('option_expiry'):
        try:
            exp = datetime.strptime(str(sig['option_expiry'])[:10], '%Y-%m-%d')
            if (exp - now).days < _MIN_DTE_DAYS:
                return f'DTE too low ({(exp - now).days}d)'
        except (ValueError, TypeError):
            pass
        return None
    # No expiry: age-based backstop
    picked_at = sig.get('picked_up_at', '')
    if not picked_at:
        return None
    try:
        picked_dt = datetime.fromisoformat(picked_at)
        days_age = (now - picked_dt).total_seconds() / 86400
        max_age = _DEFAULT_MAX_DAYS.get(sig['timeframe'], 12)
        if days_age > max_age:
            return f'age {days_age:.0f}d > {max_age}d limit'
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
#  Core: pickup from Chartink directly + ENTERED signals
# ---------------------------------------------------------------------------

def pickup_from_chartink(kite, tracker: SpotTracker, dry_run: bool = False):
    """Scan Chartink directly and start tracking immediately.

    Does NOT wait for magnet system validation (freshness, velocity, etc.).
    Any stock within 3% of ST gets tracked for pullback->flip entry timing.
    """
    if tracker.active_count() >= _MAX_ACTIVE_SIGNALS:
        return

    from .scanner import run_all_scanners, get_ltp, compute_st_for_stock
    from . import config as mcfg

    # Step 1: Chartink scan
    raw = run_all_scanners()
    if not raw:
        return

    # Dedup by stock (keep first timeframe)
    seen = set()
    unique = []
    for sig in raw:
        if sig['stock'] not in seen:
            seen.add(sig['stock'])
            unique.append(sig)

    # Already tracking OR already alerted — skip all of these
    active_syms = {s['symbol'] for s in tracker.get_active()}
    done_syms = {s['symbol'] for s in tracker.list_all()
                 if s['status'] in ('exit_alert', 'alerted')}
    skip_syms = active_syms | done_syms

    candidates = [s for s in unique if s['stock'] not in skip_syms]
    if not candidates:
        return

    # Step 2: LTP
    stocks = [s['stock'] for s in candidates]
    ltps = get_ltp(kite, stocks)

    picked = 0
    for sig in candidates:
        if tracker.active_count() >= _MAX_ACTIVE_SIGNALS:
            break

        stock = sig['stock']
        timeframe = sig['timeframe']
        price = ltps.get(stock)
        if not price:
            continue

        # Step 3: Compute higher-TF ST (for direction + target)
        st_info = compute_st_for_stock(kite, stock, timeframe)
        if not st_info:
            continue

        st_val = st_info['st']
        gap = abs(price - st_val) / st_val

        # Chartink pre-filters at 3.1%. Allow 3.5% for minor ST divergence.
        if gap > 0.035:
            continue

        # Direction: above ST = PE (expect fall), below ST = CE (expect rally)
        side = 'above' if price > st_val else 'below'
        direction = 'PE' if side == 'above' else 'CE'

        # Build a synthetic magnet-trade-like dict for add()
        pseudo_trade = {
            'id': 0,  # no magnet trade ID (independent signal)
            'stock': stock,
            'timeframe': timeframe,
            'direction': direction,
            'st_value': st_val,
            'signal_price': price,
            'signal_gap_pct': round(gap * 100, 2),
            'option_expiry': '',
        }

        # Fetch spot 15M ST
        spot_st = fetch_spot_15m_st(kite, stock)
        if not spot_st:
            continue

        sig_obj = tracker.add(pseudo_trade, spot_st['direction'],
                              spot_st['st_value'], price)
        picked += 1
        print(f"  [strack] Chartink #{sig_obj['id']} {stock} "
              f"{direction} [{timeframe}] gap={gap*100:.1f}% "
              f"15M={spot_st['direction']} -> {sig_obj['status'].upper()}")

        if sig_obj['status'] == 'pullback':
            _send_telegram(
                f"<b>PULLBACK</b> <code>{stock}</code> "
                f"{direction} [{timeframe[:1].upper()}]\n"
                f"Spot 15M already {spot_st['direction']} (against {direction})\n"
                f"Watching for flip to {sig_obj['need_reentry_dir']}...\n"
                f"Spot {price:,.2f} | ST {st_val:,.2f} | Gap {gap*100:.1f}%",
                dry_run=dry_run)

    if picked:
        print(f"  [strack] Picked up {picked} from Chartink")


def pickup_entered_trades(kite, tracker: SpotTracker, dry_run: bool = False):
    """Pick up ENTERED magnet trades for exit monitoring."""
    entered = _read_magnet_by_status('entered')
    if not entered:
        return

    picked = 0
    for mt in entered:
        if tracker.active_count() >= _MAX_ACTIVE_SIGNALS:
            break
        if tracker.is_tracking_exit(mt['id']):
            continue

        # If we had an entry-type signal for this trade, cancel it
        # (the trade entered via existing system, entry timing no longer relevant)
        for existing in tracker.get_active():
            if (existing['magnet_trade_id'] == mt['id']
                    and existing.get('tracking_type', 'entry') == 'entry'):
                tracker.cancel(existing['id'], 'magnet entered, switching to exit tracking')
                print(f"  [strack] #{existing['id']} {existing['symbol']}: "
                      f"entry signal cancelled (trade entered)")

        st_info = fetch_spot_15m_st(kite, mt['stock'])
        if not st_info:
            continue

        try:
            ltp = kite.ltp([f"NSE:{mt['stock']}"])[f"NSE:{mt['stock']}"]['last_price']
        except Exception:
            ltp = st_info['close']

        sig = tracker.add_exit_tracking(mt, st_info['direction'],
                                        st_info['st_value'], ltp)
        picked += 1
        print(f"  [strack] Exit tracking #{sig['id']} {sig['symbol']} "
              f"{sig['direction']} [{sig['timeframe']}] "
              f"15M={st_info['direction']} opt={sig.get('option_symbol', '?')}")

    if picked:
        print(f"  [strack] Picked up {picked} exit tracking signal(s)")


# ---------------------------------------------------------------------------
#  Core: poll all active signals
# ---------------------------------------------------------------------------

def poll_once(kite, tracker: SpotTracker, dry_run: bool = False):
    """Single monitoring pass for all active signals."""
    _clear_magnet_snapshot()

    active = tracker.get_active()
    if not active:
        return

    active_syms = {s['symbol'] for s in active}
    _evict_stale_cache(active_syms)

    now = datetime.now()
    dirty = False

    for sig in active:
        sym = sig['symbol']
        ttype = sig.get('tracking_type', 'entry')

        # -- Guards (shared for entry + exit) --

        # Magnet trade status check
        mt_id = sig.get('magnet_trade_id', 0)
        if mt_id and mt_id > 0:
            # Magnet-linked signal: check magnet trade lifecycle
            mt = _get_magnet_trade(mt_id)
            mt_status = mt.get('status', 'unknown') if mt else 'not_found'

            if ttype == 'entry':
                if mt_status not in ('watching', 'entered', 'unknown'):
                    tracker.cancel(sig['id'], f'magnet trade {mt_status}')
                    print(f"  [strack] #{sig['id']} {sym}: cancelled (magnet {mt_status})")
                    continue
                if mt_status == 'entered' and sig['status'] in _ENTRY_ACTIVE:
                    tracker.cancel(sig['id'], 'magnet entered, will track exit separately')
                    print(f"  [strack] #{sig['id']} {sym}: entry cancelled (magnet entered)")
                    continue
            elif ttype == 'exit':
                if mt_status in ('exited', 'cancelled', 'not_found'):
                    tracker.cancel(sig['id'], f'magnet trade {mt_status}')
                    print(f"  [strack] #{sig['id']} {sym}: exit tracking ended ({mt_status})")
                    continue
        elif ttype == 'entry' and sig['status'] in _ENTRY_ACTIVE:
            # Chartink-direct signal (mt_id=0): check if magnet entered this stock
            entered_stocks = {t['stock'] for t in _load_magnet_snapshot()
                              if t.get('status') == 'entered'}
            if sym in entered_stocks:
                tracker.cancel(sig['id'], 'stock entered via magnet')
                print(f"  [strack] #{sig['id']} {sym}: cancelled (magnet entered same stock)")
                continue

        # Age / DTE check (entry only; exit signals need alerts until expiry)
        if ttype == 'entry':
            age_reason = _check_age_limit(sig, now)
            if age_reason:
                tracker.cancel(sig['id'], age_reason)
                print(f"  [strack] #{sig['id']} {sym}: cancelled ({age_reason})")
                continue

        # Stale check (entry-type only; exit signals persist as long as trade is open)
        if ttype == 'entry':
            stale_reason = _check_stale(sig, now)
            if stale_reason:
                tracker.cancel(sig['id'], stale_reason)
                print(f"  [strack] #{sig['id']} {sym}: cancelled ({stale_reason})")
                continue

        # -- Fetch 15M ST --
        st_info = fetch_spot_15m_st(kite, sym)
        if not st_info:
            print(f"  [strack] #{sig['id']} {sym}: no 15M data, skipping")
            continue

        try:
            ltp = kite.ltp([f'NSE:{sym}'])[f'NSE:{sym}']['last_price']
        except Exception:
            ltp = st_info['close']

        cur_dir = st_info['direction']
        prev_dir = sig['last_spot_15m_dir']
        gap = _gap_pct(ltp, sig['st_value'])

        # Update state
        tracker.update_15m(sig['id'], st_info['st_value'], cur_dir, ltp)
        dirty = True

        # Freshness guard
        fresh = _is_fresh(sig, now)
        if not fresh:
            log.info("ST #%d %s: stale prev_dir, resync only", sig['id'], sym)
            print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                  f"15M={cur_dir:5s} (resync) gap={gap:+.1f}%")
            continue

        flipped = (cur_dir != prev_dir)
        tf_tag = sig['timeframe'][:1].upper()

        # -- State machine: ENTRY timing --
        if sig['status'] == 'watching':
            if flipped and cur_dir == sig['need_pullback_dir']:
                tracker.mark_pullback(sig['id'], st_info['st_value'], ltp)
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']}: "
                      f"PULLBACK (15M -> {cur_dir})")
                _send_telegram(
                    f"<b>PULLBACK</b> <code>{sym}</code> "
                    f"{sig['direction']} [{tf_tag}]\n"
                    f"Spot 15M flipped {cur_dir} (against {sig['direction']})\n"
                    f"Watching for flip to {sig['need_reentry_dir']}...\n"
                    f"Spot {ltp:,.2f} | 15M ST {st_info['st_value']:,.2f}",
                    dry_run=dry_run)
            else:
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                      f"15M={cur_dir:5s} need={sig['need_pullback_dir']} "
                      f"gap={gap:+.1f}% (watching)")

        elif sig['status'] == 'pullback':
            if flipped and cur_dir == sig['need_reentry_dir']:
                tracker.mark_alerted(sig['id'], st_info['st_value'], ltp)
                pb_at = sig.get('pullback_at', '')
                try:
                    wait_hrs = (now - datetime.fromisoformat(pb_at)).total_seconds() / 3600
                    wait_str = f"{wait_hrs:.0f}h"
                except Exception:
                    wait_str = "?"
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']}: "
                      f"FLIP! 15M -> {cur_dir} | ENTER {sig['direction']} NOW")
                _send_telegram(
                    f"<b>SPOT FLIP</b> <code>{sym}</code> "
                    f"{sig['direction']} [{tf_tag}]\n"
                    f"Spot 15M ST flipped {cur_dir} -- "
                    f"enter {sig['direction']} now\n"
                    f"Spot {ltp:,.2f} | Higher-TF ST "
                    f"{sig['st_value']:,.2f} | Gap {gap:+.1f}%\n"
                    f"Pullback: {wait_str} ago | Fresh momentum confirmed",
                    dry_run=dry_run)
            else:
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                      f"15M={cur_dir:5s} need={sig['need_reentry_dir']} "
                      f"gap={gap:+.1f}% (pullback)")

        # -- State machine: EXIT tracking (heads-up, not exit command) --
        elif sig['status'] == 'exit_tracking':
            if flipped and cur_dir == sig['need_pullback_dir']:
                tracker.mark_exit_alert(sig['id'], st_info['st_value'], ltp)
                opt_sym = sig.get('option_symbol', '')
                entry_spot = sig.get('entry_spot', 0)
                spot_chg = ""
                if entry_spot and entry_spot > 0:
                    chg = (ltp - entry_spot) / entry_spot * 100
                    spot_chg = f"\nEntry spot {entry_spot:,.2f} | Now {ltp:,.2f} ({chg:+.1f}%)"
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']}: "
                      f"HEADS UP! 15M -> {cur_dir} (against {sig['direction']})")
                _send_telegram(
                    f"<b>SPOT CAUTION</b> <code>{sym}</code> "
                    f"{sig['direction']} [{tf_tag}]\n"
                    f"Spot 15M ST flipped {cur_dir} (against {sig['direction']})\n"
                    f"<i>Heads up only -- magnet SL system handles exit</i>\n"
                    f"Spot {ltp:,.2f} | 15M ST {st_info['st_value']:,.2f}"
                    f"{spot_chg}\n"
                    f"Option: <code>{opt_sym}</code>",
                    dry_run=dry_run)
            else:
                print(f"  [strack] #{sig['id']} {sym} {sig['direction']:<3} "
                      f"15M={cur_dir:5s} (exit tracking, holding) gap={gap:+.1f}%")

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
    now = datetime.now()
    minutes_past = now.minute % 15
    delta = timedelta(minutes=(15 - minutes_past), seconds=(5 - now.second))
    if delta.total_seconds() <= 0:
        delta += timedelta(minutes=15)
    return max(int(delta.total_seconds()), 5)


# ---------------------------------------------------------------------------
#  CLI entry points
# ---------------------------------------------------------------------------

def cmd_scan(kite, tracker: SpotTracker, dry_run: bool = False):
    """One-shot: Chartink scan + pick up entered trades + poll."""
    print(f"\n  [strack] {datetime.now().strftime('%H:%M:%S')} Scan + poll")
    pickup_entered_trades(kite, tracker, dry_run=dry_run)   # exit first (protect capital)
    pickup_from_chartink(kite, tracker, dry_run=dry_run)    # direct from Chartink
    poll_once(kite, tracker, dry_run=dry_run)


def cmd_status(tracker: SpotTracker):
    """Print status table of all tracked signals."""
    signals = tracker.list_all()
    if not signals:
        print("\n  [strack] No signals tracked yet.")
        return

    by_status = {}
    for s in signals:
        by_status.setdefault(s['status'], []).append(s)

    # Count by type
    entry_active = [s for s in signals if s['status'] in _ENTRY_ACTIVE]
    exit_active = [s for s in signals if s['status'] in _EXIT_ACTIVE]

    print(f"\n  === SPOT TRACKER ===")
    print(f"  Entry signals:  {len(entry_active)} active")
    print(f"  Exit tracking:  {len(exit_active)} active")

    labels = [
        ('WATCHING (entry)', 'watching'),
        ('PULLBACK (entry)', 'pullback'),
        ('EXIT TRACKING', 'exit_tracking'),
        ('SPOT CAUTION sent', 'exit_alert'),
        ('ALERTED (enter)', 'alerted'),
    ]

    for label, status in labels:
        group = by_status.get(status, [])
        if not group:
            continue
        print(f"\n  --- {label} ---")
        for s in group:
            gap = _gap_pct(s.get('last_spot_ltp', 0), s.get('st_value', 0))
            last_dir = s.get('last_spot_15m_dir', '?')
            ttype = s.get('tracking_type', '?')

            if status in ('watching',):
                need = s['need_pullback_dir']
            elif status in ('exit_tracking',):
                need = s['need_pullback_dir']  # flip against = exit
            else:
                need = s.get('need_reentry_dir', '?')

            checked = s.get('last_checked_at', '')[:16]
            opt = s.get('option_symbol', '')
            opt_tag = f" opt={opt}" if opt else ""

            print(f"  #{s['id']:3d} {s['symbol']:15s} {s['direction']:<3} "
                  f"[{s['timeframe'][:1].upper()}] "
                  f"15M={last_dir:5s} need={need:5s} "
                  f"gap={gap:+.1f}% [{ttype}]{opt_tag}")

    cancelled = by_status.get('cancelled', [])
    if cancelled:
        print(f"\n  --- CANCELLED (last 5) ---")
        for s in cancelled[-5:]:
            print(f"  #{s['id']:3d} {s['symbol']:15s} "
                  f"[{s.get('tracking_type', '?')}] {s.get('cancel_reason', '?')}")
    print()


def cmd_monitor(kite, tracker: SpotTracker, dry_run: bool = False):
    """Long-running monitor loop aligned to 15M candle boundaries."""
    print(f"\n  [strack] Monitor loop started (Ctrl+C to stop)")
    print(f"  [strack] Polling every 15M boundary\n")

    try:
        while True:
            if _is_market_hours():
                pickup_entered_trades(kite, tracker, dry_run=dry_run)
                pickup_from_chartink(kite, tracker, dry_run=dry_run)
                poll_once(kite, tracker, dry_run=dry_run)

                sleep_sec = min(_seconds_to_next_15m(), _POLL_INTERVAL_SEC)
                print(f"  [strack] next poll in {sleep_sec}s "
                      f"({datetime.now().strftime('%H:%M:%S')})")
                time.sleep(sleep_sec)
            else:
                print(f"  [strack] Market closed. "
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

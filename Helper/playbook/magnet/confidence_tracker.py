"""Confidence Tracker -- watches magnet signals, alerts on entry/exit timing.

Flow:
  1. User adds a signal (ctrack add NAUKRI ...) after magnet fires.
  2. Tracker polls every 5 min, runs score_signal, waits for ET >= 28/40.
  3. When entry threshold met -> sends ENTER NOW Telegram alert.
  4. After user enters manually (ctrack enter ID), tracker watches for
     exhaustion signals and sends TAKE PROFIT / EXIT alerts.

Storage: logs/confidence_tracker.json (local only, no Drive sync).
"""

import json
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Paths
_HERE = Path(__file__).resolve().parent
_PLAYBOOK = _HERE.parent
_HELPER = _PLAYBOOK.parent
_BOTS = _HELPER.parent
_CONFIG_PATH = _HELPER / 'config' / 'magnet_config.json'
_TRACKER_PATH = _HELPER / 'logs' / 'confidence_tracker.json'

log = logging.getLogger(__name__)

# Lazy module-level imports from confidence.py (avoid circular at import time)
_detect_reversal_candle = None
_compute_hourly_st = None


def _ensure_confidence_imports():
    """Import confidence helpers on first use."""
    global _detect_reversal_candle, _compute_hourly_st
    if _detect_reversal_candle is None:
        from .confidence import _detect_reversal_candle as _drc, _compute_hourly_st as _chs
        _detect_reversal_candle = _drc
        _compute_hourly_st = _chs


# Entry timing thresholds
_ET_ENTER_MIN = 28        # ET >= 28/40 to trigger ENTER alert
_PULLBACK_ENTER_MIN = 6   # pullback dimension >= 6/8 to trigger ENTER alert


# ---------------------------------------------------------------------------
#  Telegram helpers
# ---------------------------------------------------------------------------

def _load_tg_config():
    """Load Telegram bot config from magnet_config.json."""
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        tg = cfg.get('telegram_watching', {})
        return tg.get('bot_token'), tg.get('chat_id')
    except Exception as e:
        log.warning("Could not load Telegram config: %s", e)
        return None, None


def _send_telegram(msg: str, dry_run: bool = False) -> bool:
    """Send a Telegram message. Returns True on success."""
    if dry_run:
        # Strip Unicode for Windows console
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        print(f"[DRY RUN] Telegram:\n{safe}\n")
        return True
    bot_token, chat_id = _load_tg_config()
    if not bot_token or not chat_id:
        log.warning("Telegram config missing -- skipping alert")
        return False
    try:
        import requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        resp = requests.post(url, json={
            'chat_id': chat_id,
            'text': msg,
            'parse_mode': 'HTML',
        }, timeout=10)
        if resp.status_code == 200:
            return True
        log.warning("Telegram error %s: %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False


def _tg_stars(score: int) -> str:
    """Return Unicode star rating for Telegram. Example: star*4 (4/5)."""
    star = '\u2b50'
    n = (5 if score >= 80 else 4 if score >= 65 else
         3 if score >= 50 else 2 if score >= 35 else 1)
    return star * n + f" ({n}/5)"


def _target_word(need_up: bool) -> str:
    return 'resistance' if need_up else 'support'


# ---------------------------------------------------------------------------
#  Alert formatters
# ---------------------------------------------------------------------------

def format_watch_alert(result: dict, option: Optional[dict] = None) -> str:
    """Format WATCHING alert for Telegram."""
    s = result
    sym = s['symbol']
    opt = s['option_type']
    stars = _tg_stars(s['total_score'])
    tgt_word = _target_word(s['need_up'])
    gap_abs = abs(s['gap_pct'])

    lines = [
        f"{stars} <b>WATCHING {sym} {opt}</b> | {s['total_score']}/100",
        "",
        f"Approaching {tgt_word} at {s['target_st']:,.0f} ({gap_abs:.1f}%)",
        f"Signal: {s['sq_total']}/60 | Entry: {s['et_total']}/40",
        "Waiting for pullback...",
    ]

    if option:
        opt_sym = option.get('symbol', '')
        opt_price = option.get('price', 0)
        opt_qty = option.get('qty', '')
        lines.append("")
        lines.append(f"Trade: {opt_sym} @ {opt_price:.2f} | Qty {opt_qty}")

    # Setup summary from strengths
    if s.get('strengths'):
        lines.append(f"Setup: {s['strengths'][0]}")

    return "\n".join(lines)


def format_enter_alert(result: dict, option: Optional[dict] = None) -> str:
    """Format ENTER NOW alert for Telegram."""
    s = result
    sym = s['symbol']
    opt = s['option_type']
    stars = _tg_stars(s['total_score'])
    tgt_word = _target_word(s['need_up'])
    gap_abs = abs(s['gap_pct'])

    lines = [
        f"{stars} <b>ENTER {sym} {opt}</b> | {s['total_score']}/100",
        "",
        f"Spot: {s['ltp']:,.0f} -> {tgt_word} at {s['target_st']:,.0f} ({gap_abs:.1f}%)",
    ]

    # Pullback description (ET pullback detail)
    pb_detail = s.get('et_dims', {}).get('pullback', (0, 8, ''))[2]
    if pb_detail:
        lines.append(pb_detail)

    # Support pattern (ET support detail)
    sup_detail = s.get('et_dims', {}).get('support', (0, 8, ''))[2]
    if sup_detail and 'No clear' not in sup_detail:
        lines.append(sup_detail)

    # Market health -- extract from score dimension detail
    mkt_dim = s.get('sq_dims', {}).get('market', (0, 7, ''))
    mkt_score = mkt_dim[0]
    if mkt_score >= 6:
        lines.append("Market: NIFTY healthy")
    elif mkt_score >= 4:
        lines.append("Market: NIFTY mixed")
    elif mkt_score >= 2:
        lines.append("Market: NIFTY weak")
    else:
        lines.append("Market: NIFTY bearish -- caution")

    if option:
        opt_sym = option.get('symbol', '')
        opt_price = option.get('price', 0)
        opt_qty = option.get('qty', '')
        sl_spot = option.get('sl_spot', 0)
        lines.append("")
        sl_str = f" | SL: {sl_spot:,.0f}" if sl_spot else ""
        lines.append(f"Trade: {opt_sym} @ {opt_price:.2f}")
        lines.append(f"Qty: {opt_qty}{sl_str}")

    return "\n".join(lines)


def format_exit_alert(symbol: str, direction: str, entry_price: float,
                      ltp: float, reason: str,
                      pnl_pct: Optional[float] = None,
                      option_ltp: Optional[float] = None,
                      entry_option_price: Optional[float] = None) -> str:
    """Format EXIT or TAKE PROFIT alert for Telegram."""
    need_up = direction == 'CE'

    # Direction-aware spot move: positive = in our favour
    if not entry_price:
        spot_move = 0.0
    elif need_up:
        spot_move = (ltp - entry_price) / entry_price * 100
    else:
        spot_move = (entry_price - ltp) / entry_price * 100

    is_profit = spot_move > 0

    if is_profit:
        header = f"!! <b>{symbol} {direction} - TAKE PROFIT</b>"
    else:
        header = f"XX <b>{symbol} {direction} - EXIT</b>"

    lines = [
        header,
        "",
        f"Spot: {entry_price:,.0f} -> {ltp:,.0f} ({spot_move:+.1f}%)",
    ]

    # Option P&L line (if available)
    if option_ltp and entry_option_price:
        opt_pnl = (option_ltp - entry_option_price) / entry_option_price * 100
        lines.append(f"Option: {entry_option_price:.2f} -> {option_ltp:.2f} ({opt_pnl:+.0f}%)")
    elif pnl_pct is not None:
        lines.append(f"Option P&L: {pnl_pct:+.1f}%")

    lines.append("")
    lines.append(reason)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  ConfidenceTracker class
# ---------------------------------------------------------------------------

class ConfidenceTracker:
    """Stores and manages confidence tracker signals."""

    def __init__(self, path: Path = _TRACKER_PATH):
        self.path = path
        self._signals: list = []
        self._next_id: int = 1
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    data = json.load(f)
                self._signals = data.get('signals', [])
                self._next_id = data.get('next_id', 1)
            except Exception as e:
                log.warning("Failed to load tracker: %s", e)
                self._signals = []
                self._next_id = 1
        else:
            self._signals = []
            self._next_id = 1

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump({'next_id': self._next_id, 'signals': self._signals},
                      f, indent=2, default=str)
        tmp.replace(self.path)

    def add(self, symbol: str, timeframe: str, target_st: float,
            direction: str, option_symbol: str, option_price: float,
            quantity: int, sl_spot: float, signal_price: float,
            score: int = 0, grade: str = '', sq: int = 0, et: int = 0,
            target_label: str = '') -> dict:
        """Add a new signal to track."""
        sig = {
            'id': self._next_id,
            'symbol': symbol,
            'timeframe': timeframe,
            'target_st': target_st,
            'target_label': target_label or ('resistance' if direction == 'CE' else 'support'),
            'direction': direction,
            'option_symbol': option_symbol,
            'option_price': option_price,
            'quantity': quantity,
            'sl_spot': sl_spot,
            'signal_price': signal_price,
            'signal_at': datetime.now().isoformat(timespec='seconds'),
            'status': 'watching',
            'score': score,
            'grade': grade,
            'sq': sq,
            'et': et,
            'entry_price': None,
            'entry_option_price': None,
            'entry_at': None,
            'exit_reason': None,
            'exit_at': None,
            'scores': [],
        }
        self._signals.append(sig)
        self._next_id += 1
        self._save()
        return sig

    def _find(self, sig_id: int) -> Optional[dict]:
        for s in self._signals:
            if s['id'] == sig_id:
                return s
        return None

    def get_watching(self) -> list:
        return [s for s in self._signals if s['status'] == 'watching']

    def get_ready(self) -> list:
        return [s for s in self._signals if s['status'] == 'ready']

    def get_entered(self) -> list:
        return [s for s in self._signals if s['status'] == 'entered']

    def update_score(self, sig_id: int, total: int, sq: int, et: int):
        """Record a score snapshot."""
        sig = self._find(sig_id)
        if not sig:
            return
        sig['score'] = total
        sig['sq'] = sq
        sig['et'] = et
        sig['scores'].append({
            'at': datetime.now().isoformat(timespec='seconds'),
            'total': total, 'sq': sq, 'et': et,
        })
        self._save()

    def mark_ready(self, sig_id: int):
        """Transition watching -> ready (entry threshold met)."""
        sig = self._find(sig_id)
        if sig and sig['status'] == 'watching':
            sig['status'] = 'ready'
            self._save()

    def mark_entered(self, sig_id: int, entry_price: float,
                     entry_option_price: float):
        """Mark signal as entered by user."""
        sig = self._find(sig_id)
        if not sig:
            return
        if not entry_price or entry_price <= 0:
            log.warning("mark_entered #%s: invalid spot price %s", sig_id, entry_price)
        sig['status'] = 'entered'
        sig['entry_price'] = entry_price
        sig['entry_option_price'] = entry_option_price
        sig['entry_at'] = datetime.now().isoformat(timespec='seconds')
        self._save()

    def mark_exited(self, sig_id: int, reason: str,
                    exit_spot: float = 0, exit_option_price: float = 0):
        """Mark signal as exited with P&L."""
        sig = self._find(sig_id)
        if not sig:
            return
        sig['status'] = 'exited'
        sig['exit_reason'] = reason
        sig['exit_at'] = datetime.now().isoformat(timespec='seconds')
        sig['exit_spot'] = exit_spot
        sig['exit_option_price'] = exit_option_price
        # Compute P&L if we have entry + exit option prices
        entry_opt = sig.get('entry_option_price') or 0
        qty = sig.get('quantity') or 0
        if entry_opt > 0 and exit_option_price > 0 and qty > 0:
            sig['pnl'] = round((exit_option_price - entry_opt) * qty, 2)
            sig['pnl_pct'] = round((exit_option_price - entry_opt) / entry_opt * 100, 1)
        else:
            sig['pnl'] = None
            sig['pnl_pct'] = None
        self._save()

    def cancel(self, sig_id: int, reason: str = 'cancelled'):
        """Cancel a watching/ready signal."""
        sig = self._find(sig_id)
        if sig and sig['status'] in ('watching', 'ready'):
            sig['status'] = 'cancelled'
            sig['exit_reason'] = reason
            sig['exit_at'] = datetime.now().isoformat(timespec='seconds')
            self._save()

    def list_all(self, status_filter: Optional[str] = None) -> list:
        if status_filter:
            return [s for s in self._signals if s['status'] == status_filter]
        return list(self._signals)


def get_tracker() -> ConfidenceTracker:
    return ConfidenceTracker()


# ---------------------------------------------------------------------------
#  Exhaustion detection
# ---------------------------------------------------------------------------

def check_exhaustion(ltp: float, entry_price: float, target_st: float,
                     need_up: bool,
                     daily: Optional[list] = None,
                     hourly: Optional[list] = None) -> Optional[dict]:
    """Check if position is showing exhaustion near target.

    Returns None (no action) or dict with action + reason.
    Actions: 'TARGET HIT', 'TAKE PROFIT', 'CAUTION', 'EXIT'
    """
    if not entry_price:
        return None
    tgt_word = _target_word(need_up)
    in_profit = (ltp > entry_price) if need_up else (ltp < entry_price)
    move_pct = abs((ltp - entry_price) / entry_price * 100)

    # 1. Target reached
    if need_up and ltp >= target_st:
        return {'action': 'TARGET HIT',
                'reason': f'Reached {tgt_word} at {target_st:,.0f}'}
    if not need_up and ltp <= target_st:
        return {'action': 'TARGET HIT',
                'reason': f'Reached {tgt_word} at {target_st:,.0f}'}

    # Distance from target
    dist_pct = abs(ltp - target_st) / target_st * 100

    # 2. Very close to target + stalling
    if dist_pct <= 0.3:
        return {'action': 'TAKE PROFIT',
                'reason': f'Near {tgt_word}, stalling ({dist_pct:.1f}% away)'}

    # 3. Within 1% of target -- check reversal candle
    if dist_pct <= 1.0 and daily and len(daily) >= 2:
        _ensure_confidence_imports()
        # For CE near resistance: look for bearish candle (reversal from UP)
        rc_name, rc_str = _detect_reversal_candle(daily[-3:], not need_up)
        if rc_str >= 5:
            return {'action': 'TAKE PROFIT',
                    'reason': (f'Reversal candle ({rc_name.replace("_", " ")}) '
                               f'near {tgt_word}')}

        # Also check hourly
        if hourly and len(hourly) >= 2:
            hrc_name, hrc_str = _detect_reversal_candle(hourly[-3:], not need_up)
            if hrc_str >= 5:
                return {'action': 'TAKE PROFIT',
                        'reason': (f'1hr reversal candle ({hrc_name.replace("_", " ")}) '
                                   f'near {tgt_word}')}

    # 4. Hourly ST flipped against position AND in profit
    if in_profit and move_pct >= 1.0 and hourly and len(hourly) >= 15:
        _ensure_confidence_imports()
        h_st = _compute_hourly_st(hourly)
        if h_st:
            h_dir = h_st['direction']
            flipped_against = (need_up and h_dir == 'DOWN') or (not need_up and h_dir == 'UP')
            if flipped_against:
                return {'action': 'CAUTION',
                        'reason': f'1hr ST flipped against position, in +{move_pct:.1f}% profit'}

    # 5. Thesis broken: spot moved against entry
    # 2% for early exit (naked options lose premium fast), 3% for hard exit
    loss_pct = ((entry_price - ltp) / entry_price * 100 if need_up
                else (ltp - entry_price) / entry_price * 100)
    if loss_pct >= 3.0:
        return {'action': 'EXIT',
                'reason': f'{tgt_word.title()} broken, moved {loss_pct:.1f}% against'}
    if loss_pct >= 2.0:
        return {'action': 'CAUTION',
                'reason': f'Moved {loss_pct:.1f}% against entry, watch closely'}

    return None


# ---------------------------------------------------------------------------
#  Monitor functions
# ---------------------------------------------------------------------------

def _fetch_option_ltp(kite, option_symbol):
    """Fetch current option LTP. Returns 0 on failure."""
    if not option_symbol:
        return 0
    try:
        data = kite.ltp([f"NFO:{option_symbol}"])
        return data.get(f"NFO:{option_symbol}", {}).get('last_price') or 0
    except Exception:
        return 0


# Per-session data cache: heavy fetches (daily 6Y, hourly 30d) only once per day
_data_cache = {}  # {symbol: {'date': 'YYYY-MM-DD', 'daily': [...], 'hourly': [...]}}


def _get_cached_data(kite, symbol, fetch_daily, fetch_hourly, fetch_15min,
                     get_token):
    """Fetch daily+hourly once per day, 15min fresh each call."""
    today = datetime.now().strftime('%Y-%m-%d')
    tok = get_token(kite, symbol)

    cached = _data_cache.get(symbol)
    if cached and cached.get('date') == today:
        daily = cached['daily']
        hourly = cached['hourly']
    else:
        daily = fetch_daily(kite, tok)
        hourly = fetch_hourly(kite, tok)
        _data_cache[symbol] = {'date': today, 'daily': daily, 'hourly': hourly}

    # 15min always fresh (entry timing)
    m15 = fetch_15min(kite, tok)
    return tok, daily, hourly, m15


def monitor_once(kite, tracker: ConfidenceTracker, dry_run: bool = False):
    """Single monitoring pass: check all watching + entered signals."""
    from .confidence import (
        score_signal, _get_instrument_token, _fetch_daily,
        _fetch_hourly, _fetch_15min, _compute_all_st,
    )

    watching = tracker.get_watching() + tracker.get_ready()
    entered = tracker.get_entered()

    if not watching and not entered:
        print("  No active signals to monitor.")
        return

    print(f"\n  [ctrack] {datetime.now().strftime('%H:%M:%S')} -- "
          f"{len(watching)} watching, {len(entered)} entered")

    # --- Check watching signals ---
    for sig in watching:
        sym = sig['symbol']
        try:
            tok, daily, hourly, m15 = _get_cached_data(
                kite, sym, _fetch_daily, _fetch_hourly, _fetch_15min,
                _get_instrument_token)

            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data.get(f'NSE:{sym}', {}).get('last_price')
            if ltp is None:
                print(f"  #{sig['id']} {sym}: SKIP -- no LTP from Kite")
                continue

            st_data = _compute_all_st(daily)
            result = score_signal(sym, ltp, st_data, daily, hourly, m15,
                                  target_tf=sig['timeframe'], kite=kite)

            if 'error' in result:
                print(f"  #{sig['id']} {sym}: score error -- {result['error']}")
                continue

            total = result['total_score']
            sq_v = result['sq_total']
            et_v = result['et_total']
            tracker.update_score(sig['id'], total, sq_v, et_v)

            # Get pullback score from dims
            pb_score = result.get('et_dims', {}).get('pullback', (0, 8, ''))[0]
            ready = (et_v >= _ET_ENTER_MIN and pb_score >= _PULLBACK_ENTER_MIN)

            # --- Gap 1: SL invalidation while watching ---
            ltp_val = result.get('ltp', ltp)
            need_up = sig['direction'] == 'CE'
            sl = sig.get('sl_spot')
            if sl:
                sl_hit = (need_up and ltp_val <= sl) or (not need_up and ltp_val >= sl)
                if sl_hit:
                    tracker.cancel(sig['id'], f'SL breached while watching (spot {ltp_val:,.0f})')
                    msg = format_exit_alert(sym, sig['direction'], sig['signal_price'],
                                            ltp_val, f'SL hit at {sl:,.0f} before entry')
                    _send_telegram(msg, dry_run=dry_run)
                    print(f"  #{sig['id']} {sym}: SL HIT while watching -> cancelled")
                    continue

            # --- Gap 2: Momentum entry override ---
            # If gap closes to <0.5% and signal quality high, don't miss the trade
            gap_abs = abs(result.get('gap_pct', 99))
            if not ready and gap_abs < 0.5 and sq_v >= 45:
                ready = True  # override -- about to touch, can't wait

            # --- Gap 3: Stale signal auto-cancel + theta Telegram alert ---
            signal_at = sig.get('signal_at', '')
            if signal_at:
                try:
                    sig_dt = datetime.fromisoformat(signal_at)
                    days_waiting = (datetime.now() - sig_dt).total_seconds() / 86400
                    tf = sig.get('timeframe', 'weekly')
                    # Auto-cancel limits: daily=1d, weekly=5d, monthly=20d
                    max_days = {'daily': 1, 'weekly': 5, 'monthly': 20}.get(tf, 5)
                    if days_waiting > max_days:
                        tracker.cancel(sig['id'],
                                       f'stale: watching {days_waiting:.0f}d (max {max_days}d for {tf})')
                        _send_telegram(
                            f"[X] {sym} {sig['direction']} CANCELLED\n"
                            f"Watching for {days_waiting:.0f} days (max {max_days} for {tf})",
                            dry_run=dry_run)
                        print(f"  #{sig['id']} {sym}: AUTO-CANCELLED (stale {days_waiting:.0f}d)")
                        continue
                    elif days_waiting > 3 and not sig.get('theta_warned'):
                        sig['theta_warned'] = True
                        tracker._save()
                        _send_telegram(
                            f"[!] {sym} {sig['direction']} watching for {days_waiting:.1f} days\n"
                            f"Option theta decaying. Consider cancelling.",
                            dry_run=dry_run)
                        print(f"  #{sig['id']} {sym}: THETA WARNING sent ({days_waiting:.1f}d)")
                except Exception:
                    pass

            print(f"  #{sig['id']} {sym} {sig['direction']:<3} "
                  f"SQ={sq_v}/60 ET={et_v}/40 Total={total}/100 "
                  f"pb={pb_score}/8 gap={gap_abs:.1f}% "
                  f"{'-> ENTER' if ready else '(watching)'}")

            # --- Gap 7: Ready -> watching revert if timing degrades ---
            if not ready and sig['status'] == 'ready':
                sig['status'] = 'watching'
                tracker._save()
                print(f"  #{sig['id']} {sym}: timing degraded, reverted to watching")

            if ready and sig['status'] == 'watching':
                # Only send ENTER alert on watching -> ready transition
                tracker.mark_ready(sig['id'])
                # Fetch fresh option price
                opt_price = sig['option_price']
                try:
                    ltp_opt = kite.ltp([f"NFO:{sig['option_symbol']}"])
                    fresh = ltp_opt.get(f"NFO:{sig['option_symbol']}", {}).get('last_price')
                    if fresh is not None:
                        opt_price = fresh
                except Exception:
                    pass

                opt_info = {
                    'symbol': sig['option_symbol'],
                    'price': opt_price,
                    'qty': sig['quantity'],
                    'sl_spot': sig['sl_spot'],
                }
                msg = format_enter_alert(result, option=opt_info)
                _send_telegram(msg, dry_run=dry_run)
                print(f"  -> ENTER alert sent for #{sig['id']} {sym}")

        except Exception as e:
            print(f"  #{sig['id']} {sym}: ERROR -- {e}")
            log.exception("Error checking watching signal #%s %s", sig['id'], sym)

    # --- Check entered signals ---
    now_dt = datetime.now()
    for sig in entered:
        sym = sig['symbol']
        try:
            tok, daily, hourly, _ = _get_cached_data(
                kite, sym, _fetch_daily, _fetch_hourly, _fetch_15min,
                _get_instrument_token)

            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data.get(f'NSE:{sym}', {}).get('last_price')
            if ltp is None:
                print(f"  #{sig['id']} {sym}: SKIP -- no LTP from Kite")
                continue

            entry_price = sig.get('entry_price') or sig['signal_price']
            need_up = sig['direction'] == 'CE'

            if not entry_price:
                print(f"  #{sig['id']} {sym}: SKIP -- no entry price")
                continue

            # --- FIX: EOD exit for daily timeframe trades (15:15) ---
            if sig.get('timeframe') == 'daily' and now_dt.hour >= 15 and now_dt.minute >= 15:
                opt_ltp = _fetch_option_ltp(kite, sig.get('option_symbol'))
                entry_opt = sig.get('entry_option_price') or 0
                msg = format_exit_alert(
                    sym, sig['direction'], entry_price, ltp,
                    'EOD exit -- daily trade closing at 15:15',
                    option_ltp=opt_ltp, entry_option_price=entry_opt or None)
                _send_telegram(msg, dry_run=dry_run)
                tracker.mark_exited(sig['id'], 'eod_daily',
                                    exit_spot=ltp, exit_option_price=opt_ltp)
                print(f"  #{sig['id']} {sym}: EOD daily exit")
                continue

            # --- FIX: Time SL (5 trading days max hold) ---
            entry_at = sig.get('entry_at', '')
            if entry_at:
                try:
                    entry_dt = datetime.fromisoformat(entry_at)
                    biz_days = 0
                    d = entry_dt + timedelta(days=1)
                    while d <= now_dt:
                        if d.weekday() < 5:
                            biz_days += 1
                        d += timedelta(days=1)
                    if biz_days >= 5:
                        opt_ltp = _fetch_option_ltp(kite, sig.get('option_symbol'))
                        entry_opt = sig.get('entry_option_price') or 0
                        msg = format_exit_alert(
                            sym, sig['direction'], entry_price, ltp,
                            f'Time SL -- held {biz_days} trading days',
                            option_ltp=opt_ltp, entry_option_price=entry_opt or None)
                        _send_telegram(msg, dry_run=dry_run)
                        tracker.mark_exited(sig['id'], f'time_sl_{biz_days}d',
                                            exit_spot=ltp, exit_option_price=opt_ltp)
                        print(f"  #{sig['id']} {sym}: TIME SL ({biz_days}d)")
                        continue
                except Exception:
                    pass

            # --- Exhaustion check ---
            result = check_exhaustion(
                ltp, entry_price, sig['target_st'], need_up,
                daily=daily, hourly=hourly,
            )

            move_pct = ((ltp - entry_price) / entry_price * 100
                        if need_up
                        else (entry_price - ltp) / entry_price * 100)

            print(f"  #{sig['id']} {sym} {sig['direction']:<3} "
                  f"entry={entry_price:,.0f} ltp={ltp:,.0f} "
                  f"move={move_pct:+.1f}% "
                  f"-> {result['action'] if result else 'holding'}")

            if result:
                action = result['action']
                reason = result['reason']
                entry_opt_price = sig.get('entry_option_price') or 0
                opt_ltp = _fetch_option_ltp(kite, sig.get('option_symbol'))
                pnl_pct = None
                if entry_opt_price and opt_ltp:
                    pnl_pct = (opt_ltp - entry_opt_price) / entry_opt_price * 100

                if action in ('TARGET HIT', 'TAKE PROFIT', 'EXIT'):
                    msg = format_exit_alert(
                        sym, sig['direction'], entry_price, ltp, reason,
                        pnl_pct=pnl_pct,
                        option_ltp=opt_ltp if opt_ltp else None,
                        entry_option_price=entry_opt_price if entry_opt_price else None,
                    )
                    _send_telegram(msg, dry_run=dry_run)
                    # FIX: Auto-mark as exited (prevents duplicate alerts)
                    tracker.mark_exited(sig['id'], f'{action}: {reason}',
                                        exit_spot=ltp, exit_option_price=opt_ltp)
                    print(f"  -> {action} #{sig['id']} {sym} -- auto-closed")
                elif action == 'CAUTION':
                    # FIX: Dedup CAUTION alerts (30 min cooldown)
                    last_caution = sig.get('last_caution_at', '')
                    cooldown_ok = True
                    if last_caution:
                        try:
                            lc_dt = datetime.fromisoformat(last_caution)
                            cooldown_ok = (now_dt - lc_dt).total_seconds() > 1800
                        except Exception:
                            pass
                    if cooldown_ok:
                        caution_msg = (
                            f"[!] <b>CAUTION {sym} {sig['direction']}</b>\n"
                            f"\n{reason}\nLTP: {ltp:,.0f} | Move: {move_pct:+.1f}%"
                        )
                        _send_telegram(caution_msg, dry_run=dry_run)
                        sig['last_caution_at'] = now_dt.isoformat(timespec='seconds')
                        tracker._save()
                        print(f"  -> CAUTION #{sig['id']} {sym}")

        except Exception as e:
            print(f"  #{sig['id']} {sym}: ERROR -- {e}")
            log.exception("Error checking entered signal #%s %s", sig['id'], sym)


def monitor_loop(kite, tracker: ConfidenceTracker, dry_run: bool = False):
    """Long-running poll loop. Runs every 5 min during market hours."""
    POLL_INTERVAL = 300  # 5 minutes
    WAIT_SLEEP = 30      # sleep interval while waiting for market
    WAIT_LOG_INTERVAL = 300  # print "waiting" message every 5 minutes

    print("  Confidence tracker monitor started. Ctrl+C to stop.")

    last_wait_msg = 0  # timestamp of last "waiting for market" print

    while True:
        now = datetime.now()
        # Market hours: Mon-Fri 9:15-15:30
        is_weekday = now.weekday() < 5
        market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
        market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)

        if not is_weekday or now < market_open or now > market_close:
            if now > market_close and is_weekday:
                print(f"  Market closed ({now.strftime('%H:%M')}). Exiting.")
                break
            if not is_weekday:
                print(f"  Weekend ({now.strftime('%A')}). Exiting.")
                break
            # Before market open -- wait with periodic status messages
            wait_secs = (market_open - now).total_seconds()
            now_ts = time.time()
            if now_ts - last_wait_msg >= WAIT_LOG_INTERVAL:
                print(f"  Waiting for market open ({wait_secs/60:.0f} min remaining)...")
                last_wait_msg = now_ts
            try:
                time.sleep(min(wait_secs, WAIT_SLEEP))
            except KeyboardInterrupt:
                print("\n  Stopped by user.")
                break
            continue

        try:
            monitor_once(kite, tracker, dry_run=dry_run)
        except KeyboardInterrupt:
            print("\n  Stopped by user.")
            break
        except Exception as e:
            log.error("monitor_loop error: %s", e)
            print(f"  Loop error: {e}")

        # Wait for next poll
        next_run = datetime.now() + timedelta(seconds=POLL_INTERVAL)
        print(f"  Next check at {next_run.strftime('%H:%M:%S')} "
              f"(in {POLL_INTERVAL//60} min)")
        try:
            time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            print("\n  Stopped by user.")
            break


# ---------------------------------------------------------------------------
#  Console list display
# ---------------------------------------------------------------------------

def print_tracker_list(signals: list):
    """Print tracker signals in a table."""
    if not signals:
        print("  No signals tracked.")
        return

    print(f"\n  {'ID':<4} {'Symbol':<10} {'Dir':<4} {'TF':<8} "
          f"{'Target':<10} {'Status':<10} {'Score':<6} {'Entry':<8} {'P&L':>10}")
    print(f"  {'-' * 78}")
    total_pnl = 0
    wins = losses = 0
    for s in signals:
        entry_val = s.get('entry_price')
        entry_str = f"{entry_val:,.0f}" if entry_val else '--'
        pnl = s.get('pnl')
        pnl_str = f"Rs {pnl:+,.0f}" if pnl is not None else ''
        if pnl is not None:
            total_pnl += pnl
            if pnl >= 0:
                wins += 1
            else:
                losses += 1
        sig_id = s.get('id', '?')
        sym = s.get('symbol', '?')
        direction = s.get('direction', '?')
        timeframe = s.get('timeframe', '?')
        target_st = s.get('target_st', 0)
        status = s.get('status', '?')
        score = s.get('score', 0)
        print(f"  {sig_id:<4} {sym:<10} {direction:<4} "
              f"{timeframe:<8} {target_st:<10,.0f} "
              f"{status:<10} {score:<6} {entry_str:<8} {pnl_str:>10}")
        if s.get('exit_reason'):
            print(f"  {'':4} Exit: {s['exit_reason']}")
    if wins + losses > 0:
        print(f"  {'-' * 78}")
        print(f"  {'':4} Total P&L: Rs {total_pnl:+,.0f} | "
              f"{wins}W / {losses}L | "
              f"Win rate: {wins/(wins+losses)*100:.0f}%")
    print()

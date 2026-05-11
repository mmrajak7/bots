"""Confidence Tracker v2 -- 15M SuperTrend on option chart for entry/exit.

Flow:
  1. Magnet scanner detects stock near higher-TF ST → WATCHING signal.
  2. Monitor delegates to tracker → picks option → computes 15M ST on option.
  3. When option price pulls back to within 3% above 15M ST → ENTER alert.
     Also detects 15M candle wick touches (low within threshold, close above ST).
     Sends APPROACHING alert at 5% gap for early heads-up.
  4. After entry: if 15M candle CLOSES below ST → EXIT (15M ST broken).
  5. Existing exits preserved: TP (spot touches higher-TF ST), Time SL (5d),
     EOD daily, spot SL.

Key insight: 15M ST on the OPTION chart gives clean pullback entries
with minimum risk. Multiple touches without a candle close below = hold.
Only UP ST matters (we always buy options).

Storage: logs/confidence_tracker.json (local + Drive sync).
"""

import html
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

# Entry/exit thresholds
_ENTRY_GAP_PCT = 0.03     # alert when option price within 3% above 15M ST
_ENTRY_GAP_ABS = 0.30     # absolute floor for cheap options (Rs 0.30 minimum gap tolerance)
_ST_PERIOD = 10
_ST_MULTIPLIER = 3
_MIN_15M_CANDLES = 15     # minimum candles for ST computation
_15M_FETCH_DAYS = 10      # fetch 10 days of 15M data (covers ~7 trading days)
_GAP_DOWN_PCT = -0.03     # cancel if option drops >3% below 15M support
_THETA_DECAY_PCT = 0.40   # cancel if option loses >40% from initial price while watching
_MIN_DTE_AT_ENTRY = 3     # minimum DTE to allow auto-entry
_WICK_LTP_MAX_PCT = 0.05  # wick entry only if current LTP within 5% of ST
_APPROACH_GAP_PCT = 0.05  # send approaching alert when gap <= 5%
_APPROACH_RESET_PCT = 0.10  # reset approaching flag when gap widens above 10%
_MIN_OPTION_PRICE = 0.50  # skip only near-worthless options (< Rs 0.50, no meaningful price action)
_SPOT_DIVERGE_PCT = 0.10  # cancel if spot moved >10% away from target ST (thesis dead)


# ---------------------------------------------------------------------------
#  Telegram helpers
# ---------------------------------------------------------------------------

_MAGNET_TG_CONFIG = _BOTS / 'data' / 'telegram_config.json'  # trade alerts


def _load_tg_config(channel='watching'):
    """Load Telegram bot config.

    channel='watching' -> telegram_watching from magnet_config.json
    channel='trade'    -> data/telegram_config.json
    """
    try:
        if channel == 'trade':
            with open(_MAGNET_TG_CONFIG) as f:
                tg = json.load(f)
        else:
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
            tg = cfg.get('telegram_watching', {})
        return tg.get('bot_token'), tg.get('chat_id')
    except Exception as e:
        log.warning("Could not load Telegram config (%s): %s", channel, e)
        return None, None


def _watch_channel_enabled() -> bool:
    """Is the 'watching' Telegram channel enabled?

    Kill-switch for WATCHING / APPROACHING / CANCEL / FLIP / CAUTION alerts.
    Set `telegram_watching.enabled: false` in magnet_config.json to silence.

    On config error: returns False (safer default — alerts stay silent rather
    than spam unexpectedly if the user has explicitly disabled them).
    Missing key: returns True (back-compat for installations without the flag).
    """
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
        return cfg.get('telegram_watching', {}).get('enabled', True) is not False
    except Exception as e:
        log.warning("Could not read telegram_watching.enabled (%s) — "
                    "defaulting to SILENT for safety", e)
        return False


_SILENCED = True  # 2026-05-11: confidence_tracker deprecated, replaced by zebra package.


def _send_telegram(msg: str, dry_run: bool = False,
                   channel: str = 'watching') -> bool:
    """Send Telegram message to specified channel."""
    if _SILENCED:
        return True  # silent success
    # Kill-switch: watching-channel alerts are silenced when disabled
    if channel != 'trade' and not _watch_channel_enabled():
        return True  # silent success
    if dry_run:
        safe = msg.encode('ascii', errors='replace').decode('ascii')
        tag = '[TRADE]' if channel == 'trade' else '[WATCH]'
        print(f"[DRY RUN] {tag} Telegram:\n{safe}\n")
        return True
    bot_token, chat_id = _load_tg_config(channel)
    if not bot_token or not chat_id:
        log.warning("Telegram config missing (%s) -- skipping alert", channel)
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
    """Unicode star rating. Example: star*4 (4/5)."""
    star = '\u2b50'
    n = (5 if score >= 80 else 4 if score >= 65 else
         3 if score >= 50 else 2 if score >= 35 else 1)
    return star * n + f" ({n}/5)"


# ---------------------------------------------------------------------------
#  15M ST computation for options
# ---------------------------------------------------------------------------

_nfo_token_cache: dict = {}  # {option_symbol: instrument_token}


def _get_option_token(kite, option_symbol: str) -> int:
    """Get NFO instrument token for an option symbol. Cached per session."""
    if option_symbol in _nfo_token_cache:
        return _nfo_token_cache[option_symbol]

    instruments = kite.instruments('NFO')
    for inst in instruments:
        _nfo_token_cache[inst['tradingsymbol']] = inst['instrument_token']

    tok = _nfo_token_cache.get(option_symbol)
    if tok is None:
        raise ValueError(f"NFO instrument token not found: {option_symbol}")
    return tok


def _fetch_option_15m(kite, option_symbol: str, days: int = _15M_FETCH_DAYS) -> list:
    """Fetch 15-minute candles for an option from Kite."""
    tok = _get_option_token(kite, option_symbol)
    now = datetime.now()
    start = now - timedelta(days=days + 3)  # pad for weekends/holidays
    candles = kite.historical_data(
        tok, start.strftime('%Y-%m-%d'), now.strftime('%Y-%m-%d'), '15minute')
    return candles


def compute_option_15m_st(candles: list) -> Optional[dict]:
    """Compute 15M ST(10,3) on option candles.

    Returns last ST point: {supertrend, direction, close, ...} or None.
    Only care about UP direction (support line for bought options).
    """
    if not candles or len(candles) < _MIN_15M_CANDLES:
        return None

    from playbook.compute_st import compute_supertrend
    st_data = compute_supertrend(candles, _ST_PERIOD, _ST_MULTIPLIER)
    if not st_data:
        return None

    last = st_data[-1]
    return {
        'st_value': last['supertrend'],
        'direction': last['direction'],
        'close': last['close'],
        'atr': last['atr'],
        'candles_used': len(candles),
    }


def _check_15m_st_break(candles: list) -> Optional[dict]:
    """Check if the 15M ST direction has flipped from UP to DOWN.

    A break means the option's uptrend support is gone — time to exit.
    Uses the last 3 ST points to detect the flip:
      - st[-3] was UP (support was intact)
      - st[-2] flipped to DOWN (the completed candle that broke it)
      - st[-1] current (may be incomplete)

    Returns break info dict if broken, None if intact.
    """
    if not candles or len(candles) < _MIN_15M_CANDLES + 2:
        return None

    from playbook.compute_st import compute_supertrend
    st_data = compute_supertrend(candles, _ST_PERIOD, _ST_MULTIPLIER)
    if not st_data or len(st_data) < 3:
        return None

    prev_prev = st_data[-3]  # candle before the break
    prev = st_data[-2]       # last completed candle (potential breaker)
    curr = st_data[-1]       # current running candle

    # Primary check: direction flipped UP → DOWN on the last completed candle
    if prev_prev['direction'] == 'UP' and prev['direction'] == 'DOWN':
        return {
            'broken': True,
            'candle_close': prev['close'],
            'st_value': prev_prev['supertrend'],  # ST value before break
            'new_direction': prev['direction'],
        }

    # Secondary: current candle flipped (less reliable since candle is running,
    # but catches fast breaks)
    if prev['direction'] == 'UP' and curr['direction'] == 'DOWN':
        return {
            'broken': True,
            'candle_close': curr['close'],
            'st_value': prev['supertrend'],
            'new_direction': 'DOWN',
        }

    return None


# ---------------------------------------------------------------------------
#  Alert formatters (compact, technical)
# ---------------------------------------------------------------------------

def format_watch_alert(result: dict, option: Optional[dict] = None,
                       st15m: Optional[dict] = None,
                       corr_block: str = '') -> str:
    """Format compact WATCHING alert (5 lines max).

    Line 1: Stars + score + stock + direction + TF
    Line 2: Spot + Potential (how far stock is from target)
    Line 3: Strengths (why interesting)
    Line 4: Risks (what to watch)
    Line 5: Option symbol + price + qty + 15M support level
    """
    s = result
    sym = s['symbol']
    opt = s['option_type']
    stars = _tg_stars(s['total_score'])
    gap_abs = abs(s['gap_pct'])
    tf_short = {'monthly': 'M', 'weekly': 'W', 'daily': 'D'}.get(
        s.get('target_tf', ''), '')

    lines = [
        f"{stars} <b>WATCHING</b> <code>{sym}</code> {opt} [{tf_short}] | {s['total_score']}/100",
        f"Spot {s['ltp']:,.0f} | Potential {s['target_st']:,.0f} ({gap_abs:.1f}%)",
    ]

    # Strengths (compact, 1 line)
    strengths = s.get('strengths', [])
    if strengths:
        short = [_shorten(st) for st in strengths[:3]]
        lines.append(f"\u2713 {', '.join(short)}")

    # Risks (compact, 1 line)
    risks = s.get('risks', [])
    if risks:
        short_r = [_shorten(r) for r in risks[:2]]
        lines.append(f"\u2717 {' | '.join(short_r)}")

    # Option info + 15M support merged into one line
    if option:
        opt_sym = option.get('symbol', '')
        opt_price = option.get('price', 0)
        opt_qty = option.get('qty', '')
        sup_str = ''
        if st15m:
            dir_icon = '\u2191' if st15m['direction'] == 'UP' else '\u2193'
            sup_str = f" | Sup {st15m['st_value']:.2f}{dir_icon}"
        lines.append(f"<code>{opt_sym}</code> @ {opt_price:.2f} | {opt_qty} qty{sup_str}")

    # NIFTY context — plain-English tailwind/headwind for informed decision
    mkt_ctx = s.get('market_context', '')
    if mkt_ctx:
        if 'tailwind' in mkt_ctx:
            lines.append(f"\U0001f7e2 {mkt_ctx}")
        elif 'headwind' in mkt_ctx:
            lines.append(f"\U0001f534 {mkt_ctx}")
        else:
            lines.append(f"\u26aa {mkt_ctx}")

    # Correlation block (if provided)
    if corr_block:
        lines.append(corr_block)

    return "\n".join(lines)


def format_enter_alert(sym: str, direction: str, tf: str,
                       option_symbol: str, opt_ltp: float,
                       st_value: float, qty: int,
                       sl_spot: float, score: int = 0,
                       via_wick: bool = False,
                       signal_price: Optional[float] = None,
                       signal_at: Optional[str] = None,
                       target_spot: Optional[float] = None,
                       spot_now: Optional[float] = None,
                       tight_sl_spot: Optional[float] = None,
                       synth: Optional[dict] = None) -> str:
    """Format compact ENTER alert — option near 15M support.

    Additional spot context (signal date/price, spot move, target) appended
    when the caller passes it. Gracefully degrades if any field is missing.
    """
    tf_short = {'monthly': 'M', 'weekly': 'W', 'daily': 'D'}.get(tf, tf)
    gap = (opt_ltp - st_value) / st_value * 100 if st_value > 0 else 0
    method = 'WICK ENTER' if via_wick else 'ENTER'

    sl_line = f"<code>{option_symbol}</code> | {qty} qty | SL {sl_spot:,.0f}"
    if tight_sl_spot:
        sl_line += f" | Tight {tight_sl_spot:,.0f}"
    lines = [
        f"\U0001f7e2 <b>{method}</b> <code>{sym}</code> {direction} [{tf_short}]",
        f"Option @ {opt_ltp:.2f} | 15M Support {st_value:.2f} ({gap:+.1f}%)",
        sl_line,
    ]

    # Spot context block (signal date, signal price, spot move, target)
    if signal_price and spot_now:
        sig_date = ''
        if signal_at:
            try:
                sig_date = datetime.fromisoformat(signal_at).strftime('%d-%b %H:%M')
            except Exception:
                sig_date = str(signal_at)[:16]

        spot_mov = (spot_now - signal_price) / signal_price * 100
        # Invert for PE (down move is favorable)
        if direction == 'PE':
            spot_mov = -spot_mov

        pieces = [f"Spot {spot_now:,.1f} ({spot_mov:+.2f}% since signal"]
        if sig_date:
            pieces.append(f"@ {sig_date}")
        pieces.append(f"{signal_price:,.1f})")
        lines.append(" ".join(pieces[:1] + [p for p in pieces[1:]]))

        if target_spot:
            remaining = (target_spot - spot_now) / spot_now * 100
            if direction == 'PE':
                remaining = -remaining
            lines.append(
                f"Target {target_spot:,.1f} ({remaining:+.2f}% to go)"
            )

    # Synthetic alternative (execution-ready, you decide)
    if synth and synth.get('viable'):
        opp = synth.get('opp_symbol', '?')
        opp_bid = synth.get('opp_bid', 0)
        opp_ask = synth.get('opp_ask', 0)
        opp_oi = synth.get('opp_oi', 0)
        opp_sprd = synth.get('opp_spread_pct', 0)
        long_sprd = synth.get('long_spread_pct', 0)
        net_debit = synth.get('net_debit')
        notional = synth.get('notional')
        margin_low = synth.get('margin_low')
        margin_high = synth.get('margin_high')
        cash_per_lot = synth.get('cash_per_lot')
        synth_type = synth.get('synth_type', 'LONG')  # LONG (CE) or SHORT (PE)

        lines.append("")  # blank separator
        lines.append(f"⚡ <b>SYNTHETIC {synth_type}</b> (you decide, not auto-executed)")
        lines.append(
            f"Buy {direction} @ {opt_ltp:.2f} + Sell {synth.get('opp_type', '?')} "
            f"@ {opp_bid:.2f} (bid)"
        )
        lines.append(
            f"<code>{opp}</code> bid/ask {opp_bid:.2f}/{opp_ask:.2f} "
            f"(sprd {opp_sprd:.1f}%) | OI {opp_oi:,}"
        )
        if long_sprd > 0:
            lines.append(f"Long-leg sprd {long_sprd:.1f}% (round-trip cost view)")
        if net_debit is not None and cash_per_lot is not None:
            credit_tag = "debit" if net_debit >= 0 else "credit"
            lines.append(
                f"Net {credit_tag} = Rs {abs(net_debit):.2f}/sh "
                f"(Rs {abs(cash_per_lot):,.0f} {'out' if net_debit>=0 else 'in'} per lot)"
            )
        if notional and margin_low and margin_high:
            lines.append(
                f"Notional Rs {notional/100000:.2f}L | "
                f"SPAN typical Rs {margin_low/100000:.2f}-{margin_high/100000:.2f}L "
                f"(18-40%)"
            )

    return "\n".join(lines)


def format_exit_15m_st(sym: str, direction: str,
                       entry_opt: float, exit_opt: float,
                       st_value: float, candle_close: float,
                       qty: int, days_held: int = 0) -> str:
    """Format EXIT alert — 15M support broken."""
    pnl = (exit_opt - entry_opt) * qty
    pnl_pct = (exit_opt - entry_opt) / entry_opt * 100 if entry_opt > 0 else 0
    pnl_icon = '\u2705' if pnl >= 0 else '\u274c'

    lines = [
        f"{pnl_icon} <b>15M BREAK</b> <code>{sym}</code> {direction}",
        f"Close {candle_close:.2f} &lt; Support {st_value:.2f}",
        f"{entry_opt:.2f}\u2192{exit_opt:.2f} | "
        f"<b>Rs {pnl:+,.0f}</b> ({pnl_pct:+.1f}%) {days_held}d",
    ]
    return "\n".join(lines)


def format_exit_alert(symbol: str, direction: str, entry_price: float,
                      ltp: float, reason: str,
                      pnl_pct: Optional[float] = None,
                      option_ltp: Optional[float] = None,
                      entry_option_price: Optional[float] = None) -> str:
    """Format EXIT/TP alert (for spot TP, time SL, EOD)."""
    need_up = direction == 'CE'
    if not entry_price:
        spot_move = 0.0
    elif need_up:
        spot_move = (ltp - entry_price) / entry_price * 100
    else:
        spot_move = (entry_price - ltp) / entry_price * 100

    is_profit = spot_move > 0
    if is_profit:
        header = f"\u2705 <b>TP</b> <code>{symbol}</code> {direction}"
    else:
        header = f"\u274c <b>EXIT</b> <code>{symbol}</code> {direction}"

    lines = [header, f"Spot: {entry_price:,.0f} \u2192 {ltp:,.0f} ({spot_move:+.1f}%)"]

    if option_ltp and entry_option_price:
        opt_pnl = (option_ltp - entry_option_price) / entry_option_price * 100
        lines.append(f"Option: {entry_option_price:.2f} \u2192 {option_ltp:.2f} ({opt_pnl:+.0f}%)")
    elif pnl_pct is not None:
        lines.append(f"Option P&L: {pnl_pct:+.1f}%")

    lines.append(reason)
    return "\n".join(lines)


def _shorten(text: str, max_len: int = 45) -> str:
    """Shorten a dimension detail string for compact display."""
    for prefix in ('Monthly ', 'Daily ', 'NIFTY '):
        text = text.replace(prefix, '')
    text = text.replace(' -- ', ': ')
    if len(text) > max_len:
        text = text[:max_len - 1] + '\u2026'
    return html.escape(text)


# ---------------------------------------------------------------------------
#  ConfidenceTracker class
# ---------------------------------------------------------------------------

class ConfidenceTracker:
    """Stores and manages confidence tracker signals.

    Drive sync: uploads to Google Drive alongside magnet_trades.json.
    """

    def __init__(self, path: Path = _TRACKER_PATH):
        self.path = path
        self._signals: list = []
        self._next_id: int = 1
        self._drive_service = None
        self._drive_file_id = None
        self._drive_enabled = False
        self._load()
        self._init_drive()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path) as f:
                    data = json.load(f)
                self._signals = data.get('signals', [])
                self._next_id = data.get('next_id', 1)
            except (json.JSONDecodeError, ValueError) as e:
                backup = self.path.with_suffix(
                    f'.corrupt.{int(time.time())}.json')
                try:
                    self.path.rename(backup)
                    log.critical("Tracker file CORRUPT (%s). Backed up to %s", e, backup)
                except OSError:
                    log.critical("Tracker file CORRUPT (%s). Backup rename failed.", e)
                self._signals = []
                self._next_id = 1
            except Exception as e:
                log.warning("Failed to load tracker: %s", e)
                self._signals = []
                self._next_id = 1
        else:
            self._signals = []
            self._next_id = 1

    def _save(self):
        import os
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump({'next_id': self._next_id, 'signals': self._signals},
                      f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(self.path)
        self._upload_to_drive()

    def _init_drive(self):
        """Initialize Google Drive sync (best-effort, never blocks)."""
        try:
            if not _CONFIG_PATH.exists():
                return
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
            drive_cfg = cfg.get('google_drive', {})
            if not drive_cfg.get('enabled', False):
                return

            import platform
            if platform.system() == 'Windows':
                creds = drive_cfg.get('credentials_path_windows')
            else:
                creds = drive_cfg.get('credentials_path_linux')
            import os
            env_creds = os.environ.get('MAGNET_GOOGLE_CREDS')
            if env_creds:
                creds = env_creds
            if not creds or not Path(creds).exists():
                return

            from bcs.drive_store import get_drive_service, find_file
            self._drive_service = get_drive_service(Path(creds))
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('confidence_file_name', 'confidence_tracker.json')
            self._drive_file_id = find_file(self._drive_service, folder_id, file_name)
            self._drive_enabled = True
            log.info("CT Drive sync enabled, file_id=%s", self._drive_file_id)
        except Exception as e:
            log.warning("CT Drive init failed: %s. Local-only.", e)

    def _upload_to_drive(self):
        """Upload tracker data to Drive (best-effort)."""
        if not self._drive_enabled:
            return
        try:
            from bcs.drive_store import upload_json
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
            drive_cfg = cfg.get('google_drive', {})
            folder_id = drive_cfg['folder_id']
            file_name = drive_cfg.get('confidence_file_name', 'confidence_tracker.json')
            data = {'next_id': self._next_id, 'signals': self._signals}
            self._drive_file_id = upload_json(
                self._drive_service, folder_id, file_name,
                data, self._drive_file_id)
        except Exception as e:
            log.warning("CT Drive upload failed: %s", e)

    def add(self, symbol: str, timeframe: str, target_st: float,
            direction: str, option_symbol: str, option_price: float,
            quantity: int, sl_spot: float, signal_price: float,
            score: int = 0, grade: str = '', sq: int = 0, et: int = 0,
            target_label: str = '', option_expiry: str = '',
            tight_sl_spot: Optional[float] = None,
            synthetic_info: Optional[dict] = None) -> dict:
        """Add a new signal to track. Deduplicates by symbol+direction."""
        # Dedup: skip if watching/entered signal already exists for same stock
        existing = [s for s in self._signals
                    if s['symbol'] == symbol and s['direction'] == direction
                    and s['status'] in ('watching', 'ready', 'entered')]
        if existing:
            log.warning("CT add: %s %s already tracked (#%d), skipping",
                        symbol, direction, existing[0]['id'])
            return existing[0]

        sig = {
            'id': self._next_id,
            'symbol': symbol,
            'timeframe': timeframe,
            'target_st': target_st,
            'target_label': target_label or ('resistance' if direction == 'CE' else 'support'),
            'direction': direction,
            'option_symbol': option_symbol,
            'option_price': option_price,
            'option_expiry': option_expiry,  # YYYY-MM-DD for expiry check
            'quantity': quantity,
            'sl_spot': sl_spot,
            'tight_sl_spot': tight_sl_spot,
            'synthetic_info': synthetic_info or {},
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
            'last_15m_st': None,
            'last_15m_dir': None,
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
        return [s for s in self._signals if s['status'] in ('watching', 'ready')]

    def get_entered(self) -> list:
        return [s for s in self._signals if s['status'] == 'entered']

    def mark_entered(self, sig_id: int, entry_price: float,
                     entry_option_price: float):
        """Mark signal as entered. Guards: must be watching/ready status."""
        sig = self._find(sig_id)
        if not sig:
            return
        if sig['status'] not in ('watching', 'ready'):
            log.error("mark_entered #%s: status '%s' not watching — BLOCKED",
                      sig_id, sig['status'])
            return
        if not entry_price or entry_price <= 0:
            log.error("mark_entered #%s: invalid spot price %s — BLOCKED", sig_id, entry_price)
            return
        sig['status'] = 'entered'
        sig['entry_price'] = entry_price
        sig['entry_option_price'] = entry_option_price
        sig['entry_at'] = datetime.now().isoformat(timespec='seconds')
        self._save()

    def mark_exited(self, sig_id: int, reason: str,
                    exit_spot: float = 0, exit_option_price: float = 0):
        """Mark signal as exited with P&L. Guards: must be entered status."""
        sig = self._find(sig_id)
        if not sig:
            return
        if sig['status'] != 'entered':
            log.error("mark_exited #%s: status '%s' not entered — BLOCKED",
                      sig_id, sig['status'])
            return
        sig['status'] = 'exited'
        sig['exit_reason'] = reason
        sig['exit_at'] = datetime.now().isoformat(timespec='seconds')
        sig['exit_spot'] = exit_spot
        sig['exit_option_price'] = exit_option_price
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


_tracker_instance: ConfidenceTracker = None


def get_tracker() -> ConfidenceTracker:
    """Get or create singleton ConfidenceTracker."""
    global _tracker_instance
    if _tracker_instance is None:
        _tracker_instance = ConfidenceTracker()
    return _tracker_instance


def cleanup_old_signals(tracker: ConfidenceTracker, before_date: str = '2026-04-11'):
    """One-time cleanup: cancel watching/ready signals from before the v2 rewrite.

    Exited/cancelled signals are kept (trading journal).
    Only removes stale watching/ready signals that will never fire
    under the new 15M ST logic.

    Args:
        before_date: Cancel watching signals created before this date (YYYY-MM-DD).
    """
    cleaned = 0
    for sig in list(tracker._signals):
        if sig['status'] not in ('watching', 'ready'):
            continue
        signal_at = sig.get('signal_at', '')
        if signal_at and signal_at[:10] < before_date:
            sig['status'] = 'cancelled'
            sig['exit_reason'] = f'cleanup: pre-v2 signal (created {signal_at[:10]})'
            sig['exit_at'] = datetime.now().isoformat(timespec='seconds')
            cleaned += 1
            log.info("Cleanup: cancelled #%s %s (created %s)",
                     sig['id'], sig['symbol'], signal_at[:10])
    if cleaned > 0:
        tracker._save()
        print(f"  Cleanup: cancelled {cleaned} stale pre-v2 signals")
    return cleaned


# ---------------------------------------------------------------------------
#  Option LTP helper
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


# ---------------------------------------------------------------------------
#  15M data cache — aligned to 15M candle boundaries
# ---------------------------------------------------------------------------

_15m_cache: dict = {}  # {option_symbol: {'boundary': str, 'candles': [...]}}
_BOUNDARY_WAIT_SEC = 5  # wait 5s past boundary for candle to finalize


def _current_15m_boundary() -> str:
    """Return the last 15M candle boundary as 'HH:MM' string.

    Boundaries: :00, :15, :30, :45. Only valid after +5 seconds
    (candle needs time to finalize on exchange side).
    """
    now = datetime.now()
    # If we're in the first 5 seconds past a boundary, the candle just closed
    # but may not be finalized — use the PREVIOUS boundary
    minute = now.minute
    second = now.second
    boundary_min = (minute // 15) * 15
    if minute % 15 == 0 and second < _BOUNDARY_WAIT_SEC:
        # Too early — candle hasn't finalized, use previous boundary
        if boundary_min == 0:
            return f"{(now.hour - 1) % 24:02d}:45"
        return f"{now.hour:02d}:{boundary_min - 15:02d}"
    return f"{now.hour:02d}:{boundary_min:02d}"


def _seconds_to_next_15m_boundary() -> float:
    """Seconds until 5s past the next 15M candle close."""
    now = datetime.now()
    next_min = ((now.minute // 15) + 1) * 15
    if next_min >= 60:
        target = now.replace(hour=(now.hour + 1) % 24, minute=0,
                             second=_BOUNDARY_WAIT_SEC, microsecond=0)
        if now.hour == 23:
            target = target + timedelta(days=1)
            target = target.replace(hour=0)
    else:
        target = now.replace(minute=next_min, second=_BOUNDARY_WAIT_SEC,
                             microsecond=0)
    delta = (target - now).total_seconds()
    return max(delta, 0)


def _get_option_15m_cached(kite, option_symbol: str) -> list:
    """Fetch 15M candles, cached until next 15M boundary.

    Cache invalidates when a new 15M candle completes (boundary changes).
    Empty/failed responses are NOT cached (prevents cache poisoning).
    """
    boundary = _current_15m_boundary()
    cached = _15m_cache.get(option_symbol)
    if cached and cached.get('boundary') == boundary:
        return cached['candles']

    candles = _fetch_option_15m(kite, option_symbol)
    # Only cache valid responses (prevents poisoning from API failures)
    if candles and len(candles) >= _MIN_15M_CANDLES:
        _15m_cache[option_symbol] = {'boundary': boundary, 'candles': candles}
    return candles


# ---------------------------------------------------------------------------
#  Monitor functions
# ---------------------------------------------------------------------------

_cleanup_done = False


def monitor_once(kite, tracker: ConfidenceTracker, dry_run: bool = False):
    """Single monitoring pass using 15M ST on option chart.

    Watching signals: check if option price near 15M support → auto-enter.
    Entered signals: check if 15M support broken → EXIT alert.

    First call runs one-time cleanup of pre-v2 stale signals.
    Also checks: spot SL, stale signals, time SL, EOD daily, spot TP.
    """
    global _cleanup_done
    if not _cleanup_done:
        cleanup_old_signals(tracker)
        _cleanup_done = True

    watching = tracker.get_watching()
    entered = tracker.get_entered()

    if not watching and not entered:
        log.info("CT monitor_once: no active signals")
        print("  No active signals to monitor.")
        return

    log.info("CT monitor_once: %d watching, %d entered", len(watching), len(entered))
    print(f"\n  [ctrack] {datetime.now().strftime('%H:%M:%S')} -- "
          f"{len(watching)} watching, {len(entered)} entered")

    # --- Check watching signals ---
    dirty = False
    for sig in watching:
        sym = sig['symbol']
        opt_sym = sig.get('option_symbol', '')
        try:
            # 1. Fetch spot LTP
            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data.get(f'NSE:{sym}', {}).get('last_price')
            if ltp is None:
                print(f"  #{sig['id']} {sym}: SKIP -- no spot LTP")
                continue

            # 2. Check spot SL (invalidation before entry)
            need_up = sig['direction'] == 'CE'
            sl = sig.get('sl_spot')
            if sl:
                sl_hit = (need_up and ltp <= sl) or (not need_up and ltp >= sl)
                if sl_hit:
                    tracker.cancel(sig['id'],
                                   f'SL breached while watching (spot {ltp:,.0f})')
                    msg = format_exit_alert(sym, sig['direction'],
                                            sig['signal_price'], ltp,
                                            f'SL hit at {sl:,.0f} before entry')
                    _send_telegram(msg, dry_run=dry_run)
                    print(f"  #{sig['id']} {sym}: SL HIT while watching -> cancelled")
                    continue

            # 2b. Spot divergence guard: cancel if spot moved far from target ST
            # E.g. ANGELONE CE with target_spot=208 but spot=295 (42% above) — thesis dead
            target = sig.get('target_spot', 0)
            if target > 0:
                spot_gap = (ltp - target) / target
                diverged = ((need_up and spot_gap > _SPOT_DIVERGE_PCT) or
                            (not need_up and spot_gap < -_SPOT_DIVERGE_PCT))
                if diverged:
                    tracker.cancel(sig['id'],
                                   f'spot diverged: {ltp:,.0f} is '
                                   f'{spot_gap*100:+.1f}% from ST {target:,.0f}')
                    _send_telegram(
                        f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                        f" | spot {spot_gap*100:+.1f}% past target ST",
                        dry_run=dry_run)
                    print(f"  #{sig['id']} {sym}: SPOT DIVERGED "
                          f"({spot_gap*100:+.1f}% from ST {target:,.0f}) -> cancelled")
                    continue

            # 3. Check stale signal auto-cancel
            signal_at = sig.get('signal_at', '')
            if signal_at:
                try:
                    sig_dt = datetime.fromisoformat(signal_at)
                    days_waiting = (datetime.now() - sig_dt).total_seconds() / 86400
                    tf = sig.get('timeframe', 'weekly')
                    max_days = {'daily': 1, 'weekly': 5, 'monthly': 20}.get(tf, 5)
                    if days_waiting > max_days:
                        tracker.cancel(sig['id'],
                                       f'stale: watching {days_waiting:.0f}d (max {max_days}d)')
                        _send_telegram(
                            f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                            f" | stale ({days_waiting:.0f}d)",
                            dry_run=dry_run)
                        print(f"  #{sig['id']} {sym}: AUTO-CANCELLED (stale {days_waiting:.0f}d)")
                        continue
                except Exception:
                    pass

            # 4. Fetch 15M candles for option + compute ST
            if not opt_sym:
                print(f"  #{sig['id']} {sym}: SKIP -- no option symbol")
                continue

            candles = _get_option_15m_cached(kite, opt_sym)
            st_info = compute_option_15m_st(candles)

            if not st_info:
                print(f"  #{sig['id']} {sym}: SKIP -- insufficient 15M data "
                      f"({len(candles)} candles)")
                continue

            st_val = st_info['st_value']
            st_dir = st_info['direction']

            # Update signal with latest 15M ST (batched save at end)
            sig['last_15m_st'] = st_val
            sig['last_15m_dir'] = st_dir
            dirty = True

            # 5. Check option validity (LTP > 0)
            opt_ltp = _fetch_option_ltp(kite, opt_sym)
            if opt_ltp <= 0:
                tracker.cancel(sig['id'],
                               f'option {opt_sym} expired/no LTP')
                _send_telegram(
                    f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                    f" | option expired",
                    dry_run=dry_run)
                print(f"  #{sig['id']} {sym}: option expired, cancelled")
                continue

            # 5a. Min option price guard: skip sub-Rs 5 options (unreliable 15M data)
            if opt_ltp < _MIN_OPTION_PRICE:
                log.info("CT #%s %s: option at Rs %.2f < Rs %.0f min, skipping",
                         sig['id'], sym, opt_ltp, _MIN_OPTION_PRICE)
                print(f"  #{sig['id']} {sym}: option Rs {opt_ltp:.2f} "
                      f"< Rs {_MIN_OPTION_PRICE:.0f} min, skipping")
                continue

            # 6. THETA DECAY guard: cancel if option lost >40% from initial price
            initial_price = sig.get('option_price', 0)
            if initial_price > 0 and opt_ltp < initial_price * (1 - _THETA_DECAY_PCT):
                decay_pct = (initial_price - opt_ltp) / initial_price * 100
                tracker.cancel(sig['id'],
                               f'theta decay: option lost {decay_pct:.0f}% '
                               f'({initial_price:.2f}->{opt_ltp:.2f})')
                _send_telegram(
                    f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                    f" | theta decay -{decay_pct:.0f}%"
                    f" ({initial_price:.2f}\u2192{opt_ltp:.2f})",
                    dry_run=dry_run)
                print(f"  #{sig['id']} {sym}: THETA DECAY ({decay_pct:.0f}%) -> cancelled")
                continue

            # 7. GAP-DOWN guard: cancel if option fell >3% below 15M support
            gap_to_st = (opt_ltp - st_val) / st_val if st_val > 0 else 999
            if st_dir == 'UP' and gap_to_st < _GAP_DOWN_PCT:
                tracker.cancel(sig['id'],
                               f'option gapped below 15M support ({gap_to_st*100:.1f}%)')
                _send_telegram(
                    f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                    f" | option {gap_to_st*100:.1f}% below support",
                    dry_run=dry_run)
                print(f"  #{sig['id']} {sym}: GAP DOWN below support -> cancelled")
                continue

            # 8. 15M DOWN guard: cancel if trend flipped bearish
            if st_dir == 'DOWN':
                log.info("CT #%s %s: 15M trend DOWN, skipping entry", sig['id'], sym)
                # Track consecutive DOWN polls — cancel after 2
                down_count = sig.get('_down_count', 0) + 1
                sig['_down_count'] = down_count
                dirty = True
                if down_count >= 2:
                    tracker.cancel(sig['id'],
                                   f'15M trend DOWN for {down_count} polls, support gone')
                    _send_telegram(
                        f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                        f" | 15M trend \u2193 (support lost)",
                        dry_run=dry_run)
                    print(f"  #{sig['id']} {sym}: 15M DOWN x{down_count} -> cancelled")
                else:
                    print(f"  #{sig['id']} {sym} {sig['direction']:<3} "
                          f"opt={opt_ltp:.2f} 15M DOWN (watching, {down_count}/2)")
                continue
            else:
                sig['_down_count'] = 0  # reset on UP

            # 8a. Approaching alert: heads-up when gap <= 5% (one-shot, reset at 10%)
            if gap_to_st <= _APPROACH_GAP_PCT and not sig.get('_approaching_alerted'):
                sig['_approaching_alerted'] = True
                dirty = True
                gap_pct_display = gap_to_st * 100
                _send_telegram(
                    f"\U0001f7e1 <b>APPROACHING</b> <code>{sym}</code> "
                    f"{sig['direction']}\n"
                    f"Option {opt_ltp:.2f} | 15M Support {st_val:.2f} "
                    f"({gap_pct_display:+.1f}%)\n"
                    f"<code>{opt_sym}</code> | Entry zone at "
                    f"{_ENTRY_GAP_PCT*100:.0f}%",
                    dry_run=dry_run, channel='watching')
                log.info("CT #%s %s: APPROACHING alert sent (gap=%.1f%%)",
                         sig['id'], sym, gap_pct_display)
                print(f"  #{sig['id']} {sym}: APPROACHING 15M support "
                      f"(gap={gap_pct_display:+.1f}%) -> alert sent")

            # Reset approaching flag if gap widens back above 10%
            if gap_to_st > _APPROACH_RESET_PCT and sig.get('_approaching_alerted'):
                sig['_approaching_alerted'] = False
                dirty = True
                log.info("CT #%s %s: approaching flag RESET (gap=%.1f%%)",
                         sig['id'], sym, gap_to_st * 100)

            # 9. Entry logic: option price near 15M support (UP only)
            #    Use max(absolute floor, percentage) so cheap options get enough room
            abs_gap = opt_ltp - st_val
            entry_threshold = max(_ENTRY_GAP_ABS, st_val * _ENTRY_GAP_PCT)
            near_st = (0 <= abs_gap <= entry_threshold)

            # 9a. Wick touch: last COMPLETED 15M candle's LOW entered threshold
            #     zone, candle CLOSED above ST (bounce confirmed), LTP within 5%.
            #     candles[-1] is the running candle; candles[-2] is last completed.
            entry_via_wick = False
            if not near_st and candles and len(candles) >= 2:
                completed = candles[-2]
                # Guard: skip wick check on stale candles (not from today)
                candle_dt = completed.get('date')
                is_today = (not candle_dt
                            or not hasattr(candle_dt, 'date')
                            or candle_dt.date() >= datetime.now().date())
                if is_today:
                    wick_low = completed['low']
                    wick_close = completed['close']
                    wick_gap = wick_low - st_val
                    ltp_gap_pct = ((opt_ltp - st_val) / st_val
                                   if st_val > 0 else 999)
                    if (0 <= wick_gap <= entry_threshold
                            and wick_close > st_val
                            and 0 < ltp_gap_pct <= _WICK_LTP_MAX_PCT):
                        near_st = True
                        entry_via_wick = True
                        log.info("CT #%s %s: WICK TOUCH on completed 15M "
                                 "candle (low=%.2f close=%.2f ST=%.2f "
                                 "LTP=%.2f)",
                                 sig['id'], sym, wick_low, wick_close,
                                 st_val, opt_ltp)

            entry_method = ('WICK_ENTER' if entry_via_wick
                            else ('ENTER' if near_st else 'waiting'))
            log.info("CT #%s %s: opt=%s LTP=%.2f 15M_Sup=%.2f dir=%s gap=%.1f%% %s",
                     sig['id'], sym, opt_sym, opt_ltp, st_val, st_dir,
                     gap_to_st * 100, entry_method)
            print(f"  #{sig['id']} {sym} {sig['direction']:<3} "
                  f"opt={opt_ltp:.2f} 15M_Sup={st_val:.2f} {st_dir} "
                  f"gap={gap_to_st*100:+.1f}% "
                  f"{'-> ' + entry_method if near_st else '(watching)'}")

            if near_st:
                # 10. DTE check before auto-entry
                opt_expiry = sig.get('option_expiry', '')
                if opt_expiry:
                    try:
                        exp_date = datetime.strptime(str(opt_expiry)[:10], '%Y-%m-%d')
                        dte = (exp_date - datetime.now()).days
                        if dte < _MIN_DTE_AT_ENTRY:
                            tracker.cancel(sig['id'],
                                           f'DTE too low ({dte}d, min {_MIN_DTE_AT_ENTRY}d)')
                            _send_telegram(
                                f"\u274c <code>{sym}</code> {sig['direction']} CANCELLED"
                                f" | DTE {dte}d (min {_MIN_DTE_AT_ENTRY}d)",
                                dry_run=dry_run)
                            print(f"  #{sig['id']} {sym}: DTE {dte}d too low -> cancelled")
                            continue
                    except (ValueError, TypeError):
                        pass

                # Auto-enter: watching → entered directly
                tracker.mark_entered(sig['id'], ltp, opt_ltp)

                wick_tag = ' (wick confirmed)' if entry_via_wick else ''
                msg = format_enter_alert(
                    sym, sig['direction'], sig.get('timeframe', ''),
                    opt_sym, opt_ltp, st_val,
                    sig['quantity'], sig.get('sl_spot', 0),
                    score=sig.get('score', 0),
                    via_wick=entry_via_wick,
                    signal_price=sig.get('signal_price'),
                    signal_at=sig.get('signal_at'),
                    target_spot=sig.get('target_st'),
                    spot_now=ltp,
                    tight_sl_spot=sig.get('tight_sl_spot'),
                    synth=sig.get('synthetic_info'))
                _send_telegram(msg, dry_run=dry_run, channel='trade')
                log.info("CT #%s %s: AUTO-ENTERED%s spot=%.0f opt=%.2f",
                         sig['id'], sym, wick_tag, ltp, opt_ltp)
                print(f"  -> AUTO-ENTERED #{sig['id']} {sym} "
                      f"(spot={ltp:,.0f}, opt={opt_ltp:.2f}){wick_tag}")

        except Exception as e:
            print(f"  #{sig['id']} {sym}: ERROR -- {e}")
            log.exception("Error checking watching signal #%s %s", sig['id'], sym)

    # Batch save 15M ST updates (avoids N saves per cycle)
    if dirty:
        tracker._save()

    # --- Check entered signals ---
    now_dt = datetime.now()
    today_str = now_dt.strftime('%Y-%m-%d')
    for sig in entered:
        sym = sig['symbol']
        opt_sym = sig.get('option_symbol', '')
        try:
            # Option expiry check (CRITICAL — prevents stuck entered signals)
            opt_expiry = sig.get('option_expiry', '')
            if opt_expiry and str(opt_expiry)[:10] < today_str:
                entry_opt = sig.get('entry_option_price') or 0
                qty = sig.get('quantity') or 0
                pnl = -entry_opt * qty if entry_opt > 0 else 0
                tracker.mark_exited(sig['id'], 'option_expired',
                                    exit_option_price=0)
                _send_telegram(
                    f"\u23f0 <b>EXPIRED</b> <code>{sym}</code> {sig['direction']}\n"
                    f"Option <code>{opt_sym}</code> expired {opt_expiry}\n"
                    f"<b>Rs {pnl:+,.0f}</b> (total loss)",
                    dry_run=dry_run, channel='trade')
                print(f"  #{sig['id']} {sym}: OPTION EXPIRED {opt_expiry}")
                continue

            # Fetch spot LTP
            ltp_data = kite.ltp([f'NSE:{sym}'])
            ltp = ltp_data.get(f'NSE:{sym}', {}).get('last_price')
            if ltp is None:
                print(f"  #{sig['id']} {sym}: SKIP -- no spot LTP")
                continue

            entry_price = sig.get('entry_price') or sig['signal_price']
            need_up = sig['direction'] == 'CE'

            if not entry_price:
                print(f"  #{sig['id']} {sym}: SKIP -- no entry price")
                continue

            # --- EOD exit for daily timeframe trades (15:15) ---
            if sig.get('timeframe') == 'daily' and now_dt.hour >= 15 and now_dt.minute >= 15:
                opt_ltp = _fetch_option_ltp(kite, opt_sym)
                entry_opt = sig.get('entry_option_price') or 0
                msg = format_exit_alert(
                    sym, sig['direction'], entry_price, ltp,
                    'EOD exit -- daily trade',
                    option_ltp=opt_ltp, entry_option_price=entry_opt or None)
                _send_telegram(msg, dry_run=dry_run, channel='trade')
                tracker.mark_exited(sig['id'], 'eod_daily',
                                    exit_spot=ltp, exit_option_price=opt_ltp)
                print(f"  #{sig['id']} {sym}: EOD daily exit")
                continue

            # --- Time SL (5 trading days max hold) ---
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
                        opt_ltp = _fetch_option_ltp(kite, opt_sym)
                        entry_opt = sig.get('entry_option_price') or 0
                        msg = format_exit_alert(
                            sym, sig['direction'], entry_price, ltp,
                            f'Time SL -- {biz_days} trading days',
                            option_ltp=opt_ltp, entry_option_price=entry_opt or None)
                        _send_telegram(msg, dry_run=dry_run, channel='trade')
                        tracker.mark_exited(sig['id'], f'time_sl_{biz_days}d',
                                            exit_spot=ltp, exit_option_price=opt_ltp)
                        print(f"  #{sig['id']} {sym}: TIME SL ({biz_days}d)")
                        continue
                except Exception:
                    pass

            # --- TP: spot touches higher-TF ST target ---
            target_st = sig.get('target_st', 0)
            if target_st:
                tp_hit = False
                if need_up and ltp >= target_st:
                    tp_hit = True
                elif not need_up and ltp <= target_st:
                    tp_hit = True

                if tp_hit:
                    opt_ltp = _fetch_option_ltp(kite, opt_sym)
                    entry_opt = sig.get('entry_option_price') or 0
                    msg = format_exit_alert(
                        sym, sig['direction'], entry_price, ltp,
                        f'TP -- spot reached potential {target_st:,.0f}',
                        option_ltp=opt_ltp, entry_option_price=entry_opt or None)
                    _send_telegram(msg, dry_run=dry_run, channel='trade')
                    tracker.mark_exited(sig['id'], f'tp_spot_touched_st',
                                        exit_spot=ltp, exit_option_price=opt_ltp)
                    print(f"  #{sig['id']} {sym}: TP -- spot touched ST {target_st:,.0f}")
                    continue

            # --- 15M ST break check on option ---
            if opt_sym:
                candles = _get_option_15m_cached(kite, opt_sym)
                break_info = _check_15m_st_break(candles)

                if break_info and break_info.get('broken'):
                    opt_ltp = _fetch_option_ltp(kite, opt_sym)
                    entry_opt = sig.get('entry_option_price') or 0

                    # Compute days held
                    days_held = 0
                    if entry_at:
                        try:
                            entry_dt = datetime.fromisoformat(entry_at)
                            days_held = (now_dt - entry_dt).days
                        except Exception:
                            pass

                    msg = format_exit_15m_st(
                        sym, sig['direction'],
                        entry_opt, opt_ltp,
                        break_info['st_value'], break_info['candle_close'],
                        sig.get('quantity', 0), days_held)
                    _send_telegram(msg, dry_run=dry_run, channel='trade')
                    tracker.mark_exited(sig['id'], '15m_st_break',
                                        exit_spot=ltp, exit_option_price=opt_ltp)
                    print(f"  #{sig['id']} {sym}: 15M ST BROKEN -- exit")
                    continue

                # Compute current ST for status display
                st_info = compute_option_15m_st(candles)
                if st_info:
                    st_val = st_info['st_value']
                    st_dir = st_info['direction']
                    opt_ltp = _fetch_option_ltp(kite, opt_sym)
                    gap = (opt_ltp - st_val) / st_val * 100 if st_val > 0 else 0

                    move_pct = ((ltp - entry_price) / entry_price * 100
                                if need_up
                                else (entry_price - ltp) / entry_price * 100)

                    print(f"  #{sig['id']} {sym} {sig['direction']:<3} "
                          f"spot={ltp:,.0f}({move_pct:+.1f}%) "
                          f"opt={opt_ltp:.2f} 15M_Sup={st_val:.2f} {st_dir} "
                          f"gap={gap:+.1f}% -> holding")
                else:
                    print(f"  #{sig['id']} {sym}: holding (no 15M ST data)")

        except Exception as e:
            print(f"  #{sig['id']} {sym}: ERROR -- {e}")
            log.exception("Error checking entered signal #%s %s", sig['id'], sym)


def monitor_loop(kite, tracker: ConfidenceTracker, dry_run: bool = False):
    """Long-running poll loop aligned to 15M candle boundaries.

    Polls at :00:05, :15:05, :30:05, :45:05 of each hour — right after
    each 15M candle closes. This ensures we always work with complete
    candle data for accurate ST computation.

    During market hours (9:15-15:30), polls every 15M boundary.
    """
    WAIT_SLEEP = 30
    WAIT_LOG_INTERVAL = 300

    print("  Confidence tracker started (15M boundary-aligned). Ctrl+C to stop.")

    last_wait_msg = 0

    while True:
        now = datetime.now()
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

        # Sleep until next 15M boundary + 5 seconds
        sleep_secs = _seconds_to_next_15m_boundary()
        next_boundary = datetime.now() + timedelta(seconds=sleep_secs)
        print(f"  Next poll at {next_boundary.strftime('%H:%M:%S')} "
              f"(in {sleep_secs/60:.1f} min, after 15M candle close)")
        try:
            time.sleep(sleep_secs)
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
          f"{'Target':<10} {'Status':<10} {'15M ST':<10} {'Entry':<8} {'P&L':>10}")
    print(f"  {'-' * 82}")
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
        st_15m = s.get('last_15m_st')
        st_dir = s.get('last_15m_dir', '')
        st_str = f"{st_15m:.2f} {st_dir}" if st_15m else '--'
        print(f"  {s.get('id', '?'):<4} {s.get('symbol', '?'):<10} "
              f"{s.get('direction', '?'):<4} {s.get('timeframe', '?'):<8} "
              f"{s.get('target_st', 0):<10,.0f} {s.get('status', '?'):<10} "
              f"{st_str:<10} {entry_str:<8} {pnl_str:>10}")
        if s.get('exit_reason'):
            print(f"  {'':4} Exit: {s['exit_reason']}")
    if wins + losses > 0:
        print(f"  {'-' * 82}")
        print(f"  {'':4} Total P&L: Rs {total_pnl:+,.0f} | "
              f"{wins}W / {losses}L | "
              f"Win rate: {wins/(wins+losses)*100:.0f}%")
    print()

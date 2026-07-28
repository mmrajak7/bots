"""
Regime Manager - market-regime entry gate with deep-capitulation override.

Validated 2020-2026 (Helper/20/fifty_hybrid_validation.py, 2026-07-23):
pause NEW entries when market internals die, but never sleep through a
capitulation bottom, and never touch exits.

Rules (all on COMPLETED sessions - no intraday lookahead):
  Signals: (a) NIFTY RSI(14) > 50   (Wilder smoothing)
           (b) market breadth > 40% (% of universe stocks above own 50DMA)
           (c) NIFTY close > its 50DMA
  PAUSE:   instantly when ALL available signals are OFF
  UNPAUSE: 2 signals ON for 7 CONSECUTIVE sessions (flicker resets streak)
  OVERRIDE: entries allowed while PAUSED if NIFTY weekly stretch <= -2.0
            (deep capitulation - the V-recovery monsters enter here)
  Exits are NEVER touched by this module.

Breadth repository (continuous, self-updating):
  data/breadth/history.json     - per-symbol trailing daily closes (working copy,
                                  gitignored; seeded from seed_history.json.gz)
  data/breadth/breadth_daily.json - date -> breadth% history
  data/regime_state.json        - state machine persistence
  Updated once per trading day INTRADAY (09:20-15:25) via batch OHLC whose
  ohlc.close = previous session's OFFICIAL close (2-3 API calls); gaps from
  downtime are backfilled per-symbol via historical API (1 attempt/day).
  The state machine advances a session only once its exact-session breadth
  reading exists (next-morning capture), matching the backtest's granularity;
  a breadth pipeline dead for >3 sessions degrades to 2-signal voting.

Telegram: alerts ONLY on state transitions (OFF -> paused, back ON -> unpaused).
"""

import gzip
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from loguru import logger

from src.telegram.bot import telegram
from src.utils.config_manager import config
from src.utils.timezone_helper import today_ist, now_ist

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BREADTH_DIR = PROJECT_ROOT / "data" / "breadth"
SEED_FILE = BREADTH_DIR / "seed_history.json.gz"
HISTORY_FILE = BREADTH_DIR / "history.json"
BREADTH_DAILY_FILE = BREADTH_DIR / "breadth_daily.json"
STATE_FILE = PROJECT_ROOT / "data" / "regime_state.json"

MAX_SESSIONS_KEPT = 120          # per-symbol trailing closes (need 50 + buffer)
BREADTH_HISTORY_KEPT = 500       # daily breadth readings kept
POST_CLOSE_TIME = "15:35"        # completed-candle boundary for today's close


class RegimeManager:
    """Singleton-ish manager owned by SignalProcessor. All state on disk."""

    def __init__(self, kite):
        self.kite = kite
        self._eval_cache_session: Optional[str] = None
        self._eval_cache_result: Optional[dict] = None
        self._history: Optional[Dict[str, Dict[str, float]]] = None
        self._bf_hold_logged: Optional[str] = None

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(config.get('regime.enabled', True))

    @property
    def deep_override(self) -> float:
        return float(config.get('regime.deep_override_stretch', -2.0))

    # ------------------------------------------------------------------
    # Breadth repository
    # ------------------------------------------------------------------
    def _load_history(self) -> Dict[str, Dict[str, float]]:
        """Load working history; initialize from committed seed on first run."""
        if self._history is not None:
            return self._history
        if HISTORY_FILE.exists():
            with open(HISTORY_FILE) as f:
                self._history = json.load(f)
            return self._history
        if SEED_FILE.exists():
            with gzip.open(SEED_FILE, 'rt', encoding='utf-8') as f:
                self._history = json.load(f)
            BREADTH_DIR.mkdir(parents=True, exist_ok=True)
            self._save_history()
            logger.info(
                f"Breadth repository initialized from seed: "
                f"{len(self._history)} symbols")
            return self._history
        logger.warning("No breadth seed/history found - breadth signal unavailable")
        self._history = {}
        return self._history

    def _save_history(self) -> None:
        BREADTH_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(self._history, f)
        tmp.replace(HISTORY_FILE)

    def _last_session_in_history(self) -> Optional[str]:
        hist = self._load_history()
        last = None
        for closes in hist.values():
            if closes:
                m = max(closes)
                if last is None or m > last:
                    last = m
        return last

    def maintenance(self) -> None:
        """Called every orchestrator cycle. Internally throttled.

        Runs during market hours (Kite's ohlc.close field = PREVIOUS session's
        OFFICIAL close only while a newer session is trading, so the batch
        capture is only valid intraday). Determines missing sessions exactly
        from NIFTY's own session calendar:
          - exactly 1 session missing -> batch-OHLC capture (2-3 API calls)
          - more than 1 missing       -> per-symbol historical backfill
            (throttled to one attempt per day via regime_state.json)
        Never raises - regime must not break the trading loop.
        """
        if not self.enabled:
            return
        try:
            hist = self._load_history()
            if not hist:
                return

            # Resume an in-flight backfill BEFORE computing what's missing.
            # Partial progress already advances the history's max session, so
            # `missing` would come back empty and strand the remaining symbols
            # half-filled.
            state = self._load_state()
            bf = state.get('backfill') or {}
            if bf and not bf.get('done') and bf.get('day') == today_ist().isoformat():
                self._run_backfill(bf['from'], bf['to'], bf.get('sessions') or [])
                return

            nifty = self._nifty_daily_signals()
            if nifty is None or not nifty.get('sessions'):
                return
            last = self._last_session_in_history()
            if last is None:
                return
            missing = [s for s in nifty['sessions'] if s > last]
            if not missing:
                return

            if len(missing) == 1:
                # Batch OHLC only: ohlc.close is the previous session's OFFICIAL
                # close *only while a newer session is trading*, so this path is
                # strictly intraday.
                if '09:20' <= now_ist().strftime('%H:%M') <= '15:25':
                    self._capture_prev_session_closes(missing[0])
                return

            # More than one session missing: only the historical API can fix it,
            # and that reads COMPLETED sessions - valid at any hour. Running it
            # outside the capture window is what lets a gap heal the same day.
            self._run_backfill(missing[0], missing[-1], missing)
        except Exception as e:
            logger.error(f"Regime maintenance error (non-fatal): {e}")

    def _capture_prev_session_closes(self, session_s: str) -> None:
        """Batch-OHLC capture of the previous session's OFFICIAL closes.

        Valid only during market hours: kite ohlc()['ohlc']['close'] is the
        close of the last COMPLETED trading day while today is trading.
        """
        hist = self._load_history()
        symbols = sorted(hist.keys())
        logger.info(f"Breadth: capturing {session_s} official closes "
                    f"for {len(symbols)} symbols")
        captured = 0
        kc = self.kite._get_read_client()
        for i in range(0, len(symbols), 400):
            chunk = [f"NSE:{s}" for s in symbols[i:i + 400]]
            try:
                self.kite._rate_limit()
                data = kc.ohlc(chunk)
            except Exception as e:
                logger.warning(f"Breadth OHLC chunk failed ({i}): {e}")
                continue
            for key, row in data.items():
                sym = key.split(':', 1)[-1]
                close = (row.get('ohlc') or {}).get('close') or 0
                if sym in hist and close and close > 0:
                    hist[sym][session_s] = float(close)
                    captured += 1
        if captured == 0:
            logger.warning("Breadth capture got 0 closes - keeping previous data")
            return
        self._trim_history()
        self._save_history()
        self._append_breadth_reading(session_s)
        logger.info(f"Breadth: captured {captured} closes for {session_s}")

    def _run_backfill(self, frm: str, to: str, sessions: List[str]) -> None:
        """Backfill missed sessions via historical API (downtime recovery).

        ~1 API call per symbol, so a full 905-symbol universe is ~9 minutes -
        far too long to run inline in the daemon's 5-minute task loop. Instead
        this is RESUMABLE and TIME-BUDGETED: each cycle spends at most
        regime.backfill_seconds_per_cycle, persists a cursor to
        regime_state.json, and picks up where it left off on the next cycle.
        A full universe therefore heals over ~10 cycles (~50 min) while never
        blocking the daemon for more than the budget.
        """
        state = self._load_state()
        today_s = today_ist().isoformat()
        bf = state.get('backfill') or {}

        # A different range (or a new day) means new work - start over.
        if (bf.get('from') != frm or bf.get('to') != to
                or bf.get('day') != today_s):
            # If the previous sweep never COMPLETED, every symbol past its
            # cursor is still missing that range's sessions. Recomputing
            # `missing` hides this: the symbols already swept advanced the
            # repository's global max session, so the older gap looks filled.
            # Widen the range back over the abandoned one and restart, else
            # those symbols keep a permanent hole and their 50DMA silently
            # spans the wrong calendar window for ~50 sessions.
            prev = bf
            if prev.get('from') and not prev.get('complete') and prev['from'] < frm:
                logger.warning(
                    f"Breadth: previous backfill {prev['from']}..{prev.get('to')} "
                    f"stopped at symbol {prev.get('cursor')} - widening range to "
                    f"{prev['from']}..{to} and restarting to avoid stranded gaps")
                frm = prev['from']
                sessions = sorted(set(list(sessions)
                                      + list(prev.get('sessions') or [])))
            bf = {'from': frm, 'to': to, 'day': today_s, 'sessions': sessions,
                  'cursor': 0, 'cycles': 0, 'filled': 0,
                  'done': False, 'complete': False}
        if bf.get('done'):
            return

        max_cycles = int(config.get('regime.backfill_max_cycles', 30))
        if bf['cycles'] >= max_cycles:
            # Give up on THIS RANGE rather than burning the budget every cycle.
            # (A genuinely new range later the same day re-arms a fresh budget,
            # which is intended - that is new work, not a retry of this one.)
            # done=True also releases the state machine's hold (see evaluate).
            bf['done'] = True
            state['backfill'] = bf
            self._save_state(state)
            logger.warning(
                f"Breadth: backfill {frm}..{to} hit {max_cycles} cycles at "
                f"{bf['cursor']} symbols - giving up for today")
            return
        bf['cycles'] += 1

        hist = self._load_history()
        symbols = sorted(hist.keys())
        cursor = int(bf.get('cursor', 0))
        budget = float(config.get('regime.backfill_seconds_per_cycle', 60))

        if cursor == 0:
            logger.info(f"Breadth: backfilling sessions {frm}..{to} "
                        f"for {len(symbols)} symbols (resumable)")

        started = time.monotonic()
        start_cursor = cursor
        filled = 0
        # Budget is checked AFTER each symbol so at least one is always
        # processed - a misconfigured (or zero) budget must never leave the
        # cursor parked forever.
        while cursor < len(symbols):
            sym = symbols[cursor]
            cursor += 1
            try:
                token = self.kite.get_instrument_token(sym)
                df = self.kite.get_historical_data(token, frm, to, 'day')
                # An EMPTY frame is NOT success: get_historical_data returns one
                # (no exception) when the historical circuit breaker is open, so
                # counting it would let a dead API sweep the remaining symbols
                # instantly and latch a COMPLETE that holds no data.
                if df is not None and not df.empty:
                    for _, r in df.iterrows():
                        ds = pd.Timestamp(r['Date']).date().isoformat()
                        hist[sym][ds] = float(r['Close'])
                    filled += 1
            except Exception:
                pass  # per-symbol failure must not abort the sweep
            if (time.monotonic() - started) >= budget:
                break

        # A cycle that fetched nothing at all means the API is down (breaker
        # open, token dead), not that those symbols have no history. Rewind so
        # the sweep resumes at the same place next cycle instead of burning
        # through the universe and declaring the range COMPLETE.
        if filled == 0 and cursor > start_cursor:
            logger.warning(
                f"Breadth: backfill fetched 0 of {cursor - start_cursor} symbols "
                f"this cycle (API down?) - rewinding cursor to {start_cursor}")
            cursor = start_cursor

        bf['filled'] = int(bf.get('filled', 0)) + filled
        complete = cursor >= len(symbols)
        bf['cursor'] = cursor
        bf['done'] = complete
        bf['complete'] = complete
        state['backfill'] = bf

        self._trim_history()
        self._save_history()
        self._save_state(state)

        logger.info(f"Breadth: backfill progress {cursor}/{len(symbols)} symbols "
                    f"(+{filled} this cycle, {bf['filled']} filled total)"
                    + (" - COMPLETE" if complete else ""))

        if complete:
            # Fill rate is the health signal: a COMPLETE carrying few actual
            # fills means the sweep ran against a dead API, not that the data
            # is genuinely absent.
            if bf['filled'] < len(symbols) * 0.5:
                logger.warning(
                    f"Breadth: backfill COMPLETE but only {bf['filled']}/"
                    f"{len(symbols)} symbols returned data - breadth readings "
                    f"may be unreliable for {frm}..{to}")
            # Record a breadth reading for every session we just filled, not
            # just the newest one.
            for ds in sorted(set(sessions)):
                self._append_breadth_reading(ds)

    def _trim_history(self) -> None:
        hist = self._load_history()
        for sym, closes in hist.items():
            if len(closes) > MAX_SESSIONS_KEPT:
                keep = sorted(closes)[-MAX_SESSIONS_KEPT:]
                hist[sym] = {d: closes[d] for d in keep}

    def _compute_breadth(self, session_s: Optional[str] = None
                         ) -> Optional[Tuple[str, float, int]]:
        """(session_date, breadth_pct, coverage) = % of universe above own 50DMA.

        `session_s` defaults to the newest session in the repository. Passing an
        explicit session lets backfilled history be scored for the session it
        actually belongs to (each symbol's 50-close window is taken as of that
        date, never using later closes).
        """
        hist = self._load_history()
        target = session_s or self._last_session_in_history()
        if not target:
            return None
        above = 0
        total = 0
        for closes in hist.values():
            if target not in closes:
                continue
            # Closes up to and including the target session only - no lookahead.
            ds = sorted(d for d in closes if d <= target)
            if len(ds) < 50:
                continue
            window = ds[-50:]  # window[-1] == target by construction
            vals = [closes[d] for d in window]
            sma = sum(vals) / 50
            total += 1
            if vals[-1] > sma:
                above += 1
        min_cov = int(config.get('regime.breadth_min_coverage', 300))
        if total < min_cov:
            logger.warning(
                f"Breadth coverage too low for {target} ({total} < {min_cov})")
            return None
        return target, above / total * 100.0, total

    def _compute_breadth_for_last_session(self) -> Optional[Tuple[str, float, int]]:
        """Back-compat alias for the newest-session breadth reading."""
        return self._compute_breadth()

    def _append_breadth_reading(self, session_s: str) -> None:
        res = self._compute_breadth(session_s)
        if not res or res[0] != session_s:
            return
        readings = {}
        if BREADTH_DAILY_FILE.exists():
            with open(BREADTH_DAILY_FILE) as f:
                readings = json.load(f)
        readings[session_s] = round(res[1], 2)
        keep = sorted(readings)[-BREADTH_HISTORY_KEPT:]
        readings = {d: readings[d] for d in keep}
        tmp = BREADTH_DAILY_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(readings, f, indent=1)
        tmp.replace(BREADTH_DAILY_FILE)

    def _breadth_info(self, session_s: str) -> Tuple[Optional[float], Optional[str]]:
        """Latest breadth reading at or before session_s -> (value, reading_date).

        (None, None) = breadth system has no usable reading.
        """
        if BREADTH_DAILY_FILE.exists():
            with open(BREADTH_DAILY_FILE) as f:
                readings = json.load(f)
            candidates = [d for d in readings if d <= session_s]
            if candidates:
                d = max(candidates)
                return float(readings[d]), d
            return None, None
        res = self._compute_breadth_for_last_session()
        if res and res[0] <= session_s:
            return res[1], res[0]
        return None, None

    # ------------------------------------------------------------------
    # NIFTY daily signals (completed sessions only)
    # ------------------------------------------------------------------
    def _nifty_daily_signals(self) -> Optional[dict]:
        """RSI(14) Wilder + close-vs-50DMA on the last COMPLETED NIFTY session."""
        nifty_token = config.get('nifty_filter.instrument_token', '256265')
        df = self.kite.get_historical_data_sampled(
            instrument_token=nifty_token, timeframe='daily', years_back=1.0)
        if df is None or df.empty or len(df) < 60:
            return None
        df = df.copy()
        df['D'] = pd.to_datetime(df['Date']).dt.date
        # Completed session: drop today's running candle during market hours
        now = now_ist()
        intraday = now.strftime('%H:%M') < POST_CLOSE_TIME
        if intraday and df['D'].iloc[-1] == today_ist():
            df = df.iloc[:-1]
        if len(df) < 60:
            return None
        closes = df['Close'].astype(float)
        delta = closes.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rs = gain / loss.replace(0, float('nan'))
        rsi = float((100 - 100 / (1 + rs)).iloc[-1])
        sma_period = int(config.get('regime.sma_period', 50))
        sma = float(closes.rolling(sma_period).mean().iloc[-1])
        close = float(closes.iloc[-1])
        return {
            'session': df['D'].iloc[-1].isoformat(),
            'rsi': rsi, 'close': close, 'sma50': sma,
            # Completed-session calendar (last 30) - used by breadth maintenance
            # to detect exactly which sessions are missing from the repository.
            'sessions': [d.isoformat() for d in df['D'].iloc[-30:]],
        }

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------
    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    return json.load(f)
            except Exception:
                logger.warning("Corrupt regime_state.json - reinitializing")
        return {'state': 'UNPAUSED', 'streak': 0, 'since': None,
                'last_session': None, 'transitions': []}

    def _save_state(self, state: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix('.tmp')
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=1)
        tmp.replace(STATE_FILE)

    def evaluate(self) -> dict:
        """Update the state machine for the latest completed session.

        Idempotent per session (streak counted once per session). Cached
        in-process per session. Fail-safe: on any data failure, keeps the
        previous persisted state (never fabricates a transition).
        Returns {'state', 'n_on', 'session', ...}.
        """
        if not self.enabled:
            return {'state': 'UNPAUSED', 'n_on': None, 'session': None,
                    'disabled': True}
        state = self._load_state()

        # While a backfill is mid-flight the repository is only partially
        # updated - just the symbols swept so far carry the newest session - so
        # any breadth reading would be computed off a biased subset. Hold the
        # machine until the sweep finishes; the gate meanwhile answers from the
        # persisted state, which fails open.
        bf = state.get('backfill') or {}
        if bf and not bf.get('done') and bf.get('day') == today_ist().isoformat():
            marker = f"{bf.get('day')}:{bf.get('cursor')}"
            if self._bf_hold_logged != marker:
                logger.info("Regime: breadth backfill in progress "
                            f"({bf.get('cursor')} symbols done) - holding state machine")
                self._bf_hold_logged = marker
            return {'state': state['state'], 'n_on': None,
                    'session': state.get('last_session'),
                    'streak': state.get('streak', 0), 'backfill': True}

        try:
            nifty = self._nifty_daily_signals()
            if nifty is None:
                logger.warning("Regime: NIFTY daily data unavailable - keeping "
                               f"previous state {state['state']}")
                return {'state': state['state'], 'n_on': None, 'session': None,
                        'stale': True}
            session = nifty['session']
            if self._eval_cache_session == session and self._eval_cache_result:
                return self._eval_cache_result

            rsi_thr = float(config.get('regime.rsi_threshold', 50))
            br_thr = float(config.get('regime.breadth_threshold', 40))
            breadth, breadth_date = self._breadth_info(session)

            # Breadth for session S is captured the NEXT morning (batch OHLC).
            # To match the validated backtest (same-session breadth), WAIT for
            # the exact reading before advancing the machine - but only while
            # the breadth pipeline is alive (reading within the last 3
            # sessions). If it's older, treat breadth as unavailable and
            # advance on 2-signal voting so a dead pipeline can't freeze us.
            if breadth is not None and breadth_date != session:
                recent = nifty['sessions'][-3:]
                if recent and breadth_date >= recent[0]:
                    if state.get('last_session') != session:
                        # INFO, not DEBUG: this is the one state in which the
                        # machine deliberately stops advancing. At DEBUG it hid
                        # a 3-session freeze (breadth capture was never wired
                        # into the daemon) with zero operator-visible signal.
                        logger.info(
                            f"Regime: waiting for {session} breadth "
                            f"(latest {breadth_date}) before advancing")
                        return {'state': state['state'], 'n_on': None,
                                'session': session, 'pending_breadth': True,
                                'streak': state.get('streak', 0)}
                    # session already advanced earlier - fall through, display only
                else:
                    logger.warning(
                        f"Regime: breadth stale ({breadth_date} < {recent[0] if recent else '?'}) "
                        "- advancing on 2-signal voting")
                    breadth = None

            sig = {
                'rsi_ok': nifty['rsi'] > rsi_thr,
                'breadth_ok': (breadth > br_thr) if breadth is not None else None,
                'sma_ok': nifty['close'] > nifty['sma50'],
            }
            avail = [v for v in sig.values() if v is not None]
            n_on = sum(1 for v in avail if v)
            unpause_need = 2 if len(avail) >= 2 else 1

            prev_state = state['state']
            # Only advance the machine once per NEW session
            if state.get('last_session') != session:
                if n_on == 0:
                    state['state'] = 'PAUSED'
                    state['streak'] = 0
                elif state['state'] == 'PAUSED':
                    if n_on >= unpause_need:
                        state['streak'] = int(state.get('streak', 0)) + 1
                        if state['streak'] >= int(config.get('regime.unpause_days', 7)):
                            state['state'] = 'UNPAUSED'
                            state['streak'] = 0
                    else:
                        state['streak'] = 0
                state['last_session'] = session

                if state['state'] != prev_state:
                    state['since'] = session
                    state['transitions'] = (state.get('transitions', [])
                                            + [{'to': state['state'],
                                                'session': session,
                                                'at': now_ist().isoformat()}])[-30:]
                    self._send_transition_alert(state['state'], nifty, breadth, n_on)
                self._save_state(state)

            result = {
                'state': state['state'], 'n_on': n_on, 'session': session,
                'streak': state.get('streak', 0),
                'rsi': round(nifty['rsi'], 1),
                'breadth': round(breadth, 1) if breadth is not None else None,
                'close_vs_sma': round(
                    (nifty['close'] / nifty['sma50'] - 1) * 100, 1),
            }
            self._eval_cache_session = session
            self._eval_cache_result = result
            return result
        except Exception as e:
            logger.error(f"Regime evaluate error (non-fatal): {e}")
            return {'state': state['state'], 'n_on': None, 'session': None,
                    'stale': True}

    def _send_transition_alert(self, new_state: str, nifty: dict,
                               breadth: Optional[float], n_on: int) -> None:
        """Telegram ONLY on transitions (user requirement)."""
        br_s = f"{breadth:.0f}%" if breadth is not None else "n/a"
        vs = (nifty['close'] / nifty['sma50'] - 1) * 100
        detail = (f"RSI {nifty['rsi']:.0f} · breadth {br_s} · "
                  f"NIFTY {vs:+.1f}% vs 50DMA")
        if new_state == 'PAUSED':
            msg = ("<b>🔴 Regime OFF — new entries paused</b>\n\n"
                   f"{detail}\n\n"
                   "<i>Resumes after 2-of-3 signals ON for "
                   f"{config.get('regime.unpause_days', 7)} straight sessions. "
                   "Deep-capitulation signals (NIFTY weekly stretch ≤ "
                   f"{self.deep_override:.0f}) still come through. "
                   "Exits are unaffected.</i>")
        else:
            msg = ("<b>🟢 Regime back ON — entries resumed</b>\n\n"
                   f"{detail}")
        try:
            telegram.send_alert(msg)
        except Exception as e:
            logger.warning(f"Regime transition alert failed: {e}")

    # ------------------------------------------------------------------
    # The gate
    # ------------------------------------------------------------------
    def entry_allowed(self, weekly_stretch: Optional[float]) -> Tuple[bool, str]:
        """Entry gate. NEVER used for exits.

        Returns (allowed, reason). reason == 'override' marks a
        deep-capitulation entry during a paused regime.
        """
        if not self.enabled:
            return True, ''
        res = self.evaluate()
        if res['state'] == 'UNPAUSED':
            return True, ''
        if weekly_stretch is not None and weekly_stretch <= self.deep_override:
            logger.info(
                f"Regime PAUSED but deep-capitulation override active "
                f"(stretch {weekly_stretch:.2f} <= {self.deep_override})")
            return True, 'override'
        return False, (f"Regime PAUSED since {res.get('session') or '?'} "
                       f"(signals ON: {res.get('n_on')}, streak "
                       f"{res.get('streak', 0)}/{config.get('regime.unpause_days', 7)})")

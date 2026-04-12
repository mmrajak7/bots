"""
NIFTY Option Magnet — SuperTrend on ATM CE/PE option premiums.

Magnet concept on options:
  1. Get NIFTY spot → ATM strike → resolve monthly CE & PE
  2. Compute ST(10,3) on CE and PE option candles (5 TFs: 15M-4H)
  3. Only ST DOWN = premium is BELOW ST (ST is resistance above)
  4. WATCHING: premium within 5% below ST | ENTRY: within 3% below ST
  5. TP: premium rises to touch its ST from below → PROFIT ✓
  6. Multi-layer exits: cost SL (at 1.5%), trailing, premium SL (-35%), EOD

Why this works:
  ST DOWN on option = premium in downtrend, ST is ceiling above
  Premium starts rising toward ceiling = potential reversal
  Buy cheap (below ST), sell when premium touches ST resistance
  Always profitable at TP because premium RISES from entry to ST level
"""
import pandas as pd
import numpy as np
import datetime
import pytz
import indicators
import os
import sys
import json
import time
import platform
import copy
import common
from kiteconnect import KiteConnect
import logging
import yaml
import atexit

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging():
    logger = logging.getLogger('option_buy')
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    log_dir = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'logs')
    os.makedirs(log_dir, exist_ok=True)
    today = datetime.datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d')
    fh = logging.FileHandler(os.path.join(log_dir, f'option_buy_{today}.log'), encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S'))
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', datefmt='%H:%M:%S'))
    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging()
IST = pytz.timezone('Asia/Kolkata')

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CURRENT_DIR = os.path.dirname(os.path.realpath(__file__))
TOKEN_PATH = (r'C:\Users\mail2\Documents\Projects\BOTS\data\kite_access_token.json'
              if platform.system() == 'Windows'
              else '/home/trustit/Desktop/BOTS/data/kite_access_token.json')

ALL_TIMEFRAMES = ['15minute', '30minute', '60minute']
TF_DISPLAY = {'15minute': '15M', '30minute': '30M', '60minute': '1H'}

NIFTY_SPOT_KEY = 'NSE:NIFTY 50'
NIFTY_SPOT_TOKEN = 256265
STRIKE_INTERVAL = 50

# Signal zones (premium below ST, ST DOWN only):
WATCH_GAP_PCT = 5.0        # WATCHING alert: premium within 5% below ST
ENTRY_GAP_PCT = 3.0        # ENTRY trigger: premium within 3% below ST
COST_SL_GAP_PCT = 1.5      # cost SL: premium within 1.5% of ST (halfway from 3% entry)

# Premium-based exits
PREMIUM_SL_PCT = 35         # hard stop: premium drops 35% from entry
TRAIL_PCT = 50              # trail at 50% of peak gain
TRAIL_MIN_GAIN_PCT = 15     # trail starts after 15% gain
# WARNING: Trailing is currently UNREACHABLE — TP fires at ~3% gain (ST touch)
# which is always < TRAIL_MIN_GAIN_PCT (15%). If entry/TP logic changes,
# revisit this parameter.

# ST computation
MIN_ST_CANDLES = 50
HISTORY_DAYS = 60           # Kite max for intraday intervals
MIN_DTE = 1                 # minimum 1 DTE (use weekly expiries)

# Sticky watch: keep monitoring watched strikes even after ATM shift
STICKY_WATCH_MAX_GAP = 10.0  # drop watched strike if gap exceeds this %

# Signal & trade timing: no signals or Neo trades before this time
# First 15 min (9:15-9:30) used for ST level computation only
SIGNAL_START_TIME = datetime.time(9, 30)

# ---------------------------------------------------------------------------
# Neo Bridge & Time SL Config
# ---------------------------------------------------------------------------

_NEO_BRIDGE = None
TRADE_ENABLED = False
TF_TIME_SL = {}  # populated from config_trade.yaml

def _init_bridge():
    """Initialize Neo bridge from config_trade.yaml if available."""
    global _NEO_BRIDGE, TRADE_ENABLED, TF_TIME_SL
    config_path = os.path.join(CURRENT_DIR, 'config_trade.yaml')
    if not os.path.exists(config_path):
        log.info("No config_trade.yaml — trade execution disabled")
        return

    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        TRADE_ENABLED = cfg.get('trade_enabled', False)
        TF_TIME_SL = cfg.get('time_sl', {})
        if TRADE_ENABLED:
            from neo import NeoTrader
            _NEO_BRIDGE = NeoTrader(config_path)
            log.info(f"Neo trader active: log_only={_NEO_BRIDGE.log_only}")
        else:
            # Still load TIME_SL even without trade execution
            log.info(f"Trade disabled. TIME_SL loaded: {TF_TIME_SL}")
    except Exception as e:
        log.error(f"Bridge init failed: {e}", exc_info=True)
        TRADE_ENABLED = False

# ---------------------------------------------------------------------------
# Kite Client & Instruments
# ---------------------------------------------------------------------------

_KITE = None
_INSTRUMENTS = None

def get_kite():
    global _KITE
    if _KITE is None:
        try:
            with open(TOKEN_PATH) as f:
                td = json.load(f)
            _KITE = KiteConnect(api_key=td['api_key'])
            _KITE.set_access_token(td['access_token'])
            log.info(f"Kite initialized ({td['api_key'][:4]}****)")
        except FileNotFoundError:
            log.critical(f"Token file not found: {TOKEN_PATH}"); sys.exit(1)
        except (json.JSONDecodeError, KeyError) as e:
            log.critical(f"Token file corrupt or missing keys: {e}"); sys.exit(1)
        except Exception as e:
            log.critical(f"Kite init failed: {e}"); sys.exit(1)
    return _KITE

def get_instruments():
    global _INSTRUMENTS
    if _INSTRUMENTS is None:
        _INSTRUMENTS = pd.read_csv(os.path.join(CURRENT_DIR, 'op_instruments.csv'))
        _INSTRUMENTS['expiry'] = pd.to_datetime(_INSTRUMENTS['expiry'])
    return _INSTRUMENTS

# ---------------------------------------------------------------------------
# API Helpers
# ---------------------------------------------------------------------------

def _hist(token, start, end, interval):
    kite = get_kite(); time.sleep(0.35)
    try:
        data = kite.historical_data(int(token), start, end, interval, continuous=False)
        if not data: return pd.DataFrame()
        return pd.DataFrame(data).rename(columns={
            'date':'Date','open':'Open','high':'High','low':'Low','close':'Close','volume':'Volume'})
    except Exception as e:
        log.error(f"Hist failed ({token},{interval}): {e}"); return pd.DataFrame()

def _ltp(keys):
    kite = get_kite(); time.sleep(0.35)
    try:
        r = kite.ltp(keys)
        return {k: v['last_price'] for k, v in r.items() if v.get('last_price', 0) > 0}
    except Exception as e:
        log.error(f"LTP failed: {e}"); return {}

def _quote(key):
    kite = get_kite(); time.sleep(0.35)
    try:
        q = kite.quote([key])
        if key not in q: return {'bid':0,'ask':0,'ltp':0}
        d = q[key]; depth = d.get('depth',{})
        bid = depth.get('buy',[{}])[0].get('price',0) if depth.get('buy') else 0
        ask = depth.get('sell',[{}])[0].get('price',0) if depth.get('sell') else 0
        return {'bid':bid, 'ask':ask, 'ltp':d.get('last_price',0)}
    except Exception as e:
        log.error(f"Quote failed {key}: {e}"); return {'bid':0,'ask':0,'ltp':0}

# ---------------------------------------------------------------------------
# Option Resolution
# ---------------------------------------------------------------------------

def get_atm_strike(spot):
    return round(spot / STRIKE_INTERVAL) * STRIKE_INTERVAL

def _get_instrument_info(opt_sym):
    """Look up lot_size and tick_size from instruments for a trading symbol."""
    df = get_instruments()
    m = df[df['tradingsymbol'] == opt_sym]
    if not m.empty:
        return {
            'lot_size': int(m['lot_size'].iloc[0]),
            'tick_size': float(m['tick_size'].iloc[0]),
        }
    # Fallback: any NIFTY instrument (lot_size is same across strikes)
    nifty = df[df['name'] == 'NIFTY']
    if not nifty.empty:
        return {
            'lot_size': int(nifty['lot_size'].iloc[0]),
            'tick_size': float(nifty['tick_size'].iloc[0]),
        }
    log.warning(f"Instrument info not found for {opt_sym}, using defaults")
    return {'lot_size': 75, 'tick_size': 0.05}

def get_weekly_expiry():
    """Pick nearest NIFTY expiry with DTE >= MIN_DTE (weekly or monthly)."""
    df = get_instruments()
    today = datetime.datetime.now(IST).date()
    opts = df[(df['name']=='NIFTY')&(df['exchange']=='NFO')&(df['instrument_type'].isin(['CE','PE']))]
    if opts.empty: return None
    expiries = sorted(opts['expiry'].dt.date.unique())
    for e in expiries:
        dte = (e - today).days
        if dte >= MIN_DTE:
            return e
    return None

def _resolve_opt(strike, expiry, opt_type):
    df = get_instruments()
    def find(s):
        m = df[df['tradingsymbol']==s]
        return (int(m['instrument_token'].iloc[0]), s) if not m.empty else (None,None)
    d = expiry.strftime('%d'); mn = expiry.strftime('%m')
    mc = {1:'1',2:'2',3:'3',4:'4',5:'5',6:'6',7:'7',8:'8',9:'9',10:'O',11:'N',12:'D'}
    ms = mc[int(mn)]; ma = expiry.strftime('%b').upper(); ys = expiry.strftime('%y'); si = int(strike)
    for fmt in [f"NIFTY{ys}{ms}{d}{si}{opt_type}", f"NIFTY{ys}{ma}{si}{opt_type}"]:
        t, s = find(fmt)
        if t: return t, s
    return None, None

# ---------------------------------------------------------------------------
# SuperTrend on Option Candles
# ---------------------------------------------------------------------------

def _st(df):
    if df.empty or len(df) < MIN_ST_CANDLES: return None
    r = indicators.SuperTrend(df.copy(), 10, 3)
    if 'ST' not in r.columns or r['ST'].iloc[-1] == 0: return None
    v = float(r['ST'].iloc[-1]); c = float(r['Close'].iloc[-1])
    return {'direction': 'UP' if c >= v else 'DOWN', 'value': v, 'close': c}

def compute_option_st(token):
    """Compute ST on option candles for 3 TFs (15M, 30M, 1H)."""
    now = datetime.datetime.now(IST)
    s = (now - datetime.timedelta(days=HISTORY_DAYS)).strftime('%Y-%m-%d')
    e = now.strftime('%Y-%m-%d')
    results = {}
    insuf = {'direction':'INSUFFICIENT','value':0,'close':0}

    for tf in ALL_TIMEFRAMES:
        df = _hist(token, s, e, tf)
        results[tf] = _st(df) or insuf.copy()

    return results

# ---------------------------------------------------------------------------
# JSON Helpers
# ---------------------------------------------------------------------------

class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, np.ndarray): return o.tolist()
        return super().default(o)

def _save(data, path):
    tmp = path+'.tmp'
    with open(tmp,'w') as f: json.dump(data,f,indent=2,cls=_Enc)
    os.replace(tmp, path)

def _load(path):
    if not os.path.exists(path): return {}
    try:
        with open(path) as f: return json.load(f)
    except Exception: return {}

def _levels_path():
    return os.path.join(CURRENT_DIR, f"st_opt_levels_{datetime.datetime.now(IST).strftime('%Y-%m-%d')}.json")
def _state_path(): return os.path.join(CURRENT_DIR, 'st_opt_state.json')
def _pos_path(): return os.path.join(CURRENT_DIR, 'st_opt_positions.json')

# ---------------------------------------------------------------------------
# Sticky Watch Helpers
# ---------------------------------------------------------------------------

def _record_watched_strike(state, opt_type, strike, symbol, expiry_iso, tf, st_val):
    """Add a watched strike to persistent state for cross-ATM monitoring."""
    ws = state.setdefault('watched_strikes', {})
    key = f"{strike}_{opt_type}"
    if key not in ws:
        ws[key] = {
            'strike': strike, 'opt_type': opt_type,
            'symbol': symbol, 'expiry': expiry_iso,
            'levels': {}, 'added': datetime.datetime.now(IST).isoformat()
        }
    ws[key]['levels'][tf] = st_val

def _remove_watched_strike(state, opt_type, strike, tf=None):
    """Remove a watched strike (or specific TF) after entry or invalidation."""
    ws = state.get('watched_strikes', {})
    key = f"{strike}_{opt_type}"
    if key not in ws:
        return
    if tf:
        ws[key].get('levels', {}).pop(tf, None)
        if not ws[key].get('levels'):
            del ws[key]
    else:
        del ws[key]

def _clean_watched_strikes():
    """Remove watched strikes from previous days (called at session start)."""
    state = _load(_state_path())
    ws = state.get('watched_strikes', {})
    if not ws:
        return
    today = datetime.datetime.now(IST).date()
    to_remove = [k for k, v in ws.items()
                 if _watch_is_stale(v, today)]
    if to_remove:
        for key in to_remove:
            ws.pop(key, None)
        _save(state, _state_path())
        log.info(f"Cleaned {len(to_remove)} old watched strikes")

def _watch_is_stale(info, today):
    try:
        added = datetime.datetime.fromisoformat(info.get('added', ''))
        if added.tzinfo is None:
            added = IST.localize(added)
        return added.date() < today
    except Exception:
        return True

# ---------------------------------------------------------------------------
# Alert Formatting
# ---------------------------------------------------------------------------

def _fmt_table(ce_levels, pe_levels, ce_ltp, pe_ltp):
    html = "<pre>"
    html += "     CE                    PE\n"
    html += " TF  Dir    ST    LTP Gap  Dir    ST    LTP Gap\n"
    html += "─────────────────────────────────────────────────\n"
    for tf in ALL_TIMEFRAMES:
        lbl = TF_DISPLAY[tf]
        parts = []
        for levels, ltp in [(ce_levels, ce_ltp), (pe_levels, pe_ltp)]:
            d = levels.get(tf, {})
            if d.get('direction') == 'INSUFFICIENT' or d.get('value',0) <= 0:
                parts.append("  --    N/A       ")
            else:
                sv = d['value']; cl = ltp if ltp > 0 else d['close']
                g = ((cl - sv)/sv)*100 if sv > 0 else 0
                icon = "\U0001f7e2" if d['direction']=='UP' else "\U0001f534"
                parts.append(f"{icon} {sv:>6.1f} {cl:>6.1f}{g:>+5.1f}%")
        html += f"{lbl:>3} {parts[0]} {parts[1]}\n"
    html += "─────────────────────────────────────────────────\n</pre>"
    return html

def _fmt_entry(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp, st_val, gap, spot):
    lbl = TF_DISPLAY[tf]; exp_str = expiry.strftime('%d-%b')
    sl = opt_ltp * (1 - PREMIUM_SL_PCT/100)
    html = f"<b>\U0001f7e2 ENTRY | NIFTY {opt_type} {strike} {lbl}</b>\n"
    html += f"Spot: {spot:,.2f} | Expiry: {exp_str} ({dte}d)\n"
    html += f"Option: <code>{opt_sym}</code>\n\n"
    html += f"Entry: <b>{opt_ltp:.2f}</b> | ST: {st_val:.2f} | Gap: {gap:+.1f}%\n"
    html += f"TP: premium touches ST ({st_val:.2f})\n"
    html += f"Premium SL: {sl:.2f} (-{PREMIUM_SL_PCT}%)\n"
    html += f"Cost SL: activates at gap &lt;= {COST_SL_GAP_PCT}%"
    return html

def _fmt_watching(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp, st_val, gap, spot):
    lbl = TF_DISPLAY[tf]; exp_str = expiry.strftime('%d-%b')
    html = f"<b>\U0001f7e1 WATCHING | NIFTY {opt_type} {strike} {lbl}</b>\n"
    html += f"Spot: {spot:,.2f} | {opt_type}: {opt_ltp:.2f} | ST: {st_val:.2f} | Gap: {gap:+.1f}%\n"
    html += f"<code>{opt_sym}</code> ({exp_str}, {dte}d)\n"
    html += f"Entry zone: gap &lt;= {ENTRY_GAP_PCT}%"
    return html

def _fmt_exit(pos, exit_prem, exit_type):
    entry = pos.get('entry_premium',0); pnl = exit_prem - entry
    pnl_pct = (pnl/entry*100) if entry > 0 else 0
    icons = {'TP':'\U0001f3af','COST_SL':'\U0001f4b0','TRAIL':'\U0001f4c9',
             'PREM_SL':'\U0001f6d1','EOD':'\U0001f556','STALE':'\u26a0\ufe0f'}
    labels = {'TP':'TARGET','COST_SL':'COST SL','TRAIL':'TRAILING SL',
              'PREM_SL':'PREMIUM SL','EOD':'END OF DAY','STALE':'STALE'}
    dur = ''
    try:
        mins = max(0,int((datetime.datetime.now(IST)-datetime.datetime.fromisoformat(pos['entry_time'])).total_seconds()/60))
        dur = f" | {mins//60}h{mins%60}m" if mins >= 60 else f" | {mins}m"
    except Exception: pass
    html = f"<b>{icons.get(exit_type,'?')} {labels.get(exit_type,exit_type)} | "
    html += f"{pos.get('opt_type','?')} {pos.get('strike','?')}{dur}</b>\n"
    html += f"<code>{pos.get('option_symbol','?')}</code>\n"
    html += f"Entry: {entry:.2f} → Exit: {exit_prem:.2f}\n"
    html += f"P&amp;L: {pnl:+.2f} ({pnl_pct:+.0f}%)"
    return html

# ---------------------------------------------------------------------------
# Position Management (Magnet-style)
# ---------------------------------------------------------------------------

def _open_pos(opt_type, tf, strike, expiry, opt_sym, opt_ltp, st_val, gap, spot):
    positions = _load(_pos_path())
    today = datetime.datetime.now(IST).strftime('%Y%m%d')
    key = f"NIFTY_{opt_type}_{tf}_{today}"
    if key in positions: return None  # used today (open or closed)
    inst = _get_instrument_info(opt_sym)
    positions[key] = {
        'status':'open', 'opt_type':opt_type, 'signal_tf':tf,
        'strike':strike, 'expiry':expiry.isoformat(),
        'option_symbol':opt_sym, 'exchange':'NFO',
        'entry_premium':opt_ltp, 'entry_spot':spot,
        'st_value':st_val, 'entry_gap':gap,
        'entry_time':datetime.datetime.now(IST).isoformat(), 'entry_date':today,
        'peak_premium':opt_ltp, 'cost_sl_active':False, 'cost_sl_level':0,
        'lot_size':inst['lot_size'], 'tick_size':inst['tick_size'],
    }
    _save(positions, _pos_path())
    log.info(f"Opened: {key} | {opt_sym} @ {opt_ltp:.2f} ST={st_val:.2f} gap={gap:+.1f}%")

    # Neo bridge: place entry + TP orders
    if TRADE_ENABLED and _NEO_BRIDGE:
        try:
            _NEO_BRIDGE.on_entry(key, positions[key])
        except Exception as e:
            log.error(f"Bridge on_entry failed: {e}")

    return key

def _close_pos(key, pos, exit_prem, exit_type):
    pos['status'] = 'closed'; pos['exit_type'] = exit_type
    pos['exit_premium'] = exit_prem; pos['exit_time'] = datetime.datetime.now(IST).isoformat()
    msg = _fmt_exit(pos, exit_prem, exit_type)
    try: common.sendMsgTelegram(msg, 'I')
    except Exception as e: log.error(f"Exit alert failed {key}: {e}")
    pnl = exit_prem - pos['entry_premium']
    log.info(f"{exit_type}: {key} | {pos['entry_premium']:.2f}→{exit_prem:.2f} P&L={pnl:+.2f}")

    # Neo trader: cancel TP + place exit order (pass exit_prem for C1 fix)
    if TRADE_ENABLED and _NEO_BRIDGE:
        try:
            _NEO_BRIDGE.on_exit(key, exit_type, exit_prem)
        except Exception as e:
            log.error(f"Neo on_exit failed: {e}")

def monitor_positions():
    """Magnet-style multi-layer exits. TP = option premium touches its ST from below."""
    positions = _load(_pos_path())
    modified = False
    for key, pos in positions.items():
        if pos.get('status') != 'open': continue
        entry_prem = pos.get('entry_premium',0)
        opt_sym = pos.get('option_symbol','')
        opt_type = pos.get('opt_type','')
        signal_tf = pos.get('signal_tf','')
        if not opt_sym or entry_prem <= 0: continue

        # Current option LTP
        depth = _quote(f"NFO:{opt_sym}")
        curr = depth['ltp']
        if curr <= 0: curr = depth.get('bid',0)
        if curr <= 0: continue

        # Update peak
        if curr > pos.get('peak_premium',0):
            pos['peak_premium'] = curr; modified = True

        # Use the ST value from ENTRY time (not current ATM which may have shifted)
        st_val = pos.get('st_value', 0)

        exit_type = None

        # Layer 1: TP — premium touches its ST from below (curr >= st_val)
        if st_val > 0 and curr >= st_val:
            exit_type = 'TP'
            log.info(f"{key}: premium {curr:.2f} >= ST {st_val:.2f} → TP")

        # Layer 1.5: Time-based SL (from backtest — closes if held too long)
        if not exit_type and TF_TIME_SL and signal_tf:
            try:
                entry_dt = datetime.datetime.fromisoformat(pos.get('entry_time', ''))
                if entry_dt.tzinfo is None:
                    entry_dt = IST.localize(entry_dt)
                now = datetime.datetime.now(IST)
                elapsed_mins = (now - entry_dt).total_seconds() / 60
                max_hold = TF_TIME_SL.get(signal_tf, TF_TIME_SL.get('default', 120))
                if elapsed_mins > max_hold:
                    exit_type = 'TIME_SL'
                    log.info(f"{key}: TIME_SL — held {elapsed_mins:.0f}min > {max_hold}min limit")
            except Exception as e:
                # Safer to close than hold forever with broken timestamp
                exit_type = 'TIME_SL'
                log.warning(f"{key}: TIME_SL parse error ({e}) — closing as safety fallback")

        # Layer 2: Cost SL activation (premium within COST_SL_GAP_PCT of ST)
        if not exit_type and st_val > 0 and not pos.get('cost_sl_active'):
            gap_to_st = ((st_val - curr) / st_val) * 100 if st_val > 0 else 99
            if gap_to_st <= COST_SL_GAP_PCT:
                pos['cost_sl_active'] = True
                pos['cost_sl_level'] = entry_prem + 0.10
                modified = True
                log.info(f"{key}: Cost SL active — gap to ST={gap_to_st:.1f}% floor={pos['cost_sl_level']:.2f}")
                try:
                    msg = f"<b>\U0001f4b0 COST SL | {opt_type} {pos.get('strike','?')}</b>\n"
                    msg += f"Premium {curr:.2f} within {gap_to_st:.1f}% of ST | Floor: {pos['cost_sl_level']:.2f}"
                    common.sendMsgTelegram(msg, 'I')
                except Exception: pass

        # Layer 3: Cost SL hit
        if not exit_type and pos.get('cost_sl_active') and curr <= pos.get('cost_sl_level',0):
            exit_type = 'COST_SL'

        # Layer 4: Trailing SL  [UNREACHABLE with current params — TP fires at ~3%, trail needs 15%]
        peak = pos.get('peak_premium', entry_prem)
        gain_pct = ((peak - entry_prem)/entry_prem*100) if entry_prem > 0 else 0
        if not exit_type and gain_pct >= TRAIL_MIN_GAIN_PCT:
            trail = entry_prem + (peak - entry_prem) * (TRAIL_PCT/100)
            if pos.get('cost_sl_active') and pos.get('cost_sl_level',0) > trail:
                trail = pos['cost_sl_level']
            if curr <= trail:
                exit_type = 'TRAIL'
                log.info(f"{key}: Trail hit — {curr:.2f} <= {trail:.2f} (peak={peak:.2f})")

        # Layer 5: Premium SL
        prem_sl = entry_prem * (1 - PREMIUM_SL_PCT/100)
        if not exit_type and curr <= prem_sl:
            exit_type = 'PREM_SL'

        if exit_type:
            _close_pos(key, pos, curr, exit_type); modified = True
        else:
            pnl_pct = ((curr-entry_prem)/entry_prem*100) if entry_prem > 0 else 0
            cost = " [COST SL]" if pos.get('cost_sl_active') else ""
            log.debug(f"{key}: {curr:.2f} ({pnl_pct:+.0f}%){cost}")

    if modified: _save(positions, _pos_path())

def _close_stale():
    positions = _load(_pos_path())
    today = datetime.datetime.now(IST).strftime('%Y%m%d')
    stale = [k for k, p in positions.items() if p.get('status') == 'open' and p.get('entry_date', '') < today]
    for k in stale:
        pos = positions[k]
        # Fetch last available premium before closing
        prem = 0
        try:
            depth = _quote(f"NFO:{pos.get('option_symbol', '')}")
            prem = max(depth['ltp'], depth.get('bid', 0), 0)
        except Exception:
            pass
        pos['status'] = 'closed'
        pos['exit_type'] = 'STALE'
        pos['exit_premium'] = prem
        pos['exit_time'] = datetime.datetime.now(IST).isoformat()
        pnl = prem - pos.get('entry_premium', 0)
        log.warning(f"Stale: {k} | exit_prem={prem:.2f} P&L={pnl:+.2f}")
    if stale:
        _save(positions, _pos_path())
        try:
            common.sendMsgTelegram(f"<b>\u26a0\ufe0f STALE</b> {len(stale)} position(s) closed from previous day", 'I')
        except Exception:
            pass

def _close_eod():
    positions = _load(_pos_path())
    modified = False
    for key, pos in positions.items():
        if pos.get('status') != 'open': continue
        depth = _quote(f"NFO:{pos['option_symbol']}")
        prem = max(depth['ltp'], depth.get('bid',0), 0)
        _close_pos(key, pos, prem, 'EOD'); modified = True
    if modified: _save(positions, _pos_path())

# ---------------------------------------------------------------------------
# Signal Detection
# ---------------------------------------------------------------------------

def _dedup(state_key, state):
    s = state.get(state_key, {}); last = s.get('t')
    if not last: return True
    try:
        return datetime.datetime.fromisoformat(last).date() < datetime.datetime.now(IST).date()
    except Exception: return True

def check_and_enter(ce_levels, pe_levels, ce_info, pe_info, spot, strike, expiry, dte, state):
    """Scan CE and PE for magnet signals: ST DOWN + premium below ST.

    Two zones:
      WATCHING: premium within WATCH_GAP_PCT (5%) below ST
      ENTRY:    premium within ENTRY_GAP_PCT (3%) below ST
    Mutual exclusion: only one of CE or PE per TF (no accidental straddle).
    """
    now_iso = datetime.datetime.now(IST).isoformat()
    positions = _load(_pos_path())
    today = datetime.datetime.now(IST).strftime('%Y%m%d')

    for opt_type, levels, info in [('CE', ce_levels, ce_info), ('PE', pe_levels, pe_info)]:
        if not info or info.get('ltp', 0) <= 0:
            continue
        opt_ltp = info['ltp']
        opt_sym = info['symbol']

        # Opposite type for mutual exclusion check
        opp = 'PE' if opt_type == 'CE' else 'CE'

        for tf in ALL_TIMEFRAMES:
            d = levels.get(tf, {})
            if d.get('direction') != 'DOWN' or d.get('value', 0) <= 0:
                continue

            st_val = d['value']
            gap = ((st_val - opt_ltp) / st_val) * 100  # positive = below ST
            if gap < 0 or gap > WATCH_GAP_PCT:
                continue  # outside signal range

            sk = f"{opt_type}_{tf}"

            if gap <= ENTRY_GAP_PCT:
                # --- ENTRY ZONE (within 3% of ST) ---
                # Mutual exclusion: skip if opposite side already open for this TF
                opp_key = f"NIFTY_{opp}_{tf}_{today}"
                if opp_key in positions and positions[opp_key].get('status') == 'open':
                    log.debug(f"Skip {opt_type} {tf}: opposite {opp} already open")
                    continue

                # Cross-TF exclusion: skip if same opt_type already open on ANY TF
                # Prevents two BUY orders for the same exchange symbol
                same_type_open = False
                for tf_chk in ALL_TIMEFRAMES:
                    chk_key = f"NIFTY_{opt_type}_{tf_chk}_{today}"
                    if chk_key in positions and positions[chk_key].get('status') == 'open':
                        same_type_open = True
                        log.debug(f"Skip {opt_type} {tf}: same type already open on {tf_chk}")
                        break
                if same_type_open:
                    continue

                ek = f"entry_{sk}"
                if _dedup(ek, state):
                    k = _open_pos(opt_type, tf, strike, expiry, opt_sym, opt_ltp, st_val, -gap, spot)
                    if k:
                        # Refresh positions dict so mutual exclusion sees this new entry
                        positions = _load(_pos_path())
                        msg = _fmt_entry(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp, st_val, -gap, spot)
                        try:
                            common.sendMsgTelegram(msg, 'I')
                        except Exception as e:
                            log.error(f"Entry alert fail: {e}")
                        # Clean from sticky watch if present
                        _remove_watched_strike(state, opt_type, strike, tf)
                    state[ek] = {'t': now_iso}

            else:
                # --- WATCHING ZONE (3-5% below ST) ---
                wk = f"watch_{sk}"
                if _dedup(wk, state):
                    msg = _fmt_watching(opt_type, strike, expiry, dte, tf, opt_sym, opt_ltp, st_val, -gap, spot)
                    try:
                        common.sendMsgTelegram(msg, 'I')
                    except Exception as e:
                        log.error(f"Watch alert fail: {e}")
                    state[wk] = {'t': now_iso}
                    log.info(f"WATCHING: {opt_type} {TF_DISPLAY[tf]} gap={gap:.1f}% below ST")

                # Sticky watch: record regardless of alert dedup so it persists
                _record_watched_strike(state, opt_type, strike, opt_sym,
                                       expiry.isoformat(), tf, st_val)

# ---------------------------------------------------------------------------
# Dual-Mode Dispatch
# ---------------------------------------------------------------------------

def should_update():
    p = _levels_path()
    if not os.path.exists(p): return True
    now = datetime.datetime.now(IST)
    if now.minute % 15 == 1: return True
    try:
        ts = _load(p).get('timestamp')
        if ts:
            st = datetime.datetime.fromisoformat(ts)
            if st.tzinfo is None: st = IST.localize(st)
            if (now - st).total_seconds()/60 > 16: return True
    except Exception: return True
    return False

def _pre_shift_check():
    """Final entry check on current strike before ATM shifts.

    If NIFTY moved and ATM is about to change, the old strike may now be
    in ENTRY zone (premium rose toward ST). Check it before the level
    update overwrites the saved data.
    """
    saved = _load(_levels_path())
    if not saved or 'CE_levels' not in saved:
        return

    old_strike = saved.get('strike', 0)
    ce_sym = saved.get('ce_symbol', '')
    pe_sym = saved.get('pe_symbol', '')
    expiry_str = saved.get('expiry', '')
    if not ce_sym or not pe_sym or not expiry_str:
        return

    # Quick spot check — is ATM actually shifting?
    spot_data = _ltp([NIFTY_SPOT_KEY])
    spot = spot_data.get(NIFTY_SPOT_KEY, 0)
    if spot <= 0:
        return
    new_strike = get_atm_strike(spot)
    if new_strike == old_strike:
        return  # ATM not shifting, normal scan handles it

    log.info(f"PRE-SHIFT: ATM {old_strike} -> {new_strike}, final check on old strike")

    # Fetch fresh LTPs for old strike options
    keys = []
    if ce_sym: keys.append(f"NFO:{ce_sym}")
    if pe_sym: keys.append(f"NFO:{pe_sym}")
    if not keys:
        return
    ltps = _ltp(keys)
    ce_ltp = ltps.get(f"NFO:{ce_sym}", 0)
    pe_ltp = ltps.get(f"NFO:{pe_sym}", 0)

    ce_levels = copy.deepcopy(saved.get('CE_levels', {}))
    pe_levels = copy.deepcopy(saved.get('PE_levels', {}))

    # Patch close with fresh LTP
    for tf in ALL_TIMEFRAMES:
        if ce_ltp > 0 and ce_levels.get(tf, {}).get('value', 0) > 0:
            ce_levels[tf]['close'] = ce_ltp
        if pe_ltp > 0 and pe_levels.get(tf, {}).get('value', 0) > 0:
            pe_levels[tf]['close'] = pe_ltp

    try:
        expiry = datetime.date.fromisoformat(expiry_str)
    except Exception:
        return
    dte = (expiry - datetime.datetime.now(IST).date()).days

    ce_info = {'symbol': ce_sym, 'ltp': ce_ltp} if ce_ltp > 0 else None
    pe_info = {'symbol': pe_sym, 'ltp': pe_ltp} if pe_ltp > 0 else None

    now_time = datetime.datetime.now(IST).time()
    if SIGNAL_START_TIME <= now_time < datetime.time(15, 25):
        state = _load(_state_path())
        check_and_enter(ce_levels, pe_levels, ce_info, pe_info,
                        spot, old_strike, expiry, dte, state)
        _save(state, _state_path())

def run_level_update(kite, df_instruments):
    time.sleep(15)
    log.info("--- LEVEL UPDATE ---")

    # Pre-check: enter old strike if it's now in ENTRY zone before ATM shifts
    _pre_shift_check()

    spot_data = _ltp([NIFTY_SPOT_KEY])
    spot = spot_data.get(NIFTY_SPOT_KEY, 0)
    if spot <= 0: log.error("No spot"); return

    strike = get_atm_strike(spot)
    expiry = get_weekly_expiry()
    if not expiry: log.error("No expiry"); return
    dte = (expiry - datetime.datetime.now(IST).date()).days
    log.info(f"Spot: {spot:.2f} | ATM: {strike} | Expiry: {expiry} ({dte}d)")

    ce_tok, ce_sym = _resolve_opt(strike, expiry, 'CE')
    pe_tok, pe_sym = _resolve_opt(strike, expiry, 'PE')
    if not ce_tok or not pe_tok: log.error(f"Resolve failed CE={ce_sym} PE={pe_sym}"); return

    ce_levels = compute_option_st(ce_tok)
    pe_levels = compute_option_st(pe_tok)

    data = {
        'timestamp': datetime.datetime.now(IST).isoformat(),
        'spot': spot, 'strike': strike, 'expiry': expiry.isoformat(), 'dte': dte,
        'ce_symbol': ce_sym, 'pe_symbol': pe_sym,
        'ce_token': ce_tok, 'pe_token': pe_tok,
        'CE_levels': ce_levels, 'PE_levels': pe_levels,
    }
    _save(data, _levels_path())

    for ot, levels in [('CE',ce_levels),('PE',pe_levels)]:
        print(f"\n  {ot} {strike}:")
        for tf in ALL_TIMEFRAMES:
            d = levels.get(tf,{})
            if d.get('direction')=='INSUFFICIENT': print(f"    {TF_DISPLAY[tf]:>3}  --  N/A"); continue
            g = ((d['close']-d['value'])/d['value']*100) if d['value']>0 else 0
            marker = " <<<" if d['direction']=='DOWN' and -WATCH_GAP_PCT <= g < 0 else ""
            print(f"    {TF_DISPLAY[tf]:>3}  {d['direction']:>4}  ST={d['value']:.1f}  LTP={d['close']:.1f}  gap={g:+.1f}%{marker}")
    log.info("Levels saved")

def _check_watched_strikes(spot, current_strike, expiry, dte, state):
    """Check watched strikes that differ from current ATM for entry signals.

    After an ATM shift, previously watched strikes are still tracked here.
    Batch-fetches LTPs, enters if gap narrows to ENTRY zone, cleans up stale.
    """
    ws = state.get('watched_strikes', {})
    if not ws:
        return

    today = datetime.datetime.now(IST).date()
    today_str = today.strftime('%Y%m%d')
    positions = _load(_pos_path())
    now_iso = datetime.datetime.now(IST).isoformat()

    # Collect symbols for watched strikes that differ from current ATM
    active = {}
    for key, info in list(ws.items()):
        s = info.get('strike', 0)
        sym = info.get('symbol', '')
        if not sym or not info.get('levels'):
            ws.pop(key, None); continue
        if s == current_strike:
            continue  # normal scan handles this
        # Check expiry
        try:
            exp = datetime.date.fromisoformat(info.get('expiry', ''))
            if exp < today:
                ws.pop(key, None); continue
        except Exception:
            ws.pop(key, None); continue
        active[key] = info

    if not active:
        return

    # Batch LTP fetch for all watched symbols
    sym_keys = {k: f"NFO:{v['symbol']}" for k, v in active.items()}
    unique_keys = list(set(sym_keys.values()))
    all_ltps = _ltp(unique_keys) if unique_keys else {}

    for key, info in active.items():
        strike = info['strike']
        opt_type = info['opt_type']
        symbol = info['symbol']
        levels = info.get('levels', {})
        ltp = all_ltps.get(f"NFO:{symbol}", 0)
        if ltp <= 0:
            continue  # can't check, keep watching

        try:
            exp = datetime.date.fromisoformat(info['expiry'])
        except Exception:
            continue
        exp_dte = (exp - today).days

        tfs_done = []
        for tf, st_val in list(levels.items()):
            if st_val <= 0:
                tfs_done.append(tf); continue

            gap = ((st_val - ltp) / st_val) * 100  # positive = below ST

            # Premium above ST — signal invalid for this TF
            if gap < 0:
                tfs_done.append(tf)
                log.info(f"STICKY DROP: {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} "
                         f"— prem {ltp:.2f} above ST {st_val:.2f}")
                continue

            # Drifted too far away
            if gap > STICKY_WATCH_MAX_GAP:
                tfs_done.append(tf)
                log.info(f"STICKY DROP: {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} "
                         f"— gap {gap:.1f}% > {STICKY_WATCH_MAX_GAP}%")
                continue

            # ENTRY zone
            if gap <= ENTRY_GAP_PCT:
                opp = 'PE' if opt_type == 'CE' else 'CE'
                opp_key = f"NIFTY_{opp}_{tf}_{today_str}"
                if opp_key in positions and positions[opp_key].get('status') == 'open':
                    log.debug(f"STICKY skip {opt_type} {strike} {tf}: opposite open")
                    continue

                # Cross-TF exclusion: same opt_type already open on any TF
                same_open = any(
                    positions.get(f"NIFTY_{opt_type}_{t}_{today_str}", {}).get('status') == 'open'
                    for t in ALL_TIMEFRAMES)
                if same_open:
                    log.debug(f"STICKY skip {opt_type} {strike} {tf}: same type open another TF")
                    continue

                ek = f"entry_{opt_type}_{tf}"
                if _dedup(ek, state):
                    k = _open_pos(opt_type, tf, strike, exp, symbol, ltp,
                                  st_val, -gap, spot)
                    if k:
                        positions = _load(_pos_path())
                        msg = _fmt_entry(opt_type, strike, exp, exp_dte, tf,
                                         symbol, ltp, st_val, -gap, spot)
                        msg += "\n<i>(watched strike — ATM shifted)</i>"
                        try:
                            common.sendMsgTelegram(msg, 'I')
                        except Exception as e:
                            log.error(f"Sticky entry alert fail: {e}")
                        log.info(f"STICKY ENTRY: {opt_type} {strike} "
                                 f"{TF_DISPLAY.get(tf,tf)} gap={gap:.1f}%")
                    state[ek] = {'t': now_iso}
                    tfs_done.append(tf)
            else:
                log.debug(f"STICKY: {opt_type} {strike} {TF_DISPLAY.get(tf,tf)} "
                          f"gap={gap:.1f}% — watching")

        for tf in tfs_done:
            levels.pop(tf, None)
        if not levels:
            ws.pop(key, None)

def run_ltp_scan():
    log.info("--- LTP SCAN ---")
    saved = _load(_levels_path())
    if not saved or 'CE_levels' not in saved: log.warning("No levels"); return

    ce_levels = copy.deepcopy(saved.get('CE_levels',{}))
    pe_levels = copy.deepcopy(saved.get('PE_levels',{}))
    strike = saved.get('strike',0); ce_sym = saved.get('ce_symbol',''); pe_sym = saved.get('pe_symbol','')
    expiry_str = saved.get('expiry','')
    if not ce_sym or not pe_sym: return
    try: expiry = datetime.date.fromisoformat(expiry_str)
    except Exception: return
    dte = (expiry - datetime.datetime.now(IST).date()).days

    # Batch fetch LTPs
    all_ltps = _ltp([NIFTY_SPOT_KEY, f"NFO:{ce_sym}", f"NFO:{pe_sym}"])
    spot = all_ltps.get(NIFTY_SPOT_KEY, 0)
    ce_ltp = all_ltps.get(f"NFO:{ce_sym}", 0)
    pe_ltp = all_ltps.get(f"NFO:{pe_sym}", 0)

    # Update close with live LTP
    for tf in ALL_TIMEFRAMES:
        if ce_ltp > 0 and ce_levels.get(tf,{}).get('value',0) > 0: ce_levels[tf]['close'] = ce_ltp
        if pe_ltp > 0 and pe_levels.get(tf,{}).get('value',0) > 0: pe_levels[tf]['close'] = pe_ltp

    # Log
    for ot, levels, ltp in [('CE',ce_levels,ce_ltp),('PE',pe_levels,pe_ltp)]:
        parts = []
        for tf in ALL_TIMEFRAMES:
            d = levels.get(tf,{})
            if d.get('direction')=='DOWN' and d.get('value',0)>0 and ltp>0:
                g = ((d['value']-ltp)/d['value'])*100
                parts.append(f"{TF_DISPLAY[tf]}:{g:.1f}%↓ST{'*' if g <= WATCH_GAP_PCT else ''}")
        if parts: log.info(f"  {ot} {strike} LTP={ltp:.2f} | {' | '.join(parts)}")

    ce_info = {'symbol':ce_sym,'ltp':ce_ltp} if ce_ltp > 0 else None
    pe_info = {'symbol':pe_sym,'ltp':pe_ltp} if pe_ltp > 0 else None

    # Signals: only between SIGNAL_START_TIME (9:30) and EOD window (15:25)
    now_time = datetime.datetime.now(IST).time()
    if SIGNAL_START_TIME <= now_time < datetime.time(15,25):
        state = _load(_state_path())
        check_and_enter(ce_levels, pe_levels, ce_info, pe_info, spot, strike, expiry, dte, state)
        _check_watched_strikes(spot, strike, expiry, dte, state)
        _save(state, _state_path())
    elif now_time < SIGNAL_START_TIME:
        log.info(f"Pre-signal window ({now_time.strftime('%H:%M')}) — levels only, no signals")

    # Monitor positions
    try: monitor_positions()
    except Exception as e: log.error(f"Monitor failed: {e}", exc_info=True)

# ---------------------------------------------------------------------------
# Lock & Main
# ---------------------------------------------------------------------------

_LOCK = os.path.join(CURRENT_DIR, 'option_buy.lock')

def _pid_alive(pid):
    if platform.system()=='Windows':
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000,False,pid)
        if h: ctypes.windll.kernel32.CloseHandle(h); return True
        return False
    try: os.kill(pid,0); return True
    except OSError: return False

def acquire_lock():
    if os.path.exists(_LOCK):
        try:
            with open(_LOCK) as f: pid = int(f.read().strip())
            if _pid_alive(pid): return False
        except Exception: pass
    with open(_LOCK,'w') as f: f.write(str(os.getpid()))
    return True

def release_lock():
    try:
        if os.path.exists(_LOCK):
            with open(_LOCK) as f: p = int(f.read().strip())
            if p == os.getpid(): os.remove(_LOCK)
    except Exception: pass

def main():
    if not acquire_lock(): log.warning("Locked"); sys.exit(0)
    atexit.register(release_lock)
    log.info("=" * 60); log.info("NIFTY OPTION MAGNET"); log.info("=" * 60)

    now = datetime.datetime.now(IST)
    if now.weekday() >= 5: log.info("Weekend"); sys.exit()
    if now.time() < datetime.time(9,15) or now.time() >= datetime.time(15,30):
        log.info(f"Off hours ({now.strftime('%H:%M')})"); sys.exit()

    # Initialize Neo bridge + time SL config
    _init_bridge()

    try: _close_stale()
    except Exception as e: log.error(f"Stale: {e}")

    try: _clean_watched_strikes()
    except Exception as e: log.error(f"Clean watches: {e}")

    if now.time() >= datetime.time(15,25):
        log.info("EOD -- closing all")
        try: _close_eod()
        except Exception as e: log.error(f"EOD: {e}")

    df_instruments = get_instruments()
    kite = get_kite()

    if should_update(): run_level_update(kite, df_instruments)
    run_ltp_scan()

    # Sync TP fills from Neo exchange (if bridge active)
    # CRITICAL: must update st_opt_positions.json to prevent double-sell
    if TRADE_ENABLED and _NEO_BRIDGE:
        try:
            fills = _NEO_BRIDGE.sync_tp_fills()
            if fills:
                positions = _load(_pos_path())
                for key, fill_price in fills:
                    log.info(f"TP filled on Neo: {key} @ {fill_price}")
                    if key in positions and positions[key].get('status') == 'open':
                        _close_pos(key, positions[key], fill_price, 'TP')
                _save(positions, _pos_path())

            # Also sync entry rejections — mark them closed in our state
            rejections = _NEO_BRIDGE.get_rejected_entries()
            if rejections:
                positions = _load(_pos_path())
                for key in rejections:
                    if key in positions and positions[key].get('status') == 'open':
                        positions[key]['status'] = 'closed'
                        positions[key]['exit_type'] = 'ENTRY_REJECTED'
                        log.warning(f"Entry rejected on exchange: {key}")
                _save(positions, _pos_path())
        except Exception as e:
            log.error(f"TP sync failed: {e}")

    log.info("=" * 60); log.info("COMPLETE"); log.info("=" * 60)

if __name__ == "__main__":
    main()

"""Smoke tests for exchange tick-size handling.

Regression cover for the 2026-07-23 ICRA rejection: the bot rounded order
prices with a 0.05/0.10 price-band heuristic while ICRA trades at a 0.50 tick,
so Zerodha rejected the GTT ("Trigger price should be a multiple of tick size
0.50.") and the retry path missed it because its regex demanded the word "is".

No network: the instruments cache is a temp CSV.
Run: cd FIFTY && python _research/filter_stretch/tick_size_smoke_test.py
"""
import os
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd

# Derive the FIFTY root from this file - these tests run on the Pi too, so a
# hardcoded Windows path breaks the deployment script.
FIFTY_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, FIFTY_DIR)
os.chdir(FIFTY_DIR)

from src.utils.price_rounder import PriceRounder

PASS, FAIL = [], []


def check(name, cond, detail=''):
    (PASS if cond else FAIL).append(name)
    print(('PASS' if cond else 'FAIL') + f' | {name} | {detail}')


# --- T1: parse the required tick out of BOTH Zerodha phrasings -------------
from src.utils.price_rounder import parse_required_tick

OLD_PATTERN = r'tick size.*?is\s+(0\.\d+)'
REAL_MSG = 'Trigger price should be a multiple of tick size 0.50.'
cases = [
    (REAL_MSG, 0.50),
    ('Price should be a multiple of tick size is 0.05', 0.05),
    ('tick size 1.00', 1.00),
    ('order price must be a multiple of tick size 5.00', 5.00),
]
ok = True
for msg, expected in cases:
    got = parse_required_tick(msg)
    if got != expected:
        ok = False
        print(f'   miss: {msg!r} -> {got}')
check('T1 parses all Zerodha tick phrasings', ok, f'{len(cases)} messages')

# Document the actual defect: the old pattern silently missed the real message.
check('T1 old regex provably missed the real message',
      re.search(OLD_PATTERN, REAL_MSG, re.IGNORECASE) is None,
      repr(REAL_MSG))

# --- T1b: digit-LEADING NSE symbols must not be mistaken for the tick ------
# 20MICRONS / 3MINDIA / 360ONE / 5PAISA are real NSE symbols. A naive
# "first number after 'tick size'" parse captures the SYMBOL's digits, and
# acting on that would re-round an order to a multiple of 20.
naive = r'tick size\D{0,20}?(\d+(?:\.\d+)?)'
traps = [
    ('Tick size for 20MICRONS is 0.05', 0.05),
    ('Tick size for 3MINDIA is 0.50', 0.50),
    ('tick size for 360ONE is 0.05', 0.05),
    ('tick size for 5PAISA is 0.01', 0.01),
]
ok = True
naive_wrong = 0
for msg, expected in traps:
    got = parse_required_tick(msg)
    if got != expected:
        ok = False
        print(f'   miss: {msg!r} -> {got}')
    m = re.search(naive, msg, re.IGNORECASE)
    if m and float(m.group(1)) != expected:
        naive_wrong += 1
check('T1b digit-leading symbols parsed correctly', ok, f'{len(traps)} messages')
check('T1b naive regex would have been wrong on all of them',
      naive_wrong == len(traps), f'{naive_wrong}/{len(traps)} mis-parsed')

# --- T1c: implausible values are refused, never guessed --------------------
check('T1c unparseable -> None (caller must not retry)',
      parse_required_tick('tick size for FOO is 20') is None
      and parse_required_tick('some unrelated error') is None
      and parse_required_tick('') is None
      and parse_required_tick(None) is None,
      'bogus ticks rejected')

# --- T2: rounding on the real tick yields exchange-valid prices ------------
# The four live queue symbols that carried a coarse tick on 2026-07-28.
live = [
    ('ICRA', 4939.36, 0.50),
    ('INDIGO', 3615.48, 0.50),
    ('PGHL', 4515.08, 0.50),
    ('SHREECEM', 22737.82, 5.00),
]
ok = True
for sym, price, tick in live:
    rounded = PriceRounder.round_to_tick(price, tick_size=tick)
    down = PriceRounder.round_down_to_tick(price, tick_size=tick)
    for value in (rounded, down):
        # Exchange-valid == an exact multiple of the tick.
        if round(value / tick) * tick - value > 1e-6:
            ok = False
            print(f'   {sym}: {value} is not a multiple of {tick}')
check('T2 real tick yields exchange-valid prices', ok, str(live[0]))

# The heuristic is what produced the rejected ICRA price.
heuristic = PriceRounder.round_to_tick(4924.70)  # auto -> 0.10 band
check('T2 heuristic reproduces the rejected price',
      abs(heuristic % 0.50) > 1e-6,
      f'{heuristic} is not a multiple of 0.50 -> exchange rejects')

# --- T3: get_tick_size reads the exchange value out of the cache ----------
tmp = Path(tempfile.mkdtemp(prefix='ticks_'))
csv = tmp / 'instruments.csv'
pd.DataFrame([
    {'instrument_token': '1', 'tradingsymbol': 'ICRA', 'name': 'ICRA',
     'exchange': 'NSE', 'instrument_type': 'EQ', 'tick_size': 0.5},
    {'instrument_token': '2', 'tradingsymbol': 'SHREECEM', 'name': 'Shree',
     'exchange': 'NSE', 'instrument_type': 'EQ', 'tick_size': 5.0},
    {'instrument_token': '3', 'tradingsymbol': 'M&M', 'name': 'M and M',
     'exchange': 'NSE', 'instrument_type': 'EQ', 'tick_size': 0.05},
]).to_csv(csv, index=False)

from src.api.dual_kite_client import DualKiteClient

client = DualKiteClient.__new__(DualKiteClient)   # skip __init__ (no network)
client._instruments_cache = {}
client._token_to_symbol_cache = {}
client._tick_size_cache = {}
client._instruments_file = str(csv)
client._fetch_instruments = lambda: (_ for _ in ()).throw(
    AssertionError('must not hit the network when the cache is valid'))

check('T3 coarse tick read from cache', client.get_tick_size('ICRA') == 0.5,
      str(client.get_tick_size('ICRA')))
check('T3 very coarse tick read from cache',
      client.get_tick_size('SHREECEM') == 5.0, str(client.get_tick_size('SHREECEM')))
# Lookup must use the SAME symbol-variation rules as get_instrument_token, so
# a symbol that resolves to a token also resolves to a tick ('M&M' -> 'M-M'/'MM').
# Note the mapping is one-way by design: 'M-M' does not resolve back to 'M&M'.
check('T3 symbol variations resolve', client.get_tick_size('M&M') == 0.05,
      str(client.get_tick_size('M&M')))
check('T3 variation rules match token lookup',
      client._generate_symbol_variations('M&M') == ['M&M', 'M-M', 'MM'],
      str(client._generate_symbol_variations('M&M')))
check('T3 unknown symbol -> None (caller falls back)',
      client.get_tick_size('NOSUCHSYM') is None,
      str(client.get_tick_size('NOSUCHSYM')))

# --- T4: a legacy cache with no tick_size column forces a refetch ---------
# Otherwise the bot would silently keep using the heuristic for a whole day.
legacy = tmp / 'legacy.csv'
pd.DataFrame([
    {'instrument_token': '1', 'tradingsymbol': 'ICRA', 'name': 'ICRA',
     'exchange': 'NSE', 'instrument_type': 'EQ'},
]).to_csv(legacy, index=False)

refetched = []
client2 = DualKiteClient.__new__(DualKiteClient)
client2._instruments_cache = {}
client2._token_to_symbol_cache = {}
client2._tick_size_cache = {}
client2._instruments_file = str(legacy)
client2._fetch_instruments = lambda: (refetched.append(True), True)[1]
client2._load_instruments_cache()
check('T4 legacy cache without tick_size triggers refetch',
      refetched == [True], f'refetched={refetched}')

# --- T5: None tick falls back to the heuristic, never to a hardcoded 0.05 --
check('T5 None tick falls back to price band',
      PriceRounder.round_to_tick(4939.36, tick_size=None)
      == PriceRounder.round_to_tick(4939.36),
      str(PriceRounder.round_to_tick(4939.36, tick_size=None)))

# --- T6: SL GTT retry must re-round the LIMIT on the corrected tick --------
# Regression for the closure bug: the retry corrected only the trigger, while
# the limit kept being rounded on the stale heuristic grid -> every attempt
# rejected -> position left with NO stop loss. Simulates a coarse-tick scrip
# whose tick lookup returned None (cache miss), which is exactly the fallback
# the retry exists to rescue.
class TickyBroker:
    """Accepts a GTT only when BOTH trigger and limit sit on a 0.50 grid."""
    def __init__(self):
        self.attempts = []

    def place_gtt_order(self, payload):
        trg = payload['condition']['trigger_values'][0]
        lim = payload['orders'][0]['price']
        self.attempts.append((trg, lim))
        for v in (trg, lim):
            if abs(v / 0.50 - round(v / 0.50)) > 1e-6:
                raise Exception(
                    'Trigger price should be a multiple of tick size 0.50.')
        return {'trigger_id': 999}


# Replicate the exit_manager control flow exactly (boxed tick + retry loop).
def simulate(box_the_tick: bool):
    broker = TickyBroker()
    sl_tick_box = [None]          # cache miss -> heuristic grid
    buffer = 0.05

    def rnd(p):
        return PriceRounder.round_down_to_tick(p, tick_size=sl_tick_box[0])

    sl = 441.30
    for attempt in range(3):
        payload = {'condition': {'trigger_values': [sl]},
                   'orders': [{'price': rnd(sl * (1 - buffer))}]}
        try:
            broker.place_gtt_order(payload)
            return True, broker.attempts
        except Exception as e:
            tick = parse_required_tick(str(e))
            if tick and attempt < 2:
                if box_the_tick:          # the fix
                    sl_tick_box[0] = tick
                sl = PriceRounder.round_down_to_tick(sl, tick_size=tick)
                continue
            return False, broker.attempts


fixed_ok, fixed_attempts = simulate(box_the_tick=True)
buggy_ok, buggy_attempts = simulate(box_the_tick=False)
check('T6 SL GTT succeeds once the tick is propagated to the limit',
      fixed_ok, f'attempts={fixed_attempts}')
check('T6 old behaviour provably burned all retries (no SL placed)',
      not buggy_ok, f'attempts={buggy_attempts}')

# --- T7: an unresolvable symbol must not re-download the instrument list ----
# get_instrument_token() refetches all ~10k NSE instruments whenever a symbol
# misses. The breadth universe carries 73 delisted names, so a single backfill
# sweep triggered 73 full downloads and effectively never finished. A delisted
# name in the signal CSV (GSPL) did the same thing on every 5-minute cycle.
import src.api.dual_kite_client as dkc

fetches = []
client3 = DualKiteClient.__new__(DualKiteClient)
client3._instruments_cache = {'REALSYM': '111'}
client3._token_to_symbol_cache = {}
client3._tick_size_cache = {}
client3._missing_symbols = set()
client3._last_instruments_fetch = 0.0
client3._instruments_file = str(csv)


def _fake_fetch():
    fetches.append(1)
    return True


client3._fetch_instruments = _fake_fetch

# cached lookup: never fetches, never raises
check('T7 cached lookup resolves a known symbol',
      client3.get_instrument_token_cached('REALSYM') == '111', 'REALSYM')
check('T7 cached lookup returns None for a delisted name',
      client3.get_instrument_token_cached('DELISTED1') is None
      and client3.get_instrument_token_cached('DELISTED2') is None,
      f'fetches so far: {len(fetches)}')
check('T7 cached lookup NEVER triggers a download', len(fetches) == 0,
      f'{len(fetches)} fetches')

# uncached lookup: at most ONE download, then the negative cache takes over
for _ in range(5):
    try:
        client3.get_instrument_token('DELISTED1')
    except ValueError:
        pass
check('T7 repeated misses trigger exactly ONE download', len(fetches) == 1,
      f'{len(fetches)} fetches for 5 lookups')

# a DIFFERENT missing symbol must not re-download either, inside the cooldown
for _ in range(3):
    try:
        client3.get_instrument_token('DELISTED2')
    except ValueError:
        pass
check('T7 cooldown blocks downloads for other misses too', len(fetches) == 1,
      f'{len(fetches)} fetches')

# still raises ValueError, so callers behave exactly as before
raised = False
try:
    client3.get_instrument_token('DELISTED1')
except ValueError:
    raised = True
check('T7 still raises ValueError for callers', raised, 'contract unchanged')

# once the cooldown lapses, one more genuine attempt is allowed
client3._last_instruments_fetch -= (dkc.INSTRUMENTS_REFETCH_COOLDOWN + 1)
client3._missing_symbols.clear()
try:
    client3.get_instrument_token('DELISTED1')
except ValueError:
    pass
check('T7 a lapsed cooldown permits one fresh attempt', len(fetches) == 2,
      f'{len(fetches)} fetches')

print('=' * 50)
print(f'{len(PASS)} passed, {len(FAIL)} failed')
if FAIL:
    print('FAILED: ' + ', '.join(FAIL))
sys.exit(1 if FAIL else 0)

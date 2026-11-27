"""Per-underlying configuration for the option magnet (spec SPEC_multi_instrument_2026-10-06 §11/§12).

One frozen record per tradeable underlying. option_buy.py chooses ONE record at import
(env OB_UNDERLYING -> --underlying -> 'NIFTY') and every instrument-specific literal it used to
hard-code (key prefix, exchange prefix, reference key, session, file names) is read from it.

NIFTY's record is, field for field, the constants the live bot ran with before this module
existed (option_buy.py 132,870 b, sha256 2b897a4c...). The NIFTY record and an empty
file_suffix are what keep NIFTY's file names, ledger keys and Telegram text byte-identical; the
record/replay harness (harness/replay_diff.py) is the proof.

Strategy constants that are the SAME for every instrument (zone %, exit ladder, TIME_SL) stay in
option_buy.py / config_trade.yaml. Only what differs per instrument lives here.

WHY MCX close is a function, not a constant: MCX's evening session follows US trading hours, so
it ends 23:30 IST while the US is on daylight saving and 23:55 IST otherwise (US DST ends
2026-11-01). Hard-coding either value would be wrong for half the year.
"""
import datetime
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import pytz

NEW_YORK = pytz.timezone('America/New_York')
SHADOW_TIME_SL_CAPS = (60, 90, 120, None)   # minutes; None = no cap (held to EOD). spec Q-B2
# Morning/evening boundary for MCX: the ledger's session_half tag and 'per_half' slots both use
# it. 17:00 IST ~ the US pre-market, after which energy trades on US flow (10-06 backtest).
SESSION_SPLIT = datetime.time(17, 0)
SLOT_MODES = ('per_day', 'per_half', 'unlimited')


def us_dst_active(day):
    """True if US Eastern daylight saving is in effect on `day` (checked at noon NY time)."""
    noon = NEW_YORK.localize(datetime.datetime(day.year, day.month, day.day, 12, 0))
    return bool(noon.dst())


def nse_close(_day):
    return datetime.time(15, 30)


def mcx_close(day):
    """MCX non-agri close: 23:30 IST during US DST, 23:55 IST otherwise."""
    return datetime.time(23, 30) if us_dst_active(day) else datetime.time(23, 55)


@dataclass(frozen=True)
class Underlying:
    name: str                      # instruments 'name' AND the ledger key prefix
    exchange: str                  # option exchange prefix used in Kite keys ('NFO', 'MCX')
    ref_key: Optional[str]         # ATM reference quote key; None = mapped futures contract
    session_open: datetime.time    # also the candle anchor Kite uses for intraday candles
    session_close: Callable        # day -> time
    eod_offset_min: int            # EOD force-close = session close - this
    lot_fallback: int              # units per lot if the instruments lookup fails
    tick_fallback: float
    multiplier: Optional[int]      # units per lot for P&L; None = instruments lot_size (NFO)
    fresh_min: int                 # liquidity: option must have traded within N minutes
    max_spread_pct: Optional[float]  # liquidity: None = spread gate off (metrics only)
    cost_floor_rs: Optional[float]   # cost-SL floor offset in Rs; None = use cost_floor_ticks
    cost_floor_ticks: int
    slot_mode: str                 # 'per_day': one entry per side x TF per day | 'per_half': one
                                   # before SESSION_SPLIT + one after | 'unlimited' (dormant)
    no_entry_windows: Tuple = ()
    min_t1_distance: float = 0.0
    events: Tuple[str, ...] = ()   # event-calendar ids that gate this underlying
    quote_companion: Optional[str] = None   # full-size chain quoted alongside (liquidity probe)
    shadow_caps: bool = False      # record shadow TIME_SL outcomes (non-NIFTY only, review-2 B6)
    turbulence: bool = False       # NIFTY+VIX tripwire (NIFTY only, review-1 M10)
    telegram_mode: str = 'full'    # 'full' | 'entry_exit'
    orders_allowed: bool = False   # hard gate under config_trade.yaml trade_enabled
    enabled: bool = False          # process refuses to run until its build steps have landed
    strike_step: Optional[float] = None   # fixed strike grid (NIFTY 50); None = strikes listed by Kite
    entry_from: Optional[datetime.time] = None   # no NEW entries before this time; skip-only (the
                                   # side x TF slot is not consumed), exits/levels/watch run all day

    def __post_init__(self):
        # A typo here would silently fall through to the per-day code path.
        if self.slot_mode not in SLOT_MODES:
            raise ValueError(f"{self.name}: slot_mode {self.slot_mode!r} not in {SLOT_MODES}")
        if self.entry_from is not None and not isinstance(self.entry_from, datetime.time):
            raise ValueError(f"{self.name}: entry_from must be a datetime.time")

    @property
    def file_suffix(self):
        """'' for NIFTY (its file names must not change); '_<NAME>' for every other underlying."""
        return '' if self.name == 'NIFTY' else f'_{self.name}'

    @property
    def update_phase(self):
        """Level-update minute (mod 10): 6 minutes into each 10M candle, as NIFTY runs today.

        _st() reads the FORMING candle, so the update minute decides how much of the candle
        exists when ST is read. NSE candles open at :05/:15/:25 (anchor 09:15) -> phase 1, the
        live bot's `minute % 10 == 1`. MCX candles open on the hour (anchor 09:00) -> phase 6.
        """
        return (self.session_open.minute + 6) % 10

    @property
    def signal_start(self):
        """First 15 minutes of the session are levels-only (09:30 for NSE, 09:15 for MCX)."""
        t = datetime.datetime.combine(datetime.date(2000, 1, 1), self.session_open)
        return (t + datetime.timedelta(minutes=15)).time()

    def eod_close(self, day):
        t = datetime.datetime.combine(day, self.session_close(day))
        return (t - datetime.timedelta(minutes=self.eod_offset_min)).time()


_NIFTY_NO_ENTRY = (
    (datetime.time(10, 30), datetime.time(11, 30)),
    (datetime.time(14, 15), datetime.time(14, 45)),
)

# slot_mode 'per_day' since 2026-10-26 (was 'unlimited'): the first live MCX hour re-bought
# NATGASMINI PE 285 10M one minute after its TIME_SL exit — premium still in zone, no fresh
# touch. User: same entry rules as NIFTY. 'unlimited' code path stays for a future revisit.
# 2026-10-27 session split (user): energy (crude, natgas) enters only from SESSION_SPLIT — the
# 10-06 backtest had energy's evening beat its morning and it trades on US flow/EIA. Metals did
# the opposite in that backtest, so they keep the whole session with separate morning and
# evening slots ('per_half') until paper data picks a half. Day one (10-26) was 1 day, n=26.
_EVENING_ONLY = SESSION_SPLIT
# 2026-10-28 CRUDEOILM DROPPED (user, after review): premium does not revert after a zone entry
# (backtest post-entry drift -0.4/-0.9/-0.7% at 15/30/45m, 40-46% up, n=72; NIFTY-like BANKNIFTY
# +0.8/+1.2/+1.7%). Gross PF <1 before costs in backtest, live and every shadow cap; the full
# CRUDEOIL contract re-priced from the companion quotes is the same trades x10. Record kept so
# its files/ledger stay addressable and re-enabling is one flag; the cron line now exits at once.
# 2026-11-05 ALL NON-NIFTY DISABLED (user): paper 10-26..11-04 MCX 130 trades gross -23.3k,
# net -28.5k PF 0.59 (worse than random before costs, bootstrap P(mean<=0)=0.98); BANKNIFTY 16
# trades net -11.2k PF 0.33. Matches the 10-06 backtest (MCX gross PF 0.50-0.96). Records kept
# so re-enabling one is a single flag; their cron lines exit at once.
_COMMON_MCX = dict(exchange='MCX', ref_key=None, session_open=datetime.time(9, 0),
                   session_close=mcx_close, eod_offset_min=10, cost_floor_rs=None,
                   cost_floor_ticks=2, slot_mode='per_day', shadow_caps=True,
                   telegram_mode='entry_exit', enabled=False)

UNDERLYINGS = {
    'NIFTY': Underlying(
        name='NIFTY', exchange='NFO', ref_key='NSE:NIFTY 50', session_open=datetime.time(9, 15),
        session_close=nse_close, eod_offset_min=5, lot_fallback=65, tick_fallback=0.05,
        multiplier=None, fresh_min=2, max_spread_pct=None, cost_floor_rs=0.10,
        cost_floor_ticks=2, slot_mode='per_day', no_entry_windows=_NIFTY_NO_ENTRY,
        min_t1_distance=3.00, turbulence=True, orders_allowed=True, enabled=True, strike_step=50),
    'BANKNIFTY': Underlying(
        name='BANKNIFTY', exchange='NFO', ref_key='NSE:NIFTY BANK', session_open=datetime.time(9, 15),
        session_close=nse_close, eod_offset_min=5, lot_fallback=30, tick_fallback=0.05,
        multiplier=None, fresh_min=2, max_spread_pct=0.5, cost_floor_rs=None,
        cost_floor_ticks=2, slot_mode='per_day', shadow_caps=True, telegram_mode='entry_exit',
        enabled=False),
    'CRUDEOILM': Underlying(name='CRUDEOILM', lot_fallback=10, tick_fallback=0.05, multiplier=10,
                            fresh_min=2, max_spread_pct=0.5, events=('EIA_WPSR',),
                            quote_companion='CRUDEOIL', entry_from=_EVENING_ONLY,
                            **{**_COMMON_MCX, 'enabled': False}),
    'NATGASMINI': Underlying(name='NATGASMINI', lot_fallback=250, tick_fallback=0.05, multiplier=250,
                             fresh_min=3, max_spread_pct=1.0, events=('EIA_NGS',),
                             quote_companion='NATURALGAS', entry_from=_EVENING_ONLY,
                             **_COMMON_MCX),
    'GOLDM': Underlying(name='GOLDM', lot_fallback=10, tick_fallback=0.5, multiplier=10,
                        fresh_min=5, max_spread_pct=1.0, **{**_COMMON_MCX, 'slot_mode': 'per_half'}),
    'SILVERM': Underlying(name='SILVERM', lot_fallback=5, tick_fallback=0.5, multiplier=5,
                          fresh_min=5, max_spread_pct=1.2, **{**_COMMON_MCX, 'slot_mode': 'per_half'}),
}


def get(name):
    """Return the record for `name` (case-insensitive). Unknown names fail loudly."""
    key = (name or '').strip().upper()
    if key not in UNDERLYINGS:
        raise ValueError(f"unknown underlying {name!r}; known: {', '.join(UNDERLYINGS)}")
    return UNDERLYINGS[key]

"""
Configuration loader for ConvictBar strategy.
Loads YAML config and provides typed access to parameters.
"""

from dataclasses import dataclass
from datetime import time, date
from pathlib import Path
from typing import Optional
import yaml


@dataclass
class ConvictionParams:
    min_range_points: float
    max_range_points: float
    min_body_ratio: float
    min_close_position_bull: float
    max_close_position_bear: float


@dataclass
class FollowthroughParams:
    require_confirmation: bool
    min_confirmation_level: str
    trap_buffer: float


@dataclass
class EntryParams:
    entry_buffer: float
    order_valid_bars: int
    entry_window_start: time
    entry_window_end: time


@dataclass
class StopLossParams:
    sl_buffer: float
    min_sl_points: float
    max_sl_points: float
    skip_if_sl_exceeds_max: bool


@dataclass
class TargetParams:
    target_multiplier: float
    min_target_points: float


@dataclass
class TrailingParams:
    enable_trailing: bool
    breakeven_at_rr: float
    breakeven_buffer: float
    trail_at_rr: float
    update_on: str


@dataclass
class RiskParams:
    fixed_lots: int
    max_trades_per_day: int
    max_daily_loss_points: float
    max_consecutive_losses: int


@dataclass
class GapParams:
    small_gap_threshold: float
    large_gap_threshold: float
    skip_if_gap_large: bool


@dataclass
class Bar1FilterParams:
    enabled: bool
    max_bar1_range: float
    min_bar1_body_ratio: float


@dataclass
class TimeParams:
    market_open: time
    hard_close: time
    timeframe_minutes: int
    timezone: str


@dataclass
class SimulationParams:
    slippage_points: float
    commission_per_lot: float
    assume_sl_first: bool


@dataclass
class InstrumentParams:
    symbol: str
    instrument_token: int
    exchange: str
    lot_size: int
    tick_size: float


@dataclass
class StrategyConfig:
    """Complete strategy configuration."""
    instrument: InstrumentParams
    conviction: ConvictionParams
    followthrough: FollowthroughParams
    entry: EntryParams
    stop_loss: StopLossParams
    target: TargetParams
    trailing: TrailingParams
    risk: RiskParams
    gap: GapParams
    bar1_filter: Bar1FilterParams
    time: TimeParams
    simulation: SimulationParams


def _parse_time(time_str: str) -> time:
    """Parse HH:MM string to time object."""
    parts = time_str.split(":")
    return time(int(parts[0]), int(parts[1]))


def load_config(config_path: Optional[Path] = None) -> StrategyConfig:
    """
    Load configuration from YAML file.

    Args:
        config_path: Path to config file. If None, uses default.

    Returns:
        StrategyConfig object with all parameters.
    """
    if config_path is None:
        config_path = Path(__file__).parent.parent.parent / "config" / "params.yaml"

    with open(config_path, 'r') as f:
        raw = yaml.safe_load(f)

    return StrategyConfig(
        instrument=InstrumentParams(
            symbol=raw['instrument']['symbol'],
            instrument_token=raw['instrument']['instrument_token'],
            exchange=raw['instrument']['exchange'],
            lot_size=raw['instrument']['lot_size'],
            tick_size=raw['instrument']['tick_size'],
        ),
        conviction=ConvictionParams(
            min_range_points=raw['conviction']['min_range_points'],
            max_range_points=raw['conviction']['max_range_points'],
            min_body_ratio=raw['conviction']['min_body_ratio'],
            min_close_position_bull=raw['conviction']['min_close_position_bull'],
            max_close_position_bear=raw['conviction']['max_close_position_bear'],
        ),
        followthrough=FollowthroughParams(
            require_confirmation=raw['followthrough']['require_confirmation'],
            min_confirmation_level=raw['followthrough']['min_confirmation_level'],
            trap_buffer=raw['followthrough']['trap_buffer'],
        ),
        entry=EntryParams(
            entry_buffer=raw['entry']['entry_buffer'],
            order_valid_bars=raw['entry']['order_valid_bars'],
            entry_window_start=_parse_time(raw['entry']['entry_window_start']),
            entry_window_end=_parse_time(raw['entry']['entry_window_end']),
        ),
        stop_loss=StopLossParams(
            sl_buffer=raw['stop_loss']['sl_buffer'],
            min_sl_points=raw['stop_loss']['min_sl_points'],
            max_sl_points=raw['stop_loss']['max_sl_points'],
            skip_if_sl_exceeds_max=raw['stop_loss']['skip_if_sl_exceeds_max'],
        ),
        target=TargetParams(
            target_multiplier=raw['target']['target_multiplier'],
            min_target_points=raw['target']['min_target_points'],
        ),
        trailing=TrailingParams(
            enable_trailing=raw['trailing']['enable_trailing'],
            breakeven_at_rr=raw['trailing']['breakeven_at_rr'],
            breakeven_buffer=raw['trailing']['breakeven_buffer'],
            trail_at_rr=raw['trailing']['trail_at_rr'],
            update_on=raw['trailing']['update_on'],
        ),
        risk=RiskParams(
            fixed_lots=raw['risk']['fixed_lots'],
            max_trades_per_day=raw['risk']['max_trades_per_day'],
            max_daily_loss_points=raw['risk']['max_daily_loss_points'],
            max_consecutive_losses=raw['risk']['max_consecutive_losses'],
        ),
        gap=GapParams(
            small_gap_threshold=raw['gap']['small_gap_threshold'],
            large_gap_threshold=raw['gap']['large_gap_threshold'],
            skip_if_gap_large=raw['gap']['skip_if_gap_large'],
        ),
        bar1_filter=Bar1FilterParams(
            enabled=raw['bar1_filter']['enabled'],
            max_bar1_range=raw['bar1_filter']['max_bar1_range'],
            min_bar1_body_ratio=raw['bar1_filter']['min_bar1_body_ratio'],
        ),
        time=TimeParams(
            market_open=_parse_time(raw['time']['market_open']),
            hard_close=_parse_time(raw['time']['hard_close']),
            timeframe_minutes=raw['time']['timeframe_minutes'],
            timezone=raw['time']['timezone'],
        ),
        simulation=SimulationParams(
            slippage_points=raw['simulation']['slippage_points'],
            commission_per_lot=raw['simulation']['commission_per_lot'],
            assume_sl_first=raw['simulation']['assume_sl_first'],
        ),
    )

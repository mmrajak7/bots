"""
Stock Analyzer — Engine Layer

Pure computation modules that take structured data from sources and produce
scored analysis. NO I/O anywhere in this layer — every function is deterministic
and testable in isolation.

Modules:
  - fundamental_scores : Piotroski F-Score, PEG, DuPont, Altman Z, composite
  - technical_scores   : RSI, DMA, MACD, 52W position, volume scoring
  - composite_scorer   : Momentum, risk, valuation, and final verdict
"""

from .fundamental_scores import (
    piotroski_f_score,
    peg_ratio,
    dupont_analysis,
    altman_z_score,
    compute_fundamental_score,
)
from .technical_scores import (
    score_rsi,
    score_dma,
    score_macd,
    score_52w_position,
    score_volume,
    compute_technical_score,
)
from .composite_scorer import (
    compute_momentum_score,
    compute_risk_score,
    compute_valuation_score,
    compute_verdict,
)

__all__ = [
    # Fundamental
    'piotroski_f_score',
    'peg_ratio',
    'dupont_analysis',
    'altman_z_score',
    'compute_fundamental_score',
    # Technical
    'score_rsi',
    'score_dma',
    'score_macd',
    'score_52w_position',
    'score_volume',
    'compute_technical_score',
    # Composite
    'compute_momentum_score',
    'compute_risk_score',
    'compute_valuation_score',
    'compute_verdict',
]

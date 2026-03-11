"""
Composite scorer — final verdict engine, pure computation, zero I/O.

Takes pre-computed scores from fundamental_scores.py and technical_scores.py,
adds momentum and risk dimensions, and produces a final BUY/WAIT/EXIT/SELL
verdict with confidence score and reasoning.

Scoring dimensions (default weights from config):
  fundamental:  35%  — Piotroski, ROCE, ROE, growth, OPM
  valuation:    25%  — PEG, PE vs industry, DuPont quality
  technical:    20%  — RSI, DMA, MACD, 52W, volume
  momentum:     10%  — FII/DII/promoter shareholding trends
  risk:         10%  — Altman Z, D/E, pledging, governance

Hard overrides can force a SELL verdict regardless of composite score when
serious red flags are detected (financial distress, governance collapse).
"""

import logging
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _safe_float(val: Any, default: float = 0.0) -> float:
    """Convert to float, returning default on failure."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    """Clamp a value to [lo, hi]."""
    return max(lo, min(hi, value))


def _get_quarterly_shareholding(screener_data: dict,
                                min_quarters: int = 1) -> Optional[list]:
    """Extract quarterly shareholding data, most recent first."""
    sh = screener_data.get('shareholding')
    if not sh or len(sh) < min_quarters:
        return None
    return sh


# ── 5a. Momentum Score ──────────────────────────────────────────────────────

def compute_momentum_score(screener_data: dict,
                           trendlyne_data: Optional[dict] = None) -> dict:
    """Compute momentum score from institutional shareholding trends.

    Institutional money flow is a strong signal — sustained FII/DII buying
    over multiple quarters indicates conviction. Promoter changes signal
    insider sentiment.

    Scoring components:
      FII increasing 2+ quarters:     +25
      DII increasing 2+ quarters:     +15
      Promoter increasing:            +25
      Promoter decreasing >1%:        -20
      Promoter decreasing >3%:        -35

    If trendlyne_data is available, its momentum signal is blended at 30%.

    Args:
        screener_data: Dict with shareholding list (quarterly, recent first).
        trendlyne_data: Optional dict with 'momentum_score' (0-100) key.

    Returns:
        {"score": 0-100, "signals": list[str]}
    """
    signals: List[str] = []
    raw_score = 50.0  # Start neutral

    shareholding = _get_quarterly_shareholding(screener_data, min_quarters=2)

    if shareholding and len(shareholding) >= 2:
        latest = shareholding[0]
        prev = shareholding[1]

        # FII trend
        fii_curr = _safe_float(latest.get('fii_pct'))
        fii_prev = _safe_float(prev.get('fii_pct'))
        fii_change = fii_curr - fii_prev

        if len(shareholding) >= 3:
            fii_prev2 = _safe_float(shareholding[2].get('fii_pct'))
            # FII increasing 2+ consecutive quarters
            if fii_curr > fii_prev > fii_prev2:
                raw_score += 25
                signals.append(
                    f"FII increasing 2+ quarters: "
                    f"{fii_prev2:.1f}% -> {fii_prev:.1f}% -> {fii_curr:.1f}%"
                )
            elif fii_curr > fii_prev:
                raw_score += 10
                signals.append(f"FII increasing: {fii_prev:.1f}% -> {fii_curr:.1f}%")
            elif fii_curr < fii_prev < fii_prev2:
                raw_score -= 15
                signals.append(
                    f"FII decreasing 2+ quarters: "
                    f"{fii_prev2:.1f}% -> {fii_prev:.1f}% -> {fii_curr:.1f}%"
                )
        elif fii_change > 0.5:
            raw_score += 10
            signals.append(f"FII increasing: {fii_prev:.1f}% -> {fii_curr:.1f}%")
        elif fii_change < -0.5:
            raw_score -= 10
            signals.append(f"FII decreasing: {fii_prev:.1f}% -> {fii_curr:.1f}%")

        # DII trend
        dii_curr = _safe_float(latest.get('dii_pct'))
        dii_prev = _safe_float(prev.get('dii_pct'))

        if len(shareholding) >= 3:
            dii_prev2 = _safe_float(shareholding[2].get('dii_pct'))
            if dii_curr > dii_prev > dii_prev2:
                raw_score += 15
                signals.append(
                    f"DII increasing 2+ quarters: "
                    f"{dii_prev2:.1f}% -> {dii_prev:.1f}% -> {dii_curr:.1f}%"
                )
            elif dii_curr > dii_prev:
                raw_score += 5
                signals.append(f"DII increasing: {dii_prev:.1f}% -> {dii_curr:.1f}%")
        elif dii_curr > dii_prev:
            raw_score += 5
            signals.append(f"DII increasing: {dii_prev:.1f}% -> {dii_curr:.1f}%")

        # Promoter trend
        promo_curr = _safe_float(latest.get('promoter_pct'))
        promo_prev = _safe_float(prev.get('promoter_pct'))
        promo_change = promo_curr - promo_prev

        if promo_change > 0.1:
            raw_score += 25
            signals.append(
                f"Promoter increasing: {promo_prev:.1f}% -> {promo_curr:.1f}% "
                f"(+{promo_change:.1f}pp — bullish insider signal)"
            )
        elif promo_change < -3.0:
            raw_score -= 35
            signals.append(
                f"Promoter decreasing sharply: {promo_prev:.1f}% -> {promo_curr:.1f}% "
                f"({promo_change:.1f}pp — significant red flag)"
            )
        elif promo_change < -1.0:
            raw_score -= 20
            signals.append(
                f"Promoter decreasing: {promo_prev:.1f}% -> {promo_curr:.1f}% "
                f"({promo_change:.1f}pp — caution)"
            )
    else:
        signals.append("Shareholding data insufficient — momentum neutral")

    # Blend with Trendlyne momentum if available
    if trendlyne_data is not None:
        tl_momentum = trendlyne_data.get('momentum_score')
        if tl_momentum is not None:
            tl_score = _safe_float(tl_momentum, 50.0)
            # Blend: 70% shareholding-derived, 30% trendlyne
            raw_score = raw_score * 0.70 + tl_score * 0.30
            signals.append(f"Trendlyne momentum blended (score={tl_score:.0f})")

    return {
        'score': round(_clamp(raw_score), 1),
        'signals': signals,
    }


# ── 5b. Risk Score ──────────────────────────────────────────────────────────

def compute_risk_score(screener_data: dict, altman_z: dict) -> dict:
    """Compute risk score (0-100, where 100 = LOWEST risk).

    This is an inverted score — higher means safer. It penalizes for
    financial distress, high leverage, promoter pledging, low promoter
    holding, and declining margins.

    Components:
      Altman Z safe:         90 base, grey: 50, distress: 10
      D/E < 0.3:             +20
      D/E 0.3-0.5:           +10
      D/E > 1.0:             -15
      D/E > 1.5:             -30
      Promoter pledge >20%:  -25
      Promoter < 30%:        -15
      OPM declining 3+ years: -10

    Args:
        screener_data: Dict with ratios, shareholding, annual_results.
        altman_z: Pre-computed Altman Z-Score result dict.

    Returns:
        {"score": 0-100, "flags": list[str]}
    """
    flags: List[str] = []

    # Base score from Altman Z
    if altman_z.get('applicable', True):
        zone = altman_z.get('zone', 'N/A')
        if zone == 'Safe':
            raw_score = 90.0
        elif zone == 'Grey zone':
            raw_score = 50.0
            flags.append("Altman Z in grey zone — moderate financial risk")
        elif zone == 'Distress':
            raw_score = 10.0
            flags.append("Altman Z in distress zone — high bankruptcy risk")
        else:
            raw_score = 50.0  # N/A or missing
    else:
        # Financial sector — Z not applicable, start neutral
        raw_score = 60.0

    # Debt-to-equity
    ratios = screener_data.get('ratios') or {}
    de_ratio = ratios.get('debt_to_equity')

    if de_ratio is not None:
        de = _safe_float(de_ratio)
        if de < 0.3:
            raw_score += 20
        elif de < 0.5:
            raw_score += 10
        elif de > 1.5:
            raw_score -= 30
            flags.append(f"High debt-to-equity ratio ({de:.2f}) — leverage risk")
        elif de > 1.0:
            raw_score -= 15
            flags.append(f"Elevated debt-to-equity ratio ({de:.2f})")

    # Promoter pledge
    shareholding = _get_quarterly_shareholding(screener_data)
    if shareholding:
        latest_sh = shareholding[0]
        pledge_pct = _safe_float(latest_sh.get('pledged_pct'))
        promoter_pct = _safe_float(latest_sh.get('promoter_pct'))

        if pledge_pct > 20:
            raw_score -= 25
            flags.append(
                f"High promoter pledge ({pledge_pct:.1f}%) — governance/liquidity risk"
            )
        elif pledge_pct > 10:
            raw_score -= 10
            flags.append(f"Moderate promoter pledge ({pledge_pct:.1f}%)")

        if promoter_pct < 30:
            raw_score -= 15
            flags.append(
                f"Low promoter holding ({promoter_pct:.1f}%) — weak insider alignment"
            )

    # OPM declining 3+ years
    annual = screener_data.get('annual_results')
    if annual and len(annual) >= 4:
        opm_values = [_safe_float(row.get('opm_pct')) for row in annual[:4]]
        # Check if declining for 3 consecutive years (index 0=latest)
        if (opm_values[0] < opm_values[1] < opm_values[2] < opm_values[3]):
            raw_score -= 10
            flags.append(
                f"OPM declining 3+ years ({opm_values[3]:.1f}% -> {opm_values[0]:.1f}%)"
            )

    if not flags:
        flags.append("No significant risk flags detected")

    return {
        'score': round(_clamp(raw_score), 1),
        'flags': flags,
    }


# ── 5c. Valuation Score ────────────────────────────────────────────────────

def compute_valuation_score(pe: Optional[float], peg: Optional[dict],
                            industry_pe: Optional[float],
                            dupont: Optional[dict]) -> dict:
    """Compute valuation score from PEG, PE vs industry, and DuPont quality.

    This answers: is the stock cheap or expensive relative to its growth
    and peers?

    Components:
      PEG < 0.5:              95 (deeply undervalued)
      PEG 0.5-1.0:            80
      PEG 1.0-1.5:            60
      PEG 1.5-2.0:            40
      PEG > 2.0:              20
      PE < industry PE:       +10
      PE > industry PE * 1.5: -10
      DuPont margin-driven:   +10
      DuPont leverage-driven: -10
      PE negative (loss):     10 (floor)

    Args:
        pe: Price-to-earnings ratio. None or negative = loss-making.
        peg: Pre-computed PEG result dict (from peg_ratio()).
        industry_pe: Sector/industry average PE.
        dupont: Pre-computed DuPont analysis result dict.

    Returns:
        {"score": 0-100, "signals": list[str]}
    """
    signals: List[str] = []

    # Handle loss-making companies
    if pe is None or pe < 0:
        signals.append("Loss-making company — valuation metrics unreliable")
        return {
            'score': 10,
            'signals': signals,
        }

    # Base score from PEG
    peg_value = None
    if peg is not None:
        peg_value = peg.get('peg')

    if peg_value is not None:
        if peg_value < 0.5:
            raw_score = 95.0
            signals.append(f"PEG {peg_value:.2f} — deeply undervalued relative to growth")
        elif peg_value < 1.0:
            raw_score = 80.0
            signals.append(f"PEG {peg_value:.2f} — undervalued relative to growth")
        elif peg_value < 1.5:
            raw_score = 60.0
            signals.append(f"PEG {peg_value:.2f} — fairly valued")
        elif peg_value < 2.0:
            raw_score = 40.0
            signals.append(f"PEG {peg_value:.2f} — moderately expensive")
        else:
            raw_score = 20.0
            signals.append(f"PEG {peg_value:.2f} — expensive relative to growth")
    else:
        # PEG not computable (negative growth or missing data)
        # Fall back to PE-based scoring
        if pe < 15:
            raw_score = 75.0
            signals.append(f"PE {pe:.1f} — low absolute PE (PEG unavailable)")
        elif pe < 25:
            raw_score = 55.0
            signals.append(f"PE {pe:.1f} — moderate (PEG unavailable)")
        elif pe < 40:
            raw_score = 35.0
            signals.append(f"PE {pe:.1f} — expensive (PEG unavailable)")
        else:
            raw_score = 20.0
            signals.append(f"PE {pe:.1f} — very expensive (PEG unavailable)")

    # PE vs industry
    if industry_pe is not None and industry_pe > 0:
        pe_val = _safe_float(pe)
        ind_pe = _safe_float(industry_pe)
        if pe_val < ind_pe:
            raw_score += 10
            signals.append(
                f"PE ({pe_val:.1f}) below industry ({ind_pe:.1f}) — relatively cheap"
            )
        elif pe_val > ind_pe * 1.5:
            raw_score -= 10
            signals.append(
                f"PE ({pe_val:.1f}) significantly above industry ({ind_pe:.1f}) — premium valuation"
            )
        else:
            signals.append(f"PE ({pe_val:.1f}) near industry average ({ind_pe:.1f})")

    # DuPont quality adjustment
    if dupont is not None:
        roe_driver = dupont.get('roe_driver', '')
        if 'Margin-driven' in roe_driver:
            raw_score += 10
            signals.append("DuPont: margin-driven ROE (high quality earnings)")
        elif 'Leverage-driven' in roe_driver:
            raw_score -= 10
            signals.append("DuPont: leverage-driven ROE (risky — not sustainable)")
        elif 'Efficiency-driven' in roe_driver:
            signals.append("DuPont: efficiency-driven ROE (good operational quality)")

    return {
        'score': round(_clamp(raw_score), 1),
        'signals': signals,
    }


# ── 5d. Final Verdict ──────────────────────────────────────────────────────

def compute_verdict(fundamental_score: float, valuation_score: float,
                    technical_score: float, momentum_score: float,
                    risk_score: float, config: dict,
                    screener_data: Optional[dict] = None,
                    altman_z: Optional[dict] = None) -> dict:
    """Compute the final BUY/WAIT/EXIT/SELL verdict.

    Uses weighted composite of all five dimensions, with hard overrides for
    extreme red flags that should force a SELL regardless of score.

    Hard overrides (checked first):
      - Altman Z < 1.81              -> SELL (financial distress)
      - Promoter pledge > 50%        -> SELL (governance risk)
      - Promoter < 15% and declining -> SELL (no skin in the game)
      - Loss-making 2+ years with increasing debt -> SELL

    Score-based verdicts:
      composite >= 70, valuation >= 50 -> BUY
      composite >= 70, valuation < 50  -> WAIT (good business, expensive)
      composite 50-70, valuation >= 60 -> BUY (value play)
      composite 50-70                  -> WAIT
      composite 35-50                  -> EXIT/AVOID
      composite < 35                   -> SELL

    Args:
        fundamental_score: 0-100 fundamental score.
        valuation_score: 0-100 valuation score.
        technical_score: 0-100 technical score.
        momentum_score: 0-100 momentum score.
        risk_score: 0-100 risk score (100 = lowest risk).
        config: Config dict with scoring_weights and thresholds.
        screener_data: Optional full screener data for hard override checks.
        altman_z: Optional pre-computed Altman Z result for hard override.

    Returns:
        {
            "verdict": "BUY"|"WAIT"|"EXIT"|"SELL",
            "confidence": 0-100,
            "composite_score": float,
            "scores": {dimension: {"score", "weight", "weighted"}},
            "reasons": [str],
            "flags": [str],
            "hard_override": None|str
        }
    """
    # ── Resolve weights ──────────────────────────────────────────────────

    weights = config.get('scoring_weights', {})
    w_fund = weights.get('fundamental', 0.35)
    w_val = weights.get('valuation', 0.25)
    w_tech = weights.get('technical', 0.20)
    w_mom = weights.get('momentum', 0.10)
    w_risk = weights.get('risk', 0.10)

    thresholds = config.get('thresholds', {})
    buy_min = thresholds.get('verdict_buy_min', 70)
    wait_min = thresholds.get('verdict_wait_min', 50)
    sell_max = thresholds.get('verdict_sell_max', 35)

    # ── Compute weighted composite ───────────────────────────────────────

    composite = (
        fundamental_score * w_fund +
        valuation_score * w_val +
        technical_score * w_tech +
        momentum_score * w_mom +
        risk_score * w_risk
    )

    scores = {
        'fundamental': {
            'score': round(fundamental_score, 1),
            'weight': w_fund,
            'weighted': round(fundamental_score * w_fund, 2),
        },
        'valuation': {
            'score': round(valuation_score, 1),
            'weight': w_val,
            'weighted': round(valuation_score * w_val, 2),
        },
        'technical': {
            'score': round(technical_score, 1),
            'weight': w_tech,
            'weighted': round(technical_score * w_tech, 2),
        },
        'momentum': {
            'score': round(momentum_score, 1),
            'weight': w_mom,
            'weighted': round(momentum_score * w_mom, 2),
        },
        'risk': {
            'score': round(risk_score, 1),
            'weight': w_risk,
            'weighted': round(risk_score * w_risk, 2),
        },
    }

    reasons: List[str] = []
    flags: List[str] = []
    hard_override: Optional[str] = None

    # ── Hard overrides ───────────────────────────────────────────────────

    # 1. Altman Z distress
    if altman_z is not None and altman_z.get('applicable', True):
        z_score = altman_z.get('z_score')
        altman_distress = thresholds.get('altman_distress', 1.81)
        if z_score is not None and z_score < altman_distress:
            hard_override = (
                f"Financial distress — Altman Z-Score {z_score:.2f} "
                f"below {altman_distress} threshold"
            )
            flags.append(hard_override)

    # 2-4. Governance/structural overrides (need screener_data)
    if screener_data is not None:
        shareholding = _get_quarterly_shareholding(screener_data, min_quarters=2)

        if shareholding:
            latest_sh = shareholding[0]
            prev_sh = shareholding[1] if len(shareholding) >= 2 else {}

            pledge_pct = _safe_float(latest_sh.get('pledged_pct'))
            promoter_pct = _safe_float(latest_sh.get('promoter_pct'))
            prev_promoter = _safe_float(prev_sh.get('promoter_pct'))
            promoter_declining = promoter_pct < prev_promoter

            # Promoter pledge > 50%
            if pledge_pct > 50 and hard_override is None:
                hard_override = (
                    f"Governance risk — promoter pledge {pledge_pct:.1f}% "
                    f"exceeds 50% threshold"
                )
                flags.append(hard_override)

            # Promoter < 15% and declining
            if promoter_pct < 15 and promoter_declining and hard_override is None:
                hard_override = (
                    f"No skin in the game — promoter holding {promoter_pct:.1f}% "
                    f"(below 15%) and declining"
                )
                flags.append(hard_override)

        # Loss-making 2+ years with increasing debt
        annual = screener_data.get('annual_results')
        balance = screener_data.get('balance_sheet')
        if annual and len(annual) >= 2 and balance and len(balance) >= 2:
            profit_y0 = _safe_float(annual[0].get('net_profit'))
            profit_y1 = _safe_float(annual[1].get('net_profit'))
            borrow_y0 = _safe_float(balance[0].get('borrowings'))
            borrow_y1 = _safe_float(balance[1].get('borrowings'))

            if (profit_y0 < 0 and profit_y1 < 0 and
                    borrow_y0 > borrow_y1 and borrow_y1 > 0):
                if hard_override is None:
                    hard_override = (
                        "Structural deterioration — loss-making for 2+ years "
                        "with increasing debt"
                    )
                flags.append(
                    "Loss-making 2+ consecutive years with increasing borrowings"
                )

    # ── Determine verdict ────────────────────────────────────────────────

    if hard_override is not None:
        verdict = "SELL"
        confidence = 90.0
        reasons.append(f"HARD OVERRIDE: {hard_override}")
    elif composite >= buy_min and valuation_score >= wait_min:
        verdict = "BUY"
        confidence = min(95, composite)
        reasons.append(
            f"Strong composite ({composite:.0f}) with reasonable valuation "
            f"({valuation_score:.0f})"
        )
    elif composite >= buy_min and valuation_score < wait_min:
        verdict = "WAIT"
        confidence = 65.0
        reasons.append(
            f"Good business (composite {composite:.0f}) but expensive "
            f"(valuation {valuation_score:.0f} < {wait_min})"
        )
    elif wait_min <= composite < buy_min and valuation_score >= 60:
        verdict = "BUY"
        confidence = min(70, composite)
        reasons.append(
            f"Value play — moderate composite ({composite:.0f}) but "
            f"attractive valuation ({valuation_score:.0f})"
        )
    elif wait_min <= composite < buy_min:
        verdict = "WAIT"
        confidence = 50.0
        reasons.append(
            f"Average composite ({composite:.0f}) — neither compelling "
            f"nor alarming"
        )
    elif sell_max <= composite < wait_min:
        verdict = "EXIT"
        confidence = 60.0
        reasons.append(
            f"Below average composite ({composite:.0f}) — EXIT if holding, "
            f"AVOID if not"
        )
    else:
        verdict = "SELL"
        confidence = 75.0
        reasons.append(
            f"Weak composite ({composite:.0f}) across multiple dimensions"
        )

    # ── Add dimension-specific reasons ───────────────────────────────────

    # Identify strongest and weakest dimensions
    dim_scores = {
        'Fundamental': fundamental_score,
        'Valuation': valuation_score,
        'Technical': technical_score,
        'Momentum': momentum_score,
        'Risk': risk_score,
    }

    strongest = max(dim_scores, key=dim_scores.get)  # type: ignore[arg-type]
    weakest = min(dim_scores, key=dim_scores.get)  # type: ignore[arg-type]

    reasons.append(
        f"Strongest dimension: {strongest} ({dim_scores[strongest]:.0f}/100)"
    )
    reasons.append(
        f"Weakest dimension: {weakest} ({dim_scores[weakest]:.0f}/100)"
    )

    # Divergence warnings
    if fundamental_score >= 70 and technical_score < 40:
        flags.append(
            "Divergence: strong fundamentals but weak technicals — "
            "may need catalyst or patience"
        )
    if technical_score >= 70 and fundamental_score < 40:
        flags.append(
            "Divergence: strong technicals but weak fundamentals — "
            "momentum trade, not investment"
        )
    if risk_score < 30:
        flags.append("Elevated overall risk profile")
    if momentum_score > 75:
        reasons.append("Strong institutional momentum supports the thesis")
    elif momentum_score < 30:
        flags.append("Weak institutional momentum — smart money is not buying")

    # Adjust confidence for extreme divergences
    score_values = list(dim_scores.values())
    spread = max(score_values) - min(score_values)
    if spread > 50:
        confidence = max(30, confidence - 15)
        flags.append(
            f"High score divergence ({spread:.0f}pt spread) — "
            f"conflicting signals reduce conviction"
        )

    return {
        'verdict': verdict,
        'confidence': round(_clamp(confidence), 1),
        'composite_score': round(composite, 2),
        'scores': scores,
        'reasons': reasons,
        'flags': flags,
        'hard_override': hard_override,
    }

"""
Ensemble Signal Scorer
Combines multiple weak signals into a composite trade score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from config import EnsembleConfig
from analysis import DigitTracker, VolatilityTracker
from regime import Regime, RegimeDetector


@dataclass
class SignalSnapshot:
    """All signal components at a point in time."""
    digit_bias: float       # |P_even - 0.5|
    chi_sq_significant: bool
    entropy: float          # digit entropy (lower = more biased)
    momentum: float         # sum of recent returns
    regime: Regime
    composite_score: float  # final weighted score
    direction: str          # "EVEN" or "ODD"


class EnsembleScorer:
    """Compute a composite trade-entry score from multiple signals."""

    MAX_ENTROPY = math.log2(10)  # ~3.32 for uniform over 10 digits

    def __init__(self, cfg: EnsembleConfig):
        self.cfg = cfg

    def score(
        self,
        digit: DigitTracker,
        vol: VolatilityTracker,
        regime_det: RegimeDetector,
    ) -> Optional[SignalSnapshot]:
        """Return a SignalSnapshot or None if insufficient data."""
        if len(digit.window) < 20:  # increased from 10 for more reliable stats
            return None

        # ── Hard gates: reject weak signals before scoring ──
        bias = digit.bias_magnitude
        regime = regime_det.current_regime

        # Gate 1: minimum bias (no point scoring if bias is tiny)
        if bias < 0.03:
            return None

        # Gate 2: heavily penalize choppy regime (but don't block entirely)
        if regime == Regime.CHOPPY and bias < 0.08:
            return None  # only block weak signals in choppy

        # ── 1. Digit bias (primary signal) ──
        norm_bias = min(bias / 0.15, 1.0)

        # ── 2. Chi-square significance ──
        _, p_val = digit.chi_square_test()
        # Graduated: strong significance = 1.0, marginal = 0.5, none = 0.0
        if p_val < 0.01:
            chi_sig = 1.0
        elif p_val < digit.cfg.chi_sq_alpha:
            chi_sig = 0.6
        else:
            chi_sig = 0.0

        # ── 3. Entropy ──
        ent = digit.digit_entropy()
        norm_entropy = max(0, 1.0 - ent / self.MAX_ENTROPY)

        # ── 4. Recent-window bias confirmation ──
        # Check if the bias is consistent in the most recent 15 ticks
        # (confirms trend isn't stale / about to flip)
        recent = list(digit.window)[-15:]
        recent_p_even = sum(1 for d in recent if d % 2 == 0) / len(recent) if recent else 0.5
        recent_bias = abs(recent_p_even - 0.5)
        norm_recent = min(recent_bias / 0.15, 1.0)

        # ── 5. Regime bonus ──
        if regime == Regime.MEAN_REVERTING:
            regime_score = 1.0
        elif regime == Regime.TRENDING:
            regime_score = 0.5
        else:
            regime_score = 0.3  # UNKNOWN

        # ── Weighted combination ──
        # Replaced momentum (irrelevant to digit outcomes) with recent-window confirmation
        w = self.cfg
        composite = (
            w.weight_digit_bias * norm_bias
            + w.weight_chi_sq * chi_sig
            + w.weight_entropy * norm_entropy
            + w.weight_momentum * norm_recent    # reuse momentum weight for recent bias
            + w.weight_regime * regime_score
        )

        # ── Direction: follow the dominant side (trend-following on digits) ──
        # In mean-reverting regime, go contrarian (the over-represented side
        # is expected to correct), otherwise follow the majority.
        p_even = digit.p_even
        if regime == Regime.MEAN_REVERTING:
            # Contrarian: if even is dominant, bet ODD (expect reversion)
            direction = "ODD" if p_even >= 0.5 else "EVEN"
        else:
            # Trend-following: bet the dominant side
            direction = "EVEN" if p_even >= 0.5 else "ODD"

        return SignalSnapshot(
            digit_bias=bias,
            chi_sq_significant=bool(chi_sig >= 0.6),
            entropy=ent,
            momentum=0.0,
            regime=regime,
            composite_score=composite,
            direction=direction,
        )

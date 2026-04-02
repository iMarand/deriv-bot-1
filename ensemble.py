"""
Ensemble Signal Scorer
Combines multiple signals into a composite trade score.

Key improvements:
  - Multi-timeframe: checks both full window AND recent 15-tick window agree
  - Direction uses recent bias (not stale full-window bias)
  - Removed irrelevant signals (price momentum has zero correlation with digits)
  - Fewer hard gates — let the score decide, not arbitrary blocks
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
        if len(digit.window) < 20:
            return None

        bias = digit.bias_magnitude
        regime = regime_det.current_regime

        # Only hard gate: need some bias to work with
        if bias < 0.02:
            return None

        # ── 1. Full-window digit bias (weight: 0.30) ──
        # How far even/odd is from 50/50 over the full window
        norm_bias = min(bias / 0.15, 1.0)

        # ── 2. Recent-window bias (weight: 0.30) ──
        # Does the RECENT data confirm the full-window bias?
        # This is the key intelligence — full window could be stale
        recent = list(digit.window)[-15:]
        recent_even = sum(1 for d in recent if d % 2 == 0) / len(recent)
        recent_bias = abs(recent_even - 0.5)
        norm_recent = min(recent_bias / 0.15, 1.0)

        # ── 3. Agreement bonus (weight: 0.20) ──
        # Do full window and recent window agree on direction?
        full_dir = "EVEN" if digit.p_even >= 0.5 else "ODD"
        recent_dir = "EVEN" if recent_even >= 0.5 else "ODD"
        agreement = 1.0 if full_dir == recent_dir else 0.0

        # ── 4. Chi-square significance (weight: 0.10) ──
        _, p_val = digit.chi_square_test()
        if p_val < 0.01:
            chi_sig = 1.0
        elif p_val < digit.cfg.chi_sq_alpha:
            chi_sig = 0.6
        else:
            chi_sig = 0.0

        # ── 5. Low entropy bonus (weight: 0.10) ──
        ent = digit.digit_entropy()
        norm_entropy = max(0, 1.0 - ent / self.MAX_ENTROPY)

        # ── Weighted combination ──
        composite = (
            0.30 * norm_bias
            + 0.30 * norm_recent
            + 0.20 * agreement
            + 0.10 * chi_sig
            + 0.10 * norm_entropy
        )

        # ── Direction: use the RECENT window (more responsive) ──
        # If recent and full agree → high confidence
        # If they disagree → the composite score will be low anyway (agreement=0)
        direction = recent_dir

        return SignalSnapshot(
            digit_bias=bias,
            chi_sq_significant=bool(chi_sig >= 0.6),
            entropy=ent,
            momentum=0.0,
            regime=regime,
            composite_score=composite,
            direction=direction,
        )

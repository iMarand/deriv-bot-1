"""
Ensemble Signal Scorer
Combines multiple signals into a composite trade score.

Key improvements (v2):
  - Multi-timeframe: checks full window, recent 15-tick window, AND micro 7-tick window
  - Recency-weighted bias: exponentially decays older observations
  - Streak momentum: consecutive same-direction results boost confidence
  - trade_strategy / barrier fields on SignalSnapshot for multi-contract support
  - Direction uses recent bias (not stale full-window bias)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
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
    direction: str          # "EVEN" or "ODD" (or "RISE"/"FALL" for price strategies)
    # ── New fields for multi-strategy support ──
    trade_strategy: str = "even_odd"    # which trade strategy produced this signal
    barrier: Optional[float] = None     # optional barrier for higher/lower, touch/notouch
    digit_barrier: Optional[int] = None # optional digit barrier for over/under (0-9)


class EnsembleScorer:
    """Compute a composite trade-entry score from multiple signals.

    Improvements over v1:
    - 3-timeframe confirmation (full + recent + micro)
    - Exponential recency weighting on bias calculation
    - Streak momentum tracking
    """

    MAX_ENTROPY = math.log2(10)  # ~3.32 for uniform over 10 digits

    def __init__(self, cfg: EnsembleConfig):
        self.cfg = cfg
        self._streak_dir: Optional[str] = None
        self._streak_len: int = 0

    def record_result(self, direction: str, is_win: bool) -> None:
        """Track streak momentum from trade outcomes."""
        if is_win and direction == self._streak_dir:
            self._streak_len += 1
        elif is_win:
            self._streak_dir = direction
            self._streak_len = 1
        else:
            self._streak_len = max(0, self._streak_len - 1)
            if self._streak_len == 0:
                self._streak_dir = None

    def _recency_weighted_bias(self, digits: list[int], halflife: int = 12) -> float:
        """Compute even/odd bias with exponential decay weighting.

        More recent ticks have exponentially higher weight.
        """
        n = len(digits)
        if n == 0:
            return 0.0

        weights = []
        values = []
        for i, d in enumerate(digits):
            w = math.exp(-(n - 1 - i) / halflife)
            weights.append(w)
            values.append(1.0 if d % 2 == 0 else 0.0)

        total_weight = sum(weights)
        if total_weight < 1e-10:
            return 0.0

        weighted_even = sum(w * v for w, v in zip(weights, values)) / total_weight
        return abs(weighted_even - 0.5)

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

        # ── 1. Full-window digit bias (weight: 0.25) ──
        norm_bias = min(bias / 0.15, 1.0)

        # ── 2. Recent-window bias (weight: 0.25) ──
        recent = list(digit.window)[-15:]
        recent_even = sum(1 for d in recent if d % 2 == 0) / len(recent)
        recent_bias = abs(recent_even - 0.5)
        norm_recent = min(recent_bias / 0.15, 1.0)

        # ── 3. Micro-window bias — 7 ticks (weight: 0.10) ──
        micro = list(digit.window)[-7:]
        micro_even = sum(1 for d in micro if d % 2 == 0) / len(micro)
        micro_bias = abs(micro_even - 0.5)
        norm_micro = min(micro_bias / 0.20, 1.0)

        # ── 4. Agreement bonus (weight: 0.15) ──
        full_dir = "EVEN" if digit.p_even >= 0.5 else "ODD"
        recent_dir = "EVEN" if recent_even >= 0.5 else "ODD"
        micro_dir = "EVEN" if micro_even >= 0.5 else "ODD"

        # All three timeframes agree = 1.0, two agree = 0.5, none = 0.0
        dirs = [full_dir, recent_dir, micro_dir]
        even_votes = sum(1 for d in dirs if d == "EVEN")
        agreement = 1.0 if even_votes >= 3 or even_votes <= 0 else 0.5

        # ── 5. Chi-square significance (weight: 0.08) ──
        _, p_val = digit.chi_square_test()
        if p_val < 0.01:
            chi_sig = 1.0
        elif p_val < digit.cfg.chi_sq_alpha:
            chi_sig = 0.6
        else:
            chi_sig = 0.0

        # ── 6. Low entropy bonus (weight: 0.07) ──
        ent = digit.digit_entropy()
        norm_entropy = max(0, 1.0 - ent / self.MAX_ENTROPY)

        # ── 7. Recency-weighted bias (weight: 0.05) ──
        recency_bias = self._recency_weighted_bias(list(digit.window))
        norm_recency = min(recency_bias / 0.12, 1.0)

        # ── 8. Streak momentum bonus (weight: 0.05) ──
        streak_bonus = 0.0
        if self._streak_dir is not None and self._streak_len >= 2:
            if self._streak_dir == recent_dir:
                streak_bonus = min(self._streak_len / 6.0, 1.0)

        # ── Weighted combination ──
        composite = (
            0.25 * norm_bias
            + 0.25 * norm_recent
            + 0.10 * norm_micro
            + 0.15 * agreement
            + 0.08 * chi_sig
            + 0.07 * norm_entropy
            + 0.05 * norm_recency
            + 0.05 * streak_bonus
        )

        # ── Direction: use the RECENT window (more responsive) ──
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

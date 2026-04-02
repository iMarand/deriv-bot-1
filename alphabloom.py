"""
AlphaBloom Digit Frequency Strategy
Simple even/odd frequency analysis over recent ticks.
Trades the dominant side when imbalance exceeds a threshold.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional

from ensemble import SignalSnapshot
from regime import Regime, RegimeDetector


@dataclass
class AlphaBloomConfig:
    """AlphaBloom strategy parameters."""
    window_size: int = 60          # number of recent ticks to analyse
    imbalance_threshold: float = 0.55  # min P(even) or P(odd) to trigger a trade
    cooldown_ticks: int = 3        # ticks to wait after a trade before next signal
    streak_confirm: int = 3        # consecutive ticks the dominant side must lead


class AlphaBloomScorer:
    """
    Digit-frequency strategy inspired by the AlphaBloom approach.

    Algorithm:
      1. Collect last N price ticks, extract last digit of each.
      2. Count even (0,2,4,6,8) vs odd (1,3,5,7,9) digits.
      3. Compute percentages.
      4. If one side exceeds the imbalance threshold -> trade that side.
      5. Optional streak confirmation: the dominant side must have led
         for `streak_confirm` consecutive ticks before triggering.
    """

    def __init__(self, cfg: AlphaBloomConfig):
        self.cfg = cfg
        self._digits: deque[int] = deque(maxlen=cfg.window_size)
        self._cooldown: int = 0
        self._streak_count: int = 0
        self._last_dominant: Optional[str] = None

    def update(self, quote: str) -> None:
        """Feed a new tick quote string. Extracts and stores the last digit."""
        digit = self._last_digit(quote)
        self._digits.append(digit)
        if self._cooldown > 0:
            self._cooldown -= 1

        # Track streak of consistent dominance
        dominant = self._current_dominant()
        if dominant is not None and dominant == self._last_dominant:
            self._streak_count += 1
        else:
            self._streak_count = 1
        self._last_dominant = dominant

    @staticmethod
    def _last_digit(quote: str) -> int:
        digits_only = "".join(ch for ch in quote if ch.isdigit())
        return int(digits_only[-1]) if digits_only else 0

    @property
    def even_count(self) -> int:
        return sum(1 for d in self._digits if d % 2 == 0)

    @property
    def odd_count(self) -> int:
        return len(self._digits) - self.even_count

    @property
    def p_even(self) -> float:
        n = len(self._digits)
        if n == 0:
            return 0.5
        return self.even_count / n

    @property
    def p_odd(self) -> float:
        return 1.0 - self.p_even

    def _current_dominant(self) -> Optional[str]:
        """Return the currently dominant side, or None if no clear imbalance."""
        if len(self._digits) < 20:
            return None
        if self.p_even >= self.cfg.imbalance_threshold:
            return "EVEN"
        if self.p_odd >= self.cfg.imbalance_threshold:
            return "ODD"
        return None

    def score(self, regime_det: Optional[RegimeDetector] = None) -> Optional[SignalSnapshot]:
        """
        Return a SignalSnapshot if conditions are met, else None.

        The composite_score is the imbalance magnitude (0.5 to 1.0 mapped to 0.0 to 1.0)
        so it integrates with the existing threshold system.
        """
        n = len(self._digits)
        if n < 20:
            return None

        if self._cooldown > 0:
            return None

        dominant = self._current_dominant()
        if dominant is None:
            return None

        # Streak confirmation: dominant side must hold for N consecutive ticks
        if self._streak_count < self.cfg.streak_confirm:
            return None

        # Compute score as the imbalance strength (0.0 to 1.0)
        p_dominant = self.p_even if dominant == "EVEN" else self.p_odd
        # Map [threshold, 1.0] -> [0.5, 1.0] for composite score
        imbalance = p_dominant - 0.5  # 0.0 to 0.5
        composite = min(imbalance / 0.25, 1.0)  # normalise: 0.25 imbalance = score 1.0

        regime = Regime.UNKNOWN
        if regime_det is not None:
            regime = regime_det.current_regime

        return SignalSnapshot(
            digit_bias=imbalance,
            chi_sq_significant=False,
            entropy=0.0,
            momentum=0.0,
            regime=regime,
            composite_score=composite,
            direction=dominant,
        )

    def on_trade_placed(self) -> None:
        """Call after a trade is placed to activate cooldown."""
        self._cooldown = self.cfg.cooldown_ticks

    def reset(self) -> None:
        """Reset state (e.g. when switching symbols)."""
        self._digits.clear()
        self._cooldown = 0
        self._streak_count = 0
        self._last_dominant = None

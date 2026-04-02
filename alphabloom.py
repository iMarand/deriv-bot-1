"""
AlphaBloom Digit Frequency Strategy

Trade the dominant side (EVEN or ODD) when it has a clear edge:
  - Even ≥ 55% → trade EVEN
  - Odd  ≥ 55% → trade ODD
  - 50-55% either side + rising trend → trade that side
  - No clear winner → wait
Scans all indices, picks the strongest imbalance.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from ensemble import SignalSnapshot
from regime import Regime, RegimeDetector

logger = logging.getLogger("alphabloom")


class AlphaBloomScorer:
    """
    Per-symbol digit frequency analyser.
    Trades BOTH sides — whichever is dominant.
    """

    def __init__(self, window_size: int = 60, imbalance_threshold: float = 0.55,
                 cooldown_ticks: int = 2, trend_window: int = 8):
        self.window_size = window_size
        self.imbalance_threshold = imbalance_threshold
        self.cooldown_ticks = cooldown_ticks
        self.trend_window = trend_window

        self._digits: deque[int] = deque(maxlen=window_size)
        self._cooldown: int = 0
        self._pct_history: deque[float] = deque(maxlen=200)

    def update(self, quote: str) -> None:
        digit = self._last_digit(quote)
        self._digits.append(digit)
        if self._cooldown > 0:
            self._cooldown -= 1
        if len(self._digits) >= 10:
            self._pct_history.append(self.p_even)

    @staticmethod
    def _last_digit(quote: str) -> int:
        digits_only = "".join(ch for ch in quote if ch.isdigit())
        return int(digits_only[-1]) if digits_only else 0

    @property
    def p_even(self) -> float:
        n = len(self._digits)
        if n == 0:
            return 0.5
        return sum(1 for d in self._digits if d % 2 == 0) / n

    @property
    def p_odd(self) -> float:
        return 1.0 - self.p_even

    @property
    def dominant_pct(self) -> float:
        """The higher of Even% or Odd%."""
        return max(self.p_even, self.p_odd)

    @property
    def trend(self) -> float:
        """Change in Even% over trend window. Positive = Even rising, Negative = Odd rising."""
        h = self._pct_history
        tw = self.trend_window
        if len(h) < tw:
            return 0.0
        return h[-1] - h[-tw]

    @property
    def warmed(self) -> bool:
        return len(self._digits) >= 20

    def zone(self) -> str:
        """Current zone: GREEN (strong), MOMENTUM (weak+trending), or WAIT."""
        if self.dominant_pct >= self.imbalance_threshold:
            return "GREEN"
        elif self.dominant_pct >= 0.50:
            # Check if the dominant side is trending stronger
            if self.p_even > 0.5 and self.trend > 0.005:
                return "MOMENTUM"
            if self.p_odd > 0.5 and self.trend < -0.005:
                return "MOMENTUM"
            return "WAIT"
        return "WAIT"

    @property
    def direction(self) -> str:
        return "EVEN" if self.p_even >= self.p_odd else "ODD"

    def score(self, regime_det: Optional[RegimeDetector] = None) -> Optional[SignalSnapshot]:
        if not self.warmed or self._cooldown > 0:
            return None

        z = self.zone()
        if z == "WAIT":
            return None

        dom = self.dominant_pct
        imbalance = dom - 0.5

        if z == "GREEN":
            composite = 0.5 + min(imbalance / 0.20, 0.5)
        else:
            t = abs(self.trend)
            composite = 0.3 + min(t / 0.05, 0.3)

        regime = Regime.UNKNOWN
        if regime_det is not None:
            regime = regime_det.current_regime

        return SignalSnapshot(
            digit_bias=imbalance,
            chi_sq_significant=False,
            entropy=0.0,
            momentum=self.trend,
            regime=regime,
            composite_score=composite,
            direction=self.direction,
        )

    def on_trade_placed(self) -> None:
        self._cooldown = self.cooldown_ticks

    def reset(self) -> None:
        self._digits.clear()
        self._pct_history.clear()
        self._cooldown = 0

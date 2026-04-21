"""
Pulse Strategy — Improved v2

Three windows, one decision:
  MICRO window (7 ticks)  — ultra-fast reaction to immediate shifts
  FAST window (15 ticks)  — reacts quickly to shifts
  SLOW window (50 ticks)  — confirms the overall trend

Rules:
  1. Fast + Slow must agree on direction → baseline signal
  2. Micro window alignment bonus → boosts confidence when all three agree
  3. Fast window has ≥ 53% on the dominant side → signal is valid
  4. Momentum confirmation: fast window's dominant% must be rising or stable
  5. Trades BOTH even and odd — follows whichever side is winning

Improvements over v1:
  - Added micro (7-tick) third timeframe for faster reaction
  - Added momentum confirmation — only trades when bias is stable or growing
  - Better score calculation with micro-window agreement boost
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

from ensemble import SignalSnapshot
from regime import Regime, RegimeDetector

logger = logging.getLogger("pulse")


class PulseScorer:
    """
    Per-symbol tri-timeframe digit analyser.

    Micro (7 ticks) + Fast (15 ticks) + Slow (50 ticks).
    Fast + Slow must agree on direction; micro provides a confidence boost.
    """

    def __init__(
        self,
        fast_window: int = 15,
        slow_window: int = 50,
        micro_window: int = 7,
        min_fast_pct: float = 0.53,
        cooldown_ticks: int = 1,
    ):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.micro_window = micro_window
        self.min_fast_pct = min_fast_pct
        self.cooldown_ticks = cooldown_ticks

        self._digits: deque[int] = deque(maxlen=slow_window)
        self._cooldown: int = 0
        # Momentum tracking: recent fast_even values
        self._fast_even_history: deque[float] = deque(maxlen=10)

    def update(self, quote: str) -> None:
        digit = self._last_digit(quote)
        self._digits.append(digit)
        if self._cooldown > 0:
            self._cooldown -= 1
        # Track fast even % for momentum check
        if len(self._digits) >= self.fast_window:
            self._fast_even_history.append(self.fast_even)

    @staticmethod
    def _last_digit(quote: str) -> int:
        digits_only = "".join(ch for ch in quote if ch.isdigit())
        return int(digits_only[-1]) if digits_only else 0

    @property
    def warmed(self) -> bool:
        return len(self._digits) >= self.slow_window

    def _even_pct(self, digits) -> float:
        n = len(digits)
        if n == 0:
            return 0.5
        return sum(1 for d in digits if d % 2 == 0) / n

    @property
    def micro_even(self) -> float:
        return self._even_pct(list(self._digits)[-self.micro_window:])

    @property
    def fast_even(self) -> float:
        return self._even_pct(list(self._digits)[-self.fast_window:])

    @property
    def slow_even(self) -> float:
        return self._even_pct(self._digits)

    @property
    def micro_dir(self) -> str:
        return "EVEN" if self.micro_even >= 0.5 else "ODD"

    @property
    def fast_dir(self) -> str:
        return "EVEN" if self.fast_even >= 0.5 else "ODD"

    @property
    def slow_dir(self) -> str:
        return "EVEN" if self.slow_even >= 0.5 else "ODD"

    @property
    def aligned(self) -> bool:
        """Fast + Slow timeframes agree on direction."""
        return self.fast_dir == self.slow_dir

    @property
    def triple_aligned(self) -> bool:
        """All three timeframes agree."""
        return self.fast_dir == self.slow_dir == self.micro_dir

    def _momentum_ok(self) -> bool:
        """Check that the fast window's dominant% is not decaying.

        Returns True if momentum is stable or rising.
        """
        hist = list(self._fast_even_history)
        if len(hist) < 5:
            return True  # Not enough data, assume OK

        # Compare recent half vs older half
        mid = len(hist) // 2
        old_avg = sum(hist[:mid]) / mid
        new_avg = sum(hist[mid:]) / (len(hist) - mid)

        # For the current direction, check if bias is stable/growing
        direction = self.fast_dir
        if direction == "EVEN":
            # We want fast_even to be stable or rising
            return new_avg >= old_avg - 0.02  # allow tiny dip
        else:
            # We want fast_even to be stable or falling (odd getting stronger)
            return new_avg <= old_avg + 0.02

    def score(self, regime_det: Optional[RegimeDetector] = None) -> Optional[SignalSnapshot]:
        if not self.warmed or self._cooldown > 0:
            return None

        # Fast + Slow must agree
        if not self.aligned:
            return None

        direction = self.fast_dir

        # Fast window must show minimum edge
        fast_dominant = self.fast_even if direction == "EVEN" else (1.0 - self.fast_even)
        if fast_dominant < self.min_fast_pct:
            return None

        # Momentum check: bias must not be decaying
        if not self._momentum_ok():
            return None

        # Slow window dominant %
        slow_dominant = self.slow_even if direction == "EVEN" else (1.0 - self.slow_even)

        # Micro window dominant %
        micro_dominant = self.micro_even if direction == "EVEN" else (1.0 - self.micro_even)

        # Score: weighted combination of all three timeframes
        fast_edge = fast_dominant - 0.5   # 0.0 to 0.5
        slow_edge = slow_dominant - 0.5   # 0.0 to 0.5
        micro_edge = micro_dominant - 0.5  # 0.0 to 0.5

        # Fast is weighted most (most current), then slow, then micro
        combined_edge = 0.50 * fast_edge + 0.30 * slow_edge + 0.20 * micro_edge
        composite = min(combined_edge / 0.15, 1.0)  # 15% edge = score 1.0

        # Triple alignment bonus
        if self.triple_aligned:
            composite = min(composite * 1.08, 1.0)

        regime = Regime.UNKNOWN
        if regime_det is not None:
            regime = regime_det.current_regime

        return SignalSnapshot(
            digit_bias=combined_edge,
            chi_sq_significant=False,
            entropy=0.0,
            momentum=fast_edge - slow_edge,  # positive = fast stronger than slow
            regime=regime,
            composite_score=composite,
            direction=direction,
        )

    def on_trade_placed(self) -> None:
        self._cooldown = self.cooldown_ticks

    def reset(self) -> None:
        self._digits.clear()
        self._fast_even_history.clear()
        self._cooldown = 0

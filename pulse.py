"""
Pulse Strategy — Simple but Powerful

Two windows, one decision:
  FAST window (15 ticks) — reacts quickly to shifts
  SLOW window (50 ticks) — confirms the overall trend

Rules:
  1. Both windows agree on direction (both say EVEN or both say ODD) → TRADE
  2. Fast window has ≥ 53% on the dominant side → signal is valid
  3. The stronger the agreement, the higher the score
  4. Trades BOTH even and odd — follows whichever side is winning
  5. No complex state machines, no HMM, no chi-square — just frequency

Why it works:
  - Fast window catches shifts early
  - Slow window filters out noise (prevents trading random blips)
  - When both agree, there's a real short-term pattern
  - No over-engineering — less logic = fewer false rejections
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
    Per-symbol dual-timeframe digit analyser.

    Fast (15 ticks) + Slow (50 ticks) must agree on direction.
    """

    def __init__(
        self,
        fast_window: int = 15,
        slow_window: int = 50,
        min_fast_pct: float = 0.53,
        cooldown_ticks: int = 1,
    ):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.min_fast_pct = min_fast_pct
        self.cooldown_ticks = cooldown_ticks

        self._digits: deque[int] = deque(maxlen=slow_window)
        self._cooldown: int = 0

    def update(self, quote: str) -> None:
        digit = self._last_digit(quote)
        self._digits.append(digit)
        if self._cooldown > 0:
            self._cooldown -= 1

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
    def fast_even(self) -> float:
        return self._even_pct(list(self._digits)[-self.fast_window:])

    @property
    def slow_even(self) -> float:
        return self._even_pct(self._digits)

    @property
    def fast_dir(self) -> str:
        return "EVEN" if self.fast_even >= 0.5 else "ODD"

    @property
    def slow_dir(self) -> str:
        return "EVEN" if self.slow_even >= 0.5 else "ODD"

    @property
    def aligned(self) -> bool:
        """Both timeframes agree on direction."""
        return self.fast_dir == self.slow_dir

    def score(self, regime_det: Optional[RegimeDetector] = None) -> Optional[SignalSnapshot]:
        if not self.warmed or self._cooldown > 0:
            return None

        # Both must agree
        if not self.aligned:
            return None

        direction = self.fast_dir

        # Fast window must show minimum edge
        fast_dominant = self.fast_even if direction == "EVEN" else (1.0 - self.fast_even)
        if fast_dominant < self.min_fast_pct:
            return None

        # Slow window dominant %
        slow_dominant = self.slow_even if direction == "EVEN" else (1.0 - self.slow_even)

        # Score: average of fast and slow imbalances, scaled
        fast_edge = fast_dominant - 0.5   # 0.0 to 0.5
        slow_edge = slow_dominant - 0.5   # 0.0 to 0.5

        # Fast edge is weighted more (it's more current)
        combined_edge = 0.6 * fast_edge + 0.4 * slow_edge
        composite = min(combined_edge / 0.15, 1.0)  # 15% edge = score 1.0

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
        self._cooldown = 0

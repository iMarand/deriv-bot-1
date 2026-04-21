"""
AlphaBloom Digit Frequency Strategy — Improved v2

Trade the dominant side (EVEN or ODD) when it has a clear edge:
  - Even ≥ 55% → trade EVEN
  - Odd  ≥ 55% → trade ODD
  - 50-55% either side + rising trend → trade that side
  - No clear winner → wait
Scans all indices, picks the strongest imbalance.

Improvements over v1:
  - Exponential weighting: recent ticks count more than older ticks
  - Breakout detection: sudden jump from <50% to >55% = bonus score
  - Volatility discount: reduce score when ATR is spiking
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Optional

from ensemble import SignalSnapshot
from regime import Regime, RegimeDetector

logger = logging.getLogger("alphabloom")


class AlphaBloomScorer:
    """
    Per-symbol digit frequency analyser with exponential weighting.
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
        # Breakout tracking
        self._prev_even_pct: float = 0.5

    def update(self, quote: str) -> None:
        digit = self._last_digit(quote)
        self._digits.append(digit)
        if self._cooldown > 0:
            self._cooldown -= 1

        if len(self._digits) >= 10:
            old_pct = self._prev_even_pct
            new_pct = self.p_even
            self._prev_even_pct = new_pct
            self._pct_history.append(new_pct)

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
    def p_even_weighted(self) -> float:
        """Exponentially weighted Even% — recent ticks matter more."""
        n = len(self._digits)
        if n == 0:
            return 0.5

        halflife = max(n // 4, 5)
        weights = []
        values = []
        for i, d in enumerate(self._digits):
            w = math.exp(-(n - 1 - i) / halflife)
            weights.append(w)
            values.append(1.0 if d % 2 == 0 else 0.0)

        total_weight = sum(weights)
        if total_weight < 1e-10:
            return 0.5
        return sum(w * v for w, v in zip(weights, values)) / total_weight

    @property
    def p_odd(self) -> float:
        return 1.0 - self.p_even

    @property
    def dominant_pct(self) -> float:
        """The higher of Even% or Odd% (weighted)."""
        we = self.p_even_weighted
        return max(we, 1.0 - we)

    @property
    def dominant_pct_raw(self) -> float:
        """Un-weighted dominant% for display."""
        return max(self.p_even, self.p_odd)

    @property
    def trend(self) -> float:
        """Change in Even% over trend window. Positive = Even rising."""
        h = self._pct_history
        tw = self.trend_window
        if len(h) < tw:
            return 0.0
        return h[-1] - h[-tw]

    @property
    def warmed(self) -> bool:
        return len(self._digits) >= 20

    def _breakout_score(self) -> float:
        """Detect if Even% just broke out from neutral to strong.

        A breakout from <50% to >55% (or vice versa for Odd) is a strong signal.
        """
        h = list(self._pct_history)
        if len(h) < 5:
            return 0.0

        # Check last 5 ticks for a sudden shift
        old_avg = sum(h[-10:-5]) / 5 if len(h) >= 10 else 0.5
        new_avg = h[-1]

        # Even breakout
        if old_avg < 0.52 and new_avg >= self.imbalance_threshold:
            return min((new_avg - old_avg) / 0.10, 0.5)
        # Odd breakout
        if old_avg > 0.48 and new_avg <= (1.0 - self.imbalance_threshold):
            return min((old_avg - new_avg) / 0.10, 0.5)

        return 0.0

    def zone(self) -> str:
        """Current zone: GREEN (strong), MOMENTUM (weak+trending), or WAIT."""
        dp = self.dominant_pct
        if dp >= self.imbalance_threshold:
            return "GREEN"
        elif dp >= 0.50:
            we = self.p_even_weighted
            if we > 0.5 and self.trend > 0.005:
                return "MOMENTUM"
            if we < 0.5 and self.trend < -0.005:
                return "MOMENTUM"
            return "WAIT"
        return "WAIT"

    @property
    def direction(self) -> str:
        we = self.p_even_weighted
        return "EVEN" if we >= 0.5 else "ODD"

    def score(self, regime_det: Optional[RegimeDetector] = None,
              vol_discount: float = 0.0) -> Optional[SignalSnapshot]:
        """Generate a signal with optional volatility discount.

        Args:
            regime_det: regime detector for regime info
            vol_discount: 0.0 to 1.0 — how much to discount the score
                          due to high volatility. Caller can compute from ATR.
        """
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

        # ── Breakout bonus ──
        breakout = self._breakout_score()
        if breakout > 0:
            composite = min(composite + breakout * 0.2, 1.0)

        # ── Volatility discount ──
        if vol_discount > 0:
            composite *= max(0.5, 1.0 - vol_discount * 0.3)

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
        self._prev_even_pct = 0.5

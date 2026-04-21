"""
Roll Cake Pattern Strategy

Detects repeating cycles in price movements using autocorrelation analysis.
The "Roll Cake" pattern looks for periodic reversals — e.g. sequences like
UUDDUU, UDUD, or UUUDDD — and predicts the next direction.

Works with:
  - RISE/FALL      → CALL / PUT
  - HIGHER/LOWER   → CALL / PUT + barrier
  - OVER/UNDER     → DIGITOVER / DIGITUNDER + digit barrier
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Optional, List, Tuple

import numpy as np

logger = logging.getLogger("rollcake")


class RollCakeScorer:
    """
    Autocorrelation-based cycle detector for price tick sequences.

    Analyses a rolling window of tick movements (up/down/flat) and detects
    repeating patterns at multiple cycle lengths (lags 2-6). When a strong
    repeating pattern is found, predicts the next direction.
    """

    def __init__(
        self,
        window_size: int = 30,
        min_autocorrelation: float = 0.25,
        cycle_lags: Tuple[int, ...] = (2, 3, 4, 5, 6),
        min_streak: int = 3,
        cooldown_ticks: int = 2,
    ):
        self.window_size = window_size
        self.min_autocorrelation = min_autocorrelation
        self.cycle_lags = cycle_lags
        self.min_streak = min_streak
        self.cooldown_ticks = cooldown_ticks

        self._prices: deque[float] = deque(maxlen=window_size + 1)
        self._moves: deque[int] = deque(maxlen=window_size)  # +1=up, -1=down, 0=flat
        self._cooldown: int = 0

    def update(self, price: float) -> None:
        """Feed a new tick price."""
        self._prices.append(price)
        if len(self._prices) >= 2:
            diff = self._prices[-1] - self._prices[-2]
            if diff > 0:
                self._moves.append(1)
            elif diff < 0:
                self._moves.append(-1)
            else:
                self._moves.append(0)
        if self._cooldown > 0:
            self._cooldown -= 1

    @property
    def warmed(self) -> bool:
        return len(self._moves) >= 15

    def _autocorrelation(self, lag: int) -> float:
        """Compute autocorrelation of the move sequence at a given lag."""
        moves = list(self._moves)
        n = len(moves)
        if n < lag + 5:
            return 0.0

        arr = np.array(moves, dtype=float)
        mean = np.mean(arr)
        var = np.var(arr)
        if var < 1e-10:
            return 0.0

        shifted = arr[lag:]
        original = arr[:n - lag]
        cov = np.mean((original - mean) * (shifted - mean))
        return cov / var

    def _detect_best_cycle(self) -> Tuple[int, float]:
        """Find the cycle lag with strongest autocorrelation. Returns (lag, corr)."""
        best_lag = 0
        best_corr = 0.0
        for lag in self.cycle_lags:
            corr = self._autocorrelation(lag)
            # We care about both positive and negative autocorrelation
            # Negative = alternating pattern (UDUDUD), Positive = repeating cycle
            if abs(corr) > abs(best_corr):
                best_corr = corr
                best_lag = lag
        return best_lag, best_corr

    def _predict_from_cycle(self, lag: int, corr: float) -> Optional[int]:
        """Given the best cycle lag/corr, predict next move direction.

        Returns +1 (up/rise), -1 (down/fall), or None.
        """
        moves = list(self._moves)
        if len(moves) < lag:
            return None

        # Look at where we are in the cycle
        lagged_move = moves[-lag]

        if corr > 0:
            # Positive autocorrelation: pattern repeats
            # The move `lag` steps ago should repeat now
            return lagged_move if lagged_move != 0 else None
        else:
            # Negative autocorrelation: pattern alternates
            # The opposite of the move `lag` steps ago
            return -lagged_move if lagged_move != 0 else None

    def _streak_direction(self) -> Tuple[Optional[int], int]:
        """Check if there's a strong current streak. Returns (direction, length)."""
        moves = list(self._moves)
        if not moves:
            return None, 0

        last = moves[-1]
        if last == 0:
            return None, 0

        count = 0
        for m in reversed(moves):
            if m == last:
                count += 1
            else:
                break

        return last, count

    def score(self) -> Optional[Tuple[str, float]]:
        """Generate a direction prediction with confidence.

        Returns: (direction, confidence) where direction is "RISE" or "FALL",
                 or None if no clear pattern.
        """
        if not self.warmed or self._cooldown > 0:
            return None

        best_lag, best_corr = self._detect_best_cycle()

        # ── Cycle-based prediction ──
        cycle_pred = None
        cycle_conf = 0.0
        if abs(best_corr) >= self.min_autocorrelation:
            pred = self._predict_from_cycle(best_lag, best_corr)
            if pred is not None:
                cycle_pred = pred
                # Confidence from autocorrelation strength
                cycle_conf = min(abs(best_corr) / 0.50, 1.0) * 0.7

        # ── Streak continuation/reversal ──
        streak_dir, streak_len = self._streak_direction()
        streak_pred = None
        streak_conf = 0.0
        if streak_dir is not None and streak_len >= self.min_streak:
            # Short streaks (3-4): likely to continue
            # Long streaks (5+): likely to reverse
            if streak_len <= 4:
                streak_pred = streak_dir  # continuation
                streak_conf = min(streak_len / 6.0, 0.5)
            else:
                streak_pred = -streak_dir  # reversal
                streak_conf = min((streak_len - 4) / 4.0, 0.6)

        # ── Recent momentum ──
        recent = list(self._moves)[-7:]
        if len(recent) >= 7:
            ups = sum(1 for m in recent if m > 0)
            downs = sum(1 for m in recent if m < 0)
            total = ups + downs
            if total > 0:
                momentum_bias = (ups - downs) / total
            else:
                momentum_bias = 0.0
        else:
            momentum_bias = 0.0

        # ── Combine predictions ──
        if cycle_pred is not None and streak_pred is not None:
            # Both agree → strong signal
            if cycle_pred == streak_pred:
                direction = cycle_pred
                confidence = min(cycle_conf + streak_conf, 1.0)
            else:
                # Disagree → use the stronger one with reduced confidence
                if cycle_conf > streak_conf:
                    direction = cycle_pred
                    confidence = cycle_conf * 0.6
                else:
                    direction = streak_pred
                    confidence = streak_conf * 0.6
        elif cycle_pred is not None:
            direction = cycle_pred
            confidence = cycle_conf
        elif streak_pred is not None:
            direction = streak_pred
            confidence = streak_conf
        else:
            # No pattern detected
            return None

        # Apply momentum bias as a small adjustment
        if (direction > 0 and momentum_bias > 0.3) or (direction < 0 and momentum_bias < -0.3):
            confidence = min(confidence * 1.15, 1.0)
        elif (direction > 0 and momentum_bias < -0.3) or (direction < 0 and momentum_bias > 0.3):
            confidence *= 0.85

        # Minimum confidence gate
        if confidence < 0.25:
            return None

        dir_str = "RISE" if direction > 0 else "FALL"
        return (dir_str, round(confidence, 4))

    @property
    def status(self) -> str:
        """Human-readable status for dashboard."""
        if not self.warmed:
            return f"warming {len(self._moves)}/15 ticks"
        lag, corr = self._detect_best_cycle()
        sdir, slen = self._streak_direction()
        streak_str = f" streak={slen}" if sdir else ""
        return f"lag={lag} corr={corr:+.2f}{streak_str}"

    def on_trade_placed(self) -> None:
        self._cooldown = self.cooldown_ticks

    def reset(self) -> None:
        self._prices.clear()
        self._moves.clear()
        self._cooldown = 0

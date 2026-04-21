"""
Zigzag 7-Tick Reversal Strategy

Analyses exactly 7 consecutive ticks to identify zigzag swing patterns.
A clear zigzag (alternating up/down) signals an upcoming reversal.

Works with:
  - RISE/FALL           → CALL / PUT
  - HIGHER/LOWER        → CALL / PUT + barrier
  - TOUCH/NO TOUCH      → ONETOUCH / NOTOUCH + barrier
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger("zigzag")


class ZigzagScorer:
    """
    7-tick zigzag pattern analyser.

    Looks at the last 7 ticks for alternating highs/lows (zigzag pattern).
    The more pronounced and regular the zigzag, the stronger the signal
    for a reversal in the next direction.
    """

    def __init__(
        self,
        tick_count: int = 7,
        min_swings: int = 3,
        amplitude_threshold: float = 0.0001,
        cooldown_ticks: int = 2,
        lookback_buffer: int = 50,
    ):
        self.tick_count = tick_count
        self.min_swings = min_swings
        self.amplitude_threshold = amplitude_threshold
        self.cooldown_ticks = cooldown_ticks

        self._prices: deque[float] = deque(maxlen=lookback_buffer)
        self._cooldown: int = 0

    def update(self, price: float) -> None:
        """Feed a new tick price."""
        self._prices.append(price)
        if self._cooldown > 0:
            self._cooldown -= 1

    @property
    def warmed(self) -> bool:
        return len(self._prices) >= self.tick_count

    def _get_window(self) -> list[float]:
        """Get the last `tick_count` prices."""
        return list(self._prices)[-self.tick_count:]

    def _compute_moves(self, window: list[float]) -> list[float]:
        """Compute price differences between consecutive ticks."""
        return [window[i + 1] - window[i] for i in range(len(window) - 1)]

    def _count_swings(self, moves: list[float]) -> int:
        """Count the number of direction changes (swings) in the moves."""
        if len(moves) < 2:
            return 0

        swings = 0
        for i in range(1, len(moves)):
            if moves[i - 1] != 0 and moves[i] != 0:
                if (moves[i - 1] > 0 and moves[i] < 0) or \
                   (moves[i - 1] < 0 and moves[i] > 0):
                    swings += 1
        return swings

    def _swing_regularity(self, moves: list[float]) -> float:
        """Measure how regular the zigzag pattern is (0.0 to 1.0).

        A perfect alternating zigzag scores 1.0.
        """
        if len(moves) < 2:
            return 0.0

        # Perfect zigzag has len(moves)-1 swings
        max_swings = len(moves) - 1
        actual = self._count_swings(moves)
        return actual / max_swings if max_swings > 0 else 0.0

    def _amplitude_score(self, moves: list[float]) -> float:
        """Score based on the amplitude of the swings.

        Larger swings relative to the price level = more significant pattern.
        """
        if not moves:
            return 0.0

        amplitudes = [abs(m) for m in moves if abs(m) > self.amplitude_threshold]
        if not amplitudes:
            return 0.0

        # Use coefficient of variation of amplitudes
        # Low CV = consistent swings = better zigzag
        mean_amp = np.mean(amplitudes)
        if mean_amp < 1e-10:
            return 0.0

        std_amp = np.std(amplitudes)
        cv = std_amp / mean_amp if mean_amp > 0 else 1.0

        # Invert: low CV = high score
        regularity = max(0, 1.0 - cv)

        # Scale by number of significant moves
        coverage = len(amplitudes) / len(moves) if moves else 0
        return regularity * coverage

    def _predict_next_direction(self, moves: list[float]) -> Optional[int]:
        """Based on the zigzag pattern, predict the next move.

        If the last move was UP, zigzag predicts DOWN (and vice versa).
        """
        # Find the last non-zero move
        for m in reversed(moves):
            if m > 0:
                return -1  # Last was up → predict down
            elif m < 0:
                return 1   # Last was down → predict up
        return None

    def _trend_exhaustion(self, window: list[float]) -> Tuple[Optional[int], float]:
        """Detect when a trend within the zigzag is exhausting.

        If the overall 7-tick window shows a clear trend BUT the last 3 ticks
        show weakening momentum, signal reversal.
        """
        if len(window) < 7:
            return None, 0.0

        # Overall trend
        overall_change = window[-1] - window[0]
        if abs(overall_change) < self.amplitude_threshold:
            return None, 0.0

        # Last 3 ticks momentum
        late_moves = [window[i + 1] - window[i] for i in range(4, 6)]

        # If overall up but last moves weakening/reversing
        if overall_change > 0:
            late_strength = sum(late_moves)
            if late_strength < 0:
                # Momentum reversed → signal FALL
                confidence = min(abs(late_strength) / abs(overall_change), 0.5)
                return -1, confidence
        elif overall_change < 0:
            late_strength = sum(late_moves)
            if late_strength > 0:
                # Momentum reversed → signal RISE
                confidence = min(abs(late_strength) / abs(overall_change), 0.5)
                return 1, confidence

        return None, 0.0

    def score(self) -> Optional[Tuple[str, float]]:
        """Generate a direction prediction with confidence.

        Returns: (direction, confidence) where direction is "RISE" or "FALL",
                 or None if no clear zigzag pattern.
        """
        if not self.warmed or self._cooldown > 0:
            return None

        window = self._get_window()
        moves = self._compute_moves(window)

        swings = self._count_swings(moves)
        regularity = self._swing_regularity(moves)
        amp_score = self._amplitude_score(moves)

        # ── Zigzag signal ──
        zigzag_confidence = 0.0
        zigzag_direction = None

        if swings >= self.min_swings:
            # Base confidence from swing count and regularity
            swing_ratio = swings / (len(moves) - 1) if len(moves) > 1 else 0
            zigzag_confidence = (
                0.4 * swing_ratio +
                0.3 * regularity +
                0.3 * amp_score
            )
            zigzag_direction = self._predict_next_direction(moves)

        # ── Trend exhaustion signal ──
        exhaust_dir, exhaust_conf = self._trend_exhaustion(window)

        # ── Combine ──
        if zigzag_direction is not None and zigzag_confidence > 0.25:
            direction = zigzag_direction
            confidence = zigzag_confidence

            # If exhaustion agrees, boost
            if exhaust_dir is not None and exhaust_dir == zigzag_direction:
                confidence = min(confidence + exhaust_conf * 0.3, 1.0)
            # If exhaustion contradicts, diminish
            elif exhaust_dir is not None and exhaust_dir != zigzag_direction:
                confidence *= 0.7
        elif exhaust_dir is not None and exhaust_conf >= 0.3:
            direction = exhaust_dir
            confidence = exhaust_conf
        else:
            return None

        # Minimum gate
        if confidence < 0.20:
            return None

        dir_str = "RISE" if direction > 0 else "FALL"
        return (dir_str, round(confidence, 4))

    @property
    def status(self) -> str:
        """Human-readable status for dashboard."""
        if not self.warmed:
            return f"warming {len(self._prices)}/{self.tick_count} ticks"
        window = self._get_window()
        moves = self._compute_moves(window)
        swings = self._count_swings(moves)
        reg = self._swing_regularity(moves)
        return f"swings={swings}/{len(moves) - 1} reg={reg:.2f}"

    @property
    def recent_high(self) -> float:
        """Recent highest price (for barrier calc)."""
        return max(list(self._prices)[-self.tick_count:]) if self.warmed else 0.0

    @property
    def recent_low(self) -> float:
        """Recent lowest price (for barrier calc)."""
        return min(list(self._prices)[-self.tick_count:]) if self.warmed else 0.0

    @property
    def current_price(self) -> float:
        """Latest price."""
        return self._prices[-1] if self._prices else 0.0

    def on_trade_placed(self) -> None:
        self._cooldown = self.cooldown_ticks

    def reset(self) -> None:
        self._prices.clear()
        self._cooldown = 0

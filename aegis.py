"""
Aegis Strategy — Ultimate Defense
Focuses on extreme loss minimization using:
1. Relative Strength Index (RSI) to avoid exhaustion (overbought/oversold).
2. Volatility (Standard Deviation) to avoid erratic spikes and dead zones.
3. Tick Velocity to measure market participation.
4. Digit Dominance (Even/Odd) to align with the micro-trend.

Rules:
- Must have enough warmup data (e.g. 50 ticks).
- RSI must be in the "Goldilocks" zone (e.g., 40 to 60) — avoiding extreme trends that snap back.
- Volatility must be within normal bounds (not top 20%, not bottom 20%).
- Tick Velocity must be stable.
- Digit dominance must be definitively on one side (≥ 55%).
"""

from __future__ import annotations

import logging
import math
from collections import deque
from typing import Optional, List

from ensemble import SignalSnapshot
from regime import Regime, RegimeDetector

logger = logging.getLogger("aegis")


class AegisScorer:
    """
    Highly sophisticated defensive algorithm.
    """

    def __init__(
        self,
        rsi_period: int = 14,
        volatility_window: int = 20,
        digit_window: int = 25,
        min_warmup: int = 60,
        cooldown_ticks: int = 3,
    ):
        self.rsi_period = rsi_period
        self.volatility_window = volatility_window
        self.digit_window = digit_window
        self.min_warmup = min_warmup
        self.cooldown_ticks = cooldown_ticks

        self._prices: deque[float] = deque(maxlen=200)
        self._digits: deque[int] = deque(maxlen=200)
        self._cooldown: int = 0

        # Tracking histories for percentile calculations
        self._volatility_history: deque[float] = deque(maxlen=100)
        self._velocity_history: deque[float] = deque(maxlen=100)

    def update(self, quote: str, price: float) -> None:
        self._prices.append(price)
        digit = self._last_digit(quote)
        self._digits.append(digit)

        if self._cooldown > 0:
            self._cooldown -= 1

        if len(self._prices) >= self.volatility_window:
            self._volatility_history.append(self._calc_std_dev())
            self._velocity_history.append(self._calc_velocity())

    @staticmethod
    def _last_digit(quote: str) -> int:
        digits_only = "".join(ch for ch in quote if ch.isdigit())
        return int(digits_only[-1]) if digits_only else 0

    @property
    def warmed(self) -> bool:
        return len(self._prices) >= self.min_warmup and len(self._volatility_history) >= 30

    def _calc_std_dev(self) -> float:
        prices = list(self._prices)[-self.volatility_window:]
        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return math.sqrt(variance)

    def _calc_velocity(self) -> float:
        prices = list(self._prices)[-self.volatility_window:]
        return sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))

    def _calc_rsi(self) -> Optional[float]:
        if len(self._prices) < self.rsi_period + 1:
            return None
        
        prices = list(self._prices)[-(self.rsi_period + 1):]
        gains = 0.0
        losses = 0.0
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i - 1]
            if change > 0:
                gains += change
            else:
                losses += abs(change)
                
        avg_gain = gains / self.rsi_period
        avg_loss = losses / self.rsi_period
        
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

    @property
    def even_pct(self) -> float:
        digits = list(self._digits)[-self.digit_window:]
        if not digits:
            return 0.5
        return sum(1 for d in digits if d % 2 == 0) / len(digits)

    def get_status_string(self) -> str:
        if not self.warmed:
            return f"warming ({len(self._prices)}/{self.min_warmup})"
        
        rsi = self._calc_rsi()
        rsi_str = f"RSI:{rsi:.1f}" if rsi is not None else "RSI:--"
        even = self.even_pct
        return f"EVEN:{even:.1%} | {rsi_str} | Vol:{self._volatility_history[-1]:.4f}"

    def score(self, regime_det: Optional[RegimeDetector] = None) -> Optional[SignalSnapshot]:
        if not self.warmed or self._cooldown > 0:
            return None

        # 1. RSI Filter
        rsi = self._calc_rsi()
        if rsi is None:
            return None
            
        # We want the market to be moving, but NOT exhausted.
        # Avoid < 35 (oversold, might snap up) and > 65 (overbought, might snap down)
        if rsi < 35 or rsi > 65:
            return None

        # 2. Volatility Filter
        current_vol = self._volatility_history[-1]
        sorted_vol = sorted(list(self._volatility_history))
        p20_vol = sorted_vol[int(len(sorted_vol) * 0.20)]
        p80_vol = sorted_vol[int(len(sorted_vol) * 0.80)]
        
        # If volatility is abnormally low (dead market) or abnormally high (erratic), block.
        if current_vol < p20_vol or current_vol > p80_vol:
            return None

        # 3. Tick Velocity Filter
        current_vel = self._velocity_history[-1]
        sorted_vel = sorted(list(self._velocity_history))
        p20_vel = sorted_vel[int(len(sorted_vel) * 0.20)]
        p80_vel = sorted_vel[int(len(sorted_vel) * 0.80)]
        
        if current_vel < p20_vel or current_vel > p80_vel:
            return None

        # 4. Digit Dominance
        even = self.even_pct
        if even >= 0.55:
            direction = "EVEN"
            edge = even - 0.5
        elif even <= 0.45:
            direction = "ODD"
            edge = (1.0 - even) - 0.5
        else:
            return None

        # Calculate composite score based on the edge. Max edge is 0.5.
        # If edge is 0.15 (e.g. 65% dominance), we give it a full 1.0 score.
        composite = min(edge / 0.15, 1.0)
        
        # Aegis gives a slight penalty if the regime is unknown to be extra safe
        regime = Regime.UNKNOWN
        if regime_det is not None:
            regime = regime_det.current_regime
            if regime == Regime.CHOPPY:
                return None  # Never trade in purely choppy markets
            if regime == Regime.UNKNOWN:
                composite *= 0.90
        
        # Final strict threshold check for safety
        if composite < 0.65:
            return None

        return SignalSnapshot(
            digit_bias=edge,
            chi_sq_significant=False,
            entropy=0.0,
            momentum=0.0,
            regime=regime,
            composite_score=composite,
            direction=direction,
        )

    def on_trade_placed(self) -> None:
        self._cooldown = self.cooldown_ticks

    def reset(self) -> None:
        self._prices.clear()
        self._digits.clear()
        self._volatility_history.clear()
        self._velocity_history.clear()
        self._cooldown = 0

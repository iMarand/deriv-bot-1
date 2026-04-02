"""
Statistical Analysis Engine
- Digit distribution tracking (chi-square, Bayesian Beta/Dirichlet)
- CUSUM change-point detection on digit bias
- Entropy of digit / price-change distributions
- Volatility: rolling std, ATR, realized vol
- Momentum indicator
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats as sp_stats

from config import DigitConfig, VolatilityConfig


# ─────────────────────────────────────────────
# Digit Distribution Tracker
# ─────────────────────────────────────────────
class DigitTracker:
    """Track last-digit distribution over a sliding window."""

    def __init__(self, cfg: DigitConfig):
        self.cfg = cfg
        self.window: deque[int] = deque(maxlen=cfg.window_size)
        self._update_count: int = 0
        # Bayesian Beta prior for P(even): Beta(alpha, beta)
        self.beta_alpha: float = 1.0
        self.beta_beta: float = 1.0
        # CUSUM state
        self._cusum_pos: float = 0.0
        self._cusum_neg: float = 0.0
        self._cusum_alarm: bool = False

    # ── update ──
    def update(self, quote: str) -> None:
        """Feed a new tick quote string (e.g. '1234.56'). Extracts last digit."""
        digit = self._last_digit(quote)
        self._update_count += 1
        self.window.append(digit)
        # Bayesian update (even=1, odd=0)
        if digit % 2 == 0:
            self.beta_alpha += 1
        else:
            self.beta_beta += 1
        # CUSUM update
        self._update_cusum(digit)

    @staticmethod
    def _last_digit(quote: str) -> int:
        digits_only = "".join(ch for ch in quote if ch.isdigit())
        return int(digits_only[-1]) if digits_only else 0

    # ── probabilities ──
    @property
    def p_even(self) -> float:
        if not self.window:
            return 0.5
        return sum(1 for d in self.window if d % 2 == 0) / len(self.window)

    @property
    def p_odd(self) -> float:
        return 1.0 - self.p_even

    def digit_counts(self) -> Dict[int, int]:
        counts = {d: 0 for d in range(10)}
        for d in self.window:
            counts[d] += 1
        return counts

    def digit_probs(self) -> Dict[int, float]:
        n = len(self.window) or 1
        counts = self.digit_counts()
        return {d: c / n for d, c in counts.items()}

    # ── chi-square test (cached) ──
    def chi_square_test(self) -> Tuple[float, float]:
        """Returns (chi2_statistic, p_value) testing uniform digit distribution.
        Cached — only recomputes every 5 updates."""
        if len(self.window) < 10:
            return 0.0, 1.0
        # Cache: recompute only every 5 ticks
        tick_count = self._update_count
        if hasattr(self, "_chi_cache_at") and tick_count - self._chi_cache_at < 5:
            return self._chi_cache
        counts = self.digit_counts()
        observed = np.array([counts[d] for d in range(10)], dtype=float)
        expected = np.full(10, len(self.window) / 10.0)
        chi2, p_val = sp_stats.chisquare(observed, expected)
        self._chi_cache = (float(chi2), float(p_val))
        self._chi_cache_at = tick_count
        return self._chi_cache

    def is_biased(self) -> bool:
        """True if chi-square rejects uniformity at the configured alpha."""
        _, p = self.chi_square_test()
        return p < self.cfg.chi_sq_alpha

    # ── Bayesian credible interval ──
    def bayesian_even_ci(self, credibility: float = 0.95) -> Tuple[float, float]:
        """Return (lower, upper) credible interval for P(even)."""
        lo = sp_stats.beta.ppf((1 - credibility) / 2, self.beta_alpha, self.beta_beta)
        hi = sp_stats.beta.ppf((1 + credibility) / 2, self.beta_alpha, self.beta_beta)
        return float(lo), float(hi)

    def bayesian_p_even(self) -> float:
        """Posterior mean of P(even)."""
        return self.beta_alpha / (self.beta_alpha + self.beta_beta)

    # ── entropy ──
    def digit_entropy(self) -> float:
        """Shannon entropy of digit distribution (max ~3.32 for uniform)."""
        probs = self.digit_probs()
        h = 0.0
        for p in probs.values():
            if p > 0:
                h -= p * math.log2(p)
        return h

    # ── CUSUM change-point detector ──
    def _update_cusum(self, digit: int) -> None:
        x = 1.0 if digit % 2 == 0 else 0.0
        deviation = x - 0.5
        self._cusum_pos = max(0, self._cusum_pos + deviation - self.cfg.cusum_drift)
        self._cusum_neg = max(0, self._cusum_neg - deviation - self.cfg.cusum_drift)
        self._cusum_alarm = (
            self._cusum_pos > self.cfg.cusum_threshold
            or self._cusum_neg > self.cfg.cusum_threshold
        )

    @property
    def cusum_alarm(self) -> bool:
        return self._cusum_alarm

    def reset_cusum(self) -> None:
        self._cusum_pos = 0.0
        self._cusum_neg = 0.0
        self._cusum_alarm = False

    # ── bias magnitude (used for multi-index scoring) ──
    @property
    def bias_magnitude(self) -> float:
        return abs(self.p_even - 0.5)


# ─────────────────────────────────────────────
# Volatility Tracker
# ─────────────────────────────────────────────
class VolatilityTracker:
    """Track rolling volatility metrics on tick prices."""

    def __init__(self, cfg: VolatilityConfig):
        self.cfg = cfg
        self.prices: deque[float] = deque(maxlen=max(
            cfg.atr_period + 1,
            cfg.rolling_std_window + 1,
            cfg.realized_vol_window + 1,
        ))
        self._atr_values: deque[float] = deque(maxlen=200)  # history for percentile

    def update(self, price: float) -> None:
        self.prices.append(price)
        atr = self.atr
        if atr is not None:
            self._atr_values.append(atr)

    @property
    def returns(self) -> np.ndarray:
        if len(self.prices) < 2:
            return np.array([])
        p = np.array(self.prices)
        return np.diff(p) / p[:-1]

    # ── rolling standard deviation of returns ──
    @property
    def rolling_std(self) -> Optional[float]:
        r = self.returns
        w = self.cfg.rolling_std_window
        if len(r) < w:
            return None
        return float(np.std(r[-w:]))

    # ── ATR (tick-based: |high-low| approximated as |close-close|) ──
    @property
    def atr(self) -> Optional[float]:
        if len(self.prices) < self.cfg.atr_period + 1:
            return None
        diffs = np.abs(np.diff(list(self.prices)[-self.cfg.atr_period - 1:]))
        return float(np.mean(diffs))

    # ── realized volatility ──
    @property
    def realized_vol(self) -> Optional[float]:
        r = self.returns
        w = self.cfg.realized_vol_window
        if len(r) < w:
            return None
        return float(math.sqrt(np.sum(r[-w:] ** 2)))

    # ── price-change entropy ──
    def price_change_entropy(self, bins: int = 10) -> Optional[float]:
        r = self.returns
        if len(r) < 20:
            return None
        counts, _ = np.histogram(r, bins=bins)
        probs = counts / counts.sum()
        h = 0.0
        for p in probs:
            if p > 0:
                h -= p * math.log2(p)
        return h

    # ── ATR percentile (for regime detection) ──
    def atr_percentile(self) -> Optional[float]:
        if len(self._atr_values) < 20:
            return None
        current = self._atr_values[-1]
        return float(sp_stats.percentileofscore(list(self._atr_values), current))

    # ── momentum (simple: sum of last N returns) ──
    def momentum(self, lookback: int = 10) -> Optional[float]:
        r = self.returns
        if len(r) < lookback:
            return None
        return float(np.sum(r[-lookback:]))

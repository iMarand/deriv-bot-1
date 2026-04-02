"""
Multi-Index Selection Algorithm
- Scores each index by digit-bias strength + volatility fit
- Penalises correlated indices
- Selects the best index to trade
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import IndexConfig
from analysis import DigitTracker, VolatilityTracker
from regime import Regime

logger = logging.getLogger(__name__)


@dataclass
class IndexScore:
    symbol: str
    bias_magnitude: float
    atr_percentile: Optional[float]
    regime: Regime
    raw_score: float
    adjusted_score: float  # after correlation penalty


class IndexSelector:
    """Select the best synthetic index to trade."""

    def __init__(self, cfg: IndexConfig):
        self.cfg = cfg
        # Store recent digit-bias series per symbol for correlation
        self._bias_history: Dict[str, List[float]] = {s: [] for s in cfg.symbols}
        self._max_history: int = 100

    def update_history(self, symbol: str, bias: float) -> None:
        if symbol in self._bias_history:
            h = self._bias_history[symbol]
            h.append(bias)
            if len(h) > self._max_history:
                self._bias_history[symbol] = h[-self._max_history:]

    def compute_correlation(self, sym_a: str, sym_b: str) -> float:
        """Pearson correlation between bias histories of two symbols."""
        ha = self._bias_history.get(sym_a, [])
        hb = self._bias_history.get(sym_b, [])
        min_len = min(len(ha), len(hb))
        if min_len < 10:
            return 0.0
        a = np.array(ha[-min_len:])
        b = np.array(hb[-min_len:])
        if np.std(a) == 0 or np.std(b) == 0:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    def rank(
        self,
        trackers: Dict[str, Tuple[DigitTracker, VolatilityTracker]],
        regimes: Dict[str, Regime],
    ) -> List[IndexScore]:
        """Score and rank all indices. Returns sorted list (best first)."""
        scores: List[IndexScore] = []

        for symbol in self.cfg.symbols:
            if symbol not in trackers:
                continue
            digit, vol = trackers[symbol]
            regime = regimes.get(symbol, Regime.UNKNOWN)

            bias = digit.bias_magnitude
            atr_pct = vol.atr_percentile()

            # Raw score: bias strength weighted by regime suitability
            if regime == Regime.MEAN_REVERTING:
                regime_mult = 1.2
            elif regime == Regime.TRENDING:
                regime_mult = 0.8
            elif regime == Regime.CHOPPY:
                regime_mult = 0.5
            else:
                regime_mult = 0.7

            raw = bias * regime_mult

            self.update_history(symbol, bias)

            scores.append(IndexScore(
                symbol=symbol,
                bias_magnitude=bias,
                atr_percentile=atr_pct,
                regime=regime,
                raw_score=raw,
                adjusted_score=raw,  # will be adjusted below
            ))

        # ── Apply correlation penalty ──
        if len(scores) > 1:
            scores.sort(key=lambda s: s.raw_score, reverse=True)
            selected_symbols: List[str] = []
            for sc in scores:
                max_corr = 0.0
                for sel in selected_symbols:
                    c = abs(self.compute_correlation(sc.symbol, sel))
                    max_corr = max(max_corr, c)
                penalty = max_corr * self.cfg.correlation_penalty
                sc.adjusted_score = max(0, sc.raw_score - penalty)
                selected_symbols.append(sc.symbol)

        scores.sort(key=lambda s: s.adjusted_score, reverse=True)
        return scores

    def best(
        self,
        trackers: Dict[str, Tuple[DigitTracker, VolatilityTracker]],
        regimes: Dict[str, Regime],
    ) -> Optional[IndexScore]:
        """Return the single best index, or None if nothing meets minimum."""
        ranked = self.rank(trackers, regimes)
        if ranked and ranked[0].adjusted_score >= self.cfg.min_score_to_trade:
            return ranked[0]
        return None

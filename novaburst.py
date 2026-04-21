"""
NovaBurst Algorithm — Multi-Layer Probabilistic Trading Engine

A high-accuracy algorithm that requires CONVERGENCE across multiple
independent probability estimators before accepting a trade. This
dramatically reduces false positives at the cost of fewer trades.

Layers:
  1. Markov chain digit transition matrix  → P(next_even | last N digits)
  2. Momentum decay estimator             → is the bias strengthening or fading?
  3. Bayesian confidence gate             → posterior P(signal_correct) > threshold
  4. Multi-scale consensus                → 3 windows must agree (2 of 3 minimum)
  5. Regime-aware weighting               → adapts window trust based on HMM regime

Trades EVEN/ODD, RISE/FALL, or any contract type via the trade_strategy system.
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass
from typing import Dict, Optional, Tuple, List

import numpy as np

from ensemble import SignalSnapshot
from regime import Regime, RegimeDetector

logger = logging.getLogger("novaburst")


@dataclass
class NovaBurstConfig:
    """Configuration for the NovaBurst algorithm."""
    windows: Tuple[int, ...] = (10, 25, 60)
    min_consensus: int = 2           # at least N of len(windows) must agree
    bayesian_gate: float = 0.55      # posterior P(correct) must exceed this
    markov_order: int = 2            # N-gram order for transition matrix
    momentum_decay_window: int = 15  # ticks to measure momentum change
    min_warmup: int = 25             # minimum ticks before any signal
    cooldown_ticks: int = 2


class _SingleScaleAnalyser:
    """One analyser at a specific window scale."""

    def __init__(self, window_size: int):
        self.window_size = window_size
        self._digits: deque[int] = deque(maxlen=window_size)

    def update(self, digit: int) -> None:
        self._digits.append(digit)

    @property
    def warmed(self) -> bool:
        return len(self._digits) >= self.window_size

    @property
    def p_even(self) -> float:
        n = len(self._digits)
        if n == 0:
            return 0.5
        return sum(1 for d in self._digits if d % 2 == 0) / n

    @property
    def bias(self) -> float:
        """Unsigned bias magnitude."""
        return abs(self.p_even - 0.5)

    @property
    def direction(self) -> str:
        return "EVEN" if self.p_even >= 0.5 else "ODD"

    @property
    def confidence(self) -> float:
        """Confidence based on bias strength, 0…1."""
        return min(self.bias / 0.15, 1.0)


class NovaBurstScorer:
    """
    Per-symbol multi-layer digit analyser.

    Only generates a signal when multiple independent layers converge,
    providing higher accuracy at the cost of fewer signals.
    """

    def __init__(self, cfg: NovaBurstConfig | None = None):
        self.cfg = cfg or NovaBurstConfig()
        self._analysers = [_SingleScaleAnalyser(w) for w in self.cfg.windows]

        # Markov transition matrix: tracks P(digit | previous N digits)
        self._digit_buffer: deque[int] = deque(maxlen=200)
        self._transitions: Dict[tuple, Dict[int, int]] = {}

        # Bayesian Beta posterior for P(signal_correct)
        self._beta_alpha: float = 1.0   # prior: 1 success
        self._beta_beta: float = 1.0    # prior: 1 failure

        # Momentum decay tracking
        self._bias_history: deque[float] = deque(maxlen=50)

        # Trade result tracking for Bayesian update
        self._pending_direction: Optional[str] = None

        self._cooldown: int = 0
        self._tick_count: int = 0

    def update(self, quote: str) -> None:
        """Feed a new tick quote string."""
        digit = self._last_digit(quote)
        self._tick_count += 1

        # Update all scale analysers
        for analyser in self._analysers:
            analyser.update(digit)

        # Update Markov chain
        self._digit_buffer.append(digit)
        self._update_markov(digit)

        # Track bias momentum
        if self._tick_count >= 10:
            combined_bias = np.mean([a.bias for a in self._analysers if a.warmed])
            self._bias_history.append(combined_bias)

        if self._cooldown > 0:
            self._cooldown -= 1

    @staticmethod
    def _last_digit(quote: str) -> int:
        digits_only = "".join(ch for ch in quote if ch.isdigit())
        return int(digits_only[-1]) if digits_only else 0

    def _update_markov(self, new_digit: int) -> None:
        """Update the N-gram transition matrix."""
        buf = list(self._digit_buffer)
        order = self.cfg.markov_order
        if len(buf) < order + 1:
            return
        # Create state from last `order` digits (before the new one)
        state = tuple(buf[-(order + 1):-1])
        if state not in self._transitions:
            self._transitions[state] = {d: 0 for d in range(10)}
        self._transitions[state][new_digit] += 1

    def _markov_prediction(self) -> Tuple[Optional[str], float]:
        """Use the Markov transition matrix to predict next even/odd probability.

        Returns (direction, confidence) or (None, 0).
        """
        buf = list(self._digit_buffer)
        order = self.cfg.markov_order
        if len(buf) < order:
            return None, 0.0

        state = tuple(buf[-order:])
        if state not in self._transitions:
            return None, 0.0

        counts = self._transitions[state]
        total = sum(counts.values())
        if total < 5:  # Need minimum observations for reliability
            return None, 0.0

        p_even = sum(counts[d] for d in range(10) if d % 2 == 0) / total
        bias = abs(p_even - 0.5)
        if bias < 0.05:
            return None, 0.0

        direction = "EVEN" if p_even > 0.5 else "ODD"
        confidence = min(bias / 0.20, 1.0)
        return direction, confidence

    def _momentum_status(self) -> str:
        """Check if the bias is strengthening, stable, or decaying.

        Returns: "strengthening", "stable", or "decaying"
        """
        history = list(self._bias_history)
        w = self.cfg.momentum_decay_window
        if len(history) < w:
            return "stable"

        recent = history[-w:]
        first_half = np.mean(recent[:w // 2])
        second_half = np.mean(recent[w // 2:])

        diff = second_half - first_half
        if diff > 0.01:
            return "strengthening"
        elif diff < -0.01:
            return "decaying"
        return "stable"

    @property
    def bayesian_posterior(self) -> float:
        """Posterior mean of P(signal_correct)."""
        return self._beta_alpha / (self._beta_alpha + self._beta_beta)

    def _bayesian_passes(self) -> bool:
        """Check if the Bayesian confidence gate is satisfied."""
        # Need at least 5 observations before we trust the posterior
        total = self._beta_alpha + self._beta_beta - 2  # subtract prior
        if total < 5:
            return True  # insufficient data, let it pass
        return self.bayesian_posterior >= self.cfg.bayesian_gate

    def _multi_scale_consensus(self) -> Tuple[Optional[str], float, int]:
        """Check multi-scale consensus across all window analysers.

        Returns: (agreed_direction, avg_confidence, n_agreeing).
        """
        warmed = [a for a in self._analysers if a.warmed]
        if len(warmed) < 2:
            return None, 0.0, 0

        # Count directions
        directions = [(a.direction, a.confidence, a.window_size) for a in warmed]
        even_count = sum(1 for d, c, _ in directions if d == "EVEN")
        odd_count = sum(1 for d, c, _ in directions if d == "ODD")

        if even_count >= self.cfg.min_consensus:
            agreed = "EVEN"
            agreeing = [(c, w) for d, c, w in directions if d == "EVEN"]
        elif odd_count >= self.cfg.min_consensus:
            agreed = "ODD"
            agreeing = [(c, w) for d, c, w in directions if d == "ODD"]
        else:
            return None, 0.0, 0

        avg_conf = np.mean([c for c, _ in agreeing])
        return agreed, float(avg_conf), len(agreeing)

    def _regime_weights(self, regime: Regime) -> Dict[int, float]:
        """Return weight multipliers for each window size based on regime.

        Mean-reverting → trust short windows more (recent shifts matter)
        Trending → trust long windows more (trend continuation)
        Unknown/choppy → equal weights
        """
        weights = {}
        for w in self.cfg.windows:
            if regime == Regime.MEAN_REVERTING:
                # Short windows get bonus
                weights[w] = 1.3 if w <= 15 else (1.0 if w <= 30 else 0.7)
            elif regime == Regime.TRENDING:
                # Long windows get bonus
                weights[w] = 0.7 if w <= 15 else (1.0 if w <= 30 else 1.3)
            else:
                weights[w] = 1.0
        return weights

    @property
    def warmed(self) -> bool:
        return self._tick_count >= self.cfg.min_warmup

    def score(self, regime_det: Optional[RegimeDetector] = None) -> Optional[SignalSnapshot]:
        """Generate a signal only when all layers converge.

        Returns SignalSnapshot or None.
        """
        if not self.warmed or self._cooldown > 0:
            return None

        # ── Layer 1: Multi-scale consensus ──
        consensus_dir, consensus_conf, n_agree = self._multi_scale_consensus()
        if consensus_dir is None:
            return None

        # ── Layer 2: Markov chain prediction ──
        markov_dir, markov_conf = self._markov_prediction()

        # If Markov has an opinion and it disagrees → no trade
        if markov_dir is not None and markov_dir != consensus_dir and markov_conf > 0.3:
            return None

        # ── Layer 3: Momentum decay check ──
        momentum = self._momentum_status()
        if momentum == "decaying":
            # Bias is fading — don't trade
            return None

        # ── Layer 4: Bayesian confidence gate ──
        if not self._bayesian_passes():
            return None

        # ── Layer 5: Regime-aware weighting ──
        regime = Regime.UNKNOWN
        if regime_det is not None:
            regime = regime_det.current_regime

        weights = self._regime_weights(regime)

        # Compute regime-weighted score
        weighted_scores = []
        for analyser in self._analysers:
            if analyser.warmed and analyser.direction == consensus_dir:
                w = weights.get(analyser.window_size, 1.0)
                weighted_scores.append(analyser.confidence * w)

        if not weighted_scores:
            return None

        composite = float(np.mean(weighted_scores))

        # Markov agreement bonus
        if markov_dir == consensus_dir and markov_conf > 0.1:
            composite = min(composite + markov_conf * 0.15, 1.0)

        # Momentum strengthening bonus
        if momentum == "strengthening":
            composite = min(composite * 1.1, 1.0)

        # Consensus strength bonus (all 3 agree)
        if n_agree == len(self.cfg.windows):
            composite = min(composite * 1.1, 1.0)

        # Save for Bayesian tracking
        self._pending_direction = consensus_dir

        return SignalSnapshot(
            digit_bias=consensus_conf,
            chi_sq_significant=False,
            entropy=0.0,
            momentum=1.0 if momentum == "strengthening" else (0.0 if momentum == "stable" else -1.0),
            regime=regime,
            composite_score=composite,
            direction=consensus_dir,
        )

    def on_trade_result(self, is_win: bool) -> None:
        """Update Bayesian posterior with trade outcome."""
        if is_win:
            self._beta_alpha += 1
        else:
            self._beta_beta += 1

    def on_trade_placed(self) -> None:
        self._cooldown = self.cfg.cooldown_ticks

    def reset(self) -> None:
        for a in self._analysers:
            a._digits.clear()
        self._digit_buffer.clear()
        self._transitions.clear()
        self._bias_history.clear()
        self._beta_alpha = 1.0
        self._beta_beta = 1.0
        self._cooldown = 0
        self._tick_count = 0

    @property
    def status_summary(self) -> str:
        """Detailed status for dashboard."""
        scales = "/".join(
            f"{a.direction[0]}{a.confidence:.0%}"
            for a in self._analysers if a.warmed
        )
        mom = self._momentum_status()
        bayes = self.bayesian_posterior
        m_dir, m_conf = self._markov_prediction()
        markov_str = f"markov={m_dir or '?'}({m_conf:.2f})" if m_dir else "markov=?"
        return f"scales=[{scales}] {mom} bayes={bayes:.2f} {markov_str}"

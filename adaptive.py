"""
Adaptive Strategy — bundles Pulse base signals with three independent gates:

    1. ML filter       — learned P(win) from historical trade logs
    2. Hotness tracker — skip symbols running cold in recent rolling window
    3. Volatility gate — skip high-entropy regimes where digits are most random

The base signal source is Pulse (fast/slow digit agreement). This module only
decides whether to ACCEPT a candidate — it does not generate new signals.
Intent: trade fewer, trade better. Expected behavior is ~80-90% reduction in
trade frequency, concentrated on higher-conviction setups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Tuple

from analysis import VolatilityTracker

logger = logging.getLogger("adaptive")


@dataclass
class VolatilityGateConfig:
    """Skip when ATR percentile rank is in the top `skip_above` fraction."""
    enabled: bool = True
    skip_above: float = 0.75     # e.g. skip top 25% most-volatile ticks
    min_samples: int = 40        # need enough ATR samples to rank meaningfully


class VolatilityGate:
    """Classify whether a symbol is currently in a low/med/high vol regime.

    Uses the realized-vol buffer already populated on VolatilityTracker. High
    realized vol on synthetic indices correlates with more-random digit
    distributions, where any apparent edge decays fastest.
    """

    def __init__(self, cfg: VolatilityGateConfig):
        self.cfg = cfg

    @staticmethod
    def _atr_samples(vt: VolatilityTracker):
        return list(getattr(vt, "_atr_values", []) or [])

    def _percentile(self, samples) -> float:
        current = samples[-1]
        sorted_s = sorted(samples)
        idx = sum(1 for s in sorted_s if s <= current)
        return idx / len(sorted_s)

    def is_high_vol(self, vt: VolatilityTracker) -> bool:
        if not self.cfg.enabled:
            return False
        samples = self._atr_samples(vt)
        if len(samples) < self.cfg.min_samples:
            return False
        return self._percentile(samples) >= self.cfg.skip_above

    def status(self, vt: VolatilityTracker) -> str:
        samples = self._atr_samples(vt)
        if len(samples) < self.cfg.min_samples:
            return f"vol warming {len(samples)}/{self.cfg.min_samples}"
        return f"vol pct={self._percentile(samples):.0%}"


# ---------------------------------------------------------------------------
# Decision bundle — used by bot.py to consult all gates in one call
# ---------------------------------------------------------------------------

@dataclass
class GateDecision:
    accept: bool
    reason: str                  # human-readable status (always populated)
    ml_prob: Optional[float]     # populated when ML filter ran


def evaluate_gates(
    symbol: str,
    contract_type: str,
    ts: float,
    *,
    ml_filter=None,              # MLFilter or None
    hotness=None,                # HotnessTracker or None
    vol_gate: Optional[VolatilityGate] = None,
    vt: Optional[VolatilityTracker] = None,
) -> GateDecision:
    """Run all active gates in order; first failure short-circuits with a reason."""

    # Hotness (cheapest, rule-based)
    if hotness is not None and hotness.is_cold(symbol):
        return GateDecision(False, f"hotness cold ({hotness.status(symbol)})", None)

    # Volatility regime
    if vol_gate is not None and vt is not None and vol_gate.is_high_vol(vt):
        return GateDecision(False, f"skip high-vol ({vol_gate.status(vt)})", None)

    # ML filter (most expensive, runs last)
    ml_prob: Optional[float] = None
    if ml_filter is not None:
        ok, ml_prob = ml_filter.passes(symbol, contract_type, ts)
        if not ok:
            return GateDecision(False, f"ml p={ml_prob:.3f} < {ml_filter.threshold:.2f}", ml_prob)

    return GateDecision(True, "gates ok", ml_prob)

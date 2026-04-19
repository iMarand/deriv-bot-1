"""
Adaptive Strategy — bundles Pulse base signals with three independent gates:

    1. ML filter       — learned P(win) from historical trade logs
    2. Hotness tracker — skip symbols running cold in recent rolling window
    3. Volatility gate — skip high-entropy regimes where digits are most random

The base signal source is Pulse (fast/slow digit agreement). This module only
decides whether to ACCEPT a candidate — it does not generate new signals.

Anti-fearfulness features:
    - Inactivity escape valve: if no trade in `max_idle_seconds`, ALL gates
      are bypassed so the bot never sits idle forever
    - Global cold bypass: if all symbols are cold, force-reset the least-cold
    - Gate statistics: tracks per-gate block counts for diagnostics
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

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
# Gate statistics — tracks which gate is blocking the most
# ---------------------------------------------------------------------------

@dataclass
class GateStats:
    """Cumulative per-gate block/pass counts for diagnostics."""
    hotness_blocks: int = 0
    vol_blocks: int = 0
    ml_blocks: int = 0
    idle_bypasses: int = 0
    cold_resets: int = 0
    total_evaluated: int = 0
    total_passed: int = 0

    def summary(self) -> str:
        return (
            f"gates: {self.total_passed}/{self.total_evaluated} passed | "
            f"blocks: hotness={self.hotness_blocks} vol={self.vol_blocks} ml={self.ml_blocks} | "
            f"bypasses: idle={self.idle_bypasses} cold_reset={self.cold_resets}"
        )


# Module-level stats instance (shared across calls)
_gate_stats = GateStats()


def get_gate_stats() -> GateStats:
    return _gate_stats


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
    last_trade_ts: float = 0.0,  # timestamp of last completed trade
    max_idle_seconds: float = 600.0,  # 10 minutes — bypass all gates if idle this long
    all_symbols: list[str] | None = None,  # for global cold bypass
) -> GateDecision:
    """Run all active gates in order; first failure short-circuits with a reason.

    Anti-fearfulness: if no trade has happened in `max_idle_seconds`, ALL gates
    are bypassed and the candidate is accepted regardless of gate verdicts.
    """
    stats = _gate_stats
    stats.total_evaluated += 1

    # ── Inactivity escape valve ──
    if last_trade_ts > 0 and ts > 0:
        idle = ts - last_trade_ts
        if idle >= max_idle_seconds:
            stats.idle_bypasses += 1
            stats.total_passed += 1
            logger.warning(
                "IDLE BYPASS: %s %s — no trade for %ds (limit=%ds), bypassing all gates",
                symbol, contract_type, int(idle), int(max_idle_seconds),
            )
            return GateDecision(True, f"idle bypass ({int(idle)}s)", None)

    # ── Global cold bypass ──
    if hotness is not None and all_symbols and hotness.all_cold(all_symbols):
        reset_sym = hotness.force_reset_least_cold(all_symbols)
        if reset_sym:
            stats.cold_resets += 1
            logger.warning("All symbols cold — reset %s, allowing trade through", reset_sym)

    # Hotness (cheapest, rule-based)
    if hotness is not None and hotness.is_cold(symbol):
        stats.hotness_blocks += 1
        return GateDecision(False, f"hotness cold ({hotness.status(symbol)})", None)

    # Volatility regime
    if vol_gate is not None and vt is not None and vol_gate.is_high_vol(vt):
        stats.vol_blocks += 1
        return GateDecision(False, f"skip high-vol ({vol_gate.status(vt)})", None)

    # ML filter (most expensive, runs last)
    ml_prob: Optional[float] = None
    if ml_filter is not None:
        ok, ml_prob = ml_filter.passes(symbol, contract_type, ts)
        if not ok:
            stats.ml_blocks += 1
            return GateDecision(False, f"ml p={ml_prob:.3f} < {ml_filter.threshold:.2f}", ml_prob)

    stats.total_passed += 1
    return GateDecision(True, "gates ok", ml_prob)

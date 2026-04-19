"""
Runtime ML Filter — gates candidate signals from alphabloom / pulse / ensemble.

Loads the artifact produced by train_filter.py and, for each candidate trade,
returns P(win). The bot skips candidates below the configured threshold.

This is a FILTER, not a signal generator: the base strategy still decides WHAT
to trade. The filter only decides WHETHER to trade it.

Anti-fearfulness features:
  - Inactivity bypass: if no trade has passed in `max_idle_minutes`, threshold
    is temporarily lowered to break deadlocks
  - Pass-rate floor: if cumulative pass rate drops below `min_pass_rate`, the
    threshold auto-reduces until trades flow again
  - Threshold never drops below `absolute_floor` to maintain basic quality
"""

from __future__ import annotations

import logging
import pickle
import time
from pathlib import Path
from typing import Optional

import numpy as np

from ml_features import FeatureBuilder, FEATURE_NAMES

logger = logging.getLogger("ml_filter")


class MLFilter:
    def __init__(
        self,
        model_path: str,
        threshold: Optional[float] = None,
        max_idle_minutes: float = 10.0,
        min_pass_rate: float = 0.05,
        idle_threshold_decay: float = 0.85,
        absolute_floor: float = 0.45,
    ):
        path = Path(model_path)
        if not path.exists():
            raise FileNotFoundError(f"ML model not found: {path}. Run: py train_filter.py")

        with open(path, "rb") as f:
            bundle = pickle.load(f)

        # Schema check so stale models fail loud
        if bundle.get("feature_names") != FEATURE_NAMES:
            raise ValueError(
                "ML model feature schema mismatch — retrain with current ml_features.py"
            )

        self._bundle = bundle
        self._model = bundle["model"]
        # CLI threshold overrides the one baked in at train time
        self._base_threshold = float(threshold) if threshold is not None else float(bundle["threshold"])
        self.threshold = self._base_threshold
        self._fb = FeatureBuilder()

        # Anti-fearfulness parameters
        self._max_idle_seconds = max_idle_minutes * 60.0
        self._min_pass_rate = min_pass_rate
        self._idle_threshold_decay = idle_threshold_decay
        self._absolute_floor = absolute_floor

        # Stats
        self.n_evaluated = 0
        self.n_passed = 0
        self.n_blocked = 0
        self._last_pass_ts: float = time.time()
        self._idle_bypass_count: int = 0
        self._passrate_bypass_count: int = 0

        logger.info(
            "MLFilter loaded: kind=%s threshold=%.3f trained_on=%d base_wr=%.3f "
            "idle_bypass=%sm floor=%.3f",
            self._model.get("kind"),
            self.threshold,
            bundle.get("n_train", 0),
            bundle.get("base_wr_train", 0.0),
            max_idle_minutes,
            absolute_floor,
        )

    # ------------------------------------------------------------------
    # Feature-state updates — call on every completed trade so the filter
    # stays in sync with reality
    # ------------------------------------------------------------------

    def on_trade_result(self, symbol: str, contract_type: str, ts: float, is_win: bool) -> None:
        self._fb.record(symbol, contract_type, ts, is_win)

    # ------------------------------------------------------------------
    # Dynamic threshold adjustment (anti-fearfulness)
    # ------------------------------------------------------------------

    def _effective_threshold(self) -> float:
        """Compute the current effective threshold, potentially relaxed.

        Two mechanisms prevent the filter from blocking everything forever:
        1. Inactivity bypass — if no trade passed in a long time, lower threshold
        2. Pass-rate floor — if cumulative pass rate is too low, lower threshold
        """
        thr = self._base_threshold
        now = time.time()
        relaxed = False

        # 1. Inactivity bypass
        idle_seconds = now - self._last_pass_ts
        if idle_seconds > self._max_idle_seconds:
            # The longer we're idle, the more we relax (down to floor)
            idle_factor = self._idle_threshold_decay
            # Apply multiple decay steps for extended idleness
            extra_periods = int(idle_seconds / self._max_idle_seconds) - 1
            for _ in range(min(extra_periods, 5)):
                idle_factor *= self._idle_threshold_decay
            thr = max(thr * idle_factor, self._absolute_floor)
            relaxed = True

        # 2. Pass-rate floor
        if self.n_evaluated > 50 and self.pass_rate < self._min_pass_rate:
            thr = max(thr * 0.90, self._absolute_floor)
            relaxed = True

        if relaxed and thr < self._base_threshold:
            logger.debug(
                "ML threshold relaxed: base=%.3f effective=%.3f idle=%ds pass_rate=%.1f%%",
                self._base_threshold, thr, int(idle_seconds), self.pass_rate * 100,
            )

        return thr

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict_proba(self, symbol: str, contract_type: str, ts: Optional[float] = None) -> float:
        if ts is None:
            ts = time.time()
        feat = self._fb.features_for(symbol, contract_type, ts)
        X = np.array([feat], dtype=np.float32)

        kind = self._model.get("kind")
        if kind == "logreg":
            Xs = self._model["scaler"].transform(X)
            p = float(self._model["clf"].predict_proba(Xs)[0, 1])
        else:
            p = float(self._model["clf"].predict_proba(X)[0, 1])
        return p

    def passes(self, symbol: str, contract_type: str, ts: Optional[float] = None) -> tuple[bool, float]:
        p = self.predict_proba(symbol, contract_type, ts)
        self.n_evaluated += 1

        # Use dynamic threshold instead of fixed
        effective_thr = self._effective_threshold()
        self.threshold = effective_thr  # expose for logging
        ok = p >= effective_thr

        if ok:
            self.n_passed += 1
            self._last_pass_ts = time.time()
            # If threshold was relaxed, log it
            if effective_thr < self._base_threshold:
                self._idle_bypass_count += 1
                logger.info(
                    "ML BYPASS PASS: p=%.3f >= relaxed_thr=%.3f (base=%.3f) bypass #%d",
                    p, effective_thr, self._base_threshold, self._idle_bypass_count,
                )
        else:
            self.n_blocked += 1
        return ok, p

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    @property
    def pass_rate(self) -> float:
        return (self.n_passed / self.n_evaluated) if self.n_evaluated else 0.0

    def summary(self) -> str:
        effective = self._effective_threshold()
        extra = ""
        if effective < self._base_threshold:
            extra = f" (relaxed from {self._base_threshold:.2f})"
        return (
            f"ml_filter: {self.n_passed}/{self.n_evaluated} passed "
            f"({self.pass_rate:.1%}) @ threshold {effective:.2f}{extra} "
            f"| bypasses: idle={self._idle_bypass_count} passrate={self._passrate_bypass_count}"
        )

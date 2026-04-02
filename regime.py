"""
Hidden Markov Model — Regime Detection (v2 — hardened)

Fixes from v1:
- Input data validation (NaN/Inf/zero-variance guards)
- Graceful fallback on ANY HMM failure (no more noisy warnings)
- Sticky regime: only switch if HMM agrees for N consecutive ticks
- Reduced n_iter + diag covariance to avoid non-convergence
- Automatic 2-regime fallback if 3-regime fails
- Auto-disables HMM after repeated failures
"""

from __future__ import annotations

import logging
import warnings
from collections import deque
from enum import IntEnum
from typing import Optional

import numpy as np

from config import HMMConfig

logger = logging.getLogger(__name__)

try:
    from hmmlearn.hmm import GaussianHMM
    HMM_AVAILABLE = True
except ImportError:
    HMM_AVAILABLE = False
    logger.info("hmmlearn not installed — using volatility-ratio regime detector")


class Regime(IntEnum):
    UNKNOWN = -1
    MEAN_REVERTING = 0
    TRENDING = 1
    CHOPPY = 2


class RegimeDetector:
    """Detect market regime using a Gaussian HMM on returns (hardened v2)."""

    def __init__(self, cfg: HMMConfig):
        self.cfg = cfg
        self.returns_buf: deque[float] = deque(maxlen=cfg.lookback)
        self._model: Optional[object] = None
        self._tick_count: int = 0
        self.current_regime: Regime = Regime.UNKNOWN
        self._regime_labels: dict = {}
        self._consecutive_regime: int = 0
        self._pending_regime: Regime = Regime.UNKNOWN
        self._sticky_threshold: int = 3
        self._fit_failures: int = 0
        self._max_fit_failures: int = 5

    def update(self, ret: float) -> Regime:
        if not np.isfinite(ret):
            return self.current_regime

        self.returns_buf.append(ret)
        self._tick_count += 1

        if len(self.returns_buf) < self.cfg.lookback:
            return Regime.UNKNOWN

        if self._tick_count % self.cfg.retrain_interval == 0:
            self._fit()

        raw_regime = self._predict()

        if raw_regime == self._pending_regime:
            self._consecutive_regime += 1
        else:
            self._pending_regime = raw_regime
            self._consecutive_regime = 1

        if self._consecutive_regime >= self._sticky_threshold:
            self.current_regime = raw_regime

        return self.current_regime

    def _validate_data(self, X: np.ndarray) -> bool:
        if len(X) < 50:
            return False
        if not np.all(np.isfinite(X)):
            return False
        if np.std(X) < 1e-12:
            return False
        return True

    def _fit(self) -> None:
        X = np.array(self.returns_buf).reshape(-1, 1)

        if not self._validate_data(X):
            return

        if not HMM_AVAILABLE or self._fit_failures >= self._max_fit_failures:
            return

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            success = self._try_fit(X, self.cfg.n_regimes)
            if not success and self.cfg.n_regimes > 2:
                success = self._try_fit(X, 2)
            if not success:
                self._fit_failures += 1
                if self._fit_failures >= self._max_fit_failures:
                    logger.info(
                        f"HMM disabled after {self._fit_failures} failures — "
                        "using volatility-ratio fallback"
                    )

    def _try_fit(self, X: np.ndarray, n_components: int) -> bool:
        try:
            model = GaussianHMM(
                n_components=n_components,
                covariance_type="diag",
                n_iter=30,
                tol=0.1,
                random_state=42,
            )
            model.fit(X)

            if not np.all(np.isfinite(model.means_)):
                return False
            covars = model.covars_.flatten()[:n_components]
            if not np.all(np.isfinite(covars)) or np.any(covars <= 0):
                return False

            self._model = model
            self._assign_labels(model, n_components)
            return True
        except Exception:
            return False

    def _assign_labels(self, model, n_components: int) -> None:
        variances = model.covars_.flatten()[:n_components]
        order = np.argsort(variances)
        mapping = {}
        if n_components >= 3:
            mapping[order[0]] = Regime.MEAN_REVERTING
            mapping[order[-1]] = Regime.CHOPPY
            for idx in order[1:-1]:
                mapping[idx] = Regime.TRENDING
        elif n_components == 2:
            mapping[order[0]] = Regime.MEAN_REVERTING
            mapping[order[1]] = Regime.CHOPPY
        else:
            mapping[0] = Regime.UNKNOWN
        self._regime_labels = mapping

    def _predict(self) -> Regime:
        X = np.array(self.returns_buf).reshape(-1, 1)

        if HMM_AVAILABLE and self._model is not None:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    states = self._model.predict(X)
                    last_state = int(states[-1])
                    result = self._regime_labels.get(last_state, Regime.UNKNOWN)
                    if result != Regime.UNKNOWN:
                        return result
            except Exception:
                pass

        # Fallback: volatility-ratio classifier
        recent = np.array(list(self.returns_buf)[-30:])
        if len(recent) < 2:
            return Regime.UNKNOWN
        vol_recent = float(np.std(recent))
        vol_full = float(np.std(X))
        if vol_full < 1e-12:
            return Regime.UNKNOWN

        ratio = vol_recent / vol_full
        if ratio < 0.7:
            return Regime.MEAN_REVERTING
        elif ratio > 1.3:
            return Regime.CHOPPY
        else:
            return Regime.TRENDING

    @property
    def regime_name(self) -> str:
        return self.current_regime.name

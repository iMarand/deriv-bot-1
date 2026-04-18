"""
Runtime ML Filter — gates candidate signals from alphabloom / pulse / ensemble.

Loads the artifact produced by train_filter.py and, for each candidate trade,
returns P(win). The bot skips candidates below the configured threshold.

This is a FILTER, not a signal generator: the base strategy still decides WHAT
to trade. The filter only decides WHETHER to trade it.
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
    def __init__(self, model_path: str, threshold: Optional[float] = None):
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
        self.threshold = float(threshold) if threshold is not None else float(bundle["threshold"])
        self._fb = FeatureBuilder()

        # Stats
        self.n_evaluated = 0
        self.n_passed = 0
        self.n_blocked = 0

        logger.info(
            "MLFilter loaded: kind=%s threshold=%.3f trained_on=%d base_wr=%.3f",
            self._model.get("kind"),
            self.threshold,
            bundle.get("n_train", 0),
            bundle.get("base_wr_train", 0.0),
        )

    # ------------------------------------------------------------------
    # Feature-state updates — call on every completed trade so the filter
    # stays in sync with reality
    # ------------------------------------------------------------------

    def on_trade_result(self, symbol: str, contract_type: str, ts: float, is_win: bool) -> None:
        self._fb.record(symbol, contract_type, ts, is_win)

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
        ok = p >= self.threshold
        if ok:
            self.n_passed += 1
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
        return (
            f"ml_filter: {self.n_passed}/{self.n_evaluated} passed "
            f"({self.pass_rate:.1%}) @ threshold {self.threshold:.2f}"
        )

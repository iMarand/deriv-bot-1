"""
Feature builder shared by offline trainer (train_filter.py) and runtime gate
(ml_filter.py). Both MUST produce identical feature vectors so the model sees
the same shape it was trained on.

Features (metadata-only — no tick stream needed):
    one-hot symbol (10)
    is_odd                          (1/0)
    hour_sin, hour_cos              (cyclical time-of-day)
    dow                             (0..6 normalized)
    prev_win_sym                    (was previous trade on this symbol a win?)
    prev_win_global                 (was the immediately previous trade a win?)
    streak_losses_global            (current global loss streak length, clipped)
    sym_wr_10, sym_wr_50            (rolling win rate on this symbol)
    global_wr_50                    (overall rolling win rate)
    sec_since_last_sym              (log-seconds since last trade on this symbol)
    sec_since_last_global           (log-seconds since last trade overall)
"""

from __future__ import annotations

import math
from collections import deque
from typing import Deque, Dict, List

SYMBOLS: List[str] = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]
SYM_INDEX: Dict[str, int] = {s: i for i, s in enumerate(SYMBOLS)}

FEATURE_NAMES: List[str] = (
    [f"sym_{s}" for s in SYMBOLS]
    + [
        "is_odd",
        "hour_sin", "hour_cos",
        "dow_norm",
        "prev_win_sym",
        "prev_win_global",
        "streak_losses_global",
        "sym_wr_10", "sym_wr_50",
        "global_wr_50",
        "log_sec_since_sym",
        "log_sec_since_global",
    ]
)

N_FEATURES = len(FEATURE_NAMES)


class FeatureBuilder:
    """Rolling state used to compute features for the NEXT trade.

    Call `features_for(...)` BEFORE the trade (to get features as seen at
    decision time), then call `record(..., is_win)` AFTER the result to update
    state.
    """

    def __init__(self, short_window: int = 10, long_window: int = 50):
        self.short = short_window
        self.long = long_window

        # per-symbol deques of recent wins (1/0)
        self._sym_hist: Dict[str, Deque[int]] = {s: deque(maxlen=long_window) for s in SYMBOLS}
        # per-symbol last-trade timestamp
        self._sym_last_ts: Dict[str, float] = {}
        # global rolling
        self._global_hist: Deque[int] = deque(maxlen=long_window)
        self._global_last_ts: float = 0.0
        self._global_loss_streak: int = 0

    # -- feature extraction -------------------------------------------------

    def features_for(self, symbol: str, contract_type: str, ts: float) -> List[float]:
        vec = [0.0] * N_FEATURES

        # one-hot symbol
        sidx = SYM_INDEX.get(symbol)
        if sidx is not None:
            vec[sidx] = 1.0

        # scalar features (indices follow FEATURE_NAMES order)
        base = len(SYMBOLS)
        is_odd = 1.0 if contract_type == "DIGITODD" else 0.0
        vec[base + 0] = is_odd

        # cyclical hour-of-day from unix timestamp
        if ts > 0:
            lt = self._local_time(ts)
            hour = lt[3] + lt[4] / 60.0
            vec[base + 1] = math.sin(2 * math.pi * hour / 24.0)
            vec[base + 2] = math.cos(2 * math.pi * hour / 24.0)
            vec[base + 3] = lt[6] / 6.0  # dow 0..6 → 0..1
        # else 0

        # prev wins
        sym_hist = self._sym_hist.get(symbol)
        if sym_hist and len(sym_hist) > 0:
            vec[base + 4] = float(sym_hist[-1])
        if len(self._global_hist) > 0:
            vec[base + 5] = float(self._global_hist[-1])
        vec[base + 6] = float(min(self._global_loss_streak, 20)) / 20.0

        # rolling WRs
        if sym_hist and len(sym_hist) > 0:
            recent10 = list(sym_hist)[-self.short:]
            vec[base + 7] = sum(recent10) / len(recent10)
            vec[base + 8] = sum(sym_hist) / len(sym_hist)
        else:
            vec[base + 7] = 0.5
            vec[base + 8] = 0.5

        if len(self._global_hist) > 0:
            vec[base + 9] = sum(self._global_hist) / len(self._global_hist)
        else:
            vec[base + 9] = 0.5

        # time since last
        last_sym = self._sym_last_ts.get(symbol, 0.0)
        sec_sym = max(0.0, ts - last_sym) if (ts > 0 and last_sym > 0) else 60.0
        sec_glb = max(0.0, ts - self._global_last_ts) if (ts > 0 and self._global_last_ts > 0) else 60.0
        vec[base + 10] = math.log1p(sec_sym)
        vec[base + 11] = math.log1p(sec_glb)

        return vec

    # -- state update -------------------------------------------------------

    def record(self, symbol: str, contract_type: str, ts: float, is_win: bool) -> None:
        flag = 1 if is_win else 0
        sh = self._sym_hist.setdefault(symbol, deque(maxlen=self.long))
        sh.append(flag)
        self._global_hist.append(flag)

        if is_win:
            self._global_loss_streak = 0
        else:
            self._global_loss_streak += 1

        if ts > 0:
            self._sym_last_ts[symbol] = ts
            self._global_last_ts = ts

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _local_time(ts: float):
        """Return time.struct_time; UTC to keep training/inference aligned."""
        import time as _t
        try:
            return _t.gmtime(ts)
        except Exception:
            return _t.gmtime(0)

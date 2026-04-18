"""
Per-symbol rolling win-rate tracker.

Purpose: when a symbol is running cold (e.g. last 50 trades on it win <48%),
temporarily skip it. When it's running hot (>52%), let the base strategy
trade it freely.

This is pure runtime state — no training, no model.
"""

from __future__ import annotations

from collections import deque
from typing import Deque, Dict


class HotnessTracker:
    def __init__(
        self,
        window: int = 50,
        min_trades: int = 15,
        cold_threshold: float = 0.48,
        hot_threshold: float = 0.52,
    ):
        self.window = window
        self.min_trades = min_trades
        self.cold_threshold = cold_threshold
        self.hot_threshold = hot_threshold
        self._hist: Dict[str, Deque[int]] = {}

    def on_trade_result(self, symbol: str, is_win: bool) -> None:
        dq = self._hist.setdefault(symbol, deque(maxlen=self.window))
        dq.append(1 if is_win else 0)

    def win_rate(self, symbol: str) -> float:
        dq = self._hist.get(symbol)
        if not dq:
            return 0.5
        return sum(dq) / len(dq)

    def sample_size(self, symbol: str) -> int:
        return len(self._hist.get(symbol, ()))

    def is_cold(self, symbol: str) -> bool:
        """Only call a symbol cold after we've seen min_trades — early noise is not signal."""
        if self.sample_size(symbol) < self.min_trades:
            return False
        return self.win_rate(symbol) < self.cold_threshold

    def is_hot(self, symbol: str) -> bool:
        if self.sample_size(symbol) < self.min_trades:
            return False
        return self.win_rate(symbol) >= self.hot_threshold

    def status(self, symbol: str) -> str:
        n = self.sample_size(symbol)
        if n < self.min_trades:
            return f"warming {n}/{self.min_trades}"
        wr = self.win_rate(symbol)
        tag = "HOT" if wr >= self.hot_threshold else ("COLD" if wr < self.cold_threshold else "neutral")
        return f"{tag} {wr:.1%} (n={n})"

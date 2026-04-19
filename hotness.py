"""
Per-symbol rolling win-rate tracker.

Purpose: when a symbol is running cold (e.g. last 100 trades on it win <43%),
temporarily skip it. When it's running hot (>52%), let the base strategy
trade it freely.

Anti-fearfulness features:
  - Recovery probing: cold symbols still get 1-in-N candidates through
  - Global bypass: if ALL symbols are cold, force-reset the least-cold one
  - Wider window + lower threshold than original to avoid premature blocking

This is pure runtime state — no training, no model.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Deque, Dict, Optional

logger = logging.getLogger("hotness")


class HotnessTracker:
    def __init__(
        self,
        window: int = 100,            # wider window — less reactive to short streaks
        min_trades: int = 25,          # need more evidence before blocking
        cold_threshold: float = 0.43,  # much harder to trigger (was 0.48)
        hot_threshold: float = 0.52,
        probe_interval: int = 20,      # when cold, allow 1 in N candidates through
    ):
        self.window = window
        self.min_trades = min_trades
        self.cold_threshold = cold_threshold
        self.hot_threshold = hot_threshold
        self.probe_interval = probe_interval
        self._hist: Dict[str, Deque[int]] = {}
        self._skip_count: Dict[str, int] = {}  # tracks consecutive skips per symbol
        self._probe_count: int = 0              # total probes allowed through

    def on_trade_result(self, symbol: str, is_win: bool) -> None:
        dq = self._hist.setdefault(symbol, deque(maxlen=self.window))
        dq.append(1 if is_win else 0)
        # A trade happened on this symbol — reset its skip counter
        self._skip_count[symbol] = 0

    def win_rate(self, symbol: str) -> float:
        dq = self._hist.get(symbol)
        if not dq:
            return 0.5
        return sum(dq) / len(dq)

    def sample_size(self, symbol: str) -> int:
        return len(self._hist.get(symbol, ()))

    def is_cold(self, symbol: str) -> bool:
        """Only call a symbol cold after we've seen min_trades — early noise is not signal.

        Recovery probing: even when cold, every `probe_interval` skips we let one
        candidate through so the bot can discover if conditions have improved.
        """
        if self.sample_size(symbol) < self.min_trades:
            return False

        wr = self.win_rate(symbol)
        if wr >= self.cold_threshold:
            self._skip_count[symbol] = 0
            return False

        # Symbol IS cold — but check if it's time for a recovery probe
        count = self._skip_count.get(symbol, 0)
        self._skip_count[symbol] = count + 1

        if count > 0 and count % self.probe_interval == 0:
            self._probe_count += 1
            logger.info(
                "HOTNESS PROBE: allowing %s through despite cold WR %.1f%% "
                "(probe #%d, %d skips since last trade)",
                symbol, wr * 100, self._probe_count, count,
            )
            return False  # let it through as a probe

        return True

    def is_hot(self, symbol: str) -> bool:
        if self.sample_size(symbol) < self.min_trades:
            return False
        return self.win_rate(symbol) >= self.hot_threshold

    def all_cold(self, symbols: list[str]) -> bool:
        """True if every symbol with sufficient data is cold."""
        evaluated = [s for s in symbols if self.sample_size(s) >= self.min_trades]
        if not evaluated:
            return False
        return all(self.win_rate(s) < self.cold_threshold for s in evaluated)

    def force_reset_least_cold(self, symbols: list[str]) -> Optional[str]:
        """When ALL symbols are cold, reset the least-cold one so the bot can trade.

        Returns the symbol that was reset, or None if no reset was needed.
        """
        evaluated = {
            s: self.win_rate(s)
            for s in symbols
            if self.sample_size(s) >= self.min_trades
        }
        if not evaluated:
            return None

        # Find the symbol closest to the cold threshold (least cold)
        best_sym = max(evaluated, key=evaluated.get)
        best_wr = evaluated[best_sym]

        # Clear its history to give it a fresh start
        old_len = len(self._hist.get(best_sym, ()))
        self._hist[best_sym] = deque(maxlen=self.window)
        self._skip_count[best_sym] = 0
        logger.warning(
            "ALL SYMBOLS COLD — force-resetting %s (was WR=%.1f%%, n=%d) "
            "to prevent total shutdown",
            best_sym, best_wr * 100, old_len,
        )
        return best_sym

    def status(self, symbol: str) -> str:
        n = self.sample_size(symbol)
        if n < self.min_trades:
            return f"warming {n}/{self.min_trades}"
        wr = self.win_rate(symbol)
        tag = "HOT" if wr >= self.hot_threshold else ("COLD" if wr < self.cold_threshold else "neutral")
        skips = self._skip_count.get(symbol, 0)
        probe_info = f" skip={skips}" if tag == "COLD" else ""
        return f"{tag} {wr:.1%} (n={n}{probe_info})"

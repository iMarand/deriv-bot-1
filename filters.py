"""
Time Filter & Shadow-Mode Paper Trading
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import TimeFilterConfig, ShadowConfig

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Time-of-Day Filter
# ─────────────────────────────────────────────
class TimeFilter:
    """Track win-rate by hour-of-day and optionally block weak hours."""

    def __init__(self, cfg: TimeFilterConfig):
        self.cfg = cfg
        self._wins_by_hour: Dict[int, int] = defaultdict(int)
        self._total_by_hour: Dict[int, int] = defaultdict(int)

    def record(self, epoch: float, is_win: bool) -> None:
        hour = time.gmtime(epoch).tm_hour
        self._total_by_hour[hour] += 1
        if is_win:
            self._wins_by_hour[hour] += 1

    def win_rate_by_hour(self) -> Dict[int, Optional[float]]:
        result = {}
        for h in range(24):
            total = self._total_by_hour[h]
            if total >= self.cfg.min_sample_per_hour:
                result[h] = self._wins_by_hour[h] / total
            else:
                result[h] = None
        return result

    def should_trade_now(self) -> bool:
        """Return False if current hour is a known weak hour."""
        if not self.cfg.enabled:
            return True
        current_hour = time.gmtime().tm_hour
        if current_hour in self.cfg.weak_hours_utc:
            wr = self.win_rate_by_hour().get(current_hour)
            if wr is not None and wr < 0.48:
                logger.info(f"Time filter: skipping hour {current_hour} (win rate {wr:.1%})")
                return False
        return True

    def auto_update_weak_hours(self) -> None:
        """Dynamically update weak hours based on accumulated data."""
        wr = self.win_rate_by_hour()
        weak = []
        for h, rate in wr.items():
            if rate is not None and rate < 0.45:
                weak.append(h)
        if weak:
            self.cfg.weak_hours_utc = weak
            logger.info(f"Time filter auto-updated weak hours: {weak}")


# ─────────────────────────────────────────────
# Shadow Mode (Paper Trading A/B)
# ─────────────────────────────────────────────
@dataclass
class ShadowTrade:
    epoch: float
    symbol: str
    contract_type: str
    stake: float
    would_profit: float
    would_win: bool


class ShadowTrader:
    """
    Paper-trade with alternative parameters alongside the live strategy.
    Tracks what *would* have happened with the shadow config.
    """

    def __init__(self, cfg: ShadowConfig):
        self.cfg = cfg
        self.trades: List[ShadowTrade] = []
        self.net_pnl: float = 0.0
        self.wins: int = 0

    def record(self, trade: ShadowTrade) -> None:
        self.trades.append(trade)
        self.net_pnl += trade.would_profit
        if trade.would_win:
            self.wins += 1

    @property
    def win_rate(self) -> Optional[float]:
        if not self.trades:
            return None
        return self.wins / len(self.trades)

    @property
    def ready_to_compare(self) -> bool:
        return len(self.trades) >= self.cfg.min_shadow_trades

    def summary(self) -> Dict:
        return {
            "shadow_trades": len(self.trades),
            "shadow_pnl": round(self.net_pnl, 2),
            "shadow_win_rate": self.win_rate,
            "ready": self.ready_to_compare,
        }

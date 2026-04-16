"""
Money Management Engine
- Kelly Criterion adaptive sizing
- Controlled Martingale with caps
- Drawdown-responsive circuit breaker
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional

from config import MartingaleConfig, KellyConfig, CircuitBreakerConfig

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# Trade Record
# ─────────────────────────────────────────────
@dataclass
class TradeRecord:
    timestamp: float
    symbol: str
    contract_type: str
    stake: float
    profit: float       # positive = win, negative = loss
    payout: float       # total payout on win
    is_win: bool


# ─────────────────────────────────────────────
# Kelly Criterion
# ─────────────────────────────────────────────
class KellyCalculator:
    """Estimate optimal stake fraction using the Kelly Criterion."""

    def __init__(self, cfg: KellyConfig):
        self.cfg = cfg
        self.history: deque[TradeRecord] = deque(maxlen=cfg.lookback_trades)

    def record(self, trade: TradeRecord) -> None:
        self.history.append(trade)

    @property
    def win_rate(self) -> Optional[float]:
        if len(self.history) < self.cfg.min_trades_for_kelly:
            return None
        wins = sum(1 for t in self.history if t.is_win)
        return wins / len(self.history)

    @property
    def avg_win_loss_ratio(self) -> Optional[float]:
        """Average win amount / average loss amount."""
        wins = [t.profit for t in self.history if t.is_win and t.profit > 0]
        losses = [abs(t.profit) for t in self.history if not t.is_win and t.profit < 0]
        if not wins or not losses:
            return None
        return (sum(wins) / len(wins)) / (sum(losses) / len(losses))

    def kelly_fraction(self) -> Optional[float]:
        """Return the recommended stake as a fraction of equity (half-Kelly)."""
        if not self.cfg.enabled:
            return None
        p = self.win_rate
        b = self.avg_win_loss_ratio
        if p is None or b is None:
            return None
        # Kelly formula: f* = (bp - (1-p)) / b
        f_star = (b * p - (1 - p)) / b
        if f_star <= 0:
            return 0.0  # no edge
        return f_star * self.cfg.kelly_fraction  # half-Kelly


# ─────────────────────────────────────────────
# Martingale Engine
# ─────────────────────────────────────────────
class MartingaleEngine:
    """Controlled Martingale with strict caps."""

    def __init__(self, cfg: MartingaleConfig):
        self.cfg = cfg
        self.current_stake: float = cfg.base_stake_usd
        self.consecutive_losses: int = 0
        self.net_pnl: float = 0.0
        self._stopped: bool = False
        self._total_resets: int = 0
        self._cooldown_remaining: int = 0
        self._start_equity: float = 1000.0  # updated on first stake call
        self.last_stake_reason: str = "base"
        self.disable_risk_engine: bool = False  # set by DerivBot when --disable-risk-engine is passed

    @property
    def is_stopped(self) -> bool:
        return self._stopped

    def next_stake(self, equity: float, kelly_fraction: Optional[float] = None, skip_cooldown: bool = False) -> float:
        """Compute the next stake, respecting all caps."""
        if self._stopped:
            return 0.0

        # Cooldown: after a max-loss streak reset, wait before trading again
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            if not skip_cooldown:
                self.last_stake_reason = "cooldown"
                return 0.0  # skip this trade

        # Kelly-informed base
        base = self.cfg.base_stake_usd
        sizing_mode = "base"
        if kelly_fraction is not None and kelly_fraction > 0:
            kelly_stake = equity * kelly_fraction
            base = max(self.cfg.base_stake_usd, min(kelly_stake, self.cfg.max_stake_usd))
            sizing_mode = f"kelly({kelly_fraction:.4f})"

        # Bankroll fraction cap
        max_base = equity * self.cfg.bankroll_fraction
        base = min(base, max_base)

        # Adaptive Martingale: softer multiplier after repeated resets
        # (bypassed when risk engine is disabled — multiplier stays fixed at cfg value)
        effective_mult = self.cfg.multiplier
        if not self.disable_risk_engine and self._total_resets >= 2:
            # Reduce aggression: drop multiplier toward 1.5 after multiple resets
            effective_mult = max(1.5, self.cfg.multiplier - 0.1 * self._total_resets)

        # Martingale progression
        stake = base * (effective_mult ** self.consecutive_losses)
        if self.consecutive_losses > 0:
            sizing_mode = f"{sizing_mode}+martingale(x{effective_mult:.2f}^{self.consecutive_losses})"

        # Drawdown-aware cap: reduce max stake when equity is below start
        equity_ratio = min(1.0, equity / max(self._start_equity, 1))
        dynamic_max = self.cfg.max_stake_usd * equity_ratio

        # Hard caps
        stake = min(stake, dynamic_max)
        stake = min(stake, equity * 0.25)  # tightened from 50% to 25%

        # Never allow a single stake to push net P/L past the configured loss limit.
        if self.cfg.loss_limit_usd is not None:
            remaining_loss_room = self.net_pnl - self.cfg.loss_limit_usd
            if remaining_loss_room <= 0:
                logger.warning("Loss room exhausted before new trade; stopping")
                self._stopped = True
                self.last_stake_reason = "loss-room-exhausted"
                return 0.0
            if remaining_loss_room < 0.35:
                logger.warning(
                    "Remaining loss room $%.2f is below minimum stake; stopping",
                    remaining_loss_room,
                )
                self._stopped = True
                self.last_stake_reason = "loss-room-below-minimum"
                return 0.0
            if stake > remaining_loss_room:
                stake = remaining_loss_room
                sizing_mode = f"{sizing_mode}+loss-cap"

        stake = max(stake, 0.35)           # Deriv minimum ~$0.35
        self.last_stake_reason = sizing_mode

        return round(stake, 2)

    def on_result(self, trade: TradeRecord) -> None:
        """Update state after a trade result."""
        self.net_pnl += trade.profit

        if trade.is_win:
            self.consecutive_losses = 0
            self.current_stake = self.cfg.base_stake_usd
        else:
            self.consecutive_losses += 1
            if self.consecutive_losses >= self.cfg.max_consecutive_losses:
                self._total_resets += 1
                if not self.disable_risk_engine:
                    # Exponential cooldown: wait longer after each reset
                    self._cooldown_remaining = min(3 * self._total_resets, 15)
                    logger.warning(
                        f"Max consecutive losses ({self.cfg.max_consecutive_losses}) hit — "
                        f"reset #{self._total_resets}, cooldown {self._cooldown_remaining} ticks"
                    )
                    self.consecutive_losses = 0
                    self.current_stake = self.cfg.base_stake_usd
                else:
                    logger.warning(
                        f"Max consecutive losses ({self.cfg.max_consecutive_losses}) hit — "
                        f"risk engine disabled, martingale continues (streak={self.consecutive_losses})"
                    )

        # Profit/loss stop (only when limits are configured)
        if self.cfg.profit_target_usd is not None and self.net_pnl >= self.cfg.profit_target_usd:
            logger.info(f"Profit target reached: ${self.net_pnl:.2f}")
            self._stopped = True
        if self.cfg.loss_limit_usd is not None and self.net_pnl <= self.cfg.loss_limit_usd:
            logger.warning(f"Loss limit reached: ${self.net_pnl:.2f}")
            self._stopped = True

    def reset(self) -> None:
        self.current_stake = self.cfg.base_stake_usd
        self.consecutive_losses = 0
        self.net_pnl = 0.0
        self._stopped = False
        self._total_resets = 0
        self._cooldown_remaining = 0

    @property
    def max_rounds_to_ruin(self) -> float:
        """Theoretical max consecutive losses before loss limit (log formula)."""
        if self.cfg.multiplier <= 1 or self.cfg.loss_limit_usd is None:
            return float("inf")
        limit = abs(self.cfg.loss_limit_usd)
        return math.log(limit / self.cfg.base_stake_usd) / math.log(self.cfg.multiplier)


# ─────────────────────────────────────────────
# Circuit Breaker
# ─────────────────────────────────────────────
class CircuitBreaker:
    """Drawdown-responsive circuit breaker that reduces risk during bad streaks."""

    def __init__(self, cfg: CircuitBreakerConfig):
        self.cfg = cfg
        self.equity_history: deque[float] = deque(maxlen=cfg.equity_ma_window)
        self._cooldown_remaining: int = 0
        self.is_active: bool = False

    def update(self, equity: float) -> None:
        """Feed current equity after each trade."""
        self.equity_history.append(equity)

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            if self._cooldown_remaining == 0:
                self.is_active = False
                logger.info("Circuit breaker cooldown ended")
            return

        if len(self.equity_history) < self.cfg.equity_ma_window:
            return

        ma = sum(self.equity_history) / len(self.equity_history)
        if ma > 0 and (equity - ma) / ma < -self.cfg.drawdown_pct_trigger:
            self.is_active = True
            self._cooldown_remaining = self.cfg.cooldown_ticks
            logger.warning(
                f"Circuit breaker ACTIVATED — equity ${equity:.2f} "
                f"is {self.cfg.drawdown_pct_trigger*100:.0f}%+ below MA ${ma:.2f}"
            )

    def adjust_stake(self, stake: float) -> float:
        """Reduce stake if circuit breaker is active."""
        if self.is_active:
            return round(stake * self.cfg.reduced_stake_fraction, 2)
        return stake

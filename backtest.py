"""
Backtesting & Simulation Engine
- Replay historical ticks
- Monte Carlo stress testing
- Performance metrics: Sharpe, max drawdown, ruin probability, expectancy
"""

from __future__ import annotations

import logging
import math
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import BotConfig
from analysis import DigitTracker, VolatilityTracker
from regime import RegimeDetector
from ensemble import EnsembleScorer
from money import MartingaleEngine, KellyCalculator, TradeRecord

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    sharpe_ratio: float = 0.0
    expectancy: float = 0.0
    ruin_probability: float = 0.0
    win_rate: float = 0.0
    max_consecutive_losses: int = 0
    equity_curve: List[float] = field(default_factory=list)
    pnl_series: List[float] = field(default_factory=list)

    def summary(self) -> Dict:
        return {
            "total_trades": self.total_trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": f"{self.win_rate:.1%}",
            "net_pnl": f"${self.net_pnl:.2f}",
            "max_drawdown": f"${self.max_drawdown:.2f}",
            "sharpe_ratio": f"{self.sharpe_ratio:.3f}",
            "expectancy": f"${self.expectancy:.4f}",
            "max_consecutive_losses": self.max_consecutive_losses,
            "ruin_probability": f"{self.ruin_probability:.1%}",
        }


class Backtester:
    """Replay ticks and simulate the full strategy."""

    def __init__(self, cfg: BotConfig, initial_equity: float = 1000.0):
        self.cfg = cfg
        self.initial_equity = initial_equity

    def run(self, ticks: List[Dict]) -> BacktestResult:
        """
        Run backtest on a list of tick dicts:
        [{"epoch": 123, "quote": "1234.56"}, ...]
        """
        digit = DigitTracker(self.cfg.digit)
        vol = VolatilityTracker(self.cfg.volatility)
        regime_det = RegimeDetector(self.cfg.hmm)
        scorer = EnsembleScorer(self.cfg.ensemble)
        martingale = MartingaleEngine(self.cfg.martingale)
        martingale._start_equity = self.initial_equity
        kelly = KellyCalculator(self.cfg.kelly)

        equity = self.initial_equity
        peak_equity = equity
        max_dd = 0.0
        pnl_series: List[float] = []
        equity_curve: List[float] = [equity]
        consec_losses = 0
        max_consec = 0
        wins = 0
        losses = 0

        # Signal confirmation state
        confirm_count = 0
        confirm_direction = None
        confirm_needed = 2          # must see signal N ticks in a row
        min_trade_spacing = 3       # minimum ticks between trades
        last_trade_tick = -100

        for i, tick in enumerate(ticks):
            quote_str = str(tick["quote"])
            price = float(tick["quote"])

            digit.update(quote_str)
            vol.update(price)

            if len(vol.returns) > 0:
                regime_det.update(float(vol.returns[-1]))

            # Check for trade signal
            signal = scorer.score(digit, vol, regime_det)

            # Signal confirmation: must see threshold-exceeding signal
            # in the SAME direction for `confirm_needed` consecutive ticks
            if signal is not None and signal.composite_score >= self.cfg.ensemble.entry_score_threshold:
                if signal.direction == confirm_direction:
                    confirm_count += 1
                else:
                    confirm_direction = signal.direction
                    confirm_count = 1
            else:
                confirm_count = 0
                confirm_direction = None
                continue

            # Not yet confirmed
            if confirm_count < confirm_needed:
                continue

            # Enforce minimum spacing between trades
            if i - last_trade_tick < min_trade_spacing:
                continue

            # Reset confirmation after acting
            confirm_count = 0

            # Determine contract outcome by looking ahead
            duration = self.cfg.contract.duration
            if i + duration >= len(ticks):
                break

            future_digit = DigitTracker._last_digit(str(ticks[i + duration]["quote"]))
            contract_type = f"DIGIT{signal.direction}"

            if signal.direction == "EVEN":
                is_win = future_digit % 2 == 0
            else:
                is_win = future_digit % 2 != 0

            # Stake
            kf = kelly.kelly_fraction()
            stake = martingale.next_stake(equity, kf)

            # Cooldown: martingale returns 0 during cooldown
            if stake <= 0:
                continue

            last_trade_tick = i

            # P/L (digit contracts typically pay ~90% on win, lose 100% on loss)
            payout_ratio = 0.90
            if is_win:
                profit = stake * payout_ratio
                wins += 1
                consec_losses = 0
            else:
                profit = -stake
                losses += 1
                consec_losses += 1
                max_consec = max(max_consec, consec_losses)

            equity += profit
            pnl_series.append(profit)
            equity_curve.append(equity)

            trade = TradeRecord(
                timestamp=tick.get("epoch", 0),
                symbol="backtest",
                contract_type=contract_type,
                stake=stake,
                profit=profit,
                payout=stake * (1 + payout_ratio) if is_win else 0,
                is_win=is_win,
            )
            kelly.record(trade)
            martingale.on_result(trade)

            peak_equity = max(peak_equity, equity)
            dd = peak_equity - equity
            max_dd = max(max_dd, dd)

            if martingale.is_stopped or equity <= 0:
                break

        total = wins + losses
        result = BacktestResult(
            total_trades=total,
            wins=wins,
            losses=losses,
            net_pnl=equity - self.initial_equity,
            max_drawdown=max_dd,
            win_rate=wins / total if total > 0 else 0,
            max_consecutive_losses=max_consec,
            equity_curve=equity_curve,
            pnl_series=pnl_series,
        )

        # Sharpe ratio (annualized with ~86400 ticks/day assumption)
        if pnl_series:
            arr = np.array(pnl_series)
            if np.std(arr) > 0:
                result.sharpe_ratio = float(np.mean(arr) / np.std(arr) * math.sqrt(252))
            result.expectancy = float(np.mean(arr))

        return result

    def monte_carlo(
        self,
        ticks: List[Dict],
        n_simulations: int = 500,
        shuffle_blocks: int = 50,
    ) -> Dict:
        """
        Monte Carlo stress test: shuffle blocks of ticks and re-run.
        Returns distribution of outcomes.
        """
        results: List[BacktestResult] = []
        tick_array = list(ticks)
        n = len(tick_array)

        for sim in range(n_simulations):
            # Block bootstrap: shuffle blocks of `shuffle_blocks` ticks
            blocks = [
                tick_array[i : i + shuffle_blocks]
                for i in range(0, n, shuffle_blocks)
            ]
            random.shuffle(blocks)
            shuffled = [t for block in blocks for t in block]

            r = self.run(shuffled)
            results.append(r)

        pnls = [r.net_pnl for r in results]
        drawdowns = [r.max_drawdown for r in results]
        ruin_count = sum(1 for r in results if r.net_pnl <= self.cfg.martingale.loss_limit_usd)

        ruin_prob = ruin_count / n_simulations if n_simulations > 0 else 0

        # Update ruin probability in each result
        for r in results:
            r.ruin_probability = ruin_prob

        return {
            "n_simulations": n_simulations,
            "mean_pnl": float(np.mean(pnls)),
            "median_pnl": float(np.median(pnls)),
            "std_pnl": float(np.std(pnls)),
            "worst_pnl": float(np.min(pnls)),
            "best_pnl": float(np.max(pnls)),
            "mean_max_drawdown": float(np.mean(drawdowns)),
            "worst_max_drawdown": float(np.max(drawdowns)),
            "ruin_probability": ruin_prob,
            "p5_pnl": float(np.percentile(pnls, 5)),
            "p95_pnl": float(np.percentile(pnls, 95)),
        }

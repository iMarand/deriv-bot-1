"""
Backtest Runner — Generate synthetic tick data and run the backtester.

Usage:
    python run_backtest.py [--ticks 5000] [--monte-carlo 500]

Since we may not have real Deriv historical data available, this script
can generate synthetic tick data that mimics Deriv synthetic indices
(random walk with controlled volatility).
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import time
from typing import List, Dict

import numpy as np

from config import BotConfig
from backtest import Backtester

logger = logging.getLogger("backtest_runner")


def generate_synthetic_ticks(
    n_ticks: int = 5000,
    base_price: float = 1000.0,
    volatility: float = 0.001,
    seed: int = 42,
) -> List[Dict]:
    """
    Generate synthetic tick data mimicking Deriv's R_100 index.
    Uses a random walk with slight digit-distribution quirks
    to test whether the bot can detect and exploit them.
    """
    rng = np.random.RandomState(seed)
    ticks = []
    price = base_price
    epoch = int(time.time()) - n_ticks

    for i in range(n_ticks):
        # Random walk
        ret = rng.normal(0, volatility)

        # Inject a subtle even-digit bias in ~10% of ticks
        # (simulates transient distribution anomalies)
        if 1000 < i < 2000 or 3000 < i < 3500:
            # Bias: nudge last digit toward even
            if random.random() < 0.15:
                price_str = f"{price:.2f}"
                last_d = int(price_str[-1])
                if last_d % 2 != 0:
                    ret += 0.01 * (1 if random.random() > 0.5 else -1)

        price *= (1 + ret)
        price = max(price, 1.0)  # floor
        quote_str = f"{price:.2f}"

        ticks.append({
            "epoch": epoch + i,
            "quote": quote_str,
            "symbol": "R_100",
        })

    return ticks


def main():
    parser = argparse.ArgumentParser(description="Backtest runner for Deriv bot")
    parser.add_argument("--ticks", type=int, default=5000, help="Number of ticks to simulate")
    parser.add_argument("--monte-carlo", type=int, default=200, help="Monte Carlo simulations")
    parser.add_argument("--seed", type=int, default=42, help="Random seed")
    parser.add_argument("--equity", type=float, default=1000.0, help="Starting equity")
    parser.add_argument("--output", type=str, default=None, help="Save results to JSON file")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )

    logger.info(f"Generating {args.ticks} synthetic ticks (seed={args.seed})...")
    ticks = generate_synthetic_ticks(n_ticks=args.ticks, seed=args.seed)

    cfg = BotConfig()
    bt = Backtester(cfg, initial_equity=args.equity)

    # ── Single backtest ──
    logger.info("Running single backtest...")
    result = bt.run(ticks)
    logger.info("=" * 60)
    logger.info("  BACKTEST RESULTS")
    for k, v in result.summary().items():
        logger.info(f"  {k:30s}: {v}")
    logger.info("=" * 60)

    # ── Monte Carlo ──
    if args.monte_carlo > 0:
        logger.info(f"Running {args.monte_carlo} Monte Carlo simulations...")
        mc = bt.monte_carlo(ticks, n_simulations=args.monte_carlo)
        logger.info("=" * 60)
        logger.info("  MONTE CARLO RESULTS")
        for k, v in mc.items():
            if isinstance(v, float):
                logger.info(f"  {k:30s}: {v:+.4f}")
            else:
                logger.info(f"  {k:30s}: {v}")
        logger.info("=" * 60)

    # ── Save results ──
    if args.output:
        output = {
            "backtest": result.summary(),
            "equity_curve": result.equity_curve,
        }
        if args.monte_carlo > 0:
            output["monte_carlo"] = mc
        with open(args.output, "w") as f:
            json.dump(output, f, indent=2)
        logger.info(f"Results saved to {args.output}")


if __name__ == "__main__":
    main()

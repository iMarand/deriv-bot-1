"""
Market Daemon — Continuous background volatility monitor

Subscribes to live tick streams for all synthetic indices via the Deriv
WebSocket API. Continuously updates analysis trackers and writes a JSON
snapshot to data/market_scan.json every --interval seconds.

The dashboard reads this cached file instead of re-fetching history,
making auto-scan instant and efficient.

Usage:
    python3 market_daemon.py --app-id 1089 --interval 30
    python3 market_daemon.py --interval 15 --symbols R_10 R_50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
import warnings
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
warnings.filterwarnings("ignore")

import websockets

from config import (
    DigitConfig, VolatilityConfig, HMMConfig,
    PulseConfig, RollCakeConfig, ZigzagConfig, NovaBurstConfig,
)
from analysis import DigitTracker, VolatilityTracker
from regime import RegimeDetector, Regime
from pulse import PulseScorer
from rollcake import RollCakeScorer
from zigzag import ZigzagScorer

logger = logging.getLogger("market_daemon")

ALL_SYMBOLS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

OUTPUT_FILE = Path(__file__).parent / "data" / "market_scan.json"


class SymbolTracker:
    """All analysis components for one symbol."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.digit_tracker = DigitTracker(DigitConfig(window_size=60))
        self.vol_tracker = VolatilityTracker(VolatilityConfig())
        self.regime_detector = RegimeDetector(HMMConfig(lookback=200, retrain_interval=100))
        self.pulse_scorer = PulseScorer(fast_window=15, slow_window=50, micro_window=7, min_fast_pct=0.53)
        self.rollcake_scorer = RollCakeScorer(window_size=30, min_autocorrelation=0.25)
        self.zigzag_scorer = ZigzagScorer(tick_count=7, min_swings=3)
        self.tick_count = 0
        self.last_price: Optional[float] = None
        self.prev_price: Optional[float] = None

    def update(self, price: float, quote: str) -> None:
        self.tick_count += 1
        self.prev_price = self.last_price
        self.last_price = price

        self.digit_tracker.update(quote)
        self.vol_tracker.update(price)

        if self.prev_price is not None and self.prev_price != 0:
            ret = (price - self.prev_price) / self.prev_price
            self.regime_detector.update(ret)

        self.pulse_scorer.update(quote)
        self.rollcake_scorer.update(price)
        self.zigzag_scorer.update(price)

    def snapshot(self) -> dict:
        """Generate analysis snapshot for this symbol."""
        regime = self.regime_detector.current_regime
        regime_name = regime.name

        vt = self.vol_tracker
        dt = self.digit_tracker
        atr = vt.atr
        atr_pctile = vt.atr_percentile()
        momentum = vt.momentum(lookback=10)
        price_entropy = vt.price_change_entropy()

        # Volatility level
        vol_level = "UNKNOWN"
        if atr_pctile is not None:
            if atr_pctile >= 80:
                vol_level = "EXTREME"
            elif atr_pctile >= 65:
                vol_level = "HIGH"
            elif atr_pctile >= 35:
                vol_level = "MODERATE"
            else:
                vol_level = "LOW"

        # Pattern scores
        pulse_signal = self.pulse_scorer.score(self.regime_detector)
        rollcake_signal = self.rollcake_scorer.score()
        zigzag_signal = self.zigzag_scorer.score()

        pulse_score = pulse_signal.composite_score if pulse_signal else 0.0
        pulse_dir = pulse_signal.direction if pulse_signal else None
        rollcake_score = rollcake_signal[1] if rollcake_signal else 0.0
        rollcake_dir = rollcake_signal[0] if rollcake_signal else None
        zigzag_score = zigzag_signal[1] if zigzag_signal else 0.0
        zigzag_dir = zigzag_signal[0] if zigzag_signal else None

        is_biased = dt.is_biased()
        bias_mag = dt.bias_magnitude

        # Tradability score
        tradability = self._tradability(regime_name, vol_level, is_biased, bias_mag,
                                         pulse_score, rollcake_score, zigzag_score)

        return {
            "symbol": self.symbol,
            "status": "OK" if self.tick_count >= 20 else "WARMING",
            "ticks": self.tick_count,
            "last_price": self.last_price,
            "regime": regime_name,
            "volatility": {
                "level": vol_level,
                "atr": round(atr, 8) if atr else None,
                "atr_percentile": round(atr_pctile, 1) if atr_pctile is not None else None,
                "rolling_std": round(vt.rolling_std, 8) if vt.rolling_std else None,
                "realized_vol": round(vt.realized_vol, 8) if vt.realized_vol else None,
                "momentum": round(momentum, 6) if momentum else None,
                "price_entropy": round(price_entropy, 3) if price_entropy else None,
            },
            "digits": {
                "p_even": round(dt.p_even, 4),
                "bayesian_p_even": round(dt.bayesian_p_even(), 4),
                "digit_entropy": round(dt.digit_entropy(), 3),
                "chi_sq": round(dt.chi_square_test()[0], 2),
                "chi_p": round(dt.chi_square_test()[1], 4),
                "is_biased": is_biased,
                "bias_magnitude": round(bias_mag, 4),
                "cusum_alarm": dt.cusum_alarm,
            },
            "patterns": {
                "pulse": {"score": round(pulse_score, 4), "direction": pulse_dir},
                "rollcake": {"score": round(rollcake_score, 4), "direction": rollcake_dir},
                "zigzag": {"score": round(zigzag_score, 4), "direction": zigzag_dir},
            },
            "tradability": tradability,
        }

    def _tradability(self, regime_name, vol_level, is_biased, bias_mag,
                     pulse_score, rollcake_score, zigzag_score) -> int:
        score = 50
        regime_bonus = {"MEAN_REVERTING": 15, "TRENDING": 5, "CHOPPY": -15, "UNKNOWN": -5}
        score += regime_bonus.get(regime_name, 0)
        vol_bonus = {"LOW": 10, "MODERATE": 15, "HIGH": -5, "EXTREME": -30, "UNKNOWN": 0}
        score += vol_bonus.get(vol_level, 0)
        if is_biased:
            score += min(int(bias_mag * 200), 15)
        best_pattern = max(pulse_score, rollcake_score, zigzag_score)
        score += int(best_pattern * 20)
        return max(0, min(100, score))


class MarketDaemon:
    """Long-running daemon that subscribes to live ticks and writes analysis snapshots."""

    def __init__(self, symbols: List[str], app_id: int, interval: int):
        self.symbols = symbols
        self.app_id = app_id
        self.interval = interval
        self.trackers: Dict[str, SymbolTracker] = {}
        self._running = False
        self._last_write = 0.0

        for s in symbols:
            self.trackers[s] = SymbolTracker(s)

    def stop(self):
        self._running = False

    def _write_snapshot(self) -> None:
        """Write current analysis state to JSON file."""
        now = time.time()
        results = []
        for s in self.symbols:
            t = self.trackers[s]
            snap = t.snapshot()
            results.append(snap)

        # Sort by tradability
        results.sort(key=lambda x: x.get("tradability", 0), reverse=True)

        output = {
            "timestamp": now,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)),
            "interval_seconds": self.interval,
            "symbols_count": len(self.symbols),
            "results": results,
        }

        OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OUTPUT_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(output, f, indent=2)
        os.replace(tmp, OUTPUT_FILE)

        logger.info(
            "Snapshot written: %d symbols, top=%s (trad=%d)",
            len(results),
            results[0]["symbol"] if results else "?",
            results[0].get("tradability", 0) if results else 0,
        )

    async def run(self) -> None:
        self._running = True
        ws_url = f"wss://ws.derivws.com/websockets/v3?app_id={self.app_id}"

        while self._running:
            try:
                logger.info("Connecting to Deriv WebSocket...")
                async with websockets.connect(ws_url, ping_interval=30, ping_timeout=10) as ws:
                    # Subscribe to all symbols
                    for symbol in self.symbols:
                        await ws.send(json.dumps({"ticks": symbol, "subscribe": 1}))
                        logger.info("Subscribed: %s", symbol)
                        await asyncio.sleep(0.05)

                    logger.info("All subscriptions active. Writing snapshots every %ds.", self.interval)
                    self._write_snapshot()  # initial write

                    while self._running:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            # Check if it's time to write
                            if time.time() - self._last_write >= self.interval:
                                self._write_snapshot()
                                self._last_write = time.time()
                            continue

                        data = json.loads(msg)
                        tick = data.get("tick")
                        if tick:
                            symbol = tick.get("symbol", "")
                            price = float(tick.get("quote", 0))
                            quote = str(tick.get("quote", "0"))
                            if symbol in self.trackers:
                                self.trackers[symbol].update(price, quote)

                        # Periodic snapshot write
                        if time.time() - self._last_write >= self.interval:
                            self._write_snapshot()
                            self._last_write = time.time()

            except Exception as e:
                if not self._running:
                    break
                logger.error("Connection error: %s. Reconnecting in 5s...", e)
                await asyncio.sleep(5)


def main() -> int:
    ap = argparse.ArgumentParser(description="Market Daemon — continuous volatility monitor")
    ap.add_argument("--app-id", type=int, default=1089)
    ap.add_argument("--interval", type=int, default=30, help="Seconds between snapshot writes (default 30)")
    ap.add_argument("--hours", type=float, default=1.0, help="Lookback hours (ignored by daemon)")
    ap.add_argument("--symbols", nargs="+", default=None)
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )

    symbols = args.symbols or ALL_SYMBOLS
    daemon = MarketDaemon(symbols, args.app_id, args.interval)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _stop(signum=None, frame=None):
        daemon.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, _stop)

    try:
        loop.run_until_complete(daemon.run())
    except KeyboardInterrupt:
        daemon.stop()
    finally:
        loop.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

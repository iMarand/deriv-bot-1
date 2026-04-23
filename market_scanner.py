"""
Market Scanner — Pre-trade volatility & regime analysis

Fetches recent ticks for all synthetic indices via the Deriv WebSocket API,
runs VolatilityTracker, DigitTracker, RegimeDetector, and the pattern
scorers (Pulse, RollCake, Zigzag) on each symbol, then outputs one
JSON line per symbol so the PHP backend can stream it as SSE events.

Each output line is a complete JSON object (one per symbol) printed to stdout.
A final "__done__" sentinel line is printed when all symbols are processed.

Usage:
    python3 market_scanner.py                          # scan all 10 symbols
    python3 market_scanner.py --symbols R_10 R_50      # scan specific symbols
    python3 market_scanner.py --hours 1                # last 1 hour of ticks
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
import warnings
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

# Suppress HMM convergence warnings
warnings.filterwarnings("ignore")

try:
    import websockets
except ImportError:
    websockets = None

# ── Local imports ──────────────────────────────────────────────────────────────
from config import (
    DigitConfig, VolatilityConfig, HMMConfig,
    PulseConfig, RollCakeConfig, ZigzagConfig, NovaBurstConfig,
    TRADE_STRATEGIES,
)
from analysis import DigitTracker, VolatilityTracker
from regime import RegimeDetector, Regime
from pulse import PulseScorer
from rollcake import RollCakeScorer
from zigzag import ZigzagScorer

logger = logging.getLogger("market_scanner")

ALL_SYMBOLS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

MAX_TICKS_PER_REQUEST = 5000
REQUEST_DELAY = 0.3


# ── Tick fetcher (reused from fetch_history.py) ───────────────────────────────
async def fetch_ticks(
    ws_url: str,
    app_id: int,
    symbol: str,
    start_epoch: int,
    end_epoch: int,
) -> List[dict]:
    """Fetch historical ticks for a single symbol via Deriv WebSocket API."""
    all_ticks = []
    current_start = start_epoch

    url = f"{ws_url}?app_id={app_id}"
    try:
        async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
            while current_start < end_epoch:
                request = {
                    "ticks_history": symbol,
                    "adjust_start_time": 1,
                    "count": MAX_TICKS_PER_REQUEST,
                    "end": min(end_epoch, current_start + 86400),
                    "start": current_start,
                    "style": "ticks",
                }
                await ws.send(json.dumps(request))
                resp = json.loads(await ws.recv())

                if "error" in resp:
                    current_start += 3600
                    await asyncio.sleep(REQUEST_DELAY)
                    continue

                history = resp.get("history", {})
                times = history.get("times", [])
                prices = history.get("prices", [])

                if not times:
                    current_start += 3600
                    await asyncio.sleep(REQUEST_DELAY)
                    continue

                for t, p in zip(times, prices):
                    all_ticks.append({
                        "epoch": int(t),
                        "quote": str(p),
                        "symbol": symbol,
                    })

                last_epoch = int(times[-1])
                if last_epoch <= current_start:
                    current_start += 3600
                else:
                    current_start = last_epoch + 1

                await asyncio.sleep(REQUEST_DELAY)
    except Exception as e:
        logger.error("Fetch failed for %s: %s", symbol, e)

    return all_ticks


# ── Analysis engine ───────────────────────────────────────────────────────────
def analyse_symbol(symbol: str, ticks: List[dict]) -> dict:
    """Run all trackers and scorers on the ticks for one symbol.

    Returns a dict with regime, volatility metrics, digit stats,
    pattern scores, and strategy recommendations.
    """
    if not ticks:
        return {
            "symbol": symbol,
            "status": "NO_DATA",
            "ticks": 0,
        }

    # ── Initialise trackers ──
    digit_tracker = DigitTracker(DigitConfig(window_size=60))
    vol_tracker = VolatilityTracker(VolatilityConfig())
    regime_detector = RegimeDetector(HMMConfig(lookback=200, retrain_interval=100))
    pulse_scorer = PulseScorer(fast_window=15, slow_window=50, micro_window=7, min_fast_pct=0.53)
    rollcake_scorer = RollCakeScorer(window_size=30, min_autocorrelation=0.25)
    zigzag_scorer = ZigzagScorer(tick_count=7, min_swings=3)

    # ── Feed ticks ──
    prev_price = None
    for tick in ticks:
        price = float(tick["quote"])
        quote = tick["quote"]

        digit_tracker.update(quote)
        vol_tracker.update(price)

        if prev_price is not None and prev_price != 0:
            ret = (price - prev_price) / prev_price
            regime_detector.update(ret)

        pulse_scorer.update(quote)
        rollcake_scorer.update(price)
        zigzag_scorer.update(price)

        prev_price = price

    # ── Collect metrics ──
    regime = regime_detector.current_regime
    regime_name = regime.name

    atr = vol_tracker.atr
    rolling_std = vol_tracker.rolling_std
    realized_vol = vol_tracker.realized_vol
    atr_pctile = vol_tracker.atr_percentile()
    momentum = vol_tracker.momentum(lookback=10)
    price_entropy = vol_tracker.price_change_entropy()

    p_even = digit_tracker.p_even
    digit_entropy = digit_tracker.digit_entropy()
    chi2, chi_p = digit_tracker.chi_square_test()
    is_biased = digit_tracker.is_biased()
    bayesian_p_even = digit_tracker.bayesian_p_even()
    cusum_alarm = digit_tracker.cusum_alarm
    bias_mag = digit_tracker.bias_magnitude

    # ── Pattern scores ──
    pulse_signal = pulse_scorer.score(regime_detector)
    rollcake_signal = rollcake_scorer.score()
    zigzag_signal = zigzag_scorer.score()

    pulse_score = pulse_signal.composite_score if pulse_signal else 0.0
    pulse_dir = pulse_signal.direction if pulse_signal else None

    rollcake_score = rollcake_signal[1] if rollcake_signal else 0.0
    rollcake_dir = rollcake_signal[0] if rollcake_signal else None

    zigzag_score = zigzag_signal[1] if zigzag_signal else 0.0
    zigzag_dir = zigzag_signal[0] if zigzag_signal else None

    # ── Volatility classification ──
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

    # ── Strategy recommendation ──
    recommended_algorithm, recommended_trade_strategy, entry_signal = _recommend_strategy(
        regime_name=regime_name,
        vol_level=vol_level,
        atr_pctile=atr_pctile,
        is_biased=is_biased,
        bias_mag=bias_mag,
        pulse_score=pulse_score,
        pulse_dir=pulse_dir,
        rollcake_score=rollcake_score,
        rollcake_dir=rollcake_dir,
        zigzag_score=zigzag_score,
        zigzag_dir=zigzag_dir,
        momentum=momentum,
    )

    # ── Composite tradability score (0–100) ──
    tradability = _compute_tradability(
        regime_name=regime_name,
        vol_level=vol_level,
        is_biased=is_biased,
        bias_mag=bias_mag,
        pulse_score=pulse_score,
        rollcake_score=rollcake_score,
        zigzag_score=zigzag_score,
    )

    return {
        "symbol": symbol,
        "status": "OK",
        "ticks": len(ticks),
        "last_price": float(ticks[-1]["quote"]) if ticks else None,
        "regime": regime_name,
        "volatility": {
            "level": vol_level,
            "atr": round(atr, 8) if atr else None,
            "atr_percentile": round(atr_pctile, 1) if atr_pctile is not None else None,
            "rolling_std": round(rolling_std, 8) if rolling_std else None,
            "realized_vol": round(realized_vol, 8) if realized_vol else None,
            "momentum": round(momentum, 6) if momentum else None,
            "price_entropy": round(price_entropy, 3) if price_entropy else None,
        },
        "digits": {
            "p_even": round(p_even, 4),
            "bayesian_p_even": round(bayesian_p_even, 4),
            "digit_entropy": round(digit_entropy, 3),
            "chi_sq": round(chi2, 2),
            "chi_p": round(chi_p, 4),
            "is_biased": is_biased,
            "bias_magnitude": round(bias_mag, 4),
            "cusum_alarm": cusum_alarm,
        },
        "patterns": {
            "pulse": {"score": round(pulse_score, 4), "direction": pulse_dir},
            "rollcake": {"score": round(rollcake_score, 4), "direction": rollcake_dir},
            "zigzag": {"score": round(zigzag_score, 4), "direction": zigzag_dir},
        },
        "recommendation": {
            "algorithm": recommended_algorithm,
            "trade_strategy": recommended_trade_strategy,
            "entry_signal": entry_signal,
            "tradability": tradability,
        },
    }


def _recommend_strategy(
    regime_name: str,
    vol_level: str,
    atr_pctile: Optional[float],
    is_biased: bool,
    bias_mag: float,
    pulse_score: float,
    pulse_dir: Optional[str],
    rollcake_score: float,
    rollcake_dir: Optional[str],
    zigzag_score: float,
    zigzag_dir: Optional[str],
    momentum: Optional[float],
) -> Tuple[str, str, str]:
    """Recommend (algorithm, trade_strategy, entry_signal).

    entry_signal is one of: STRONG_ENTRY, GOOD_ENTRY, WAIT, DO_NOT_ENTER
    """

    # ── Extreme volatility → stay out ──
    if vol_level == "EXTREME":
        return "adaptive", "even_odd", "DO_NOT_ENTER"

    # ── Mean-reverting + low/moderate vol → ideal for digit strategies ──
    if regime_name == "MEAN_REVERTING" and vol_level in ("LOW", "MODERATE"):
        if is_biased and bias_mag >= 0.06:
            # Strong digit bias → use digit-based strategies
            if pulse_score >= 0.60:
                return "pulse", "even_odd", "STRONG_ENTRY"
            elif pulse_score >= 0.40:
                return "pulse", "even_odd", "GOOD_ENTRY"
            else:
                return "alphabloom", "even_odd", "GOOD_ENTRY"
        # Weak bias but mean-reverting → Roll Cake patterns work well
        if rollcake_score >= 0.40:
            return "pulse", "rise_fall_roll", "GOOD_ENTRY"
        return "ensemble", "even_odd", "WAIT"

    # ── Trending regime → directional strategies ──
    if regime_name == "TRENDING":
        if vol_level == "HIGH":
            # High vol + trending → Zigzag reversals
            if zigzag_score >= 0.35:
                return "novaburst", "rise_fall_zigzag", "GOOD_ENTRY"
            return "adaptive", "higher_lower_zigzag", "WAIT"
        else:
            # Moderate/low vol + trending → Roll Cake or Zigzag
            if rollcake_score >= 0.40:
                return "pulse", "rise_fall_roll", "GOOD_ENTRY"
            if zigzag_score >= 0.30:
                return "novaburst", "rise_fall_zigzag", "GOOD_ENTRY"
            # Digit bias is still usable in mild trending
            if pulse_score >= 0.55:
                return "pulse", "even_odd", "GOOD_ENTRY"
            return "adaptive", "even_odd", "WAIT"

    # ── Choppy regime → cautious, digit strategies only ──
    if regime_name == "CHOPPY":
        if vol_level == "HIGH":
            return "adaptive", "even_odd", "DO_NOT_ENTER"
        if is_biased and bias_mag >= 0.08 and pulse_score >= 0.55:
            return "pulse", "even_odd", "GOOD_ENTRY"
        if rollcake_score >= 0.45:
            return "pulse", "over_under_roll", "WAIT"
        return "adaptive", "even_odd", "WAIT"

    # ── Unknown regime → default conservative ──
    best_pattern = max(pulse_score, rollcake_score, zigzag_score)
    if best_pattern >= 0.55 and vol_level != "HIGH":
        if pulse_score >= rollcake_score and pulse_score >= zigzag_score:
            return "pulse", "even_odd", "GOOD_ENTRY"
        elif rollcake_score >= zigzag_score:
            return "pulse", "rise_fall_roll", "GOOD_ENTRY"
        else:
            return "novaburst", "rise_fall_zigzag", "GOOD_ENTRY"

    return "adaptive", "even_odd", "WAIT"


def _compute_tradability(
    regime_name: str,
    vol_level: str,
    is_biased: bool,
    bias_mag: float,
    pulse_score: float,
    rollcake_score: float,
    zigzag_score: float,
) -> int:
    """Compute a 0–100 tradability score."""
    score = 50  # start neutral

    # Regime contribution
    regime_bonus = {
        "MEAN_REVERTING": 15,
        "TRENDING": 5,
        "CHOPPY": -15,
        "UNKNOWN": -5,
    }
    score += regime_bonus.get(regime_name, 0)

    # Volatility contribution
    vol_bonus = {
        "LOW": 10,
        "MODERATE": 15,
        "HIGH": -5,
        "EXTREME": -30,
        "UNKNOWN": 0,
    }
    score += vol_bonus.get(vol_level, 0)

    # Digit bias
    if is_biased:
        score += min(int(bias_mag * 200), 15)

    # Best pattern score
    best_pattern = max(pulse_score, rollcake_score, zigzag_score)
    score += int(best_pattern * 20)

    return max(0, min(100, score))


# ── Main ──────────────────────────────────────────────────────────────────────
def emit(data: dict) -> None:
    """Print a JSON line to stdout (for SSE consumption by PHP)."""
    print(json.dumps(data), flush=True)


async def scan(symbols: List[str], hours: float, app_id: int, ws_url: str) -> None:
    """Fetch ticks and analyse each symbol, emitting one JSON line per symbol."""
    end_epoch = int(time.time())
    start_epoch = end_epoch - int(hours * 3600)

    emit({
        "type": "start",
        "symbols": symbols,
        "hours": hours,
        "timestamp": end_epoch,
    })

    url = f"{ws_url}?app_id={app_id}"
    try:
        async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
            for idx, symbol in enumerate(symbols):
                emit({
                    "type": "progress",
                    "symbol": symbol,
                    "step": idx + 1,
                    "total": len(symbols),
                    "message": f"Fetching {symbol}...",
                })

                # Fetch ticks for this symbol through the shared connection
                all_ticks = []
                current_start = start_epoch
                try:
                    while current_start < end_epoch:
                        request = {
                            "ticks_history": symbol,
                            "adjust_start_time": 1,
                            "count": MAX_TICKS_PER_REQUEST,
                            "end": min(end_epoch, current_start + 86400),
                            "start": current_start,
                            "style": "ticks",
                        }
                        await ws.send(json.dumps(request))
                        resp = json.loads(await ws.recv())

                        if "error" in resp:
                            current_start += 3600
                            await asyncio.sleep(REQUEST_DELAY)
                            continue

                        history = resp.get("history", {})
                        times = history.get("times", [])
                        prices = history.get("prices", [])

                        if not times:
                            current_start += 3600
                            await asyncio.sleep(REQUEST_DELAY)
                            continue

                        for t, p in zip(times, prices):
                            all_ticks.append({
                                "epoch": int(t),
                                "quote": str(p),
                                "symbol": symbol,
                            })

                        last_epoch = int(times[-1])
                        if last_epoch <= current_start:
                            current_start += 3600
                        else:
                            current_start = last_epoch + 1

                        await asyncio.sleep(REQUEST_DELAY)

                except Exception as e:
                    emit({
                        "type": "error",
                        "symbol": symbol,
                        "message": str(e),
                    })
                    continue

                # Analyse
                result = analyse_symbol(symbol, all_ticks)
                result["type"] = "result"
                emit(result)

    except Exception as e:
        emit({
            "type": "error",
            "symbol": "__connection__",
            "message": f"WebSocket connection failed: {e}",
        })

    emit({"type": "done", "timestamp": int(time.time())})


def main() -> int:
    ap = argparse.ArgumentParser(description="Market Scanner — volatility & regime analysis")
    ap.add_argument("--app-id", type=int, default=1089, help="Deriv app_id")
    ap.add_argument("--ws-url", default="wss://ws.derivws.com/websockets/v3", help="WebSocket URL")
    ap.add_argument("--hours", type=float, default=1, help="Hours of recent history (default 1)")
    ap.add_argument("--symbols", nargs="+", default=None, help="Symbols to scan (default: all 10)")
    ap.add_argument("--debug", action="store_true", help="Debug logging to stderr")
    args = ap.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )

    if websockets is None:
        emit({"type": "error", "symbol": "__init__", "message": "websockets package not installed"})
        emit({"type": "done", "timestamp": int(time.time())})
        return 1

    symbols = args.symbols or ALL_SYMBOLS

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(scan(symbols, args.hours, args.app_id, args.ws_url))
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

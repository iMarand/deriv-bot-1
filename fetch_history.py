"""
Historical Tick Fetcher + Pulse Simulator

Fetches historical tick data from Deriv's public WebSocket API (`ticks_history`)
and simulates Pulse strategy outcomes to generate synthetic training data.

This gives the ML filter thousands of realistic trades to train on — instead of
relying solely on the user's small session JSON logs.

Usage:
    py fetch_history.py --app-id 1089 --hours 48
    py fetch_history.py --hours 168 --symbols R_10 R_25 R_50

Output:
    data/history-trades.json — same format as session trade logs, usable by
    train_filter.py automatically.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import websockets
except ImportError:
    websockets = None

logger = logging.getLogger("fetch_history")

DATA_DIR = Path(__file__).parent / "data"

ALL_SYMBOLS = [
    "R_10", "R_25", "R_50", "R_75", "R_100",
    "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
]

# Maximum ticks per single API request (Deriv limit)
MAX_TICKS_PER_REQUEST = 5000
# Delay between API requests to avoid rate limiting
REQUEST_DELAY = 0.5


def _last_digit(quote: str) -> int:
    """Extract the last digit from a price quote string."""
    digits_only = "".join(ch for ch in quote if ch.isdigit())
    return int(digits_only[-1]) if digits_only else 0


class PulseSimulator:
    """Simulates the Pulse strategy on a stream of ticks to generate trade labels.

    For each tick where Pulse would have triggered a trade, we look ahead 5 ticks
    to determine the contract outcome (DIGITEVEN/DIGITODD at expiry).
    """

    def __init__(
        self,
        fast_window: int = 15,
        slow_window: int = 50,
        min_fast_pct: float = 0.53,
        cooldown_ticks: int = 1,
        contract_ticks: int = 5,
    ):
        self.fast_window = fast_window
        self.slow_window = slow_window
        self.min_fast_pct = min_fast_pct
        self.cooldown_ticks = cooldown_ticks
        self.contract_ticks = contract_ticks

    def simulate(self, ticks: List[dict]) -> List[dict]:
        """Run Pulse strategy over historical ticks, return list of trade dicts.

        Each tick dict must have: {"epoch": <int>, "quote": "<str>"}
        """
        digits = deque(maxlen=self.slow_window)
        cooldown = 0
        trades = []

        for i, tick in enumerate(ticks):
            quote = str(tick.get("quote", "0"))
            epoch = int(tick.get("epoch", 0))
            digit = _last_digit(quote)
            digits.append(digit)

            if cooldown > 0:
                cooldown -= 1
                continue

            if len(digits) < self.slow_window:
                continue

            # Compute fast/slow even %
            all_digits = list(digits)
            fast_digits = all_digits[-self.fast_window:]
            slow_digits = all_digits

            fast_even = sum(1 for d in fast_digits if d % 2 == 0) / len(fast_digits)
            slow_even = sum(1 for d in slow_digits if d % 2 == 0) / len(slow_digits)

            fast_dir = "EVEN" if fast_even >= 0.5 else "ODD"
            slow_dir = "EVEN" if slow_even >= 0.5 else "ODD"

            # Both must agree
            if fast_dir != slow_dir:
                continue

            direction = fast_dir
            fast_dominant = fast_even if direction == "EVEN" else (1.0 - fast_even)

            if fast_dominant < self.min_fast_pct:
                continue

            # Signal triggered — look ahead to determine outcome
            expiry_idx = i + self.contract_ticks
            if expiry_idx >= len(ticks):
                break  # Not enough future data

            expiry_quote = str(ticks[expiry_idx].get("quote", "0"))
            expiry_digit = _last_digit(expiry_quote)
            expiry_epoch = int(ticks[expiry_idx].get("epoch", 0))

            contract_type = f"DIGIT{direction}"
            if direction == "EVEN":
                is_win = expiry_digit % 2 == 0
            else:
                is_win = expiry_digit % 2 != 0

            # Compute a score similar to real Pulse
            slow_dominant = slow_even if direction == "EVEN" else (1.0 - slow_even)
            fast_edge = fast_dominant - 0.5
            slow_edge = slow_dominant - 0.5
            combined_edge = 0.6 * fast_edge + 0.4 * slow_edge
            composite = min(combined_edge / 0.15, 1.0)

            trade = {
                "trade_no": len(trades) + 1,
                "timestamp": epoch,
                "symbol": tick.get("symbol", ""),
                "contract_type": contract_type,
                "stake": 1.0,
                "profit": 0.82 if is_win else -1.0,  # typical digit payout ~1.82x
                "payout": 1.82 if is_win else 0.0,
                "result": "win" if is_win else "loss",
                "source": "history_simulation",
                "score": round(composite, 4),
                "fast_pct": round(fast_dominant, 4),
                "slow_pct": round(slow_dominant, 4),
                "expiry_epoch": expiry_epoch,
            }
            trades.append(trade)
            cooldown = self.cooldown_ticks

        return trades


async def fetch_ticks_for_symbol(
    ws_url: str,
    app_id: int,
    symbol: str,
    start_epoch: int,
    end_epoch: int,
) -> List[dict]:
    """Fetch historical ticks for a single symbol via Deriv WebSocket API.

    Returns a list of {"epoch": ..., "quote": ..., "symbol": ...} dicts.
    Automatically handles pagination for large time ranges.
    """
    all_ticks = []
    current_start = start_epoch

    url = f"{ws_url}?app_id={app_id}"
    async with websockets.connect(url, ping_interval=30, ping_timeout=10) as ws:
        while current_start < end_epoch:
            request = {
                "ticks_history": symbol,
                "adjust_start_time": 1,
                "count": MAX_TICKS_PER_REQUEST,
                "end": min(end_epoch, current_start + 86400),  # max 1 day per request
                "start": current_start,
                "style": "ticks",
            }
            await ws.send(json.dumps(request))
            resp = json.loads(await ws.recv())

            if "error" in resp:
                error_msg = resp["error"].get("message", str(resp["error"]))
                logger.error("API error for %s at epoch %d: %s", symbol, current_start, error_msg)
                # Skip ahead by 1 hour and try again
                current_start += 3600
                await asyncio.sleep(REQUEST_DELAY)
                continue

            history = resp.get("history", {})
            times = history.get("times", [])
            prices = history.get("prices", [])

            if not times:
                logger.debug("No ticks returned for %s at epoch %d, moving forward", symbol, current_start)
                current_start += 3600
                await asyncio.sleep(REQUEST_DELAY)
                continue

            for t, p in zip(times, prices):
                all_ticks.append({
                    "epoch": int(t),
                    "quote": str(p),
                    "symbol": symbol,
                })

            # Move start past the last received tick
            last_epoch = int(times[-1])
            if last_epoch <= current_start:
                current_start += 3600  # prevent infinite loop
            else:
                current_start = last_epoch + 1

            logger.debug(
                "%s: fetched %d ticks (total %d), up to epoch %d",
                symbol, len(times), len(all_ticks), last_epoch,
            )
            await asyncio.sleep(REQUEST_DELAY)

    return all_ticks


async def fetch_all_symbols(
    ws_url: str,
    app_id: int,
    symbols: List[str],
    hours: float,
) -> Dict[str, List[dict]]:
    """Fetch historical ticks for all symbols."""
    end_epoch = int(time.time())
    start_epoch = end_epoch - int(hours * 3600)

    result = {}
    for symbol in symbols:
        logger.info("Fetching %s: %d hours of history...", symbol, int(hours))
        try:
            ticks = await fetch_ticks_for_symbol(ws_url, app_id, symbol, start_epoch, end_epoch)
            result[symbol] = ticks
            logger.info("  %s: %d ticks fetched", symbol, len(ticks))
        except Exception as e:
            logger.error("  %s: fetch failed: %s", symbol, e)
            result[symbol] = []

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch historical ticks and simulate Pulse trades")
    ap.add_argument("--app-id", type=int, default=1089, help="Deriv app_id")
    ap.add_argument("--ws-url", default="wss://ws.derivws.com/websockets/v3", help="WebSocket URL")
    ap.add_argument("--hours", type=float, default=48, help="Hours of history to fetch (default 48)")
    ap.add_argument("--symbols", nargs="+", default=None, help="Symbols to fetch (default: all 10)")
    ap.add_argument("--fast-window", type=int, default=15, help="Pulse fast window")
    ap.add_argument("--slow-window", type=int, default=50, help="Pulse slow window")
    ap.add_argument("--min-fast-pct", type=float, default=0.53, help="Pulse min fast %%")
    ap.add_argument("--contract-ticks", type=int, default=5, help="Contract duration in ticks")
    ap.add_argument("--output", default=None, help="Output file (default: data/history-trades.json)")
    ap.add_argument("--debug", action="store_true", help="Debug logging")
    args = ap.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(name)-14s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if websockets is None:
        print("ERROR: 'websockets' package is required. Install with: pip install websockets")
        return 1

    symbols = args.symbols or ALL_SYMBOLS
    output_path = Path(args.output) if args.output else DATA_DIR / "history-trades.json"

    print(f"═" * 60)
    print(f"  Historical Tick Fetcher + Pulse Simulator")
    print(f"  Symbols: {symbols}")
    print(f"  Hours:   {args.hours}")
    print(f"  Pulse:   fast={args.fast_window} slow={args.slow_window} min={args.min_fast_pct:.0%}")
    print(f"  Output:  {output_path}")
    print(f"═" * 60)

    # Fetch ticks
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        all_ticks = loop.run_until_complete(
            fetch_all_symbols(args.ws_url, args.app_id, symbols, args.hours)
        )
    finally:
        loop.close()

    # Simulate Pulse on each symbol
    simulator = PulseSimulator(
        fast_window=args.fast_window,
        slow_window=args.slow_window,
        min_fast_pct=args.min_fast_pct,
        contract_ticks=args.contract_ticks,
    )

    all_trades = []
    total_ticks = 0
    for symbol, ticks in all_ticks.items():
        total_ticks += len(ticks)
        if not ticks:
            print(f"  {symbol}: no ticks fetched — skipping")
            continue
        trades = simulator.simulate(ticks)
        all_trades.extend(trades)
        wins = sum(1 for t in trades if t["result"] == "win")
        wr = wins / len(trades) if trades else 0
        print(f"  {symbol}: {len(ticks)} ticks → {len(trades)} simulated trades (WR={wr:.1%})")

    if not all_trades:
        print("ERROR: no trades generated from historical data")
        return 1

    # Sort by timestamp
    all_trades.sort(key=lambda t: t.get("timestamp", 0))

    # Re-number
    for i, t in enumerate(all_trades, 1):
        t["trade_no"] = i

    # Build output in the same format as session trade logs
    wins = sum(1 for t in all_trades if t["result"] == "win")
    payload = {
        "session": {
            "started_at": all_trades[0]["timestamp"],
            "updated_at": all_trades[-1]["timestamp"],
            "account_mode": "history_simulation",
            "source": "fetch_history.py",
            "hours_fetched": args.hours,
            "symbols_fetched": symbols,
            "total_ticks_fetched": total_ticks,
        },
        "summary": {
            "trade_count": len(all_trades),
            "wins": wins,
            "losses": len(all_trades) - wins,
            "net_pnl": round(sum(t["profit"] for t in all_trades), 2),
            "win_rate": round(wins / len(all_trades), 4),
        },
        "equity_curve": [],
        "trades": all_trades,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)

    print(f"\n{'═' * 60}")
    print(f"  DONE")
    print(f"  Total ticks fetched: {total_ticks:,}")
    print(f"  Total trades simulated: {len(all_trades):,}")
    print(f"  Win rate: {wins / len(all_trades):.1%}")
    print(f"  Net P/L: ${sum(t['profit'] for t in all_trades):+.2f}")
    print(f"  Saved to: {output_path}")
    print(f"{'═' * 60}")
    print(f"\nNext step: run 'py train_filter.py' to retrain the ML model")
    print(f"with this historical data included.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

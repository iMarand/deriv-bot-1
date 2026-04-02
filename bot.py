"""
Deriv Synthetic-Indices Trading Bot — Main Engine
WebSocket connection, full trading loop with all components integrated.

Usage:
    python bot.py --token YOUR_TOKEN --account-mode {demo|real} [--app-id 1089]
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import websockets

from config import BotConfig
from analysis import DigitTracker, VolatilityTracker
from regime import RegimeDetector, Regime
from ensemble import EnsembleScorer, SignalSnapshot
from alphabloom import AlphaBloomScorer
from money import (
    MartingaleEngine,
    KellyCalculator,
    CircuitBreaker,
    TradeRecord,
)
from index_selector import IndexSelector
from filters import TimeFilter, ShadowTrader, ShadowTrade

logger = logging.getLogger("deriv_bot")


class ContractRecoveryRequired(Exception):
    """Raised when the active contract monitor needs a websocket reconnect."""


def symbol_tick_seconds(symbol: str) -> float:
    if symbol.startswith("1HZ"):
        return 1.0
    return 2.0


def estimate_contract_seconds(symbol: str, duration: int, duration_unit: str) -> Optional[float]:
    if duration_unit == "t":
        return duration * symbol_tick_seconds(symbol)
    if duration_unit == "s":
        return float(duration)
    if duration_unit == "m":
        return float(duration * 60)
    if duration_unit == "h":
        return float(duration * 3600)
    if duration_unit == "d":
        return float(duration * 86400)
    return None


class DerivBot:
    """
    Full-featured trading bot integrating:
    - Multi-index tick subscriptions
    - Digit distribution + chi-square + CUSUM
    - HMM regime detection
    - Ensemble signal scoring
    - Kelly + Martingale stake sizing
    - Circuit breaker
    - Time-of-day filter
    - Shadow paper-trading mode
    """

    def __init__(
        self,
        token: str,
        cfg: BotConfig | None = None,
        account_mode: str = "demo",
        save_app_json: Optional[str] = None,
    ):
        self.token = token
        self.cfg = cfg or BotConfig()
        self.account_mode = account_mode
        self.save_app_json = Path(save_app_json).resolve() if save_app_json else None
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._req_id = 0

        # Per-index analysis components
        self._digit_trackers: Dict[str, DigitTracker] = {}
        self._vol_trackers: Dict[str, VolatilityTracker] = {}
        self._regime_detectors: Dict[str, RegimeDetector] = {}
        self._ab_scorers: Dict[str, AlphaBloomScorer] = {}  # per-symbol AlphaBloom

        # Shared components
        self.scorer = EnsembleScorer(self.cfg.ensemble)
        self.martingale = MartingaleEngine(self.cfg.martingale)
        self.kelly = KellyCalculator(self.cfg.kelly)
        self.circuit_breaker = CircuitBreaker(self.cfg.circuit_breaker)
        self.index_selector = IndexSelector(self.cfg.index)
        self.time_filter = TimeFilter(self.cfg.time_filter)
        self.shadow = ShadowTrader(self.cfg.shadow)

        # State
        self.equity: float = 0.0
        self.active_contract: Optional[str] = None  # contract_id if waiting
        self._pending_proposal: Optional[dict] = None
        self._current_symbol: Optional[str] = None
        self._current_signal: Optional[SignalSnapshot] = None
        self._trade_count: int = 0
        self._tick_counts: Dict[str, int] = {}
        self._symbol_status: Dict[str, str] = {}
        self._symbol_scores: Dict[str, float] = {}
        self._last_status_log_ts: float = 0.0
        self._active_contract_symbol: Optional[str] = None
        self._active_contract_started_at: Optional[float] = None
        self._active_contract_expected_seconds: Optional[float] = None
        self._last_contract_poll_ts: float = 0.0
        self._last_contract_update_ts: Optional[float] = None
        self._contract_poll_attempts: int = 0
        self._last_best_symbol: Optional[str] = None
        self._session_started_at: float = time.time()
        self._initial_equity: float = 0.0
        self._account_loginid: Optional[str] = None
        self._account_currency: Optional[str] = None
        self._account_fullname: Optional[str] = None

        # Trade log
        self.trade_history: list[TradeRecord] = []

    # ─────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────
    def _next_req_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _send(self, payload: dict) -> None:
        payload.setdefault("req_id", self._next_req_id())
        if self.ws:
            await self.ws.send(json.dumps(payload))
            logger.debug(f"→ {payload.get('req_id')}: {list(payload.keys())}")

    def _init_trackers(self, symbol: str) -> None:
        if symbol not in self._digit_trackers:
            self._digit_trackers[symbol] = DigitTracker(self.cfg.digit)
            self._vol_trackers[symbol] = VolatilityTracker(self.cfg.volatility)
            self._regime_detectors[symbol] = RegimeDetector(self.cfg.hmm)
            self._ab_scorers[symbol] = AlphaBloomScorer(self.cfg.alphabloom)
            self._tick_counts[symbol] = 0
            self._symbol_status[symbol] = "warming up"
            self._symbol_scores[symbol] = 0.0

    def _set_symbol_status(self, symbol: str, status: str, score: float = 0.0) -> None:
        self._symbol_status[symbol] = status
        self._symbol_scores[symbol] = score

    def _maybe_log_status(self) -> None:
        now = time.time()
        if now - self._last_status_log_ts < 15:
            return

        self._last_status_log_ts = now
        if not self._symbol_status:
            return

        warmed = sum(1 for dt in self._digit_trackers.values() if len(dt.window) >= 20)
        if self.active_contract and self._active_contract_symbol and self._active_contract_started_at:
            expected = (
                f"~{int(self._active_contract_expected_seconds)}s"
                if self._active_contract_expected_seconds is not None
                else "unknown"
            )
            logger.info(
                "Status: warmed=%s/%s | active=%s | contract=%s | elapsed=%ss | expected=%s",
                warmed,
                len(self.cfg.index.symbols),
                self._active_contract_symbol,
                self.active_contract,
                int(now - self._active_contract_started_at),
                expected,
            )
            return

        best_symbol = self._last_best_symbol or max(self._symbol_scores, key=self._symbol_scores.get, default=None)
        if best_symbol is None:
            return

        logger.info(
            "Status: warmed=%s/%s | best=%s | threshold=%.3f | %s",
            warmed,
            len(self.cfg.index.symbols),
            best_symbol,
            self.cfg.ensemble.entry_score_threshold,
            self._symbol_status.get(best_symbol, "no signal yet"),
        )

    def _build_app_json_payload(self) -> dict:
        total_pnl = sum(t.profit for t in self.trade_history)
        wins = sum(1 for t in self.trade_history if t.is_win)
        losses = len(self.trade_history) - wins
        trades = []
        equity_curve = []
        running_equity = self._initial_equity

        for idx, trade in enumerate(self.trade_history, start=1):
            equity_before = running_equity
            equity_after = equity_before + trade.profit
            running_equity = equity_after
            trade_entry = {
                "trade_no": idx,
                "timestamp": trade.timestamp,
                "symbol": trade.symbol,
                "contract_type": trade.contract_type,
                "stake": trade.stake,
                "profit": trade.profit,
                "payout": trade.payout,
                "result": "win" if trade.is_win else "loss",
                "equity_before": round(equity_before, 2),
                "equity_after": round(equity_after, 2),
            }
            trades.append(trade_entry)
            equity_curve.append(
                {
                    "trade_no": idx,
                    "timestamp": trade.timestamp,
                    "equity": round(equity_after, 2),
                }
            )

        return {
            "session": {
                "started_at": self._session_started_at,
                "updated_at": time.time(),
                "account_mode": self.account_mode,
                "account_loginid": self._account_loginid,
                "account_currency": self._account_currency,
                "account_fullname": self._account_fullname,
                "initial_equity": round(self._initial_equity, 2),
                "current_equity": round(self.equity, 2),
                "base_stake": self.cfg.martingale.base_stake_usd,
                "profit_target": self.cfg.martingale.profit_target_usd,
                "loss_limit": self.cfg.martingale.loss_limit_usd,
                "score_threshold": self.cfg.ensemble.entry_score_threshold,
                "duration": self.cfg.contract.duration,
                "duration_unit": self.cfg.contract.duration_unit,
                "symbols": list(self.cfg.index.symbols),
                "active_contract": self.active_contract,
            },
            "summary": {
                "trade_count": len(self.trade_history),
                "wins": wins,
                "losses": losses,
                "net_pnl": round(total_pnl, 2),
                "win_rate": round((wins / len(self.trade_history)) if self.trade_history else 0.0, 4),
                "martingale_net_pnl": round(self.martingale.net_pnl, 2),
            },
            "equity_curve": equity_curve,
            "trades": trades,
        }

    def _write_app_json(self) -> None:
        if self.save_app_json is None:
            return

        payload = self._build_app_json_payload()
        self.save_app_json.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.save_app_json.with_suffix(f"{self.save_app_json.suffix}.tmp")
        with temp_path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(temp_path, self.save_app_json)

    # ─────────────────────────────────────
    # Connection lifecycle
    # ─────────────────────────────────────
    async def connect(self) -> None:
        url = f"{self.cfg.api.ws_url}?app_id={self.cfg.api.app_id}"
        logger.info(f"Connecting to {url}")
        self.ws = await websockets.connect(url, ping_interval=30, ping_timeout=10)
        logger.info("WebSocket connected")

    async def authorize(self) -> bool:
        await self._send({"authorize": self.token})
        resp = await self.ws.recv()
        data = json.loads(resp)
        if "error" in data:
            logger.error(f"Auth failed: {data['error']['message']}")
            return False
        auth = data.get("authorize", {})
        loginid = str(auth.get("loginid", ""))
        is_virtual = loginid.upper().startswith("VRTC")
        actual_mode = "demo" if is_virtual else "real"
        if actual_mode != self.account_mode:
            logger.error(
                "Authorized account mode does not match --account-mode: "
                f"requested={self.account_mode}, actual={actual_mode}, account={loginid}. "
                "Use a token created for the correct account."
            )
            return False
        self.equity = float(auth.get("balance", 0))
        if self._initial_equity == 0.0:
            self._initial_equity = self.equity
        self._account_loginid = loginid
        self._account_currency = auth.get("currency", "?")
        self._account_fullname = auth.get("fullname", "?")
        logger.info(
            f"Authorized: {auth.get('fullname', '?')} | "
            f"Balance: {self.equity} {auth.get('currency', '?')} | "
            f"Account: {loginid} | "
            f"Mode: {actual_mode}"
        )
        self._write_app_json()
        return True

    async def close(self) -> None:
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.close()
            self.ws = None

    async def subscribe_ticks(self) -> None:
        for symbol in self.cfg.index.symbols:
            self._init_trackers(symbol)
            await self._send({"ticks": symbol, "subscribe": 1})
            logger.info(f"Subscribed to ticks: {symbol}")
            await asyncio.sleep(0.05)  # respect rate limits

    async def _resume_active_contract_monitoring(self) -> None:
        if not self.active_contract or self.active_contract == "proposal_pending":
            return

        logger.warning(
            "Resuming active contract after reconnect: symbol=%s contract=%s",
            self._active_contract_symbol,
            self.active_contract,
        )
        await self._send(
            {
                "proposal_open_contract": 1,
                "contract_id": int(self.active_contract),
                "subscribe": 1,
            }
        )
        self._last_contract_poll_ts = time.time()

    async def fetch_balance(self) -> float:
        await self._send({"balance": 1, "subscribe": 0})
        resp = await self.ws.recv()
        data = json.loads(resp)
        if "balance" in data:
            self.equity = float(data["balance"]["balance"])
        return self.equity

    # ─────────────────────────────────────
    # Core trading logic
    # ─────────────────────────────────────
    def _process_tick(self, tick: dict) -> Optional[Tuple[str, SignalSnapshot]]:
        """
        Process a single tick. Returns (symbol, signal) if a trade should be placed.
        """
        symbol = tick.get("symbol", "")
        quote_str = str(tick.get("quote", "0"))
        price = float(tick.get("quote", 0))

        self._init_trackers(symbol)
        self._tick_counts[symbol] += 1
        dt = self._digit_trackers[symbol]
        vt = self._vol_trackers[symbol]
        rd = self._regime_detectors[symbol]

        dt.update(quote_str)
        vt.update(price)
        ab = self._ab_scorers[symbol]
        ab.update(quote_str)

        if len(vt.returns) > 0:
            rd.update(float(vt.returns[-1]))

        # Don't trade if we already have an active contract
        if self.active_contract:
            self._set_symbol_status(symbol, f"waiting on active contract {self.active_contract}")
            return None

        # Time filter
        if not self.time_filter.should_trade_now():
            self._set_symbol_status(symbol, "blocked by time filter")
            return None

        # Circuit breaker may be active
        # (doesn't block, just reduces stake — checked at stake calc)

        # ── Strategy dispatch ──
        if self.cfg.strategy == "alphabloom":
            return self._process_tick_alphabloom(symbol, ab, rd)
        else:
            return self._process_tick_ensemble(symbol, dt, vt, rd)

    def _process_tick_alphabloom(
        self, symbol: str, ab: AlphaBloomScorer, rd: RegimeDetector,
    ) -> Optional[Tuple[str, SignalSnapshot]]:
        """AlphaBloom strategy: pure digit frequency."""
        n = len(ab._digits)
        if n < 20:
            self._set_symbol_status(symbol, f"warming {n}/20 ticks")
            return None

        signal = ab.score(regime_det=rd)
        if signal is None:
            p_e = ab.p_even
            p_o = ab.p_odd
            thresh = self.cfg.alphabloom.imbalance_threshold
            self._set_symbol_status(
                symbol,
                f"even={p_e:.1%} odd={p_o:.1%} (need >{thresh:.0%})",
                max(p_e, p_o),
            )
            return None

        if signal.composite_score < self.cfg.ensemble.entry_score_threshold:
            self._set_symbol_status(
                symbol,
                f"score {signal.composite_score:.3f} below threshold {self.cfg.ensemble.entry_score_threshold:.3f}",
                signal.composite_score,
            )
            return None

        self._set_symbol_status(
            symbol,
            (
                f"AB candidate dir={signal.direction} "
                f"even={ab.p_even:.1%} odd={ab.p_odd:.1%} "
                f"score={signal.composite_score:.3f}"
            ),
            signal.composite_score,
        )
        return (symbol, signal)

    def _process_tick_ensemble(
        self, symbol: str, dt: DigitTracker, vt: VolatilityTracker, rd: RegimeDetector,
    ) -> Optional[Tuple[str, SignalSnapshot]]:
        """Original ensemble strategy."""
        if self.cfg.ensemble.require_known_regime and rd.current_regime == Regime.UNKNOWN:
            self._set_symbol_status(
                symbol,
                f"waiting for regime {len(rd.returns_buf)}/{self.cfg.hmm.lookback} returns",
            )
            return None

        # Ensemble score
        signal = self.scorer.score(dt, vt, rd)
        if signal is None:
            if len(dt.window) < 20:
                self._set_symbol_status(symbol, f"warming {len(dt.window)}/20 ticks")
            elif dt.bias_magnitude < 0.03:
                self._set_symbol_status(symbol, f"bias too low ({dt.bias_magnitude:.3f})")
            elif rd.current_regime == Regime.CHOPPY and dt.bias_magnitude < 0.08:
                self._set_symbol_status(symbol, f"choppy regime, weak bias ({dt.bias_magnitude:.3f})")
            elif len(rd.returns_buf) < self.cfg.hmm.lookback:
                self._set_symbol_status(
                    symbol,
                    f"regime warming {len(rd.returns_buf)}/{self.cfg.hmm.lookback} returns",
                )
            else:
                self._set_symbol_status(symbol, "signal unavailable")
            return None
        if signal.composite_score < self.cfg.ensemble.entry_score_threshold:
            self._set_symbol_status(
                symbol,
                f"score {signal.composite_score:.3f} below threshold {self.cfg.ensemble.entry_score_threshold:.3f}",
                signal.composite_score,
            )
            return None

        # CUSUM alarm boosts confidence — only require for very borderline scores
        # (within 0.03 of threshold) to avoid filtering out most trades
        borderline_band = 0.03
        if not dt.cusum_alarm and signal.composite_score < self.cfg.ensemble.entry_score_threshold + borderline_band:
            self._set_symbol_status(
                symbol,
                f"score {signal.composite_score:.3f} needs CUSUM confirmation",
                signal.composite_score,
            )
            return None

        self._set_symbol_status(
            symbol,
            (
                f"candidate score={signal.composite_score:.3f} "
                f"bias={signal.digit_bias:.3f} regime={signal.regime.name}"
            ),
            signal.composite_score,
        )

        return (symbol, signal)

    async def _request_proposal(self, symbol: str, signal: SignalSnapshot) -> None:
        contract_type = f"DIGIT{signal.direction}"
        threshold = self.cfg.ensemble.entry_score_threshold
        if signal.composite_score < threshold:
            logger.warning(
                "Skipping proposal below threshold: symbol=%s score=%.3f threshold=%.3f",
                symbol,
                signal.composite_score,
                threshold,
            )
            self._set_symbol_status(
                symbol,
                f"guard blocked score {signal.composite_score:.3f} below threshold {threshold:.3f}",
                signal.composite_score,
            )
            return

        kf = self.kelly.kelly_fraction()
        stake = self.martingale.next_stake(self.equity, kf)
        stake = self.circuit_breaker.adjust_stake(stake)
        if stake <= 0:
            self._set_symbol_status(symbol, "stake skipped by risk engine")
            return

        proposal = {
            "proposal": 1,
            "amount": stake,
            "basis": self.cfg.contract.basis,
            "contract_type": contract_type,
            "currency": self.cfg.contract.currency,
            "duration": self.cfg.contract.duration,
            "duration_unit": self.cfg.contract.duration_unit,
            "symbol": symbol,
        }
        self._pending_proposal = {
            "symbol": symbol,
            "signal": signal,
            "stake": stake,
            "contract_type": contract_type,
        }
        self._current_symbol = symbol
        self._current_signal = signal
        self.active_contract = "proposal_pending"
        self._active_contract_symbol = symbol
        self._active_contract_started_at = time.time()
        self._active_contract_expected_seconds = estimate_contract_seconds(
            symbol,
            self.cfg.contract.duration,
            self.cfg.contract.duration_unit,
        )

        logger.info(
            f"Requesting proposal: {contract_type} on {symbol} | "
            f"stake=${stake:.2f} | score={signal.composite_score:.3f} | "
            f"threshold={threshold:.3f} | regime={signal.regime.name} | "
            f"sizing={self.martingale.last_stake_reason}"
        )
        await self._send(proposal)

    async def _execute_buy(self, proposal_data: dict) -> None:
        proposal_id = proposal_data.get("id")
        ask_price = proposal_data.get("ask_price", 0)
        if not proposal_id:
            logger.error("Proposal response missing id")
            self.active_contract = None
            self._pending_proposal = None
            return
        logger.info(f"Buying proposal {proposal_id} at ask_price={ask_price}")
        await self._send({"buy": proposal_id, "price": ask_price})

    async def _handle_buy_result(self, buy_data: dict) -> None:
        contract_id = buy_data.get("contract_id")
        if not contract_id:
            logger.error("Buy response missing contract_id")
            self.active_contract = None
            self._active_contract_symbol = None
            self._active_contract_started_at = None
            self._active_contract_expected_seconds = None
            self._pending_proposal = None
            return

        self.active_contract = str(contract_id)
        self._last_contract_update_ts = time.time()
        self._contract_poll_attempts = 0
        # Notify AlphaBloom scorers of trade placement (cooldown)
        if self._active_contract_symbol and self._active_contract_symbol in self._ab_scorers:
            self._ab_scorers[self._active_contract_symbol].on_trade_placed()
        logger.info(f"Buy accepted: contract_id={contract_id} | monitoring open contract")
        await self._send(
            {
                "proposal_open_contract": 1,
                "contract_id": contract_id,
                "subscribe": 1,
            }
        )

    def _is_contract_terminal(self, contract_data: dict) -> bool:
        status = str(contract_data.get("status", "")).lower()
        is_sold = int(contract_data.get("is_sold") or 0) == 1
        is_expired = int(contract_data.get("is_expired") or 0) == 1
        return is_sold or is_expired or status in {"won", "lost", "sold", "expired", "cancelled"}

    def _handle_contract_update(self, contract_data: dict) -> None:
        contract_id = str(contract_data.get("contract_id", ""))
        if not contract_id or contract_id != self.active_contract:
            return

        self._last_contract_update_ts = time.time()
        self._contract_poll_attempts = 0
        status = str(contract_data.get("status", "unknown")).lower()
        is_sold = int(contract_data.get("is_sold") or 0) == 1
        is_expired = int(contract_data.get("is_expired") or 0) == 1

        if not self._is_contract_terminal(contract_data):
            logger.debug(
                "Contract update: id=%s symbol=%s status=%s is_sold=%s is_expired=%s current_spot=%s",
                contract_id,
                self._active_contract_symbol,
                status,
                is_sold,
                is_expired,
                contract_data.get("current_spot"),
            )
            return

        pending = self._pending_proposal or {}
        symbol = pending.get("symbol", contract_data.get("underlying", "?"))
        contract_type = pending.get("contract_type", contract_data.get("contract_type", "?"))
        stake = float(contract_data.get("buy_price") or pending.get("stake") or 0.0)
        payout = float(contract_data.get("payout") or 0.0)
        profit = float(contract_data.get("profit") or 0.0)
        is_win = profit > 0

        trade = TradeRecord(
            timestamp=time.time(),
            symbol=symbol,
            contract_type=contract_type,
            stake=stake,
            profit=profit,
            payout=payout,
            is_win=is_win,
        )

        self.trade_history.append(trade)
        self.kelly.record(trade)
        self.martingale.on_result(trade)
        self.equity = float(contract_data.get("balance_after") or (self.equity + profit))
        self.circuit_breaker.update(self.equity)
        self.time_filter.record(time.time(), is_win)
        self._trade_count += 1
        self._write_app_json()

        icon = "WIN" if is_win else "LOSS"
        logger.info(
            "Trade #%s %s: %s on %s | stake=$%.2f | profit=$%+.2f | equity=$%.2f | consec_losses=%s",
            self._trade_count,
            icon,
            contract_type,
            symbol,
            stake,
            profit,
            self.equity,
            self.martingale.consecutive_losses,
        )

        self.active_contract = None
        self._active_contract_symbol = None
        self._active_contract_started_at = None
        self._active_contract_expected_seconds = None
        self._last_contract_update_ts = None
        self._contract_poll_attempts = 0
        self._pending_proposal = None

    async def _poll_stuck_contract(self) -> None:
        if not self.active_contract or self.active_contract == "proposal_pending":
            return
        if not self._active_contract_started_at:
            return

        expected = self._active_contract_expected_seconds or 0.0
        elapsed = time.time() - self._active_contract_started_at
        threshold = max(30.0, expected * 3) if expected > 0 else 30.0
        if elapsed < threshold:
            return
        if time.time() - self._last_contract_poll_ts < 10:
            return

        self._last_contract_poll_ts = time.time()
        self._contract_poll_attempts += 1
        logger.warning(
            "Contract taking longer than expected: symbol=%s contract=%s elapsed=%ss expected=%ss. Polling status again (attempt %s).",
            self._active_contract_symbol,
            self.active_contract,
            int(elapsed),
            int(expected) if expected else -1,
            self._contract_poll_attempts,
        )
        await self._send(
            {
                "proposal_open_contract": 1,
                "contract_id": int(self.active_contract),
                "subscribe": 0,
            }
        )
        stale_for = (
            time.time() - self._last_contract_update_ts
            if self._last_contract_update_ts is not None
            else elapsed
        )
        if self._contract_poll_attempts >= 3 and stale_for >= threshold:
            raise ContractRecoveryRequired(
                f"Active contract {self.active_contract} stale for {int(stale_for)}s"
            )

    # ─────────────────────────────────────
    # Main message handler
    # ─────────────────────────────────────
    async def _on_message(self, raw: str) -> None:
        data = json.loads(raw)
        msg_type = data.get("msg_type", "")

        if "error" in data:
            err = data["error"]
            logger.error(f"API error ({msg_type}): {err.get('message', err)}")
            # If it's a buy error, clear pending state
            if msg_type in ("buy", "proposal"):
                self.active_contract = None
                self._active_contract_symbol = None
                self._active_contract_started_at = None
                self._active_contract_expected_seconds = None
                self._last_contract_update_ts = None
                self._contract_poll_attempts = 0
                self._pending_proposal = None
            return

        if msg_type == "tick":
            tick = data.get("tick", {})
            result = self._process_tick(tick)
            await self._poll_stuck_contract()
            if result and not self.martingale.is_stopped:
                symbol, signal = result
                # Multi-index: check if this is the best index
                trackers = {
                    s: (self._digit_trackers[s], self._vol_trackers[s])
                    for s in self._digit_trackers
                }
                regimes = {
                    s: self._regime_detectors[s].current_regime
                    for s in self._regime_detectors
                }
                best = self.index_selector.best(trackers, regimes)
                if best and best.symbol == symbol:
                    self._last_best_symbol = best.symbol
                    await self._request_proposal(symbol, signal)
                elif best:
                    self._last_best_symbol = best.symbol
                    self._set_symbol_status(
                        symbol,
                        f"candidate, but {best.symbol} ranks higher ({best.adjusted_score:.3f})",
                        signal.composite_score,
                    )
            self._maybe_log_status()

        elif msg_type == "proposal":
            proposal = data.get("proposal", {})
            await self._execute_buy(proposal)

        elif msg_type == "buy":
            buy = data.get("buy", {})
            await self._handle_buy_result(buy)

        elif msg_type == "proposal_open_contract":
            contract = data.get("proposal_open_contract", {})
            self._handle_contract_update(contract)

        elif msg_type == "balance":
            bal = data.get("balance", {})
            self.equity = float(bal.get("balance", self.equity))

    # ─────────────────────────────────────
    # Run loop
    # ─────────────────────────────────────
    async def run(self) -> None:
        self._running = True
        retry_count = 0

        while self._running:
            try:
                await self.connect()
                authed = await self.authorize()
                if not authed:
                    logger.error("Authorization failed — stopping")
                    self._running = False
                    break

                await self.subscribe_ticks()
                await self._resume_active_contract_monitoring()
                retry_count = 0

                logger.info("═" * 60)
                logger.info("  Bot is LIVE — listening for signals...")
                logger.info(f"  Strategy: {self.cfg.strategy}")
                logger.info(f"  Indices: {self.cfg.index.symbols}")
                logger.info(f"  Base stake: ${self.cfg.martingale.base_stake_usd}")
                logger.info(f"  Profit target: ${self.cfg.martingale.profit_target_usd}")
                logger.info(f"  Loss limit: ${self.cfg.martingale.loss_limit_usd}")
                logger.info(f"  Score threshold: {self.cfg.ensemble.entry_score_threshold:.3f}")
                logger.info(f"  Kelly sizing: {'enabled' if self.cfg.kelly.enabled else 'disabled'}")
                logger.info(
                    f"  Require known regime: {'yes' if self.cfg.ensemble.require_known_regime else 'no'}"
                )
                logger.info(f"  Regime warmup: {self.cfg.hmm.lookback} returns per symbol")
                if self.cfg.contract.duration_unit == "t":
                    logger.info(
                        "  Expected duration: 1HZ*=~%ss, R_*=~%ss",
                        int(estimate_contract_seconds("1HZ10V", self.cfg.contract.duration, self.cfg.contract.duration_unit) or 0),
                        int(estimate_contract_seconds("R_10", self.cfg.contract.duration, self.cfg.contract.duration_unit) or 0),
                    )
                logger.info("═" * 60)

                async for message in self.ws:
                    if not self._running:
                        break
                    await self._on_message(message)

                    if self.martingale.is_stopped:
                        logger.info("Bot stopped by Martingale engine (profit/loss limit)")
                        self._running = False
                        break

            except websockets.ConnectionClosed as e:
                logger.warning(f"Connection closed: {e}. Reconnecting...")
                retry_count += 1
                await asyncio.sleep(min(self.cfg.api.reconnect_delay * retry_count, 60))

            except ContractRecoveryRequired as e:
                logger.warning("%s. Reconnecting and resuming monitoring...", e)
                retry_count = min(retry_count + 1, 3)
                await asyncio.sleep(min(self.cfg.api.reconnect_delay, 5))

            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                retry_count += 1
                await asyncio.sleep(min(self.cfg.api.reconnect_delay * retry_count, 60))
            finally:
                await self.close()

        logger.info("Bot shut down")
        self._write_app_json()
        self._print_summary()

    def stop(self) -> None:
        logger.info("Stop requested")
        self._running = False

    def _print_summary(self) -> None:
        total = len(self.trade_history)
        if total == 0:
            logger.info("No trades executed")
            return
        wins = sum(1 for t in self.trade_history if t.is_win)
        total_pnl = sum(t.profit for t in self.trade_history)
        logger.info("═" * 60)
        logger.info("  SESSION SUMMARY")
        logger.info(f"  Trades: {total} | Wins: {wins} | Losses: {total - wins}")
        logger.info(f"  Win rate: {wins/total:.1%}")
        logger.info(f"  Net P/L: ${total_pnl:+.2f}")
        logger.info(f"  Final equity: ${self.equity:.2f}")
        logger.info(f"  Martingale net: ${self.martingale.net_pnl:+.2f}")
        logger.info("═" * 60)


# ─────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────
def main():
    import argparse

    parser = argparse.ArgumentParser(description="Deriv Synthetic-Indices Trading Bot")
    parser.add_argument("--token", required=True, help="Deriv API token (OAuth or API token)")
    parser.add_argument(
        "--account-mode",
        choices=("demo", "real"),
        required=True,
        help="Expected Deriv account mode for the supplied token",
    )
    parser.add_argument("--app-id", type=int, default=1089, help="Deriv app_id")
    parser.add_argument("--base-stake", type=float, default=1.0, help="Base stake in USD")
    parser.add_argument("--profit-target", type=float, default=100.0, help="Stop after this profit")
    parser.add_argument("--loss-limit", type=float, default=-100.0, help="Stop after this loss")
    parser.add_argument("--multiplier", type=float, default=2.0, help="Martingale multiplier")
    parser.add_argument("--max-losses", type=int, default=4, help="Max consecutive losses before reset")
    parser.add_argument("--symbols", nargs="+", default=None, help="Symbols to trade")
    parser.add_argument("--duration", type=int, default=5, help="Contract duration (ticks)")
    parser.add_argument(
        "--max-contract-seconds",
        type=float,
        default=None,
        help="Skip symbols whose expected contract time exceeds this limit",
    )
    parser.add_argument(
        "--save-app-json",
        nargs="?",
        const="app_data.json",
        default=None,
        help="Write session and trade history JSON for app.html (default: app_data.json)",
    )
    parser.add_argument(
        "--disable-kelly",
        action="store_true",
        help="Disable Kelly sizing and use base stake plus Martingale only",
    )
    parser.add_argument(
        "--require-known-regime",
        action="store_true",
        help="Block entries until a symbol's regime is no longer UNKNOWN",
    )
    parser.add_argument("--score-threshold", type=float, default=0.60, help="Ensemble score threshold")
    parser.add_argument(
        "--strategy",
        choices=("ensemble", "alphabloom"),
        default="ensemble",
        help="Trading strategy: 'ensemble' (multi-signal) or 'alphabloom' (digit frequency)",
    )
    parser.add_argument(
        "--ab-window", type=int, default=60,
        help="AlphaBloom: number of ticks to analyse (default 60)",
    )
    parser.add_argument(
        "--ab-threshold", type=float, default=0.55,
        help="AlphaBloom: min even/odd percentage to trigger trade (default 0.55)",
    )
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(name)-12s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # Build config from CLI args
    cfg = BotConfig()
    cfg.api.app_id = args.app_id
    cfg.martingale.base_stake_usd = args.base_stake
    cfg.martingale.profit_target_usd = args.profit_target
    cfg.martingale.loss_limit_usd = args.loss_limit
    cfg.martingale.multiplier = args.multiplier
    cfg.martingale.max_consecutive_losses = args.max_losses
    cfg.contract.duration = args.duration
    cfg.ensemble.entry_score_threshold = args.score_threshold
    cfg.ensemble.require_known_regime = args.require_known_regime
    cfg.kelly.enabled = not args.disable_kelly
    cfg.strategy = args.strategy
    cfg.alphabloom.window_size = args.ab_window
    cfg.alphabloom.imbalance_threshold = args.ab_threshold
    if args.symbols:
        cfg.index.symbols = args.symbols
    if args.max_contract_seconds is not None:
        filtered_symbols = [
            symbol for symbol in cfg.index.symbols
            if (
                estimate_contract_seconds(symbol, cfg.contract.duration, cfg.contract.duration_unit) is not None
                and estimate_contract_seconds(symbol, cfg.contract.duration, cfg.contract.duration_unit) <= args.max_contract_seconds
            )
        ]
        excluded_symbols = sorted(set(cfg.index.symbols) - set(filtered_symbols))
        if not filtered_symbols:
            raise SystemExit(
                "No symbols left after applying --max-contract-seconds. "
                "Increase the limit or reduce --duration."
            )
        cfg.index.symbols = filtered_symbols
        if excluded_symbols:
            logging.getLogger("deriv_bot").info(
                "Filtered symbols by max contract seconds %.1f: excluded=%s",
                args.max_contract_seconds,
                excluded_symbols,
            )

    # Create and run bot
    bot = DerivBot(
        token=args.token,
        cfg=cfg,
        account_mode=args.account_mode,
        save_app_json=args.save_app_json,
    )

    # Graceful shutdown on Ctrl+C
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _request_stop() -> None:
        if loop.is_closed():
            return
        loop.call_soon_threadsafe(bot.stop)

    def _sync_signal_handler(signum, frame):
        _request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, _sync_signal_handler)

    try:
        loop.run_until_complete(bot.run())
    except KeyboardInterrupt:
        bot.stop()
    finally:
        pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    main()

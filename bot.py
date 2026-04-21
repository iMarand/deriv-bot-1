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
import random
import signal
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

import websockets

from config import BotConfig, TRADE_STRATEGIES
from analysis import DigitTracker, VolatilityTracker
from regime import RegimeDetector, Regime
from ensemble import EnsembleScorer, SignalSnapshot
from alphabloom import AlphaBloomScorer
from pulse import PulseScorer
from novaburst import NovaBurstScorer
from rollcake import RollCakeScorer
from zigzag import ZigzagScorer
from money import (
    MartingaleEngine,
    KellyCalculator,
    CircuitBreaker,
    TradeRecord,
)
from index_selector import IndexSelector
from filters import TimeFilter, ShadowTrader, ShadowTrade
from adaptive import VolatilityGate, VolatilityGateConfig, evaluate_gates, get_gate_stats
from hotness import HotnessTracker

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
        disable_risk_engine: bool = False,
        ml_filter=None,
        hotness_tracker=None,
        vol_gate: Optional[VolatilityGate] = None,
    ):
        self.token = token
        self.cfg = cfg or BotConfig()
        self.account_mode = account_mode
        self.save_app_json = Path(save_app_json).resolve() if save_app_json else None
        self._disable_risk_engine = disable_risk_engine
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._req_id = 0

        # Per-index analysis components
        self._digit_trackers: Dict[str, DigitTracker] = {}
        self._vol_trackers: Dict[str, VolatilityTracker] = {}
        self._regime_detectors: Dict[str, RegimeDetector] = {}
        self._ab_scorers: Dict[str, AlphaBloomScorer] = {}  # per-symbol AlphaBloom
        self._pulse_scorers: Dict[str, PulseScorer] = {}  # per-symbol Pulse
        self._novaburst_scorers: Dict[str, NovaBurstScorer] = {}  # per-symbol NovaBurst
        self._rollcake_scorers: Dict[str, RollCakeScorer] = {}  # per-symbol RollCake
        self._zigzag_scorers: Dict[str, ZigzagScorer] = {}  # per-symbol Zigzag

        # Shared components
        self.scorer = EnsembleScorer(self.cfg.ensemble)
        self.martingale = MartingaleEngine(self.cfg.martingale)
        self.martingale.disable_risk_engine = disable_risk_engine
        self.kelly = KellyCalculator(self.cfg.kelly)
        self.circuit_breaker = CircuitBreaker(self.cfg.circuit_breaker)
        self.index_selector = IndexSelector(self.cfg.index)
        self.time_filter = TimeFilter(self.cfg.time_filter)
        self.shadow = ShadowTrader(self.cfg.shadow)

        # Optional adaptive gates (any may be None)
        self.ml_filter = ml_filter
        self.hotness_tracker = hotness_tracker
        self.vol_gate = vol_gate

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
        self._last_trade_ts: float = 0.0  # for adaptive idle bypass
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
            ab_cfg = self.cfg.alphabloom
            self._ab_scorers[symbol] = AlphaBloomScorer(
                window_size=ab_cfg.window_size,
                imbalance_threshold=ab_cfg.imbalance_threshold,
                cooldown_ticks=ab_cfg.cooldown_ticks,
                trend_window=ab_cfg.trend_window,
            )
            p_cfg = self.cfg.pulse
            self._pulse_scorers[symbol] = PulseScorer(
                fast_window=p_cfg.fast_window,
                slow_window=p_cfg.slow_window,
                micro_window=p_cfg.micro_window,
                min_fast_pct=p_cfg.min_fast_pct,
                cooldown_ticks=p_cfg.cooldown_ticks,
            )
            # NovaBurst
            nb_cfg = self.cfg.novaburst
            self._novaburst_scorers[symbol] = NovaBurstScorer(nb_cfg)
            # Pattern detectors (used by trade strategies)
            rc_cfg = self.cfg.rollcake
            self._rollcake_scorers[symbol] = RollCakeScorer(
                window_size=rc_cfg.window_size,
                min_autocorrelation=rc_cfg.min_autocorrelation,
                cycle_lags=rc_cfg.cycle_lags,
                min_streak=rc_cfg.min_streak,
                cooldown_ticks=rc_cfg.cooldown_ticks,
            )
            zz_cfg = self.cfg.zigzag
            self._zigzag_scorers[symbol] = ZigzagScorer(
                tick_count=zz_cfg.tick_count,
                min_swings=zz_cfg.min_swings,
                amplitude_threshold=zz_cfg.amplitude_threshold,
                cooldown_ticks=zz_cfg.cooldown_ticks,
                lookback_buffer=zz_cfg.lookback_buffer,
            )
            self._tick_counts[symbol] = 0
            self._symbol_status[symbol] = "warming up"
            self._symbol_scores[symbol] = 0.0

    def _set_symbol_status(self, symbol: str, status: str, score: float = 0.0) -> None:
        self._symbol_status[symbol] = status
        self._symbol_scores[symbol] = score

    def _regime_required(self) -> bool:
        return self.cfg.ensemble.require_known_regime

    def _regime_ready(self, rd: RegimeDetector) -> bool:
        return (not self._regime_required()) or rd.current_regime != Regime.UNKNOWN

    def _direction_threshold(self, signal: SignalSnapshot) -> float:
        threshold = self.cfg.ensemble.entry_score_threshold
        if self.cfg.direction.even_priority and signal.direction == "ODD":
            threshold += self.cfg.direction.odd_extra_threshold
        return threshold

    def _direction_adjusted_score(self, signal: SignalSnapshot) -> float:
        adjusted = signal.composite_score
        if self.cfg.direction.even_priority and signal.direction == "EVEN":
            adjusted += self.cfg.direction.even_score_bonus
        return adjusted

    def _passes_direction_policy(self, signal: SignalSnapshot) -> bool:
        return signal.composite_score >= self._direction_threshold(signal)

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
        ps = self._pulse_scorers[symbol]
        ps.update(quote_str)
        nb = self._novaburst_scorers[symbol]
        nb.update(quote_str)
        # Feed price to pattern detectors
        rc = self._rollcake_scorers[symbol]
        rc.update(price)
        zz = self._zigzag_scorers[symbol]
        zz.update(price)

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

        if not self._regime_ready(rd):
            self._set_symbol_status(
                symbol,
                f"waiting for regime {len(rd.returns_buf)}/{self.cfg.hmm.lookback} returns",
            )
            return None

        # ── Strategy dispatch ──
        strategy = self.cfg.strategy
        if strategy == "alphabloom":
            return self._process_tick_alphabloom(symbol, ab, rd)
        elif strategy == "pulse":
            return self._process_tick_pulse(symbol, ps, rd)
        elif strategy == "novaburst":
            return self._process_tick_novaburst(symbol, nb, rd)
        elif strategy == "adaptive":
            # Adaptive reuses pulse's scan/selection, then bot-level gates filter.
            return self._process_tick_pulse(symbol, ps, rd)
        else:
            return self._process_tick_ensemble(symbol, dt, vt, rd)

    def _process_tick_alphabloom(
        self, symbol: str, ab: AlphaBloomScorer, rd: RegimeDetector,
    ) -> Optional[Tuple[str, SignalSnapshot]]:
        """AlphaBloom: scan all indices, trade the best one (EVEN or ODD)."""
        if not ab.warmed:
            self._set_symbol_status(symbol, f"warming {len(ab._digits)}/20 ticks")
            return None

        zone = ab.zone()
        self._set_symbol_status(
            symbol,
            f"[{zone}] {ab.direction} {ab.dominant_pct:.1%} trend={ab.trend:+.1%}",
            ab.dominant_pct if zone != "WAIT" else 0.0,
        )

        # Scan ALL symbols, pick strongest
        best_sym: Optional[str] = None
        best_sig: Optional[SignalSnapshot] = None
        best_rank: float = -1.0

        for s, s_ab in self._ab_scorers.items():
            if not s_ab.warmed:
                continue
            s_rd = self._regime_detectors.get(s)
            if s_rd is None or not self._regime_ready(s_rd):
                continue
            sig = s_ab.score(regime_det=s_rd)
            if sig is None or not self._passes_direction_policy(sig):
                continue
            rank = self._direction_adjusted_score(sig)
            if rank > best_rank:
                best_rank = rank
                best_sym = s
                best_sig = sig

        if best_sym is None or best_sig is None:
            current_sig = ab.score(regime_det=rd)
            if current_sig is not None and not self._passes_direction_policy(current_sig):
                needed = self._direction_threshold(current_sig)
                self._set_symbol_status(
                    symbol,
                    (
                        f"[{zone}] {current_sig.direction} blocked by even-priority "
                        f"({current_sig.composite_score:.3f} < {needed:.3f})"
                    ),
                    current_sig.composite_score,
                )
            return None
        if best_sym != symbol:
            self._set_symbol_status(
                symbol,
                f"[{zone}] — {best_sym} is better ({self._direction_adjusted_score(best_sig):.3f})",
                0.0,
            )
            return None

        self._set_symbol_status(
            symbol,
            f"[{zone}] TRADE {best_sig.direction} — {ab.dominant_pct:.1%} score={best_sig.composite_score:.3f}",
            best_sig.composite_score,
        )
        return (symbol, best_sig)

    def _process_tick_pulse(
        self, symbol: str, ps: PulseScorer, rd: RegimeDetector,
    ) -> Optional[Tuple[str, SignalSnapshot]]:
        """Pulse: dual-timeframe, scan all indices, trade the strongest aligned one."""
        if not ps.warmed:
            self._set_symbol_status(symbol, f"warming {len(ps._digits)}/{ps.slow_window} ticks")
            return None

        aligned = ps.aligned
        self._set_symbol_status(
            symbol,
            f"fast={ps.fast_dir}({ps.fast_even:.1%}) slow={ps.slow_dir}({ps.slow_even:.1%}) {'ALIGNED' if aligned else 'split'}",
            ps.fast_even if aligned else 0.0,
        )

        # Scan ALL symbols for best pulse signal
        best_sym: Optional[str] = None
        best_sig: Optional[SignalSnapshot] = None
        best_score: float = -1.0

        for s, s_ps in self._pulse_scorers.items():
            if not s_ps.warmed:
                continue
            s_rd = self._regime_detectors.get(s)
            if s_rd is None or not self._regime_ready(s_rd):
                continue
            sig = s_ps.score(regime_det=s_rd)
            if sig is None or not self._passes_direction_policy(sig):
                continue
            rank = self._direction_adjusted_score(sig)
            if rank > best_score:
                best_score = rank
                best_sym = s
                best_sig = sig

        if best_sym is None or best_sig is None:
            current_sig = ps.score(regime_det=rd)
            if current_sig is not None and not self._passes_direction_policy(current_sig):
                needed = self._direction_threshold(current_sig)
                self._set_symbol_status(
                    symbol,
                    (
                        f"{current_sig.direction} blocked by even-priority "
                        f"({current_sig.composite_score:.3f} < {needed:.3f})"
                    ),
                    current_sig.composite_score,
                )
            return None
        if best_sym != symbol:
            return None

        self._set_symbol_status(
            symbol,
            f"TRADE {best_sig.direction} — fast={ps.fast_even:.1%} slow={ps.slow_even:.1%} score={best_sig.composite_score:.3f}",
            best_sig.composite_score,
        )
        return (symbol, best_sig)

    def _process_tick_novaburst(
        self, symbol: str, nb: NovaBurstScorer, rd: RegimeDetector,
    ) -> Optional[Tuple[str, SignalSnapshot]]:
        """NovaBurst: multi-layer convergence, scan all indices."""
        if not nb.warmed:
            self._set_symbol_status(symbol, f"warming {nb._tick_count}/{nb.cfg.min_warmup} ticks")
            return None

        self._set_symbol_status(
            symbol,
            f"NB {nb.status_summary}",
            0.0,
        )

        # Scan ALL symbols for best NovaBurst signal
        best_sym: Optional[str] = None
        best_sig: Optional[SignalSnapshot] = None
        best_score: float = -1.0

        for s, s_nb in self._novaburst_scorers.items():
            if not s_nb.warmed:
                continue
            s_rd = self._regime_detectors.get(s)
            if s_rd is None or not self._regime_ready(s_rd):
                continue
            sig = s_nb.score(regime_det=s_rd)
            if sig is None or not self._passes_direction_policy(sig):
                continue
            rank = self._direction_adjusted_score(sig)
            if rank > best_score:
                best_score = rank
                best_sym = s
                best_sig = sig

        if best_sym is None or best_sig is None:
            return None
        if best_sym != symbol:
            return None

        self._set_symbol_status(
            symbol,
            f"NB TRADE {best_sig.direction} score={best_sig.composite_score:.3f} {nb.status_summary}",
            best_sig.composite_score,
        )
        return (symbol, best_sig)

    def _process_tick_ensemble(
        self, symbol: str, dt: DigitTracker, vt: VolatilityTracker, rd: RegimeDetector,
    ) -> Optional[Tuple[str, SignalSnapshot]]:
        """Ensemble strategy: multi-signal scoring with dual-window confirmation."""
        signal = self.scorer.score(dt, vt, rd)
        if signal is None:
            if len(dt.window) < 20:
                self._set_symbol_status(symbol, f"warming {len(dt.window)}/20 ticks")
            elif dt.bias_magnitude < 0.02:
                self._set_symbol_status(symbol, f"bias too low ({dt.bias_magnitude:.3f})")
            else:
                self._set_symbol_status(symbol, "no signal")
            return None

        threshold = self._direction_threshold(signal)
        if signal.composite_score < threshold:
            if self.cfg.direction.even_priority and signal.direction == "ODD":
                self._set_symbol_status(
                    symbol,
                    f"ODD blocked by even-priority ({signal.composite_score:.3f} < {threshold:.3f})",
                    signal.composite_score,
                )
                return None
            self._set_symbol_status(
                symbol,
                f"score {signal.composite_score:.3f} < {threshold:.3f}",
                signal.composite_score,
            )
            return None

        self._set_symbol_status(
            symbol,
            f"candidate {signal.direction} score={signal.composite_score:.3f} bias={signal.digit_bias:.3f}",
            signal.composite_score,
        )
        return (symbol, signal)

    def _gates_accept(self, symbol: str, signal: SignalSnapshot) -> bool:
        """Consult optional ML / hotness / volatility gates. Returns True when
        no gate is active OR all active gates accept. Writes a human-readable
        status on rejection so the reason is visible in the dashboard.
        """
        if self.ml_filter is None and self.hotness_tracker is None and self.vol_gate is None:
            return True

        contract_type = self._resolve_contract_type(signal)
        vt = self._vol_trackers.get(symbol)
        decision = evaluate_gates(
            symbol, contract_type, time.time(),
            ml_filter=self.ml_filter,
            hotness=self.hotness_tracker,
            vol_gate=self.vol_gate,
            vt=vt,
            last_trade_ts=self._last_trade_ts,
            all_symbols=list(self.cfg.index.symbols),
        )
        if not decision.accept:
            self._set_symbol_status(symbol, decision.reason, signal.composite_score)
            logger.info(
                "GATE SKIP %s %s score=%.3f — %s",
                symbol, signal.direction, signal.composite_score, decision.reason,
            )
        return decision.accept

    def _pattern_filter_passes(self, symbol: str, signal: SignalSnapshot) -> bool:
        """Check if the selected trade strategy's pattern filter confirms the signal.

        For even_odd trade strategy, no pattern filter is needed.
        For rollcake/zigzag strategies, the pattern detector must agree.
        """
        ts_info = TRADE_STRATEGIES.get(self.cfg.trade_strategy, {})
        pattern = ts_info.get("pattern")
        if pattern is None:
            return True  # No pattern filter for even_odd

        if pattern == "rollcake":
            rc = self._rollcake_scorers.get(symbol)
            if rc is None or not rc.warmed:
                return False
            result = rc.score()
            if result is None:
                return False
            rc_dir, rc_conf = result
            # Pattern must have minimum confidence
            if rc_conf < 0.25:
                return False
            # Attach pattern info to signal
            signal.trade_strategy = self.cfg.trade_strategy
            return True

        if pattern == "zigzag":
            zz = self._zigzag_scorers.get(symbol)
            if zz is None or not zz.warmed:
                return False
            result = zz.score()
            if result is None:
                return False
            zz_dir, zz_conf = result
            if zz_conf < 0.20:
                return False
            signal.trade_strategy = self.cfg.trade_strategy
            return True

        return True

    def _resolve_contract_type(self, signal: SignalSnapshot) -> str:
        """Map the algorithm's signal direction + trade_strategy to a Deriv contract_type."""
        ts = self.cfg.trade_strategy
        ts_info = TRADE_STRATEGIES.get(ts, {})
        contracts = ts_info.get("contracts", ("DIGITEVEN", "DIGITODD"))

        if ts == "even_odd":
            return f"DIGIT{signal.direction}"  # DIGITEVEN or DIGITODD

        # For RISE/FALL, HIGHER/LOWER: use pattern direction
        if ts in ("rise_fall_roll", "rise_fall_zigzag", "higher_lower_roll", "higher_lower_zigzag"):
            # Get pattern direction
            pattern = ts_info.get("pattern")
            pattern_dir = self._get_pattern_direction(signal, pattern)
            return "CALL" if pattern_dir == "RISE" else "PUT"

        # For OVER/UNDER: use digit bias direction
        if ts == "over_under_roll":
            return "DIGITOVER" if signal.direction == "EVEN" else "DIGITUNDER"

        # For TOUCH/NO TOUCH: use zigzag direction
        if ts == "touch_notouch_zigzag":
            zz = self._zigzag_scorers.get(self._current_symbol or "")
            if zz:
                result = zz.score()
                if result:
                    zz_dir, _ = result
                    return "ONETOUCH" if zz_dir == "RISE" else "NOTOUCH"
            return "ONETOUCH"

        return f"DIGIT{signal.direction}"

    def _get_pattern_direction(self, signal: SignalSnapshot, pattern: str) -> str:
        """Get the pattern detector's direction prediction."""
        sym = self._current_symbol or ""
        if pattern == "rollcake":
            rc = self._rollcake_scorers.get(sym)
            if rc:
                result = rc.score()
                if result:
                    return result[0]  # "RISE" or "FALL"
        elif pattern == "zigzag":
            zz = self._zigzag_scorers.get(sym)
            if zz:
                result = zz.score()
                if result:
                    return result[0]
        # Fallback: use digit bias as direction hint
        return "RISE" if signal.direction == "EVEN" else "FALL"

    def _compute_barrier(self, symbol: str) -> Optional[float]:
        """Compute a barrier value for contracts that need one.

        Uses recent price data from the zigzag/rollcake scorers.
        """
        ts = self.cfg.trade_strategy
        ts_info = TRADE_STRATEGIES.get(ts, {})

        if ts_info.get("digit_barrier"):
            # OVER/UNDER: barrier is a digit (default 4 or 5)
            return None  # Handled separately via digit_barrier field

        if not ts_info.get("barrier"):
            return None

        # Price barrier: use recent high/low from zigzag scorer
        zz = self._zigzag_scorers.get(symbol)
        vt = self._vol_trackers.get(symbol)

        if zz and zz.warmed:
            current = zz.current_price
            high = zz.recent_high
            low = zz.recent_low
            spread = high - low
            if spread > 0:
                # For HIGHER: barrier slightly above current
                # For TOUCH: barrier at recent high/low
                offset = spread * 0.3 + self.cfg.contract.barrier_offset
                return round(current + offset, 2)

        # Fallback: use ATR if available
        if vt and vt.atr is not None:
            prices = list(vt.prices)
            if prices:
                current = prices[-1]
                return round(current + vt.atr * 0.5, 2)

        return None

    def _compute_digit_barrier(self) -> int:
        """Compute digit barrier for OVER/UNDER contracts."""
        # Default: 4 (DIGITOVER 4 means last digit > 4, i.e. 5-9)
        return 4

    async def _request_proposal(self, symbol: str, signal: SignalSnapshot) -> None:
        contract_type = self._resolve_contract_type(signal)
        threshold = self._direction_threshold(signal)
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

        # Pattern filter: confirm with rollcake/zigzag if applicable
        if not self._pattern_filter_passes(symbol, signal):
            self._set_symbol_status(
                symbol,
                f"pattern filter blocked ({self.cfg.trade_strategy})",
                signal.composite_score,
            )
            return

        kf = self.kelly.kelly_fraction()
        stake = self.martingale.next_stake(self.equity, kf, skip_cooldown=self._disable_risk_engine)
        if not self._disable_risk_engine:
            stake = self.circuit_breaker.adjust_stake(stake)
        if stake <= 0:
            self._set_symbol_status(symbol, "stake skipped by risk engine")
            return

        # Determine duration from trade strategy
        ts_info = TRADE_STRATEGIES.get(self.cfg.trade_strategy, {})
        duration = ts_info.get("duration", self.cfg.contract.duration)

        proposal = {
            "proposal": 1,
            "amount": stake,
            "basis": self.cfg.contract.basis,
            "contract_type": contract_type,
            "currency": self.cfg.contract.currency,
            "duration": duration,
            "duration_unit": self.cfg.contract.duration_unit,
            "symbol": symbol,
        }

        # Add barrier if needed
        if ts_info.get("barrier"):
            barrier = self._compute_barrier(symbol)
            if barrier is not None:
                proposal["barrier"] = str(barrier)
            else:
                logger.warning("Cannot compute barrier for %s — skipping", self.cfg.trade_strategy)
                return

        if ts_info.get("digit_barrier"):
            digit_b = self._compute_digit_barrier()
            proposal["barrier"] = str(digit_b)

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
            duration,
            self.cfg.contract.duration_unit,
        )

        barrier_str = ""
        if "barrier" in proposal:
            barrier_str = f" | barrier={proposal['barrier']}"

        logger.info(
            f"Requesting proposal: {contract_type} on {symbol} | "
            f"stake=${stake:.2f} | score={signal.composite_score:.3f} | "
            f"threshold={threshold:.3f} | regime={signal.regime.name} | "
            f"trade_strategy={self.cfg.trade_strategy}{barrier_str} | "
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
        # Notify scorers of trade placement (cooldown)
        sym = self._active_contract_symbol
        if sym:
            if sym in self._ab_scorers:
                self._ab_scorers[sym].on_trade_placed()
            if sym in self._pulse_scorers:
                self._pulse_scorers[sym].on_trade_placed()
            if sym in self._novaburst_scorers:
                self._novaburst_scorers[sym].on_trade_placed()
            if sym in self._rollcake_scorers:
                self._rollcake_scorers[sym].on_trade_placed()
            if sym in self._zigzag_scorers:
                self._zigzag_scorers[sym].on_trade_placed()
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
        # Adaptive gates: feed result so rolling WR and ML feature-state stay current
        if self.hotness_tracker is not None:
            self.hotness_tracker.on_trade_result(symbol, is_win)
        if self.ml_filter is not None:
            self.ml_filter.on_trade_result(symbol, contract_type, trade.timestamp, is_win)
        # Update NovaBurst Bayesian posterior with trade outcome
        if symbol in self._novaburst_scorers:
            self._novaburst_scorers[symbol].on_trade_result(is_win)
        # Update Ensemble streak momentum
        if self._current_signal:
            self.scorer.record_result(self._current_signal.direction, is_win)
        self._trade_count += 1
        self._last_trade_ts = time.time()  # update for adaptive idle bypass
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
        # ── Proposal timeout: if we've been waiting for a proposal response
        # for over 30 seconds, the API likely dropped the request. Reset.
        if self.active_contract == "proposal_pending":
            if self._active_contract_started_at:
                proposal_elapsed = time.time() - self._active_contract_started_at
                if proposal_elapsed > 30.0:
                    logger.warning(
                        "PROPOSAL TIMEOUT: %s waited %ds for proposal response — resetting to allow new trades",
                        self._active_contract_symbol,
                        int(proposal_elapsed),
                    )
                    self._clear_active_contract()
            return

        if not self.active_contract:
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
                if self.cfg.strategy == "ensemble":
                    trackers = {
                        s: (self._digit_trackers[s], self._vol_trackers[s])
                        for s in self._digit_trackers
                        if self._regime_ready(self._regime_detectors[s])
                    }
                    regimes = {
                        s: self._regime_detectors[s].current_regime
                        for s in trackers
                    }
                    best = self.index_selector.best(trackers, regimes)
                    if best and best.symbol == symbol:
                        self._last_best_symbol = best.symbol
                        if self._gates_accept(symbol, signal):
                            await self._request_proposal(symbol, signal)
                    elif best:
                        self._last_best_symbol = best.symbol
                        self._set_symbol_status(
                            symbol,
                            f"candidate, but {best.symbol} ranks higher ({best.adjusted_score:.3f})",
                            signal.composite_score,
                        )
                else:
                    self._last_best_symbol = symbol
                    if self._gates_accept(symbol, signal):
                        await self._request_proposal(symbol, signal)
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
                logger.info(f"  Algorithm: {self.cfg.strategy}")
                logger.info(f"  Trade Strategy: {self.cfg.trade_strategy}")
                if self.cfg.strategy == "alphabloom":
                    ab_cfg = self.cfg.alphabloom
                    logger.info(f"  AB window: {ab_cfg.window_size} ticks")
                    logger.info(f"  AB GREEN zone: dominant ≥ {ab_cfg.imbalance_threshold:.0%}")
                    logger.info("  AB MOMENTUM zone: 50-55% + rising trend")
                    logger.info("  AB trades EVEN or ODD — picks best index")
                elif self.cfg.strategy == "pulse":
                    p_cfg = self.cfg.pulse
                    logger.info(f"  Pulse micro: {p_cfg.micro_window} | fast: {p_cfg.fast_window} | slow: {p_cfg.slow_window} ticks")
                    logger.info(f"  Pulse min fast edge: {p_cfg.min_fast_pct:.0%}")
                    logger.info("  Pulse mode: tri-timeframe, trades when fast+slow agree")
                elif self.cfg.strategy == "novaburst":
                    nb_cfg = self.cfg.novaburst
                    logger.info(f"  NovaBurst windows: {nb_cfg.windows}")
                    logger.info(f"  NovaBurst consensus: {nb_cfg.min_consensus}/{len(nb_cfg.windows)}")
                    logger.info(f"  NovaBurst Bayesian gate: {nb_cfg.bayesian_gate}")
                    logger.info(f"  NovaBurst Markov order: {nb_cfg.markov_order}")
                ts_info = TRADE_STRATEGIES.get(self.cfg.trade_strategy, {})
                contracts = ts_info.get("contracts", ("?",))
                logger.info(f"  Contract types: {contracts}")
                logger.info(f"  Pattern filter: {ts_info.get('pattern', 'none')}")
                logger.info(f"  Indices: {self.cfg.index.symbols}")
                logger.info(f"  Base stake: ${self.cfg.martingale.base_stake_usd}")
                logger.info(f"  Martingale multiplier: x{self.cfg.martingale.multiplier}")
                pt = self.cfg.martingale.profit_target_usd
                ll = self.cfg.martingale.loss_limit_usd
                logger.info("  Profit target: %s", f"${pt}" if pt is not None else "unlimited")
                logger.info("  Loss limit:    %s", f"${ll}" if ll is not None else "unlimited")
                logger.info(f"  Score threshold: {self.cfg.ensemble.entry_score_threshold:.3f}")
                logger.info(
                    "  Even priority: %s",
                    (
                        "enabled "
                        f"(ODD needs +{self.cfg.direction.odd_extra_threshold:.2f}, "
                        f"EVEN bonus +{self.cfg.direction.even_score_bonus:.2f})"
                        if self.cfg.direction.even_priority
                        else "disabled"
                    ),
                )
                logger.info(f"  Kelly sizing: {'enabled' if self.cfg.kelly.enabled else 'disabled'}")
                logger.info(f"  Risk engine:  {'DISABLED (no cooldown, no circuit breaker)' if self._disable_risk_engine else 'enabled'}")
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
        # Adaptive gate statistics
        if self.cfg.strategy == "adaptive":
            stats = get_gate_stats()
            logger.info("  %s", stats.summary())
            if self.ml_filter is not None:
                logger.info("  %s", self.ml_filter.summary())
        logger.info(f"  Trade strategy: {self.cfg.trade_strategy}")
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
    parser.add_argument("--profit-target", type=float, default=None, help="Stop after reaching this net profit (omit to run forever)")
    parser.add_argument("--loss-limit", type=float, default=None, help="Stop after hitting this net loss, e.g. -50 (omit to run forever)")
    parser.add_argument("--martingale", type=float, default=2.0, help="Martingale multiplier on loss, e.g. 2.0")
    parser.add_argument("--max-stake", type=float, default=50.0, help="Maximum stake per trade in USD (caps Martingale escalation)")
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
        "--disable-kelly",
        action="store_true",
        help="Disable Kelly sizing and use base stake plus Martingale only",
    )
    parser.add_argument(
        "--disable-risk-engine",
        action="store_true",
        help="Disable circuit breaker and martingale cooldown — martingale progression continues uninterrupted",
    )
    parser.add_argument(
        "--require-known-regime",
        action="store_true",
        help="Block entries until a symbol's regime is no longer UNKNOWN",
    )
    parser.add_argument(
        "--even-priority",
        action="store_true",
        help="Prefer DIGITEVEN trades and require a higher score for DIGITODD",
    )
    parser.add_argument(
        "--odd-extra-threshold",
        type=float,
        default=0.12,
        help="Extra score required for DIGITODD when --even-priority is enabled",
    )
    parser.add_argument(
        "--even-score-bonus",
        type=float,
        default=0.05,
        help="Ranking bonus added to DIGITEVEN when --even-priority is enabled",
    )
    parser.add_argument("--score-threshold", type=float, default=0.60, help="Ensemble score threshold")
    parser.add_argument(
        "--strategy",
        choices=("ensemble", "alphabloom", "pulse", "novaburst", "adaptive"),
        default="ensemble",
        help="Algorithm: 'ensemble', 'alphabloom', 'pulse', 'novaburst', or 'adaptive' (pulse + ML/hotness/vol gates)",
    )
    parser.add_argument(
        "--trade-strategy",
        choices=("even_odd", "rise_fall_roll", "rise_fall_zigzag",
                 "higher_lower_roll", "higher_lower_zigzag",
                 "over_under_roll", "touch_notouch_zigzag"),
        default="even_odd",
        help="Trade strategy: contract type + pattern filter (default: even_odd)",
    )
    parser.add_argument(
        "--ml-filter",
        action="store_true",
        help="Gate candidate trades through the trained ML filter (data/ml_filter.pkl).",
    )
    parser.add_argument(
        "--ml-model", type=str, default="data/ml_filter.pkl",
        help="Path to trained ML model pickle",
    )
    parser.add_argument(
        "--ml-threshold", type=float, default=None,
        help="P(win) cutoff override (default: value baked into the model)",
    )
    parser.add_argument(
        "--hotness-window", type=int, default=50,
        help="Adaptive: rolling window for per-symbol WR (default 50)",
    )
    parser.add_argument(
        "--hotness-cold", type=float, default=0.43,
        help="Adaptive: skip symbols whose rolling WR is below this (default 0.43)",
    )
    parser.add_argument(
        "--hotness-probe", type=int, default=20,
        help="Adaptive: when cold, allow 1 in N candidates through as recovery probe (default 20)",
    )
    parser.add_argument(
        "--ml-idle-minutes", type=float, default=10.0,
        help="ML filter: bypass threshold after this many idle minutes (default 10)",
    )
    parser.add_argument(
        "--ml-floor", type=float, default=0.35,
        help="ML filter: absolute minimum threshold even during bypass (default 0.35)",
    )
    parser.add_argument(
        "--vol-skip-pct", type=float, default=0.75,
        help="Adaptive: skip when ATR percentile >= this (default 0.75 = skip top 25%%)",
    )
    parser.add_argument(
        "--disable-vol-gate", action="store_true",
        help="Adaptive: disable the volatility-regime gate",
    )
    parser.add_argument(
        "--ab-window", type=int, default=60,
        help="AlphaBloom: number of ticks to analyse (default 60)",
    )
    parser.add_argument(
        "--ab-threshold", type=float, default=0.55,
        help="AlphaBloom: min even/odd percentage to trigger trade (default 0.55)",
    )
    parser.add_argument(
        "--pulse-fast", type=int, default=15,
        help="Pulse: fast window size in ticks (default 15)",
    )
    parser.add_argument(
        "--pulse-slow", type=int, default=50,
        help="Pulse: slow window size in ticks (default 50)",
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
    cfg.martingale.profit_target_usd = args.profit_target   # None = no stop
    cfg.martingale.loss_limit_usd = args.loss_limit         # None = no stop
    cfg.martingale.multiplier = args.martingale
    cfg.martingale.max_stake_usd = args.max_stake
    cfg.martingale.max_consecutive_losses = args.max_losses
    cfg.contract.duration = args.duration
    cfg.ensemble.entry_score_threshold = args.score_threshold
    cfg.ensemble.require_known_regime = args.require_known_regime
    cfg.direction.even_priority = args.even_priority
    cfg.direction.odd_extra_threshold = args.odd_extra_threshold
    cfg.direction.even_score_bonus = args.even_score_bonus
    cfg.kelly.enabled = not args.disable_kelly
    cfg.strategy = args.strategy
    cfg.trade_strategy = args.trade_strategy
    cfg.pulse.fast_window = args.pulse_fast
    cfg.pulse.slow_window = args.pulse_slow
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

    # Auto-generate session file in data/ folder
    data_dir = Path(__file__).parent / "data"
    session_file = data_dir / f"{random.randint(1000, 9999)}-trades.json"
    logging.getLogger("deriv_bot").info("Session data → %s", session_file)

    # Optional adaptive gates — only built when --strategy adaptive or --ml-filter is set
    ml_filter_instance = None
    hotness_instance = None
    vol_gate_instance = None
    if args.strategy == "adaptive" or args.ml_filter:
        if args.ml_filter or args.strategy == "adaptive":
            from ml_filter import MLFilter
            try:
                ml_filter_instance = MLFilter(
                    args.ml_model,
                    threshold=args.ml_threshold,
                    max_idle_minutes=args.ml_idle_minutes,
                    absolute_floor=args.ml_floor,
                )
                # Pre-seed with recent session data to avoid cold-start
                data_dir = Path(__file__).parent / "data"
                ml_filter_instance.warm_from_session(data_dir)
            except FileNotFoundError as e:
                logging.getLogger("deriv_bot").warning(
                    "ML filter unavailable: %s  (continuing without ML gate)", e,
                )
    if args.strategy == "adaptive":
        hotness_instance = HotnessTracker(
            window=args.hotness_window,
            cold_threshold=args.hotness_cold,
            probe_interval=args.hotness_probe,
        )
        vol_gate_instance = VolatilityGate(VolatilityGateConfig(
            enabled=not args.disable_vol_gate,
            skip_above=args.vol_skip_pct,
        ))

    # Create and run bot
    bot = DerivBot(
        token=args.token,
        cfg=cfg,
        account_mode=args.account_mode,
        save_app_json=str(session_file),
        disable_risk_engine=args.disable_risk_engine,
        ml_filter=ml_filter_instance,
        hotness_tracker=hotness_instance,
        vol_gate=vol_gate_instance,
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

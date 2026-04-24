"""
Deriv Synthetic-Indices Trading Bot — Configuration
All tunable parameters in one place.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DigitConfig:
    """Digit-distribution analysis parameters."""
    window_size: int = 50              # ticks to track digit distribution
    even_odd_threshold: float = 0.55   # min P(even) or P(odd) to trigger signal
    chi_sq_alpha: float = 0.05         # significance level for chi-square test
    cusum_drift: float = 0.02          # CUSUM drift parameter for change-point detection
    cusum_threshold: float = 1.0       # CUSUM alarm threshold


@dataclass
class VolatilityConfig:
    """Volatility estimation parameters."""
    atr_period: int = 14
    rolling_std_window: int = 30
    realized_vol_window: int = 50
    low_vol_percentile: float = 30.0   # ATR below this percentile → low-vol regime
    high_vol_percentile: float = 70.0  # ATR above this percentile → high-vol regime


@dataclass
class HMMConfig:
    """Hidden Markov Model regime detection."""
    n_regimes: int = 3                 # trending, mean-reverting, choppy
    lookback: int = 200                # ticks to fit the HMM
    retrain_interval: int = 200        # retrain every N ticks (increased for performance)


@dataclass
class EnsembleConfig:
    """Ensemble signal scoring weights (logistic combination)."""
    weight_digit_bias: float = 0.35
    weight_chi_sq: float = 0.20
    weight_entropy: float = 0.15
    weight_momentum: float = 0.15
    weight_regime: float = 0.15
    entry_score_threshold: float = 0.65  # raised from 0.60 — only trade strong signals
    require_known_regime: bool = False


@dataclass
class KellyConfig:
    """Kelly Criterion adaptive stake sizing."""
    enabled: bool = True
    lookback_trades: int = 30          # trades to estimate win-rate / payout
    kelly_fraction: float = 0.5        # half-Kelly for safety
    min_trades_for_kelly: int = 15     # need at least this many trades before using Kelly


@dataclass
class MartingaleConfig:
    """Controlled Martingale money management."""
    multiplier: float = 1.8              # reduced from 2.0 — slower escalation
    max_consecutive_losses: int = 5       # increased from 4 — gives Martingale more room
    max_stake_usd: float = 50.0
    base_stake_usd: float = 1.0
    profit_target_usd: Optional[float] = None  # None = run forever (no profit stop)
    loss_limit_usd: Optional[float] = None     # None = run forever (no loss stop)
    bankroll_fraction: float = 0.02    # max base stake as fraction of equity


@dataclass
class CircuitBreakerConfig:
    """Drawdown-responsive circuit breakers."""
    equity_ma_window: int = 20         # moving average window of equity curve
    drawdown_pct_trigger: float = 0.10 # if equity drops 10% below MA, activate
    cooldown_ticks: int = 50           # ticks to wait when circuit breaker fires
    reduced_stake_fraction: float = 0.5 # reduce stake to this fraction during cooldown


@dataclass
class TimeFilterConfig:
    """Time-of-day / session pattern filters."""
    enabled: bool = False               # disabled by default until enough data
    weak_hours_utc: List[int] = field(default_factory=lambda: [3, 4, 5])  # hours to avoid
    min_sample_per_hour: int = 50       # trades per hour before we trust the filter


@dataclass
class ShadowConfig:
    """Paper-trading shadow mode for A/B testing."""
    enabled: bool = False
    min_shadow_trades: int = 50         # trades before promoting shadow params


@dataclass
class AlphaBloomConfig:
    """AlphaBloom digit-frequency strategy parameters."""
    window_size: int = 60              # ticks to analyse
    imbalance_threshold: float = 0.55  # min Even% for GREEN zone (instant trade)
    cooldown_ticks: int = 2            # ticks to wait after a trade
    trend_window: int = 8              # ticks to measure Even% trend direction


@dataclass
class PulseConfig:
    """Pulse dual-timeframe strategy parameters."""
    fast_window: int = 15              # fast digit window
    slow_window: int = 50              # slow digit window
    micro_window: int = 7              # micro (ultra-fast) digit window
    min_fast_pct: float = 0.53         # min dominant% in fast window
    cooldown_ticks: int = 1            # ticks to wait after a trade


@dataclass
class RollCakeConfig:
    """Roll Cake autocorrelation-based cycle detection strategy parameters."""
    window_size: int = 30              # ticks for pattern analysis
    min_autocorrelation: float = 0.25  # minimum autocorrelation for valid cycle
    cycle_lags: tuple = (2, 3, 4, 5, 6)  # cycle lengths to test
    min_streak: int = 3                # minimum streak length to consider
    cooldown_ticks: int = 2            # ticks to wait after a trade


@dataclass
class ZigzagConfig:
    """Zigzag 7-tick reversal detection strategy parameters."""
    tick_count: int = 7                # ticks per zigzag window
    min_swings: int = 3                # minimum direction changes for valid zigzag
    amplitude_threshold: float = 0.0001  # minimum move to count as significant
    cooldown_ticks: int = 2            # ticks to wait after a trade
    lookback_buffer: int = 50          # total buffer for additional analysis


@dataclass
class NovaBurstConfig:
    """NovaBurst multi-layer probabilistic algorithm parameters."""
    windows: tuple = (10, 25, 60)      # multi-scale analysis windows
    min_consensus: int = 2             # at least N of len(windows) must agree
    bayesian_gate: float = 0.55        # posterior P(correct) must exceed this
    markov_order: int = 2              # N-gram order for transition matrix
    momentum_decay_window: int = 15    # ticks to measure momentum decay
    min_warmup: int = 25               # minimum ticks before any signal
    cooldown_ticks: int = 2            # ticks to wait after a trade


@dataclass
class AegisConfig:
    """Aegis highly sophisticated defensive algorithm parameters."""
    rsi_period: int = 14
    volatility_window: int = 20
    digit_window: int = 25
    min_warmup: int = 60
    cooldown_ticks: int = 3


@dataclass
class IndexConfig:
    """Multi-index selection."""
    symbols: List[str] = field(default_factory=lambda: [
        "R_10", "R_25", "R_50", "R_75", "R_100",
        "1HZ10V", "1HZ25V", "1HZ50V", "1HZ75V", "1HZ100V",
    ])
    correlation_penalty: float = 0.3    # penalise correlated indices
    min_score_to_trade: float = 0.03    # minimum |bias| score to consider trading


@dataclass
class ContractConfig:
    """Contract / trade parameters."""
    contract_type: str = "DIGITEVEN"    # overridden dynamically
    duration: int = 5                   # ticks
    duration_unit: str = "t"
    currency: str = "USD"
    basis: str = "stake"
    barrier_offset: float = 0.0        # for higher/lower, touch/notouch


@dataclass
class DirectionPreferenceConfig:
    """Optional bias toward EVEN trades across all strategies."""
    even_priority: bool = False
    even_score_bonus: float = 0.05
    odd_extra_threshold: float = 0.12


@dataclass
class APIConfig:
    """Deriv API connection settings."""
    ws_url: str = "wss://ws.derivws.com/websockets/v3"
    app_id: int = 1089                  # replace with your registered app_id
    max_req_per_sec: int = 80           # stay under 100 limit
    max_subscriptions: int = 90         # stay under 100 limit
    reconnect_delay: float = 5.0        # seconds before reconnect on disconnect


# ── Trade Strategy constants ──────────────────────────────────────────────────
# These map high-level strategy names to Deriv contract types.
# barrier_mode: "relative" = offset from spot (+/-X.XX), "absolute" = exact price
# duration_unit_override: override the default "t" (ticks) with e.g. "m" (minutes)
TRADE_STRATEGIES = {
    "even_odd":              {"contracts": ("DIGITEVEN", "DIGITODD"), "pattern": None, "duration": 5},
    "rise_fall_roll":        {"contracts": ("CALL", "PUT"),          "pattern": "rollcake", "duration": 5},
    "rise_fall_zigzag":      {"contracts": ("CALL", "PUT"),          "pattern": "zigzag",   "duration": 7},
    "higher_lower_roll":     {"contracts": ("CALL", "PUT"),          "pattern": "rollcake", "duration": 5,
                              "barrier": True, "barrier_mode": "relative"},
    "higher_lower_zigzag":   {"contracts": ("CALL", "PUT"),          "pattern": "zigzag",   "duration": 7,
                              "barrier": True, "barrier_mode": "relative"},
    "over_under_roll":       {"contracts": ("DIGITOVER", "DIGITUNDER"), "pattern": "rollcake", "duration": 5, "digit_barrier": True},
    "touch_notouch_zigzag":  {"contracts": ("ONETOUCH", "NOTOUCH"),  "pattern": "zigzag",
                              "duration": 5, "duration_unit_override": "m",
                              "barrier": True, "barrier_mode": "relative"},
}

TRADE_STRATEGY_CHOICES = list(TRADE_STRATEGIES.keys())


@dataclass
class BotConfig:
    """Master configuration aggregating all sub-configs."""
    digit: DigitConfig = field(default_factory=DigitConfig)
    volatility: VolatilityConfig = field(default_factory=VolatilityConfig)
    hmm: HMMConfig = field(default_factory=HMMConfig)
    ensemble: EnsembleConfig = field(default_factory=EnsembleConfig)
    kelly: KellyConfig = field(default_factory=KellyConfig)
    martingale: MartingaleConfig = field(default_factory=MartingaleConfig)
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)
    time_filter: TimeFilterConfig = field(default_factory=TimeFilterConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    alphabloom: AlphaBloomConfig = field(default_factory=AlphaBloomConfig)
    pulse: PulseConfig = field(default_factory=PulseConfig)
    rollcake: RollCakeConfig = field(default_factory=RollCakeConfig)
    zigzag: ZigzagConfig = field(default_factory=ZigzagConfig)
    novaburst: NovaBurstConfig = field(default_factory=NovaBurstConfig)
    aegis: AegisConfig = field(default_factory=AegisConfig)
    index: IndexConfig = field(default_factory=IndexConfig)
    contract: ContractConfig = field(default_factory=ContractConfig)
    direction: DirectionPreferenceConfig = field(default_factory=DirectionPreferenceConfig)
    api: APIConfig = field(default_factory=APIConfig)
    strategy: str = "ensemble"           # algorithm: "ensemble", "alphabloom", "pulse", "novaburst", "adaptive", "aegis"
    trade_strategy: str = "even_odd"     # contract strategy: see TRADE_STRATEGIES

# Deriv Synthetic-Indices Trading Bot

A sophisticated algorithmic trading bot for Deriv's synthetic indices, combining digit-distribution analysis, regime detection, ensemble signal scoring, and controlled Martingale money management.

## Architecture

```
deriv_bot/
├── config.py           # All tunable parameters
├── analysis.py         # Digit distribution + volatility trackers
├── regime.py           # HMM regime detection (trending/mean-reverting/choppy)
├── ensemble.py         # Composite signal scorer
├── money.py            # Kelly Criterion + Martingale + circuit breaker
├── index_selector.py   # Multi-index selection with correlation penalty
├── filters.py          # Time-of-day filter + shadow paper-trading
├── backtest.py         # Backtesting engine + Monte Carlo
├── bot.py              # Main trading bot (WebSocket, full loop)
├── run_backtest.py     # Backtest runner with synthetic data
└── requirements.txt
```

## Features

| Component | Description |
|---|---|
| **Digit Analysis** | Chi-square test, Bayesian Beta updates, CUSUM change-point detection, Shannon entropy |
| **Volatility** | Rolling std, ATR, realized volatility, price-change entropy |
| **HMM Regime Detection** | 3-state Gaussian HMM classifying market as trending / mean-reverting / choppy |
| **Ensemble Scoring** | Weighted combination of digit bias, chi-square significance, entropy, momentum, regime |
| **Kelly Criterion** | Adaptive stake sizing based on estimated edge (half-Kelly for safety) |
| **Martingale** | Controlled doubling with max-loss cap, max-stake cap, profit/loss stops |
| **Circuit Breaker** | Reduces stake when equity drops below its moving average |
| **Multi-Index Selector** | Ranks indices by signal strength, penalises correlated ones |
| **Time Filter** | Tracks win-rate by hour, auto-avoids weak periods |
| **Shadow Mode** | Paper-trades alternative parameters for A/B comparison |
| **Backtester** | Tick replay with Monte Carlo stress tests, Sharpe, drawdown, ruin probability |

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run backtest with synthetic data
python run_backtest.py --ticks 10000 --monte-carlo 500

# Run live bot (use demo account first!)
python bot.py --token YOUR_DERIV_TOKEN --account-mode demo --base-stake 1.0 --profit-target 50 --loss-limit -30 --max-contract-seconds 30 --save-app-json
```

## CLI Options (bot.py)

| Flag | Default | Description |
|---|---|---|
| `--token` | (required) | Deriv API/OAuth token |
| `--account-mode` | (required) | Expected account type for the supplied token (`demo` or `real`) |
| `--app-id` | 1089 | Deriv app ID |
| `--base-stake` | 1.0 | Base stake in USD |
| `--profit-target` | 100.0 | Stop after this profit |
| `--loss-limit` | -100.0 | Stop after this loss |
| `--multiplier` | 2.0 | Martingale multiplier |
| `--max-losses` | 4 | Max consecutive losses before reset |
| `--symbols` | R_10 R_25 ... | Symbols to trade |
| `--duration` | 5 | Contract duration (ticks) |
| `--max-contract-seconds` | none | Skip symbols whose expected contract time exceeds this limit |
| `--save-app-json` | none | Write session/trade JSON for frontend graphs (`app_data.json` if passed without a value) |
| `--disable-kelly` | false | Disable Kelly sizing and use base stake + Martingale only |
| `--require-known-regime` | false | Block entries until a symbol's regime is no longer `UNKNOWN` |
| `--score-threshold` | 0.60 | Min ensemble score to trade |
| `--debug` | false | Verbose logging |

## Risk Warning

**Trading synthetic indices is high risk.** Martingale strategies can rapidly deplete an account. This bot makes no guarantee of profit. Always test on a demo account first. Only trade with capital you can afford to lose. Past backtest results do not predict future performance.

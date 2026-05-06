# Crypto Trading Bot

Fee-aware cryptocurrency quantitative trading bot with support for grid trading, mean reversion, and trend following strategies.

## Features

- **Fee-aware execution**: All strategies account for trading fees and slippage
- **Multiple strategies**: Grid, Mean Reversion, Trend Following
- **Risk management**: Position sizing, circuit breakers, daily loss limits
- **Paper trading**: Test strategies without real money
- **Backtesting**: Validate strategies on historical data
- **Micro account support**: Optimized config for small capital (30 USD)

## Quick Start

### 1. Install Dependencies

```bash
pip install -e ".[dev]"
```

### 2. Configure API Keys

```bash
cp .env.example .env
# Edit .env with your exchange API keys
```

### 3. Choose Your Mode

**Paper Trading (Recommended for beginners):**
```bash
make paper
```

**Backtesting:**
```bash
make download    # Download historical data first
make backtest
```

**Live Trading:**
```bash
# Edit .env: TRADING_MODE=live
python -m src.main
```

## Account Size Configurations

### Standard Account (1000+ USD)

Uses `config/default.yaml`:
- Grid count: 20 levels
- Order amount: 100 USDT per level
- Max position: 10% of equity

### Micro Account (30 USD)

Uses `config/micro_30usd.yaml`:
- Grid count: 5 levels
- Order amount: 5 USDT per level
- Max position: 15% of equity (4.5 USD)
- Daily loss limit: 3% (0.9 USD)

Run with micro config:
```bash
TRADING_MODE=paper CONFIG_PATH=config/micro_30usd.yaml python -m src.main
```

## Project Structure

```
crypto-trading-bot/
├── config/                 # Configuration files
│   ├── default.yaml        # Standard config
│   ├── micro_30usd.yaml    # Micro account (30 USD)
│   ├── paper.yaml          # Paper trading overrides
│   └── backtest.yaml       # Backtest overrides
├── src/
│   ├── core/               # Engine, event bus, constants
│   ├── exchange/           # Exchange interfaces (CCXT, WebSocket)
│   ├── strategy/           # Trading strategies
│   ├── risk/               # Risk management
│   ├── portfolio/          # Position tracking
│   ├── backtest/           # Backtesting engine
│   ├── config/             # Config loader
│   ├── data/               # Data management
│   └── utils/              # Utilities
├── scripts/
│   ├── download_data.py    # Download historical data
│   └── run_backtest.py     # Run backtest
├── tests/                  # Test suite
└── data/
    ├── historical/         # OHLCV data
    └── logs/               # Trade journals
```

## Strategies

### Grid Trading
Places buy/sell orders at evenly spaced price levels. Best for ranging markets.

**Key parameters:**
- `grid_count`: Number of price levels
- `order_amount`: Quote currency per level
- `min_profit_buffer_pct`: Minimum profit after fees

### Mean Reversion
Buys oversold, sells overbought using Bollinger Bands + RSI.

**Key parameters:**
- `bb_period`, `bb_std`: Bollinger Band settings
- `rsi_oversold`, `rsi_overbought`: RSI thresholds

### Trend Following
MACD-based trend detection with ATR trailing stops.

**Key parameters:**
- `fast_ema`, `slow_ema`, `signal_ema`: MACD settings
- `atr_multiplier`: Trailing stop width

## Risk Management

| Parameter | Description | Default | Micro (30 USD) |
|-----------|-------------|---------|----------------|
| `max_position_pct` | Max % per position | 10% | 15% |
| `max_daily_loss_pct` | Daily loss limit | 5% | 3% |
| `max_drawdown_pct` | Circuit breaker | 20% | 15% |
| `cooldown_seconds` | Min time between trades | 60s | 120s |
| `max_concurrent_positions` | Max open positions | 5 | 2 |

## Commands

```bash
make help           # Show all commands
make install        # Install dependencies
make test           # Run tests
make test-cov       # Run tests with coverage
make download       # Download historical data
make backtest       # Run backtest
make paper          # Start paper trading
make lint           # Run type checking
make clean          # Remove generated files
```

## Backtesting

```bash
# Download 1 year of 5m data
python scripts/download_data.py --symbol BTC/USDT --timeframe 5m --days 365

# Run backtest with grid strategy
python scripts/run_backtest.py --symbol BTC/USDT --strategy grid --export

# Run backtest with micro account settings
python scripts/run_backtest.py --symbol BTC/USDT --strategy grid --config config/micro_30usd.yaml --export
```

## Important Notes

1. **Always test with paper trading first** before using real money
2. **Start with sandbox/testnet** mode enabled
3. **Micro accounts** have higher relative fees — use wider grid spacing
4. **Past performance** does not guarantee future results
5. **Never invest** more than you can afford to lose

## License

Private use only.

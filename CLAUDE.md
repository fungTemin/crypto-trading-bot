# Crypto Trading Bot

## Project Overview
Fee-aware cryptocurrency quantitative trading bot supporting grid, mean reversion, and trend following strategies.

## Key Commands
```bash
make install        # Install dependencies
make test           # Run tests
make paper          # Paper trading (standard)
make paper-micro    # Paper trading (30 USD micro account)
make backtest       # Run backtest
make download       # Download historical data
make lint           # Type checking
```

## Architecture
- `src/core/` - Engine, event bus
- `src/exchange/` - CCXT exchange interfaces
- `src/strategy/` - Trading strategies (grid, mean_reversion, trend_following)
- `src/risk/` - Risk management, circuit breaker
- `src/backtest/` - Backtesting engine

## Config Files
- `config/default.yaml` - Standard account (1000+ USD)
- `config/micro_30usd.yaml` - Micro account (30 USD)
- `config/paper.yaml` - Paper trading overrides
- `config/backtest.yaml` - Backtest overrides

## Development Notes
- All monetary values use `Decimal` for precision
- Fee gate validates strategy profitability before execution
- Circuit breaker halts trading on excessive drawdown
- Paper mode simulates fills without real orders

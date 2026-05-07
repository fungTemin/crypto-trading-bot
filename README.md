# Crypto Trading Bot

Fee-aware cryptocurrency quantitative trading bot. Every trade must cover transaction fees + slippage and still yield a net profit.

## Quick Start

```bash
source /Users/zhifeng.zhou/Documents/python/venv/bin/activate
make install    # install dependencies
make test       # run 45 tests
```

## Trading Modes

```bash
# Offline simulation — synthetic meme coin data, no API needed
python scripts/run_meme_bot.py --offline --capital 30

# Paper trading — real OKX prices, simulated fills (requires VPN/proxy in China)
python scripts/run_meme_bot.py --paper --proxy socks5://127.0.0.1:7890 --capital 30

# Futures paper trading — enables short-selling signals
python scripts/run_meme_bot.py --paper --futures --proxy socks5://127.0.0.1:7890 --capital 30

# Live trading — real OKX orders with API keys
python scripts/run_meme_bot.py --live --proxy socks5://127.0.0.1:7890 --capital 30
```

## Architecture

```
src/
├── main.py                     # Entry point
├── core/
│   ├── engine.py               # Trading engine orchestrator
│   ├── event_bus.py            # Async pub/sub event bus
│   └── constants.py            # Enums: OrderSide, SignalType, MarketType
├── exchange/
│   ├── base.py                 # Abstract ExchangeInterface
│   ├── ccxt_exchange.py        # Real exchange via ccxt
│   ├── paper_exchange.py       # Simulated fills + virtual balances
│   └── models.py               # Ticker, Order, Fill, Position
├── strategy/
│   ├── base.py                 # BaseStrategy with fee gate
│   ├── fee_calculator.py       # Pure fee math (core invariant)
│   ├── meme_scalper.py         # Meme coin momentum scalper (long + short)
│   ├── grid.py                 # Grid trading
│   ├── mean_reversion.py       # Bollinger Bands + RSI
│   └── trend_following.py      # MACD crossover + ATR trailing stop
├── risk/
│   ├── manager.py              # Signal validation pipeline
│   ├── position_sizer.py       # Fixed/Kelly/Volatility sizing
│   └── circuit_breaker.py      # Drawdown/daily loss limits
├── backtest/
│   ├── runner.py               # Backtest engine (reuses PaperExchange)
│   ├── data_loader.py          # Load historical OHLCV
│   └── reporter.py             # Performance metrics
├── portfolio/
│   └── tracker.py              # Real-time P&L tracking
├── data/
│   ├── indicator.py            # SMA, EMA, RSI, Bollinger, ATR, MACD
│   └── ohlcv_store.py          # Rolling OHLCV window
└── utils/
    ├── logger.py               # Structured JSON logger + trade journal
    └── retry.py                # Exponential backoff
```

## Core Design: Fee Gate

Every trading signal passes through a mandatory profitability check BEFORE reaching the exchange:

```
expected_return > entry_fee + exit_fee + slippage + min_profit_buffer

spot:    expected_return > 0.1% + 0.1% + spread + 0.05% = ~0.25%
futures: expected_return > 0.02% + 0.04% + spread + 0.05% = ~0.11%
```

Signals that fail this check are rejected and logged — no order is placed.

## Meme Scalper Strategy

Default strategy for small accounts ($30+). Trades 7 meme coins on OKX spot/futures.

### Entry Signals

| Condition | LONG | SHORT |
|-----------|------|-------|
| Volume spike | K-line vol > avg × 2.5 | Same |
| Trend | Price > EMA(5) | Price < EMA(5) |
| RSI | 35 ≤ RSI(8) ≤ 65 | 55 ≤ RSI(8) ≤ 65 |
| Fee gate | expected_return > fees + buffer | Same |

### Exit Rules

| Trigger | LONG | SHORT |
|---------|------|-------|
| Take profit | +2% | -2% |
| Stop loss | -2.5% | +2.5% |
| Trailing stop | -1% from peak | +1% from trough |
| Time stop | 45 min hold | 45 min hold |

### Supported Coins

PEPE, FLOKI, WIF, BONK, MEME, SHIB, DOGE (all against USDT)

## Risk Management

| Parameter | Value | Description |
|-----------|-------|-------------|
| Max position | 40% | Max $12 per trade on $30 account |
| Max concurrent | 3 | Max 3 coins held simultaneously |
| Daily loss limit | 15% | Stop trading if daily loss > 15% |
| Max drawdown | 25% | Circuit breaker RED state |
| Cooldown | 30s | Minimum seconds between same-symbol trades |

## Configuration

Layered config system (env vars override YAML):

```
config/default.yaml     ← base defaults
config/paper.yaml       ← paper trading overrides
config/meme.yaml        ← meme coin specific
.env                    ← API keys (gitignored)
```

`.env` template:

```
OKX_API_KEY=your_key
OKX_SECRET=your_secret
OKX_PASSWORD=your_password
TRADING_MODE=paper
```

## Backtest

```bash
# Download historical data
make download  # 365 days of BTC/USDT 5m candles

# Run backtest
make backtest  # grid strategy on BTC
```

## Test Results

```
45 passed | Fee-aware profitability: 3/3 closed trades net positive
```

### 2-Hour Paper Trading Test

| Mode | Trades | Profit Trades | Net P&L | Shorts |
|------|--------|--------------|---------|--------|
| SPOT | 4 (2 buy + 2 sell) | 2/2 (100%) | -$0.00 | N/A |
| FUTURES | 3 (2 buy + 1 sell) | 1/1 (100%) | -$0.08 | 0 |

All 3 closed trades were net profitable after fees. WIF position still open at test end.

## Project Status

| Phase | Feature | Status |
|-------|---------|--------|
| 1 | Project scaffold + config | Done |
| 2 | Exchange layer + fee calculator | Done |
| 3 | Trading strategies (4) | Done |
| 4 | Risk management | Done |
| 5 | Data layer + indicators | Done |
| 6 | Core engine + main | Done |
| 7 | Backtesting | Done |
| 8 | Tests + documentation | Done |
| 9 | OKX meme coin live test | Done |

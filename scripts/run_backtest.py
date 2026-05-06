#!/usr/bin/env python3
"""Run a backtest on historical data.

Usage:
    source /Users/zhifeng.zhou/Documents/python/venv/bin/activate
    python scripts/run_backtest.py --symbol BTC/USDT --strategy grid --days 180
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.constants import MarketType
from src.core.event_bus import EventBus
from src.backtest.data_loader import DataLoader
from src.backtest.runner import BacktestRunner
from src.backtest.reporter import print_summary, print_trade_log, export_csv
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.grid import GridStrategy
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.trend_following import TrendFollowingStrategy
from src.data.ohlcv_store import OHLCVStore
from src.data.indicator import compute_all


def create_strategy(name: str, market_type: MarketType, fee_calculator: FeeCalculator, event_bus: EventBus, **kwargs):
    """Factory for creating strategy instances."""
    if name == "grid":
        upper = Decimal(str(kwargs.get("upper_price", 70000)))
        lower = Decimal(str(kwargs.get("lower_price", 60000)))
        return GridStrategy(
            market_type=market_type,
            fee_calculator=fee_calculator,
            event_bus=event_bus,
            upper_price=upper,
            lower_price=lower,
            grid_count=kwargs.get("grid_count", 20),
            order_amount=Decimal(str(kwargs.get("order_amount", 100))),
        )

    if name == "mean_reversion":
        return MeanReversionStrategy(
            market_type=market_type,
            fee_calculator=fee_calculator,
            event_bus=event_bus,
            bb_period=kwargs.get("bb_period", 20),
            bb_std=kwargs.get("bb_std", 2.0),
            rsi_period=kwargs.get("rsi_period", 14),
            rsi_oversold=kwargs.get("rsi_oversold", 30),
            rsi_overbought=kwargs.get("rsi_overbought", 70),
        )

    if name == "trend_following":
        return TrendFollowingStrategy(
            market_type=market_type,
            fee_calculator=fee_calculator,
            event_bus=event_bus,
            fast_ema=kwargs.get("fast_ema", 12),
            slow_ema=kwargs.get("slow_ema", 26),
            signal_ema=kwargs.get("signal_ema", 9),
            atr_period=kwargs.get("atr_period", 14),
            atr_multiplier=kwargs.get("atr_multiplier", 2.0),
        )

    raise ValueError(f"Unknown strategy: {name}")


async def run(symbol: str, strategy_name: str, data_dir: str, **kwargs) -> None:
    """Run backtest."""
    # Load data
    loader = DataLoader(data_dir)
    timeframe = kwargs.get("timeframe", "5m")
    candles = loader.load(symbol=symbol, timeframe=timeframe)

    if not candles:
        print(f"No data found for {symbol} {timeframe} in {data_dir}")
        print("Run scripts/download_data.py first to download historical data.")
        return

    print(f"Loaded {len(candles)} candles for {symbol} {timeframe}")

    # Setup
    event_bus = EventBus()
    market_type = MarketType.SPOT
    fee_schedule = FeeSchedule.spot()
    fee_calculator = FeeCalculator(fee_schedule=fee_schedule, min_profit_buffer=Decimal("0.0005"))

    strategy = create_strategy(
        strategy_name, market_type, fee_calculator, event_bus,
        upper_price=Decimal(str(kwargs.get("upper_price", 70000))),
        lower_price=Decimal(str(kwargs.get("lower_price", 60000))),
        grid_count=20,
        order_amount=Decimal(str(kwargs.get("order_amount", 100))),
    )
    strategy.set_symbols([symbol])

    # Pre-compute indicators
    ohlcv_store = OHLCVStore(max_candles=200)
    for candle in candles:
        ohlcv_store.add(symbol, timeframe, candle)

    # Update strategy state with indicator values before each tick
    # (We'll do this inline in the loop)

    # Build a wrapper strategy that computes indicators then delegates
    class IndicatorAwareStrategy:
        def __init__(self, inner_strategy, ohlcv, symbol, timeframe):
            self.inner = inner_strategy
            self.ohlcv = ohlcv
            self.symbol = symbol
            self.timeframe = timeframe
            self.event_bus = event_bus

        @property
        def name(self):
            return self.inner.name

        @property
        def fee_calculator(self):
            return self.inner.fee_calculator

        @fee_calculator.setter
        def fee_calculator(self, val):
            self.inner.fee_calculator = val

        def set_symbols(self, symbols):
            self.inner.set_symbols(symbols)

        async def on_ticker(self, ticker):
            # Calculate indicators from data up to this ticker timestamp
            # Find candles up to this point
            from src.data.ohlcv_store import Candle
            candles_list = self.ohlcv.get(self.symbol, self.timeframe)
            relevant = [c for c in candles_list if c.timestamp <= ticker.timestamp]

            if len(relevant) >= 30:
                closes = [c.close for c in relevant]
                highs = [c.high for c in relevant]
                lows = [c.low for c in relevant]

                indicators = compute_all(
                    closes, highs, lows,
                    bb_period=20, bb_std=2.0, rsi_period=14,
                    atr_period=14, macd_fast=12, macd_slow=26, macd_signal=9,
                )

                # Update strategy-specific state
                if isinstance(self.inner, MeanReversionStrategy):
                    bb = indicators.get("bollinger")
                    rsi_val = indicators.get("rsi")
                    if bb and rsi_val is not None:
                        self.inner.set_state(
                            sma=bb.sma, upper_band=bb.upper,
                            lower_band=bb.lower, rsi=rsi_val,
                        )
                elif isinstance(self.inner, TrendFollowingStrategy):
                    macd_result = indicators.get("macd")
                    atr_val = indicators.get("atr")
                    if macd_result and atr_val is not None:
                        self.inner.set_state(
                            macd=macd_result.macd, macd_signal=macd_result.signal,
                            macd_histogram=macd_result.histogram, atr=atr_val,
                        )

            # Check position
            positions = await runner.exchange.fetch_positions()
            has_pos = any(p.symbol == self.symbol for p in positions)
            self.inner.set_position(has_pos)

            return await self.inner.on_ticker(ticker)

    runner = BacktestRunner(
        symbol=symbol,
        strategy=strategy,
        market_type=market_type,
        initial_capital=Decimal(str(kwargs.get("initial_capital", 10000))),
        fee_rate=Decimal(str(kwargs.get("fee_rate", 0.001))),
    )

    # Override strategy with indicator-aware wrapper
    wrapped = IndicatorAwareStrategy(strategy, ohlcv_store, symbol, timeframe)
    runner.strategy = wrapped

    # Run
    result = await runner.run(candles)

    # Report
    print_summary(result)
    print_trade_log(result, limit=15)

    if kwargs.get("export"):
        export_csv(result, f"data/logs/backtest_{symbol.replace('/', '_')}_{strategy_name}.csv")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run backtest on historical data")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--strategy", default="grid", choices=["grid", "mean_reversion", "trend_following"])
    parser.add_argument("--timeframe", default="5m", help="Candle timeframe")
    parser.add_argument("--data-dir", default="data/historical", help="Directory with historical data")
    parser.add_argument("--initial-capital", type=float, default=10000, help="Starting capital")
    parser.add_argument("--fee-rate", type=float, default=0.001, help="Fee rate per side")
    parser.add_argument("--upper-price", type=float, default=70000, help="Grid upper price")
    parser.add_argument("--lower-price", type=float, default=60000, help="Grid lower price")
    parser.add_argument("--order-amount", type=float, default=100, help="Order amount in quote currency")
    parser.add_argument("--export", action="store_true", help="Export results to CSV")

    args = parser.parse_args()

    asyncio.run(run(
        symbol=args.symbol,
        strategy_name=args.strategy,
        data_dir=args.data_dir,
        timeframe=args.timeframe,
        initial_capital=args.initial_capital,
        fee_rate=args.fee_rate,
        upper_price=args.upper_price,
        lower_price=args.lower_price,
        order_amount=args.order_amount,
        export=args.export,
    ))


if __name__ == "__main__":
    main()

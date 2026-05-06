"""Tests for backtest runner."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import pytest

from src.core.constants import MarketType
from src.core.event_bus import EventBus
from src.data.ohlcv_store import Candle
from src.backtest.runner import BacktestRunner
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.grid import GridStrategy


def make_candles(
    base_price: Decimal = Decimal("50000"),
    count: int = 200,
    volatility: Decimal = Decimal("0.001"),  # 0.1% per candle
) -> list[Candle]:
    """Generate synthetic candles with small random walk."""
    import random
    random.seed(42)

    candles = []
    price = base_price
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)

    for i in range(count):
        change = price * volatility * Decimal(str(random.gauss(0, 1)))
        open_price = price
        close_price = price + change

        high = max(open_price, close_price) * (Decimal("1") + volatility * Decimal(str(random.random())))
        low = min(open_price, close_price) * (Decimal("1") - volatility * Decimal(str(random.random())))

        volume = Decimal(str(random.uniform(10, 100)))

        candles.append(Candle(
            timestamp=start + timedelta(minutes=i * 5),
            open_=open_price,
            high=high,
            low=low,
            close=close_price,
            volume=volume,
        ))

        price = close_price

    return candles


class TestBacktestRunner:
    @pytest.mark.asyncio
    async def test_runs_without_errors(self) -> None:
        candles = make_candles()

        event_bus = EventBus()
        fee_calculator = FeeCalculator(FeeSchedule.spot())

        strategy = GridStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=fee_calculator,
            event_bus=event_bus,
            upper_price=Decimal("55000"),
            lower_price=Decimal("45000"),
            grid_count=20,
            order_amount=Decimal("500"),
        )
        strategy.set_symbols(["BTC/USDT"])

        runner = BacktestRunner(
            symbol="BTC/USDT",
            strategy=strategy,
            market_type=MarketType.SPOT,
            initial_capital=Decimal("10000"),
            fee_rate=Decimal("0.001"),
        )

        result = await runner.run(candles)

        assert result.initial_equity == Decimal("10000")
        assert result.final_equity > 0
        assert result.total_trades >= 0

    def test_rejects_grid_when_spacing_too_tight_for_fees(self) -> None:
        """Verify grid strategy rejects initialization when spacing < fees."""
        event_bus = EventBus()
        fee_calculator = FeeCalculator(FeeSchedule.spot())

        with pytest.raises(ValueError, match="too tight"):
            GridStrategy(
                market_type=MarketType.SPOT,
                fee_calculator=fee_calculator,
                event_bus=event_bus,
                upper_price=Decimal("50200"),
                lower_price=Decimal("50000"),
                grid_count=20,
                order_amount=Decimal("100"),
            )

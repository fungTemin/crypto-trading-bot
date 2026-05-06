"""Tests for trading strategies."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.constants import MarketType
from src.core.event_bus import EventBus
from src.exchange.models import Ticker
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.grid import GridStrategy
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.trend_following import TrendFollowingStrategy


def make_ticker(price: Decimal, spread_pct: float = 0.001) -> Ticker:
    half_spread = price * Decimal(str(spread_pct)) / 2
    return Ticker(
        symbol="BTC/USDT",
        bid=price - half_spread,
        ask=price + half_spread,
        last=price,
        high=price * Decimal("1.01"),
        low=price * Decimal("0.99"),
        volume=Decimal("100000"),
    )


def make_fee_calculator() -> FeeCalculator:
    return FeeCalculator(
        fee_schedule=FeeSchedule.spot(),
        min_profit_buffer=Decimal("0.0005"),
    )


class TestGridStrategy:
    def test_rejects_too_tight_grid(self) -> None:
        calc = make_fee_calculator()
        # Grid spacing 0.01% is too tight (fees ~0.25%)
        with pytest.raises(ValueError, match="too tight"):
            GridStrategy(
                market_type=MarketType.SPOT,
                fee_calculator=calc,
                event_bus=EventBus(),
                upper_price=Decimal("50100"),
                lower_price=Decimal("50000"),
                grid_count=100,  # ~0.02% spacing
                order_amount=Decimal("100"),
            )

    def test_accepts_valid_grid(self) -> None:
        calc = make_fee_calculator()
        strategy = GridStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=calc,
            event_bus=EventBus(),
            upper_price=Decimal("55000"),
            lower_price=Decimal("50000"),
            grid_count=10,  # ~1% spacing
            order_amount=Decimal("100"),
        )
        assert strategy.grid_count == 10


class TestMeanReversionStrategy:
    def test_no_signal_without_indicators(self) -> None:
        calc = make_fee_calculator()
        strategy = MeanReversionStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=calc,
            event_bus=EventBus(),
        )

        ticker = make_ticker(Decimal("50000"))
        import asyncio
        signal = asyncio.run(strategy.compute_signal(ticker))
        assert signal is None

    def test_buy_signal_when_oversold(self) -> None:
        calc = make_fee_calculator()
        strategy = MeanReversionStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=calc,
            event_bus=EventBus(),
        )
        strategy.set_state(
            sma=Decimal("50000"),
            upper_band=Decimal("51000"),
            lower_band=Decimal("49000"),
            rsi=Decimal("25"),
        )

        ticker = make_ticker(Decimal("48900"))  # below lower band
        import asyncio
        signal = asyncio.run(strategy.compute_signal(ticker))
        assert signal is not None
        assert signal.signal_type.value == "buy_long"

    def test_no_buy_when_not_oversold(self) -> None:
        calc = make_fee_calculator()
        strategy = MeanReversionStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=calc,
            event_bus=EventBus(),
        )
        strategy.set_state(
            sma=Decimal("50000"),
            upper_band=Decimal("51000"),
            lower_band=Decimal("49000"),
            rsi=Decimal("50"),  # not oversold
        )

        ticker = make_ticker(Decimal("48900"))
        import asyncio
        signal = asyncio.run(strategy.compute_signal(ticker))
        assert signal is None


class TestTrendFollowingStrategy:
    def test_no_signal_without_indicators(self) -> None:
        calc = make_fee_calculator()
        strategy = TrendFollowingStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=calc,
            event_bus=EventBus(),
        )
        ticker = make_ticker(Decimal("50000"))
        import asyncio
        signal = asyncio.run(strategy.compute_signal(ticker))
        assert signal is None

    def test_buy_signal_on_bullish_crossover(self) -> None:
        calc = make_fee_calculator()
        strategy = TrendFollowingStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=calc,
            event_bus=EventBus(),
        )
        strategy.set_state(
            macd=Decimal("100"),
            macd_signal=Decimal("50"),
            macd_histogram=Decimal("50"),  # positive = bullish
            atr=Decimal("200"),
        )
        strategy._prev_histogram = Decimal("-10")  # was negative, now positive = crossover

        ticker = make_ticker(Decimal("50000"))
        import asyncio
        signal = asyncio.run(strategy.compute_signal(ticker))
        assert signal is not None
        assert signal.signal_type.value == "buy_long"

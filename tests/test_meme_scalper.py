"""Tests for meme coin scalping strategy."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.constants import MarketType, OrderSide, SignalType
from src.core.event_bus import EventBus
from src.exchange.models import Ticker
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.meme_scalper import MemeScalperStrategy


def make_ticker(symbol: str, price: Decimal, volume: Decimal) -> Ticker:
    spread = price * Decimal("0.0001")
    return Ticker(
        symbol=symbol,
        bid=price - spread,
        ask=price + spread,
        last=price,
        high=price * Decimal("1.005"),
        low=price * Decimal("0.995"),
        volume=volume,
    )


class TestMemeScalperStrategy:
    def setup_method(self) -> None:
        self.fee_calc = FeeCalculator(
            fee_schedule=FeeSchedule.spot(),
            min_profit_buffer=Decimal("0.0005"),
        )
        self.event_bus = EventBus()
        self.strategy = MemeScalperStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=self.fee_calc,
            event_bus=self.event_bus,
        )
        self.strategy.set_symbols(["PEPE/USDT", "WIF/USDT"])

    @pytest.mark.asyncio
    async def test_no_signal_without_volume_history(self) -> None:
        ticker = make_ticker("PEPE/USDT", Decimal("0.00001000"), Decimal("500000"))
        signal = await self.strategy.compute_signal(ticker)
        assert signal is None  # Not enough history

    @pytest.mark.asyncio
    async def test_no_signal_below_volume_threshold(self) -> None:
        # Fill price/volume history with quiet data
        for _ in range(30):
            self.strategy.update_state(
                "PEPE/USDT", Decimal("0.00001000"), Decimal("100000")
            )

        ticker = make_ticker("PEPE/USDT", Decimal("0.00001000"), Decimal("100000"))
        signal = await self.strategy.compute_signal(ticker)
        assert signal is None  # Volume too low (< $500k)

    @pytest.mark.asyncio
    async def test_no_signal_when_rsi_overbought(self) -> None:
        # Fill with rising prices to create high RSI
        base = Decimal("0.00001000")
        for i in range(30):
            price = base * (Decimal("1") + Decimal(str(i)) * Decimal("0.001"))
            self.strategy.update_state("PEPE/USDT", price, Decimal("2000000"))

        # Now volume spike at overbought
        ticker = make_ticker(
            "PEPE/USDT", Decimal("0.00001300"), Decimal("5000000")  # 5M volume = spike
        )
        signal = await self.strategy.compute_signal(ticker)
        assert signal is None  # RSI should be overbought

    @pytest.mark.asyncio
    async def test_take_profit_exit(self) -> None:
        """Verify take profit triggers exit when price rises enough."""
        # Set up entry
        self.strategy.update_state("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))
        # Fill enough history
        for _ in range(30):
            self.strategy.update_state("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))

        # Record entry
        self.strategy.record_entry("WIF/USDT", Decimal("0.50000000"))

        # Price goes up 3% (above 2% take profit)
        ticker = make_ticker("WIF/USDT", Decimal("0.51500000"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(ticker)

        assert signal is not None
        assert signal.signal_type == SignalType.SELL_LONG
        assert signal.metadata["exit_reason"] == "take_profit"

    @pytest.mark.asyncio
    async def test_stop_loss_exit(self) -> None:
        """Verify stop loss triggers exit when price drops enough."""
        self.strategy.update_state("PEPE/USDT", Decimal("0.00001000"), Decimal("2000000"))
        for _ in range(30):
            self.strategy.update_state("PEPE/USDT", Decimal("0.00001000"), Decimal("2000000"))

        self.strategy.record_entry("PEPE/USDT", Decimal("0.00001000"))

        # Price drops 3% (below 2.5% stop loss)
        ticker = make_ticker("PEPE/USDT", Decimal("0.00000970"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(ticker)

        assert signal is not None
        assert signal.signal_type == SignalType.SELL_LONG
        assert signal.metadata["exit_reason"] == "stop_loss"

    @pytest.mark.asyncio
    async def test_trailing_stop_exit(self) -> None:
        """Verify trailing stop triggers when price retraces."""
        self.strategy.update_state("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))
        for _ in range(30):
            self.strategy.update_state("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))

        self.strategy.record_entry("WIF/USDT", Decimal("0.50000000"))

        # Price goes up 2.5% (sets highest)
        up_ticker = make_ticker("WIF/USDT", Decimal("0.51250000"), Decimal("2000000"))
        await self.strategy.compute_signal(up_ticker)  # updates highest_price

        # Price drops 1.5% from highest (0.5125 * 0.985 = 0.5048)
        down_ticker = make_ticker("WIF/USDT", Decimal("0.50400000"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(down_ticker)

        assert signal is not None
        assert signal.signal_type == SignalType.SELL_LONG
        assert signal.metadata["exit_reason"] == "trailing_stop"

    @pytest.mark.asyncio
    async def test_record_entry_and_exit(self) -> None:
        """Test internal position tracking."""
        assert not self.strategy.is_in_position("PEPE/USDT")

        self.strategy.record_entry("PEPE/USDT", Decimal("0.00001000"))
        assert self.strategy.is_in_position("PEPE/USDT")

        self.strategy.record_exit("PEPE/USDT")
        assert not self.strategy.is_in_position("PEPE/USDT")

    def test_volume_spike_detection(self) -> None:
        """Test volume spike calculation."""
        # Fill with steady volume
        for _ in range(20):
            self.strategy.update_state("PEPE/USDT", Decimal("0.00001000"), Decimal("1000000"))

        assert len(self.strategy._coins["PEPE/USDT"].volume_history) == 20
        avg = self.strategy._coins["PEPE/USDT"].avg_volume
        assert avg == Decimal("1000000")

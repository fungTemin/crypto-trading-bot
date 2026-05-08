"""Tests for meme coin scalping strategy — long, short, kline volume."""

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
        symbol=symbol, bid=price - spread, ask=price + spread, last=price,
        high=price * Decimal("1.005"), low=price * Decimal("0.995"), volume=volume,
    )


def make_fee_calc() -> FeeCalculator:
    return FeeCalculator(fee_schedule=FeeSchedule.spot(), min_profit_buffer=Decimal("0.0005"))


def build_strategy(market_type=MarketType.SPOT, **kwargs) -> MemeScalperStrategy:
    return MemeScalperStrategy(
        market_type=market_type, fee_calculator=make_fee_calc(),
        event_bus=EventBus(), **kwargs,
    )


class TestKlineVolumeDetection:
    """Verify kline-based volume spike detection triggers signals."""

    def setup_method(self) -> None:
        # Relax RSI thresholds for testing — accept any RSI
        self.strategy = build_strategy(
            volume_spike_ratio=Decimal("2.0"),
            rsi_long_min=0,
            rsi_long_max=100,
        )
        self.strategy.set_symbols(["PEPE/USDT"])

    @pytest.mark.asyncio
    async def test_no_signal_without_kline_data(self) -> None:
        # No kline data fed — no signal
        for _ in range(20):
            self.strategy.update_ticker_data("PEPE/USDT", Decimal("0.00001"), Decimal("1000000"))
        ticker = make_ticker("PEPE/USDT", Decimal("0.00001"), Decimal("1000000"))
        signal = await self.strategy.compute_signal(ticker)
        assert signal is None

    @pytest.mark.asyncio
    async def test_buy_signal_on_kline_volume_spike(self) -> None:
        # Feed kline history: average ~500k, then a big spike
        for _ in range(10):
            self.strategy.update_kline_volume("PEPE/USDT", Decimal("500000"))
        self.strategy.update_kline_volume("PEPE/USDT", Decimal("5000000"))  # 10x spike

        # Feed price history — uptrend
        base = Decimal("0.00001000")
        for i in range(30):
            self.strategy.update_ticker_data(
                "PEPE/USDT",
                base * (Decimal("1") + Decimal(str(i)) * Decimal("0.0005")),
                Decimal("1000000"),
            )

        ticker = make_ticker("PEPE/USDT", base * Decimal("1.015"), Decimal("1000000"))
        signal = await self.strategy.compute_signal(ticker)
        assert signal is not None, "Should trigger BUY on volume spike + uptrend"
        assert signal.signal_type == SignalType.BUY_LONG
        assert signal.order_side == OrderSide.BUY

    @pytest.mark.asyncio
    async def test_no_signal_when_volume_below_threshold(self) -> None:
        # Feed klines with steady volume — no spike
        for _ in range(15):
            self.strategy.update_kline_volume("PEPE/USDT", Decimal("500000"))
            self.strategy.update_ticker_data("PEPE/USDT", Decimal("0.00001"), Decimal("1000000"))

        ticker = make_ticker("PEPE/USDT", Decimal("0.00001005"), Decimal("1000000"))
        signal = await self.strategy.compute_signal(ticker)
        assert signal is None  # no volume spike


class TestShortSelling:
    """Verify short-selling entry and exit logic."""

    def setup_method(self) -> None:
        # Use FUTURE market type to enable shorts, accept any RSI for test
        self.strategy = build_strategy(
            market_type=MarketType.FUTURE,
            volume_spike_ratio=Decimal("2.0"),
            rsi_long_min=0, rsi_long_max=100,
            rsi_short_min=0, rsi_short_max=100,
        )
        self.strategy.set_symbols(["WIF/USDT"])

    def _feed_downtrend_with_spike(self) -> None:
        """Feed kline volume spike + downtrend price data for short setup."""
        for _ in range(10):
            self.strategy.update_kline_volume("WIF/USDT", Decimal("500000"))
        self.strategy.update_kline_volume("WIF/USDT", Decimal("5000000"))  # 10x spike

        # Steady downtrend
        base = Decimal("0.50000000")
        for i in range(30):
            self.strategy.update_ticker_data(
                "WIF/USDT",
                base * (Decimal("1") - Decimal(str(i)) * Decimal("0.001")),
                Decimal("2000000"),
            )

    @pytest.mark.asyncio
    async def test_short_entry_on_downtrend_volume_spike(self) -> None:
        self._feed_downtrend_with_spike()
        # Use a price that continues the downtrend (below EMA)
        state = self.strategy._coins["WIF/USDT"]
        last_p = state.last_price
        ticker = make_ticker("WIF/USDT", last_p, Decimal("2000000"))
        signal = await self.strategy.compute_signal(ticker)

        assert signal is not None, f"Should trigger SHORT. EMA={state.cached_ema} price={last_p}"
        assert signal.signal_type == SignalType.SELL_SHORT
        assert signal.order_side == OrderSide.SELL

    @pytest.mark.asyncio
    async def test_no_short_in_spot_mode(self) -> None:
        """Short signals are disabled for spot market."""
        spot_strategy = build_strategy(market_type=MarketType.SPOT,
                                       volume_spike_ratio=Decimal("2.0"))
        spot_strategy.set_symbols(["WIF/USDT"])

        self.strategy = spot_strategy
        self._feed_downtrend_with_spike()

        ticker = make_ticker("WIF/USDT", Decimal("0.49200000"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(ticker)
        # Should be None because spot mode skips shorts
        assert signal is None or signal.signal_type != SignalType.SELL_SHORT

    @pytest.mark.asyncio
    async def test_short_trailing_stop_exit(self) -> None:
        """Short trailing stop: price drops then rebounds 0.8%."""
        self.strategy.update_ticker_data("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))
        for _ in range(30):
            self.strategy.update_ticker_data("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))

        self.strategy.record_entry("WIF/USDT", Decimal("0.50000000"),
                                   amount=Decimal("50"), side="short")
        # First push price down to set lowest_price (0.495)
        down_ticker = make_ticker("WIF/USDT", Decimal("0.49500000"), Decimal("2000000"))
        await self.strategy.compute_signal(down_ticker)
        # Then price rebounds: 0.495 * 1.008 = 0.49896
        # 0.499 > 0.49896 → triggers trailing_stop
        up_ticker = make_ticker("WIF/USDT", Decimal("0.49900000"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(up_ticker)

        assert signal is not None
        assert signal.signal_type == SignalType.BUY_SHORT
        assert signal.metadata["exit_reason"] == "trailing_stop"

    @pytest.mark.asyncio
    async def test_short_stop_loss_exit(self) -> None:
        """Short exit: price rises to stop-loss level."""
        self.strategy.update_ticker_data("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))
        for _ in range(30):
            self.strategy.update_ticker_data("WIF/USDT", Decimal("0.50000000"), Decimal("2000000"))

        self.strategy.record_entry("WIF/USDT", Decimal("0.50000000"),
                                   amount=Decimal("50"), side="short")
        # SL for short: entry * (1 + 2.5%) = 0.5125
        ticker = make_ticker("WIF/USDT", Decimal("0.51300000"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(ticker)

        assert signal is not None
        assert signal.signal_type == SignalType.BUY_SHORT
        assert signal.metadata["exit_reason"] == "stop_loss"


class TestLongExit:
    def setup_method(self) -> None:
        self.strategy = build_strategy()
        self.strategy.set_symbols(["PEPE/USDT"])

    @pytest.mark.asyncio
    async def test_trailing_stop_primary_exit(self) -> None:
        """Trailing stop is now primary exit: price rises then retraces 0.8%."""
        for _ in range(30):
            self.strategy.update_ticker_data("PEPE/USDT", Decimal("0.00001000"), Decimal("2000000"))
        self.strategy.record_entry("PEPE/USDT", Decimal("0.00001000"),
                                   amount=Decimal("1000000"), side="long")
        # First push price up to set highest_price (0.00001050)
        up_ticker = make_ticker("PEPE/USDT", Decimal("0.00001050"), Decimal("2000000"))
        await self.strategy.compute_signal(up_ticker)
        # Then drop below trailing stop: 0.00001050 * 0.992 = 0.000010416
        # 0.00001040 < 0.000010416 → triggers trailing_stop
        down_ticker = make_ticker("PEPE/USDT", Decimal("0.00001040"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(down_ticker)
        assert signal is not None, "Should trigger trailing_stop on retracement"
        assert signal.signal_type == SignalType.SELL_LONG
        assert signal.metadata["exit_reason"] == "trailing_stop"
        assert signal.amount > 0

    @pytest.mark.asyncio
    async def test_stop_loss_exit(self) -> None:
        for _ in range(30):
            self.strategy.update_ticker_data("PEPE/USDT", Decimal("0.00001000"), Decimal("2000000"))
        self.strategy.record_entry("PEPE/USDT", Decimal("0.00001000"),
                                   amount=Decimal("1000000"), side="long")
        # SL: entry * 0.975 = 0.00000975
        ticker = make_ticker("PEPE/USDT", Decimal("0.00000970"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(ticker)
        assert signal is not None
        assert signal.metadata["exit_reason"] == "stop_loss"

    @pytest.mark.asyncio
    async def test_trailing_stop_retrace_exit(self) -> None:
        for _ in range(30):
            self.strategy.update_ticker_data("PEPE/USDT", Decimal("0.00001000"), Decimal("2000000"))
        self.strategy.record_entry("PEPE/USDT", Decimal("0.00001000"),
                                   amount=Decimal("1000000"), side="long")
        # Price rises to 0.00001050 (+5%), sets highest_price
        up_ticker = make_ticker("PEPE/USDT", Decimal("0.00001050"), Decimal("2000000"))
        signal1 = await self.strategy.compute_signal(up_ticker)
        assert signal1 is None, "Should not trigger while price is rising"

        # Price drops: trail = 0.00001050 * (1-0.008) = 0.000010416
        # 0.00001040 < 0.000010416 → triggers trailing_stop (0.8%)
        down_ticker = make_ticker("PEPE/USDT", Decimal("0.00001040"), Decimal("2000000"))
        signal = await self.strategy.compute_signal(down_ticker)
        assert signal is not None
        assert signal.metadata["exit_reason"] == "trailing_stop"

    @pytest.mark.asyncio
    async def test_no_signal_when_rsi_overbought(self) -> None:
        """Long signal blocked when RSI is too high (above rsi_long_max=65)."""
        for _ in range(10):
            self.strategy.update_kline_volume("PEPE/USDT", Decimal("500000"))
        self.strategy.update_kline_volume("PEPE/USDT", Decimal("2000000"))  # spike

        # Default rsi_long_max=55 — feed rapidly rising prices to create high RSI
        strat = build_strategy()  # uses defaults: rsi_long_min=35, rsi_long_max=55
        strat.set_symbols(["PEPE/USDT"])
        for _ in range(10):
            strat.update_kline_volume("PEPE/USDT", Decimal("500000"))
        strat.update_kline_volume("PEPE/USDT", Decimal("2000000"))  # spike

        base = Decimal("0.00001000")
        for i in range(30):
            strat.update_ticker_data("PEPE/USDT", base * (Decimal("1") + Decimal(str(i)) * Decimal("0.003")), Decimal("1000000"))

        ticker = make_ticker("PEPE/USDT", base * Decimal("1.09"), Decimal("1000000"))
        signal = await strat.compute_signal(ticker)
        assert signal is None  # RSI should be overbought (>55)

    def test_record_entry_and_exit(self) -> None:
        assert not self.strategy.is_in_position("PEPE/USDT")
        self.strategy.record_entry("PEPE/USDT", Decimal("0.00001000"),
                                   amount=Decimal("1000000"), side="long")
        assert self.strategy.is_in_position("PEPE/USDT")
        assert self.strategy.get_position_side("PEPE/USDT") == "long"
        self.strategy.record_exit("PEPE/USDT")
        assert not self.strategy.is_in_position("PEPE/USDT")

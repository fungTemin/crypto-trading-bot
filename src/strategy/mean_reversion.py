"""Mean reversion strategy using Bollinger Bands + RSI.

Fee gate: (z_score_deviation * volatility) > round_trip_cost + buffer.
The price deviation from mean must be wide enough that reverting to
the mean covers all fees and yields profit.

Signals:
    - BUY LONG:  price < lower_band AND RSI < oversold
    - SELL LONG: price > middle_band AND RSI > 50
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.core.constants import MarketType, OrderSide, SignalType
from src.exchange.models import Ticker
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator


class MeanReversionStrategy(BaseStrategy):
    """Mean reversion with Bollinger Bands and RSI confirmation."""

    def __init__(
        self,
        market_type: MarketType,
        fee_calculator: FeeCalculator,
        event_bus,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        rsi_oversold: int = 30,
        rsi_overbought: int = 70,
        confirmation_candles: int = 2,
    ) -> None:
        super().__init__("mean_reversion", market_type, fee_calculator, event_bus)
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.confirmation_candles = confirmation_candles

        # State: stored externally via set_state()
        self._sma: Optional[Decimal] = None
        self._upper_band: Optional[Decimal] = None
        self._lower_band: Optional[Decimal] = None
        self._rsi: Optional[Decimal] = None
        self._in_position: bool = False
        self._entry_price: Optional[Decimal] = None

    def set_state(
        self,
        sma: Decimal,
        upper_band: Decimal,
        lower_band: Decimal,
        rsi: Decimal,
    ) -> None:
        """Update indicator state from external indicator calculator."""
        self._sma = sma
        self._upper_band = upper_band
        self._lower_band = lower_band
        self._rsi = rsi

    def set_position(self, in_position: bool, entry_price: Decimal | None = None) -> None:
        self._in_position = in_position
        self._entry_price = entry_price

    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        if self._sma is None or self._lower_band is None or self._upper_band is None or self._rsi is None:
            return None

        price = ticker.last
        volatility = (self._upper_band - self._lower_band) / self._sma

        if not self._in_position:
            # BUY signal: price below lower band AND RSI oversold
            if price <= self._lower_band and self._rsi <= self.rsi_oversold:
                # Expected return = distance from lower band to SMA (mean)
                deviation_from_mean = (self._sma - price) / price
                expected_return = deviation_from_mean

                amount = self._calculate_position_size(ticker)

                return Signal(
                    symbol=ticker.symbol,
                    signal_type=SignalType.BUY_LONG,
                    order_side=OrderSide.BUY,
                    order_type="limit",
                    price=price,
                    amount=amount,
                    expected_return_rate=expected_return,
                    metadata={
                        "sma": str(self._sma),
                        "lower_band": str(self._lower_band),
                        "rsi": str(self._rsi),
                        "volatility": str(volatility),
                    },
                )
        else:
            # SELL signal: price reverts to mean or crosses upper band
            if price >= self._sma or (price >= self._upper_band and self._rsi >= self.rsi_overbought):
                if self._entry_price:
                    expected_return = (price - self._entry_price) / self._entry_price
                else:
                    expected_return = (price - self._sma) / self._sma

                return Signal(
                    symbol=ticker.symbol,
                    signal_type=SignalType.SELL_LONG,
                    order_side=OrderSide.SELL,
                    order_type="limit",
                    price=price,
                    amount=Decimal("0"),  # full position close
                    expected_return_rate=expected_return,
                    metadata={
                        "entry_price": str(self._entry_price or self._sma),
                        "current_price": str(price),
                    },
                )

        return None

    def _calculate_position_size(self, ticker: Ticker) -> Decimal:
        """Default position sizing — override via risk manager."""
        return Decimal("0.01")  # 0.01 BTC equivalent

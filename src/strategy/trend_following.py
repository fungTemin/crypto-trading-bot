"""Trend following strategy using MACD + ATR trailing stop.

Fee gate: (trend_strength * ATR / price) > round_trip_cost + buffer.
The trend must be strong enough that the expected move to take-profit
covers fees and yields profit.

Signals:
    - BUY LONG:  MACD line crosses above signal line (bullish crossover)
    - SELL LONG: MACD line crosses below signal line, OR trailing stop hit
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.core.constants import MarketType, OrderSide, SignalType
from src.exchange.models import Ticker
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator


class TrendFollowingStrategy(BaseStrategy):
    """MACD crossover with ATR-based trailing stop."""

    def __init__(
        self,
        market_type: MarketType,
        fee_calculator: FeeCalculator,
        event_bus,
        fast_ema: int = 12,
        slow_ema: int = 26,
        signal_ema: int = 9,
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
    ) -> None:
        super().__init__("trend_following", market_type, fee_calculator, event_bus)
        self.fast_ema = fast_ema
        self.slow_ema = slow_ema
        self.signal_ema = signal_ema
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

        # State
        self._macd: Optional[Decimal] = None
        self._macd_signal: Optional[Decimal] = None
        self._macd_histogram: Optional[Decimal] = None
        self._atr: Optional[Decimal] = None
        self._in_position: bool = False
        self._entry_price: Optional[Decimal] = None
        self._highest_price: Optional[Decimal] = None  # trailing stop tracking

    def set_state(
        self,
        macd: Decimal,
        macd_signal: Decimal,
        macd_histogram: Decimal,
        atr: Decimal,
    ) -> None:
        """Update indicator state."""
        prev_histogram = self._macd_histogram
        self._macd = macd
        self._macd_signal = macd_signal
        self._macd_histogram = macd_histogram
        self._atr = atr

        # Store previous for crossover detection
        self._prev_histogram = prev_histogram

    _prev_histogram: Optional[Decimal] = None

    def set_position(self, in_position: bool, entry_price: Decimal | None = None) -> None:
        self._in_position = in_position
        self._entry_price = entry_price
        if entry_price:
            self._highest_price = entry_price

    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        if self._atr is None or self._macd is None or self._macd_signal is None:
            return None

        price = ticker.last

        # Track highest price for trailing stop
        if self._in_position and self._highest_price is not None:
            if price > self._highest_price:
                self._highest_price = price

        if not self._in_position:
            # BUY signal: MACD crosses above signal line (bullish)
            is_bullish_crossover = (
                self._prev_histogram is not None
                and self._prev_histogram <= 0
                and self._macd_histogram > 0
            )

            if is_bullish_crossover:
                # Trend strength = MACD histogram magnitude relative to price
                trend_strength = abs(self._macd_histogram) / price
                atr_pct = self._atr / price

                # Expected return = ATR * multiplier (our take-profit target)
                expected_return = atr_pct * Decimal(str(self.atr_multiplier))

                amount = self._calculate_position_size(ticker)

                return Signal(
                    symbol=ticker.symbol,
                    signal_type=SignalType.BUY_LONG,
                    order_side=OrderSide.BUY,
                    order_type="market",
                    price=price,
                    amount=amount,
                    expected_return_rate=expected_return,
                    metadata={
                        "macd": str(self._macd),
                        "signal": str(self._macd_signal),
                        "histogram": str(self._macd_histogram),
                        "atr": str(self._atr),
                        "atr_pct": str(atr_pct),
                        "trend_strength": str(trend_strength),
                    },
                )

        else:
            # SELL signals: MACD bearish crossover OR trailing stop hit
            is_bearish_crossover = (
                self._prev_histogram is not None
                and self._prev_histogram >= 0
                and self._macd_histogram < 0
            )

            trailing_stop = None
            if self._highest_price is not None and self._atr is not None:
                trailing_stop = self._highest_price - (self._atr * Decimal(str(self.atr_multiplier)))

            stop_hit = trailing_stop is not None and price <= trailing_stop

            if is_bearish_crossover or stop_hit:
                if self._entry_price:
                    expected_return = (price - self._entry_price) / self._entry_price
                else:
                    expected_return = Decimal("0")

                exit_reason = "trailing_stop" if stop_hit else "bearish_crossover"

                return Signal(
                    symbol=ticker.symbol,
                    signal_type=SignalType.SELL_LONG,
                    order_side=OrderSide.SELL,
                    order_type="market",
                    price=price,
                    amount=Decimal("0"),  # full close
                    expected_return_rate=expected_return,
                    metadata={
                        "entry_price": str(self._entry_price or price),
                        "exit_reason": exit_reason,
                        "trailing_stop": str(trailing_stop) if trailing_stop else "N/A",
                    },
                )

        return None

    def _calculate_position_size(self, ticker: Ticker) -> Decimal:
        return Decimal("0.01")

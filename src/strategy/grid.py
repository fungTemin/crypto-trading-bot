"""Grid trading strategy.

Places buy/sell orders at evenly spaced price levels within a range.
Fee gate: grid spacing MUST exceed round-trip fees + profit buffer.
If spacing is too tight, the strategy refuses to initialize.

Formula:
    grid_spacing_pct > entry_fee + exit_fee + slippage + min_profit_buffer
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from src.core.constants import MarketType, OrderSide, SignalType
from src.exchange.models import Ticker
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class GridStrategy(BaseStrategy):
    """Simple grid trading: buys below mid, sells above mid.

    The grid is initialized with a price range and number of levels.
    Each level has a fixed quote amount.
    """

    def __init__(
        self,
        market_type: MarketType,
        fee_calculator: FeeCalculator,
        event_bus,
        upper_price: Decimal,
        lower_price: Decimal,
        grid_count: int,
        order_amount: Decimal,
    ) -> None:
        super().__init__("grid", market_type, fee_calculator, event_bus)
        self.upper_price = upper_price
        self.lower_price = lower_price
        self.grid_count = grid_count
        self.order_amount = order_amount

        # Validate grid spacing against fees
        spacing = (upper_price - lower_price) / grid_count
        mid = (upper_price + lower_price) / 2
        grid_spacing_pct = spacing / mid

        # Min required: entry fee + exit fee + buffer (no slippage for grid limits)
        min_required = fee_calculator._fee_schedule.taker + fee_calculator._fee_schedule.taker
        min_required += fee_calculator.min_profit_buffer

        if grid_spacing_pct <= min_required:
            raise ValueError(
                f"Grid spacing ({grid_spacing_pct * 100:.4f}%) is too tight. "
                f"Minimum required: {min_required * 100:.4f}% (fees + buffer). "
                f"Increase the range or decrease grid_count."
            )

        self._grid_levels: list[Decimal] = []
        self._filled_levels: set[int] = set()  # levels with open buy orders
        self._init_grid()

        logger.info(
            "Grid strategy initialized",
            extra={
                "upper": str(upper_price),
                "lower": str(lower_price),
                "grid_count": grid_count,
                "spacing_pct": f"{grid_spacing_pct * 100:.4f}%",
                "min_required_pct": f"{min_required * 100:.4f}%",
            },
        )

    def _init_grid(self) -> None:
        step = (self.upper_price - self.lower_price) / self.grid_count
        self._grid_levels = [self.lower_price + i * step for i in range(self.grid_count + 1)]

    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        mid = ticker.mid

        # Find levels below mid (buy) and above mid (sell)
        best_buy_level: Optional[tuple[int, Decimal]] = None
        best_sell_level: Optional[tuple[int, Decimal]] = None

        for i, level in enumerate(self._grid_levels):
            if level <= mid and (best_buy_level is None or level > best_buy_level[1]):
                best_buy_level = (i, level)
            if level >= mid and (best_sell_level is None or level < best_sell_level[1]):
                best_sell_level = (i, level)

        # Buy at level below mid
        if best_buy_level and best_buy_level[0] not in self._filled_levels:
            price = best_buy_level[1]
            amount = self.order_amount / price
            expected_return = (mid - price) / price  # return when price reverts to mid

            self._filled_levels.add(best_buy_level[0])

            return Signal(
                symbol=ticker.symbol,
                signal_type=SignalType.BUY_LONG,
                order_side=OrderSide.BUY,
                order_type="limit",
                price=price,
                amount=amount,
                expected_return_rate=expected_return,
            )

        # Sell at level above mid (closing existing grid buys)
        if best_sell_level and best_sell_level[0] in self._filled_levels:
            price = best_sell_level[1]
            amount = self.order_amount / price

            self._filled_levels.discard(best_sell_level[0])

            return Signal(
                symbol=ticker.symbol,
                signal_type=SignalType.SELL_LONG,
                order_side=OrderSide.SELL,
                order_type="limit",
                price=price,
                amount=amount,
                expected_return_rate=(price - mid) / mid,
            )

        return None

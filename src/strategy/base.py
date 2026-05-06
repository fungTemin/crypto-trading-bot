"""Base strategy with mandatory fee-profit gate.

Every strategy MUST pass the fee gate before any signal is emitted.
The gate ensures: expected_return > entry_fee + exit_fee + slippage + min_profit_buffer
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.constants import EventType, MarketType, OrderSide, SignalType
from src.exchange.models import Ticker
from src.strategy.fee_calculator import FeeCalculator, FeeEstimate, FeeSchedule


@dataclass
class Signal:
    """Trading signal emitted by a strategy after passing the fee gate."""

    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    symbol: str = ""
    signal_type: SignalType = SignalType.BUY_LONG
    order_side: OrderSide = OrderSide.BUY
    order_type: str = "limit"
    price: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")
    expected_return_rate: Decimal = Decimal("0")
    fee_estimate: Optional[FeeEstimate] = None
    strategy_name: str = ""
    confidence: Decimal = Decimal("1")
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_profitable(self) -> bool:
        if self.fee_estimate is None:
            return False
        return self.expected_return_rate > self.fee_estimate.round_trip_rate

    @property
    def expected_net_return(self) -> Decimal:
        """Expected return after fees."""
        if self.fee_estimate is None:
            return self.expected_return_rate
        return self.expected_return_rate - self.fee_estimate.round_trip_rate


class BaseStrategy(ABC):
    """Abstract strategy with built-in fee validation gate.

    Subclasses implement compute_signal(). The base on_tick() method
    wraps it with fee-profit validation so no trade can execute
    without covering fees + buffer.

    Flow:
        1. compute_signal(ticker, ohlcv, positions) → raw Signal | None
        2. If signal: FeeCalculator.validate_trade() → (profitable, FeeEstimate)
        3. If profitable: attach FeeEstimate to signal, emit to event bus
        4. If not profitable: log rejection, return None
    """

    def __init__(
        self,
        name: str,
        market_type: MarketType,
        fee_calculator: FeeCalculator,
        event_bus,  # EventBus, avoided for circular import
    ) -> None:
        self.name = name
        self.market_type = market_type
        self.fee_calculator = fee_calculator
        self.event_bus = event_bus
        self._symbols: list[str] = []

    def set_symbols(self, symbols: list[str]) -> None:
        self._symbols = symbols

    @property
    def symbols(self) -> list[str]:
        return self._symbols

    async def on_ticker(self, ticker: Ticker) -> Optional[Signal]:
        """Process a ticker update. Returns Signal if fee gate passes, None otherwise."""
        signal = await self.compute_signal(ticker)
        if signal is None:
            return None

        # Fee gate: validate the trade is worth taking
        profitable, fee_estimate = self.fee_calculator.validate_trade(
            expected_return_rate=signal.expected_return_rate,
            market_type=self.market_type,
            bid=ticker.bid,
            ask=ticker.ask,
        )

        if not profitable:
            return None

        signal.fee_estimate = fee_estimate
        signal.strategy_name = self.name

        await self.event_bus.publish(EventType.SIGNAL, signal=signal)
        return signal

    @abstractmethod
    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        """Compute a trading signal from a ticker. Subclasses implement this.

        Returns None if no trade should be taken.
        """
        ...

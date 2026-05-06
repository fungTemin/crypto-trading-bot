from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from src.core.constants import OrderSide, OrderStatus, OrderType, MarketType

if TYPE_CHECKING:
    from src.strategy.fee_calculator import FeeEstimate


@dataclass
class Ticker:
    symbol: str
    bid: Decimal
    ask: Decimal
    last: Decimal
    high: Decimal
    low: Decimal
    volume: Decimal
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        if self.last == 0:
            return Decimal("0")
        return self.spread / self.last

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2


@dataclass
class Balance:
    asset: str
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass
class Order:
    id: str = ""
    symbol: str = ""
    side: OrderSide = OrderSide.BUY
    order_type: OrderType = OrderType.MARKET
    price: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")
    filled: Decimal = Decimal("0")
    remaining: Decimal = Decimal("0")
    status: OrderStatus = OrderStatus.PENDING
    fee: Optional[FeeEstimate] = None
    metadata: dict = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.status == OrderStatus.CLOSED

    @property
    def is_alive(self) -> bool:
        return self.status in (OrderStatus.PENDING, OrderStatus.OPEN)


@dataclass
class Fill:
    order_id: str
    symbol: str
    side: OrderSide
    price: Decimal
    amount: Decimal
    fee: Decimal
    fee_currency: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class Position:
    symbol: str
    side: OrderSide
    amount: Decimal
    entry_price: Decimal
    current_price: Decimal
    unrealized_pnl: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fee_paid: Decimal = Decimal("0")
    market_type: MarketType = MarketType.SPOT

    @property
    def pnl_pct(self) -> Decimal:
        if self.entry_price == 0:
            return Decimal("0")
        if self.side == OrderSide.BUY:
            return (self.current_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.current_price) / self.entry_price



from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from decimal import Decimal

from src.exchange.models import Balance, Fill, Order, Position, Ticker


class FeeSchedule:
    """Exchange fee structure — single source of truth for fee rates."""

    def __init__(
        self,
        maker: Decimal = Decimal("0.001"),
        taker: Decimal = Decimal("0.001"),
    ) -> None:
        self.maker = maker
        self.taker = taker

    @property
    def conservative_round_trip(self) -> Decimal:
        """Assume taker on both entry and exit (worst case)."""
        return self.taker + self.taker

    @classmethod
    def spot_default(cls) -> FeeSchedule:
        """Spot market: typically 0.1% per side."""
        return cls(maker=Decimal("0.001"), taker=Decimal("0.001"))

    @classmethod
    def futures_default(cls) -> FeeSchedule:
        """Futures: typically 0.02% maker, 0.04% taker."""
        return cls(maker=Decimal("0.0002"), taker=Decimal("0.0004"))

    # Aliases for backward compatibility with code that used the old
    # duplicate FeeSchedule from strategy/fee_calculator.py
    spot = spot_default
    futures = futures_default


class ExchangeInterface(ABC):
    """Abstract interface for all exchange backends.

    Implementations: CCXTExchange (live), PaperExchange (simulation).
    BacktestEngine reuses PaperExchange for fill simulation.
    """

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> Ticker:
        ...

    @abstractmethod
    async def fetch_balance(self) -> dict[str, Balance]:
        ...

    @abstractmethod
    async def fetch_positions(self) -> list[Position]:
        ...

    @abstractmethod
    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: Decimal,
        price: Decimal | None = None,
    ) -> Order:
        ...

    @abstractmethod
    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        ...

    @abstractmethod
    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        ...

    @abstractmethod
    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        ...

    @abstractmethod
    async def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        ...

    @property
    @abstractmethod
    def fee_schedule(self) -> FeeSchedule:
        ...

    @abstractmethod
    async def close(self) -> None:
        ...

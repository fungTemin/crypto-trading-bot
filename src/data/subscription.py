"""Data feed subscription manager.

Manages which symbols and timeframes are being tracked,
distributing market data events to registered callbacks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Awaitable
from typing import Any

from src.core.constants import EventType
from src.exchange.models import Ticker
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

TickerCallback = Callable[[Ticker], Awaitable[None]]


class SubscriptionManager:
    """Manages ticker subscriptions and routes data to callbacks."""

    def __init__(self) -> None:
        self._ticker_callbacks: dict[str, list[TickerCallback]] = defaultdict(list)

    def subscribe_ticker(self, symbol: str, callback: TickerCallback) -> None:
        """Register a callback for ticker updates on a symbol."""
        self._ticker_callbacks[symbol].append(callback)
        logger.info("Subscribed to ticker", extra={"symbol": symbol})

    def unsubscribe_ticker(self, symbol: str, callback: TickerCallback) -> None:
        """Remove a ticker callback."""
        try:
            self._ticker_callbacks[symbol].remove(callback)
        except ValueError:
            pass

    async def on_ticker(self, ticker: Ticker) -> None:
        """Distribute a ticker update to all registered callbacks."""
        callbacks = self._ticker_callbacks.get(ticker.symbol, [])
        for cb in callbacks:
            try:
                await cb(ticker)
            except Exception as e:
                logger.error(
                    "Ticker callback error",
                    extra={"symbol": ticker.symbol, "error": str(e)},
                )

    @property
    def subscribed_symbols(self) -> list[str]:
        return list(self._ticker_callbacks.keys())

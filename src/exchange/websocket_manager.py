"""WebSocket/polling data feed manager.

Uses ccxt's REST API in a polling loop (ccxt pro would provide true WebSocket).
For production, consider ccxt.pro or exchange-native WebSocket clients.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from src.data.ohlcv_store import Candle, OHLCVStore
from src.data.subscription import SubscriptionManager
from src.exchange.models import Ticker
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class DataFeedManager:
    """Polling-based data feed that bridges exchange data to strategies.

    Polls REST endpoints on a configurable interval, updates the OHLCV store,
    and distributes ticker updates via the SubscriptionManager.
    """

    def __init__(
        self,
        exchange,          # ExchangeInterface
        subscription: SubscriptionManager,
        ohlcv_store: OHLCVStore,
        poll_interval: float = 5.0,    # seconds
        timeframe: str = "5m",
    ) -> None:
        self._exchange = exchange
        self._subscription = subscription
        self._ohlcv = ohlcv_store
        self._poll_interval = poll_interval
        self._timeframe = timeframe
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def start(self, symbols: list[str]) -> None:
        """Start polling for the given symbols."""
        self._running = True
        self._tasks = [
            asyncio.create_task(self._poll_symbol(symbol))
            for symbol in symbols
        ]
        logger.info("Data feed started", extra={"symbols": symbols, "interval": self._poll_interval})

    async def stop(self) -> None:
        """Stop all polling tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Data feed stopped")

    async def _poll_symbol(self, symbol: str) -> None:
        """Poll ticker and OHLCV for a single symbol."""
        while self._running:
            try:
                # Fetch ticker
                ticker = await self._exchange.fetch_ticker(symbol)

                # Fetch OHLCV and update store
                candles_raw = await self._exchange.fetch_ohlcv(symbol, self._timeframe, limit=2)
                candles = [Candle.from_ccxt(c) for c in candles_raw]
                for c in candles:
                    self._ohlcv.add(symbol, self._timeframe, c)

                # Distribute to subscribers
                await self._subscription.on_ticker(ticker)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Poll error", extra={"symbol": symbol, "error": str(e)})

            await asyncio.sleep(self._poll_interval)

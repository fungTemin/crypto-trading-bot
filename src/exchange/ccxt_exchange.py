from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import ccxt.async_support as ccxt_async

from src.exchange.base import ExchangeInterface, FeeSchedule
from src.exchange.models import Balance, Fill, Order, Position, Ticker
from src.core.constants import OrderSide, OrderStatus, OrderType, MarketType
from src.utils.logger import setup_logger
from src.utils.retry import retry_async

logger = setup_logger(__name__)


class CCXTExchange(ExchangeInterface):
    """Live exchange adapter wrapping ccxt.async_support."""

    def __init__(
        self,
        exchange_id: str,
        api_key: str = "",
        secret: str = "",
        password: str = "",
        sandbox: bool = True,
        timeout: int = 30000,
        market_type: MarketType = MarketType.SPOT,
    ) -> None:
        self.exchange_id = exchange_id
        self.market_type = market_type
        self._opts: dict[str, Any] = {"timeout": timeout}

        if sandbox:
            self._opts["sandbox"] = True

        exchange_class = getattr(ccxt_async, exchange_id, None)
        if exchange_class is None:
            raise ValueError(f"Unknown exchange: {exchange_id}")

        self._client = exchange_class({
            "apiKey": api_key,
            "secret": secret,
            "password": password,
            "timeout": timeout,
            "enableRateLimit": True,
            "options": {"defaultType": market_type.value},
        })

        self._fee_schedule = FeeSchedule.spot_default() if market_type == MarketType.SPOT else FeeSchedule.futures_default()

        logger.info(
            "CCXT exchange initialized",
            extra={"exchange": exchange_id, "sandbox": sandbox, "market": market_type.value},
        )

    @property
    def fee_schedule(self) -> FeeSchedule:
        return self._fee_schedule

    async def _update_fee_schedule(self) -> None:
        """Fetch actual fees from exchange metadata if available."""
        try:
            markets = await retry_async(lambda: self._client.fetch_markets())
            if markets:
                market = markets[0]
                self._fee_schedule = FeeSchedule(
                    maker=Decimal(str(market.get("maker", 0.001))),
                    taker=Decimal(str(market.get("taker", 0.001))),
                )
        except Exception as e:
            logger.warning("Failed to fetch fee schedule, using defaults", extra={"error": str(e)})

    async def fetch_ticker(self, symbol: str) -> Ticker:
        raw = await retry_async(lambda: self._client.fetch_ticker(symbol))
        return Ticker(
            symbol=symbol,
            bid=Decimal(str(raw["bid"] or 0)),
            ask=Decimal(str(raw["ask"] or 0)),
            last=Decimal(str(raw["last"] or 0)),
            high=Decimal(str(raw["high"] or 0)),
            low=Decimal(str(raw["low"] or 0)),
            volume=Decimal(str(raw["baseVolume"] or raw.get("volume", 0))),
        )

    async def fetch_balance(self) -> dict[str, Balance]:
        raw = await retry_async(lambda: self._client.fetch_balance())
        balances: dict[str, Balance] = {}
        for asset, data in (raw.get("free", {}) or {}).items():
            free = Decimal(str(raw["free"].get(asset, 0)))
            locked = Decimal(str(raw["used"].get(asset, 0)))
            if free > 0 or locked > 0:
                balances[asset] = Balance(asset=asset, free=free, locked=locked)
        return balances

    async def fetch_positions(self) -> list[Position]:
        if self.market_type == MarketType.SPOT:
            balances = await self.fetch_balance()
            # Spot positions derived from non-quote balances
            positions: list[Position] = []
            # Fetch tickers concurrently for all held assets
            symbols_to_fetch = []
            for asset, bal in balances.items():
                if bal.total > 0:
                    symbols_to_fetch.append(f"{asset}/USDT")

            async def _fetch_safe(sym: str):
                try:
                    return sym, await self.fetch_ticker(sym)
                except Exception:
                    return sym, None

            ticker_results = await asyncio.gather(*[_fetch_safe(s) for s in symbols_to_fetch])
            tickers: dict[str, Ticker] = {s: t for s, t in ticker_results if t is not None}

            for asset, bal in balances.items():
                if bal.total > 0:
                    symbol = f"{asset}/USDT"
                    ticker = tickers.get(symbol)
                    if ticker is None:
                        continue
                    positions.append(Position(
                        symbol=symbol,
                        side=OrderSide.BUY,
                        amount=bal.total,
                        entry_price=ticker.last,  # approximate
                        current_price=ticker.last,
                        market_type=MarketType.SPOT,
                    ))
            return positions

        # Futures
        raw = await retry_async(lambda: self._client.fetch_positions())
        positions: list[Position] = []
        for p in raw:
            if float(p.get("contracts", 0)) == 0:
                continue
            positions.append(Position(
                symbol=p["symbol"],
                side=OrderSide.BUY if p.get("side", "long") == "long" else OrderSide.SELL,
                amount=Decimal(str(p["contracts"])),
                entry_price=Decimal(str(p["entryPrice"] or 0)),
                current_price=Decimal(str(p["markPrice"] or 0)),
                unrealized_pnl=Decimal(str(p.get("unrealizedPnl", 0))),
                realized_pnl=Decimal(str(p.get("realizedPnl", 0))),
                market_type=self.market_type,
            ))
        return positions

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: Decimal,
        price: Decimal | None = None,
    ) -> Order:
        params: dict[str, Any] = {"symbol": symbol, "type": order_type, "side": side, "amount": float(amount)}
        if price is not None:
            params["price"] = float(price)

        raw = await retry_async(lambda: self._client.create_order(**params))

        return Order(
            id=raw.get("id", ""),
            symbol=symbol,
            side=OrderSide(side),
            order_type=OrderType(order_type),
            price=Decimal(str(raw.get("price", price or 0))),
            amount=amount,
            filled=Decimal(str(raw.get("filled", 0))),
            remaining=Decimal(str(raw.get("remaining", amount))),
            status=OrderStatus(raw.get("status", "open")),
        )

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        try:
            await retry_async(lambda: self._client.cancel_order(order_id, symbol))
            return True
        except Exception as e:
            logger.error("Cancel order failed", extra={"order_id": order_id, "error": str(e)})
            return False

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        raw = await retry_async(
            lambda: self._client.fetch_ohlcv(symbol, timeframe, since=since, limit=limit)
        )
        keys = ["timestamp", "open", "high", "low", "close", "volume"]
        return [dict(zip(keys, candle)) for candle in raw]

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        raw = await retry_async(lambda: self._client.fetch_order_book(symbol, limit))
        return {
            "bids": [(Decimal(str(p)), Decimal(str(q))) for p, q in raw.get("bids", [])],
            "asks": [(Decimal(str(p)), Decimal(str(q))) for p, q in raw.get("asks", [])],
            "timestamp": raw.get("timestamp", 0),
        }

    async def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        """WebSocket-based ticker stream. Requires ccxt pro for real WebSocket support."""
        while True:
            try:
                ticker = await self.fetch_ticker(symbol)
                yield ticker
                await asyncio.sleep(1)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Ticker watch error", extra={"symbol": symbol, "error": str(e)})
                await asyncio.sleep(5)

    async def close(self) -> None:
        await self._client.close()
        logger.info("CCXT exchange connection closed")

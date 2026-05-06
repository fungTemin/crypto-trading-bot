from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from decimal import Decimal

from src.exchange.base import ExchangeInterface, FeeSchedule
from src.exchange.models import Balance, Fill, Order, Position, Ticker
from src.core.constants import OrderSide, OrderStatus, OrderType, MarketType
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class PaperExchange(ExchangeInterface):
    """Paper trading simulator.

    Wraps a live ExchangeInterface for market data, but simulates all
    fills and maintains virtual balances. No real orders are placed.

    Also reused by BacktestEngine for fill simulation against historical data.
    """

    def __init__(
        self,
        data_source: ExchangeInterface | None = None,
        initial_balances: dict[str, Decimal] | None = None,
        fee_schedule: FeeSchedule | None = None,
    ) -> None:
        self._data_source = data_source
        self._fee_schedule = fee_schedule or FeeSchedule.spot_default()

        initial = initial_balances or {"USDT": Decimal("10000")}
        self._balances: dict[str, Balance] = {
            asset: Balance(asset=asset, free=amount, locked=Decimal("0"))
            for asset, amount in initial.items()
        }

        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._positions: dict[str, Position] = {}
        self._last_trade_times: dict[str, datetime] = {}

        self._ticker_cache: dict[str, Ticker] = {}
        self._ticker_override: dict[str, Ticker] = {}

    @property
    def fee_schedule(self) -> FeeSchedule:
        return self._fee_schedule

    def set_ticker(self, ticker: Ticker) -> None:
        """Override ticker for backtesting (no live data source needed)."""
        self._ticker_override[ticker.symbol] = ticker
        self._ticker_cache[ticker.symbol] = ticker

    def set_balance(self, asset: str, free: Decimal) -> None:
        self._balances[asset] = Balance(asset=asset, free=free, locked=Decimal("0"))

    async def fetch_ticker(self, symbol: str) -> Ticker:
        if symbol in self._ticker_override:
            return self._ticker_override[symbol]
        if self._data_source:
            ticker = await self._data_source.fetch_ticker(symbol)
            self._ticker_cache[symbol] = ticker
            return ticker
        cached = self._ticker_cache.get(symbol)
        if cached is None:
            raise ValueError(f"No ticker data for {symbol}")
        return cached

    async def fetch_balance(self) -> dict[str, Balance]:
        return dict(self._balances)

    async def fetch_positions(self) -> list[Position]:
        return list(self._positions.values())

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        amount: Decimal,
        price: Decimal | None = None,
    ) -> Order:
        ticker = await self.fetch_ticker(symbol)
        side_enum = OrderSide(side)
        fill_price = price if price is not None else ticker.last

        if side_enum == OrderSide.BUY:
            fill_price = price or ticker.ask
        else:
            fill_price = price or ticker.bid

        # Calculate fee
        fee_rate = self._fee_schedule.taker
        fee = amount * fill_price * fee_rate

        # Simulate fill
        order_id = str(uuid.uuid4())[:8]
        order = Order(
            id=order_id,
            symbol=symbol,
            side=side_enum,
            order_type=OrderType(order_type),
            price=fill_price,
            amount=amount,
            filled=amount,
            remaining=Decimal("0"),
            status=OrderStatus.CLOSED,
        )

        # Update balances
        base, quote = symbol.split("/", 1)
        quote_cost = amount * fill_price
        total_cost = quote_cost + fee

        if side_enum == OrderSide.BUY:
            if self._balances.get(quote, Balance(quote, 0, 0)).free < total_cost:
                order.status = OrderStatus.REJECTED
                logger.warning("Insufficient balance", extra={"symbol": symbol, "needed": str(total_cost)})
                return order

            self._balances[quote] = Balance(
                asset=quote,
                free=self._balances.get(quote, Balance(quote, 0, 0)).free - total_cost,
                locked=Decimal("0"),
            )
            existing = self._balances.get(base, Balance(base, 0, 0))
            self._balances[base] = Balance(asset=base, free=existing.free + amount, locked=existing.locked)
        else:
            if self._balances.get(base, Balance(base, 0, 0)).free < amount:
                order.status = OrderStatus.REJECTED
                logger.warning("Insufficient balance", extra={"symbol": symbol, "needed": str(amount)})
                return order

            self._balances[base] = Balance(
                asset=base,
                free=self._balances.get(base, Balance(base, 0, 0)).free - amount,
                locked=Decimal("0"),
            )
            existing = self._balances.get(quote, Balance(quote, 0, 0))
            self._balances[quote] = Balance(
                asset=quote,
                free=existing.free + quote_cost - fee,
                locked=existing.locked,
            )

        # Record fill
        fill = Fill(
            order_id=order_id,
            symbol=symbol,
            side=side_enum,
            price=fill_price,
            amount=amount,
            fee=fee,
            fee_currency=quote,
        )
        self._fills.append(fill)
        self._orders[order_id] = order
        self._last_trade_times[symbol] = datetime.now(timezone.utc)

        # Update position
        if symbol in self._positions:
            pos = self._positions[symbol]
            if pos.side == side_enum:
                # Adding to existing
                total_amount = pos.amount + amount
                pos.entry_price = (pos.entry_price * pos.amount + fill_price * amount) / total_amount
                pos.amount = total_amount
            else:
                # Closing or reversing
                closed = min(pos.amount, amount)
                profit = (fill_price - pos.entry_price) * closed if pos.side == OrderSide.BUY else (pos.entry_price - fill_price) * closed
                pos.realized_pnl += profit
                pos.fee_paid += fee
                pos.amount -= closed
                if pos.amount == 0:
                    del self._positions[symbol]
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                side=side_enum,
                amount=amount,
                entry_price=fill_price,
                current_price=fill_price,
                fee_paid=fee,
            )

        logger.info(
            "Paper fill",
            extra={
                "symbol": symbol,
                "side": side,
                "price": str(fill_price),
                "amount": str(amount),
                "fee": str(fee),
            },
        )
        return order

    async def cancel_order(self, order_id: str, symbol: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = OrderStatus.CANCELED
            return True
        return False

    async def fetch_ohlcv(
        self,
        symbol: str,
        timeframe: str,
        since: int | None = None,
        limit: int = 100,
    ) -> list[dict]:
        if self._data_source:
            return await self._data_source.fetch_ohlcv(symbol, timeframe, since, limit)
        return []

    async def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        if self._data_source:
            return await self._data_source.fetch_order_book(symbol, limit)
        return {"bids": [], "asks": [], "timestamp": 0}

    async def watch_ticker(self, symbol: str) -> AsyncIterator[Ticker]:
        if self._data_source:
            async for ticker in self._data_source.watch_ticker(symbol):
                self.set_ticker(ticker)
                yield ticker
        else:
            while True:
                import asyncio
                if symbol in self._ticker_override:
                    yield self._ticker_override[symbol]
                await asyncio.sleep(1)

    @property
    def fills(self) -> list[Fill]:
        return list(self._fills)

    @property
    def total_fees_paid(self) -> Decimal:
        return sum(f.fee for f in self._fills)

    def get_equity(self, quote_currency: str = "USDT") -> Decimal:
        """Calculate total equity in quote currency."""
        total = Decimal("0")
        for asset, bal in self._balances.items():
            if asset == quote_currency:
                total += bal.total
            else:
                symbol = f"{asset}/{quote_currency}"
                try:
                    # Use cached ticker or fetch
                    cached = self._ticker_cache.get(symbol)
                    if cached:
                        total += bal.total * cached.last
                except Exception:
                    pass
        return total

    async def close(self) -> None:
        if self._data_source:
            await self._data_source.close()

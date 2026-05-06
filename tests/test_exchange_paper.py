"""Tests for PaperExchange and exchange models."""

from __future__ import annotations

from decimal import Decimal

import pytest

from src.core.constants import OrderSide, OrderStatus, OrderType, MarketType
from src.exchange.base import FeeSchedule
from src.exchange.models import Balance, Ticker, Position
from src.exchange.paper_exchange import PaperExchange


def make_ticker(symbol: str = "BTC/USDT", price: Decimal = Decimal("50000")) -> Ticker:
    spread = price * Decimal("0.0001")
    return Ticker(
        symbol=symbol,
        bid=price - spread,
        ask=price + spread,
        last=price,
        high=price * Decimal("1.01"),
        low=price * Decimal("0.99"),
        volume=Decimal("100000"),
    )


class TestPaperExchange:
    @pytest.mark.asyncio
    async def test_initial_balance(self) -> None:
        ex = PaperExchange(initial_balances={"USDT": Decimal("10000")})
        balances = await ex.fetch_balance()
        assert "USDT" in balances
        assert balances["USDT"].total == Decimal("10000")

    @pytest.mark.asyncio
    async def test_buy_order_updates_balance(self) -> None:
        ex = PaperExchange(initial_balances={"USDT": Decimal("100000")})
        ticker = make_ticker(price=Decimal("50000"))
        ex.set_ticker(ticker)

        fee_schedule = FeeSchedule(maker=Decimal("0.001"), taker=Decimal("0.001"))
        ex._fee_schedule = fee_schedule

        order = await ex.create_order(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            amount=Decimal("1"),
        )

        assert order.status == OrderStatus.CLOSED
        assert order.filled == Decimal("1")

        balances = await ex.fetch_balance()
        assert "BTC" in balances
        assert balances["BTC"].total == Decimal("1")
        # USDT should be 100000 - 50000 - fee
        assert balances["USDT"].total < Decimal("50000")

    @pytest.mark.asyncio
    async def test_sell_order_updates_balance(self) -> None:
        ex = PaperExchange(initial_balances={"BTC": Decimal("2"), "USDT": Decimal("0")})
        ticker = make_ticker(price=Decimal("50000"))
        ex.set_ticker(ticker)

        ex._fee_schedule = FeeSchedule(maker=Decimal("0.001"), taker=Decimal("0.001"))

        order = await ex.create_order(
            symbol="BTC/USDT",
            side="sell",
            order_type="market",
            amount=Decimal("1"),
        )

        assert order.status == OrderStatus.CLOSED

        balances = await ex.fetch_balance()
        assert balances["BTC"].total == Decimal("1")
        # USDT = 50000 - fee
        assert balances["USDT"].total > 0

    @pytest.mark.asyncio
    async def test_rejects_buy_with_insufficient_balance(self) -> None:
        ex = PaperExchange(initial_balances={"USDT": Decimal("100")})
        ticker = make_ticker(price=Decimal("50000"))
        ex.set_ticker(ticker)

        order = await ex.create_order(
            symbol="BTC/USDT",
            side="buy",
            order_type="market",
            amount=Decimal("1"),
        )

        assert order.status == OrderStatus.REJECTED

    @pytest.mark.asyncio
    async def test_tracks_total_fees(self) -> None:
        ex = PaperExchange(initial_balances={"USDT": Decimal("100000")})
        ticker = make_ticker(price=Decimal("50000"))
        ex.set_ticker(ticker)

        ex._fee_schedule = FeeSchedule(maker=Decimal("0.001"), taker=Decimal("0.001"))

        await ex.create_order(symbol="BTC/USDT", side="buy", order_type="market", amount=Decimal("1"))

        assert ex.total_fees_paid > 0

    @pytest.mark.asyncio
    async def test_get_equity(self) -> None:
        ex = PaperExchange(initial_balances={"USDT": Decimal("10000")})
        ticker = make_ticker(price=Decimal("50000"))
        ex.set_ticker(ticker)

        equity = ex.get_equity("USDT")
        assert equity == Decimal("10000")

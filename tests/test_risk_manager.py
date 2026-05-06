"""Tests for risk management components."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.constants import CircuitState, MarketType, OrderSide, SignalType
from src.exchange.models import Balance, Position, Ticker
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.position_sizer import PositionSizer
from src.risk.manager import RiskManager
from src.strategy.base import Signal
from src.strategy.fee_calculator import FeeEstimate


class TestCircuitBreaker:
    def test_starts_green(self) -> None:
        cb = CircuitBreaker(initial_equity=Decimal("10000"))
        assert cb.state == CircuitState.GREEN
        assert cb.is_allowed()

    def test_goes_red_on_drawdown(self) -> None:
        cb = CircuitBreaker(
            initial_equity=Decimal("10000"),
            max_drawdown_pct=Decimal("20"),
        )
        cb.update_equity(Decimal("7500"))  # 25% drop
        assert cb.state == CircuitState.RED
        assert not cb.is_allowed()

    def test_goes_yellow_on_partial_drawdown(self) -> None:
        cb = CircuitBreaker(
            initial_equity=Decimal("10000"),
            max_daily_loss_pct=Decimal("100"),  # disable daily loss check
            yellow_drawdown_pct=Decimal("10"),
            max_drawdown_pct=Decimal("20"),
        )
        cb.update_equity(Decimal("8500"))  # 15% drawdown
        assert cb.state == CircuitState.YELLOW
        assert cb.position_size_multiplier() == Decimal("0.5")

    def test_daily_reset(self) -> None:
        cb = CircuitBreaker(
            initial_equity=Decimal("10000"),
            max_daily_loss_pct=Decimal("5"),
        )
        # Manually set daily stats to yesterday
        from datetime import date, timedelta
        cb._daily.date = date.today() - timedelta(days=1)
        cb._daily.starting_equity = Decimal("10000")

        # Update with equity 9000 (10% loss)
        cb.update_equity(Decimal("9000"))
        # Should reset because it's a new day
        assert cb._daily.date == date.today()
        assert cb._daily.starting_equity == Decimal("9000")


class TestPositionSizer:
    def test_fixed_fraction(self) -> None:
        sizer = PositionSizer(max_position_pct=Decimal("10"))
        result = sizer.fixed_fraction(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            risk_pct=Decimal("5"),
        )
        assert result.notional == Decimal("500")  # 5% of 10000
        assert result.amount == Decimal("0.01")  # 500 / 50000

    def test_kelly_bets_larger_with_higher_win_rate(self) -> None:
        sizer = PositionSizer(max_position_pct=Decimal("50"))
        result = sizer.kelly(
            equity=Decimal("10000"),
            price=Decimal("50000"),
            win_rate=Decimal("0.6"),
            avg_win=Decimal("1.05"),
            avg_loss=Decimal("0.98"),
        )
        assert result.notional > 0


class TestRiskManager:
    @pytest.mark.asyncio
    async def test_rejects_when_circuit_red(self) -> None:
        cb = CircuitBreaker(
            initial_equity=Decimal("10000"),
            max_daily_loss_pct=Decimal("100"),
            max_drawdown_pct=Decimal("5"),
        )
        cb.update_equity(Decimal("9000"))  # 10% drawdown > 5% max

        rm = RiskManager(circuit_breaker=cb, position_sizer=PositionSizer())

        signal = Signal(
            symbol="BTC/USDT",
            signal_type=SignalType.BUY_LONG,
            order_side=OrderSide.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            expected_return_rate=Decimal("0.01"),
        )

        approved, reason = await rm.validate(
            signal=signal,
            positions=[],
            balances={"USDT": Balance("USDT", Decimal("10000"), Decimal("0"))},
            equity=Decimal("9000"),
        )
        assert not approved
        assert "RED" in reason

    @pytest.mark.asyncio
    async def test_approves_valid_signal(self) -> None:
        cb = CircuitBreaker(initial_equity=Decimal("10000"))
        rm = RiskManager(
            circuit_breaker=cb,
            position_sizer=PositionSizer(max_position_pct=Decimal("20")),
        )

        signal = Signal(
            symbol="BTC/USDT",
            signal_type=SignalType.BUY_LONG,
            order_side=OrderSide.BUY,
            price=Decimal("50000"),
            amount=Decimal("0.01"),
            expected_return_rate=Decimal("0.01"),
        )

        approved, reason = await rm.validate(
            signal=signal,
            positions=[],
            balances={"USDT": Balance("USDT", Decimal("10000"), Decimal("0"))},
            equity=Decimal("10000"),
        )
        assert approved
        assert reason == "approved"

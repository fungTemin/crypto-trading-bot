"""Tests for fee_calculator — the core fee-profit gate."""

from __future__ import annotations

from decimal import Decimal

from src.core.constants import MarketType
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule, FeeEstimate


class TestFeeEstimate:
    def test_round_trip_rate(self) -> None:
        est = FeeEstimate(
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.0005"),
        )
        assert est.round_trip_rate == Decimal("0.0025")
        assert est.round_trip_pct == Decimal("0.25")

    def test_is_profitable_returns_false_when_below_cost(self) -> None:
        est = FeeEstimate(
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.0005"),
        )
        # Expected return 0.2% is less than 0.25% total cost
        assert not est.is_profitable(Decimal("0.002"))

    def test_is_profitable_returns_true_when_above_cost(self) -> None:
        est = FeeEstimate(
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.0005"),
        )
        # Expected return 0.3% exceeds 0.25% total cost
        assert est.is_profitable(Decimal("0.003"))

    def test_min_profit_price_long(self) -> None:
        est = FeeEstimate(
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.0005"),
        )
        min_price = est.min_profit_price_long(Decimal("50000"))
        expected = Decimal("50000") * (Decimal("1") + Decimal("0.0025"))
        assert min_price == expected

    def test_min_profit_price_short(self) -> None:
        est = FeeEstimate(
            entry_fee_rate=Decimal("0.001"),
            exit_fee_rate=Decimal("0.001"),
            slippage_rate=Decimal("0.0005"),
        )
        min_price = est.min_profit_price_short(Decimal("50000"))
        expected = Decimal("50000") * (Decimal("1") - Decimal("0.0025"))
        assert min_price == expected


class TestFeeSchedule:
    def test_spot_defaults(self) -> None:
        fs = FeeSchedule.spot()
        assert fs.maker == Decimal("0.001")
        assert fs.taker == Decimal("0.001")

    def test_futures_defaults(self) -> None:
        fs = FeeSchedule.futures()
        assert fs.maker == Decimal("0.0002")
        assert fs.taker == Decimal("0.0004")


class TestFeeCalculator:
    def test_rejects_unprofitable_trade(self) -> None:
        calc = FeeCalculator(
            fee_schedule=FeeSchedule.spot(),
            min_profit_buffer=Decimal("0.0005"),
        )
        profitable, est = calc.validate_trade(
            expected_return_rate=Decimal("0.001"),  # 0.1% — too small
            market_type=MarketType.SPOT,
            bid=Decimal("50000"),
            ask=Decimal("50050"),  # wide spread
        )
        assert not profitable
        assert est.round_trip_rate > Decimal("0.001")

    def test_accepts_profitable_trade(self) -> None:
        calc = FeeCalculator(
            fee_schedule=FeeSchedule.spot(),
            min_profit_buffer=Decimal("0.0005"),
        )
        profitable, est = calc.validate_trade(
            expected_return_rate=Decimal("0.01"),  # 1% — covers fees easily
            market_type=MarketType.SPOT,
            bid=Decimal("50000"),
            ask=Decimal("50050"),
        )
        assert profitable

    def test_futures_lower_cost_than_spot(self) -> None:
        spot_calc = FeeCalculator(FeeSchedule.spot())
        futures_calc = FeeCalculator(FeeSchedule.futures())

        _, spot_est = spot_calc.validate_trade(
            Decimal("0.01"), MarketType.SPOT, Decimal("50000"), Decimal("50025"),
        )
        _, futures_est = futures_calc.validate_trade(
            Decimal("0.01"), MarketType.FUTURE, Decimal("50000"), Decimal("50025"),
        )

        # Futures fees should be cheaper
        assert futures_est.round_trip_rate < spot_est.round_trip_rate

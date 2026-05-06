"""Pure fee calculation — no I/O, no async.

Every strategy calls this module BEFORE emitting a signal to ensure
the expected return exceeds fees + slippage + profit buffer.

Core formula:
    expected_return > entry_fee + exit_fee + slippage + min_profit_buffer

Fee assumptions (conservative):
    - Entry: always assume taker (market order fills immediately)
    - Exit:  always assume taker (limit orders may not fill, trapping the position)
    - Slippage: derived from bid-ask spread at time of signal
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.core.constants import MarketType


@dataclass(frozen=True)
class FeeEstimate:
    """Breakdown of estimated costs for a round-trip trade."""

    entry_fee_rate: Decimal   # e.g. 0.001 for spot taker
    exit_fee_rate: Decimal    # e.g. 0.001 for spot taker
    slippage_rate: Decimal    # e.g. 0.0005 from bid-ask spread
    market_type: MarketType = MarketType.SPOT

    @property
    def total_fee_rate(self) -> Decimal:
        """Entry + exit fee only (no slippage)."""
        return self.entry_fee_rate + self.exit_fee_rate

    @property
    def round_trip_rate(self) -> Decimal:
        """Total cost: entry fee + exit fee + slippage."""
        return self.entry_fee_rate + self.exit_fee_rate + self.slippage_rate

    @property
    def round_trip_pct(self) -> Decimal:
        """Round-trip cost as percentage (multiplied by 100)."""
        return self.round_trip_rate * 100

    def is_profitable(self, expected_return_rate: Decimal, min_buffer: Decimal = Decimal("0")) -> bool:
        """Check if expected return exceeds total costs + minimum profit buffer.

        Args:
            expected_return_rate: Expected return as a decimal (e.g. 0.01 = 1%).
            min_buffer: Minimum net profit required after fees (e.g. 0.0005 = 0.05%).

        Returns:
            True if the trade is expected to be profitable after all costs.
        """
        return expected_return_rate > self.round_trip_rate + min_buffer

    def min_profit_price_long(self, entry_price: Decimal, min_buffer: Decimal = Decimal("0")) -> Decimal:
        """Minimum exit price for a long position to be profitable after fees.

        Args:
            entry_price: The price at which the position was entered.
            min_buffer: Minimum net profit required after fees.

        Returns:
            The minimum price needed at exit to achieve profitability.
        """
        return entry_price * (Decimal("1") + self.round_trip_rate + min_buffer)

    def min_profit_price_short(self, entry_price: Decimal, min_buffer: Decimal = Decimal("0")) -> Decimal:
        """Minimum exit price for a short position to be profitable after fees.

        Args:
            entry_price: The price at which the short was entered.
            min_buffer: Minimum net profit required after fees.

        Returns:
            The maximum price needed at exit to achieve profitability.
        """
        return entry_price * (Decimal("1") - self.round_trip_rate - min_buffer)


@dataclass(frozen=True)
class FeeSchedule:
    """Fee structure for a market type."""
    maker: Decimal
    taker: Decimal

    @classmethod
    def spot(cls, maker_pct: Decimal = Decimal("0.001"), taker_pct: Decimal = Decimal("0.001")) -> FeeSchedule:
        """Spot market: typically 0.1% per side."""
        return cls(maker=maker_pct, taker=taker_pct)

    @classmethod
    def futures(cls, maker_pct: Decimal = Decimal("0.0002"), taker_pct: Decimal = Decimal("0.0004")) -> FeeSchedule:
        """Futures: typically 0.02% maker, 0.04% taker."""
        return cls(maker=maker_pct, taker=taker_pct)


class FeeCalculator:
    """Calculates fee estimates for trades.

    Always makes conservative assumptions:
        - Entry and exit are both taker (worst case).
        - Slippage is estimated from current bid-ask spread.
    """

    def __init__(
        self,
        fee_schedule: FeeSchedule,
        min_profit_buffer: Decimal = Decimal("0.0005"),  # 0.05% minimum net profit
    ) -> None:
        self._fee_schedule = fee_schedule
        self._min_profit_buffer = min_profit_buffer

    def estimate_round_trip(
        self,
        market_type: MarketType,
        bid: Decimal,
        ask: Decimal,
    ) -> FeeEstimate:
        """Estimate total round-trip costs including slippage.

        Args:
            market_type: SPOT or FUTURE.
            bid: Current best bid price.
            ask: Current best ask price.

        Returns:
            FeeEstimate with all cost components.
        """
        # Slippage from bid-ask spread
        mid = (bid + ask) / 2
        slippage_rate = (ask - bid) / mid if mid > 0 else Decimal("0.001")

        return FeeEstimate(
            entry_fee_rate=self._fee_schedule.taker,
            exit_fee_rate=self._fee_schedule.taker,
            slippage_rate=slippage_rate,
            market_type=market_type,
        )

    def validate_trade(
        self,
        expected_return_rate: Decimal,
        market_type: MarketType,
        bid: Decimal,
        ask: Decimal,
    ) -> tuple[bool, FeeEstimate]:
        """Validate if a trade is worth taking after fees.

        Args:
            expected_return_rate: Strategy's expected return as decimal.
            market_type: SPOT or FUTURE.
            bid: Current best bid.
            ask: Current best ask.

        Returns:
            (is_profitable, fee_estimate) tuple.
        """
        estimate = self.estimate_round_trip(market_type, bid, ask)
        profitable = estimate.is_profitable(expected_return_rate, self._min_profit_buffer)
        return profitable, estimate

    @property
    def min_profit_buffer(self) -> Decimal:
        return self._min_profit_buffer


def calculate_break_even(
    entry_price: Decimal,
    fee_rate: Decimal,
    is_long: bool = True,
) -> Decimal:
    """Quick break-even price calculation for a single leg.

    break_even = entry_price * (1 + fee_rate)   for longs
    break_even = entry_price * (1 - fee_rate)   for shorts
    """
    if is_long:
        return entry_price * (Decimal("1") + fee_rate)
    return entry_price * (Decimal("1") - fee_rate)

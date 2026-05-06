"""Position sizing algorithms.

Strategies:
    1. Fixed fraction: risk a fixed % of equity per trade
    2. Kelly criterion: bet proportion = edge / odds (growth-optimal)
    3. Volatility targeting: size inversely proportional to ATR
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class SizeResult:
    amount: Decimal         # base currency (e.g. BTC)
    notional: Decimal       # quote currency (e.g. USDT)
    pct_of_equity: Decimal
    method: str


class PositionSizer:
    """Calculates position size based on risk parameters."""

    def __init__(
        self,
        max_position_pct: Decimal = Decimal("10"),   # max 10% of equity per trade
        max_leverage: int = 1,
    ) -> None:
        self.max_position_pct = max_position_pct / 100
        self.max_leverage = max_leverage

    def fixed_fraction(
        self,
        equity: Decimal,
        price: Decimal,
        risk_pct: Decimal | None = None,
    ) -> SizeResult:
        """Size a position as a fixed % of equity.

        Args:
            equity: Total portfolio equity in quote currency.
            price: Current price of the asset.
            risk_pct: % of equity to allocate (defaults to max_position_pct).

        Returns:
            SizeResult with calculated amount.
        """
        fraction = (risk_pct / 100) if risk_pct else self.max_position_pct
        notional = equity * fraction * self.max_leverage
        amount = notional / price

        return SizeResult(
            amount=amount,
            notional=notional,
            pct_of_equity=fraction * 100,
            method="fixed_fraction",
        )

    def kelly(
        self,
        equity: Decimal,
        price: Decimal,
        win_rate: Decimal,            # probability of winning (0-1)
        avg_win: Decimal,             # average win as multiplier (e.g. 1.02 = 2%)
        avg_loss: Decimal,            # average loss as multiplier (e.g. 0.98 = -2%)
        max_fraction: Decimal | None = None,
    ) -> SizeResult:
        """Kelly criterion position sizing.

        Formula: f* = (p * b - q) / b
        where p = win_rate, q = 1-p, b = avg_win / avg_loss

        The result is capped at max_position_pct by default.
        """
        q = Decimal("1") - win_rate
        if avg_loss == 0:
            return self.fixed_fraction(equity, price)

        b = (avg_win - Decimal("1")) / (Decimal("1") - avg_loss)
        kelly_fraction = (win_rate * b - q) / b

        # Kelly can go negative or very large — clamp
        kelly_fraction = max(Decimal("0"), min(kelly_fraction, max_fraction or self.max_position_pct))

        notional = equity * kelly_fraction * self.max_leverage
        amount = notional / price

        return SizeResult(
            amount=amount,
            notional=notional,
            pct_of_equity=kelly_fraction * 100,
            method="kelly",
        )

    def volatility_targeted(
        self,
        equity: Decimal,
        price: Decimal,
        atr: Decimal,
        target_vol_pct: Decimal = Decimal("1"),  # 1% portfolio vol per position
    ) -> SizeResult:
        """Size inversely to volatility (ATR).

        Larger position when volatility is low, smaller when high.
        amount = (equity * target_vol) / (atr * sqrt(period))
        """
        # Simplified: amount = (equity * target_vol%) / ATR
        notional = equity * (target_vol_pct / 100)
        if atr == 0:
            return self.fixed_fraction(equity, price)

        amount = notional / atr

        # Cap at max position
        max_notional = equity * self.max_position_pct
        if notional > max_notional:
            notional = max_notional
            amount = notional / price

        return SizeResult(
            amount=amount,
            notional=notional,
            pct_of_equity=(notional / equity) * 100,
            method="volatility_targeted",
        )

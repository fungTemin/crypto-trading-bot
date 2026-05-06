"""Real-time portfolio and P&L tracking.

Tracks equity, positions, realized/unrealized P&L, and fee burden.
Emits PositionUpdateEvent after each fill.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from src.core.constants import EventType, OrderSide
from src.exchange.models import Balance, Fill, Order, Position, Ticker
from src.utils.logger import TradeLogger


@dataclass
class PortfolioSnapshot:
    equity: Decimal
    balance: Decimal           # free cash
    position_value: Decimal    # total value of open positions
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    num_positions: int
    drawdown_pct: Decimal


class PortfolioTracker:
    """Tracks portfolio state and emits position updates."""

    def __init__(
        self,
        initial_equity: Decimal,
        quote_currency: str = "USDT",
        event_bus=None,
        trade_logger: Optional[TradeLogger] = None,
    ) -> None:
        self.quote_currency = quote_currency
        self.event_bus = event_bus
        self.trade_logger = trade_logger

        self._initial_equity = initial_equity
        self._peak_equity = initial_equity
        self._realized_pnl = Decimal("0")
        self._total_fees = Decimal("0")
        self._trades: list[dict] = []

        self._positions: dict[str, Position] = {}

    @property
    def realized_pnl(self) -> Decimal:
        return self._realized_pnl

    @property
    def total_fees(self) -> Decimal:
        return self._total_fees

    def on_fill(self, fill: Fill, order: Order) -> None:
        """Process a fill event, update positions and P&L."""
        self._total_fees += fill.fee

        # Track as a trade
        trade = {
            "symbol": fill.symbol,
            "side": fill.side.value,
            "price": fill.price,
            "amount": fill.amount,
            "fee": fill.fee,
            "fee_currency": fill.fee_currency,
            "order_id": fill.order_id,
            "entry_fee_rate": str(order.fee.entry_fee_rate) if order.fee else "",
            "exit_fee_rate": str(order.fee.exit_fee_rate) if order.fee else "",
            "slippage_rate": str(order.fee.slippage_rate) if order.fee else "",
            "expected_return_pct": "",  # filled from signal metadata
        }
        self._trades.append(trade)

        if self.trade_logger:
            self.trade_logger.log(**trade)

    def on_position_update(self, positions: list[Position]) -> None:
        """Update tracked positions from exchange data."""
        self._positions = {p.symbol: p for p in positions}

    def get_snapshot(self, balances: dict[str, Balance], tickers: dict[str, Ticker] | None = None) -> PortfolioSnapshot:
        """Calculate current portfolio snapshot."""
        # Balance value
        balance_value = Decimal("0")
        for asset, bal in balances.items():
            if asset == self.quote_currency:
                balance_value += bal.total
            elif tickers:
                symbol = f"{asset}/{self.quote_currency}"
                ticker = tickers.get(symbol)
                if ticker:
                    balance_value += bal.total * ticker.last

        # Position value
        position_value = Decimal("0")
        unrealized_pnl = Decimal("0")
        for pos in self._positions.values():
            position_value += pos.amount * pos.current_price
            unrealized_pnl += pos.unrealized_pnl

        equity = balance_value + position_value

        # Drawdown
        if equity > self._peak_equity:
            self._peak_equity = equity
        drawdown_pct = Decimal("0")
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - equity) / self._peak_equity * 100

        return PortfolioSnapshot(
            equity=equity,
            balance=balance_value,
            position_value=position_value,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=self._realized_pnl,
            total_fees=self._total_fees,
            num_positions=len(self._positions),
            drawdown_pct=drawdown_pct,
        )

    @property
    def trade_count(self) -> int:
        return len(self._trades)

    @property
    def fee_burden_pct(self) -> Decimal:
        """Fees as % of initial equity."""
        if self._initial_equity == 0:
            return Decimal("0")
        return self._total_fees / self._initial_equity * 100

"""Circuit breaker: halts trading when drawdown or daily loss exceeds limits.

States:
    GREEN  — normal trading, full position sizes
    YELLOW — reduced trading, half position sizes
    RED    — trading halted, all new positions rejected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal

from src.core.constants import CircuitState
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class DailyStats:
    date: date
    starting_equity: Decimal
    current_equity: Decimal
    trades: int = 0
    realized_pnl: Decimal = Decimal("0")
    fees_paid: Decimal = Decimal("0")

    @property
    def pnl_pct(self) -> Decimal:
        if self.starting_equity == 0:
            return Decimal("0")
        return (self.current_equity - self.starting_equity) / self.starting_equity * 100


class CircuitBreaker:
    """Monitors drawdown and daily loss, managing circuit state."""

    def __init__(
        self,
        initial_equity: Decimal,
        max_daily_loss_pct: Decimal = Decimal("5"),    # 5% daily loss limit
        max_drawdown_pct: Decimal = Decimal("20"),     # 20% drawdown limit
        yellow_drawdown_pct: Decimal = Decimal("10"),  # 10% drawdown = reduce
    ) -> None:
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.yellow_drawdown_pct = yellow_drawdown_pct

        self._peak_equity = initial_equity
        self._current_equity = initial_equity
        self._state = CircuitState.GREEN

        self._daily: DailyStats = DailyStats(
            date=date.today(),
            starting_equity=initial_equity,
            current_equity=initial_equity,
        )

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def current_equity(self) -> Decimal:
        return self._current_equity

    @property
    def peak_equity(self) -> Decimal:
        return self._peak_equity

    def update_equity(self, equity: Decimal) -> None:
        """Update with current portfolio equity (called periodically)."""
        self._current_equity = equity
        self._daily.current_equity = equity

        # Reset daily stats on date change
        today = date.today()
        if today != self._daily.date:
            self._daily = DailyStats(
                date=today,
                starting_equity=equity,
                current_equity=equity,
            )

        if equity > self._peak_equity:
            self._peak_equity = equity

        self._recalculate()

    def record_trade(self, pnl: Decimal, fees: Decimal) -> None:
        """Record a completed trade's P&L for daily tracking."""
        self._daily.trades += 1
        self._daily.realized_pnl += pnl
        self._daily.fees_paid += fees

    def is_allowed(self) -> bool:
        """Check if new trades are allowed in current state."""
        return self._state != CircuitState.RED

    def position_size_multiplier(self) -> Decimal:
        """Get position size multiplier for current state.

        1.0 in GREEN, 0.5 in YELLOW, 0.0 in RED.
        """
        if self._state == CircuitState.GREEN:
            return Decimal("1")
        if self._state == CircuitState.YELLOW:
            return Decimal("0.5")
        return Decimal("0")

    def _recalculate(self) -> None:
        """Re-evaluate circuit state based on current metrics."""
        # Check drawdown
        drawdown_pct = Decimal("0")
        if self._peak_equity > 0:
            drawdown_pct = (self._peak_equity - self._current_equity) / self._peak_equity * 100

        # Check daily loss
        daily_loss_pct = Decimal("0")
        if self._daily.starting_equity > 0:
            daily_loss_pct = (
                (self._daily.starting_equity - self._current_equity)
                / self._daily.starting_equity
            ) * 100

        old_state = self._state

        if drawdown_pct >= self.max_drawdown_pct or daily_loss_pct >= self.max_daily_loss_pct:
            self._state = CircuitState.RED
        elif drawdown_pct >= self.yellow_drawdown_pct:
            self._state = CircuitState.YELLOW
        else:
            self._state = CircuitState.GREEN

        if self._state != old_state:
            logger.warning(
                "Circuit breaker state change",
                extra={
                    "old": old_state.value,
                    "new": self._state.value,
                    "drawdown_pct": f"{drawdown_pct:.2f}%",
                    "daily_loss_pct": f"{daily_loss_pct:.2f}%",
                },
            )

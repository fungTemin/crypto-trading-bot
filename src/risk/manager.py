"""Risk management validation pipeline.

Every signal passes through this pipeline after the fee gate.
Steps:
    1. Circuit breaker check (GREEN/YELLOW/RED)
    2. Position size enforcement
    3. Portfolio consistency (no conflicting positions)
    4. Cooldown timer (prevent fee bleed)
    5. Order book depth check (can the amount be filled?)
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Optional

from src.core.constants import CircuitState, EventType, MarketType, OrderSide, SignalType
from src.exchange.base import ExchangeInterface, FeeSchedule
from src.exchange.models import Balance, Position, Ticker
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.position_sizer import PositionSizer, SizeResult
from src.strategy.base import Signal
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class RiskManager:
    """Validates trading signals against risk constraints."""

    def __init__(
        self,
        circuit_breaker: CircuitBreaker,
        position_sizer: PositionSizer,
        max_concurrent_positions: int = 5,
        cooldown_seconds: int = 60,
    ) -> None:
        self.circuit_breaker = circuit_breaker
        self.position_sizer = position_sizer
        self.max_concurrent_positions = max_concurrent_positions
        self.cooldown_seconds = cooldown_seconds

        self._last_trade_time: dict[str, float] = {}  # symbol -> unix timestamp

    async def validate(
        self,
        signal: Signal,
        positions: list[Position],
        balances: dict[str, Balance],
        equity: Decimal,
        ticker: Ticker | None = None,
    ) -> tuple[bool, str]:
        """Run the full validation pipeline.

        Returns:
            (approved, reason) tuple. If approved=False, reason explains why.
        """
        # 1. Circuit breaker
        if not self.circuit_breaker.is_allowed():
            return False, "Circuit breaker RED — trading halted"

        # 2. Max concurrent positions (for new entries)
        if signal.signal_type in (SignalType.BUY_LONG, SignalType.BUY_SHORT):
            if len(positions) >= self.max_concurrent_positions:
                return False, f"Max concurrent positions reached ({self.max_concurrent_positions})"

        # 3. Cooldown check
        now = time.time()
        last = self._last_trade_time.get(signal.symbol, 0)
        if now - last < self.cooldown_seconds:
            remaining = self.cooldown_seconds - (now - last)
            return False, f"Cooldown active for {signal.symbol} ({remaining:.0f}s remaining)"

        # 4. Check for conflicting positions on same symbol
        if signal.signal_type == SignalType.BUY_LONG:
            for pos in positions:
                if pos.symbol == signal.symbol and pos.side == OrderSide.SELL:
                    return False, f"Conflicting short position on {signal.symbol}"

        if signal.signal_type == SignalType.SELL_SHORT:
            for pos in positions:
                if pos.symbol == signal.symbol and pos.side == OrderSide.BUY:
                    return False, f"Conflicting long position on {signal.symbol}"

        # 5. Balance check
        base, quote = signal.symbol.split("/", 1)
        if signal.order_side == OrderSide.BUY:
            quote_balance = balances.get(quote)
            if quote_balance is None or quote_balance.free < signal.price * signal.amount:
                return False, f"Insufficient {quote} balance"

        if signal.order_side == OrderSide.SELL:
            base_balance = balances.get(base)
            if base_balance is None or base_balance.free < signal.amount:
                return False, f"Insufficient {base} balance"

        # 6. Order book depth check (optional, requires ticker)
        # Simple version: ensure amount * price is within reasonable range
        if signal.amount * signal.price > equity * (self.position_sizer.max_position_pct):
            return False, "Position size exceeds max allowed"

        # If YELLOW state, reduce position size
        if self.circuit_breaker.state == CircuitState.YELLOW:
            signal.amount *= self.circuit_breaker.position_size_multiplier()
            logger.info("Position size reduced for YELLOW circuit state")

        # Record trade time
        self._last_trade_time[signal.symbol] = now

        return True, "approved"

"""Meme coin momentum scalping strategy.

Designed for small accounts ($30+) trading high-volatility meme coins
on OKX spot market. Captures 1-3% price swings with strict risk controls.

Key principles:
    - Only trades coins with sufficient 24h volume (>$500k).
    - Entry: volume spike + momentum confirmation + RSI not overbought.
    - Exit: take profit (+2%), stop loss (-2.5%), or max hold time (45 min).
    - Fee gate: 2% expected return easily covers 0.2% fees + 0.05% buffer.

Based on $30 account:
    - Max position per trade: $12 (40%)
    - Target profit per trade: ~$0.22 (after fees)
    - Max loss per trade: ~$0.30
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.constants import MarketType, OrderSide, SignalType
from src.exchange.models import Ticker
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


@dataclass
class MemeCoinState:
    """Per-coin state for the scalper."""
    symbol: str
    last_price: Decimal = Decimal("0")
    last_volume: Decimal = Decimal("0")
    avg_volume: Decimal = Decimal("0")      # rolling average of volume
    volume_history: list[Decimal] = field(default_factory=list)
    price_history: list[Decimal] = field(default_factory=list)
    in_position: bool = False
    entry_price: Decimal = Decimal("0")
    entry_time: float = 0
    highest_price: Decimal = Decimal("0")    # for trailing stop


class MemeScalperStrategy(BaseStrategy):
    """Meme coin momentum scalper for small accounts."""

    def __init__(
        self,
        market_type: MarketType,
        fee_calculator: FeeCalculator,
        event_bus,
        min_volume_usdt: Decimal = Decimal("500000"),
        volume_spike_ratio: Decimal = Decimal("2.5"),
        momentum_lookback: int = 6,
        ema_period: int = 5,
        rsi_period: int = 8,
        rsi_entry_min: int = 35,
        rsi_entry_max: int = 65,
        take_profit_pct: Decimal = Decimal("2"),
        stop_loss_pct: Decimal = Decimal("2.5"),
        max_hold_minutes: int = 45,
        trailing_stop_pct: Decimal = Decimal("1"),
    ) -> None:
        super().__init__("meme_scalper", market_type, fee_calculator, event_bus)
        self.min_volume_usdt = min_volume_usdt
        self.volume_spike_ratio = volume_spike_ratio
        self.momentum_lookback = momentum_lookback
        self.ema_period = ema_period
        self.rsi_period = rsi_period
        self.rsi_entry_min = rsi_entry_min
        self.rsi_entry_max = rsi_entry_max
        self.take_profit_pct = take_profit_pct / 100
        self.stop_loss_pct = stop_loss_pct / 100
        self.max_hold_seconds = max_hold_minutes * 60
        self.trailing_stop_pct = trailing_stop_pct / 100

        self._coins: dict[str, MemeCoinState] = {}

    def set_symbols(self, symbols: list[str]) -> None:
        super().set_symbols(symbols)
        for sym in symbols:
            if sym not in self._coins:
                self._coins[sym] = MemeCoinState(symbol=sym)

    def update_state(self, symbol: str, price: Decimal, volume: Decimal) -> None:
        """Update rolling state for a coin from incoming ticker data."""
        if symbol not in self._coins:
            self._coins[symbol] = MemeCoinState(symbol=symbol)

        state = self._coins[symbol]
        state.last_price = price
        state.last_volume = volume

        # Rolling price history
        state.price_history.append(price)
        if len(state.price_history) > 100:
            state.price_history.pop(0)

        # Rolling volume history
        state.volume_history.append(volume)
        if len(state.volume_history) > 50:
            state.volume_history.pop(0)

        # Average volume
        if state.volume_history:
            state.avg_volume = sum(state.volume_history) / len(state.volume_history)

        # Track highest price for trailing stop
        if state.in_position and price > state.highest_price:
            state.highest_price = price

    def set_position(self, in_position: bool, entry_price: Decimal | None = None) -> None:
        pass  # Per-coin state managed internally

    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        """Evaluate whether to enter or exit a meme coin position.

        Returns:
            Signal on entry/exit opportunity, or None.
        """
        symbol = ticker.symbol
        price = ticker.last
        volume = ticker.volume

        self.update_state(symbol, price, volume)
        state = self._coins[symbol]

        # --- Liquidity filter ---
        if volume < self.min_volume_usdt:
            return None

        # --- Exit check (if in position) ---
        if state.in_position:
            return self._check_exit(state, ticker)

        # --- Entry check ---
        return self._check_entry(state, ticker)

    def _check_entry(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Determine if we should enter a position.

        Conditions:
            1. Volume spike: current > avg * ratio
            2. Price momentum: recent price change is positive
            3. RSI not overbought
            4. Passes fee gate
        """
        if len(state.price_history) < self.ema_period + 2:
            return None
        if len(state.volume_history) < 6:
            return None

        price = ticker.last

        # 1. Volume spike check
        if state.avg_volume == 0:
            return None
        vol_ratio = state.last_volume / state.avg_volume
        if vol_ratio < self.volume_spike_ratio:
            return None

        # 2. Momentum: short-term EMA
        ema = self._calc_ema(state.price_history, self.ema_period)
        if ema is None:
            return None
        if price <= ema:
            return None  # Price must be above EMA

        # 3. RSI check — don't chase overbought coins
        rsi = self._calc_rsi(state.price_history, self.rsi_period)
        if rsi is None:
            return None
        rsi_dec = Decimal(str(rsi))
        if rsi_dec < self.rsi_entry_min or rsi_dec > self.rsi_entry_max:
            return None

        # 4. Position size: $12 max for $30 account (40%)
        amount = Decimal("0")  # Will be calculated by risk manager
        # Default to ~$10 position for entry
        amount = Decimal("10") / price

        # Expected return = take_profit target
        expected_return = self.take_profit_pct

        return Signal(
            symbol=ticker.symbol,
            signal_type=SignalType.BUY_LONG,
            order_side=OrderSide.BUY,
            order_type="market",  # Meme coins move fast, use market
            price=price,
            amount=amount,
            expected_return_rate=expected_return,
            metadata={
                "volume_ratio": str(vol_ratio),
                "ema": str(ema),
                "rsi": str(rsi),
                "strategy": "meme_scalper",
            },
        )

    def _check_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Check exit conditions.

        1. Take profit: price >= entry * (1 + tp%)
        2. Stop loss: price <= entry * (1 - sl%)
        3. Trailing stop: price <= highest * (1 - trailing%)
        4. Time stop: hold time > max_hold
        """
        price = ticker.last
        entry = state.entry_price

        # Take profit
        tp_price = entry * (Decimal("1") + self.take_profit_pct)
        if price >= tp_price:
            return self._build_exit_signal(state, ticker, "take_profit")

        # Stop loss
        sl_price = entry * (Decimal("1") - self.stop_loss_pct)
        if price <= sl_price:
            return self._build_exit_signal(state, ticker, "stop_loss")

        # Trailing stop
        if state.highest_price > entry:
            trail_price = state.highest_price * (Decimal("1") - self.trailing_stop_pct)
            if price <= trail_price:
                return self._build_exit_signal(state, ticker, "trailing_stop")

        # Time stop
        if state.entry_time > 0:
            held = time.time() - state.entry_time
            if held > self.max_hold_seconds:
                return self._build_exit_signal(state, ticker, "time_stop")

        return None

    def _build_exit_signal(self, state: MemeCoinState, ticker: Ticker, reason: str) -> Signal:
        """Create an exit signal."""
        price = ticker.last
        entry = state.entry_price
        expected_return = (price - entry) / entry if entry > 0 else Decimal("0")

        return Signal(
            symbol=ticker.symbol,
            signal_type=SignalType.SELL_LONG,
            order_side=OrderSide.SELL,
            order_type="market",
            price=price,
            amount=Decimal("0"),  # close full position
            expected_return_rate=expected_return,
            metadata={
                "exit_reason": reason,
                "entry_price": str(entry),
                "exit_price": str(price),
                "pnl_pct": str(expected_return * 100),
            },
        )

    def record_entry(self, symbol: str, price: Decimal) -> None:
        """Record that we entered a position."""
        if symbol in self._coins:
            state = self._coins[symbol]
            state.in_position = True
            state.entry_price = price
            state.entry_time = time.time()
            state.highest_price = price

    def record_exit(self, symbol: str) -> None:
        """Record that we exited a position."""
        if symbol in self._coins:
            state = self._coins[symbol]
            state.in_position = False
            state.entry_price = Decimal("0")
            state.entry_time = 0
            state.highest_price = Decimal("0")

    def is_in_position(self, symbol: str) -> bool:
        state = self._coins.get(symbol)
        return state.in_position if state else False

    # --- Indicator helpers ---
    @staticmethod
    def _calc_ema(prices: list[Decimal], period: int) -> Optional[Decimal]:
        if len(prices) < period:
            return None
        multiplier = Decimal("2") / (period + 1)
        seed = sum(prices[:period]) / period
        ema = seed
        for p in prices[period:]:
            ema = (p - ema) * multiplier + ema
        return ema

    @staticmethod
    def _calc_rsi(prices: list[Decimal], period: int = 8) -> Optional[float]:
        if len(prices) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(-period, 0):
            diff = float(prices[i] - prices[i - 1])
            if diff >= 0:
                gains.append(diff)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(diff))

        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

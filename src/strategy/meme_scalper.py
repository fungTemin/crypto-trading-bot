"""Meme coin momentum scalping strategy — long + short.

Designed for small accounts ($30+) trading high-volatility meme coins
on OKX spot market. Uses kline-based volume detection for higher signal
frequency than ticker-level 24h delta comparison.

Entry signals:
    LONG:  volume spike + price > EMA + RSI between entry_min and entry_max
    SHORT: volume spike + price < EMA + RSI between entry_min and entry_max

Exit signals:
    LONG:  take_profit (+2%) / stop_loss (-2.5%) / trailing_stop (-1%) / max_hold (45 min)
    SHORT: take_profit (-2%) / stop_loss (+2.5%) / trailing_stop (+1%) / max_hold (45 min)

Fee gate: expected_return > entry_fee + exit_fee + slippage + min_profit_buffer
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
    avg_volume: Decimal = Decimal("0")
    volume_history: list[Decimal] = field(default_factory=list)
    price_history: list[Decimal] = field(default_factory=list)
    # Kline-based volume detection
    kline_volumes: list[Decimal] = field(default_factory=list)   # recent kline volumes
    kline_avg_volume: Decimal = Decimal("0")
    # Position tracking
    in_position: bool = False
    position_side: str = ""   # "long" or "short"
    entry_price: Decimal = Decimal("0")
    entry_time: float = 0
    highest_price: Decimal = Decimal("0")   # for long trailing stop
    lowest_price: Decimal = Decimal("0")    # for short trailing stop


class MemeScalperStrategy(BaseStrategy):
    """Meme coin momentum scalper — long + short with kline volume detection."""

    def __init__(
        self,
        market_type: MarketType,
        fee_calculator: FeeCalculator,
        event_bus,
        # Volume filter
        min_volume_usdt: Decimal = Decimal("500000"),
        volume_spike_ratio: Decimal = Decimal("2.5"),
        # Momentum
        momentum_lookback: int = 6,
        ema_period: int = 5,
        # RSI
        rsi_period: int = 8,
        rsi_entry_min: int = 35,    # below this = oversold (long entry zone)
        rsi_entry_max: int = 65,    # above this = overbought (short entry zone)
        rsi_short_min: int = 55,    # RSI min for short entry
        rsi_long_max: int = 65,     # RSI max for long entry
        # Exit parameters
        take_profit_pct: Decimal = Decimal("2"),
        stop_loss_pct: Decimal = Decimal("2.5"),
        max_hold_minutes: int = 45,
        trailing_stop_pct: Decimal = Decimal("1"),
        # Kline
        kline_lookback: int = 20,
    ) -> None:
        super().__init__("meme_scalper", market_type, fee_calculator, event_bus)
        self.min_volume_usdt = min_volume_usdt
        self.volume_spike_ratio = volume_spike_ratio
        self.momentum_lookback = momentum_lookback
        self.ema_period = ema_period
        self.rsi_period = rsi_period
        self.rsi_entry_min = rsi_entry_min
        self.rsi_entry_max = rsi_entry_max
        self.rsi_short_min = rsi_short_min    # RSI must be > this to consider short
        self.rsi_long_max = rsi_long_max       # RSI must be < this to consider long
        self.take_profit_pct = take_profit_pct / 100
        self.stop_loss_pct = stop_loss_pct / 100
        self.max_hold_seconds = max_hold_minutes * 60
        self.trailing_stop_pct = trailing_stop_pct / 100
        self.kline_lookback = kline_lookback

        self._coins: dict[str, MemeCoinState] = {}

    def set_symbols(self, symbols: list[str]) -> None:
        super().set_symbols(symbols)
        for sym in symbols:
            if sym not in self._coins:
                self._coins[sym] = MemeCoinState(symbol=sym)

    # ---- Update kline data (called from bot runner each tick) ----

    def update_kline_volume(self, symbol: str, volume: Decimal) -> None:
        """Feed kline candle volume into the coin state for spike detection."""
        if symbol not in self._coins:
            return
        state = self._coins[symbol]
        state.kline_volumes.append(volume)
        if len(state.kline_volumes) > self.kline_lookback:
            state.kline_volumes.pop(0)
        if state.kline_volumes:
            state.kline_avg_volume = sum(state.kline_volumes) / len(state.kline_volumes)

    def update_ticker_data(self, symbol: str, price: Decimal, volume_24h: Decimal) -> None:
        """Update rolling price history from ticker data."""
        if symbol not in self._coins:
            self._coins[symbol] = MemeCoinState(symbol=symbol)
        state = self._coins[symbol]
        state.last_price = price
        state.last_volume = volume_24h

        state.price_history.append(price)
        if len(state.price_history) > 100:
            state.price_history.pop(0)

        # Track extremes for trailing stop
        if state.in_position:
            if state.position_side == "long" and price > state.highest_price:
                state.highest_price = price
            elif state.position_side == "short":
                if state.lowest_price == Decimal("0") or price < state.lowest_price:
                    state.lowest_price = price

    # ---- Main signal computation ----

    def set_position_state(self, in_position: bool, side: str = "") -> None:
        """Called by engine for compatibility (no-op, per-coin tracking used)."""
        pass

    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        """Evaluate entry or exit for a coin.

        Checks BOTH long and short signals. Returns the best opportunity.
        Exit checks have priority over entries.
        """
        symbol = ticker.symbol
        price = ticker.last
        volume_24h = ticker.volume

        self.update_ticker_data(symbol, price, volume_24h)
        state = self._coins[symbol]

        # Liquidity filter (24h volume check)
        if volume_24h < self.min_volume_usdt:
            return None

        # ---- Exit check first (if in position) ----
        if state.in_position:
            return self._check_exit(state, ticker)

        # ---- Entry checks ----
        # Try long entry first, then short
        signal = self._check_long_entry(state, ticker)
        if signal is not None:
            return signal

        signal = self._check_short_entry(state, ticker)
        return signal

    # ============ LONG ENTRY ============

    def _check_long_entry(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Long entry: volume spike + price > EMA + RSI not overbought + passes fee gate."""
        if len(state.price_history) < self.ema_period + self.rsi_period + 2:
            return None
        if len(state.kline_volumes) < 6:
            return None

        price = ticker.last

        # Kline volume spike check (primary signal source)
        if state.kline_avg_volume == 0:
            return None
        latest_kline_vol = state.kline_volumes[-1]
        vol_ratio = latest_kline_vol / state.kline_avg_volume
        if vol_ratio < self.volume_spike_ratio:
            return None

        # Price > EMA (uptrend)
        ema = self._calc_ema(state.price_history, self.ema_period)
        if ema is None or price <= ema:
            return None

        # RSI: not overbought for long
        rsi = self._calc_rsi(state.price_history, self.rsi_period)
        if rsi is None:
            return None
        if rsi < self.rsi_entry_min or rsi > self.rsi_long_max:
            return None

        # Position size for ~$10 on $30 account (40%)
        amount = Decimal("10") / price

        return Signal(
            symbol=ticker.symbol,
            signal_type=SignalType.BUY_LONG,
            order_side=OrderSide.BUY,
            order_type="market",
            price=price,
            amount=amount,
            expected_return_rate=self.take_profit_pct,
            metadata={
                "direction": "long",
                "volume_ratio": str(vol_ratio),
                "ema": str(ema),
                "rsi": str(rsi),
                "strategy": "meme_scalper",
            },
        )

    # ============ SHORT ENTRY ============

    def _check_short_entry(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Short entry: volume spike + price < EMA + RSI not oversold + passes fee gate.

        On spot market, "short" = sell borrowed tokens. Assumes margin/spot-short available.
        For pure spot without margin, short entries are skipped.
        """
        if self.market_type == MarketType.SPOT:
            return None  # Spot trading without margin: skip shorts

        if len(state.price_history) < self.ema_period + self.rsi_period + 2:
            return None
        if len(state.kline_volumes) < 6:
            return None

        price = ticker.last

        # Kline volume spike
        if state.kline_avg_volume == 0:
            return None
        latest_kline_vol = state.kline_volumes[-1]
        vol_ratio = latest_kline_vol / state.kline_avg_volume
        if vol_ratio < self.volume_spike_ratio:
            return None

        # Price < EMA (downtrend)
        ema = self._calc_ema(state.price_history, self.ema_period)
        if ema is None or price >= ema:
            return None

        # RSI: not oversold for short (RSI should be somewhat elevated)
        rsi = self._calc_rsi(state.price_history, self.rsi_period)
        if rsi is None:
            return None
        if rsi < self.rsi_short_min or rsi > self.rsi_entry_max:
            return None

        amount = Decimal("10") / price

        return Signal(
            symbol=ticker.symbol,
            signal_type=SignalType.SELL_SHORT,
            order_side=OrderSide.SELL,
            order_type="market",
            price=price,
            amount=amount,
            expected_return_rate=self.take_profit_pct,  # 2% expected drop
            metadata={
                "direction": "short",
                "volume_ratio": str(vol_ratio),
                "ema": str(ema),
                "rsi": str(rsi),
                "strategy": "meme_scalper",
            },
        )

    # ============ EXIT LOGIC ============

    def _check_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Check exit conditions for either long or short."""
        if state.position_side == "long":
            return self._check_long_exit(state, ticker)
        elif state.position_side == "short":
            return self._check_short_exit(state, ticker)
        return None

    def _check_long_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Exit long: TP / SL / trailing stop / time stop."""
        price = ticker.last
        entry = state.entry_price

        # Take profit (price rises to target)
        tp_price = entry * (Decimal("1") + self.take_profit_pct)
        if price >= tp_price:
            return self._build_exit_signal(state, ticker, "take_profit", SignalType.SELL_LONG, OrderSide.SELL)

        # Stop loss (price drops to threshold)
        sl_price = entry * (Decimal("1") - self.stop_loss_pct)
        if price <= sl_price:
            return self._build_exit_signal(state, ticker, "stop_loss", SignalType.SELL_LONG, OrderSide.SELL)

        # Trailing stop
        if state.highest_price > entry:
            trail_price = state.highest_price * (Decimal("1") - self.trailing_stop_pct)
            if price <= trail_price:
                return self._build_exit_signal(state, ticker, "trailing_stop", SignalType.SELL_LONG, OrderSide.SELL)

        # Time stop
        if state.entry_time > 0:
            held = time.time() - state.entry_time
            if held > self.max_hold_seconds:
                return self._build_exit_signal(state, ticker, "time_stop", SignalType.SELL_LONG, OrderSide.SELL)

        return None

    def _check_short_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Exit short: TP (price drops) / SL (price rises) / trailing stop / time."""
        price = ticker.last
        entry = state.entry_price

        # Take profit (price drops to target: entry * (1 - tp%))
        tp_price = entry * (Decimal("1") - self.take_profit_pct)
        if price <= tp_price:
            return self._build_exit_signal(state, ticker, "take_profit", SignalType.BUY_SHORT, OrderSide.BUY)

        # Stop loss (price rises to threshold: entry * (1 + sl%))
        sl_price = entry * (Decimal("1") + self.stop_loss_pct)
        if price >= sl_price:
            return self._build_exit_signal(state, ticker, "stop_loss", SignalType.BUY_SHORT, OrderSide.BUY)

        # Trailing stop (for shorts: price rises from lowest point)
        if state.lowest_price > Decimal("0") and state.lowest_price < entry:
            trail_price = state.lowest_price * (Decimal("1") + self.trailing_stop_pct)
            if price >= trail_price:
                return self._build_exit_signal(state, ticker, "trailing_stop", SignalType.BUY_SHORT, OrderSide.BUY)

        # Time stop
        if state.entry_time > 0:
            held = time.time() - state.entry_time
            if held > self.max_hold_seconds:
                return self._build_exit_signal(state, ticker, "time_stop", SignalType.BUY_SHORT, OrderSide.BUY)

        return None

    def _build_exit_signal(self, state: MemeCoinState, ticker: Ticker,
                           reason: str, signal_type: SignalType,
                           order_side: OrderSide) -> Signal:
        """Create an exit signal."""
        price = ticker.last
        entry = state.entry_price

        if state.position_side == "long":
            expected_return = (price - entry) / entry if entry > 0 else Decimal("0")
        else:  # short
            expected_return = (entry - price) / entry if entry > 0 else Decimal("0")

        return Signal(
            symbol=ticker.symbol,
            signal_type=signal_type,
            order_side=order_side,
            order_type="market",
            price=price,
            amount=Decimal("0"),  # close full position
            expected_return_rate=expected_return,
            metadata={
                "exit_reason": reason,
                "direction": state.position_side,
                "entry_price": str(entry),
                "exit_price": str(price),
                "pnl_pct": str(expected_return * 100),
            },
        )

    # ---- Position tracking (called by bot runner) ----

    def record_entry(self, symbol: str, price: Decimal, side: str = "long") -> None:
        if symbol in self._coins:
            state = self._coins[symbol]
            state.in_position = True
            state.position_side = side
            state.entry_price = price
            state.entry_time = time.time()
            state.highest_price = price
            state.lowest_price = price

    def record_exit(self, symbol: str) -> None:
        if symbol in self._coins:
            state = self._coins[symbol]
            state.in_position = False
            state.position_side = ""
            state.entry_price = Decimal("0")
            state.entry_time = 0
            state.highest_price = Decimal("0")
            state.lowest_price = Decimal("0")

    def is_in_position(self, symbol: str) -> bool:
        state = self._coins.get(symbol)
        return state.in_position if state else False

    def get_position_side(self, symbol: str) -> str:
        state = self._coins.get(symbol)
        return state.position_side if state else ""

    # ---- Indicator helpers ----

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
        """RSI using percentage changes (works for sub-penny meme coin prices)."""
        if len(prices) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(-period, 0):
            prev = float(prices[i - 1])
            curr = float(prices[i])
            if prev == 0:
                continue
            pct_change = ((curr - prev) / prev) * 100
            if pct_change >= 0:
                gains.append(pct_change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(pct_change))

        if len(gains) == 0:
            return None

        avg_gain = sum(gains) / len(gains)
        avg_loss = sum(losses) / len(losses)

        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0

        rs = avg_gain / avg_loss
        return 100.0 - (100.0 / (1.0 + rs))

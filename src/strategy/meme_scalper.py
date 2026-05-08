"""Meme coin momentum scalping strategy — long + short.

Designed for small accounts ($30+) trading high-volatility meme coins.
Uses kline-based volume spike detection as the primary entry signal.

Entry signals:
    LONG:  volume spike + price > EMA(5) + RSI in [35, 55] (oversold→neutral zone)
    SHORT: volume spike + price < EMA(5) + RSI in [55, 75] (neutral→overbought zone)

Exit signals (all-or-nothing, no partial exits):
    LONG:  trailing_stop (-0.8%) / stop_loss (-2.5%) / max_hold (10 min)
    SHORT: trailing_stop (+0.8%) / stop_loss (+2.5%) / max_hold (10 min)

Fee gate: expected_return > entry_fee + exit_fee + slippage + min_profit_buffer

Key design decisions:
    - Kline volume (not 24h ticker vol) triggers entries — faster response to sudden interest.
    - RSI uses percentage-change variant (rsi_pct) to work correctly with sub-penny prices.
    - EMA is cached recursively (not recomputed from scratch) for performance.
    - Position amount is stored on entry and reused on exit to correctly close the full position.
    - Post-exit per-symbol cooldown prevents immediate re-entry after a stop-loss.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.constants import MarketType, OrderSide, SignalType
from src.data.indicator import rsi_pct
from src.exchange.models import Ticker
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator
from src.utils.logger import setup_logger

logger = setup_logger(__name__)

# Post-exit cooldown: don't re-enter the same symbol for this many seconds
# Prevents "double-tap" losses when a stopped-out position would immediately
# re-trigger on the still-forming signal that caused the stop.
EXIT_COOLDOWN_SECONDS = 120


@dataclass
class MemeCoinState:
    """Per-coin state tracked across ticks.

    Only fields that are actually read/written by the strategy are retained.
    Dead fields (avg_volume, volume_history) have been removed.
    """
    symbol: str

    # Live price data (updated from ticker each poll)
    last_price: Decimal = Decimal("0")
    last_volume: Decimal = Decimal("0")       # 24h volume from ticker (for liquidity filter)

    # Rolling price history for indicator calculation (max 100 points)
    price_history: list[Decimal] = field(default_factory=list)

    # Kline volume ring buffer for spike detection
    kline_volumes: list[Decimal] = field(default_factory=list)
    kline_avg_volume: Decimal = Decimal("0")

    # Cached indicators (incrementally updated per tick, read by UI & entry logic)
    cached_ema: Decimal = Decimal("0")
    cached_rsi: float = 50.0

    # Position tracking
    in_position: bool = False
    position_side: str = ""             # "long" or "short"
    entry_price: Decimal = Decimal("0")
    position_amount: Decimal = Decimal("0")  # base-currency amount (e.g. PEPE tokens)
    entry_time: float = 0

    # Trailing stop reference prices
    highest_price: Decimal = Decimal("0")   # long: peak since entry
    lowest_price: Decimal = Decimal("0")    # short: trough since entry

    # Post-exit cooldown (prevents immediate re-entry)
    last_exit_time: float = 0


class MemeScalperStrategy(BaseStrategy):
    """Meme coin momentum scalper — long + short with kline volume detection.

    The strategy emits one Signal per tick. Exit signals always take priority
    over entry signals so that open positions are managed before new ones.
    """

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
        # RSI — long and short use non-overlapping zones
        rsi_period: int = 8,
        rsi_long_min: int = 35,     # long: RSI must be >= this (avoid extreme oversold)
        rsi_long_max: int = 55,     # long: RSI must be <= this (stay below neutral)
        rsi_short_min: int = 55,    # short: RSI must be >= this (stay above neutral)
        rsi_short_max: int = 75,    # short: RSI must be <= this (avoid extreme overbought)
        # Exit parameters — trailing stop primary, SL as safety net, short max hold
        take_profit_pct: Decimal = Decimal("1.5"),  # kept for backward compat
        stop_loss_pct: Decimal = Decimal("2.5"),
        max_hold_minutes: int = 10,        # fast rotation: exit within 10 min
        trailing_stop_pct: Decimal = Decimal("0.8"),  # tighter trail (0.8%)
        # Kline
        kline_lookback: int = 20,
        # Position sizing (USDT notional per trade)
        base_order_usdt: Decimal = Decimal("10"),
    ) -> None:
        super().__init__("meme_scalper", market_type, fee_calculator, event_bus)
        self.base_order_usdt = base_order_usdt
        self.min_volume_usdt = min_volume_usdt
        self.volume_spike_ratio = volume_spike_ratio
        self.momentum_lookback = momentum_lookback
        self.ema_period = ema_period
        self.rsi_period = rsi_period
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max

        # Convert percentage params from human-readable to decimal form
        self.take_profit_pct = take_profit_pct / 100
        self.stop_loss_pct = stop_loss_pct / 100
        self.trailing_stop_pct = trailing_stop_pct / 100
        self.max_hold_seconds = max_hold_minutes * 60
        self.kline_lookback = kline_lookback

        self._coins: dict[str, MemeCoinState] = {}
        self.current_equity: Decimal = Decimal("0")  # Updated by bot runner each tick

    # -- Symbol registration --------------------------------------------------

    def set_symbols(self, symbols: list[str]) -> None:
        super().set_symbols(symbols)
        for sym in symbols:
            if sym not in self._coins:
                self._coins[sym] = MemeCoinState(symbol=sym)

    # -- Kline volume feed (called by bot runner) -----------------------------

    def update_kline_volume(self, symbol: str, volume: Decimal) -> None:
        """Push one kline candle volume into the ring buffer for spike detection."""
        if symbol not in self._coins:
            return
        state = self._coins[symbol]
        state.kline_volumes.append(volume)
        if len(state.kline_volumes) > self.kline_lookback:
            state.kline_volumes.pop(0)
        if state.kline_volumes:
            state.kline_avg_volume = sum(state.kline_volumes) / len(state.kline_volumes)

    # -- Ticker update + indicator caching ------------------------------------

    def update_ticker_data(self, symbol: str, price: Decimal, volume_24h: Decimal) -> None:
        """Update rolling price history and refresh cached EMA/RSI."""
        if symbol not in self._coins:
            self._coins[symbol] = MemeCoinState(symbol=symbol)
        state = self._coins[symbol]
        state.last_price = price
        state.last_volume = volume_24h

        state.price_history.append(price)
        if len(state.price_history) > 100:
            state.price_history.pop(0)

        self._update_cached_indicators(state)

        # Track extremes for trailing stop while in position
        if state.in_position:
            if state.position_side == "long" and price > state.highest_price:
                state.highest_price = price
            elif state.position_side == "short":
                if state.lowest_price == Decimal("0") or price < state.lowest_price:
                    state.lowest_price = price

    def _update_cached_indicators(self, state: MemeCoinState) -> None:
        """Recursively update EMA and recompute RSI (avoids O(n) per tick)."""
        prices = state.price_history
        if len(prices) >= self.ema_period:
            price = prices[-1]
            alpha = Decimal("2") / (self.ema_period + 1)
            if state.cached_ema > 0:
                # Recursive update: EMA = alpha * price + (1-alpha) * EMA_prev
                state.cached_ema = (price - state.cached_ema) * alpha + state.cached_ema
            else:
                # Seed with SMA on first calculation
                state.cached_ema = sum(prices[-self.ema_period:]) / self.ema_period

        if len(prices) >= self.rsi_period + 1:
            rsi_val = rsi_pct(prices, self.rsi_period)
            if rsi_val is not None:
                state.cached_rsi = rsi_val

    # -- Main signal entry point ----------------------------------------------

    def set_position_state(self, in_position: bool, side: str = "") -> None:
        """No-op: per-coin tracking is used instead of global state."""
        pass

    async def compute_signal(self, ticker: Ticker) -> Optional[Signal]:
        """Evaluate entry or exit for one coin.

        Exit checks always run first (managing open positions takes priority).
        """
        symbol = ticker.symbol
        price = ticker.last
        volume_24h = ticker.volume

        # Price sanity filter: skip extreme low-price coins (e.g. BABYDOGE at 0)
        if price < Decimal("1e-8"):
            return None

        self.update_ticker_data(symbol, price, volume_24h)
        state = self._coins[symbol]

        # Liquidity filter: skip coins below minimum 24h volume
        if volume_24h < self.min_volume_usdt:
            return None

        # Exit check takes priority over entry
        if state.in_position:
            return self._check_exit(state, ticker)

        # Entry: try long first, then short
        signal = self._check_long_entry(state, ticker)
        if signal is not None:
            return signal

        return self._check_short_entry(state, ticker)

    # ======================= LONG ENTRY ======================================

    def _check_long_entry(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Long entry when: volume spikes + price above EMA + RSI in [35, 55]."""
        if len(state.price_history) < self.ema_period + self.rsi_period + 2:
            return None
        if len(state.kline_volumes) < 6:
            return None

        # Post-exit cooldown: prevent immediate re-entry
        if state.last_exit_time > 0:
            if time.time() - state.last_exit_time < EXIT_COOLDOWN_SECONDS:
                return None

        price = ticker.last

        # Volume spike gate — use max of last candle and recent completed average.
        # In live mode the last candle is incomplete (low vol), so the completed
        # average catches real spikes. In backtesting all candles are complete.
        if state.kline_avg_volume == 0:
            return None
        if len(state.kline_volumes) < 7:
            return None
        last_vol = state.kline_volumes[-1]
        completed_vols = state.kline_volumes[-4:-1]  # 3 candles before the forming one
        completed_avg = sum(completed_vols) / max(len(completed_vols), 1)
        effective_vol = max(last_vol, completed_avg)
        if effective_vol <= 0:
            return None
        vol_ratio = effective_vol / state.kline_avg_volume
        if vol_ratio < self.volume_spike_ratio:
            return None

        # Trend filter: price must be above EMA(5)
        if state.cached_ema == 0 or price <= state.cached_ema:
            return None

        # RSI zone: [rsi_long_min, rsi_long_max] — oversold to neutral
        rsi = state.cached_rsi
        if rsi < self.rsi_long_min or rsi > self.rsi_long_max:
            return None

        # Expected return: distance from current price to take-profit target,
        # capped at take_profit_pct (don't over-promise)
        tp_price = price * (Decimal("1") + self.take_profit_pct)
        expected_return = (tp_price - price) / price

        # Dynamic position sizing: 25% of current equity per trade
        if self.current_equity > 0:
            order_usdt = self.current_equity * Decimal("0.25")
        else:
            order_usdt = self.base_order_usdt
        amount = order_usdt / price

        return Signal(
            symbol=ticker.symbol,
            signal_type=SignalType.BUY_LONG,
            order_side=OrderSide.BUY,
            order_type="market",
            price=price,
            amount=amount,
            expected_return_rate=expected_return,
            metadata={
                "direction": "long",
                "volume_ratio": str(vol_ratio),
                "ema": str(state.cached_ema),
                "rsi": str(rsi),
                "entry_amount": str(amount),
                "strategy": "meme_scalper",
            },
        )

    # ======================= SHORT ENTRY =====================================

    def _check_short_entry(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Short entry when: volume spikes + price below EMA + RSI in [55, 75].

        Only available in futures mode. Spot market skips short entries.
        """
        if self.market_type == MarketType.SPOT:
            return None

        if len(state.price_history) < self.ema_period + self.rsi_period + 2:
            return None
        if len(state.kline_volumes) < 6:
            return None

        # Post-exit cooldown
        if state.last_exit_time > 0:
            if time.time() - state.last_exit_time < EXIT_COOLDOWN_SECONDS:
                return None

        price = ticker.last

        # Volume spike gate
        if state.kline_avg_volume == 0:
            return None
        latest_kline_vol = state.kline_volumes[-1]
        vol_ratio = latest_kline_vol / state.kline_avg_volume
        if vol_ratio < self.volume_spike_ratio:
            return None

        # Trend filter: price must be below EMA(5) for short
        if state.cached_ema == 0 or price >= state.cached_ema:
            return None

        # RSI zone: [rsi_short_min, rsi_short_max] — neutral to overbought
        rsi = state.cached_rsi
        if rsi < self.rsi_short_min or rsi > self.rsi_short_max:
            return None

        # Expected return: distance from current price to take-profit target
        tp_price = price * (Decimal("1") - self.take_profit_pct)
        expected_return = (price - tp_price) / price

        # Dynamic position sizing: 25% of current equity per trade
        if self.current_equity > 0:
            order_usdt = self.current_equity * Decimal("0.25")
        else:
            order_usdt = self.base_order_usdt
        amount = order_usdt / price

        return Signal(
            symbol=ticker.symbol,
            signal_type=SignalType.SELL_SHORT,
            order_side=OrderSide.SELL,
            order_type="market",
            price=price,
            amount=amount,
            expected_return_rate=expected_return,
            metadata={
                "direction": "short",
                "volume_ratio": str(vol_ratio),
                "ema": str(state.cached_ema),
                "rsi": str(rsi),
                "entry_amount": str(amount),
                "strategy": "meme_scalper",
            },
        )

    # ======================= EXIT LOGIC ======================================

    def _check_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Route to long or short exit check based on position side."""
        if state.position_side == "long":
            return self._check_long_exit(state, ticker)
        elif state.position_side == "short":
            return self._check_short_exit(state, ticker)
        return None

    def _check_long_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Exit long: trailing_stop (primary) / stop_loss (safety) / time_stop (deadline).

        Trailing stop activates immediately from entry price, not just above it.
        This captures small upward moves instead of waiting for a fixed TP target.
        """
        price = ticker.last
        entry = state.entry_price

        # 1. Stop loss — hard safety net at -2.5%
        sl_price = entry * (Decimal("1") - self.stop_loss_pct)
        if price <= sl_price:
            return self._build_exit_signal(state, ticker, "stop_loss", SignalType.SELL_LONG, OrderSide.SELL)

        # 2. Trailing stop — activates from highest price (starts at entry on fill)
        # After entry, if price rises at all, trail kicks in at 0.8% below peak.
        # If price never rises above entry, exit via time_stop at 10 min.
        if state.highest_price >= entry:
            trail_price = state.highest_price * (Decimal("1") - self.trailing_stop_pct)
            if price <= trail_price:
                # If highest > entry, we locked some profit → trailing_stop exit
                reason = "trailing_stop" if state.highest_price > entry else "time_stop"
                return self._build_exit_signal(state, ticker, reason, SignalType.SELL_LONG, OrderSide.SELL)

        # 3. Time stop — max hold expired
        if state.entry_time > 0:
            held = time.time() - state.entry_time
            if held > self.max_hold_seconds:
                return self._build_exit_signal(state, ticker, "time_stop", SignalType.SELL_LONG, OrderSide.SELL)

        return None

    def _check_short_exit(self, state: MemeCoinState, ticker: Ticker) -> Optional[Signal]:
        """Exit short: trailing_stop (primary) / stop_loss (safety) / time_stop."""
        price = ticker.last
        entry = state.entry_price

        # 1. Stop loss — price rose 2.5%
        sl_price = entry * (Decimal("1") + self.stop_loss_pct)
        if price >= sl_price:
            return self._build_exit_signal(state, ticker, "stop_loss", SignalType.BUY_SHORT, OrderSide.BUY)

        # 2. Trailing stop — from lowest price (starts at entry)
        if state.lowest_price > Decimal("0") and state.lowest_price <= entry:
            trail_price = state.lowest_price * (Decimal("1") + self.trailing_stop_pct)
            if price >= trail_price:
                reason = "trailing_stop" if state.lowest_price < entry else "time_stop"
                return self._build_exit_signal(state, ticker, reason, SignalType.BUY_SHORT, OrderSide.BUY)

        # 3. Time stop
        if state.entry_time > 0:
            held = time.time() - state.entry_time
            if held > self.max_hold_seconds:
                return self._build_exit_signal(state, ticker, "time_stop", SignalType.BUY_SHORT, OrderSide.BUY)

        return None

    def _build_exit_signal(self, state: MemeCoinState, ticker: Ticker,
                           reason: str, signal_type: SignalType,
                           order_side: OrderSide) -> Signal:
        """Create an exit signal that closes the FULL position.

        Uses the stored position_amount from entry to ensure the entire
        position is closed (fixes the amount=0 bug).
        """
        price = ticker.last
        entry = state.entry_price
        amount = state.position_amount  # full close — not zero!

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
            amount=amount,
            expected_return_rate=expected_return,
            metadata={
                "exit_reason": reason,
                "direction": state.position_side,
                "entry_price": str(entry),
                "exit_price": str(price),
                "pnl_pct": str(expected_return * 100),
            },
        )

    # -- Position tracking (called by bot runner) -----------------------------

    def record_entry(self, symbol: str, price: Decimal, amount: Decimal = Decimal("0"),
                     side: str = "long") -> None:
        """Record that a position was opened. Stores amount for full close on exit."""
        if symbol in self._coins:
            state = self._coins[symbol]
            state.in_position = True
            state.position_side = side
            state.entry_price = price
            state.position_amount = amount
            state.entry_time = time.time()
            state.highest_price = price
            state.lowest_price = price

    def record_exit(self, symbol: str) -> None:
        """Record position close and start post-exit cooldown."""
        if symbol in self._coins:
            state = self._coins[symbol]
            state.in_position = False
            state.position_side = ""
            state.entry_price = Decimal("0")
            state.position_amount = Decimal("0")
            state.entry_time = 0
            state.highest_price = Decimal("0")
            state.lowest_price = Decimal("0")
            state.last_exit_time = time.time()  # cooldown for re-entry prevention

    def is_in_position(self, symbol: str) -> bool:
        state = self._coins.get(symbol)
        return state.in_position if state else False

    def get_position_side(self, symbol: str) -> str:
        state = self._coins.get(symbol)
        return state.position_side if state else ""

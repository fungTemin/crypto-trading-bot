from __future__ import annotations

from enum import Enum


class OrderSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    STOP_LIMIT = "stop_limit"


class OrderStatus(str, Enum):
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class MarketType(str, Enum):
    SPOT = "spot"
    FUTURE = "future"
    SWAP = "swap"


class CircuitState(str, Enum):
    GREEN = "green"    # normal trading
    YELLOW = "yellow"  # reduced position sizes
    RED = "red"        # halt all trading


class SignalType(str, Enum):
    BUY_LONG = "buy_long"
    SELL_LONG = "sell_long"
    BUY_SHORT = "buy_short"
    SELL_SHORT = "sell_short"


class EventType(str, Enum):
    TICKER = "ticker"
    SIGNAL = "signal"
    ORDER = "order"
    FILL = "fill"
    POSITION_UPDATE = "position_update"
    ERROR = "error"
    SHUTDOWN = "shutdown"

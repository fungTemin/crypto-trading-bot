"""In-memory rolling OHLCV store for real-time indicator calculation.

Maintains a fixed-size rolling window of candles per symbol+timeframe.
New candles are appended, old candles evicted.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.data.indicator import compute_all


class Candle:
    __slots__ = ("timestamp", "open", "high", "low", "close", "volume")

    def __init__(
        self,
        timestamp: datetime,
        open_: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
    ) -> None:
        self.timestamp = timestamp
        self.open = open_
        self.high = high
        self.low = low
        self.close = close
        self.volume = volume

    @classmethod
    def from_ccxt(cls, data: dict) -> Candle:
        return cls(
            timestamp=datetime.fromtimestamp(data["timestamp"] / 1000, tz=timezone.utc),
            open_=Decimal(str(data["open"])),
            high=Decimal(str(data["high"])),
            low=Decimal(str(data["low"])),
            close=Decimal(str(data["close"])),
            volume=Decimal(str(data["volume"])),
        )


class OHLCVStore:
    """Rolling window OHLCV store.

    Each symbol+timeframe combination holds up to max_candles candles.
    """

    def __init__(self, max_candles: int = 200) -> None:
        self.max_candles = max_candles
        self._stores: dict[str, list[Candle]] = defaultdict(list)

    def _key(self, symbol: str, timeframe: str) -> str:
        return f"{symbol}:{timeframe}"

    def add(self, symbol: str, timeframe: str, candle: Candle) -> None:
        key = self._key(symbol, timeframe)
        store = self._stores[key]

        # Replace last candle if same timestamp (update in-place)
        if store and store[-1].timestamp == candle.timestamp:
            store[-1] = candle
        else:
            store.append(candle)
            if len(store) > self.max_candles:
                store.pop(0)

    def add_bulk(self, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        for c in candles:
            self.add(symbol, timeframe, c)

    def get(self, symbol: str, timeframe: str) -> list[Candle]:
        key = self._key(symbol, timeframe)
        return self._stores.get(key, [])

    @property
    def closes(self) -> list[Decimal]:
        return [c.close for c in self._stores.get(self._active_key, [])]

    def calc_indicators(
        self,
        symbol: str,
        timeframe: str,
        bb_period: int = 20,
        bb_std: float = 2.0,
        rsi_period: int = 14,
        atr_period: int = 14,
    ) -> dict:
        """Calculate indicators from stored OHLCV data."""
        candles = self.get(symbol, timeframe)
        if len(candles) < max(bb_period, rsi_period, atr_period) + 1:
            return {}

        closes = [c.close for c in candles]
        highs = [c.high for c in candles]
        lows = [c.low for c in candles]

        return compute_all(
            closes=closes,
            highs=highs,
            lows=lows,
            bb_period=bb_period,
            bb_std=bb_std,
            rsi_period=rsi_period,
            atr_period=atr_period,
        )

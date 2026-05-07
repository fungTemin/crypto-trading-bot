"""Synthetic ticker data feed for offline simulation / backtesting."""

from __future__ import annotations

import random
import time as time_module
from datetime import datetime, timezone
from decimal import Decimal

from src.exchange.models import Ticker


class SyntheticTickerFeed:
    """Generates realistic random-walk price data with occasional pump events."""

    def __init__(self, symbols: list[str], profiles: dict, seed: int = 42) -> None:
        self.symbols = symbols
        rng = random.Random(seed)
        self._states: dict[str, dict] = {}
        for sym in symbols:
            profile = profiles.get(sym, {})
            self._states[sym] = {
                "price": float(profile.get("base_price", 0.001)),
                "base_vol": profile.get("base_volume", 500_000),
                "volatility": profile.get("volatility", 0.015),
                "rng": random.Random(rng.randint(1, 10000)),
                "pump_chance": 0.10,
                "pump_active": False,
                "pump_strength": 0,
                "pump_remaining": 0,
            }

    async def fetch_ticker(self, symbol: str) -> Ticker:
        state = self._states[symbol]
        rng = state["rng"]
        vol_pct = state["volatility"]
        if state["pump_active"]:
            vol_pct *= 2
            state["pump_remaining"] -= 1
            if state["pump_remaining"] <= 0:
                state["pump_active"] = False

        change = rng.gauss(0, vol_pct)
        state["price"] = max(state["price"] * (1 + change), 1e-12)
        price = Decimal(str(state["price"]))

        if not state["pump_active"] and rng.random() < state["pump_chance"]:
            state["pump_active"] = True
            state["pump_strength"] = rng.uniform(2.0, 6.0)
            state["pump_remaining"] = rng.randint(3, 8)

        if state["pump_active"]:
            volume = Decimal(str(int(state["base_vol"] * state["pump_strength"])))
        else:
            volume = Decimal(str(int(state["base_vol"] * rng.uniform(0.3, 1.7))))

        spread = price * Decimal("0.001")
        return Ticker(
            symbol=symbol, bid=price - spread / 2, ask=price + spread / 2,
            last=price,
            high=price * (Decimal("1") + Decimal(str(abs(rng.gauss(0, vol_pct))))),
            low=price * (Decimal("1") - Decimal(str(abs(rng.gauss(0, vol_pct))))),
            volume=volume,
            timestamp=datetime.now(timezone.utc),
        )

    async def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 30) -> list:
        """Generate synthetic kline candles so volume spike detection works offline.

        Each candle's volume is derived from the same volatility/pump state
        used by fetch_ticker, so volume spikes and price moves are correlated.
        """
        if symbol not in self._states:
            return []
        state = self._states[symbol]
        rng = state["rng"]
        base_vol = state["base_vol"]
        klines = []
        # Walk back from current price to build realistic-looking candles
        price = state["price"]
        for i in range(limit):
            change = rng.gauss(0, state["volatility"])
            close = price * (1 + change)
            open_p = price
            high = max(open_p, close) * (1 + abs(rng.gauss(0, state["volatility"] * 0.5)))
            low = min(open_p, close) * (1 - abs(rng.gauss(0, state["volatility"] * 0.5)))
            # Volume: occasionally inject a spike (correlated with larger moves)
            vol_mult = rng.uniform(0.3, 1.7)
            if abs(change) > state["volatility"] * 1.5:
                vol_mult = rng.uniform(3.0, 8.0)  # spike on big moves
            vol = int(base_vol * vol_mult)
            # CCXT OHLCV format: [timestamp, open, high, low, close, volume]
            ts = int(time_module.time() * 1000) - (limit - i) * 60_000  # 1m candles
            klines.append([ts, open_p, high, low, close, vol])
            price = close
        return klines

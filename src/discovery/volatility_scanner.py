"""Volatility scanner — dynamically discovers high-volatility coins from OKX.

Replaces the hardcoded symbol list with a market-driven approach.
Re-scans periodically to chase momentum as market conditions shift.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional


class VolatilityScanner:
    """Scans OKX futures market for top-N coins by volatility × volume score.

    The score formula weights 24h price range and volume logarithmically:
        score = abs(change_pct) * volume^0.3

    This favors coins with both large price moves AND sufficient liquidity,
    while preventing ultra-low-volume coins from dominating.
    """

    def __init__(
        self,
        min_volume_usdt: float = 100_000,   # minimum 24h USD volume
        top_n: int = 30,                     # return top N coins
        quote: str = "USDT",
    ) -> None:
        self.min_volume_usdt = min_volume_usdt
        self.top_n = top_n
        self.quote = quote
        self._profiles: dict[str, dict] = {}  # cached profiles for synthetic feed

    async def scan(self, exchange) -> list[str]:
        """Fetch all USDT tickers from exchange, score by volatility, return top-N symbols.

        Args:
            exchange: A ccxt async exchange instance (e.g. ccxt_async.okx()).

        Returns:
            List of symbol strings like ["PEPE/USDT", "WIF/USDT", ...].
        """
        try:
            tickers = await exchange.fetch_tickers()
        except Exception:
            return []  # fallback to hardcoded list

        scored = []
        for symbol, t in tickers.items():
            if not isinstance(symbol, str) or not symbol.endswith(f"/{self.quote}"):
                continue
            # Skip non-standard symbols (options, perpetuals with : suffixes)
            if ":" in symbol:
                continue

            vol = float(t.get("baseVolume") or t.get("volume") or 0)
            if vol < self.min_volume_usdt:
                continue

            # Percentage change over 24h
            change = abs(float(t.get("percentage") or t.get("change") or 0))
            price = float(t.get("last") or 0)
            if price <= 0:
                continue

            # Score: change% weighted by volume factor
            score = change * (vol ** 0.3)

            scored.append((
                symbol,
                score,
                {
                    "base_price": price,
                    "volatility": min(max(change / 100, 0.005), 0.05),
                    "base_volume": int(vol),
                },
            ))

        # Sort by score descending, take top N
        scored.sort(key=lambda x: x[1], reverse=True)

        self._profiles = {}
        symbols = []
        for symbol, score, profile in scored[:self.top_n]:
            symbols.append(symbol)
            self._profiles[symbol] = profile

        return symbols

    @property
    def profiles(self) -> dict:
        """Return cached profiles for the last scan (used by SyntheticTickerFeed)."""
        return self._profiles

    def get_static_fallback(self) -> list[str]:
        """Fallback symbol list when exchange is unreachable."""
        return [
            "PEPE/USDT", "FLOKI/USDT", "WIF/USDT", "BONK/USDT",
            "SHIB/USDT", "DOGE/USDT", "DOGS/USDT", "NOT/USDT",
            "HMSTR/USDT", "TURBO/USDT", "MEW/USDT", "BOME/USDT",
            "NEIRO/USDT", "BABYDOGE/USDT", "CAT/USDT", "DUCK/USDT",
            "JTO/USDT", "STORJ/USDT", "CFG/USDT", "CATI/USDT",
            "VIRTUAL/USDT", "MAJOR/USDT", "ICP/USDT", "BIO/USDT",
            "VINE/USDT", "NEAR/USDT", "OP/USDT", "ENA/USDT",
            "STRK/USDT", "ONDO/USDT",
        ]

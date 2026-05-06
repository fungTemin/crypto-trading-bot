from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd

from src.data.ohlcv_store import Candle


class DataLoader:
    """Loads historical OHLCV data from CSV/Parquet files.

    Supported formats:
        - CSV: timestamp,open,high,low,close,volume (ccxt-style)
        - Parquet: same schema

    Timestamps can be epoch milliseconds or ISO 8601 strings.
    """

    SUPPORTED_EXTENSIONS = {".csv", ".parquet", ".pq"}

    def __init__(self, data_dir: str | Path = "data/historical") -> None:
        self.data_dir = Path(data_dir)

    def list_available(self) -> dict[str, list[str]]:
        """List available symbol/timeframe combinations.

        Returns: {symbol: [timeframes]} dict.
        """
        available: dict[str, list[str]] = {}
        for f in self.data_dir.glob("*"):
            if f.suffix not in self.SUPPORTED_EXTENSIONS:
                continue
            # Expected naming: BTC_USDT-5m.csv or BTC_USDT_5m.csv
            stem = f.stem
            parts = stem.replace("-", "_").split("_")
            if len(parts) >= 2:
                symbol = f"{parts[0]}/{parts[1]}"
                timeframe = parts[2] if len(parts) > 2 else "5m"
                available.setdefault(symbol, []).append(timeframe)
        return available

    def load(
        self,
        symbol: str,
        timeframe: str = "5m",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> list[Candle]:
        """Load OHLCV data from file.

        Args:
            symbol: Trading pair, e.g. "BTC/USDT".
            timeframe: Candle timeframe.
            start_date: ISO date string for start of range (inclusive).
            end_date: ISO date string for end of range (inclusive).

        Returns list of Candle sorted by timestamp ascending.
        """
        filepath = self._find_file(symbol, timeframe)
        if filepath is None:
            raise FileNotFoundError(
                f"No data file for {symbol} {timeframe} in {self.data_dir}"
            )

        df = self._read_file(filepath)
        df = df.sort_values("timestamp")

        # Filter by date range
        if "datetime" not in df.columns:
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)

        if start_date:
            df = df[df["datetime"] >= pd.Timestamp(start_date, tz="UTC")]
        if end_date:
            df = df[df["datetime"] <= pd.Timestamp(end_date, tz="UTC")]

        candles: list[Candle] = []
        for _, row in df.iterrows():
            ts = row["datetime"].to_pydatetime()
            candles.append(Candle(
                timestamp=ts,
                open_=Decimal(str(row["open"])),
                high=Decimal(str(row["high"])),
                low=Decimal(str(row["low"])),
                close=Decimal(str(row["close"])),
                volume=Decimal(str(row["volume"])),
            ))

        return candles

    def _find_file(self, symbol: str, timeframe: str) -> Optional[Path]:
        safe_symbol = symbol.replace("/", "_")
        patterns = [
            f"{safe_symbol}-{timeframe}",
            f"{safe_symbol}_{timeframe}",
            f"{safe_symbol}",  # fallback: no timeframe in name
        ]
        for ext in self.SUPPORTED_EXTENSIONS:
            for pattern in patterns:
                path = self.data_dir / f"{pattern}{ext}"
                if path.exists():
                    return path
        return None

    def _read_file(self, path: Path) -> pd.DataFrame:
        if path.suffix == ".csv":
            return pd.read_csv(path)
        return pd.read_parquet(path)

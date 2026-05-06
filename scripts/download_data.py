#!/usr/bin/env python3
"""Download historical OHLCV data from exchange for backtesting.

Usage:
    source /Users/zhifeng.zhou/Documents/python/venv/bin/activate
    python scripts/download_data.py --symbol BTC/USDT --timeframe 5m --days 180
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ccxt.async_support as ccxt_async


async def download_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since_ms: int,
    output_dir: str,
    limit: int = 500,
) -> str:
    """Download OHLCV data and save to CSV."""
    exchange = getattr(ccxt_async, exchange_id)({"enableRateLimit": True})

    try:
        all_candles = []
        current_since = since_ms

        while True:
            candles = await exchange.fetch_ohlcv(symbol, timeframe, since=current_since, limit=limit)
            if not candles:
                break

            all_candles.extend(candles)
            last_ts = candles[-1][0]
            current_since = last_ts + 1

            print(f"  Downloaded {len(all_candles)} candles... latest: {datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc).isoformat()}")

            if len(candles) < limit:
                break

    finally:
        await exchange.close()

    # Save to CSV
    os.makedirs(output_dir, exist_ok=True)
    safe_symbol = symbol.replace("/", "_")
    filename = f"{safe_symbol}-{timeframe}.csv"
    filepath = os.path.join(output_dir, filename)

    with open(filepath, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "open", "high", "low", "close", "volume"])
        for candle in all_candles:
            writer.writerow(candle)

    print(f"Saved {len(all_candles)} candles to {filepath}")
    return filepath


def main() -> None:
    parser = argparse.ArgumentParser(description="Download historical OHLCV data")
    parser.add_argument("--exchange", default="binance", help="Exchange ID (ccxt)")
    parser.add_argument("--symbol", default="BTC/USDT", help="Trading pair")
    parser.add_argument("--timeframe", default="5m", help="Candle timeframe")
    parser.add_argument("--days", type=int, default=180, help="Days of history to download")
    parser.add_argument("--output", default="data/historical", help="Output directory")

    args = parser.parse_args()

    since_ms = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp() * 1000)

    print(f"Downloading {args.symbol} {args.timeframe} from {args.exchange}")
    print(f"Period: {args.days} days ({datetime.fromtimestamp(since_ms / 1000, tz=timezone.utc).isoformat()} to now)")

    asyncio.run(download_ohlcv(
        exchange_id=args.exchange,
        symbol=args.symbol,
        timeframe=args.timeframe,
        since_ms=since_ms,
        output_dir=args.output,
    ))


if __name__ == "__main__":
    main()

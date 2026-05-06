#!/usr/bin/env python3
"""Crypto Trading Bot — Entry Point.

Usage:
    source /Users/zhifeng.zhou/Documents/python/venv/bin/activate
    python -m src.main                    # paper trading (default)
    TRADING_MODE=paper python -m src.main # paper trading
    TRADING_MODE=live python -m src.main  # live trading
"""

from __future__ import annotations

import asyncio
import signal
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

from src.config.loader import load_config
from src.core.engine import TradingEngine
from src.utils.logger import setup_logger

logger = setup_logger("crypto_bot")
console = Console()


def print_startup_info(config) -> None:
    """Display startup banner with config summary."""
    table = Table(title="Crypto Trading Bot — Starting Up")
    table.add_column("Setting", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Exchange", config.exchange.id)
    table.add_row("Market Type", config.market.type)
    table.add_row("Symbols", ", ".join(config.market.symbols))
    table.add_row("Timeframe", config.market.timeframe)
    table.add_row("Strategy", config.strategy.active)
    table.add_row("Min Profit Buffer", f"{config.strategy.min_profit_buffer_pct}%")
    table.add_row("Max Position %", f"{config.risk.max_position_pct}%")
    table.add_row("Max Daily Loss", f"{config.risk.max_daily_loss_pct}%")
    table.add_row("Max Drawdown", f"{config.risk.max_drawdown_pct}%")
    table.add_row("Sandbox", str(config.exchange.sandbox))

    console.print(table)


async def run_bot(engine: TradingEngine) -> None:
    """Run the trading bot with graceful shutdown."""
    loop = asyncio.get_event_loop()
    shutdown_event = asyncio.Event()

    def shutdown_handler(sig=None, frame=None):
        logger.info(f"Received signal {sig}, shutting down...")
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda s=sig: shutdown_handler(s, None))
        except NotImplementedError:
            signal.signal(sig, shutdown_handler)

    await engine.start()

    try:
        await shutdown_event.wait()
    finally:
        await engine.stop()
        console.print("\n[bold red]Bot stopped.[/bold red]")


def main() -> None:
    """Main entry point."""
    import os

    config_path = Path(os.getenv("CONFIG_PATH", "config"))
    mode = os.getenv("TRADING_MODE", "paper")

    try:
        config = load_config(config_dir=config_path, mode=mode)
    except Exception as e:
        console.print(f"[bold red]Failed to load config: {e}[/bold red]")
        sys.exit(1)

    print_startup_info(config)

    engine = TradingEngine(config)

    try:
        asyncio.run(run_bot(engine))
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user[/yellow]")
    except Exception as e:
        logger.exception("Fatal error")
        console.print(f"[bold red]Fatal error: {e}[/bold red]")
        sys.exit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Test paper trading with 30 USD initial capital.

Usage:
    python scripts/test_paper.py
"""

from __future__ import annotations

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.table import Table

from src.config.loader import load_config
from src.exchange.paper_exchange import PaperExchange
from src.exchange.models import Ticker
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.mean_reversion import MeanReversionStrategy
from src.core.constants import MarketType
from src.core.event_bus import EventBus

console = Console()


async def test_paper_trading():
    """Run a simple paper trading test."""
    console.print("\n[bold cyan]=== OKX Paper Trading Test (30 USD) ===[/bold cyan]\n")

    # Load config directly (avoid encoding issues)
    import yaml
    config_path = Path("config/okx_micro_30usd.yaml")
    with open(config_path, encoding="utf-8") as f:
        config_data = yaml.safe_load(f)

    # Also load default config for missing fields
    default_path = Path("config/default.yaml")
    with open(default_path, encoding="utf-8") as f:
        default_data = yaml.safe_load(f)

    # Merge configs
    from src.config.loader import _deep_merge
    merged = _deep_merge(default_data, config_data)

    from src.config.schema import AppConfig
    config = AppConfig(**merged)

    console.print(f"[green]Config loaded:[/green]")
    console.print(f"  Exchange: {config.exchange.id}")
    console.print(f"  Strategy: {config.strategy.active}")
    console.print(f"  Initial Capital: {config.backtest.initial_capital} USDT")
    console.print(f"  Max Position: {config.risk.max_position_pct}%")
    console.print()

    # Create paper exchange with 30 USDT
    initial_capital = Decimal("30")  # Fixed 30 USD
    exchange = PaperExchange(
        initial_balances={"USDT": initial_capital},
        fee_schedule=FeeSchedule(
            maker=Decimal("0.001"),
            taker=Decimal("0.001"),
        ),
    )

    # Display initial balance
    balances = await exchange.fetch_balance()
    console.print("[bold]Initial Balances:[/bold]")
    for asset, bal in balances.items():
        console.print(f"  {asset}: {bal.free} (free) + {bal.locked} (locked) = {bal.total}")
    console.print()

    # Set up fee calculator
    fee_calc = FeeCalculator(
        fee_schedule=FeeSchedule.spot(),
        min_profit_buffer=Decimal(str(config.strategy.min_profit_buffer_pct / 100)),
    )

    # Create event bus
    event_bus = EventBus()

    # Create strategy
    strategy = MeanReversionStrategy(
        market_type=MarketType.SPOT,
        fee_calculator=fee_calc,
        event_bus=event_bus,
        bb_period=config.strategy.mean_reversion.bb_period,
        bb_std=config.strategy.mean_reversion.bb_std,
        rsi_period=config.strategy.mean_reversion.rsi_period,
        rsi_oversold=config.strategy.mean_reversion.rsi_oversold,
        rsi_overbought=config.strategy.mean_reversion.rsi_overbought,
        confirmation_candles=config.strategy.mean_reversion.confirmation_candles,
    )

    # Simulate meme coin ticker data (DOGE)
    test_tickers = [
        Ticker(symbol="DOGE/USDT", bid=Decimal("0.149"), ask=Decimal("0.151"), last=Decimal("0.150"),
               high=Decimal("0.155"), low=Decimal("0.145"), volume=Decimal("50000000")),
        Ticker(symbol="DOGE/USDT", bid=Decimal("0.139"), ask=Decimal("0.141"), last=Decimal("0.140"),
               high=Decimal("0.150"), low=Decimal("0.138"), volume=Decimal("60000000")),
        Ticker(symbol="DOGE/USDT", bid=Decimal("0.129"), ask=Decimal("0.131"), last=Decimal("0.130"),
               high=Decimal("0.140"), low=Decimal("0.128"), volume=Decimal("75000000")),
        Ticker(symbol="DOGE/USDT", bid=Decimal("0.139"), ask=Decimal("0.141"), last=Decimal("0.140"),
               high=Decimal("0.142"), low=Decimal("0.138"), volume=Decimal("55000000")),
        Ticker(symbol="DOGE/USDT", bid=Decimal("0.149"), ask=Decimal("0.151"), last=Decimal("0.150"),
               high=Decimal("0.152"), low=Decimal("0.148"), volume=Decimal("45000000")),
    ]

    # Set oversold state for mean reversion signal
    # RSI below 30 triggers buy signal
    strategy.set_state(
        sma=Decimal("100000"),
        upper_band=Decimal("105000"),
        lower_band=Decimal("95000"),
        rsi=Decimal("20"),  # Very oversold - will trigger buy
    )

    console.print("[bold]Running Trading Simulation...[/bold]\n")

    # Process tickers
    for i, ticker in enumerate(test_tickers):
        console.print(f"[cyan]Step {i+1}:[/cyan] {ticker.symbol} @ {ticker.last} USDT")

        # Set ticker in exchange
        exchange.set_ticker(ticker)

        # Get signal from strategy
        signal = await strategy.on_ticker(ticker)

        if signal:
            console.print(f"  [yellow]Signal: {signal.signal_type.value} {signal.amount:.6f} BTC @ {signal.price}[/yellow]")

            # Execute order
            order = await exchange.create_order(
                symbol=signal.symbol,
                side=signal.order_side.value,
                order_type=signal.order_type,
                amount=signal.amount,
                price=signal.price,
            )

            if order.status.value == "closed":
                console.print(f"  [green]Order filled: {order.id}[/green]")
            else:
                console.print(f"  [red]Order status: {order.status.value}[/red]")
        else:
            console.print("  [dim]No signal[/dim]")

        # Show current balance
        balances = await exchange.fetch_balance()
        usdt_balance = balances.get("USDT")
        usdt_free = usdt_balance.free if usdt_balance else Decimal("0")
        btc_balance = balances.get("BTC")
        btc_free = btc_balance.free if btc_balance else Decimal("0")
        console.print(f"  Balance: {usdt_free:.2f} USDT + {btc_free:.6f} BTC")

        # Calculate equity
        equity = exchange.get_equity("USDT")
        console.print(f"  Equity: {equity:.2f} USDT")
        console.print()

    # Final summary
    console.print("\n[bold cyan]=== Final Summary ===[/bold cyan]\n")

    final_balances = await exchange.fetch_balance()
    table = Table(title="Final Balances")
    table.add_column("Asset", style="cyan")
    table.add_column("Free", style="green")
    table.add_column("Locked", style="yellow")
    table.add_column("Total", style="bold")

    for asset, bal in final_balances.items():
        table.add_row(asset, str(bal.free), str(bal.locked), str(bal.total))

    console.print(table)

    final_equity = exchange.get_equity("USDT")
    initial_equity = initial_capital
    pnl = final_equity - initial_equity
    pnl_pct = (pnl / initial_equity) * 100

    console.print(f"\n[bold]Initial Capital:[/bold] {initial_equity:.2f} USDT")
    console.print(f"[bold]Final Equity:[/bold] {final_equity:.2f} USDT")

    if pnl >= 0:
        console.print(f"[bold green]P&L: +{pnl:.2f} USDT (+{pnl_pct:.2f}%)[/bold green]")
    else:
        console.print(f"[bold red]P&L: {pnl:.2f} USDT ({pnl_pct:.2f}%)[/bold red]")

    console.print(f"[bold]Total Fees:[/bold] {exchange.total_fees_paid:.4f} USDT")
    console.print(f"[bold]Total Trades:[/bold] {len(exchange.fills)}")

    # Log to operation logger
    from src.utils.operation_logger import OperationLogger
    op_logger = OperationLogger("data/logs/paper_test_operations.jsonl")
    op_logger.log(
        step="paper_test",
        action="Completed paper trading test",
        details={
            "initial_capital": float(initial_equity),
            "final_equity": float(final_equity),
            "pnl": float(pnl),
            "pnl_pct": float(pnl_pct),
            "total_fees": float(exchange.total_fees_paid),
            "total_trades": len(exchange.fills),
        },
    )

    console.print("\n[green]Operations logged to: data/logs/paper_test_operations.jsonl[/green]")
    console.print("[green]Trade journal: data/logs/okx_micro_trades.csv[/green]")


if __name__ == "__main__":
    asyncio.run(test_paper_trading())

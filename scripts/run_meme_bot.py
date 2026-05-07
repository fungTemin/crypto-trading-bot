#!/usr/bin/env python3
"""Meme Coin Spot Trading Bot — OKX small account ($30+).

Scans meme coins on OKX for volume + momentum breakouts,
enters with market orders, exits on take-profit/stop-loss/trailing-stop.

Usage:
    source /Users/zhifeng.zhou/Documents/python/venv/bin/activate

    # Paper trading first (recommended)
    python scripts/run_meme_bot.py

    # Paper with different capital
    python scripts/run_meme_bot.py --capital 30

    # Live trading (requires OKX API keys in .env)
    python scripts/run_meme_bot.py --live

Supported meme coins: PEPE, FLOKI, WIF, BONK, MEME, SHIB, DOGE
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel

from src.core.constants import MarketType
from src.core.event_bus import EventBus
from src.exchange.ccxt_exchange import CCXTExchange
from src.exchange.paper_exchange import PaperExchange
from src.exchange.models import Ticker
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.meme_scalper import MemeScalperStrategy
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.utils.logger import TradeLogger, setup_logger

logger = setup_logger("meme_bot")
console = Console()

MEME_SYMBOLS = [
    "PEPE/USDT",
    "FLOKI/USDT",
    "WIF/USDT",
    "BONK/USDT",
    "MEME/USDT",
    "SHIB/USDT",
    "DOGE/USDT",
]

MODE_PAPER = "paper"
MODE_LIVE = "live"


def create_exchange(mode: str, capital: Decimal) -> tuple:
    """Create exchange instance based on mode."""
    if mode == MODE_PAPER:
        exchange = PaperExchange(
            fee_schedule=None,
            initial_balances={"USDT": capital},
        )
        # Set fee schedule for spot
        from src.exchange.base import FeeSchedule as ExFeeSchedule
        exchange._fee_schedule = ExFeeSchedule(maker=Decimal("0.001"), taker=Decimal("0.001"))
        return exchange, capital

    api_key = os.getenv("OKX_API_KEY", "")
    secret = os.getenv("OKX_SECRET", "")
    password = os.getenv("OKX_PASSWORD", "")

    if not api_key:
        console.print("[red]OKX_API_KEY not set in .env[/red]")
        sys.exit(1)

    exchange = CCXTExchange(
        exchange_id="okx",
        api_key=api_key,
        secret=secret,
        password=password,
        sandbox=False,
        market_type=MarketType.SPOT,
    )
    return exchange, capital


def status_table(bot: MemeBot) -> Table:
    """Build rich status table."""
    table = Table(title=f"Meme Bot — {bot.mode.upper()} Mode")
    table.add_column("Coin", style="cyan")
    table.add_column("Price", style="white")
    table.add_column("Vol Spike", style="yellow")
    table.add_column("RSI", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("P&L", style="bold")

    for sym in MEME_SYMBOLS:
        state = bot.strategy._coins.get(sym)
        if state and state.last_price > 0:
            vol_spike = "—"
            if state.avg_volume > 0:
                ratio = float(state.last_volume / state.avg_volume)
                vol_spike = f"{ratio:.1f}x"
            rsi_str = "—"
            if len(state.price_history) >= 9:
                rsi_val = bot.strategy._calc_rsi(state.price_history, 8)
                if rsi_val is not None:
                    rsi_str = f"{rsi_val:.0f}"
            status = "[green]LONG[/green]" if state.in_position else "—"
            pnl = "—"
            if state.in_position and state.entry_price > 0:
                pnl_pct = float((state.last_price - state.entry_price) / state.entry_price * 100)
                color = "green" if pnl_pct >= 0 else "red"
                pnl = f"[{color}]{pnl_pct:+.2f}%[/{color}]"
            table.add_row(sym, f"{float(state.last_price):.8f}", vol_spike, rsi_str, status, pnl)

    # Footer
    equity = bot.exchange.get_equity("USDT")
    pnl_total = equity - bot.starting_capital
    pnl_pct = float(pnl_total / bot.starting_capital * 100) if bot.starting_capital > 0 else 0
    color = "green" if pnl_total >= 0 else "red"
    table.caption = (
        f"Equity: ${float(equity):.2f} | "
        f"P&L: [{color}]${float(pnl_total):.2f} ({pnl_pct:+.2f}%)[/{color}] | "
        f"Trades: {len(bot.trades)}"
    )
    return table


class MemeBot:
    """Meme coin spot trading bot."""

    def __init__(self, mode: str = MODE_PAPER, capital: Decimal = Decimal("30")) -> None:
        self.mode = mode
        self.starting_capital = capital
        self.trades: list[dict] = []

        self.exchange, _ = create_exchange(mode, capital)

        # Fee calculator
        fee_schedule = FeeSchedule.spot()
        self.fee_calculator = FeeCalculator(
            fee_schedule=fee_schedule,
            min_profit_buffer=Decimal("0.0005"),
        )

        # Event bus
        self.event_bus = EventBus()

        # Strategy
        self.strategy = MemeScalperStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=self.fee_calculator,
            event_bus=self.event_bus,
            min_volume_usdt=Decimal("500000"),
            volume_spike_ratio=Decimal("2.5"),
            momentum_lookback=6,
            ema_period=5,
            rsi_period=8,
            rsi_entry_min=35,
            rsi_entry_max=65,
            take_profit_pct=Decimal("2"),
            stop_loss_pct=Decimal("2.5"),
            max_hold_minutes=45,
            trailing_stop_pct=Decimal("1"),
        )
        self.strategy.set_symbols(MEME_SYMBOLS)

        # Risk
        self.circuit_breaker = CircuitBreaker(
            initial_equity=capital,
            max_daily_loss_pct=Decimal("15"),
            max_drawdown_pct=Decimal("25"),
        )
        self.risk_manager = RiskManager(
            circuit_breaker=self.circuit_breaker,
            position_sizer=PositionSizer(max_position_pct=Decimal("40")),
            max_concurrent_positions=3,
            cooldown_seconds=30,
        )

        # Trade logger
        self.trade_logger = TradeLogger("data/logs/meme_trades.csv")

        self._running = False
        # Live data source for paper mode
        self._live_source: CCXTExchange | None = None

    async def start(self) -> None:
        self._running = True

        if self.mode == MODE_PAPER:
            # Paper mode: use OKX public API for real prices, simulate fills
            console.print("[yellow]Paper trading mode — no real money used[/yellow]")
            self._live_source = CCXTExchange(
                exchange_id="okx",
                sandbox=False,
                market_type=MarketType.SPOT,
            )
        else:
            console.print("[bold red]LIVE trading mode — real money at risk![/bold red]")

        console.print(f"Capital: ${float(self.starting_capital):.2f}")
        console.print(f"Symbols: {', '.join(MEME_SYMBOLS)}")
        console.print("Press Ctrl+C to stop\n")

        with Live(status_table(self), refresh_per_second=2, console=console) as live:
            while self._running:
                try:
                    await self._tick(live)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Tick error", extra={"error": str(e)})
                    await asyncio.sleep(5)

    async def _tick(self, live: Live) -> None:
        """One round of scanning all meme coins."""
        source = self._live_source if self.mode == MODE_PAPER else self.exchange

        for symbol in MEME_SYMBOLS:
            if not self._running:
                break

            try:
                ticker = await source.fetch_ticker(symbol)

                # Update paper exchange ticker
                if self.mode == MODE_PAPER:
                    self.exchange.set_ticker(ticker)

                # Check exit first (if in position)
                signal = await self.strategy.on_ticker(ticker)

                if signal:
                    await self._handle_signal(signal)

            except Exception as e:
                logger.warning("Ticker fetch failed", extra={"symbol": symbol, "error": str(e)})
                continue

            # Small delay between symbols to avoid rate limit
            await asyncio.sleep(0.3)

        live.update(status_table(self))

    async def _handle_signal(self, signal: Signal) -> None:
        """Process a trading signal."""
        is_buy = signal.order_side == OrderSide.BUY

        # Check if already in position for this symbol
        if is_buy and self.strategy.is_in_position(signal.symbol):
            return

        # Risk validation
        positions = await self.exchange.fetch_positions()
        balances = await self.exchange.fetch_balance()
        equity = self.exchange.get_equity("USDT")

        approved, reason = await self.risk_manager.validate(
            signal=signal,
            positions=positions,
            balances=balances,
            equity=equity,
        )

        if not approved:
            logger.debug("Signal rejected", extra={"symbol": signal.symbol, "reason": reason})
            return

        # Execute order
        order = await self.exchange.create_order(
            symbol=signal.symbol,
            side=signal.order_side.value,
            order_type=signal.order_type,
            amount=signal.amount if signal.amount > 0 else Decimal("0"),
            price=signal.price if signal.order_type == "limit" else None,
        )

        if order.status.value == "rejected":
            return

        # Update strategy position tracking
        if is_buy:
            self.strategy.record_entry(signal.symbol, signal.price)
        else:
            self.strategy.record_exit(signal.symbol)

        # Log trade
        pnl_str = signal.metadata.get("pnl_pct", "")
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.symbol,
            "side": signal.order_side.value,
            "price": str(signal.price),
            "amount": str(signal.amount) if is_buy else "close",
            "expected_return_pct": f"{signal.expected_return_rate * 100:.2f}",
            "pnl_pct": pnl_str,
            "exit_reason": signal.metadata.get("exit_reason", "entry"),
        }
        self.trades.append(trade)
        self.trade_logger.log(**trade)

        # Update circuit breaker
        equity = self.exchange.get_equity("USDT")
        self.circuit_breaker.update_equity(equity)

        logger.info(
            "Trade executed",
            extra=trade,
        )

    async def stop(self) -> None:
        self._running = False
        if self._live_source:
            await self._live_source.close()
        await self.exchange.close()

        # Print summary
        equity = self.exchange.get_equity("USDT")
        pnl = equity - self.starting_capital
        pnl_pct = float(pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0
        color = "green" if pnl >= 0 else "red"

        console.print(f"\n[bold]Session Summary[/bold]")
        console.print(f"  Trades: {len(self.trades)}")
        console.print(f"  Starting: ${float(self.starting_capital):.2f}")
        console.print(f"  Final: ${float(equity):.2f}")
        console.print(f"  P&L: [{color}]${float(pnl):.2f} ({pnl_pct:+.2f}%)[/{color}]")
        console.print(f"  Fees: ${float(self.exchange.total_fees_paid):.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meme Coin Spot Trading Bot — OKX")
    parser.add_argument("--live", action="store_true", help="Enable live trading (default: paper)")
    parser.add_argument("--capital", type=float, default=30, help="Starting capital in USDT (default: 30)")
    args = parser.parse_args()

    mode = MODE_LIVE if args.live else MODE_PAPER
    capital = Decimal(str(args.capital))

    bot = MemeBot(mode=mode, capital=capital)

    loop = asyncio.get_event_loop()
    shutdown = asyncio.Event()

    def sig_handler(sig=None, frame=None):
        console.print("\n[yellow]Shutting down...[/yellow]")
        shutdown.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, lambda: sig_handler(s, None))
        except NotImplementedError:
            signal.signal(s, sig_handler)

    async def run() -> None:
        task = asyncio.create_task(bot.start())
        await shutdown.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()

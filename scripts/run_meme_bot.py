#!/usr/bin/env python3
"""Meme Coin Spot Trading Bot — $30+ small account.

Scans meme coins for volume + momentum breakouts,
enters with market orders, exits on take-profit/stop-loss/trailing-stop.

Usage:
    source /Users/zhifeng.zhou/Documents/python/venv/bin/activate

    # Offline paper trading (synthetic data, no API needed)
    python scripts/run_meme_bot.py --offline

    # Paper trading with real OKX data (requires VPN/proxy in China)
    python scripts/run_meme_bot.py

    # Paper with proxy
    python scripts/run_meme_bot.py --proxy socks5://127.0.0.1:7890

    # Live trading (requires OKX API keys in .env)
    python scripts/run_meme_bot.py --live

Supported meme coins: PEPE, FLOKI, WIF, BONK, MEME, SHIB, DOGE
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
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

from src.core.constants import MarketType
from src.core.event_bus import EventBus
from src.exchange.models import Ticker
from src.exchange.paper_exchange import PaperExchange
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.meme_scalper import MemeScalperStrategy
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.utils.logger import TradeLogger, setup_logger

logger = setup_logger("meme_bot", level="INFO", fmt="text")
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

# Meme coin typical price ranges and characteristics for synthetic data
MEME_PROFILES = {
    "PEPE/USDT":  {"base_price": Decimal("0.00001000"), "volatility": 0.015, "base_volume": 2_000_000},
    "FLOKI/USDT": {"base_price": Decimal("0.00010000"), "volatility": 0.012, "base_volume": 800_000},
    "WIF/USDT":   {"base_price": Decimal("0.50000000"), "volatility": 0.018, "base_volume": 3_000_000},
    "BONK/USDT":  {"base_price": Decimal("0.00002000"), "volatility": 0.014, "base_volume": 1_500_000},
    "MEME/USDT":  {"base_price": Decimal("0.01500000"), "volatility": 0.016, "base_volume": 600_000},
    "SHIB/USDT":  {"base_price": Decimal("0.00002000"), "volatility": 0.010, "base_volume": 5_000_000},
    "DOGE/USDT":  {"base_price": Decimal("0.15000000"), "volatility": 0.008, "base_volume": 10_000_000},
}


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
            if state.avg_volume > 0 and state.last_volume > 0:
                ratio = float(state.last_volume / state.avg_volume)
                vol_spike = f"{ratio:.1f}x"
            rsi_str = "—"
            if len(state.price_history) >= 9:
                rsi_val = bot.strategy._calc_rsi(state.price_history, 8)
                if rsi_val is not None:
                    rsi_str = f"{rsi_val:.0f}"
            status_str = "[green]LONG[/green]" if state.in_position else "—"
            pnl = "—"
            if state.in_position and state.entry_price > 0:
                pnl_pct = float((state.last_price - state.entry_price) / state.entry_price * 100)
                color = "green" if pnl_pct >= 0 else "red"
                pnl = f"[{color}]{pnl_pct:+.2f}%[/{color}]"
            table.add_row(sym, f"{float(state.last_price):.8f}", vol_spike, rsi_str, status_str, pnl)

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


class SyntheticTickerFeed:
    """Generates realistic meme coin ticker data for offline testing.

    Simulates price walks with random volatility, occasional volume spikes,
    and meme-coin-like behavior (sudden pumps, mean reversion).
    """

    def __init__(self, symbols: list[str], seed: int = 42) -> None:
        self.symbols = symbols
        rng = random.Random(seed)
        self._states: dict[str, dict] = {}
        for sym in symbols:
            profile = MEME_PROFILES.get(sym, {"base_price": Decimal("0.001"), "volatility": 0.01, "base_volume": 500_000})
            self._states[sym] = {
                "price": float(profile["base_price"]),
                "base_vol": profile["base_volume"],
                "volatility": profile["volatility"],
                "rng": random.Random(rng.randint(1, 10000)),
                "pump_chance": 0.10,   # 10% chance per tick of a volume spike (offline demo)
                "pump_active": False,
                "pump_strength": 0,
                "pump_remaining": 0,
            }

    async def fetch_ticker(self, symbol: str) -> Ticker:
        """Generate the next synthetic ticker."""
        state = self._states[symbol]
        rng = state["rng"]

        # Price random walk with occasional pump
        vol_pct = state["volatility"]
        if state["pump_active"]:
            vol_pct *= 2
            state["pump_remaining"] -= 1
            if state["pump_remaining"] <= 0:
                state["pump_active"] = False

        change = rng.gauss(0, vol_pct)
        state["price"] = state["price"] * (1 + change)
        if state["price"] <= 0:
            state["price"] = float(MEME_PROFILES[symbol]["base_price"])

        price = Decimal(str(state["price"]))

        # Volume: occasional spike
        if not state["pump_active"] and rng.random() < state["pump_chance"]:
            state["pump_active"] = True
            state["pump_strength"] = rng.uniform(2.0, 6.0)
            state["pump_remaining"] = rng.randint(3, 8)

        if state["pump_active"]:
            volume = Decimal(str(int(state["base_vol"] * state["pump_strength"])))
        else:
            volume = Decimal(str(int(state["base_vol"] * rng.uniform(0.5, 1.5))))

        spread = price * Decimal("0.001")
        return Ticker(
            symbol=symbol,
            bid=price - spread / 2,
            ask=price + spread / 2,
            last=price,
            high=price * (Decimal("1") + Decimal(str(abs(rng.gauss(0, vol_pct))))),
            low=price * (Decimal("1") - Decimal(str(abs(rng.gauss(0, vol_pct))))),
            volume=volume,
            timestamp=datetime.now(timezone.utc),
        )


class MemeBot:
    """Meme coin spot trading bot."""

    MODE_OFFLINE = "offline"
    MODE_PAPER = "paper"
    MODE_LIVE = "live"

    def __init__(
        self,
        mode: str = MODE_OFFLINE,
        capital: Decimal = Decimal("30"),
        proxy: str | None = None,
    ) -> None:
        self.mode = mode
        self.starting_capital = capital
        self.trades: list[dict] = []
        self.proxy = proxy

        # Exchange: PaperExchange for all modes (live mode wraps it)
        self.exchange = PaperExchange(
            initial_balances={"USDT": capital},
        )
        from src.exchange.base import FeeSchedule as ExFeeSchedule
        self.exchange._fee_schedule = ExFeeSchedule(maker=Decimal("0.001"), taker=Decimal("0.001"))

        # Fee calculator
        fee_schedule = FeeSchedule.spot()
        self.fee_calculator = FeeCalculator(
            fee_schedule=fee_schedule,
            min_profit_buffer=Decimal("0.0005"),
        )

        # Event bus
        self.event_bus = EventBus()

        # Strategy params — conservative for real trading
        # 实盘参数：使用成交量变化率（每次ticker查询间的差值）来检测突破
        # 24h 滚动成交量变化慢，使用较低阈值
        vol_ratio = Decimal("1.5")
        min_vol = Decimal("200000")

        self.strategy = MemeScalperStrategy(
            market_type=MarketType.SPOT,
            fee_calculator=self.fee_calculator,
            event_bus=self.event_bus,
            min_volume_usdt=min_vol,
            volume_spike_ratio=vol_ratio,
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
        self._data_feed = None  # Real API or synthetic
        self._prev_volumes: dict[str, Decimal] = {}  # for volume delta calc

    async def start(self) -> None:
        self._running = True

        if self.mode == self.MODE_OFFLINE:
            console.print("[cyan]Offline simulation mode — synthetic meme coin data[/cyan]")
            self._data_feed = SyntheticTickerFeed(MEME_SYMBOLS, seed=random.randint(1, 9999))

        elif self.mode == self.MODE_PAPER:
            console.print("[yellow]Paper trading mode — OKX real prices, simulated fills[/yellow]")
            self._data_feed = await self._setup_real_feed()
            if self._data_feed is None:
                console.print("[red]Cannot connect to OKX. Trying offline mode...[/red]")
                self.mode = self.MODE_OFFLINE
                self._data_feed = SyntheticTickerFeed(MEME_SYMBOLS)
                console.print("[cyan]Falling back to offline simulation.[/cyan]")

        elif self.mode == self.MODE_LIVE:
            console.print("[bold red]LIVE trading mode — real OKX API, real money![/bold red]")
            self._data_feed = await self._setup_real_feed()
            if self._data_feed is None:
                console.print("[red]Cannot connect to OKX. Aborting live mode.[/red]")
                return

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
                    await asyncio.sleep(1)

    async def _setup_real_feed(self):
        """Try to set up real OKX feed, returns None if unavailable."""
        try:
            import ccxt.async_support as ccxt_async
            opts: dict = {"enableRateLimit": True, "timeout": 15000}
            if self.proxy:
                opts["socksProxy"] = self.proxy  # ccxt uses socksProxy directly
            exchange = ccxt_async.okx(opts)
            # Test connection
            ticker = await exchange.fetch_ticker("PEPE/USDT")
            console.print(f"[green]OKX connected: PEPE/USDT = {ticker.get('last')}[/green]")
            return exchange
        except Exception as e:
            logger.warning(f"OKX connection failed: {e}")
            return None

    async def _tick(self, live: Live) -> None:
        """One round of scanning all meme coins."""
        for symbol in MEME_SYMBOLS:
            if not self._running:
                break
            try:
                raw = await self._data_feed.fetch_ticker(symbol)

                # 24h rolling volume
                current_vol = Decimal(str(raw.get('baseVolume', 0) or 0))
                # Volume delta: change since last tick (proxy for incoming volume)
                vol_delta = current_vol
                prev_vol = self._prev_volumes.get(symbol)
                if prev_vol is not None and prev_vol > 0:
                    vol_delta = current_vol - prev_vol
                    if vol_delta < 0:
                        vol_delta = current_vol  # reset if negative (unlikely)
                self._prev_volumes[symbol] = current_vol

                # Convert ccxt ticker to our Ticker model
                ticker = Ticker(
                    symbol=symbol,
                    bid=Decimal(str(raw.get('bid', 0) or 0)),
                    ask=Decimal(str(raw.get('ask', 0) or 0)),
                    last=Decimal(str(raw.get('last', 0) or 0)),
                    high=Decimal(str(raw.get('high', 0) or 0)),
                    low=Decimal(str(raw.get('low', 0) or 0)),
                    volume=vol_delta,
                )
                self.exchange.set_ticker(ticker)

                signal = await self.strategy.on_ticker(ticker)
                if signal:
                    await self._handle_signal(signal)
            except Exception as e:
                logger.debug("Ticker error for %s: %s", symbol, str(e)[:100])
                continue
            await asyncio.sleep(0.1)

        live.update(status_table(self))

    async def _handle_signal(self, signal) -> None:
        """Process a trading signal."""
        from src.core.constants import OrderSide
        is_buy = signal.order_side == OrderSide.BUY

        if is_buy and self.strategy.is_in_position(signal.symbol):
            return

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
            return

        order = await self.exchange.create_order(
            symbol=signal.symbol,
            side=signal.order_side.value,
            order_type=signal.order_type,
            amount=signal.amount if signal.amount > 0 else Decimal("0"),
            price=signal.price if signal.order_type == "limit" else None,
        )

        if order.status.value == "rejected":
            return

        if is_buy:
            self.strategy.record_entry(signal.symbol, signal.price)
        else:
            self.strategy.record_exit(signal.symbol)

        pnl_str = signal.metadata.get("pnl_pct", "")
        reason_str = signal.metadata.get("exit_reason", "entry")
        trade = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.symbol,
            "side": signal.order_side.value,
            "price": str(signal.price),
            "expected_return_pct": f"{float(signal.expected_return_rate * 100):.2f}",
            "pnl_pct": pnl_str,
            "exit_reason": reason_str,
        }
        self.trades.append(trade)
        self.trade_logger.log(**trade)

        equity = self.exchange.get_equity("USDT")
        self.circuit_breaker.update_equity(equity)

        logger.info(
            "Trade: %s %s @ %s - %s",
            signal.order_side.value.upper(),
            signal.symbol,
            signal.price,
            reason_str,
        )

    async def stop(self) -> None:
        self._running = False
        if self._data_feed and hasattr(self._data_feed, "close"):
            try:
                await self._data_feed.close()
            except Exception:
                pass
        await self.exchange.close()

        # Print summary
        equity = self.exchange.get_equity("USDT")
        pnl = equity - self.starting_capital
        pnl_pct = float(pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0
        color = "green" if pnl >= 0 else "red"

        console.print(f"\n[bold]Session Summary[/bold]")
        console.print(f"  Mode: {self.mode}")
        console.print(f"  Trades: {len(self.trades)}")
        console.print(f"  Starting: ${float(self.starting_capital):.2f}")
        console.print(f"  Final: ${float(equity):.2f}")
        console.print(f"  P&L: [{color}]${float(pnl):.2f} ({pnl_pct:+.2f}%)[/{color}]")
        console.print(f"  Fees: ${float(self.exchange.total_fees_paid):.4f}")

        # Trade breakdown
        if self.trades:
            buys = [t for t in self.trades if t["side"] == "buy"]
            sells = [t for t in self.trades if t["side"] == "sell"]
            console.print(f"  Entry trades: {len(buys)}")
            console.print(f"  Exit trades: {len(sells)} (TP/stop/trailing/time)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meme Coin Spot Trading Bot")
    parser.add_argument("--offline", action="store_true", help="Offline simulation (default, no API needed)")
    parser.add_argument("--paper", action="store_true", help="Paper trading with real OKX prices")
    parser.add_argument("--live", action="store_true", help="Live trading with OKX (needs API keys)")
    parser.add_argument("--capital", type=float, default=30, help="Starting capital in USDT")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy URL (socks5://127.0.0.1:7890)")
    args = parser.parse_args()

    if args.live:
        mode = MemeBot.MODE_LIVE
    elif args.paper:
        mode = MemeBot.MODE_PAPER
    else:
        mode = MemeBot.MODE_OFFLINE

    capital = Decimal(str(args.capital))
    bot = MemeBot(mode=mode, capital=capital, proxy=args.proxy)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    shutdown = asyncio.Event()

    def sig_handler(sig=None, frame=None):
        console.print("\n[yellow]Shutting down...[/yellow]")
        shutdown.set()

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, sig_handler)
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

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

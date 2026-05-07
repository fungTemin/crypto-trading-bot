#!/usr/bin/env python3
"""Meme Coin Spot Trading Bot — $30+ small account.

Usage:
    source /Users/zhifeng.zhou/Documents/python/venv/bin/activate
    python scripts/run_meme_bot.py --offline              # synthetic data
    python scripts/run_meme_bot.py --paper --proxy socks5://127.0.0.1:7890  # real OKX
    python scripts/run_meme_bot.py --live --proxy socks5://127.0.0.1:7890   # real money
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import signal
import sys
import time as time_module
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.layout import Layout

from src.core.constants import MarketType, OrderSide
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
    "PEPE/USDT", "FLOKI/USDT", "WIF/USDT", "BONK/USDT",
    "MEME/USDT", "SHIB/USDT", "DOGE/USDT",
]

MEME_PROFILES = {
    "PEPE/USDT":  {"base_price": Decimal("0.00001000"), "volatility": 0.015, "base_volume": 2_000_000},
    "FLOKI/USDT": {"base_price": Decimal("0.00010000"), "volatility": 0.012, "base_volume": 800_000},
    "WIF/USDT":   {"base_price": Decimal("0.50000000"), "volatility": 0.018, "base_volume": 3_000_000},
    "BONK/USDT":  {"base_price": Decimal("0.00002000"), "volatility": 0.014, "base_volume": 1_500_000},
    "MEME/USDT":  {"base_price": Decimal("0.01500000"), "volatility": 0.016, "base_volume": 600_000},
    "SHIB/USDT":  {"base_price": Decimal("0.00002000"), "volatility": 0.010, "base_volume": 5_000_000},
    "DOGE/USDT":  {"base_price": Decimal("0.15000000"), "volatility": 0.008, "base_volume": 10_000_000},
}


def format_price(p: Decimal) -> str:
    """Format price for display, handling sub-penny meme coins."""
    return f"{float(p):.8f}".rstrip('0').rstrip('.')


def format_pnl(value: Decimal, pct: Decimal) -> str:
    """Colored P&L string."""
    color = "green" if value >= 0 else "red"
    return f"[{color}]${float(value):+.4f} ({float(pct):+.2f}%)[/{color}]"


# ---- Synthetic Data Feed (offline mode) ----

class SyntheticTickerFeed:
    def __init__(self, symbols: list[str], seed: int = 42) -> None:
        self.symbols = symbols
        rng = random.Random(seed)
        self._states: dict[str, dict] = {}
        for sym in symbols:
            profile = MEME_PROFILES[sym]
            self._states[sym] = {
                "price": float(profile["base_price"]),
                "base_vol": profile["base_volume"],
                "volatility": profile["volatility"],
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


# ---- Meme Bot ----

class MemeBot:
    MODE_OFFLINE = "offline"
    MODE_PAPER = "paper"
    MODE_LIVE = "live"

    def __init__(self, mode: str = MODE_OFFLINE, capital: Decimal = Decimal("30"),
                 proxy: str | None = None) -> None:
        self.mode = mode
        self.starting_capital = capital
        self.trades: list[dict] = []      # trade history log
        self.open_positions: dict[str, dict] = {}  # symbol -> entry info
        self.proxy = proxy

        self.exchange = PaperExchange(initial_balances={"USDT": capital})
        from src.exchange.base import FeeSchedule as ExFS
        self.exchange._fee_schedule = ExFS(maker=Decimal("0.001"), taker=Decimal("0.001"))

        fs = FeeSchedule.spot()
        self.fee_calculator = FeeCalculator(fee_schedule=fs, min_profit_buffer=Decimal("0.0005"))
        self.event_bus = EventBus()

        self.strategy = MemeScalperStrategy(
            market_type=MarketType.SPOT, fee_calculator=self.fee_calculator,
            event_bus=self.event_bus,
            min_volume_usdt=Decimal("200000"), volume_spike_ratio=Decimal("1.5"),
            momentum_lookback=6, ema_period=5, rsi_period=8,
            rsi_entry_min=35, rsi_entry_max=65,
            take_profit_pct=Decimal("2"), stop_loss_pct=Decimal("2.5"),
            max_hold_minutes=45, trailing_stop_pct=Decimal("1"),
        )
        self.strategy.set_symbols(MEME_SYMBOLS)

        self.circuit_breaker = CircuitBreaker(
            initial_equity=capital, max_daily_loss_pct=Decimal("15"),
            max_drawdown_pct=Decimal("25"),
        )
        self.risk_manager = RiskManager(
            circuit_breaker=self.circuit_breaker,
            position_sizer=PositionSizer(max_position_pct=Decimal("40")),
            max_concurrent_positions=3, cooldown_seconds=30,
        )
        self.trade_logger = TradeLogger("data/logs/meme_trades.csv")

        self._session_start = None
        self._running = False
        self._data_feed = None
        self._prev_volumes: dict[str, Decimal] = {}

    # ---- Build UI ----

    def build_layout(self) -> Layout:
        """Build the rich layout: header, positions table, trade log."""
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="body"),
        )
        layout["body"].split_column(
            Layout(name="positions", ratio=2),
            Layout(name="trades", ratio=1),
        )
        return layout

    def render_header(self) -> Panel:
        equity = self.exchange.get_equity("USDT")
        pnl = equity - self.starting_capital
        pnl_pct = float(pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0
        elapsed = ""
        if self._session_start:
            secs = time_module.time() - self._session_start
            elapsed = f" | Runtime: {int(secs // 60)}m {int(secs % 60)}s"

        text = Text()
        text.append(f"Mode: {self.mode.upper()}  |  ", style="bold cyan")
        text.append(f"Capital: ${float(self.starting_capital):.2f}  |  ", style="white")
        text.append(f"Equity: ${float(equity):.2f}  |  ", style="white")
        color = "green" if pnl >= 0 else "red"
        text.append(f"P&L: ${float(pnl):+.4f} ({pnl_pct:+.2f}%)  |  ", style=f"bold {color}")
        text.append(f"Fees: ${float(self.exchange.total_fees_paid):.4f}{elapsed}", style="dim white")

        return Panel(text, title="[bold yellow]Meme Coin Spot Bot[/bold yellow]")

    def render_positions_table(self) -> Panel:
        table = Table(show_header=True, header_style="bold cyan", box=None, padding=(0, 0))
        table.add_column("Symbol")
        table.add_column("Price", justify="right")
        table.add_column("Vol Δ", justify="right")
        table.add_column("RSI", justify="right")
        table.add_column("Side", justify="center")
        table.add_column("Entry", justify="right")
        table.add_column("Float P&L", justify="right")
        table.add_column("Signal", justify="left")

        for sym in MEME_SYMBOLS:
            state = self.strategy._coins.get(sym)
            if state is None or state.last_price == 0:
                table.add_row(sym, "—", "—", "—", "—", "—", "—", "—")
                continue

            price_str = format_price(state.last_price)
            vol_str = "—"
            if state.avg_volume > 0 and state.last_volume > 0:
                ratio = float(state.last_volume / state.avg_volume)
                vc = "green" if ratio >= 2.0 else ("yellow" if ratio >= 1.5 else "white")
                vol_str = f"[{vc}]{ratio:.1f}x[/{vc}]"

            rsi_str = "—"
            if len(state.price_history) >= 9:
                rsi_val = self.strategy._calc_rsi(state.price_history, 8)
                if rsi_val is not None:
                    rc = "red" if rsi_val >= 70 else ("green" if rsi_val <= 30 else "white")
                    rsi_str = f"[{rc}]{rsi_val:.0f}[/{rc}]"

            position_info = self.open_positions.get(sym)
            if state.in_position and position_info:
                entry = Decimal(str(position_info["entry_price"]))
                entry_str = format_price(entry)
                float_pnl = state.last_price - entry
                float_pnl_pct = (float_pnl / entry * 100) if entry > 0 else Decimal("0")
                side_str = "[green]LONG[/green]"
                pnl_str = format_pnl(float_pnl * 10, float_pnl_pct)  # approximate for small amount
                # More accurate P&L using actual position
                pos_amount = Decimal(str(position_info.get("amount", 0)))
                if pos_amount > 0:
                    pos_value = pos_amount * state.last_price
                    entry_value = pos_amount * entry
                    actual_pnl = pos_value - entry_value
                    actual_pnl_pct = ((actual_pnl) / entry_value * 100) if entry_value > 0 else Decimal("0")
                    pnl_str = format_pnl(actual_pnl, actual_pnl_pct)

                signal_info = position_info.get("signal", "")
                table.add_row(sym, price_str, vol_str, rsi_str, side_str, entry_str, pnl_str, signal_info)
            else:
                table.add_row(sym, price_str, vol_str, rsi_str, "—", "—", "—", "—")

        return Panel(table, title="[bold]Positions[/bold]")

    def render_trade_log(self) -> Panel:
        text = Text()
        if not self.trades:
            text.append("No trades yet.\n", style="dim")

        recent = self.trades[-10:]
        for t in recent:
            side = t["side"].upper()
            sc = "green" if side == "BUY" else "red"
            sym = t["symbol"]
            price = format_price(Decimal(str(t.get("price", 0))))
            timestamp = t.get("timestamp", "")[:19].replace("T", " ")
            pnl_str = t.get("pnl_display", "")

            line = f"[dim]{timestamp}[/dim]  [{sc}]{side:4s}[/{sc}] {sym:12s} @ {price}"
            if pnl_str:
                line += f"  {pnl_str}"
            if t.get("exit_reason"):
                line += f"  [{sc}]{t['exit_reason']}[/{sc}]"
            text.append(line + "\n")

        return Panel(text, title=f"[bold]Trade Log ({len(self.trades)} total)[/bold]")

    # ---- Network ----

    async def _setup_real_feed(self):
        try:
            import ccxt.async_support as ccxt_async
            opts: dict = {"enableRateLimit": True, "timeout": 15000}
            if self.proxy:
                opts["socksProxy"] = self.proxy
            exchange = ccxt_async.okx(opts)
            t = await exchange.fetch_ticker("PEPE/USDT")
            console.print(f"[green]OKX connected: PEPE/USDT = {t.get('last')}[/green]")
            return exchange
        except Exception as e:
            logger.warning(f"OKX connection failed: {e}")
            return None

    # ---- Run loop ----

    async def start(self) -> None:
        self._running = True
        self._session_start = time_module.time()

        if self.mode == self.MODE_OFFLINE:
            console.print("[cyan]Offline simulation[/cyan]")
            self._data_feed = SyntheticTickerFeed(MEME_SYMBOLS, seed=random.randint(1, 9999))
        elif self.mode == self.MODE_PAPER:
            console.print("[yellow]Paper trading — real OKX prices[/yellow]")
            self._data_feed = await self._setup_real_feed()
            if self._data_feed is None:
                console.print("[red]OKX unreachable, falling back to offline mode[/red]")
                self.mode = self.MODE_OFFLINE
                self._data_feed = SyntheticTickerFeed(MEME_SYMBOLS)
        elif self.mode == self.MODE_LIVE:
            console.print("[bold red]LIVE TRADING — REAL MONEY AT RISK[/bold red]")
            self._data_feed = await self._setup_real_feed()
            if self._data_feed is None:
                console.print("[red]Cannot connect to OKX. Aborting.[/red]")
                return

        console.print(f"Capital: ${float(self.starting_capital):.2f}  |  "
                      f"Symbols: {len(MEME_SYMBOLS)}  |  "
                      f"Min vol: $200k  |  TP: +2%  |  SL: -2.5%")
        console.print("=" * 70)

        layout = self.build_layout()

        with Live(layout, refresh_per_second=2, console=console, screen=False) as live:
            while self._running:
                try:
                    await self._tick(live, layout)
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Tick error: %s", str(e)[:100])
                    await asyncio.sleep(0.5)

    async def _tick(self, live: Live, layout: Layout) -> None:
        for symbol in MEME_SYMBOLS:
            if not self._running:
                break
            try:
                raw = await self._data_feed.fetch_ticker(symbol)
                current_vol = Decimal(str(raw.get('baseVolume', 0) or 0))
                vol_delta = current_vol
                prev_vol = self._prev_volumes.get(symbol)
                if prev_vol is not None and prev_vol > 0:
                    vol_delta = current_vol - prev_vol
                    if vol_delta < 0:
                        vol_delta = current_vol
                self._prev_volumes[symbol] = current_vol

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
                    await self._handle_signal(signal, ticker)

            except Exception:
                continue
            await asyncio.sleep(0.1)

        layout["header"].update(self.render_header())
        layout["positions"].update(self.render_positions_table())
        layout["trades"].update(self.render_trade_log())

    async def _handle_signal(self, signal, ticker: Ticker) -> None:
        is_buy = signal.order_side == OrderSide.BUY

        if is_buy and self.strategy.is_in_position(signal.symbol):
            return

        positions = await self.exchange.fetch_positions()
        balances = await self.exchange.fetch_balance()
        equity = self.exchange.get_equity("USDT")

        approved, reason = await self.risk_manager.validate(
            signal=signal, positions=positions, balances=balances, equity=equity,
        )
        if not approved:
            return

        price = signal.price
        amount = signal.amount if signal.amount > 0 else Decimal("0")

        order = await self.exchange.create_order(
            symbol=signal.symbol, side=signal.order_side.value,
            order_type=signal.order_type, amount=amount,
            price=price if signal.order_type == "limit" else None,
        )

        if order.status.value == "rejected":
            return

        exit_reason = signal.metadata.get("exit_reason", "")
        pnl_pct = signal.metadata.get("pnl_pct", "")

        if is_buy:
            # ---- ENTRY ----
            self.strategy.record_entry(signal.symbol, price)
            # Calculate actual amount from order
            actual_amount = order.filled if order.filled > 0 else amount
            self.open_positions[signal.symbol] = {
                "entry_price": str(price),
                "amount": str(actual_amount),
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "signal": f"vol_brk",
                "fee": str(order.fee.total_fee_rate * price * actual_amount) if order.fee and order.fee.total_fee_rate else "0",
                "expected_return": f"{float(signal.expected_return_rate * 100):.2f}%",
            }
            pnl_display = ""

            console.print(
                f"[bold green]▶ BUY[/bold green]  {signal.symbol}  "
                f"@ {format_price(price)}  "
                f"amount={float(actual_amount):.6f}  "
                f"expected_return={float(signal.expected_return_rate * 100):.2f}%  "
                f"fee=${float(Decimal(str(self.open_positions[signal.symbol]['fee']))):.4f}"
            )
        else:
            # ---- EXIT ----
            self.strategy.record_exit(signal.symbol)
            pos_info = self.open_positions.pop(signal.symbol, {})
            entry_price = Decimal(str(pos_info.get("entry_price", 0)))
            pos_amount = Decimal(str(pos_info.get("amount", 0)))

            if entry_price > 0 and pos_amount > 0:
                exit_value = price * pos_amount
                entry_value = entry_price * pos_amount
                realized_pnl = exit_value - entry_value
                entry_fee = Decimal(str(pos_info.get("fee", 0)))
                exit_fee = self.exchange._fee_schedule.taker * price * pos_amount if self.exchange._fee_schedule else Decimal("0")
                total_fee = entry_fee + exit_fee
                net_pnl = realized_pnl - total_fee
                realized_pnl_pct = (realized_pnl / entry_value * 100) if entry_value > 0 else Decimal("0")
                net_pnl_pct = (net_pnl / entry_value * 100) if entry_value > 0 else Decimal("0")

                pnl_color = "green" if net_pnl >= 0 else "red"
                pnl_display = (
                    f"gross=[{pnl_color}]${float(realized_pnl):+.4f}[/{pnl_color}] "
                    f"net=[{pnl_color}]${float(net_pnl):+.4f} ({float(net_pnl_pct):+.2f}%)[/{pnl_color}]"
                )

                exit_msg = (
                    f"[bold red]◀ SELL[/bold red] {signal.symbol}  "
                    f"@ {format_price(price)}  "
                    f"entry= {format_price(entry_price)}  "
                    f"reason=[bold]{exit_reason}[/bold]  "
                    f"gross_P&L= ${float(realized_pnl):+.4f} ({float(realized_pnl_pct):+.2f}%)  "
                    f"fees= ${float(total_fee):.4f}  "
                    f"net_P&L= [{pnl_color}]${float(net_pnl):+.4f} ({float(net_pnl_pct):+.2f}%)[/{pnl_color}]"
                )
                console.print(exit_msg)
            else:
                pnl_display = ""
                console.print(f"[bold red]◀ SELL[/bold red] {signal.symbol}  @ {format_price(price)}  reason=[bold]{exit_reason}[/bold]")

        # Record trade
        trade_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.symbol,
            "side": signal.order_side.value,
            "price": str(price),
            "expected_return_pct": f"{float(signal.expected_return_rate * 100):.2f}",
            "exit_reason": exit_reason,
            "pnl_pct": pnl_pct,
            "pnl_display": locals().get("pnl_display", ""),
        }
        self.trades.append(trade_entry)
        self.trade_logger.log(**trade_entry)

        equity = self.exchange.get_equity("USDT")
        self.circuit_breaker.update_equity(equity)

    async def stop(self) -> None:
        self._running = False
        if self._data_feed and hasattr(self._data_feed, "close"):
            try: await self._data_feed.close()
            except: pass
        await self.exchange.close()

        equity = self.exchange.get_equity("USDT")
        pnl = equity - self.starting_capital
        pnl_pct = float(pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0

        console.print("\n" + "=" * 60)
        console.print("[bold]Session Summary[/bold]")
        console.print(f"  Mode:           {self.mode}")
        console.print(f"  Runtime:        {int((time_module.time() - (self._session_start or time_module.time())) // 60)}m")
        console.print(f"  Total trades:   {len(self.trades)}")
        console.print(f"  Buys:           {sum(1 for t in self.trades if t['side'] == 'buy')}")
        console.print(f"  Sells:          {sum(1 for t in self.trades if t['side'] == 'sell')}")
        console.print(f"  Starting:       ${float(self.starting_capital):.2f}")
        console.print(f"  Final equity:   ${float(equity):.2f}")
        color = "green" if pnl >= 0 else "red"
        console.print(f"  P&L:            [{color}]${float(pnl):+.4f} ({pnl_pct:+.2f}%)[/{color}]")
        console.print(f"  Total fees:     ${float(self.exchange.total_fees_paid):.4f}")

        if self.trades:
            sells = [t for t in self.trades if t['side'] == 'sell']
            tp_count = sum(1 for t in sells if 'take_profit' in t.get('exit_reason', ''))
            sl_count = sum(1 for t in sells if 'stop_loss' in t.get('exit_reason', ''))
            trail_count = sum(1 for t in sells if 'trailing_stop' in t.get('exit_reason', ''))
            time_count = sum(1 for t in sells if 'time_stop' in t.get('exit_reason', ''))
            console.print(f"  TP exits: {tp_count} | SL exits: {sl_count} | Trailing: {trail_count} | Time: {time_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Meme Coin Spot Trading Bot")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--capital", type=float, default=30)
    parser.add_argument("--proxy", type=str, default=None)
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
        try: await task
        except asyncio.CancelledError: pass
        await bot.stop()

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

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

from src.config.loader import load_config
from src.core.constants import MarketType, OrderSide, SignalType
from src.core.event_bus import EventBus
from src.discovery.volatility_scanner import VolatilityScanner
from src.exchange.models import Ticker
from src.exchange.paper_exchange import PaperExchange
from src.exchange.synthetic_feed import SyntheticTickerFeed
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule
from src.strategy.meme_scalper import MemeScalperStrategy
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.utils.local_trade_logger import LocalTradeLogger
from src.utils.logger import TradeLogger, setup_logger

logger = setup_logger("meme_bot", level="INFO", fmt="json")
console = Console()

# ---- Local File Logger (gitignored, never uploaded) ----

def load_meme_config() -> tuple[list[str], dict]:
    """Load meme bot configuration from config/meme.yaml.

    Returns (symbols, profiles) from the config system.
    """
    config = load_config("config", mode="meme")
    symbols = config.market.symbols
    profiles = {}
    for sym, profile in config.strategy.synthetic_profiles.items():
        profiles[sym] = {
            "base_price": profile.base_price,
            "volatility": profile.volatility,
            "base_volume": profile.base_volume,
        }
    return symbols, profiles


def format_price(p: Decimal) -> str:
    """Format price for display, handling sub-penny meme coins."""
    return f"{float(p):.8f}".rstrip('0').rstrip('.')


def format_pnl(value: Decimal, pct: Decimal) -> str:
    """Colored P&L string."""
    color = "green" if value >= 0 else "red"
    return f"[{color}]${float(value):+.4f} ({float(pct):+.2f}%)[/{color}]"



# ---- Meme Bot ----

class MemeBot:
    MODE_OFFLINE = "offline"
    MODE_PAPER = "paper"
    MODE_LIVE = "live"

    def __init__(self, mode: str = MODE_OFFLINE, capital: Decimal = Decimal("30"),
                 proxy: str | None = None, market_type: MarketType = MarketType.SPOT,
                 symbols: list[str] | None = None, profiles: dict | None = None) -> None:
        self.mode = mode
        self.market_type = market_type
        self.starting_capital = capital
        self.trades: list[dict] = []      # trade history log
        self.open_positions: dict[str, dict] = {}  # symbol -> entry info
        self.proxy = proxy
        self._symbols = symbols or []
        self._profiles = profiles or {}

        self.exchange = PaperExchange(initial_balances={"USDT": capital})
        from src.exchange.base import FeeSchedule as ExFS
        self.exchange._fee_schedule = ExFS(maker=Decimal("0.001"), taker=Decimal("0.001"))

        fs = FeeSchedule.spot()
        self.fee_calculator = FeeCalculator(fee_schedule=fs, min_profit_buffer=Decimal("0.0005"))
        self.event_bus = EventBus()

        # 动态仓位：权益 × 25%，随账户增长自动缩放
        initial_equity = capital
        base_order = max(initial_equity * Decimal("0.25"), Decimal("5"))
        if market_type == MarketType.FUTURE:
            base_order = max(initial_equity * Decimal("0.25"), Decimal("10"))

        self.strategy = MemeScalperStrategy(
            market_type=self.market_type, fee_calculator=self.fee_calculator,
            event_bus=self.event_bus,
            min_volume_usdt=Decimal("100000"), volume_spike_ratio=Decimal("1.2"),
            momentum_lookback=6, ema_period=5, rsi_period=8,
            rsi_long_min=35, rsi_long_max=55,
            rsi_short_min=55, rsi_short_max=75,
            take_profit_pct=Decimal("1.5"), stop_loss_pct=Decimal("2.5"),
            max_hold_minutes=10, trailing_stop_pct=Decimal("0.8"),
            base_order_usdt=base_order,
        )
        self.strategy.set_symbols(self._symbols)

        self.circuit_breaker = CircuitBreaker(
            initial_equity=capital, max_daily_loss_pct=Decimal("15"),
            max_drawdown_pct=Decimal("25"),
        )
        # 合约模式: 3x杠杆, 仓位上限 = 40% × 3 = 120% 名义价值
        # 现货模式: 1x, 仓位上限 = 40%
        if market_type == MarketType.FUTURE:
            leverage = 3
            max_pos_pct = Decimal("120")  # $30 equity × 120% = $36 notional max
        else:
            leverage = 1
            max_pos_pct = Decimal("40")

        self.risk_manager = RiskManager(
            circuit_breaker=self.circuit_breaker,
            position_sizer=PositionSizer(max_position_pct=max_pos_pct, max_leverage=leverage),
            max_concurrent_positions=5, cooldown_seconds=30,
        )
        self.trade_logger = TradeLogger("data/logs/meme_trades.csv")

        self._session_start = None
        self._running = False
        self._data_feed = None
        self._prev_volumes: dict[str, Decimal] = {}
        self.local_log = LocalTradeLogger("data/logs")
        self._last_kline_fetch: dict[str, float] = {}  # symbol -> last fetch timestamp
        self._kline_interval = 60.0  # fetch klines every 60s per symbol
        self._exit_time: dict[str, float] = {}  # symbol -> last exit timestamp (for post-exit delay)
        self._post_exit_delay = 60.0  # wait 1 kline (60s) before re-entry after exit
        # Hourly volatility scanner — refreshes symbol list dynamically
        self._scanner = VolatilityScanner(min_volume_usdt=100_000, top_n=30)
        self._last_scan_time: float = 0
        self._scan_interval = 3600.0  # rescan every 1 hour

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

        for sym in self._symbols:
            state = self.strategy._coins.get(sym)
            if state is None or state.last_price == 0:
                table.add_row(sym, "—", "—", "—", "—", "—", "—", "—")
                continue

            price_str = format_price(state.last_price)
            vol_str = "—"
            if state.kline_avg_volume > 0 and state.last_volume > 0:
                ratio = float(state.last_volume / state.kline_avg_volume)
                vc = "green" if ratio >= 2.0 else ("yellow" if ratio >= 1.5 else "white")
                vol_str = f"[{vc}]{ratio:.1f}x[/{vc}]"

            rsi_str = "—"
            if state.cached_rsi != 50.0 or len(state.price_history) >= 9:
                rsi_val = state.cached_rsi
                rc = "red" if rsi_val >= 70 else ("green" if rsi_val <= 30 else "white")
                rsi_str = f"[{rc}]{rsi_val:.0f}[/{rc}]"

            position_info = self.open_positions.get(sym)
            if state.in_position and position_info:
                entry = Decimal(str(position_info["entry_price"]))
                entry_str = format_price(entry)
                pos_side = position_info.get("side", state.position_side)
                pos_amount = Decimal(str(position_info.get("amount", 0)))

                # P&L differs for long vs short
                if pos_side == "short":
                    side_str = "[red]SHORT[/red]"
                    if pos_amount > 0:
                        pos_value = pos_amount * state.last_price
                        entry_value = pos_amount * entry
                        # For short: profit when price drops
                        actual_pnl = entry_value - pos_value
                        actual_pnl_pct = (actual_pnl / entry_value * 100) if entry_value > 0 else Decimal("0")
                        pnl_str = format_pnl(actual_pnl, actual_pnl_pct)
                    else:
                        pnl_str = "—"
                else:
                    side_str = "[green]LONG[/green]"
                    if pos_amount > 0:
                        pos_value = pos_amount * state.last_price
                        entry_value = pos_amount * entry
                        actual_pnl = pos_value - entry_value
                        actual_pnl_pct = (actual_pnl / entry_value * 100) if entry_value > 0 else Decimal("0")
                        pnl_str = format_pnl(actual_pnl, actual_pnl_pct)
                    else:
                        pnl_str = "—"

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
        self._tick_count = 0

        # Log file paths for OpenClaw monitoring
        log_files = {
            "session_csv": str(self.local_log.csv_path),
            "session_txt": str(self.local_log.txt_path),
            "session_json": str(self.local_log.json_path),
            "trade_csv": str(self.trade_logger.path),
        }
        logger.info("Bot starting",
                    extra={"symbols": len(self._symbols), "mode": self.mode,
                           "capital": float(self.starting_capital),
                           "log_files": log_files})
        console.print(f"[dim]Log files:[/dim]")
        console.print(f"  [dim]Session CSV:[/dim] {log_files['session_csv']}")
        console.print(f"  [dim]Session JSON:[/dim] {log_files['session_json']}")
        console.print(f"  [dim]Trade CSV:[/dim] {log_files['trade_csv']}")

        if self.mode == self.MODE_OFFLINE:
            console.print("[cyan]Offline simulation[/cyan]")
            self._data_feed = SyntheticTickerFeed(self._symbols, self._profiles, seed=random.randint(1, 9999))
        elif self.mode == self.MODE_PAPER:
            console.print("[yellow]Paper trading — real OKX prices[/yellow]")
            self._data_feed = await self._setup_real_feed()
            if self._data_feed is None:
                console.print("[red]OKX unreachable, falling back to offline mode[/red]")
                self.mode = self.MODE_OFFLINE
                self._data_feed = SyntheticTickerFeed(self._symbols, self._profiles)
        elif self.mode == self.MODE_LIVE:
            console.print("[bold red]LIVE TRADING — REAL MONEY AT RISK[/bold red]")
            self._data_feed = await self._setup_real_feed()
            if self._data_feed is None:
                console.print("[red]Cannot connect to OKX. Aborting.[/red]")
                return

        mt_text = "SPOT" if self.market_type == MarketType.SPOT else "FUTURES"
        console.print(f"Capital: ${float(self.starting_capital):.2f}  |  "
                      f"Market: {mt_text}  |  "
                      f"Symbols: {len(self._symbols)}  |  "
                      f"Min vol: $200k  |  TP: ±2%  |  SL: ±2.5%")
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
        self._tick_count = getattr(self, '_tick_count', 0) + 1
        if self._tick_count == 1 or self._tick_count % 60 == 0:
            print(f"[TICK #{self._tick_count}] looping {len(self._symbols)} symbols...", flush=True)

        # Fetch all tickers concurrently
        async def _fetch_one(symbol: str):
            try:
                raw = await self._data_feed.fetch_ticker(symbol)
                return symbol, raw, None
            except Exception as e:
                return symbol, None, e

        results = await asyncio.gather(*[_fetch_one(s) for s in self._symbols])

        # Fetch klines concurrently (staggered on first 2 ticks to warm up)
        now = time_module.time()
        kline_tasks = []
        for i, symbol in enumerate(self._symbols):
            last_fetch = self._last_kline_fetch.get(symbol, 0)
            # Force initial kline fetch within first 3 ticks (staggered)
            if self._tick_count <= 3 and last_fetch == 0:
                self._last_kline_fetch[symbol] = now
                kline_tasks.append(self._fetch_kline_for_symbol(symbol))
            elif now - last_fetch >= self._kline_interval:
                kline_tasks.append(self._fetch_kline_for_symbol(symbol))

        if kline_tasks:
            await asyncio.gather(*kline_tasks)

        # Update strategy equity for dynamic position sizing
        self.strategy.current_equity = self.exchange.get_equity("USDT")

        # Hourly volatility scan — refresh symbol list with top volatile coins
        if now - self._last_scan_time > self._scan_interval:
            await self._refresh_symbols()

        # Process signals sequentially (avoids race conditions on positions/balances)
        for symbol, raw, err in results:
            if not self._running:
                break
            if err is not None or raw is None:
                continue
            try:
                current_vol = Decimal(str(raw.get('baseVolume', 0) or 0))
                ticker = Ticker(
                    symbol=symbol,
                    bid=Decimal(str(raw.get('bid', 0) or 0)),
                    ask=Decimal(str(raw.get('ask', 0) or 0)),
                    last=Decimal(str(raw.get('last', 0) or 0)),
                    high=Decimal(str(raw.get('high', 0) or 0)),
                    low=Decimal(str(raw.get('low', 0) or 0)),
                    volume=current_vol,
                )
                self.exchange.set_ticker(ticker)

                signal = await self.strategy.on_ticker(ticker)
                if signal:
                    await self._handle_signal(signal, ticker)
            except Exception:
                continue

        layout["header"].update(self.render_header())
        layout["positions"].update(self.render_positions_table())
        layout["trades"].update(self.render_trade_log())

        # Structured heartbeat for OpenClaw monitoring (every 60s)
        now_ts = int(time_module.time())
        if not hasattr(self, '_last_hb') or now_ts - self._last_hb >= 60:
            self._last_hb = now_ts
            equity = self.exchange.get_equity("USDT")
            pnl = equity - self.starting_capital
            positions = await self.exchange.fetch_positions()
            open_pos = len(positions)
            logger.info("heartbeat",
                        extra={"tick": self._tick_count,
                               "trades": len(self.trades),
                               "open_positions": open_pos,
                               "equity": float(equity),
                               "pnl": float(pnl),
                               "pnl_pct": float(pnl / self.starting_capital * 100) if self.starting_capital > 0 else 0,
                               "symbols": len(self._symbols),
                               "mode": self.mode})

    async def _refresh_symbols(self) -> None:
        """Scan OKX for top volatile coins and update strategy symbol list."""
        if self._data_feed is None or not hasattr(self._data_feed, 'fetch_tickers'):
            return
        try:
            symbols = await self._scanner.scan(self._data_feed)
            if symbols and len(symbols) >= 10:
                self._symbols = symbols
                self.strategy.set_symbols(symbols)
                self._profiles = self._scanner.profiles
                self._last_scan_time = time_module.time()
                logger.info("Symbols refreshed",
                            extra={"count": len(symbols),
                                   "top3": symbols[:3]})
        except Exception as e:
            logger.warning("Volatility scan failed", extra={"error": str(e)[:100]})

    async def _fetch_kline_for_symbol(self, symbol: str) -> None:
        """Fetch kline data for one symbol (used concurrently)."""
        try:
            klines = await self._data_feed.fetch_ohlcv(symbol, "1m", limit=30)
            if klines:
                for k in klines:
                    k_vol = Decimal(str(k[5]))
                    self.strategy.update_kline_volume(symbol, k_vol)
                if self._tick_count <= 1:
                    logger.debug("kline loaded",
                                extra={"symbol": symbol, "candles": len(klines),
                                       "last_vol": klines[-1][5]})
            self._last_kline_fetch[symbol] = time_module.time()
        except Exception as e:
            if self._tick_count <= 5:
                logger.warning("kline fetch failed",
                              extra={"symbol": symbol, "error": str(e)[:100]})

    async def _handle_signal(self, signal, ticker: Ticker) -> None:
        sig_type = signal.signal_type
        direction = signal.metadata.get("direction", "long")
        is_entry_long = sig_type == SignalType.BUY_LONG
        is_entry_short = sig_type == SignalType.SELL_SHORT
        is_exit_long = sig_type == SignalType.SELL_LONG
        is_exit_short = sig_type == SignalType.BUY_SHORT
        is_entry = is_entry_long or is_entry_short
        is_exit = is_exit_long or is_exit_short

        # Prevent duplicate entries
        if is_entry and self.strategy.is_in_position(signal.symbol):
            logger.info("Signal rejected: already in position",
                        extra={"symbol": signal.symbol, "direction": direction})
            return

        positions = await self.exchange.fetch_positions()
        balances = await self.exchange.fetch_balance()
        equity = self.exchange.get_equity("USDT")

        approved, reason = await self.risk_manager.validate(
            signal=signal, positions=positions, balances=balances, equity=equity,
        )
        if not approved:
            logger.info("Signal rejected by risk manager",
                        extra={"symbol": signal.symbol, "reason": reason,
                               "direction": direction, "type": sig_type.value})
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
        fee_rate = Decimal("0.001")
        if self.exchange._fee_schedule:
            fee_rate = self.exchange._fee_schedule.taker

        # ==================== ENTRY ====================
        if is_entry:
            # Post-exit delay: wait 1 kline (60s) before re-entry
            last_exit = self._exit_time.get(signal.symbol, 0)
            if last_exit > 0:
                elapsed = time_module.time() - last_exit
                if elapsed < self._post_exit_delay:
                    logger.info("Entry rejected: post-exit delay",
                                extra={"symbol": signal.symbol, "elapsed": f"{elapsed:.0f}s",
                                       "required": f"{self._post_exit_delay:.0f}s"})
                    return
            actual_amount = order.filled if order.filled > 0 else amount
            entry_fee = fee_rate * price * actual_amount
            side_label = "long" if is_entry_long else "short"

            self.strategy.record_entry(signal.symbol, price, amount=actual_amount, side=side_label)

            self.open_positions[signal.symbol] = {
                "entry_price": str(price),
                "amount": str(actual_amount),
                "side": side_label,
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "signal": "vol_brk",
                "fee": str(entry_fee),
                "expected_return": f"{float(signal.expected_return_rate * 100):.2f}%",
            }

            self.local_log.log_entry(
                symbol=signal.symbol, price=price, amount=actual_amount,
                expected_return=f"{float(signal.expected_return_rate * 100):.2f}%",
                fee=entry_fee,
            )

            self.trades.append({
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "symbol": signal.symbol, "side": signal.order_side.value,
                "price": str(price),
                "expected_return_pct": f"{float(signal.expected_return_rate * 100):.2f}",
                "exit_reason": "", "pnl_display": "",
            })

            # Structured log for OpenClaw monitoring
            logger.info("trade_entry",
                        extra={"symbol": signal.symbol, "side": side_label,
                               "price": float(price), "amount": float(actual_amount),
                               "fee": float(entry_fee),
                               "expected_return_pct": float(signal.expected_return_rate * 100),
                               "equity": float(self.exchange.get_equity("USDT"))})

            if is_entry_long:
                console.print(
                    f"[bold green]▶ BUY (LONG)[/bold green]  {signal.symbol}  "
                    f"@ {format_price(price)}  "
                    f"amount={float(actual_amount):.6f}  "
                    f"exp_ret=+{float(signal.expected_return_rate * 100):.2f}%  "
                    f"fee=${float(entry_fee):.4f}"
                )
            else:
                console.print(
                    f"[bold red]▼ SELL (SHORT)[/bold red]  {signal.symbol}  "
                    f"@ {format_price(price)}  "
                    f"amount={float(actual_amount):.6f}  "
                    f"exp_ret=+{float(signal.expected_return_rate * 100):.2f}%  "
                    f"fee=${float(entry_fee):.4f}"
                )
            return

        # ==================== EXIT ====================
        if is_exit:
            self.strategy.record_exit(signal.symbol)
            self._exit_time[signal.symbol] = time_module.time()  # Record exit time for post-exit delay
            pos_info = self.open_positions.pop(signal.symbol, {})
            entry_price = Decimal(str(pos_info.get("entry_price", 0)))
            pos_amount = Decimal(str(pos_info.get("amount", 0)))
            pos_side = pos_info.get("side", "long")

            if entry_price > 0 and pos_amount > 0:
                exit_value = price * pos_amount
                entry_value = entry_price * pos_amount

                if pos_side == "short":
                    # Short: profit = entry_value - exit_value (price dropped)
                    gross_pnl = entry_value - exit_value
                else:
                    # Long: profit = exit_value - entry_value (price rose)
                    gross_pnl = exit_value - entry_value

                entry_fee = Decimal(str(pos_info.get("fee", 0)))
                exit_fee = fee_rate * price * pos_amount
                total_fee = entry_fee + exit_fee
                net_pnl = gross_pnl - total_fee
                gross_pnl_pct = (gross_pnl / entry_value * 100) if entry_value > 0 else Decimal("0")
                net_pnl_pct = (net_pnl / entry_value * 100) if entry_value > 0 else Decimal("0")

                self.local_log.log_exit(
                    symbol=signal.symbol, exit_price=price, entry_price=entry_price,
                    amount=pos_amount, exit_reason=exit_reason,
                    gross_pnl=gross_pnl, net_pnl=net_pnl, net_pnl_pct=net_pnl_pct,
                    entry_fee=entry_fee, exit_fee=exit_fee,
                )

                # Structured log for OpenClaw monitoring
                logger.info("trade_exit",
                            extra={"symbol": signal.symbol, "side": pos_side,
                                   "entry_price": float(entry_price),
                                   "exit_price": float(price),
                                   "amount": float(pos_amount),
                                   "exit_reason": exit_reason,
                                   "gross_pnl": float(gross_pnl),
                                   "net_pnl": float(net_pnl),
                                   "net_pnl_pct": float(net_pnl_pct),
                                   "total_fees": float(total_fee),
                                   "equity": float(self.exchange.get_equity("USDT"))})

                pnl_color = "green" if net_pnl >= 0 else "red"
                exit_tag = "◀ SELL" if is_exit_long else "▶ BUY"
                exit_label = "CLOSE LONG" if is_exit_long else "COVER SHORT"

                console.print(
                    f"[bold {pnl_color}]{exit_tag} ({exit_label})[/bold {pnl_color}] {signal.symbol}  "
                    f"@ {format_price(price)}  "
                    f"entry={format_price(entry_price)}  "
                    f"reason=[bold]{exit_reason}[/bold]  "
                    f"gross=${float(gross_pnl):+.4f} ({float(gross_pnl_pct):+.2f}%)  "
                    f"fees=${float(total_fee):.4f}  "
                    f"net=[{pnl_color}]${float(net_pnl):+.4f} ({float(net_pnl_pct):+.2f}%)[/{pnl_color}]"
                )
            else:
                console.print(f"[bold]{exit_reason}[/bold] {signal.symbol}  @ {format_price(price)}")

        # Record trade
        trade_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": signal.symbol,
            "side": signal.order_side.value,
            "price": str(price),
            "expected_return_pct": f"{float(signal.expected_return_rate * 100):.2f}",
            "exit_reason": exit_reason,
            "pnl_pct": pnl_pct,
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

        tp_count = sl_count = trail_count = time_count = 0
        if self.trades:
            sells = [t for t in self.trades if t['side'] == 'sell']
            tp_count = sum(1 for t in sells if 'take_profit' in t.get('exit_reason', ''))
            sl_count = sum(1 for t in sells if 'stop_loss' in t.get('exit_reason', ''))
            trail_count = sum(1 for t in sells if 'trailing_stop' in t.get('exit_reason', ''))
            time_count = sum(1 for t in sells if 'time_stop' in t.get('exit_reason', ''))
            console.print(f"  TP exits: {tp_count} | SL exits: {sl_count} | Trailing: {trail_count} | Time: {time_count}")

        # Write to local log files
        runtime = time_module.time() - (self._session_start or time_module.time())
        log_files = self.local_log.log_summary(
            mode=self.mode,
            start_capital=self.starting_capital,
            final_equity=equity,
            total_trades=len(self.trades),
            buys=sum(1 for t in self.trades if t.get('side') == 'buy'),
            sells=sum(1 for t in self.trades if t.get('side') == 'sell'),
            total_fees=self.exchange.total_fees_paid,
            tp_count=tp_count, sl_count=sl_count,
            trail_count=trail_count, time_count=time_count,
            runtime_secs=runtime,
        )
        console.print(f"\n[dim]Log files (local only, not uploaded to GitHub):[/dim]")
        for f in log_files.strip().split("\n"):
            console.print(f"  [dim cyan]{f}[/dim cyan]")

        # Structured session summary for OpenClaw
        logger.info("session_complete",
                    extra={"mode": self.mode,
                           "runtime_mins": int((time_module.time() - (self._session_start or time_module.time())) // 60),
                           "total_trades": len(self.trades),
                           "buys": sum(1 for t in self.trades if t.get('side') == 'buy'),
                           "sells": sum(1 for t in self.trades if t.get('side') == 'sell'),
                           "start_capital": float(self.starting_capital),
                           "final_equity": float(equity),
                           "pnl": float(pnl),
                           "pnl_pct": pnl_pct,
                           "total_fees": float(self.exchange.total_fees_paid),
                           "tp_exits": tp_count, "sl_exits": sl_count,
                           "trailing_exits": trail_count, "time_exits": time_count,
                           "log_files": log_files.strip().split("\n")})


def main() -> None:
    parser = argparse.ArgumentParser(description="Meme Coin Trading Bot")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--paper", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--futures", action="store_true", help="Enable futures mode (supports short-selling)")
    parser.add_argument("--capital", type=float, default=30)
    parser.add_argument("--proxy", type=str, default=None)
    args = parser.parse_args()

    if args.live:
        mode = MemeBot.MODE_LIVE
    elif args.paper:
        mode = MemeBot.MODE_PAPER
    else:
        mode = MemeBot.MODE_OFFLINE

    # Load symbols and profiles from config/meme.yaml
    symbols, profiles = load_meme_config()

    capital = Decimal(str(args.capital))
    market_type = MarketType.FUTURE if args.futures else MarketType.SPOT
    bot = MemeBot(mode=mode, capital=capital, proxy=args.proxy,
                  market_type=market_type, symbols=symbols, profiles=profiles)

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

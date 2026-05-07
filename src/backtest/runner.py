"""Backtesting engine.

Reuses the same strategy and fee calculator code as live trading.
Feeds historical OHLCV candles through PaperExchange for fill simulation.

Key design: the BacktestRunner replaces the live DataFeedManager.
Instead of polling an exchange API, it iterates over historical candles
in chronological order, calling strategy.on_ticker() for each tick.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from src.core.constants import MarketType, OrderSide, SignalType
from src.exchange.base import FeeSchedule
from src.exchange.models import Position, Ticker
from src.exchange.paper_exchange import PaperExchange
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule as CalcFeeSchedule
from src.utils.logger import setup_logger

logger = setup_logger(__name__)


class BacktestResult:
    """Container for backtest results."""

    def __init__(self) -> None:
        self.trades: list[dict] = []
        self.equity_curve: list[tuple[datetime, Decimal]] = []
        self.initial_equity = Decimal("0")
        self.final_equity = Decimal("0")
        self.total_return_pct = Decimal("0")
        self.total_fees = Decimal("0")
        self.sharpe_ratio = Decimal("0")
        self.max_drawdown_pct = Decimal("0")
        self.win_rate_pct = Decimal("0")
        self.profit_factor = Decimal("0")
        self.fee_burden_pct = Decimal("0")
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0


class BacktestRunner:
    """Runs a strategy against historical data via PaperExchange."""

    def __init__(
        self,
        symbol: str,
        strategy: BaseStrategy,
        market_type: MarketType,
        initial_capital: Decimal,
        fee_rate: Decimal = Decimal("0.001"),
        slippage_rate: Decimal = Decimal("0.0005"),
        max_position_pct: Decimal = Decimal("10"),
        cooldown_seconds: int = 0,
    ) -> None:
        self.symbol = symbol
        self.strategy = strategy
        self.market_type = market_type

        # Paper exchange for fill simulation
        fee_schedule = FeeSchedule(maker=fee_rate, taker=fee_rate)
        self.exchange = PaperExchange(
            fee_schedule=fee_schedule,
            initial_balances={"USDT": initial_capital},
        )

        # Fee calculator
        fee_schedule_calc = CalcFeeSchedule(
            maker=fee_rate,
            taker=fee_rate,
        )
        self.fee_calculator = FeeCalculator(
            fee_schedule=fee_schedule_calc,
            min_profit_buffer=Decimal("0.0005"),
        )
        self.strategy.fee_calculator = self.fee_calculator

        # Risk management (simplified for backtest — no circuit breaker by default)
        self.circuit_breaker = CircuitBreaker(
            initial_equity=initial_capital,
            max_daily_loss_pct=Decimal("100"),   # no limit in backtest
            max_drawdown_pct=Decimal("100"),
        )
        position_sizer = PositionSizer(
            max_position_pct=max_position_pct,
        )
        self.risk_manager = RiskManager(
            circuit_breaker=self.circuit_breaker,
            position_sizer=position_sizer,
            cooldown_seconds=cooldown_seconds,
        )

        self.initial_capital = initial_capital
        self._trades: list[dict] = []
        self._equity_curve: list[tuple[datetime, Decimal]] = []

    async def run(self, candles: list) -> BacktestResult:
        """Run the backtest across candles.

        Args:
            candles: List of OHLCV Candle objects sorted by timestamp.
        """
        from src.data.ohlcv_store import Candle

        if not candles:
            raise ValueError("No candles provided")

        logger.info(
            "Starting backtest",
            extra={
                "symbol": self.symbol,
                "candles": len(candles),
                "from": candles[0].timestamp.isoformat(),
                "to": candles[-1].timestamp.isoformat(),
            },
        )

        for i, candle in enumerate(candles):
            # Build ticker from candle (mid-point for simplicity)
            mid = (candle.high + candle.low) / 2
            ticker = Ticker(
                symbol=self.symbol,
                bid=mid * (Decimal("1") - Decimal("0.0005")),  # simulate spread
                ask=mid * (Decimal("1") + Decimal("0.0005")),
                last=candle.close,
                high=candle.high,
                low=candle.low,
                volume=candle.volume,
                timestamp=candle.timestamp,
            )

            # Update paper exchange ticker
            self.exchange.set_ticker(ticker)

            # Update indicators in ohlcv-like fashion (strategy reads from its own state)
            # For backtest, we manually call on_ticker with pre-calculated indicators
            # The strategy's on_ticker will run the fee gate internally
            signal = await self.strategy.on_ticker(ticker)

            if signal is None:
                # Track equity curve
                equity = self.exchange.get_equity("USDT")
                self._equity_curve.append((candle.timestamp, equity))
                continue

            # Validate via risk manager
            positions = await self.exchange.fetch_positions()
            balances = await self.exchange.fetch_balance()
            equity = self.exchange.get_equity("USDT")

            approved, reason = await self.risk_manager.validate(
                signal=signal,
                positions=positions,
                balances=balances,
                equity=equity,
                ticker=ticker,
            )

            if not approved:
                equity = self.exchange.get_equity("USDT")
                self._equity_curve.append((candle.timestamp, equity))
                continue

            # Execute via paper exchange
            order = await self.exchange.create_order(
                symbol=signal.symbol,
                side=signal.order_side.value,
                order_type=signal.order_type,
                amount=signal.amount,
                price=signal.price if signal.order_type == "limit" else None,
            )

            if order.status.value == "rejected":
                equity = self.exchange.get_equity("USDT")
                self._equity_curve.append((candle.timestamp, equity))
                continue

            # Record trade
            fee_estimate = signal.fee_estimate
            trade_record = {
                "timestamp": candle.timestamp.isoformat(),
                "symbol": signal.symbol,
                "side": signal.order_side.value,
                "price": str(signal.price),
                "amount": str(signal.amount),
                "fee": str(order.fee.total_fee_rate * signal.price * signal.amount) if order.fee else "0",
                "expected_return_pct": f"{signal.expected_return_rate * 100:.4f}",
                "fee_round_trip_pct": f"{fee_estimate.round_trip_pct:.4f}" if fee_estimate else "N/A",
                "net_expected_pct": f"{signal.expected_net_return * 100:.4f}",
            }
            self._trades.append(trade_record)

            # Track equity
            equity = self.exchange.get_equity("USDT")
            self._equity_curve.append((candle.timestamp, equity))

        logger.info(
            "Backtest complete",
            extra={"trades": len(self._trades), "fees": str(self.exchange.total_fees_paid)},
        )

        return self._build_result()

    def _build_result(self) -> BacktestResult:
        """Compute final metrics from trade and equity data."""
        result = BacktestResult()

        if not self._trades or not self._equity_curve:
            return result

        result.trades = self._trades
        result.equity_curve = self._equity_curve
        result.initial_equity = self.initial_capital
        result.final_equity = self._equity_curve[-1][1]
        result.total_fees = self.exchange.total_fees_paid

        # Total return
        if result.initial_equity > 0:
            result.total_return_pct = (
                (result.final_equity - result.initial_equity) / result.initial_equity * 100
            )

        # Max drawdown
        peak = result.initial_equity
        max_dd = Decimal("0")
        for _, equity in self._equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak * 100 if peak > 0 else Decimal("0")
            if dd > max_dd:
                max_dd = dd
        result.max_drawdown_pct = max_dd

        # Win rate based on actual realized P&L from closed round-trips
        result.total_trades = len(self._trades)
        fills = self.exchange.fills
        # Match sell fills against preceding buy fills per symbol to compute realized P&L
        buy_stack: dict[str, list] = {}  # symbol -> list of (price, amount)
        for fill in fills:
            if fill.side.value == "buy":
                if fill.symbol not in buy_stack:
                    buy_stack[fill.symbol] = []
                buy_stack[fill.symbol].append((fill.price, fill.amount))
            elif fill.side.value == "sell":
                stack = buy_stack.get(fill.symbol, [])
                if stack:
                    entry_price, _ = stack.pop(0)
                    realized_return = (fill.price - entry_price) / entry_price
                    if realized_return > 0:
                        result.winning_trades += 1
                    else:
                        result.losing_trades += 1

        # Fallback: if no pair matching succeeded, use trade record side counts
        if result.winning_trades == 0 and result.losing_trades == 0:
            for trade in self._trades:
                if trade.get("side") == "sell":
                    result.losing_trades += 1  # conservative default

        if result.total_trades > 0:
            result.win_rate_pct = Decimal(result.winning_trades) / Decimal(result.total_trades) * 100

        # Profit factor
        if result.losing_trades > 0 and result.winning_trades > 0:
            result.profit_factor = Decimal(result.winning_trades) / Decimal(result.losing_trades)
        elif result.winning_trades > 0:
            result.profit_factor = Decimal("999")  # no losers = max

        # Fee burden
        if result.initial_equity > 0:
            result.fee_burden_pct = result.total_fees / result.initial_equity * 100

        # Sharpe (simplified: using period returns)
        result.sharpe_ratio = self._calc_sharpe()

        return result

    def _calc_sharpe(self) -> Decimal:
        """Calculate Sharpe ratio from equity curve.

        Uses simple returns between each data point.
        Annualized assuming 365 days.
        """
        if len(self._equity_curve) < 2:
            return Decimal("0")

        returns: list[Decimal] = []
        for i in range(1, len(self._equity_curve)):
            prev = self._equity_curve[i - 1][1]
            curr = self._equity_curve[i][1]
            if prev > 0:
                returns.append((curr - prev) / prev)

        if not returns:
            return Decimal("0")

        avg_return = sum(returns) / len(returns)
        if len(returns) < 2:
            return Decimal("0")

        variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
        from src.data.indicator import decimal_sqrt
        stdev = decimal_sqrt(variance)

        if stdev == 0:
            return Decimal("0")

        # Annualize
        return (avg_return / stdev) * decimal_sqrt(Decimal("365"))

"""Trading Engine — orchestrates all components.

Wires together:
    - Data feed (ticker polling)
    - Strategy (signal generation with fee gate)
    - Risk manager (signal validation)
    - Exchange (order execution)
    - Portfolio tracker (P&L tracking)
    - Circuit breaker (risk limits)
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Optional

from src.config.schema import AppConfig
from src.core.constants import EventType, OrderSide, SignalType
from src.core.event_bus import EventBus
from src.data.ohlcv_store import OHLCVStore
from src.data.subscription import SubscriptionManager
from src.exchange.base import ExchangeInterface, FeeSchedule
from src.exchange.ccxt_exchange import CCXTExchange
from src.exchange.paper_exchange import PaperExchange
from src.exchange.models import Order, Position, Ticker
from src.exchange.websocket_manager import DataFeedManager
from src.portfolio.tracker import PortfolioTracker
from src.risk.circuit_breaker import CircuitBreaker
from src.risk.manager import RiskManager
from src.risk.position_sizer import PositionSizer
from src.strategy.base import BaseStrategy, Signal
from src.strategy.fee_calculator import FeeCalculator, FeeSchedule as FeeScheduleCalc
from src.strategy.grid import GridStrategy
from src.strategy.mean_reversion import MeanReversionStrategy
from src.strategy.trend_following import TrendFollowingStrategy
from src.strategy.meme_scalper import MemeScalperStrategy
from src.utils.logger import TradeLogger, setup_logger

logger = setup_logger(__name__)


class TradingEngine:
    """Main trading orchestrator."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.event_bus = EventBus()
        self._running = False
        self._task: Optional[asyncio.Task] = None

        # Determine market type
        from src.core.constants import MarketType
        self.market_type = MarketType(config.market.type)

        # Components (built in build())
        self.exchange: Optional[ExchangeInterface] = None
        self.fee_calculator: Optional[FeeCalculator] = None
        self.strategy: Optional[BaseStrategy] = None
        self.risk_manager: Optional[RiskManager] = None
        self.circuit_breaker: Optional[CircuitBreaker] = None
        self.portfolio: Optional[PortfolioTracker] = None
        self.data_feed: Optional[DataFeedManager] = None
        self.subscription: Optional[SubscriptionManager] = None
        self.ohlcv: Optional[OHLCVStore] = None
        self.trade_logger: Optional[TradeLogger] = None

        self._build()

    def _build(self) -> None:
        """Initialize all components."""
        cfg = self.config

        # Exchange
        if cfg.exchange.id in ("paper", "backtest"):
            fee_schedule = FeeSchedule(
                maker=Decimal(str(cfg.backtest.fee_rate)),
                taker=Decimal(str(cfg.backtest.fee_rate)),
            )
            self.exchange = PaperExchange(
                fee_schedule=fee_schedule,
                initial_balances={cfg.market.quote_currency: Decimal(str(cfg.backtest.initial_capital))},
            )
        else:
            import os
            self.exchange = CCXTExchange(
                exchange_id=cfg.exchange.id,
                api_key=os.getenv(f"{cfg.exchange.id.upper()}_API_KEY", os.getenv("EXCHANGE_API_KEY", "")),
                secret=os.getenv(f"{cfg.exchange.id.upper()}_SECRET", os.getenv("EXCHANGE_SECRET", "")),
                password=os.getenv(f"{cfg.exchange.id.upper()}_PASSWORD", os.getenv("EXCHANGE_PASSWORD", "")),
                sandbox=cfg.exchange.sandbox,
                timeout=cfg.exchange.timeout,
                market_type=self.market_type,
            )

        # Fee calculator
        fee_schedule_calc = FeeScheduleCalc.spot() if self.market_type.value == "spot" else FeeScheduleCalc.futures()
        self.fee_calculator = FeeCalculator(
            fee_schedule=fee_schedule_calc,
            min_profit_buffer=Decimal(str(cfg.strategy.min_profit_buffer_pct / 100)),
        )

        # Strategy
        self.subscription = SubscriptionManager()
        self.ohlcv = OHLCVStore(max_candles=200)
        self.strategy = self._create_strategy()

        # Risk
        initial_equity = Decimal(str(cfg.backtest.initial_capital))
        self.circuit_breaker = CircuitBreaker(
            initial_equity=initial_equity,
            max_daily_loss_pct=Decimal(str(cfg.risk.max_daily_loss_pct)),
            max_drawdown_pct=Decimal(str(cfg.risk.max_drawdown_pct)),
        )
        position_sizer = PositionSizer(
            max_position_pct=Decimal(str(cfg.risk.max_position_pct)),
            max_leverage=cfg.risk.max_leverage,
        )
        self.risk_manager = RiskManager(
            circuit_breaker=self.circuit_breaker,
            position_sizer=position_sizer,
            max_concurrent_positions=cfg.risk.max_concurrent_positions,
            cooldown_seconds=cfg.risk.cooldown_seconds,
        )

        # Portfolio
        self.trade_logger = TradeLogger(cfg.logging.trade_journal)
        self.portfolio = PortfolioTracker(
            initial_equity=initial_equity,
            quote_currency=cfg.market.quote_currency,
            event_bus=self.event_bus,
            trade_logger=self.trade_logger,
        )

        # Data feed
        self.data_feed = DataFeedManager(
            exchange=self.exchange,
            subscription=self.subscription,
            ohlcv_store=self.ohlcv,
            poll_interval=5.0,
            timeframe=cfg.market.timeframe,
        )

        # Register event handlers
        self._register_handlers()

    def _create_strategy(self) -> BaseStrategy:
        cfg = self.config
        strategy_name = cfg.strategy.active

        if strategy_name == "grid":
            grid_cfg = cfg.strategy.grid
            upper = Decimal(str(grid_cfg.upper_price)) if grid_cfg.upper_price else Decimal("0")
            lower = Decimal(str(grid_cfg.lower_price)) if grid_cfg.lower_price else Decimal("0")
            # Need ticker data for auto-detect — handled at start time
            if upper == 0 or lower == 0:
                raise ValueError("Grid strategy requires upper_price and lower_price in config")

            return GridStrategy(
                market_type=self.market_type,
                fee_calculator=self.fee_calculator,
                event_bus=self.event_bus,
                upper_price=upper,
                lower_price=lower,
                grid_count=grid_cfg.grid_count,
                order_amount=Decimal(str(grid_cfg.order_amount)),
            )

        if strategy_name == "mean_reversion":
            mr_cfg = cfg.strategy.mean_reversion
            return MeanReversionStrategy(
                market_type=self.market_type,
                fee_calculator=self.fee_calculator,
                event_bus=self.event_bus,
                bb_period=mr_cfg.bb_period,
                bb_std=mr_cfg.bb_std,
                rsi_period=mr_cfg.rsi_period,
                rsi_oversold=mr_cfg.rsi_oversold,
                rsi_overbought=mr_cfg.rsi_overbought,
                confirmation_candles=mr_cfg.confirmation_candles,
            )

        if strategy_name == "trend_following":
            tf_cfg = cfg.strategy.trend_following
            return TrendFollowingStrategy(
                market_type=self.market_type,
                fee_calculator=self.fee_calculator,
                event_bus=self.event_bus,
                fast_ema=tf_cfg.fast_ema,
                slow_ema=tf_cfg.slow_ema,
                signal_ema=tf_cfg.signal_ema,
                atr_period=tf_cfg.atr_period,
                atr_multiplier=tf_cfg.atr_multiplier,
            )

        if strategy_name == "meme_scalper":
            return MemeScalperStrategy(
                market_type=self.market_type,
                fee_calculator=self.fee_calculator,
                event_bus=self.event_bus,
            )

        raise ValueError(f"Unknown strategy: {strategy_name}")

    def _register_handlers(self) -> None:
        """Wire up event handlers on the event bus."""

        async def handle_signal(signal: Signal, **kwargs) -> None:
            """Process a trading signal: validate -> execute."""
            logger.info(
                "Signal received",
                extra={
                    "symbol": signal.symbol,
                    "type": signal.signal_type.value,
                    "expected_return": f"{signal.expected_return_rate * 100:.4f}%",
                    "fee_estimate": f"{signal.fee_estimate.round_trip_pct:.4f}%" if signal.fee_estimate else "N/A",
                },
            )

            # Risk validation
            positions = await self.exchange.fetch_positions()
            balances = await self.exchange.fetch_balance()
            equity = self.circuit_breaker.current_equity

            approved, reason = await self.risk_manager.validate(
                signal=signal,
                positions=positions,
                balances=balances,
                equity=equity,
            )

            if not approved:
                logger.info("Signal rejected by risk manager", extra={"reason": reason})
                return

            # Execute order
            order = await self.exchange.create_order(
                symbol=signal.symbol,
                side=signal.order_side.value,
                order_type=signal.order_type,
                amount=signal.amount,
                price=signal.price if signal.order_type == "limit" else None,
            )

            if order.status.value == "rejected":
                logger.warning("Order rejected by exchange", extra={"order_id": order.id})
                return

            # Track in portfolio
            from src.exchange.models import Fill
            fill = Fill(
                order_id=order.id,
                symbol=order.symbol,
                side=order.side,
                price=order.price,
                amount=order.filled,
                fee=order.fee.total_fee_rate * order.price * order.filled if order.fee else Decimal("0"),
                fee_currency=self.config.market.quote_currency,
            )
            self.portfolio.on_fill(fill, order)

            # Update circuit breaker
            self.circuit_breaker.update_equity(self.circuit_breaker.current_equity)

            await self.event_bus.publish(EventType.ORDER, order=order)

        async def handle_ticker(ticker: Ticker, **kwargs) -> None:
            """Route ticker to strategy."""
            if self.strategy:
                signal = await self.strategy.on_ticker(ticker)

        self.event_bus.subscribe(EventType.SIGNAL, handle_signal)

    async def start(self) -> None:
        """Start the trading engine."""
        self._running = True
        symbols = self.config.market.symbols

        # Set up strategy symbol list
        self.strategy.set_symbols(symbols)

        # Subscribe strategy to ticker updates
        for symbol in symbols:
            self.subscription.subscribe_ticker(symbol, self._on_ticker_for_strategy)

        # Start data feed
        await self.data_feed.start(symbols)

        logger.info("Trading engine started", extra={"symbols": symbols, "strategy": self.strategy.name})

    async def _on_ticker_for_strategy(self, ticker: Ticker) -> None:
        """Ticker callback: update indicators, feed strategy, update risk."""
        # Update indicators
        indicators = self.ohlcv.calc_indicators(
            symbol=ticker.symbol,
            timeframe=self.config.market.timeframe,
            bb_period=self.config.strategy.mean_reversion.bb_period,
            bb_std=self.config.strategy.mean_reversion.bb_std,
            rsi_period=self.config.strategy.mean_reversion.rsi_period,
            atr_period=self.config.strategy.trend_following.atr_period,
        )

        # Update strategy-specific state
        if isinstance(self.strategy, MeanReversionStrategy):
            bb = indicators.get("bollinger")
            rsi_val = indicators.get("rsi")
            if bb and rsi_val is not None:
                self.strategy.set_state(
                    sma=bb.sma,
                    upper_band=bb.upper,
                    lower_band=bb.lower,
                    rsi=rsi_val,
                )
        elif isinstance(self.strategy, TrendFollowingStrategy):
            macd_result = indicators.get("macd")
            atr_val = indicators.get("atr")
            if macd_result and atr_val is not None:
                self.strategy.set_state(
                    macd=macd_result.macd,
                    macd_signal=macd_result.signal,
                    macd_histogram=macd_result.histogram,
                    atr=atr_val,
                )

        # Check positions
        positions = await self.exchange.fetch_positions()
        has_position = any(p.symbol == ticker.symbol for p in positions)
        self.strategy.set_position(has_position)

        # Feed ticker to strategy (fee gate happens inside)
        signal = await self.strategy.on_ticker(ticker)

        # Update risk with current equity
        balances = await self.exchange.fetch_balance()
        equity = self.circuit_breaker.current_equity
        if hasattr(self.exchange, 'get_equity'):
            equity = self.exchange.get_equity(self.config.market.quote_currency)
        self.circuit_breaker.update_equity(equity)

    async def stop(self) -> None:
        """Gracefully stop the trading engine."""
        self._running = False

        if self.data_feed:
            await self.data_feed.stop()

        if self.exchange:
            await self.exchange.close()

        logger.info("Trading engine stopped",
                     extra={"total_trades": self.portfolio.trade_count,
                            "total_fees": str(self.portfolio.total_fees)})

    @property
    def is_running(self) -> bool:
        return self._running

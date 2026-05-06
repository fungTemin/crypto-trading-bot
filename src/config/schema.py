from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, field_validator


class ExchangeConfig(BaseModel):
    id: str = "binance"
    sandbox: bool = True
    timeout: int = Field(default=30000, ge=1000, le=120000)
    rate_limit: int = Field(default=100, ge=1, le=1000)


class MarketConfig(BaseModel):
    type: str = "spot"
    symbols: list[str] = Field(default=["BTC/USDT"])
    quote_currency: str = "USDT"
    timeframe: str = "5m"

    @field_validator("type")
    @classmethod
    def validate_market_type(cls, v: str) -> str:
        valid = {"spot", "future", "swap"}
        if v not in valid:
            raise ValueError(f"market type must be one of {valid}")
        return v


class GridConfig(BaseModel):
    upper_price: Optional[float] = None
    lower_price: Optional[float] = None
    grid_count: int = Field(default=20, ge=3, le=200)
    order_amount: float = Field(default=100, gt=0)


class MeanReversionConfig(BaseModel):
    bb_period: int = Field(default=20, ge=5)
    bb_std: float = Field(default=2.0, ge=0.5, le=5.0)
    rsi_period: int = Field(default=14, ge=5)
    rsi_oversold: int = Field(default=30, ge=10, le=50)
    rsi_overbought: int = Field(default=70, ge=50, le=90)
    confirmation_candles: int = Field(default=2, ge=1, le=10)


class TrendFollowingConfig(BaseModel):
    fast_ema: int = Field(default=12, ge=3)
    slow_ema: int = Field(default=26, ge=5)
    signal_ema: int = Field(default=9, ge=3)
    atr_period: int = Field(default=14, ge=5)
    atr_multiplier: float = Field(default=2.0, ge=0.5, le=10.0)


class StrategyConfig(BaseModel):
    active: str = "grid"
    min_profit_buffer_pct: float = Field(default=0.05, ge=0.0)
    grid: GridConfig = GridConfig()
    mean_reversion: MeanReversionConfig = MeanReversionConfig()
    trend_following: TrendFollowingConfig = TrendFollowingConfig()


class RiskConfig(BaseModel):
    max_position_pct: float = Field(default=10, gt=0, le=100)
    max_leverage: int = Field(default=1, ge=1, le=125)
    max_daily_loss_pct: float = Field(default=5, gt=0, le=100)
    max_drawdown_pct: float = Field(default=20, gt=0, le=100)
    cooldown_seconds: int = Field(default=60, ge=0)
    max_concurrent_positions: int = Field(default=5, ge=1, le=50)


class BacktestConfig(BaseModel):
    start_date: str = "2025-01-01"
    end_date: str = "2026-01-01"
    initial_capital: float = Field(default=10000, gt=0)
    fee_rate: float = Field(default=0.001, ge=0.0, le=0.05)
    slippage_rate: float = Field(default=0.0005, ge=0.0, le=0.05)


class LoggingConfig(BaseModel):
    level: str = "INFO"
    format: str = "json"
    trade_journal: str = "data/logs/trades.csv"
    operation_log: str = "data/logs/operations.jsonl"


class AppConfig(BaseModel):
    exchange: ExchangeConfig = ExchangeConfig()
    market: MarketConfig = MarketConfig()
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskConfig = RiskConfig()
    backtest: BacktestConfig = BacktestConfig()
    logging: LoggingConfig = LoggingConfig()

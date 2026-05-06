"""Technical indicators implemented in pure Python.

All calculations work on lists of Decimal values.
No external TA library dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from math import sqrt
from typing import Optional


def decimal_sqrt(value: Decimal) -> Decimal:
    """Decimal square root using float conversion (adequate for price data)."""
    return Decimal(str(sqrt(float(value))))


@dataclass
class BollingerResult:
    sma: Decimal
    upper: Decimal
    lower: Decimal
    width: Decimal   # (upper - lower) / sma — measures volatility


@dataclass
class MACDResult:
    macd: Decimal
    signal: Decimal
    histogram: Decimal  # macd - signal


def sma(prices: list[Decimal], period: int) -> Optional[Decimal]:
    """Simple Moving Average."""
    if len(prices) < period:
        return None
    return sum(prices[-period:]) / period


def ema(prices: list[Decimal], period: int) -> Optional[Decimal]:
    """Exponential Moving Average."""
    if len(prices) < period:
        return None

    # SMA as seed for first EMA
    seed = sum(prices[:period]) / period
    multiplier = Decimal("2") / (period + 1)

    ema_val = seed
    for price in prices[period:]:
        ema_val = (price - ema_val) * multiplier + ema_val

    return ema_val


def ema_series(prices: list[Decimal], period: int) -> list[Optional[Decimal]]:
    """Calculate EMA for each position in the price series."""
    result: list[Optional[Decimal]] = [None] * len(prices)

    if len(prices) < period:
        return result

    multiplier = Decimal("2") / (period + 1)
    seed = sum(prices[:period]) / period
    result[period - 1] = seed

    ema_val = seed
    for i, price in enumerate(prices[period:], start=period):
        ema_val = (price - ema_val) * multiplier + ema_val
        result[i] = ema_val

    return result


def rsi(prices: list[Decimal], period: int = 14) -> Optional[Decimal]:
    """Relative Strength Index."""
    if len(prices) < period + 1:
        return None

    gains: list[Decimal] = []
    losses: list[Decimal] = []

    for i in range(-period, 0):
        diff = prices[i] - prices[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(Decimal("0"))
        else:
            gains.append(Decimal("0"))
            losses.append(abs(diff))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        return Decimal("100")

    rs = avg_gain / avg_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + rs))


def bollinger_bands(prices: list[Decimal], period: int = 20, std_dev: float = 2.0) -> Optional[BollingerResult]:
    """Bollinger Bands: middle = SMA, upper/lower = SMA ± std_dev * stdev."""
    if len(prices) < period:
        return None

    mean = sum(prices[-period:]) / period
    variance = sum((p - mean) ** 2 for p in prices[-period:]) / period
    stdev = decimal_sqrt(variance)

    std_mult = Decimal(str(std_dev))
    upper = mean + stdev * std_mult
    lower = mean - stdev * std_mult

    width = (upper - lower) / mean if mean > 0 else Decimal("0")

    return BollingerResult(sma=mean, upper=upper, lower=lower, width=width)


def atr(
    highs: list[Decimal],
    lows: list[Decimal],
    closes: list[Decimal],
    period: int = 14,
) -> Optional[Decimal]:
    """Average True Range."""
    if len(closes) < period + 1:
        return None

    true_ranges: list[Decimal] = []
    for i in range(-period, 0):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(low - prev_close),
        )
        true_ranges.append(tr)

    return sum(true_ranges) / period


def macd(
    prices: list[Decimal],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Optional[MACDResult]:
    """MACD = EMA(fast) - EMA(slow), Signal = EMA(MACD, signal), Histogram = MACD - Signal."""
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)

    if fast_ema is None or slow_ema is None:
        return None

    # To compute signal line we need a series of MACD values
    # Simplified: compute MACD for recent values
    macd_series: list[Decimal] = []
    fast_series = ema_series(prices, fast)
    slow_series = ema_series(prices, slow)

    for f, s in zip(fast_series, slow_series):
        if f is not None and s is not None:
            macd_series.append(f - s)

    macd_val = fast_ema - slow_ema
    signal_val = ema(macd_series, signal) if len(macd_series) >= signal else None

    if signal_val is None:
        return None

    histogram = macd_val - signal_val

    return MACDResult(macd=macd_val, signal=signal_val, histogram=histogram)


def compute_all(
    closes: list[Decimal],
    highs: list[Decimal],
    lows: list[Decimal],
    bb_period: int = 20,
    bb_std: float = 2.0,
    rsi_period: int = 14,
    atr_period: int = 14,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
) -> dict:
    """Compute all indicators in one pass. Returns dict of Optional[result]."""
    return {
        "sma": sma(closes, bb_period),
        "bollinger": bollinger_bands(closes, bb_period, bb_std),
        "rsi": rsi(closes, rsi_period),
        "atr": atr(highs, lows, closes, atr_period),
        "macd": macd(closes, macd_fast, macd_slow, macd_signal),
    }

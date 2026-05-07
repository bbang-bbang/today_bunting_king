"""변동성 지표: Bollinger Bands, ATR, Keltner, Donchian."""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.indicators.trend import _true_range


def bollinger_bands(
    close: pd.Series,
    period: int = 20,
    std_mult: float = 2.0,
) -> pd.DataFrame:
    """Bollinger Bands + %B + bandwidth.

    - middle = SMA(close, period)
    - upper = middle + std_mult * std
    - lower = middle - std_mult * std
    - %B    = (close - lower) / (upper - lower)      (0~1 정상)
    - bandwidth = (upper - lower) / middle           (스퀴즈 감지)
    """
    middle = close.rolling(period).mean()
    std = close.rolling(period).std(ddof=0)
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    width = (upper - lower).replace(0, np.nan)
    percent_b = (close - lower) / width
    bandwidth = width / middle
    return pd.DataFrame({
        "middle": middle,
        "upper": upper,
        "lower": lower,
        "percent_b": percent_b,
        "bandwidth": bandwidth,
    })


def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Wilder ATR (True Range 의 RMA)."""
    tr = _true_range(high, low, close)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def keltner_channel(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
    atr_mult: float = 2.0,
) -> pd.DataFrame:
    """Keltner Channel — EMA ± ATR 배수."""
    middle = close.ewm(span=period, adjust=False).mean()
    atr_val = atr(high, low, close, period)
    upper = middle + atr_mult * atr_val
    lower = middle - atr_mult * atr_val
    return pd.DataFrame({"middle": middle, "upper": upper, "lower": lower})


def donchian_channel(
    high: pd.Series,
    low: pd.Series,
    period: int = 20,
) -> pd.DataFrame:
    """Donchian Channel — 기간 내 최고/최저."""
    upper = high.rolling(period).max()
    lower = low.rolling(period).min()
    middle = (upper + lower) / 2
    return pd.DataFrame({"upper": upper, "middle": middle, "lower": lower})


def historical_volatility(close: pd.Series, period: int = 20) -> pd.Series:
    """로그 수익률의 연환산 표준편차 (252영업일 기준)."""
    log_ret = (close / close.shift(1)).apply(lambda x: 0 if x is None or x <= 0 else np.log(x))
    return log_ret.rolling(period).std(ddof=0) * (252 ** 0.5)

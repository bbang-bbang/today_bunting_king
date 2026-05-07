"""모멘텀 지표: RSI, Stochastic, CCI, Williams %R, ROC."""
from __future__ import annotations

import numpy as np
import pandas as pd


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI (smoothed via alpha = 1/period)."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k: int = 14,
    d: int = 3,
    slowing: int = 3,
) -> pd.DataFrame:
    """Slow Stochastic Oscillator (Slow %K, %D)."""
    lowest = low.rolling(k).min()
    highest = high.rolling(k).max()
    fast_k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    slow_k = fast_k.rolling(slowing).mean()
    slow_d = slow_k.rolling(d).mean()
    return pd.DataFrame({"slow_k": slow_k, "slow_d": slow_d})


def stoch_rsi(close: pd.Series, period: int = 14, k: int = 3, d: int = 3) -> pd.DataFrame:
    """Stochastic RSI — RSI 값에 대해 Stochastic 적용."""
    r = rsi(close, period)
    lowest = r.rolling(period).min()
    highest = r.rolling(period).max()
    stoch_rsi_line = 100 * (r - lowest) / (highest - lowest).replace(0, np.nan)
    kk = stoch_rsi_line.rolling(k).mean()
    dd = kk.rolling(d).mean()
    return pd.DataFrame({"stoch_rsi": stoch_rsi_line, "k": kk, "d": dd})


def cci(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 20,
) -> pd.Series:
    """Commodity Channel Index.

    표준 공식: MD = 각 rolling 윈도우의 "평균 절대 편차".
    (tp - sma_tp).abs().rolling(p).mean() 는 근사이며 앞쪽 NaN 과다.
    여기선 rolling apply 로 정확한 MD 를 계산.
    """
    tp = (high + low + close) / 3
    sma_tp = tp.rolling(period).mean()
    mean_dev = tp.rolling(period).apply(
        lambda x: abs(x - x.mean()).mean(),
        raw=True,
    )
    return (tp - sma_tp) / (0.015 * mean_dev.replace(0, np.nan))


def williams_r(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Williams %R — [-100, 0] 범위, 과매도 -80 이하, 과매수 -20 이상."""
    highest = high.rolling(period).max()
    lowest = low.rolling(period).min()
    return -100 * (highest - close) / (highest - lowest).replace(0, np.nan)


def roc(close: pd.Series, period: int = 10) -> pd.Series:
    """Rate of Change (%)."""
    return (close / close.shift(period) - 1) * 100


def momentum_raw(close: pd.Series, period: int = 10) -> pd.Series:
    """가격 모멘텀 (현재가 - N일 전 가격)."""
    return close - close.shift(period)

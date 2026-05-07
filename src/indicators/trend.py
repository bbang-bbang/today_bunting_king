"""추세 지표: SMA, EMA, WMA, MACD, ADX+DMI, Ichimoku."""
from __future__ import annotations

import numpy as np
import pandas as pd


# ============================================================
# 이동평균
# ============================================================

def sma(close: pd.Series, period: int) -> pd.Series:
    """단순이동평균."""
    return close.rolling(window=period, min_periods=period).mean()


def ema(close: pd.Series, period: int) -> pd.Series:
    """지수이동평균 (adjust=False: 전통적 정의)."""
    return close.ewm(span=period, adjust=False).mean()


def wma(close: pd.Series, period: int) -> pd.Series:
    """가중이동평균 (최근일 가중치 최대)."""
    weights = pd.Series(range(1, period + 1), dtype=float)
    return close.rolling(window=period, min_periods=period).apply(
        lambda w: (pd.Series(w) * weights.values).sum() / weights.sum(),
        raw=True,
    )


# ============================================================
# MACD
# ============================================================

def macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    """MACD = EMA(fast) - EMA(slow), Signal = EMA(MACD, signal), Hist = MACD - Signal."""
    fast_ema = ema(close, fast)
    slow_ema = ema(close, slow)
    macd_line = fast_ema - slow_ema
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return pd.DataFrame({"macd": macd_line, "signal": signal_line, "hist": hist})


# ============================================================
# True Range (ATR/ADX 공용)
# ============================================================

def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr


# ============================================================
# ADX / DMI
# ============================================================

def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14,
) -> pd.DataFrame:
    """Wilder ADX + +DI / -DI."""
    up_move = high.diff()
    down_move = -low.diff()

    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    tr = _true_range(high, low, close)
    atr_ = tr.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr_

    di_sum = plus_di + minus_di
    dx = 100 * (plus_di - minus_di).abs() / di_sum.replace(0, np.nan)
    adx_line = dx.ewm(alpha=1 / period, adjust=False).mean()

    return pd.DataFrame({"plus_di": plus_di, "minus_di": minus_di, "adx": adx_line})


# ============================================================
# Ichimoku (일목균형표)
# ============================================================

def ichimoku(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    tenkan: int = 9,
    kijun: int = 26,
    senkou_b_period: int = 52,
    shift: int = 26,
) -> pd.DataFrame:
    """Ichimoku Kinko Hyo.

    - Tenkan (전환선): (High9 + Low9) / 2
    - Kijun (기준선):  (High26 + Low26) / 2
    - Senkou A (선행스팬 1): (Tenkan + Kijun) / 2, +26 시프트
    - Senkou B (선행스팬 2): (High52 + Low52) / 2, +26 시프트
    - Chikou (후행스팬): Close, -26 시프트
    """
    tenkan_line = (high.rolling(tenkan).max() + low.rolling(tenkan).min()) / 2
    kijun_line = (high.rolling(kijun).max() + low.rolling(kijun).min()) / 2
    senkou_a = ((tenkan_line + kijun_line) / 2).shift(shift)
    senkou_b = (
        (high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2
    ).shift(shift)
    chikou = close.shift(-shift)

    return pd.DataFrame({
        "tenkan": tenkan_line,
        "kijun": kijun_line,
        "senkou_a": senkou_a,
        "senkou_b": senkou_b,
        "chikou": chikou,
    })

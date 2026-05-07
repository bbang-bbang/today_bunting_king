"""거래량 지표: OBV, MFI, Volume SMA/Ratio."""
from __future__ import annotations

import numpy as np
import pandas as pd


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume — 상승일 +volume, 하락일 -volume 누적."""
    direction = np.sign(close.diff().fillna(0))
    signed_volume = direction * volume
    return signed_volume.cumsum()


def mfi(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
    period: int = 14,
) -> pd.Series:
    """Money Flow Index — 거래량 가중 RSI."""
    tp = (high + low + close) / 3
    money_flow = tp * volume
    delta = tp.diff()

    positive = money_flow.where(delta > 0, 0.0)
    negative = money_flow.where(delta < 0, 0.0)

    pos_sum = positive.rolling(period).sum()
    neg_sum = negative.rolling(period).sum().replace(0, np.nan)
    ratio = pos_sum / neg_sum
    return 100 - 100 / (1 + ratio)


def volume_sma(volume: pd.Series, period: int = 20) -> pd.Series:
    """거래량 단순이동평균."""
    return volume.rolling(period).mean()


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """당일 거래량 / 기간 평균 거래량. 1.0 = 평균, 2.0 = 2배 급증."""
    avg = volume_sma(volume, period).replace(0, np.nan)
    return volume / avg


def accumulation_distribution(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Accumulation/Distribution Line (A/D)."""
    clv_denom = (high - low).replace(0, np.nan)
    clv = ((close - low) - (high - close)) / clv_denom
    return (clv * volume).fillna(0).cumsum()

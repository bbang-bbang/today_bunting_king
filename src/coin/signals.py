"""코인 시그널 v1 — 단순 모멘텀.

자동 매수 결정의 근거. 사람 필터 없으니 보수적·정량적 패턴 사용.

기본 시그널 (and 조건):
  1) RSI(14) <= 35 에서 직전 캔들 대비 반등 (과매도 → 회복)
  2) EMA(12) > EMA(26) (단기 우상향)
  3) 거래량 > 직전 20캔들 평균의 1.2배 (돌파 거래량)
"""
from __future__ import annotations

import pandas as pd


def _rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def precompute_signals(
    df: pd.DataFrame,
    rsi_threshold: float = 40.0,
    vol_multiplier: float = 1.0,
    require_ema_uptrend: bool = False,
) -> pd.Series:
    """전체 df 에 대해 매수 시그널 boolean Series 미리 계산 (벡터화).

    백테스트에서 매 캔들마다 momentum_signal(sub) 호출하면 O(N²) → 이 함수로 O(N).
    Returns Series 같은 길이, True = "buy" 시그널 발동.
    """
    if len(df) < 30:
        return pd.Series([False] * len(df), index=df.index)

    close = df["close"]
    volume = df["volume"]
    rsi = _rsi(close, 14)
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    avg_vol_20 = volume.rolling(window=20).mean().shift(1)   # 직전 20캔들 평균

    cond_rsi = (rsi.shift(1) <= rsi_threshold) & (rsi > rsi.shift(1))
    cond_vol = volume > avg_vol_20 * vol_multiplier
    cond_ema = (ema12 > ema26) if require_ema_uptrend else pd.Series([True] * len(df), index=df.index)

    out = cond_rsi & cond_vol & cond_ema
    out = out.fillna(False)
    # 처음 30 캔들 워밍업은 강제 False
    out.iloc[:30] = False
    return out


def momentum_signal(
    df: pd.DataFrame,
    rsi_threshold: float = 40.0,
    vol_multiplier: float = 1.0,
    require_ema_uptrend: bool = False,
) -> str | None:
    """현재 캔들(마지막 행) 기준 매수 시그널.

    완화된 v1.5 (2026-05-04): 90일 BTC/ETH 백테스트에서 거래 발생하도록 임계 조정.
      - RSI <= 40 (was 35) — 과매도 빈도 ↑
      - vol > avg * 1.0 (was 1.2) — 거의 "평균 이상" 만으로 통과
      - EMA 우상향은 옵션 (기본 False — 진입 빈도 ↑)

    Returns "buy" if 모든 활성 조건 만족, else None.
    """
    if len(df) < 30:
        return None

    close = df["close"]
    volume = df["volume"]

    rsi = _rsi(close, 14)
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)

    last = -1
    prev = -2

    # 조건 1: RSI 과매도 회복 (RSI <= threshold 였다가 반등)
    cond_rsi = (
        rsi.iloc[prev] <= rsi_threshold
        and rsi.iloc[last] > rsi.iloc[prev]
    )

    # 조건 2: 거래량 > 직전 20 평균 * multiplier
    avg_vol_20 = volume.iloc[-21:-1].mean()
    cond_vol = volume.iloc[last] > avg_vol_20 * vol_multiplier

    # 조건 3 (옵션): EMA12 > EMA26
    cond_ema = (
        ema12.iloc[last] > ema26.iloc[last]
        if require_ema_uptrend else True
    )

    if cond_rsi and cond_vol and cond_ema:
        return "buy"
    return None


# 테스트·디버그용 — 시그널 컴포넌트 분해
def debug_signal_components(df: pd.DataFrame) -> dict:
    if len(df) < 30:
        return {"insufficient_data": True}
    close = df["close"]
    volume = df["volume"]
    rsi = _rsi(close, 14)
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    avg_vol_20 = volume.iloc[-21:-1].mean()
    last = -1
    prev = -2
    return {
        "rsi_now": float(rsi.iloc[last]),
        "rsi_prev": float(rsi.iloc[prev]),
        "ema12": float(ema12.iloc[last]),
        "ema26": float(ema26.iloc[last]),
        "ema12_gt_ema26": bool(ema12.iloc[last] > ema26.iloc[last]),
        "volume_now": float(volume.iloc[last]),
        "avg_vol_20": float(avg_vol_20),
        "vol_ratio": float(volume.iloc[last] / max(avg_vol_20, 1e-10)),
        "buy_signal": momentum_signal(df) == "buy",
    }

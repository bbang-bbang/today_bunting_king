"""앙상블 시그널 사전 계산 — 백테스트용.

매 캔들마다 ensemble_evaluate 호출하면 O(N²) 이라 느림.
이 모듈은 **벡터화된** technical 점수 + (이미 시계열인) sentiment·arb 점수를
한 번에 계산하고 가중 합산해서 boolean Series 반환.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from src.coin.signals import _rsi, _ema


def precompute_ensemble_signals(
    ohlcv: pd.DataFrame,
    fng_hourly: pd.Series | None = None,    # 시간봉 인덱스에 맞춘 FNG
    kp_hourly: pd.Series | None = None,     # 시간봉 김프
    threshold: float = 0.5,
    weights: dict | None = None,            # {"technical": 1.0, "sentiment": 0.8, "arbitrage": 0.6}
) -> pd.DataFrame:
    """캔들마다 앙상블 점수 + buy 여부 계산.

    Returns DataFrame with cols:
      tech_score, sent_score, arb_score, total_score, buy(bool)
    """
    if weights is None:
        weights = {"technical": 1.0, "sentiment": 0.8, "arbitrage": 0.6}

    n = len(ohlcv)
    out = pd.DataFrame(index=ohlcv.index)

    # ----- Technical (벡터화) -----
    close = ohlcv["close"]
    volume = ohlcv["volume"]
    rsi = _rsi(close, 14)
    ema12 = _ema(close, 12)
    ema26 = _ema(close, 26)
    avg_vol_20 = volume.rolling(20).mean().shift(1)
    vol_ratio = volume / avg_vol_20.replace(0, np.nan)

    tech = pd.Series(0.0, index=ohlcv.index)
    # RSI 회복: prev <=35 and now > prev → +0.4
    tech += np.where((rsi.shift(1) <= 35) & (rsi > rsi.shift(1)), 0.4, 0.0)
    # RSI 과매수: now >=75 → -0.3
    tech += np.where(rsi >= 75, -0.3, 0.0)
    # EMA 우상향 → +0.3, 그 반대 -0.2
    tech += np.where(ema12 > ema26, 0.3, -0.2)
    # 거래량 폭발 → +0.3, 너무 적음 -0.1
    tech += np.where(vol_ratio >= 1.5, 0.3, 0.0)
    tech += np.where(vol_ratio <= 0.5, -0.1, 0.0)
    tech = tech.clip(-1.0, 1.0)
    out["tech_score"] = tech

    # ----- Sentiment (FNG → -1~+1) -----
    if fng_hourly is not None:
        sent = (50 - fng_hourly.astype(float)) / 50.0
        sent = sent.clip(-1.0, 1.0)
    else:
        sent = pd.Series(0.0, index=ohlcv.index)
    out["sent_score"] = sent

    # ----- Arbitrage (KP) -----
    if kp_hourly is not None:
        # 7일(168h) rolling avg/std 로 z-score
        kp_avg = kp_hourly.rolling(168).mean()
        kp_std = kp_hourly.rolling(168).std()
        z = (kp_hourly - kp_avg) / kp_std.replace(0, np.nan)
        arb = (-z * 0.3).clip(-0.8, 0.8)
        # 절대 임계 — 5% 이상은 강제 -0.5
        arb = arb.where(kp_hourly < 5.0, -0.5)
        arb = arb.fillna(0.0)
    else:
        arb = pd.Series(0.0, index=ohlcv.index)
    out["arb_score"] = arb

    # ----- 가중 합산 -----
    w_tech = weights.get("technical", 1.0)
    w_sent = weights.get("sentiment", 0.0 if fng_hourly is None else 0.8)
    w_arb = weights.get("arbitrage", 0.0 if kp_hourly is None else 0.6)
    w_total = w_tech + w_sent + w_arb
    if w_total == 0:
        out["total_score"] = 0.0
    else:
        out["total_score"] = (
            tech * w_tech + sent * w_sent + arb * w_arb
        ) / w_total

    # 워밍업 30 캔들은 buy False 강제
    out["buy"] = out["total_score"] >= threshold
    out.iloc[:30, out.columns.get_loc("buy")] = False
    return out

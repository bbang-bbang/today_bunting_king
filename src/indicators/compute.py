"""종목 OHLCV → 모든 지표 DataFrame 오케스트레이터.

사용:
  from src.indicators import load_ohlcv, compute_all
  df = load_ohlcv("005930")
  enriched = compute_all(df)
  last_row = enriched.iloc[-1]    # 최신일 전 지표 값
"""
from __future__ import annotations

import pandas as pd

from src.indicators import momentum, trend, volatility, volume

# MA 기본 기간
_MA_PERIODS = (5, 10, 20, 60, 120)


def compute_all(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """OHLCV DataFrame (open/high/low/close/volume) → 전 지표 포함 DataFrame."""
    required = {"open", "high", "low", "close", "volume"}
    missing = required - set(ohlcv.columns)
    if missing:
        raise ValueError(f"필수 컬럼 누락: {missing}")

    out = ohlcv.copy()
    o, h, l, c, v = out["open"], out["high"], out["low"], out["close"], out["volume"]

    # ---- Trend ----
    for p in _MA_PERIODS:
        out[f"sma_{p}"] = trend.sma(c, p)
        out[f"ema_{p}"] = trend.ema(c, p)

    macd_df = trend.macd(c, 12, 26, 9)
    out["macd"] = macd_df["macd"]
    out["macd_signal"] = macd_df["signal"]
    out["macd_hist"] = macd_df["hist"]

    adx_df = trend.adx(h, l, c, 14)
    out["plus_di"] = adx_df["plus_di"]
    out["minus_di"] = adx_df["minus_di"]
    out["adx"] = adx_df["adx"]

    ichi = trend.ichimoku(h, l, c)
    for col in ichi.columns:
        out[f"ichi_{col}"] = ichi[col]

    # ---- Momentum ----
    out["rsi_14"] = momentum.rsi(c, 14)

    stoch_df = momentum.stochastic(h, l, c, 14, 3, 3)
    out["stoch_k"] = stoch_df["slow_k"]
    out["stoch_d"] = stoch_df["slow_d"]

    stoch_rsi_df = momentum.stoch_rsi(c, 14, 3, 3)
    out["stoch_rsi"] = stoch_rsi_df["stoch_rsi"]
    out["stoch_rsi_k"] = stoch_rsi_df["k"]
    out["stoch_rsi_d"] = stoch_rsi_df["d"]

    out["cci_20"] = momentum.cci(h, l, c, 20)
    out["willr_14"] = momentum.williams_r(h, l, c, 14)
    out["roc_10"] = momentum.roc(c, 10)
    out["momentum_10"] = momentum.momentum_raw(c, 10)

    # ---- Volatility ----
    bb = volatility.bollinger_bands(c, 20, 2.0)
    for col in bb.columns:
        out[f"bb_{col}"] = bb[col]

    out["atr_14"] = volatility.atr(h, l, c, 14)

    kel = volatility.keltner_channel(h, l, c, 20, 2.0)
    for col in kel.columns:
        out[f"kel_{col}"] = kel[col]

    don = volatility.donchian_channel(h, l, 20)
    for col in don.columns:
        out[f"don_{col}"] = don[col]

    out["hv_20"] = volatility.historical_volatility(c, 20)

    # ---- Volume ----
    out["obv"] = volume.obv(c, v)
    out["mfi_14"] = volume.mfi(h, l, c, v, 14)
    out["vol_sma_20"] = volume.volume_sma(v, 20)
    out["vol_ratio_20"] = volume.volume_ratio(v, 20)
    out["ad"] = volume.accumulation_distribution(h, l, c, v)

    return out

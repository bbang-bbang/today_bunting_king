"""기술적 지표 테스트.

합성 데이터로 각 지표의 기본 sanity 검증 +
실제 DB에 수집된 데이터가 있으면 end-to-end smoke test.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.indicators import momentum, trend, volatility
from src.indicators import volume as vol
from src.indicators.compute import compute_all


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def uptrend_ohlcv():
    """30일 꾸준한 상승 추세 합성 데이터."""
    dates = pd.date_range("2026-01-01", periods=30, freq="D")
    close = pd.Series([
        100, 102, 101, 103, 105, 104, 106, 108, 107, 110,
        112, 111, 113, 115, 114, 116, 118, 117, 120, 119,
        121, 123, 122, 124, 126, 125, 127, 129, 128, 130,
    ], index=dates, dtype=float)
    high = close + 2
    low = close - 2
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(range(100, 130), index=dates, dtype=float) * 1000
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": volume
    })


@pytest.fixture
def long_ohlcv():
    """120일 합성 데이터 (120 MA, Ichimoku 52+26 시프트 계산 가능)."""
    import numpy as np
    rng = np.random.default_rng(seed=42)
    dates = pd.date_range("2025-01-01", periods=200, freq="D")
    returns = rng.normal(0.001, 0.02, len(dates))
    close = pd.Series(100 * (1 + returns).cumprod(), index=dates)
    high = close * (1 + rng.uniform(0, 0.015, len(dates)))
    low = close * (1 - rng.uniform(0, 0.015, len(dates)))
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(rng.integers(50_000, 500_000, len(dates)), index=dates, dtype=float)
    return pd.DataFrame({
        "open": open_, "high": high, "low": low, "close": close, "volume": volume
    })


# ============================================================
# Trend
# ============================================================

def test_sma_value(uptrend_ohlcv):
    s = trend.sma(uptrend_ohlcv["close"], 5)
    # 첫 4개는 NaN, index 4 는 (100+102+101+103+105)/5 = 102.2
    assert pd.isna(s.iloc[3])
    assert s.iloc[4] == pytest.approx(102.2)


def test_ema_no_nan_after_period(uptrend_ohlcv):
    s = trend.ema(uptrend_ohlcv["close"], 10)
    # adjust=False 이므로 첫 값부터 유효
    assert not pd.isna(s.iloc[0])


def test_wma_last_value(uptrend_ohlcv):
    s = trend.wma(uptrend_ohlcv["close"], 5)
    assert not pd.isna(s.iloc[-1])


def test_macd_columns(uptrend_ohlcv):
    m = trend.macd(uptrend_ohlcv["close"])
    assert set(m.columns) == {"macd", "signal", "hist"}
    # 상승 추세 → MACD > 0 (후반부)
    assert m["macd"].iloc[-1] > 0


def test_adx_columns(uptrend_ohlcv):
    a = trend.adx(uptrend_ohlcv["high"], uptrend_ohlcv["low"], uptrend_ohlcv["close"])
    assert set(a.columns) == {"plus_di", "minus_di", "adx"}


def test_adx_uptrend_plus_di_dominates(uptrend_ohlcv):
    a = trend.adx(uptrend_ohlcv["high"], uptrend_ohlcv["low"], uptrend_ohlcv["close"])
    # 상승 추세 → +DI > -DI
    assert a["plus_di"].iloc[-1] > a["minus_di"].iloc[-1]


def test_ichimoku_shape(long_ohlcv):
    i = trend.ichimoku(long_ohlcv["high"], long_ohlcv["low"], long_ohlcv["close"])
    assert set(i.columns) == {"tenkan", "kijun", "senkou_a", "senkou_b", "chikou"}


# ============================================================
# Momentum
# ============================================================

def test_rsi_uptrend(uptrend_ohlcv):
    r = momentum.rsi(uptrend_ohlcv["close"])
    # 꾸준한 상승 → RSI > 60
    assert r.iloc[-1] > 60


def test_rsi_range(long_ohlcv):
    r = momentum.rsi(long_ohlcv["close"]).dropna()
    assert (r >= 0).all() and (r <= 100).all()


def test_stochastic_range(long_ohlcv):
    s = momentum.stochastic(long_ohlcv["high"], long_ohlcv["low"], long_ohlcv["close"]).dropna()
    assert (s["slow_k"] >= -1).all() and (s["slow_k"] <= 101).all()
    assert (s["slow_d"] >= -1).all() and (s["slow_d"] <= 101).all()


def test_cci_not_null(uptrend_ohlcv):
    c = momentum.cci(uptrend_ohlcv["high"], uptrend_ohlcv["low"], uptrend_ohlcv["close"], 20)
    assert not pd.isna(c.iloc[-1])


def test_williams_r_range(long_ohlcv):
    w = momentum.williams_r(long_ohlcv["high"], long_ohlcv["low"], long_ohlcv["close"]).dropna()
    assert (w >= -100).all() and (w <= 0).all()


def test_roc(uptrend_ohlcv):
    r = momentum.roc(uptrend_ohlcv["close"], 10)
    # 10일 전 대비 상승 → 양수
    assert r.iloc[-1] > 0


def test_stoch_rsi_columns(long_ohlcv):
    sr = momentum.stoch_rsi(long_ohlcv["close"])
    assert set(sr.columns) == {"stoch_rsi", "k", "d"}


# ============================================================
# Volatility
# ============================================================

def test_bollinger_ordering(long_ohlcv):
    bb = volatility.bollinger_bands(long_ohlcv["close"], 20, 2.0).dropna()
    assert (bb["lower"] <= bb["middle"]).all()
    assert (bb["middle"] <= bb["upper"]).all()


def test_bollinger_percent_b_mostly_in_range(long_ohlcv):
    bb = volatility.bollinger_bands(long_ohlcv["close"], 20, 2.0).dropna()
    # %B 는 보통 [0, 1] 범위, 극단값은 나올 수 있음
    assert (bb["percent_b"] >= -0.5).all()
    assert (bb["percent_b"] <= 1.5).all()


def test_atr_positive(long_ohlcv):
    a = volatility.atr(long_ohlcv["high"], long_ohlcv["low"], long_ohlcv["close"]).dropna()
    assert (a > 0).all()


def test_keltner_ordering(long_ohlcv):
    k = volatility.keltner_channel(long_ohlcv["high"], long_ohlcv["low"], long_ohlcv["close"]).dropna()
    assert (k["lower"] <= k["middle"]).all()
    assert (k["middle"] <= k["upper"]).all()


def test_donchian_ordering(long_ohlcv):
    d = volatility.donchian_channel(long_ohlcv["high"], long_ohlcv["low"]).dropna()
    assert (d["lower"] <= d["middle"]).all()
    assert (d["middle"] <= d["upper"]).all()


# ============================================================
# Volume
# ============================================================

def test_obv_length(uptrend_ohlcv):
    o = vol.obv(uptrend_ohlcv["close"], uptrend_ohlcv["volume"])
    assert len(o) == len(uptrend_ohlcv)


def test_obv_uptrend_positive(uptrend_ohlcv):
    o = vol.obv(uptrend_ohlcv["close"], uptrend_ohlcv["volume"])
    # 상승일 우세 → OBV 증가 추세
    assert o.iloc[-1] > o.iloc[5]


def test_mfi_range(long_ohlcv):
    m = vol.mfi(
        long_ohlcv["high"], long_ohlcv["low"],
        long_ohlcv["close"], long_ohlcv["volume"], 14,
    ).dropna()
    assert (m >= 0).all() and (m <= 100).all()


def test_volume_ratio_average_near_one(long_ohlcv):
    vr = vol.volume_ratio(long_ohlcv["volume"], 20).dropna()
    # 장기 평균은 대체로 1.0 주변
    assert 0.5 < vr.mean() < 1.5


# ============================================================
# compute_all 통합
# ============================================================

def test_compute_all_columns(long_ohlcv):
    result = compute_all(long_ohlcv)
    expected = {
        # Trend
        "sma_5", "sma_20", "sma_60", "ema_10", "ema_20",
        "macd", "macd_signal", "macd_hist",
        "plus_di", "minus_di", "adx",
        "ichi_tenkan", "ichi_kijun", "ichi_senkou_a", "ichi_senkou_b", "ichi_chikou",
        # Momentum
        "rsi_14", "stoch_k", "stoch_d", "stoch_rsi", "cci_20", "willr_14",
        "roc_10", "momentum_10",
        # Volatility
        "bb_middle", "bb_upper", "bb_lower", "bb_percent_b", "bb_bandwidth",
        "atr_14", "kel_middle", "kel_upper", "kel_lower",
        "don_upper", "don_middle", "don_lower", "hv_20",
        # Volume
        "obv", "mfi_14", "vol_sma_20", "vol_ratio_20", "ad",
    }
    assert expected <= set(result.columns), \
        f"누락: {expected - set(result.columns)}"


def test_compute_all_preserves_index(long_ohlcv):
    result = compute_all(long_ohlcv)
    assert list(result.index) == list(long_ohlcv.index)


def test_compute_all_raises_on_missing_columns():
    bad = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError, match="필수 컬럼"):
        compute_all(bad)


def test_compute_all_latest_indicators_not_nan(long_ohlcv):
    result = compute_all(long_ohlcv)
    latest = result.iloc[-1]
    # 충분한 기간(200일) 데이터면 핵심 지표가 NaN 아니어야 함
    for key in ("sma_60", "ema_20", "rsi_14", "atr_14", "bb_middle", "macd", "adx"):
        assert not pd.isna(latest[key]), f"{key} 가 NaN"

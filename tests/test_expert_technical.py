"""기술 전문가 테스트.

합성 데이터로 시그널 감지 / 스코어 범위 / 모드 적합도 검증.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.experts.base import ExpertOpinion
from src.experts.technical import TechnicalExpert
from src.indicators.compute import compute_all


def _make_ohlcv(close: pd.Series) -> pd.DataFrame:
    high = close * 1.01
    low = close * 0.99
    open_ = close.shift(1).fillna(close.iloc[0])
    volume = pd.Series(range(100_000, 100_000 + len(close)), index=close.index, dtype=float)
    return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": volume})


@pytest.fixture
def uptrend_enriched():
    """완만하게 우상향하는 120일 데이터."""
    dates = pd.date_range("2025-10-01", periods=120, freq="D")
    close = pd.Series(100 + 0.3 * np.arange(120), index=dates)
    return compute_all(_make_ohlcv(close))


@pytest.fixture
def downtrend_enriched():
    """꾸준한 하락 120일."""
    dates = pd.date_range("2025-10-01", periods=120, freq="D")
    close = pd.Series(200 - 0.5 * np.arange(120), index=dates)
    return compute_all(_make_ohlcv(close))


@pytest.fixture
def short_enriched():
    """30일 — 데이터 부족."""
    dates = pd.date_range("2026-03-01", periods=30, freq="D")
    close = pd.Series(np.arange(100, 130, dtype=float), index=dates)
    return compute_all(_make_ohlcv(close))


# ============================================================

def test_insufficient_data_returns_error(short_enriched):
    op = TechnicalExpert().evaluate("TEST", short_enriched)
    assert not op.is_valid
    assert "데이터 부족" in op.error
    assert op.score == 0


def test_empty_data_returns_error():
    op = TechnicalExpert().evaluate("TEST", pd.DataFrame())
    assert not op.is_valid


def test_uptrend_has_positive_score(uptrend_enriched):
    op = TechnicalExpert().evaluate("UP", uptrend_enriched)
    assert op.is_valid
    assert op.score > 20, f"상승추세 점수가 낮음: {op.score}"


def test_downtrend_scores_lower_than_uptrend(uptrend_enriched, downtrend_enriched):
    up = TechnicalExpert().evaluate("UP", uptrend_enriched)
    down = TechnicalExpert().evaluate("DOWN", downtrend_enriched)
    assert down.score < up.score, f"하락 {down.score} >= 상승 {up.score}"


def test_score_capped_at_100(uptrend_enriched):
    op = TechnicalExpert().evaluate("UP", uptrend_enriched)
    assert 0 <= op.score <= 100


def test_mode_fit_keys(uptrend_enriched):
    op = TechnicalExpert().evaluate("UP", uptrend_enriched)
    assert {"bunt", "squeeze"} <= set(op.mode_fit.keys())
    assert all(0 <= v <= 1 for v in op.mode_fit.values())


def test_signals_non_empty_on_valid_data(uptrend_enriched):
    op = TechnicalExpert().evaluate("UP", uptrend_enriched)
    assert len(op.signals) > 0
    # 모드 적합도 시그널은 항상 포함
    assert any(s.name == "모드 적합도" for s in op.signals)


def test_reason_summary_generated(uptrend_enriched):
    op = TechnicalExpert().evaluate("UP", uptrend_enriched)
    assert op.reason_summary  # 빈 문자열 아님


def test_squeeze_fit_high_when_bb_contracts():
    """BB bandwidth 수축 + 거래량 급증 → 스퀴즈 적합도 상승 확인."""
    dates = pd.date_range("2025-10-01", periods=120, freq="D")
    # 앞 100일 변동성 크게, 마지막 20일 평탄 → bandwidth 수축
    base = pd.Series([100.0] * 120, index=dates)
    noise = pd.Series(
        np.concatenate([
            np.random.default_rng(1).normal(0, 5, 100),
            np.random.default_rng(2).normal(0, 0.3, 20),
        ]),
        index=dates,
    )
    close = base + noise
    # 마지막 거래량 급증
    vol = pd.Series([100_000.0] * 120, index=dates)
    vol.iloc[-1] = 250_000

    high = close * 1.005
    low = close * 0.995
    open_ = close.shift(1).fillna(close.iloc[0])
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close, "volume": vol})
    enriched = compute_all(df)

    op = TechnicalExpert().evaluate("SQ", enriched)
    # 스퀴즈 적합도가 번트보다 높아야 함
    assert op.mode_fit["squeeze"] >= op.mode_fit["bunt"], f"mode_fit={op.mode_fit}"


def test_expert_opinion_is_valid_when_no_error():
    op = ExpertOpinion(code="X", expert="technical", score=50.0)
    assert op.is_valid
    op2 = ExpertOpinion(code="X", expert="technical", score=0.0, error="데이터 부족")
    assert not op2.is_valid

"""앙상블 스코어러 테스트.

DB 의존 없이 가짜 전문가 주입으로 가중합·필터·fallback 검증.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.ensemble.scorer import (
    BUNT_WEIGHTS,
    SQUEEZE_WEIGHTS,
    EnsembleScorer,
    EnsembleWeights,
)
from src.experts.base import ExpertOpinion


class _FakeTechnical:
    def __init__(self, score=70.0, bunt_fit=0.6, squeeze_fit=0.4, error=""):
        self.score = score
        self.bunt_fit = bunt_fit
        self.squeeze_fit = squeeze_fit
        self.error = error

    def evaluate(self, code, enriched, as_of=None):
        if self.error:
            return ExpertOpinion(code=code, expert="technical", score=0, error=self.error)
        return ExpertOpinion(
            code=code, expert="technical", score=self.score,
            mode_fit={"bunt": self.bunt_fit, "squeeze": self.squeeze_fit},
            reason_summary=f"tech {self.score}",
        )


class _FakeFundamental:
    def __init__(self, score=60.0, bunt_fit=0.7, squeeze_fit=0.3, acceptable=True, reason=""):
        self.score = score
        self.bunt_fit = bunt_fit
        self.squeeze_fit = squeeze_fit
        self.acceptable = acceptable
        self.reason = reason

    def evaluate(self, code, as_of=None):
        return ExpertOpinion(
            code=code, expert="fundamental", score=self.score,
            mode_fit={"bunt": self.bunt_fit, "squeeze": self.squeeze_fit},
            reason_summary=f"fund {self.score}",
        )

    def is_acceptable(self, code, as_of=None):
        return self.acceptable, self.reason


class _MissingFundamental(_FakeFundamental):
    """재무 데이터 없음 — evaluate 가 error 반환."""
    def evaluate(self, code, as_of=None):
        return ExpertOpinion(code=code, expert="fundamental", score=0, error="데이터 없음")


class _FakeFlow:
    def __init__(self, score=50.0, bunt_fit=0.5, squeeze_fit=0.5, error=""):
        self.score = score
        self.bunt_fit = bunt_fit
        self.squeeze_fit = squeeze_fit
        self.error = error

    def evaluate(self, code, as_of=None):
        if self.error:
            return ExpertOpinion(code=code, expert="flow", score=0, error=self.error)
        return ExpertOpinion(
            code=code, expert="flow", score=self.score,
            mode_fit={"bunt": self.bunt_fit, "squeeze": self.squeeze_fit},
            reason_summary=f"flow {self.score}",
        )


class _MissingFlow(_FakeFlow):
    def evaluate(self, code, as_of=None):
        return ExpertOpinion(code=code, expert="flow", score=0, error="데이터 없음")


@pytest.fixture
def dummy_enriched():
    dates = pd.date_range("2025-01-01", periods=80, freq="D")
    return pd.DataFrame({"close": range(100, 180)}, index=dates)


# ============================================================

def _make_scorer(mode="bunt", tech=None, fund=None, flow=None):
    return EnsembleScorer(
        mode=mode,
        technical=tech or _FakeTechnical(),
        fundamental=fund or _FakeFundamental(),
        flow=flow or _FakeFlow(),
    )


def test_weights_sum_must_be_one():
    with pytest.raises(ValueError):
        EnsembleWeights(technical=0.7, fundamental=0.5, flow=0.1)


def test_mode_weight_differentiation():
    # 번트는 재무를 더 중시, 스퀴즈는 기술·분봉(모멘텀)을 더 중시
    assert BUNT_WEIGHTS.fundamental > SQUEEZE_WEIGHTS.fundamental
    assert SQUEEZE_WEIGHTS.technical > BUNT_WEIGHTS.technical
    assert SQUEEZE_WEIGHTS.minute > BUNT_WEIGHTS.minute


def test_weighted_sum_bunt_3expert(dummy_enriched):
    scorer = _make_scorer(
        mode="bunt",
        tech=_FakeTechnical(score=80),
        fund=_FakeFundamental(score=60),
        flow=_FakeFlow(score=40),
    )
    op = scorer.evaluate("X", dummy_enriched)
    # 번트 7-expert 중 tech/fund/flow 만 유효 → 유효 가중치 재정규화
    # (80*0.33 + 60*0.18 + 40*0.16) / (0.33+0.18+0.16) = 43.6 / 0.67 ≈ 65.075
    assert op.ensemble_score == pytest.approx(65.075, abs=0.01)
    # 7명 중 3명만 유효 → fallback
    assert op.fallback_used


def test_weighted_sum_squeeze_3expert(dummy_enriched):
    scorer = _make_scorer(
        mode="squeeze",
        tech=_FakeTechnical(score=80),
        fund=_FakeFundamental(score=60),
        flow=_FakeFlow(score=40),
    )
    op = scorer.evaluate("X", dummy_enriched)
    # 스퀴즈: (80*0.37 + 60*0.09 + 40*0.16) / (0.37+0.09+0.16) = 41.4 / 0.62 ≈ 66.774
    assert op.ensemble_score == pytest.approx(66.774, abs=0.01)


def test_hard_filter_warning_stock_blocks(dummy_enriched):
    scorer = _make_scorer(
        fund=_FakeFundamental(acceptable=False, reason="관리종목"),
    )
    op = scorer.evaluate("X", dummy_enriched)
    assert op.filtered
    assert "관리종목" in op.filter_reason


def test_missing_fundamental_fallback(dummy_enriched):
    scorer = _make_scorer(
        mode="bunt",
        tech=_FakeTechnical(score=70),
        fund=_MissingFundamental(),
        flow=_FakeFlow(score=50),
    )
    op = scorer.evaluate("X", dummy_enriched)
    assert op.fallback_used
    # 재무 누락 → tech 0.33, flow 0.16 유효 → 재정규화
    # (70*0.33 + 50*0.16) / (0.33+0.16) = 31.1 / 0.49 ≈ 63.469
    assert op.ensemble_score == pytest.approx(63.469, abs=0.01)


def test_missing_flow_fallback(dummy_enriched):
    scorer = _make_scorer(
        mode="bunt",
        tech=_FakeTechnical(score=70),
        fund=_FakeFundamental(score=60),
        flow=_MissingFlow(),
    )
    op = scorer.evaluate("X", dummy_enriched)
    assert op.fallback_used
    # flow 누락 → tech 0.33, fund 0.18 유효
    # (70*0.33 + 60*0.18) / (0.33+0.18) = 33.9 / 0.51 ≈ 66.471
    assert op.ensemble_score == pytest.approx(66.471, abs=0.01)


def test_both_non_tech_missing_fallback(dummy_enriched):
    scorer = _make_scorer(
        mode="bunt",
        tech=_FakeTechnical(score=70),
        fund=_MissingFundamental(),
        flow=_MissingFlow(),
    )
    op = scorer.evaluate("X", dummy_enriched)
    assert op.fallback_used
    # 기술만 유효 → 기술 점수 그대로
    assert op.ensemble_score == pytest.approx(70.0, abs=0.01)


def test_technical_error_fails_ensemble(dummy_enriched):
    scorer = _make_scorer(tech=_FakeTechnical(error="데이터 부족"))
    op = scorer.evaluate("X", dummy_enriched)
    assert op.filtered


def test_mode_fit_weighted_average(dummy_enriched):
    scorer = _make_scorer(
        mode="bunt",
        tech=_FakeTechnical(bunt_fit=0.8, squeeze_fit=0.2),
        fund=_FakeFundamental(bunt_fit=0.6, squeeze_fit=0.4),
        flow=_FakeFlow(bunt_fit=0.4, squeeze_fit=0.6),
    )
    op = scorer.evaluate("X", dummy_enriched)
    # bunt: (0.8*0.33 + 0.6*0.18 + 0.4*0.16) / (0.33+0.18+0.16) = 0.436 / 0.67 ≈ 0.6507
    assert op.mode_fit["bunt"] == pytest.approx(0.6507, abs=0.001)


def test_score_capped(dummy_enriched):
    scorer = _make_scorer(
        tech=_FakeTechnical(score=100),
        fund=_FakeFundamental(score=100),
        flow=_FakeFlow(score=100),
    )
    op = scorer.evaluate("X", dummy_enriched)
    assert 0 <= op.ensemble_score <= 100


def test_invalid_mode_rejected():
    with pytest.raises(ValueError):
        EnsembleScorer(mode="invalid")


def test_opinions_dict_has_all_three(dummy_enriched):
    scorer = _make_scorer()
    op = scorer.evaluate("X", dummy_enriched)
    assert {"technical", "fundamental", "flow"} <= set(op.opinions.keys())

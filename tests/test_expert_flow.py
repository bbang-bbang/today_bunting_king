"""흐름 전문가 테스트 (순수 함수 기반, DB 의존 없음)."""
from __future__ import annotations

import pytest

from src.experts.flow import FlowExpert, FlowSnapshot


def snap(
    foreign_5d=None, institution_5d=None,
    foreign_today=None, institution_today=None,
    momentum_5d=None, trading_value=None,
):
    return FlowSnapshot(
        foreign_5d_net=foreign_5d,
        institution_5d_net=institution_5d,
        foreign_today_net=foreign_today,
        institution_today_net=institution_today,
        momentum_5d_pct=momentum_5d,
        trading_value_today=trading_value,
    )


def test_all_none_returns_error():
    op = FlowExpert().evaluate_snapshot("X", snap())
    assert not op.is_valid
    assert op.score == 0


def test_strong_foreign_5d_high_score():
    op = FlowExpert().evaluate_snapshot(
        "X", snap(foreign_5d=3_000_000_000, momentum_5d=1),
    )
    # 30억+ 외인 순매수 → 5일 외인 25점 만점 카테고리
    assert op.score >= 25


def test_selling_no_points():
    op = FlowExpert().evaluate_snapshot(
        "X", snap(foreign_5d=-1_000_000_000, institution_5d=-500_000_000, momentum_5d=-2),
    )
    # 순매도 → 0점 시그널만
    assert op.score <= 10   # 모드 적합도만 소량 가산될 수 있음


def test_dual_buying_bonus():
    op_both = FlowExpert().evaluate_snapshot(
        "X", snap(foreign_5d=1_000_000_000, institution_5d=1_000_000_000, momentum_5d=3),
    )
    op_only_one = FlowExpert().evaluate_snapshot(
        "X", snap(foreign_5d=1_000_000_000, institution_5d=-500_000_000, momentum_5d=3),
    )
    # 쌍끌이 보너스 5점 차이 이상
    assert op_both.score > op_only_one.score


def test_strong_momentum_scores():
    op = FlowExpert().evaluate_snapshot("X", snap(momentum_5d=12))
    # +12% → 강한 상승 15점
    names = [s.name for s in op.signals]
    assert "5일 강한 상승" in names


def test_squeeze_fit_for_surge():
    op = FlowExpert().evaluate_snapshot(
        "X", snap(foreign_today=2_000_000_000, institution_today=800_000_000, momentum_5d=12),
    )
    assert op.mode_fit["squeeze"] > op.mode_fit["bunt"]


def test_bunt_fit_for_steady_accumulation():
    op = FlowExpert().evaluate_snapshot(
        "X", snap(foreign_5d=1_000_000_000, institution_5d=500_000_000, momentum_5d=2),
    )
    # 꾸준한 수급 + 평온 모멘텀 → 번트
    assert op.mode_fit["bunt"] >= op.mode_fit["squeeze"]


def test_score_capped_at_100():
    op = FlowExpert().evaluate_snapshot(
        "X", snap(
            foreign_5d=10_000_000_000, institution_5d=10_000_000_000,
            foreign_today=5_000_000_000, institution_today=3_000_000_000,
            momentum_5d=15,
        ),
    )
    assert 0 <= op.score <= 100


def test_only_momentum_data():
    op = FlowExpert().evaluate_snapshot("X", snap(momentum_5d=6))
    # 흐름 데이터 없지만 모멘텀만 있으면 평가는 됨
    assert op.is_valid
    assert op.score > 0

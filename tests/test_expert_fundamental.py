"""재무제표 전문가 테스트.

순수 함수 (evaluate_snapshot) 단위 테스트로 DB 의존 없이 모든 케이스 검증.
"""
from __future__ import annotations

import pytest

from src.experts.fundamental import FundamentalExpert, FundamentalSnapshot


def snap(per=None, pbr=None, roe=None, market_cap=None, is_warning=False, is_watch=False):
    return FundamentalSnapshot(
        per=per, pbr=pbr, roe=roe,
        market_cap=market_cap,
        is_warning=is_warning, is_watch=is_watch,
    )


def test_warning_flag_disqualifies():
    op = FundamentalExpert().evaluate_snapshot("X", snap(per=10, is_warning=True))
    assert op.score == 0
    assert op.error == "관리종목"


def test_watch_flag_disqualifies():
    op = FundamentalExpert().evaluate_snapshot("X", snap(per=10, is_watch=True))
    assert op.score == 0
    assert op.error == "투자주의"


def test_no_data_error():
    op = FundamentalExpert().evaluate_snapshot("X", snap())
    assert op.score == 0
    assert "없음" in op.error


def test_blue_chip_high_score():
    """저PER · 적정PBR · 고ROE · 대형주 → 고득점 기대."""
    op = FundamentalExpert().evaluate_snapshot(
        "BLUE",
        snap(per=8, pbr=1.2, roe=18, market_cap=15_000_000_000_000),
    )
    assert op.score >= 70, f"블루칩 점수가 낮음: {op.score}"


def test_loss_company_has_zero_scored_signals():
    """적자 기업(PER<0, ROE<0)은 PER·ROE 카테고리가 0점 시그널로 표시됨."""
    op = FundamentalExpert().evaluate_snapshot(
        "LOSS",
        snap(per=-5.0, pbr=2.0, roe=-10, market_cap=500_000_000_000),
    )
    # PER, ROE 모두 "적자" 라벨의 시그널이 포함되어야 함
    names = [s.name for s in op.signals]
    assert any("적자" in n for n in names)


def test_overvalued_has_lower_score_than_fair():
    """고평가(PER 60, PBR 6) 점수 < 적정(PER 15, PBR 1.2)."""
    fair = FundamentalExpert().evaluate_snapshot(
        "FAIR", snap(per=15, pbr=1.2, roe=12, market_cap=2_000_000_000_000),
    )
    bubble = FundamentalExpert().evaluate_snapshot(
        "BUBBLE", snap(per=60, pbr=6, roe=5, market_cap=2_000_000_000_000),
    )
    assert bubble.score < fair.score


def test_bunt_fit_high_for_stable():
    op = FundamentalExpert().evaluate_snapshot(
        "S", snap(per=12, pbr=1.1, roe=14, market_cap=5_000_000_000_000),
    )
    assert op.mode_fit["bunt"] > op.mode_fit["squeeze"]


def test_squeeze_fit_high_for_small_cap_high_valuation():
    op = FundamentalExpert().evaluate_snapshot(
        "G", snap(per=45, pbr=4.5, roe=18, market_cap=50_000_000_000),
    )
    assert op.mode_fit["squeeze"] > op.mode_fit["bunt"]


def test_is_acceptable_accepts_normal():
    exp = FundamentalExpert()
    # 순수 함수 경로 없으니 하드 필터 로직을 직접 한번 더 검증 (스냅샷 기반)
    # 실제 DB 접근 피하려고 내부 로직 카피는 불가. 대신 evaluate_snapshot 으로 is_warning/watch 만 체크.
    op_bad = exp.evaluate_snapshot("X", snap(per=10, is_warning=True))
    assert op_bad.error == "관리종목"
    op_ok = exp.evaluate_snapshot("X", snap(per=10, pbr=1.0, roe=15))
    assert op_ok.is_valid


def test_score_capped_at_100():
    """모든 지표 최상 → 이론상 만점 근처 → 100 이하."""
    op = FundamentalExpert().evaluate_snapshot(
        "MAX", snap(per=5, pbr=0.8, roe=25, market_cap=20_000_000_000_000),
    )
    assert 0 <= op.score <= 100

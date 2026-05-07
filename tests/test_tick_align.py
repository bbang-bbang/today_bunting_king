"""호가단위 정렬 — KIS 비호가 가격 40030000 거부 회귀 테스트.

2026-04-28 사고: target=60562, stop=7163 등 호가단위 안 맞아서 KIS 매도 16건 연속 실패.
"""
from __future__ import annotations

from src.risk.guard import RiskGuard, StrategyMode, align_to_tick


def test_tick_size_boundaries():
    """한국증시 호가단위 — 가격대별 정확히."""
    cases = [
        (500, 1, 500),         # < 1000: 1원
        (3_000, 5, 3_000),     # 1k-5k: 5원
        (3_007, 5, 3_005),     # nearest down
        (7_163, 10, 7_160),    # 5k-10k: 10원 (031330 케이스)
        (7_165, 10, 7_170),    # nearest up (5 boundary → up)
        (30_050, 50, 30_050),  # 10k-50k: 50원
        (30_077, 50, 30_100),  # nearest
        (60_562, 100, 60_600), # 50k-100k: 100원 (192080 케이스)
        (70_941, 100, 70_900), # nearest down
        (250_500, 500, 250_500),  # 100k-500k: 500원
        (250_700, 500, 250_500),  # nearest down
        (1_500_000, 1_000, 1_500_000),  # 500k+: 1000원
    ]
    for price, expected_tick, nearest in cases:
        # 정렬 한 결과가 tick 의 배수
        aligned = align_to_tick(price, "nearest")
        assert aligned % expected_tick == 0, f"{price} → {aligned} 호가 위반"
        assert aligned == nearest, f"{price} expected {nearest}, got {aligned}"


def test_align_directions():
    """down / up / nearest 방향 검증."""
    assert align_to_tick(60_562, "down") == 60_500
    assert align_to_tick(60_562, "up") == 60_600
    assert align_to_tick(60_562, "nearest") == 60_600  # 62 >= 50 → up
    assert align_to_tick(60_530, "nearest") == 60_500  # 30 < 50 → down


def test_compute_target_stop_returns_tick_aligned():
    """compute_target_stop 결과가 호가단위 정렬되어 있어야 함 (KIS 거부 방지)."""
    # 192080 진입 56,600 → bunt swing TP+7%/SL-4%
    tp, sl = RiskGuard.compute_target_stop(56_600, StrategyMode.BUNT, holding_mode="swing_week")
    assert tp % 100 == 0, f"TP {tp} 호가단위(100) 안 맞음"
    assert sl % 100 == 0, f"SL {sl} 호가단위(100) 안 맞음"
    # 7,540 (031330) → 호가단위 10원
    tp2, sl2 = RiskGuard.compute_target_stop(7_540, StrategyMode.BUNT, holding_mode="swing_week")
    assert tp2 % 10 == 0, f"TP {tp2} 호가단위(10) 안 맞음"
    assert sl2 % 10 == 0, f"SL {sl2} 호가단위(10) 안 맞음"


def test_align_zero_or_negative():
    """0 이하는 그대로 반환 (방어)."""
    assert align_to_tick(0) == 0
    assert align_to_tick(-100) == -100

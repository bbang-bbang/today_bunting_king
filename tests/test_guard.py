"""RiskGuard 불변조건 테스트.

이 테스트는 프로젝트의 생명선이다.
실패 시 불변조건이 깨진 것이므로 절대 비활성화하지 말 것.
"""
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from src.risk.guard import (
    GuardContext,
    OrderIntent,
    RiskGuard,
    SEED_CAP_KRW,
    Side,
    StrategyMode,
)

KST = ZoneInfo("Asia/Seoul")
DURING_MARKET = datetime(2026, 4, 16, 10, 0, tzinfo=KST)   # 목 10:00


def guard(active=1_000_000):
    return RiskGuard(active_seed_krw=active)


def ctx(cash=1_000_000, pos_value=0, positions=None, pnl=0, streak=0):
    return GuardContext(
        cash_balance=cash,
        position_value=pos_value,
        open_position_codes=set(positions or []),
        daily_realized_pnl=pnl,
        losing_days_streak=streak,
    )


def intent(side, code="005930", qty=10, price=70_000, mode=StrategyMode.BUNT, pin=False):
    return OrderIntent(
        chat_id=1, side=side, code=code, quantity=qty, price=price,
        strategy_mode=mode, pin_provided=pin,
    )


# ========================================================
# 절대 상한 방어 (활성 시드가 SEED_CAP 를 넘을 수 없다)
# ========================================================

def test_active_seed_above_absolute_cap_rejected():
    with pytest.raises(ValueError, match="상한"):
        RiskGuard(active_seed_krw=SEED_CAP_KRW + 1)


def test_active_seed_zero_or_negative_rejected():
    with pytest.raises(ValueError):
        RiskGuard(active_seed_krw=0)
    with pytest.raises(ValueError):
        RiskGuard(active_seed_krw=-1)


# ========================================================
# 활성 시드 = 100만 (기본)
# ========================================================

def test_buy_exceeds_cash_rejected():
    r = guard().check(
        intent(Side.BUY, qty=100, price=8_000),
        ctx(cash=500_000),
        DURING_MARKET,
    )
    assert not r.approved and "가용 현금" in r.reason


def test_buy_exceeds_seed_cap_rejected():
    # 이미 70만 보유 + 40만 주문 = 110만 → 활성 시드 상한 위반
    r = guard().check(
        intent(Side.BUY, qty=4, price=100_000, pin=True),
        ctx(cash=1_000_000, pos_value=700_000),
        DURING_MARKET,
    )
    assert not r.approved and "시드 상한" in r.reason


def test_buy_exceeds_per_position_cap_rejected():
    r = guard().check(
        intent(Side.BUY, qty=6, price=100_000, pin=True),
        ctx(),
        DURING_MARKET,
    )
    assert not r.approved and "한도" in r.reason


def test_buy_over_30pct_without_pin_requires_pin():
    r = guard().check(
        intent(Side.BUY, qty=4, price=100_000, pin=False),
        ctx(),
        DURING_MARKET,
    )
    assert not r.approved and r.requires_pin


def test_buy_over_30pct_with_pin_ok():
    r = guard().check(
        intent(Side.BUY, qty=4, price=100_000, pin=True),
        ctx(),
        DURING_MARKET,
    )
    assert r.approved


def test_buy_under_30pct_no_pin_ok():
    r = guard().check(
        intent(Side.BUY, qty=2, price=100_000),
        ctx(),
        DURING_MARKET,
    )
    assert r.approved


def test_buy_duplicate_same_code_rejected():
    r = guard().check(
        intent(Side.BUY),
        ctx(positions={"005930"}),
        DURING_MARKET,
    )
    assert not r.approved and "1회" in r.reason


def test_buy_after_force_liquidate_time_rejected():
    late = datetime(2026, 4, 16, 15, 21, tzinfo=KST)
    r = guard().check(intent(Side.BUY), ctx(), late)
    assert not r.approved and "15:20" in r.reason


def test_buy_before_market_open_rejected():
    before = datetime(2026, 4, 16, 8, 30, tzinfo=KST)
    r = guard().check(intent(Side.BUY), ctx(), before)
    assert not r.approved


def test_buy_after_market_close_rejected():
    after = datetime(2026, 4, 16, 15, 31, tzinfo=KST)
    r = guard().check(intent(Side.BUY), ctx(), after)
    assert not r.approved


def test_sell_non_holding_rejected():
    r = guard().check(intent(Side.SELL), ctx(positions=set()), DURING_MARKET)
    assert not r.approved


def test_sell_holding_ok():
    r = guard().check(intent(Side.SELL), ctx(positions={"005930"}), DURING_MARKET)
    assert r.approved


def test_daily_loss_cut_blocks_buy():
    r = guard().check(intent(Side.BUY), ctx(pnl=-30_000), DURING_MARKET)
    assert not r.approved and "일일 손실" in r.reason


def test_losing_streak_cut_blocks_buy():
    r = guard().check(intent(Side.BUY), ctx(streak=3), DURING_MARKET)
    assert not r.approved


def test_bunt_target_stop():
    tp, sl = RiskGuard.compute_target_stop(100_000, StrategyMode.BUNT)
    assert tp == 103_000
    assert sl == 98_000


def test_squeeze_target_stop():
    tp, sl = RiskGuard.compute_target_stop(100_000, StrategyMode.SQUEEZE)
    assert tp == 105_000
    assert sl == 97_000


def test_squeeze_risk_strictly_greater_than_bunt():
    _, bunt_sl = RiskGuard.compute_target_stop(100_000, StrategyMode.BUNT)
    _, sq_sl = RiskGuard.compute_target_stop(100_000, StrategyMode.SQUEEZE)
    assert sq_sl < bunt_sl


def test_bunt_target_stop_swing_week():
    tp, sl = RiskGuard.compute_target_stop(100_000, StrategyMode.BUNT, holding_mode="swing_week")
    assert tp == 107_000
    assert sl == 96_000


def test_squeeze_target_stop_swing_week():
    tp, sl = RiskGuard.compute_target_stop(100_000, StrategyMode.SQUEEZE, holding_mode="swing_week")
    assert tp == 112_000
    assert sl == 95_000


def test_swing_band_wider_than_day():
    day_tp, day_sl = RiskGuard.compute_target_stop(100_000, StrategyMode.BUNT)
    sw_tp, sw_sl = RiskGuard.compute_target_stop(100_000, StrategyMode.BUNT, holding_mode="swing_week")
    assert sw_tp > day_tp
    assert sw_sl < day_sl


# ========================================================
# 소액 실전 테스트 시나리오 (활성 시드 = 10만원)
# ========================================================

def test_smalltest_buy_under_30pct_ok():
    # 10만 활성시드 · 2만원 주문 = 20% → PIN 불필요, 통과
    r = guard(active=100_000).check(
        intent(Side.BUY, qty=2, price=10_000),
        ctx(cash=100_000),
        DURING_MARKET,
    )
    assert r.approved


def test_smalltest_buy_over_30pct_requires_pin():
    # 10만 활성시드 · 4만원 주문 = 40% → PIN 필요
    r = guard(active=100_000).check(
        intent(Side.BUY, qty=4, price=10_000, pin=False),
        ctx(cash=100_000),
        DURING_MARKET,
    )
    assert not r.approved and r.requires_pin


def test_smalltest_buy_over_per_position_cap_rejected():
    # 10만 활성시드 · 6만원 주문 = 60% → 종목당 50% 한도 위반
    r = guard(active=100_000).check(
        intent(Side.BUY, qty=6, price=10_000, pin=True),
        ctx(cash=100_000),
        DURING_MARKET,
    )
    assert not r.approved and "한도" in r.reason


def test_smalltest_daily_loss_cut_scales():
    # 10만 활성시드 → 일일 손실 컷 -3% = -3,000원
    r = guard(active=100_000).check(
        intent(Side.BUY, qty=1, price=10_000),
        ctx(cash=100_000, pnl=-3_000),
        DURING_MARKET,
    )
    assert not r.approved and "일일 손실" in r.reason

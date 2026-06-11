"""레짐 트립 시 '단타 전환' 발송 경로 테스트.

급변장(전일 코스피 급락)에서 추천을 통째 보류하지 않고, REGIME_DEGRADED_ENABLED 면
번트 only · 높은 점수 바 · 축소 시드로 발송하는지 검증.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import config
from src.bot import scheduler


def _ctx():
    return SimpleNamespace(bot=AsyncMock())


def _run_morning(ctx):
    asyncio.run(scheduler.job_morning_recommend(ctx))


def test_regime_trip_switches_to_degraded_daytrade():
    ctx = _ctx()
    reason = "전일 코스피 -2.3% 하락 (임계값 -1.5%) — 오늘 추천 보류"
    with patch.object(config, "REGIME_DEGRADED_ENABLED", True), \
         patch.object(config, "REGIME_DEGRADED_MIN_SCORE", 70.0), \
         patch.object(config, "REGIME_DEGRADED_SEED_PCT", 50), \
         patch.object(config, "SEED_KRW", 1_000_000), \
         patch.object(scheduler, "is_kr_trading_day", return_value=True), \
         patch.object(scheduler, "_check_market_regime",
                      new=AsyncMock(return_value=(False, reason))), \
         patch.object(scheduler, "_list_candidate_codes", return_value=["005930"]), \
         patch.object(scheduler, "_get_approved_users",
                      return_value=[SimpleNamespace(chat_id=111)]), \
         patch.object(scheduler, "send_recommendations_dual",
                      new=AsyncMock(return_value=1)) as send_mock:
        _run_morning(ctx)

    # 단타 전환 안내 메시지가 사용자에게 발송됐다
    sent = "\n".join(c.args[1] for c in ctx.bot.send_message.await_args_list)
    assert "급변장 단타 전환" in sent
    assert reason in sent

    # 축소 파라미터로 발송 호출 — 번트 only · 점수 70 · 시드 50%(=50만)
    send_mock.assert_awaited_once()
    kwargs = send_mock.await_args.kwargs
    assert kwargs["modes"] == ("bunt",)
    assert kwargs["min_score"] == 70.0
    assert kwargs["active_seed_krw"] == 500_000


def test_regime_trip_disabled_falls_back_to_hold():
    ctx = _ctx()
    reason = "전일 코스피 -2.3% 하락 (임계값 -1.5%) — 오늘 추천 보류"
    with patch.object(config, "REGIME_DEGRADED_ENABLED", False), \
         patch.object(scheduler, "is_kr_trading_day", return_value=True), \
         patch.object(scheduler, "_check_market_regime",
                      new=AsyncMock(return_value=(False, reason))), \
         patch.object(scheduler, "_get_approved_users",
                      return_value=[SimpleNamespace(chat_id=111)]), \
         patch.object(scheduler, "send_recommendations_dual",
                      new=AsyncMock(return_value=1)) as send_mock:
        _run_morning(ctx)

    # 보류 메시지만, 추천 발송은 호출되지 않음
    sent = "\n".join(c.args[1] for c in ctx.bot.send_message.await_args_list)
    assert "추천 보류" in sent
    send_mock.assert_not_awaited()


def test_normal_regime_sends_dual_unchanged():
    ctx = _ctx()
    with patch.object(scheduler, "is_kr_trading_day", return_value=True), \
         patch.object(scheduler, "_check_market_regime",
                      new=AsyncMock(return_value=(True, ""))), \
         patch.object(scheduler, "_list_candidate_codes", return_value=["005930"]), \
         patch.object(scheduler, "_get_approved_users",
                      return_value=[SimpleNamespace(chat_id=111)]), \
         patch.object(scheduler, "send_recommendations_dual",
                      new=AsyncMock(return_value=2)) as send_mock:
        _run_morning(ctx)

    # 평시: modes/min_score/seed 오버라이드 없이 기본 호출
    send_mock.assert_awaited_once()
    assert send_mock.await_args.kwargs == {}

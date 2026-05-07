"""execute_buy in-flight lock — 동일 (chat_id, code) 중복 매수 방어.

2026-05-04: /strategy + /early 후 같은 종목 매수 버튼 (번트/스퀴즈) 둘 다 빠르게
클릭하면 KIS 에 두 번 주문 가는 race condition 방어.
"""
from __future__ import annotations

import asyncio
import importlib

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "1000000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "dummy")
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


@pytest.fixture
def reset_lock():
    """테스트 간 in-flight set 초기화."""
    from src.services import portfolio_service
    portfolio_service._INFLIGHT_BUYS.clear()
    yield
    portfolio_service._INFLIGHT_BUYS.clear()


# ============================================================
# 동시 매수 시도 → 1건만 통과
# ============================================================

def test_concurrent_buys_same_code_only_one_succeeds(monkeypatch, reset_lock):
    from src.services import user_service, portfolio_service

    chat_id = 100
    user_service.register_user(chat_id)

    # _execute_buy_inner 가 100ms 걸리는 척 — 그 사이 두 번째 호출 시도
    call_count = {"n": 0}

    async def slow_inner(**kwargs):
        call_count["n"] += 1
        await asyncio.sleep(0.1)
        return {"success": True, "position_id": call_count["n"], "code": kwargs["code"],
                "qty": 10, "price": 100_000, "target": 107_000, "stop": 96_000,
                "commission": 0}

    monkeypatch.setattr(portfolio_service, "_execute_buy_inner", slow_inner)

    async def runner():
        # 같은 (chat_id, code) 두 번 동시 호출
        results = await asyncio.gather(
            portfolio_service.execute_buy(
                chat_id=chat_id, code="005930", quantity=10, price=100_000,
                strategy_mode="bunt", active_seed=1_000_000,
            ),
            portfolio_service.execute_buy(
                chat_id=chat_id, code="005930", quantity=10, price=100_000,
                strategy_mode="squeeze", active_seed=1_000_000,
            ),
        )
        return results

    results = asyncio.run(runner())

    # 1건은 success, 1건은 거절
    successes = [r for r in results if r.get("success")]
    rejects = [r for r in results if not r.get("success")]
    assert len(successes) == 1, f"동시 매수 1건만 통과해야: {results}"
    assert len(rejects) == 1
    assert "진행 중" in rejects[0]["reason"]
    # _execute_buy_inner 가 1번만 호출됐어야
    assert call_count["n"] == 1


# ============================================================
# 다른 종목 동시 매수 → 둘 다 통과
# ============================================================

def test_concurrent_buys_different_codes_both_succeed(monkeypatch, reset_lock):
    from src.services import user_service, portfolio_service

    chat_id = 200
    user_service.register_user(chat_id)

    async def slow_inner(**kwargs):
        await asyncio.sleep(0.05)
        return {"success": True, "position_id": 1, "code": kwargs["code"],
                "qty": 5, "price": 100_000, "target": 107_000, "stop": 96_000,
                "commission": 0}

    monkeypatch.setattr(portfolio_service, "_execute_buy_inner", slow_inner)

    async def runner():
        return await asyncio.gather(
            portfolio_service.execute_buy(
                chat_id=chat_id, code="005930", quantity=5, price=100_000,
                strategy_mode="bunt", active_seed=1_000_000,
            ),
            portfolio_service.execute_buy(
                chat_id=chat_id, code="000660", quantity=5, price=100_000,
                strategy_mode="bunt", active_seed=1_000_000,
            ),
        )

    results = asyncio.run(runner())
    assert all(r.get("success") for r in results)


# ============================================================
# 락은 매수 완료 후 해제 → 순차 호출은 둘 다 통과
# ============================================================

def test_sequential_buys_same_code_both_succeed(monkeypatch, reset_lock):
    from src.services import user_service, portfolio_service

    chat_id = 300
    user_service.register_user(chat_id)

    async def fast_inner(**kwargs):
        return {"success": True, "position_id": 1, "code": kwargs["code"],
                "qty": 5, "price": 100_000, "target": 107_000, "stop": 96_000,
                "commission": 0}

    monkeypatch.setattr(portfolio_service, "_execute_buy_inner", fast_inner)

    async def runner():
        r1 = await portfolio_service.execute_buy(
            chat_id=chat_id, code="005930", quantity=5, price=100_000,
            strategy_mode="bunt", active_seed=1_000_000,
        )
        r2 = await portfolio_service.execute_buy(
            chat_id=chat_id, code="005930", quantity=5, price=100_000,
            strategy_mode="bunt", active_seed=1_000_000,
        )
        return r1, r2

    r1, r2 = asyncio.run(runner())
    assert r1["success"]
    assert r2["success"]


# ============================================================
# inner 가 예외로 죽어도 락은 풀림
# ============================================================

def test_lock_released_on_inner_exception(monkeypatch, reset_lock):
    from src.services import user_service, portfolio_service

    chat_id = 400
    user_service.register_user(chat_id)

    async def failing_inner(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(portfolio_service, "_execute_buy_inner", failing_inner)

    async def runner():
        try:
            await portfolio_service.execute_buy(
                chat_id=chat_id, code="005930", quantity=5, price=100_000,
                strategy_mode="bunt", active_seed=1_000_000,
            )
        except RuntimeError:
            pass
        # 락이 풀렸어야 다음 호출이 들어감 (inner 가 또 RuntimeError)
        try:
            await portfolio_service.execute_buy(
                chat_id=chat_id, code="005930", quantity=5, price=100_000,
                strategy_mode="bunt", active_seed=1_000_000,
            )
        except RuntimeError:
            return "second_attempt_reached"
        return "no_exception"

    result = asyncio.run(runner())
    assert result == "second_attempt_reached"
    assert (chat_id, "005930") not in portfolio_service._INFLIGHT_BUYS

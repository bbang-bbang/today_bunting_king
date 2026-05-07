"""서비스 레이어 통합 테스트.

임시 DB 경로로 스키마 초기화 후 user/confirmation/portfolio 서비스 검증.
"""
from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """각 테스트마다 새 임시 DB 로 격리."""
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "100000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    # 환경변수 반영을 위해 config 를 리로드
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    # 서비스들도 config/connection 을 참조하므로 리로드
    from src.db.connection import init_schema
    init_schema()
    yield
    # cleanup handled by tmp_path fixture


# ============================================================
# user_service
# ============================================================

def test_register_and_get_user():
    from src.services import user_service
    user = user_service.register_user(111, trade_mode="paper")
    assert user.chat_id == 111
    assert user.status == "approved"
    assert user.strategy_mode == "bunt"

    got = user_service.get_user(111)
    assert got is not None and got.chat_id == 111


def test_get_nonexistent_user():
    from src.services import user_service
    assert user_service.get_user(9999) is None


def test_update_strategy_mode():
    from src.services import user_service
    user_service.register_user(222)
    assert user_service.update_strategy_mode(222, "squeeze") is True
    assert user_service.get_user(222).strategy_mode == "squeeze"
    assert user_service.update_strategy_mode(222, "invalid") is False


def test_pin_set_and_verify():
    from src.services import user_service
    user_service.register_user(333)
    assert user_service.set_pin(333, "123456") is True
    assert user_service.verify_pin(333, "123456") is True
    assert user_service.verify_pin(333, "000000") is False
    # 잘못된 포맷 PIN
    assert user_service.set_pin(333, "abc123") is False
    assert user_service.set_pin(333, "12345") is False


def test_is_approved():
    from src.services import user_service
    assert user_service.is_approved(444) is False
    user_service.register_user(444)
    assert user_service.is_approved(444) is True


# ============================================================
# confirmation_service
# ============================================================

def test_create_and_consume_confirmation():
    from src.services import confirmation_service, user_service
    user_service.register_user(555)
    u = confirmation_service.create(555, {"code": "005930", "qty": 1, "price": 100_000})
    assert u
    intent = confirmation_service.consume(u, 555)
    assert intent["code"] == "005930"
    # 한 번 consume 하면 재사용 불가
    assert confirmation_service.consume(u, 555) is None


def test_wrong_chat_id_cannot_consume():
    from src.services import confirmation_service, user_service
    user_service.register_user(666)
    u = confirmation_service.create(666, {"x": 1})
    assert confirmation_service.consume(u, 777) is None   # 다른 chat_id


def test_expired_confirmation_not_consumable(monkeypatch):
    from src.services import confirmation_service, user_service
    from src.db.connection import get_connection
    user_service.register_user(888)
    u = confirmation_service.create(888, {"x": 1})
    # DB 를 직접 수정해 만료시킴
    past = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    conn = get_connection()
    try:
        conn.execute("UPDATE pending_confirmations SET expires_at=? WHERE uuid=?", (past, u))
    finally:
        conn.close()
    assert confirmation_service.consume(u, 888) is None


# ============================================================
# portfolio_service (paper 모드, 장 시간 우회)
# ============================================================

def test_empty_account_summary():
    from src.services import portfolio_service, user_service
    user_service.register_user(1001)
    s = portfolio_service.get_account_summary(1001, 100_000)
    assert s["cash_available"] == 100_000
    assert s["open_positions"] == []
    assert s["closed_pnl_total"] == 0


def test_execute_buy_during_market_hours(monkeypatch):
    """장 시간 우회 후 매수 체결 확인."""
    from src.services import portfolio_service, user_service
    import src.risk.guard as guard_mod

    user_service.register_user(2001)

    # RiskGuard 의 시간 체크 우회: now 를 장중 시각으로 patch
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    kst = ZoneInfo("Asia/Seoul")
    during = _dt(2026, 4, 16, 10, 30, tzinfo=kst)

    real_check = guard_mod.RiskGuard.check
    def patched_check(self, intent, ctx, now=None):
        return real_check(self, intent, ctx, now=during)
    monkeypatch.setattr(guard_mod.RiskGuard, "check", patched_check)

    result = asyncio.run(portfolio_service.execute_buy(
        chat_id=2001, code="005930", quantity=1, price=30_000,
        strategy_mode="bunt", active_seed=100_000, pin_verified=False,
        holding_mode="day",
    ))
    assert result["success"] is True, result
    assert result["code"] == "005930"
    assert result["target"] == 30_900   # +3% (당일매매 MODE_PARAMS)
    assert result["stop"] == 29_400     # -2%

    # 잔고 반영 확인
    s = portfolio_service.get_account_summary(2001, 100_000)
    assert len(s["open_positions"]) == 1
    assert s["cash_available"] == 100_000 - 30_000


def test_execute_buy_rejected_by_guard(monkeypatch):
    """장 외 시각이면 RiskGuard 가 거절."""
    from src.services import portfolio_service, user_service
    user_service.register_user(2002)

    # 현재 시각이 장 외면 패스. 장 중이면 주말로 patch 하는 것도 방법이지만
    # 여기선 active_seed 초과로 자연스럽게 기각되도록 테스트
    result = asyncio.run(portfolio_service.execute_buy(
        chat_id=2002, code="005930", quantity=10, price=1_000_000,
        strategy_mode="bunt", active_seed=100_000, pin_verified=True,
    ))
    assert result["success"] is False
    # 사유는 시간/시드/PIN 중 하나
    assert result.get("reason")

"""reconcile_positions orphan 입양 — schema NOT NULL 버그 회귀 방지.

2026-05-04: KIS 잔고에 있는 종목을 봇 DB 에 INSERT 할 때 positions.buy_order_id
가 NOT NULL 이라 IntegrityError. synthetic broker_orders row 만들어 해결.
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "10000000")
    monkeypatch.setenv("TRADE_MODE", "kis_mock")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "dummy")
    monkeypatch.setenv("KIS_MOCK_APP_KEY", "x")
    monkeypatch.setenv("KIS_MOCK_APP_SECRET", "y")
    monkeypatch.setenv("KIS_MOCK_ACCOUNT_NO", "12345-01")
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


def test_reconcile_orphan_adopt_no_integrity_error(monkeypatch):
    """KIS 잔고에 있는데 봇 DB open 없는 종목 → 입양 시 IntegrityError 안 나야."""
    from scripts.reconcile_positions import reconcile_chat
    from src.services import user_service, portfolio_service
    from src.db.connection import get_connection

    chat_id = 100
    user_service.register_user(chat_id)

    # KIS 잔고 mock — orphan 종목 1건 (DB 에 없음)
    async def fake_balance(_):
        return {
            "total_evaluation": 5_000_000,
            "total_pnl": 0,
            "total_pnl_pct": 0.0,
            "cash_available": 1_000_000,
            "positions": [
                {"code": "005930", "name": "삼성전자",
                 "quantity": 30, "avg_price": 77_000,
                 "current_price": 77_000, "pnl": 0, "pnl_pct": 0.0},
            ],
        }
    monkeypatch.setattr(portfolio_service, "get_broker_balance", fake_balance)

    res = asyncio.run(reconcile_chat(chat_id, apply=True))

    assert res.get("adopted") == 1, f"orphan adopted 안 됨: {res}"

    # positions 에 buy_order_id 가 박혀있고, broker_orders 에 synthetic row 가 있어야
    conn = get_connection()
    try:
        pos = conn.execute(
            "SELECT id, code, quantity, buy_price, buy_order_id FROM positions "
            "WHERE chat_id=? AND status='open'",
            (chat_id,),
        ).fetchone()
        assert pos is not None
        assert pos[1] == "005930"
        assert pos[2] == 30
        assert pos[3] == 77_000
        assert pos[4] is not None, "buy_order_id 가 NULL — synthetic broker_orders 누락"

        bo = conn.execute(
            "SELECT side, status, broker_order_id, code, quantity FROM broker_orders WHERE id=?",
            (pos[4],),
        ).fetchone()
        assert bo[0] == "buy"
        assert bo[1] == "filled"
        assert bo[2] == "ORPHAN-ADOPT"   # 입양 마커
        assert bo[3] == "005930"
        assert bo[4] == 30
    finally:
        conn.close()


def test_reconcile_orphan_adopt_uses_user_holding_mode(monkeypatch):
    """입양 시 TP/SL 은 사용자의 현재 holding_mode 적용."""
    from scripts.reconcile_positions import reconcile_chat
    from src.services import user_service, portfolio_service
    from src.db.connection import get_connection

    chat_id = 200
    user_service.register_user(chat_id)
    user_service.update_holding_mode(chat_id, "day")  # day 모드 = 좁은 밴드 (+3%/-2%)

    async def fake_balance(_):
        return {
            "total_evaluation": 5_000_000, "total_pnl": 0, "total_pnl_pct": 0.0,
            "cash_available": 1_000_000,
            "positions": [
                {"code": "035720", "name": "카카오",
                 "quantity": 20, "avg_price": 50_000,
                 "current_price": 50_000, "pnl": 0, "pnl_pct": 0.0},
            ],
        }
    monkeypatch.setattr(portfolio_service, "get_broker_balance", fake_balance)

    res = asyncio.run(reconcile_chat(chat_id, apply=True))
    assert res.get("adopted") == 1

    conn = get_connection()
    try:
        pos = conn.execute(
            "SELECT target_price, stop_price FROM positions "
            "WHERE chat_id=? AND code='035720'",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()

    # day 모드 bunt: TP +3% = 51,500 / SL -2% = 49,000 (호가 정렬 후 약간 낮을 수 있음)
    assert 51_000 <= pos[0] <= 51_500   # TP
    assert 48_900 <= pos[1] <= 49_000   # SL


def test_no_orphan_no_adopt(monkeypatch):
    """KIS 잔고가 봇 DB 와 일치하면 입양 0건."""
    from scripts.reconcile_positions import reconcile_chat
    from src.services import user_service, portfolio_service

    chat_id = 300
    user_service.register_user(chat_id)

    async def fake_balance(_):
        return {
            "total_evaluation": 0, "total_pnl": 0, "total_pnl_pct": 0.0,
            "cash_available": 1_000_000, "positions": [],
        }
    monkeypatch.setattr(portfolio_service, "get_broker_balance", fake_balance)

    res = asyncio.run(reconcile_chat(chat_id, apply=True))
    assert res.get("adopted", 0) == 0

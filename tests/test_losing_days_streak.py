"""get_losing_days_streak NULL safety 테스트.

5/6 사고: closed positions 의 P&L 이 NULL 이면 `int(None)` 폭발 → 추천 발송 단계에서
TypeError → 사용자 25분 기다리고 결과 0건. NULL 을 streak 종료 신호로 처리하도록 fix.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from src.db.connection import get_connection, init_schema
from src.services.portfolio_service import get_losing_days_streak


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import src.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    import src.db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)
    init_schema()
    # bot_users + 사용자 stub
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO bot_users (chat_id, status, registered_at) VALUES (?, ?, ?)",
            (123456789, "approved", "2026-04-01T00:00:00"),
        )
        conn.execute(
            "INSERT INTO instruments (code, name, market, is_tradable, updated_at) "
            "VALUES ('005930', '삼성', 'KOSPI', 1, '2026-05-06T00:00:00')"
        )
        conn.execute(
            "INSERT INTO audit_log (chat_id, event_type, payload_json, ts) "
            "VALUES (?, 'test', '{}', ?)",
            (123456789, "2026-05-06T00:00:00"),
        )
        # 매수 broker_orders stub (FK 충족)
        conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, created_at, updated_at)
               VALUES (1, 'kis_mock', 'buy', '005930', 1, 60000, 'filled',
                       1, 60000, '2026-05-06T00:00:00', '2026-05-06T00:00:00')"""
        )
        conn.commit()
    finally:
        conn.close()
    yield db_path


def _insert_closed(conn, chat_id: int, days_ago: int, pnl):
    closed = (datetime(2026, 5, 6) - timedelta(days=days_ago)).isoformat()
    conn.execute(
        """INSERT INTO positions
           (chat_id, code, strategy_mode, buy_order_id, sell_order_id,
            buy_price, quantity, target_price, stop_price, status, pnl,
            opened_at, closed_at)
           VALUES (?, '005930', 'bunt', 1, NULL,
                   60000, 1, 65000, 57000, 'closed', ?,
                   '2026-05-04T09:00:00', ?)""",
        (chat_id, pnl, closed),
    )


# ============================================================
# NULL P&L 핸들링 (5/6 회귀 방지)
# ============================================================

def test_streak_handles_null_pnl_without_typeerror(temp_db):
    """NULL P&L row 가 있어도 TypeError 없이 streak 반환."""
    conn = get_connection()
    try:
        _insert_closed(conn, 123456789, days_ago=0, pnl=None)
        conn.commit()
    finally:
        conn.close()
    assert get_losing_days_streak(123456789) == 0


def test_streak_null_pnl_breaks_streak(temp_db):
    """NULL 은 산출 불가 → streak 종료."""
    conn = get_connection()
    try:
        # 어제 손실, 오늘 NULL → streak=0 (오늘 NULL 이 끊음)
        _insert_closed(conn, 123456789, days_ago=1, pnl=-10000)
        _insert_closed(conn, 123456789, days_ago=0, pnl=None)
        conn.commit()
    finally:
        conn.close()
    assert get_losing_days_streak(123456789) == 0


def test_streak_counts_consecutive_losses(temp_db):
    """오늘부터 역순으로 연속 손실 — NULL 없을 때 정상 동작."""
    conn = get_connection()
    try:
        _insert_closed(conn, 123456789, days_ago=0, pnl=-5000)
        _insert_closed(conn, 123456789, days_ago=1, pnl=-3000)
        _insert_closed(conn, 123456789, days_ago=2, pnl=-1000)
        _insert_closed(conn, 123456789, days_ago=3, pnl=2000)  # 끊는 양수
        conn.commit()
    finally:
        conn.close()
    assert get_losing_days_streak(123456789) == 3


def test_streak_zero_when_no_closed(temp_db):
    assert get_losing_days_streak(123456789) == 0

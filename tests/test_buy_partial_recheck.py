"""job_buy_partial_recheck — 매수 부분체결 시간차 재검증 회귀 테스트.

2026-05-06 P0 fix: KIS 모의투자 잔고 단계적 갱신 결함 보정.
submit_order 잔고 fallback 이 부분만 인식한 경우 5분 후 잔고 재조회로 보정.
"""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import datetime, timedelta

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


@pytest.fixture(autouse=True)
def force_market_session(monkeypatch):
    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "is_kr_market_session_now", lambda: True)


class FakeBroker:
    def __init__(self, balance_positions: list[dict]):
        self._positions = balance_positions

    async def get_balance(self):
        return {"positions": self._positions, "total_value": 0}


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@dataclass
class FakeCtx:
    bot: FakeBot


def _seed_partial_buy(chat_id, code, requested_qty, filled_qty, age_minutes):
    """매수 부분체결 인식된 broker_orders + position seed.

    submit_order 잔고 fallback 이 filled_qty 만 인식한 상태 (status='filled' 인데
    filled_quantity < quantity 인 패턴) 재현.
    """
    from src.db.connection import get_connection
    from src.services import user_service
    user_service.register_user(chat_id)
    created = (datetime.now() - timedelta(minutes=age_minutes)).isoformat(timespec="seconds")
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO audit_log(chat_id, event_type, payload_json, ts) VALUES (?, 'order_buy', '{}', ?)",
            (chat_id, created),
        )
        audit_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, broker_order_id, created_at, updated_at)
               VALUES (?, 'kis_mock', 'buy', ?, ?, ?, 'filled', ?, ?, ?, ?, ?)""",
            (audit_id, code, requested_qty, 8000, filled_qty, 8000,
             f"ODNO-{code}", created, created),
        )
        buy_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, opened_at)
               VALUES (?, ?, 'bunt', ?, ?, ?, ?, ?, 'open', ?)""",
            (chat_id, code, buy_id, 8000, filled_qty, 8500, 7500, created),
        )
        pos_id = cur.lastrowid
        return {"buy_bo_id": buy_id, "position_id": pos_id}
    finally:
        conn.close()


# ============================================================
# 시나리오 1: 잔고 시간차로 추가 체결 발견 → 보정
# ============================================================

def test_partial_recheck_corrects_when_balance_increased(monkeypatch):
    """매수 100주 발주 → fallback 54주 인식 → 10분 후 잔고 100주 → 보정."""
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 100
    s = _seed_partial_buy(chat_id, "031330", requested_qty=100,
                          filled_qty=54, age_minutes=10)
    fake = FakeBroker(balance_positions=[
        {"code": "031330", "quantity": 100, "current_price": 8000, "avg_price": 8000},
    ])
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_buy_partial_recheck(FakeCtx(bot=bot)))

    conn = get_connection()
    try:
        bo = conn.execute(
            "SELECT status, filled_quantity FROM broker_orders WHERE id=?",
            (s["buy_bo_id"],),
        ).fetchone()
        pos = conn.execute(
            "SELECT quantity FROM positions WHERE id=?", (s["position_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert bo[0] == "filled"
    assert bo[1] == 100  # 보정됨
    assert pos[0] == 100
    assert any("매수 추가 체결" in t for _, t in bot.sent)
    assert any("+46주" in t for _, t in bot.sent)


# ============================================================
# 시나리오 2: 진짜 부분체결 (잔고도 그대로) → 보정 안 됨
# ============================================================

def test_partial_recheck_skips_when_balance_unchanged(monkeypatch):
    """매수 100주 발주 → 54주 체결 인식 → 잔고도 54주 그대로 → 보정 안 됨."""
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 200
    s = _seed_partial_buy(chat_id, "AAA", requested_qty=100,
                          filled_qty=54, age_minutes=10)
    fake = FakeBroker(balance_positions=[
        {"code": "AAA", "quantity": 54, "current_price": 8000, "avg_price": 8000},
    ])
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_buy_partial_recheck(FakeCtx(bot=bot)))

    conn = get_connection()
    try:
        bo = conn.execute(
            "SELECT filled_quantity FROM broker_orders WHERE id=?",
            (s["buy_bo_id"],),
        ).fetchone()
        pos = conn.execute(
            "SELECT quantity FROM positions WHERE id=?", (s["position_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert bo[0] == 54
    assert pos[0] == 54
    assert not bot.sent  # 알림 없음


# ============================================================
# 시나리오 3: 윈도우 외 row 무시
# ============================================================

def test_partial_recheck_ignores_outside_window(monkeypatch):
    """5분 미만 (3분 전) + 30분 초과 (40분 전) row 는 윈도우 외라 무시."""
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 300
    s_too_recent = _seed_partial_buy(chat_id, "BBB", 100, 54, age_minutes=3)
    s_too_old = _seed_partial_buy(chat_id, "CCC", 100, 54, age_minutes=40)
    fake = FakeBroker(balance_positions=[
        {"code": "BBB", "quantity": 100, "current_price": 8000, "avg_price": 8000},
        {"code": "CCC", "quantity": 100, "current_price": 8000, "avg_price": 8000},
    ])
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_buy_partial_recheck(FakeCtx(bot=bot)))

    conn = get_connection()
    try:
        bo_recent = conn.execute(
            "SELECT filled_quantity FROM broker_orders WHERE id=?",
            (s_too_recent["buy_bo_id"],),
        ).fetchone()
        bo_old = conn.execute(
            "SELECT filled_quantity FROM broker_orders WHERE id=?",
            (s_too_old["buy_bo_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert bo_recent[0] == 54  # 그대로
    assert bo_old[0] == 54     # 그대로
    assert not bot.sent


# ============================================================
# 시나리오 4: 같은 종목 다른 매수 row 가 있으면 귀속 분리 정확
# ============================================================

def test_partial_recheck_attributes_correctly_with_other_holdings(monkeypatch):
    """같은 종목 다른 매수가 50주 보유 중 + 이번 매수 100주 발주 → 54 인식.
    KIS 잔고 150주 (50 + 100) → 이번 매수 귀속 = 150 - 50 = 100 → 보정.
    """
    from src.bot import scheduler
    from src.db.connection import get_connection
    from src.services import user_service

    chat_id = 400
    user_service.register_user(chat_id)
    created_old = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    # 기존 보유 50주 (다른 buy_order_id)
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO audit_log(chat_id, event_type, payload_json, ts) VALUES (?, 'order_buy', '{}', ?)",
            (chat_id, created_old),
        )
        audit_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, broker_order_id, created_at, updated_at)
               VALUES (?, 'kis_mock', 'buy', 'DDD', 50, 9000, 'filled',
                       50, 9000, 'ODNO-DDD-OLD', ?, ?)""",
            (audit_id, created_old, created_old),
        )
        old_buy_id = cur.lastrowid
        conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, opened_at)
               VALUES (?, 'DDD', 'bunt', ?, 9000, 50, 9500, 8500, 'open', ?)""",
            (chat_id, old_buy_id, created_old),
        )
    finally:
        conn.close()

    # 이번 매수 100주 발주 → 54 인식
    s = _seed_partial_buy(chat_id, "DDD", requested_qty=100,
                          filled_qty=54, age_minutes=10)
    fake = FakeBroker(balance_positions=[
        {"code": "DDD", "quantity": 150, "current_price": 9000, "avg_price": 9000},
    ])
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_buy_partial_recheck(FakeCtx(bot=bot)))

    conn = get_connection()
    try:
        bo = conn.execute(
            "SELECT filled_quantity FROM broker_orders WHERE id=?",
            (s["buy_bo_id"],),
        ).fetchone()
        pos = conn.execute(
            "SELECT quantity FROM positions WHERE id=?", (s["position_id"],),
        ).fetchone()
    finally:
        conn.close()

    assert bo[0] == 100  # 100 으로 보정 (150 - 50 = 100)
    assert pos[0] == 100
    assert any("+46주" in t for _, t in bot.sent)

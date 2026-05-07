"""job_pending_sell_polling — 매도 pending 폴링 + 좀비 정리 회귀 테스트.

2026-05-04 도입:
  - filled/partial → broker_orders + position 갱신
  - zombie (cancel '수량 없음' 응답) → bot DB 자동 정리
  - 그 외 pending → 10분 경과 시 사용자 알림 1회
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
    """is_kr_market_session_now → True 강제."""
    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "is_kr_market_session_now", lambda: True)


# ============================================================
# Fakes
# ============================================================

@dataclass
class FakeOrderResp:
    status: str
    filled_quantity: int = 0
    filled_avg_price: int = 0
    commission: int = 0
    tax: int = 0


class FakeBroker:
    def __init__(self, status_by_odno=None, cancel_results=None):
        self.status_by_odno = status_by_odno or {}
        self.cancel_results = cancel_results or {}

    async def get_order_status(self, odno: str):
        if odno in self.status_by_odno:
            return self.status_by_odno[odno]
        return FakeOrderResp(status="pending")

    async def cancel_order_detail(self, odno: str):
        return self.cancel_results.get(odno, {"ok": False, "msg_cd": "", "msg": "", "is_zombie": False})


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@dataclass
class FakeCtx:
    bot: FakeBot


def _seed_pending_sell(bo_id_target, chat_id, code, qty, buy_price, target, stop, odno, age_minutes=60):
    """매도 pending broker_orders + open position(sell_order_id 마킹) seed."""
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
        # buy order (이미 체결됨)
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, broker_order_id, created_at, updated_at)
               VALUES (?, 'kis_mock', 'buy', ?, ?, ?, 'filled', ?, ?, 'BUY-?', ?, ?)""",
            (audit_id, code, qty, buy_price, qty, buy_price, created, created),
        )
        buy_id = cur.lastrowid
        # position open
        cur = conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, opened_at)
               VALUES (?, ?, 'bunt', ?, ?, ?, ?, ?, 'open', ?)""",
            (chat_id, code, buy_id, buy_price, qty, target, stop, created),
        )
        pos_id = cur.lastrowid
        # sell pending
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, broker_order_id, created_at, updated_at)
               VALUES (?, 'kis_mock', 'sell', ?, ?, ?, 'pending', 0, 0, ?, ?, ?)""",
            (audit_id, code, qty, target, odno, created, created),
        )
        sell_id = cur.lastrowid
        conn.execute(
            "UPDATE positions SET sell_order_id=? WHERE id=?",
            (sell_id, pos_id),
        )
        return {"sell_bo_id": sell_id, "position_id": pos_id, "buy_bo_id": buy_id}
    finally:
        conn.close()


# ============================================================
# 시나리오 1: filled → 봇 DB 청산
# ============================================================

def test_sell_filled_closes_position_and_notifies(monkeypatch):
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 100
    s = _seed_pending_sell(0, chat_id, "AAA", 10, 100_000, 107_000, 96_000,
                           "ODNO-FILL-1", age_minutes=5)
    fake = FakeBroker(
        status_by_odno={
            "ODNO-FILL-1": FakeOrderResp(status="filled", filled_quantity=10,
                                         filled_avg_price=107_000, commission=160, tax=214)
        },
    )
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_pending_sell_polling(FakeCtx(bot=bot)))

    # 봇 DB 검증
    conn = get_connection()
    try:
        bo = conn.execute("SELECT status, filled_quantity, filled_avg_price FROM broker_orders WHERE id=?",
                          (s["sell_bo_id"],)).fetchone()
        pos = conn.execute("SELECT status, pnl FROM positions WHERE id=?",
                           (s["position_id"],)).fetchone()
    finally:
        conn.close()
    assert bo[0] == "filled"
    assert bo[1] == 10
    assert pos[0] == "closed"

    # 사용자 알림 발송
    assert any("매도 체결" in t for _, t in bot.sent)


# ============================================================
# 시나리오 2: 30분+ pending + cancel 시 'is_zombie' → 자동 정리
# ============================================================

def test_zombie_pending_sell_auto_cleaned(monkeypatch):
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 200
    s = _seed_pending_sell(0, chat_id, "BBB", 30, 60_000, 64_200, 57_600,
                           "ODNO-ZOMBIE-1", age_minutes=60)   # 60분 지남
    fake = FakeBroker(
        status_by_odno={"ODNO-ZOMBIE-1": FakeOrderResp(status="pending")},
        cancel_results={
            "ODNO-ZOMBIE-1": {
                "ok": False, "msg_cd": "40330000",
                "msg": "모의투자 정정/취소할 수량이 없습니다.", "is_zombie": True,
            }
        },
    )
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_pending_sell_polling(FakeCtx(bot=bot)))

    conn = get_connection()
    try:
        bo = conn.execute("SELECT status FROM broker_orders WHERE id=?",
                          (s["sell_bo_id"],)).fetchone()
        pos = conn.execute("SELECT status, sell_order_id FROM positions WHERE id=?",
                           (s["position_id"],)).fetchone()
        audit = conn.execute(
            "SELECT event_type FROM audit_log WHERE event_type='sell_zombie_cleaned' AND chat_id=?",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()

    assert bo[0] == "cancelled"
    assert pos[0] == "open"          # position 은 그대로 open (사용자가 다시 매도 가능)
    assert pos[1] is None             # sell_order_id 해제
    assert audit is not None          # zombie_cleaned 이벤트 기록


# ============================================================
# 시나리오 3: 30분 미만 pending → 좀비 검사 안 함
# ============================================================

def test_recent_pending_sell_not_treated_as_zombie(monkeypatch):
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 300
    # 5분만 지난 pending — 좀비 검사 X
    s = _seed_pending_sell(0, chat_id, "CCC", 10, 50_000, 53_500, 48_000,
                           "ODNO-RECENT-1", age_minutes=5)
    cancel_called = []

    class CountingBroker(FakeBroker):
        async def cancel_order_detail(self, odno: str):
            cancel_called.append(odno)
            return {"ok": False, "msg_cd": "", "msg": "", "is_zombie": False}

    fake = CountingBroker(
        status_by_odno={"ODNO-RECENT-1": FakeOrderResp(status="pending")},
    )
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_pending_sell_polling(FakeCtx(bot=bot)))

    # cancel_order_detail 이 호출되지 않아야 (30분 미만)
    assert cancel_called == []
    # 봇 DB 도 그대로
    conn = get_connection()
    try:
        bo = conn.execute("SELECT status FROM broker_orders WHERE id=?",
                          (s["sell_bo_id"],)).fetchone()
    finally:
        conn.close()
    assert bo[0] == "pending"


# ============================================================
# 시나리오 4: 10분 경과 + zombie 아님 → 사용자 1회 알림
# ============================================================

def test_pending_sell_10min_alert_sent_once(monkeypatch):
    from src.bot import scheduler
    from src.db.connection import get_connection

    chat_id = 400
    # 15분 지났지만 30분 미만 → 좀비 검사 안 함, 알림은 발송돼야
    # 테스트 의도: 정확히 10분 ~ 30분 사이의 timing 검증.
    s = _seed_pending_sell(0, chat_id, "DDD", 10, 80_000, 85_600, 76_800,
                           "ODNO-WAIT-1", age_minutes=15)
    fake = FakeBroker(
        status_by_odno={"ODNO-WAIT-1": FakeOrderResp(status="pending")},
    )
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", lambda mode: fake,
    )
    bot = FakeBot()

    # 1차 호출 — 알림 발송
    asyncio.run(scheduler.job_pending_sell_polling(FakeCtx(bot=bot)))
    assert any("미체결 10분 경과" in t for _, t in bot.sent)

    # 2차 호출 — dedup 으로 미발송
    bot2 = FakeBot()
    asyncio.run(scheduler.job_pending_sell_polling(FakeCtx(bot=bot2)))
    assert not any("미체결 10분" in t for _, t in bot2.sent)


# ============================================================
# 시나리오 5: paper 모드는 즉시 리턴 (KIS 호출 X)
# ============================================================

def test_paper_mode_skipped(monkeypatch):
    from src.bot import scheduler
    import src.config as cfg

    monkeypatch.setattr(cfg, "TRADE_MODE", cfg.TradeMode.PAPER)

    called = []
    def fake_get_broker(_):
        called.append(1)
        raise RuntimeError("should not be called")
    monkeypatch.setattr(
        "src.services.portfolio_service.get_broker", fake_get_broker,
    )
    bot = FakeBot()
    asyncio.run(scheduler.job_pending_sell_polling(FakeCtx(bot=bot)))
    assert called == []

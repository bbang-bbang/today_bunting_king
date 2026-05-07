"""/admin_stats 운영 메트릭 대시보드 — 회귀 테스트."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "1000000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "dummy")
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "999")
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


@dataclass
class FakeChat:
    id: int


@dataclass
class FakeUpdate:
    effective_chat: FakeChat
    message: FakeMessage = field(default_factory=FakeMessage)

    @property
    def effective_user(self):
        return self.effective_chat


@dataclass
class FakeCtx:
    args: list = field(default_factory=list)
    user_data: dict = field(default_factory=dict)


def _mk(chat_id):
    return FakeUpdate(effective_chat=FakeChat(id=chat_id))


# ============================================================
# 권한
# ============================================================

def test_admin_stats_rejects_non_admin():
    """non-admin 호출 시 거절."""
    from src.bot.telegram_bot import cmd_admin_stats
    upd = _mk(123)   # admin (999) 아님
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))
    assert "어드민 전용" in upd.message.replies[0]


def test_admin_stats_admin_returns_dashboard():
    """admin 호출 시 대시보드 반환."""
    from src.bot.telegram_bot import cmd_admin_stats
    upd = _mk(999)   # admin
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))

    text = upd.message.replies[0]
    assert "운영 메트릭" in text
    assert "현재 상태" in text
    assert "오늘" in text
    assert "7일" in text


# ============================================================
# 카운터 정확성
# ============================================================

def test_admin_stats_counts_today_events():
    from src.bot.telegram_bot import cmd_admin_stats
    from src.services import audit_service

    # 오늘자 이벤트 시드
    audit_service.log_event(999, "button_buy", {"code": "A"})
    audit_service.log_event(999, "button_buy", {"code": "B"})
    audit_service.log_event(999, "order_buy", {"code": "A"})
    audit_service.log_event(999, "sell_zombie_cleaned", {"code": "X"})
    audit_service.log_event(999, "pending_rec_alert", {"code": "Y"})

    upd = _mk(999)
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))

    text = upd.message.replies[0]
    assert "매수 클릭            2건" in text
    assert "매수 주문 등록       1건" in text
    assert "좀비 자동 정리       1건" in text
    assert "진입가 도달 알림     1건" in text


def test_admin_stats_tp_sl_counted_only_when_auto_sell_success():
    from src.bot.telegram_bot import cmd_admin_stats
    from src.services import audit_service

    # auto_sell + sell_success → 카운트
    audit_service.log_event(999, "price_alert", {
        "type": "tp_hit", "auto_sell": True, "sell_success": True,
    })
    audit_service.log_event(999, "price_alert", {
        "type": "sl_hit", "auto_sell": True, "sell_success": True,
    })
    audit_service.log_event(999, "price_alert", {
        "type": "sl_hit", "auto_sell": True, "sell_success": False,   # 실패는 제외
    })
    audit_service.log_event(999, "price_alert", {
        "type": "near_tp",   # 단순 알림 — 제외
    })

    upd = _mk(999)
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))
    text = upd.message.replies[0]

    assert "자동 익절 (TP)       1건" in text
    assert "자동 손절 (SL)       1건" in text


def test_admin_stats_empty_db_zeros():
    """이벤트 0건 DB 에서도 죽지 않고 모든 카운터 0 반환."""
    from src.bot.telegram_bot import cmd_admin_stats

    upd = _mk(999)
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))
    text = upd.message.replies[0]
    assert "매수 클릭            0건" in text
    assert "보유 포지션 (봇 DB)   0종목" in text

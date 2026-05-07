"""KIS 5xx 인스트루먼테이션 + /admin_stats 통합 회귀 테스트."""
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


# ============================================================
# /admin_stats 가 KIS 5xx 카운트 + top 종목 표시
# ============================================================

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


def test_admin_stats_shows_kis_5xx_count_zero():
    from src.bot.telegram_bot import cmd_admin_stats

    upd = _mk(999)
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))
    text = upd.message.replies[0]
    assert "KIS 5xx 발생         0건" in text


def test_admin_stats_counts_kis_5xx_with_top_codes():
    from src.bot.telegram_bot import cmd_admin_stats
    from src.services import audit_service

    # 178920 가 가장 자주 5xx
    for _ in range(5):
        audit_service.log_event(None, "kis_5xx", {
            "endpoint": "inquire-price", "code": "178920", "status": 500,
        })
    for _ in range(2):
        audit_service.log_event(None, "kis_5xx", {
            "endpoint": "inquire-price", "code": "425420", "status": 500,
        })
    audit_service.log_event(None, "kis_5xx", {
        "endpoint": "inquire-balance", "status": 500,
    })

    upd = _mk(999)
    asyncio.run(cmd_admin_stats(upd, FakeCtx()))
    text = upd.message.replies[0]

    assert "KIS 5xx 발생         8건" in text
    # 가장 빈번한 종목이 top 으로 노출
    assert "top:" in text
    assert "178920(5)" in text
    assert "425420(2)" in text


# ============================================================
# 매수 콜백 — "처리 중" 메시지가 먼저 발송되는지
# ============================================================

class FakeQuery:
    def __init__(self, data: str, chat_id: int):
        self.data = data
        self._from_id = chat_id
        self.edits: list[str] = []

    @property
    def from_user(self):
        from types import SimpleNamespace
        return SimpleNamespace(id=self._from_id)

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


@dataclass
class FakeUpd2:
    callback_query: FakeQuery


def test_buy_callback_sends_processing_message_first(monkeypatch):
    """bgo:confirm 클릭 시 KIS 호출 전 '처리 중' 먼저 표시."""
    from src.bot import telegram_bot
    from src.services import user_service, portfolio_service, recommendation_service as rs
    from datetime import date

    user_service.register_user(100)
    rec_id = rs.create_recommendation(
        chat_id=100, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date.today(),
    )

    # execute_buy 가 0.05초 걸리는 척 — 그 전에 "처리 중" 메시지 떠야
    async def slow_buy(**kwargs):
        await asyncio.sleep(0.05)
        return {"success": True, "position_id": 1, "code": "005930",
                "qty": 13, "price": 72_000, "target": 74_160, "stop": 70_560,
                "commission": 108}
    monkeypatch.setattr(portfolio_service, "execute_buy", slow_buy)

    intent = {"action": "buy", "rec_id": rec_id, "code": "005930",
              "quantity": 13, "price": 72_000, "strategy_mode": "bunt"}

    @dataclass
    class FCtx:
        user_data: dict = field(default_factory=dict)

    ctx = FCtx(user_data={"pending_buy": intent})
    query = FakeQuery(data="bgo:confirm", chat_id=100)
    asyncio.run(telegram_bot.cb_button(FakeUpd2(callback_query=query), ctx))

    # 1번째 edit = "처리 중", 마지막 edit = 최종 결과
    assert len(query.edits) >= 2
    assert "처리 중" in query.edits[0]
    assert "매수 체결" in query.edits[-1]

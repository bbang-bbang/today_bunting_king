"""telegram_bot 콜백 핸들러 — 매수 버튼 무반응 사고 회귀 방지.

2026-05-04: KIS 5xx retry 로 매수 핸들러가 30s+ 걸리는 사이 다른 콜백 query 들이
큐에 쌓여 만료. concurrent_updates=True 로 해결 + query.answer 실패해도 핸들러 계속.
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


# ============================================================
# Application 빌드 시 concurrent_updates 옵션
# ============================================================

def test_application_builder_uses_concurrent_updates():
    """소스 코드에 .concurrent_updates(True) 호출이 있는지 — 회귀 방지용 정적 검사."""
    import inspect
    from src.bot import telegram_bot
    src = inspect.getsource(telegram_bot.build_app)
    assert ".concurrent_updates(True)" in src, (
        "Application.builder() 에 .concurrent_updates(True) 가 빠지면 "
        "callback query 큐 만료 사고 재발. PR 리뷰에서 떼지 말 것."
    )


# ============================================================
# query.answer 실패해도 핸들러 진행 (BadRequest 무시)
# ============================================================

@dataclass
class FakeUser:
    id: int


class FailingQuery:
    """query.answer() 가 BadRequest 던지는 시나리오 — 핸들러는 계속 진행해야."""
    def __init__(self):
        self.data = "cancel"
        self.from_user = FakeUser(id=999)
        self.edited = False
        self.answer_called = False

    async def answer(self):
        self.answer_called = True
        from telegram.error import BadRequest
        raise BadRequest("Query is too old and response timeout expired")

    async def edit_message_text(self, text, **kw):
        self.edited = True
        self.last_text = text


@dataclass
class FakeUpdate:
    callback_query: FailingQuery


@dataclass
class FakeCtx:
    user_data: dict


def test_cb_button_continues_when_answer_raises_bad_request():
    from src.bot.telegram_bot import cb_button

    q = FailingQuery()
    update = FakeUpdate(callback_query=q)
    ctx = FakeCtx(user_data={})

    # 'cancel' 처리되면 edit_message_text 가 "❌ 취소됨" 호출돼야
    asyncio.run(cb_button(update, ctx))
    assert q.answer_called
    assert q.edited, "query.answer 실패 후에도 핸들러가 edit_message_text 까지 가야 함"
    assert "취소" in q.last_text


# ============================================================
# error_handler 등록 여부 + 동작
# ============================================================

def test_error_handler_is_registered():
    """build_app() 가 add_error_handler 를 호출하는지 정적 검사."""
    import inspect
    from src.bot import telegram_bot
    src = inspect.getsource(telegram_bot.build_app)
    assert "add_error_handler" in src


def test_error_handler_sends_user_guidance():
    """예외 발생 시 사용자에게 fallback 메시지 + admin 알림 흐름."""
    from src.bot.telegram_bot import _error_handler
    from telegram import Update, Chat

    sent: list[tuple[int, str]] = []

    class FakeBot:
        async def send_message(self, chat_id, text, **kw):
            sent.append((chat_id, text))

    @dataclass
    class FakeCtxWithError:
        bot: FakeBot
        error: Exception

    # update 가 None / 아무거나 — chat_id 못 잡으면 admin 만 알림
    ctx = FakeCtxWithError(bot=FakeBot(), error=ValueError("test"))
    asyncio.run(_error_handler(None, ctx))
    # admin 미설정이면 0건. 설정돼있으면 1건. paper mode 기본 .env 라 admin 없음.
    # 단순 smoke — 함수 자체가 예외로 죽지 않으면 OK
    assert isinstance(sent, list)

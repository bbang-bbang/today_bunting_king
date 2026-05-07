"""cb_button 통합 테스트 — 매수/건너뜀/태그 클릭 전이 체인 + DB 상태 검증."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

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
# fake Telegram objects
# ============================================================

@dataclass
class FakeQuery:
    data: str
    chat_id: int
    message_text: str = ""
    edits: list[tuple[str, dict]] = field(default_factory=list)

    @property
    def from_user(self):
        return SimpleNamespace(id=self.chat_id)

    @property
    def message(self):
        return SimpleNamespace(text=self.message_text)

    async def answer(self):
        pass

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


@dataclass
class FakeUpdate:
    callback_query: FakeQuery


@dataclass
class FakeCtx:
    """user_data 딕셔너리만 흉내 — 현재 매수 플로우는 user_data 에 intent 저장."""
    user_data: dict = field(default_factory=dict)


# ============================================================
# helpers
# ============================================================

def _register(chat_id: int):
    from src.services import user_service
    user_service.register_user(chat_id)


def _create_rec(chat_id: int) -> str:
    from src.services import recommendation_service as rs
    from datetime import date
    return rs.create_recommendation(
        chat_id=chat_id, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date.today(),
    )


def _make_buy_conf(chat_id: int, rec_id: str) -> str:
    from src.services import confirmation_service
    return confirmation_service.create(chat_id, {
        "action": "buy", "rec_id": rec_id, "code": "005930",
        "quantity": 13, "price": 72_000, "strategy_mode": "bunt",
    })


def _count_actions(chat_id: int) -> list[dict]:
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT rec_id, action_type, reason_tag, price, quantity "
            "FROM recommendation_actions WHERE chat_id=? ORDER BY id",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ============================================================
# cancel / unknown
# ============================================================

def test_cancel_only_edits_message():
    from src.bot.telegram_bot import cb_button
    query = FakeQuery(data="cancel", chat_id=1)
    asyncio.run(cb_button(FakeUpdate(query), None))
    assert query.edits[0][0] == "❌ 취소됨"


def test_unknown_callback_data():
    from src.bot.telegram_bot import cb_button
    query = FakeQuery(data="garbage:xxx", chat_id=1)
    asyncio.run(cb_button(FakeUpdate(query), None))
    assert "알 수 없는" in query.edits[0][0]


# ============================================================
# buy 버튼
# ============================================================

def _drive_buy_to_confirm(chat_id: int, rec_id: str, monkeypatch, fake_execute_buy):
    """현재 매수 흐름 (buy: → bprc:cur → bgo:confirm) 을 한 번에 진행하는 헬퍼.
    intent 를 user_data 에 직접 박아 price/qty 단계 생략하고 bgo:confirm 만 실행."""
    from src.bot import telegram_bot
    from src.services import portfolio_service

    monkeypatch.setattr(portfolio_service, "execute_buy", fake_execute_buy)
    # KIS 현재가 조회 (price 선택 화면) bypass — bgo:confirm 만 구동
    intent = {
        "action": "buy", "rec_id": rec_id, "code": "005930",
        "quantity": 13, "price": 72_000, "strategy_mode": "bunt",
    }
    ctx = FakeCtx(user_data={"pending_buy": intent})
    query = FakeQuery(data="bgo:confirm", chat_id=chat_id)
    asyncio.run(telegram_bot.cb_button(FakeUpdate(query), ctx))
    return query


def test_buy_success_inserts_action_and_shows_filled_message(monkeypatch):
    """bgo:confirm 으로 매수 확정 시 bought action 기록 + '매수 체결' 메시지."""
    _register(100)
    rec_id = _create_rec(100)

    async def fake_execute_buy(**kwargs):
        return {
            "success": True, "position_id": 1, "code": "005930",
            "qty": 13, "price": 72_000, "target": 74_160, "stop": 70_560,
            "commission": 108,
        }
    query = _drive_buy_to_confirm(100, rec_id, monkeypatch, fake_execute_buy)

    actions = _count_actions(100)
    assert len(actions) == 1
    assert actions[0]["action_type"] == "bought"
    assert actions[0]["reason_tag"] == "trust_ensemble"
    assert actions[0]["price"] == 72_000
    assert actions[0]["quantity"] == 13

    text, _kwargs = query.edits[-1]
    assert "매수 체결" in text
    assert "사유를 선택" not in text


def test_buy_failure_no_action_inserted(monkeypatch):
    _register(101)
    rec_id = _create_rec(101)

    async def fake_execute_buy(**kwargs):
        return {"success": False, "reason": "시드 초과"}
    query = _drive_buy_to_confirm(101, rec_id, monkeypatch, fake_execute_buy)

    assert _count_actions(101) == []
    assert "거절" in query.edits[-1][0]


def test_buy_partial_fill_message_emphasizes_unfilled(monkeypatch):
    """부분 체결 시 요청/체결/미체결 + 자동매도가 체결분 기준이라는 점 명시.

    2026-05-04 사고: 30주 요청 → 11주 체결인데 메시지가 약하게 "(부분)" 만 보여서
    사용자가 미체결 19주 인지 못 함.
    """
    _register(102)
    rec_id = _create_rec(102)

    async def fake_execute_buy(**kwargs):
        return {
            "success": True, "position_id": 1, "code": "005930",
            "qty": 11, "requested_qty": 30, "price": 72_000,
            "target": 74_160, "stop": 70_560, "commission": 108,
            "partial": True,
        }
    query = _drive_buy_to_confirm(102, rec_id, monkeypatch, fake_execute_buy)

    text = query.edits[-1][0]
    assert "부분 체결" in text
    assert "11주" in text
    assert "미체결 19주" in text         # 차이 명시
    assert "자동 익절/손절" in text       # 체결분만 보호된다는 점
    assert "체결된 11주" in text
    assert "/reconcile" in text


def test_buy_with_expired_confirmation():
    from src.bot.telegram_bot import cb_button
    _register(102)
    rec_id = _create_rec(102)
    _make_buy_conf(102, rec_id)   # 실제 uuid 미사용

    query = FakeQuery(data="buy:nonexistent-uuid", chat_id=102)
    asyncio.run(cb_button(FakeUpdate(query), None))
    assert "만료" in query.edits[-1][0] or "이미" in query.edits[-1][0]
    assert _count_actions(102) == []


# ============================================================
# buy 후 자동 사유 기록 (사용자 선택 UI 없음 — 2026-04-22 결정)
# ============================================================

def test_buy_auto_trust_ensemble(monkeypatch):
    """매수 성공 시 reason_tag 가 자동으로 trust_ensemble 로 기록되는지."""
    _register(110)
    rec_id = _create_rec(110)

    async def fake_execute_buy(**kwargs):
        return {"success": True, "position_id": 1, "code": "005930",
                "qty": 13, "price": 72_000, "target": 74_160, "stop": 70_560,
                "commission": 108}
    _drive_buy_to_confirm(110, rec_id, monkeypatch, fake_execute_buy)

    actions = _count_actions(110)
    assert len(actions) == 1
    assert actions[0]["reason_tag"] == "trust_ensemble"


# ============================================================
# skip 버튼 + skipped 태그
# ============================================================

def test_skip_button_edits_message_only_no_action_yet():
    """skip 클릭 단계에서는 태그 키보드만 뜨고 DB 행위 로그는 아직 없음."""
    from src.bot.telegram_bot import cb_button
    _register(120)
    rec_id = _create_rec(120)
    conf_uuid = _make_buy_conf(120, rec_id)

    query = FakeQuery(data=f"skip:{conf_uuid}", chat_id=120)
    asyncio.run(cb_button(FakeUpdate(query), None))

    assert _count_actions(120) == []
    text, kwargs = query.edits[-1]
    assert "건너뜀" in text
    assert kwargs.get("reply_markup") is not None


def test_skipped_tag_inserts_action():
    from src.bot import telegram_bot
    _register(121)
    rec_id = _create_rec(121)

    query = FakeQuery(data=f"rtag:s:{rec_id}:missed_timing", chat_id=121)
    asyncio.run(telegram_bot.cb_button(FakeUpdate(query), None))

    actions = _count_actions(121)
    assert len(actions) == 1
    assert actions[0]["action_type"] == "skipped"
    assert actions[0]["reason_tag"] == "missed_timing"


def test_skipped_tag_rejects_wrong_enum():
    from src.bot import telegram_bot
    _register(122)
    rec_id = _create_rec(122)
    query = FakeQuery(data=f"rtag:s:{rec_id}:trust_ensemble", chat_id=122)
    asyncio.run(telegram_bot.cb_button(FakeUpdate(query), None))
    assert "오류" in query.edits[-1][0]
    assert _count_actions(122) == []


# ============================================================
# sold 태그 (매도 리마인더에서 뜨는 버튼)
# ============================================================

def test_sold_tag_inserts_action():
    from src.bot import telegram_bot
    _register(130)
    rec_id = _create_rec(130)

    query = FakeQuery(data=f"rtag:d:{rec_id}:target_hit", chat_id=130)
    asyncio.run(telegram_bot.cb_button(FakeUpdate(query), None))

    actions = _count_actions(130)
    assert len(actions) == 1
    assert actions[0]["action_type"] == "sold"
    assert actions[0]["reason_tag"] == "target_hit"


def test_sold_tag_rejects_wrong_enum():
    from src.bot import telegram_bot
    _register(131)
    rec_id = _create_rec(131)
    query = FakeQuery(data=f"rtag:d:{rec_id}:no_cash", chat_id=131)
    asyncio.run(telegram_bot.cb_button(FakeUpdate(query), None))
    assert "오류" in query.edits[-1][0]
    assert _count_actions(131) == []

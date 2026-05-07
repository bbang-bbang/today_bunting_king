"""scheduler.job_pending_rec_monitor — 같은 종목 번트+스퀴즈 통합 알림 테스트.

2026-05-04: 기존엔 같은 종목 양 모드 도달 시 메시지 2개 따로 발송. 이제 1개 통합.
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


@pytest.fixture(autouse=True)
def force_market_session(monkeypatch):
    """is_kr_market_session_now → True 강제."""
    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "is_kr_market_session_now", lambda: True)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.sent.append((chat_id, text, reply_markup))


@dataclass
class FakeCtx:
    bot: FakeBot


def _make_alert(rec_id, code, mode, current_price=10_000, entry=10_000):
    from src.services.price_monitor import PendingRecAlert
    return PendingRecAlert(
        rec_id=rec_id,
        code=code,
        strategy_mode=mode,
        entry_price=entry,
        target_price=int(entry * 1.07),
        stop_price=int(entry * 0.96),
        current_price=current_price,
        discount_pct=(entry - current_price) / entry * 100,
        ensemble_score=72.5,
        session_date="2026-05-04",
        name="테스트",
    )


# ============================================================
# 같은 종목 양 모드 → 1개 메시지로 통합
# ============================================================

def test_same_code_both_modes_consolidates_into_one_message(monkeypatch):
    from src.bot import scheduler
    from src.services import user_service

    chat_id = 100
    user_service.register_user(chat_id)

    # 같은 코드 003490 이 번트+스퀴즈 양쪽 도달
    alerts = [
        _make_alert("KR-20260504-01", "003490", "bunt"),
        _make_alert("KR-20260504-02", "003490", "squeeze"),
    ]

    # _get_pending_rec_monitor 가 위 alerts 반환하도록 mock
    class FakeMonitor:
        def check_pending_recs(self, cid, days=1):
            return alerts
    monkeypatch.setattr(scheduler, "_get_pending_rec_monitor", lambda: FakeMonitor())

    bot = FakeBot()
    asyncio.run(scheduler.job_pending_rec_monitor(FakeCtx(bot)))

    # 메시지 1개만 발송됐는지
    assert len(bot.sent) == 1
    chat, text, kb = bot.sent[0]
    assert chat == chat_id

    # 통합 메시지 내용
    assert "003490" in text
    assert "🔁 양 모드 추천" in text
    assert "[번트]" in text
    assert "[스퀴즈]" in text

    # 매수 버튼 2개 + 무시 버튼 1개
    inline_buttons = kb.inline_keyboard[0]
    assert len(inline_buttons) == 3
    labels = [b.text for b in inline_buttons]
    assert "매수 (번트)" in labels
    assert "매수 (스퀴즈)" in labels
    assert "무시" in labels


# ============================================================
# 한 모드만 도달 → 단일 메시지 (기존 포맷)
# ============================================================

def test_single_mode_uses_single_format(monkeypatch):
    from src.bot import scheduler
    from src.services import user_service

    chat_id = 200
    user_service.register_user(chat_id)

    alerts = [_make_alert("KR-20260504-03", "005930", "bunt")]

    class FakeMonitor:
        def check_pending_recs(self, cid, days=1):
            return alerts
    monkeypatch.setattr(scheduler, "_get_pending_rec_monitor", lambda: FakeMonitor())

    bot = FakeBot()
    asyncio.run(scheduler.job_pending_rec_monitor(FakeCtx(bot)))

    assert len(bot.sent) == 1
    _, text, kb = bot.sent[0]

    # 단일 모드 포맷에는 "🔁 양 모드" 가 없어야 함
    assert "🔁 양 모드" not in text
    assert "🎯 진입가 도달" in text
    assert "005930" in text

    # 매수 버튼 1개 + 무시
    inline_buttons = kb.inline_keyboard[0]
    assert len(inline_buttons) == 2
    assert inline_buttons[0].text == "매수 (번트)"
    assert inline_buttons[1].text == "무시"


# ============================================================
# 다른 종목들은 각자 메시지
# ============================================================

def test_different_codes_separate_messages(monkeypatch):
    from src.bot import scheduler
    from src.services import user_service

    chat_id = 300
    user_service.register_user(chat_id)

    alerts = [
        _make_alert("KR-20260504-04", "005930", "bunt"),
        _make_alert("KR-20260504-05", "000660", "squeeze"),
        _make_alert("KR-20260504-06", "035720", "bunt"),
    ]

    class FakeMonitor:
        def check_pending_recs(self, cid, days=1):
            return alerts
    monkeypatch.setattr(scheduler, "_get_pending_rec_monitor", lambda: FakeMonitor())

    bot = FakeBot()
    asyncio.run(scheduler.job_pending_rec_monitor(FakeCtx(bot)))

    # 3개 종목 → 3개 메시지
    assert len(bot.sent) == 3
    codes_in_msgs = [text for _, text, _ in bot.sent]
    assert any("005930" in t for t in codes_in_msgs)
    assert any("000660" in t for t in codes_in_msgs)
    assert any("035720" in t for t in codes_in_msgs)


# ============================================================
# audit_log: 통합 시 양 모드 모두 기록
# ============================================================

def test_audit_log_records_both_modes_when_consolidated(monkeypatch):
    from src.bot import scheduler
    from src.services import user_service
    from src.db.connection import get_connection

    chat_id = 400
    user_service.register_user(chat_id)

    alerts = [
        _make_alert("KR-20260504-07", "035420", "bunt"),
        _make_alert("KR-20260504-08", "035420", "squeeze"),
    ]

    class FakeMonitor:
        def check_pending_recs(self, cid, days=1):
            return alerts
    monkeypatch.setattr(scheduler, "_get_pending_rec_monitor", lambda: FakeMonitor())

    bot = FakeBot()
    asyncio.run(scheduler.job_pending_rec_monitor(FakeCtx(bot)))

    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT payload_json FROM audit_log WHERE event_type='pending_rec_alert'"
        ).fetchall()
    finally:
        conn.close()

    import json
    modes = []
    consolidated_flags = []
    for r in rows:
        p = json.loads(r[0])
        modes.append(p["mode"])
        consolidated_flags.append(p.get("consolidated"))

    assert sorted(modes) == ["bunt", "squeeze"]
    assert all(consolidated_flags)

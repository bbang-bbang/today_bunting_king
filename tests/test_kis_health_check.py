"""job_kis_health_check — KIS 5xx 다발 자동 알림 회귀 테스트."""
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
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", "999")
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


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


@dataclass
class FakeCtx:
    bot: FakeBot


# ============================================================
# 임계치 미달 → 알림 X
# ============================================================

def test_health_check_no_alert_below_threshold():
    from src.bot import scheduler
    from src.services import audit_service

    # 5건만 (임계 30 미만)
    for _ in range(5):
        audit_service.log_event(None, "kis_5xx", {"code": "178920"})

    bot = FakeBot()
    asyncio.run(scheduler.job_kis_health_check(FakeCtx(bot=bot)))
    assert bot.sent == []


# ============================================================
# 임계치 도달 → admin 1회 알림
# ============================================================

def test_health_check_alerts_admin_when_threshold_reached():
    from src.bot import scheduler
    from src.services import audit_service

    # 35건 — top: 178920 20, 425420 10, balance 5
    for _ in range(20):
        audit_service.log_event(None, "kis_5xx", {"code": "178920"})
    for _ in range(10):
        audit_service.log_event(None, "kis_5xx", {"code": "425420"})
    for _ in range(5):
        audit_service.log_event(None, "kis_5xx", {"endpoint": "inquire-balance"})

    bot = FakeBot()
    asyncio.run(scheduler.job_kis_health_check(FakeCtx(bot=bot)))

    assert len(bot.sent) == 1
    chat, text = bot.sent[0]
    assert chat == 999
    assert "KIS 5xx 다발" in text
    assert "35건" in text
    assert "178920" in text and "20건" in text
    assert "425420" in text


# ============================================================
# 60분 dedup
# ============================================================

def test_health_check_dedups_within_60min():
    from src.bot import scheduler
    from src.services import audit_service

    for _ in range(35):
        audit_service.log_event(None, "kis_5xx", {"code": "X"})

    bot1 = FakeBot()
    asyncio.run(scheduler.job_kis_health_check(FakeCtx(bot=bot1)))
    assert len(bot1.sent) == 1

    # 즉시 두 번째 호출 — dedup 으로 미발송
    for _ in range(35):
        audit_service.log_event(None, "kis_5xx", {"code": "X"})
    bot2 = FakeBot()
    asyncio.run(scheduler.job_kis_health_check(FakeCtx(bot=bot2)))
    assert bot2.sent == []


# ============================================================
# 장 외 시간 → 스킵
# ============================================================

def test_health_check_skipped_outside_market_session(monkeypatch):
    from src.bot import scheduler
    from src.services import audit_service

    # 시장 세션 강제 OFF
    monkeypatch.setattr(scheduler, "is_kr_market_session_now", lambda: False)

    for _ in range(50):
        audit_service.log_event(None, "kis_5xx", {"code": "X"})

    bot = FakeBot()
    asyncio.run(scheduler.job_kis_health_check(FakeCtx(bot=bot)))
    assert bot.sent == []


# ============================================================
# admin chat 미설정 → 스킵
# ============================================================

def test_health_check_skipped_when_no_admin(monkeypatch):
    from src.bot import scheduler
    import src.config as cfg
    from src.services import audit_service

    monkeypatch.setattr(cfg, "TELEGRAM_ADMIN_CHAT_ID", 0)

    for _ in range(50):
        audit_service.log_event(None, "kis_5xx", {"code": "X"})

    bot = FakeBot()
    asyncio.run(scheduler.job_kis_health_check(FakeCtx(bot=bot)))
    assert bot.sent == []

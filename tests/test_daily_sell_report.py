"""scheduler.job_daily_sell_report — 평일 08:00 매도 통계 리포트 테스트."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import date, timedelta

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
def force_trading_day(monkeypatch):
    """is_kr_trading_day → True 강제 (pykrx 호출 회피)."""
    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "_is_trading_day_cached", lambda iso: True)


class FakeBot:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@dataclass
class FakeCtx:
    bot: FakeBot


def _seed_closed_position(
    chat_id: int, code: str, buy_price: int, qty: int, pnl: int, closed_at: str,
    name: str = "테스트종목",
):
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code, name, market, updated_at) "
            "VALUES (?, ?, 'KOSPI', '2026-04-16')",
            (code, name),
        )
        cur = conn.execute(
            "INSERT INTO audit_log(chat_id, event_type, payload_json, ts) "
            "VALUES (?, 'order_buy', '{}', ?)",
            (chat_id, closed_at),
        )
        audit_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, created_at, updated_at)
               VALUES (?, 'paper', 'buy', ?, ?, ?, 'filled', ?, ?, ?, ?)""",
            (audit_id, code, qty, buy_price, qty, buy_price, closed_at, closed_at),
        )
        buy_order_id = cur.lastrowid
        conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, pnl, opened_at, closed_at)
               VALUES (?, ?, 'bunt', ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
            (chat_id, code, buy_order_id, buy_price, qty,
             buy_price * 103 // 100, buy_price * 98 // 100,
             pnl, closed_at, closed_at),
        )
    finally:
        conn.close()


# ============================================================
# 0건이면 발송 생략
# ============================================================

def test_no_sells_yesterday_skips_send():
    """직전 거래일 청산 0건 → send_message 호출 0회."""
    from src.bot.scheduler import job_daily_sell_report, _previous_trading_day_iso
    from src.services import user_service

    user_service.register_user(100)

    bot = FakeBot()
    asyncio.run(job_daily_sell_report(FakeCtx(bot)))
    assert bot.sent == []


# ============================================================
# 매도 1건 이상이면 발송
# ============================================================

def test_one_sell_sends_report():
    from src.bot.scheduler import job_daily_sell_report, _previous_trading_day_iso
    from src.services import user_service

    chat_id = 200
    user_service.register_user(chat_id)
    yesterday = _previous_trading_day_iso() + " 15:20:00"
    _seed_closed_position(
        chat_id, "005930", buy_price=70_000, qty=10, pnl=25_000,
        closed_at=yesterday, name="삼성전자",
    )

    bot = FakeBot()
    asyncio.run(job_daily_sell_report(FakeCtx(bot)))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "어제 매도 통계" in text
    assert "005930" in text
    assert "삼성전자" in text
    assert "+25,000원" in text
    assert "승 1/1" in text


def test_loss_sell_shows_in_report():
    from src.bot.scheduler import job_daily_sell_report, _previous_trading_day_iso
    from src.services import user_service

    chat_id = 300
    user_service.register_user(chat_id)
    yesterday = _previous_trading_day_iso() + " 14:00:00"
    _seed_closed_position(
        chat_id, "000660", buy_price=130_000, qty=5, pnl=-15_000,
        closed_at=yesterday, name="SK하이닉스",
    )

    bot = FakeBot()
    asyncio.run(job_daily_sell_report(FakeCtx(bot)))
    assert len(bot.sent) == 1
    text = bot.sent[0][1]
    assert "❌" in text             # 손실 이모지
    assert "-15,000원" in text
    assert "승 0/1" in text


def test_today_sell_not_in_yesterday_report():
    """오늘 청산된 포지션은 오늘 아침 리포트에 안 잡힘 (내일 잡힘)."""
    from src.bot.scheduler import job_daily_sell_report
    from src.services import user_service
    from datetime import datetime

    chat_id = 400
    user_service.register_user(chat_id)
    today_str = datetime.now().date().isoformat() + " 09:30:00"
    _seed_closed_position(
        chat_id, "005380", buy_price=200_000, qty=2, pnl=4_000,
        closed_at=today_str,
    )

    bot = FakeBot()
    asyncio.run(job_daily_sell_report(FakeCtx(bot)))
    assert bot.sent == []


# ============================================================
# 직전 거래일 계산
# ============================================================

def test_previous_trading_day_skips_weekend(monkeypatch):
    """월요일이면 직전 거래일은 금요일."""
    from src.bot import scheduler
    from datetime import datetime, timezone, timedelta

    KST = timezone(timedelta(hours=9))
    monday = datetime(2026, 5, 4, 8, 0, tzinfo=KST)  # 월

    real_dt = scheduler.datetime
    class FixedDt(real_dt):
        @classmethod
        def now(cls, tz=None):
            return monday.astimezone(tz) if tz else monday.replace(tzinfo=None)
    monkeypatch.setattr(scheduler, "datetime", FixedDt)

    assert scheduler._previous_trading_day_iso() == "2026-05-01"  # 금요일


def test_previous_trading_day_weekday(monkeypatch):
    """수요일이면 직전 거래일은 화요일."""
    from src.bot import scheduler
    from datetime import datetime, timezone, timedelta

    KST = timezone(timedelta(hours=9))
    wed = datetime(2026, 5, 6, 8, 0, tzinfo=KST)  # 수

    real_dt = scheduler.datetime
    class FixedDt(real_dt):
        @classmethod
        def now(cls, tz=None):
            return wed.astimezone(tz) if tz else wed.replace(tzinfo=None)
    monkeypatch.setattr(scheduler, "datetime", FixedDt)

    assert scheduler._previous_trading_day_iso() == "2026-05-05"  # 화요일

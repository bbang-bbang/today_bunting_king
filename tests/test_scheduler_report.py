"""scheduler.job_eod_review_report 테스트 — 추천 vs 실제 결과 리포트 생성."""
from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import date, datetime

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
# 헬퍼
# ============================================================

class FakeBot:
    """ctx.bot.send_message 만 흉내 — 호출된 (chat_id, text) 캡처."""
    def __init__(self):
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))


@dataclass
class FakeCtx:
    bot: FakeBot


def _seed_instrument(code: str, name: str):
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code, name, market, updated_at) "
            "VALUES (?, ?, 'KOSPI', '2026-04-16')",
            (code, name),
        )
    finally:
        conn.close()


def _seed_ohlcv_close(code: str, today: str, close: int):
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO ohlcv_daily(code, date, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, 100)""",
            (code, today, close, close, close, close),
        )
    finally:
        conn.close()


def _seed_closed_position(chat_id: int, code: str, buy_price: int, qty: int, pnl: int, today: str):
    """audit_log + broker_orders + positions 을 뭉쳐서 closed position 생성."""
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO audit_log(chat_id, event_type, payload_json, ts) "
            "VALUES (?, 'order_buy', '{}', ?)",
            (chat_id, today + " 09:00:00"),
        )
        audit_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO broker_orders
               (audit_id, trade_mode, side, code, quantity, price, status,
                filled_quantity, filled_avg_price, created_at, updated_at)
               VALUES (?, 'paper', 'buy', ?, ?, ?, 'filled', ?, ?, ?, ?)""",
            (audit_id, code, qty, buy_price, qty, buy_price,
             today + " 09:00:00", today + " 09:00:00"),
        )
        buy_order_id = cur.lastrowid
        conn.execute(
            """INSERT INTO positions
               (chat_id, code, strategy_mode, buy_order_id, buy_price, quantity,
                target_price, stop_price, status, pnl, opened_at, closed_at)
               VALUES (?, ?, 'bunt', ?, ?, ?, ?, ?, 'closed', ?, ?, ?)""",
            (chat_id, code, buy_order_id, buy_price, qty,
             buy_price * 103 // 100, buy_price * 98 // 100,
             pnl, today + " 09:00:00", today + " 15:20:00"),
        )
    finally:
        conn.close()


# ============================================================
# 테스트
# ============================================================

@pytest.fixture
def force_friday(monkeypatch):
    """job_eod_review_report 가 금요일에만 발사되므로 date.today() / datetime.now() 를
    금요일 (2026-05-01) 로 일관되게 강제. _is_trading_day_cached 도 True.

    insert_action 등 다른 곳에서 datetime.now() 호출 시에도 같은 날짜라야 SQL date()
    매칭이 일치 — date 와 datetime 둘 다 패치."""
    import src.bot.scheduler as sch_mod
    from datetime import date as _real_date, datetime as _real_dt

    FRIDAY = _real_date(2026, 5, 1)
    FRIDAY_DT = _real_dt(2026, 5, 1, 15, 40, 0)   # 15:40 = report 시간

    class FakeDate(_real_date):
        @classmethod
        def today(cls):
            return FRIDAY

    class FakeDatetime(_real_dt):
        @classmethod
        def now(cls, tz=None):
            return FRIDAY_DT.replace(tzinfo=tz) if tz else FRIDAY_DT

    monkeypatch.setattr(sch_mod, "_is_trading_day_cached", lambda iso: True)

    import datetime as _dt_module
    monkeypatch.setattr(_dt_module, "date", FakeDate)
    monkeypatch.setattr(_dt_module, "datetime", FakeDatetime)

    # 모듈에서 `from datetime import datetime` 한 곳들도 패치 (top-level import 라
    # 위 datetime.datetime 패치가 안 닿음).
    from src.services import recommendation_service as _rs
    from src.services import portfolio_service as _ps
    monkeypatch.setattr(_rs, "datetime", FakeDatetime)
    monkeypatch.setattr(_ps, "datetime", FakeDatetime)
    monkeypatch.setattr(sch_mod, "datetime", FakeDatetime)
    yield "2026-05-01"


def test_report_not_sent_when_no_recommendations(force_friday):
    """추천 없는 사용자에게는 리포트 미전송."""
    from src.bot.scheduler import job_eod_review_report
    from src.services import user_service

    user_service.register_user(10)
    bot = FakeBot()
    asyncio.run(job_eod_review_report(FakeCtx(bot)))
    assert bot.sent == []


def test_report_formats_bought_skipped_and_untouched(force_friday):
    """매수·건너뜀·미응답 3종 모두 리포트에 표시."""
    from src.bot.scheduler import job_eod_review_report
    from src.services import recommendation_service as rs
    from src.services import user_service
    from datetime import date as _real_date

    today_d = _real_date(2026, 5, 1)   # 금요일 — force_friday 와 일치
    today = today_d.isoformat()
    chat_id = 20

    user_service.register_user(chat_id)
    _seed_instrument("005930", "삼성전자")
    _seed_instrument("000660", "SK하이닉스")
    _seed_instrument("035720", "카카오")

    # rec1: 매수 후 매도 (승)
    rec1 = rs.create_recommendation(
        chat_id=chat_id, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t", ensemble_score=66.5,
        session_date=today_d,
    )
    a = rs.insert_action(
        rec_id=rec1, chat_id=chat_id, action_type="bought",
        reason_tag="trust_ensemble", price=72_000, quantity=13,
    )
    rs.insert_action(
        rec_id=rec1, chat_id=chat_id, action_type="sold",
        reason_tag="target_hit", price=74_160, quantity=13,
    )
    _seed_closed_position(chat_id, "005930", 72_000, 13, pnl=27_300, today=today)
    _seed_ohlcv_close("005930", today, 74_160)

    # rec2: 건너뜀
    rec2 = rs.create_recommendation(
        chat_id=chat_id, market="KR", code="000660", strategy_mode="bunt",
        entry_price=130_000, target_price=133_900, stop_price=127_400,
        expected_return_pct=3.0, reason_summary="t", ensemble_score=60.0,
        session_date=today_d,
    )
    rs.insert_action(
        rec_id=rec2, chat_id=chat_id, action_type="skipped",
        reason_tag="no_cash",
    )
    _seed_ohlcv_close("000660", today, 132_500)   # 매수했으면 +1.92%

    # rec3: 미응답
    rec3 = rs.create_recommendation(
        chat_id=chat_id, market="KR", code="035720", strategy_mode="bunt",
        entry_price=50_000, target_price=51_500, stop_price=49_000,
        expected_return_pct=3.0, reason_summary="t", ensemble_score=55.0,
        session_date=today_d,
    )
    _seed_ohlcv_close("035720", today, 49_500)   # -1.0%

    bot = FakeBot()
    asyncio.run(job_eod_review_report(FakeCtx(bot)))

    assert len(bot.sent) == 1
    sent_chat, text = bot.sent[0]
    assert sent_chat == chat_id

    # 세 rec_id 모두 포함
    assert rec1 in text
    assert rec2 in text
    assert rec3 in text

    # 상태 마커
    assert "🟢" in text   # bought
    assert "⏭" in text   # skipped
    assert "⬜" in text   # untouched

    # 집계 라인
    assert "추천 3" in text
    assert "매수 1" in text
    assert "건너뜀 2" in text
    # 실현 손익 +27,300
    assert "+27,300" in text
    # 승률 100.0%
    assert "100.0" in text

    # 건너뜀 가정 수익률
    assert "+1.92%" in text or "+1.93%" in text  # rounding
    # 미응답 종가 등락
    assert "-1.00%" in text


def test_report_win_rate_with_loss(force_friday):
    """손실 매수 + 익절 매수 혼재 시 승률 50%."""
    from src.bot.scheduler import job_eod_review_report
    from src.services import recommendation_service as rs
    from src.services import user_service
    from datetime import date as _real_date

    today_d = _real_date(2026, 5, 1)
    today = today_d.isoformat()
    chat_id = 30

    user_service.register_user(chat_id)
    _seed_instrument("A", "종목A")
    _seed_instrument("B", "종목B")

    recA = rs.create_recommendation(
        chat_id=chat_id, market="KR", code="A", strategy_mode="bunt",
        entry_price=10_000, target_price=10_300, stop_price=9_800,
        expected_return_pct=3.0, reason_summary="t",
        session_date=today_d,
    )
    rs.insert_action(
        rec_id=recA, chat_id=chat_id, action_type="bought",
        reason_tag="trust_ensemble", price=10_000, quantity=10,
    )
    _seed_closed_position(chat_id, "A", 10_000, 10, pnl=3_000, today=today)
    _seed_ohlcv_close("A", today, 10_300)

    recB = rs.create_recommendation(
        chat_id=chat_id, market="KR", code="B", strategy_mode="bunt",
        entry_price=20_000, target_price=20_600, stop_price=19_600,
        expected_return_pct=3.0, reason_summary="t",
        session_date=today_d,
    )
    rs.insert_action(
        rec_id=recB, chat_id=chat_id, action_type="bought",
        reason_tag="intuition", price=20_000, quantity=5,
    )
    _seed_closed_position(chat_id, "B", 20_000, 5, pnl=-2_000, today=today)
    _seed_ohlcv_close("B", today, 19_600)

    bot = FakeBot()
    asyncio.run(job_eod_review_report(FakeCtx(bot)))

    assert len(bot.sent) == 1
    _, text = bot.sent[0]
    # 순손익 +3000 -2000 = +1000
    assert "+1,000" in text
    # 승률 50.0%
    assert "50.0" in text
    # 양쪽 rec_id 포함
    assert recA in text and recB in text


def test_report_skips_users_with_no_today_session():
    """session_date 가 오늘이 아닌 추천만 있는 사용자는 리포트 미전송."""
    from src.bot.scheduler import job_eod_review_report
    from src.services import recommendation_service as rs
    from src.services import user_service

    user_service.register_user(40)
    rs.create_recommendation(
        chat_id=40, market="KR", code="005930", strategy_mode="bunt",
        entry_price=70_000, target_price=72_100, stop_price=68_600,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2025, 1, 1),     # 과거
    )
    bot = FakeBot()
    asyncio.run(job_eod_review_report(FakeCtx(bot)))
    assert bot.sent == []

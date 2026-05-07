"""send_recommendations_dual idempotent 진입 가드 회귀 테스트.

2026-04-28 사고 — push_recommend_now.py + cmd_recommend 동시 실행으로
같은 (chat_id, session_date) 추천이 두 번(01-20 + 21-40) 작성된 케이스.
"""
from __future__ import annotations

import asyncio
import importlib
from datetime import date

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "1000000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


def _seed_user_and_rec(chat_id: int, session_date: str) -> None:
    from src.db.connection import get_connection
    from src.services import recommendation_service, user_service

    user_service.register_user(chat_id)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code, name, market, updated_at) "
            "VALUES (?, ?, ?, '2026-04-28')",
            ("005930", "삼성전자", "KOSPI"),
        )
    finally:
        conn.close()
    recommendation_service.create_recommendation(
        chat_id=chat_id,
        market="KR",
        code="005930",
        strategy_mode="bunt",
        entry_price=72_000,
        target_price=77_040,
        stop_price=69_120,
        expected_return_pct=7.0,
        reason_summary="test",
        ensemble_score=60.0,
        session_date=date.fromisoformat(session_date),
    )


def test_cached_recs_dedupes_same_code_keeping_canonical(monkeypatch):
    """기존 사고 데이터 — 같은 code 가 여러 rec_id 로 저장돼도 cache 조회 시 canonical(가장 빠른) 1건만."""
    from src.bot.scheduler import _cached_recs_for_today
    from src.db.connection import get_connection
    from src.services import recommendation_service, user_service

    chat_id = 777
    today = date.today()
    user_service.register_user(chat_id)
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code, name, market, updated_at) "
            "VALUES (?, ?, ?, '2026-04-28')",
            ("033240", "자화전자", "KOSPI"),
        )
    finally:
        conn.close()
    # 같은 (chat_id, session_date, mode, code) 로 두 rec_id 저장 (사고 재현)
    rec_a = recommendation_service.create_recommendation(
        chat_id=chat_id, market="KR", code="033240", strategy_mode="bunt",
        entry_price=48_900, target_price=52_323, stop_price=46_944,
        expected_return_pct=7.0, reason_summary="A", ensemble_score=51.2,
        session_date=today,
    )
    rec_b = recommendation_service.create_recommendation(
        chat_id=chat_id, market="KR", code="033240", strategy_mode="bunt",
        entry_price=48_900, target_price=52_323, stop_price=46_944,
        expected_return_pct=7.0, reason_summary="B", ensemble_score=51.2,
        session_date=today,
    )
    assert rec_a < rec_b  # 빠른 시퀀스가 canonical

    rows = _cached_recs_for_today(chat_id, "bunt", today.isoformat())
    assert len(rows) == 1, f"중복 정리 후 1건이어야 하는데 {len(rows)}건 반환"
    assert rows[0][0] == rec_b, f"가장 늦은 rec_id 가 canonical (최신 갱신본): got {rows[0][0]}"


def test_send_recommendations_dual_skips_when_today_already_has_recs(monkeypatch):
    """오늘 (chat_id, session_date) 추천 존재 → fresh gen 안 돌리고 cache replay 위임."""
    chat_id = 999
    today = date.today().isoformat()
    _seed_user_and_rec(chat_id, today)

    # recommend() 가 호출되면 안 됨 — 만약 호출되면 실패시킴
    def _fail_recommend(*args, **kwargs):
        raise AssertionError("send_recommendations_dual 이 fresh recommend() 를 호출함 — 진입 가드 실패")

    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "recommend", _fail_recommend)

    bot = _FakeBot()
    n = asyncio.run(scheduler.send_recommendations_dual(bot, chat_id, ["005930"]))

    # cache replay 가 1건 발송 (per_cap // 72_000 >= 1)
    assert n >= 1, f"cache replay 가 동작해야 하는데 발송 0건: {bot.sent}"

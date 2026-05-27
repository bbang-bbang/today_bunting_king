"""파이프라인 헬스 체크 테스트.

silent 데이터 실패(StepResult ok=True 인데 0건/stale)를 DB 독립 검증으로 잡는다.
2026-05: 종목마스터 3주 0건, 수급 4주 정지가 우연히 발견된 사고 → 자동 감지 추가.
"""
from __future__ import annotations

import asyncio
from datetime import date

import pytest

from src.bot import scheduler
from src.db.connection import get_connection, init_schema


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import src.config as cfg
    monkeypatch.setattr(cfg, "DB_PATH", db_path)
    import src.db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)
    init_schema()
    yield db_path


def _seed_green(today: date, prev: date) -> None:
    """전 검사 통과하는 신선한 데이터 시딩."""
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT INTO instruments (code,name,market,is_tradable,updated_at) VALUES (?,?,?,1,?)",
            [(f"{i:06d}", f"N{i}", "KOSPI", today.isoformat()) for i in range(2000)],
        )
        conn.execute(
            "INSERT INTO ohlcv_daily (code,date,open,high,low,close,volume,value,change_pct) "
            "VALUES ('000000',?,1,1,1,1,1,1,0)",
            (prev.isoformat(),),
        )
        conn.executemany(
            "INSERT INTO fundamentals_snapshot "
            "(code,snapshot_date,market_cap,per,pbr,roe,debt_ratio,is_warning,is_watch,source) "
            "VALUES (?,?,1,1,1,1,1,0,0,'test')",
            [(f"{i:06d}", prev.isoformat()) for i in range(400)],
        )
        conn.executemany(
            "INSERT INTO investor_flow (date,code,foreign_net,institution_net,individual_net) "
            "VALUES (?,?,0,0,0)",
            [(prev.isoformat(), f"{i:06d}") for i in range(400)],
        )
        conn.executemany(
            "INSERT INTO analysis_universe (code,market_cap,adv_20d,rank,added_at) VALUES (?,1,1,?,?)",
            [(f"{i:06d}", i + 1, today.isoformat()) for i in range(400)],
        )
        conn.execute(
            "INSERT INTO bot_users (chat_id,status,registered_at) VALUES (999,'approved','t')"
        )
        conn.execute(
            "INSERT INTO recommendations "
            "(rec_id,chat_id,session_date,market,code,name,strategy_mode,"
            " entry_price,target_price,stop_price,expected_return_pct,ensemble_score,"
            " reason_summary,reason_json,sent_at) "
            "VALUES ('r1',999,?,'KR','000000','N','bunt',1,1,1,1.0,1.0,'','{}',?)",
            (today.isoformat(), today.isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def _as_dict(checks):
    return {name: (ok, detail) for name, ok, detail in checks}


# ============================================================
# _check_pipeline_health — DB 독립 신선도 검증
# ============================================================

def test_empty_db_all_red(temp_db):
    """빈 DB → 전 검사 RED (SQL 유효성 + 전체실패 감지 확인)."""
    checks = scheduler._check_pipeline_health()
    assert len(checks) == 6
    assert all(not ok for _, ok, _ in checks)


def test_all_green_when_fresh(temp_db):
    today = date.today()
    prev = scheduler._prev_trading_day(today)
    _seed_green(today, prev)
    checks = _as_dict(scheduler._check_pipeline_health())
    assert all(ok for ok, _ in checks.values()), checks


def test_flags_stale_investor_flow(temp_db):
    today = date.today()
    prev = scheduler._prev_trading_day(today)
    _seed_green(today, prev)
    conn = get_connection()
    try:
        conn.execute("UPDATE investor_flow SET date='2020-01-01'")
        conn.commit()
    finally:
        conn.close()
    checks = _as_dict(scheduler._check_pipeline_health())
    assert checks["수급"][0] is False
    assert checks["OHLCV"][0] is True   # 다른 검사는 영향 없음


def test_flags_collapsed_flow_coverage(temp_db):
    """수급 날짜는 신선하지만 종목수가 붕괴(10종목) → RED."""
    today = date.today()
    prev = scheduler._prev_trading_day(today)
    _seed_green(today, prev)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM investor_flow WHERE code >= '000010'")  # 10종목만 남김
        conn.commit()
    finally:
        conn.close()
    checks = _as_dict(scheduler._check_pipeline_health())
    assert checks["수급"][0] is False


def test_flags_collapsed_universe(temp_db):
    today = date.today()
    prev = scheduler._prev_trading_day(today)
    _seed_green(today, prev)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM analysis_universe WHERE code >= '000001'")  # 1종목
        conn.commit()
    finally:
        conn.close()
    checks = _as_dict(scheduler._check_pipeline_health())
    assert checks["유니버스"][0] is False


def test_flags_stale_instruments(temp_db):
    today = date.today()
    prev = scheduler._prev_trading_day(today)
    _seed_green(today, prev)
    conn = get_connection()
    try:
        conn.execute("UPDATE instruments SET updated_at='2020-01-01T00:00:00'")
        conn.commit()
    finally:
        conn.close()
    checks = _as_dict(scheduler._check_pipeline_health())
    assert checks["종목마스터"][0] is False


def test_flags_no_recommendations(temp_db):
    today = date.today()
    prev = scheduler._prev_trading_day(today)
    _seed_green(today, prev)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM recommendations")
        conn.commit()
    finally:
        conn.close()
    checks = _as_dict(scheduler._check_pipeline_health())
    assert checks["추천발송"][0] is False


# ============================================================
# job_pipeline_health_check — RED 일 때만 경보
# ============================================================

class _FakeBot:
    def __init__(self):
        self.sent: list[tuple] = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text))


class _Ctx:
    def __init__(self, bot):
        self.bot = bot


def _run_job(monkeypatch, checks, *, trading_day=True, admin=999):
    monkeypatch.setattr(scheduler, "is_kr_trading_day", lambda: trading_day)
    monkeypatch.setattr(scheduler.config, "TELEGRAM_ADMIN_CHAT_ID", admin)
    monkeypatch.setattr(scheduler, "_check_pipeline_health", lambda: checks)
    monkeypatch.setattr(scheduler.audit_service, "log_event", lambda *a, **k: None)
    bot = _FakeBot()
    asyncio.run(scheduler.job_pipeline_health_check(_Ctx(bot)))
    return bot


def test_job_no_alert_when_all_green(monkeypatch):
    checks = [("종목마스터", True, ""), ("수급", True, "")]
    bot = _run_job(monkeypatch, checks)
    assert bot.sent == []


def test_job_alerts_on_failure(monkeypatch):
    checks = [("종목마스터", True, "2777종목"), ("수급", False, "최신 2020-01-01 · 10종목")]
    bot = _run_job(monkeypatch, checks)
    assert len(bot.sent) == 1
    chat_id, text = bot.sent[0]
    assert chat_id == 999
    assert "🚨" in text
    assert "수급" in text
    assert "종목마스터" in text   # green 항목은 ✅ 요약에 노출


def test_job_skips_when_not_trading_day(monkeypatch):
    checks = [("수급", False, "fail")]
    bot = _run_job(monkeypatch, checks, trading_day=False)
    assert bot.sent == []


def test_job_skips_without_admin_chat(monkeypatch):
    checks = [("수급", False, "fail")]
    bot = _run_job(monkeypatch, checks, admin=None)
    assert bot.sent == []

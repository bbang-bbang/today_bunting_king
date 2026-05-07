"""일봉 증분에 --universe-only flag 적용 테스트.

5/6 운영 진단: 데이터 재수집 18분 중 ~99% 가 일봉 증분(전 종목 2,771).
universe(500) 만 처리하면 3~4분으로 단축.
"""
from __future__ import annotations

import pytest

from src.crawlers import collect_all, fetch_ohlcv
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


# ============================================================
# fetch_ohlcv.list_universe_codes
# ============================================================

def test_list_universe_codes_returns_codes_in_rank_order(temp_db):
    conn = get_connection()
    try:
        # instruments FK 충족용 stub
        conn.execute(
            "INSERT INTO instruments (code, name, market, is_tradable, updated_at) VALUES "
            "('005930', 'A', 'KOSPI', 1, '2026-05-06'), "
            "('035720', 'B', 'KOSPI', 1, '2026-05-06'), "
            "('000660', 'C', 'KOSPI', 1, '2026-05-06')"
        )
        conn.execute(
            "INSERT INTO analysis_universe (code, market_cap, adv_20d, rank, added_at) VALUES "
            "('005930', 500000000000, 1000000000, 1, '2026-05-06T00:00:00'), "
            "('035720', 300000000000, 800000000, 2, '2026-05-06T00:00:00'), "
            "('000660', 200000000000, 500000000, 3, '2026-05-06T00:00:00')"
        )
        conn.commit()
        codes = fetch_ohlcv.list_universe_codes(conn)
    finally:
        conn.close()
    assert codes == ["005930", "035720", "000660"]


def test_list_universe_codes_returns_empty_when_table_empty(temp_db):
    conn = get_connection()
    try:
        codes = fetch_ohlcv.list_universe_codes(conn)
    finally:
        conn.close()
    assert codes == []


# ============================================================
# collect_all.step_ohlcv 의 subprocess args
# ============================================================

def test_step_ohlcv_daily_appends_universe_only_flag(monkeypatch):
    captured: list[list[str]] = []

    def fake_subprocess(module, args):
        captured.append(args)
        return True, "ok"

    monkeypatch.setattr(collect_all, "_run_subprocess", fake_subprocess)
    r = collect_all.step_ohlcv(
        first_time=False, years=3, end="2026-05-06", universe_only=True,
    )
    assert r.ok
    assert captured[0] == ["--incremental", "--universe-only"]
    assert "universe" in r.label


def test_step_ohlcv_daily_without_universe_only_keeps_full_scope(monkeypatch):
    captured: list[list[str]] = []
    monkeypatch.setattr(
        collect_all, "_run_subprocess",
        lambda m, a: (captured.append(a), (True, ""))[1],
    )
    collect_all.step_ohlcv(first_time=False, years=3, universe_only=False)
    assert captured[0] == ["--incremental"]


def test_step_ohlcv_first_time_ignores_universe_only(monkeypatch):
    """백필 모드는 universe 가 아직 없을 수 있음 — flag 무시."""
    captured: list[list[str]] = []
    monkeypatch.setattr(
        collect_all, "_run_subprocess",
        lambda m, a: (captured.append(a), (True, ""))[1],
    )
    collect_all.step_ohlcv(
        first_time=True, years=3, end="2026-05-06", universe_only=True,
    )
    assert "--universe-only" not in captured[0]
    assert captured[0][0] == "--backfill"


# ============================================================
# run_pipeline 이 daily 일 때 자동으로 universe_only=True 전달
# ============================================================

def test_run_pipeline_daily_invokes_step_ohlcv_with_universe_only(monkeypatch):
    captured: dict = {}

    def fake_step_ohlcv(first_time, years, end=None, universe_only=False):
        captured["first_time"] = first_time
        captured["universe_only"] = universe_only
        return collect_all.StepResult("일봉 (증분 universe)", True, 0.1, "ok")

    # 다른 단계 모두 빠르게 통과
    def fake_step(*a, **kw):
        return collect_all.StepResult("step", True, 0.0, "")

    monkeypatch.setattr(collect_all, "step_ohlcv", fake_step_ohlcv)
    monkeypatch.setattr(collect_all, "step_instruments", fake_step)
    monkeypatch.setattr(collect_all, "step_fundamentals", fake_step)
    monkeypatch.setattr(collect_all, "step_investor_flow", fake_step)
    monkeypatch.setattr(collect_all, "step_rebuild_universe", fake_step)
    monkeypatch.setattr(collect_all, "_detect_latest_krx_date", lambda: "2026-05-06")
    monkeypatch.setattr(collect_all, "init_schema", lambda: None)

    collect_all.run_pipeline(
        first_time=False, years=3, codes_limit=None,
        skip_per_code=True, per_code_days=1, continue_on_error=True,
    )
    assert captured["first_time"] is False
    assert captured["universe_only"] is True

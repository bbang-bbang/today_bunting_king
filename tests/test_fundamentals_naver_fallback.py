"""재무 스냅샷 naver fallback + universe-only 테스트.

5/6 옵션 D: KRX 인증 환경 변수 누락 환경에서 fetch_fundamentals (pykrx 의존) 가
실패하면 fetch_fundamentals_naver (무인증 scraping) 로 자동 fallback. universe_only
모드는 analysis_universe 500 종목만 처리해 시간 단축.
"""
from __future__ import annotations

import pytest

from src.crawlers import collect_all, fetch_fundamentals_naver
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
# fetch_fundamentals_naver._list_target_codes
# ============================================================

def test_list_target_codes_universe_only_returns_universe_in_rank_order(temp_db):
    conn = get_connection()
    try:
        for code in ("005930", "000660", "035720"):
            conn.execute(
                "INSERT INTO instruments (code, name, market, is_tradable, updated_at) "
                "VALUES (?, ?, 'KOSPI', 1, '2026-05-06')",
                (code, code),
            )
        conn.execute(
            "INSERT INTO analysis_universe (code, market_cap, adv_20d, rank, added_at) VALUES "
            "('035720', 100, 100, 3, 't'), "
            "('005930', 100, 100, 1, 't'), "
            "('000660', 100, 100, 2, 't')"
        )
        conn.commit()
    finally:
        conn.close()
    codes = fetch_fundamentals_naver._list_target_codes(universe_only=True)
    assert codes == ["005930", "000660", "035720"]


def test_list_target_codes_universe_only_falls_back_when_universe_empty(temp_db):
    """universe 비어있으면 instruments 전체로 fallback (안전)."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO instruments (code, name, market, is_tradable, updated_at) "
            "VALUES ('005930', 'A', 'KOSPI', 1, 't')"
        )
        conn.commit()
    finally:
        conn.close()
    codes = fetch_fundamentals_naver._list_target_codes(universe_only=True)
    assert codes == ["005930"]


def test_list_target_codes_default_returns_all_tradable_instruments(temp_db):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO instruments (code, name, market, is_tradable, updated_at) VALUES "
            "('005930', 'A', 'KOSPI', 1, 't'), "
            "('000660', 'B', 'KOSPI', 1, 't'), "
            "('999999', 'C', 'KOSPI', 0, 't')"   # 거래정지
        )
        conn.commit()
    finally:
        conn.close()
    codes = fetch_fundamentals_naver._list_target_codes()
    assert codes == ["000660", "005930"]   # 거래정지 999999 제외


# ============================================================
# collect_all.step_fundamentals fallback
# ============================================================

def test_step_fundamentals_uses_pykrx_when_succeeds(monkeypatch):
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        collect_all, "_run_subprocess",
        lambda m, a: (captured.append((m, a)), (True, "ok"))[1],
    )
    r = collect_all.step_fundamentals()
    assert r.ok
    assert "pykrx" in r.label
    assert len(captured) == 1
    assert captured[0][0] == "src.crawlers.fetch_fundamentals"


def test_step_fundamentals_fallbacks_to_naver_on_pykrx_failure(monkeypatch):
    """pykrx 실패 → naver fallback subprocess 호출."""
    calls: list[tuple[str, list[str]]] = []

    def fake(module, args):
        calls.append((module, args))
        if module == "src.crawlers.fetch_fundamentals":
            return False, "exit_code=1"   # pykrx 실패
        return True, "naver ok"           # naver 성공

    monkeypatch.setattr(collect_all, "_run_subprocess", fake)
    r = collect_all.step_fundamentals(universe_only=True)
    assert r.ok
    assert "naver" in r.label
    assert "universe" in r.label
    assert len(calls) == 2
    assert calls[0][0] == "src.crawlers.fetch_fundamentals"
    assert calls[1][0] == "src.crawlers.fetch_fundamentals_naver"
    assert "--universe-only" in calls[1][1]


def test_step_fundamentals_naver_fallback_without_universe_only(monkeypatch):
    """first_time(백필) 처럼 universe 가 없을 때는 --universe-only 미지정."""
    calls: list[tuple[str, list[str]]] = []

    def fake(module, args):
        calls.append((module, args))
        return (False, "fail") if "fetch_fundamentals" == module.split(".")[-1] else (True, "ok")

    monkeypatch.setattr(collect_all, "_run_subprocess", fake)
    r = collect_all.step_fundamentals(universe_only=False)
    assert "naver" in r.label
    assert "universe" not in r.label
    assert "--universe-only" not in calls[1][1]


# ============================================================
# run_pipeline 이 daily 모드에서 fundamentals 에 universe_only=True 전달
# ============================================================

def test_run_pipeline_daily_passes_universe_only_to_fundamentals(monkeypatch):
    captured: dict = {}

    def fake_step_fund(as_of=None, universe_only=False):
        captured["universe_only"] = universe_only
        return collect_all.StepResult("재무", True, 0.1, "ok")

    fake_ok = lambda *a, **kw: collect_all.StepResult("step", True, 0.0, "")
    monkeypatch.setattr(collect_all, "step_fundamentals", fake_step_fund)
    monkeypatch.setattr(collect_all, "step_instruments", fake_ok)
    monkeypatch.setattr(collect_all, "step_ohlcv", fake_ok)
    monkeypatch.setattr(collect_all, "step_investor_flow", fake_ok)
    monkeypatch.setattr(collect_all, "step_rebuild_universe", fake_ok)
    monkeypatch.setattr(collect_all, "_detect_latest_krx_date", lambda: "2026-05-06")
    monkeypatch.setattr(collect_all, "init_schema", lambda: None)

    collect_all.run_pipeline(
        first_time=False, years=3, codes_limit=None,
        skip_per_code=True, per_code_days=1, continue_on_error=True,
    )
    assert captured["universe_only"] is True

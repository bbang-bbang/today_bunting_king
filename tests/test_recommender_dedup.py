"""recommend() 중복 입력 종목 방어 회귀 테스트.

2026-05 사고: fundamentals_snapshot 시계열(종목당 ~14 snapshot_date)을 DISTINCT 없이
조회 → codes 에 같은 종목이 14번 주입 → top_n 이 동일 종목 복제본으로 채워짐
(추천 다양성 10→1 붕괴, 사용자가 1종목만 받음). fix: 입력 codes dedup + 시총필터 SELECT DISTINCT.
"""
from __future__ import annotations

import importlib

import pandas as pd
import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


def _count_load_calls(monkeypatch):
    """load_ohlcv 를 빈 DataFrame 반환 + 호출 종목 기록으로 대체."""
    from src.ensemble import recommender
    seen: list[str] = []

    def _fake(code, end=None):
        seen.append(code)
        return pd.DataFrame()   # empty → len<60 으로 스킵되지만 호출은 카운트됨

    monkeypatch.setattr(recommender, "load_ohlcv", _fake)
    return seen


def test_recommend_dedupes_duplicate_input_codes(monkeypatch):
    """중복 입력 codes → 각 종목 1회만 평가 (min_market_cap=0 으로 시총필터 우회)."""
    from src.ensemble import recommender
    seen = _count_load_calls(monkeypatch)

    recommender.recommend(
        codes=["005930", "005930", "005930", "000660", "000660"],
        active_seed_krw=1_000_000, mode="bunt",
        top_n=10, min_score=0.0, min_market_cap=0,
    )
    assert sorted(seen) == ["000660", "005930"], f"중복 평가됨: {seen}"


def test_recommend_market_cap_filter_is_distinct(monkeypatch):
    """fundamentals_snapshot 에 같은 종목 다중 snapshot 이 있어도 1회만 평가 (DISTINCT)."""
    from src.db.connection import get_connection
    from src.ensemble import recommender

    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code,name,market,updated_at) "
            "VALUES ('005930','삼성전자','KOSPI','2026-05-27')"
        )
        # 같은 종목 3개 snapshot_date (시계열) — 사고 재현
        for d in ("2026-05-25", "2026-05-26", "2026-05-27"):
            conn.execute(
                "INSERT INTO fundamentals_snapshot"
                "(code,snapshot_date,market_cap,per,pbr,roe,debt_ratio,is_warning,is_watch,source)"
                " VALUES ('005930',?,500000000000,1,1,1,1,0,0,'t')",
                (d,),
            )
        conn.commit()
    finally:
        conn.close()

    seen = _count_load_calls(monkeypatch)
    recommender.recommend(
        codes=["005930"], active_seed_krw=1_000_000, mode="bunt",
        top_n=10, min_score=0.0, min_market_cap=100_000_000_000,
    )
    assert seen == ["005930"], f"snapshot 3개로 중복 평가됨: {seen}"

"""임의 종목 조회(#2) 종목 해석기 _resolve_stock 테스트.

6자리 코드 직행 / 이름 정확매칭 / 부분매칭 다수 → 후보목록 / 미존재 처리.
"""
from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema, get_connection
    init_schema()
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT INTO instruments(code,name,market,is_tradable,updated_at) "
            "VALUES(?,?,?,1,'2026-06-11')",
            [
                ("005930", "삼성전자", "KOSPI"),
                ("000660", "SK하이닉스", "KOSPI"),
                ("207940", "삼성바이오로직스", "KOSPI"),
            ],
        )
    finally:
        conn.close()
    yield


def _resolve(q):
    from src.bot import telegram_bot
    return telegram_bot._resolve_stock(q)


def test_resolve_by_exact_code():
    code, name, cands = _resolve("005930")
    assert code == "005930" and name == "삼성전자" and cands == []


def test_resolve_unknown_code_still_tries():
    # instruments 에 없는 6자리도 코드로 간주해 평가 시도(코드 반환, 이름 공백)
    code, name, cands = _resolve("999999")
    assert code == "999999" and name == "" and cands == []


def test_resolve_by_exact_name():
    code, name, cands = _resolve("삼성전자")
    assert code == "005930" and cands == []


def test_resolve_partial_name_multiple_returns_candidates():
    # "삼성" 부분일치 → 삼성전자 + 삼성바이오로직스 → 후보목록(code=None)
    code, name, cands = _resolve("삼성")
    assert code is None
    codes = {c for c, _ in cands}
    assert {"005930", "207940"} <= codes


def test_resolve_not_found():
    code, name, cands = _resolve("없는종목명xyz")
    assert code is None and cands == []

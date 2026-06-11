"""분봉 정리(purge_old_bars) 데이터 계층 테스트.

분봉 expert 는 직전 세션만 읽으므로 보존창 밖 오래된 분봉을 삭제해 테이블을 가볍게 유지.
"""
from __future__ import annotations

import importlib
from datetime import date

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


def _seed(dates: list[str], code: str = "005930"):
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code,name,market,is_tradable,updated_at) "
            "VALUES(?,?,?,1,'2026-06-11')",
            (code, "삼성전자", "KOSPI"),
        )
        rows = [(code, f"{d} 09:00", 100, 110, 90, 105, 1000) for d in dates]
        conn.executemany(
            "INSERT INTO ohlcv_minute(code,datetime,open,high,low,close,volume) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
    finally:
        conn.close()


def _count():
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        return conn.execute("SELECT COUNT(*) FROM ohlcv_minute").fetchone()[0]
    finally:
        conn.close()


def test_purge_deletes_older_than_window():
    from src.crawlers import fetch_ohlcv_minute as f
    from src.db.connection import get_connection
    # as_of=2026-06-11, 보존 10일 → 2026-06-01 이전 삭제
    _seed(["2026-05-20", "2026-05-27", "2026-06-01", "2026-06-05", "2026-06-10"])
    assert _count() == 5
    conn = get_connection()
    try:
        deleted = f.purge_old_bars(conn, date(2026, 6, 11), keep_days=10)
    finally:
        conn.close()
    # cutoff = 2026-06-01 00:00 → 5/20, 5/27 삭제 (2건), 6/01 이후 보존
    assert deleted == 2
    assert _count() == 3


def test_purge_keep_days_zero_is_noop():
    from src.crawlers import fetch_ohlcv_minute as f
    from src.db.connection import get_connection
    _seed(["2026-01-01", "2026-06-10"])
    conn = get_connection()
    try:
        deleted = f.purge_old_bars(conn, date(2026, 6, 11), keep_days=0)
    finally:
        conn.close()
    assert deleted == 0
    assert _count() == 2


def test_purge_keeps_recent_session():
    from src.crawlers import fetch_ohlcv_minute as f
    from src.db.connection import get_connection
    # 직전 세션(어제)은 반드시 보존되어야 함
    _seed(["2026-06-10"])
    conn = get_connection()
    try:
        f.purge_old_bars(conn, date(2026, 6, 11), keep_days=10)
    finally:
        conn.close()
    assert _count() == 1


def test_purge_wrapper_manages_connection():
    """purge() 래퍼가 커넥션 열고닫으며 동작."""
    from src.crawlers import fetch_ohlcv_minute as f
    _seed(["2026-05-01", "2026-06-10"])
    deleted = f.purge(date(2026, 6, 11), keep_days=10)
    assert deleted == 1
    assert _count() == 1

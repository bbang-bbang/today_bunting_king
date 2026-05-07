"""analysis_universe 빌더 검증."""
from __future__ import annotations

from datetime import date, timedelta
import pytest

from src.db.connection import get_connection, init_schema
from src.universe.builder import (
    get_universe_codes,
    rebuild_universe,
    universe_size,
)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.setenv("SEED_KRW", "1000000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "dummy")
    monkeypatch.setenv("TELEGRAM_INVITE_CODE", "dummy")
    # src.config 를 이미 import 한 테스트가 있으면 reload 필요.
    import importlib
    from src import config, db as db_mod
    importlib.reload(config)
    importlib.reload(db_mod.connection)
    init_schema()
    yield
    # cleanup: connection close 는 각 호출에서 처리됨


def _seed(code: str, market_cap: int, close: int, volume: int, days: int):
    """한 종목에 대해 days 일치의 일봉 + 재무 스냅샷을 생성."""
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO instruments (code, name, market, is_tradable, updated_at) "
            "VALUES (?, ?, 'KOSPI', 1, '2026-04-22')",
            (code, f"NAME_{code}"),
        )
        base = date(2026, 4, 22)
        for i in range(days):
            d = (base - timedelta(days=days - 1 - i)).isoformat()
            conn.execute(
                "INSERT OR REPLACE INTO ohlcv_daily "
                "(code, date, open, high, low, close, volume) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (code, d, close, close, close, close, volume),
            )
        conn.execute(
            "INSERT OR REPLACE INTO fundamentals_snapshot "
            "(code, snapshot_date, market_cap, per, pbr, roe) "
            "VALUES (?, '2026-04-22', ?, 10, 1, 5)",
            (code, market_cap),
        )
        conn.commit()
    finally:
        conn.close()


def test_rebuild_basic(fresh_db):
    """시총 내림차순으로 top_n 저장, ADV/days 필터 통과."""
    # ADV = close*volume = 1000원 * 2,000,000 = 20억 (>10억 pass)
    _seed("A", market_cap=100_000_000_000_000, close=1000, volume=2_000_000, days=80)
    _seed("B", market_cap=50_000_000_000_000,  close=1000, volume=2_000_000, days=80)
    _seed("C", market_cap=10_000_000_000_000,  close=1000, volume=2_000_000, days=80)

    n = rebuild_universe(as_of=date(2026, 4, 22), top_n=10)
    assert n == 3
    assert get_universe_codes("rank") == ["A", "B", "C"]


def test_filter_min_days(fresh_db):
    """60일 미만 종목은 제외."""
    _seed("A", market_cap=1_000_000_000_000, close=1000, volume=2_000_000, days=80)
    _seed("B", market_cap=900_000_000_000,   close=1000, volume=2_000_000, days=40)  # 40 only

    n = rebuild_universe(as_of=date(2026, 4, 22))
    assert n == 1
    assert get_universe_codes() == ["A"]


def test_filter_min_adv(fresh_db):
    """20일 평균 거래대금 < 10억 제외."""
    _seed("A", market_cap=1_000_000_000_000, close=1000, volume=2_000_000, days=80)   # ADV=20억 pass
    _seed("B", market_cap=900_000_000_000,   close=1000, volume=500_000,   days=80)   # ADV=5억 fail

    n = rebuild_universe(as_of=date(2026, 4, 22))
    assert n == 1
    assert get_universe_codes() == ["A"]


def test_top_n_cap(fresh_db):
    """top_n 보다 많은 후보가 있어도 top_n 만 저장."""
    caps = [5_000, 4_000, 3_000, 2_000, 1_000]   # 단위: 억
    for i, cap in enumerate(caps):
        _seed(
            f"C{i}", market_cap=cap * 100_000_000,
            close=1000, volume=2_000_000, days=80,
        )
    n = rebuild_universe(as_of=date(2026, 4, 22), top_n=3)
    assert n == 3
    assert get_universe_codes("rank") == ["C0", "C1", "C2"]


def test_atomic_replace(fresh_db):
    """재빌드는 기존 레코드 전체 교체."""
    _seed("A", market_cap=1_000_000_000_000, close=1000, volume=2_000_000, days=80)
    rebuild_universe(as_of=date(2026, 4, 22))
    assert universe_size() == 1

    # A 제거 + B 추가 (B가 새로 들어와야)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM fundamentals_snapshot WHERE code='A'")
        conn.commit()
    finally:
        conn.close()
    _seed("B", market_cap=500_000_000_000, close=1000, volume=2_000_000, days=80)
    rebuild_universe(as_of=date(2026, 4, 22))
    assert get_universe_codes() == ["B"]


def test_empty_when_no_data(fresh_db):
    """데이터 없으면 0 반환, 에러 없음."""
    n = rebuild_universe(as_of=date(2026, 4, 22))
    assert n == 0
    assert universe_size() == 0

"""섀도우 캘리브레이션 표시 테스트.

앙상블 점수는 미래수익에 캘리브레이션된 적 없음 → 같은 점수대의 과거 실측을 카드에 표시(픽 변경 X).
measurement.current_regime / calibration_index 및 scheduler._calib_shadow_line 포맷 검증.
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
        conn.execute(
            "INSERT INTO instruments(code,name,market,is_tradable,updated_at) "
            "VALUES('005930','삼성전자','KOSPI',1,'2026-06-11')"
        )
        conn.execute(
            "INSERT INTO regime_daily(date,breadth_pct,n_codes,regime,computed_at) "
            "VALUES('2026-06-10',22.0,400,'down','2026-06-10T08:10:00')"
        )
        # down 레짐 60-63 버킷: 4건(고유 2) — 평균 -8%, 1승 → 승률 25%
        rows = [
            ("2026-06-01", "005930", "bunt", "60-63", "down", -10.0, -5.0),
            ("2026-06-02", "005930", "bunt", "60-63", "down", -12.0, -6.0),
            ("2026-06-03", "005930", "bunt", "60-63", "down", -8.0, -4.0),
            ("2026-06-04", "005930", "bunt", "60-63", "down", +2.0, +1.0),
        ]
        conn.executemany(
            "INSERT INTO signal_outcomes(session_date,code,strategy_mode,score_bucket,"
            "regime_at_entry,fwd_ret_5d,excess_ret_5d,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'t','t')",
            rows,
        )
    finally:
        conn.close()
    yield


def test_current_regime_reads_latest():
    from src.db.connection import get_connection
    from src.services import measurement as m
    conn = get_connection()
    try:
        assert m.current_regime(conn) == "down"
    finally:
        conn.close()


def test_calibration_index_buckets():
    from src.db.connection import get_connection
    from src.services import measurement as m
    conn = get_connection()
    try:
        idx = m.calibration_index(conn, "down", min_n=3)
    finally:
        conn.close()
    assert "60-63" in idx
    row = idx["60-63"]
    assert row["n"] == 4 and row["uniq_codes"] == 1
    assert row["win_pct"] == 25.0
    assert row["avg_ret_5d"] == pytest.approx(-7.0, abs=0.01)


def test_calibration_index_excludes_thin_buckets():
    from src.db.connection import get_connection
    from src.services import measurement as m
    conn = get_connection()
    try:
        idx = m.calibration_index(conn, "down", min_n=10)  # 4건 < 10 → 제외
    finally:
        conn.close()
    assert "60-63" not in idx


def test_shadow_line_format_and_repeat_flag():
    from src.bot import scheduler
    idx = {
        "60-63": {
            "bucket": "60-63", "regime": "down", "n": 4, "uniq_codes": 1,
            "avg_ret_5d": -7.0, "win_pct": 25.0, "median_ret_5d": -9.0,
            "avg_excess_5d": -3.5,
        }
    }
    line = scheduler._calib_shadow_line(61.0, idx)
    assert "급락장" in line and "60-63" in line
    assert "-7.0%" in line and "25%" in line
    assert "⚠반복표본" in line  # uniq(1)*2 < n(4)


def test_shadow_line_empty_when_no_bucket():
    from src.bot import scheduler
    assert scheduler._calib_shadow_line(99.0, {"60-63": {}}) == ""
    assert scheduler._calib_shadow_line(61.0, {}) == ""

"""recommendation_service 테스트 — rec_id 생성·저장·조회·행위 로그."""
from __future__ import annotations

import importlib
from datetime import date

import pytest


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test_bunting.db"
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("SEED_KRW", "100000")
    monkeypatch.setenv("TRADE_MODE", "paper")
    import src.config
    importlib.reload(src.config)
    import src.db.connection
    importlib.reload(src.db.connection)
    from src.db.connection import init_schema
    init_schema()
    yield


def _register(chat_id: int) -> None:
    from src.services import user_service
    user_service.register_user(chat_id)


def _seed_instrument(code: str, name: str, market: str = "KOSPI") -> None:
    from src.db.connection import get_connection
    conn = get_connection()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO instruments(code, name, market, updated_at) "
            "VALUES (?, ?, ?, '2026-04-16')",
            (code, name, market),
        )
    finally:
        conn.close()


# ============================================================
# create_recommendation
# ============================================================

def test_rec_id_format_and_sequence():
    from src.services import recommendation_service as rs
    _register(100)
    rec1 = rs.create_recommendation(
        chat_id=100, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    rec2 = rs.create_recommendation(
        chat_id=100, market="KR", code="000660", strategy_mode="bunt",
        entry_price=130_000, target_price=133_900, stop_price=127_400,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    assert rec1 == "KR-20260416-01"
    assert rec2 == "KR-20260416-02"


def test_rec_id_sequence_resets_per_date():
    from src.services import recommendation_service as rs
    _register(101)
    rec1 = rs.create_recommendation(
        chat_id=101, market="KR", code="005930", strategy_mode="bunt",
        entry_price=70_000, target_price=72_100, stop_price=68_600,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    rec2 = rs.create_recommendation(
        chat_id=101, market="KR", code="005930", strategy_mode="bunt",
        entry_price=71_000, target_price=73_130, stop_price=69_580,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 17),
    )
    assert rec1 == "KR-20260416-01"
    assert rec2 == "KR-20260417-01"


def test_rec_id_sequence_separated_per_market():
    from src.services import recommendation_service as rs
    _register(102)
    kr = rs.create_recommendation(
        chat_id=102, market="KR", code="005930", strategy_mode="bunt",
        entry_price=70_000, target_price=72_100, stop_price=68_600,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    us = rs.create_recommendation(
        chat_id=102, market="US", code="AAPL", strategy_mode="bunt",
        entry_price=200, target_price=206, stop_price=196,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    assert kr == "KR-20260416-01"
    assert us == "US-20260416-01"


def test_invalid_market_rejected():
    from src.services import recommendation_service as rs
    _register(103)
    with pytest.raises(ValueError, match="market"):
        rs.create_recommendation(
            chat_id=103, market="JP", code="x", strategy_mode="bunt",
            entry_price=1, target_price=2, stop_price=0,
            expected_return_pct=1.0, reason_summary="t",
        )


def test_invalid_mode_rejected():
    from src.services import recommendation_service as rs
    _register(104)
    with pytest.raises(ValueError, match="strategy_mode"):
        rs.create_recommendation(
            chat_id=104, market="KR", code="x", strategy_mode="hyper",
            entry_price=1, target_price=2, stop_price=0,
            expected_return_pct=1.0, reason_summary="t",
        )


def test_name_resolved_from_instruments_when_omitted():
    from src.services import recommendation_service as rs
    _register(105)
    _seed_instrument("005930", "삼성전자")
    rec_id = rs.create_recommendation(
        chat_id=105, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    got = rs.get_recommendation(rec_id)
    assert got["name"] == "삼성전자"


def test_name_falls_back_to_code_when_instrument_missing():
    from src.services import recommendation_service as rs
    _register(106)
    rec_id = rs.create_recommendation(
        chat_id=106, market="KR", code="999999", strategy_mode="bunt",
        entry_price=10_000, target_price=10_300, stop_price=9_800,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )
    got = rs.get_recommendation(rec_id)
    assert got["name"] == "999999"


def test_explicit_name_overrides_lookup():
    from src.services import recommendation_service as rs
    _register(107)
    _seed_instrument("005930", "삼성전자")
    rec_id = rs.create_recommendation(
        chat_id=107, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        name="직접지정명",
        session_date=date(2026, 4, 16),
    )
    assert rs.get_recommendation(rec_id)["name"] == "직접지정명"


def test_reason_json_persisted():
    from src.services import recommendation_service as rs
    import json
    _register(108)
    rec_id = rs.create_recommendation(
        chat_id=108, market="KR", code="005930", strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        reason_json={"expert_scores": {"technical": 65.5}},
        session_date=date(2026, 4, 16),
    )
    got = rs.get_recommendation(rec_id)
    assert json.loads(got["reason_json"])["expert_scores"]["technical"] == 65.5


def test_get_recommendation_missing_returns_none():
    from src.services import recommendation_service as rs
    assert rs.get_recommendation("KR-19990101-99") is None


# ============================================================
# insert_action / update_action_reason
# ============================================================

def _seed_rec(chat_id: int, code: str = "005930") -> str:
    from src.services import recommendation_service as rs
    return rs.create_recommendation(
        chat_id=chat_id, market="KR", code=code, strategy_mode="bunt",
        entry_price=72_000, target_price=74_160, stop_price=70_560,
        expected_return_pct=3.0, reason_summary="t",
        session_date=date(2026, 4, 16),
    )


def test_insert_bought_pending_and_update():
    from src.services import recommendation_service as rs
    _register(200)
    rec_id = _seed_rec(200)
    action_id = rs.insert_action(
        rec_id=rec_id, chat_id=200, action_type="bought",
        reason_tag="pending", price=72_000, quantity=13,
    )
    assert action_id > 0
    assert rs.find_pending_bought_action(rec_id) == action_id

    assert rs.update_action_reason(action_id, "trust_ensemble") is True
    # pending 해제 후 find 는 None
    assert rs.find_pending_bought_action(rec_id) is None


def test_insert_skipped_with_tag():
    from src.services import recommendation_service as rs
    _register(201)
    rec_id = _seed_rec(201)
    action_id = rs.insert_action(
        rec_id=rec_id, chat_id=201, action_type="skipped",
        reason_tag="missed_timing",
    )
    assert action_id > 0


def test_insert_rejects_tag_not_allowed_for_action():
    from src.services import recommendation_service as rs
    _register(202)
    rec_id = _seed_rec(202)
    with pytest.raises(ValueError, match="not allowed"):
        rs.insert_action(
            rec_id=rec_id, chat_id=202, action_type="skipped",
            reason_tag="trust_ensemble",
        )
    with pytest.raises(ValueError, match="not allowed"):
        rs.insert_action(
            rec_id=rec_id, chat_id=202, action_type="sold",
            reason_tag="no_cash",
        )


def test_insert_rejects_unknown_action_type():
    from src.services import recommendation_service as rs
    _register(203)
    rec_id = _seed_rec(203)
    with pytest.raises(ValueError, match="unknown action_type"):
        rs.insert_action(
            rec_id=rec_id, chat_id=203, action_type="hesitated",
            reason_tag="other",
        )


def test_update_rejects_cross_type_tag():
    from src.services import recommendation_service as rs
    _register(204)
    rec_id = _seed_rec(204)
    action_id = rs.insert_action(
        rec_id=rec_id, chat_id=204, action_type="bought",
        reason_tag="pending", price=72_000, quantity=1,
    )
    with pytest.raises(ValueError, match="not allowed"):
        rs.update_action_reason(action_id, "missed_timing")  # skipped 태그


def test_update_nonexistent_action_returns_false():
    from src.services import recommendation_service as rs
    assert rs.update_action_reason(999_999, "trust_ensemble") is False


def test_update_with_reason_text():
    from src.services import recommendation_service as rs
    from src.db.connection import get_connection
    _register(205)
    rec_id = _seed_rec(205)
    action_id = rs.insert_action(
        rec_id=rec_id, chat_id=205, action_type="bought",
        reason_tag="pending", price=72_000, quantity=1,
    )
    rs.update_action_reason(action_id, "intuition", reason_text="차트 깔끔해서")
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT reason_tag, reason_text FROM recommendation_actions WHERE id=?",
            (action_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "intuition"
    assert row[1] == "차트 깔끔해서"


def test_find_pending_returns_most_recent():
    """같은 rec_id 에 pending 이 여러 개면 가장 최근 id 반환."""
    from src.services import recommendation_service as rs
    _register(206)
    rec_id = _seed_rec(206)
    a1 = rs.insert_action(
        rec_id=rec_id, chat_id=206, action_type="bought",
        reason_tag="pending", price=72_000, quantity=1,
    )
    a2 = rs.insert_action(
        rec_id=rec_id, chat_id=206, action_type="bought",
        reason_tag="pending", price=72_000, quantity=1,
    )
    assert rs.find_pending_bought_action(rec_id) == max(a1, a2)

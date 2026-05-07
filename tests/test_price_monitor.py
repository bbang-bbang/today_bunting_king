"""PriceMonitor 테스트 — 가짜 KIS 소스로 TP/SL 감지 검증."""
from __future__ import annotations

from datetime import datetime

import pytest

from src.adapters.market_data_base import CurrentPrice
from src.services.portfolio_service import OpenPositionDTO
from src.services.price_monitor import AlertType, PriceMonitor


# ============================================================
# Fakes
# ============================================================

class _FakeKIS:
    """가짜 KIS 소스. 종목별 현재가를 딕셔너리로 주입."""

    def __init__(self, price_map: dict[str, CurrentPrice]) -> None:
        self._map = price_map

    def fetch_current_prices(self, codes: list[str]) -> dict[str, CurrentPrice]:
        return {c: self._map[c] for c in codes if c in self._map}


def _cp(code, price, high, low, prev_close=100_000):
    return CurrentPrice(
        code=code, price=price, open=prev_close, high=high, low=low,
        prev_close=prev_close, volume=10000, change_pct=0.0,
        fetched_at=datetime.now(),
    )


def _pos(code, buy_price, target_price, stop_price, position_id=1):
    return OpenPositionDTO(
        position_id=position_id, code=code, buy_price=buy_price,
        quantity=10, target_price=target_price, stop_price=stop_price,
        strategy_mode="bunt", opened_at="2026-04-14T09:00:00",
    )


# ============================================================
# 테스트
# ============================================================

def test_tp_hit(monkeypatch):
    """고가가 목표가 도달 → TP_HIT 알림."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=106_500, high=107_500, low=100_000)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.TP_HIT
    assert alerts[0].is_exit_signal


def test_sl_hit(monkeypatch):
    """저가가 손절가 도달 → SL_HIT 알림."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=96_500, high=100_500, low=95_500)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.SL_HIT
    assert alerts[0].is_exit_signal


def test_near_tp(monkeypatch):
    """목표가 근접 → NEAR_TP 경고."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    # 현재가 106,000 → 목표까지 1,000원 (목표-매수=7,000원의 ~14%)
    cp = _cp("005930", price=106_000, high=106_200, low=100_500)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.NEAR_TP
    assert not alerts[0].is_exit_signal


def test_near_sl(monkeypatch):
    """손절가 근접 → NEAR_SL 경고."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    # 현재가 97,000 → 손절까지 1,000원 (매수-손절=4,000원의 25%)
    cp = _cp("005930", price=97_000, high=100_500, low=96_800)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.NEAR_SL


def test_no_alert_when_normal(monkeypatch):
    """정상 범위 → 알림 없음."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=102_000, high=103_000, low=100_000)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 0


def test_near_alert_dedupes_across_passes(monkeypatch):
    """근접 알림(NEAR_TP)은 같은 포지션+타입 → sticky dedupe (한 번만 알림)."""
    # 목표 107_000, 현재 105_000 → tp_dist = (107k-105k)/(107k-100k) ≈ 0.286 < 0.3 → NEAR_TP
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=105_000, high=106_000, low=100_500)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts1 = monitor.check_positions(chat_id=123)
    alerts2 = monitor.check_positions(chat_id=123)
    assert len(alerts1) == 1
    assert alerts1[0].alert_type == AlertType.NEAR_TP
    assert len(alerts2) == 0  # 근접 알림 중복 방지


def test_exit_signal_retries_when_position_still_open(monkeypatch):
    """exit signal(TP_HIT/SL_HIT)은 매도 실패 대비 — 포지션이 계속 open이면 다음 사이클에도 재알림."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=106_500, high=107_500, low=100_000)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts1 = monitor.check_positions(chat_id=123)
    alerts2 = monitor.check_positions(chat_id=123)
    assert len(alerts1) == 1 and alerts1[0].alert_type == AlertType.TP_HIT
    # 포지션이 여전히 open 이면(매도 실패 시나리오) 다음 사이클에도 재알림되어야 retry 가능
    assert len(alerts2) == 1 and alerts2[0].alert_type == AlertType.TP_HIT


def test_same_code_multi_lot_exits_one_per_pass(monkeypatch):
    """같은 code 다중 lot 이 동시에 SL hit → 한 사이클에 1건만 알림 (race + 노이즈 방지).

    009540 SL_HIT 3회 사고 (2026-04-28 09:29) 회귀 테스트.
    """
    pos1 = _pos("009540", buy_price=100_000, target_price=107_000, stop_price=96_000, position_id=1)
    pos2 = _pos("009540", buy_price=100_000, target_price=107_000, stop_price=96_000, position_id=2)
    pos3 = _pos("009540", buy_price=100_000, target_price=107_000, stop_price=96_000, position_id=3)
    cp = _cp("009540", price=95_500, high=99_000, low=95_000)  # SL hit

    kis = _FakeKIS({"009540": cp})
    monitor = PriceMonitor(kis=kis)
    positions = [pos1, pos2, pos3]
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: positions,
    )

    alerts1 = monitor.check_positions(chat_id=123)
    assert len(alerts1) == 1
    assert alerts1[0].alert_type == AlertType.SL_HIT

    # 첫 lot 매도 완료 시뮬레이션 — 두번째 사이클에 두번째 lot 알림
    positions.pop(0)
    alerts2 = monitor.check_positions(chat_id=123)
    assert len(alerts2) == 1
    assert alerts2[0].position_id == 2

    # 두번째 lot 매도 완료 — 세번째 사이클에 마지막 lot 알림
    positions.pop(0)
    alerts3 = monitor.check_positions(chat_id=123)
    assert len(alerts3) == 1
    assert alerts3[0].position_id == 3


def test_reset_clears_history(monkeypatch):
    """reset_alerts 후 NEAR_* 알림 다시 발생."""
    # NEAR_TP 시나리오 (105_000 → tp_dist ≈ 0.286)
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=105_000, high=106_000, low=100_500)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts1 = monitor.check_positions(chat_id=123)
    monitor.reset_alerts()
    alerts2 = monitor.check_positions(chat_id=123)
    assert len(alerts1) == 1 and alerts1[0].alert_type == AlertType.NEAR_TP
    assert len(alerts2) == 1 and alerts2[0].alert_type == AlertType.NEAR_TP


def test_multiple_positions(monkeypatch):
    """2개 포지션 각각 다른 알림."""
    pos_a = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000, position_id=1)
    pos_b = _pos("000660", buy_price=200_000, target_price=214_000, stop_price=192_000, position_id=2)

    cp_a = _cp("005930", price=106_500, high=107_500, low=100_000)  # TP hit
    cp_b = _cp("000660", price=193_000, high=201_000, low=191_500)  # SL hit

    kis = _FakeKIS({"005930": cp_a, "000660": cp_b})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos_a, pos_b],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 2
    types = {a.alert_type for a in alerts}
    assert AlertType.TP_HIT in types
    assert AlertType.SL_HIT in types


def test_format_message(monkeypatch):
    """알림 메시지 포맷팅."""
    pos = _pos("005930", buy_price=100_000, target_price=107_000, stop_price=96_000)
    cp = _cp("005930", price=106_500, high=107_500, low=100_000)

    kis = _FakeKIS({"005930": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    msg = alerts[0].format_message()
    assert "005930" in msg
    assert "목표가 도달" in msg
    assert "익절" in msg


def test_grace_period_blocks_pre_buy_low_from_triggering_sl(monkeypatch):
    """매수 직후 grace 기간엔 cp.low/cp.high 무시 — 매수 전 찍힌 저가가 SL 만들지 않음.

    2026-04-28 007340 사고 회귀: 매수 직후 1분만에 SL_HIT 발사로 강제매도된 케이스.
    """
    from datetime import datetime, timedelta
    # 매수 1분 전 (grace 안에 있음)
    now = datetime.now()
    pos = OpenPositionDTO(
        position_id=1, code="007340", buy_price=41_400,
        quantity=60, target_price=44_300, stop_price=39_750,
        strategy_mode="bunt",
        opened_at=(now - timedelta(minutes=1)).isoformat(),
    )
    # 매수 전에 찍힌 저가가 SL 보다 낮음 (당일 저가 39,500), 현재가는 41,350 (정상)
    cp = _cp("007340", price=41_350, high=42_000, low=39_500)

    kis = _FakeKIS({"007340": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 0, f"grace 기간에 SL_HIT 가 발사되면 안 됨 (cp.low 가 매수 전 저가): {alerts}"


def test_grace_period_expires_after_window(monkeypatch):
    """grace 기간 지나면 정상적으로 cp.low/cp.high 기반 exit 판정."""
    from datetime import datetime, timedelta
    now = datetime.now()
    # 매수 11분 전 (grace 10분 지남)
    pos = OpenPositionDTO(
        position_id=1, code="007340", buy_price=41_400,
        quantity=60, target_price=44_300, stop_price=39_750,
        strategy_mode="bunt",
        opened_at=(now - timedelta(minutes=11)).isoformat(),
    )
    cp = _cp("007340", price=41_350, high=42_000, low=39_500)  # low <= SL

    kis = _FakeKIS({"007340": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.SL_HIT


def test_grace_period_still_triggers_on_current_price(monkeypatch):
    """grace 기간이라도 현재가가 SL 이하면 진짜 손절 — 보호하지 않음."""
    from datetime import datetime, timedelta
    now = datetime.now()
    pos = OpenPositionDTO(
        position_id=1, code="007340", buy_price=41_400,
        quantity=60, target_price=44_300, stop_price=39_750,
        strategy_mode="bunt",
        opened_at=(now - timedelta(minutes=1)).isoformat(),  # grace 안
    )
    # 현재가가 SL 이하 (진짜 매수 후 손절 케이스)
    cp = _cp("007340", price=39_700, high=41_400, low=39_700)

    kis = _FakeKIS({"007340": cp})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [pos],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert len(alerts) == 1
    assert alerts[0].alert_type == AlertType.SL_HIT


def test_no_positions_no_alerts(monkeypatch):
    """포지션 없으면 알림 없음."""
    kis = _FakeKIS({})
    monitor = PriceMonitor(kis=kis)
    monkeypatch.setattr(
        "src.services.price_monitor.get_open_positions",
        lambda chat_id: [],
    )

    alerts = monitor.check_positions(chat_id=123)
    assert alerts == []

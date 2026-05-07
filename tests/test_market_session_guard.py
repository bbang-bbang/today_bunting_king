"""scheduler.is_kr_market_session_now / is_kr_trading_day 가드 테스트.

2026-05-04 사고 (월요일 새벽~장전에 알람 폭주) 회귀 방지.
- is_kr_market_open() 이 시간을 안 봐서 새벽에도 True 반환 → 잡 가드 통과 → 알림 발송.
- 함수를 분리해 트레이딩 데이 체크와 세션 체크를 명시적으로 구분.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest


KST = timezone(timedelta(hours=9))


@pytest.fixture
def mock_now(monkeypatch):
    """src.bot.scheduler.datetime.now() 를 고정값으로 monkeypatch."""
    from src.bot import scheduler

    real_datetime = scheduler.datetime

    def make_fixed(target: datetime):
        class FixedDatetime(real_datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return target.replace(tzinfo=None)
                return target.astimezone(tz)

        monkeypatch.setattr(scheduler, "datetime", FixedDatetime)

    return make_fixed


@pytest.fixture(autouse=True)
def mock_trading_day_cache(monkeypatch):
    """pykrx 호출을 피하기 위해 _is_trading_day_cached 를 항상 True 로."""
    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "_is_trading_day_cached", lambda iso: True)


# ============================================================
# is_kr_market_session_now — 시간 가드까지 포함
# ============================================================

@pytest.mark.parametrize(
    "iso, expected",
    [
        # 월요일 2026-05-04 KST
        ("2026-05-04T03:00:00+09:00", False),  # 새벽 3시 — NO
        ("2026-05-04T08:30:00+09:00", False),  # 장전 추천 시간 — NO (세션은 아님)
        ("2026-05-04T08:59:59+09:00", False),  # 장 시작 1초 전 — NO
        ("2026-05-04T09:00:00+09:00", True),   # 장 시작 — YES
        ("2026-05-04T12:30:00+09:00", True),   # 점심 — YES (KRX는 점심 휴장 없음)
        ("2026-05-04T15:30:00+09:00", True),   # 장 마감 — YES (포함)
        ("2026-05-04T15:30:01+09:00", False),  # 장 마감 1초 후 — NO
        ("2026-05-04T16:00:00+09:00", False),  # 장후 — NO
        ("2026-05-04T23:00:00+09:00", False),  # 야간 — NO
    ],
)
def test_market_session_now_weekday(mock_now, iso, expected):
    from src.bot.scheduler import is_kr_market_session_now
    mock_now(datetime.fromisoformat(iso))
    assert is_kr_market_session_now() is expected


def test_market_session_now_weekend(mock_now):
    """토요일 09:30 KST — 거래일 체크 통과해도 weekday 가드로 False."""
    from src.bot.scheduler import is_kr_market_session_now
    mock_now(datetime.fromisoformat("2026-05-09T09:30:00+09:00"))  # 토요일
    assert is_kr_market_session_now() is False


def test_market_session_now_holiday(mock_now, monkeypatch):
    """평일이지만 KRX 휴장일 (예: 어린이날) — False."""
    from src.bot import scheduler
    monkeypatch.setattr(scheduler, "_is_trading_day_cached", lambda iso: False)
    mock_now(datetime.fromisoformat("2026-05-05T10:00:00+09:00"))  # 화요일 어린이날
    assert scheduler.is_kr_market_session_now() is False


# ============================================================
# is_kr_trading_day — 시간 무시, 거래일만
# ============================================================

def test_trading_day_returns_true_pre_market(mock_now):
    """장 시작 전이라도 거래일이면 True (08:30 잡 등에서 사용)."""
    from src.bot.scheduler import is_kr_trading_day
    mock_now(datetime.fromisoformat("2026-05-04T08:30:00+09:00"))
    assert is_kr_trading_day() is True


def test_trading_day_returns_true_post_market(mock_now):
    """장 마감 후라도 거래일이면 True (16:00 잡 등에서 사용)."""
    from src.bot.scheduler import is_kr_trading_day
    mock_now(datetime.fromisoformat("2026-05-04T16:00:00+09:00"))
    assert is_kr_trading_day() is True


def test_trading_day_false_on_weekend(mock_now):
    from src.bot.scheduler import is_kr_trading_day
    mock_now(datetime.fromisoformat("2026-05-09T10:00:00+09:00"))  # 토요일
    assert is_kr_trading_day() is False

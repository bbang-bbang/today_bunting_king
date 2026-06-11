"""분봉 수집 잡(job_minute_collect) 테스트.

장마감 후 15:40 당일 분봉을 유니버스 종목에 대해 수집 → 다음날 추천의 분봉 expert 피드.
휴장일 스킵 · 빈 유니버스 스킵 · 정상 시 fetch_ohlcv_minute.run 호출 검증.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import src.bot.scheduler as scheduler


def _ctx():
    return SimpleNamespace(bot=SimpleNamespace())


def test_skips_on_holiday():
    called = {"run": False}

    def fake_run(as_of, codes=None):
        called["run"] = True
        return 0

    with patch.object(scheduler, "is_kr_trading_day", return_value=False), \
         patch("src.crawlers.fetch_ohlcv_minute.run", side_effect=fake_run):
        asyncio.run(scheduler.job_minute_collect(_ctx()))

    assert called["run"] is False


def test_skips_on_empty_universe():
    called = {"run": False}

    def fake_run(as_of, codes=None):
        called["run"] = True
        return 0

    with patch.object(scheduler, "is_kr_trading_day", return_value=True), \
         patch.object(scheduler, "_list_candidate_codes", return_value=[]), \
         patch("src.crawlers.fetch_ohlcv_minute.run", side_effect=fake_run):
        asyncio.run(scheduler.job_minute_collect(_ctx()))

    assert called["run"] is False


def test_collects_universe_codes():
    captured = {}

    def fake_run(as_of, codes=None):
        captured["as_of"] = as_of
        captured["codes"] = codes
        return len(codes) * 175

    universe = ["005930", "000660", "207940"]
    with patch.object(scheduler, "is_kr_trading_day", return_value=True), \
         patch.object(scheduler, "_list_candidate_codes", return_value=universe), \
         patch("src.crawlers.fetch_ohlcv_minute.run", side_effect=fake_run):
        asyncio.run(scheduler.job_minute_collect(_ctx()))

    assert captured["codes"] == universe  # 유니버스 종목 그대로 전달
    import datetime as _dt
    assert isinstance(captured["as_of"], _dt.date)  # 당일 날짜로 수집


def test_swallows_fetch_errors():
    """수집 실패가 잡을 죽이지 않음 (예외 삼킴)."""
    def boom(as_of, codes=None):
        raise RuntimeError("KIS 다운")

    with patch.object(scheduler, "is_kr_trading_day", return_value=True), \
         patch.object(scheduler, "_list_candidate_codes", return_value=["005930"]), \
         patch("src.crawlers.fetch_ohlcv_minute.run", side_effect=boom):
        # 예외가 전파되지 않아야 함
        asyncio.run(scheduler.job_minute_collect(_ctx()))


def test_registered_in_job_queue():
    """register_jobs 가 minute_collect 를 15:40 평일로 등록."""
    import inspect
    src = inspect.getsource(scheduler.register_jobs)
    assert "job_minute_collect" in src
    assert 'name="minute_collect"' in src
    assert "time(15, 40" in src

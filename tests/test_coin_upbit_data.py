"""Upbit ohlcv 다운로더 단위 테스트 (httpx mock)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest


def _fake_candle(ts_utc: str, o: int, h: int, l: int, c: int, v: float = 1.0) -> dict:
    return {
        "market": "KRW-BTC",
        "candle_date_time_utc": ts_utc,
        "opening_price": o,
        "high_price": h,
        "low_price": l,
        "trade_price": c,
        "candle_acc_trade_volume": v,
        "timestamp": 0,
    }


def test_fetch_candles_paginates_when_count_over_200(monkeypatch):
    """count=300 → 200 + 100 두 번 호출 = 300 캔들 반환."""
    from src.coin import upbit_data

    calls = []
    base_ts = pd.Timestamp("2026-05-04T00:00:00", tz="UTC")
    def fake_get(url, **kw):
        params = kw.get("params", {})
        calls.append(params)
        n = 200 if len(calls) == 1 else 100
        offset = 0 if len(calls) == 1 else 200
        page = [
            _fake_candle(
                (base_ts - pd.Timedelta(hours=offset + i)).strftime("%Y-%m-%dT%H:%M:%S"),
                100, 101, 99, 100,
            )
            for i in range(n)
        ]
        class FakeRes:
            def raise_for_status(self): pass
            def json(self): return page
        return FakeRes()

    monkeypatch.setattr(upbit_data.httpx, "get", fake_get)
    monkeypatch.setattr(upbit_data.time, "sleep", lambda s: None)

    df = upbit_data.fetch_candles("KRW-BTC", unit_min=60, count=300)
    assert len(calls) == 2
    assert calls[0]["count"] == 200
    assert calls[1]["count"] == 100
    # 페이징 후 cursor 가 첫 페이지 마지막 ts 로 넘어가야
    assert "to" in calls[1]
    assert len(df) == 300
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]


def test_fetch_candles_invalid_unit_raises():
    from src.coin import upbit_data
    with pytest.raises(ValueError):
        upbit_data.fetch_candles("KRW-BTC", unit_min=2)


def test_save_and_load_csv_roundtrip(tmp_path):
    from src.coin import upbit_data
    df = pd.DataFrame({
        "open": [100, 101, 102],
        "high": [103, 104, 105],
        "low": [99, 100, 101],
        "close": [102, 103, 104],
        "volume": [1.5, 2.0, 1.8],
    }, index=pd.to_datetime([
        "2026-05-04T00:00:00", "2026-05-04T01:00:00", "2026-05-04T02:00:00",
    ], utc=True))
    df.index.name = "timestamp"

    path = tmp_path / "test.csv"
    upbit_data.save_to_csv(df, path)
    assert path.exists()

    df2 = upbit_data.load_from_csv(path)
    assert len(df2) == 3
    assert list(df2.columns) == ["open", "high", "low", "close", "volume"]
    assert df2.index.tz is not None

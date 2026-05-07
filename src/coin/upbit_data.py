"""Upbit 공개 API — ohlcv 다운로드 (인증 X).

API ref: https://docs.upbit.com/reference/market

페이징: 한 번에 최대 200 캔들. 90일 분봉(60min) = ~2160 캔들 → 11번 호출.
사용 예:
    from src.coin.upbit_data import fetch_candles, save_to_csv
    df = fetch_candles("KRW-BTC", unit_min=60, count=2160)
    save_to_csv(df, "data/upbit/KRW-BTC_60min.csv")
"""
from __future__ import annotations

import csv
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

log = logging.getLogger("bunting.coin.upbit")

UPBIT_BASE = "https://api.upbit.com/v1"


def fetch_candles(
    market: str,
    unit_min: int = 60,        # 1, 3, 5, 15, 30, 60, 240
    count: int = 2160,         # 총 받고 싶은 캔들 수
    to: str | None = None,     # ISO datetime; 이 시각 이전 캔들들. None = 최신
) -> pd.DataFrame:
    """Upbit 분봉 OHLCV 다운로드. count 가 200 초과면 자동 페이징.

    Returns DataFrame index=datetime(UTC) cols: open, high, low, close, volume.
    """
    if unit_min not in (1, 3, 5, 15, 30, 60, 240):
        raise ValueError(f"unit_min 허용값: 1/3/5/15/30/60/240, got {unit_min}")
    endpoint = f"{UPBIT_BASE}/candles/minutes/{unit_min}"

    all_rows: list[dict] = []
    cursor = to
    remaining = count
    while remaining > 0:
        page_count = min(200, remaining)
        params = {"market": market, "count": page_count}
        if cursor:
            params["to"] = cursor
        try:
            res = httpx.get(endpoint, params=params, timeout=10)
            res.raise_for_status()
            page = res.json()
        except httpx.HTTPError as e:
            log.warning("[upbit %s] %d-min 다운로드 실패: %s", market, unit_min, e)
            break
        if not page:
            break

        all_rows.extend(page)
        # 페이지 마지막 캔들의 시각 = 다음 호출의 to
        last_ts = page[-1]["candle_date_time_utc"]   # "2026-05-04T01:00:00"
        cursor = last_ts
        remaining -= len(page)

        # rate limit 보호 — Upbit 분당 600회 무료, 보수적으로 0.1s sleep
        time.sleep(0.1)

    if not all_rows:
        return pd.DataFrame(
            columns=["open", "high", "low", "close", "volume"],
        )

    df = pd.DataFrame(all_rows)
    df["timestamp"] = pd.to_datetime(df["candle_date_time_utc"], utc=True)
    df = df.set_index("timestamp").sort_index()
    df = df.rename(columns={
        "opening_price": "open",
        "high_price": "high",
        "low_price": "low",
        "trade_price": "close",
        "candle_acc_trade_volume": "volume",
    })[["open", "high", "low", "close", "volume"]]

    # 중복 제거 (페이지 경계 중복 가능)
    df = df[~df.index.duplicated(keep="last")]
    return df


def save_to_csv(df: pd.DataFrame, path: str | Path) -> None:
    """DataFrame 을 CSV 로 저장 (백테스트 캐시)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=True, index_label="timestamp")
    log.info("upbit ohlcv 저장: %s (%d rows)", p, len(df))


def load_from_csv(path: str | Path) -> pd.DataFrame:
    """저장된 CSV → DataFrame (timestamp index, UTC)."""
    p = Path(path)
    df = pd.read_csv(p, parse_dates=["timestamp"], index_col="timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def list_markets(quote: str = "KRW") -> list[str]:
    """KRW 페어 시장 코드 목록 (KRW-BTC, KRW-ETH ...)"""
    res = httpx.get(f"{UPBIT_BASE}/market/all", timeout=10, params={"isDetails": "false"})
    res.raise_for_status()
    return [m["market"] for m in res.json() if m["market"].startswith(quote + "-")]

"""Fear & Greed Index — alternative.me 무료 API.

API: https://api.alternative.me/fng/
응답: {"data": [{"value": "32", "value_classification": "Fear", "timestamp": "...", ...}, ...]}

해석:
  0~24    : Extreme Fear  (매수 기회로 해석되곤 함)
  25~49   : Fear
  50~74   : Greed
  75~100  : Extreme Greed (조정 위험)

봇 사용:
  - 현재 값 < 30 = 매수 우호 시그널 (오버솔드 sentiment)
  - 현재 값 > 75 = 매수 회피 (과열)
  - 단독 시그널은 약함 → 다른 expert 와 앙상블해야
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

log = logging.getLogger("bunting.coin.fng")

FNG_BASE = "https://api.alternative.me/fng/"


def fetch_fear_greed_history(days: int = 90) -> pd.DataFrame:
    """최근 N일 Fear & Greed 일별 시계열.

    Returns DataFrame index=date(UTC midnight), cols: value (0~100), classification.
    """
    try:
        res = httpx.get(FNG_BASE, params={"limit": days}, timeout=10)
        res.raise_for_status()
        data = res.json().get("data", [])
    except httpx.HTTPError as e:
        log.warning("FNG 다운로드 실패: %s", e)
        return pd.DataFrame(columns=["value", "classification"])

    rows = []
    for d in data:
        try:
            ts = datetime.fromtimestamp(int(d["timestamp"]), tz=timezone.utc)
            rows.append({
                "date": ts.date(),
                "value": int(d["value"]),
                "classification": d.get("value_classification", ""),
            })
        except (ValueError, KeyError):
            continue
    if not rows:
        return pd.DataFrame(columns=["value", "classification"])

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True)
    df = df.set_index("date").sort_index()
    return df[["value", "classification"]]


def save_fng_to_csv(df: pd.DataFrame, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(p, index=True, index_label="date")


def load_fng_from_csv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    df = pd.read_csv(p, parse_dates=["date"], index_col="date")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def fng_to_hourly_index(daily_fng: pd.DataFrame, ohlcv_index: pd.DatetimeIndex) -> pd.Series:
    """일별 FNG 를 시간봉 인덱스에 forward-fill — 백테스트에서 캔들 단위로 lookup.

    같은 날 캔들들엔 같은 FNG 값. 새 일 시작 시 새 값.
    """
    if daily_fng.empty:
        return pd.Series([50] * len(ohlcv_index), index=ohlcv_index, dtype=int)

    # 시간봉 캔들의 날짜 컴포넌트로 매핑
    fng_by_date = daily_fng["value"].to_dict()
    # ohlcv_index 의 각 시각의 date()
    keys = pd.DatetimeIndex(ohlcv_index).normalize()
    values = [fng_by_date.get(k, 50) for k in keys]
    return pd.Series(values, index=ohlcv_index, dtype=int)

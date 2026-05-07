"""김치 프리미엄 = (Upbit KRW 가격 / (Binance USDT × 환율) - 1) × 100.

해석:
  + 큰 값 (3%+): 한국이 비쌈 → 매도 압력 또는 매수 가속 신호
  - 음수: 한국이 더 쌈 → 외국인 차익거래 매수 압력 가능

자동매매에서:
  - 김프 너무 높을 때 (5%+) 매수 자제 (역사적으로 조정 잦음)
  - 김프 평균 이하로 떨어졌을 때 (예: 직전 7일 평균 - 1σ) = 매수 우호

데이터 소스:
  - Upbit: src.coin.upbit_data 재사용 (KRW-BTC 가격)
  - Binance: 무료 API (https://api.binance.com/api/v3/klines, 인증 X)
  - USD/KRW 환율: 한국은행 또는 무료 API. 일단 단순화해서 봇에서 fix 또는 별도 페치
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pandas as pd

log = logging.getLogger("bunting.coin.kimchi")

BINANCE_BASE = "https://api.binance.com"


def fetch_binance_klines(
    symbol: str,         # "BTCUSDT", "ETHUSDT"
    interval: str = "1h",
    limit: int = 1000,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> pd.DataFrame:
    """Binance 시간봉 데이터.

    Returns DataFrame index=UTC, cols: open/high/low/close/volume.
    """
    params = {"symbol": symbol, "interval": interval, "limit": min(limit, 1000)}
    if start_ms:
        params["startTime"] = start_ms
    if end_ms:
        params["endTime"] = end_ms
    try:
        res = httpx.get(f"{BINANCE_BASE}/api/v3/klines", params=params, timeout=15)
        res.raise_for_status()
        data = res.json()
    except httpx.HTTPError as e:
        log.warning("Binance %s 다운로드 실패: %s", symbol, e)
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    if not data:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

    rows = []
    for k in data:
        # k: [open_time, open, high, low, close, volume, close_time, ...]
        ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
        rows.append({
            "timestamp": ts,
            "open": float(k[1]), "high": float(k[2]),
            "low": float(k[3]), "close": float(k[4]),
            "volume": float(k[5]),
        })
    df = pd.DataFrame(rows).set_index("timestamp").sort_index()
    return df


def fetch_usdkrw_proxy(days: int = 90) -> pd.Series:
    """USD/KRW 환율 시계열 (일별).

    무료 환율 API: https://open.exchangerate-api.com/v6/latest/USD (현재값만)
    또는 https://api.exchangerate.host/latest (대체)
    백테스트용 history 는 별도 — 여기선 단순화해서 최근 평균 환율 1개 값 반환 (1320~1380 사이).

    Returns Series index=date(UTC), value=KRW per USD.
    실전 운영 시엔 한국은행 ECOS API 등으로 정확한 일별 환율 적용해야.
    """
    # 단순화: 최근 90일 환율은 1340 ± 약간 변동으로 가정
    # 실제 R&D 단계에서는 ECOS API 또는 yfinance(KRW=X) 도입 필요
    today = datetime.now(timezone.utc).date()
    dates = pd.date_range(end=pd.Timestamp(today, tz="UTC"), periods=days, freq="D")
    # 1340 평균에 작은 변동 (±0.5%) — 실제 환율은 R&D 단계 보강 필요
    values = [1340 + (i % 7 - 3) * 2 for i in range(days)]
    return pd.Series(values, index=dates, name="usd_krw")


def kimchi_premium_series(
    upbit_krw: pd.DataFrame,        # "KRW-BTC" ohlcv (close 기준)
    binance_usdt: pd.DataFrame,     # "BTCUSDT" ohlcv (close 기준)
    usd_krw: pd.Series,             # 일별 환율
) -> pd.Series:
    """캔들마다 김치 프리미엄 % 계산.

    프리미엄 = (KRW_close / (USDT_close × USD_KRW환율) - 1) × 100

    Series index = upbit ohlcv 인덱스, value = 프리미엄 %.
    """
    if upbit_krw.empty or binance_usdt.empty:
        return pd.Series(dtype=float)

    # binance 인덱스를 upbit 와 맞춤 (timestamp 기준)
    common_idx = upbit_krw.index.intersection(binance_usdt.index)
    if len(common_idx) == 0:
        return pd.Series(dtype=float)

    krw_close = upbit_krw.loc[common_idx, "close"]
    usdt_close = binance_usdt.loc[common_idx, "close"]

    # 캔들 시각의 날짜에 해당하는 환율 lookup (일별 forward-fill)
    fx_by_date = usd_krw.to_dict()
    fx = pd.Series(
        [fx_by_date.get(pd.Timestamp(t.date(), tz="UTC"), 1340) for t in common_idx],
        index=common_idx, dtype=float,
    )

    krw_per_btc_via_usdt = usdt_close * fx
    premium_pct = (krw_close / krw_per_btc_via_usdt - 1) * 100
    return premium_pct


def save_kp_to_csv(s: pd.Series, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    s.to_csv(p, index=True, index_label="timestamp", header=["premium_pct"])


def load_kp_from_csv(path: str | Path) -> pd.Series:
    p = Path(path)
    df = pd.read_csv(p, parse_dates=["timestamp"], index_col="timestamp")
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df["premium_pct"]

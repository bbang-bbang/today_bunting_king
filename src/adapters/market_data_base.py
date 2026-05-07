"""시세 데이터 소스 추상 인터페이스.

pykrx / KIS / yfinance 등 어떤 소스든 이 인터페이스를 구현하면 교체 가능.
수집 파이프라인은 구체 소스를 모른다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Instrument:
    code: str
    name: str
    market: str          # 'KOSPI' | 'KOSDAQ'
    sector: str | None = None


@dataclass(frozen=True)
class OHLCV:
    code: str
    date: date
    open: int
    high: int
    low: int
    close: int
    volume: int
    value: int | None = None         # 거래대금 (소스에 따라 누락 가능)
    change_pct: float | None = None


class MarketDataSource(ABC):
    """시세/종목마스터 조회 인터페이스 (읽기 전용)."""

    @abstractmethod
    def list_instruments(self, as_of: date, market: str) -> list[Instrument]:
        """특정 날짜 기준 해당 시장의 전 종목 리스트."""
        ...

    @abstractmethod
    def fetch_ohlcv(self, code: str, start: date, end: date) -> list[OHLCV]:
        """종목의 [start, end] 구간 일봉. end 포함."""
        ...


from datetime import datetime as _datetime  # noqa: E402


@dataclass(frozen=True)
class MinuteBar:
    code: str
    datetime: _datetime
    open: int
    high: int
    low: int
    close: int
    volume: int


@dataclass(frozen=True)
class CurrentPrice:
    """종목 현재가 스냅샷."""
    code: str
    price: int              # 현재가
    open: int               # 당일 시가
    high: int               # 당일 고가
    low: int                # 당일 저가
    prev_close: int         # 전일 종가
    volume: int             # 당일 누적 거래량
    change_pct: float       # 전일 대비 등락률 (%)
    fetched_at: _datetime   # 조회 시각

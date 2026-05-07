"""pykrx 기반 시세 데이터 소스."""
from __future__ import annotations

from datetime import date, timedelta

from pykrx import stock

from src.adapters.market_data_base import Instrument, MarketDataSource, OHLCV

_MARKET_CODES = ("KOSPI", "KOSDAQ")


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


class PyKRXMarketDataSource(MarketDataSource):
    def list_instruments(self, as_of: date, market: str) -> list[Instrument]:
        if market not in _MARKET_CODES:
            raise ValueError(f"market must be one of {_MARKET_CODES}, got {market}")

        # 휴장일이면 빈 리스트가 오므로 최대 10영업일 전까지 거슬러 올라감
        d = as_of
        for _ in range(10):
            tickers = stock.get_market_ticker_list(_yyyymmdd(d), market=market)
            if tickers:
                break
            d -= timedelta(days=1)
        else:
            return []

        out: list[Instrument] = []
        for code in tickers:
            name = stock.get_market_ticker_name(code)
            out.append(Instrument(code=code, name=name, market=market))
        return out

    def fetch_ohlcv(self, code: str, start: date, end: date) -> list[OHLCV]:
        if start > end:
            return []
        df = stock.get_market_ohlcv(_yyyymmdd(start), _yyyymmdd(end), code)
        if df is None or df.empty:
            return []

        out: list[OHLCV] = []
        for idx, row in df.iterrows():
            # pykrx: Index 는 Timestamp, 컬럼은 한글 (시가/고가/저가/종가/거래량/등락률)
            d = idx.date() if hasattr(idx, "date") else idx
            out.append(OHLCV(
                code=code,
                date=d,
                open=int(row["시가"]),
                high=int(row["고가"]),
                low=int(row["저가"]),
                close=int(row["종가"]),
                volume=int(row["거래량"]),
                value=None,
                change_pct=float(row.get("등락률", 0.0)) if row.get("등락률") is not None else None,
            ))
        return out
